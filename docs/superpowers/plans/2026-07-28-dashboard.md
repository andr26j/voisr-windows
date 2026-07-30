# Voisr Windows — Dashboard Implementation Plan (Phase 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Voisr desktop app — a real window with Home, History, Dictionary and Settings, the floating pill wired to live dictations, and a Ctrl+K command palette.

**Architecture:** A pywebview window (WebView2 backend) renders the macOS app's HTML/CSS/JS essentially unchanged. Python exposes a `js_api` object; the JS calls it over the pywebview bridge. `HotkeyService` drives push-to-talk from the existing keyboard hook and feeds a `RecordingQueue` so a new dictation can start while the previous one transcribes.

**Tech Stack:** Python 3.13, pywebview 6.2.1 + pythonnet (EdgeChromium), WebView2 Runtime 150.x, node 24 for the JS suite.

**Depends on:** Phase 0–1 and Phase 2, both complete. 221 tests passing.

**macOS reference repo, `$MAC`:** clone `andr26j/voisr-mac`.

---

## What already exists

Do not rebuild these. They are done, tested, and verified against the real system:

| Module | Provides |
|---|---|
| `voisr/keys.py` | `DEFAULT_HOTKEY`, `resolve_key(name)`, `key_label(vk)`, `hotkey_options()` |
| `voisr/keytap.py` | `WindowsKeyListener(handler)`, `capture_next_vk(timeout)` |
| `voisr/overlay.py` | `RecordingWidget` — the pill, with `sanitize_pill_theme` / `sanitize_pill_color` |
| `voisr/history.py` | `HistoryStore` — `add/list/delete/restore/clear/stats` |
| `voisr/inject.py` | `paste_at_cursor`, `set_clipboard`, `foreground_is_elevated` |
| `voisr/transcriber.py` | `transcribe(wav, model_path, whisper_bin, prompt)` |
| `voisr/stream_recorder.py` | `StreamRecorder` |
| `voisr/cleanup.py` | `clean(text, dictionary)` |
| `voisr/app.py` | `DictationSession`, `load_config`, `hotkey_mode` (CLI) |

The macOS `ui.py` contains its own copies of key resolution, the pill, and permission probes. **Delete those from your port and import ours instead.** The port is ~1341 lines on macOS; expect materially less here because four of its concerns already live in dedicated modules.

---

## File Structure

| File | Responsibility |
|---|---|
| `voisr/recording_queue.py` | Ordered session queue (verbatim port) |
| `voisr/permissions.py` | Microphone probe + `ms-settings:` deep link |
| `voisr/launch_agent.py` | Launch-at-login via `HKCU\...\Run` |
| `voisr/window_chrome.py` | DWM dark titlebar; flat fallback |
| `voisr/ui.py` | `Api` (the js_api surface), `HotkeyService`, `main()` |
| `voisr/web/**` | Dashboard assets, ported with Windows labels |
| `tests/test_*.py` | Per-module Python suites |
| `tests/js/**` | Ported node suite |

`ui.py` is the largest file here. If it exceeds ~900 lines, stop and report rather than splitting on your own — the split should be a deliberate decision, not an improvisation.

---

## Task 1: `recording_queue.py` — verbatim port

**Files:** Create `voisr/recording_queue.py`, `tests/test_recording_queue.py`

- [ ] **Step 1: Copy both files byte-for-byte**

```bash
cp "$MAC/voisr/recording_queue.py" voisr/recording_queue.py
cp "$MAC/tests/test_recording_queue.py" tests/test_recording_queue.py
```

- [ ] **Step 2: Confirm no platform coupling**

Run: `grep -nE "fcntl|Quartz|AppKit|objc|osascript|pbcopy|NSW" voisr/recording_queue.py`
Expected: no output. This module is pure `threading`. If anything matches, STOP and report — the plan assumed it was portable.

- [ ] **Step 3: Run the tests**

Run: `./venv/Scripts/python -m pytest tests/test_recording_queue.py -v`
Expected: all pass unmodified.

- [ ] **Step 4: Verify it is genuinely unmodified**

