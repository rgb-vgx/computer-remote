#!/usr/bin/env python3
"""host.py — Remote desktop HOST (chạy trên Kubuntu / X11).

Chức năng:
  * Chụp màn hình bằng mss, encode JPEG bằng OpenCV, stream qua TCP tới 1 client.
  * Nhận control event (mouse, keyboard, clipboard) từ client và inject.
  * Auth bằng token.

Chạy:
  python host.py --bind 0.0.0.0 --port 7777 --token "1" --fps 8 --quality 60
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import select
import socket
import struct
import subprocess
import sys
import threading
import time

import cv2
import mss
import numpy as np

from common import protocol

log = logging.getLogger("host")


# ---------------------------------------------------------------------------
# Clipboard helpers (dùng xclip cho non-GUI mode)
# ---------------------------------------------------------------------------

def _clipboard_set_xclip(text: str) -> None:
    try:
        p = subprocess.run(
            ["xclip", "-i", "-selection", "clipboard"],
            input=text.encode("utf-8"), capture_output=True, timeout=2)
        if p.returncode != 0:
            log.warning("xclip set clipboard failed")
    except FileNotFoundError:
        log.debug("xclip not installed, clipboard set disabled")
    except Exception as exc:
        log.debug("xclip set error: %s", exc)


def _clipboard_get_xclip() -> str:
    try:
        p = subprocess.run(
            ["xclip", "-o", "-selection", "clipboard"],
            capture_output=True, timeout=2)
        if p.returncode == 0:
            return p.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        log.debug("xclip not installed, clipboard get disabled")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Key mapping (Qt → pynput)
# ---------------------------------------------------------------------------

QT_KEY_TO_NAME = {
    # Modifiers
    0x01000020: "shift",      # Qt.Key.Key_Shift
    0x01000021: "ctrl",       # Qt.Key.Key_Control
    0x01000023: "alt",        # Qt.Key.Key_Alt
    0x01000024: "meta",       # Qt.Key.Key_Meta
    0x01001103: "alt_gr",     # Qt.Key.Key_AltGr
    # Lock
    0x01000022: "caps_lock",  # Qt.Key.Key_CapsLock
    # Navigation
    0x01000004: "enter",      # Qt.Key.Key_Enter
    0x01000005: "return",     # Qt.Key.Key_Return (same as enter)
    0x01000001: "tab",        # Qt.Key.Key_Tab
    0x01000003: "backspace",  # Qt.Key.Key_Backspace
    0x01000000: "esc",        # Qt.Key.Key_Escape
    0x01000006: "delete",     # Qt.Key.Key_Delete
    0x01000010: "home",       # Qt.Key.Key_Home
    0x01000011: "end",        # Qt.Key.Key_End
    0x01000012: "page_up",    # Qt.Key.Key_PageUp
    0x01000013: "page_down",  # Qt.Key.Key_PageDown
    0x01000007: "insert",     # Qt.Key.Key_Insert
    0x01000008: "menu",       # Qt.Key.Key_Menu
    0x01000009: "pause",      # Qt.Key.Key_Pause
    0x0100000a: "print_screen",
    # Arrows
    0x01000012: "left",       # Qt.Key.Key_Left
    0x01000013: "up",         # Qt.Key.Key_Up
    0x01000014: "right",      # Qt.Key.Key_Right
    0x01000015: "down",       # Qt.Key.Key_Down
    # F keys
    0x01000030: "f1",  0x01000031: "f2",  0x01000032: "f3",
    0x01000033: "f4",  0x01000034: "f5",  0x01000035: "f6",
    0x01000036: "f7",  0x01000037: "f8",  0x01000038: "f9",
    0x01000039: "f10", 0x0100003a: "f11", 0x0100003b: "f12",
}

# Reverse: fix duplicate entries — "left", "up", "right", "down" vs PageUp/PageDown
# Key_Left=0x01000012 conflicts with Key_PageUp=0x01000012? No, let me check actual Qt values.
# Actually Qt: Key_Left=0x01000012, Key_Up=0x01000013, Key_Right=0x01000014, Key_Down=0x01000015
# Key_PageUp=0x01000016, Key_PageDown=0x01000017
# Let me fix duplicates:
_QT_KEY_MAP_FIX = {
    0x01000012: "left",
    0x01000013: "up",
    0x01000014: "right",
    0x01000015: "down",
    0x01000016: "page_up",
    0x01000017: "page_down",
}
QT_KEY_TO_NAME.update(_QT_KEY_MAP_FIX)


def _qt_key_to_name(qt_key: int) -> str | None:
    return QT_KEY_TO_NAME.get(qt_key)


# ---------------------------------------------------------------------------
# Environment check
# ---------------------------------------------------------------------------

def check_display_env() -> None:
    session = os.environ.get("XDG_SESSION_TYPE", "")
    display = os.environ.get("DISPLAY", "")
    log.info("XDG_SESSION_TYPE=%r", session)
    log.info("DISPLAY=%r", display)

    if session.lower() != "x11" or not display:
        log.warning("=" * 64)
        log.warning("CẢNH BÁO: Không phát hiện session X11 hợp lệ.")
        log.warning("Bản MVP này cần chạy TRONG một desktop GUI session (X11).")
        log.warning("KHÔNG chạy qua SSH/TTY — sẽ không capture/input được.")
        log.warning("=" * 64)
    else:
        log.info("Session X11 OK — sẵn sàng capture & inject input.")


# ---------------------------------------------------------------------------
# Host server
# ---------------------------------------------------------------------------

class HostServer:
    """TCP server stream màn hình + nhận control. Phục vụ 1 client/lần."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop_event = threading.Event()
        self.server_sock: socket.socket | None = None

        # Mouse + keyboard (lazy init)
        self._mouse = None
        self._keyboard = None
        self._view_only_logged = False

        # Clipboard: queue for data to send, polling state
        self._clipboard_send_queue: queue.Queue[str] = queue.Queue()
        self._last_clipboard = ""
        self._clipboard_skip = 0

        # Clipboard backend — có thể override từ host_gui.py để dùng QClipboard.
        self.clipboard_get = _clipboard_get_xclip
        self.clipboard_set = _clipboard_set_xclip
        self.on_clipboard_received: None | (lambda str: None) = None

    # ---- Public API ------------------------------------------------------

    def send_clipboard(self, text: str) -> None:
        """Queue clipboard text to send to client (thread-safe)."""
        self._clipboard_send_queue.put_nowait(text)

    # ---- Server lifecycle ------------------------------------------------

    def serve_forever(self) -> None:
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.args.bind, self.args.port))
        self.server_sock.listen(1)
        self.server_sock.settimeout(1.0)
        log.info("Đang lắng nghe trên %s:%d (chờ 1 client)",
                 self.args.bind, self.args.port)

        while not self.stop_event.is_set():
            try:
                client_sock, addr = self.server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log.info("Client kết nối từ %s:%d", *addr)
            try:
                self._handle_client(client_sock, addr)
            except Exception as exc:
                log.error("Lỗi khi xử lý client %s: %s", addr, exc)
            finally:
                _safe_close(client_sock)
                log.info("Client %s:%d đã ngắt — quay lại chờ client mới", *addr)

    def shutdown(self) -> None:
        self.stop_event.set()
        _safe_close(self.server_sock)

    # ---- Xử lý 1 client -------------------------------------------------

    def _handle_client(self, sock: socket.socket, addr) -> None:
        sock.settimeout(10.0)
        ptype, payload = protocol.recv_packet(sock)
        if ptype != protocol.PKT_HELLO:
            log.warning("Client %s gửi packet không phải hello — đóng", addr)
            return
        try:
            hello = protocol.decode_json(payload)
        except Exception:
            log.warning("Hello không phải JSON hợp lệ — đóng")
            return

        if hello.get("token") != self.args.token:
            log.warning("AUTH FAIL từ %s — token sai.", addr)
            try:
                protocol.send_json(sock, protocol.PKT_INFO,
                                   {"type": "error", "message": "auth failed"})
            except OSError:
                pass
            return

        log.info("AUTH SUCCESS từ %s", addr)
        protocol.send_json(sock, protocol.PKT_INFO,
                           {"type": "info", "message": "auth ok"})

        sock.settimeout(None)
        client_stop = threading.Event()

        send_thread = threading.Thread(
            target=self._capture_loop, args=(sock, client_stop),
            name="capture", daemon=True)
        recv_thread = threading.Thread(
            target=self._control_loop, args=(sock, client_stop),
            name="control", daemon=True)
        send_thread.start()
        recv_thread.start()

        while not client_stop.is_set() and not self.stop_event.is_set():
            time.sleep(0.2)
        client_stop.set()
        _safe_close(sock)
        send_thread.join(timeout=2.0)
        recv_thread.join(timeout=2.0)

    # ---- Capture / send thread -------------------------------------------

    def _capture_loop(self, sock: socket.socket,
                      client_stop: threading.Event) -> None:
        frame_budget = 1.0 / max(1, self.args.fps)
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.args.quality]
        frames_since_log = 0
        last_log = time.time()

        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                while not client_stop.is_set() and not self.stop_event.is_set():
                    t0 = time.time()
                    raw = sct.grab(monitor)
                    img = np.asarray(raw)[:, :, :3]
                    h, w = img.shape[:2]
                    if w > self.args.max_width:
                        scale = self.args.max_width / w
                        img = cv2.resize(
                            img, (self.args.max_width, int(h * scale)),
                            interpolation=cv2.INTER_AREA)
                    ok, buf = cv2.imencode(".jpg", img, encode_params)
                    if not ok:
                        log.error("cv2.imencode thất bại — bỏ frame")
                        continue
                    protocol.send_packet(sock, protocol.PKT_FRAME,
                                         buf.tobytes())

                    frames_since_log += 1
                    now = time.time()
                    if now - last_log >= 5.0:
                        fps = frames_since_log / (now - last_log)
                        log.info("Đang stream ~%.1f FPS (%dx%d, q=%d)",
                                 fps, img.shape[1], img.shape[0],
                                 self.args.quality)
                        frames_since_log = 0
                        last_log = now

                    elapsed = time.time() - t0
                    if elapsed < frame_budget:
                        time.sleep(frame_budget - elapsed)
        except (ConnectionError, OSError, struct.error) as exc:
            log.info("Capture loop dừng (kết nối): %s", exc)
        except Exception as exc:
            log.error("Capture loop lỗi: %s", exc)
        finally:
            client_stop.set()

    # ---- Control receive thread ------------------------------------------

    def _control_loop(self, sock: socket.socket,
                      client_stop: threading.Event) -> None:
        """Nhận control event + flush clipboard queue + poll host clipboard."""
        sock.setblocking(True)
        clipboard_poll_interval = 0  # counter for polling every ~2s
        try:
            while not client_stop.is_set() and not self.stop_event.is_set():
                # 1) Gửi clipboard data từ queue (GUI thread gửi lên).
                self._flush_clipboard_send(sock)

                # 2) Poll clipboard host định kỳ để phát hiện copy tại host.
                clipboard_poll_interval += 1
                if clipboard_poll_interval >= 4:  # ~2s (4 * 0.5s)
                    clipboard_poll_interval = 0
                    self._poll_host_clipboard(sock)

                # 3) Chờ control event từ client.
                ready, _, _ = select.select([sock], [], [], 0.5)
                if not ready:
                    continue

                ptype, payload = protocol.recv_packet(sock)
                if ptype != protocol.PKT_CONTROL:
                    continue
                if self.args.view_only:
                    if not self._view_only_logged:
                        log.info("--view-only: bỏ qua control event.")
                        self._view_only_logged = True
                    continue
                try:
                    event = protocol.decode_json(payload)
                except Exception:
                    continue
                self._apply_event(event)
        except (ConnectionError, OSError, struct.error) as exc:
            log.info("Control loop dừng (kết nối): %s", exc)
        except Exception as exc:
            log.error("Control loop lỗi: %s", exc)
        finally:
            client_stop.set()

    def _flush_clipboard_send(self, sock: socket.socket) -> None:
        """Gửi clipboard data đang chờ trong queue tới client."""
        while True:
            try:
                text = self._clipboard_send_queue.get_nowait()
            except queue.Empty:
                return
            try:
                protocol.send_json(
                    sock, protocol.PKT_CONTROL,
                    {"event": "clipboard", "text": text, "source": "host"})
                log.debug("Đã gửi clipboard tới client (%d bytes)", len(text))
            except OSError:
                return

    def _poll_host_clipboard(self, sock: socket.socket) -> None:
        """Kiểm tra clipboard host có thay đổi không → gửi client."""
        if self._clipboard_skip > 0:
            self._clipboard_skip -= 1
            return
        text = self.clipboard_get()
        if text and text != self._last_clipboard:
            self._last_clipboard = text
            try:
                protocol.send_json(
                    sock, protocol.PKT_CONTROL,
                    {"event": "clipboard", "text": text, "source": "host"})
                log.debug("Host clipboard changed → gửi client (%d bytes)",
                          len(text))
            except OSError:
                pass

    # ---- Apply events ----------------------------------------------------

    def _apply_event(self, event: dict) -> None:
        kind = event.get("event")

        if kind in ("mouse_move", "mouse_down", "mouse_up"):
            self._apply_mouse(event)
        elif kind in ("key_down", "key_up"):
            self._apply_keyboard(event)
        elif kind == "clipboard":
            self._apply_clipboard(event)

    def _apply_mouse(self, event: dict) -> None:
        mouse = self._get_mouse()
        if mouse is None:
            return
        from pynput.mouse import Button

        screen_w, screen_h = self._screen_size()
        kind = event.get("event")

        if kind == "mouse_move":
            x = _clamp01(event.get("x", 0.0)) * screen_w
            y = _clamp01(event.get("y", 0.0)) * screen_h
            mouse.position = (int(x), int(y))
        elif kind in ("mouse_down", "mouse_up"):
            x = _clamp01(event.get("x", 0.0)) * screen_w
            y = _clamp01(event.get("y", 0.0)) * screen_h
            mouse.position = (int(x), int(y))
            btn = Button.right if event.get("button") == "right" else Button.left
            if kind == "mouse_down":
                mouse.press(btn)
            else:
                mouse.release(btn)

    def _apply_keyboard(self, event: dict) -> None:
        kbd = self._get_keyboard()
        if kbd is None:
            return
        from pynput.keyboard import Key, KeyCode

        kind = event.get("event")
        key_str = event.get("key", "")
        if not key_str:
            return

        if key_str.startswith("Key."):
            name = key_str[4:]
            try:
                key = getattr(Key, name)
            except AttributeError:
                log.warning("Unknown special key: %s", name)
                return
        else:
            key = key_str  # pynput accepts str for regular chars

        try:
            if kind == "key_down":
                kbd.press(key)
            else:
                kbd.release(key)
        except Exception as exc:
            log.debug("Keyboard inject error: %s", exc)

    def _apply_clipboard(self, event: dict) -> None:
        text = event.get("text", "")
        if not text:
            return
        self.clipboard_set(text)
        self._last_clipboard = text
        self._clipboard_skip = 4
        if self.on_clipboard_received:
            self.on_clipboard_received(text)
        log.debug("Clipboard từ client → set host (%d bytes)", len(text))

    # ---- Lazy init helpers -----------------------------------------------

    def _get_mouse(self):
        if self._mouse is None:
            try:
                from pynput.mouse import Controller
                self._mouse = Controller()
            except Exception as exc:
                log.error("Không khởi tạo được pynput mouse: %s", exc)
                self._mouse = False
        return self._mouse or None

    def _get_keyboard(self):
        if self._keyboard is None:
            try:
                from pynput.keyboard import Controller
                self._keyboard = Controller()
            except Exception as exc:
                log.error("Không khởi tạo được pynput keyboard: %s", exc)
                self._keyboard = False
        return self._keyboard or None

    def _screen_size(self) -> tuple[int, int]:
        if not hasattr(self, "_screen_cache"):
            with mss.mss() as sct:
                mon = sct.monitors[1]
                self._screen_cache = (mon["width"], mon["height"])
        return self._screen_cache


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _clamp01(v: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _safe_close(sock) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Remote desktop HOST (Kubuntu/X11).")
    p.add_argument("--bind", default="0.0.0.0",
                   help="Địa chỉ bind (mặc định 0.0.0.0).")
    p.add_argument("--port", type=int, default=7777, help="Cổng TCP (mặc định 7777).")
    p.add_argument("--token", default="1",
                   help="Token — client phải gửi đúng mới được stream.")
    p.add_argument("--fps", type=int, default=8, help="Số frame/giây mục tiêu.")
    p.add_argument("--quality", type=int, default=60, help="Chất lượng JPEG (1-100).")
    p.add_argument("--max-width", type=int, default=1280,
                   help="Resize xuống nếu rộng hơn (giữ aspect ratio).")
    p.add_argument("--view-only", action="store_true",
                   help="Nếu bật: chỉ stream, KHÔNG nhận/inject control.")
    p.add_argument("--debug", action="store_true",
                   help="Bật log debug chi tiết.")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(logs_dir, "host.log"), encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(fh)

    log.info("Host log file: %s", os.path.join(logs_dir, "host.log"))
    if args.debug:
        log.info("DEBUG mode ON")

    log.info("=== Remote desktop HOST (MVP, foreground) ===")
    log.info("Token: %s", args.token)
    check_display_env()
    if args.view_only:
        log.info("Chế độ --view-only: input control sẽ bị bỏ qua.")

    server = HostServer(args)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Nhận Ctrl+C — đang tắt...")
    finally:
        server.shutdown()
        log.info("Host đã tắt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
