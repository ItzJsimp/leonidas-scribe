"""Leonidas Scribe transcript audit.

Reads dictation history written since the last run and asks the polish model to
flag words Whisper probably got wrong. Writes a review file and proposes
dictionary rules. It never edits dictionary.json itself: a bad auto-applied rule
silently corrupts every future dictation, so a human approves each one.

    python Audit-Transcripts.py            # new entries since last run
    python Audit-Transcripts.py --all      # every entry, ignore the watermark
    python Audit-Transcripts.py --apply    # install rules left in the last report
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

import requests

from engine import load_config_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(BASE_DIR, "history.jsonl")
CONFIG = os.path.join(BASE_DIR, "config.json")
DICTIONARY = os.path.join(BASE_DIR, "dictionary.json")
STATE = os.path.join(BASE_DIR, ".audit-state.json")
REPORTS = os.path.join(BASE_DIR, "audits")

BATCH = 15          # transcripts per LLM call, keeps each request well inside limits
MAX_BATCHES = 20    # hard ceiling so a huge backlog cannot run up a bill
MIN_RULE_LEN = 3    # shorter than this matches inside other words
MAX_RETRIES = 4     # 429 is expected when a backlog is audited in one go
RETRY_BACKOFF = 4.0 # seconds, doubled each attempt
BATCH_PAUSE = 2.0   # breathing room between batches

AUDIT_PROMPT = """You review voice dictation transcripts for speech-to-text errors.

The speaker is the user: an entrepreneur who talks about AI tooling, software, content
creation and their own systems. Known correct spellings: {vocab}

For each transcript, find words Whisper almost certainly misheard. Report ONLY:
1. A real word that is clearly wrong for the context (Clawd or Cloud for Claude,
   Jaysun for Jason).
2. An acronym or name flattened into something else ("A" where "AI" was meant).
3. Nonsense tokens that are obviously a mangled real word.

Do NOT report grammar, style, filler words, punctuation, informal phrasing, or
anything you are merely unsure about. A false positive costs more than a miss,
because these become permanent replacement rules.

Return STRICT JSON, no prose, no code fence:
{{"findings": [{{"heard": "<exact wrong text>", "should_be": "<correction>",
  "context": "<up to 8 surrounding words>", "confidence": "high|medium|low"}}]}}
Return {{"findings": []}} if nothing is clearly wrong.

