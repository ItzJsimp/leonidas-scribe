"""
MeetingsView (Slice S6): gallery, side sheet, expanded overlay, recording pill, call prompt.

Reads the real meeting store (meetings/<YYYY-MM-DD-HHMM>-<slug>/manifest.json, transcript.json,
note.json, status.json). SCRIBE_UI_DEMO=1 swaps in a neutral demo store (render tests and
screenshots only); SCRIBE_MEETINGS_DIR points the view at another store (tests).

Threading (agent-playbook 5b): every disk read, subprocess and worker command runs through
ui.widgets.run_async; callbacks land on the GUI thread, which is the only thread that touches
widgets. Styling lives in ui/styles.py (object names + [property] selectors, no inline sheets).
"""
import datetime
import os
import re
import shutil
import tempfile
import time

from PySide6.QtCore import Qt, Signal, QTimer, QRect, QUrl, QDateTime, QPoint
from PySide6.QtGui import QColor, QPainter, QKeySequence, QShortcut, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QComboBox, QSlider,
    QFrame, QStackedWidget, QButtonGroup, QInputDialog, QGridLayout, QSizePolicy, QMessageBox,
    QFileDialog, QDateTimeEdit, QAbstractSpinBox, QApplication, QMenu, QStyle,
)

import meeting_recorder as mr
import meeting_store as mst
import meeting_speakers as msp
import meeting_detector as mdt
import meeting_pipeline as mp
from .. import motion, float_stack
from ..icons import glyph_icon, create_app_icon_pixmap
from ..styles import T, DARK_STYLESHEET
from ..widgets import (
    page_header, section_label, button, icon_button, EmptyState, ElidingLabel, run_async,
)

SCRIBE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEMPLATES_DIR = os.path.join(SCRIBE_DIR, "meeting_templates")
HEARTBEAT_DEAD_S = 10.0
SPEEDS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
ASK_EST = "Free"
BROWSE = "__browse__"


# ---------------------------------------------------------------------------
# Store (pure functions: no widgets, safe on worker threads, unit-tested)
# ---------------------------------------------------------------------------
def store_root() -> str:
    if os.environ.get("SCRIBE_UI_DEMO") == "1":
        return demo_store()
    return os.environ.get("SCRIBE_MEETINGS_DIR") or os.path.join(SCRIBE_DIR, "meetings")


def _parse_iso(ts):
    return mst._parse_iso(ts)


def _local(ts):
    dt = _parse_iso(ts)
    return dt.astimezone() if dt else None


def fmt_length(seconds: float) -> str:
    s = int(round(seconds or 0))
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else f"{s}s"


def fmt_card_date(ts) -> str:
    dt = _local(ts)
    return f"{dt:%b} {dt.day}" if dt else ""


def fmt_long_date(ts) -> str:
    dt = _local(ts)
    return f"{dt:%b} {dt.day}, {dt.year} {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}" if dt else ""


def _chan(name) -> str:
    """'〔🔊〕・Lounge' -> 'Lounge': drop the decoration servers put in front of channel names."""
    return re.sub(r"^\W+", "", name or "").strip() or (name or "")


def discord_hops(m: dict) -> list:
    """Voice channels this meeting was in, in order: [{guild, channel, t}] (channel switches included)."""
    hops = [h for h in m.get("discord_channels") or [] if h.get("channel")]
    d = m.get("discord") or {}
    return hops or ([dict(d, t=0.0)] if d.get("channel") else [])


def fmt_place(m: dict) -> str:
    """'Friends › Lounge', then '→ General' per switch; '' when no Discord channel is known."""
    hops = discord_hops(m)
    if not hops:
        return ""
    first = hops[0]
    out = f"{first.get('guild') or 'Direct Message'} › {_chan(first.get('channel'))}"
    for prev, h in zip(hops, hops[1:]):
        out += f"  →  {'' if h.get('guild') == prev.get('guild') else (h.get('guild') or '') + ' › '}{_chan(h.get('channel'))}"
    return out


def fmt_place_tip(m: dict) -> str:
    return "\n".join(f"{mst.format_timestamp(h.get('t') or 0)}  {h.get('guild') or 'Direct Message'} › {_chan(h.get('channel'))}"
                     for h in discord_hops(m))


def _audio_files(mdir: str) -> list:
    return [p for p in (os.path.join(mdir, "audio", "them.opus"), os.path.join(mdir, "audio", "me.opus"))
            if os.path.exists(p)]


def _duration(man: dict, mdir: str, segments: list) -> float:
    a, b = _parse_iso(man.get("started")), _parse_iso(man.get("ended"))
    if a and b and b > a:
        return (b - a).total_seconds()
    saved = max(mr.saved_audio_seconds(mdir, "me"), mr.saved_audio_seconds(mdir, "them"))
    if saved:
        return saved
    if segments:
        return max(float(s.get("t1", s.get("t0", 0.0)) or 0.0) for s in segments)
    files = _audio_files(mdir)
    if files:
        try:
            import soundfile as sf
            return float(sf.info(files[0]).duration)
        except Exception:
            pass
    return 0.0


def speakers_of(man: dict, segments: list) -> list:
    out = []
    for p in man.get("participants") or []:
        n = p.get("name") or p.get("key")
        if n and n not in out:
            out.append(n)
    for s in segments:
        for n in (s.get("speaker_key") or "").split(" + "):
            if n and n not in out:
                out.append(n)
    return out


def load_meeting(mdir: str, retention_days: int = 30) -> dict | None:
    man = mr.read_manifest(mdir)
    if not man:
        return None
    segments = mr._read_json(os.path.join(mdir, "transcript.json")) or []
    if not isinstance(segments, list):
        segments = []
    note = mr._read_json(os.path.join(mdir, "note.json")) or {}
    status = mr.read_status(mdir) if man.get("state") in ("recording", "paused") else None
    hb_age = (time.time() - float(status.get("heartbeat_at", 0))) if status else None
    avatars = {p.get("name") or p.get("key"): p.get("avatar_path") for p in man.get("participants") or []}
    return {
        **man,
        "dir": mdir,
        "segments": segments,
        "note": note if isinstance(note, dict) else {},
        "duration_s": _duration(man, mdir, segments),
        "speakers": speakers_of(man, segments),
        "avatars": {k: v for k, v in avatars.items() if v and os.path.exists(v)},
        "retention_badge": mst.audio_retention_badge(man, retention_days),
        "audio": _audio_files(mdir),
        "hb_age": hb_age,
    }


def load_store(root: str, retention_days: int = 30) -> list:
    if not os.path.isdir(root):
        return []
    out = []
    for entry in os.listdir(root):
        mdir = os.path.join(root, entry)
        if entry.startswith(".") or not os.path.isdir(mdir):
            continue
        m = load_meeting(mdir, retention_days)
        if m:
            out.append(m)
    out.sort(key=lambda m: m.get("started") or "", reverse=True)
    return out


def month_cost(root: str, now: datetime.datetime | None = None) -> float:
    """Dollars logged in manifests (costs[].usd) for meetings started this calendar month."""
    now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone()
    total = 0.0
    if not os.path.isdir(root):
        return 0.0
    for entry in os.listdir(root):
        man = mr.read_manifest(os.path.join(root, entry)) if not entry.startswith(".") else None
        dt = _local((man or {}).get("started"))
        if dt and (dt.year, dt.month) == (now.year, now.month):
            total += sum(float(c.get("usd") or 0.0) for c in man.get("costs") or [])
    return round(total, 4)


def trash_meeting(mdir: str, root: str) -> str:
    """Never a hard delete: move into <store>/.trash/<today>/<id>."""
    day = os.path.join(root, ".trash", datetime.date.today().isoformat())
    os.makedirs(day, exist_ok=True)
    dest = os.path.join(day, os.path.basename(os.path.abspath(mdir)))
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(day, f"{os.path.basename(mdir)}-{n}")
        n += 1
    shutil.move(mdir, dest)
    return dest


def set_pinned(mdir: str, pinned: bool) -> None:
    man = mr.read_manifest(mdir) or {}
    man["pinned"] = bool(pinned)
    mr.write_manifest(mdir, man)


def set_manifest_field(mdir: str, key: str, value) -> None:
    man = mr.read_manifest(mdir) or {}
    man[key] = value
    mr.write_manifest(mdir, man)


def new_meeting_dir(root: str, title: str) -> str:
    now = datetime.datetime.now()
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "meeting").lower()).strip("-")[:40] or "meeting"
    base = os.path.join(root, f"{now:%Y-%m-%d-%H%M}-{slug}")
    path, n = base, 1
    while os.path.exists(path):
        path, n = f"{base}-{n}", n + 1
    return path


def finalize_meeting(mdir: str, me_name: str = "Me") -> dict:
    """Post-call. Prefers the full pipeline (meeting_pipeline.finalize_meeting: transcribe, attribute, polish,
    note, save, Opus, index, cost; never raises). Fallback: stitch finished chunks, encode Opus, mark ready."""
    full = getattr(mp, "finalize_meeting", None)
    if full is not None:
        return full(mdir)
    if mp.read_chunks_jsonl(mdir):
        mp.stitch_and_polish(mdir, me_speaker=me_name)
    try:
        mp.finalize_opus(mdir)
    except Exception:
        pass  # no FLAC segments (silent or imported call): transcript and note still stand
    man = mr.read_manifest(mdir) or {}
    man["state"] = "ready"
    man["ended"] = man.get("ended") or datetime.datetime.now(datetime.timezone.utc).isoformat()
    mr.write_manifest(mdir, man)
    return man


def list_templates() -> list:
    out = []
    if os.path.isdir(TEMPLATES_DIR):
        for f in sorted(os.listdir(TEMPLATES_DIR)):
            if f.endswith(".md"):
                tid, name = f[:-3], f[:-3].replace("-", " ").title()
                head = open(os.path.join(TEMPLATES_DIR, f), encoding="utf-8").read(600)
                for line in head.splitlines():
                    if line.startswith("id:"):
                        tid = line[3:].strip()
                    elif line.startswith("name:"):
                        name = line[5:].strip()
                out.append((tid, name))
    out.sort(key=lambda t: t[0] != "default")
    return out or [("default", "Default meeting note")]


def note_path_for(mdir: str, dest: dict) -> str:
    man = mr.read_manifest(mdir) or {}
    date_part, slug = mst._meeting_date_and_slug(mdir, man.get("title") or "meeting")
    return os.path.join(dest.get("path") or "", f"{date_part}-{slug}.md")


def mark_accepted(mdir: str, task: str) -> None:
    path = os.path.join(mdir, "note.json")
    note = mr._read_json(path) or {}
    for a in note.get("action_items") or []:
        if a.get("task") == task:
            a["accepted"] = True
    mr._atomic_write_json(path, note, fsync=True)


# ---------------------------------------------------------------------------
# Demo store: neutral invented people, only when SCRIBE_UI_DEMO=1
# ---------------------------------------------------------------------------
_DEMO_ROOT = None
DEMO_DESTINATIONS = [{"id": "personal", "name": "Personal Vault"}, {"id": "work", "name": "Work Notes"}]


