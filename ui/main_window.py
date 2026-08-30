"""主窗口：节点列表 + 参数面板 + 运行控制 + 录制面板 + 全局热键。"""
from __future__ import annotations

import dataclasses
import threading

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QToolButton, QMenu, QFormLayout, QDoubleSpinBox, QSpinBox,
    QLineEdit, QTextEdit, QGroupBox, QFileDialog, QMessageBox, QSplitter,
    QLabel, QApplication, QComboBox,
)

from core.events import Workflow, Node, MacroEvent
from core.recorder import Recorder
from core.executor import Executor
from core import permissions
import tasks.builtin  # noqa: F401  触发节点注册
from tasks.base import all_definitions, get_task
from ui.params_panel import ParamsPanel
from core.paths import workflows_dir


class Signals(QObject):
    record_event = Signal(object)       # MacroEvent
    record_stopped = Signal(object)     # RecordResult
    node_running = Signal(int, str)
    play_progress = Signal(int, int)
    play_done = Signal(bool)
    status = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Auto Flow — macOS 工作流自动化")
        self.resize(1080, 720)

        self.wf = Workflow()
        self.signals = Signals()
        self.recorder: Recorder | None = None
        self.record_timer = QTimer(self)
        self.record_timer.setInterval(100)
        self.record_timer.timeout.connect(self._poll_record)
        self.executor = Executor()
        self.exec_thread: QThread | None = None
        self.base_pos = (0, 0)
        self.saved_path: str | None = None
        self._capturing_widget = None

        self._build_ui()
        self._build_hotkeys()
        self.signals.node_running.connect(self._on_node_running)
        self.signals.play_progress.connect(self._on_play_progress)
        self.signals.play_done.connect(self._on_play_done)
        self.signals.record_event.connect(self._on_record_event)
        self.signals.record_stopped.connect(self._on_record_stopped)
        self.signals.status.connect(self.statusBar().showMessage)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # 左列：运行控制 + 节点列表
        left = QVBoxLayout()
        root.addLayout(left, 3)

        run_box = QGroupBox("运行")
        form = QFormLayout(run_box)
        self.speed_spin = QDoubleSpinBox(); self.speed_spin.setRange(0.1, 20.0)
        self.speed_spin.setSingleStep(0.1); self.speed_spin.setValue(1.0)
        self.repeat_spin = QSpinBox(); self.repeat_spin.setRange(1, 9999); self.repeat_spin.setValue(1)
        self.base_x_edit = QSpinBox(); self.base_x_edit.setRange(-99999, 99999)
        self.base_y_edit = QSpinBox(); self.base_y_edit.setRange(-99999, 99999)
        pick_btn = QPushButton("取点 (F11)")
        pick_btn.clicked.connect(self.pick_base_point)
        base_row = QWidget(); bl = QHBoxLayout(base_row); bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.base_x_edit); bl.addWidget(self.base_y_edit); bl.addWidget(pick_btn)
        self.use_rel_check = None  # 相对开关在录制回放节点参数里
        form.addRow("速度倍率", self.speed_spin)
        form.addRow("整体循环", self.repeat_spin)
        form.addRow("基点", base_row)

        btn_row = QWidget(); rl = QHBoxLayout(btn_row); rl.setContentsMargins(0, 0, 0, 0)
        self.run_btn = QPushButton("▶ 运行 (F10)")
        self.run_btn.clicked.connect(self.toggle_run)
        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.clicked.connect(self.stop_run)
        self.stop_btn.setEnabled(False)
        rl.addWidget(self.run_btn); rl.addWidget(self.stop_btn)
        form.addRow(btn_row)
        left.addWidget(run_box)

        node_box = QGroupBox("工作流节点")
        nl = QVBoxLayout(node_box)
        add_btn = QToolButton(); add_btn.setText("＋ 添加节点")
        menu = QMenu(add_btn)
        for d in all_definitions():
            act = menu.addAction(d["name"])
            act.triggered.connect(lambda _=False, t=d["type"]: self.add_node(t))
        add_btn.setMenu(menu); add_btn.setPopupMode(QToolButton.InstantPopup)
        nl.addWidget(add_btn)
        self.node_list = QListWidget()
        self.node_list.currentRowChanged.connect(self._on_node_selected)
        nl.addWidget(self.node_list)
        node_btn_row = QWidget(); nbl = QHBoxLayout(node_btn_row); nbl.setContentsMargins(0, 0, 0, 0)
        for text, fn in (("上移", self._move_up), ("下移", self._move_down),
                         ("删除", self._delete_node), ("启用/停用", self._toggle_node)):
            b = QPushButton(text); b.clicked.connect(fn); nbl.addWidget(b)
        nl.addWidget(node_btn_row)
        left.addWidget(node_box, 1)

        # 中列：参数面板
        param_box = QGroupBox("节点参数")
        pl = QVBoxLayout(param_box)
        self.params_panel = ParamsPanel()
        self.params_panel.capture_requested.connect(self._start_capture)
        pl.addWidget(self.params_panel)
        root.addWidget(param_box, 2)

        # 右列：录制面板
        rec_box = QGroupBox("录制")
        recl = QVBoxLayout(rec_box)
        rec_row = QWidget(); rc = QHBoxLayout(rec_row); rc.setContentsMargins(0, 0, 0, 0)
        self.rec_btn = QPushButton("● 开始录制 (F9)")
        self.rec_btn.clicked.connect(self.toggle_record)
        rc.addWidget(self.rec_btn)
        recl.addWidget(rec_row)
        self.event_list = QListWidget()
        self.event_list.setMaximumWidth(360)
        recl.addWidget(self.event_list, 1)
        apply_row = QWidget(); ar = QHBoxLayout(apply_row); ar.setContentsMargins(0, 0, 0, 0)
        apply_btn = QPushButton("录制结果 → 新节点")
        apply_btn.clicked.connect(lambda: self._record_to_node(new_node=True))
        update_btn = QPushButton("→ 更新选中节点")
        update_btn.clicked.connect(lambda: self._record_to_node(new_node=False))
        ar.addWidget(apply_btn); ar.addWidget(update_btn)
        recl.addWidget(apply_row)
        root.addWidget(rec_box, 2)

        # 顶部菜单：保存/加载
        bar = self.menuBar()
        file_menu = bar.addMenu("文件")
        file_menu.addAction("新建", self.new_workflow)
        file_menu.addAction("打开…", self.load_workflow)
        file_menu.addAction("保存", self.save_workflow)
        file_menu.addAction("另存为…", self.save_workflow_as)
        help_menu = bar.addMenu("帮助")
        help_menu.addAction("macOS 权限说明", self.show_permissions)

        self.statusBar().showMessage("就绪")

    # ---------- 热键 ----------
    def _build_hotkeys(self) -> None:
        from pynput import keyboard
        self.hotkey_listener = keyboard.GlobalHotKeys({
            '<f9>': self.toggle_record,
            '<f10>': self.toggle_run,
            '<f11>': self.pick_base_point,
        })
        self.hotkey_listener.daemon = True
        self.hotkey_listener.start()

    # ---------- 节点管理 ----------
    def add_node(self, node_type: str) -> None:
        task = get_task(node_type)
        node = Node(type=node_type, params=task.defaults())
        self.wf.nodes.append(node)
        self._refresh_node_list()
        self.node_list.setCurrentRow(len(self.wf.nodes) - 1)

    def _refresh_node_list(self) -> None:
        self.node_list.blockSignals(True)
        self.node_list.clear()
        for n in self.wf.nodes:
            task = get_task(n.type)
            title = task.name if task else n.type
            mark = "" if n.enabled else "（已停用）"
            extra = ""
            if n.type == "record_replay":
                extra = f" · {len(n.params.get('events', []))} 事件"
            self.node_list.addItem(QListWidgetItem(f"{title}{mark}{extra}"))
        self.node_list.blockSignals(False)

    def _current_node(self) -> Node | None:
        row = self.node_list.currentRow()
        if 0 <= row < len(self.wf.nodes):
            return self.wf.nodes[row]
        return None

    def _on_node_selected(self, row: int) -> None:
        if 0 <= row < len(self.wf.nodes):
            node = self.wf.nodes[row]
            self.params_panel.build(node.type, node.params)

    def _commit_params(self) -> None:
        node = self._current_node()
        if node:
            node.params.update(self.params_panel.values())

    def _move_up(self) -> None:
        row = self.node_list.currentRow()
        if row > 0:
            self.wf.nodes[row - 1], self.wf.nodes[row] = self.wf.nodes[row], self.wf.nodes[row - 1]
            self._refresh_node_list(); self.node_list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self.node_list.currentRow()
        if 0 <= row < len(self.wf.nodes) - 1:
            self.wf.nodes[row + 1], self.wf.nodes[row] = self.wf.nodes[row], self.wf.nodes[row + 1]
            self._refresh_node_list(); self.node_list.setCurrentRow(row + 1)

    def _delete_node(self) -> None:
        row = self.node_list.currentRow()
        if 0 <= row < len(self.wf.nodes):
            del self.wf.nodes[row]
            self._refresh_node_list()
            self.params_panel.build("", {})

    def _toggle_node(self) -> None:
        node = self._current_node()
        if node:
            node.enabled = not node.enabled
            self._refresh_node_list()

    # ---------- 录制 ----------
    def toggle_record(self) -> None:
        if self.recorder:
            self._stop_record()
        else:
            self._start_record()

    def _start_record(self) -> None:
        self.recorder = Recorder()
        self.recorder.start()
        self.event_list.clear()
        self.rec_btn.setText("■ 停止录制 (F9)")
        self.record_timer.start()
        self.signals.status.emit("录制中…")

    def _stop_record(self) -> None:
        if not self.recorder:
            return
        self.recorder.stop()
        result = self.recorder.result()
        self.recorder = None
        self.record_timer.stop()
        self.rec_btn.setText("● 开始录制 (F9)")
        self.signals.record_stopped.emit(result)

    def _poll_record(self) -> None:
        if not self.recorder:
            return
        for ev in self.recorder.poll():
            self.signals.record_event.emit(ev)
        if self.recorder.result().stopped_by_limit:
            self._stop_record()

    def _on_record_event(self, ev: MacroEvent) -> None:
        self.event_list.addItem(QListWidgetItem(ev.describe()))
        self.event_list.scrollToBottom()

    def _on_record_stopped(self, result) -> None:
        self._last_record = result
        self.signals.status.emit(f"录制结束：{len(result.events)} 个事件，原点 ({result.origin_x},{result.origin_y})")

    def _record_to_node(self, new_node: bool) -> None:
        result = getattr(self, "_last_record", None)
        if not result:
            QMessageBox.information(self, "提示", "还没有录制结果")
            return
        events = [dataclasses.asdict(e) for e in result.events]
        if new_node:
            node = Node(type="record_replay", params={
                "events": events, "origin_x": result.origin_x, "origin_y": result.origin_y,
                "use_relative": True, "speed": 1.0, "repeat": 1,
            })
            self.wf.nodes.append(node)
            self._refresh_node_list()
            self.node_list.setCurrentRow(len(self.wf.nodes) - 1)
        else:
            node = self._current_node()
            if not node or node.type != "record_replay":
                QMessageBox.information(self, "提示", "请先选中一个「录制回放」节点")
                return
            node.params["events"] = events
            node.params["origin_x"] = result.origin_x
            node.params["origin_y"] = result.origin_y
            self._refresh_node_list()

    # ---------- 取点 ----------
    def pick_base_point(self) -> None:
        from pynput.mouse import Controller
        pos = Controller().position
        self.base_pos = (int(pos[0]), int(pos[1]))
        self.base_x_edit.setValue(self.base_pos[0])
        self.base_y_edit.setValue(self.base_pos[1])
        self.signals.status.emit(f"基点已取：{self.base_pos}")

    # ---------- 键盘捕获 ----------
    def _start_capture(self, widget) -> None:
        if self._capturing_widget:
            return
        self._capturing_widget = widget
        widget.setText("按任意键…")
        from pynput import keyboard
        self._capture_listener = keyboard.Listener(on_release=self._on_capture_key)
        self._capture_listener.start()

    def _on_capture_key(self, key) -> None:
        from core.keymap import key_to_name
        name = key_to_name(key)
        w = self._capturing_widget
        self._capturing_widget = None
        self._capture_listener.stop()
        if w:
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(w, "setText", Qt.QueuedConnection, Q_ARG(str, name))

    # ---------- 运行 ----------
    def toggle_run(self) -> None:
        if self.executor.running:
            self.stop_run()
            return
        self._commit_params()
        if not self.wf.nodes:
            QMessageBox.information(self, "提示", "工作流为空，请先添加节点")
            return
        base = (self.base_x_edit.value(), self.base_y_edit.value())
        self.wf.speed = self.speed_spin.value()
        self.wf.repeat = self.repeat_spin.value()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.signals.status.emit("运行中…")
        self.exec_thread = QThread(parent=self)
        worker = _ExecWorker(self.executor, self.wf, base, self.signals)
        worker.moveToThread(self.exec_thread)
        self.exec_thread.started.connect(worker.run)
        worker.finished.connect(self.exec_thread.quit)
        self._exec_worker = worker
        self.exec_thread.start()

    def stop_run(self) -> None:
        self.executor.stop_run()
        self.signals.status.emit("正在停止…")

    def _on_node_running(self, idx: int, ntype: str) -> None:
        self.node_list.setCurrentRow(idx)
        self.signals.status.emit(f"运行节点 {idx + 1}: {ntype}")

    def _on_play_progress(self, done: int, total: int) -> None:
        self.signals.status.emit(f"回放进度 {done}/{total}")

    def _on_play_done(self, stopped: bool) -> None:
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.signals.status.emit("已停止" if stopped else "运行完成")

    # ---------- 文件 ----------
    def new_workflow(self) -> None:
        self.wf = Workflow(name="未命名工作流")
        self.saved_path = None
        self._refresh_node_list()
        self.params_panel.build("", {})

    def save_workflow(self) -> None:
        if self.saved_path:
            self._commit_params()
            self.wf.save(self.saved_path)
            self.signals.status.emit(f"已保存 {self.saved_path}")
        else:
            self.save_workflow_as()

    def save_workflow_as(self) -> None:
        self._commit_params()
        path, _ = QFileDialog.getSaveFileName(self, "保存工作流", workflows_dir(), "工作流 (*.json)")
        if path:
            self.wf.save(path)
            self.saved_path = path
            self.signals.status.emit(f"已保存 {path}")

    def load_workflow(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "打开工作流", workflows_dir(), "工作流 (*.json)")
        if not path:
            return
        try:
            self.wf = Workflow.load(path)
        except Exception as e:
            QMessageBox.warning(self, "加载失败", str(e))
            return
        self.saved_path = path
        self.speed_spin.setValue(self.wf.speed)
        self.repeat_spin.setValue(self.wf.repeat)
        self._refresh_node_list()
        if self.wf.nodes:
            self.node_list.setCurrentRow(0)
        self.signals.status.emit(f"已加载 {path}")

    def show_permissions(self) -> None:
        ok = permissions.check_accessibility()
        msg = "辅助功能权限：" + ("已授予 ✓" if ok else "未授予 ✗（模拟输入将无效）")
        msg += "\n\n需要同时在 系统设置→隐私与安全性 中授予：\n· 辅助功能（回放/模拟输入）\n· 输入监控（录制监听）"
        box = QMessageBox(self)
        box.setWindowTitle("权限")
        box.setText(msg)
        btn = box.addButton("打开辅助功能设置", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if box.clickedButton() is btn:
            permissions.open_accessibility_settings()

    def closeEvent(self, event) -> None:
        self.executor.stop_run()
        if self.recorder:
            self.recorder.stop()
        try:
            self.hotkey_listener.stop()
        except Exception:
            pass
        super().closeEvent(event)


class _ExecWorker(QObject):
    finished = Signal()

    def __init__(self, executor: Executor, wf: Workflow, base: tuple, signals: Signals) -> None:
        super().__init__()
        self.executor = executor
        self.wf = wf
        self.base = base
        self.signals = signals

    def run(self) -> None:
        self.executor.run_workflow(
            self.wf, self.base[0], self.base[1],
            on_node=lambda i, t: self.signals.node_running.emit(i, t),
            on_progress=lambda d, t: self.signals.play_progress.emit(d, t),
            on_done=lambda stopped: self.signals.play_done.emit(stopped),
        )
        self.finished.emit()
