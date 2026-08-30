"""OCR：macOS Vision 框架离线文字识别。

区域截图（物理像素）→ VNRecognizeTextRequest → 归一化框（左下原点）
→ 逻辑坐标（点，左上原点）中心点，与 pynput 点击坐标一致。
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
    x: int = 0           # 逻辑坐标中心点
    y: int = 0
    confidence: float = 0.0


def _scale_factor(mon: dict) -> float:
    from AppKit import NSScreen
    main = NSScreen.mainScreen().frame()
    return mon["width"] / max(int(main.size.width), 1)


def grab_region_bgr(region: tuple | None = None) -> tuple[np.ndarray, float]:
    """region=(left, top, w, h) 逻辑坐标；None 为全主屏。"""
    from core import vision
    if region is None:
        return vision.grab_screen_bgr()
    img, scale = vision.grab_screen_bgr()
    l, t, w, h = region
    crop = img[max(int(t*scale), 0):int((t+h)*scale), max(int(l*scale), 0):int((l+w)*scale)]
    return crop, scale


def recognize_texts(bgr: np.ndarray, scale: float,
                    languages: list | None = None) -> list[TextHit]:
    """对截图做 OCR，返回所有文本及逻辑坐标中心点。"""
    import cv2
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
        box = obs.boundingBox()  # 归一化，左下原点
        cx = (box.origin.x + box.size.width / 2) * w / scale
        cy = (1 - (box.origin.y + box.size.height / 2)) * h / scale
        hits.append(TextHit(text=str(candidate.string()), x=int(cx), y=int(cy),
                            confidence=float(candidate.confidence())))
    return hits


def find_text(target: str, region: tuple | None = None,
              languages: list | None = None) -> TextHit | None:
    """在屏幕（或指定区域）中找包含 target 的文字，返回第一个命中。"""
    img, scale = grab_region_bgr(region)
    for hit in recognize_texts(img, scale, languages):
        if target in hit.text:
            if region is not None:
                hit.x += int(region[0])
                hit.y += int(region[1])
            return hit
    return None
