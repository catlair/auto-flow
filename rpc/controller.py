"""后端应用控制器：持有 current_workflow 作为唯一真源（§10）。

前端近乎无状态：任何结构操作（增删改移/启停/改参）→ 调用这里 → 改树 →
通过 on_notify 广播 `workflow.changed`。运行/录制/热键/定时等状态也在这里。

本模块只装业务逻辑，不碰 stdio/JSON-RPC 传输（见 rpc/server.py）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from core.events import Node, Workflow
from core import executor as _executor_mod  # 仅类型/构造；真正运行在 P1-2

# 节点菜单顺序（§9.4）：鼠标、键盘、延时、录制回放、图像、OCR、YOLO、条件、注释。
# 类型字符串以 tasks/builtin.py 实际注册为准（image_click/ocr_click/yolo_click）。
_NODE_ORDER = {
    "mouse": 0, "keyboard": 1, "delay": 2, "record_replay": 3,
    "image_click": 4, "ocr_click": 5, "yolo_click": 6, "condition": 7, "note": 8,
}

# 通用参数「执行条件」（§9.3），所有节点尾部都渲染，后端为真源。
COMMON_PARAMS = [
    {"key": "run_when", "label": "执行条件", "ptype": "select",
     "default": "总是", "options": ["总是", "条件成立", "条件不成立"]},
]

# 热键物理键顺序：F9/F10/F11 依次对应 record/run/pick（与现状 ui/main_window 一致）。
_HOTKEY_KEYS = ["F9", "F10", "F11"]
_HOTKEY_ACTIONS = {"record", "run", "pick"}
_VALID_SCHEDULE_MODES = {"每天时刻", "固定间隔"}


class ControllerError(Exception):
    """业务错误，携带 JSON-RPC 错误码。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _err(code: int, message: str, data: Any = None) -> tuple[bool, dict]:
    """handler 失败约定：返回 (False, {code,message,data})，由 server 转 _send_error。"""
    return (False, {"code": code, "message": message, "data": data})


