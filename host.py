#!/usr/bin/env python3
"""host.py — Remote desktop HOST (Linux/X11 hoặc Windows).

Supports JPEG and H.264 codec. H.264 requires ffmpeg with libx264.

Chạy:
  python host.py --codec h264 --token "1"
"""

from __future__ import annotations

import argparse
import hmac
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
from logging.handlers import RotatingFileHandler

import cv2
import mss
import numpy as np

from common import app_log_dir, no_console_kwargs, protocol

log = logging.getLogger("host")

# Chống dò token: khóa IP tạm thời sau N lần sai.
_AUTH_MAX_FAILURES = 5
_AUTH_BLOCK_SECONDS = 30.0

# Debounce tạo lại encoder H.264: client auto-resize có thể gửi dồn dập,
# gộp về tối đa 2 lần/giây (frame dừng tối đa ~500ms trong lúc chờ).
_RECREATE_MIN_INTERVAL = 0.5


def _jpeg_encode_params(quality: int) -> list:
    """Tham số ``cv2.imencode`` JPEG: quality (1-100) + sampling 4:4:4.

    4:4:4 giữ nguyên chroma → chữ/kẻ viền sắc nét (4:2:0 làm mềm chroma).
    Benchmark (30 frame text): q75 4:2:0 = 141 KiB, 39.2 dB;
    q90 4:4:4 = 215 KiB, 46.0 dB.
    OpenCV không có cờ sampling → chỉ gửi quality (4:2:0 mặc định).
    """
    params = [int(cv2.IMWRITE_JPEG_QUALITY), int(max(1, min(100, quality)))]
    factor = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR", None)
    factor_444 = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_444", None)
    if factor is not None and factor_444 is not None:
        params += [int(factor), int(factor_444)]
    return params


# ---------------------------------------------------------------------------
# H.264 encoder (ffmpeg subprocess)
# ---------------------------------------------------------------------------

class H264Encoder:
    """Encode raw BGRA frames to H.264 via ffmpeg subprocess.

    Output được drain bằng 1 reader thread riêng: x264/ffmpeg có thể trả
    output trễ hơn thời điểm ``encode()`` được gọi, đọc ngay sau khi ghi sẽ
    hụt frame (nhất là màn hình tĩnh, P-frame rất nhỏ nằm lâu trong buffer).
    """

    def __init__(self, width: int, height: int, fps: int,
                 crf: int = 18) -> None:
        # CRF 0-51 (thấp = nét hơn): mặc định 18 — nét, dung lượng thấp.
        # Benchmark (30 frame text): ultrafast crf20 = 13.7 KiB/frame,
        # veryfast crf14 = 9.0 KiB/frame (rất mạnh cho text).
        self.width = width
        self.height = height

        self._proc = sp.Popen(
            ['ffmpeg',
             '-f', 'rawvideo',
             '-pix_fmt', 'bgra',
             '-s', f'{width}x{height}',
             '-r', str(max(1, fps)),
             '-i', '-',
             '-c:v', 'libx264',
             # zerolatency đã tắt bframes/lookahead; ultrafast giảm CPU encode
             # (spike trễ) — đổi sang veryfast nếu muốn tiết bandwidth hơn.
             '-preset', 'ultrafast',
             '-tune', 'zerolatency',
             '-crf', str(int(max(0, min(51, crf)))),
             '-pix_fmt', 'yuv420p',
             '-g', '30',
             '-flush_packets', '1',
             '-f', 'h264',
             '-'],
            stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL,
            bufsize=1024**2, **no_console_kwargs())

        self._out = bytearray()
        self._out_lock = threading.Lock()
        self._closed = False

        try:
            os.set_blocking(self._proc.stdout.fileno(), False)
        except (OSError, AttributeError):
            # Windows (một số bản Python): reader thread dùng blocking read,
            # vẫn đúng vì nó chạy ở thread riêng.
            pass
        self._reader = threading.Thread(
            target=self._reader_loop, name="h264-enc-out", daemon=True)
        self._reader.start()

    def _reader_loop(self) -> None:
        fd = self._proc.stdout.fileno()
        while not self._closed and self._proc.poll() is None:
            try:
                chunk = os.read(fd, 262144)
                if not chunk:
                    break
                with self._out_lock:
                    self._out += chunk
            except BlockingIOError:
                time.sleep(0.002)
            except OSError:
                break

    def encode(self, frame: np.ndarray) -> bytes:
        """Encode one BGRA frame, return H.264 Annex B data.

        Ghi thẳng buffer của numpy vào pipe, không copy qua ``tobytes()``.
        """
        self._proc.stdin.write(np.ascontiguousarray(frame).data)
        self._proc.stdin.flush()

        # Chờ ngắn cho access unit hiện tại kịp ra; thường đã có sẵn vì
        # reader thread drain song song trong lúc chờ frame budget.
        deadline = time.time() + 0.1
        while time.time() < deadline:
            with self._out_lock:
                if self._out:
                    break
            time.sleep(0.002)
        return self.take_output()

    def take_output(self) -> bytes:
        with self._out_lock:
            data = bytes(self._out)
            self._out.clear()
        return data

    def close(self) -> None:
        self._closed = True
        try:
            self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            self._proc.kill()
        self._reader.join(timeout=1.0)

    @staticmethod
    def available() -> bool:
        try:
            sp.run(['ffmpeg', '-version'], capture_output=True, timeout=2,
                   **no_console_kwargs())
            return True
        except Exception:
            return False

    @staticmethod
    def libx264_available() -> bool:
        try:
            r = sp.run(['ffmpeg', '-encoders'], capture_output=True, text=True,
                       timeout=2, **no_console_kwargs())
            return 'libx264' in r.stdout
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Keyboard injection (XSendEvent)
# ---------------------------------------------------------------------------

