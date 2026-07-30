# Voisr Windows — Tray Icon and Installer Implementation Plan (Phase 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Voisr reachable from the system tray without its window open, and installable so it launches from the Start Menu instead of a terminal.

**Architecture:** A `pystray` tray icon backed by a Pillow-drawn waveform glyph, wired into the existing `HotkeyService` session bookkeeping so it shows when a dictation is in flight. Installation copies the runtime to `%LOCALAPPDATA%\Voisr\app`, builds a venv there, and creates Start Menu and Desktop shortcuts — the direct analogue of the macOS `scripts/install_app.sh`.

**Tech Stack:** pystray 0.19.5, Pillow 12.3.0 (both already installed), PowerShell, WScript.Shell for shortcut creation.

**Depends on:** Phases 0–3, complete. 357 Python tests + 133 node tests passing.

**macOS reference repo, `$MAC`:** clone `andr26j/voisr-mac`. Key files: `$MAC/voisr/status_item.py`, `$MAC/scripts/install_app.sh`, `$MAC/assets/icon_1024.png`.

---

## Scope note on "installer"

The macOS app does not ship a signed `.pkg`. It ships `install_app.sh`, which copies the runtime to `~/Library/Application Support/Voisr`, creates a venv, and writes a `Voisr.app` launcher bundle. This plan is the direct Windows analogue.

