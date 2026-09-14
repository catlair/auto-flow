"""内置任务节点：开始/结束、鼠标、键盘、延时、录制回放、图像/文字/YOLO 查找、
条件、多路分支、注释。

每个类用 `@register` 注册，并用 `order` 声明它在「添加节点」菜单里的位置。
菜单顺序是：开始 → 鼠标 → 键盘 → 延时 → 录制回放 → 图像 → OCR → YOLO
→ 条件 → 多路分支 → 注释 → 结束。新增节点请给一个 order 值，否则会落到菜单末尾。

**节点有「出口」**（v4 起工作流是流程图，见 `core/events.py`）：操作类节点只有
`out` 一个出口；`condition` 有 `true`/`false`；`branch` 有 `case:1..N` 与 `else`；
`end` 没有出口。节点通过 `ctx.set_port(name)` 声明本次该走哪个出口，不调用就默认 `out`。
"""
from __future__ import annotations

from pynput.mouse import Button

from typing import Optional

from tasks.base import BaseTask, ParamDef, register
from core.player import PlayOptions
from core.events import (MacroEvent, MAX_BRANCH_CASES,
                         PORT_OUT, PORT_TRUE, PORT_FALSE, PORT_ELSE, case_port)
from core.keymap import name_to_key

# 全局热键名：回放录制事件时跳过，避免回放又触发热键
HOTKEY_NAMES = {"F9", "F10", "F11"}

# 「检测方式」三个选项被 condition 与 branch 共用，抽出来避免两处漂移
CHECK_KINDS = ["图像存在", "文字存在", "目标存在(YOLO)"]
CASE_VALUE_TOOLTIP = ("图像存在 = 模板图路径（可点「选择」挑文件）；"
                      "文字存在 = 要找的文字；目标存在(YOLO) = 目标类别，留空=任意")


def tolerant_event(d: dict) -> Optional[MacroEvent]:
    """容忍缺字段/多字段的 MacroEvent 构造；非 dict（如损坏文件的字符串/摘要
    {"count":N}）返回 None，由调用方跳过——绝不抛 'str' has no 'get'。

    v2 旧脚本没有 dragged/clicks/flags/wheel_unit/text，取默认值即可（等价于
    "非拖拽的单击、按行滚轮、无文本"），因此 v2 → v3 不需要单独迁移步骤。
    """
    if not isinstance(d, dict):
        return None
    keys = {"ts_ms", "kind", "key", "button", "pressed", "x", "y", "wheel_dx", "wheel_dy",
            "dragged", "clicks", "flags", "wheel_unit", "text"}
    defaults = {"ts_ms": 0, "kind": "move", "key": None, "button": None, "pressed": None,
                "x": 0, "y": 0, "wheel_dx": 0, "wheel_dy": 0,
                "dragged": False, "clicks": 1, "flags": 0, "wheel_unit": "line",
                "text": ""}
    data = {k: d.get(k, v) for k, v in defaults.items() if k in keys}
    return MacroEvent(**data)


def detect_on_all_screens(engine, conf: float) -> list:
    """在所有显示器上跑检测，坐标统一换算成**全局逻辑坐标**。

    单屏时等价于原来的「主屏检测」；多屏时目标在哪块屏上都能找到，
    且 `d.x/d.y` 已加上该屏的 `left/top` 偏移，可直接用于点击。
    """
    from core import vision
    dets: list = []
    for cap in vision.captures():
        if cap.image.size == 0:
            continue
        for d in engine.detect_bgr(cap.image, cap.scale, conf):
            d.x += cap.left
            d.y += cap.top
            dets.append(d)
    dets.sort(key=lambda d: -d.confidence)
    return dets


@register
class StartTask(BaseTask):
    """流程图起点。不做事，只是给画布一个明确的入口。

    执行器并不依赖它——没有 start 节点时退回「第一个节点」（见
    `Executor.entry_uid`）。它的价值是**显式**：用户一眼能看出从哪开始，
    而不是靠「把节点拖到最前面」这种隐式约定。
    """
    type = "start"
    name = "开始"
    order = 5

    def run(self, ctx) -> None:
        return


