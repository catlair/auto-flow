"""鼠标节点的行为用例（滚动 / 窗口锚点）。

这些用例都是「真机上踩过才补的」：
- 滚动：滚轮事件落在**光标所在窗口**上，不先把光标挪过去就是「滚了没反应」。
- 窗口锚点：算出来的点必须落在窗口里，否则会把点击送到屏幕左上角的苹果菜单上。
"""
from __future__ import annotations

from tasks.builtin import MouseActionTask


class _Mouse:
    def __init__(self):
        self.clicks = []
        self.presses = []
        self.releases = []

    def click(self, b, n=1):
        self.clicks.append((b, n))

    def press(self, b):
        self.presses.append(b)

    def release(self, b):
        self.releases.append(b)


class _Player:
    def __init__(self):
        self.mouse = _Mouse()
        self.calls = []
        self.stopping = False

    def glide_now(self, pt):
        self.calls.append(("glide", tuple(pt)))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))


class _Ctx:
    def __init__(self, params):
        self.params = params
        self.player = _Player()
        self.base_x = 0
        self.base_y = 0
        self.port = None

    def set_port(self, p):
        self.port = p


def test_scroll_moves_cursor_to_target_first():
    ctx = _Ctx({"action": "scroll", "anchor": "absolute", "x": 100, "y": 200,
                "scroll_dx": 0, "scroll_dy": 5})
    MouseActionTask().run(ctx)
    assert ctx.player.calls == [("glide", (100, 200)), ("scroll", 0, 5)], \
        "必须先挪光标再滚轮——滚轮事件只作用于光标下的窗口"
    assert ctx.player.mouse.clicks == [], "滚动不该产生点击"


def test_scroll_up_is_negative():
    ctx = _Ctx({"action": "scroll", "anchor": "absolute", "x": 1, "y": 2, "scroll_dy": -4})
    MouseActionTask().run(ctx)
    assert ctx.player.calls[-1] == ("scroll", 0, -4)


def test_click_with_window_anchor_uses_resolved_point():
    from core import selfmask
    selfmask.reset_cache()
    # 桩：前台窗口 (100, 50, 800, 482)；右下角往里 (-29,-30) → (871, 502)
    orig = selfmask.front_window_rect
    selfmask.front_window_rect = lambda: (100, 50, 800, 482)
    try:
        ctx = _Ctx({"action": "click", "anchor": "win_br", "x": -29, "y": -30})
        MouseActionTask().run(ctx)
        assert ctx.player.calls == [("glide", (871, 502))]
        assert ctx.player.mouse.clicks == [("left", 1)]
    finally:
        selfmask.front_window_rect = orig


def test_click_is_skipped_when_point_falls_outside_window():
    """点算到窗口外 → 一次都不该点（真机上把苹果菜单点开过）。"""
    from core import selfmask
    selfmask.reset_cache()
    orig = selfmask.front_window_rect
    selfmask.front_window_rect = lambda: (100, 50, 800, 482)
    try:
        ctx = _Ctx({"action": "click", "anchor": "win_br", "x": -5000, "y": -30})
        MouseActionTask().run(ctx)
        assert ctx.player.calls == [], "不该挪光标"
        assert ctx.player.mouse.clicks == [], "不该点"
    finally:
        selfmask.front_window_rect = orig


def test_click_is_skipped_when_no_front_window():
    from core import selfmask
    selfmask.reset_cache()
    orig = selfmask.front_window_rect
    selfmask.front_window_rect = lambda: None
    try:
        ctx = _Ctx({"action": "click", "anchor": "win_tr", "x": -20, "y": 46})
        MouseActionTask().run(ctx)
        assert ctx.player.mouse.clicks == []
    finally:
        selfmask.front_window_rect = orig
