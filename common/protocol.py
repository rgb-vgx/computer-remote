"""Wire protocol cho remote desktop MVP.

Định dạng packet (length-prefix để tránh dính/đứt packet trên TCP stream):

    +------------------+-------------+----------------------+
    | payload_len (4B) | type (1B)   | payload (payload_len)|
    |   uint32 BE      | uint8       |   bytes              |
    +------------------+-------------+----------------------+

Header = 5 bytes. payload_len là độ dài của phần payload theo sau.

Packet types:
    1 = frame JPEG (binary)
    2 = control event (JSON)
    3 = hello / auth (JSON)
    4 = info / error (JSON)
    5 = frame H.264 (binary)
    6 = dữ liệu file (id uint32 BE + bytes; xem common/filetransfer.py)

Toàn bộ lỗi socket (timeout, đứt kết nối) được để "bong" lên cho caller
xử lý (đóng socket, log). Module này không nuốt lỗi im lặng.
"""

from __future__ import annotations

import json
import socket
import struct

# Header: unsigned int (big-endian) cho payload_len + 1 byte packet type.
HEADER = struct.Struct(">IB")
HEADER_SIZE = HEADER.size  # = 5

# Packet types.
PKT_FRAME = 1   # JPEG frame
PKT_CONTROL = 2  # control event (mouse/keyboard/clipboard JSON)
PKT_HELLO = 3    # auth hello (JSON)
PKT_INFO = 4     # info/error (JSON)
PKT_FRAME_H264 = 5  # H.264 frame
PKT_FILE_DATA = 6   # chunk dữ liệu file (truyền file hai chiều)

# Guard: từ chối payload_len bất thường (lỗi đồng bộ hoặc dữ liệu độc) để
# không cấp phát bộ nhớ khổng lồ. 64 MiB dư sức cho 1 frame JPEG.
MAX_PAYLOAD = 64 * 1024 * 1024


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Đọc đúng ``n`` byte từ socket, ghép các lần recv lại.

    TCP có thể trả về ít hơn số byte yêu cầu, nên phải lặp tới khi đủ.
    Raise ConnectionError nếu peer đóng kết nối giữa chừng.
    """
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Kết nối đóng trong khi đang đọc dữ liệu")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_packet(sock: socket.socket, ptype: int, payload: bytes) -> None:
    """Gửi 1 packet (header + payload). ``payload`` phải là bytes.

    Dùng sendall để đảm bảo toàn bộ dữ liệu được gửi đi.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload phải là bytes; dùng send_json cho dict/JSON")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload quá lớn: {len(payload)} > {MAX_PAYLOAD}")
    header = HEADER.pack(len(payload), ptype)
    sock.sendall(header + payload)


def send_json(sock: socket.socket, ptype: int, obj: dict) -> None:
    """Tiện ích: serialize dict thành JSON UTF-8 rồi gửi như 1 packet."""
    payload = json.dumps(obj).encode("utf-8")
    send_packet(sock, ptype, payload)


def recv_packet(sock: socket.socket) -> tuple[int, bytes]:
    """Đọc 1 packet, trả về ``(packet_type, payload_bytes)``.

    Xử lý partial read qua recv_exact. Raise ConnectionError nếu peer đóng,
    ValueError nếu payload_len vượt MAX_PAYLOAD.
    """
    header = recv_exact(sock, HEADER_SIZE)
    payload_len, ptype = HEADER.unpack(header)
    if payload_len > MAX_PAYLOAD:
        raise ValueError(f"payload_len bất thường: {payload_len} (sai đồng bộ?)")
    payload = recv_exact(sock, payload_len) if payload_len else b""
    return ptype, payload


def decode_json(payload: bytes) -> dict:
    """Giải mã payload JSON UTF-8 thành dict."""
    return json.loads(payload.decode("utf-8"))
