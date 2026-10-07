"""Leonidas Scribe macro miner (data layer).

Finds dictations the user keeps repeating in history.jsonl and proposes turning them into
fixed-text spoken macros (lens_router `snippet` triggers). Suggestions wait in
macro_suggestions.json; nothing is installed until approve() is called.

    python macro_miner.py               # mine real history, print the count
    python macro_miner.py --self-check  # synthetic rows, no network, nonzero exit on failure
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from collections import Counter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(BASE_DIR, "history.jsonl")
PENDING_PATH = os.path.join(BASE_DIR, "macro_suggestions.json")
LENSES_PATH = os.path.join(BASE_DIR, "lenses.json")

SIMILARITY = 0.85
MIN_COUNT = 3
MIN_WORDS = 8        # the spoken trigger is ~4 words; shorter repeats save nothing
MAX_WORDS = 60       # longer texts are essays, not macros, and make the word diff slow
# ponytail: last 3000 rows, greedy O(n^2) clustering on word lists; if history outgrows
# this, bucket by first word or length before comparing instead of raising the cap.
MAX_ROWS = 3000
MAX_SUGGESTIONS = 50

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_lock = threading.Lock()


def _norm(text):
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower()))


def _id(norm_text):
    return hashlib.sha1(norm_text.encode("utf-8")).hexdigest()[:12]


def _looks_like_refusal(text):
    text = text.replace("’", "'")  # Whisper/LLMs emit curly apostrophes; engine triggers use straight ones
    try:
        from engine import looks_like_refusal
        return looks_like_refusal(text)
    except Exception:
        return any(t in text[:160].lower() for t in ("i'm sorry", "i cannot", "as an ai", "i am unable", "i'm unable"))


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _state():
    s = _read_json(PENDING_PATH, {})
    return {"pending": list(s.get("pending", [])), "rejected": list(s.get("rejected", [])),
            "rejected_texts": list(s.get("rejected_texts", []))}


def _usable(row, router):
    text = (row.get("cleaned_text") or "").strip()
    if not text or text.startswith("["):
        return None
    mode = row.get("mode") or ""
    if mode.startswith("macro:") or mode in ("refactor", "reply", "capture") or row.get("preset"):
        return None
    if str((row.get("metrics") or {}).get("polish_model", "")).startswith("unpolished"):
        return None
    if _looks_like_refusal(text):
        return None
    if router is not None and router.match_trigger(text):
        return None
    n = _norm(text)
    if not (MIN_WORDS <= len(n.split()) <= MAX_WORDS):
        return None
    return text


def cluster(rows, router=None):
    """Return [{"text", "count", "norm"}] for near-duplicate dictations repeated MIN_COUNT+ times."""
    counts, display = Counter(), {}
    for row in rows[-MAX_ROWS:]:
        text = _usable(row, router)
        if text is None:
            continue
        n = _norm(text)
        counts[n] += 1
        display.setdefault(n, Counter())[text.strip()] += 1

    clusters = []  # each: {"rep": norm, "words": [...], "count": int, "members": Counter}
    sm = difflib.SequenceMatcher(None, autojunk=False)
    for n, c in counts.most_common():
        words = n.split()
        sm.set_seq1(words)
        for cl in clusters:
            sm.set_seq2(cl["words"])
            if sm.real_quick_ratio() >= SIMILARITY and sm.quick_ratio() >= SIMILARITY and sm.ratio() >= SIMILARITY:
                cl["count"] += c
                cl["members"][n] += c
                break
        else:
            clusters.append({"rep": n, "words": words, "count": c, "members": Counter({n: c})})

    out = []
    for cl in clusters:
        if cl["count"] < MIN_COUNT:
            continue
        rep = cl["members"].most_common(1)[0][0]
        text = display[rep].most_common(1)[0][0]
        out.append({"text": text, "count": cl["count"], "norm": rep})
    out.sort(key=lambda d: (-d["count"], d["text"]))
    return out[:MAX_SUGGESTIONS]


def _trigger_name(text, taken):
    words = re.findall(r"[a-z0-9]+", text.lower())[:3]
    base = "_".join(words) or "macro"
    if not base[0].isalpha():
        base = "m_" + base
    base = base[:36]
    name, i = base, 2
    while name in taken:
        name, i = f"{base}_{i}", i + 1
    return name


def _prefixes(name):
    return ["leonidas " + name.replace("_", " ")]  # same "leonidas <words>" convention as lenses.json


def _is_rejected(norm, state):
    if _id(norm) in state["rejected"]:
        return True
    words = norm.split()
    sm = difflib.SequenceMatcher(None, words, autojunk=False)
    for r in state["rejected_texts"]:
        sm.set_seq2(r.split())
        if sm.ratio() >= SIMILARITY:
            return True
    return False


def _load_history(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows[-MAX_ROWS:]


def load_pending():
    with _lock:
        return _state()["pending"]


def mine(history_path=None):
    """Recompute suggestions from history, drop rejected ones, save and return pending."""
    rows = _load_history(history_path or HISTORY_PATH)
    with _lock:
        from lens_router import VoiceLensRouter
        router = VoiceLensRouter(LENSES_PATH)
        state = _state()
        taken = set(router.triggers) | {p["trigger"] for p in state["pending"]}
        old = {p["id"]: p for p in state["pending"]}
        pending = []
        for cl in cluster(rows, router):
            if _is_rejected(cl["norm"], state):
                continue
            sid = _id(cl["norm"])
            if sid in old:
                name = old[sid]["trigger"]
            else:
                name = _trigger_name(cl["text"], taken)
                taken.add(name)
            pending.append({"id": sid, "text": cl["text"], "count": cl["count"],
                            "trigger": name, "prefixes": _prefixes(name)})
        state["pending"] = pending
        _write_json(PENDING_PATH, state)
        return pending


def approve(suggestion_id, trigger=None, text=None):
    """Install a pending suggestion as a lenses.json snippet trigger; returns its name."""
    with _lock:
        state = _state()
        sug = next((p for p in state["pending"] if p["id"] == suggestion_id), None)
        if sug is None:
            raise ValueError(f"unknown suggestion id: {suggestion_id}")
        name = (trigger or sug["trigger"]).strip().lower().replace(" ", "_").replace("-", "_")
        if not _NAME_RE.match(name):
            raise ValueError(f"invalid trigger name: {name!r}")
        body = (text if text is not None else sug["text"]).strip()
        if not body:
            raise ValueError("macro text is empty")
        lenses = _read_json(LENSES_PATH, None)
        if not isinstance(lenses, dict):
            raise ValueError("lenses.json unreadable")
        triggers = lenses.setdefault("triggers", {})
        prefixes = _prefixes(name)
        used = {p.strip().lower() for cfg in triggers.values() for p in cfg.get("prefixes", [])}
        # an existing prefix that starts the new one (or vice versa) would shadow it
        if name in triggers or any(p.startswith(u) or u.startswith(p) for p in prefixes for u in used):
            raise ValueError(f"trigger name collision: {name}")
        triggers[name] = {"type": "snippet", "prefixes": prefixes, "execution": "paste",
                          "templates": {"default": body}}
        _write_json(LENSES_PATH, lenses)
        state["pending"] = [p for p in state["pending"] if p["id"] != suggestion_id]
        _write_json(PENDING_PATH, state)
        return name


def reject(suggestion_id):
    """Remove from pending and remember it so mine() never proposes it (or a near copy) again."""
    with _lock:
        state = _state()
        sug = next((p for p in state["pending"] if p["id"] == suggestion_id), None)
        if suggestion_id not in state["rejected"]:
            state["rejected"].append(suggestion_id)
        if sug is not None:
            n = _norm(sug["text"])
            if n not in state["rejected_texts"]:
                state["rejected_texts"].append(n)
        state["pending"] = [p for p in state["pending"] if p["id"] != suggestion_id]
        _write_json(PENDING_PATH, state)


def _self_check():
    """Synthetic rows only: no network, never touches the real pending file or lenses.json."""
    global PENDING_PATH, LENSES_PATH
    real = (PENDING_PATH, LENSES_PATH)
    with tempfile.TemporaryDirectory() as d:
        PENDING_PATH = os.path.join(d, "macro_suggestions.json")
        LENSES_PATH = os.path.join(d, "lenses.json")
        _write_json(LENSES_PATH, {"version": "1.0.0", "triggers": {"task": {
            "prefixes": ["leonidas task", "task:"], "execution": "paste",
            "templates": {"default": "- [ ] {title}"}}}})
        row = lambda t, **k: {"cleaned_text": t, "mode": "hands-free", "metrics": {}, **k}
        rows = (
            [row("Please review the diff and tell me what you think.")] * 2
            + [row("Please review the diff and tell me what you think")]
            + [row("Thank you.")] * 5                                       # under 8 words
            + [row("[VOICE REPLY FAILED]: timeout while sending reply now")] * 4
            + [row("I'm sorry, I cannot help with that request at all.")] * 4
            + [row("task: build the thing for the new project today")] * 4  # existing trigger
            + [row("Run the full test suite and report any failures back.", mode="macro:x")] * 4
            + [row("Run the full test suite and report any failures.")] * 2  # only 2
            + [row("Okay is everything working now with the new build?"),
               row("Okay, is everything working now with the new build?"),
               row("Okay is everything working now with the new builds?"),
               row("Something completely different that happens once here.")]
        )
        hist = os.path.join(d, "history.jsonl")
        with open(hist, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        got = mine(hist)
        texts = sorted(p["text"] for p in got)
        assert len(got) == 2, texts
        by_count = {p["count"]: p for p in got}
        assert set(by_count) == {3}, by_count
        assert all(p["trigger"] and p["prefixes"] and len(p["id"]) == 12 for p in got), got
        assert load_pending() == got
        review = next(p for p in got if p["text"].startswith("Please review"))
        assert review["trigger"] == "please_review_the", review

        # approve: valid snippet entry, removed from pending, collision raises
        name = approve(review["id"])
        entry = _read_json(LENSES_PATH, {})["triggers"][name]
        assert entry["type"] == "snippet" and entry["execution"] == "paste", entry
        assert entry["prefixes"] == ["leonidas please review the"], entry
        assert entry["templates"]["default"] == review["text"], entry
        assert all(p["id"] != review["id"] for p in load_pending())
        try:
            approve(review["id"])
            raise AssertionError("approve of a removed id must raise")
        except ValueError:
            pass
        ok = next(p for p in load_pending())
        try:
            approve(ok["id"], trigger="task")
            raise AssertionError("name collision must raise")
        except ValueError:
            pass
        try:
            approve(ok["id"], trigger="Bad Name!")
            raise AssertionError("invalid name must raise")
        except ValueError:
            pass

        # reject persists across mine()
        reject(ok["id"])
        assert load_pending() == []
        again = mine(hist)
        assert all(p["id"] != ok["id"] for p in again), again
        assert ok["id"] in _read_json(PENDING_PATH, {})["rejected"]
    PENDING_PATH, LENSES_PATH = real
    print("macro_miner.py self-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mine repeated Scribe dictations into macro suggestions.")
    ap.add_argument("--self-check", action="store_true", help="run assertions and exit")
    a = ap.parse_args()
    if a.self_check:
        try:
            _self_check()
        except AssertionError as e:
            print(f"macro_miner.py self-check FAILED: {e}")
            sys.exit(1)
        sys.exit(0)
    print(f"{len(mine())} macro suggestion(s) pending")
