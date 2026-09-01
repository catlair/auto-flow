"""RPC sidecar 端到端测试：起真实子进程，校验协议帧与 stdio 洁净性。

对应 docs/迁移计划书 §13（stdio 洁净性）与 §17.1（RPC 层测试）。
测试会真的拉起 `python -m rpc.server`，故仅 macOS 可跑；且会调用权限检测
API（仅查询，不弹窗）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="sidecar 仅支持 macOS"
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SIDE = [sys.executable, "-m", "rpc.server"]


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

        # nodes.definitions 含 common_params 且按 §9.4 顺序排列
        defs = _call(p, "nodes.definitions", req_id=9)
        assert defs["id"] == 9
        order = [d["type"] for d in defs["result"]]
        assert order.index("mouse") < order.index("keyboard") < order.index("delay") < order.index("record_replay") < order.index("image_click") < order.index("ocr_click") < order.index("yolo_click")
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
