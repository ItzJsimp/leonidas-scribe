"""
Goals: levels, daily goal, quests, achievements and the celebrations they trigger.

Everything is replayed from history.jsonl (no second store that can drift); the only
state is goals_state.json, which remembers what was already celebrated so a burst
fires once. Pure functions, no Qt: safe on worker threads and unit-tested below.

The game rewards the habit Scribe exists for: long, spoken prompts to AI agents.
XP is words spoken, agent prompts earn 1.5x, and a "deep prompt" (100+ words to an
agent) earns a bonus, so talking a problem through always beats a one-liner.
"""
import datetime as dt
import json
import math
import os
import random
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "goals_state.json")

SKIP_MODES = {"preset_transform", "cancelled"}
AGENT_LENSES = {"agent", "prompt", "code"}
AGENT_APPS = {"claude.exe", "antigravity.exe", "hermes.exe", "cursor.exe", "code.exe", "windsurf.exe",
              "codex.exe", "zed.exe", "warp.exe", "windowsterminal.exe", "kiro.exe", "trae.exe"}
TYPING_WPM = 70
DEEP_WORDS = 100
DUMP_WORDS = 300
XP_DEEP_BONUS = 25
XP_QUEST = 150
XP_SWEEP = 200
XP_GOAL = 100

# (level reached, rank). Fast early ranks so a new friend levels up on day one.
RANKS = [
    (1, "Keyboard Hostage"), (3, "Mic Check"), (6, "Talker"), (10, "Prompt Whisperer"),
    (15, "Vibe Coder"), (22, "Agent Wrangler"), (30, "Flow State"), (40, "Voice Architect"),
    (52, "Orchestrator"), (66, "Commander"), (80, "Spartan"), (100, "Leonidas"),
]


def xp_for_level(level: int) -> int:
    """Total XP needed to reach `level` (level 1 = 0)."""
    return int(150 * max(0, level - 1) ** 1.9)


def level_for_xp(xp: float) -> int:
    lvl = int((max(0.0, xp) / 150) ** (1 / 1.9)) + 1
    while xp_for_level(lvl + 1) <= xp:
        lvl += 1
    while lvl > 1 and xp_for_level(lvl) > xp:
        lvl -= 1
    return lvl


def rank_for(level: int) -> str:
    return [name for lv, name in RANKS if lv <= level][-1]


def next_rank(level: int):
    return next(((lv, name) for lv, name in RANKS if lv > level), None)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
