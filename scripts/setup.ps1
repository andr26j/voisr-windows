# Voisr Windows setup: venv, dependencies, whisper.cpp build, model.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$MODEL     = "ggml-base.en.bin"
$MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL"

Write-Host "==> venv + dependencies"
if (-not (Test-Path "$root\venv")) { python -m venv "$root\venv" }
& "$root\venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$root\venv\Scripts\python.exe" -m pip install -q -r "$root\requirements.txt"

& "$PSScriptRoot\fetch_whisper.ps1" -Destination "$root\bin"

Write-Host "==> model $MODEL"
New-Item -ItemType Directory -Force "$root\models" | Out-Null
if (-not (Test-Path "$root\models\$MODEL")) {
    Invoke-WebRequest -Uri $MODEL_URL -OutFile "$root\models\$MODEL"
}

# ffmpeg is a test-only dependency: the app records via voisr.stream_recorder
# (sounddevice), but the e2e suite's spoken_wav fixture resamples SAPI output
# through voisr.audio.to_wav16k. Warn rather than fail -- dictation works
# without it. Mirrors find_binary(): bundled bin\ first, then PATH.
Write-Host "==> ffmpeg (tests only)"
$ffmpeg = if (Test-Path "$root\bin\ffmpeg.exe") { "$root\bin\ffmpeg.exe" }
          else { (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source }
if ($ffmpeg) {
    Write-Host "    found: $ffmpeg"
} else {
    Write-Warning ("ffmpeg not found. Dictation works without it, but the " +
        "2 tests marked 'slow' will fail. Install with: winget install Gyan.FFmpeg " +
        "(then open a new shell), or run: pytest -m `"not slow`"")
}

Write-Host "==> done"
& "$root\bin\whisper-cli.exe" --help | Select-Object -First 3
