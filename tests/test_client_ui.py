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
    assert client._qt_key_to_key_str(Qt.Key_Tab, "\t") == "Key.tab"
    assert client._qt_key_to_key_str(Qt.Key_Backtab, "") == "Key.tab"
    assert client._qt_key_to_key_str(Qt.Key_Insert, "") == "Key.insert"
    assert client._qt_key_to_key_str(Qt.Key_Delete, "") == "Key.delete"
    assert client._qt_key_to_key_str(Qt.Key_Meta, "") == "Key.cmd"
    assert client._qt_key_to_key_str(Qt.Key_CapsLock, "") == "Key.caps_lock"
    assert client._qt_key_to_key_str(Qt.Key_Menu, "") == "Key.menu"
    assert client._qt_key_to_key_str(Qt.Key_Pause, "") == "Key.pause"
    assert client._qt_key_to_key_str(Qt.Key_Print, "") == "Key.print_screen"
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
    monitors_ready = Signal(list, int)
    permissions_ready = Signal(dict)
    cursor_shape = Signal(dict)
    cursor_pos = Signal(float, float)
    pong = Signal(float)
    quality_info = Signal(dict)
    file_progress = Signal(bool, str, int, int)
    file_done = Signal(bool, str, bool, str)

    instances: list["FakeWorker"] = []

    def __init__(self, host, port, token):
        super().__init__()
        self.host = host
        self.port = port
        self.token = token
        self.sent = []
        self.files = []
        self.bytes_received = 0
        FakeWorker.instances.append(self)

    def send_control(self, event):
        self.sent.append(event)

    def send_file(self, path):
        self.files.append(path)

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
    first.res_combo.setCurrentIndex(first.res_combo.findText("1600 (HD+)"))
    first._save_settings()

    second = window()
    assert second.host_edit.text() == "100.64.0.7"
    assert second.port_edit.text() == "9999"
    assert second.token_edit.text() == "secret"
    assert second.res_combo.currentText() == "1600 (HD+)"
    assert second._resolution_mode() == "manual"


def test_settings_roundtrip_modes(window):
    first = window()
    first.res_combo.setCurrentIndex(first.res_combo.findText("Theo host"))
    first._save_settings()
    assert window()._resolution_mode() == "host"

    first.settings.setValue("manual_width", 1600)
    first.res_combo.setCurrentIndex(first.res_combo.findData(client.RES_AUTO))
    first._save_settings()
    second = window()
    assert second._resolution_mode() == "auto"
    assert second.res_combo.currentText() == "Tự động (vừa cửa sổ)"
    # Lưu ở mode auto không được ghi đè manual_width (dùng lại khi về manual).
    assert int(second.settings.value("manual_width")) == 1600


def test_settings_legacy_resolution_text(window):
    """Cài đặt bản cũ (chỉ có key 'resolution' dạng text) vẫn đọc được."""
    first = window()
    first.res_combo.setCurrentIndex(first.res_combo.findText("Theo host"))
    first._save_settings()
    first.settings.remove("resolution_mode")
    first.settings.setValue("resolution", "1280 (HD)")

    second = window()
    assert second.res_combo.currentText() == "1280 (HD)"
    assert second._resolution_mode() == "manual"
    second._save_settings()  # ghi lại key mới cho các test sau


def test_resolution_selection(window):
    win = window()
    win.res_combo.setCurrentIndex(win.res_combo.findText("Theo host"))
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
    assert win.connect_btn.text() == win.TEXT_DISCONNECT

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
    assert win.connect_btn.text() == win.TEXT_CONNECT
    assert win.host_edit.isEnabled()


def test_frame_size_label(window):
    from PySide6.QtGui import QImage

    win = window()
    img = QImage(1280, 720, QImage.Format_RGB888)
    img.fill(0)
    win._on_frame_ready(img)
    assert win.size_label.text() == "1280x720"


