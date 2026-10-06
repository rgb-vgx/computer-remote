"""End-to-end: HostServer thật (màn hình giả) ↔ NetworkWorker thật qua TCP."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from mss.screenshot import ScreenShot

import client
import host
from common import filetransfer as ft

W, H = 320, 180


class FakeScreen:
    monitors = [None, {"left": 0, "top": 0, "width": W, "height": H}]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def grab(self, monitor):
        arr = np.full((H, W, 4), 90, np.uint8)
        return ScreenShot(bytearray(arr.tobytes()), monitor)


def wait_for(pred, timeout=8.0):
    """Chờ điều kiện; xử lý event Qt vì signal từ thread mạng là queued."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def running_host(monkeypatch, tmp_path):
    monkeypatch.setattr(host.mss, "MSS", lambda *a, **k: FakeScreen())
    monkeypatch.setattr(host, "list_monitors", lambda: [
        {"index": 1, "width": W, "height": H, "left": 0, "top": 0, "primary": True}])
    args = SimpleNamespace(bind="127.0.0.1", port=0, token="tok", fps=20,
                           quality=70, max_width=1920, codec="jpeg",
                           h264_crf=18, view_only=False, debug=False)
    server = host.HostServer(args)
    inbox = tmp_path / "host_inbox"
    inbox.mkdir()
    server._incoming = ft.IncomingFiles(directory_factory=lambda: inbox)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert wait_for(lambda: server.server_sock is not None
                    and server.server_sock.getsockname()[1] != 0)
    port = server.server_sock.getsockname()[1]
    yield server, port, inbox
    server.shutdown()
    thread.join(timeout=5)


def start_worker(port, token="tok"):
    worker = client.NetworkWorker("127.0.0.1", port, token)
    got = {"frames": 0, "perms": None, "pong": [], "done": [],
           "rejected": None, "connected": False}
    worker.frame_ready.connect(lambda img: got.__setitem__("frames", got["frames"] + 1))
    worker.permissions_ready.connect(lambda p: got.__setitem__("perms", p))
    worker.pong.connect(got["pong"].append)
    worker.file_done.connect(lambda *a: got["done"].append(a))
    worker.rejected.connect(lambda r: got.__setitem__("rejected", r))
    worker.connected.connect(lambda: got.__setitem__("connected", True))
    # Chạy run() trên thread Python thường: signal gọi thẳng các lambda trên.
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    return worker, thread, got


def test_session_frames_ping_and_file_upload(qapp, running_host, tmp_path):
    server, port, inbox = running_host
    worker, thread, got = start_worker(port)
    try:
        assert wait_for(lambda: got["connected"] and got["frames"] >= 2)
        assert got["perms"]["files"] and got["perms"]["control"]

        worker.send_control({"event": "ping", "t": time.monotonic() * 1000.0})
        assert wait_for(lambda: got["pong"])
        assert 0 <= got["pong"][0] < 1000

        src = tmp_path / "upload.bin"
        data = np.random.default_rng(1).bytes(ft.CHUNK_SIZE * 3 + 123)
        src.write_bytes(data)
        worker.send_file(str(src))
        assert wait_for(lambda: got["done"])
        assert got["done"][0] == (True, "upload.bin", True, "")
        assert (inbox / "upload.bin").read_bytes() == data
        assert worker.bytes_received > 0
    finally:
        worker.stop()
        thread.join(timeout=5)
