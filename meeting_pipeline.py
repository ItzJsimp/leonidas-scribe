"""Post-call and live chunking pipeline for Scribe Meetings (S2).

Handles:
- 10-30 s pause-cut chunking per track with energy gate silence skipping
- Groq whisper-large-v3-turbo verbose_json word-timestamp STT (with whisper-large-v3 429 failover)
- chunks.jsonl append with flush + os.fsync
- 2k-token window stitch + dictionary/LLM polish -> transcript.json
- Opus finalization (audio/me.opus, audio/them.opus) with verified read-back before deleting FLAC segments
- Cost logging in manifest.json (<= $0.09/hr Discord, <= $0.12 gate)
"""
import argparse
import getpass
import io
import json
import os
import re
import sys
import time
import wave

import numpy as np
import requests
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meeting_recorder as mr
import meeting_speakers as ms
import meeting_store as mst

SAMPLE_RATE = 16000
GROQ_TURBO_USD_PER_SEC = 0.04 / 3600.0
POLISH_USD_PER_1K_TOKENS = 0.0005


class MeetingChunker:
    """Per-track pause-cut chunker (min 10 s, max 30 s) that skips pure silence."""

    MIN_CHUNK_S = 10.0
    MAX_CHUNK_S = 30.0
    PAUSE_S = 0.8
    MIN_VOICED_S = 0.4
    ENERGY_FLOOR = 55.0

    def __init__(self, track: str, on_chunk, sample_rate: int = SAMPLE_RATE):
        self.track = track
        self.on_chunk = on_chunk
        self.sr = sample_rate
        self.blocks: list[np.ndarray] = []
        self.rms: list[float] = []
        self.block_durations: list[float] = []
        self.cursor_s = 0.0
        self.voiced_s = 0.0
        self.quiet_s = 0.0
        self.sent_seconds = 0.0

    def push(self, pcm_int16: np.ndarray) -> None:
        arr = np.asarray(pcm_int16, dtype=np.int16).reshape(-1)
        if len(arr) == 0:
            return
        dur = len(arr) / float(self.sr)
        r = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        voiced = r >= self.ENERGY_FLOOR

        # Skip leading silence so pure-silence windows never accumulate toward MAX_CHUNK_S
        if not self.blocks and not voiced:
            self.cursor_s += dur
            return

        self.blocks.append(arr)
        self.rms.append(r)
        self.block_durations.append(dur)
        if voiced:
            self.voiced_s += dur
            self.quiet_s = 0.0
        else:
            self.quiet_s += dur

        chunk_len_s = sum(self.block_durations)
        if self.quiet_s >= self.PAUSE_S and chunk_len_s >= self.MIN_CHUNK_S and self.voiced_s >= self.MIN_VOICED_S:
            self._emit_all()
        elif chunk_len_s >= self.MAX_CHUNK_S:
            # Cut at the quietest block in the last 3 seconds
            tail_s = 0.0
            tail_count = 0
            for d in reversed(self.block_durations):
                tail_s += d
                tail_count += 1
                if tail_s >= 3.0:
                    break
            start_idx = max(1, len(self.rms) - tail_count)
            cut_idx = start_idx + int(np.argmin(self.rms[start_idx:])) + 1
            self._emit_prefix(cut_idx)

    def _emit_prefix(self, count: int) -> None:
        emit_blocks = self.blocks[:count]
        emit_durs = self.block_durations[:count]
        rem_blocks = self.blocks[count:]
        rem_rms = self.rms[count:]
        rem_durs = self.block_durations[count:]

        dur = sum(emit_durs)
        t0 = self.cursor_s
        t1 = t0 + dur
        self.cursor_s = t1

        self.blocks = rem_blocks
        self.rms = rem_rms
        self.block_durations = rem_durs
        self.voiced_s = sum(d for d, r in zip(rem_durs, rem_rms) if r >= self.ENERGY_FLOOR)
        self.quiet_s = 0.0

        if emit_blocks:
            pcm = np.concatenate(emit_blocks)
            self.sent_seconds += dur
            self.on_chunk(round(t0, 3), round(t1, 3), pcm)

    def _emit_all(self) -> None:
        self._emit_prefix(len(self.blocks))

    def flush(self) -> None:
        if self.blocks and self.voiced_s >= 0.1:
            self._emit_all()
        else:
            self.cursor_s += sum(self.block_durations)
            self.blocks.clear()
            self.rms.clear()
            self.block_durations.clear()
            self.voiced_s = 0.0
            self.quiet_s = 0.0