Run: `diff "$MAC/voisr/recording_queue.py" voisr/recording_queue.py`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add voisr/recording_queue.py tests/test_recording_queue.py
git commit -m "feat: port ordered recording queue verbatim from macOS"
```

---

## Task 2: `permissions.py` — microphone only

macOS gates Microphone, Input Monitoring and Accessibility. Windows gates none of them for desktop apps, so two chips disappear entirely. There is no reliable programmatic probe equivalent to `AVCaptureDevice.authorizationStatus`, so the probe is an attempted stream open.

**Files:** Create `voisr/permissions.py`, `tests/test_permissions.py`

- [ ] **Step 1: Write the failing tests**

```python
import voisr.permissions as permissions
from voisr.permissions import open_pane, settings_url, states


def test_states_reports_only_microphone():
    result = states(probes={"microphone": lambda: True})
    assert [p["key"] for p in result] == ["microphone"]


def test_granted_probe_is_reported_granted():
    assert states(probes={"microphone": lambda: True})[0]["granted"] is True


def test_denied_probe_is_reported_denied():
    assert states(probes={"microphone": lambda: False})[0]["granted"] is False


def test_probe_exception_is_unknown_never_granted():
    def explode():
        raise OSError("no device")

    assert states(probes={"microphone": explode})[0]["granted"] is None


def test_states_includes_a_settings_url():
    assert states(probes={"microphone": lambda: True})[0]["settings_url"] \
        == "ms-settings:privacy-microphone"


def test_settings_url_for_unknown_key_is_none():
    assert settings_url("accessibility") is None


def test_open_pane_rejects_unknown_key():
    assert open_pane("../../evil") is False


def test_open_pane_opens_known_key():
    opened = []
    assert open_pane("microphone", opener=opened.append) is True
    assert opened == ["ms-settings:privacy-microphone"]


def test_open_pane_survives_opener_failure():
    def explode(url):
        raise OSError("no shell")

    assert open_pane("microphone", opener=explode) is False
```

- [ ] **Step 2:** Run them, observe `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Model it on `$MAC/voisr/permissions.py` — same `states()` / `settings_url()` / `open_pane()` shape, same rule that an exception yields `None` (unknown) and **never** `True`. Read the macOS docstring; the reasoning is stated there and still applies.

Windows specifics:
- `PANES = {"microphone": ("Microphone", "ms-settings:privacy-microphone")}`
- `ORDER = ["microphone"]`
- The default probe opens a brief `sounddevice` input stream and closes it, returning True on success. Any exception propagates to `states()`, which converts it to `None`.
- `open_pane`'s default opener launches the URL via `os.startfile` or `subprocess.run(["cmd", "/c", "start", "", url])`. The key must be validated against `PANES` first so caller input never reaches the shell — the macOS version is explicit about this and the same rule holds here.

- [ ] **Step 4:** Run the tests. Expected: 9 passed.

- [ ] **Step 5: Verify the real probe and the real deep link**

```bash
./venv/Scripts/python -c "from voisr.permissions import states; print(states())"
```
Expected: `granted: True` on a machine with a working microphone.

Then `./venv/Scripts/python -c "from voisr.permissions import open_pane; open_pane('microphone')"` and confirm the Windows Settings microphone page actually opens. Report what happened.

- [ ] **Step 6: Commit**

```bash
git add voisr/permissions.py tests/test_permissions.py
git commit -m "feat: microphone permission probe with ms-settings deep link"
```

---

## Task 3: `launch_agent.py` — launch at login

macOS writes a LaunchAgent plist. Windows uses a registry value.

**Files:** Create `voisr/launch_agent.py`, `tests/test_launch_agent.py`

- [ ] **Step 1: Write the failing tests**

Tests must NOT touch the real registry. Take a registry-accessor object as an injectable dependency, defaulting to a real `winreg` implementation.

