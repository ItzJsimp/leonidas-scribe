"""Call detection, Discord/app rule evaluation, 2-minute auto-stop + rejoin tracker,
Ask, What did I miss?, and audio file Import (S5).
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meeting_pipeline as mp
import meeting_recorder as mr
import meeting_speakers as ms
import meeting_store as mst

IGNORED_PROCESSES = {"system sounds", "systemsettings", "idle", "audiodg", "discord"}
DISPLAY_APP_NAMES = {
    "chrome": "Chrome",
    "msedge": "Edge",
    "vivaldi": "Vivaldi",
    "firefox": "Firefox",
    "brave": "Brave",
    "zoom": "Zoom",
    "teams": "Teams",
    "ms-teams": "Teams",
    "slack": "Slack",
    "telegram": "Telegram",
}


def evaluate_discord_call_event(ev: dict, cfg: dict) -> dict:
    """Evaluate a Discord channel event against channel -> server -> DM -> global rules."""
    m_cfg = (cfg or {}).get("meetings") or {}
    if not m_cfg.get("enabled", True):
        return {"should_prompt": False, "action": "never", "destination_id": None, "template_id": "default", "toast": None}

    cid = ev.get("channel_id")
    if not cid:
        return {"should_prompt": False, "action": "none", "destination_id": None, "template_id": "default", "toast": None}

    cname = ev.get("channel") or ""
    gid = ev.get("guild_id")
    gname = ev.get("guild") or ""
    rules = ((m_cfg.get("discord") or {}).get("rules")) or []

    matched_rule = None
    # 1. Channel rule
    for r in rules:
        if r.get("scope") == "channel" and (
            (r.get("id") and str(r.get("id")) == str(cid))
            or (r.get("name") and r.get("name").lower() == cname.lower())
        ):
            matched_rule = r
            break

    # 2. Server rule
    if matched_rule is None and gid:
        for r in rules:
            if r.get("scope") == "server" and (
                (r.get("id") and str(r.get("id")) == str(gid))
                or (r.get("name") and r.get("name").lower() == gname.lower())
            ):
                matched_rule = r
                break

    # 3. DM rule
    if matched_rule is None and not gid:
        for r in rules:
            if r.get("scope") == "dm":
                matched_rule = r
                break

    default_action = "ask" if m_cfg.get("call_prompts", True) else "never"
    action = (matched_rule.get("action") if matched_rule else default_action) or default_action
    dest_id = matched_rule.get("destination_id") if matched_rule else None
    tmpl_id = (matched_rule.get("template_id") if matched_rule else None) or "default"

    place = gname or cname or "Direct Message"
    should_prompt = action == "ask"
    toast = (
        {
            "kind": "discord_call",
            "message": f"Discord call in {place}. Record?",
            "buttons": ["Record", "Not now", "Never here"],
            "destination_id": dest_id,
            "template_id": tmpl_id,
            "channel_id": cid,
            "guild_id": gid,
            "place": place,
        }
        if should_prompt
        else None
    )
    return {
        "should_prompt": should_prompt,
        "action": action,
        "destination_id": dest_id,
        "template_id": tmpl_id,
        "toast": toast,
    }


def apply_never_here(cfg: dict, ev: dict) -> dict:
    """Persist a 'never' rule for the Discord server (or channel/DM) when user clicks 'Never here'."""
    m_cfg = cfg.setdefault("meetings", {})
    d_cfg = m_cfg.setdefault("discord", {})
    rules = d_cfg.setdefault("rules", [])
    gid = ev.get("guild_id")
    gname = ev.get("guild")
    cid = ev.get("channel_id")
    cname = ev.get("channel")

    if gid:
        rule = {"scope": "server", "id": str(gid), "name": gname or str(gid), "action": "never"}
    else:
        rule = {"scope": "channel", "id": str(cid or ""), "name": cname or "DM", "action": "never"}

    for existing in rules:
        if existing.get("scope") == rule["scope"] and str(existing.get("id")) == rule["id"]:
            existing["action"] = "never"
            return rule
    rules.append(rule)
    return rule


def detect_other_app_calls(sessions: list[dict], cfg: dict) -> list[dict]:
    """An app (excluding Discord/Scribe/system) with an active capture session AND an active render session = call."""
    m_cfg = (cfg or {}).get("meetings") or {}
    if not m_cfg.get("enabled", True) or not m_cfg.get("call_prompts", True):
        return []
    app_prompts = {k.lower(): v for k, v in (m_cfg.get("app_prompts") or {}).items()}

    capture_pids: dict[str, int] = {}
    render_pids: dict[str, int] = {}
    raw_names: dict[str, str] = {}

    for s in sessions or []:
        if s.get("state") != "active":
            continue
        proc = (s.get("process") or "").strip()
        proc_l = proc.lower()
        if not proc_l or proc_l in IGNORED_PROCESSES or "python" in proc_l or "scribecapture" in proc_l:
            continue
        pid = int(s.get("pid") or 0)
        if pid <= 0:
            continue
        raw_names[proc_l] = proc
        if s.get("flow") == "capture":
            capture_pids[proc_l] = pid
        elif s.get("flow") == "render":
            render_pids[proc_l] = pid

    detected = []
    for proc_l, cap_pid in capture_pids.items():
        if proc_l not in render_pids:
            continue
        disp = DISPLAY_APP_NAMES.get(proc_l, raw_names[proc_l].capitalize())
        rule_action = app_prompts.get(proc_l) or app_prompts.get(disp.lower()) or "ask"
        if rule_action == "never":
            continue
        detected.append(
            {
                "app": disp,
                "process": raw_names[proc_l],
                "pid": render_pids[proc_l],
                "capture_pid": cap_pid,
                "toast": {
                    "kind": "app_call",
                    "message": f"{disp} is using your mic. Record?",
                    "buttons": ["Record", "Not now", "Never here"],
                    "app": disp,
                    "pid": render_pids[proc_l],
                },
            }
        )
    return detected


class CallLifecycleTracker:
    """Tracks call inactivity; auto-stops after 2 min (120 s), or continues the same meeting if rejoined within 2 min."""

    def __init__(self, auto_stop_seconds: float = 120.0):
        self.auto_stop_seconds = float(auto_stop_seconds)
        self.meeting_id: str | None = None
        self.inactive_since: float | None = None

    def on_active(self, meeting_id: str, now: float | None = None) -> dict:
        now_ts = time.monotonic() if now is None else float(now)
        if self.meeting_id == meeting_id and self.inactive_since is not None:
            elapsed = now_ts - self.inactive_since
            self.inactive_since = None
            if elapsed < self.auto_stop_seconds:
                return {"action": "continue", "meeting_id": meeting_id}
        self.meeting_id = meeting_id
        self.inactive_since = None
        return {"action": "active", "meeting_id": meeting_id}

    def on_inactive(self, now: float | None = None) -> dict:
        now_ts = time.monotonic() if now is None else float(now)
        if self.inactive_since is None:
            self.inactive_since = now_ts
        return {"action": "waiting", "meeting_id": self.meeting_id}

    def check(self, now: float | None = None) -> dict:
        now_ts = time.monotonic() if now is None else float(now)
        if self.meeting_id and self.inactive_since is not None:
            if (now_ts - self.inactive_since) >= self.auto_stop_seconds:
                mid = self.meeting_id
                self.meeting_id = None
                self.inactive_since = None
                return {"action": "auto_stop", "meeting_id": mid}
            return {"action": "waiting", "meeting_id": self.meeting_id}
        return {"action": "idle", "meeting_id": self.meeting_id}


def what_did_i_miss(
    meeting_dir: str,
    window_minutes: int = 5,
    now_recording_s: float | None = None,
    llm_fn=None,
) -> dict:
    """Summarize the last 2/5/10 minutes of finished chunks in second person (2-3 sentences)."""
    chunks = mp.read_chunks_jsonl(meeting_dir)
    if not chunks:
        return {"answer": "You have not missed any speech in the selected window.", "chunks_used": []}
    max_t = float(now_recording_s) if now_recording_s is not None else max(float(c.get("t1", 0.0)) for c in chunks)
    cutoff = max(0.0, max_t - float(window_minutes) * 60.0)
    recent = [c for c in chunks if float(c.get("t1", 0.0)) >= cutoff and (c.get("text") or "").strip()]
    if not recent:
        return {"answer": f"You did not miss any spoken updates in the last {window_minutes} minutes.", "chunks_used": []}

    if llm_fn is not None:
        prompt_lines = [f"[{mst.format_timestamp(c.get('t0', 0.0))}] {c.get('speaker_key', c.get('track', 'Speaker'))}: {c['text']}" for c in recent]
        ans = llm_fn("\n".join(prompt_lines), window_minutes).replace("\u2014", ", ")
        return {"answer": ans, "chunks_used": recent}

    snippets = []
    for c in recent[-3:]:
        spk = c.get("speaker_key") or ("You" if c.get("track") == "me" else "Them")
        t_lbl = mst.format_timestamp(float(c.get("t0", 0.0)))
        snippets.append(f"at [{t_lbl}] {spk} said \"{c['text'].strip()}\"")
    summary = f"In the last {window_minutes} minutes, you heard {len(recent)} update(s): " + "; ".join(snippets) + "."
    return {"answer": summary.replace("\u2014", ", "), "chunks_used": recent}


def ask_meetings(
    store_root: str,
    question: str,
    scope: str = "this_meeting",
    meeting_dir: str | None = None,
    llm_fn=None,
) -> dict:
    """Answer questions across 'this_meeting' (<30k tokens full transcript, else FTS) or 'all_meetings' (FTS top 12)."""
    estimated_usd = 0.001
    passages = []
    used_full_transcript = False

    if scope == "this_meeting" and meeting_dir:
        manifest = mr.read_manifest(meeting_dir) or {}
        mid = manifest.get("id") or os.path.basename(os.path.abspath(meeting_dir))
        title = manifest.get("title") or mid
        segs = mr._read_json(os.path.join(meeting_dir, "transcript.json")) or []
        full_text = "\n".join(f"[{mst.format_timestamp(s.get('t0', 0.0))}] {s.get('speaker_key', 'Speaker')}: {s.get('text', '')}" for s in segs)
        approx_tokens = len(full_text) // 4
        if approx_tokens < 30000 and segs:
            used_full_transcript = True
            for s in segs:
                passages.append(
                    {
                        "meeting_id": mid,
                        "title": title,
                        "speaker_key": s.get("speaker_key") or "Speaker",
                        "t0": float(s.get("t0", 0.0)),
                        "text": s.get("text") or "",
                    }
                )
        else:
            hits = mst.search_meetings(store_root, question, limit=12)
            passages = [h for h in hits if h["meeting_id"] == mid]
    else:
        passages = mst.search_meetings(store_root, question, limit=12)

    if not passages:
        return {
            "answer": "No matching passages found.",
            "citations": [],
            "estimated_usd": estimated_usd,
            "used_full_transcript": used_full_transcript,
        }

    if llm_fn is not None:
        ans = llm_fn(question, passages).replace("\u2014", ", ")
    else:
        top = passages[0]
        t_str = mst.format_timestamp(top.get("t0", 0.0))
        ans = f"In {top.get('title') or top.get('meeting_id')} at [{t_str}], {top.get('speaker_key') or 'Note'}: {top.get('text')}".replace(
            "\u2014", ", "
        )

    citations = [
        {
            "meeting_id": p.get("meeting_id"),
            "title": p.get("title"),
            "timestamp": mst.format_timestamp(p.get("t0", 0.0)),
            "t0": p.get("t0", 0.0),
        }
        for p in passages[:5]
    ]
    return {
        "answer": ans,
        "citations": citations,
        "estimated_usd": estimated_usd,
        "used_full_transcript": used_full_transcript,
    }


def import_audio_file(
    audio_path: str,
    meeting_dir: str,
    title: str | None = None,
    mock_stt_text: str | None = None,
) -> dict:
    """Import any soundfile or ffmpeg-readable audio file into a meeting with one mixed Them track."""
    mr.init_meeting_dir(
        meeting_dir,
        title=title or os.path.basename(audio_path),
        source={"kind": "import", "app": "import", "pid": None, "device": os.path.basename(audio_path)},
    )
    try:
        data, sr = sf.read(audio_path, dtype="int16")
    except Exception:
        tmp_wav = os.path.join(tempfile.gettempdir(), f"scribe_import_{os.getpid()}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", tmp_wav],
            check=True,
            capture_output=True,
        )
        data, sr = sf.read(tmp_wav, dtype="int16")
        try:
            os.remove(tmp_wav)
        except OSError:
            pass

    if data.ndim > 1:
        data = data.mean(axis=1).astype(np.int16)
    if sr != 16000:
        data = mr.resample_pcm_s16le(data.tobytes(), sr, 1, 16000)
        sr = 16000

    # Write 10s FLAC segments for the Them track
    seg_len = sr * 10
    idx = 0
    for start in range(0, len(data), seg_len):
        idx += 1
        mr.write_flac_segment(meeting_dir, "them", idx, data[start : start + seg_len], sr)

    # Transcribe + diarize on local CPU
    segs = mp.process_wav_fixture(
        audio_path,
        meeting_dir,
        speaker="Speaker A",
        track="them",
        mock_stt_text=mock_stt_text,
    )
    diar = ms.diarize_local_cpu(data, sample_rate=sr, num_speakers=2)
    if diar and segs:
        for s in segs:
            mid_t = (float(s.get("t0", 0.0)) + float(s.get("t1", 0.0))) / 2.0
            for d_seg in diar:
                if d_seg["t0"] <= mid_t <= d_seg["t1"]:
                    s["speaker_key"] = d_seg["speaker"]
                    break
        mr._atomic_write_json(os.path.join(meeting_dir, "transcript.json"), segs, fsync=True)

    opus_res = mp.finalize_opus(meeting_dir)
    manifest = mr.read_manifest(meeting_dir) or {}
    manifest["state"] = "ready"
    mr.write_manifest(meeting_dir, manifest)
    return {
        "meeting_dir": meeting_dir,
        "transcript_path": os.path.join(meeting_dir, "transcript.json"),
        "opus_path": opus_res["them_opus"],
        "segments": segs,
    }
