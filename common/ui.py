"""common/ui.py — Theme dùng chung cho host GUI và client (PySide6).

- Bảng màu dạng token (dark, slate + xanh dương), không hard-code hex ở widget.
- Icon vector kiểu Lucide (stroke 2px) nhúng sẵn, render bằng QtSvg →
  không cần file asset riêng khi đóng gói PyInstaller.
- ``StatusPill``: chấm màu + chữ (không truyền trạng thái chỉ bằng màu).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

COLORS = {
    "bg": "#0B1120",            # nền cửa sổ
    "surface": "#111827",       # card
    "surface_2": "#1A2333",     # input, nút phụ
    "surface_3": "#243047",     # hover
    "canvas": "#05080F",        # vùng xem màn hình remote
    "border": "#273449",
    "border_strong": "#3A4A66",
    "text": "#E5E7EB",          # 14.9:1 trên surface
    "muted": "#94A3B8",         # 6.9:1 trên surface
    "subtle": "#64748B",
    "primary": "#2563EB",       # chữ trắng 5.2:1
    "primary_hover": "#1D4ED8",
    "primary_pressed": "#1E40AF",
    "accent": "#60A5FA",        # focus ring, link
    "success": "#22C55E",
    "warning": "#F59E0B",
    "danger": "#EF4444",
    "danger_strong": "#DC2626",  # chữ trắng 4.8:1
    "on_primary": "#FFFFFF",
}

# Màu chấm trạng thái theo state của StatusPill.
STATE_COLORS = {
    "idle": COLORS["subtle"],
    "busy": COLORS["warning"],
    "online": COLORS["success"],
    "active": COLORS["accent"],
    "error": COLORS["danger"],
}

FONT_FAMILIES = ["Inter", "Segoe UI", "Noto Sans", "Ubuntu", "Cantarell",
                 "DejaVu Sans", "sans-serif"]
MONO_FAMILIES = ["JetBrains Mono", "Cascadia Mono", "Consolas", "Fira Code",
                 "DejaVu Sans Mono", "monospace"]

# ---------------------------------------------------------------------------
# Icons (Lucide, ISC license) — chỉ phần thân SVG, viewBox 24x24
# ---------------------------------------------------------------------------

_ICONS = {
    "monitor": '<rect width="20" height="14" x="2" y="3" rx="2"/>'
               '<line x1="8" x2="16" y1="21" y2="21"/>'
               '<line x1="12" x2="12" y1="17" y2="21"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "stop": '<rect width="14" height="14" x="5" y="5" rx="2"/>',
    "eye": '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>'
           '<circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/>'
               '<path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16'
               ' 13.16 0 0 1-1.67 2.68"/>'
               '<path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74'
               ' 0 0 0 5.39-1.61"/>'
               '<line x1="2" x2="22" y1="2" y2="22"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>'
            '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>'
               '<path d="M21 3v5h-5"/>'
               '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>'
               '<path d="M8 16H3v5"/>',
    "power": '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
    "plug": '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/>'
            '<path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    "unplug": '<path d="m19 5 3-3"/><path d="m2 22 3-3"/>'
              '<path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4'
              ' 2.4 0 0 0 0 3.4Z"/>'
              '<path d="M7.5 13.5 10 11"/><path d="M10.5 16.5 13 14"/>'
              '<path d="m12 6 6 6 2.3-2.3a2.4 2.4 0 0 0 0-3.4l-2.6-2.6a2.4 2.4'
              ' 0 0 0-3.4 0Z"/>',
    "user-x": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>'
              '<circle cx="9" cy="7" r="4"/>'
              '<line x1="17" x2="22" y1="8" y2="13"/>'
              '<line x1="22" x2="17" y1="8" y2="13"/>',
    "laptop": '<path d="M20 16V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9m16 0H4m16 0'
              ' 1.28 2.55a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45L4 16"/>',
    "trash": '<path d="M3 6h18"/>'
             '<path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>'
             '<path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                '<polyline points="7 10 12 15 17 10"/>'
                '<line x1="12" x2="12" y1="15" y2="3"/>',
    "terminal": '<polyline points="4 17 10 11 4 5"/>'
                '<line x1="12" x2="20" y1="19" y2="19"/>',
    "settings": '<path d="M20 7h-9"/><path d="M14 17H5"/>'
                '<circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
}


def _svg(name: str, color: str, stroke: float = 2.0) -> bytes:
    body = _ICONS[name]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        f' stroke="{color}" stroke-width="{stroke}" stroke-linecap="round"'
        f' stroke-linejoin="round">{body}</svg>'
    ).encode()


def icon_pixmap(name: str, color: str | None = None, size: int = 18,
                dpr: float = 2.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(_svg(name, color or COLORS["text"])))
    px = int(size * dpr)
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def icon(name: str, color: str | None = None, disabled: str | None = None) -> QIcon:
    """QIcon vector-render; có pixmap riêng cho trạng thái disabled."""
    ic = QIcon()
    ic.addPixmap(icon_pixmap(name, color, 32), QIcon.Mode.Normal)
    ic.addPixmap(icon_pixmap(name, disabled or COLORS["subtle"], 32),
                 QIcon.Mode.Disabled)
    return ic


def app_icon(dot: str | None = None) -> QIcon:
    """Icon ứng dụng: khối bo góc xanh + glyph màn hình; ``dot`` = chấm trạng thái."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(COLORS["primary"]))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    QSvgRenderer(QByteArray(_svg("monitor", "#FFFFFF", 2.2))).render(
        painter, QRectF(14, 14, 36, 36))
    if dot:
        painter.setBrush(QColor(COLORS["bg"]))
        painter.drawEllipse(QRectF(38, 38, 26, 26))
        painter.setBrush(QColor(dot))
        painter.drawEllipse(QRectF(42, 42, 18, 18))
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

