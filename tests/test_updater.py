import functools
import http.server
import os
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from common import updater


def test_parse_version():
    assert updater.parse_version("v0.1.3") == (0, 1, 3)
    assert updater.parse_version("0.1.3-3-geb7fcac") == (0, 1, 3)
    assert updater.parse_version("v2.0") == (2, 0, 0)
    assert updater.parse_version("rác") == (0, 0, 0)


def test_is_newer():
    assert updater.is_newer("0.1.4", "0.1.3-2-gabc")
    assert not updater.is_newer("0.1.3", "0.1.3-2-gabc")
    assert not updater.is_newer("0.1.3", "0.1.3")


ASSETS = [
    updater.Asset("remote-client-0.2.0-windows-x64.zip", "u1"),
    updater.Asset("remote-client-0.2.0-linux-x64.tar.gz", "u2"),
    updater.Asset("remote-host-gui-0.2.0-linux-x64.tar.gz", "u3"),
    updater.Asset("remote-host-0.2.0-linux-x64.tar.gz", "u4"),
]


def test_pick_asset_linux(monkeypatch):
    monkeypatch.setattr(updater, "platform",
                        SimpleNamespace(system=lambda: "Linux", machine=lambda: "x86_64"))
    assert updater.pick_asset(ASSETS, "client").url == "u2"
    assert updater.pick_asset(ASSETS, "host-gui").url == "u3"
    # "remote-host-" không được khớp nhầm "remote-host-gui-..."
    assert updater.pick_asset(ASSETS, "host").url == "u4"


def test_pick_asset_windows(monkeypatch):
    monkeypatch.setattr(updater, "platform",
                        SimpleNamespace(system=lambda: "Windows", machine=lambda: "AMD64"))
    assert updater.pick_asset(ASSETS, "client").url == "u1"
    assert updater.pick_asset(ASSETS, "host") is None


def _make_payload(tmp_path: Path) -> Path:
    payload = tmp_path / "remote-client"
    (payload / "_internal").mkdir(parents=True)
    (payload / "remote-client").write_text("bin")
    (payload / "_internal" / "lib.so").write_text("lib")
    return payload


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_extract_payload(tmp_path, kind):
    payload = _make_payload(tmp_path)
    if kind == "zip":
        archive = tmp_path / "a.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for item in payload.rglob("*"):
                zf.write(item, item.relative_to(tmp_path))
    else:
        archive = tmp_path / "a.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(payload, arcname="remote-client")
    got = updater.extract_payload(archive, tmp_path / f"out-{kind}")
    assert (got / "remote-client").read_text() == "bin"
    assert (got / "_internal" / "lib.so").read_text() == "lib"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # bỏ qua ConnectionReset khi test cancel tải giữa chừng


@pytest.fixture
def http_blob(tmp_path):
    serve = tmp_path / "serve"
    serve.mkdir()
    blob = os.urandom(300_000)
    (serve / "blob.bin").write_bytes(blob)
    server = _QuietServer(
        ("127.0.0.1", 0),
        functools.partial(_QuietHandler, directory=str(serve)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/blob.bin"
    # chờ server sẵn sàng (tránh flaky khi máy bận)
    import urllib.request
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1).read(1)
            break
        except Exception:
            time.sleep(0.05)
    else:
        pytest.fail("local HTTP server không lên")
    yield url, blob
    server.shutdown()


def test_download_with_progress(tmp_path, http_blob):
    url, blob = http_blob
    calls = []
    asset = updater.Asset("blob.bin", url, len(blob))
    out = updater.download(asset, tmp_path / "dl",
                           progress=lambda done, total: calls.append(done))
    assert out.read_bytes() == blob
    assert calls and calls[-1] == len(blob)


def test_download_cancel(tmp_path, http_blob):
    url, blob = http_blob
    cancel = threading.Event()
    cancel.set()
    asset = updater.Asset("blob.bin", url, len(blob))
    with pytest.raises(updater.UpdateCancelled):
        updater.download(asset, tmp_path / "dl2", cancel=cancel)
    assert not (tmp_path / "dl2" / "blob.bin").exists()


def test_download_error_is_wrapped(tmp_path):
    asset = updater.Asset("x.bin", "http://127.0.0.1:1/x.bin", 1)
    with pytest.raises(updater.UpdateError):
        updater.download(asset, tmp_path / "dl3")


@pytest.mark.skipif(os.name == "nt", reason="script swap chỉ test trên POSIX")
def test_install_and_restart_frozen(tmp_path, monkeypatch):
    """Thay thư mục app giả + chạy exe mới + giữ logs (Linux)."""
    app_dir = tmp_path / "app"
    (app_dir / "logs").mkdir(parents=True)
    (app_dir / "logs" / "host.log").write_text("log cu")
    (app_dir / "_internal").mkdir()
    (app_dir / "_internal" / "old.so").write_text("old")
    exe = app_dir / "remote-client"
    exe.write_text("#!/bin/sh\necho new > \"$(dirname \"$0\")/STARTED\"\n")
    exe.chmod(0o755)

    payload = tmp_path / "new" / "remote-client"
    (payload / "_internal").mkdir(parents=True)
    (payload / "_internal" / "new.so").write_text("new")
    (payload / "version.txt").write_text("9.9.9\n")
    new_exe = payload / "remote-client"
    new_exe.write_text("#!/bin/sh\necho new > \"$(dirname \"$0\")/STARTED\"\n")
    new_exe.chmod(0o755)
    archive = tmp_path / "new.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload, arcname="remote-client")

    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    monkeypatch.setattr(updater.os, "getpid", lambda: 99999999)

    assert updater.install_and_restart(archive, tmp_path / "work") is True
    deadline = time.time() + 15
    while time.time() < deadline and not (app_dir / "STARTED").exists():
        time.sleep(0.1)
    assert (app_dir / "STARTED").exists()
    assert (app_dir / "_internal" / "new.so").exists()
    assert not (app_dir / "_internal" / "old.so").exists()
    assert (app_dir / "version.txt").read_text().strip() == "9.9.9"
    assert (app_dir / "logs" / "host.log").read_text() == "log cu"


@pytest.mark.skipif(shutil.which("git") is None, reason="cần git")
def test_source_update(tmp_path):
    git = "git"
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    origin = tmp_path / "origin.git"
    subprocess.run([git, "init", "--bare", "-q", str(origin)], check=True)
    dev = tmp_path / "dev"
    subprocess.run([git, "clone", "-q", str(origin), str(dev)], check=True)
    (dev / "a.txt").write_text("1")
    subprocess.run([git, "-C", str(dev), "add", "."], check=True, env=env)
    subprocess.run([git, "-C", str(dev), "commit", "-qm", "1"], check=True, env=env)
    subprocess.run([git, "-C", str(dev), "push", "-q", "origin", "HEAD"],
                   check=True, env=env)

    work = tmp_path / "work"
    subprocess.run([git, "clone", "-q", str(origin), str(work)], check=True)
    (dev / "b.txt").write_text("2")
    subprocess.run([git, "-C", str(dev), "add", "."], check=True, env=env)
    subprocess.run([git, "-C", str(dev), "commit", "-qm", "2"], check=True, env=env)
    subprocess.run([git, "-C", str(dev), "push", "-q", "origin", "HEAD"],
                   check=True, env=env)

    updater.source_update(repo=work)
    assert (work / "b.txt").exists()
