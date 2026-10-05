import socket
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

import host
from common import protocol


def make_server(**overrides):
    values = dict(bind="127.0.0.1", port=0, token="secret1", fps=15,
                  quality=75, max_width=1920, codec="jpeg", h264_crf=18,
                  view_only=False, debug=False)
    values.update(overrides)
    return host.HostServer(SimpleNamespace(**values))


def run_auth(server, addr, token, timeout=3.0):
    """Chạy _handle_client với HELLO token cho trước, trả về packet đầu tiên."""
    a, b = socket.socketpair()
    thread = threading.Thread(target=server._handle_client, args=(a, addr),
                              daemon=True)
    thread.start()
    protocol.send_json(b, protocol.PKT_HELLO, {"token": token})
    ptype, payload = protocol.recv_packet(b)
    thread.join(timeout=timeout)
    a.close()
    b.close()
    return protocol.decode_json(payload), thread.is_alive()


def test_token_matches():
    assert host._token_matches("abc", "abc")
    assert not host._token_matches("abc", "abd")
    assert not host._token_matches(123, "abc")
    assert not host._token_matches(None, "abc")


def test_auth_fail_then_block():
    server = make_server()
    addr = ("10.0.0.9", 4000)
    for i in range(host._AUTH_MAX_FAILURES):
        info, alive = run_auth(server, (addr[0], addr[1] + i), "sai")
        assert info["type"] == "error" and info["message"] == "auth failed"
        assert not alive
    # Sai tiếp lần nữa → bị khóa tạm
    info, alive = run_auth(server, (addr[0], 4999), "sai")
    assert "too many attempts" in info["message"]
    assert not alive


def test_auth_success_resets_failures(monkeypatch):
    server = make_server()
    addr = ("10.0.0.8", 5000)
    run_auth(server, addr, "sai")
    assert server._auth_failures.get(addr[0]) == 1

    def idle_loop(sock, stop):
        while not stop.is_set():
            time.sleep(0.05)

    monkeypatch.setattr(server, "_capture_loop", idle_loop)
    monkeypatch.setattr(server, "_control_loop", idle_loop)
    a, b = socket.socketpair()
    thread = threading.Thread(target=server._handle_client, args=(a, addr),
                              daemon=True)
    thread.start()
    protocol.send_json(b, protocol.PKT_HELLO, {"token": "secret1"})
    ptype, payload = protocol.recv_packet(b)
    info = protocol.decode_json(payload)
    assert info["type"] == "info" and info["message"] == "auth ok"
    assert addr[0] not in server._auth_failures
    server.disconnect_client()
    thread.join(timeout=3)
    assert not thread.is_alive()
    a.close()
    b.close()


def test_disconnect_client_sends_error():
    server = make_server()
    a, b = socket.socketpair()
    stop = threading.Event()
    server._client_sock = a
    server._client_stop = stop
    server.disconnect_client()
    assert stop.is_set()
    ptype, payload = protocol.recv_packet(b)
    info = protocol.decode_json(payload)
    assert ptype == protocol.PKT_INFO
    assert info["type"] == "error"
    assert "ngắt kết nối" in info["message"]
    a.close()
    b.close()


def test_notify_guard():
    server = make_server()
    calls = []

    def bad():
        raise RuntimeError("boom")

    server._notify(bad)          # không được raise
    server._notify(None)         # None = bỏ qua
    server._notify(lambda: calls.append(1))
    assert calls == [1]


def test_detect_display_non_linux(monkeypatch):
    monkeypatch.setattr(host.sys, "platform", "win32")
    assert host.detect_display() is True


def test_clipboard_backend_windows(monkeypatch):
    monkeypatch.setattr(host.sys, "platform", "win32")
    server = make_server()
    assert server.clipboard_get is host._clipboard_get_windows
    assert server.clipboard_set is host._clipboard_set_windows


