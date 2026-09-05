# -*- mode: python ; coding: utf-8 -*-
import os

# Every module under tabs/, listed from the filesystem rather than by importing the
# package: collect_submodules imports it, and if any dependency is missing at build
# time it returns nothing and only warns. Listing them by hand meant a new tab could be
# bundled while the worker it imports was left out, which fails only at runtime, in the
# built executable, with a ModuleNotFoundError.
tabs_modules = ['tabs.' + name[:-3] for name in sorted(os.listdir('tabs'))
                if name.endswith('.py') and name != '__init__.py']


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('./assets', 'assets'), ('./licenses', 'licenses'), ('./translations', 'translations')],
    hiddenimports=['win32console', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'pyqtgraph', 'shiboken6', 'odrive.fibre'] + tabs_modules,
    hookspath=['./hooks'],
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
    name='ODrive GUI Configurador',
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
    icon=['assets\\odrive_icon.ico'],
)