```python
from voisr.launch_agent import RUN_KEY, VALUE_NAME, disable, enable, is_enabled


class FakeRegistry:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def read(self, name):
        return self.values.get(name)

    def write(self, name, value):
        self.values[name] = value

    def remove(self, name):
        self.values.pop(name, None)


def test_disabled_when_value_absent():
    assert is_enabled(registry=FakeRegistry()) is False


def test_enabled_when_value_present():
    reg = FakeRegistry({VALUE_NAME: "anything"})
    assert is_enabled(registry=reg) is True


def test_enable_writes_a_value():
    reg = FakeRegistry()
    enable(registry=reg)
    assert VALUE_NAME in reg.values
    assert is_enabled(registry=reg) is True


def test_enable_command_launches_without_a_console_window():
    reg = FakeRegistry()
    enable(registry=reg)
    assert "pythonw" in reg.values[VALUE_NAME].lower()


def test_enable_command_is_quoted_for_paths_with_spaces():
    reg = FakeRegistry()
    enable(registry=reg)
    assert reg.values[VALUE_NAME].startswith('"')


def test_disable_removes_the_value():
    reg = FakeRegistry({VALUE_NAME: "x"})
    disable(registry=reg)
    assert is_enabled(registry=reg) is False


def test_disable_is_idempotent():
    reg = FakeRegistry()
    disable(registry=reg)
    disable(registry=reg)
    assert is_enabled(registry=reg) is False


def test_run_key_is_per_user_not_machine_wide():
    assert "HKEY_CURRENT_USER" in RUN_KEY or "HKCU" in RUN_KEY
```

- [ ] **Step 2:** Run them, observe failure.

- [ ] **Step 3: Implement**

- `RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"`, `VALUE_NAME = "Voisr"`
- Per-user (`HKEY_CURRENT_USER`), never `HKEY_LOCAL_MACHINE` — machine-wide would need elevation, and this app deliberately runs unelevated.
- The command must use **`pythonw.exe`**, not `python.exe`. `python.exe` opens a console window at every login, which is unacceptable for a background app.
- Quote the executable path: it lives under `C:\Users\andre\OneDrive\Documentos\...`, and unquoted paths with spaces break `Run` entries.

- [ ] **Step 4:** Run the tests. Expected: 8 passed.

- [ ] **Step 5: Verify against the real registry, then clean up**

Enable it for real, read the value back with `reg query`, confirm it is well-formed, then disable and confirm the value is gone. Report the actual value string. **Leave the registry as you found it.**

- [ ] **Step 6: Commit**

```bash
git add voisr/launch_agent.py tests/test_launch_agent.py
git commit -m "feat: launch at login via per-user Run key"
```

---

## Task 4: `window_chrome.py` — dark titlebar

Mica is Windows 11 only and the target is Windows 10 19045, so the backdrop attempt is expected to fail and fall through to flat. That is a designed-for path: `base.css` already ships a `:root[data-vibrancy="off"]` fallback. `DWMWA_USE_IMMERSIVE_DARK_MODE` **does** work on 19045.

**Files:** Create `voisr/window_chrome.py`, `tests/test_window_chrome.py`

- [ ] **Step 1: Write the failing tests**

```python
from voisr.window_chrome import apply_chrome


class FakeDwm:
    def __init__(self, fail_attributes=()):
        self.calls = []
        self.fail = set(fail_attributes)

    def set_attribute(self, hwnd, attribute, value):
        self.calls.append((attribute, value))
        return 0 if attribute not in self.fail else 1


def test_dark_mode_is_applied():
    dwm = FakeDwm()
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result["dark_titlebar"] is True
    assert (20, 1) in dwm.calls


def test_light_mode_clears_dark_titlebar():
    dwm = FakeDwm()
    apply_chrome(1234, dark=False, dwm=dwm)
    assert (20, 0) in dwm.calls


def test_backdrop_failure_reports_no_vibrancy():
    dwm = FakeDwm(fail_attributes={38})
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result["vibrancy"] is False


def test_backdrop_success_reports_vibrancy():
    result = apply_chrome(1234, dark=True, dwm=FakeDwm())
    assert result["vibrancy"] is True


def test_dark_titlebar_failure_is_reported_not_raised():
    dwm = FakeDwm(fail_attributes={20})
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result["dark_titlebar"] is False


def test_exception_never_propagates():
    class Exploding:
        def set_attribute(self, *args):
            raise OSError("dwmapi missing")

    result = apply_chrome(1234, dark=True, dwm=Exploding())
    assert result == {"dark_titlebar": False, "vibrancy": False}
```

- [ ] **Step 2:** Run them, observe failure.

- [ ] **Step 3: Implement**

- `apply_chrome(hwnd, dark=True, dwm=None) -> dict` returning `{"dark_titlebar": bool, "vibrancy": bool}`
- Attribute 20 is `DWMWA_USE_IMMERSIVE_DARK_MODE`; attribute 38 is `DWMWA_SYSTEMBACKDROP_TYPE` (value 2 = Mica)
- Default `dwm` wraps `ctypes.WinDLL("dwmapi").DwmSetWindowAttribute`. **Declare `argtypes` and `restype` explicitly** — a missing `argtypes` on a Win32 call has already caused a real bug on this project, where a pointer-sized argument was truncated to 32 bits and the call failed silently.
- Chrome is cosmetic: nothing here may ever raise.

