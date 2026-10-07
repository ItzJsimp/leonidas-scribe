"""
Leonidas Scribe - Voice Lens & Spoken Macro Expander Router (ADR-0041)
Provides declarative trigger routing, context-gated phonetic auto-repair,
and window-adaptive command expansion.
"""
import os
import re
import json


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


class VoiceLensRouter:
    def __init__(self, lenses_path=None):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.lenses_path = lenses_path or os.path.join(base_dir, "lenses.json")
        self._last_mtime = 0
        self.config = {}
        self.triggers = {}
        self.context_gated_repairs = {}
        self._compiled_repairs = []
        self._load_config()

    def _load_config(self):
        if not os.path.exists(self.lenses_path):
            self.config = {}
            self.triggers = {}
            self.context_gated_repairs = {}
            self._compiled_repairs = []
            return

        try:
            mtime = os.path.getmtime(self.lenses_path)
            with open(self.lenses_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
            self._last_mtime = mtime
            self.triggers = self.config.get("triggers", {})
            self.context_gated_repairs = self.config.get("context_gated_repairs", {})

            # Compile repair regexes sorted by phrase length descending
            compiled = []
            sorted_repairs = sorted(self.context_gated_repairs.items(), key=lambda x: len(x[0]), reverse=True)
            for phrase, replacement in sorted_repairs:
                pattern = re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
                compiled.append((pattern, replacement))
            self._compiled_repairs = compiled
        except Exception as e:
            print(f"[VoiceLensRouter] Failed to load lenses.json: {e}")

    def _check_reload(self):
        if not os.path.exists(self.lenses_path):
            return
        try:
            mtime = os.path.getmtime(self.lenses_path)
            if mtime != self._last_mtime:
                self._load_config()
        except Exception:
            pass

    def is_aios_context(self, text: str, context_lens: str = "prose") -> bool:
        """Determine if active context or spoken utterance implies AIOS operational scope."""
        if context_lens in ("code", "prompt", "knowledge", "agent"):
            return True
        stripped = text.strip().lower()
        if stripped.startswith("leonidas"):
            return True
        return False

    def apply_context_repairs(self, text: str, context_lens: str = "prose") -> str:
        """Apply phonetic slip repairs only when in AIOS context."""
        if not text:
            return ""
        self._check_reload()

        if not self.is_aios_context(text, context_lens):
            return text

        result = text
        for pattern, replacement in self._compiled_repairs:
            result = pattern.sub(replacement.replace("\\", "\\\\"), result)
        return result

    def match_trigger(self, text: str) -> dict | None:
        """Check if spoken text begins with a registered macro trigger prefix."""
        if not text:
            return None
        self._check_reload()

        stripped = text.strip()
        lower_text = stripped.lower()

        # Build list of all candidate prefixes sorted by length descending
        candidates = []
        for name, cfg in self.triggers.items():
            for pfx in cfg.get("prefixes", []):
                candidates.append((pfx.strip().lower(), name, cfg))
        candidates.sort(key=lambda x: len(x[0]), reverse=True)

        for pfx, name, cfg in candidates:
            if cfg.get("type") == "snippet":
                # Fixed text: only the whole utterance may match, so a stray trailing word is
                # never swallowed. Words compare punctuation-free (Whisper adds commas/periods).
                if _words(lower_text) == _words(pfx):
                    return {
                        "trigger_name": name,
                        "matched_prefix": pfx,
                        "type": "snippet",
                        "execution": cfg.get("execution", "paste"),
                        "handler": cfg.get("handler"),
                        "payload": "",
                        "templates": cfg.get("templates", {})
                    }
                continue
            # Match prefix followed by colon, space, or end of string
            if lower_text == pfx or lower_text.startswith(pfx + ":") or lower_text.startswith(pfx + " "):
                # Extract payload
                matched_len = len(pfx)
                if lower_text.startswith(pfx + ":"):
                    matched_len += 1
                payload = stripped[matched_len:].lstrip(": ").strip()
                return {
                    "trigger_name": name,
                    "matched_prefix": pfx,
                    "type": cfg.get("type", "template"),
                    "execution": cfg.get("execution", "paste"),
                    "handler": cfg.get("handler"),
                    "payload": payload,
                    "templates": cfg.get("templates", {})
                }

        # Explicit prefixes only. A semantic classifier (TypeSafe Jev, 2026-09-24 to
        # 2026-09-25) replaced 4 of 4 real dictations with "/os-audit" because they
        # mentioned an audit, and added ~370ms to every dictation. A macro that can
        # delete what the user said must be something he says on purpose.
        return None

    def resolve_target_type(self, process_name: str, lens: str) -> str:
        """Map active foreground process and lens to template target type."""
        proc = (process_name or "").lower()
        if proc in ("windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe"):
            return "terminal"
        if proc in ("claude.exe",) or lens == "prompt":
            return "ai_chat"
        return "default"

    def extract_parameters(self, trigger_name: str, payload: str) -> dict:
        """Extract structured parameters from payload.
        Concise inputs are parsed via regex; complex multi-sentence inputs
        can be enriched by caller.
        """
        clean = payload.strip()
        if trigger_name == "task":
            proj_match = re.search(r"\s+(?:under|for|in)\s+([a-zA-Z0-9_-]+)$", clean, re.IGNORECASE)
            project = None
            title = clean
            if proj_match:
                project = proj_match.group(1)
                title = clean[:proj_match.start()].strip()
            return {"title": title, "project": project, "raw": clean}
        return {"title": clean, "instruction": clean, "raw": clean}

    def render_expansion(self, trigger_name: str, target_type: str, payload: str) -> str:
        """Render prompt or command expansion from template."""
        self._check_reload()
        cfg = self.triggers.get(trigger_name, {})
        templates = cfg.get("templates", {})
        if cfg.get("type") == "snippet":
            return templates.get("default", "")  # fixed text, pasted verbatim, no substitution
        tpl = templates.get(target_type) or templates.get("default", "{instruction}")

        params = self.extract_parameters(trigger_name, payload)
        clean_title = params.get("title", payload.strip())
        clean_instr = params.get("instruction", payload.strip())

        expanded = tpl.replace("{title}", clean_title).replace("{instruction}", clean_instr)
        return expanded
