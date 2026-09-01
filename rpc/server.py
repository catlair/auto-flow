"""Auto Flow Python 后端 sidecar：stdin/stdout JSON-RPC 2.0 over NDJSON。

设计要点（对照 docs/迁移计划书 §13 stdio 洁净性）：
- sys.stdout 重定向到 stderr，协议帧只走**原始 fd 1**（二进制、无缓冲、flush），
  杜绝 print/logging/第三方库的 C++ 层告警污染帧流。
- 帧为 compact NDJSON（separators=(",",":")，JSON 自行转义换行），每条独占一行。
- 通知走独立写线程 + 队列，避免阻塞业务/读线程（§13 背压）。
- 读线程按行解析 stdin；stdin 按 utf-8 重新配置。

本文件是 P0-S 的「双端 hello world」：实现 app.* 系列与权限轮询，
业务方法（run/record/workflow…）在 P1 接入（见 docs §9）。
"""
from __future__ import annotations

# 保证无论以 `python rpc/server.py` 还是 PyInstaller 打包运行，项目根都在 sys.path，
# 使 `from rpc import ...` 与核心模块可被找到。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import logging
import os
import queue
import sys
import threading
from typing import Any, Optional

from rpc import PROTOCOL_VERSION, __version__ as APP_VERSION

from core import permissions
from core.paths import app_dir

logger = logging.getLogger("autoflow-sidecar")

# 协议输出句柄（原始 fd，绝不经过 sys.stdout）
OUT: Optional[object] = None
_notify_queue: "queue.Queue[dict]" = queue.Queue(maxsize=1024)
_shutdown = threading.Event()

# 权限状态缓存，用于检测变化后推送
_last_perm: Optional[dict] = None


# --------------------------------------------------------------------------- #
# 启动 / 输出
# --------------------------------------------------------------------------- #
def _init_output() -> None:
    global OUT
    # 关键：把 Python 默认 stdout 重定向到 stderr，任何 print/logging 都不会进帧流
    sys.stdout = sys.stderr
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # 协议走原始 fd 1，二进制、无缓冲
    OUT = os.fdopen(1, "wb", buffering=0)


def _send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        assert OUT is not None
        OUT.write(line + b"\n")
        OUT.flush()
    except (BrokenPipeError, ValueError, OSError):
        # 对端（Tauri）已关闭管道，安静退出
        _request_shutdown()