def pcm_to_wav_bytes(pcm_int16: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    arr = np.asarray(pcm_int16, dtype=np.int16).reshape(-1)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(arr.tobytes())
    return buf.getvalue()


def load_dictionary(dict_path: str | None = None) -> dict:
    path = dict_path or os.path.join(HERE, "dictionary.json")
    if not os.path.exists(path):
        return {"replacements": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"replacements": {}}


def apply_dictionary_replacements(text: str, dict_data: dict | None = None) -> str:
    if not text:
        return ""
    d = dict_data or load_dictionary()
    replacements = d.get("replacements") or {}
    out = text
    for bad, good in replacements.items():
        if bad and bad in out:
            out = out.replace(bad, good)
    return out


def interpolate_words(text: str, t0: float, t1: float) -> list[dict]:
    tokens = [w for w in (text or "").strip().split() if w]
    if not tokens:
        return []
    span = max(0.05, float(t1) - float(t0))
    step = span / len(tokens)
    words = []
    for i, w in enumerate(tokens):
        w_t0 = round(float(t0) + i * step, 3)
        w_t1 = round(float(t0) + (i + 1) * step, 3)
        words.append({"w": w, "t0": w_t0, "t1": w_t1})
    return words


def append_chunk_jsonl(meeting_dir: str, chunk_record: dict) -> None:
    path = os.path.join(meeting_dir, "chunks.jsonl")
    line = json.dumps(chunk_record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_chunks_jsonl(meeting_dir: str) -> list[dict]:
    path = os.path.join(meeting_dir, "chunks.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def transcribe_chunk_groq(
    pcm_int16: np.ndarray,
    t0: float,
    t1: float,
    api_key: str,
    prompt_vocab: str = "",
    sample_rate: int = SAMPLE_RATE,
    retries: int = 5,
    api_key_backup: str = "",
    post_fn=None,
) -> dict:
    """Transcribe a 10-30s chunk with Groq whisper-large-v3-turbo (429 instant backup key failover, then whisper-large-v3)."""
    wav_bytes = pcm_to_wav_bytes(pcm_int16, sample_rate)
    models = ["whisper-large-v3-turbo", "whisper-large-v3"]
    last_err = None
    active_key = api_key or api_key_backup
    backup_tried = False
    for attempt in range(retries + 1):
        model = models[0] if (attempt == 0 or backup_tried) else models[min(attempt, len(models) - 1)]
        try:
            files = {"file": ("chunk.wav", io.BytesIO(wav_bytes), "audio/wav")}
            data = {
                "model": model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            }
            if prompt_vocab:
                data["prompt"] = prompt_vocab[:800]
            headers = {"Authorization": f"Bearer {active_key}"}
            if post_fn is not None:
                status_code, body = post_fn(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers, files, data, 30,
                )
                if status_code == 429 and api_key_backup and active_key != api_key_backup and not backup_tried:
                    active_key = api_key_backup
                    backup_tried = True
                    status_code, body = post_fn(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        {"Authorization": f"Bearer {active_key}"}, files, data, 30,
                    )
                if status_code == 429 and attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if status_code >= 400:
                    raise RuntimeError(f"HTTP {status_code}: {body}")
            else:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=30,
                )
                if resp.status_code == 429 and api_key_backup and active_key != api_key_backup and not backup_tried:
                    active_key = api_key_backup
                    backup_tried = True
                    files = {"file": ("chunk.wav", io.BytesIO(wav_bytes), "audio/wav")}
                    resp = requests.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {active_key}"},
                        files=files,
                        data=data,
                        timeout=30,
                    )
                if resp.status_code == 429 and attempt < retries:
                    # free tier is 20 RPM: finalize bursts ~90 chunks, so wait out the window (paid 2026-10-09: 7 chunks lost)
                    try:
                        wait = float(resp.headers.get("retry-after") or "")
                    except ValueError:
                        wait = 5.0 * 2 ** attempt
                    time.sleep(min(60.0, max(1.0, wait)))
                    continue
                resp.raise_for_status()
                body = resp.json()
            text = (body.get("text") or "").strip()
            raw_words = body.get("words") or []
            words = []
            for rw in raw_words:
                w_str = (rw.get("word") or rw.get("w") or "").strip()
                if not w_str:
                    continue
                ws = float(rw.get("start", 0.0))
                we = float(rw.get("end", ws + 0.1))
                words.append({"w": w_str, "t0": round(t0 + ws, 3), "t1": round(t0 + we, 3)})
            if not words and text:
                words = interpolate_words(text, t0, t1)
            return {"text": text, "words": words, "model": model}
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Groq chunk STT failed after {retries + 1} tries: {last_err}")


