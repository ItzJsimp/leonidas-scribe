"""
Copy: searchable clipboard history (clipboard_history.ClipStore) with pin, delete, pause and clear.

Click a row, or press Enter in the search box, to paste that clip into the app you were in.
Up/Down in the search box move the highlight. New copies appear live via ClipWatcher.clip_added.
"""
import ctypes
import os
import time
from ctypes import wintypes

from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel, QFrame, QMessageBox

from ..icons import glyph_icon
from ..styles import T
from ..widgets import ElidingLabel, EmptyState, page_header, button, icon_button

SHOWN = 100  # rows rendered; search reaches the whole store


def ago(ts: float, now: float = None) -> str:
    s = max(0, int((now or time.time()) - ts))
    if s < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if s >= size:
            return f"{s // size}{unit} ago"
    return ""


def one_line(text: str, n: int = 240) -> str:
    return " ".join(text[: n * 2].split())[:n]


def previous_app_hwnd(own: int) -> int:
    """The window under ours in z-order that looks like a real app: where the paste should land."""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetWindow.restype = ctypes.c_void_p
    u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    dwm = ctypes.WinDLL("dwmapi")
    h = u.GetWindow(own, 2)  # GW_HWNDNEXT
    for _ in range(300):
        if not h:
            return 0
        cloaked, pid = ctypes.c_int(0), wintypes.DWORD(0)
        dwm.DwmGetWindowAttribute(ctypes.c_void_p(h), 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if (u.IsWindowVisible(h) and u.GetWindowTextLengthW(h) > 0 and not cloaked.value
                and not u.GetWindowLongW(h, -20) & 0x80  # WS_EX_TOOLWINDOW
                and pid.value != os.getpid()):
            return h
        h = u.GetWindow(h, 2)
    return 0


class ClipRow(QFrame):
    def __init__(self, clip: dict, on_paste, on_pin, on_delete):
        super().__init__()
        self.clip = clip
        self.setObjectName("ClipRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("pinned", "true" if clip["pinned"] else "false")
        self.setProperty("sel", "false")
        self._on_paste = on_paste
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 8, 8)
        h.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.text = ElidingLabel(one_line(clip["text"]))
        self.text.setObjectName("ClipText")
        col.addWidget(self.text)
        n = len(clip["text"])
        meta = QLabel(f"{ago(clip['ts'])}  ·  {n:,} characters")
        meta.setObjectName("Meta")
        col.addWidget(meta)
        h.addLayout(col, 1)
        self.btn_pin = icon_button("pin", tooltip="Unpin" if clip["pinned"] else "Pin to the top",
                                   color=T["gold"] if clip["pinned"] else None)
        self.btn_pin.clicked.connect(lambda: on_pin(clip))
        self.btn_del = icon_button("delete", tooltip="Delete this clip")
        self.btn_del.clicked.connect(lambda: on_delete(clip))
        h.addWidget(self.btn_pin)
        h.addWidget(self.btn_del)
        self.setToolTip(clip["text"][:600])

    def set_selected(self, on: bool):
        self.setProperty("sel", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._on_paste(self.clip)
        super().mousePressEvent(e)


class CopyView(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.rows: list[ClipRow] = []
        self.sel = 0
        self._stale = True
        self.store = None
        self.watcher = None

        scroll, content = host.scroll_host(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        v = QVBoxLayout(content)
        v.setContentsMargins(28, 24, 24, 28)
        v.setSpacing(12)

        self.btn_pause = button("Pause recording", "GhostBtn", "pause",
                                "Stop recording new copies until you switch this off")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._set_paused)
        self.btn_clear = button("Clear history", "DangerBtn", "delete", "Delete every clip that is not pinned")
        self.btn_clear.clicked.connect(self._clear)
        v.addLayout(page_header("Copy", "Everything you copy, newest first. Click a clip or press Enter to paste it "
                                        "into the app you were just in.", (self.btn_pause, self.btn_clear)))

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your clipboard history")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(36)
        self.search.addAction(glyph_icon("search", 14, T["ink_dim"]), QLineEdit.ActionPosition.LeadingPosition)
        self.search.installEventFilter(self)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(80)
        self._debounce.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _t: self._debounce.start())
        self.search.returnPressed.connect(self._paste_selected)
        v.addWidget(self.search)

        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        v.addLayout(self.list)
        self.empty = EmptyState("copy", "Nothing copied yet", "Copy something anywhere and it shows up here.")
        v.addWidget(self.empty)
        v.addStretch(1)

    # ----- wiring
    def bind(self, store, watcher):
        self.store, self.watcher = store, watcher
        self.btn_pause.blockSignals(True)
        self.btn_pause.setChecked(not watcher.enabled)
        self.btn_pause.blockSignals(False)
        watcher.clip_added.connect(self._on_clip_added)
        self._stale = True
        if self.isVisible():
            self.refresh()

    def _on_clip_added(self, _id):
        if self.isVisible():
            self._debounce.start()
        else:
            self._stale = True

    def showEvent(self, e):
        super().showEvent(e)
        if self._stale:
            self.refresh()
        self.search.setFocus()

    # ----- list
    def refresh(self):
        if self.store is None:
            return
        self._stale = False
        for r in self.rows:
            self.list.removeWidget(r)
            r.setParent(None)
            r.deleteLater()
        clips = self.store.recent(self.search.text().strip(), SHOWN)
        self.rows = [ClipRow(c, self._paste, self._pin, self._delete) for c in clips]
        for r in self.rows:
            self.list.addWidget(r)
        self.sel = min(self.sel, max(0, len(self.rows) - 1))
        self._mark()
        searching = bool(self.search.text().strip())
        self.empty.setVisible(not self.rows)
        self.empty.lbl_title.setText("No matches" if searching else "Nothing copied yet")
        self.empty.lbl_body.setText("Try a different word." if searching else
                                    "Copy something anywhere and it shows up here.")

    def _mark(self):
        for i, r in enumerate(self.rows):
            r.set_selected(i == self.sel)

    def eventFilter(self, obj, ev):
        if obj is self.search and ev.type() == QEvent.Type.KeyPress and self.rows:
            step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1}.get(ev.key())
            if step:
                self.sel = max(0, min(len(self.rows) - 1, self.sel + step))
                self._mark()
                return True
        return super().eventFilter(obj, ev)

    # ----- actions
    def _paste_selected(self):
        if self.rows:
            self._paste(self.rows[self.sel].clip)

    def _paste(self, clip):
        self.host.paste_clip(clip["text"])

    def _pin(self, clip):
        self.store.pin(clip["id"], not clip["pinned"])
        self.refresh()

    def _delete(self, clip):
        self.store.delete(clip["id"])
        self.refresh()

    def _confirm_clear(self) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, "Clear history",
                          "Delete every clip that is not pinned? Pinned clips stay.",
                          QMessageBox.StandardButton.Cancel, self)
        ok = box.addButton("Clear history", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is ok

    def _clear(self):
        if self.store is not None and self._confirm_clear():
            self.store.clear()
            self.refresh()
            self.host.toast("Clipboard history cleared. Pinned clips were kept.", "success")

    def _set_paused(self, paused: bool):
        if self.watcher is not None:
            self.watcher.enabled = not paused
        self.host.save_clipboard_enabled(not paused)
        self.host.toast("Recording paused. New copies are not saved." if paused else "Recording copies again.",
                        "warn" if paused else "success")
