param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("cpu", "cuda")]
    [string]$Variant,

    [Parameter(Mandatory = $true)]
    [string]$Destination
)

# Fetches a whisper.cpp release build (CPU/BLAS or CUDA) to $Destination.
$ErrorActionPreference = "Stop"

$WHISPER_TAG = "v1.9.1"
$ASSET = @{
    cpu  = "whisper-blas-bin-x64.zip"
    cuda = "whisper-cublas-12.4.0-bin-x64.zip"
}[$Variant]

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
