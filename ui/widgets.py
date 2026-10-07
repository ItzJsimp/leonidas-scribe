"""
Widgets for Leonidas Scribe's Command-register UI.

Styling lives in ui/styles.py; widgets here set semantic objectNames only.
Colour inside paint code always comes from the token table T.
"""
import difflib
import html
from datetime import datetime

from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QApplication,
    QSizePolicy, QWidget, QCheckBox, QGraphicsOpacityEffect, QAbstractButton,
)
from PySide6.QtCore import (
    Qt, QTimer, Signal, QSize, QRectF, QPropertyAnimation, QEasingCurve, Property, QEvent,
    QObject, QRunnable, QThreadPool, QPoint,
)
from PySide6.QtGui import QFontMetrics, QPainter, QColor, QLinearGradient

from . import labels, motion
from .dialogs import WordCorrectionDialog
from .icons import glyph_icon
from .styles import T


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def tab_focus_only(root: QWidget) -> None:
    """Buttons take focus from the keyboard only. Clicking a button used to
    give it focus, so the nav item you clicked kept a focus ring after you
    left its page; that is the root cause of the stuck ring, fixed here once."""
    for b in [root] + root.findChildren(QAbstractButton):
        if isinstance(b, QAbstractButton):
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)


class ElidingLabel(QLabel):
    """Single-line label that truncates with an ellipsis instead of widening the row."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(24)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        super().setText(self._full)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        if not self._full:
            return
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideRight,
                                           max(self.width() - 2, 16)))


def button(text: str, kind: str = "", glyph: str = None, tooltip: str = "") -> QPushButton:
    """kind: "" secondary, PrimaryBtn, GhostBtn, DangerBtn, LinkBtn, DestructiveBtn."""
    b = QPushButton(("  " + text) if glyph and text else text)
    if kind:
        b.setObjectName(kind)
    if glyph:
        ink = T["white"] if kind in ("PrimaryBtn", "DestructiveBtn") else T["ink_muted"]
        b.setIcon(glyph_icon(glyph, 14, ink))
        b.setIconSize(QSize(14, 14))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    b.setMinimumHeight(32)
    if tooltip:
        b.setToolTip(tooltip)
    b.setAccessibleName(tooltip or text or glyph or "")
    return b


def icon_button(name: str, text: str = "", tooltip: str = "",
                object_name: str = "GhostBtn", color: str = None) -> QPushButton:
    """Ghost button with a Fluent glyph. Square (28px) when text is empty."""
    btn = QPushButton(text)
    btn.setObjectName(object_name)
    btn.setIcon(glyph_icon(name, 14, color or T["ink_muted"]))
    btn.setIconSize(QSize(14, 14))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setAccessibleName(tooltip or text or name)
    if not text:
        btn.setFixedSize(28, 28)
    else:
        btn.setMinimumHeight(28)
    return btn


def section_label(text: str) -> QLabel:
    """`// SECTION` in JetBrains Mono with a cyan prefix (design-system.md)."""
    lbl = QLabel(f'<span style="color:{T["cyan"]}">//</span>&nbsp;{html.escape(text.upper())}')
    lbl.setObjectName("SectionLabel")
    lbl.setTextFormat(Qt.TextFormat.RichText)
    return lbl


def badge(text: str, role: str = "") -> QLabel:
    """role: "" neutral, Cyan, Gold, Success, Error, Crimson."""
    lbl = QLabel(text.upper())
    lbl.setObjectName("Badge" + role)
    return lbl


def kbd(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Kbd")
    return lbl


def page_header(title: str, subtitle: str = "", actions=()) -> QHBoxLayout:
    """Big Shoulders page title (uppercase, 20px), helper line, actions right."""
    row = QHBoxLayout()
    row.setSpacing(8)
    col = QVBoxLayout()
    col.setSpacing(2)
    lbl = QLabel(title.upper())
    lbl.setObjectName("PageTitle")
    col.addWidget(lbl)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("Helper")
        sub.setWordWrap(True)
        col.addWidget(sub)
    row.addLayout(col, 1)
    for a in actions:
        row.addWidget(a, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def make_card(title: str = "", hint: str = "", action: QPushButton = None):
    """Card (obsidian-850, 12px radius, 16px padding, 12px gaps). Returns (frame, body)."""
    card = QFrame()
    card.setObjectName("Card")
    outer = QVBoxLayout(card)
    outer.setContentsMargins(16, 16, 16, 16)
    outer.setSpacing(12)
    if title:
        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        lbl = QLabel(title)
        lbl.setObjectName("CardTitle")
        titles.addWidget(lbl)
        if hint:
            sub = QLabel(hint)
            sub.setObjectName("CardHint")
            sub.setWordWrap(True)
            titles.addWidget(sub)
        head.addLayout(titles, 1)
        if action is not None:
            head.addWidget(action, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)
    body = QVBoxLayout()
    body.setSpacing(12)
    outer.addLayout(body)
    return card, body


def kpi_tile(label: str, value: str = "-", note: str = "", gold: bool = False):
    """Big mono number, `// LABEL`, helper note. Returns (frame, value_label, note_label)."""
    card = QFrame()
    card.setObjectName("Card")
    v = QVBoxLayout(card)
    v.setContentsMargins(16, 14, 16, 16)
    v.setSpacing(4)
    lbl_value = QLabel(value)
    lbl_value.setObjectName("KpiGold" if gold else "Kpi")
    v.addWidget(lbl_value)
    v.addWidget(section_label(label))
    lbl_note = QLabel(note)
    lbl_note.setObjectName("KpiNote")
    lbl_note.setWordWrap(True)
    v.addWidget(lbl_note)
    v.addStretch(1)
    return card, lbl_value, lbl_note


def field(label_text: str, widget: QWidget) -> QVBoxLayout:
    """Labelled form field: visible label above the control (never placeholder-only)."""
    col = QVBoxLayout()
    col.setSpacing(6)
    lbl = QLabel(label_text)
    lbl.setObjectName("FieldLabel")
    col.addWidget(lbl)
    col.addWidget(widget)
    return col


# ---------------------------------------------------------------------------
# Async load (keeps the UI thread free while history.jsonl is read)
# ---------------------------------------------------------------------------
class _Done(QObject):
    done = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn, signal):
        super().__init__()
        self.fn, self.signal = fn, signal

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # handed to the callback, never swallowed
            result = e
        self.signal.done.emit(result)


_live_jobs = set()


def run_async(fn, callback) -> None:
    """Run fn on the thread pool; callback(result) runs on the UI thread."""
    sig = _Done()
    _live_jobs.add(sig)

    def finish(result):
        _live_jobs.discard(sig)
        callback(result)

    sig.done.connect(finish, Qt.ConnectionType.QueuedConnection)
    QThreadPool.globalInstance().start(_Job(fn, sig))


# ---------------------------------------------------------------------------
# Loading and empty states
# ---------------------------------------------------------------------------
class Skeleton(QWidget):
    """Placeholder blocks shaped like the content, with a slow shimmer.
    Never a spinner on a blank page (app-building-standard.md section 5)."""

    def __init__(self, rows=4, row_h=96, parent=None):
        super().__init__(parent)
        self.rows, self.row_h = rows, row_h
        self._x = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.setMinimumHeight(rows * (row_h + 12))

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start()

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def _tick(self):
        if motion.enabled():
            self._x = (self._x + 0.018) % 1.6
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        base = QColor(T["obsidian_850"])
        hi = QColor(T["obsidian_800"])
        for i in range(self.rows):
            y = i * (self.row_h + 12)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(base)
            p.drawRoundedRect(QRectF(0, y, w, self.row_h), 12, 12)
            grad = QLinearGradient(w * (self._x - 0.4), 0, w * self._x, 0)
            grad.setColorAt(0.0, QColor(T["obsidian_800"]))
            grad.setColorAt(0.5, QColor(T["obsidian_700"]))
            grad.setColorAt(1.0, hi)
            p.setBrush(grad)
            for bx, by, bw in ((16, 16, 0.28), (16, 42, 0.86), (16, 62, 0.62)):
                p.drawRoundedRect(QRectF(bx, y + by, (w - 32) * bw, 10), 5, 5)


class EmptyState(QWidget):
    """Designed empty state: glyph in a soft circle, title, one line, one action."""

    def __init__(self, glyph: str, title: str, body: str = "", action: QPushButton = None, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 48, 24, 48)
        v.setSpacing(10)
        v.addStretch(1)
        icon = QLabel()
        icon.setObjectName("EmptyIcon")
        icon.setFixedSize(56, 56)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(glyph_icon(glyph, 22, T["cyan"]).pixmap(22, 22))
        v.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self.lbl_title = QLabel(title)
        self.lbl_title.setObjectName("EmptyTitle")
        self.lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.lbl_title)
        self.lbl_body = QLabel(body)
        self.lbl_body.setObjectName("Helper")
        self.lbl_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_body.setWordWrap(True)
        v.addWidget(self.lbl_body)
        self.action = action
        if action is not None:
            v.addSpacing(6)
            v.addWidget(action, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addStretch(2)


# ---------------------------------------------------------------------------
# Toast with Undo
# ---------------------------------------------------------------------------
class Toast(QFrame):
    """Bottom-centre toast: glyph, message, optional action (Undo), auto-dismiss.

    on_expire runs when the toast times out OR is replaced by the next toast,
    so a deferred delete commits exactly once unless Undo was pressed.
    """
    ROLE_GLYPH = {"success": ("check", "success"), "info": ("info", "cyan"),
                  "warn": ("warning", "gold"), "error": ("warning", "error")}

    def __init__(self, host: QWidget, message: str, role: str = "success",
                 action_text: str = "", on_action=None, on_expire=None, ms: int = 5000):
        super().__init__(host)
        self.setObjectName("Toast")
        self.on_action, self.on_expire = on_action, on_expire
        self._settled = False
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 8, 8, 8)
        h.setSpacing(10)
        glyph, token = self.ROLE_GLYPH.get(role, self.ROLE_GLYPH["info"])
        ic = QLabel()
        ic.setPixmap(glyph_icon(glyph, 16, T[token]).pixmap(16, 16))
        h.addWidget(ic)
        self.lbl_message = QLabel(message)
        self.lbl_message.setObjectName("Body")
        self.lbl_message.setWordWrap(True)
        self.lbl_message.setMaximumWidth(460)
        h.addWidget(self.lbl_message, 1)
        if action_text:
            act = button(action_text, "LinkBtn")
            act.setMinimumHeight(28)
            act.clicked.connect(self._act)
            h.addWidget(act)
        close = icon_button("close", "", "Dismiss")
        close.clicked.connect(self.settle)
        h.addWidget(close)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.settle)
        self._timer.start(ms)

    def place(self):
        host = self.parentWidget()
        self.adjustSize()
        x = (host.width() - self.width()) // 2
        y = host.height() - self.height() - 24
        self.show()
        self.raise_()
        if motion.enabled():
            self.move(x, y + 12)
            anim = QPropertyAnimation(self, b"pos", self)
            anim.setDuration(240)
            anim.setEndValue(QPoint(x, y))
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        else:
            self.move(x, y)

    def _act(self):
        if self._settled:
            return
        self._settled = True
        if self.on_action:
            self.on_action()
        self._close()

    def settle(self):
        """Time out (or be replaced): commit the pending action once."""
        if self._settled:
            return
        self._settled = True
        if self.on_expire:
            self.on_expire()
        self._close()

    def _close(self):
        self._timer.stop()
        self.hide()
        self.deleteLater()


# ---------------------------------------------------------------------------
# Transcript card: quiet by default, click anywhere to copy
# ---------------------------------------------------------------------------
def _build_word_diff_html(raw: str, cleaned: str) -> str:
    """Word diff: removals struck through in error rose, insertions in success green."""
    raw_words, cleaned_words = raw.split(), cleaned.split()
    matcher = difflib.SequenceMatcher(None, raw_words, cleaned_words, autojunk=False)
    parts = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            parts.append(html.escape(" ".join(raw_words[i1:i2])))
            continue
        if i1 != i2:
            parts.append(f'<span style="color:{T["error"]}; text-decoration:line-through;">'
                         f'{html.escape(" ".join(raw_words[i1:i2]))}</span>')
        if j1 != j2:
            parts.append(f'<span style="color:{T["success"]};">'
                         f'{html.escape(" ".join(cleaned_words[j1:j2]))}</span>')
    return " ".join(parts)


def format_clock(ts_raw: str) -> str:
    """Time of day only, e.g. "2:24 PM" (cards grouped under a day header)."""
    try:
        return datetime.fromisoformat(ts_raw).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return ts_raw


def audio_path_for(item: dict) -> str:
    """Absolute path of the take's kept audio, or "" if none survives."""
    import os
    m = item.get("metrics") or {}
    rel = item.get("audio") or m.get("saved_wav") or (m.get("capture_mismatch") or {}).get("saved_wav") or ""
    if not rel:
        return ""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = rel if os.path.isabs(rel) else os.path.join(base, rel)
    return path if os.path.exists(path) else ""


