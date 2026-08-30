#!/usr/bin/env python3
"""Auto Flow 入口：权限检测 → 启动主窗口。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core import permissions

    app = QApplication(sys.argv)
    if not permissions.check_accessibility():
        msg = QMessageBox()
        msg.setWindowTitle("需要辅助功能权限")
        msg.setText(
            "Auto Flow 需要「辅助功能」权限才能模拟鼠标键盘。\n"
            "请在 系统设置 → 隐私与安全性 → 辅助功能 中\n"
            "勾选本应用（或运行它的终端/IDE），然后重新运行。")
        open_btn = msg.addButton("打开设置", QMessageBox.ActionRole)
        msg.addButton("稍后", QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() is open_btn:
            permissions.open_accessibility_settings()
            return 1

    from ui.main_window import MainWindow  # noqa: F401  内部触发 tasks 注册
    import tasks.builtin  # noqa: F401
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
