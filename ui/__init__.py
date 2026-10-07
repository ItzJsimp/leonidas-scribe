"""
UI package for Leonidas Scribe (Command register of the AIOS design system).
"""
from .styles import DARK_STYLESHEET, build_stylesheet, apply_native_window_chrome, load_brand_fonts, T as TOKENS
from .icons import create_app_icon_pixmap, get_app_icon, glyph_icon, GLYPH
from .dialogs import WordCorrectionDialog, PresetEditorDialog
from .widgets import (
    TranscriptCard, KeybindRecorderButton, ElidingLabel, icon_button, make_card, field,
    ToggleSwitch, setting_row, setting_group,
)
from .charts import SplitBar, BarList, DayBars, Heatmap
from .preset_picker import PresetPickerWidget

__all__ = [
    "DARK_STYLESHEET", "build_stylesheet", "apply_native_window_chrome", "load_brand_fonts", "TOKENS",
    "create_app_icon_pixmap", "get_app_icon", "glyph_icon", "GLYPH",
    "WordCorrectionDialog", "PresetEditorDialog",
    "TranscriptCard", "KeybindRecorderButton", "ElidingLabel", "icon_button", "make_card", "field",
    "ToggleSwitch", "setting_row", "setting_group",
    "SplitBar", "BarList", "DayBars", "Heatmap", "PresetPickerWidget",
]
