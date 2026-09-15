"""RPC sidecar 端到端测试：起真实子进程，校验协议帧与 stdio 洁净性。

对应 docs/迁移计划书 §13（stdio 洁净性）与 §17.1（RPC 层测试）。
测试会真的拉起 `python -m rpc.server`，故仅 macOS 可跑；且会调用权限检测
API（仅查询，不弹窗）。
"""
from __future__ import annotations

import json
import logging
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
            "start", "mouse", "keyboard", "delay", "record_replay",
            "image_click", "ocr_click", "yolo_click", "condition", "branch", "note", "end",
        ]
        # 顺序由 definition()['order'] 提供，前端不再自己排
        assert [d["order"] for d in defs["result"]] == sorted(
            d["order"] for d in defs["result"])
        # common_params 在 v4 起**故意为空**：run_when 的语义改由「边」表达
        assert all(d["common_params"] == [] for d in defs["result"])
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


def test_flowchart_edges_and_positions() -> None:
    """画布 RPC：连边/断边/节点坐标/起始节点，以及删节点时的悬空边清理。"""
    p = _start()
    try:
        _call(p, "node.add", {"type": "start", "x": 10, "y": 20}, req_id=1)
        cur = _call(p, "workflow.current", req_id=2)
        wf = cur["result"]["workflow"]
        a = wf["nodes"][0]["uid"]
        # 坐标必须原样存下来（画布布局的唯一真源在后端）
        assert (wf["nodes"][0]["x"], wf["nodes"][0]["y"]) == (10, 20)
        assert wf["edges"] == [] and wf["start"] == ""
        assert wf["migrated_from_list"] is False

        b = _call(p, "node.add", {"type": "condition"}, req_id=3)["result"]["node"]["uid"]
        c = _call(p, "node.add", {"type": "delay"}, req_id=4)["result"]["node"]["uid"]

        # 连边。`dst_side` 是画布落点侧（线从目标节点哪一侧画进去），缺省 left
        e1 = _call(p, "edge.add", {"src": a, "dst": b, "port": "out"}, req_id=5)
        assert e1["result"]["workflow"]["edges"] == [
            {"src": a, "port": "out", "dst": b, "dst_side": "left"}]

        # 同一个 (源, 出口) 再连一次 = **替换**，不是新增（不允许扇出）
        # 同时验证落点侧确实被存下来（画布据此决定线画在哪一侧）
        e2 = _call(p, "edge.add", {"src": a, "dst": c, "port": "out",
                                   "dst_side": "bottom"}, req_id=6)
        assert e2["result"]["workflow"]["edges"] == [
            {"src": a, "port": "out", "dst": c, "dst_side": "bottom"}]

        # 脏的落点侧必须**收敛**到默认值，不能原样存：存了前端就找不到对应手柄，
        # Vue Flow 直接画不出这条边（只在控制台刷告警），用户看到「连线莫名消失」。
        # 注意 "right" 也是脏值——右边是输出侧，会与出口手柄同点重合，故不在词表里。
        # "left " 前后有空白，strip 后就是合法值，同样应得到 left。
        for dirty in ["right", "middle", "", None, 123, "LEFT", "left "]:
            ed = _call(p, "edge.add", {"src": a, "dst": c, "port": "out",
                                       "dst_side": dirty}, req_id=61)
            got = ed["result"]["workflow"]["edges"][0]["dst_side"]
            assert got == "left", f"dst_side={dirty!r} 没收敛到默认侧，得到 {got!r}"

        # 同一个源的不同出口互不影响（条件的 true / false 各自一条）
        _call(p, "edge.add", {"src": b, "dst": c, "port": "true"}, req_id=7)
        e3 = _call(p, "edge.add", {"src": b, "dst": a, "port": "false"}, req_id=8)
        assert {(x["port"], x["dst"]) for x in e3["result"]["workflow"]["edges"]
                if x["src"] == b} == {("true", c), ("false", a)}

        # 自环要被挡住（连到自己一定是错的，早点报比运行时报好）
        bad = _call(p, "edge.add", {"src": a, "dst": a, "port": "out"}, req_id=9)
        assert bad["error"]["code"] == -32602

        # 起始节点
        s = _call(p, "workflow.setStart", {"uid": b}, req_id=10)
        assert s["result"]["workflow"]["start"] == b

        # 拖拽结束回写坐标，小数要取整
        pos = _call(p, "node.setPos", {"uid": c, "x": 300.4, "y": 199.6}, req_id=11)
        moved = [n for n in pos["result"]["workflow"]["nodes"] if n["uid"] == c][0]
        assert (moved["x"], moved["y"]) == (300, 200)

        # 断边
        e_rm = _call(p, "edge.remove", {"src": a, "port": "out"}, req_id=12)
        assert all(not (e["src"] == a and e["port"] == "out")
                   for e in e_rm["result"]["workflow"]["edges"])

        # 删节点必须同时清掉连着它的边：否则画布上留着悬空连线，
        # 而执行器走到那儿会找不到节点直接断路径——两种表现对不上。
        cidx = [i for i, n in enumerate(e_rm["result"]["workflow"]["nodes"])
                if n["uid"] == c][0]
        rm = _call(p, "node.remove", {"index": cidx}, req_id=13)
        assert all(e["src"] != c and e["dst"] != c
                   for e in rm["result"]["workflow"]["edges"])
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


