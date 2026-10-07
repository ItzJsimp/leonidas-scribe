"""
Dialog windows and modal interfaces for Leonidas Scribe.

Imports styles/icons only: ui.widgets imports this module, so pulling widgets
back in here would close an import cycle.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, QWidget, QSizePolicy
)
from PySide6.QtCore import QSize
from .styles import build_stylesheet, apply_native_window_chrome, T
from .icons import glyph_icon


def _field(label_text: str, widget: QWidget, hint: str = "") -> QVBoxLayout:
    col = QVBoxLayout()
    col.setSpacing(6)
    lbl = QLabel(label_text)
    lbl.setObjectName("FieldLabel")
    col.addWidget(lbl)
    col.addWidget(widget)
    if hint:
        h = QLabel(hint)
        h.setObjectName("Helper")
        h.setWordWrap(True)
        col.addWidget(h)
    return col


def _footer(dialog: QDialog, save_text: str, glyph: str = "check") -> QHBoxLayout:
    """Cancel / confirm pair. Enter confirms, Esc cancels (Qt default on QDialog)."""
    box = QHBoxLayout()
    box.setSpacing(8)
    box.addStretch(1)

    btn_cancel = QPushButton("Cancel")
    btn_cancel.setMinimumHeight(32)
    btn_cancel.clicked.connect(dialog.reject)
    box.addWidget(btn_cancel)

    btn_save = QPushButton(save_text)
    btn_save.setObjectName("PrimaryBtn")
    btn_save.setMinimumHeight(32)
    btn_save.setIcon(glyph_icon(glyph, 14, T["white"]))
    btn_save.setIconSize(QSize(14, 14))
    btn_save.setDefault(True)
    btn_save.setAutoDefault(True)
    btn_save.clicked.connect(dialog.accept)
    box.addWidget(btn_save)
    return box


class _BaseDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet(build_stylesheet())
        self.setSizeGripEnabled(True)
        apply_native_window_chrome(self)


class WordCorrectionDialog(_BaseDialog):
    def __init__(self, sample_text="", parent=None):
        super().__init__("Correct a Word", parent)
        self.setMinimumSize(460, 300)
        self.resize(480, 320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(16)

        head = QLabel("Teach a replacement")
        head.setObjectName("CardTitle")
        layout.addWidget(head)

        desc = QLabel(
            "Leonidas Scribe will swap the misheard word for the correct spelling "
            "in every future dictation."
        )
        desc.setObjectName("Helper")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.txt_wrong = QLineEdit()
        self.txt_wrong.setPlaceholderText("Clawed")
        self.txt_wrong.setMinimumHeight(34)
        layout.addLayout(_field("Misheard word or phrase", self.txt_wrong))

        self.txt_correct = QLineEdit()
        self.txt_correct.setPlaceholderText("Claude")
        self.txt_correct.setMinimumHeight(34)
        layout.addLayout(_field("Correct replacement", self.txt_correct))

        layout.addStretch(1)
        layout.addLayout(_footer(self, "Learn & Save"))

    def get_values(self):
        return self.txt_wrong.text().strip(), self.txt_correct.text().strip()


class PresetEditorDialog(_BaseDialog):
    def __init__(self, preset=None, parent=None):
        self.preset = preset or {}
        is_edit = bool(preset and preset.get("name"))
        super().__init__("Edit Transform Preset" if is_edit else "New Transform Preset", parent)
        self.setMinimumSize(560, 480)
        self.resize(600, 540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(16)

        head = QLabel("Transform preset")
        head.setObjectName("CardTitle")
        layout.addWidget(head)

        sub = QLabel("Presets appear in the quick-picker overlay and rewrite the current selection in place.")
        sub.setObjectName("Helper")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        row_top = QHBoxLayout()
        row_top.setSpacing(14)

        self.txt_key = QLineEdit()
        self.txt_key.setFixedWidth(84)
        self.txt_key.setMinimumHeight(34)
        self.txt_key.setMaxLength(1)
        self.txt_key.setPlaceholderText("1")
        self.txt_key.setText(str(self.preset.get("key", "1")))
        row_top.addLayout(_field("Trigger key", self.txt_key))

        self.txt_name = QLineEdit()
        self.txt_name.setMinimumHeight(34)
        self.txt_name.setPlaceholderText("Prompt Engineer")
        self.txt_name.setText(self.preset.get("name", ""))
        row_top.addLayout(_field("Preset name", self.txt_name), 1)

        layout.addLayout(row_top)

        self.txt_desc = QLineEdit()
        self.txt_desc.setMinimumHeight(34)
        self.txt_desc.setPlaceholderText("Compress to a token-dense AI prompt")
        self.txt_desc.setText(self.preset.get("description", ""))
        layout.addLayout(_field("Short description", self.txt_desc,
                                "Shown next to the trigger key in the quick-picker HUD."))

        self.txt_instr = QTextEdit()
        self.txt_instr.setPlaceholderText(
            "Rewrite the selected text as…\n\nBe specific about tone, length, and what to preserve."
        )
        self.txt_instr.setText(self.preset.get("instruction", ""))
        self.txt_instr.setMinimumHeight(150)
        self.txt_instr.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        instr_col = _field("Rewrite directives", self.txt_instr,
                           "Sent verbatim to the polishing model alongside the selection.")
        layout.addLayout(instr_col, 1)

        layout.addLayout(_footer(self, "Save Preset", "save"))

    def get_preset(self) -> dict:
        return {
            "key": self.txt_key.text().strip() or "1",
            "name": self.txt_name.text().strip() or "Custom Preset",
            "description": self.txt_desc.text().strip() or "Custom rewrite preset",
            "instruction": self.txt_instr.toPlainText().strip()
        }