def _ts(row):
    try:
        return dt.datetime.fromisoformat(row.get("timestamp", "").replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize(rows: list) -> list:
    """History rows -> [{t, day, words, agent, deep, app, wpm, transform}], oldest first."""
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("mode") == "cancelled":
            continue
        t = _ts(r)
        if t is None:
            continue
        t = t.astimezone()  # naive stamps are local time; aware ones convert to it
        words = len((r.get("cleaned_text") or "").split())
        m = r.get("metrics") or {}
        app = (r.get("app") or "").lower()
        transform = r.get("mode") == "preset_transform"
        agent = not transform and (r.get("lens") in AGENT_LENSES or app in AGENT_APPS)
        out.append({
            "t": t, "day": t.date().isoformat(), "words": 0 if transform else words, "agent": agent,
            "deep": agent and words >= DEEP_WORDS, "app": app, "transform": transform,
            "wpm": float(m.get("wpm") or 0) if words >= 40 else 0.0,
            "speech_s": float(m.get("speech_seconds") or m.get("audio_seconds") or 0.0),
        })
    out.sort(key=lambda x: x["t"])
    return out


def row_xp(x: dict) -> float:
    return x["words"] * (1.5 if x["agent"] else 1.0) + (XP_DEEP_BONUS if x["deep"] else 0)


# ---------------------------------------------------------------------------
# Daily goal (adaptive) and quests
# ---------------------------------------------------------------------------
def daily_goal(words_by_day: dict, day: str, fixed: int = 0) -> int:
    """Fixed when set; else 110% of the last 7 active days' average (min 500), rounded to 50.
    It grows with the user: a new friend aims for 500, a heavy user aims past their norm."""
    if fixed:
        return int(fixed)
    past = [w for d, w in sorted(words_by_day.items()) if d < day and w > 0][-7:]
    avg = sum(past) / len(past) if past else 0
    return max(500, int(math.ceil(avg * 1.1 / 50.0) * 50))


def _burst(rows, n=5, window_s=900):
    ts = [x["t"] for x in rows if not x["transform"]]
    return max((sum(1 for u in ts[i:] if (u - t).total_seconds() <= window_s) for i, t in enumerate(ts)), default=0)


QUEST_POOL = {
    "dump":      ("Brain dump", "Talk one problem all the way through: a 300+ word prompt",
                  lambda rs: (min(1, sum(x["agent"] and x["words"] >= DUMP_WORDS for x in rs)), 1)),
    "apps":      ("Agent hopper", "Dictate prompts into 2 different AI apps",
                  lambda rs: (min(2, len({x["app"] for x in rs if x["agent"] and x["app"]})), 2)),
    "count":     ("Hands off the keyboard", "Make 15 dictations today",
                  lambda rs: (min(15, sum(not x["transform"] for x in rs)), 15)),
    "speed":     ("Speed run", "Hit 180+ words per minute on a 40+ word take",
                  lambda rs: (min(1, sum(x["wpm"] >= 180 for x in rs)), 1)),
    "transform": ("Polish pass", "Rewrite something with a Transform preset",
                  lambda rs: (min(1, sum(x["transform"] for x in rs)), 1)),
    "burst":     ("Flow burst", "5 dictations inside 15 minutes",
                  lambda rs: (min(5, _burst(rs)), 5)),
}
DEEP_QUEST = ("deep", "Go deep", "Send 3 deep prompts (100+ words) to an AI agent",
              lambda rs: (min(3, sum(x["deep"] for x in rs)), 3))


def quests_for(day: str) -> list:
    """3 quests a day: always Go deep, plus 2 picked by the date (same for every user that day)."""
    pick = random.Random(day).sample(sorted(QUEST_POOL), 2)
    return [DEEP_QUEST] + [(k, *QUEST_POOL[k]) for k in pick]


def quest_status(day: str, day_rows: list) -> list:
    out = []
    for qid, title, desc, fn in quests_for(day):
        have, need = fn(day_rows)
        out.append({"id": qid, "title": title, "desc": desc, "have": have, "need": need,
                    "done": have >= need, "xp": XP_QUEST})
    return out


# ---------------------------------------------------------------------------
# Achievements: (id, name, how, glyph, metric key, target)
# ---------------------------------------------------------------------------
ACHIEVEMENTS = [
    ("first_words", "First Words", "Finish your first dictation", "mic", "dictations", 1),
    ("deep_diver", "Deep Diver", "Send a 100+ word prompt to an AI agent", "flash", "longest_prompt", DEEP_WORDS),
    ("brain_dump", "Brain Dump", "Send a 300+ word prompt", "bulb", "longest_prompt", DUMP_WORDS),
    ("monologue", "The Monologue", "Send a 750+ word prompt", "volume", "longest_prompt", 750),
    ("century", "Century", "100 dictations", "check", "dictations", 100),
    ("thousand", "Thousand Takes", "1,000 dictations", "history", "dictations", 1000),
    ("w10k", "10K Club", "Speak 10,000 words", "chart", "words", 10_000),
    ("w100k", "100K Club", "Speak 100,000 words", "chart", "words", 100_000),
    ("w1m", "Million Word Mouth", "Speak 1,000,000 words", "star", "words", 1_000_000),
    ("streak3", "Warming Up", "3-day streak", "sync", "best_streak", 3),
    ("streak7", "Week Warrior", "7-day streak", "sync", "best_streak", 7),
    ("streak30", "Unbreakable", "30-day streak", "sync", "best_streak", 30),
    ("speed", "Speed Demon", "200+ wpm on a 40+ word take", "flash", "best_wpm", 200),
    ("bigday", "Big Day", "3,000 words in one day", "calendar", "best_day", 3000),
    ("marathon", "Marathon", "10,000 words in one day", "calendar", "best_day", 10_000),
    ("polyglot", "Agent Polyglot", "Prompt 3 different AI apps", "people", "agent_apps", 3),
    ("deep100", "Deep Thinker", "100 deep prompts", "bulb", "deep_prompts", 100),
    ("hour_back", "Hour Back", "Save 1 hour versus typing", "clock", "saved_min", 60),
    ("day_back", "Day Back", "Save 8 hours versus typing", "clock", "saved_min", 480),
    ("sweep", "Clean Sweep", "Finish all 3 daily quests in one day", "star", "sweeps", 1),
    ("deep1000", "Deep Legend", "1,000 deep prompts", "bulb", "deep_prompts", 1000),
    ("leviathan", "Leviathan Day", "25,000 words in one day", "calendar", "best_day", 25_000),
    ("fortnight", "Two Weeks Back", "Save 80 hours versus typing", "clock", "saved_min", 4800),
    ("streak100", "Centurion", "100-day streak", "sync", "best_streak", 100),
]


def compute(rows: list, cfg: dict | None = None, now: dt.datetime | None = None) -> dict:
    """Replay history into the full Goals snapshot the UI and the celebrator read."""
    g = (cfg or {}).get("goals") or {}
    xs = normalize(rows)
    now = (now or dt.datetime.now().astimezone())
    today = now.date().isoformat()
    by_day = defaultdict(list)
    for x in xs:
        by_day[x["day"]].append(x)
    words_by_day = {d: sum(x["words"] for x in rs) for d, rs in by_day.items()}

    xp, sweeps, quests_done = 0.0, 0, 0
    for d, rs in sorted(by_day.items()):
        xp += sum(row_xp(x) for x in rs)
        qs = quest_status(d, rs)
        quests_done += sum(q["done"] for q in qs)
        xp += XP_QUEST * sum(q["done"] for q in qs)
        if all(q["done"] for q in qs):
            sweeps += 1
            xp += XP_SWEEP
        if words_by_day[d] >= daily_goal(words_by_day, d, g.get("daily_words", 0)):
            xp += XP_GOAL

    # streaks (days with any dictation)
    days = sorted(d for d, rs in by_day.items() if any(not x["transform"] for x in rs))
    best = run = 0
    prev = None
    for d in days:
        cur = dt.date.fromisoformat(d)
        run = run + 1 if prev and (cur - prev).days == 1 else 1
        best, prev = max(best, run), cur
    streak = 0
    cur = now.date()
    dayset = set(days)
    if cur.isoformat() not in dayset:
        cur -= dt.timedelta(days=1)  # today not started yet: the streak is still alive
    while cur.isoformat() in dayset:
        streak += 1
        cur -= dt.timedelta(days=1)

    agent_rows = [x for x in xs if x["agent"]]
    words = sum(x["words"] for x in xs)
    speech_min = sum(x["speech_s"] for x in xs) / 60.0
    week_ago, fortnight = (now - dt.timedelta(days=7)), (now - dt.timedelta(days=14))
    depth_now = [x["words"] for x in agent_rows if x["t"] >= week_ago]
    depth_prev = [x["words"] for x in agent_rows if fortnight <= x["t"] < week_ago]
    wpms = [x["wpm"] for x in xs if x["wpm"]]
    metrics = {
        "dictations": sum(not x["transform"] for x in xs), "words": words,
        "longest_prompt": max((x["words"] for x in agent_rows), default=0),
        "best_streak": best, "best_wpm": max(wpms, default=0), "best_day": max(words_by_day.values(), default=0),
        "agent_apps": len({x["app"] for x in agent_rows if x["app"]}),
        "deep_prompts": sum(x["deep"] for x in xs),
        "saved_min": max(0.0, words / TYPING_WPM - speech_min), "sweeps": sweeps,
    }
    achievements = [{"id": a, "name": n, "how": h, "glyph": gl, "key": k, "have": metrics[k], "need": t,
                     "unlocked": metrics[k] >= t} for a, n, h, gl, k, t in ACHIEVEMENTS]

    level = level_for_xp(xp)
    lo, hi = xp_for_level(level), xp_for_level(level + 1)
    nr = next_rank(level)
    today_rows = by_day.get(today, [])
    goal = daily_goal(words_by_day, today, g.get("daily_words", 0))
    voice_wpm = sorted(wpms[-200:])[len(wpms[-200:]) // 2] if wpms else 0
    return {
        "xp": int(xp), "level": level, "rank": rank_for(level), "level_floor": lo, "level_ceil": hi,
        "level_frac": (xp - lo) / max(1, hi - lo),
        "next_rank": {"level": nr[0], "name": nr[1]} if nr else None,
        "today": {"day": today, "words": words_by_day.get(today, 0), "goal": goal,
                  "deep": sum(x["deep"] for x in today_rows), "deep_goal": 3,
                  "xp": int(sum(row_xp(x) for x in today_rows)),
                  "dictations": sum(not x["transform"] for x in today_rows)},
        "quests": quest_status(today, today_rows),
        "streak": streak, "best_streak": best,
        "depth_7d": round(sum(depth_now) / len(depth_now)) if depth_now else 0,
        "depth_prev": round(sum(depth_prev) / len(depth_prev)) if depth_prev else 0,
        "voice_wpm": round(voice_wpm), "multiplier": round(voice_wpm / TYPING_WPM, 1) if voice_wpm else 0,
        "metrics": metrics, "achievements": achievements, "quests_done": quests_done,
        "last_row_xp": int(row_xp(xs[-1])) if xs else 0,
        "last_row_deep": bool(xs and xs[-1]["deep"]), "last_t": xs[-1]["t"].isoformat() if xs else "",
    }


# ---------------------------------------------------------------------------
# Celebrations: diff a snapshot against what was already celebrated
# ---------------------------------------------------------------------------
def load_state(path: str = STATE_PATH) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state: dict, path: str = STATE_PATH) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def state_of(snap: dict) -> dict:
    return {
        "level": snap["level"], "achievements": sorted(a["id"] for a in snap["achievements"] if a["unlocked"]),
        "quest_day": snap["today"]["day"], "quests": sorted(q["id"] for q in snap["quests"] if q["done"]),
        "goal_day": snap["today"]["day"] if snap["today"]["words"] >= snap["today"]["goal"] else "",
        "record": snap["metrics"]["longest_prompt"], "last_t": snap["last_t"],
    }


def events(prev: dict | None, snap: dict) -> list:
    """Celebrations owed since `prev` (biggest first). prev None = first run: one welcome, no backlog."""
    if prev is None:
        return [{"kind": "welcome", "size": 3, "title": f"Level {snap['level']}",
                 "body": f"You are a {snap['rank']}. Every word you speak earns XP; deep prompts earn more."}]
    out = []
    if snap["level"] > prev.get("level", 0):
        rank_up = rank_for(prev.get("level", 1)) != snap["rank"]
        out.append({"kind": "level", "size": 3, "title": f"Level {snap['level']}",
                    "body": f"New rank: {snap['rank']}" if rank_up else snap["rank"]})
    names = {a["id"]: a for a in snap["achievements"]}
    for aid in [a["id"] for a in snap["achievements"] if a["unlocked"] and a["id"] not in set(prev.get("achievements", []))]:
        out.append({"kind": "achievement", "size": 2, "title": names[aid]["name"], "body": names[aid]["how"],
                    "glyph": names[aid]["glyph"]})
    day = snap["today"]["day"]
    done_before = set(prev.get("quests", [])) if prev.get("quest_day") == day else set()
    new_q = [q for q in snap["quests"] if q["done"] and q["id"] not in done_before]
    if new_q and all(q["done"] for q in snap["quests"]):
        out.append({"kind": "sweep", "size": 3, "title": "Clean sweep", "body": f"All 3 quests done: +{XP_SWEEP} bonus XP"})
    else:
        for q in new_q:
            out.append({"kind": "quest", "size": 1, "title": f"Quest complete: {q['title']}", "body": f"+{XP_QUEST} XP"})
    if snap["today"]["words"] >= snap["today"]["goal"] and prev.get("goal_day") != day:
        out.append({"kind": "goal", "size": 2, "title": "Daily goal hit",
                    "body": f"{snap['today']['words']:,} of {snap['today']['goal']:,} words. +{XP_GOAL} XP"})
    rec = snap["metrics"]["longest_prompt"]
    if rec > prev.get("record", 0) and rec >= DEEP_WORDS and prev.get("record", 0) > 0:
        out.append({"kind": "record", "size": 2, "title": "New record prompt", "body": f"{rec:,} words in one go"})
    if snap["last_t"] and snap["last_t"] != prev.get("last_t") and snap["last_row_xp"] >= 10:
        out.append({"kind": "xp", "size": 0, "title": f"+{snap['last_row_xp']} XP", "xp": snap["last_row_xp"],
                    "body": "Deep prompt" if snap["last_row_deep"] else ""})
    out.sort(key=lambda e: -e["size"])
    return out


if __name__ == "__main__":
    # self-check: curve, adaptive goal, quests, first-run welcome, level-up and xp events
    assert xp_for_level(1) == 0 and level_for_xp(0) == 1
    assert all(level_for_xp(xp_for_level(n)) == n for n in range(1, 120))
    assert rank_for(1) == "Keyboard Hostage" and rank_for(100) == "Leonidas" and next_rank(100) is None
    assert daily_goal({}, "2026-10-09") == 500
    assert daily_goal({"2026-10-08": 2000}, "2026-10-09") == 2200
    assert daily_goal({"2026-10-08": 2000}, "2026-10-09", fixed=800) == 800
    assert quests_for("2026-10-09")[0][0] == "deep" and len({q[0] for q in quests_for("2026-10-09")}) == 3

    def row(t, words, app="claude.exe", lens="agent", **kw):
        return {"timestamp": t, "mode": "hands-free", "cleaned_text": " ".join(["w"] * words), "app": app,
                "lens": lens, "metrics": kw}
    now = dt.datetime(2026, 10, 9, 20, 0).astimezone()
    rows = [row("2026-10-08T10:00:00", 50), row("2026-10-09T10:00:00", 120)]
    s0 = compute(rows[:1], now=now)
    assert s0["streak"] == 1 and s0["metrics"]["longest_prompt"] == 50
    s1 = compute(rows, now=now)
    assert s1["today"]["deep"] == 1 and s1["metrics"]["longest_prompt"] == 120 and s1["streak"] == 2
    assert s1["xp"] > s0["xp"] and s1["last_row_xp"] == int(120 * 1.5 + XP_DEEP_BONUS)
    assert [e["kind"] for e in events(None, s1)] == ["welcome"]
    ev = events(state_of(s0), s1)
    kinds = {e["kind"] for e in ev}
    assert {"achievement", "xp"} <= kinds and "deep_diver" in json.dumps(state_of(s1))
    assert events(state_of(s1), s1) == []  # nothing owed twice
    print("goals ok")
