"""macOS 键盘全局监听：自建只读 CGEventTap。

pynput 的键盘监听在 macOS 15 上启动/输入源变化时会经 ctypes 调
TSMGetInputSourceProperty（HIToolbox 新增主线程断言）→ EXC_BREAKPOINT 崩溃
（与 Tauri 版 rdev 崩溃同源）。本模块只读 keycode/flags/unicode，绝不查询 TIS。

除逐键事件外，本模块还负责识别**文本提交**（输入法上屏的中文/emoji、或任何
`CGEventKeyboardSetUnicodeString` 投递的文本）：这类输入在事件层是"一次带
Unicode 字符串的按键"，用 `CGEventKeyboardGetUnicodeString` 取回原文后经
`on_text` 单独派发，录制端据此聚合为 text 事件（见 `core.mackeys.is_text_commit`
对判据的完整说明）。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import Quartz

# 预热懒加载符号（见 recorder.py 头注释：与 pynput 鼠标 tap 并发首访会 KeyError）
from Quartz import (  # noqa: F401
    CGEventGetLocation,
    CGEventGetIntegerValueField,
    CGEventGetFlags,
    CGEventKeyboardGetUnicodeString,
    CGEventTapCreate,
    CGEventTapEnable,
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopRun,
    CFRunLoopStop,
    CFRunLoopGetCurrent,
)

from core.mackeys import is_text_commit, modifier_edge, vk_to_name

logger = logging.getLogger("autoflow.keyboard")

_KEY_DOWN = Quartz.kCGEventKeyDown
_KEY_UP = Quartz.kCGEventKeyUp
_FLAGS_CHANGED = Quartz.kCGEventFlagsChanged
_MOUSE_MOVED = Quartz.kCGEventMouseMoved

# 单次读取的 Unicode 缓冲上限（UTF-16 码元数）。输入法一次上屏通常只有几个字，
# 但整句提交也见过，留足余量；超出会被系统截断而不是报错。
_MAX_UNICODE = 64


def _unicode_of(event) -> str:
    """取该键盘事件携带的 Unicode 文本（取不到返回空串，绝不抛）。

    pyobjc 的桥接约定：第 3/4 参是 out 参数，传 None 由桥接层按第 2 参分配，
    返回 `(实际长度, 文本)`——注意**不是** C 里的 4 参调用形式。

    **读到空串不代表"这个事件没有文本"**：没挂字符串的事件返回的是按 keycode
    换算出来的布局字符（实测 keycode 0 → `'a'`、0x24 → `'\\r'`），而不是 `''`。
    所以文本判定一律以内容为准（见 `core.mackeys.is_text_commit`），
    绝不能写成"文本为空即非文本"。
    """
    try:
        _n, s = Quartz.CGEventKeyboardGetUnicodeString(event, _MAX_UNICODE, None, None)
        return s or ""
    except Exception:  # noqa: BLE001
        return ""


def _source_pid(event) -> int:
    """投递该事件的进程 PID（硬件事件为 0，合成事件为投递方 PID）。

    唯一用途：区分"我们自己/输入法补的那个合成配对 keyUp"与真实按键。
    它**不参与**文本判定——万一某系统上硬件事件也带 PID，最坏后果只是少抑制
    一个无害的 keyUp，而不会把正常按键误判成文本。
    """
    try:
        return int(Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGEventSourceUnixProcessID))
    except Exception:  # noqa: BLE001
        return 0



class MacKeyboardListener(threading.Thread):
    """后台线程创建事件 tap 并跑 CFRunLoop。

    on_key(name: str, pressed: bool, x: int, y: int, flags: int) 在 tap 线程回调，
    坐标为按键时刻光标的逻辑位置（键盘事件自带 location——纯 CLI 进程里
    CGEventGetLocation(CGEventCreate(None)) 恒返回 (0,0)，不能用来读光标），
    flags 为该事件时刻的 CGEventFlags 快照（修饰键状态）。

    on_text(text: str, x: int, y: int) 在识别到**文本提交**时回调（输入法上屏 /
    合成 Unicode 投递）。不传则该类输入直接丢弃——热键监听不需要它。

    消费方自行保证线程安全（Recorder 入队；UI 用 Qt 信号跨线程）。
    """

    def __init__(self, on_key: Callable[[str, bool, int, int], None],
                 on_text: Optional[Callable[[str, int, int], None]] = None) -> None:
        super().__init__(daemon=True)
        self._on_key = on_key
        self._on_text = on_text
        self._last_flags = 0
        self._last_mouse = (0, 0)   # 最近光标位置（兜底：合成键盘事件 location=(0,0)）
        # 文本提交后会紧跟一个合成配对 keyUp（keycode=0、无内容）。把它记下来
        # 抑制掉，否则它会被当成一次 A 键释放录进去（keycode 0 就是物理 A 键）。
        self._expect_synth_up = False
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
            keycode = int(Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode))
            loc = Quartz.CGEventGetLocation(event)
            x, y = int(round(loc.x)), int(round(loc.y))
            if (x, y) == (0, 0):
                # 合成键盘事件不带 location：用最近鼠标事件的光标位置兜底
                x, y = self._last_mouse
            flags = int(Quartz.CGEventGetFlags(event))
            if evtype == _KEY_DOWN:
                text = _unicode_of(event)
                if is_text_commit(keycode, text):
                    self._expect_synth_up = True
                    if text and self._on_text is not None:
                        self._on_text(text, x, y)
                    return event
                self._expect_synth_up = False
                self._dispatch(vk_to_name(keycode), True, x, y, flags)
            elif evtype == _KEY_UP:
                # 合成配对 keyUp：紧跟文本提交、keycode=0、且由进程投递（非硬件）
                if (self._expect_synth_up and keycode == 0
                        and _source_pid(event) != 0):
                    self._expect_synth_up = False
                    return event
                self._expect_synth_up = False
                self._dispatch(vk_to_name(keycode), False, x, y, flags)
            elif evtype == _FLAGS_CHANGED:
                self._expect_synth_up = False
                pressed = modifier_edge(keycode, flags, self._last_flags)
                self._last_flags = flags
                if pressed is not None:
                    self._dispatch(vk_to_name(keycode), pressed, x, y, flags)
        except Exception:
            # 回调异常必须留痕：此前静默 pass，消费方签名不匹配时表现为
            # 「热键/按键捕获完全无效，日志毫无线索」。只记首次，避免刷屏。
            if not self._callback_error_logged:
                self._callback_error_logged = True
                logger.exception("键盘 tap 回调异常（同类异常后续不再记录）")
        return event

    def _dispatch(self, name, pressed, x, y, flags: int = 0):
        if name:
            self._on_key(name, pressed, x, y, flags)

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
