param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

# Fetches the whisper.cpp CPU/BLAS release build to $Destination. CPU is the
# only build Voisr ships: it runs on any machine, and the CUDA build it used
# to offer was a 1.1 GB payload that only helped the minority with an NVIDIA
# GPU. Dev and release builds now use the identical binaries.
#
# Voisr runs two of its programs: whisper-cli.exe (one process per
# dictation) and whisper-server.exe (the kept-loaded model, see
# voisr\whisper_server.py). Both are in the same zip and share its DLLs.
$ErrorActionPreference = "Stop"

$WHISPER_TAG = "v1.9.1"
$ASSET = "whisper-blas-bin-x64.zip"

Write-Host "==> whisper.cpp $WHISPER_TAG ($ASSET)"
New-Item -ItemType Directory -Force $Destination | Out-Null
$need = @("whisper-cli.exe", "whisper-server.exe")
if ($need | Where-Object { -not (Test-Path "$Destination\$_") }) {
    $zip = "$env:TEMP\$ASSET"
    $url = "https://github.com/ggml-org/whisper.cpp/releases/download/$WHISPER_TAG/$ASSET"
    Invoke-WebRequest -Uri $url -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath "$env:TEMP\whisper-extract" -Force
    Get-ChildItem "$env:TEMP\whisper-extract" -Recurse -Include *.exe,*.dll |
        ForEach-Object { Copy-Item $_.FullName "$Destination\" -Force }
    Remove-Item $zip, "$env:TEMP\whisper-extract" -Recurse -Force
}
foreach ($exe in $need) {
    if (-not (Test-Path "$Destination\$exe")) {
        throw "$exe missing after extract - check the release asset layout"
    }
}
