# Voisr Windows — Core Dictation Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hold right-Ctrl anywhere in Windows, speak, and cleaned text appears at the cursor — with the original clipboard preserved.

**Architecture:** Python package `voisr`, ported from the macOS app `voisr-mac`. Audio is captured with `sounddevice` straight to a 16 kHz mono WAV, transcribed by a bundled `whisper-cli.exe` (CUDA build) as a subprocess, cleaned by pure-Python text normalisation, then delivered by writing to the Win32 clipboard and injecting Ctrl+V via `SendInput`. A low-level keyboard hook (`WH_KEYBOARD_LL`) on a dedicated message-pump thread drives push-to-talk. All Win32 access is via `ctypes`, no compiled extensions.

**Tech Stack:** Python 3.13, ctypes/Win32, sounddevice, whisper.cpp (cuBLAS x64 build), pywebview + WebView2 (overlay only), pytest.

**Covers:** Phases 0 and 1 of `docs/superpowers/specs/2026-07-28-voisr-windows-port-design.md`. Phases 2–5 (history durability, dashboard, tray/autostart/chrome, installer) are separate plans.

**Source app for ported code:** clone `andr26j/voisr-mac` to a scratch directory and reference it as `$MAC` below. Every "copy verbatim" instruction means byte-for-byte, no reformatting.

---

## File Structure

| File | Responsibility |
|---|---|
| `voisr/__init__.py` | Empty package marker |
| `voisr/paths.py` | Locate bundled binaries; resolve `%LOCALAPPDATA%\Voisr` |
| `voisr/cleanup.py` | Transcript normalisation (verbatim from macOS) |
| `voisr/stream_recorder.py` | Mic capture to WAV (verbatim from macOS) |
| `voisr/transcriber.py` | whisper-cli.exe subprocess wrapper |
| `voisr/pasteboard.py` | Win32 clipboard adapter + error types |
| `voisr/inject.py` | Clipboard write, Ctrl+V injection, UIPI detection, paste state machine |
| `voisr/keys.py` | Windows VK code table, name↔code↔label mapping |
| `voisr/keytap.py` | `WH_KEYBOARD_LL` listener on a message-pump thread |
| `voisr/overlay.py` | Floating pill (transparent, click-through, non-focusing) |
| `voisr/app.py` | CLI entry: wires hook → record → transcribe → clean → paste |
| `scripts/setup.ps1` | venv, deps, whisper binary + model download |
| `scripts/spike_overlay.py` | **Throwaway.** Phase 0 proof. Deleted in Task 12. |

Separation rationale: `keys.py` is new (macOS kept the key table inside `ui.py`); it is pure data + lookup with no Win32 calls, so it is trivially testable and both `keytap.py` and `app.py` depend on it without depending on each other.

---

## Task 1: Repository scaffolding

**Files:**
- Create: `.gitignore`, `requirements.txt`, `pytest.ini`, `voisr/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Create `.gitignore`**

```gitignore
venv/
__pycache__/
*.pyc
.pytest_cache/
bin/
models/
data/
node_modules/
*.wav
*.aiff
```

`bin/` and `models/` are ignored deliberately — the whisper binary is ~30 MB and the models are 141–465 MB. `scripts/setup.ps1` fetches both.

- [ ] **Step 2: Create `requirements.txt`**

```
pytest
pywebview
sounddevice
pywin32
pystray
Pillow
pythonnet
```

- [ ] **Step 3: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
filterwarnings =
    error::DeprecationWarning:voisr.*
```

- [ ] **Step 4: Create empty package markers**

```bash
python -c "open('voisr/__init__.py','w').close(); open('tests/__init__.py','w').close()"
```

- [ ] **Step 5: Create the venv and install**

```bash
python -m venv venv && ./venv/Scripts/python -m pip install -q -r requirements.txt
```

Expected: completes without error. If `pythonnet` fails to build on Python 3.13, stop and report — that blocks Task 2.

- [ ] **Step 6: Verify pytest runs**

Run: `./venv/Scripts/python -m pytest -q`
Expected: `no tests ran` — exit code 5. That is success at this stage.

- [ ] **Step 7: Commit**

```bash
git add .gitignore requirements.txt pytest.ini voisr/__init__.py tests/__init__.py
git commit -m "chore: scaffold Windows package, deps, and pytest config"
```

---

## Task 2: Phase 0 spike — prove the overlay is possible

**This task is a gate.** Its only output is a yes/no answer plus a throwaway script. Do not build anything on top of it until it passes. If it fails, stop and report — the fallback (native-drawn pill) changes Task 12 entirely.

**Files:**
- Create: `scripts/spike_overlay.py` (deleted in Task 12)

- [ ] **Step 1: Write the spike**

```python
"""Phase 0 spike: can a pywebview window be transparent, click-through,
always-on-top, and never take focus?

Run it, then follow the checklist printed to the console. This file is
deleted once voisr/overlay.py exists.
"""
import ctypes
from ctypes import wintypes
import threading
import time

import webview

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]

HTML = """
<body style="margin:0;background:transparent;">
  <div style="width:232px;height:52px;border-radius:26px;
              background:rgba(20,20,22,0.85);color:#fff;
              font:600 14px system-ui;display:flex;
              align-items:center;justify-content:center;">
    spike pill
  </div>
</body>
"""


def find_hwnd(title: str):
    return user32.FindWindowW(None, title)


def apply_styles(hwnd) -> int:
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    return ex


def patch_later(title: str):
    for _ in range(50):
        hwnd = find_hwnd(title)
        if hwnd:
            ex = apply_styles(hwnd)
            print(f"hwnd={hwnd:#x} new exstyle={ex:#x}")
            print("\n--- CHECK ALL FOUR ---")
            print("1. Pill visible with NO white/black box around it")
            print("2. Open a fullscreen app - pill still visible on top")
            print("3. Click where the pill is - the app underneath receives it")
            print("4. Type in Notepad while pill shows - keystrokes land in Notepad")
            return
        time.sleep(0.1)
    print("FAILED: never found the window")


title = "voisr-spike"
threading.Thread(target=patch_later, args=(title,), daemon=True).start()
window = webview.create_window(
    title, html=HTML, width=232, height=52,
    frameless=True, on_top=True, transparent=True, resizable=False,
)
webview.start(gui="edgechromium")
```

- [ ] **Step 2: Run the spike**

Run: `./venv/Scripts/python scripts/spike_overlay.py`
Expected: a window appears and the console prints an hwnd plus the checklist.

- [ ] **Step 3: Walk the four checks manually**

Record each as pass/fail. Check 1 (real transparency) is the one most likely to fail — WebView2 may paint an opaque rectangle behind the rounded pill.

- [ ] **Step 4: Decide the gate**

- **All four pass** → continue to Task 3. Task 12 uses this approach.
- **Check 1 fails, 2–4 pass** → try `webview.create_window(..., transparent=True, background_color='#000000')` and re-run. If still opaque, **stop and report**: the fallback is a native-drawn pill and Task 12 must be rewritten.
- **Check 3 or 4 fails** → the extended styles are being reset by pywebview after our patch. Re-apply them from a `window.events.shown` handler and re-run.

- [ ] **Step 5: Commit the spike with its verdict**

```bash
git add scripts/spike_overlay.py
git commit -m "spike: prove transparent click-through non-focusing overlay

Checks: 1 transparency=<PASS|FAIL>, 2 fullscreen=<PASS|FAIL>,
3 click-through=<PASS|FAIL>, 4 no-focus=<PASS|FAIL>."
```

Replace each `<PASS|FAIL>` with the observed result before committing.

---

## Task 3: `paths.py` — binary and data-directory resolution

**Files:**
- Create: `voisr/paths.py`
- Test: `tests/test_paths.py`

- [ ] **Step 1: Write the failing tests**

```python
import voisr.paths as paths


def test_find_binary_prefers_bundled_bin(tmp_path, monkeypatch):
    bundled = tmp_path / "whisper-cli.exe"
    bundled.write_bytes(b"")
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    assert paths.find_binary("whisper-cli") == str(bundled)


def test_find_binary_appends_exe_suffix(tmp_path, monkeypatch):
    bundled = tmp_path / "ffmpeg.exe"
    bundled.write_bytes(b"")
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    assert paths.find_binary("ffmpeg") == str(bundled)


def test_find_binary_does_not_double_suffix(tmp_path, monkeypatch):
    bundled = tmp_path / "ffmpeg.exe"
    bundled.write_bytes(b"")
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    assert paths.find_binary("ffmpeg.exe") == str(bundled)


def test_find_binary_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda name: r"C:\tools\ffmpeg.exe")
    assert paths.find_binary("ffmpeg") == r"C:\tools\ffmpeg.exe"


def test_find_binary_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    assert paths.find_binary("nope") is None


def test_app_data_dir_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.app_data_dir() == tmp_path / "Voisr"


def test_app_data_dir_falls_back_without_env(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    result = paths.app_data_dir()
    assert result.name == "Voisr"
    assert "AppData" in str(result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python -m pytest tests/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.paths'`

- [ ] **Step 3: Write the implementation**

