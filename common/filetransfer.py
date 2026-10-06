"""common/filetransfer.py — Truyền file hai chiều trên kết nối remote sẵn có.

Luồng (giống nhau cho client → host và host → client):

    control {"event": "file_begin", "id", "name", "size"}
    PKT_FILE_DATA  payload = id (uint32 BE) + bytes   (lặp lại, mỗi chunk ≤ CHUNK_SIZE)
    control {"event": "file_end", "id"}               (hoặc "file_cancel")
    ← control {"event": "file_result", "id", "ok", "name", "error"?}

Bên nhận ghi vào ``<tên>.part`` rồi đổi tên khi đủ byte; tên file được làm
sạch (chỉ lấy basename) để bên gửi không ghi được ra ngoài thư mục nhận.
Module này không đụng tới socket — caller tự gửi event/packet nó trả ra.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

CHUNK_SIZE = 256 * 1024
DATA_HEADER = struct.Struct(">I")

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WIN = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


class TransferError(Exception):
    """Lỗi truyền file (tên file xấu, sai kích thước, lỗi ghi đĩa...)."""


def receive_dir() -> Path:
    """Thư mục nhận file: ~/Downloads/RemoteDesktop (tạo nếu chưa có)."""
    path = Path.home() / "Downloads" / "RemoteDesktop"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(name: str) -> str:
    """Chỉ giữ basename hợp lệ trên cả Windows lẫn Linux."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = _FORBIDDEN.sub("_", base).strip().strip(".")
    if not base:
        raise TransferError("tên file không hợp lệ")
    stem = base.split(".")[0].upper()
    if stem in _RESERVED_WIN:
        base = "_" + base
    if len(base) > 200:
        root, ext = os.path.splitext(base)
        base = root[:200 - len(ext)] + ext
    return base


def unique_path(directory: Path, name: str) -> Path:
    """``name`` nếu chưa có, ngược lại ``name (1).ext``, ``name (2).ext``..."""
    candidate = directory / name
    root, ext = os.path.splitext(name)
    index = 1
    while candidate.exists() or candidate.with_name(candidate.name + ".part").exists():
        candidate = directory / f"{root} ({index}){ext}"
        index += 1
    return candidate


def pack_data(transfer_id: int, chunk: bytes) -> bytes:
    return DATA_HEADER.pack(transfer_id) + chunk


def unpack_data(payload: bytes) -> tuple[int, bytes]:
    if len(payload) < DATA_HEADER.size:
        raise TransferError("packet dữ liệu file quá ngắn")
    (transfer_id,) = DATA_HEADER.unpack_from(payload)
    return transfer_id, payload[DATA_HEADER.size:]


# ---------------------------------------------------------------------------
# Gửi
# ---------------------------------------------------------------------------

class OutgoingFile:
    """Một file đang gửi: sinh event begin, các chunk dữ liệu, event end."""

    def __init__(self, path: str | os.PathLike, transfer_id: int) -> None:
        self.path = Path(path)
        self.id = transfer_id
        self.name = self.path.name
        self.size = self.path.stat().st_size
        self.sent = 0
        self._fh = open(self.path, "rb")

    def begin_event(self) -> dict:
        return {"event": "file_begin", "id": self.id, "name": self.name,
                "size": self.size}

    def end_event(self) -> dict:
        return {"event": "file_end", "id": self.id}

    def cancel_event(self) -> dict:
        return {"event": "file_cancel", "id": self.id}

    def next_packet(self) -> bytes | None:
        """Payload PKT_FILE_DATA kế tiếp; None khi đã đọc hết file."""
        chunk = self._fh.read(CHUNK_SIZE)
        if not chunk:
            return None
        self.sent += len(chunk)
        return pack_data(self.id, chunk)

    @property
    def done(self) -> bool:
        return self.sent >= self.size

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


