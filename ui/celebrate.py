"""
Celebrations: confetti, a banner and "+XP" floaters that burst out of the dictation pill.

One click-through, never-focused overlay window sits just above where the HUD pill lives
(bottom-centre of the primary screen, or top-centre when hud.position says so), so the reward
lands where the eye already is after a dictation and typing into the agent is never touched.
Events play one after another from a queue. Calm mode keeps the banner and drops the motion;
a game in the foreground holds big events for a minute and drops "+XP" floaters.
"""
import math
import os
import random

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QElapsedTimer
from PySide6.QtGui import QColor, QPainter, QFont, QFontMetrics, QPainterPath, QPen
from PySide6.QtWidgets import QWidget, QApplication

from . import motion
from .icons import glyph_icon
from .styles import T, FONT_DISPLAY, FONT_SANS, FONT_MONO

W, H = 1040, 760          # overlay size; the pill anchor sits at the bottom middle
CONFETTI = ("crimson", "cyan", "gold", "success", "white", "gold")
DURATION_MS = {0: 1300, 1: 2400, 2: 3200, 3: 4200}
PARTICLES = {0: 0, 1: 46, 2: 110, 3: 220}
SOUND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sounds", "success.wav")


def _ease_out_back(t: float) -> float:
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _gaming() -> bool:
    try:
        import context_detector
        return bool(context_detector.is_game_foreground())
    except Exception:
        return False


