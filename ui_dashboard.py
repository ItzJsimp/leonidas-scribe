"""
Leonidas Scribe - desktop dashboard shell.

The Command register of the AIOS design system (standards/design-system.md):
obsidian canvas with an ambient crimson/cyan drift, a rail with the one crimson
action, pages under ui/views/, Ctrl+K palette, and Undo toasts. This module is
the shell and wiring only; every page lives in its own view module.
"""
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QSystemTrayIcon, QMenu, QMessageBox, QScrollArea, QStackedWidget, QButtonGroup, QPushButton,
)

import ctypes
import os
import time
import threading

from PySide6.QtCore import Qt, QSize, QTimer, QFileSystemWatcher, Signal
from PySide6.QtGui import QShortcut, QKeySequence, QPixmap

from ui import (
    DARK_STYLESHEET, apply_native_window_chrome, load_brand_fonts, get_app_icon, create_app_icon_pixmap,
    glyph_icon, TOKENS,
)
from ui import motion, labels
from ui.palette import CommandPalette, Command
from ui.views.transcripts import TranscriptsView
from ui.views.insights import InsightsView
from ui.views.dictionary import DictionaryView
from ui.views.voice import VoiceView
from ui.views.meetings import MeetingsView
from ui.views.home import HomeView
from ui.views.settings import SECTIONS, SettingsView
from ui.widgets import AudioPlayer, Toast, button, kbd, tab_focus_only, section_label
from controllers.voice_controller import VoiceController
from controllers.dashboard_controller import DashboardController

PAGE_MAX_WIDTH = 1280


