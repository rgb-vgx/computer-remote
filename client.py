#!/usr/bin/env python3
"""client.py — Remote desktop CLIENT/VIEWER.

Supports JPEG and H.264 decoding. H.264 requires ffmpeg.

Usage:
  python client.py [--debug]
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import select
import socket
import subprocess as sp
import sys
import threading
import time

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QVBoxLayout, QWidget,
)

from common import app_log_dir, protocol

log = logging.getLogger("client")


# ---------------------------------------------------------------------------
# H.264 decoder (ffmpeg subprocess)
# ---------------------------------------------------------------------------

class H264Decoder:
    """Decode H.264 Annex B stream to raw BGR frames via ffmpeg."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.frame_size = width * height * 3
        self._buf = b''
        self._lock = threading.Lock()
        self._closed = False

        self._proc = sp.Popen(
            ['ffmpeg',
             '-f', 'h264',
             '-i', '-',
             '-f', 'rawvideo',
             '-pix_fmt', 'bgr24',
             '-flags', 'low_delay',
             '-'],
            stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL,
            bufsize=1024**2)

        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    def _reader_loop(self) -> None:
        fd = self._proc.stdout.fileno()
        nonblocking = False
        try:
            os.set_blocking(fd, False)
            nonblocking = True
        except (OSError, AttributeError):
            pass

        while not self._closed and self._proc.poll() is None:
            try:
                if nonblocking:
                    chunk = os.read(fd, 65536)
                else:
                    chunk = self._proc.stdout.read1(65536)
                if not chunk:
                    break
                with self._lock:
                    self._buf += chunk
            except BlockingIOError:
                time.sleep(0.01)
            except Exception:
                break

    def feed(self, data: bytes) -> None:
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except Exception:
            pass

    def read_frame(self) -> np.ndarray | None:
        with self._lock:
            if len(self._buf) >= self.frame_size:
                raw = self._buf[:self.frame_size]
                self._buf = self._buf[self.frame_size:]
                return np.frombuffer(raw, dtype=np.uint8).reshape(
                    self.height, self.width, 3)
        return None

    def close(self) -> None:
        self._closed = True
        try:
            self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            self._proc.kill()

    @staticmethod
    def available() -> bool:
        try:
            r = sp.run(['ffmpeg', '-version'],
                       capture_output=True, timeout=2, shell=False)
            return r.returncode == 0
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Key mapping
# ---------------------------------------------------------------------------

_QT_KEY_TO_NAME = {
    0x01000020: "shift", 0x01000021: "ctrl", 0x01000023: "alt",
    0x01000024: "cmd", 0x01001103: "alt_gr", 0x01000022: "caps_lock",
    0x01000004: "enter", 0x01000005: "enter", 0x01000001: "tab",
    0x01000003: "backspace", 0x01000000: "esc", 0x01000006: "delete",
    0x01000010: "home", 0x01000011: "end", 0x01000016: "page_up",
    0x01000017: "page_down", 0x01000007: "insert", 0x01000008: "menu",
    0x01000009: "pause", 0x0100000a: "print_screen",
    0x01000012: "left", 0x01000013: "up", 0x01000014: "right",
    0x01000015: "down",
    0x01000030: "f1",  0x01000031: "f2",  0x01000032: "f3",
    0x01000033: "f4",  0x01000034: "f5",  0x01000035: "f6",
    0x01000036: "f7",  0x01000037: "f8",  0x01000038: "f9",
    0x01000039: "f10", 0x0100003a: "f11", 0x0100003b: "f12",
}


def _qt_key_to_key_str(qt_key: int, text: str) -> str | None:
    name = _QT_KEY_TO_NAME.get(qt_key)
    if name:
        return f"Key.{name}"
    if text and text.isprintable():
        return text
    if qt_key == 0x20:
        return " "
    return None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    try:
        log_path = app_log_dir() / "client.log"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(fh)
        log.info("Client log file: %s", log_path)
    except OSError as exc:
        log.warning("Không mở được file log: %s", exc)
    if debug:
        log.info("DEBUG mode ON")


# ---------------------------------------------------------------------------
# Network worker
# ---------------------------------------------------------------------------

