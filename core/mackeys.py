"""macOS 虚拟键码 → 脚本键名（与 keymap 的脚本键名约定一致）。

打印字符 → 小写字符本身；功能/修饰键 → 脚本名（Space/Return/F9/UpArrow…）。
播放侧由 core.keymap.name_to_key 还原为 pynput 按键。
"""
from __future__ import annotations

# kVK_* → 脚本键名
VK_NAMES: dict[int, str] = {
    0x00: "a", 0x01: "s", 0x02: "d", 0x03: "f", 0x04: "h", 0x05: "g",
    0x06: "z", 0x07: "x", 0x08: "c", 0x09: "v", 0x0B: "b", 0x0C: "q",
    0x0D: "w", 0x0E: "e", 0x0F: "r", 0x10: "y", 0x11: "t",
    0x12: "1", 0x13: "2", 0x14: "3", 0x15: "4", 0x16: "6", 0x17: "5",
    0x18: "=", 0x19: "9", 0x1A: "7", 0x1B: "-", 0x1C: "8", 0x1D: "0",
    0x1E: "]", 0x1F: "o", 0x20: "u", 0x21: "[", 0x22: "i", 0x23: "p",
    0x24: "Return",
    0x25: "l", 0x26: "j", 0x27: "'", 0x28: "k", 0x29: ";", 0x2A: "\\",
    0x2B: ",", 0x2C: "/", 0x2D: "n", 0x2E: "m", 0x2F: ".",
    0x30: "Tab", 0x31: "Space", 0x32: "`", 0x33: "Backspace", 0x35: "Escape",
    0x36: "Command", 0x37: "Command", 0x38: "Shift", 0x39: "CapsLock",
    0x3A: "Option", 0x3B: "Control", 0x3C: "Shift", 0x3D: "Option", 0x3E: "Control",
    0x45: "KeypadPlus", 0x4B: "KeypadDivide", 0x4C: "KeypadEnter",
    0x4E: "KeypadMinus", 0x43: "KeypadMultiply", 0x47: "KeypadClear",
    0x41: "KeypadDecimal", 0x51: "KeypadEquals",
    0x52: "Keypad0", 0x53: "Keypad1", 0x54: "Keypad2", 0x55: "Keypad3",
    0x56: "Keypad4", 0x57: "Keypad5", 0x58: "Keypad6", 0x59: "Keypad7",
    0x5B: "Keypad8", 0x5C: "Keypad9",
    0x60: "F5", 0x61: "F6", 0x62: "F7", 0x63: "F3", 0x64: "F8", 0x65: "F9",
    0x66: "F20", 0x67: "F11", 0x69: "F13", 0x6A: "F14", 0x6B: "F10",
    0x6D: "F12", 0x6F: "F15", 0x76: "F4", 0x78: "F2", 0x7A: "F1",
    0x72: "Help", 0x73: "Home", 0x74: "PageUp", 0x75: "Delete",
    0x77: "End", 0x79: "PageDown", 0x7B: "LeftArrow", 0x7C: "RightArrow",
    0x7D: "DownArrow", 0x7E: "UpArrow",
}

# 修饰键：键码 → (脚本名, CGEventFlags 位)
# 规范位：CapsLock=0x10000 Shift=0x20000 Control=0x40000 Option=0x80000 Command=0x100000
# （低 0x1F 内是设备相关的左右键位，不用于边沿判断）
VK_MODIFIERS: dict[int, tuple[str, int]] = {
    0x38: ("Shift", 0x20000), 0x3C: ("Shift", 0x20000),
    0x3B: ("Control", 0x40000), 0x3E: ("Control", 0x40000),
    0x3A: ("Option", 0x80000), 0x3D: ("Option", 0x80000),
    0x37: ("Command", 0x100000), 0x36: ("Command", 0x100000),
    0x39: ("CapsLock", 0x10000),
}


def vk_to_name(keycode: int) -> str | None:
    if keycode in VK_NAMES:
        return VK_NAMES[keycode]
    if keycode in VK_MODIFIERS:
        return VK_MODIFIERS[keycode][0]
    return None


def modifier_edge(keycode: int, flags: int, last_flags: int) -> bool | None:
    """修饰键 FlagsChanged 边沿检测：True 按下 / False 释放 / None 忽略。"""
    if keycode not in VK_MODIFIERS:
        return None
    bit = VK_MODIFIERS[keycode][1]
    now, prev = bool(flags & bit), bool(last_flags & bit)
    if now and not prev:
        return True
    if prev and not now:
        return False
    return None
