"""后端应用控制器：持有 current_workflow 作为唯一真源（§10）。

前端近乎无状态：任何结构操作（增删改移/启停/改参）→ 调用这里 → 改树 →
通过 on_notify 广播 `workflow.changed`。运行/录制/热键/定时等状态也在这里。

本模块只装业务逻辑，不碰 stdio/JSON-RPC 传输（见 rpc/server.py）。
"""
from __future__ import annotations

import os
import threading
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
        self.running = False
        self.recording = False
        self._lock = threading.RLock()
        self._notify: Callable[[str, Any], None] = lambda _m, _p: None

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