class NetworkWorker(QThread):
    frame_ready = Signal(QImage)
    status = Signal(str)
    disconnected = Signal(str)
    clipboard_from_host = Signal(str)

    def __init__(self, host: str, port: int, token: str) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.token = token
        self.control_queue: queue.Queue[dict] = queue.Queue()
        self._running = True
        self._sock: socket.socket | None = None
        self._h264_dec: H264Decoder | None = None
        self._frame_w = 0
        self._frame_h = 0

    def stop(self) -> None:
        self._running = False
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
        if self._running:
            self.control_queue.put(event)

    def run(self) -> None:
        sock: socket.socket | None = None
        try:
            self.status.emit(f"Đang kết nối tới {self.host}:{self.port} ...")
            sock = socket.create_connection((self.host, self.port), timeout=10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setblocking(True)

            protocol.send_json(sock, protocol.PKT_HELLO, {
                "token": self.token,
                "codecs": ["jpeg", "h264"] if H264Decoder.available() else ["jpeg"],
            })
            log.debug("Đã gửi HELLO tới %s:%d", self.host, self.port)

            self.status.emit("Đã gửi token, chờ host xác thực ...")
            self._sock = sock
            self._recv_loop()
        except OSError as exc:
            self.disconnected.emit(f"Không kết nối được: {exc}")
        except Exception as exc:
            self.disconnected.emit(f"Lỗi: {exc}")
        finally:
            if self._h264_dec:
                self._h264_dec.close()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _recv_loop(self) -> None:
        assert self._sock is not None
        while self._running:
            self._flush_control()

            ready, _, _ = select.select([self._sock], [], [], 0.01)
            if not ready:
                continue

            try:
                ptype, payload = protocol.recv_packet(self._sock)
            except ValueError as exc:
                log.error("Lỗi protocol: %s", exc)
                self.disconnected.emit(f"Lỗi: {exc}")
                return
            except (ConnectionError, OSError) as exc:
                if self._running:
                    self.disconnected.emit(f"Mất kết nối: {exc}")
                return

            if ptype == protocol.PKT_FRAME:
                self._handle_jpeg(payload)
            elif ptype == protocol.PKT_FRAME_H264:
                self._handle_h264(payload)
            elif ptype == protocol.PKT_CONTROL:
                self._handle_control(payload)
            elif ptype == protocol.PKT_INFO:
                self._flush_control()  # flush before potentially slow h264 init
                try:
                    info = protocol.decode_json(payload)
                except Exception:
                    continue
                msg = info.get("message", "")
                if info.get("type") == "error":
                    self.disconnected.emit(f"Host từ chối: {msg}")
                    return
                elif info.get("type") == "codec":
                    c = info.get("codec", "")
                    if c == "h264":
                        w = info.get("width", 0)
                        h = info.get("height", 0)
                        if w and h:
                            self._frame_w = w
                            self._frame_h = h
                            if H264Decoder.available():
                                try:
                                    self._h264_dec = H264Decoder(w, h)
                                    log.info("H.264 decoder ready (%dx%d)", w, h)
                                except Exception:
                                    log.warning("H.264 decoder init fail, yêu cầu host dùng JPEG")
                                    self.send_control({
                                        "event": "codec_request",
                                        "codec": "jpeg",
                                    })
                            else:
                                log.warning("ffmpeg not found, yêu cầu host dùng JPEG")
                                self.send_control({
                                    "event": "codec_request",
                                    "codec": "jpeg",
                                })
                    continue
                log.info("Kết nối thành công — %s", msg)
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
                log.debug("Gửi control: %s", event.get("event"))
            except OSError:
                return

    def _handle_jpeg(self, payload: bytes) -> None:
        arr = np.frombuffer(payload, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w, _ = img.shape
        qimg = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.frame_ready.emit(qimg)

    def _handle_h264(self, payload: bytes) -> None:
        if self._h264_dec is None:
            return
        self._h264_dec.feed(payload)
        img = self._h264_dec.read_frame()
        if img is not None:
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            h, w = self._frame_h, self._frame_w
            qimg = QImage(img_rgb.data, w, h, 3 * w,
                          QImage.Format_RGB888).copy()
            self.frame_ready.emit(qimg)

    def _handle_control(self, payload: bytes) -> None:
        try:
            event = protocol.decode_json(payload)
        except Exception:
            return
        if event.get("event") == "clipboard":
            text = event.get("text", "")
            if text:
                self.clipboard_from_host.emit(text)


# ---------------------------------------------------------------------------
# Remote view
# ---------------------------------------------------------------------------

class RemoteView(QLabel):
    mouse_event = Signal(dict)
    key_event = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(640, 360)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #101010; color: #aaa;")
        self.setText("Chưa kết nối")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._pixmap: QPixmap | None = None
        self._draw_rect = (0, 0, 0, 0)

    def set_frame(self, qimg: QImage) -> None:
        self._pixmap = QPixmap.fromImage(qimg)
        self.setText("")
        self._rescale()
        self.update()
        self.setFocus()

    def clear_frame(self, text: str) -> None:
        self._pixmap = None
        self.setPixmap(QPixmap())
        self.setText(text)

    def resizeEvent(self, event) -> None:
        self._rescale()
        super().resizeEvent(event)

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        area = self.size()
        scaled = self._pixmap.scaled(
            area, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = (area.width() - scaled.width()) // 2
        y = (area.height() - scaled.height()) // 2
        self._draw_rect = (x, y, scaled.width(), scaled.height())
        self.setPixmap(scaled)

    def _normalized(self, pos) -> tuple[float, float] | None:
        if self._pixmap is None:
            return None
        x0, y0, w, h = self._draw_rect
        if w <= 0 or h <= 0:
            return None
        nx = (pos.x() - x0) / w
        ny = (pos.y() - y0) / h
        if nx < 0 or nx > 1 or ny < 0 or ny > 1:
            return None
        return nx, ny

    @staticmethod
    def _button_name(qt_button) -> str | None:
        if qt_button == Qt.LeftButton:
            return "left"
        if qt_button == Qt.RightButton:
            return "right"
        return None

    def mouseMoveEvent(self, event) -> None:
        coord = self._normalized(event.position())
        if coord:
            self.mouse_event.emit(
                {"event": "mouse_move", "x": coord[0], "y": coord[1]})

    def mousePressEvent(self, event) -> None:
        coord = self._normalized(event.position())
        btn = self._button_name(event.button())
        if coord and btn:
            self.mouse_event.emit(
                {"event": "mouse_down", "button": btn,
                 "x": coord[0], "y": coord[1]})

    def mouseReleaseEvent(self, event) -> None:
        coord = self._normalized(event.position())
        btn = self._button_name(event.button())
        if coord and btn:
            self.mouse_event.emit(
                {"event": "mouse_up", "button": btn,
                 "x": coord[0], "y": coord[1]})

    def keyPressEvent(self, event) -> None:
        key_str = _qt_key_to_key_str(event.key(), event.text())
        if key_str:
            self.key_event.emit({"event": "key_down", "key": key_str})
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        key_str = _qt_key_to_key_str(event.key(), event.text())
        if key_str:
            self.key_event.emit({"event": "key_up", "key": key_str})
        super().keyReleaseEvent(event)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Client (MVP)")
        self.worker: NetworkWorker | None = None

        self._last_clipboard = ""
        self._clipboard_skip = 0

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

        self.view = RemoteView()
        self.view.mouse_event.connect(self._on_mouse_event)
        self.view.key_event.connect(self._on_key_event)
        self.status_label = QLabel("Trạng thái: Disconnected")
        self.status_label.setStyleSheet("padding: 4px;")
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(form)
        layout.addWidget(self.view, stretch=1)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)
        self.resize(1100, 720)

        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.timeout.connect(self._check_clipboard)

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

        log.info("Đang kết nối tới %s:%d ...", host, port)
        self.worker = NetworkWorker(host, port, token)
        self.worker.frame_ready.connect(self.view.set_frame)
        self.worker.status.connect(
            lambda m: self.status_label.setText(f"Trạng thái: {m}"))
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.clipboard_from_host.connect(self._on_clipboard_from_host)
        self.worker.start()

        self.connect_btn.setText("Disconnect")
        self._set_form_enabled(False)
        self.status_label.setText("Trạng thái: Connecting...")

        self._last_clipboard = ""
        self._clipboard_skip = 0
        self._clipboard_timer.start(500)

    def _disconnect(self, reason: str) -> None:
        log.info("Ngắt kết nối: %s", reason)
        self._clipboard_timer.stop()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(2000)
            self.worker = None
        self.connect_btn.setText("Connect")
        self._set_form_enabled(True)
        self.view.clear_frame("Disconnected")
        self.status_label.setText(f"Trạng thái: Disconnected ({reason})")

    def _on_disconnected(self, reason: str) -> None:
        self._disconnect(reason)

    def _set_form_enabled(self, enabled: bool) -> None:
        for w in (self.host_edit, self.port_edit, self.token_edit):
            w.setEnabled(enabled)

    def _on_mouse_event(self, event: dict) -> None:
        if self.worker is not None:
            self.worker.send_control(event)

    def _on_key_event(self, event: dict) -> None:
        if self.worker is not None:
            self.worker.send_control(event)

    def closeEvent(self, event) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(2000)
        super().closeEvent(event)

    def _check_clipboard(self) -> None:
        if self._clipboard_skip > 0:
            self._clipboard_skip -= 1
            return
        if self.worker is None:
            return
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if text and text != self._last_clipboard:
            self._last_clipboard = text
            log.debug("Client clipboard changed → gửi host (%d bytes)", len(text))
            self.worker.send_control(
                {"event": "clipboard", "text": text, "source": "client"})

    def _on_clipboard_from_host(self, text: str) -> None:
        self._last_clipboard = text
        self._clipboard_skip = 2
        QApplication.clipboard().setText(text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Remote desktop CLIENT (PySide6).")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    _setup_logging(args.debug)
    log.info("=== Remote Desktop Client ===")
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
