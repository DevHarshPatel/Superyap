"""Superyap -- free, minimal Wispr Flow-style dictation for Windows.

Hold (or tap) Left Ctrl and speak. When you stop, the take is transcribed
with Groq Whisper and the text is pasted into whatever window has focus
(PR flow: pill -> waveform -> processing -> pasted text, clipboard kept).

`python main.py --demo` cycles all four pill states with fake audio levels.
See README.md for the plan.
"""

import logging
import os
import sys
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import config
import groq_client
import paster
from tray import TrayIcon

# A windowed single .exe (PyInstaller --noconsole) has no console, so
# sys.stdout/sys.stderr are None there. Make print() harmless instead of
# crashing; real diagnostics always go to the log file anyway.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
from hotkey import HotkeyManager
from pill_ui import PillState, PillWindow
from recorder import MicrophoneMonitor, is_usable_audio, wav_duration_seconds

# Simple log file next to the app (PRD section 12). config.APP_DIR is the
# .exe's folder when frozen, the project folder when running from source.
LOG_FILE = config.APP_DIR / "superyap.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("superyap")


class Bridge(QObject):
    """Signals emitted from worker threads; delivered on the GUI thread."""

    show_error = Signal()   # pill: brief red flash, then idle
    show_idle = Signal()    # pill: back to idle


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Superyap")

    if not config.GROQ_API_KEY:
        # Clear console message now; later errors just flash the pill.
        log.error("GROQ_API_KEY is not set -- copy .env.example to .env and add your key")

    pill = PillWindow()
    pill.show()
    bridge = Bridge()

    # System tray: exactly "Start with Windows" + "Quit" (PRD section 5).
    if QSystemTrayIcon.isSystemTrayAvailable():
        pill._tray = TrayIcon()  # kept on pill so it isn't garbage collected
        pill._tray.show()
    else:
        log.warning("System tray is unavailable -- tray icon disabled.")

    if "--demo" in sys.argv:
        # Test hook: cycle the four states with fake speech-like audio levels.
        pill.start_demo()
    else:
        # One microphone service: live level for the waveform + recording.
        # The microphone is opened on hotkey key-down and released the moment
        # the take ends -- nothing is captured while the app is idle.
        monitor = MicrophoneMonitor()
        monitor.level_changed.connect(pill.set_audio_level)
        monitor.error.connect(lambda msg: _fail(pill, bridge, msg))

        # Left Ctrl hotkey (observe only -- normal shortcuts keep working).
        hotkeys = HotkeyManager()
        hotkeys.recording_begun.connect(monitor.start_recording)
        hotkeys.recording_ui_show.connect(lambda: pill.set_state(PillState.RECORDING))
        hotkeys.recording_discarded.connect(monitor.discard_recording)
        hotkeys.recording_cancelled.connect(lambda: pill.set_state(PillState.IDLE))
        hotkeys.recording_stopped.connect(lambda: _on_stopped(monitor, pill, hotkeys, bridge))
        # 5-minute maximum length: the recorder auto-stops and we finish the
        # hotkey session as if the user had released the key.
        monitor.max_length_reached.connect(hotkeys.force_finish)
        hotkeys.start()

        # Worker-thread outcomes arrive on the GUI thread via the bridge.
        bridge.show_error.connect(lambda: pill.set_state(PillState.ERROR))
        bridge.show_idle.connect(lambda: pill.set_state(PillState.IDLE))

        # Keep references so they aren't garbage collected, and clean the
        # global hooks up on exit.
        pill._mic_monitor = monitor
        pill._hotkeys = hotkeys
        pill._bridge = bridge
        app.aboutToQuit.connect(hotkeys.stop)
        app.aboutToQuit.connect(monitor.stop)

    return app.exec()


def _on_stopped(monitor: MicrophoneMonitor, pill: PillWindow, hotkeys, bridge: Bridge) -> None:
    """A recording session finished: keep the audio, transcribe, paste."""
    audio = monitor.stop_recording()

    # PRD section 6: skip processing for very short or silent takes.
    if not is_usable_audio(audio):
        print("Take was too short or silent -- nothing to transcribe.")
        pill.set_state(PillState.IDLE)
        return

    print(f"Recorded {wav_duration_seconds(audio):.1f} s of audio. Transcribing...")
    pill.set_state(PillState.PROCESSING)

    # The API call can take seconds: run it off the GUI thread.
    threading.Thread(
        target=_transcribe_and_paste, args=(audio, hotkeys, bridge), daemon=True
    ).start()


def _transcribe_and_paste(audio: bytes, hotkeys, bridge: Bridge) -> None:
    """Worker thread: transcribe the take and paste the text.

    Never touches the UI directly -- outcomes go through bridge signals.
    """
    try:
        text = groq_client.transcribe(audio)
    except Exception as exc:  # GroqError and anything unexpected
        _fail_message(f"Transcription failed: {exc!r}", bridge, exc_info=True)
        return

    log.info("Raw transcript: %r", text)

    if not text:
        # PRD section 9: never paste an empty result.
        log.info("Transcription was empty; nothing to paste.")
        bridge.show_idle.emit()
        return

    # Light cleanup (milestone 5): filler words, punctuation, self-corrections.
    # Never raises -- it falls back to the raw transcript on any problem, and
    # it logs the exact reason (see groq_client.cleanup).
    final_text = groq_client.cleanup(text)
    if final_text != text:
        log.info("Cleanup changed the transcript.")

    try:
        paster.paste_text(final_text, hotkeys)  # waits for key release, guards self-trigger
    except Exception as exc:
        _fail_message(f"Pasting failed: {exc!r}", bridge, exc_info=True)
        return

    log.info("Final text pasted: %r", final_text)
    bridge.show_idle.emit()


def _fail(pill: PillWindow, bridge: Bridge, message: str) -> None:
    """Show the error flash on the pill and log the message (GUI thread)."""
    _fail_message(message, bridge)


def _fail_message(message: str, bridge: Bridge, exc_info: bool = False) -> None:
    """Log an error and flash the pill red (safe from any thread)."""
    log.error(message, exc_info=exc_info)
    bridge.show_error.emit()


if __name__ == "__main__":
    raise SystemExit(main())
