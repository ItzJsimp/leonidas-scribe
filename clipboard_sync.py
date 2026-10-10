"""
Shared Encrypted Clipboard Channel for Leonidas Scribe (Slice 2 & 3).
Synchronizes clipboard clips between the user and Mob using ntfy.sh with symmetric Fernet encryption.
Zero server state, zero plaintext on the wire, sub-second latency.
"""
import os
import sys
import json
import time
import logging
import threading
import requests
from typing import Optional, Dict, Any, Callable

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger("LeonidasScribe.ClipboardSync")


def generate_shared_key() -> str:
    """Generate a fresh 32-byte URL-safe base64 Fernet encryption key."""
    if not Fernet:
        raise RuntimeError("cryptography package is required for Fernet encryption")
    return Fernet.generate_key().decode("utf-8")


class ClipboardSyncCipher:
    """Handles symmetric payload encryption and decryption."""

    def __init__(self, key: Optional[str] = None):
        self.fernet: Optional[Fernet] = None
        if key and Fernet:
            try:
                # Accept string or bytes
                raw_key = key.strip().encode("utf-8") if isinstance(key, str) else key
                self.fernet = Fernet(raw_key)
            except Exception as e:
                logger.warning(f"[ClipboardSync] Invalid encryption key: {e}")
                self.fernet = None

    @property
    def is_configured(self) -> bool:
        return self.fernet is not None

    def encrypt(self, payload: Dict[str, Any]) -> Optional[str]:
        if not self.fernet:
            return None
        try:
            raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return self.fernet.encrypt(raw_bytes).decode("utf-8")
        except Exception as e:
            logger.error(f"[ClipboardSync] Encryption error: {e}")
            return None

    def decrypt(self, token_str: str) -> Optional[Dict[str, Any]]:
        if not self.fernet:
            return None
        try:
            decrypted_bytes = self.fernet.decrypt(token_str.strip().encode("utf-8"))
            return json.loads(decrypted_bytes.decode("utf-8"))
        except Exception as e:
            # Token from different key or corrupt data
            logger.debug(f"[ClipboardSync] Decryption failure: {e}")
            return None


class ClipboardSync(QObject):
    """
    Coordinates outbound sending and inbound background streaming reception
    of encrypted clipboard clips.
    """

    sig_clip_received = Signal(str, str, float)  # (text, sender_name, timestamp)
    sig_status_changed = Signal(str)            # status text for UI

    def __init__(
        self,
        config: Dict[str, Any],
        secrets: Dict[str, Any],
        on_clip_received: Optional[Callable[[str, str, float], None]] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        share_cfg = config.get("clipboard_share", {})
        self.enabled: bool = bool(share_cfg.get("enabled", False))
        self.topic: str = str(share_cfg.get("topic", "")).strip()
        self.server: str = str(share_cfg.get("server", "https://ntfy.sh")).rstrip("/")
        self.my_name: str = str(share_cfg.get("my_name", "the user")).strip()
        self.partner_name: str = str(share_cfg.get("partner_name", "Mob")).strip()
        self.auto_apply: bool = bool(share_cfg.get("auto_apply", False))
        self.auto_share: bool = bool(share_cfg.get("auto_share", False))

        # Encryption key resolution
        key = secrets.get("shared_clipboard_key", "")
        self.cipher = ClipboardSyncCipher(key)

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_sent_text: Optional[str] = None
        self._last_sent_time: float = 0.0

        if on_clip_received:
            self.sig_clip_received.connect(on_clip_received)

    def is_ready(self) -> bool:
        """Returns True if enabled, has a topic, and valid encryption cipher."""
        return self.enabled and bool(self.topic) and self.cipher.is_configured

    def send_clip(self, text: str) -> bool:
        """
        Encrypts text and dispatches to ntfy.sh topic.
        Returns True if successfully received by relay.
        """
        if not self.is_ready():
            logger.warning("[ClipboardSync] Cannot send: sync not configured or missing key/topic.")
            return False

        if not text or not text.strip():
            return False

        payload = {
            "text": text,
            "sender": self.my_name,
            "ts": time.time(),
        }

        ciphertext = self.cipher.encrypt(payload)
        if not ciphertext:
            logger.error("[ClipboardSync] Failed to encrypt payload.")
            return False

        url = f"{self.server}/{self.topic}"
        headers = {
            "Title": f"Clip from {self.my_name}",
            "Priority": "default",
            "Tags": "clipboard",
        }

        try:
            resp = requests.post(
                url,
                data=ciphertext.encode("utf-8"),
                headers=headers,
                timeout=5.0,
            )
            if resp.status_code == 200:
                self._last_sent_text = text
                self._last_sent_time = time.time()
                logger.info(f"[ClipboardSync] Sent clip ({len(text)} chars) to {self.partner_name}.")
                self.sig_status_changed.emit(f"Sent to {self.partner_name}")
                return True
            else:
                logger.error(f"[ClipboardSync] Relay returned HTTP {resp.status_code}: {resp.text[:100]}")
                return False
        except Exception as e:
            logger.error(f"[ClipboardSync] Send exception: {e}")
            return False

    def start_receiver(self):
        """Starts background streaming listener daemon thread."""
        if not self.is_ready():
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._receiver_worker,
            daemon=True,
            name="ScribeClipboardReceiverThread",
        )
        self._thread.start()
        logger.info(f"[ClipboardSync] Receiver listening on {self.topic} for clips from {self.partner_name}.")

    def stop_receiver(self):
        """Halts the background listener thread cleanly."""
        self._stop_event.set()

    def _receiver_worker(self):
        """Background thread streaming from ntfy raw endpoint with auto-reconnect."""
        backoff = 1.0
        stream_url = f"{self.server}/{self.topic}/raw"

        while not self._stop_event.is_set():
            try:
                # Stream raw lines; ntfy sends a keepalive newline every ~45s
                with requests.get(stream_url, stream=True, timeout=60.0) as resp:
                    if resp.status_code != 200:
                        time.sleep(backoff)
                        backoff = min(30.0, backoff * 2)
                        continue

                    backoff = 1.0  # Reset backoff on successful connect
                    for line in resp.iter_lines():
                        if self._stop_event.is_set():
                            break
                        if not line:
                            continue  # Keepalive ping

                        line_str = line.decode("utf-8", errors="replace").strip()
                        if not line_str:
                            continue

                        decrypted = self.cipher.decrypt(line_str)
                        if not decrypted:
                            continue

                        sender = decrypted.get("sender", "Partner")
                        clip_text = decrypted.get("text", "")
                        ts = float(decrypted.get("ts", time.time()))

                        # Ignore clips echoed back from ourselves
                        if sender == self.my_name:
                            continue

                        if clip_text:
                            logger.info(f"[ClipboardSync] Received clip ({len(clip_text)} chars) from {sender}.")
                            self.sig_clip_received.emit(clip_text, sender, ts)

            except Exception as e:
                if not self._stop_event.is_set():
                    time.sleep(backoff)
                    backoff = min(30.0, backoff * 1.5)
