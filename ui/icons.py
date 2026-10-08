"""
App icon plus a monochrome glyph set for Leonidas Scribe.

The app icon is the one Leonidas avatar, installed as icon.ico / icon.png by the
brand-icons skill (.agents/skills/brand-icons). Nothing here paints its own mark.

Glyphs come from Segoe Fluent Icons (Win11) with a Segoe MDL2 Assets fallback
(Win10): both ship with Windows, so there are no icon files or dependencies,
and single-colour glyphs follow the token palette where emoji cannot.
"""
import os
import sys
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QIcon, QFont, QColor, QPainter, QPixmap, QFontDatabase

from .styles import T

# Segoe MDL2 / Fluent codepoints (present in both fonts).
GLYPH = {
    "play":      "\uE768",
    "pause":     "\uE769",
    "chevron_right": "\uE76C",
    "chevron_left":  "\uE76B",
    "folder":    "\uE8B7",
    "people":    "\uE716",
    "calendar":  "\uE787",
    "clock":     "\uE823",
    "expand":    "\uE740",
    "pin":       "\uE718",
    "send":      "\uE724",
    "upload":    "\uE898",
    "stop":      "\uE71A",
    "home":      "\uE80F",
    "bulb":      "\uEA80",
    "mic":       "",
    "history":   "",
    "library":   "",
    "settings":  "",
    "search":    "",
    "refresh":   "",
    "copy":      "",
    "close":     "",
    "add":       "",
    "edit":      "",
    "save":      "",
    "undo":      "",
    "check":     "",
    "delete":    "",
    "keyboard":  "",
    "info":      "",
    "chart":     "",
    "warning":   "",
    "volume":    "",
    "flash":     "",
    "shuffle":   "",
    "sync":      "",
}

_ICON_FONT_CANDIDATES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
_cache = {}
_icon_family = None


def icon_font_family() -> str:
    global _icon_family
    if _icon_family is None:
        available = set(QFontDatabase.families())
        _icon_family = next(
            (f for f in _ICON_FONT_CANDIDATES if f in available),
            _ICON_FONT_CANDIDATES[-1],
        )
    return _icon_family


def glyph_icon(name: str, size: int = 16, color: str = None) -> QIcon:
    """Render a named glyph to a QIcon. Cached per (name, size, colour)."""
    color = color or T["ink_muted"]
    key = (name, size, color)
    if key in _cache:
        return _cache[key]

    ratio = 2  # render at 2x so the glyph stays crisp on scaled displays
    pix = QPixmap(size * ratio, size * ratio)
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    # With devicePixelRatio set the painter works in LOGICAL units.
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = QFont(icon_font_family())
    font.setPixelSize(max(int(size * 0.86), 8))
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter,
                     GLYPH.get(name, GLYPH["info"]))
    painter.end()

    icon = QIcon(pix)
    _cache[key] = icon
    return icon


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app_icon_pixmap(size: int = 64) -> QPixmap:
    """The Leonidas avatar at `size` logical px, from the installed icon.png."""
    pix = QPixmap(os.path.join(_base_dir(), "icon.png"))
    if pix.isNull():
        pix = QPixmap(size, size)
        pix.fill(QColor(T["obsidian_850"]))
        return pix
    return pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)


def get_app_icon() -> QIcon:
    base = _base_dir()
    for name in ("icon.ico", "icon.png"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return QIcon(path)
    return QIcon(create_app_icon_pixmap())
