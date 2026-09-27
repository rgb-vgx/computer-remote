#!/usr/bin/env python3
"""Smoke test cài đặt tự động trên CI (Linux + Windows).

Tạo app giả + archive giả rồi chạy ``updater.install_and_restart`` với
``is_frozen``/``sys.executable`` giả lập. Kiểm tra: script chạy, chờ PID,
robocopy/mv thay được file, giữ ``logs/``, và log ``update.log`` ghi đúng.

    python packaging/smoke_update.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import updater  # noqa: E402


def make_exe(path: Path, marker: str) -> None:
    if sys.platform == "win32":
        src = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
        shutil.copy2(src, path)
    else:
        path.write_text(f"#!/bin/sh\ntouch \"$(dirname \"$0\")/{marker}\"\n")
        path.chmod(0o755)


def main() -> int:
    if sys.platform == "win32":
        name, ext = "remote-client.exe", ".zip"
    else:
        name, ext = "remote-client", ".tar.gz"

    tmp = Path(tempfile.mkdtemp(prefix="smoke-update-"))
    try:
        # 1. app giả (bản cũ)
        app_dir = tmp / "app"
        (app_dir / "logs").mkdir(parents=True)
        (app_dir / "logs" / "host.log").write_text("log cu", encoding="utf-8")
        (app_dir / "_internal").mkdir()
        (app_dir / "_internal" / "old.txt").write_text("old", encoding="utf-8")
        make_exe(app_dir / name, "STARTED-OLD")
        (app_dir / "version.txt").write_text("0.0.1\n", encoding="utf-8")

        # 2. payload mới
        payload = tmp / "payload" / "remote-client"
        (payload / "_internal").mkdir(parents=True)
        (payload / "_internal" / "new.txt").write_text("new", encoding="utf-8")
        (payload / "version.txt").write_text("9.9.9\n", encoding="utf-8")
        make_exe(payload / name, "STARTED-NEW")
        archive = tmp / f"remote-client-9.9.9{ext}"
        if ext == ".zip":
            with zipfile.ZipFile(archive, "w") as zf:
                for item in payload.rglob("*"):
                    zf.write(item, item.relative_to(payload.parent))
        else:
            with tarfile.open(archive, "w:gz") as tf:
                tf.add(payload, arcname="remote-client")

        # 3. giả lập bản đóng gói đang chạy
        orig = (updater.is_frozen, updater.sys.executable, updater.os.getpid)
        updater.is_frozen = lambda: True
        updater.sys.executable = str(app_dir / name)
        updater.os.getpid = lambda: 99999999  # không tồn tại → không phải chờ
        try:
            needs_quit = updater.install_and_restart(archive, tmp / "work")
        finally:
            updater.is_frozen, updater.sys.executable, updater.os.getpid = orig
        assert needs_quit is True, "phải báo cần thoát app"
        print("spawn script OK")

        # 4. chờ script thay file
        deadline = time.time() + 30
        version = ""
        while time.time() < deadline:
            try:
                version = (app_dir / "version.txt").read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if version == "9.9.9":
                break
            time.sleep(0.5)
        try:
            assert version == "9.9.9", f"version chưa đổi: {version!r}"
            assert (app_dir / "_internal" / "new.txt").exists(), "thiếu file mới"
            assert (app_dir / "logs" / "host.log").read_text(
                encoding="utf-8") == "log cu", "mất log cũ"
            update_log = (app_dir / "logs" / "update.log").read_text(
                encoding="utf-8", errors="replace")
            assert "robocopy rc=" in update_log or "swapped" in update_log, update_log
            assert "timeout" not in update_log, update_log
        except AssertionError:
            print("--- CHẨN ĐOÁN ---")
            print("app dir:", sorted(str(p.relative_to(app_dir))
                                     for p in app_dir.rglob("*")))
            work = tmp / "work"
            print("work dir:", sorted(str(p.relative_to(work))
                                      for p in work.rglob("*"))
                  if work.exists() else "không có")
            for bat in list(work.glob("remote-update.*")) if work.exists() else []:
                print(f"--- nội dung {bat.name} ---")
                print(bat.read_text(encoding="utf-8", errors="replace"))
            log_file = app_dir / "logs" / "update.log"
            print("--- update.log (tồn tại:", log_file.exists(), ") ---")
            if log_file.exists():
                print(log_file.read_text(encoding="utf-8", errors="replace"))
            raise
        if sys.platform != "win32":
            started = app_dir / "STARTED-NEW"
            deadline = time.time() + 10
            while time.time() < deadline and not started.exists():
                time.sleep(0.2)
            assert started.exists(), "app mới không được mở lại"
        print("update.log:")
        for line in update_log.strip().splitlines():
            print("   ", line)
        print("SMOKE UPDATE OK")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
