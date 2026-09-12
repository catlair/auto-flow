"""录制引擎：pynput 全局监听线程 → 事件队列 → 消费者。

- 移动事件按 4px 阈值过滤
- 原点固定为首个鼠标事件位置（相对坐标基准）
- 事件数达到上限自动停止
"""
from __future__ import annotations

import threading
import queue
from typing import Callable, Optional

from pynput import mouse

from core.events import MacroEvent, RecordResult, now_ms
from core.keymap import key_to_name
from core.maclistener import MacKeyboardListener

# pyobjc 的框架懒加载符号表非线程安全：keyboard tap 线程（maclistener）与
# pynput 鼠标 tap 线程并发首次访问 Quartz 符号会 KeyError('CGEventGetLocation')，
# 异常被 pynput 吞掉后【每条鼠标点击都丢失】（move 分支不访问该符号故幸存）。
# 导入期（单线程）主动解析全部后续会用到的符号，之后进程内命中缓存。
from Quartz import (  # noqa: F401
    CGEventGetLocation,
    CGEventGetIntegerValueField,
    CGEventGetFlags,
    CGEventPost,
    CGEventCreateKeyboardEvent,
    CGEventSetIntegerValueField,
    CGEventTapCreate,
    CGEventTapEnable,
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopRun,
    CFRunLoopStop,
    CFRunLoopGetCurrent,
)

MOVE_THRESHOLD_PX = 4
MAX_RECORD_EVENTS = 100000

ButtonName = {"left": "left", "right": "right", "middle": "middle"}