class AppController:
    def __init__(self) -> None:
        self.workflow = Workflow()
        self.path: Optional[str] = None
        self.executor = _executor_mod.Executor()
        self.recorder = None  # 在 P1-2 装入 Recorder
        self._last_record = None
        self._rec_poller = None
        self._rec_poller_rec = None
        self.running = False
        self.recording = False
        self._record_subscribed = False  # §3.2：仅订阅后推送 record.event
        self._lock = threading.RLock()
        self._notify: Callable[[str, Any], None] = lambda _m, _p: None

        # ---- 热键 / 键盘捕获（P1-3，§3.2）----
        # 单一 MacKeyboardListener，按 _key_mode 在「热键分发」与「按键捕获」间切换，
        # 捕获期间暂挂热键分发（复用同一 listener，避免捕获键被当热键触发）。
        self._key_listener = None
        self._key_mode = "idle"        # idle | hotkey | capture
        self._hotkey_map: dict = {}    # 物理键名 -> 动作（record/run/pick）
        self._capturing = False

        # ---- 取点（F11，base.pick）由 pynput 直接读全局光标位置 ----

        # ---- 定时（P1-3，§9.2）：配置持久化到 config.json，后端 threading.Timer 触发 ----
        self._schedule: dict = self._schedule_load()
        self._schedule_timer = None
        if self._schedule:
            # 启动时若已有持久化配置则恢复装定（仅 enabled 且路径有效才真正起计时器）
            self._schedule_arm(self._schedule)

    # ---- 通知 ----
    def set_notifier(self, fn: Callable[[str, Any], None]) -> None:
        self._notify = fn

    def _broadcast_workflow(self) -> None:
        self._notify("workflow.changed", self.workflow.to_dict())

    # ---- workflow.* ----
    def workflow_current(self) -> dict:
        return {
            "workflow": self.workflow.to_dict(),
            "path": self.path,
            "running": self.running,
            "recording": self.recording,
        }

    def workflow_load(self, path: str) -> dict:
        if not path or not isinstance(path, str):
            raise ControllerError(-32602, "参数无效：path 必填")
        if not os.path.exists(path):
            raise ControllerError(-32602, "文件不存在", {"path": path})
        with self._lock:
            self.workflow = Workflow.load(path)
            self.path = path
        self._broadcast_workflow()
        return self.workflow_current()

    def workflow_save(self, path: Optional[str] = None) -> dict:
        target = path or self.path
        if not target:
            raise ControllerError(-32602, "尚未指定保存路径（先 load 或传 path）")
        with self._lock:
            self.workflow.save(target)
            self.path = target
        return {"path": target}

    def workflow_new(self) -> dict:
        with self._lock:
            self.workflow = Workflow()
            self.path = None
        self._broadcast_workflow()
        return self.workflow_current()

    def workflow_update(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ControllerError(-32602, "参数无效：patch 须为对象")
        with self._lock:
            if "name" in patch and isinstance(patch["name"], str):
                self.workflow.name = patch["name"]
            if "speed" in patch:
                try:
                    self.workflow.speed = float(patch["speed"])
                except (TypeError, ValueError):
                    raise ControllerError(-32602, "speed 须为数字")
            if "repeat" in patch:
                try:
                    self.workflow.repeat = max(int(patch["repeat"]), 1)
                except (TypeError, ValueError):
                    raise ControllerError(-32602, "repeat 须为整数")
        self._broadcast_workflow()
        return self.workflow_current()

    # ---- node.* ----
    def _node_at(self, index: int) -> Node:
        if not isinstance(index, int) or index < 0 or index >= len(self.workflow.nodes):
            raise ControllerError(-32602, "节点索引越界", {"index": index})
        return self.workflow.nodes[index]

    def node_add(self, type_name: str, index: Optional[int] = None) -> dict:
        import tasks.builtin  # 确保节点已注册
        from tasks.base import get_task
        if not get_task(type_name):
            raise ControllerError(-32602, "未知节点类型", {"type": type_name})
        with self._lock:
            node = Node(type=type_name, params={}, enabled=True)
            if index is None or index >= len(self.workflow.nodes):
                self.workflow.nodes.append(node)
                inserted = len(self.workflow.nodes) - 1
            else:
                inserted = max(0, index)
                self.workflow.nodes.insert(inserted, node)
        self._broadcast_workflow()
        return {"index": inserted, "node": node.to_dict()}

    def node_remove(self, index: int) -> dict:
        with self._lock:
            self._node_at(index)
            self.workflow.nodes.pop(index)
        self._broadcast_workflow()
        return self.workflow_current()

    def node_move(self, index: int, to: int) -> dict:
        with self._lock:
            node = self._node_at(index)
            if not isinstance(to, int) or to < 0 or to >= len(self.workflow.nodes):
                raise ControllerError(-32602, "目标索引越界", {"to": to})
            self.workflow.nodes.pop(index)
            self.workflow.nodes.insert(to, node)
        self._broadcast_workflow()
        return self.workflow_current()

    def node_toggle(self, index: int, enabled: bool) -> dict:
        with self._lock:
            node = self._node_at(index)
            node.enabled = bool(enabled)
        self._broadcast_workflow()
        return self.workflow_current()

    def node_params_set(self, index: int, key: str, value: Any) -> dict:
        if not isinstance(key, str):
            raise ControllerError(-32602, "参数 key 须为字符串")
        with self._lock:
            node = self._node_at(index)
            node.params[key] = value
        self._broadcast_workflow()
        return self.workflow_current()

    # ---- nodes.definitions（含 common_params + 顺序，§9.3/§9.4） ----
    def nodes_definitions(self) -> list:
        import tasks.builtin  # 确保节点已注册
        from tasks.base import all_definitions
        defs = all_definitions()
        out = []
        for d in defs:
            d = dict(d)
            d["common_params"] = list(COMMON_PARAMS)
            out.append(d)
        out.sort(key=lambda d: _NODE_ORDER.get(d.get("type", ""), 999))
        return out

    # ---- run.*（Executor 包装，§5/§9.5） ----
    def run_start(self, base_x: int = 0, base_y: int = 0) -> dict:
        import tasks.builtin  # 确保节点已注册
        with self._lock:
            if self.running:
                raise ControllerError(-32001, "already_running")
            if self.recording:
                raise ControllerError(-32002, "busy_recording")
            runnable = [n for n in self.workflow.nodes if n.enabled and n.type != "note"]
            if not runnable:
                raise ControllerError(-32004, "workflow_empty")
            wf = self.workflow
            self.running = True

        def on_node(idx, ntype):
            self._notify("run.node", {"index": idx, "type": ntype})

        def on_progress(done, total):
            self._notify("run.progress", {"done": done, "total": total})

        def on_done(stopped):
            self.running = False
            self._notify("run.finished", {"stopped": bool(stopped)})

        def on_error(idx, ntype, exc):
            self._notify("run.error", {"index": idx, "type": ntype, "message": str(exc)})

        threading.Thread(
            target=self.executor.run_workflow,
            args=(wf, base_x, base_y),
            kwargs={"on_node": on_node, "on_progress": on_progress,
                    "on_done": on_done, "on_error": on_error},
            name="run-workflow", daemon=True,
        ).start()
        return {"running": True}

    def run_stop(self) -> dict:
        self.executor.stop_run()
        return {"running": self.running}

    # ---- record.*（Recorder 包装，100ms 批量推 record.event，§5 节流） ----
    def record_start(self) -> dict:
        from core.recorder import Recorder
        with self._lock:
            if self.recording:
                raise ControllerError(-32001, "already_running")
            if self.running:
                raise ControllerError(-32002, "busy_recording")
            self.recorder = Recorder()
            self.recorder.start()
            self.recording = True
            self._rec_poller_rec = self.recorder
        self._rec_poller = threading.Thread(target=self._record_poll, name="rec-poll", daemon=True)
        self._rec_poller.start()
        return {"recording": True}

    def record_subscribe(self, on: bool) -> dict:
        """§3.2：仅当录制面板订阅时才向 stdout 推送 record.event，避免高频事件打爆管道。"""
        with self._lock:
            self._record_subscribed = bool(on)
        return {"subscribed": self._record_subscribed}

    def _record_poll(self) -> None:
        # 仅用启动时刻捕获的 rec 引用；record_stop 会置空 self.recorder，这里绝不回读属性
        rec = self._rec_poller_rec
        while self.recording:
            time.sleep(0.1)
            if not self.recording:
                break
            evs = rec.poll()
            if evs and self._record_subscribed:
                self._notify("record.event", {"events": [asdict(e) for e in evs]})
        # 退出前再 flush 一次残留（仍受订阅门控）
        evs = rec.poll()
        if evs and self._record_subscribed:
            self._notify("record.event", {"events": [asdict(e) for e in evs]})

    def record_stop(self) -> dict:
        with self._lock:
            if not self.recording or self.recorder is None:
                raise ControllerError(-32003, "not_recording")
            rec = self.recorder
            self.recorder = None
            self.recording = False
        result = rec.stop()
        self._last_record = result
        self._notify("record.stopped", {
            "count": len(result.events),
            "origin": [result.origin_x, result.origin_y],
            "stopped_by_limit": result.stopped_by_limit,
        })
        return {"count": len(result.events),
                "origin": [result.origin_x, result.origin_y],
                "stopped_by_limit": result.stopped_by_limit}

    def record_to_node(self) -> dict:
        res = self._last_record
        if res is None:
            raise ControllerError(-32003, "no_record_result")
        node = Node(type="record_replay", params={
            "events": [asdict(e) for e in res.events],
            "origin_x": res.origin_x, "origin_y": res.origin_y,
            "use_relative": True,
        }, enabled=True)
        with self._lock:
            self.workflow.nodes.append(node)
            inserted = len(self.workflow.nodes) - 1
        self._broadcast_workflow()
        return {"index": inserted, "node": node.to_dict()}

    # ---- 热键 / 键盘捕获（P1-3，§3.2 + §9 说明） ----
    def _ensure_key_listener(self) -> None:
        """确保全局键盘监听线程存活（CGEventTap 失败则返回，线程随即结束）。"""
        if self._key_listener is None or not self._key_listener.is_alive():
            from core.maclistener import MacKeyboardListener
            self._key_listener = MacKeyboardListener(self._on_key)
            self._key_listener.start()

    def _stop_key_listener(self) -> None:
        lis = self._key_listener
        self._key_listener = None
        if lis is not None:
            try:
                lis.stop()
            except Exception:  # noqa: BLE001
                pass

    def _on_key(self, name: str, pressed: bool) -> None:
        """单一 listener 回调：按 _key_mode 在捕获 / 热键间分发。"""
        if not name:
            return
        with self._lock:
            mode = self._key_mode
        if mode == "capture":
            if pressed:
                with self._lock:
                    self._capturing = False
                    self._key_mode = "hotkey" if self._hotkey_map else "idle"
                self._notify("key.captured", {"name": name})
            return
        if mode == "hotkey" and pressed:
            with self._lock:
                action = self._hotkey_map.get(name)
            if action:
                self._notify("hotkey.triggered", {"action": action})

    def hotkey_set(self, actions: Any) -> dict:
        """绑定 F9/F10/F11 → record/run/pick。

        actions 可以是数组（按 F9/F10/F11 顺序，元素为动作名或 None 解除绑定），
        也可以是对象 {record, run, pick}。返回当前三键绑定快照。
        """
        if isinstance(actions, dict):
            actions = [actions.get("record"), actions.get("run"), actions.get("pick")]
        if not isinstance(actions, (list, tuple)):
            raise ControllerError(-32602, "参数无效：actions 须为数组或对象")
        mapping: dict = {}
        for i, act in enumerate(actions[:3]):
            if act and str(act) in _HOTKEY_ACTIONS:
                mapping[_HOTKEY_KEYS[i]] = str(act)
        with self._lock:
            self._hotkey_map = mapping
            if not self._capturing:
                self._key_mode = "hotkey" if mapping else "idle"
        if mapping or self._capturing:
            self._ensure_key_listener()
        else:
            self._stop_key_listener()
        return {"hotkeys": [mapping.get(k) for k in _HOTKEY_KEYS]}

    def hotkey_clear(self) -> dict:
        with self._lock:
            self._hotkey_map = {}
            if not self._capturing:
                self._key_mode = "idle"
        if not self._capturing:
            self._stop_key_listener()
        return {"hotkeys": [None, None, None]}

    def key_capture_start(self) -> dict:
        """进入按键捕获模式（暂挂热键分发），捕获到的键经 `key.captured` 异步回填。"""
        with self._lock:
            self._capturing = True
            self._key_mode = "capture"
        self._ensure_key_listener()
        return {"capturing": True}

    def key_capture_stop(self) -> dict:
        with self._lock:
            self._capturing = False
            self._key_mode = "hotkey" if self._hotkey_map else "idle"
        return {"capturing": False}

    def base_pick(self) -> dict:
        """F11 取点：读全局光标位置（与现状 pynput 坐标空间一致，供相对回放对齐）。"""
        from pynput.mouse import Controller as MouseController
        pos = MouseController().position
        return {"x": int(round(pos[0])), "y": int(round(pos[1]))}

    # ---- 定时运行（P1-3，§9.2） ----
    def _schedule_path(self) -> str:
        from core.paths import app_dir
        return os.path.join(app_dir(), "config.json")

    def _schedule_load(self) -> dict:
        try:
            with open(self._schedule_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data.get("schedule", {}) or {}

    def _schedule_save(self, cfg: dict) -> None:
        from core.paths import app_dir
        os.makedirs(app_dir(), exist_ok=True)
        try:
            with open(self._schedule_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data["schedule"] = cfg
        try:
            with open(self._schedule_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _schedule_next_fire(self, cfg: dict) -> Optional[datetime]:
        if not cfg:
            return None
        now = datetime.now()
        if cfg.get("mode") == "固定间隔":
            iv = max(int(cfg.get("intervalMin", 1)), 1)
            return now + timedelta(minutes=iv)
        at = str(cfg.get("atTime", "09:00"))
        try:
            hh, mm = map(int, at.split(":"))
        except ValueError:
            hh, mm = 9, 0
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return nxt

    def _schedule_cancel_timer(self) -> None:
        t = self._schedule_timer
        self._schedule_timer = None
        if t is not None:
            t.cancel()

    def _schedule_arm(self, cfg: dict) -> None:
        self._schedule_cancel_timer()
        if not (cfg.get("enabled") and cfg.get("workflowPath")):
            return
        nxt = self._schedule_next_fire(cfg)
        if nxt is None:
            return
        delay = max((nxt - datetime.now()).total_seconds(), 0.1)
        self._schedule_timer = threading.Timer(delay, self._schedule_fire)
        self._schedule_timer.daemon = True
        self._schedule_timer.start()

    def schedule_configure(self, cfg: Any) -> dict:
        if not isinstance(cfg, dict):
            raise ControllerError(-32602, "参数无效：cfg 须为对象")
        mode = cfg.get("mode", "每天时刻")
        if mode not in _VALID_SCHEDULE_MODES:
            raise ControllerError(-32602, "mode 须为 每天时刻 或 固定间隔", {"mode": mode})
        at_time = str(cfg.get("atTime", "09:00"))
        try:
            interval = int(cfg.get("intervalMin", 30))
        except (TypeError, ValueError):
            raise ControllerError(-32602, "intervalMin 须为整数")
        path = cfg.get("workflowPath", "") or ""
        enabled = bool(cfg.get("enabled", False)) and bool(path)
        clean = {
            "mode": mode, "atTime": at_time, "intervalMin": interval,
            "workflowPath": path, "enabled": enabled,
        }
        with self._lock:
            self._schedule = clean
        self._schedule_save(clean)
        self._schedule_arm(clean)
        nxt = self._schedule_next_fire(clean)
        return {"schedule": clean, "nextFire": nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""}

    def schedule_get(self) -> dict:
        with self._lock:
            cfg = dict(self._schedule)
        nxt = self._schedule_next_fire(cfg) if cfg else None
        return {"schedule": cfg, "nextFire": nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""}

    def _schedule_fire(self) -> None:
        """定时触发：从磁盘重载工作流并广播 workflow.changed + schedule.fired，再装定下一次。"""
        with self._lock:
            cfg = dict(self._schedule)
        path = cfg.get("workflowPath")
        if path and os.path.exists(path):
            try:
                wf = Workflow.load(path)
            except Exception:  # noqa: BLE001
                wf = None
            if wf is not None:
                with self._lock:
                    self.workflow = wf
                    self.path = path
                self._broadcast_workflow()
        self._notify("schedule.fired", {"path": path})
        self._schedule_arm(cfg)

    # ---- 停机清理（app.shutdown 时调用） ----
    def shutdown(self) -> None:
        self._schedule_cancel_timer()
        self._stop_key_listener()
