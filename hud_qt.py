"""
Leonidas Scribe - floating dictation pill HUD (PySide6 native).

Reads its palette from ui.styles (the Command register of the AIOS design
system), so the overlay and the dashboard cannot drift. Each state owns one hue:
crimson REC dot and a cyan live glow while listening, gold while polishing,
success green on paste, error rose on failure. Every state keeps its text label.
Glass here is painted, not composited: Qt Widgets has no backdrop-filter, and
clipping a translucent window to a rounded region via SetWindowRgn loses
anti-aliasing on the pill edge. A layered fill plus a top sheen and a light rim
reads as glass at this size and stays crisp on every display.
"""
import math
import time
import random
import collections
import os
import winsound
import threading
from PySide6.QtCore import Qt, QTimer, QRectF, Signal, QObject
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


class ConfettiParticle:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.vx = random.uniform(-2.2, 2.2)
        self.vy = random.uniform(-3.2, -0.6)
        self.gravity = 0.12
        self.alpha = 255.0
        self.fade_speed = random.uniform(5.5, 9.5)
        self.size = random.uniform(2.8, 4.8)
        self.rot = random.uniform(0, 360)
        self.vrot = random.uniform(-10.0, 10.0)
        palette = [_c("cyan"), _c("gold"), _c("success"), _c("success"), _c("ink")]
        self.color = random.choice(palette)

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.vy += self.gravity
        self.rot += self.vrot
        self.alpha -= self.fade_speed
        return self.alpha > 0


