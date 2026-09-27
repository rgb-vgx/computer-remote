import sys
from pathlib import Path

import pytest

import common


def test_app_log_dir_source_mode():
    log_dir = common.app_log_dir()
    assert log_dir.name == "logs"
    assert log_dir.is_dir()
    assert log_dir.parent == Path(common.__file__).resolve().parents[1]


def test_app_log_dir_frozen(monkeypatch, tmp_path):
    fake_exe = tmp_path / "remote-host-gui"
    monkeypatch.setattr(common.sys, "frozen", True, raising=False)
    monkeypatch.setattr(common.sys, "executable", str(fake_exe))
    assert common.app_log_dir() == tmp_path / "logs"


def test_no_console_kwargs_linux():
    if sys.platform == "win32":
        pytest.skip("chỉ kiểm tra nhánh non-Windows trên Linux CI")
    assert common.no_console_kwargs() == {}


def test_no_console_kwargs_windows(monkeypatch):
    monkeypatch.setattr(common.sys, "platform", "win32")
    kwargs = common.no_console_kwargs()
    assert kwargs["creationflags"] == 0x08000000
