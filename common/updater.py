"""Kiểm tra & tự cập nhật từ GitHub Releases.

Không phụ thuộc Qt — `host.py` (CLI) có thể import mà không cần PySide6.
Repo release là public nên gọi API ẩn danh được (rate limit 60 req/h/IP).

Luồng: fetch_latest() → so version → pick_asset() → download() →
install_and_restart(). Bản đóng gói (PyInstaller) được thay thư mục bằng
script tách rời rồi mở lại; bản chạy source thì ``git pull`` + ``pip install``
rồi ``execv`` để khởi động lại.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GITHUB_REPO = "rgb-vgx/computer-remote"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_USER_AGENT = "python-remote-mvp-updater"

_TARGET_NAMES = {
    "client": "remote-client",
    "host-gui": "remote-host-gui",
    "host": "remote-host",
}


class UpdateError(Exception):
    """Lỗi khi kiểm tra / tải / cài bản mới."""


class UpdateCancelled(Exception):
    """Người dùng hủy giữa chừng."""


@dataclass
class Asset:
    name: str
    url: str
    size: int = 0


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    url: str
    notes: str
    assets: list[Asset] = field(default_factory=list)


def _headers() -> dict[str, str]:
    return {"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_kind() -> str:
    """Đoán loại app theo tên file chạy: client | host-gui | host."""
    stem = Path(sys.executable).stem if is_frozen() else Path(sys.argv[0]).stem
    if stem in ("client", "remote-client"):
        return "client"
    if stem in ("host_gui", "remote-host-gui"):
        return "host-gui"
    return "host"


def platform_tag() -> str:
    os_name = platform.system().lower()
    if os_name == "darwin":
        os_name = "macos"
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower())
    return f"{os_name}-{arch}"


def current_version() -> str:
    """Version hiện tại: bản đóng gói đọc version.txt cạnh exe, source dùng git."""
    if is_frozen():
        path = Path(sys.executable).resolve().parent / "version.txt"
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
        return "0.0.0"
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root()), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().lstrip("v")
    except (OSError, subprocess.SubprocessError):
        pass
    return "0.0.0"


def parse_version(text: str) -> tuple[int, int, int]:
    match = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", text.strip())
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


def fetch_latest(timeout: float = 15.0) -> ReleaseInfo:
    """Lấy release mới nhất từ GitHub (ẩn danh, repo public)."""
    try:
        with urlopen(Request(_API_LATEST, headers=_headers()),
                     timeout=timeout) as resp:
            data = json.load(resp)
    except HTTPError as exc:
        raise UpdateError(f"GitHub trả lỗi HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Không kết nối được GitHub: {exc}") from exc

    assets = [
        Asset(a.get("name", ""), a.get("browser_download_url", ""),
              int(a.get("size", 0)))
        for a in data.get("assets", [])
    ]
    tag = data.get("tag_name", "")
    return ReleaseInfo(
        version=tag.lstrip("v"),
        tag=tag,
        url=data.get("html_url", ""),
        notes=data.get("body") or "",
        assets=assets,
    )


def pick_asset(assets: list[Asset], kind: str) -> Asset | None:
    """Chọn asset khớp OS/arch hiện tại cho loại app."""
    prefix = f"{_TARGET_NAMES[kind]}-"
    ext = ".zip" if platform.system().lower() == "windows" else ".tar.gz"
    suffix = f"-{platform_tag()}{ext}"
    for asset in assets:
        if not asset.name.startswith(prefix) or not asset.name.endswith(suffix):
            continue
        middle = asset.name[len(prefix):-len(suffix)]
        # tránh "remote-host-" khớp nhầm "remote-host-gui-..."
        if middle[:1].isdigit():
            return asset
    return None


def download(asset: Asset, dest_dir: Path, progress=None, cancel=None) -> Path:
    """Tải asset xuống ``dest_dir``, gọi ``progress(done, total)`` mỗi chunk."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / asset.name
    request = Request(asset.url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=30) as resp, open(dest, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or asset.size or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled()
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except UpdateCancelled:
        dest.unlink(missing_ok=True)
        raise
    except (HTTPError, URLError, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise UpdateError(f"Tải thất bại: {exc}") from exc
    return dest


def extract_payload(archive: Path, workdir: Path) -> Path:
    """Giải nén archive, trả về thư mục payload (chứa exe + _internal)."""
    out = Path(workdir) / "extract"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    try:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(out)
        else:
            with tarfile.open(archive) as tf:
                try:
                    tf.extractall(out, filter="data")  # Python >= 3.12
                except TypeError:
                    tf.extractall(out)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        raise UpdateError(f"Giải nén thất bại: {exc}") from exc
    entries = [p for p in out.iterdir() if p.is_dir()]
    return entries[0] if len(entries) == 1 else out


def source_update(repo: Path | None = None) -> None:
    """Cập nhật bản chạy source: git pull --ff-only + pip install."""
    repo = repo or repo_root()
    if not (repo / ".git").exists():
        raise UpdateError("Không tìm thấy .git để cập nhật source")

    def run(cmd: list[str]) -> None:
        proc = subprocess.run(cmd, cwd=str(repo), capture_output=True,
                              text=True, timeout=300)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise UpdateError("Lệnh thất bại: " + " ".join(cmd[:3])
                              + (" — " + " | ".join(tail[-3:]) if tail else ""))

    run(["git", "pull", "--ff-only"])
    requirements = repo / "requirements.txt"
    if requirements.exists():
        run([sys.executable, "-m", "pip", "install", "-q", "-r",
             str(requirements)])


def install_and_restart(archive: Path, workdir: Path) -> bool:
    """Cài bản mới và khởi động lại.

    Trả về True nếu bản đóng gói đã spawn script thay file — caller cần thoát
    app để script chạy. Bản source tự ``execv`` (không quay lại).
    """
    workdir = Path(workdir)
    if not is_frozen():
        source_update()
        entry = Path(sys.argv[0]).resolve()
        os.execv(sys.executable, [sys.executable, str(entry), *sys.argv[1:]])
        return False

    src = extract_payload(archive, workdir)
    exe = Path(sys.executable).resolve()
    app_dir = exe.parent
    if not (src / exe.name).exists():
        raise UpdateError(f"File cập nhật không chứa {exe.name}")

    if sys.platform == "win32":
        script = workdir / "remote-update.bat"
        script.write_text(_WIN_SCRIPT, encoding="utf-8")
        subprocess.Popen(
            ["cmd", "/c", str(script), str(src), str(app_dir), exe.name],
            creationflags=0x00000008 | 0x08000000,  # DETACHED | NO_WINDOW
            close_fds=True)
    else:
        script = workdir / "remote-update.sh"
        script.write_text(
            _LINUX_SCRIPT.format(src=src, dst=app_dir, exe=exe),
            encoding="utf-8")
        script.chmod(0o755)
        subprocess.Popen(["/bin/sh", str(script), str(os.getpid())],
                         start_new_session=True, close_fds=True)
    return True


_LINUX_SCRIPT = """#!/bin/sh
# Chờ app thoát hẳn rồi thay thư mục và mở lại (do updater sinh ra).
PID="$1"
i=0
while kill -0 "$PID" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -gt 100 ] && break
    sleep 0.2
done
sleep 0.3
cp -a "{dst}/logs/." "{src}/logs/" 2>/dev/null || true
rm -rf "{dst}"
mv "{src}" "{dst}"
exec "{exe}"
"""

_WIN_SCRIPT = """@echo off
setlocal
set "SRC=%~1"
set "DST=%~2"
set "EXE=%~3"
:wait
tasklist /fi "imagename eq %EXE%" 2>nul | find /i "%EXE%" >nul
if not errorlevel 1 (
  ping -n 2 127.0.0.1 >nul
  goto wait
)
if exist "%DST%\\logs\\." xcopy "%DST%\\logs" "%SRC%\\logs\\" /E /I /Y >nul 2>&1
robocopy "%SRC%" "%DST%" /E /R:2 /W:1 >nul
start "" "%DST%\\%EXE%"
cd /d "%TEMP%"
rmdir /s /q "%SRC%" >nul 2>&1
del "%~f0" >nul 2>&1
endlocal
"""
