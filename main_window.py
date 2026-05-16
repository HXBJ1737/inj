"""Main application window — 3 tabs x 5 pump columns with serial port controls."""

import serial.tools.list_ports
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QComboBox, QPushButton, QCheckBox, QStatusBar, QGroupBox,
    QMessageBox, QScrollArea, QTextEdit, QSplitter, QApplication,
)
from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtGui import QFont

from modbus_client import ModbusClient
from pump_controller import PumpController
from pump_widget import PumpWidget, PollWorker, scale_stylesheet


NUM_PUMPS = 15
PUMPS_PER_TAB = 4
NUM_TABS = 4

_TOOLBAR_STYLE = """
    QGroupBox { font-size: 14px; font-weight: bold;
        border: 1px solid #555; border-radius: 6px;
        margin-top: 8px; padding: 8px; }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
    QLabel { font-size: 13px; }
    QComboBox { font-size: 14px; min-height: 34px; min-width: 100px; }
    QPushButton { font-size: 14px; min-height: 34px; min-width: 70px;
        border-radius: 5px; border: 1px solid #555;
        background: #3c3f41; color: white; }
    QPushButton:pressed { background: #5294e2; }
"""

_TAB_STYLE = """
    QTabBar::tab { font-size: 14px; min-width: 160px;
        min-height: 32px; padding: 6px 16px; }
"""