# Tên phím (theo pynput, client gửi "Key.<name>") → X keysym.
_KEY_NAME_TO_KEYSYM = {
    "shift": 0xFFE1, "ctrl": 0xFFE3, "alt": 0xFFE9, "cmd": 0xFFEB,
    "alt_gr": 0xFE03, "caps_lock": 0xFFE5,
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09,
    "backspace": 0xFF08, "esc": 0xFF1B, "escape": 0xFF1B,
    "delete": 0xFFFF, "home": 0xFF50, "end": 0xFF57,
    "page_up": 0xFF55, "page_down": 0xFF56, "insert": 0xFF63,
    "menu": 0xFF67, "pause": 0xFF13, "print_screen": 0xFF61,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "space": 0x0020,
}
for _i in range(1, 13):
    _KEY_NAME_TO_KEYSYM[f"f{_i}"] = 0xFFBE + _i - 1


def key_to_keysym(key: str) -> int | None:
    """Chuyển tên phím / ký tự client gửi thành X11 keysym.

    Ký tự Unicode > 0xFF dùng keysym ``0x01000000 + codepoint`` (chuẩn X11),
    nhờ vậy gõ được cả ký tự không có trong layout host (tiếng Việt có dấu,
    CJK...) bằng cách remap tạm 1 keycode trống.
    """
    name = key[4:] if key.startswith("Key.") else key
    if name in _KEY_NAME_TO_KEYSYM:
        return _KEY_NAME_TO_KEYSYM[name]
    if len(name) == 1:
        cp = ord(name)
        return cp if cp < 0x100 else 0x01000000 + cp
    return None


class X11Keyboard:
    """Inject bàn phím qua XTEST (cùng cách xdotool).

    XTEST hoạt động với mọi app: Qt/KDE, GTK, terminal, và cả
    Electron/Chromium (VS Code) — trong khi XSendEvent bị Chromium bỏ qua.
    Ký tự cần Shift/AltGr được bọc bằng phím modifier tạm thời (nếu
    modifier đó chưa được giữ). Ký tự ngoài layout host được inject bằng
    cách remap tạm một keycode trống sang Unicode keysym.
    """

    def __init__(self) -> None:
        from Xlib import display
        from Xlib.ext import xtest
        self._display = display.Display()
        self._xtest = xtest
        self._lock = threading.Lock()
        # Keycode trống dùng để remap ký tự Unicode ngoài layout.
        self._spare: int | None = None
        self._spare_original: list[int] | None = None
        self._spare_keysym: int | None = None
        self._restore_timer: threading.Timer | None = None

    def press(self, key: str) -> None:
        with self._lock:
            self._inject(key, is_press=True)

    def release(self, key: str) -> None:
        with self._lock:
            self._inject(key, is_press=False)

    def _spare_keycode(self) -> int | None:
        if self._spare is None:
            for keycode in range(8, 256):
                mapping = self._display.get_keyboard_mapping(keycode, 1)
                if mapping and not any(mapping[0]):
                    self._spare = keycode
                    break
        return self._spare

    def _inject_unicode(self, keysym: int, is_press: bool) -> None:
        """Gõ ký tự ngoài layout: remap tạm 1 keycode (kỹ thuật xdotool)."""
        from Xlib import X

        keycode = self._spare_keycode()
        if keycode is None:
            log.warning("Không tìm được keycode trống để inject Unicode")
            return
        if self._spare_keysym != keysym:
            if self._restore_timer is not None:
                # Huỷ khôi phục đang chờ để không đổi mapping giữa chừng.
                self._restore_timer.cancel()
                self._restore_timer = None
            if self._spare_original is None:
                self._spare_original = list(
                    self._display.get_keyboard_mapping(keycode, 1)[0])
            self._display.change_keyboard_mapping(keycode, [[keysym] * 4])
            self._display.sync()
            self._spare_keysym = keysym
        self._xtest.fake_input(
            self._display, X.KeyPress if is_press else X.KeyRelease, keycode)
        self._display.sync()
        if not is_press:
            self._schedule_restore()

    def _schedule_restore(self) -> None:
        if self._restore_timer is not None:
            self._restore_timer.cancel()
        self._restore_timer = threading.Timer(0.1, self._restore_mapping)
        self._restore_timer.daemon = True
        self._restore_timer.start()

    def _restore_mapping(self) -> None:
        with self._lock:
            if self._spare is None or self._spare_original is None:
                return
            try:
                self._display.change_keyboard_mapping(
                    self._spare, [self._spare_original])
                self._display.sync()
            except Exception as exc:
                log.debug("Không khôi phục được keymap: %s", exc)
            self._spare_original = None
            self._spare_keysym = None

    def _inject(self, key: str, is_press: bool) -> None:
        from Xlib import X

        keysym = key_to_keysym(key)
        if keysym is None:
            log.warning("Unknown special key: %s", key)
            return
        keycode = self._display.keysym_to_keycode(keysym)
        if not keycode:
            if len(key) == 1 and not key.startswith("Key."):
                # Ký tự không có trong layout host → inject Unicode trực tiếp.
                log.debug("Ký tự %r ngoài layout, inject Unicode keysym 0x%X",
                          key, keysym)
                self._inject_unicode(keysym, is_press)
                return
            log.warning("Không map được phím %r trên layout hiện tại", key)
            return

        shift_kc = self._display.keysym_to_keycode(0xFFE1)   # Shift_L
        altgr_kc = self._display.keysym_to_keycode(0xFE03)   # ISO_Level3_Shift
        need_shift = need_altgr = False
        if self._display.keycode_to_keysym(keycode, 0) != keysym:
            for index, shift, altgr in ((1, True, False), (2, False, True),
                                        (3, True, True)):
                if self._display.keycode_to_keysym(keycode, index) == keysym:
                    need_shift, need_altgr = shift, altgr
                    break
        mods = self._display.screen().root.query_pointer().mask
        press_shift = need_shift and not (mods & X.ShiftMask)
        press_altgr = need_altgr and not (mods & 0x80)

        if press_shift:
            self._xtest.fake_input(self._display, X.KeyPress, shift_kc)
        if press_altgr:
            self._xtest.fake_input(self._display, X.KeyPress, altgr_kc)
        self._xtest.fake_input(
            self._display, X.KeyPress if is_press else X.KeyRelease, keycode)
        if press_altgr:
            self._xtest.fake_input(self._display, X.KeyRelease, altgr_kc)
        if press_shift:
            self._xtest.fake_input(self._display, X.KeyRelease, shift_kc)
        self._display.sync()