def test_input_method_commit_text(qapp):
    from PySide6.QtGui import QInputMethodEvent

    view = client.RemoteView()
    assert view.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
    events = []
    view.key_event.connect(events.append)

    ev = QInputMethodEvent("", [])
    ev.setCommitString("ếa")
    view.inputMethodEvent(ev)
    assert events == [
        {"event": "key_down", "key": "ế"},
        {"event": "key_up", "key": "ế"},
        {"event": "key_down", "key": "a"},
        {"event": "key_up", "key": "a"},
    ]

    events.clear()
    view.inputMethodEvent(QInputMethodEvent("", []))  # preedit rỗng, không lỗi
    assert events == []


MONITORS = [
    {"index": 1, "width": 1920, "height": 1080, "left": 0, "top": 0,
     "primary": True},
    {"index": 2, "width": 1280, "height": 720, "left": 1920, "top": 0,
     "primary": False},
]


def test_monitors_combo_populates(window):
    win = window()
    win._on_monitors_ready(MONITORS, 2)
    assert win.monitor_combo.count() == 3
    assert win.monitor_combo.itemText(0) == "Theo host"
    assert "(chính)" in win.monitor_combo.itemText(1)
    assert win.monitor_combo.isEnabled()
    assert win.monitor_combo.currentData() == 2
    assert win.monitor_combo.itemData(2) == 2


def test_monitors_combo_single_monitor(window):
    win = window()
    win._on_monitors_ready(MONITORS[:1], 1)
    assert win.monitor_combo.count() == 2
    assert not win.monitor_combo.isEnabled()


def test_monitor_change_sends_control(window):
    win = window()
    win.host_edit.setText("127.0.0.1")
    win.token_edit.setText("t")
    win._connect()
    win._on_monitors_ready(MONITORS, 1)
    win.monitor_combo.setCurrentIndex(2)   # chọn màn hình #2
    assert win.worker.sent[-1] == {"event": "set_monitor", "index": 2}


def test_monitor_combo_reset_on_disconnect(window):
    win = window()
    win.host_edit.setText("127.0.0.1")
    win.token_edit.setText("t")
    win._connect()
    win._on_monitors_ready(MONITORS, 1)
    win._disconnect("test")
    assert win.monitor_combo.count() == 1
    assert not win.monitor_combo.isEnabled()


def _select_auto(win):
    win.res_combo.setCurrentIndex(win.res_combo.findData(client.RES_AUTO))


def test_auto_width_contains_monitor(window, monkeypatch):
    """Tự động: contain-fit vào view, không bao giờ vượt monitor (no upscale)."""
    win = window()
    _select_auto(win)
    monkeypatch.setattr(win.view, "width", lambda: 800)
    monkeypatch.setattr(win.view, "height", lambda: 500)
    monkeypatch.setattr(win.view, "devicePixelRatioF", lambda: 1.0)
    win._on_monitors_ready(MONITORS, 1)  # 1920x1080
    # scale = min(800/1920, 500/1080, 1) = 800/1920 -> width = 800
    assert win._auto_width() == 800
    win._on_monitors_ready(MONITORS, 2)  # đổi màn hình #2: 1280x720
    # scale = min(800/1280, 500/720, 1) = 0.625 -> width = 800
    assert win._auto_width() == 800
    monkeypatch.setattr(win.view, "width", lambda: 2000)
    monkeypatch.setattr(win.view, "height", lambda: 1000)
    # 1280x720 nhỏ hơn view -> scale=1.0 -> width = 1280 (không upscale)
    assert win._auto_width() == 1280


def test_auto_width_hidpi_bucket_clamp(window, monkeypatch):
    """Theo physical pixel (× DPR), bucket 8px, clamp 160–7680, fallback 16:9."""
    win = window()
    _select_auto(win)
    monkeypatch.setattr(win.view, "height", lambda: 500)
    monkeypatch.setattr(win.view, "devicePixelRatioF", lambda: 2.0)
    monkeypatch.setattr(win.view, "width", lambda: 800)
    # Chưa có monitor -> giả định 16:9; theo physical: 1600x1000
    # min(1600, 1000*16/9 = 1777.8) -> 1600
    assert win._auto_width() == 1600
    # bucket 8px: vật lý 1606 (803 x2) -> 1600
    monkeypatch.setattr(win.view, "width", lambda: 803)
    assert win._auto_width() == 1600
    # clamp dưới: vật lý 100px -> bucket 96 -> 160
    monkeypatch.setattr(win.view, "width", lambda: 50)
    monkeypatch.setattr(win.view, "height", lambda: 50)
    assert win._auto_width() == 160


