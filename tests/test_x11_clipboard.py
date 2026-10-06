"""Clipboard X11 (python-xlib) trên X server thật (Xvfb) — skip nếu không có."""

import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("Xvfb") is None,
    reason="cần Linux + Xvfb")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def xvfb():
    display = ":87"
    proc = subprocess.Popen(["Xvfb", display, "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lock = "/tmp/.X11-unix/X87"
    for _ in range(100):
        if os.path.exists(lock):
            break
        time.sleep(0.05)
    yield display
    proc.terminate()
    proc.wait(timeout=5)


def run_py(display, code, timeout=20):
    """Chạy code ở tiến trình riêng (một X client khác) trên display Xvfb."""
    env = dict(os.environ, DISPLAY=display, QT_QPA_PLATFORM="xcb",
               PYTHONPATH=ROOT)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                          env=env, capture_output=True, text=True,
                          timeout=timeout, cwd=ROOT)


@pytest.fixture
def host_clipboard(xvfb, monkeypatch):
    import host

    monkeypatch.setenv("DISPLAY", xvfb)
    clip = host.X11Clipboard()
    yield clip
    clip.close()


def wait_owner(clip):
    deadline = time.time() + 2
    while time.time() < deadline:
        owner = clip._reader.get_selection_owner(clip._clipboard)
        if getattr(owner, "id", owner) == clip._owner_win.id:
            return
        time.sleep(0.02)
    raise AssertionError("không giành được CLIPBOARD")


def test_set_then_other_process_reads(host_clipboard, xvfb):
    host_clipboard.set_text("xin chào ✓ clipboard")
    wait_owner(host_clipboard)
    assert host_clipboard.get_text() == "xin chào ✓ clipboard"
    out = run_py(xvfb, """
        import host
        print(host.X11Clipboard().get_text(), end="")
    """)
    assert out.stdout == "xin chào ✓ clipboard", out.stderr


def test_large_text_uses_incr_both_ways(host_clipboard, xvfb):
    big = "dòng log 0123456789 " * 30000  # ~600 KB > 1 chunk → INCR
    host_clipboard.set_text(big)
    wait_owner(host_clipboard)
    out = run_py(xvfb, """
        import host
        text = host.X11Clipboard().get_text(timeout=5)
        print(len(text), text[:20] == "dòng log 0123456789 "[:20], end="")
    """)
    assert out.stdout == f"{len(big)} True", out.stderr

    # Tiến trình khác sở hữu text lớn → host đọc qua INCR.
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent("""
            import time, host
            c = host.X11Clipboard()
            c.set_text("Ω" * 400000)
            time.sleep(6)
        """)],
        env=dict(os.environ, DISPLAY=xvfb, PYTHONPATH=ROOT), cwd=ROOT)
    try:
        deadline = time.time() + 5
        text = ""
        while time.time() < deadline and len(text) != 400000:
            time.sleep(0.2)
            text = host_clipboard.get_text(timeout=5)
        assert text == "Ω" * 400000
    finally:
        proc.terminate()


def test_interop_with_qt_app(host_clipboard, xvfb):
    pytest.importorskip("PySide6")
    # App Qt (như Kate, Firefox...) copy → host đọc được.
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent("""
            import sys
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import QTimer
            app = QApplication(sys.argv)
            app.clipboard().setText("từ app Qt")
            QTimer.singleShot(4000, app.quit)
            app.exec()
        """)],
        env=dict(os.environ, DISPLAY=xvfb, QT_QPA_PLATFORM="xcb"), cwd=ROOT)
    try:
        deadline = time.time() + 4
        text = ""
        while time.time() < deadline and text != "từ app Qt":
            time.sleep(0.2)
            text = host_clipboard.get_text()
        assert text == "từ app Qt"
    finally:
        proc.wait(timeout=10)

    # Host đặt clipboard (client gửi sang) → app Qt dán được.
    host_clipboard.set_text("dán vào app Qt ✓")
    wait_owner(host_clipboard)
    out = run_py(xvfb, """
        import sys
        from PySide6.QtWidgets import QApplication
        app = QApplication(sys.argv)
        print(app.clipboard().text(), end="")
    """)
    assert out.stdout == "dán vào app Qt ✓", out.stderr
