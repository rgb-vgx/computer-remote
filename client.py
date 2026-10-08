#!/usr/bin/env python3
"""client.py — Remote desktop CLIENT/VIEWER.

Supports JPEG and H.264 decoding. H.264 requires ffmpeg.

Usage:
  python client.py [--debug]
"""

from __future__ import annotations

import argparse
import base64
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
from PySide6.QtCore import QEvent, QObject, QPointF, QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QCursor, QDesktopServices, QFont, QImage, QIntValidator,
    QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QProgressBar, QPushButton, QToolButton, QVBoxLayout,
    QWidget,
)

from common import app_log_dir, filetransfer, no_console_kwargs, protocol, ui
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
                    chunk = os.read(fd, 262144)
                else:
                    chunk = self._proc.stdout.read1(262144)
                if not chunk:
                    break
                with self._lock:
                    self._buf += chunk
            except BlockingIOError:
                # 1ms (không phải 10ms): EAGAIN là "chưa có byte tới" —
                # wake sớm để kịp nhận frame khi pipe vừa có dữ liệu;
                # 10ms thêm độ trễ thấy rõ cho feed -> frame-ready.
                time.sleep(0.001)
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
    0x01000022: "cmd", 0x01001103: "alt_gr", 0x01000024: "caps_lock",
    0x01000004: "enter", 0x01000005: "enter", 0x01000001: "tab",
    0x01000002: "tab",
    0x01000003: "backspace", 0x01000000: "esc", 0x01000007: "delete",
    0x01000010: "home", 0x01000011: "end", 0x01000016: "page_up",
    0x01000017: "page_down", 0x01000006: "insert", 0x01000055: "menu",
    0x01000008: "pause", 0x01000009: "print_screen",
    0x0100000a: "print_screen",
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
    permissions_ready = Signal(dict)  # quyền của phiên (control/clipboard/files)
    cursor_shape = Signal(dict)      # {"shape": tên} hoặc {"png", "hx", "hy"}
    cursor_pos = Signal(float, float)  # vị trí con trỏ host (chuẩn hoá 0..1)
    pong = Signal(float)             # round-trip ms
    quality_info = Signal(dict)      # bậc chất lượng host đang dùng (tự chỉnh)
    file_progress = Signal(bool, str, int, int)  # outgoing, tên, đã, tổng
    file_done = Signal(bool, str, bool, str)     # outgoing, tên, ok, chi tiết

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
        # generation encoder của host: bỏ packet codec cũ đến trễ (defense-in-depth,
        # TCP đã thứ tự hóa — client chỉ bảo vệ khi reconnect/re-negotiate).
        self._codec_generation = 0
        # Thống kê: tổng byte nhận (MainWindow đọc định kỳ để tính bitrate).
        self.bytes_received = 0
        # Số frame đã nhận — ack cho host (host chỉ gửi tiếp khi mình theo kịp).
        self._frames_received = 0
        self._ack_pending = False
        # Truyền file
        self._file_queue: queue.Queue[str] = queue.Queue()
        self._outgoing = filetransfer.SendQueue()
        self._incoming = filetransfer.IncomingFiles()

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

    def send_file(self, path: str) -> None:
        if self._running:
            self._file_queue.put(str(path))

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
                "features": ["frame_ack"],
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
            self._abort_transfers()
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
            self._pump_files()

            wait = 0.0 if self._outgoing.busy else 0.01
            ready, _, _ = select.select([self._sock], [], [], wait)
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

            self.bytes_received += protocol.HEADER_SIZE + len(payload)
            if ptype in (protocol.PKT_FRAME, protocol.PKT_FRAME_H264):
                # Ack SAU khi decode: client CPU chậm cũng là "nghẽn".
                if ptype == protocol.PKT_FRAME:
                    self._handle_jpeg(payload)
                else:
                    self._handle_h264(payload)
                self._frames_received += 1
                self._ack_pending = True
            elif ptype == protocol.PKT_FILE_DATA:
                self._on_file_data(payload)
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
                    self._handle_codec_info(info)
                    continue
                elif info.get("type") == "monitors":
                    self.monitors_ready.emit(
                        list(info.get("monitors", [])),
                        int(info.get("current", 1) or 1))
                    continue
                elif info.get("type") == "permissions":
                    self.permissions_ready.emit(dict(info))
                    continue
                elif info.get("type") == "quality":
                    self.quality_info.emit(dict(info))
                    continue
                if info.get("type") != "info":
                    continue
                log.info("Kết nối thành công — %s", msg)
                self.status.emit(f"Đã kết nối — {msg}")
                self.connected.emit()

    def _handle_codec_info(self, info: dict) -> None:
        """Nhận packet {type: codec}: bỏ packet cũ (generation), dựng decoder.

        Packet có ``generation`` (host mới): bỏ nếu đã nhận generation ≥ nó
        (đến trễ sau khi network giật). Host cũ không gửi → xử lý như trước.
        """
        gen = info.get("generation")
        if gen is not None:
            try:
                gen = int(gen)
            except (TypeError, ValueError):
                gen = None
        if gen is not None:
            if gen <= self._codec_generation:
                return  # packet codec cũ (tạo lại trước đó) — bỏ
            self._codec_generation = gen
        if info.get("codec", "") != "h264":
            return
        w = info.get("width", 0)
        h = info.get("height", 0)
        if not (w and h):
            return
        # Codec info gửi lại khi host đổi độ phân giải → tạo lại decoder
        # cho kích thước mới.
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

    def _flush_control(self) -> None:
        assert self._sock is not None
        if self._ack_pending:
            # Gộp: nhiều frame nhận liền nhau chỉ cần 1 ack (số mới nhất).
            self._ack_pending = False
            try:
                protocol.send_json(self._sock, protocol.PKT_CONTROL,
                                   {"event": "frame_ack",
                                    "n": self._frames_received})
            except OSError:
                return
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
        # Poll ngắn ~3ms sau feed: decode thường xong trong vài ms — nếu frame
        # ready thì paint NGAY packet này. Gọi read đúng 1 lần như cũ gần như
        # luôn trả None → frame phải chờ packet kế tiếp (+1 period =
        # 33–66ms @15–30fps) → lag thấy rõ so với JPEG (decode inline).
        deadline = time.perf_counter() + 0.003
        img = self._h264_dec.read_frame()
        while img is None and time.perf_counter() < deadline:
            time.sleep(0.0005)
            img = self._h264_dec.read_frame()
        if img is None:
            return
        # Drain-to-latest: mỗi packet có thể để lại nhiều frame đã decode —
        # đọc hết và chỉ paint frame MỚI NHẤT. Nếu chỉ đọc 1 frame/packet mà
        # decoder chậm hơn network thì backlog tích lùi, lag tăng vô hạn
        # (~67ms/frame tồn đọng @15fps).
        while True:
            nxt = self._h264_dec.read_frame()
            if nxt is None:
                break
            img = nxt
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
        kind = event.get("event")
        if kind == "clipboard":
            text = event.get("text", "")
            if text:
                self.clipboard_from_host.emit(text)
        elif kind == "cursor_pos":
            try:
                self.cursor_pos.emit(float(event["x"]), float(event["y"]))
            except (KeyError, TypeError, ValueError):
                pass
        elif kind == "cursor":
            self.cursor_shape.emit(event)
        elif kind == "pong":
            try:
                rtt = time.monotonic() * 1000.0 - float(event.get("t"))
            except (TypeError, ValueError):
                return
            if 0 <= rtt < 60000:
                self.pong.emit(rtt)
        elif kind in ("file_begin", "file_end", "file_cancel", "file_result"):
            self._on_file_event(event)

    # ---- File transfer (chạy trong thread mạng) ------------------------

    def _send_file_result(self, transfer_id, ok: bool, name: str,
                          error: str = "") -> None:
        event = {"event": "file_result", "id": transfer_id, "ok": ok,
                 "name": name}
        if error:
            event["error"] = error
        self.send_control(event)

    def _on_file_event(self, event: dict) -> None:
        kind = event.get("event")
        transfer_id = event.get("id")
        if kind == "file_result":
            ok = bool(event.get("ok"))
            self.file_done.emit(True, str(event.get("name", "")), ok,
                                "" if ok else str(event.get("error", "")))
        elif kind == "file_begin":
            try:
                name = self._incoming.begin(event)
            except filetransfer.TransferError as exc:
                self._send_file_result(transfer_id, False,
                                       str(event.get("name", "")), str(exc))
                return
            self.file_progress.emit(False, name, 0, int(event.get("size", 0) or 0))
        elif kind == "file_end":
            name = self._incoming.name_of(int(transfer_id or 0))
            try:
                path = self._incoming.end(event)
            except filetransfer.TransferError as exc:
                self._send_file_result(transfer_id, False, name, str(exc))
                self.file_done.emit(False, name, False, str(exc))
                return
            log.info("Đã nhận file từ host: %s", path)
            self._send_file_result(transfer_id, True, path.name)
            self.file_done.emit(False, path.name, True, str(path))
        elif kind == "file_cancel":
            name = self._incoming.name_of(int(transfer_id or 0))
            self._incoming.cancel(int(transfer_id or 0))
            if name:
                self.file_done.emit(False, name, False, "host đã huỷ")

    def _on_file_data(self, payload: bytes) -> None:
        try:
            _id, name, done, total = self._incoming.data(payload)
        except filetransfer.TransferError as exc:
            log.warning("Nhận file lỗi: %s", exc)
            return
        self.file_progress.emit(False, name, done, total)

    def _pump_files(self) -> None:
        while True:
            try:
                self._outgoing.add(self._file_queue.get_nowait())
            except queue.Empty:
                break
        if not self._outgoing.busy:
            return
        # Chỉ gửi khi socket còn chỗ: không kẹt sendall trong lúc host đang
        # gửi frame cho mình (hai bên cùng đầy buffer).
        _, writable, _ = select.select([], [self._sock], [], 0)
        if not writable:
            return
        sock = self._sock
        self._outgoing.pump(
            lambda ev: protocol.send_json(sock, protocol.PKT_CONTROL, ev),
            lambda data: protocol.send_packet(sock, protocol.PKT_FILE_DATA, data),
            budget=2 * filetransfer.CHUNK_SIZE,
            on_progress=lambda name, done, total: self.file_progress.emit(
                True, name, done, total),
            on_error=lambda name, err: self.file_done.emit(True, name, False, err))

    def _abort_transfers(self) -> None:
        self._incoming.abort_all()
        current = self._outgoing.current
        if current is not None:
            self.file_done.emit(True, current.name, False, "mất kết nối")
        self._outgoing.cancel_all()


