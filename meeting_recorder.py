"""Detached meeting recorder worker, FLAC segment writer, heartbeat, and crash recovery (S1).

Runs out-of-process (`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`) so a Scribe crash
or self-restart never interrupts recording. Writes 10 s FLAC segments via *.tmp + fsync +
os.replace and updates status.json every 2 s.
"""
import argparse
import datetime
import json
import os
import re
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
) -> str:
    track_dir = os.path.join(meeting_dir, track)
    os.makedirs(track_dir, exist_ok=True)
    final_path = os.path.join(track_dir, f"seg-{index:05d}.flac")
    tmp_path = final_path + ".tmp"
    arr = np.asarray(pcm_int16, dtype=np.int16)
    with open(tmp_path, "wb") as f:
        sf.write(f, arr, sample_rate, format="FLAC", subtype="PCM_16")
        f.flush()
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


def resume_meeting(
    meeting_dir: str,
    gap_seconds: float | None = None,
    **worker_kwargs,
) -> int:
    manifest = read_manifest(meeting_dir)
    if not manifest:
        raise FileNotFoundError(f"No manifest.json in {meeting_dir}")
    saved_s = saved_audio_seconds(meeting_dir, "me")
    if gap_seconds is None:
        st = read_status(meeting_dir)
        hb_at = float(st.get("heartbeat_at", time.time())) if st else time.time()
        gap_seconds = max(0.0, time.time() - hb_at)
    manifest.setdefault("gaps", []).append(
        {
            "t": round(saved_s, 2),
            "seconds": round(float(gap_seconds), 2),
            "reason": "worker_crash_recovery",
        }
    )
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


def run_worker_loop(
    meeting_dir: str,
    synthetic: bool = False,
    segment_seconds: float = 10.0,
    heartbeat_interval: float = 2.0,
    finalize: bool | None = None,
    mic_sd=None,
) -> int:
    """Record until control.json says stop, then (real recordings only) run the post-call pipeline."""
    if finalize is None:
        finalize = not synthetic
    init_meeting_dir(meeting_dir)
    manifest = read_manifest(meeting_dir) or {}
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
    mic = None
    lvl_me = lvl_them = 0.0
    if not synthetic:
        src = manifest.get("source") or {}
        mic = MicCapture(_configured_mic_device(src), sd_module=mic_sd)
        mic.start()
        exe = os.path.join(HERE, "ScribeCapture.exe")
        if os.path.exists(exe):
            args = [exe, "--pid", str(src["pid"])] if src.get("kind") == "app" and src.get("pid") else [
                exe, "--device", str(src.get("device") or "default")
            ]
            try:
                cap_proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0,
                )
            except OSError:
                cap_proc = None

    def _emit_heartbeat(cur_state: str, rms_me: float = 0.0, rms_them: float = 0.0):
        write_status(
            meeting_dir,
            {
                "pid": os.getpid(),
                "state": cur_state,
                "me_seconds": round(me_total_frames / float(SAMPLE_RATE), 2),
                "them_seconds": round(them_total_frames / float(SAMPLE_RATE), 2),
                "me_segments": seg_idx,
                "them_segments": seg_idx,
                "last_chunk_time": last_chunk_time,
                "levels": {"me": round(rms_me, 5), "them": round(rms_them, 5)},
                "mic_ok": True if mic is None and synthetic else bool(mic and mic.ok),
                "heartbeat_at": time.time(),
            },
        )

    _emit_heartbeat(state)
    last_hb = time.monotonic()
    ctrl_path = os.path.join(meeting_dir, "control.json")

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

            t_step = time.monotonic()
            if state == "recording":
                if synthetic or cap_proc is None:
                    me_block = mic.read(block_frames) if mic is not None else np.zeros(block_frames, dtype=np.int16)
                    them_block = np.zeros(block_frames, dtype=np.int16)
                else:
                    raw = cap_proc.stdout.read(block_frames * 2)
                    them_block = (
                        np.frombuffer(raw, dtype=np.int16)
                        if raw and len(raw) == block_frames * 2
                        else np.zeros(block_frames, dtype=np.int16)
                    )
                    me_block = mic.read(block_frames) if mic is not None else np.zeros(block_frames, dtype=np.int16)

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
                    write_flac_segment(meeting_dir, "me", seg_idx, me_pcm, SAMPLE_RATE)
                    write_flac_segment(meeting_dir, "them", seg_idx, them_pcm, SAMPLE_RATE)
                    me_buf.clear()
                    them_buf.clear()
                    me_frames_in_seg = 0
                    last_chunk_time = time.time()

            now_m = time.monotonic()
            if (now_m - last_hb) >= heartbeat_interval:
                _emit_heartbeat(state, lvl_me, lvl_them)
                lvl_me = lvl_them = 0.0
                last_hb = now_m

            elapsed = time.monotonic() - t_step
            if elapsed < block_s:
                time.sleep(block_s - elapsed)
    finally:
        if mic is not None:
            mic.close()
        if cap_proc is not None:
            try:
                if cap_proc.stdin:
                    cap_proc.stdin.close()
                cap_proc.wait(timeout=2)
            except Exception:
                try:
                    cap_proc.kill()
                except Exception:
                    pass
        if me_frames_in_seg > 0 and me_buf and them_buf:
            seg_idx += 1
            write_flac_segment(meeting_dir, "me", seg_idx, np.concatenate(me_buf), SAMPLE_RATE)
            write_flac_segment(meeting_dir, "them", seg_idx, np.concatenate(them_buf), SAMPLE_RATE)
        manifest = read_manifest(meeting_dir) or {}
        manifest["ended"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest["state"] = "processing"
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
    args = ap.parse_args(argv)
    if args.worker:
        return run_worker_loop(
            args.meeting_dir,
            synthetic=args.synthetic,
            segment_seconds=args.segment_seconds,
            heartbeat_interval=args.heartbeat_interval,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