def _asset_dir() -> Path:
    """Thư mục chứa SVG cho QSS (``image: url(...)`` chỉ nhận đường dẫn file)."""
    path = Path(tempfile.gettempdir()) / "remote-mvp-ui"
    path.mkdir(parents=True, exist_ok=True)
    for name, color in (("chevron-down", COLORS["muted"]),
                        ("check", "#FFFFFF")):
        target = path / f"{name}.svg"
        data = _svg(name, color, 2.5 if name == "check" else 2.0)
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)
    return path


def stylesheet() -> str:
    c = COLORS
    assets = _asset_dir().as_posix()
    return f"""
QMainWindow, QDialog {{ background: {c['bg']}; }}
QWidget {{ color: {c['text']}; font-size: 13px; }}
QToolTip {{
    background: {c['surface_2']}; color: {c['text']};
    border: 1px solid {c['border_strong']}; border-radius: 6px; padding: 6px 8px;
}}

/* ---- Cards & typography ---------------------------------------------- */
QFrame#card {{
    background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 12px;
}}
QFrame#toolbar {{
    background: {c['surface']}; border-bottom: 1px solid {c['border']};
}}
QFrame#statusbar {{
    background: {c['surface']}; border-top: 1px solid {c['border']};
}}
QFrame#vline {{ background: {c['border']}; max-width: 1px; min-width: 1px; }}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 16px; font-weight: 600; }}
QLabel[role="section"] {{
    color: {c['muted']}; font-size: 11px; font-weight: 600; letter-spacing: 0.6px;
}}
QLabel[role="muted"] {{ color: {c['muted']}; }}
QLabel[role="field"] {{ color: {c['muted']}; font-size: 12px; font-weight: 500; }}
QLabel[role="address"] {{ font-size: 24px; font-weight: 600; letter-spacing: 0.5px; }}

/* ---- Buttons --------------------------------------------------------- */
QPushButton, QToolButton {{
    background: {c['surface_2']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 8px;
    padding: 7px 14px; min-height: 20px;
}}
QToolButton {{ padding: 6px; }}
QPushButton:hover, QToolButton:hover {{
    background: {c['surface_3']}; border-color: {c['border_strong']};
}}
QPushButton:pressed, QToolButton:pressed {{ background: {c['border']}; }}
QPushButton:focus, QToolButton:focus {{ border: 1px solid {c['accent']}; }}
QPushButton:disabled, QToolButton:disabled {{
    color: {c['subtle']}; background: {c['surface']}; border-color: {c['border']};
}}
QToolButton:checked {{ background: {c['surface_3']}; }}

QPushButton[variant="primary"] {{
    background: {c['primary']}; color: {c['on_primary']};
    border: 1px solid {c['primary']}; font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{
    background: {c['primary_hover']}; border-color: {c['primary_hover']};
}}
QPushButton[variant="primary"]:pressed {{ background: {c['primary_pressed']}; }}
QPushButton[variant="primary"]:focus {{ border: 1px solid {c['accent']}; }}

QPushButton[variant="danger"] {{
    background: {c['danger_strong']}; color: {c['on_primary']};
    border: 1px solid {c['danger_strong']}; font-weight: 600;
}}
QPushButton[variant="danger"]:hover {{ background: #B91C1C; border-color: #B91C1C; }}
QPushButton[variant="danger"]:pressed {{ background: #991B1B; }}

QPushButton[variant="ghost"], QToolButton[variant="ghost"] {{
    background: transparent; border-color: transparent; color: {c['muted']};
}}
QPushButton[variant="ghost"]:hover, QToolButton[variant="ghost"]:hover {{
    background: {c['surface_2']}; color: {c['text']};
}}
QPushButton[variant="danger-outline"] {{
    background: transparent; color: #FCA5A5; border: 1px solid #7F1D1D;
}}
QPushButton[variant="danger-outline"]:hover {{ background: #2A1215; border-color: {c['danger']}; }}
QPushButton[variant="danger-outline"]:disabled {{
    color: {c['subtle']}; border-color: {c['border']}; background: transparent;
}}
QPushButton[size="lg"] {{ padding: 11px 18px; font-size: 14px; border-radius: 10px; }}

/* ---- Inputs ---------------------------------------------------------- */
QLineEdit, QComboBox, QPlainTextEdit {{
    background: {c['surface_2']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 8px;
    padding: 6px 10px; min-height: 20px;
    selection-background-color: {c['primary']}; selection-color: #FFFFFF;
}}
QLineEdit:hover, QComboBox:hover {{ border-color: {c['border_strong']}; }}
QLineEdit:focus, QComboBox:focus, QComboBox:on {{ border: 1px solid {c['accent']}; }}
QLineEdit:disabled, QComboBox:disabled {{
    color: {c['muted']}; background: {c['surface']};
}}
QLineEdit[readonly="true"] {{ background: {c['surface']}; }}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 26px; border: none;
}}
QComboBox::down-arrow {{ image: url({assets}/chevron-down.svg); width: 14px; height: 14px; }}
QComboBox QLineEdit {{ background: transparent; border: none; padding: 0; min-height: 0; }}
QComboBox QAbstractItemView {{
    background: {c['surface_2']}; color: {c['text']};
    border: 1px solid {c['border_strong']}; border-radius: 8px; padding: 4px;
    outline: 0; selection-background-color: {c['primary']};
}}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 2px 8px; border-radius: 6px; }}

QPlainTextEdit#log {{
    background: {c['canvas']}; color: #CBD5E1; border-radius: 8px;
    padding: 8px; font-size: 12px;
}}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {c['border_strong']}; background: {c['surface_2']};
}}
QCheckBox::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{
    background: {c['primary']}; border-color: {c['primary']};
    image: url({assets}/check.svg);
}}
QCheckBox:focus {{ color: #FFFFFF; }}

/* ---- Status pill ----------------------------------------------------- */
QFrame#pill {{
    background: {c['surface_2']}; border: 1px solid {c['border']}; border-radius: 13px;
}}
QLabel#pillText {{ font-size: 12px; font-weight: 500; }}

/* ---- Menus & scrollbars ---------------------------------------------- */
QMenu {{
    background: {c['surface_2']}; border: 1px solid {c['border_strong']};
    border-radius: 8px; padding: 4px;
}}
QMenu::item {{ padding: 6px 18px 6px 10px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c['primary']}; color: #FFFFFF; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 6px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {c['border_strong']}; border-radius: 3px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {c['subtle']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMessageBox QLabel {{ color: {c['text']}; }}
"""


