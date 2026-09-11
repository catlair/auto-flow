"""OCR：macOS Vision 框架离线文字识别。

截图（物理像素）→ VNRecognizeTextRequest → 归一化框（左下原点）
→ 全局逻辑坐标（点，左上原点）中心点，与 pynput 点击坐标一致。

坐标一律经 `vision.ScreenCapture.to_global()` 换算：多显示器下每块屏的
`left/top` 不同，靠调用方手动加偏移迟早会漏一处。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import Quartz
from Foundation import NSData
import Vision


@dataclass
class TextHit:
    text: str
    x: int = 0           # 全局逻辑坐标中心点
    y: int = 0
    confidence: float = 0.0


def grab_region_bgr(region: tuple | None = None):
    """截取全局逻辑坐标区域 `(left, top, w, h)`；`None` = 主屏整块。

    返回 `vision.ScreenCapture`（含图、scale 与该图左上角的全局坐标）。
    """
    from core import vision
    return vision.grab_region_bgr(region)


def _center_from_box(box, img_w: int, img_h: int, cap) -> tuple[int, int]:
    """Vision 归一化框（左下原点）→ 全局逻辑坐标中心点。"""
    s = cap.scale or 1.0
    cx = cap.left + (box.origin.x + box.size.width / 2) * img_w / s
    cy = cap.top + (1 - (box.origin.y + box.size.height / 2)) * img_h / s
    return int(cx), int(cy)


def recognize_texts(cap, languages: list | None = None) -> list[TextHit]:
    """对截图做 OCR，返回所有文本及全局逻辑坐标中心点。"""
    import cv2
    bgr = cap.image
    if bgr.size == 0:
        return []
    h, w = bgr.shape[:2]
    ok, png = cv2.imencode(".png", bgr)
    if not ok:
        return []
    nsdata = NSData.dataWithBytes_length_(png.tobytes(), len(png))
    cg = Quartz.CGImageSourceCreateWithData(nsdata, None)
    cgimg = Quartz.CGImageSourceCreateImageAtIndex(cg, 0, None)
    if cgimg is None:
        return []
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimg, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    if languages:
        req.setRecognitionLanguages_(languages)
    else:
        req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    handler.performRequests_error_([req], None)
    hits: list[TextHit] = []
    for obs in (req.results() or []):
        candidate = obs.topCandidates_(1)[0]
        cx, cy = _center_from_box(obs.boundingBox(), w, h, cap)
        hits.append(TextHit(text=str(candidate.string()), x=cx, y=cy,
                            confidence=float(candidate.confidence())))
    return hits


def find_text(target: str, region: tuple | None = None,
              languages: list | None = None) -> TextHit | None:
    """在屏幕（或指定全局区域）中找包含 target 的文字，返回第一个命中。"""
    cap = grab_region_bgr(region)
    for hit in recognize_texts(cap, languages):
        if target in hit.text:
            return hit
    return None
