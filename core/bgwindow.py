"""按窗口截图与「投递到进程」的输入——为了能后台跑。

为什么需要它：现在的截图走 mss（抓显示器），输入走全局事件（光标要真的移过去）。
这两条都要求游戏窗口在最前面，于是挂机时电脑就没法干别的。

后台要成立，需要两个能力：

1. **按窗口截图**：ScreenCaptureKit（macOS 15+ 上 CGWindowListCreateImage 已经不可用）。
   它按窗口 id 抓，窗口被别的窗口盖住也能拿到画面——**前提是游戏还在渲染**。
   很多游戏被完全遮住后会降帧甚至停帧，那样抓到的是旧帧或黑屏，OCR 自然失效。
2. **输入投递给进程**：CGEventPostToPid，事件直接进游戏进程，不移动真实光标、
   也不抢焦点。键盘通常没问题；鼠标要看游戏认不认（有的走 raw input，只认真实光标）。

这两条都是「能不能成要看游戏」的事，所以这里只提供能力 + 自检，不假设结论。
"""
from __future__ import annotations

import logging
import time

import numpy as np

logger = logging.getLogger(__name__)


def _cgimage_to_bgr(img) -> np.ndarray | None:
    """CGImage → BGR ndarray。"""
    import Quartz
    w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    if not w or not h:
        return None
    provider = Quartz.CGImageGetDataProvider(img)
    if provider is None:
        return None
    data = Quartz.CGDataProviderCopyData(provider)
    if data is None:
        return None
    bpr = Quartz.CGImageGetBytesPerRow(img)
    bpp = Quartz.CGImageGetBitsPerPixel(img) // 8
    buf = np.frombuffer(data, dtype=np.uint8)
    if buf.size < bpr * h:
        return None
    arr = buf[: bpr * h].reshape(h, bpr)
    arr = arr[:, : w * bpp].reshape(h, w, bpp)
    if bpp == 4:      # BGRA
        return arr[:, :, :3].copy()
    if bpp == 3:
        return arr.copy()
    return None


def _window_id_by_owner(owner_keyword: str):
    """按属主名找窗口 id（CGWindowListCopyWindowInfo 不需要屏幕录制权限）。"""
    import Quartz
    opts = (Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements)
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID):
        owner = str(w.get("kCGWindowOwnerName", ""))
        if owner_keyword and owner_keyword in owner:
            b = w.get("kCGWindowBounds") or {}
            if int(b.get("Width") or 0) < 50 or int(b.get("Height") or 0) < 50:
                continue
            return w.get("kCGWindowNumber"), owner, b
    return None, None, None