```python
import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def find_binary(name: str) -> str | None:
    """Resolve a CLI tool: bundled bin/ first, then PATH.

    bin/ wins because the CUDA whisper build ships its DLLs alongside the
    exe, and Windows searches an executable's own directory first — a
    whisper-cli.exe found on PATH would not have them.
    """
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    bundled = Path(BIN_DIR) / exe
    if bundled.is_file():
        return str(bundled)
    return shutil.which(exe)


def app_data_dir() -> Path:
    """Per-user data root: %LOCALAPPDATA%\\Voisr."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "Voisr"


def ensure_app_data_dir() -> Path:
    target = app_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python -m pytest tests/test_paths.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add voisr/paths.py tests/test_paths.py
git commit -m "feat: resolve bundled binaries and %LOCALAPPDATA% data dir"
```

---

## Task 4: `setup.ps1` — fetch whisper binary and model

**Files:**
- Create: `scripts/setup.ps1`

- [ ] **Step 1: Write the setup script**

```powershell
# Voisr Windows setup: venv, dependencies, whisper.cpp CUDA build, model.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$WHISPER_TAG   = "v1.9.1"
$WHISPER_ASSET = "whisper-cublas-12.4.0-bin-x64.zip"
$MODEL         = "ggml-base.en.bin"
$MODEL_URL     = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL"

Write-Host "==> venv + dependencies"
if (-not (Test-Path "$root\venv")) { python -m venv "$root\venv" }
& "$root\venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$root\venv\Scripts\python.exe" -m pip install -q -r "$root\requirements.txt"

Write-Host "==> whisper.cpp $WHISPER_TAG ($WHISPER_ASSET)"
New-Item -ItemType Directory -Force "$root\bin" | Out-Null
if (-not (Test-Path "$root\bin\whisper-cli.exe")) {
    $zip = "$env:TEMP\$WHISPER_ASSET"
    $url = "https://github.com/ggml-org/whisper.cpp/releases/download/$WHISPER_TAG/$WHISPER_ASSET"
    Invoke-WebRequest -Uri $url -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath "$env:TEMP\whisper-extract" -Force
    Get-ChildItem "$env:TEMP\whisper-extract" -Recurse -Include *.exe,*.dll |
        ForEach-Object { Copy-Item $_.FullName "$root\bin\" -Force }
    Remove-Item $zip, "$env:TEMP\whisper-extract" -Recurse -Force
}
if (-not (Test-Path "$root\bin\whisper-cli.exe")) {
    throw "whisper-cli.exe missing after extract - check the release asset layout"
}

Write-Host "==> model $MODEL"
New-Item -ItemType Directory -Force "$root\models" | Out-Null
if (-not (Test-Path "$root\models\$MODEL")) {
    Invoke-WebRequest -Uri $MODEL_URL -OutFile "$root\models\$MODEL"
}

Write-Host "==> done"
& "$root\bin\whisper-cli.exe" --help | Select-Object -First 3
```

- [ ] **Step 2: Run it**

Run: `powershell -ExecutionPolicy Bypass -File scripts/setup.ps1`
Expected: ends by printing the first three lines of whisper-cli's help text.

- [ ] **Step 3: Verify CUDA is actually present**

Run: `powershell -Command "Get-ChildItem bin\*.dll | Select-Object -ExpandProperty Name"`
Expected: includes `cublas64_12.dll` and `cudart64_12.dll`. If those are missing, the wrong asset was downloaded and transcription will fall back to CPU.

- [ ] **Step 4: Commit**

```bash
git add scripts/setup.ps1
git commit -m "feat: setup script fetching whisper cuBLAS build and base.en model"
```

---

## Task 5: `cleanup.py` — copy verbatim

**Files:**
- Create: `voisr/cleanup.py` (copy of `$MAC/voisr/cleanup.py`)
- Test: `tests/test_cleanup.py` (copy of `$MAC/tests/test_cleanup.py`)

This module is pure `re` and `hashlib`. Nothing in it is platform-specific.

- [ ] **Step 1: Copy the module and its tests byte-for-byte**

```bash
cp "$MAC/voisr/cleanup.py" voisr/cleanup.py
cp "$MAC/tests/test_cleanup.py" tests/test_cleanup.py
```

- [ ] **Step 2: Run the tests**

Run: `./venv/Scripts/python -m pytest tests/test_cleanup.py -v`
Expected: all pass.

If any test errors with `fixture 'spoken_wav' not found`, that test needs the conftest built in Task 14. Mark those specific tests with `@pytest.mark.slow` and add `import pytest` to the file if absent — Task 14 registers the marker and supplies the fixture, at which point they run. Do not delete them, and do not edit `voisr/cleanup.py` itself.

- [ ] **Step 3: Verify the module is genuinely unmodified**

Run: `diff "$MAC/voisr/cleanup.py" voisr/cleanup.py`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add voisr/cleanup.py tests/test_cleanup.py
git commit -m "feat: port transcript cleanup verbatim from macOS"
```

---

## Task 6: `stream_recorder.py` — copy verbatim

**Files:**
- Create: `voisr/stream_recorder.py` (copy of `$MAC/voisr/stream_recorder.py`)
- Test: `tests/test_stream_recorder.py` (copy of `$MAC/tests/test_stream_recorder.py`)

`sounddevice` wraps PortAudio, which supports WASAPI/MME on Windows. The macOS comment about the "microphone in use" indicator does not apply on Windows, but the open-only-while-recording behaviour is still correct and the code is unchanged.

- [ ] **Step 1: Copy the module and its tests**

```bash
cp "$MAC/voisr/stream_recorder.py" voisr/stream_recorder.py
cp "$MAC/tests/test_stream_recorder.py" tests/test_stream_recorder.py
```

- [ ] **Step 2: Run the tests**

Run: `./venv/Scripts/python -m pytest tests/test_stream_recorder.py -v`
Expected: all pass.

- [ ] **Step 3: Prove real capture works on this machine**

```bash
./venv/Scripts/python -c "
from voisr.stream_recorder import StreamRecorder
import time, wave
r = StreamRecorder(); r.start('probe.wav')
print('speak for 3 seconds...'); time.sleep(3); r.stop()
with wave.open('probe.wav') as w:
    print('frames:', w.getnframes(), 'rate:', w.getframerate())
"
```

Expected: `frames:` around 48000, `rate: 16000`. A frame count near zero means the default input device is wrong or muted — fix that before continuing, because every later task depends on capture working.

- [ ] **Step 4: Commit**

```bash
git add voisr/stream_recorder.py tests/test_stream_recorder.py
git commit -m "feat: port sounddevice stream recorder verbatim from macOS"
```

---

## Task 7: `transcriber.py` — whisper-cli.exe

**Files:**
- Create: `voisr/transcriber.py`
- Test: `tests/test_transcriber.py`

- [ ] **Step 1: Write the failing tests**

```python
import subprocess
from pathlib import Path

import pytest

import voisr.transcriber as transcriber
from voisr.transcriber import TranscriptionError, resolve_model, transcribe


def test_resolve_model_prefers_named(tmp_path):
    (tmp_path / "ggml-small.en.bin").write_bytes(b"")
    (tmp_path / "ggml-base.en.bin").write_bytes(b"")
    assert resolve_model("base.en", tmp_path) == tmp_path / "ggml-base.en.bin"


def test_resolve_model_prefers_small_when_unnamed(tmp_path):
    (tmp_path / "ggml-small.en.bin").write_bytes(b"")
    (tmp_path / "ggml-base.en.bin").write_bytes(b"")
    assert resolve_model(None, tmp_path) == tmp_path / "ggml-small.en.bin"


def test_resolve_model_falls_back_to_base_path(tmp_path):
    assert resolve_model(None, tmp_path) == tmp_path / "ggml-base.en.bin"


def test_missing_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "find_binary", lambda name: None)
    with pytest.raises(TranscriptionError, match="whisper-cli.exe not found"):
        transcribe(tmp_path / "a.wav")


def test_missing_model_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    with pytest.raises(TranscriptionError, match="model missing"):
        transcribe(tmp_path / "a.wav", model_path=tmp_path / "absent.bin")


