# Voisr for Windows — Design

**Date:** 2026-07-28
**Status:** Approved
**Source app:** `andr26j/voisr-mac` @ `main` (private)

## Goal

A Windows version of Voisr with full feature parity: hold a key, speak, and clean
text appears at the cursor in any app. All processing stays on the machine.

This is a separate repository, not a branch of the macOS app. The two will
diverge; that is accepted.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Codebase | Separate Windows repo | Every line is testable on the target machine. No risk of breaking a working macOS app while editing it blind. |
| Transcription | whisper.cpp `whisper-cublas-12.4.0-bin-x64` | Same `ggml-*.bin` models and same `transcriber.py` shape as macOS, so output matches. One download, no CUDA toolkit install. GPU-accelerated on the target GTX 1070 (compute 6.1, supported by CUDA 12.x). |
| Scope | Full parity, Windows-idiomatic | Ctrl+K palette, tray icon, Ctrl+V, flat chrome. Permission strip shrinks to Microphone only. |
| Install | `setup.ps1` now, installer later | Mirrors the macOS `setup.sh` + `install_app.sh`. Fast to iterate. PyInstaller friction with PortAudio, WebView2, and a 500 MB model is deferred until the app is proven. |
| Pill overlay | pywebview + Win32 style patch, spiked first | Reuses `widget.html` and its theme + custom-colour features. Spiked before anything is built on top of it. |
| Elevation | Run unelevated | Avoids a UAC prompt per launch and an always-elevated keyboard hook running all day. Elevated-window failures are detected and reported instead. |
| Default hotkey | Right Ctrl | Layout-safe. Right Alt is AltGr on many layouts, which corrupts an injected Ctrl+V. |

## Environment (verified 2026-07-28)

- Windows 10 Pro 19045, AMD Ryzen 5 2600, 16 GB RAM
- NVIDIA GeForce GTX 1070
- Python 3.13.12, node v24.14.1, winget available
- WebView2 Runtime 150.0.4078.99 already installed

## Architecture

Module names are kept **identical to the macOS repo**, including macOS jargon
like `pasteboard.py`. Not for merging — the repos are separate — but so that a
misbehaving Windows module can be diffed against a known-good implementation of
the same contract.

```
Voicr-Windows/
├── voisr/
│   ├── cleanup.py           copy verbatim              (152 lines)
│   ├── recording_queue.py   copy verbatim              (333)
│   ├── stream_recorder.py   copy verbatim              (102)  sounddevice is cross-platform
│   ├── history.py           rewrite durability layer   (503 → ~300)
│   ├── pasteboard.py        rewrite on Win32 clipboard (208)
│   ├── keytap.py            rewrite as WH_KEYBOARD_LL  (112)
│   ├── inject.py            rewrite: SendInput Ctrl+V  (180)
│   ├── status_item.py       rewrite as tray icon       (248)
│   ├── overlay.py           NEW: pill, extracted from ui.py
│   ├── launch_agent.py      rewrite: HKCU Run key      (30)
│   ├── permissions.py       collapses to mic check     (92 → ~30)
│   ├── window_chrome.py     rewrite: DWM dark titlebar (54)
│   ├── recorder.py          ffmpeg dshow + stdin 'q'   (85)
│   ├── transcriber.py       whisper-cli.exe path       (52, light edit)
│   ├── paths.py             bin/ + %LOCALAPPDATA%      (18, rewrite)
│   ├── ui.py                port; pill extracted out   (1341 → ~1150)
│   ├── app.py               port CLI entry             (141)
│   └── web/                 copy; ⌘ labels → Ctrl      (~2300)
├── scripts/setup.ps1
├── bin/          whisper-cli.exe + CUDA DLLs
├── models/       ggml-small.en.bin, ggml-base.en.bin
└── tests/
```

### The one structural change

`RecordingWidget` moves out of `ui.py` into `overlay.py`. Justification: it is
~175 lines of pure platform code inside the largest module, it must be rewritten
from scratch regardless, and it is the piece being spiked first. Isolating it
means the spike result lands in one file instead of surgery on `ui.py`.

No other restructuring is in scope.

