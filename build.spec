# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for MT2 Fishing Bot
# Bundles the executable with the icon and GIF resources

import os
import sys

python_base = sys.base_prefix
python_dlls = os.path.join(python_base, 'DLLs')

# No module exclusions - include everything to avoid missing library errors
# This will result in a larger executable but ensures all dependencies are present
excludes = []

a = Analysis(
    ['src/fishing_bot.py'],
    pathex=['src'],
    binaries=[
        (os.path.join(python_dlls, '_tkinter.pyd'), '.'),
        (os.path.join(python_dlls, 'tcl86t.dll'), '.'),
        (os.path.join(python_dlls, 'tk86t.dll'), '.'),
    ],
    datas=[
        ('assets/ac_valhalla_logo.gif', '.'),
        ('assets/ac_valhalla.ico', '.'),
        ('assets', 'assets'),
        (os.path.join(python_base, 'tcl', 'tcl8.6'), '_tcl_data'),
        (os.path.join(python_base, 'tcl', 'tk8.6'), '_tk_data'),
        (os.path.join(python_base, 'tcl', 'tcl8'), '_tcl_data/tcl8'),
        (os.path.join(python_base, 'tcl', 'dde1.4'), '_tcl_data/dde1.4'),
        (os.path.join(python_base, 'tcl', 'reg1.3'), '_tcl_data/reg1.3'),
        (os.path.join(python_base, 'Lib', 'tkinter'), 'tkinter'),
    ],
    hiddenimports=[
        # Only explicitly list what's actually imported
        '_tkinter',
        'tkinter',
        'tkinter.colorchooser',
        'tkinter.filedialog',
        'tkinter.font',
        'tkinter.messagebox',
        'tkinter.ttk',
        'PIL.Image',
        'PIL.ImageTk',
        'PIL.GifImagePlugin',
        'PIL.PngImagePlugin',  # For PNG support
        'pynput.keyboard',
        'cv2',
        'numpy',
        'numpy.core',
        'psutil',
        'pyautogui',
        'mss',
        'pygetwindow',
        'updater',
        'version',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
    optimize=0,  # No bytecode optimization (NumPy requires docstrings)
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Huangue Fish bot v 1.2.1',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,  # strip is Linux-only, doesn't work on Windows
    upx=False,     # UPX compression disabled
    upx_exclude=[],
    runtime_tmpdir='.',
    console=False,
    uac_admin=True,
    disable_windowed_traceback=False,
    icon=os.path.join(SPECPATH, 'assets/ac_valhalla.ico'),
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
