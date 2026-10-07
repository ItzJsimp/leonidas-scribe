"""
Leonidas Scribe - sequential dictation stitching decision (pure, no Qt, no network).

Decides whether a new dictation continues the previous paste (leading space plus
recent_context for polish). False merge across sessions is the failure to prevent:
every guard must pass, any doubt returns False.
"""
import os
import re
import threading
import time
from datetime import datetime
from statistics import quantiles

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.jsonl")

FALLBACK_WINDOW = 45.0
MIN_WINDOW, MAX_WINDOW = 20.0, 90.0
MIN_SAMPLES = 20
MAX_GAP = 600.0
REFRESH_SECONDS = 600.0

CONTINUATION_WORDS = ("and", "but", "so", "because", "or", "then", "which")

_TS_RE = re.compile(r'^\{"timestamp": "([^"]+)"')
_APP_RE = re.compile(r'"app": "([^"\\]*)"')


def learned_window(app, rows):
    """p75 of consecutive same-app gaps (0 < gap < 600s) clamped to 20-90s, else 45s.

    rows: dicts with "timestamp" (ISO) and "app", oldest first. Rows without an app
    break adjacency, so a gap is only counted between two adjacent rows of this app."""
    gaps, prev_ts, prev_app = [], None, None
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"]).timestamp()
        except Exception:
            prev_ts, prev_app = None, None
            continue
        cur_app = r.get("app") or None
        if cur_app == app and prev_app == app and prev_ts is not None and 0 < ts - prev_ts < MAX_GAP:
            gaps.append(ts - prev_ts)
        prev_ts, prev_app = ts, cur_app
    if not app or len(gaps) < MIN_SAMPLES:
        return FALLBACK_WINDOW
    p75 = quantiles(gaps, n=4, method="inclusive")[2]
    return max(MIN_WINDOW, min(MAX_WINDOW, p75))


def _read_rows(path):
    """timestamp + app per history line via regex, no json.loads: a worker thread must
    not allocate thousands of dicts (cyclic GC vs dead PySide wrappers, playbook 2026-09-28)."""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = _TS_RE.match(line)
            if not m:
                continue
            apps = _APP_RE.findall(line)  # top-level "app" is the last one on the line
            rows.append({"timestamp": m.group(1), "app": apps[-1] if apps else None})
    return rows


class WindowCache:
    """Per-app windows from history.jsonl, refreshed in the background at most every 10 min.
    window_for never blocks: it returns the cached value or the 45s fallback."""

    def __init__(self, path=HISTORY_PATH, refresh_seconds=REFRESH_SECONDS):
        self.path = path
        self.refresh_seconds = refresh_seconds
        self._windows = {}
        self._rows = None
        self._loaded_at = None
        self._lock = threading.Lock()
        self._loading = False

    def refresh(self):
        """Synchronous load (tests, or called from the background thread)."""
        try:
            rows = _read_rows(self.path)
        except Exception:
            rows = None
        with self._lock:
            if rows is not None:
                self._rows = rows
                self._windows = {}
            self._loaded_at = time.monotonic()
            self._loading = False

    def warm(self):
        """Kick a background refresh unless one is running or the cache is fresh."""
        with self._lock:
            fresh = self._loaded_at is not None and time.monotonic() - self._loaded_at < self.refresh_seconds
            if fresh or self._loading:
                return
            self._loading = True
        threading.Thread(target=self.refresh, daemon=True, name="stitch-window-refresh").start()

    def window_for(self, app):
        self.warm()
        with self._lock:
            if self._rows is None:
                return FALLBACK_WINDOW
            if app not in self._windows:
                self._windows[app] = learned_window(app, self._rows)
            return self._windows[app]


_cache = WindowCache()


def warm():
    _cache.warm()


def window_for(app):
    return _cache.window_for(app)


def _starts_new_thought(prev_text, new_raw):
    # ponytail: heuristic sentence guard, a finished sentence followed by a capitalised
    # non-continuation start is treated as a new thought; misses real continuations that
    # start with a capital (names, "I"), upgrade to an LLM/clause check if that bites.
    if not prev_text.rstrip().endswith((".", "?", "!")):
        return False
    new = new_raw.lstrip()
    if not new[:1].isupper():
        return False
    first = re.match(r"[A-Za-z']+", new)
    return not (first and first.group(0).lower() in CONTINUATION_WORDS)


def _guards_pass(prev, now, last_user_input, window):
    """Every should_merge guard except the sentence guard. Returns the stripped prev
    text when all pass, else None. Raises on malformed input (callers catch)."""
    prev_text = (prev.get("text") or "").strip()
    if not prev_text:
        return None
    if not prev.get("hwnd") or prev.get("hwnd") != now.get("hwnd"):
        return None
    if not prev.get("title") or prev.get("title") != now.get("title"):
        return None
    if last_user_input > prev["time"]:
        return None
    if window is None:
        window = window_for(now.get("app"))
    gap = now["time"] - prev["time"]
    if not (0 <= gap < window):
        return None
    return prev_text


def preview_merge(prev, now, last_user_input=0.0, window=None):
    """Would a dictation started now probably join the last paste? Same guards as
    should_merge minus the sentence guard (the raw text is unknown while recording)."""
    try:
        return _guards_pass(prev, now, last_user_input, window) is not None
    except Exception:
        return False


def should_merge(prev, now, last_user_input=0.0, window=None):
    """True only when every guard passes.

    prev: {"text", "hwnd", "title", "time"} of the last paste.
    now:  {"raw", "hwnd", "title", "time", "app"} of the new dictation (raw_text:
          polish has not run yet at decision time).
    last_user_input: time of the last real (non-injected) keyboard/mouse input.
    window: seconds, defaults to the learned window for now["app"]."""
    try:
        prev_text = _guards_pass(prev, now, last_user_input, window)
        if prev_text is None:
            return False
        return not _starts_new_thought(prev_text, now.get("raw") or "")
    except Exception:
        return False
