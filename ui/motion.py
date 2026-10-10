"""
Motion, glow and the ambient canvas (app-building-standard.md section 6).

Budgets: 150ms micro, 240ms entrances, bursts up to 900ms, ambient 8-13s loops.
Everything decorative asks `enabled()` first, which is off only when Calm (or the
older "reduced motion" key) is set in Settings. Windows' own "Animation effects"
switch is not followed. With motion off, state still changes, instantly.

Qt has no box-shadow, so the standard's glows are QGraphicsDropShadowEffect with
zero offset: cyan on whatever has keyboard focus, crimson on the hovered primary
button. One glow per region holds because only one widget has focus and the
pointer hovers one button.
"""
import math
import time

import numpy as np
from PySide6.QtCore import (
    Qt, QObject, QEvent, QTimer, QPropertyAnimation, QVariantAnimation, QEasingCurve, QRectF
)
from PySide6.QtGui import QColor, QPainter, QImage, QCursor
from PySide6.QtWidgets import (
    QWidget, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QApplication,
    QAbstractButton, QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox,
    QFrame, QLabel, QPushButton,
)

from .styles import T

_calm = False

# Island spring (direction C): per 16 ms frame, ~4% overshoot, settles in ~260 ms. Every Island
# surface that morphs uses these so the overlays move as one system.
SPRING_K, SPRING_D = 0.22, 0.56


def set_calm(on: bool) -> None:
    global _calm
    _calm = bool(on)


def enabled() -> bool:
    # Scribe's own Calm switch is the only control. Windows' "Animation effects"
    # switch is deliberately ignored: the user keeps it off so Windows feels snappy and
    # still wants Scribe animated (decided 2026-09-25).
    return not _calm


# ---------------------------------------------------------------------------
# Glow
# ---------------------------------------------------------------------------
def glow(widget: QWidget, token: str, radius: int = 22, alpha: int = 110) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    col = QColor(T[token])
    col.setAlpha(alpha)
    effect.setColor(col)
    effect.setBlurRadius(radius)
    effect.setOffset(0, 0)
    widget.setGraphicsEffect(effect)


def clear_glow(widget: QWidget) -> None:
    if isinstance(widget.graphicsEffect(), QGraphicsDropShadowEffect):
        widget.setGraphicsEffect(None)


_GLOWABLE = (QAbstractButton, QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox, QFrame)


class FocusGlow(QObject):
    """Cyan glow follows keyboard focus. Buttons take focus from Tab only (see
    ui.widgets.tab_focus_only), so a mouse click never leaves a ring behind."""

    def __init__(self, app: QApplication):
        super().__init__(app)
        app.focusChanged.connect(self._moved)

    @staticmethod
    def _moved(old, new):
        if old is not None and isinstance(old, _GLOWABLE):
            clear_glow(old)
        if new is not None and isinstance(new, _GLOWABLE) and new.graphicsEffect() is None:
            if isinstance(new, QFrame) and new.objectName() != "Card":
                return
            glow(new, "cyan", 20, 90)


def _rgba(token: str, alpha: float) -> QColor:
    col = QColor(T[token])
    col.setAlphaF(max(0.0, min(1.0, alpha)))
    return col


