# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Voisr (Phase 5 Task 4 gate).
#
# One-folder build. Resource paths route through voisr.paths.resource_root(),
# which resolves to sys._MEIPASS when frozen -- see voisr/paths.py.
#
# WHISPER_DIR must point at a whisper.cpp build (fetch with
# scripts\fetch_whisper.ps1 -Destination <dir>). scripts\build_release.ps1
# stages one under build\_whisper_cpu and points this at it.
import os

block_cipher = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))
WHISPER_DIR = os.environ.get(
    "VOISR_WHISPER_CPU_DIR",
    os.path.join(REPO_ROOT, "build", "_whisper_cpu"),
)

a = Analysis(
    [os.path.join(REPO_ROOT, "build", "entry.py")],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=[
        (os.path.join(REPO_ROOT, "voisr", "web"), "voisr/web"),
        (os.path.join(REPO_ROOT, "assets"), "assets"),
        (os.path.join(REPO_ROOT, "models", "ggml-base.en.bin"), "models"),
        (WHISPER_DIR, "bin"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

# whisper-cli.exe and the ggml DLLs dynamically link the MSVC runtime and
# OpenMP (see the import tables). None of these ship with Windows -- they come
# from the VC++ 2015-2022 redistributable, which a clean install does not have,
# and voisr.iss is deliberately per-user/unelevated so it cannot run
# vc_redist.exe. So deploy them app-local, which needs no admin.
#
# They must land in bin/ beside whisper-cli.exe: Windows searches a new
# process's own directory first, and PyInstaller's own copies sit one level up
# in _MEIPASS, where whisper-cli.exe will never look.
WHISPER_MSVC_DLLS = (
    "MSVCP140.dll",
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
    "VCOMP140.DLL",
)


def _locate_dll(name):
    """Reuse PyInstaller's collected copy, else fall back to System32."""
    for dest, src, _kind in a.binaries:
        if os.path.basename(dest).lower() == name.lower():
            return src
    system32 = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", name)
    if os.path.isfile(system32):
        return system32
    # Failing loudly beats shipping a bundle whose transcriber cannot start.
    raise SystemExit(
        f"voisr.spec: {name} not found. Install the Visual C++ 2015-2022 "
        "redistributable on this build machine and rebuild.")


a.binaries += [
    (os.path.join("bin", name), _locate_dll(name), "BINARY")
    for name in WHISPER_MSVC_DLLS
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Voisr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(REPO_ROOT, "assets", "voisr.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Voisr",
)
