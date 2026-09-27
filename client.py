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
from logging.handlers import RotatingFileHandler

import cv2
import numpy as np
from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QIntValidator, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QVBoxLayout, QWidget,
)

from common import app_log_dir, no_console_kwargs, protocol
from common.updater import current_version
from common.updater_qt import UpdateController

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
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._closed = False

        self._proc = sp.Popen(
            ['ffmpeg',
             # Input pipe live: mặc định demuxer chờ đủ probesize/analyzeduration
             # mới phát frame đầu — hạ xuống 0 để decode ngay. KHÔNG dùng
             # -fflags nobuffer: nó drop packet khi bắt kịp stream.
             # -threads 1 để decoder không giữ frame theo frame-threading
             # (stream ngắn/tĩnh sẽ bị delay hoặc không ra frame).
             '-threads', '1',
             '-flags', 'low_delay',
             '-analyzeduration', '0',
             '-probesize', '32',
             '-f', 'h264',
             '-i', '-',
             '-f', 'rawvideo',
             '-pix_fmt', 'bgr24',
             '-'],
            stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL,
            bufsize=1024**2, **no_console_kwargs())

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
                # Copy 1 frame ra bytes trước khi xoá prefix: numpy giữ
                # memoryview vào _buf nên không thể resize khi view còn sống.
                raw = bytes(self._buf[:self.frame_size])
                del self._buf[:self.frame_size]
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
                       capture_output=True, timeout=2, shell=False,
                       **no_console_kwargs())
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
    if 0x21 <= qt_key <= 0x7E:
        # Tổ hợp Ctrl/Alt: Qt không cho text printable, dùng ký tự gốc
        # (vd Ctrl+C → "c"); modifier được gửi riêng qua Key.ctrl.
        return chr(qt_key).lower()
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
        fh = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
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
    connected = Signal()
    rejected = Signal(str)   # host từ chối (sai token, bị ngắt) — không retry
    disconnected = Signal(str)
    clipboard_from_host = Signal(str)
    monitors_ready = Signal(list, int)

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
                    self.rejected.emit(f"Host từ chối: {msg}")
                    return
                elif info.get("type") == "codec":
                    c = info.get("codec", "")
                    if c == "h264":
                        w = info.get("width", 0)
                        h = info.get("height", 0)
                        if w and h:
                            # Codec info gửi lại khi host đổi độ phân giải →
                            # tạo lại decoder cho kích thước mới.
                            if self._h264_dec is not None:
                                self._h264_dec.close()
                                self._h264_dec = None
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
                elif info.get("type") == "monitors":
                    self.monitors_ready.emit(
                        list(info.get("monitors", [])),
                        int(info.get("current", 1) or 1))
                    continue
                if info.get("type") != "info":
                    continue
                log.info("Kết nối thành công — %s", msg)
                self.status.emit(f"Đã kết nối — {msg}")
                self.connected.emit()

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
        # Cho phép IME (bộ gõ tiếng Việt/CJK) commit text vào widget này.
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)

        self._pixmap: QPixmap | None = None
        self._draw_rect = (0, 0, 0, 0)
        self._scroll_acc = [0, 0]

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
            area, Qt.KeepAspectRatio, Qt.FastTransformation)
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
        self.setFocus()
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

    def wheelEvent(self, event) -> None:
        coord = self._normalized(event.position())
        if coord is None:
            event.ignore()
            return
        delta = event.angleDelta()
        if delta.isNull():
            delta = event.pixelDelta()
        self._scroll_acc[0] += delta.x()
        self._scroll_acc[1] += delta.y()
        dx = int(self._scroll_acc[0] / 120)
        dy = int(self._scroll_acc[1] / 120)
        if dx or dy:
            self._scroll_acc[0] -= dx * 120
            self._scroll_acc[1] -= dy * 120
            self.mouse_event.emit(
                {"event": "scroll", "x": coord[0], "y": coord[1],
                 "dx": dx, "dy": dy})
        event.accept()

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

    def inputMethodEvent(self, event) -> None:
        """Text do IME/bộ gõ commit (Telex, Pinyin...) — gửi từng ký tự.

        Qt không sinh keyPress cho text IME đã commit; nếu bỏ qua thì gõ
        tiếng Việt/CJK bằng bộ gõ sẽ mất chữ.
        """
        commit = event.commitString()
        for ch in commit:
            self.key_event.emit({"event": "key_down", "key": ch})
            self.key_event.emit({"event": "key_up", "key": ch})
        if commit:
            event.accept()
            return
        super().inputMethodEvent(event)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Remote Desktop Client (MVP) v{current_version()}")
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
        self.update_btn = QPushButton("Cập nhật")
        self.update_btn.clicked.connect(self._check_update)
        self.updater = UpdateController(self, "client")

        self.res_combo = QComboBox()
        self.res_combo.setEditable(True)
        self.res_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.res_combo.lineEdit().setValidator(
            QIntValidator(160, 7680, self.res_combo))
        self.res_combo.addItem("Theo host", None)
        for label, width in (
            ("3840 (4K UHD)", 3840), ("2560 (QHD)", 2560),
            ("1920 (Full HD)", 1920), ("1680", 1680), ("1600 (HD+)", 1600),
            ("1440", 1440), ("1366", 1366), ("1280 (HD)", 1280),
            ("1152", 1152), ("1024", 1024), ("960", 960), ("854", 854),
            ("800", 800), ("720 (HD)", 720), ("640", 640), ("480", 480),
            ("384", 384), ("320", 320),
        ):
            self.res_combo.addItem(label, width)
        self.res_combo.setCurrentIndex(0)
        self.res_combo.setToolTip(
            "Độ phân giải stream tối đa (max-width, 160–7680).\n"
            "Thấp hơn = mượt hơn, ít băng thông hơn.\n"
            "'Theo host' = giữ nguyên tham số --max-width của host.\n"
            "Có thể gõ số tùy ý.")
        self.res_combo.currentIndexChanged.connect(self._on_resolution_changed)
        self.res_combo.lineEdit().editingFinished.connect(
            self._on_resolution_edited)

        self.monitor_combo = QComboBox()
        self.monitor_combo.addItem("Theo host", None)
        self.monitor_combo.setEnabled(False)
        self.monitor_combo.setToolTip(
            "Chọn màn hình host để xem/điều khiển.\n"
            "Host gửi danh sách khi kết nối; 'Theo host' = màn hình chính.")
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)

        form = QHBoxLayout()
        form.addWidget(QLabel("Host:"))
        form.addWidget(self.host_edit)
        form.addWidget(QLabel("Port:"))
        form.addWidget(self.port_edit)
        form.addWidget(QLabel("Token:"))
        form.addWidget(self.token_edit)
        form.addWidget(QLabel("Màn hình:"))
        form.addWidget(self.monitor_combo)
        form.addWidget(QLabel("Độ phân giải:"))
        form.addWidget(self.res_combo)
        form.addWidget(self.connect_btn)
        form.addWidget(self.update_btn)

        self.view = RemoteView()
        self.view.mouse_event.connect(self._on_mouse_event)
        self.view.key_event.connect(self._on_key_event)
        self.status_label = QLabel("Trạng thái: Disconnected")
        self.status_label.setStyleSheet("padding: 4px;")
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.size_label = QLabel("")
        self.size_label.setStyleSheet("padding: 4px; color: #888;")
        self.size_label.setToolTip("Kích thước frame nhận được từ host")

        bottom = QHBoxLayout()
        bottom.addWidget(self.status_label, stretch=1)
        bottom.addWidget(self.size_label)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(form)
        layout.addWidget(self.view, stretch=1)
        layout.addLayout(bottom)
        self.setCentralWidget(central)
        self.resize(1100, 720)

        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.timeout.connect(self._check_clipboard)

        self.settings = QSettings("computer-remote", "remote-client")
        self._want_connected = False
        self._reconnect_attempts = 0
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._reconnect_now)
        self._restore_settings()

    # ---- Settings --------------------------------------------------------

    def _restore_settings(self) -> None:
        host = self.settings.value("host", "", str)
        if host:
            self.host_edit.setText(host)
        self.port_edit.setText(self.settings.value("port", "7777", str))
        token = self.settings.value("token", "", str)
        if token:
            self.token_edit.setText(token)
        resolution = self.settings.value("resolution", "", str)
        if resolution:
            index = self.res_combo.findText(resolution)
            if index >= 0:
                self.res_combo.setCurrentIndex(index)
            else:
                self.res_combo.setEditText(resolution)
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        self.settings.setValue("host", self.host_edit.text().strip())
        self.settings.setValue("port", self.port_edit.text().strip())
        self.settings.setValue("token", self.token_edit.text())
        self.settings.setValue("resolution", self.res_combo.currentText())
        self.settings.setValue("geometry", self.saveGeometry())

    def _toggle_connection(self) -> None:
        if self._want_connected or self.worker is not None:
            self._disconnect("Đã ngắt bởi người dùng")
        else:
            self._connect()

    def _check_update(self) -> None:
        self.updater.start()

    def _selected_width(self) -> int | None:
        """Width đang chọn: None = 'Theo host', số = max-width cụ thể."""
        text = self.res_combo.currentText().strip()
        if not text or text.lower().startswith("theo"):
            return None
        first = text.split()[0]
        if not first.isdigit():
            return None
        return max(160, min(7680, int(first)))

    def _send_resolution(self) -> None:
        if self.worker is not None:
            self.worker.send_control(
                {"event": "set_resolution", "max_width": self._selected_width()})

    def _on_resolution_changed(self, _index: int = -1) -> None:
        self._send_resolution()

    def _on_resolution_edited(self) -> None:
        # Chỉ chuẩn hoá khi user gõ tay (index = -1), giữ nguyên nhãn preset.
        if self.res_combo.currentIndex() < 0:
            width = self._selected_width()
            if width is not None and self.res_combo.currentText() != str(width):
                self.res_combo.setEditText(str(width))
        self._send_resolution()

    def _on_monitors_ready(self, monitors: list, current: int) -> None:
        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        self.monitor_combo.addItem("Theo host", None)
        for mon in monitors:
            label = f"#{mon.get('index')}: {mon.get('width')}x{mon.get('height')}"
            if mon.get("primary"):
                label += " (chính)"
            self.monitor_combo.addItem(label, mon.get("index"))
        self.monitor_combo.setEnabled(len(monitors) > 1)
        index = self.monitor_combo.findData(current)
        self.monitor_combo.setCurrentIndex(index if index > 0 else 0)
        self.monitor_combo.blockSignals(False)

    def _on_monitor_changed(self, index: int) -> None:
        if self.worker is None:
            return
        data = self.monitor_combo.itemData(index)
        self.worker.send_control({"event": "set_monitor",
                                  "index": int(data or 1)})

    def _reset_monitor_combo(self) -> None:
        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        self.monitor_combo.addItem("Theo host", None)
        self.monitor_combo.setEnabled(False)
        self.monitor_combo.blockSignals(False)

    def _on_frame_ready(self, img) -> None:
        size = f"{img.width()}x{img.height()}"
        if self.size_label.text() != size:
            self.size_label.setText(size)

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

        self._reconnect_timer.stop()
        self._want_connected = True
        self._save_settings()

        log.info("Đang kết nối tới %s:%d ...", host, port)
        self.worker = NetworkWorker(host, port, token)
        self.worker.frame_ready.connect(self.view.set_frame)
        self.worker.frame_ready.connect(self._on_frame_ready)
        self.worker.status.connect(
            lambda m: self.status_label.setText(f"Trạng thái: {m}"))
        self.worker.connected.connect(self._on_connected)
        self.worker.rejected.connect(self._on_rejected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.clipboard_from_host.connect(self._on_clipboard_from_host)
        self.worker.monitors_ready.connect(self._on_monitors_ready)
        self.worker.start()
        width = self._selected_width()
        if width is not None:
            self.worker.send_control(
                {"event": "set_resolution", "max_width": width})

        self.connect_btn.setText("Disconnect")
        self._set_form_enabled(False)
        if self._reconnect_attempts == 0:
            self.status_label.setText("Trạng thái: Connecting...")
        self.view.setFocus()  # để phím đi vào RemoteView ngay từ đầu

        self._last_clipboard = ""
        self._clipboard_skip = 0
        self._clipboard_timer.start(500)

    def _on_connected(self) -> None:
        self._reconnect_attempts = 0

    def _on_rejected(self, reason: str) -> None:
        # Host từ chối (sai token / bị ngắt) — dừng hẳn, không thử lại.
        self._want_connected = False
        self._disconnect(reason)

    def _on_disconnected(self, reason: str) -> None:
        self._teardown_worker()
        if self._want_connected:
            self._reconnect_attempts += 1
            delay = min(10, 2 ** min(self._reconnect_attempts - 1, 4))
            self.status_label.setText(
                f"Trạng thái: Mất kết nối — thử lại sau {delay}s "
                f"(lần {self._reconnect_attempts}): {reason}")
            log.info("Sẽ kết nối lại sau %ds (lần %d): %s",
                     delay, self._reconnect_attempts, reason)
            self._reconnect_timer.start(delay * 1000)
            return
        self._disconnect(reason)

    def _reconnect_now(self) -> None:
        if not self._want_connected:
            return
        if not self.isVisible():
            self._disconnect("Cửa sổ đã đóng")
            return
        self._connect()

    def _teardown_worker(self) -> None:
        self._clipboard_timer.stop()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(2000)
            self.worker = None

    def _disconnect(self, reason: str) -> None:
        log.info("Ngắt kết nối: %s", reason)
        self._want_connected = False
        self._reconnect_timer.stop()
        self._reconnect_attempts = 0
        self._teardown_worker()
        self.connect_btn.setText("Connect")
        self._set_form_enabled(True)
        self._reset_monitor_combo()
        self.view.clear_frame("Disconnected")
        self.size_label.setText("")
        self.status_label.setText(f"Trạng thái: Disconnected ({reason})")

    def _set_form_enabled(self, enabled: bool) -> None:
        for w in (self.host_edit, self.port_edit, self.token_edit):
            w.setEnabled(enabled)

    def _on_mouse_event(self, event: dict) -> None:
        if self.worker is not None:
            self.worker.send_control(event)

    def _on_key_event(self, event: dict) -> None:
        log.debug("Key → host: %s %s", event.get("event"), event.get("key"))
        if self.worker is not None:
            self.worker.send_control(event)

    def closeEvent(self, event) -> None:
        self._want_connected = False
        self._reconnect_timer.stop()
        self._save_settings()
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
