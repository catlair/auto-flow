"""键名映射：pynput Key/KeyCode ↔ 脚本键名（与 Tauri 版 macro-recorder 兼容）。"""
from __future__ import annotations

from pynput.keyboard import Key, KeyCode

# pynput Key.name → 脚本键名
_NAME_MAP = {
    "space": "Space", "enter": "Return", "esc": "Escape", "tab": "Tab",
    "backspace": "Backspace", "delete": "Delete", "home": "Home", "end": "End",
    "page_up": "PageUp", "page_down": "PageDown", "up": "UpArrow", "down": "DownArrow",
    "left": "LeftArrow", "right": "RightArrow", "cmd": "Command", "option": "Option",
    "alt_l": "Option", "alt_r": "Option", "ctrl": "Control", "ctrl_l": "Control",
    "ctrl_r": "Control", "shift": "Shift", "shift_l": "Shift", "shift_r": "Shift",
    "caps_lock": "CapsLock", "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10",
    "f11": "F11", "f12": "F12",
}

# 脚本键名 → pynput Key（回放用）
_REV_MAP = {v: k for k, v in _NAME_MAP.items()}


def key_to_name(key) -> str:
    """pynput 按键对象 → 脚本键名。"""
    if isinstance(key, Key):
        return _NAME_MAP.get(key.name, key.name.capitalize())
    if isinstance(key, KeyCode):
        if key.vk is not None and (key.char is None or key.char == ""):
            return f"vk:{key.vk}"
        return key.char or "?"
    return str(key)


def name_to_key(name: str):
    """脚本键名 → pynput 可按下的对象。"""
    if not name:
        return None
    if name.startswith("vk:"):
        try:
            return KeyCode.from_vk(int(name[3:]))
        except ValueError:
            return None
    if name in _REV_MAP:            # 脚本键名（Space/Return/UpArrow/F9…）
        pname = _REV_MAP[name]
        return Key[pname] if pname in Key.__members__ else None
    low = name.lower()
    if low in _NAME_MAP:            # 直接给了 pynput 名（space/enter/f9…）
        return Key[low] if low in Key.__members__ else None
    if len(name) == 1:
        return name
    if low in Key.__members__:      # 其它 pynput 原始名
        return Key[low]
    return None