class Hover(QObject):
    """Every hover in the app, installed once on the QApplication.

    - Primary button: crimson glow (`hover:shadow-glow-crimson`; QSS cannot draw a shadow).
    - Cards, nav items, ghost/secondary buttons, segments and badges: a 150ms eased
      highlight. QSS has no transitions, so styles.py leaves their :hover
      background to this class, which paints it at the animated strength t:
      "under" before the control paints (its text stays on top), "over" after it
      (a card's content is child widgets, painted later still). Badges follow
      their card's t. With motion off, t jumps: hover is instant, never absent.
      No graphics effect is used: it would re-render the text without ClearType.
    """

    MS = 150
    LIFT = 3  # px a lift card grows on each side at full hover: a ~1% scale-up
    # objectName: (when, fill token, fill alpha, edge token, edge alpha, radius) at t=1
    STYLE = {
        "Card":       ("over", "white", 0.025, "cyan", 0.30, 12),
        "NavItem":    ("under", "obsidian_850", 1.0, None, 0, 0),
        "SubNavItem": ("under", "obsidian_850", 1.0, None, 0, 0),
        "Segment":    ("under", "obsidian_850", 1.0, None, 0, 0),
        "SettingRow": ("under", "obsidian_800", 1.0, None, 0, 8),
        "GhostBtn":   ("under", "obsidian_800", 1.0, "obsidian_700", 1.0, 8),
        "DangerBtn":  ("under", "error", 0.12, None, 0, 8),
        "":           ("over", None, 0, "ink_dim", 0.55, 8),  # secondary QPushButton
    }

    @classmethod
    def _style(cls, w):
        name = w.objectName()
        if name.startswith("Badge") and isinstance(w, QLabel):
            return "badge"
        if name == "" and not isinstance(w, QPushButton):
            return None
        if name == "Card" and not isinstance(w, QFrame):
            return None
        if name == "Card" and w.property("lift"):
            return "lift"
        return cls.STYLE.get(name)

    def _animate(self, w, target: float):
        if not enabled():
            self._set_t(w, target)
            return
        anim = getattr(w, "_hover_anim", None)
        if anim is None:
            anim = QVariantAnimation(w)
            anim.setDuration(self.MS)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(lambda v, w=w: self._set_t(w, v))
            w._hover_anim = anim
        anim.stop()
        anim.setStartValue(float(getattr(w, "_hover_t", 0.0)))
        anim.setEndValue(target)
        anim.start()

    @staticmethod
    def _set_t(w, t):
        w._hover_t = float(t)
        w.update()  # repaints the card's badges too: children sit inside its dirty region

    def _paint_lift(self, w):
        """A lift card draws its own surface (styles.py leaves it bare), inset by
        LIFT at rest, so hover grows it outward sub-pixel smooth. A real scale
        would blur the text; this keeps text and neighbouring cards still."""
        t = getattr(w, "_hover_t", 0.0)
        g = self.LIFT * (1 - t) + 0.5
        rect = QRectF(w.rect()).adjusted(g, g, -g, -g)
        p = QPainter(w)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(T["obsidian_850"]))
        p.setPen(_rgba("white", 0.06))  # the hairline token
        p.drawRoundedRect(rect, 12, 12)
        if t > 0.001:
            p.setBrush(_rgba("white", 0.025 * t))
            p.setPen(_rgba("cyan", 0.30 * t))
            p.drawRoundedRect(rect, 12, 12)
        if w.hasFocus() or w.property("copied"):
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QColor(T["cyan"]))
            p.drawRoundedRect(rect, 12, 12)
        p.end()
        return False

    def _paint(self, w, style, event):
        if style == "lift":
            return self._paint_lift(w)
        if style == "badge":
            card = w.parentWidget()
            while card is not None and card.objectName() != "Card":
                card = card.parentWidget()
            t = getattr(card, "_hover_t", 0.0) if card is not None else 0.0
            style = ("over", None, 0, None, 0.45, w.height() / 2)
            edge = QColor(w.palette().color(w.foregroundRole()))
        else:
            t = getattr(w, "_hover_t", 0.0)
            edge = QColor(T[style[3]]) if style[3] else None
        when, fill, fill_a, _, edge_a, radius = style
        if t <= 0.001:
            return False
        if when == "over":
            w.event(event)  # the widget's own paint first (a direct call skips filters)
        p = QPainter(w)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(w.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        if edge is not None:
            edge.setAlphaF(edge_a * t)
            p.setPen(edge)
        p.setBrush(_rgba(fill, fill_a * t) if fill else Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)
        p.end()
        return when == "over"

    def eventFilter(self, obj, event):
        et = event.type()
        if et not in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.Paint):
            return False
        if not isinstance(obj, QWidget):
            return False
        if isinstance(obj, QAbstractButton) and obj.objectName() == "PrimaryBtn":
            if et == QEvent.Type.Enter and obj.isEnabled():
                glow(obj, "crimson", 26, 120)
            elif et in (QEvent.Type.Leave, QEvent.Type.Hide) and not obj.hasFocus():
                clear_glow(obj)
            return False
        style = self._style(obj)
        if style is None:
            return False
        if et == QEvent.Type.Paint:
            return self._paint(obj, style, event)
        if style != "badge":
            self._animate(obj, 1.0 if et == QEvent.Type.Enter and obj.isEnabled() else 0.0)
        return False


