"""
Leonidas Scribe - floating dictation pill HUD (PySide6 native).

Direction C, Island (picked 2026-10-09, design/pill-redesign/contact-sheet.html): a black
island whose shape is the state. It grows out of a 6 px nub, stretches to listen (crimson
REC dot, level meter, timer), tucks into a small progress ring to transcribe (cyan) and
polish (gold), shows a green disc with a self-drawing check on paste, and tucks back into
the nub to leave. +XP rises out of the island over the label, tiered quiet, gold or rainbow.
No wave, no glow rings, no confetti per dictation. Palette from ui.styles so the overlay and
the dashboard cannot drift; painted with QPainter on one 60 fps timer.
"""
import math
import time
import collections
import os
import winsound
import threading
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, Signal, QObject
from PySide6.QtWidgets import QWidget, QApplication
from PySide6.QtGui import (
    QPainter, QColor, QFont, QPainterPath, QLinearGradient,
    QBrush, QPen, QFontMetricsF
)

from ui import motion
from ui.styles import T, FONT_SANS, FONT_MONO


def _c(token: str, alpha: int = 255) -> QColor:
    """Design token to QColor, with an optional alpha override."""
    col = QColor(T[token])
    col.setAlpha(alpha)
    return col


class AudioFeedback:
    def __init__(self, enabled=True, volume=100):
        self.enabled = enabled
        self.volume = volume
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.sounds_dir = os.path.join(self.base_dir, "sounds")
        self._scaled = {}

    def _scaled_wav(self, wav_path):
        """The WAV scaled to self.volume, as in-memory bytes. Cached per volume."""
        key = (wav_path, self.volume)
        if key not in self._scaled:
            import io
            import wave
            import array
            with wave.open(wav_path, "rb") as w:
                params = w.getparams()
                frames = w.readframes(w.getnframes())
            if params.sampwidth == 2:
                samples = array.array("h", frames)
                gain = max(0, min(100, self.volume)) / 100.0
                frames = array.array("h", (int(v * gain) for v in samples)).tobytes()
            buf = io.BytesIO()
            with wave.open(buf, "wb") as out:
                out.setparams(params)
                out.writeframes(frames)
            self._scaled[key] = buf.getvalue()
        return self._scaled[key]

    def _play_wav(self, name, fallback_freq=880, fallback_dur=60):
        if not self.enabled or self.volume <= 0:
            return
        wav_path = os.path.join(self.sounds_dir, f"{name}.wav")
        def _target():
            if os.path.exists(wav_path):
                try:
                    if self.volume >= 100:
                        winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                    else:
                        # SND_MEMORY cannot be async; this runs on its own thread instead.
                        winsound.PlaySound(self._scaled_wav(wav_path), winsound.SND_MEMORY)
                    return
                except Exception:
                    pass
            winsound.Beep(fallback_freq, fallback_dur)
        threading.Thread(target=_target, daemon=True).start()

    def play_start(self):
        self._play_wav("start", 880, 50)

    def play_stop(self):
        self._play_wav("stop", 659, 50)

    def play_success(self):
        self._play_wav("success", 1046, 60)

    def play_error(self):
        self._play_wav("error", 350, 100)


SPRING_K, SPRING_D = motion.SPRING_K, motion.SPRING_D   # shared Island spring (ui/motion.py)
NUB_W, NUB_H = 44.0, 6.0          # the resting island the pill grows out of and tucks back into
FADE_IN_S = 0.18                  # content cross-fade after a state change
CLOSE_S = 0.30                    # tuck back to the nub, then hide the window
XP_MS = {"normal": 1000, "high": 1300, "exceptional": 1900}


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _ease_out_back(t: float) -> float:
    t = max(0.0, min(1.0, t))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def xp_tier(ev: dict) -> str:
    """normal for an everyday dictation, high for a deep prompt or 150+ XP, exceptional for 400+."""
    try:
        xp = int(ev.get("xp") or "".join(ch for ch in str(ev.get("title", "")) if ch.isdigit()) or 0)
    except ValueError:
        xp = 0
    if xp >= 400:
        return "exceptional"
    if xp >= 150 or ev.get("body"):
        return "high"
    return "normal"


