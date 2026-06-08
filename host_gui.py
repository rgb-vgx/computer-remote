#!/usr/bin/env python3
"""host_gui.py — Remote desktop HOST with GUI (PySide6 tray app).

Chạy từ desktop: click icon hoặc ``python host_gui.py``.
Tự động dò DISPLAY nếu chạy từ SSH.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCursor, QIcon, QPainter, QPixmap, QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from host import HostServer

log = logging.getLogger("host_gui")


# ---------------------------------------------------------------------------
# Auto-detect DISPLAY
# ---------------------------------------------------------------------------

def detect_display() -> bool:
    if os.environ.get("DISPLAY"):
        return True
    x11_dir = "/tmp/.X11-unix"
    if not os.path.isdir(x11_dir):
        return False
    displays = sorted(
        (s for s in os.listdir(x11_dir) if s.startswith("X") and s[1:].isdigit()),
        key=lambda s: int(s[1:]),
    )
    if not displays:
        return False
    for disp in displays:
        dnum = disp[1:]
        display_str = f":{dnum}"
        xauth_paths = [
            f"/run/sddm/xauth_nfMZXD", f"/var/run/sddm/xauth_nfMZXD",
            os.path.expanduser("~/.Xauthority"),
            f"/run/user/{os.getuid()}/xauth",
        ]
        for auth in xauth_paths:
            if os.path.isfile(auth):
                try:
                    os.environ["XAUTHORITY"] = auth
                    os.environ["DISPLAY"] = display_str
                    log.info("Auto DISPLAY=%s (XAUTHORITY=%s)", display_str, auth)
                    return True
                except Exception:
                    continue
        os.environ["DISPLAY"] = display_str
        try:
            result = subprocess.run(
                ["xdpyinfo"], capture_output=True, timeout=2,
                env={**os.environ, "DISPLAY": display_str})
            if result.returncode == 0:
                log.info("Auto DISPLAY=%s", display_str)
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Log handler -> Qt signal
# ---------------------------------------------------------------------------

class _LogSignal(QObject):
    emit_log = Signal(str)

_log_signal = _LogSignal()


class QtLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_signal.emit_log.emit(self.format(record))


def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.getLogger().setLevel(level)

    logs_dir = Path(__file__).resolve().parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    fh = logging.FileHandler(logs_dir / "host.log", encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(fh)

    handler = QtLogHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(handler)

    log.info("Host log file: %s", logs_dir / "host.log")
    if debug:
        log.info("DEBUG mode ON")


# ---------------------------------------------------------------------------
# Server thread
# ---------------------------------------------------------------------------

class ServerThread(QThread):
    status_changed = Signal(str)
    clipboard_from_client = Signal(str)

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.server: HostServer | None = None

    def run(self) -> None:
        self.server = HostServer(self.args)
        # Override clipboard backend: GUI handles it via signals + QTimer.
        self.server.clipboard_get = lambda: ""
        self.server.clipboard_set = lambda text: None
        self.server.on_clipboard_received = self._on_clipboard

        self.status_changed.emit("Đang lắng nghe...")
        try:
            self.server.serve_forever()
        except Exception as exc:
            log.error("Server dừng: %s", exc)
        finally:
            self.status_changed.emit("Đã dừng")

    def _on_clipboard(self, text: str) -> None:
        self.clipboard_from_client.emit(text)

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.server_thread: ServerThread | None = None
        self.tray_manager: TrayManager | None = None
        self.setWindowTitle("Remote Desktop Host")
        self.resize(600, 450)

        # Clipboard state
        self._last_clipboard = ""
        self._clipboard_skip = 0

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Info
        info_group = QGroupBox("Thông tin kết nối")
        info_layout = QFormLayout(info_group)
        self.token_edit = QLineEdit(args.token)
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setMinimumWidth(200)
        self.show_token_cb = QCheckBox("Hiện token")
        self.show_token_cb.toggled.connect(
            lambda checked: self.token_edit.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password))
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit)
        token_row.addWidget(self.show_token_cb)
        info_layout.addRow("Token:", token_row)
        self.ip_label = QLabel(self._get_ip())
        info_layout.addRow("IP:", self.ip_label)
        self.port_label = QLabel(str(args.port))
        info_layout.addRow("Port:", self.port_label)

        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["H.264 (cần ffmpeg)", "JPEG"])
        self.codec_combo.setCurrentIndex(0 if args.codec == "h264" else 1)
        info_layout.addRow("Codec:", self.codec_combo)

        layout.addWidget(info_group)

        # Status
        status_group = QGroupBox("Trạng thái")
        status_layout = QVBoxLayout(status_group)
        self.status_label = QLabel("Chưa chạy")
        self.status_label.setStyleSheet("font-weight: bold; padding: 4px;")
        status_layout.addWidget(self.status_label)
        btn_row = QHBoxLayout()
        self.start_stop_btn = QPushButton("Bắt đầu")
        self.start_stop_btn.clicked.connect(self._toggle_server)
        btn_row.addWidget(self.start_stop_btn)
        self.quit_btn = QPushButton("Thoát")
        self.quit_btn.setStyleSheet("color: red;")
        self.quit_btn.clicked.connect(self._quit_app)
        btn_row.addWidget(self.quit_btn)
        status_layout.addLayout(btn_row)
        layout.addWidget(status_group)

        # Log
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, stretch=1)

        _log_signal.emit_log.connect(self._append_log)

        # Clipboard timer
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.timeout.connect(self._check_clipboard)

    def _get_ip(self) -> str:
        try:
            result = subprocess.run(
                ["tailscale", "ip", "-4"],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "Không xác định"

    def _append_log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)

    def _toggle_server(self) -> None:
        if self.server_thread is None:
            self._start_server()
        else:
            self._stop_server()

    def _start_server(self) -> None:
        self.args.token = self.token_edit.text()
        if not self.args.token:
            QMessageBox.warning(self, "Lỗi", "Token không được để trống.")
            return
        self.args.codec = "h264" if self.codec_combo.currentIndex() == 0 else "jpeg"

        self.server_thread = ServerThread(self.args)
        self.server_thread.status_changed.connect(self._on_status_changed)
        self.server_thread.finished.connect(self._on_server_finished)
        self.server_thread.clipboard_from_client.connect(self._on_clipboard_from_client)
        self.server_thread.start()

        self.start_stop_btn.setText("Dừng")
        self.token_edit.setEnabled(False)
        self.status_label.setText("Đang khởi động...")

        # Start clipboard polling
        self._last_clipboard = ""
        self._clipboard_skip = 0
        self._clipboard_timer.start(500)

    def _stop_server(self) -> None:
        self._clipboard_timer.stop()
        if self.server_thread is not None:
            self.server_thread.stop()
            self.server_thread.wait(3000)
            self.server_thread = None
        self.start_stop_btn.setText("Bắt đầu")
        self.token_edit.setEnabled(True)
        self.status_label.setText("Đã dừng")

    def _on_status_changed(self, status: str) -> None:
        self.status_label.setText(status)

    def _on_server_finished(self) -> None:
        self._clipboard_timer.stop()
        self.server_thread = None
        self.start_stop_btn.setText("Bắt đầu")
        self.token_edit.setEnabled(True)
        self.status_label.setText("Đã dừng")

    def _quit_app(self) -> None:
        if self.tray_manager:
            self.tray_manager._quit()
        else:
            if self.server_thread is not None:
                self.server_thread.stop()
                self.server_thread.wait(3000)
            QApplication.quit()

    def closeEvent(self, event) -> None:
        if self.server_thread is not None:
            event.ignore()
            self.hide()
            if self.tray_manager and self.tray_manager.icon:
                self.tray_manager.icon.showMessage(
                    "Remote Desktop Host",
                    "App đang chạy ẩn dưới tray. Chuột phải để thoát.",
                    QSystemTrayIcon.MessageIcon.Information, 3000)
        else:
            if self.tray_manager:
                self.tray_manager._quit()
            else:
                event.accept()

    # ---- Clipboard -------------------------------------------------------

    def _check_clipboard(self) -> None:
        if self._clipboard_skip > 0:
            self._clipboard_skip -= 1
            return
        if self.server_thread is None or self.server_thread.server is None:
            return
        text = QApplication.clipboard().text()
        if text and text != self._last_clipboard:
            self._last_clipboard = text
            log.debug("Host clipboard changed → gửi client (%d bytes)", len(text))
            self.server_thread.server.send_clipboard(text)

    def _on_clipboard_from_client(self, text: str) -> None:
        self._last_clipboard = text
        self._clipboard_skip = 2
        QApplication.clipboard().setText(text)
        log.debug("Clipboard từ client → set host (%d bytes)", len(text))


# ---------------------------------------------------------------------------
# Tray
# ---------------------------------------------------------------------------

class TrayManager:
    def __init__(self, app: QApplication, window: MainWindow) -> None:
        self.app = app
        self.window = window
        self.icon: QSystemTrayIcon | None = None
        window.tray_manager = self

    def setup(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("Hệ thống không hỗ trợ tray icon.")
            return
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setBrush(QColor(0, 180, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 28, 28)
        painter.end()
        icon = QIcon(pixmap)

        self.icon = QSystemTrayIcon(icon, self.app)
        self.icon.setToolTip("Remote Desktop Host")
        menu = QMenu()
        show_action = QAction("Hiện / Ẩn")
        show_action.triggered.connect(self._toggle_window)
        menu.addAction(show_action)
        menu.addSeparator()
        quit_action = QAction("Thoát")
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self.icon.show()

    def _toggle_window(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()

    def _show_menu(self) -> None:
        if self.icon and self.icon.contextMenu():
            self.icon.contextMenu().popup(QCursor.pos())

    def _on_activated(self, reason) -> None:
        # KDE Plasma often doesn't show setContextMenu on right-click,
        # so we show it manually on every activation except double-click.
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle_window()
        else:
            self._show_menu()

    def _quit(self) -> None:
        if self.window.server_thread is not None:
            self.window.server_thread.stop()
            self.window.server_thread.wait(3000)
        QApplication.quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_gui_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Remote desktop HOST (GUI mode).")
    p.add_argument("--bind", default="0.0.0.0",
                   help="Địa chỉ bind (mặc định 0.0.0.0).")
    p.add_argument("--port", type=int, default=7777, help="Cổng TCP (mặc định 7777).")
    p.add_argument("--token", default="1",
                   help="Token để client xác thực.")
    p.add_argument("--fps", type=int, default=8, help="Số frame/giây mục tiêu.")
    p.add_argument("--quality", type=int, default=75, help="Chất lượng nén (1-100).")
    p.add_argument("--max-width", type=int, default=1920,
                   help="Resize xuống nếu rộng hơn (giữ aspect ratio).")
    p.add_argument("--codec", choices=["jpeg", "h264"], default="jpeg",
                   help="Codec: jpeg hoặc h264 (mặc định jpeg)")
    p.add_argument("--view-only", action="store_true",
                   help="Chỉ stream, không nhận control.")
    p.add_argument("--auto-start", action="store_true",
                   help="Tự động bắt đầu stream ngay khi khởi động.")
    p.add_argument("--tray", action="store_true",
                   help="Chạy ẩn dưới tray (không hiện cửa sổ).")
    p.add_argument("--debug", action="store_true",
                   help="Bật log debug chi tiết.")
    return p.parse_args(argv)


def main() -> int:
    args = parse_gui_args()
    _setup_logging(args.debug)
    log.info("Token: %s", args.token)

    if not detect_display():
        log.warning("KHÔNG tìm thấy display X11 nào!")
        log.warning("Hãy chạy app này từ desktop (click icon)")

    log.info("=== Remote Desktop Host (GUI mode) ===")
    log.info("Bind: %s:%d | FPS: %d | Quality: %d | Max-width: %d",
             args.bind, args.port, args.fps, args.quality, args.max_width)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow(args)
    TrayManager(app, window).setup()

    if args.tray:
        window.hide()
    else:
        window.show()
    if args.auto_start:
        window._start_server()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
