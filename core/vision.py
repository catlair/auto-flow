"""屏幕截图与模板匹配（Retina 适配）。

mss 截屏返回的是物理像素；pynput 点击用的是逻辑坐标（点），
scale = 物理像素宽 / 逻辑点宽，匹配结果除以 scale 再给点击层。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
import mss


@dataclass
class MatchResult:
    found: bool
    x: int = 0          # 逻辑坐标（点），目标中心
    y: int = 0
    score: float = 0.0  # 0~1 置信度


def _scale_factor(monitor: dict) -> float:
    """当前主屏 物理像素/逻辑点 比例。"""
    from AppKit import NSScreen
    main = NSScreen.mainScreen().frame()
    logical_w = int(main.size.width)
    return monitor["width"] / max(logical_w, 1)


def _quartz_grab_bgr() -> tuple[np.ndarray, float]:
    """mss 枚举不到显示器时（休眠/锁屏）的兜底：Quartz 全屏捕获。"""
    import Quartz
    from AppKit import NSScreen
    cgimg = Quartz.CGWindowListCreateImage(
        Quartz.CGRectInfinite, Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
    if cgimg is None:
        raise RuntimeError("屏幕捕获不可用")
    from AppKit import NSBitmapImageRep
    rep = NSBitmapImageRep.alloc().initWithCGImage_(cgimg)
    data = rep.representationUsingType_properties_(4, None)  # 4 = NSBitmapImageFileTypePNG
    arr = np.frombuffer(bytes(data), dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("屏幕捕获解码失败")
    logical_w = int(NSScreen.mainScreen().frame().size.width)
    return img, img.shape[1] / max(logical_w, 1)


def grab_screen_bgr() -> tuple[np.ndarray, float]:
    """全屏截图（BGR）+ 缩放系数。"""
    try:
        with mss.mss() as sct:
            mons = sct.monitors
            if len(mons) > 1 and mons[1]["width"] > 0:
                img = np.asarray(sct.grab(mons[1]))[:, :, :3]
                return img, _scale_factor(mons[1])
    except Exception:
        pass
    return _quartz_grab_bgr()


def find_template(threshold: float = 0.8,
                  template_bgr: np.ndarray | None = None,
                  template_path: str = "") -> MatchResult:
    """在主屏截图中找模板图，返回逻辑坐标中心点。"""
    if template_bgr is None:
        tpl = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if tpl is None:
            return MatchResult(found=False)
    else:
        tpl = template_bgr
    screen, scale = grab_screen_bgr()
    th, tw = tpl.shape[:2]
    if th >= screen.shape[0] or tw >= screen.shape[1]:
        return MatchResult(found=False)
    res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
    _min, max_val, _loc, max_loc = cv2.minMaxLoc(res)
    if max_val < threshold:
        return MatchResult(found=False, score=float(max_val))
    cx = int((max_loc[0] + tw / 2) / scale)
    cy = int((max_loc[1] + th / 2) / scale)
    return MatchResult(found=True, x=cx, y=cy, score=float(max_val))
