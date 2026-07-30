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
