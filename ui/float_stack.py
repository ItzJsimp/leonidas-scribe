"""
One bottom-right stack for every floating card Scribe shows outside its window (the voice
card, the meetings call prompt). Each card asks place(); visible cards stack upward with a
gap in show order, and the stack closes up when one hides, so two never sit on top of each other.
"""
import weakref

from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import QApplication

MARGIN_X, MARGIN_Y, GAP = 24, 48, 12
_items: list = []  # weakrefs, oldest (bottom) first


def _alive():
    out = []
    for r in _items:
        w = r()
        try:
            if w is not None and w.isVisible():
                out.append(w)
        except RuntimeError:  # C++ side already deleted
            pass
    return out


class _HideWatch(QObject):
    def eventFilter(self, obj, e):
        if e.type() in (QEvent.Type.Hide, QEvent.Type.Resize):
            relayout()
        return False


_watch = _HideWatch()


def place(w) -> None:
    """Add w to the stack (if new) and lay every visible card out. Call it instead of move()."""
    if not any(r() is w for r in _items):
        _items.append(weakref.ref(w))
        w.installEventFilter(_watch)
    relayout(extra=w)


def relayout(extra=None) -> None:
    scr = QApplication.primaryScreen()
    if scr is None:
        return
    area = scr.availableGeometry()
    live = _alive()
    if extra is not None and extra not in live:
        live.append(extra)  # being placed just before show()
    _items[:] = [r for r in _items if r() is not None]  # keep hidden cards registered; drop only dead ones
    y = area.y() + area.height() - MARGIN_Y
    for w in live:
        h = w.height()
        w.move(area.x() + area.width() - w.width() - MARGIN_X, y - h)
        y -= h + GAP
