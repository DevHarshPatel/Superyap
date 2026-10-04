"""The floating pill -- the entire user interface of Superyap.

A frameless, always-on-top pill showing one of four states (idle / recording /
processing / error): a solid dark fill, a thin dark double outline, and a
soft shadow.

The pill has two sizes and animates between them smoothly:
- a small, thin, non-invasive pill while idle (a dim dot inside),
- the full-size pill while recording or processing.

In no-hands mode (Ctrl+Win pressed twice) two controls animate in:
- a circular cancel button (same height as the pill) that emerges from
  behind the pill's left edge; clicking it -- or pressing Esc -- discards
  the take with no API call,
- a red record button inside the pill's right side; clicking it -- or
  pressing Enter -- finishes the take so it gets transcribed.

Dragged close to the left or right screen edge, the whole pill flips 90
degrees and stands upright. Everything is drawn in coordinates anchored to
the pill's center and rotated about it, so the flip is just an animation.
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

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSettings, Qt, QTimer, Signal
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
COL_CANCEL_X = QColor(198, 198, 205)          # the X on the cancel button
COL_REC_PANEL = QColor(122, 22, 14)           # dark red record-button panel
COL_REC_ICON = QColor(244, 85, 60)            # bright red record icon

# How many waveform bars we draw while recording.
NUM_BARS = 7

# How many times per second the waveform bars swap between tall and short
# (even bars high while odd bars low, then the other way round).
BAR_SWAP_HZ = 1.0

# Subtle per-bar variety so the row doesn't look mechanical. It only scales
# how far each bar swings; the even/odd alternation stays exact.
BAR_VARIETY = (1.0, 0.88, 0.96, 0.85, 1.0, 0.9, 0.93)

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
    # The circular cancel button next to the pill was clicked.
    cancel_clicked = Signal()
    # The red record button inside the pill was clicked (no-hands mode).
    record_clicked = Signal()

    def __init__(self) -> None:
        super().__init__(None)

        self._state = PillState.IDLE
        self._prev_state: PillState | None = None
        self._state_started = time.monotonic()

        # --- Size animation: 0.0 = small idle pill, 1.0 = full-size pill ---
        self._size_t = 0.0
        self._size_target = 0.0

        # --- Control animations: 0.0 = hidden, 1.0 = fully shown. Both
        # --- controls are no-hands-only: the cancel button emerges from
        # --- behind the pill, the red record button pops into the pill. ---
        self._cancel_t = 0.0
        self._cancel_target = 0.0
        self._record_t = 0.0
        self._record_target = 0.0

        # --- Edge flip: 0.0 = flat, 1.0 = standing upright (rotated 90
        # --- degrees) after being dragged near a left/right screen edge. ---
        self._rot_t = 0.0
        self._rot_target = 0.0
        self._vertical = False     # orientation decision (with hysteresis)

        # --- Audio animation (all eased toward targets every frame) ---
        self._level_target = 0.0    # newest raw audio level (0..1)
        self._level_smooth = 0.0    # smoothed level (fast attack, slow release)
        self._bar_levels = [0.0] * NUM_BARS
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
        self._press_pos: QPoint | None = None  # where the press started

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

    def _cancel_extent(self) -> float:
        """How far the window sticks out left of the pill for the cancel button."""
        _, pill_h = self._pill_size()
        return self._cancel_t * pill_h * (
            config.CANCEL_BUTTON_GAP + config.CANCEL_BUTTON_SCALE
        )

    def _rotation_deg(self) -> float:
        """Current rotation: 0 = flat, 90 = standing upright at a screen edge."""
        return 90.0 * self._rot_t

    def _pill_rect(self) -> QRectF:
        """The drawn pill rect in content coordinates (origin = pill center).

        Everything (controls, waveform) is laid out in these coordinates and
        painted through a rotation about the origin, so flipping the pill
        upright near a screen edge needs no per-shape rewriting.
        """
        pill_w, pill_h = self._pill_size()
        return QRectF(-pill_w / 2, -pill_h / 2, pill_w, pill_h)

    def _layout_extents(self) -> tuple[float, float, float, float]:
        """Padded content half-extents around the pill center, rotated.

        Returns (left, right, up, down): the distance from the pill center to
        each edge of the window, in widget-local coordinates.
        """
        pill_w, pill_h = self._pill_size()
        extra = self._cancel_extent()  # the cancel button sticks out left
        pad = config.PILL_SHADOW_MARGIN
        x_neg = pill_w / 2 + extra + pad
        x_pos = pill_w / 2 + pad
        y = max(pill_h, pill_h * config.CANCEL_BUTTON_SCALE) / 2 + pad
        ang = math.radians(self._rotation_deg())
        c, s = abs(math.cos(ang)), abs(math.sin(ang))
        return (
            x_neg * c + y * s,   # left
            x_pos * c + y * s,   # right
            x_neg * s + y * c,   # up
            x_pos * s + y * c,   # down
        )

    def _paint_origin(self) -> QPointF:
        """Widget-local position of the pill center (= the rotation origin)."""
        left, _, up, _ = self._layout_extents()
        return QPointF(left, up)

    def _to_content(self, point: QPointF) -> QPointF:
        """Map a widget-local point into content coordinates (un-rotate)."""
        origin = self._paint_origin()
        dx, dy = point.x() - origin.x(), point.y() - origin.y()
        ang = -math.radians(self._rotation_deg())
        c, s = math.cos(ang), math.sin(ang)
        return QPointF(dx * c - dy * s, dx * s + dy * c)

    def _cancel_geometry(self) -> tuple[QRectF, float]:
        """Cancel circle rect (widget-local) plus its current opacity.

        The circle is the same size as the pill is tall. It starts tucked
        behind the pill's left edge and slides out to the left as `_cancel_t`
        grows. The rect is in content coordinates (see _pill_rect).
        """
        pill_rect = self._pill_rect()
        pill_h = pill_rect.height()
        radius_full = pill_h * config.CANCEL_BUTTON_SCALE / 2
        gap = pill_h * config.CANCEL_BUTTON_GAP
        p = self._cancel_t
        radius = radius_full * (0.55 + 0.45 * p)
        center_x = (
            pill_rect.left()
            + (1.0 - p) * radius_full * 0.25
            - p * (gap + radius_full)
        )
        rect = QRectF(
            center_x - radius,
            pill_rect.center().y() - radius,
            2 * radius,
            2 * radius,
        )
        return rect, min(1.0, p * 1.6)

    def _record_panel_rect(self) -> QRectF:
        """The record button's full panel rect (content coordinates)."""
        pill_rect = self._pill_rect()
        pill_w, pill_h = pill_rect.width(), pill_rect.height()
        inset = pill_h * config.RECORD_BUTTON_INSET
        panel_h = pill_h * config.RECORD_BUTTON_HEIGHT
        panel_w = pill_w * config.RECORD_BUTTON_WIDTH
        return QRectF(
            pill_rect.right() - inset - panel_w,
            pill_rect.center().y() - panel_h / 2,
            panel_w,
            panel_h,
        )

    def _apply_geometry(self) -> None:
        """Resize the window to hug the (rotated) drawing; the pill's center
        stays anchored at self._center."""
        left, right, up, down = self._layout_extents()
        win_w = int(round(left + right))
        win_h = int(round(up + down))
        pos = (
            int(round(self._center.x() - left)),
            int(round(self._center.y() - up)),
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
        # Clamp on the pill body only (plus a small pad): the controls may
        # transiently lean past the edge while the pill flips upright, and
        # the flip trigger must stay reachable before the clamp bites.
        pill_w, pill_h = self._pill_size()
        pad = config.PILL_EDGE_PAD
        ang = math.radians(self._rotation_deg())
        c, s = abs(math.cos(ang)), abs(math.sin(ang))
        half_x = (pill_w / 2 + pad) * c + (pill_h / 2 + pad) * s
        half_y = (pill_w / 2 + pad) * s + (pill_h / 2 + pad) * c

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

        cx = min(
            max(self._center.x(), avail.left() + half_x),
            avail.right() - half_x,
        )
        cy = min(max(self._center.y(), avail.top() + half_y), bottom_limit - half_y)
        self._center = QPoint(int(cx), int(cy))

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Start dragging the pill (left button only)."""
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Move the pill while the left button is held."""
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._sync_center_from_window()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """End the drag; a click on the cancel / record button fires it."""
        if event.button() == Qt.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            clicked = (
                self._press_pos is not None
                and (event.globalPosition().toPoint() - self._press_pos).manhattanLength() < 5
            )
            self._press_pos = None
            if clicked and self._click_controls(event.position()):
                super().mouseReleaseEvent(event)
                return
            self._sync_center_from_window()
            # A drop over the taskbar would hide the pill behind it forever.
            self._clamp_to_safe_area()
            self._apply_geometry()
            self._settings.setValue("pill/center", [self._center.x(), self._center.y()])
            self.position_changed.emit(self._center)
        super().mouseReleaseEvent(event)

    def _sync_center_from_window(self) -> None:
        """Recompute the pill center after a drag moved the window.

        The pill's center is NOT the window's center (extra space on the
        left for the cancel button, and the rotated bounding box), so the
        center must be derived from the paint origin.
        """
        top_left = self.frameGeometry().topLeft()
        origin = self._paint_origin()
        self._center = QPoint(
            top_left.x() + int(round(origin.x())),
            top_left.y() + int(round(origin.y())),
        )

    def _click_controls(self, point) -> bool:
        """Fire cancel_clicked / record_clicked if the click hit a control."""
        p = self._to_content(point)
        if self._cancel_t > 0.3 and self._cancel_geometry()[0].contains(p):
            self.cancel_clicked.emit()
            return True
        if self._record_t > 0.5 and self._record_panel_rect().contains(p):
            self.record_clicked.emit()
            return True
        return False

    def is_over_ui(self) -> bool:
        """Is the cursor over the cancel or record button right now?

        Called from the global mouse hook (hotkey.py) on a hook thread so a
        click on the pill's own controls is not mistaken for "other input".
        It must never call into live Qt geometry, so it only does arithmetic
        on cached values -- safe to call from any thread.
        """
        pos = _win32_cursor_pos()
        if pos is None:
            return False
        point = self._to_content(
            QPointF(pos[0] - self._last_win_pos[0], pos[1] - self._last_win_pos[1])
        )
        if self._cancel_t > 0.3 and self._cancel_geometry()[0].contains(point):
            return True
        return self._record_t > 0.5 and self._record_panel_rect().contains(point)

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

        # Both pill controls are no-hands-only: reset here and re-shown by
        # show_record_button() when no-hands mode starts.
        self._cancel_target = 0.0
        self._record_target = 0.0

        if state != PillState.RECORDING:
            self._level_target = 0.0

    def show_record_button(self) -> None:
        """No-hands mode: pop in the red record button and the cancel button."""
        if self._state == PillState.RECORDING:
            self._record_target = 1.0
            self._cancel_target = 1.0

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

        # Ease the cancel / record buttons toward their targets. The cancel
        # button moves at nearly the pill's pace, so it reads as "emerging
        # from behind the pill" while the pill grows.
        self._cancel_t += (self._cancel_target - self._cancel_t) * 0.15
        if abs(self._cancel_target - self._cancel_t) < 0.002:
            self._cancel_t = self._cancel_target
        self._record_t += (self._record_target - self._record_t) * 0.18
        if abs(self._record_target - self._record_t) < 0.002:
            self._record_t = self._record_target

        # Near the left/right screen edge the pill flips upright; the
        # rotation eases so it reads as a smooth flip, not a snap.
        self._update_flip_target()
        self._rot_t += (self._rot_target - self._rot_t) * 0.18
        if abs(self._rot_target - self._rot_t) < 0.002:
            self._rot_t = self._rot_target

        # Smooth the level: quick attack (peaks show up right away), slower
        # release (troughs follow smoothly instead of snapping down).
        if self._level_target > self._level_smooth:
            self._level_smooth += (self._level_target - self._level_smooth) * 0.55
        else:
            self._level_smooth += (self._level_target - self._level_smooth) * 0.18
        if self._level_smooth < 0.005:
            self._level_smooth = 0.0  # fully silent -> fully static

        # Ease each bar toward its target. Even and odd bars swing in
        # opposite phase and swap every half period, so the waveform dances
        # left/right; every bar's height is still driven by the loudness.
        for i in range(NUM_BARS):
            target = self._level_smooth * self._bar_scale(i, t)
            self._bar_levels[i] += (target - self._bar_levels[i]) * 0.4
            if self._bar_levels[i] < 0.004:
                self._bar_levels[i] = 0.0

        self.update()  # schedule a repaint

    def _update_flip_target(self) -> None:
        """Decide flat vs upright, with hysteresis so it never flickers.

        The pill flips upright when it (with its controls) comes within
        EDGE_FLIP_GAP pixels of the left or right edge of its screen, and
        flips back only once it is EDGE_FLIP_GAP + EDGE_FLIP_HYSTERESIS away
        from both edges again.
        """
        screen = QApplication.screenAt(self._center) or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        pill_w, _ = self._pill_size()
        pad = config.PILL_SHADOW_MARGIN
        # Trigger distances use the horizontal layout (what the pill would
        # need if it stayed flat), including the cancel button's slot.
        limit_left = pill_w / 2 + self._cancel_extent() + pad + config.EDGE_FLIP_GAP
        limit_right = pill_w / 2 + pad + config.EDGE_FLIP_GAP
        dist_left = self._center.x() - avail.left()
        dist_right = avail.right() - self._center.x()
        if self._vertical:
            if (
                dist_left > limit_left + config.EDGE_FLIP_HYSTERESIS
                and dist_right > limit_right + config.EDGE_FLIP_HYSTERESIS
            ):
                self._vertical = False
        elif dist_left < limit_left or dist_right < limit_right:
            self._vertical = True
        self._rot_target = 1.0 if self._vertical else 0.0

    def _bar_scale(self, i: int, t: float) -> float:
        """0..1 multiplier for bar i: tall/short alternating over time.

        Even bars are high while odd bars are low and vice versa -- exactly
        opposite phase -- and the groups swap BAR_SWAP_HZ times per second.
        The result multiplies the live loudness, so quiet speech stays
        subtle and loud speech fills the pill.
        """
        phase = t * 2.0 * math.pi * BAR_SWAP_HZ + (i % 2) * math.pi
        swing = 0.5 + 0.5 * math.sin(phase)
        return 0.30 + 0.70 * swing * BAR_VARIETY[i]

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

        # Everything is drawn in content coordinates (origin = pill center)
        # through a rotation about that center, so the pill can stand upright
        # near a screen edge. The window leaves room for the shadow and for
        # the cancel button left of the pill (see _layout_extents).
        origin = self._paint_origin()
        painter.translate(origin.x(), origin.y())
        painter.rotate(self._rotation_deg())
        pill_rect = self._pill_rect()
        radius = pill_rect.height() / 2

        # The cancel button goes in first so the pill's body can cover it
        # while it is still tucked behind the pill.
        self._paint_cancel_button(painter)
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
            self._paint_record_button(painter, pill_rect)
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

    def _paint_cancel_button(self, painter: QPainter) -> None:
        """The circular cancel button (drawn behind the pill body)."""
        rect, alpha = self._cancel_geometry()
        if alpha <= 0.01:
            return
        painter.setOpacity(alpha)
        self._paint_shadow(painter, rect, rect.height() / 2)

        painter.setPen(Qt.NoPen)
        painter.setBrush(COL_BACKGROUND)
        painter.drawEllipse(rect)
        # Same double outline as the pill, for a consistent look.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(COL_BORDER_INNER, 1))
        painter.drawEllipse(rect.adjusted(1.5, 1.5, -1.5, -1.5))
        painter.setPen(QPen(COL_BORDER_OUTER, 1))
        painter.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))

        # The X: round-capped but deliberately small and light.
        c = rect.center()
        span = rect.height() * config.CANCEL_CROSS_SPAN
        pen = QPen(COL_CANCEL_X, rect.height() * config.CANCEL_CROSS_WIDTH)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(c.x() - span, c.y() - span), QPointF(c.x() + span, c.y() + span)
        )
        painter.drawLine(
            QPointF(c.x() - span, c.y() + span), QPointF(c.x() + span, c.y() - span)
        )
        painter.setOpacity(1.0)

    def _paint_record_button(self, painter: QPainter, pill_rect: QRectF) -> None:
        """The red record button inside the pill's right side (no-hands mode).

        The dark red panel grows open from the pill's right edge, then the
        bright red icon pops into its middle.
        """
        p = self._record_t
        if p <= 0.01:
            return
        panel = self._record_panel_rect()
        visible = QRectF(
            panel.right() - panel.width() * p,
            panel.top(),
            panel.width() * p,
            panel.height(),
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(COL_REC_PANEL)
        painter.drawRoundedRect(visible, panel.height() * 0.45, panel.height() * 0.45)

        # The icon pops in (scale + fade) once the panel has mostly opened.
        icon_t = max(0.0, min(1.0, (p - 0.55) / 0.45))
        if icon_t > 0.0:
            size = panel.height() * 0.52 * (0.5 + 0.5 * icon_t)
            color = QColor(COL_REC_ICON)
            color.setAlpha(int(255 * icon_t))
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(
                    panel.center().x() - size / 2,
                    panel.center().y() - size / 2,
                    size,
                    size,
                ),
                size * 0.3,
                size * 0.3,
            )

    def _paint_waveform(self, painter: QPainter, pill_rect: QRectF) -> None:
        """Waveform driven by the real microphone level.

        Heights follow the smoothed RMS (silence -> a flat, still row) while
        the bars alternate tall and short, swapping sides over time.
        """
        bar_w, gap = 3.0, 3.0
        total_w = NUM_BARS * bar_w + (NUM_BARS - 1) * gap
        # When the red record button is in the pill, the waveform sits in
        # whatever space is left of it.
        inset = pill_rect.height() * config.RECORD_BUTTON_INSET
        panel_w = pill_rect.width() * config.RECORD_BUTTON_WIDTH
        reserved = self._record_t * (panel_w + 2.0 * inset)
        area = pill_rect.adjusted(0, 0, -reserved, 0)
        x0 = area.center().x() - total_w / 2
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
        state = sequence[self._demo_step % len(sequence)]
        self.set_state(state)
        if state == PillState.RECORDING:
            self.show_record_button()  # demo the no-hands layout too
        self._demo_step += 1


def _win32_cursor_pos() -> tuple[int, int] | None:
    """The cursor position in global screen coordinates (None off Windows)."""
    if sys.platform != "win32":
        return None

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return None
    return point.x, point.y


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
