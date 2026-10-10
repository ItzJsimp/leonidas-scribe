"""
Goals: the rank ladder, today's goal and quests, prompt depth coaching and the achievement wall.

Every number is replayed from history.jsonl by goals.compute (via host.goals_snapshot on a
worker thread); this view only paints it. The one authored motion moment is the XP bar and
the ladder filling up when the page opens.
"""
from PySide6.QtCore import Qt, QRectF, QPointF, QVariantAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QPainterPath
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame, QComboBox,
                               QCheckBox, QApplication, QSizePolicy)

import goals as G
from .. import motion
from ..icons import glyph_icon
from ..styles import T, FONT_DISPLAY, FONT_MONO, FONT_SANS
from ..widgets import page_header, section_label, button, make_card, run_async

GOAL_CHOICES = [(0, "Auto: grows with you"), (500, "500 words"), (1000, "1,000 words"), (2000, "2,000 words"),
                (3000, "3,000 words"), (5000, "5,000 words"), (10000, "10,000 words")]


def _label(text: str, obj: str, wrap: bool = False, tip: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(obj)
    lbl.setWordWrap(wrap)
    if tip:
        lbl.setToolTip(tip)
    return lbl


def _repolish(w, **props):
    for k, v in props.items():
        w.setProperty(k, v)
    w.style().unpolish(w)
    w.style().polish(w)


class _Animated(QWidget):
    """A painted widget whose value eases from its old value to the new one (instant under Calm)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.shown = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutExpo)
        self._anim.valueChanged.connect(self._step)

    def animate_to(self, v: float, ms: int = 1100):
        self._anim.stop()
        if not motion.enabled():
            self._step(v)
            return
        self._anim.setDuration(ms)
        self._anim.setStartValue(float(self.shown))
        self._anim.setEndValue(float(v))
        self._anim.start()

    def _step(self, v):
        self.shown = float(v)
        self.update()


class Shield(_Animated):
    """The level plate: a pointed shield with the level number, gold rim, ticking up on open."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(108, 124)
        self.setToolTip("Your level. Every word you dictate earns XP; prompts to AI agents earn 1.5x.")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width() - 8, self.height() - 8
        path = QPainterPath()
        path.moveTo(4 + w / 2, 4)
        path.lineTo(4 + w, 4 + h * 0.16)
        path.lineTo(4 + w, 4 + h * 0.52)
        path.quadTo(4 + w, 4 + h * 0.86, 4 + w / 2, 4 + h)
        path.quadTo(4, 4 + h * 0.86, 4, 4 + h * 0.52)
        path.lineTo(4, 4 + h * 0.16)
        path.closeSubpath()
        p.setPen(QPen(QColor(T["gold"]), 2))
        p.setBrush(QColor(T["obsidian_900"]))
        p.drawPath(path)
        inner = QColor(T["gold"])
        inner.setAlphaF(0.10)
        p.setBrush(inner)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)
        f = QFont(FONT_MONO, 10)
        p.setFont(f)
        p.setPen(QColor(T["ink_muted"]))
        p.drawText(QRectF(0, 22, self.width(), 16), Qt.AlignmentFlag.AlignHCenter, "LEVEL")
        f = QFont(FONT_DISPLAY, 40)
        f.setWeight(QFont.Weight.Black)
        p.setFont(f)
        p.setPen(QColor(T["gold"]))
        p.drawText(QRectF(0, 36, self.width(), 56), Qt.AlignmentFlag.AlignCenter, str(int(round(self.shown))))
        p.end()


class Meter(_Animated):
    """Thin progress bar with an optional goal tick. value is a 0..1 fraction (can exceed 1)."""

    def __init__(self, color: str = "gold", height: int = 10, parent=None):
        super().__init__(parent)
        self.color = color
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        rad = self.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T["obsidian_700"]))
        p.drawRoundedRect(r, rad, rad)
        frac = max(0.0, min(1.0, self.shown))
        if frac > 0:
            done = self.shown >= 1.0
            c = QColor(T["success" if done and self.color != "gold" else self.color])
            p.setBrush(c)
            p.drawRoundedRect(QRectF(0, 0, max(self.height(), self.width() * frac), self.height()), rad, rad)
        p.end()


