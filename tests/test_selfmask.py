"""把自己窗口从截图里挖掉（core/selfmask）的用例。

背景：真机上出现过「操作穿透了游戏，点到后面的桌面」——原因是全屏 OCR 读到了
Auto Flow 自己面板上的文字（运行轨迹里就写着「点『继续』」），而运行期间主窗是
鼠标穿透的。遮罩必须在**匹配之前**生效，否则命中点会落在自己窗口上。
"""
import numpy as np

from core import selfmask, vision


class _Cap:
    def __init__(self, image, scale=1.0, left=0, top=0):
        self.image = image
        self.scale = scale
        self.left = left
        self.top = top


def _patch_rects(monkeypatch, rects):
    selfmask.reset_cache()
    monkeypatch.setattr(selfmask, "own_window_rects", lambda force=False: list(rects))


def test_masks_own_window_pixels(monkeypatch):
    _patch_rects(monkeypatch, [(10, 20, 30, 40)])
    cap = _Cap(np.full((100, 200, 3), 255, np.uint8))
    n = selfmask.mask_capture(cap)
    assert n == 1
    assert cap.image[20:60, 10:40].max() == 0, "自身窗口区域没被涂黑"
    assert cap.image[0, 0].min() == 255, "窗口之外的像素不该被动"
    assert cap.image[90, 190].min() == 255


def test_mask_respects_retina_scale_and_display_origin(monkeypatch):
    # 逻辑坐标 (100,50) 起、在 2x 屏上、该屏原点 (0,0) → 物理像素 (200,100) 起
    _patch_rects(monkeypatch, [(100, 50, 20, 10)])
    cap = _Cap(np.full((400, 800, 3), 255, np.uint8), scale=2.0, left=0, top=0)
    selfmask.mask_capture(cap)
    assert cap.image[100:120, 200:240].max() == 0
    assert cap.image[99, 199].min() == 255


def test_no_rects_is_a_noop(monkeypatch):
    _patch_rects(monkeypatch, [])
    cap = _Cap(np.full((10, 10, 3), 7, np.uint8))
    assert selfmask.mask_capture(cap) == 0
    assert cap.image.min() == 7


def test_point_in_own_window(monkeypatch):
    _patch_rects(monkeypatch, [(10, 20, 30, 40)])
    assert selfmask.point_in_own_window(10, 20) is True
    assert selfmask.point_in_own_window(39, 59) is True
    assert selfmask.point_in_own_window(40, 60) is False
    assert selfmask.point_in_own_window(0, 0) is False


def test_captures_returns_masked_images(monkeypatch):
    """captures() 出来的图必须已经是遮过的——下游（模板匹配/OCR）不该再自己记得遮。"""
    _patch_rects(monkeypatch, [(5, 5, 10, 10)])
    raw_img = np.full((40, 40, 3), 200, np.uint8)
    monkeypatch.setattr(vision, "_raw_screens",
                        lambda: [({"width": 40, "height": 40, "left": 0, "top": 0}, raw_img)])
    caps = vision.captures()
    assert len(caps) == 1
    assert caps[0].image[5:15, 5:15].max() == 0, "captures() 没把自身窗口遮掉"
    assert caps[0].image[0, 0].min() == 200


# ---- 遮挡关系（真机事故：把游戏一起涂黑了）----
#
# 事故现场：Auto Flow 窗口 (90,34,1100x720)、游戏窗口 (4,98,800x482) 在它**前面**。
# 早期实现按「窗口矩形」整块涂黑，于是游戏画面 74% 被涂掉，「开始匹配」明明在屏上
# 却认不出来，脚本在大厅里空转。正确做法是减掉挡在前面的窗口。


def test_fully_covered_window_is_not_masked():
    """自己的窗口整个被前面的窗口盖住 → 什么都不该遮。"""
    rects = selfmask.rects_from_windows([
        {"owner": "燕云十六声", "x": 0, "y": 0, "w": 1280, "h": 832},
        {"owner": "Auto Flow", "x": 90, "y": 34, "w": 1100, "h": 720},
    ])
    assert rects == [], "被完全盖住的窗口不该产生遮罩"


def test_occluding_window_carves_the_mask():
    """游戏窗口挡在前面时，只有**露出来**的那部分被遮。"""
    rects = selfmask.rects_from_windows([
        {"owner": "燕云十六声", "x": 4, "y": 98, "w": 800, "h": 482},
        {"owner": "Auto Flow", "x": 90, "y": 34, "w": 1100, "h": 720},
    ])
    assert rects, "自己的窗口还有露出来的部分，应该产生遮罩"
    area = sum(w * h for _, _, w, h in rects)
    # 1100*720 = 792000；被挡掉的部分 = (800-90)*(482-98)… 这里只断言「明显小于整块」
    assert area < 792000 * 0.75, "遮罩面积没减去前面的窗口"
    # 游戏窗口中心绝不能落在任何遮罩块里
    gx, gy = 400, 300
    for (x, y, w, h) in rects:
        assert not (x <= gx < x + w and y <= gy < y + h), "游戏画面被涂黑了"


