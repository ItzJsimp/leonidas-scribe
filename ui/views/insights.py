"""
Insights: a daily reason to open the page (Today strip in gold), then the
all-time totals, the week against last week, latency (split + regression
radar), habits, and the activity heatmap.
"""
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QStackedWidget

from .. import labels
from ..charts import SplitBar, BarList, DayBars, Heatmap
from ..widgets import (
    Skeleton, page_header, section_label, kpi_tile, make_card, badge, run_async, button,
)


def _pct_change(now, before) -> str:
    if not before:
        return "new"
    return f"{(now / before - 1) * 100:+.0f}%"


class InsightsView(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._loaded_once = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        sk = QWidget()
        skl = QVBoxLayout(sk)
        skl.setContentsMargins(28, 24, 32, 24)
        skl.setSpacing(16)
        skl.addLayout(page_header("Insights", "Measured from every dictation on this machine."))
        skl.addWidget(Skeleton(rows=4, row_h=120))
        skl.addStretch(1)
        self.stack.addWidget(sk)

        scroll, content = host.scroll_host(self)
        self.stack.addWidget(scroll)
        v = QVBoxLayout(content)
        v.setContentsMargins(28, 24, 24, 28)
        v.setSpacing(12)
        v.addLayout(page_header("Insights", "Measured from every dictation on this machine."))

        # --- Today (gold KPIs) -----------------------------------------
        v.addSpacing(4)
        v.addWidget(section_label("Today"))
        today = QHBoxLayout()
        today.setSpacing(12)
        c1, self.lbl_today_words, self.lbl_today_words_note = kpi_tile("Words today", gold=True)
        c2, self.lbl_streak, self.lbl_streak_note = kpi_tile("Day streak", gold=True)
        c3, self.lbl_saved, self.lbl_saved_note = kpi_tile("Time saved today", gold=True)
        c4, self.lbl_vs_avg, self.lbl_vs_avg_note = kpi_tile("Vs usual by now", gold=True)
        for c in (c1, c2, c3, c4):
            today.addWidget(c, 1)
        v.addLayout(today)

        # --- All time --------------------------------------------------
        v.addSpacing(8)
        v.addWidget(section_label("All time"))
        stats = QHBoxLayout()
        stats.setSpacing(12)
        c1, self.lbl_words, self.lbl_words_note = kpi_tile("Words dictated")
        c2, self.lbl_speed, self.lbl_speed_note = kpi_tile("Speaking pace, all time")
        c3, self.lbl_lat, self.lbl_lat_note = kpi_tile("Median latency, all time")
        c4, self.lbl_saved_total, self.lbl_saved_total_note = kpi_tile("Time saved")
        for c in (c1, c2, c3, c4):
            stats.addWidget(c, 1)
        v.addLayout(stats)

        # --- Weekly voice profile -----------------------------------------
        v.addSpacing(8)
        v.addWidget(section_label("Voice profile"))
        self.btn_profile = button("Rebuild now", "GhostBtn", "sync",
                                  "Rebuild this week's voice profile from your transcripts (one polish-model call)")
        self.btn_profile.clicked.connect(self._rebuild_profile)
        prof_card, prof_body = make_card("This week in your voice", "", self.btn_profile)
        self.lbl_prof_head = QLabel("")
        self.lbl_prof_head.setObjectName("Body")
        self.lbl_prof_head.setWordWrap(True)
        prof_body.addWidget(self.lbl_prof_head)
        self.prof_grid = QGridLayout()
        self.prof_grid.setHorizontalSpacing(24)
        self.prof_grid.setVerticalSpacing(10)
        prof_body.addLayout(self.prof_grid)
        self.lbl_prof_foot = QLabel("")
        self.lbl_prof_foot.setObjectName("Meta")
        self.lbl_prof_foot.setWordWrap(True)
        prof_body.addWidget(self.lbl_prof_foot)
        v.addWidget(prof_card)

        # --- This week -------------------------------------------------
        v.addSpacing(8)
        v.addWidget(section_label("This week"))
        week_card, week_body = make_card("Last 7 days", "Words per day, today in gold.")
        row = QHBoxLayout()
        row.setSpacing(24)
        self.day_bars = DayBars()
        row.addWidget(self.day_bars, 3)
        facts = QGridLayout()
        facts.setHorizontalSpacing(16)
        facts.setVerticalSpacing(10)
        self.week_cells = {}
        for r, (key, name) in enumerate((("words", "Words"), ("transcripts", "Transcripts"),
                                         ("p50", "Median latency"), ("top_app", "Top app"))):
            lbl = QLabel(name)
            lbl.setObjectName("Helper")
            val = QLabel("-")
            val.setObjectName("Num")
            delta = QLabel("")
            delta.setObjectName("Meta")
            facts.addWidget(lbl, r, 0)
            facts.addWidget(val, r, 1, Qt.AlignmentFlag.AlignRight)
            facts.addWidget(delta, r, 2, Qt.AlignmentFlag.AlignRight)
            self.week_cells[key] = (val, delta)
        row.addLayout(facts, 2)
        week_body.addLayout(row)
        v.addWidget(week_card)

        # --- Latency ---------------------------------------------------
        v.addSpacing(8)
        v.addWidget(section_label("Latency"))
        split_card, split_body = make_card("Where the time goes",
                                           "Median split across every audio dictation since metrics landed.")
        self.bar_latency = SplitBar([])
        split_body.addWidget(self.bar_latency)
        self.lbl_lat_detail = QLabel()
        self.lbl_lat_detail.setObjectName("Helper")
        self.lbl_lat_detail.setWordWrap(True)
        split_body.addWidget(self.lbl_lat_detail)
        v.addWidget(split_card)

        radar_card, radar_body = make_card(
            "Regression radar",
            "Each model and lens pairing against its own trailing 7-day baseline. "
            "Drifting means 25%+ slower for 2 active days in a row.")
        self.radar_grid = QGridLayout()
        self.radar_grid.setHorizontalSpacing(16)
        self.radar_grid.setVerticalSpacing(8)
        radar_body.addLayout(self.radar_grid)
        v.addWidget(radar_card)

        # --- Habits ----------------------------------------------------
        v.addSpacing(8)
        v.addWidget(section_label("Habits"))
        habits = QHBoxLayout()
        habits.setSpacing(12)
        card_mode, body_mode = make_card("How you dictate", "Transcripts by trigger.")
        self.bars_mode = BarList([])
        body_mode.addWidget(self.bars_mode)
        body_mode.addStretch(1)
        habits.addWidget(card_mode, 1)
        card_app, body_app = make_card("Where it lands", "Transcripts by destination app.")
        self.bars_app = BarList([], empty_text="Collecting from the next dictation onward.")
        body_app.addWidget(self.bars_app)
        body_app.addStretch(1)
        habits.addWidget(card_app, 1)
        v.addLayout(habits)

        card_heat, body_heat = make_card("Activity", "One square per day. Today is outlined in gold.")
        self.heatmap = Heatmap({})
        body_heat.addWidget(self.heatmap)
        v.addWidget(card_heat)
        v.addStretch(1)

    # ------------------------------------------------------------------
    def refresh(self):
        if not self._loaded_once:
            self.stack.setCurrentIndex(0)
        run_async(self._load, self._apply)

    def _load(self):
        import voice_profile
        return self.host.controller.load_insights(), voice_profile.latest()

    def _rebuild_profile(self):
        import voice_profile
        self.btn_profile.setEnabled(False)
        self.btn_profile.setText("Building...")

        def done(result):
            self.btn_profile.setEnabled(True)
            self.btn_profile.setText("Rebuild now")
            if isinstance(result, Exception):
                self.host.toast(f"Voice profile failed: {result}", "error")
            self.refresh()
        run_async(voice_profile.build, done)

    def _render_profile(self, rep: dict):
        while self.prof_grid.count():
            item = self.prof_grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        st, sm = (rep or {}).get("stats"), (rep or {}).get("summary") or {}
        if not st:
            self.lbl_prof_head.setText("Builds automatically once a week from your transcripts. "
                                       "Press Rebuild now to make the first one.")
            self.lbl_prof_foot.setText("")
            return
        themes = "".join(f"<br>·  {t}" for t in sm.get("themes") or [])
        self.lbl_prof_head.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_prof_head.setText(
            (f"<b>{sm.get('headline', '')}</b>{themes}<br><br>{sm.get('style', '')}"
             + (f"<br><br><span>Tip: {sm['tip']}</span>" if sm.get("tip") else "")) if sm else
            "Summary unavailable this week (the polish provider did not answer); the numbers below are complete.")
        change = (f"{(st['words'] / st['prev_words'] - 1) * 100:+.0f}% vs the week before"
                  if st.get("prev_words") else "first week on record")
        blocks = [
            ("WORDS", f"{st['words']:,} in {st['dictations']} dictations, {change}"),
            ("PACE THIS WEEK", (f"{st['wpm']} words per minute" + ("" if st.get("wpm_measured") else " (estimated)"))
             if st.get("wpm") else "-"),
            ("CATCHPHRASES", ", ".join(f"\u201c{p}\u201d \u00d7{n}" for p, n in st["catchphrases"][:6]) or "-"),
            ("MOST USED WORDS", ", ".join(w for w, _ in st["top_words"][:10]) or "-"),
            ("FILLERS PER 1,000 WORDS", ", ".join(f"{f} {r}" for f, _, r in st["fillers"][:6]) or "-"),
            ("DICTIONARY FIXED", ", ".join(f"{c} \u00d7{n}" for c, n in st["corrections"][:6]) or "nothing this week"),
            ("POLISH REMOVED MOST", ", ".join(f"\u201c{w}\u201d \u00d7{n}" for w, n in st["polish_removed"][:6]) or "-"),
            ("WHERE AND WHEN", ", ".join(filter(None, [
                ", ".join(labels.app(a) for a, _ in st["top_apps"][:3]),
                f"busiest {st['busiest_day']}" if st.get("busiest_day") else "",
                f"peak hour {datetime.strptime(str(st['busiest_hour']), '%H').strftime('%I %p').lstrip('0')}"
                if st.get("busiest_hour") is not None else ""]))),
        ]
        for r, (head, text) in enumerate(blocks):
            h = QLabel(head)
            h.setObjectName("KpiLabel")
            t = QLabel(text)
            t.setObjectName("Body")
            t.setWordWrap(True)
            self.prof_grid.addWidget(h, r, 0, Qt.AlignmentFlag.AlignTop)
            self.prof_grid.addWidget(t, r, 1)
        self.prof_grid.setColumnStretch(1, 1)
        self.lbl_prof_foot.setText(f"{rep['period'][0]} to {rep['period'][1]}  \u00b7  built "
                                   f"{rep['generated'].replace('T', ' ')[:16]}"
                                   + (f"  \u00b7  summary by {sm['model']}" if sm.get("model") else ""))

    def _apply(self, result):
        if isinstance(result, Exception):
            self.host.toast(f"Could not compute insights: {result}", "error")
            return
        d, rep = result
        self._render_profile(rep)
        self._loaded_once = True
        self.stack.setCurrentIndex(1)

        self.lbl_today_words.setText(f"{d['today_words']:,}")
        self.lbl_today_words_note.setText(f"{d['today_transcripts']} dictations today")
        self.lbl_streak.setText(f"{d['streak_days']}d")
        self.lbl_streak_note.setText("Consecutive days with a dictation")
        self.lbl_saved.setText(f"{d['time_saved_min']}m")
        self.lbl_saved_note.setText("Versus typing the same words at 40 WPM")
        avg = d["week_avg_words"]
        # Pro-rated to the time of day: comparing a full day's average against however
        # much of today has elapsed made every morning look like a bad day.
        now = datetime.now()
        day_fraction = (now.hour * 3600 + now.minute * 60 + now.second) / 86400
        expected_by_now = avg * day_fraction if avg else 0
        vs_usual = _pct_change(d["today_words"], expected_by_now) if expected_by_now else "-"
        self.lbl_vs_avg.setText(vs_usual)
        self.lbl_vs_avg_note.setText(f"7-day average: {avg:,} words a day" if avg else "")
        # Gold reads as good news; a negative delta gets the neutral ink token instead.
        self.lbl_vs_avg.setObjectName("Kpi" if vs_usual.startswith("-") else "KpiGold")
        self.lbl_vs_avg.style().unpolish(self.lbl_vs_avg)
        self.lbl_vs_avg.style().polish(self.lbl_vs_avg)

        self.lbl_words.setText(f"{d['words']:,}")
        books = d["words"] / 80000.0
        self.lbl_words_note.setText(f"Spoken, across {d['transcripts']:,} transcripts over {d['days_active']} days"
                                    + (f", about {books:.1f} books" if books >= 0.1 else ""))
        if d["wpm"]:
            self.lbl_speed.setText(f"{d['wpm']:.0f}")
            self.lbl_speed_note.setText(
                f"Words per minute, measured over {d['wpm_samples_minutes']:.0f} min of speaking"
                + (f"; this week {d['wpm_7d']:.0f}" if d["wpm_7d"] else ""))
        else:
            self.lbl_speed.setText(f"~{d['wpm_estimate']:.0f}" if d["wpm_estimate"] else "-")
            self.lbl_speed_note.setText("Estimated from total audio; measured pace starts with your next dictations")
        self.lbl_lat.setText(f"{d['p50'] / 1000:.2f}s" if d["p50"] else "-")
        self.lbl_lat_note.setText(f"p90 {d['p90'] / 1000:.1f}s over {d['latency_samples']:,} samples"
                                  if d["p90"] else "Not enough samples yet")
        self.lbl_saved_total.setText(f"{d['time_saved_total_hours']:.0f}h")
        self.lbl_saved_total_note.setText("Hours you did not spend typing")

        w = d["weekly"]
        self.day_bars.set_days(w["days"])
        cells = {
            "words": (f"{w['words']:,}", _pct_change(w["words"], w["prev_words"])),
            "transcripts": (f"{w['transcripts']:,}", _pct_change(w["transcripts"], w["prev_transcripts"])),
            "p50": (f"{w['p50']:,} ms" if w["p50"] else "-",
                    _pct_change(w["p50"], w["prev_p50"]) if w["p50"] and w["prev_p50"] else ""),
            "top_app": (labels.app(w["top_app"]) or "-", ""),
        }
        for key, (val, delta) in cells.items():
            self.week_cells[key][0].setText(val)
            self.week_cells[key][1].setText(f"{delta} vs last week" if delta else "")

        if d["latency_samples"]:
            self.bar_latency.set_segments([
                ("Speech to text", d["stt_share"], "cyan"),
                ("AI polish", d["polish_share"], "ink_dim"),
            ])
            slow = "speech to text" if d["stt_share"] >= d["polish_share"] else "AI polish"
            self.lbl_lat_detail.setText(
                f"Median {d['stt_p50']:,} ms transcribing and {d['polish_p50']:,} ms polishing. "
                f"The slow half is {slow}. Both run after you stop talking, so they add up.")
        else:
            self.lbl_lat_detail.setText("No latency samples recorded yet.")

        self._render_radar(d.get("radar") or [])
        self.bars_mode.set_rows([(labels.mode(k), n) for k, n in d["by_mode"]])
        self.bars_app.set_rows([(labels.app(k), n) for k, n in d["by_app"]])
        self.heatmap.set_data(d["by_day"])

        drift = d.get("latency_drift") or []
        if drift:
            top = drift[0]
            self.host.toast(
                f"Latency drift: {top['polish_model']} in the {top['lens']} lens is "
                f"{top['pct_over']:.0f}% slower than its 7-day baseline.", "warn")

    def _render_radar(self, rows):
        while self.radar_grid.count():
            item = self.radar_grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        if not rows:
            lbl = QLabel("No dictations with latency in the last 7 days.")
            lbl.setObjectName("Helper")
            self.radar_grid.addWidget(lbl, 0, 0)
            return
        for col, head in enumerate(("PAIRING", "LATEST P50", "BASELINE", "SAMPLES", "STATUS")):
            h = QLabel(head)
            h.setObjectName("KpiLabel")
            self.radar_grid.addWidget(h, 0, col, Qt.AlignmentFlag.AlignLeft if col == 0 else Qt.AlignmentFlag.AlignRight)
        rows = [r for r in rows if r["samples"] >= 3] or rows[:1]  # one-off pairings are noise
        for r, row in enumerate(rows[:8], start=1):
            stt = row["stt_model"].replace("whisper-large-", "")
            name = QLabel(f"{row['polish_model']}  ·  {row['lens']} lens  ·  {stt}")
            name.setObjectName("Body")
            name.setToolTip(f"STT: {row['stt_model']}\nPolish: {row['polish_model']}\nLatest day: {row['day']}")
            self.radar_grid.addWidget(name, r, 0)
            for col, text in ((1, f"{row['latest_p50']:,} ms"),
                              (2, f"{row['baseline_p50']:,} ms" if row["baseline_p50"] else "building"),
                              (3, f"{row['samples']}")):
                lbl = QLabel(text)
                lbl.setObjectName("Num")
                self.radar_grid.addWidget(lbl, r, col, Qt.AlignmentFlag.AlignRight)
            status = badge("Drifting", "Gold") if row["drifting"] else (
                badge("Stable", "Success") if row["baseline_p50"] else badge("Learning"))
            self.radar_grid.addWidget(status, r, 4, Qt.AlignmentFlag.AlignRight)
        self.radar_grid.setColumnStretch(0, 1)
