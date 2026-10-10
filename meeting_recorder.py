"""Detached meeting recorder worker, FLAC segment writer, heartbeat, and crash recovery (S1).

Runs out-of-process (`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`) so a Scribe crash
or self-restart never interrupts recording. Writes 10 s FLAC segments via *.tmp + fsync +
os.replace and updates status.json every 2 s.
"""
import argparse
import datetime
import json
import mmap
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_RATE = 16000


def _atomic_write_json(path: str, data: dict, fsync: bool = True) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        if fsync:
            os.fsync(f.fileno())
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.02)


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    for _ in range(4):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            time.sleep(0.02)
    return None


# ---------------------------------------------------------------------------
# Dictation spans: Scribe dictations share the mic with a meeting's me track. The app logs each
# dictation's wall-clock span here; finalize drops me-track words inside them (meetings.exclude_dictation).
# ---------------------------------------------------------------------------
DICTATIONS_FILE = "dictations.jsonl"
DICTATIONS_MAX_BYTES = 256 * 1024


def default_store_root() -> str:
    return os.environ.get("SCRIBE_MEETINGS_DIR") or os.path.join(HERE, "meetings")


def log_dictation_span(store_root: str, start_wall: float, end_wall: float) -> None:
    """Append one span; trims itself to the newest 2000 lines past 256 KB. Never raises."""
    try:
        os.makedirs(store_root, exist_ok=True)
        path = os.path.join(store_root, DICTATIONS_FILE)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"start": round(start_wall, 3), "end": round(end_wall, 3)}) + "\n")
        if os.path.getsize(path) > DICTATIONS_MAX_BYTES:
            with open(path, encoding="utf-8") as f:
                keep = f.readlines()[-2000:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(keep)
    except OSError:
        pass


def read_dictation_spans(store_root: str, w0: float, w1: float) -> list:
    """[(start_wall, end_wall)] that overlap [w0, w1]."""
    out = []
    try:
        with open(os.path.join(store_root, DICTATIONS_FILE), encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    s, e = float(d["start"]), float(d["end"])
                except (ValueError, KeyError, TypeError):
                    continue
                if e > w0 and s < w1:
                    out.append((s, e))
    except OSError:
        pass
    return out


def init_meeting_dir(
    meeting_dir: str,
    title: str = "Meeting",
    source: dict | None = None,
    discord: dict | None = None,
    participants: list | None = None,
    destination_id: str | None = None,
    template_id: str = "default",
) -> dict:
    os.makedirs(os.path.join(meeting_dir, "me"), exist_ok=True)
    os.makedirs(os.path.join(meeting_dir, "them"), exist_ok=True)
    manifest_path = os.path.join(meeting_dir, "manifest.json")
    existing = _read_json(manifest_path)
    if existing:
        return existing
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest = {
        "id": os.path.basename(os.path.abspath(meeting_dir)),
        "title": title,
        "started": now_iso,
        "ended": None,
        "state": "recording",
        "source": source or {"kind": "device", "app": None, "pid": None, "device": "default"},
        "discord": discord or {"guild_id": None, "guild": None, "channel_id": None, "channel": None},
        "participants": participants or [],
        "destination_id": destination_id,
        "template_id": template_id,
        "pinned": False,
        "audio_deleted_at": None,
        "costs": [],
        "gaps": [],
    }
    _atomic_write_json(manifest_path, manifest, fsync=True)
    return manifest


def read_manifest(meeting_dir: str) -> dict | None:
    return _read_json(os.path.join(meeting_dir, "manifest.json"))


def write_manifest(meeting_dir: str, manifest: dict) -> None:
    _atomic_write_json(os.path.join(meeting_dir, "manifest.json"), manifest, fsync=True)


def read_status(meeting_dir: str) -> dict | None:
    return _read_json(os.path.join(meeting_dir, "status.json"))


def write_status(meeting_dir: str, status: dict) -> None:
    _atomic_write_json(os.path.join(meeting_dir, "status.json"), status, fsync=False)


def write_flac_segment(
    meeting_dir: str,
    track: str,
    index: int,
    pcm_int16: np.ndarray,
    sample_rate: int = SAMPLE_RATE,
    fsync: bool = True,
) -> str:
    track_dir = os.path.join(meeting_dir, track)
    os.makedirs(track_dir, exist_ok=True)
    final_path = os.path.join(track_dir, f"seg-{index:05d}.flac")
    tmp_path = final_path + ".tmp"
    arr = np.asarray(pcm_int16, dtype=np.int16)
    with open(tmp_path, "wb") as f:
        sf.write(f, arr, sample_rate, format="FLAC", subtype="PCM_16")
        f.flush()
        if fsync:  # the pre-roll buffer is disposable: skipping the flush keeps it from queueing disk writes behind dictation
            os.fsync(f.fileno())
    os.replace(tmp_path, final_path)
    return final_path


def _segment_files(meeting_dir: str, track: str = "me") -> list[str]:
    tdir = os.path.join(meeting_dir, track)
    if not os.path.isdir(tdir):
        return []
    files = [
        os.path.join(tdir, f)
        for f in os.listdir(tdir)
        if f.startswith("seg-") and f.endswith(".flac")
    ]
    return sorted(files)


def _highest_segment_index(meeting_dir: str) -> int:
    highest = 0
    for track in ("me", "them"):
        for path in _segment_files(meeting_dir, track):
            m = re.search(r"seg-(\d+)\.flac$", os.path.basename(path))
            if m:
                highest = max(highest, int(m.group(1)))
    return highest


def saved_audio_seconds(meeting_dir: str, track: str = "me") -> float:
    total = 0.0
    for path in _segment_files(meeting_dir, track):
        try:
            info = sf.info(path)
            total += float(info.frames) / float(info.samplerate)
        except Exception:
            pass
    return total


def resample_pcm_s16le(raw_bytes: bytes, src_rate: int, src_channels: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Convert raw s16le PCM of arbitrary rate/channels into dst_rate mono int16."""
    if not raw_bytes:
        return np.zeros(0, dtype=np.int16)
    samples = np.frombuffer(raw_bytes, dtype=np.int16)
    if src_channels > 1:
        usable = (len(samples) // src_channels) * src_channels
        samples = samples[:usable].reshape(-1, src_channels).mean(axis=1).astype(np.int16)
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    dst_len = int(round(len(samples) * float(dst_rate) / float(src_rate)))
    if dst_len <= 0:
        return np.zeros(0, dtype=np.int16)
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
    return np.interp(x_new, x_old, samples.astype(np.float32)).astype(np.int16)


def _wasapi_input_device(sd, device_index=None):
    """The WASAPI entry for the selected mic (default mic when None); same logic as engine.AudioRecorder._wasapi_twin."""
    apis = sd.query_hostapis()
    wi = next(i for i, h in enumerate(apis) if "WASAPI" in h["name"])
    if device_index is None:
        return apis[wi]["default_input_device"]
    d = sd.query_devices(device_index)
    if d["hostapi"] == wi:
        return device_index
    name = d["name"].strip()
    return next(
        i for i, w in enumerate(sd.query_devices())
        if w["hostapi"] == wi and w["max_input_channels"] > 0 and w["name"].startswith(name)
    )


def _configured_mic_device(source: dict | None):
    """Manifest source.mic_device wins, then config.json audio.device_index, else None (default mic)."""
    if (source or {}).get("mic_device") is not None:
        return source["mic_device"]
    cfg = _read_json(os.path.join(HERE, "config.json")) or {}
    return (cfg.get("audio") or {}).get("device_index")


class MicCapture:
    """16 kHz mono int16 mic feed (sounddevice WASAPI, auto_convert) for the Me track.
    Never raises: a dead or missing device just reports ok=False and read() returns zeros."""

    MAX_FRAMES = SAMPLE_RATE * 5
    STALE_S = 2.5

    def __init__(self, device=None, sd_module=None):
        self.device = device
        self._sd = sd_module
        self._stream = None
        self._pending = np.zeros(0, dtype=np.int16)
        self._lock = threading.Lock()
        self._last_cb = 0.0

    def _cb(self, indata, frames, time_info, status):
        self._last_cb = time.monotonic()
        chunk = np.asarray(indata, dtype=np.int16).reshape(-1)
        with self._lock:
            self._pending = np.concatenate((self._pending, chunk))[-self.MAX_FRAMES:]

    def start(self) -> bool:
        try:
            sd = self._sd
            if sd is None:
                import sounddevice as sd
            kw = dict(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=480, callback=self._cb)
            try:
                self._stream = sd.InputStream(
                    device=_wasapi_input_device(sd, self.device),
                    extra_settings=sd.WasapiSettings(auto_convert=True),
                    **kw,
                )
            except Exception:
                self._stream = sd.InputStream(device=self.device, **kw)
            self._stream.start()
            self._last_cb = time.monotonic()
        except Exception:
            self._stream = None
        return self._stream is not None

    @property
    def ok(self) -> bool:
        return self._stream is not None and (time.monotonic() - self._last_cb) < self.STALE_S

    def read(self, n: int) -> np.ndarray:
        with self._lock:
            out, self._pending = self._pending[:n], self._pending[n:]
        if len(out) < n:
            out = np.concatenate((out, np.zeros(n - len(out), dtype=np.int16)))
        return out

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


def _level(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) / 32768.0 if len(block) else 0.0


LEVELS_FILE = "levels.bin"
_LV = struct.Struct("<8fI")  # me x4, them x4 (25 ms sub-levels of one 100 ms block), then seq: written last, read twice
LEVEL_SUBS = 4


def level_norm(block: np.ndarray) -> float:
    """Same dB scale as the dictation pill (engine.AudioRecorder): -52 dBFS reads 0, -14 dBFS reads 1."""
    if not len(block):
        return 0.0
    rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
    db = 20.0 * np.log10(rms / 32768.0 + 1e-9)
    return float(min(1.0, max(0.0, (db + 52.0) / 38.0)))


class LevelPublisher:
    """Worker side of the live meter: one memory-mapped struct, rewritten every block. No JSON, no fsync."""

    def __init__(self, meeting_dir: str):
        path = os.path.join(meeting_dir, LEVELS_FILE)
        with open(path, "ab"):
            pass
        if os.path.getsize(path) < _LV.size:  # never truncate: the pill may have it mapped (Windows refuses)
            with open(path, "r+b") as f:
                f.write(b"\0" * _LV.size)
        self._f = open(path, "r+b")
        self._mm = mmap.mmap(self._f.fileno(), _LV.size)
        self._seq = 0
        self._iir = [0.0, 0.0]

    def publish(self, me_block: np.ndarray, them_block: np.ndarray) -> None:
        vals = []
        for lane, blk in enumerate((me_block, them_block)):
            for sub in np.array_split(blk, LEVEL_SUBS):
                self._iir[lane] = self._iir[lane] * 0.3 + level_norm(sub) * 0.7  # the callback smoothing in engine.py
                vals.append(self._iir[lane])
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        self._mm[:_LV.size] = _LV.pack(*vals, self._seq)

    def close(self) -> None:
        try:
            self._mm.close()
            self._f.close()
        except Exception:
            pass


class LevelReader:
    """App side: read() -> (seq, me[4], them[4]) or None. Opens lazily, so it may start before the worker."""

    def __init__(self, meeting_dir: str):
        self._path = os.path.join(meeting_dir, LEVELS_FILE)
        self._f = self._mm = None

    def read(self):
        try:
            if self._mm is None:
                self._f = open(self._path, "rb")
                self._mm = mmap.mmap(self._f.fileno(), _LV.size, access=mmap.ACCESS_READ)
            for _ in range(3):  # seqlock: seq is the last field; retry a torn read
                a = _LV.unpack(self._mm[:_LV.size])
                b = _LV.unpack(self._mm[:_LV.size])
                if a[8] == b[8]:
                    return a[8], a[0:4], a[4:8]
        except (OSError, ValueError):
            self.close()
        return None

    def close(self) -> None:
        try:
            if self._mm is not None:
                self._mm.close()
            if self._f is not None:
                self._f.close()
        except Exception:
            pass
        self._f = self._mm = None


def send_worker_command(meeting_dir: str, cmd: str) -> None:
    _atomic_write_json(
        os.path.join(meeting_dir, "control.json"),
        {"cmd": cmd, "ts": time.time()},
        fsync=False,
    )


def stop_worker(meeting_dir: str, timeout: float = 5.0) -> bool:
    send_worker_command(meeting_dir, "stop")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = read_status(meeting_dir)
        if st and st.get("state") in ("stopped", "ready", "processing"):
            return True
        time.sleep(0.1)
    return False


def start_worker(
    meeting_dir: str,
    synthetic: bool = False,
    segment_seconds: float = 10.0,
    heartbeat_interval: float = 2.0,
    preroll_s: float = 0.0,
) -> int:
    init_meeting_dir(meeting_dir)
    ctrl_path = os.path.join(meeting_dir, "control.json")
    if os.path.exists(ctrl_path):
        try:
            os.remove(ctrl_path)
        except OSError:
            pass
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "--worker",
        "--meeting-dir",
        os.path.abspath(meeting_dir),
        "--segment-seconds",
        str(segment_seconds),
        "--heartbeat-interval",
        str(heartbeat_interval),
    ]
    if synthetic:
        cmd.append("--synthetic")
    if preroll_s:
        cmd += ["--preroll-seconds", str(preroll_s)]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    return proc.pid


def _format_saved_duration(seconds: float) -> str:
    if seconds >= 60.0:
        mins = max(1, int(round(seconds / 60.0)))
        return f"{mins} min"
    return f"{max(1, int(round(seconds)))} s"


def check_recovery(
    store_root: str,
    now: float | None = None,
    heartbeat_timeout: float = 10.0,
    on_toast=None,
) -> list[dict]:
    """Scan store_root for meetings left in 'recording' with a stale (>10s) heartbeat."""
    if not os.path.isdir(store_root):
        return []
    now_ts = time.time() if now is None else float(now)
    recovered = []
    for entry in sorted(os.listdir(store_root)):
        mdir = os.path.join(store_root, entry)
        if not os.path.isdir(mdir):
            continue
        manifest = read_manifest(mdir)
        if not manifest or manifest.get("state") != "recording":
            continue
        status = read_status(mdir)
        hb_at = float(status.get("heartbeat_at", 0.0)) if status else 0.0
        if (now_ts - hb_at) > heartbeat_timeout:
            saved_s = saved_audio_seconds(mdir, "me")
            manifest["state"] = "needs_resume"
            write_manifest(mdir, manifest)
            label = _format_saved_duration(saved_s)
            toast = {
                "meeting_id": manifest["id"],
                "meeting_dir": mdir,
                "saved_seconds": saved_s,
                "message": f"Recording stopped unexpectedly ({label} saved). Resume recording this call?",
                "actions": ["Resume", "Decline"],
            }
            if on_toast:
                on_toast(toast)
            recovered.append(toast)
    return recovered


def meeting_sessions(manifest: dict) -> list:
    """Recording sessions as (wall0, wall1, audio0): wall clock span and where the session starts on the audio
    timeline. Every resume (crash or user) appends a point, so a meeting continued days later still aligns
    Discord speaking events and dictation spans, which are logged in wall time."""
    start_wall, start_audio, out = float(manifest.get("record_started_wall") or 0.0), 0.0, []
    for p in manifest.get("resume_points") or []:
        out.append((start_wall, float(p.get("prev_end_wall") or p["wall"]), start_audio))
        start_wall, start_audio = float(p["wall"]), float(p["audio_t"])
    out.append((start_wall, None, start_audio))
    return out


def wall_to_audio(manifest: dict, wall: float) -> float:
    """Wall clock time to seconds on the audio timeline (the latest session that began at or before it)."""
    w0, a0 = float(manifest.get("record_started_wall") or wall), 0.0
    for sw0, _sw1, sa0 in meeting_sessions(manifest):
        if sw0 and wall >= sw0:
            w0, a0 = sw0, sa0
    return max(0.0, a0 + wall - w0)


def rehydrate_audio(meeting_dir: str, segment_seconds: float = 10.0) -> float:
    """Opus back into FLAC segments. Finalize deletes the segments after encoding, so a finished meeting
    has to get them back before a worker can append to its timeline. Opus is removed only after every
    track verifies; on any failure the new segments are removed and the Opus is left untouched."""
    audio = os.path.join(meeting_dir, "audio")
    written, seconds = [], 0.0
    try:
        for track in ("me", "them"):
            opus = os.path.join(audio, f"{track}.opus")
            if _segment_files(meeting_dir, track) or not os.path.exists(opus):
                continue
            data, sr = sf.read(opus, dtype="int16")
            if data.ndim > 1:
                data = data[:, 0]
            per = int(round(segment_seconds * sr))
            for i in range(0, len(data), per):
                written.append(write_flac_segment(meeting_dir, track, i // per + 1, data[i:i + per], sr))
            if abs(saved_audio_seconds(meeting_dir, track) - len(data) / float(sr)) > 0.5:
                raise RuntimeError(f"{track}: rebuilt {saved_audio_seconds(meeting_dir, track):.1f}s, expected {len(data) / float(sr):.1f}s")
            seconds = max(seconds, len(data) / float(sr))
    except Exception:
        for p in written:
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    for track in ("me", "them"):
        for name in (f"{track}.opus", f"{track}.opus.idx"):  # finalize re-encodes the whole call from the new segments
            try:
                os.remove(os.path.join(audio, name))
            except OSError:
                pass
    return seconds


def resume_meeting(
    meeting_dir: str,
    gap_seconds: float | None = None,
    reason: str = "worker_crash_recovery",
    source: dict | None = None,
    **worker_kwargs,
) -> int:
    """Continue a meeting in its own folder: crash recovery, or (reason 'user_resume') any past meeting.
    A user resume rebuilds the audio from Opus, sets the cached transcript and note aside so finalize
    rebuilds them over the whole call, and points the worker at the call audio that is live now."""
    manifest = read_manifest(meeting_dir)
    if not manifest:
        raise FileNotFoundError(f"No manifest.json in {meeting_dir}")
    now = time.time()
    st = read_status(meeting_dir)
    if reason == "user_resume":
        if st and (st.get("state") in ("recording", "paused")) and now - float(st.get("heartbeat_at", 0) or 0) < 15:
            raise RuntimeError("This meeting is still recording")
        if manifest.get("audio_deleted_at"):
            raise RuntimeError("The audio was deleted by the retention sweep, so there is nothing to continue from")
        rehydrate_audio(meeting_dir)
    saved_s = saved_audio_seconds(meeting_dir, "me")
    try:
        ended_wall = datetime.datetime.fromisoformat(manifest["ended"]).timestamp() if manifest.get("ended") else None
    except (TypeError, ValueError):
        ended_wall = None
    hb_at = float(st.get("heartbeat_at", now)) if st else now
    prev_end = ended_wall or hb_at
    if gap_seconds is None:
        gap_seconds = max(0.0, now - prev_end)
    manifest.setdefault("gaps", []).append({"t": round(saved_s, 2), "seconds": round(float(gap_seconds), 2), "reason": reason})
    point = {"wall": now, "audio_t": round(saved_s, 3), "prev_end_wall": prev_end}
    if reason == "user_resume":
        point["floor_s"] = point["audio_t"]  # chunks exist for everything before this: only newer audio is chunked
    manifest.setdefault("resume_points", []).append(point)
    if reason == "user_resume":
        for name in ("transcript.json", "note.json"):  # cached stages: kept as .prev, rebuilt over the whole call
            p = os.path.join(meeting_dir, name)
            if os.path.exists(p):
                os.replace(p, p + ".prev")
        manifest.pop("note_path", None)
        manifest.pop("transcript_path", None)
        manifest["audio_pending"] = False
        manifest["error"] = None
        if source:
            manifest["source"] = source
    manifest["ended"] = None
    manifest["state"] = "recording"
    write_manifest(meeting_dir, manifest)
    return start_worker(meeting_dir, **worker_kwargs)


def decline_resume(meeting_dir: str, pipeline_fn=None) -> dict:
    manifest = read_manifest(meeting_dir)
    if not manifest:
        raise FileNotFoundError(f"No manifest.json in {meeting_dir}")
    manifest["state"] = "processing"
    manifest["ended"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    write_manifest(meeting_dir, manifest)
    if pipeline_fn is not None:
        pipeline_fn(meeting_dir)
    else:
        manifest["state"] = "ready"
        write_manifest(meeting_dir, manifest)
    return manifest


def _note_stall(meeting_dir: str, at_s: float, behind_s: float) -> None:
    """The worker fell more than a second behind real time (a starved machine) and resynced instead of bursting:
    that stretch of the call is missing from the audio. Recorded so the transcript can say so (2026-10-10: a loaded
    machine left a 25 minute stretch 43% captured, with no trace)."""
    man = read_manifest(meeting_dir) or {}
    man.setdefault("gaps", []).append({"t": round(at_s, 1), "seconds": round(behind_s, 1), "reason": "worker_stall"})
    write_manifest(meeting_dir, man)


def _note_capture_restart(meeting_dir: str, at_s: float, cap) -> None:
    man = read_manifest(meeting_dir) or {}
    man.setdefault("capture_restarts", []).append(
        {"t": round(at_s, 1), "pid": cap.src.get("pid"), "error": cap.error or "capture helper exited"})
    write_manifest(meeting_dir, man)


class CallCapture:
    """ScribeCapture.exe drained on its own thread into a bounded buffer, read like MicCapture.

    Paid 2026-10-09: the worker read the pipe inline and then slept, so it ran ~0.6% slower than
    real time; the 4 KB pipe filled, ScribeCapture blocked on write, its wall-clock zero fill took
    over and after ~2 minutes the call track was silence (meetings 2003 and 2123). Draining here
    keeps the helper unblocked. If the helper exits (stream invalidated, Discord restarted), the
    worker respawns it, re-resolving Discord's pid."""

    MAX_BUFFER_S = 2.0
    RETRY_S = 3.0

    def __init__(self, src: dict, exe: str | None = None):
        self.src = dict(src or {})
        self.exe = exe or os.path.join(HERE, "ScribeCapture.exe")
        self.proc = None
        self.error = ""
        self.restarts = 0
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._next_try = 0.0

    def _args(self):
        if self.src.get("kind") == "app" and self.src.get("pid"):
            return [self.exe, "--pid", str(self.src["pid"])]
        return [self.exe, "--device", str(self.src.get("device") or "default")]

    def _refresh_pid(self):
        if self.src.get("kind") != "app" or (self.src.get("app") or "").lower() != "discord":
            return
        try:
            import meeting_service as msvc  # lazy: meeting_service imports this module

            ds = msvc.discord_source(msvc.list_audio_sessions())
            if ds:
                self.src["pid"] = ds["pid"]
        except Exception:
            pass

    def start(self) -> bool:
        if not os.path.exists(self.exe):
            return False
        try:
            self.proc = subprocess.Popen(
                self._args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0,
            )
        except OSError as e:
            self.proc, self.error = None, str(e)
            return False
        proc = self.proc
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        threading.Thread(target=self._drain_err, args=(proc,), daemon=True).start()
        return True

    def _pump(self, proc):
        cap = int(SAMPLE_RATE * 2 * self.MAX_BUFFER_S)
        while True:
            b = proc.stdout.read1(65536)
            if not b:
                return
            with self._lock:
                self._buf += b
                if len(self._buf) > cap:  # the worker stalled: keep the newest audio, stay in real time
                    del self._buf[: len(self._buf) - cap]

    def _drain_err(self, proc):
        for line in proc.stderr:
            line = line.decode("utf-8", "replace").strip()
            if '"error"' in line:
                self.error = line[:300]

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def tick(self, now: float) -> bool:
        """Respawn a dead helper (with backoff). Returns True when it was restarted this call."""
        if self.alive() or now < self._next_try:
            return False
        self._next_try = now + self.RETRY_S
        self._refresh_pid()
        if self.start():
            self.restarts += 1
            return True
        return False

    def read(self, n: int) -> np.ndarray:
        with self._lock:
            take = min(len(self._buf), n * 2) // 2 * 2
            out = bytes(self._buf[:take])
            del self._buf[:take]
        a = np.frombuffer(out, dtype=np.int16)
        return a if len(a) == n else np.concatenate((a, np.zeros(n - len(a), dtype=np.int16)))

    def close(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


PREROLL_LEASE_S = 180.0


def stop_buffer(buffer_dir: str, timeout: float = 8.0) -> None:
    """Ask a buffer worker to stop and wait until it has flushed its last segment."""
    send_worker_command(buffer_dir, "stop")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = read_status(buffer_dir) or {}
        if st.get("state") == "stopped" or not _pid_running(st.get("pid")):
            return
        time.sleep(0.1)


def _pid_running(pid) -> bool:
    try:
        if not pid:
            return False
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = ctypes.c_void_p
            h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong(0)
            ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
            k.CloseHandle(ctypes.c_void_p(h))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def promote_preroll(buffer_dir: str, meeting_dir: str, handoff_s: float = 1.5) -> float:
    """Turn a buffer into the start of a meeting: stop it, move its segments into meeting_dir (renumbered from 1),
    and make the meeting's clock start where the buffered audio starts. The new worker then continues from the
    saved seconds exactly like a resume; the stop-to-start handoff is a recorded gap of about a second.
    Returns the seconds adopted (0.0 when the buffer held nothing)."""
    stop_buffer(buffer_dir)
    pre = read_manifest(buffer_dir) or {}
    for track in ("me", "them"):
        os.makedirs(os.path.join(meeting_dir, track), exist_ok=True)
        for i, path in enumerate(_segment_files(buffer_dir, track), start=1):
            shutil.move(path, os.path.join(meeting_dir, track, f"seg-{i:05d}.flac"))
    seconds = saved_audio_seconds(meeting_dir, "me")
    if seconds <= 0:
        return 0.0
    man = read_manifest(meeting_dir) or {}
    now = time.time()
    start_wall = float(pre.get("ring_start_wall") or pre.get("record_started_wall") or (now - seconds))
    spk = os.path.join(buffer_dir, "speaking.jsonl")
    if os.path.exists(spk):  # who spoke while it was buffering; events from pruned audio are dropped
        with open(spk, "r", encoding="utf-8") as src, open(os.path.join(meeting_dir, "speaking.jsonl"), "a", encoding="utf-8") as dst:
            for line in src:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") in ("start", "stop") and float(ev.get("wall") or 0) < start_wall:
                    continue
                dst.write(line if line.endswith("\n") else line + "\n")
    man["record_started_wall"] = start_wall
    man["started"] = datetime.datetime.fromtimestamp(start_wall, datetime.timezone.utc).isoformat()
    man["preroll_s"] = round(seconds, 1)
    man.setdefault("resume_points", []).append({"wall": now + handoff_s, "audio_t": round(seconds, 3), "prev_end_wall": now})
    man.setdefault("gaps", []).append({"t": round(seconds, 2), "seconds": handoff_s, "reason": "preroll_handoff"})
    write_manifest(meeting_dir, man)
    return seconds


def prune_ring(meeting_dir: str, keep_s: float, segment_seconds: float) -> None:
    """Keep only the newest keep_s seconds of segments. ring_pruned_s counts what was dropped, so the wall time of
    the first kept sample stays known (ring_start_wall = record_started_wall + ring_pruned_s)."""
    keep = max(1, int(keep_s // segment_seconds))
    me, them = _segment_files(meeting_dir, "me"), _segment_files(meeting_dir, "them")
    extra = len(me) - keep
    if extra <= 0:
        return
    for path in me[:extra] + them[:extra]:
        try:
            os.remove(path)
        except OSError:
            pass
    man = read_manifest(meeting_dir) or {}
    man["ring_pruned_s"] = round(float(man.get("ring_pruned_s") or 0.0) + extra * segment_seconds, 2)
    man["ring_start_wall"] = float(man.get("record_started_wall") or time.time()) + man["ring_pruned_s"]
    write_manifest(meeting_dir, man)


def run_worker_loop(
    meeting_dir: str,
    synthetic: bool = False,
    segment_seconds: float = 10.0,
    heartbeat_interval: float = 2.0,
    finalize: bool | None = None,
    mic_sd=None,
    preroll_s: float = 0.0,
) -> int:
    """Record until control.json says stop, then (real recordings only) run the post-call pipeline.
    preroll_s > 0 is the retroactive buffer: the same capture, but only the last preroll_s seconds are kept,
    nothing is transcribed, and it stops itself when the app stops sending its lease (control.json ts)."""
    if finalize is None:
        finalize = not synthetic and not preroll_s
    init_meeting_dir(meeting_dir)
    manifest = read_manifest(meeting_dir) or {}
    if preroll_s and manifest.get("state") != "buffering":
        manifest["state"] = "buffering"  # not 'recording': nothing adopts or lists a buffer as a meeting
        write_manifest(meeting_dir, manifest)
    if not manifest.get("record_started_wall"):
        # lets finalize align Discord speaking events (wall clock) to the audio timeline
        manifest["record_started_wall"] = time.time()
        write_manifest(meeting_dir, manifest)
    seg_idx = _highest_segment_index(meeting_dir)
    base_seconds = saved_audio_seconds(meeting_dir, "me")
    block_s = 0.1
    block_frames = int(round(SAMPLE_RATE * block_s))
    seg_target_frames = int(round(SAMPLE_RATE * segment_seconds))

    me_buf = []
    them_buf = []
    me_frames_in_seg = 0
    me_total_frames = int(round(base_seconds * SAMPLE_RATE))
    them_total_frames = int(round(saved_audio_seconds(meeting_dir, "them") * SAMPLE_RATE))
    state = "recording"
    last_hb = 0.0
    last_chunk_time = None

    cap_proc = None
    live = None
    mic = None
    mic_final = []  # the last live mic_ok, so the post-close heartbeats do not report a closed mic as dead
    lvl_me = lvl_them = 0.0
    try:
        lv_pub = LevelPublisher(meeting_dir)
    except Exception:
        lv_pub = None  # the meter is cosmetic: never let it stop a recording
    if not synthetic:
        src = manifest.get("source") or {}
        mic = MicCapture(_configured_mic_device(src), sd_module=mic_sd)
        mic.start()
        live = None
        if finalize:
            try:
                import meeting_pipeline as mp  # lazy: meeting_pipeline imports this module

                live = mp.make_live_transcriber(meeting_dir)
                if live:
                    live.start()
            except Exception as e:
                print(f"[meeting_recorder] live transcription off: {e}")
                live = None
        cap_proc = CallCapture(src)
        if not os.path.exists(cap_proc.exe):
            cap_proc = None
        else:
            cap_proc.start()  # a failed first start is retried by cap_proc.tick in the loop

    def _emit_heartbeat(cur_state: str, rms_me: float = 0.0, rms_them: float = 0.0):
        write_status(
            meeting_dir,
            {
                "pid": os.getpid(),
                "state": "buffering" if preroll_s and cur_state == "recording" else cur_state,
                "me_seconds": round(me_total_frames / float(SAMPLE_RATE), 2),
                "them_seconds": round(them_total_frames / float(SAMPLE_RATE), 2),
                "me_segments": seg_idx,
                "them_segments": seg_idx,
                "last_chunk_time": last_chunk_time,
                "levels": {"me": round(rms_me, 5), "them": round(rms_them, 5)},
                "mic_ok": True if mic is None and synthetic else (mic_final[0] if mic_final else bool(mic and mic.ok)),
                "heartbeat_at": time.time(),
            },
        )

    _emit_heartbeat(state)
    last_hb = time.monotonic()
    ctrl_path = os.path.join(meeting_dir, "control.json")

    next_tick = time.monotonic()
    booted_wall = time.time()
    try:
        while True:
            ctrl = _read_json(ctrl_path)
            if ctrl:
                cmd = ctrl.get("cmd")
                if cmd == "stop":
                    break
                elif cmd == "pause":
                    state = "paused"
                elif cmd == "resume":
                    state = "recording"
            if preroll_s and time.time() - float((ctrl or {}).get("ts") or booted_wall) > PREROLL_LEASE_S:
                break  # the app died or left without stopping the buffer: never keep recording unsupervised

            t_step = time.monotonic()
            if state == "recording":
                if synthetic or cap_proc is None:
                    me_block = mic.read(block_frames) if mic is not None else np.zeros(block_frames, dtype=np.int16)
                    them_block = np.zeros(block_frames, dtype=np.int16)
                else:
                    if cap_proc.tick(t_step):
                        _note_capture_restart(meeting_dir, me_total_frames / float(SAMPLE_RATE), cap_proc)
                    them_block = cap_proc.read(block_frames)
                    me_block = mic.read(block_frames) if mic is not None else np.zeros(block_frames, dtype=np.int16)

                if lv_pub is not None:
                    lv_pub.publish(me_block, them_block)
                lvl_me = max(lvl_me, _level(me_block))
                lvl_them = max(lvl_them, _level(them_block))
                me_buf.append(me_block)
                them_buf.append(them_block)
                me_frames_in_seg += len(me_block)
                me_total_frames += len(me_block)
                them_total_frames += len(them_block)

                if me_frames_in_seg >= seg_target_frames:
                    seg_idx += 1
                    me_pcm = np.concatenate(me_buf)[:seg_target_frames]
                    them_pcm = np.concatenate(them_buf)[:seg_target_frames]
                    write_flac_segment(meeting_dir, "me", seg_idx, me_pcm, SAMPLE_RATE, fsync=not preroll_s)
                    write_flac_segment(meeting_dir, "them", seg_idx, them_pcm, SAMPLE_RATE, fsync=not preroll_s)
                    me_buf.clear()
                    them_buf.clear()
                    me_frames_in_seg = 0
                    last_chunk_time = time.time()
                    if preroll_s:
                        prune_ring(meeting_dir, preroll_s, segment_seconds)

            now_m = time.monotonic()
            if (now_m - last_hb) >= heartbeat_interval:
                _emit_heartbeat(state, lvl_me, lvl_them)
                lvl_me = lvl_them = 0.0
                last_hb = now_m

            # fixed schedule, not "sleep block_s after the work": per-step overhead used to add up to
            # ~0.6% slower than real time; after a long stall, resync instead of bursting to catch up
            next_tick += block_s
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1.0:
                if not preroll_s:
                    _note_stall(meeting_dir, me_total_frames / float(SAMPLE_RATE), -delay)  # audio is not caught up: say so
                next_tick = time.monotonic()
    finally:
        if lv_pub is not None:
            lv_pub.close()
        if live is not None:
            live.stop()  # before the "stopped" heartbeat: the app's finalize may start right after it
        if mic is not None:
            mic_final.append(mic.ok)
            mic.close()
        if cap_proc is not None:
            cap_proc.close()
        if me_frames_in_seg > 0 and me_buf and them_buf:
            seg_idx += 1
            write_flac_segment(meeting_dir, "me", seg_idx, np.concatenate(me_buf), SAMPLE_RATE)
            write_flac_segment(meeting_dir, "them", seg_idx, np.concatenate(them_buf), SAMPLE_RATE)
        manifest = read_manifest(meeting_dir) or {}
        manifest["ended"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest["state"] = "buffering" if preroll_s else "processing"  # a buffer is promoted or deleted, never finalized
        write_manifest(meeting_dir, manifest)
        _emit_heartbeat("stopped")
    if finalize:
        try:
            import meeting_pipeline as mp  # lazy: meeting_pipeline imports this module

            mp.finalize_meeting(meeting_dir)
        except Exception as e:  # never leave the state stuck on 'processing'
            m = read_manifest(meeting_dir) or {}
            m["state"], m["error"] = "failed", str(e)[:300]
            write_manifest(meeting_dir, m)
        _emit_heartbeat((read_manifest(meeting_dir) or {}).get("state", "stopped"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Leonidas Scribe detached meeting recorder worker")
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--meeting-dir", required=True)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--segment-seconds", type=float, default=10.0)
    ap.add_argument("--heartbeat-interval", type=float, default=2.0)
    ap.add_argument("--preroll-seconds", type=float, default=0.0)
    args = ap.parse_args(argv)
    if args.worker:
        return run_worker_loop(
            args.meeting_dir,
            synthetic=args.synthetic,
            segment_seconds=args.segment_seconds,
            heartbeat_interval=args.heartbeat_interval,
            preroll_s=args.preroll_seconds,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