## Platform seams

### Global key listener — `keytap.py`

`SetWindowsHookExW(WH_KEYBOARD_LL)` on a dedicated thread running its own
`GetMessage` pump. The hook must be installed from the thread that pumps.

Two simplifications over macOS:

- Windows reports modifiers as ordinary key events, so the macOS `"flags"` event
  kind collapses. The handler signature narrows to `(vk, kind)` with kind in
  `{"down", "up"}`.
- No `kCGEventTapDisabledByTimeout` re-enable path.

**Self-injection filter.** macOS compares the event's source PID to ignore its
own synthetic Cmd-V. `LLKHF_INJECTED` is not equivalent — it is set by any
injecting app. Instead, tag our own `SendInput` calls with a magic
`dwExtraInfo` sentinel and filter on that. Exact rather than approximate.

**Callback budget.** A low-level hook is silently unregistered if its callback
exceeds `LowLevelHooksTimeout` (default 300 ms). The callback therefore does
nothing but record a virtual key and return. This is the same *class* of failure
as the macOS tap disable, so `HotkeyService._watch` is retained as the watchdog,
checking hook liveness instead of tap liveness.

### Clipboard — `pasteboard.py`

`GetClipboardSequenceNumber()` is a true analogue of `NSPasteboard.changeCount`,
so the ownership model transfers directly and the public interface is unchanged:
`snapshot()`, `write_text_if_unchanged()`, `is_owned()`, `restore_if_unchanged()`.

Capture: `OpenClipboard(NULL)` → `EnumClipboardFormats` loop →
`GetClipboardData` → `GlobalLock` / `GlobalSize` / copy / `GlobalUnlock` →
`CloseClipboard`. Restore: `OpenClipboard` → `EmptyClipboard` →
`SetClipboardData` per format.

Three hazards with no macOS equivalent:

1. **`OpenClipboard` fails transiently** whenever another process holds the
   clipboard. This is the primary source of Windows clipboard flakiness. Bounded
   retry with backoff; exhaustion raises `ClipboardPreservationError` rather than
   surfacing a raw Win32 error.
2. **Delayed rendering.** Excel and Word advertise formats with a null handle and
   render on demand. `GetClipboardData` on those can hang or fail. Policy: skip
   null-handle formats at capture time and record that the snapshot is partial.
   A partial snapshot must never be reported as a lossless restore.
3. **Synthesized formats.** Windows fabricates `CF_TEXT`, `CF_OEMTEXT`, and
   `CF_LOCALE` from `CF_UNICODETEXT`. Capturing and re-setting them conflicts
   with the ones Windows regenerates, so synthesized formats are skipped at
   capture and left for the OS to rebuild.

### Paste — `inject.py`

`SendInput` with Ctrl down, V down, V up, Ctrl up, tagged with the
`dwExtraInfo` sentinel.

`_paste_with_adapter` — the snapshot / write / ownership-check /
restore-on-every-failure state machine, ~90 lines — **copies verbatim**, because
the adapter interface is preserved. This is the most valuable logic in the
module and it survives untouched.

`set_clipboard` / `get_clipboard` replace `pbcopy` / `pbpaste` with Win32 calls
through the same adapter.

### Windows-only failure modes

These are designed in from the start, not discovered later.

**UIPI.** A normal-integrity process cannot hook input from, or `SendInput`
into, an elevated window. Both the hotkey and the paste fail against Task
Manager, admin terminals, and similar — and `SendInput` fails *silently*,
returning success. macOS's `CGPreflightPostEventAccess` has no counterpart, so
detection is ours: read the foreground window's process integrity level via
`GetForegroundWindow` → `GetWindowThreadProcessId` → `OpenProcessToken` →
`TokenIntegrityLevel`, and compare against our own. Higher integrity means the
paste will be swallowed, so take the existing blocked path:
`PasteDeliveryError(pasted=False)` with the clipboard preserved. The existing
error model already fits this exactly.