def test_transcribe_reads_output_and_cleans_up(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    model = tmp_path / "m.bin"
    model.write_bytes(b"")
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        Path(str(wav) + ".out.txt").write_text("  hello world  ")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    monkeypatch.setattr(transcriber.subprocess, "run", fake_run)

    assert transcribe(wav, model_path=model) == "hello world"
    assert not Path(str(wav) + ".out.txt").exists()
    assert "--beam-size" in recorded["cmd"]


def test_transcribe_passes_prompt(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    model = tmp_path / "m.bin"
    model.write_bytes(b"")
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        Path(str(wav) + ".out.txt").write_text("x")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    monkeypatch.setattr(transcriber.subprocess, "run", fake_run)

    transcribe(wav, model_path=model, prompt="TradeMate")
    assert "--prompt" in recorded["cmd"]
    assert "TradeMate" in recorded["cmd"]


def test_nonzero_exit_raises(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    model = tmp_path / "m.bin"
    model.write_bytes(b"")

    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    monkeypatch.setattr(
        transcriber.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "cuda error"))

    with pytest.raises(TranscriptionError, match="cuda error"):
        transcribe(wav, model_path=model)


def test_subprocess_hides_console_window(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    model = tmp_path / "m.bin"
    model.write_bytes(b"")
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded.update(kwargs)
        Path(str(wav) + ".out.txt").write_text("x")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    monkeypatch.setattr(transcriber.subprocess, "run", fake_run)

    transcribe(wav, model_path=model)
    assert recorded["creationflags"] == transcriber.CREATE_NO_WINDOW
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python -m pytest tests/test_transcriber.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.transcriber'`

- [ ] **Step 3: Write the implementation**

```python
import subprocess
from pathlib import Path

from voisr.paths import find_binary

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH = MODELS_DIR / "ggml-base.en.bin"

# best available first: small.en is a large accuracy jump over base.en
PREFERRED_MODELS = ["small.en", "base.en"]

# Without this every dictation flashes a console window over the user's app.
CREATE_NO_WINDOW = 0x08000000


class TranscriptionError(RuntimeError):
    pass


def resolve_model(name: str | None = None, models_dir: Path = MODELS_DIR) -> Path:
    """Best model on disk: the configured one, else small.en, else base.en."""
    candidates = ([name] if name else []) + PREFERRED_MODELS
    for n in candidates:
        p = Path(models_dir) / f"ggml-{n}.bin"
        if p.exists():
            return p
    return Path(models_dir) / f"ggml-{PREFERRED_MODELS[-1]}.bin"


def transcribe(wav_path, model_path=None, whisper_bin=None, prompt=None) -> str:
    """Transcribe a 16 kHz mono WAV with whisper-cli.exe, return plain text.

    prompt: optional context string (e.g. the user's dictionary terms) that
    biases decoding toward the right spellings of names and jargon."""
    whisper = whisper_bin or find_binary("whisper-cli")
    if not whisper:
        raise TranscriptionError(
            "whisper-cli.exe not found — run: powershell -File scripts/setup.ps1")
    model = Path(model_path) if model_path else resolve_model()
    if not model.exists():
        raise TranscriptionError(f"model missing: {model} — run scripts/setup.ps1")
    out_prefix = str(wav_path) + ".out"
    cmd = [whisper, "-m", str(model), "-f", str(wav_path),
           "--output-txt", "--output-file", out_prefix, "--no-prints",
           "--beam-size", "5"]
    if prompt:
        cmd += ["--prompt", prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise TranscriptionError(proc.stderr.strip() or "whisper-cli failed")
    txt = Path(out_prefix + ".txt")
    text = txt.read_text(encoding="utf-8").strip()
    txt.unlink(missing_ok=True)
    return text
```

Differences from macOS, all deliberate: `find_binary("whisper-cli")` now resolves `.exe` via `paths.py`; `creationflags=CREATE_NO_WINDOW` suppresses the console flash; `read_text(encoding="utf-8")` is explicit because Windows would otherwise use the ANSI codepage and mangle non-ASCII output.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python -m pytest tests/test_transcriber.py -v`
Expected: 9 passed

- [ ] **Step 5: Prove real GPU transcription end-to-end**

```bash
./venv/Scripts/python -c "
from voisr.stream_recorder import StreamRecorder
from voisr.transcriber import transcribe
from voisr.cleanup import clean
import time
r = StreamRecorder(); r.start('probe.wav')
print('say: testing one two three'); time.sleep(4); r.stop()
print(repr(clean(transcribe('probe.wav'))))
"
```

Expected: prints roughly `'Testing one two three.'` within a couple of seconds. **This is the first proof the whole transcription half works.**

- [ ] **Step 6: Commit**

```bash
git add voisr/transcriber.py tests/test_transcriber.py
git commit -m "feat: transcribe via bundled whisper-cli.exe with no console flash"
```

---

## Task 8: `pasteboard.py` — Win32 clipboard adapter

The error types and snapshot dataclasses are copied verbatim so `inject.py`'s state machine (Task 9) works unchanged. Only the adapter class is new.

**Files:**
- Create: `voisr/pasteboard.py`
- Test: `tests/test_pasteboard.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from voisr.pasteboard import (
    CF_LOCALE,
    CF_OEMTEXT,
    CF_TEXT,
    CF_UNICODETEXT,
    ClipboardPreservationError,
    PasteboardItemSnapshot,
    PasteboardSnapshot,
    WindowsClipboard,
    is_synthesized,
)


class FakeWin32:
    """Stand-in for the Win32 clipboard surface WindowsClipboard calls."""

    def __init__(self, formats=None, sequence=100):
        self.formats = dict(formats or {CF_UNICODETEXT: "hello".encode("utf-16-le")})
        self.sequence = sequence
        self.open_failures = 0
        self.opened = 0
        self.closed = 0
        self.emptied = 0
        self.null_handles = set()

    def open(self):
        if self.open_failures:
            self.open_failures -= 1
            return False
        self.opened += 1
        return True

    def close(self):
        self.closed += 1

    def empty(self):
        self.emptied += 1
        self.formats.clear()
        self.sequence += 1

    def enum_formats(self):
        return list(self.formats)

    def get_data(self, fmt):
        if fmt in self.null_handles:
            return None
        return self.formats[fmt]

    def set_data(self, fmt, payload):
        self.formats[fmt] = payload
        return True

    def get_sequence(self):
        return self.sequence


def make(win32):
    return WindowsClipboard(win32=win32)


def test_synthesized_text_formats_are_recognised():
    assert is_synthesized(CF_TEXT)
    assert is_synthesized(CF_OEMTEXT)
    assert is_synthesized(CF_LOCALE)
    assert not is_synthesized(CF_UNICODETEXT)


def test_snapshot_captures_sequence_and_payload():
    win32 = FakeWin32()
    snap = make(win32).snapshot()
    assert snap.change_count == 100
    assert snap.items[0].payloads[0][0] == str(CF_UNICODETEXT)
    assert win32.closed == win32.opened


def test_snapshot_skips_synthesized_formats():
    win32 = FakeWin32({CF_UNICODETEXT: b"a", CF_TEXT: b"a", CF_LOCALE: b"x"})
    snap = make(win32).snapshot()
    captured = {p[0] for p in snap.items[0].payloads}
    assert captured == {str(CF_UNICODETEXT)}


def test_snapshot_marks_partial_on_null_handle():
    win32 = FakeWin32({CF_UNICODETEXT: b"a", 49161: b"b"})
    win32.null_handles.add(49161)
    snap = make(win32).snapshot()
    assert snap.partial is True
    captured = {p[0] for p in snap.items[0].payloads}
    assert captured == {str(CF_UNICODETEXT)}


def test_snapshot_is_not_partial_when_everything_read():
    assert make(FakeWin32()).snapshot().partial is False


def test_open_retries_then_succeeds():
    win32 = FakeWin32()
    win32.open_failures = 2
    assert make(win32).snapshot().change_count == 100
    assert win32.opened == 1


def test_open_exhaustion_raises_preservation_error():
    win32 = FakeWin32()
    win32.open_failures = 99
    with pytest.raises(ClipboardPreservationError, match="could not be opened"):
        make(win32).snapshot()


def test_sequence_change_during_capture_raises():
    win32 = FakeWin32({CF_UNICODETEXT: b"a"})
    clip = make(win32)
    original = win32.get_data

    def racing(fmt):
        win32.sequence += 1
        return original(fmt)

    win32.get_data = racing
    with pytest.raises(ClipboardPreservationError, match="changed during capture"):
        clip.snapshot()


def test_is_owned_compares_sequence():
    win32 = FakeWin32()
    clip = make(win32)
    assert clip.is_owned(100) is True
    assert clip.is_owned(99) is False


def test_write_text_returns_new_sequence_and_sets_unicode():
    win32 = FakeWin32()
    clip = make(win32)
    snap = clip.snapshot()
    count = clip.write_text_if_unchanged("bye", snap)
    assert count == win32.sequence
    assert win32.formats[CF_UNICODETEXT] == "bye".encode("utf-16-le") + b"\x00\x00"


def test_written_text_is_null_terminated():
    """CF_UNICODETEXT must be null-terminated or readers drop the last
    character — observed truncating 'TEMPORARY-VOISR' to 'TEMPORARY-VOIS'."""
    win32 = FakeWin32()
    clip = make(win32)
    snap = clip.snapshot()
    clip.write_text_if_unchanged("hello", snap)
    assert win32.formats[CF_UNICODETEXT].endswith(b"\x00\x00")
    assert win32.formats[CF_UNICODETEXT].decode("utf-16-le").rstrip("\x00") == "hello"


def test_write_text_refuses_when_clipboard_changed():
    win32 = FakeWin32()
    clip = make(win32)
    snap = clip.snapshot()
    win32.sequence += 1
    with pytest.raises(ClipboardPreservationError, match="changed before"):
        clip.write_text_if_unchanged("bye", snap)


def test_restore_puts_every_captured_format_back():
    win32 = FakeWin32({CF_UNICODETEXT: b"a", 49161: b"rich"})
    clip = make(win32)
    snap = clip.snapshot()
    count = clip.write_text_if_unchanged("temp", snap)
    assert clip.restore_if_unchanged(snap, count) is True
    assert win32.formats[49161] == b"rich"


def test_restore_skips_when_someone_else_took_the_clipboard():
    win32 = FakeWin32()
    clip = make(win32)
    snap = clip.snapshot()
    count = clip.write_text_if_unchanged("temp", snap)
    win32.sequence += 5
    assert clip.restore_if_unchanged(snap, count) is False


def test_empty_snapshot_restores_without_error():
    win32 = FakeWin32({})
    clip = make(win32)
    snap = clip.snapshot()
    count = clip.write_text_if_unchanged("temp", snap)
    assert clip.restore_if_unchanged(snap, count) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python -m pytest tests/test_pasteboard.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.pasteboard'`

- [ ] **Step 3: Write the implementation**

```python
"""Win32 clipboard with macOS-compatible preservation semantics.

GetClipboardSequenceNumber() is the direct analogue of NSPasteboard's
changeCount, so the ownership model from the macOS app transfers intact and
voisr.inject's paste state machine is unchanged.

Three Windows-only hazards are handled here:
  * OpenClipboard fails transiently whenever another process holds the
    clipboard, so every open is retried with backoff.
  * Delayed rendering: apps like Excel advertise formats with a NULL handle
    and only render on request, which can hang. Those are skipped and the
    snapshot is flagged partial rather than silently claimed lossless.
  * Windows synthesizes CF_TEXT/CF_OEMTEXT/CF_LOCALE from CF_UNICODETEXT.
    Re-setting them fights the ones Windows regenerates, so they are skipped.
"""
import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_ENHMETAFILE = 14
CF_UNICODETEXT = 13
CF_LOCALE = 16
CF_DIBV5 = 17

# Formats Windows generates from another format on the clipboard. Capturing
# and re-setting these conflicts with the OS-generated copies.
SYNTHESIZED_FORMATS = frozenset({
    CF_TEXT, CF_OEMTEXT, CF_LOCALE,      # from CF_UNICODETEXT
    CF_BITMAP, CF_PALETTE, CF_DIBV5,     # from CF_DIB
    CF_ENHMETAFILE,                      # from CF_METAFILEPICT
})

OPEN_RETRIES = 10
OPEN_BACKOFF_S = 0.02
GMEM_MOVEABLE = 0x0002


def is_synthesized(fmt: int) -> bool:
    return fmt in SYNTHESIZED_FORMATS


@dataclass(frozen=True)
class PasteboardItemSnapshot:
    payloads: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class PasteboardSnapshot:
    change_count: int
    items: tuple[PasteboardItemSnapshot, ...]
    partial: bool = field(default=False)


class PasteDeliveryError(RuntimeError):
    def __init__(self, message: str, *, pasted: bool = False,
                 fallback_safe: bool = False):
        super().__init__(message)
        self.pasted = pasted
        self.fallback_safe = fallback_safe


class ClipboardPreservationError(PasteDeliveryError):
    pass


class _Win32Clipboard:
    """Thin ctypes surface. Isolated so tests can substitute a fake."""

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalSize.restype = ctypes.c_size_t
        self.kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self.kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.user32.GetClipboardData.restype = wintypes.HANDLE
        self.user32.GetClipboardData.argtypes = [wintypes.UINT]
        self.user32.SetClipboardData.restype = wintypes.HANDLE
        self.user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    def open(self) -> bool:
        return bool(self.user32.OpenClipboard(None))

    def close(self) -> None:
        self.user32.CloseClipboard()

    def empty(self) -> None:
        self.user32.EmptyClipboard()

    def enum_formats(self) -> list[int]:
        formats, fmt = [], 0
        while True:
            fmt = self.user32.EnumClipboardFormats(fmt)
            if not fmt:
                return formats
            formats.append(fmt)

    def get_data(self, fmt: int) -> bytes | None:
        handle = self.user32.GetClipboardData(fmt)
        if not handle:
            return None
        size = self.kernel32.GlobalSize(handle)
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            self.kernel32.GlobalUnlock(handle)

    def set_data(self, fmt: int, payload: bytes) -> bool:
        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload) or 1)
        if not handle:
            return False
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            self.kernel32.GlobalUnlock(handle)
        return bool(self.user32.SetClipboardData(fmt, handle))

    def get_sequence(self) -> int:
        return int(self.user32.GetClipboardSequenceNumber())


class WindowsClipboard:
    """Same interface as the macOS AppKitPasteboard: snapshot,
    write_text_if_unchanged, is_owned, restore_if_unchanged."""

    def __init__(self, win32=None):
        self._w = win32 if win32 is not None else _Win32Clipboard()

    def _open(self) -> None:
        for attempt in range(OPEN_RETRIES):
            if self._w.open():
                return
            time.sleep(OPEN_BACKOFF_S * (attempt + 1))
        raise ClipboardPreservationError(
            "clipboard could not be opened; another app is holding it")

    def snapshot(self) -> PasteboardSnapshot:
        self._open()
        try:
            sequence = self._w.get_sequence()
            payloads, partial = [], False
            for fmt in self._w.enum_formats():
                if is_synthesized(fmt):
                    continue
                data = self._w.get_data(fmt)
                if data is None:
                    partial = True  # delayed-render owner declined
                    continue
                payloads.append((str(fmt), data))
            if self._w.get_sequence() != sequence:
                raise ClipboardPreservationError("clipboard changed during capture")
        finally:
            self._w.close()
        items = (PasteboardItemSnapshot(tuple(payloads)),) if payloads else ()
        return PasteboardSnapshot(sequence, items, partial)

    def is_owned(self, expected_count) -> bool:
        try:
            return self._w.get_sequence() == expected_count
        except ClipboardPreservationError:
            raise
        except Exception as error:
            raise ClipboardPreservationError(
                "clipboard ownership check failed") from error

    def write_text_if_unchanged(self, text, snapshot) -> int:
        if self._w.get_sequence() != snapshot.change_count:
            raise ClipboardPreservationError(
                "clipboard changed before temporary write")
        self._open()
        try:
            self._w.empty()
            # The null terminator is mandatory. Without it readers drop the
            # last character — observed truncating "TEMPORARY-VOISR" to
            # "TEMPORARY-VOIS", which would corrupt every dictation.
            if not self._w.set_data(CF_UNICODETEXT,
                                    text.encode("utf-16-le") + b"\x00\x00"):
                raise ClipboardPreservationError("temporary clipboard write failed")
        finally:
            self._w.close()
        # Read the sequence AFTER close: CloseClipboard itself bumps the
        # counter (the clipboard-history service reacts to WM_CLIPBOARDUPDATE),
        # so a value read before closing is stale and restore would refuse.
        return self._w.get_sequence()

    def restore_if_unchanged(self, snapshot, expected_change_count) -> bool:
        if self._w.get_sequence() != expected_change_count:
            return False
        self._open()
        try:
            self._w.empty()
            for item in snapshot.items:
                for fmt, payload in item.payloads:
                    if not self._w.set_data(int(fmt), payload):
                        raise ClipboardPreservationError(
                            f"clipboard type could not be restored: {fmt}")
        finally:
            self._w.close()
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python -m pytest tests/test_pasteboard.py -v`
Expected: 15 passed

- [ ] **Step 5: Verify against the real clipboard**

```bash
./venv/Scripts/python -c "
from voisr.pasteboard import WindowsClipboard
c = WindowsClipboard()
s = c.snapshot()
print('formats:', [p[0] for i in s.items for p in i.payloads], 'partial:', s.partial)
n = c.write_text_if_unchanged('TEMP-VOISR', s)
print('restored:', c.restore_if_unchanged(s, n))
"
```

Copy some text in another app first. Expected: the original clipboard content is intact afterwards — paste somewhere to confirm.

- [ ] **Step 6: Commit**

```bash
git add voisr/pasteboard.py tests/test_pasteboard.py
git commit -m "feat: Win32 clipboard adapter with sequence-number preservation"
```

---

## Task 9: `inject.py` — Ctrl+V injection, UIPI detection, paste state machine

The `_paste_with_adapter` function and its helpers are copied **verbatim** from `$MAC/voisr/inject.py` lines 72–158. Only the two injection functions and the clipboard helpers are new.

**Files:**
- Create: `voisr/inject.py`
- Test: `tests/test_inject_unit.py`

- [ ] **Step 1: Copy the platform-independent tests**

Copy `$MAC/tests/test_inject_unit.py` lines 62–325 (everything from `class FakeAdapter:` onward) into `tests/test_inject_unit.py`, with this import header replacing lines 1–11:

```python
import subprocess

import pytest

import voisr.inject as inject
from voisr.pasteboard import (
    ClipboardPreservationError,
    PasteDeliveryError,
    PasteboardSnapshot,
)
```

Then apply exactly four renames throughout the copied tests — both the keyword arguments and the monkeypatched attribute names:

| Find | Replace |
|---|---|
| `post_cmd_v=` | `post_ctrl_v=` |
| `applescript_cmd_v=` | `fallback_ctrl_v=` |
| `"_post_cmd_v"` | `"_post_ctrl_v"` |
| `"_applescript_cmd_v"` | `"_fallback_ctrl_v"` |

The last two appear inside `monkeypatch.setattr` calls in `test_restore_disabled_leaves_text_for_manual_paste` and `test_restore_disabled_command_error_leaves_text_for_manual_paste`; missing them makes those two tests patch attributes that do not exist and silently test nothing.

Change nothing else — the assertions encode the exact ordering and error-chaining contract.

The two macOS-only tests (`test_plain_text_clipboard_commands_are_bounded_and_checked` and `test_clipboard_command_timeout_propagates`) are **not** copied; they test `pbcopy`/`pbpaste`/`osascript`. They are replaced in Step 2.

- [ ] **Step 2: Add the Windows-specific tests**

Append to `tests/test_inject_unit.py`:

```python
def test_set_and_get_clipboard_round_trip(monkeypatch):
    store = {}
    monkeypatch.setattr(inject, "_write_text", lambda text: store.update(text=text))
    monkeypatch.setattr(inject, "_read_text", lambda: store.get("text", ""))
    inject.set_clipboard("new")
    assert inject.get_clipboard() == "new"


def test_paste_blocked_when_foreground_is_higher_integrity(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: True)
    sent = []
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: sent.append(True))
    assert inject._post_ctrl_v() is False
    assert sent == []


def test_paste_proceeds_when_foreground_is_same_integrity(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: True)
    assert inject._post_ctrl_v() is True


def test_held_modifiers_are_released_before_injection(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    held = [inject.VK_MENU, inject.VK_SHIFT]
    monkeypatch.setattr(inject, "_held_modifiers", lambda: list(held))
    released, restored, sent = [], [], []
    monkeypatch.setattr(inject, "_key_up", released.append)
    monkeypatch.setattr(inject, "_key_down", restored.append)
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: sent.append(True) or True)

    assert inject._post_ctrl_v() is True
    assert released == held
    assert sent == [True]
    assert restored == held


def test_injection_failure_still_restores_held_modifiers(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(inject, "_held_modifiers", lambda: [inject.VK_MENU])
    restored = []
    monkeypatch.setattr(inject, "_key_up", lambda vk: None)
    monkeypatch.setattr(inject, "_key_down", restored.append)
    monkeypatch.setattr(
        inject, "_send_ctrl_v",
        lambda: (_ for _ in ()).throw(OSError("SendInput failed")))

    with pytest.raises(OSError):
        inject._post_ctrl_v()
    assert restored == [inject.VK_MENU]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./venv/Scripts/python -m pytest tests/test_inject_unit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.inject'`

- [ ] **Step 4: Write the implementation**

```python
"""Deliver dictated text to the frontmost app.

_paste_with_adapter and its helpers are ported unchanged from the macOS app:
the ordering and error-chaining contract there is exactly right and its tests
came across with it. Only the injection primitives are Windows-specific.
"""
import ctypes
import time
from ctypes import wintypes

from voisr.pasteboard import (
    CF_UNICODETEXT,
    ClipboardPreservationError,
    PasteDeliveryError,
    WindowsClipboard,
    _Win32Clipboard,
)

PASTE_GRACE_S = 0.3

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# Tags our own synthetic events so voisr.keytap can ignore them. LLKHF_INJECTED
# is not usable for this — it is set by every injecting app, not just ours.
VOISR_SIGNATURE = 0x564F4953  # 'VOIS'

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56

MODIFIER_KEYS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)

TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    # The padding MUST be 32, not sizeof(KEYBDINPUT). The real Win32 INPUT
    # union is sized by its largest member, MOUSEINPUT — so sizeof(INPUT) is
    # 40 on x64, not 32. Get this wrong and SendInput rejects every call with
    # ERROR_INVALID_PARAMETER (87) while unit tests that mock the send still
    # pass. Nothing is injected and nothing complains.
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(vk: int, flags: int) -> None:
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0,
                          dwExtraInfo=VOISR_SIGNATURE)
    if not user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)):
        raise OSError(f"SendInput failed: {ctypes.get_last_error()}")


def _key_down(vk: int) -> None:
    _send(vk, 0)


def _key_up(vk: int) -> None:
    _send(vk, KEYEVENTF_KEYUP)


def _held_modifiers() -> list[int]:
    """Modifiers physically down right now. Push-to-talk means the user is
    holding a key while we inject, and a held Alt turns Ctrl+V into something
    else entirely — AltGr+Ctrl+V on many layouts."""
    return [vk for vk in MODIFIER_KEYS
            if user32.GetAsyncKeyState(vk) & 0x8000]


def _send_ctrl_v() -> bool:
    _key_down(VK_CONTROL)
    try:
        _key_down(VK_V)
        _key_up(VK_V)
    finally:
        _key_up(VK_CONTROL)
    return True


def _process_integrity_level(pid: int) -> int | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0,
                                     ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer,
                                            size, ctypes.byref(size)):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        count = advapi32.GetSidSubAuthorityCount(sid)[0]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        return int(advapi32.GetSidSubAuthority(sid, count - 1)[0])
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def foreground_is_elevated() -> bool:
    """True when the focused window outranks us, meaning SendInput will be
    silently swallowed by UIPI. Unknown is treated as not-elevated: guessing
    'blocked' would refuse pastes that would have worked."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        theirs = _process_integrity_level(pid.value)
        ours = _process_integrity_level(kernel32.GetCurrentProcessId())
        if theirs is None or ours is None:
            return False
        return theirs > ours
    except Exception:
        return False


def _post_ctrl_v() -> bool:
    """Inject Ctrl+V. Returns False when UIPI would swallow it — SendInput
    reports success in that case, so checking beforehand is the only way to
    avoid claiming a paste that never happened."""
    if foreground_is_elevated():
        return False
    held = _held_modifiers()
    for vk in held:
        _key_up(vk)
    try:
        return bool(_send_ctrl_v())
    finally:
        for vk in held:
            _key_down(vk)


def _fallback_ctrl_v() -> bool:
    """No second injection path exists on Windows. macOS falls back from
    CGEvent to AppleScript; here a failed SendInput means blocked, full stop.
    Kept so _paste_with_adapter's signature matches the ported tests."""
    return False


def _write_text(text: str) -> None:
    """Unconditional clipboard write, no preservation. Used only on the
    no-restore path; the preserving path goes through the adapter."""
    win32 = _Win32Clipboard()
    clip = WindowsClipboard(win32=win32)
    clip._open()
    try:
        win32.empty()
        # Null terminator is mandatory — without it readers drop the last
        # character. Same requirement as pasteboard.write_text_if_unchanged.
        if not win32.set_data(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00"):
            raise ClipboardPreservationError("clipboard write failed")
    finally:
        win32.close()


def _read_text() -> str:
    win32 = _Win32Clipboard()
    clip = WindowsClipboard(win32=win32)
    clip._open()
    try:
        data = win32.get_data(CF_UNICODETEXT)
    finally:
        win32.close()
    return data.decode("utf-16-le").rstrip("\x00") if data else ""


def set_clipboard(text: str) -> None:
    _write_text(text)


def get_clipboard() -> str:
    return _read_text()


def _restore_after_failure(adapter, snapshot, owner_count, error, *,
                           message, pasted):
    try:
        adapter.restore_if_unchanged(snapshot, owner_count)
    except Exception as restore_error:
        if isinstance(error, Exception):
            raise PasteDeliveryError(
                f"{message} and clipboard restore failed: {error}; "
                f"{restore_error}",
                pasted=pasted,
            ) from error
        raise error from restore_error
    if isinstance(error, Exception):
        raise PasteDeliveryError(
            f"{message}: {error}", pasted=pasted) from error
    raise error


def _restore_after_ownership_check_failure(adapter, snapshot, owner_count,
                                           error):
    try:
        adapter.restore_if_unchanged(snapshot, owner_count)
    except BaseException as restore_error:
        if not isinstance(error, Exception):
            raise error from restore_error
        if not isinstance(restore_error, Exception):
            raise restore_error from error
        raise ClipboardPreservationError(
            "clipboard ownership check and restore failed") from error
    if isinstance(error, ClipboardPreservationError):
        raise error
    if isinstance(error, Exception):
        raise ClipboardPreservationError(
            "clipboard ownership check failed") from error
    raise error


def _paste_with_adapter(text, *, adapter, post_ctrl_v,
                        fallback_ctrl_v, sleeper) -> bool:
    snapshot = adapter.snapshot()
    owner_count = adapter.write_text_if_unchanged(text, snapshot)
    try:
        owned = adapter.is_owned(owner_count)
    except BaseException as error:
        _restore_after_ownership_check_failure(
            adapter, snapshot, owner_count, error)
    if not owned:
        try:
            adapter.restore_if_unchanged(snapshot, owner_count)
        except Exception as error:
            raise PasteDeliveryError(
                f"clipboard ownership lost and restore failed: {error}",
                pasted=False,
            ) from error
        raise PasteDeliveryError(
            "clipboard ownership lost before paste", pasted=False)
    try:
        pasted = post_ctrl_v() or fallback_ctrl_v()
    except BaseException as error:
        _restore_after_failure(
            adapter, snapshot, owner_count, error,
            message="paste command failed", pasted=False)
    if not pasted:
        try:
            adapter.restore_if_unchanged(snapshot, owner_count)
        except Exception as restore_error:
            raise PasteDeliveryError(
                f"paste was blocked and clipboard restore failed: "
                f"{restore_error}",
                pasted=False,
            ) from restore_error
        raise PasteDeliveryError(
            "paste was blocked; clipboard preserved", pasted=False)
    try:
        sleeper(PASTE_GRACE_S)
    except BaseException as error:
        _restore_after_failure(
            adapter, snapshot, owner_count, error,
            message="dictation pasted but clipboard wait failed", pasted=True)
    try:
        adapter.restore_if_unchanged(snapshot, owner_count)
    except Exception as error:
        raise PasteDeliveryError(
            f"dictation pasted but clipboard restore failed: {error}",
            pasted=True,
        ) from error
    return True


def paste_at_cursor(text: str, restore_clipboard: bool = True) -> bool:
    """Put text on the clipboard and paste it into the frontmost app.

    With restoration enabled (the default), a blocked paste raises a
    preservation-aware PasteDeliveryError. Set restore_clipboard=False to
    leave text available for a manual paste and receive False when blocked.
    """
    if not restore_clipboard:
        set_clipboard(text)
        try:
            return _post_ctrl_v() or _fallback_ctrl_v()
        except Exception:
            return False
    return _paste_with_adapter(
        text,
        adapter=WindowsClipboard(),
        post_ctrl_v=_post_ctrl_v,
        fallback_ctrl_v=_fallback_ctrl_v,
        sleeper=time.sleep,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/Scripts/python -m pytest tests/test_inject_unit.py -v`
Expected: all pass — 21 tests (16 ported + 5 new).

Note these tests mock `_send_ctrl_v`, so they pass even if the `INPUT` struct
layout is wrong and `SendInput` never fires. Step 6's real paste is the only
thing that proves injection actually works. Do not skip it.

- [ ] **Step 6: Verify a real paste**

Open Notepad, click into it, then run from a second terminal:

```bash
./venv/Scripts/python -c "
import time; time.sleep(3)
from voisr.inject import paste_at_cursor
print('pasted:', paste_at_cursor('hello from voisr'))
"
```

Click into Notepad during the 3-second delay. Expected: `hello from voisr` appears in Notepad, and your prior clipboard content still pastes correctly afterwards.

- [ ] **Step 7: Verify the UIPI path**

Open Task Manager (elevated), click it, and re-run the command above. Expected: `paste_at_cursor` raises `PasteDeliveryError` with "paste was blocked; clipboard preserved" rather than silently doing nothing.

- [ ] **Step 8: Commit**

```bash
git add voisr/inject.py tests/test_inject_unit.py
git commit -m "feat: Ctrl+V injection with UIPI detection and modifier clearing"
```

---

## Task 10: `keys.py` — Windows virtual-key table

**Files:**
- Create: `voisr/keys.py`
- Test: `tests/test_keys.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from voisr.keys import (
    DEFAULT_HOTKEY,
    hotkey_options,
    key_label,
    resolve_key,
)


def test_default_hotkey_is_right_ctrl():
    assert DEFAULT_HOTKEY == "ctrl_r"


def test_resolve_key_known_names():
    assert resolve_key("ctrl_r") == 0xA3
    assert resolve_key("ctrl_l") == 0xA2
    assert resolve_key("alt_r") == 0xA5
    assert resolve_key("shift_r") == 0xA1
    assert resolve_key("f13") == 0x7C


def test_resolve_key_is_case_insensitive():
    assert resolve_key("CTRL_R") == 0xA3


def test_resolve_key_unknown_returns_none():
    assert resolve_key("nope") is None


def test_key_label_round_trips():
    assert key_label(0xA3) == "Right Ctrl"
    assert key_label(0xA5) == "Right Alt"


def test_key_label_unknown_reports_hex_code():
    assert key_label(0x9F) == "VK 0x9F"


def test_hotkey_options_are_name_label_pairs():
    options = hotkey_options()
    assert {"name": "ctrl_r", "label": "Right Ctrl"} in options
    assert all(set(o) == {"name", "label"} for o in options)


def test_every_option_resolves():
    for option in hotkey_options():
        assert resolve_key(option["name"]) is not None


def test_labels_are_unique():
    labels = [o["label"] for o in hotkey_options()]
    assert len(labels) == len(set(labels))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python -m pytest tests/test_keys.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.keys'`

- [ ] **Step 3: Write the implementation**

```python
"""Windows virtual-key codes for the push-to-talk hotkey.

Right Ctrl is the default rather than macOS's right Alt: on many keyboard
layouts right Alt is AltGr, and holding it while we inject Ctrl+V produces a
different chord entirely.
"""

DEFAULT_HOTKEY = "ctrl_r"

# name -> (vk, label). Only keys that are safe to hold down while typing
# continues elsewhere, and that no common app claims.
KEYS = {
    "ctrl_r":  (0xA3, "Right Ctrl"),
    "ctrl_l":  (0xA2, "Left Ctrl"),
    "alt_r":   (0xA5, "Right Alt"),
    "alt_l":   (0xA4, "Left Alt"),
    "shift_r": (0xA1, "Right Shift"),
    "shift_l": (0xA0, "Left Shift"),
    "win_r":   (0x5C, "Right Win"),
    "caps":    (0x14, "Caps Lock"),
    "f13":     (0x7C, "F13"),
    "f14":     (0x7D, "F14"),
    "f15":     (0x7E, "F15"),
    "scroll":  (0x91, "Scroll Lock"),
    "pause":   (0x13, "Pause"),
}

_LABELS = {vk: label for vk, label in KEYS.values()}


def resolve_key(name: str):
    """Virtual-key code for a config hotkey name, or None if unknown."""
    entry = KEYS.get((name or "").lower())
    return entry[0] if entry else None


def key_label(vk: int) -> str:
    """Human-readable name for a virtual-key code."""
    return _LABELS.get(vk, f"VK 0x{vk:02X}")


def hotkey_options() -> list[dict]:
    """Selectable hotkeys for the settings UI, in declaration order."""
    return [{"name": name, "label": label} for name, (_, label) in KEYS.items()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python -m pytest tests/test_keys.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add voisr/keys.py tests/test_keys.py
git commit -m "feat: Windows virtual-key table with right Ctrl default"
```

---

## Task 11: `keytap.py` — low-level keyboard hook

**Files:**
- Create: `voisr/keytap.py`
- Test: `tests/test_keytap.py`

- [ ] **Step 1: Write the failing tests**

```python
import ctypes

import pytest

import voisr.keytap as keytap
from voisr.keytap import WindowsKeyListener


class FakeHookStruct:
    def __init__(self, vk, extra=0):
        self.vkCode = vk
        self.dwExtraInfo = extra


def install(monkeypatch, listener, struct):
    """Drive the listener's hook callback directly, without a real hook."""
    monkeypatch.setattr(keytap, "_struct_from_lparam", lambda lparam: struct)
    monkeypatch.setattr(keytap.user32, "CallNextHookEx",
                        lambda *args: 0, raising=False)
    return listener._callback


def test_keydown_is_reported(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(0, keytap.WM_KEYDOWN, 0)
    assert seen == [(0xA3, "down")]


def test_keyup_is_reported(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(0, keytap.WM_KEYUP, 0)
    assert seen == [(0xA3, "up")]


def test_syskeydown_is_reported_as_down(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA5))
    cb(0, keytap.WM_SYSKEYDOWN, 0)
    assert seen == [(0xA5, "down")]


def test_our_own_injected_events_are_ignored(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    struct = FakeHookStruct(0x56, extra=keytap.VOISR_SIGNATURE)
    cb = install(monkeypatch, listener, struct)
    cb(0, keytap.WM_KEYDOWN, 0)
    assert seen == []


def test_other_apps_injected_events_are_not_ignored(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0x56, extra=0xDEAD))
    cb(0, keytap.WM_KEYDOWN, 0)
    assert seen == [(0x56, "down")]


def test_negative_ncode_is_passed_through_untouched(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(-1, keytap.WM_KEYDOWN, 0)
    assert seen == []


def test_handler_exception_never_escapes_the_callback(monkeypatch):
    def explode(vk, kind):
        raise RuntimeError("handler bug")

    listener = WindowsKeyListener(explode)
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(0, keytap.WM_KEYDOWN, 0)  # must not raise — a raise kills the hook


def test_unknown_message_is_ignored(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(0, 0x9999, 0)
    assert seen == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python -m pytest tests/test_keytap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.keytap'`

- [ ] **Step 3: Write the implementation**

```python
"""Listen-only global keyboard hook.

WH_KEYBOARD_LL delivers every key event system-wide. The hook must be
installed on the thread that pumps messages, so the listener owns a thread
running its own GetMessage loop.

The callback does as little as possible: Windows silently unregisters a
low-level hook whose callback exceeds LowLevelHooksTimeout (300 ms by
default), so all it does is record a virtual key and return.
"""
import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# Must match voisr.inject.VOISR_SIGNATURE — that is how we tell our own
# synthetic Ctrl+V apart from a real keypress.
VOISR_SIGNATURE = 0x564F4953

_DOWN_MESSAGES = (WM_KEYDOWN, WM_SYSKEYDOWN)
_UP_MESSAGES = (WM_KEYUP, WM_SYSKEYUP)

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# These argtypes are MANDATORY. Without them ctypes defaults integer
# arguments to 32-bit c_int, and lparam is pointer-sized — so every real
# keystroke raises "OverflowError: int too long to convert" inside the
# callback, where ctypes silently swallows it. Unit tests mock
# CallNextHookEx and pass anyway; only a real hook exposes this.
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


def _struct_from_lparam(lparam):
    return ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents


class WindowsKeyListener:
    """Calls handler(vk, kind) for every key event.

    kind: "down" | "up". Unlike the macOS Quartz tap there is no separate
    "flags" kind — Windows reports modifiers as ordinary key events.
    """

    def __init__(self, handler):
        self._handler = handler
        self._thread = None
        self._hook = None
        self._thread_id = None
        self.running = False

    def _callback(self, ncode, wparam, lparam):
        if ncode < 0:
            return user32.CallNextHookEx(None, ncode, wparam, lparam)
        try:
            if wparam in _DOWN_MESSAGES:
                kind = "down"
            elif wparam in _UP_MESSAGES:
                kind = "up"
            else:
                kind = None
            if kind is not None:
                data = _struct_from_lparam(lparam)
                if data.dwExtraInfo != VOISR_SIGNATURE:
                    self._handler(int(data.vkCode), kind)
        except Exception:
            pass  # a callback error must never take down the hook
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._proc = HOOKPROC(self._callback)  # kept alive on self
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            return  # caller retries; nothing usable without a hook
        self.running = True
        try:
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self.running = False
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def stop(self) -> None:
        thread_id, self._thread_id = self._thread_id, None
        if thread_id is not None:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)


def capture_next_vk(timeout: float = 8.0):
    """Block until any key goes down; return its virtual-key code or None."""
    result = {}
    got = threading.Event()

    def handler(vk, kind):
        if kind == "down" and "vk" not in result:
            result["vk"] = vk
            got.set()

    listener = WindowsKeyListener(handler)
    listener.start()
    got.wait(timeout)
    listener.stop()
    return result.get("vk")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python -m pytest tests/test_keytap.py -v`
Expected: 8 passed

- [ ] **Step 5: Verify the hook against real keystrokes**

```bash
./venv/Scripts/python -c "
from voisr.keytap import capture_next_vk
from voisr.keys import key_label
vk = capture_next_vk()
print('captured:', hex(vk), key_label(vk))
"
```

Press right Ctrl. Expected: `captured: 0xa3 Right Ctrl`.

- [ ] **Step 6: Commit**

```bash
git add voisr/keytap.py tests/test_keytap.py
git commit -m "feat: WH_KEYBOARD_LL listener ignoring our own injected events"
```

---

## Task 12: `overlay.py` — the floating pill

**Prerequisite:** Task 2 passed all four checks. If it did not, stop — this task assumes the pywebview approach.

**Files:**
- Create: `voisr/overlay.py`
- Copy: `voisr/web/widget.html` and `voisr/web/css/` from `$MAC`
- Test: `tests/test_overlay.py`
- Delete: `scripts/spike_overlay.py`

- [ ] **Step 1: Copy the pill's web assets**

```bash
mkdir -p voisr/web/css
cp "$MAC/voisr/web/widget.html" voisr/web/widget.html
cp "$MAC/voisr/web/css/tokens.css" voisr/web/css/tokens.css
```

- [ ] **Step 2: Write the failing tests**

State transitions are testable without a window; only `_apply_styles` needs Win32.

```python
import voisr.overlay as overlay
from voisr.overlay import RecordingWidget


class FakeWindow:
    def __init__(self):
        self.evaluated = []

    def evaluate_js(self, script):
        self.evaluated.append(script)


def widget_with_window():
    widget = RecordingWidget()
    widget._window = FakeWindow()
    return widget


def test_show_recording_sets_state_and_theme():
    widget = widget_with_window()
    widget.show_recording(theme="classic", color="#ff0000")
    script = widget._window.evaluated[-1]
    assert "recording" in script
    assert "classic" in script
    assert "#ff0000" in script


def test_show_processing_sets_state():
    widget = widget_with_window()
    widget.show_processing()
    assert "processing" in widget._window.evaluated[-1]


def test_show_error_sets_state():
    widget = widget_with_window()
    widget.show_error()
    assert "error" in widget._window.evaluated[-1]


def test_hide_sets_hidden_state():
    widget = widget_with_window()
    widget.hide()
    assert "hidden" in widget._window.evaluated[-1]


def test_calls_before_create_are_dropped_not_raised():
    RecordingWidget().show_recording()  # must not raise


def test_epoch_guard_drops_stale_updates():
    widget = widget_with_window()
    widget.show_recording()
    before = len(widget._window.evaluated)
    widget.show_done(is_current=lambda: False)
    assert len(widget._window.evaluated) == before


def test_epoch_guard_allows_current_updates():
    widget = widget_with_window()
    widget.show_recording()
    before = len(widget._window.evaluated)
    widget.show_done(is_current=lambda: True)
    assert len(widget._window.evaluated) == before + 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./venv/Scripts/python -m pytest tests/test_overlay.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.overlay'`

- [ ] **Step 4: Write the implementation**

```python
"""Floating dictation pill.

A frameless, transparent, always-on-top pywebview window rendering
web/widget.html, with Win32 extended styles applied to its HWND so it never
takes focus and never eats a click. Focus theft would be fatal: the paste
targets whatever window is frontmost, so the pill must never be frontmost.
"""
import ctypes
import json
import threading
import time
from ctypes import wintypes
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]

WINDOW_TITLE = "Voisr Recording"
WIDTH, HEIGHT = 232, 52
BOTTOM_MARGIN = 72


def _apply_styles(hwnd) -> None:
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


class RecordingWidget:
    """Same interface as the macOS RecordingWidget: create/show_*/hide."""

    def __init__(self):
        self._window = None

    def create(self) -> None:
        """Build the window and patch its styles. Call from the thread that
        runs webview.start(); patching happens once the HWND exists."""
        import webview

        self._window = webview.create_window(
            WINDOW_TITLE, str(WEB_DIR / "widget.html"),
            width=WIDTH, height=HEIGHT,
            frameless=True, on_top=True, transparent=True,
            resizable=False, focus=False,
        )
        threading.Thread(target=self._patch_when_ready, daemon=True).start()

    def _patch_when_ready(self) -> None:
        for _ in range(50):
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                _apply_styles(hwnd)
                return
            time.sleep(0.1)

    def _set(self, state: str, theme: str | None = None,
             color: str | None = None, is_current=None, if_current=None) -> None:
        if self._window is None:
            return  # not created yet; a missing pill must never break dictation
        if is_current is not None and not is_current():
            return  # a newer dictation already owns the pill
        payload = json.dumps({"state": state, "theme": theme, "color": color})
        try:
            self._window.evaluate_js(f"window.voisrSetState({payload})")
        except Exception:
            pass  # cosmetic: never let the pill take down a dictation

    def show_recording(self, theme: str = "classic", color: str | None = None,
                       is_current=None, if_current=None) -> None:
        self._set("recording", theme, color, is_current, if_current)

    def show_processing(self, is_current=None, if_current=None) -> None:
        self._set("processing", is_current=is_current, if_current=if_current)

    def show_done(self, is_current=None, if_current=None) -> None:
        self._set("done", is_current=is_current, if_current=if_current)

    def show_error(self, is_current=None, if_current=None) -> None:
        self._set("error", is_current=is_current, if_current=if_current)

    def hide(self, is_current=None, if_current=None) -> None:
        self._set("hidden", is_current=is_current, if_current=if_current)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/Scripts/python -m pytest tests/test_overlay.py -v`
Expected: 7 passed

- [ ] **Step 6: Check widget.html exposes the state hook**

Run: `grep -n "voisrSetState" voisr/web/widget.html`

If absent, the macOS version is driven differently. Add this before `</body>`, adapting the class names to the ones already in the file:

```html
<script>
  window.voisrSetState = ({state, theme, color}) => {
    const pill = document.querySelector(".pill");
    pill.dataset.state = state;
    if (theme) pill.dataset.theme = theme;
    if (color) pill.style.setProperty("--pill-color", color);
  };
</script>
```

- [ ] **Step 7: Delete the spike**

```bash
git rm scripts/spike_overlay.py
```

- [ ] **Step 8: Commit**

```bash
git add voisr/overlay.py voisr/web tests/test_overlay.py
git commit -m "feat: floating pill as click-through non-focusing overlay"
```

---

## Task 13: `app.py` — wire the dictation loop

**Files:**
- Create: `voisr/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

import voisr.app as app
from voisr.app import DictationSession, load_config
from voisr.pasteboard import PasteDeliveryError


def test_load_config_uses_defaults_when_absent(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["hotkey"] == "ctrl_r"
    assert cfg["paste"] is True


def test_load_config_merges_over_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"hotkey": "f13"}')
    cfg = load_config(path)
    assert cfg["hotkey"] == "f13"
    assert cfg["paste"] is True


def test_load_config_survives_corrupt_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    assert load_config(path)["hotkey"] == "ctrl_r"


class FakeRecorder:
    def __init__(self):
        self.started = self.stopped = False

    def start(self, path):
        self.started = True

    def stop(self):
        self.stopped = True
        return ""

    def cancel(self):
        pass


def session(monkeypatch, transcribe=None, deliver=None):
    recorder = FakeRecorder()
    monkeypatch.setattr(app, "transcribe", transcribe or (lambda wav, **kw: "raw text"))
    delivered = []
    monkeypatch.setattr(app, "paste_at_cursor",
                        deliver or (lambda text, **kw: delivered.append(text) or True))
    return DictationSession(recorder=recorder, config={"paste": True, "dictionary": {}}), delivered


def test_press_starts_the_recorder(monkeypatch, tmp_path):
    s, _ = session(monkeypatch)
    s.press(tmp_path / "a.wav")
    assert s.recorder.started


def test_release_transcribes_cleans_and_delivers(monkeypatch, tmp_path):
    s, delivered = session(monkeypatch, transcribe=lambda wav, **kw: "um hello world")
    s.press(tmp_path / "a.wav")
    s.release()
    assert delivered == ["Hello world"]


def test_release_without_press_is_a_noop(monkeypatch, tmp_path):
    s, delivered = session(monkeypatch)
    s.release()
    assert delivered == []


def test_empty_transcript_is_not_delivered(monkeypatch, tmp_path):
    s, delivered = session(monkeypatch, transcribe=lambda wav, **kw: "   ")
    s.press(tmp_path / "a.wav")
    s.release()
    assert delivered == []


def test_blocked_paste_is_reported_not_swallowed(monkeypatch, tmp_path):
    def blocked(text, **kw):
        raise PasteDeliveryError("paste was blocked; clipboard preserved")

    s, _ = session(monkeypatch, deliver=blocked)
    s.press(tmp_path / "a.wav")
    result = s.release()
    assert result.blocked is True
    assert result.text == "Raw text"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python -m pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.app'`

- [ ] **Step 3: Write the implementation**

```python
"""Push-to-talk dictation loop.

Transcription runs off the hook thread: a low-level keyboard hook whose
callback blocks gets silently unregistered by Windows, and whisper takes
seconds.
"""
import argparse
import json
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from voisr.cleanup import clean
from voisr.inject import paste_at_cursor, set_clipboard
from voisr.keys import DEFAULT_HOTKEY, key_label, resolve_key
from voisr.keytap import WindowsKeyListener
from voisr.pasteboard import PasteDeliveryError
from voisr.paths import ensure_app_data_dir
from voisr.stream_recorder import StreamRecorder
from voisr.transcriber import transcribe

DEFAULT_CONFIG = {
    "hotkey": DEFAULT_HOTKEY,
    "paste": True,
    "dictionary": {},
    "model": None,
}


def config_path() -> Path:
    return ensure_app_data_dir() / "config.json"


def load_config(path=None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path) if path else config_path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            print(f"voisr: ignoring unreadable config {p}: {e}", flush=True)
    return cfg


@dataclass
class DictationResult:
    text: str
    delivered: bool
    blocked: bool = False


class DictationSession:
    """One press/release cycle. Owns the temp WAV and its cleanup."""

    def __init__(self, recorder=None, config=None):
        self.recorder = recorder or StreamRecorder()
        self.config = config or DEFAULT_CONFIG
        self._wav = None

    def press(self, wav_path=None) -> None:
        if self._wav is not None:
            return  # already recording; auto-repeat sends many keydowns
        if wav_path is None:
            fd, name = tempfile.mkstemp(suffix=".wav")
            import os
            os.close(fd)
            wav_path = Path(name)
        self._wav = Path(wav_path)
        self.recorder.start(self._wav)

    def release(self) -> DictationResult | None:
        wav, self._wav = self._wav, None
        if wav is None:
            return None
        try:
            self.recorder.stop()
            prompt = ", ".join(self.config.get("dictionary") or {}) or None
            text = clean(
                transcribe(wav, model_path=None, prompt=prompt),
                dictionary=self.config.get("dictionary"),
            )
            if not text.strip():
                return DictationResult("", delivered=False)
            if not self.config.get("paste", True):
                set_clipboard(text)
                return DictationResult(text, delivered=False)
            try:
                paste_at_cursor(text)
            except PasteDeliveryError as error:
                return DictationResult(text, delivered=bool(error.pasted),
                                       blocked=not error.pasted)
            return DictationResult(text, delivered=True)
        finally:
            wav.unlink(missing_ok=True)


def hotkey_mode(cfg) -> None:
    target = resolve_key(cfg["hotkey"])
    if target is None:
        raise SystemExit(f"unknown hotkey: {cfg['hotkey']}")
    session = DictationSession(config=cfg)
    lock = threading.Lock()

    def finish():
        with lock:
            result = session.release()
        if result is None:
            return
        if result.blocked:
            print(f"blocked (clipboard preserved): {result.text}")
        elif result.delivered:
            print(f"pasted: {result.text}")
        elif result.text:
            print(f"copied: {result.text}")
        else:
            print("(nothing transcribed)")

    def handler(vk, kind):
        if vk != target:
            return
        if kind == "down":
            with lock:
                session.press()
        else:
            # off the hook thread: a slow callback gets the hook unregistered
            threading.Thread(target=finish, daemon=True).start()

    listener = WindowsKeyListener(handler)
    listener.start()
    print(f"Hold [{key_label(target)}] to dictate. Ctrl-C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="voisr: on-device push-to-talk dictation")
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-paste", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.no_paste:
        cfg["paste"] = False
    hotkey_mode(cfg)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python -m pytest tests/test_app.py -v`
Expected: 9 passed

- [ ] **Step 5: Run the whole suite**

Run: `./venv/Scripts/python -m pytest -v`
Expected: everything passes. Fix any regression before continuing.

- [ ] **Step 6: THE PHASE 1 ACCEPTANCE TEST**

```bash
./venv/Scripts/python -m voisr.app
```

Open Notepad, click into it, hold **right Ctrl**, say "testing one two three", release.

Expected: `Testing one two three.` appears at the cursor within a couple of seconds, and the console prints `pasted: Testing one two three.`

Then verify clipboard preservation: copy something else, dictate again, and confirm Ctrl+V still pastes your original content.

- [ ] **Step 7: Commit**

```bash
git add voisr/app.py tests/test_app.py
git commit -m "feat: wire push-to-talk dictation loop end to end"
```

---

## Task 14: Windows conftest for speech synthesis

This unblocks the ported end-to-end tests in the later plans, and proves the SAPI substitution works while the pipeline is fresh.

**Files:**
- Create: `tests/conftest.py`
- Create: `voisr/audio.py`
- Test: `tests/test_e2e.py`

- [ ] **Step 1: Write `voisr/audio.py`**

```python
import subprocess
from pathlib import Path

from voisr.paths import find_binary

CREATE_NO_WINDOW = 0x08000000


def to_wav16k(src, dst) -> Path:
    """Convert any audio file to 16 kHz mono PCM WAV (whisper.cpp's format)."""
    subprocess.run(
        [find_binary("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error",
         "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
         str(dst)],
        check=True, creationflags=CREATE_NO_WINDOW)
    return Path(dst)
```

- [ ] **Step 2: Write `tests/conftest.py`**

```python
import subprocess

import pytest

from voisr.audio import to_wav16k

# SAPI writes 16-bit PCM WAV directly, so ffmpeg only has to resample.
_SAPI = """
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToWaveFile("{path}")
$s.Speak("{text}")
$s.Dispose()
"""


@pytest.fixture
def spoken_wav(tmp_path):
    def make(text: str):
        raw = tmp_path / "speech_raw.wav"
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _SAPI.format(path=str(raw), text=text.replace('"', ""))],
            check=True, capture_output=True)
        return to_wav16k(raw, tmp_path / "speech.wav")

    return make
```

- [ ] **Step 3: Write the end-to-end test**

```python
import pytest

from voisr.cleanup import clean
from voisr.transcriber import transcribe


@pytest.mark.slow
def test_speech_round_trips_to_text(spoken_wav):
    wav = spoken_wav("testing one two three")
    result = clean(transcribe(wav)).lower()
    assert "testing" in result
    assert "three" in result


@pytest.mark.slow
def test_dictionary_is_applied_to_transcript(spoken_wav):
    wav = spoken_wav("open trade mate now")
    result = clean(transcribe(wav), dictionary={"trade mate": "TradeMate"})
    assert "TradeMate" in result
```

- [ ] **Step 4: Register the marker**

Add to `pytest.ini` under `[pytest]`:

```ini
markers =
    slow: needs a real model and SAPI; excluded with -m "not slow"
```

- [ ] **Step 5: Check ffmpeg is available**

Run: `./venv/Scripts/python -c "from voisr.paths import find_binary; print(find_binary('ffmpeg'))"`

If it prints `None`, install it: `winget install Gyan.FFmpeg` and reopen the terminal.

- [ ] **Step 6: Run the end-to-end tests**

Run: `./venv/Scripts/python -m pytest tests/test_e2e.py -v`
Expected: 2 passed. This proves speech → text works without a live microphone.

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_e2e.py voisr/audio.py pytest.ini
git commit -m "test: SAPI speech synthesis replacing macOS say for e2e tests"
```

---

## Done criteria

- [ ] `./venv/Scripts/python -m pytest -v` — all green
- [ ] Holding right Ctrl in Notepad dictates text at the cursor
- [ ] The clipboard survives a dictation intact, including rich content copied from Word
- [ ] Dictating while Task Manager is focused reports blocked instead of failing silently
- [ ] The pill appears while recording, over a fullscreen app, without stealing focus or eating clicks

## Follow-on plans

| Plan | Phase | Depends on |
|---|---|---|
| `history-durability.md` | 2 | This plan |
| `dashboard.md` | 3 | Phase 2 |
| `tray-autostart-chrome.md` | 4 | Phase 3 |
| `installer.md` | 5 | Phase 4 |