def demo_store() -> str:
    global _DEMO_ROOT
    if _DEMO_ROOT and os.path.isdir(_DEMO_ROOT):
        return _DEMO_ROOT
    root = tempfile.mkdtemp(prefix="scribe-demo-meetings-")
    now = datetime.datetime.now(datetime.timezone.utc)
    lines = [
        ("Alex", "Okay, where are we on stage four?"),
        ("Sam", "Playable, but it feels flat. Everyone just holds forward."),
        ("Riley", "Spinners are the problem. We have three already."),
        ("Sam", "Stage four needs a moving platform, not another spinner."),
        ("Riley", "I can reuse the conveyor script from the lobby."),
        ("Alex", "Do that, and lock the leaderboard screen tonight."),
        ("Riley", "On it. Pushing to the test place in ten."),
        ("Riley", "The leaderboard needs a weekly reset or nobody comes back."),
        ("Sam", "Weekly, Sunday night. I will write the reset job."),
        ("Alex", "Good. The timer resets on death, not on checkpoint."),
    ]
    demos = [
        ("game-night", "Game night: stage four platforms", 26, 6720, None, False, ["Alex", "Sam", "Riley"], True),
        ("weekly-planning", "Weekly planning with Sam", 3, 2820, "work", False, ["Alex", "Sam"], False),
        ("pricing-call", "Pricing call with Riley: plans for small teams and the annual discount", 5, 4080,
         "personal", True, ["Alex", "Riley"], False),
        ("voice-memo", "voice-memo-0412.m4a", 6, 372, "personal", False, ["Speaker A"], False),
    ]
    for slug, title, days, dur, dest, pinned, people, rich in demos:
        started = now - datetime.timedelta(days=days, hours=2)
        mdir = os.path.join(root, f"{started:%Y-%m-%d-%H%M}-{slug}")
        os.makedirs(mdir)
        man = {
            "id": os.path.basename(mdir), "title": title, "started": started.isoformat(),
            "ended": (started + datetime.timedelta(seconds=dur)).isoformat(), "state": "ready",
            "source": {"kind": "app" if rich else "import", "app": "discord" if rich else "import"},
            "discord": {}, "participants": [{"key": p, "name": p} for p in people],
            "destination_id": dest, "template_id": "default", "pinned": pinned, "audio_deleted_at": None,
            "costs": [{"provider": "groq:whisper-large-v3-turbo", "seconds": dur, "usd": round(dur / 3600 * 0.04, 4)}],
            "gaps": [],
        }
        mr.write_manifest(mdir, man)
        t0 = 2698.0 if rich else 4.0
        segs = []
        for i, (spk, text) in enumerate(lines if rich else [(people[i % len(people)], t) for i, (_, t) in enumerate(lines[:4])]):
            segs.append({"speaker_key": spk, "t0": t0 + i * 13, "t1": t0 + i * 13 + 11, "text": text,
                         "confidence": "?" if i == 6 else "high", "overlap": i == 6})
        mr._atomic_write_json(os.path.join(mdir, "transcript.json"), segs, fsync=False)
        note = {
            "title": title,
            "summary": ["Stage four gets a moving platform, not a fourth spinner.",
                        "Riley reuses the lobby conveyor script.",
                        "Leaderboard screen locks tonight; weekly reset on Sundays."],
            "decisions": [{"t": 2737.0, "text": "Stage four gets a moving platform instead of a spinner."},
                          {"t": 2815.0, "text": "The leaderboard resets weekly, Sunday night."}],
            "action_items": [{"task": "Lock the leaderboard screen", "owner": "Alex", "t": 2763.0},
                             {"task": "Push the conveyor to the test place", "owner": "Riley", "t": 2776.0},
                             {"task": "Write the weekly reset job", "owner": "Sam", "t": 2815.0}],
            "open_questions": ["Does the timer reset on checkpoint for speedrun mode?"],
        } if rich else {}
        if note:
            mr._atomic_write_json(os.path.join(mdir, "note.json"), note, fsync=False)
        try:
            mst.index_meeting(root, mdir, note_text=" ".join(note.get("summary", [])))
        except Exception:
            pass
    _DEMO_ROOT = root
    return root


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------
def _clear(layout) -> None:
    """Empty a layout. setParent(None) before deleteLater (playbook 5b: GC on dead wrappers)."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _repolish(w, **props) -> None:
    for k, v in props.items():
        w.setProperty(k, v)
    w.style().unpolish(w)
    w.style().polish(w)


def _label(text: str, obj: str, tip: str = "", wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(obj)
    if tip:
        lbl.setToolTip(tip)
    if wrap:
        lbl.setWordWrap(True)
    return lbl


def _badge(text: str, role: str, tip: str) -> QLabel:
    return _label(text.upper(), "Badge" + role, tip)


class ClampLabel(QLabel):
    """Word-wrapped label clamped to N lines with an ellipsis; full text in the tooltip."""

    def __init__(self, text: str, lines: int = 2, obj: str = "CardTitle", fit: bool = False):
        super().__init__()
        self._fit = fit
        self.setObjectName(obj)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._lines = lines
        self._full = ""
        self.ensurePolished()
        self.setFixedHeight(self.fontMetrics().lineSpacing() * lines + 2)
        self.set_full(text)

    def set_full(self, text: str):
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _fits(self, t: str, w: int) -> bool:
        fm = self.fontMetrics()
        return fm.boundingRect(QRect(0, 0, w, 10000), Qt.TextFlag.TextWordWrap, t).height() <= self.height()

    def _elide(self):
        need = self.fontMetrics().lineSpacing() * self._lines + 2  # the QSS font lands at polish, after __init__
        if self.height() != need and not self._fit:
            self.setFixedHeight(need)
        elif self._fit and self.height() < need:
            self.setFixedHeight(need)  # measure at full height, shrink once the text is known
        w = max(self.width(), 40)
        if self._fits(self._full, w):
            super().setText(self._full)
            if self._fit:
                fm = self.fontMetrics()
                h = fm.boundingRect(QRect(0, 0, w, 10000), Qt.TextFlag.TextWordWrap, self._full or "X").height()
                self.setFixedHeight(min(need, h + 2))
            return
        lo, hi = 0, len(self._full)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._fits(self._full[:mid].rstrip() + "…", w):
                lo = mid
            else:
                hi = mid - 1
        super().setText(self._full[:lo].rstrip() + "…")


def avatar(name: str, path: str | None = None, size: int = 20) -> QLabel:
    lbl = QLabel()
    lbl.setObjectName("Avatar")
    lbl.setFixedSize(size, size)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setToolTip(name)
    pix = QPixmap(path) if path else QPixmap()
    if not pix.isNull():
        lbl.setPixmap(pix.scaled(size - 2, size - 2, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation))
    else:
        parts = (name or "?").split()
        lbl.setText((parts[-1] if parts[0] == "Speaker" and len(parts) > 1 else parts[0])[:1].upper())
    return lbl


def _icon_label(glyph: str, text: str) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(8)
    ic = QLabel()
    ic.setPixmap(glyph_icon(glyph, 14, T["ink_dim"]).pixmap(14, 14))
    h.addWidget(ic)
    h.addWidget(_label(text, "FieldLabel"))
    h.addStretch(1)
    w.setFixedWidth(104)
    return w


def time_link(t: float, on_seek, tip: str = "") -> QPushButton:
    b = button(mst.format_timestamp(t), "TimeLink", None, tip or f"Play from {mst.format_timestamp(t)}")
    b.setMinimumHeight(26)
    b.clicked.connect(lambda _=False: on_seek(t))
    return b


class LevelBars(QWidget):
    """Rolling audio level: n thin bars, newest on the right. Paint reads T only."""

    def __init__(self, n: int = 6, height: int = 14, parent=None):
        super().__init__(parent)
        self._vals = [0.0] * n
        self.setFixedSize(n * 4, height)

    def push(self, v: float):
        self._vals = self._vals[1:] + [max(0.0, min(1.0, v))]
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        h = self.height()
        for i, v in enumerate(self._vals):
            bh = max(2, int(v * h))
            p.fillRect(i * 4, h - bh, 3, bh, QColor(T["cyan"] if v > 0.04 else T["obsidian_600"]))
        p.end()


def _level(rms) -> float:
    v = float(rms or 0.0)
    v = v / 32768.0 if v > 1.0 else v
    return min(1.0, v * 10.0)


# ---------------------------------------------------------------------------
# Gallery card
# ---------------------------------------------------------------------------
class MeetingCard(QFrame):
    """Notion-style card: title (2 lines), date and length, people, one badge, one destination chip."""
    clicked = Signal(str)

    def __init__(self, m: dict, dest_names: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.mdir = m["dir"]
        title = m.get("title") or "Untitled meeting"
        self.setAccessibleName(f"Meeting: {title}")
        self.setToolTip(f"{title}\nClick or press Enter to open")

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        self.lbl_title = ClampLabel(title, 2, "CardTitle")  # one face: mixed line heights broke the card baselines
        v.addWidget(self.lbl_title)
        hops = discord_hops(m)
        where = _chan(hops[-1].get("channel")) if hops else ""
        v.addWidget(_label(" · ".join(x for x in (fmt_card_date(m.get("started")), fmt_length(m["duration_s"]), where) if x),
                           "Meta", "Date, length and Discord channel\n" + fmt_place_tip(m) if where else "Date and length"))

        foot = QHBoxLayout()
        foot.setSpacing(4)
        people = m.get("speakers") or []
        for name in people[:3]:
            foot.addWidget(avatar(name, m["avatars"].get(name)))
        if len(people) > 3:
            foot.addWidget(_label(f"+{len(people) - 3}", "Meta", ", ".join(people[3:])))
        foot.addSpacing(4)
        b = self._badge(m)
        if b is not None:
            foot.addWidget(b)
        dest = m.get("destination_id")
        if dest:
            name = dest_names.get(dest, dest)
            chip = _label(name, "Chip", f"Saved to {name}")
            foot.addWidget(chip)
        foot.addStretch(1)
        v.addLayout(foot)

    @staticmethod
    def _badge(m):
        state = m.get("state")
        if state in ("recording", "paused"):
            return _badge("Rec" if state == "recording" else "Paused", "Crimson", "Recording now")
        if state == "needs_resume":
            return _badge("Needs resume", "Gold", "Recording stopped unexpectedly. Open to resume or finish it.")
        if state == "processing":
            return _badge("Processing", "Cyan", "Transcript and note are being finished")
        if state == "failed":
            return _badge("Failed", "Error", "Processing failed. Open for details.")
        if m.get("pinned"):
            return _badge("Pinned", "Cyan", "Pinned: audio is kept forever")
        if m.get("retention_badge"):
            return _badge(m["retention_badge"], "Gold", "Audio is deleted by retention soon. Pin to keep it.")
        if not m.get("destination_id"):
            return _badge("Not saved", "Gold", "Only in Scribe. Open it and pick Saved to.")
        return None

    def set_selected(self, on: bool):
        _repolish(self, selected="true" if on else "false")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.mdir)
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.mdir)
            return
        super().keyPressEvent(e)


# ---------------------------------------------------------------------------
# Audio player (QtMultimedia: seek and pitch-kept speed are native)
# ---------------------------------------------------------------------------
class SeekSlider(QSlider):
    """Click anywhere on the bar to jump there (stock QSlider only page-steps), then drag."""

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.width() > 0:
            self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                         int(e.position().x()), self.width()))
        super().mousePressEvent(e)


class MeetingPlayer(QFrame):
    position_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PlayerBar")
        self._players = []
        self._files = []
        self._speed = SPEEDS.index(1.0)
        self._dragging = False
        self._duration = 0.0
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 10, 6)
        h.setSpacing(8)
        self.btn_play = icon_button("play", "", "Play (both tracks)", "PlayBtn", T["cyan"])
        self.btn_play.clicked.connect(self.toggle)
        h.addWidget(self.btn_play)
        self.btn_back = icon_button("undo", "", "Back 15 seconds")
        self.btn_back.clicked.connect(lambda: self.seek(self.position() - 15, play=None))
        h.addWidget(self.btn_back)
        self.lbl_pos = _label("00:00", "NumCyan", "Current position")
        h.addWidget(self.lbl_pos)
        self.slider = SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setToolTip("Click or drag to jump to any point")
        self.slider.setAccessibleName("Audio position")
        self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.slider.sliderReleased.connect(self._released)
        self.slider.sliderMoved.connect(lambda v: self.lbl_pos.setText(mst.format_timestamp(v / 1000 * self._duration)))
        h.addWidget(self.slider, 1)
        self.lbl_total = _label("00:00", "Meta", "Length")
        h.addWidget(self.lbl_total)
        self.btn_speed = QComboBox()
        for s in SPEEDS:
            self.btn_speed.addItem(f"{s:g}x", s)
        self.btn_speed.setCurrentIndex(self._speed)
        self.btn_speed.setToolTip("Playback speed (pitch stays natural)")
        self.btn_speed.setAccessibleName("Playback speed")
        self.btn_speed.currentIndexChanged.connect(self._set_speed)
        h.addWidget(self.btn_speed)
        self._tick = QTimer(self)
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._on_tick)

    def load(self, m: dict):
        self.stop()
        for pl in self._players:
            pl.deleteLater()
        self._players = []
        self._files = list(m.get("audio") or [])
        self._duration = float(m.get("duration_s") or 0.0)
        self.lbl_total.setText(mst.format_timestamp(self._duration))
        self.lbl_pos.setText("00:00")
        self.slider.setValue(0)
        if self._files:
            reason = ""
        elif m.get("audio_deleted_at"):
            reason = f"Audio removed by retention on {fmt_card_date(m['audio_deleted_at'])}. Transcript and note stay."
        elif m.get("state") in ("recording", "paused", "processing") or m.get("audio_pending"):
            reason = "Audio is still being recorded or encoded"
        else:
            reason = "No audio was kept for this meeting"
        for w in (self.btn_play, self.btn_back, self.slider, self.btn_speed):
            w.setEnabled(not reason)
        self.setToolTip(reason)
        self.btn_play.setToolTip(reason or "Play (both tracks)")

    def _ensure(self) -> bool:
        if self._players or not self._files:
            return bool(self._players)
        from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
        for f in self._files:
            pl = QMediaPlayer(self)
            out = QAudioOutput(pl)
            pl.setAudioOutput(out)
            pl.setSource(QUrl.fromLocalFile(f))
            pl.setPlaybackRate(SPEEDS[self._speed])
            pl.errorOccurred.connect(self._on_error)
            self._players.append(pl)
        return True

    def _on_error(self, *_):
        self.stop()
        self.btn_play.setEnabled(False)
        self.btn_play.setToolTip("This audio file could not be played")

    def is_playing(self) -> bool:
        from PySide6.QtMultimedia import QMediaPlayer
        return bool(self._players) and self._players[0].playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def position(self) -> float:
        return self._players[0].position() / 1000.0 if self._players else self.slider.value() / 1000 * self._duration

    def toggle(self):
        if self.is_playing():
            self.pause()
        elif self._ensure():
            for pl in self._players:
                pl.play()
            self._tick.start()
            self.btn_play.setIcon(glyph_icon("pause", 14, T["cyan"]))
            self.btn_play.setToolTip("Pause")

    def pause(self):
        for pl in self._players:
            pl.pause()
        self._tick.stop()
        self.btn_play.setIcon(glyph_icon("play", 14, T["cyan"]))
        self.btn_play.setToolTip("Play (both tracks)")

    def stop(self):
        for pl in self._players:
            pl.stop()
        self._tick.stop()
        self.btn_play.setIcon(glyph_icon("play", 14, T["cyan"]))

    def seek(self, t: float, play=True):
        if not self.btn_play.isEnabled():
            return
        t = max(0.0, min(float(t), self._duration or float(t)))
        if self._ensure():
            for pl in self._players:
                pl.setPosition(int(t * 1000))
        self._show(t)
        if play and not self.is_playing():
            self.toggle()

    def _released(self):
        self._dragging = False
        self.seek(self.slider.value() / 1000 * self._duration, play=None)

    def _set_speed(self, i: int):
        self._speed = max(0, i)
        for pl in self._players:
            pl.setPlaybackRate(SPEEDS[self._speed])

    def _show(self, t: float):
        self.lbl_pos.setText(mst.format_timestamp(t))
        if self._duration and not self._dragging:
            self.slider.setValue(int(t / self._duration * 1000))
        self.position_changed.emit(t)

    def _on_tick(self):
        self._show(self.position())
        if self._players and self._duration and self.position() >= self._duration - 0.3:
            self.stop()


# ---------------------------------------------------------------------------
# Note and transcript builders (shared by the sheet and the overlay)
# ---------------------------------------------------------------------------
def fill_summary(lay, m: dict, view, compact: bool = False) -> None:
    _clear(lay)
    note = m.get("note") or {}
    seek = view.seek
    if not any(note.get(k) for k in ("summary", "decisions", "action_items", "open_questions")):
        lay.addWidget(_label("No note yet. The note is written after the call finishes processing; "
                             "the transcript is already searchable.", "Helper", wrap=True))
        lay.addStretch(1)
        return

    def bullets(items):
        for b in items:
            row = QHBoxLayout()
            row.setSpacing(8)
            dot = QLabel()
            dot.setObjectName("BulletDot")
            dot.setFixedSize(4, 4)
            col = QVBoxLayout()
            col.setContentsMargins(4, 7, 0, 0)
            col.addWidget(dot)
            col.addStretch(1)
            row.addLayout(col)
            row.addWidget(_label(str(b), "Body", wrap=True), 1)
            lay.addLayout(row)

    if note.get("summary"):
        lay.addWidget(section_label("Summary"))
        bullets(note["summary"])
    if note.get("decisions"):
        lay.addSpacing(8)
        lay.addWidget(section_label("Decisions"))
        for d in note["decisions"]:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(time_link(float(d.get("t", 0)), seek), 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(_label(d.get("text", ""), "Body", wrap=True), 1)
            lay.addLayout(row)
    if note.get("action_items"):
        lay.addSpacing(8)
        lay.addWidget(section_label("Action items"))
        for a in note["action_items"]:
            row = QHBoxLayout()
            row.setSpacing(8)
            owner = a.get("owner") or "Unassigned"
            row.addWidget(avatar(owner, m["avatars"].get(owner)), 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(_label(a.get("task", ""), "Body", f"Owner: {owner}", wrap=True), 1)
            if a.get("t") is not None:
                row.addWidget(time_link(float(a["t"]), seek), 0, Qt.AlignmentFlag.AlignTop)
            done = bool(a.get("accepted"))
            btn = button("Accepted" if done else "Accept", "GhostBtn", "check",
                         "Already added to the note checklist" if done else view.accept_tooltip(m))
            btn.setMinimumHeight(28)
            btn.setEnabled(not done)
            btn.clicked.connect(lambda _=False, task=a.get("task", ""), o=owner, b=btn: view.accept_item(m, task, o, b))
            row.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
            lay.addLayout(row)
    if note.get("open_questions") and not compact:
        lay.addSpacing(8)
        lay.addWidget(section_label("Open questions"))
        bullets(note["open_questions"])
    lay.addStretch(1)


def fill_transcript(lay, m: dict, view) -> list:
    """Rows: timestamp link, speaker, '?' and overlap labels, text. Returns [(t0, row)] for highlighting."""
    _clear(lay)
    segs = m.get("segments") or []
    if not segs:
        live = m.get("state") in ("recording", "paused")
        lay.addWidget(_label("Finished chunks appear here 10 to 20 seconds behind the call." if live else
                             "No speech was transcribed in this meeting.", "Helper", wrap=True))
        lay.addStretch(1)
        return []
    rows = []
    for s in segs:
        t0 = float(s.get("t0", 0.0))
        row = QFrame()
        row.setObjectName("TranscriptRow")
        g = QGridLayout(row)
        g.setContentsMargins(8, 6, 8, 6)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(2)
        g.addWidget(time_link(t0, view.seek), 0, 0, 2, 1, Qt.AlignmentFlag.AlignTop)
        top = QHBoxLayout()
        top.setSpacing(6)
        spk = s.get("speaker_key") or "Them"
        top.addWidget(_label(spk, "Speaker"))
        if s.get("confidence") in ("?", "low"):
            top.addWidget(_badge("?", "Gold", "Speaker guessed: nobody was lit in Discord at this moment"))
        if s.get("overlap"):
            top.addWidget(_badge("Overlap", "", "Two people spoke at once"))
        top.addStretch(1)
        g.addLayout(top, 0, 1)
        g.addWidget(_label(s.get("text", ""), "Body", wrap=True), 1, 1)
        g.setColumnStretch(1, 1)
        lay.addWidget(row)
        rows.append((t0, row))
    lay.addStretch(1)
    return rows


def highlight(rows: list, t: float, current):
    """Light the row playing at t; returns the lit row so callers repolish only on change."""
    lit = None
    for t0, row in rows:
        if t0 <= t:
            lit = row
        else:
            break
    if lit is not current:
        if current is not None:
            try:
                _repolish(current, active="false")
            except RuntimeError:
                pass
        if lit is not None:
            _repolish(lit, active="true")
    return lit


class AskBox(QFrame):
    """Suggestion chips, the answer with clickable citations, and the question field."""

    def __init__(self, view, placeholder: str, parent=None):
        super().__init__(parent)
        self.setObjectName("AskBox")
        self.view = view
        self.meeting = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 12)
        v.setSpacing(8)

        self.answer = QWidget()
        av = QVBoxLayout(self.answer)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(6)
        self.lbl_q = _label("", "AskQuestion", wrap=True)
        av.addWidget(self.lbl_q, 0, Qt.AlignmentFlag.AlignRight)
        arow = QHBoxLayout()
        arow.setSpacing(8)
        ic = QLabel()
        ic.setPixmap(glyph_icon("flash", 14, T["cyan"]).pixmap(14, 14))
        arow.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        self.lbl_a = _label("", "Body", wrap=True)
        self.lbl_a.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        arow.addWidget(self.lbl_a, 1)
        av.addLayout(arow)
        self.cites = QHBoxLayout()
        self.cites.setSpacing(6)
        av.addLayout(self.cites)
        self.answer.hide()
        v.addWidget(self.answer)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.btn_missed = button("What did I miss?", "ChipBtn", None,
                                 "Recap the last 5 minutes of the live transcript (free OpenRouter model)")
        self.btn_missed.clicked.connect(self._missed)
        chips.addWidget(self.btn_missed)
        self.btn_actions = button("Action items", "ChipBtn", None, f"Ask for the action items ({ASK_EST})")
        self.btn_actions.clicked.connect(lambda: self._ask("What are the action items?"))
        chips.addWidget(self.btn_actions)
        chips.addStretch(1)
        self.lbl_cost = _badge(ASK_EST, "", "Ask uses free OpenRouter models only, never a paid one")
        chips.addWidget(self.lbl_cost)
        v.addLayout(chips)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.cb_scope = QComboBox()
        self.cb_scope.addItems(["This meeting", "All meetings"])
        self.cb_scope.setToolTip("This meeting reads its whole transcript; All meetings searches the top 12 passages")
        row.addWidget(self.cb_scope)
        self.in_ask = QLineEdit()
        self.in_ask.setPlaceholderText(placeholder)
        self.in_ask.setToolTip(f"Ask a question, then press Enter ({ASK_EST} per question)")
        self.in_ask.returnPressed.connect(lambda: self._ask(self.in_ask.text()))
        row.addWidget(self.in_ask, 1)
        self.btn_send = icon_button("send", "", f"Ask ({ASK_EST})", "PlayBtn", T["cyan"])
        self.btn_send.clicked.connect(lambda: self._ask(self.in_ask.text()))
        row.addWidget(self.btn_send)
        v.addLayout(row)

    def load(self, m: dict):
        self.meeting = m
        self.answer.hide()
        self.in_ask.clear()
        self.lbl_cost.setText(ASK_EST)

    def _busy(self, q: str):
        self.lbl_q.setText(q)
        self.lbl_a.setText("Searching the transcript…")
        _clear(self.cites)
        self.answer.show()
        self.btn_send.setEnabled(False)

    def _ask(self, q: str):
        q = (q or "").strip()
        if not q or not self.meeting:
            return
        self._busy(q)
        scope = "this_meeting" if self.cb_scope.currentIndex() == 0 else "all_meetings"
        self.view.ask(self.meeting, q, scope, self._show)

    def _missed(self):
        if not self.meeting:
            return
        self._busy("What did I miss?")
        self.view.missed(self.meeting["dir"], self._show)

    def _show(self, res):
        try:
            self.btn_send.setEnabled(True)
        except RuntimeError:
            return
        if isinstance(res, Exception):
            self.lbl_a.setText(f"Could not answer: {res}. Try again, or search the transcript.")
            return
        self.lbl_a.setText((res.get("answer") or "No matching passages found.").replace("\u2014", ", "))
        usd = res.get("estimated_usd")
        self.lbl_cost.setText(f"${usd:.3f} est." if usd else ASK_EST)
        same = self.meeting.get("id")
        for c in (res.get("citations") or [])[:3]:
            if c.get("meeting_id") == same:
                self.cites.addWidget(time_link(float(c.get("t0", 0)), self.view.seek))
        self.cites.addStretch(1)


# ---------------------------------------------------------------------------
# Side sheet (v2 A base + v2 C player) and overlay (v2 A frame + v2 B body)
# ---------------------------------------------------------------------------
class _SheetGrip(QWidget):
    """The sheet's left edge: drag it left to widen the panel, right to narrow it."""

    def __init__(self, sheet):
        super().__init__(sheet)
        self.setObjectName("SheetGrip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("Drag to resize the panel")
        self.sheet = sheet
        self._from = None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._from = (e.globalPosition().x(), self.sheet.width())
            e.accept()

    def mouseMoveEvent(self, e):
        if self._from is not None:
            x0, w0 = self._from
            self.sheet.view.set_sheet_width(int(w0 + (x0 - e.globalPosition().x())))
            e.accept()

    def mouseReleaseEvent(self, e):
        if self._from is not None:
            self._from = None
            self.sheet.view.save_sheet_width()
            e.accept()


class SideSheetWidget(QFrame):
    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.setObjectName("SideSheet")
        self.view = view
        self.grip = _SheetGrip(self)
        self.m = {}
        self._loading = False
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 12, 20, 0)
        root.setSpacing(12)

        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        self.btn_close = icon_button("chevron_right", "", "Close panel (Esc)")
        self.btn_close.clicked.connect(view.close_sheet)
        hdr.addWidget(self.btn_close)
        self.btn_expand = icon_button("expand", "", "Open as a centered page")
        self.btn_expand.clicked.connect(view.open_overlay)
        hdr.addWidget(self.btn_expand)
        hdr.addStretch(1)
        self.btn_pin = icon_button("pin", "", "Pin: keep this meeting's audio forever")
        self.btn_pin.setCheckable(True)
        self.btn_pin.clicked.connect(lambda: view.toggle_pin(self.m))
        hdr.addWidget(self.btn_pin)
        self.btn_delete = icon_button("delete", "", "Move this meeting to the trash folder", "DangerBtn")
        self.btn_delete.clicked.connect(lambda: view.delete_meeting(self.m))
        hdr.addWidget(self.btn_delete)
        root.addLayout(hdr)

        self.lbl_title = ClampLabel("", 2, "SheetTitle", fit=True)
        root.addWidget(self.lbl_title)

        self.player_slot = QVBoxLayout()
        root.addLayout(self.player_slot)

        props = QGridLayout()
        props.setContentsMargins(0, 0, 0, 0)
        props.setHorizontalSpacing(12)
        props.setVerticalSpacing(4)
        self.in_date = QDateTimeEdit()
        self.in_date.setDisplayFormat("MMM d, yyyy  h:mm AP")
        self.in_date.setCalendarPopup(True)
        self.in_date.setToolTip("When the meeting started. Click the arrow for a calendar; click the time to type it.")
        self.in_date.editingFinished.connect(self._date_done)
        self.in_date.calendarWidget().clicked.connect(lambda _d: QTimer.singleShot(0, self._date_done))
        self.lbl_len = _label("", "Num", "Length (from the recording)")
        self.people = QHBoxLayout()
        self.people.setSpacing(4)
        people_w = QWidget()
        people_w.setLayout(self.people)
        self.people.setContentsMargins(0, 0, 0, 0)
        self.cb_dest = QComboBox()
        self.cb_dest.setToolTip("Pick a folder to write the note and transcript there, or Browse for any folder. "
                                "Picking is your OK to write.")
        self.cb_dest.activated.connect(self._dest_picked)
        self.btn_fav = icon_button("star", "", "Favorite this folder: it stays at the top of the list")
        self.btn_fav.clicked.connect(lambda: view.toggle_favorite(self.cb_dest.currentData()))
        dest_w = QWidget()
        dh = QHBoxLayout(dest_w)
        dh.setContentsMargins(0, 0, 0, 0)
        dh.setSpacing(4)
        dh.addWidget(self.cb_dest, 1)
        dh.addWidget(self.btn_fav)
        self.lbl_where = ElidingLabel("")
        self.lbl_where.setObjectName("Meta")
        self.cb_tmpl = QComboBox()
        self.cb_tmpl.setToolTip("Note layout. A change applies the next time the note is generated.")
        self.cb_tmpl.activated.connect(self._tmpl_picked)
        self.where_row = []
        for r, (g, name, w) in enumerate((("calendar", "Date", self.in_date), ("clock", "Length", self.lbl_len),
                                          ("volume", "Where", self.lbl_where),
                                          ("people", "People", people_w), ("folder", "Saved to", dest_w),
                                          ("library", "Template", self.cb_tmpl))):
            lab = _icon_label(g, name)
            props.addWidget(lab, r, 0)
            props.addWidget(w, r, 1)
            props.setRowMinimumHeight(r, 32)
            if w is self.lbl_where:
                self.where_row = [lab, w]
        props.setColumnStretch(1, 1)
        root.addLayout(props)

        tabs = QHBoxLayout()
        tabs.setSpacing(16)
        self.tab_group = QButtonGroup(self)
        for i, (txt, tip) in enumerate((("Summary", "Summary, decisions and action items"),
                                        ("Transcript", "Every line with speaker and timestamp"))):
            b = button(txt, "TabBtn", None, tip)
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.tab_group.addButton(b, i)
            tabs.addWidget(b)
        tabs.addStretch(1)
        self.tab_group.idClicked.connect(lambda i: self.stack.setCurrentIndex(i))
        root.addLayout(tabs)

        self.stack = QStackedWidget()
        s1, c1 = view.host.scroll_host(self)
        self.lay_summary = QVBoxLayout(c1)
        self.lay_summary.setContentsMargins(0, 4, 8, 8)
        self.lay_summary.setSpacing(8)
        s2, c2 = view.host.scroll_host(self)
        self.lay_transcript = QVBoxLayout(c2)
        self.lay_transcript.setContentsMargins(0, 4, 8, 8)
        self.lay_transcript.setSpacing(2)
        self.stack.addWidget(s1)
        self.stack.addWidget(s2)
        root.addWidget(self.stack, 1)

        self.ask = AskBox(view, "Ask about this meeting")
        root.addWidget(self.ask)
        self.rows, self._lit = [], None

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.grip.setGeometry(0, 0, 6, self.height())
        self.grip.raise_()

    def showEvent(self, e):  # every open/close path lands here, so the list reflows once
        super().showEvent(e)
        self.view._place_panels()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.view._place_panels()

    def load(self, m: dict):
        self._loading = True
        same = self.m.get("dir") == m.get("dir")
        self.m = m
        self.lbl_title.set_full((m.get("title") or "Untitled meeting").upper())
        self.btn_pin.setChecked(bool(m.get("pinned")))
        self.btn_pin.setIcon(glyph_icon("pin", 14, T["cyan"] if m.get("pinned") else T["ink_muted"]))
        self.btn_pin.setToolTip("Unpin: audio follows retention again" if m.get("pinned")
                                else "Pin: keep this meeting's audio forever")
        dt = _local(m.get("started"))
        if dt:
            self.in_date.setDateTime(QDateTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, 0))
        self.lbl_len.setText(fmt_length(m["duration_s"]))
        place = fmt_place(m)
        self.lbl_where.setText(place)
        self.lbl_where.setToolTip("Discord voice channel (updates live when you switch)\n" + fmt_place_tip(m) if place else "")
        for w in self.where_row:
            w.setVisible(bool(place))
        _clear(self.people)
        for name in m.get("speakers") or []:
            chip = button(name, "PersonChip", None, f"Rename {name} everywhere in this meeting")
            chip.setMinimumHeight(26)
            chip.clicked.connect(lambda _=False, n=name: self.view.rename_speaker(self.m, n))
            self.people.addWidget(chip)
        add = icon_button("add", "", "Add a person: pick someone from past meetings or type a new name")
        add.clicked.connect(lambda _=False, b=add: self.view.person_menu(self.m, b))
        self.people.addWidget(add)
        if not m.get("speakers"):
            self.people.addWidget(_label("Nobody named yet", "Helper"))
        self.people.addStretch(1)
        self.view.fill_dest_combo(self.cb_dest, m)
        self._sync_fav()
        self.view.fill_template_combo(self.cb_tmpl, m)
        fill_summary(self.lay_summary, m, self.view)
        self.rows, self._lit = fill_transcript(self.lay_transcript, m, self.view), None
        if not same:
            self.ask.load(m)
        self._loading = False

    def on_position(self, t: float):
        self._lit = highlight(self.rows, t, self._lit)

    def _date_done(self):
        if not self._loading and self.m:
            self.view.set_date(self.m, self.in_date.dateTime().toPython())

    def _dest_picked(self, i: int):
        self.view.save_to(self.m, self.cb_dest.itemData(i), self.cb_dest)
        self._sync_fav()

    def _sync_fav(self):
        d = self.view._dest(self.cb_dest.currentData())
        self.btn_fav.setEnabled(bool(d))
        on = bool(d and d.get("pinned"))
        self.btn_fav.setIcon(glyph_icon("star_fill" if on else "star", 14, T["gold"] if on else T["ink_muted"]))
        self.btn_fav.setToolTip("Unfavorite this folder" if on else "Favorite this folder: it stays at the top of the list")

    def _tmpl_picked(self, i: int):
        self.view.set_template(self.m, self.cb_tmpl.itemData(i))


