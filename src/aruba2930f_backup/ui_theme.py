"""Two-button application appearance shared by all Qt operator pages."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

_THEMES = {
    "light": {
        "window": "#f5f8fb",
        "base": "#ffffff",
        "alternate": "#f0f4f9",
        "text": "#1b2b3c",
        "muted": "#617387",
        "border": "#cbd5e1",
        "button": "#ffffff",
        "accent": "#d77414",
        "accent_text": "#ffffff",
        "sidebar": "#e9f0f6",
        "panel": "#ffffff",
    },
    "dark": {
        "window": "#101b28",
        "base": "#172537",
        "alternate": "#1e3045",
        "text": "#ecf2f8",
        "muted": "#adbdcc",
        "border": "#354b60",
        "button": "#22364b",
        "accent": "#ec902d",
        "accent_text": "#101b28",
        "sidebar": "#142337",
        "panel": "#192a3d",
    },
}


def selected_theme() -> str:
    """Load only one of the two explicit choices; never use an auto mode."""
    value = str(
        QSettings("sebia1993", "Aruba2930FNetworkManager").value("appearance/theme", "dark")
    )
    return value if value in _THEMES else "dark"


def set_theme(mode: str, *, persist: bool = True) -> None:
    if mode not in _THEMES:
        raise ValueError(f"Unsupported appearance: {mode}")
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return
    c = _THEMES[mode]
    app.setStyle("Fusion")
    palette = QPalette()
    for role, name in (
        (QPalette.ColorRole.Window, "window"),
        (QPalette.ColorRole.Base, "base"),
        (QPalette.ColorRole.AlternateBase, "alternate"),
        (QPalette.ColorRole.Text, "text"),
        (QPalette.ColorRole.WindowText, "text"),
        (QPalette.ColorRole.Button, "button"),
        (QPalette.ColorRole.ButtonText, "text"),
        (QPalette.ColorRole.Highlight, "accent"),
        (QPalette.ColorRole.HighlightedText, "accent_text"),
        (QPalette.ColorRole.Mid, "border"),
        (QPalette.ColorRole.Dark, "border"),
        (QPalette.ColorRole.Light, "alternate"),
        (QPalette.ColorRole.ToolTipBase, "base"),
        (QPalette.ColorRole.ToolTipText, "text"),
        (QPalette.ColorRole.PlaceholderText, "muted"),
        (QPalette.ColorRole.Link, "accent"),
    ):
        palette.setColor(role, QColor(c[name]))
    app.setPalette(palette)
    app.setStyleSheet(
        f"""
        QMainWindow, QDialog, QWidget#networkManagerPages {{
            background: {c["window"]};
            color: {c["text"]};
        }}
        QFrame#networkManagerSidebar {{
            background: {c["sidebar"]};
            border: 1px solid {c["border"]};
            border-radius: 10px;
        }}
        QFrame#portMatrixDetails {{
            background: {c["panel"]};
            border: 1px solid {c["border"]};
            border-radius: 9px;
        }}
        QTableView, QTreeWidget, QTableWidget, QListWidget {{
            background: {c["base"]};
            alternate-background-color: {c["alternate"]};
            color: {c["text"]};
            border: 1px solid {c["border"]};
            border-radius: 5px;
            selection-background-color: {c["accent"]};
            selection-color: {c["accent_text"]};
        }}
        QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {{
            background: {c["base"]};
            color: {c["text"]};
            border: 1px solid {c["border"]};
            border-radius: 5px;
            padding: 4px 6px;
        }}
        QPushButton, QToolButton {{
            background: {c["button"]};
            color: {c["text"]};
            border: 1px solid {c["border"]};
            border-radius: 6px;
            padding: 5px 9px;
        }}
        QPushButton:hover, QToolButton:hover {{
            border-color: {c["accent"]};
        }}
        QPushButton:disabled, QToolButton:disabled {{
            color: {c["muted"]};
        }}
        QPushButton:checked, QToolButton:checked {{
            background: {c["accent"]};
            color: {c["accent_text"]};
            border-color: {c["accent"]};
        }}
        QFrame#poeQuickActionBar {{
            background: {c["panel"]};
            border: 1px solid {c["border"]};
            border-radius: 8px;
        }}
        QFrame#themeSegmentedControl {{
            background: {c["alternate"]};
            border: 1px solid {c["border"]};
            border-radius: 10px;
        }}
        QFrame#themeSegmentedControl QPushButton#themeLight,
        QFrame#themeSegmentedControl QPushButton#themeDark {{
            margin: 0px;
            padding: 5px 10px;
            background: transparent;
            color: {c["muted"]};
            border: 1px solid transparent;
            border-radius: 0px;
            font-weight: 500;
        }}
        QFrame#themeSegmentedControl QPushButton#themeLight {{
            border-top-left-radius: 8px;
            border-bottom-left-radius: 8px;
        }}
        QFrame#themeSegmentedControl QPushButton#themeDark {{
            border-top-right-radius: 8px;
            border-bottom-right-radius: 8px;
        }}
        QFrame#themeSegmentedControl QPushButton#themeLight:hover:!checked,
        QFrame#themeSegmentedControl QPushButton#themeDark:hover:!checked {{
            background: {c["button"]};
            color: {c["text"]};
        }}
        QFrame#themeSegmentedControl QPushButton#themeLight:checked,
        QFrame#themeSegmentedControl QPushButton#themeDark:checked {{
            background: {c["accent"]};
            color: {c["accent_text"]};
            border-color: {c["accent"]};
            font-weight: 700;
        }}
        QGroupBox {{
            color: {c["text"]};
        }}
        QLabel#portMatrixSummary, QLabel#portMatrixLegend {{
            color: {c["muted"]};
        }}
        """
    )
    if persist:
        QSettings("sebia1993", "Aruba2930FNetworkManager").setValue("appearance/theme", mode)
