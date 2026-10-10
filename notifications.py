"""Local, durable notification inbox. No network service or extra packages required."""
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


class NotificationStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, payload TEXT, unread INTEGER DEFAULT 1, delivered INTEGER DEFAULT 0, created REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def add(self, event):
        if not isinstance(event, dict) or not event.get('title'):
            return False
        event = dict(event)
        event.setdefault('at', time.time())
        event.setdefault('level', 'info')
        event.setdefault('kind', 'app')
        event['text'] = str(event.get('text') or event['title'])[:4000]
        event['title'] = str(event['title'])[:200]
        key = hashlib.sha256(json.dumps([event.get('id'), event['at'], event['title'], event['text']], ensure_ascii=False).encode()).hexdigest()
        with closing(self.connect()) as db, db:
            count = db.execute("INSERT OR IGNORE INTO events(key,payload,created) VALUES(?,?,?)", (key, json.dumps(event, ensure_ascii=False), time.time())).rowcount
            db.execute("DELETE FROM events WHERE key NOT IN (SELECT key FROM events ORDER BY created DESC LIMIT 1000)")
        return bool(count)

    def history(self, pending=False):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT key,payload,unread FROM events " + ("WHERE delivered=0 " if pending else "") + "ORDER BY created DESC LIMIT 100").fetchall()
        return [dict(json.loads(payload), key=key, unread=bool(unread)) for key, payload, unread in rows]

    def unread_count(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT COUNT(*) FROM events WHERE unread=1").fetchone()[0]

    def delivered(self, key):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE events SET delivered=1 WHERE key=?", (key,))

    def mark_all_read(self):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE events SET unread=0")

    def preference(self, key, default=None):
        with closing(self.connect()) as db:
            row = db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_preference(self, key, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", (key, json.dumps(value)))


class StreamReader:
    """Tail an AIOS stream, retaining incomplete lines and offsets across restarts."""
    def __init__(self, path, store):
        self.path, self.store = Path(path), store
        self.key = 'offset:' + str(self.path.resolve())
        self.offset = store.preference(self.key, self.path.stat().st_size if self.path.exists() else 0)

    def poll(self):
        if not self.path.exists():
            return
        if self.path.stat().st_size < self.offset:
            self.offset = 0
        with self.path.open('rb') as f:
            f.seek(self.offset)
            for _ in range(100):
                line = f.readline(65537)
                if not line or not line.endswith(b'\n'):
                    break
                self.offset = f.tell()
                try:
                    self.store.add(json.loads(line.decode('utf-8-sig')))
                except (ValueError, UnicodeError):
                    pass
        self.store.set_preference(self.key, self.offset)


def install_claude_hooks(settings_path, base_dir):
    """Merge only our command hooks, with a backup and no edits to permissions."""
    path = Path(settings_path)
    raw = path.read_text(encoding='utf-8-sig') if path.exists() else '{}'
    cfg = json.loads(raw)
    hooks = cfg.setdefault('hooks', {})
    command = str(Path(sys.executable).with_name('python.exe') if sys.platform == 'win32' else Path(sys.executable))
    arguments = [str(Path(base_dir).resolve() / 'notification_cli.py'), '--hook']
    for event, matcher in [('Stop', None), ('Notification', 'permission_prompt|elicitation_dialog')]:
        groups = hooks.setdefault(event, [])
        # Remove old Scribe-owned commands after an install moves, keeping peer hooks.
        clean = []
        for group in groups:
            remaining = [h for h in group.get('hooks', []) if 'notification_cli.py' not in h.get('command', '') and not any('notification_cli.py' in str(a) for a in h.get('args', []))]
            if remaining:
                clean.append(dict(group, hooks=remaining))
        entry = {'hooks': [{'type': 'command', 'command': command, 'args': arguments, 'timeout': 5}]}
        if matcher:
            entry['matcher'] = matcher
        hooks[event] = clean + [entry]
    new = json.dumps(cfg, indent=2, ensure_ascii=False) + '\n'
    if new != raw:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_suffix('.scribe-backup.json')
        if path.exists() and not backup.exists():
            backup.write_text(raw, encoding='utf-8')
        temp = path.with_suffix('.scribe-tmp.json')
        temp.write_text(new, encoding='utf-8')
        # Do not overwrite another settings writer that raced the merge.
        if path.exists() and path.read_text(encoding='utf-8-sig') != raw:
            raise RuntimeError('Claude settings changed during notification setup; retry next launch')
        os.replace(temp, path)
    return 'Claude completion and approval alerts connected'


def discover_streams(base_dir, config):
    paths = list(config.get('notifications', {}).get('streams', []))
    for parent in Path(base_dir).resolve().parents:
        if (parent / 'AGENTS.md').exists() and (parent / 'logs').is_dir():
            paths.append(str(parent / 'logs/notifications.jsonl'))
            break
    return list(dict.fromkeys(paths))