**Held-modifier corruption.** Push-to-talk means a key is physically down when
Ctrl+V is injected. Any held modifier composes with the injected chord. Right
Alt is AltGr on many layouts, where the result is not Ctrl+V at all. Mitigation:
synthesize key-ups for held modifiers immediately before injection and restore
their state after. Choosing right Ctrl as the default removes the worst case,
but the mitigation is still required — the hotkey is user-configurable.

**No Mica on Windows 10.** Mica is Windows 11 only, and the target is 19045.
The Windows 10 acrylic route is an undocumented `SetWindowCompositionAttribute`
call that visibly degrades window dragging. Decision: attempt
`DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE)`, expect failure, fall through
to flat. This is a designed-for path, not an improvised one — `base.css` already
ships a `:root[data-vibrancy="off"]` fallback for exactly this case.
`DWMWA_USE_IMMERSIVE_DARK_MODE` does work on 19045 and provides the dark
titlebar.

### Tray — `status_item.py`

`pystray` + Pillow rather than ~200 lines of raw `Shell_NotifyIcon`. pystray owns
its message window and thread, which replaces the macOS `AppHelper.callAfter`
main-thread discipline; `set_recording` becomes an icon swap that pystray
marshals internally.

The 3-bar waveform icon is drawn with Pillow instead of `NSBezierPath`. Windows
tray icons do not auto-invert the way macOS template images do, so light/dark
variants are selected from the `SystemUsesLightTheme` registry value.

### Permissions — `permissions.py`

Windows has no Input Monitoring or Accessibility gate, so two of the three chips
disappear. Microphone has a privacy setting but no reliable programmatic probe
for Win32 desktop apps equivalent to `AVCaptureDevice.authorizationStatus`, so
the probe is an attempted short `sounddevice` stream open. Deep link:
`ms-settings:privacy-microphone`.

`home.js` already renders chips from whatever list `get_permissions()` returns,
so a single-entry list works. One hardcoded string at `home.js:23`
("Microphone, Input Monitoring and Accessibility all granted.") needs editing.

### History — `history.py`

The macOS implementation is deeply POSIX and cannot be copied:

| Line | Uses | Windows status |
|---|---|---|
| 2, 330 | `fcntl.flock` | no such API |
| 25–27 | `os.O_DIRECTORY`, `O_NOFOLLOW`, `O_NONBLOCK` | flags do not exist |
| 210 | `os.register_at_fork` | does not exist |
| 391–397 | `os.fsync(parent_fd)` | cannot fsync a directory |
| 171, 295+ | `os.fchmod(0o600)` | no-op; Windows uses ACLs |

The guarantees from `2026-07-23-atomic-history-durability.md` — atomic appends,
crash durability, safety across concurrent processes — must be re-established on
Windows primitives: `LockFileEx` for locking, `os.replace` for atomic swap
(atomic on Windows).

The rewrite is *simpler* than the original. There is no fork on Windows, so the
`register_at_fork` epoch machinery is deleted outright, and `%LOCALAPPDATA%` is
already per-user ACL'd, making the `0600` handling redundant. Expected ~503 → ~300
lines.

### Recorder — `recorder.py`

`recorder.py:43` stops ffmpeg with `SIGINT`, which does not work on Windows.
Replacement: open ffmpeg's stdin as a pipe and write `q`, its documented
graceful-quit input. Input device moves from `avfoundation` to `dshow`.

This is the CLI toggle-mode path only. The primary app path is
`stream_recorder.py`, which is already cross-platform.

### `ui.py`

- `request_permissions()` → microphone prime only
- `apply_app_appearance` → DWM dark titlebar + webview notification
- `_run_save_panel` (`NSSavePanel`) → native Win32 save dialog via pywin32
- `resolve_key` / `key_label` / `legacy_name_to_vk` → remapped from Quartz
  virtual keycodes to Windows VK codes. macOS config files do not carry over.
- `main()` → pywebview `create_window` with the EdgeChromium backend
- All `AppHelper.callAfter` dispatch → pywebview's own thread marshalling and
  pystray's internal thread

## Data and configuration