@register
class MouseActionTask(BaseTask):
    type = "mouse"
    name = "鼠标操作"
    order = 10

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("action", "动作", "select", "click", ["click", "double_click", "move", "press", "release"]),
            ParamDef("button", "按键", "select", "left", ["left", "right", "middle"]),
            ParamDef("x", "X", "int", 0, min_value=0),
            ParamDef("y", "Y", "int", 0, min_value=0),
            ParamDef("use_relative", "相对偏移", "bool", False, tooltip="以工作流基点为原点计算偏移"),
        ]

    def run(self, ctx) -> None:
        p = ctx.params
        dx, dy = ctx.offset_for(bool(p.get("use_relative")), int(p.get("x", 0)), int(p.get("y", 0)))
        m = ctx.player.mouse
        if p.get("action") == "move":
            ctx.player.glide_now((dx, dy))
            return
        ctx.player.glide_now((dx, dy))
        btn = str(p.get("button", "left"))
        if p.get("action") == "double_click":
            m.click(btn, 2)
        elif p.get("action") == "press":
            m.press(btn)
        elif p.get("action") == "release":
            m.release(btn)
        else:
            m.click(btn, 1)


@register
class KeyboardInputTask(BaseTask):
    type = "keyboard"
    name = "键盘输入"
    order = 20

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("mode", "模式", "select", "text", ["text", "hotkey", "key"],
                     tooltip="text: 输入文本 | hotkey: 组合键(如 ctrl+c) | key: 单键按下再释放"),
            ParamDef("text", "文本", "text", ""),
            ParamDef("keys", "按键", "keys", "", tooltip="单键名或 + 连接的组合，如 ctrl+c / Space"),
        ]

    def run(self, ctx) -> None:
        p = ctx.params
        kb = ctx.player.kb
        mode = p.get("mode", "text")
        if mode == "text":
            kb.type(str(p.get("text", "")))
        elif mode == "key":
            key = name_to_key(str(p.get("keys", "")))
            if key is not None:
                kb.press(key)
                kb.release(key)
        else:  # hotkey
            parts = [s.strip() for s in str(p.get("keys", "")).split("+") if s.strip()]
            keys = [name_to_key(s) for s in parts]
            keys = [k for k in keys if k is not None]
            for k in keys:
                kb.press(k)
            for k in reversed(keys):
                kb.release(k)


@register
class DelayTask(BaseTask):
    type = "delay"
    name = "延时等待"
    order = 30

    def __init__(self) -> None:
        super().__init__()
        self.params = [ParamDef("ms", "毫秒", "int", 500, min_value=0)]

    def run(self, ctx) -> None:
        ctx.player.wait(float(ctx.params.get("ms", 500)) / 1000.0 / max(ctx.speed, 0.01))


@register
class RecordReplayTask(BaseTask):
    type = "record_replay"
    name = "录制回放"
    order = 40

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("events", "事件序列", "events", []),
            ParamDef("origin_x", "原点X", "int", 0),
            ParamDef("origin_y", "原点Y", "int", 0),
            ParamDef("use_relative", "相对坐标", "bool", False),
            ParamDef("speed", "速度倍率", "float", 1.0, min_value=0.1),
            ParamDef("repeat", "重复次数", "int", 1, min_value=1),
        ]

    def run(self, ctx) -> None:
        # 「重复次数」不在此处循环：执行器已按 ctx.params["repeat"] 重复调用本任务，
        # 两层循环会把 N 次放大成 N² 次（再乘工作流整体循环）。
        p = ctx.params
        events = [e for e in (tolerant_event(x) for x in (p.get("events") or [])) if e]
        opt = PlayOptions(
            speed=float(p.get("speed", 1.0)) * ctx.speed,
            use_relative=bool(p.get("use_relative", False)),
            base_x=ctx.base_x, base_y=ctx.base_y,
            origin_x=int(p.get("origin_x", 0)), origin_y=int(p.get("origin_y", 0)),
            suppress_keys=set(HOTKEY_NAMES),
        )
        ctx.player.play(events, opt, on_progress=ctx.on_progress)


