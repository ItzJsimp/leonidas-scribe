"""
Voice: everything about Leonidas' spoken voice, as a pure HTTP client of the
voice daemon (127.0.0.1:8765).

Layout: a live strip (daemon, active voice, queue, don't-talk rules, snooze),
then five sub-pages: Voices (named profiles with Play), Rules (when he may
talk), History (replay, resend, filter, lifecycle), Analytics, and Advanced
(persona, pronunciation, raw voices). Every setting waits in one save bar at
the bottom, exactly like Settings; actions (snooze, replay, announce,
pronunciation) run immediately.
"""
import collections
from datetime import date, timedelta

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QSpinBox, QDoubleSpinBox,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QAbstractItemView, QButtonGroup, QRadioButton, QStackedWidget, QGridLayout, QMenu,
)

from .. import motion
from ..charts import BarList, DayBars
from ..icons import glyph_icon
from ..styles import T
from ..widgets import (
    page_header, button, section_label, make_card, kpi_tile, ToggleSwitch, setting_row, setting_group,
    ElidingLabel, run_async,
)
from controllers.voice_controller import VoiceController, TIERS

PAGES = [("volume", "Voices"), ("settings", "Rules"), ("history", "History"),
         ("chart", "Analytics"), ("edit", "Advanced")]
GATES = [("scribe_mic", "While I'm dictating", "Holds everything, even critical, until Scribe stops recording."),
         ("call", "On a call or meeting", "Any app holding the mic (except the ignore list below)."),
         ("focus", "Full-screen or presenting", "Info waits; critical still speaks."),
         ("discord", "In a Discord voice channel", "Uses the Discord policy below.")]
DISCORD_POLICIES = ["undeafened", "unmuted", "in_voice"]
GATE_CHIPS = {
    "quiet": ("quiet", "Quiet is on (snooze, mute or the hotkey)."),
    "scribe_mic": ("dictating", "Scribe is recording; he speaks when you finish."),
    "call": ("call", "An app is holding the mic, so he waits."),
    "focus": ("focus", "Windows reports fullscreen, presenting or away."),
}
EVENTS_LIMIT = 2000
FISH_FREE_UNTIL = "2026-11-30"  # Fish Audio's s2.1-pro-free model; $15 per million characters after


def voice_to_text(voice) -> str:
    if isinstance(voice, dict):
        return ", ".join(f"{n}:{w:g}" for n, w in voice.items())
    return str(voice or "")


