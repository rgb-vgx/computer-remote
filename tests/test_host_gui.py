from types import SimpleNamespace

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
