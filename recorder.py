"""Microphone access: in-memory recording of takes.

The microphone is opened only when a take starts (hotkey key-down) and is
released the moment the take ends (stop, cancel, 5-minute auto-stop, or app
exit) -- nothing is captured while the app is idle. While a take runs:

- `level_changed(float)` is emitted continuously (~60x/s) with the smoothed
  microphone level, which drives the pill's waveform.
- Audio is kept in memory and `stop_recording()` returns the take as WAV bytes
  ready for the Groq upload (16 kHz mono 16-bit PCM; 5 minutes ~= 9.6 MB, well
  under Groq's 25 MB free-tier limit, so no FLAC needed).

Every path that ends a take also clears the audio buffers.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal

import config

# Absolute noise floor: RMS below this is always treated as silence.
SILENCE_FLOOR_RMS = 0.0015

# Below this fraction of the current peak level is treated as silence too,
# so room noise doesn't wiggle the bars on a sensitive microphone.
SILENCE_RELATIVE = 0.06

# The running peak decays slowly so the bars adapt to quiet and loud mics
# alike (a tiny auto-gain). 0.999 per callback at ~60 callbacks/s ~= half
# the level after ~10 seconds of true silence.
PEAK_DECAY = 0.999
PEAK_FLOOR = 0.01

# A take whose loudest sample is below this (fraction of full scale) counts
# as silence and is not worth transcribing (PRD section 6).
SILENCE_PEAK = 0.01


class MicrophoneMonitor(QObject):
    """Records takes on demand -- the microphone is open only while a take runs.

    Signals:
    - `level_changed(float)`: live level 0.0 (silence) to 1.0 (loud).
    - `max_length_reached()`: the recording hit MAX_RECORDING_S and was
      auto-stopped; the caller should finish the session (PRD section 6).
    - `error(str)`: the microphone could not be opened.
    """

    level_changed = Signal(float)
    max_length_reached = Signal()
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stream: sd.InputStream | None = None
        self._peak = PEAK_FLOOR  # running peak of the RMS (for auto-gain)

        # --- Recording session state ---
        self._capturing = False
        self._blocks: list[bytes] = []   # int16 PCM blocks, in order
        self._captured_samples = 0

    # ------------------------------------------------------------------
    # Microphone open/close -- only while a take actually runs
    # ------------------------------------------------------------------
    def _open_stream(self) -> bool:
        """Open the default input device. On failure emit `error` and return False."""
        if self._stream is not None:
            return True
        try:
            try:
                name = sd.query_devices(kind="input")["name"]
                print(f"Using microphone: {name}")
            except Exception:
                pass
            self._stream = sd.InputStream(
                samplerate=config.SAMPLE_RATE,
                channels=config.CHANNELS,
                dtype="float32",
                blocksize=256,  # 16 ms of audio per callback at 16 kHz
                callback=self._on_audio,
            )
            self._stream.start()
            return True
        except Exception as exc:  # missing mic, busy device, driver issues...
            self._stream = None
            self.error.emit(f"Microphone unavailable: {exc}")
            return False

    def _close_stream(self) -> None:
        """Release the microphone and clear every audio buffer."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._blocks = []
        self._captured_samples = 0

    def stop(self) -> None:
        """App is quitting: release the microphone and drop any audio."""
        self._capturing = False
        self._close_stream()

    # ------------------------------------------------------------------
    # Recording sessions
    # ------------------------------------------------------------------
    def start_recording(self) -> None:
        """Open the microphone and begin capturing (hotkey key-down).

        Discards any old take first. If the microphone cannot be opened,
        nothing is captured and `error` is emitted.
        """
        self._capturing = False
        self._close_stream()  # drop any leftovers from an earlier take
        self._peak = PEAK_FLOOR
        if not self._open_stream():
            return
        self._capturing = True

    def stop_recording(self) -> bytes | None:
        """End the take: release the microphone, clear buffers, return WAV bytes."""
        self._capturing = False
        blocks = self._blocks
        self._close_stream()  # also clears _blocks / _captured_samples
        if not blocks:
            return None

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(config.CHANNELS)
            wav.setsampwidth(2)  # int16
            wav.setframerate(config.SAMPLE_RATE)
            wav.writeframes(b"".join(blocks))
        return buffer.getvalue()

    def discard_recording(self) -> None:
        """Throw the take away and release the microphone (Esc / shortcut)."""
        self._capturing = False
        self._close_stream()

    # ------------------------------------------------------------------
    # Audio callback
    # ------------------------------------------------------------------
    # Called on sounddevice's audio thread -- emitting a Qt signal from here
    # is safe; Qt delivers it to the GUI thread on the next event loop tick.
    def _on_audio(self, indata, frames, time_info, status) -> None:
        if frames == 0:
            self.level_changed.emit(0.0)
            return

        samples = indata[:, 0]
        rms = float(np.sqrt(np.mean(np.square(samples))))

        # --- Keep the audio when a session is active (milestone 3) ---
        if self._capturing:
            pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
            self._blocks.append(pcm.tobytes())
            self._captured_samples += frames
            if self._captured_samples >= config.MAX_RECORDING_S * config.SAMPLE_RATE:
                # Auto-stop at the maximum length (PRD section 6).
                self._capturing = False
                self.max_length_reached.emit()

        # --- Tiny auto-gain: remember the loudest recent RMS so quiet mics
        # still produce full-range levels and loud mics don't pin at 1.0. ---
        self._peak = max(rms, self._peak * PEAK_DECAY, PEAK_FLOOR)

        # Silence gate -> exactly 0.0 -> the pill's bars are completely still.
        if rms < max(SILENCE_FLOOR_RMS, self._peak * SILENCE_RELATIVE):
            self.level_changed.emit(0.0)
            return

        # Normalize to the running peak. The 0.65 power keeps quiet speech
        # clearly visible instead of hugging the floor.
        level = min(1.0, (rms / self._peak) ** 0.65)
        self.level_changed.emit(level)


# ----------------------------------------------------------------------
# Helpers for finished takes (used before calling the Groq API)
# ----------------------------------------------------------------------
def wav_duration_seconds(wav_bytes: bytes) -> float:
    """Length of a mono 16-bit WAV in seconds."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


def is_usable_audio(wav_bytes: bytes | None) -> bool:
    """False for empty, very short, or essentially silent takes.

    PRD section 6: skip the transcription call for recordings shorter than
    about 0.3 seconds or that are just silence.
    """
    if not wav_bytes:
        return False
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
    if len(frames) < int(config.MIN_RECORDING_S * config.SAMPLE_RATE) * 2:
        return False  # too short (2 bytes per int16 sample)
    samples = np.frombuffer(frames, dtype=np.int16)
    peak = float(np.max(np.abs(samples))) / 32768.0
    return peak >= SILENCE_PEAK