_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004


def _send_unicode_windows(char: str) -> bool:
    """Gõ 1 ký tự Unicode trực tiếp trên Windows (SendInput KEYEVENTF_UNICODE).

    Dùng khi ký tự không có trong layout bàn phím host (pynput VkKeyScan lỗi).
    """
    try:
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        scan = ord(char)
        if scan > 0xFFFF:
            return False  # ngoài BMP: cần surrogate pair, chưa hỗ trợ
        inputs = (INPUT * 2)()
        for i, flags in enumerate((_KEYEVENTF_UNICODE,
                                   _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)):
            inputs[i].type = 1  # INPUT_KEYBOARD
            inputs[i].ki = KEYBDINPUT(0, scan, flags, 0, None)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        sent = user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        return sent == 2
    except Exception as exc:
        log.debug("SendInput Unicode lỗi: %s", exc)
        return False


class PynputKeyboard:
    """Inject bàn phím qua pynput trên Windows/macOS.

    Client gửi phím đặc biệt dạng ``Key.<name>`` (theo pynput), còn lại là
    ký tự thường. ``Key.return`` là alias cũ của ``Key.enter``. Ký tự ngoài
    layout host được gõ bằng SendInput Unicode (xem ``_send_unicode_windows``).
    """

    def __init__(self) -> None:
        from pynput.keyboard import Controller

        self._kbd = Controller()
        self._lock = threading.Lock()
        # Ký tự đã gửi bằng Unicode SendInput (press gửi cả down+up).
        self._unicode_pending: set[str] = set()

    @staticmethod
    def _key(key: str):
        from pynput.keyboard import Key

        if key.startswith("Key."):
            name = key[4:]
            if name == "return":
                name = "enter"
            return getattr(Key, name)
        return key

    def press(self, key: str) -> None:
        with self._lock:
            try:
                self._kbd.press(self._key(key))
                return
            except Exception:
                pass
            if len(key) == 1 and _send_unicode_windows(key):
                self._unicode_pending.add(key)
            else:
                log.warning("Không inject được phím %r trên layout hiện tại", key)

    def release(self, key: str) -> None:
        with self._lock:
            if key in self._unicode_pending:
                self._unicode_pending.discard(key)
                return
            try:
                self._kbd.release(self._key(key))
            except Exception as exc:
                log.debug("Keyboard release error (%s): %s", key, exc)


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


# Windows: dùng PowerShell (có sẵn trong Windows 10/11), ép UTF-8 để không
# hỏng ký tự có dấu. GUI host override bằng Qt clipboard nên chỉ CLI dùng.

def _clipboard_set_windows(text: str) -> None:
    try:
        sp.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::InputEncoding=[Text.Encoding]::UTF8; "
             "[Console]::In.ReadToEnd() | Set-Clipboard"],
            input=text.encode("utf-8"), capture_output=True, timeout=4,
            **no_console_kwargs())
    except FileNotFoundError:
        log.debug("powershell not found, clipboard set disabled")
    except Exception as exc:
        log.debug("powershell set clipboard error: %s", exc)


