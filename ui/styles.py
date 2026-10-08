"""
Leonidas Scribe design system: the Command register of AIOS.

Every value mirrors projects/leonidas-command-center/src/tokens.css, the single
source of truth (usage contract: standards/design-system.md). This file maps
those tokens into Qt: a QSS template plus the brand font loader. Widgets carry
semantic objectNames (Card, SectionLabel, PrimaryBtn, ...) instead of inline
stylesheets, so the whole app restyles from here.

Qt has no box-shadow, so the standard's glows are real QGraphicsDropShadowEffect
glows applied by ui/motion.py (keyboard focus, primary hover), not QSS.
"""
import os
import sys
from pathlib import Path
from string import Template

# ---------------------------------------------------------------------------
# Tokens (tokens.css, Command register). Names follow the CSS variables.
# ---------------------------------------------------------------------------
T = {
    # Obsidian elevation: step up one tier per layer, never skip two.
    "obsidian_950": "#07070a",   # canvas
    "obsidian_900": "#0e0e12",   # rail, page sections
    "obsidian_850": "#15151b",   # cards
    "obsidian_800": "#1d1d25",   # raised: hover, popovers, dialogs, secondary buttons
    "obsidian_700": "#2a2a35",   # border
    "obsidian_600": "#3a3a48",   # border strong, scrollbar hover
    "glass":        "rgba(21, 21, 27, 0.72)",
    "hairline":     "rgba(255, 255, 255, 0.06)",
    "scrim":        "rgba(7, 7, 10, 0.82)",

    # Ink
    "ink":          "#f4f4f5",   # 18.3:1
    "ink_label":    "#e4e4e7",   # mono // section labels
    "ink_muted":    "#a1a1aa",   # 7.9:1
    "ink_dim":      "#84848e",   # 4.5:1 on every obsidian tier

    # Brand: each hue has one job
    "crimson":       "#d92626",  # primary action, active nav bar, the one thing to press
    "crimson_hover": "#e53939",  # fills without text only (white on it is 4.2:1)
    "crimson_press": "#b51e1e",
    "crimson_text":  "#f05252",  # crimson as text on obsidian
    "crimson_soft":  "rgba(217, 38, 38, 0.12)",
    "cyan":          "#00e5ff",  # interaction and live: focus, links, selection, // prefix, chart series 1
    "cyan_soft":     "rgba(0, 229, 255, 0.12)",
    "gold":          "#f59e0b",  # status: attention, pending, KPI highlights
    "gold_soft":     "rgba(245, 158, 11, 0.12)",
    "success":       "#10b981",
    "success_soft":  "rgba(16, 185, 129, 0.12)",
    "error":         "#ff4d6d",  # rose-shifted so it never reads as crimson
    "error_soft":    "rgba(255, 77, 109, 0.12)",
    "white":         "#ffffff",  # text on crimson and error fills

    # Shape (S2 soft)
    "r_control": "8px",
    "r_card":    "12px",
    "r_panel":   "16px",

    # Type scale
    "fs_micro":   "10px",   # badges
    "fs_label":   "11px",   # mono // section labels, captions
    "fs_body":    "12px",   # UI text, inputs, table cells
    "fs_title":   "14px",   # card titles; transcript reading text
    "fs_heading": "20px",   # page headers, the smallest Big Shoulders size
    "fs_kpi":     "32px",   # big numbers, always mono

    "font_display": '"Big Shoulders Display", "Archivo Narrow", "Arial Narrow"',
    "font_sans":    '"Archivo", "Segoe UI Variable Text", "Segoe UI"',
    "font_mono":    '"JetBrains Mono", "Cascadia Mono", "Consolas"',
}

# Family names as registered by QFontDatabase, for QFont() in paint code.
FONT_DISPLAY = "Big Shoulders Display"
FONT_SANS = "Archivo"
FONT_MONO = "JetBrains Mono"