def polish_windows(segments: list[dict], dict_data: dict | None = None, llm_polish_fn=None) -> list[dict]:
    """Polish stitched segments in ~2k-token windows while preserving speakers and timestamps."""
    d = dict_data or load_dictionary()
    polished = []
    window: list[dict] = []
    tok_est = 0
    for seg in segments:
        s_copy = dict(seg)
        s_copy["text"] = apply_dictionary_replacements(s_copy.get("text", ""), d).replace("\u2014", ", ")
        polished.append(s_copy)
        window.append(s_copy)
        tok_est += max(1, len(s_copy["text"]) // 4)
        if tok_est >= 2000 and llm_polish_fn is not None:
            llm_polish_fn(window)
            window = window[-2:]  # overlap
            tok_est = sum(max(1, len(x["text"]) // 4) for x in window)
    if window and llm_polish_fn is not None:
        llm_polish_fn(window)
    return polished


def stitch_and_polish(
    meeting_dir: str,
    me_speaker: str = "Me",
    them_speaker: str = "Them",
    dict_data: dict | None = None,
    llm_polish_fn=None,
    attribute_fn=None,
) -> list[dict]:
    """attribute_fn(chunk) -> list of chunk-shaped pieces (speaker_key/confidence/overlap set); used for Them chunks only."""
    chunks = sorted(read_chunks_jsonl(meeting_dir), key=lambda c: float(c.get("t0", 0.0)))
    raw_segments = []
    for ch in chunks:
        text = (ch.get("text") or "").strip()
        if not text:
            continue
        pieces = attribute_fn(ch) if (attribute_fn and ch.get("track") != "me") else [ch]
        for pc in pieces:
            ptext = (pc.get("text") or "").strip()
            if not ptext:
                continue
            speaker = me_speaker if pc.get("track") == "me" else pc.get("speaker_key") or them_speaker
            raw_segments.append(
                {
                    "speaker_key": speaker,
                    "t0": round(float(pc.get("t0", 0.0)), 3),
                    "t1": round(float(pc.get("t1", 0.0)), 3),
                    "text": ptext,
                    "confidence": pc.get("confidence", "high"),
                    "overlap": bool(pc.get("overlap", False)),
                    "words": pc.get("words") or interpolate_words(ptext, float(pc.get("t0", 0.0)), float(pc.get("t1", 0.0))),
                }
            )
    segments = polish_windows(raw_segments, dict_data=dict_data, llm_polish_fn=llm_polish_fn)
    mr._atomic_write_json(os.path.join(meeting_dir, "transcript.json"), segments, fsync=True)
    return segments


def finalize_opus(meeting_dir: str) -> dict:
    """Encode me/seg-*.flac and them/seg-*.flac into audio/me.opus and audio/them.opus.
    Deletes FLAC segments ONLY after verified read-back of the Opus files."""
    audio_dir = os.path.join(meeting_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    result = {}
    for track in ("me", "them"):
        seg_files = mr._segment_files(meeting_dir, track)
        opus_path = os.path.join(audio_dir, f"{track}.opus")
        if not seg_files:
            result[f"{track}_opus"] = opus_path
            result[f"{track}_duration"] = 0.0
            continue
        arrays = []
        expected_frames = 0
        sr = SAMPLE_RATE
        for fpath in seg_files:
            data, file_sr = sf.read(fpath, dtype="float32")
            sr = file_sr
            arrays.append(data)
            expected_frames += len(data)
        full_audio = np.concatenate(arrays) if arrays else np.zeros(0, dtype=np.float32)
        tmp_opus = opus_path + ".tmp"
        sf.write(tmp_opus, full_audio, sr, format="OGG", subtype="OPUS")
        os.replace(tmp_opus, opus_path)

        # Verified read-back before deleting source segments
        info = sf.info(opus_path)
        readback, rb_sr = sf.read(opus_path, dtype="float32")
        expected_dur = expected_frames / float(sr)
        actual_dur = len(readback) / float(rb_sr)
        if not os.path.exists(opus_path) or os.path.getsize(opus_path) == 0 or abs(actual_dur - expected_dur) > 0.5:
            raise RuntimeError(
                f"Opus read-back verification failed for {opus_path}: expected {expected_dur:.2f}s, got {actual_dur:.2f}s"
            )
        for fpath in seg_files:
            os.remove(fpath)
        result[f"{track}_opus"] = opus_path
        result[f"{track}_duration"] = round(actual_dur, 3)
    return result


def estimate_and_log_call_cost(
    meeting_dir: str,
    me_voiced_seconds: float,
    them_voiced_seconds: float,
    polish_tokens: int = 8000,
    diarization_seconds: float = 0.0,
    diarization_provider: str = "local_cpu",
) -> dict:
    """Compute and record call cost in manifest.json (Discord 1h <= $0.09, gate <= $0.12)."""
    manifest = mr.read_manifest(meeting_dir) or mr.init_meeting_dir(meeting_dir)
    stt_seconds = float(me_voiced_seconds) + float(them_voiced_seconds)
    stt_usd = round(stt_seconds * GROQ_TURBO_USD_PER_SEC, 4)
    polish_usd = round((float(polish_tokens) / 1000.0) * POLISH_USD_PER_1K_TOKENS, 4)
    costs = [
        {"provider": "groq:whisper-large-v3-turbo", "seconds": round(stt_seconds, 1), "usd": stt_usd},
        {"provider": "groq:polish", "seconds": 0.0, "usd": polish_usd},
    ]
    if diarization_seconds > 0:
        diar_usd = round((diarization_seconds / 3600.0) * 0.17, 4) if diarization_provider == "assemblyai" else 0.0
        costs.append({"provider": diarization_provider, "seconds": round(diarization_seconds, 1), "usd": diar_usd})
    manifest["costs"] = costs
    mr.write_manifest(meeting_dir, manifest)
    total_usd = round(sum(c["usd"] for c in costs), 4)
    return {"costs": costs, "total_usd": total_usd}


def process_wav_fixture(
    wav_path: str,
    meeting_dir: str,
    speaker: str = "Me",
    track: str = "me",
    mock_stt_text: str | None = None,
) -> list[dict]:
    mr.init_meeting_dir(meeting_dir, title=os.path.basename(wav_path))
    data, sr = sf.read(wav_path, dtype="int16")
    if data.ndim > 1:
        data = data.mean(axis=1).astype(np.int16)

    def _on_chunk(t0: float, t1: float, pcm: np.ndarray):
        if mock_stt_text is not None:
            text = mock_stt_text
            words = interpolate_words(text, t0, t1)
        else:
            sec_path = os.path.join(HERE, "secrets.json")
            sec = json.load(open(sec_path, encoding="utf-8")) if os.path.exists(sec_path) else {}
            key = (sec.get("api") or {}).get("groq_api_key") or sec.get("GROQ_API_KEY") or ""
            res = transcribe_chunk_groq(pcm, t0, t1, api_key=key, sample_rate=sr)
            text = res["text"]
            words = res["words"]
        append_chunk_jsonl(
            meeting_dir,
            {
                "track": track,
                "speaker_key": speaker,
                "t0": t0,
                "t1": t1,
                "text": text,
                "words": words,
            },
        )

    chunker = MeetingChunker(track=track, on_chunk=_on_chunk, sample_rate=sr)
    step = max(1, int(sr * 0.1))
    for i in range(0, len(data), step):
        chunker.push(data[i : i + step])
    chunker.flush()
    return stitch_and_polish(meeting_dir, me_speaker=speaker, them_speaker=speaker)


# ---------------------------------------------------------------- post-call finalize (idempotent per stage)
def load_scribe_config() -> dict:
    try:
        from engine import load_config_file

        return load_config_file(os.path.join(HERE, "config.json"))
    except Exception:
        return {}


def groq_keys(cfg: dict) -> tuple[str, str]:
    """(primary, backup) Groq keys from config.api, then env; the backup doubles as primary when alone."""
    api = (cfg or {}).get("api") or {}
    backup = api.get("groq_api_key_backup") or os.environ.get("GROQ_API_KEY_BACKUP") or ""
    return api.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or backup, backup


def _patch_manifest(meeting_dir: str, **kw) -> dict:
    m = mr.read_manifest(meeting_dir) or {}
    m.update(kw)
    mr.write_manifest(meeting_dir, m)
    return m


def _llm_chain(cfg: dict, note: bool = False) -> list[tuple[str, str, str]]:
    """[(base_url, key, model)] tried in order: polish = Cerebras then Groq; note = Groq gpt-oss-120b (both keys)."""
    api = (cfg or {}).get("api") or {}
    gk, gb = groq_keys(cfg)
    gurl = api.get("groq_base_url", "https://api.groq.com/openai/v1")
    chain = []
    ck = api.get("cerebras_api_key") or os.environ.get("CEREBRAS_API_KEY") or ""
    if ck and not note:
        chain.append((api.get("cerebras_base_url", "https://api.cerebras.ai/v1"), ck, api.get("cerebras_llm_model", "qwen-3.8-27b")))
    model = "openai/gpt-oss-120b" if note else api.get("groq_oss_llm_model", "openai/gpt-oss-20b")
    chain += [(gurl, k, model) for k in dict.fromkeys([gk, gb]) if k]
    return chain


OPENROUTER_URL = "https://openrouter.ai/api/v1"
FREE_ASK_MODELS = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "thinkingmachines/inkling:free",
    "openrouter/free",
)


def is_free_model(model: str) -> bool:
    return model.endswith(":free") or model == "openrouter/free"


def free_chain(cfg: dict) -> list[tuple[str, str, str]]:
    """OpenRouter free models only (meetings.ask_models overrides the order). A paid id is dropped, never called."""
    api = (cfg or {}).get("api") or {}
    key = api.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or ""
    models = ((cfg or {}).get("meetings") or {}).get("ask_models") or FREE_ASK_MODELS
    return [(OPENROUTER_URL, key, m) for m in models if key and is_free_model(m)]


def _chat(chain, messages, json_mode: bool = False, timeout: float = 90, post=None) -> str:
    post = post or requests.post
    last = None
    for base, key, model in chain:
        body = {"model": model, "messages": messages, "temperature": 0.1}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=timeout)
            if r.status_code >= 400:
                last = f"HTTP {r.status_code}"
                continue
            out = r.json()["choices"][0]["message"]["content"] or ""
            if out.strip():
                return out
            last = f"{model}: empty reply"
        except Exception as e:
            last = e
    raise RuntimeError(f"LLM chain failed: {last}")


def _json_from_llm(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else {}


def make_polish_fn(cfg: dict, chat=_chat):
    """In-place window polish for polish_windows(); any failure leaves the dictionary-polished text untouched."""
    chain = _llm_chain(cfg)
    if not chain:
        return None
    system = (
        "Fix mis-hearings, punctuation and names in these transcript lines. Keep wording and meaning. "
        'Reply JSON only: the same keys mapped to the corrected lines. Never use an em dash.'
    )

    def polish(window: list[dict]) -> None:
        lines = {str(i): s["text"] for i, s in enumerate(window)}
        try:
            out = _json_from_llm(chat(chain, [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(lines)}], json_mode=True))
        except Exception:
            return
        for i, s in enumerate(window):
            new = out.get(str(i))
            # drift guard: a rewrite that changes the length a lot is a hallucination, not a polish
            if isinstance(new, str) and new.strip() and abs(len(new) - len(s["text"])) <= 0.5 * len(s["text"]) + 20:
                s["text"] = new.strip().replace(chr(0x2014), ", ")

    return polish


def load_template(template_id: str | None) -> str:
    tid = os.path.basename(template_id or "default")
    for name in (tid, "default"):
        path = os.path.join(HERE, "meeting_templates", f"{name}.md")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return ""


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def generate_note_data(manifest: dict, segments: list[dict], cfg: dict, chat=_chat) -> dict:
    """Groq gpt-oss-120b JSON note following the chosen template's section instructions."""
    chain = _llm_chain(cfg, note=True)
    if not chain:
        raise RuntimeError("no Groq key")
    transcript = "\n".join(
        f"[t={int(_num(s.get('t0')))}] {s.get('speaker_key') or 'Them'}: {s.get('text', '')}" for s in segments
    )[:300000]  # ponytail: hard cap (about 75k tokens); chunked map-reduce if multi-hour calls need it
    system = (
        "You write concise meeting notes. Reply JSON only with keys: title (string), summary (list of strings), "
        "decisions (list of {t: seconds, text}), action_items (list of {owner, task, t: seconds}), "
        "open_questions (list of strings). Follow these section instructions:\n"
        + load_template(manifest.get("template_id"))
        + "\nNever use an em dash."
    )
    raw = _json_from_llm(chat(chain, [{"role": "system", "content": system}, {"role": "user", "content": transcript}], json_mode=True))
    return {
        "title": str(raw.get("title") or manifest.get("title") or "Meeting"),
        "summary": [str(x) for x in raw.get("summary") or []],
        "decisions": [{"t": _num(d.get("t")), "text": str(d.get("text", ""))} for d in raw.get("decisions") or [] if isinstance(d, dict)],
        "action_items": [
            {"owner": d.get("owner") or "", "task": str(d.get("task", "")), "t": _num(d.get("t"))}
            for d in raw.get("action_items") or [] if isinstance(d, dict)
        ],
        "open_questions": [str(x) for x in raw.get("open_questions") or []],
    }


def _read_track(meeting_dir: str, track: str) -> np.ndarray:
    parts = [sf.read(p, dtype="int16")[0] for p in mr._segment_files(meeting_dir, track)]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


def _transcribe_missing(meeting_dir: str, stt, sample_rate: int = SAMPLE_RATE) -> int:
    """Chunk both tracks' FLAC segments and transcribe chunks not yet in chunks.jsonl. Returns failed-chunk count."""
    done = {(r.get("track"), round(_num(r.get("t0")), 3), round(_num(r.get("t1")), 3)) for r in read_chunks_jsonl(meeting_dir)}
    failed = [0]
    for track in ("me", "them"):
        def on_chunk(t0, t1, pcm, track=track):
            if (track, round(t0, 3), round(t1, 3)) in done:
                return
            try:
                res = stt(pcm, t0, t1, track)
            except Exception as e:
                failed[0] += 1
                _patch_manifest(meeting_dir, last_stt_error=str(e)[:200])
                return
            append_chunk_jsonl(meeting_dir, {"track": track, "t0": t0, "t1": t1, "text": res.get("text", ""), "words": res.get("words", [])})

        chunker = MeetingChunker(track=track, on_chunk=on_chunk, sample_rate=sample_rate)
        step = int(sample_rate * 0.1)
        for path in mr._segment_files(meeting_dir, track):
            data, _sr = sf.read(path, dtype="int16")
            for i in range(0, len(data), step):
                chunker.push(data[i : i + step])
        chunker.flush()
    return failed[0]


def _load_speaking_events(meeting_dir: str, manifest: dict, store_root: str) -> list[dict]:
    path = os.path.join(meeting_dir, "speaking.jsonl")
    if not os.path.exists(path):
        return []
    off = manifest.get("record_started_wall")
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # torn last line after a crash
            if off and ev.get("wall") is not None:
                ev["t"] = max(0.0, _num(ev["wall"]) - _num(off))
            events.append(ev)
    for uid in sorted({str(e.get("user_id")) for e in events if e.get("event") in ("start", "stop")}):
        remembered = ms.lookup_remembered_discord_name(store_root, uid)
        if remembered:
            events.append({"event": "join", "user_id": uid, "name": remembered, "t": 0.0})
    return events


def _split_by_speaker(ch: dict, labelled: list[dict]) -> list[dict]:
    """Group consecutive same-speaker words into pieces; a single-speaker chunk keeps its punctuated text."""
    pieces: list[dict] = []
    for w in labelled:
        key = (w.get("speaker_key") or "Them", w.get("confidence", "high"), bool(w.get("overlap")))
        if pieces and pieces[-1]["_k"] == key:
            pieces[-1]["words"].append(w)
            pieces[-1]["t1"] = w["t1"]
        else:
            pieces.append({"_k": key, "track": ch.get("track"), "speaker_key": key[0], "confidence": key[1],
                           "overlap": key[2], "t0": w["t0"], "t1": w["t1"], "words": [w]})
    if len(pieces) <= 1:
        p = pieces[0] if pieces else {"speaker_key": "Them", "confidence": "?", "overlap": False}
        return [dict(ch, speaker_key=p["speaker_key"], confidence=p["confidence"], overlap=p["overlap"])]
    for p in pieces:
        p.pop("_k")
        p["text"] = " ".join(x["w"] for x in p["words"])
    return pieces


def make_attribute_fn(meeting_dir: str, manifest: dict, store_root: str):
    """Them-chunk splitter: Discord speaking timeline if present, else local CPU diarization (assumes 2 speakers)."""
    events = _load_speaking_events(meeting_dir, manifest, store_root)
    chunks = read_chunks_jsonl(meeting_dir)
    dur = max([_num(c.get("t1")) for c in chunks] or [1.0])
    if any(e.get("event") in ("start", "stop") for e in events):
        local = (manifest.get("discord") or {}).get("local_user_id")

        def by_discord(ch):
            words = ch.get("words") or interpolate_words(ch.get("text", ""), _num(ch.get("t0")), _num(ch.get("t1")))
            return _split_by_speaker(ch, ms.attribute_discord_words(words, events, call_duration_s=dur, local_user_id=local))

        return by_discord
    pcm = _read_track(meeting_dir, "them")
    diar = ms.diarize_local_cpu(pcm, num_speakers=2) if len(pcm) else []
    if not diar:
        return None

    def by_diarization(ch):
        words = ch.get("words") or interpolate_words(ch.get("text", ""), _num(ch.get("t0")), _num(ch.get("t1")))
        out, last = [], "Them"
        for w in words:
            mid = (_num(w.get("t0")) + _num(w.get("t1"))) / 2.0
            last = next((d["speaker"] for d in diar if d["t0"] <= mid <= d["t1"]), last)
            out.append(dict(w, speaker_key=last, confidence="high", overlap=False))
        return _split_by_speaker(ch, out)

    return by_diarization


def _merge_participants(meeting_dir: str, manifest: dict, store_root: str) -> None:
    """People who joined mid-call are only in speaking.jsonl (the service never rewrites the manifest while recording)."""
    seen = {str(p.get("discord_id")) for p in manifest.get("participants") or [] if p.get("discord_id")}
    new = []
    for e in _load_speaking_events(meeting_dir, manifest, store_root):
        people = e.get("participants") or [] if e.get("event") == "channel" else [e] if e.get("event") == "join" else []
        for p in people:
            uid = str(p.get("discord_id") or p.get("user_id") or "")
            if uid and uid not in seen and p.get("name"):
                seen.add(uid)
                new.append({"key": p["name"], "name": p["name"], "discord_id": uid, "avatar_path": None})
    if new:
        _patch_manifest(meeting_dir, participants=(manifest.get("participants") or []) + new)


def _find_destination(cfg: dict, dest_id) -> dict | None:
    for d in ((cfg or {}).get("meetings") or {}).get("destinations") or []:
        if dest_id and dest_id in (d.get("id"), d.get("name")) and d.get("path"):
            return d
    return None


FINALIZE_LOCK = "finalize.lock"
FINALIZE_WAIT_S = 30 * 60.0


def _lock_owner_alive(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            pid = int((f.read().split() or ["0"])[0])
    except (OSError, ValueError):
        return True  # being written right now: treat as held
    if pid == os.getpid():
        return True
    from meeting_service import pid_alive  # lazy: meeting_service imports this module
    return pid_alive(pid)


def finalize_meeting(meeting_dir: str, *args, **kwargs) -> dict:
    """One finalizer per meeting across processes. The worker finalizes after stop and the app also
    finalizes after stop_worker; on 2026-10-09 both ran at once, so one deleted the FLAC segments the
    other was reading ("Error opening seg-00043.flac: System error") and both appended every chunk.
    A second caller now waits for the first and returns the state it left."""
    lock = os.path.join(meeting_dir, FINALIZE_LOCK)
    deadline = time.monotonic() + FINALIZE_WAIT_S
    waited = False
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if not _lock_owner_alive(lock):
                try:
                    os.remove(lock)  # the owner crashed: take over
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                return {"state": (mr.read_manifest(meeting_dir) or {}).get("state", "processing"), "busy": True}
            waited = True
            time.sleep(0.25)
    try:
        if waited:  # the other finalizer just finished: report its outcome instead of redoing the work
            m = mr.read_manifest(meeting_dir) or {}
            return {"state": m.get("state", "failed"), "error": m.get("error"), "joined": True}
        os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
        return _finalize_meeting_unlocked(meeting_dir, *args, **kwargs)
    finally:
        os.close(fd)
        try:
            os.remove(lock)
        except OSError:
            pass


def _finalize_meeting_unlocked(
    meeting_dir: str,
    cfg: dict | None = None,
    store_root: str | None = None,
    stt_fn=None,
    note_fn=None,
    polish_fn=None,
) -> dict:
    """Post-call pipeline: transcribe -> attribute -> stitch/polish -> note -> save -> Opus -> index -> cost -> ready.

    Safe to rerun at any point (crash, recovery): every stage skips when its output already exists and only missing
    chunks are re-sent. stt_fn(pcm, t0, t1, track) -> {text, words}; note_fn(manifest, segments) -> note_data;
    polish_fn(window) are injectable for tests. Never raises for a missing key (state needs_key) or a stage error (failed).
    """
    cfg = cfg if cfg is not None else load_scribe_config()
    store_root = store_root or os.path.dirname(os.path.abspath(meeting_dir))
    manifest = mr.read_manifest(meeting_dir) or mr.init_meeting_dir(meeting_dir)
    key, backup = groq_keys(cfg)
    if not key and stt_fn is None:
        _patch_manifest(meeting_dir, state="needs_key")
        return {"state": "needs_key"}
    try:
        _patch_manifest(meeting_dir, state="processing", error=None)
        if stt_fn is None:
            vocab = ", ".join(load_dictionary().get("vocabulary") or [])

            def stt_fn(pcm, t0, t1, track):
                return transcribe_chunk_groq(pcm, t0, t1, api_key=key, prompt_vocab=vocab, api_key_backup=backup)

        # 1. chunk + transcribe (resumable: chunks already in chunks.jsonl are skipped)
        failed = _transcribe_missing(meeting_dir, stt_fn)
        if failed:
            _patch_manifest(meeting_dir, state="failed", error=f"{failed} chunk(s) failed to transcribe; rerun to retry")
            return {"state": "failed", "failed_chunks": failed}

        # 2. attribute + stitch + polish -> transcript.json
        tpath = os.path.join(meeting_dir, "transcript.json")
        if not os.path.exists(tpath):
            me_name = ((cfg.get("user") or {}).get("name") or "").strip() or getpass.getuser()
            stitch_and_polish(
                meeting_dir,
                me_speaker=me_name,
                llm_polish_fn=polish_fn if polish_fn is not None else make_polish_fn(cfg),
                attribute_fn=make_attribute_fn(meeting_dir, manifest, store_root),
            )
        segments = mr._read_json(tpath) or []
        _merge_participants(meeting_dir, manifest, store_root)

        # 3. note (cached in note.json) + save to destination (note + transcript files)
        npath = os.path.join(meeting_dir, "note.json")
        note_data = mr._read_json(npath)
        if note_data is None:
            try:
                note_data = (note_fn or (lambda m, s: generate_note_data(m, s, cfg)))(manifest, segments)
                mr._atomic_write_json(npath, note_data, fsync=True)
            except Exception as e:
                # transcript.json is already safe locally; audio is kept and a rerun retries only the note
                _patch_manifest(meeting_dir, state="failed" if key else "needs_key", error=f"note: {e}"[:300])
                return {"state": "failed" if key else "needs_key", "error": str(e)}
        manifest = mr.read_manifest(meeting_dir) or manifest
        if re.match(r"Meeting \w{3} \d{1,2}, \d", manifest.get("title") or "") and note_data.get("title", "").strip():
            manifest = _patch_manifest(meeting_dir, title=note_data["title"].strip())  # auto title -> AI title
        dest = _find_destination(cfg, manifest.get("destination_id"))
        saved = manifest.get("note_path")
        if dest and not (saved and os.path.exists(saved)):
            paths = mst.save_meeting_to_destination(meeting_dir, dest, note_data=note_data, store_root=store_root)
            _patch_manifest(meeting_dir, note_path=paths["note_path"], transcript_path=paths["transcript_path"])

        # 4. Opus (deletes FLAC only after verified read-back); skipped once segments are gone
        if mr._segment_files(meeting_dir, "me") or mr._segment_files(meeting_dir, "them"):
            finalize_opus(meeting_dir)

        # 5. index + cost
        mst.index_meeting(store_root, meeting_dir)
        rows = read_chunks_jsonl(meeting_dir)
        voiced = {t: sum(_num(r.get("t1")) - _num(r.get("t0")) for r in rows if r.get("track") == t) for t in ("me", "them")}
        tokens = sum(len(s.get("text", "")) for s in segments) // 4 + 1000
        estimate_and_log_call_cost(meeting_dir, voiced["me"], voiced["them"], polish_tokens=tokens)
        _patch_manifest(meeting_dir, state="ready")
        return {"state": "ready", "segments": len(segments)}
    except Exception as e:
        _patch_manifest(meeting_dir, state="failed", error=str(e)[:300])
        return {"state": "failed", "error": str(e)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Leonidas Scribe meeting pipeline (S2)")
    ap.add_argument("--from-wav", help="Process a WAV file into meeting_dir/transcript.json")
    ap.add_argument("--meeting-dir", required=True)
    ap.add_argument("--speaker", default="Me")
    ap.add_argument("--track", default="me")
    ap.add_argument("--mock-stt-text", default=None)
    args = ap.parse_args(argv)
    if args.from_wav:
        segs = process_wav_fixture(
            args.from_wav,
            args.meeting_dir,
            speaker=args.speaker,
            track=args.track,
            mock_stt_text=args.mock_stt_text,
        )
        print(f"wrote {len(segs)} segment(s) to {os.path.join(args.meeting_dir, 'transcript.json')}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
