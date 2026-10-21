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
