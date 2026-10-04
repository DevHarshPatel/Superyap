"""Global hotkey handling -- the Ctrl+Win chord (PRD section 4).

The state machine in this file decides when recording starts and stops:

- The hotkey is a CHORD (by default Left Ctrl + Left Windows, see
  config.HOTKEY). It is only OBSERVED, never blocked, so every Windows
  shortcut keeps working exactly as before.
- Windows auto-repeats a held key; we react only to the moment the last
  chord key goes down ("chord press") and to the first key release
  ("chord release"), never to the repeats.
- Pressing the chord starts capturing immediately (so no speech is lost),
  but the recording UI only appears after RECORDING_UI_DELAY_MS.
- If any other key, mouse button, or the mouse wheel happens while the
  chord is down, the recording is cancelled and discarded right away
  (that is what keeps Ctrl+Win+<key> combinations normal). If this
  happens before the UI delay, the UI never changed at all.
- Releasing the chord decides the mode:
    * held >= HOLD_THRESHOLD_MS  -> push-to-talk: stop and process now.
    * released sooner (a tap)    -> a single tap does nothing at all: it
      only opens the DOUBLE_PRESS_MS window for a second tap. If the chord
      is pressed again inside that window, no-hands mode starts: keep
      recording (UI shows at once) and the next clean chord press stops
      and processes. If no second tap comes, the recording is discarded
      and the UI never changes.
- Esc while recording cancels it with no processing at all, and so does a
  click on the pill's circular cancel button (cancel_from_ui).
- Enter while recording -- or a click on the pill's red record button
  (submit_from_ui) -- finishes the take and processes it.
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

# The keys we can use in the hotkey chord, by name from config.py.
_HOTKEY_KEYS = {
    "left ctrl": pkeyboard.Key.ctrl_l,
    "right ctrl": pkeyboard.Key.ctrl_r,
    "left alt": pkeyboard.Key.alt_l,
    "right alt": pkeyboard.Key.alt_r,
    "left shift": pkeyboard.Key.shift_l,
    "right shift": pkeyboard.Key.shift_r,
    "left windows": pkeyboard.Key.cmd_l,
    "right windows": pkeyboard.Key.cmd_r,
}


def _parse_hotkey(spec: str) -> tuple:
    """Turn a config.HOTKEY string like "left ctrl + left windows" into keys."""
    parts = [p.strip().lower() for p in spec.replace(",", "+").split("+")]
    parts = [p for p in parts if p]
    keys = []
    for part in parts:
        if part not in _HOTKEY_KEYS:
            raise ValueError(
                f"Unsupported HOTKEY {spec!r} (bad key {part!r}); use one of: "
                + ", ".join(sorted(_HOTKEY_KEYS))
                + ", or a chord of them joined with '+'"
            )
        key = _HOTKEY_KEYS[part]
        if key not in keys:  # ignore accidental duplicates
            keys.append(key)
    if not keys:
        raise ValueError("HOTKEY must name at least one key")
    return tuple(keys)


class HotkeyManager(QObject):
    """Observes the hotkey chord and turns presses into recording events."""

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
    # so the pill "must not change at all" when a shortcut or a lone tap
    # happens before the 150 ms UI delay.
    recording_cancelled = Signal()
    # No-hands mode started: the pill should show its red record button.
    record_ui_show = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._hotkey_keys = _parse_hotkey(config.HOTKEY)

        # Self-trigger protection (PRD 4): paster.py sets this while sending
        # the simulated Ctrl+V so we never react to our own keystrokes.
        self.is_pasting = False

        # Optional hit-test (set by main.py): True while the cursor is over
        # the pill's own controls. Clicks there are handled by the pill
        # itself and must not count as "other input".
        self.ui_hit_test: Optional[Callable[[], bool]] = None

        self._down_keys: set = set()  # hotkey keys physically down right now
        self._chord_active = False    # the whole chord is down (a "press")
        self._recording = False       # a capture session is active
        self._toggle_mode = False     # session is in no-hands mode
        self._stop_press = False      # current press may stop the session
        self._pending_tap = False     # one tap seen; waiting for a second
        self._ui_shown = False        # recording UI is visible
        self._other_input = False     # other input happened during this press
        self._press_started = 0.0     # time.monotonic() of the chord press
        self._ui_timer: Optional[threading.Timer] = None
        self._tap_timer: Optional[threading.Timer] = None
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
        self._cancel_tap_timer()

    # ------------------------------------------------------------------
    # Hook callbacks (run on hook threads -- only emit signals from here)
    # ------------------------------------------------------------------
    def _on_key_press(self, key) -> None:
        """Called for every key press anywhere in Windows."""
        if self.is_pasting:
            return  # never react to input we generated ourselves

        if key in self._hotkey_keys:
            self._down_keys.add(key)
            if not self._chord_active and len(self._down_keys) >= len(self._hotkey_keys):
                self._chord_active = True
                self._on_chord_down()
            return  # a chord key on its own is never "other input"

        if key == pkeyboard.Key.enter:
            # Enter confirms the take: stop and process it now.
            if self._recording:
                self._finish()
            elif self._chord_active:
                self._mark_other_input()
            return

        if key == pkeyboard.Key.esc:
            if self._recording:
                self._cancel()
        elif self._chord_active:
            # Any other key while the chord is held means the user is
            # really using a shortcut (Ctrl+Win+<key>, ...): discard.
            self._mark_other_input()

    def _on_key_release(self, key) -> None:
        """Called for every key release anywhere in Windows."""
        if self.is_pasting:
            return
        if key in self._hotkey_keys:
            if key in self._down_keys:
                self._down_keys.discard(key)
                if self._chord_active and len(self._down_keys) < len(self._hotkey_keys):
                    self._chord_active = False
                    self._on_chord_up()

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
        if self._chord_active:
            if self.ui_hit_test is not None and self.ui_hit_test():
                return  # a click on the pill's buttons: the pill handles it
            self._mark_other_input()

    # ------------------------------------------------------------------
    # The hotkey state machine
    # ------------------------------------------------------------------
    def _on_chord_down(self) -> None:
        self._press_started = time.monotonic()
        self._other_input = False

        if self._pending_tap:
            # Second press of the double press: enter no-hands mode. The
            # capture started by the first tap is still running, so no
            # speech is lost. This press STARTS the session, so its release
            # must not stop it again.
            self._cancel_tap_timer()
            self._pending_tap = False
            self._stop_press = False
            self._toggle_mode = True
            self._show_ui()
            self.record_ui_show.emit()  # the red record button pops in
        elif self._recording:
            # No-hands recording is running; this press might be the
            # "stop" press. We wait for a clean release before stopping,
            # so a shortcut typed mid-recording can still cancel the take.
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

    def _on_chord_up(self) -> None:
        held_ms = (time.monotonic() - self._press_started) * 1000.0

        if self._other_input:
            # A shortcut happened during this press; the recording was
            # already discarded at that moment.
            self._stop_press = False
            return

        if self._toggle_mode:
            if self._stop_press:
                # Clean press while no-hands recording: stop and process.
                self._stop_press = False
                self._finish()
            return  # otherwise this was the press that STARTED no-hands

        if not self._recording:
            return

        if held_ms >= config.HOLD_THRESHOLD_MS:
            # Push-to-talk: released after a hold -> stop and process.
            self._finish()
        else:
            # A tap. A single tap does nothing: keep capturing for now and
            # wait for a possible second tap (DOUBLE_PRESS_MS). If none
            # comes, the take is discarded without any UI change.
            self._pending_tap = True
            self._arm_tap_timer()

    @property
    def is_pressed(self) -> bool:
        """True while any hotkey key is physically held down right now.

        The paste code waits for this to become False so the simulated
        Ctrl+V cannot collide with a still-held chord key (a held Windows
        key would turn Ctrl+V into Win+V).
        """
        return bool(self._down_keys)

    def force_finish(self) -> None:
        """Force the current recording to stop and process.

        Used for the maximum-length auto-stop (PRD section 6): the session
        ends even though the user is still holding the hotkey. The key-up
        and any auto-repeats that follow are ignored cleanly.
        """
        self._finish()

    def cancel_from_ui(self) -> None:
        """The pill's cancel button was clicked: discard, no API call."""
        if self._recording:
            self._cancel()

    def submit_from_ui(self) -> None:
        """The pill's red record button was clicked: stop and process."""
        if self._recording:
            self._finish()

    def _mark_other_input(self) -> None:
        """Record that normal input happened while the chord is down."""
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
        self._cancel_tap_timer()
        self._pending_tap = False
        if self._recording:
            self._recording = False
            self._toggle_mode = False
            self._ui_shown = False
            self.recording_stopped.emit()

    def _cancel(self) -> None:
        """Discard the recording (Esc, shortcut, or a lone tap)."""
        self._cancel_ui_timer()
        self._cancel_tap_timer()
        was_recording = self._recording
        ui_shown = self._ui_shown
        self._recording = False
        self._toggle_mode = False
        self._ui_shown = False
        self._stop_press = False
        self._pending_tap = False
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
        # Only show the recording UI if the chord is still held and the
        # recording was not cancelled in the meantime.
        if self._recording and self._chord_active and not self._other_input:
            self._show_ui()

    def _arm_tap_timer(self) -> None:
        """Start the DOUBLE_PRESS_MS window for a second tap."""
        self._cancel_tap_timer()
        timer = threading.Timer(config.DOUBLE_PRESS_MS / 1000.0, self._on_tap_window)
        timer.daemon = True
        self._tap_timer = timer
        timer.start()

    def _cancel_tap_timer(self) -> None:
        if self._tap_timer is not None:
            self._tap_timer.cancel()
            self._tap_timer = None

    def _on_tap_window(self) -> None:
        """Runs on a timer thread after DOUBLE_PRESS_MS without a second tap."""
        if self._pending_tap and self._recording and not self._toggle_mode:
            # A lone tap: it does nothing at all -- drop the take.
            self._pending_tap = False
            self._cancel()