def _send_response(req_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _send_error(req_id: Any, code: int, message: str, data: Any = None) -> None:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": req_id, "error": err})


def _send_notification(method: str, params: Any) -> None:
    """入队通知；队列满时丢弃最旧（§13 背压：log 类可丢，run/record 类保留）。"""
    try:
        _notify_queue.put_nowait({"jsonrpc": "2.0", "method": method, "params": params})
    except queue.Full:
        try:
            _notify_queue.get_nowait()
            _notify_queue.put_nowait({"jsonrpc": "2.0", "method": method, "params": params})
        except queue.Empty:
            pass


def _writer_loop() -> None:
    while not _shutdown.is_set() or not _notify_queue.empty():
        try:
            item = _notify_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        _send(item)
        _notify_queue.task_done()


# --------------------------------------------------------------------------- #
# 权限轮询（每 2s，变化才推）
# --------------------------------------------------------------------------- #
def _snapshot_permissions() -> dict:
    return {
        "accessibility": bool(permissions.check_accessibility()),
        "inputMonitoring": bool(permissions.check_input_monitoring()),
        "screenRecording": bool(permissions.check_screen_recording()),
    }


def _perm_loop() -> None:
    global _last_perm
    _last_perm = _snapshot_permissions()
    while not _shutdown.is_set():
        _shutdown.wait(2.0)
        if _shutdown.is_set():
            break
        cur = _snapshot_permissions()
        if cur != _last_perm:
            _last_perm = cur
            _send_notification("permission.changed", cur)


# --------------------------------------------------------------------------- #
# 方法实现
# --------------------------------------------------------------------------- #
def _app_info() -> dict:
    return {
        "appVersion": APP_VERSION,
        "sidecarVersion": APP_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "platform": "macos",
        "pythonVersion": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "permissions": _snapshot_permissions(),
    }


def _log_tail(n: int = 50) -> list:
    log_path = os.path.expanduser("~/Library/Logs/AutoFlow/backend.log")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[-n:]
        return lines
    except OSError:
        return []


def _diagnose() -> dict:
    return {
        "appVersion": APP_VERSION,
        "sidecarVersion": APP_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "platform": "macos",
        "frozen": bool(getattr(sys, "frozen", False)),
        "dataDir": app_dir(),
        "permissions": _snapshot_permissions(),
        "logTail": _log_tail(),
    }


_PANEL_HANDLERS = {
    "accessibility": permissions.open_accessibility_settings,
    "input_monitoring": permissions.open_input_monitoring_settings,
    "screen_recording": permissions.open_screen_recording_settings,
}


def _open_settings(panel: Optional[str]) -> None:
    handler = _PANEL_HANDLERS.get(panel or "accessibility")
    if handler:
        handler()


def _request_permissions() -> dict:
    """弹系统授权提示：辅助功能 + 屏幕录制（输入监控无直接 prompt API，需手动在
    系统设置添加；这里只回传当前快照）。用于 P0-S 授权 spike（T5）。"""
    try:
        permissions.check_accessibility(prompt=True)
    except Exception:  # noqa: BLE001
        logger.exception("request accessibility prompt failed")
    try:
        permissions.check_screen_recording(prompt=True)
    except Exception:  # noqa: BLE001
        logger.exception("request screen recording prompt failed")
    return _snapshot_permissions()


_HANDLERS = {
    "app.info": lambda _p: (True, _app_info()),
    "app.diagnose": lambda _p: (True, _diagnose()),
    "app.openPermissionSettings": lambda p: (True, _open_settings((p or {}).get("panel"))),
    "app.requestPermissions": lambda _p: (True, _request_permissions()),
}


def _handle(msg: dict) -> None:
    method = msg.get("method")
    req_id = msg.get("id")
    handler = _HANDLERS.get(method)
    if handler is None:
        if req_id is not None:
            _send_error(req_id, -32601, "Method not found", {"method": method})
        return
    try:
        ok, result = handler(msg.get("params") or {})
    except Exception as e:  # noqa: BLE001
        logger.exception("handle %s failed", method)
        if req_id is not None:
            _send_error(req_id, -32000, "Internal error", {"detail": str(e)})
        return
    if req_id is not None:
        _send_response(req_id, result if ok else {})


def _request_shutdown() -> None:
    _shutdown.set()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    _init_output()
    log_dir = os.path.expanduser("~/Library/Logs/AutoFlow")
    try:
        os.makedirs(log_dir, exist_ok=True)
        _fh = logging.FileHandler(
            os.path.join(log_dir, "backend.log"), encoding="utf-8", delay=True
        )
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stderr), _fh])
    except OSError:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    logger.info("Auto Flow sidecar v%s protocol v%d 启动", APP_VERSION, PROTOCOL_VERSION)

    threading.Thread(target=_writer_loop, name="rpc-writer", daemon=True).start()
    threading.Thread(target=_perm_loop, name="perm-poll", daemon=True).start()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            _send_error(None, -32700, "Parse error")
            continue
        method = msg.get("method")
        if method == "app.shutdown":
            _send_response(msg.get("id"), {})
            _request_shutdown()
            break
        try:
            _handle(msg)
        except Exception as e:  # noqa: BLE001
            logger.exception("unhandled in dispatch")
            if msg.get("id") is not None:
                _send_error(msg["id"], -32000, "Internal error", {"detail": str(e)})
        if _shutdown.is_set():
            break

    # 收尾：把排队的（通知）同步写完，不依赖 daemon writer 线程，避免进程退出丢帧
    _shutdown.set()
    try:
        while not _notify_queue.empty():
            _send(_notify_queue.get_nowait())
    except Exception:  # noqa: BLE001
        pass
    logger.info("sidecar 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
