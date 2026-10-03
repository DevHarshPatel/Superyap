"""Pasting text into whatever window currently has focus.

The trick (PRD section 9): save the clipboard, put the text on it, send a
simulated Ctrl+V, then put the user's original clipboard back. While this
happens the hotkey listener ignores all input (`is_pasting`) so the app
never triggers itself with its own keystrokes.

Known limitation: `pyperclip` only handles text, so a non-text clipboard
(e.g. a copied image) cannot be saved and restored.
"""

from __future__ import annotations

import time

import pyautogui
import pyperclip

# We only send keystrokes; make sure pyautogui's corner-of-screen fail-safe
# can't throw in the middle of a paste.
pyautogui.FAILSAFE = False

# How long to wait for the hotkey to be physically released before pasting,
# so the simulated Ctrl+V doesn't collide with a held Left Ctrl (PRD 4).
RELEASE_WAIT_S = 5.0

# Short pause so the clipboard content is fully set before Ctrl+V goes out.
CLIPBOARD_SETTLE_S = 0.05

# How long the text stays on the clipboard before the original is restored
# (PRD section 9 says about 150-300 ms).
CLIPBOARD_HOLD_S = 0.2


def paste_text(text: str, hotkeys=None) -> bool:
    """Paste `text` into the active window. Returns False if there was no text.

    `hotkeys` is the HotkeyManager: its `is_pasting` flag is raised during
    the paste (self-trigger protection) and its `is_pressed` state tells us
    whether the hotkey is still physically held.
    """
    text = (text or "").strip()  # never paste trailing newlines (PRD 9)
    if not text:
        return False

    # PRD section 4: wait until the hotkey is physically released so the
    # simulated Ctrl+V cannot collide with a still-held Left Ctrl.
    deadline = time.monotonic() + RELEASE_WAIT_S
    while hotkeys is not None and hotkeys.is_pressed and time.monotonic() < deadline:
        time.sleep(0.05)

    # 1. Save the user's clipboard (text only; see module docstring).
    try:
        original = pyperclip.paste()
    except pyperclip.PyperclipException:
        original = None

    try:
        # 2. Self-trigger protection ON.
        if hotkeys is not None:
            hotkeys.is_pasting = True

        # 3. Put the final text on the clipboard.
        pyperclip.copy(text)
        time.sleep(CLIPBOARD_SETTLE_S)

        # 4. Paste into the active window.
        pyautogui.hotkey("ctrl", "v")

        # 5. Give the target app time to read the clipboard before it is
        #    taken away again.
        time.sleep(CLIPBOARD_HOLD_S)
        return True
    finally:
        # Restore the user's original clipboard (even if Ctrl+V failed)
        # and always put the protection flag back down.
        if original is not None:
            try:
                pyperclip.copy(original)
            except pyperclip.PyperclipException:
                pass
        if hotkeys is not None:
            hotkeys.is_pasting = False