class FloatingHUDWidget(QWidget):
    """Direction C, Island (picked 2026-10-09): the shape is the state. A black island grows out
    of a 6 px nub, stretches to listen, tucks into a spinner to think, shows a disc on success and
    tucks back into the nub to leave. One 60 fps timer drives a spring on width and height."""
    WAVE_BARS = 26
    METER_BARS = 7
    PANEL_W = 540.0
    PANEL_LINES = 5

    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        # The window is taller than the pill: the live transcript panel opens above
        # it (below it when the pill sits at the top). Fully transparent pixels are
        # click-through, so the empty area never blocks the desktop.
        self.win_width = 620
        self.win_height = 300
        self.current_pill_width = NUB_W
        self.current_pill_height = NUB_H
        self.target_pill_width = NUB_W
        self.target_pill_height = NUB_H
        self._vw = 0.0
        self._vh = 0.0
        self.custom_coords = None
        self.live_transcript = ""
        self._committed = 0          # words in finished Whisper chunks
        self._pending_s = 0.0        # speech heard since the last chunk landed (its words are not transcribed yet)
        self._shown = 0              # the live word count: committed plus an estimate for what is still being said
        self.position = "bottom-center"      # or "top-center" (config hud.position)
        self.transcript_mode = "hover"       # "hover" | "always" | "off" (config hud.live_transcript)
        self.target_app = ""
        # Merge preview: "joins" when this dictation will be stitched onto the last one
        # (stitch.preview_merge). A click on the pill cancels it for this dictation only.
        self.merge_hint = ""
        self.merge_cancelled = False
        self.on_merge_toggle = None
        self.started_at = 0.0
        self.hovered = False
        self.wave = collections.deque([0.0] * self.WAVE_BARS, maxlen=self.WAVE_BARS)
        self.setMouseTracking(True)

        self.resize(self.win_width, self.win_height)

        self.state = "hidden"
        self.message = "Listening..."
        self.live_level = 0.0
        self.level_provider = None

        self.anim_tick = 0
        self.changed_at = time.monotonic()
        self.closing_at = None
        self.xp = None                       # {"text", "tier", "t0"} while a +XP rises out of the island

        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(16)
        self.anim_timer.timeout.connect(self._on_anim_frame)

        self.fade_timer = QTimer(self)
        self.fade_timer.setSingleShot(True)
        self.fade_timer.timeout.connect(self.hide_hud)

    @property
    def pill_width(self):
        return self.current_pill_width

    @pill_width.setter
    def pill_width(self, val):
        self.target_pill_width = float(val)
        self.current_pill_width = float(val)

    @property
    def pill_height(self):
        return self.current_pill_height

    @pill_height.setter
    def pill_height(self, val):
        self.target_pill_height = float(val)
        self.current_pill_height = float(val)

    def set_level_provider(self, provider_cb):
        self.level_provider = provider_cb

    def set_custom_position(self, x: int, y: int):
        self.custom_coords = (int(x), int(y))
        self.move(int(x), int(y))

    def update_live_transcript(self, text: str):
        """Accurate text from the live Whisper chunks. The pill keeps its size; the
        word count updates and the panel (hover or always) shows the words."""
        self.live_transcript = (text or "").replace("⏸", "\n").strip()
        now = len(self.live_transcript.split())
        # a chunk landed: its words are real now, so the speech it covered stops counting as an estimate
        self._pending_s = max(0.0, self._pending_s - max(0, now - self._committed) / self.WORDS_PER_SPEECH_SECOND)
        self._committed = now
        self._shown = now + int(self._pending_s * self.WORDS_PER_SPEECH_SECOND)
        if self.state in ("listening", "capturing"):
            self._fit_listening()
        self.update()

    def set_target_app(self, name: str):
        self.target_app = name or ""
        if self.state in ("listening", "capturing"):
            self._fit_listening()
            self.update()

    def set_merge_hint(self, hint: str):
        self.merge_hint = hint or ""
        self.merge_cancelled = False
        if self.state in ("listening", "capturing"):
            self._fit_listening()
            self.update()

    def _merge_label(self) -> str:
        if not self.merge_hint:
            return ""
        return "new line" if self.merge_cancelled else "+ joins last"

    def _toggle_merge(self):
        self.merge_cancelled = not self.merge_cancelled
        if callable(self.on_merge_toggle):
            try:
                self.on_merge_toggle(self.merge_cancelled)
            except Exception as e:
                print(f"[HUD] merge toggle callback failed: {e}")
        self._fit_listening()
        self.update()

    WORDS_PER_SPEECH_SECOND = 2.3   # about 140 words a minute while actually speaking

    def word_count(self) -> int:
        return self._shown

    def _meta_parts(self) -> list:
        secs = int(time.monotonic() - self.started_at) if self.started_at else 0
        parts = [f"{secs // 60}:{secs % 60:02d}"]
        if self.target_app:
            parts.append(self.target_app)
        if self._shown:
            parts.append(f"{self._shown:,} words")
        if self._merge_label():
            parts.append(self._merge_label())
        return parts

    def _fit_listening(self):
        """REC dot, level meter, timer, app, words: sized once per change, not per frame."""
        font = QFont(FONT_MONO)
        font.setPixelSize(12)
        fm = QFontMetricsF(font)
        extras = ["0:00"] + self._meta_parts()[1:]
        text_w = sum(fm.horizontalAdvance(t) for t in extras) + 18 * (len(extras) - 1)
        meter_w = self.METER_BARS * 3.0 + (self.METER_BARS - 1) * 3.0
        self.target_pill_width = min(self.win_width - 40.0, 16 + 8 + 10 + meter_w + 12 + text_w + 18)
        self.target_pill_height = 42.0

    def _pill_center_y(self) -> float:
        return 50.0 if self.position == "top-center" else self.win_height - 50.0

    def _fit_pill(self, label: str, trailing: float = 0.0):
        """Size the island to its content: 16 lead, 20 disc or spinner, 10 gap, text, trailing, 18 tail."""
        font = QFont(FONT_SANS)
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        text_w = QFontMetricsF(font).horizontalAdvance(label)
        width = 16 + 20 + 10 + text_w + trailing + 18
        self.target_pill_width = max(120.0, min(float(self.win_width - 40), width))
        self.target_pill_height = 42.0

    def _position_window(self):
        if self.custom_coords:
            self.move(self.custom_coords[0], self.custom_coords[1])
            return
        # One fixed home, never following the focused window (removed 2026-09-25):
        # bottom-centre by default, top-centre when hud.position says so.
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if area is None:
            return
        x = area.x() + (area.width() - self.win_width) // 2
        if self.position == "top-center":
            y = area.y() + 14 - int(self._pill_center_y() - 21)
        else:
            y = area.y() + area.height() - 36 - int(self._pill_center_y() + 21)
        self.move(int(x), int(y))

    def enterEvent(self, event):
        self.hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_active = True
            self._drag_start_pos = event.globalPosition().toPoint()
            self._window_start_pos = self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if getattr(self, "_drag_active", False) and (event.buttons() & Qt.MouseButton.LeftButton):
            delta = event.globalPosition().toPoint() - self._drag_start_pos
            new_pos = self._window_start_pos + delta
            self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and getattr(self, "_drag_active", False):
            self._drag_active = False
            moved = (event.globalPosition().toPoint() - self._drag_start_pos).manhattanLength()
            if moved < 4 and self.merge_hint and self.state in ("listening", "capturing"):
                self._toggle_merge()
                event.accept()
                return
            # Snap against the monitor the pill is actually on (multi-monitor correct),
            # not the primary display's metrics, and without flipping process DPI mode.
            geom = self.screen().availableGeometry()
            screen_w = geom.width()
            screen_h = geom.height()

            cur_x = self.x() - geom.x()
            cur_y = self.y() - geom.y()
            snap_dist = 45

            # Snap to left/center/right edges
            if cur_x < snap_dist:
                cur_x = 12
            elif abs(cur_x - (screen_w - self.win_width) // 2) < snap_dist:
                cur_x = (screen_w - self.win_width) // 2
            elif cur_x + self.win_width > screen_w - snap_dist:
                cur_x = screen_w - self.win_width - 12

            # Snap to top/bottom edges
            if cur_y < snap_dist:
                cur_y = 12
            elif cur_y + self.win_height > screen_h - snap_dist - 40:
                cur_y = screen_h - self.win_height - 70

            self.set_custom_position(geom.x() + cur_x, geom.y() + cur_y)
            event.accept()

    # ----- motion -------------------------------------------------------------
    def _spring(self):
        """Width and height follow their targets on one spring; Calm snaps them."""
        if not motion.enabled():
            self.current_pill_width, self.current_pill_height = self.target_pill_width, self.target_pill_height
            self._vw = self._vh = 0.0
            return
        self._vw = (self._vw + SPRING_K * (self.target_pill_width - self.current_pill_width)) * SPRING_D
        self._vh = (self._vh + SPRING_K * (self.target_pill_height - self.current_pill_height)) * SPRING_D
        self.current_pill_width += self._vw
        self.current_pill_height += self._vh
        if abs(self.target_pill_width - self.current_pill_width) < 0.3 and abs(self._vw) < 0.05:
            self.current_pill_width, self._vw = self.target_pill_width, 0.0
        if abs(self.target_pill_height - self.current_pill_height) < 0.3 and abs(self._vh) < 0.05:
            self.current_pill_height, self._vh = self.target_pill_height, 0.0

    def _on_anim_frame(self):
        self.anim_tick += 1
        self._spring()

        if self.level_provider:
            try:
                target_lvl = max(0.0, min(1.0, float(self.level_provider())))
                if target_lvl > self.live_level:
                    self.live_level += (target_lvl - self.live_level) * 0.55
                else:
                    self.live_level += (target_lvl - self.live_level) * 0.14
            except Exception:
                pass
        if self.state in ("listening", "capturing") and self.anim_tick % 3 == 0:
            self.wave.append(self.live_level)  # ~20 samples a second
        if self.state in ("listening", "capturing") and self.live_level > 0.12:
            # words show while you speak, not a Whisper round trip after the pause (it lagged by several seconds)
            self._pending_s += 0.016
            est = self._committed + int(self._pending_s * self.WORDS_PER_SPEECH_SECOND)
            if est > self._shown:
                self._shown = est
                self._fit_listening()

        if self.state == "transforming" and self.closing_at is None:
            elapsed = time.monotonic() - getattr(self, "transform_started_at", time.monotonic())
            self.message = f"{getattr(self, 'transform_preset_name', 'Transforming')} · {elapsed:.1f}s"
            self._fit_pill(getattr(self, "transform_preset_name", "Transforming"), trailing=40.0)

        if self.xp and (time.monotonic() - self.xp["t0"]) * 1000 > XP_MS[self.xp["tier"]]:
            self.xp = None
        if self.closing_at is not None and time.monotonic() - self.closing_at >= CLOSE_S:
            self._finish_hide()
            return

        self.update()

    def _enter(self, state: str, message: str):
        """Every visible state goes through here: cancels a pending tuck, restarts the cross-fade,
        and opens from the nub when the island was hidden."""
        was_hidden = self.state == "hidden" or not self.isVisible()
        self.fade_timer.stop()
        self.closing_at = None
        self.state = state
        self.message = message
        self.changed_at = time.monotonic()
        if state != "success":
            self.xp = None
        if was_hidden:
            self.current_pill_width, self.current_pill_height = NUB_W, NUB_H
            self._vw = self._vh = 0.0
            self.xp = None
            self._position_window()
            self.show()
        if not self.anim_timer.isActive():
            self.anim_timer.start()
        self.update()

    def _start_session(self):
        self.live_transcript = ""
        self._committed, self._pending_s, self._shown = 0, 0.0, 0
        self.started_at = time.monotonic()
        self.wave = collections.deque([0.0] * self.WAVE_BARS, maxlen=self.WAVE_BARS)
        self.live_level = 0.0

    def show_listening(self):
        self._start_session()
        self._enter("listening", "Listening")
        self._fit_listening()

    def show_capturing(self):
        self._start_session()
        self._enter("capturing", "Capturing")
        self._fit_listening()

    def show_transcribing(self):
        self._enter("transcribing", "Transcribing")
        self._fit_pill(self.message)

    def show_polishing(self):
        self._enter("polishing", "Polishing")
        self._fit_pill(self.message)

    def show_transforming(self, preset_name="Transforming", detail="Refactoring..."):
        self.transform_preset_name = preset_name
        self.transform_detail = detail
        self.transform_started_at = time.monotonic()
        self._enter("transforming", f"{preset_name} · 0.0s")
        self._fit_pill(preset_name, trailing=40.0)

    def show_success(self, message="Pasted"):
        self.merge_hint = ""
        self._enter("success", message or "Pasted")
        self._fit_pill(self.message)
        self.fade_timer.start(1200)

    def show_xp(self, text: str, tier: str = "normal"):
        """+XP rises out of the island just above the success label. Only while the success
        state is up (or still tucking away); a late event is dropped, never replayed later."""
        if self.state != "success":
            return
        tier = tier if tier in XP_MS else "normal"
        self.closing_at = None
        self._fit_pill(self.message)
        self.xp = {"text": text, "tier": tier, "t0": time.monotonic()}
        remaining = self.fade_timer.remainingTime() if self.fade_timer.isActive() else 0
        self.fade_timer.start(max(remaining, XP_MS[tier] + 150))
        if not self.anim_timer.isActive():
            self.anim_timer.start()

    @staticmethod
    def _trim_words(text: str, limit: int) -> str:
        """Cut on a word boundary; a mid-word chop reads like a rendering bug."""
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        cut = text[:limit]
        space = cut.rfind(" ")
        if space > limit * 0.5:
            cut = cut[:space]
        return cut.rstrip(" ,.;:") + "…"

    def show_captured(self, title="Saved"):
        self._enter("success", "Saved: " + self._trim_words(title, 22))
        self._fit_pill(self.message)
        self.fade_timer.start(1500)

    def show_error(self, message="API Error"):
        # Unclamped, this drew straight past the pill's edge onto the desktop for
        # any real API error string (they routinely run 60-150+ chars).
        self._enter("error", self._trim_words(message, 70))
        self._fit_pill(self.message)
        self.fade_timer.start(1800)

    def hide_hud(self):
        """Tuck back into the nub, then hide. Calm hides at once."""
        self.merge_hint = ""
        self.fade_timer.stop()
        if self.state == "hidden":
            return
        if not motion.enabled() or not self.isVisible():
            self._finish_hide()
            return
        if self.closing_at is None:
            self.closing_at = time.monotonic()
            self.target_pill_width, self.target_pill_height = NUB_W, NUB_H
            if not self.anim_timer.isActive():
                self.anim_timer.start()

    def _finish_hide(self):
        self.state = "hidden"
        self.closing_at = None
        self.xp = None
        self.anim_timer.stop()
        self.current_pill_width = self.target_pill_width = NUB_W
        self.current_pill_height = self.target_pill_height = NUB_H
        self._vw = self._vh = 0.0
        self.hide()

    # ----- paint --------------------------------------------------------------
    def paintEvent(self, event):
        if self.state == "hidden":
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = time.monotonic()
        live = motion.enabled()

        closing = 0.0 if self.closing_at is None else min(1.0, (now - self.closing_at) / CLOSE_S)
        body_alpha = 1.0 - max(0.0, (closing - 0.45) / 0.55)          # the nub fades on the way out
        content = (_ease_out((now - self.changed_at) / FADE_IN_S) if live else 1.0) * max(0.0, 1.0 - closing * 3.0)

        w, h = max(NUB_W * 0.6, self.current_pill_width), max(2.0, self.current_pill_height)
        px = (self.win_width - w) / 2.0
        py = self._pill_center_y() - h / 2.0
        rect = QRectF(px, py, w, h)
        radius = h / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        # soft offset shadow (CSS 0 10px 24px -8px): stacked translucent rects, no glow rings
        for i in range(5, 0, -1):
            s = i * 1.8
            sh = QPainterPath()
            sh.addRoundedRect(rect.adjusted(-s + 4, -s + 6, s - 4, s + 6), radius + s, radius + s)
            painter.fillPath(sh, QColor(0, 0, 0, int(22 * body_alpha * min(1.0, h / 30.0))))

        # black island; at nub height it reads as a quiet obsidian sliver
        nub = max(0.0, min(1.0, (h - NUB_H) / 20.0))
        o6, isl = _c("obsidian_600"), _c("island")
        fill = QColor(int(o6.red() + (isl.red() - o6.red()) * nub), int(o6.green() + (isl.green() - o6.green()) * nub),
                      int(o6.blue() + (isl.blue() - o6.blue()) * nub), int((178 + 77 * nub) * body_alpha))
        painter.fillPath(path, fill)
        painter.setPen(QPen(QColor(255, 255, 255, int(10 * body_alpha)), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        if content <= 0.01 or h < 30.0:
            return
        painter.save()
        painter.setClipPath(path)
        painter.setOpacity(content)
        cy = py + h / 2.0
        lead = px + 16.0
        text_x = lead + 30.0
        font_text = QFont(FONT_SANS)
        font_text.setPixelSize(13)
        font_text.setWeight(QFont.Weight.DemiBold)
        font_meta = QFont(FONT_MONO)
        font_meta.setPixelSize(12)

        if self.state in ("listening", "capturing"):
            self._paint_listening(painter, lead, cy, h, py, font_meta, live)
        elif self.state in ("transcribing", "polishing"):
            self._paint_spinner(painter, lead + 10.0, cy, "cyan" if self.state == "transcribing" else "gold", now, live)
            painter.setFont(font_text)
            painter.setPen(_c("ink"))
            painter.drawText(QRectF(text_x, py, w - (text_x - px) - 12, h),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.message)
        elif self.state == "transforming":
            from ui.icons import icon_font_family, GLYPH
            font_icon = QFont(icon_font_family())
            font_icon.setPixelSize(14)
            painter.setFont(font_icon)
            painter.setPen(_c("cyan"))
            painter.drawText(QRectF(lead, py, 20, h), Qt.AlignmentFlag.AlignCenter, GLYPH["flash"])
            name = getattr(self, "transform_preset_name", "Transforming")
            painter.setFont(font_text)
            painter.setPen(_c("ink"))
            name_w = painter.fontMetrics().horizontalAdvance(name)
            painter.drawText(QRectF(text_x, py, name_w + 2, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
            elapsed = now - getattr(self, "transform_started_at", now)
            painter.setFont(font_meta)
            painter.setPen(_c("ink_muted"))
            painter.drawText(QRectF(text_x + name_w + 10, py, 60, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             f"{elapsed:.1f}s")
        elif self.state in ("success", "error"):
            ok = self.state == "success"
            self._paint_disc(painter, lead + 10.0, cy, ok, (now - self.changed_at) if live else 1.0)
            painter.setFont(font_text)
            painter.setPen(_c("ink"))
            painter.drawText(QRectF(text_x, py, w - (text_x - px) - 14, h),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.message)
        painter.restore()

        if self.xp:
            self._paint_xp(painter, path, text_x, py, h, now, live, body_alpha)

        if self.state in ("listening", "capturing") and self.live_transcript and (
                self.transcript_mode == "always" or (self.transcript_mode == "hover" and self.hovered)):
            self._paint_transcript_panel(painter, py)

    def _paint_listening(self, painter, lead, cy, h, py, font_meta, live):
        # crimson REC dot (cyan while capturing), breathing 1.6 s like the sheet
        breathe = (0.675 + 0.325 * math.cos(self.anim_tick * 0.016 * 2 * math.pi / 1.6)) if live else 1.0
        dot = _c("crimson" if self.state == "listening" else "cyan", int(255 * breathe))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(QRectF(lead, cy - 4.0, 8.0, 8.0))

        # level meter: 7 bars, newest level in the middle spreading outward
        cyan = _c("cyan")
        mx = lead + 18.0
        samples = list(self.wave)
        mid = self.METER_BARS // 2
        for i in range(self.METER_BARS):
            lvl = samples[-1 - abs(i - mid) * 2] if samples else 0.0
            bh = 4.0 + min(18.0, lvl * 22.0)
            cyan.setAlpha(int(255 * (0.55 + 0.45 * min(1.0, lvl * 1.6))))
            painter.setBrush(cyan)
            painter.drawRoundedRect(QRectF(mx + i * 6.0, cy - bh / 2.0, 3.0, bh), 1.5, 1.5)

        parts = self._meta_parts()
        merge_label = self._merge_label()
        painter.setFont(font_meta)
        fm = painter.fontMetrics()
        tx = mx + self.METER_BARS * 6.0 - 3.0 + 12.0
        for i, t in enumerate(parts):
            if i:
                painter.setPen(_c("ink_dim"))
                painter.drawText(QRectF(tx, py, 18, h), Qt.AlignmentFlag.AlignCenter, "·")
                tx += 18
            role = "ink" if i == 0 else "ink_muted"
            if merge_label and i == len(parts) - 1:
                role = "ink_dim" if self.merge_cancelled else "cyan"
            painter.setPen(_c(role))
            tw = fm.horizontalAdvance(t) + 1
            painter.drawText(QRectF(tx, py, tw, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, t)
            tx += tw

    def _paint_spinner(self, painter, cx, cy, token, now, live):
        """The little progress ring: an 88 degree arc on an 18% track, one lap per 900 ms."""
        r = 6.5
        box = QRectF(cx - r, cy - r, r * 2, r * 2)
        col = _c(token)
        track = QColor(col)
        track.setAlpha(46)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(track, 2.0))
        painter.drawEllipse(box)
        pen = QPen(col, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        start = -((now % 0.9) / 0.9) * 360.0 if live else 90.0
        painter.drawArc(box, int(start * 16), int(-88 * 16))

    def _paint_disc(self, painter, cx, cy, ok, age):
        """20 px success/error disc with a black mark; the check draws itself in 360 ms after 120 ms."""
        pop = _ease_out_back(age / 0.32)
        r = 10.0 * max(0.0, pop)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_c("success" if ok else "error"))
        painter.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
        pen = QPen(QColor(0, 0, 0), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        s, ox, oy = 0.75, cx - 6.0, cy - 6.0       # the sheet's 16-unit glyph drawn at 12 px
        if ok:
            pts = [(3.5, 8.5), (6.5, 11.5), (12.5, 4.5)]
            lens = [math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2])]
            left = sum(lens) * _ease_out((age - 0.12) / 0.36)
            for (a, b), seg in zip(zip(pts, pts[1:]), lens):
                if left <= 0:
                    break
                f = min(1.0, left / seg)
                bx, by = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
                painter.drawLine(QPointF(ox + a[0] * s, oy + a[1] * s), QPointF(ox + bx * s, oy + by * s))
                left -= seg
        else:
            for a, b in (((4.5, 4.5), (11.5, 11.5)), ((11.5, 4.5), (4.5, 11.5))):
                painter.drawLine(QPointF(ox + a[0] * s, oy + a[1] * s), QPointF(ox + b[0] * s, oy + b[1] * s))

    def _paint_xp(self, painter, pill, x, py, h, now, live, body_alpha):
        """+XP rises out of the island over the label: quiet grey, gold with a slight overshoot,
        or a slow rainbow sweep for an exceptional dictation. Sinks below the pill at top-centre."""
        tier, text = self.xp["tier"], self.xp["text"]
        dur = XP_MS[tier] / 1000.0
        t = min(1.0, (now - self.xp["t0"]) / dur) if live else 0.5
        ease = _ease_out_back if tier != "normal" else _ease_out
        travel = {"normal": 14.0, "high": 18.0, "exceptional": 20.0}[tier] * ease(t / 0.55)
        alpha = min(1.0, t / 0.12) * (1.0 if t < 0.6 else max(0.0, (1 - t) / 0.4)) * body_alpha
        font = QFont(FONT_MONO)
        font.setPixelSize(11 if tier == "normal" else 12)
        font.setWeight(QFont.Weight.Normal if tier == "normal" else QFont.Weight.DemiBold)
        painter.save()
        outside = QPainterPath()
        outside.addRect(QRectF(self.rect()))
        painter.setClipPath(outside.subtracted(pill))  # it emerges from the island's edge
        painter.setFont(font)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        if self.position == "top-center":
            base = py + h + 2.0 + travel - 14.0 + fm.ascent()
        else:
            base = py - 2.0 - travel + 14.0
        painter.setOpacity(alpha * (0.85 if tier == "normal" else 1.0))
        # it floats over whatever is behind the pill: a 1 px drop shadow lifts it off light pages
        painter.setPen(QColor(0, 0, 0, 110))
        painter.drawText(QPointF(x, base + 1.0), text)
        if tier == "exceptional":
            grad = QLinearGradient(x, 0, x + tw, 0)
            hue = (now * 0.35) % 1.0
            for i in range(7):
                grad.setColorAt(i / 6.0, QColor.fromHsvF((hue + i / 7.0) % 1.0, 0.62, 1.0))
            painter.setPen(QPen(QBrush(grad), 1.0))
        else:
            painter.setPen(_c("gold" if tier == "high" else "ink_muted"))
        painter.drawText(QPointF(x, base), text)
        painter.restore()

    def _paint_transcript_panel(self, painter: QPainter, pill_y: float):
        font = QFont(FONT_SANS)
        font.setPixelSize(13)
        inner = self.PANEL_W - 36.0
        lines = _panel_lines(self.live_transcript, font, inner, self.PANEL_LINES)
        line_h = QFontMetricsF(font).lineSpacing()
        h = 30.0 + len(lines) * line_h
        x = (self.win_width - self.PANEL_W) / 2.0
        y = pill_y - 12.0 - h if self.position != "top-center" else pill_y + self.current_pill_height + 12.0
        rect = QRectF(x, y, self.PANEL_W, h)
        path = QPainterPath()
        path.addRoundedRect(rect, 14.0, 14.0)
        painter.fillPath(path, _c("obsidian_850", 240))
        painter.setPen(QPen(_c("obsidian_700"), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        head = QFont(FONT_MONO)
        head.setPixelSize(10)
        painter.setFont(head)
        painter.setPen(_c("ink_label"))
        painter.drawText(QRectF(x + 18, y + 8, inner, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         "// SO FAR  (updates at each pause)")
        painter.setFont(font)
        for i, line in enumerate(lines):
            age = (i + 1) / len(lines)            # older lines fade so the eye lands on the newest
            painter.setPen(_c("ink", int(120 + 135 * age)))
            painter.drawText(QRectF(x + 18, y + 24 + i * line_h, inner, line_h),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)


def _panel_lines(text: str, font: QFont, width: float, max_lines: int) -> list:
    """The last max_lines wrapped lines of text: the panel always shows the newest words."""
    from PySide6.QtGui import QTextLayout
    lines = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        layout = QTextLayout(para, font)
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            lines.append(para[line.textStart(): line.textStart() + line.textLength()].strip())
        layout.endLayout()
    return lines[-max_lines:]


class QtFloatingHUD(QObject):
    """Thin proxy in front of FloatingHUDWidget. ScribeBridge.sig_hud_call already
    marshals every background-thread caller onto the GUI thread before invoking
    these methods (see LeonidasScribe.py's _dispatch_hud_call), so this class only
    needs ONE queued signal, not one per widget method, to stay thread-safe for any
    caller that still reaches it directly."""

    _sig_call = Signal(str, tuple)

    def __init__(self, enabled=True):
        super().__init__()
        self.enabled = enabled
        self.widget = None
        self._sig_call.connect(self._dispatch)

    def bind_widget(self, widget: FloatingHUDWidget):
        self.widget = widget
        widget.on_merge_toggle = getattr(self, "_merge_cb", None)

    def set_merge_hint(self, hint: str):
        self._sig_call.emit("set_merge_hint", (hint or "",))

    def set_merge_toggle_callback(self, cb):
        """cb(cancelled: bool) runs on the GUI thread; keep it to setting a flag."""
        self._merge_cb = cb
        if self.widget is not None:
            self.widget.on_merge_toggle = cb

    def _dispatch(self, method: str, args: tuple):
        if self.widget is not None:
            getattr(self.widget, method)(*args)

    def show_listening(self):
        if self.enabled:
            self._sig_call.emit("show_listening", ())

    def show_capturing(self):
        if self.enabled:
            self._sig_call.emit("show_capturing", ())

    def show_transcribing(self):
        if self.enabled:
            self._sig_call.emit("show_transcribing", ())

    def show_polishing(self):
        if self.enabled:
            self._sig_call.emit("show_polishing", ())

    def show_xp(self, text: str, tier: str = "normal"):
        if self.enabled:
            self._sig_call.emit("show_xp", (text, tier))

    def show_transforming(self, preset_name="Transforming", detail="Refactoring..."):
        if self.enabled:
            self._sig_call.emit("show_transforming", (preset_name, detail))

    def show_success(self, message="Pasted"):
        if self.enabled:
            self._sig_call.emit("show_success", (message or "Pasted",))

    def show_captured(self, title="Saved"):
        if self.enabled:
            self._sig_call.emit("show_captured", (title,))

    def show_error(self, message="Error"):
        if self.enabled:
            self._sig_call.emit("show_error", (message,))

    def update_live_transcript(self, text: str):
        if self.enabled:
            self._sig_call.emit("update_live_transcript", (text,))

    def set_target_app(self, name: str):
        self._sig_call.emit("set_target_app", (name or "",))

    def hide(self):
        if self.enabled:
            self._sig_call.emit("hide_hud", ())
