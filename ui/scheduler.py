"""定时运行工作流：支持「每天时刻」与「固定间隔分钟」两种模式。"""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox, QTimeEdit, QSpinBox,
    QCheckBox, QDialogButtonBox, QLabel, QFileDialog,
)

from core.paths import workflows_dir


class Scheduler(QObject):
    fire = Signal(str)      # 要运行的工作流路径

    def __init__(self) -> None:
        super().__init__()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.enabled = False
        self.mode = "每天时刻"     # 每天时刻 / 固定间隔
        self.at_time = "09:00"
        self.interval_min = 30
        self.workflow_path = ""
        self._next: datetime | None = None

    def configure(self, mode: str, at_time: str, interval_min: int,
                  workflow_path: str, enabled: bool) -> None:
        self.mode, self.at_time = mode, at_time
        self.interval_min = interval_min
        self.workflow_path = workflow_path
        self.enabled = enabled and bool(workflow_path)
        if not self.enabled:
            self.timer.stop()
            self._next = None
            return
        now = datetime.now()
        if self.mode == "固定间隔":
            self._next = now + timedelta(minutes=self.interval_min)
        else:
            hh, mm = map(int, self.at_time.split(":"))
            self._next = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if self._next <= now:
                self._next += timedelta(days=1)
        self.timer.start(10_000)

    def next_fire_text(self) -> str:
        if not self.enabled or not self._next:
            return ""
        return self._next.strftime("%m-%d %H:%M")

    def _tick(self) -> None:
        if not self.enabled:
            return
        now = datetime.now()
        if self._next and now >= self._next:
            path = self.workflow_path
            if self.mode == "固定间隔":
                self._next = now + timedelta(minutes=self.interval_min)
            else:
                self._next += timedelta(days=1)
            self.fire.emit(path)


class ScheduleDialog(QDialog):
    def __init__(self, sched: Scheduler, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("定时运行")
        self.sched = sched
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.mode_cb = QComboBox(); self.mode_cb.addItems(["每天时刻", "固定间隔"])
        self.mode_cb.setCurrentText(sched.mode)
        from PySide6.QtWidgets import QTimeEdit
        self.time_edit = QTimeEdit()
        hh, mm = map(int, (sched.at_time or "09:00").split(":"))
        self.time_edit.setTime(QTime(hh, mm))
        self.interval_spin = QSpinBox(); self.interval_spin.setRange(1, 1440)
        self.interval_spin.setValue(sched.interval_min)
        self.path_edit = QLabel(sched.workflow_path or "（未选择）")
        from PySide6.QtWidgets import QPushButton
        pick = QPushButton("选择工作流…")
        pick.clicked.connect(self._pick)
        self.enable_cb = QCheckBox("启用定时")
        self.enable_cb.setChecked(sched.enabled)
        form.addRow("模式", self.mode_cb)
        form.addRow("每天时刻", self.time_edit)
        form.addRow("间隔(分钟)", self.interval_spin)
        form.addRow("工作流", self.path_edit)
        form.addRow("", pick)
        form.addRow("", self.enable_cb)
        lay.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._apply)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择工作流", workflows_dir(), "工作流 (*.json)")
        if path:
            self.path_edit.setText(path)

    def _apply(self) -> None:
        self.sched.configure(
            mode=self.mode_cb.currentText(),
            at_time=self.time_edit.time().toString("HH:mm"),
            interval_min=self.interval_spin.value(),
            workflow_path=self.path_edit.text(),
            enabled=self.enable_cb.isChecked(),
        )
        self.accept()
