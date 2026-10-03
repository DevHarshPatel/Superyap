# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build script for Superyap (milestone 7).

Build the single .exe with:

    python -m PyInstaller superyap.spec

The result is dist\\Superyap.exe -- one file, no console window, with the
pill icon. Put your .env (GROQ_API_KEY) next to the .exe; the log file
(superyap.log) is written next to it too.
"""

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # No data files on purpose: .env must NOT be baked into the exe (it holds
    # the API key) -- it is read from the exe's folder at runtime (config.py).
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Superyap",
    icon="app_icon.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX triggers antivirus false positives; not worth it
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # no console window (the pill is the whole UI)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