class RankLadder(_Animated):
    """Every rank as a node on one track. Passed ranks are gold, the next one is the target."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = 1
        self.setMinimumHeight(64)
        self.setToolTip("The rank ladder. Ranks unlock at the levels shown; Leonidas is the top.")
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._beat)

    def set_level(self, level: int):
        self.level = level
        self.animate_to(level, 1400)
        if motion.enabled():
            self._timer.start()

    def _beat(self):
        self._pulse = (self._pulse + 0.04) % 1.0
        self.update()

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def showEvent(self, e):
        if motion.enabled():
            self._timer.start()
        super().showEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ranks = G.RANKS
        n = len(ranks)
        pad = 34
        span = self.width() - 2 * pad
        y = 16
        xs = [pad + span * i / (n - 1) for i in range(n)]
        # position along the track for a level, piecewise between rank nodes
        def x_for(level: float) -> float:
            for i in range(n - 1):
                a, b = ranks[i][0], ranks[i + 1][0]
                if level <= b:
                    return xs[i] + (xs[i + 1] - xs[i]) * max(0.0, (level - a) / (b - a))
            return xs[-1]
        p.setPen(QPen(QColor(T["obsidian_700"]), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(xs[0], y), QPointF(xs[-1], y))
        p.setPen(QPen(QColor(T["gold"]), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(xs[0], y), QPointF(x_for(self.shown), y))
        nxt = G.next_rank(self.level)
        f = QFont(FONT_SANS, 8)
        p.setFont(f)
        fm = p.fontMetrics()
        for i, (lv, name) in enumerate(ranks):
            reached = self.shown >= lv
            target = nxt and lv == nxt[0]
            p.setPen(Qt.PenStyle.NoPen)
            if target:
                halo = QColor(T["cyan"])
                halo.setAlphaF(0.35 * (1 - self._pulse))
                p.setBrush(halo)
                rr = 7 + 9 * self._pulse
                p.drawEllipse(QPointF(xs[i], y), rr, rr)
            p.setBrush(QColor(T["gold"] if reached else (T["cyan"] if target else T["obsidian_600"])))
            p.drawEllipse(QPointF(xs[i], y), 6 if (reached or target) else 4.5, 6 if (reached or target) else 4.5)
            # labels alternate rows so twelve names fit on narrow windows
            row = 0 if i % 2 == 0 else 1
            p.setPen(QColor(T["ink"] if target else (T["gold"] if reached else T["ink_dim"])))
            label = name if self.width() > 760 or reached or target or i in (0, n - 1) else ""
            if label:
                tw = fm.horizontalAdvance(label)
                tx = min(max(0, xs[i] - tw / 2), self.width() - tw)
                p.drawText(QPointF(tx, y + 22 + row * 14), label)
        p.end()


class Pips(QWidget):
    """Deep prompts today: one pip per prompt up to the goal, filled as they land."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.have, self.need = 0, 3
        self.setFixedHeight(22)
        self.setMinimumWidth(120)

    def set(self, have: int, need: int):
        self.have, self.need = have, need
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i in range(self.need):
            r = QRectF(i * 30, 3, 24, 16)
            full = i < self.have
            p.setPen(QPen(QColor(T["cyan"] if full else T["obsidian_600"]), 1.5))
            p.setBrush(QColor(T["cyan"]) if full else Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 5, 5)
        if self.have > self.need:
            p.setPen(QColor(T["cyan"]))
            p.setFont(QFont(FONT_MONO, 10))
            p.drawText(QPointF(self.need * 30 + 4, 16), f"+{self.have - self.need}")
        p.end()


