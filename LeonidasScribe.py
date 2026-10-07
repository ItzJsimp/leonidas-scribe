"""
Leonidas Scribe - Native Voice Dictation Application for Windows
"""
import sys
import os
import subprocess
import json
import time
import tempfile
import threading
import traceback
import collections
import statistics
import ctypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Set explicit Windows AppUserModelID so Explorer groups taskbar windows
# into the pinned Leonidas Scribe shortcut instead of showing a generic Python icon.
APP_USER_MODEL_ID = "Leonidas.Scribe.VoiceDictation.1.0"
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
except Exception:
    pass

# Per-monitor DPI awareness, requested before Qt touches the GUI subsystem.
# Qt6 is per-monitor aware by default, but that only holds if nothing upstream
# (pythonw.exe's own manifest) already claimed a coarser awareness level first.
# Desktop and the AORUS 16X laptop run at different scale factors, so a stale
# system-DPI claim here would render blurry when the window moves screens.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    pass

# File logging for pythonw background execution with test isolation guard
def _is_test_environment() -> bool:
    if "pytest" in sys.modules or "unittest" in sys.modules:
        return True
    if any("test" in arg.lower() or "pytest" in arg.lower() for arg in sys.argv):
        return True
    return False

def _get_log_file() -> str:
    override = os.environ.get("LEONIDAS_SCRIBE_LOG")
    if override:
        return override
    if _is_test_environment():
        return os.path.join(tempfile.gettempdir(), "leonidas-scribe-test.log")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "leonidas-scribe.log")

LOG_FILE = _get_log_file()

