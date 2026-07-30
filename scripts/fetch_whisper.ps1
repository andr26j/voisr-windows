param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

# Fetches the whisper.cpp CPU/BLAS release build to $Destination. CPU is the
# only build Voisr ships: it runs on any machine, and the CUDA build it used
# to offer was a 1.1 GB payload that only helped the minority with an NVIDIA
# GPU. Dev and release builds now use the identical binaries.
$ErrorActionPreference = "Stop"

$WHISPER_TAG = "v1.9.1"
$ASSET = "whisper-blas-bin-x64.zip"

Write-Host "==> whisper.cpp $WHISPER_TAG ($ASSET)"
New-Item -ItemType Directory -Force $Destination | Out-Null
if (-not (Test-Path "$Destination\whisper-cli.exe")) {
    $zip = "$env:TEMP\$ASSET"
    $url = "https://github.com/ggml-org/whisper.cpp/releases/download/$WHISPER_TAG/$ASSET"
    Invoke-WebRequest -Uri $url -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath "$env:TEMP\whisper-extract" -Force
    Get-ChildItem "$env:TEMP\whisper-extract" -Recurse -Include *.exe,*.dll |
        ForEach-Object { Copy-Item $_.FullName "$Destination\" -Force }
    Remove-Item $zip, "$env:TEMP\whisper-extract" -Recurse -Force
}
if (-not (Test-Path "$Destination\whisper-cli.exe")) {
    throw "whisper-cli.exe missing after extract - check the release asset layout"
}