class QuestRow(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("QuestRow")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(20, 20)
        h.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.title = _label("", "CardTitle")
        self.desc = _label("", "Helper", wrap=True)
        self.bar = Meter("cyan", 6)
        col.addWidget(self.title)
        col.addWidget(self.desc)
        col.addWidget(self.bar)
        h.addLayout(col, 1)
        right = QVBoxLayout()
        right.setSpacing(2)
        self.count = _label("", "Num")
        self.xp = _label("", "GoalXp")
        right.addWidget(self.count, 0, Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.xp, 0, Qt.AlignmentFlag.AlignRight)
        right.addStretch(1)
        h.addLayout(right)

    def set(self, q: dict):
        self.title.setText(q["title"])
        self.desc.setText(q["desc"])
        self.count.setText(f"{q['have']}/{q['need']}")
        self.xp.setText("Done" if q["done"] else f"+{q['xp']} XP")
        self.icon.setPixmap(glyph_icon("check" if q["done"] else "flash", 18,
                                       T["success"] if q["done"] else T["ink_dim"]).pixmap(18, 18))
        self.bar.animate_to(q["have"] / max(1, q["need"]))
        self.setToolTip("Completed today" if q["done"] else "Quests reset at midnight. Each one is worth "
                        f"{q['xp']} XP; finish all three for a {G.XP_SWEEP} XP bonus.")
        _repolish(self, done="true" if q["done"] else "false")


def _fmt(a: dict, n) -> str:
    return f"{n / 60:.0f}h" if a.get("key") == "saved_min" else f"{int(n):,}"


class AchTile(QFrame):
    def __init__(self, a: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("AchTile")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        ic = QLabel()
        on = a["unlocked"]
        ic.setPixmap(glyph_icon(a["glyph"], 18, T["gold"] if on else T["ink_dim"]).pixmap(18, 18))
        top.addWidget(ic)
        top.addWidget(_label(a["name"], "CardTitle"), 1)
        v.addLayout(top)
        v.addWidget(_label(a["how"], "Helper", wrap=True))
        v.addStretch(1)
        if on:
            v.addWidget(_label("UNLOCKED", "BadgeGold"), 0, Qt.AlignmentFlag.AlignLeft)
        else:
            bar = Meter("gold", 4)
            bar.animate_to(min(1.0, a["have"] / max(1, a["need"])))
            v.addWidget(bar)
            v.addWidget(_label(f"{_fmt(a, a['have'])} / {_fmt(a, a['need'])}", "Meta"))
        self.setToolTip(f"{a['name']}: {a['how']}" + ("" if on else f" ({_fmt(a, a['have'])} of {_fmt(a, a['need'])})"))
        _repolish(self, unlocked="true" if on else "false")


class GoalsView(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.snap = None
        scroll, content = host.scroll_host(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        v = QVBoxLayout(content)
        v.setContentsMargins(28, 24, 24, 28)
        v.setSpacing(12)

        self.btn_copy = button("Copy level card", "GhostBtn", "copy",
                               "Copy an image of your rank plate to paste in Discord")
        self.btn_copy.clicked.connect(self._copy_card)
        v.addLayout(page_header("Goals", "Talk to your agents more and in longer prompts: every spoken word is XP, "
                                         "and deep prompts earn the most.", (self.btn_copy,)))

        # rank plate
        self.plate, pb = make_card()
        row = QHBoxLayout()
        row.setSpacing(20)
        self.shield = Shield()
        row.addWidget(self.shield, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        self.lbl_rank = _label("", "RankTitle")
        self.lbl_next = _label("", "Helper", wrap=True)
        self.xpbar = Meter("gold", 12)
        self.xpbar.setToolTip("Progress to the next level")
        self.lbl_xp = _label("", "Meta")
        col.addWidget(self.lbl_rank)
        col.addWidget(self.lbl_next)
        col.addSpacing(4)
        col.addWidget(self.xpbar)
        col.addWidget(self.lbl_xp)
        col.addStretch(1)
        row.addLayout(col, 1)
        pb.addLayout(row)
        self.ladder = RankLadder()
        pb.addWidget(self.ladder)
        v.addWidget(self.plate)

        # today
        v.addSpacing(8)
        v.addWidget(section_label("Today"))
        today = QHBoxLayout()
        today.setSpacing(12)
        day_card, db = make_card("Daily goal")
        self.lbl_words = _label("", "GoalNum")
        self.words_bar = Meter("cyan", 10)
        self.lbl_words_note = _label("", "Helper", wrap=True)
        db.addWidget(self.lbl_words)
        db.addWidget(self.words_bar)
        db.addWidget(self.lbl_words_note)
        db.addSpacing(6)
        db.addWidget(_label("Deep prompts", "FieldLabel",
                            tip=f"Prompts of {G.DEEP_WORDS}+ words to an AI agent. Say the why, the constraints "
                                "and what done looks like."))
        self.pips = Pips()
        db.addWidget(self.pips)
        self.lbl_streak = _label("", "CardTitle", wrap=True,
                                 tip="Days in a row with at least one dictation. Today counts once you dictate.")
        db.addWidget(self.lbl_streak)
        db.addStretch(1)
        today.addWidget(day_card, 2)
        q_card, qb = make_card("Daily quests", "New quests every midnight. Finish all three for a bonus.")
        self.quests = [QuestRow() for _ in range(3)]
        for q in self.quests:
            qb.addWidget(q)
        today.addWidget(q_card, 3)
        v.addLayout(today)

        # coaching
        v.addSpacing(8)
        v.addWidget(section_label("Your prompting"))
        self.coach_card, cb = make_card()
        self.lbl_coach = _label("", "Reading", wrap=True)
        self.lbl_coach.setTextFormat(Qt.TextFormat.RichText)
        cb.addWidget(self.lbl_coach)
        v.addWidget(self.coach_card)

        # achievements
        v.addSpacing(8)
        self.lbl_ach = section_label("Achievements")
        v.addWidget(self.lbl_ach)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        v.addLayout(self.grid)

        # tune
        v.addSpacing(8)
        v.addWidget(section_label("Tune"))
        tune, tb = make_card()
        trow = QHBoxLayout()
        trow.setSpacing(16)
        self.cb_goal = QComboBox()
        for n, text in GOAL_CHOICES:
            self.cb_goal.addItem(text, n)
        self.cb_goal.setToolTip("Daily word goal. Auto aims 10% past your last 7 active days (at least 500).")
        self.cb_goal.activated.connect(lambda _i: self._save("daily_words", self.cb_goal.currentData()))
        trow.addWidget(_label("Daily goal", "FieldLabel"))
        trow.addWidget(self.cb_goal)
        self.chk_party = QCheckBox("Celebrations")
        self.chk_party.setToolTip("Confetti and a banner over the dictation pill when you level up or hit a goal")
        self.chk_party.toggled.connect(lambda on: self._save("celebrations", on))
        self.chk_xp = QCheckBox("+XP after each dictation")
        self.chk_xp.setToolTip("A small +XP pop above the pill after every dictation")
        self.chk_xp.toggled.connect(lambda on: self._save("xp_popups", on))
        self.chk_sound = QCheckBox("Chime")
        self.chk_sound.setToolTip("Play a chime on big moments")
        self.chk_sound.toggled.connect(lambda on: self._save("sound", on))
        for w in (self.chk_party, self.chk_xp, self.chk_sound):
            trow.addWidget(w)
        trow.addStretch(1)
        self.btn_preview = button("Preview", "GhostBtn", "flash", "Play a level-up celebration now")
        self.btn_preview.clicked.connect(self._preview)
        trow.addWidget(self.btn_preview)
        tb.addLayout(trow)
        v.addWidget(tune)
        v.addStretch(1)
        self._tiles = []

    # ----- data
    def _g(self) -> dict:
        return (getattr(self.host, "config", None) or {}).get("goals") or {}

    def refresh(self):
        run_async(self.host.goals_snapshot, self._apply)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def _apply(self, s):
        if isinstance(s, Exception):
            self.host.toast(f"Could not load Goals: {s}", "error")
            return
        self.snap = s
        self.shield.animate_to(s["level"], 900)
        self.lbl_rank.setText(s["rank"].upper())
        to_next = s["level_ceil"] - s["xp"]
        nr = s["next_rank"]
        self.lbl_next.setText(f"{to_next:,} XP to level {s['level'] + 1}"
                              + (f". {nr['name']} unlocks at level {nr['level']}." if nr else ". Top rank reached."))
        self.xpbar.animate_to(s["level_frac"])
        self.lbl_xp.setText(f"{s['xp']:,} XP total  ·  +{s['today']['xp']:,} today")
        self.ladder.set_level(s["level"])

        t = s["today"]
        self.lbl_words.setText(f"{t['words']:,} / {t['goal']:,} words")
        self.words_bar.animate_to(t["words"] / max(1, t["goal"]))
        left = t["goal"] - t["words"]
        auto = not self._g().get("daily_words")
        self.lbl_words_note.setText(("Goal hit. Everything past here is bonus XP." if left <= 0 else
                                     f"{left:,} words to go, about {max(1, round(left / max(60, s['voice_wpm'] or 150)))} "
                                     "minutes of talking.") + (" Auto goal: grows with you." if auto else ""))
        self.pips.set(t["deep"], t["deep_goal"])
        self.pips.setToolTip(f"{t['deep']} deep prompts today (goal {t['deep_goal']})")
        streak = s["streak"]
        self.lbl_streak.setText(f"{streak}-day streak" + (f", best {s['best_streak']}" if s["best_streak"] > streak else "")
                                + (". Dictate today to keep it alive." if not t["dictations"] and streak else ""))
        for row, q in zip(self.quests, s["quests"]):
            row.set(q)

        d, prev = s["depth_7d"], s["depth_prev"]
        trend = "" if not prev else (f", up from {prev}" if d > prev else f", down from {prev}" if d < prev else ", flat")
        tip = ("Push past 100: say what you are trying to do, why, the constraints, and what done looks like."
               if d < G.DEEP_WORDS else "That is deep-prompt territory. Keep thinking out loud.")
        speed = (f"You speak at <b>{s['voice_wpm']} wpm</b>, about <b>{s['multiplier']}x</b> your typing. "
                 if s["voice_wpm"] else "")
        self.lbl_coach.setText(f"Your agent prompts average <b>{d} words</b> this week{trend}. {tip}<br>{speed}"
                               f"Longest prompt so far: <b>{s['metrics']['longest_prompt']:,} words</b>.")

        for w in self._tiles:
            w.setParent(None)
            w.deleteLater()
        self._tiles = []
        # locked first, closest to done on top, so the next one to chase is the first thing you see
        ordered = sorted(s["achievements"], key=lambda a: (a["unlocked"], -(a["have"] / max(1, a["need"]))))
        cols = 4 if self.width() > 900 else 3
        for i, a in enumerate(ordered):
            tile = AchTile(a)
            self._tiles.append(tile)
            self.grid.addWidget(tile, i // cols, i % cols)
        got = sum(a["unlocked"] for a in s["achievements"])
        self.lbl_ach.setText(self.lbl_ach.text().split("ACHIEVEMENTS")[0] + f"ACHIEVEMENTS  {got}/{len(s['achievements'])}")

        g = self._g()
        for w, key in ((self.chk_party, "celebrations"), (self.chk_xp, "xp_popups"), (self.chk_sound, "sound")):
            w.blockSignals(True)
            w.setChecked(bool(g.get(key, True)))
            w.blockSignals(False)
        i = self.cb_goal.findData(int(g.get("daily_words", 0) or 0))
        self.cb_goal.setCurrentIndex(max(0, i))

    # ----- actions
    def _save(self, key, value):
        cfg = getattr(self.host, "config", None)
        if cfg is not None:
            cfg.setdefault("goals", {})[key] = value
        ctl = getattr(self.host, "controller", None)
        if ctl is not None:
            def job():
                fresh = ctl.load_config()
                fresh.setdefault("goals", {})[key] = value
                return ctl.save_config(fresh)
            run_async(job, lambda _r: None)
        if key == "daily_words":
            self.refresh()

    def _preview(self):
        s = self.snap or {"level": 1, "rank": G.rank_for(1)}
        self.host.celebrator.play([{"kind": "level", "size": 3, "title": f"Level {s['level']}",
                                    "body": f"Rank: {s['rank']}. This is what a level-up looks like."},
                                   {"kind": "xp", "size": 0, "title": "+184 XP", "body": "Deep prompt"}])

    def _copy_card(self):
        QApplication.clipboard().setPixmap(self.plate.grab())
        self.host.toast("Level card copied. Paste it anywhere.", "success")
