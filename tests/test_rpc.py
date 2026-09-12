"""RPC sidecar 端到端测试：起真实子进程，校验协议帧与 stdio 洁净性。

对应 docs/迁移计划书 §13（stdio 洁净性）与 §17.1（RPC 层测试）。
测试会真的拉起 `python -m rpc.server`，故仅 macOS 可跑；且会调用权限检测
API（仅查询，不弹窗）。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="sidecar 仅支持 macOS"
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SIDE = [sys.executable, "-m", "rpc.server"]


def _cfg_file() -> str:
    """定时配置文件路径。

    必须走 core.paths.app_dir()（受 conftest 的 AUTOFLOW_DATA_DIR 控制）：
    此前硬编码 REPO/config.json，而开发态 config.json 就落在仓库根目录，
    测试开头会把它删掉——一旦用户配过定时运行，跑一次测试就丢配置。
    """
    from core import paths

    return os.path.join(paths.app_dir(), "config.json")


def _start() -> subprocess.Popen:
    return subprocess.Popen(
        _SIDE,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


def _rpc(p: subprocess.Popen, method: str, params=None, req_id=None) -> None:
    msg = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        msg["id"] = req_id
    if params is not None:
        msg["params"] = params
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()


def _read_json(p: subprocess.Popen, timeout: float = 5.0) -> dict:
    """读取一行并解析为 dict；非合法 JSON 会冒泡使测试失败（洁净性自检）。"""
    end = time.time() + timeout
    while time.time() < end:
        line = p.stdout.readline()
        if line:
            return json.loads(line)
    raise TimeoutError("sidecar 未在超时内返回帧")


def test_init_output_keeps_protocol_channel_clean_of_c_level_stdout() -> None:
    """绕过 Python 直接写 fd 1 的字节必须落到 stderr，不能混进协议帧流。

    回归：`_init_output` 原先只把 `sys.stdout` 指向 stderr，fd 1 仍指向协议管道。
    第三方库的 C++ 层告警 / printf 会原样插进 NDJSON 流，前端随机 JSON 解析失败，
    表现为「通知/响应偶发丢失」——排查方向完全跑偏。
    """
    code = "\n".join([
        "import os, sys",
        f"sys.path.insert(0, {REPO!r})",
        "from rpc import server",
        "server._init_output()",
        # 模拟 C 层直接写 fd 1（不经 Python 的 sys.stdout）
        "os.write(1, b'C_LEVEL_NOISE\\n')",
        'server.OUT.write(b\'{"jsonrpc":"2.0","id":1,"result":{}}\\n\')',
    ])
    p = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO,
        capture_output=True, text=True, timeout=15,
    )
    assert p.returncode == 0, p.stderr
    # 协议帧独占 stdout，且是干净可解析的一行
    assert "C_LEVEL_NOISE" not in p.stdout
    assert json.loads(p.stdout.strip()) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    # C 层噪音被赶到 stderr（Rust 侧作为 sidecar_stderr 记录，可见可查）
    assert "C_LEVEL_NOISE" in p.stderr


def test_app_info_and_protocol() -> None:
    p = _start()
    try:
        _rpc(p, "app.info", req_id=1)
        resp = _read_json(p)
        assert resp.get("id") == 1
        assert "result" in resp
        r = resp["result"]
        assert r["protocolVersion"] == 1
        assert r["platform"] == "macos"
        # 三项权限均存在且为布尔
        perms = r["permissions"]
        assert set(perms) == {"accessibility", "inputMonitoring", "screenRecording"}
        assert all(isinstance(v, bool) for v in perms.values())
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


def test_unknown_method_and_parse_error() -> None:
    p = _start()
    try:
        # 未知方法 -> -32601
        _rpc(p, "nope", req_id=2)
        err = _read_json(p)
        assert err["id"] == 2
        assert err["error"]["code"] == -32601

        # 非法 JSON -> -32700
        p.stdin.write("{ this is not json }\n")
        p.stdin.flush()
        perr = _read_json(p)
        assert perr["error"]["code"] == -32700
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


def test_stdout_is_clean_json_only() -> None:
    """整段生命周期内 stdout 不应出现任何非 JSON 行；日志/print 必须走 stderr。"""
    p = _start()
    try:
        _rpc(p, "app.info", req_id=1)
        _read_json(p)
        _rpc(p, "app.diagnose", req_id=9)
        diag = _read_json(p)
        assert diag["id"] == 9 and "result" in diag
        assert isinstance(diag["result"]["dataDir"], str)
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)

    # 进程已退出，读尽 stdout / stderr
    out = p.stdout.read()
    err = p.stderr.read()
    for i, line in enumerate(out.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except Exception as e:
            raise AssertionError(f"stdout 第 {i} 行不是合法 JSON: {line!r}") from e
    assert out.strip(), "未产生任何协议帧"

    # app.info 等的正常帧不应出现在 stderr（stderr 只承载日志，可空）
    assert "jsonrpc" not in err, "协议帧泄漏到了 stderr"


# --------------------------------------------------------------------------- #
# P1-1：后端工作流真源 + workflow/node/nodes RPC（§9/§10）
# --------------------------------------------------------------------------- #
def _call(p: subprocess.Popen, method: str, params=None, req_id=1) -> dict:
    """发送请求并读回对应 id 的响应；途中出现的通知（如 workflow.changed）会被跳过。"""
    _rpc(p, method, params, req_id)
    while True:
        frame = _read_json(p)
        if frame.get("id") == req_id:
            return frame
        # 其余视为通知（method 无 id），跳过


def test_workflow_new_and_node_crud() -> None:
    p = _start()
    try:
        # 空工作流
        cur = _call(p, "workflow.current", req_id=1)
        assert cur["id"] == 1
        assert cur["result"]["workflow"]["nodes"] == []
        assert cur["result"]["path"] is None

        # 新增两个节点
        a = _call(p, "node.add", {"type": "mouse", "index": 0}, req_id=2)
        assert a["id"] == 2 and a["result"]["index"] == 0
        assert a["result"]["node"]["type"] == "mouse"
        b = _call(p, "node.add", {"type": "keyboard"}, req_id=3)
        assert b["id"] == 3 and b["result"]["index"] == 1

        # 改参 + 启停
        t = _call(p, "node.toggle", {"index": 0, "enabled": False}, req_id=4)
        assert t["id"] == 4 and t["result"]["workflow"]["nodes"][0]["enabled"] is False
        ps = _call(p, "node.params.set", {"index": 0, "key": "speed", "value": 2.5}, req_id=5)
        assert ps["id"] == 5 and ps["result"]["workflow"]["nodes"][0]["params"]["speed"] == 2.5

        # 移动：index 1 -> 0
        mv = _call(p, "node.move", {"index": 1, "to": 0}, req_id=6)
        assert mv["id"] == 6
        types = [n["type"] for n in mv["result"]["workflow"]["nodes"]]
        assert types == ["keyboard", "mouse"]

        # 删除
        rm = _call(p, "node.remove", {"index": 0}, req_id=7)
        assert rm["id"] == 7
        assert [n["type"] for n in rm["result"]["workflow"]["nodes"]] == ["mouse"]

        # workflow.update 改名字与速度
        up = _call(p, "workflow.update", {"patch": {"name": "测试流", "speed": 1.5, "repeat": 3}}, req_id=8)
        assert up["id"] == 8
        wf = up["result"]["workflow"]
        assert wf["name"] == "测试流" and wf["speed"] == 1.5 and wf["repeat"] == 3

        # nodes.definitions 含 common_params，且顺序严格等于 §9.4 的菜单顺序
        defs = _call(p, "nodes.definitions", req_id=9)
        assert defs["id"] == 9
        assert [d["type"] for d in defs["result"]] == [
            "mouse", "keyboard", "delay", "record_replay",
            "image_click", "ocr_click", "yolo_click", "condition", "note",
        ]
        # 顺序由 definition()['order'] 提供，前端不再自己排
        assert [d["order"] for d in defs["result"]] == sorted(
            d["order"] for d in defs["result"])
        assert all("common_params" in d and any(c["key"] == "run_when" for c in d["common_params"]) for d in defs["result"])
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


def test_workflow_error_codes() -> None:
    p = _start()
    try:
        # 未知节点类型 -> -32602
        e1 = _call(p, "node.add", {"type": "nope"}, req_id=1)
        assert e1["id"] == 1 and e1["error"]["code"] == -32602

        # 索引越界 -> -32602
        e2 = _call(p, "node.remove", {"index": 99}, req_id=2)
        assert e2["id"] == 2 and e2["error"]["code"] == -32602

        # 保存未指定路径 -> -32602
        e3 = _call(p, "workflow.save", req_id=3)
        assert e3["id"] == 3 and e3["error"]["code"] == -32602

        # 加载不存在文件 -> -32602（data 指名 path）
        e4 = _call(p, "workflow.load", {"path": "/tmp/__no_such__.json"}, req_id=4)
        assert e4["id"] == 4 and e4["error"]["code"] == -32602
        assert e4["error"]["data"]["path"] == "/tmp/__no_such__.json"
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


# --------------------------------------------------------------------------- #
# P1-2：run/record RPC（§5/§9.5）
# --------------------------------------------------------------------------- #
def test_run_and_record_error_codes() -> None:
    p = _start()
    try:
        # 空工作流运行 -> -32004
        e1 = _call(p, "run.start", req_id=1)
        assert e1["id"] == 1 and e1["error"]["code"] == -32004

        # 录制前 toNode -> -32003
        e2 = _call(p, "record.toNode", req_id=2)
        assert e2["id"] == 2 and e2["error"]["code"] == -32003

        # 未录制就 stop -> -32003
        e3 = _call(p, "record.stop", req_id=3)
        assert e3["id"] == 3 and e3["error"]["code"] == -32003

        # 重复 record.start -> -32001（already_running）
        _call(p, "record.start", req_id=4)
        e4 = _call(p, "record.start", req_id=5)
        assert e4["id"] == 5 and e4["error"]["code"] == -32001
        # 录制中运行 -> -32002（busy_recording）
        e5 = _call(p, "run.start", req_id=6)
        assert e5["id"] == 6 and e5["error"]["code"] == -32002
        _call(p, "record.stop", req_id=7)
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


def test_record_to_node_roundtrip() -> None:
    """真实起停 Recorder（需要辅助功能/输入监控授权），录一小段再写回节点。"""
    p = _start()
    try:
        _call(p, "record.start", req_id=1)
        time.sleep(0.3)  # 让监听线程抓到若干事件（无操作也会落移动/原点）
        stopped = _call(p, "record.stop", req_id=2)
        assert stopped["id"] == 2 and stopped["result"]["count"] >= 0
        # 写回节点
        to_node = _call(p, "record.toNode", req_id=3)
        assert to_node["id"] == 3
        node = to_node["result"]["node"]
        assert node["type"] == "record_replay"
        assert "events" in node["params"] and node["params"]["use_relative"] is True
        # 节点已进工作流
        cur = _call(p, "workflow.current", req_id=4)
        assert len(cur["result"]["workflow"]["nodes"]) == 1
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


# --------------------------------------------------------------------------- #
# P1-3：热键 / 键盘捕获 / 取点 / 定时（§3.2 + §9.2）
# --------------------------------------------------------------------------- #
def test_hotkey_set_clear_and_key_capture() -> None:
    p = _start()
    try:
        # F9/F10/F11 -> record/run/pick
        s = _call(p, "hotkey.set", {"actions": ["record", "run", "pick"]}, req_id=1)
        assert s["id"] == 1
        assert s["result"]["hotkeys"] == ["record", "run", "pick"]

        # 解除绑定
        c = _call(p, "hotkey.clear", req_id=2)
        assert c["id"] == 2
        assert c["result"]["hotkeys"] == [None, None, None]

        # 键盘捕获起停
        cap = _call(p, "key.capture", req_id=3)
        assert cap["id"] == 3 and cap["result"]["capturing"] is True
        stop = _call(p, "key.capture.stop", req_id=4)
        assert stop["id"] == 4 and stop["result"]["capturing"] is False
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


def test_base_pick_returns_coords() -> None:
    """取点为 arm 流程：base.pick 应答 armed，随后按键（直接驱动控制器回调）
    经 base.picked 通知带回事件自带坐标。"""
    from rpc.controller import AppController
    ctrl = AppController()
    collected = []
    ctrl.set_notifier(lambda m, prm: collected.append((m, prm)))
    r = ctrl.base_pick()
    assert r == {"armed": True}
    # 模拟一次按键（坐标来自键盘事件 location）
    ctrl._on_key("F11", True, 640, 400)
    methods = [m for m, _ in collected]
    assert "base.picked" in methods
    payload = dict(collected)["base.picked"]
    assert payload["x"] == 640 and payload["y"] == 400
    assert ctrl._picking_once is False  # 一次性
    ctrl.shutdown()


def test_schedule_configure_and_get() -> None:
    """schedule.configure/get 闭环 + 持久化到 config.json（§9.2）。"""
    cfg_file = _cfg_file()
    if os.path.exists(cfg_file):
        os.remove(cfg_file)
    p = _start()
    try:
        cfg = {"mode": "每天时刻", "atTime": "09:00", "intervalMin": 30,
               "workflowPath": "", "enabled": False}
        r = _call(p, "schedule.configure", cfg, req_id=1)
        assert r["id"] == 1
        assert "result" in r and "error" not in r
        got = _call(p, "schedule.get", req_id=2)
        assert got["id"] == 2
        assert got["result"]["schedule"]["mode"] == "每天时刻"
        assert got["result"]["schedule"]["enabled"] is False
        assert isinstance(got["result"]["nextFire"], str)
        # 退出后配置应落盘
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)
        assert os.path.exists(cfg_file)
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("schedule", {}).get("mode") == "每天时刻"
    finally:
        if os.path.exists(cfg_file):
            os.remove(cfg_file)


def test_schedule_fire_reloads_workflow(tmp_path) -> None:
    """直接驱动控制器：定时触发从磁盘重载工作流并广播 changed/fired（无真实计时等待）。"""
    from rpc.controller import AppController
    cfg_file = _cfg_file()
    if os.path.exists(cfg_file):
        os.remove(cfg_file)  # 避免启动时装定残留配置
    ctrl = AppController()
    collected = []
    ctrl.set_notifier(lambda m, p: collected.append((m, p)))
    wf_path = tmp_path / "wf.json"
    wf_path.write_text(json.dumps({
        "name": "sched", "speed": 1.0, "repeat": 1,
        "nodes": [{"type": "mouse", "params": {}, "enabled": True}],
    }))
    ctrl.schedule_configure({"mode": "固定间隔", "intervalMin": 1,
                             "workflowPath": str(wf_path), "enabled": True})
    ctrl._schedule_cancel_timer()  # 取下真实计时器，避免测试后残留线程
    ctrl._schedule_fire()          # 直接模拟触发
    assert ctrl.path == str(wf_path)
    assert ctrl.workflow.name == "sched"
    methods = [m for m, _ in collected]
    assert "workflow.changed" in methods
    assert "schedule.fired" in methods
    ctrl.shutdown()
    if os.path.exists(cfg_file):
        os.remove(cfg_file)


def test_schedule_fire_skips_without_touching_current_workflow(tmp_path) -> None:
    """定时触发的前置校验：不满足条件时既不运行、也不覆盖用户正在编辑的工作流。

    回归：此前是「先替换 self.workflow，再 run_start」——忙时虽然跳过了运行，
    但编辑态已被定时脚本覆盖；文件缺失/损坏时更糟：会直接跑内存里那个
    完全不相干的工作流。
    """
    from rpc.controller import AppController
    from core.events import Node

    ctrl = AppController()
    collected = []
    ctrl.set_notifier(lambda m, p: collected.append((m, p)))
    ctrl.workflow.name = "用户正在编辑"
    ctrl.path = "/tmp/user-editing.json"
    ctrl.workflow.nodes.append(Node(type="delay", params={"ms": 1}))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"name": "定时脚本", "nodes": [{"type": "delay", "params": {}}]}))
    broken = tmp_path / "broken.json"
    broken.write_text("{ 这不是合法 JSON")

    def fired_reason() -> str:
        return [p["reason"] for m, p in collected if m == "schedule.fired"][-1]

    # 1) 运行中：不覆盖、不运行
    ctrl.schedule_configure({"mode": "固定间隔", "intervalMin": 1,
                             "workflowPath": str(good), "enabled": True})
    ctrl._schedule_cancel_timer()
    ctrl.running = True
    ctrl._schedule_fire()
    assert fired_reason() == "already_running"
    assert ctrl.workflow.name == "用户正在编辑"
    assert ctrl.path == "/tmp/user-editing.json"
    ctrl.running = False

    # 2) 文件缺失：不运行内存里的旧工作流，也不清空编辑态
    ctrl.schedule_configure({"mode": "固定间隔", "intervalMin": 1,
                             "workflowPath": str(tmp_path / "nope.json"), "enabled": True})
    ctrl._schedule_cancel_timer()
    ctrl._schedule_fire()
    assert fired_reason() == "workflow_missing"
    assert ctrl.workflow.name == "用户正在编辑"
    assert ctrl.path == "/tmp/user-editing.json"

    # 3) 文件损坏：同上
    ctrl.schedule_configure({"mode": "固定间隔", "intervalMin": 1,
                             "workflowPath": str(broken), "enabled": True})
    ctrl._schedule_cancel_timer()
    ctrl._schedule_fire()
    assert fired_reason() == "workflow_load_failed"
    assert ctrl.workflow.name == "用户正在编辑"

    # 4) 正常路径才替换并运行
    ctrl.schedule_configure({"mode": "固定间隔", "intervalMin": 1,
                             "workflowPath": str(good), "enabled": True})
    ctrl._schedule_cancel_timer()
    ctrl._schedule_fire()
    assert ctrl.workflow.name == "定时脚本"
    assert ctrl.path == str(good)
    assert [p for m, p in collected if m == "schedule.fired"][-1]["ran"] is True
    ctrl.shutdown(timeout=2.0)


def test_shutdown_stops_running_workflow() -> None:
    """退出时必须停执行器并等运行线程收尾，否则会残留未释放的按键/鼠标键。"""
    import tasks.base as tb
    from rpc.controller import AppController
    from core.events import Node

    class _SlowShutdown(tb.BaseTask):
        type = "_test_slow_shutdown"
        name = "slow"
        def run(self, ctx):
            for _ in range(500):
                if ctx.stopping:
                    return
                time.sleep(0.01)

    tb.register(_SlowShutdown())
    ctrl = AppController()
    ctrl.workflow.nodes.append(Node(type="_test_slow_shutdown"))
    ctrl.run_start()
    assert ctrl.running is True
    time.sleep(0.05)  # 让运行线程真正进入任务
    ctrl.shutdown(timeout=2.0)
    assert ctrl.running is False
    assert ctrl.executor.running is False
    assert ctrl._run_thread is None


def test_shutdown_stops_recorder() -> None:
    """退出时录制器必须被停掉（释放 CGEventTap），不能留在监听状态。"""
    from rpc.controller import AppController

    class _FakeRec:
        def __init__(self):
            self.stopped = 0
        def start(self):
            pass
        def poll(self):
            return []
        def stop(self):
            self.stopped += 1

    ctrl = AppController()
    rec = _FakeRec()
    ctrl.recorder = rec
    ctrl.recording = True
    ctrl._rec_poller_rec = rec
    ctrl.shutdown(timeout=1.0)
    assert rec.stopped == 1
    assert ctrl.recording is False
    assert ctrl.recorder is None


def _drain(q: "queue.Queue[dict]") -> list:
    out = []
    while True:
        try:
            item = q.get_nowait()
            q.task_done()
            out.append(item)
        except queue.Empty:
            return out


def test_notify_queue_backpressure_drops_only_droppable() -> None:
    """队列满时只丢「可丢类」，状态类通知必须送达。

    回归：此前无差别 `get_nowait()` 丢最旧，与注释声称的「run/record 类保留」不符——
    一次 record.event 洪水就能把 run.finished / workflow.changed 挤掉，
    前端随后永久停在旧状态（看起来像「通知偶发丢失」，实为策略写错）。
    """
    from rpc import server

    q = server._notify_queue
    _drain(q)  # 清掉可能残留的队列内容

    # 1) 可丢类：队列满时放弃新来的，已入队的一条都不动
    for i in range(q.maxsize):
        server._send_notification("run.progress", {"done": i, "total": 1})
    assert q.full()
    server._send_notification("record.event", {"events": []})
    items = _drain(q)
    assert len(items) == q.maxsize
    assert items[0]["params"]["done"] == 0          # 最旧的仍在
    assert all(it["method"] == "run.progress" for it in items)  # 洪水没挤掉任何一条

    # 2) 状态类：必须送达，代价是挤掉最旧一条
    for i in range(q.maxsize):
        server._send_notification("run.progress", {"done": i, "total": 1})
    server._send_notification("run.finished", {"stopped": False})
    items = _drain(q)
    assert len(items) == q.maxsize
    assert items[-1]["method"] == "run.finished"
    assert items[0]["params"]["done"] == 1          # 只有最旧那条被挤掉

    # 3) 计数器配对：丢弃路径也要 task_done，否则 unfinished_tasks 只增不减
    assert q.unfinished_tasks == 0


def test_record_subscribe_gates_event_stream() -> None:
    """§3.2：record.subscribe 切换订阅态；未订阅时 _record_poll 不推送 record.event。

    直接驱动控制器（无真实 Recorder / 权限依赖），用替身 recorder 验证逻辑门控。
    """
    from rpc.controller import AppController
    from core.events import MacroEvent

    cfg_file = _cfg_file()
    if os.path.exists(cfg_file):
        os.remove(cfg_file)  # 避免启动装定残留定时配置

    class _FakeRec:
        def __init__(self, ctrl, events):
            self._ctrl = ctrl
            self._events = events
            self.poll_calls = 0

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return self._events
            # 第二轮让轮询循环退出（模拟 record_stop 置空 running 态）
            self._ctrl.recording = False
            return []

    def _run_once(ctrl, events):
        ctrl.recording = True
        rec = _FakeRec(ctrl, events)
        ctrl._rec_poller_rec = rec
        ctrl._record_poll()
        return rec

    ctrl = AppController()
    collected = []
    ctrl.set_notifier(lambda m, p: collected.append((m, p)))

    # 默认未订阅；开关返回正确
    assert ctrl._record_subscribed is False
    assert ctrl.record_subscribe(True) == {"subscribed": True}
    assert ctrl._record_subscribed is True
    assert ctrl.record_subscribe(False) == {"subscribed": False}
    assert ctrl._record_subscribed is False

    # 门控：未订阅时 _record_poll 不应推送 record.event
    _run_once(ctrl, [MacroEvent(ts_ms=0, kind="move", x=1, y=2)])
    assert all(m != "record.event" for m, _ in collected), "未订阅不应推送 record.event"

    # 订阅后推送 record.event
    ctrl.record_subscribe(True)
    _run_once(ctrl, [MacroEvent(ts_ms=5, kind="move", x=3, y=4)])
    assert any(m == "record.event" for m, _ in collected)
    ctrl.shutdown()
    if os.path.exists(cfg_file):
        os.remove(cfg_file)
