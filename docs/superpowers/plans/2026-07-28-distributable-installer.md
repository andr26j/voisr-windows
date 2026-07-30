# Voisr Windows — Distributable Installer Implementation Plan (Phase 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single `VoisrSetup.exe` that installs Voisr on a Windows machine with no Python, no git checkout, and no developer tooling.

**Architecture:** PyInstaller freezes the app into a one-folder bundle carrying the CPU whisper build, the `base.en` model, and the web assets. Inno Setup wraps that into an installer with Start Menu and Desktop shortcuts, an uninstaller, and an optional launch-at-login step.

**Tech Stack:** PyInstaller, Inno Setup 6, whisper.cpp `whisper-blas-bin-x64`, existing runtime deps.

**Depends on:** Phases 0–4, complete. 379 Python tests + 133 node tests passing.

---

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Compute backend | **CPU/BLAS only** | Audience is employees on their own devices; most have no NVIDIA GPU. CUDA is 1,134 MB of the current 1,153 MB `bin/` — 98% — and non-functional without NVIDIA hardware. |
| Payload | **Bundled** | ~200 MB installer, works offline immediately. Comparable to Wispr Flow's 350 MB, which ships no model at all because it transcribes in the cloud. |
| Code signing | **Unsigned for now** | No certificate yet. The build must be structured so `signtool` can be added later without rework. |
| Audience | Company employees, own devices | Needs a CPU fallback, a WebView2 check, and honest first-run errors. |

**This does not change the developer install.** `scripts/install.ps1` still installs from the repo with the CUDA build. The two artifacts coexist.

---

## Known risks, stated upfront

PyInstaller has real friction with three of this app's dependencies. Each gets an explicit verification step rather than an assumption:

1. **`sounddevice`** loads PortAudio from `_sounddevice_data/portaudio-binaries/libportaudio64bit.dll` at runtime. PyInstaller does not collect it automatically. Symptom: recording fails with an opaque OSError.
2. **`pythonnet`** loads the CLR through `clr_loader`, which needs `Python.Runtime.dll` and a runtime config. Symptom: `import clr` fails, so pywebview's EdgeChromium backend never loads, so the window never opens.
3. **`pywebview`** resolves its backend dynamically, so PyInstaller's static analysis misses `webview.platforms.winforms`. Symptom: "no available GUI backend".

A fourth risk is ours, not a library's: **seven hard-coded repo-layout paths** across `voisr/paths.py`, `voisr/transcriber.py`, `voisr/ui.py` and `voisr/overlay.py`. When frozen, resources live under `sys._MEIPASS`, not `Path(__file__).parent.parent`. Task 2 fixes this.

---

## Task 1: LICENSE and third-party notices

Nothing ships without this.

**Files:** Create `LICENSE`, `THIRD_PARTY_NOTICES.md`

- [ ] **Step 1: Check what the macOS repo used**

`$MAC/LICENSE` exists. Read it and report which licence it is. Match it unless there is a reason not to.

- [ ] **Step 2: Write `LICENSE`**

Use the same licence as the macOS repo, with the correct copyright holder and year (2026).

- [ ] **Step 3: Write `THIRD_PARTY_NOTICES.md`**

Every bundled component and its licence. Verify each one rather than assuming — report the actual licence text location you checked:

- whisper.cpp (the `whisper-blas-bin-x64` binaries)
- `ggml-base.en.bin` — the model weights. Check the licence on the Hugging Face repo it is downloaded from.
- Python and the bundled stdlib
- pywebview, pythonnet, sounddevice, PortAudio, pystray, Pillow

**If any component's licence forbids redistribution, STOP and report it.** That is a blocker for a public release, not a detail.

- [ ] **Step 4: Commit**

```bash
git add LICENSE THIRD_PARTY_NOTICES.md
git commit -m "docs: licence and third-party notices for distribution"
```

---

## Task 2: Frozen-safe resource paths

**Files:** Modify `voisr/paths.py`, `voisr/transcriber.py`, `voisr/ui.py`, `voisr/overlay.py`; test `tests/test_paths.py`

- [ ] **Step 1: Write the failing tests**