class _LogWriter:
    def __init__(self, original_stream):
        self.orig = original_stream

    def write(self, text):
        if text:
            try:
                target_log = _get_log_file()
                with open(target_log, "a", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass
        if self.orig and hasattr(self.orig, "write"):
            try:
                self.orig.write(text)
            except Exception:
                pass

    def flush(self):
        if self.orig and hasattr(self.orig, "flush"):
            try:
                self.orig.flush()
            except Exception:
                pass

sys.stdout = _LogWriter(sys.stdout)
sys.stderr = _LogWriter(sys.stderr)

# pythonw has no console, so a crash before the event loop (a broken import, a
# half-applied edit from another session) used to exit silently: the taskbar
# click "did nothing". Until the loop runs, any uncaught error also gets a box.
_event_loop_running = False


def _startup_excepthook(exc_type, exc, tb):
    traceback.print_exception(exc_type, exc, tb)  # to leonidas-scribe.log
    if not _event_loop_running:
        user32.MessageBoxW(None, f"Leonidas Scribe could not start:\n\n{exc_type.__name__}: {exc}\n\n"
                           f"Full traceback: {LOG_FILE}", "Leonidas Scribe", 0x10 | 0x40000)  # MB_ICONERROR | MB_TOPMOST


if __name__ == "__main__":
    sys.excepthook = _startup_excepthook

ERROR_ALREADY_EXISTS = 183
SINGLE_INSTANCE_MUTEX = "Local\\LeonidasScribe_SingleInstance_Mutex"
SHOW_EVENT_NAME = "Local\\LeonidasScribe_ShowDashboard_Event"

_instance_mutex = None

def acquire_single_instance() -> bool:
    global _instance_mutex
    _instance_mutex = kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        if _instance_mutex:
            kernel32.CloseHandle(_instance_mutex)
            _instance_mutex = None
        return False
    return True

def release_single_instance():
    """Closes the mutex handle so a re-exec'd process can acquire it immediately.
    Windows would also release it on process exit, but that race is what a
    fast re-exec (see _restart_for_update) cannot afford to lose."""
    global _instance_mutex
    if _instance_mutex:
        kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject, Signal, QTimer

from engine import AudioRecorder, DictationEngine, load_config_file

def engine_strip(text: str) -> str:
    return DictationEngine.strip_pause_markers(text or "")
from clipboard import ClipboardInjector
from context_detector import get_active_window_info, is_game_foreground
from controllers.voice_controller import VoiceController
from pathlib import Path
import hotkeys
import stitch
import edit_learner
from hotkeys import HotkeyManager
from hud_qt import FloatingHUDWidget, QtFloatingHUD, AudioFeedback
from card_qt import LeonidasCardWidget
from live_transcriber import LiveTranscriber
from ui import labels as ui_labels, load_brand_fonts, motion, get_app_icon
from ui_dashboard import DashboardWindow
from ui.preset_picker import PresetPickerWidget

import faulthandler
try:
    _crash_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")
    _crash_file = open(_crash_log_path, "a", encoding="utf-8")
    faulthandler.enable(file=_crash_file)
except Exception:
    pass

class ScribeBridge(QObject):
    sig_start_recording = Signal(str)
    sig_stop_recording = Signal()
    sig_cancel_recording = Signal()
    sig_history_updated = Signal()
    sig_show_dashboard = Signal()
    sig_show_preset_picker = Signal()
    sig_alert = Signal(str, str)  # title, message: shown as a Windows notification
    sig_voice_reply_received = Signal(str)
    # HUD state mutates self.particles and calls show()/update(), which are GUI-thread-only
    # (PySide6 access violation if hit from a background thread). Background threads
    # (_process_and_inject, _handle_preset_transform) must marshal HUD calls through this
    # signal instead of calling self.hud.<method>() directly.
    sig_hud_call = Signal(str, tuple)
    # A source .py changed and Scribe has been idle+quiet for the debounce window:
    # re-exec on the main thread instead of an agent Stop-Process'ing a live instance.
    sig_restart_for_update = Signal()


class LeonidasScribeApp:
    # Voice daemon do-not-talk gate (ADR-0059 slice 2): the flag exists only while recording,
    # since the always-open mic stream hides real recording from Windows' mic-in-use registry.
    RECORDING_FLAG = os.path.join(os.environ.get("TEMP", "."), "leonidas-scribe-recording.flag")

    @property
    def is_recording(self):
        return self._is_recording

    @is_recording.setter
    def is_recording(self, value):
        self._is_recording = value
        try:
            if value:
                open(self.RECORDING_FLAG, "w").close()
            elif os.path.exists(self.RECORDING_FLAG):
                os.remove(self.RECORDING_FLAG)
        except OSError:
            pass  # the gate is a courtesy; never break dictation over it

    def __init__(self):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = os.path.join(self.base_dir, "config.json")
        self.dictionary_path = os.path.join(self.base_dir, "dictionary.json")
        self.history_path = os.path.join(self.base_dir, "history.jsonl")

        self.load_config()

        # Qt Application
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setWindowIcon(get_app_icon())
        load_brand_fonts()  # before any widget: the HUD and preset picker paint with them
        ui_cfg = self.config.get("ui", {})
        motion.set_calm(bool(ui_cfg.get("calm") or ui_cfg.get("reduced_motion")))

        # Core Engines
        self.recorder = AudioRecorder(
            sample_rate=self.config.get("audio", {}).get("sample_rate", 16000),
            channels=self.config.get("audio", {}).get("channels", 1),
            device_index=self.config.get("audio", {}).get("device_index")
        )
        self.engine = DictationEngine(self.config_path, self.dictionary_path)
        self.clipboard = ClipboardInjector()

        # Audio Feedback
        audio_cfg = self.config.get("audio", {})
        self.audio_feedback = AudioFeedback(enabled=audio_cfg.get("sound_feedback", True),
                                            volume=int(audio_cfg.get("earcon_volume", 100)))

        # Qt HUD Pill Widget
        hud_enabled = self.config.get("hud", {}).get("enabled", True)
        self.hud = QtFloatingHUD(enabled=hud_enabled)
        self.hud_widget = FloatingHUDWidget()
        self.hud_widget.set_level_provider(self.recorder.get_current_level)
        saved_coords = self.config.get("hud", {}).get("custom_coords")
        if saved_coords and len(saved_coords) == 2:
            self.hud_widget.custom_coords = (int(saved_coords[0]), int(saved_coords[1]))
        self.hud.bind_widget(self.hud_widget)

        # Live transcription while you talk (live_transcriber.py): the same Groq
        # Whisper model on each pause-bounded chunk. Feeds the pill's transcript panel
        # and lets the final text skip re-transcribing everything already done.
        # Its own engine instance, so chunk calls never overwrite the main engine's
        # per-take diagnostics.
        self.live_engine = DictationEngine(self.config_path, self.dictionary_path)
        self.live = LiveTranscriber(
            lambda wav, end: self.live_engine.transcribe(wav, speech_end_s=end),
            AudioRecorder(sample_rate=self.recorder.sample_rate, channels=self.recorder.channels),
            sample_rate=self.recorder.sample_rate,
            on_text=self.hud.update_live_transcript,
        )
        self.recorder.streaming_tap_cb = self.live.push
        self._apply_hud_config()

        # Bridge for Thread-Safe Qt Signals
        self.bridge = ScribeBridge()
        self.bridge.sig_start_recording.connect(self._handle_start_recording)
        self.bridge.sig_stop_recording.connect(self._handle_stop_recording)
        self.bridge.sig_cancel_recording.connect(self._handle_cancel_recording)
        self.bridge.sig_hud_call.connect(self._dispatch_hud_call)
        if hasattr(self.hud, "set_merge_toggle_callback"):
            self.hud.set_merge_toggle_callback(self._on_merge_toggle)

        # Dashboard Window
        self.dashboard = DashboardWindow(
            config_path=self.config_path,
            dictionary_path=self.dictionary_path,
            history_path=self.history_path,
            on_toggle_dictation_cb=self.toggle_recording,
            on_settings_saved_cb=self._on_settings_saved,
            state_provider=self._engine_state,
        )
        self.bridge.sig_history_updated.connect(self.dashboard.refresh_history)
        self.bridge.sig_alert.connect(self.dashboard.notify)
        self._latency_window = collections.deque(maxlen=20)
        self._last_latency_alert = 0.0
        self._seed_latency_window()

        # State
        max_sec = self.config.get("audio", {}).get("max_record_seconds", 300)
        hk_cfg = self.config.get("hotkeys", {})
        self.is_recording = False
        self.is_processing = False
        self.is_transforming = False
        self.current_mode = "ptt"
        self.target_hwnd = None
        self.active_lens = "prose"
        self.active_app_name = ""
        self.selected_text = ""
        self.last_injected_text = ""
        self.last_injected_time = 0.0
        self.last_target_hwnd = None
        self.target_title = ""
        self.last_target_title = ""
        self.merge_cancelled = False  # set by the HUD's one-tap "joins" cancel
        stitch.warm()  # background load of per-app stitch windows from history.jsonl

        # Hotkey Manager
        self.hotkeys = HotkeyManager(
            on_ptt_start=lambda: self.bridge.sig_start_recording.emit("ptt"),
            on_ptt_stop=lambda: self.bridge.sig_stop_recording.emit(),
            on_toggle_trigger=self.toggle_recording,
            on_capture_trigger=lambda: self.toggle_recording(mode="capture"),
            on_cancel=lambda: self.bridge.sig_cancel_recording.emit(),
            on_transform_trigger=lambda: self.bridge.sig_show_preset_picker.emit(),
            on_quiet_toggle_trigger=self._toggle_voice_quiet,
            is_active_cb=lambda: self.is_recording,
            is_gaming_mode_cb=self._is_gaming_mode_active,
            max_toggle_seconds=max_sec,
            toggle_button=hk_cfg.get("toggle_button", "Mouse5"),
            secondary_toggle_button=hk_cfg.get("secondary_toggle_button", "Ctrl + Shift + Space"),
            capture_button=hk_cfg.get("capture_button", "Ctrl + Shift + C"),
            ptt_chord=hk_cfg.get("ptt_shortcut", "Left Ctrl + Left Win"),
            transform_button=hk_cfg.get("transform_shortcut", "Ctrl + Shift + R"),
            quiet_toggle_button=hk_cfg.get("quiet_toggle_shortcut", "Ctrl + Shift + Q")
        )

        # Preset Picker Overlay (lazy init on first transform trigger)
        presets = self.config.get("presets", [])
        self.preset_picker = PresetPickerWidget(presets)
        self.preset_picker.preset_selected.connect(self._on_preset_selected)
        self.preset_picker.voice_instruction_requested.connect(self._start_voice_refactor)
        self.bridge.sig_show_preset_picker.connect(self._show_preset_picker)

        # Leonidas Card Overlay for voice announcements and interactive reply loop
        self.card = LeonidasCardWidget()
        self.card.sig_action_triggered.connect(self._on_card_action_or_reply)
        self.card.sig_reply_submitted.connect(self._on_card_action_or_reply)
        self.bridge.sig_voice_reply_received.connect(self._on_voice_reply_received)
        self._last_announcement_id = None

        self.bridge.sig_show_dashboard.connect(self.dashboard.show_dashboard)
        self.app.aboutToQuit.connect(self._on_about_to_quit)
        self._start_show_request_watcher()
        self.bridge.sig_restart_for_update.connect(self._restart_for_update)
        self._start_source_watch()

        # Git Auto-Updater: checks origin/main silently in background for standalone client installs
        try:
            has_local_git = os.path.isdir(os.path.join(self.base_dir, ".git"))
            from updater import GitUpdater
            update_cfg = self.config.get("updates", {})
            self.updater = GitUpdater(
                repo_dir=self.base_dir,
                check_interval_s=int(update_cfg.get("check_interval_seconds", 60)),
                on_update_applied=lambda: self.bridge.sig_restart_for_update.emit() if not (self.is_recording or self.is_transforming or self.is_processing) else None,
                enabled=bool(update_cfg.get("auto_update", True)) and has_local_git,
            )
        except Exception as e:
            print(f"[LeonidasScribe] GitUpdater init warning: {e}")
            self.updater = None


        # Weekly voice profile: built by Scribe itself when the newest is 7+ days old.
        # Checked 90s after launch, then every 6 hours; no scheduled task to maintain.
        self._profile_timer = QTimer()
        self._profile_timer.timeout.connect(self._maybe_build_profile)
        QTimer.singleShot(90_000, self._maybe_build_profile)
        self._profile_timer.start(6 * 3600 * 1000)

        # Voice daemon hands-free reply window hook (Step 4 / ADR-0059)
        self._last_reply_window_id = None
        self._active_reply_data = None
        self._reply_stop_timer = QTimer()
        self._reply_stop_timer.setSingleShot(True)
        self._reply_stop_timer.timeout.connect(self._on_reply_timeout)
        self._reply_poll_timer = QTimer()
        self._reply_poll_timer.timeout.connect(self._poll_reply_window)
        self._reply_poll_timer.start(1000)

    def _on_card_action_or_reply(self, *args):
        """User responded via Card: stop voice listening if reply window is open."""
        if self.is_recording and self.current_mode == "reply":
            self.bridge.sig_stop_recording.emit()
        self._reply_stop_timer.stop()

    def _on_voice_reply_received(self, text: str):
        """Dispatched strictly on Qt main thread via signal."""
        if getattr(self, "card", None):
            self.card.show_voice_reply_received(text)

    def _poll_reply_window(self):
        """Monitor %TEMP% for active announcements and reply windows."""
        # Touch heartbeat flag at most once every 5 seconds so voice daemon knows Scribe is running
        now_t = time.time()
        if now_t - getattr(self, "_last_heartbeat_touch", 0.0) >= 5.0:
            self._last_heartbeat_touch = now_t
            try:
                Path(os.path.join(tempfile.gettempdir(), "leonidas-scribe-heartbeat.flag")).touch()
            except Exception:
                pass

        # Check for active announcement for Leonidas Card
        ann_path = os.path.join(tempfile.gettempdir(), "leonidas-active-announcement.json")
        if os.path.exists(ann_path):
            try:
                with open(ann_path, "r", encoding="utf-8") as af:
                    ann_data = json.load(af)
                if isinstance(ann_data, dict):
                    ann_id = ann_data.get("id")
                    if ann_id and ann_id != self._last_announcement_id:
                        self._last_announcement_id = ann_id
                        if getattr(self, "card", None):
                            self.card.show_announcement(ann_data)
            except Exception:
                pass

        if not self.config.get("voice", {}).get("listen_for_reply", True):
            return
        if self.is_recording or self.is_processing:
            return
        flag_path = os.path.join(tempfile.gettempdir(), "leonidas-reply-window.json")
        if not os.path.exists(flag_path):
            return
        try:
            with open(flag_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if not isinstance(data, dict) or data.get("status") != "open":
            return
        rw_id = data.get("id")
        if not rw_id or rw_id == self._last_reply_window_id:
            return
        now = time.time()
        expires_at = float(data.get("expires_at", 0))
        if now >= expires_at:
            return

        self._last_reply_window_id = rw_id
        self._active_reply_data = data
        duration_s = max(1.5, expires_at - now)
        print(f"[LeonidasScribe] Voice question reply window detected ({rw_id}, {duration_s:.1f}s left). Opening mic...")
        self.bridge.sig_start_recording.emit("reply")
        self._reply_stop_timer.start(int(duration_s * 1000))

    def _on_reply_timeout(self):
        """Automatically close the microphone when the hands-free reply window expires."""
        if self.is_recording and self.current_mode == "reply":
            print("[LeonidasScribe] Hands-free reply window timed out. Stopping recording...")
            self.bridge.sig_stop_recording.emit()

    def _engine_state(self) -> dict:
        """What the dashboard's status block shows, read live."""
        if self.is_recording:
            return {"state": "listening", "since": getattr(self, "_recording_started_at", time.perf_counter()),
                    "app": self.active_app_name}
        if self.is_processing:
            return {"state": "processing", "app": self.active_app_name}
        if self.recorder.stream is None:
            return {"state": "mic_error"}
        return {"state": "ready"}

    def _maybe_build_profile(self):
        import voice_profile
        if not voice_profile.is_due():
            return

        def work():
            try:
                path = voice_profile.build(self.history_path, self.dictionary_path, engine=self.engine)
                print(f"[LeonidasScribe] Weekly voice profile built: {path}")
                self.bridge.sig_history_updated.emit()
                self.bridge.sig_alert.emit("Your weekly voice profile is ready",
                                           "Open Scribe, Insights, to see what you worked on and how you speak.")
            except Exception as exc:
                print(f"[LeonidasScribe] Voice profile failed: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def _is_gaming_mode_active(self) -> bool:
        gaming_cfg = self.config.get("gaming_mode", {})
        if not gaming_cfg.get("enabled", True):
            return False
        if not gaming_cfg.get("suppress_mouse_buttons", True):
            return False
        custom_games = gaming_cfg.get("custom_game_processes", [])
        return is_game_foreground(custom_games=custom_games)

    def _toggle_voice_quiet(self):
        """Hook thread callback for the quiet-toggle hotkey. Pure HTTP call to the
        voice daemon, so it never touches Qt directly; the toast goes through
        sig_hud_call like every other background-thread HUD update."""
        ok, msg = VoiceController().toggle_quiet()
        self.bridge.sig_hud_call.emit("show_success" if ok else "show_error", (msg,))

    def _on_about_to_quit(self):
        if hasattr(self, "card") and self.card:
            self.card.hide_card()
        hb_path = os.path.join(tempfile.gettempdir(), "leonidas-scribe-heartbeat.flag")
        try:
            if os.path.exists(hb_path):
                os.remove(hb_path)
        except Exception:
            pass
        if hasattr(self, "dashboard"):
            self.dashboard.is_really_quitting = True
            if hasattr(self.dashboard, "tray_icon"):
                self.dashboard.tray_icon.hide()
        if hasattr(self, "hotkeys"):
            self.hotkeys.stop()
        if hasattr(self, "recorder"):
            self.recorder.close()

    def _apply_hud_config(self):
        hud_cfg = self.config.get("hud", {})
        self.hud_widget.position = hud_cfg.get("position", "bottom-center")
        self.hud_widget.transcript_mode = hud_cfg.get("live_transcript", "hover")

    def _live_enabled(self) -> bool:
        return bool(self.config.get("transcription", {}).get("live_chunks", True))

    def _on_settings_saved(self):
        self.load_config()
        self.engine.load_config()
        self.live_engine.load_config()
        self._apply_hud_config()
        # Earcons, HUD and Calm apply live: these used to need a restart.
        audio_cfg = self.config.get("audio", {})
        self.audio_feedback.enabled = audio_cfg.get("sound_feedback", True)
        self.audio_feedback.volume = int(audio_cfg.get("earcon_volume", 100))
        self.hud.enabled = self.config.get("hud", {}).get("enabled", True)
        ui_cfg = self.config.get("ui", {})
        motion.set_calm(bool(ui_cfg.get("calm") or ui_cfg.get("reduced_motion")))
        # Update audio input device live if changed
        mic_dev = self.config.get("audio", {}).get("device_index")
        if mic_dev != self.recorder.device_index:
            self.recorder.device_index = mic_dev
            if not self.is_recording:
                try:
                    self.recorder.open()
                except Exception as e:
                    print(f"[LeonidasScribe] Mic reopen failed, will retry on next press: {e}")

        hk_cfg = self.config.get("hotkeys", {})
        self.hotkeys.update_bindings(
            toggle_button=hk_cfg.get("toggle_button", "Mouse5"),
            secondary_toggle_button=hk_cfg.get("secondary_toggle_button", "Ctrl + Shift + Space"),
            capture_button=hk_cfg.get("capture_button", "Ctrl + Shift + C"),
            ptt_chord=hk_cfg.get("ptt_shortcut", "Left Ctrl + Left Win"),
            transform_button=hk_cfg.get("transform_shortcut", "Ctrl + Shift + R"),
            quiet_toggle_button=hk_cfg.get("quiet_toggle_shortcut", "Ctrl + Shift + Q"),
            is_gaming_mode_cb=self._is_gaming_mode_active
        )
        # Rebuild preset picker with updated presets
        presets = self.config.get("presets", [])
        self.preset_picker.presets = presets
        self.preset_picker._build_ui()
        self.preset_picker.adjustSize()
        print("[LeonidasScribe] Configuration, hotkeys, audio device, and transform presets live-reloaded successfully.")

    def _dispatch_hud_call(self, method: str, args: tuple):
        """Runs on the main Qt thread via sig_hud_call. Background threads must emit
        that signal instead of calling self.hud.<method>() directly (see crash.log:
        access violation from HUD paint state mutated off the GUI thread)."""
        getattr(self.hud, method)(*args)

    def _show_preset_picker(self):
        """Called on main Qt thread via signal. Grabs selected text then shows preset picker."""
        if self.is_transforming:
            print("[LeonidasScribe] Transform: already transforming, ignoring.")
            return
        try:
            sel = self.clipboard.get_selected_text_fast()
        except Exception as e:
            print(f"[LeonidasScribe] Transform: selection grab failed: {e}")
            return
        if not sel or len(sel.strip()) <= 1:
            print("[LeonidasScribe] Transform: no selection detected. Select text first.")
            self.hud.show_error("No selection")
            return
        # Cache target window and selection before picker opens
        win_info = get_active_window_info()
        self.target_hwnd = win_info.get("hwnd")
        self.active_lens = win_info.get("lens", "prose")
        self.active_app_name = win_info.get("process_name", "")
        self.is_agent_target = win_info.get("is_agent", False)
        self.selected_text = sel
        print(f"[LeonidasScribe] Transform: captured {len(sel)} chars. Showing preset picker.")
        self.preset_picker.show_at_pill_position()

    def _start_voice_refactor(self):
        """Picker Space/0: record a spoken instruction for the selection _show_preset_picker
        cached. Stop with the usual toggle (Mouse5) or cancel with Escape."""
        if not self.selected_text:
            return
        self.bridge.sig_start_recording.emit("refactor")

    def _on_preset_selected(self, key: str):
        """Called when user presses a preset number key in the picker."""
        selected = self.selected_text
        target_hwnd = self.target_hwnd
        if not selected:
            print("[LeonidasScribe] Transform: no cached selection when preset selected.")
            return
        preset_name = next(
            (p.get("name", f"Preset {key}") for p in self.config.get("presets", [])
             if str(p.get("key", "")) == str(key)),
            f"Preset {key}"
        )
        self.is_transforming = True
        self.hud.show_transforming(preset_name, "Refactoring...")
        threading.Thread(
            target=self._handle_preset_transform,
            args=(selected, key, target_hwnd, preset_name),
            daemon=True
        ).start()

    def _handle_preset_transform(self, selected_text: str, preset_key: str, target_hwnd, preset_name: str = "Transforming"):
        """Background thread: apply preset rewrite and paste result."""
        t0 = time.perf_counter()
        try:
            print(f"[LeonidasScribe] Transform: applying preset '{preset_name}' to {len(selected_text)} chars.")
            transformed = self.engine.refactor_selection(selected_text, instruction="", preset_key=preset_key)
            total_ms = int((time.perf_counter() - t0) * 1000)

            if transformed and transformed != selected_text:
                self.clipboard.paste_text(transformed, restore_original=True, target_hwnd=target_hwnd)
                self._log_history(
                    selected_text,
                    transformed,
                    {
                        "total_ms": total_ms, "stt_ms": 0, "polish_ms": total_ms, "audio_seconds": 0.0,
                        "polish_model": getattr(self.engine, "last_transform_model", None)
                    },
                    "preset_transform",
                    preset=preset_name,
                    preset_key=preset_key
                )
                print(f"[LeonidasScribe] Transform: done in {total_ms}ms.")
                self.audio_feedback.play_success()
                if self.is_recording:
                    self.bridge.sig_hud_call.emit("show_listening", ())
                else:
                    self.bridge.sig_hud_call.emit("show_success", ("Transformed",))
            else:
                print("[LeonidasScribe] Transform: result unchanged or empty. No paste.")
                if self.is_recording:
                    self.bridge.sig_hud_call.emit("show_listening", ())
                else:
                    self.bridge.sig_hud_call.emit("show_error", ("No change",))
        except Exception as e:
            print(f"[LeonidasScribe] Transform error: {e}")
            self.audio_feedback.play_error()
            err_msg = self._short_error(e)
            self.bridge.sig_alert.emit("Transform Failed", f"Preset '{preset_name}' failed: {err_msg}")
            if self.is_recording:
                self.bridge.sig_hud_call.emit("show_listening", ())
            else:
                self.bridge.sig_hud_call.emit("show_error", (f"Transform: {err_msg}",))
        finally:
            self.is_transforming = False
            self.selected_text = ""

    def _start_show_request_watcher(self):
        def _watch():
            h_event = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
            if not h_event:
                return
            while True:
                res = kernel32.WaitForSingleObject(h_event, 500)
                if res == 0:
                    self.bridge.sig_show_dashboard.emit()
        threading.Thread(target=_watch, daemon=True).start()

    # Directories under base_dir that never hold source an edit should hot-reload for.
    _SOURCE_WATCH_SKIP_DIRS = {
        "__pycache__", "build", "dist", ".pytest_cache", ".git", ".uishots",
        "recordings", "models", "sounds",
    }

    @classmethod
    def _source_mtimes(cls, root: str) -> dict:
        """mtime of every .py file under root, so any edit (not just the file an
        agent thinks it touched) is caught by one snapshot diff."""
        mtimes = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in cls._SOURCE_WATCH_SKIP_DIRS]
            for name in filenames:
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    try:
                        mtimes[path] = os.path.getmtime(path)
                    except OSError:
                        pass
        return mtimes

    def _start_source_watch(self):
        """Agents edit Scribe's own source while it runs. Instead of an agent
        Stop-Process'ing a live instance (killing mid-dictation, or the edit never
        loading), Scribe watches its own *.py mtimes and re-execs itself once it has
        been idle and the source has stopped changing for a few seconds. See
        agent-playbook.md section 4: agents just save files, never kill Scribe."""
        def _watch():
            baseline = self._source_mtimes(self.base_dir)
            stable_since = None
            while True:
                time.sleep(3.0)
                current = self._source_mtimes(self.base_dir)
                if current != baseline:
                    baseline = current
                    stable_since = time.monotonic()
                    continue
                if stable_since is None:
                    continue
                if time.monotonic() - stable_since < 3.0:
                    continue
                if self.is_recording or self.is_transforming or self.is_processing:
                    continue  # still pending; keep polling until idle
                print("[LeonidasScribe] Source changed and idle: restarting to load it.")
                self.bridge.sig_restart_for_update.emit()
                return
        threading.Thread(target=_watch, daemon=True).start()

    def _restart_for_update(self):
        """Runs on the main Qt thread via sig_restart_for_update. Re-execs with the
        same argv (so --hidden survives) instead of a fresh process an agent has to
        remember to relaunch."""
        print("[LeonidasScribe] Restarting to pick up a source change...")
        try:
            self._on_about_to_quit()
        except Exception as e:
            print(f"[LeonidasScribe] Cleanup before restart failed (continuing): {e}")
        release_single_instance()
        # Not os.execv: on Windows it passes argv unquoted, so "C:\\Program Files\\..." splits at
        # the space and the new process dies silently under pythonw. Popen quotes each arg.
        subprocess.Popen([sys.executable] + sys.argv, close_fds=True)
        for stream in (sys.stdout, sys.stderr):  # None under pythonw
            try:
                stream and stream.flush()
            except Exception:
                pass
        faulthandler.disable()
        # os._exit runs DLL detach handlers while LiveWhisper/PortAudio threads are mid-call
        # (access violation in crash.log); TerminateProcess skips them. os._exit is the fallback.
        if sys.platform == "win32":
            ctypes.windll.kernel32.TerminateProcess(ctypes.windll.kernel32.GetCurrentProcess(), 0)
        os._exit(0)

    def load_config(self):
        self.config = load_config_file(self.config_path)

    def toggle_recording(self, mode="hands-free"):
        if self.is_recording:
            self.bridge.sig_stop_recording.emit()
        else:
            self.bridge.sig_start_recording.emit(mode)

    def _on_merge_toggle(self, cancelled: bool):
        self.merge_cancelled = bool(cancelled)

    def _show_merge_hint(self):
        """Tell the HUD whether this take will probably join the last paste. Fail closed."""
        try:
            self.merge_cancelled = False
            if not hasattr(self.hud, "set_merge_hint"):
                return
            joins = stitch.preview_merge(
                {"text": self.last_injected_text, "hwnd": self.last_target_hwnd,
                 "title": self.last_target_title, "time": self.last_injected_time},
                {"hwnd": self.target_hwnd, "title": self.target_title,
                 "time": time.time(), "app": self.active_app_name},
                last_user_input=hotkeys.last_user_input,
            )
            self.hud.set_merge_hint("joins" if joins else "")
        except Exception:
            pass

    def _handle_start_recording(self, mode="ptt"):
        if self.is_recording or self.is_processing:
            return

        # Mark the start first: the mic stream is already open and holds a pre-roll,
        # so nothing below can delay or clip the first word.
        self.live.start()
        self.recorder.start()
        self.is_recording = True
        self.current_mode = mode
        self._recording_started_at = time.perf_counter()

        # Save active window info
        win_info = get_active_window_info()
        self.target_hwnd = win_info.get("hwnd")
        self.target_title = win_info.get("title") or ""
        self.active_lens = win_info.get("lens", "prose")
        self.active_app_name = win_info.get("process_name", "")
        self.is_agent_target = win_info.get("is_agent", False)
        self.hud.set_target_app(ui_labels.app(self.active_app_name))
        self._show_merge_hint()

        # Dictation never grabs a selection (no synthetic Ctrl+C into the target app).
        # Voice-refactor starts from the preset picker, which caches the selection first.
        if mode != "refactor":
            self.selected_text = ""

        print(f"[LeonidasScribe] Recording started ({mode.upper()})...")
        self.audio_feedback.play_start()

        if mode == "capture":
            self.hud.show_capturing()
        else:
            self.hud.show_listening()

        # Pre-warm TLS connections to Groq/Cerebras during speech so sockets are hot on stop
        self.engine.prewarm_connections()

    def _handle_stop_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        if hasattr(self, "_reply_stop_timer") and self._reply_stop_timer.isActive():
            self._reply_stop_timer.stop()
        print(f"[LeonidasScribe] Recording stopped ({self.current_mode.upper()}). Processing...")
        self.audio_feedback.play_stop()
        self.hud.show_transcribing()

        wav_bytes = self.recorder.stop()

        # Wall-clock (button-to-button) vs. actual captured audio duration should track
        # within a second or two. A big gap means frames were lost between start() and
        # stop(), most likely a second start()/stop() pair fired unnoticed mid-recording
        # and reset AudioRecorder.frames, discarding everything spoken before the reset.
        # Cheap to detect after the fact, impossible to detect once the process exits.
        wall_clock_s = time.perf_counter() - getattr(self, "_recording_started_at", time.perf_counter())
        captured_s = self.recorder.last_recorded_seconds
        self._last_capture_mismatch = None
        if wall_clock_s - captured_s > 3.0 and wall_clock_s > captured_s * 1.5:
            self._last_capture_mismatch = {"wall_clock_s": round(wall_clock_s, 2), "captured_s": round(captured_s, 2)}
            print(f"[LeonidasScribe] WARNING: possible dropped audio. Held the toggle for "
                  f"{wall_clock_s:.1f}s but only captured {captured_s:.1f}s. If the output looks "
                  f"short, check for a stray extra start/stop chime during that recording.")
            # save_audio_wav stays off by default (raw voice on disk is a real retention
            # cost), but this specific case is exactly what recordings/failed/ exists for:
            # keep the evidence only when something looks wrong, not for every dictation.
            saved_path = self._save_failed_audio(self.recorder.last_raw_wav)
            if saved_path:
                self._last_capture_mismatch["saved_wav"] = saved_path
                print(f"[LeonidasScribe] Preserved the short capture for inspection: {saved_path}")

        if not wav_bytes:
            self.live.cancel()
            print("[LeonidasScribe] No speech detected. Nothing pasted.")
            self.hud.hide()
            return

        self.is_processing = True
        reply_data = self._active_reply_data if self.current_mode == "reply" else None
        self._active_reply_data = None
        threading.Thread(
            target=self._process_and_inject,
            args=(wav_bytes, self.current_mode, self.selected_text, self._take_info(), reply_data),
            daemon=True
        ).start()

    def _take_info(self) -> dict:
        """Snapshot of the recorder's per-take facts, taken on the UI thread at stop,
        so a new dictation starting meanwhile cannot overwrite them."""
        r = self.recorder
        return {"raw_wav": r.last_raw_wav, "recorded_seconds": round(r.last_recorded_seconds, 2),
                "speech_seconds": r.last_speech_seconds, "speech_end_s": r.last_speech_end_s,
                "started": time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(time.time() - r.last_recorded_seconds))}

    def _archive_take(self, take: dict, force: bool = False):
        """Every take is kept as FLAC under recordings/takes/YYYY-MM/ so a transcript
        card can play it back. Pruned after audio.keep_takes_days (0 = keep forever)."""
        if not take.get("raw_wav") or not (force or self.config.get("audio", {}).get("keep_takes", True)):
            return None
        try:
            import io
            import soundfile as sf
            samples, sr = sf.read(io.BytesIO(take["raw_wav"]), dtype="int16")
            out_dir = os.path.join(self.base_dir, "recordings", "takes", take["started"][:7])
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, take["started"] + ".flac")
            sf.write(path, samples, sr, format="FLAC", subtype="PCM_16")
            return os.path.relpath(path, self.base_dir).replace("\\", "/")
        except Exception as exc:
            print(f"[LeonidasScribe] Could not archive take audio: {exc}")
            return None

    def _prune_takes(self):
        days = int(self.config.get("audio", {}).get("keep_takes_days", 60) or 0)
        root = os.path.join(self.base_dir, "recordings", "takes")
        if days <= 0 or not os.path.isdir(root):
            return
        cutoff = time.time() - days * 86400
        removed = 0
        for folder, _, files in os.walk(root):
            for name in files:
                path = os.path.join(folder, name)
                if name.endswith(".flac") and os.path.getmtime(path) < cutoff:
                    os.remove(path)  # the app's own audio cache, past its configured retention
                    removed += 1
        if removed:
            print(f"[LeonidasScribe] Pruned {removed} takes older than {days} days.")

    def _handle_cancel_recording(self):
        """Escape means "do not paste", never "throw it away". The full recording is
        saved to recordings/cancelled/ first, then transcribed and polished in the
        background into history as a Cancelled card you can copy from."""
        if not self.is_recording:
            return
        if hasattr(self, "_reply_stop_timer") and self._reply_stop_timer.isActive():
            self._reply_stop_timer.stop()
        is_reply = (self.current_mode == "reply")
        reply_data = getattr(self, "_active_reply_data", None)
        self._active_reply_data = None
        self.is_recording = False
        self.live.cancel()
        wav_bytes = self.recorder.stop()
        self.audio_feedback.play_error()
        self.hud.hide()
        if is_reply:
            print("[LeonidasScribe] Voice question reply cancelled.")
            if reply_data:
                try:
                    VoiceController().cancel_reply(reply_data.get("announcement_id"))
                except Exception as e:
                    print(f"[LeonidasScribe] Failed to cancel reply window: {e}")
            return
        print("[LeonidasScribe] Dictation cancelled, not pasted. Keeping it in history.")
        threading.Thread(target=self._keep_cancelled,
                         args=(wav_bytes, self._take_info(), self.active_app_name, self.active_lens),
                         daemon=True).start()

    def _keep_cancelled(self, wav_bytes: bytes, take: dict, app_name: str, lens: str):
        # ponytail: shares DictationEngine's last_* diagnostics with any dictation
        # started meanwhile; worst case a card shows the other one's model names.
        saved_wav = self._archive_take(take, force=True)
        if not wav_bytes:
            return  # nothing was said; the raw take is still on disk
        t0 = time.perf_counter()
        raw_text, text, stt_ms, polish_ms = "", "", 0, 0
        try:
            raw_text = self.engine.transcribe(wav_bytes, context_lens=lens, app_name=app_name,
                                              speech_end_s=take.get("speech_end_s")).strip()
            stt_ms = int((time.perf_counter() - t0) * 1000)
            self.engine.last_polish_model = None
            self.engine.last_failover = None  # per-dictation state: a bypass must not inherit the last dictation's failover
            if raw_text:
                t1 = time.perf_counter()
                text = self.engine.polish_text(raw_text, context_lens=lens, is_agent_harness=getattr(self, "is_agent_target", False))
                polish_ms = int((time.perf_counter() - t1) * 1000)
        except Exception as e:
            print(f"[LeonidasScribe] Cancelled dictation could not be transcribed: {e}")
            text = f"[Transcription failed ({self._short_error(e)}). Audio saved at {saved_wav}]"
        if not raw_text and not text:
            return  # nothing was said
        metrics = self._metrics(int((time.perf_counter() - t0) * 1000), stt_ms, polish_ms, text, take, raw_text)
        self._log_history(raw_text, text, metrics, "cancelled", app=app_name, lens=lens, audio=saved_wav)

    def _save_failed_audio(self, wav_bytes: bytes, folder: str = "failed", force: bool = False):
        if not wav_bytes or not (force or self.config.get("audio", {}).get("save_failed_audio", True)):
            return None
        try:
            out_dir = os.path.join(self.base_dir, "recordings", folder)
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, time.strftime(f"{folder}-%Y-%m-%dT%H-%M-%S.wav"))
            with open(path, "wb") as f:
                f.write(wav_bytes)
            return path
        except Exception as exc:
            print(f"[LeonidasScribe] Could not preserve failed audio: {exc}")
            return None

    @staticmethod
    def _short_error(exc) -> str:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return "Rate limited"
        if status == 401:
            return "Bad API key"
        if status:
            return f"API {status}"
        name = type(exc).__name__
        if "Timeout" in name:
            return "Timed out"
        if "Connection" in name:
            return "No connection"
        return name[:18]

    def _log_history(self, raw_text: str, cleaned_text: str, metrics: dict, mode: str, preset: str = None,
                     preset_key: str = None, app: str = None, lens: str = None, audio: str = None):
        if not self.config.get("clipboard", {}).get("log_history", True):
            return
        # Single chokepoint every dictation/transform routes through, so the
        # capture mismatch flagged in _handle_stop_recording lands in history
        # without touching every metrics-dict construction site individually.
        if getattr(self, "_last_capture_mismatch", None):
            metrics = {**metrics, "capture_mismatch": self._last_capture_mismatch}
            self._last_capture_mismatch = None
        diag = getattr(self, "_last_drop_diagnostics", None)
        if diag and (diag.get("dropped_tail") or diag.get("dropped_midstream")):
            metrics = {**metrics, **diag}
        self._last_drop_diagnostics = None
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mode": mode,
            "cleaned_text": cleaned_text,
            "raw_text": raw_text,
            "metrics": metrics
        }
        if preset:
            entry["preset"] = preset
        # Stable preset identifier at top level: "preset" holds the display name,
        # which is editable and can collide; "preset_key" is the config key the
        # transform actually ran under, safe to aggregate on forever.
        if preset_key:
            entry["preset_key"] = preset_key
        # Where the text landed: the app captured when recording (or the transform)
        # started, not whatever window has focus now that the paste is done.
        entry["app"] = app if app is not None else getattr(self, "active_app_name", "")
        entry["lens"] = lens if lens is not None else getattr(self, "active_lens", "")
        if audio:
            entry["audio"] = audio
        try:
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.bridge.sig_history_updated.emit()
        except Exception as e:
            print(f"[LeonidasScribe] Failed to log history: {e}")

    def _seed_latency_window(self):
        """Last 20 dictations from history, so the alert works from the first take."""
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                tail = collections.deque(f, maxlen=200)
            for line in tail:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("mode") in ("hands-free", "ptt"):
                    ms = (e.get("metrics") or {}).get("total_ms")
                    if ms:
                        self._latency_window.append(ms)
        except OSError:
            pass

    def _check_latency_alert(self, total_ms: int):
        """Deterministic latency alarm: median of the last 20 dictations over
        alerts.latency_median_ms raises one Windows notification, then stays quiet for
        alerts.cooldown_hours. Code only, no scheduled job, no model call."""
        self._latency_window.append(total_ms)
        cfg = self.config.get("alerts", {})
        limit = int(cfg.get("latency_median_ms", 2500))
        if not cfg.get("latency_enabled", True) or len(self._latency_window) < 10:
            return
        median = statistics.median(self._latency_window)
        cooldown = float(cfg.get("cooldown_hours", 6)) * 3600
        if median > limit and time.time() - self._last_latency_alert > cooldown:
            self._last_latency_alert = time.time()
            msg = (f"Median of your last {len(self._latency_window)} dictations is {median:,.0f} ms "
                   f"(limit {limit:,} ms). Open Insights to see which stage slowed down.")
            print(f"[LeonidasScribe] LATENCY ALERT: {msg}")
            self.bridge.sig_alert.emit("Scribe is running slow", msg)

    def _metrics(self, total_ms: int, stt_ms: int, polish_ms: int, text: str = "",
                 take: dict = None, raw_text: str = "") -> dict:
        """What a transcript card shows. Every value is read from the call that
        produced this dictation, not from config, so a model switch shows up on
        the very next card."""
        e = self.engine
        m = {
            "total_ms": total_ms,
            "stt_ms": stt_ms,
            "polish_ms": polish_ms,
            "audio_seconds": getattr(e, "last_audio_seconds", 0.0),
            "recorded_seconds": (take or {}).get("recorded_seconds", 0.0),
            "stt_model": getattr(e, "whisper_model", None),
            "polish_model": getattr(e, "last_polish_model", None),
        }
        if getattr(e, "last_failover", None):
            m["failover"] = e.last_failover
        if (take or {}).get("stt_mode"):
            m["stt_mode"] = take["stt_mode"]
        if text:
            m["words"] = len(text.split())
        # Speaking pace: words you said (raw, fillers included) over speaking time
        # (speech plus short pauses; lead/tail silence and thinking pauses excluded).
        speech_s = (take or {}).get("speech_seconds") or 0.0
        if speech_s >= 2.0 and raw_text:
            m["speech_seconds"] = speech_s
            m["wpm"] = round(len(engine_strip(raw_text).split()) / speech_s * 60)
        return m

    def _process_and_inject(self, wav_bytes: bytes, mode: str = "ptt", selected_text: str = "", take: dict = None, reply_data: dict = None):
        take = take or {}
        t0_total = time.perf_counter()
        audio_path = None
        try:
            t0_stt = time.perf_counter()
            lens = getattr(self, "active_lens", "prose")
            app_name = getattr(self, "active_app_name", "")

            # Chunks transcribed while you talked cover all but the tail; only that is
            # left to do now. Any chunk failure falls back to the full recording.
            stt_res = self.live.finish() if self._live_enabled() else None
            if stt_res is not None:
                e = self.engine
                e.last_audio_seconds = round(self.live.sent_seconds, 2)
                e.last_dropped_segment, e.last_dropped_midstream, e.last_min_logprob = None, [], None
                take["stt_mode"] = f"live ({len(self.live.futures)} chunks)"
            else:
                stt_res = self.engine.transcribe(wav_bytes, context_lens=lens, app_name=app_name,
                                                 speech_end_s=take.get("speech_end_s"))
                take["stt_mode"] = "full"
            stt_ms = int((time.perf_counter() - t0_stt) * 1000)
            audio_path = self._archive_take(take)
            # Cleared so a mode that makes no polish call (macro expansion) doesn't
            # inherit a stale model name from whatever dictation last polished.
            self.engine.last_polish_model = None
            self.engine.last_failover = None  # per-dictation state: a bypass must not inherit the last dictation's failover

            # Whisper's hallucination filters (_join_segments / _drop_hallucinated_tail_segment
            # in engine.py) can drop a real, low-confidence tail or mid-stream segment, not just
            # invented filler. That drop previously vanished with no trace: dropped_tail and
            # dropped_midstream were computed on self.engine but never read back out into
            # history.jsonl, so "the last few sentences went missing" had no forensic trail to
            # confirm or rule this out. Surface it here and preserve the audio so a real drop
            # is something you can listen to and verify, not just infer.
            dropped_tail = getattr(self.engine, "last_dropped_segment", None)
            dropped_mid = getattr(self.engine, "last_dropped_midstream", None) or []
            self._last_drop_diagnostics = {
                "min_logprob": getattr(self.engine, "last_min_logprob", None),
                "dropped_tail": dropped_tail,
                "dropped_midstream": dropped_mid or None,
            }
            if dropped_tail or dropped_mid:
                saved_path = self._save_failed_audio(wav_bytes)
                print(f"[LeonidasScribe] WARNING: hallucination filter dropped a segment. "
                      f"tail={dropped_tail} midstream={dropped_mid}"
                      + (f" | Preserved audio: {saved_path}" if saved_path else ""))

            stt_text = stt_res.get("text", "") if isinstance(stt_res, dict) else str(stt_res)
            raw_text = self.engine.strip_hallucinated_tail(stt_text).strip()

            if not raw_text:
                self.bridge.sig_hud_call.emit("hide", ())
                return

            # Paths that skip polish (macros, capture, voice-refactor instructions) get
            # the text without pause markers; only polish reads them.
            plain_text = engine_strip(raw_text)
            # Check for Spoken Macro Trigger (ADR-0041)
            trigger_match = self.engine.lens_router.match_trigger(plain_text) if getattr(self.engine, "lens_router", None) else None

            if mode == "capture" or (trigger_match and trigger_match.get("execution") == "background"):
                t0_cap = time.perf_counter()
                payload = trigger_match["payload"] if trigger_match else plain_text
                print(f"[LeonidasScribe] Processing AIOS background capture: {payload}")
                cap_res = self.engine.capture_aios_item(payload)
                cap_ms = int((time.perf_counter() - t0_cap) * 1000)
                total_ms = int((time.perf_counter() - t0_total) * 1000)

                metrics = self._metrics(total_ms, stt_ms, cap_ms, take=take, raw_text=raw_text)
                title = cap_res.get("title", "Captured")
                if cap_res.get("status") == "success":
                    self.audio_feedback.play_success()
                    self.bridge.sig_hud_call.emit("show_captured", (title,))
                    self._log_history(raw_text, f"[AIOS CAPTURE]: {title}", metrics, mode, audio=audio_path)
                else:
                    error_msg = cap_res.get("error", title)
                    print(f"[LeonidasScribe] AIOS capture failed: {error_msg}")
                    self.audio_feedback.play_error()
                    self.bridge.sig_hud_call.emit("show_error", (error_msg,))
                    self._log_history(raw_text, f"[AIOS CAPTURE FAILED]: {error_msg}", metrics, mode, audio=audio_path)
            elif trigger_match and trigger_match.get("execution") == "paste":
                # Spoken Macro Expansion into active window (ADR-0041)
                t0_exp = time.perf_counter()
                win_info = get_active_window_info()
                proc_name = win_info.get("process_name", "")
                lens = win_info.get("lens", "prose")
                target_type = self.engine.lens_router.resolve_target_type(proc_name, lens)

                # Context-gated repair of payload before expanding template
                clean_payload = self.engine.lens_router.apply_context_repairs(trigger_match["payload"], context_lens="prompt")
                expanded_text = self.engine.lens_router.render_expansion(
                    trigger_match["trigger_name"],
                    target_type,
                    clean_payload
                )
                exp_ms = int((time.perf_counter() - t0_exp) * 1000)
                total_ms = int((time.perf_counter() - t0_total) * 1000)

                metrics = self._metrics(total_ms, stt_ms, exp_ms, take=take, raw_text=raw_text)

                print(f"[LeonidasScribe] Macro [{trigger_match['trigger_name']} -> {target_type}] ({total_ms}ms): {expanded_text}")
                self.clipboard.paste_text(expanded_text, restore_original=True, target_hwnd=self.target_hwnd)
                self._log_history(raw_text, expanded_text, metrics, f"macro:{trigger_match['trigger_name']}", audio=audio_path)
                self.audio_feedback.play_success()
                self.bridge.sig_hud_call.emit("show_success", ())
            elif selected_text and len(selected_text.strip()) > 1:
                # In-Place Voice Selection Transform (Refactor)
                t0_ref = time.perf_counter()
                print(f"[LeonidasScribe] Refactoring selection with instruction: '{raw_text}'")
                transformed_text = self.engine.refactor_selection(selected_text, instruction=plain_text)
                ref_ms = int((time.perf_counter() - t0_ref) * 1000)
                total_ms = int((time.perf_counter() - t0_total) * 1000)

                metrics = self._metrics(total_ms, stt_ms, ref_ms, take=take, raw_text=raw_text)

                print(f"[LeonidasScribe] Injecting transformed text ({total_ms}ms): {transformed_text[:60]}...")
                self.clipboard.paste_text(transformed_text, restore_original=True, target_hwnd=self.target_hwnd)
                self._log_history(f"[Instruction: {plain_text}]\n{selected_text}", transformed_text, metrics, "refactor", audio=audio_path)
                self.audio_feedback.play_success()
                self.bridge.sig_hud_call.emit("show_success", ())
            elif mode == "reply":
                total_ms = int((time.perf_counter() - t0_total) * 1000)
                metrics = self._metrics(total_ms, stt_ms, 0, take=take, raw_text=raw_text)
                ann_id = reply_data.get("announcement_id") if reply_data else None

                print(f"[LeonidasScribe] Sending voice question reply for {ann_id}: '{plain_text}'")
                try:
                    ok, res = VoiceController().send_reply(ann_id, plain_text, source="voice")
                    if ok:
                        self.audio_feedback.play_success()
                        self.bridge.sig_hud_call.emit("show_captured", (f"Replied: {plain_text[:35]}",))
                        self.bridge.sig_voice_reply_received.emit(plain_text)
                        self._log_history(raw_text, f"[VOICE REPLY]: {plain_text}", metrics, "reply", audio=audio_path)
                    else:
                        print(f"[LeonidasScribe] Voice reply failed: {res}")
                        self.audio_feedback.play_error()
                        self.bridge.sig_hud_call.emit("show_error", ("Reply failed",))
                        self._log_history(raw_text, f"[VOICE REPLY FAILED]: {res}", metrics, "reply", audio=audio_path)
                except Exception as e:
                    print(f"[LeonidasScribe] Exception sending voice reply: {e}")
                    self.audio_feedback.play_error()
                    self.bridge.sig_hud_call.emit("show_error", ("Reply error",))
            else:
                # Standard Dictation with App Lens
                lens = getattr(self, "active_lens", "prose")
                t0_pol = time.perf_counter()

                # Sequential dictation context check: stitch.should_merge applies every guard
                # (same window and title, no real user input, learned per-app window,
                # sentence guard); any doubt means no merge.
                now = time.time()
                recent_context = ""
                prepend_space = False
                merged = False
                if not self.merge_cancelled and stitch.should_merge(
                    {"text": self.last_injected_text, "hwnd": self.last_target_hwnd,
                     "title": self.last_target_title, "time": self.last_injected_time},
                    {"raw": raw_text, "hwnd": self.target_hwnd, "title": self.target_title,
                     "time": now, "app": self.active_app_name},
                    last_user_input=hotkeys.last_user_input,
                ):
                    merged = True
                    recent_context = self.last_injected_text.strip()
                    if not self.last_injected_text.endswith((" ", "\n", "\t")):
                        prepend_space = True

                cleaned_text = self.engine.polish_text(raw_text, context_lens=lens, recent_context=recent_context, is_agent_harness=getattr(self, "is_agent_target", False))
                polish_ms = int((time.perf_counter() - t0_pol) * 1000)
                total_ms = int((time.perf_counter() - t0_total) * 1000)

                metrics = self._metrics(total_ms, stt_ms, polish_ms, cleaned_text, take, raw_text)

                final_text = cleaned_text
                if prepend_space and final_text and not final_text.startswith((" ", "\n", "\t")):
                    final_text = " " + final_text

                print(f"[LeonidasScribe] Injecting text ({total_ms}ms total | STT: {stt_ms}ms, Polish: {polish_ms}ms, Lens: {lens}): {final_text}")
                restore_orig = self.config.get("clipboard", {}).get("restore_original", True)
                self.clipboard.paste_text(final_text, restore_original=restore_orig, target_hwnd=self.target_hwnd)

                self.last_injected_text = final_text
                self.last_injected_time = time.time()
                self.last_target_hwnd = self.target_hwnd
                self.last_target_title = self.target_title
                edit_learner.schedule_check(final_text, self.target_hwnd)

                self._log_history(raw_text, final_text, metrics, mode, audio=audio_path)
                self._check_latency_alert(total_ms)
                self.audio_feedback.play_success()
                self.bridge.sig_hud_call.emit("show_success", ("Joined",) if merged else ())
        except Exception as e:
            saved = self._save_failed_audio(take.get("raw_wav") or wav_bytes)
            print(f"[LeonidasScribe] Error processing audio: {type(e).__name__}: {e}")
            traceback.print_exc()
            if saved:
                print(f"[LeonidasScribe] Recording preserved at {saved}")
            self.audio_feedback.play_error()
            self.bridge.sig_hud_call.emit("show_error", (self._short_error(e),))
        finally:
            self.is_processing = False

    def run(self):
        threading.Thread(target=self._prune_takes, daemon=True).start()
        try:
            self.recorder.open()
        except Exception as e:
            print(f"[LeonidasScribe] Mic stream open failed, will retry on first press: {e}")
        self.hotkeys.start()
        if getattr(self, "updater", None) and self.updater.enabled:
            self.updater.start_background_watch()

        # Onboarding: if no Groq API key is present, guide user straight to Settings > Models
        if not self.engine.groq_api_key:
            self.dashboard.show_dashboard()
            try:
                self.dashboard.select_view(self.dashboard.settings_view)
                self.dashboard.settings_view.select_section(1)
                self.dashboard.toast("Welcome to Leonidas Scribe! Paste your Groq API key to activate dictation.", "info")
            except Exception:
                pass
        elif "--settings" in sys.argv or "-s" in sys.argv or "--hidden" not in sys.argv:
            self.dashboard.show_dashboard()
        self._start_stall_watchdog()

        global _event_loop_running
        _event_loop_running = True
        return self.app.exec()

    def _start_stall_watchdog(self, limit=1.0):
        """Logs the GUI thread's stack whenever it stops pumping events for > limit seconds,
        so a Not Responding freeze names its own cause in leonidas-scribe.log."""
        beat = [time.perf_counter()]
        main_id = threading.main_thread().ident
        self._stall_timer = QTimer()
        self._stall_timer.timeout.connect(lambda: beat.__setitem__(0, time.perf_counter()))
        self._stall_timer.start(100)

        def _watch_stalls():
            reported = 0.0
            while True:
                time.sleep(0.5)
                stall = time.perf_counter() - beat[0]
                if stall > limit and beat[0] != reported:
                    reported = beat[0]
                    frame = sys._current_frames().get(main_id)
                    print(f"[LeonidasScribe] GUI thread stalled {stall:.1f}s at:\n"
                          + "".join(traceback.format_stack(frame)[-24:]))
        threading.Thread(target=_watch_stalls, daemon=True, name="stall-watchdog").start()


def signal_existing_instance():
    # Grant permission to existing background process to take foreground focus
    try:
        user32.AllowSetForegroundWindow(0xFFFFFFFF)
    except Exception:
        pass
    h_event = kernel32.OpenEventW(0x0002, False, SHOW_EVENT_NAME)
    if h_event:
        kernel32.SetEvent(h_event)
        kernel32.CloseHandle(h_event)
        return True
    return False


if __name__ == "__main__":
    if not acquire_single_instance():
        print("[LeonidasScribe] Application already running. Signalling dashboard...")
        signal_existing_instance()
        sys.exit(0)

    app = LeonidasScribeApp()
    sys.exit(app.run())
