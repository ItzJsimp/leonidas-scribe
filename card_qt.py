"""
Leonidas Scribe: Visual Notification Card Overlay (Leonidas Card).
PySide6 native overlay for voice announcements and interactive reply loop.
Displays incoming announcements (headline, text, suggested step, action buttons,
and chat/dictation reply input) as a sleek, non-intrusive floating HUD card.
Zero em-dash policy enforced per ADR-0012.
"""
import os
import threading

from PySide6.QtCore import Qt, Signal, QTimer, QRectF, QUrl
from PySide6.QtWidgets import (
    QWidget, QApplication, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit
)
from PySide6.QtGui import (
    QPainter, QColor, QFont, QPainterPath, QLinearGradient, QPen, QBrush,
    QDesktopServices
)

from ui.styles import T, FONT_SANS, FONT_MONO
from controllers.voice_controller import VoiceController


def _c(token: str, alpha: int = 255) -> QColor:
    col = QColor(T[token])
    col.setAlpha(alpha)
    return col


def _font(family: str, px: int, weight=QFont.Weight.Normal) -> QFont:
    f = QFont(family)
    f.setPixelSize(max(9, round(px * 0.9)))  # compact card: ~1 px smaller, never below 9 px
    f.setWeight(weight)
    return f


class LeonidasCardWidget(QWidget):
    """Floating frameless notification card for Leonidas voice announcements.

    Provides a visual twin for spoken updates, interactive action buttons,
    and a typed or Scribe-dictated chat reply box.
    """
    sig_action_triggered = Signal(str, str)  # (event_id, action_id)
    sig_reply_submitted = Signal(str, str, str)  # (event_id, reply_text, source)
    sig_dismissed = Signal(str)  # (event_id)

    CARD_WIDTH = 368  # 20% narrower (the user, 2026-10-09: cards took too much of the screen edge)

    def __init__(self, controller: VoiceController | None = None, parent=None):
        super().__init__(parent)
        self.controller = controller or VoiceController()
        self.current_event = None
        self.event_id = None
        self._hovered = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.setFixedWidth(self.CARD_WIDTH)
        self.setMouseTracking(True)

        self._build_ui()

        self._auto_dismiss_timer = QTimer(self)
        self._auto_dismiss_timer.setSingleShot(True)
        self._auto_dismiss_timer.timeout.connect(self.dismiss_card)

        self._close_after_feedback_timer = QTimer(self)
        self._close_after_feedback_timer.setSingleShot(True)
        self._close_after_feedback_timer.timeout.connect(self.hide_card)

    def _build_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(14, 12, 14, 12)
        root_layout.setSpacing(8)

        # Header row: Brand label, Tier pill, and Dismiss button
        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        self.lbl_dot = QLabel("●")
        self.lbl_dot.setFont(_font(FONT_SANS, 11, QFont.Weight.Bold))
        self.lbl_dot.setStyleSheet(f"color: {T['cyan']};")
        header_row.addWidget(self.lbl_dot)

        self.lbl_brand = QLabel("// LEONIDAS AI")
        self.lbl_brand.setFont(_font(FONT_MONO, 10, QFont.Weight.Bold))
        self.lbl_brand.setStyleSheet(f"color: {T['ink_label']}; letter-spacing: 0.5px;")
        header_row.addWidget(self.lbl_brand)

        self.lbl_tier = QLabel("INFO")
        self.lbl_tier.setFont(_font(FONT_MONO, 9, QFont.Weight.DemiBold))
        self.lbl_tier.setStyleSheet(
            f"background: {T['obsidian_800']}; color: {T['cyan']}; "
            f"border: 1px solid {T['obsidian_700']}; border-radius: 4px; padding: 2px 6px;"
        )
        header_row.addWidget(self.lbl_tier)

        self.lbl_queue_count = QLabel("")
        self.lbl_queue_count.setFont(_font(FONT_MONO, 9, QFont.Weight.DemiBold))
        self.lbl_queue_count.setStyleSheet(
            f"background: {T['obsidian_800']}; color: {T['gold']}; "
            f"border: 1px solid {T['obsidian_700']}; border-radius: 4px; padding: 2px 6px;"
        )
        self.lbl_queue_count.setVisible(False)
        header_row.addWidget(self.lbl_queue_count)
        header_row.addStretch(1)

        self.btn_skip = QPushButton("Skip ⏭")
        self.btn_skip.setFixedHeight(22)
        self.btn_skip.setFont(_font(FONT_SANS, 10, QFont.Weight.Medium))
        self.btn_skip.setToolTip("Skip to next queued alert")
        self.btn_skip.setStyleSheet(
            f"QPushButton {{ background: {T['obsidian_800']}; color: {T['ink_dim']}; border: 1px solid {T['obsidian_700']}; border-radius: 4px; padding: 0 6px; }}"
            f"QPushButton:hover {{ background: {T['obsidian_700']}; color: {T['ink']}; }}"
        )
        self.btn_skip.clicked.connect(self.skip_card)
        self.btn_skip.setVisible(False)
        header_row.addWidget(self.btn_skip)

        self.btn_dismiss_all = QPushButton("Dismiss All")
        self.btn_dismiss_all.setFixedHeight(22)
        self.btn_dismiss_all.setFont(_font(FONT_SANS, 10, QFont.Weight.Medium))
        self.btn_dismiss_all.setToolTip("Dismiss all queued alerts")
        self.btn_dismiss_all.setStyleSheet(
            f"QPushButton {{ background: {T['obsidian_800']}; color: {T['crimson']}; border: 1px solid {T['obsidian_700']}; border-radius: 4px; padding: 0 6px; }}"
            f"QPushButton:hover {{ background: {T['crimson_soft']}; color: {T['crimson']}; }}"
        )
        self.btn_dismiss_all.clicked.connect(self.dismiss_all_queued)
        self.btn_dismiss_all.setVisible(False)
        header_row.addWidget(self.btn_dismiss_all)

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(22, 22)
        self.btn_close.setFont(_font(FONT_SANS, 11))
        self.btn_close.setToolTip("Dismiss (Esc)")
        self.btn_close.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T['ink_dim']}; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ background: {T['obsidian_800']}; color: {T['ink']}; }}"
        )
        self.btn_close.clicked.connect(self.dismiss_card)
        header_row.addWidget(self.btn_close)
        root_layout.addLayout(header_row)

        # Headline
        self.lbl_headline = QLabel("Announcement Headline")
        self.lbl_headline.setFont(_font(FONT_SANS, 13, QFont.Weight.DemiBold))
        self.lbl_headline.setStyleSheet(f"color: {T['ink']}; line-height: 1.3;")
        self.lbl_headline.setWordWrap(True)
        root_layout.addWidget(self.lbl_headline)

        # Full body text
        self.lbl_text = QLabel("Detailed text of the announcement appears here.")
        self.lbl_text.setFont(_font(FONT_SANS, 12))
        self.lbl_text.setStyleSheet(f"color: {T['ink_muted']}; line-height: 1.4;")
        self.lbl_text.setWordWrap(True)
        root_layout.addWidget(self.lbl_text)

        # Suggested next step pill (optional)
        self.box_suggested = QWidget()
        box_sugg_layout = QHBoxLayout(self.box_suggested)
        box_sugg_layout.setContentsMargins(10, 6, 10, 6)
        self.lbl_suggested = QLabel()
        self.lbl_suggested.setFont(_font(FONT_SANS, 11, QFont.Weight.Medium))
        self.lbl_suggested.setStyleSheet(f"color: {T['cyan']};")
        self.lbl_suggested.setWordWrap(True)
        box_sugg_layout.addWidget(self.lbl_suggested)
        self.box_suggested.setStyleSheet(
            f"background: {T['obsidian_800']}; border: 1px solid {T['obsidian_700']}; border-radius: 6px;"
        )
        root_layout.addWidget(self.box_suggested)

        # Action buttons row
        self.actions_widget = QWidget()
        self.actions_layout = QHBoxLayout(self.actions_widget)
        self.actions_layout.setContentsMargins(0, 2, 0, 2)
        self.actions_layout.setSpacing(8)
        root_layout.addWidget(self.actions_widget)

        # Chat box reply input row
        self.chat_row = QWidget()
        chat_layout = QHBoxLayout(self.chat_row)
        chat_layout.setContentsMargins(0, 2, 0, 0)
        chat_layout.setSpacing(8)

        self.in_reply = QLineEdit()
        self.in_reply.setPlaceholderText("Type a reply or press Enter...")
        self.in_reply.setFont(_font(FONT_SANS, 12))
        self.in_reply.setMinimumHeight(32)
        self.in_reply.setStyleSheet(
            f"QLineEdit {{ background: {T['obsidian_900']}; color: {T['ink']}; "
            f"border: 1px solid {T['obsidian_700']}; border-radius: 6px; padding: 4px 10px; }}"
            f"QLineEdit:focus {{ border: 1px solid {T['cyan']}; background: {T['obsidian_800']}; }}"
        )
        self.in_reply.returnPressed.connect(self._on_chat_submit)
        chat_layout.addWidget(self.in_reply, 1)

        self.btn_send = QPushButton("Reply")
        self.btn_send.setFont(_font(FONT_SANS, 11, QFont.Weight.DemiBold))
        self.btn_send.setMinimumHeight(32)
        self.btn_send.setStyleSheet(
            f"QPushButton {{ background: {T['obsidian_800']}; color: {T['cyan']}; "
            f"border: 1px solid {T['obsidian_700']}; border-radius: 6px; padding: 4px 14px; }}"
            f"QPushButton:hover {{ background: {T['obsidian_700']}; color: {T['ink']}; }}"
        )
        self.btn_send.clicked.connect(self._on_chat_submit)
        chat_layout.addWidget(self.btn_send)
        root_layout.addWidget(self.chat_row)

        # Feedback label (revealed upon reply or action selection)
        self.lbl_feedback = QLabel()
        self.lbl_feedback.setFont(_font(FONT_SANS, 11, QFont.Weight.Medium))
        self.lbl_feedback.setStyleSheet(f"color: {T['success']};")
        self.lbl_feedback.setVisible(False)
        root_layout.addWidget(self.lbl_feedback)

    def _position_window(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        self.adjustSize()
        from ui import float_stack
        float_stack.place(self)  # shares the bottom-right stack with the meetings call prompt

    def show_announcement(self, ann: dict):
        """Display an announcement card with actions and reply capability."""
        self._auto_dismiss_timer.stop()
        self._close_after_feedback_timer.stop()
        self.current_event = ann
        self.event_id = ann.get("id")

        tier = ann.get("tier", "info").lower()
        headline = ann.get("headline") or "Voice Announcement"
        text = ann.get("text") or ""
        suggested = ann.get("suggested_step") or ""
        actions = ann.get("actions") or []
        expects_reply = ann.get("expects_reply", False)

        # Style by tier
        tier_token = "crimson" if tier == "critical" else ("gold" if tier == "ambient" else "cyan")
        self.lbl_dot.setStyleSheet(f"color: {T[tier_token]};")
        self.lbl_tier.setText(tier.upper())
        self.lbl_tier.setStyleSheet(
            f"background: {T['obsidian_800']}; color: {T[tier_token]}; "
            f"border: 1px solid {T['obsidian_700']}; border-radius: 4px; padding: 2px 6px;"
        )

        self.lbl_headline.setText(headline)
        self.lbl_text.setText(text)

        if suggested:
            self.lbl_suggested.setText(f"⚡ Suggested: {suggested}")
            self.box_suggested.setVisible(True)
        else:
            self.box_suggested.setVisible(False)

        # Clear existing action buttons
        while self.actions_layout.count():
            item = self.actions_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        # Render up to 3 action buttons
        has_actions = False
        for act in actions[:3]:
            has_actions = True
            act_id = act.get("id", "action")
            label = act.get("label", act_id)
            kind = act.get("kind", "secondary")
            act_url = act.get("url") or (act_id if (isinstance(act_id, str) and ("://" in act_id or act_id.startswith("vscode:"))) else None)

            btn = QPushButton(label)
            btn.setFont(_font(FONT_SANS, 11, QFont.Weight.DemiBold))
            btn.setMinimumHeight(30)
            if kind == "primary":
                btn.setStyleSheet(
                    f"QPushButton {{ background: {T['cyan_soft']}; color: {T['cyan']}; "
                    f"border: 1px solid {T['cyan']}; border-radius: 6px; padding: 4px 12px; }}"
                    f"QPushButton:hover {{ background: {T['cyan']}; color: {T['obsidian_950']}; }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background: {T['obsidian_800']}; color: {T['ink']}; "
                    f"border: 1px solid {T['obsidian_700']}; border-radius: 6px; padding: 4px 12px; }}"
                    f"QPushButton:hover {{ background: {T['obsidian_700']}; border-color: {T['obsidian_600']}; }}"
                )
            btn.clicked.connect(lambda checked=False, aid=act_id, albl=label, aurl=act_url: self.submit_action(aid, albl, aurl))
            self.actions_layout.addWidget(btn)

        self.actions_widget.setVisible(has_actions)

        # Reset chat input
        self.in_reply.setEnabled(True)
        self.in_reply.clear()
        self.btn_send.setEnabled(True)
        self.lbl_feedback.setVisible(False)
        self.chat_row.setVisible(True)

        self._position_window()
        self.show()
        self.raise_()

        # Auto-dismiss timeout: 15s if no reply expected, 60s if reply expected
        timeout_ms = 60000 if expects_reply else 15000
        self._auto_dismiss_timer.start(timeout_ms)

        # Update queue indicators
        queued = int(ann.get("queued_remaining") or 0)
        if queued > 0:
            self.lbl_queue_count.setText(f"+{queued} QUEUED")
            self.lbl_queue_count.setVisible(True)
            self.btn_skip.setVisible(True)
            self.btn_dismiss_all.setVisible(True)
        else:
            self.lbl_queue_count.setVisible(False)
            self.btn_skip.setVisible(False)
            self.btn_dismiss_all.setVisible(False)

    def submit_action(self, action_id: str, action_label: str, url: str | None = None):
        """Action button clicked: submit reply, launch URL if present, and acknowledge event."""
        if action_id == "dismiss":
            self.dismiss_card()
            return

        self._auto_dismiss_timer.stop()
        self.actions_widget.setEnabled(False)
        self.chat_row.setEnabled(False)

        # Launch associated URL or protocol scheme if present
        if url:
            try:
                if not QDesktopServices.openUrl(QUrl(url)):
                    if hasattr(os, "startfile"):
                        os.startfile(url)
            except Exception:
                pass

        # Send reply to daemon and halt voice playback immediately
        calls = []
        if hasattr(self.controller, "stop"):
            calls.append(lambda: self.controller.stop())
        if self.event_id:
            calls.append(lambda: self.controller.send_reply(self.event_id, action_label, source="button"))
            calls.append(lambda: self.controller.ack_event(self.event_id))

        if calls:
            self._daemon_calls(*calls)

        self.sig_action_triggered.emit(self.event_id or "", action_id)

        self.lbl_feedback.setText(f"✓ Selected: {action_label}")
        self.lbl_feedback.setStyleSheet(f"color: {T['cyan']};")
        self.lbl_feedback.setVisible(True)
        self._close_after_feedback_timer.start(1200)

    def _on_chat_submit(self):
        text = self.in_reply.text().strip()
        if not text:
            return
        self.submit_reply(text)

    def submit_reply(self, reply_text: str):
        """Submit text reply entered via chat input or dictation."""
        self._auto_dismiss_timer.stop()
        self.in_reply.setEnabled(False)
        self.btn_send.setEnabled(False)
        self.actions_widget.setEnabled(False)

        calls = []
        if hasattr(self.controller, "stop"):
            calls.append(lambda: self.controller.stop())
        if self.event_id:
            calls.append(lambda: self.controller.send_reply(self.event_id, reply_text, source="chat"))
            calls.append(lambda: self.controller.ack_event(self.event_id))

        if calls:
            self._daemon_calls(*calls)

        self.sig_reply_submitted.emit(self.event_id or "", reply_text, "chat")

        self.lbl_feedback.setText(f"✓ Replied: {reply_text[:40]}")
        self.lbl_feedback.setStyleSheet(f"color: {T['success']};")
        self.lbl_feedback.setVisible(True)
        self._close_after_feedback_timer.start(1500)

    def show_voice_reply_received(self, text: str):
        """Called when hands-free speech was transcribed and submitted by voice."""
        self._auto_dismiss_timer.stop()
        self.in_reply.setEnabled(False)
        self.btn_send.setEnabled(False)
        self.actions_widget.setEnabled(False)

        self.lbl_feedback.setText(f"✓ Voice Reply: {text[:40]}")
        self.lbl_feedback.setStyleSheet(f"color: {T['success']};")
        self.lbl_feedback.setVisible(True)
        self._close_after_feedback_timer.start(1500)

    def dismiss_card(self):
        """Dismiss card without acting and halt voice speech immediately."""
        self._auto_dismiss_timer.stop()
        calls = []
        if hasattr(self.controller, "stop"):
            calls.append(lambda: self.controller.stop())
        if self.event_id:
            calls.append(lambda: self.controller.ack_event(self.event_id))
            calls.append(lambda: self.controller.cancel_reply(self.event_id))
            self.sig_dismissed.emit(self.event_id)
        if calls:
            self._daemon_calls(*calls)
        self.hide_card()

    def skip_card(self):
        """Skip this notification and advance to next in queue immediately."""
        self._auto_dismiss_timer.stop()
        calls = []
        if hasattr(self.controller, "stop"):
            calls.append(lambda: self.controller.stop())
        if hasattr(self.controller, "skip_queued"):
            calls.append(lambda: self.controller.skip_queued(self.event_id))
        if self.event_id:
            calls.append(lambda: self.controller.ack_event(self.event_id))
            calls.append(lambda: self.controller.cancel_reply(self.event_id))
            self.sig_dismissed.emit(self.event_id)
        if calls:
            self._daemon_calls(*calls)
        self.hide_card()

    def dismiss_all_queued(self):
        """Dismiss all queued notifications immediately without waiting."""
        self._auto_dismiss_timer.stop()
        calls = []
        if hasattr(self.controller, "stop"):
            calls.append(lambda: self.controller.stop())
        if hasattr(self.controller, "clear_queue"):
            calls.append(lambda: self.controller.clear_queue())
        if self.event_id:
            calls.append(lambda: self.controller.ack_event(ack_all=True))
            calls.append(lambda: self.controller.cancel_reply(self.event_id))
            self.sig_dismissed.emit(self.event_id)
        if calls:
            self._daemon_calls(*calls)
        self.hide_card()

    @staticmethod
    def _daemon_calls(*calls):
        """Run daemon HTTP calls in order off the GUI thread: a busy daemon blocks up to 10s per call."""
        def run():
            for call in calls:
                try:
                    call()
                except Exception as e:
                    print(f"[LeonidasCard] Daemon call failed: {e}")
        threading.Thread(target=run, daemon=True, name="card-daemon-calls").start()

    def hide_card(self):
        self._auto_dismiss_timer.stop()
        self._close_after_feedback_timer.stop()
        self.hide()

    def enterEvent(self, event):
        self._hovered = True
        self._auto_dismiss_timer.stop()  # Pause auto-dismiss while hovered
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        # Resume auto-dismiss with a fresh 8s window
        if self.isVisible() and not self._close_after_feedback_timer.isActive():
            self._auto_dismiss_timer.start(8000)
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss_card()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        radius = 14.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        # Soft drop shadow
        for i in range(3, 0, -1):
            spread = i * 2.0
            shadow = QPainterPath()
            shadow.addRoundedRect(
                rect.adjusted(-spread, -spread * 0.5, spread, spread * 1.0),
                radius + spread, radius + spread
            )
            painter.fillPath(shadow, QColor(0, 0, 0, 22))

        # Obsidian-850 glass body
        painter.fillPath(path, _c("obsidian_850", 248))

        # Top sheen gradient
        sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 14))
        sheen.setColorAt(0.4, QColor(255, 255, 255, 3))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, QBrush(sheen))

        # Hairline border matching tier
        tier = (self.current_event.get("tier", "info") if self.current_event else "info").lower()
        border_token = "crimson" if tier == "critical" else ("gold" if tier == "ambient" else "obsidian_700")
        border_col = _c(border_token, 180 if tier in ("critical", "gold") else 220)

        painter.setPen(QPen(border_col, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
