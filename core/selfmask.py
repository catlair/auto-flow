"""把自己窗口从截图里挖掉。

**为什么必须这么做**：识别是**全屏**做的——OCR 与模板匹配都会读到屏幕上的所有
窗口，包括 Auto Flow 自己的界面。而运行期间主窗开着「鼠标穿透」（见
`set_click_through`：回放时点击不能被自己的窗口吃掉），于是一旦命中的文字
出现在自己的面板里（比如轨迹列表里就写着「点『继续』（结算页）」），
点击坐标就会落在自己窗口上，然后**穿透到窗口下方的桌面或别的程序**——
用户看到的现象是「操作穿透了游戏，点到了后面的程序」。

所以：截屏之后、匹配之前，把本进程所有可见窗口的矩形涂黑。涂黑是就地修改，
下游的模板匹配与 OCR 都不会再看到这些像素。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("core.selfmask")

# 认自己：按窗口 owner 名匹配（大小写不敏感）。开发时是 "autoflow-tauri"，
# 打包后 CFBundleName 是 "Auto Flow"，两种都覆盖。
_OWNER_KEYS = ("autoflow", "auto flow")

# 窗口列表的缓存时长。截图在重试循环里每 300~400ms 一轮，一次条件组要连做
# 好几次识别——每轮都去问 WindowServer 是纯浪费。0.25s 足够跟上窗口拖动。
_TTL_S = 0.25
_cache: dict = {"t": 0.0, "rects": []}
# 「已经提示过」的闸门：只在第一次真的遮到东西时说明一句，不刷屏。
_noted = {"done": False}


def own_window_rects(force: bool = False) -> list:
    """本进程可见窗口的矩形列表，全局**逻辑**坐标 `(left, top, w, h)`。

    取不到窗口列表（没有屏幕录制权限、WindowServer 异常）时返回空列表——
    此时按「不遮」处理，行为退回改动之前，绝不因为遮罩把识别整体搞挂。
    """
    now = time.monotonic()
    if not force and now - _cache["t"] < _TTL_S:
        return _cache["rects"]
    rects: list = []
    try:
        import Quartz
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            owner = str(w.get("kCGWindowOwnerName", "") or "").lower()
            if not any(k in owner for k in _OWNER_KEYS):
                continue
            # 只遮普通窗口层（layer 0）：面板、菜单栏、悬浮窗不是我们的识别目标，
            # 误遮反而可能把游戏画面挖掉一块。
            if int(w.get("kCGWindowLayer", 0) or 0) != 0:
                continue
            b = w.get("kCGWindowBounds") or {}
            wd, ht = int(b.get("Width", 0)), int(b.get("Height", 0))
            if wd <= 0 or ht <= 0:
                continue
            rects.append((int(b.get("X", 0)), int(b.get("Y", 0)), wd, ht))
    except Exception:  # noqa: BLE001  遮罩是保护措施，失败就退回不遮
        rects = []
    _cache.update(t=now, rects=rects)
    return rects


def mask_capture(cap) -> int:
    """就地把 `cap.image` 里属于自身窗口的像素涂黑，返回涂了几块。"""
    rects = own_window_rects()
    if not rects:
        return 0
    img = cap.image
    if img is None or img.size == 0:
        return 0
    s = cap.scale or 1.0
    h, w = img.shape[:2]
    n = 0
    for (rx, ry, rw, rh) in rects:
        x1 = int(round((rx - cap.left) * s))
        y1 = int(round((ry - cap.top) * s))
        x2 = int(round((rx + rw - cap.left) * s))
        y2 = int(round((ry + rh - cap.top) * s))
        x1, y1 = max(x1, 0), max(y1, 0)
        x2, y2 = min(x2, w), min(y2, h)
        if x2 <= x1 or y2 <= y1:
            continue
        img[y1:y2, x1:x2] = 0
        n += 1
    if n and not _noted["done"]:
        _noted["done"] = True
        logger.info("已把 Auto Flow 自己的 %d 个窗口区域从截图里排除（防止识别到自己的界面）", n)
    return n


def point_in_own_window(x: int, y: int) -> bool:
    """这个全局逻辑坐标是不是落在自己的窗口上。

    给「点击前二次保险」用：即使遮罩失效（窗口列表读不到），也绝不该把点击
    打到自己窗口上——那会穿透到下面的桌面。
    """
    for (rx, ry, rw, rh) in own_window_rects():
        if rx <= x < rx + rw and ry <= y < ry + rh:
            return True
    return False


def reset_cache() -> None:
    """测试用：清掉缓存与闸门。"""
    _cache.update(t=0.0, rects=[])
    _noted["done"] = False
