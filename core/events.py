"""事件与工作流数据模型。

MacroEvent 与 Tauri 版 macro-recorder 的脚本格式保持字段兼容，
旧版录制的 .json 脚本可以直接导入使用。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SCRIPT_VERSION = 3

# 滚轮增量单位：line=逐行（传统滚轮）pixel=逐点（触控板/妙控鼠标）
WHEEL_UNITS = ("line", "pixel")


@dataclass
class MacroEvent:
    """时间线上的一个输入事件（v3）。

    v3 相对 v2 新增语义字段（全部有默认值，v2 旧脚本可直接构造，等价于默认值）：

    - `dragged`：该移动发生在按键保持期间（拖拽）。录制端必须保留，
      回放端据此投递 `kCGEvent*MouseDragged` 而非 `MouseMoved`——**这是拖拽类
      操作能否回放成功的唯一判据**，v2 丢弃它导致所有拖拽回放失效。
    - `clicks`：点击序列号（1=单击 2=双击 3=三击），回放写入
      `kCGMouseEventClickState`，否则双击会被目标应用识别成两次单击。
    - `flags`：事件时刻的 `CGEventFlags` 快照（修饰键状态）。
    - `wheel_unit`：滚轮增量单位，见 `WHEEL_UNITS`。v2 把滚轮增量一律当像素
      投递，而传统滚轮的增量是"行"，导致回放几乎不滚动。
    - `x` / `y`：**所有事件都带坐标**（含 key 事件）。v2 的 key 事件恒为 (0,0)。
    - `text` + `kind="text"`：一次**文本提交**（输入法上屏的中文/emoji、或任何走
      `CGEventKeyboardSetUnicodeString` 的投递）。这类输入在事件层是"一次带
      Unicode 的按键"，按普通按键记录会退化成拼音字母或废键码，回放彻底失真。
      回放端按 `core.mactype.type_text` 原样投递，不经过键码映射。
    """

    ts_ms: int                      # 相对录制起点的时间戳
    kind: str                       # key / mouse / move / wheel / text
    key: Optional[str] = None       # 键名（key 事件）
    button: Optional[str] = None    # left / right / middle（mouse 事件）
    pressed: Optional[bool] = None  # mouse: True 按下 False 释放; key 事件同义
    x: int = 0
    y: int = 0
    wheel_dx: int = 0
    wheel_dy: int = 0
    dragged: bool = False           # v3：拖拽中的移动
    clicks: int = 1                 # v3：点击序列号 1/2/3
    flags: int = 0                  # v3：CGEventFlags 快照
    wheel_unit: str = "line"        # v3：line / pixel
    text: str = ""                  # text 事件：一次提交的文本（中文/emoji/整段）

    def describe(self) -> str:
        if self.kind == "text":
            return f"文本 {self.text!r} ({self.x},{self.y})"
        if self.kind == "key":
            return f"按键 {'↓' if self.pressed else '↑'} {self.key} ({self.x},{self.y})"
        if self.kind == "mouse":
            suffix = f" ×{self.clicks}" if self.clicks > 1 else ""
            return (f"鼠标 {'按下' if self.pressed else '释放'} "
                    f"{self.button}{suffix} ({self.x},{self.y})")
        if self.kind == "wheel":
            unit = "行" if self.wheel_unit == "line" else "px"
            return (f"滚轮 dy={self.wheel_dy}{unit} dx={self.wheel_dx}{unit} "
                    f"({self.x},{self.y})")
        return f"{'拖拽' if self.dragged else '移动'} ({self.x},{self.y})"


@dataclass
class RecordResult:
    events: list[MacroEvent] = field(default_factory=list)
    origin_x: int = 0
    origin_y: int = 0
    stopped_by_limit: bool = False
    # 丢帧定位计量：系统投递数 / 被降采样丢弃数 / 窗口过滤丢弃数 / 超上限丢弃数
    n_captured: int = 0
    n_filtered: int = 0              # = n_decimated + n_window_dropped（兼容 v2 口径）
    n_limit_dropped: int = 0
    n_decimated: int = 0             # v3：保轨采样丢弃的冗余移动（不含信息）
    n_window_dropped: int = 0        # v3：窗口过滤丢弃（默认关闭，应恒为 0）
    n_text_merged: int = 0           # v3：被聚合进同一条 text 事件的提交次数
    # 监听线程中途死亡检测（tap 回调抛异常 → 线程退出，此后事件全丢，表现为「中间断段」）
    mouse_listener_died: bool = False
    kb_listener_died: bool = False


@dataclass
class Node:
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)  # 前端列表/拖拽 key，稳定唯一
    name: str = ""                              # 自定义名（空则显示类型名）

    def to_dict(self) -> dict:
        return {"type": self.type, "params": self.params, "enabled": self.enabled,
                "uid": self.uid, "name": self.name}

    @staticmethod
    def from_dict(d: dict) -> "Node":
        return Node(type=d.get("type", ""), params=d.get("params") or {},
                    enabled=d.get("enabled", True), uid=d.get("uid") or uuid.uuid4().hex,
                    name=str(d.get("name") or ""))


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
