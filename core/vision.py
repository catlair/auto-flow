"""屏幕截图与模板匹配（多显示器 + Retina 适配）。

这里有**三层坐标系**，混了就会点偏（而且不报错，只是点歪）：

1. **全局逻辑坐标（点）**——pynput / CGEvent 点击用的坐标。原点在主显示器
   左上角，x 向右、y 向下。mss 的 `monitor["left"/"top"]` 就在这一层：
   darwin 实现直接取 `CGDisplayBounds` 的 origin，单位是点。
2. **显示器局部逻辑坐标（点）**——全局坐标减去该显示器的 `left/top`。
3. **图像物理像素坐标**——`sct.grab()` 返回的 ndarray 下标。

换算关系：`全局点 = 显示器 left/top + 像素 / scale`。

`scale`（物理像素/逻辑点）必须用**被截那块屏自己的宽度**算：
`scale = 图宽 / monitor["width"]`。拿主屏宽度去除是错的——混合
Retina/非 Retina 的双屏下两块屏比例不同，用错就整体点偏。

还有一条**同样不报错、只是点不准/找不到**的约束：模板图必须与截屏同
**像素密度**。截屏走的是物理像素（Retina 上 scale=2），「截取模板」用的
`screencapture` 也是物理像素，两者天然对齐；但 mss 默认会请求「名义分辨率」
（逻辑尺寸），一旦如此，模板就比屏上目标大一倍、分数整体砍半——见
`_prefer_physical_resolution()`。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np
import mss

# mss 10.2 起 `mss.mss` 弃用并改名 `mss.MSS`；两者并存期取新的，老版本回退旧名。
_MSS = getattr(mss, "MSS", None) or mss.mss

logger = logging.getLogger(__name__)


def _prefer_physical_resolution() -> None:
    """让 mss 交回**物理像素**截图，而不是「名义分辨率」。

    mss 10.2 在 darwin 上默认请求 `kCGWindowImageNominalResolution`（见
    `mss/darwin.py` 的 `IMAGE_OPTIONS`）：Retina 屏拿回的是**逻辑尺寸**，
    正好是物理尺寸的一半。而「截取模板」走的是 `screencapture`，产出的是
    **物理像素**。于是模板比屏上目标大一倍，匹配分数掉到 0.5 上下、永远过不了
    阈值——表现就是「截图识别没有效果」，而且全程无日志。

    2026-09-13 实测（Retina 2x，同一目标）：2x 模板对 1x 屏 = 0.53；
    把模板缩到同密度 = 0.9998；2x 模板对 2x 屏 = 1.0。

    去掉该标志位后 `scale` 变成 2.0。坐标换算全部按 scale 除
    （`to_global()`、OCR 的 `_center_from_box`、YOLO 的 `detect_bgr`），
    所以点击落点不受影响；匹配改在原生分辨率上做，也更不容易被相似图案骗到。
    """
    try:
        import mss.darwin as _darwin
    except ImportError:  # pragma: no cover - 非 macOS
        return
    nominal = getattr(_darwin, "kCGWindowImageNominalResolution", 0)
    opts = getattr(_darwin, "IMAGE_OPTIONS", 0)
    if nominal and (opts & nominal):
        _darwin.IMAGE_OPTIONS = opts & ~nominal
        logger.debug("mss 截图改用物理分辨率：IMAGE_OPTIONS %d → %d",
                     opts, _darwin.IMAGE_OPTIONS)


_prefer_physical_resolution()


@dataclass
class ScreenCapture:
    """一块屏的截图，以及它左上角在全局逻辑坐标里的位置。"""

    image: np.ndarray
    scale: float = 1.0
    left: int = 0
    top: int = 0

    def to_global(self, px: float, py: float) -> tuple[int, int]:
        """图内物理像素坐标 → 全局逻辑坐标（点）。"""
        s = self.scale or 1.0
        return (int(round(self.left + px / s)), int(round(self.top + py / s)))

    @property
    def logical_size(self) -> tuple[int, int]:
        """本块屏的逻辑尺寸（点）。"""
        s = self.scale or 1.0
        return (int(round(self.image.shape[1] / s)), int(round(self.image.shape[0] / s)))


@dataclass
class MatchResult:
    found: bool
    x: int = 0          # 全局逻辑坐标（点），目标中心
    y: int = 0
    score: float = 0.0  # 0~1 置信度


def _raw_screens() -> list[tuple[dict, np.ndarray]]:
    """枚举每块物理显示器并截图。

    跳过 mss 的 `monitors[0]`——那是所有屏的合成矩形，不是一块真实显示器，
    它的 left/top 是并集原点，拿它当单块屏用会把坐标算错。
    """
    out: list[tuple[dict, np.ndarray]] = []
    with _MSS() as sct:
        for mon in sct.monitors[1:]:
            if mon["width"] <= 0 or mon["height"] <= 0:
                continue
            img = np.asarray(sct.grab(mon))[:, :, :3]
            out.append((dict(mon), img))
    return out


def _captures_from(raw: list[tuple[dict, np.ndarray]]) -> list[ScreenCapture]:
    caps: list[ScreenCapture] = []
    for mon, img in raw:
        w = int(mon["width"]) or 0
        caps.append(ScreenCapture(
            image=img,
            scale=(img.shape[1] / w) if w else 1.0,
            left=int(mon["left"]),
            top=int(mon["top"]),
        ))
    return caps


# 「几乎是纯色」的判定阈值（0~255 尺度）。正常界面截图的 std 远大于 1，
# 而纯色图的 std 恰为 0，留 1 的余量只为挡压缩噪声。
_FLAT_STD = 1.0

# 「同一类问题只报一次」的闸门：匹配重试循环每 400ms 跑一轮，不设闸门会刷屏。
_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(msg, *args)


def _is_flat(img: np.ndarray) -> bool:
    """图像是否近似纯色。

    纯色图会让 `TM_CCOEFF_NORMED` 变成退化输入（方差为 0 → 0/0），结果是
    **未定义的**：实测同一张纯色屏上，40x60 的纯色模板得到 `max=1.0 @ (0,0)`，
    40x20 的得到 `0.0`（差异来自 OpenCV 的 SIMD 尾块处理）。也就是说
    「纯色模板」会**随机地**在左上角报出满分命中——比报「找不到」危险得多：
    工作流会点向屏幕左上角，而且不报错。

    所以不能依赖 OpenCV 的退化结果，必须自己拦。

    实测触发场景：没授予「屏幕录制」权限时 mss 会返回一张全均匀的图
    （此时 scale 往往还是 2.0）；用户若在这种状态下「截取模板」，存下的
    就是纯色模板。
    """
    return img.size == 0 or float(img.std()) < _FLAT_STD


def _warn_flat_once() -> None:
    _warn_once(
        "flat-screen",
        "屏幕截图近似纯色，已跳过匹配。最常见原因是没有授予「屏幕录制」权限"
        "（系统设置 → 隐私与安全性 → 屏幕录制），也可能是屏幕处于全黑/锁屏状态。")


# PNG 的 pHYs 块里记着像素密度：`screencapture` 在 Retina 屏上写 144（=2x），
# 普通屏写 72（=1x）。用它判断「模板是不是按另一种密度截出来的」。
_PNG_DPI_BASE = 72.0


def _png_density(path: str) -> float | None:
    """读 PNG 的 pHYs 块，返回相对 72dpi 的密度倍率；读不到返回 None。

    只解析到 pHYs 为止、不解码像素，比 `cv2.imread` 便宜得多，可以放心放在
    「匹配失败」的诊断路径上。
    """
    import struct
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return None
                (length,) = struct.unpack(">I", head[:4])
                ctype = head[4:8]
                if ctype == b"pHYs":
                    data = f.read(length)
                    if len(data) < 9:
                        return None
                    ppm, _ppm_y, unit = struct.unpack(">IIB", data[:9])
                    if unit != 1 or ppm == 0:   # 1 = 单位是米；0 = 只给纵横比
                        return None
                    return (ppm * 0.0254) / _PNG_DPI_BASE
                if ctype == b"IDAT":        # 像素数据之前都没有 pHYs，即未声明密度
                    return None
                f.seek(length + 4, 1)       # 跳过数据 + CRC
    except OSError:
        return None


def _warn_density_mismatch(path: str, screen_scales: list[float]) -> None:
    """匹配失败时，若模板密度与截屏密度不一致，点名这个原因。

    密度不一致属于**静默失败**：分数只是整体偏低，从外面看跟「屏上确实没有」
    一模一样（2026-09-13 的「截图识别没有效果」就是它，排查了很久）。
    """
    if not screen_scales or path in _warned:
        return
    density = _png_density(path)
    if density is None or any(abs(density - s) < 0.01 for s in screen_scales):
        return
    _warn_once(
        "density:" + path,
        "模板 %s 的像素密度是 %.2fx，而截屏是 %sx，两者不一致会让匹配分数整体"
        "偏低（看起来像「找不到」）。请用参数面板上的「截取」按钮重新截模板。",
        path, density, "/".join(f"{s:g}" for s in screen_scales))


def captures() -> list[ScreenCapture]:
    """全部显示器（每块一张图 + 自己的 scale 与全局原点）。

    mss 枚举不到显示器时（休眠 / 锁屏 / 权限未就绪）退化为 Quartz 全屏兜底，
    此时只有一块「所有屏的并集」。
    """
    try:
        raw = _raw_screens()
    except Exception:
        raw = []
    if raw:
        return _captures_from(raw)
    return [_quartz_capture()]


def main_capture() -> ScreenCapture:
    """主显示器（菜单栏所在那块）。

    Quartz 全局坐标里主屏原点恒为 (0,0)，按这个挑比依赖 mss 的枚举顺序可靠。
    """
    caps = captures()
    for cap in caps:
        if cap.left == 0 and cap.top == 0:
            return cap
    return caps[0]


def grab_screen_bgr() -> tuple[np.ndarray, float]:
    """主屏截图 + 缩放系数。

    保留旧签名给「只关心主屏」的调用方；需要覆盖多屏请用 `captures()`。
    """
    cap = main_capture()
    return cap.image, cap.scale


def _logical_union() -> tuple[int, int, int, int]:
    """所有显示器的逻辑并集 (left, top, w, h)，原点左上、y 向下。

    `NSScreen.frame()` 是 Cocoa 坐标（原点左下、y 向上），要翻一下：
    `top = 主屏高 - (origin.y + height)`。
    """
    from AppKit import NSScreen
    screens = NSScreen.screens() or []
    if not screens:
        return 0, 0, 1, 1
    primary_h = int(NSScreen.mainScreen().frame().size.height)
    lefts: list[int] = []
    tops: list[int] = []
    rights: list[int] = []
    bottoms: list[int] = []
    for s in screens:
        f = s.frame()
        l, w = int(f.origin.x), int(f.size.width)
        t = primary_h - int(f.origin.y + f.size.height)
        h = int(f.size.height)
        lefts.append(l)
        tops.append(t)
        rights.append(l + w)
        bottoms.append(t + h)
    left, top = min(lefts), min(tops)
    return left, top, max(rights) - left, max(bottoms) - top


def _quartz_capture() -> ScreenCapture:
    """mss 枚举不到显示器时的兜底：Quartz 全屏捕获。

    返回的是所有显示器的**并集图**，原点取并集左上角。已知近似：混合
    Retina/非 Retina 时这里只有一个全局 scale（按并集宽度算），因为
    Quartz 这张图本身就是一张位图；正常路径（mss）是每块屏各算各的。
    """
    import Quartz
    from AppKit import NSBitmapImageRep

    cgimg = Quartz.CGWindowListCreateImage(
        Quartz.CGRectInfinite, Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
    if cgimg is None:
        raise RuntimeError("屏幕捕获不可用")
    rep = NSBitmapImageRep.alloc().initWithCGImage_(cgimg)
    data = rep.representationUsingType_properties_(4, None)  # 4 = NSBitmapImageFileTypePNG
    arr = np.frombuffer(bytes(data), dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("屏幕捕获解码失败")
    left, top, w, _h = _logical_union()
    return ScreenCapture(image=img, scale=img.shape[1] / max(w, 1), left=left, top=top)


def grab_region_bgr(region: tuple | None = None) -> ScreenCapture:
    """截取全局逻辑坐标区域 `(left, top, w, h)`；`None` = 主屏整块。

    返回的 `ScreenCapture` 的 `left/top` 是**实际截到那块**的全局左上角，
    所以下游一律用 `to_global()` 换算，不用自己加偏移。

    区域跨屏时只取与它重叠面积最大的那块屏上的部分：跨屏拼接没有意义——
    点击坐标可以跨屏，但 OCR 框、模板匹配只能落在某一块屏的图里。
    截不到（区域完全在屏幕外）时返回空图，调用方按「没找到」处理。
    """
    if region is None:
        return main_capture()
    l, t, w, h = (int(v) for v in region)
    best_cap: ScreenCapture | None = None
    best_area = 0
    best_box = (l, t, l, t)
    for cap in captures():
        cw, ch = cap.logical_size
        il, it = max(l, cap.left), max(t, cap.top)
        ir, ib = min(l + w, cap.left + cw), min(t + h, cap.top + ch)
        if ir <= il or ib <= it:
            continue
        area = (ir - il) * (ib - it)
        if area > best_area:
            best_area, best_cap, best_box = area, cap, (il, it, ir, ib)
    if best_cap is None:
        return ScreenCapture(image=np.zeros((0, 0, 3), np.uint8), scale=1.0, left=l, top=t)
    s = best_cap.scale or 1.0
    il, it, ir, ib = best_box
    x1 = max(int(round((il - best_cap.left) * s)), 0)
    y1 = max(int(round((it - best_cap.top) * s)), 0)
    x2 = min(int(round((ir - best_cap.left) * s)), best_cap.image.shape[1])
    y2 = min(int(round((ib - best_cap.top) * s)), best_cap.image.shape[0])
    return ScreenCapture(image=best_cap.image[y1:y2, x1:x2], scale=s, left=il, top=it)


def find_template(threshold: float = 0.8,
                  template_bgr: np.ndarray | None = None,
                  template_path: str = "") -> MatchResult:
    """在所有显示器里找模板图，返回**全局逻辑坐标**中心点。

    逐块屏匹配、取最高分：目标在哪块屏上都能找到，且坐标已含该屏偏移。
    代价是每轮重试的匹配次数随屏数线性增长（双屏约 2 倍）。

    **模板必须与截屏同像素密度**：截屏是物理像素（Retina 上 scale=2），
    而「截取模板」用的 `screencapture` 产出的也是物理像素，两者天然对齐；
    若手工塞进来一张别处下载的 1x 小图，密度差一倍会让分数整体偏低，
    看起来和「找不到」一样（`_warn_density_mismatch` 会在这种情况下点名）。
    """
    if template_bgr is None:
        tpl = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if tpl is None:
            # 文件被移动/删除与「屏上真没有」在返回值上完全一样，必须留个话
            _warn_once("missing-template:" + template_path,
                       "模板图读不出来：%s。文件可能已被移动或删除，"
                       "该节点会一直按「找不到」处理。", template_path)
            return MatchResult(found=False)
    else:
        tpl = template_bgr
    if _is_flat(tpl):
        # 纯色模板：匹配结果无意义，明确报「找不到」而不是返回 (0,0) 的假命中
        _warn_once("flat-template",
                   "模板图近似纯色（多半是在没有「屏幕录制」权限时截出来的），"
                   "匹配无意义，已按「找不到」处理：%s", template_path or "(内存图)")
        return MatchResult(found=False)
    th, tw = tpl.shape[:2]
    best = MatchResult(found=False)
    scales: list[float] = []
    for cap in captures():
        screen = cap.image
        scales.append(cap.scale or 1.0)
        # 模板不小于屏幕时 matchTemplate 会直接抛错，跳过这块屏
        if th >= screen.shape[0] or tw >= screen.shape[1]:
            continue
        if _is_flat(screen):
            _warn_flat_once()
            continue
        res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
        _min, max_val, _loc, max_loc = cv2.minMaxLoc(res)
        if max_val > best.score:
            x, y = cap.to_global(max_loc[0] + tw / 2, max_loc[1] + th / 2)
            best = MatchResult(found=bool(max_val >= threshold), x=x, y=y,
                               score=float(max_val))
        if best.found:
            break
    if not best.found and template_path:
        _warn_density_mismatch(template_path, scales)
    return best
