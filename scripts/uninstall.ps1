# Uninstalls Voisr: removes shortcuts, the launch-at-login Run key entry,
# and the installed runtime at %LOCALAPPDATA%\Voisr\app.
#
# User data (config.json, history.jsonl, at %LOCALAPPDATA%\Voisr) is kept
# by default -- deleting someone's dictation history without asking is not
# acceptable. Pass -RemoveUserData to remove it without prompting.
param(
    [switch]$RemoveUserData
)
$ErrorActionPreference = "Stop"

$appDir = Join-Path $env:LOCALAPPDATA "Voisr\app"
$dataDir = Join-Path $env:LOCALAPPDATA "Voisr"

Write-Host "==> Removing shortcuts"
$shortcuts = @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Voisr.lnk"),
    (Join-Path $env:USERPROFILE "Desktop\Voisr.lnk")
)
foreach ($lnkPath in $shortcuts) {
    if (Test-Path $lnkPath) {
        Remove-Item $lnkPath -Force
        Write-Host "    removed $lnkPath"
    }
}

Write-Host "==> Removing launch-at-login registry entry"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (Get-ItemProperty -Path $runKey -Name "Voisr" -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $runKey -Name "Voisr"
    Write-Host "    removed Voisr from $runKey"
} else {
    Write-Host "    no Voisr entry found"
}

Write-Host "==> Removing installed app"
if (Test-Path $appDir) {
    Remove-Item $appDir -Recurse -Force
    Write-Host "    removed $appDir"
} else {
    Write-Host "    $appDir not present"
}

$configPath = Join-Path $dataDir "config.json"
$historyPath = Join-Path $dataDir "history.jsonl"
$hasUserData = (Test-Path $configPath) -or (Test-Path $historyPath)

if ($hasUserData) {
    $removeData = $RemoveUserData.IsPresent
    if (-not $removeData) {
        $answer = Read-Host "Remove user data (config and dictation history) at $dataDir too? [y/N]"
        $removeData = $answer -match '^[Yy]'
    }
    if ($removeData) {
        Remove-Item $dataDir -Recurse -Force
        Write-Host "==> Removed user data at $dataDir"
    } else {
        Write-Host "==> Kept user data at $dataDir"
    }
} else {
    Write-Host "==> No user data found at $dataDir"
}

Write-Host "==> Done."
