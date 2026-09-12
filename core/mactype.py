"""macOS 任意 Unicode 文本输入（中文 / emoji / 数学字母都能打）。

**为什么需要这个**：`pynput` 的 `Controller.type()` 走的是「字符 → 虚拟键码」
映射，只覆盖 ASCII。填中文时它**静默跳过**——「键盘输入-文本」节点看起来执行了，
目标框里什么都没有，也不报错。

**做法**：改用 `CGEventKeyboardSetUnicodeString`，把文本直接挂在一个键盘事件上
投递，绕开键码映射。

两个容易踩的点（都实测过）：

1. `CGEventKeyboardSetUnicodeString(event, length, text)` 的 `length` 是
   **UTF-16 码元数**，不是 Python 的字符数。`len("emoji 🎯")` 是 8，但 UTF-16
   要 9 个码元（代理对占 2）。传错会**静默截断**，甚至把代理对劈成半个字符。
   用 `u16_len()` 算。
2. 事件层实测没有 20 码元的上限（36 码元也能完整往返），但**接收方**普遍只处理
   每个事件的前若干字符，所以仍按 20 码元切块，且切块时绝不切开代理对。

控制字符（`\\n` / `\\t`）仍按**真实按键**投递：pynput 的旧行为就是
`\\n` → Return、`\\t` → Tab，用 Unicode 通道发字面换行符多数应用不认，
属行为回退，这里保持旧语义。
"""
from __future__ import annotations

import Quartz
from pynput.keyboard import Controller as _PynputKeyboard

# 每个事件最多携带的 UTF-16 码元数（接收方兼容性，不是事件层的限制）
MAX_U16_PER_EVENT = 20

# 虚拟键码：None 表示「这个事件不带键码，只带 Unicode 字符串」
_KVK_NONE = 0
_KVK_RETURN = 0x24
_KVK_TAB = 0x30

# 控制字符 → 真实按键（保持 pynput type() 的旧语义）
_CONTROL_KEYCODES = {"\n": _KVK_RETURN, "\t": _KVK_TAB}


def u16_len(text: str) -> int:
    """UTF-16 码元数——`CGEventKeyboardSetUnicodeString` 的长度参数要的是这个。"""
    return len(text.encode("utf-16-le")) // 2


def _chunks(text: str, limit: int = MAX_U16_PER_EVENT) -> list[str]:
    """按 UTF-16 码元数切块，**绝不切开代理对**（切开会产生半个字符）。"""
    out: list[str] = []
    buf: list[str] = []
    used = 0
    for ch in text:
        n = u16_len(ch)
        if buf and used + n > limit:
            out.append("".join(buf))
            buf, used = [], 0
        buf.append(ch)
        used += n
    if buf:
        out.append("".join(buf))
    return out


def _post_text(chunk: str, post) -> None:
    """投递一段文本：keyDown 携带字符串，再配一个空 keyUp。"""
    down = Quartz.CGEventCreateKeyboardEvent(None, _KVK_NONE, True)
    Quartz.CGEventKeyboardSetUnicodeString(down, u16_len(chunk), chunk)
    post(down)
    post(Quartz.CGEventCreateKeyboardEvent(None, _KVK_NONE, False))


def _post_key(keycode: int, post) -> None:
    post(Quartz.CGEventCreateKeyboardEvent(None, keycode, True))
    post(Quartz.CGEventCreateKeyboardEvent(None, keycode, False))


def _default_post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def type_text(text: str, post=None) -> int:
    """输入任意 Unicode 文本，返回投递的事件数。

    `post` 是投递函数的注入点（测试用；生产走 `CGEventPost`）。
    """
    if not text:
        return 0
    post = post or _default_post
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    count = 0
    buf: list[str] = []

    def flush() -> None:
        nonlocal count, buf
        for chunk in _chunks("".join(buf)):
            _post_text(chunk, post)
            count += 2
        buf = []

    for ch in text:
        if ch in _CONTROL_KEYCODES:
            flush()
            _post_key(_CONTROL_KEYCODES[ch], post)
            count += 2
        else:
            buf.append(ch)
    flush()
    return count


class MacKeyboardController(_PynputKeyboard):
    """pynput 键盘控制器的替代：`type()` 走 CGEvent Unicode 通道。

    只覆盖 `type()`；`press()` / `release()` 仍用 pynput 的键码实现
    （组合键、单键本来就是键码语义，且 pynput 那条路径是验证过的）。
    """

    def type(self, text) -> None:  # type: ignore[override]
        type_text(str(text))
