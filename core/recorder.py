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

MOVE_THRESHOLD_PX = 4
MAX_RECORD_EVENTS = 20000

ButtonName = {"left": "left", "right": "right", "middle": "middle"}


class Recorder:
    """调用 start() 后台监听；poll() 取增量事件；stop() 结束并返回 RecordResult。"""

    def __init__(self) -> None:
        self._q: queue.Queue[Optional[MacroEvent]] = queue.Queue()
        self._events: list[MacroEvent] = []
        self._start_ms = 0
        self._last_pos: Optional[tuple[int, int]] = None
        self._origin: Optional[tuple[int, int]] = None
        self._stopped_by_limit = False
        self._stopping = False
        self._mouse_listener: Optional[mouse.Listener] = None
        self._kb_listener: Optional[MacKeyboardListener] = None
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

    def _on_key_event(self, name: str, pressed: bool) -> None:
        self._push(MacroEvent(
            ts_ms=now_ms() - self._start_ms, kind="key",
            key=name, pressed=pressed))

    def _push(self, ev: MacroEvent) -> None:
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
        self._stopping = True
        for lis in (self._mouse_listener, self._kb_listener):
            if lis:
                lis.stop()
        # 排空队列里残留事件
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            self._accept(ev)
        return self.result()

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
                return
            if ev.kind == "move":
                if self._last_pos is not None and abs(ev.x - self._last_pos[0]) < MOVE_THRESHOLD_PX \
                        and abs(ev.y - self._last_pos[1]) < MOVE_THRESHOLD_PX:
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
                                stopped_by_limit=self._stopped_by_limit)
