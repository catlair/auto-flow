#!/usr/bin/env python3
"""Auto Flow 入口：权限检测 → 启动主窗口。

--check: 把 Finder 启动环境下的真实权限状态写入 诊断文件 后退出。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_check() -> int:
    import pprint
    import subprocess
    from core import permissions
    from core.paths import app_dir
    info = {
        "ax_trusted": permissions.check_accessibility(),
        "argv0": sys.argv[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "meipass": getattr(sys, "_MEIPASS", ""),
        "cwd": os.getcwd(),
    }
    try:
        out = subprocess.run(
            ["codesign", "-dv", sys.argv[0]], capture_output=True, text=True)
        info["codesign"] = (out.stderr or out.stdout).strip().splitlines()[:4]
    except Exception as e:
        info["codesign"] = str(e)
    with open(os.path.join(app_dir(), "ax_check.txt"), "w") as f:
        f.write(pprint.pformat(info))
    print(info)
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return run_check()

    from PySide6.QtWidgets import QApplication, QMessageBox
    from core import permissions

    app = QApplication(sys.argv)

    from ui.main_window import MainWindow  # noqa: F401  内部触发 tasks 注册
    import tasks.builtin  # noqa: F401

    trusted = permissions.check_accessibility()
    if not trusted:
        msg = QMessageBox()
        msg.setWindowTitle("需要辅助功能权限")
        msg.setText(
            "Auto Flow 需要「辅助功能」权限才能模拟鼠标键盘。\n"
            "请在 系统设置 → 隐私与安全性 → 辅助功能 中\n"
            "勾选 Auto Flow，然后回到本窗口点「重新检测」。")
        open_btn = msg.addButton("打开设置", QMessageBox.ActionRole)
        msg.addButton("稍后再说", QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() is open_btn:
            permissions.open_accessibility_settings()
        # 不退出：进主窗，横幅每 2 秒自动重检

    win = MainWindow()
    win.show()
    if not trusted:
        win.show_permission_banner()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
