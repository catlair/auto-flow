"""多显示器截图与坐标换算（全部用假显示器，不碰真实屏幕）。

为什么必须有这组测试：`core/vision.py` 的坐标有三层——全局逻辑点 /
显示器局部逻辑点 / 图像物理像素——换算写错时**不报错**，只是点歪或
「找不到」。多屏 + 混合 Retina 无法在 headless 环境复现，只能靠假显示器
把换算关系钉死。

假显示器规格刻意做成混合 Retina：左屏 1280x832@1x、右屏 1440x900@2x。
旧实现用**主屏**逻辑宽度当每块屏 scale 的分母（1440/1280 = 1.125），
在这组数据上会算出错误比例，正好能抓住这个回归。

模板一律用固定种子的随机图案，不用纯色块：纯色模板方差为 0，
`TM_CCOEFF_NORMED` 是退化输入（0/0），结果不可预期。
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from core import vision
from core.yolo import Detection

# 右屏上模板中心的全局逻辑坐标：像素 (1420, 810) / scale 2 + left 1280 = (1990, 405)
RIGHT_TPL_GLOBAL = (1990, 405)


def _blank(w: int, h: int, color=(0, 0, 0)) -> np.ndarray:
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = color
    return img


def _pattern(w: int, h: int, seed: int) -> np.ndarray:
    """固定种子的噪声图案：有纹理，归一化互相关才有定义。"""
    return np.random.default_rng(seed).integers(0, 256, size=(h, w, 3), dtype=np.uint8)


@pytest.fixture
def two_screens(monkeypatch):
    """混合 Retina 双屏，模板只贴在右屏。返回 (模板图, 期望全局坐标)。"""
    tpl = _pattern(40, 20, seed=7)
    left = _blank(1280, 832)
    right = _blank(2880, 1800)              # 物理像素 = 逻辑点 x2
    right[800:820, 1400:1440] = tpl         # 右屏局部逻辑 (700, 400) 起
    raws = [
        ({"left": 0, "top": 0, "width": 1280, "height": 832}, left),
        ({"left": 1280, "top": 0, "width": 1440, "height": 900}, right),
    ]
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    return tpl, RIGHT_TPL_GLOBAL


# --------------------------------------------------------------------------- #
# 截图枚举
# --------------------------------------------------------------------------- #
def test_captures_skip_composite_and_keep_global_origin(two_screens):
    """每块屏各一份 capture，left/top 是全局逻辑坐标。"""
    caps = vision.captures()
    assert [(c.left, c.top) for c in caps] == [(0, 0), (1280, 0)]
    assert [(c.image.shape[1], c.image.shape[0]) for c in caps] == [(1280, 832), (2880, 1800)]


def test_scale_comes_from_each_display_not_the_main_one(two_screens):
    """回归：scale 必须用被截那块屏自己的宽度算。

    旧实现拿主屏逻辑宽度当分母，右屏会算成 1440/1280 = 1.125（错），
    正确值是 2880/1440 = 2.0。混合 Retina 双屏下点歪的根因。
    """
    caps = vision.captures()
    assert caps[0].scale == pytest.approx(1.0)
    assert caps[1].scale == pytest.approx(2.0)


def test_logical_size_undoes_scale(two_screens):
    caps = vision.captures()
    assert caps[0].logical_size == (1280, 832)
    assert caps[1].logical_size == (1440, 900)


def test_main_capture_uses_quartz_origin_not_list_order(monkeypatch):
    """主屏按「原点在 (0,0)」识别，不依赖 mss 的枚举顺序。"""
    second = _blank(800, 600)
    primary = _blank(1280, 832)
    raws = [
        ({"left": 800, "top": 0, "width": 800, "height": 600}, second),
        ({"left": 0, "top": 0, "width": 1280, "height": 832}, primary),
    ]
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    assert vision.main_capture().image.shape[:2] == (832, 1280)


def test_grab_screen_bgr_still_returns_main_display_only(two_screens):
    img, scale = vision.grab_screen_bgr()
    assert img.shape[:2] == (832, 1280)
    assert scale == pytest.approx(1.0)


def test_captures_fall_back_to_quartz_when_mss_fails(monkeypatch):
    sentinel = vision.ScreenCapture(image=_blank(10, 10), scale=1.0)

    def boom():
        raise OSError("锁屏 / 休眠")

    monkeypatch.setattr(vision, "_raw_screens", boom)
    monkeypatch.setattr(vision, "_quartz_capture", lambda: sentinel)
    assert vision.captures() == [sentinel]


def test_real_screen_capture_is_blocked_by_default():
    """conftest 默认禁止真实截屏。

    忘写假显示器的用例必须在这里明确报错，而不是去截用户当时的桌面——
    那会弹 TCC 授权、在 headless 下阻塞，并让结果绑定到桌面内容上。
    """
    with pytest.raises(RuntimeError, match="禁止真实截屏"):
        vision.captures()


# --------------------------------------------------------------------------- #
# 模板匹配：跨屏 + 全局坐标
# --------------------------------------------------------------------------- #
def test_find_template_reports_global_coords_on_second_display(two_screens):
    """模板在副屏时，返回的坐标要带上那块屏的偏移。"""
    tpl, expected = two_screens
    m = vision.find_template(0.9, template_bgr=tpl)
    assert m.found
    assert (m.x, m.y) == expected


def test_find_template_still_works_on_single_display(monkeypatch):
    """单屏行为不变：偏移为 0，坐标就是图内像素 / scale。"""
    tpl = _pattern(40, 20, seed=11)
    screen = _blank(1280, 832)
    screen[100:120, 200:240] = tpl
    monkeypatch.setattr(vision, "_raw_screens",
                        lambda: [({"left": 0, "top": 0, "width": 1280, "height": 832}, screen)])
    m = vision.find_template(0.9, template_bgr=tpl)
    assert (m.x, m.y) == (220, 110)


def test_find_template_not_present_reports_best_score_below_threshold(two_screens):
    """屏幕上没有的目标：found=False，但仍回传最高分供排查。"""
    absent = _pattern(40, 20, seed=99)
    m = vision.find_template(0.9, template_bgr=absent)
    assert not m.found
    assert 0.0 <= m.score < 0.9


def test_find_template_skips_template_larger_than_screen(monkeypatch):
    """模板比屏幕还大时 matchTemplate 会抛错，必须跳过而不是崩掉。"""
    monkeypatch.setattr(vision, "_raw_screens",
                        lambda: [({"left": 0, "top": 0, "width": 1280, "height": 832},
                                  _blank(1280, 832))])
    m = vision.find_template(0.8, template_bgr=_blank(2000, 2000))
    assert not m.found


def test_find_template_missing_file_returns_not_found():
    assert not vision.find_template(0.8, template_path="/nonexistent/tpl.png").found


# --------------------------------------------------------------------------- #
# 区域裁剪
# --------------------------------------------------------------------------- #
def test_grab_region_none_returns_main_screen(two_screens):
    cap = vision.grab_region_bgr(None)
    assert cap.image.shape[:2] == (832, 1280)
    assert (cap.left, cap.top) == (0, 0)


def test_grab_region_on_second_display_records_global_origin(monkeypatch):
    """区域落在副屏：裁剪尺寸按该屏 scale 换算，原点记为全局坐标。"""
    right = _blank(2880, 1800, (7, 7, 7))
    raws = [({"left": 1280, "top": 0, "width": 1440, "height": 900}, right)]
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    cap = vision.grab_region_bgr((1400, 100, 200, 100))     # 全局逻辑
    assert (cap.left, cap.top) == (1400, 100)
    assert cap.image.shape[:2] == (200, 400)                # 100x200 逻辑 @2x
    assert int(cap.image[0, 0, 0]) == 7
    # 裁剪图内的点能换算回全局
    assert cap.to_global(400, 200) == (1600, 200)


def test_grab_region_picks_the_display_with_largest_overlap(monkeypatch):
    """跨屏区域只取重叠面积最大的那块屏上的部分。"""
    raws = [
        ({"left": 0, "top": 0, "width": 1280, "height": 832}, _blank(1280, 832, (1, 1, 1))),
        ({"left": 1280, "top": 0, "width": 1440, "height": 900}, _blank(2880, 1800, (2, 2, 2))),
    ]
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    # 横跨边界，右屏占 300 宽、左屏占 100 宽 → 取右屏
    cap = vision.grab_region_bgr((1180, 0, 400, 100))
    assert (cap.left, cap.top) == (1280, 0)
    assert cap.image.shape[:2] == (200, 600)                # 300x100 逻辑 @2x


def test_grab_region_entirely_off_screen_returns_empty(monkeypatch):
    monkeypatch.setattr(vision, "_raw_screens",
                        lambda: [({"left": 0, "top": 0, "width": 1280, "height": 832},
                                  _blank(1280, 832))])
    cap = vision.grab_region_bgr((5000, 5000, 100, 100))
    assert cap.image.size == 0


def test_grab_region_clamps_negative_origin(monkeypatch):
    """负坐标区域（副屏在主屏左侧）不能被裁成错位图。"""
    raws = [({"left": -1600, "top": 0, "width": 1600, "height": 1000},
             _blank(1600, 1000, (3, 3, 3)))]
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    cap = vision.grab_region_bgr((-1500, 100, 200, 100))
    assert (cap.left, cap.top) == (-1500, 100)
    assert cap.image.shape[:2] == (100, 200)


# --------------------------------------------------------------------------- #
# OCR / YOLO 的坐标换算
# --------------------------------------------------------------------------- #
def test_ocr_box_center_includes_display_origin():
    """Vision 归一化框（左下原点）→ 全局逻辑坐标，且含副屏偏移。"""
    from core import ocr

    cap = vision.ScreenCapture(image=_blank(2880, 1800), scale=2.0, left=1280, top=0)
    box = SimpleNamespace(origin=SimpleNamespace(x=0.1, y=0.2),
                          size=SimpleNamespace(width=0.2, height=0.1))
    # cx = 1280 + (0.1+0.1)*2880/2 = 1568；cy = (1-(0.2+0.05))*1800/2 = 675
    assert ocr._center_from_box(box, 2880, 1800, cap) == (1568, 675)


def test_ocr_empty_capture_returns_no_hits():
    from core import ocr

    cap = vision.ScreenCapture(image=np.zeros((0, 0, 3), np.uint8), scale=1.0)
    assert ocr.recognize_texts(cap) == []


def test_yolo_detections_are_translated_by_display_origin(two_screens):
    """YOLO 检测结果要加上所在屏的 left/top，否则点击会落在主屏上。"""
    from tasks.builtin import detect_on_all_screens

    class FakeEngine:
        def detect_bgr(self, bgr, scale, conf):
            return [Detection(label="person", confidence=0.9, x=10, y=20, w=4, h=4)]

    dets = detect_on_all_screens(FakeEngine(), 0.5)
    assert [(d.x, d.y) for d in dets] == [(10, 20), (1290, 20)]


def test_yolo_detections_sorted_by_confidence_across_screens(monkeypatch):
    """跨屏结果按置信度统一排序，命中序号（第 N 个）才有意义。"""
    from tasks.builtin import detect_on_all_screens

    raws = [
        ({"left": 0, "top": 0, "width": 100, "height": 100}, _blank(100, 100)),
        ({"left": 100, "top": 0, "width": 100, "height": 100}, _blank(100, 100)),
    ]
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    confs = iter([0.3, 0.8])

    class FakeEngine:
        def detect_bgr(self, bgr, scale, conf):
            return [Detection(label="x", confidence=next(confs), x=0, y=0, w=1, h=1)]

    dets = detect_on_all_screens(FakeEngine(), 0.5)
    assert [d.confidence for d in dets] == [0.8, 0.3]
    assert dets[0].x == 100          # 高置信度那个在副屏，坐标已带偏移