class OverlayFrameWidget(QFrame):
    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.setObjectName("OverlayFrame")
        self.view = view
        self.m = {}
        f = QVBoxLayout(self)
        f.setContentsMargins(28, 16, 28, 20)
        f.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.btn_back = icon_button("chevron_left", "", "Back to the side panel")
        self.btn_back.clicked.connect(view.close_overlay)
        top.addWidget(self.btn_back)
        top.addStretch(1)
        self.btn_pin = icon_button("pin", "", "Pin: keep this meeting's audio forever")
        self.btn_pin.clicked.connect(lambda: view.toggle_pin(self.m))
        top.addWidget(self.btn_pin)
        self.btn_close = icon_button("close", "", "Close (Esc)")
        self.btn_close.clicked.connect(view.close_all)
        top.addWidget(self.btn_close)
        f.addLayout(top)

        self.lbl_title = ClampLabel("", 1, "SheetTitle")
        f.addWidget(self.lbl_title)
        meta = QHBoxLayout()
        meta.setSpacing(6)
        self.lbl_meta = _label("", "Meta", "Date and length")
        meta.addWidget(self.lbl_meta)
        meta.addSpacing(4)
        self.people = QHBoxLayout()
        self.people.setSpacing(4)
        meta.addLayout(self.people)
        meta.addStretch(1)
        self.cb_dest = QComboBox()
        self.cb_dest.setToolTip("Pick a folder to write the note and transcript there. Picking is your OK to write.")
        self.cb_dest.activated.connect(lambda i: view.save_to(self.m, self.cb_dest.itemData(i), self.cb_dest))
        meta.addWidget(self.cb_dest)
        f.addLayout(meta)

        self.player_slot = QVBoxLayout()
        f.addLayout(self.player_slot)

        body = QHBoxLayout()
        body.setSpacing(24)
        for attr, title in (("lay_left", None), ("lay_right", "Transcript")):  # the note brings its own // SUMMARY
            col = QVBoxLayout()
            col.setSpacing(8)
            if title:
                col.addWidget(section_label(title))
            sc, content = view.host.scroll_host(self)
            lay = QVBoxLayout(content)
            lay.setContentsMargins(0, 0, 8, 8)
            lay.setSpacing(8 if attr == "lay_left" else 2)
            setattr(self, attr, lay)
            col.addWidget(sc, 1)
            body.addLayout(col, 1)
        f.addLayout(body, 1)
        self.ask = AskBox(view, "Ask about this meeting, or across all meetings")
        f.addWidget(self.ask)
        self.rows, self._lit = [], None

    def load(self, m: dict):
        self.m = m
        self.lbl_title.set_full((m.get("title") or "Untitled meeting").upper())
        self.lbl_meta.setText(f"{fmt_long_date(m.get('started'))}  ·  {fmt_length(m['duration_s'])}")
        self.btn_pin.setIcon(glyph_icon("pin", 14, T["cyan"] if m.get("pinned") else T["ink_muted"]))
        _clear(self.people)
        for name in m.get("speakers") or []:
            self.people.addWidget(avatar(name, m["avatars"].get(name)))
        self.view.fill_dest_combo(self.cb_dest, m)
        fill_summary(self.lay_left, m, self.view, compact=False)
        self.rows, self._lit = fill_transcript(self.lay_right, m, self.view), None
        self.ask.load(m)

    def on_position(self, t: float):
        self._lit = highlight(self.rows, t, self._lit)


