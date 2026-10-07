"""
Ctrl+K command palette (app-building-standard.md section 3).

Mirrors every important button and adds navigation. It speeds up repeats; every
command here also exists as a visible button somewhere in the app.
"""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QDialog, QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QLabel,
)

from .icons import glyph_icon
from .styles import T, build_stylesheet
from .widgets import section_label, kbd


class Command:
    def __init__(self, title: str, run, group: str = "Action", glyph: str = "flash", keywords: str = ""):
        self.title, self.run, self.group, self.glyph = title, run, group, glyph
        self.haystack = f"{title} {group} {keywords}".lower()

    def matches(self, query: str) -> bool:
        return all(tok in self.haystack for tok in query.lower().split())


class CommandPalette(QDialog):
    def __init__(self, parent, commands_fn):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(build_stylesheet())
        self.commands_fn = commands_fn
        self.commands = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("Palette")
        outer.addWidget(panel)
        v = QVBoxLayout(panel)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(8)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a command or a page")
        self.input.setMinimumHeight(38)
        self.input.addAction(glyph_icon("search", 14, T["ink_dim"]), QLineEdit.ActionPosition.LeadingPosition)
        self.input.textChanged.connect(self._filter)
        self.input.installEventFilter(self)
        v.addWidget(self.input)

        v.addWidget(section_label("Commands"))
        self.list = QListWidget()
        self.list.setMinimumHeight(240)
        self.list.itemActivated.connect(self._run_item)
        self.list.itemClicked.connect(self._run_item)
        v.addWidget(self.list, 1)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        for key, what in (("Enter", "run"), ("Up/Down", "move"), ("Esc", "close")):
            foot.addWidget(kbd(key))
            lbl = QLabel(what)
            lbl.setObjectName("Meta")
            foot.addWidget(lbl)
            foot.addSpacing(8)
        foot.addStretch(1)
        v.addLayout(foot)
        self.resize(580, 480)

    def open_over(self, window):
        self.commands = self.commands_fn()
        self.input.clear()
        self._filter("")
        geo = window.geometry()
        self.move(geo.x() + (geo.width() - self.width()) // 2, geo.y() + int(geo.height() * 0.14))
        self.show()
        self.input.setFocus()

    def _filter(self, text):
        self.list.clear()
        for cmd in self.commands:
            if cmd.matches(text):
                item = QListWidgetItem(glyph_icon(cmd.glyph, 14, T["ink_muted"]), f"{cmd.title}")
                item.setToolTip(cmd.group)
                item.setData(Qt.ItemDataRole.UserRole, cmd)
                self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, event):
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.list.currentRow() + (1 if key == Qt.Key.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.list.currentItem()
                if item:
                    self._run_item(item)
                return True
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def _run_item(self, item):
        cmd = item.data(Qt.ItemDataRole.UserRole)
        self.close()
        if cmd:
            cmd.run()
