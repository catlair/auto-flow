"""macOS 键盘全局监听：自建只读 CGEventTap。

pynput 的键盘监听在 macOS 15 上启动/输入源变化时会经 ctypes 调
TSMGetInputSourceProperty（HIToolbox 新增主线程断言）→ EXC_BREAKPOINT 崩溃
（与 Tauri 版 rdev 崩溃同源）。本模块只读 keycode/flags，绝不查询 TIS。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

import Quartz

# 预热懒加载符号（见 recorder.py 头注释：与 pynput 鼠标 tap 并发首访会 KeyError）
from Quartz import (  # noqa: F401
    CGEventGetLocation,
    CGEventGetIntegerValueField,
    CGEventGetFlags,
    CGEventTapCreate,
    CGEventTapEnable,
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopRun,
    CFRunLoopStop,
    CFRunLoopGetCurrent,
)

from core.mackeys import modifier_edge, vk_to_name

logger = logging.getLogger("autoflow.keyboard")

_KEY_DOWN = Quartz.kCGEventKeyDown
_KEY_UP = Quartz.kCGEventKeyUp
_FLAGS_CHANGED = Quartz.kCGEventFlagsChanged
_MOUSE_MOVED = Quartz.kCGEventMouseMoved


class MacKeyboardListener(threading.Thread):
    """后台线程创建事件 tap 并跑 CFRunLoop。

    on_key(name: str, pressed: bool, x: int, y: int) 在 tap 线程回调，坐标为
    按键时刻光标的逻辑位置（键盘事件自带 location——纯 CLI 进程里
    CGEventGetLocation(CGEventCreate(None)) 恒返回 (0,0)，不能用来读光标）。
    消费方自行保证线程安全（Recorder 入队；UI 用 Qt 信号跨线程）。
    """

    def __init__(self, on_key: Callable[[str, bool, int, int], None]) -> None:
        super().__init__(daemon=True)
        self._on_key = on_key
        self._last_flags = 0
        self._last_mouse = (0, 0)   # 最近光标位置（兜底：合成键盘事件 location=(0,0)）
        self._runloop = None
        self._tap = None
        self._stopping = threading.Event()
        self._callback_error_logged = False

    # ---- tap 回调（tap 线程）----
    def _callback(self, _proxy, evtype, event, _refcon):
        try:
            if evtype == _MOUSE_MOVED:
                loc = Quartz.CGEventGetLocation(event)
                self._last_mouse = (int(round(loc.x)), int(round(loc.y)))
                return event
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            loc = Quartz.CGEventGetLocation(event)
            x, y = int(round(loc.x)), int(round(loc.y))
            if (x, y) == (0, 0):
                # 合成键盘事件不带 location：用最近鼠标事件的光标位置兜底
                x, y = self._last_mouse
            if evtype == _FLAGS_CHANGED:
                flags = int(Quartz.CGEventGetFlags(event))
                pressed = modifier_edge(int(keycode), flags, self._last_flags)
                self._last_flags = flags
                if pressed is not None:
                    self._dispatch(vk_to_name(int(keycode)), pressed, x, y)
            elif evtype == _KEY_DOWN:
                self._dispatch(vk_to_name(int(keycode)), True, x, y)
            elif evtype == _KEY_UP:
                self._dispatch(vk_to_name(int(keycode)), False, x, y)
        except Exception:
            # 回调异常必须留痕：此前静默 pass，消费方签名不匹配时表现为
            # 「热键/按键捕获完全无效，日志毫无线索」。只记首次，避免刷屏。
            if not self._callback_error_logged:
                self._callback_error_logged = True
                logger.exception("键盘 tap 回调异常（同类异常后续不再记录）")
        return event

    def _dispatch(self, name, pressed, x, y):
        if name:
            self._on_key(name, pressed, x, y)

    # ---- 线程 ----
    def run(self) -> None:
        mask = (Quartz.CGEventMaskBit(_KEY_DOWN) | Quartz.CGEventMaskBit(_KEY_UP)
                | Quartz.CGEventMaskBit(_FLAGS_CHANGED)
                | Quartz.CGEventMaskBit(_MOUSE_MOVED))
        try:
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly, mask, self._callback, None)
        except Exception:
            logger.exception("CGEventTapCreate 抛异常")
            return
        if tap is None:
            # 无输入监控权限/被策略拒绝。绝不能静默返回——否则表现为「热键无效」
            # 而日志毫无线索（frozen sidecar 的 stderr 通常不可见）。
            logger.error("CGEventTapCreate 返回 None：无输入监控权限或被系统拒绝")
            return
        self._tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        runloop = Quartz.CFRunLoopGetCurrent()
        self._runloop = runloop
        Quartz.CGEventTapEnable(tap, True)
        Quartz.CFRunLoopAddSource(runloop, source, Quartz.kCFRunLoopDefaultMode)
        logger.info("keyboard tap 已建立（mask=%#x）", mask)
        Quartz.CFRunLoopRun()
        logger.info("keyboard tap runloop 退出")

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        rl = self._runloop
        if rl is not None:
            Quartz.CFRunLoopStop(rl)
