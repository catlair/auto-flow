"""录制引擎 v3：全局监听 → 自适应保轨采样 → 事件队列 → 时间线。

设计原则（v3 重写）：**只消除冗余采样，不删除任何信息**。

v2 的四类"静默丢弃"及其修复：

1. **拖拽语义丢失**。v2 的 `_on_move(x, y, _dragged)` 以为第三参是 dragged——
   实际是 pynput 的 `injected`（事件是否由事件源合成）。真正的拖拽信息从未被
   记录，导致回放端只能发 `MouseMoved`，所有拖拽类操作（拖文件、拖滚动条、
   框选）回放失效。v3 自行跟踪按键保持状态（见 `_held_buttons`），移动事件
   带 `dragged` 标记。
2. **固定 4px 阈值**。v2 用「与上一个已保留点比较、x/y 同时 <4px 才丢」的规则，
   慢速操作与精细微调会被大段吃掉，且最后一个位置常常丢失，回放终点不对。
   v3 改为「位移 ≥2px **或** 距上次保留 ≥20ms **或** 处于拖拽中」三者之一即保留，
   并额外记住最后一个被降采样的位置，停止时补回（轨迹终点精确）。
3. **窗口矩形过滤**。v2 默认按 Auto Flow 窗口矩形丢弃落于其中的事件，边界只
   在录制开始时下发一次、前端监听从不注销——边界一旦过期就成片吞掉真实操作，
   这正是"莫名其妙被裁掉"的主因。v3 默认**关闭**该过滤（`drop_in_window=False`）；
   窗口边界仍会传给录制器，但只用于停止时**定位停止点击**做尾部裁剪。
4. **语义缺失**。v3 补齐 `clicks`（双击/三击序列号）、`wheel_unit`（滚轮单位）、
   `flags`，并让 key 事件也带上坐标。
"""
from __future__ import annotations

import threading
import queue
from typing import Optional

from pynput import mouse

from core.events import MacroEvent, RecordResult, now_ms
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

# ---- 采样参数（只影响"记录得多密"，不影响"记录到没有"）----
MOVE_MIN_PX = 2          # 相对上一个已保留点的最小切比雪夫位移
MOVE_MAX_GAP_MS = 20     # 距上一个已保留点的最大时间间隔（超时必须保留）
MAX_RECORD_EVENTS = 100000

# ---- 点击序列（双击/三击）归并参数 ----
# macOS 默认双击间隔约 500ms；超出即视为新的一次单击。
DOUBLE_CLICK_MS = 500
DOUBLE_CLICK_PX = 6
MAX_CLICKS = 3

ButtonName = {"left": "left", "right": "right", "middle": "middle"}