- [ ] **Step 4:** Run the tests. Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add voisr/window_chrome.py tests/test_window_chrome.py
git commit -m "feat: DWM dark titlebar with flat fallback on Windows 10"
```

---

## Task 5: Harden `inject.py` against self-paste

**This closes a real defect, not a hypothetical one.** Task 12 of Phase 1 measured WebView2's animation loop stealing foreground **229 times in 9 seconds**; a guard thread now returns focus within ~30 ms, but that is a mitigation, not a guarantee. Once the pill is wired to live dictations (Task 7), a paste landing inside that window would inject Ctrl+V into our own webview and the user's text would vanish silently.

**Files:** Modify `voisr/inject.py`, `tests/test_inject_unit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inject_unit.py`:

```python
def test_paste_blocked_when_foreground_is_our_own_window(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(inject, "foreground_is_own_process", lambda: True)
    sent = []
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: sent.append(True))
    assert inject._post_ctrl_v() is False
    assert sent == []


def test_paste_proceeds_when_foreground_is_another_process(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(inject, "foreground_is_own_process", lambda: False)
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: True)
    assert inject._post_ctrl_v() is True


def test_own_process_check_failure_does_not_block_paste(monkeypatch):
    """Unknown must not mean blocked — that would refuse pastes that
    would have worked, which is worse than the rare self-paste."""
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(
        inject, "_foreground_pid",
        lambda: (_ for _ in ()).throw(OSError("no window")))
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: True)
    assert inject._post_ctrl_v() is True
```

- [ ] **Step 2:** Run them, observe failure.

- [ ] **Step 3: Implement**

- Add `_foreground_pid() -> int | None` and `foreground_is_own_process() -> bool`, comparing against `kernel32.GetCurrentProcessId()`
- `_post_ctrl_v` returns False when the foreground window belongs to us
- Any failure determining this returns False (not our process), matching the existing `foreground_is_elevated` convention — an unknown must never block a paste that would have worked
- Refactor `foreground_is_elevated` to reuse `_foreground_pid` rather than duplicating the lookup

The existing blocked path already preserves the clipboard and raises `PasteDeliveryError(pasted=False)`, so the user gets a reported failure with their text recoverable instead of silent loss.

- [ ] **Step 4:** Run the tests, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add voisr/inject.py tests/test_inject_unit.py
git commit -m "fix: never paste into our own window"
```

---

## Task 6: Port the web assets

**Files:** Copy `$MAC/voisr/web/**` (except `widget.html`, already ported); create `tests/test_web_assets.py`, `tests/js/**`

- [ ] **Step 1: Copy everything**

```bash
cp -r "$MAC/voisr/web/css" voisr/web/
cp -r "$MAC/voisr/web/js" voisr/web/
cp "$MAC/voisr/web/index.html" voisr/web/index.html
cp -r "$MAC/tests/js" tests/js
cp "$MAC/tests/test_web_assets.py" tests/test_web_assets.py
cp "$MAC/tests/test_js_behaviour.py" tests/test_js_behaviour.py
```

- [ ] **Step 2: Find every macOS-ism**

```bash
grep -rnE "⌘|⌥|⌃|⇧|Cmd|Command key|Option|macOS|System Settings|x-apple" voisr/web/
```

Report the full list before changing anything.

- [ ] **Step 3: Apply Windows labels**

Translate, and only translate — do not restyle or refactor:

| macOS | Windows |
|---|---|
| `⌘K` / "Cmd-K" | `Ctrl+K` |
| `⌘F`, `⌘C` | `Ctrl+F`, `Ctrl+C` |
| "Right Option (⌥)" and the rest of the hotkey labels | the labels from `voisr/keys.py` `hotkey_options()` |
| "System Settings" | "Settings" |
| `x-apple.systempreferences:...` (in the api.js dev mock) | `ms-settings:privacy-microphone` |
| "Microphone, Input Monitoring and Accessibility all granted." (`home.js`) | "Microphone access granted." |

The key-handling JS must accept `ctrlKey` where it currently checks `metaKey`. Search for `metaKey` and report every occurrence you change.

