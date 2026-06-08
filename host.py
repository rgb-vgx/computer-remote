#!/usr/bin/env python3
"""host.py — Remote desktop HOST (Kubuntu / X11).

Supports JPEG and H.264 codec. H.264 requires ffmpeg with libx264.

Chạy:
  python host.py --codec h264 --token "1"
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import queue
import select
import socket
import struct
import subprocess as sp
import sys
import threading
import time

import cv2
import mss
import numpy as np

from common import protocol

log = logging.getLogger("host")


# ---------------------------------------------------------------------------
# H.264 encoder (ffmpeg subprocess)
# ---------------------------------------------------------------------------

class H264Encoder:
    """Encode raw BGR frames to H.264 via ffmpeg subprocess."""

    def __init__(self, width: int, height: int, fps: int,
                 quality: int = 85) -> None:
        # quality 1-100 → CRF 45–10 (lower CRF = better quality)
        crf = int(max(10, 51 - quality * 0.41))
        self.width = width
        self.height = height

        self._proc = sp.Popen(
            ['ffmpeg',
             '-f', 'rawvideo',
             '-pix_fmt', 'bgr24',
             '-s', f'{width}x{height}',
             '-r', str(max(1, fps)),
             '-i', '-',
             '-c:v', 'libx264',
             '-preset', 'ultrafast',
             '-tune', 'zerolatency',
             '-crf', str(crf),
             '-pix_fmt', 'yuv420p',
             '-g', '30',
             '-f', 'h264',
             '-'],
            stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL,
            bufsize=1024**2)

        # Non-blocking stdout read.
        fd = self._proc.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    def encode(self, frame: np.ndarray) -> bytes:
        """Encode one BGR frame, return H.264 Annex B data."""
        self._proc.stdin.write(frame.tobytes())
        self._proc.stdin.flush()

        chunks = []
        while True:
            try:
                chunk = os.read(self._proc.stdout.fileno(), 131072)
                if not chunk:
                    break
                chunks.append(chunk)
            except BlockingIOError:
                break
        return b''.join(chunks)

    def close(self) -> None:
        try:
            self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            self._proc.kill()

    @staticmethod
    def available() -> bool:
        try:
            sp.run(['ffmpeg', '-version'], capture_output=True, timeout=2)
            return True
        except Exception:
            return False

    @staticmethod
    def libx264_available() -> bool:
        try:
            r = sp.run(['ffmpeg', '-encoders'], capture_output=True, text=True, timeout=2)
            return 'libx264' in r.stdout
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Clipboard helpers
# ---------------------------------------------------------------------------

def _clipboard_set_xclip(text: str) -> None:
    try:
        p = sp.run(
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
        p = sp.run(
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
# Key mapping
# ---------------------------------------------------------------------------

QT_KEY_TO_NAME = {
    0x01000020: "shift", 0x01000021: "ctrl", 0x01000023: "alt",
    0x01000024: "meta", 0x01001103: "alt_gr", 0x01000022: "caps_lock",
    0x01000004: "enter", 0x01000005: "return", 0x01000001: "tab",
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
        log.warning("Bản MVP này cần X11 để capture & inject.")
        log.warning("=" * 64)
    else:
        log.info("Session X11 OK.")


# ---------------------------------------------------------------------------
# Host server
# ---------------------------------------------------------------------------

class HostServer:
    """TCP server stream màn hình + nhận control."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop_event = threading.Event()
        self.server_sock: socket.socket | None = None

        self._mouse = None
        self._keyboard = None
        self._view_only_logged = False

        # Clipboard
        self._clipboard_send_queue: queue.Queue[str] = queue.Queue()
        self._last_clipboard = ""
        self._clipboard_skip = 0
        self.clipboard_get = _clipboard_get_xclip
        self.clipboard_set = _clipboard_set_xclip
        self.on_clipboard_received = None

    def send_clipboard(self, text: str) -> None:
        self._clipboard_send_queue.put_nowait(text)

    def serve_forever(self) -> None:
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.args.bind, self.args.port))
        self.server_sock.listen(1)
        self.server_sock.settimeout(1.0)
        log.info("Đang lắng nghe trên %s:%d", self.args.bind, self.args.port)

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
                log.error("Lỗi xử lý client %s: %s", addr, exc)
            finally:
                _safe_close(client_sock)
                log.info("Client %s:%d đã ngắt", *addr)

    def shutdown(self) -> None:
        self.stop_event.set()
        _safe_close(self.server_sock)

    def _handle_client(self, sock: socket.socket, addr) -> None:
        sock.settimeout(10.0)
        ptype, payload = protocol.recv_packet(sock)
        if ptype != protocol.PKT_HELLO:
            log.warning("Client %s gửi packet không phải hello", addr)
            return
        try:
            hello = protocol.decode_json(payload)
        except Exception:
            log.warning("Hello không phải JSON hợp lệ")
            return

        if hello.get("token") != self.args.token:
            log.warning("AUTH FAIL từ %s", addr)
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

    # ---- Capture / send ------------------------------------------------

    def _capture_loop(self, sock: socket.socket,
                      client_stop: threading.Event) -> None:
        frame_budget = 1.0 / max(1, self.args.fps)
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.args.quality]
        frames_since_log = 0
        last_log = time.time()

        # Decide codec
        use_h264 = (self.args.codec == 'h264' and H264Encoder.available()
                    and H264Encoder.libx264_available())
        h264_enc: H264Encoder | None = None

        if use_h264:
            log.info("Sử dụng codec H.264 (ffmpeg libx264)")
        else:
            log.info("Sử dụng codec JPEG")

        try:
            with mss.MSS() as sct:
                monitor = sct.monitors[1]

                # First capture to get dimensions
                raw = sct.grab(monitor)
                img = np.asarray(raw)[:, :, :3]
                cap_h, cap_w = img.shape[:2]
                if cap_w > self.args.max_width:
                    scale = self.args.max_width / cap_w
                    frame_w, frame_h = self.args.max_width, int(cap_h * scale)
                else:
                    frame_w, frame_h = cap_w, cap_h

                if use_h264:
                    h264_enc = H264Encoder(frame_w, frame_h, self.args.fps,
                                           self.args.quality)
                    protocol.send_json(
                        sock, protocol.PKT_INFO,
                        {"type": "codec", "codec": "h264",
                         "width": frame_w, "height": frame_h})

                while not client_stop.is_set() and not self.stop_event.is_set():
                    t0 = time.time()
                    raw = sct.grab(monitor)
                    img = np.asarray(raw)[:, :, :3]
                    if img.shape[1] > self.args.max_width:
                        scale = self.args.max_width / img.shape[1]
                        img = cv2.resize(
                            img, (self.args.max_width,
                                  int(img.shape[0] * scale)),
                            interpolation=cv2.INTER_AREA)

                    if use_h264 and h264_enc:
                        data = h264_enc.encode(img)
                        if data:
                            protocol.send_packet(
                                sock, protocol.PKT_FRAME_H264, data)
                    else:
                        ok, buf = cv2.imencode(".jpg", img, encode_params)
                        if not ok:
                            log.error("cv2.imencode thất bại")
                            continue
                        protocol.send_packet(
                            sock, protocol.PKT_FRAME, buf.tobytes())

                    frames_since_log += 1
                    now = time.time()
                    if now - last_log >= 5.0:
                        if use_h264:
                            log.info("Stream ~%.1f FPS (H.264 %dx%d)",
                                     frames_since_log / (now - last_log),
                                     frame_w, frame_h)
                        else:
                            log.info("Stream ~%.1f FPS (JPEG %dx%d, q=%d)",
                                     frames_since_log / (now - last_log),
                                     img.shape[1], img.shape[0],
                                     self.args.quality)
                        frames_since_log = 0
                        last_log = now

                    elapsed = time.time() - t0
                    if elapsed < frame_budget:
                        time.sleep(frame_budget - elapsed)
        except (ConnectionError, OSError, struct.error) as exc:
            log.info("Capture loop dừng: %s", exc)
        except Exception as exc:
            log.error("Capture loop lỗi: %s", exc)
        finally:
            if h264_enc:
                h264_enc.close()
            client_stop.set()

    # ---- Control receive -----------------------------------------------

    def _control_loop(self, sock: socket.socket,
                      client_stop: threading.Event) -> None:
        sock.setblocking(True)
        clipboard_poll_interval = 0
        try:
            while not client_stop.is_set() and not self.stop_event.is_set():
                self._flush_clipboard_send(sock)
                clipboard_poll_interval += 1
                if clipboard_poll_interval >= 4:
                    clipboard_poll_interval = 0
                    self._poll_host_clipboard(sock)

                ready, _, _ = select.select([sock], [], [], 0.5)
                if not ready:
                    continue

                ptype, payload = protocol.recv_packet(sock)
                if ptype != protocol.PKT_CONTROL:
                    continue
                if self.args.view_only:
                    if not self._view_only_logged:
                        log.info("--view-only: bỏ qua control.")
                        self._view_only_logged = True
                    continue
                try:
                    event = protocol.decode_json(payload)
                except Exception:
                    continue
                self._apply_event(event)
        except (ConnectionError, OSError, struct.error) as exc:
            log.info("Control loop dừng: %s", exc)
        except Exception as exc:
            log.error("Control loop lỗi: %s", exc)
        finally:
            client_stop.set()

    def _flush_clipboard_send(self, sock: socket.socket) -> None:
        while True:
            try:
                text = self._clipboard_send_queue.get_nowait()
            except queue.Empty:
                return
            try:
                protocol.send_json(
                    sock, protocol.PKT_CONTROL,
                    {"event": "clipboard", "text": text, "source": "host"})
            except OSError:
                return

    def _poll_host_clipboard(self, sock: socket.socket) -> None:
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
            except OSError:
                pass

    # ---- Apply events -------------------------------------------------

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
        from pynput.keyboard import Key
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
            key = key_str
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

    # ---- Lazy init ----------------------------------------------------

    def _get_mouse(self):
        if self._mouse is None:
            try:
                from pynput.mouse import Controller
                self._mouse = Controller()
            except Exception as exc:
                log.error("Không khởi tạo được mouse: %s", exc)
                self._mouse = False
        return self._mouse or None

    def _get_keyboard(self):
        if self._keyboard is None:
            try:
                from pynput.keyboard import Controller
                self._keyboard = Controller()
            except Exception as exc:
                log.error("Không khởi tạo được keyboard: %s", exc)
                self._keyboard = False
        return self._keyboard or None

    def _screen_size(self) -> tuple[int, int]:
        if not hasattr(self, "_screen_cache"):
            with mss.MSS() as sct:
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
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--token", default="1")
    p.add_argument("--fps", type=int, default=8)
    p.add_argument("--quality", type=int, default=85,
                   help="Chất lượng nén (JPEG: 1-100, H.264: 1-100 → CRF 51-10)")
    p.add_argument("--max-width", type=int, default=1920)
    p.add_argument("--codec", choices=["jpeg", "h264"], default="h264",
                   help="Codec: jpeg hoặc h264 (mặc định h264, fallback jpeg nếu thiếu ffmpeg)")
    p.add_argument("--view-only", action="store_true")
    p.add_argument("--debug", action="store_true")
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

    log.info("=== Remote desktop HOST ===")
    log.info("Token: %s | Codec: %s", args.token, args.codec)
    check_display_env()
    if args.view_only:
        log.info("--view-only: bỏ qua control.")

    server = HostServer(args)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Ctrl+C — đang tắt...")
    finally:
        server.shutdown()
        log.info("Host đã tắt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
