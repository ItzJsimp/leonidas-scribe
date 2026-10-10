"""
DashboardController - Manages configuration, dictionary data, history persistence,
audio devices, and provider quota checks for Leonidas Scribe.
"""
import gc
import os
import threading
import time
import json
import requests
import sounddevice as sd
from engine import load_config_file, save_config_file

# One parse per history.jsonl version, shared by every concurrent loader. A dictation write used to
# fan out ~6 pooled jobs that each re-parsed 3MB; their cyclic GC then walked dead PySide wrappers on
# a worker thread and crashed (Windows access violation, crash.log 2026-09-28 21:48).
_HISTORY_LOCK = threading.Lock()
_HISTORY_CACHE = {"key": None, "rows": []}


class DashboardController:
    def __init__(self, config_path=None, dictionary_path=None, history_path=None):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.config_path = config_path or os.path.join(base_dir, "config.json")
        self.dictionary_path = dictionary_path or os.path.join(base_dir, "dictionary.json")
        self.history_path = history_path or os.path.join(base_dir, "history.jsonl")

    # --- Config Operations ---
    def load_config(self) -> dict:
        try:
            return load_config_file(self.config_path)
        except Exception as e:
            print(f"[DashboardController] Error reading config: {e}")
        return {}

    def save_config(self, config: dict) -> tuple[bool, str]:
        try:
            save_config_file(config, self.config_path)
            return True, "Settings saved successfully."
        except Exception as e:
            return False, f"Failed to save settings: {e}"

    # --- Dictionary Operations ---
    def load_dictionary(self) -> dict:
        if os.path.exists(self.dictionary_path):
            try:
                with open(self.dictionary_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[DashboardController] Error reading dictionary: {e}")
        return {"replacements": {}, "vocabulary": []}

    def save_dictionary(self, dict_data: dict) -> tuple[bool, str]:
        try:
            with open(self.dictionary_path, "w", encoding="utf-8") as f:
                json.dump(dict_data, f, indent=2, ensure_ascii=False)
            return True, "Dictionary saved successfully."
        except Exception as e:
            return False, f"Failed to save dictionary: {e}"

    def add_replacement(self, wrong: str, correct: str) -> tuple[bool, str]:
        wrong = wrong.strip()
        correct = correct.strip()
        if not wrong or not correct:
            return False, "Both wrong and correct values must be non-empty."
        data = self.load_dictionary()
        if "replacements" not in data:
            data["replacements"] = {}
        data["replacements"][wrong] = correct
        return self.save_dictionary(data)

    def delete_replacement(self, wrong: str) -> tuple[bool, str]:
        data = self.load_dictionary()
        if "replacements" in data and wrong in data["replacements"]:
            del data["replacements"][wrong]
            return self.save_dictionary(data)
        return False, f"Key '{wrong}' not found in replacements."

    def add_vocabulary(self, term: str) -> tuple[bool, str]:
        term = term.strip()
        if not term:
            return False, "Vocabulary term cannot be empty."
        data = self.load_dictionary()
        if "vocabulary" not in data:
            data["vocabulary"] = []
        if term not in data["vocabulary"]:
            data["vocabulary"].append(term)
            return self.save_dictionary(data)
        return True, "Term already exists."

    def delete_vocabulary(self, term: str) -> tuple[bool, str]:
        data = self.load_dictionary()
        if "vocabulary" in data and term in data["vocabulary"]:
            data["vocabulary"].remove(term)
            return self.save_dictionary(data)
        return False, f"Term '{term}' not found in vocabulary."

    # --- History Operations ---
    def _history_rows(self) -> list:
        """Parsed history.jsonl, oldest first. Callers must not mutate the rows (load_history copies)."""
        try:
            st = os.stat(self.history_path)
        except OSError:
            return []
        key = (self.history_path, st.st_mtime_ns, st.st_size)
        with _HISTORY_LOCK:
            if _HISTORY_CACHE["key"] == key:
                return _HISTORY_CACHE["rows"]
            rows = []
            was_enabled = gc.isenabled()
            gc.disable()  # parsing allocates thousands of dicts; keep cyclic GC off this worker thread
            try:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(entry, dict):  # a stray `null` once hid the whole history
                            rows.append(entry)
            except Exception as e:
                print(f"[DashboardController] Error reading history: {e}")
                return []
            finally:
                if was_enabled:
                    gc.enable()
            _HISTORY_CACHE["key"], _HISTORY_CACHE["rows"] = key, rows
            return rows

    def load_history(self, limit: int = 50, modes: list[str] = None, exclude_modes: list[str] = None) -> list[dict]:
        """modes/exclude_modes filter before the limit is applied, so a rare mode
        (e.g. preset_transform) isn't crowded out by the far more common ones."""
        items = []
        for entry in self._history_rows():
            mode = entry.get("mode")
            if modes and mode not in modes:
                continue
            if exclude_modes and mode in exclude_modes:
                continue
            items.append(entry)
        items.reverse()
        return [dict(e) for e in items[:limit]]

    def delete_history_item(self, target_item: dict) -> tuple[bool, str]:
        if not os.path.exists(self.history_path):
            return False, "History file does not exist."
        all_lines = []
        deleted = False
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                        if (not deleted and 
                            data.get("timestamp") == target_item.get("timestamp") and 
                            data.get("cleaned_text") == target_item.get("cleaned_text")):
                            deleted = True
                            continue
                    except Exception:
                        pass
                    all_lines.append(line_str)
            # Write-then-rename: a crash mid-write can never truncate the history.
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for l in all_lines:
                    f.write(l + "\n")
            os.replace(tmp, self.history_path)
            return deleted, "Item deleted." if deleted else "Item not found."
        except Exception as e:
            return False, f"Error deleting history item: {e}"

    # --- Hardware & Network Telemetry ---
    def get_audio_input_devices(self) -> list[dict]:
        input_devices = []
        try:
            devices = sd.query_devices()
            for idx, d in enumerate(devices):
                if d.get("max_input_channels", 0) > 0:
                    input_devices.append({
                        "index": idx,
                        "name": d.get("name"),
                        "display": f"[{idx}] {d.get('name')}"
                    })
        except Exception as e:
            print(f"[DashboardController] Error querying audio devices: {e}")
        return input_devices

    @staticmethod
    def describe_error(exc) -> str:
        """Map a raw exception to the same short, designed-copy vocabulary the
        HUD already uses (see LeonidasScribe.py's _short_error), instead of
        dumping the exception's own text straight into the UI."""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return "Rate limited"
        if status == 401:
            return "Bad API key"
        if status:
            return f"HTTP {status}"
        name = type(exc).__name__
        if "Timeout" in name:
            return "Timed out"
        if "Connection" in name:
            return "No connection"
        return name[:18]

    QUOTA_ENDPOINTS = (
        ("Groq", "https://api.groq.com/openai/v1/models", None),
        ("Cerebras", "https://api.cerebras.ai/v1/models", "Active (1M Free TPD)"),
        ("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/models", "Active (1,500 Free RPD)"),
    )

    def check_api_quota(self, groq_key: str, cerebras_key: str, gemini_key: str = "") -> tuple[bool, str]:
        """(all_ok, summary). A rejected key or network failure makes all_ok False,
        so the UI never paints a dead key green."""
        keys = (groq_key, cerebras_key, gemini_key)
        if not any(keys):
            return False, "Enter a Groq, Cerebras, or Gemini API Key to check live status."
        status_msgs, failed = [], False
        for (name, url, active), key in zip(self.QUOTA_ENDPOINTS, keys):
            if not key:
                continue
            try:
                resp = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=5)
                if resp.status_code == 200:
                    msg = active or f"{resp.headers.get('x-ratelimit-remaining-requests', '7,190+')} reqs remaining"
                else:
                    failed = True
                    msg = {401: "Key rejected", 403: "Key rejected", 429: "Rate limited"}.get(
                        resp.status_code, f"HTTP {resp.status_code}")
            except Exception as e:
                failed = True
                msg = self.describe_error(e)
            status_msgs.append(f"{name}: {msg}")
        return not failed, " • ".join(status_msgs)

    def check_model_updates(self) -> dict:
        """Queries OpenRouter and model providers to check for new models or free tier changes."""
        results = {
            "timestamp": time.time(),
            "new_models": [],
            "status": "Checked successfully"
        }
        try:
            req = requests.get("https://openrouter.ai/api/v1/models", headers={"User-Agent": "LeonidasScribe/2.0"}, timeout=6)
            if req.status_code == 200:
                data = req.json().get("data", [])
                free_models = [m["id"] for m in data if m.get("pricing", {}).get("prompt") == "0" and m.get("pricing", {}).get("completion") == "0"]
                results["free_openrouter_count"] = len(free_models)
                results["top_free"] = free_models[:8]
        except Exception as e:
            results["status"] = f"OpenRouter check failed: {e}"
        return results

    # --- Insights ---
    # stt_ms only entered the metrics schema on 2026-09-02. Averaging across the
    # boundary reports a fake sub-second STT leg, so latency stats start there.
    METRICS_EPOCH = "2026-09-02"
    TEXT_ONLY_MODES = {"preset_transform", "refactor"}

    # A latency baseline needs enough samples to mean anything; below this a
    # quiet model/lens pairing would "drift" off two or three noisy dictations.
    DRIFT_MIN_BASELINE_SAMPLES = 5
    DRIFT_THRESHOLD = 1.25  # 25% slower than the trailing baseline
    # ponytail: fixed average typing speed; make it a setting if the user measures his own.
    TYPING_WPM = 40

    def load_insights(self) -> dict:
        """Aggregate history.jsonl into the figures the Insights page shows.

        One pass over the file; the whole history is ~500KB, so streaming it is
        cheaper than maintaining a second derived store that can go stale.
        """
        import collections

        words = transcripts = 0
        by_day = collections.Counter()
        dictations_by_day = collections.Counter()  # audio takes only, no transforms
        words_by_day = collections.Counter()
        by_mode = collections.Counter()
        by_app = collections.Counter()
        app_by_day = collections.defaultdict(collections.Counter)
        totals_by_day = collections.defaultdict(list)
        totals, stts, polishes = [], [], []
        by_key_day = collections.defaultdict(lambda: collections.defaultdict(list))
        audio_seconds = 0.0
        # Speaking pace, only from takes where the recorder measured speaking time
        # (speech plus short pauses, no lead/tail silence or thinking pauses).
        pace_words, pace_seconds = 0, 0.0
        pace_by_day = collections.defaultdict(lambda: [0, 0.0])
        first_day = last_day = None

        if not os.path.exists(self.history_path):
            return self._empty_insights()

        try:
            for row in self._history_rows():

                transcripts += 1
                mode = row.get("mode", "unknown")
                m = row.get("metrics") or {}
                # Words you actually spoke: raw Whisper text of audio takes. Transform
                # output and polish edits are not dictation.
                row_words = 0 if mode in self.TEXT_ONLY_MODES else len(
                    (row.get("raw_text") or "").replace("\u23f8", " ").split())
                words += row_words
                day = row.get("timestamp", "")[:10]
                if m.get("speech_seconds") and row_words:
                    pace_words += row_words
                    pace_seconds += m["speech_seconds"]
                    pace_by_day[day][0] += row_words
                    pace_by_day[day][1] += m["speech_seconds"]
                if day and mode not in self.TEXT_ONLY_MODES:
                    dictations_by_day[day] += 1
                if day:
                    by_day[day] += 1
                    words_by_day[day] += row_words
                    first_day = day if first_day is None else min(first_day, day)
                    last_day = day if last_day is None else max(last_day, day)

                by_mode[mode] += 1
                app = (row.get("app") or "").replace(".exe", "")
                if app:
                    by_app[app] += 1
                    if day:
                        app_by_day[day][app] += 1

                audio_seconds += float(m.get("audio_seconds", 0) or 0)
                if (day >= self.METRICS_EPOCH and m.get("total_ms")
                        and mode not in self.TEXT_ONLY_MODES):
                    totals.append(m["total_ms"])
                    totals_by_day[day].append(m["total_ms"])
                    stts.append(m.get("stt_ms", 0))
                    polishes.append(m.get("polish_ms", 0))
                    key = (m.get("stt_model") or "unknown",
                           m.get("polish_model") or "unknown",
                           row.get("lens") or "unknown")
                    by_key_day[key][day].append(m["total_ms"])
        except OSError as e:
            print(f"[DashboardController] Error reading history: {e}")
            return self._empty_insights()

        def pct(values, p):
            if not values:
                return 0
            values = sorted(values)
            return values[min(len(values) - 1, int(len(values) * p / 100))]

        latency_drift = self._detect_latency_drift(by_key_day, pct)

        stt_total, polish_total = sum(stts), sum(polishes)
        leg_total = stt_total + polish_total

        from datetime import date, timedelta
        today_date = date.today()
        today_str = today_date.strftime("%Y-%m-%d")
        today_words = words_by_day.get(today_str, 0)
        today_transcripts = dictations_by_day.get(today_str, 0)

        # Day streak calculation
        curr = today_date
        streak = 0
        if by_day.get(curr.strftime("%Y-%m-%d"), 0) == 0:
            curr = curr - timedelta(days=1)
        while by_day.get(curr.strftime("%Y-%m-%d"), 0) > 0:
            streak += 1
            curr = curr - timedelta(days=1)

        # 7-day average words
        past_7_days = [words_by_day.get((today_date - timedelta(days=i)).strftime("%Y-%m-%d"), 0) for i in range(1, 8)]
        week_avg_words = int(sum(past_7_days) / 7.0) if past_7_days else 0

        # Time saved vs typing: typing the same words at TYPING_WPM minus the time
        # spent speaking them at the measured speaking rate.
        measured_wpm = pace_words / (pace_seconds / 60.0) if pace_seconds >= 30 else 0.0
        speak_wpm = measured_wpm or ((words / (audio_seconds / 60.0)) if audio_seconds > 30 else 0.0) or 130.0
        def saved_min(n_words):
            return max(0.0, n_words / self.TYPING_WPM - n_words / speak_wpm)
        time_saved_min = int(saved_min(today_words))

        # This week (last 7 days, today included) against the 7 days before it.
        def span(start, end):
            return [(today_date - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(start, end)]
        week, prev = span(0, 7), span(7, 14)
        week_apps = collections.Counter()
        for d in week:
            week_apps.update(app_by_day.get(d, {}))
        weekly = {
            "words": sum(words_by_day.get(d, 0) for d in week),
            "prev_words": sum(words_by_day.get(d, 0) for d in prev),
            "transcripts": sum(by_day.get(d, 0) for d in week),
            "prev_transcripts": sum(by_day.get(d, 0) for d in prev),
            "p50": pct([v for d in week for v in totals_by_day.get(d, [])], 50),
            "prev_p50": pct([v for d in prev for v in totals_by_day.get(d, [])], 50),
            "top_app": week_apps.most_common(1)[0][0] if week_apps else "",
            "days": [(d, words_by_day.get(d, 0)) for d in reversed(week)],
        }
        radar = self._latency_radar(by_key_day, pct, {(r["stt_model"], r["polish_model"], r["lens"]) for r in latency_drift})

        return {
            "words": words,
            "transcripts": transcripts,
            "days_active": len(by_day),
            "first_day": first_day,
            "last_day": last_day,
            "today_words": today_words,
            "today_transcripts": today_transcripts,
            "streak_days": streak,
            "time_saved_min": time_saved_min,
            "time_saved_total_hours": saved_min(words) / 60.0,
            "weekly": weekly,
            "radar": radar,
            "week_avg_words": week_avg_words,
            "audio_minutes": audio_seconds / 60.0,
            "wpm": measured_wpm,
            "wpm_estimate": (words / (audio_seconds / 60.0)) if audio_seconds > 30 else 0.0,
            "wpm_samples_minutes": pace_seconds / 60.0,
            "wpm_7d": (lambda w, sec: w / (sec / 60.0) if sec >= 30 else 0.0)(
                sum(pace_by_day[d][0] for d in week), sum(pace_by_day[d][1] for d in week)),
            "today_wpm": (lambda w, sec: w / (sec / 60.0) if sec >= 20 else 0.0)(*pace_by_day[today_str]),
            "today_p50": pct(totals_by_day.get(today_str, []), 50),
            "latency_samples": len(totals),
            "p50": pct(totals, 50),
            "p90": pct(totals, 90),
            "stt_p50": pct(stts, 50),
            "polish_p50": pct(polishes, 50),
            "stt_share": (stt_total / leg_total * 100) if leg_total else 0.0,
            "polish_share": (polish_total / leg_total * 100) if leg_total else 0.0,
            "by_day": dict(by_day),
            "by_mode": by_mode.most_common(),
            "by_app": by_app.most_common(8),
            "latency_drift": latency_drift,
        }

    def time_saved(self, now=None) -> dict:
        """Net time saved vs typing, for today / last 7 days / all history.

        Per dictation: typing time at the user's measured wpm range (config typing_wpm)
        minus what dictating cost (recorded seconds + processing). Not clamped: a
        slow short take counts as negative. Reads only the cached _history_rows()."""
        from datetime import datetime, timedelta
        now = now or datetime.now()
        try:
            lo, hi = sorted(float(w) for w in self.load_config().get("typing_wpm", [65, 75]))
            if lo <= 0:
                raise ValueError
        except Exception:
            lo, hi = 65.0, 75.0
        mid = (lo + hi) / 2.0
        today = now.strftime("%Y-%m-%d")
        week_days = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
        zero = lambda: {"words": 0, "dictations": 0, "s_lo": 0.0, "s_mid": 0.0, "s_hi": 0.0}
        acc = {"today": zero(), "week": zero(), "all": zero()}
        for row in self._history_rows():
            text = (row.get("cleaned_text") or "").strip()
            if row.get("mode") not in ("hands-free", "ptt") or not text or text.startswith("["):
                continue
            m = row.get("metrics") or {}
            words = int(m.get("words") or len(text.split()))
            spent = float(m.get("recorded_seconds") or m.get("audio_seconds") or 0) + float(m.get("total_ms") or 0) / 1000.0
            day = (row.get("timestamp") or "")[:10]
            buckets = ["all"] + (["week"] if day in week_days else []) + (["today"] if day == today else [])
            for b in buckets:
                a = acc[b]
                a["words"] += words
                a["dictations"] += 1
                # wpm low = slowest typing = most saved
                a["s_hi"] += words / lo * 60.0 - spent
                a["s_mid"] += words / mid * 60.0 - spent
                a["s_lo"] += words / hi * 60.0 - spent
        return {k: {"words": a["words"], "dictations": a["dictations"],
                    "saved_min": a["s_mid"] / 60.0,
                    "saved_min_low": a["s_lo"] / 60.0,
                    "saved_min_high": a["s_hi"] / 60.0} for k, a in acc.items()}

    def _detect_latency_drift(self, by_key_day, pct) -> list:
        """Flag a (stt_model, polish_model, lens) pairing whose latency has held
        25%+ above its own trailing 7-day baseline for the last 2 active days.

        Two consecutive days, not one, so a single slow dictation (a long
        pause, a cold API connection) never fires a false alarm; the baseline
        is per-pairing so switching models never drifts against a stranger's
        prior performance.
        """
        from datetime import datetime, timedelta

        drift = []
        for key, day_map in by_key_day.items():
            days_sorted = sorted(day_map.keys())
            if len(days_sorted) < 3:
                continue

            last_two = days_sorted[-2:]
            result = None
            for d in last_two:
                cutoff = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
                baseline_samples = [v for dd in days_sorted if cutoff <= dd < d for v in day_map[dd]]
                if len(baseline_samples) < self.DRIFT_MIN_BASELINE_SAMPLES:
                    result = None
                    break
                baseline_p50 = pct(baseline_samples, 50)
                today_p50 = pct(day_map[d], 50)
                if not baseline_p50 or today_p50 < baseline_p50 * self.DRIFT_THRESHOLD:
                    result = None
                    break
                result = (d, today_p50, baseline_p50)

            if result:
                d, today_p50, baseline_p50 = result
                stt_model, polish_model, lens = key
                drift.append({
                    "stt_model": stt_model, "polish_model": polish_model, "lens": lens,
                    "day": d, "today_p50": today_p50, "baseline_p50": baseline_p50,
                    "pct_over": round((today_p50 / baseline_p50 - 1) * 100, 1),
                })

        drift.sort(key=lambda x: -x["pct_over"])
        return drift

    def _latency_radar(self, by_key_day, pct, drifting) -> list:
        """Every (stt, polish, lens) pairing active in the last 7 days: its latest
        day's p50 against its own trailing 7-day baseline, flagged when the
        drift detector above fires for it."""
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        rows = []
        for key, day_map in by_key_day.items():
            days = sorted(day_map)
            recent = [d for d in days if d >= cutoff]
            if not recent:
                continue
            last = recent[-1]
            base_from = (date.fromisoformat(last) - timedelta(days=7)).isoformat()
            baseline = [v for d in days if base_from <= d < last for v in day_map[d]]
            stt_model, polish_model, lens = key
            rows.append({
                "stt_model": stt_model, "polish_model": polish_model, "lens": lens,
                "day": last, "latest_p50": pct(day_map[last], 50),
                "baseline_p50": pct(baseline, 50) if len(baseline) >= self.DRIFT_MIN_BASELINE_SAMPLES else 0,
                "samples": sum(len(day_map[d]) for d in recent),
                "drifting": key in drifting,
            })
        rows.sort(key=lambda r: (not r["drifting"], -r["samples"]))
        return rows

    @staticmethod
    def _empty_insights() -> dict:
        return {
            "words": 0, "transcripts": 0, "days_active": 0,
            "first_day": None, "last_day": None,
            "today_words": 0, "today_transcripts": 0, "streak_days": 0,
            "time_saved_min": 0, "week_avg_words": 0,
            "time_saved_total_hours": 0.0, "radar": [],
            "weekly": {"words": 0, "prev_words": 0, "transcripts": 0, "prev_transcripts": 0,
                       "p50": 0, "prev_p50": 0, "top_app": "", "days": []},
            "audio_minutes": 0.0, "wpm": 0.0, "wpm_estimate": 0.0, "wpm_samples_minutes": 0.0,
            "wpm_7d": 0.0, "today_wpm": 0.0, "today_p50": 0,
            "latency_samples": 0, "p50": 0, "p90": 0, "stt_p50": 0, "polish_p50": 0,
            "stt_share": 0.0, "polish_share": 0.0, "latency_drift": [],
            "by_day": {}, "by_mode": [], "by_app": [],
        }

    # --- Git Updates Operations ---
    def check_updates(self) -> dict:
        try:
            if getattr(self, "updater", None):
                return self.updater.check_for_updates()
            from updater import GitUpdater
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            updater = GitUpdater(repo_dir=base_dir)
            return updater.check_for_updates()
        except Exception as e:
            return {"ok": False, "error": str(e), "status": f"Check failed: {e}"}

    def apply_update(self) -> dict:
        try:
            if getattr(self, "updater", None):
                applied = self.updater.check_and_apply_update()
                return {"ok": applied, "deferred": not applied and "waiting" in self.updater.last_status.lower(), "status": self.updater.last_status}
            from updater import GitUpdater
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            updater = GitUpdater(repo_dir=base_dir)
            return updater.pull_update()
        except Exception as e:
            return {"ok": False, "error": str(e)}

