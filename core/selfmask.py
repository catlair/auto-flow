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


def _minus(rect: tuple, cut: tuple) -> list:
    """矩形相减：从 `rect` 里挖掉 `cut`，返回剩下的若干块（最多 4 块）。"""
    ax, ay, aw, ah = rect
    bx, by, bw, bh = cut
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix1 >= ix2 or iy1 >= iy2:
        return [rect]                      # 不相交，原样保留
    out = []
    if iy1 > ay:
        out.append((ax, ay, aw, iy1 - ay))          # 上条
    if iy2 < ay2:
        out.append((ax, iy2, aw, ay2 - iy2))        # 下条
    if ix1 > ax:
        out.append((ax, iy1, ix1 - ax, iy2 - iy1))  # 左块
    if ix2 < ax2:
        out.append((ix2, iy1, ax2 - ix2, iy2 - iy1))  # 右块
    return out


def own_window_rects(force: bool = False) -> list:
    """本进程**实际可见**的那部分窗口矩形，全局逻辑坐标 `(left, top, w, h)`。

    为什么不是「窗口矩形直接当遮罩」：窗口列表是前到后有序的，而我们的窗口经常
    **被游戏窗口挡住一部分**。整块遮掉会把挡在前面的游戏画面一起涂黑——真机实测
    出现过「屏幕 74% 变黑、OCR 一个字都读不出来」，表现为脚本在大厅里空转、
    「开始匹配」明明在屏上却认不到。

    所以逐个窗口从前往后扫：遇到挡在**我们前面**的窗口（别的 App、layer 0、不透明），
    就把它的矩形从我们的遮罩里减掉。剩下的才是「我们自己真正露出来的部分」。

    取不到窗口列表（没有屏幕录制权限、WindowServer 异常）时返回空列表——
    按「不遮」处理，行为退回改动之前，绝不因为遮罩把识别整体搞挂。
    """
    now = time.monotonic()
    if not force and now - _cache["t"] < _TTL_S:
        return _cache["rects"]
    rects: list = []
    try:
        import Quartz
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        windows = []
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            b = w.get("kCGWindowBounds") or {}
            windows.append({
                "layer": w.get("kCGWindowLayer", 0),
                "alpha": w.get("kCGWindowAlpha", 1.0),
                "owner": w.get("kCGWindowOwnerName", ""),
                "x": b.get("X", 0), "y": b.get("Y", 0),
                "w": b.get("Width", 0), "h": b.get("Height", 0),
            })
        rects = rects_from_windows(windows)
    except Exception:  # noqa: BLE001  遮罩是保护措施，失败就退回不遮
        rects = []
    _cache.update(t=now, rects=rects)
    return rects


def rects_from_windows(windows: list) -> list:
    """窗口列表（**前到后**有序，元素是 dict）→ 需要涂黑的矩形列表。

    纯函数：不碰 Quartz，用例可以直接喂数组。规则见 `own_window_rects`：
    自己的窗口要遮，但必须减掉挡在它前面的那些窗口——否则游戏窗口在我们的窗口
    前面时，游戏画面会被一起涂黑（真机事故：屏幕 74% 变黑、大厅里「开始匹配」
    认不出来，脚本一直空转）。
    """
    rects: list = []
    front_cuts: list = []
    for w in windows or []:
        if int(w.get("layer", 0) or 0) != 0:
            continue
        if float(w.get("alpha", 1.0) or 0.0) <= 0.01:
            continue
        wd, ht = int(w.get("w", 0) or 0), int(w.get("h", 0) or 0)
        if wd <= 0 or ht <= 0:
            continue
        rect = (int(w.get("x", 0) or 0), int(w.get("y", 0) or 0), wd, ht)
        owner = str(w.get("owner", "") or "").lower()
        if any(k in owner for k in _OWNER_KEYS):
            pieces = [rect]
            for cut in front_cuts:
                nxt = []
                for r in pieces:
                    nxt.extend(_minus(r, cut))
                pieces = nxt
            rects.extend(pieces)
        front_cuts.append(rect)
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

# 「这个窗口像不像游戏窗口」的最小尺寸。真机踩过：结算动画期间最前面会出现一个
# 贴在 (0,0) 的小窗口（过渡/遮罩层），按它算「右下角」就把点击送到了屏幕左上角的
# 苹果菜单上——把系统菜单点开了。所以太小的一律不认。
_MIN_FRONT_W = 320
_MIN_FRONT_H = 240
# 系统自己的面板/悬浮层，即使层号是 0 也不该当锚点。
_NOT_GAME_OWNERS = ("window server", "程序坞", "dock", "控制中心", "通知中心",
                    "spotlight", "系统设置", "control center")


def front_window_from_windows(windows: list) -> tuple | None:
    """窗口列表（前到后）→ 最前面那个**不属于我们**的窗口矩形 `(left, top, w, h)`。

    给「按窗口定位点击」用：游戏窗口的右上角这类位置没有文字、也没法用模板稳定匹配
    （背后是随时变化的游戏画面），只能按窗口几何算。取不到就返回 None，
    调用方应当**跳过这次点击**而不是瞎点——点到别处比不点危险得多。
    """
    for w in windows or []:
        if int(w.get("layer", 0) or 0) != 0:
            continue
        if float(w.get("alpha", 1.0) or 0.0) <= 0.01:
            continue
        wd, ht = int(w.get("w", 0) or 0), int(w.get("h", 0) or 0)
        if wd <= 0 or ht <= 0:
            continue
        if wd < _MIN_FRONT_W or ht < _MIN_FRONT_H:
            continue
        owner = str(w.get("owner", "") or "").lower()
        if any(k in owner for k in _OWNER_KEYS):
            continue
        if any(k in owner for k in _NOT_GAME_OWNERS):
            continue
        return (int(w.get("x", 0) or 0), int(w.get("y", 0) or 0), wd, ht)
    return None


def _window_list() -> list:
    """当前屏上窗口列表（前到后，已转成普通 dict）；读不到返回空列表。"""
    try:
        import Quartz
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        out = []
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            b = w.get("kCGWindowBounds") or {}
            out.append({
                "layer": w.get("kCGWindowLayer", 0),
                "alpha": w.get("kCGWindowAlpha", 1.0),
                "owner": w.get("kCGWindowOwnerName", ""),
                "x": b.get("X", 0), "y": b.get("Y", 0),
                "w": b.get("Width", 0), "h": b.get("Height", 0),
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def front_window_rect() -> tuple | None:
    """最前面那个不属于我们的窗口矩形（游戏窗口）；读不到返回 None。"""
    return front_window_from_windows(_window_list())