@register
class ImageClickTask(BaseTask):
    type = "image_click"
    name = "图像匹配点击"
    order = 50

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("image_path", "模板图片", "file", "", tooltip="要找的小图（可点面板上的「截取模板」框选）"),
            ParamDef("confidence", "置信度", "float", 0.8, min_value=0.1),
            ParamDef("action", "动作", "select", "click", ["click", "double_click", "move"]),
            ParamDef("timeout_s", "查找超时(秒)", "float", 3.0, min_value=0.0),
            ParamDef("interval_ms", "重试间隔(ms)", "int", 400, min_value=50),
            ParamDef("not_found", "找不到时", "select", "跳过", ["跳过", "停止工作流"]),
            ParamDef("offset_x", "偏移X", "int", 0),
            ParamDef("offset_y", "偏移Y", "int", 0),
        ]

    def run(self, ctx) -> None:
        import time as _time
        from core import vision
        p = ctx.params
        timeout = float(p.get("timeout_s", 3.0))
        deadline = _time.monotonic() + timeout
        while True:
            if ctx.player.stopping:
                return
            m = vision.find_template(float(p.get("confidence", 0.8)),
                                     template_path=str(p.get("image_path", "")))
            if m.found:
                x, y = m.x + int(p.get("offset_x", 0)), m.y + int(p.get("offset_y", 0))
                ctx.player.glide_now((x, y))
                action = p.get("action", "click")
                if action == "move":
                    return
                ctx.player.mouse.click("left", 2 if action == "double_click" else 1)
                return
            if _time.monotonic() >= deadline:
                if p.get("not_found", "跳过") == "停止工作流":
                    ctx.stop_workflow()
                return
            ctx.player.wait(float(p.get("interval_ms", 400)) / 1000.0)


@register
class OcrClickTask(BaseTask):
    type = "ocr_click"
    name = "找文字点击"
    order = 60

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("text", "文字", "text", "", tooltip="要找的屏幕文字（macOS Vision 离线识别，支持中英文）"),
            ParamDef("action", "动作", "select", "click", ["click", "double_click", "move"]),
            ParamDef("timeout_s", "查找超时(秒)", "float", 3.0, min_value=0.0),
            ParamDef("interval_ms", "重试间隔(ms)", "int", 500, min_value=50),
            ParamDef("not_found", "找不到时", "select", "跳过", ["跳过", "停止工作流"]),
            ParamDef("offset_x", "偏移X", "int", 0),
            ParamDef("offset_y", "偏移Y", "int", 0),
        ]

    def run(self, ctx) -> None:
        import time as _time
        from core import ocr
        p = ctx.params
        deadline = _time.monotonic() + float(p.get("timeout_s", 3.0))
        while True:
            if ctx.player.stopping:
                return
            hit = ocr.find_text(str(p.get("text", "")))
            if hit:
                x, y = hit.x + int(p.get("offset_x", 0)), hit.y + int(p.get("offset_y", 0))
                ctx.player.glide_now((x, y))
                action = p.get("action", "click")
                if action == "move":
                    return
                ctx.player.mouse.click("left", 2 if action == "double_click" else 1)
                return
            if _time.monotonic() >= deadline:
                if p.get("not_found", "跳过") == "停止工作流":
                    ctx.stop_workflow()
                return
            ctx.player.wait(float(p.get("interval_ms", 500)) / 1000.0)


