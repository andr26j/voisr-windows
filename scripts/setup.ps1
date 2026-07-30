# Voisr Windows setup: venv, dependencies, whisper.cpp CUDA build, model.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$MODEL     = "ggml-base.en.bin"
$MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL"

Write-Host "==> venv + dependencies"
if (-not (Test-Path "$root\venv")) { python -m venv "$root\venv" }
& "$root\venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$root\venv\Scripts\python.exe" -m pip install -q -r "$root\requirements.txt"

& "$PSScriptRoot\fetch_whisper.ps1" -Variant cuda -Destination "$root\bin"

Write-Host "==> model $MODEL"
New-Item -ItemType Directory -Force "$root\models" | Out-Null
if (-not (Test-Path "$root\models\$MODEL")) {
    Invoke-WebRequest -Uri $MODEL_URL -OutFile "$root\models\$MODEL"
}

Write-Host "==> done"
& "$root\bin\whisper-cli.exe" --help | Select-Object -First 3
