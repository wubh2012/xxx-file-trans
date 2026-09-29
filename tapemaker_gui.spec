# -*- mode: python ; coding: utf-8 -*-
# 制片 GUI 独立 exe（issue #27 同模式：onefile + 无控制台窗口）。
# 构建命令：.venv/Scripts/python.exe -m PyInstaller tapemaker_gui.spec
# 产物：dist/tapemaker_gui.exe（双击即用，无需 Python 环境）


a = Analysis(
    ['tapemaker_gui.pyw'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='tapemaker_gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
