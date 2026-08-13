# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
# spicelib is vendored locally (see spicelib_vendor/VENDORED.md) instead of
# depending on the pip-installed package, so this collects from the local
# folder, not site-packages.
hiddenimports = ['spicelib_vendor.scripts.asc_to_qsch']
tmp_ret = collect_all('spicelib_vendor')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


# pandas, scipy, and matplotlib are NOT used by anything this tool's own
# code path calls. collect_all('spicelib_vendor') above pulls in the whole
# vendored package regardless -- including unused utility scripts (a
# histogram/step-log plotter, a raw-file-to-DataFrame exporter) that
# reference these only inside their own function bodies, several already
# behind their own "except ImportError" guards (spicelib's own optional-
# dependency handling). PyInstaller's static analysis can't know those
# functions are never called, so without this exclude it bundles all three
# anyway -- confirmed over 200MB combined (pandas 62MB + scipy 111MB +
# matplotlib 28MB) of genuinely dead weight, a major reason the built exe
# was large and slow to self-extract on first launch. numpy is NOT excluded
# here -- RawRead's actual array-based .raw file parsing (used by this
# tool's digital-gate logic-level lookup) genuinely needs it.
excludes = ['pandas', 'scipy', 'matplotlib']

a = Analysis(
    ['newtool.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name='newtool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX compression trades a smaller file for slower startup (every
    # bundled binary has to be decompressed on load) and makes an already-
    # unsigned exe look more like a malware packer to antivirus real-time
    # scanning -- a common cause of "hangs on first launch." Not worth it
    # for a tool run locally from a known folder, not distributed as a
    # download -- especially now that the excludes above already do most
    # of the size reduction UPX would have tried to claw back anyway.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