# ---------------------------------------------------------------------------
# Remote view
# ---------------------------------------------------------------------------

# Tên hình con trỏ host gửi → Qt.CursorShape (con trỏ local đổi theo host).
_CURSOR_SHAPES = {
    "arrow": Qt.CursorShape.ArrowCursor, "ibeam": Qt.CursorShape.IBeamCursor,
    "wait": Qt.CursorShape.WaitCursor, "busy": Qt.CursorShape.BusyCursor,
    "cross": Qt.CursorShape.CrossCursor, "up_arrow": Qt.CursorShape.UpArrowCursor,
    "hand": Qt.CursorShape.PointingHandCursor,
    "size_h": Qt.CursorShape.SizeHorCursor, "size_v": Qt.CursorShape.SizeVerCursor,
    "size_fd": Qt.CursorShape.SizeFDiagCursor,
    "size_bd": Qt.CursorShape.SizeBDiagCursor,
    "size_all": Qt.CursorShape.SizeAllCursor,
    "forbidden": Qt.CursorShape.ForbiddenCursor,
    "help": Qt.CursorShape.WhatsThisCursor,
    "hidden": Qt.CursorShape.BlankCursor,
}


def cursor_from_event(event: dict) -> QCursor | None:
    """Event ``cursor`` của host → QCursor (theo tên chuẩn hoặc ảnh PNG)."""
    if "shape" in event:
        shape = _CURSOR_SHAPES.get(str(event.get("shape")))
        return QCursor(shape) if shape is not None else None
    try:
        data = base64.b64decode(event.get("png", ""), validate=True)
    except (ValueError, TypeError):
        return None
    pixmap = QPixmap()
    if not data or not pixmap.loadFromData(data, "PNG"):
        return None
    return QCursor(pixmap, int(event.get("hx", 0)), int(event.get("hy", 0)))


