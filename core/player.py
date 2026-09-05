"""回放引擎：按事件时间戳重放，鼠标移动做轨迹插值，支持速度倍率与停止标志。

插值算法与 Tauri 版 macro-recorder 一致：
两点间按 10px 步长滑行（≤120 步），步数同时受事件时间预算约束（≥8ms/步），
保证轨迹连续、不瞬移。时间基准为事件绝对时间戳 ts_ms / speed。
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Controller as KeyboardController

from core.events import MacroEvent
from core.keymap import name_to_key

STEP_PX = 10.0
MAX_STEPS = 120
MIN_STEP_S = 0.008


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
        self.mouse = MouseController()
        self.kb = KeyboardController()
        self._t0 = 0.0

    def stop_playback(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def play(self, events: list, opt: PlayOptions,
             on_progress: Optional[Callable[[int, int], None]] = None) -> None:
        """回放一段事件序列；相对模式下偏移由基点与原点计算。"""
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
        if has_mouse:
            self.mouse.position = start
        self._t0 = time.monotonic()

        pos = start
        held_button: Optional[Button] = None
        pressed_keys: list = []
        total = len(events)
        try:
            for i, ev in enumerate(events):
                if self.stopping:
                    break
                if on_progress and (i % 25 == 0 or i == total - 1):
                    on_progress(i + 1, total)
                budget_s = ev.ts_ms / 1000.0 / speed - self._elapsed_s()
                if ev.kind == "key":
                    self._wait(budget_s)
                    self._play_key(ev, opt, pressed_keys)
                elif ev.kind in ("mouse", "move", "wheel"):
                    target = (ev.x + dx, ev.y + dy)
                    # 滑行消耗时间预算；已到位（点击/原地事件）也必须等到计划
                    # 时间点再执行——否则 press/release 挤在上一事件后立刻发出，
                    # 与录制时序脱节，表现为「点击录到了但回放无效果」。
                    if math.hypot(target[0] - pos[0], target[1] - pos[1]) < 1:
                        self._wait(max(budget_s, 0.0))
                    pos = self._glide(pos, target, max(budget_s, 0.0))
                    if ev.kind == "mouse":
                        btn = self._button(ev.button)
                        if ev.pressed:
                            held_button = btn
                            self.mouse.press(btn)
                        else:
                            held_button = None
                            self.mouse.release(btn)
                    elif ev.kind == "wheel":
                        self.mouse.scroll(ev.wheel_dx, ev.wheel_dy)
        finally:
            # 中断/异常时也要松开，避免鼠标键或键盘卡在按下状态
            if held_button:
                try:
                    self.mouse.release(held_button)
                except Exception:
                    pass
            for k in pressed_keys:
                try:
                    self.kb.release(k)
                except Exception:
                    pass
        if on_progress:
            on_progress(total, total)

    # ---- 供节点使用的公开工具 ----
    def wait(self, seconds: float) -> None:
        self._wait(seconds)

    def glide_now(self, target: tuple) -> None:
        """立即从当前位置滑到目标点（短预算，用于节点式移动）。"""
        self._glide(self.mouse.position, target, 0.15)

    def _elapsed_s(self) -> float:
        return time.monotonic() - self._t0

    @staticmethod
    def _first_xy(events: list, opt: PlayOptions) -> tuple:
        for ev in events:
            if ev.kind in ("move", "mouse", "wheel"):
                return (ev.x, ev.y)
        return (opt.base_x, opt.base_y)

    @staticmethod
    def _button(name: Optional[str]) -> Button:
        return {"left": Button.left, "right": Button.right, "middle": Button.middle}.get(
            name or "left", Button.left)

    def _wait(self, seconds: float) -> None:
        """等待到事件的计划时间点，随时响应停止。"""
        deadline = time.monotonic() + max(seconds, 0.0)
        while not self.stopping:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            time.sleep(min(remain, 0.02))

    def _glide(self, from_pos: tuple, target: tuple, budget_s: float) -> tuple:
        """从当前位置滑向目标：10px 步长、≤120 步、步数受时间预算约束。"""
        cx, cy = from_pos
        tx, ty = int(target[0]), int(target[1])
        dist = math.hypot(tx - cx, ty - cy)
        if dist < 1:
            return (tx, ty)
        steps = min(max(int(math.ceil(dist / STEP_PX)), 1), MAX_STEPS)
        if budget_s > MIN_STEP_S:
            steps = min(steps, max(int(budget_s / MIN_STEP_S), 1))
        step_wait = max(min(budget_s / steps, 0.05), MIN_STEP_S) if steps else MIN_STEP_S
        for s in range(1, steps + 1):
            if self.stopping:
                self.mouse.position = (tx, ty)
                return (tx, ty)
            ratio = s / steps
            self.mouse.position = (int(cx + (tx - cx) * ratio), int(cy + (ty - cy) * ratio))
            time.sleep(step_wait)
        self.mouse.position = (tx, ty)
        return (tx, ty)

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
        except Exception:
            pass