**Out of scope:** a distributable `.exe` that another person could run without Python. That needs PyInstaller (which has known friction with `sounddevice`'s PortAudio DLL, `pythonnet`/CLR, and WebView2) plus Inno Setup, and would have to bundle or download a 141 MB model. It is a real project of its own. If it is wanted later, it gets its own plan.

---

## File Structure

| File | Responsibility |
|---|---|
| `assets/icon_1024.png` | App icon source, ported from macOS |
| `assets/voisr.ico` | Multi-resolution Windows icon, generated |
| `scripts/make_icon.py` | PNG → ICO conversion |
| `voisr/status_item.py` | Tray icon and menu |
| `voisr/ui.py` | Restore the tray wiring deliberately omitted in Phase 3 |
| `scripts/install.ps1` | Install runtime + shortcuts |
| `scripts/uninstall.ps1` | Remove shortcuts, runtime, and the Run key |
| `README.md` | Setup, usage, uninstall |

---

## Task 1: App icon

**Files:** Create `assets/icon_1024.png` (copy), `scripts/make_icon.py`, `assets/voisr.ico`

- [ ] **Step 1: Copy the source icon**

```bash
mkdir -p assets
cp "$MAC/assets/icon_1024.png" assets/icon_1024.png
cp "$MAC/assets/logo.svg" assets/logo.svg
```

Do NOT copy `Voisr.icns` — it is a macOS format with no use here.

- [ ] **Step 2: Write `scripts/make_icon.py`**

Convert the PNG to a multi-resolution `.ico`. Windows picks different sizes for the taskbar, Start Menu, Alt-Tab and file listings, so a single-size icon looks wrong somewhere. Include at minimum 16, 24, 32, 48, 64, 128, 256.

Pillow's `Image.save(..., format="ICO", sizes=[...])` does this in one call.

- [ ] **Step 3: Generate and verify**

Run it, then verify the result actually contains every size — do not assume the save honoured the list:

```bash
./venv/Scripts/python -c "
from PIL import Image
im = Image.open('assets/voisr.ico')
print(sorted(im.info.get('sizes', [])))
"
```

Report the actual sizes found. Anything missing is a real failure.

- [ ] **Step 4: Commit**

```bash
git add assets scripts/make_icon.py
git commit -m "feat: Windows app icon generated from the macOS source art"
```

`.gitignore` currently ignores `bin/` and `models/` but not `assets/` — confirm the `.ico` is actually tracked.

---

## Task 2: `status_item.py` — the tray icon

**Files:** Create `voisr/status_item.py`, `tests/test_status_item.py`

Read `$MAC/voisr/status_item.py` first. Its structure is worth keeping: a `_dispatch(action)` method routes every menu action through one testable entry point, so menu routing can be tested without a real tray. Keep that. Replace the AppKit rendering and the main-thread dispatch — pystray owns its own thread.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from voisr.status_item import StatusItem, glyph_image


def make(**overrides):
    calls = []
    kwargs = dict(
        on_dictate=lambda: calls.append("dictate"),
        on_open=lambda: calls.append("open"),
        on_quit=lambda: calls.append("quit"),
        hotkey_label_fn=lambda: "Right Ctrl",
    )
    kwargs.update(overrides)
    return StatusItem(**kwargs), calls


def test_glyph_renders_at_the_requested_size():
    assert glyph_image(recording=False, size=32).size == (32, 32)


def test_recording_glyph_differs_from_idle():
    idle = glyph_image(recording=False, size=32).tobytes()
    rec = glyph_image(recording=True, size=32).tobytes()
    assert idle != rec


def test_glyph_adapts_to_taskbar_theme():
    light = glyph_image(recording=False, size=32, dark_taskbar=False).tobytes()
    dark = glyph_image(recording=False, size=32, dark_taskbar=True).tobytes()
    assert light != dark


def test_dispatch_routes_each_action():
    item, calls = make()
    item._dispatch("dictate")
    item._dispatch("open")
    item._dispatch("quit")
    assert calls == ["dictate", "open", "quit"]


def test_dispatch_ignores_unknown_action():
    item, calls = make()
    item._dispatch("nonsense")
    assert calls == []


def test_handler_exception_never_escapes():
    def explode():
        raise RuntimeError("handler bug")

    item, _ = make(on_dictate=explode)
    item._dispatch("dictate")  # must not raise


def test_menu_shows_the_current_hotkey():
    item, _ = make(hotkey_label_fn=lambda: "F13")
    assert "F13" in item._hotkey_menu_text()


def test_menu_survives_a_failing_hotkey_lookup():
    def explode():
        raise RuntimeError("no config")

    item, _ = make(hotkey_label_fn=explode)
    assert item._hotkey_menu_text()  # some text, no exception


def test_set_recording_before_create_is_a_noop():
    item, _ = make()
    item.set_recording(True)  # must not raise


def test_release_before_create_is_a_noop():
    item, _ = make()
    item.release()


def test_set_recording_updates_the_icon():
    item, _ = make()

    class FakeIcon:
        def __init__(self):
            self.icon = None

    item._icon = FakeIcon()
    item.set_recording(True)
    assert item._icon.icon is not None
```

- [ ] **Step 2:** Run them, observe `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`glyph_image(recording, size=32, dark_taskbar=None)` draws the three-bar waveform with Pillow — filled/taller bars when recording, matching the macOS design (`$MAC/voisr/status_item.py` `_glyph_image`).

**macOS template images auto-invert to match the menu bar; Windows tray icons do not.** So pick the bar colour from the taskbar theme, read from:

`HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize` → `SystemUsesLightTheme`

`dark_taskbar=None` means "detect it"; the explicit argument exists so tests are deterministic. A failed registry read must fall back to light-on-dark (the Windows default), not raise.

`StatusItem` keeps the macOS interface: `create()`, `set_recording(bool)`, `release()`, and `_dispatch(action)`. Menu items: **Dictate now**, **Open Dashboard**, a disabled **Push-to-talk: <key>** row, a separator, **Quit Voisr**.

pystray runs its own thread and marshals updates internally, so there is no equivalent of the macOS `_on_main_thread` dance — delete it. Keep every `try/except`: a tray failure must never take down the app, which is explicit in the macOS code.

`create()` must not block. `pystray.Icon.run()` blocks, so start it on a daemon thread, or use `run_detached()` if it proves reliable — report which you chose and why.

- [ ] **Step 4:** Run the tests. Expected: 11 passed.

- [ ] **Step 5: Verify a real tray icon**

Temporary script (delete after; commit nothing): create a `StatusItem`, show it, toggle `set_recording(True)` then `False`, then `release()`.

Verify what you can programmatically — that `create()` returns without blocking, that the pystray icon object reports visible, that `set_recording` swaps the image without raising, and that `release()` removes it cleanly and the thread exits.

You cannot see the taskbar. Do not claim the icon looked right. Report what you measured, and note that visual confirmation needs a human.

- [ ] **Step 6: Commit**

```bash
git add voisr/status_item.py tests/test_status_item.py
git commit -m "feat: system tray icon with recording state"
```

---

## Task 3: Wire the tray into the app

Phase 3 deliberately omitted this. `voisr/ui.py` has a comment at lines 13–14 saying the glyph bookkeeping (`_mark_session`, `_sessions_in_flight`, `attach_status_item`) was left out because no tray existed. It exists now.

**Files:** Modify `voisr/ui.py`, `tests/test_ui_api.py`

- [ ] **Step 1: Read the macOS original**

`$MAC/voisr/ui.py` — `Api._mark_session` (~line 799), `attach_status_item` (~788), and the `_notify_dictation` path. Note that `_mark_session` only pushes on a *change*, so the icon is not redrawn on every event.

- [ ] **Step 2: Write the failing tests**

```python
class FakeStatus:
    def __init__(self):
        self.states = []

    def set_recording(self, value):
        self.states.append(bool(value))

    def release(self):
        self.states.append("released")


def test_status_lights_while_a_dictation_is_in_flight(api_with_status):
    api, status = api_with_status
    api._mark_session(1, in_flight=True)
    assert status.states == [True]


def test_status_clears_when_the_last_session_finishes(api_with_status):
    api, status = api_with_status
    api._mark_session(1, in_flight=True)
    api._mark_session(1, in_flight=False)
    assert status.states == [True, False]


def test_overlapping_dictations_keep_the_status_lit(api_with_status):
    """Two queued dictations must not clear the icon when the first ends."""
    api, status = api_with_status
    api._mark_session(1, in_flight=True)
    api._mark_session(2, in_flight=True)
    api._mark_session(1, in_flight=False)
    assert status.states == [True]
    api._mark_session(2, in_flight=False)
    assert status.states == [True, False]


def test_status_is_only_pushed_on_change(api_with_status):
    api, status = api_with_status
    api._mark_session(1, in_flight=True)
    api._mark_session(2, in_flight=True)
    assert status.states == [True]


def test_status_failure_never_breaks_a_dictation(api_with_status_failing):
    api, _ = api_with_status_failing
    api._mark_session(1, in_flight=True)  # must not raise


def test_api_without_a_status_item_works(api):
    api._mark_session(1, in_flight=True)  # must not raise
```

Write the fixtures against `Api`'s real constructor.

- [ ] **Step 3:** Run them, observe failure.

- [ ] **Step 4: Implement**

Restore `_mark_session`, `_sessions_in_flight` and `attach_status_item` from the macOS original, and delete the comment at `ui.py:13-14` explaining their absence.

In `main()`: build the `StatusItem`, wire `on_dictate` to the same entry point the palette uses, `on_open` to showing/restoring the window, and `on_quit` to a clean shutdown (stop the hotkey service, release the tray, close the window).

**Closing the dashboard window must not quit the app** — that is the entire point of a tray icon. Verify what pywebview does on window close and handle it explicitly.

- [ ] **Step 5:** Run the tests, then the full suite.

- [ ] **Step 6: Verify the wiring end to end**

Drive a real dictation cycle the way earlier agents did — hotkey `f13`, untagged `SendInput` with `dwExtraInfo=0`. Confirm the tray icon goes to recording state on press and back to idle after the dictation completes.

- [ ] **Step 7: Commit**

```bash
git add voisr/ui.py tests/test_ui_api.py
git commit -m "feat: tray icon reflects in-flight dictations"
```

---

## Task 4: `scripts/install.ps1`

Mirrors `$MAC/scripts/install_app.sh`. Read it first.

**Files:** Create `scripts/install.ps1`

- [ ] **Step 1: Write the installer**

Behaviour:

1. Copy the runtime to `%LOCALAPPDATA%\Voisr\app` — `voisr/`, `requirements.txt`, `assets/voisr.ico`. Exclude `__pycache__`, `tests`, `docs`, `.git`.
2. Copy `bin/` (whisper + CUDA DLLs) and `models/` if not already present at the destination. These are large; skip the copy when the destination file already exists rather than re-copying ~700 MB.
3. Create a venv at `%LOCALAPPDATA%\Voisr\app\venv` and install `requirements.txt`.
4. Create shortcuts via `WScript.Shell`:
   - `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Voisr.lnk`
   - `%USERPROFILE%\Desktop\Voisr.lnk`
   Both target the installed `pythonw.exe` with `-m voisr.ui`, `WorkingDirectory` set to the app dir, and `IconLocation` set to the `.ico`.
5. Print where it installed and how to uninstall.

**`pythonw.exe`, never `python.exe`** — otherwise every launch flashes a console window.

Config and history already live in `%LOCALAPPDATA%\Voisr\` via `paths.py`, so they sit alongside the install and survive reinstalls. Do NOT overwrite an existing `config.json`.

The installer must be safe to re-run.

- [ ] **Step 2: Run it**

```bash
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

Report the actual output.

- [ ] **Step 3: Verify the install**

- Both `.lnk` files exist
- Read each shortcut back with `WScript.Shell` and report `TargetPath`, `Arguments`, `WorkingDirectory`, `IconLocation`
- `%LOCALAPPDATA%\Voisr\app\venv\Scripts\pythonw.exe` exists
- The installed copy runs: launch it, confirm the window opens, then close it

**Launch from the installed location, not the repo.** An installer that only works because the repo happens to be present has not been tested.

- [ ] **Step 4: Verify re-running is safe**

Run it a second time. Confirm it completes, does not duplicate shortcuts, does not re-download, and does not clobber `config.json`. Report what the second run did differently.

- [ ] **Step 5: Commit**

```bash
git add scripts/install.ps1
git commit -m "feat: install to LOCALAPPDATA with Start Menu and Desktop shortcuts"
```

---

## Task 5: `scripts/uninstall.ps1`

An installer without an uninstaller is half a feature.

**Files:** Create `scripts/uninstall.ps1`

- [ ] **Step 1: Write it**

1. Remove both shortcuts
2. Remove the `Voisr` value from `HKCU\...\Run` (reuse `voisr.launch_agent.disable` logic, or delete the value directly)
3. Remove `%LOCALAPPDATA%\Voisr\app`
4. **Prompt before removing user data** (`config.json`, `history.jsonl`), defaulting to keeping it. Support `-RemoveUserData` to skip the prompt. Deleting someone's dictation history without asking is not acceptable.
5. Report what was removed and what was kept

- [ ] **Step 2: Verify the full cycle**

Uninstall, confirm shortcuts and app dir are gone and user data survived. Then re-install and confirm the app still works — that proves the uninstall did not remove something the install does not recreate.

Report both.

- [ ] **Step 3: Commit**

```bash
git add scripts/uninstall.ps1
git commit -m "feat: uninstaller preserving user data by default"
```

---

## Task 6: README

**Files:** Create `README.md`

- [ ] **Step 1: Write it**

Model it on `$MAC/README.md`, corrected for Windows reality. Cover:

- What it does — the four-step loop
- Setup: `scripts/setup.ps1` then `scripts/install.ps1`
- Usage: hold **right Ctrl**, speak, release. Dashboard from Start Menu; tray icon.
- The dashboard's four views and Ctrl+K
- Configuration: `%LOCALAPPDATA%\Voisr\config.json`, with the real Windows defaults
- Development: `venv\Scripts\python -m pytest`, `node --test tests/js/*.test.mjs` (note the glob — plain `tests/js/` fails on Node 24)
- Uninstall

**Known limitations — state these honestly:**
- Dictation does not work over elevated windows (Task Manager, admin terminals). UIPI blocks input injection from an unelevated process. The failure is reported and the clipboard preserved, not silent.
- No Mica/acrylic backdrop on Windows 10; the dashboard uses a flat background.
- English-only by default (`ggml-base.en`).
- Requires an NVIDIA GPU for the bundled cuBLAS build; CPU builds exist but are slower.

Do not claim a distributable installer exists. `install.ps1` installs from a working checkout.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README covering setup, usage, and known limitations"
```

---

## Done criteria

- [ ] Full Python suite + node suite green
- [ ] Tray icon appears, menu works, reflects recording state
- [ ] Closing the dashboard window leaves the app running in the tray
- [ ] Quit from the tray shuts down cleanly — no orphaned process, no stuck keyboard hook
- [ ] Start Menu and Desktop shortcuts launch the installed copy with no console window
- [ ] Re-running the installer is safe
- [ ] Uninstall removes the app and keeps user data unless told otherwise
- [ ] README states the real limitations

## Human verification required

The tray icon's appearance in light and dark taskbars, and whether the installed shortcuts look right in the Start Menu.

## Possible future work

A distributable `.exe` via PyInstaller + Inno Setup, for installing without a checkout or a Python install. Substantial: PortAudio, pythonnet/CLR and WebView2 all need coaxing through PyInstaller, and the 141 MB model has to be bundled or fetched on first run.
