"""All configuration for Superyap lives here.

Anything that might need tweaking later (thresholds, model names, sizes)
is a constant in this file. Secret values (the API key) come from the
environment or a .env file -- never from hardcoded strings.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Where the app's own files live. Packaged as a single .exe (PyInstaller),
# `__file__` points into a temporary unpack folder, so .env (and other files
# next to the app) must be read from the folder the .exe itself is in.
APP_DIR = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)

# Load a .env file next to the app (if present) into the environment.
load_dotenv(APP_DIR / ".env")

# ---------------------------------------------------------------------------
# Groq API
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

GROQ_API_BASE = "https://api.groq.com/openai/v1"
GROQ_STT_MODEL = "whisper-large-v3-turbo"

# NOTE: verify which chat models are currently free on Groq's rate-limits
# page (https://console.groq.com/docs/rate-limits) -- free models change!
GROQ_CLEANUP_MODEL = "openai/gpt-oss-20b"

# Set to False to skip the LLM cleanup step entirely.
CLEANUP_ENABLED = True

# Skip the cleanup call for very short inputs (fewer than this many words).
CLEANUP_MIN_WORDS = 15

# Reasoning effort for the cleanup model ("low" = fast; cleaning up a
# transcript is simple and does not need deep reasoning).
CLEANUP_REASONING_EFFORT = "low"

# Output budget (tokens) for the cleanup call. Keep this generous: for the
# gpt-oss models the internal reasoning tokens count against this budget, so
# a tight budget makes the model run out of tokens before writing any visible
# text (empty content, finish_reason "length").
CLEANUP_MAX_COMPLETION_TOKENS = 1024

# A single HTTP request timeout, in seconds.
REQUEST_TIMEOUT_S = 20

# How many times to retry after an HTTP 429 (rate limit) before giving up.
RATE_LIMIT_RETRIES = 2

# ---------------------------------------------------------------------------
# Hotkey (milestone 2+)
# ---------------------------------------------------------------------------
# The hotkey is Left Ctrl *alone*. It is only observed, never blocked.
HOTKEY = "left ctrl"

# Held at least this long => push-to-talk. Released sooner => tap (toggle).
HOLD_THRESHOLD_MS = 300

# Show the recording animation only after the key has been held this long.
RECORDING_UI_DELAY_MS = 150

# ---------------------------------------------------------------------------
# Audio (milestone 3+)
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000  # Hz, mono
CHANNELS = 1

# Ignore recordings shorter than this (probably an accidental tap).
MIN_RECORDING_S = 0.3

# Auto-stop a recording after this long.
MAX_RECORDING_S = 5 * 60

# ---------------------------------------------------------------------------
# Pill UI
# ---------------------------------------------------------------------------
# Full-size pill (shown while recording and processing).
PILL_WIDTH = 100
PILL_HEIGHT = 26

# Small, non-invasive pill (shown while idle).
PILL_SMALL_WIDTH = 64
PILL_SMALL_HEIGHT = 12

# Extra room around the pill for its shadow (pixels of transparent padding).
PILL_SHADOW_MARGIN = 14

# How far above the taskbar the pill sits by default (pixels).
PILL_BOTTOM_GAP = 10

# Set this to an (x, y) tuple to force a startup position instead of the
# default bottom-center position, e.g. PILL_DEFAULT_POS = (400, 300).
PILL_DEFAULT_POS = None

# Where the dragged pill position is remembered between runs.
SETTINGS_ORG = "Superyap"
SETTINGS_APP = "Superyap"