```python
import sys

import voisr.paths as paths


def test_resource_root_is_repo_root_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.resource_root() == paths.REPO_ROOT


def test_resource_root_is_meipass_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_root() == tmp_path


def test_bin_dir_follows_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.bin_dir() == tmp_path / "bin"


def test_models_dir_follows_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.models_dir() == tmp_path / "models"


def test_web_dir_follows_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.web_dir() == tmp_path / "voisr" / "web"


def test_assets_dir_follows_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.assets_dir() == tmp_path / "assets"


def test_app_data_dir_is_unaffected_by_freezing(monkeypatch, tmp_path):
    """User data must NEVER move into the bundle -- it would be wiped on
    every upgrade, and _MEIPASS is a temp dir for one-file builds."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert "Voisr" in str(paths.app_data_dir())
    assert str(tmp_path) not in str(paths.app_data_dir())
```

- [ ] **Step 2:** Run them, observe failure.

- [ ] **Step 3: Implement**

Add to `voisr/paths.py`:

```python
def resource_root() -> Path:
    """Where bundled resources live: the repo when running from source,
    PyInstaller's extraction dir when frozen."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else REPO_ROOT
```

Plus `bin_dir()`, `models_dir()`, `web_dir()`, `assets_dir()` derived from it.

Then replace all seven hard-coded paths:
- `voisr/paths.py:6-7` — `BIN_DIR`, `ASSETS_DIR` → keep the names as module-level for compatibility if anything imports them, but make `find_binary` and `app_icon` call the functions
- `voisr/transcriber.py:6` — `MODELS_DIR` → `models_dir()`
- `voisr/ui.py:53` and `voisr/overlay.py:19` — `WEB_DIR` → `web_dir()`

**`app_data_dir()` must NOT change.** Config and history belong in `%LOCALAPPDATA%`, never in the bundle.

- [ ] **Step 4:** Run the full suite. Everything must still pass unfrozen.

- [ ] **Step 5: Commit**

```bash
git add voisr tests/test_paths.py
git commit -m "refactor: resolve bundled resources through a frozen-safe root"
```

---

## Task 3: CPU whisper build

**Files:** Modify `scripts/setup.ps1`; create `scripts/fetch_whisper.ps1`

- [ ] **Step 1: Extract the fetch logic**

Pull the whisper download out of `setup.ps1` into `scripts/fetch_whisper.ps1`, taking a `-Variant` parameter of `cpu` or `cuda`:

- `cpu` → `whisper-blas-bin-x64.zip`
- `cuda` → `whisper-cublas-12.4.0-bin-x64.zip`

and a `-Destination` parameter. `setup.ps1` calls it with `cuda` (unchanged developer behaviour); the distributable build calls it with `cpu`.

- [ ] **Step 2: Fetch the CPU build to a scratch dir and verify it works**

Download it, then transcribe a real WAV with it:

```bash
./venv/Scripts/python -c "
from voisr.transcriber import transcribe
print(transcribe('<a real wav>', whisper_bin=r'<scratch>\whisper-cli.exe'))
"
```

Generate the WAV with the SAPI fixture approach already used in `tests/conftest.py`.

Report the actual transcript AND the wall-clock time. The CPU build's speed is the main thing employees will notice — measure it, do not estimate it.

- [ ] **Step 3: Report the size difference**

Report total bytes for the CPU build vs the CUDA build now in `bin/`. This is the number that justifies the decision.

- [ ] **Step 4: Commit**

```bash
git add scripts/fetch_whisper.ps1 scripts/setup.ps1
git commit -m "feat: fetch either the CPU or CUDA whisper build"
```

---

## Task 4: PyInstaller spike — GATE

**This task is a gate, exactly like the overlay spike in Phase 0.** Its output is a yes/no plus a throwaway spec. Do not build the installer on top of a bundle that does not launch.

**Files:** Create `build/voisr.spec` (kept), temporary scratch as needed

- [ ] **Step 1: Install PyInstaller**

```bash
./venv/Scripts/python -m pip install pyinstaller
```

Add it to a new `requirements-build.txt`, NOT `requirements.txt` — it is a build tool, not a runtime dependency.

- [ ] **Step 2: Write a minimal spec and build**

One-folder (`--onedir`), windowed (no console), targeting `voisr/ui.py`.

Start minimal deliberately. The point is to find out which of the three known-fragile dependencies actually breaks, not to guess all the fixes up front.

- [ ] **Step 3: Run the frozen app and record exactly how it fails**

Run `dist/Voisr/Voisr.exe`. It will very likely fail. **Report the exact traceback.** Build it with a console temporarily if that is what it takes to see the error — a silent windowed failure tells you nothing.

