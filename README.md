# Superyap

**A free, minimal Wispr Flow-style dictation app for Windows: hold Left Ctrl,
speak, and the cleaned-up transcription is pasted into whatever app has focus.**

![Superyap demo](docs/demo.gif)
*(placeholder — replace with a demo GIF or a screenshot of the pill)*

## What it does

Superyap runs quietly in the background showing only a tiny floating pill.
Hold **Left Ctrl**, say what you want to write, and let go — your words are
transcribed with Groq Whisper, lightly cleaned up (filler words removed,
punctuation fixed, self-corrections applied), and pasted at the cursor in the
app you were using. Your clipboard is put back afterwards, and normal
shortcuts like Ctrl+C keep working exactly as before.

## Features

- Push-to-talk (hold Left Ctrl) and tap-to-toggle dictation
- Shortcut-safe hotkey: Ctrl+C, Ctrl+V, Ctrl+Z, Ctrl+click and Ctrl+scroll
  work normally and simply cancel the take
- Esc cancels a recording with nothing pasted
- Floating pill UI: idle, recording (live microphone waveform), processing,
  brief red error flash
- Never steals focus — no taskbar button, no Alt+Tab entry
- Draggable pill whose position is remembered between runs
- Groq Whisper transcription + LLM cleanup, with automatic fallback to the
  raw transcript if cleanup fails for any reason
- Pastes into the active window via the clipboard and restores your original
  clipboard afterwards
- System tray menu: "Start with Windows" toggle + Quit
- Full debug trail of every dictation in `superyap.log`
- Builds to a single standalone `.exe` (PyInstaller)

## How it works

1. Press **Left Ctrl** (hold it, or tap it) — the microphone opens and
   recording starts immediately.
2. Speak; the pill shows your live microphone level as a waveform.
3. Stop: release the key after holding it, or tap it again if you tapped
   first. Press **Esc** to cancel instead.
4. The take (16 kHz mono WAV, kept in memory only) is sent to **Groq Whisper**
   (`whisper-large-v3-turbo`) for transcription.
5. The transcript goes to a **Groq LLM** (`openai/gpt-oss-20b`) for light
   cleanup: filler words, punctuation, obvious slips, self-corrections. If
   this call fails or returns something unusable, the raw transcript is used
   instead — dictation never breaks because of cleanup.
6. The final text is put on the clipboard and pasted with a simulated
   **Ctrl+V** into the active app, then your original clipboard is restored.

## Requirements

- Windows 10/11
- Python 3.11 or newer (https://www.python.org/downloads/ — tick
  "Add python.exe to PATH" during setup)
