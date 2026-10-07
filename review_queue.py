"""Leonidas Scribe review queue: one list for everything Scribe wants the user to approve.

Merges three pending sources (all plain JSON reads, safe on the GUI thread):
  shortcut    macro_miner.load_pending()         repeated dictations -> spoken macro
  correction  edit_learner.load_pending()        words the user fixed by hand after a paste
  mishearing  .audit-state.json "pending"        Audit-Transcripts.py findings
"""
import hashlib
import os

import edit_learner
import macro_miner
from macro_miner import _read_json, _write_json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_STATE_PATH = os.path.join(BASE_DIR, ".audit-state.json")
DICTIONARY_PATH = os.path.join(BASE_DIR, "dictionary.json")

SOURCES = {"shortcut": "Repeated dictation", "correction": "You fixed it after pasting",
           "mishearing": "Transcript audit"}


def finding_id(heard, should_be):
    """Stable id of an audit finding. Audit-Transcripts.py computes the same hash to skip rejects."""
    return hashlib.sha1((heard + "\0" + should_be).lower().encode("utf-8")).hexdigest()[:12]


def _audit_pending():
    items = _read_json(AUDIT_STATE_PATH, {}).get("pending", [])
    return [p for p in items if isinstance(p, dict) and p.get("heard") and p.get("should_be")]


def load_all():
    rows = []
    for p in macro_miner.load_pending():
        rows.append({"kind": "shortcut", "id": p["id"], "title": " / ".join(p.get("prefixes") or [p["trigger"]]),
                     "detail": p["text"], "count": int(p.get("count", 1)), "source": SOURCES["shortcut"]})
    for p in edit_learner.load_pending():
        rows.append({"kind": "correction", "id": p["id"], "title": p["heard"], "detail": p["should_be"],
                     "count": int(p.get("count", 1)), "source": SOURCES["correction"]})
    for p in _audit_pending():
        rows.append({"kind": "mishearing", "id": finding_id(p["heard"], p["should_be"]), "title": p["heard"],
                     "detail": p["should_be"], "count": int(p.get("count", 1)), "source": SOURCES["mishearing"]})
    rows.sort(key=lambda r: -r["count"])
    return rows


def _take_audit(item_id):
    state = _read_json(AUDIT_STATE_PATH, {})
    hit = next((p for p in state.get("pending", [])
                if p.get("heard") and p.get("should_be") and finding_id(p["heard"], p["should_be"]) == item_id), None)
    return state, hit


def approve(kind, item_id):
    """Apply one item; returns a short message for a toast."""
    try:
        if kind == "shortcut":
            name = macro_miner.approve(item_id)
            return 'Shortcut added: say "' + macro_miner._prefixes(name)[0] + '"'
        if kind == "correction":
            item = edit_learner.approve(item_id)
            return "Already handled" if item is None else 'Learned: "%s" becomes "%s"' % (item["heard"], item["should_be"])
        if kind == "mishearing":
            state, hit = _take_audit(item_id)
            if hit is None:
                return "Already handled"
            dictionary = _read_json(DICTIONARY_PATH, None)
            if not isinstance(dictionary, dict):
                return "Could not read the dictionary"
            dictionary.setdefault("replacements", {})[hit["heard"]] = hit["should_be"]
            _write_json(DICTIONARY_PATH, dictionary)
            state["pending"] = [p for p in state["pending"] if p is not hit]
            _write_json(AUDIT_STATE_PATH, state)
            return 'Learned: "%s" becomes "%s"' % (hit["heard"], hit["should_be"])
        return "Unknown item"
    except Exception as e:
        return "Could not apply: %s" % e


def reject(kind, item_id):
    try:
        if kind == "shortcut":
            macro_miner.reject(item_id)
        elif kind == "correction":
            edit_learner.reject(item_id)
        elif kind == "mishearing":
            state, hit = _take_audit(item_id)
            rejected = state.setdefault("rejected", [])
            if item_id not in rejected:
                rejected.append(item_id)
            state["pending"] = [p for p in state.get("pending", []) if p is not hit]
            _write_json(AUDIT_STATE_PATH, state)
    except Exception:
        pass