- [ ] **Step 4: Fix each failure and record what was needed**

Expected fixes, in rough order of likelihood:

- `hiddenimports=["webview.platforms.winforms", "clr", "clr_loader", "pythonnet"]`
- `collect_data_files("sounddevice")` for the PortAudio DLL
- `collect_all("pythonnet")` / `collect_all("clr_loader")` for `Python.Runtime.dll`
- `datas` entries for `voisr/web`, `assets`, `bin`, `models`
- `collect_submodules("pystray")` — it selects a backend dynamically, like pywebview

Do not cargo-cult all of these. Add what the actual error demands, and report which were genuinely needed. A spec full of unnecessary hooks is harder to maintain than one that earns each line.

- [ ] **Step 5: The gate**

The frozen app must:
1. Launch and show the dashboard
2. Record from the microphone
3. Transcribe with the bundled CPU whisper
4. Paste into another app
5. Show the tray icon

Test each. **Report pass/fail per item with evidence.** If any cannot be made to work, STOP and report — a partially working bundle is worse than an honest blocker.

- [ ] **Step 6: Commit**

```bash
git add build/voisr.spec requirements-build.txt
git commit -m "build: PyInstaller spec producing a working frozen app"
```

---

## Task 5: WebView2 runtime check

The dashboard is a WebView2 window. The runtime ships with Windows 11 and current Windows 10, but is not guaranteed. Without it the app starts and shows nothing.

**Files:** Create `voisr/webview2.py`, `tests/test_webview2.py`

- [ ] **Step 1: Write the failing tests**

```python
from voisr.webview2 import DOWNLOAD_URL, is_runtime_installed


class FakeRegistry:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def read(self, key):
        return self.values.get(key)


def test_absent_runtime_is_detected():
    assert is_runtime_installed(registry=FakeRegistry()) is False


def test_present_runtime_is_detected():
    reg = FakeRegistry({"pv": "150.0.4078.99"})
    assert is_runtime_installed(registry=reg) is True


def test_empty_version_counts_as_absent():
    """A present key with an empty pv is how an uninstalled runtime can
    look; treating that as installed would give a blank window."""
    assert is_runtime_installed(registry=FakeRegistry({"pv": ""})) is False


def test_zero_version_counts_as_absent():
    assert is_runtime_installed(registry=FakeRegistry({"pv": "0.0.0.0"})) is False


def test_registry_failure_assumes_installed():
    """Unknown must not block launch: a false negative would refuse to
    start on a machine where the runtime is fine."""
    class Exploding:
        def read(self, key):
            raise OSError("no registry")

    assert is_runtime_installed(registry=Exploding()) is True


def test_download_url_is_the_official_evergreen_bootstrapper():
    assert "go.microsoft.com" in DOWNLOAD_URL or "developer.microsoft.com" in DOWNLOAD_URL
```

- [ ] **Step 2:** Run them, observe failure.

- [ ] **Step 3: Implement**

Check these keys (the third catches per-user installs):
```
HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}
HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}
HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}
```

`DOWNLOAD_URL` is the official Evergreen Bootstrapper link.

- [ ] **Step 4:** Run the tests, then verify against this machine — it has runtime 150.0.4078.99, so `is_runtime_installed()` must return True. Report the actual result.

- [ ] **Step 5: Commit**

```bash
git add voisr/webview2.py tests/test_webview2.py
git commit -m "feat: detect the WebView2 runtime before the window opens"
```

---

## Task 6: Inno Setup installer

**Files:** Create `build/voisr.iss`

- [ ] **Step 1: Install Inno Setup**

`winget install JRSoftware.InnoSetup`. Report the version and the path to `ISCC.exe`.

- [ ] **Step 2: Write the installer script**

Requirements:
- Installs per-user to `%LOCALAPPDATA%\Programs\Voisr` — **no admin rights**, matching the app's unelevated design (`PrivilegesRequired=lowest`)
- App name Voisr, publisher, version, `assets/voisr.ico`
- Start Menu shortcut always; Desktop shortcut as a checkbox task
- "Start Voisr when I log in" as an unchecked-by-default task writing the same `HKCU\...\Run` value `voisr/launch_agent.py` uses. **The two must not disagree** — if the installer writes a different command string than `launch_agent.enable()`, the Settings toggle and the installer will fight. Verify they match exactly.
- `AppUserModelID` set to `Voisr.Voisr`, matching what `ui.py` sets at runtime, so the taskbar and Start Menu agree
- Uninstaller removes the program dir and the Run value, and **leaves `%LOCALAPPDATA%\Voisr` user data alone**
- If `is_runtime_installed()` is false, offer to download the WebView2 bootstrapper
- `LICENSE` shown during install

