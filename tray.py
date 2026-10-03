"""System tray icon -- the only extra UI besides the pill (PRD section 5).

Right-clicking the tray icon shows exactly two items:
- "Start with Windows" (a checkable toggle), and
- "Quit".

"Start with Windows" writes a Run entry under
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run so Windows launches
Superyap when you log in. When running from source the entry points at
pythonw.exe + main.py (pythonw = no console window pops up); after PyInstaller
packaging (milestone 7) it points at the .exe itself.
"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

log = logging.getLogger("superyap")

# The Windows "run at login" registry key and our value name inside it.
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Superyap"


# ----------------------------------------------------------------------
# "Start with Windows" -- a single value in the user's Run key
# ----------------------------------------------------------------------
def is_startup_enabled() -> bool:
    """True if Superyap is currently set to start when the user logs in."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> None:
    """Turn "start with Windows" on or off (raises OSError on failure)."""
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enabled:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass  # already off


def _startup_command() -> str:
    """The command Windows should run at login to start Superyap."""
    if getattr(sys, "frozen", False):
        # Packaged .exe (milestone 7): just run the exe itself.
        return f'"{sys.executable}"'
    # Running from source: use pythonw.exe so no console window pops up.
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if pythonw.exists():
        python = pythonw
    main_py = Path(__file__).with_name("main.py")
    return f'"{python}" "{main_py}"'


# ----------------------------------------------------------------------
# The tray icon itself
# ----------------------------------------------------------------------
class TrayIcon(QSystemTrayIcon):
    """Tray icon with exactly two menu items: startup toggle + Quit."""

    def __init__(self, parent=None) -> None:
        super().__init__(_make_icon(), parent)
        self.setToolTip("Superyap -- hold Left Ctrl and speak")

        # Keep our own reference: Qt does not take ownership of the menu.
        self._menu = QMenu()

        self._startup_action = QAction("Start with Windows", checkable=True)
        self._startup_action.setChecked(is_startup_enabled())
        # Connect only after setChecked so creating the menu never writes
        # to the registry.
        self._startup_action.toggled.connect(self._on_startup_toggled)
        self._menu.addAction(self._startup_action)

        self._menu.addSeparator()
        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self._quit)
        self._menu.addAction(quit_action)

        self.setContextMenu(self._menu)

    def _on_startup_toggled(self, checked: bool) -> None:
        """Apply the toggle; on failure log it and put the checkmark back."""
        try:
            set_startup_enabled(checked)
        except OSError as exc:
            log.error("Could not update 'Start with Windows': %s", exc)
            self._startup_action.blockSignals(True)
            self._startup_action.setChecked(is_startup_enabled())
            self._startup_action.blockSignals(False)
        else:
            log.info("'Start with Windows' is now %s.", "on" if checked else "off")

    def _quit(self) -> None:
        """Quit the app (aboutToQuit already unhooks keys and the mic)."""
        app = QApplication.instance()
        if app is not None:
            app.quit()


def _make_icon() -> QIcon:
    """Draw the tray icon: a tiny dark pill with three waveform bars.

    Drawn in code so the app needs no image files.
    """
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    # Dark rounded pill.
    pill = QRectF(6, 22, 52, 20)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(30, 30, 38))
    painter.drawRoundedRect(pill, 10, 10)

    # Three white waveform bars in the middle.
    painter.setBrush(QColor(240, 240, 245))
    for i, bar_h in enumerate((8, 15, 11)):
        x = 22 + i * 8
        painter.drawRoundedRect(QRectF(x, 32 - bar_h / 2, 4, bar_h), 2, 2)

    painter.end()
    return QIcon(pixmap)
