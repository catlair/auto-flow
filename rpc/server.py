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
from rpc.controller import AppController, ControllerError, _err

from core import permissions
from core.paths import app_dir

logger = logging.getLogger("autoflow-sidecar")

# 协议输出句柄（原始 fd，绝不经过 sys.stdout）
OUT: Optional[object] = None
# 帧写锁：响应（stdin 读线程）与通知（writer 线程）并发写同一 fd。
# 管道对超长帧不保证原子性，无锁时两条帧会字节级交错，前端随机 JSON 解析失败——
# 表现为「通知/响应偶发丢失」，实为传输层损坏。
_WRITE_LOCK = threading.Lock()
_notify_queue: "queue.Queue[dict]" = queue.Queue(maxsize=1024)
_shutdown = threading.Event()

# 队列满时**可以丢弃**的通知（§13 背压）。
#
# 只有这几个是「高频且状态可被后一条覆盖」的：record.event 每 100ms 推一批事件，
# run.progress 随回放进度频繁推送，丢一条不影响最终状态。
# log.warning 是**诊断**不是状态跃迁——丢一条前端不会卡在旧状态，所以宁可丢它，
# 也不能让它去挤掉下面的状态类通知。
# 其余通知（run.finished / run.error / record.stopped / workflow.changed /
# permission.changed / hotkey.triggered / schedule.fired …）都是**状态跃迁**，
# 丢一条前端就可能永久停在旧状态——绝不能被高频洪水挤掉。
_DROPPABLE_NOTIFICATIONS = frozenset({"record.event", "run.progress", "log.warning"})

# 权限状态缓存，用于检测变化后推送
_last_perm: Optional[dict] = None

# 后端应用控制器：持有 current_workflow 唯一真源（§10）
_CTRL = AppController()


