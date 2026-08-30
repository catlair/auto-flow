"""事件与工作流数据模型。

MacroEvent 与 Tauri 版 macro-recorder 的脚本格式保持字段兼容，
旧版录制的 .json 脚本可以直接导入使用。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SCRIPT_VERSION = 2


@dataclass
class MacroEvent:
    ts_ms: int                      # 相对录制起点的时间戳
    kind: str                       # key / mouse / move / wheel
    key: Optional[str] = None       # 键名（key 事件）
    button: Optional[str] = None    # left / right / middle（mouse 事件）
    pressed: Optional[bool] = None  # mouse: True 按下 False 释放; key 事件同义
    x: int = 0
    y: int = 0
    wheel_dx: int = 0
    wheel_dy: int = 0

    def describe(self) -> str:
        if self.kind == "key":
            return f"按键 {'↓' if self.pressed else '↑'} {self.key}"
        if self.kind == "mouse":
            return f"鼠标 {'按下' if self.pressed else '释放'} {self.button} ({self.x},{self.y})"
        if self.kind == "wheel":
            return f"滚轮 dy={self.wheel_dy} ({self.x},{self.y})"
        return f"移动 ({self.x},{self.y})"


@dataclass
class RecordResult:
    events: list[MacroEvent] = field(default_factory=list)
    origin_x: int = 0
    origin_y: int = 0
    stopped_by_limit: bool = False


@dataclass
class Node:
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"type": self.type, "params": self.params, "enabled": self.enabled}

    @staticmethod
    def from_dict(d: dict) -> "Node":
        return Node(type=d.get("type", ""), params=d.get("params") or {}, enabled=d.get("enabled", True))


@dataclass
class Workflow:
    version: int = SCRIPT_VERSION
    name: str = "未命名工作流"
    speed: float = 1.0              # 全局速度倍率，>1 加速
    repeat: int = 1                 # 整体循环次数
    nodes: list[Node] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "speed": self.speed,
            "repeat": self.repeat,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path: str) -> "Workflow":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        wf = Workflow(
            version=data.get("version", 1),
            name=data.get("name", "未命名工作流"),
            speed=float(data.get("speed", 1.0)),
            repeat=int(data.get("repeat", 1)),
        )
        nodes = data.get("nodes")
        if nodes is None and isinstance(data.get("events"), list):
            # 兼容 Tauri 版裸事件脚本：整体作为一个录制回放节点
            from tasks.builtin import tolerant_event
            events = [tolerant_event(e) for e in data["events"]]
            wf.nodes.append(Node(type="record_replay", params={
                "events": [asdict(ev) for ev in events],
                "origin_x": int(data.get("origin_x", 0)),
                "origin_y": int(data.get("origin_y", 0)),
                "use_relative": True,
            }))
        else:
            wf.nodes = [Node.from_dict(n) for n in (nodes or [])]
        return wf


def now_ms() -> int:
    return int(time.monotonic() * 1000)
