"""Notification history and release status, using the existing dashboard components."""
import json
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QCheckBox
from ui.widgets import button, run_async


class NotificationInbox(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.setWindowTitle('Notifications')
        self.resize(640, 620)
        self.setMinimumSize(460, 400)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        check = button('Check for updates', 'GhostBtn', 'sync', 'Check the release channel. Active recordings wait until safe.')
        check.clicked.connect(self.check_update)
        read = button('Mark all read', 'GhostBtn', 'check', 'Keep history and clear the unread count.')
        read.clicked.connect(lambda: (host.notification_store.mark_all_read(), self.refresh()))
        actions.addWidget(check)
        actions.addWidget(read)
        layout.addLayout(actions)
        for key, title in [('muted', 'Mute desktop alerts'), ('mute_chat', 'Mute chat completion alerts')]:
            toggle = QCheckBox(title)
            toggle.setToolTip('Notifications still appear in history. No sounds or paid AI calls.')
            toggle.setChecked(bool(host.notification_store.preference(key, False)))
            toggle.toggled.connect(lambda value, k=key: host.notification_store.set_preference(k, value))
            layout.addWidget(toggle)
        self.items = QListWidget()
        self.items.setWordWrap(True)
        self.items.setAccessibleName('Notification and release history')
        layout.addWidget(self.items, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def check_update(self):
        run_async(lambda: self.host.controller.check_updates(), lambda result: self._checked(result))

    def _checked(self, result):
        if isinstance(result, Exception):
            self.status.setText(str(result))
            return
        if not result.get('ok'):
            self.status.setText(result.get('error', result.get('status', 'Check failed')))
        elif result.get('update_available'):
            run_async(lambda: self.host.controller.apply_update(), lambda res: self.status.setText(str(res) if isinstance(res, Exception) else res.get('status', res.get('error', 'Update queued'))))
        else:
            self.status.setText(result.get('status', 'Up to date'))

    def refresh(self):
        updater = getattr(self.host.controller, 'updater', None)
        self.status.setText('Updates: ' + (updater.last_status if updater else 'Managed by this installation'))
        self.items.clear()
        for event in self.host.notification_store.history():
            item = QListWidgetItem(('● ' if event['unread'] else '') + event['title'] + '\n' + event['text'])
            item.setToolTip(str(event.get('source', event.get('kind', 'app'))) + ' · ' + str(event.get('at', '')))
            self.items.addItem(item)
        history = Path(self.host.config_path).parent / 'release-history.json'
        try:
            for release in reversed(json.loads(history.read_text(encoding='utf-8-sig'))):
                self.items.addItem('v' + release['version'] + ' · ' + release.get('kind', 'patch') + '\n' + release.get('notes', 'App update'))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not self.items.count():
            self.items.addItem('You are up to date. Important app and chat alerts will appear here.')
