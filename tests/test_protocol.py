import socket

import pytest

from common import protocol


@pytest.fixture
def pair():
    a, b = socket.socketpair()
    yield a, b
    a.close()
    b.close()


def test_packet_roundtrip(pair):
    a, b = pair
    protocol.send_json(a, protocol.PKT_CONTROL, {"event": "mouse_move", "x": 0.5})
    ptype, payload = protocol.recv_packet(b)
    assert ptype == protocol.PKT_CONTROL
    assert protocol.decode_json(payload) == {"event": "mouse_move", "x": 0.5}


def test_frames_are_binary(pair):
    a, b = pair
    data = bytes(range(256)) * 40
    protocol.send_packet(a, protocol.PKT_FRAME, data)
    ptype, payload = protocol.recv_packet(b)
    assert ptype == protocol.PKT_FRAME
    assert payload == data


def test_send_packet_rejects_str(pair):
    a, _ = pair
    with pytest.raises(TypeError):
        protocol.send_packet(a, protocol.PKT_FRAME, "không phải bytes")


def test_recv_exact_handles_partial_reads(pair):
    a, b = pair
    a.sendall(b"hello ")
    a.sendall(b"world")
    assert protocol.recv_exact(b, 11) == b"hello world"


def test_oversize_payload_rejected(pair):
    a, b = pair
    header = protocol.HEADER.pack(protocol.MAX_PAYLOAD + 1, protocol.PKT_FRAME)
    a.sendall(header)
    with pytest.raises(ValueError):
        protocol.recv_packet(b)


def test_connection_closed(pair):
    a, b = pair
    a.close()
    with pytest.raises(ConnectionError):
        protocol.recv_exact(b, 1)