def capture_window(owner_keyword: str, timeout_s: float = 5.0):
    """按窗口抓一帧。返回 (bgr, meta)；失败返回 (None, meta)，meta 里有原因。

    主路径是 `screencapture -l <窗口id>`：
    - macOS 15+ 上 CGWindowListCreateImage 已不可用，只能走窗口级截图 API；
    - pyobjc 的 ScreenCaptureKit 绑定在这个版本里没暴露 SCScreenshotManager 的类方法
      （实测 "No attribute captureImageInFilter_configuration_completionHandler_"），
      所以先用命令行这条更稳的路；
    - `screencapture` 需要屏幕录制权限，从 app 里（sidecar 继承 app 的授权）能跑；
      从没有授权的 shell 里跑会报 "could not create image from display"。
    """
    import os
    import subprocess
    import tempfile

    import cv2

    wid, owner, bounds = _window_id_by_owner(owner_keyword)
    if wid is None:
        return None, {"ok": False, "reason": f"没找到窗口（属主含「{owner_keyword}」）"}
    fd, path = tempfile.mkstemp(suffix=".png", prefix="af-win-")
    os.close(fd)
    try:
        r = subprocess.run(["/usr/sbin/screencapture", "-l", str(wid), "-x", path],
                           capture_output=True, text=True, timeout=timeout_s)
        if r.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) < 1000:
            err = (r.stderr or "").strip()
            return None, {
                "ok": False,
                "windowId": wid,
                "reason": f"按窗口截图失败：{err or '没有产出文件'}"
                          "（屏幕录制权限没给到本程序时就是这个表现）",
            }
        bgr = cv2.imread(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if bgr is None:
        return None, {"ok": False, "windowId": wid, "reason": "截图读不出来"}
    meta = {
        "ok": True,
        "windowId": wid,
        "owner": owner,
        "w": int(bgr.shape[1]),
        "h": int(bgr.shape[0]),
        "mean": round(float(bgr.mean()), 1),
        "std": round(float(bgr.std()), 1),
        # 全黑/纯色基本等于「窗口被遮住后停止渲染」，这一条就是判断后台截图可不可行的依据
        "looks_blank": bool(bgr.std() < 2.0),
    }
    return bgr, meta


def _capture_window_sck(owner_keyword: str, timeout_s: float = 5.0):
    """ScreenCaptureKit 路径（保留但默认不用：见 capture_window 的说明）。"""
    try:
        import ScreenCaptureKit as SCK
    except Exception as e:                                   # noqa: BLE001
        return None, {"ok": False, "reason": f"没有 ScreenCaptureKit：{e}"}

    box: dict = {"content": None, "err": None}

    def on_content(content, err):
        box["content"], box["err"] = content, err

    try:
        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(on_content)
    except Exception as e:                                   # noqa: BLE001
        return None, {"ok": False, "reason": f"调用 SCShareableContent 失败：{e}"}

    t0 = time.time()
    while box["content"] is None and box["err"] is None and time.time() - t0 < timeout_s:
        time.sleep(0.03)
    if box["err"] is not None:
        msg = str(box["err"])
        hint = ""
        if "TCC" in msg or "-3801" in msg:
            hint = "（屏幕录制权限没给到本程序）"
        return None, {"ok": False, "reason": f"取窗口列表被拒{hint}：{msg}"}
    content = box["content"]
    if content is None:
        return None, {"ok": False, "reason": "取窗口列表超时"}

    target = None
    for w in content.windows():
        app = w.owningApplication()
        name = str(app.applicationName()) if app else ""
        if owner_keyword and owner_keyword in name:
            target = w
            break
    if target is None:
        return None, {"ok": False, "reason": f"没找到窗口（属主含「{owner_keyword}」）"}

    frame = target.frame()
    cfg = SCK.SCStreamConfiguration.alloc().init()
    cfg.setWidth_(int(frame.size.width))
    cfg.setHeight_(int(frame.size.height))
    cfg.setShowsCursor_(False)
    filt = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(target)

    shot: dict = {"img": None, "err": None}

    def on_shot(img, err):
        shot["img"], shot["err"] = img, err

    SCK.SCScreenshotManager.captureImageInFilter_configuration_completionHandler_(
        filt, cfg, on_shot)
    t0 = time.time()
    while shot["img"] is None and shot["err"] is None and time.time() - t0 < timeout_s:
        time.sleep(0.03)
    if shot["err"] is not None:
        return None, {"ok": False, "reason": f"抓窗口失败：{shot['err']}"}
    img = shot["img"]
    if img is None:
        return None, {"ok": False, "reason": "抓窗口超时"}
    bgr = _cgimage_to_bgr(img)
    if bgr is None:
        return None, {"ok": False, "reason": "截图转 numpy 失败"}
    meta = {
        "ok": True,
        "w": int(bgr.shape[1]),
        "h": int(bgr.shape[0]),
        "mean": round(float(bgr.mean()), 1),
        "std": round(float(bgr.std()), 1),
        # 全黑/纯色基本等于「窗口被遮住后停止渲染」，这一条就是判断后台截图可不可行的依据
        "looks_blank": bool(bgr.std() < 2.0),
    }
    return bgr, meta


def post_key_to_pid(pid: int, keycode: int) -> bool:
    """把按键事件直接投递给指定进程（不移动光标、不抢焦点）。"""
    import Quartz
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(src, keycode, False)
    if down is None or up is None:
        return False
    Quartz.CGEventPostToPid(pid, down)
    Quartz.CGEventPostToPid(pid, up)
    return True


def post_click_to_pid(pid: int, x: int, y: int, button: str = "left") -> bool:
    """把鼠标点击直接投递给指定进程。坐标是全局屏幕坐标。"""
    import Quartz
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    btn = {"left": Quartz.kCGMouseButtonLeft,
           "right": Quartz.kCGMouseButtonRight,
           "middle": Quartz.kCGMouseButtonCenter}.get(button, Quartz.kCGMouseButtonLeft)
    down_type = (Quartz.kCGEventRightMouseDown if button == "right" else Quartz.kCGEventLeftMouseDown)
    up_type = (Quartz.kCGEventRightMouseUp if button == "right" else Quartz.kCGEventLeftMouseUp)
    pt = Quartz.CGPointMake(float(x), float(y))
    down = Quartz.CGEventCreateMouseEvent(src, down_type, pt, btn)
    up = Quartz.CGEventCreateMouseEvent(src, up_type, pt, btn)
    if down is None or up is None:
        return False
    Quartz.CGEventPostToPid(pid, down)
    Quartz.CGEventPostToPid(pid, up)
    return True
