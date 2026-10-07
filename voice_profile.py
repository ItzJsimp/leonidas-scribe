"""
Weekly voice profile: what the user dictated this week and how he speaks.

Every number is computed by code from history.jsonl and dictionary.json. One
model call (the polish provider, a few cents a year) writes only the prose:
what the week was about. If that call fails the report still ships without it.

Scribe builds a report by itself when the latest one is 7+ days old (checked at
startup and every 6 hours); the Insights page shows the newest. Reports live in
reports/ (gitignored: they quote the user's own speech).

    python voice_profile.py            build now and print the JSON path
    python voice_profile.py --selftest  offline checks
"""
import collections
import datetime as dt
import difflib
import glob
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(BASE, "reports")
TEXT_ONLY = {"preset_transform", "refactor"}
PAUSE = "⏸"
STOP = set("""
a an the and or but if then so to of in on for with at by from as is are was were be been
being do does did have has had this that these those i you he she it we they my your his her
its our their me him them us not no yes just really very can could would should will shall
may might about into over under again here there all any both each few more most other some
such only own same than too also what when where which who why how im i'm it's that's don't
dont i've i'll we're you're there's can't isn't want like going gonna wanna get got make sure
know think right now okay yeah basically actually kind sort thing things something way lot
one two need even still well much many every any also because though through
""".split())
FILLERS = ("like", "kind of", "sort of", "you know", "basically", "i mean", "actually",
           "literally", "honestly", "right now", "okay", "yeah", "so yeah", "things like that")


def _rows(history_path: str):
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("mode") not in TEXT_ONLY:
                    yield row
    except OSError:
        return


def _day(row) -> dt.date:
    return dt.date.fromisoformat(row.get("timestamp", "1970-01-01")[:10])


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(PAUSE, " ")).strip()


def _tokens(text: str) -> list:
    return re.findall(r"[a-z0-9][a-z0-9'.-]*[a-z0-9]|[a-z0-9]", text.lower())


def compute_stats(rows: list, prev_rows: list, replacements: dict) -> dict:
    """Pure: the whole report except the model-written summary."""
    raw = [_plain(r.get("raw_text")) for r in rows]
    words = sum(len(t.split()) for t in raw)
    speech_s = sum((r.get("metrics") or {}).get("speech_seconds") or 0 for r in rows)
    audio_s = sum((r.get("metrics") or {}).get("audio_seconds") or 0 for r in rows)
    measured_words = sum(len(_plain(r.get("raw_text")).split()) for r in rows
                         if (r.get("metrics") or {}).get("speech_seconds"))
    wpm = round(measured_words / (speech_s / 60)) if speech_s >= 30 else (
        round(words / (audio_s / 60)) if audio_s >= 30 else 0)

    toks = [t for text in raw for t in _tokens(text)]
    top_words = collections.Counter(t for t in toks if len(t) >= 4 and t not in STOP).most_common(15)

    grams = collections.Counter()
    for text in raw:
        tk = _tokens(text)
        for n in (3, 4, 5):
            for i in range(len(tk) - n + 1):
                g = tk[i:i + n]
                if sum(w not in STOP for w in g) >= 1 and g[0] not in ("and", "the", "of", "to"):
                    grams[" ".join(g)] += 1
    phrases = []
    for g, c in grams.most_common(200):
        if c < 3:
            break
        if any(g in p and c <= pc for p, pc in phrases):
            continue  # a longer phrase already covers it
        phrases = [(p, pc) for p, pc in phrases if not (p in g and pc <= c)]
        phrases.append((g, c))
        if len(phrases) >= 8:
            break

    joined = " " + " ".join(t.lower() for t in raw) + " "
    fillers = sorted(((f, len(re.findall(r"(?<![\w'])" + re.escape(f) + r"(?![\w'])", joined))) for f in FILLERS),
                     key=lambda x: -x[1])
    fillers = [(f, n, round(n / max(words, 1) * 1000, 1)) for f, n in fillers if n][:8]

    fixes = collections.Counter()
    if replacements:
        rx = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in sorted(replacements, key=len, reverse=True))
                        + r")(?!\w)", re.IGNORECASE)
        lookup = {k.lower(): v for k, v in replacements.items()}
        for text in raw:
            for m in rx.finditer(text):
                if lookup[m.group(0).lower()].lower() != m.group(0).lower():
                    fixes[(m.group(0), lookup[m.group(0).lower()])] += 1
    removed = collections.Counter()
    for r in rows:
        a, b = _tokens(_plain(r.get("raw_text"))), _tokens(r.get("cleaned_text") or "")
        for op, i1, i2, _, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            gone = a[i1:i2]
            # Single glue words ("and", "the") are grammar fixes, not habits.
            if op == "delete" and len(gone) <= 3 and not (len(gone) == 1 and gone[0] in STOP and gone[0] not in FILLERS):
                removed[" ".join(gone)] += 1

    apps = collections.Counter((r.get("app") or "").lower().removesuffix(".exe") for r in rows if r.get("app"))
    hours = collections.Counter(int(r["timestamp"][11:13]) for r in rows if len(r.get("timestamp", "")) > 13)
    days = collections.Counter(_day(r).strftime("%A") for r in rows)
    longest = max(rows, key=lambda r: len(_plain(r.get("raw_text")).split()), default=None)
    prev_words = sum(len(_plain(r.get("raw_text")).split()) for r in prev_rows)
    return {
        "dictations": len(rows),
        "words": words,
        "prev_words": prev_words,
        "speaking_minutes": round((speech_s or audio_s) / 60, 1),
        "wpm": wpm,
        "wpm_measured": speech_s >= 30,
        "top_apps": apps.most_common(5),
        "top_words": top_words,
        "catchphrases": phrases,
        "fillers": fillers,
        "corrections": [(f"{a} -> {b}", n) for (a, b), n in fixes.most_common(10)],
        "polish_removed": [(w, n) for w, n in removed.most_common(12) if n >= 2][:10],
        "busiest_hour": hours.most_common(1)[0][0] if hours else None,
        "busiest_day": days.most_common(1)[0][0] if days else None,
        "longest": {"words": len(_plain(longest.get("raw_text")).split()),
                    "when": longest.get("timestamp")} if longest else None,
    }