# ---------------------------------------------------------------------------
# Brand fonts: shipped as files, never a CDN (design-system.md "Typography")
# ---------------------------------------------------------------------------
def _font_dir() -> Path:
    """Frozen build: fonts/ beside the bundle. Source run: the repo's assets/fonts."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))) / "fonts"
    return Path(__file__).resolve().parents[3] / "assets" / "fonts"


_fonts_loaded = None


def load_brand_fonts() -> list:
    """Register every bundled TTF once per process. Returns the families found.
    Needs a QGuiApplication; safe to call again (it no-ops after the first run)."""
    global _fonts_loaded
    if _fonts_loaded is not None:
        return _fonts_loaded
    from PySide6.QtGui import QFontDatabase, QFont, QGuiApplication
    families = set()
    for ttf in sorted(_font_dir().rglob("*.ttf")):
        fid = QFontDatabase.addApplicationFont(str(ttf))
        if fid >= 0:
            families.update(QFontDatabase.applicationFontFamilies(fid))
    # Qt's DirectWrite default leaves app fonts unhinted: glyphs land on fractional
    # pixels, so 11-14px text smears and spaces unevenly at 100% scaling. Full
    # hinting snaps stems and advances to the pixel grid. Every QSS and QPainter
    # font resolves against the app font, so this one line covers HUD and charts.
    font = QGuiApplication.font()
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    QGuiApplication.setFont(font)
    _fonts_loaded = sorted(families)
    return _fonts_loaded


# ---------------------------------------------------------------------------
# Vector chrome (QSS cannot draw a checkmark or chevron; it needs an image)
# ---------------------------------------------------------------------------
_ASSETS = Path(__file__).parent / "assets"

_SVG = {
    "check.svg":
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
        f'<path d="M2.5 6.2 L4.8 8.6 L9.5 3.6" fill="none" stroke="{T["obsidian_950"]}" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "chevron_down.svg":
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
        f'<path d="M2.2 3.8 L5 6.6 L7.8 3.8" fill="none" stroke="{T["ink_dim"]}" '
        'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "chevron_up.svg":
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
        f'<path d="M2.2 6.2 L5 3.4 L7.8 6.2" fill="none" stroke="{T["ink_dim"]}" '
        'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
}


def _ensure_assets() -> dict:
    """Write the SVG chrome once, return POSIX paths (QSS chokes on backslashes)."""
    _ASSETS.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, body in _SVG.items():
        p = _ASSETS / name
        try:
            if not p.exists() or p.read_text(encoding="utf-8") != body:
                p.write_text(body, encoding="utf-8")
        except OSError:
            pass
        paths[name.replace(".svg", "_url")] = p.as_posix()
    return paths


# The canvas is painted by the ambient layer (ui/motion.py), so plain widgets
# are transparent and every container that needs a fill names its own tier.
_QSS = Template("""
/* ---------- base ---------- */
QWidget {
    background: transparent;
    color: $ink;
    font-family: $font_sans;
    font-size: $fs_body;
}
QMainWindow, QDialog { background-color: $obsidian_950; }
QMessageBox { background-color: $obsidian_800; }
QToolTip {
    background-color: $obsidian_800;
    color: $ink;
    border: 1px solid $obsidian_600;
    border-radius: 6px;
    padding: 5px 8px;
    font-size: $fs_label;
}

/* ---------- type roles ---------- */
QLabel#PageTitle   { font-family: $font_display; font-size: $fs_heading; font-weight: 800; color: $ink; }
QLabel#Wordmark    { font-family: $font_display; font-size: $fs_heading; font-weight: 800; color: $ink; }
QLabel#SectionLabel { font-family: $font_mono; font-size: $fs_label; color: $ink_label; }
QLabel#CardTitle   { font-size: $fs_title; font-weight: 600; color: $ink; }
QLabel#CardHint, QLabel#Helper { font-size: $fs_body; color: $ink_muted; }
QLabel#FieldLabel  { font-size: $fs_body; font-weight: 600; color: $ink_muted; }
QLabel#RowLabel, QLabel#Body { font-size: $fs_body; color: $ink; }
QLabel#Reading     { font-size: $fs_title; color: $ink; }
QLabel#Meta        { font-family: $font_mono; font-size: $fs_label; color: $ink_dim; }
/* Status tone: set with setProperty("tone", ...) + repolish, never an inline stylesheet. */
QLabel#RowLabel[tone="error"], QLabel#Meta[tone="error"]     { color: $error; }
QLabel#RowLabel[tone="success"], QLabel#Meta[tone="success"] { color: $success; }
QLabel#RowLabel[tone="gold"], QLabel#Meta[tone="gold"]       { color: $gold; }
QLabel#EmptyTitle  { font-size: $fs_title; font-weight: 600; color: $ink; }
QLabel#EmptyIcon   { background-color: $obsidian_800; border: 1px solid $obsidian_700; border-radius: 28px; }
QLabel#Kbd {
    font-family: $font_mono; font-size: $fs_micro; color: $ink_muted;
    background-color: $obsidian_800; border: 1px solid $obsidian_700;
    border-radius: 4px; padding: 2px 6px;
}
QLabel#StatOk   { font-size: $fs_body; color: $success; }
QLabel#StatWarn { font-size: $fs_body; color: $gold; }
QLabel#StatIdle { font-size: $fs_body; color: $ink_muted; }

