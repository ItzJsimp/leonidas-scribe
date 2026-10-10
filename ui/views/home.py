"""
Home: Scribe at a glance. A live greeting, today's numbers, the latest
transcripts, this week's voice profile and a health line. Every figure is read
from history.jsonl and the newest reports/voice-profile-*.json on refresh, which
the shell triggers on every history write, so nothing here is ever typed in.
"""
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from .. import labels
from ..widgets import TranscriptCard, page_header, section_label, kpi_tile, make_card, button, run_async


def _since(day: str) -> str:
    from .transcripts import day_label
    return day_label(day).split(", ", 1)[-1] if day else "day one"


def fmt_saved(minutes: float) -> str:
    """Net minutes saved as 18m / 2.4h / -3m. Negative is honest: short takes can
    cost more than typing them would have."""
    m = float(minutes or 0)
    sign = "-" if m < 0 else ""
    m = abs(m)
    if m < 60:
        return f"{sign}{m:.0f}m"
    return f"{sign}{m / 60:.1f}h"


import os


def resolve_user_name(config: dict = None) -> str:
    """
    Resolves display name for the Home greeting:
    1. config.json 'user.name' if explicitly configured.
    2. Windows environment USERNAME if available.
    """
    if config:
        name = config.get("user", {}).get("name", "").strip()
        if name:
            return name
    u = os.environ.get("USERNAME", "").strip()
    if u and u.lower() not in ("administrator", "default", "system", "user"):
        return u.capitalize() if u.islower() else u
    return ""


def greeting(name=None, now: datetime = None) -> str:
    if isinstance(name, datetime):
        now = name
        name = ""
    name_str = (name or "").strip()
    h = (now or datetime.now()).hour
    period = "Late night" if (h < 5 or h >= 22) else ("Good morning" if h < 12 else ("Good afternoon" if h < 17 else "Good evening"))
    if name_str:
        return f"{period}, {name_str}"
    return period


