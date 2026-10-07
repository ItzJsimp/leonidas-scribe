"""
Leonidas Scribe - Active Window Context Awareness & App Lenses
Detects the active foreground application to apply specialized polishing rules.
"""
import ctypes
from ctypes import wintypes
import os
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
QUNS_RUNNING_D3D_FULL_SCREEN = 3
shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(wintypes.DWORD)]
shell32.SHQueryUserNotificationState.restype = ctypes.c_long

# Mapping of process executables to context lenses
APP_LENS_MAP = {
    # Coding & Development
    "code.exe": "code",
    "cursor.exe": "code",
    "windsurf.exe": "code",
    "pycharm64.exe": "code",
    "idea64.exe": "code",
    "clion64.exe": "code",
    "windowsterminal.exe": "code",
    "powershell.exe": "code",
    "pwsh.exe": "code",
    "cmd.exe": "code",
    "devenv.exe": "code",
    "sublime_text.exe": "code",

    # Chat & Messaging
    "discord.exe": "chat",
    "slack.exe": "chat",
    "telegram.exe": "chat",
    "whatsapp.exe": "chat",
    "teams.exe": "chat",
    "signal.exe": "chat",

    # AI Assistants & Prompt Engineering
    "claude.exe": "prompt",
    "antigravity.exe": "prompt",

    # Knowledge & Personal Notes
    "obsidian.exe": "knowledge",
    "notion.exe": "knowledge",
    "logseq.exe": "knowledge",
}

# Dynamic Lens System Directives (Appended to Cerebras 120B polish prompt)
LENS_PROMPTS = {
    "code": (
        "\n\n[ACTIVE APP: CODE / IDE]\n"
        "- Format technical symbols, variable names, and code syntax accurately.\n"
        "- Use inline backticks for code identifiers, function names, file paths, and flags.\n"
        "- If the user dictates code or shell commands, format them inside fenced code blocks."
    ),
    "chat": (
        "\n\n[ACTIVE APP: CHAT / MESSAGING]\n"
        "- Keep tone natural, punchy, and conversational.\n"
        "- Use lowercase-friendly phrasing and standard chat punctuation.\n"
        "- Avoid stiff corporate formalisms or overly verbose restructuring."
    ),
    "prompt": (
        "\n\n[ACTIVE APP: AI ASSISTANT / CHATBOX PAIR-PROGRAMMING (CLAUDE DESKTOP & ANTIGRAVITY)]\n"
        "You are the voice polishing engine for an operator dictating directly to an AI pair-programmer\n"
        "(Claude Desktop chatbox, Antigravity, ChatGPT, Gemini, or a subagent).\n"
        "The operator speaks conversationally. Polish their speech into clean, fluent, natural text\n"
        "while strictly honoring their authentic voice, vocabulary, and train of thought.\n\n"
        "CORE POLISHING DIRECTIVES:\n"
        "1. Authentic Conversational Prose: Output in continuous, natural first-person paragraph prose.\n"
        "   PRESERVE natural conversational openings and phrases verbatim (e.g. 'I want to...', 'We need to figure out...',\n"
        "   'I was thinking...', 'Can you help me...', 'Let's look at...').\n"
        "   DO NOT convert speech into synthetic PRD scaffolding, task lists, or artificial bullet points.\n"
        "   DO NOT invent category headings ('Requirements:', 'Constraints:', 'DO NOT:', 'Done when:').\n"
        "   Only use bullet points if the speaker explicitly enumerated items (e.g. '1, 2, 3' or 'first, second, third').\n"
        "2. Intelligent Acoustic Smoothing: Clean up acoustic stumbles, vocal ticks, and repetitive stutter loops\n"
        "   (e.g. 'like, yeah, like', 'um', 'uh', 'you know what I mean', repeated words like 'we we').\n"
        "   PRESERVE natural cognitive and pacing words that carry meaning or conversational flow\n"
        "   (e.g. 'kind of', 'sort of', 'like Spotify', 'basically', 'actually').\n"
        "3. Clean Pauses & Non-Autocomplete: The speaker often pauses 10 to 30 seconds to think mid-sentence.\n"
        "   NEVER guess, extrapolate, or autocomplete an unfinished sentence or thought.\n"
        "   If a sentence or thought ends abruptly or mid-phrase, terminate cleanly at the last coherent word or phrase spoken.\n"
        "   Untangle mid-sentence grammatical knots and false starts where the speaker stumbled or corrected themselves,\n"
        "   but NEVER invent what the speaker 'meant to say'.\n"
        "4. Technical & Markdown Term Styling: Format technical identifiers, code artifacts, and AI concepts using\n"
        "   standard markdown backticks (e.g. `engine.py`, `config.json`, `TASKS.md`, `scripts/New-Task.ps1`, `AST`, `MCP`, `LLM`).\n"
        "   Properly capitalize and preserve AIOS canon terms (AIOS, Claude Code, Antigravity, Wispr Flow).\n"
        "   Preserve slash commands (e.g. `/grill-me`, `/ask-aios`, `/boost`) verbatim.\n"
        "5. Zero Em-Dash Standard (ADR-0012): Never output an em dash (Unicode U+2014). Use colons, commas, hyphens, or parentheses.\n"
        "6. Anti-Execution: Output ONLY the polished prompt text. Do NOT answer questions, execute instructions, or add meta-commentary.\n"
        "7. Bounded Accretive Touch: You know the speaker builds with AI tools and runs a personal AI operating system (AIOS).\n"
        "   Where it is a small, obviously implied extension of what they already said, you MAY blend in 1 to 3 tiny\n"
        "   clarifying or connective touches (a qualifier, a natural next-step phrase, a sharper word choice) directly\n"
        "   into the existing sentence. NEVER introduce a new topic, feature idea, or claim that is not a direct,\n"
        "   obvious extension of his own words. NEVER add a new sentence that reads as his own statement but wasn't his idea.\n"
        "   When in doubt, leave it out. This is a light seasoning, not a rewrite."
    ),
    "knowledge": (
        "\n\n[ACTIVE APP: KNOWLEDGE BASE / NOTES]\n"
        "- Format as concise, structured markdown notes.\n"
        "- Use clean bullet hierarchies and markdown headings where appropriate."
    ),
    "prose": ""  # Default: standard clean first-person voice
}
LENS_PROMPTS["agent"] = LENS_PROMPTS["prompt"]