def test_auto_mode_sends_on_monitors_ready(window, monkeypatch):
    """Mode auto: gửi fallback 16:9 ngay khi connect (host cũ không gửi
    monitors), chỉnh lại đúng tỉ lệ ngay khi nhận packet monitors."""
    win = window()
    _select_auto(win)
    monkeypatch.setattr(win.view, "width", lambda: 800)
    monkeypatch.setattr(win.view, "height", lambda: 500)
    monkeypatch.setattr(win.view, "devicePixelRatioF", lambda: 1.0)
    win.host_edit.setText("127.0.0.1")
    win.token_edit.setText("t")
    win._connect()
    # Chưa có monitor -> giả định 16:9: min(800, 500*16/9) = 800
    assert win.worker.sent[-1] == {"event": "set_resolution", "max_width": 800}
    # Monitor 1200x1080 (không phải 16:9) -> contain-fit = 555.6 -> bucket 552
    win._on_monitors_ready(
        [{"index": 1, "width": 1200, "height": 1080,
          "left": 0, "top": 0, "primary": True}], 1)
    assert win.worker.sent[-1] == {"event": "set_resolution", "max_width": 552}


def test_auto_mode_debounces_view_resize(window, monkeypatch):
    """Resize cửa sổ -> debounce 200ms, gộp thành 1 lệnh set_resolution."""
    win = window()
    _select_auto(win)
    monkeypatch.setattr(win.view, "width", lambda: 800)
    monkeypatch.setattr(win.view, "height", lambda: 500)
    monkeypatch.setattr(win.view, "devicePixelRatioF", lambda: 1.0)
    win.host_edit.setText("127.0.0.1")
    win.token_edit.setText("t")
    win._connect()
    win._on_monitors_ready(MONITORS, 1)
    sent_before = len(win.worker.sent)

    win.view.view_resized.emit()
    win.view.view_resized.emit()  # resize dồn -> vẫn chỉ hẹn 1 lần
    assert win._auto_timer.isActive()
    assert len(win.worker.sent) == sent_before  # chưa gửi ngay
    win._auto_timer.timeout.emit()  # mô phỏng hết 200ms (không cần chờ thật)
    assert len(win.worker.sent) == sent_before + 1
    assert win.worker.sent[-1]["max_width"] == 800
    win._auto_timer.stop()

    # Chế độ manual/khác: resize KHÔNG hẹn giờ gửi.
    win.res_combo.setCurrentIndex(win.res_combo.findText("Theo host"))
    win.view.view_resized.emit()
    assert not win._auto_timer.isActive()


def test_codec_generation_guard(monkeypatch):
    """Bỏ packet codec có generation <= đã nhận (đến trễ); host cũ vẫn nhận."""
    created: list[tuple[int, int]] = []

    class FakeDec:
        @staticmethod
        def available():
            return True

        def __init__(self, w, h):
            created.append((w, h))

        def close(self):
            pass

    monkeypatch.setattr(client, "H264Decoder", FakeDec)
    worker = client.NetworkWorker("h", 7777, "t")

    worker._handle_codec_info(
        {"codec": "h264", "width": 640, "height": 360, "generation": 1})
    assert created == [(640, 360)]
    assert worker._codec_generation == 1

    # generation cũ bằng/lớn hơn đã nhận -> bỏ, không dựng decoder mới
    worker._handle_codec_info(
        {"codec": "h264", "width": 320, "height": 180, "generation": 1})
    assert created == [(640, 360)]
    worker._handle_codec_info(
        {"codec": "h264", "width": 320, "height": 180, "generation": 0})
    assert created == [(640, 360)]

    # generation mới hơn -> dựng lại decoder
    worker._handle_codec_info(
        {"codec": "h264", "width": 320, "height": 180, "generation": 2})
    assert created == [(640, 360), (320, 180)]
    assert worker._codec_generation == 2

    # host cũ không gửi generation -> xử lý như trước (không bỏ)
    worker._handle_codec_info({"codec": "h264", "width": 640, "height": 360})
    assert created[-1] == (640, 360)


