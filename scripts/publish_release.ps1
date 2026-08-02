# Publishes build\Output\VoisrSetup.exe + latest.json as a GitHub release on
# the public release channel. Run scripts\build_release.ps1 first.
#
# The release is created as a DRAFT with both assets attached and published
# only once they are both up. A draft is invisible to
# releases/latest/download/..., so there is never a moment at which installed
# copies can see a release whose manifest or installer is missing -- including
# when a run dies half way, which leaves an invisible draft rather than a
# published release that 404s at every client until someone notices.
param(
    [string]$Repo = "andr26j/voisr-releases"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $root "build\Output"
$installer = Join-Path $outDir "VoisrSetup.exe"
$manifest = Join-Path $outDir "latest.json"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "gh not found on PATH -- install the GitHub CLI and run 'gh auth login'"
}

foreach ($f in @($installer, $manifest)) {
    if (-not (Test-Path $f)) { throw "$f missing -- run scripts\build_release.ps1 first" }
}

$data = Get-Content $manifest -Raw | ConvertFrom-Json
if (-not $data.notes) { throw "latest.json has empty notes; the updater rejects that manifest. Set VOISR_RELEASE_NOTES and rebuild." }
$tag = "v$($data.version)"

# Verify the manifest describes the installer actually being uploaded.
$actual = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $data.sha256) { throw "sha256 mismatch: latest.json says $($data.sha256), installer is $actual. Rebuild." }

# The download URL is baked into latest.json by build_release.ps1 from its own
# hardcoded copy of the repo slug, and voisr\updater.py pins a third copy in
# RELEASE_URL_PREFIX. Nothing makes those agree, so check here -- publishing to
# a $Repo the manifest does not point at yields a release whose clients all
# fail their download, and this is the one moment all three are in hand.
$expectedUrl = "https://github.com/$Repo/releases/download/$tag/VoisrSetup.exe"
if ($data.url -ne $expectedUrl) {
    throw "latest.json url is $($data.url) but publishing to $Repo expects $expectedUrl. The repo slug in scripts\build_release.ps1, voisr\updater.py and this script's -Repo default must agree."
}

# --- does the tag already exist? -------------------------------------------
# This guard is the only thing standing between a re-run and a clobbered
# release, so it judges by exit code, not by whether gh printed anything.
#
# Two traps, both measured on gh 2.93.0 / PowerShell 5.1:
#  * `if (gh release view ... 2>$null)` -- the obvious spelling -- is wrong in
#    the dangerous direction's mirror image: redirecting a native command's
#    stderr turns each line into an ErrorRecord, and under
#    $ErrorActionPreference = "Stop" that is a *terminating* error. So the
#    "tag is free" path throws "release not found" and the script never
#    publishes at all. Hence the local drop to Continue.
#  * gh exits 1 for "no such release" AND for "no such repo", a bad slug, and
#    every auth or network failure. Only the first means the tag is free, so
#    anything else is re-thrown: an unreachable GitHub must not be read as
#    permission to publish.
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $global:LASTEXITCODE = 0
    $viewOut = (gh release view $tag --repo $Repo 2>&1 | Out-String)
    $viewExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prev
}
if ($viewExit -eq 0) {
    throw "$tag already exists on $Repo (it may be an unpublished draft from a failed run -- check 'gh release list --repo $Repo'). Bump __version__ in voisr\__init__.py and rebuild, or delete the draft."
}
if ($viewOut -notmatch 'release not found') {
    throw "could not determine whether $tag exists on $Repo (gh exit $viewExit): $($viewOut.Trim())"
}

# --- release notes ----------------------------------------------------------
# Via a file, not `--notes $data.notes`. PowerShell 5.1 does not escape a
# double quote when it builds a native command line, so notes reading
# `Fixes the "Check now" button.` arrive at gh as two separate arguments with
# the quotes eaten -- gh then treats the stray one as an asset path. A note
# ending in a backslash is mangled the same way (it becomes a quote). Newlines,
# `$`, backticks and apostrophes all survive direct passing, but quotes are the
# likeliest thing a human writes, so route every note through a file.
$notesFile = Join-Path ([System.IO.Path]::GetTempPath()) "voisr-notes-$([guid]::NewGuid()).md"
[System.IO.File]::WriteAllText(
    $notesFile, [string]$data.notes, (New-Object System.Text.UTF8Encoding($false)))

try {
    Write-Host "==> Creating $tag on $Repo as a draft"
    gh release create $tag $installer $manifest --repo $Repo --title $tag --notes-file $notesFile --draft
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed (exit $LASTEXITCODE)" }

    Write-Host "==> Publishing $tag"
    gh release edit $tag --repo $Repo --draft=false
    if ($LASTEXITCODE -ne 0) {
        throw "gh release edit --draft=false failed (exit $LASTEXITCODE). The draft $tag exists with both assets but is not published; publish or delete it before re-running."
    }
} finally {
    Remove-Item $notesFile -Force -ErrorAction SilentlyContinue
}

Write-Host "==> Published: https://github.com/$Repo/releases/tag/$tag"
Write-Host "    Clients poll: https://github.com/$Repo/releases/latest/download/latest.json"
