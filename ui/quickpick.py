"""
Quick-pick: a small frameless search-and-pick window over the clipboard history, opened by the
global hotkey `hotkeys.copy_quickpick`.

Built once at startup and shown/hidden after that. The history is cached in memory and only
re-read when ClipStore.version moves, so opening never touches SQLite in the common case.
Open time (hotkey press to window shown) is logged against the 150 ms target.
"""
import ctypes
import time

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QLabel

from .icons import glyph_icon
from .styles import T, build_stylesheet
from .views.copy import one_line
from .widgets import kbd

SHOWN = 50  # rows listed; filtering still searches every cached clip
TARGET_MS = 150
SIZE = (520, 380)


def force_foreground(hwnd: int):
    """A hotkey press from another app does not entitle us to focus: borrow its input queue briefly."""
    u = ctypes.windll.user32
    fg = u.GetForegroundWindow()
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    them = u.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(them and them != me and u.AttachThreadInput(me, them, True))
    try:
        u.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(me, them, False)


class QuickPick(QFrame):
    def __init__(self, store, paste_fn):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(build_stylesheet())
        self.store, self._paste = store, paste_fn
        self._clips, self._ver, self._target = [], -1, 0
        self.last_open_ms = 0.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("Palette")
        outer.addWidget(panel)
        v = QVBoxLayout(panel)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Search your clipboard history")
        self.input.setMinimumHeight(36)
        self.input.addAction(glyph_icon("search", 14, T["ink_dim"]), QLineEdit.ActionPosition.LeadingPosition)
        self.input.textChanged.connect(self._filter)
        self.input.installEventFilter(self)
        v.addWidget(self.input)
        self.list = QListWidget()
        self.list.itemClicked.connect(self._pick)
        v.addWidget(self.list, 1)
        foot = QHBoxLayout()
        foot.setSpacing(6)
        for key, what in (("Enter", "paste"), ("Up/Down", "move"), ("Esc", "close")):
            foot.addWidget(kbd(key))
            lbl = QLabel(what)
            lbl.setObjectName("Meta")
            foot.addWidget(lbl)
            foot.addSpacing(8)
        foot.addStretch(1)
        v.addLayout(foot)
        self.setFixedSize(*SIZE)
        self._pin_icon = glyph_icon("pin", 14, T["gold"])
        self._clip_icon = glyph_icon("copy", 14, T["ink_dim"])
        self.winId()  # create the native window now, not on the first hotkey press

    def _reload(self):
        self._clips = self.store.recent("", 500)
        self._ver = self.store.version

    def _filter(self, text=""):
        q = text.strip().lower()
        self.list.clear()
        shown = 0
        for c in self._clips:
            if q and q not in c["text"].lower():
                continue
            item = QListWidgetItem(self._pin_icon if c["pinned"] else self._clip_icon, one_line(c["text"], 120))
            item.setData(Qt.ItemDataRole.UserRole, c["text"])
            item.setToolTip(c["text"][:600])
            self.list.addItem(item)
            shown += 1
            if shown >= SHOWN:
                break
        if shown:
            self.list.setCurrentRow(0)

    def open_near_cursor(self, target_hwnd: int = 0, t0: float = None):
        """`t0` is the perf_counter() at the hotkey press, so the logged time covers the whole path."""
        t0 = t0 or time.perf_counter()
        self._target = target_hwnd
        if self._ver != self.store.version:
            self._reload()
        self.input.blockSignals(True)
        self.input.clear()
        self.input.blockSignals(False)
        self._filter("")
        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(area.left(), pos.x() + 8), area.right() - self.width())
        y = min(max(area.top(), pos.y() + 8), area.bottom() - self.height())
        self.move(x, y)
        self.show()
        self.raise_()
        try:
            force_foreground(int(self.winId()))
        except Exception:
            pass
        self.activateWindow()
        self.input.setFocus()
        self.last_open_ms = (time.perf_counter() - t0) * 1000
        print(f"[LeonidasScribe QuickPick] opened in {self.last_open_ms:.0f} ms "
              f"({len(self._clips)} clips cached, target {TARGET_MS} ms)")

    def eventFilter(self, obj, ev):
        if obj is self.input and ev.type() == QEvent.Type.KeyPress:
            key = ev.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.list.currentRow() + (1 if key == Qt.Key.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.list.currentItem()
                if item:
                    self._pick(item)
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, ev)

    def _pick(self, item):
        text = item.data(Qt.ItemDataRole.UserRole)
        self.hide()
        self._paste(text, self._target)
