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
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import mss

# mss 10.2 起 `mss.mss` 弃用并改名 `mss.MSS`；两者并存期取新的，老版本回退旧名。
_MSS = getattr(mss, "MSS", None) or mss.mss


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
    """
    if template_bgr is None:
        tpl = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if tpl is None:
            return MatchResult(found=False)
    else:
        tpl = template_bgr
    th, tw = tpl.shape[:2]
    best = MatchResult(found=False)
    for cap in captures():
        screen = cap.image
        # 模板不小于屏幕时 matchTemplate 会直接抛错，跳过这块屏
        if th >= screen.shape[0] or tw >= screen.shape[1]:
            continue
        res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
        _min, max_val, _loc, max_loc = cv2.minMaxLoc(res)
        if max_val > best.score:
            x, y = cap.to_global(max_loc[0] + tw / 2, max_loc[1] + th / 2)
            best = MatchResult(found=bool(max_val >= threshold), x=x, y=y,
                               score=float(max_val))
        if best.found:
            break
    return best