The `api.js` dev-mock's `mockCfg` should use `hotkey: "ctrl_r"` and drop the macOS `device: ":0"`.

- [ ] **Step 4: Run the JS suite**

Run: `node --test tests/js/`
Expected: all pass. node 24.14.1 is installed. Report the actual output.

- [ ] **Step 5: Run the Python asset tests**

`tests/test_web_assets.py` and `tests/test_js_behaviour.py` assert on file structure and run the node suite. They may reference macOS strings — if one fails because it asserts on a `⌘` label you just changed, update the assertion to the Windows label. That is the correct fix. Report every test you touched.

- [ ] **Step 6: Preview in a browser**

`index.html` has a dev fallback that runs against an in-memory mock when the pywebview bridge is absent. Open it directly:

```bash
start voisr/web/index.html
```

Confirm the dashboard renders and the four views are reachable. You cannot judge whether it looks *right* — report what rendered and what errored in the console, and leave the aesthetic judgement to a human.

- [ ] **Step 7: Commit**

```bash
git add voisr/web tests/js tests/test_web_assets.py tests/test_js_behaviour.py
git commit -m "feat: port dashboard web assets with Windows key labels"
```

---

## Task 7: `ui.py` — the Api, HotkeyService, and window

The largest task. Port `$MAC/voisr/ui.py`, deleting everything that already lives in a dedicated module here.

**Files:** Create `voisr/ui.py`, `tests/test_ui_api.py`

- [ ] **Step 1: Read the macOS original and plan the deletions**

Read `$MAC/voisr/ui.py` in full (1341 lines). Report, BEFORE writing code, which sections you are deleting because they are already implemented here:

| macOS section | Replace with |
|---|---|
| `resolve_key`, `key_label`, `legacy_name_to_vk`, `event_vk`, `HOTKEY_OPTIONS` | `voisr.keys` |
| `capture_next_key`, `_quartz_listener_factory` | `voisr.keytap` |
| `class RecordingWidget` (~175 lines) | `voisr.overlay.RecordingWidget` |
| `request_permissions`, `input_permissions_ok`, `paste_ready` | `voisr.permissions`; Windows has no Input Monitoring or Accessibility gate |
| `apply_app_appearance` (NSAppearance) | `voisr.window_chrome.apply_chrome` |
| `_run_save_panel` (NSSavePanel) | a native Win32 save dialog |
| `_find_webview`, `window_chrome` vibrancy plumbing | not applicable |

- [ ] **Step 2: Write the failing Api tests**

Port `$MAC/tests/test_ui_api.py`. It exercises `Api` against fakes and is largely platform-independent. Triage as in Phase 2: keep / rewrite / delete, and report the counts with a justification for every deletion.

- [ ] **Step 3:** Run them, observe failure.

- [ ] **Step 4: Implement `voisr/ui.py`**

`Api` must expose every method the JS calls. Derive the list from `voisr/web/js/` — grep for `V.api().` and make sure each one exists. At minimum: `get_history`, `delete_history_item`, `restore_history_item`, `clear_history`, `get_stats`, `get_config`, `set_config`, `get_hotkey_options`, `hotkey_display`, `get_hotkey_status`, `capture_hotkey`, `get_pill_themes`, `get_permissions`, `open_settings_pane`, `get_models`, `set_launch_at_login`, `set_appearance`, `export_history`, `save_text_file`, `prime_microphone`, `copy_text`, `start_recording`, `stop_recording`, `is_recording`.

**A missing method is a silently broken button.** Verify the grep list matches your implementation and report any JS call with no Python counterpart.

Windows-specific notes:
- `set_appearance` calls `voisr.window_chrome.apply_chrome(hwnd, dark=...)` and tells the webview, instead of setting `NSApp.appearance`
- `save_text_file` uses a native Win32 save dialog (`win32gui.GetSaveFileNameW` via pywin32) instead of `NSSavePanel`
- There is no `AppHelper.callAfter`. pywebview marshals `evaluate_js` from any thread. Delete the main-thread dispatch machinery — but keep every `try/except` around UI updates: the macOS code is deliberate that a cosmetic failure must never break a dictation.
- `main()` creates the window with `webview.create_window(...)` pointing at `voisr/web/index.html`, passing `js_api=api`, then `webview.start(gui="edgechromium")`

