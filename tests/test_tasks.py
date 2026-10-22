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
        self.scrolls = []

    def scroll(self, dx, dy, unit="line"):
        self.scrolls.append((dx, dy, unit))

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
    assert ctx.player.calls == [("glide", (100, 200))], \
        "必须先把光标挪过去——滚轮事件只作用于光标下的窗口"
    assert ctx.player.mouse.scrolls == [(0, 5, "line")], "滚轮要发在 player.mouse 上（Player 没有 scroll）"
    assert ctx.player.mouse.clicks == [], "滚动不该产生点击"


def test_scroll_up_is_negative():
    ctx = _Ctx({"action": "scroll", "anchor": "absolute", "x": 1, "y": 2, "scroll_dy": -4})
    MouseActionTask().run(ctx)
    assert ctx.player.mouse.scrolls == [(0, -4, "line")]


def test_scroll_unit_is_passed_through():
    """游戏里常常只认像素单位；单位要能透传到 player.mouse.scroll。"""
    ctx = _Ctx({"action": "scroll", "anchor": "absolute", "x": 5, "y": 6,
                "scroll_dy": 1200, "scroll_unit": "pixel"})
    MouseActionTask().run(ctx)
    assert ctx.player.mouse.scrolls == [(0, 1200, "pixel")]


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


class _Hit:
    def __init__(self, x, y, text="解决诬陷"):
        self.x, self.y, self.w, self.h, self.text = x, y, 120, 20, text


def test_filter_hits_keeps_only_left_side():
    """卡片在左、详情面板在右，两边写着同一句话——只留左边的。"""
    from tasks.builtin import filter_hits
    win = (0, 0, 1000, 600)
    hits = [_Hit(300, 200), _Hit(800, 210)]   # 300=卡片，800=右侧面板
    kept = filter_hits(hits, win, 0, 70)
    assert [h.x for h in kept] == [300]


def test_filter_hits_no_window_means_no_filter():
    """读不到前台窗口时不过滤——宁可点多，也别因为拿不到窗口就完全不点。"""
    from tasks.builtin import filter_hits
    hits = [_Hit(300, 200), _Hit(800, 210)]
    assert len(filter_hits(hits, None, 0, 70)) == 2


def test_filter_hits_uses_window_origin():
    """百分比是相对**窗口**算的，不是相对屏幕。窗口在屏幕中间时不能算错。"""
    from tasks.builtin import filter_hits
    win = (500, 100, 1000, 600)          # 窗口横向 500..1500
    hits = [_Hit(600, 200), _Hit(1200, 210)]
    kept = filter_hits(hits, win, 0, 70)  # 右边界 = 500 + 700 = 1200
    assert [h.x for h in kept] == [600, 1200]
    kept = filter_hits(hits, win, 0, 60)  # 右边界 = 1100
    assert [h.x for h in kept] == [600]


def test_filter_hits_swapped_bounds_are_tolerated():
    from tasks.builtin import filter_hits
    win = (0, 0, 1000, 600)
    hits = [_Hit(300, 200), _Hit(800, 210)]
    assert [h.x for h in filter_hits(hits, win, 70, 0)] == [300]


def test_pick_hit_modes():
    from tasks.builtin import pick_hit
    hits = [_Hit(1, 1), _Hit(2, 2), _Hit(3, 3)]
    assert pick_hit(hits, "第一个").x == 1
    assert pick_hit(hits, "最后一个").x == 3
    assert pick_hit(hits, "随机").x in (1, 2, 3)
    assert pick_hit([], "第一个") is None


def test_drag_moves_in_steps_while_pressed():
    """拖拽必须分步移动：一步跳到终点的话，游戏只看到光标瞬移，不认拖拽。"""
    from tasks.builtin import MouseActionTask

    class M(_Mouse):
        def __init__(self):
            super().__init__()
            self.moves = []

        def move_to(self, pt, button=None):
            self.moves.append((tuple(pt), button))

    class P(_Player):
        def __init__(self):
            super().__init__()
            self.mouse = M()

    ctx = _Ctx({"action": "drag", "anchor": "absolute", "x": 100, "y": 400,
                "drag_dx": 0, "drag_dy": -200, "drag_steps": 4, "drag_ms": 40})
    ctx.player = P()
    MouseActionTask().run(ctx)
    assert ctx.player.calls == [("glide", (100, 400))]
    assert ctx.player.mouse.presses == ["left"] and ctx.player.mouse.releases == ["left"]
    assert len(ctx.player.mouse.moves) == 4, "要分 4 步移动"
    # 按键保持期间必须发 Dragged 类型，否则应用只看到光标在动
    assert all(btn == "left" for _pt, btn in ctx.player.mouse.moves)
    assert ctx.player.mouse.moves[-1][0] == (100, 200), "终点 = 起点 + 位移"


def test_drag_releases_even_if_stopped():
    """中途停止也要松开按键，否则鼠标会一直处于按下状态。"""
    from tasks.builtin import MouseActionTask

    class M(_Mouse):
        def __init__(self):
            super().__init__()
            self.moves = []

        def move_to(self, pt, button=None):
            self.moves.append((tuple(pt), button))

    class P(_Player):
        def __init__(self):
            super().__init__()
            self.mouse = M()

    ctx = _Ctx({"action": "drag", "anchor": "absolute", "x": 10, "y": 20,
                "drag_dy": -50, "drag_steps": 4, "drag_ms": 40})
    ctx.player = P()
    ctx.player.stopping = True   # 一开始就停
    MouseActionTask().run(ctx)
    assert ctx.player.mouse.releases == ["left"]
