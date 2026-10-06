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


# ---------------------------------------------------------------------------
# Xác nhận kết nối, quyền phiên, ping, truyền file
# ---------------------------------------------------------------------------

def idle_capture(sock, stop):
    while not stop.is_set():
        time.sleep(0.02)


class Session:
    """_handle_client thật (control loop thật, capture giả) trên socketpair."""

    def __init__(self, server, addr=("10.1.1.1", 6000), token="secret1"):
        self.server = server
        server._capture_loop = idle_capture
        self.a, self.b = socket.socketpair()
        self.b.settimeout(5)
        self.thread = threading.Thread(target=server._handle_client,
                                       args=(self.a, addr), daemon=True)
        self.thread.start()
        protocol.send_json(self.b, protocol.PKT_HELLO, {"token": token})

    def recv(self):
        ptype, payload = protocol.recv_packet(self.b)
        if ptype in (protocol.PKT_INFO, protocol.PKT_CONTROL):
            return ptype, protocol.decode_json(payload)
        return ptype, payload

    def recv_until(self, pred):
        while True:
            ptype, obj = self.recv()
            if pred(ptype, obj):
                return obj

    def send(self, event):
        protocol.send_json(self.b, protocol.PKT_CONTROL, event)

    def close(self):
        self.server.disconnect_client()
        self.thread.join(timeout=3)
        self.a.close()
        self.b.close()
        assert not self.thread.is_alive()


def test_view_only_session_blocks_input_but_allows_resolution(monkeypatch):
    server = make_server(view_only=True)
    applied = []
    monkeypatch.setattr(server, "_apply_mouse", lambda e: applied.append(e))
    s = Session(server)
    perms = s.recv_until(lambda t, o: o.get("type") == "permissions")
    assert perms["control"] is False and perms["files"] is True
    s.send({"event": "mouse_move", "x": 0.5, "y": 0.5})
    s.send({"event": "set_resolution", "max_width": 1280})
    s.send({"event": "ping", "t": 123.5})
    pong = s.recv_until(lambda t, o: o.get("event") == "pong")
    assert pong["t"] == 123.5
    assert applied == []
    assert server._max_width == 1280
    s.close()


def test_file_upload_and_download_over_socket(tmp_path, monkeypatch):
    from common import filetransfer as ft

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    server = make_server()
    server._incoming = ft.IncomingFiles(directory_factory=lambda: inbox)
    done = []
    server.on_file_done = lambda *a: done.append(a)
    s = Session(server)
    s.recv_until(lambda t, o: o.get("type") == "permissions")

    # client → host
    payload = b"hello world" * 50000
    s.send({"event": "file_begin", "id": 7, "name": "../evil.txt",
            "size": len(payload)})
    for i in range(0, len(payload), ft.CHUNK_SIZE):
        protocol.send_packet(s.b, protocol.PKT_FILE_DATA,
                             ft.pack_data(7, payload[i:i + ft.CHUNK_SIZE]))
    s.send({"event": "file_end", "id": 7})
    result = s.recv_until(lambda t, o: o.get("event") == "file_result")
    assert result == {"event": "file_result", "id": 7, "ok": True,
                      "name": "evil.txt"}
    assert (inbox / "evil.txt").read_bytes() == payload
    assert done[-1][:3] == (False, "evil.txt", True)

    # host → client
    src = tmp_path / "from_host.txt"
    src.write_bytes(b"abc" * 1000)
    server.send_file(str(src))
    begin = s.recv_until(lambda t, o: isinstance(o, dict)
                         and o.get("event") == "file_begin")
    assert begin["name"] == "from_host.txt" and begin["size"] == 3000
    chunks = b""
    while True:
        ptype, obj = s.recv()
        if ptype == protocol.PKT_FILE_DATA:
            chunks += ft.unpack_data(obj)[1]
        elif isinstance(obj, dict) and obj.get("event") == "file_end":
            break
    assert chunks == b"abc" * 1000
    s.send({"event": "file_result", "id": begin["id"], "ok": True,
            "name": "from_host.txt"})
    deadline = time.time() + 3
    while time.time() < deadline and done[-1][0] is not True:
        time.sleep(0.02)
    assert done[-1] == (True, "from_host.txt", True, "")
    s.close()


def test_files_denied_by_session_permissions(tmp_path, monkeypatch):
    from common import filetransfer as ft

    server = make_server()
    server._incoming = ft.IncomingFiles(directory_factory=lambda: tmp_path)
    monkeypatch.setattr(server, "default_perms", lambda: {
        "control": True, "clipboard": True, "files": False})
    s = Session(server)
    s.recv_until(lambda t, o: o.get("type") == "permissions")
    s.send({"event": "file_begin", "id": 1, "name": "x.bin", "size": 1})
    result = s.recv_until(lambda t, o: o.get("event") == "file_result")
    assert result["ok"] is False and "không cho phép" in result["error"]
    assert not list(tmp_path.iterdir())
    s.close()


def test_mouse_middle_button(monkeypatch):
    server = make_server()
    pressed = []

    class FakeMouse:
        position = (0, 0)

        def press(self, btn):
            pressed.append(btn)

        def release(self, btn):
            pass

    fake_button = SimpleNamespace(left="L", right="R", middle="M")
    monkeypatch.setitem(sys.modules, "pynput.mouse",
                        types.SimpleNamespace(Button=fake_button))
    monkeypatch.setattr(server, "_get_mouse", lambda: FakeMouse())
    monkeypatch.setattr(server, "_monitor_rect", lambda: (0, 0, 100, 100))
    for name in ("left", "right", "middle"):
        server._apply_mouse({"event": "mouse_down", "button": name,
                             "x": 0.5, "y": 0.5})
    assert pressed == ["L", "R", "M"]


def test_cursor_image_event_roundtrip():
    import base64

    import cv2
    import numpy as np

    # 2x1: pixel đỏ đặc + pixel trắng 50% alpha (premultiplied = 0x80808080)
    event = host.cursor_image_event(2, 1, 1, 0, [0xFFFF0000, 0x80808080])
    assert event["event"] == "cursor" and (event["hx"], event["hy"]) == (1, 0)
    img = cv2.imdecode(np.frombuffer(base64.b64decode(event["png"]), np.uint8),
                       cv2.IMREAD_UNCHANGED)
    assert img.shape == (1, 2, 4)
    assert tuple(img[0, 0]) == (0, 0, 255, 255)        # BGRA đỏ
    assert tuple(img[0, 1]) == (255, 255, 255, 128)    # đã bỏ premultiply
    assert host.cursor_image_event(0, 0, 0, 0, []) is None
    assert host.cursor_image_event(2, 2, 0, 0, [1]) is None


def test_poll_cursor_sends_shape_and_position(monkeypatch):
    server = make_server()

    class Source:
        def __init__(self):
            self.results = [(50, 25, {"event": "cursor", "shape": "ibeam"}),
                            (50, 25, None), (75, 50, None)]

        def poll(self):
            return self.results.pop(0)

    source = Source()
    monkeypatch.setattr(host, "make_cursor_source", lambda: source)
    monkeypatch.setattr(server, "_monitor_rect", lambda: (0, 0, 100, 100))
    sent = []
    monkeypatch.setattr(server, "_send_json",
                        lambda sock, ptype, obj, timeout=None: sent.append(obj))
    for _ in range(3):
        server._poll_cursor(None)
    assert sent == [{"event": "cursor", "shape": "ibeam"},
                    {"event": "cursor_pos", "x": 0.5, "y": 0.25},
                    {"event": "cursor_pos", "x": 0.75, "y": 0.5}]
