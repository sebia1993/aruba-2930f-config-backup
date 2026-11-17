"""Compact home navigation cards with embedded vector icons.

Icons are drawn from self-contained SVG paths at runtime; no network request,
external icon font, or additional asset is needed in the Windows EXE.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

_ICON_PATHS = {
    "backup": (
        '<ellipse cx="10" cy="5.5" rx="6.5" ry="2.5"/>'
        '<path d="M3.5 5.5v6c0 1.4 2.9 2.5 6.5 2.5"/>'
        '<path d="M3.5 11.5v6c0 1.4 2.7 2.5 6.5 2.5"/>'
        '<path d="M16.5 5.5v4.5"/>'
        '<path d="M13 15.1a5 5 0 1 1 .5 5.3"/>'
        '<path d="M12.1 13.6v3.4h3.4"/>'
    ),
    "ports": (
        '<rect x="10" y="2.5" width="4" height="4" rx="1"/>'
        '<rect x="2.5" y="17.5" width="4" height="4" rx="1"/>'
        '<rect x="17.5" y="17.5" width="4" height="4" rx="1"/>'
        '<path d="M12 6.5v5M4.5 17.5v-3.5a2.5 2.5 0 0 1 2.5-2.5h10'
        'a2.5 2.5 0 0 1 2.5 2.5v3.5"/>'
    ),
    "history": (
        '<path d="M3 9a9 9 0 1 1 .4 7"/>'
        '<path d="M3 3v6h6"/>'
        '<path d="M12 7v5l3 2"/>'
    ),
}

_CARD_STYLE = """
QPushButton {
    background-color: #fcfcfc;
    border: 1px solid #dedede;
    border-radius: 13px;
    padding: 0;
}
QPushButton:hover {
    background-color: #f4f7fb;
    border-color: #becad8;
}
QPushButton:pressed {
    background-color: #eaf0f7;
}
QPushButton:focus {
    border: 2px solid #6a93c9;
}
QPushButton:disabled {
    background-color: #f2f2f2;
    border-color: #e3e3e3;
}
"""


def _render_icon(name: str, color: str) -> QPixmap:
    """Rasterize a small stroke icon at 2x for sharp HiDPI display."""
    paths = _ICON_PATHS[name]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="none" stroke="{color}" stroke-width="1.9"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        f'{paths}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError("Invalid embedded home navigation icon")
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return pixmap


class HomeShortcutButton(QPushButton):
    """Full-surface accessible button displaying an icon above a short title."""

    def __init__(
        self, title: str, description: str, icon_name: str, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.setText("")
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        self.setFixedHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(_CARD_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        layout.addStretch(1)

        icon = QLabel(self)
        icon.setObjectName("homeShortcutIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(
            _render_icon(
                icon_name,
                self.palette().color(QPalette.ColorRole.ButtonText).name(),
            )
        )
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignHCenter)

        label = QLabel(title, self)
        label.setObjectName("homeShortcutTitle")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("background: transparent; color: #171717; font-size: 13px;")
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(label)
        layout.addStretch(1)
