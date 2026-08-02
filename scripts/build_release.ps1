# One-command release build: fetch the CPU whisper build, freeze the app
# with PyInstaller, wrap it with Inno Setup, and report the result.
#
# Produces build\Output\VoisrSetup.exe -- unsigned (see the signtool block
# at the bottom for what changes once a certificate is available).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$buildDir = Join-Path $root "build"

# Single source of truth: voisr/__init__.py. Passed to ISCC below so the
# installer's version can never drift from the app's.
$versionLine = Select-String -Path (Join-Path $root "voisr\__init__.py") -Pattern '^__version__\s*=\s*"([^"]+)"'
if (-not $versionLine) { throw "__version__ not found in voisr\__init__.py" }
$appVersion = $versionLine.Matches[0].Groups[1].Value
Write-Host "==> Version $appVersion"

# ---------------------------------------------------------------------------
# 1. whisper build, staged under build\ rather than reused from the repo's
#    bin\ so a release never depends on setup.ps1 having been run first.
#    Same CPU/BLAS binaries either way.
# ---------------------------------------------------------------------------
$whisperCpuDir = Join-Path $buildDir "_whisper_cpu"
Write-Host "==> Fetching whisper build"
& "$PSScriptRoot\fetch_whisper.ps1" -Destination $whisperCpuDir

# ---------------------------------------------------------------------------
# 2. Model. Fetched once by setup.ps1 into the repo's models\, and
#    build\voisr.spec bundles it straight from there.
# ---------------------------------------------------------------------------
$model = "ggml-base.en.bin"
$modelPath = Join-Path $root "models\$model"
if (-not (Test-Path $modelPath)) {
    Write-Host "==> Fetching model $model"
    New-Item -ItemType Directory -Force (Join-Path $root "models") | Out-Null
    Invoke-WebRequest -Uri "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$model" -OutFile $modelPath
}

# ---------------------------------------------------------------------------
# 3. WebView2 Evergreen Bootstrapper. build\voisr.iss embeds this (dontcopy)
#    and runs it silently when the runtime is missing, so ISCC needs it
#    present at compile time. ~2 MB; it downloads the runtime itself.
# ---------------------------------------------------------------------------
$webview2 = Join-Path $buildDir "MicrosoftEdgeWebview2Setup.exe"
if (-not (Test-Path $webview2)) {
    Write-Host "==> Fetching WebView2 bootstrapper"
    Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $webview2
}

# ---------------------------------------------------------------------------
# 4. PyInstaller -- one-folder frozen bundle at build\dist\Voisr
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
# 5. Inno Setup -- wrap the frozen bundle into VoisrSetup.exe
# ---------------------------------------------------------------------------
$isccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "ISCC.exe not found -- install Inno Setup 6 (winget install JRSoftware.InnoSetup)" }

Write-Host "==> Compiling installer with $iscc"
& $iscc "/DMyAppVersion=$appVersion" (Join-Path $buildDir "voisr.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }

$installer = Join-Path $buildDir "Output\VoisrSetup.exe"
if (-not (Test-Path $installer)) { throw "Installer missing: $installer" }

# ---------------------------------------------------------------------------
# Unsigned for now. To sign once a certificate is available, uncomment and
# fill in the details -- nothing else in this script needs to change.
# ---------------------------------------------------------------------------
# & signtool sign /f "$env:VOISR_CERT_PATH" /p "$env:VOISR_CERT_PASSWORD" `
#     /tr http://timestamp.digicert.com /td sha256 /fd sha256 $installer

$sha = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()
$manifestPath = Join-Path $buildDir "Output\latest.json"
$manifestJson = @{
    version = $appVersion
    url     = "https://github.com/andr26j/voisr-releases/releases/download/v$appVersion/VoisrSetup.exe"
    sha256  = $sha
    notes   = $env:VOISR_RELEASE_NOTES
} | ConvertTo-Json

# Write UTF-8 WITHOUT a BOM. Do not use Set-Content -Encoding utf8: on
# Windows PowerShell 5.1 that emits a BOM, and a BOM makes Python's
# json.loads reject the manifest ("Unexpected UTF-8 BOM"). The client
# decodes with utf-8-sig so it survives one either way, but emitting a BOM
# here would leave every published release silently invisible to any client
# that did not.
[System.IO.File]::WriteAllText(
    $manifestPath, $manifestJson, (New-Object System.Text.UTF8Encoding($false)))

$sizeMb = [math]::Round((Get-Item $installer).Length / 1MB, 1)
Write-Host "==> Done: $installer ($sizeMb MB)"
Write-Host "    sha256:   $sha"
Write-Host "    manifest: $manifestPath"
if (-not $env:VOISR_RELEASE_NOTES) {
    Write-Warning "VOISR_RELEASE_NOTES was not set, so latest.json has empty notes. The updater rejects a manifest with an empty field -- set it and rebuild before publishing."
}