class Recorder:
    """调用 start() 后台监听；poll() 取增量事件；stop() 结束并返回 RecordResult。"""

    def __init__(self, skip_keys: Optional[set] = None,
                 window_bounds: Optional[tuple] = None,
                 drop_in_window: bool = False) -> None:
        self._q: queue.Queue[Optional[MacroEvent]] = queue.Queue()
        self._events: list[MacroEvent] = []
        self._start_ms = 0
        self._origin: Optional[tuple[int, int]] = None
        self._stopped_by_limit = False
        self._stopping = False
        self._mouse_listener: Optional[mouse.Listener] = None
        self._kb_listener: Optional[MacKeyboardListener] = None
        # 热键物理键不进录制结果（否则停止录制的 F9 按键会被录成脏事件）
        self._skip_keys: set = set(skip_keys or ())

        # ---- 保轨采样状态（仅在监听线程内读写）----
        self._held_buttons: set = set()          # 当前按住的鼠标键 → dragged 判据
        self._last_kept_xy: Optional[tuple[int, int]] = None
        self._last_kept_ts: int = 0
        self._pending_move: Optional[MacroEvent] = None   # 最近一个被降采样的移动
        # 点击序列：最后一次按下 → (按下时刻, x, y, 序列号)
        self._last_press: dict[str, tuple[int, int, int, int]] = {}
        self._press_clicks: dict[str, int] = {}

        # ---- 计量（丢帧定位）----
        self._n_captured = 0        # 系统投递（入队）
        self._n_decimated = 0       # 保轨降采样丢弃的冗余移动
        self._n_window_dropped = 0  # 窗口过滤丢弃（仅在显式开启时非零）
        self._n_limit_dropped = 0   # 超上限丢弃
        self._mouse_died = False
        self._kb_died = False

        # Auto Flow 自身窗口边界（逻辑坐标 x,y,w,h）。默认**不**据此丢弃事件，
        # 仅在 stop(trim=True) 时用于定位"停止按钮那次点击"。
        self._win_bounds = window_bounds
        self._drop_in_window = bool(drop_in_window)
        self._lock = threading.Lock()

    # ---- 监听回调（监听线程里执行，只入队） ----
    # 注意：pynput 的 darwin 后端会用 (x, y, injected) / (x, y, button, pressed,
    # injected) / (x, y, dx, dy, injected) 调用回调，多余的 injected 由 pynput
    # 的 _wrap 按签名截断，这里不要试图读取它。
    def _on_move(self, x: int, y: int, _injected: bool = False) -> None:
        self._push(MacroEvent(
            ts_ms=now_ms() - self._start_ms, kind="move",
            x=int(x), y=int(y), dragged=bool(self._held_buttons)))

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        name = button.name if hasattr(button, "name") else str(button)
        xi, yi = int(x), int(y)
        ts = now_ms() - self._start_ms
        if pressed:
            self._held_buttons.add(name)
            clicks = self._next_clicks(name, xi, yi, ts)
            self._press_clicks[name] = clicks
        else:
            self._held_buttons.discard(name)
            clicks = self._press_clicks.pop(name, 1)
        self._push(MacroEvent(
            ts_ms=ts, kind="mouse", button=name,
            pressed=bool(pressed), x=xi, y=yi, clicks=clicks))

    def _on_scroll(self, x: int, y: int, dx, dy) -> None:
        # pynput 取的是 kCGScrollWheelEventDeltaAxis1/2——**行**增量（触控板同样
        # 走这两个字段），不是像素。回放端必须按 line 单位投递，否则量级差一个
        # 数量级，表现为"滚轮回放几乎不动"。
        self._push(MacroEvent(
            ts_ms=now_ms() - self._start_ms, kind="wheel",
            wheel_dx=int(dx), wheel_dy=int(dy), wheel_unit="line",
            x=int(x), y=int(y)))

    def _on_key_event(self, name: str, pressed: bool, x: int = 0, y: int = 0,
                      flags: int = 0) -> None:
        if name in self._skip_keys:
            return
        self._push(MacroEvent(
            ts_ms=now_ms() - self._start_ms, kind="key",
            key=name, pressed=bool(pressed), x=int(x), y=int(y), flags=int(flags)))

    def _next_clicks(self, name: str, x: int, y: int, ts: int) -> int:
        """把相邻的快速按下归并成双击/三击序列号（回放写入 ClickState）。

        `ts` 与事件时间戳同源（调用方算一次传进来），避免取两次时钟产生偏差。
        """
        last = self._last_press.get(name)
        if (last is not None
                and ts - last[0] <= DOUBLE_CLICK_MS
                and abs(x - last[1]) <= DOUBLE_CLICK_PX
                and abs(y - last[2]) <= DOUBLE_CLICK_PX):
            clicks = min(last[3] + 1, MAX_CLICKS)
        else:
            clicks = 1
        self._last_press[name] = (ts, x, y, clicks)
        return clicks

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
        # 监听线程存活状态必须在 stop() **之前**采样：pynput 的 stop() 不 join，
        # 线程可能仍存活；而自建 tap 的 CFRunLoopStop 是异步的——stop 之后再读
        # is_alive() 得到的是竞态结果，会把健康监听报成"中途死亡"。
        # 判定含义：录音结束时监听线程已不在运行 = 它中途退出（回调异常或
        # CGEventTap 创建失败），死亡时刻之后的事件全部丢失。
        self._mouse_died = bool(self._mouse_listener is not None
                                and not self._mouse_listener.is_alive())
        self._kb_died = bool(self._kb_listener is not None
                             and not self._kb_listener.is_alive())
        # 先停监听再关闸：先置 _stopping 会把「最后一次 poll 与 stop 之间」
        # 仍在入队路上的事件判为丢弃，表现为录制尾部丢帧。
        for lis in (self._mouse_listener, self._kb_listener):
            if lis:
                try:
                    lis.stop()
                except Exception:  # noqa: BLE001
                    pass
        self._stopping = True
        # 排空队列里残留事件
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            self._accept(ev)
        # 补回最后一个被降采样的位置：否则"移过去就停手"的终点会停在轨迹中段
        self._flush_pending_move()
        return self.result()

    def elapsed_ms(self) -> int:
        """录制已进行时长（毫秒），用于把尾部停留补进最后一个事件。"""
        return now_ms() - self._start_ms if self._start_ms else 0

    def set_window_bounds(self, bounds: Optional[tuple]) -> None:
        self._win_bounds = bounds

    def set_drop_in_window(self, on: bool) -> None:
        """显式开关窗口过滤（默认关）。仅调试/特例场景使用。"""
        self._drop_in_window = bool(on)

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

    # ---- 保轨采样 ----
    def _accept(self, ev: MacroEvent) -> None:
        with self._lock:
            if len(self._events) >= MAX_RECORD_EVENTS:
                self._stopped_by_limit = True
                self._n_limit_dropped += 1
                return
            if (self._drop_in_window and ev.kind in ("move", "mouse", "wheel")
                    and self._in_window(ev.x, ev.y)):
                self._n_window_dropped += 1
                return
            if ev.kind == "move" and not self._keep_move(ev):
                self._n_decimated += 1
                self._pending_move = ev      # 记住被降采样的位置，停止时补回
                return
            # 非移动事件一定保留；其坐标成为后续移动的比较基准
            if ev.kind == "move":
                self._pending_move = None
            self._last_kept_xy = (ev.x, ev.y)
            self._last_kept_ts = ev.ts_ms
            if ev.kind in ("move", "mouse", "wheel") and self._origin is None:
                self._origin = (ev.x, ev.y)
            self._events.append(ev)

    def _keep_move(self, ev: MacroEvent) -> bool:
        """是否保留这条移动。三选一即保留——只删冗余采样，不删信息。"""
        if ev.dragged:
            # 拖拽期间轨迹就是操作语义本身（画线、拖拽距离），永不降采样
            return True
        if self._last_kept_xy is None:
            return True
        lx, ly = self._last_kept_xy
        if max(abs(ev.x - lx), abs(ev.y - ly)) >= MOVE_MIN_PX:
            return True
        if ev.ts_ms - self._last_kept_ts >= MOVE_MAX_GAP_MS:
            return True
        return False

    def _flush_pending_move(self) -> None:
        """把最后一个被降采样的位置补到序列末尾，保证轨迹终点精确。"""
        pm = self._pending_move
        self._pending_move = None
        if pm is None:
            return
        if self._last_kept_xy == (pm.x, pm.y):
            return
        if self._events and self._events[-1].kind == "move" \
                and (self._events[-1].x, self._events[-1].y) == (pm.x, pm.y):
            return
        self._last_kept_xy = (pm.x, pm.y)
        self._last_kept_ts = pm.ts_ms
        self._events.append(pm)

    def result(self) -> RecordResult:
        with self._lock:
            ox, oy = self._origin or (0, 0)
            return RecordResult(events=list(self._events), origin_x=ox, origin_y=oy,
                                stopped_by_limit=self._stopped_by_limit,
                                n_captured=self._n_captured,
                                n_filtered=self._n_decimated + self._n_window_dropped,
                                n_limit_dropped=self._n_limit_dropped,
                                mouse_listener_died=self._mouse_died,
                                kb_listener_died=self._kb_died,
                                n_decimated=self._n_decimated,
                                n_window_dropped=self._n_window_dropped)