class CelebrationOverlay(QWidget):
    def __init__(self, top: bool = False):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.top = top
        self.resize(W, H)
        self.ev = None
        self.parts = []
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.on_done = None

    # ----- geometry: pill anchor in widget coordinates
    def _place(self):
        scr = QApplication.primaryScreen()
        area = scr.availableGeometry()
        x = area.x() + (area.width() - W) // 2
        y = area.y() + 4 if self.top else area.y() + area.height() - H - 36
        self.move(x, y)

    def anchor(self) -> QPointF:
        return QPointF(W / 2, 30 if self.top else H - 22)

    # ----- play
    def play(self, ev: dict, on_done=None):
        self.ev, self.on_done = ev, on_done
        size = int(ev.get("size", 1))
        self.parts = []
        if motion.enabled():
            a = self.anchor()
            down = 1 if self.top else -1
            for i in range(PARTICLES.get(size, 0)):
                # two cannons either side of the pill plus a centre fountain on the big ones
                side = (-1, 1, 0)[i % (3 if size >= 3 else 2)]
                ang = math.radians(random.uniform(55, 88)) if side else math.radians(random.uniform(70, 110))
                spd = random.uniform(7.5, 15.5) * (1.15 if size >= 3 else 1.0)
                vx = math.cos(ang) * spd * (side or random.choice((-1, 1)) * 0.35)
                vy = math.sin(ang) * spd * down
                self.parts.append({
                    "x": a.x() + side * 70 + random.uniform(-10, 10), "y": a.y(), "vx": vx, "vy": vy,
                    "rot": random.uniform(0, 360), "vr": random.uniform(-14, 14),
                    "w": random.uniform(5, 10), "h": random.uniform(8, 15),
                    "shape": random.choice("rrrc"), "c": QColor(T[random.choice(CONFETTI)]),
                    "wob": random.uniform(0, 6.28),
                })
        self.dur = DURATION_MS.get(size, 2400) if motion.enabled() else 2600
        self._place()
        self.show()
        self.raise_()
        self.clock.start()
        self.timer.start()

    def _tick(self):
        g = 0.32 * (-1 if self.top else 1)
        for p in self.parts:
            p["vy"] += g
            p["vx"] *= 0.985
            p["vy"] *= 0.985
            p["x"] += p["vx"] + math.sin(p["wob"]) * 0.6
            p["y"] += p["vy"]
            p["rot"] += p["vr"]
            p["wob"] += 0.12
        if self.clock.elapsed() >= self.dur:
            self.timer.stop()
            self.hide()
            cb, self.on_done = self.on_done, None
            if cb:
                cb()
            return
        self.update()

    # ----- paint
    def paintEvent(self, _e):
        if not self.ev:
            return
        t = self.clock.elapsed() / self.dur
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fade = 1.0 if t < 0.75 else max(0.0, (1 - t) / 0.25)
        for q in self.parts:
            p.save()
            p.setOpacity(fade)
            p.translate(q["x"], q["y"])
            p.rotate(q["rot"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(q["c"])
            # paper flip: width breathes with the wobble so pieces look like they tumble
            w = q["w"] * (0.35 + 0.65 * abs(math.cos(q["wob"])))
            if q["shape"] == "c":
                p.drawEllipse(QRectF(-w / 2, -w / 2, w, w))
            else:
                p.drawRoundedRect(QRectF(-w / 2, -q["h"] / 2, w, q["h"]), 1.5, 1.5)
            p.restore()
        if self.ev.get("kind") == "xp":
            self._paint_xp(p, t)
        else:
            self._paint_banner(p, t, fade)
        p.end()

    def _paint_xp(self, p: QPainter, t: float):
        a = self.anchor()
        rise = 70 * (1 - (1 - t) ** 3)
        y = a.y() + (rise + 24 if self.top else -rise - 24)
        p.setOpacity(min(1.0, t * 6) * (1.0 if t < 0.6 else max(0.0, (1 - t) / 0.4)))
        deep = bool(self.ev.get("body"))
        f = QFont(FONT_MONO, 15 if deep else 13)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        text = self.ev["title"] + (f"  ·  {self.ev['body']}" if deep else "")
        r = p.fontMetrics().boundingRect(text)
        box = QRectF(a.x() - r.width() / 2 - 12, y - 16, r.width() + 24, 32)
        p.setPen(Qt.PenStyle.NoPen)
        bg = QColor(T["obsidian_900"])
        bg.setAlphaF(0.88)
        p.setBrush(bg)
        p.drawRoundedRect(box, 16, 16)
        p.setPen(QColor(T["gold"] if deep else T["cyan"]))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_banner(self, p: QPainter, t: float, fade: float):
        ev = self.ev
        a = self.anchor()
        big = ev.get("size", 1) >= 3
        bh = 128 if big else 96
        # hug the text: icon + gap + widest line + padding, clamped so long bodies wrap
        ft = QFont(FONT_DISPLAY, 30 if big else 22)
        ft.setWeight(QFont.Weight.Bold)
        fb = QFont(FONT_SANS, 12 if big else 11)
        text_w = max(QFontMetrics(ft).horizontalAdvance(ev.get("title", "").upper()),
                     QFontMetrics(fb).horizontalAdvance(ev.get("body", "")))
        bw = max(300, min(600, 24 + (30 if big else 24) + 16 + text_w + 32))
        k = _ease_out_back(min(1.0, t / 0.16)) if motion.enabled() else 1.0
        cx = a.x()
        cy = (a.y() + 140 + bh / 2) if self.top else (a.y() - 150 - bh / 2)
        p.save()
        p.setOpacity(min(1.0, t * 8) * fade)
        p.translate(cx, cy + (1 - k) * (-30 if self.top else 30))
        p.scale(0.86 + 0.14 * k, 0.86 + 0.14 * k)
        rect = QRectF(-bw / 2, -bh / 2, bw, bh)
        accent = QColor(T["gold"] if ev.get("kind") in ("level", "welcome", "sweep", "goal") else T["cyan"])
        # glow: a few widening translucent strokes
        for i, alpha in enumerate((0.20, 0.12, 0.06)):
            c = QColor(accent)
            c.setAlphaF(alpha)
            p.setPen(QPen(c, 6 + i * 6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, 18, 18)
        bg = QColor(T["obsidian_850"])
        bg.setAlphaF(0.96)
        p.setBrush(bg)
        p.setPen(QPen(accent, 1.5))
        p.drawRoundedRect(rect, 18, 18)
        # shimmer sweep across the card on the way in
        if motion.enabled() and t < 0.5:
            sx = -bw / 2 + (bw + 160) * (t / 0.5) - 80
            path = QPainterPath()
            path.addRoundedRect(rect, 18, 18)
            p.setClipPath(path)
            sh = QColor(T["white"])
            sh.setAlphaF(0.07)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(sh)
            p.drawPolygon([QPointF(sx, -bh / 2), QPointF(sx + 50, -bh / 2), QPointF(sx + 10, bh / 2), QPointF(sx - 40, bh / 2)])
            p.setClipping(False)
        glyph = ev.get("glyph") or {"level": "star_fill", "welcome": "star_fill", "sweep": "check", "goal": "flash",
                                    "quest": "check", "record": "volume"}.get(ev.get("kind"), "star")
        icon = glyph_icon(glyph, 30 if big else 24, accent.name())
        isz = 30 if big else 24
        p.drawPixmap(int(-bw / 2 + 24), int(-isz / 2), icon.pixmap(isz, isz))
        tx = -bw / 2 + 24 + isz + 16
        f = QFont(FONT_DISPLAY, 30 if big else 22)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.setPen(QColor(T["ink"]))
        title_h = 40 if big else 30
        p.drawText(QRectF(tx, -bh / 2 + (20 if big else 14), bw - (tx + bw / 2) - 20, title_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ev.get("title", "").upper())
        f2 = QFont(FONT_SANS, 12 if big else 11)
        p.setFont(f2)
        p.setPen(QColor(T["ink_muted"]))
        p.drawText(QRectF(tx, -bh / 2 + (20 if big else 14) + title_h + 2, bw - (tx + bw / 2) - 20, 40),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, ev.get("body", ""))
        p.restore()


class Celebrator:
    """Plays goal events in order on one overlay. Lives on the GUI thread (the dashboard owns it)."""

    def __init__(self, cfg_fn):
        self.cfg_fn = cfg_fn  # -> live config dict
        self.queue = []
        self.overlay = None
        self.busy = False

    def _g(self) -> dict:
        return (self.cfg_fn() or {}).get("goals") or {}

    def play(self, evs: list):
        g = self._g()
        if not g.get("enabled", True) or not g.get("celebrations", True):
            return
        if not g.get("xp_popups", True):
            evs = [e for e in evs if e.get("kind") != "xp"]
        # a burst of big news: keep the best 4 plus the xp floater so it never turns into a slideshow
        big = [e for e in evs if e.get("kind") != "xp"][:4]
        self.queue += big + [e for e in evs if e.get("kind") == "xp"][:1]
        if not self.busy:
            self._next()

    def _next(self):
        if not self.queue:
            self.busy = False
            return
        if _gaming():
            self.queue = [e for e in self.queue if e.get("kind") != "xp"]
            if self.queue:
                self.busy = True
                QTimer.singleShot(60_000, self._next)
            else:
                self.busy = False
            return
        self.busy = True
        ev = self.queue.pop(0)
        top = ((self.cfg_fn() or {}).get("hud") or {}).get("position") == "top-center"
        if self.overlay is None or self.overlay.top != top:
            self.overlay = CelebrationOverlay(top=top)
        if ev.get("size", 0) >= 2 and self._g().get("sound", True):
            self._chime()
        self.overlay.play(ev, on_done=self._next)

    @staticmethod
    def _chime():
        try:
            import winsound
            winsound.PlaySound(SOUND, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass
