"""内置任务节点：开始/结束、鼠标、键盘、延时、录制回放、图像/文字/YOLO 查找、
条件、条件组、多路分支、注释。

每个类用 `@register` 注册，并用 `order` 声明它在「添加节点」菜单里的位置。
菜单顺序是：开始 → 鼠标 → 键盘 → 延时 → 录制回放 → 图像 → OCR → YOLO
→ 条件 → 条件组 → 多路分支 → 注释 → 结束。新增节点请给一个 order 值，否则会落到菜单末尾。

**节点有「出口」**（v4 起工作流是流程图，见 `core/events.py`）：操作类节点只有
`out` 一个出口；`condition` 与 `condition_group` 有 `true`/`false`；`branch` 有
`case:1..N` 与 `else`；`end` 没有出口。节点通过 `ctx.set_port(name)` 声明本次该走
哪个出口，不调用就默认 `out`。
"""
from __future__ import annotations

import logging
import random

from pynput.mouse import Button

from typing import Optional

from tasks.base import BaseTask, ParamDef, register
from core.player import PlayOptions
from core.events import (MacroEvent, MAX_BRANCH_CASES, MAX_GROUP_CONDITIONS,
                         PORT_OUT, PORT_TRUE, PORT_FALSE, PORT_ELSE, case_port)
from core.keymap import name_to_key

logger = logging.getLogger("tasks.builtin")

# 全局热键名：回放录制事件时跳过，避免回放又触发热键
HOTKEY_NAMES = {"F9", "F10", "F11"}

# 「检测方式」三个选项被 condition、condition_group 与 branch 共用，抽出来避免两处漂移
CHECK_KINDS = ["图像存在", "文字存在", "目标存在(YOLO)"]
CASE_VALUE_TOOLTIP = ("图像存在 = 模板图路径（可点「选择」挑文件）；"
                      "文字存在 = 要找的文字；目标存在(YOLO) = 目标类别，留空=任意")

# 「点击点落在目标框内」的三种模式。被 image_click 与 ocr_click 共用，抽成常量
# 是因为参数定义、run() 里的判断、用例三处都要引用同一批字面量——各写一遍，
# 改了面板上的选项却忘了改判断，表现是「选了随机却总是点中心」，没有任何报错。
CLICK_POS_CENTER = "目标中心"
CLICK_POS_RANDOM = "框内随机"
CLICK_POS_CUSTOM = "框内自定义"
CLICK_POS_KINDS = (CLICK_POS_CENTER, CLICK_POS_RANDOM, CLICK_POS_CUSTOM)
CLICK_POS_TOOLTIP = ("目标中心：点框正中（默认，行为与以前一致）| "
                     "框内随机：框内均匀取一点，更像真人、也避免每次都压在同一处 | "
                     "框内自定义：按下面的百分比取，四个角就是 0/0、100/0、0/100、100/100")


def click_point_in_box(cx: int, cy: int, w: int, h: int, *,
                       mode: str = CLICK_POS_CENTER,
                       rel_x: float = 50.0, rel_y: float = 50.0,
                       inset: int = 0, rand=None) -> tuple:
    """在检测到的目标框里取一个点击点，返回全局逻辑坐标。

    `(cx, cy)` 是框中心、`(w, h)` 是框的逻辑尺寸（`vision.MatchResult` 与
    `ocr.TextHit` 都给）。`inset` 把框四边各往里收这么多像素——避免点到边缘，
    也避免「随机」时压到边框上。

    **拿不到框时一律回落到中心**（`w <= 0` 或 `h <= 0`，或 `mode` 不认识）：
    检测层在有些路径上给不出尺寸（老数据、退化结果），此时中心仍是最合理的猜测。
    这里抛错、或返回 (0, 0)，都会让「本来能跑的流程」突然点向屏幕左上角。

    `rand` 可注入（默认 `random.random`），用例才能把随机点钉死。
    """
    if w <= 0 or h <= 0 or mode not in CLICK_POS_KINDS:
        return int(cx), int(cy)
    if mode == CLICK_POS_CENTER:
        return int(cx), int(cy)

    # 内缩不能把框收没——收没了就谈不上「框内」。钳到至少留 1 像素：这样
    # 「内缩填太大」只会退化成点正中心，而不是抛错或把点送到框外。
    inset = max(int(inset), 0)
    inset = min(inset, (w - 1) // 2, (h - 1) // 2)
    left, top = cx - w / 2 + inset, cy - h / 2 + inset
    bw, bh = w - 2 * inset, h - 2 * inset
    # 跨度取 `bw - 1`（可用像素索引 0..bw-1 的最后那个）：这样 100% 能落在框内
    # 最右/最下一列，而不是刚好越过边界一像素。百分比与随机都用它，两种模式
    # 的取值区间才一致。
    span_x, span_y = bw - 1, bh - 1

    if mode == CLICK_POS_RANDOM:
        f = rand or random.random
        return (int(round(left + f() * span_x)),
                int(round(top + f() * span_y)))
    # 框内自定义：0 = 左/上边，100 = 右/下边。越界的百分比钳到 [0, 100]。
    px = min(max(float(rel_x), 0.0), 100.0) / 100.0
    py = min(max(float(rel_y), 0.0), 100.0) / 100.0
    return (int(round(left + px * span_x)),
            int(round(top + py * span_y)))


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


def _int_or_0(v) -> int:
    """宽松取整：参数可能来自旧文件（字符串数字），也可能被手工改坏。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


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
            ParamDef("action", "动作", "select", "click",
                     ["click", "double_click", "move", "press", "release", "scroll", "drag"]),
            ParamDef("button", "按键", "select", "left", ["left", "right", "middle"]),
            ParamDef("anchor", "坐标基准", "select", "absolute",
                     ["absolute", "base", "win_tl", "win_tr", "win_bl", "win_br"],
                     tooltip="absolute: 屏幕绝对坐标 | base: 以基点为原点 | "
                             "win_tl/win_tr/win_bl/win_br: 以最前面那个窗口（游戏）的左上/右上/左下/右下角为原点"),
            ParamDef("x", "X", "int", 0),
            ParamDef("y", "Y", "int", 0),
            ParamDef("use_relative", "相对偏移", "bool", False, tooltip="以工作流基点为原点计算偏移（旧字段，等价于坐标基准=base）"),
            ParamDef("scroll_dy", "滚动格数(Y)", "int", 0,
                     tooltip="动作=scroll 时使用：正数向下滚，负数向上滚（一格 ≈ 3 行）"),
            ParamDef("scroll_dx", "滚动格数(X)", "int", 0, tooltip="动作=scroll 时使用：横向滚动，一般留 0"),
            ParamDef("scroll_unit", "滚动单位", "select", "line", ["line", "pixel"],
                     tooltip="line=按行（一格≈1 行，力度小）| pixel=按像素（游戏里往往只认这个）"),
            ParamDef("scroll_target", "滚轮投递", "select", "全局", ["全局", "前台应用"],
                     tooltip="全局=系统级投递；前台应用=直接投递给游戏进程。"
                             "游戏把合成滚轮过滤掉时（滚了没反应），换成「前台应用」"),
            ParamDef("drag_dx", "拖拽位移 X", "int", 0,
                     tooltip="动作=drag 时使用：从 (X,Y) 按住往这边拖。负数=往左拖"),
            ParamDef("drag_dy", "拖拽位移 Y", "int", -250,
                     tooltip="动作=drag 时使用：负数=往上拖（内容向下滚，看下面的东西）"),
            ParamDef("drag_steps", "拖拽步数", "int", 12, min_value=2, max_value=120,
                     tooltip="分成几步移动。必须分步——一步跳到终点，游戏只看到光标瞬移，不认拖拽"),
            ParamDef("drag_ms", "拖拽耗时(ms)", "int", 400, min_value=50, max_value=5000,
                     tooltip="整段拖拽的时长，太快游戏可能不认"),
        ]

    def run(self, ctx) -> None:
        p = ctx.params
        anchor = str(p.get("anchor") or ("base" if p.get("use_relative") else "absolute"))
        win = None
        if anchor in ("win_tl", "win_tr", "win_bl", "win_br"):
            from core import selfmask
            win = selfmask.front_window_rect()
            if win is None:
                from core.vision import _warn_once
                _warn_once("no-front-window",
                           "读不到前台窗口（游戏窗口）的位置，已跳过这次点击——"
                           "请确认游戏窗口没被完全盖住、且屏幕录制权限正常。")
                return
        pt = resolve_anchor_point(anchor, int(p.get("x", 0)), int(p.get("y", 0)),
                                  ctx.base_x, ctx.base_y, win)
        if pt is None:
            from core.vision import _warn_once
            _warn_once("anchor-outside-window",
                       "按窗口几何算出来的点击点落在窗口外面，已跳过这次点击"
                       "（多半是前台窗口不是游戏，或窗口刚在切场景）。")
            return
        dx, dy = pt
        if anchor != "absolute":
            logger.info("鼠标：基准 %s + (%d,%d) → 实际点 (%d,%d)%s",
                        anchor, int(p.get("x", 0)), int(p.get("y", 0)), dx, dy,
                        ("（前台窗口 %s）" % (win,)) if win else "")
        m = ctx.player.mouse
        if p.get("action") == "drag":
            # 滚轮在不少游戏里被直接忽略（真机上试过 4 档力度/方向都没反应），
            # 拖拽列表才是通用解法：按住 → 分步移动 → 松开。
            # **必须分步**：一步跳到终点，游戏只看到光标瞬移，不认这是一次拖拽。
            import time as _time
            steps = max(2, int(p.get("drag_steps", 12) or 12))
            total_ms = max(50, int(p.get("drag_ms", 400) or 400))
            ddx = int(p.get("drag_dx", 0) or 0)
            ddy = int(p.get("drag_dy", 0) or 0)
            ctx.player.glide_now((dx, dy))
            ctx.player.mouse.press("left")
            try:
                for i in range(1, steps + 1):
                    if ctx.player.stopping:
                        break
                    ratio = i / float(steps)
                    ctx.player.mouse.move_to(
                        (dx + int(round(ddx * ratio)), dy + int(round(ddy * ratio))), "left")
                    _time.sleep(total_ms / 1000.0 / steps)
            finally:
                ctx.player.mouse.release("left")
            return
        if p.get("action") == "scroll":
            # 滚轮事件落在**光标所在的那个窗口**上，所以先把光标挪到目标位置再滚，
            # 否则滚的是别的地方（游戏里最常见的表现就是「滚了没反应」）。
            ctx.player.glide_now((dx, dy))
            # 滚轮在 mouse 上（Player 自己没有 scroll 方法）——真机报过
            # "'Player' object has no attribute 'scroll'"。
            pid = 0
            if str(p.get("scroll_target", "全局") or "全局") == "前台应用":
                from core import selfmask as _sm
                pid = _sm.front_window_pid() or 0
            ctx.player.mouse.scroll(int(p.get("scroll_dx", 0)), int(p.get("scroll_dy", 0)),
                                    str(p.get("scroll_unit", "line") or "line"), pid)
            return
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

    # 等待至时刻的分段长度（秒）。每段结束都用**墙钟**重算剩余，而不是一次
    # 睡到底：`player.wait` 走 monotonic 时钟，macOS 上系统睡眠期间它不走——
    # 等到明早 09:00 的节点在机器睡眠后醒来会**多等一整段睡眠时间**。
    # 分段重算让醒来后的下一段直接对准墙钟；顺带让停止检查保持高频响应。
    CHUNK_S = 1.0

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("mode", "等待方式", "select", "延时毫秒", ["延时毫秒", "等待至时刻"],
                     tooltip="延时毫秒：固定时长 | 等待至时刻：等到当天的某个时间点，已过则等明天的"),
            # ptype="duration"：面板上是「时/分/秒/毫秒」四格，**存下来仍是毫秒总数**。
            # 这样已保存的流程（params.ms = 500）不用迁移，run() 也照旧读一个数。
            # 注意两个选项的字面量（"延时毫秒"）**不能改名**：它就是存进文件的 value，
            # 改了老文件的 mode 对不上，select 显示为空、show_if 也会把字段藏掉。
            ParamDef("ms", "等待时长", "duration", 500, min_value=0,
                     show_if={"key": "mode", "eq": "延时毫秒"},
                     tooltip="按时 / 分 / 秒 / 毫秒分别填；等同于一个毫秒总数"),
            # ptype="time"：时刻选择器（只能选，不能手打），值仍是 "HH:MM:SS" 字符串。
            ParamDef("at", "时刻", "time", "09:00:00",
                     show_if={"key": "mode", "eq": "等待至时刻"},
                     tooltip="24 小时制；已过则等明天的这一刻"),
        ]

    def run(self, ctx) -> None:
        if str(ctx.params.get("mode", "延时毫秒")) == "等待至时刻":
            self._wait_until(ctx, str(ctx.params.get("at", "") or ""))
        else:
            ctx.player.wait(float(ctx.params.get("ms", 500)) / 1000.0 / max(ctx.speed, 0.01))

    def _wait_until(self, ctx, at: str) -> None:
        """等到墙钟时刻。注意**不受速度倍率缩放**——「等到 09:00」是日历语义，
        把它除以 speed 就成了"等到假的时刻"。"""
        while not ctx.stopping:
            remain = self._seconds_until(at)
            if remain <= 0:
                return
            ctx.player.wait(min(remain, self.CHUNK_S))
            # 兜底：player 被单独停掉时 `wait` 会立刻返回，而此时 ctx.stopping 可能
            # 还是 False。正常路径下两者同置（executor.stop_run 都设），但缺了这道
            # 检查，异常路径就会**每秒空转**直到墙钟自然走完（几小时 100% CPU）。
            if ctx.player.stopping:
                return

    @staticmethod
    def _seconds_until(at: str, now=None) -> float:
        """距 `at`（HH:MM 或 HH:MM:SS）的秒数；已过则算**明天**的同一时刻。

        `now` 可注入以便测试。格式/范围错误抛 ValueError（进 run.error，
        用户能在面板上看到原因，而不是静默等 0 秒直接放行）。
        """
        import datetime

        parts = [s for s in str(at).strip().split(":") if s != ""]
        if len(parts) not in (2, 3):
            raise ValueError("时刻格式应为 HH:MM 或 HH:MM:SS，例如 09:30")
        try:
            h, mi = int(parts[0]), int(parts[1])
            sec = int(parts[2]) if len(parts) == 3 else 0
        except ValueError:
            raise ValueError("时刻格式应为 HH:MM 或 HH:MM:SS，例如 09:30") from None
        if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= sec <= 59):
            raise ValueError("时刻超出范围（时 0-23，分/秒 0-59）")
        now = now or datetime.datetime.now()
        target = now.replace(hour=h, minute=mi, second=sec, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return (target - now).total_seconds()


@register
class RecordReplayTask(BaseTask):
    type = "record_replay"
    name = "录制回放"
    order = 40

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            # 事件**存在哪里**。内嵌 = 跟着流程文件走（拷给别人也能跑）；
            # 录制库 = 节点只存一个 id，回放时现取——「在库里改一次，
            # 所有引用它的节点一起变」全靠这条路径。
            ParamDef("source", "录制来源", "select", "内嵌", ["内嵌", "录制库"],
                     tooltip="内嵌：事件存在节点里，跟流程文件一起走 | "
                             "录制库：只存 id，回放时现取，库里改了这里跟着变"),
            ParamDef("recording_id", "选择录制", "recording", "",
                     show_if={"key": "source", "eq": "录制库"},
                     tooltip="来自侧栏「录制库」；同一条录制可以被多个节点共用"),
            ParamDef("events", "事件序列", "events", [],
                     show_if={"key": "source", "eq": "内嵌"}),
            # 原点只在「内嵌」时可填：用库里的录制时，原点是**那条录制自己的属性**
            # （它是在哪块屏、哪个窗口位置录的）。节点再存一份就成了第二份真源，
            # 在库里重设原点后节点不跟着变，回放位置会偏。
            ParamDef("origin_x", "原点X", "int", 0,
                     show_if={"key": "source", "eq": "内嵌"}),
            ParamDef("origin_y", "原点Y", "int", 0,
                     show_if={"key": "source", "eq": "内嵌"}),
            ParamDef("use_relative", "相对坐标", "bool", False),
            ParamDef("speed", "速度倍率", "float", 1.0, min_value=0.1),
            ParamDef("repeat", "重复次数", "int", 1, min_value=1),
        ]

    def run(self, ctx) -> None:
        # 「重复次数」不在此处循环：执行器已按 ctx.params["repeat"] 重复调用本任务，
        # 两层循环会把 N 次放大成 N² 次（再乘工作流整体循环）。
        p = ctx.params
        if str(p.get("source") or "内嵌") == "录制库":
            origin_x, origin_y, raw = self._from_library(str(p.get("recording_id") or ""))
        else:
            origin_x = _int_or_0(p.get("origin_x"))
            origin_y = _int_or_0(p.get("origin_y"))
            raw = p.get("events") or []
        events = [e for e in (tolerant_event(x) for x in raw) if e]
        opt = PlayOptions(
            speed=float(p.get("speed", 1.0)) * ctx.speed,
            use_relative=bool(p.get("use_relative", False)),
            base_x=ctx.base_x, base_y=ctx.base_y,
            origin_x=origin_x, origin_y=origin_y,
            suppress_keys=set(HOTKEY_NAMES),
        )
        ctx.player.play(events, opt, on_progress=ctx.on_progress)

    @staticmethod
    def _from_library(rid: str):
        """按 id 取库里的录制，返回 `(origin_x, origin_y, events)`。

        取不到**必须抛**：静默跳过会让「这条录制被删了」表现成「节点跑过了、
        但什么都没发生」——用户只会以为回放坏了，而不知道该去库里补一条。
        """
        if not rid:
            raise ValueError("录制来源选了「录制库」，但没选具体是哪一条")
        from core import recording_lib
        try:
            data = recording_lib.load(rid)
        except ValueError as e:
            raise ValueError(str(e)) from None
        origin = data.get("origin") or [0, 0]
        seq = origin if isinstance(origin, (list, tuple)) else []
        ox = _int_or_0(seq[0]) if len(seq) > 0 else 0
        oy = _int_or_0(seq[1]) if len(seq) > 1 else 0
        return ox, oy, data["events"]


def resolve_anchor_point(anchor: str, x: int, y: int,
                          base_x: int = 0, base_y: int = 0,
                          win: tuple | None = None) -> tuple | None:
    """「坐标基准」+ 偏移 → 实际点击点；**算出来的点落在窗口外就返回 None**。

    `win` 是最前面那个窗口（游戏窗口）的 `(left, top, w, h)`；`win_tl/tr/bl/br` 用它
    对应的角当原点。

    为什么需要这个：游戏里有些按钮是**图标**（没有文字，OCR 读不到），背后又是随时
    变化的游戏画面（模板匹配不稳）。这种位置只能按窗口几何算，窗口挪了也跟着走。

    返回 None 的那条：真机事故——结算动画期间最前面冒出一个贴在 (0,0) 的小窗口，
    按它算「右下角」得到负坐标，点击被钳到 (0,0)，**把屏幕左上角的苹果菜单点开了**。
    所以窗口太小不认（见 selfmask._MIN_FRONT_W），并且这里再挡一层：
    算出来的点只要不在窗口矩形里，就不点。
    """
    if win is not None and anchor in ("win_tl", "win_tr", "win_bl", "win_br"):
        left, top, w, h = int(win[0]), int(win[1]), int(win[2]), int(win[3])
        if anchor == "win_tr":
            px, py = left + w + x, top + y
        elif anchor == "win_bl":
            px, py = left + x, top + h + y
        elif anchor == "win_br":
            px, py = left + w + x, top + h + y
        else:
            px, py = left + x, top + y
        if not (left <= px <= left + w and top <= py <= top + h):
            return None
        return (px, py)
    if anchor == "base":
        return (int(base_x) + x, int(base_y) + y)
    return (x, y)


def _self_window_guard(x: int, y: int) -> bool:
    """点下去之前，确认落点不在 Auto Flow 自己的窗口上。

    识别是全屏做的，理论上 `core.selfmask` 已经把自身窗口涂黑、不该命中；
    但窗口列表读不到（权限/异常）时遮罩会静默失效，那时命中的就可能落在自己
    面板上——而运行期间主窗是**鼠标穿透**的，点上去会穿到窗口下方的桌面或
    别的程序（用户实测：「操作穿透了游戏，点到了后面的程序」）。

    所以这里是第二道保险：真撞上就跳过这一次点击并留一条告警，宁可少点一下，
    也不要在用户的桌面上乱点。
    """
    try:
        from core import selfmask
        if selfmask.point_in_own_window(int(x), int(y)):
            from core.vision import _warn_once
            _warn_once("self-click",
                       "命中点在 Auto Flow 自己的窗口上（(%d,%d)），已跳过这次点击——"
                       "请把 Auto Flow 窗口移开或收进托盘，别盖住游戏。", x, y)
            return False
    except Exception:  # noqa: BLE001  护栏本身不能把流程搞挂
        pass
    return True


# 「一句话在屏幕上出现多次」时选哪个命中。默认第一个（阅读顺序），
# 随机用于「列表里随便挑一个」这种场景，避免每次都点同一个。
HIT_PICK_KINDS = ("第一个", "最后一个", "随机")


def region_from_window(win, left_pct: int = 0, right_pct: int = 100,
                       top_pct: int = 0, bottom_pct: int = 100):
    """窗口矩形 + 四个百分比 → 屏幕矩形 (x, y, w, h)，给 OCR 只识别这一块用。

    为什么要裁：全屏识别一次约 0.4 秒，而抢悬赏这种场景「列表出现到点下去」总共只有
    一两秒的窗口期。裁到卡片区域（约占屏幕 1/3）能把单次识别压到一百多毫秒。
    读不到窗口时返回 None（不裁）。
    """
    if not win:
        return None
    left, top, w, h = win
    l = max(0, min(100, int(left_pct)))
    r = max(0, min(100, int(right_pct)))
    t = max(0, min(100, int(top_pct)))
    bt = max(0, min(100, int(bottom_pct)))
    if l > r:
        l, r = r, l
    if t > bt:
        t, bt = bt, t
    x1 = left + int(w * l / 100.0)
    x2 = left + int(w * r / 100.0)
    y1 = top + int(h * t / 100.0)
    y2 = top + int(h * bt / 100.0)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return (x1, y1, x2 - x1, y2 - y1)


def expand_pct(l: int, r: int, t: int, bt: int, margin: int = 3):
    """把裁剪范围往外扩几格。

    裁剪是为了提速和排干扰，但**边界贴着目标会把目标文字切掉**——真机上第一张卡片的字从
    窗口 7.3% 开始，裁到 8% 就把首字「解」切了，识别结果不再包含「解决诬陷」，
    表现是「列表里只有一张卡时永远抢不到」，而且不报任何错。
    所以：**裁剪范围 = 判断范围 ± margin**，切图时宽松一点，命中后再按精确范围过滤。
    """
    return (max(0, l - margin), min(100, r + margin),
            max(0, t - margin), min(100, bt + margin))


def filter_hits(hits: list, win, left_pct: int = 0, right_pct: int = 100,
                top_pct: int = 0, bottom_pct: int = 100) -> list:
    """按「窗口宽度的百分比区间」筛命中，只保留横向落在区间内的。

    真机场景：悬赏卡片在左、详情面板在右，两边写着同一句话「解决诬陷…」。
    把搜索范围收到左边 70%，就只会命中卡片，不会点到右侧那个不可点的标签。
    读不到前台窗口时**不过滤**（宁可点多，也不要因为拿不到窗口就完全不点）。
    """
    if not hits or not win:
        return list(hits)
    left, top, w, h_ = win
    lo = left + w * max(0, min(100, left_pct)) / 100.0
    hi = left + w * max(0, min(100, right_pct)) / 100.0
    if lo > hi:
        lo, hi = hi, lo
    tlo = top + h_ * max(0, min(100, top_pct)) / 100.0
    thi = top + h_ * max(0, min(100, bottom_pct)) / 100.0
    if tlo > thi:
        tlo, thi = thi, tlo
    return [h for h in hits if lo <= h.x <= hi and tlo <= h.y <= thi]


def pick_hit(hits: list, mode: str = "第一个"):
    if not hits:
        return None
    if mode == "最后一个":
        return hits[-1]
    if mode == "随机":
        import random
        return random.choice(hits)
    return hits[0]


def _click_target(p: dict, cx: int, cy: int, w: int, h: int) -> tuple:
    """节点参数 → 最终点击点：先在目标框内取点，再叠加固定偏移。

    两个点击节点（`image_click` / `ocr_click`）共用一份——「框内取点」的语义必须
    完全一致，否则同一个目标在「找图」与「找文字」上会点到不同地方，而用户完全
    无从判断该信哪个（与 `_detect` 被三个检测节点共用的理由相同）。

    偏移**最后**叠加：它是相对目标点的微调。以前没有框内取点，偏移就是直接加在
    中心上的，所以这样叠加对老流程是逐字等价的。
    """
    def num(key: str, default: float) -> float:
        # 参数可能来自旧文件（字符串数字），也可能被手工改坏；`0` 是合法值，
        # 所以不能用 `p.get(k) or default` —— 那会把 0 当成「没填」。
        try:
            return float(p.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    x, y = click_point_in_box(cx, cy, w, h,
                              mode=str(p.get("click_pos") or CLICK_POS_CENTER),
                              rel_x=num("rel_x", 50), rel_y=num("rel_y", 50),
                              inset=int(num("inset", 0)))
    return x + int(num("offset_x", 0)), y + int(num("offset_y", 0))


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
            ParamDef("click_pos", "点击位置", "select", CLICK_POS_CENTER,
                     list(CLICK_POS_KINDS), tooltip=CLICK_POS_TOOLTIP),
            ParamDef("rel_x", "横向位置(%)", "int", 50, min_value=0, max_value=100,
                     show_if={"key": "click_pos", "eq": CLICK_POS_CUSTOM},
                     tooltip="0=框左边缘，100=框右边缘"),
            ParamDef("rel_y", "纵向位置(%)", "int", 50, min_value=0, max_value=100,
                     show_if={"key": "click_pos", "eq": CLICK_POS_CUSTOM},
                     tooltip="0=框上边缘，100=框下边缘"),
            ParamDef("inset", "内缩像素", "int", 0, min_value=0,
                     tooltip="把目标框四边各往里收这么多再取点，避免点到边缘"),
            ParamDef("offset_x", "偏移X", "int", 0, tooltip="在算出的点之上再平移"),
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
                x, y = _click_target(p, m.x, m.y, m.w, m.h)
                if not _self_window_guard(x, y):
                    return
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
            ParamDef("click_pos", "点击位置", "select", CLICK_POS_CENTER,
                     list(CLICK_POS_KINDS), tooltip=CLICK_POS_TOOLTIP),
            ParamDef("rel_x", "横向位置(%)", "int", 50, min_value=0, max_value=100,
                     show_if={"key": "click_pos", "eq": CLICK_POS_CUSTOM},
                     tooltip="0=文字框左边缘，100=右边缘"),
            ParamDef("rel_y", "纵向位置(%)", "int", 50, min_value=0, max_value=100,
                     show_if={"key": "click_pos", "eq": CLICK_POS_CUSTOM},
                     tooltip="0=文字框上边缘，100=下边缘"),
            ParamDef("inset", "内缩像素", "int", 0, min_value=0,
                     tooltip="把文字框四边各往里收这么多再取点，避免点到边缘"),
            ParamDef("offset_x", "偏移X", "int", 0, tooltip="在算出的点之上再平移"),
            ParamDef("offset_y", "偏移Y", "int", 0),
            ParamDef("search_left_pct", "搜索左边界(%)", "int", 0, min_value=0, max_value=100,
                     tooltip="只在窗口横向的这个百分比之后找（默认 0=最左边）"),
            ParamDef("search_right_pct", "搜索右边界(%)", "int", 100, min_value=0, max_value=100,
                     tooltip="只在窗口横向的这个百分比之前找。同一句话左右各有一处时，用这个把右边那个排掉"),
            ParamDef("pick", "多个命中时", "select", "第一个", list(HIT_PICK_KINDS),
                     tooltip="同一句话在屏幕上出现多次时选哪个"),
            ParamDef("search_top_pct", "搜索上边界(%)", "int", 0, min_value=0, max_value=100,
                     tooltip="只在窗口纵向的这个百分比之后找。配合上下边界把识别区域裁小，识别更快"),
            ParamDef("search_bottom_pct", "搜索下边界(%)", "int", 100, min_value=0, max_value=100,
                     tooltip="只在窗口纵向的这个百分比之前找"),
            # --- 点完校验 ---
            # 为什么要内置：点击「没生效」是这类脚本最常见的静默失败——点歪了、
            # 点到别的格子上、或者界面还没渲染完。以前每处都要手接一个条件节点去查，
            # 忘了接就会带着错误状态往下走，最后表现为「流程跑完了但什么都没做」。
            ParamDef("verify_text", "点完校验文字", "text", "",
                     tooltip="点完之后等这段文字出现；留空=不校验"),
            ParamDef("verify_timeout_s", "校验超时(秒)", "float", 1.5, min_value=0.0),
            ParamDef("verify_retries", "校验失败重试次数", "int", 1, min_value=0,
                     tooltip="没等到就重新找目标再点一次（0=不重试）"),
            ParamDef("verify_fail", "重试也没成功", "select", "继续走下一步",
                     ["继续走下一步", "停止工作流"]),
        ]

    @staticmethod
    def _wait_verify(ctx, text: str, timeout_s: float) -> bool:
        """等校验文字出现（不看搜索范围：校验的是「点了有没有反应」）。"""
        import time as _time
        from core import ocr
        if not text:
            return True
        deadline = _time.monotonic() + max(0.0, timeout_s)
        while True:
            if ctx.player.stopping:
                return True
            if ocr.find_text(text):
                return True
            if _time.monotonic() >= deadline:
                return False
            ctx.player.wait(0.15)

    def run(self, ctx) -> None:
        import time as _time
        from core import ocr, selfmask
        p = ctx.params
        deadline = _time.monotonic() + float(p.get("timeout_s", 3.0))
        verify = str(p.get("verify_text", "") or "")
        retries_left = max(0, int(p.get("verify_retries", 1) or 0))
        while True:
            if ctx.player.stopping:
                return
            _win = selfmask.front_window_rect()
            _l = int(p.get("search_left_pct", 0) or 0)
            _r = int(p.get("search_right_pct", 100) if p.get("search_right_pct") is not None else 100)
            _t = int(p.get("search_top_pct", 0) or 0)
            _bt = int(p.get("search_bottom_pct", 100) if p.get("search_bottom_pct") is not None else 100)
            _el, _er, _et, _eb = expand_pct(_l, _r, _t, _bt)
            _region = region_from_window(_win, _el, _er, _et, _eb)
            hits = ocr.find_texts(str(p.get("text", "")), region=_region)
            if hits:
                logger.info("找文字点击：「%s」范围内命中 %d 处 → %s", p.get("text", ""), len(hits),
                            [(h.x, h.y, h.text[:10]) for h in hits])
            hits = filter_hits(hits, _win, _l, _r, _t, _bt)
            hit = pick_hit(hits, str(p.get("pick", "第一个") or "第一个"))
            if hit:
                x, y = _click_target(p, hit.x, hit.y, hit.w, hit.h)
                # 点偏过一次（想点「悬赏」结果点进「伙伴」），所以把命中数和落点都记下来：
                # 面板滚动有惯性，读到的位置和真正点下去的时刻可能差了几百毫秒。
                logger.info("找文字点击：「%s」命中 %d 处，选 (%d,%d) 框 %dx%d → 点 (%d,%d)",
                            p.get("text", ""), len(hits), hit.x, hit.y, hit.w, hit.h, x, y)
                if not _self_window_guard(x, y):
                    return
                ctx.player.glide_now((x, y))
                action = p.get("action", "click")
                if action == "move":
                    return
                ctx.player.mouse.click("left", 2 if action == "double_click" else 1)
                if not verify:
                    return
                if self._wait_verify(ctx, verify, float(p.get("verify_timeout_s", 1.5))):
                    return
                if retries_left > 0:
                    retries_left -= 1
                    logger.warning("点完没等到「%s」，重新找目标再点一次（还剩 %d 次）",
                                   verify, retries_left)
                    continue
                logger.warning("点「%s」后始终没等到「%s」，判定这次点击没生效",
                               p.get("text", ""), verify)
                if p.get("verify_fail", "继续走下一步") == "停止工作流":
                    ctx.stop_workflow()
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
            model_path: str = "", left_pct: int = 0, right_pct: int = 100,
            top_pct: int = 0, bottom_pct: int = 100) -> bool:
    """执行一次「图像存在 / 文字存在 / 目标存在(YOLO)」检测。

    `condition`、`condition_group` 与 `branch` **共用这一份实现**——三者对同一种
    检测方式的语义必须完全一致，否则「条件成立」「条件组成立」和「分支命中」会在
    同一张屏上给出不同答案，而用户完全无从判断该信哪个。`value` 的含义随 `kind` 变：
    图像存在=模板图路径；文字存在=要找的文字；YOLO=目标类别（留空=任意）。
    """
    from core import vision, ocr, yolo
    if kind == "文字存在":
        # 带范围：同一句话在屏幕上可能出现在多处（真机上「悬赏」在菜单面板里，
        # 也在左侧任务栏里，结果面板还没滚到底就判成「已经能看见悬赏」，随后点到了别的格子）
        from core import selfmask as _sm
        win = _sm.front_window_rect()
        el, er, et, eb = expand_pct(int(left_pct), int(right_pct), int(top_pct), int(bottom_pct))
        region = region_from_window(win, el, er, et, eb)
        hits = ocr.find_texts(str(value), region=region)
        kept = filter_hits(hits, win, int(left_pct), int(right_pct), int(top_pct), int(bottom_pct))
        # 判据不成立时最容易「看着明明在屏幕上」——把命中位置和窗口都打出来，
        # 下次直接看是没识别到、还是被搜索范围滤掉了。
        if hits and not kept:
            logger.info("文字检测：「%s」命中 %d 处但都被范围(%d%%~%d%%)滤掉：%s | 窗口 %s",
                        value, len(hits), int(left_pct), int(right_pct),
                        [(h.x, h.y) for h in hits], win)
        elif not hits:
            logger.info("文字检测：「%s」整屏没找到 | 窗口 %s", value, win)
        return bool(kept)
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
            ParamDef("search_left_pct", "搜索左边界(%)", "int", 0, min_value=0, max_value=100,
                     tooltip="文字检测只在窗口横向的这个百分比之后找。"
                             "同一句话屏幕上有多处时（比如任务栏里也有），用它把范围收到面板那一侧"),
            ParamDef("search_right_pct", "搜索右边界(%)", "int", 100, min_value=0, max_value=100,
                     tooltip="文字检测只在窗口横向的这个百分比之前找"),
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
                           model_path=str(p.get("model_path") or ""),
                           left_pct=int(p.get("search_left_pct", 0) or 0),
                           right_pct=int(p.get("search_right_pct", 100) if p.get("search_right_pct") is not None else 100))

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
class ConditionGroupTask(BaseTask):
    """条件组：把多个检测按「与 / 或」合成一个判断，成立走 `true`、不成立走 `false`。

    为什么要有它：`condition` 一次只能测一个东西。想表达「A 在**且** B 在」，
    只能串两个条件节点——而条件的出口是二选一的，串起来要么把中间那个 `false`
    再接一个条件（画布迅速变成一团线），要么接受「第一个不成立时没有统一失败
    出口」。一个节点里放 N 项、由 `combine` 决定怎么合，画布上就还是「一个判断、
    两个出口」，路径数不随条件数膨胀。

    求值**必须短路**：一次检测 = 截屏 + 模板匹配/OCR/推理，是本节点里最贵的操作。
    「与」碰到第一个不成立就没有再测的必要（结果已定），「或」碰到第一个成立同理。
    用户把哪项排前面因此获得了「优先被检测」的含义——不短路的话顺序完全不影响
    结果，用户就没法用它表达优先级（与 `branch` 的「先到先得」是同一个道理）。

    刻意**没有**「至少 N 项成立」这类参数：那属于多路分支的范畴（同一份检测结果
    要分出三种以上走法）。两个节点各管一件事，比在一个节点里堆参数好理解。
    """
    type = "condition_group"
    name = "条件组"
    order = 82

    # 「组合方式」的两个取值。抽成常量是为了让 run() 与参数定义引用同一份字面量——
    # 两处各写一遍，改了参数面板上的选项却忘了改判断，表现是「选了『与』却按『或』
    # 算」，没有任何报错。
    COMBINE_AND = "全部成立(与)"
    COMBINE_OR = "任一成立(或)"
    COMBINE_KINDS = (COMBINE_AND, COMBINE_OR)

    def __init__(self) -> None:
        super().__init__()
        self.params = [
            ParamDef("cond_count", "条件数量", "int", 2,
                     min_value=2, max_value=MAX_GROUP_CONDITIONS,
                     tooltip=f"最多 {MAX_GROUP_CONDITIONS} 项"),
            ParamDef("combine", "组合方式", "select", self.COMBINE_AND,
                     list(self.COMBINE_KINDS),
                     tooltip="「与」= 全部成立才算成立；「或」= 任一成立即算成立"),
            ParamDef("search_left_pct", "搜索左边界(%)", "int", 0, min_value=0, max_value=100,
                     tooltip="文字检测只在窗口横向的这个百分比之后找"),
            ParamDef("search_right_pct", "搜索右边界(%)", "int", 100, min_value=0, max_value=100,
                     tooltip="文字检测只在窗口横向的这个百分比之前找"),
        ]
        for i in range(1, MAX_GROUP_CONDITIONS + 1):
            show = {"key": "cond_count", "gte": i}
            self.params.append(ParamDef(
                f"cond{i}_kind", f"条件 {i} 检测方式", "select", "图像存在",
                list(CHECK_KINDS), show_if=show))
            self.params.append(ParamDef(
                f"cond{i}_value", f"条件 {i} 取值", "text", "",
                show_if=show, pick=True, tooltip=CASE_VALUE_TOOLTIP))
        self.params.append(ParamDef("confidence", "置信度", "float", 0.8, min_value=0.05))
        self.params.append(ParamDef("timeout_s", "等待超时(秒)", "float", 0.0, min_value=0.0,
                                    tooltip=">0 时在超时时间内反复求值，组合结果成立即走 true"))

    def run(self, ctx) -> None:
        import time as _time
        p = ctx.params
        n = max(2, min(int(p.get("cond_count", 2) or 2), MAX_GROUP_CONDITIONS))
        conf = float(p.get("confidence", 0.8))
        # 缺字段/脏值一律回落到默认的「与」：旧文件里没有 combine 键时必须和
        # 面板上的默认值表现一致，否则「我什么都没改，行为却不一样」。
        combine = str(p.get("combine") or self.COMBINE_AND)
        is_and = combine != self.COMBINE_OR
        deadline = _time.monotonic() + float(p.get("timeout_s", 0.0))

        left_pct = int(p.get("search_left_pct", 0) or 0)
        right_pct = int(p.get("search_right_pct", 100) if p.get("search_right_pct") is not None else 100)

        def evaluate() -> bool:
            for i in range(1, n + 1):
                hit = _detect(str(p.get(f"cond{i}_kind", "图像存在")),
                              str(p.get(f"cond{i}_value", "")), confidence=conf,
                              left_pct=left_pct, right_pct=right_pct)
                if is_and:
                    if not hit:
                        return False   # 与：一项不成立，整体已定，后面的不用测
                elif hit:
                    return True        # 或：一项成立，整体已定，后面的不用测
            # 循环走完说明没有触发短路：与 = 全部成立；或 = 全部不成立
            return is_and

        while True:
            if ctx.player.stopping:
                ctx.set_port(PORT_FALSE)
                return
            if evaluate():
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