SUMMARY_PROMPT = (
    "You write a short weekly voice profile for the person who dictated the transcripts below "
    "(their own speech, addressed to AI tools and apps). Return JSON only, with keys: "
    '"headline" (one sentence, second person, what their week was about), '
    '"themes" (3 to 6 short strings: the main projects or topics, most time first), '
    '"style" (one or two sentences on how they speak and ask for things, second person), '
    '"tip" (one concrete suggestion to dictate or prompt better, grounded in what you read). '
    "Never quote private details beyond project names. No em dashes."
)


def summarize(texts: list, engine=None) -> dict:
    """The only model call. Returns {} on any failure; the report ships without it."""
    try:
        if engine is None:
            from engine import DictationEngine
            engine = DictationEngine()
        url, key, model = engine._resolve_provider(engine.primary_polish_provider, engine.primary_polish_model)
        if not key:
            return {}
        budget, picked = 24000, []
        step = max(1, len(texts) // 120)
        for t in texts[::step]:
            if budget <= 0:
                break
            picked.append(t[:1500])
            budget -= len(picked[-1])
        payload = {"model": model, "temperature": 0.2, "max_tokens": 900,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": SUMMARY_PROMPT},
                                {"role": "user", "content": "\n---\n".join(picked)}]}
        if "qwen" in model.lower():
            payload["reasoning_effort"] = "none"
        r = engine.session.post(f"{url}/chat/completions", json=payload, timeout=40,
                                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        r.raise_for_status()
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        out["model"] = model
        return {k: (v.replace(chr(0x2014), ", ") if isinstance(v, str) else v) for k, v in out.items()}
    except Exception as exc:
        print(f"[VoiceProfile] Summary skipped: {exc}")
        return {}


def build(history_path=None, dictionary_path=None, today=None, engine=None, with_summary=True) -> str:
    history_path = history_path or os.path.join(BASE, "history.jsonl")
    dictionary_path = dictionary_path or os.path.join(BASE, "dictionary.json")
    today = today or dt.date.today()
    start, prev_start = today - dt.timedelta(days=7), today - dt.timedelta(days=14)
    rows, prev = [], []
    for r in _rows(history_path):
        d = _day(r)
        if start <= d < today:
            rows.append(r)
        elif prev_start <= d < start:
            prev.append(r)
    try:
        replacements = json.load(open(dictionary_path, encoding="utf-8")).get("replacements", {})
    except (OSError, ValueError):
        replacements = {}
    report = {"generated": dt.datetime.now().isoformat(timespec="seconds"),
              "period": [start.isoformat(), (today - dt.timedelta(days=1)).isoformat()],
              "stats": compute_stats(rows, prev, replacements)}
    if with_summary and rows:
        report["summary"] = summarize([r.get("cleaned_text") or "" for r in rows], engine)
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"voice-profile-{today.isoformat()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return path


def latest() -> dict:
    files = sorted(glob.glob(os.path.join(REPORTS, "voice-profile-*.json")))
    if not files:
        return {}
    try:
        with open(files[-1], encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def is_due(days: int = 7) -> bool:
    rep = latest()
    if not rep:
        return True
    return (dt.datetime.now() - dt.datetime.fromisoformat(rep["generated"])).days >= days


def _selftest():
    rows = [{"timestamp": "2026-09-20T10:00:00-0700", "mode": "hands-free", "app": "claude.exe",
             "raw_text": f"I want to make sure the Leonidas Scribe app works {PAUSE} like really well", "cleaned_text":
             "I want to make sure the Leonidas Scribe app works really well.",
             "metrics": {"speech_seconds": 20}}] * 4
    s = compute_stats(rows, [], {"Leo and I": "Leonidas", "Scribe": "Scribe"})
    assert s["dictations"] == 4 and s["words"] == 4 * 13, s["words"]
    assert any("leonidas scribe" in p for p, _ in s["catchphrases"]), s["catchphrases"]
    assert ("like", 4, round(4 / 52 * 1000, 1)) in s["fillers"], s["fillers"]
    assert ("like", 4) in s["polish_removed"], s["polish_removed"]
    assert s["corrections"] == [], "identity rules are not corrections"
    assert s["top_apps"] == [("claude", 4)]
    print("voice_profile selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(build())