class DashboardWindow(QMainWindow):
    # Stack order = NAV order = Ctrl+N. Settings is last and sits at the bottom of
    # the rail, where most desktop apps put it.
    NAV = [
        ("home", "Home"),
        ("history", "Transcripts"),
        ("refresh", "Transforms"),
        ("chart", "Insights"),
        ("library", "Dictionary"),
        ("volume", "Voice"),
        ("mic", "Meetings"),
        ("settings", "Settings"),
    ]

    # GET /health runs on a background thread (network I/O); this marshals the
    # result back to the GUI thread instead of touching widgets from that thread.
    sig_voice_health = Signal(object)

    def __init__(self, config_path=None, dictionary_path=None, history_path=None,
                 on_toggle_dictation_cb=None, on_settings_saved_cb=None, state_provider=None):
        super().__init__()
        # Returns the engine's live state for the rail status block (see _poll_state).
        self.state_provider = state_provider
        self.controller = DashboardController(config_path, dictionary_path, history_path)
        self.config_path = self.controller.config_path
        self.dictionary_path = self.controller.dictionary_path
        self.history_path = self.controller.history_path
        self.on_toggle_dictation = on_toggle_dictation_cb
        self.on_settings_saved = on_settings_saved_cb
        self.is_really_quitting = False
        self._toast = None

        load_brand_fonts()
        self.load_data()
        ui_cfg = self.config.get("ui", {})
        motion.set_calm(bool(ui_cfg.get("calm") or ui_cfg.get("reduced_motion")))

        self.setWindowTitle("Leonidas Scribe")
        self.setWindowIcon(get_app_icon())
        self.setMinimumSize(1080, 660)
        self.resize(1200, 840)
        self.setStyleSheet(DARK_STYLESHEET)
        apply_native_window_chrome(self)

        app = QApplication.instance()
        self._focus_glow = motion.FocusGlow(app)
        self._hover = motion.Hover(self)
        app.installEventFilter(self._hover)

        self._init_ui()
        self._init_tray()
        self.palette = CommandPalette(self, self._commands)
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.open_palette)
        for i in range(len(self.NAV)):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda idx=i: self._select_page(idx))

        self._voice_controller = VoiceController()
        self.sig_voice_health.connect(self._on_voice_health)
        threading.Thread(target=self._poll_voice_health_loop, daemon=True).start()

    def load_data(self):
        self.config = self.controller.load_config()
        self.dict_data = self.controller.load_dictionary()

    # ------------------------------------------------------------------
    # Shared helpers the views use
    # ------------------------------------------------------------------
    @staticmethod
    def scroll_host(parent):
        """Frameless vertical scroller + its content widget. Guarantees no squish."""
        scroll = QScrollArea(parent)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        scroll.setWidget(content)
        return scroll, content

    def toast(self, message, role="success", action_text="", on_action=None, on_expire=None, ms=5000):
        """One toast at a time. A new toast settles the previous one, so a
        deferred delete commits before the next message appears."""
        if self._toast is not None:
            try:
                self._toast.settle()
            except RuntimeError:
                pass
        self._toast = Toast(self.centralWidget(), message, role, action_text, on_action, on_expire, ms)
        self._toast.place()

    def toggle_dictation(self):
        if self.on_toggle_dictation:
            self.on_toggle_dictation()
        else:
            self.toast("Dictation is only available while Scribe's engine is running.", "warn")

    def learn_correction(self, wrong, correct):
        ok, msg = self.controller.add_replacement(wrong, correct)
        if not ok:
            self.toast(f"Correction failed: {msg}", "error")
            return
        self.load_data()
        self.dictionary_view.refresh()

        def undo():
            self.controller.delete_replacement(wrong)
            self.load_data()
            self.dictionary_view.refresh()

        self.toast(f"Learned: “{wrong}” will be written as “{correct}”", "success", "Undo", undo)

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------
    def _init_ui(self):
        canvas = motion.AmbientCanvas()
        # The canvas takes focus on open so no control starts with a focus ring.
        canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setCentralWidget(canvas)
        shell = QHBoxLayout(canvas)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self._build_rail())

        # `self.tabs` stays the name callers and tests use: the page stack.
        self.tabs = QStackedWidget()
        # Every page caps at a reading width and centres on wide monitors (2560px split
        # labels from their numbers by ~1,500px); below the cap nothing changes.
        self.tabs.setMaximumWidth(PAGE_MAX_WIDTH)
        shell.addStretch(0)
        shell.addWidget(self.tabs, 1)
        shell.addStretch(0)

        self.home_view = HomeView(self)
        self.history_view = TranscriptsView(
            self, "history", "Transcripts", "Everything dictated on this machine, newest first. Click a card to copy it.",
            "Search every transcript", {"exclude_modes": ["preset_transform"]},
            "Nothing dictated yet", f"Press {self._toggle_key()} anywhere to start dictating.")
        self.transforms_view = TranscriptsView(
            self, "transforms", "Transforms", "Every selection rewrite from the preset picker, newest first.",
            "Search every transform", {"modes": ["preset_transform"]},
            "No transforms yet", "Select text anywhere and press your transform shortcut.")
        self.insights_view = InsightsView(self)
        self.dictionary_view = DictionaryView(self)
        self.voice_view = VoiceView(self)
        self.meetings_view = MeetingsView(self)
        self.settings_view = SettingsView(self)
        self.settings_view.dirty_changed.connect(self._on_settings_dirty)
        for view in (self.home_view, self.history_view, self.transforms_view, self.insights_view,
                     self.dictionary_view, self.voice_view, self.meetings_view, self.settings_view):
            self.tabs.addWidget(view)

        # Expose sub-view controls on DashboardWindow for test contracts and callers
        self.search_input = self.history_view.search_input
        self.table_replacements = getattr(self.dictionary_view, "table_replacements", None)
        self.table_vocab = getattr(self.dictionary_view, "table_vocab", None)
        self.cb_accent = None
        self.cb_accent2 = None

        for attr in (
            "cb_mic", "spin_max_sec", "spin_pause_sec",
            "cb_primary_prov", "cb_primary_model", "cb_backup_prov", "cb_backup_model",
            "cb_stt", "in_groq_key", "in_groq_backup_key", "in_cerebras_key",
            "btn_rec_toggle", "btn_rec_ptt", "btn_rec_capture", "btn_rec_transform",
            "txt_polish_instructions", "presets_container",
            "chk_failover", "chk_sound", "chk_hud", "chk_notify", "chk_lenses",
            "lbl_quota_stats", "btn_save",
        ):
            setattr(self, attr, getattr(self.settings_view, attr, None))

        for attr in (
            "lbl_words", "lbl_speed", "lbl_lat",
            "bar_latency", "bars_mode", "bars_app", "heatmap",
        ):
            setattr(self, attr, getattr(self.insights_view, attr, None))

        tab_focus_only(self)
        self.update_status()
        self.history_view.refresh()
        self.transforms_view.refresh()
        self._select_page(0)

        # Instant, deterministic refresh: anything that edits these files (Scribe,
        # an agent, a hand edit) repaints the pages that read them, no polling job.
        self._watcher = QFileSystemWatcher(self)
        for path in (self.history_path, self.dictionary_path):
            if os.path.exists(path):
                self._watcher.addPath(path)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(250)
        self._state_timer.timeout.connect(self._poll_state)
        self._state_timer.start()

    def toggle_key(self):
        return self._toggle_key()

    def select_view(self, view):
        self._select_page(self.tabs.indexOf(view))

    def _on_file_changed(self, path):
        # Editors replace files (delete + rename), which drops the watch; re-add it.
        if os.path.exists(path) and path not in self._watcher.files():
            self._watcher.addPath(path)
        if os.path.normcase(path) == os.path.normcase(self.dictionary_path):
            self.load_data()
            self.dictionary_view.refresh()
        else:
            self.refresh_history()

    def notify(self, title: str, message: str):
        """Native Windows notification from the tray icon (alerts, reports)."""
        self.tray_icon.showMessage(title, message, get_app_icon(), 10000)

    def _poll_state(self):
        """The rail status block mirrors the engine, 4 times a second, only while visible."""
        if not self.isVisible() or not self.state_provider:
            return
        try:
            st = self.state_provider()
        except Exception:
            return
        state = st.get("state", "ready")
        where = labels.app(st.get("app", ""))
        if state == "listening":
            secs = int(time.perf_counter() - st.get("since", time.perf_counter()))
            text, meta, token = f"Listening  {secs // 60}:{secs % 60:02d}", f"into {where}" if where else "", "crimson"
        elif state == "processing":
            text, meta, token = "Polishing", f"into {where}" if where else "", "gold"
        elif state == "mic_error":
            text, meta, token = "Mic unavailable", "Check Settings, Audio", "error"
        else:
            text, meta, token = "Ready", f"{self._toggle_key()} to dictate", "success"
        if self.lbl_status.text() != text:
            self.lbl_status.setText(text)
        if self.lbl_status_meta.text() != meta:
            self.lbl_status_meta.setText(meta)
        if getattr(self, "_status_token", None) != token:
            self._status_token = token
            self.status_dot.set_token(token)

    def _poll_voice_health_loop(self):
        """Background thread: GET /health every ~10s. Never touches widgets directly
        (see hud_qt.py access violation history); emits sig_voice_health instead."""
        while True:
            try:
                health = self._voice_controller.health()
            except Exception:
                health = None
            self.sig_voice_health.emit(health)
            time.sleep(10)

    def _on_voice_health(self, health):
        """Runs on the GUI thread via sig_voice_health."""
        if not health or "quiet" not in (health.get("gates") or []):
            self.btn_quiet_pill.hide()
            return
        held = health.get("queued", 0)
        self._quiet_held = held
        self.btn_quiet_pill.setText(f"  {DashboardWindow.quiet_pill_label(health.get('quiet_until', -1), held)}")
        self.btn_quiet_pill.setToolTip(f"Voice quiet is on, {held} announcement{'s' if held != 1 else ''} held. "
                                       "Click to turn it off now or choose when it ends.")
        self.btn_quiet_pill.show()

    @staticmethod
    def quiet_pill_label(quiet_until, held, now=None):
        """'Quiet, no end (3)' or 'Quiet to 9:15 PM (3)'; the held count is the number in
        brackets (spelled out in the tooltip), so the label fits the 179px rail pill.
        -1 (or a past time while the gate is still on) means manual quiet with no expiry."""
        now = time.time() if now is None else now
        if quiet_until and 0 < quiet_until < 2000000000 and quiet_until > now:
            end = " to " + time.strftime("%I:%M %p", time.localtime(quiet_until)).lstrip("0")
        else:
            end = ", no end"
        return f"Quiet{end} ({held})" if held else f"Quiet{end}"

    def _on_quiet_pill_clicked(self):
        """End-time selector: turn quiet off now (held announcements play) or set a timed quiet."""
        held = getattr(self, "_quiet_held", 0)
        menu = QMenu(self)
        off = menu.addAction(f"Turn quiet off now (play {held} held)" if held else "Turn quiet off now")
        off.triggered.connect(lambda: self._set_voice_quiet(None))
        menu.addSeparator()
        for minutes, text in ((30, "Quiet for 30 minutes"), (60, "Quiet for 1 hour"), (120, "Quiet for 2 hours")):
            act = menu.addAction(text)
            act.triggered.connect(lambda _=False, m=minutes: self._set_voice_quiet(m))
        menu.exec(self.btn_quiet_pill.mapToGlobal(self.btn_quiet_pill.rect().topLeft()))

    def _set_voice_quiet(self, minutes):
        """None turns quiet off; N minutes sets a timed quiet. Daemon HTTP stays off the GUI thread."""
        def _apply():
            try:
                if minutes is None:
                    self._voice_controller.unmute()
                else:
                    self._voice_controller.set_quiet(minutes=minutes)
                health = self._voice_controller.health()
            except Exception:
                health = None
            self.sig_voice_health.emit(health)

        threading.Thread(target=_apply, daemon=True).start()

    def _toggle_key(self):
        return self.config.get("hotkeys", {}).get("toggle_button", "Mouse5")

    def _build_rail(self):
        rail = QFrame()
        rail.setObjectName("Rail")
        rail.setFixedWidth(208)
        v = QVBoxLayout(rail)
        v.setContentsMargins(14, 18, 14, 14)
        v.setSpacing(2)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        mark = QLabel()
        ratio = self.devicePixelRatioF() or 1.0
        helmet = QPixmap(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mark.png"))
        if helmet.isNull():  # fall back to the badge if the helmet mark is not installed
            helmet = create_app_icon_pixmap(int(38 * ratio))
        pix = helmet.scaled(int(38 * ratio), int(38 * ratio), Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        pix.setDevicePixelRatio(ratio)
        mark.setPixmap(pix)
        mark.setFixedSize(38, 38)
        brand.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(0)
        over = QLabel("LEONIDAS")
        over.setObjectName("Meta")
        names.addWidget(over)
        word = QLabel("SCRIBE")
        word.setObjectName("Wordmark")
        names.addWidget(word)
        brand.addLayout(names)
        brand.addStretch(1)
        v.addLayout(brand)
        v.addSpacing(22)
        v.addWidget(section_label("Navigate"))
        v.addSpacing(6)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        for i, (glyph, label) in enumerate(self.NAV):
            if label == "Settings":
                v.addStretch(1)  # Settings sits at the bottom, above the status block
            btn = QPushButton("  " + label)
            btn.setObjectName("NavItem")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setIcon(glyph_icon(glyph, 15, TOKENS["ink_muted"]))
            btn.setIconSize(QSize(15, 15))
            btn.setMinimumHeight(38)
            btn.setToolTip(f"{label}  (Ctrl+{i + 1})")
            btn.clicked.connect(lambda _=False, idx=i: self._select_page(idx))
            self.nav_group.addButton(btn, i)
            self.nav_buttons.append(btn)
            v.addWidget(btn)

        btn_palette = button("Commands", "GhostBtn", "search", "Search pages and actions (Ctrl+K)")
        btn_palette.clicked.connect(self.open_palette)
        pal_row = QHBoxLayout()
        pal_row.setSpacing(6)
        pal_row.addWidget(btn_palette, 1)
        pal_row.addWidget(kbd("Ctrl K"))
        v.addSpacing(4)
        v.addLayout(pal_row)
        v.addSpacing(10)

        status = QFrame()
        status.setObjectName("StatusBlock")
        sh = QHBoxLayout(status)
        sh.setContentsMargins(8, 6, 10, 6)
        sh.setSpacing(6)
        self.status_dot = motion.BreathingDot("success")
        sh.addWidget(self.status_dot)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setObjectName("RowLabel")
        col.addWidget(self.lbl_status)
        self.lbl_status_meta = QLabel("")
        self.lbl_status_meta.setObjectName("Meta")
        col.addWidget(self.lbl_status_meta)
        sh.addLayout(col, 1)
        v.addWidget(status)
        v.addSpacing(6)

        # Voice quiet pill: hidden while the voice daemon speaks normally, shown
        # (with the held-announcement count) whenever quiet is active, whether
        # timed or indefinite. Click turns quiet off. See GET/POST 127.0.0.1:8765/quiet.
        self.btn_quiet_pill = button("Quiet on", "GhostBtn", "close", "Voice quiet: turn it off now or choose when it ends")
        self.btn_quiet_pill.clicked.connect(self._on_quiet_pill_clicked)
        self.btn_quiet_pill.hide()
        v.addWidget(self.btn_quiet_pill)
        v.addSpacing(8)

        self.btn_dictate = button("Dictate", "PrimaryBtn", "mic", "Start or stop dictation")
        self.btn_dictate.setMinimumHeight(40)
        self.btn_dictate.clicked.connect(self.toggle_dictation)
        v.addWidget(self.btn_dictate)
        return rail

    def _select_page(self, index):
        if index != self.tabs.currentIndex() and AudioPlayer.get().current:
            AudioPlayer.get().stop()  # a take playing on Transcripts ends when you leave it
        self.tabs.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)
            btn.setIcon(glyph_icon(self.NAV[i][0], 15, TOKENS["ink"] if i == index else TOKENS["ink_muted"]))
        page = self.tabs.widget(index)
        if page in (self.insights_view, self.home_view, self.meetings_view):
            page.refresh()
        motion.fade_in(page, 240)

    def _on_settings_dirty(self, n):
        """Q11: one crimson button per view. While Settings has unsaved changes,
        Save owns crimson and Dictate steps down to secondary."""
        self.btn_dictate.setObjectName("" if n else "PrimaryBtn")
        self.btn_dictate.setIcon(glyph_icon("mic", 14, TOKENS["ink_muted"] if n else TOKENS["white"]))
        self.btn_dictate.style().unpolish(self.btn_dictate)
        self.btn_dictate.style().polish(self.btn_dictate)
        motion.clear_glow(self.btn_dictate)

    def update_status(self):
        """Live state now comes from _poll_state; this seeds the idle text."""
        self.lbl_status_meta.setText(f"{self._toggle_key()} to dictate")

    # ------------------------------------------------------------------
    # Palette
    # ------------------------------------------------------------------
    def open_palette(self):
        self.palette.open_over(self)

    def _commands(self):
        cmds = [Command(f"Go to {name}", lambda i=i: self._select_page(i), "Page", glyph, f"page open ctrl+{i + 1}")
                for i, (glyph, name) in enumerate(self.NAV)]
        cmds += [
            Command("Start or stop dictation", self.toggle_dictation, "Action", "mic", "record dictate"),
            Command("Copy last transcript", self._copy_last, "Action", "copy", "clipboard paste latest"),
            Command("Search transcripts", self._focus_search, "Action", "search", "find"),
            Command("Add a replacement rule", self._focus_rule, "Dictionary", "add", "correction word fix"),
            Command("Run transcript audit", self.dictionary_view.run_transcript_audit, "Dictionary", "sync", "mishear"),
            Command("Toggle Calm (motion off)", self._toggle_calm, "Motion", "volume", "animation reduce"),
            Command("Check provider quotas", self._check_quotas, "Models", "refresh", "limits api"),
            Command("Refresh insights", self.insights_view.refresh, "Insights", "chart", "stats"),
        ]
        for i, (glyph, name) in enumerate(SECTIONS):
            cmds.append(Command(f"Settings: {name.replace('&&', '&')}",
                                lambda i=i: (self.select_view(self.settings_view), self.settings_view.select_section(i)),
                                "Settings", glyph, "preferences options"))
        return cmds

    def _copy_last(self):
        items = self.controller.load_history(limit=1, exclude_modes=["preset_transform"])
        if not items:
            self.toast("Nothing dictated yet.", "info")
            return
        QApplication.clipboard().setText((items[0].get("cleaned_text") or "").strip())
        self.toast("Copied your last transcript", "success")

    def _focus_search(self):
        self.select_view(self.history_view)
        self.history_view.search_input.setFocus()

    def _focus_rule(self):
        self.select_view(self.dictionary_view)
        self.dictionary_view.btn_tab_rules.click()
        self.dictionary_view.in_from.setFocus()

    def _toggle_calm(self):
        sv = self.settings_view
        sv.chk_calm.setChecked(not sv.chk_calm.isChecked())
        motion.set_calm(sv.chk_calm.isChecked())
        self.toast(f"Calm {'on: motion off' if sv.chk_calm.isChecked() else 'off'} (save Settings to keep it)", "info")

    def _check_quotas(self):
        self.select_view(self.settings_view)
        self.settings_view.select_section(1)
        self.settings_view._check_api_quota()

    # ------------------------------------------------------------------
    # Tray and window lifecycle
    # ------------------------------------------------------------------
    def _init_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(get_app_icon())
        self.tray_icon.setToolTip("Leonidas Scribe")
        tray_menu = QMenu()
        tray_menu.setStyleSheet(DARK_STYLESHEET)
        tray_menu.addAction(glyph_icon("history", 14), "Open dashboard").triggered.connect(self.show_dashboard)
        act_toggle = tray_menu.addAction(glyph_icon("mic", 14), f"Toggle dictation  ({self._toggle_key()})")
        act_toggle.triggered.connect(self.toggle_dictation)
        tray_menu.addSeparator()
        tray_menu.addAction(glyph_icon("close", 14), "Quit Leonidas Scribe").triggered.connect(self._quit_application)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _quit_application(self):
        self._settle_toast()
        self.is_really_quitting = True
        self.tray_icon.hide()
        QApplication.instance().quit()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick, QSystemTrayIcon.ActivationReason.Trigger):
            self.show_dashboard()

    def show_dashboard(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized | Qt.WindowState.WindowActive)
        self.raise_()
        self.activateWindow()
        try:
            hwnd = int(self.winId())
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self.refresh_history()

    def showEvent(self, event):
        super().showEvent(event)
        self.centralWidget().setFocus(Qt.FocusReason.OtherFocusReason)

    def refresh_history(self):
        """Fires on every history.jsonl write. Skipped while hidden: show_dashboard()
        refreshes on open, so nothing goes stale."""
        if not self.isVisible():
            return
        self.history_view.refresh()
        self.transforms_view.refresh()
        self.update_status()
        if self.tabs.currentWidget() in (self.insights_view, self.home_view):
            self.tabs.currentWidget().refresh()

    def _settle_toast(self):
        if self._toast is not None:
            try:
                self._toast.settle()
            except RuntimeError:
                pass
            self._toast = None

    def closeEvent(self, event):
        if AudioPlayer.get().current:
            AudioPlayer.get().stop()  # closing to tray should not leave a take playing
        if getattr(self, "is_really_quitting", False):
            event.accept()
            return
        if self.settings_view.dirty_count():
            box = QMessageBox(self)
            box.setWindowTitle("Unsaved settings")
            box.setText("You have unsaved settings. Save them before closing?")
            save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
            save.setObjectName("PrimaryBtn")
            discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is save:
                if not self.settings_view.save():
                    event.ignore()
                    return
            elif box.clickedButton() is discard:
                self.settings_view.discard()
            else:
                event.ignore()
                return
        self._settle_toast()
        event.ignore()
        self.hide()
        if self.config.get("hud", {}).get("notify_on_minimize", False):
            self.tray_icon.showMessage("Leonidas Scribe",
                                       f"Still running. {self._toggle_key()} or Left Ctrl + Left Win to dictate.",
                                       get_app_icon(), 2500)
