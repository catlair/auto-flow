"""内置任务节点：鼠标、键盘、延时、录制回放、图像/文字/YOLO 查找、条件、注释。"""
from __future__ import annotations

from pynput.mouse import Button

from typing import Optional

from tasks.base import BaseTask, ParamDef, register
from core.player import PlayOptions
from core.events import MacroEvent
from core.keymap import name_to_key

# 全局热键名：回放录制事件时跳过，避免回放又触发热键
HOTKEY_NAMES = {"F9", "F10", "F11"}


def tolerant_event(d: dict) -> Optional[MacroEvent]:
    """容忍缺字段/多字段的 MacroEvent 构造；非 dict（如损坏文件的字符串/摘要
    {"count":N}）返回 None，由调用方跳过——绝不抛 'str' has no 'get'。"""
    if not isinstance(d, dict):
        return None
    keys = {"ts_ms", "kind", "key", "button", "pressed", "x", "y", "wheel_dx", "wheel_dy"}
    defaults = {"ts_ms": 0, "kind": "move", "key": None, "button": None, "pressed": None,
                "x": 0, "y": 0, "wheel_dx": 0, "wheel_dy": 0}
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


class MouseActionTask(BaseTask):
    type = "mouse"
    name = "鼠标操作"

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
        btn = {"left": Button.left, "right": Button.right, "middle": Button.middle}.get(
            p.get("button", "left"), Button.left)
        if p.get("action") == "double_click":
            m.click(btn, 2)
        elif p.get("action") == "press":
            m.press(btn)
        elif p.get("action") == "release":
            m.release(btn)
        else:
            m.click(btn, 1)


class KeyboardInputTask(BaseTask):
    type = "keyboard"
    name = "键盘输入"

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


class DelayTask(BaseTask):
    type = "delay"
    name = "延时等待"

    def __init__(self) -> None:
        super().__init__()
        self.params = [ParamDef("ms", "毫秒", "int", 500, min_value=0)]

    def run(self, ctx) -> None:
        ctx.player.wait(float(ctx.params.get("ms", 500)) / 1000.0 / max(ctx.speed, 0.01))


@register
class RecordReplayTask(BaseTask):
    type = "record_replay"
    name = "录制回放"

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
                btn = Button.left
                ctx.player.mouse.click(btn, 2 if action == "double_click" else 1)
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
                btn = Button.left
                ctx.player.mouse.click(btn, 2 if action == "double_click" else 1)
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
                btn = Button.left
                ctx.player.mouse.click(btn, 2 if action == "double_click" else 1)
                return
            if _time.monotonic() >= deadline:
                if p.get("not_found", "跳过") == "停止工作流":
                    ctx.stop_workflow()
                return
            ctx.player.wait(float(p.get("interval_ms", 800)) / 1000.0)


@register
class ConditionTask(BaseTask):
    """检测条件并把结果写入执行状态，供后续节点的「执行条件」使用。"""
    type = "condition"
    name = "条件判断"

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("check", "检测方式", "select", "图像存在",
                     ["图像存在", "文字存在", "目标存在(YOLO)"]),
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
        from core import vision, ocr, yolo
        p = ctx.params
        method = p.get("check", "图像存在")
        timeout = float(p.get("timeout_s", 0.0))
        deadline = _time.monotonic() + timeout

        def check() -> bool:
            if method == "文字存在":
                return ocr.find_text(str(p.get("text", ""))) is not None
            if method == "目标存在(YOLO)":
                from core.paths import default_model
                model = str(p.get("model_path") or "") or default_model()
                if not model:
                    return False
                engine = yolo.get_engine(model)
                label = str(p.get("label", "")).strip()
                conf = min(float(p.get("confidence", 0.8)), 0.99)
                dets = detect_on_all_screens(engine, conf)
                return any(not label or d.label == label for d in dets)
            m = vision.find_template(float(p.get("confidence", 0.8)),
                                     template_path=str(p.get("image_path", "")))
            return m.found

        while True:
            if ctx.player.stopping:
                ctx.set_condition(False)
                return
            if check():
                ctx.set_condition(True)
                return
            if _time.monotonic() >= deadline:
                ctx.set_condition(False)
                return
            ctx.player.wait(0.3)


class NoteTask(BaseTask):
    type = "note"
    name = "注释"

    def __init__(self) -> None:
        super().__init__()
        self.params = [ParamDef("text", "内容", "text", "")]

    def run(self, ctx) -> None:
        pass


register(MouseActionTask())
register(KeyboardInputTask())
register(DelayTask())
register(RecordReplayTask())
register(ImageClickTask())
register(OcrClickTask())
register(YoloClickTask())
register(ConditionTask())
register(NoteTask())
