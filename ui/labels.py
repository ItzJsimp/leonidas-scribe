"""Human names for the raw keys history.jsonl stores (modes and process names)."""

MODES = {
    "hands-free": "Hands-free",
    "ptt": "Push-to-talk",
    "preset_transform": "Transform",
    "capture": "Capture",
    "refactor": "Voice refactor",
    "macro:task": "Task macro",
    "macro:review": "Audit macro",
    "macro:slice": "Slice macro",
    "cancelled": "Cancelled, not pasted",
}

APPS = {
    "antigravity": "Antigravity",
    "claude": "Claude",
    "hermes": "Hermes",
    "vivaldi": "Vivaldi",
    "chrome": "Chrome",
    "msedge": "Edge",
    "firefox": "Firefox",
    "code": "VS Code",
    "cursor": "Cursor",
    "pythonw": "Python",
    "pythonw-leonidas-scribe": "Leonidas Scribe",
    "python": "Python",
    "pwsh": "PowerShell",
    "powershell": "PowerShell",
    "windowsterminal": "Terminal",
    "discord": "Discord",
    "explorer": "File Explorer",
    "obsidian": "Obsidian",
    "notepad": "Notepad",
    "slack": "Slack",
}


def mode(key: str) -> str:
    key = key or ""
    if key in MODES:
        return MODES[key]
    return key.replace("_", " ").replace(":", ": ").capitalize() or "Unknown"


def app(name: str) -> str:
    key = (name or "").lower().removesuffix(".exe")
    return APPS.get(key, key.replace("_", " ").title()) if key else ""


if __name__ == "__main__":
    assert mode("preset_transform") == "Transform" and mode("new_mode") == "New mode"
    assert app("pwsh.exe") == "PowerShell" and app("SomeTool.exe") == "Sometool" and app("") == ""
    print("labels ok")