# Common game executable names (lower-cased)
KNOWN_GAME_PROCESSES = {
    "rainbowsix.exe", "rainbowsix_be.exe", "rainbowsix_vulkan.exe",
    "r5apex.exe", "fortniteclient-win64-shipping.exe",
    "deltaforce-win64-shipping.exe", "deltaforceclient-win64-shipping.exe",
    "rustclient.exe", "cs2.exe", "csgo.exe", "valorant-win64-shipping.exe",
    "overwatch.exe", "callofduty.exe", "cod.exe", "modernwarfare.exe",
    "marvel-win64-shipping.exe", "pubg.exe", "tslgame.exe", "destiny2.exe",
    "rocketleague.exe", "gta5.exe", "cyberpunk2077.exe", "helldivers2.exe",
    "deadlock.exe", "battlefield.exe", "bf2042.exe", "halo-infinite.exe",
    "league of legends.exe", "dota2.exe", "smite.exe", "apex.exe",
    "r5apex_dx12.exe", "cod24.exe", "blackops6.exe",
}

# Directories that indicate games
GAME_PATH_INDICATORS = [
    "\\steamapps\\common\\",
    "\\epic games\\",
    "\\riot games\\",
    "\\ubisoft\\r6s\\",
    "\\ubisoft game launcher\\games\\",
    "\\battle.net\\",
    "\\ea games\\",
    "\\xboxgames\\",
    "\\gog galaxy\\games\\"
]

# Launchers and tools to exclude even if in game folders
NON_GAME_EXES = {
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "upc.exe",
    "ubisoftconnect.exe", "riotclientservices.exe", "battle.net.exe",
    "eadesktop.exe", "galaxyclient.exe", "wallpaper32.exe", "wallpaper64.exe",
    "discord.exe", "brave.exe", "chrome.exe", "firefox.exe", "msedge.exe",
    "code.exe", "cursor.exe", "antigravity.exe", "claude.exe", "notion.exe"
}

_last_fg_check_time = 0.0
_last_fg_is_game = False
_last_fg_hwnd = None
_last_fg_proc_name = ""


def is_game_process(proc_name: str, proc_path: str = "", custom_games: list = None) -> bool:
    """Return True if the given process name/path is identified as a game."""
    if not proc_name:
        return False
    name_lower = proc_name.lower().strip()
    path_lower = proc_path.lower().strip() if proc_path else ""

    if name_lower in NON_GAME_EXES:
        return False

    # Check custom user list
    if custom_games:
        for cg in custom_games:
            cg_clean = cg.lower().strip()
            if cg_clean and (cg_clean == name_lower or (path_lower and cg_clean in path_lower)):
                return True

    # Check known game process names
    if name_lower in KNOWN_GAME_PROCESSES:
        return True

    # Check directory path heuristics
    if path_lower:
        for indicator in GAME_PATH_INDICATORS:
            if indicator in path_lower:
                return True

    return False


