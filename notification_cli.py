"""Notification front door for local AI agents and scripts. Hook failures never block chats."""
import argparse
import json
import sys
import time
from pathlib import Path
from notifications import NotificationStore


def event_from_hook(data):
    event = data.get('hook_event_name')
    if event == 'Stop' and data.get('stop_hook_active'):
        return None
    if event not in ('Stop', 'Notification'):
        return None
    if event == 'Notification' and data.get('notification_type') not in ('permission_prompt', 'elicitation_dialog'):
        return None
    project = Path(data.get('cwd') or '.').name
    return {'id': f"claude:{data.get('session_id', '')}:{event}", 'at': time.time(),
            'kind': 'chat', 'level': 'info' if event == 'Stop' else 'warn',
            'title': 'Chat finished' if event == 'Stop' else 'Chat needs you',
            'text': f"{project}: " + (str(data.get('message') or 'Claude finished responding.')[:1000]),
            'source': 'Claude Code'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hook', action='store_true')
    parser.add_argument('--title', default='AIOS update')
    parser.add_argument('--text', default='')
    parser.add_argument('--level', choices=['info', 'warn', 'critical', 'log'], default='info')
    parser.add_argument('--kind', default='automation')
    args = parser.parse_args()
    try:
        data = json.load(sys.stdin) if args.hook else vars(args)
        event = event_from_hook(data) if args.hook else data
        if event:
            NotificationStore(Path(__file__).resolve().parent / 'notifications.db').add(event)
    except Exception as exc:
        # A missing app or a busy disk must never stop the user's agent.
        if not args.hook:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