class RemoteView(QLabel):
    mouse_event = Signal(dict)
    key_event = Signal(dict)
    # Báo MainWindow: vùng hiển thị đổi kích thước (dùng cho độ phân giải Tự động)
    view_resized = Signal()
    files_dropped = Signal(list)
    fullscreen_requested = Signal()
    focus_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(640, 360)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(f"background-color: {ui.COLORS['canvas']};")
        self.setText("Chưa kết nối")
        self._hint = "Nhập địa chỉ Host và Token, rồi bấm Kết nối"
        self._empty_icon = ui.icon_pixmap("monitor", ui.COLORS["subtle"], 56)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Cho phép IME (bộ gõ tiếng Việt/CJK) commit text vào widget này.
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)

        # Invariant B0: _frame = frame host gửi, kích thước PIXEL THẬT (physical);
        # dpr chỉ dùng để Qt map physical -> logical khi vẽ.
        self._frame: QImage | None = None
        self._pixmap: QPixmap | None = None
        self._draw_rect = (0.0, 0.0, 0.0, 0.0)
        self._scroll_acc = [0, 0]
        # Con trỏ host: vị trí (chuẩn hoá) + vị trí local vừa gửi. Khi người
        # ở máy host tự di chuột (lệch xa vị trí mình gửi) thì vẽ overlay.
        self._remote_pos: tuple[float, float] | None = None
        self._sent_pos: tuple[float, float] | None = None
        self._remote_hidden = False
        self.accept_files = False
        self.setAcceptDrops(True)

    def set_frame(self, qimg: QImage) -> None:
        self._frame = qimg
        self.setText("")
        self._rescale()
        self.update()
        # Không setFocus() ở đây: mỗi frame (~15/s) sẽ cướp focus khi user
        # đang gõ ở form (token, độ phân giải). Focus sang view khi Connect
        # (_connect) hoặc click vào vùng xem (mousePressEvent).

    def clear_frame(self, text: str, hint: str = "") -> None:
        self._frame = None
        self._pixmap = None
        self._hint = hint
        self._remote_pos = None
        self._sent_pos = None
        self._remote_hidden = False
        self.unsetCursor()
        self.setPixmap(QPixmap())
        self.setText(text)
        self.update()

    # ---- Con trỏ host ----------------------------------------------------

    def set_remote_cursor(self, event: dict) -> None:
        cursor = cursor_from_event(event)
        if cursor is None:
            return
        self._remote_hidden = event.get("shape") == "hidden"
        self.setCursor(cursor)

    def set_remote_pos(self, x: float, y: float) -> None:
        old = self._remote_pos
        self._remote_pos = (x, y)
        if old is not None or self._overlay_visible():
            self.update()

    def _overlay_visible(self) -> bool:
        if self._remote_pos is None or self._frame is None or self._remote_hidden:
            return False
        if self._sent_pos is None or not self.underMouse():
            return True
        dx = self._remote_pos[0] - self._sent_pos[0]
        dy = self._remote_pos[1] - self._sent_pos[1]
        return dx * dx + dy * dy > 0.0004  # lệch > ~2% khung hình

    def _paint_remote_cursor(self, painter: QPainter) -> None:
        x0, y0, w, h = self._draw_rect
        px = x0 + self._remote_pos[0] * w
        py = y0 + self._remote_pos[1] * h
        path = QPainterPath(QPointF(px, py))
        for dx, dy in ((0, 16), (4.5, 12), (7.5, 18.5), (10, 17.3),
                       (7, 11), (12, 11)):
            path.lineTo(px + dx, py + dy)
        path.closeSubpath()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#000000"), 1.2))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawPath(path)

    def paintEvent(self, event) -> None:
        if self._frame is not None:
            super().paintEvent(event)
            if self._overlay_visible():
                painter = QPainter(self)
                self._paint_remote_cursor(painter)
                painter.end()
            return
        # Empty state: icon + tiêu đề + gợi ý, căn giữa vùng xem.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(ui.COLORS["canvas"]))
        icon_size = 56
        cx, cy = self.width() / 2, self.height() / 2
        painter.drawPixmap(int(cx - icon_size / 2), int(cy - 64), self._empty_icon)
        title_font = QFont(self.font())
        title_font.setPixelSize(16)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(ui.COLORS["text"]))
        painter.drawText(0, int(cy + 4), self.width(), 24,
                         Qt.AlignmentFlag.AlignHCenter, self.text())
        if self._hint:
            hint_font = QFont(self.font())
            hint_font.setPixelSize(13)
            painter.setFont(hint_font)
            painter.setPen(QColor(ui.COLORS["muted"]))
            painter.drawText(24, int(cy + 32), self.width() - 48, 40,
                             Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
                             self._hint)
        painter.end()

    def resizeEvent(self, event) -> None:
        self._rescale()
        super().resizeEvent(event)
        self.view_resized.emit()

    def _rescale(self) -> None:
        """Scale frame vào vùng hiển thị theo physical pixel.

        - Frame == kích thước physical của view -> render 1:1, KHÔNG resample
          (nét tối đa, đúng mục tiêu "như AnyDesk").
        - Khác -> SmoothTransformation (bilinear), KHÔNG dùng FastTransformation
          (nearest neighbor đang bỏ ~50% pixel khi co 1.8x).
        - QPixmap mang dpr của view để QLabel vẽ đúng logical size, tránh
          scale hai lần trên màn hình HiDPI.
        """
        if self._frame is None:
            return
        dpr = self.devicePixelRatioF() or 1.0
        fw, fh = self._frame.width(), self._frame.height()
        vw, vh = self.width() * dpr, self.height() * dpr
        if fw <= 0 or fh <= 0 or vw <= 0 or vh <= 0:
            return
        scale = min(vw / fw, vh / fh)
        if 0.995 < scale < 1.005:
            img = self._frame  # 1:1 physical — bỏ qua resample
        else:
            img = self._frame.scaled(
                max(1, round(fw * scale)), max(1, round(fh * scale)),
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
        disp = QImage(img)  # copy chia sẻ pixel (COW) — chỉ đổi metadata dpr
        disp.setDevicePixelRatio(dpr)
        self._pixmap = QPixmap.fromImage(disp)
        # _draw_rect theo logical để toạ độ chuột (logical) khớp khi map.
        lw = self._pixmap.width() / dpr
        lh = self._pixmap.height() / dpr
        self._draw_rect = ((self.width() - lw) / 2, (self.height() - lh) / 2,
                           lw, lh)
        self.setPixmap(self._pixmap)

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
        if qt_button == Qt.MiddleButton:
            return "middle"
        return None

    def event(self, event) -> bool:
        # Tab/Shift+Tab: Qt dùng để chuyển focus giữa widget trước cả
        # keyPressEvent — chặn lại để phím Tab đi sang host.
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)):
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focus_changed.emit(False)

    def dragEnterEvent(self, event) -> None:
        if self.accept_files and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile() and os.path.isfile(url.toLocalFile())]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    def mouseMoveEvent(self, event) -> None:
        coord = self._normalized(event.position())
        if coord:
            was_visible = self._overlay_visible()
            self._sent_pos = coord
            if was_visible:
                self.update()  # mình lại cầm chuột → ẩn overlay con trỏ host
            self.mouse_event.emit(
                {"event": "mouse_move", "x": coord[0], "y": coord[1]})

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        if self._remote_pos is not None:
            self.update()

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
        mods = event.modifiers()
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and mods & Qt.KeyboardModifier.ControlModifier
                and mods & Qt.KeyboardModifier.AltModifier):
            # Ctrl+Alt+Enter: phím tắt local bật/tắt toàn màn hình.
            if not event.isAutoRepeat():
                self.fullscreen_requested.emit()
            return
        key_str = _qt_key_to_key_str(event.key(), event.text())
        if key_str:
            self.key_event.emit({"event": "key_down", "key": key_str})
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        key_str = _qt_key_to_key_str(event.key(), event.text())
        if key_str:
            self.key_event.emit({"event": "key_up", "key": key_str})
            event.accept()
        else:
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
# Phím hệ thống (Alt+Tab, Win, Alt+F4...) → host
# ---------------------------------------------------------------------------

# Tổ hợp gửi nhanh từ menu "Gửi phím" (key_down theo thứ tự, key_up ngược lại).
KEY_COMBOS = (
    ("Ctrl + Alt + Del", ("Key.ctrl", "Key.alt", "Key.delete")),
    ("Ctrl + Shift + Esc  (Task Manager)", ("Key.ctrl", "Key.shift", "Key.esc")),
    ("Alt + Tab", ("Key.alt", "Key.tab")),
    ("Alt + F4", ("Key.alt", "Key.f4")),
    ("Phím Windows / Meta", ("Key.cmd",)),
    ("Win + L  (khoá máy)", ("Key.cmd", "l")),
    ("Print Screen", ("Key.print_screen",)),
)


def combo_events(keys) -> list[dict]:
    return ([{"event": "key_down", "key": k} for k in keys]
            + [{"event": "key_up", "key": k} for k in reversed(keys)])


_VK_LWIN, _VK_RWIN, _VK_APPS = 0x5B, 0x5C, 0x5D


def win_system_key(vk: int, alt: bool, ctrl: bool) -> str | None:
    """Phím Windows sẽ "ăn" mất trước khi tới app → tên phím để chuyển sang host.

    None = để Windows/Qt xử lý bình thường.
    """
    if vk in (_VK_LWIN, _VK_RWIN):
        return "Key.cmd"
    if vk == _VK_APPS:
        return "Key.menu"
    if vk == 0x2C:  # VK_SNAPSHOT
        return "Key.print_screen"
    if vk == 0x09:           # Tab (thường, Shift+Tab, Ctrl+Tab, Alt+Tab)
        return "Key.tab"
    if alt and vk == 0x73:   # Alt+F4 (không đóng cửa sổ client)
        return "Key.f4"
    if alt and vk == 0x20:   # Alt+Space (menu cửa sổ)
        return " "
    if (alt or ctrl) and vk == 0x1B:  # Alt+Esc, Ctrl+Esc (Start)
        return "Key.esc"
    return None


