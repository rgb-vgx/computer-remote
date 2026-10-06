import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Không đọc/ghi autostart thật của máy chạy test (Linux).
if sys.platform != "win32":
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="remote-mvp-test-xdg-")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="session", autouse=True)
def isolated_settings(tmp_path_factory):
    """Không ghi QSettings vào config thật của máy chạy test."""
    from PySide6.QtCore import QSettings

    path = tmp_path_factory.mktemp("qsettings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(path))
    return path


@pytest.fixture(autouse=True)
def no_host_cursor(monkeypatch):
    """Test không đọc con trỏ thật của máy chạy test (X11/Windows)."""
    try:
        import host
    except Exception:
        return
    monkeypatch.setattr(host, "make_cursor_source", lambda: None)
