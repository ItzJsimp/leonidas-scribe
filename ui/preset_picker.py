"""
Leonidas Scribe - Preset Quick-Picker Overlay
Frameless, non-focus-stealing Qt overlay for selecting a text transform preset.
Shows numbered presets at the pill HUD position. Dismissed by 1-9 or Escape.
Space or 0 records a spoken instruction instead (voice-refactor).
"""
import ctypes
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QColor, QPainter, QPainterPath

user32 = ctypes.windll.user32

from .styles import T, FONT_SANS, FONT_MONO  # one palette, shared with the dashboard

PRESET_BG = QColor(T["obsidian_850"])
PRESET_BG.setAlpha(245)
PRESET_BORDER = QColor(T["obsidian_600"])


def _f(family, px, weight=QFont.Weight.Normal):
    f = QFont(family)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


class PresetPickerWidget(QWidget):
    """Floating frameless overlay that shows available transform presets.

    Emits preset_selected(key: str) when the user presses 1-9.
    Emits voice_instruction_requested() on Space or 0.
    Emits dismissed() on Escape or timeout.
    """
    preset_selected = Signal(str)  # preset key string ("1"-"9")
    voice_instruction_requested = Signal()
    dismissed = Signal()

    def __init__(self, presets: list, parent=None):
        super().__init__(parent)
        self.presets = presets  # list of dicts: {key, name, description}

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._build_ui()
        self._hook_handle = None
        self._hook_proc_ref = None

        # Auto-dismiss after 8s of inactivity
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.setInterval(8000)
        self._dismiss_timer.timeout.connect(self._do_dismiss)

    def _build_ui(self):
        if self.layout() is not None:
            while self.layout().count():
                item = self.layout().takeAt(0)
                widget = item.widget()
                if widget:
                    widget.setParent(None)
                    widget.deleteLater()
                elif item.layout():
                    while item.layout().count():
                        sub_item = item.layout().takeAt(0)
                        sub_w = sub_item.widget()
                        if sub_w:
                            sub_w.setParent(None)
                            sub_w.deleteLater()
            layout = self.layout()
        else:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(18, 14, 18, 14)
            layout.setSpacing(6)

        header = QLabel(f'<span style="color:{T["cyan"]}">//</span> TRANSFORM SELECTION')
        header.setFont(_f(FONT_MONO, 11))
        header.setStyleSheet(f"color: {T['ink_label']};")
        layout.addWidget(header)

        for p in self.presets:
            row = QHBoxLayout()
            row.setSpacing(10)

            key_badge = QLabel(p.get("key", "?"))
            key_badge.setFixedSize(22, 22)
            key_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            key_badge.setFont(_f(FONT_MONO, 12, QFont.Weight.DemiBold))
            key_badge.setStyleSheet(
                f"background: {T['obsidian_800']}; color: {T['cyan']}; "
                f"border: 1px solid {T['obsidian_700']}; border-radius: 6px;"
            )

            name_lbl = QLabel(p.get("name", ""))
            name_lbl.setFont(_f(FONT_SANS, 13, QFont.Weight.DemiBold))
            name_lbl.setStyleSheet(f"color: {T['ink']};")

            desc_lbl = QLabel(p.get("description", ""))
            desc_lbl.setFont(_f(FONT_SANS, 12))
            desc_lbl.setStyleSheet(f"color: {T['ink_dim']};")

            row.addWidget(key_badge)
            row.addWidget(name_lbl)
            row.addStretch()
            row.addWidget(desc_lbl)
            layout.addLayout(row)

        default = next((p for p in self.presets if str(p.get("key")) == self.default_key()), None)
        if default:
            enter_lbl = QLabel(f"Enter  {default.get('name', '')} (default)")
            enter_lbl.setFont(_f(FONT_SANS, 12))
            enter_lbl.setStyleSheet(f"color: {T['ink_muted']}; margin-top: 4px;")
            layout.addWidget(enter_lbl)

        voice_lbl = QLabel("Space or 0  Speak your own instruction")
        voice_lbl.setFont(_f(FONT_SANS, 12))
        voice_lbl.setStyleSheet(f"color: {T['ink_muted']}; margin-top: 4px;")
        layout.addWidget(voice_lbl)

        esc_lbl = QLabel("Esc to cancel")
        esc_lbl.setFont(_f(FONT_MONO, 11))
        esc_lbl.setStyleSheet(f"color: {T['ink_dim']}; margin-top: 4px;")
        layout.addWidget(esc_lbl)

        self.adjustSize()

    def _install_keyboard_hook(self):
        """Install a low-level keyboard hook only while the picker is visible."""
        if self._hook_handle:
            return

        import ctypes
        from ctypes import wintypes
        WH_KEYBOARD_LL = 13
        WM_KEYDOWN = 0x0100
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_ulonglong)
            ]

        def _hook_proc(nCode, wParam, lParam):
            if nCode >= 0 and self.isVisible() and wParam == WM_KEYDOWN:
                kb = KBDLLHOOKSTRUCT.from_address(lParam)
                vk = kb.vkCode
                # Number row keys 1-9 (VK 0x31-0x39) and numpad 1-9 (0x61-0x69)
                if 0x31 <= vk <= 0x39:
                    key = chr(vk)
                    QTimer.singleShot(0, self, lambda k=key: self._on_key(k))
                    return 1
                elif 0x61 <= vk <= 0x69:
                    key = str(vk - 0x60)
                    QTimer.singleShot(0, self, lambda k=key: self._on_key(k))
                    return 1
                elif vk in (0x20, 0x30, 0x60):  # Space, 0, numpad 0
                    QTimer.singleShot(0, self, lambda: self._on_key("0"))
                    return 1
                elif vk == 0x0D:  # Enter: the default preset
                    QTimer.singleShot(0, self, lambda: self._on_key(self.default_key()))
                    return 1
                elif vk == 0x1B:  # Escape
                    QTimer.singleShot(0, self, self._do_dismiss)
                    return 1
            return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._hook_proc_ref = HOOKPROC(_hook_proc)
        self._hook_handle = ctypes.windll.user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc_ref, None, 0
        )

    def _remove_keyboard_hook(self):
        if getattr(self, "_hook_handle", None):
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None
            self._hook_proc_ref = None

    def hideEvent(self, event):
        self._remove_keyboard_hook()
        super().hideEvent(event)

    def default_key(self) -> str:
        """The preset marked "default": true, else the first one."""
        for p in self.presets:
            if p.get("default"):
                return str(p.get("key"))
        return str(self.presets[0].get("key")) if self.presets else ""

    def _on_key(self, key: str):
        if key == "0":
            self._dismiss_timer.stop()
            self._remove_keyboard_hook()
            self.hide()
            self.voice_instruction_requested.emit()
            return
        valid_keys = {p.get("key") for p in self.presets}
        if key in valid_keys:
            self._dismiss_timer.stop()
            self._remove_keyboard_hook()
            self.hide()
            self.preset_selected.emit(key)

    def _do_dismiss(self):
        self._dismiss_timer.stop()
        self._remove_keyboard_hook()
        self.hide()
        self.dismissed.emit()

    def show_at_pill_position(self):
        """Position above where the HUD pill sits (bottom-center of primary screen)."""
        user32.SetProcessDPIAware()
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        w = max(self.sizeHint().width(), 280)
        h = self.sizeHint().height()
        x = (screen_w - w) // 2
        y = screen_h - h - 110  # just above the HUD pill
        self.setFixedWidth(w)
        self.move(x, y)
        self._install_keyboard_hook()
        self.show()
        self._dismiss_timer.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        path = QPainterPath()
        path.addRoundedRect(
            rect.x() + 1, rect.y() + 1, rect.width() - 2, rect.height() - 2,
            16, 16  # rounded-panel
        )

        # Dark frosted background
        painter.fillPath(path, PRESET_BG)

        # Subtle border
        painter.setPen(PRESET_BORDER)
        painter.drawPath(path)
