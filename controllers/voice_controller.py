"""
VoiceController - pure HTTP client of the Leonidas Voice Daemon settings API
(127.0.0.1:8765). The daemon owns its files; this controller never writes to
projects/leonidas-voice/. Endpoints: GET /health, GET/POST /config, GET /voices,
POST /preview, POST /announce. Pronunciation editing talks to GET/POST/DELETE
/pronunciation and degrades gracefully (returns None) while the daemon does not
serve it. The announcements log is read read-only from the daemon's
announcements.jsonl (path reported by GET /config).
"""
import json
import os
import subprocess

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
TIERS = ("critical", "info", "ambient")
TIMEOUT_S = 10


class VoiceController:
    def __init__(self, base_url=None, daemon_dir=None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        scribe_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.daemon_dir = daemon_dir or os.path.normpath(
            os.path.join(scribe_dir, os.pardir, "leonidas-voice"))
        self.start_script = os.path.join(os.path.dirname(self.daemon_dir),
                                         os.pardir, "scripts", "AIOS-VoiceDaemon.ps1")

    # --- low level ---
    def _get(self, path):
        try:
            r = requests.get(self.base_url + path, timeout=TIMEOUT_S)
        except requests.RequestException:
            return None
        return r.json() if r.status_code == 200 else None

    def _post(self, path, payload):
        """Returns (ok, body_or_error_text)."""
        try:
            r = requests.post(self.base_url + path, json=payload, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            return False, f"daemon unreachable: {e.__class__.__name__}"
        if r.status_code == 422:
            try:
                return False, r.json().get("detail", "invalid value")
            except Exception:
                return False, r.text
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        return True, r.json()

    # --- health ---
    def health(self, timeout=1.0):
        """GET /health, or None when the daemon is down."""
        try:
            r = requests.get(self.base_url + "/health", timeout=timeout)
            return r.json() if r.status_code == 200 else None
        except requests.RequestException:
            return None

    def start_daemon(self) -> tuple[bool, str]:
        """Spawn the idempotent launcher; it exits 0 whether or not it started anything."""
        script = os.path.normpath(self.start_script)
        if not os.path.exists(script):
            return False, f"launcher not found: {script}"
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-WindowStyle", "Hidden", "-File", script],
                cwd=os.path.dirname(script),
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            return True, "Launcher started, waiting for the daemon..."
        except OSError as e:
            return False, f"Failed to start launcher: {e}"

    # --- config ---
    def get_config(self):
        return self._get("/config")

    def save_config(self, voice=None, speed=None, max_chars=None) -> tuple[bool, str]:
        payload = {}
        if voice is not None:
            payload["voice"] = voice
        if speed is not None:
            payload["speed"] = round(float(speed), 2)
        if max_chars is not None:
            payload["max_chars"] = int(max_chars)
        if not payload:
            return False, "nothing to save"
        ok, body = self._post("/config", payload)
        if not ok:
            return False, body
        return True, "Saved. Next announcement uses the new settings."

    def get_voices(self):
        data = self._get("/voices")
        if not data:
            return []
        return list(data.get("voices", []))

    # --- speak ---
    def announce(self, text, tier) -> tuple[bool, str]:
        return self._post("/announce", {"text": text, "tier": tier})

    def preview(self, text, voice=None, speed=None) -> tuple[bool, str]:
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if speed is not None:
            payload["speed"] = round(float(speed), 2)
        ok, body = self._post("/preview", payload)
        if ok and isinstance(body, dict):
            if body.get("played"):
                return True, f"Played {body.get('voice')} in {body.get('duration_ms', '?')} ms"
            return True, f"Not played: {body.get('held', 'gate blocked')}"
        return ok, body

    # --- announcements log (read-only) ---
    def load_announcements(self, limit=20) -> list[dict]:
        """Last `limit` rows of the daemon's announcements.jsonl, newest first."""
        cfg = self.get_config() or {}
        name = cfg.get("log_path", "announcements.jsonl")
        path = os.path.join(self.daemon_dir, name)
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(entry, dict) and entry.get("kind") != "preview":
                        rows.append(entry)
        except OSError:
            return []
        rows.reverse()
        return rows[:limit]

    # --- quiet & snooze ---
    def get_quiet(self):
        """GET /quiet -> {'status': 'active'|'snoozed'|'muted', 'quiet_until': N, 'remaining_seconds': N}"""
        return self._get("/quiet")

    def set_quiet(self, minutes=None, mute=False, unmute=False, toggle=False) -> tuple[bool, dict | str]:
        payload = {}
        if minutes is not None:
            payload["minutes"] = int(minutes)
        elif mute:
            payload["mute"] = True
        elif unmute:
            payload["unmute"] = True
        elif toggle:
            payload["toggle"] = True
        else:
            return False, "Specify minutes, mute, unmute, or toggle"
        return self._post("/quiet", payload)

    def snooze(self, minutes: int = 30) -> tuple[bool, str]:
        ok, res = self.set_quiet(minutes=minutes)
        if ok and isinstance(res, dict):
            return True, f"Snoozed for {minutes}m."
        return ok, str(res)

    def mute(self, minutes: int = 120) -> tuple[bool, str]:
        """Timed quiet, never indefinite: an unattended quiet_until=-1 holds every
        announcement silently with no visible signal (2026-09-27 incident: 7 queued)."""
        ok, res = self.set_quiet(minutes=minutes)
        if ok:
            hours = minutes / 60
            label = f"{hours:g}h"
            return True, f"Muted voice notifications for {label}."
        return ok, str(res)

    def mute_indefinite(self) -> tuple[bool, str]:
        """Explicit 'until I turn it off' quiet (the user's 2026-09-24 gating decision).
        Always shown live via the status line / rail pill, unlike the bare
        quiet_until=-1 that caused the 2026-09-27 silent-hold incident `mute()` was
        fixed to avoid."""
        ok, res = self.set_quiet(minutes=-1)
        if ok:
            return True, "Muted voice notifications until you turn it back on."
        return ok, str(res)

    def unmute(self) -> tuple[bool, str]:
        ok, res = self.set_quiet(unmute=True)
        if ok:
            return True, "Voice notifications active (unmuted)."
        return ok, str(res)

    def toggle_quiet(self) -> tuple[bool, str]:
        ok, res = self.set_quiet(toggle=True)
        if ok and isinstance(res, dict):
            st = res.get("status", "toggled")
            return True, f"Voice is now {st}."
        return ok, str(res)

    # --- events ---
    def get_events(self, limit: int = 50, unread_only: bool = False) -> list[dict]:
        path = f"/events?limit={limit}"
        if unread_only:
            path += "&unread_only=true"
        data = self._get(path)
        if not data or not isinstance(data, dict):
            return []
        return data.get("events", [])

    def ack_event(self, event_id: str | None = None, ack_all: bool = False) -> tuple[bool, str]:
        payload = {"all": True} if ack_all else {"id": event_id}
        ok, res = self._post("/events/ack", payload)
        if ok and isinstance(res, dict):
            return True, f"Acknowledged {res.get('acknowledged', 0)} event(s)."
        return ok, str(res)

    # --- persona ---
    def get_persona(self):
        return self._get("/persona")

    def save_persona(self, address_style=None, chime_enabled=None, chime_volume=None,
                     pause_ms=None, ai_rewrite=None) -> tuple[bool, str]:
        payload = {}
        if address_style is not None:
            payload["address_style"] = str(address_style).strip()
        if chime_enabled is not None:
            payload["chime_enabled"] = bool(chime_enabled)
        if chime_volume is not None:
            payload["chime_volume"] = float(chime_volume)
        if pause_ms is not None:
            payload["pause_between_sentences_ms"] = int(pause_ms)
        if ai_rewrite is not None:
            payload["ai_rewrite"] = bool(ai_rewrite)
        if not payload:
            return False, "nothing to update"
        ok, res = self._post("/persona", payload)
        if ok:
            return True, "Persona settings saved."
        return ok, str(res)

    # --- pronunciation (daemon endpoint; None while not served) ---
    def load_pronunciation(self):
        """GET /pronunciation -> {'pronunciations': {...}} or None when the
        daemon does not serve it yet (404) or is down."""
        return self._get("/pronunciation")

    def save_pronunciation(self, word, spoken) -> tuple[bool, str]:
        if not word.strip() or not spoken.strip():
            return False, "Both the word and its spoken form are required."
        return self._post("/pronunciation", {"word": word.strip(), "spoken": spoken.strip()})

    def delete_pronunciation(self, word) -> tuple[bool, str]:
        try:
            r = requests.delete(self.base_url + "/pronunciation", json={"word": word},
                                timeout=TIMEOUT_S)
        except requests.RequestException:
            return False, "daemon unreachable"
        if r.status_code == 404:
            return False, "The daemon does not serve /pronunciation yet."
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        return True, f"Removed '{word}'."

    # --- reply window ---
    def send_reply(self, announcement_id: str | None, reply: str, source: str = "voice") -> tuple[bool, dict | str]:
        """POST /reply -> {'success': True, 'announcement_id': '...', 'reply': '...'}"""
        if not reply or not reply.strip():
            return False, "Reply text cannot be empty."
        ok, res = self._post("/reply", {"announcement_id": announcement_id, "reply": reply.strip(), "source": source})
        if ok and isinstance(res, dict):
            return True, res
        return ok, str(res)

    def cancel_reply(self, announcement_id: str | None = None) -> tuple[bool, str]:
        """POST /reply/cancel"""
        payload = {"announcement_id": announcement_id} if announcement_id else {}
        ok, res = self._post("/reply/cancel", payload)
        if ok:
            return True, "Reply window cancelled."
        return ok, str(res)

    def save_reply_settings(self, enabled: bool | None = None, timeout_seconds: float | None = None) -> tuple[bool, str]:
        """Update reply_window settings inside persona.json via POST /persona."""
        payload = {}
        curr = self.get_persona() or {}
        rw = dict(curr.get("reply_window", {}))
        if enabled is not None:
            rw["enabled"] = bool(enabled)
        if timeout_seconds is not None:
            rw["timeout_seconds"] = max(3.0, min(10.0, float(timeout_seconds)))
        payload["reply_window"] = rw
        ok, res = self._post("/persona", payload)
        if ok:
            return True, "Reply window settings saved."
        return ok, str(res)

    def get_moments(self) -> list[dict]:
        """GET /moments: the Jarvis Moments rules, or [] when the daemon is down."""
        return (self._get("/moments") or {}).get("moments", [])

    def set_moment(self, moment_id: str, enabled: bool) -> tuple[bool, str]:
        ok, res = self._post("/moments", {"id": moment_id, "enabled": bool(enabled)})
        return (True, f"Moment {'on' if enabled else 'off'}.") if ok else (False, str(res))

    # --- profiles, history actions ---
    def save_settings(self, payload: dict) -> tuple[bool, str]:
        """POST /config with any mix of profiles, active_profile, max_chars and gate rules."""
        ok, res = self._post("/config", payload)
        return (True, "Voice settings saved.") if ok else (False, str(res))

    def preview_profile(self, text: str, profile: dict) -> tuple[bool, str]:
        ok, body = self._post("/preview", {"text": text, "profile": profile})
        if ok and isinstance(body, dict):
            return (True, f"Played in {body.get('duration_ms', '?')} ms") if body.get("played") \
                else (True, f"Not played: {body.get('reason') or body.get('held') or 'blocked'}")
        return ok, str(body)

    def replay_event(self, event_id: str) -> tuple[bool, str]:
        ok, res = self._post("/events/replay", {"id": event_id})
        return (True, "Replaying.") if ok else (False, str(res))

    def resend_event(self, event_id: str) -> tuple[bool, str]:
        ok, res = self._post("/events/resend", {"id": event_id})
        return (True, "Sent again.") if ok else (False, str(res))

    def stop(self) -> tuple[bool, str]:
        """POST /stop: halt active speech or preview audio immediately."""
        ok, res = self._post("/stop", {})
        return (True, "Stopped.") if ok else (False, str(res))

    def get_queue(self) -> list[dict]:
        """GET /queue -> [{'tier': ..., 'text': ..., 'event_id': ..., 'headline': ...}, ...]"""
        data = self._get("/queue")
        if not data or not isinstance(data, dict):
            return []
        return data.get("items", [])

    def skip_queued(self, event_id: str | None = None) -> tuple[bool, dict | str]:
        """POST /queue/skip: skip current or specific queued announcement."""
        payload = {"event_id": event_id} if event_id else {}
        ok, res = self._post("/queue/skip", payload)
        return (True, res) if ok else (False, str(res))

    def clear_queue(self) -> tuple[bool, int]:
        """POST /queue/clear: drop all queued announcements without speaking."""
        ok, res = self._post("/queue/clear", {})
        if ok and isinstance(res, dict):
            return True, res.get("cleared", 0)
        return False, 0

