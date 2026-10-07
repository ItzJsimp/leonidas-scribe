"""
Painted chart primitives for the Insights page.

Hand-painted with QPainter rather than QtCharts or matplotlib: these are small
fixed shapes, and a charting dependency would cost more to theme than to draw.
Series 1 is always cyan (design-system.md); labels are Archivo, numbers are
JetBrains Mono.
"""
from datetime import date, timedelta

from PySide6.QtWidgets import QWidget, QSizePolicy, QToolTip
from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QPainter, QColor, QFont

from .styles import T, FONT_SANS, FONT_MONO


def _font(px: int, mono=False, weight=QFont.Weight.Normal) -> QFont:
    f = QFont(FONT_MONO if mono else FONT_SANS)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


class SplitBar(QWidget):
    """One stacked horizontal bar: where the time goes. segments: [(label, value, token)]."""

    def __init__(self, segments, parent=None):
        super().__init__(parent)
        self.segments = segments
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_segments(self, segments):
        self.segments = segments
        self.update()

    def paintEvent(self, event):
        total = sum(max(0.0, s[1]) for s in self.segments)
        if total <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar_h, gap, x = 10.0, 3.0, 0.0
        usable = self.width() - gap * (len(self.segments) - 1)
        p.setPen(Qt.PenStyle.NoPen)
        for label, value, token in self.segments:
            seg_w = usable * (max(0.0, value) / total)
            p.setBrush(QColor(T[token]))
            p.drawRoundedRect(QRectF(x, 0, seg_w, bar_h), 5, 5)
            x += seg_w + gap
        x = 0.0
        for label, value, token in self.segments:
            p.setBrush(QColor(T[token]))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(x, bar_h + 16, 8, 8))
            p.setFont(_font(12))
            p.setPen(QColor(T["ink_muted"]))
            p.drawText(QRectF(x + 14, bar_h + 10, 200, 20),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            lw = p.fontMetrics().horizontalAdvance(label)
            p.setFont(_font(12, mono=True))
            p.setPen(QColor(T["ink"]))
            pct = f"{value / total * 100:.0f}%"
            p.drawText(QRectF(x + 20 + lw, bar_h + 10, 60, 20),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, pct)
            x += 20 + lw + p.fontMetrics().horizontalAdvance(pct) + 28


class BarList(QWidget):
    """Ranked horizontal bars: label left, bar, count right. rows: [(label, count)]."""
    ROW_H = 30

    def __init__(self, rows, empty_text="No data yet.", parent=None):
        super().__init__(parent)
        self.rows = list(rows)
        self.empty_text = empty_text
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_rows(self, rows):
        self.rows = list(rows)
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        return QSize(300, self.ROW_H * len(self.rows) if self.rows else 56)

    def minimumSizeHint(self):
        return QSize(220, self.sizeHint().height())

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.rows:
            p.setFont(_font(12))
            p.setPen(QColor(T["ink_dim"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)
            return
        top = max(c for _, c in self.rows) or 1
        label_w, count_w = 132.0, 56.0
        track_x = label_w + 12
        track_w = max(40.0, self.width() - track_x - count_w)
        for i, (label, count) in enumerate(self.rows):
            y = i * self.ROW_H
            mid = y + self.ROW_H / 2.0
            p.setFont(_font(12))
            p.setPen(QColor(T["ink"]))
            p.drawText(QRectF(0, y, label_w, self.ROW_H),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       p.fontMetrics().elidedText(str(label), Qt.TextElideMode.ElideRight, int(label_w)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(T["obsidian_700"]))
            p.drawRoundedRect(QRectF(track_x, mid - 3, track_w, 6), 3, 3)
            p.setBrush(QColor(T["cyan"]))
            p.drawRoundedRect(QRectF(track_x, mid - 3, max(6.0, track_w * count / top), 6), 3, 3)
            p.setFont(_font(12, mono=True))
            p.setPen(QColor(T["ink_muted"]))
            p.drawText(QRectF(track_x + track_w + 8, y, count_w - 8, self.ROW_H),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{count:,}")


class DayBars(QWidget):
    """Seven vertical bars, one per day, today last and highlighted in gold.
    days: [(date_iso, words)] oldest first."""

    def __init__(self, days=None, parent=None):
        super().__init__(parent)
        self.days = list(days or [])
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_days(self, days):
        self.days = list(days)
        self.update()

    def paintEvent(self, event):
        if not self.days:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = max(w for _, w in self.days) or 1
        n = len(self.days)
        slot = self.width() / n
        bar_w = min(28.0, slot * 0.5)
        chart_h = self.height() - 40
        for i, (day, words) in enumerate(self.days):
            cx = slot * i + slot / 2
            h = max(3.0, chart_h * words / top)
            y = 16 + chart_h - h
            is_today = i == n - 1
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(T["gold" if is_today else "cyan"]))
            p.drawRoundedRect(QRectF(cx - bar_w / 2, y, bar_w, h), 4, 4)
            p.setFont(_font(10, mono=True))
            p.setPen(QColor(T["ink_muted"]))
            p.drawText(QRectF(cx - slot / 2, y - 16, slot, 14), Qt.AlignmentFlag.AlignCenter,
                       f"{words / 1000:.1f}k" if words >= 1000 else str(words))
            p.setFont(_font(11))
            p.setPen(QColor(T["ink" if is_today else "ink_dim"]))
            label = "Today" if is_today else date.fromisoformat(day).strftime("%a")
            p.drawText(QRectF(cx - slot / 2, self.height() - 20, slot, 18), Qt.AlignmentFlag.AlignCenter, label)


class Heatmap(QWidget):
    """Activity grid, one column per week, Sunday at the top. by_day: {"YYYY-MM-DD": count}."""
    CELL = 13
    GAP = 3
    LABEL_W = 30

    def __init__(self, by_day=None, weeks=18, parent=None):
        super().__init__(parent)
        self.by_day = dict(by_day or {})
        self.weeks = weeks
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, by_day, min_weeks=8, max_weeks=26):
        self.by_day = dict(by_day or {})
        if self.by_day:
            first = date.fromisoformat(min(self.by_day))
            span = ((date.today() - first).days // 7) + 1
            self.weeks = max(min_weeks, min(max_weeks, span))
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        return QSize(self.LABEL_W + self.weeks * (self.CELL + self.GAP), 7 * (self.CELL + self.GAP) + 4)

    def _grid_start(self) -> date:
        today = date.today()
        sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        return sunday - timedelta(weeks=self.weeks - 1)

    def _cell_date(self, col: int, row: int) -> date:
        return self._grid_start() + timedelta(weeks=col, days=row)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = max(self.by_day.values()) if self.by_day else 0
        today = date.today()
        p.setFont(_font(10, mono=True))
        for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
            p.setPen(QColor(T["ink_dim"]))
            p.drawText(QRectF(0, row * (self.CELL + self.GAP), self.LABEL_W - 6, self.CELL),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, name)
        p.setPen(Qt.PenStyle.NoPen)
        for col in range(self.weeks):
            for row in range(7):
                d = self._cell_date(col, row)
                if d > today:
                    continue
                count = self.by_day.get(d.isoformat(), 0)
                if count and top:
                    c = QColor(T["cyan"])
                    c.setAlphaF(min(1.0, 0.25 + 0.75 * (count / top)))  # floor keeps 1 visible
                else:
                    c = QColor(T["obsidian_800"])
                p.setBrush(c)
                p.drawRoundedRect(QRectF(self.LABEL_W + col * (self.CELL + self.GAP),
                                         row * (self.CELL + self.GAP), self.CELL, self.CELL), 3, 3)
                if d == today:
                    p.setPen(QColor(T["gold"]))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(QRectF(self.LABEL_W + col * (self.CELL + self.GAP) - 1.5,
                                             row * (self.CELL + self.GAP) - 1.5, self.CELL + 3, self.CELL + 3), 4, 4)
                    p.setPen(Qt.PenStyle.NoPen)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        col = (pos.x() - self.LABEL_W) // (self.CELL + self.GAP)
        row = pos.y() // (self.CELL + self.GAP)
        if 0 <= col < self.weeks and 0 <= row < 7:
            d = self._cell_date(int(col), int(row))
            if d <= date.today():
                n = self.by_day.get(d.isoformat(), 0)
                QToolTip.showText(event.globalPosition().toPoint(),
                                  f"{d.strftime('%b %d')}: {n} transcript{'' if n == 1 else 's'}", self)
                return
        QToolTip.hideText()