# ---------------------------------------------------------------------------
# Phím hệ thống, con trỏ host, quyền phiên, truyền file, ping
# ---------------------------------------------------------------------------

def connected_window(window):
    win = window()
    win.host_edit.setText("127.0.0.1")
    win.token_edit.setText("t")
    win._connect()
    return win


def test_win_system_key():
    assert client.win_system_key(0x5B, alt=False, ctrl=False) == "Key.cmd"
    assert client.win_system_key(0x09, alt=True, ctrl=False) == "Key.tab"
    assert client.win_system_key(0x09, alt=False, ctrl=False) == "Key.tab"
    assert client.win_system_key(0x73, alt=True, ctrl=False) == "Key.f4"
    assert client.win_system_key(0x1B, alt=False, ctrl=True) == "Key.esc"
    assert client.win_system_key(0x1B, alt=False, ctrl=False) is None
    assert client.win_system_key(0x41, alt=True, ctrl=True) is None


def test_combo_events_and_menu(window):
    assert client.combo_events(("Key.ctrl", "Key.alt", "Key.delete")) == [
        {"event": "key_down", "key": "Key.ctrl"},
        {"event": "key_down", "key": "Key.alt"},
        {"event": "key_down", "key": "Key.delete"},
        {"event": "key_up", "key": "Key.delete"},
        {"event": "key_up", "key": "Key.alt"},
        {"event": "key_up", "key": "Key.ctrl"},
    ]
    win = connected_window(window)
    win.worker.sent.clear()
    cad = next(a for a in win.keys_menu.actions() if a.text() == "Ctrl + Alt + Del")
    cad.trigger()
    assert [e["key"] for e in win.worker.sent] == [
        "Key.ctrl", "Key.alt", "Key.delete", "Key.delete", "Key.alt", "Key.ctrl"]


def test_capture_setting_persisted(window):
    win = window()
    win.capture_action.setChecked(False)
    assert not win.syskeys.enabled
    assert not window().capture_action.isChecked()
    win.capture_action.setChecked(True)


def test_view_middle_button_tab_and_fullscreen_shortcut(qapp):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent

    view = client.RemoteView()
    view.resize(200, 100)
    img = QImage(200, 100, QImage.Format_RGB888)
    img.fill(0)
    view.set_frame(img)
    mouse, keys, full = [], [], []
    view.mouse_event.connect(mouse.append)
    view.key_event.connect(keys.append)
    view.fullscreen_requested.connect(lambda: full.append(1))

    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(100, 50),
                        QPointF(100, 50), Qt.MiddleButton, Qt.MiddleButton,
                        Qt.NoModifier)
    view.mousePressEvent(press)
    assert mouse[-1]["button"] == "middle"

    tab = QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Tab, Qt.NoModifier, "\t")
    assert view.event(tab) is True  # không bị Qt dùng để chuyển focus
    assert keys[-1] == {"event": "key_down", "key": "Key.tab"}

    keys.clear()
    combo = QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Return,
                      Qt.ControlModifier | Qt.AltModifier, "\r")
    view.keyPressEvent(combo)
    assert full == [1] and keys == []


def test_remote_cursor_shape_and_overlay(qapp):
    import base64

    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage, QPixmap

    assert client.cursor_from_event({"shape": "ibeam"}).shape() == Qt.IBeamCursor
    assert client.cursor_from_event({"shape": "???"}) is None
    assert client.cursor_from_event({"png": "!!!"}) is None
    pix = QPixmap(8, 8)
    pix.fill(Qt.red)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")
    png = base64.b64encode(bytes(buf.data())).decode()
    cursor = client.cursor_from_event({"png": png, "hx": 3, "hy": 4})
    assert cursor.hotSpot().x() == 3 and cursor.hotSpot().y() == 4

    view = client.RemoteView()
    view.resize(200, 100)
    img = QImage(200, 100, QImage.Format_RGB888)
    img.fill(0)
    view.set_frame(img)
    view.set_remote_cursor({"shape": "hand"})
    assert view.cursor().shape() == Qt.PointingHandCursor
    view.set_remote_pos(0.5, 0.5)
    assert view._overlay_visible()        # chuột local không ở trong view
    view.set_remote_cursor({"shape": "hidden"})
    assert not view._overlay_visible()
    view.clear_frame("x")
    assert view._remote_pos is None


