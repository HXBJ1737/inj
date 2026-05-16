"""Single pump control widget — touchscreen-friendly UI with debug/run modes."""

import csv
import os
import re
import time
from PyQt5.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QGridLayout, QDialog, QCheckBox, QWidget,
    QStackedWidget, QComboBox, QProgressBar,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from pump_controller import PumpController

# ── Syringe data loader ────────────────────────────────────────

_SYRINGE_DB = {}  # { "1mL": {"volume_ml": 1, "length_um": 62000}, ... }

def _load_syringe_db():
    csv_path = os.path.join(os.path.dirname(__file__), "data.csv")
    if not os.path.isfile(csv_path):
        return
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Strip whitespace from both keys and values
            row = {k.strip(): v.strip() for k, v in row.items()}
            model = row["型号"]
            volume_ml = float(row["体积_mL"])
            length_um = float(row["满刻度长度_um"])
            _SYRINGE_DB[model] = {"volume_ml": volume_ml, "length_um": length_um}

_load_syringe_db()

_global_scale = 1.0


def scale_stylesheet(ss: str, scale: float = None) -> str:
    """Scale all px values in a Qt stylesheet by the given factor."""
    if scale is None:
        scale = _global_scale
    if scale == 1.0:
        return ss
    return re.sub(
        r"(\d+)px",
        lambda m: f"{max(1, round(int(m.group(1)) * scale))}px",
        ss,
    )


# ── Numpad dialog ──────────────────────────────────────────────

