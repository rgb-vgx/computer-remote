"""Tiện ích dùng chung cho host và client."""

from __future__ import annotations

import sys
from pathlib import Path


def app_log_dir() -> Path:
    """Thư mục ``logs`` cạnh app.

    Chạy từ source: cạnh repo. Chạy bản đóng gói (PyInstaller): cạnh file
    thực thi, thay vì nằm sâu trong ``_internal``.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[1]
    log_dir = base / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir
