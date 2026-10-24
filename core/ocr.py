"""OCR：macOS Vision 框架离线文字识别。

截图（物理像素）→ VNRecognizeTextRequest → 归一化框（左下原点）
→ 全局逻辑坐标（点，左上原点）中心点，与 pynput 点击坐标一致。

坐标一律经 `vision.ScreenCapture.to_global()` 换算：多显示器下每块屏的
`left/top` 不同，靠调用方手动加偏移迟早会漏一处。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

import Quartz
from Foundation import NSData
import Vision

logger = logging.getLogger(__name__)


@dataclass
class TextHit:
    text: str
    x: int = 0           # 全局逻辑坐标中心点
    y: int = 0
    confidence: float = 0.0
    w: int = 0
    h: int = 0
    """识别到的文字框尺寸（**全局逻辑点**）。

    给「点击点落在目标框内」用（`tasks.builtin.click_point_in_box`）：一段文字
    可能很长（一整行菜单项、一整句提示），固定点中心有时正好压在链接/图标上，
    或者用户就是想点这行的靠左/靠右。单位与 `x/y` 同一套换算（物理像素 / scale），
    理由见 `vision.MatchResult.w`。
    """


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


def _box_size(box, img_w: int, img_h: int, cap) -> tuple[int, int]:
    """Vision 归一化框（左下原点）→ 全局逻辑坐标下的**尺寸**（点）。

    单独一个函数而不是让 `_center_from_box` 多返回两个值：那个函数已被用例直接
    断言成二元组（`test_ocr_box_center_includes_display_origin`），改签名是白拆。
    """
    s = cap.scale or 1.0
    return (int(round(box.size.width * img_w / s)),
            int(round(box.size.height * img_h / s)))


def _split_merged(hit: TextHit, target: str) -> list[TextHit]:
    """把「一个框里出现多次 target」拆成多个命中。

    Vision 会把**同一行、内容相同**的几段文字合并成一个观察结果。真机上踩过：
    悬赏列表里三张卡写着同样的「解决诬陷「被诬陷偷猎动物」」，识别出来只有一个框，
    框中心正好落在**第二张卡**上——于是「点第一个」点到的永远是第二张，
    而且日志里看是「命中 1 处」，完全看不出问题。

    按出现次数把框横向等分，每个等分段的中心就是一个命中的位置。等分只在
    「同一行、等宽排列」时成立，而这正是卡片/格子列表的排法。
    """
    n = hit.text.count(target)
    if n <= 1 or hit.w <= 0:
        return [hit]
    seg = hit.w / float(n)
    left = hit.x - hit.w / 2.0
    out = []
    for i in range(n):
        out.append(TextHit(text=target,
                           x=int(round(left + seg * (i + 0.5))),
                           y=hit.y, confidence=hit.confidence,
                           w=int(round(seg)), h=hit.h))
    return out


def recognize_bgr(bgr, languages: list | None = None) -> list[TextHit]:
    """对一张现成的 BGR 图做 OCR，坐标是**这张图的像素坐标**（左上原点）。

    给「按窗口截图」用：那张图来自 ScreenCaptureKit，不是屏幕截图，
    所以没法套用全局坐标的换算——调用方要的是「窗口里第几个像素」。
    """
    import cv2
    if bgr is None or bgr.size == 0:
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
    req.setRecognitionLanguages_(languages or ["zh-Hans", "en-US"])
    handler.performRequests_error_([req], None)
    hits: list[TextHit] = []
    for obs in (req.results() or []):
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        text = str(cand[0].string())
        box = obs.boundingBox()
        bw = float(box.size.width) * w
        bh = float(box.size.height) * h
        cx = float(box.origin.x) * w + bw / 2.0
        # Vision 是左下原点，转成左上原点
        cy = (1.0 - float(box.origin.y) - float(box.size.height)) * h + bh / 2.0
        hits.append(TextHit(text=text, x=int(round(cx)), y=int(round(cy)),
                            confidence=float(cand[0].confidence()),
                            w=int(round(bw)), h=int(round(bh))))
    hits.sort(key=lambda t: (t.y, t.x))
    return hits


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
        box = obs.boundingBox()
        cx, cy = _center_from_box(box, w, h, cap)
        tw, th = _box_size(box, w, h, cap)
        hits.append(TextHit(text=str(candidate.string()), x=cx, y=cy,
                            confidence=float(candidate.confidence()), w=tw, h=th))
    return hits


def find_texts(target: str, region: tuple | None = None,
               languages: list | None = None) -> list:
    """找出**所有**包含 target 的文字，按阅读顺序（先上后下、同一行先左后右）返回。

    为什么要有「全部」：同一句话在屏幕上常常出现多次。真机上踩过的例子——悬赏列表里
    卡片上写着「解决诬陷…」，右侧详情面板里**也**写着同一句话。只取第一个命中时，
    到底点的是卡片还是详情面板全看识别顺序，表现是「点了没反应」，而且完全无法排查。
    有了全部命中，调用方就能按位置筛（比如「只在左边 70% 里找」）。
    """
    cap = grab_region_bgr(region)
    hits: list[TextHit] = []
    for h in recognize_texts(cap, languages):
        if target not in h.text:
            continue
        parts = _split_merged(h, target)
        if len(parts) > 1:
            logger.info("「%s」在一个框里出现 %d 次，按等分拆开：%s → %s",
                        target, len(parts), (h.x, h.w), [(p.x, p.w) for p in parts])
        hits.extend(parts)
    hits.sort(key=lambda h: (h.y, h.x))
    return hits


def find_text(target: str, region: tuple | None = None,
              languages: list | None = None) -> TextHit | None:
    """在屏幕（或指定全局区域）中找包含 target 的文字，返回第一个命中。"""
    for hit in find_texts(target, region, languages):
        return hit
    return None