class AudioPlayer(QObject):
    """One take plays at a time (sounddevice output, FLAC or WAV via soundfile).
    `changed(path)` fires on start and with "" on stop or end."""
    changed = Signal(str)
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.current = ""
        self._end = QTimer(self)
        self._end.setSingleShot(True)
        self._end.timeout.connect(self.stop)

    def toggle(self, path: str):
        if self.current == path:
            self.stop()
            return
        import sounddevice as sd
        import soundfile as sf
        data, sr = sf.read(path, dtype="int16")
        sd.stop()
        sd.play(data, sr)
        self.current = path
        self._end.start(int(len(data) / sr * 1000) + 150)
        self.changed.emit(path)

    def stop(self):
        import sounddevice as sd
        sd.stop()
        self._end.stop()
        self.current = ""
        self.changed.emit("")


def format_time(ts_raw: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_raw)
        return dt.strftime("%b %d  %I:%M %p").replace(" 0", " ")
    except Exception:
        return ts_raw


class TranscriptCard(QFrame):
    """One transcript. The whole card is the copy target (click, or Enter when
    focused); Raw, Diff, Teach and Delete fade in on hover or keyboard focus so
    1,500 cards do not each shout five buttons."""
    CLAMP_CHARS = 420
    copied = Signal(str)

    def __init__(self, item: dict, on_delete_callback=None, on_learn_callback=None, time_only: bool = False):
        super().__init__()
        self.item = item
        self.on_delete = on_delete_callback
        self.on_learn = on_learn_callback
        self.setObjectName("Card")
        self.setProperty("copied", False)
        self.setProperty("lift", True)  # grows slightly on hover (motion.Hover.LIFT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip("Click to copy")

        self._raw_text = (item.get("raw_text") or "").strip()
        self._cleaned_text = (item.get("cleaned_text") or "").strip()
        self._view_mode = "polished"  # "polished" | "raw" | "diff"
        self._full_text = self._cleaned_text
        self._expanded = False

        layout = QVBoxLayout(self)
        lift = motion.Hover.LIFT  # room for the hover grow; content stays put
        layout.setContentsMargins(16 + lift, 10 + lift, 10 + lift, 14 + lift)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        m = item.get("metrics", {}) or {}
        meta = [(format_clock if time_only else format_time)(item.get("timestamp", ""))]
        app_name = labels.app(item.get("app", ""))
        if app_name:
            meta.append(app_name)
        words = m.get("words") or len(self._cleaned_text.split())
        if words:
            meta.append(f"{words:,} words")
        if m.get("total_ms"):
            meta.append(f"{m['total_ms']:,} ms")
        lbl_meta = QLabel("  ·  ".join(meta))
        lbl_meta.setObjectName("Meta")
        lbl_meta.setToolTip(self._metrics_tooltip(m, words, item.get("lens", "")))
        header.addWidget(lbl_meta)

        mode = item.get("mode", "ptt")
        if mode != "hands-free":  # the everyday default needs no badge on every card
            header.addWidget(badge(labels.mode(mode), "Gold" if mode == "cancelled" else ""))
        if str(m.get("polish_model") or "").startswith("unpolished"):
            # Every provider failed: the text is the cleaned raw transcript, not polished output.
            pf = badge("Polish failed", "Error")
            pf.setToolTip("No polish provider answered; this is the cleaned raw transcript, not polished text.")
            header.addWidget(pf)
        elif m.get("failover"):
            fo = badge("Failover", "Gold")
            fo.setToolTip(f"Primary polish provider failed; used {m['failover']}")
            header.addWidget(fo)
        preset_name = item.get("preset", "")
        if preset_name:
            header.addWidget(badge(preset_name, "Cyan"))
        mismatch = m.get("capture_mismatch")
        if mismatch:
            warn = badge("Possible dropped audio", "Error")
            warn.setToolTip(
                f"Held the toggle for {mismatch.get('wall_clock_s')}s but only "
                f"{mismatch.get('captured_s')}s of audio was captured.")
            header.addWidget(warn)

        self.lbl_view_badge = badge("Raw STT", "Gold")
        self.lbl_view_badge.setVisible(False)
        header.addWidget(self.lbl_view_badge)
        header.addStretch(1)

        self.lbl_copied = badge("Copied", "Cyan")
        self.lbl_copied.setVisible(False)
        header.addWidget(self.lbl_copied)

        # Hover actions share one container so they fade as a group.
        self.actions = QWidget()
        acts = QHBoxLayout(self.actions)
        acts.setContentsMargins(0, 0, 0, 0)
        acts.setSpacing(2)
        self.btn_toggle_raw = None
        self.btn_toggle_diff = None
        if self._raw_text:
            self.btn_toggle_raw = button("Raw", "GhostBtn", tooltip="Show the verbatim dictation before AI polish")
            self.btn_toggle_raw.setMinimumHeight(28)
            self.btn_toggle_raw.clicked.connect(self._toggle_raw_view)
            acts.addWidget(self.btn_toggle_raw)
        if self._raw_text and self._raw_text != self._cleaned_text:
            self.btn_toggle_diff = button("Diff", "GhostBtn", tooltip="Word diff: struck = removed, green = added")
            self.btn_toggle_diff.setMinimumHeight(28)
            self.btn_toggle_diff.clicked.connect(self._toggle_diff_view)
            acts.addWidget(self.btn_toggle_diff)
        if self.on_learn:
            self.btn_learn = icon_button("edit", "", "Teach a word correction from this transcript")
            self.btn_learn.clicked.connect(self._open_learn_dialog)
            acts.addWidget(self.btn_learn)
        self._audio = audio_path_for(item)
        self.btn_play = None
        if self._audio:
            self.btn_play = icon_button("play", "", "Play the recording")
            self.btn_play.clicked.connect(lambda: self._toggle_play())
            acts.addWidget(self.btn_play)
            AudioPlayer.get().changed.connect(self._on_player)
        self.btn_copy = icon_button("copy", "", "Copy transcript")
        self.btn_copy.clicked.connect(self._copy_text)
        acts.addWidget(self.btn_copy)
        if self.on_delete:
            btn_del = icon_button("delete", "", "Delete this transcript", "DangerBtn")
            btn_del.clicked.connect(lambda: self.on_delete(self))
            acts.addWidget(btn_del)
        self._fx = QGraphicsOpacityEffect(self.actions)
        self._fx.setOpacity(0.0)
        self.actions.setGraphicsEffect(self._fx)
        for b in self.actions.findChildren(QAbstractButton):
            b.installEventFilter(self)  # Tab onto an action reveals the row
        header.addWidget(self.actions)
        layout.addLayout(header)

        self.lbl_text = QLabel()
        self.lbl_text.setObjectName("Reading")
        self.lbl_text.setWordWrap(True)
        layout.addWidget(self.lbl_text)

        self.btn_more = button("Show more", "LinkBtn")
        self.btn_more.setMinimumHeight(28)
        self.btn_more.clicked.connect(self._toggle_expand)
        layout.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignLeft)

        self._apply_clamp()

    def _toggle_play(self):
        try:
            AudioPlayer.get().toggle(self._audio)
        except Exception as exc:
            self.setToolTip(f"Could not play the recording: {exc}")

    def _on_player(self, path: str):
        try:
            playing = path == self._audio
            self.btn_play.setIcon(glyph_icon("stop" if playing else "play", 14,
                                             T["cyan"] if playing else T["ink_muted"]))
            self.btn_play.setToolTip("Stop playback" if playing else "Play the recording")
            self._reveal(playing or self._engaged())
        except RuntimeError:
            pass  # card already deleted

    @staticmethod
    def _metrics_tooltip(m: dict, words: int = 0, lens: str = "") -> str:
        """Every figure here was recorded by the dictation itself (see
        LeonidasScribeApp._metrics); nothing is filled in from current settings."""
        parts = []
        total, stt, pol = m.get("total_ms") or 0, m.get("stt_ms") or 0, m.get("polish_ms") or 0
        audio = m.get("audio_seconds") or 0.0
        if total:
            per_s = f"  ({total / audio:.0f} ms per second of audio)" if audio >= 1 else ""
            parts.append(f"Total: {total:,} ms{per_s}")
        if stt:
            parts.append(f"   Speech to text: {stt:,} ms" + (f"  ·  {m['stt_model']}" if m.get("stt_model") else ""))
        if pol or m.get("polish_model"):
            parts.append(f"   Polish: {pol:,} ms" + (f"  ·  {m['polish_model']}" if m.get("polish_model") else ""))
        other = total - stt - pol
        if total and other > 50:
            parts.append(f"   Routing and dictionary: {other:,} ms")
        rec = m.get("recorded_seconds") or 0.0
        if audio and rec > audio + 0.5:
            parts.append(f"Audio: {audio:.1f}s sent of {rec:.1f}s recorded ({rec - audio:.1f}s silence trimmed)")
        elif audio:
            parts.append(f"Audio: {audio:.1f}s")
        if m.get("wpm"):
            parts.append(f"Words: {words:,}  ·  you spoke at {m['wpm']} words per minute "
                         f"({m.get('speech_seconds', 0):.0f}s of speaking time)")
        elif words:
            parts.append(f"Words: {words:,}")
        if lens:
            parts.append(f"Lens: {lens}")
        if str(m.get("polish_model") or "").startswith("unpolished"):
            parts.append("Polish failed: every provider errored, cleaned raw transcript used")
        elif m.get("failover"):
            parts.append(f"Failover: {m['failover']}")
        dropped = [d.get("text") for d in ([m.get("dropped_tail")] + list(m.get("dropped_midstream") or [])) if d]
        if dropped:
            parts.append("Hallucination filter removed: " + ", ".join(f'"{t}"' for t in dropped))
        if m.get("saved_wav"):
            parts.append(f"Audio kept: {m['saved_wav']}")
        return "\n".join(parts)

    # --- hover / focus reveal -------------------------------------------
    def _engaged(self) -> bool:
        focus = QApplication.focusWidget()
        return self.underMouse() or self.hasFocus() or (focus is not None and self.isAncestorOf(focus))

    def eventFilter(self, obj, e):
        if e.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            QTimer.singleShot(0, self, lambda: self._reveal(self._engaged()))  # after focus settles
        return False

    def _reveal(self, show: bool):
        pinned = self._view_mode != "polished"
        motion.fade_to(self._fx, 1.0 if (show or pinned) else 0.0, 150)

    def enterEvent(self, e):
        self._reveal(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        if not self._engaged():
            self._reveal(False)
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self._reveal(True)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        QTimer.singleShot(0, self, lambda: self._reveal(self._engaged()))
        super().focusOutEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self._copy_text()
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        key = e.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._copy_text()
        elif key == Qt.Key.Key_R and self.btn_toggle_raw:
            self._toggle_raw_view()
        elif key == Qt.Key.Key_D and self.btn_toggle_diff:
            self._toggle_diff_view()
        elif key == Qt.Key.Key_Delete and self.on_delete:
            self.on_delete(self)
        else:
            super().keyPressEvent(e)

    # --- view modes (API kept for test_regression_suite) ------------------
    def set_view_raw(self, show_raw: bool):
        if not self._raw_text:
            return
        target = "raw" if show_raw else "polished"
        if self._view_mode != target:
            self._set_view_mode(target)

    def _toggle_raw_view(self):
        self._set_view_mode("polished" if self._view_mode == "raw" else "raw")

    def _toggle_diff_view(self):
        self._set_view_mode("polished" if self._view_mode == "diff" else "diff")

    def _set_view_mode(self, mode: str):
        self._view_mode = mode
        self.lbl_view_badge.setVisible(mode == "raw")
        if self.btn_toggle_raw:
            self.btn_toggle_raw.setText("Polished" if mode == "raw" else "Raw")
        if self.btn_toggle_diff:
            self.btn_toggle_diff.setText("Hide Diff" if mode == "diff" else "Diff")
        self._full_text = self._raw_text if mode == "raw" else self._cleaned_text
        self._apply_clamp()
        self._reveal(self.underMouse() or self.hasFocus())

    def _apply_clamp(self):
        if self._view_mode == "diff":
            self.lbl_text.setTextFormat(Qt.TextFormat.RichText)
            self.lbl_text.setText(_build_word_diff_html(self._raw_text, self._cleaned_text))
            self.btn_more.setVisible(False)
            return
        self.lbl_text.setTextFormat(Qt.TextFormat.PlainText)
        needs_clamp = len(self._full_text) > self.CLAMP_CHARS
        self.btn_more.setVisible(needs_clamp)
        if not needs_clamp or self._expanded:
            self.lbl_text.setText(self._full_text)
            self.btn_more.setText("Show less" if self._expanded else "Show more")
            return
        cut = self._full_text[:self.CLAMP_CHARS]
        space = cut.rfind(" ")
        if space > self.CLAMP_CHARS - 60:
            cut = cut[:space]
        self.lbl_text.setText(cut.rstrip().rstrip(".,;:") + "…")
        self.btn_more.setText("Show more")

    def _toggle_expand(self):
        self._expanded = not self._expanded
        self._apply_clamp()

    def _open_learn_dialog(self):
        text_for_learn = self._raw_text if self._view_mode == "raw" else self._cleaned_text
        dlg = WordCorrectionDialog(text_for_learn, self)
        if dlg.exec():
            wrong, correct = dlg.get_values()
            if wrong and correct and self.on_learn:
                self.on_learn(wrong, correct)

    def _copy_text(self):
        text = self._raw_text if self._view_mode == "raw" else self._cleaned_text
        QApplication.clipboard().setText(text)
        self.lbl_copied.setText("RAW COPIED" if self._view_mode == "raw" else "COPIED")
        self.lbl_copied.setVisible(True)
        self.setProperty("copied", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self.copied.emit(text)
        QTimer.singleShot(1200, self._reset_copied)

    def _reset_copied(self):
        self.lbl_copied.setVisible(False)
        self.setProperty("copied", False)
        self.style().unpolish(self)
        self.style().polish(self)


# ---------------------------------------------------------------------------
# Keybind recorder
# ---------------------------------------------------------------------------
class KeybindRecorderButton(QPushButton):
    keybind_changed = Signal(str)

    def __init__(self, current_binding="Mouse5", parent=None):
        super().__init__(current_binding, parent)
        self.current_binding = current_binding or "Mouse5"
        self.is_recording = False
        self.pending_modifier_chord = ""
        self.setObjectName("KeybindBtn")
        self.setProperty("recording", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setMinimumWidth(210)
        self.setMinimumHeight(32)
        self.clicked.connect(self._toggle_recording)
        self._update_style()

    def _repolish(self):
        self.style().unpolish(self)
        self.style().polish(self)

    def _update_style(self):
        if self.is_recording:
            self.setText("Listening…  Esc to cancel")
            self.setProperty("recording", True)
        else:
            self.setText(self.current_binding)
            self.setToolTip("Click, then press the key, chord, or mouse button to bind")
            self.setProperty("recording", False)
        self._repolish()

    def _toggle_recording(self):
        if not self.is_recording:
            self.is_recording = True
            self.pending_modifier_chord = ""
            self._update_style()
            self.grabKeyboard()
            self.grabMouse()
        else:
            self._finish_recording(self.current_binding)

    def _finish_recording(self, new_binding):
        self.is_recording = False
        self.pending_modifier_chord = ""
        self.current_binding = new_binding or self.current_binding
        self.releaseKeyboard()
        self.releaseMouse()
        self._update_style()
        self.keybind_changed.emit(self.current_binding)

    def keyPressEvent(self, event):
        if not self.is_recording:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._finish_recording(self.current_binding)
            return
        mods = event.modifiers()
        parts = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if mods & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if mods & Qt.KeyboardModifier.MetaModifier:
            parts.append("Win")
        is_mod = key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
                         Qt.Key.Key_Meta, Qt.Key.Key_AltGr)
        if is_mod:
            if key == Qt.Key.Key_Meta and "Ctrl" in parts:
                self.pending_modifier_chord = "Left Ctrl + Left Win"
                self.setText("Left Ctrl + Left Win  (release to save)")
                return
            self.setText(f"{' + '.join(parts)} + …" if parts else "Listening…  press a key")
            return
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F12:
            key_name = f"F{key - Qt.Key.Key_F1 + 1}"
        elif Qt.Key.Key_A <= key <= Qt.Key.Key_Z or Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            key_name = chr(key)
        elif key == Qt.Key.Key_Space:
            key_name = "Space"
        elif key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            key_name = "Tab"
        else:
            txt = event.text().strip().upper()
            if txt and len(txt) == 1 and ord(txt) >= 32:
                key_name = txt
            else:
                raw = event.keyCombination().key().name
                key_name = raw[4:] if raw.startswith("Key_") else raw
        if not key_name:
            return
        chord_parts = [p for p in parts if p.upper() != key_name.upper()]
        chord_parts.append(key_name)
        self._finish_recording(" + ".join(chord_parts))

    def keyReleaseEvent(self, event):
        if self.is_recording and self.pending_modifier_chord:
            self._finish_recording(self.pending_modifier_chord)
            return
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event):
        if not self.is_recording:
            super().mousePressEvent(event)
            return
        mapping = {Qt.MouseButton.XButton1: "Mouse4", Qt.MouseButton.XButton2: "Mouse5",
                   Qt.MouseButton.MiddleButton: "MiddleClick", Qt.MouseButton.RightButton: "RightClick"}
        if event.button() in mapping:
            self._finish_recording(mapping[event.button()])


# ---------------------------------------------------------------------------
# Toggle switch + setting rows
# ---------------------------------------------------------------------------
class ToggleSwitch(QCheckBox):
    """Switch that stays a QCheckBox, so isChecked()/setChecked() call sites are
    untouched. On = cyan track (interaction colour), off = obsidian-700."""
    W, H = 40, 22

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(self.W, self.H)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on):
        self._anim.stop()
        if not motion.enabled():
            self._set_knob(1.0 if on else 0.0)
            return
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _get_knob(self):
        return self._pos

    def _set_knob(self, v):
        self._pos = float(v)
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    @staticmethod
    def _mix(a: QColor, b: QColor, t: float) -> QColor:
        return QColor(round(a.red() + (b.red() - a.red()) * t),
                      round(a.green() + (b.green() - a.green()) * t),
                      round(a.blue() + (b.blue() - a.blue()) * t))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track_rect = QRectF(0.5, 0.5, self.W - 1, self.H - 1)
        radius = track_rect.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._mix(QColor(T["obsidian_700"]), QColor(T["cyan"]), self._pos))
        p.drawRoundedRect(track_rect, radius, radius)
        knob_d = self.H - 6.0
        knob_x = 3.0 + (self.W - knob_d - 6.0) * self._pos
        p.setBrush(self._mix(QColor(T["ink_muted"]), QColor(T["obsidian_950"]), self._pos))
        p.drawEllipse(QRectF(knob_x, 3.0, knob_d, knob_d))

    def sizeHint(self):
        return QSize(self.W, self.H)


def setting_row(label_text: str, control: QWidget, hint: str = "") -> QWidget:
    """Label (+ hint) left, control hard right."""
    row = QWidget()
    row.setObjectName("SettingRow")
    h = QHBoxLayout(row)
    h.setContentsMargins(16, 12, 16, 12)
    h.setSpacing(16)
    col = QVBoxLayout()
    col.setSpacing(2)
    lbl = QLabel(label_text)
    lbl.setObjectName("RowLabel")
    lbl.setWordWrap(True)
    col.addWidget(lbl)
    if hint:
        sub = QLabel(hint)
        sub.setObjectName("Helper")
        sub.setWordWrap(True)
        col.addWidget(sub)
    h.addLayout(col, 1)
    h.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def setting_group(rows) -> QFrame:
    """Rounded container holding setting rows separated by hairlines."""
    group = QFrame()
    group.setObjectName("SettingGroup")
    v = QVBoxLayout(group)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    for i, row in enumerate(rows):
        if i:
            sep = QFrame()
            sep.setObjectName("Divider")
            sep.setFixedHeight(1)
            v.addWidget(sep)
        v.addWidget(row)
    return group