@register
class YoloClickTask(BaseTask):
    type = "yolo_click"
    name = "YOLO 找目标点击"
    order = 70

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("model_path", "模型文件", "file", "", tooltip="YOLO ONNX 模型（如 yolov8n.onnx），旁边可放同名 .txt 自定义类别"),
            ParamDef("label", "目标类别", "text", "person", tooltip="COCO 类别名（person/bus/cat…）或自定义类别名；留空=任意目标"),
            ParamDef("confidence", "置信度", "float", 0.5, min_value=0.05),
            ParamDef("index", "命中序号", "int", 1, min_value=1, tooltip="屏幕上有多个目标时点第几个（按置信度排序），1=最高"),
            ParamDef("action", "动作", "select", "click", ["click", "double_click", "move"]),
            ParamDef("timeout_s", "查找超时(秒)", "float", 3.0, min_value=0.0),
            ParamDef("interval_ms", "重试间隔(ms)", "int", 800, min_value=100),
            ParamDef("not_found", "找不到时", "select", "跳过", ["跳过", "停止工作流"]),
            ParamDef("offset_x", "偏移X", "int", 0),
            ParamDef("offset_y", "偏移Y", "int", 0),
        ]

    def run(self, ctx) -> None:
        import time as _time
        from core import yolo
        from core.paths import default_model
        p = ctx.params
        model = str(p.get("model_path") or "") or default_model()
        if not model:
            raise FileNotFoundError("未指定 YOLO 模型，且未找到默认模型 yolo11n.onnx")
        engine = yolo.get_engine(model)
        label = str(p.get("label", "")).strip()
        want_idx = max(int(p.get("index", 1)), 1)
        deadline = _time.monotonic() + float(p.get("timeout_s", 3.0))
        while True:
            if ctx.player.stopping:
                return
            dets = [d for d in detect_on_all_screens(engine, float(p.get("confidence", 0.5)))
                    if not label or d.label == label]
            if len(dets) >= want_idx:
                d = dets[want_idx - 1]
                x, y = d.x + int(p.get("offset_x", 0)), d.y + int(p.get("offset_y", 0))
                ctx.player.glide_now((x, y))
                action = p.get("action", "click")
                if action == "move":
                    return
                ctx.player.mouse.click("left", 2 if action == "double_click" else 1)
                return
            if _time.monotonic() >= deadline:
                if p.get("not_found", "跳过") == "停止工作流":
                    ctx.stop_workflow()
                return
            ctx.player.wait(float(p.get("interval_ms", 800)) / 1000.0)


def _detect(kind: str, value: str = "", *, confidence: float = 0.8,
            model_path: str = "") -> bool:
    """执行一次「图像存在 / 文字存在 / 目标存在(YOLO)」检测。

    `condition` 与 `branch` **共用这一份实现**——两者对同一种检测方式的语义
    必须完全一致，否则「条件成立」和「分支命中」会在同一张屏上给出不同答案，
    而用户完全无从判断该信哪个。`value` 的含义随 `kind` 变：
    图像存在=模板图路径；文字存在=要找的文字；YOLO=目标类别（留空=任意）。
    """
    from core import vision, ocr, yolo
    if kind == "文字存在":
        return ocr.find_text(str(value)) is not None
    if kind == "目标存在(YOLO)":
        from core.paths import default_model
        model = str(model_path or "") or default_model()
        if not model:
            return False
        engine = yolo.get_engine(model)
        want = str(value).strip()
        dets = detect_on_all_screens(engine, min(float(confidence), 0.99))
        return any(not want or d.label == want for d in dets)
    return vision.find_template(float(confidence), template_path=str(value)).found


@register
class ConditionTask(BaseTask):
    """条件判断：两个出口——成立走 `true`，不成立走 `false`。"""
    type = "condition"
    name = "条件判断"
    order = 80

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("check", "检测方式", "select", "图像存在", list(CHECK_KINDS)),
            ParamDef("image_path", "模板图片", "file", ""),
            ParamDef("text", "文字", "text", "", tooltip="检测方式=文字存在 时使用"),
            ParamDef("model_path", "YOLO 模型", "file", "", tooltip="检测方式=目标存在(YOLO) 时使用"),
            ParamDef("label", "目标类别", "text", "", tooltip="留空=任意检测到的目标"),
            ParamDef("confidence", "置信度", "float", 0.8, min_value=0.05),
            ParamDef("timeout_s", "等待超时(秒)", "float", 0.0, min_value=0.0,
                     tooltip=">0 时在超时时间内反复检测，出现即算成立"),
        ]

    def run(self, ctx) -> None:
        import time as _time
        p = ctx.params
        method = p.get("check", "图像存在")
        deadline = _time.monotonic() + float(p.get("timeout_s", 0.0))

        def value_for() -> str:
            if method == "文字存在":
                return str(p.get("text", ""))
            if method == "目标存在(YOLO)":
                return str(p.get("label", ""))
            return str(p.get("image_path", ""))

        def check() -> bool:
            return _detect(method, value_for(),
                           confidence=float(p.get("confidence", 0.8)),
                           model_path=str(p.get("model_path") or ""))

        while True:
            if ctx.player.stopping:
                ctx.set_port(PORT_FALSE)
                return
            if check():
                ctx.set_port(PORT_TRUE)
                return
            if _time.monotonic() >= deadline:
                ctx.set_port(PORT_FALSE)
                return
            ctx.player.wait(0.3)


