"""The floating pill -- the entire user interface of Superyap.

A frameless, always-on-top pill showing one of four states (idle / recording /
processing / error): a solid dark fill, a thin dark double outline, and a
soft shadow.

The pill has two sizes and animates between them smoothly:
- a small, thin, non-invasive pill while idle (a dim dot inside),
- the full-size pill while recording or processing.
The pill's center stays anchored while its size animates, and the content of
one state crossfades into the next, so every transition (especially
processing -> idle) feels cohesive instead of abrupt.

Two hard rules for this widget:
1. It must NEVER steal keyboard focus from the active application.
   We do this with Qt.Tool + Qt.WindowDoesNotAcceptFocus + WA_ShowWithoutActivating
   *and* with the Win32 WS_EX_NOACTIVATE extended style.
2. It must not appear in the taskbar or Alt+Tab (Qt.Tool + WS_EX_TOOLWINDOW).
"""

from __future__ import annotations

import ctypes
import math
import random
import sys
import time
from enum import Enum

from PySide6.QtCore import QPoint, QRect, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

import config


class PillState(Enum):
    """The four visual states of the pill."""

    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    ERROR = "error"


# --- Colors (kept in one place so the pill is easy to restyle) -------------
COL_BACKGROUND = QColor(30, 30, 38)           # solid dark fill
COL_BORDER_INNER = QColor(37, 37, 37)         # thin #252525 inner outline
COL_BORDER_OUTER = QColor(6, 6, 6)            # thin #060606 outer outline
COL_IDLE = QColor(150, 155, 175, 170)         # dim idle dot
COL_RECORDING = QColor(240, 240, 245)         # white waveform
COL_PROCESSING = QColor(150, 165, 200, 230)   # soft dots
COL_ERROR = QColor(235, 70, 80, 255)          # red flash

# How many waveform bars we draw while recording.
NUM_BARS = 7

# Animation loop period in milliseconds (~60 fps).
FRAME_MS = 16

# Error flash duration in seconds.
ERROR_FLASH_S = 0.7

# How long the content of one state takes to fade into the next.
CONTENT_FADE_S = 0.35

# Easing speed for the size animation (higher = snappier). Expanding feels a
# little faster than shrinking, which reads as "responsive but gentle".
SIZE_EASE_EXPAND = 0.16
SIZE_EASE_SHRINK = 0.11

# How often to re-assert "always on top" (see _reassert_topmost). Windows
# lets the z-order of topmost windows shuffle -- clicking the taskbar raises
# it above us -- so we periodically ask to be at the top again.
TOPMOST_REFRESH_S = 2.0