class FloatingHUDWidget(QWidget):
    WAVE_BARS = 26
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
        self.current_pill_width = 210.0
        self.current_pill_height = 42.0
        self.target_pill_width = 210.0
        self.target_pill_height = 42.0
        self.custom_coords = None
        self.live_transcript = ""
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
        self.gradient_angle = 0.0
        self.particles = []
        self.success_anim_progress = 0.0

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
        self.live_transcript = (text or "").replace("\u23f8", "\n").strip()
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

    def word_count(self) -> int:
        return len(self.live_transcript.split())

    def _fit_listening(self):
        """REC dot, waveform, timer, app, words: sized once per change, not per frame."""
        font = QFont(FONT_MONO)
        font.setPixelSize(12)
        fm = QFontMetricsF(font)
        extras = ["0:00"]
        if self.target_app:
            extras.append(self.target_app)
        if self.live_transcript:
            extras.append(f"{self.word_count():,} words")
        if self._merge_label():
            extras.append(self._merge_label())
        text_w = sum(fm.horizontalAdvance(t) for t in extras) + 18 * (len(extras) - 1)
        wave_w = self.WAVE_BARS * 4.0
        self.target_pill_width = min(self.win_width - 40.0, 18 + 12 + 12 + wave_w + 14 + text_w + 20)
        self.target_pill_height = 42.0

    def _pill_center_y(self) -> float:
        return 50.0 if self.position == "top-center" else self.win_height - 50.0

    def _fit_pill(self, label: str, trailing: float = 0.0):
        """Size the pill to its content, the way a Dynamic Island does.

        A fixed width leaves "Pasted" swimming in dead space and truncates a
        long error. Measured with the same font the paint pass uses.
        """
        font = QFont(FONT_SANS)
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        text_w = QFontMetricsF(font).horizontalAdvance(label)
        # 16 lead + 20 glyph/dot + 6 gap + text + trailing + 18 tail
        width = 16 + 20 + 6 + text_w + trailing + 18
        self.target_pill_width = max(150.0, min(float(self.win_width - 40), width))
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

    def _on_anim_frame(self):
        self.anim_tick += 1

        # Smooth, instant interpolation of Dynamic Island pill geometry
        lerp_factor = 0.44
        if abs(self.target_pill_width - self.current_pill_width) > 0.4:
            self.current_pill_width += (self.target_pill_width - self.current_pill_width) * lerp_factor
        else:
            self.current_pill_width = self.target_pill_width

        if abs(self.target_pill_height - self.current_pill_height) > 0.4:
            self.current_pill_height += (self.target_pill_height - self.current_pill_height) * lerp_factor
        else:
            self.current_pill_height = self.target_pill_height

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
            self.wave.append(self.live_level)  # ~20 samples a second scroll right to left

        if self.state == "transforming":
            elapsed = time.monotonic() - getattr(self, "transform_started_at", time.monotonic())
            self.message = f"{getattr(self, 'transform_preset_name', 'Transforming')} · {elapsed:.1f}s"
            self._fit_pill(self.message, trailing=28.0)

        if motion.enabled():
            target_rot_speed = 0.012 + min(1.0, self.live_level) * 0.02
            self.gradient_angle = (self.gradient_angle + target_rot_speed) % (2 * math.pi)
            if self.state == "success":
                self.success_anim_progress += 0.025

        if self.particles:
            self.particles = [p for p in self.particles if p.update()]

        self.update()

    def _start_session(self):
        self.live_transcript = ""
        self.started_at = time.monotonic()
        self.wave = collections.deque([0.0] * self.WAVE_BARS, maxlen=self.WAVE_BARS)
        self._fit_listening()
        self.current_pill_width = self.target_pill_width

    def show_listening(self):
        self.fade_timer.stop()
        self.state = "listening"
        self.message = "Listening"
        self._start_session()
        self.live_level = 0.0
        self.particles = []
        self.success_anim_progress = 0.0
        self._position_window()
        self.show()
        if not self.anim_timer.isActive():
            self.anim_timer.start()

    def show_capturing(self):
        self.fade_timer.stop()
        self.state = "capturing"
        self.message = "Capturing"
        self._start_session()
        self.live_level = 0.0
        self.particles = []
        self.success_anim_progress = 0.0
        self._position_window()
        self.show()
        if not self.anim_timer.isActive():
            self.anim_timer.start()

    def show_transcribing(self):
        self.fade_timer.stop()
        self.state = "transcribing"
        self.message = "Polishing"
        self._fit_pill("Polishing")
        self.particles = []
        self.success_anim_progress = 0.0
        self.update()

    def show_transforming(self, preset_name="Transforming", detail="Refactoring..."):
        self.fade_timer.stop()
        self.state = "transforming"
        self.transform_preset_name = preset_name
        self.transform_detail = detail
        self.transform_started_at = time.monotonic()
        self.message = f"{preset_name} · 0.0s"
        self._fit_pill(self.message, trailing=28.0)
        self.particles = []
        self.success_anim_progress = 0.0
        self._position_window()
        self.show()
        if not self.anim_timer.isActive():
            self.anim_timer.start()
        self.update()

    def show_success(self, message="Pasted"):
        self.merge_hint = ""
        self.state = "success"
        self.message = message or "Pasted"
        self._fit_pill(self.message)
        self.success_anim_progress = 0.0
        cx, cy = self.win_width / 2.0, self._pill_center_y()
        self.particles = [ConfettiParticle(cx, cy) for _ in range(24)] if motion.enabled() else []
        self.update()
        self.fade_timer.start(1200)

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
        self.state = "success"
        self.message = "Saved: " + self._trim_words(title, 22)
        self._fit_pill(self.message)
        self.success_anim_progress = 0.0
        cx, cy = self.win_width / 2.0, self._pill_center_y()
        self.particles = [ConfettiParticle(cx, cy) for _ in range(24)] if motion.enabled() else []
        self.update()
        self.fade_timer.start(1500)

    def show_error(self, message="API Error"):
        self.state = "error"
        # Unclamped, this drew straight past the pill's edge onto the desktop for
        # any real API error string (they routinely run 60-150+ chars). _fit_pill
        # only bounds the pill's own width, not the text painted inside it.
        self.message = self._trim_words(message, 70)
        self._fit_pill(self.message)
        self.particles = []
        self.success_anim_progress = 0.0
        self.update()
        self.fade_timer.start(1500)

    def hide_hud(self):
        self.merge_hint = ""
        self.state = "hidden"
        self.anim_timer.stop()
        self.particles = []
        self.success_anim_progress = 0.0
        self.hide()

    def paintEvent(self, event):
        if self.state == "hidden":
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        offset_y = 0.0
        global_alpha = 1.0
        if self.state == "success":
            offset_y = min(12.0, self.success_anim_progress * 16.0)
            global_alpha = max(0.0, 1.0 - max(0.0, (self.success_anim_progress - 0.35) * 1.5))

        # Confetti
        if self.particles:
            for p in self.particles:
                painter.save()
                painter.translate(p.x, p.y + offset_y)
                painter.rotate(p.rot)
                c = QColor(p.color)
                c.setAlpha(int(max(0, min(255, p.alpha * global_alpha))))
                painter.setBrush(QBrush(c))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRect(QRectF(-p.size / 2, -p.size / 2, p.size, p.size * 0.7))
                painter.restore()

        px = (self.win_width - self.current_pill_width) / 2.0
        py = self._pill_center_y() - self.current_pill_height / 2.0 + offset_y
        rect = QRectF(px, py, self.current_pill_width, self.current_pill_height)
        radius = min(22.0, rect.height() / 2.0)

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        # --- soft drop shadow -------------------------------------------
        # A translucent top-level window cannot use QGraphicsDropShadowEffect
        # without artefacts, so the shadow is four expanding rings at low alpha.
        for i in range(4, 0, -1):
            spread = i * 2.2
            shadow = QPainterPath()
            shadow.addRoundedRect(
                rect.adjusted(-spread, -spread * 0.55, spread, spread * 1.15),
                radius + spread, radius + spread,
            )
            painter.fillPath(shadow, QColor(0, 0, 0, int(16 * global_alpha)))

        # --- glass body: obsidian-850 glass with a hairline rim ------------
        painter.fillPath(path, _c("obsidian_850", int(238 * global_alpha)))
        sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        sheen.setColorAt(0.0, QColor(255, 255, 255, int(18 * global_alpha)))
        sheen.setColorAt(0.5, QColor(255, 255, 255, int(4 * global_alpha)))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, QBrush(sheen))

        live = motion.enabled()
        rim_token = {"listening": "cyan", "capturing": "cyan", "transcribing": "gold",
                     "success": "success", "error": "error"}.get(self.state, "cyan")
        rim = _c(rim_token)

        # --- the one glow: soft rings in the state's hue ----------------------
        glow_alpha = {"listening": 46, "capturing": 46, "transcribing": 30}.get(self.state, 22)
        if self.state in ("listening", "capturing"):
            glow_alpha = int(glow_alpha * (0.75 + 0.5 * min(1.0, self.live_level)))
        for i in range(1, 5):
            ring = QPainterPath()
            ring.addRoundedRect(rect.adjusted(-i * 1.6, -i * 1.6, i * 1.6, i * 1.6), radius + i * 1.6, radius + i * 1.6)
            painter.setPen(QPen(QColor(rim.red(), rim.green(), rim.blue(),
                                       int(glow_alpha * global_alpha / i)), 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(ring)

        # --- rim --------------------------------------------------------------
        if self.state in ("listening", "capturing", "transcribing") and live:
            angle = self.gradient_angle if self.state != "transcribing" else (self.anim_tick * 0.024) % (2 * math.pi)
            cx, cy = rect.center().x(), rect.center().y()
            rx, ry = rect.width() / 2.0, rect.height() / 2.0
            grad = QLinearGradient(cx + math.cos(angle) * rx, cy + math.sin(angle) * ry,
                                   cx - math.cos(angle) * rx, cy - math.sin(angle) * ry)
            grad.setColorAt(0.0, QColor(rim.red(), rim.green(), rim.blue(), 245))
            grad.setColorAt(0.55, QColor(rim.red(), rim.green(), rim.blue(), 120))
            grad.setColorAt(1.0, QColor(255, 255, 255, 30))
            painter.setPen(QPen(QBrush(grad), 1.4))
        else:
            painter.setPen(QPen(QColor(rim.red(), rim.green(), rim.blue(), int(235 * global_alpha)), 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # --- content ----------------------------------------------------------
        from ui.icons import icon_font_family, GLYPH
        font_icon = QFont(icon_font_family())
        font_icon.setPixelSize(13)
        font_text = QFont(FONT_SANS)
        font_text.setPixelSize(13)
        font_text.setWeight(QFont.Weight.DemiBold)
        if self.state in ("listening", "capturing"):
            cy = py + self.current_pill_height / 2.0

            # Crimson REC dot: the universal "recording" sign, breathing when motion is on.
            pulse = (0.5 + 0.5 * math.sin(self.anim_tick * 0.12)) if live else 0.6
            dot_r = 3.6 + pulse * 1.2
            dot_x = px + 20.0
            dot = _c("crimson" if self.state == "listening" else "cyan")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(dot.red(), dot.green(), dot.blue(), int(40 + pulse * 70)))
            painter.drawEllipse(QRectF(dot_x - dot_r - 3.0, cy - dot_r - 3.0, (dot_r + 3.0) * 2, (dot_r + 3.0) * 2))
            painter.setBrush(dot)
            painter.drawEllipse(QRectF(dot_x - dot_r, cy - dot_r, dot_r * 2, dot_r * 2))

            # Waveform: the last ~1.3s of mic level scrolling right to left, mirrored
            # around the centre line. Flat means the mic hears nothing.
            cyan = _c("cyan")
            wx = px + 36.0
            for i, lvl in enumerate(self.wave):
                h = 2.0 + min(26.0, lvl * 26.0)
                age = i / max(1, self.WAVE_BARS - 1)          # 0 oldest .. 1 newest
                a = int((70 + 150 * age) * (0.45 + 0.55 * min(1.0, lvl * 1.6)))
                painter.setBrush(QColor(cyan.red(), cyan.green(), cyan.blue(), max(50, min(255, a))))
                painter.drawRoundedRect(QRectF(wx + i * 4.0, cy - h / 2.0, 2.4, h), 1.2, 1.2)

            # Timer, target app, live word count: mono, dim, separated by dots.
            secs = int(time.monotonic() - self.started_at) if self.started_at else 0
            parts = [f"{secs // 60}:{secs % 60:02d}"]
            if self.target_app:
                parts.append(self.target_app)
            if self.live_transcript:
                parts.append(f"{self.word_count():,} words")
            merge_label = self._merge_label()
            if merge_label:
                parts.append(merge_label)
            font_meta = QFont(FONT_MONO)
            font_meta.setPixelSize(12)
            painter.setFont(font_meta)
            tx = wx + self.WAVE_BARS * 4.0 + 14.0
            fm = painter.fontMetrics()
            for i, t in enumerate(parts):
                if i:
                    painter.setPen(_c("ink_dim", int(200 * global_alpha)))
                    painter.drawText(QRectF(tx, py, 18, self.current_pill_height), Qt.AlignmentFlag.AlignCenter, "\u00b7")
                    tx += 18
                role = "ink" if i == 0 else "ink_muted"
                if merge_label and i == len(parts) - 1:
                    role = "ink_dim" if self.merge_cancelled else "cyan"
                painter.setPen(_c(role, int(240 * global_alpha)))
                w = fm.horizontalAdvance(t) + 1
                painter.drawText(QRectF(tx, py, w, self.current_pill_height),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, t)
                tx += w

            if self.live_transcript and (self.transcript_mode == "always" or
                                         (self.transcript_mode == "hover" and self.hovered)):
                self._paint_transcript_panel(painter, py)

        elif self.state == "transcribing":
            painter.setFont(font_icon)
            painter.setPen(_c("gold"))
            painter.drawText(QRectF(px + 16, py, 20, self.current_pill_height), Qt.AlignmentFlag.AlignCenter, GLYPH["refresh"])
            painter.setFont(font_text)
            fm = painter.fontMetrics()
            start_tx = px + 42
            for i, ch in enumerate("Polishing"):
                ch_w = fm.horizontalAdvance(ch)
                wave = math.sin(self.anim_tick * 0.15 - i * 0.45) if live else 0.0
                painter.setPen(_c("ink", int(215 + 40 * wave)))
                painter.drawText(QRectF(start_tx, py + wave * 2.0, ch_w + 3, self.current_pill_height),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, ch)
                start_tx += ch_w

        elif self.state == "transforming":
            painter.setFont(font_icon)
            pulse = (0.5 + 0.5 * math.sin(self.anim_tick * 0.18)) if live else 0.8
            painter.setPen(_c("cyan", int(200 + 55 * pulse)))
            painter.drawText(QRectF(px + 16, py, 20, self.current_pill_height), Qt.AlignmentFlag.AlignCenter, GLYPH["flash"])
            painter.setFont(font_text)
            fm = painter.fontMetrics()
            name = getattr(self, "transform_preset_name", "Transforming")
            elapsed = time.monotonic() - getattr(self, "transform_started_at", time.monotonic())
            timer_text = f" · {elapsed:.1f}s"

            name_w = fm.horizontalAdvance(name)
            painter.setPen(_c("ink"))
            painter.drawText(QRectF(px + 42, py, name_w + 4, self.current_pill_height),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)

            font_meta = QFont(FONT_MONO)
            font_meta.setPixelSize(12)
            painter.setFont(font_meta)
            painter.setPen(_c("ink_muted", int(220 * global_alpha)))
            painter.drawText(QRectF(px + 42 + name_w + 4, py, self.current_pill_width - 48 - name_w, self.current_pill_height),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, timer_text)

        elif self.state in ("success", "error"):
            token, glyph = ("success", "check") if self.state == "success" else ("error", "close")
            painter.setFont(font_icon)
            painter.setPen(_c(token, int(255 * global_alpha)))
            painter.drawText(QRectF(px + 16, py, 20, self.current_pill_height), Qt.AlignmentFlag.AlignCenter, GLYPH[glyph])
            painter.setFont(font_text)
            painter.setPen(_c("ink", int(255 * global_alpha)))
            painter.drawText(QRectF(px + 42, py, self.current_pill_width - 58, self.current_pill_height),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.message)

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
