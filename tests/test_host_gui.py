import sys
import types
from types import SimpleNamespace

import pytest

import host_gui


def make_window(qapp):
    win = host_gui.MainWindow(host_gui.parse_gui_args([]))
    qapp.processEvents()
    return win


def cleanup(qapp, win):
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_client_status_and_kick(qapp):
    win = make_window(qapp)
    assert win.client_label.text() == "Client: chưa có"
    assert not win.kick_btn.isEnabled()

    win._on_client_connected("100.64.0.9:50123")
    assert "100.64.0.9:50123" in win.client_label.text()
    assert win.kick_btn.isEnabled()

    calls = []

    class FakeServer:
        def disconnect_client(self):
            calls.append("kick")

    class FakeThread:
        def __init__(self):
            self.server = FakeServer()

        def stop(self):
            pass

        def wait(self, ms):
            pass

    win.server_thread = FakeThread()
    win._kick_client()
    assert calls == ["kick"]

    win._on_client_disconnected()
    assert win.client_label.text() == "Client: chưa có"
    assert not win.kick_btn.isEnabled()
    win.server_thread = None
    cleanup(qapp, win)


def test_server_thread_signals(qapp):
    thread = host_gui.ServerThread(SimpleNamespace(
        token="t", codec="jpeg", bind="127.0.0.1", port=0, fps=15,
        quality=75, max_width=1920, view_only=False, debug=False))
    assert hasattr(thread, "client_connected")
    assert hasattr(thread, "client_disconnected")


def test_autostart_linux_xdg(monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("nhánh XDG chỉ có trên Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not host_gui.autostart_enabled()

    host_gui.apply_autostart(True, "tok en")
    assert host_gui.autostart_enabled()
    content = host_gui.autostart_path().read_text(encoding="utf-8")
    assert "host_gui.py" in content
    assert '--token "tok en" --auto-start' in content

    host_gui.apply_autostart(False, "tok en")
    assert not host_gui.autostart_enabled()


def test_autostart_windows_registry(monkeypatch):
    store = {}
    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = object()
    fake.KEY_SET_VALUE = 1
    fake.REG_SZ = 1

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def query(root, name):
        if name in store:
            return store[name], fake.REG_SZ
        raise FileNotFoundError(name)

    def set_value(key, name, reserved, typ, value):
        store[name] = value

    def delete(key, name):
        if name not in store:
            raise FileNotFoundError(name)
        del store[name]

    fake.OpenKey = lambda *a, **k: _Key()
    fake.CreateKeyEx = lambda *a, **k: _Key()
    fake.QueryValueEx = query
    fake.SetValueEx = set_value
    fake.DeleteValue = delete

    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(host_gui.sys, "platform", "win32")

    assert not host_gui.autostart_enabled()
    host_gui.apply_autostart(True, "tok")
    assert host_gui.autostart_enabled()
    value = store[host_gui._AUTOSTART_REG_NAME]
    assert "host_gui.py" in value
    assert "--token tok --auto-start" in value

    host_gui.apply_autostart(False, "tok")
    assert not host_gui.autostart_enabled()