TRANSCRIPTS:
{transcripts}"""


def finding_id(item):
    """Same hash as review_queue.finding_id: how a rejected finding is remembered."""
    return hashlib.sha1((item["heard"] + "\0" + item["should_be"]).lower().encode("utf-8")).hexdigest()[:12]


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_history():
    rows = []
    if not os.path.exists(HISTORY):
        return rows
    with open(HISTORY, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def ask_model(cfg, vocab, chunk):
    api = cfg.get("api", {})
    key = api.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise SystemExit("No Groq API key in secrets.json or GROQ_API_KEY.")

    blob = "\n\n".join("[{}] {}".format(i + 1, t) for i, t in enumerate(chunk))
    payload = {
        "model": api.get("groq_llm_model", "qwen/qwen3.8-27b"),
        "messages": [{"role": "user", "content": AUDIT_PROMPT.format(
            vocab=", ".join(vocab), transcripts=blob)}],
        "temperature": 0.0,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }
    url = api.get("groq_base_url", "https://api.groq.com/openai/v1") + "/chat/completions"
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    # Groq rate limits a burst of batches. It tells us how long to wait in the
    # retry-after header, so honour that instead of hammering and losing the run.
    for attempt in range(MAX_RETRIES):
        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        if resp.status_code != 429:
            break
        wait = float(resp.headers.get("retry-after") or 0) or RETRY_BACKOFF * (2 ** attempt)
        wait = min(wait, 60.0)
        print("    rate limited, waiting {:.0f}s (attempt {}/{})".format(wait, attempt + 1, MAX_RETRIES))
        time.sleep(wait)
    resp.raise_for_status()
    body = resp.json()["choices"][0]["message"]["content"]
    body = re.sub(r"^```(?:json)?|```$", "", body.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(body).get("findings", [])
    except Exception:
        print("  (model returned unparseable JSON, skipping this batch)")
        return []


def merge_findings(findings, known):
    """Collapse duplicates, drop anything already known or unsafe as a rule."""
    merged = {}
    for f in findings:
        heard = (f.get("heard") or "").strip()
        should = (f.get("should_be") or "").strip()
        if not heard or not should or heard.lower() == should.lower():
            continue
        if heard.lower() in known:
            continue
        if len(heard) < MIN_RULE_LEN or len(should) < MIN_RULE_LEN:
            continue
        key = (heard.lower(), should.lower())
        entry = merged.setdefault(key, dict(f, heard=heard, should_be=should, count=0))
        entry["count"] += 1
    return sorted(merged.values(), key=lambda x: (-x["count"], x["heard"].lower()))


def audit(args):
    cfg = load_config_file(CONFIG)  # merges keys from the gitignored secrets.json
    dictionary = load_json(DICTIONARY, {})
    vocab = dictionary.get("vocabulary", [])
    known = set(k.lower() for k in dictionary.get("replacements", {}))

    rows = read_history()
    state = load_json(STATE, {})
    seen = 0 if args.all else int(state.get("entries_audited", 0))
    fresh = rows[seen:]

    if not fresh:
        print("Nothing new to audit ({} entries, all previously reviewed).".format(len(rows)))
        return 0

    texts = [r.get("cleaned_text", "").strip() for r in fresh]
    texts = [t for t in texts if len(t.split()) >= 5]
    print("Auditing {} transcripts (entries {} to {})...".format(len(texts), seen + 1, len(rows)))

    findings = []
    failed = 0
    batches = [texts[i:i + BATCH] for i in range(0, len(texts), BATCH)][:MAX_BATCHES]
    for i, chunk in enumerate(batches, 1):
        print("  batch {}/{}...".format(i, len(batches)))
        try:
            findings.extend(ask_model(cfg, vocab, chunk))
        except Exception as exc:
            print("  batch {} failed: {}".format(i, exc))
            failed += 1
        if i < len(batches):
            time.sleep(BATCH_PAUSE)

    rejected = set(state.get("rejected", []))  # rejected in the app's review queue
    items = [i for i in merge_findings(findings, known) if finding_id(i) not in rejected]

    os.makedirs(REPORTS, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    report = os.path.join(REPORTS, "transcript-audit-{}.md".format(stamp))
    lines = [
        "# Transcript audit: {}".format(stamp), "",
        "Reviewed {} dictations (history entries {} to {}).".format(len(texts), seen + 1, len(rows)),
        "Found {} candidate corrections.".format(len(items)), "",
        "Nothing here is applied automatically. Delete any row you disagree with,",
        "then run `python Audit-Transcripts.py --apply` to install what is left.", "",
    ]
    if items:
        lines += ["| Heard | Should be | Times | Confidence | Context |", "|---|---|---|---|---|"]
        for it in items:
            ctx = (it.get("context") or "").replace("|", "/")[:60]
            lines.append("| `{}` | `{}` | {} | {} | {} |".format(
                it["heard"], it["should_be"], it["count"], it.get("confidence", "?"), ctx))
    else:
        lines.append("No clear transcription errors found.")
    lines.append("")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # A failed batch means those transcripts were never actually looked at.
    # Advancing the watermark anyway would silently skip them forever.
    watermark = seen if failed else len(rows)
    if failed:
        print("{} of {} batches failed. Watermark held at {} so nothing is skipped; "
              "re-run to retry.".format(failed, len(batches), seen))
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"entries_audited": watermark,
                   "last_run": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "last_report": report,
                   "rejected": sorted(rejected),
                   "pending": items}, f, indent=2, ensure_ascii=False)

    print("\nReport: {}".format(report))
    for it in items[:10]:
        print("  {!r} -> {!r}  ({}x, {})".format(
            it["heard"], it["should_be"], it["count"], it.get("confidence")))
    if items:
        print("\nApply with: python Audit-Transcripts.py --apply")
    return 0


def apply_pending(args):
    state = load_json(STATE, {})
    pending = state.get("pending", [])
    if not pending:
        print("Nothing pending. Run the audit first.")
        return 1

    report = state.get("last_report", "")
    approved = None
    if report and os.path.exists(report):
        # Only install rows still present in the report, so deleting a line rejects it.
        text = open(report, encoding="utf-8").read()
        approved = set(p["heard"].lower() for p in pending
                       if "`" + p["heard"] + "`" in text)

    dictionary = load_json(DICTIONARY, {"replacements": {}, "vocabulary": []})
    dictionary.setdefault("replacements", {})
    added = 0
    for p in pending:
        if approved is not None and p["heard"].lower() not in approved:
            continue
        if args.high_only and p.get("confidence") != "high":
            continue
        dictionary["replacements"][p["heard"]] = p["should_be"]
        added += 1
        print("  + {!r} -> {!r}".format(p["heard"], p["should_be"]))

    if added:
        with open(DICTIONARY, "w", encoding="utf-8") as f:
            json.dump(dictionary, f, indent=2, ensure_ascii=False)
    state["pending"] = []
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print("\nInstalled {} rule(s). Restart Leonidas Scribe to load them.".format(added))
    return 0


def _self_check():
    """Runs without touching the network or any real file."""
    known = {"jaysun"}
    raw = [
        {"heard": "Clawd", "should_be": "Claude", "confidence": "high"},
        {"heard": "clawd", "should_be": "claude", "confidence": "high"},
        {"heard": "Jaysun", "should_be": "Jason", "confidence": "high"},
        {"heard": "A", "should_be": "AI", "confidence": "high"},
        {"heard": "same", "should_be": "same", "confidence": "low"},
        {"heard": "", "should_be": "Claude", "confidence": "low"},
    ]
    out = merge_findings(raw, known)
    assert len(out) == 1, out
    assert out[0]["heard"] == "Clawd" and out[0]["count"] == 2, out
    assert not any(i["heard"] == "Jaysun" for i in out), "already-known rule leaked"
    assert not any(len(i["heard"]) < MIN_RULE_LEN for i in out), "unsafe short rule leaked"
    print("Audit-Transcripts.py self-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Audit Leonidas Scribe transcripts for STT errors.")
    ap.add_argument("--all", action="store_true", help="audit every entry, ignore the watermark")
    ap.add_argument("--apply", action="store_true", help="install rules left in the last report")
    ap.add_argument("--high-only", action="store_true", help="with --apply, install high confidence only")
    ap.add_argument("--self-check", action="store_true", help="run assertions and exit")
    a = ap.parse_args()
    if a.self_check:
        _self_check()
        sys.exit(0)
    sys.exit(apply_pending(a) if a.apply else audit(a))
