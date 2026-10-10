"""Dark date and time picker for the meetings sheet, in place of QDateTimeEdit's native calendar.

Nothing is written while you browse: months and days only move the selection, and Set commits it
(the native popup committed on every click, so one stray month click rewrote a meeting to December).
Styling is QSS in ui/styles.py (DatePopup, DayBtn, DateField); there are no glows, and a
selected day is a thin rounded cyan outline, never a square box.
"""
import calendar
import datetime

from PySide6.QtCore import Qt, Signal, QDateTime
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QComboBox, QApplication,
)

from .widgets import button, icon_button

WEEKDAYS = ("S", "M", "T", "W", "T", "F", "S")


def _repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


class DatePopup(QFrame):
    picked = Signal(object)  # naive local datetime

    def __init__(self, dt: datetime.datetime, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("DatePopup")
        self.sel = dt.replace(second=0, microsecond=0)
        self.view_y, self.view_m = dt.year, dt.month
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(4)
        self.btn_prev = icon_button("chevron_left", "", "Previous month")
        self.btn_next = icon_button("chevron_right", "", "Next month")
        self.btn_prev.clicked.connect(lambda: self._shift(-1))
        self.btn_next.clicked.connect(lambda: self._shift(1))
        self.lbl_month = QLabel()
        self.lbl_month.setObjectName("Body")
        self.lbl_month.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_today = button("Today", "LinkBtn", None, "Jump to today")
        btn_today.clicked.connect(self._today)
        head.addWidget(self.btn_prev)
        head.addWidget(self.lbl_month, 1)
        head.addWidget(self.btn_next)
        head.addWidget(btn_today)
        root.addLayout(head)

        self.grid = QGridLayout()
        self.grid.setSpacing(2)
        for c, d in enumerate(WEEKDAYS):
            lbl = QLabel(d)
            lbl.setObjectName("Meta")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.grid.addWidget(lbl, 0, c)
        self.days = []
        for r in range(6):
            for c in range(7):
                b = QPushButton()
                b.setObjectName("DayBtn")
                b.setFixedSize(34, 30)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # no square focus box on a day
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _=False, i=len(self.days): self._pick(i))
                self.grid.addWidget(b, r + 1, c)
                self.days.append(b)
        root.addLayout(self.grid)

        trow = QHBoxLayout()
        trow.setSpacing(6)
        lbl = QLabel("TIME")
        lbl.setObjectName("Meta")
        self.cb_h, self.cb_m, self.cb_ap = QComboBox(), QComboBox(), QComboBox()
        self.cb_h.addItems([str(h) for h in range(1, 13)])
        self.cb_m.addItems([f"{m:02d}" for m in range(60)])
        self.cb_ap.addItems(["AM", "PM"])
        h12 = self.sel.hour % 12 or 12
        self.cb_h.setCurrentIndex(h12 - 1)
        self.cb_m.setCurrentIndex(self.sel.minute)
        self.cb_ap.setCurrentIndex(0 if self.sel.hour < 12 else 1)
        for cb in (self.cb_h, self.cb_m, self.cb_ap):
            cb.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            cb.setMaxVisibleItems(8)
        trow.addWidget(lbl)
        trow.addWidget(self.cb_h)
        trow.addWidget(QLabel(":"))
        trow.addWidget(self.cb_m)
        trow.addWidget(self.cb_ap)
        trow.addStretch(1)
        root.addLayout(trow)

        brow = QHBoxLayout()
        brow.setSpacing(8)
        brow.addStretch(1)
        btn_cancel = button("Cancel", "GhostBtn")
        btn_cancel.clicked.connect(self.close)
        self.btn_set = button("Set date", "AccentBtn")
        self.btn_set.clicked.connect(self._set)
        brow.addWidget(btn_cancel)
        brow.addWidget(self.btn_set)
        root.addLayout(brow)
        self._fill()

    # ----- month grid ----------------------------------------------------------
    def _cells(self):
        first = datetime.date(self.view_y, self.view_m, 1)
        start = first - datetime.timedelta(days=(first.weekday() + 1) % 7)  # weeks start on Sunday
        return [start + datetime.timedelta(days=i) for i in range(42)]

    def _fill(self):
        self.lbl_month.setText(f"{calendar.month_name[self.view_m]} {self.view_y}")
        today = datetime.date.today()
        self._cell_dates = self._cells()
        for b, d in zip(self.days, self._cell_dates):
            b.setText(str(d.day))
            b.setProperty("out", d.month != self.view_m)
            b.setProperty("today", d == today)
            b.setProperty("sel", d == self.sel.date())
            _repolish(b)

    def _shift(self, n: int):
        m = self.view_m - 1 + n
        self.view_y, self.view_m = self.view_y + m // 12, m % 12 + 1
        self._fill()  # browsing only: the selection does not move

    def _today(self):
        t = datetime.date.today()
        self.view_y, self.view_m = t.year, t.month
        self.sel = self.sel.replace(year=t.year, month=t.month, day=t.day)
        self._fill()

    def _pick(self, i: int):
        d = self._cell_dates[i]
        self.sel = self.sel.replace(year=d.year, month=d.month, day=d.day)
        self.view_y, self.view_m = d.year, d.month
        self._fill()

    def _set(self):
        h = self.cb_h.currentIndex() + 1
        hour = h % 12 + (12 if self.cb_ap.currentIndex() else 0)
        self.picked.emit(self.sel.replace(hour=hour, minute=self.cb_m.currentIndex()))
        self.close()

    def show_below(self, anchor):
        self.adjustSize()
        pos = anchor.mapToGlobal(anchor.rect().bottomLeft())
        scr = (anchor.screen() or QApplication.primaryScreen()).availableGeometry()
        x = max(scr.left() + 8, min(pos.x(), scr.right() - self.width() - 8))
        y = pos.y() + 6
        if y + self.height() > scr.bottom() - 8:  # no room below: open above
            y = anchor.mapToGlobal(anchor.rect().topLeft()).y() - self.height() - 6
        self.move(x, max(scr.top() + 8, y))
        self.show()


class DateField(QPushButton):
    """A field that shows 'Oct 10, 2026  2:00 AM' and opens DatePopup. Same small API as the QDateTimeEdit it replaces."""
    editingFinished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DateField")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dt = datetime.datetime.now().replace(second=0, microsecond=0)
        self._popup = None
        self.clicked.connect(self._open)
        self._show()

    def _show(self):
        d = self._dt
        self.setText(f"{d:%b} {d.day}, {d.year}  {d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}")

    def setDateTime(self, q: QDateTime):
        self._dt = datetime.datetime(q.date().year(), q.date().month(), q.date().day(), q.time().hour(), q.time().minute())
        self._show()

    def dateTime(self) -> QDateTime:
        d = self._dt
        return QDateTime(d.year, d.month, d.day, d.hour, d.minute, 0)

    def _open(self):
        self._popup = DatePopup(self._dt, self)
        self._popup.picked.connect(self._picked)
        self._popup.show_below(self)

    def _picked(self, dt: datetime.datetime):
        self._dt = dt
        self._show()
        self.editingFinished.emit()
