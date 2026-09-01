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