# ---------------------------------------------------------------------------
# Entrances
# ---------------------------------------------------------------------------
def fade_in(widget: QWidget, ms: int = 240) -> None:
    """240ms entrance. The effect is removed on finish: an opacity effect left on
    a page would render the whole subtree offscreen on every repaint."""
    if not enabled() or widget.graphicsEffect() is not None:
        return
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


def fade_to(effect: QGraphicsOpacityEffect, target: float, ms: int = 150) -> None:
    if not enabled():
        effect.setOpacity(target)
        return
    anim = QPropertyAnimation(effect, b"opacity", effect)
    anim.setDuration(ms)
    anim.setEndValue(target)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


# ---------------------------------------------------------------------------
# Ambient canvas and live dots
# ---------------------------------------------------------------------------
class AmbientCanvas(QWidget):
    """The obsidian canvas: a liquid crimson and cyan drift and a faint light that
    follows the pointer. Ticks at 24 fps, only while the window is visible and
    active, and pauses while you scroll or type so it never competes with a
    long transcript list.

    Why not QRadialGradient: a glow this faint spans ~13 levels of 8-bit colour,
    so any gradient Qt draws shows those levels as concentric rings. Instead the
    field is computed in float (numpy) at 1/3 or 1/4 size from separable Gaussian
    blobs, a fixed noise pattern dithers it as it is quantised, and it is
    upscaled nearest-neighbour so the dither survives. No rings, no shimmer."""

    FPS = 24
    # token, loop seconds, centre x/y, drift x/y (fractions of the window),
    # sigma (fraction of the long side), peak strength 0-1. Loops are 8-13s and the
    # drifts wide enough that crimson and cyan cross and mix mid-canvas.
    BLOBS = (
        ("crimson", 11.0, 0.18, 0.12, 0.16, 0.10, 0.22, 0.072),
        ("crimson", 13.0, 0.50, 0.04, 0.22, 0.08, 0.15, 0.038),
        ("cyan",    12.0, 0.84, 0.90, 0.15, 0.11, 0.24, 0.058),
        ("cyan",     8.5, 0.62, 0.58, 0.18, 0.14, 0.13, 0.030),
    )
    CURSOR_PEAK = 0.022   # the pointer light: cyan, small, eases toward the pointer

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # Animation clock: advances only on ticks that actually animate, by the real
        # time since the last one (capped), so pausing on blur or scroll and then
        # resuming continues from the same frame instead of jumping ahead.
        self._t = 0.0
        self._last_tick = None
        self._quiet_until = 0.0
        self._noise = None
        self._frame = None  # (buffer, image): rebuilt per tick, reused by partial repaints
        self._cursor = None  # eased pointer position in widget pixels, or None
        self._bg = np.array(QColor(T["obsidian_950"]).getRgb()[:3], np.float32)
        self._deltas = np.array([QColor(T[b[0]]).getRgb()[:3] for b in self.BLOBS]
                                + [QColor(T["cyan"]).getRgb()[:3]], np.float32) - self._bg
        self._timer = QTimer(self)
        self._timer.setInterval(1000 // self.FPS)
        self._timer.timeout.connect(self._tick)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Wheel, QEvent.Type.KeyPress):
            self._quiet_until = time.monotonic() + 1.2
        return False

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self):
        now = time.monotonic()
        if not (enabled() and now >= self._quiet_until and self.window().isActiveWindow()):
            self._last_tick = None  # paused: the clock stops here
            return
        if self._last_tick is not None:
            self._t += min(now - self._last_tick, 2.0 / self.FPS)
        self._last_tick = now
        p = self.mapFromGlobal(QCursor.pos())
        if self.rect().contains(p):
            target = np.array([p.x(), p.y()], np.float32)
            self._cursor = target if self._cursor is None else self._cursor + (target - self._cursor) * 0.12
        self._frame = None
        self.update()

    def resizeEvent(self, event):
        self._frame = None
        super().resizeEvent(event)

    def field(self, t: float) -> np.ndarray:
        """The dithered frame at time t: (h, w, 3) uint8 at 1/scale() size."""
        out = self.smooth(t)
        if self._noise is None or self._noise.shape != out.shape:
            # Fixed pattern (seeded), so the grain never crawls between frames.
            self._noise = np.random.default_rng(7).random(out.shape, dtype=np.float32)
        out += self._noise  # floor(v + U[0,1)) keeps every local average equal to v
        return out.astype(np.uint8)  # peaks sum well under 1, so no clip is needed

    def smooth(self, t: float) -> np.ndarray:
        """The exact float frame at time t, before quantising."""
        W, H, s = max(1, self.width()), max(1, self.height()), self.scale()
        w, h = -(-W // s), -(-H // s)
        xs = (np.arange(w, dtype=np.float32) + 0.5) * s
        ys = (np.arange(h, dtype=np.float32) + 0.5) * s
        long_side = max(W, H)
        g = np.zeros((h, w, len(self.BLOBS) + 1), np.float32)
        for i, (_, period, cx, cy, ax, ay, sigma, peak) in enumerate(self.BLOBS):
            ph = 2 * math.pi * t / period
            # Lissajous 1:2 path with a breathing radius reads as liquid, not orbiting.
            x0 = W * (cx + ax * math.sin(ph))
            y0 = H * (cy + ay * math.sin(2 * ph + 1.3))
            sd = sigma * long_side * (1 + 0.12 * math.sin(ph + 0.7))
            gx = np.exp(-0.5 * ((xs - x0) / sd) ** 2)
            gy = np.exp(-0.5 * ((ys - y0) / sd) ** 2)
            np.multiply.outer(gy * peak, gx, out=g[:, :, i])
        if self._cursor is not None:
            sd = 0.09 * long_side
            gx = np.exp(-0.5 * ((xs - self._cursor[0]) / sd) ** 2)
            gy = np.exp(-0.5 * ((ys - self._cursor[1]) / sd) ** 2)
            np.multiply.outer(gy * self.CURSOR_PEAK, gx, out=g[:, :, -1])
        out = g @ self._deltas
        out += self._bg
        return out

    def scale(self) -> int:
        """1/3 size normally, 1/4 when maximised on a 1440p screen: keeps a frame under ~6 ms."""
        return 3 if self.width() * self.height() < 2_000_000 else 4

    def paintEvent(self, event):
        # Scrolling and hover repaint small regions many times a second; they
        # reuse the cached frame instead of recomputing the field.
        if self._frame is None:
            t = self._t if enabled() else 0.0
            buf = np.ascontiguousarray(self.field(t))
            h, w = buf.shape[:2]
            self._frame = (buf, QImage(buf.data, w, h, 3 * w, QImage.Format.Format_RGB888))
        img, s = self._frame[1], self.scale()
        painter = QPainter(self)
        # Nearest-neighbour on purpose: smoothing would average the dither away.
        painter.drawImage(QRectF(0, 0, img.width() * s, img.height() * s), img)
        painter.end()


class BreathingDot(QWidget):
    """Live status dot: breathes on a 2.4s loop, holds steady with motion off."""

    def __init__(self, token: str = "success", size: int = 10, parent=None):
        super().__init__(parent)
        self.token = token
        self.setFixedSize(size + 8, size + 8)
        self._size = size
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self.update)

    def set_token(self, token: str):
        self.token = token
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start()

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 2 * math.pi / 2.4) if enabled() else 0.5
        c = QRectF(self.rect()).center()
        col = QColor(T[self.token])
        halo = QColor(col)
        halo.setAlpha(int(40 + 60 * pulse))
        r = self._size / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, r + 3 * pulse, r + 3 * pulse)
        p.setBrush(col)
        p.drawEllipse(c, r * 0.62, r * 0.62)
