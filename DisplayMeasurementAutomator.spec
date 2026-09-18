from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


project_root = Path(SPECPATH)
datas = []
binaries = []
hiddenimports = []

for package_name in ("rapidocr", "onnxruntime"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

datas.extend(copy_metadata("rapidocr"))
hiddenimports.extend(
    [
        "cv2",
        "numpy",
        "PIL.Image",
        "PIL.ImageOps",
        "PIL.ImageTk",
        "xlsxwriter",
    ]
)

analysis = Analysis(
    [str(project_root / "display_measurement_automator_v2.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DisplayMeasurementAutomator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DisplayMeasurementAutomator",
)
