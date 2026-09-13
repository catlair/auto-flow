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
                                  _pattern(1280, 832, seed=5))])
    m = vision.find_template(0.8, template_bgr=_pattern(2000, 2000, seed=6))
    assert not m.found


def test_find_template_missing_file_returns_not_found():
    assert not vision.find_template(0.8, template_path="/nonexistent/tpl.png").found


# --------------------------------------------------------------------------- #
# 纯色（退化输入）防护
#
# 背景：纯色图让 TM_CCOEFF_NORMED 变成 0/0 的退化输入，而 OpenCV 的结果
# **不确定**——实测同一个「纯色屏 + 纯色 40x60 模板」，有的进程跑出
# max=1.0 @ (0,0)（满分的假命中），有的跑出 0.0。真机上就复现过一次
# found=True score=1.0000 落在 (0,0)，即工作流会点向屏幕左上角且不报错。
#
# 正因为不确定，用「行为」断言去反向验证这条路不可靠（删掉防护后可能碰巧
# 还是 0.0）。所以：谓词与诊断各有一个确定性用例，行为用例只作兜底。
# --------------------------------------------------------------------------- #
def test_is_flat_detects_uniform_images():
    """谓词本身：纯色/空图为真，有纹理为假（确定性用例，可反向验证）。"""
    assert vision._is_flat(_blank(60, 40, (200, 200, 200)))
    assert vision._is_flat(np.zeros((0, 0, 3), np.uint8))
    assert not vision._is_flat(_pattern(60, 40, seed=5))


def test_find_template_rejects_flat_template(monkeypatch):
    """纯色模板必须报「找不到」。

    兜底用例：OpenCV 的退化输出不确定，这个断言在某些进程里不加防护也能过；
    真正保证行为的是上面的谓词用例。
    """
    monkeypatch.setattr(vision, "_raw_screens",
                        lambda: [({"left": 0, "top": 0, "width": 1280, "height": 832},
                                  _pattern(1280, 832, seed=5))])
    flat_tpl = np.full((40, 60, 3), 200, np.uint8)
    m = vision.find_template(0.8, template_bgr=flat_tpl)
    assert not m.found
    assert m.score == 0.0


def test_flat_screen_is_skipped_and_warned_once(monkeypatch, caplog):
    """全均匀的截图（典型成因：没给屏幕录制权限）跳过并留一次诊断。

    返回值上与「匹配不到」无法区分，所以这里断言**诊断行为**：权限缺失此前
    只表现为「节点找不到目标」，用户无从判断是权限问题。这个用例是确定性的，
    删掉防护后不会有任何日志 → 必然失败。
    """
    monkeypatch.setattr(vision, "_raw_screens",
                        lambda: [({"left": 0, "top": 0, "width": 1280, "height": 832},
                                  _blank(1280, 832, (30, 30, 30)))])
    monkeypatch.setattr(vision, "_warned", set())
    tpl = _pattern(40, 20, seed=9)
    with caplog.at_level("WARNING", logger="core.vision"):
        assert not vision.find_template(0.8, template_bgr=tpl).found
        assert not vision.find_template(0.8, template_bgr=tpl).found
    warnings = [r for r in caplog.records if "屏幕录制" in r.getMessage()]
    assert len(warnings) == 1, "纯色屏幕的诊断只应记一次，否则重试循环会刷屏"


def test_broken_flat_display_does_not_block_the_working_one(monkeypatch):
    """一块屏纯色（比如没权限的那块）、另一块正常时，仍要在正常那块上找到。"""
    tpl = _pattern(40, 20, seed=13)
    good = _blank(1280, 832)
    good[300:320, 500:540] = tpl
    raws = [
        ({"left": 0, "top": 0, "width": 1280, "height": 832}, _blank(1280, 832, (0, 0, 0))),
        ({"left": 1280, "top": 0, "width": 1280, "height": 832}, good),
    ]
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    m = vision.find_template(0.9, template_bgr=tpl)
    assert m.found
    assert (m.x, m.y) == (1280 + 520, 310)


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


