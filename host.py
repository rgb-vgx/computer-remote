#!/usr/bin/env python3
"""host.py — Remote desktop HOST (Linux/X11 hoặc Windows).

Supports JPEG and H.264 codec. H.264 requires ffmpeg with libx264.

Chạy:
  python host.py --codec h264 --token "1"
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
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
from collections import deque
from logging.handlers import RotatingFileHandler

import cv2
import mss
import numpy as np

from common import app_log_dir, filetransfer, no_console_kwargs, protocol

log = logging.getLogger("host")

# Chống dò token: khóa IP tạm thời sau N lần sai.
_AUTH_MAX_FAILURES = 5
_AUTH_BLOCK_SECONDS = 30.0
# Event điều khiển chuột/bàn phím — cần quyền "control".
_INPUT_EVENTS = ("mouse_move", "mouse_down", "mouse_up", "scroll",
                 "key_down", "key_up")

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
        self.crf = int(max(0, min(51, crf)))

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
        _warn_no_clipboard()
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
        _warn_no_clipboard()
    except Exception:
        pass
    return ""


class X11Clipboard:
    """CLIPBOARD của X11 qua python-xlib — không cần cài xclip/xsel.

    Hai kết nối X riêng (python-xlib không thread-safe):
    - kết nối "đọc": ``get_text()`` gửi ConvertSelection rồi chờ SelectionNotify.
    - kết nối "sở hữu": thread nền giữ quyền sở hữu CLIPBOARD sau ``set_text()``
      và trả dữ liệu cho app khác dán (SelectionRequest), kể cả giao thức INCR
      cho text lớn hơn một request X.
    """

    _CHUNK = 128 * 1024

    def __init__(self) -> None:
        from Xlib import X, display

        self._X = X
        self._reader = display.Display()
        self._owner = display.Display()
        self._read_lock = threading.Lock()
        self._set_lock = threading.Lock()
        atom = self._reader.intern_atom
        self._clipboard = atom("CLIPBOARD")
        self._utf8 = atom("UTF8_STRING")
        self._targets = atom("TARGETS")
        self._incr = atom("INCR")
        self._prop = atom("REMOTE_MVP_CLIP")
        self._text_targets = [self._utf8, atom("TEXT"), atom("STRING"),
                              atom("text/plain;charset=utf-8"),
                              atom("text/plain")]
        self._string = atom("STRING")
        self._atom_type = atom("ATOM")
        self._read_win = self._reader.screen().root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent,
            event_mask=X.PropertyChangeMask)
        self._owner_win = self._owner.screen().root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent,
            event_mask=X.PropertyChangeMask)
        self._reader.flush()
        self._owner.flush()
        self._text: str | None = None      # text mình đang sở hữu
        self._pending: list[str] = []      # set_text chờ thread nền áp dụng
        self._incr_jobs: dict[tuple[int, int], tuple[object, bytes, int]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._owner_loop,
                                        name="x11-clipboard", daemon=True)
        self._thread.start()

    # ---- Đọc ------------------------------------------------------------

    def get_text(self, timeout: float = 1.0) -> str:
        X = self._X
        with self._read_lock:
            owner = self._reader.get_selection_owner(self._clipboard)
            owner_id = getattr(owner, "id", owner) or 0
            if owner_id == X.NONE:
                return ""
            if owner_id == self._owner_win.id:
                return self._text or ""
            for target in (self._utf8, self._string):
                data = self._convert(target, timeout)
                if data is not None:
                    encoding = "utf-8" if target == self._utf8 else "latin-1"
                    return data.decode(encoding, errors="replace")
            return ""

    def _wait_event(self, display, match, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            while display.pending_events():
                ev = display.next_event()
                if match(ev):
                    return ev
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            select.select([display], [], [], remaining)

    def _convert(self, target: int, timeout: float) -> bytes | None:
        X = self._X
        win = self._read_win
        win.delete_property(self._prop)
        win.convert_selection(self._clipboard, target, self._prop, X.CurrentTime)
        self._reader.flush()
        ev = self._wait_event(
            self._reader,
            lambda e: e.type == X.SelectionNotify and e.requestor.id == win.id,
            timeout)
        if ev is None or ev.property == X.NONE:
            return None
        reply = win.get_full_property(self._prop, X.AnyPropertyType)
        if reply is None:
            return None
        if reply.property_type != self._incr:
            win.delete_property(self._prop)
            self._reader.flush()
            return bytes(reply.value)
        # INCR: xoá property để báo sẵn sàng, đọc từng phần tới khi rỗng.
        chunks = []
        win.delete_property(self._prop)
        self._reader.flush()
        while True:
            ev = self._wait_event(
                self._reader,
                lambda e: (e.type == X.PropertyNotify and e.window.id == win.id
                           and e.atom == self._prop
                           and e.state == X.PropertyNewValue),
                timeout)
            if ev is None:
                return None
            part = win.get_full_property(self._prop, X.AnyPropertyType)
            win.delete_property(self._prop)
            self._reader.flush()
            if part is None or not part.value:
                return b"".join(chunks)
            chunks.append(bytes(part.value))

    # ---- Ghi (sở hữu selection) ---------------------------------------

    def set_text(self, text: str) -> None:
        with self._set_lock:
            self._pending.append(text)

    def close(self) -> None:
        self._stop.set()

    def _owner_loop(self) -> None:
        X = self._X
        display = self._owner
        while not self._stop.is_set():
            try:
                with self._set_lock:
                    pending, self._pending = self._pending, []
                if pending:
                    self._text = pending[-1]
                    self._owner_win.set_selection_owner(self._clipboard,
                                                        X.CurrentTime)
                    display.flush()
                while display.pending_events():
                    self._handle_owner_event(display.next_event())
                select.select([display], [], [], 0.05)
            except Exception as exc:
                log.warning("Clipboard X11 lỗi: %s", exc)
                time.sleep(0.5)

    def _handle_owner_event(self, ev) -> None:
        X = self._X
        if ev.type == X.SelectionClear:
            self._text = None  # app khác đã copy → không còn là chủ
        elif ev.type == X.SelectionRequest:
            self._serve(ev)
        elif ev.type == X.PropertyNotify and ev.state == X.PropertyDelete:
            self._continue_incr(ev)

    def _serve(self, ev) -> None:
        from Xlib.protocol import event as xevent

        X = self._X
        requestor = ev.requestor
        prop = ev.property if ev.property != X.NONE else ev.target
        text = self._text
        if text is None or ev.selection != self._clipboard:
            prop = X.NONE
        elif ev.target == self._targets:
            requestor.change_property(prop, self._atom_type, 32,
                                      [self._targets, *self._text_targets])
        elif ev.target in self._text_targets:
            if ev.target == self._string:
                data = text.encode("latin-1", errors="replace")
            else:
                data = text.encode("utf-8")
            if len(data) > self._CHUNK:
                # INCR: báo kích thước, gửi từng phần mỗi khi bên nhận xoá property.
                requestor.change_attributes(event_mask=X.PropertyChangeMask)
                requestor.change_property(prop, self._incr, 32, [len(data)])
                self._incr_jobs[(requestor.id, prop)] = (requestor, data, 0)
            else:
                requestor.change_property(prop, ev.target, 8, data)
        else:
            prop = X.NONE
        notify = xevent.SelectionNotify(
            time=ev.time, requestor=requestor, selection=ev.selection,
            target=ev.target, property=prop)
        requestor.send_event(notify)
        self._owner.flush()

    def _continue_incr(self, ev) -> None:
        key = (ev.window.id, ev.atom)
        job = self._incr_jobs.get(key)
        if job is None:
            return
        requestor, data, offset = job
        chunk = data[offset:offset + self._CHUNK]
        requestor.change_property(ev.atom, self._utf8, 8, chunk)
        if chunk:
            self._incr_jobs[key] = (requestor, data, offset + len(chunk))
        else:
            del self._incr_jobs[key]  # gửi property rỗng = kết thúc
        self._owner.flush()


_x11_clipboard: X11Clipboard | None = None
_x11_clipboard_failed = False
_clipboard_warned = False


def _linux_clipboard() -> X11Clipboard | None:
    global _x11_clipboard, _x11_clipboard_failed
    if _x11_clipboard is None and not _x11_clipboard_failed:
        try:
            _x11_clipboard = X11Clipboard()
        except Exception as exc:
            _x11_clipboard_failed = True
            log.warning("Không mở được clipboard X11 (%s) — thử dùng xclip", exc)
    return _x11_clipboard


def _clipboard_get_linux() -> str:
    cb = _linux_clipboard()
    if cb is not None:
        try:
            return cb.get_text()
        except Exception as exc:
            log.debug("Đọc clipboard X11 lỗi: %s", exc)
            return ""
    return _clipboard_get_xclip()


def _clipboard_set_linux(text: str) -> None:
    cb = _linux_clipboard()
    if cb is not None:
        cb.set_text(text)
        return
    _clipboard_set_xclip(text)


def _warn_no_clipboard() -> None:
    global _clipboard_warned
    if not _clipboard_warned:
        _clipboard_warned = True
        log.warning("Không có xclip — đồng bộ clipboard bị tắt "
                    "(cài: sudo apt install xclip)")


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

# ---------------------------------------------------------------------------
# Kiểm soát luồng + tự chỉnh chất lượng theo đường truyền
# ---------------------------------------------------------------------------

class FrameFlow:
    """Giới hạn số frame "đang bay" (đã gửi, client chưa ack).

    TCP tự nới buffer gửi lên nhiều MB: nếu cứ sendall thì frame xếp hàng
    trong kernel và độ trễ tăng tới hàng giây khi mạng chậm. Chờ ack trước
    khi gửi tiếp → luôn gửi frame MỚI NHẤT, độ trễ bị chặn ~max_inflight frame.
    Client cũ không gửi ack → ``enabled=False``, hành vi như trước.
    """

    STALL_RESET = 3.0  # quá lâu không có ack → bỏ chờ (phòng client lỗi)

    def __init__(self, enabled: bool, max_inflight: int = 2) -> None:
        self.enabled = enabled
        self.max_inflight = max_inflight
        self.sent = 0
        self.acked = 0
        self._lock = threading.Lock()
        self._unacked: deque[tuple[int, float, int]] = deque()
        self.rtt = deque(maxlen=30)

    def can_send(self, now: float) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            if self.sent - self.acked < self.max_inflight:
                return True
            if self._unacked and now - self._unacked[0][1] > self.STALL_RESET:
                log.warning("Client không ack frame %.0fs — bỏ chờ",
                            now - self._unacked[0][1])
                self.acked = self.sent
                self._unacked.clear()
                return True
            return False

    def on_sent(self, nbytes: int, now: float) -> None:
        with self._lock:
            self.sent += 1
            if self.enabled:
                self._unacked.append((self.sent, now, nbytes))

    def on_ack(self, count, now: float) -> None:
        try:
            count = int(count)
        except (TypeError, ValueError):
            return
        with self._lock:
            count = min(count, self.sent)
            if count <= self.acked:
                return
            self.acked = count
            while self._unacked and self._unacked[0][0] <= count:
                _seq, sent_at, _nbytes = self._unacked.popleft()
                self.rtt.append(now - sent_at)


# Bậc chất lượng, từ tốt nhất → nhẹ nhất: (JPEG quality | H.264 CRF, scale).
JPEG_LEVELS = [(90, 1.0), (80, 1.0), (70, 1.0), (60, 1.0), (55, 0.85),
               (50, 0.75), (45, 0.66), (40, 0.5)]
H264_LEVELS = [(18, 1.0), (21, 1.0), (24, 1.0), (27, 0.85), (30, 0.75),
               (33, 0.66), (36, 0.5)]
# Chế độ (client chọn) → (bậc thấp nhất, bậc cao nhất, số frame đang bay).
QUALITY_MODES = {
    "balanced": (0, None, 2),
    "quality": (0, 3, 3),   # không giảm độ phân giải
    "speed": (2, None, 1),
}


class QualityController:
    """Hạ/nâng bậc chất lượng theo tỉ lệ thời gian phải chờ ack.

    - Nghẽn: >30% thời gian chờ ack và FPS < 80% mục tiêu → hạ 1 bậc ngay,
      rồi giữ nguyên một lúc (backoff 5s → 30s nếu cứ dao động).
    - Thông: <10% thời gian chờ trong 3 cửa sổ 1s liên tiếp → nâng 1 bậc.
    """

    WINDOW = 1.0

    def __init__(self, codec: str, base: int, mode: str = "balanced") -> None:
        table = H264_LEVELS if codec == "h264" else JPEG_LEVELS
        if codec == "h264":
            # CRF thấp hơn = nét hơn; tham số --h264-crf là mức nét nhất.
            self.levels = [(max(value, base), scale) for value, scale in table]
        else:
            self.levels = [(min(value, base), scale) for value, scale in table]
        self.codec = codec
        self.mode = "balanced"
        self.lo, self.hi = 0, len(self.levels) - 1
        self.level = 0
        self.max_inflight = 2
        self._window_start = time.monotonic()
        self._blocked = 0.0
        self._frames = 0
        self._good = 0
        self._hold_until = 0.0
        self._backoff = 5.0
        self._last_drop = 0.0
        self.set_mode(mode)

    @property
    def value(self) -> int:
        return self.levels[self.level][0]

    @property
    def scale(self) -> float:
        return self.levels[self.level][1]

    def set_mode(self, mode: str) -> bool:
        if mode not in QUALITY_MODES:
            return False
        lo, hi, inflight = QUALITY_MODES[mode]
        self.mode = mode
        self.lo = min(lo, len(self.levels) - 1)
        self.hi = len(self.levels) - 1 if hi is None else min(hi, len(self.levels) - 1)
        self.max_inflight = inflight
        old = self.level
        self.level = max(self.lo, min(self.hi, self.level))
        self._good = 0
        return self.level != old

    def note_blocked(self, seconds: float) -> None:
        self._blocked += seconds

    def note_frame(self) -> None:
        self._frames += 1

    def info(self) -> dict:
        return {"type": "quality", "mode": self.mode, "level": self.level,
                "levels": len(self.levels), "codec": self.codec,
                "value": self.value, "scale": self.scale}

    def tick(self, now: float, target_fps: float) -> bool:
        """Gọi mỗi vòng capture; True nếu vừa đổi bậc."""
        window = now - self._window_start
        if window < self.WINDOW:
            return False
        blocked = self._blocked / window
        fps = self._frames / window
        self._window_start = now
        self._blocked = 0.0
        self._frames = 0
        if now - self._last_drop > 60.0:
            self._backoff = 5.0  # ổn định lâu → quên lịch sử dao động
        if blocked > 0.3 and fps < 0.8 * target_fps:
            self._good = 0
            if self.level < self.hi:
                self.level += 1
                self._hold_until = now + self._backoff
                self._backoff = min(30.0, self._backoff * 2)
                self._last_drop = now
                return True
            return False
        if blocked < 0.1 and self._frames_ok(fps):
            self._good += 1
            if self._good >= 3 and now >= self._hold_until and self.level > self.lo:
                self.level -= 1
                self._good = 0
                return True
            return False
        self._good = 0
        return False

    @staticmethod
    def _frames_ok(fps: float) -> bool:
        return fps > 0  # có gửi frame (kể cả heartbeat) trong cửa sổ


def frame_size(cap_w: int, cap_h: int, max_width: int, scale: float,
               even: bool) -> tuple[int, int]:
    """Kích thước frame gửi: ≤ max-width, nhân hệ số scale (giữ tỉ lệ)."""
    width = min(cap_w, max_width)
    width = max(160, int(width * scale)) if scale < 1.0 else width
    width = min(width, cap_w)
    height = max(1, int(cap_h * width / cap_w))
    if even:  # yuv420p (libx264) yêu cầu kích thước chẵn
        width -= width % 2
        height -= height % 2
    return width, height


# ---------------------------------------------------------------------------
# Cursor tracking (client vẽ con trỏ local theo hình dạng con trỏ host)
# ---------------------------------------------------------------------------

def cursor_image_event(width: int, height: int, xhot: int, yhot: int,
                       argb) -> dict | None:
    """Ảnh con trỏ ARGB (premultiplied, như XFixes) → event PNG base64."""
    if not (0 < width <= 256 and 0 < height <= 256):
        return None
    arr = np.asarray(argb, dtype="<u4")
    if arr.size != width * height:
        return None
    bgra = arr.reshape(height, width).view(np.uint8).reshape(
        height, width, 4).copy()
    alpha = bgra[..., 3:4].astype(np.uint16)
    rgb = bgra[..., :3].astype(np.uint16)
    visible = alpha[..., 0] > 0
    rgb[visible] = np.minimum(255, rgb[visible] * 255 // alpha[visible])
    bgra[..., :3] = rgb.astype(np.uint8)
    ok, buf = cv2.imencode(".png", bgra)
    if not ok:
        return None
    return {"event": "cursor", "png": base64.b64encode(buf.tobytes()).decode(),
            "hx": int(xhot), "hy": int(yhot)}


class X11CursorSource:
    """Vị trí + ảnh con trỏ qua XFixes (chỉ gửi ảnh khi cursor_serial đổi)."""

    def __init__(self) -> None:
        from Xlib import display
        self._display = display.Display()
        if not self._display.has_extension("XFIXES"):
            raise RuntimeError("X server không có XFIXES")
        self._display.xfixes_query_version()
        self._root = self._display.screen().root
        self._serial = None

    def poll(self) -> tuple[int, int, dict | None] | None:
        img = self._display.xfixes_get_cursor_image(self._root)
        shape = None
        if img.cursor_serial != self._serial:
            self._serial = img.cursor_serial
            shape = cursor_image_event(img.width, img.height, img.xhot,
                                       img.yhot, img.cursor_image)
        return img.x, img.y, shape


class WindowsCursorSource:
    """Vị trí + hình con trỏ chuẩn (so handle với LoadCursor IDC_*)."""

    _IDC = {32512: "arrow", 32513: "ibeam", 32514: "wait", 32515: "cross",
            32516: "up_arrow", 32642: "size_fd", 32643: "size_bd",
            32644: "size_h", 32645: "size_v", 32646: "size_all",
            32648: "forbidden", 32649: "hand", 32650: "busy", 32651: "help"}

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hCursor", wintypes.HANDLE),
                        ("ptScreenPos", wintypes.POINT)]

        self._ctypes = ctypes
        self._info_cls = CURSORINFO
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.GetCursorInfo.restype = wintypes.BOOL
        user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
        self._user32 = user32
        self._names: dict[int, str] = {}
        for idc, name in self._IDC.items():
            handle = user32.LoadCursorW(None, ctypes.c_void_p(idc))
            if handle:
                self._names[handle] = name
        self._last = None

    def poll(self) -> tuple[int, int, dict | None] | None:
        info = self._info_cls()
        info.cbSize = self._ctypes.sizeof(info)
        if not self._user32.GetCursorInfo(self._ctypes.byref(info)):
            return None
        if info.flags & 1:  # CURSOR_SHOWING
            name = self._names.get(info.hCursor or 0, "arrow")
        else:
            name = "hidden"
        shape = None
        if name != self._last:
            self._last = name
            shape = {"event": "cursor", "shape": name}
        return info.ptScreenPos.x, info.ptScreenPos.y, shape


def make_cursor_source():
    """Nguồn con trỏ theo nền tảng; None nếu không hỗ trợ (SSH, Wayland...)."""
    try:
        if sys.platform == "win32":
            return WindowsCursorSource()
        if sys.platform == "linux" and os.environ.get("DISPLAY"):
            return X11CursorSource()
    except Exception as exc:
        log.info("Không theo dõi được con trỏ host: %s", exc)
    return None


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
        # Phím client đang giữ (key_down chưa có key_up) — nhả khi ngắt phiên
        # để không kẹt phím trên host (vd Tab kẹt → tự chạy tab liên tục).
        self._pressed_keys: set[str] = set()
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
            else _clipboard_get_linux)
        self.clipboard_set = (
            _clipboard_set_windows if sys.platform == "win32"
            else _clipboard_set_linux)
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
        # Quyền của phiên (gửi cho client trong packet "permissions").
        self._perms = self.default_perms()
        # Nhiều thread cùng gửi trên 1 socket (frame, clipboard, file, pong)
        # → khoá để packet không xen byte vào nhau.
        self._send_lock = threading.Lock()
        # Truyền file
        self._incoming = filetransfer.IncomingFiles()
        self._outgoing = filetransfer.SendQueue()
        self._file_queue: queue.Queue[str] = queue.Queue()
        self.on_file_progress = None  # (outgoing, name, done, total)
        self.on_file_done = None      # (outgoing, name, ok, detail)
        # Kiểm soát luồng / chất lượng (tạo lại mỗi phiên trong _handle_client).
        self._flow = FrameFlow(enabled=False)
        self._quality_mode = "balanced"
        # Con trỏ host (lazy, tạo trong capture thread).
        self._cursor_source = None
        self._cursor_source_ready = False
        self._last_cursor_pos: tuple[int, int] | None = None

    def default_perms(self) -> dict:
        return {"control": not self.args.view_only, "clipboard": True,
                "files": True}

    @property
    def permissions(self) -> dict:
        return dict(self._perms)

    def _send(self, sock: socket.socket, ptype: int, payload: bytes,
              timeout: float | None = None) -> None:
        if timeout is None:
            self._send_lock.acquire()
        elif not self._send_lock.acquire(timeout=timeout):
            raise OSError("socket bận (đang gửi frame)")
        try:
            protocol.send_packet(sock, ptype, payload)
        finally:
            self._send_lock.release()

    def _send_json(self, sock: socket.socket, ptype: int, obj: dict,
                   timeout: float | None = None) -> None:
        self._send(sock, ptype, json.dumps(obj).encode("utf-8"), timeout)

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
                # timeout: không treo GUI nếu capture thread đang kẹt sendall.
                self._send_json(sock, protocol.PKT_INFO,
                                {"type": "error",
                                 "message": "host đã ngắt kết nối"},
                                timeout=0.5)
            except OSError:
                pass
        if stop is not None:
            stop.set()
        _safe_close(sock)

    def send_clipboard(self, text: str) -> None:
        self._clipboard_send_queue.put_nowait(text)

    def send_file(self, path: str) -> None:
        """Gửi file cho client đang kết nối (GUI thread gọi; control thread gửi)."""
        self._file_queue.put_nowait(str(path))

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
                self._send_json(sock, protocol.PKT_INFO,
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
                self._send_json(sock, protocol.PKT_INFO,
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
        perms = self.default_perms()
        self._perms = perms
        self._view_only_logged = False
        self._send_json(sock, protocol.PKT_INFO,
                           {"type": "info", "message": "auth ok"})
        self._send_json(sock, protocol.PKT_INFO,
                        {"type": "permissions", **perms,
                         "features": ["files", "cursor", "ping"]})
        self._notify(self.on_client_connected, addr)
        try:
            self._send_json(sock, protocol.PKT_INFO,
                               {"type": "monitors",
                                "monitors": list_monitors(),
                                "current": self._monitor_index})
        except OSError:
            return

        # Codec negotiation: client's supported codecs
        client_codecs = hello.get("codecs", ["jpeg"])
        self._client_supports_h264 = "h264" in client_codecs
        features = hello.get("features") or []
        self._flow = FrameFlow(enabled="frame_ack" in features)
        self._quality_mode = "balanced"

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
        flow = self._flow
        adapt = QualityController(
            "h264" if use_h264 else "jpeg",
            self.args.h264_crf if use_h264 else self.args.quality,
            self._quality_mode)
        flow.max_inflight = adapt.max_inflight
        active_mode = adapt.mode
        encode_params = _jpeg_encode_params(adapt.value)
        if flow.enabled:
            log.info("Tự chỉnh chất lượng theo đường truyền: bật (chế độ %s)",
                     adapt.mode)

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
                active_level = adapt.level
                frame_w, frame_h = frame_size(cap_w, cap_h, active_max_width,
                                              adapt.scale, use_h264)
                if flow.enabled:
                    self._send_json(sock, protocol.PKT_INFO, adapt.info())

                if use_h264:
                    h264_enc = H264Encoder(frame_w, frame_h, self.args.fps,
                                           adapt.value)
                    last_recreate = time.time()
                    self._codec_generation += 1
                    self._send_json(
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

                    # Chế độ hình ảnh client chọn + đánh giá đường truyền.
                    mono = time.monotonic()
                    if self._quality_mode != active_mode:
                        active_mode = self._quality_mode
                        adapt.set_mode(active_mode)
                        flow.max_inflight = adapt.max_inflight
                    if flow.enabled:
                        adapt.tick(mono, self.args.fps)
                    if not flow.can_send(mono):
                        # Client chưa nhận xong frame trước → chờ, rồi chụp
                        # frame MỚI NHẤT thay vì dồn frame cũ vào buffer.
                        time.sleep(0.004)
                        adapt.note_blocked(time.monotonic() - mono)
                        continue
                    if adapt.level != active_level:
                        active_level = adapt.level
                        encode_params = _jpeg_encode_params(adapt.value)
                        last_jpeg = b""
                        force_send = True
                        log.info("Chất lượng tự động: bậc %d/%d (%s %d, %.0f%% kích thước)",
                                 adapt.level + 1, len(adapt.levels),
                                 "CRF" if use_h264 else "JPEG q", adapt.value,
                                 adapt.scale * 100)
                        self._send_json(sock, protocol.PKT_INFO, adapt.info())

                    t0 = now
                    self._poll_cursor(sock)
                    shot = sct.grab(monitor)
                    # View BGRA trên buffer của mss — không copy.
                    frame = np.asarray(shot)

                    # Client đổi monitor / độ phân giải giữa chừng?
                    monitor_changed = self._monitor_index != active_monitor
                    want_w, want_h = frame_size(
                        frame.shape[1], frame.shape[0], self._max_width,
                        adapt.scale, use_h264)
                    h264_crf_changed = (
                        use_h264 and h264_enc is not None
                        and getattr(h264_enc, "crf", adapt.value) != adapt.value)
                    if (monitor_changed
                            or self._max_width != active_max_width
                            or (want_w, want_h) != (frame_w, frame_h)
                            or h264_crf_changed):
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
                        frame_w, frame_h = frame_size(
                            cap_w, cap_h, active_max_width, adapt.scale,
                            use_h264)
                        if use_h264:
                            if h264_enc:
                                h264_enc.close()
                            h264_enc = H264Encoder(frame_w, frame_h,
                                                   self.args.fps,
                                                   adapt.value)
                            last_recreate = time.time()
                            self._codec_generation += 1
                            self._send_json(
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
                            self._send(
                                sock, protocol.PKT_FRAME_H264, data)
                            flow.on_sent(len(data), time.monotonic())
                            adapt.note_frame()
                        sent_w, sent_h = frame_w, frame_h
                    else:
                        # Không thay đổi (heartbeat) thì gửi lại JPEG đã encode,
                        # khỏi cvtColor + imencode lại.
                        if changed or not last_jpeg:
                            img = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                            if img.shape[1] != frame_w:
                                img = cv2.resize(
                                    img, (frame_w, frame_h),
                                    interpolation=cv2.INTER_AREA)
                            ok, buf = cv2.imencode(".jpg", img, encode_params)
                            if not ok:
                                log.error("cv2.imencode thất bại")
                                skip_until = now + frame_budget
                                continue
                            last_jpeg = buf.tobytes()
                            sent_w, sent_h = img.shape[1], img.shape[0]
                        self._send(
                            sock, protocol.PKT_FRAME, last_jpeg)
                        flow.on_sent(len(last_jpeg), time.monotonic())
                        adapt.note_frame()

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
        clipboard_poll_interval = 0
        try:
            sock.setblocking(True)
            while not client_stop.is_set() and not self.stop_event.is_set():
                self._flush_clipboard_send(sock)
                clipboard_poll_interval += 1
                if clipboard_poll_interval >= 4:
                    clipboard_poll_interval = 0
                    self._poll_host_clipboard(sock)
                self._pump_files(sock)

                # Đang gửi file → vòng nhanh để bơm chunk tiếp theo.
                wait = 0.0 if self._outgoing.busy else 0.5
                ready, _, _ = select.select([sock], [], [], wait)
                if not ready:
                    continue

                ptype, payload = protocol.recv_packet(sock)
                if ptype == protocol.PKT_FILE_DATA:
                    self._on_file_data(sock, payload)
                    continue
                if ptype != protocol.PKT_CONTROL:
                    continue
                try:
                    event = protocol.decode_json(payload)
                except Exception:
                    continue
                try:
                    self._handle_control_event(sock, event)
                except Exception as exc:
                    log.warning("Bỏ qua control event lỗi: %s", exc)
        except (ConnectionError, OSError, struct.error) as exc:
            log.info("Control loop dừng: %s", exc)
        except Exception as exc:
            log.error("Control loop lỗi: %s", exc)
        finally:
            client_stop.set()
            self._release_pressed_keys()
            self._abort_transfers()

    def _handle_control_event(self, sock: socket.socket, event: dict) -> None:
        kind = event.get("event")
        if kind == "ping":
            self._send_json(sock, protocol.PKT_CONTROL,
                            {"event": "pong", "t": event.get("t")})
            return
        if kind == "frame_ack":
            self._flow.on_ack(event.get("n"), time.monotonic())
            return
        if kind == "set_quality_mode":
            mode = str(event.get("mode", ""))
            if mode in QUALITY_MODES and mode != self._quality_mode:
                self._quality_mode = mode
                log.info("Client chọn chế độ hình ảnh: %s", mode)
            return
        if kind in ("file_begin", "file_end", "file_cancel", "file_result"):
            self._on_file_event(sock, event)
            return
        if kind in _INPUT_EVENTS and not self._perms["control"]:
            if not self._view_only_logged:
                log.info("Phiên chỉ xem: bỏ qua điều khiển từ client.")
                self._view_only_logged = True
            return
        if kind == "clipboard" and not self._perms["clipboard"]:
            return
        self._apply_event(event)

    # ---- File transfer -------------------------------------------------

    def _file_result(self, sock: socket.socket, transfer_id, ok: bool,
                     name: str = "", error: str = "") -> None:
        event = {"event": "file_result", "id": transfer_id, "ok": ok,
                 "name": name}
        if error:
            event["error"] = error
        self._send_json(sock, protocol.PKT_CONTROL, event)

    def _on_file_event(self, sock: socket.socket, event: dict) -> None:
        kind = event.get("event")
        transfer_id = event.get("id")
        if kind == "file_result":
            # Client báo kết quả nhận file host gửi.
            ok = bool(event.get("ok"))
            detail = event.get("error", "") if not ok else ""
            name = str(event.get("name", ""))
            log.info("Gửi file %s: %s", name, "xong" if ok else detail)
            self._notify(self.on_file_done, True, name, ok, detail)
            return
        if not self._perms["files"]:
            if kind == "file_begin":
                self._file_result(sock, transfer_id, False,
                                  str(event.get("name", "")),
                                  "host không cho phép truyền file")
            return
        if kind == "file_begin":
            try:
                name = self._incoming.begin(event)
            except filetransfer.TransferError as exc:
                self._file_result(sock, transfer_id, False,
                                  str(event.get("name", "")), str(exc))
                return
            log.info("Nhận file từ client: %s (%s byte)", name,
                     event.get("size"))
            self._notify(self.on_file_progress, False, name, 0,
                         int(event.get("size", 0) or 0))
        elif kind == "file_end":
            name = self._incoming.name_of(int(transfer_id or 0))
            try:
                path = self._incoming.end(event)
            except filetransfer.TransferError as exc:
                self._file_result(sock, transfer_id, False, name, str(exc))
                self._notify(self.on_file_done, False, name, False, str(exc))
                return
            log.info("Đã nhận file: %s", path)
            self._file_result(sock, transfer_id, True, path.name)
            self._notify(self.on_file_done, False, path.name, True, str(path))
        elif kind == "file_cancel":
            name = self._incoming.name_of(int(transfer_id or 0))
            self._incoming.cancel(int(transfer_id or 0))
            if name:
                self._notify(self.on_file_done, False, name, False,
                             "client đã huỷ")

    def _on_file_data(self, sock: socket.socket, payload: bytes) -> None:
        if not self._perms["files"]:
            return
        try:
            transfer_id, name, done, total = self._incoming.data(payload)
        except filetransfer.TransferError as exc:
            log.warning("Nhận file lỗi: %s", exc)
            try:
                transfer_id, _ = filetransfer.unpack_data(payload)
            except filetransfer.TransferError:
                return
            self._file_result(sock, transfer_id, False, "", str(exc))
            return
        self._notify(self.on_file_progress, False, name, done, total)

    def _pump_files(self, sock: socket.socket) -> None:
        while True:
            try:
                path = self._file_queue.get_nowait()
            except queue.Empty:
                break
            if self._perms["files"]:
                self._outgoing.add(path)
            else:
                self._notify(self.on_file_done, True, os.path.basename(path),
                             False, "phiên này không cho phép truyền file")
        if not self._outgoing.busy:
            return
        self._outgoing.pump(
            lambda ev: self._send_json(sock, protocol.PKT_CONTROL, ev),
            lambda data: self._send(sock, protocol.PKT_FILE_DATA, data),
            on_progress=lambda name, done, total: self._notify(
                self.on_file_progress, True, name, done, total),
            on_error=lambda name, err: self._notify(
                self.on_file_done, True, name, False, err))

    def _abort_transfers(self) -> None:
        self._incoming.abort_all()
        current = self._outgoing.current
        if current is not None:
            self._notify(self.on_file_done, True, current.name, False,
                         "mất kết nối")
        self._outgoing.cancel_all()
        while True:
            try:
                self._file_queue.get_nowait()
            except queue.Empty:
                break

    # ---- Cursor ----------------------------------------------------------

    def _poll_cursor(self, sock: socket.socket) -> None:
        if not self._cursor_source_ready:
            self._cursor_source_ready = True
            self._cursor_source = make_cursor_source()
            self._last_cursor_pos = None
        source = self._cursor_source
        if source is None:
            return
        try:
            result = source.poll()
        except Exception as exc:
            log.info("Tắt theo dõi con trỏ: %s", exc)
            self._cursor_source = None
            return
        if result is None:
            return
        x, y, shape = result
        if shape is not None:
            self._send_json(sock, protocol.PKT_CONTROL, shape)
        if (x, y) == self._last_cursor_pos:
            return
        self._last_cursor_pos = (x, y)
        left, top, width, height = self._monitor_rect()
        if width <= 0 or height <= 0:
            return
        self._send_json(sock, protocol.PKT_CONTROL, {
            "event": "cursor_pos",
            "x": round((x - left) / width, 5),
            "y": round((y - top) / height, 5)})

    def _flush_clipboard_send(self, sock: socket.socket) -> None:
        while True:
            try:
                text = self._clipboard_send_queue.get_nowait()
            except queue.Empty:
                return
            if not self._perms["clipboard"]:
                continue
            try:
                self._send_json(
                    sock, protocol.PKT_CONTROL,
                    {"event": "clipboard", "text": text, "source": "host"})
            except OSError:
                return

    def _poll_host_clipboard(self, sock: socket.socket) -> None:
        if not self._perms["clipboard"]:
            return
        if self._clipboard_skip > 0:
            self._clipboard_skip -= 1
            return
        text = self.clipboard_get()
        if text and text != self._last_clipboard:
            self._last_clipboard = text
            try:
                self._send_json(
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
            btn = {"right": Button.right, "middle": Button.middle}.get(
                event.get("button"), Button.left)
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
                self._pressed_keys.add(key_str)
            else:
                kbd.release(key_str)
                self._pressed_keys.discard(key_str)
        except Exception as exc:
            log.warning("Keyboard inject error (%s): %s", key_str, exc)

    def _release_pressed_keys(self) -> None:
        """Nhả mọi phím client còn giữ khi phiên kết thúc (chống kẹt phím)."""
        keys = sorted(self._pressed_keys)
        self._pressed_keys.clear()
        kbd = self._keyboard or None
        if kbd is None:
            return
        for key in keys:
            try:
                kbd.release(key)
            except Exception as exc:
                log.debug("Không nhả được phím %r: %s", key, exc)

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