class NumpadDialog(QDialog):
    """Touchscreen numeric keypad popup."""

    def __init__(self, title="", value="", parent=None, allow_negative=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self._allow_negative = allow_negative
        self._result = value
        self._init_ui()

    def _init_ui(self):
        self._numpad_base_style = """
            QDialog { background: #2b2b2b; }
            QPushButton {
                font-size: 22px; min-width: 64px; min-height: 54px;
                border-radius: 6px; border: 1px solid #555;
                background: #3c3f41; color: white;
            }
            QPushButton:pressed { background: #5294e2; }
            QLineEdit {
                font-size: 26px; min-height: 50px;
                background: #1e1e1e; color: #00ff00;
                border: 2px solid #5294e2; border-radius: 6px;
                padding: 4px 10px;
            }
        """
        self.setStyleSheet(scale_stylesheet(self._numpad_base_style))
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        self.display = QLineEdit(self._result)
        self.display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.display.setReadOnly(True)
        layout.addWidget(self.display)

        buttons = [
            ["7", "8", "9", "C"],
            ["4", "5", "6", "←"],
            ["1", "2", "3", "-"],
            ["0", ".", "OK", "+"],
        ]
        grid = QGridLayout()
        grid.setSpacing(4)
        for r, row in enumerate(buttons):
            for c, text in enumerate(row):
                btn = QPushButton(text)
                if text == "OK":
                    btn.setStyleSheet(
                        "QPushButton { background: #4e9a06; font-weight: bold; }"
                        "QPushButton:pressed { background: #3d7d05; }"
                    )
                elif text == "C":
                    btn.setStyleSheet(
                        "QPushButton { background: #a40000; }"
                        "QPushButton:pressed { background: #cc0000; }"
                    )
                btn.clicked.connect(lambda _, t=text: self._on_key(t))
                grid.addWidget(btn, r, c)
        layout.addLayout(grid)

    def _on_key(self, key):
        txt = self.display.text()
        if key == "OK":
            self._result = txt
            self.accept()
        elif key == "C":
            self.display.clear()
        elif key == "←":
            self.display.setText(txt[:-1])
        elif key == "-":
            if self._allow_negative:
                if txt.startswith("-"):
                    self.display.setText(txt[1:])
                else:
                    self.display.setText("-" + txt)
        elif key == ".":
            if "." not in txt:
                self.display.setText(txt + ".")
        elif key == "+":
            pass
        else:
            self.display.setText(txt + key)

    def value(self) -> str:
        return self._result


# ── Touch-friendly line edit ───────────────────────────────────

class TouchLineEdit(QLineEdit):
    """QLineEdit that opens a numpad on click (touchscreen friendly)."""

    _dialog_open = False

    def __init__(self, text="", allow_negative=False, parent=None, on_value_set=None):
        super().__init__(text, parent)
        self._allow_negative = allow_negative
        self._on_value_set = on_value_set
        self.setReadOnly(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not TouchLineEdit._dialog_open:
            self._open_numpad()

    def _open_numpad(self):
        TouchLineEdit._dialog_open = True
        try:
            dlg = NumpadDialog(
                title="输入数值",
                value=self.text(),
                parent=self.window(),
                allow_negative=self._allow_negative,
            )
            if dlg.exec_() == QDialog.Accepted:
                self.setText(dlg.value())
                if self._on_value_set:
                    self._on_value_set()
        finally:
            TouchLineEdit._dialog_open = False


# ── Poll worker ────────────────────────────────────────────────

class PollWorker(QThread):
    """Background thread that polls pump status and position periodically."""
    status_updated = pyqtSignal(int, int)
    position_updated = pyqtSignal(int, float)

    def __init__(self, widgets: list, interval_ms: int = 500):
        super().__init__()
        self.widgets = widgets
        self.interval = interval_ms / 1000.0
        self._running = True

    def run(self):
        while self._running:
            for widget in self.widgets:
                if not self._running:
                    break
                if not widget._enabled:
                    continue
                pump = widget.pump
                try:
                    status = pump.get_status()
                    if status is not None:
                        self.status_updated.emit(pump.addr, status)
                    pos = pump.get_current_position()
                    if pos is not None:
                        self.position_updated.emit(pump.addr, pos)
                except Exception:
                    pass
            time.sleep(self.interval)

    def stop(self):
        self._running = False
        self.wait(3000)


# ── Styles ─────────────────────────────────────────────────────

_GROUP_STYLE = """
QGroupBox {
    font-size: 13px; font-weight: bold;
    border: 1px solid #555; border-radius: 6px;
    margin-top: 8px; padding: 6px 4px 4px 4px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px; padding: 0 4px;
}
"""

_LABEL_STYLE = "font-size: 15px;"

_INPUT_STYLE = """
    font-size: 13px; min-height: 40px;
    background: #1e1e1e; color: #00ff00;
    border: 1px solid #555; border-radius: 4px;
    padding: 2px 4px;
"""

_BTN_SET_STYLE = """
    QPushButton {
        font-size: 13px; min-height: 40px; min-width: 44px;
        background: #3574a5; color: white;
        border: 1px solid #2a5a80; border-radius: 5px;
    }
    QPushButton:pressed { background: #2a5a80; }
"""

_BTN_CTRL_STYLE = """
    QPushButton {{
        font-size: 13px; font-weight: bold;
        min-height: 36px;
        background: {bg}; color: white;
        border: 1px solid {border}; border-radius: 5px;
    }}
    QPushButton:pressed {{ background: {pressed}; }}
"""

_STATUS_STYLE = "font-size: 14px; font-weight: bold; padding: 2px;"

_COMBO_STYLE = """
    QComboBox { font-size: 14px; min-height: 36px;
        background: #1e1e1e; color: #00ff00;
        border: 1px solid #555; border-radius: 4px; padding: 2px 6px; }
    QComboBox::drop-down { width: 30px; }
    QComboBox QAbstractItemView {
        font-size: 14px; min-height: 36px;
        background: #2b2b2b; color: white; selection-background-color: #3574a5;
    }
"""

_PROGRESS_STYLE = """
    QProgressBar { font-size: 13px; min-height: 24px;
        border: 1px solid #555; border-radius: 4px; background: #5e5e5e; text-align: center; }
    QProgressBar::chunk { background: #4e9a06; border-radius: 3px; }
"""


# ── Pump widget ────────────────────────────────────────────────

class PumpWidget(QGroupBox):
    """UI widget for controlling a single syringe pump (touchscreen)."""

    feedback = pyqtSignal(str)

    def __init__(self, pump: PumpController, parent=None):
        super().__init__(parent)
        self.pump = pump
        self._enabled = False
        self._run_mode = False  # False=debug, True=run
        self._run_total_um = 0.0
        self._run_speed_um_s = 0.0
        self._run_start_pos = 0.0
        self._current_pos = 0.0
        self._current_status = 0  # 0=idle,1=running,2=paused
        self._font_scale = 1.0
        self._style_registry: list[tuple[object, str]] = []  # (widget, base_stylesheet)
        self.setTitle(f"泵 {pump.addr:02d}")
        self.setStyleSheet(_GROUP_STYLE)
        self._init_ui()
        self._set_children_enabled(False)
        self._record_base_styles()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        # --- Enable + mode row ---
        top_row = QHBoxLayout()
        self.enable_cb = QCheckBox("启用")
        self.enable_cb.setStyleSheet(
            "QCheckBox { font-size: 14px; font-weight: bold; padding: 4px; spacing: 6px; }"
            "QCheckBox::indicator { width: 22px; height: 22px; }"
        )
        self.enable_cb.toggled.connect(self._toggle_enable)
        top_row.addWidget(self.enable_cb)

        top_row.addStretch()

        self.btn_debug = QPushButton("调试")
        self.btn_debug.setCheckable(True)
        self.btn_debug.setChecked(True)
        self.btn_debug.setFixedWidth(60)
        self.btn_debug.setStyleSheet(_BTN_SET_STYLE)
        self.btn_run = QPushButton("运行")
        self.btn_run.setCheckable(True)
        self.btn_run.setFixedWidth(60)
        self.btn_run.setStyleSheet(_BTN_SET_STYLE)
        self.btn_debug.clicked.connect(lambda: self._switch_mode(False))
        self.btn_run.clicked.connect(lambda: self._switch_mode(True))
        top_row.addWidget(self.btn_debug)
        top_row.addWidget(self.btn_run)
        layout.addLayout(top_row)

        # --- Stacked pages ---
        self.stack = QStackedWidget()

        # Page 0: Debug mode
        self.stack.addWidget(self._build_debug_page())
        # Page 1: Run mode
        self.stack.addWidget(self._build_run_page())

        layout.addWidget(self.stack)

        layout.addStretch()

    # ── Debug page ──

    def _build_debug_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(2)

        def add_input(row, label_text, default, allow_neg=False):
            lbl = QLabel(label_text)
            lbl.setStyleSheet(_LABEL_STYLE)
            grid.addWidget(lbl, row, 0)
            inp = TouchLineEdit(default, allow_negative=allow_neg)
            inp.setStyleSheet(_INPUT_STYLE)
            grid.addWidget(inp, row, 1, 1, 2)
            return inp

        self.speed_input = add_input(0, "速度 (um/s):", "1000")
        self.accel_input = add_input(1, "加速度 (1-100):", "80")
        self.incr_input = add_input(2, "增量位移 (um):", "1000", allow_neg=True)

        # Absolute position (has its own button, not part of start)
        lbl = QLabel("绝对位置 (um):")
        lbl.setStyleSheet(_LABEL_STYLE)
        grid.addWidget(lbl, 3, 0)
        self.abs_input = TouchLineEdit("0", allow_negative=True)
        self.abs_input.setStyleSheet(_INPUT_STYLE)
        grid.addWidget(self.abs_input, 3, 1)
        btn_abs = QPushButton("移动")
        btn_abs.setStyleSheet(_BTN_SET_STYLE)
        btn_abs.clicked.connect(self._set_absolute)
        grid.addWidget(btn_abs, 3, 2)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        btn_zero = QPushButton("设为零点")
        btn_zero.setStyleSheet(_BTN_SET_STYLE.replace("#3574a5", "#75507b").replace("#2a5a80", "#5c3d65"))
        btn_zero.clicked.connect(self._set_zero)
        layout.addWidget(btn_zero)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(4)

        btn = QPushButton("开始")
        btn.setStyleSheet(_BTN_CTRL_STYLE.format(
            bg="#4e9a06", border="#3d7d05", pressed="#2d5e04"))
        btn.clicked.connect(self._start)
        ctrl.addWidget(btn)

        self.btn_pause_resume = QPushButton("暂停")
        self.btn_pause_resume.setStyleSheet(_BTN_CTRL_STYLE.format(
            bg="#f57900", border="#ce5c00", pressed="#a84b00"))
        self.btn_pause_resume.clicked.connect(self._toggle_pause)
        self.btn_pause_resume.setEnabled(False)
        ctrl.addWidget(self.btn_pause_resume)

        btn = QPushButton("停止")
        btn.setStyleSheet(_BTN_CTRL_STYLE.format(
            bg="#cc0000", border="#a40000", pressed="#800000"))
        btn.clicked.connect(self._stop)
        ctrl.addWidget(btn)

        layout.addLayout(ctrl)

        self.status_label = QLabel("状态: --")
        self.status_label.setStyleSheet(_STATUS_STYLE)
        layout.addWidget(self.status_label)

        self.position_label = QLabel("位置: -- um")
        self.position_label.setStyleSheet(_STATUS_STYLE)
        layout.addWidget(self.position_label)

        return page

    # ── Run page ──

    def _build_run_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)

        # Syringe selector
        lbl1 = QLabel("注射器:")
        lbl1.setStyleSheet(_LABEL_STYLE)
        grid.addWidget(lbl1, 0, 0)
        self.syringe_combo = QComboBox()
        self.syringe_combo.setStyleSheet(_COMBO_STYLE)
        for model in _SYRINGE_DB:
            self.syringe_combo.addItem(model)
        if _SYRINGE_DB:
            self.syringe_combo.setCurrentIndex(0)
        grid.addWidget(self.syringe_combo, 0, 1)
        self.syringe_info = QLabel("")
        self.syringe_info.setStyleSheet(_LABEL_STYLE)
        grid.addWidget(self.syringe_info, 0, 2)
        self.syringe_combo.currentTextChanged.connect(self._on_syringe_changed)

        # Volume input (mL)
        lbl2 = QLabel("注射量 (mL):")
        lbl2.setStyleSheet(_LABEL_STYLE)
        grid.addWidget(lbl2, 1, 0)
        self.run_volume_input = TouchLineEdit("1.0")
        self.run_volume_input.setStyleSheet(_INPUT_STYLE)
        grid.addWidget(self.run_volume_input, 1, 1)
        self._vol_um_label = QLabel("")
        self._vol_um_label.setStyleSheet(_LABEL_STYLE)
        grid.addWidget(self._vol_um_label, 1, 2)

        # Speed input (mL/min)
        lbl3 = QLabel("速度 (mL/min):")
        lbl3.setStyleSheet(_LABEL_STYLE)
        grid.addWidget(lbl3, 2, 0)
        self.run_speed_input = TouchLineEdit("1.0")
        self.run_speed_input.setStyleSheet(_INPUT_STYLE)
        grid.addWidget(self.run_speed_input, 2, 1)
        self._spd_ums_label = QLabel("")
        self._spd_ums_label.setStyleSheet(_LABEL_STYLE)
        grid.addWidget(self._spd_ums_label, 2, 2)

        # Column 0 (labels) tight, column 1 (inputs) stretchy, column 2 (info) auto
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 0)

        layout.addLayout(grid)

        # Start / Stop
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_run_start = QPushButton("开始运行")
        self.btn_run_start.setStyleSheet(_BTN_CTRL_STYLE.format(
            bg="#4e9a06", border="#3d7d05", pressed="#2d5e04"))
        self.btn_run_start.clicked.connect(self._run_start)
        btn_row.addWidget(self.btn_run_start)

        self.btn_run_pause_resume = QPushButton("暂停")
        self.btn_run_pause_resume.setStyleSheet(_BTN_CTRL_STYLE.format(
            bg="#f57900", border="#ce5c00", pressed="#a84b00"))
        self.btn_run_pause_resume.clicked.connect(self._toggle_pause)
        self.btn_run_pause_resume.setEnabled(False)
        btn_row.addWidget(self.btn_run_pause_resume)

        self.btn_run_stop = QPushButton("停止")
        self.btn_run_stop.setStyleSheet(_BTN_CTRL_STYLE.format(
            bg="#cc0000", border="#a40000", pressed="#800000"))
        self.btn_run_stop.clicked.connect(self._run_stop)
        btn_row.addWidget(self.btn_run_stop)
        layout.addLayout(btn_row)

        self.run_status_label = QLabel("状态: --")
        self.run_status_label.setStyleSheet(_STATUS_STYLE)
        layout.addWidget(self.run_status_label)

        self.run_position_label = QLabel("位置: -- um")
        self.run_position_label.setStyleSheet(_STATUS_STYLE)
        layout.addWidget(self.run_position_label)

        self.run_progress_bar = QProgressBar()
        self.run_progress_bar.setRange(0, 100)
        self.run_progress_bar.setValue(0)
        self.run_progress_bar.setFormat("已注射 0 mL")
        self.run_progress_bar.setStyleSheet(_PROGRESS_STYLE)
        self.run_progress_bar.setVisible(False)
        layout.addWidget(self.run_progress_bar)

        self.run_remain_label = QLabel("")
        self.run_remain_label.setStyleSheet(_STATUS_STYLE)
        self.run_remain_label.setVisible(False)
        layout.addWidget(self.run_remain_label)

        # Trigger initial calculation
        self._on_syringe_changed(self.syringe_combo.currentText())

        # Update conversion labels on input change
        self.run_volume_input.textChanged.connect(self._update_run_conversion)
        self.run_speed_input.textChanged.connect(self._update_run_conversion)

        return page

    # ── Debug mode commands ──

    def _set_zero(self):
        ok = self.pump.set_current_position_zero()
        self.feedback.emit(f"泵{self.pump.addr}: 设为零点 {'成功' if ok else '失败'}")

    def _set_absolute(self):
        addr = self.pump.addr
        try:
            speed_val = float(self.speed_input.text())
        except ValueError:
            self.feedback.emit(f"泵{addr}: 速度值无效")
            return
        try:
            pos_val = float(self.abs_input.text())
        except ValueError:
            self.feedback.emit(f"泵{addr}: 位置值无效")
            return

        if not self.pump.set_speed(speed_val):
            self.feedback.emit(f"泵{addr}: 设置速度失败")
            return
        if not self.pump.set_absolute_position(pos_val):
            self.feedback.emit(f"泵{addr}: 设置绝对位置失败")
            return
        ok = self.pump.start()
        self.feedback.emit(f"泵{addr}: 速度{speed_val}um/s 移动到{pos_val}um 开始{'成功' if ok else '失败'}")

    def _start(self):
        addr = self.pump.addr
        try:
            speed_val = float(self.speed_input.text())
        except ValueError:
            self.feedback.emit(f"泵{addr}: 速度值无效")
            return
        try:
            accel_val = int(self.accel_input.text())
        except ValueError:
            self.feedback.emit(f"泵{addr}: 加速度值无效")
            return
        try:
            incr_val = float(self.incr_input.text())
        except ValueError:
            self.feedback.emit(f"泵{addr}: 位移值无效")
            return

        if not self.pump.set_speed(speed_val):
            self.feedback.emit(f"泵{addr}: 设置速度失败")
            return
        if not self.pump.set_accel(accel_val):
            self.feedback.emit(f"泵{addr}: 设置加速度失败")
            return
        if not self.pump.set_increment(incr_val):
            self.feedback.emit(f"泵{addr}: 设置位移失败")
            return

        ok = self.pump.start()
        self.feedback.emit(
            f"泵{addr}: 速度{speed_val}um/s 加速度{accel_val} "
            f"增量{incr_val}um 开始{'成功' if ok else '失败'}"
        )

    def _pause(self):
        ok = self.pump.pause()
        self.feedback.emit(f"泵{self.pump.addr}: 暂停 {'成功' if ok else '失败'}")

    def _resume(self):
        ok = self.pump.resume()
        self.feedback.emit(f"泵{self.pump.addr}: 继续 {'成功' if ok else '失败'}")

    def _stop(self):
        ok = self.pump.stop()
        self.feedback.emit(f"泵{self.pump.addr}: 停止 {'成功' if ok else '失败'}")

    def _toggle_pause(self):
        if self._current_status == 1:
            self._pause()
        elif self._current_status == 2:
            self._resume()

    # ── Mode switch ──

    def _switch_mode(self, run: bool):
        self._run_mode = run
        self.btn_debug.setChecked(not run)
        self.btn_run.setChecked(run)
        self.stack.setCurrentIndex(1 if run else 0)
        self.run_progress_bar.setVisible(run)
        self.run_remain_label.setVisible(run)

    # ── Syringe changed ──

    def _on_syringe_changed(self, model: str):
        info = _SYRINGE_DB.get(model, {})
        if isinstance(info, dict):
            length_um = info.get("length_um", 0)
            volume_ml = info.get("volume_ml", 1)
        else:
            length_um = info
            volume_ml = 1
        if volume_ml > 0:
            self.syringe_info.setText(f"满刻度:{length_um/1000:.1f}mm")
        else:
            self.syringe_info.setText("")
        self._update_run_conversion()

    def _update_run_conversion(self):
        model = self.syringe_combo.currentText()
        info = _SYRINGE_DB.get(model, {})
        if isinstance(info, dict):
            length_um = info.get("length_um", 0)
            volume_ml = info.get("volume_ml", 1)
        else:
            length_um = 0
            volume_ml = 1

        um_per_mL = length_um / volume_ml if volume_ml > 0 else 0

        try:
            vol_ml = float(self.run_volume_input.text())
            self._vol_um_label.setText(f"{vol_ml * um_per_mL:.1f}um")
        except ValueError:
            self._vol_um_label.setText("")

        try:
            spd_ml_min = float(self.run_speed_input.text())
            spd_um_s = spd_ml_min * um_per_mL / 60.0
            self._spd_ums_label.setText(f"{spd_um_s:.2f}um/s")
        except ValueError:
            self._spd_ums_label.setText("")

    # ── Run mode commands ──

    def _run_start(self):
        model = self.syringe_combo.currentText()
        info = _SYRINGE_DB.get(model)
        if not info:
            self.feedback.emit(f"泵{self.pump.addr}: 无效的注射器型号")
            return

        volume_ml = info["volume_ml"]
        length_um = info["length_um"]
        um_per_mL = length_um / volume_ml  # µm per mL

        # Read injection volume (mL)
        try:
            inject_ml = float(self.run_volume_input.text())
        except ValueError:
            self.feedback.emit(f"泵{self.pump.addr}: 注射量无效")
            return
        if inject_ml <= 0:
            self.feedback.emit(f"泵{self.pump.addr}: 注射量必须大于0")
            return

        # Read speed (mL/min)
        try:
            speed_ml_min = float(self.run_speed_input.text())
        except ValueError:
            self.feedback.emit(f"泵{self.pump.addr}: 速度值无效")
            return
        if speed_ml_min <= 0:
            self.feedback.emit(f"泵{self.pump.addr}: 速度必须大于0")
            return

        # Convert to µm
        inject_um = inject_ml * um_per_mL
        speed_um_s = speed_ml_min * um_per_mL / 60.0  # mL/min → µm/s

        # Set speed → set increment → start
        ok = self.pump.set_speed(speed_um_s)
        if not ok:
            self.feedback.emit(f"泵{self.pump.addr}: 设置速度失败")
            return
        ok = self.pump.set_increment(inject_um)
        if not ok:
            self.feedback.emit(f"泵{self.pump.addr}: 设置位移失败")
            return

        self._run_total_um = inject_um
        self._run_total_ml = inject_ml
        self._run_speed_um_s = speed_um_s
        self._run_start_pos = self._current_pos
        self.run_progress_bar.setValue(0)
        self.run_progress_bar.setFormat("已注射 0 mL")
        self.run_remain_label.setText("剩余: 计算中...")

        ok = self.pump.start()
        self.feedback.emit(
            f"泵{self.pump.addr}: {model} 注射{inject_ml}mL "
            f"@{speed_ml_min}mL/min ({inject_um/1000:.1f}mm) {'成功' if ok else '失败'}"
        )

    def _run_stop(self):
        ok = self.pump.stop()
        self._run_total_um = 0
        self.run_progress_bar.setValue(0)
        self.run_progress_bar.setFormat("已注射 0 mL")
        self.run_remain_label.setText("")
        self.feedback.emit(f"泵{self.pump.addr}: 停止 {'成功' if ok else '失败'}")

    # ── Enable / disable ──

    def _toggle_enable(self, checked: bool):
        self._enabled = checked
        self._set_children_enabled(checked)
        if not checked:
            for label in (self.status_label, self.run_status_label):
                label.setText("状态: --")
                label.setStyleSheet(scale_stylesheet(_STATUS_STYLE, self._font_scale))
                for i, (w, _) in enumerate(self._style_registry):
                    if w is label:
                        self._style_registry[i] = (w, _STATUS_STYLE)
                        break
            for label in (self.position_label, self.run_position_label):
                label.setText("位置: -- um")
            self.run_progress_bar.setValue(0)
            self.run_progress_bar.setFormat("已注射 0 mL")
            self.run_remain_label.setText("")
            for btn in (self.btn_pause_resume, self.btn_run_pause_resume):
                btn.setText("暂停")
                btn.setStyleSheet(scale_stylesheet(_BTN_CTRL_STYLE.format(
                    bg="#f57900", border="#ce5c00", pressed="#a84b00"), self._font_scale))

    def _set_children_enabled(self, enabled: bool):
        for btn in self.findChildren(QPushButton):
            if btn is self.enable_cb:
                continue
            btn.setEnabled(enabled)
        for inp in self.findChildren(TouchLineEdit):
            inp.setEnabled(enabled)
        self.syringe_combo.setEnabled(enabled)

    # ── Font scaling ──

    def _record_base_styles(self):
        """Walk the widget tree and record all set stylesheets as base (scale=1.0)."""
        self._style_registry.clear()
        for child in self.findChildren(QWidget):
            ss = child.styleSheet()
            if ss:
                self._style_registry.append((child, ss))
        ss = self.styleSheet()
        if ss:
            self._style_registry.append((self, ss))

    def set_font_scale(self, scale: float):
        if abs(scale - self._font_scale) < 0.01:
            return
        self._font_scale = scale
        global _global_scale
        _global_scale = scale
        self._apply_scale()

    def _apply_scale(self):
        for widget, base in self._style_registry:
            widget.setStyleSheet(scale_stylesheet(base, self._font_scale))

    # ── Polling callbacks ──

    def update_status(self, addr: int, status: int):
        if addr != self.pump.addr:
            return
        self._current_status = status
        status_map = {0: "空闲", 1: "运行中", 2: "暂停中"}
        text = status_map.get(status, f"未知({status})")
        color = {0: "#4e9a06", 1: "#3465a4", 2: "#f57900"}.get(status, "#888")
        base = f"{_STATUS_STYLE} color: {color};"
        for label in (self.status_label, self.run_status_label):
            label.setText(f"状态: {text}")
            label.setStyleSheet(scale_stylesheet(base, self._font_scale))
            for i, (w, _) in enumerate(self._style_registry):
                if w is label:
                    self._style_registry[i] = (w, base)
                    break

        # Toggle pause/resume button
        for btn in (self.btn_pause_resume, self.btn_run_pause_resume):
            if status == 0:
                btn.setText("暂停")
                btn.setEnabled(False)
                btn.setStyleSheet(scale_stylesheet(_BTN_CTRL_STYLE.format(
                    bg="#f57900", border="#ce5c00", pressed="#a84b00"), self._font_scale))
            elif status == 1:
                btn.setText("暂停")
                btn.setEnabled(True)
                btn.setStyleSheet(scale_stylesheet(_BTN_CTRL_STYLE.format(
                    bg="#f57900", border="#ce5c00", pressed="#a84b00"), self._font_scale))
            elif status == 2:
                btn.setText("继续")
                btn.setEnabled(True)
                btn.setStyleSheet(scale_stylesheet(_BTN_CTRL_STYLE.format(
                    bg="#3465a4", border="#204a87", pressed="#153566"), self._font_scale))

    def update_position(self, addr: int, position: float):
        if addr != self.pump.addr:
            return
        self._current_pos = position
        for label in (self.position_label, self.run_position_label):
            label.setText(f"位置: {position:.1f} um")

        # Update run-mode progress
        if self._run_mode and self._run_total_um > 0:
            traveled = abs(position - self._run_start_pos)
            pct = min(100, int(traveled / self._run_total_um * 100))
            injected_ml = pct / 100 * self._run_total_ml
            self.run_progress_bar.setValue(pct)
            self.run_progress_bar.setFormat(f"已注射 {injected_ml:.2f} mL")

            # Remaining time estimate
            if self._current_status == 1:  # running
                speed = getattr(self, '_run_speed_um_s', 0)
                remaining_um = self._run_total_um - traveled
                if speed > 0 and remaining_um > 0:
                    secs = int(remaining_um / speed)
                    m, s = divmod(secs, 60)
                    self.run_remain_label.setText(f"剩余: {m:02d}:{s:02d}")
                else:
                    self.run_remain_label.setText("剩余: 00:00")
            elif self._current_status == 0:  # idle → finished
                self.run_remain_label.setText("已完成")
