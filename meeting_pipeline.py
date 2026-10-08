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
import io
import json
import os
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
    retries: int = 2,
) -> dict:
    """Transcribe a 10-30s chunk with Groq whisper-large-v3-turbo (429 fallback to whisper-large-v3)."""
    wav_bytes = pcm_to_wav_bytes(pcm_int16, sample_rate)
    models = ["whisper-large-v3-turbo", "whisper-large-v3"]
    last_err = None
    for attempt in range(retries + 1):
        model = models[min(attempt, len(models) - 1)]
        try:
            files = {"file": ("chunk.wav", io.BytesIO(wav_bytes), "audio/wav")}
            data = {
                "model": model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            }
            if prompt_vocab:
                data["prompt"] = prompt_vocab[:800]
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data,
                timeout=30,
            )
            if resp.status_code == 429 and attempt < retries:
                time.sleep(0.5 * (attempt + 1))
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
) -> list[dict]:
    chunks = sorted(read_chunks_jsonl(meeting_dir), key=lambda c: float(c.get("t0", 0.0)))
    raw_segments = []
    for ch in chunks:
        text = (ch.get("text") or "").strip()
        if not text:
            continue
        speaker = me_speaker if ch.get("track") == "me" else ch.get("speaker_key") or them_speaker
        raw_segments.append(
            {
                "speaker_key": speaker,
                "t0": round(float(ch.get("t0", 0.0)), 3),
                "t1": round(float(ch.get("t1", 0.0)), 3),
                "text": text,
                "confidence": ch.get("confidence", "high"),
                "overlap": bool(ch.get("overlap", False)),
                "words": ch.get("words") or interpolate_words(text, float(ch.get("t0", 0.0)), float(ch.get("t1", 0.0))),
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