def trim_stop_interaction(events: list[MacroEvent],
                          window_bounds: Optional[tuple] = None) -> list[MacroEvent]:
    """裁掉「停止录制」这次交互（按钮停止时调用）。

    用鼠标点「停止录制」时，这次点击（和移向按钮的路径）已在事件序列里——
    回放会复现它，点到回放当时该位置的任意东西。规则：

    1) 找到最后一次**落在 Auto Flow 窗口内**的鼠标按下——那就是停止点击，
       从其处截断；
    2) 再移除尾部连续移动（移向停止按钮的路径），
       使回放终止于最后一次实质动作（点击/按键/滚轮）。

    **识别不到就不裁**：窗口边界缺失、或边界内没有任何鼠标按下时一律原样返回。
    宁可让一次停止点击被回放（顶多多点一下），也不要把用户真实的最后一次
    拖拽（同样以按下开始）误判成停止操作而删掉——后者是静默的数据损坏。
    """
    if not events or not window_bounds:
        return events
    bx, by, bw, bh = window_bounds
    idx = -1
    for i, ev in enumerate(events):
        if ev.kind == "mouse" and ev.pressed \
                and bx <= ev.x < bx + bw and by <= ev.y < by + bh:
            idx = i
    if idx < 0:
        return events
    out = events[:idx]
    while out and out[-1].kind == "move":
        out.pop()
    return out