Config and history move to `%LOCALAPPDATA%\Voisr\` rather than the repo
directory — Windows convention, and it survives reinstalls.

Default config changes from macOS: `hotkey` becomes right Ctrl; `device`
changes from the avfoundation `":0"` form to a dshow device name.

## Dependencies

**Drop:** `pynput`, `pyobjc-framework-AVFoundation`
**Keep:** `pytest`, `pywebview`, `sounddevice`
**Add:** `pywin32`, `pystray`, `Pillow`, `pythonnet` (required by pywebview's
WebView2 backend)

## Build order and verification

Ordered by risk, not by module list. Each phase has a pass/fail check.

| Phase | Work | Verified by |
|---|---|---|
| 0 | Pill spike — nothing else | Pill visible over a fullscreen app; clicks pass through; typing into Notepad continues to work while it is shown |
| 1 | Core loop: capture → whisper CUDA → clean → clipboard → paste | Hold right-Ctrl in Notepad, speak, correct text appears at the cursor |
| 2 | `history.py` durability rewrite + queue | Ported suite green, plus a kill-mid-write crash test showing no partial records |
| 3 | Dashboard: pywebview + web assets + `Api` | JS tests green under node; all four views working |
| 4 | Tray, autostart, chrome, permission chip | Tray menu drives dictation; app survives reboot with launch-at-login on |
| 5 | `setup.ps1` + Start Menu shortcut | Clean run: fresh clone → working app |

Phase 1 is the usability milestone. Everything after it is the shell.

**Phase 0 fallback.** If WebView2 transparency cannot deliver a transparent,
click-through, non-focusing overlay, fall back to drawing the pill natively
(tkinter or Qt). That fallback loses `widget.html` and requires reimplementing
the pill theme and custom-colour features in Python drawing code, which is why
it is a fallback and why the spike runs before anything depends on it.

## Testing

The 27 Python test files split the same way the modules do. Portable tests
travel with their module. Platform tests are rewritten — and the Windows test is
written *first* in each case, because the existing macOS test is a precise
statement of the contract the new implementation must meet.

`tests/conftest.py` synthesizes speech with the macOS `say` command. Windows
replacement: `System.Speech.Synthesis.SpeechSynthesizer` with
`SetOutputToWaveFile`, invoked via PowerShell. Built into the OS, no install.
This keeps `test_e2e.py` — which runs the whole pipeline and asserts on the
resulting text — working. It is the single best proof the port is real.

JS tests under `tests/js/` run unchanged on node v24.14.1.

**Manual checklist** for what tests cannot reach:

- Pill renders over a fullscreen app and does not steal focus
- Paste into Notepad, Chrome, VS Code, Word
- Rich clipboard preservation: copy formatted text from Word, dictate, confirm
  formatting survives
- Blocked-paste path: focus an elevated window, dictate, confirm the clipboard is
  preserved and the failure is reported rather than silent

## Error-handling stance

Carried over from the macOS app deliberately. That codebase is careful never to
claim success it did not achieve: unknown permission is never rendered as
granted, a blocked paste always preserves the clipboard, a failed restore is
reported rather than swallowed.

The Windows version keeps this stance. It is the reason UIPI detection is in
scope: a silent `SendInput` failure is precisely the kind of lie the existing
design refuses to tell.

## Out of scope

- Packaged installer (PyInstaller / Inno Setup) — deferred until the app is proven
- Running elevated, and any Task Scheduler mechanism to do so without UAC
- Mica or acrylic backdrop on Windows 10
- Command Mode (voice editing) — out of scope in the macOS app too
- Any restructuring of `ui.py` beyond extracting `RecordingWidget`
- Merging back into the macOS repo

## Open risks

1. **WebView2 transparency** — the Phase 0 spike exists to resolve this. Fallback
   is designed.
2. **CUDA DLL loading** — the cuBLAS build ships DLLs that must resolve at
   subprocess launch. May need `os.add_dll_directory` or a PATH prepend in
   `paths.py`.
3. **Clipboard delayed rendering** — behaviour varies by source app. The partial-
   snapshot policy bounds the damage but real-world coverage needs the manual
   Word/Excel checks.
4. **pywebview + pythonnet on Python 3.13** — recent enough to warrant an early
   smoke check rather than an assumption; folded into Phase 0.
