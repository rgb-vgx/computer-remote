#!/usr/bin/env python3
"""Đóng gói release bằng PyInstaller.

Lưu ý: PyInstaller KHÔNG cross-compile — phải chạy script trên đúng OS đích:

    Linux   -> remote-host (CLI), remote-host-gui, remote-client
    Windows -> remote-client

Cách dùng (trong venv đã cài requirements + pyinstaller):

    python packaging/build.py                # build tất cả target của OS hiện tại
    python packaging/build.py client         # chỉ build target client
    python packaging/build.py --no-archive   # giữ nguyên thư mục dist, không nén

Kết quả: dist/<target>/... và release/<target>-<version>-<os>-<arch>.(zip|tar.gz)
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist"
WORK_DIR = ROOT / "build" / "pyinstaller"
RELEASE_DIR = ROOT / "release"

# Bỏ các module không dùng đến để giảm kích thước bundle.
EXCLUDES = [
    "tkinter",
    "PyQt5", "PyQt6", "PySide2",
    "matplotlib", "IPython", "pytest", "PIL", "pandas", "scipy",
]

# pynput/mss import backend theo platform bằng importlib → cần khai báo tay.
HIDDEN_LINUX = [
    "pynput.keyboard._xorg",
    "pynput.mouse._xorg",
    "pynput._util.xorg",
    "pynput._util.xorg_keysyms",
    "mss.linux",
    "mss.linux.xlib",
    "mss.linux.xgetimage",
    "mss.linux.xshmgetimage",
]
HIDDEN_WINDOWS = [
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "pynput._util.win32",
    "pynput._util.win32_vks",
    "mss.windows",
    "mss.windows.gdi",
]


@dataclass
class Target:
    entry: Path
    name: str
    windowed: bool
    platforms: set[str]
    requires: set[str] = field(default_factory=set)


TARGETS: dict[str, Target] = {
    "client": Target(
        entry=ROOT / "client.py",
        name="remote-client",
        windowed=True,
        platforms={"linux", "windows"},
    ),
    "host": Target(
        entry=ROOT / "host.py",
        name="remote-host",
        windowed=False,
        platforms={"linux"},
        requires={"mss", "pynput"},
    ),
    "host-gui": Target(
        entry=ROOT / "host_gui.py",
        name="remote-host-gui",
        windowed=True,
        platforms={"linux"},
        requires={"mss", "pynput"},
    ),
}


def current_platform() -> str:
    os_name = platform.system().lower()
    return {"darwin": "macos"}.get(os_name, os_name)


def arch_tag() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(
        machine, machine)


def app_version() -> str:
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=ROOT, capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().lstrip("v")
    except (OSError, subprocess.SubprocessError):
        pass
    return "dev"


def hidden_imports(target: Target) -> list[str]:
    if not target.requires:
        return []
    return HIDDEN_LINUX if current_platform() == "linux" else HIDDEN_WINDOWS


def build_target(target: Target) -> Path:
    app_dir = DIST_DIR / target.name
    if app_dir.exists():
        shutil.rmtree(app_dir)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir",
        "--name", target.name,
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(WORK_DIR),
        "--paths", str(ROOT),
    ]
    if target.windowed:
        cmd.append("--windowed")
    for mod in hidden_imports(target):
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(target.entry))

    print(f"\n=== Build {target.name} ({target.entry.name}) ===", flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)
    if not app_dir.exists():
        raise RuntimeError(f"PyInstaller không tạo được {app_dir}")
    return app_dir


def archive_target(target: Target, version: str) -> Path:
    RELEASE_DIR.mkdir(exist_ok=True)
    base = f"{target.name}-{version}-{current_platform()}-{arch_tag()}"
    print(f"=== Archive {base} ===", flush=True)
    archive = Path(shutil.make_archive(
        str(RELEASE_DIR / base), "zip" if current_platform() == "windows"
        else "gztar", root_dir=str(DIST_DIR), base_dir=target.name))
    return archive


def dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GiB"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("targets", nargs="*", choices=sorted(TARGETS),
                   help="Target cần build (mặc định: tất cả theo OS hiện tại)")
    p.add_argument("--no-archive", action="store_true",
                   help="Không nén zip/tar.gz sau khi build")
    p.add_argument("--version", default=None,
                   help="Version dùng cho tên file release (mặc định: git describe)")
    args = p.parse_args()

    if subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                      capture_output=True).returncode != 0:
        print("Thiếu PyInstaller. Cài: pip install pyinstaller", file=sys.stderr)
        return 1

    version = (args.version or app_version()).lstrip("v")
    plat = current_platform()

    names = args.targets or [n for n, t in TARGETS.items()
                             if plat in t.platforms]
    skipped = [n for n in TARGETS if plat not in TARGETS[n].platforms]
    for name in skipped:
        print(f"Bỏ qua {name}: không hỗ trợ trên {plat}")

    archives = []
    for name in names:
        target = TARGETS[name]
        if plat not in target.platforms:
            print(f"Bỏ qua {name}: không hỗ trợ trên {plat}", file=sys.stderr)
            continue
        app_dir = build_target(target)
        print(f"  {app_dir.relative_to(ROOT)}: {human(dir_size(app_dir))}")
        if not args.no_archive:
            archives.append(archive_target(target, version))

    print(f"\nVersion: {version}")
    for path in archives:
        print(f"  release/{path.name}: {human(path.stat().st_size)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
