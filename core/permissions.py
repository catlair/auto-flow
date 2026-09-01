"""macOS 权限检测：辅助功能 + 输入监控 + 屏幕录制（截屏依赖）。"""
from __future__ import annotations

import subprocess

import ApplicationServices
from Foundation import NSMutableDictionary


def _retry_call(fn):
    """PyObjC 懒加载首访某些符号（如 AXIsProcessTrusted）可能抛 KeyError/AttributeError，

    重试一次通常即解析成功（框架首次载入后符号被缓存）。避免 sidecar 第一次
    app.info 即撞上 -32000。"""
    try:
        return fn()
    except (KeyError, AttributeError):
        return fn()


def check_accessibility(prompt: bool = False) -> bool:
    """检测辅助功能权限；prompt=True 时弹系统授权提示。"""
    try:
        if prompt:
            opts = NSMutableDictionary.dictionary()
            opts.setObject_forKey_(True, ApplicationServices.kAXTrustedCheckOptionPrompt)
            return bool(_retry_call(lambda: ApplicationServices.AXIsProcessTrustedWithOptions(opts)))
        return bool(_retry_call(lambda: ApplicationServices.AXIsProcessTrusted()))
    except Exception:  # noqa: BLE001
        # 任何意外（含懒加载失败）都降级为「未授权」，由授权提示引导用户，而非炸掉调用方
        return False


def check_input_monitoring() -> bool:
    """检测输入监控权限（CGEventTap 监听键盘）。

    macOS 10.15+ 用 CGPreflightTapRequestsAreNonUserApproved 预检，
    旧系统降级为「无法静态判断」→ 返回 True（实际是否可用由 keymap 监听失败推断）。
    """
    try:
        from Quartz import (  # type: ignore
            CGPreflightTapRequestsAreNonUserApproved,
        )
    except Exception:  # pragma: no cover - 旧系统无该 API
        return True
    try:
        return not bool(_retry_call(lambda: CGPreflightTapRequestsAreNonUserApproved()))
    except Exception:
        return True


def check_screen_recording(prompt: bool = False) -> bool:
    """检测屏幕录制权限（mss / Quartz 截屏、screencapture -i 依赖）。

    macOS 10.15+ 用 CGPreflightScreenCaptureAccess；prompt=True 时请求授权。
    旧系统无该 API → 返回 True（旧系统截屏多不需要该权限）。
    """
    try:
        from Quartz import (  # type: ignore
            CGPreflightScreenCaptureAccess,
            CGRequestScreenCaptureAccess,
        )
    except Exception:  # pragma: no cover - 旧系统无该 API
        return True
    try:
        if _retry_call(lambda: CGPreflightScreenCaptureAccess()):
            return True
        if prompt:
            CGRequestScreenCaptureAccess()
        return False
    except Exception:
        return True


def open_accessibility_settings() -> None:
    subprocess.Popen([
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ])


def open_input_monitoring_settings() -> None:
    subprocess.Popen([
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    ])


def open_screen_recording_settings() -> None:
    subprocess.Popen([
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    ])
