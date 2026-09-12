"""中文 / Unicode 文本输入。

全部用假投递器：`type_text(text, post=记录器)` 只构造 CGEvent 并交给记录器，
**不 CGEventPost**（那会真的往用户当前聚焦的窗口打字）。事件本身是真的
CGEvent 对象，所以可以用 CGEventKeyboardGetUnicodeString 读回内容来断言。
"""
from __future__ import annotations

import Quartz
import pytest

from core import mactype


def _read(ev) -> tuple[int, str, int]:
    """读回 (携带文本的长度, 文本, 键码)。

    ⚠️ 只能用来断言**确实挂了字符串**的事件。对没挂字符串的事件（纯键码的
    Return/Tab、空 keyUp），`CGEventKeyboardGetUnicodeString` 返回的是
    未定义垃圾值（实测 `(1, '\\r')`），不是 `(0, '')`——拿它当「不带文本」的
    依据会写出假断言。
    """
    n, text = Quartz.CGEventKeyboardGetUnicodeString(ev, 64, None, None)
    code = Quartz.CGEventGetIntegerValueField(ev, Quartz.kCGKeyboardEventKeycode)
    return int(n), str(text), int(code)


def _text_of(ev) -> str:
    """只对「文本事件」用：取它携带的字符串。"""
    return _read(ev)[1]


def _is_down(ev) -> bool:
    return Quartz.CGEventGetType(ev) == Quartz.kCGEventKeyDown


@pytest.fixture
def posted():
    """收集被投递的事件，不真正投递。"""
    events: list = []
    return events, events.append


# --------------------------------------------------------------------------- #
# UTF-16 长度：CGEventKeyboardSetUnicodeString 的长度参数
# --------------------------------------------------------------------------- #
def test_u16_len_counts_utf16_units_not_python_chars():
    """长度参数是 UTF-16 码元数。传错会静默截断，甚至劈开代理对。"""
    assert mactype.u16_len("abc") == 3
    assert mactype.u16_len("中文") == 2
    assert mactype.u16_len("🎯") == 2          # 代理对：Python 算 1 个字符，UTF-16 算 2
    assert mactype.u16_len("𝕏") == 2
    assert mactype.u16_len("emoji 🎯") == 8    # 6 + 1(空格) + 2


def test_chunks_respect_limit_and_never_split_surrogate_pairs():
    text = "🎯" * 25                              # 每个 2 码元
    chunks = mactype._chunks(text, limit=20)
    assert "".join(chunks) == text, "切块不能丢字符"
    assert all(mactype.u16_len(c) <= 20 for c in chunks)
    for c in chunks:
        assert c.encode("utf-16-le", "surrogatepass").decode("utf-16-le") == c


def test_chunks_keep_single_long_char_intact():
    """单个字符超过上限时也不能被丢掉。"""
    assert mactype._chunks("🎯", limit=1) == ["🎯"]


# --------------------------------------------------------------------------- #
# 投递内容
# --------------------------------------------------------------------------- #
def test_type_text_posts_text_then_empty_keyup(posted):
    events, post = posted
    n = mactype.type_text("中文", post=post)
    assert n == 2
    assert len(events) == 2
    assert _is_down(events[0]) and not _is_down(events[1])
    assert _read(events[0])[:2] == (2, "中文"), "keyDown 要带上整段文本"
    assert _read(events[0])[2] == 0, "文本事件不带键码"
    assert _read(events[1])[2] == 0, "keyUp 只用来释放（其文本读回不可靠，不断言）"


def test_type_text_round_trips_astral_characters(posted):
    """emoji / 数学字母这类星平面字符要完整送达（长度按 UTF-16 算）。"""
    events, post = posted
    text = "emoji 🎯 𝕏 混排"
    mactype.type_text(text, post=post)
    assert _text_of(events[0]) == text


def test_type_text_splits_into_multiple_events_for_long_text(posted):
    events, post = posted
    text = "中" * 50                              # 50 个 UTF-16 码元 → 3 块
    mactype.type_text(text, post=post)
    joined = "".join(_text_of(e) for e in events if _is_down(e))
    assert joined == text
    assert sum(1 for e in events if _is_down(e)) == 3


def test_type_text_sends_newline_and_tab_as_real_keys(posted):
    """控制字符仍走真实按键：旧行为是 \\n → Return、\\t → Tab。

    用 Unicode 通道发字面换行符多数应用不认，属行为回退，必须保持旧语义。
    """
    events, post = posted
    mactype.type_text("a\nb\tc", post=post)
    downs = [e for e in events if _is_down(e)]
    # 键码 0 = 文本事件；Return / Tab 是真键码
    assert [_read(e)[2] for e in downs] == [0, mactype._KVK_RETURN, 0, mactype._KVK_TAB, 0]
    assert [_text_of(e) for e in downs if _read(e)[2] == 0] == ["a", "b", "c"]


def test_type_text_normalises_crlf_to_a_single_return(posted):
    events, post = posted
    mactype.type_text("a\r\nb", post=post)
    downs = [e for e in events if _is_down(e)]
    assert [_read(e)[2] for e in downs] == [0, mactype._KVK_RETURN, 0]


def test_type_text_empty_string_posts_nothing(posted):
    events, post = posted
    assert mactype.type_text("", post=post) == 0
    assert events == []


# --------------------------------------------------------------------------- #
# 控制器接线
# --------------------------------------------------------------------------- #
def test_keyboard_controller_type_goes_through_unicode_channel(monkeypatch):
    """`MacKeyboardController.type()` 必须走 Unicode 通道，否则中文静默丢失。"""
    events: list = []
    monkeypatch.setattr(mactype, "_default_post", events.append)
    mactype.MacKeyboardController().type("中文输入")
    assert [e for e in events if _is_down(e)]
    assert _text_of(events[0]) == "中文输入"


def test_player_wires_the_unicode_capable_controller(monkeypatch):
    """接线检查：`core/player.py` 必须导入能打中文的控制器。

    改回 `pynput.keyboard.Controller` 时中文会静默失效且无任何报错。
    先 `monkeypatch.undo()` 撤掉 conftest 对 `player.KeyboardController` 的
    假控制器替换，否则断言的是那个桩。
    """
    from core import player

    monkeypatch.undo()
    assert player.KeyboardController is mactype.MacKeyboardController


def test_real_posting_is_blocked_by_default():
    """conftest 默认禁止真实键盘投递，免得测试往用户窗口里打字。"""
    with pytest.raises(RuntimeError, match="禁止真实键盘投递"):
        mactype.type_text("中文")
