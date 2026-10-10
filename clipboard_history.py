"""Local clipboard history for the Copy tab (slice 1): SQLite store + QClipboard watcher.

Honors the Windows "exclude from monitoring" clipboard format, so password managers and
Scribe's own dictation paste (clipboard.py sets it) never land in the history.
"""
import sqlite3
import threading
import time

from PySide6.QtCore import QObject, Signal

from clipboard import CF_EXCLUDE_MONITOR, user32

MAX_CHARS = 100_000  # longer copies are almost never meant to be recalled
KEEP = 2000  # unpinned clips kept; pinned clips are never pruned


class ClipStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS clips("
            "id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, ts REAL NOT NULL, "
            "pinned INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'local')"
        )
        self._db.commit()

    def add(self, text: str, source: str = "local") -> int:
        """Insert a clip, or move an identical one back to the top. Returns its id."""
        with self._lock:
            self._db.execute(
                "INSERT INTO clips(text, ts, source) VALUES(?,?,?) "
                "ON CONFLICT(text) DO UPDATE SET ts=excluded.ts",
                (text, time.time(), source),
            )
            self._db.execute(
                "DELETE FROM clips WHERE pinned=0 AND id NOT IN "
                "(SELECT id FROM clips WHERE pinned=0 ORDER BY ts DESC LIMIT ?)",
                (KEEP,),
            )
            self._db.commit()
            return self._db.execute("SELECT id FROM clips WHERE text=?", (text,)).fetchone()[0]

    def recent(self, query: str = "", limit: int = 200) -> list[dict]:
        """Pinned first, then newest. `query` is a case-insensitive substring match."""
        # ponytail: LIKE scan is fine at KEEP=2000 rows; move to FTS5 if the cap is raised a lot
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text, ts, pinned, source FROM clips WHERE text LIKE ? ESCAPE '\\' "
                "ORDER BY pinned DESC, ts DESC LIMIT ?",
                (like, limit),
            ).fetchall()
        return [dict(zip(("id", "text", "ts", "pinned", "source"), r)) for r in rows]

    def pin(self, clip_id: int, pinned: bool = True):
        with self._lock:
            self._db.execute("UPDATE clips SET pinned=? WHERE id=?", (int(pinned), clip_id))
            self._db.commit()

    def delete(self, clip_id: int):
        with self._lock:
            self._db.execute("DELETE FROM clips WHERE id=?", (clip_id,))
            self._db.commit()


def clipboard_is_excluded() -> bool:
    return bool(user32.IsClipboardFormatAvailable(CF_EXCLUDE_MONITOR))


class ClipWatcher(QObject):
    """Records every text copy via QClipboard.dataChanged (event driven, no polling)."""

    clip_added = Signal(int)

    def __init__(self, store: ClipStore, clipboard):
        super().__init__()
        self.store = store
        self.enabled = True
        clipboard.dataChanged.connect(lambda: self._on_change(clipboard))

    def _on_change(self, clipboard):
        if not self.enabled or clipboard_is_excluded():
            return
        text = clipboard.text()
        if text and text.strip() and len(text) <= MAX_CHARS:
            self.clip_added.emit(self.store.add(text))