/* ---------- numbers: every figure is JetBrains Mono ---------- */
QLabel#Kpi      { font-family: $font_mono; font-size: $fs_kpi; font-weight: 600; color: $ink; }
QLabel#KpiGold  { font-family: $font_mono; font-size: $fs_kpi; font-weight: 600; color: $gold; }
QLabel#KpiLabel { font-family: $font_mono; font-size: $fs_label; color: $ink_label; }
QLabel#KpiNote  { font-size: $fs_body; color: $ink_muted; }
QLabel#Num      { font-family: $font_mono; font-size: $fs_body; color: $ink; }

/* ---------- status badges: soft fill, matching text ---------- */
QLabel#Badge, QLabel#BadgeCyan, QLabel#BadgeGold, QLabel#BadgeSuccess, QLabel#BadgeError, QLabel#BadgeCrimson {
    font-family: $font_mono; font-size: $fs_micro; font-weight: 600;
    padding: 2px 8px; border-radius: 9px;
}
QLabel#Badge        { background-color: $obsidian_800; color: $ink_muted; }
QLabel#BadgeCyan    { background-color: $cyan_soft; color: $cyan; }
QLabel#BadgeGold    { background-color: $gold_soft; color: $gold; }
QLabel#BadgeSuccess { background-color: $success_soft; color: $success; }
QLabel#BadgeError   { background-color: $error_soft; color: $error; }
QLabel#BadgeCrimson { background-color: $crimson_soft; color: $crimson_text; }
QLabel#BadgeKey {
    font-family: $font_mono; font-size: $fs_body; font-weight: 600;
    background-color: $obsidian_800; color: $cyan;
    border: 1px solid $obsidian_700; border-radius: 6px;
}

/* ---------- surfaces ---------- */
QFrame#Rail { background-color: $obsidian_900; border: none; border-right: 1px solid $hairline; }
QFrame#Card {
    background-color: $obsidian_850;
    border: 1px solid $hairline;
    border-radius: $r_card;
}
QFrame#Card:focus { border: 1px solid $cyan; }
QFrame#Card[copied="true"] { border: 1px solid $cyan; }
/* Lift cards (transcripts) paint their own surface in motion.Hover so hover can grow it. */
QFrame#Card[lift="true"], QFrame#Card[lift="true"]:focus, QFrame#Card[lift="true"][copied="true"] {
    background: transparent; border: none;
}
QFrame#SettingGroup {
    background-color: $obsidian_900;
    border: 1px solid $hairline;
    border-radius: $r_card;
}
QWidget#SettingRow { background: transparent; border: none; border-radius: $r_control; }
QFrame#Divider { background-color: $obsidian_700; border: none; }
QFrame#SaveBar, QFrame#Toast {
    background-color: $obsidian_800;
    border: 1px solid $obsidian_600;
    border-radius: $r_card;
}
QFrame#Palette {
    background-color: $obsidian_800;
    border: 1px solid $obsidian_600;
    border-radius: $r_panel;
}
QFrame#StatusBlock {
    background-color: $obsidian_850;
    border: 1px solid $hairline;
    border-radius: $r_control;
}
QFrame#SideSheet {
    background-color: $obsidian_900;
    border: none;
    border-left: 1px solid $obsidian_700;
}
QFrame#OverlayScrim {
    background-color: $scrim;
}
QFrame#OverlayFrame {
    background-color: $obsidian_900;
    border: 1px solid $obsidian_700;
    border-radius: $r_panel;
}
QFrame#PlayerBar {
    background-color: $obsidian_850;
    border: 1px solid $hairline;
    border-radius: $r_control;
}
QFrame#PillIsland, QFrame#PillCapsule {
    background-color: $obsidian_900;
    border: 1px solid $obsidian_700;
    border-radius: 18px;
}
QFrame#PillPanel {
    background-color: $obsidian_900;
    border: 1px solid $obsidian_600;
    border-radius: $r_panel;
}
QFrame#CallPromptToast {
    background-color: $obsidian_850;
    border: 1px solid $obsidian_600;
    border-radius: $r_card;
}