def test_own_window_behind_nothing_is_fully_masked():
    """没有别的窗口挡着时，自己整块都要遮。"""
    rects = selfmask.rects_from_windows([
        {"owner": "Auto Flow", "x": 10, "y": 10, "w": 100, "h": 50},
    ])
    assert rects == [(10, 10, 100, 50)]


def test_ignores_transparent_and_nonzero_layer_windows():
    """透明窗口 / 菜单栏那层（layer≠0）既不算遮挡，也不产生遮罩。"""
    rects = selfmask.rects_from_windows([
        {"owner": "控制中心", "layer": 25, "x": 0, "y": 0, "w": 1280, "h": 29},
        {"owner": "某悬浮窗", "alpha": 0.0, "x": 0, "y": 0, "w": 1280, "h": 832},
        {"owner": "Auto Flow", "x": 0, "y": 30, "w": 200, "h": 100},
    ])
    assert rects == [(0, 30, 200, 100)], "透明窗口/菜单栏不该参与遮挡计算"


def test_minus_returns_original_when_no_overlap():
    assert selfmask._minus((0, 0, 10, 10), (50, 50, 5, 5)) == [(0, 0, 10, 10)]


def test_minus_splits_into_at_most_four_pieces():
    pieces = selfmask._minus((0, 0, 100, 100), (25, 25, 50, 50))
    assert len(pieces) == 4
    assert sum(w * h for _, _, w, h in pieces) == 100 * 100 - 50 * 50


# ---- 按窗口几何点击（图标类按钮没有文字，只能这么点）----


def test_front_window_skips_our_own_windows():
    win = selfmask.front_window_from_windows([
        {"owner": "控制中心", "layer": 25, "x": 0, "y": 0, "w": 1280, "h": 29},
        {"owner": "Auto Flow", "x": 90, "y": 34, "w": 1100, "h": 720},
        {"owner": "燕云十六声", "x": 4, "y": 98, "w": 800, "h": 482},
    ])
    assert win == (4, 98, 800, 482), "应该拿到游戏窗口，而不是自己的窗口/菜单栏"


def test_front_window_none_when_only_we_are_visible():
    assert selfmask.front_window_from_windows([
        {"owner": "Auto Flow", "x": 0, "y": 0, "w": 100, "h": 100},
    ]) is None


def test_front_window_ignores_tiny_overlay_windows():
    """结算动画期间最前面会冒出一个贴在 (0,0) 的小窗口——按它算坐标会点到苹果菜单上。"""
    win = selfmask.front_window_from_windows([
        {"owner": "燕云十六声", "x": 0, "y": 0, "w": 12, "h": 12},     # 过渡遮罩
        {"owner": "燕云十六声", "x": 4, "y": 98, "w": 800, "h": 482},  # 真窗口
    ])
    assert win == (4, 98, 800, 482)


def test_front_window_ignores_system_panels():
    win = selfmask.front_window_from_windows([
        {"owner": "控制中心", "x": 0, "y": 0, "w": 400, "h": 300},
        {"owner": "Auto Flow", "x": 0, "y": 0, "w": 1100, "h": 700},
    ])
    assert win is None, "系统面板/自己的窗口都不能当锚点"


def test_resolve_anchor_point_variants():
    from tasks.builtin import resolve_anchor_point
    win = (100, 50, 800, 482)
    # 右上角 + 偏移（游戏里「退出」图标就在这个位置：距右边 ~20、距顶 ~46）
    assert resolve_anchor_point("win_tr", -20, 46, 0, 0, win) == (880, 96)
    assert resolve_anchor_point("win_tl", 10, 20, 0, 0, win) == (110, 70)
    assert resolve_anchor_point("base", 5, 6, 1000, 2000, win) == (1005, 2006)
    assert resolve_anchor_point("absolute", 300, 400, 1000, 2000, win) == (300, 400)
    # 右下角：结算页的「Space 继续」就在窗口右下角往里约 (-29, -30)
    assert resolve_anchor_point("win_br", -29, -30, 0, 0, win) == (871, 502)
    assert resolve_anchor_point("win_bl", 29, -30, 0, 0, win) == (129, 502)


def test_anchor_point_outside_window_is_refused():
    """算出来的点落在窗口外 → 返回 None（调用方跳过点击，绝不瞎点）。"""
    from tasks.builtin import resolve_anchor_point
    win = (4, 98, 800, 482)
    assert resolve_anchor_point("win_br", -2000, -30, 0, 0, win) is None
    assert resolve_anchor_point("win_tr", -5000, 46, 0, 0, win) is None
    # 窗口内就正常
    assert resolve_anchor_point("win_br", -29, -30, 0, 0, win) == (775, 550)


def test_window_anchor_falls_back_to_offset_when_window_unknown():
    """读不到窗口时不能算出个「相对原点」的坐标去瞎点——这里退回纯偏移，
    真正的保护在 MouseActionTask：拿不到窗口就跳过点击。"""
    from tasks.builtin import resolve_anchor_point
    assert resolve_anchor_point("win_tr", -20, 46, 0, 0, None) == (-20, 46)
