"""Re-run a dictation that failed to process.

Leonidas Scribe keeps the audio in recordings/failed/ whenever the pipeline
raises, so a rate limit or a dropped connection costs a retry rather than the
recording. This sends those files back through transcription and polish, copies
the result to the clipboard, and logs it to history like a normal dictation.

    python Recover-Dictation.py              # process every pending failure
    python Recover-Dictation.py --list       # show what is pending, do nothing
    python Recover-Dictation.py --keep       # do not move files to recovered/
"""
import argparse
import json
import os
import shutil
import sys
import time

from engine import DictationEngine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAILED_DIR = os.path.join(BASE_DIR, "recordings", "failed")
RECOVERED_DIR = os.path.join(BASE_DIR, "recordings", "recovered")
HISTORY = os.path.join(BASE_DIR, "history.jsonl")


def pending():
    if not os.path.isdir(FAILED_DIR):
        return []
    return sorted(os.path.join(FAILED_DIR, f)
                  for f in os.listdir(FAILED_DIR) if f.lower().endswith(".wav"))


def describe(path):
    size = os.path.getsize(path)
    seconds = max(0.0, (size - 44) / (16000.0 * 2))
    return "{}  ({:.0f}s, {:.1f} MB)".format(os.path.basename(path), seconds, size / 1024 / 1024)


def log_history(entry):
    try:
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print("  could not append to history: {}".format(exc))


def copy_to_clipboard(text):
    try:
        from clipboard import ClipboardInjector
        ClipboardInjector().set_clipboard_text(text)
        return True
    except Exception as exc:
        print("  could not set clipboard: {}".format(exc))
        return False


def transcribe_and_polish(engine, wav_bytes):
    """Transcribe + polish one recording and report the same metrics the live
    dictation path does. This was engine.process_audio(); pulled in here since
    this script was its only caller."""
    t0 = time.perf_counter()
    raw_text = engine.transcribe(wav_bytes)
    t_transcribe = time.perf_counter()

    if not raw_text:
        return {"raw_text": "", "cleaned_text": "", "metrics": {}}

    replaced_text = engine.apply_deterministic_replacements(raw_text)
    cleaned_text = engine.polish_text(replaced_text)
    t_polish = time.perf_counter()

    metrics = {
        "audio_seconds": round(getattr(engine, "last_audio_seconds", 0.0), 2),
        "min_logprob": getattr(engine, "last_min_logprob", None),
        "dropped_tail": getattr(engine, "last_dropped_segment", None),
        "dropped_midstream": getattr(engine, "last_dropped_midstream", None) or None,
        "failover": getattr(engine, "last_failover", None),
        "transcribe_ms": round((t_transcribe - t0) * 1000, 1),
        "polish_ms": round((t_polish - t_transcribe) * 1000, 1),
        "total_ms": round((t_polish - t0) * 1000, 1),
    }
    failover_str = "  | Failover: {}".format(metrics["failover"]) if metrics["failover"] else ""
    print("  Transcribe: {}ms | Polish: {}ms | Total: {}ms{}".format(
        metrics["transcribe_ms"], metrics["polish_ms"], metrics["total_ms"], failover_str))
    return {"raw_text": raw_text, "cleaned_text": cleaned_text, "metrics": metrics}


def main(args):
    files = pending()
    if not files:
        print("Nothing pending in {}".format(FAILED_DIR))
        return 0

    if args.list:
        print("{} recording(s) waiting:".format(len(files)))
        for p in files:
            print("  " + describe(p))
        return 0

    engine = DictationEngine()
    last_text = ""
    ok = 0

    for path in files:
        print("Recovering {}".format(describe(path)))
        try:
            with open(path, "rb") as f:
                wav = f.read()
            result = transcribe_and_polish(engine, wav)
        except Exception as exc:
            print("  still failing: {}: {}".format(type(exc).__name__, exc))
            print("  left in place, safe to retry later")
            continue

        text = result.get("cleaned_text", "").strip()
        if not text:
            print("  transcribed to nothing, leaving the file in place")
            continue

        print("  {} words recovered".format(len(text.split())))
        print("  " + "-" * 60)
        print(text)
        print("  " + "-" * 60)

        log_history({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mode": "recovered",
            "cleaned_text": text,
            "raw_text": result.get("raw_text", ""),
            "metrics": result.get("metrics", {}),
            "recovered_from": os.path.basename(path),
        })
        last_text = text
        ok += 1

        if not args.keep:
            os.makedirs(RECOVERED_DIR, exist_ok=True)
            shutil.move(path, os.path.join(RECOVERED_DIR, os.path.basename(path)))

    if last_text and copy_to_clipboard(last_text):
        print("\nMost recent recovery is on the clipboard. Paste with Ctrl+V.")
    print("Recovered {} of {}. Also appended to the dashboard History tab.".format(ok, len(files)))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Re-run dictations that failed to process.")
    ap.add_argument("--list", action="store_true", help="show pending failures and exit")
    ap.add_argument("--keep", action="store_true", help="leave files in failed/ after success")
    sys.exit(main(ap.parse_args()))
