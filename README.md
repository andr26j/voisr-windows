# Voisr (Windows)

A local-only Windows port of [Wispr Flow](https://wisprflow.ai)'s core loop: **hold a key, speak, and clean text appears at your cursor** — in any app. All processing happens on your machine (whisper.cpp, GPU-accelerated); no audio or text ever leaves it.

## What it does

1. **Capture** — records your mic while you hold the hotkey
2. **Transcribe** — on-device speech-to-text via whisper.cpp (`ggml-base.en`), CUDA-accelerated
3. **Clean up** — strips filler words (um, uh, hmm…), normalizes spacing/punctuation, capitalizes, and applies your custom dictionary (e.g. `"trademake" → "TradeMate"` for names the ASR mishears)
4. **Insert** — pastes the result at the cursor of the frontmost window (Ctrl+V via `SendInput`), restoring your clipboard afterwards. If the paste is blocked, your original clipboard is preserved and the failure is reported, not silent. The dictation is recorded to history either way.

## Setup

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Creates the dev venv, downloads the CUDA whisper.cpp build into `bin\`, and downloads the `ggml-base.en` model (~141 MB) into `models\`.

## Install (Start Menu app)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

This copies the runtime to `%LOCALAPPDATA%\Voisr\app` (code, whisper binary, model, and a dedicated venv) and creates Start Menu and Desktop shortcuts. It is **not** a distributable installer — it installs from this working checkout, so `scripts\setup.ps1` must have already populated `bin\` and `models\`. Someone without this repo and a Python install can't run it; see [Possible future work](#possible-future-work).

The installer is safe to re-run: it skips re-copying `bin\` and `models\` when they already exist at the destination (both are large), and never touches an existing `config.json` or `history.jsonl`. Re-run it after pulling code changes to update the installed copy.

Config and history live in `%LOCALAPPDATA%\Voisr\config.json` / `history.jsonl` — one level above the installed app, so they survive reinstalls and even an uninstall (see below).

For development, run the dashboard straight from the repo instead:

```powershell
venv\Scripts\python -m voisr.ui
```

## Usage

Hold **right Ctrl** to dictate, speak, release — the transcription is cleaned up and pasted at your cursor. A floating pill shows recording/transcribing state near your cursor.

The dashboard (Start Menu → Voisr, or the Desktop shortcut) launches to a system tray icon; closing its window leaves Voisr running in the tray. From the tray menu: **Dictate now**, **Open Dashboard**, and **Quit Voisr**.

### Dashboard

**Home** leads with your push-to-talk key rendered as a keycap (`Hold <key> to dictate`) and a headline stat showing **Time saved** (measured against typing at 40 wpm) with a 14-day sparkline.

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
  "appearance": "system"
}
```

Add a dictionary entry for **every variant the ASR produces** for a term — dictate the word once, see what comes out, and map it.

**Why right Ctrl, not right Alt (Wispr Flow / the macOS build's default):** right Alt is AltGr on many keyboard layouts, and AltGr + V can produce a different character than plain V on those layouts, which corrupts an injected Ctrl+V paste. Right Ctrl avoids that.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
```

Removes the shortcuts, the launch-at-login registry entry, and `%LOCALAPPDATA%\Voisr\app`. It asks before deleting `config.json`/`history.jsonl` and keeps them by default; pass `-RemoveUserData` to delete them without asking.

## Known limitations

- **Dictation does not work over elevated windows** (Task Manager, an admin terminal, etc.). Windows' UIPI blocks input injection from an unelevated process into an elevated one. The failure is reported and your clipboard is preserved — not silent.
- **No Mica/acrylic backdrop on Windows 10** — the dashboard falls back to a flat background there. (Windows 11 gets the translucent effect.)
- **English-only by default** (`ggml-base.en`). Swap in a multilingual `ggml-*.bin` in `models\` and update `voisr\transcriber.py` if you need another language.
- **Requires an NVIDIA GPU.** The bundled whisper.cpp build is cuBLAS/CUDA. A CPU build of whisper.cpp exists upstream but is slower and isn't what `scripts\setup.ps1` downloads.

## Development

```powershell
venv\Scripts\python -m pytest -v
node --test tests/js/*.test.mjs
```

`node --test tests/js/` (without the glob) fails on Node 24 — it treats the directory path as a module specifier instead of discovering the `.test.mjs` files inside it. Use the glob form.

## Possible future work

A distributable `.exe` that runs without this repo or a separate Python install would need PyInstaller (which has friction with `sounddevice`'s PortAudio DLL, `pythonnet`/CLR, and WebView2) plus an installer builder like Inno Setup, and would have to bundle or fetch the 141 MB model on first run. Out of scope here; `install.ps1` installs from a working checkout only.