class PillWindow(QWidget):
    """The floating pill widget. See module docstring for the design rules."""

    # Emitted whenever the pill is dragged to a new spot (future use).
    position_changed = Signal(QPoint)

    def __init__(self) -> None:
        super().__init__(None)

        self._state = PillState.IDLE
        self._prev_state: PillState | None = None
        self._state_started = time.monotonic()

        # --- Size animation: 0.0 = small idle pill, 1.0 = full-size pill ---
        self._size_t = 0.0
        self._size_target = 0.0

        # --- Audio animation (all eased toward targets every frame) ---
        self._level_target = 0.0    # newest raw audio level (0..1)
        self._level_smooth = 0.0    # smoothed level (fast attack, slow release)
        self._bar_levels = [0.0] * NUM_BARS
        # Bell-shaped weights: center bars swing highest, edge bars stay low.
        self._bar_weights = [
            math.sin(math.pi * (i + 0.5) / NUM_BARS) for i in range(NUM_BARS)
        ]
        self._error_started: float | None = None  # time.monotonic() of flash
        self._t0 = time.monotonic()
        self._last_topmost = 0.0  # last time we re-asserted always-on-top
        self._demo_fake_audio = False  # True only while --demo is running

        # --- Window setup: frameless, translucent, always on top, no focus ---
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool                      # no taskbar button
            | Qt.WindowDoesNotAcceptFocus  # never take focus from other apps
        )
        self.setAttribute(Qt.WA_TranslucentBackground)  # rounded, see-through
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # show() won't focus us

        # The window hugs the drawn pill (+ shadow padding) and is resized as
        # the pill animates; self._center keeps the pill anchored in place.
        self._center = QPoint(0, 0)          # global coords of the pill center
        self._last_win_size = (0, 0)
        self._last_win_pos = (0, 0)
        self._apply_geometry()

        # --- Animation timer (~60 fps) ---
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(FRAME_MS)

        # --- Dragging ---
        self._drag_offset: QPoint | None = None

        self._settings = QSettings(config.SETTINGS_ORG, config.SETTINGS_APP)
        self._place_initial()

    # ------------------------------------------------------------------
    # Geometry: the pill keeps its center while its size animates
    # ------------------------------------------------------------------
    def _pill_size(self) -> tuple[float, float]:
        """Current drawn pill size, interpolated by the size animation."""
        w = config.PILL_SMALL_WIDTH + (
            config.PILL_WIDTH - config.PILL_SMALL_WIDTH
        ) * self._size_t
        h = config.PILL_SMALL_HEIGHT + (
            config.PILL_HEIGHT - config.PILL_SMALL_HEIGHT
        ) * self._size_t
        return w, h

    def _apply_geometry(self) -> None:
        """Resize the window to hug the pill and keep the pill centered."""
        w, h = self._pill_size()
        win_w = int(round(w + 2 * config.PILL_SHADOW_MARGIN))
        win_h = int(round(h + 2 * config.PILL_SHADOW_MARGIN))
        pos = (
            int(round(self._center.x() - win_w / 2)),
            int(round(self._center.y() - win_h / 2)),
        )
        if (win_w, win_h) != self._last_win_size or pos != self._last_win_pos:
            self._last_win_size = (win_w, win_h)
            self._last_win_pos = pos
            self.resize(win_w, win_h)
            self.move(pos[0], pos[1])

    def _place_initial(self) -> None:
        """Put the pill where the user left it, or at the default spot."""
        win_w, win_h = self._last_win_size
        placed = False

        if config.PILL_DEFAULT_POS is not None:
            x, y = config.PILL_DEFAULT_POS
            self._center = QPoint(int(x + win_w / 2), int(y + win_h / 2))
            placed = True
        else:
            # Remembered position from a previous run? QSettings stores values
            # in the Windows registry, where they come back as *strings*, so
            # convert. "pill/center" is the current format; "pill/pos" is
            # migrated from older builds (fixed-size window top-left).
            saved = self._settings.value("pill/center")
            try:
                self._center = QPoint(int(saved[0]), int(saved[1]))  # type: ignore[index]
                placed = True
            except (TypeError, ValueError, IndexError):
                saved = self._settings.value("pill/pos")
                try:
                    self._center = QPoint(
                        int(saved[0]) + (config.PILL_WIDTH + 2 * config.PILL_SHADOW_MARGIN) // 2,
                        int(saved[1]) + (config.PILL_HEIGHT + 2 * config.PILL_SHADOW_MARGIN) // 2,
                    )
                    placed = True
                except (TypeError, ValueError, IndexError):
                    pass

        if not placed:
            # Default: bottom center of the primary screen, just above the
            # taskbar. availableGeometry() already excludes the taskbar.
            screen = QApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                self._center = QPoint(
                    geo.center().x(),
                    geo.bottom() - win_h // 2 - config.PILL_BOTTOM_GAP,
                )

        # Never start out over the taskbar or off screen (a bad saved value
        # from an old drag should not make the pill vanish).
        self._clamp_to_safe_area()
        self._apply_geometry()

    def _clamp_to_safe_area(self) -> None:
        """Keep the pill fully on screen and out of the taskbar.

        The pill sits near the bottom of the screen, exactly where the
        taskbar lives; if a drag (or an old saved position) puts it over the
        taskbar it disappears behind it. This nudges it back up.
        """
        screen = QApplication.screenAt(self._center) or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        win_w, win_h = self._last_win_size
        half_w, half_h = win_w // 2, win_h // 2

        # Lowest allowed window bottom: above the taskbar band if we can find
        # it (also covers an auto-hidden taskbar, which availableGeometry()
        # does not reserve space for), else above the available-area bottom.
        bottom_limit = avail.bottom() - config.PILL_BOTTOM_GAP
        if screen is QApplication.primaryScreen():
            taskbar = _win32_taskbar_rect()
            if taskbar is not None:
                dpr = screen.devicePixelRatio() or 1.0
                taskbar_top = int(taskbar.top() / dpr)  # physical -> logical
                bottom_limit = min(bottom_limit, taskbar_top - config.PILL_BOTTOM_GAP)

        cx = min(max(self._center.x(), avail.left() + half_w), avail.right() - half_w)
        cy = min(max(self._center.y(), avail.top() + half_h), bottom_limit - half_h)
        self._center = QPoint(cx, cy)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Start dragging the pill (left button only)."""
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Move the pill while the left button is held."""
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._center = self.frameGeometry().center()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """End the drag and remember the position for the next run."""
        if event.button() == Qt.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self._center = self.frameGeometry().center()
            # A drop over the taskbar would hide the pill behind it forever.
            self._clamp_to_safe_area()
            self._apply_geometry()
            self._settings.setValue("pill/center", [self._center.x(), self._center.y()])
            self.position_changed.emit(self._center)
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    # State control (the hotkey/recorder code call these)
    # ------------------------------------------------------------------
    def set_state(self, state: PillState) -> None:
        """Switch the pill to a new visual state (with a smooth transition)."""
        if state == self._state:
            return
        self._prev_state = self._state
        self._state = state
        self._state_started = time.monotonic()

        if state == PillState.ERROR and self._prev_state != PillState.ERROR:
            self._error_started = time.monotonic()

        # Size target: full while recording/processing, small while idle.
        # During the error flash the pill holds its current size.
        if state in (PillState.RECORDING, PillState.PROCESSING):
            self._size_target = 1.0
        elif state == PillState.IDLE:
            self._size_target = 0.0
        else:  # ERROR: freeze at the current size
            self._size_target = self._size_t

        if state != PillState.RECORDING:
            self._level_target = 0.0

    def set_audio_level(self, level: float) -> None:
        """Feed the waveform with the live microphone level (0.0 - 1.0).

        Called by main.py with real RMS values from recorder.MicrophoneMonitor
        (and by the fake generator in --demo mode).
        """
        level = max(0.0, min(1.0, level))
        # Silence gate: nothing to show -> exactly 0 -> the bars stay dead still.
        self._level_target = 0.0 if level < 0.02 else level

    # ------------------------------------------------------------------
    # Animation
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        """Advance all animation values one frame, then repaint."""
        t = time.monotonic() - self._t0

        if self._state == PillState.RECORDING:
            if self._demo_fake_audio:
                self._fake_audio_tick(t)
        elif self._state == PillState.ERROR:
            # Red flash ends automatically, then back to idle.
            if (
                self._error_started is not None
                and time.monotonic() - self._error_started > ERROR_FLASH_S
            ):
                self.set_state(PillState.IDLE)

        # Keep the pill above other topmost windows (e.g. the taskbar).
        if t - self._last_topmost > TOPMOST_REFRESH_S:
            self._last_topmost = t
            self._reassert_topmost()

        # Ease the pill size toward its target (smooth, no jumps).
        rate = SIZE_EASE_EXPAND if self._size_target > self._size_t else SIZE_EASE_SHRINK
        self._size_t += (self._size_target - self._size_t) * rate
        if abs(self._size_target - self._size_t) < 0.002:
            self._size_t = self._size_target
        self._apply_geometry()

        # Smooth the level: quick attack (peaks show up right away), slower
        # release (troughs follow smoothly instead of snapping down).
        if self._level_target > self._level_smooth:
            self._level_smooth += (self._level_target - self._level_smooth) * 0.55
        else:
            self._level_smooth += (self._level_target - self._level_smooth) * 0.18
        if self._level_smooth < 0.005:
            self._level_smooth = 0.0  # fully silent -> fully static

        # Ease each bar toward its share of the current level. The center bars
        # react slightly faster than the outer ones, giving a gentle ripple
        # when speech starts and stops.
        center = (NUM_BARS - 1) / 2
        for i in range(NUM_BARS):
            target = self._level_smooth * self._bar_weights[i]
            bar_rate = 0.45 - 0.28 * abs(i - center) / max(center, 1)
            self._bar_levels[i] += (target - self._bar_levels[i]) * bar_rate
            if self._bar_levels[i] < 0.004:
                self._bar_levels[i] = 0.0

        self.update()  # schedule a repaint

    def _fake_audio_tick(self, t: float) -> None:
        """--demo only: invent speech-like levels (talk bursts + pauses).

        Real recording gets its levels from recorder.MicrophoneMonitor via
        set_audio_level(); this is just so the demo shows something sensible
        on a machine without a microphone.
        """
        cycle = t % 3.6
        if cycle < 2.2:
            # "Talking": a syllable-like envelope with some randomness.
            envelope = 0.5 + 0.5 * math.sin(t * 1.7) * math.sin(t * 0.6 + 1.0)
            fade_in = min(1.0, cycle * 8.0)  # ramp up at the start of a burst
            self._level_target = envelope * fade_in * (0.45 + 0.55 * random.random())
        else:
            # Pause between sentences: silence -> static bars.
            self._level_target = 0.0

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        """Draw shadow, body, double outline, and the crossfading content."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # The drawn pill is centered in the widget; the widget is larger to
        # leave room for the soft shadow around it.
        pill_w, pill_h = self._pill_size()
        cx = self.width() / 2
        cy = self.height() / 2
        pill_rect = QRectF(cx - pill_w / 2, cy - pill_h / 2, pill_w, pill_h)
        radius = pill_h / 2

        self._paint_shadow(painter, pill_rect, radius)

        # Solid dark fill (red-tinted during the error flash).
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._current_background())
        painter.drawRoundedRect(pill_rect, radius, radius)

        # Thin double outline: inner #252525, outer #060606 around it.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(COL_BORDER_INNER, 1))
        painter.drawRoundedRect(pill_rect.adjusted(1.5, 1.5, -1.5, -1.5), radius - 1, radius - 1)
        painter.setPen(QPen(COL_BORDER_OUTER, 1))
        painter.drawRoundedRect(pill_rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        # Crossfade the content of the previous state into the new one so
        # transitions (especially processing -> idle) feel cohesive.
        fade = min(1.0, (time.monotonic() - self._state_started) / CONTENT_FADE_S)
        if self._prev_state is not None and self._prev_state != self._state and fade < 1.0:
            self._paint_content(painter, pill_rect, self._prev_state, 1.0 - fade)
        self._paint_content(painter, pill_rect, self._state, fade)

        painter.end()

    def _paint_content(
        self, painter: QPainter, pill_rect: QRectF, state: PillState, alpha: float
    ) -> None:
        """Draw one state's content at the given opacity (for crossfading)."""
        if alpha <= 0.01:
            return
        painter.setOpacity(alpha)
        if state == PillState.RECORDING:
            self._paint_waveform(painter, pill_rect)
        elif state == PillState.PROCESSING:
            self._paint_dots(painter, pill_rect)
        elif state == PillState.IDLE:
            self._paint_idle_dot(painter, pill_rect)
        # ERROR: content is just the red background flash (drawn separately).
        painter.setOpacity(1.0)

    def _current_background(self) -> QColor:
        """Blend the dark fill toward red while the error flash runs."""
        if self._state != PillState.ERROR or self._error_started is None:
            return COL_BACKGROUND
        progress = min(1.0, (time.monotonic() - self._error_started) / ERROR_FLASH_S)
        blend = (1.0 - progress) ** 2  # quick in, slow out
        out = QColor(COL_BACKGROUND)
        return QColor(
            int(out.red() + (COL_ERROR.red() - out.red()) * blend),
            int(out.green() + (COL_ERROR.green() - out.green()) * blend),
            int(out.blue() + (COL_ERROR.blue() - out.blue()) * blend),
            int(out.alpha() + (COL_ERROR.alpha() - out.alpha()) * blend),
        )

    def _paint_shadow(self, painter: QPainter, pill_rect: QRectF, radius: float) -> None:
        """Draw a soft shadow as a few expanding, very transparent layers."""
        painter.setPen(Qt.NoPen)
        for i in range(4, 0, -1):
            spread = i * 1.5
            alpha = 16 - i * 3  # outermost layer faintest
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(
                pill_rect.adjusted(-spread, -spread + 1, spread, spread + 1),
                radius + spread,
                radius + spread,
            )

    def _paint_idle_dot(self, painter: QPainter, pill_rect: QRectF) -> None:
        """A dim, completely static dot -- calm and non-invasive."""
        radius = max(1.5, pill_rect.height() * 0.16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(COL_IDLE)
        painter.drawEllipse(pill_rect.center(), radius, radius)

    def _paint_waveform(self, painter: QPainter, pill_rect: QRectF) -> None:
        """Waveform driven by the real microphone level.

        Heights follow the smoothed RMS: silence -> a flat, still row of bars;
        speech -> a peak/trough shape that mirrors what the user is saying.
        """
        bar_w, gap = 3.0, 3.0
        total_w = NUM_BARS * bar_w + (NUM_BARS - 1) * gap
        x0 = pill_rect.center().x() - total_w / 2
        y_mid = pill_rect.center().y()
        max_h = pill_rect.height() * 0.52
        min_h = 2.5  # static floor while silent

        painter.setPen(Qt.NoPen)
        for i in range(NUM_BARS):
            h = min_h + self._bar_levels[i] * max_h
            painter.setBrush(COL_RECORDING)
            painter.drawRoundedRect(
                QRectF(x0 + i * (bar_w + gap), y_mid - h / 2, bar_w, h), bar_w / 2, bar_w / 2
            )

    def _paint_dots(self, painter: QPainter, pill_rect: QRectF) -> None:
        """Three dots pulsing in sequence (processing shimmer)."""
        t = time.monotonic() - self._t0
        y_mid = pill_rect.center().y()
        x_mid = pill_rect.center().x()
        spacing = 9.0

        painter.setPen(Qt.NoPen)
        for i in range(3):
            # Each dot pulses 0.9 radians after the previous one.
            pulse = 0.5 + 0.5 * math.sin(t * 4.0 - i * 0.9)
            radius = 1.8 + 1.1 * pulse
            color = QColor(COL_PROCESSING)
            color.setAlpha(int(90 + 165 * pulse))
            painter.setBrush(color)
            painter.drawEllipse(
                QRectF(
                    x_mid + (i - 1) * spacing - radius,
                    y_mid - radius,
                    radius * 2,
                    radius * 2,
                )
            )

    # ------------------------------------------------------------------
    # Win32: never steal focus, never appear in the taskbar / Alt+Tab
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        """Apply the Win32 extended window styles the first time we show."""
        super().showEvent(event)
        _apply_win32_no_activate(self)
        self._reassert_topmost()

    def _reassert_topmost(self) -> None:
        """Ask Windows to keep the pill at the top of the z-order.

        "Always on top" is not a promise Windows keeps forever: clicking the
        taskbar raises it above us, and some apps re-assert their own topmost
        state when they get focus. Since the pill lives at the bottom of the
        screen, losing the z-order makes it look like it vanished behind the
        taskbar. Re-asking is cheap and never activates the window.
        """
        if sys.platform != "win32" or not self.isVisible():
            return
        HWND_TOPMOST = -1
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
        ctypes.windll.user32.SetWindowPos(
            int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    # ------------------------------------------------------------------
    # Demo helpers (only used by `python main.py --demo`)
    # ------------------------------------------------------------------
    def start_demo(self) -> None:
        """Cycle through the four states with fake speech-like audio levels."""
        self._demo_fake_audio = True
        self._demo_step = 0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._demo_next)
        self._demo_next()
        self._demo_timer.start(2200)

    def _demo_next(self) -> None:
        sequence = [PillState.IDLE, PillState.RECORDING, PillState.PROCESSING, PillState.ERROR]
        self.set_state(sequence[self._demo_step % len(sequence)])
        self._demo_step += 1


def _win32_taskbar_rect() -> QRect | None:
    """The taskbar's dock rectangle in physical pixels (None if not found).

    Works even for an auto-hidden taskbar, which reserves no space in the
    screen's available geometry but can still slide over the pill.
    """
    if sys.platform != "win32":
        return None

    class APPBARDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("hWnd", ctypes.c_void_p),
            ("uCallbackMessage", ctypes.c_uint),
            ("uEdge", ctypes.c_uint),
            ("rc", ctypes.c_long * 4),  # left, top, right, bottom
            ("lParam", ctypes.c_ssize_t),
        ]

    ABM_GETTASKBARPOS = 0x7
    data = APPBARDATA()
    data.cbSize = ctypes.sizeof(APPBARDATA)
    try:
        shell32 = ctypes.windll.shell32
        sh_app_bar_message = shell32.SHAppBarMessage
        sh_app_bar_message.argtypes = [ctypes.c_uint, ctypes.POINTER(APPBARDATA)]
        sh_app_bar_message.restype = ctypes.c_ssize_t  # UINT_PTR
        if not sh_app_bar_message(ABM_GETTASKBARPOS, ctypes.byref(data)):
            return None
    except Exception:
        return None
    left, top, right, bottom = data.rc
    return QRect(left, top, right - left, bottom - top)


def _apply_win32_no_activate(widget: QWidget) -> None:
    """Add WS_EX_NOACTIVATE (+ WS_EX_TOOLWINDOW) to the window's ex-style.

    Qt flags alone are usually enough, but Windows can still activate a
    window in some situations. WS_EX_NOACTIVATE makes it a hard guarantee
    that the pill can never take keyboard focus.
    """
    if sys.platform != "win32":
        return

    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080      # keep out of the taskbar and Alt+Tab
    WS_EX_NOACTIVATE = 0x08000000      # never steal focus

    user32 = ctypes.windll.user32
    hwnd = int(widget.winId())
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
