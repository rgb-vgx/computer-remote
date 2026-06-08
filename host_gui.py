#!/usr/bin/env python3
"""host_gui.py — Remote desktop HOST with GUI (PySide6 tray app).

Chạy từ desktop: click icon hoặc ``python host_gui.py``.
Tự động dò DISPLAY nếu chạy từ SSH.

Chức năng:
  * Cửa sổ nhỏ + tray icon: xem log, token, trạng thái.
  * Bật/tắt server.
  * Thu nhỏ xuống tray khi đóng cửa sổ.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import string
import subprocess
import sys

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap, QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout,
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
            f"/run/sddm/xauth_nfMZXD",
            f"/var/run/sddm/xauth_nfMZXD",
            os.path.expanduser("~/.Xauthority"),
            f"/run/user/{os.getuid()}/xauth",
        ]

        for auth in xauth_paths:
            if os.path.isfile(auth):
                try:
                    os.environ["XAUTHORITY"] = auth
                    os.environ["DISPLAY"] = display_str
                    log.info("Tự động dò DISPLAY=%s (XAUTHORITY=%s)", display_str, auth)
                    return True
                except Exception:
                    continue

        os.environ["DISPLAY"] = display_str
        try:
            result = subprocess.run(
                ["xdpyinfo"], capture_output=True, timeout=2,
                env={**os.environ, "DISPLAY": display_str},
            )
            if result.returncode == 0:
                log.info("Tự động dò DISPLAY=%s", display_str)
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
        msg = self.format(record)
        _log_signal.emit_log.emit(msg)


def _setup_logging() -> None:
    handler = QtLogHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Server thread
# ---------------------------------------------------------------------------

class ServerThread(QThread):
    status_changed = Signal(str)

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.server: HostServer | None = None

    def run(self) -> None:
        self.server = HostServer(self.args)
        self.status_changed.emit("Đang lắng nghe...")
        try:
            self.server.serve_forever()
        except Exception as exc:
            log.error("Server dừng: %s", exc)
        finally:
            self.status_changed.emit("Đã dừng")

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
        self.resize(600, 400)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Connection info
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

        layout.addWidget(info_group)

        # Status
        status_group = QGroupBox("Trạng thái")
        status_layout = QVBoxLayout(status_group)

        self.status_label = QLabel("Chưa chạy")
        self.status_label.setStyleSheet("font-weight: bold; padding: 4px;")
        status_layout.addWidget(self.status_label)

        self.start_stop_btn = QPushButton("Bắt đầu")
        self.start_stop_btn.clicked.connect(self._toggle_server)
        status_layout.addWidget(self.start_stop_btn)

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

        self.server_thread = ServerThread(self.args)
        self.server_thread.status_changed.connect(self._on_status_changed)
        self.server_thread.finished.connect(self._on_server_finished)
        self.server_thread.start()

        self.start_stop_btn.setText("Dừng")
        self.token_edit.setEnabled(False)
        self.status_label.setText("Đang khởi động...")

    def _stop_server(self) -> None:
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
        self.server_thread = None
        self.start_stop_btn.setText("Bắt đầu")
        self.token_edit.setEnabled(True)
        self.status_label.setText("Đã dừng")

    def closeEvent(self, event) -> None:
        if self.server_thread is not None:
            event.ignore()
            self.hide()
            if self.tray_manager and self.tray_manager.icon:
                self.tray_manager.icon.showMessage(
                    "Remote Desktop Host",
                    "App đang chạy ẩn dưới tray. Click chuột phải để thoát.",
                    QSystemTrayIcon.MessageIcon.Information, 3000)
        else:
            if self.tray_manager:
                self.tray_manager._quit()
            else:
                event.accept()


# ---------------------------------------------------------------------------
# System tray
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

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle_window()

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
    p.add_argument("--token", default="",
                   help="Token (mặc định: tự sinh).")
    p.add_argument("--fps", type=int, default=8, help="Số frame/giây mục tiêu.")
    p.add_argument("--quality", type=int, default=60, help="Chất lượng JPEG (1-100).")
    p.add_argument("--max-width", type=int, default=1280,
                   help="Resize xuống nếu rộng hơn (giữ aspect ratio).")
    p.add_argument("--view-only", action="store_true",
                   help="Chỉ stream, không nhận control.")
    return p.parse_args(argv)


def main() -> int:
    _setup_logging()
    args = parse_gui_args()

    if not args.token:
        args.token = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        log.info("Token tự sinh: %s", args.token)

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

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
