"""事件与工作流数据模型。

MacroEvent 与 Tauri 版 macro-recorder 的脚本格式保持字段兼容，
旧版录制的 .json 脚本可以直接导入使用。

**v4 起工作流是流程图**（节点 + 有向边），不再是「有序列表 + run_when 门控」。
旧文件仍能打开（见 `Workflow.load`），但 `run_when` 不再生效。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SCRIPT_VERSION = 4

# 滚轮增量单位：line=逐行（传统滚轮）pixel=逐点（触控板/妙控鼠标）
WHEEL_UNITS = ("line", "pixel")

# ---- 流程图出口名（port） ----
# 一条边 = (源节点 uid, 源节点的某个出口名) -> 目标节点 uid。
# 出口名是**协议的一部分**：前端按它渲染连接手柄，后端按它选下一跳。
# 同一个 (源节点, 出口名) 只允许一条边（`edge.add` 会替换旧的）——
# 不允许扇出，否则「一个出口同时跑两条路径」的执行语义要引入并行，
# 那和「顺序执行 + 汇合」是两套模型，混在一起没人能预测行为。
PORT_OUT = "out"        # 操作 / 开始：唯一出口
PORT_TRUE = "true"      # 条件：成立
PORT_FALSE = "false"    # 条件：不成立
PORT_ELSE = "else"      # 分支：所有 case 都不成立


def case_port(i: int) -> str:
    """分支节点的第 i 个 case 出口名（i 从 1 开始，与用户看到的序号一致）。"""
    return f"case:{i}"


# 分支节点的 case 上限。参数面板是按 ParamDef 平铺渲染的，每个 case 要占
# 两个参数（检测方式 + 取值），所以这个数字直接决定参数面板的长度。
MAX_BRANCH_CASES = 6


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
class Edge:
    """流程图里的一条有向边。

    `src` / `dst` 都是节点的 **uid**，不是下标——下标会随增删移动漂移，
    删掉一个节点就会让后面的连线整体错位接错人；uid 是稳定的。

    `dst` 为空串表示「这个出口没有下一跳」。之所以允许存下来，是为了让画布上
    「我留空了这个出口」和「这条边不存在」看起来一样——反正执行器都不会往下走。
    """
    src: str
    port: str = PORT_OUT
    dst: str = ""

    def to_dict(self) -> dict:
        return {"src": self.src, "port": self.port, "dst": self.dst}

    @staticmethod
    def from_dict(d: dict) -> "Edge":
        return Edge(src=str(d.get("src") or ""),
                    port=str(d.get("port") or PORT_OUT),
                    dst=str(d.get("dst") or ""))


@dataclass
class Node:
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)  # 前端列表/拖拽 key，稳定唯一
    name: str = ""                              # 自定义名（空则显示类型名）
    x: int = 0                                  # 画布坐标：后端只存不算，布局由前端负责
    y: int = 0

    def to_dict(self) -> dict:
        return {"type": self.type, "params": self.params, "enabled": self.enabled,
                "uid": self.uid, "name": self.name, "x": self.x, "y": self.y}

    @staticmethod
    def from_dict(d: dict) -> "Node":
        return Node(type=d.get("type", ""), params=d.get("params") or {},
                    enabled=d.get("enabled", True), uid=d.get("uid") or uuid.uuid4().hex,
                    name=str(d.get("name") or ""),
                    x=int(d.get("x") or 0), y=int(d.get("y") or 0))


@dataclass
class Workflow:
    version: int = SCRIPT_VERSION
    name: str = "未命名工作流"
    speed: float = 1.0              # 全局速度倍率，>1 加速
    repeat: int = 1                 # 整体循环次数
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    start: str = ""                 # 起始节点 uid；空 = 第一个 start 节点，再退回 nodes[0]
    # 加载时是否由 v3 线性列表迁移而来。**不落盘**（to_dict 里刻意不带），
    # 只在本次会话里让界面提示「条件不再自动门控，请重连」。
    migrated_from_list: bool = False

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "speed": self.speed,
            "repeat": self.repeat,
            "start": self.start,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
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
            wf.edges = [Edge.from_dict(e) for e in (data.get("edges") or [])]
            wf.start = str(data.get("start") or "")
        # v3 及更早：有序列表，没有边。接成线性边链让它还能跑。
        if wf.version < SCRIPT_VERSION and not wf.edges:
            wf.edges = _linear_edges(wf.nodes)
            _linear_positions(wf.nodes)
            wf.migrated_from_list = bool(wf.nodes)
        return wf


def _linear_edges(nodes: list[Node]) -> list[Edge]:
    """把 v3 的线性节点列表接成一条线性边链（v4 迁移用）。

    **不试图还原分支**：v3 的 `run_when` 依赖一个全局的「最近一次条件结果」，
    同一条件下可以有任意多节点挂不同的 run_when，语义无法一对一映射成边。
    所以这里只保证「还能从头跑到尾」，由调用方提示用户重连。

    条件节点要**两个出口都接上**：它没有 `out` 出口，只接一条的话执行到它就
    走不下去了（流程图直接断在那儿）。两条都接到下一个节点 = 条件不门控，
    与「run_when 失效」的承诺一致，而且画布上能一眼看出「这里该重连」。
    """
    out: list[Edge] = []
    for i in range(len(nodes) - 1):
        src, dst = nodes[i], nodes[i + 1].uid
        if src.type == "condition":
            out.append(Edge(src=src.uid, port=PORT_TRUE, dst=dst))
            out.append(Edge(src=src.uid, port=PORT_FALSE, dst=dst))
        elif src.type == "branch":
            # v3 没有 branch 节点，但防御一下：把 case 与 else 都接到下一个
            for k in range(1, MAX_BRANCH_CASES + 1):
                out.append(Edge(src=src.uid, port=case_port(k), dst=dst))
            out.append(Edge(src=src.uid, port=PORT_ELSE, dst=dst))
        else:
            out.append(Edge(src=src.uid, port=PORT_OUT, dst=dst))
    return out


# 迁移时的节点坐标：横向排成一条链。
# x 间距要大于卡片宽度（前端约 180px），否则卡片会首尾相叠。
MIGRATED_X0 = 80
MIGRATED_Y0 = 120
MIGRATED_DX = 240


def _linear_positions(nodes: list[Node]) -> None:
    """就地给迁移来的节点摆开位置。

    为什么必须有这一步：x/y 是 v4 才有的字段，v3 文件里一个坐标都没有，
    全部落在 (0, 0)。画布上看到的就是**一摞完全重叠的卡片**——看起来像
    「打开旧文件之后工作流被毁了」，而数据其实完好。这里按原顺序把它们摆开。

    **横向**而不是纵向：手柄在节点的左右两侧，横向链的边才是直的；
    纵向排列会让每条边都绕成 S 形。

    只在坐标为 (0, 0) 的节点上动手：手工给旧文件补过坐标的人不该被覆盖。
    """
    for i, n in enumerate(nodes):
        if n.x or n.y:
            continue
        n.x = MIGRATED_X0 + i * MIGRATED_DX
        n.y = MIGRATED_Y0


def now_ms() -> int:
    return int(time.monotonic() * 1000)