class SystemKeyCapture(QObject):
    """Chuyển phím hệ thống sang host khi vùng xem đang có focus.

    - Windows: low-level keyboard hook (WH_KEYBOARD_LL) chặn Alt+Tab, phím
      Win... chỉ khi cửa sổ client ở foreground và RemoteView có focus.
    - Linux/X11: ``grabKeyboard()`` (XGrabKeyboard) khi RemoteView có focus,
      nên phím tắt của KWin/GNOME đi vào app; nhả ra khi mất focus.
    """

    def __init__(self, view: "RemoteView", emit_key) -> None:
        super().__init__(view)
        self.view = view
        self.emit_key = emit_key
        self.enabled = True
        self.active = False
        self._grabbed = False
        self._hook = None
        self._hook_proc = None
        self._swallowed: dict[int, str] = {}
        view.focus_changed.connect(self._on_focus)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._update()

    def set_active(self, active: bool) -> None:
        self.active = active
        self._update()

    @property
    def wanted(self) -> bool:
        return self.enabled and self.active

    def _update(self) -> None:
        if sys.platform == "win32":
            if self.wanted:
                self._install_hook()
            else:
                self._remove_hook()
        else:
            self._on_focus(self.view.hasFocus())

    def _on_focus(self, focused: bool) -> None:
        if sys.platform == "win32":
            return
        if focused and self.wanted and not self._grabbed:
            self.view.grabKeyboard()
            self._grabbed = True
        elif self._grabbed and not (focused and self.wanted):
            self.view.releaseKeyboard()
            self._grabbed = False

    # ---- Windows hook ----------------------------------------------------

    def _install_hook(self) -> None:
        if self._hook is not None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            lresult = ctypes.c_ssize_t
            hookproc = ctypes.WINFUNCTYPE(lresult, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM)

            class KBDLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [("vkCode", wintypes.DWORD),
                            ("scanCode", wintypes.DWORD),
                            ("flags", wintypes.DWORD),
                            ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t)]

            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, hookproc, wintypes.HINSTANCE, wintypes.DWORD]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = [
                wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user32.CallNextHookEx.restype = lresult
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            def proc(code, wparam, lparam):
                try:
                    if code == 0:
                        kb = ctypes.cast(
                            lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        ctrl = bool(user32.GetAsyncKeyState(0x11) & 0x8000)
                        if self._handle_win_key(kb.vkCode, wparam, kb.flags,
                                                ctrl, user32):
                            return 1
                except Exception as exc:
                    log.debug("Keyboard hook lỗi: %s", exc)
                return user32.CallNextHookEx(None, code, wparam, lparam)

            self._user32 = user32
            self._hook_proc = hookproc(proc)  # giữ tham chiếu, tránh bị GC
            self._hook = user32.SetWindowsHookExW(
                13, self._hook_proc, kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                log.warning("Không cài được keyboard hook (lỗi %d)",
                            ctypes.get_last_error())
                self._hook = None
            else:
                log.info("Bật chuyển phím hệ thống sang host")
        except Exception as exc:
            log.warning("Không bật được chuyển phím hệ thống: %s", exc)
            self._hook = None

    def _remove_hook(self) -> None:
        if self._hook is None:
            return
        try:
            self._user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None
        self._hook_proc = None
        for key in self._swallowed.values():
            self.emit_key({"event": "key_up", "key": key})
        self._swallowed.clear()

    def _handle_win_key(self, vk: int, msg: int, flags: int, ctrl: bool,
                        user32) -> bool:
        """True = đã chuyển sang host, chặn không cho Windows xử lý."""
        if flags & 0x10:  # LLKHF_INJECTED (do chính SendInput tạo ra)
            return False
        is_down = msg in (0x100, 0x104)   # WM_KEYDOWN, WM_SYSKEYDOWN
        if not is_down:
            key = self._swallowed.pop(vk, None)
            if key is None:
                return False
            self.emit_key({"event": "key_up", "key": key})
            return True
        if vk in self._swallowed:  # auto-repeat của phím đã chặn
            self.emit_key({"event": "key_down", "key": self._swallowed[vk]})
            return True
        if not self.view.hasFocus():
            return False
        if user32.GetForegroundWindow() != int(self.view.window().winId()):
            return False
        key = win_system_key(vk, alt=bool(flags & 0x20), ctrl=ctrl)
        if key is None:
            return False
        self._swallowed[vk] = key
        self.emit_key({"event": "key_down", "key": key})
        return True

    def shutdown(self) -> None:
        self.active = False
        self._remove_hook()
        if self._grabbed:
            self.view.releaseKeyboard()
            self._grabbed = False


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

# itemData sentinel của combo "Độ phân giải" cho chế độ tự động (fit cửa sổ).
RES_AUTO = "auto"


class MainWindow(QMainWindow):
    TEXT_CONNECT = "Kết nối"
    TEXT_DISCONNECT = "Ngắt kết nối"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Remote Desktop Client v{current_version()}")
        self.setWindowIcon(ui.app_icon())
        self.worker: NetworkWorker | None = None

        self._last_clipboard = ""
        self._clipboard_skip = 0

        self.host_edit = QLineEdit("100.")
        self.host_edit.setPlaceholderText("100.x.y.z")
        self.host_edit.setToolTip("Địa chỉ IP Tailscale của máy host")
        self.host_edit.setAccessibleName("Địa chỉ host")
        self.host_edit.setMinimumWidth(150)
        self.host_edit.setFont(ui.mono_font(13))
        self.port_edit = QLineEdit("7777")
        self.port_edit.setFixedWidth(68)
        self.port_edit.setValidator(QIntValidator(1, 65535, self.port_edit))
        self.port_edit.setAccessibleName("Port")
        self.port_edit.setFont(ui.mono_font(13))
        self.token_edit = QLineEdit()
        self.token_edit.setPlaceholderText("token")
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setAccessibleName("Token")
        self.token_edit.setMinimumWidth(120)
        self.show_token_btn = QToolButton()
        self.show_token_btn.setCheckable(True)
        self.show_token_btn.setProperty("variant", "ghost")
        self.show_token_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_token_btn.toggled.connect(self._on_show_token)
        self._on_show_token(False)
        self.connect_btn = QPushButton(self.TEXT_CONNECT)
        self.connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connect_btn.setMinimumWidth(130)
        self.connect_btn.setDefault(True)
        self.connect_btn.clicked.connect(self._toggle_connection)
        for edit in (self.host_edit, self.port_edit, self.token_edit):
            edit.returnPressed.connect(self._connect_from_form)
        self.update_btn = QToolButton()
        self.update_btn.setIcon(ui.icon("refresh", ui.COLORS["muted"]))
        self.update_btn.setProperty("variant", "ghost")
        self.update_btn.setToolTip("Kiểm tra cập nhật")
        self.update_btn.setAccessibleName("Kiểm tra cập nhật")
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.clicked.connect(self._check_update)
        self.updater = UpdateController(self, "client")

        self.res_combo = QComboBox()
        self.res_combo.setEditable(True)
        self.res_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.res_combo.lineEdit().setValidator(
            QIntValidator(160, 7680, self.res_combo))
        self.res_combo.addItem("Tự động (vừa cửa sổ)", RES_AUTO)
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
        self.res_combo.setMinimumWidth(195)
        # Combo editable cuộn tới cuối chữ → luôn hiện từ đầu nhãn preset.
        self.res_combo.currentIndexChanged.connect(
            lambda _i: self.res_combo.lineEdit().setCursorPosition(0))
        self.res_combo.setAccessibleName("Độ phân giải stream")
        self.res_combo.setToolTip(
            "Độ phân giải stream (max-width, 160–7680).\n"
            "'Tự động' (mặc định): stream khớp đúng kích thước cửa sổ này,\n"
            "  hiển thị 1:1 pixel — nét nhất, ít băng thông nhất (như AnyDesk).\n"
            "'Theo host': giữ nguyên tham số --max-width của host.\n"
            "Thấp hơn = mượt hơn, ít băng thông hơn. Có thể gõ số tùy ý.")
        self.res_combo.currentIndexChanged.connect(self._on_resolution_changed)
        self.res_combo.lineEdit().editingFinished.connect(
            self._on_resolution_edited)

        self.monitor_combo = QComboBox()
        self.monitor_combo.addItem("Theo host", None)
        self.monitor_combo.setEnabled(False)
        self.monitor_combo.setMinimumWidth(130)
        self.monitor_combo.setAccessibleName("Màn hình host")
        self.monitor_combo.setToolTip(
            "Chọn màn hình host để xem/điều khiển.\n"
            "Host gửi danh sách khi kết nối; 'Theo host' = màn hình chính.")
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)

        # ---- Toolbar: [logo] kết nối | hiển thị ............ [cập nhật]
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(14, 10, 14, 10)
        bar.setSpacing(8)
        logo = QLabel()
        logo.setPixmap(ui.app_icon().pixmap(28, 28))
        logo.setToolTip("Remote Desktop Client")
        bar.addWidget(logo)
        bar.addSpacing(6)

        def field(text: str, widget: QWidget) -> None:
            lbl = ui.label(text, "field")
            lbl.setBuddy(widget)
            bar.addWidget(lbl)
            bar.addWidget(widget)

        field("Host", self.host_edit)
        field("Port", self.port_edit)
        field("Token", self.token_edit)
        bar.addWidget(self.show_token_btn)
        bar.addWidget(self.connect_btn)
        bar.addSpacing(4)
        bar.addWidget(ui.vline())
        bar.addSpacing(4)
        # Hai combo này tự mô tả bằng giá trị → nhãn icon (kèm tooltip) cho gọn.
        for icon_name, tip, combo in (
                ("monitor", "Màn hình host", self.monitor_combo),
                ("settings", "Độ phân giải stream", self.res_combo)):
            icon_lbl = QLabel()
            icon_lbl.setPixmap(ui.icon_pixmap(icon_name, ui.COLORS["muted"], 16))
            icon_lbl.setToolTip(tip)
            icon_lbl.setBuddy(combo)
            bar.addWidget(icon_lbl)
            bar.addWidget(combo)
        bar.addStretch(1)

        # Gửi file / gửi phím / toàn màn hình
        self.file_btn = self._tool_button(
            "upload", "Gửi file sang máy host (hoặc kéo-thả file vào vùng xem)")
        self.file_btn.clicked.connect(self._pick_files)
        self.keys_menu = self._build_keys_menu()
        self.keys_btn = self._tool_button("keyboard", "Gửi phím / tổ hợp phím")
        self.keys_btn.setMenu(self.keys_menu)
        self.keys_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.fullscreen_btn = self._tool_button(
            "maximize", "Toàn màn hình (Ctrl+Alt+Enter)")
        self.fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        self.mode_menu = self._build_mode_menu()
        self.mode_btn = self._tool_button(
            "activity", "Chế độ hình ảnh (tự chỉnh theo đường truyền)")
        self.mode_btn.setMenu(self.mode_menu)
        self.mode_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        for btn in (self.file_btn, self.keys_btn, self.mode_btn,
                    self.fullscreen_btn):
            bar.addWidget(btn)
        bar.addWidget(ui.vline())
        bar.addWidget(self.update_btn)
        self.toolbar = toolbar

        self.view = RemoteView()
        self.view.mouse_event.connect(self._on_mouse_event)
        self.view.key_event.connect(self._on_key_event)
        self.view.view_resized.connect(self._on_view_resized)
        self.view.files_dropped.connect(self._send_files)
        self.view.fullscreen_requested.connect(self._toggle_fullscreen)
        self.syskeys = SystemKeyCapture(self.view, self._on_key_event)
        # Danh sách monitor host (nhận khi kết nối) — dùng cho chế độ Tự động.
        self._monitors: list[dict] = []
        self._monitor_current = 1

        # ---- Status bar: pill trạng thái ........ kích thước frame | version
        self.status_label = QLabel("Chưa kết nối")
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_pill = ui.StatusPill(state="idle", text_label=self.status_label)
        self.size_icon = QLabel()
        self.size_icon.setPixmap(ui.icon_pixmap("monitor", ui.COLORS["muted"], 14))
        self.size_icon.setVisible(False)
        self.size_label = ui.label("", "muted")
        self.size_label.setFont(ui.mono_font(12))
        self.size_label.setToolTip("Kích thước frame nhận được từ host")

        # Truyền file: tiến trình + kết quả + mở thư mục nhận
        self.transfer_label = ui.label("", "muted")
        self.transfer_bar = QProgressBar()
        self.transfer_bar.setTextVisible(False)
        self.transfer_bar.setFixedWidth(140)
        self.transfer_bar.setRange(0, 1000)
        self.transfer_bar.setVisible(False)
        self.open_folder_btn = self._tool_button(
            "folder", "Mở thư mục chứa file nhận từ host")
        self.open_folder_btn.clicked.connect(self._open_receive_dir)
        self.open_folder_btn.setVisible(False)
        self._transfer_clear = QTimer(self)
        self._transfer_clear.setSingleShot(True)
        self._transfer_clear.timeout.connect(lambda: self.transfer_label.setText(""))
        # Độ trễ · FPS · bitrate
        self.stats_label = ui.label("", "muted")
        self.stats_label.setFont(ui.mono_font(12))
        self.stats_label.setToolTip(
            "Độ trễ khứ hồi (ping qua cùng kết nối) · khung hình/giây · băng thông nhận\n"
            "· chất lượng host đang dùng (Q = JPEG, CRF = H.264; % = kích thước khi\n"
            "  đường truyền chậm, host tự giảm độ phân giải)")

        statusbar = QFrame()
        statusbar.setObjectName("statusbar")
        bottom = QHBoxLayout(statusbar)
        bottom.setContentsMargins(12, 6, 14, 6)
        bottom.setSpacing(8)
        bottom.addWidget(self.status_pill)
        bottom.addSpacing(8)
        bottom.addWidget(self.transfer_bar)
        bottom.addWidget(self.transfer_label)
        bottom.addWidget(self.open_folder_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.stats_label)
        bottom.addSpacing(8)
        bottom.addWidget(self.size_icon)
        bottom.addWidget(self.size_label)
        bottom.addSpacing(8)
        bottom.addWidget(ui.label(f"v{current_version()}", "muted"))
        self.statusbar = statusbar

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addWidget(self.view, stretch=1)
        layout.addWidget(statusbar)
        self.setCentralWidget(central)
        self.resize(1280, 780)
        self._build_float_bar(central)
        self._set_connected_ui(False)
        self._was_maximized = False
        self.file_btn.setEnabled(False)
        self.float_file_btn.setEnabled(False)

        # Ping/thống kê
        self._perms: dict = {}
        self._quality: dict = {}
        self._rtt: float | None = None
        self._frames = 0
        self._last_bytes = 0
        self._ping_timer = QTimer(self)
        self._ping_timer.setInterval(2000)
        self._ping_timer.timeout.connect(self._send_ping)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(1000)
        self._stats_timer.timeout.connect(self._update_stats)

        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.timeout.connect(self._check_clipboard)

        self.settings = QSettings("computer-remote", "remote-client")
        self._want_connected = False
        self._reconnect_attempts = 0
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._reconnect_now)
        # Debounce cho độ phân giải Tự động: gộp loạt resize trong 200ms thành
        # 1 lệnh set_resolution (host không phải tạo lại encoder liên tục).
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(200)
        self._auto_timer.timeout.connect(self._send_resolution)
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
        mode = self.settings.value("resolution_mode", "", str)
        if mode in ("auto", "host", "manual"):
            if mode == "auto":
                self.res_combo.setCurrentIndex(self.res_combo.findData(RES_AUTO))
            elif mode == "host":
                self.res_combo.setCurrentIndex(self.res_combo.findText("Theo host"))
            else:
                try:
                    width = int(self.settings.value("manual_width", 1920))
                except (TypeError, ValueError):
                    width = 1920
                index = self.res_combo.findData(width)
                if index >= 0:
                    self.res_combo.setCurrentIndex(index)
                else:
                    self.res_combo.setEditText(str(max(160, min(7680, width))))
        else:
            # Cài đặt của bản cũ: lưu theo nhãn text.
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
        mode = self.settings.value("quality_mode", "balanced", str)
        for action in self.mode_actions.actions():
            if action.data() == mode:
                action.setChecked(True)
        capture = self.settings.value("capture_system_keys", True, bool)
        self.capture_action.setChecked(capture)
        self.syskeys.set_enabled(capture)

    def _save_settings(self) -> None:
        self.settings.setValue("host", self.host_edit.text().strip())
        self.settings.setValue("port", self.port_edit.text().strip())
        self.settings.setValue("token", self.token_edit.text())
        self.settings.setValue("resolution_mode", self._resolution_mode())
        if self._resolution_mode() == "manual":
            width = self._selected_width()
            if width is not None:
                self.settings.setValue("manual_width", width)
        self.settings.setValue("resolution", self.res_combo.currentText())
        self.settings.setValue("geometry", self.saveGeometry())

    # ---- UI state --------------------------------------------------------

    def _on_show_token(self, checked: bool) -> None:
        self.token_edit.setEchoMode(
            QLineEdit.Normal if checked else QLineEdit.Password)
        self.show_token_btn.setIcon(
            ui.icon("eye-off" if checked else "eye", ui.COLORS["muted"]))
        tip = "Ẩn token" if checked else "Hiện token"
        self.show_token_btn.setToolTip(tip)
        self.show_token_btn.setAccessibleName(tip)

    def _set_status(self, state: str, text: str) -> None:
        self.status_pill.set_state(state, text)

    def _set_connected_ui(self, connected: bool) -> None:
        """Nút Kết nối/Ngắt kết nối: đổi chữ, icon và màu (primary ↔ danger)."""
        if connected:
            self.connect_btn.setText(self.TEXT_DISCONNECT)
            self.connect_btn.setIcon(ui.icon("unplug", "#FFFFFF"))
            ui.set_prop(self.connect_btn, "variant", "danger")
        else:
            self.connect_btn.setText(self.TEXT_CONNECT)
            self.connect_btn.setIcon(ui.icon("plug", "#FFFFFF"))
            ui.set_prop(self.connect_btn, "variant", "primary")

    def _tool_button(self, icon_name: str, tip: str) -> QToolButton:
        btn = QToolButton()
        btn.setIcon(ui.icon(icon_name, ui.COLORS["muted"]))
        btn.setProperty("variant", "ghost")
        btn.setToolTip(tip)
        btn.setAccessibleName(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def _build_keys_menu(self) -> QMenu:
        menu = QMenu(self)
        for label, keys in KEY_COMBOS:
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, k=keys: self._send_combo(k))
        menu.addSeparator()
        self.capture_action = QAction(
            "Chuyển phím hệ thống sang host (Alt+Tab, Win…)", menu)
        self.capture_action.setCheckable(True)
        self.capture_action.setChecked(True)
        self.capture_action.setToolTip(
            "Khi vùng xem đang có focus, các phím tắt của hệ điều hành\n"
            "được gửi sang máy host thay vì máy này.")
        self.capture_action.toggled.connect(self._on_capture_toggled)
        menu.addAction(self.capture_action)
        return menu

    QUALITY_MODES = (
        ("balanced", "Cân bằng (tự chỉnh theo đường truyền)"),
        ("quality", "Ưu tiên chất lượng (giữ độ phân giải)"),
        ("speed", "Ưu tiên tốc độ (độ trễ thấp nhất)"),
    )

    def _build_mode_menu(self) -> QMenu:
        menu = QMenu(self)
        self.mode_actions = QActionGroup(menu)
        self.mode_actions.setExclusive(True)
        for mode, label in self.QUALITY_MODES:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            action.setChecked(mode == "balanced")
            self.mode_actions.addAction(action)
        self.mode_actions.triggered.connect(self._on_mode_changed)
        return menu

    def _quality_mode(self) -> str:
        action = self.mode_actions.checkedAction()
        return action.data() if action is not None else "balanced"

    def _on_mode_changed(self, _action=None) -> None:
        mode = self._quality_mode()
        self.settings.setValue("quality_mode", mode)
        if self.worker is not None:
            self.worker.send_control({"event": "set_quality_mode", "mode": mode})

    def _on_quality_info(self, info: dict) -> None:
        self._quality = dict(info)
        self._update_stats()

    def _quality_text(self) -> str:
        info = self._quality
        if not info:
            return ""
        try:
            label = "CRF" if info.get("codec") == "h264" else "Q"
            text = f"{label}{int(info['value'])}"
            scale = float(info.get("scale", 1.0))
        except (KeyError, TypeError, ValueError):
            return ""
        if scale < 0.999:
            text += f" {scale * 100:.0f}%"
        return text

    def _send_combo(self, keys) -> None:
        if self.worker is None:
            return
        for event in combo_events(keys):
            self.worker.send_control(event)
        self.view.setFocus()

    def _on_capture_toggled(self, checked: bool) -> None:
        self.syskeys.set_enabled(checked)
        self.settings.setValue("capture_system_keys", checked)

    # ---- Toàn màn hình ---------------------------------------------------

    def _build_float_bar(self, parent: QWidget) -> None:
        """Thanh nổi ở mép trên khi toàn màn hình (hiện khi chuột chạm mép)."""
        bar = QFrame(parent)
        bar.setObjectName("floatbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 8)
        row.setSpacing(6)
        self.float_stats = ui.label("", "muted")
        self.float_stats.setFont(ui.mono_font(12))
        row.addWidget(self.float_stats)
        row.addSpacing(6)
        float_file = self._tool_button("upload", "Gửi file")
        float_file.clicked.connect(self._pick_files)
        float_keys = self._tool_button("keyboard", "Gửi phím")
        float_keys.setMenu(self.keys_menu)
        float_keys.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        float_exit = self._tool_button("minimize", "Thoát toàn màn hình (Ctrl+Alt+Enter)")
        float_exit.clicked.connect(self._toggle_fullscreen)
        float_disc = self._tool_button("unplug", "Ngắt kết nối")
        float_disc.setIcon(ui.icon("unplug", "#FCA5A5"))
        float_disc.clicked.connect(lambda: self._disconnect("Đã ngắt bởi người dùng"))
        self.float_file_btn = float_file
        for btn in (float_file, float_keys, float_exit, float_disc):
            row.addWidget(btn)
        bar.hide()
        self.float_bar = bar
        self._float_timer = QTimer(self)
        self._float_timer.setInterval(150)
        self._float_timer.timeout.connect(self._update_float_bar)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.toolbar.show()
            self.statusbar.show()
            self._float_timer.stop()
            self.float_bar.hide()
            self.fullscreen_btn.setIcon(ui.icon("maximize", ui.COLORS["muted"]))
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self._was_maximized = self.isMaximized()
            self.toolbar.hide()
            self.statusbar.hide()
            self.showFullScreen()
            self.fullscreen_btn.setIcon(ui.icon("minimize", ui.COLORS["muted"]))
            self._show_float_bar()
            # Gợi ý cách thoát: giữ thanh nổi 2.5s rồi tự ẩn.
            self._float_hold_until = time.monotonic() + 2.5
            self._float_timer.start()
        self.view.setFocus()

    def _show_float_bar(self) -> None:
        bar = self.float_bar
        bar.adjustSize()
        parent = bar.parentWidget()
        bar.move((parent.width() - bar.width()) // 2, 0)
        bar.show()
        bar.raise_()

    def _update_float_bar(self) -> None:
        if not self.isFullScreen():
            return
        pos = self.mapFromGlobal(QCursor.pos())
        bar = self.float_bar
        if pos.y() <= 3 and 0 <= pos.x() < self.width():
            if not bar.isVisible():
                self._show_float_bar()
        elif bar.isVisible():
            menu_open = self.keys_menu.isVisible()
            near = pos.y() <= bar.geometry().bottom() + 24
            if (not near and not menu_open
                    and time.monotonic() > getattr(self, "_float_hold_until", 0)):
                bar.hide()

    # ---- Truyền file -----------------------------------------------------

    def _files_allowed(self) -> bool:
        return self.worker is not None and bool(self._perms.get("files"))

    def _pick_files(self) -> None:
        if not self._files_allowed():
            return
        start = self.settings.value("last_file_dir", os.path.expanduser("~"), str)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Chọn file gửi sang máy host", start)
        if paths:
            self.settings.setValue("last_file_dir", os.path.dirname(paths[0]))
            self._send_files(paths)

    def _send_files(self, paths: list) -> None:
        if not self._files_allowed():
            return
        for path in paths:
            log.info("Gửi file sang host: %s", path)
            self.worker.send_file(path)
        self.transfer_label.setText(
            f"Chuẩn bị gửi {len(paths)} file..." if len(paths) > 1 else "")

    def _on_file_progress(self, outgoing: bool, name: str, done: int,
                          total: int) -> None:
        self._transfer_clear.stop()
        self.transfer_bar.setVisible(True)
        self.transfer_bar.setValue(int(done * 1000 / total) if total else 1000)
        verb = "Đang gửi" if outgoing else "Đang nhận"
        percent = int(done * 100 / total) if total else 100
        self.transfer_label.setText(f"{verb} {name} · {percent}%")

    def _on_file_done(self, outgoing: bool, name: str, ok: bool,
                      detail: str) -> None:
        self.transfer_bar.setVisible(False)
        if ok and outgoing:
            text = f"Đã gửi {name}"
        elif ok:
            text = f"Đã nhận {name}"
            self.open_folder_btn.setVisible(True)
            self.open_folder_btn.setToolTip(f"Mở thư mục: {os.path.dirname(detail)}")
        else:
            verb = "Gửi" if outgoing else "Nhận"
            text = f"{verb} {name} thất bại: {detail}"
        log.info("File: %s", text)
        self.transfer_label.setText(text)
        self._transfer_clear.start(8000)

    def _open_receive_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(filetransfer.receive_dir())))

    # ---- Ping / thống kê -------------------------------------------------

    def _send_ping(self) -> None:
        if self.worker is not None:
            self.worker.send_control(
                {"event": "ping", "t": round(time.monotonic() * 1000.0, 1)})

    def _on_pong(self, rtt: float) -> None:
        self._rtt = rtt

    def _update_stats(self) -> None:
        if self.worker is None:
            return
        received = self.worker.bytes_received
        mbps = (received - self._last_bytes) * 8 / 1e6
        self._last_bytes = received
        fps, self._frames = self._frames, 0
        rtt = "—" if self._rtt is None else f"{self._rtt:.0f}"
        text = f"{rtt} ms · {fps} fps · {mbps:.1f} Mbps"
        quality = self._quality_text()
        if quality:
            text += f" · {quality}"
        self.stats_label.setText(text)
        self.float_stats.setText(text)
        color = ui.COLORS["muted"]
        if self._rtt is not None and self._rtt >= 150:
            color = ui.COLORS["danger"]
        elif self._rtt is not None and self._rtt >= 60:
            color = ui.COLORS["warning"]
        self.stats_label.setStyleSheet(f"color: {color};")

    def _on_permissions(self, info: dict) -> None:
        self._perms = dict(info)
        allowed = bool(info.get("files"))
        for btn in (self.file_btn, self.float_file_btn):
            btn.setEnabled(allowed)
        self.view.accept_files = allowed
        if not info.get("control", True):
            self._set_status("online", "Đã kết nối — chỉ xem (host không cho điều khiển)")

    def _reset_session_ui(self) -> None:
        self._perms = {}
        self._quality = {}
        self._rtt = None
        self._frames = 0
        self._last_bytes = 0
        for btn in (self.file_btn, self.float_file_btn):
            btn.setEnabled(False)
        self.view.accept_files = False
        self.transfer_bar.setVisible(False)
        self.stats_label.setText("")
        self.float_stats.setText("")

    def _connect_from_form(self) -> None:
        if not self._want_connected and self.worker is None:
            self._connect()

    def _toggle_connection(self) -> None:
        if self._want_connected or self.worker is not None:
            self._disconnect("Đã ngắt bởi người dùng")
        else:
            self._connect()

    def _check_update(self) -> None:
        self.updater.start()

    def _selected_width(self) -> int | None:
        """Width đang chọn: None = 'Theo host'/không phải số, số = max-width.

        Chế độ Tự động không có width tĩnh — kiểm tra ``_resolution_mode()``
        trước khi gọi hàm này.
        """
        if self.res_combo.currentData() == RES_AUTO:
            return None
        text = self.res_combo.currentText().strip()
        if not text or text.lower().startswith("theo"):
            return None
        first = text.split()[0]
        if not first.isdigit():
            return None
        return max(160, min(7680, int(first)))

    def _resolution_mode(self) -> str:
        """auto | host | manual — theo lựa chọn hiện tại của combo."""
        if self.res_combo.currentData() == RES_AUTO:
            return "auto"
        if self._selected_width() is None:
            return "host"
        return "manual"

    def _selected_monitor_size(self) -> tuple[int, int] | None:
        """(width, height) của monitor đang chọn; None = chưa nhận được."""
        if not self._monitors:
            return None
        index = self.monitor_combo.currentData() or self._monitor_current
        for mon in self._monitors:
            if mon.get("index") == index:
                width, height = int(mon.get("width") or 0), int(mon.get("height") or 0)
                if width > 0 and height > 0:
                    return width, height
        return None

    def _auto_width(self) -> int | None:
        """Tính max-width cho chế độ Tự động (contain-fit, không upscale).

        - ``scale = min(view/monitor, view_h/monitor_h, 1.0)`` → width yêu cầu
          không bao giờ vượt monitor (host không upscale; client tự fill view).
        - Theo **physical pixel** (view × devicePixelRatio) để hiển thị 1:1.
        - Quantize bucket 8px: DPR fractional (1.25/1.5) không gây đổi width
          liên tục mỗi lần resize 1px (tránh host tạo lại encoder).
        """
        dpr = self.view.devicePixelRatioF() or 1.0
        view_w = self.view.width() * dpr
        view_h = self.view.height() * dpr
        if view_w <= 0 or view_h <= 0:
            return None
        mon = self._selected_monitor_size()
        if mon is not None:
            mon_w, mon_h = mon
            scale = min(view_w / mon_w, view_h / mon_h, 1.0)
            width = mon_w * scale
        else:
            width = min(view_w, view_h * 16 / 9)  # chưa có monitor → giả định 16:9
        # +1e-6 chống lỗi làm tròn fp (800/1920*1920 có thể ra 799.9999...)
        width = int(width + 1e-6) // 8 * 8
        return max(160, min(7680, int(width)))

    def _send_resolution(self) -> None:
        if self.worker is None:
            return
        mode = self._resolution_mode()
        if mode == "auto":
            width = self._auto_width()
            if width is None:
                return
        elif mode == "manual":
            width = self._selected_width()
            if width is None:
                return
        else:
            width = None  # 'Theo host' → host quay về --max-width lúc khởi động
        self.worker.send_control({"event": "set_resolution", "max_width": width})

    def _on_resolution_changed(self, _index: int = -1) -> None:
        self._auto_timer.stop()
        self._send_resolution()

    def _on_resolution_edited(self) -> None:
        # Chỉ chuẩn hoá khi user gõ tay (index = -1), giữ nguyên nhãn preset.
        if self.res_combo.currentIndex() < 0:
            width = self._selected_width()
            if width is not None and self.res_combo.currentText() != str(width):
                self.res_combo.setEditText(str(width))
        self._send_resolution()

    def _on_view_resized(self) -> None:
        """Cửa sổ đổi size → gộp loạt resize rồi gửi 1 lần (debounce 200ms)."""
        if self.worker is None or self._resolution_mode() != "auto":
            return
        self._auto_timer.start()

    def _on_monitors_ready(self, monitors: list, current: int) -> None:
        self._monitors = list(monitors)
        self._monitor_current = int(current or 1)
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
        # Mới biết tỉ lệ monitor → tính lại width auto (nếu đang bật).
        if self.worker is not None and self._resolution_mode() == "auto":
            self._send_resolution()

    def _on_monitor_changed(self, index: int) -> None:
        if self.worker is None:
            return
        data = self.monitor_combo.itemData(index)
        self.worker.send_control({"event": "set_monitor",
                                  "index": int(data or 1)})
        if self._resolution_mode() == "auto":
            # Monitor khác tỉ lệ → tính lại width auto (debounced).
            self._auto_timer.start()

    def _reset_monitor_combo(self) -> None:
        self._monitors = []
        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        self.monitor_combo.addItem("Theo host", None)
        self.monitor_combo.setEnabled(False)
        self.monitor_combo.blockSignals(False)

    def _on_frame_ready(self, img) -> None:
        self._frames += 1
        size = f"{img.width()}x{img.height()}"
        if self.size_label.text() != size:
            self.size_label.setText(size)
            self.size_icon.setVisible(True)

    def _connect(self) -> None:
        host = self.host_edit.text().strip()
        token = self.token_edit.text()
        try:
            port = int(self.port_edit.text().strip())
        except ValueError:
            self._set_status("error", "Port không hợp lệ")
            self.port_edit.setFocus()
            return
        if not host or not token:
            self._set_status("error", "Cần nhập Host và Token")
            (self.host_edit if not host else self.token_edit).setFocus()
            return

        self._reconnect_timer.stop()
        self._want_connected = True
        self._save_settings()

        log.info("Đang kết nối tới %s:%d ...", host, port)
        self.worker = NetworkWorker(host, port, token)
        self.worker.frame_ready.connect(self.view.set_frame)
        self.worker.frame_ready.connect(self._on_frame_ready)
        self.worker.status.connect(self._on_worker_status)
        self.worker.connected.connect(self._on_connected)
        self.worker.rejected.connect(self._on_rejected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.clipboard_from_host.connect(self._on_clipboard_from_host)
        self.worker.monitors_ready.connect(self._on_monitors_ready)
        self.worker.permissions_ready.connect(self._on_permissions)
        self.worker.cursor_shape.connect(self.view.set_remote_cursor)
        self.worker.cursor_pos.connect(self.view.set_remote_pos)
        self.worker.pong.connect(self._on_pong)
        self.worker.quality_info.connect(self._on_quality_info)
        self.worker.file_progress.connect(self._on_file_progress)
        self.worker.file_done.connect(self._on_file_done)
        self._last_bytes = 0
        self.worker.start()
        self._ping_timer.start()
        self._stats_timer.start()
        self.syskeys.set_active(True)
        if self._quality_mode() != "balanced":
            self.worker.send_control(
                {"event": "set_quality_mode", "mode": self._quality_mode()})
        mode = self._resolution_mode()
        if mode == "manual":
            width = self._selected_width()
            if width is not None:
                self.worker.send_control(
                    {"event": "set_resolution", "max_width": width})
        elif mode == "auto":
            # Gửi ngay theo giả định 16:9 (phòng host cũ không gửi packet
            # monitors); sẽ gửi lại đúng tỉ lệ ngay khi nhận monitors_ready.
            self._send_resolution()

        self._set_connected_ui(True)
        self._set_form_enabled(False)
        if self._reconnect_attempts == 0:
            self._set_status("busy", "Đang kết nối...")
        if self.view._frame is None:
            self.view.clear_frame("Đang kết nối...", f"{host}:{port}")
        self.view.setFocus()  # để phím đi vào RemoteView ngay từ đầu

        self._last_clipboard = ""
        self._clipboard_skip = 0
        self._clipboard_timer.start(500)

    def _on_worker_status(self, msg: str) -> None:
        self._set_status("online" if msg.startswith("Đã kết nối") else "busy", msg)

    def _on_connected(self) -> None:
        self._reconnect_attempts = 0
        if self.view._frame is None:
            self.view.clear_frame("Đã kết nối", "Đang chờ khung hình đầu tiên từ host...")

    def _on_rejected(self, reason: str) -> None:
        # Host từ chối (sai token / bị ngắt) — dừng hẳn, không thử lại.
        self._want_connected = False
        self._disconnect(reason)

    def _on_disconnected(self, reason: str) -> None:
        self._teardown_worker()
        if self._want_connected:
            self._reconnect_attempts += 1
            delay = min(10, 2 ** min(self._reconnect_attempts - 1, 4))
            self._set_status(
                "busy",
                f"Mất kết nối — thử lại sau {delay}s "
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
        self._auto_timer.stop()
        self._ping_timer.stop()
        self._stats_timer.stop()
        self.syskeys.set_active(False)
        self._reset_session_ui()
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
        self._set_connected_ui(False)
        self._set_form_enabled(True)
        self._reset_monitor_combo()
        if self.isFullScreen():
            self._toggle_fullscreen()
        self.view.clear_frame("Đã ngắt kết nối", reason)
        self.size_label.setText("")
        self.size_icon.setVisible(False)
        failed = reason.startswith(("Host từ chối", "Lỗi", "Không kết nối"))
        self._set_status("error" if failed else "idle",
                         f"Đã ngắt kết nối ({reason})")

    def _set_form_enabled(self, enabled: bool) -> None:
        for w in (self.host_edit, self.port_edit, self.token_edit,
                  self.show_token_btn):
            w.setEnabled(enabled)

    def _on_mouse_event(self, event: dict) -> None:
        if self.worker is not None:
            self.worker.send_control(event)

    def _on_key_event(self, event: dict) -> None:
        log.debug("Key → host: %s %s", event.get("event"), event.get("key"))
        if self.worker is not None:
            self.worker.send_control(event)

    def closeEvent(self, event) -> None:
        self.syskeys.shutdown()
        self._want_connected = False
        self._reconnect_timer.stop()
        self._auto_timer.stop()
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
    ui.apply_theme(app)
    app.setWindowIcon(ui.app_icon())
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