class SendQueue:
    """Hàng đợi file cần gửi, gửi lần lượt từng file.

    ``pump(send_json, send_data, budget)`` gửi tối đa ``budget`` byte rồi trả
    quyền, để vòng lặp của caller vẫn kịp nhận frame/control xen giữa.
    """

    def __init__(self) -> None:
        self._pending: list[Path] = []
        self._next_id = 1
        self.current: OutgoingFile | None = None

    def add(self, path: str | os.PathLike) -> None:
        self._pending.append(Path(path))

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self._pending)

    def pump(self, send_json, send_data, budget: int = 4 * CHUNK_SIZE,
             on_progress=None, on_error=None) -> None:
        sent = 0
        while sent < budget:
            if self.current is None:
                if not self._pending:
                    return
                path = self._pending.pop(0)
                try:
                    self.current = OutgoingFile(path, self._next_id)
                except OSError as exc:
                    if on_error:
                        on_error(path.name, f"không đọc được file: {exc}")
                    continue
                self._next_id += 1
                send_json(self.current.begin_event())
                if on_progress:
                    on_progress(self.current.name, 0, self.current.size)
            packet = self.current.next_packet()
            if packet is None:
                send_json(self.current.end_event())
                self.current.close()
                self.current = None
                continue
            send_data(packet)
            sent += len(packet)
            if on_progress:
                on_progress(self.current.name, self.current.sent,
                            self.current.size)

    def cancel_all(self) -> None:
        self._pending.clear()
        if self.current is not None:
            self.current.close()
            self.current = None


# ---------------------------------------------------------------------------
# Nhận
# ---------------------------------------------------------------------------

@dataclass
class _Incoming:
    name: str
    size: int
    final: Path
    part: Path
    fh: object
    received: int = 0


@dataclass
class IncomingFiles:
    """Quản lý các file đang nhận theo id. ``directory`` lấy lười khi cần."""

    directory_factory: object = receive_dir
    _files: dict[int, _Incoming] = field(default_factory=dict)

    def begin(self, event: dict) -> str:
        try:
            transfer_id = int(event["id"])
            size = int(event.get("size", 0))
        except (KeyError, TypeError, ValueError):
            raise TransferError("file_begin thiếu id/size") from None
        if size < 0:
            raise TransferError("kích thước file không hợp lệ")
        name = safe_name(event.get("name", ""))
        self.cancel(transfer_id)
        directory = Path(self.directory_factory())
        final = unique_path(directory, name)
        part = final.with_name(final.name + ".part")
        try:
            fh = open(part, "wb")
        except OSError as exc:
            raise TransferError(f"không tạo được file: {exc}") from None
        self._files[transfer_id] = _Incoming(final.name, size, final, part, fh)
        return final.name

    def data(self, payload: bytes) -> tuple[int, str, int, int]:
        """Ghi chunk; trả (id, tên, đã nhận, tổng)."""
        transfer_id, chunk = unpack_data(payload)
        item = self._files.get(transfer_id)
        if item is None:
            raise TransferError(f"dữ liệu cho file không tồn tại (id {transfer_id})")
        if item.received + len(chunk) > item.size:
            self.cancel(transfer_id)
            raise TransferError(f"{item.name}: nhận nhiều byte hơn khai báo")
        try:
            item.fh.write(chunk)
        except OSError as exc:
            self.cancel(transfer_id)
            raise TransferError(f"{item.name}: lỗi ghi đĩa: {exc}") from None
        item.received += len(chunk)
        return transfer_id, item.name, item.received, item.size

    def end(self, event: dict) -> Path:
        transfer_id = int(event.get("id", -1))
        item = self._files.pop(transfer_id, None)
        if item is None:
            raise TransferError(f"file_end cho file không tồn tại (id {transfer_id})")
        item.fh.close()
        if item.received != item.size:
            item.part.unlink(missing_ok=True)
            raise TransferError(
                f"{item.name}: thiếu dữ liệu ({item.received}/{item.size} byte)")
        os.replace(item.part, item.final)
        return item.final

    def name_of(self, transfer_id: int) -> str:
        item = self._files.get(transfer_id)
        return item.name if item else ""

    def cancel(self, transfer_id: int) -> None:
        item = self._files.pop(transfer_id, None)
        if item is None:
            return
        try:
            item.fh.close()
        except OSError:
            pass
        item.part.unlink(missing_ok=True)

    def abort_all(self) -> None:
        for transfer_id in list(self._files):
            self.cancel(transfer_id)
