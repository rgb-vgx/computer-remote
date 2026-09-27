import pytest
from PySide6.QtCore import QObject, Qt, Signal

import client


@pytest.fixture
def window(qapp, monkeypatch):
    """Tạo MainWindow với NetworkWorker giả; dọn sạch sau mỗi test."""
    FakeWorker.instances = []
    monkeypatch.setattr(client, "NetworkWorker", FakeWorker)
    created = []

    def factory():
        win = client.MainWindow()
        created.append(win)
        return win

    yield factory
    for win in created:
        win._want_connected = False
        win._reconnect_timer.stop()
        win.close()
        win.deleteLater()
    qapp.processEvents()


def test_qt_key_to_key_str():
    assert client._qt_key_to_key_str(Qt.Key_A, "a") == "a"
    assert client._qt_key_to_key_str(Qt.Key_A, "A") == "A"
    assert client._qt_key_to_key_str(Qt.Key_Return, "\r") == "Key.enter"
    assert client._qt_key_to_key_str(Qt.Key_Enter, "\r") == "Key.enter"
    assert client._qt_key_to_key_str(Qt.Key_F5, "") == "Key.f5"
    assert client._qt_key_to_key_str(Qt.Key_Left, "") == "Key.left"
    assert client._qt_key_to_key_str(Qt.Key_Backspace, "\x08") == "Key.backspace"
    assert client._qt_key_to_key_str(Qt.Key_Space, " ") == " "
    # tổ hợp Ctrl: Qt không cho text printable -> dùng ký tự gốc
    assert client._qt_key_to_key_str(Qt.Key_C, "\x03") == "c"
    assert client._qt_key_to_key_str(Qt.Key_1, "\x01") == "1"


class FakeWorker(QObject):
    frame_ready = Signal(object)
    status = Signal(str)
    connected = Signal()
    rejected = Signal(str)
    disconnected = Signal(str)
    clipboard_from_host = Signal(str)

    instances: list["FakeWorker"] = []

    def __init__(self, host, port, token):
        super().__init__()
        self.host = host
        self.port = port
        self.token = token
        self.sent = []
        FakeWorker.instances.append(self)

    def send_control(self, event):
        self.sent.append(event)

    def start(self):
        pass

    def stop(self):
        pass

    def wait(self, ms):
        pass


def test_settings_roundtrip(window):
    first = window()
    first.host_edit.setText("100.64.0.7")
    first.port_edit.setText("9999")
    first.token_edit.setText("secret")
    first.res_combo.setCurrentIndex(5)  # 1600 (HD+)
    first._save_settings()

    second = window()
    assert second.host_edit.text() == "100.64.0.7"
    assert second.port_edit.text() == "9999"
    assert second.token_edit.text() == "secret"
    assert second.res_combo.currentText() == "1600 (HD+)"


def test_resolution_selection(window):
    win = window()
    win.res_combo.setCurrentIndex(0)  # "Theo host" (QSettings có thể đã lưu trước đó)
    win.host_edit.setText("127.0.0.1")
    win.token_edit.setText("t")
    win._connect()
    assert win.worker.sent == []  # "Theo host" -> không gửi
    widths = [win.res_combo.itemData(i) for i in range(win.res_combo.count())]
    win.res_combo.setCurrentIndex(widths.index(2560))
    assert win.worker.sent[-1]["max_width"] == 2560
    win.res_combo.setEditText("2200")
    win.res_combo.lineEdit().editingFinished.emit()
    assert win.worker.sent[-1]["max_width"] == 2200


def test_auto_reconnect_flow(window):
    win = window()
    win.show()
    win.host_edit.setText("127.0.0.1")
    win.token_edit.setText("t")
    win._connect()
    assert win._want_connected
    first = win.worker
    assert win.connect_btn.text() == "Disconnect"

    # rớt mạng -> hẹn thử lại 1s
    first.disconnected.emit("mất mạng")
    assert win.worker is None
    assert win._reconnect_timer.isActive()
    assert "thử lại sau 1s" in win.status_label.text()

    # thử lại nhưng vẫn rớt -> backoff 2s
    win._reconnect_timer.stop()
    win._reconnect_now()
    assert win.worker is not first
    win.worker.disconnected.emit("rớt nữa")
    assert win._reconnect_attempts == 2
    assert "thử lại sau 2s" in win.status_label.text()

    # kết nối được -> reset
    win._reconnect_timer.stop()
    win._reconnect_now()
    win.worker.connected.emit()
    assert win._reconnect_attempts == 0

    # host từ chối -> dừng hẳn, không retry
    win.worker.rejected.emit("Host từ chối: auth failed")
    assert not win._want_connected
    assert win.worker is None
    assert not win._reconnect_timer.isActive()
    assert win.connect_btn.text() == "Connect"
    assert win.host_edit.isEnabled()


def test_frame_size_label(window):
    from PySide6.QtGui import QImage

    win = window()
    img = QImage(1280, 720, QImage.Format_RGB888)
    img.fill(0)
    win._on_frame_ready(img)
    assert win.size_label.text() == "1280x720"