def text_to_voice(text: str):
    """'af_nicole' -> name; 'am_fenrir:0.6, bm_george:0.4' -> blend dict."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) == 1 and ":" not in parts[0]:
        return parts[0]
    blend = {}
    for p in parts:
        name, _, w = p.partition(":")
        blend[name.strip()] = float(w) if w.strip() else 1.0
    return blend


def lifecycle(e: dict) -> str:
    """Where one announcement got to, as a short trail."""
    steps = []
    if e.get("gates"):
        steps.append(f"Held ({', '.join(e['gates'])})")
    st = e.get("status", "")
    if st == "dropped":
        steps.append(f"Dropped ({e.get('reason') or 'gated ambient'})")
    elif st in ("queued", "requeued"):
        steps.append("Waiting to speak")
    elif st in ("playing", "speaking"):
        steps.append("Speaking")
    elif st in ("spoken", "replied"):
        steps.append("Spoken")
    if e.get("reply"):
        steps.append(f"Replied: {e['reply']}")
    if e.get("acknowledged"):
        steps.append("Read")
    return "  →  ".join(steps) or st.capitalize()


class ProfileCard(QFrame):
    """One named voice: what it is, its knobs, Play (unsaved values), and Active."""

    def __init__(self, pid: str, prof: dict, voices_hint: str, on_play, group: QButtonGroup):
        super().__init__()
        self.pid, self.base = pid, dict(prof)
        self.setObjectName("Card")
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel(prof.get("label", pid))
        title.setObjectName("CardTitle")
        head.addWidget(title)
        engine = QLabel("FISH AUDIO" if prof.get("engine") == "fish" else "KOKORO · LOCAL")
        engine.setObjectName("Meta")
        head.addWidget(engine)
        head.addStretch(1)
        self.radio = QRadioButton("Use this voice")
        self.radio.toggled.connect(
            lambda checked: self.radio.setText("Active voice" if checked else "Use this voice"))
        group.addButton(self.radio)
        head.addWidget(self.radio)
        self.btn_play = button("Play", "GhostBtn", "play", "Hear this voice with the values below, before saving")
        self.btn_play.clicked.connect(lambda: on_play(self))
        head.addWidget(self.btn_play)
        v.addLayout(head)
        if prof.get("blurb"):
            hint = QLabel(prof["blurb"])
            hint.setObjectName("CardHint")
            hint.setWordWrap(True)
            v.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.in_voice = QLineEdit(voice_to_text(prof.get("voice")))
        self.in_voice.setToolTip(voices_hint)
        self.in_fish = QLineEdit(prof.get("fish_reference_id", ""))
        self.in_fish.setToolTip("Fish Audio voice model ID (from the voice's page on fish.audio)")
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(0.5, 2.0)
        self.spin_speed.setSingleStep(0.05)
        self.spin_speed.setValue(float(prof.get("speed", 1.0)))
        self.spin_pitch = QDoubleSpinBox()
        self.spin_pitch.setRange(0.7, 1.3)
        self.spin_pitch.setSingleStep(0.02)
        self.spin_pitch.setValue(float(prof.get("pitch", 1.0)))
        self.spin_pitch.setToolTip("Below 1 is deeper. Kokoro voices only.")
        self.in_address = QLineEdit(prof.get("address_style", ""))
        self.in_address.setToolTip("What this voice calls you")
        fish = prof.get("engine") == "fish"
        cells = [("Fish voice ID", self.in_fish)] if fish else []
        cells += [("Offline fallback voice" if fish else "Voice or blend", self.in_voice),
                  ("Speed", self.spin_speed)]
        if not fish:
            cells.append(("Pitch", self.spin_pitch))
        cells.append(("Calls you", self.in_address))
        for col, (label, w) in enumerate(cells):
            lbl = QLabel(label)
            lbl.setObjectName("Helper")
            grid.addWidget(lbl, 0, col)
            w.setMinimumHeight(32)
            grid.addWidget(w, 1, col)
            grid.setColumnStretch(col, 2 if isinstance(w, QLineEdit) else 1)
        v.addLayout(grid)
        self.editors = (self.in_voice, self.in_fish, self.spin_speed, self.spin_pitch, self.in_address, self.radio)

    def value(self) -> dict:
        prof = dict(self.base)
        try:
            prof["voice"] = text_to_voice(self.in_voice.text())
        except ValueError:
            prof["voice"] = self.in_voice.text()
        if prof.get("engine") == "fish":
            prof["fish_reference_id"] = self.in_fish.text().strip()
        prof["speed"] = round(self.spin_speed.value(), 2)
        prof["pitch"] = round(self.spin_pitch.value(), 2)
        prof["address_style"] = self.in_address.text().strip()
        return prof


class VoiceView(QWidget):
    dirty_changed = Signal(int)

    def __init__(self, host, controller=None):
        super().__init__()
        self.host = host
        self.controller = controller or VoiceController()
        self._up = False
        self._voices = []
        self._pron_available = False
        self._loading = False
        self._baseline = {}
        self._events = []
        self.profile_cards = {}
        self.moment_checks = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 24, 16)
        root.setSpacing(12)
        self.btn_start = button("Start daemon", "GhostBtn", "play", "Run the idempotent voice daemon launcher")
        self.btn_start.clicked.connect(self.start_daemon)
        self.btn_refresh = button("Refresh", "", "sync", "Re-check the daemon and reload everything")
        self.btn_refresh.clicked.connect(self.refresh)
        root.addLayout(page_header("Voice", "How Leonidas sounds, when he talks, and everything he has said.",
                                   [self.btn_refresh, self.btn_start]))
        root.addWidget(self._build_strip())

        # Same segmented control as the Dictionary tabs (QPushButton#Segment).
        tabs = QHBoxLayout()
        tabs.setSpacing(0)
        self.page_group = QButtonGroup(self)
        self.page_buttons = []
        for i, (_glyph, name) in enumerate(PAGES):
            b = button(name, "Segment")
            b.setProperty("first" if i == 0 else "last" if i == len(PAGES) - 1 else "mid", True)
            b.setCheckable(True)
            self.page_group.addButton(b, i)
            self.page_buttons.append(b)
            tabs.addWidget(b)
        tabs.addStretch(1)
        root.addLayout(tabs)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        for build in (self._page_voices, self._page_rules, self._page_history,
                      self._page_analytics, self._page_advanced):
            if host is not None and hasattr(host, "scroll_host"):
                scroll, content = host.scroll_host(self)
            else:
                scroll = content = QWidget()
            v = QVBoxLayout(content)
            v.setContentsMargins(0, 4, 8, 12)
            v.setSpacing(12)
            build(v)
            v.addStretch(1)
            self.stack.addWidget(scroll)
        self.page_group.idClicked.connect(self.select_page)
        for w in (*self.gate_switches.values(), self.in_ignore_apps, self.cb_discord, self.chk_listen_reply,
                  self.spin_reply_window, self.in_address_style, self.chk_chime, self.chk_ai_rewrite,
                  self.spin_pause_ms, self.spin_max):
            self._watch(w)
        root.addWidget(self._build_save_bar())
        self.select_page(0)

        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.check_health)
        self._timer.start()
        self.check_health()

    # ------------------------------------------------------------------
    # Live strip
    # ------------------------------------------------------------------
    def _build_strip(self):
        strip = QFrame()
        strip.setObjectName("Card")
        h = QHBoxLayout(strip)
        h.setContentsMargins(16, 10, 12, 10)
        h.setSpacing(14)
        self.lbl_status = ElidingLabel("Checking the voice daemon...")
        self.lbl_status.setObjectName("RowLabel")
        h.addWidget(self.lbl_status, 1)
        # One chip per holding gate plus the held count; fixed-size chips (max 5) never clip.
        # Only the status label gives way (it elides), so the strip fits the window minimum.
        self.chip_row = QHBoxLayout()
        self.chip_row.setSpacing(6)
        self._chip_state = None
        h.addLayout(self.chip_row)
        self.lbl_quiet_state = QLabel("Voice: Active")
        self.lbl_quiet_state.setObjectName("Meta")
        h.addWidget(self.lbl_quiet_state)
        # One "Quiet" menu button replaces the five separate snooze/mute buttons that
        # used to clip lbl_live at 1200px. Unmute stays its own button, shown only
        # while a snooze or mute is active (see _update_quiet_display).
        self.btn_quiet = button("Quiet", "GhostBtn", "close", "Snooze or mute voice announcements")
        quiet_menu = QMenu(self.btn_quiet)
        quiet_menu.addAction("15 min", lambda: self.snooze(15))
        quiet_menu.addAction("30 min", lambda: self.snooze(30))
        quiet_menu.addAction("1 hour", lambda: self.snooze(60))
        quiet_menu.addAction("Mute 2h", self.mute_voice)
        quiet_menu.addAction("Mute until I unmute", self.mute_voice_indefinite)
        quiet_menu.addSeparator()
        quiet_menu.addAction("Skip next alert", self.skip_queue_action)
        quiet_menu.addAction("Clear held queue", self.clear_queue_action)
        self.btn_quiet.setMenu(quiet_menu)
        self.btn_unmute = button("Unmute", "", "volume", "Unmute and resume now")
        self.btn_unmute.clicked.connect(self.unmute_voice)
        self.btn_unmute.setVisible(False)
        h.addWidget(self.btn_quiet)
        h.addWidget(self.btn_unmute)
        return strip

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    def _page_voices(self, v):
        v.addWidget(section_label("Voices"))
        hint = QLabel("Pick the voice Leonidas speaks in. Play uses the values on the card before you save; "
                      "Active and edits wait in the save bar.")
        hint.setObjectName("Helper")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.profile_group = QButtonGroup(self)
        self.profile_group.setExclusive(True)
        self.profiles_box = QVBoxLayout()
        self.profiles_box.setSpacing(12)
        v.addLayout(self.profiles_box)

    def _page_rules(self, v):
        card, b = make_card("Don't talk over me", "When any of these is on and true, announcements wait.")
        self.gate_switches = {}
        rows = []
        for key, label, hint in GATES:
            sw = ToggleSwitch()
            self.gate_switches[key] = sw
            rows.append(setting_row(label, sw, hint))
        self.in_ignore_apps = QLineEdit()
        self.in_ignore_apps.setMinimumWidth(260)
        self.in_ignore_apps.setPlaceholderText("discord.exe, obs64.exe")
        rows.append(setting_row("Never counts as a call", self.in_ignore_apps,
                                "Apps that hold the mic without meaning you're talking (comma separated)."))
        self.cb_discord = QComboBox()
        self.cb_discord.addItems(DISCORD_POLICIES)
        rows.append(setting_row("Discord holds when I'm", self.cb_discord,
                                "undeafened (default), unmuted, or just in a voice channel."))
        b.addWidget(setting_group(rows))
        v.addWidget(card)

        card, b = make_card("Replies", "After a question, Leonidas can listen briefly for your spoken answer.")
        self.chk_listen_reply = ToggleSwitch()
        self.spin_reply_window = QSpinBox()
        self.spin_reply_window.setRange(3, 10)
        self.spin_reply_window.setSuffix(" s")
        b.addWidget(setting_group([
            setting_row("Listen for my reply", self.chk_listen_reply, "Opens the mic only after a question."),
            setting_row("Listening window", self.spin_reply_window, "How long he waits for you, 3 to 10 seconds."),
        ]))
        v.addWidget(card)

        card, b = make_card("Jarvis Moments", "Times Leonidas speaks up on his own. Edit rules in "
                                              "leonidas-voice/moments.json; switch them here.")
        self.moments_group = QVBoxLayout()
        self.moments_group.setSpacing(0)
        b.addLayout(self.moments_group)
        v.addWidget(card)

    def _page_history(self, v):
        row = QHBoxLayout()
        row.setSpacing(8)
        self.in_search = QLineEdit()
        self.in_search.setPlaceholderText("Search what he said")
        self.in_search.setMinimumHeight(32)
        self.in_search.textChanged.connect(self._fill_log)
        row.addWidget(self.in_search, 2)
        self.cb_filter_tier = QComboBox()
        self.cb_filter_tier.addItems(["All tiers", *TIERS])
        self.cb_filter_tier.currentIndexChanged.connect(self._fill_log)
        row.addWidget(self.cb_filter_tier)
        self.cb_filter_status = QComboBox()
        self.cb_filter_status.addItems(["Everything", "Unread", "Spoken", "Held or waiting", "Dropped", "Replied"])
        self.cb_filter_status.currentIndexChanged.connect(self._fill_log)
        row.addWidget(self.cb_filter_status)
        v.addLayout(row)

        acts = QHBoxLayout()
        acts.setSpacing(8)
        self.btn_replay = button("Replay", "", "play", "Play the selected announcement again (double-click a row too)")
        self.btn_replay.clicked.connect(self.replay_selected)
        self.btn_resend = button("Send again", "GhostBtn", "refresh", "Announce it again through the full pipeline")
        self.btn_resend.clicked.connect(self.resend_selected)
        self.btn_mark_read = button("Mark read", "GhostBtn", "check", "Mark the selected one read")
        self.btn_mark_read.clicked.connect(self.mark_selected_read)
        self.btn_ack_all = button("Mark all read", "GhostBtn", "check", "Mark everything read")
        self.btn_ack_all.clicked.connect(self.ack_all_events)
        for b in (self.btn_replay, self.btn_resend, self.btn_mark_read):
            acts.addWidget(b)
        acts.addStretch(1)
        self.lbl_log_count = QLabel("")
        self.lbl_log_count.setObjectName("Meta")
        acts.addWidget(self.lbl_log_count)
        self.btn_clear_queue = button("Clear queue", "GhostBtn", "trash", "Dismiss all queued announcements without speaking")
        self.btn_clear_queue.clicked.connect(self.clear_queue_action)
        acts.addWidget(self.btn_clear_queue)
        acts.addWidget(self.btn_ack_all)
        v.addLayout(acts)

        self.table_log = QTableWidget(0, 6)
        self.table_log.setHorizontalHeaderLabels(["When", "Tier", "From", "Announcement", "What happened", "Voice"])
        self._style_table(self.table_log, stretch=3)
        self.table_log.setMinimumHeight(320)
        self.table_log.itemSelectionChanged.connect(self._show_detail)
        self.table_log.itemDoubleClicked.connect(lambda *_: self.replay_selected())
        v.addWidget(self.table_log, 1)
        self.lbl_detail = QLabel("Select an announcement to see the full text, what was spoken, and any reply.")
        self.lbl_detail.setObjectName("Helper")
        self.lbl_detail.setWordWrap(True)
        self.lbl_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.lbl_detail)

    def _page_analytics(self, v):
        grid = QGridLayout()
        grid.setSpacing(12)
        self.kpis = {}
        for i, (key, label, note) in enumerate([
                ("today", "Today", "announcements"), ("week", "Last 7 days", "announcements"),
                ("minutes", "Spoken", "minutes, last 7 days"), ("held", "Held", "waited for a rule, 7 days"),
                ("replies", "Reply rate", "of questions answered"), ("fish", "Fish Audio", "characters this month")]):
            card, val, nt = kpi_tile(label, "-", note, gold=(key == "today"))
            self.kpis[key] = (val, nt)
            grid.addWidget(card, i // 3, i % 3)
        v.addLayout(grid)
        card, b = make_card("Last 7 days", "Announcements per day, today in gold.")
        self.chart_days = DayBars()
        b.addWidget(self.chart_days)
        v.addWidget(card)
        row = QHBoxLayout()
        row.setSpacing(12)
        for attr, title, hint in (("bars_source", "Who talks most", "Scripts and features that sent them."),
                                  ("bars_held", "Why he waited", "The rule that held each one."),
                                  ("bars_voice", "Voices used", "Which profile spoke.")):
            card, b = make_card(title, hint)
            bars = BarList([], "Nothing yet.")
            setattr(self, attr, bars)
            b.addWidget(bars)
            row.addWidget(card, 1)
        v.addLayout(row)

    def _page_advanced(self, v):
        card, b = make_card("Persona", "How every voice phrases things unless its card says otherwise.")
        self.in_address_style = QLineEdit()
        self.in_address_style.setMinimumWidth(160)
        self.chk_chime = ToggleSwitch()
        self.chk_ai_rewrite = ToggleSwitch()
        self.spin_pause_ms = QSpinBox()
        self.spin_pause_ms.setRange(0, 1500)
        self.spin_pause_ms.setSingleStep(50)
        self.spin_pause_ms.setSuffix(" ms")
        self.spin_max = QSpinBox()
        self.spin_max.setRange(80, 800)
        self.spin_max.setSuffix(" chars")
        b.addWidget(setting_group([
            setting_row("Default address", self.in_address_style, "Used when a voice card leaves 'Calls you' empty."),
            setting_row("Chime before speaking", self.chk_chime, "A soft two-tone cue so speech never startles you."),
            setting_row("AI phrasing", self.chk_ai_rewrite, "A fast model rewrites each line in the voice's persona."),
            setting_row("Pause between sentences", self.spin_pause_ms, "Breathing room between sentences."),
            setting_row("Longest announcement", self.spin_max, "Longer text is cut at a sentence."),
        ]))
        v.addWidget(card)

        card, b = make_card("Say something", "Send a test through the full pipeline: persona, chime, rules, Card.")
        row = QHBoxLayout()
        self.in_announce = QLineEdit()
        self.in_announce.setPlaceholderText("Test announcement text")
        self.in_announce.setMinimumHeight(34)
        row.addWidget(self.in_announce, 3)
        self.cb_tier = QComboBox()
        self.cb_tier.addItems(TIERS)
        self.cb_tier.setToolTip("critical interrupts, info waits for rules, ambient is skipped when held")
        row.addWidget(self.cb_tier)
        self.btn_announce = button("Announce", "", "volume", "Send it now")
        self.btn_announce.clicked.connect(self.send_announcement)
        row.addWidget(self.btn_announce)
        b.addLayout(row)
        v.addWidget(card)

        card, b = make_card("All Kokoro voices", "Codes read language + gender + name: a = American, b = British; "
                                                 "f = female, m = male. Use a code, or a blend like "
                                                 "am_fenrir:0.6, bm_george:0.4, in a voice card.")
        row = QHBoxLayout()
        self.cb_audition = QComboBox()
        self.cb_audition.setMinimumHeight(34)
        row.addWidget(self.cb_audition, 2)
        self.btn_audition = button("Sample", "GhostBtn", "play", "Hear this raw voice")
        self.btn_audition.clicked.connect(self.send_preview)
        row.addWidget(self.btn_audition)
        row.addStretch(1)
        b.addLayout(row)
        v.addWidget(card)

        card, b = make_card("Pronunciation", "Words he says wrong, and how to say them.")
        row = QHBoxLayout()
        self.in_pron_word = QLineEdit()
        self.in_pron_word.setPlaceholderText("Word")
        self.in_pron_spoken = QLineEdit()
        self.in_pron_spoken.setPlaceholderText("Spoken as")
        row.addWidget(self.in_pron_word, 1)
        row.addWidget(self.in_pron_spoken, 2)
        self.btn_pron_add = button("Add", "", "add", "Add a pronunciation")
        self.btn_pron_add.clicked.connect(self.add_pronunciation)
        row.addWidget(self.btn_pron_add)
        self.btn_pron_del = button("Remove", "GhostBtn", "close", "Remove the selected pronunciation")
        self.btn_pron_del.clicked.connect(self.remove_pronunciation)
        row.addWidget(self.btn_pron_del)
        b.addLayout(row)
        self.table_pron = QTableWidget(0, 2)
        self.table_pron.setHorizontalHeaderLabels(["Word", "Spoken as"])
        self._style_table(self.table_pron, stretch=1)
        self.table_pron.setMinimumHeight(160)
        b.addWidget(self.table_pron)
        v.addWidget(card)

    def select_page(self, index):
        self.stack.setCurrentIndex(index)
        for i, b in enumerate(self.page_buttons):
            b.setChecked(i == index)
        if index in (2, 3) and self._up:
            self.refresh_log()
        motion.fade_in(self.stack.currentWidget(), 200)

    @staticmethod
    def _style_table(t, stretch):
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(34)
        hh = t.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setFixedHeight(30)
        for c in range(t.columnCount()):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c == stretch
                                    else QHeaderView.ResizeMode.ResizeToContents)
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    # ------------------------------------------------------------------
    # Save bar and dirty tracking (same model as Settings)
    # ------------------------------------------------------------------
    def _build_save_bar(self):
        self.save_bar = QFrame()
        self.save_bar.setObjectName("SaveBar")
        row = QHBoxLayout(self.save_bar)
        row.setContentsMargins(16, 10, 10, 10)
        row.setSpacing(10)
        dot = QLabel()
        dot.setPixmap(glyph_icon("warning", 14, T["gold"]).pixmap(14, 14))
        row.addWidget(dot)
        self.lbl_dirty_count = QLabel()
        self.lbl_dirty_count.setObjectName("RowLabel")
        row.addWidget(self.lbl_dirty_count, 1)
        self.btn_discard = button("Discard", "GhostBtn")
        self.btn_discard.clicked.connect(self.discard)
        row.addWidget(self.btn_discard)
        self.btn_save = button("Save and apply", "PrimaryBtn", "save")
        self.btn_save.setMinimumWidth(150)
        self.btn_save.clicked.connect(self.save)
        row.addWidget(self.btn_save)
        self.save_bar.setVisible(False)
        return self.save_bar

    def _watch(self, w):
        for sig in ("toggled", "valueChanged", "textChanged", "currentIndexChanged"):
            if hasattr(w, sig) and not (sig == "textChanged" and isinstance(w, (QSpinBox, QDoubleSpinBox))):
                getattr(w, sig).connect(self._check_dirty)
                return

    def values(self) -> dict:
        active = next((pid for pid, c in self.profile_cards.items() if c.radio.isChecked()), None)
        return {
            **{f"profile.{pid}": c.value() for pid, c in self.profile_cards.items()},
            "active_profile": active,
            **{f"gate.{k}": sw.isChecked() for k, sw in self.gate_switches.items()},
            "call_gate_ignore_apps": [a.strip().lower() for a in self.in_ignore_apps.text().split(",") if a.strip()],
            "discord_hold_when": self.cb_discord.currentText(),
            "listen_reply": self.chk_listen_reply.isChecked(),
            "reply_seconds": self.spin_reply_window.value(),
            **{f"moment.{mid}": sw.isChecked() for mid, sw in self.moment_checks.items()},
            "address_style": self.in_address_style.text().strip(),
            "chime_enabled": self.chk_chime.isChecked(),
            "ai_rewrite": self.chk_ai_rewrite.isChecked(),
            "pause_ms": self.spin_pause_ms.value(),
            "max_chars": self.spin_max.value(),
        }

    def dirty_keys(self) -> list:
        now = self.values()
        return [k for k, v in now.items() if self._baseline.get(k) != v]

    def dirty_count(self) -> int:
        return len(self.dirty_keys())

    def _check_dirty(self, *_):
        if self._loading:
            return
        n = self.dirty_count()
        self.lbl_dirty_count.setText(f"{n} unsaved change{'' if n == 1 else 's'}")
        if n and not self.save_bar.isVisible():
            self.save_bar.setVisible(True)
            motion.fade_in(self.save_bar, 150)
        elif not n:
            self.save_bar.setVisible(False)
        self.dirty_changed.emit(n)

    def discard(self):
        self.refresh()
        self._toast("Changes discarded", "info")

    def save(self) -> bool:
        v, dirty = self.values(), set(self.dirty_keys())
        errors = []
        cfg = {}
        if any(k.startswith("profile.") for k in dirty) or "active_profile" in dirty:
            cfg["profiles"] = {pid: v[f"profile.{pid}"] for pid in self.profile_cards}
            if v["active_profile"]:
                cfg["active_profile"] = v["active_profile"]
        if any(k.startswith("gate.") for k in dirty):
            cfg["gates_enabled"] = {k: v[f"gate.{k}"] for k in self.gate_switches}
        for key in ("call_gate_ignore_apps", "discord_hold_when", "max_chars"):
            if key in dirty:
                cfg[key] = v[key]
        if cfg:
            ok, msg = self.controller.save_settings(cfg)
            if not ok:
                errors.append(msg)
        if dirty & {"address_style", "chime_enabled", "ai_rewrite", "pause_ms"}:
            ok, msg = self.controller.save_persona(address_style=v["address_style"] or "Sir",
                                                   chime_enabled=v["chime_enabled"], pause_ms=v["pause_ms"],
                                                   ai_rewrite=v["ai_rewrite"])
            if not ok:
                errors.append(msg)
        if dirty & {"listen_reply", "reply_seconds"}:
            ok, msg = self.controller.save_reply_settings(enabled=v["listen_reply"], timeout_seconds=v["reply_seconds"])
            if not ok:
                errors.append(msg)
        for key in sorted(k for k in dirty if k.startswith("moment.")):
            ok, msg = self.controller.set_moment(key.split(".", 1)[1], v[key])
            if not ok:
                errors.append(msg)
        if errors:
            self._set_status("Not saved: " + "; ".join(errors), T["error"])
            self._toast("Save failed: " + errors[0], "error")
            return False
        self.refresh()
        self._toast("Voice settings saved. The next announcement uses them.", "success")
        return True

    # ------------------------------------------------------------------
    # Health, loading
    # ------------------------------------------------------------------
    @property
    def controls(self):
        return (self.btn_quiet, self.btn_unmute, self.btn_clear_queue,
                self.in_address_style, self.chk_chime, self.chk_ai_rewrite, self.spin_pause_ms, self.spin_max,
                self.chk_listen_reply, self.spin_reply_window, self.in_ignore_apps, self.cb_discord,
                *self.gate_switches.values(), self.in_announce, self.cb_tier, self.btn_announce,
                self.cb_audition, self.btn_audition, self.in_pron_word, self.in_pron_spoken,
                self.btn_pron_add, self.btn_pron_del, self.btn_replay, self.btn_resend, self.btn_mark_read,
                self.btn_ack_all)

    def _set_status(self, text, color):
        self.lbl_status.setText(text)
        self._set_tone(self.lbl_status, {T["error"]: "error", T["success"]: "success", T["gold"]: "gold"}.get(color))

    @staticmethod
    def _set_tone(label, tone):
        """Colour a status label through the stylesheet's [tone] rules, not an inline stylesheet."""
        label.setProperty("tone", tone or "")
        label.style().unpolish(label)
        label.style().polish(label)

    def _toast(self, text, kind):
        if self.host is not None and hasattr(self.host, "toast"):
            self.host.toast(text, kind)

    def check_health(self):
        # The daemon can take seconds to answer while it synthesises; never wait on the GUI thread.
        run_async(self.controller.health, self._apply_health)

    def _apply_health(self, health):
        if isinstance(health, Exception):
            health = None
        was_up = self._up
        self._up = health is not None
        if not self._up:
            self._set_status("Voice daemon is down. Press Start daemon.", T["error"])
            self._set_chips(None, 0)
        else:
            self._set_status("● Voice daemon up", T["success"])
            self._set_chips(health.get("gates") or [], health.get("queued", 0))
        if was_up != self._up:  # not "or no voices": an unloaded engine has none, and that re-ran 6 sync calls every 5s
            self.refresh()
        else:
            self._set_enabled()
            self._update_quiet_display()

    def _set_chips(self, gates, held):
        """gates None = daemon down (no chips). Rebuilds only when the state changes."""
        state = (tuple(gates) if gates is not None else None, held)
        if state == self._chip_state:
            return
        self._chip_state = state
        while self.chip_row.count():
            self.chip_row.takeAt(0).widget().deleteLater()
        if gates is None:
            return
        for g in gates:
            text, tip = GATE_CHIPS.get(g, (g, "A delivery rule is holding announcements."))
            self.chip_row.addWidget(self._chip(text, "BadgeGold", tip))
        if not gates:
            self.chip_row.addWidget(self._chip("clear", "BadgeSuccess", "No rule is holding; announcements speak now."))
        self.chip_row.addWidget(self._chip(f"{held} held", "BadgeGold" if held else "Badge",
                                           "Announcements waiting for every rule to clear."))
        if hasattr(self, "btn_clear_queue"):
            self.btn_clear_queue.setText(f"Clear queue ({held})" if (held and held > 0) else "Clear queue")

    @staticmethod
    def _chip(text, kind, tip):
        lbl = QLabel(text)
        lbl.setObjectName(kind)
        lbl.setToolTip(tip)
        return lbl

    def _set_enabled(self):
        for w in self.controls:
            w.setEnabled(self._up)
        for c in self.profile_cards.values():
            for w in (*c.editors, c.btn_play):
                w.setEnabled(self._up)
        self.btn_pron_add.setEnabled(self._up and self._pron_available)
        self.btn_pron_del.setEnabled(self._up and self._pron_available)

    def start_daemon(self):
        ok, msg = self.controller.start_daemon()
        self._set_status(msg, T["success"] if ok else T["gold"])
        if ok:
            QTimer.singleShot(4000, self.check_health)

    def refresh(self):
        if not self._up:
            self._set_enabled()
            return
        run_async(self._fetch_refresh, self._apply_refresh)

    def _fetch_refresh(self):
        """Worker thread: /voices alone takes ~13s while the daemon lazy-loads Kokoro."""
        c = self.controller
        return (c.get_config() or {}, c.get_persona() or {}, c.get_voices(),
                getattr(c, "get_moments", lambda: [])(), c.load_pronunciation())

    def _apply_refresh(self, data):
        if isinstance(data, Exception):
            self._set_status(f"Could not load voice settings: {data}", T["error"])
            return
        cfg, p, self._voices, moments, pron = data
        self._loading = True
        self.cb_audition.clear()
        self.cb_audition.addItems(self._voices)
        self._fill_profiles(cfg)
        enabled = cfg.get("gates_enabled") or {}
        for k, sw in self.gate_switches.items():
            sw.setChecked(bool(enabled.get(k, True)))
        self.in_ignore_apps.setText(", ".join(cfg.get("call_gate_ignore_apps") or []))
        self.cb_discord.setCurrentText(cfg.get("discord_hold_when", "undeafened"))
        rw = p.get("reply_window", {}) if isinstance(p, dict) else {}
        self.chk_listen_reply.setChecked(bool(rw.get("enabled", False)))
        self.spin_reply_window.setValue(int(rw.get("timeout_seconds", 6)))
        self._fill_moments(moments)
        self.in_address_style.setText(p.get("address_style", "Sir"))
        self.chk_chime.setChecked(bool(p.get("chime_enabled", True)))
        self.chk_ai_rewrite.setChecked(bool(p.get("ai_rewrite", True)))
        self.spin_pause_ms.setValue(int(p.get("pause_between_sentences_ms", 350)))
        self.spin_max.setValue(int(cfg.get("max_chars", 500)))
        self._pron_available = pron is not None
        self._fill_pron((pron or {}).get("pronunciations", {}))
        self._baseline = self.values()
        self._loading = False
        self._check_dirty()
        self.refresh_log()
        self._update_quiet_display()
        self._set_enabled()

    def _fill_profiles(self, cfg):
        for c in self.profile_cards.values():
            self.profile_group.removeButton(c.radio)
            c.setParent(None)
            c.deleteLater()
        self.profile_cards = {}
        profiles = cfg.get("profiles") or {"default": {"label": "Default", "engine": "kokoro",
                                                       "voice": cfg.get("voice", "af_heart"),
                                                       "speed": cfg.get("speed", 1.0)}}
        hint = "A Kokoro code like af_nicole, or a blend like am_fenrir:0.6, bm_george:0.4"
        for pid, prof in profiles.items():
            card = ProfileCard(pid, prof, hint, self._play_profile, self.profile_group)
            card.radio.setChecked(pid == (cfg.get("active_profile") or next(iter(profiles))))
            for w in card.editors:
                self._watch(w)
            self.profiles_box.addWidget(card)
            self.profile_cards[pid] = card
        active = self.profile_cards.get(cfg.get("active_profile") or "")
        self._active_label = (active.base.get("label") if active else "Default")

    def _fill_moments(self, rules):
        while self.moments_group.count():
            item = self.moments_group.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.moment_checks = {}
        rows = []
        for m in rules:
            sw = ToggleSwitch(bool(m.get("enabled", True)))
            self._watch(sw)
            self.moment_checks[m["id"]] = sw
            rows.append(setting_row(m.get("label", m["id"]), sw,
                                    f"{m.get('tier', 'info')} priority, at most once every {m.get('cooldown_hours', 12)}h"))
        if rows:
            self.moments_group.addWidget(setting_group(rows))

    def _fill_pron(self, entries):
        self.table_pron.setRowCount(0)
        for word, spoken in sorted(entries.items()):
            r = self.table_pron.rowCount()
            self.table_pron.insertRow(r)
            self.table_pron.setItem(r, 0, QTableWidgetItem(str(word)))
            self.table_pron.setItem(r, 1, QTableWidgetItem(str(spoken)))

    def _update_quiet_display(self):
        info = self.controller.get_quiet() if hasattr(self.controller, "get_quiet") else None
        if not isinstance(info, dict):
            return
        status = info.get("status", "active")
        label = getattr(self, "_active_label", "")
        if status == "muted":
            self.lbl_quiet_state.setText("Muted")
            self._set_tone(self.lbl_quiet_state, "error")
        elif status == "snoozed":
            self.lbl_quiet_state.setText(f"Snoozed, {max(1, info.get('remaining_seconds', 0) // 60)}m left")
            self._set_tone(self.lbl_quiet_state, "gold")
        else:
            self.lbl_quiet_state.setText(f"Speaking as {label}  ·  Active" if label else "Active")
            self._set_tone(self.lbl_quiet_state, "success")
        self.btn_unmute.setVisible(status in ("muted", "snoozed"))

    # ------------------------------------------------------------------
    # History and analytics
    # ------------------------------------------------------------------
    def refresh_log(self):
        events = []
        if hasattr(self.controller, "get_events"):
            try:
                events = self.controller.get_events(limit=EVENTS_LIMIT)
            except Exception:
                events = []
        if not events and hasattr(self.controller, "load_announcements"):
            events = self.controller.load_announcements(limit=50)
        self._events = [e for e in events if e.get("type") != "reply_window_opened"]
        self._fill_log()
        self._fill_analytics()

    def _filtered_events(self):
        q = self.in_search.text().strip().lower()
        tier = self.cb_filter_tier.currentText()
        status = self.cb_filter_status.currentText()
        out = []
        for e in self._events:
            if q and q not in f"{e.get('text', '')} {e.get('spoken_text', '')} {e.get('headline', '')}".lower():
                continue
            if tier in TIERS and e.get("tier") != tier:
                continue
            st = e.get("status", "")
            if status == "Unread" and e.get("acknowledged"):
                continue
            if status == "Spoken" and st not in ("spoken", "replied"):
                continue
            if status == "Held or waiting" and not (e.get("gates") or st in ("queued", "requeued")):
                continue
            if status == "Dropped" and st != "dropped":
                continue
            if status == "Replied" and not e.get("reply"):
                continue
            out.append(e)
        return out

    def _fill_log(self, *_):
        rows = self._filtered_events()[:300]
        self.table_log.setRowCount(0)
        for e in rows:
            r = self.table_log.rowCount()
            self.table_log.insertRow(r)
            ts = str(e.get("timestamp", ""))
            when = ts[11:16] if ts[:10] == date.today().isoformat() else ts[5:10] + " " + ts[11:16]
            cells = [when, str(e.get("tier", "")).upper(), e.get("source", ""),
                     str(e.get("headline") or e.get("text", "")), lifecycle(e), e.get("profile", "")]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, e.get("id"))
                if c == 3 and not e.get("acknowledged"):
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.table_log.setItem(r, c, item)
        self.lbl_log_count.setText(f"{len(rows)} of {len(self._events)}")

    def _selected_event(self):
        row = self.table_log.currentRow()
        if row < 0:
            return None
        eid = self.table_log.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((e for e in self._events if e.get("id") == eid), None)

    def _show_detail(self):
        e = self._selected_event()
        if not e:
            return
        parts = [f"<b>Sent:</b> {e.get('text', '')}"]
        if e.get("spoken_text") and e.get("spoken_text") != e.get("text"):
            parts.append(f"<b>Said:</b> {e['spoken_text']}")
        if e.get("suggested_step"):
            parts.append(f"<b>Suggested:</b> {e['suggested_step']}")
        meta = [x for x in (e.get("source") and f"from {e['source']}", e.get("engine"),
                            e.get("audio_s") and f"{e['audio_s']}s of audio") if x]
        if meta:
            parts.append(" · ".join(meta))
        self.lbl_detail.setText("<br>".join(parts))

    def replay_selected(self):
        e = self._selected_event()
        if e:
            ok, msg = self.controller.replay_event(e["id"])
            self._set_status(msg, T["success"] if ok else T["error"])

    def resend_selected(self):
        e = self._selected_event()
        if e:
            ok, msg = self.controller.resend_event(e["id"])
            self._set_status(msg, T["success"] if ok else T["error"])
            QTimer.singleShot(600, self.refresh_log)

    def mark_selected_read(self):
        e = self._selected_event()
        if e:
            self.controller.ack_event(event_id=e["id"])
            self.refresh_log()

    def ack_all_events(self):
        if hasattr(self.controller, "ack_event"):
            self.controller.ack_event(ack_all=True)
            self.refresh_log()

    def _fill_analytics(self):
        today = date.today()
        week = {(today - timedelta(days=i)).isoformat() for i in range(7)}
        month = today.isoformat()[:7]
        per_day = collections.Counter()
        sources, held, voices = collections.Counter(), collections.Counter(), collections.Counter()
        spoken_s = fish = asked = answered = 0
        for e in self._events:
            d = str(e.get("timestamp", ""))[:10]
            if d[:7] == month:
                fish += int(e.get("fish_chars") or 0)
            if d not in week:
                continue
            per_day[d] += 1
            sources[e.get("source") or "unknown"] += 1
            for g in e.get("gates") or []:
                held[g] += 1
            if e.get("profile"):
                voices[e["profile"]] += 1
            spoken_s += float(e.get("audio_s") or 0)
            if e.get("expects_reply") or e.get("reply"):
                asked += 1
                answered += bool(e.get("reply"))
        self.kpis["today"][0].setText(str(per_day[today.isoformat()]))
        self.kpis["week"][0].setText(str(sum(per_day.values())))
        self.kpis["minutes"][0].setText(f"{spoken_s / 60:.1f}")
        self.kpis["held"][0].setText(str(sum(held.values())))
        self.kpis["replies"][0].setText(f"{answered * 100 // asked}%" if asked else "-")
        self.kpis["fish"][0].setText(f"{fish:,}")
        self.kpis["fish"][1].setText(f"free until {FISH_FREE_UNTIL}, then about ${fish * 15 / 1e6:.2f} at this pace")
        self.chart_days.set_days([((today - timedelta(days=6 - i)).isoformat(),
                                   per_day[(today - timedelta(days=6 - i)).isoformat()]) for i in range(7)])
        self.bars_source.set_rows(sources.most_common(6))
        self.bars_held.set_rows(held.most_common(6))
        self.bars_voice.set_rows(voices.most_common(6))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _play_profile(self, card):
        prof = card.value()
        addr = prof.get("address_style") or self.in_address_style.text().strip() or "Sir"
        card.btn_play.setEnabled(False)
        self._set_status(f"Playing {prof.get('label', card.pid)} preview...", T["gold"])

        def _call():
            return self.controller.preview_profile(
                f"{addr}, this is how I sound. This is the voice you will hear for alerts.", prof)

        def _done(res):
            card.btn_play.setEnabled(True)
            if isinstance(res, Exception):
                self._set_status(f"Preview error: {res}", T["error"])
                return
            ok, msg = res
            self._set_status(f"{prof.get('label', card.pid)}: {msg}", T["success"] if ok else T["error"])

        run_async(_call, _done)

    def clear_queue_action(self):
        if not hasattr(self.controller, "clear_queue"):
            return
        self.btn_clear_queue.setEnabled(False)

        def _call():
            return self.controller.clear_queue()

        def _done(res):
            self.btn_clear_queue.setEnabled(True)
            if isinstance(res, Exception):
                self._set_status(f"Clear queue error: {res}", T["error"])
                return
            ok, cleared = res
            self._set_status(f"Cleared {cleared} queued notification(s).", T["success"] if ok else T["error"])
            self.check_health()
            self.refresh_log()

        run_async(_call, _done)

    def skip_queue_action(self):
        if not hasattr(self.controller, "skip_queued"):
            return

        def _call():
            return self.controller.skip_queued()

        def _done(res):
            if isinstance(res, Exception):
                return
            ok, body = res
            if ok and isinstance(body, dict):
                rem = body.get("remaining", 0)
                self._set_status(f"Skipped alert. {rem} remaining.", T["success"])
            self.check_health()
            self.refresh_log()

        run_async(_call, _done)

    def snooze(self, minutes: int):
        ok, msg = self.controller.snooze(minutes)
        self._set_status(msg, T["success"] if ok else T["error"])
        self._update_quiet_display()

    def mute_voice(self):
        ok, msg = self.controller.mute()
        self._set_status(msg, T["success"] if ok else T["error"])
        self._update_quiet_display()

    def mute_voice_indefinite(self):
        ok, msg = self.controller.mute_indefinite()
        self._set_status(msg, T["success"] if ok else T["error"])
        self._update_quiet_display()

    def unmute_voice(self):
        ok, msg = self.controller.unmute()
        self._set_status(msg, T["success"] if ok else T["error"])
        self._update_quiet_display()

    def send_announcement(self):
        text = self.in_announce.text().strip()
        if not text:
            return
        ok, msg = self.controller.announce(text, self.cb_tier.currentText())
        self._set_status(f"Announcement accepted ({self.cb_tier.currentText()})." if ok else msg,
                         T["success"] if ok else T["error"])
        if ok:
            self.in_announce.clear()
            self.refresh_log()

    def send_preview(self):
        voice = self.cb_audition.currentText()
        if voice:
            self.btn_audition.setEnabled(False)
            self._set_status(f"Sampling voice {voice}...", T["gold"])

            def _call():
                return self.controller.preview("This is a sample of the selected voice.", voice=voice)

            def _done(res):
                self.btn_audition.setEnabled(True)
                if isinstance(res, Exception):
                    self._set_status(f"Sample error: {res}", T["error"])
                    return
                ok, msg = res
                self._set_status(f"Sample: {msg}", T["success"] if ok else T["error"])

            run_async(_call, _done)

    def add_pronunciation(self):
        ok, msg = self.controller.save_pronunciation(self.in_pron_word.text(), self.in_pron_spoken.text())
        self._set_status(msg if isinstance(msg, str) else "Added.", T["success"] if ok else T["error"])
        if ok:
            self.in_pron_word.clear()
            self.in_pron_spoken.clear()
            self._fill_pron((self.controller.load_pronunciation() or {}).get("pronunciations", {}))

    def remove_pronunciation(self):
        row = self.table_pron.currentRow()
        if row < 0:
            return
        ok, msg = self.controller.delete_pronunciation(self.table_pron.item(row, 0).text())
        self._set_status(msg, T["success"] if ok else T["error"])
        if ok:
            self._fill_pron((self.controller.load_pronunciation() or {}).get("pronunciations", {}))
