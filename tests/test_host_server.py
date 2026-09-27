import socket
import threading
import time
from types import SimpleNamespace

import host
from common import protocol


def make_server(**overrides):
    values = dict(bind="127.0.0.1", port=0, token="secret1", fps=15,
                  quality=75, max_width=1920, codec="jpeg",
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