/* ---------- meetings (S6) ---------- */
QFrame#Card[selected="true"] { border: 1px solid $cyan; }
QLabel#CardTitleMono { font-family: $font_mono; font-size: $fs_body; font-weight: 600; color: $ink; }
QLabel#SheetTitle  { font-family: $font_display; font-size: $fs_heading; font-weight: 800; color: $ink; }
QLabel#Speaker     { font-size: $fs_body; font-weight: 600; color: $ink; }
QLabel#NumCyan     { font-family: $font_mono; font-size: $fs_body; color: $cyan; }
QLabel#PillTime    { font-family: $font_mono; font-size: $fs_title; font-weight: 600; color: $ink; }
QLabel#Chip {
    font-size: $fs_label; font-weight: 600; color: $ink_muted;
    background-color: $obsidian_800; border: 1px solid $obsidian_700;
    border-radius: 6px; padding: 1px 8px;
}
QLabel#Avatar {
    font-family: $font_mono; font-size: $fs_micro; font-weight: 600; color: $ink_muted;
    background-color: $obsidian_800; border: 1px solid $obsidian_600; border-radius: 10px;
}
QLabel#BulletDot { background-color: $cyan; border-radius: 2px; }
QLabel#RecDot { background-color: $crimson_text; border-radius: 5px; }
QLabel#RecDot[state="paused"] { background-color: $gold; }
QLabel#RecDot[state="dead"]   { background-color: $error; }
QLabel#AskQuestion {
    font-size: $fs_body; color: $ink; background-color: $obsidian_800;
    border-radius: $r_control; padding: 6px 10px;
}
QFrame#AskBox { background-color: $obsidian_900; border: none; border-top: 1px solid $obsidian_700; }
QFrame#AnswerCard { background-color: $obsidian_850; border: 1px solid $obsidian_700; border-radius: $r_control; }
QFrame#TranscriptRow { background: transparent; border: none; border-radius: $r_control; }
QFrame#TranscriptRow[active="true"] { background-color: $cyan_soft; }