# --------------------------------------------------------------------------- #
# 截屏密度：模板必须与截屏同像素密度
#
# 背景（2026-09-13 真机定位）：「截图识别没有效果」的真凶。mss 10.2 在 darwin
# 上默认带 kCGWindowImageNominalResolution，Retina 屏拿回的是**逻辑尺寸**
# （物理尺寸的一半）；而「截取模板」走 screencapture，产出的是**物理像素**。
# 模板于是比屏上目标大一倍，分数从 1.0 掉到 0.5 上下、永远过不了阈值，
# 而且全程无日志。实测：2x 模板对 1x 屏 = 0.53；缩到同密度 = 0.9998。
# --------------------------------------------------------------------------- #
def _write_png(path, img: np.ndarray, ppm: int | None) -> str:
    """把给定图像写成 PNG；`ppm=None` 时不写 pHYs（未声明密度）。

    144dpi 对应 ppm=5669、72dpi 对应 ppm=2835（`screencapture` 就按这两种写）。
    """
    import struct
    import zlib

    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    h, w = img.shape[:2]
    # PNG 的颜色类型 2 是 **RGB**，而 numpy 图是 BGR：必须翻一下通道再写，
    # 否则 cv2.imread 读回来会把 R/B 对调，自匹配分数从 1.0 掉到 0.33。
    raw = b"".join(b"\x00" + img[y][:, ::-1].tobytes() for y in range(h))
    body = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    if ppm is not None:
        body += chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1))
    body += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + body)
    return str(path)


def _png_with_dpi(path, ppm: int | None) -> str:
    """写一张 64x64 的最小 PNG；`ppm=None` 时不写 pHYs（未声明密度）。"""
    size = 64
    flat = np.array([(x * 4) % 256 for x in range(size * 3)], np.uint8)
    img = np.tile(flat.reshape(size, 3), (size, 1, 1))
    return _write_png(path, img, ppm)


def _one_plain_screen(seed: int = 21):
    """一块 1280x832 的假屏：图宽=逻辑宽 → scale=1.0（即「1x 屏」）。"""
    return [({"left": 0, "top": 0, "width": 1280, "height": 832},
             _pattern(1280, 832, seed=seed))]


def test_png_helper_writes_a_real_decodable_png(tmp_path):
    """辅助函数本身要诚实：产出必须是真的 PNG，否则后面的密度用例是自说自话。"""
    import cv2

    img = cv2.imread(_png_with_dpi(tmp_path / "t.png", 5669))
    assert img is not None and img.shape[:2] == (64, 64)


def test_mss_asks_for_physical_resolution():
    """截屏必须请求**物理像素**，否则与 screencapture 的模板差一倍密度。

    确定性用例：`IMAGE_OPTIONS` 里只要出现 NominalResolution 位就是回归。
    """
    mss_darwin = pytest.importorskip("mss.darwin")
    nominal = mss_darwin.kCGWindowImageNominalResolution
    assert not (mss_darwin.IMAGE_OPTIONS & nominal), (
        "mss 又被设回「名义分辨率」：Retina 上截图只有物理尺寸的一半，"
        "与 screencapture 截出的模板密度不一致，匹配分数会整体砍半")


def test_png_density_reads_phys_chunk(tmp_path):
    """144dpi→2x、72dpi→1x、未声明/不存在→None。"""
    assert vision._png_density(_png_with_dpi(tmp_path / "retina.png", 5669)) \
        == pytest.approx(2.0, abs=0.01)
    assert vision._png_density(_png_with_dpi(tmp_path / "plain.png", 2835)) \
        == pytest.approx(1.0, abs=0.01)
    assert vision._png_density(_png_with_dpi(tmp_path / "nodpi.png", None)) is None
    assert vision._png_density(str(tmp_path / "不存在.png")) is None


def test_density_mismatch_is_reported_once(monkeypatch, caplog, tmp_path):
    """模板密度与截屏不一致时必须点名原因——这是「静默失败」的典型。

    确定性：屏幕固定为 1x（图宽=逻辑宽），模板声明 2x，比值必然不等。
    """
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_raw_screens", lambda: _one_plain_screen())
    tpl = _png_with_dpi(tmp_path / "retina.png", 5669)
    with caplog.at_level("WARNING", logger="core.vision"):
        assert not vision.find_template(1.0, template_path=tpl).found
        assert not vision.find_template(1.0, template_path=tpl).found
    hits = [r for r in caplog.records if "像素密度" in r.getMessage()]
    assert len(hits) == 1, "重试循环每 400ms 一轮，密度告警不能刷屏"