class _UiLogHandler(logging.Handler):
    """把 WARNING 及以上的后端日志转发成 `log.warning` 通知，让**界面**能看见。

    存在的理由：`core.vision` 那些「不报错、只是点歪/找不到」的诊断
    （模板文件失效 / 纯色模板 / 密度不一致 / 自动缩放说明）此前只进 stderr
    与 `~/Library/Logs/autoflow-tauri.log`。而那份日志是 5000+ 行的 RPC 帧流水
    （`send_rpc` / `rpc_event`），让用户去里面 grep 等于没有诊断。

    只转发 WARNING 及以上：INFO 是流水，全转会把队列当垃圾桶。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        # 防递归：`_send_notification` 在队列满时自己会 `logger.warning`，
        # 那条记录又会回到本 handler → 无限递归。用线程本地标记掐断。
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            _send_notification("log.warning", {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })
        except Exception:  # noqa: BLE001 - 日志路径绝不能让主流程炸掉
            pass
        finally:
            self._local.busy = False


def _install_ui_log_handler() -> None:
    """把 `_UiLogHandler` 挂到 root logger（幂等，重复调用不会挂两份）。"""
    root = logging.getLogger()
    if any(isinstance(h, _UiLogHandler) for h in root.handlers):
        return
    root.addHandler(_UiLogHandler())


# --------------------------------------------------------------------------- #
# 启动 / 输出
# --------------------------------------------------------------------------- #
def _init_output() -> None:
    global OUT
    # 1) Python 层的 stdout 指向 stderr：print / logging 都不会进帧流
    sys.stdout = sys.stderr
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # 2) 协议通道：先把 fd 1 复制到一个新 fd，再把 fd 1 本身指向 stderr。
    #
    #    只改 sys.stdout 拦不住**绕过 Python 的写入**——第三方库的 C++ 层告警、
    #    printf、直接用 fd 1 的代码，字节会原样混进 NDJSON 帧流，前端随机
    #    JSON 解析失败（表现为「通知/响应偶发丢失」）。dup 之后：
    #      proto_fd → 协议管道（Rust 的 rpc_event）
    #      fd 1     → stderr 管道（Rust 的 sidecar_stderr，可见可查）
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    OUT = os.fdopen(proto_fd, "wb", buffering=0)


def _send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        assert OUT is not None
        with _WRITE_LOCK:
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
    """入队通知；队列满时按 _DROPPABLE_NOTIFICATIONS 分流。

    此前是无差别 `get_nowait()` 丢最旧一条，与注释声称的「run/record 类保留」
    不符：一次 record.event 洪水就能把 run.finished / workflow.changed 挤掉，
    前端随后永久停在旧状态（看起来像「通知偶发丢失」，实为策略写错）。
    """
    item = {"jsonrpc": "2.0", "method": method, "params": params}
    try:
        _notify_queue.put_nowait(item)
        return
    except queue.Full:
        pass

    if method in _DROPPABLE_NOTIFICATIONS:
        # 自身可丢：直接放弃这条，队列里已有的（尤其状态类）一条都不动
        return

    # 状态类通知必须送达：挤掉最旧的一条，并留痕便于定位背压
    try:
        dropped = _notify_queue.get_nowait()
        _notify_queue.task_done()  # 与 put 配对，否则 unfinished_tasks 只增不减
        _notify_queue.put_nowait(item)
        logger.warning(
            "通知队列已满，丢弃最旧通知以送达 %s（被丢：%s）",
            method, dropped.get("method"),
        )
    except queue.Empty:
        pass
    except queue.Full:
        logger.warning("通知队列已满，%s 未能入队", method)


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
    try:
        _last_perm = _snapshot_permissions()
    except Exception:  # noqa: BLE001
        logger.exception("initial permission snapshot failed")
        _last_perm = {}
    while not _shutdown.is_set():
        _shutdown.wait(2.0)
        if _shutdown.is_set():
            break
        try:
            cur = _snapshot_permissions()
        except Exception:  # noqa: BLE001
            # 单轮快照失败不应打死轮询线程；记录后跳过本轮，下个周期再试
            logger.exception("permission snapshot failed; skip this round")
            continue
        if cur != _last_perm:
            _last_perm = cur
            _send_notification("permission.changed", cur)


# --------------------------------------------------------------------------- #
# 方法实现
# --------------------------------------------------------------------------- #
def _ctl(method):
    """把 controller 方法包成 JSON-RPC handler：成功 (True, result)；
    ControllerError → 对应业务错误码；其余异常 → -32000。"""
    def handler(params: dict) -> tuple:
        try:
            return (True, method(params or {}))
        except ControllerError as e:
            return _err(e.code, e.message, e.data)
        except Exception as e:  # noqa: BLE001
            logger.exception("controller error: %s", method)
            return _err(-32000, "Internal error", {"detail": str(e)})
    return handler


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
    # --- 应用/权限（P0-S） ---
    "app.info": lambda _p: (True, _app_info()),
    "app.diagnose": lambda _p: (True, _diagnose()),
    "app.openPermissionSettings": lambda p: (True, _open_settings((p or {}).get("panel"))),
    "app.requestPermissions": lambda _p: (True, _request_permissions()),
    # --- 工作流真源（§9/§10，P1-1） ---
    "workflow.current": _ctl(lambda p: _CTRL.workflow_current()),
    "workflow.load": _ctl(lambda p: _CTRL.workflow_load(p.get("path", ""))),
    "workflow.save": _ctl(lambda p: _CTRL.workflow_save(p.get("path"))),
    "workflow.new": _ctl(lambda p: _CTRL.workflow_new()),
    "workflow.update": _ctl(lambda p: _CTRL.workflow_update(p.get("patch", {}))),
    "node.add": _ctl(lambda p: _CTRL.node_add(p.get("type", ""), p.get("index"))),
    "node.remove": _ctl(lambda p: _CTRL.node_remove(p.get("index", -1))),
    "node.move": _ctl(lambda p: _CTRL.node_move(p.get("index", -1), p.get("to", -1))),
    "node.toggle": _ctl(lambda p: _CTRL.node_toggle(p.get("index", -1), p.get("enabled", True))),
    "node.rename": _ctl(lambda p: _CTRL.node_rename(p.get("index", -1), p.get("name", ""))),
    "node.params.set": _ctl(lambda p: _CTRL.node_params_set(p.get("index", -1), p.get("key"), p.get("value"))),
    "nodes.definitions": _ctl(lambda p: _CTRL.nodes_definitions()),
    # --- 运行 / 录制（§5/§9.5，P1-2） ---
    "run.start": _ctl(lambda p: _CTRL.run_start(p.get("base_x", 0), p.get("base_y", 0))),
    "run.stop": _ctl(lambda p: _CTRL.run_stop()),
    "record.start": _ctl(lambda p: _CTRL.record_start(
        p.get("window_bounds"), drop_in_window=bool(p.get("drop_in_window", False)))),
    "record.stop": _ctl(lambda p: _CTRL.record_stop(
        trim=bool(p.get("trim", False)), window_bounds=p.get("window_bounds"))),
    "record.toNode": _ctl(lambda p: _CTRL.record_to_node()),
    "record.current": _ctl(lambda p: _CTRL.record_current()),
    "record.remove": _ctl(lambda p: _CTRL.record_remove(p.get("indexes"))),
    "record.removeMovesBefore": _ctl(lambda p: _CTRL.record_remove_moves_before(
        p.get("index", 0))),
    "record.setOrigin": _ctl(lambda p: _CTRL.record_set_origin(p.get("index"))),
    "record.setText": _ctl(lambda p: _CTRL.record_set_text(p.get("index"), p.get("text"))),
    "record.keysToText": _ctl(lambda p: _CTRL.record_keys_to_text(
        p.get("index"), p.get("text"))),
    "record.undo": _ctl(lambda p: _CTRL.record_undo()),
    "record.subscribe": _ctl(lambda p: _CTRL.record_subscribe((p or {}).get("on", False))),
    "record.bounds": _ctl(lambda p: _CTRL.record_bounds(p.get("bounds"))),
    # --- 模板截取（screencapture -i 框选，异步回填 template.snipped） ---
    "template.snip": _ctl(lambda p: _CTRL.template_snip()),
    # --- 输入监控实际可收性自检（F18 合成键回环） ---
    "input.probe": _ctl(lambda p: _CTRL.input_probe()),
    # --- 热键 / 键盘捕获 / 取点 / 定时（P1-3，§3.2/§9.2） ---
    "hotkey.set": _ctl(lambda p: _CTRL.hotkey_set(p.get("actions"))),
    "hotkey.clear": _ctl(lambda p: _CTRL.hotkey_clear()),
    "key.capture": _ctl(lambda p: _CTRL.key_capture_start()),
    "key.capture.stop": _ctl(lambda p: _CTRL.key_capture_stop()),
    "base.pick": _ctl(lambda p: _CTRL.base_pick()),
    "schedule.get": _ctl(lambda p: _CTRL.schedule_get()),
    "schedule.configure": _ctl(lambda p: _CTRL.schedule_configure(p or {})),
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
        if ok:
            _send_response(req_id, result)
        else:
            # 业务错误：result = {code, message, data}
            _send_error(req_id, result["code"], result["message"], result.get("data"))


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
    # 把 WARNING 及以上的日志接到界面（`log.warning` 通知）。装在 basicConfig
    # 之后、启动日志之前，这样启动期的告警（如权限快照异常）用户也能看到。
    _install_ui_log_handler()
    # 把启动时的权限快照写进日志：权限检查归属"责任进程"，同一 sidecar 由 shell 拉起
    # 与由 App 拉起读到的值可能不同——排查「横幅全 ✗ 但 sidecar 明明活着」必须有这份记录
    try:
        logger.info("权限快照: %s", _snapshot_permissions())
    except Exception:  # noqa: BLE001
        logger.exception("startup permission snapshot failed")

    _CTRL.set_notifier(_send_notification)

    threading.Thread(target=_writer_loop, name="rpc-writer", daemon=True).start()
    threading.Thread(target=_perm_loop, name="perm-poll", daemon=True).start()

    exit_reason = "stdin EOF（外壳关闭或管道断开）"
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
            exit_reason = "app.shutdown"
            break
        try:
            _handle(msg)
        except Exception as e:  # noqa: BLE001
            logger.exception("unhandled in dispatch")
            if msg.get("id") is not None:
                _send_error(msg["id"], -32000, "Internal error", {"detail": str(e)})
        if _shutdown.is_set():
            exit_reason = "shutdown 请求（handler 内触发）"
            break

    # 停机清理：停定时计时器、松键监听线程（守护线程随之退出）
    try:
        _CTRL.shutdown()
    except Exception:  # noqa: BLE001
        pass

    # 收尾：把排队的（通知）同步写完，不依赖 daemon writer 线程，避免进程退出丢帧
    _shutdown.set()
    try:
        while not _notify_queue.empty():
            _send(_notify_queue.get_nowait())
    except Exception:  # noqa: BLE001
        pass
    logger.info("sidecar 退出（%s）", exit_reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
