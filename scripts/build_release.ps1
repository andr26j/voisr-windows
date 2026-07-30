# One-command release build: fetch the CPU whisper build, freeze the app
# with PyInstaller, wrap it with Inno Setup, and report the result.
#
# Produces build\Output\VoisrSetup.exe -- unsigned (see the signtool block
# at the bottom for what changes once a certificate is available).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$buildDir = Join-Path $root "build"

# ---------------------------------------------------------------------------
# 1. CPU whisper build, staged separately from the repo's bin\ (CUDA, dev-only)
# ---------------------------------------------------------------------------
$whisperCpuDir = Join-Path $buildDir "_whisper_cpu"
Write-Host "==> Fetching CPU whisper build"
& "$PSScriptRoot\fetch_whisper.ps1" -Variant cpu -Destination $whisperCpuDir

# ---------------------------------------------------------------------------
# 2. Model. Shared between the CUDA and CPU builds -- fetched once by
#    setup.ps1 into the repo's models\, and build\voisr.spec bundles it
#    straight from there.
# ---------------------------------------------------------------------------
$model = "ggml-base.en.bin"
$modelPath = Join-Path $root "models\$model"
if (-not (Test-Path $modelPath)) {
    Write-Host "==> Fetching model $model"
    New-Item -ItemType Directory -Force (Join-Path $root "models") | Out-Null
    Invoke-WebRequest -Uri "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$model" -OutFile $modelPath
}

# ---------------------------------------------------------------------------
# 3. PyInstaller -- one-folder frozen bundle at build\dist\Voisr
# ---------------------------------------------------------------------------
Write-Host "==> Running PyInstaller"
$env:VOISR_WHISPER_CPU_DIR = $whisperCpuDir
& "$root\venv\Scripts\python.exe" -m PyInstaller (Join-Path $buildDir "voisr.spec") `
    --distpath (Join-Path $buildDir "dist") `
    --workpath (Join-Path $buildDir "work") `
    --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$frozenExe = Join-Path $buildDir "dist\Voisr\Voisr.exe"
if (-not (Test-Path $frozenExe)) { throw "Frozen build missing: $frozenExe" }

# ---------------------------------------------------------------------------
# 4. Inno Setup -- wrap the frozen bundle into VoisrSetup.exe
# ---------------------------------------------------------------------------
$isccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "ISCC.exe not found -- install Inno Setup 6 (winget install JRSoftware.InnoSetup)" }

Write-Host "==> Compiling installer with $iscc"
& $iscc (Join-Path $buildDir "voisr.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }

$installer = Join-Path $buildDir "Output\VoisrSetup.exe"
if (-not (Test-Path $installer)) { throw "Installer missing: $installer" }

# ---------------------------------------------------------------------------
# Unsigned for now. To sign once a certificate is available, uncomment and
# fill in the details -- nothing else in this script needs to change.
# ---------------------------------------------------------------------------
# & signtool sign /f "$env:VOISR_CERT_PATH" /p "$env:VOISR_CERT_PASSWORD" `
#     /tr http://timestamp.digicert.com /td sha256 /fd sha256 $installer

$sizeMb = [math]::Round((Get-Item $installer).Length / 1MB, 1)
Write-Host "==> Done: $installer ($sizeMb MB)"
