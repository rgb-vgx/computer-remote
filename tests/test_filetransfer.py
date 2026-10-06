import pytest

from common import filetransfer as ft


def test_safe_name_strips_paths_and_bad_chars():
    assert ft.safe_name("../../etc/passwd") == "passwd"
    assert ft.safe_name(r"C:\Users\a\report.pdf") == "report.pdf"
    assert ft.safe_name('a<b>c:"d|e?.txt') == "a_b_c__d_e_.txt"
    assert ft.safe_name("CON.txt") == "_CON.txt"
    assert len(ft.safe_name("x" * 300 + ".bin")) == 200
    for bad in ("", "..", "/", "  . "):
        with pytest.raises(ft.TransferError):
            ft.safe_name(bad)


def test_unique_path(tmp_path):
    assert ft.unique_path(tmp_path, "a.txt") == tmp_path / "a.txt"
    (tmp_path / "a.txt").write_text("x")
    assert ft.unique_path(tmp_path, "a.txt") == tmp_path / "a (1).txt"
    (tmp_path / "a (1).txt.part").write_text("x")  # đang nhận dở
    assert ft.unique_path(tmp_path, "a.txt") == tmp_path / "a (2).txt"


def roundtrip(src, dest_dir, budget=10 * ft.CHUNK_SIZE):
    """Bơm SendQueue → IncomingFiles như hai đầu socket, trả về file đích."""
    queue = ft.SendQueue()
    incoming = ft.IncomingFiles(directory_factory=lambda: dest_dir)
    results = []

    def send_json(event):
        kind = event["event"]
        if kind == "file_begin":
            incoming.begin(event)
        elif kind == "file_end":
            results.append(incoming.end(event))

    queue.add(src)
    progress = []
    while queue.busy:
        queue.pump(send_json, incoming.data, budget,
                   on_progress=lambda n, d, t: progress.append((d, t)))
    return results, progress


def test_roundtrip_multi_chunk(tmp_path):
    src = tmp_path / "src.bin"
    data = bytes(range(256)) * (ft.CHUNK_SIZE // 256 * 2 + 7)  # > 2 chunk
    src.write_bytes(data)
    dest = tmp_path / "out"
    dest.mkdir()
    results, progress = roundtrip(src, dest, budget=ft.CHUNK_SIZE)
    assert results == [dest / "src.bin"]
    assert (dest / "src.bin").read_bytes() == data
    assert progress[0] == (0, len(data))
    assert progress[-1] == (len(data), len(data))
    assert not list(dest.glob("*.part"))


def test_roundtrip_empty_file(tmp_path):
    src = tmp_path / "empty.txt"
    src.write_bytes(b"")
    dest = tmp_path / "out"
    dest.mkdir()
    results, _ = roundtrip(src, dest)
    assert results == [dest / "empty.txt"]
    assert (dest / "empty.txt").read_bytes() == b""


def test_incoming_rejects_size_mismatch(tmp_path):
    incoming = ft.IncomingFiles(directory_factory=lambda: tmp_path)
    incoming.begin({"id": 1, "name": "a.bin", "size": 3})
    with pytest.raises(ft.TransferError):
        incoming.data(ft.pack_data(1, b"abcd"))  # nhiều hơn khai báo
    assert not list(tmp_path.iterdir())  # .part đã bị xoá

    incoming.begin({"id": 2, "name": "b.bin", "size": 3})
    incoming.data(ft.pack_data(2, b"ab"))
    with pytest.raises(ft.TransferError):
        incoming.end({"id": 2})  # thiếu byte
    assert not list(tmp_path.iterdir())


def test_incoming_unknown_id_and_cancel(tmp_path):
    incoming = ft.IncomingFiles(directory_factory=lambda: tmp_path)
    with pytest.raises(ft.TransferError):
        incoming.data(ft.pack_data(9, b"x"))
    with pytest.raises(ft.TransferError):
        incoming.data(b"\x00")
    incoming.begin({"id": 3, "name": "c.bin", "size": 10})
    assert incoming.name_of(3) == "c.bin"
    incoming.abort_all()
    assert not list(tmp_path.iterdir())


def test_send_queue_missing_file(tmp_path):
    queue = ft.SendQueue()
    queue.add(tmp_path / "nope.bin")
    errors = []
    queue.pump(lambda e: None, lambda d: None,
               on_error=lambda name, err: errors.append(name))
    assert errors == ["nope.bin"]
    assert not queue.busy