def test_clipboard_backend_linux(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("nhánh xclip chỉ có trên Linux")
    monkeypatch.setattr(host.sys, "platform", "linux")
    server = make_server()
    assert server.clipboard_get is host._clipboard_get_xclip
    assert server.clipboard_set is host._clipboard_set_xclip


def test_pynput_keyboard_key_mapping(monkeypatch):
    fake_kb = types.ModuleType("pynput.keyboard")

    class Key:
        enter = "ENTER"
        esc = "ESC"

    fake_kb.Key = Key
    fake_pkg = types.ModuleType("pynput")
    fake_pkg.keyboard = fake_kb
    monkeypatch.setitem(sys.modules, "pynput", fake_pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", fake_kb)

    assert host.PynputKeyboard._key("Key.enter") == "ENTER"
    assert host.PynputKeyboard._key("Key.return") == "ENTER"
    assert host.PynputKeyboard._key("a") == "a"
    with pytest.raises(AttributeError):
        host.PynputKeyboard._key("Key.khong_ton_tai")


def test_key_to_keysym():
    assert host.key_to_keysym("a") == 0x61
    assert host.key_to_keysym("é") == 0xE9
    assert host.key_to_keysym("ế") == 0x01000000 + 0x1EBF
    assert host.key_to_keysym("中") == 0x01000000 + 0x4E2D
    assert host.key_to_keysym("Key.enter") == 0xFF0D
    assert host.key_to_keysym("Key.return") == 0xFF0D
    assert host.key_to_keysym("ab") is None
    assert host.key_to_keysym("Key.khong_ton_tai") is None


def _fake_pynput(monkeypatch, controller_cls):
    fake_kb = types.ModuleType("pynput.keyboard")
    fake_kb.Controller = controller_cls

    class Key:
        enter = "ENTER"

    fake_kb.Key = Key
    fake_pkg = types.ModuleType("pynput")
    fake_pkg.keyboard = fake_kb
    monkeypatch.setitem(sys.modules, "pynput", fake_pkg)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", fake_kb)


def test_pynput_keyboard_unicode_fallback(monkeypatch):
    calls = []

    class FakeController:
        def press(self, key):
            if key == "ế":
                raise ValueError("không có trong layout")
            calls.append(("press", key))

        def release(self, key):
            calls.append(("release", key))

    _fake_pynput(monkeypatch, FakeController)
    monkeypatch.setattr(host, "_send_unicode_windows",
                        lambda ch: calls.append(("unicode", ch)) or True)

    kbd = host.PynputKeyboard()
    kbd.press("a")
    kbd.release("a")
    kbd.press("ế")
    kbd.release("ế")  # Unicode press đã gửi cả down+up
    assert calls == [("press", "a"), ("release", "a"), ("unicode", "ế")]


def test_pynput_keyboard_unicode_send_fail(monkeypatch):
    calls = []

    class FakeController:
        def press(self, key):
            raise ValueError("không có trong layout")

        def release(self, key):
            calls.append(("release", key))

    _fake_pynput(monkeypatch, FakeController)
    monkeypatch.setattr(host, "_send_unicode_windows", lambda ch: False)

    kbd = host.PynputKeyboard()
    kbd.press("ế")      # Unicode gửi thất bại → cảnh báo, không crash
    kbd.release("ế")
    assert calls == [("release", "ế")]


class FakeMSS:
    def __init__(self, monitors):
        self.monitors = monitors

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


MONITORS = [
    {"left": 0, "top": 0, "width": 3840, "height": 1080},
    {"left": 0, "top": 0, "width": 1920, "height": 1080},
    {"left": 1920, "top": 0, "width": 1280, "height": 720},
]


def _fake_mss(monkeypatch, monitors=MONITORS):
    monkeypatch.setattr(host.mss, "MSS", lambda: FakeMSS(monitors))


def test_list_monitors(monkeypatch):
    _fake_mss(monkeypatch)
    monitors = host.list_monitors()
    assert [m["index"] for m in monitors] == [1, 2]
    assert monitors[0]["primary"] is True
    assert monitors[1]["primary"] is False
    assert monitors[1]["left"] == 1920


def test_apply_monitor(monkeypatch):
    server = make_server()
    server._apply_monitor({"index": 2})
    assert server._monitor_index == 2
    server._apply_monitor({"index": "sai"})
    assert server._monitor_index == 2
    server._apply_monitor({"index": 0})   # clamp về 1
    assert server._monitor_index == 1


def test_monitor_rect_selected_monitor(monkeypatch):
    _fake_mss(monkeypatch)
    server = make_server()
    server._monitor_index = 2
    assert server._monitor_rect() == (1920, 0, 1280, 720)
    server._monitor_index = 1
    assert server._monitor_rect() == (0, 0, 1920, 1080)


def test_mouse_mapping_uses_monitor_offset(monkeypatch):
    server = make_server()
    positions = []

    class FakeMouse:
        @property
        def position(self):
            return None

        @position.setter
        def position(self, value):
            positions.append(value)

    # pynput.mouse cần X connection khi import trên Linux → thay bằng fake.
    fake_mouse = types.ModuleType("pynput.mouse")

    class Button:
        left = "L"
        right = "R"

    fake_mouse.Button = Button
    fake_pkg = types.ModuleType("pynput")
    fake_pkg.mouse = fake_mouse
    monkeypatch.setitem(sys.modules, "pynput", fake_pkg)
    monkeypatch.setitem(sys.modules, "pynput.mouse", fake_mouse)

    monkeypatch.setattr(server, "_get_mouse", lambda: FakeMouse())
    monkeypatch.setattr(server, "_monitor_rect",
                        lambda: (1920, 0, 1280, 720))
    server._apply_mouse({"event": "mouse_move", "x": 0.5, "y": 0.5})
    assert positions[-1] == (1920 + 640, 360)