/* Underline tabs (Meetings filters, sheet Summary | Transcript). */
QPushButton#TabBtn {
    background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0px;
    color: $ink_muted; font-weight: 600; padding: 6px 2px;
}
QPushButton#TabBtn:hover   { color: $ink; }
QPushButton#TabBtn:checked { color: $ink; border-bottom: 2px solid $cyan; }
QPushButton#TabBtn:focus   { color: $cyan; }
/* Mono timestamp that plays audio from that moment. */
QPushButton#TimeLink {
    background: transparent; border: 1px solid transparent; border-radius: 4px;
    color: $cyan; font-family: $font_mono; font-size: $fs_label; font-weight: 500; padding: 2px 4px;
    text-align: left;
}
QPushButton#TimeLink:hover { color: $ink; background-color: $cyan_soft; }
QPushButton#TimeLink:focus { border: 1px solid $cyan; }
QPushButton#TimeLink:disabled { color: $ink_dim; }
QPushButton#ChipBtn, QPushButton#PersonChip {
    background-color: $obsidian_850; border: 1px solid $obsidian_700; border-radius: 13px;
    color: $ink_muted; font-weight: 500; padding: 3px 10px;
}
QPushButton#ChipBtn:hover, QPushButton#PersonChip:hover { color: $ink; border-color: $obsidian_600; }
QPushButton#ChipBtn:focus, QPushButton#PersonChip:focus { border: 1px solid $cyan; }
QPushButton#PersonChip { color: $ink; }
QPushButton#PlayBtn {
    background-color: $cyan_soft; border: 1px solid transparent; border-radius: 14px; padding: 0px;
}
QPushButton#PlayBtn:hover { border-color: $cyan; }
QPushButton#PlayBtn:focus { border: 1px solid $cyan; }
QPushButton#PlayBtn:disabled { background-color: $obsidian_800; }
QPushButton#PillBtn {
    background-color: $obsidian_800; border: 1px solid transparent; border-radius: 14px; padding: 0px;
}
QPushButton#PillBtn:hover { border-color: $obsidian_600; }
QPushButton#PillBtn:focus { border: 1px solid $cyan; }
QPushButton#AccentBtn {
    background-color: $cyan_soft; border: 1px solid transparent; color: $cyan; padding: 6px 12px;
    border-radius: 14px;
}
QPushButton#AccentBtn:hover { border-color: $cyan; }
/* Meetings Record: cyan is "live"; crimson stays with Dictate (one per view). */
QPushButton#RecordBtn { background-color: $cyan_soft; border: 1px solid transparent; color: $cyan; }
QPushButton#RecordBtn:hover { border-color: $cyan; }
QPushButton#RecordBtn:focus { border: 1px solid $cyan; }
QPushButton#RecordBtn:disabled { color: $ink_dim; background-color: $obsidian_850; }
QPushButton#AccentBtn:focus { border: 1px solid $cyan; }

/* ---------- buttons ---------- */
/* Default = secondary: obsidian-800 with a border that strengthens on hover. */
QPushButton {
    background-color: $obsidian_800;
    color: $ink;
    border: 1px solid $obsidian_700;
    border-radius: $r_control;
    padding: 7px 14px;
    min-height: 18px;
    font-size: $fs_body;
    font-weight: 600;
}
QPushButton:pressed  { background-color: $obsidian_850; }
QPushButton:focus    { border: 1px solid $cyan; }
QPushButton:disabled { color: $ink_dim; border-color: $obsidian_700; background-color: $obsidian_850; }

/* One per view. Hover keeps the fill and adds the glow (white on crimson-hover
   fails 4.5:1), so only press changes the fill. */
QPushButton#PrimaryBtn {
    background-color: $crimson;
    color: $white;
    border: 1px solid $crimson;
}
QPushButton#PrimaryBtn:hover   { border-color: $crimson_text; }
QPushButton#PrimaryBtn:pressed { background-color: $crimson_press; border-color: $crimson_press; }
QPushButton#PrimaryBtn:focus   { border: 1px solid $cyan; }

QPushButton#DestructiveBtn { background-color: $error; color: $white; border: 1px solid $error; }
QPushButton#DestructiveBtn:hover { border-color: $white; }

QPushButton#GhostBtn {
    background-color: transparent;
    border: 1px solid transparent;
    color: $ink_muted;
    padding: 5px 10px;
}
QPushButton#GhostBtn:hover   { color: $ink; }  /* fill and edge: motion.Hover, eased */
QPushButton#GhostBtn:pressed { background-color: $obsidian_850; }
QPushButton#GhostBtn:checked { color: $cyan; background-color: $cyan_soft; border-color: transparent; }
QPushButton#GhostBtn:focus   { border: 1px solid $cyan; }

QPushButton#LinkBtn {
    background: transparent; border: none; color: $cyan;
    padding: 2px 0px; font-weight: 600; text-align: left;
}
QPushButton#LinkBtn:hover { color: $ink; }
QPushButton#LinkBtn:focus { color: $ink; text-decoration: underline; }

QPushButton#DangerBtn {
    background-color: transparent;
    border: 1px solid transparent;
    color: $ink_muted;
    padding: 5px 10px;
}
QPushButton#DangerBtn:hover { color: $error; }  /* fill: motion.Hover, eased */
QPushButton#DangerBtn:focus { border: 1px solid $cyan; }

