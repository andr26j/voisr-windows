# Installs Voisr as a Start Menu / Desktop app.
#
# Runtime (code, venv, whisper binary, model) goes to
#   %LOCALAPPDATA%\Voisr\app
# Config and history already live one level up, at
#   %LOCALAPPDATA%\Voisr
# via voisr/paths.py, so they survive reinstalls untouched.
#
# Safe to re-run: skips the large bin/ and models/ copies when already
# present at the destination, and never overwrites an existing config.json
# or history.jsonl.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$appDir = Join-Path $env:LOCALAPPDATA "Voisr\app"

Write-Host "==> Installing runtime to $appDir"
New-Item -ItemType Directory -Force $appDir | Out-Null

# voisr/ package: mirror the tree, excluding __pycache__.
$destVoisr = Join-Path $appDir "voisr"
New-Item -ItemType Directory -Force $destVoisr | Out-Null
robocopy "$root\voisr" $destVoisr /MIR /XD __pycache__ /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed copying voisr/ (exit $LASTEXITCODE)"
}

Copy-Item "$root\requirements.txt" $appDir -Force
New-Item -ItemType Directory -Force (Join-Path $appDir "assets") | Out-Null
Copy-Item "$root\assets\voisr.ico" (Join-Path $appDir "assets\voisr.ico") -Force

# bin/ (whisper-cli.exe + BLAS DLLs) and models/: only copy
# files that are missing at the destination, never re-copy what's there.
Write-Host "==> Syncing bin/ and models/ (skipping files already present)"
foreach ($pair in @(@{src = "bin"; dst = "bin" }, @{src = "models"; dst = "models" })) {
    $srcDir = Join-Path $root $pair.src
    $dstDir = Join-Path $appDir $pair.dst
    if (-not (Test-Path $srcDir)) { continue }
    New-Item -ItemType Directory -Force $dstDir | Out-Null
    Get-ChildItem $srcDir -File | ForEach-Object {
        $target = Join-Path $dstDir $_.Name
        if (-not (Test-Path $target)) {
            Copy-Item $_.FullName $target
        }
    }
}

Write-Host "==> Creating venv"
$venvPython = Join-Path $appDir "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    python -m venv "$appDir\venv"
}
& "$venvPython" -m pip install -q --upgrade pip
& "$venvPython" -m pip install -q -r "$appDir\requirements.txt"

Write-Host "==> Creating shortcuts"
$pythonw = Join-Path $appDir "venv\Scripts\pythonw.exe"
$icon = Join-Path $appDir "assets\voisr.ico"
$shell = New-Object -ComObject WScript.Shell

$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$targets = @(
    (Join-Path $startMenuDir "Voisr.lnk"),
    (Join-Path $env:USERPROFILE "Desktop\Voisr.lnk")
)
foreach ($lnkPath in $targets) {
    $shortcut = $shell.CreateShortcut($lnkPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = "-m voisr.ui"
    $shortcut.WorkingDirectory = $appDir
    $shortcut.IconLocation = $icon
    $shortcut.Save()
    Write-Host "    $lnkPath"
}

Write-Host "==> Done."
Write-Host "    Installed to: $appDir"
Write-Host "    Launch from the Start Menu ('Voisr') or the Desktop shortcut."
Write-Host "    Config and history live in: $(Join-Path $env:LOCALAPPDATA 'Voisr')"
Write-Host "    To uninstall: powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1"