def test_node_mutations_prefer_uid_over_stale_index() -> None:
    """节点改动按 uid 寻址：下标在「一次删多个」时会指向别人。

    画布上多选后按 Delete，会给每个 remove 各发一次请求，而它们都按**同一份
    「删除前」的节点列表**算下标：第一笔删掉 a 之后，第二笔原本算出的下标已经
    指到另一个节点上了——删错人且不报任何错。uid 在节点真被删掉前一直有效，
    没有这个窗口。

    这条用例**故意送一个错的下标 + 正确的 uid**，断言死的是 uid 指的那个。
    """
    p = _start()
    try:
        a = _call(p, "node.add", {"type": "delay"}, req_id=1)["result"]["node"]["uid"]
        b = _call(p, "node.add", {"type": "delay"}, req_id=2)["result"]["node"]["uid"]
        c = _call(p, "node.add", {"type": "delay"}, req_id=3)["result"]["node"]["uid"]

        # 下标写 0（指向 a），但 uid 是 c → 必须删掉 c，不是 a
        rm = _call(p, "node.remove", {"index": 0, "uid": c}, req_id=4)
        assert [n["uid"] for n in rm["result"]["workflow"]["nodes"]] == [a, b]

        # 改名 / 停用 / 改参数同样 uid 优先（下标一律给 0 干扰）
        _call(p, "node.rename", {"index": 0, "uid": b, "name": "改过名"}, req_id=5)
        _call(p, "node.toggle", {"index": 0, "uid": b, "enabled": False}, req_id=6)
        cur = _call(p, "node.params.set",
                    {"index": 0, "uid": b, "key": "ms", "value": 123},
                    req_id=7)["result"]["workflow"]
        target = [n for n in cur["nodes"] if n["uid"] == b][0]
        assert target["name"] == "改过名"
        assert target["enabled"] is False
        assert target["params"]["ms"] == 123
        # 0 号（a）完全不该被动过
        zero = [n for n in cur["nodes"] if n["uid"] == a][0]
        assert zero["name"] == "" and zero["enabled"] is True
        assert "ms" not in zero["params"]

        # 不存在的 uid 要**报错**，不能悄悄退回下标 0 上把 a 删了
        ghost = _call(p, "node.remove", {"index": 0, "uid": "nope"}, req_id=8)
        assert ghost["error"]["code"] == -32602
        still = _call(p, "workflow.current", req_id=9)["result"]["workflow"]
        assert [n["uid"] for n in still["nodes"]] == [a, b]

        # 只给 index 的旧调用方式仍然可用（向后兼容，v3 时代的客户端）
        legacy = _call(p, "node.remove", {"index": 0}, req_id=10)
        assert [n["uid"] for n in legacy["result"]["workflow"]["nodes"]] == [b]
    finally:
        _rpc(p, "app.shutdown")
        p.wait(timeout=5)


def test_workflow_load_reports_list_migration(tmp_path) -> None:
    """打开 v3 旧文件：接成线性边链，并把「已迁移」标记透给界面。

    标记必须让前端看到——否则用户会发现条件不再门控而完全不知道为什么。
    """
    old = tmp_path / "v3.json"
    old.write_text(json.dumps({"version": 3, "name": "旧流", "nodes": [
        {"type": "delay", "params": {"ms": 1}, "uid": "a"},
        {"type": "condition", "params": {}, "uid": "b"},
        {"type": "delay", "params": {"ms": 2}, "uid": "c"},
    ]}), encoding="utf-8")

    p = _start()
    try:
        ld = _call(p, "workflow.load", {"path": str(old)}, req_id=1)
        wf = ld["result"]["workflow"]
        assert wf["migrated_from_list"] is True
        assert {(e["src"], e["port"], e["dst"]) for e in wf["edges"]} == {
            ("a", "out", "b"), ("b", "true", "c"), ("b", "false", "c")}
        # 落盘时不该带迁移标记（否则每次打开都提示一遍）
        out = tmp_path / "saved.json"
        _call(p, "workflow.save", {"path": str(out)}, req_id=2)
        assert "migrated_from_list" not in json.loads(out.read_text(encoding="utf-8"))
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