/* Navigation: a 2px crimson bar on obsidian-800 (design-system.md recipe). */
QPushButton#NavItem, QPushButton#SubNavItem {
    background-color: transparent;
    border: none;
    border-left: 2px solid transparent;
    border-radius: 0px;
    color: $ink_muted;
    font-size: $fs_body;
    font-weight: 500;
    padding: 8px 12px;
    text-align: left;
}
QPushButton#NavItem:hover, QPushButton#SubNavItem:hover { color: $ink; }  /* fill: motion.Hover, eased */
QPushButton#NavItem:checked, QPushButton#SubNavItem:checked {
    background-color: $obsidian_800;
    border-left: 2px solid $crimson;
    color: $ink;
    font-weight: 600;
}
QPushButton#NavItem:focus, QPushButton#SubNavItem:focus { background-color: $cyan_soft; }

/* Segmented page tabs (Dictionary). */
QPushButton#Segment {
    background-color: transparent;
    border: 1px solid $obsidian_700;
    border-radius: 0px;
    color: $ink_muted;
    font-weight: 600;
    padding: 7px 16px;
}
QPushButton#Segment[first="true"] { border-top-left-radius: $r_control; border-bottom-left-radius: $r_control; }
QPushButton#Segment[mid="true"]   { border-left: none; }
QPushButton#Segment[last="true"]  { border-top-right-radius: $r_control; border-bottom-right-radius: $r_control; border-left: none; }
QPushButton#Segment:hover   { color: $ink; }  /* fill: motion.Hover, eased */
QPushButton#Segment:checked { color: $ink; background-color: $obsidian_800; }
QPushButton#Segment:focus   { color: $cyan; }

QPushButton#KeybindBtn {
    background-color: $obsidian_900;
    border: 1px solid $obsidian_700;
    color: $ink;
    font-family: $font_mono;
    font-size: $fs_body;
    padding: 6px 12px;
    text-align: center;
}
QPushButton#KeybindBtn:hover { border-color: $cyan; }
QPushButton#KeybindBtn[recording="true"] { background-color: $cyan_soft; border: 1px solid $cyan; color: $cyan; }

/* ---------- inputs ---------- */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: $obsidian_900;
    color: $ink;
    border: 1px solid $obsidian_700;
    border-radius: $r_control;
    padding: 7px 10px;
    font-size: $fs_body;
    selection-background-color: $cyan;
    selection-color: $obsidian_950;
}
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover { border-color: $obsidian_600; }
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus { border: 1px solid $cyan; }
QLineEdit:disabled, QTextEdit:disabled { color: $ink_dim; background-color: $obsidian_950; }
QTextEdit#Mono, QPlainTextEdit#Mono { font-family: $font_mono; font-size: $fs_body; }

QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit {
    background-color: $obsidian_900;
    color: $ink;
    border: 1px solid $obsidian_700;
    border-radius: $r_control;
    padding: 7px 10px;
    font-size: $fs_body;
    min-height: 18px;
}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QDateTimeEdit:hover { border-color: $obsidian_600; }
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateTimeEdit:focus { border: 1px solid $cyan; }
QComboBox:disabled, QSpinBox:disabled { color: $ink_dim; background-color: $obsidian_950; }
QSpinBox, QDoubleSpinBox { font-family: $font_mono; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 26px; border: none; background: transparent;
}
QComboBox::down-arrow { image: url($chevron_down_url); width: 10px; height: 10px; }
QComboBox QAbstractItemView {
    background-color: $obsidian_800;
    color: $ink;
    border: 1px solid $obsidian_600;
    border-radius: $r_control;
    padding: 4px;
    outline: none;
    selection-background-color: $cyan_soft;
    selection-color: $ink;
}
QComboBox QAbstractItemView::item { min-height: 26px; padding: 2px 8px; border-radius: 6px; }
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; width: 22px; border: none; background: transparent;
}
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-position: top right; }
QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-position: bottom right; }
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background-color: $obsidian_800; border-radius: 4px; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url($chevron_up_url); width: 10px; height: 10px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url($chevron_down_url); width: 10px; height: 10px; }

