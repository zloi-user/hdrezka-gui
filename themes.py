#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Светлая и тёмная темы приложения."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    window: str
    base: str
    alt: str
    text: str
    muted: str
    disabled: str
    border: str
    highlight: str
    highlight_text: str
    accent: str
    accent_hover: str
    accent_text: str
    link: str
    rating: str
    group_bg: str
    group_border: str
    group_text: str
    placeholder_bg: str
    placeholder_text: str
    button_bg: str
    button_hover: str
    button_pressed: str
    status_checking_bg: str
    status_checking_text: str
    status_video_bg: str
    status_video_text: str
    status_site_bg: str
    status_site_text: str
    status_error_bg: str
    status_error_text: str
    status_stale_bg: str
    status_stale_text: str


LIGHT = Theme(
    key="light",
    label="Светлая",
    window="#f0f0f0",
    base="#ffffff",
    alt="#f7f8f9",
    text="#1f2328",
    muted="#777777",
    disabled="#a0a0a0",
    border="#c9ced3",
    highlight="#2f80c8",
    highlight_text="#ffffff",
    accent="#2a9d8f",
    accent_hover="#238578",
    accent_text="#ffffff",
    link="#2a6f9e",
    rating="#b16286",
    group_bg="#dbe5eb",
    group_border="#c5d3db",
    group_text="#3f5966",
    placeholder_bg="#e7edf0",
    placeholder_text="#5b6b73",
    button_bg="#f7f7f7",
    button_hover="#ffffff",
    button_pressed="#e8e8e8",
    status_checking_bg="#fff3cd",
    status_checking_text="#3f3200",
    status_video_bg="#d4edda",
    status_video_text="#1b5e20",
    status_site_bg="#ffe5b4",
    status_site_text="#7a4b00",
    status_error_bg="#f8d7da",
    status_error_text="#842029",
    status_stale_bg="#eeeeee",
    status_stale_text="#888888",
)

DARK = Theme(
    key="dark",
    label="Тёмная",
    window="#1e1f22",
    base="#25272b",
    alt="#2b2d31",
    text="#e6e8eb",
    muted="#a4a9af",
    disabled="#6f747a",
    border="#3d4147",
    highlight="#3f7fae",
    highlight_text="#ffffff",
    accent="#45c4b4",
    accent_hover="#5bd3c3",
    accent_text="#0d211d",
    link="#7ab8e0",
    rating="#e08ab0",
    group_bg="#2a3a44",
    group_border="#3b5360",
    group_text="#b8d0dc",
    placeholder_bg="#2c3338",
    placeholder_text="#a9bcc5",
    button_bg="#2f3237",
    button_hover="#3a3e44",
    button_pressed="#272a2e",
    status_checking_bg="#4a3b16",
    status_checking_text="#ffd166",
    status_video_bg="#1d3b29",
    status_video_text="#9be3b0",
    status_site_bg="#4a3318",
    status_site_text="#ffc078",
    status_error_bg="#451f24",
    status_error_text="#ff9aa8",
    status_stale_bg="#33363a",
    status_stale_text="#a0a4aa",
)

THEMES: Dict[str, Theme] = {
    "light": LIGHT,
    "dark": DARK,
}

_CURRENT: Theme = LIGHT
_SYSTEM_KEY: Optional[str] = None


def current_theme() -> Theme:
    return _CURRENT


def capture_system_theme(app: QApplication):
    """Запоминает системную тему до смены стиля/палитры приложения."""
    global _SYSTEM_KEY
    if _SYSTEM_KEY is None:
        _SYSTEM_KEY = detect_system_theme(app)


def detect_system_theme(app: QApplication) -> str:
    """Определяет системную тему один раз, до применения нашей палитры."""
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
        if scheme == Qt.ColorScheme.Light:
            return "light"
    except Exception:
        pass
    try:
        window = app.palette().color(QPalette.ColorRole.Window)
        if window.lightness() < 128:
            return "dark"
    except Exception:
        pass
    return "light"


def resolve_theme(app: QApplication, key: str) -> str:
    global _SYSTEM_KEY
    if _SYSTEM_KEY is None:
        _SYSTEM_KEY = detect_system_theme(app)
    if key in THEMES:
        return key
    return _SYSTEM_KEY


def _build_qss(t: Theme) -> str:
    return f"""
QWidget {{
    color: {t.text};
}}
QMainWindow, QDialog {{
    background: {t.window};
}}
QToolTip {{
    background: {t.base};
    color: {t.text};
    border: 1px solid {t.border};
    padding: 4px;
}}
QStatusBar {{
    background: {t.window};
    color: {t.muted};
}}
QTreeWidget, QTableWidget {{
    background: {t.base};
    alternate-background-color: {t.alt};
    color: {t.text};
    border: 1px solid {t.border};
}}
QHeaderView::section {{
    background: {t.window};
    color: {t.text};
    border: 1px solid {t.border};
    padding: 4px;
}}
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background: {t.base};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 4px;
    padding: 3px;
    selection-background-color: {t.highlight};
    selection-color: {t.highlight_text};
}}
QComboBox QAbstractItemView {{
    background: {t.base};
    color: {t.text};
    selection-background-color: {t.highlight};
    selection-color: {t.highlight_text};
}}
QPushButton {{
    background: {t.button_bg};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 4px;
    padding: 5px 9px;
}}
QPushButton:hover {{
    background: {t.button_hover};
}}
QPushButton:pressed {{
    background: {t.button_pressed};
}}
QPushButton:default {{
    border: 1px solid {t.accent};
}}
QCheckBox, QRadioButton {{
    color: {t.text};
}}
QScrollBar:vertical {{
    background: {t.base};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t.border};
    min-height: 20px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t.muted};
}}
QScrollBar:horizontal {{
    background: {t.base};
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {t.border};
    min-width: 20px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {t.muted};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}
QSplitter::handle {{
    background: {t.border};
}}
QMenu {{
    background: {t.base};
    color: {t.text};
    border: 1px solid {t.border};
}}
QMenu::item:selected {{
    background: {t.highlight};
    color: {t.highlight_text};
}}
"""


def apply_theme(app: QApplication, key: str) -> str:
    """Применяет тему и возвращает фактический ключ (light/dark)."""
    global _CURRENT
    actual = resolve_theme(app, key)
    theme = THEMES[actual]
    _CURRENT = theme

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.window))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Base, QColor(theme.base))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme.alt))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(theme.base))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme.window))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.highlight))
    palette.setColor(
        QPalette.ColorRole.HighlightedText, QColor(theme.highlight_text)
    )
    palette.setColor(QPalette.ColorRole.Link, QColor(theme.link))
    try:
        palette.setColor(
            QPalette.ColorRole.PlaceholderText, QColor(theme.muted)
        )
    except AttributeError:
        pass
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(
            QPalette.ColorGroup.Disabled,
            role,
            QColor(theme.disabled),
        )

    app.setPalette(palette)
    app.setStyleSheet(_build_qss(theme))
    return actual