# ── Main window ────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("注射泵 RS485 控制系统")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setMinimumSize(1280, 720)
        self.resize(1280, 720)
        self._drag_pos = None

        self._client = ModbusClient()
        self._poll_worker = None
        self._pump_widgets: dict[int, PumpWidget] = {}
        self._pumps: dict[int, PumpController] = {}
        self._log_buffer: list[tuple[str, str]] = []  # (direction, msg)
        self._log_buffer_max = 5000
        self._font_scale = 1.0
        self._style_registry: list[tuple[object, str]] = []  # (widget, base_stylesheet)

        self._init_ui()
        self._record_base_styles()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # --- Serial port controls ---
        self._port_group = QGroupBox("串口设置")
        self._port_group.setStyleSheet(_TOOLBAR_STYLE)
        self._port_group.installEventFilter(self)
        port_layout = QHBoxLayout()
        port_layout.setSpacing(10)

        port_layout.addWidget(QLabel("串口:"))
        self.port_combo = QComboBox()
        self._refresh_ports()
        port_layout.addWidget(self.port_combo)

        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self._refresh_ports)
        port_layout.addWidget(btn_refresh)

        port_layout.addWidget(QLabel("波特率:"))
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"])
        self.baud_combo.setCurrentText("9600")
        port_layout.addWidget(self.baud_combo)

        self.btn_connect = QPushButton("连接")
        self.btn_connect.setStyleSheet(
            "QPushButton { background: #4e9a06; font-weight: bold; }"
            "QPushButton:pressed { background: #3d7d05; }"
        )
        self.btn_connect.clicked.connect(self._toggle_connection)
        port_layout.addWidget(self.btn_connect)

        self.conn_status_label = QLabel("未连接")
        self.conn_status_label.setStyleSheet("font-size: 14px; color: red; font-weight: bold;")
        port_layout.addWidget(self.conn_status_label)

        port_layout.addStretch()

        # Log toggle button
        self.btn_log = QPushButton("串口日志")
        self.btn_log.setCheckable(True)
        self.btn_log.setChecked(False)
        self.btn_log.setStyleSheet(
            "QPushButton { background: #555; }"
            "QPushButton:checked { background: #5294e2; }"
        )
        self.btn_log.clicked.connect(self._toggle_log_panel)
        port_layout.addWidget(self.btn_log)

        # Batch start / stop
        self.btn_start_all = QPushButton("一键开始")
        self.btn_start_all.setStyleSheet(
            "QPushButton { background: #4e9a06; font-weight: bold; color: white;"
            "font-size: 14px; min-height: 34px; min-width: 80px;"
            "border-radius: 5px; border: 1px solid #3d7d05; }"
            "QPushButton:pressed { background: #3d7d05; }"
        )
        self.btn_start_all.clicked.connect(self._start_all)
        port_layout.addWidget(self.btn_start_all)

        self.btn_stop_all = QPushButton("一键停止")
        self.btn_stop_all.setStyleSheet(
            "QPushButton { background: #cc0000; font-weight: bold; color: white;"
            "font-size: 14px; min-height: 34px; min-width: 80px;"
            "border-radius: 5px; border: 1px solid #a40000; }"
            "QPushButton:pressed { background: #a40000; }"
        )
        self.btn_stop_all.clicked.connect(self._stop_all)
        port_layout.addWidget(self.btn_stop_all)

        self.btn_pause_all = QPushButton("一键暂停")
        self.btn_pause_all.setStyleSheet(
            "QPushButton { background: #f57900; font-weight: bold; color: white;"
            "font-size: 14px; min-height: 34px; min-width: 80px;"
            "border-radius: 5px; border: 1px solid #ce5c00; }"
            "QPushButton:pressed { background: #ce5c00; }"
        )
        self.btn_pause_all.clicked.connect(self._pause_all)
        port_layout.addWidget(self.btn_pause_all)

        self.btn_resume_all = QPushButton("一键继续")
        self.btn_resume_all.setStyleSheet(
            "QPushButton { background: #3465a4; font-weight: bold; color: white;"
            "font-size: 14px; min-height: 34px; min-width: 80px;"
            "border-radius: 5px; border: 1px solid #204a87; }"
            "QPushButton:pressed { background: #204a87; }"
        )
        self.btn_resume_all.clicked.connect(self._resume_all)
        port_layout.addWidget(self.btn_resume_all)

        port_layout.addSpacing(20)

        self.btn_maximize = QPushButton("最大化")
        self.btn_maximize.setStyleSheet(
            "QPushButton { background: #555; font-weight: bold; color: white;"
            "font-size: 14px; min-height: 34px; min-width: 56px;"
            "border-radius: 5px; border: 1px solid #555; }"
            "QPushButton:pressed { background: #5294e2; }"
        )
        self.btn_maximize.clicked.connect(self._toggle_maximize)
        port_layout.addWidget(self.btn_maximize)

        btn_close = QPushButton("关闭")
        btn_close.setStyleSheet(
            "QPushButton { background: #555; font-weight: bold; color: white;"
            "font-size: 14px; min-height: 34px; min-width: 44px;"
            "border-radius: 5px; border: 1px solid #555; }"
            "QPushButton:pressed { background: #cc0000; }"
        )
        btn_close.clicked.connect(self._quit_app)
        port_layout.addWidget(btn_close)

        self._port_group.setLayout(port_layout)
        main_layout.addWidget(self._port_group)

        # --- Splitter: tabs (top) + log panel (bottom) ---
        self._splitter = QSplitter(Qt.Vertical)
        self._splitter.setHandleWidth(4)

        # Tab widget with pump columns
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(_TAB_STYLE)

        for tab_idx in range(NUM_TABS):
            tab = QWidget()
            tab_layout = QHBoxLayout(tab)
            tab_layout.setSpacing(4)
            tab_layout.setContentsMargins(2, 2, 2, 2)

            start_addr = tab_idx * PUMPS_PER_TAB + 1
            end_addr = start_addr
            for i in range(PUMPS_PER_TAB):
                addr = start_addr + i
                if addr > NUM_PUMPS:
                    break
                end_addr = addr
                pump = PumpController(self._client, addr)
                widget = PumpWidget(pump)
                widget.feedback.connect(self._show_feedback)
                self._pumps[addr] = pump
                self._pump_widgets[addr] = widget
                tab_layout.addWidget(widget, 1)

            scroll = QScrollArea()
            scroll.setWidget(tab)
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea { border: none; }")
            self.tabs.addTab(scroll, f"泵 {start_addr:02d}-{end_addr:02d}")

        self._splitter.addWidget(self.tabs)

        # Log panel (hidden by default)
        self._log_panel = self._build_log_panel()
        self._splitter.addWidget(self._log_panel)
        self._log_panel.hide()

        # Default splitter sizes: tabs gets all space
        self._splitter.setSizes([1, 0])
        main_layout.addWidget(self._splitter)

        # Connect data logger
        self._client.set_data_logger(self._on_serial_data)

        # --- Status bar ---
        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet("font-size: 13px;")
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

    def _build_log_panel(self) -> QWidget:
        """Build the embedded serial log panel."""
        panel = QWidget()
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(4)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        lbl = QLabel("串口日志")
        lbl.setStyleSheet("font-size: 13px; font-weight: bold;")
        toolbar.addWidget(lbl)

        btn_clear = QPushButton("清空")
        btn_clear.setStyleSheet(
            "QPushButton { font-size: 12px; min-height: 28px; min-width: 50px; }"
        )
        btn_clear.clicked.connect(lambda: self._data_log.clear())
        toolbar.addWidget(btn_clear)

        self._btn_auto_scroll = QPushButton("自动滚动: 开")
        self._btn_auto_scroll.setCheckable(True)
        self._btn_auto_scroll.setChecked(True)
        self._btn_auto_scroll.setStyleSheet(
            "QPushButton { font-size: 12px; min-height: 28px; min-width: 80px; }"
            "QPushButton:checked { background: #5294e2; }"
        )
        self._btn_auto_scroll.clicked.connect(self._toggle_auto_scroll)
        toolbar.addWidget(self._btn_auto_scroll)

        toolbar.addStretch()
        vbox.addLayout(toolbar)

        # Log text area
        self._data_log = QTextEdit()
        self._data_log.setReadOnly(True)
        self._data_log.setFont(QFont("Consolas", 6))
        self._data_log.setStyleSheet(
            "QTextEdit { background: #1e1e1e; color: #d4d4d4; border: 1px solid #555; }"
        )
        vbox.addWidget(self._data_log)

        self._auto_scroll = True
        return panel

    def _toggle_auto_scroll(self):
        self._auto_scroll = self._btn_auto_scroll.isChecked()
        self._btn_auto_scroll.setText(f"自动滚动: {'开' if self._auto_scroll else '关'}")

    def _toggle_log_panel(self, checked):
        if checked:
            self._log_panel.show()
            # Replay buffered entries
            self._data_log.clear()
            for d, m in self._log_buffer:
                self._append_log_line(d, m)
            total = self._splitter.height()
            log_h =int(total // 2.6)
            # print(f"Splitter total={total}, log={log_h}")
            self._splitter.setSizes([total - log_h, log_h])
        else:
            self._log_panel.hide()
            self._splitter.setSizes([1, 0])

    def _append_log_line(self, direction: str, msg: str):
        """Append a colored log line to the embedded log."""
        try:
            color_map = {
                "TX": "#5294e2",
                "RX": "#4e9a06",
                "ERR": "#cc0000",
                "SYS": "#f57900",
            }
            color = color_map.get(direction, "#d4d4d4")
            tag = f'<span style="color:{color};font-weight:bold;">[{direction}]</span>'
            self._data_log.append(f'{tag} {msg}')
            if self._auto_scroll:
                sb = self._data_log.verticalScrollBar()
                sb.setValue(sb.maximum())
        except Exception:
            pass

    # ── Serial port ──

    def _refresh_ports(self):
        self.port_combo.clear()
        ports = serial.tools.list_ports.comports()
        for p in ports:
            self.port_combo.addItem(p.device)

    def _toggle_connection(self):
        if self._client.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_combo.currentText()
        if not port:
            QMessageBox.warning(self, "警告", "请选择串口")
            return

        baud = int(self.baud_combo.currentText())
        self._client.port = port
        self._client.baudrate = baud

        if self._client.connect():
            self.btn_connect.setText("断开")
            conn_btn_style = (
                "QPushButton { background: #cc0000; font-weight: bold; }"
                "QPushButton:pressed { background: #a40000; }"
            )
            self.btn_connect.setStyleSheet(scale_stylesheet(conn_btn_style, self._font_scale))
            self._update_style_registry(self.btn_connect, conn_btn_style)
            self.conn_status_label.setText(f"已连接 ({port})")
            conn_lbl_style = "font-size: 14px; color: #4e9a06; font-weight: bold;"
            self.conn_status_label.setStyleSheet(scale_stylesheet(conn_lbl_style, self._font_scale))
            self._update_style_registry(self.conn_status_label, conn_lbl_style)
            self.status_bar.showMessage(f"已连接到 {port}，波特率 {baud}")
            self._start_polling()
        else:
            QMessageBox.critical(self, "错误", f"无法连接到 {port}")
            self.status_bar.showMessage("连接失败")

    def _disconnect(self):
        self._stop_polling()
        self._client.disconnect()
        self.btn_connect.setText("连接")
        conn_btn_style = (
            "QPushButton { background: #4e9a06; font-weight: bold; }"
            "QPushButton:pressed { background: #3d7d05; }"
        )
        self.btn_connect.setStyleSheet(scale_stylesheet(conn_btn_style, self._font_scale))
        self._update_style_registry(self.btn_connect, conn_btn_style)
        self.conn_status_label.setText("未连接")
        conn_lbl_style = "font-size: 14px; color: red; font-weight: bold;"
        self.conn_status_label.setStyleSheet(scale_stylesheet(conn_lbl_style, self._font_scale))
        self._update_style_registry(self.conn_status_label, conn_lbl_style)
        self.status_bar.showMessage("已断开连接")

    # ── Polling ──

    def _start_polling(self):
        self._stop_polling()
        widgets = list(self._pump_widgets.values())
        self._poll_worker = PollWorker(widgets, interval_ms=500)
        for widget in widgets:
            self._poll_worker.status_updated.connect(widget.update_status)
            self._poll_worker.position_updated.connect(widget.update_position)
        self._poll_worker.start()

    def _stop_polling(self):
        if self._poll_worker:
            self._poll_worker.stop()
            self._poll_worker = None

    def _show_feedback(self, msg: str):
        self.status_bar.showMessage(msg)

    # ── Batch operations ──

    def _start_all(self):
        for w in self._pump_widgets.values():
            if w._enabled:
                if w._run_mode:
                    w._run_start()
                else:
                    w._start()
        self.status_bar.showMessage("一键开始：已发送所有启用泵的启动指令")

    def _stop_all(self):
        for w in self._pump_widgets.values():
            if w._enabled:
                if w._run_mode:
                    w._run_stop()
                else:
                    w._stop()
        self.status_bar.showMessage("一键停止：已发送所有启用泵的停止指令")

    def _pause_all(self):
        for w in self._pump_widgets.values():
            if w._enabled:
                w._pause()
        self.status_bar.showMessage("一键暂停：已发送所有启用泵的暂停指令")

    def _resume_all(self):
        for w in self._pump_widgets.values():
            if w._enabled:
                w._resume()
        self.status_bar.showMessage("一键继续：已发送所有启用泵的继续指令")

    # ── Font scaling ──

    def _record_base_styles(self):
        """Walk the widget tree and record all set stylesheets as base (scale=1.0)."""
        self._style_registry.clear()
        central = self.centralWidget()
        if central:
            for child in central.findChildren(QWidget):
                # Skip pump widgets — they self-manage their own scaling
                if isinstance(child, PumpWidget):
                    continue
                parent = child.parent()
                while parent:
                    if isinstance(parent, PumpWidget):
                        break
                    parent = parent.parent()
                else:
                    ss = child.styleSheet()
                    if ss:
                        self._style_registry.append((child, ss))
        ss = central.styleSheet() if central else ""
        if ss:
            self._style_registry.append((central, ss))
        self._log_font_base_size = 6

    def _update_style_registry(self, widget, base: str):
        """Update or add a widget's base stylesheet in the registry."""
        for i, (w, _) in enumerate(self._style_registry):
            if w is widget:
                self._style_registry[i] = (widget, base)
                return
        self._style_registry.append((widget, base))

    def _apply_scale(self, scale: float):
        """Reapply all registered stylesheets scaled by the given factor."""
        for widget, base in self._style_registry:
            try:
                widget.setStyleSheet(scale_stylesheet(base, scale))
            except RuntimeError:
                pass
        # Scale the log font
        try:
            self._data_log.setFont(QFont("Consolas", max(6, round(self._log_font_base_size * scale))))
        except Exception:
            pass
        # Propagate to pump widgets
        for pw in self._pump_widgets.values():
            pw.set_font_scale(scale)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        h = self.height()
        scale = min(w / 1280, h / 720)
        scale = max(0.7, min(2.5, scale))
        if abs(scale - self._font_scale) >= 0.01:
            self._font_scale = scale
            self._apply_scale(scale)

    # ── Data logger callback ──

    def _on_serial_data(self, direction: str, msg: str):
        QTimer.singleShot(0, lambda d=direction, m=msg: self._append_log(d, m))

    def _append_log(self, direction: str, msg: str):
        # Always buffer
        self._log_buffer.append((direction, msg))
        if len(self._log_buffer) > self._log_buffer_max:
            self._log_buffer = self._log_buffer[-self._log_buffer_max:]
        # Forward to log panel if visible
        if self._log_panel.isVisible():
            self._append_log_line(direction, msg)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            child = QApplication.widgetAt(event.globalPos())
            if child and isinstance(child, (QPushButton, QComboBox, QCheckBox)):
                return False
            self._drag_pos = event.globalPos()
            return True
        if event.type() == QEvent.MouseMove and self._drag_pos is not None:
            delta = event.globalPos() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPos()
            return True
        if event.type() == QEvent.MouseButtonRelease:
            self._drag_pos = None
            return True
        return super().eventFilter(obj, event)

    def _quit_app(self):
        self._stop_polling()
        self._client.disconnect()
        QApplication.instance().quit()

    def _toggle_maximize(self):
        if self.isFullScreen():
            self.showNormal()
            self.btn_maximize.setText("最大化")
        else:
            self.showFullScreen()
            self.btn_maximize.setText("还原")

    def closeEvent(self, event):
        self._stop_polling()
        self._client.disconnect()
        event.accept()
