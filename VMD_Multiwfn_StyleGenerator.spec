# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


root = Path(SPECPATH).resolve()
style_dir = root / 'vmd_cube_styles'
style_data = [
    (str(path), 'vmd_cube_styles')
    for path in style_dir.iterdir()
    if path.is_file() and not path.name.startswith('custom_')
]


a = Analysis(
    [str(root / 'vmd_style_tool_qt6.py')],
    pathex=[str(root)],
    binaries=[],
    datas=style_data + [
        (str(root / 'vmd_custom_styles.default.json'), '.'),
    ],
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
    name='VMD_Multiwfn_StyleGenerator',
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
