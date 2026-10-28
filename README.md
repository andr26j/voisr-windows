# Voisr (Windows)

A local-only Windows port of [Wispr Flow](https://wisprflow.ai)'s core loop: **hold a key, speak, and clean text appears at your cursor** — in any app. All processing happens on your machine (whisper.cpp); no audio or text ever leaves it.

## Download

Grab `VoisrSetup.exe` from your release channel and run it. No Python, no git checkout, no admin rights.

- **The installer is unsigned.** There's no code-signing certificate yet, so Windows SmartScreen will show "Windows protected your PC" on first run. Click **More info**, then **Run anyway**. This is expected.
- **Voisr is CPU-only.** It bundles whisper.cpp's BLAS build — the audience is employees on their own machines, most without an NVIDIA GPU, so there's no GPU build to pick. Measured on this machine: **~3.1 seconds to transcribe a ~14-second utterance.** Running from source with `scripts\setup.ps1` gets the same binaries.
- Installs per-user to `%LOCALAPPDATA%\Programs\Voisr` — no admin prompt. Uninstalling leaves `%LOCALAPPDATA%\Voisr` (your config and dictation history) alone.
- Installer size: **~150–160 MB** compressed (the frozen bundle itself is ~300 MB, dominated by the bundled `ggml-base.en` model and whisper.cpp's BLAS DLLs).

### What the installer brings with it

Nothing needs installing first. Everything Voisr needs at runtime either ships inside the bundle or is installed for you, all without admin:

- **Python, PortAudio, pythonnet** — frozen into the bundle by PyInstaller.
- **The MSVC runtime** (`MSVCP140`, `VCRUNTIME140`, `VCRUNTIME140_1`) and **OpenMP** (`VCOMP140`) — deployed app-local into `bin\` beside `whisper-cli.exe`, so no Visual C++ redistributable is required. They go in `bin\` specifically because Windows searches a process's own directory first; PyInstaller's copies one level up are invisible to `whisper-cli.exe`.
- **whisper.cpp + the `ggml-base.en` model** — bundled. The CPU variants ship from `ggml-cpu-x64` (baseline) up to `ggml-cpu-alderlake`, and whisper picks by CPU at load, so pre-AVX2 machines work too.
- **The Edge WebView2 Runtime** — the dashboard's renderer. Already present on Windows 11 and anywhere with a recent Edge; where it isn't, setup installs it silently per-user via the Evergreen Bootstrapper. This step needs an internet connection, and setup stops with an explanation if it fails.

Two things are outside the installer's reach:

- **.NET Framework 4.7.2+**, which pythonnet hosts the dashboard on. In-box on Windows 10 1803 and later and on Windows 11, so every supported Windows already has it — but it cannot be installed per-user, so an ancient build would need it added by an admin.
- **Microphone permission.** On by default, but if dictation records silence check Settings → Privacy & security → Microphone → *Let desktop apps access your microphone*.

## What it does

1. **Capture** — records your mic while you hold the hotkey
2. **Transcribe** — on-device speech-to-text via whisper.cpp (`ggml-base.en`), CPU/BLAS
3. **Clean up** — strips filler words (um, uh, hmm…), normalizes spacing/punctuation, capitalizes, and applies your custom dictionary (e.g. `"trademake" → "TradeMate"` for names the ASR mishears)
4. **Insert** — pastes the result at the cursor of the frontmost window (Ctrl+V via `SendInput`), restoring your clipboard afterwards. If the paste is blocked, your original clipboard is preserved and the failure is reported, not silent. The dictation is recorded to history either way.

## Setup

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Creates the dev venv, downloads the whisper.cpp CPU/BLAS build into `bin\`, and downloads the `ggml-base.en` model (~141 MB) into `models\`. No GPU required.

## Install (Start Menu app)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

This copies the runtime to `%LOCALAPPDATA%\Voisr\app` (code, whisper binary, model, and a dedicated venv) and creates Start Menu and Desktop shortcuts. It installs from this working checkout, so `scripts\setup.ps1` must have already populated `bin\` and `models\` — it's for developers, not end users. Someone without this repo and a Python install should use `VoisrSetup.exe` instead (see [Download](#download)); the two installs coexist and share the same config/history.

The installer is safe to re-run: it skips re-copying `bin\` and `models\` when they already exist at the destination (both are large), and never touches an existing `config.json` or `history.jsonl`. Re-run it after pulling code changes to update the installed copy.

Config and history live in `%LOCALAPPDATA%\Voisr\config.json` / `history.jsonl` — one level above the installed app, so they survive reinstalls and even an uninstall (see below).

For development, run the dashboard straight from the repo instead:

```powershell
venv\Scripts\python -m voisr.ui
```

## Usage

Hold **right Ctrl** to dictate, speak, release — the transcription is cleaned up and pasted at your cursor. A floating pill shows recording/transcribing state, and its bars follow your voice.

- **Hands-free:** double-tap the key to keep recording without holding it; press it once more to finish.
- **Tap to nothing:** a quick tap on its own (under half a second) is dropped, so brushing the key never pastes anything.
- **Esc cancels** a dictation in progress — the audio is dropped and nothing is pasted.
- **Shortcuts stay shortcuts:** pressing another key within a second of the hotkey (Right Ctrl+C, say) cancels the dictation instead of recording it.
- **Silence is caught before whisper:** a recording with no speech in it says so instead of pasting `[BLANK_AUDIO]`, a microphone that sends only zeros is reported as one, and leading/trailing silence is trimmed before transcription.
- **Microphone switched off?** If Windows' microphone privacy switch is off for desktop apps, the press is refused with an error pill and Settings → Privacy & security → Microphone opens (at most once a minute).

The dashboard (Start Menu → Voisr, or the Desktop shortcut) launches to a system tray icon; closing its window leaves Voisr running in the tray. From the tray menu: **Dictate now**, **Open Dashboard**, and **Quit Voisr**.

### Dashboard

**Home** leads with a one-line push-to-talk status (`Hold <key> to dictate`) whose dot shows whether the key listener is actually live. Three tiles — **Time saved** (against typing at 40 wpm), **Words**, and **Streak** — each carry their own 14-day trend and week-over-week change; numbers count up when they change. If microphone access is off, the whole panel says so and links to the Settings page. A docked recorder at the bottom of Home records from the window itself when the key can't be used, with a live voice meter.

**Ctrl+K command palette** lets you start dictation, jump between views, change the push-to-talk key, toggle appearance, and copy the last dictation.

**History** supports Ctrl+F search with match highlighting, arrow-key navigation, Ctrl+C to copy, and Backspace to delete the selected row. It pages past 200 entries and can export to `.txt`, `.md`, or `.json`. Deleting a row shows an Undo toast instead of a confirmation dialog.

**Dictionary** entries can be edited in place instead of deleted and retyped.

**Settings** are grouped into Dictation, Push-to-talk, Appearance, and Privacy & Data sections, including launch-at-login.

## Configuration (`config.json`)

`%LOCALAPPDATA%\Voisr\config.json`:

```json
{
  "hotkey": "ctrl_r",
  "paste": true,
  "dictionary": {
    "trade mate": "TradeMate",
    "trademake": "TradeMate"
  },
  "model": null,
  "show_widget": true,
  "pill_theme": "classic",
  "pill_color": null,
  "launch_at_login": false,
  "appearance": "system",
  "check_for_updates": true
}
```

Add a dictionary entry for **every variant the ASR produces** for a term — dictate the word once, see what comes out, and map it. The dictionary's written forms are also passed to whisper as a glossary prompt (most recent 40 terms) to bias spelling.

A hand-edited value of the wrong type falls back to its default (the rest of the file is kept, and a dictionary keeps its valid entries). A file that isn't valid JSON at all is moved aside to `config.json.corrupt-<timestamp>` rather than overwritten, and the dashboard says so once on the next launch. Settings are written atomically.

**Why right Ctrl, not right Alt (Wispr Flow / the macOS build's default):** right Alt is AltGr on many keyboard layouts, and AltGr + V can produce a different character than plain V on those layouts, which corrupts an injected Ctrl+V paste. Right Ctrl avoids that.

### Updating

Voisr checks for a new release 10 seconds after launch and every 24 hours,
downloads it in the background, and shows an **Install** banner on the
dashboard. Nothing installs until you click it, and the update needs no admin
rights. Turn checking off in Settings → Privacy & data, where **Check now**
also works on demand.

This version check is the only network call Voisr makes. It sends no audio, no
text, and nothing identifying — it fetches a small JSON file describing the
latest release. Downloads are verified against a published SHA256 before
anything is executed.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
```

Removes the shortcuts, the launch-at-login registry entry, and `%LOCALAPPDATA%\Voisr\app`. It asks before deleting `config.json`/`history.jsonl` and keeps them by default; pass `-RemoveUserData` to delete them without asking.

## Known limitations

- **Dictation does not work over elevated windows** (Task Manager, an admin terminal, etc.). Windows' UIPI blocks input injection from an unelevated process into an elevated one. The failure is reported and your clipboard is preserved — not silent.
- **No Mica/acrylic backdrop on Windows 10** — the dashboard falls back to a flat background there. (Windows 11 gets the translucent effect.)
- **English-only by default** (`ggml-base.en`). Swap in a multilingual `ggml-*.bin` in `models\` and update `voisr\transcriber.py` if you need another language.
- **No GPU acceleration.** Transcription is CPU-only on every install; see [Download](#download) for the measured timing.
- **Windows Defender false positive.** Signature 1.455.474.0 (2 Aug 2026) flags Inno Setup-built executables as `Trojan:Win32/Wacatac.C!ml` — it fired even on a minimal probe installer, not just Voisr's ~155 MB payload. Employees downloading `VoisrSetup.exe` may see it quarantined outright, not merely warned about (compare the SmartScreen warning above, which the user can click through). Code signing (see "Possible future work") would likely resolve this along with the SmartScreen warning.

## Development

```powershell
venv\Scripts\python -m pytest -v
node --test tests/js/*.test.mjs
```

The two tests marked `slow` need **ffmpeg** on PATH (`winget install Gyan.FFmpeg`, then open a new shell). They synthesize speech with SAPI and resample it to 16 kHz through `voisr\audio.py`, which shells out to ffmpeg; the app itself doesn't — it records via `voisr\stream_recorder.py`. `scripts\setup.ps1` warns if ffmpeg is missing. To skip those two instead:

```powershell
venv\Scripts\python -m pytest -m "not slow"
```

`node --test tests/js/` (without the glob) fails on Node 24 — it treats the directory path as a module specifier instead of discovering the `.test.mjs` files inside it. Use the glob form.

## Building a release

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

One command, end to end: fetches the whisper.cpp build into `build\_whisper_cpu` (staged there so a release build doesn't depend on `scripts\setup.ps1` having populated `bin\`), freezes the app with PyInstaller (`build\voisr.spec`) into `build\dist\Voisr`, and compiles `build\voisr.iss` with Inno Setup into `build\Output\VoisrSetup.exe`. Requires:

- The dev venv set up (`scripts\setup.ps1`)
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`)

Neither `build\dist\` nor `build\Output\` is committed — see `.gitignore`.

**Code signing:** not done yet (no certificate). `scripts\build_release.ps1` has a commented-out `signtool` block right after the Inno Setup step — filling in a certificate path and re-enabling it is the only change needed once one is available.

## Publishing a release

This has not been run against real GitHub — `scripts\publish_release.ps1` is exercised only against a stubbed `gh`. No real release has been published this way, so treat the first one as a dry run.

1. Bump `__version__` in `voisr/__init__.py`.
2. Build, with the notes the updater will show in its banner:

```powershell
$env:VOISR_RELEASE_NOTES = "What changed, in one line."
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

3. Publish to the public release channel:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish_release.ps1
```

Releases live in [andr26j/voisr-releases](https://github.com/andr26j/voisr-releases) — public so installed copies can fetch updates without a token, while this source repo stays private. It holds only `VoisrSetup.exe` and `latest.json`.

`publish_release.ps1` refuses to publish if the tag exists, if `notes` is empty, or if the installer's SHA256 disagrees with `latest.json`. Installed copies verify that hash before running anything, so a mismatch means every client rejects the update.

## Possible future work

- **Code signing**, once a certificate exists (see above) — removes the SmartScreen warning. Would also let auto-update verify a signature instead of relying only on the published SHA256; see the Defender false positive under Known limitations, which the same certificate would likely resolve.
- **MSI/Intune packaging**, if the company needs managed deployment instead of a manually-run installer.