def apply_theme(app: QApplication) -> None:
    """Fusion + palette tối (cho phần Qt tự vẽ) + QSS cho chi tiết."""
    app.setStyle("Fusion")
    c = COLORS
    pal = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, c["bg"]),
        (QPalette.ColorRole.WindowText, c["text"]),
        (QPalette.ColorRole.Base, c["surface_2"]),
        (QPalette.ColorRole.AlternateBase, c["surface"]),
        (QPalette.ColorRole.Text, c["text"]),
        (QPalette.ColorRole.Button, c["surface_2"]),
        (QPalette.ColorRole.ButtonText, c["text"]),
        (QPalette.ColorRole.ToolTipBase, c["surface_2"]),
        (QPalette.ColorRole.ToolTipText, c["text"]),
        (QPalette.ColorRole.PlaceholderText, c["subtle"]),
        (QPalette.ColorRole.Highlight, c["primary"]),
        (QPalette.ColorRole.HighlightedText, "#FFFFFF"),
        (QPalette.ColorRole.Link, c["accent"]),
    ):
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["subtle"]))
    app.setPalette(pal)
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPixelSize(13)
    app.setFont(font)
    app.setStyleSheet(stylesheet())


# ---------------------------------------------------------------------------
# Widgets & helpers
# ---------------------------------------------------------------------------

def mono_font(pixel_size: int = 12) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(pixel_size)
    return font


def set_prop(widget: QWidget, name: str, value) -> None:
    """Đổi dynamic property + repolish để QSS áp lại ngay."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def label(text: str = "", role: str | None = None) -> QLabel:
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    return lbl


def card(title: str | None = None, spacing: int = 12) -> tuple[QFrame, QVBoxLayout]:
    """Card bo góc; trả (frame, layout nội dung). ``title`` = nhãn section nhỏ."""
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 16)
    layout.setSpacing(spacing)
    if title:
        layout.addWidget(label(title.upper(), "section"))
    return frame, layout


def vline() -> QFrame:
    line = QFrame()
    line.setObjectName("vline")
    line.setFixedHeight(22)
    return line


class StatusPill(QFrame):
    """Chấm màu + chữ trạng thái. ``text_label`` là QLabel thường (test/đọc text)."""

    def __init__(self, text: str = "", state: str = "idle",
                 text_label: QLabel | None = None) -> None:
        super().__init__()
        self.setObjectName("pill")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 12, 4)
        layout.setSpacing(8)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        self.text_label = text_label or QLabel()
        self.text_label.setObjectName("pillText")
        if text:
            self.text_label.setText(text)
        layout.addWidget(self.dot)
        layout.addWidget(self.text_label)
        self.state = ""
        self.set_state(state)

    def set_state(self, state: str, text: str | None = None) -> None:
        self.state = state
        color = STATE_COLORS.get(state, STATE_COLORS["idle"])
        self.dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        if text is not None:
            self.text_label.setText(text)
