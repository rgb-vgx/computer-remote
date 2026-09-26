"""Luồng UI tự cập nhật (PySide6) dùng chung cho client & host GUI.

Tách khỏi ``common/updater.py`` để host CLI không phải import PySide6.
Mọi thao tác mạng/đĩa chạy trong QThread, GUI chỉ hiện dialog.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMessageBox, QProgressDialog, QWidget,
)

from common import updater


class _CheckWorker(QThread):
    available = Signal(object, object)  # ReleaseInfo, Asset
    uptodate = Signal(str)
    failed = Signal(str)

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(parent)
        self.kind = kind

    def run(self) -> None:
        try:
            info = updater.fetch_latest()
            if not updater.is_newer(info.version, updater.current_version()):
                self.uptodate.emit(updater.current_version())
                return
            asset = updater.pick_asset(info.assets, self.kind)
            if asset is None:
                raise updater.UpdateError(
                    "Bản mới không có file cho nền tảng này")
            self.available.emit(info, asset)
        except Exception as exc:
            self.failed.emit(str(exc))


class _InstallWorker(QThread):
    progress = Signal(int, int)  # done, total
    installed = Signal(bool)     # True: caller cần thoát app
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, asset: updater.Asset, parent=None) -> None:
        super().__init__(parent)
        self.asset = asset
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            workdir = Path(tempfile.mkdtemp(prefix="remote-update-"))
            archive = updater.download(self.asset, workdir,
                                       progress=self.progress.emit,
                                       cancel=self._cancel)
            needs_quit = updater.install_and_restart(archive, workdir)
            self.installed.emit(needs_quit)
        except updater.UpdateCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateController(QObject):
    """Gắn vào 1 window: kiểm tra → xác nhận → tải → cài → khởi động lại."""

    def __init__(self, window: QWidget, kind: str) -> None:
        super().__init__(window)
        self.window = window
        self.kind = kind
        self._worker: QThread | None = None

    def start(self) -> None:
        if self._worker is not None:
            return
        dialog = QProgressDialog("Đang kiểm tra bản mới...", "", 0, 0,
                                 self.window)
        dialog.setWindowTitle("Cập nhật")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setCancelButton(None)
        dialog.setMinimumDuration(300)

        worker = _CheckWorker(self.kind, self)
        self._worker = worker

        def finish() -> None:
            dialog.close()
            self._worker = None

        def on_available(info, asset) -> None:
            finish()
            self._confirm_and_install(info, asset)

        def on_uptodate(version: str) -> None:
            finish()
            QMessageBox.information(
                self.window, "Cập nhật",
                f"Bạn đang dùng bản mới nhất (v{version}).")

        def on_failed(message: str) -> None:
            finish()
            QMessageBox.warning(self.window, "Cập nhật",
                                f"Không kiểm tra được: {message}")

        worker.available.connect(on_available)
        worker.uptodate.connect(on_uptodate)
        worker.failed.connect(on_failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        dialog.show()

    def _confirm_and_install(self, info: updater.ReleaseInfo,
                             asset: updater.Asset) -> None:
        notes = info.notes.strip()
        if len(notes) > 500:
            notes = notes[:500] + "…"
        mode = ("Tải về, tự thay thế và khởi động lại"
                if updater.is_frozen()
                else "Chạy git pull + pip install rồi khởi động lại")
        text = (f"Có bản mới v{info.version} "
                f"(đang dùng v{updater.current_version()}).\n{mode}.\n")
        if notes:
            text += f"\n{notes}"
        answer = QMessageBox.question(
            self.window, "Có bản cập nhật", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return

        dialog = QProgressDialog("Đang tải...", "Hủy", 0, 0, self.window)
        dialog.setWindowTitle("Cập nhật")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)

        worker = _InstallWorker(asset, self)
        self._worker = worker

        def finish() -> None:
            dialog.close()
            self._worker = None

        def on_progress(done: int, total: int) -> None:
            if total > 0:
                if dialog.maximum() != total:
                    dialog.setRange(0, total)
                dialog.setValue(done)
            text = f"Đang tải {asset.name}\n{done / 1048576:.1f} MiB"
            if total > 0:
                text += f" / {total / 1048576:.1f} MiB"
            dialog.setLabelText(text)

        def on_installed(needs_quit: bool) -> None:
            finish()
            if needs_quit:
                QMessageBox.information(
                    self.window, "Cập nhật",
                    f"Đã tải v{info.version}. App sẽ khởi động lại ngay.")
                self._restart_app()

        def on_failed(message: str) -> None:
            finish()
            QMessageBox.warning(self.window, "Cập nhật",
                                f"Cập nhật thất bại: {message}")

        def on_cancelled() -> None:
            finish()

        worker.progress.connect(on_progress)
        worker.installed.connect(on_installed)
        worker.failed.connect(on_failed)
        worker.cancelled.connect(on_cancelled)
        worker.finished.connect(worker.deleteLater)
        dialog.canceled.connect(worker.cancel)
        worker.start()
        dialog.show()

    def _restart_app(self) -> None:
        if hasattr(self.window, "_quit_app"):
            self.window._quit_app()
        else:
            self.window.close()
        QApplication.quit()
