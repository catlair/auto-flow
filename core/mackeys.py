"""macOS 虚拟键码 → 脚本键名（与 keymap 的脚本键名约定一致）。

打印字符 → 小写字符本身；功能/修饰键 → 脚本名（Space/Return/F9/UpArrow…）。
播放侧由 core.keymap.name_to_key 还原为 pynput 按键。

另含「这条键盘事件是不是一段文本的提交」的判据（见 `is_text_commit`）——
输入法上屏、以及任何 `CGEventKeyboardSetUnicodeString` 投递的文本，在事件层
表现为**一次带 Unicode 的按键**，不是逐字符的真实按键，必须区别对待。
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
    0x67: "F11", 0x69: "F13", 0x6A: "F16", 0x6B: "F14", 0x71: "F15",
    0x6D: "F10", 0x6F: "F12", 0x76: "F4", 0x78: "F2", 0x7A: "F1",
    0x40: "F17", 0x4F: "F18", 0x50: "F19", 0x5A: "F20",
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


def is_text_commit(keycode: int, text: str) -> bool:
    """这条键盘事件是「一段文本的提交」而不是一次物理按键吗？

    背景：输入法上屏、以及 `CGEventKeyboardSetUnicodeString` 投递的文本，在
    事件层都是**一次**带 Unicode 字符串的按键（keycode 通常是 0）。若当成普通
    按键记录，中文会被拆成拼音字母、emoji 会变成一串废键码，回放完全失真。

    **陷阱**：keycode 0 不是"无键码"——按 ANSI 布局它就是物理 `A` 键，物理 `A`
    键的事件同样读出 `'a'`。所以绝不能"keycode==0 即文本"。真正可靠的判据是
    **文本内容本身在物理上是不是单键能产生的**：

    - 文本为空 → 不是（没有内容的键盘事件没有文本语义）。
    - 多字符 → 是。单次物理按键产生不了两个字符。
    - 可打印 ASCII（0x20–0x7E）单字符 → **不是**。哪怕它来自输入法也是安全的：
      判成按键后按物理键回放，产出的字符完全相同（keycode 0 → `A` 键 → `'a'`）。
    - Apple 私有区（U+E000–U+F8FF）→ 不是。功能键（方向键等）在事件层用
      U+F700 段表示，按文本回放会打出一个乱码私用字符。
    - 其余（中文、emoji、控制字符之外的任意非 ASCII）→ 键码未知或为 0 时判定
      为文本；键码已知且是普通键时不算（例如瑞典布局 `å` 落在 `[` 的键码上，
      按物理键回放能还原成 `å`，比按文本投递更忠实）。

    结论：**这是一个"判错也不会改变行为"的保守判据**——每一条误判都对应一种
    等价的回放方式，不存在"判错就丢信息"的分支。
    """
    if not text:
        return False
    if len(text) > 1:
        return True
    cp = ord(text[0])
    if 0x20 <= cp <= 0x7E:
        return False
    if 0xE000 <= cp <= 0xF8FF:
        return False
    return keycode == 0 or keycode not in VK_NAMES