def test_permissions_enable_files_and_send(window, tmp_path):
    win = connected_window(window)
    assert not win.file_btn.isEnabled()
    win._send_files([str(tmp_path / "a.txt")])
    assert win.worker.files == []        # chưa có quyền → không gửi
    win.worker.permissions_ready.emit(
        {"type": "permissions", "control": True, "clipboard": True, "files": True})
    assert win.file_btn.isEnabled() and win.view.accept_files
    win._send_files([str(tmp_path / "a.txt")])
    assert win.worker.files == [str(tmp_path / "a.txt")]

    win.worker.file_progress.emit(True, "a.txt", 50, 100)
    assert win.transfer_bar.isVisibleTo(win) and win.transfer_bar.value() == 500
    assert "50%" in win.transfer_label.text()
    win.worker.file_done.emit(False, "b.txt", True, str(tmp_path / "b.txt"))
    assert not win.transfer_bar.isVisibleTo(win)
    assert win.transfer_label.text() == "Đã nhận b.txt"
    assert win.open_folder_btn.isVisibleTo(win)

    win._disconnect("test")
    assert not win.file_btn.isEnabled() and not win.view.accept_files


def test_view_only_permissions(window):
    win = connected_window(window)
    win.worker.permissions_ready.emit({"control": False, "files": False})
    assert "chỉ xem" in win.status_label.text()
    assert not win.file_btn.isEnabled()


def test_ping_and_stats(window):
    win = connected_window(window)
    assert win._ping_timer.isActive() and win._stats_timer.isActive()
    win.worker.sent.clear()
    win._send_ping()
    assert win.worker.sent[-1]["event"] == "ping"
    win.worker.pong.emit(42.4)
    win._frames = 15
    win.worker.bytes_received = 250_000
    win._update_stats()
    assert win.stats_label.text() == "42 ms · 15 fps · 2.0 Mbps"
    win._disconnect("x")
    assert not win._ping_timer.isActive()
    assert win.stats_label.text() == ""


def test_fullscreen_toggle(window):
    win = connected_window(window)
    win.show()
    win._toggle_fullscreen()
    assert win.isFullScreen() and not win.toolbar.isVisible()
    assert win.float_bar.isVisible()
    win._toggle_fullscreen()
    assert not win.isFullScreen() and win.toolbar.isVisible()
    assert not win.float_bar.isVisible()


def test_worker_receives_file_and_pong(qapp, tmp_path, monkeypatch):
    from common import filetransfer as ft

    worker = client.NetworkWorker("127.0.0.1", 1, "t")
    worker._incoming = ft.IncomingFiles(directory_factory=lambda: tmp_path)
    progress, done, pongs = [], [], []
    worker.file_progress.connect(lambda *a: progress.append(a))
    worker.file_done.connect(lambda *a: done.append(a))
    worker.pong.connect(pongs.append)

    worker._on_file_event({"event": "file_begin", "id": 1, "name": "r.txt",
                           "size": 5})
    worker._on_file_data(ft.pack_data(1, b"hello"))
    worker._on_file_event({"event": "file_end", "id": 1})
    assert (tmp_path / "r.txt").read_bytes() == b"hello"
    assert progress[-1] == (False, "r.txt", 5, 5)
    assert done[-1] == (False, "r.txt", True, str(tmp_path / "r.txt"))
    result = worker.control_queue.get_nowait()
    assert result == {"event": "file_result", "id": 1, "ok": True, "name": "r.txt"}

    import json
    import time as _time
    t = round(_time.monotonic() * 1000.0 - 30, 1)
    worker._handle_control(json.dumps({"event": "pong", "t": t}).encode())
    assert pongs and 25 <= pongs[0] < 1000
