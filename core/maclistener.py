"""macOS 键盘全局监听：自建只读 CGEventTap。

pynput 的键盘监听在 macOS 15 上启动/输入源变化时会经 ctypes 调
TSMGetInputSourceProperty（HIToolbox 新增主线程断言）→ EXC_BREAKPOINT 崩溃
（与 Tauri 版 rdev 崩溃同源）。本模块只读 keycode/flags，绝不查询 TIS。
"""
from __future__ import annotations

import threading
from typing import Callable

import Quartz

from core.mackeys import modifier_edge, vk_to_name

_KEY_DOWN = Quartz.kCGEventKeyDown
_KEY_UP = Quartz.kCGEventKeyUp
_FLAGS_CHANGED = Quartz.kCGEventFlagsChanged


class MacKeyboardListener(threading.Thread):
    """后台线程创建事件 tap 并跑 CFRunLoop。

    on_key(name: str, pressed: bool) 在 tap 线程回调，消费方自行保证线程安全
    （Recorder 入队；UI 用 Qt 信号跨线程）。
    """

    def __init__(self, on_key: Callable[[str, bool], None]) -> None:
        super().__init__(daemon=True)
        self._on_key = on_key
        self._last_flags = 0
        self._runloop = None
        self._tap = None
        self._stopping = threading.Event()

    # ---- tap 回调（tap 线程）----
    def _callback(self, _proxy, evtype, event, _refcon):
        try:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            if evtype == _FLAGS_CHANGED:
                flags = int(Quartz.CGEventGetFlags(event))
                pressed = modifier_edge(int(keycode), flags, self._last_flags)
                self._last_flags = flags
                if pressed is not None:
                    self._dispatch(vk_to_name(int(keycode)), pressed)
            elif evtype == _KEY_DOWN:
                self._dispatch(vk_to_name(int(keycode)), True)
            elif evtype == _KEY_UP:
                self._dispatch(vk_to_name(int(keycode)), False)
        except Exception:
            pass
        return event

    def _dispatch(self, name, pressed):
        if name:
            self._on_key(name, pressed)

    # ---- 线程 ----
    def run(self) -> None:
        mask = (Quartz.CGEventMaskBit(_KEY_DOWN) | Quartz.CGEventMaskBit(_KEY_UP)
                | Quartz.CGEventMaskBit(_FLAGS_CHANGED))
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly, mask, self._callback, None)
        if tap is None:
            return  # 无输入监控权限
        self._tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        runloop = Quartz.CFRunLoopGetCurrent()
        self._runloop = runloop
        Quartz.CGEventTapEnable(tap, True)
        Quartz.CFRunLoopAddSource(runloop, source, Quartz.kCFRunLoopDefaultMode)
        Quartz.CFRunLoopRun()

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        rl = self._runloop
        if rl is not None:
            Quartz.CFRunLoopStop(rl)