@register
class BranchTask(BaseTask):
    """多路分支：按序号逐个检测，命中**第一个**成立的走它的 `case:i` 出口，都不成立走 `else`。

    为什么 case 要按序号求值而不是「全都测一遍挑最好的」：多路分支的语义就是
    「先到先得」，用户把哪一路放在前面就是在表达优先级。改成挑最优会让顺序
    变得没有意义，用户就没法表达「先看 A，A 不在再看 B」。
    """
    type = "branch"
    name = "多路分支"
    order = 85

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("case_count", "分支数量", "int", 2,
                     min_value=2, max_value=MAX_BRANCH_CASES,
                     tooltip=f"最多 {MAX_BRANCH_CASES} 路；每路一个出口，"
                             f"外加一个「都不成立」出口"),
        ]
        for i in range(1, MAX_BRANCH_CASES + 1):
            show = {"key": "case_count", "gte": i}
            self.params.append(ParamDef(
                f"case{i}_kind", f"分支 {i} 检测方式", "select", "图像存在",
                list(CHECK_KINDS), show_if=show))
            self.params.append(ParamDef(
                f"case{i}_value", f"分支 {i} 取值", "text", "",
                show_if=show, pick=True, tooltip=CASE_VALUE_TOOLTIP))
        self.params.append(ParamDef("confidence", "置信度", "float", 0.8, min_value=0.05))
        self.params.append(ParamDef("timeout_s", "等待超时(秒)", "float", 0.0, min_value=0.0,
                                    tooltip=">0 时在超时时间内反复检测，任一路命中即走它"))

    def run(self, ctx) -> None:
        import time as _time
        p = ctx.params
        n = max(2, min(int(p.get("case_count", 2) or 2), MAX_BRANCH_CASES))
        conf = float(p.get("confidence", 0.8))
        deadline = _time.monotonic() + float(p.get("timeout_s", 0.0))

        def pick() -> str:
            for i in range(1, n + 1):
                if _detect(str(p.get(f"case{i}_kind", "图像存在")),
                           str(p.get(f"case{i}_value", "")), confidence=conf):
                    return case_port(i)
            return PORT_ELSE

        while True:
            if ctx.player.stopping:
                ctx.set_port(PORT_ELSE)
                return
            hit = pick()
            if hit != PORT_ELSE:
                ctx.set_port(hit)
                return
            if _time.monotonic() >= deadline:
                ctx.set_port(PORT_ELSE)
                return
            ctx.player.wait(0.3)


@register
class NoteTask(BaseTask):
    type = "note"
    name = "注释"
    order = 90

    def __init__(self) -> None:
        super().__init__()
        self.params = [ParamDef("text", "内容", "text", "")]

    def run(self, ctx) -> None:
        pass


@register
class EndTask(BaseTask):
    """流程图终点。执行到这里，本路径结束。

    没有出边也会结束，所以 end 不是必需的；它的价值是**显式**——让
    「这条分支到此为止」和「我忘了连线」在画布上能区分开。
    """
    type = "end"
    name = "结束"
    order = 95

    def run(self, ctx) -> None:
        return


# 菜单顺序由各类的 `order` 决定（§9.4），见 tasks/base.py::all_definitions。
# 这里此前还有 9 行 `register(实例)`：对已被 @register 装饰的类是**空操作**
# （dict 重复赋值保留原位置），只对没装饰的那几个真正生效——既误导，又让菜单
# 顺序变成「先装饰的几个、后补的几个」。现在全部改用装饰器 + order。
