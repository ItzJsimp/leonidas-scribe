"""Leonidas Scribe edit learner (data layer, no GUI).

~25 s after a dictation is pasted, read the focused field back with UI Automation and
diff it against what Scribe pasted. Small word swaps the user made by hand (1-3 words
each side) become pending correction candidates in learned_corrections.json. Nothing
reaches dictionary.json until approve() is called.

Fail closed: every error here is swallowed; this never touches a dictation.
"""
import difflib
import hashlib
import json
import os
import re
import threading
import time

from macro_miner import _read_json, _write_json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "learned_corrections.json")
DICTIONARY_PATH = os.path.join(BASE_DIR, "dictionary.json")

CHECK_DELAY = 25.0
MIN_SIMILARITY = 0.6
MAX_REPLACE_OPS = 3
MAX_SIDE_WORDS = 3
MAX_FIELD_CHARS = 20000

_lock = threading.Lock()  # guards learned_corrections.json only; the dictation path never takes it


def _tokens(text):
    out = []
    for t in (text or "").split():
        t = re.sub(r"^\W+|\W+$", "", t)
        if t:
            out.append(t)
    return out


def _best_window(pw, fw):
    """(similarity, start, end) of the stretch of fw that best matches pw."""
    sm = difflib.SequenceMatcher(None, pw, fw, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if not blocks:
        return 0.0, 0, 0
    top = max(blocks, key=lambda b: b.size)
    est = top.b - top.a
    best = (0.0, 0, 0)
    n = len(pw)
    for start in range(max(0, est - 4), min(len(fw), est + 5)):
        for length in range(max(1, n - 4), n + 5):
            seg = fw[start:start + length]
            if not seg:
                continue
            r = difflib.SequenceMatcher(None, pw, seg, autojunk=False).ratio()
            if r > best[0]:
                best = (r, start, start + len(seg))
    return best


def extract_corrections(pasted, field_text):
    """[(heard, should_be)] for small word swaps between pasted and the edited field text."""
    try:
        pw, fw = _tokens(pasted), _tokens(field_text)
        if not pw or not fw:
            return []
        ratio, start, end = _best_window(pw, fw)
        if ratio < MIN_SIMILARITY:
            return []
        seg = fw[start:end]
        ops = [o for o in difflib.SequenceMatcher(None, pw, seg, autojunk=False).get_opcodes()
               if o[0] == "replace"]
        if not ops or len(ops) > MAX_REPLACE_OPS:
            return []
        out = []
        for _, i1, i2, j1, j2 in ops:
            if not (1 <= i2 - i1 <= MAX_SIDE_WORDS and 1 <= j2 - j1 <= MAX_SIDE_WORDS):
                continue
            heard, should = " ".join(pw[i1:i2]), " ".join(seg[j1:j2])
            if heard != should:
                out.append((heard, should))
        return out
    except Exception:
        return []


def _id(heard, should_be):
    return hashlib.sha1((heard.lower() + "\0" + should_be).encode("utf-8")).hexdigest()[:12]


def _state():
    s = _read_json(STATE_PATH, {})
    return {"pending": list(s.get("pending", [])), "rejected": list(s.get("rejected", []))}


def load_pending():
    with _lock:
        return _state()["pending"]


def record(pasted, pairs):
    """Merge candidate pairs into the pending file (count += 1 for a repeat)."""
    if not pairs:
        return
    known = {k.lower() for k in _read_json(DICTIONARY_PATH, {}).get("replacements", {})}
    with _lock:
        state = _state()
        for heard, should in pairs:
            cid = _id(heard, should)
            if cid in state["rejected"] or heard.lower() in known:
                continue
            item = next((p for p in state["pending"] if p["id"] == cid), None)
            if item is None:
                item = {"id": cid, "heard": heard, "should_be": should, "count": 0, "example": ""}
                state["pending"].append(item)
            item["count"] += 1
            item["example"] = (pasted or "").strip()[:160]
            item["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write_json(STATE_PATH, state)


def approve(item_id):
    """Add the replacement to dictionary.json and drop it from pending. Returns the item or None."""
    with _lock:
        state = _state()
        item = next((p for p in state["pending"] if p["id"] == item_id), None)
        if item is None:
            return None
        dictionary = _read_json(DICTIONARY_PATH, None)
        if not isinstance(dictionary, dict):
            raise ValueError("dictionary.json unreadable")
        dictionary.setdefault("replacements", {})[item["heard"]] = item["should_be"]
        _write_json(DICTIONARY_PATH, dictionary)
        state["pending"] = [p for p in state["pending"] if p["id"] != item_id]
        _write_json(STATE_PATH, state)
        return item


def reject(item_id):
    with _lock:
        state = _state()
        if item_id not in state["rejected"]:
            state["rejected"].append(item_id)
        state["pending"] = [p for p in state["pending"] if p["id"] != item_id]
        _write_json(STATE_PATH, state)


def _foreground_hwnd():
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow()


def read_focused_text():
    """Text of the focused UI element via UI Automation, or None (password, unsupported, error)."""
    import comtypes
    import comtypes.client
    comtypes.CoInitialize()
    try:
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
                                           interface=UIA.IUIAutomation)
        el = uia.GetFocusedElement()
        if el.CurrentIsPassword:
            return None
        try:
            vp = el.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern)
            value = vp.CurrentValue
            if value:
                return value
        except Exception:
            pass
        tp = el.GetCurrentPattern(UIA.UIA_TextPatternId).QueryInterface(UIA.IUIAutomationTextPattern)
        return tp.DocumentRange.GetText(MAX_FIELD_CHARS)
    finally:
        comtypes.CoUninitialize()


def _check(pasted, hwnd):
    try:
        if not hwnd or _foreground_hwnd() != hwnd:
            return
        text = read_focused_text()
        if text:
            record(pasted, extract_corrections(pasted, text))
    except Exception:
        return


def schedule_check(pasted, hwnd, delay=CHECK_DELAY):
    """Fire-and-forget: check the field once, `delay` seconds from now, on a daemon thread."""
    try:
        t = threading.Timer(delay, _check, args=(pasted, hwnd))
        t.daemon = True
        t.start()
    except Exception:
        pass