# ---------------------------------------------------------------------------
# Recording pill (v2 pill sheet): island, capsule (pinned), panel (hover)
# ---------------------------------------------------------------------------
class MeetingPillWidget(QFrame):
    pause_clicked = Signal()
    stop_clicked = Signal()
    missed_clicked = Signal()
    resume_clicked = Signal()
    decline_clicked = Signal()
    pinned_changed = Signal(bool)

    def __init__(self, parent=None, pinned: bool = False):
        super().__init__(parent)
        self.pinned = pinned
        self.state = "capsule" if pinned else "island"
        self.paused = False
        self.dead = False
        self.them_label = "Them"
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 6, 8, 6)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel()
        self.dot.setObjectName("RecDot")
        self.dot.setFixedSize(10, 10)
        self.dot.setToolTip("Recording")
        top.addWidget(self.dot)
        self.lbl_time = _label("0:00", "PillTime", "Recorded so far")
        top.addWidget(self.lbl_time)
        self.island_bars = QWidget()
        ib = QHBoxLayout(self.island_bars)
        ib.setContentsMargins(0, 0, 0, 0)
        ib.setSpacing(6)
        self.bars = {k: [LevelBars(6), LevelBars(10), LevelBars(14)] for k in ("me", "them")}
        ib.addWidget(self.bars["me"][0])
        ib.addWidget(self.bars["them"][0])
        self.island_bars.setToolTip("Your mic level and the call level")
        top.addWidget(self.island_bars)
        self.capsule_meters = self._meters(1)
        top.addWidget(self.capsule_meters)
        self.lbl_dead = _label("", "Body")
        top.addWidget(self.lbl_dead)
        top.addStretch(1)
        self.btn_pause = icon_button("pause", "", "Pause both tracks", "PillBtn")
        self.btn_pause.clicked.connect(self.pause_clicked.emit)
        self.btn_stop = icon_button("stop", "", "Stop and save the meeting", "PillBtn")
        self.btn_stop.clicked.connect(self.stop_clicked.emit)
        self.btn_missed_inline = button("What did I miss?", "AccentBtn", "undo",
                                        "Recap the last 5 minutes (free OpenRouter model)")
        self.btn_missed_inline.clicked.connect(self.missed_clicked.emit)
        self.btn_resume = button("Resume", "", "play", "Resume recording this call (the gap is noted)")
        self.btn_resume.clicked.connect(self.resume_clicked.emit)
        self.btn_decline = button("Finish", "GhostBtn", None, "Stop here and process what was saved")
        self.btn_decline.clicked.connect(self.decline_clicked.emit)
        self.btn_arrow = icon_button("chevron_right", "", "Pin open as a capsule", "PillBtn")
        self.btn_arrow.clicked.connect(self._toggle_pin)
        for b in (self.btn_pause, self.btn_stop, self.btn_missed_inline, self.btn_resume, self.btn_decline, self.btn_arrow):
            top.addWidget(b)
        root.addLayout(top)

        self.panel = QWidget()
        pv = QVBoxLayout(self.panel)
        pv.setContentsMargins(0, 0, 4, 4)
        pv.setSpacing(8)
        pv.addWidget(self._meters(2))
        self.btn_missed = button("What did I miss?", "AccentBtn", "undo", "Recap the last 5 minutes (free OpenRouter model)")
        self.btn_missed.clicked.connect(self.missed_clicked.emit)
        pv.addWidget(self.btn_missed)
        self.answer = QFrame()
        self.answer.setObjectName("AnswerCard")
        avl = QVBoxLayout(self.answer)
        avl.setContentsMargins(12, 8, 12, 8)
        avl.setSpacing(4)
        self.lbl_answer = _label("", "Body", wrap=True)
        avl.addWidget(self.lbl_answer)
        self.answer.hide()
        pv.addWidget(self.answer)
        self.chunks = QVBoxLayout()
        self.chunks.setSpacing(4)
        pv.addLayout(self.chunks)
        self.lbl_next = _label("Waiting for the first chunk", "Meta", "Transcript arrives in finished chunks")
        pv.addWidget(self.lbl_next)
        root.addWidget(self.panel)
        self._apply()

    def _meters(self, idx: int) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        for key, glyph in (("me", "mic"), ("them", "volume")):
            ic = QLabel()
            ic.setPixmap(glyph_icon(glyph, 12, T["ink_dim"]).pixmap(12, 12))
            h.addWidget(ic)
            lbl = _label("You" if key == "me" else self.them_label, "Meta")
            setattr(w, f"lbl_{key}", lbl)
            h.addWidget(lbl)
            h.addWidget(self.bars[key][idx])
            h.addSpacing(6)
        h.addStretch(1)
        w.setToolTip("Live levels: your mic and the call")
        self._meter_widgets = getattr(self, "_meter_widgets", []) + [w]
        return w

    def set_state(self, state: str):
        self.state = state
        self._apply()

    def _toggle_pin(self):
        self.pinned = not self.pinned
        self.pinned_changed.emit(self.pinned)
        self.set_state("capsule" if self.pinned else "island")

    def _apply(self):
        s, dead = self.state, self.dead
        self.setObjectName({"island": "PillIsland", "capsule": "PillCapsule"}.get(s, "PillPanel"))
        self.style().unpolish(self)
        self.style().polish(self)
        self.island_bars.setVisible(s == "island" and not dead)
        self.capsule_meters.setVisible(s == "capsule" and not dead)
        self.lbl_dead.setVisible(dead)
        for b in (self.btn_pause, self.btn_stop):
            b.setVisible(s != "island" and not dead)
        self.btn_missed_inline.setVisible(s == "capsule" and not dead)
        self.btn_resume.setVisible(dead)
        self.btn_decline.setVisible(dead)
        self.btn_arrow.setVisible(not dead)
        self.panel.setVisible(s == "panel" and not dead)
        self.btn_arrow.setIcon(glyph_icon("chevron_left" if self.pinned else "chevron_right", 14, T["ink_muted"]))
        self.btn_arrow.setToolTip("Collapse to the island" if self.pinned else "Pin open as a capsule")
        win = self.window()
        # the host is centred once on show; keep that centre x so hover growth is symmetric
        cx = win.x() + win.width() // 2 if win is not self and win.isVisible() else None
        self.setFixedWidth(520 if dead else {"island": 180, "capsule": 620}.get(s, 400))
        self.adjustSize()
        if win is not self:
            win.adjustSize()
            if cx is not None:
                win.move(cx - win.width() // 2, win.y())

    def set_status(self, elapsed_s: float, levels: dict, paused: bool, them_label: str = "Them"):
        self.dead = False
        self.paused = paused
        m, s = divmod(int(elapsed_s), 60)
        h, m = divmod(m, 60)
        self.lbl_time.setText(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")
        for key in ("me", "them"):
            lv = 0.0 if paused else _level((levels or {}).get(key))
            for b in self.bars[key]:
                b.push(lv)
        if them_label != self.them_label:
            self.them_label = them_label
            for w in self._meter_widgets:
                w.lbl_them.setText(them_label)
        _repolish(self.dot, state="paused" if paused else "rec")
        self.dot.setToolTip("Paused" if paused else "Recording")
        self.btn_pause.setIcon(glyph_icon("play" if paused else "pause", 14, T["ink_muted"]))
        self.btn_pause.setToolTip("Resume both tracks" if paused else "Pause both tracks")
        if self.lbl_dead.isVisible() or self.btn_resume.isVisible():
            self._apply()

    def set_chunks(self, chunks: list, next_in: float | None):
        _clear(self.chunks)
        for c in chunks[-3:]:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(_label(mst.format_timestamp(float(c.get("t0", 0))), "Meta"), 0, Qt.AlignmentFlag.AlignTop)
            spk = c.get("speaker_key") or ("You" if c.get("track") == "me" else self.them_label)
            row.addWidget(_label(f"{spk}: {(c.get('text') or '').strip()}", "Body", wrap=True), 1)
            self.chunks.addLayout(row)
        self.lbl_next.setText(f"Next chunk in ~{int(next_in)} s" if next_in is not None else "Waiting for the first chunk")

    def set_dead(self, saved_s: float):
        self.dead = True
        _repolish(self.dot, state="dead")
        self.dot.setToolTip("Recorder stopped")
        self.lbl_dead.setText(f"Recording stopped unexpectedly ({fmt_length(saved_s)} saved)")
        self._apply()

    def set_answer(self, text: str):
        self.lbl_answer.setText(text)
        self.answer.show()
        self.adjustSize()

    def enterEvent(self, e):
        if not self.dead:
            self.set_state("panel")
        super().enterEvent(e)

    def leaveEvent(self, e):
        if not self.dead:
            self.set_state("capsule" if self.pinned else "island")
        super().leaveEvent(e)


class MeetingEndCard(QFrame):
    """Island-style confirmation after End: 'saved, transcribing' turns into 'notes ready' with Open."""
    open_clicked = Signal()
    closed = Signal()
    DISMISS_MS = 8000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PillIsland")
        self.state = "saving"
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 6, 8, 6)
        h.setSpacing(8)
        self.dot = QLabel()
        self.dot.setObjectName("RecDot")
        self.dot.setFixedSize(10, 10)
        h.addWidget(self.dot)
        self.lbl = _label("", "Body")
        h.addWidget(self.lbl, 1)
        self.btn_open = button("Open", "AccentBtn", None, "Open the meeting notes")
        self.btn_open.clicked.connect(self.open_clicked.emit)
        h.addWidget(self.btn_open)
        self.btn_close = icon_button("close", "", "Dismiss", "PillBtn")
        self.btn_close.clicked.connect(self.closed.emit)
        h.addWidget(self.btn_close)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.closed.emit)
        self.setFixedWidth(360)

    def set_state(self, state: str, title: str = ""):
        """saving | ready | needs_key | failed. Only the finished states auto-dismiss."""
        self.state = state
        name = title or "Meeting"
        text, dot, tip = {
            "saving": (f"Saved, transcribing: {name}", "saving", "The note appears here when it is ready"),
            "ready": (f"Notes ready: {name}", "ready", name),
            "needs_key": ("Saved. Needs a Groq key", "paused", "Add a Groq key in Settings, Models, to transcribe"),
            "failed": ("Saved. Will retry later", "dead", "Transcription failed; it retries on the next start"),
        }.get(state, (f"Saved: {name}", "ready", name))
        room = self.width() - 64 - (0 if state == "saving" else 76)  # dot, close, margins; Open
        self.lbl.setText(self.lbl.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, room))
        self.lbl.setToolTip(tip)
        _repolish(self.dot, state=dot)
        self.btn_open.setVisible(state != "saving")
        self.timer.stop()
        if state != "saving":
            self.timer.start(self.DISMISS_MS)
        self.adjustSize()

    def enterEvent(self, e):
        self.timer.stop()  # reading it: hold
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self.state != "saving":
            self.timer.start(self.DISMISS_MS)
        super().leaveEvent(e)


