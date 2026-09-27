import socket
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from mss.screenshot import ScreenShot

import client
import host
from common import protocol

W, H = 640, 360


class FakeScreen:
    monitors = [None, {"left": 0, "top": 0, "width": W, "height": H}]
    color = [10, 20, 30, 255]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def grab(self, monitor):
        arr = np.empty((H, W, 4), np.uint8)
        arr[:] = self.color
        return ScreenShot(bytearray(arr.tobytes()), monitor)


@pytest.fixture
def fake_screen(monkeypatch):
    fake = FakeScreen()
    monkeypatch.setattr(host.mss, "MSS", lambda *a, **k: fake)
    return fake


def make_args(codec="jpeg", max_width=W):
    return SimpleNamespace(bind="127.0.0.1", port=0, token="t", fps=15,
                           quality=75, max_width=max_width, codec=codec,
                           view_only=False, debug=False)


def run_capture(fake_screen, codec="jpeg", supports_h264=False,
                duration=2.2, apply_at=None, apply_max_width=None):
    srv = host.HostServer(make_args(codec))
    srv._client_supports_h264 = supports_h264
    a, b = socket.socketpair()
    stop = threading.Event()
    packets = []

    def reader():
        end = time.time() + duration
        while time.time() < end:
            ready, _, _ = __import__("select").select([b], [], [], 0.2)
            if not ready:
                continue
            try:
                packets.append(protocol.recv_packet(b))
            except Exception:
                return

    threading.Thread(target=reader, daemon=True).start()
    thread = threading.Thread(target=srv._capture_loop, args=(a, stop), daemon=True)
    thread.start()
    time.sleep(apply_at if apply_at is not None else duration)
    if apply_at is not None:
        srv._apply_event({"event": "set_resolution", "max_width": apply_max_width})
        time.sleep(duration - apply_at + 0.3)
    stop.set()
    thread.join(timeout=4)
    a.close()
    b.close()
    return packets


def test_jpeg_frames_and_heartbeat(fake_screen):
    packets = run_capture(fake_screen, "jpeg", duration=2.0)
    jpegs = [p for t, p in packets if t == protocol.PKT_FRAME]
    assert len(jpegs) >= 2
    img = cv2.imdecode(np.frombuffer(jpegs[0], np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (H, W)
    assert np.allclose(img.mean(axis=(0, 1)), (10, 20, 30), atol=3)  # BGR của [10,20,30]


def test_jpeg_change_triggers_frame(fake_screen):
    srv = host.HostServer(make_args())
    a, b = socket.socketpair()
    stop = threading.Event()
    packets = []

    def reader():
        end = time.time() + 1.6
        while time.time() < end + 0.3:
            ready, _, _ = __import__("select").select([b], [], [], 0.2)
            if not ready:
                continue
            try:
                packets.append(protocol.recv_packet(b))
            except Exception:
                return

    threading.Thread(target=reader, daemon=True).start()
    thread = threading.Thread(target=srv._capture_loop, args=(a, stop), daemon=True)
    thread.start()
    time.sleep(0.4)
    fake_screen.color = [200, 100, 50, 255]
    time.sleep(1.2)
    stop.set()
    thread.join(timeout=4)
    a.close()
    b.close()
    jpegs = [p for t, p in packets if t == protocol.PKT_FRAME]
    img = cv2.imdecode(np.frombuffer(jpegs[-1], np.uint8), cv2.IMREAD_COLOR)
    assert np.allclose(img.mean(axis=(0, 1)), (200, 100, 50), atol=3)


def test_resolution_change_jpeg(fake_screen):
    packets = run_capture(fake_screen, "jpeg", duration=2.6,
                          apply_at=0.9, apply_max_width=320)
    sizes = []
    for ptype, payload in packets:
        if ptype == protocol.PKT_FRAME:
            img = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            sizes.append(img.shape[:2])
    assert (H, W) in sizes
    assert (180, 320) in sizes   # 640x360 -> max-width 320 giữ tỉ lệ
    assert sizes[-1] == (180, 320)


def test_resolution_clamp():
    srv = host.HostServer(make_args())
    srv._apply_event({"event": "set_resolution", "max_width": 100})
    assert srv._max_width == 160
    srv._apply_event({"event": "set_resolution", "max_width": "rác"})
    assert srv._max_width == 1920
    srv._apply_event({"event": "set_resolution", "max_width": 99999})
    assert srv._max_width == 7680
    srv._apply_event({"event": "set_resolution", "max_width": None})
    assert srv._max_width == W  # quay về --max-width lúc khởi động


requires_ffmpeg = pytest.mark.skipif(
    not (host.H264Encoder.available() and host.H264Encoder.libx264_available()),
    reason="cần ffmpeg + libx264")


@requires_ffmpeg
def test_h264_stream_and_resolution(fake_screen):
    packets = run_capture(fake_screen, "h264", supports_h264=True,
                          duration=3.0, apply_at=1.0, apply_max_width=320)
    infos = [protocol.decode_json(p) for t, p in packets
             if t == protocol.PKT_INFO and protocol.decode_json(p).get("type") == "codec"]
    dims = [(i["width"], i["height"]) for i in infos]
    assert dims[0] == (W, H)
    assert (320, 180) in dims

    dec = None
    decoded = {}
    for ptype, payload in packets:
        if ptype == protocol.PKT_INFO:
            info = protocol.decode_json(payload)
            if info.get("type") == "codec" and info.get("codec") == "h264":
                if dec is not None:
                    dec.close()
                dec = client.H264Decoder(info["width"], info["height"])
        elif ptype == protocol.PKT_FRAME_H264 and dec is not None:
            dec.feed(payload)
            for _ in range(10):
                frame = dec.read_frame()
                if frame is None:
                    time.sleep(0.01)
                    frame = dec.read_frame()
                    if frame is None:
                        break
                key = (frame.shape[1], frame.shape[0])
                decoded[key] = decoded.get(key, 0) + 1
    if dec is not None:
        deadline = time.time() + 3
        while time.time() < deadline:
            frame = dec.read_frame()
            if frame is None:
                time.sleep(0.02)
                continue
            key = (frame.shape[1], frame.shape[0])
            decoded[key] = decoded.get(key, 0) + 1
        dec.close()
    assert decoded.get((320, 180), 0) >= 1, decoded