# --------------------------------------------------------------------------- #
# 后端告警接到界面（log.warning 通知）
#
# 背景：core.vision 那些「不报错、只是点歪/找不到」的诊断（模板文件失效 /
# 纯色模板 / 密度不一致）此前只进 stderr 与 ~/Library/Logs/autoflow-tauri.log。
# 而那份日志是 5000+ 行的 RPC 帧流水（send_rpc / rpc_event），让用户去里面
# grep 等于没有诊断。现在 WARNING 及以上会转发成 log.warning 通知。
# --------------------------------------------------------------------------- #
@pytest.fixture
def ui_log():
    """装上 UI 日志 handler，用例结束摘掉（它挂在 root logger 上，是全局副作用）。"""
    from rpc import server

    server._install_ui_log_handler()
    yield server
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h, server._UiLogHandler)]:
        root.removeHandler(h)


def test_backend_warning_is_forwarded_to_ui(ui_log) -> None:
    """后端 WARNING 必须变成 log.warning 通知，否则用户只能去翻 5000 行日志。"""
    server = ui_log
    _drain(server._notify_queue)
    logging.getLogger("core.vision").warning("模板读不出来：%s", "/tmp/x.png")

    hits = [i for i in _drain(server._notify_queue) if i["method"] == "log.warning"]
    assert len(hits) == 1
    p = hits[0]["params"]
    assert p["level"] == "WARNING"
    assert p["logger"] == "core.vision"
    assert p["message"] == "模板读不出来：/tmp/x.png"   # 占位符要已插值


def test_backend_info_is_not_forwarded_to_ui(ui_log) -> None:
    """INFO 是流水，不该进队列——否则队列会被当垃圾桶。

    注意要把 root 调成 INFO，否则 INFO 记录根本走不到 handler，
    这条用例就成了自说自话（假绿）。
    """
    server = ui_log
    _drain(server._notify_queue)
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.INFO)
    try:
        logging.getLogger("core.vision").info("这只是流水")
    finally:
        root.setLevel(old)
    assert [i for i in _drain(server._notify_queue)
            if i["method"] == "log.warning"] == []


def test_ui_log_handler_does_not_recurse(ui_log, monkeypatch) -> None:
    """防递归必须真的有效，而不是「碰巧走不到」。

    队列满时 `_send_notification` 自己会 `logger.warning`，那条记录又会回到本
    handler。但当前 `log.warning` 属于**可丢类**，队列满时走的是「直接 return」
    分支、压根不会 logger.warning —— 不把这条路逼出来，防递归就是测不到的死代码
    （典型的假绿）。所以这里刻意把可丢类清空。
    """
    server = ui_log
    monkeypatch.setattr(server, "_DROPPABLE_NOTIFICATIONS", frozenset())
    q = server._notify_queue
    _drain(q)
    for i in range(q.maxsize):
        server._send_notification("run.progress", {"done": i, "total": 1})
    assert q.full()

    logging.getLogger("core.vision").warning("队列满时的告警")

    # 没有防递归的话会深递归：每层都挤掉一条并再入队一条 log.warning，
    # 直到 RecursionError 被 emit 里的 except 吞掉——表现为「悄悄放大上百条」。
    hits = [i for i in _drain(q) if i["method"] == "log.warning"]
    assert len(hits) == 1, f"防递归失效：一条告警被放大了 {len(hits)} 次"


def test_ui_log_handler_install_is_idempotent(ui_log) -> None:
    """重复安装不能挂两份，否则同一条告警会被转发两次。"""
    server = ui_log
    server._install_ui_log_handler()
    root = logging.getLogger()
    assert len([h for h in root.handlers
                if isinstance(h, server._UiLogHandler)]) == 1


def test_log_warning_is_droppable_so_it_never_evicts_state_notifications() -> None:
    """log.warning 是**诊断**不是状态跃迁：队列满时该丢它，不能让它挤掉状态类。"""
    from rpc import server

    assert "log.warning" in server._DROPPABLE_NOTIFICATIONS
    q = server._notify_queue
    _drain(q)
    for i in range(q.maxsize):
        server._send_notification("run.progress", {"done": i, "total": 1})
    server._send_notification("log.warning", {"level": "WARNING", "message": "x"})
    items = _drain(q)
    assert len(items) == q.maxsize
    assert all(it["method"] == "run.progress" for it in items)   # 没挤掉任何一条


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
