"""
Transcripts and Transforms pages: the same quiet card list over two slices of
history.jsonl (dictations vs preset transforms).

Search runs over the whole history, not just the rendered page; cards render
50 at a time with a "Show more" link, and deletes are deferred behind an Undo
toast instead of a blocking confirm.
"""
import collections
from datetime import date, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QStackedWidget, QLabel

from ..icons import glyph_icon
from ..styles import T
from ..motion import Hover
from ..widgets import (
    TranscriptCard, Skeleton, EmptyState, page_header, button, run_async,
)


def _key(item):
    return item.get("timestamp"), item.get("cleaned_text")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def day_label(day: str, today: date = None) -> str:
    """Today, Yesterday, then "Wednesday, September 23rd, 2026"."""
    today = today or date.today()
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day or "Undated"
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return f"{d.strftime('%A, %B')} {_ordinal(d.day)}, {d.year}"


class _DayHeader(QWidget):
    """Click (or Enter/Space when focused) to collapse or expand that day's cards."""
    def __init__(self, on_click):
        super().__init__()
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._on_click()
        else:
            super().keyPressEvent(event)


def _day_header(label: str, meta: str, collapsed: bool = False, on_click=None) -> QWidget:
    w = _DayHeader(on_click) if on_click else QWidget()
    w.setToolTip("Expand" if collapsed else "Collapse")
    row = QHBoxLayout(w)
    row.setContentsMargins(4, 10, 12, 0)
    title = QLabel(f"{'▸' if collapsed else '▾'}  {label}" if on_click else label)
    title.setObjectName("CardTitle")
    row.addWidget(title)
    row.addStretch(1)
    info = QLabel(meta)
    info.setObjectName("Meta")
    row.addWidget(info)
    return w


