"""参数面板：根据节点定义通用渲染参数控件。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QFormLayout, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox,
    QCheckBox, QToolButton, QFileDialog,
)

from tasks.base import get_task


class EventsEditMixin:
    """events 类型参数：显示「N 个事件」摘要 + 编辑按钮打开 JSON 文件。"""
    pass


class ParamsPanel(QWidget):
    params_changed = Signal()
    capture_requested = Signal(object)      # 请求键盘捕获（传入目标 QLineEdit）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._form = QFormLayout(self)
        self._form.setContentsMargins(8, 8, 8, 8)
        self.widgets: dict[str, QWidget] = {}

    def build(self, node_type: str, params: dict) -> None:
        while self._form.count():
            item = self._form.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.widgets.clear()
        task = get_task(node_type)
        if task is None:
            return
        for d in task.definition()["params"]:
            key, ptype = d["key"], d["ptype"]
            value = params.get(key, d["default"])
            if ptype == "int":
                w = QSpinBox(); w.setRange(d["min"] or 0, d["max"] or 1_000_000)
                w.setValue(int(value or 0))
            elif ptype == "float":
                w = QDoubleSpinBox(); w.setRange(d["min"] if d["min"] is not None else 0.0,
                                                 d["max"] or 100.0)
                w.setDecimals(2); w.setSingleStep(0.1); w.setValue(float(value or 1.0))
            elif ptype == "bool":
                w = QCheckBox(); w.setChecked(bool(value))
            elif ptype == "select":
                w = QComboBox(); w.addItems(d["options"]); w.setCurrentText(str(value))
            elif ptype == "events":
                w = QLineEdit(f"{len(value or [])} 个事件"); w.setReadOnly(True)
                self._form.addRow(d["label"], w)
                self.widgets[key] = w
                continue
            elif ptype == "file":
                w = QLineEdit(str(value or ""))
                row = QWidget(); from PySide6.QtWidgets import QHBoxLayout
                lay = QHBoxLayout(row); lay.setContentsMargins(0, 0, 0, 0)
                lay.addWidget(w)
                browse = QToolButton(); browse.setText("选择…")
                browse.clicked.connect(lambda _=False, lw=w: self._browse_image(lw))
                shot = QToolButton(); shot.setText("截取模板")
                shot.setToolTip("框选屏幕一块区域保存为模板图（会填入路径）")
                shot.clicked.connect(lambda _=False, lw=w: self._snip_template(lw))
                lay.addWidget(browse); lay.addWidget(shot)
                self._form.addRow(d["label"], row)
                self.widgets[key] = w
                continue
            else:
                w = QLineEdit(str(value if value is not None else ""))
                if ptype == "text" and key == "keys":
                    btn = QToolButton(); btn.setText("捕获")
                    btn.clicked.connect(lambda _=False, lw=w: self.capture_requested.emit(lw))
                    row = QWidget(); from PySide6.QtWidgets import QHBoxLayout
                    lay = QHBoxLayout(row); lay.setContentsMargins(0, 0, 0, 0)
                    lay.addWidget(w); lay.addWidget(btn)
                    self._form.addRow(d["label"], row)
                    self.widgets[key] = w
                    continue
            w.setToolTip(d["tooltip"])
            self._form.addRow(d["label"], w)
            self.widgets[key] = w
        # 通用参数：执行条件（依赖最近一次「条件判断」节点结果）
        w = QComboBox(); w.addItems(["总是", "条件成立", "条件不成立"])
        w.setCurrentText(str(params.get("run_when", "总是")))
        w.setToolTip("配合「条件判断」节点：控制本节点在条件成立/不成立时才执行")
        self._form.addRow("执行条件", w)
        self.widgets["run_when"] = w

    def values(self) -> dict:
        out = {}
        for key, w in self.widgets.items():
            if isinstance(w, QSpinBox):
                out[key] = w.value()
            elif isinstance(w, QDoubleSpinBox):
                out[key] = round(w.value(), 2)
            elif isinstance(w, QCheckBox):
                out[key] = w.isChecked()
            elif isinstance(w, QComboBox):
                out[key] = w.currentText()
            elif isinstance(w, QLineEdit):
                txt = w.text()
                if txt.endswith(" 个事件") and txt[:-4].isdigit():
                    continue  # events 摘要保持原值
                out[key] = txt
        return out

    def update_events_count(self, key: str, count: int) -> None:
        w = self.widgets.get(key)
        if isinstance(w, QLineEdit):
            w.setText(f"{count} 个事件")

    # ---- 文件类参数辅助 ----
    def _browse_image(self, line_edit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择模板图片", "", "图片 (*.png *.jpg *.jpeg *.bmp)")
        if path:
            line_edit.setText(path)
            self.params_changed.emit()

    def _snip_template(self, line_edit) -> None:
        """调用 macOS 系统交互式框选截图，保存为模板文件并回填路径。"""
        import subprocess, time
        from core.paths import templates_dir
        path = os.path.join(templates_dir(), f"tpl_{int(time.time())}.png")
        # 最小化本窗口干扰；screencapture -i 阻塞直到用户框选完成
        proc = subprocess.Popen(["screencapture", "-i", "-o", path])
        def wait_done():
            if proc.poll() is None:
                QTimer.singleShot(300, wait_done)
                return
            if os.path.exists(path) and os.path.getsize(path) > 0:
                line_edit.setText(path)
                self.params_changed.emit()
        QTimer.singleShot(300, wait_done)