QCheckBox { color: $ink; font-size: $fs_body; spacing: 9px; padding: 3px 0px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid $obsidian_600; border-radius: 4px; background-color: $obsidian_900;
}
QCheckBox::indicator:hover { border-color: $cyan; }
QCheckBox::indicator:checked { background-color: $cyan; border-color: $cyan; image: url($check_url); }

QRadioButton { color: $ink; font-size: $fs_body; spacing: 9px; padding: 3px 0px; }
QRadioButton::indicator {
    width: 16px; height: 16px;
    border: 1px solid $obsidian_600; border-radius: 8px; background-color: $obsidian_900;
}
QRadioButton::indicator:hover { border-color: $cyan; }
QRadioButton::indicator:checked { background-color: $cyan; border-color: $cyan; }

QSlider::groove:horizontal { height: 4px; background: $obsidian_700; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $cyan; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; height: 14px; margin: -5px 0px; background: $ink; border-radius: 7px; }
QSlider::handle:horizontal:hover { background: $white; }

/* ---------- scrollbars ---------- */
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { border: none; background: transparent; width: 10px; margin: 2px 0px; }
QScrollBar::handle:vertical { background: $obsidian_700; min-height: 32px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: $obsidian_600; }
QScrollBar:horizontal { border: none; background: transparent; height: 10px; margin: 0px 2px; }
QScrollBar::handle:horizontal { background: $obsidian_700; min-width: 32px; border-radius: 5px; }
QScrollBar::handle:horizontal:hover { background: $obsidian_600; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; background: none; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* ---------- tables: comfortable 40px rows ---------- */
QTableWidget, QTableView {
    background-color: transparent;
    alternate-background-color: transparent;
    border: none;
    gridline-color: transparent;
    color: $ink;
    font-size: $fs_body;
    selection-background-color: $cyan_soft;
    selection-color: $ink;
    outline: none;
}
QTableWidget::item, QTableView::item { padding: 6px 10px; border: none; border-bottom: 1px solid $obsidian_700; }
QTableWidget::item:hover { background-color: $obsidian_850; }
QTableWidget::item:selected { background-color: $cyan_soft; color: $ink; }
QHeaderView { background: transparent; }
QHeaderView::section {
    background-color: transparent;
    color: $ink_label;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid $obsidian_700;
    font-family: $font_mono;
    font-size: $fs_label;
}
QTableCornerButton::section { background-color: transparent; border: none; }

QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { color: $ink; padding: 8px 10px; border-radius: $r_control; }
QListWidget::item:selected { background-color: $obsidian_700; color: $ink; }
QListWidget::item:hover { background-color: $obsidian_850; }

/* ---------- menus (tray + context) ---------- */
QMenu {
    background-color: $obsidian_800;
    color: $ink;
    border: 1px solid $obsidian_600;
    border-radius: $r_control;
    padding: 6px;
    font-size: $fs_body;
}
QMenu::item { padding: 7px 14px 7px 12px; border-radius: 6px; margin: 1px 2px; }
QMenu::item:selected { background-color: $obsidian_700; color: $ink; }
QMenu::separator { height: 1px; background: $obsidian_700; margin: 5px 8px; }
QMenu::icon { padding-left: 6px; }

QMessageBox QLabel { color: $ink; font-size: $fs_body; }
QMessageBox QPushButton { min-width: 84px; }
""")


def build_stylesheet() -> str:
    values = dict(T)
    values.update(_ensure_assets())
    return _QSS.substitute(**values)


DARK_STYLESHEET = build_stylesheet()


# ---------------------------------------------------------------------------
# Native Windows chrome
# ---------------------------------------------------------------------------
def apply_native_window_chrome(widget) -> None:
    """Dark title bar, rounded corners, and an obsidian caption via DWM.
    Silently no-ops off Windows or on builds without the attributes."""
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        rgb = int(T["obsidian_900"][1:], 16)
        colorref = ((rgb & 0xFF) << 16) | (rgb & 0xFF00) | ((rgb >> 16) & 0xFF)
        # IMMERSIVE_DARK_MODE, WINDOW_CORNER_PREFERENCE=ROUND, CAPTION_COLOR (Win11)
        for attr, val in ((20, 1), (33, 2), (35, colorref)):
            v = ctypes.c_int(val)
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_int(attr),
                                      ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass
