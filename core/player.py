"""回放引擎 v3：虚拟时钟 + 时间采样插值 + 三档追赶。

v2 的三个结构性缺陷（"回放丢帧严重"的直接成因）：

1. **每步强制 ≥8ms**（`MIN_STEP_S`）。滑行循环哪怕预算已经耗尽也至少 sleep 8ms，
   而录制侧快速拖拽的移动事件率轻松超过 125/s——播放器从结构上就追不上，
   落后的量只增不减。
2. **落后即雪崩**。预算算成 `ts/1000/speed - 已耗时`，一旦为负，
   `max(budget, 0)` 让剩余事件全部瞬时连发，整条时间线塌成一坨。
3. **按像素步长插值**（10px/步、上限 120 步）。快速长距离移动被压成十几个
   上百像素的跳变，视觉上就是丢帧；慢速移动又因为点太少而不平滑。

v3 的做法：

- **虚拟时钟**：`due = t0 + ts_ms / speed`，每个事件的计划时刻只由时间线决定，
  误差不累积。
- **时间采样插值**：给定剩余时间与位移，按 ~6ms 一个采样点铺开（点数由**时间**
  决定而非像素），既保证慢速平滑，也保证快速不跳变。
- **三档追赶**：`剩余 > 4ms` 正常插值；`0 < 剩余 ≤ 4ms` 压缩为一次投递；
  `剩余 ≤ 0`（已落后）直接跳帧投递目标位置并计数。**任何情况下单事件的开销
  上限是一次投递**，落后会被自然追平，绝不出现"剩余事件瞬时连发"。
- **拖拽回放**：按键保持期间投递 `kCGEvent*MouseDragged` 而非 `MouseMoved`。
- **点击序列**：写入 `kCGMouseEventClickState`，双击/三击才能被目标应用识别。
- **滚轮单位**：按 `wheel_unit` 选择 line/pixel，且横纵两轴一起创建
  （v2 只用 1 个轴创建事件再设第 2 轴字段，横向滚轮被丢弃；且把"行"当"像素"
  投递，量级差一个数量级，表现为滚轮几乎不动）。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Optional

from core.events import MacroEvent
from core.keymap import name_to_key
from core.mactype import MacKeyboardController as KeyboardController

import Quartz as _Q

logger = logging.getLogger("autoflow.player")

# ---- 输出目标 ----
# HID 层最接近真实硬件，兼容性优于 session 层（v2 用 session，部分应用收不到）。
_POST_TAP = _Q.kCGHIDEventTap

# ---- 插值/追赶参数 ----
SAMPLE_S = 0.006        # 采样间隔 ≈ 6ms（约 160Hz，够平滑且开销可控）
MIN_SAMPLE_S = 0.004    # 剩余时间低于此值：不再插值，直接投递目标点
TRAVEL_MAX_S = 0.25     # 单次移动的最长滑行时间（更长的空档先等待，不匀速爬行）
MAX_STEPS = 400

SCROLL_UNIT = {"line": _Q.kCGScrollEventUnitLine,
               "pixel": _Q.kCGScrollEventUnitPixel}

_BUTTON_CODE = {"left": _Q.kCGMouseButtonLeft, "right": _Q.kCGMouseButtonRight,
                "middle": _Q.kCGMouseButtonCenter}
_DOWN = {"left": _Q.kCGEventLeftMouseDown, "right": _Q.kCGEventRightMouseDown,
         "middle": _Q.kCGEventOtherMouseDown}
_UP = {"left": _Q.kCGEventLeftMouseUp, "right": _Q.kCGEventRightMouseUp,
       "middle": _Q.kCGEventOtherMouseUp}
_DRAGGED = {"left": _Q.kCGEventLeftMouseDragged,
            "right": _Q.kCGEventRightMouseDragged,
            "middle": _Q.kCGEventOtherMouseDragged}


class QuartzMouse:
    """Quartz 直发的鼠标输出（post 到 HID 层）。

    为什么不用 pynput 的 Controller：PyInstaller frozen sidecar 里
    pynput 鼠标 press/release 会【静默失效】（不抛异常、事件不出现），
    表现为「回放移动正常但点击无效果」。

    相对 pynput Controller 额外补上它内部才有的两件事：按键保持期间的
    拖拽事件类型切换、点击序列号（ClickState）写入。
    """

    def __init__(self) -> None:
        self._pos = _current_pos()

    @property
    def position(self) -> tuple:
        return self._pos

    @position.setter
    def position(self, xy) -> None:
        self.move_to(xy)

    def move_to(self, xy, button: Optional[str] = None) -> None:
        """移动到目标点。`button` 非空表示按键保持中——必须发 Dragged 类型，
        否则应用只看到"光标在动"，不会执行拖拽。"""
        x, y = int(xy[0]), int(xy[1])
        etype = _DRAGGED.get(button or "", _Q.kCGEventMouseMoved)
        ev = _Q.CGEventCreateMouseEvent(
            None, etype, (x, y), _BUTTON_CODE.get(button or "", _Q.kCGMouseButtonLeft))
        _Q.CGEventPost(_POST_TAP, ev)
        self._pos = (x, y)

    def _post_button(self, etype, button: str, clicks: int) -> None:
        x, y = self._pos
        ev = _Q.CGEventCreateMouseEvent(
            None, etype, (x, y), _BUTTON_CODE.get(button, _Q.kCGMouseButtonLeft))
        if clicks > 1:
            _Q.CGEventSetIntegerValueField(ev, _Q.kCGMouseEventClickState, clicks)
        _Q.CGEventPost(_POST_TAP, ev)

    def press(self, button: str = "left", clicks: int = 1) -> None:
        self._post_button(_DOWN.get(button, _Q.kCGEventLeftMouseDown), button, clicks)

    def release(self, button: str = "left", clicks: int = 1) -> None:
        self._post_button(_UP.get(button, _Q.kCGEventLeftMouseUp), button, clicks)

    def click(self, button: str = "left", count: int = 1) -> None:
        for i in range(1, count + 1):
            self.press(button, i)
            self.release(button, i)

    def scroll(self, dx: int, dy: int, unit: str = "line") -> None:
        # 两个轴一起创建：只创建 1 个轴再回填第 2 轴字段会被系统忽略（横向滚轮丢失）
        ev = _Q.CGEventCreateScrollWheelEvent(
            None, SCROLL_UNIT.get(unit, _Q.kCGScrollEventUnitLine), 2, int(dy), int(dx))
        _Q.CGEventPost(_POST_TAP, ev)


def _current_pos() -> tuple:
    loc = _Q.CGEventGetLocation(_Q.CGEventCreate(None))
    return (loc.x, loc.y)


class PlayOptions:
    def __init__(self, speed: float = 1.0, use_relative: bool = False,
                 base_x: int = 0, base_y: int = 0,
                 origin_x: int = 0, origin_y: int = 0,
                 suppress_keys: Optional[set] = None) -> None:
        self.speed = speed
        self.use_relative = use_relative
        self.base_x = base_x
        self.base_y = base_y
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.suppress_keys = suppress_keys or set()


class Player:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self.mouse = QuartzMouse()
        self.kb = KeyboardController()
        self._t0 = 0.0
        # 上一次回放的诊断计数（跳帧次数 / 总事件数），供 UI 展示
        self.last_skipped = 0
        self.last_total = 0

    def stop_playback(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ---- 主循环 ----
    def play(self, events: list, opt: PlayOptions,
             on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
        """回放一段事件序列；相对模式下偏移由基点与原点计算。

        返回诊断字典 `{"total": n, "skipped": k}`——`skipped` 是因落后而
        跳过的中间采样点数（不是事件丢失，落点仍然精确）。
        """
        self._stop.clear()
        speed = max(opt.speed, 0.01)
        has_mouse = any(ev.kind in ("move", "mouse", "wheel") for ev in events)
        ox, oy = opt.origin_x, opt.origin_y
        if opt.use_relative:
            if ox == 0 and oy == 0:
                for ev in events:
                    if ev.kind in ("move", "mouse", "wheel"):
                        ox, oy = ev.x, ev.y
                        break
            # 基点未设置（默认 0,0）→ 视为在原位置回放，避免整条轨迹飞到屏幕左上角
            base = (opt.base_x, opt.base_y) if (opt.base_x, opt.base_y) != (0, 0) else (ox, oy)
            dx, dy = base[0] - ox, base[1] - oy
            start = base
        else:
            dx, dy = 0, 0
            start = self._first_xy(events, opt)

        self._t0 = time.monotonic()
        skipped = 0
        total = len(events)
        pos = start
        if has_mouse:
            self.mouse.move_to(start)

        held_button: Optional[str] = None
        pressed_keys: list = []
        try:
            for i, ev in enumerate(events):
                if self.stopping:
                    break
                if on_progress and (i % 25 == 0 or i == total - 1):
                    on_progress(i + 1, total)
                due = self._t0 + ev.ts_ms / 1000.0 / speed

                if ev.kind == "key":
                    self._sleep_until(due)
                    self._play_key(ev, opt, pressed_keys)
                    continue

                target = (ev.x + dx, ev.y + dy)
                pos, jumped = self._travel(pos, target, due, held_button)
                skipped += jumped

                if ev.kind == "mouse":
                    btn = self._button(ev.button)
                    clicks = max(int(ev.clicks or 1), 1)
                    if ev.pressed:
                        held_button = btn
                        self.mouse.press(btn, clicks)
                    else:
                        held_button = None
                        self.mouse.release(btn, clicks)
                elif ev.kind == "wheel":
                    self.mouse.scroll(ev.wheel_dx, ev.wheel_dy,
                                      getattr(ev, "wheel_unit", "line"))
        finally:
            # 中断/异常时也要松开，避免鼠标键或键盘卡在按下状态
            if held_button:
                try:
                    self.mouse.release(held_button)
                except Exception:  # noqa: BLE001
                    pass
            for k in pressed_keys:
                try:
                    self.kb.release(k)
                except Exception:  # noqa: BLE001
                    pass
        if on_progress:
            on_progress(total, total)
        self.last_skipped, self.last_total = skipped, total
        return {"total": total, "skipped": skipped}

    # ---- 供节点使用的公开工具 ----
    def wait(self, seconds: float) -> None:
        self._sleep_until(time.monotonic() + max(seconds, 0.0))

    def glide_now(self, target: tuple) -> None:
        """立即从当前位置滑到目标点（短预算，用于节点式移动）。"""
        self._glide(self.mouse.position, target, 0.15)

    # ---- 调度核心 ----
    def _elapsed_s(self) -> float:
        return time.monotonic() - self._t0

    def _travel(self, pos: tuple, target: tuple, due: float,
                button: Optional[str]) -> tuple:
        """把光标从 pos 移到 target，计划时刻为 due。

        返回 `(新位置, 跳过的采样点数)`：`0` 表示按计划插值到达，
        `1` 表示因为没有剩余时间而一次性投递（落后时发生，落点仍精确）。
        """
        tx, ty = int(target[0]), int(target[1])
        cx, cy = pos
        dist = math.hypot(tx - cx, ty - cy)
        remaining = due - time.monotonic()

        if dist < 1:
            # 十字光标已在目标点（点击/原地事件）：只需等到计划时刻，
            # 否则 press/release 会挤在上一事件后立刻发出，应用不认这种瞬时点击。
            self._sleep_until(due)
            return (tx, ty), 0

        if remaining <= MIN_SAMPLE_S:
            # 已落后或几乎没有预算：跳帧——一次投递到目标，绝不追加延迟。
            # 这是 v2"剩余事件瞬时连发"的替代方案：落后被限制在单个事件内。
            self.mouse.move_to((tx, ty), button)
            return (tx, ty), 1

        # 长空档先等待，只把最后 TRAVEL_MAX_S 用于滑行（否则会匀速爬行很久）
        travel = min(remaining, TRAVEL_MAX_S)
        self._sleep_until(due - travel)

        steps = int(travel / SAMPLE_S)
        steps = max(1, min(steps, MAX_STEPS))
        step_dt = travel / steps
        start = time.monotonic()
        for s in range(1, steps + 1):
            if self.stopping:
                self.mouse.move_to((tx, ty), button)
                return (tx, ty), 0
            self._sleep_until(start + step_dt * s)
            ratio = s / steps
            self.mouse.move_to((cx + (tx - cx) * ratio, cy + (ty - cy) * ratio), button)
        return (tx, ty), 0

    def _glide(self, from_pos: tuple, target: tuple, budget_s: float) -> tuple:
        """兼容旧调用：给定预算时间把光标滑到目标。"""
        pos, _ = self._travel(from_pos, target, time.monotonic() + max(budget_s, 0.0), None)
        return pos

    def _sleep_until(self, deadline: float) -> None:
        """等待到指定时刻，随时响应停止。"""
        while not self.stopping:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            time.sleep(min(remain, 0.002))

    @staticmethod
    def _first_xy(events: list, opt: PlayOptions) -> tuple:
        for ev in events:
            if ev.kind in ("move", "mouse", "wheel"):
                return (ev.x, ev.y)
        return (opt.base_x, opt.base_y)

    @staticmethod
    def _button(name: Optional[str]) -> str:
        return name if name in ("left", "right", "middle") else "left"

    def _play_key(self, ev: MacroEvent, opt: PlayOptions, pressed_keys: list) -> None:
        if ev.key in opt.suppress_keys:
            return
        key = name_to_key(ev.key or "")
        if key is None:
            return
        try:
            if ev.pressed:
                self.kb.press(key)
                pressed_keys.append(key)
            else:
                self.kb.release(key)
                if key in pressed_keys:
                    pressed_keys.remove(key)
        except Exception:  # noqa: BLE001
            pass