def _clipboard_get_windows() -> str:
    try:
        p = sp.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             "Get-Clipboard -Raw"],
            capture_output=True, timeout=4, **no_console_kwargs())
        if p.returncode == 0:
            return p.stdout.decode("utf-8", errors="replace").rstrip("\r\n")
    except FileNotFoundError:
        log.debug("powershell not found, clipboard get disabled")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Environment check
# ---------------------------------------------------------------------------

def _probe_display() -> bool:
    """Thử capture 1 frame để chắc chắn DISPLAY hiện tại dùng được."""
    try:
        with mss.MSS() as sct:
            sct.grab(sct.monitors[1])
        return True
    except Exception:
        return False


def detect_display() -> bool:
    """Tự dò X display khi chạy từ SSH/tty (DISPLAY trống).

    Quét socket trong /tmp/.X11-unix, thử lần lượt các Xauthority khả dụng,
    chỉ nhận display capture được thật.
    """
    if sys.platform != "linux":
        # Windows/macOS: mss & pynput dùng API native, không cần X11.
        return True
    if os.environ.get("DISPLAY"):
        return True

    x11_dir = "/tmp/.X11-unix"
    if not os.path.isdir(x11_dir):
        return False
    displays = sorted(
        (s for s in os.listdir(x11_dir) if s.startswith("X") and s[1:].isdigit()),
        key=lambda s: int(s[1:]))

    auth_candidates = [
        os.environ.get("XAUTHORITY", ""),
        os.path.expanduser("~/.Xauthority"),
        f"/run/user/{os.getuid()}/xauth",
    ]
    for disp in displays:
        display_str = f":{disp[1:]}"
        for auth in auth_candidates:
            if auth and not os.path.isfile(auth):
                continue
            if auth:
                os.environ["XAUTHORITY"] = auth
            os.environ["DISPLAY"] = display_str
            if _probe_display():
                log.info("Tự dò DISPLAY=%s (XAUTHORITY=%s)",
                         display_str, auth or "mặc định")
                return True
        os.environ.pop("DISPLAY", None)
    return False


def check_display_env() -> None:
    if sys.platform != "linux":
        log.info("Session %s — capture/input native, không cần X11.", sys.platform)
        return
    detected = False
    if not os.environ.get("DISPLAY"):
        detected = detect_display()
    session = os.environ.get("XDG_SESSION_TYPE", "")
    display = os.environ.get("DISPLAY", "")
    log.info("XDG_SESSION_TYPE=%r", session)
    log.info("DISPLAY=%r", display)
    if display and (detected or _probe_display()):
        log.info("Session X11 OK.")
        return
    log.warning("=" * 64)
    log.warning("CẢNH BÁO: Không kết nối được X11 — capture & inject sẽ lỗi.")
    log.warning("Chạy host trong terminal của session desktop, hoặc từ SSH:")
    log.warning("  DISPLAY=:0 python host.py ...")
    log.warning("=" * 64)


# ---------------------------------------------------------------------------
# Host server
# ---------------------------------------------------------------------------

def list_monitors() -> list[dict]:
    """Danh sách màn hình (mss index ≥ 1; bỏ index 0 = toàn bộ desktop)."""
    result: list[dict] = []
    try:
        with mss.MSS() as sct:
            for index, mon in enumerate(sct.monitors):
                if index == 0:
                    continue
                result.append({
                    "index": index,
                    "width": mon["width"],
                    "height": mon["height"],
                    "left": mon["left"],
                    "top": mon["top"],
                    "primary": index == 1,
                })
    except Exception as exc:
        log.debug("Không liệt kê được monitors: %s", exc)
    return result