class Recorder:
    """调用 start() 后台监听；poll() 取增量事件；stop() 结束并返回 RecordResult。"""

    def __init__(self, skip_keys: Optional[set] = None,
                 window_bounds: Optional[tuple] = None) -> None:
        self._q: queue.Queue[Optional[MacroEvent]] = queue.Queue()
        self._events: list[MacroEvent] = []
        self._start_ms = 0
        self._last_pos: Optional[tuple[int, int]] = None
        self._origin: Optional[tuple[int, int]] = None
        self._stopped_by_limit = False
        self._stopping = False
        self._mouse_listener: Optional[mouse.Listener] = None
        self._kb_listener: Optional[MacKeyboardListener] = None
        # 热键物理键不进录制结果（否则停止录制的 F9 按键会被录成脏事件）
        self._skip_keys: set = set(skip_keys or ())
        # 丢帧定位计量：系统投递（入队）/ 阈值过滤 / 超限丢弃 / 监听线程死亡
        self._n_captured = 0
        self._n_filtered = 0
        self._n_limit_dropped = 0
        self._mouse_died = False
        self._kb_died = False
        # Auto Flow 自身窗口边界（逻辑坐标 x,y,w,h）：录制时丢弃落在窗口内的
        # 鼠标事件——点「停止录制」按钮、移动到按钮等操作天然不入库（确定性，
        # 无需猜测式裁剪）。边界由前端在 record.start/窗口移动时下发。
        self._win_bounds = window_bounds
        self._lock = threading.Lock()

    # ---- 监听回调（监听线程里执行，只入队） ----
    def _on_move(self, x: int, y: int, _dragged: bool) -> None:
        self._push(MacroEvent(ts_ms=now_ms() - self._start_ms, kind="move", x=int(x), y=int(y)))

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        self._push(MacroEvent(
            ts_ms=now_ms() - self._start_ms, kind="mouse",
            button=button.name if hasattr(button, "name") else str(button),
            pressed=pressed, x=int(x), y=int(y)))

    def _on_scroll(self, x: int, y: int, _dx, dy) -> None:
        self._push(MacroEvent(
            ts_ms=now_ms() - self._start_ms, kind="wheel",
            wheel_dx=0, wheel_dy=int(dy), x=int(x), y=int(y)))

    def _on_key_event(self, name: str, pressed: bool, _x: int = 0, _y: int = 0) -> None:
        if name in self._skip_keys:
            return
        self._push(MacroEvent(
            ts_ms=now_ms() - self._start_ms, kind="key",
            key=name, pressed=pressed))

    def _push(self, ev: MacroEvent) -> None:
        with self._lock:
            self._n_captured += 1
            if not self._stopping:
                self._q.put(ev)

    # ---- 生命周期 ----
    def start(self) -> None:
        self._start_ms = now_ms()
        self._stopping = False
        self._mouse_listener = mouse.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self._mouse_listener.start()
        # 键盘用自建 CGEventTap（pynput 键盘监听在 macOS 15 会崩溃，见 maclistener.py）
        self._kb_listener = MacKeyboardListener(self._on_key_event)
        self._kb_listener.start()

    def stop(self) -> RecordResult:
        # 先停监听再关闸：先置 _stopping 会把「最后一次 poll 与 stop 之间」
        # 仍在入队路上的事件判为丢弃，表现为录制尾部丢帧。
        for lis in (self._mouse_listener, self._kb_listener):
            if lis:
                lis.stop()
        # 记录监听线程健康状态：线程中途死亡（回调异常）会让「死亡时刻之后」的事件
        # 全部丢失，正是「中间某段没录到」的元凶之一，必须在结果里显式暴露。
        self._mouse_died = bool(self._mouse_listener and not self._mouse_listener.is_alive())
        self._kb_died = bool(self._kb_listener and not self._kb_listener.is_alive())
        self._stopping = True
        # 排空队列里残留事件
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            self._accept(ev)
        return self.result()

    def elapsed_ms(self) -> int:
        """录制已进行时长（毫秒），用于把尾部停留补进最后一个事件。"""
        return now_ms() - self._start_ms if self._start_ms else 0

    def set_window_bounds(self, bounds: Optional[tuple]) -> None:
        self._win_bounds = bounds

    def _in_window(self, x: int, y: int) -> bool:
        b = self._win_bounds
        if not b:
            return False
        bx, by, bw, bh = b
        return bx <= x < bx + bw and by <= y < by + bh

    def poll(self) -> list[MacroEvent]:
        out = []
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            self._accept(ev)
            out.append(ev)
        return out

    def _accept(self, ev: MacroEvent) -> None:
        with self._lock:
            if len(self._events) >= MAX_RECORD_EVENTS:
                self._stopped_by_limit = True
                self._n_limit_dropped += 1
                return
            if ev.kind in ("move", "mouse", "wheel") and self._in_window(ev.x, ev.y):
                self._n_filtered += 1  # 落在 Auto Flow 窗口内的交互不录制
                return
            if ev.kind == "move":
                if self._last_pos is not None and abs(ev.x - self._last_pos[0]) < MOVE_THRESHOLD_PX \
                        and abs(ev.y - self._last_pos[1]) < MOVE_THRESHOLD_PX:
                    self._n_filtered += 1
                    return
                self._last_pos = (ev.x, ev.y)
            else:
                self._last_pos = (ev.x, ev.y) if ev.kind != "key" else self._last_pos
            if ev.kind in ("move", "mouse", "wheel") and self._origin is None:
                self._origin = (ev.x, ev.y)
            self._events.append(ev)

    def result(self) -> RecordResult:
        with self._lock:
            ox, oy = self._origin or (0, 0)
            return RecordResult(events=list(self._events), origin_x=ox, origin_y=oy,
                                stopped_by_limit=self._stopped_by_limit,
                                n_captured=self._n_captured, n_filtered=self._n_filtered,
                                n_limit_dropped=self._n_limit_dropped,
                                mouse_listener_died=self._mouse_died,
                                kb_listener_died=self._kb_died)



def _trim(events: list[MacroEvent]) -> list[MacroEvent]:
    """裁掉「停止录制」交互本身（仅按钮停止时调用）。

    用鼠标点「停止录制」时，这次点击（和移向按钮的移动）已在事件序列里——
    回放会复现它，点到回放当时该位置的任意东西。规则：
    1) 从末次鼠标按下起全部移除（停止点击 + 其后残留）；
    2) 再移除尾部连续移动（移向停止按钮的路径），
       使回放终止于最后一次实质动作（点击/按键/滚轮）。
    """
    last_down = -1
    for i, ev in enumerate(events):
        if ev.kind == "mouse" and ev.pressed:
            last_down = i
    if last_down < 0:
        return events
    out = events[:last_down]
    while out and out[-1].kind == "move":
        out.pop()
    return out