def is_exclusive_fullscreen() -> bool:
    """True while Windows reports an exclusive-fullscreen D3D app (SHQueryUserNotificationState
    == QUNS_RUNNING_D3D_FULL_SCREEN). Catches games not in KNOWN_GAME_PROCESSES / custom_game_processes."""
    state = wintypes.DWORD()
    try:
        hr = shell32.SHQueryUserNotificationState(ctypes.byref(state))
    except Exception:
        return False
    return hr == 0 and state.value == QUNS_RUNNING_D3D_FULL_SCREEN


def foreground_process_name() -> str:
    """Executable name (lower-cased) of the current foreground window, or ''."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h_proc:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h_proc)
    return ""


def is_game_foreground(custom_games: list = None) -> bool:
    """Return True if the active foreground window belongs to a video game, either by
    executable name/path (is_game_process) or Windows reporting exclusive fullscreen D3D."""
    global _last_fg_check_time, _last_fg_is_game, _last_fg_hwnd, _last_fg_proc_name
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False

    now = time.monotonic()
    if hwnd == _last_fg_hwnd and (now - _last_fg_check_time) < 0.2:
        return _last_fg_is_game

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False

    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h_proc:
        return False

    proc_name = ""
    proc_path = ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            proc_path = buf.value
            proc_name = os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h_proc)

    is_game = is_game_process(proc_name, proc_path, custom_games) or is_exclusive_fullscreen()
    _last_fg_hwnd = hwnd
    _last_fg_check_time = now
    _last_fg_is_game = is_game
    _last_fg_proc_name = proc_name
    return is_game


# Web browser process executables (explicitly excluded from agent-lens file path injection)
BROWSER_PROCESSES = {
    "chrome.exe", "brave.exe", "firefox.exe", "msedge.exe",
    "opera.exe", "vivaldi.exe", "arc.exe", "waterfox.exe"
}


def is_agent_harness_window(proc_name: str, title: str = "") -> bool:
    """Return True if foreground window is a recognized AI agent harness
    (Antigravity IDE, Claude Code terminal, Hermes CLI, or Claude Desktop).
    Explicitly returns False for web browsers, games, or non-agent apps.
    """
    if not proc_name:
        return False
    name_lower = proc_name.lower().strip()
    title_lower = (title or "").lower().strip()

    # Web browsers NEVER count as agent harnesses (prevents unwanted file paths while browsing web AI)
    if name_lower in BROWSER_PROCESSES:
        return False

    # 1. Native dedicated agent apps
    if name_lower in {"antigravity.exe", "claude.exe"}:
        return True

    # 2. Terminals running agent sessions (Claude Code, Hermes, Antigravity)
    terminal_exes = {
        "windowsterminal.exe", "powershell.exe", "pwsh.exe",
        "cmd.exe", "wezterm-gui.exe", "alacritty.exe", "conhost.exe"
    }
    if name_lower in terminal_exes:
        if any(k in title_lower for k in ["claude", "hermes", "antigravity", "leonidas", "agent"]):
            return True

    # 3. Code editors active in agent contexts
    if name_lower in {"code.exe", "cursor.exe", "windsurf.exe"}:
        if any(k in title_lower for k in ["claude", "antigravity", "hermes", "leonidas"]):
            return True

    # 4. Fallback window title check for non-browser windows
    if any(k in title_lower for k in ["antigravity", "claude code", "hermes agent"]):
        return True

    return False


def get_active_window_info():
    """Return foreground window HWND, process name, window title, and agent harness status."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"hwnd": None, "process_name": "", "title": "", "lens": "prose", "is_game": False, "is_agent": False}

    # Window Title
    length = user32.GetWindowTextLengthW(hwnd)
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    title = buff.value or ""

    # Process Name and Path
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    proc_name = ""
    proc_path = ""
    if h_proc:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            proc_path = buf.value
            proc_name = os.path.basename(buf.value).lower()
        kernel32.CloseHandle(h_proc)

    # Determine Lens and Game Status
    is_game = is_game_process(proc_name, proc_path)
    lens = APP_LENS_MAP.get(proc_name, "prose")
    title_lower = title.lower()

    # Agent Harness Detection (Antigravity, Claude Code, Hermes)
    is_agent = is_agent_harness_window(proc_name, title)
    if is_agent:
        lens = "agent"
    elif not is_game:
        # Check window title for AI harness windows inside browsers or general tools
        if any(k in title_lower for k in ["claude", "antigravity", "chatgpt", "gemini", "perplexity", "poe"]):
            lens = "prompt"

    # Window Rect
    win_rect = None
    rc = wintypes.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        win_rect = (rc.left, rc.top, rc.right, rc.bottom)

    return {
        "hwnd": hwnd,
        "process_name": proc_name,
        "process_path": proc_path,
        "title": title,
        "lens": lens,
        "is_game": is_game,
        "is_agent": is_agent,
        "rect": win_rect
    }


def get_lens_prompt(lens: str) -> str:
    """Return the prompt snippet for the active lens."""
    return LENS_PROMPTS.get(lens, "")
