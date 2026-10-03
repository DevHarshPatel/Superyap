"""Global hotkey handling -- Left Ctrl alone (PRD section 4).

The state machine in this file decides when recording starts and stops:

- Left Ctrl is only OBSERVED, never blocked, so every Windows shortcut
  (Ctrl+C, Ctrl+V, Ctrl+Z, ...) keeps working exactly as before.
- Windows auto-repeats a held key; we react only to the *first* key-down
  and to the key-up.
- Pressing the hotkey starts capturing immediately (so no speech is lost),
  but the recording UI only appears after RECORDING_UI_DELAY_MS.
- If any other key, mouse button, or the mouse wheel happens while the
  hotkey is down, the recording is cancelled and discarded right away
  (that is what keeps Ctrl+C, Ctrl+click, Ctrl+scroll, ... normal). If this
  happens before the UI delay, the UI never changed at all.
- Releasing the hotkey decides the mode:
    * held >= HOLD_THRESHOLD_MS  -> push-to-talk: stop and process now.
    * released sooner            -> tap: keep recording (UI shows at once);
      the next clean press of the hotkey stops and processes.
- Esc while recording cancels it with no processing at all.
- `is_pasting` is the self-trigger guard: while True (set by paster.py while
  we simulate Ctrl+V), no event we generate ourselves is reacted to.

Hook choice: keyboard events come from `pynput` (it reliably distinguishes
Left from Right Ctrl and works on current Python), mouse clicks/wheel come
from the `mouse` package. Both hooks only observe; nothing is ever
suppressed. All callbacks arrive on hook threads; the class emits Qt
signals, which Qt delivers to the GUI thread automatically.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import mouse
from pynput import keyboard as pkeyboard
from PySide6.QtCore import QObject, Signal

import config

# The keys we can use as the hotkey, by name from config.py.
_HOTKEY_KEYS = {
    "left ctrl": pkeyboard.Key.ctrl_l,
    "right ctrl": pkeyboard.Key.ctrl_r,
    "left alt": pkeyboard.Key.alt_l,
    "right alt": pkeyboard.Key.alt_r,
    "left shift": pkeyboard.Key.shift_l,
    "right shift": pkeyboard.Key.shift_r,
}


class HotkeyManager(QObject):
    """Observes the hotkey and turns presses into recording events."""

    # Capture started (audio begins immediately; the UI is not shown yet).
    recording_begun = Signal()
    # Switch the pill to the recording state.
    recording_ui_show = Signal()
    # Recording is finished and ready to be processed.
    recording_stopped = Signal()
    # A recording was discarded and its audio must be thrown away. Emitted
    # for EVERY discarded take, even one cancelled before the UI appeared
    # (otherwise the recorder would keep capturing audio nobody ever sees).
    recording_discarded = Signal()
    # Recording was discarded while the pill was showing it; the pill should
    # go back to idle. NOT emitted when the recording UI was never visible,
    # so the pill "must not change at all" when a shortcut happens before
    # the 150 ms UI delay.
    recording_cancelled = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        hotkey = config.HOTKEY.lower()
        if hotkey not in _HOTKEY_KEYS:
            raise ValueError(
                f"Unsupported HOTKEY {config.HOTKEY!r}; use one of: "
                + ", ".join(sorted(_HOTKEY_KEYS))
            )
        self._hotkey = _HOTKEY_KEYS[hotkey]

        # Self-trigger protection (PRD 4): paster.py sets this while sending
        # the simulated Ctrl+V so we never react to our own keystrokes.
        self.is_pasting = False

        self._pressed = False          # hotkey is physically down right now
        self._recording = False        # a capture session is active
        self._toggle_mode = False      # session was started by a tap
        self._stop_press = False       # current press may stop the session
        self._ui_shown = False         # recording UI is visible
        self._other_input = False      # other input happened during this press
        self._press_started = 0.0      # time.monotonic() of the key-down
        self._ui_timer: Optional[threading.Timer] = None
        self._kb_listener: Optional[pkeyboard.Listener] = None
        self._mouse_hook: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # Hook setup / teardown
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Install the global keyboard and mouse hooks (observe only)."""
        if self._kb_listener is not None:
            return
        self._kb_listener = pkeyboard.Listener(
            on_press=self._on_key_press, on_release=self._on_key_release
        )
        self._kb_listener.start()
        self._mouse_hook = mouse.hook(self._on_mouse_event)

    def stop(self) -> None:
        """Remove all global hooks."""
        if self._kb_listener is not None:
            self._kb_listener.stop()
            self._kb_listener = None
        if self._mouse_hook is not None:
            try:
                mouse.unhook(self._mouse_hook)
            except Exception:
                pass
            self._mouse_hook = None
        self._cancel_ui_timer()

    # ------------------------------------------------------------------
    # Hook callbacks (run on hook threads -- only emit signals from here)
    # ------------------------------------------------------------------
    def _on_key_press(self, key) -> None:
        """Called for every key press anywhere in Windows."""
        if self.is_pasting:
            return  # never react to input we generated ourselves

        if key == self._hotkey:
            self._on_hotkey(True)
        elif key == pkeyboard.Key.esc:
            if self._recording:
                self._cancel()
        elif self._pressed:
            # Any other key while the hotkey is held means the user is
            # really using a shortcut (Ctrl+C, Ctrl+V, ...): discard.
            self._mark_other_input()

    def _on_key_release(self, key) -> None:
        """Called for every key release anywhere in Windows."""
        if self.is_pasting:
            return
        if key == self._hotkey:
            self._on_hotkey(False)

    def _on_mouse_event(self, event) -> None:
        """Called for mouse button clicks and wheel scrolls (moves ignored).

        The `mouse` package sends ButtonEvent (has event_type 'up', 'down',
        'double') and WheelEvent (only has .delta) objects.
        """
        if self.is_pasting:
            return
        event_type = getattr(event, "event_type", "wheel")
        if event_type == "move":
            return
        if self._pressed:
            self._mark_other_input()

    # ------------------------------------------------------------------
    # The hotkey state machine
    # ------------------------------------------------------------------
    def _on_hotkey(self, is_down: bool) -> None:
        if is_down:
            if self._pressed:
                return  # auto-repeat while the key is held: ignore
            self._pressed = True
            self._press_started = time.monotonic()
            self._other_input = False

            if self._recording:
                # Toggle-recording is running; this press might be the
                # "stop" tap. We wait for a clean release before stopping,
                # so Ctrl+C typed mid-recording can still cancel it.
                self._stop_press = True
            else:
                # Start capturing immediately so no speech is lost. The
                # recording UI waits for the 150 ms delay timer.
                self._recording = True
                self._toggle_mode = False
                self._stop_press = False
                self._ui_shown = False
                self.recording_begun.emit()
                self._arm_ui_timer()
        else:
            if not self._pressed:
                return  # stray key-up (e.g. after a shortcut cancelled us)
            self._pressed = False
            held_ms = (time.monotonic() - self._press_started) * 1000.0

            if self._other_input:
                # A shortcut happened during this press; the recording was
                # already discarded at that moment.
                self._stop_press = False
                return

            if self._stop_press:
                # Clean press while toggle-recording: stop and process.
                self._stop_press = False
                self._finish()
                return

            if not self._recording:
                return

            if held_ms >= config.HOLD_THRESHOLD_MS:
                # Push-to-talk: released after a hold -> stop and process.
                self._finish()
            else:
                # Tap: keep recording and show the UI immediately.
                self._toggle_mode = True
                self._show_ui()

    @property
    def is_pressed(self) -> bool:
        """True while the hotkey is physically held down right now."""
        return self._pressed

    def force_finish(self) -> None:
        """Force the current recording to stop and process.

        Used for the maximum-length auto-stop (PRD section 6): the session
        ends even though the user is still holding the hotkey. The key-up
        and any auto-repeats that follow are ignored cleanly.
        """
        self._finish()

    def _mark_other_input(self) -> None:
        """Record that normal input happened while the hotkey is down."""
        self._other_input = True
        if self._recording:
            self._cancel()

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------
    def _show_ui(self) -> None:
        if not self._ui_shown:
            self._ui_shown = True
            self.recording_ui_show.emit()

    def _finish(self) -> None:
        """Stop the recording and hand it over for processing."""
        self._cancel_ui_timer()
        if self._recording:
            self._recording = False
            self._toggle_mode = False
            self._ui_shown = False
            self.recording_stopped.emit()

    def _cancel(self) -> None:
        """Discard the recording (Esc, or a shortcut while the key is down)."""
        self._cancel_ui_timer()
        was_recording = self._recording
        ui_shown = self._ui_shown
        self._recording = False
        self._toggle_mode = False
        self._ui_shown = False
        self._stop_press = False
        if was_recording:
            self.recording_discarded.emit()  # always drop the captured audio
            if ui_shown:
                self.recording_cancelled.emit()
        # If the UI was never shown, recording_cancelled is not emitted: the
        # pill must not change at all.

    def _arm_ui_timer(self) -> None:
        """Start the RECORDING_UI_DELAY_MS timer for the recording UI."""
        self._cancel_ui_timer()
        timer = threading.Timer(config.RECORDING_UI_DELAY_MS / 1000.0, self._on_ui_delay)
        timer.daemon = True
        self._ui_timer = timer
        timer.start()

    def _cancel_ui_timer(self) -> None:
        if self._ui_timer is not None:
            self._ui_timer.cancel()
            self._ui_timer = None

    def _on_ui_delay(self) -> None:
        """Runs on a timer thread after RECORDING_UI_DELAY_MS."""
        # Only show the recording UI if the hotkey is still held and the
        # recording was not cancelled in the meantime.
        if self._recording and self._pressed and not self._other_input:
            self._show_ui()
