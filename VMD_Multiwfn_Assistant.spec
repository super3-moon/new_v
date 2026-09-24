# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


root = Path(SPECPATH).resolve()
style_dir = root / 'vmd_cube_styles'
used_style_assets = {
    '02_color_mapped_transparent.jpg',
    '03_iso2_overlap_cube.png',
    '04_isosurface_iso_tachyon.png',
    '05_edgyglass_tuned_opacity.jpg',
    '06_turbo_colorscale_edgyglass.jpg',
    '07_glossy_default.jpg',
    '08_vmdrender_soft_material.jpg',
    '10_tachyon_mediumshade_vmd.jpg',
    '13_bbs_goodsell_example.jpg',
    '14_bbs_edgy_example1.jpg',
    '18_sob449_1.jpg',
    '19_sob449_2.jpg',
    '23_bright_blue_yellow.png',
    '24_modern_cool_palette.png',
    'esp_e7_wireframe_reference.png',
}
style_data = [
    (str(path), 'vmd_cube_styles')
    for path in style_dir.iterdir()
    if path.is_file() and path.name in used_style_assets
]

# Keep the build policy narrow and local to this spec file. NumPy is only
# discovered through Pillow's optional array-conversion branches; the
# application never imports or passes NumPy arrays to Pillow. The listed Qt
# modules are pulled in by optional QtGui plugins, while the application uses
# only QtCore, QtGui, QtWidgets, and QtSvg.
unused_python_modules = [
    'numpy',
    'PySide6.QtNetwork',
    'PySide6.QtOpenGL',
    'PySide6.QtPdf',
    'PySide6.QtQml',
    'PySide6.QtQuick',
    'PySide6.QtVirtualKeyboard',
]

unused_qt_entries = {
    # Qt 6.11 resolves the Windows ICU runtime from System32.  The build
    # environment also exposes Poppler's incompatible ICU 78 on PATH; never
    # bundle those unrelated DLLs or QtCore fails to load after extraction.
    'icuuc.dll',
    'icudt78.dll',
    'pyside6/qt6network.dll',
    'pyside6/qt6opengl.dll',
    'pyside6/qt6pdf.dll',
    'pyside6/qt6qml.dll',
    'pyside6/qt6qmlmeta.dll',
    'pyside6/qt6qmlmodels.dll',
    'pyside6/qt6qmlworkerscript.dll',
    'pyside6/qt6quick.dll',
    'pyside6/qt6virtualkeyboard.dll',
    'pyside6/qtnetwork.pyd',
    'pyside6/plugins/imageformats/qpdf.dll',
    'pyside6/plugins/networkinformation/qnetworklistmanager.dll',
    'pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll',
    'pyside6/plugins/tls/qcertonlybackend.dll',
    'pyside6/plugins/tls/qopensslbackend.dll',
    'pyside6/plugins/tls/qschannelbackend.dll',
}


def keep_runtime_entry(entry):
    destination = str(entry[0]).replace('\\', '/').lower()
    return (
        destination not in unused_qt_entries
        and not destination.startswith('pyside6/translations/')
    )


a = Analysis(
    [str(root / 'vmd_style_tool_qt6.py')],
    pathex=[str(root)],
    binaries=[],
    datas=style_data + [
        (str(root / 'vmd_custom_styles.default.json'), '.'),
    ],
    hiddenimports=[
        'orbital_diagram_workflow',
        'orbital_diagram_renderer',
        'orbital_data',
        'orbital_vmd',
        'scientific_workflows',
        'scientific_workflows_qt6',
        'PIL',
        'PIL.Image',
        'PIL.ImageChops',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        'PIL.PngImagePlugin',
        'PIL.TgaImagePlugin',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=unused_python_modules,
    noarchive=False,
    optimize=0,
)
a.binaries = [entry for entry in a.binaries if keep_runtime_entry(entry)]
a.datas = [entry for entry in a.datas if keep_runtime_entry(entry)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VMD_Multiwfn_Assistant',
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