class _FloatingHost(QWidget):
    """Frameless, non-activating, always-on-top window that carries a pill or toast."""

    def __init__(self, child: QWidget):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(DARK_STYLESHEET)  # a top-level window: the dashboard's sheet does not reach it
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(child)
        self.child = child


class CallPromptToast(QFrame):
    """'Discord call in <place>. Record?' with Record / Not now / Never here and the destination chip."""
    record_clicked = Signal(object)
    not_now_clicked = Signal()
    never_here_clicked = Signal()

    def __init__(self, place: str, destinations: list, destination=None, parent=None):
        super().__init__(parent)
        self.setObjectName("CallPromptToast")
        self.setFixedWidth(320)  # 20% narrower (was 400)
        v = QVBoxLayout(self)
        v.setContentsMargins(13, 10, 13, 13)
        v.setSpacing(8)
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        ic = QLabel()
        ic.setPixmap(create_app_icon_pixmap(16))
        hdr.addWidget(ic)
        hdr.addWidget(_label("LEONIDAS SCRIBE", "Meta"))
        hdr.addStretch(1)
        now = datetime.datetime.now()
        hdr.addWidget(_label(f"{now.hour % 12 or 12}:{now:%M} {'AM' if now.hour < 12 else 'PM'}", "Meta"))
        self.btn_close = icon_button("close", "", "Dismiss (same as Not now)")
        self.btn_close.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # non-activating window: never shows a stray ring
        self.btn_close.clicked.connect(self.not_now_clicked.emit)
        hdr.addWidget(self.btn_close)
        v.addLayout(hdr)
        self.lbl_msg = ElidingLabel(f"Discord call in {place}. Record?")
        self.lbl_msg.setObjectName("CardTitle")
        self.lbl_msg.setToolTip(f"Discord call in {place}. Record?")
        v.addWidget(self.lbl_msg)
        v.addWidget(_label("Records you and the call as two tracks, about $0.09 an hour.", "Helper", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(_label("SAVES TO", "Meta"))
        self.cb_dest = QComboBox()
        self.cb_dest.setToolTip("Where the note and transcript go after the call")
        self.cb_dest.addItem(glyph_icon("folder", 14, T["ink_muted"]), "Not saved (keep in Scribe)", None)
        for d in destinations:
            self.cb_dest.addItem(glyph_icon("folder", 14, T["ink_muted"]), d.get("name") or d.get("id"), d.get("id"))
        i = self.cb_dest.findData(destination)
        self.cb_dest.setCurrentIndex(max(0, i))
        row.addWidget(self.cb_dest)
        row.addStretch(1)
        v.addLayout(row)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_record = button("Record", "PrimaryBtn", "mic", "Start recording this call (about $0.09 an hour)")
        self.btn_record.clicked.connect(lambda: self.record_clicked.emit(self.cb_dest.currentData()))
        btns.addWidget(self.btn_record)
        self.btn_not_now = button("Not now", "", None, "Skip this call only")
        self.btn_not_now.clicked.connect(self.not_now_clicked.emit)
        btns.addWidget(self.btn_not_now)
        btns.addStretch(1)
        self.btn_never = button("Never here", "", None, "Never ask again for this channel or server")
        self.btn_never.clicked.connect(self.never_here_clicked.emit)
        btns.addWidget(self.btn_never)
        v.addLayout(btns)


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------
class MeetingsView(QWidget):
    FILTERS = (("all", "All", "Every meeting"), ("pinned", "Pinned", "Meetings whose audio is kept forever"),
               ("notsaved", "Not saved", "Meetings only in Scribe, not yet written to a folder"))
    CARD_MIN = 220
    CARD_MAX = 340          # cards stop growing here; spare width stays empty instead of stretching them
    SHEET_DEFAULT_W = 520   # was 380-460; the user wanted it a bit wider (2026-10-09)
    SHEET_MIN_W = 380
    SHEET_MAX_W = 760       # past this the player and fields only stretch, so the drag stops
    LIST_MIN_W = 520        # header (title, search, Import, Record) plus one card column beside the sheet

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.store_root = store_root()
        self.all_meetings = []
        self.loaded_generation = 0
        self._gen = 0
        self._filter = "all"
        self._search_q = ""
        self._search_ids = None
        self._cols = 0
        self._cards = []
        self.selected = None
        self.rec_dir = None
        self._rec_title = ""
        self._prompt = None
        self._pill_host = None
        self._end_host = None
        self._pending_open = None
        self._sheet_w = None  # set by dragging the sheet edge; else config meetings.sheet_width
        self._reserve = 0     # px of the list under the open sheet, pushed into the right margin
        self._recovery_toasted = set()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll, content = host.scroll_host(self)
        outer.addWidget(self.scroll)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(24, 20, 24, 24)
        lay.setSpacing(12)
        self._lay = lay

        self.in_search = QLineEdit()
        self.in_search.setPlaceholderText("Search")
        self.in_search.setToolTip("Searches titles, notes and every transcript line (Ctrl+F)")
        self.in_search.setClearButtonEnabled(True)
        self.in_search.setMinimumWidth(160)
        self.in_search.setMaximumWidth(280)
        self.in_search.addAction(glyph_icon("search", 14, T["ink_dim"]), QLineEdit.ActionPosition.LeadingPosition)
        self.in_search.textChanged.connect(self._on_search)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.in_search.setFocus,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.btn_import = button("Import", "", "upload",
                                 "Import an audio file and transcribe it (Groq, about $0.04 per hour of audio)")
        self.btn_import.clicked.connect(self.import_audio)
        self.btn_record = button("Record", "RecordBtn", "mic",
                                 "Record a meeting: your mic plus the call (about $0.09 per Discord hour)")
        self.btn_record.setIcon(glyph_icon("mic", 14, T["cyan"]))
        self.btn_record.clicked.connect(self._on_record)
        lay.addLayout(page_header("Meetings", "", actions=[self.in_search, self.btn_import, self.btn_record]))

        tabs = QHBoxLayout()
        tabs.setSpacing(16)
        self.filter_group = QButtonGroup(self)
        self.filter_btns = []
        for i, (key, txt, tip) in enumerate(self.FILTERS):
            b = button(txt, "TabBtn", None, tip)
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.filter_group.addButton(b, i)
            self.filter_btns.append(b)
            tabs.addWidget(b)
        tabs.addStretch(1)
        self.lbl_updated = _label("", "Meta", "When the meeting list was last read from disk")
        tabs.addWidget(self.lbl_updated)
        self.filter_group.idClicked.connect(self._on_filter)
        lay.addLayout(tabs)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lay.addLayout(self.grid)
        self.empty_slot = QVBoxLayout()
        lay.addLayout(self.empty_slot)
        lay.addStretch(1)

        self.player = MeetingPlayer()
        self.sheet = SideSheetWidget(self, self)
        self.sheet.player_slot.addWidget(self.player)
        self.sheet.hide()
        self.scrim = QFrame(self)
        self.scrim.setObjectName("OverlayScrim")
        self.overlay = OverlayFrameWidget(self, self.scrim)
        self.scrim.hide()
        self.player.position_changed.connect(self._on_position)
        self._restore = (False, False)
        for w in (self, self.sheet, self.scrim):
            QShortcut(QKeySequence(Qt.Key.Key_Escape), w, activated=self._on_escape,
                      context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._run_search)
        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self._poll_status)
        self._recovery = QTimer(self)
        self._recovery.setInterval(10000)
        self._recovery.timeout.connect(self.refresh)
        self._recovery.start()
        self.refresh()

    # ----- config ---------------------------------------------------------
    def _cfg(self) -> dict:
        return (getattr(self.host, "config", None) or {}).get("meetings") or {}

    def destinations(self) -> list:
        if os.environ.get("SCRIBE_UI_DEMO") == "1":
            base = os.path.join(self.store_root, ".dest")
            return [dict(d, path=os.path.join(base, d["id"])) for d in DEMO_DESTINATIONS]
        return [d for d in self._cfg().get("destinations") or [] if isinstance(d, dict) and d.get("path")]

    def _dest(self, dest_id):
        return next((d for d in self.destinations() if (d.get("id") or d.get("name")) == dest_id), None)

    def _dest_names(self) -> dict:
        return {(d.get("id") or d.get("name")): (d.get("name") or d.get("id")) for d in self.destinations()}

    def _retention(self) -> int:
        return int(self._cfg().get("retention_days") or 30)

    def _user_name(self) -> str:
        return ((getattr(self.host, "config", None) or {}).get("user") or {}).get("name") or os.environ.get("USERNAME") or "Me"

    # ----- async helper ---------------------------------------------------
    def _async(self, fn, ok, fail: str = ""):
        def done(res):
            try:
                if isinstance(res, Exception):
                    if fail:
                        self.host.toast(f"{fail}: {str(res)[:140]}", "error")
                    return
                ok(res)
            except RuntimeError:
                pass  # the view was torn down while the job ran
        run_async(fn, done)

    # ----- loading --------------------------------------------------------
    def refresh(self):
        self._gen += 1
        gen, root, days = self._gen, self.store_root, self._retention()

        def job():
            mst.sweep_retention(root, days)
            recovered = mr.check_recovery(root)
            return gen, load_store(root, days), recovered
        self._async(job, self._on_loaded, "Could not read meetings")

    def _on_loaded(self, res):
        gen, meetings, recovered = res
        if gen != self._gen:
            return  # an older, slower load lost the race
        self.all_meetings = meetings
        self.loaded_generation = gen
        now = datetime.datetime.now()
        self.lbl_updated.setText(f"Updated {now.hour % 12 or 12}:{now:%M} {'AM' if now.hour < 12 else 'PM'}")
        if not self.rec_dir:
            live = next((m for m in meetings if m.get("state") in ("recording", "paused")
                         and m.get("hb_age") is not None and m["hb_age"] < HEARTBEAT_DEAD_S), None)
            if live:
                self._adopt(live["dir"], live.get("title") or "")
        for r in recovered:
            if r["meeting_dir"] != self.rec_dir and r["meeting_dir"] not in self._recovery_toasted:
                self._recovery_toasted.add(r["meeting_dir"])
                self.host.toast(r["message"], "warn", "Resume", lambda d=r["meeting_dir"]: self.resume(d), ms=15000)
        self._render()
        if self._pending_open and self._find(self._pending_open):
            self.open_sheet(self._pending_open)
            self._pending_open = None
        if self.selected:
            m = self._find(self.selected)
            if m is None:
                self.close_all()
            else:
                self.sheet.load(m)
                if self.scrim.isVisible():
                    self.overlay.load(m)

    def show_recovery_prompt(self, message, resume_cb, decline_cb=None):
        """Resume toast for a crashed recording found by meeting_service (same toast the 10 s scan uses)."""
        self.host.toast(message, "warn", "Resume", resume_cb, ms=15000)  # ponytail: no Decline button, toast expiry leaves it needs_resume

    def _find(self, mdir):
        return next((m for m in self.all_meetings if m["dir"] == mdir), None)

    # ----- gallery --------------------------------------------------------
    def _visible(self) -> list:
        out = []
        q = self._search_q.lower()
        for m in self.all_meetings:
            if self._filter == "pinned" and not m.get("pinned"):
                continue
            if self._filter == "notsaved" and m.get("destination_id"):
                continue
            if q and q not in (m.get("title") or "").lower() and m.get("id") not in (self._search_ids or set()):
                continue
            out.append(m)
        return out

    def _render(self):
        counts = (len(self.all_meetings), sum(1 for m in self.all_meetings if m.get("pinned")),
                  sum(1 for m in self.all_meetings if not m.get("destination_id")))
        for b, (_, txt, _tip), n in zip(self.filter_btns, self.FILTERS, counts):
            b.setText(f"{txt}  {n}")
        _clear(self.grid)
        _clear(self.empty_slot)
        self._cards = []
        items = self._visible()
        if not items:
            if not self.all_meetings:
                acts = QWidget()
                h = QHBoxLayout(acts)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(8)
                rec = button("Record", "RecordBtn", "mic", "Record a meeting now (about $0.09 per Discord hour)")
                rec.setIcon(glyph_icon("mic", 14, T["cyan"]))
                rec.clicked.connect(self._on_record)
                imp = button("Import audio", "", "upload", "Import an audio file (about $0.04 per hour of audio)")
                imp.clicked.connect(self.import_audio)
                h.addWidget(rec)
                h.addWidget(imp)
                empty = EmptyState("people", "No meetings yet",
                                   "Join a call or press Record. You get a speaker-labeled transcript and a note.", acts)
            else:
                empty = EmptyState("search", "Nothing matches",
                                   "Clear the search or pick All to see every meeting.")
            self.empty_slot.addWidget(empty)
            return
        cols = self._columns()
        dest_names = self._dest_names()
        for i, m in enumerate(items):
            card = MeetingCard(m, dest_names)
            card.clicked.connect(self.open_sheet)
            card.set_selected(m["dir"] == self.selected)
            self.grid.addWidget(card, i // cols, i % cols)
            self._cards.append(card)
        self._cols = cols
        self._size_cards()

    def _list_width(self) -> int:
        """Width the card grid can use: viewport minus page margins minus whatever the sheet covers."""
        return max(self.CARD_MIN, self.scroll.viewport().width() - 48 - self._reserve)

    def _columns(self) -> int:
        return max(1, min(4, (self._list_width() + 12) // (self.CARD_MIN + 12)))

    def _size_cards(self):
        cols = max(1, self._cols)
        cw = min(self.CARD_MAX, (self._list_width() - 12 * (cols - 1)) // cols)
        for c in self._cards:
            c.setFixedWidth(max(200, cw))

    def _reflow(self):
        if self._cards and self._columns() != self._cols:
            self._render()
        else:
            self._size_cards()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_panels()
        self._reflow()

    def _layer(self) -> QWidget:
        """The window's central widget (falls back to the view): the sheet sits at the window edge
        like Notion's side peek, and the overlay scrim dims the rail too."""
        cw = self.host.centralWidget() if hasattr(self.host, "centralWidget") else None
        layer = cw or self
        if self.sheet.parentWidget() is not layer:
            self.sheet.setParent(layer)
            self.scrim.setParent(layer)
            self.sheet.hide()
            self.scrim.hide()
            if layer is not self:
                layer.installEventFilter(self)
        return layer

    def eventFilter(self, obj, e):
        if e.type() == e.Type.Resize and obj is self.sheet.parentWidget():
            self._place_panels()
        return False

    def _place_panels(self):
        if not hasattr(self, "overlay"):
            return  # sheet events fire while __init__ is still building the view
        layer = self.sheet.parentWidget() or self
        w, h = layer.width(), layer.height()
        vp = self.scroll.viewport()
        if layer is self:
            left, right = 0, vp.width()
        else:
            left = self.mapTo(layer, QPoint(0, 0)).x()
            right = vp.mapTo(layer, QPoint(vp.width(), 0)).x()
        want = self._sheet_w or int(self._cfg().get("sheet_width") or self.SHEET_DEFAULT_W)
        sw = max(self.SHEET_MIN_W, min(want, self.SHEET_MAX_W, w - left - self.LIST_MIN_W))
        self.sheet.setGeometry(w - sw, 0, sw, h)
        self.scrim.setGeometry(0, 0, w, h)
        fw = min(1040, w - 64)
        self.overlay.setGeometry((w - fw) // 2, 24, fw, h - 48)
        # Docked, not floating: the list gives up the width the sheet covers, so the header and
        # cards reflow left instead of sliding under it.
        reserve = max(0, right - (w - sw)) if not self.sheet.isHidden() else 0
        if reserve != self._reserve:
            self._reserve = reserve
            self._lay.setContentsMargins(24, 20, 24 + reserve, 24)
            self._reflow()

    def showEvent(self, e):
        super().showEvent(e)
        sheet, scrim = self._restore
        self._restore = (False, False)
        if sheet and self.selected:
            self._place_panels()
            self.sheet.show()
            self.sheet.raise_()
        if scrim and self.selected:
            self.scrim.show()
            self.scrim.raise_()

    def _on_filter(self, i: int):
        self._filter = self.FILTERS[i][0]
        self._render()

    def _on_search(self, txt: str):
        self._search_q = txt.strip()
        if not self._search_q:
            self._search_ids = None
            self._render()
            return
        self._render()  # title matches show at once; transcript hits follow
        self._search_timer.start()

    def _run_search(self):
        q, root = self._search_q, self.store_root
        if not q:
            return

        def done(hits):
            if q == self._search_q:
                self._search_ids = {h["meeting_id"] for h in hits}
                self._render()
        self._async(lambda: mst.search_meetings(root, q, limit=50), done)

    # ----- sheet and overlay ------------------------------------------------
    def open_sheet(self, mdir: str):
        m = self._find(mdir)
        if not m:
            return
        self.selected = mdir
        for c in self._cards:
            c.set_selected(c.mdir == mdir)
        self._layer()
        self.sheet.player_slot.addWidget(self.player)
        self.player.load(m)
        self.sheet.load(m)
        self._place_panels()
        if not self.sheet.isVisible():
            self.sheet.show()
            self.sheet.raise_()
            motion.fade_in(self.sheet, 240)

    def close_sheet(self):
        self.player.pause()
        self.sheet.hide()
        self.selected = None
        for c in self._cards:
            c.set_selected(False)

    def open_overlay(self):
        m = self._find(self.selected) if self.selected else None
        if not m:
            return
        self._layer()
        self.overlay.player_slot.addWidget(self.player)
        self.overlay.load(m)
        self._place_panels()
        self.scrim.show()
        self.scrim.raise_()
        motion.fade_in(self.overlay, 240)

    def close_overlay(self):
        self.scrim.hide()
        self.sheet.player_slot.addWidget(self.player)

    def close_all(self):
        self.close_overlay()
        self.close_sheet()

    def _on_escape(self):
        if self.scrim.isVisible():
            self.close_overlay()
        elif self.sheet.isVisible():
            self.close_sheet()

    def seek(self, t: float):
        self.player.seek(t)

    def _on_position(self, t: float):
        self.sheet.on_position(t)
        if self.scrim.isVisible():
            self.overlay.on_position(t)

    def hideEvent(self, e):
        self.player.pause()
        if self.sheet.parentWidget() is not self:  # the panels sit on the window layer: leave with the page
            self._restore = (self.sheet.isVisible(), self.scrim.isVisible())
            self.sheet.hide()
            self.scrim.hide()
        super().hideEvent(e)

    # ----- property edits ---------------------------------------------------
    def fill_dest_combo(self, cb: QComboBox, m: dict):
        cb.blockSignals(True)
        cb.clear()
        cb.addItem(glyph_icon("folder", 14, T["ink_muted"]), "Not saved", None)
        # favorites first, then every folder ever picked; labelled with the real path so it reads as where it lives
        for d in sorted(self.destinations(), key=lambda d: not d.get("pinned")):
            fav = bool(d.get("pinned"))
            cb.addItem(glyph_icon("star_fill" if fav else "folder", 14, T["gold"] if fav else T["ink_muted"]),
                       d.get("path"), d.get("id") or d.get("name"))
            cb.setItemData(cb.count() - 1, f"{d.get('name') or d.get('id')}: {d.get('path')}", Qt.ItemDataRole.ToolTipRole)
        cb.addItem(glyph_icon("add", 14, T["cyan"]), "Browse for a folder…", BROWSE)
        i = cb.findData(m.get("destination_id"))
        cb.setCurrentIndex(i if i >= 0 else 0)
        cb.blockSignals(False)

    def _update_meetings_cfg(self, mutate):
        """Apply mutate(meetings_cfg) to the live config and to config.json (fresh read, so other edits survive)."""
        cfg = getattr(self.host, "config", None)
        if cfg is not None:
            mutate(cfg.setdefault("meetings", {}))
        ctl = getattr(self.host, "controller", None)
        if ctl is not None:
            def job():
                fresh = ctl.load_config()
                mutate(fresh.setdefault("meetings", {}))
                return ctl.save_config(fresh)
            self._async(job, lambda _: None, "Could not save the folder list")

    def browse_destination(self, m: dict, cb: QComboBox):
        cur = self._dest(m.get("destination_id"))
        path = QFileDialog.getExistingDirectory(self, "Save this meeting to a folder", (cur or {}).get("path") or "")
        if not path:
            self.fill_dest_combo(cb, m)
            return
        path = os.path.normpath(path)
        known = next((d for d in self.destinations()
                      if os.path.normcase(os.path.normpath(d["path"])) == os.path.normcase(path)), None)
        if known is None:
            known = {"id": path, "name": os.path.basename(path) or path, "path": path, "pinned": False}
            self._update_meetings_cfg(lambda mc: mc.setdefault("destinations", []).append(dict(known)))
        self.fill_dest_combo(cb, m)
        self.save_to(m, known.get("id") or known.get("name"), cb)

    def toggle_favorite(self, dest_id):
        d = self._dest(dest_id)
        if not d:
            return
        on = not d.get("pinned")

        def mutate(mc):
            for x in mc.get("destinations") or []:
                if (x.get("id") or x.get("name")) == dest_id:
                    x["pinned"] = on
        self._update_meetings_cfg(mutate)
        self.host.toast(f"{'Favorited' if on else 'Unfavorited'} {d.get('path')}", "success")
        self.refresh()

    # ----- people -----------------------------------------------------------
    def known_people(self) -> list:
        """Saved people plus everyone named in any past meeting, most frequent first."""
        counts = {}
        for n in self._cfg().get("people") or []:
            counts[n] = counts.get(n, 0) + 1000
        for mm in self.all_meetings:
            for n in mm.get("speakers") or []:
                counts[n] = counts.get(n, 0) + 1
        return [n for n in sorted(counts, key=lambda n: -counts[n])
                if n not in ("Them", "You", "Me", "Speaker") and not re.fullmatch(r"Speaker \w{1,2}", n)]

    def person_menu(self, m: dict, anchor: QWidget):
        menu = QMenu(self)
        have = set(m.get("speakers") or [])
        for n in [p for p in self.known_people() if p not in have][:20]:
            menu.addAction(n, lambda n=n: self.add_person(m, n))
        if not menu.isEmpty():
            menu.addSeparator()
        menu.addAction(glyph_icon("add", 14, T["cyan"]), "New person…", lambda: self._new_person(m))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _new_person(self, m: dict):
        name, ok = QInputDialog.getText(self, "Add a person", "Name (saved to your people list):")
        name = (name or "").strip().replace("\u2014", "-")
        if ok and name:
            if name not in (self._cfg().get("people") or []):
                self._update_meetings_cfg(lambda mc: mc.setdefault("people", []).append(name))
            self.add_person(m, name)

    def add_person(self, m: dict, name: str):
        mdir = m["dir"]

        def job():
            man = mr.read_manifest(mdir) or {}
            ps = man.get("participants") or []
            if not any((p.get("name") or p.get("key")) == name for p in ps):
                ps.append({"key": name, "name": name, "discord_id": None, "avatar_path": None, "manual": True})
            man["participants"] = ps
            mr.write_manifest(mdir, man)
        self._async(job, lambda _: (self.host.toast(f"Added {name}", "success"), self.refresh()), "Could not add")

    def fill_template_combo(self, cb: QComboBox, m: dict):
        cb.blockSignals(True)
        cb.clear()
        for tid, name in list_templates():
            cb.addItem(name, tid)
        i = cb.findData(m.get("template_id") or "default")
        cb.setCurrentIndex(max(0, i))
        cb.blockSignals(False)

    def toggle_pin(self, m: dict):
        if not m:
            return
        on = not m.get("pinned")
        self._async(lambda: set_pinned(m["dir"], on), lambda _: (
            self.host.toast("Pinned: this meeting's audio is kept forever" if on else
                            "Unpinned: audio follows the retention setting again", "success"),
            self.refresh()), "Could not pin")

    def delete_meeting(self, m: dict):
        if not m:
            return
        if m["dir"] == self.rec_dir:
            self.host.toast("Stop the recording before deleting this meeting", "warn")
            return
        title = m.get("title") or "this meeting"
        box = QMessageBox(QMessageBox.Icon.Warning, "Delete meeting",
                          f"Move “{title}” to the trash folder?\n\nAudio, transcript and note move to "
                          f"meetings/.trash. Files already saved to a destination folder stay there.",
                          QMessageBox.StandardButton.Cancel, self)
        ok = box.addButton("Move to trash", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not ok:
            return
        root = self.store_root
        self.close_all()
        self._async(lambda: trash_meeting(m["dir"], root), lambda dest: (
            self.host.toast(f"Moved to trash: {os.path.relpath(dest, root)}", "success"), self.refresh()),
            "Could not delete")

    def set_date(self, m: dict, local_dt: datetime.datetime):
        old = _local(m.get("started"))
        if old and abs((old.replace(tzinfo=None) - local_dt).total_seconds()) < 60:
            return
        iso = local_dt.astimezone().astimezone(datetime.timezone.utc).isoformat()
        self._async(lambda: set_manifest_field(m["dir"], "started", iso),
                    lambda _: (self.host.toast("Meeting date updated", "success"), self.refresh()),
                    "Could not change the date")

    def rename_speaker(self, m: dict, old: str):
        new, ok = QInputDialog.getText(self, "Rename speaker",
                                       f"New name for “{old}” (renames every line and the note):", text=old)
        new = (new or "").strip().replace("\u2014", "-")
        if not ok or not new or new == old:
            return
        mdir, root = m["dir"], self.store_root

        def job():
            msp.rename_speaker(mdir, old, new, store_root=root)
            path = os.path.join(mdir, "note.json")
            note = mr._read_json(path)
            if note:
                for a in note.get("action_items") or []:
                    if a.get("owner") == old:
                        a["owner"] = new
                mr._atomic_write_json(path, note, fsync=True)
        self._async(job, lambda _: (self.host.toast(f"Renamed {old} to {new} everywhere in this meeting", "success"),
                                    self.refresh()), "Could not rename")

    def save_to(self, m: dict, dest_id, cb: QComboBox):
        if dest_id == BROWSE:
            self.browse_destination(m, cb)
            return
        if dest_id is None:
            if m.get("destination_id"):
                self.fill_dest_combo(cb, m)
                self.host.toast("Already saved. The files stay in that folder; pick another folder to copy them.", "info")
            return
        dest = self._dest(dest_id)
        if not dest:
            return
        note = m.get("note") or None
        self.host.toast(f"Saving to {dest.get('path')}…", "info")
        self._async(lambda: mst.save_meeting_to_destination(m["dir"], dest, note), lambda r: (
            self.host.toast(f"Saved to {dest.get('path')}: {os.path.basename(r['note_path'])}", "success"),
            self.refresh()), "Could not save")

    def set_template(self, m: dict, tid):
        if not tid or tid == m.get("template_id"):
            return
        mdir, has_note = m["dir"], bool(m.get("note")) and m.get("state") == "ready"

        def job():
            set_manifest_field(mdir, "template_id", tid)
            if not has_note:
                return None
            # rewrite the note with the new template (free Groq); the saved copy is overwritten in place
            os.replace(os.path.join(mdir, "note.json"), os.path.join(mdir, "note.prev.json"))
            res = mp.finalize_meeting(mdir)
            man = mr.read_manifest(mdir) or {}
            dest = self._dest(man.get("destination_id"))
            if dest and res.get("state") == "ready":
                mst.save_meeting_to_destination(mdir, dest, mr._read_json(os.path.join(mdir, "note.json")))
            return res
        if has_note:
            self.host.toast("Rewriting the note with the new template…", "info")
        self._async(job, lambda r: (
            self.host.toast("Note rewritten with the new template" if r else
                            "Template changed. It applies when the note is written.", "success"),
            self.refresh()), "Could not change the template")

    def accept_tooltip(self, m: dict) -> str:
        dest = self._dest(m.get("destination_id"))
        if not dest:
            return "Save the meeting to a folder first: Accept adds the task to that note"
        cmd = "and runs its task command" if (dest.get("on_accept_cmd") or "").strip() else "(checklist only)"
        return f"Add to the {dest.get('name')} note checklist {cmd}"

    def accept_item(self, m: dict, task: str, owner: str, btn: QPushButton):
        dest = self._dest(m.get("destination_id"))
        if not dest:
            self.host.toast("Save the meeting to a folder first (Saved to), then Accept adds the task there", "warn")
            return
        btn.setEnabled(False)
        btn.setText("Adding…")
        mdir = m["dir"]

        def job():
            res = mst.accept_action_item(mdir, dest, task, owner=owner, note_path=note_path_for(mdir, dest))
            if res.get("returncode", 0) == 0:
                mark_accepted(mdir, task)
            return res

        def done(res):
            if res.get("returncode", 0) == 0:
                self.host.toast(f"Accepted: {task}", "success")
            else:
                self.host.toast(f"Task command failed: {(res.get('stderr') or '').strip()[:120]}", "error")
            self.refresh()
        self._async(job, done, "Could not accept")

    # ----- ask --------------------------------------------------------------
    # Free OpenRouter models only (mp.free_chain drops any paid id). No key: the closest line, labelled as such.
    def ask(self, m: dict, q: str, scope: str, cb):
        root, mdir = self.store_root, m.get("dir")

        def job():
            fn = mdt.free_ask_fn()
            res = mdt.ask_meetings(root, q, scope=scope, meeting_dir=mdir, llm_fn=fn)
            if fn is None:
                res["answer"] = "No OpenRouter key (Settings, Models), so here is the closest line: " + res["answer"]
            return res
        run_async(job, cb)

    def missed(self, mdir: str, cb, minutes: int = 5):
        run_async(lambda: mdt.what_did_i_miss(mdir, window_minutes=minutes, llm_fn=mdt.free_missed_fn()), cb)

    # ----- import -----------------------------------------------------------
    def import_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import audio", "",
                                              "Audio files (*.wav *.mp3 *.m4a *.flac *.ogg)")
        if path:
            self._async(lambda: _probe_seconds(path), lambda secs: self._confirm_import(path, secs),
                        "Could not read that file")

    def _confirm_import(self, path: str, secs):
        name = os.path.basename(path)
        cost = f"about ${secs / 3600 * 0.04:.2f}" if secs else "about $0.04 per hour"
        length = f" ({fmt_length(secs)})" if secs else ""
        box = QMessageBox(QMessageBox.Icon.Question, "Import audio",
                          f"Transcribe {name}{length} with Groq whisper-large-v3-turbo?\n\nCost: {cost}. "
                          f"Speakers are labeled on this PC for free.", QMessageBox.StandardButton.Cancel, self)
        ok = box.addButton("Import and transcribe", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is not ok:
            return
        title = os.path.basename(path)
        mdir = new_meeting_dir(self.store_root, os.path.splitext(title)[0])
        self.host.toast(f"Importing {title}: transcribing in the background…", "info", ms=8000)
        self.btn_import.setEnabled(False)

        def done(res):
            self.btn_import.setEnabled(True)
            self.host.toast(f"Imported {title}", "success")
            self.refresh()

        def job():
            try:
                return mdt.import_audio_file(path, mdir, title=title)
            except Exception:
                man = mr.read_manifest(mdir)
                if man:
                    man["state"] = "failed"
                    mr.write_manifest(mdir, man)
                raise

        def guarded(res):
            if isinstance(res, Exception):
                self.btn_import.setEnabled(True)
                self.host.toast(f"Import failed: {str(res)[:140]}", "error")
                self.refresh()
            else:
                done(res)
        run_async(job, lambda r: self._safe(guarded, r))

    def _safe(self, fn, arg):
        try:
            fn(arg)
        except RuntimeError:
            pass

    # ----- recording --------------------------------------------------------
    def is_recording(self) -> bool:
        return self.rec_dir is not None

    def _on_record(self):
        if self.rec_dir:
            self.stop_recording()
            return
        box = QMessageBox(QMessageBox.Icon.Question, "Record a meeting",
                          "Record your mic and the call as two tracks? During a Discord call Scribe records "
                          "Discord itself, whatever device it plays on; otherwise the default speakers.\n\nAfter you stop, Groq "
                          "whisper-large-v3-turbo transcribes it and gpt-oss-120b writes the note: about $0.09 per "
                          "Discord hour, $0.26 for other apps.", QMessageBox.StandardButton.Cancel, self)
        ok = box.addButton("Start recording", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is not ok:
            return
        audio = (getattr(self.host, "config", None) or {}).get("audio") or {}
        self.start_recording({"kind": "device", "app": None, "pid": None, "device": "default",
                              "mic_device": audio.get("device_index")})

    def start_recording(self, source: dict):
        """Public API for the app shell. source: manifest source {kind, app, pid, device, mic} plus optional
        title, destination_id, template_id, discord {...}, participants [...]."""
        src = dict(source or {})
        if src.get("meeting_dir"):  # the meeting service already made the folder and started the worker
            if self.rec_dir != src["meeting_dir"]:
                self._adopt(src["meeting_dir"], src.get("title") or "")
                self.refresh()
            return
        if self.rec_dir:
            self.host.toast("Already recording. Stop the current meeting first.", "warn")
            return
        now = datetime.datetime.now()
        title = src.pop("title", None) or f"Meeting {now:%b} {now.day}, {now.hour % 12 or 12}:{now:%M} " \
                                                f"{'AM' if now.hour < 12 else 'PM'}"
        cfg = self._cfg()
        dest = src.pop("destination_id", None) or cfg.get("default_destination_id")
        tmpl = src.pop("template_id", None) or cfg.get("default_template_id") or "default"
        discord = src.pop("discord", None)
        people = src.pop("participants", None)
        mdir = new_meeting_dir(self.store_root, title)
        self.btn_record.setEnabled(False)

        def job():
            import meeting_service as msvc  # lazy: it lists audio sessions through ScribeCapture
            source = msvc.prefer_call_audio(src)
            svc = getattr(msvc.MeetingService, "current", None)
            ch = svc.current_channel() if svc else None
            mr.init_meeting_dir(mdir, title=title, source=source, discord=discord or msvc.discord_fields(ch),
                                participants=people or [{"key": p.get("name") or p.get("discord_id"), "name": p.get("name"),
                                                         "discord_id": p.get("discord_id"), "avatar_path": None}
                                                        for p in (ch or {}).get("participants") or []] or None,
                                destination_id=dest, template_id=tmpl)
            _starting(mdir)  # a fresh heartbeat, so the recovery scan never flags a worker still booting
            pid = mr.start_worker(mdir)
            if svc:
                svc.attach(mdir)  # channel switches mid-call land in this meeting's manifest
            return pid

        def started(_pid):
            self.btn_record.setEnabled(True)
            self._adopt(mdir, title)
            self.host.toast(f"Recording: {title}", "success")
            self.refresh()

        def failed(res):
            self.btn_record.setEnabled(True)
            if isinstance(res, Exception):
                self.host.toast(f"Could not start the recorder: {str(res)[:140]}", "error")
            else:
                started(res)
        run_async(job, lambda r: self._safe(failed, r))

    def _adopt(self, mdir: str, title: str):
        self.rec_dir, self._rec_title = mdir, title
        self.btn_record.setText("  Stop")
        self.btn_record.setIcon(glyph_icon("stop", 14, T["crimson_text"]))
        self.btn_record.setToolTip("Stop recording and save the meeting")
        self.pill().set_state("capsule" if self.pill().pinned else "island")
        self._show_pill()
        self._poll.start()
        self._poll_status()

    def _release(self):
        self.rec_dir = None
        self._poll.stop()
        self.btn_record.setText("  Record")
        self.btn_record.setIcon(glyph_icon("mic", 14, T["cyan"]))
        self.btn_record.setToolTip("Record a meeting: your mic plus the call (about $0.09 per Discord hour)")
        if self._pill_host is not None:
            self._pill_host.hide()

    def stop_recording(self):
        mdir, title, me = self.rec_dir, self._rec_title, self._user_name()
        if not mdir:
            return
        self._release()
        self._show_end_card(mdir, title)

        def job():
            mr.stop_worker(mdir, timeout=30)  # was 5 s: finalizing early orphaned the last segment
            return finalize_meeting(mdir, me)
        self._async(job, lambda res: self._finished(mdir, title, res), "Could not finish the meeting")

    def _finished(self, mdir: str, title: str, res):
        self._finished_toast(title, res)
        card = self._end_host.child if self._end_host is not None else None
        if card is not None and card.property("mdir") == mdir:
            card.set_state((res or {}).get("state") if (res or {}).get("state") in ("needs_key", "failed") else "ready", title)
            self._place_end_card()
        self.refresh()

    # ----- end-of-meeting card ----------------------------------------------
    def _show_end_card(self, mdir: str, title: str):
        """Shown synchronously on End, at the pill's spot, so the click never looks like a vanish."""
        if self._end_host is None:
            card = MeetingEndCard()
            card.open_clicked.connect(lambda: self._open_from_card(card.property("mdir")))
            card.closed.connect(self._dismiss_end_card)
            self._end_host = _FloatingHost(card)
        card = self._end_host.child
        card.setProperty("mdir", mdir)
        card.set_state("saving", title)
        self._place_end_card()
        self._end_host.show()

    def _place_end_card(self):
        host = self._end_host
        host.adjustSize()
        scr = (self.window().screen() or QApplication.primaryScreen()).availableGeometry()
        host.move(scr.center().x() - host.width() // 2, scr.top() + 12)

    def _dismiss_end_card(self):
        if self._end_host is not None:
            self._end_host.child.timer.stop()
            self._end_host.hide()

    def _open_from_card(self, mdir):
        self._dismiss_end_card()
        if hasattr(self.host, "show_dashboard"):
            self.host.show_dashboard()
        if hasattr(self.host, "select_view"):
            self.host.select_view(self)
        if mdir and self._find(mdir):
            self.open_sheet(mdir)
        elif mdir:
            self._pending_open = mdir
            self.refresh()

    def _finished_toast(self, title: str, res):
        state = (res or {}).get("state")
        if state == "needs_key":
            self.host.toast(f"{title} is saved, but needs a Groq key to transcribe. Add one in Settings, Models.", "warn")
        elif state == "failed":
            self.host.toast(f"{title} is saved; transcription failed. It retries on the next start.", "error")
        else:
            self.host.toast(f"Saved {title}", "success")

    def toggle_pause(self):
        if not self.rec_dir:
            return
        mdir, cmd = self.rec_dir, "resume" if self.pill().paused else "pause"
        self._async(lambda: mr.send_worker_command(mdir, cmd), lambda _: self._poll_status(), "Could not pause")

    def resume(self, mdir: str):
        def started(_):
            m = self._find(mdir)
            self._adopt(mdir, (m or {}).get("title") or "")
            self.host.toast("Recording resumed. The gap is noted in the transcript.", "success")
            self.refresh()
        def job():
            pid = mr.resume_meeting(mdir)
            _starting(mdir)
            return pid
        self._async(job, started, "Could not resume")

    def decline(self, mdir: str):
        me, title = self._user_name(), self._rec_title or "The recording"
        self._release()
        self._show_end_card(mdir, title)
        self._async(lambda: mr.decline_resume(mdir, pipeline_fn=lambda d: finalize_meeting(d, me)),
                    lambda res: self._finished(mdir, title, res), "Could not finish the meeting")

    def pill(self) -> MeetingPillWidget:
        if self._pill_host is None:
            p = MeetingPillWidget(pinned=bool(self._cfg().get("pill_pinned")))
            p.pause_clicked.connect(self.toggle_pause)
            p.stop_clicked.connect(self.stop_recording)
            p.missed_clicked.connect(self._pill_missed)
            p.resume_clicked.connect(lambda: self.rec_dir and self.resume(self.rec_dir))
            p.decline_clicked.connect(lambda: self.rec_dir and self.decline(self.rec_dir))
            p.pinned_changed.connect(self._remember_pin)
            self._pill_host = _FloatingHost(p)
        return self._pill_host.child

    def _show_pill(self):
        host = self._pill_host
        host.adjustSize()
        scr = (self.window().screen() or QApplication.primaryScreen()).availableGeometry()
        host.move(scr.center().x() - host.width() // 2, scr.top() + 12)
        host.show()

    def _remember_pin(self, on: bool):
        self._remember("pill_pinned", on)

    def _remember(self, key: str, value):
        """One meetings.* value into the live config and config.json, off the GUI thread."""
        ctl = getattr(self.host, "controller", None)
        cfg = getattr(self.host, "config", None)
        if cfg is not None:
            cfg.setdefault("meetings", {})[key] = value
        if ctl is None:
            return

        def job():
            fresh = ctl.load_config()
            fresh.setdefault("meetings", {})[key] = value
            return ctl.save_config(fresh)
        self._async(job, lambda _: None)

    # ----- side sheet width ----------------------------------------------------
    def set_sheet_width(self, w: int):
        self._sheet_w = max(self.SHEET_MIN_W, min(self.SHEET_MAX_W, int(w)))
        self._place_panels()

    def save_sheet_width(self):
        self._remember("sheet_width", self.sheet.width())

    def _pill_missed(self):
        if self.rec_dir:
            self.pill().set_answer("Reading the last 5 minutes…")
            self.missed(self.rec_dir, lambda r: self._safe(
                lambda res: self.pill().set_answer(str(res) if isinstance(res, Exception) else res.get("answer", "")), r))

    def _poll_status(self):
        mdir = self.rec_dir
        if not mdir:
            return

        def job():
            st = mr.read_status(mdir) or {}
            man = mr.read_manifest(mdir) or {}
            chunks = mp.read_chunks_jsonl(mdir)[-3:]
            return st, man, chunks
        self._async(job, lambda r: self._on_status(mdir, *r))

    def _on_status(self, mdir, st, man, chunks):
        if mdir != self.rec_dir:
            return
        pill = self.pill()
        age = time.time() - float(st.get("heartbeat_at", 0) or 0)
        if age > HEARTBEAT_DEAD_S and st.get("state") not in ("stopped",):
            pill.set_dead(float(st.get("me_seconds") or 0.0))
            self._show_pill()
            return
        app = ((man.get("source") or {}).get("app") or "").title() or "Them"
        pill.set_status(float(st.get("me_seconds") or 0.0), st.get("levels") or {}, st.get("state") == "paused", app)
        last = st.get("last_chunk_time")
        pill.set_chunks(chunks, max(0.0, 10.0 - (time.time() - float(last))) if last else None)

    # ----- call prompt (public API) -----------------------------------------
    def show_call_prompt(self, place: str, destination=None, on_record=None, on_never=None, timeout_ms: int = 20000):
        """Non-activating toast at the host window's bottom-right; auto-dismisses after timeout_ms."""
        self.dismiss_call_prompt()
        toast = CallPromptToast(place, self.destinations(), destination)
        host = _FloatingHost(toast)

        def record(dest_id):
            self.dismiss_call_prompt()
            if on_record:
                on_record(dest_id)
            else:
                self.start_recording({"kind": "app", "app": "discord", "title": f"Call in {place}",
                                      "destination_id": dest_id})

        def never():
            self.dismiss_call_prompt()
            if on_never:
                on_never()
        toast.record_clicked.connect(record)
        toast.not_now_clicked.connect(self.dismiss_call_prompt)
        toast.never_here_clicked.connect(never)
        host.adjustSize()
        float_stack.place(host)  # stacks above the voice card instead of covering it
        host.show()
        self._prompt = host
        QTimer.singleShot(timeout_ms, toast, self.dismiss_call_prompt)
        return toast

    def dismiss_call_prompt(self):
        if self._prompt is not None:
            self._prompt.hide()
            self._prompt.deleteLater()
            self._prompt = None


def _starting(mdir: str) -> None:
    mr.write_status(mdir, {"state": "starting", "me_seconds": mr.saved_audio_seconds(mdir, "me"),
                           "levels": {}, "heartbeat_at": time.time()})


def _probe_seconds(path: str):
    try:
        import soundfile as sf
        return float(sf.info(path).duration)
    except Exception:
        return None
