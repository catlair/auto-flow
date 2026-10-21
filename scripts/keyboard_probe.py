#!/usr/bin/env python3
"""键盘注入方式探测：一次性试多种投递方式，看游戏认哪一种。

背景：真机上 Auto Flow 的「键盘输入」节点（pynput → CGEvent）在燕云十六声里
按 F 完全没反应，而同一套 CGEvent 鼠标点击每次都生效。怀疑游戏读的是 HID 原始
设备或只认某个特定的投递通道。

用法（先把游戏停在一个「按 F 有反应」的页面，例如结算页的 F 再来一局）：

    .venv/bin/python scripts/keyboard_probe.py

脚本会先数 5 秒让你把游戏点到前台，然后**每 3 秒**用一种方式按一次 F（vk=3），
同时把「第几步 / 用了什么方式 / 投递了几条事件 / 当时最前面的 App」写进
/tmp/keyboard_probe.log。

哪一步之后游戏有反应（比如回到排队），那一步的方式就是有效的——把日志发出来。

注意：这个脚本从终端跑，需要**终端 App 本身**有「辅助功能」权限（Auto Flow 的
权限不算）。如果日志里 trusted=False，先去系统设置给终端勾上再重试。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import Quartz
import AppKit

VK_F = 3          # kVK_ANSI_F
VK_SPACE = 49     # kVK_Space
LOG = "/tmp/keyboard_probe.log"
GAP = 3.0         # 每步之间的间隔（秒）


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def frontmost() -> str:
    try:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName())
    except Exception as e:  # noqa: BLE001
        return f"(读不到: {e})"


def permissions() -> str:
    out = []
    try:
        out.append("AXIsProcessTrusted=" + str(bool(Quartz.AXIsProcessTrusted())))
    except Exception:
        out.append("AXIsProcessTrusted=?")
    try:
        out.append("PostEventAccess=" + str(bool(Quartz.CGPreflightPostEventAccess())))
    except Exception:
        out.append("PostEventAccess=?")
    return " ".join(out)


def key_events(vk: int, unicode_char: str = "", source=None):
    down = Quartz.CGEventCreateKeyboardEvent(source, vk, True)
    up = Quartz.CGEventCreateKeyboardEvent(source, vk, False)
    if unicode_char:
        for ev in (down, up):
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(unicode_char), unicode_char)
    return down, up


def post_hid(events):
    for ev in events:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    return len(events)


def post_session(events):
    for ev in events:
        Quartz.CGEventPost(Quartz.kCGSessionEventTap, ev)
    return len(events)


def post_pid(pid: int):
    def _post(events):
        for ev in events:
            Quartz.CGEventPostToPid(pid, ev)
        return len(events)
    return _post


def post_hold(events):
    down, up = events
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.15)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    return 2


def post_osascript(events):
    subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to key code %d' % VK_F],
        check=False, capture_output=True)
    return 1


def game_pid() -> int:
    for pat in ("yysls", "燕云"):
        try:
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout
            pids = [int(x) for x in out.split()]
            if pids:
                return pids[-1]
        except Exception:
            pass
    return 0


def main() -> int:
    pid = game_pid()
    log("=== 键盘注入探测开始 ===")
    log("最前面的 App: " + frontmost())
    log("权限: " + permissions())
    log("游戏 pid: %d" % pid)
    log("5 秒后开始，请把游戏点到前台……")
    for i in (5, 4, 3, 2, 1):
        print("  %d" % i, end="", flush=True)
        time.sleep(1)
    print()

    steps = [
        ("1) vk 码 + HID tap（和 Auto Flow 现在等价）", key_events(VK_F), post_hid),
        ("2) vk 码 + Unicode 字符 + HID tap", key_events(VK_F, "f"), post_hid),
        ("3) vk 码 + Session tap", key_events(VK_F), post_session),
        ("4) vk 码 + PostToPid(游戏进程)", key_events(VK_F), post_pid(pid) if pid else post_hid),
        ("5) vk 码 + 按住 150ms", key_events(VK_F), post_hold),
        ("6) vk 码 + 显式 HID 事件源", key_events(VK_F, source=Quartz.CGEventSourceCreate(
            Quartz.kCGEventSourceStateHIDSystemState)), post_hid),
        ("7) 只发 Unicode 字符串（keycode=0）", key_events(0, "f"), post_hid),
        ("8) AppleScript System Events key code", [], post_osascript),
    ]

    for label, events, poster in steps:
        log("--- %s ---" % label)
        log("    投递前最前面的 App: " + frontmost())
        try:
            n = poster(events)
            log("    已投递 %d 条事件" % n)
        except Exception as e:  # noqa: BLE001
            log("    投递失败: %r" % (e,))
        time.sleep(GAP)

    log("=== 结束。哪一步之后游戏有反应，就告诉我那一步的编号 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
