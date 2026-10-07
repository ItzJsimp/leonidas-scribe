"""
Live transcription while you talk: the recording is cut at natural pauses and each
finished chunk goes to the same Groq Whisper model as the final paste, on a
background thread. It feeds two things:

1. The pill's live transcript: accurate text roughly 1 to 2 seconds after each pause.
2. A faster finish: when you stop, only the audio after the last cut still needs
   transcribing, and the pasted text is the chunks joined in order. If any chunk
   failed, the app transcribes the whole take instead, so nothing is lost.

Measured 2026-09-25 on two saved takes (153s, 113s): stitched chunks matched the
full-file transcript within 2-3% of words, every difference being words the
full-file pass dropped. Replaces the local sherpa-onnx preview (inaccurate).

    python live_transcriber.py --selftest   offline cut-logic check
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

PAUSE_MARKER = "⏸"


class LiveTranscriber:
    BLOCK_S = 0.03
    PAUSE_S = 0.8          # a pause this long ends a chunk
    MIN_CHUNK_S = 6.0      # keeps requests near one per 6-10s of talking (Groq: 2,000/day)
    MIN_VOICED_S = 1.5
    MAX_CHUNK_S = 25.0     # no pause for this long: cut at the quietest recent block
    PARAGRAPH_PAUSE_S = 1.2

    def __init__(self, transcribe_fn, compactor, sample_rate=16000, on_text=None):
        """transcribe_fn(wav_bytes, speech_end_s) -> str, compactor: an AudioRecorder
        used only for its _compact_silence/_wav_from_array (never opened)."""
        self.transcribe_fn = transcribe_fn
        self.compactor = compactor
        self.sr = sample_rate
        self.on_text = on_text
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="LiveWhisper")
        self._lock = threading.Lock()
        self._session = 0
        self._reset()

    def _reset(self):
        self.blocks, self.rms = [], []
        self.chunk_start = 0          # block index where the current chunk begins
        self.quiet = 0
        self.voiced = 0
        self.futures = []             # (future, pause_before_s) in order
        self.texts = {}
        self.failed = False
        self.sent_seconds = 0.0
        self._last_pause = 0.0

    # --- audio thread ------------------------------------------------------
    def start(self):
        with self._lock:
            self._session += 1
            self._reset()

    def push(self, chunk: np.ndarray):
        """Called from the audio callback with each 30ms block. Must stay cheap."""
        self.blocks.append(chunk)
        r = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        self.rms.append(r)
        n = len(self.rms)
        if n % 10 == 0 or n < 10:
            self._thr = max(float(np.percentile(self.rms, 10)) * 4.0, 40.0)
        if r > getattr(self, "_thr", 40.0):
            self.voiced += 1
            self.quiet = 0
        else:
            self.quiet += 1
        length_s = (n - self.chunk_start) * self.BLOCK_S
        paused = self.quiet * self.BLOCK_S >= self.PAUSE_S
        if paused and length_s >= self.MIN_CHUNK_S and self.voiced * self.BLOCK_S >= self.MIN_VOICED_S:
            self._cut(n - self.quiet // 2, self.quiet * self.BLOCK_S)
        elif length_s >= self.MAX_CHUNK_S:
            recent = self.rms[-int(3 / self.BLOCK_S):]
            self._cut(n - len(recent) + int(np.argmin(recent)) + 1, 0.0)

    def _cut(self, end: int, pause_s: float):
        seg = self.blocks[self.chunk_start:end]
        pause_before = getattr(self, "_last_pause", 0.0)
        self._last_pause = pause_s
        self.chunk_start, self.voiced, self.quiet = end, 0, 0
        if seg:
            self._submit(np.concatenate(seg), pause_before)

    def _submit(self, audio: np.ndarray, pause_before: float):
        session, idx = self._session, len(self.futures)
        fut = self._pool.submit(self._run, audio, session, idx)
        self.futures.append((fut, pause_before))

    # --- worker thread -----------------------------------------------------
    def _run(self, audio, session, idx):
        try:
            compact = self.compactor._compact_silence(audio.reshape(-1, 1))
            if len(compact) == 0:
                text = ""
            else:
                wav = self.compactor._wav_from_array(compact)
                self.sent_seconds += len(compact) / float(self.sr)
                text = (self.transcribe_fn(wav, self.compactor.last_speech_end_s) or "").strip()
        except Exception as exc:
            print(f"[LiveTranscriber] chunk {idx} failed: {exc}")
            if session == self._session:
                self.failed = True
            return None
        if session == self._session:
            self.texts[idx] = text
            if self.on_text:
                self.on_text(self.transcript())
        return text

    def transcript(self) -> str:
        """Finished chunks in order, stopping at the first one still in flight."""
        parts = []
        for i, (_, pause) in enumerate(self.futures):
            if i not in self.texts:
                break
            if self.texts[i]:
                if parts and pause >= self.PARAGRAPH_PAUSE_S:
                    parts.append(PAUSE_MARKER)
                parts.append(self.texts[i])
        return " ".join(parts)

    # --- stop --------------------------------------------------------------
    def finish(self, timeout: float = 20.0):
        """Transcribe the tail and return the whole take's text, or None if any chunk
        failed (the caller then transcribes the full recording)."""
        if self.chunk_start < len(self.blocks):
            self._cut(len(self.blocks), 0.0)
        deadline = time.time() + timeout
        for fut, _ in self.futures:
            try:
                fut.result(timeout=max(0.1, deadline - time.time()))
            except Exception:
                self.failed = True
        if self.failed or not self.futures:
            return None
        return self.transcript()

    def cancel(self):
        with self._lock:
            self._session += 1  # results still in flight are ignored


def _selftest():
    class FakeCompactor:
        last_speech_end_s = 1.0
        def _compact_silence(self, a):
            return a
        def _wav_from_array(self, a):
            return a.tobytes()
    calls = []
    lt = LiveTranscriber(lambda wav, end: calls.append(len(wav)) or f"chunk{len(calls)}", FakeCompactor())
    lt.start()
    loud = np.full((480, 1), 3000, np.int16)
    quiet = np.full((480, 1), 50, np.int16)
    # 7s speech, 1s pause, 7s speech, 1s pause, 2s speech
    for n_loud in (233, 233, 66):
        for _ in range(n_loud):
            lt.push(loud)
        for _ in range(34):
            lt.push(quiet)
    text = lt.finish()
    assert len(calls) == 3, calls
    assert text == "chunk1 chunk2 chunk3", text
    lt.start()
    for _ in range(40):
        lt.push(quiet)
    assert lt.finish() == "chunk4", "a short final chunk is still transcribed"
    print("live transcriber selftest ok")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
