"""Main application window — 3 tabs x 5 pump columns with serial port controls."""

import serial.tools.list_ports
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QComboBox, QPushButton, QStatusBar, QGroupBox,
    QMessageBox, QScrollArea, QTextEdit, QSplitter,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont

from modbus_client import ModbusClient
from pump_controller import PumpController
from pump_widget import PumpWidget, PollWorker


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
        self.resize(1280, 720)

        self._client = ModbusClient()
        self._poll_worker = None
        self._pump_widgets: dict[int, PumpWidget] = {}
        self._pumps: dict[int, PumpController] = {}
        self._log_buffer: list[tuple[str, str]] = []  # (direction, msg)
        self._log_buffer_max = 5000

        self._init_ui()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # --- Serial port controls ---
        port_group = QGroupBox("串口设置")
        port_group.setStyleSheet(_TOOLBAR_STYLE)
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

        port_group.setLayout(port_layout)
        main_layout.addWidget(port_group)

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
        self._data_log.setFont(QFont("Consolas", 10))
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
            self._splitter.setSizes([3, 1])
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
            self.btn_connect.setStyleSheet(
                "QPushButton { background: #cc0000; font-weight: bold; }"
                "QPushButton:pressed { background: #a40000; }"
            )
            self.conn_status_label.setText(f"已连接 ({port})")
            self.conn_status_label.setStyleSheet("font-size: 14px; color: #4e9a06; font-weight: bold;")
            self.status_bar.showMessage(f"已连接到 {port}，波特率 {baud}")
            self._start_polling()
        else:
            QMessageBox.critical(self, "错误", f"无法连接到 {port}")
            self.status_bar.showMessage("连接失败")

    def _disconnect(self):
        self._stop_polling()
        self._client.disconnect()
        self.btn_connect.setText("连接")
        self.btn_connect.setStyleSheet(
            "QPushButton { background: #4e9a06; font-weight: bold; }"
            "QPushButton:pressed { background: #3d7d05; }"
        )
        self.conn_status_label.setText("未连接")
        self.conn_status_label.setStyleSheet("font-size: 14px; color: red; font-weight: bold;")
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

    def closeEvent(self, event):
        self._stop_polling()
        self._client.disconnect()
        event.accept()