def test_no_density_warning_when_densities_agree(monkeypatch, caplog, tmp_path):
    """密度一致时不能误报——否则用户会被引向错误方向（反向验证的配对用例）。"""
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_raw_screens", lambda: _one_plain_screen())
    tpl = _png_with_dpi(tmp_path / "plain.png", 2835)      # 1x 模板对 1x 屏
    with caplog.at_level("WARNING", logger="core.vision"):
        assert not vision.find_template(1.0, template_path=tpl).found
    assert not [r for r in caplog.records if "像素密度" in r.getMessage()]


def test_missing_template_file_is_reported_once(monkeypatch, caplog, tmp_path):
    """模板文件没了要留话：返回值上与「屏上真没有」完全一样，静默就没法排查。"""
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_raw_screens", lambda: _one_plain_screen())
    missing = str(tmp_path / "nope.png")
    with caplog.at_level("WARNING", logger="core.vision"):
        assert not vision.find_template(0.8, template_path=missing).found
        assert not vision.find_template(0.8, template_path=missing).found
    hits = [r for r in caplog.records if "读不出来" in r.getMessage()]
    assert len(hits) == 1


def test_flat_template_is_reported(monkeypatch, caplog):
    """纯色模板（没有屏幕录制权限时截出来的）此前是静默「找不到」，现在要留话。"""
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_raw_screens", lambda: _one_plain_screen())
    flat = np.full((40, 60, 3), 200, np.uint8)
    with caplog.at_level("WARNING", logger="core.vision"):
        assert not vision.find_template(0.8, template_bgr=flat).found
    assert any("近似纯色" in r.getMessage() and "模板" in r.getMessage()
               for r in caplog.records)


# --------------------------------------------------------------------------- #
# 模板密度自动对齐（2026-09-13 真机实测后加）
#
# 背景：屏幕分辨率换到另一个**密度组**（2.0x ↔ 1.0x）时，原模板与截屏密度差
# 一倍。真机实测 2x 模板对 1x 屏最高分 **0.7738**，而且落在**错误位置**
# （屏幕中部），只比默认阈值 0.8 低一点点——用户把置信度调到 0.77 就会点错
# 地方且不报错。修法是按 `屏幕 scale / 模板密度` 自动缩放模板再匹配。
#
# 真机验证：跨组从 False 0.7738 → True 0.9354（位置经放大截图肉眼确认）；
# 同组 factor=1.0，结果与不重采样完全一致（0.9590），无回归。
# --------------------------------------------------------------------------- #
def _one_1x_screen_with_target(patch_seed: int, top: int = 100, left: int = 200):
    """一块 1280x832 的 1x 假屏，在 (left, top) 贴一个 40x20 的图案目标。"""
    screen = _pattern(1280, 832, seed=31)
    screen[top:top + 20, left:left + 40] = _pattern(40, 20, seed=patch_seed)
    raws = [({"left": 0, "top": 0, "width": 1280, "height": 832}, screen)]
    return screen, raws


def _template_2x(screen, top=100, left=200):
    """把屏上那块目标放大一倍、并声明 144dpi，模拟「在 2x 屏上截的模板」。

    用 INTER_NEAREST 放大：每个像素变成 2x2 的同值块，于是按 0.5x
    INTER_AREA 缩回来是**逐像素无损**的，分数应接近满分。
    """
    import cv2

    patch = screen[top:top + 20, left:left + 40]
    return cv2.resize(patch, (80, 40), interpolation=cv2.INTER_NEAREST)


def test_density_mismatch_is_auto_resampled_and_matches(monkeypatch, tmp_path):
    """跨密度组的核心保证：模板是 2x、屏幕是 1x 时，自动缩放后应该能匹配上。"""
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_resampled", {})
    screen, raws = _one_1x_screen_with_target(patch_seed=32)
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    path = _write_png(tmp_path / "tpl2x.png", _template_2x(screen), 5669)

    m = vision.find_template(0.9, template_path=path)
    assert m.found, "密度不一致时应自动缩放后匹配成功"
    assert m.adjusted == pytest.approx(0.5), "缩放系数 = 屏幕 1.0x / 模板 2.0x"
    assert (m.x, m.y) == (220, 110), "坐标仍要落在目标中心"
    assert m.score > 0.99, "往返缩放无损，分数应接近满分"