class HostServer:
    """TCP server stream màn hình + nhận control."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop_event = threading.Event()
        self.server_sock: socket.socket | None = None

        self._mouse = None
        self._keyboard = None
        self._view_only_logged = False
        # Client có thể đổi max-width khi đang stream (set_resolution).
        self._max_width = self._clamp_width(args.max_width)
        # Multi-monitor: mss index (≥ 1); client có thể đổi (set_monitor).
        self._monitor_index = 1
        # Generation của codec packet: tăng mỗi lần tạo lại encoder H.264;
        # client bỏ packet codec cũ đến trễ (generation <= đã nhận).
        self._codec_generation = 0
        # Mốc input tương tác gần nhất (monotonic) + throttle log đo phản hồi.
        self._last_input_at = 0.0
        self._last_input_log = 0.0
        self._monitor_rect_cache: tuple[int, int, int, int] | None = None
        self._monitor_rect_index: int | None = None

        # Clipboard
        self._clipboard_send_queue: queue.Queue[str] = queue.Queue()
        self._last_clipboard = ""
        self._clipboard_skip = 0
        self.clipboard_get = (
            _clipboard_get_windows if sys.platform == "win32"
            else _clipboard_get_xclip)
        self.clipboard_set = (
            _clipboard_set_windows if sys.platform == "win32"
            else _clipboard_set_xclip)
        self.on_clipboard_received = None
        # Callback cho GUI: nhận addr khi client kết nối/ngắt.
        self.on_client_connected = None
        self.on_client_disconnected = None
        self._client_stop: threading.Event | None = None
        self._client_sock: socket.socket | None = None
        # Auth: đếm lần sai / khóa tạm theo IP.
        self._auth_failures: dict[str, int] = {}
        self._auth_blocked_until: dict[str, float] = {}
        self._auth_lock = threading.Lock()

    def _notify(self, callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            log.warning("Callback lỗi: %s", exc)

    def disconnect_client(self) -> None:
        """Ngắt client đang kết nối (dùng cho nút trên host GUI)."""
        sock, stop = self._client_sock, self._client_stop
        if sock is not None:
            try:
                protocol.send_json(sock, protocol.PKT_INFO,
                                   {"type": "error",
                                    "message": "host đã ngắt kết nối"})
            except OSError:
                pass
        if stop is not None:
            stop.set()
        _safe_close(sock)

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
            self._notify(self.on_client_connected, addr)
            try:
                self._handle_client(client_sock, addr)
            except Exception as exc:
                log.error("Lỗi xử lý client %s: %s", addr, exc)
            finally:
                _safe_close(client_sock)
                self._client_sock = None
                self._client_stop = None
                log.info("Client %s:%d đã ngắt", *addr)
                self._notify(self.on_client_disconnected)

    def shutdown(self) -> None:
        self.stop_event.set()
        _safe_close(self.server_sock)

    def _handle_client(self, sock: socket.socket, addr) -> None:
        sock.settimeout(10.0)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        # Đăng ký sớm để GUI có thể "ngắt client" cả khi đang xác thực.
        client_stop = threading.Event()
        self._client_stop = client_stop
        self._client_sock = sock
        ptype, payload = protocol.recv_packet(sock)
        if ptype != protocol.PKT_HELLO:
            log.warning("Client %s gửi packet không phải hello", addr)
            return
        try:
            hello = protocol.decode_json(payload)
        except Exception:
            log.warning("Hello không phải JSON hợp lệ")
            return

        ip = addr[0]
        now = time.time()
        with self._auth_lock:
            blocked_until = self._auth_blocked_until.get(ip, 0.0)
        if now < blocked_until:
            log.warning("AUTH BLOCKED từ %s (còn %.0fs)", addr, blocked_until - now)
            try:
                protocol.send_json(sock, protocol.PKT_INFO,
                                   {"type": "error",
                                    "message": "too many attempts, thử lại sau"})
            except OSError:
                pass
            return

        if not _token_matches(hello.get("token"), self.args.token):
            with self._auth_lock:
                failures = self._auth_failures.get(ip, 0) + 1
                self._auth_failures[ip] = failures
                if failures >= _AUTH_MAX_FAILURES:
                    self._auth_blocked_until[ip] = now + _AUTH_BLOCK_SECONDS
                    self._auth_failures[ip] = 0
                    log.warning("AUTH BLOCK %s sau %d lần sai (%.0fs)",
                                addr, failures, _AUTH_BLOCK_SECONDS)
            log.warning("AUTH FAIL từ %s (lần %d)", addr, failures)
            try:
                protocol.send_json(sock, protocol.PKT_INFO,
                                   {"type": "error", "message": "auth failed"})
            except OSError:
                pass
            return

        with self._auth_lock:
            self._auth_failures.pop(ip, None)
        log.info("AUTH SUCCESS từ %s", addr)
        if client_stop.is_set():
            log.info("Client %s bị ngắt trong lúc xác thực", addr)
            return
        protocol.send_json(sock, protocol.PKT_INFO,
                           {"type": "info", "message": "auth ok"})
        try:
            protocol.send_json(sock, protocol.PKT_INFO,
                               {"type": "monitors",
                                "monitors": list_monitors(),
                                "current": self._monitor_index})
        except OSError:
            return

        # Codec negotiation: client's supported codecs
        client_codecs = hello.get("codecs", ["jpeg"])
        self._client_supports_h264 = "h264" in client_codecs

        sock.settimeout(None)

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
        encode_params = _jpeg_encode_params(self.args.quality)
        frames_since_log = 0
        last_log = time.time()

        # Decide codec: host supports h264 AND client supports it
        host_h264 = (self.args.codec == 'h264' and H264Encoder.available()
                     and H264Encoder.libx264_available())
        use_h264 = host_h264 and getattr(self, '_client_supports_h264', False)
        if self.args.codec == 'h264' and not use_h264:
            log.info("Client không hỗ trợ H.264, fallback JPEG")
        h264_enc: H264Encoder | None = None
        # Thời điểm tạo lại encoder gần nhất (debounce _RECREATE_MIN_INTERVAL).
        last_recreate = 0.0

        if use_h264:
            log.info("Sử dụng codec H.264 (ffmpeg libx264)")
        else:
            log.info("Sử dụng codec JPEG")

        try:
            with mss.MSS() as sct:
                active_monitor = self._clamp_monitor(sct, self._monitor_index)
                self._monitor_index = active_monitor
                monitor = sct.monitors[active_monitor]

                # First capture to get dimensions
                shot = sct.grab(monitor)
                first = np.asarray(shot)
                cap_h, cap_w = first.shape[:2]
                active_max_width = self._max_width
                if cap_w > active_max_width:
                    scale = active_max_width / cap_w
                    frame_w, frame_h = active_max_width, int(cap_h * scale)
                else:
                    frame_w, frame_h = cap_w, cap_h

                if use_h264:
                    # yuv420p (libx264) yêu cầu kích thước chẵn.
                    frame_w -= frame_w % 2
                    frame_h -= frame_h % 2
                    h264_enc = H264Encoder(frame_w, frame_h, self.args.fps,
                                           self.args.h264_crf)
                    last_recreate = time.time()
                    self._codec_generation += 1
                    protocol.send_json(
                        sock, protocol.PKT_INFO,
                        {"type": "codec", "codec": "h264",
                         "width": frame_w, "height": frame_h,
                         "generation": self._codec_generation})

                skip_until = 0.0
                prev_frame: np.ndarray | None = None
                last_jpeg = b""
                sent_w = sent_h = 0
                last_send = time.time()
                force_send = False
                # H.264: decoder giữ 1-2 frame (parser + libavcodec), P-frame
                # rất nhỏ nên heartbeat dày hơn để không lag khi màn hình tĩnh.
                heartbeat_interval = 0.25 if use_h264 else 1.0
                frame_ms_total = 0.0
                frame_ms_count = 0

                while not client_stop.is_set() and not self.stop_event.is_set():
                    now = time.time()
                    if now < skip_until:
                        time.sleep(min(0.01, skip_until - now))
                        continue

                    t0 = now
                    shot = sct.grab(monitor)
                    # View BGRA trên buffer của mss — không copy.
                    frame = np.asarray(shot)

                    # Client đổi monitor / độ phân giải giữa chừng?
                    monitor_changed = self._monitor_index != active_monitor
                    if (monitor_changed
                            or self._max_width != active_max_width):
                        # Debounce chỉ cho đổi max-width (auto-resize dồn dập
                        # của client); đổi monitor áp dụng NGAY — không để
                        # user nhìn thấy màn hình cũ thêm ~0.5s.
                        if (not monitor_changed and use_h264
                                and time.time() - last_recreate
                                < _RECREATE_MIN_INTERVAL):
                            # Dồn loạt đổi độ phân giải thành 1 lần tạo lại
                            # encoder. skip_until tính thẳng từ last_recreate
                            # (cùng hệ thời gian với time.time() ở đầu vòng).
                            skip_until = (last_recreate
                                          + _RECREATE_MIN_INTERVAL)
                            continue
                        if monitor_changed:
                            active_monitor = self._clamp_monitor(
                                sct, self._monitor_index)
                            self._monitor_index = active_monitor
                            monitor = sct.monitors[active_monitor]
                            frame = np.asarray(sct.grab(monitor))
                            prev_frame = None
                            log.info("Đổi monitor: #%d (%dx%d)",
                                     active_monitor, frame.shape[1],
                                     frame.shape[0])
                        active_max_width = self._max_width
                        cap_w, cap_h = frame.shape[1], frame.shape[0]
                        if cap_w > active_max_width:
                            scale = active_max_width / cap_w
                            frame_w = active_max_width
                            frame_h = int(cap_h * scale)
                        else:
                            frame_w, frame_h = cap_w, cap_h
                        if use_h264:
                            frame_w -= frame_w % 2
                            frame_h -= frame_h % 2
                            if h264_enc:
                                h264_enc.close()
                            h264_enc = H264Encoder(frame_w, frame_h,
                                                   self.args.fps,
                                                   self.args.h264_crf)
                            last_recreate = time.time()
                            self._codec_generation += 1
                            protocol.send_json(
                                sock, protocol.PKT_INFO,
                                {"type": "codec", "codec": "h264",
                                 "width": frame_w, "height": frame_h,
                                 "generation": self._codec_generation})
                        last_jpeg = b""
                        force_send = True
                        log.info("Cấu hình stream: monitor #%d, max-width=%d → %dx%d",
                                 active_monitor, active_max_width,
                                 frame_w, frame_h)

                    # Differential update: màn hình tĩnh cho frame giống hệt
                    # nhau nên chỉ cần so khớp chính xác — bắt được cả thay
                    # đổi nhỏ (con trỏ soạn thảo, text) mà ngưỡng MSE bỏ sót.
                    changed = (prev_frame is None
                               or frame.shape != prev_frame.shape
                               or not np.array_equal(frame, prev_frame))
                    prev_frame = frame

                    if (not changed and not force_send
                            and (now - last_send) < heartbeat_interval):
                        # Chưa đủ thay đổi, chưa đến hạn heartbeat
                        elapsed = time.time() - t0
                        if elapsed < frame_budget:
                            time.sleep(frame_budget - elapsed)
                        # Đã trễ hơn budget -> quay vòng grab NGAY, không ngủ
                        # thêm budget (tránh nhân đôi gap khi grab/compare
                        # chậm hơn bình thường).
                        continue

                    if use_h264 and h264_enc:
                        if frame.shape[1] != frame_w or frame.shape[0] != frame_h:
                            frame = cv2.resize(
                                frame, (frame_w, frame_h),
                                interpolation=cv2.INTER_AREA)
                        data = h264_enc.encode(frame)
                        if data:
                            protocol.send_packet(
                                sock, protocol.PKT_FRAME_H264, data)
                        sent_w, sent_h = frame_w, frame_h
                    else:
                        # Không thay đổi (heartbeat) thì gửi lại JPEG đã encode,
                        # khỏi cvtColor + imencode lại.
                        if changed or not last_jpeg:
                            img = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                            if img.shape[1] > active_max_width:
                                scale = active_max_width / img.shape[1]
                                img = cv2.resize(
                                    img, (active_max_width,
                                          int(img.shape[0] * scale)),
                                    interpolation=cv2.INTER_AREA)
                            ok, buf = cv2.imencode(".jpg", img, encode_params)
                            if not ok:
                                log.error("cv2.imencode thất bại")
                                skip_until = now + frame_budget
                                continue
                            last_jpeg = buf.tobytes()
                            sent_w, sent_h = img.shape[1], img.shape[0]
                        protocol.send_packet(
                            sock, protocol.PKT_FRAME, last_jpeg)

                    last_send = time.time()
                    force_send = False
                    # Đo phản hồi: input gần nhất của client → frame vừa gửi
                    # (apply + chờ capture + encode + send). Chỉ log khi còn
                    # trong 3s kể từ input, throttle 0.5s để không spam.
                    if self._last_input_at:
                        resp = time.monotonic() - self._last_input_at
                        if (resp < 3.0
                                and time.time() - self._last_input_log >= 0.5):
                            log.info("input → frame gửi: %.0f ms",
                                     resp * 1000.0)
                            self._last_input_log = time.time()
                    frame_ms_total += (last_send - t0) * 1000.0
                    frame_ms_count += 1
                    frames_since_log += 1
                    now2 = time.time()
                    if now2 - last_log >= 5.0:
                        fps = frames_since_log / (now2 - last_log)
                        avg_ms = frame_ms_total / max(1, frame_ms_count)
                        skipped = max(0, int(1.0 / max(0.001, frame_budget) - fps))
                        if use_h264:
                            log.info("Stream ~%.1f FPS (H.264 %dx%d, skip ~%d, %.0f ms/frame)",
                                     fps, sent_w, sent_h, skipped, avg_ms)
                        else:
                            log.info("Stream ~%.1f FPS (JPEG %dx%d, q=%d, skip ~%d, %.0f ms/frame)",
                                     fps, sent_w, sent_h,
                                     self.args.quality, skipped, avg_ms)
                        frames_since_log = 0
                        frame_ms_total = 0.0
                        frame_ms_count = 0
                        last_log = now2

                    elapsed = now2 - t0
                    if elapsed < frame_budget:
                        time.sleep(frame_budget - elapsed)
                    # encode/send đã trễ hơn budget -> quay vòng NGAY, không
                    # ngủ thêm budget: một lần encode 80ms ở 15fps mà ngủ thêm
                    # 66ms sẽ thành gap ~146ms -> stutter thấy rõ (latency
                    # review: pacing phải neo theo capture-start, không cộng
                    # dồn sau khi trễ).
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
                try:
                    self._apply_event(event)
                except Exception as exc:
                    log.warning("Bỏ qua control event lỗi: %s", exc)
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
        if kind in ("mouse_move", "mouse_down", "mouse_up", "scroll",
                    "key_down", "key_up"):
            # Tương tác của client — lưu mốc để capture loop đo thời gian
            # "input → frame gửi ra" (log INFO, throttle 0.5s).
            self._last_input_at = time.monotonic()
            log.debug("input ← %s", event)
        if kind in ("mouse_move", "mouse_down", "mouse_up", "scroll"):
            self._apply_mouse(event)
        elif kind in ("key_down", "key_up"):
            self._apply_keyboard(event)
        elif kind == "clipboard":
            self._apply_clipboard(event)
        elif kind == "set_resolution":
            self._apply_resolution(event)
        elif kind == "set_monitor":
            self._apply_monitor(event)

    def _apply_monitor(self, event: dict) -> None:
        try:
            index = int(event.get("index", 1))
        except (TypeError, ValueError):
            return
        index = max(1, index)
        if index != self._monitor_index:
            self._monitor_index = index
            log.info("Client yêu cầu monitor #%d", index)

    def _apply_resolution(self, event: dict) -> None:
        value = event.get("max_width")
        if value is None:
            # "Theo host": quay về tham số --max-width lúc khởi động.
            width = self._clamp_width(self.args.max_width)
        else:
            width = self._clamp_width(value)
        if width != self._max_width:
            self._max_width = width
            log.info("Client yêu cầu max-width=%d", width)

    @staticmethod
    def _clamp_width(value) -> int:
        try:
            width = int(value)
        except (TypeError, ValueError):
            return 1920
        return max(160, min(7680, width))

    def _apply_mouse(self, event: dict) -> None:
        mouse = self._get_mouse()
        if mouse is None:
            return
        from pynput.mouse import Button
        left, top, screen_w, screen_h = self._monitor_rect()
        kind = event.get("event")
        if kind == "mouse_move":
            x = left + _clamp01(event.get("x", 0.0)) * screen_w
            y = top + _clamp01(event.get("y", 0.0)) * screen_h
            mouse.position = (int(x), int(y))
        elif kind in ("mouse_down", "mouse_up"):
            x = left + _clamp01(event.get("x", 0.0)) * screen_w
            y = top + _clamp01(event.get("y", 0.0)) * screen_h
            mouse.position = (int(x), int(y))
            btn = Button.right if event.get("button") == "right" else Button.left
            if kind == "mouse_down":
                mouse.press(btn)
            else:
                mouse.release(btn)
        elif kind == "scroll":
            x = left + _clamp01(event.get("x", 0.0)) * screen_w
            y = top + _clamp01(event.get("y", 0.0)) * screen_h
            mouse.position = (int(x), int(y))
            dx = _scroll_steps(event.get("dx", 0))
            dy = _scroll_steps(event.get("dy", 0))
            if dx or dy:
                try:
                    mouse.scroll(dx, dy)
                except Exception as exc:
                    log.debug("Scroll inject error: %s", exc)

    def _apply_keyboard(self, event: dict) -> None:
        kbd = self._get_keyboard()
        if kbd is None:
            return
        kind = event.get("event")
        key_str = event.get("key", "")
        if not key_str:
            return
        log.debug("Inject key: %s %r", kind, key_str)
        try:
            if kind == "key_down":
                kbd.press(key_str)
            else:
                kbd.release(key_str)
        except Exception as exc:
            log.warning("Keyboard inject error (%s): %s", key_str, exc)

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
                if sys.platform == "linux":
                    self._keyboard = X11Keyboard()
                else:
                    self._keyboard = PynputKeyboard()
            except Exception as exc:
                log.error("Không khởi tạo được keyboard: %s", exc)
                self._keyboard = False
        return self._keyboard or None

    def _monitor_rect(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) của monitor đang stream (cached)."""
        if (self._monitor_rect_cache is None
                or self._monitor_rect_index != self._monitor_index):
            with mss.MSS() as sct:
                index = self._clamp_monitor(sct, self._monitor_index)
                mon = sct.monitors[index]
            self._monitor_rect_cache = (
                mon["left"], mon["top"], mon["width"], mon["height"])
            self._monitor_rect_index = self._monitor_index
        return self._monitor_rect_cache

    @staticmethod
    def _clamp_monitor(sct, index: int) -> int:
        count = len(sct.monitors)
        return max(1, min(int(index), count - 1))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _clamp01(v: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _scroll_steps(v) -> int:
    """Chuẩn hoá số bước cuộn, chặn giá trị bất thường từ client."""
    try:
        steps = int(v)
    except (TypeError, ValueError):
        return 0
    return max(-50, min(50, steps))


def _safe_close(sock) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _token_matches(given, expected: str) -> bool:
    """So token bằng compare_digest để tránh timing attack."""
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(given.encode("utf-8", "replace"),
                               expected.encode("utf-8", "replace"))


def warn_weak_config(bind: str, token: str) -> None:
    """Cảnh báo cấu hình dễ bị tấn công (bind mở, token yếu)."""
    if bind in ("0.0.0.0", "::"):
        log.warning("=" * 64)
        log.warning("CẢNH BÁO: bind %s = mở trên MỌI interface (LAN/Internet).", bind)
        log.warning("Nên --bind <IP Tailscale> hoặc chặn firewall (README mục 6).")
        log.warning("=" * 64)
    if len(token) < 6:
        log.warning("Token yếu (< 6 ký tự) — chỉ nên dùng trong tailnet.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Remote desktop HOST (X11 hoặc Windows).")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--token", default="1")
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--quality", type=int, default=90,
                   help="Chất lượng JPEG (1-100, sampling 4:4:4)")
    p.add_argument("--max-width", type=int, default=2560)
    p.add_argument("--h264-crf", type=int, default=18,
                   help="CRF H.264 (0-51, thấp hơn = nét; mặc định 18)")
    p.add_argument("--codec", choices=["jpeg", "h264"], default="jpeg",
                   help="Codec: jpeg hoặc h264 (mặc định jpeg; h264 cần ffmpeg, có thể bị delay)")
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

    try:
        log_path = app_log_dir() / "host.log"
        fh = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(fh)
        log.info("Host log file: %s", log_path)
    except OSError as exc:
        log.warning("Không mở được file log: %s", exc)
    if args.debug:
        log.info("DEBUG mode ON")

    log.info("=== Remote desktop HOST ===")
    log.info("Token: %s | Codec: %s", args.token, args.codec)
    check_display_env()
    warn_weak_config(args.bind, args.token)
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
