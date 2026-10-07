# Leonidas Scribe

> Ultra low-latency, private Windows voice dictation powered by Whisper and fast LLM polishing.

Leonidas Scribe is an open-source, system-wide speech-to-text dictation application for Windows. It captures your speech with sub-second latency, cleans up false starts, pauses, and formatting using smart LLM post-processing, and types it directly into any active application or game.

---

## ⚡ Quick Start (Setup in 60 Seconds)

### 1. Download & Install
```powershell
git clone https://github.com/ItzJsimp/leonidas-scribe.git
cd leonidas-scribe
```
Open PowerShell in the folder (or double-click `Run.bat`):
```powershell
.\Setup.ps1
```
`Setup.ps1` automatically:
- Checks your Python environment (Python 3.10+ required).
- Installs dependencies from `requirements.txt`.
- Compiles the native zero-flicker launcher (`LeonidasScribe.exe`).
- Creates a Desktop shortcut and registers Windows startup on logon.
- Launches the dashboard.

### 2. Enter Your API Keys
On first run, Scribe opens directly to **Settings > Models**:
1. Paste your **Groq API Key** (`gsk_...`).
2. *(Optional)* Add your **Cerebras** or **Gemini** API key for instant backup failover.
3. Click **Save changes** at the bottom.

Your keys are saved locally in `secrets.json`. They are never sent anywhere except directly to official provider APIs and are completely gitignored.

---

## 🔑 Getting Your Free API Keys

Leonidas Scribe runs entirely on free-tier limits:

| Provider | Purpose | Free Tier Limits | Get Key |
|---|---|---|---|
| **Groq** *(Required)* | Whisper STT + Qwen Polish | 7,200 STT reqs/day, 8k tokens/min | [console.groq.com/keys](https://console.groq.com/keys) |
| **Cerebras** *(Optional)* | Ultra-fast Polish | 1,000,000 tokens/day | [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| **Google Gemini** *(Optional)* | Tertiary Failover | 1,500 requests/day | [aistudio.google.com](https://aistudio.google.com) |

> **Tip:** You can check your remaining provider quotas at any time by clicking **Check quotas** in the Settings tab!

---

## 🎙️ How to Use

- **Hands-Free Dictation:** Click **Mouse5** (side mouse button) or press **Ctrl + Shift + Space** to start dictating. Click again to stop and auto-type the polished text.
- **Push-to-Talk:** Hold **Ctrl + Shift + T** (or **Left Ctrl + Left Win**) while speaking, release to insert.
- **Selection Rewrite / Presets:** Select any text in any app and press **Ctrl + Shift + A** to open the transform menu (Clarity, Concise, Friendly, Prompt Engineer).
- **Cancel Dictation:** Press **Escape** to cancel active recording.
- **Open Dashboard:** Click the tray icon or launch `LeonidasScribe.exe`.

---

## 🔄 Automatic Instant Updates

Leonidas Scribe includes a background Git auto-updater:
- It checks for updates from the repository every 60 seconds.
- When an update is pushed, Scribe pulls it cleanly via fast-forward.
- As soon as your current dictation finishes, Scribe seamlessly hot-reloads the new version without interrupting your workflow.
- You can also click **Check for updates** under Settings anytime to sync on demand.
- Local configuration (`secrets.json`, `config.json`, dictation history) is never overwritten.

---

## 🛠️ Optimizations & Troubleshooting

- **Microphone Selection:** Go to **Settings > Audio** and choose your preferred input device (default Windows recording device is selected automatically).
- **Calm Mode:** If you prefer reduced motion, toggle Calm mode under **Settings > Motion & Sound**.
- **Game Mode:** Scribe automatically detects fullscreen games and suspends hotkeys so you never accidentally trigger dictation mid-game.
- **Hotkeys:** You can rebind any shortcut to your liking in **Settings > Hotkeys**.
