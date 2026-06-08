#!/usr/bin/env python3
"""client.py — Remote desktop CLIENT/VIEWER (chạy trên Windows).

Chức năng:
  * GUI PySide6: nhập Host IP / Port / Token, Connect / Disconnect.
  * Nhận frame JPEG từ host, decode bằng OpenCV, hiển thị (giữ aspect ratio).
  * Bắt mouse move / press / release trên vùng hiển thị, chuyển thành toạ độ
    normalized (0..1) và gửi về host — resize cửa sổ vẫn điều khiển đúng.

Mọi việc socket nằm trong 1 worker thread (QThread); GUI thread chỉ chạm UI.
Chạy: python client.py
"""

from __future__ import annotations

import queue
import socket
import sys

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QVBoxLayout, QWidget,
)

from common import protocol


class NetworkWorker(QThread):
    """Thread giữ socket: connect → hello → loop nhận frame; flush control queue.

    Chỉ thread này chạm tới socket. GUI thread đẩy control event vào
    ``self.control_queue`` (thread-safe), worker lấy ra và gửi đi.
    """

    frame_ready = Signal(QImage)
    status = Signal(str)
    disconnected = Signal(str)

    def __init__(self, host: str, port: int, token: str) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.token = token
        self.control_queue: "queue.Queue[dict]" = queue.Queue()
        self._running = True
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        self._running = False
        # Đóng socket để recv đang block thoát ra ngay.
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass

    def send_control(self, event: dict) -> None:
        """Gọi từ GUI thread — chỉ bỏ event vào queue, không chạm socket."""
        if self._running:
            self.control_queue.put(event)

    def run(self) -> None:  # chạy trong thread riêng
        try:
            self.status.emit(f"Đang kết nối tới {self.host}:{self.port} ...")
            self._sock = socket.create_connection((self.host, self.port), timeout=10)
            # Timeout ngắn để vòng recv định kỳ flush control queue & check _running.
            self._sock.settimeout(0.05)

            protocol.send_json(self._sock, protocol.PKT_HELLO, {"token": self.token})

            self.status.emit("Đã gửi token, chờ host xác thực ...")
            self._recv_loop()
        except OSError as exc:
            self.disconnected.emit(f"Không kết nối được: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.disconnected.emit(f"Lỗi: {exc}")
        finally:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass

    def _recv_loop(self) -> None:
        assert self._sock is not None
        while self._running:
            # 1) flush control event đang chờ gửi.
            self._flush_control()

            # 2) thử nhận 1 packet (non-blocking-ish nhờ timeout ngắn).
            try:
                ptype, payload = protocol.recv_packet(self._sock)
            except socket.timeout:
                continue
            except (ConnectionError, OSError) as exc:
                if self._running:
                    self.disconnected.emit(f"Mất kết nối: {exc}")
                return

            if ptype == protocol.PKT_FRAME:
                self._handle_frame(payload)
            elif ptype == protocol.PKT_INFO:
                try:
                    info = protocol.decode_json(payload)
                except Exception:
                    continue
                msg = info.get("message", "")
                if info.get("type") == "error":
                    self.disconnected.emit(f"Host từ chối: {msg}")
                    return
                self.status.emit(f"Đã kết nối — {msg}")

    def _flush_control(self) -> None:
        assert self._sock is not None
        while True:
            try:
                event = self.control_queue.get_nowait()
            except queue.Empty:
                return
            try:
                protocol.send_json(self._sock, protocol.PKT_CONTROL, event)
            except OSError:
                return

    def _handle_frame(self, payload: bytes) -> None:
        arr = np.frombuffer(payload, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # BGR
        if img is None:
            return
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w, _ = img.shape
        # QImage không copy buffer ngay → .copy() để an toàn khi numpy array bị GC.
        qimg = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.frame_ready.emit(qimg)


class RemoteView(QLabel):
    """QLabel hiển thị frame + bắt mouse, chuyển sang toạ độ normalized (0..1).

    Ảnh được letterbox (giữ aspect ratio) trong widget. Toạ độ normalized tính
    theo vùng ảnh thực sự vẽ ra, không theo kích thước widget → click đúng chỗ.
    """

    mouse_event = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(640, 360)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #101010; color: #aaa;")
        self.setText("Chưa kết nối")
        self.setMouseTracking(True)  # nhận mouseMove cả khi không nhấn nút

        self._pixmap: QPixmap | None = None
        # Hình chữ nhật vùng ảnh đã letterbox: (x, y, w, h) trong toạ độ widget.
        self._draw_rect = (0, 0, 0, 0)

    def set_frame(self, qimg: QImage) -> None:
        self._pixmap = QPixmap.fromImage(qimg)
        self.setText("")
        self._rescale()
        self.update()

    def clear_frame(self, text: str) -> None:
        self._pixmap = None
        self.setPixmap(QPixmap())  # xoá ảnh cũ
        self.setText(text)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._rescale()
        super().resizeEvent(event)

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        area = self.size()
        scaled = self._pixmap.scaled(
            area, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # Vị trí letterbox (căn giữa).
        x = (area.width() - scaled.width()) // 2
        y = (area.height() - scaled.height()) // 2
        self._draw_rect = (x, y, scaled.width(), scaled.height())
        self.setPixmap(scaled)

    # ---- map toạ độ + emit event ----------------------------------------

    def _normalized(self, pos) -> tuple[float, float] | None:
        if self._pixmap is None:
            return None
        x0, y0, w, h = self._draw_rect
        if w <= 0 or h <= 0:
            return None
        nx = (pos.x() - x0) / w
        ny = (pos.y() - y0) / h
        if nx < 0 or nx > 1 or ny < 0 or ny > 1:
            return None  # con trỏ ngoài vùng ảnh (vùng letterbox đen)
        return nx, ny

    @staticmethod
    def _button_name(qt_button) -> str | None:
        if qt_button == Qt.LeftButton:
            return "left"
        if qt_button == Qt.RightButton:
            return "right"
        return None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        coord = self._normalized(event.position())
        if coord:
            self.mouse_event.emit(
                {"event": "mouse_move", "x": coord[0], "y": coord[1]})

    def mousePressEvent(self, event) -> None:  # noqa: N802
        coord = self._normalized(event.position())
        btn = self._button_name(event.button())
        if coord and btn:
            self.mouse_event.emit(
                {"event": "mouse_down", "button": btn, "x": coord[0], "y": coord[1]})

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        coord = self._normalized(event.position())
        btn = self._button_name(event.button())
        if coord and btn:
            self.mouse_event.emit(
                {"event": "mouse_up", "button": btn, "x": coord[0], "y": coord[1]})


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Client (MVP)")
        self.worker: NetworkWorker | None = None

        # ---- form kết nối ----
        self.host_edit = QLineEdit("100.")
        self.host_edit.setPlaceholderText("Tailscale IP, vd 100.x.y.z")
        self.port_edit = QLineEdit("7777")
        self.port_edit.setFixedWidth(70)
        self.token_edit = QLineEdit()
        self.token_edit.setPlaceholderText("token")
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)

        form = QHBoxLayout()
        form.addWidget(QLabel("Host:"))
        form.addWidget(self.host_edit)
        form.addWidget(QLabel("Port:"))
        form.addWidget(self.port_edit)
        form.addWidget(QLabel("Token:"))
        form.addWidget(self.token_edit)
        form.addWidget(self.connect_btn)

        # ---- vùng hiển thị + status ----
        self.view = RemoteView()
        self.view.mouse_event.connect(self._on_mouse_event)
        self.status_label = QLabel("Trạng thái: Disconnected")
        self.status_label.setStyleSheet("padding: 4px;")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(form)
        layout.addWidget(self.view, stretch=1)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)
        self.resize(1100, 720)

    # ---- connect / disconnect -------------------------------------------

    def _toggle_connection(self) -> None:
        if self.worker is None:
            self._connect()
        else:
            self._disconnect("Đã ngắt bởi người dùng")

    def _connect(self) -> None:
        host = self.host_edit.text().strip()
        token = self.token_edit.text()
        try:
            port = int(self.port_edit.text().strip())
        except ValueError:
            self.status_label.setText("Trạng thái: Port không hợp lệ")
            return
        if not host or not token:
            self.status_label.setText("Trạng thái: Cần nhập Host và Token")
            return

        self.worker = NetworkWorker(host, port, token)
        self.worker.frame_ready.connect(self.view.set_frame)
        self.worker.status.connect(
            lambda m: self.status_label.setText(f"Trạng thái: {m}"))
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.start()

        self.connect_btn.setText("Disconnect")
        self._set_form_enabled(False)
        self.status_label.setText("Trạng thái: Connecting...")

    def _disconnect(self, reason: str) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(2000)
            self.worker = None
        self.connect_btn.setText("Connect")
        self._set_form_enabled(True)
        self.view.clear_frame("Disconnected")
        self.status_label.setText(f"Trạng thái: Disconnected ({reason})")

    def _on_disconnected(self, reason: str) -> None:
        # Worker tự báo lỗi/đóng → dọn dẹp về trạng thái Connect.
        self._disconnect(reason)

    def _set_form_enabled(self, enabled: bool) -> None:
        for w in (self.host_edit, self.port_edit, self.token_edit):
            w.setEnabled(enabled)

    def _on_mouse_event(self, event: dict) -> None:
        if self.worker is not None:
            self.worker.send_control(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(2000)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
