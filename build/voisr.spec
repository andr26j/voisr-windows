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
