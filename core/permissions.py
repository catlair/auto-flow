"""macOS 权限检测：辅助功能（模拟输入）+ 输入监控（监听输入）。"""
from __future__ import annotations

import subprocess

import ApplicationServices
from Foundation import NSMutableDictionary


def check_accessibility(prompt: bool = False) -> bool:
    """检测辅助功能权限；prompt=True 时弹系统授权提示。"""
    if prompt:
        opts = NSMutableDictionary.dictionary()
        opts.setObject_forKey_(True, ApplicationServices.kAXTrustedCheckOptionPrompt)
        return bool(ApplicationServices.AXIsProcessTrustedWithOptions(opts))
    return bool(ApplicationServices.AXIsProcessTrusted())


def open_accessibility_settings() -> None:
    subprocess.Popen([
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ])


def open_input_monitoring_settings() -> None:
    subprocess.Popen([
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    ])