def test_without_alignment_a_2x_template_would_miss(monkeypatch, tmp_path):
    """反向验证上面那条：同一组数据、关掉密度识别，就必须匹配不上。

    没有这条对照，`test_density_mismatch_is_auto_resampled_and_matches`
    可能只是碰巧通过，无法证明「自动缩放」真的起了作用。
    """
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_resampled", {})
    screen, raws = _one_1x_screen_with_target(patch_seed=32)
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    path = _write_png(tmp_path / "tpl2x.png", _template_2x(screen), 5669)
    # 假装模板没声明密度 → 不做缩放，退回修复前的行为
    monkeypatch.setattr(vision, "_png_density", lambda p: None)

    m = vision.find_template(0.9, template_path=path)
    assert not m.found
    assert m.score < 0.9
    assert m.adjusted is None


def test_density_agreement_uses_the_template_as_is(monkeypatch, tmp_path):
    """密度一致时不得重采样——白做功，还会引入插值误差。"""
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_resampled", {})
    screen, raws = _one_1x_screen_with_target(patch_seed=33, top=300, left=400)
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    path = _write_png(tmp_path / "tpl1x.png",
                      screen[300:320, 400:440], 2835)      # 1x 模板对 1x 屏

    m = vision.find_template(0.9, template_path=path)
    assert m.found and m.adjusted is None
    assert (m.x, m.y) == (420, 310)
    assert vision._resampled == {}, "密度一致时不该产生重采样缓存"


def test_resampled_template_is_cached(monkeypatch, tmp_path):
    """重采样结果必须缓存：匹配在重试循环里每 400ms 一轮，每轮都 resize 是浪费。"""
    import cv2

    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_resampled", {})
    screen, raws = _one_1x_screen_with_target(patch_seed=34)
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    path = _write_png(tmp_path / "tpl2x.png", _template_2x(screen), 5669)

    calls = []
    real_resize = cv2.resize
    monkeypatch.setattr(cv2, "resize",
                        lambda *a, **k: (calls.append(1), real_resize(*a, **k))[1])
    for _ in range(3):
        assert vision.find_template(0.9, template_path=path).found
    assert len(calls) == 1, "三轮回调只该真正 resize 一次"


def test_resampled_but_absent_reports_density_reason(monkeypatch, caplog, tmp_path):
    """缩放后仍找不到时，reason 要点明密度不一致，免得用户以为缩放没生效。"""
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_resampled", {})
    monkeypatch.setattr(vision, "_raw_screens", lambda: _one_plain_screen())
    absent = _pattern(80, 40, seed=37)                      # 屏上根本没有这个图案
    path = _write_png(tmp_path / "absent2x.png", absent, 5669)

    with caplog.at_level("WARNING", logger="core.vision"):
        m = vision.find_template(0.9, template_path=path)
    assert not m.found
    assert m.adjusted == pytest.approx(0.5)
    assert "密度" in m.reason
    assert any("像素密度" in r.getMessage() for r in caplog.records)


def test_in_memory_template_is_never_resampled(monkeypatch):
    """内存模板没有 pHYs，密度无从得知 ⇒ 不做缩放（调用方自己保证密度一致）。"""
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_resampled", {})
    tpl = _pattern(40, 20, seed=38)
    screen = _blank(1280, 832)
    screen[50:70, 60:100] = tpl
    monkeypatch.setattr(vision, "_raw_screens",
                        lambda: [({"left": 0, "top": 0, "width": 1280, "height": 832}, screen)])

    m = vision.find_template(0.9, template_bgr=tpl)
    assert m.found and m.adjusted is None
    assert vision._resampled == {}


def test_auto_resample_is_announced_once(monkeypatch, caplog, tmp_path):
    """自动缩放成功后要留一句 INFO 说明，否则「换了分辨率居然还能用」像玄学。"""
    monkeypatch.setattr(vision, "_warned", set())
    monkeypatch.setattr(vision, "_resampled", {})
    screen, raws = _one_1x_screen_with_target(patch_seed=39)
    monkeypatch.setattr(vision, "_raw_screens", lambda: raws)
    path = _write_png(tmp_path / "tpl2x.png", _template_2x(screen), 5669)

    with caplog.at_level("INFO", logger="core.vision"):
        for _ in range(3):
            assert vision.find_template(0.9, template_path=path).found
    hits = [r for r in caplog.records if "自动缩放" in r.getMessage()]
    assert len(hits) == 1, "同一张模板只说明一次，不能每轮刷屏"
    assert hits[0].levelname == "INFO", "这是说明不是告警，别标成 WARNING"