class HomeView(QWidget):
    def _user_name(self) -> str:
        cfg = getattr(self.host, "config", None) or {}
        return resolve_user_name(cfg)

    def __init__(self, host):
        super().__init__()
        self.host = host
        scroll, content = host.scroll_host(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        v = QVBoxLayout(content)
        v.setContentsMargins(28, 24, 24, 28)
        v.setSpacing(12)

        self.header = page_header(greeting(self._user_name()), "")
        self.lbl_title = self.header.itemAt(0).layout().itemAt(0).widget()
        v.addLayout(self.header)

        self.lbl_line = QLabel("")
        self.lbl_line.setObjectName("Helper")
        self.lbl_line.setWordWrap(True)
        v.addWidget(self.lbl_line)

        # goal strip: level, today's goal and quests, one click to the Goals page
        from .goals import Meter
        self.goal_card, gb = make_card()
        self.goal_card.setToolTip("Your level and today's goal. Click for quests and achievements.")
        self.goal_card.setCursor(Qt.CursorShape.PointingHandCursor)
        self.goal_card.mouseReleaseEvent = lambda _e: host.select_view(host.goals_view)
        grow = QHBoxLayout()
        grow.setSpacing(16)
        self.lbl_level = QLabel("")
        self.lbl_level.setObjectName("GoalNum")
        grow.addWidget(self.lbl_level)
        gcol = QVBoxLayout()
        gcol.setSpacing(6)
        self.lbl_goal = QLabel("")
        self.lbl_goal.setObjectName("Body")
        self.goal_bar = Meter("cyan", 8)
        gcol.addWidget(self.lbl_goal)
        gcol.addWidget(self.goal_bar)
        grow.addLayout(gcol, 1)
        open_goals = button("Goals", "LinkBtn", None, "Open quests, ranks and achievements")
        open_goals.clicked.connect(lambda: host.select_view(host.goals_view))
        grow.addWidget(open_goals)
        gb.addLayout(grow)
        v.addWidget(self.goal_card)

        v.addSpacing(4)
        v.addWidget(section_label("Today"))
        row = QHBoxLayout()
        row.setSpacing(12)
        c0, self.k_saved, self.k_saved_note = kpi_tile("Time saved today", gold=True)
        c0.setToolTip("Time it would take to type your dictated words at your 65-75 wpm, "
                      "minus the time you actually spent speaking and waiting for Scribe.")
        c1, self.k_words, self.k_words_note = kpi_tile("Words today", gold=True)
        c2, self.k_pace, self.k_pace_note = kpi_tile("Speaking pace", gold=True)
        c3, self.k_lat, self.k_lat_note = kpi_tile("Median latency today", gold=True)
        c4, self.k_streak, self.k_streak_note = kpi_tile("Day streak", gold=True)
        for c in (c0, c1, c2, c3, c4):
            row.addWidget(c, 1)
        v.addLayout(row)

        v.addSpacing(8)
        cols = QHBoxLayout()
        cols.setSpacing(12)
        def header_row(text, link_btn=None):
            """Same widget/spacing as both column headers, so a bare label (no link
            button on this column) still reserves the LinkBtn's 32px row height and
            the section titles and card tops beneath them line up."""
            row = QHBoxLayout()
            lbl = section_label(text)
            lbl.setMinimumHeight(32)
            row.addWidget(lbl)
            row.addStretch(1)
            if link_btn:
                row.addWidget(link_btn)
            return row

        left = QVBoxLayout()
        left.setSpacing(10)
        see_all = button("All transcripts", "LinkBtn")
        see_all.clicked.connect(lambda: host.select_view(host.history_view))
        left.addLayout(header_row("Latest", see_all))
        self.latest_box = QVBoxLayout()
        self.latest_box.setSpacing(8)
        left.addLayout(self.latest_box)
        left.addStretch(1)
        cols.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addLayout(header_row("This week"))
        open_insights = button("Open Insights", "LinkBtn")
        open_insights.clicked.connect(lambda: host.select_view(host.insights_view))
        self.week_card, week_body = make_card("Voice profile", "", open_insights)
        self.lbl_week = QLabel("")
        self.lbl_week.setObjectName("Body")
        self.lbl_week.setWordWrap(True)
        week_body.addWidget(self.lbl_week)
        right.addWidget(self.week_card)
        health_card, health_body = make_card("Health")
        self.lbl_health = QLabel("")
        self.lbl_health.setObjectName("Body")
        self.lbl_health.setWordWrap(True)
        self.lbl_health.setTextFormat(Qt.TextFormat.RichText)
        health_body.addWidget(self.lbl_health)
        right.addWidget(health_card)
        right.addStretch(1)
        cols.addLayout(right, 2)
        v.addLayout(cols)
        v.addStretch(1)
        self._cards = []

    def refresh(self):
        self.lbl_title.setText(greeting(self._user_name()).upper())
        run_async(self._load, self._apply)

    def _load(self):
        import voice_profile
        c = self.host.controller
        saved = c.time_saved() if hasattr(c, "time_saved") else None
        return (c.load_insights(), c.load_history(limit=3, exclude_modes=["preset_transform"]),
                voice_profile.latest(), saved, self.host.goals_snapshot())

    def _apply(self, result):
        if isinstance(result, Exception):
            self.host.toast(f"Could not load Home: {result}", "error")
            return
        d, latest, report, saved, gs = result
        t = gs["today"]
        quests = sum(q["done"] for q in gs["quests"])
        self.lbl_level.setText(f"LV {gs['level']}")
        self.lbl_goal.setText(f"{gs['rank']}  ·  {t['words']:,} / {t['goal']:,} words today  ·  "
                              f"{t['deep']} deep prompts  ·  quests {quests}/3  ·  {gs['streak']}-day streak")
        self.goal_bar.animate_to(t["words"] / max(1, t["goal"]))
        n, w = d["today_transcripts"], d["today_words"]
        self.lbl_line.setText(
            f"{w:,} words across {n} {'dictation' if n == 1 else 'dictations'} today. "
            f"{d['words']:,} words since {_since(d['first_day'])}." if n else
            f"Nothing dictated yet today. Press {self.host.toggle_key()} anywhere to start.")

        self.k_words.setText(f"{w:,}")
        avg = d["week_avg_words"]
        self.k_words_note.setText(f"7-day average {avg:,} a day" if avg else "")
        # Falls back through today -> this week -> all-time, whichever has samples; the
        # note says which window it actually came from so the number is never unlabeled.
        pace, pace_window = (d["today_wpm"], "today") if d["today_wpm"] else \
            (d["wpm_7d"], "7-day avg") if d["wpm_7d"] else (d["wpm"], "all-time")
        self.k_pace.setText(f"{pace:.0f}" if pace else f"~{d['wpm_estimate']:.0f}" if d["wpm_estimate"] else "-")
        self.k_pace_note.setText(f"words per minute, {pace_window}" if pace else "words per minute, estimated")
        self.k_lat.setText(f"{d['today_p50'] / 1000:.2f}s" if d["today_p50"] else "-")
        self.k_lat_note.setText(f"All-time median {d['p50'] / 1000:.2f}s" if d["p50"] else "")
        self.k_streak.setText(f"{d['streak_days']}d")
        self.k_streak_note.setText("days in a row with a dictation")
        self._apply_saved(saved)

        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        for item in latest:
            card = TranscriptCard(item, on_delete_callback=None, on_learn_callback=None)
            self._cards.append(card)
            self.latest_box.addWidget(card)

        summary = (report or {}).get("summary") or {}
        stats = (report or {}).get("stats") or {}
        if stats:
            themes = "".join(f"<br>·  {t}" for t in (summary.get("themes") or [])[:4])
            period = " to ".join(report.get("period") or [])
            self.lbl_week.setText(
                (f"{summary.get('headline', '')}{themes}<br><br>" if summary else "")
                + f"{stats['words']:,} words in {stats['dictations']} dictations ({period}).")
            self.lbl_week.setTextFormat(Qt.TextFormat.RichText)
        else:
            self.lbl_week.setText("Your first weekly profile builds automatically within a few minutes of launch.")

        limit = int(self.host.config.get("alerts", {}).get("latency_median_ms", 2500))
        recent = d["today_p50"] or d["p50"]
        ok = recent and recent <= limit
        top_app = labels.app(d["weekly"]["top_app"]) or "-"
        self.lbl_health.setText(
            f"Latency {'within' if ok else 'over'} your {limit / 1000:.1f}s limit "
            f"({recent / 1000:.2f}s median).<br>Most dictation lands in {top_app} this week."
            if recent else "No latency measured yet.")


    def _apply_saved(self, saved):
        if not saved:
            self.k_saved.setText("-")
            self.k_saved_note.setText("")
            return
        t, wk, total = saved["today"], saved["week"], saved["all"]
        self.k_saved.setText(fmt_saved(t["saved_min"]))
        self.k_saved_note.setText(f"{fmt_saved(wk['saved_min'])} this week, {fmt_saved(total['saved_min'])} in total")
        self.k_saved_note.setToolTip(
            f"Today: {t['words']:,} words in {t['dictations']} dictations. "
            f"Range {fmt_saved(t['saved_min_low'])} (at 75 wpm) to {fmt_saved(t['saved_min_high'])} (at 65 wpm).")
        if t["dictations"] and t["saved_min"] > 0:
            self.lbl_line.setText(self.lbl_line.text() + f" That's about {fmt_saved(t['saved_min'])} of typing you skipped.")


if __name__ == "__main__":
    assert fmt_saved(18.4) == "18m" and fmt_saved(150) == "2.5h" and fmt_saved(-3.2) == "-3m"
    assert greeting(now=datetime(2026, 9, 25, 9)) == "Good morning"
    assert greeting("Alex", datetime(2026, 9, 25, 9)) == "Good morning, Alex"
    assert greeting("Sam", datetime(2026, 9, 25, 23)) == "Late night, Sam"
    assert greeting(datetime(2026, 9, 25, 23)) == "Late night"
    print("home ok")