- [ ] **Step 3: Build**

Report the actual output path and the installer's size in MB.

- [ ] **Step 4: Commit**

```bash
git add build/voisr.iss
git commit -m "build: Inno Setup installer, per-user and unelevated"
```

---

## Task 7: Clean-environment verification

An installer tested only on the development machine has not been tested. The repo, the venv, and a working Python are all sitting there masking failures.

**Files:** none committed

- [ ] **Step 1: Install to a clean location and prove independence**

Run the installer. Then, before launching, verify the installed tree contains everything: the exe, `bin/whisper-cli.exe`, `models/ggml-base.en.bin`, `voisr/web/index.html`, `assets/voisr.ico`.

- [ ] **Step 2: Launch with the repo hidden**

Rename the repo directory temporarily (or launch with a working directory far from it and a `PATH` scrubbed of the venv), then run the installed app.

This matters: an earlier agent on this project found that `pythonw -m voisr.ui` silently imported the *repo's* package because Python inserts the cwd into `sys.path`. A frozen build has the same class of hazard through `PYTHONPATH` and `PYTHONHOME`. Prove the running app is the installed one — check the process image path.

- [ ] **Step 3: Exercise the full loop**

On the installed app: dictate (synthesize an untagged F13 via `SendInput` as earlier agents did), confirm text pastes, confirm it lands in history, confirm the tray icon appears, confirm the taskbar shows the Voisr icon and not Python's.

Report each with evidence.

- [ ] **Step 4: Uninstall and verify**

Confirm the program directory and Run value are gone and `%LOCALAPPDATA%\Voisr` user data survived.

- [ ] **Step 5: Restore the machine**

Reinstall the developer copy from the repo so the machine is left working. Report the final state.

---

## Task 8: Build script and release docs

**Files:** Create `scripts/build_release.ps1`; modify `README.md`

- [ ] **Step 1: Write `scripts/build_release.ps1`**

One command, end to end: fetch the CPU whisper build and model into a staging dir, run PyInstaller, run ISCC, and report the output path and size.

Leave a clearly marked, commented-out `signtool` block with a note on what changes when a certificate is available. The plan is unsigned *for now*; make adding it a small edit rather than a rediscovery.

- [ ] **Step 2: Update the README**

Add a "Download" section for end users and a "Building a release" section for maintainers.

State honestly:
- The installer is **unsigned**, so Windows SmartScreen will warn on first run, with the exact click-path to proceed
- The distributable is **CPU-only**; developers running from source with `setup.ps1` get CUDA
- Typical CPU transcription time, using the number actually measured in Task 3

- [ ] **Step 3: Commit**

```bash
git add scripts/build_release.ps1 README.md
git commit -m "build: one-command release build and download docs"
```

---

## Done criteria

- [ ] `VoisrSetup.exe` installs on a machine with no Python and no checkout
- [ ] Installed app dictates, pastes, records history, shows the tray icon, and shows the Voisr taskbar icon
- [ ] Per-user install, no admin prompt
- [ ] Uninstall preserves user data
- [ ] Installer size reported and near the ~200 MB estimate
- [ ] Full test suite still green
- [ ] LICENSE and third-party notices present and verified

## Explicitly out of scope

- **Code signing.** Structured for it; no certificate yet.
- **Auto-update.** Employees reinstall manually.
- **CUDA in the distributable.** Developer install only.
- **MSI/Intune packaging.** Ask if the company needs managed deployment.

## For the company rollout

Two things need a human decision before wide distribution:

1. **SmartScreen.** Unsigned means every employee sees "Windows protected your PC" and must click "More info → Run anyway". Some corporate policies block it outright. An OV certificate (~$200–400/yr) fixes this over weeks of reputation-building; an EV certificate fixes it immediately.
2. **Privacy review.** Voisr is fully local — no audio or text leaves the machine — which is a strong story, but a company deploying a keyboard-hooking, clipboard-touching app should have that reviewed and documented. The keyboard hook in particular will be flagged by any competent security review, and the honest answer is that it is a global low-level hook, which is what push-to-talk requires.