- A free Groq API key (https://console.groq.com/keys)
- A microphone

## Installation

```powershell
git clone https://github.com/<your-username>/Superyap.git
cd Superyap
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell refuses to run scripts, run
`Set-ExecutionPolicy -Scope Process RemoteSigned` first, or use `cmd` instead
(`.venv\Scripts\activate.bat`).

Then open `.env` and replace `your_key_here` with your Groq API key.

## Running it

```powershell
python main.py          # with a console window (handy: logs visible)
pythonw main.py         # with no console window at all
python main.py --demo   # cycle the pill states with fake audio
```

(With the venv activated. Without activating:
`.venv\Scripts\pythonw.exe main.py`.)

## Usage

- **Hold Left Ctrl** for at least ~0.3 s and speak, then release → the take
  is transcribed, cleaned up and pasted (push-to-talk).
- **Tap Left Ctrl** briefly → recording toggles on (the pill shows it at
  once); the next clean tap stops and pastes.
- **Esc** while recording → cancel. Nothing is transcribed or pasted.
- Any other key, mouse click, or scroll while Left Ctrl is held → the take is
  discarded instantly, so normal shortcuts keep working.
- Drag the pill anywhere with the mouse; the position is remembered.

**Pill states**

| Pill | Meaning |
|---|---|
| Small, dim dot | Idle |
| Waving bars | Recording (bars follow your voice level) |
| Pulsing dots | Processing (transcription + cleanup) |
| Red flash | Error — details in `superyap.log` |

**Tray menu** (notification area): **Start with Windows** (toggle) and
**Quit**.

## Configuration

All knobs are constants in `config.py`; the API key lives in `.env`.

| Setting | Default | What it does |
|---|---|---|
| `GROQ_API_KEY` (`.env`) | *(none)* | Your Groq API key — never hardcoded |
| `GROQ_API_BASE` | `https://api.groq.com/openai/v1` | Groq API endpoint |
| `GROQ_STT_MODEL` | `whisper-large-v3-turbo` | Speech-to-text model |
| `GROQ_CLEANUP_MODEL` | `openai/gpt-oss-20b` | LLM used for cleanup |
| `CLEANUP_ENABLED` | `True` | Set `False` to paste raw transcripts |
| `CLEANUP_MIN_WORDS` | `10` | Skip cleanup for shorter takes |
| `CLEANUP_REASONING_EFFORT` | `"low"` | Reasoning effort for the cleanup model |
| `CLEANUP_MAX_COMPLETION_TOKENS` | `1024` | Output budget for cleanup (reasoning tokens count against it) |
| `REQUEST_TIMEOUT_S` | `20` | Timeout per Groq request (seconds) |
| `RATE_LIMIT_RETRIES` | `2` | Retries after HTTP 429 |
| `HOTKEY` | `"left ctrl"` | The dictation key (see supported list below) |
| `HOLD_THRESHOLD_MS` | `300` | Held this long → push-to-talk; shorter → tap-toggle |
| `RECORDING_UI_DELAY_MS` | `150` | Delay before the recording animation shows |
| `SAMPLE_RATE` | `16000` | Microphone sample rate (Hz) |
| `CHANNELS` | `1` | Mono capture |
| `MIN_RECORDING_S` | `0.3` | Takes shorter than this are ignored |
| `MAX_RECORDING_S` | `300` | Auto-stop a take after this many seconds |
| `PILL_WIDTH` / `PILL_HEIGHT` | `100` / `26` | Full-size pill (recording/processing) |
| `PILL_SMALL_WIDTH` / `PILL_SMALL_HEIGHT` | `64` / `12` | Idle pill size |
| `PILL_SHADOW_MARGIN` | `14` | Transparent padding around the pill (shadow) |
| `PILL_BOTTOM_GAP` | `10` | Default gap above the taskbar (pixels) |
| `PILL_DEFAULT_POS` | `None` | Force a startup position `(x, y)` instead of bottom-center |
| `SETTINGS_ORG` / `SETTINGS_APP` | `"Superyap"` | Registry key where the dragged position is stored |

Supported `HOTKEY` values: `left ctrl`, `right ctrl`, `left alt`,
`right alt`, `left shift`, `right shift`. Anything else is rejected at
startup with an error in the log.

## Building a standalone .exe

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m PyInstaller superyap.spec
Copy-Item .env dist\.env
```

The result is `dist\Superyap.exe` — one file (~60 MB), the pill icon, no
console window. Re-run the last-but-one command after every code change.

Good to know:

- First launch is a little slower (a single-file exe unpacks itself to a
  temporary folder each start).
- The `.env` is read from the exe's folder and is deliberately **not** baked
  into the exe, so your API key is never inside the file you share.
- Windows SmartScreen and some antivirus tools warn about unsigned PyInstaller
  exes — expected without a code-signing certificate.

### Running at startup

Use the tray menu item **Start with Windows**. It writes one value under
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` — pointing at
`pythonw.exe` + `main.py` when running from source, or at `Superyap.exe` when
packaged. Check or remove it with:

```powershell
reg query HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v Superyap
reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v Superyap
```

## Troubleshooting

| Problem | What to do |
|---|---|
| Text is not pasted into some windows | Windows blocks synthetic input into apps running **as administrator**. Run Superyap as administrator too, or don't run the target app elevated. |
| The hotkey does nothing | Check `HOTKEY` in `config.py` (see supported values above) and restart the app. Other keyboard-hook tools can also interfere. |
| Pill flashes red, nothing pasted | Usually a missing/invalid `GROQ_API_KEY`. Copy `.env.example` to `.env`, set your key, restart. Details are in `superyap.log`. |
| HTTP 429 rate limit | Groq free-tier limit. The app waits and retries (per the `Retry-After` header), then shows the error flash — wait a minute and try again. |
| Cleanup model not available / no longer free | Free models change. Raw text is pasted instead when cleanup fails; check https://console.groq.com/docs/rate-limits and update `GROQ_CLEANUP_MODEL`. |
| Microphone not detected / silent takes | Windows Settings → Privacy & security → Microphone: allow desktop apps. Also check the default input device. |
| Antivirus/SmartScreen warning on the exe | Heuristics for unsigned exes. Build from source yourself or add an exception. |
| Where are the logs? | `superyap.log` next to the app: next to `Superyap.exe` when packaged, in the project folder when running from source. |

## Privacy and security

- While you dictate, the recorded audio and the transcript text are sent to
  Groq for transcription and cleanup
  (https://console.groq.com/docs/privacy-policy). Nothing else leaves the app.
- Audio is recorded **in memory only** — the microphone is open only while a
  take runs, and no audio or transcript is ever saved to disk or kept as
  history. The log file does record what was transcribed and pasted (for
  debugging) and stays on your PC.
- The API key stays in your local `.env`, which is gitignored and never
  committed or baked into the exe. **Never share your `.env` file.**

## Free-tier limits

Groq's free-tier limits (requests per minute/day, audio seconds) change over
time — see the current numbers at
https://console.groq.com/docs/rate-limits instead of trusting any list here.

## Project structure

```
Superyap/
├── main.py           # entry point: wires UI, hotkey, mic, Groq, paste
├── config.py         # all settings + .env loading
├── hotkey.py         # Left Ctrl state machine (hold/tap, Esc, shortcut guard)
├── recorder.py       # mic capture (open only while recording, in-memory takes)
├── groq_client.py    # Whisper transcription + LLM cleanup with fallback
├── paster.py         # clipboard save/restore + simulated Ctrl+V
├── pill_ui.py        # the floating pill (4 states, dragging, no focus stealing)
├── tray.py           # tray icon: Start with Windows + Quit
├── superyap.spec     # PyInstaller build script (single .exe)
├── app_icon.ico      # icon for the .exe
├── requirements.txt  # dependencies
├── .env.example      # config template (copy to .env)
└── README.md
```

## Known limitations

- Pasting into windows running as administrator requires running Superyap as
  administrator too (Windows restriction).
- Clipboard save/restore handles **text only** — a copied image is replaced
  by the pasted text.
- The cleanup step adds one API call per dictation (~0.3 s); when it fails,
  the raw transcript is pasted.
- The microphone opens when you press the hotkey (it stays closed while
  idle), so capture begins a few dozen milliseconds after key-down.
- No settings UI, hotkey remapping UI, history, or streaming partial text —
  by design. Edit `config.py` to change behavior.
- Windows only (by design).

## License

MIT — see [LICENSE](LICENSE).