`HotkeyService` ports closely — it already delegates to an injectable listener factory. Point it at `voisr.keytap.WindowsKeyListener` and keep the `_watch` watchdog; the failure it guards (a hook silently unregistered) exists on Windows too, just with a different trigger.

- [ ] **Step 5:** Run the Api tests, then the full suite.

- [ ] **Step 6: Launch the real dashboard**

```bash
./venv/Scripts/python -m voisr.ui
```

Verify programmatically what you can: the window is created, the HWND exists, no exception on startup, and the browser console has no errors (`window.evaluate_js` can read them back). Report the actual console output.

You cannot judge the visual result. Do not claim to.

- [ ] **Step 7: Commit**

```bash
git add voisr/ui.py tests/test_ui_api.py
git commit -m "feat: dashboard window, js_api surface, and hotkey service"
```

---

## Task 8: Wire the pill to live dictations

**Files:** Modify `voisr/ui.py`, `tests/test_ui_api.py`

- [ ] **Step 1: Write the failing tests**

```python
class FakeWidget:
    def __init__(self):
        self.calls = []

    def show_recording(self, **kw):
        self.calls.append("recording")

    def show_processing(self, **kw):
        self.calls.append("processing")

    def show_done(self, **kw):
        self.calls.append("done")

    def show_error(self, **kw):
        self.calls.append("error")

    def hide(self, **kw):
        self.calls.append("hide")


def test_press_shows_the_recording_pill(hotkey_service_with_widget):
    service, widget = hotkey_service_with_widget
    service.press()
    assert widget.calls == ["recording"]


def test_release_shows_processing_then_done(hotkey_service_with_widget):
    service, widget = hotkey_service_with_widget
    service.press()
    service.release()
    assert "processing" in widget.calls
    assert widget.calls[-1] in ("done", "hide")


def test_transcription_failure_shows_the_error_pill(hotkey_service_failing):
    service, widget = hotkey_service_failing
    service.press()
    service.release()
    assert "error" in widget.calls


def test_widget_disabled_in_config_is_never_shown(hotkey_service_no_widget):
    service, widget = hotkey_service_no_widget
    service.press()
    service.release()
    assert widget.calls == []
```

Write the three fixtures to match `HotkeyService`'s actual constructor. The `show_widget` config key already exists in the macOS `DEFAULT_CONFIG`.

- [ ] **Step 2:** Run them, observe failure.

- [ ] **Step 3: Implement**

Port the macOS `HotkeyService` widget wiring — `_widget_enabled`, `_run_widget_action`, `_widget_action`, and the epoch guard. The epoch guard matters: without it, a finished dictation can hide the pill belonging to a newer one that has already started.

Read `$MAC/voisr/ui.py` `HotkeyService.press` / `release` / `_finish` for the exact ordering.

- [ ] **Step 4:** Run the tests, then the full suite.

- [ ] **Step 5: Verify the pill during a real dictation**

Drive a full cycle programmatically the way earlier agents did: run the service with hotkey `f13`, synthesize UNTAGGED F13 down/up via a raw `SendInput` with `dwExtraInfo=0` (`voisr.inject._key_down` tags with `VOISR_SIGNATURE` and the hook filters it).

Confirm: the pill window becomes visible on press, transitions state, hides afterwards, and — critically — `GetForegroundWindow()` is never the pill's HWND at the moment the paste fires. Task 5's guard should make a self-paste impossible, but verify the guard is actually reached rather than assuming.

- [ ] **Step 6: Commit**

```bash
git add voisr/ui.py tests/test_ui_api.py
git commit -m "feat: show the dictation pill during live dictations"
```

---

## Done criteria

- [ ] Full Python suite green; `node --test tests/js/` green
- [ ] `python -m voisr.ui` opens a dashboard with Home, History, Dictionary, Settings reachable
- [ ] Every `V.api()` call in the JS has a Python counterpart
- [ ] Dictating records to history and the entry appears in the History view
- [ ] The pill appears during a real dictation and never becomes the foreground window
- [ ] Ctrl+K opens the command palette
- [ ] Launch-at-login toggles the `Run` key and reverts cleanly

## Human verification required

Nothing in this plan can judge whether the dashboard *looks* right. After it lands, a human should check: the four views render correctly, dark and light mode both work, the pill animates, and the History view shows real dictations.

## Next

Phase 4 — tray icon and installer.