class TranscriptsView(QWidget):
    PAGE = 5  # days per page, newest first; 14 built ~50 cards up front and cost ~370ms on first dashboard open
    PER_DAY = 10  # cards a day shows before its "Show all" link

    def __init__(self, host, key, title, subtitle, placeholder, load_kwargs, empty_title, empty_body):
        super().__init__()
        self.host = host
        self.key = key
        self.load_kwargs = dict(load_kwargs)
        self.all_items = []
        self.cards = []
        self._headers = []
        self.shown = self.PAGE
        self._open_days = set()
        self._collapsed_days = set()
        self.all_raw_mode = False
        self._loaded_once = False
        self._pending_delete = set()
        self._load_gen = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 24, 16)
        layout.setSpacing(16)

        self.btn_toggle_all_raw = button("Show raw", "GhostBtn", "shuffle",
                                         "Show every card's verbatim dictation before AI polish")
        self.btn_toggle_all_raw.setCheckable(True)
        self.btn_toggle_all_raw.toggled.connect(self._toggle_all_raw)
        layout.addLayout(page_header(title, subtitle, [self.btn_toggle_all_raw]))

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(placeholder)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumHeight(36)
        self.search_input.addAction(glyph_icon("search", 14, T["ink_dim"]), QLineEdit.ActionPosition.LeadingPosition)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._render)
        self.search_input.textChanged.connect(lambda _: self._restart_search())
        layout.addWidget(self.search_input)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.skeleton = Skeleton(rows=5, row_h=92)
        sk_wrap = QWidget()
        skl = QVBoxLayout(sk_wrap)
        skl.setContentsMargins(0, 0, 8, 0)
        skl.addWidget(self.skeleton)
        skl.addStretch(1)
        self.stack.addWidget(sk_wrap)

        scroll, container = host.scroll_host(self)
        self.card_layout = QVBoxLayout(container)
        self.card_layout.setContentsMargins(0, 0, 8, 8)
        self.card_layout.setSpacing(12 - 2 * Hover.LIFT)  # cards carry LIFT px of hover room each side
        self.btn_more = button("Show older days", "LinkBtn")
        self.btn_more.clicked.connect(self._show_more)
        self.card_layout.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignHCenter)
        self.card_layout.addStretch(1)
        self.stack.addWidget(scroll)

        start = button("Start dictating", "", "mic")
        start.clicked.connect(host.toggle_dictation)
        self.empty = EmptyState("mic", empty_title, empty_body, start)
        self.stack.addWidget(self.empty)
        self._empty_defaults = (empty_title, empty_body)

        clear = button("Clear search", "", "close")
        clear.clicked.connect(self.search_input.clear)
        self.no_match = EmptyState("search", "No matches", "", clear)
        self.stack.addWidget(self.no_match)

    # ------------------------------------------------------------------
    def refresh(self):
        if not self._loaded_once:
            self.stack.setCurrentIndex(0)
        kwargs = dict(self.load_kwargs, limit=100000)  # search spans the whole history
        self._load_gen += 1
        gen = self._load_gen
        run_async(lambda: self.host.controller.load_history(**kwargs), lambda r: self._loaded(r, gen))

    def _loaded(self, items, gen=None):
        if gen is not None and gen != self._load_gen:
            return  # a newer read is in flight; an older, slower one must not overwrite it
        if isinstance(items, Exception):
            self.host.toast(f"Could not read history: {items}", "error")
            items = []
        self._loaded_once = True
        self.all_items = items
        self._render()

    def _restart_search(self):
        self.shown = self.PAGE
        self._debounce.start()

    def _filtered(self):
        query = self.search_input.text().strip().lower()
        live = [it for it in self.all_items if _key(it) not in self._pending_delete]
        if not query:
            return live
        return [it for it in live
                if query in (it.get("cleaned_text") or "").lower() or query in (it.get("raw_text") or "").lower()]

    def _render(self):
        items = self._filtered()
        for card in self.cards + self._headers:
            card.setParent(None)  # detach first: a dangling wrapper crashes the 3.14 GC
            card.deleteLater()
        self.cards, self._headers = [], []
        if not items:
            query = self.search_input.text().strip()
            if query:
                self.no_match.lbl_body.setText(f"Nothing in {len(self.all_items):,} transcripts matches “{query}”.")
                self.stack.setCurrentWidget(self.no_match)
            else:
                self.stack.setCurrentWidget(self.empty)
            return
        per_day = collections.Counter()
        words_day = collections.Counter()
        for it in items:
            d = (it.get("timestamp") or "")[:10]
            per_day[d] += 1
            # Spoken words (raw) for dictations, output words for transforms: the same
            # figure Insights and Home use.
            spoken = it.get("mode") not in ("preset_transform", "refactor") and it.get("raw_text")
            words_day[d] += len((it.get("raw_text") if spoken else it.get("cleaned_text") or "")
                                .replace("⏸", " ").split())
        by_day = {}
        for item in items:  # newest first, so days come out in date order
            by_day.setdefault((item.get("timestamp") or "")[:10], []).append(item)
        days = list(by_day)
        for day in days[:self.shown]:
            n = per_day[day]
            collapsed = day in self._collapsed_days
            header = _day_header(day_label(day), f"{n} {'transcript' if n == 1 else 'transcripts'}  ·  "
                                                 f"{words_day[day]:,} words", collapsed,
                                 lambda d=day: self._toggle_day(d))
            self._headers.append(header)
            self.card_layout.insertWidget(self.card_layout.count() - 2, header)
            if collapsed:
                continue
            day_items = by_day[day]
            limit = len(day_items) if day in self._open_days else self.PER_DAY
            for item in day_items[:limit]:
                card = TranscriptCard(item, on_delete_callback=self._delete,
                                      on_learn_callback=self.host.learn_correction, time_only=True)
                if self.all_raw_mode:
                    card.set_view_raw(True)
                self.cards.append(card)
                self.card_layout.insertWidget(self.card_layout.count() - 2, card)
            if len(day_items) > limit:
                more = button(f"Show all {len(day_items):,} from {day_label(day).split(', ', 1)[-1]}", "LinkBtn")
                more.clicked.connect(lambda _=False, d=day: self._open_day(d))
                self._headers.append(more)
                self.card_layout.insertWidget(self.card_layout.count() - 2, more, 0, Qt.AlignmentFlag.AlignHCenter)
        remaining = len(days) - self.shown
        self.btn_more.setVisible(remaining > 0)
        self.btn_more.setText(f"Show {min(self.PAGE, remaining)} older days of {remaining:,}")
        self.stack.setCurrentIndex(1)

    def _toggle_day(self, day):
        self._collapsed_days ^= {day}
        self._render()

    def _open_day(self, day):
        self._open_days.add(day)
        self._render()

    def _show_more(self):
        self.shown += self.PAGE
        self._render()

    def _toggle_all_raw(self, on):
        self.all_raw_mode = on
        self.btn_toggle_all_raw.setText("Show polished" if on else "Show raw")
        for card in self.cards:
            card.set_view_raw(on)

    def _delete(self, card):
        """Hide now, delete when the Undo toast expires."""
        item = card.item
        self._pending_delete.add(_key(item))
        card.hide()

        def undo():
            self._pending_delete.discard(_key(item))
            self._render()

        def commit():
            self._pending_delete.discard(_key(item))
            ok, msg = self.host.controller.delete_history_item(item)
            if ok:
                self.all_items = [it for it in self.all_items if _key(it) != _key(item)]
            else:
                self._render()
                self.host.toast(f"Delete failed: {msg}", "error")

        self.host.toast("Transcript deleted", "info", "Undo", undo, commit)


if __name__ == "__main__":
    t = date(2026, 9, 25)
    assert day_label("2026-09-25", t) == "Today" and day_label("2026-09-24", t) == "Yesterday"
    assert day_label("2026-09-23", t) == "Wednesday, September 23rd, 2026", day_label("2026-09-23", t)
    assert day_label("2026-09-11", t).endswith("11th, 2026") and day_label("2026-09-22", t).endswith("22nd, 2026")
    print("day labels ok")
