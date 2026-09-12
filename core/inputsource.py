"""当前输入源（输入法）探测。

存在的理由：**中文录制能不能work，取决于输入法走哪条通道，而不是取决于我们的
代码。** 2026-09-12 实测（自建窗口 + 生产代码的 `_unicode_of` 打桩）：

    系统拼音（com.apple.inputmethod.SCIM.ITABC）输入 "nihao" → 输入框得到「你好」，
    而事件 tap 全程只看到 keycode 45/34/4/0/31/49 读出的 'n','i','h','a','o',' ',
    **没有任何一条事件携带 CJK 文本，也没有 keycode 0 + Unicode 的上屏事件**。

即系统拼音走的是 `insertText:`（IMK 直接把文本交给聚焦应用，不投递 CGEvent）。
事件通道实现（`is_text_commit` + 文本聚合）因此**原理上覆盖不到系统拼音**。

所以自检只报 {"alive": true, "text_alive": true} 是不够的——那会让人以为
"中文能录"，而实际永远录不到。必须把"当前是哪个输入法"一起报出来。

只用 TIS 的只读 API；HIToolbox 未随 pyobjc 分发，故直接用 ctypes 加载。
"""
from __future__ import annotations

import ctypes
from typing import Optional

_HITOOLBOX = ("/System/Library/Frameworks/Carbon.framework/Versions/A/"
              "Frameworks/HIToolbox.framework/HIToolbox")
_COREFOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UTF8 = 0x08000100

# 已知不经过事件层（上屏不投递 CGEvent）的输入源前缀。
# 依据：2026-09-12 实测，见模块 docstring。仅列**确认过**的，不做推测。
_NO_EVENT_CHANNEL = ("com.apple.inputmethod.SCIM",)

_libs = None


def _load():
    """惰性加载并声明签名（失败抛异常，由调用方吞掉）。"""
    global _libs
    if _libs is not None:
        return _libs
    cf = ctypes.CDLL(_COREFOUNDATION)
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.c_long, ctypes.c_uint32]
    ht = ctypes.CDLL(_HITOOLBOX)
    ht.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
    ht.TISCopyCurrentKeyboardInputSource.argtypes = []
    ht.TISGetInputSourceProperty.restype = ctypes.c_void_p
    ht.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    sym = lambda n: ctypes.c_void_p.in_dll(ht, n).value  # noqa: E731
    _libs = (cf, ht, sym("kTISPropertyInputSourceID"), sym("kTISPropertyLocalizedName"))
    return _libs


def _cfstr(cf, ref) -> Optional[str]:
    if not ref:
        return None
    buf = ctypes.create_string_buffer(2048)
    ok = cf.CFStringGetCString(ctypes.c_void_p(ref), buf, 2048, _UTF8)
    return buf.value.decode("utf-8", "replace") if ok else None


def current_input_source() -> Optional[dict]:
    """当前输入源 {'id', 'name'}；探测失败返回 None（绝不抛）。

    绝不抛是设计取舍：输入源探测只是**锦上添花**的诊断信息，任何环境问题
    （框架未加载、无 GUI 会话、API 变更）都不该让输入监控自检整体失败。
    """
    try:
        cf, ht, k_id, k_name = _load()
        src = ht.TISCopyCurrentKeyboardInputSource()
        if not src:
            return None
        get = lambda k: _cfstr(cf, ht.TISGetInputSourceProperty(ctypes.c_void_p(src), k))  # noqa: E731
        return {"id": get(k_id) or "", "name": get(k_name) or ""}
    except Exception:  # noqa: BLE001
        return None


def event_channel_unsupported(source_id: Optional[str]) -> Optional[bool]:
    """该输入源是否**已确认**不经过事件层。

    返回 True 表示已实测确认上屏不投递 CGEvent（本实现覆盖不到）；
    False 表示不在已知名单里（**不等于**一定支持，只是没确认过）；
    None 表示输入源未知。UI 必须区分"确认不支持"与"未知"，不能把未知说成可用。
    """
    if not source_id:
        return None
    return any(source_id.startswith(p) for p in _NO_EVENT_CHANNEL)
