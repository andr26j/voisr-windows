# Auto-update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Voisr notices a new release, downloads and verifies it in the background, and installs it when the user clicks Install — with no admin rights and no interrupted dictation.

**Architecture:** A new `voisr/updater.py` holds small injectable units (version compare, manifest parse, fetch, download, hash verify) plus an `UpdateChecker` that owns the schedule and a state machine. The dashboard reads that state and renders a banner; it never drives the sequence. Applying an update re-runs the existing Inno Setup installer silently with a new `/RELAUNCH=1` switch.

**Tech Stack:** Python 3.14 (stdlib only — `urllib.request`, `hashlib`, `threading`, `subprocess`), pywebview bridge, vanilla JS dashboard, Inno Setup 6, PowerShell, `gh` CLI.

**Spec:** `docs/superpowers/specs/2026-07-31-auto-update-design.md`

---

## Conventions used throughout

- Python tests: `venv\Scripts\python -m pytest tests/test_updater.py -v`
- JS tests: `node --test tests/js/*.test.mjs` (the glob is required — a bare directory fails on Node 24)
- Full suites before every commit: `venv\Scripts\python -m pytest -q` and the JS command above
- The repo has **396 Python + 133 JS tests passing** at plan time. Any task that lowers either number is a regression, not progress.
- Injected collaborators everywhere: no test may touch the network or spawn an installer.

## File Structure

**Create:**

| File | Responsibility |
| --- | --- |
| `voisr/updater.py` | Version compare, manifest parse/validate, fetch, download, hash verify, `UpdateChecker` state machine |
| `tests/test_updater.py` | All Python coverage for the above, fakes only |
| `scripts/publish_release.ps1` | Compute SHA256, write `latest.json`, create the GitHub release |
| `tests/js/update.test.mjs` | Banner rendering and Install wiring |

**Modify:**

| File | Change |
| --- | --- |
| `voisr/__init__.py` | Add `__version__` |
| `voisr/app.py:27-36` | Add `check_for_updates: True` to `DEFAULT_CONFIG` |
| `voisr/ui.py` | Construct `UpdateChecker`; add four `Api` methods |
| `voisr/web/index.html` | Banner markup, two settings rows, fix the false privacy copy at line 304 |
| `voisr/web/js/views/home.js` | Render the banner |
| `voisr/web/js/views/settings.js` | Toggle, Check now, version label |
| `voisr/web/js/api.js` | Dev mocks for the four new bridge methods |
| `build/voisr.iss` | `MyAppVersion` from define, `/RELAUNCH=1`, `CloseApplications` |
| `scripts/build_release.ps1` | Read `__version__`, pass `/DMyAppVersion`, emit `latest.json` + SHA256 |
| `README.md` | Release process, the one network call, the toggle |

---

### Task 1: Version as a single source of truth

**Why first:** every later task compares against `__version__`. If the app and installer can disagree about the current version, an update loop can reinstall the same build forever.

**Files:**
- Modify: `voisr/__init__.py`
- Modify: `build/voisr.iss:19`
- Modify: `scripts/build_release.ps1`
- Test: `tests/test_updater.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_updater.py`:

```python
import voisr
from voisr.updater import parse_version


def test_package_exposes_a_parseable_version():
    assert isinstance(voisr.__version__, str)
    assert parse_version(voisr.__version__) >= (1, 0, 0)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voisr.updater'`

- [ ] **Step 3: Add the version and a stub `parse_version`**

In `voisr/__init__.py`, add at the top:

```python
__version__ = "1.0.0"
```

Create `voisr/updater.py`:

```python
"""Auto-update: check a published manifest, fetch the installer, hand off.

Every step takes its collaborator as an argument -- the HTTP opener, the
downloader, the process spawner -- so the whole module is testable without
network access and without ever executing an installer.
"""


def parse_version(text) -> tuple[int, ...]:
    """'1.10.0' -> (1, 10, 0). Tolerates a leading 'v'."""
    parts = str(text).strip().lstrip("vV").split(".")
    if not parts or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(p) for p in parts)
```

- [ ] **Step 4: Run it and watch it pass**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: PASS (1 test)

- [ ] **Step 5: Make the installer read the version instead of hardcoding it**

In `build/voisr.iss`, replace line 19 (`#define MyAppVersion "1.0.0"`) with:

```
; Version comes from voisr/__init__.py via scripts\build_release.ps1
; (/DMyAppVersion=...). The fallback only applies to a manual ISCC run.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
```

In `scripts/build_release.ps1`, immediately after the `$buildDir` assignment near the top, add:

```powershell
# Single source of truth: voisr/__init__.py. Passed to ISCC below so the
# installer's version can never drift from the app's.
$versionLine = Select-String -Path (Join-Path $root "voisr\__init__.py") -Pattern '^__version__\s*=\s*"([^"]+)"'
if (-not $versionLine) { throw "__version__ not found in voisr\__init__.py" }
$appVersion = $versionLine.Matches[0].Groups[1].Value
Write-Host "==> Version $appVersion"
```

Then change the ISCC invocation (the `& $iscc (Join-Path $buildDir "voisr.iss")` line) to:

```powershell
& $iscc "/DMyAppVersion=$appVersion" (Join-Path $buildDir "voisr.iss")
```

- [ ] **Step 6: Verify the wiring by building the installer**

Run: `powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1`
Expected: prints `==> Version 1.0.0`, then `==> Done: ...VoisrSetup.exe (about 155 MB)`

Confirm the version reached the installer:

Run: `(Get-Item build\Output\VoisrSetup.exe).VersionInfo.ProductVersion`
Expected: `1.0.0` (not `0.0.0`)

- [ ] **Step 7: Commit**

```bash
git add voisr/__init__.py voisr/updater.py tests/test_updater.py build/voisr.iss scripts/build_release.ps1
git commit -m "feat: single version source of truth for app and installer"
```

---

### Task 2: Version comparison

**Files:**
- Modify: `voisr/updater.py`
- Test: `tests/test_updater.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updater.py`:

```python
import pytest

from voisr.updater import is_newer


def test_parse_version_rejects_junk():
    for bad in ["", "abc", "1.x.0", "1..0", None]:
        with pytest.raises(ValueError):
            parse_version(bad)


def test_is_newer_compares_numerically_not_as_text():
    # The bug this guards: "1.10.0" < "1.9.0" as strings.
    assert is_newer("1.10.0", "1.9.0")
    assert not is_newer("1.9.0", "1.10.0")


def test_is_newer_is_false_for_equal_and_older():
    assert not is_newer("1.0.0", "1.0.0")
    assert not is_newer("0.9.0", "1.0.0")


def test_is_newer_pads_shorter_versions():
    assert not is_newer("1.1", "1.1.0")
    assert is_newer("1.1.1", "1.1")
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_newer'`

- [ ] **Step 3: Implement**

Append to `voisr/updater.py`:

```python
def is_newer(remote, local) -> bool:
    """True when `remote` is a strictly higher version than `local`.

    Pads the shorter side with zeros so "1.1" and "1.1.0" compare equal
    rather than by length.
    """
    r, l = parse_version(remote), parse_version(local)
    width = max(len(r), len(l))
    r += (0,) * (width - len(r))
    l += (0,) * (width - len(l))
    return r > l
```

- [ ] **Step 4: Run and watch them pass**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add voisr/updater.py tests/test_updater.py
git commit -m "feat: numeric version comparison for update checks"
```

---

### Task 3: Manifest parsing and validation

The build is unsigned, so the manifest is the only thing standing between a download and executing an `.exe`. Validation is strict on purpose: a manifest that half-works is worse than one that is rejected.

**Files:**
- Modify: `voisr/updater.py`
- Test: `tests/test_updater.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updater.py`:

```python
import json

from voisr.updater import RELEASE_URL_PREFIX, Manifest

GOOD_SHA = "a" * 64


def _manifest_dict(**overrides):
    data = {
        "version": "1.1.0",
        "url": RELEASE_URL_PREFIX + "v1.1.0/VoisrSetup.exe",
        "sha256": GOOD_SHA,
        "notes": "Adds auto-update.",
    }
    data.update(overrides)
    return data


def test_manifest_parses_a_good_payload():
    m = Manifest.from_json(json.dumps(_manifest_dict()))
    assert m.version == "1.1.0"
    assert m.sha256 == GOOD_SHA
    assert m.notes == "Adds auto-update."


def test_manifest_requires_every_field():
    for missing in ("version", "url", "sha256", "notes"):
        data = _manifest_dict()
        del data[missing]
        with pytest.raises(ValueError, match=missing):
            Manifest.from_json(json.dumps(data))


def test_manifest_rejects_a_url_outside_the_release_channel():
    # A tampered manifest must not be able to point the installer download
    # at an attacker's host.
    for bad in [
        "https://evil.example.com/VoisrSetup.exe",
        "http://github.com/andr26j/voisr-releases/releases/download/v1/VoisrSetup.exe",
        "https://github.com/andr26j/voisr-releases/../evil/VoisrSetup.exe",
    ]:
        with pytest.raises(ValueError, match="release channel"):
            Manifest.from_json(json.dumps(_manifest_dict(url=bad)))


def test_manifest_rejects_a_malformed_sha256():
    for bad in ["", "abc", "z" * 64, GOOD_SHA[:-1]]:
        with pytest.raises(ValueError):
            Manifest.from_json(json.dumps(_manifest_dict(sha256=bad)))


def test_manifest_rejects_an_unparseable_version():
    with pytest.raises(ValueError):
        Manifest.from_json(json.dumps(_manifest_dict(version="next")))


def test_manifest_rejects_a_non_object_payload():
    with pytest.raises(ValueError):
        Manifest.from_json("[]")
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: FAIL — `ImportError: cannot import name 'RELEASE_URL_PREFIX'`

- [ ] **Step 3: Implement**

Add these imports at the top of `voisr/updater.py`:

```python
import json
from dataclasses import dataclass
```

Then append:

```python
# The public release channel. Source stays in a private repo; a token shipped
# inside the bundle would be extractable by anyone who installs it.
MANIFEST_URL = (
    "https://github.com/andr26j/voisr-releases/releases/latest/download/latest.json"
)
RELEASE_URL_PREFIX = (
    "https://github.com/andr26j/voisr-releases/releases/download/"
)
_HEX = set("0123456789abcdef")


@dataclass(frozen=True)
class Manifest:
    version: str
    url: str
    sha256: str
    notes: str

    @classmethod
    def from_json(cls, raw) -> "Manifest":
        """Parse and fully validate a manifest. Raises ValueError on anything
        suspect -- callers turn that into "no update", never a partial one."""
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(data, dict):
            raise ValueError("manifest must be a JSON object")

        for field in ("version", "url", "sha256", "notes"):
            if not data.get(field):
                raise ValueError(f"manifest missing {field}")

        parse_version(data["version"])  # raises if unparseable

        url = str(data["url"])
        # Pinning the prefix also rejects http:// and any host swap, so a
        # tampered manifest cannot redirect the download elsewhere.
        if not url.startswith(RELEASE_URL_PREFIX) or ".." in url:
            raise ValueError(f"url is outside the release channel: {url}")

        sha = str(data["sha256"]).strip().lower()
        if len(sha) != 64 or not set(sha) <= _HEX:
            raise ValueError("sha256 must be 64 hex characters")

        return cls(version=str(data["version"]), url=url, sha256=sha,
                   notes=str(data["notes"]))
```

- [ ] **Step 4: Run and watch them pass**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add voisr/updater.py tests/test_updater.py
git commit -m "feat: strict update manifest validation"
```

---

### Task 4: Fetching the manifest

**Files:**
- Modify: `voisr/updater.py`
- Test: `tests/test_updater.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updater.py`:

```python
from voisr.updater import fetch_manifest


def test_fetch_manifest_returns_a_parsed_manifest():
    captured = {}

    def opener(url):
        captured["url"] = url
        return json.dumps(_manifest_dict())

    m = fetch_manifest("https://example.test/latest.json", opener=opener)
    assert m.version == "1.1.0"
    assert captured["url"] == "https://example.test/latest.json"


def test_fetch_manifest_returns_none_when_there_are_no_releases_yet():
    # A 404 is the normal "nothing published" state, not an error.
    def opener(url):
        raise OSError("HTTP Error 404: Not Found")

    assert fetch_manifest("https://example.test/latest.json", opener=opener) is None


def test_fetch_manifest_returns_none_on_network_failure():
    def opener(url):
        raise TimeoutError("timed out")

    assert fetch_manifest("https://example.test/latest.json", opener=opener) is None


def test_fetch_manifest_returns_none_on_malformed_payload():
    for payload in ["not json", "{}", json.dumps(_manifest_dict(url="https://evil.test/x.exe"))]:
        assert fetch_manifest("https://example.test/latest.json",
                              opener=lambda url, p=payload: p) is None
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_manifest'`

- [ ] **Step 3: Implement**

Add to the imports in `voisr/updater.py`:

```python
import urllib.request
```

Append:

```python
_TIMEOUT_S = 10


def _default_text_opener(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response:
        return response.read().decode("utf-8")


def fetch_manifest(url: str = MANIFEST_URL, opener=None) -> "Manifest | None":
    """The manifest, or None if there isn't a usable one.

    None covers every failure a background check can hit -- no releases yet
    (404), offline, timeout, malformed JSON, failed validation. None of those
    is worth interrupting the user for, so they are logged and swallowed.
    """
    opener = opener or _default_text_opener
    try:
        raw = opener(url)
    except Exception as e:
        print(f"voisr: update check failed: {e}", flush=True)
        return None
    try:
        return Manifest.from_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"voisr: ignoring malformed update manifest: {e}", flush=True)
        return None
```

- [ ] **Step 4: Run and watch them pass**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
git add voisr/updater.py tests/test_updater.py
git commit -m "feat: fetch and validate the update manifest"
```

---

### Task 5: Downloading and hash verification

**Files:**
- Modify: `voisr/updater.py`
- Test: `tests/test_updater.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updater.py`:

```python
import hashlib

from voisr.updater import download_to, verify_sha256


def test_download_writes_the_payload_and_leaves_no_part_file(tmp_path):
    dest = tmp_path / "VoisrSetup-1.1.0.exe"
    download_to("https://example.test/x.exe", dest, opener=lambda url: b"payload")
    assert dest.read_bytes() == b"payload"
    assert list(tmp_path.iterdir()) == [dest]


def test_interrupted_download_leaves_no_partial_file_behind(tmp_path):
    # A half-written installer that looked complete would be executed.
    dest = tmp_path / "VoisrSetup-1.1.0.exe"

    def opener(url):
        raise OSError("connection reset")

    with pytest.raises(OSError):
        download_to("https://example.test/x.exe", dest, opener=opener)
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_creates_the_destination_directory(tmp_path):
    dest = tmp_path / "updates" / "VoisrSetup-1.1.0.exe"
    download_to("https://example.test/x.exe", dest, opener=lambda url: b"x")
    assert dest.is_file()


def test_verify_sha256_accepts_a_matching_digest(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"payload")
    assert verify_sha256(f, hashlib.sha256(b"payload").hexdigest())


def test_verify_sha256_rejects_a_mismatch(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"payload")
    assert not verify_sha256(f, "b" * 64)


def test_verify_sha256_ignores_digest_case_and_whitespace(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    assert verify_sha256(f, f"  {digest.upper()}  ")
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: FAIL — `ImportError: cannot import name 'download_to'`

- [ ] **Step 3: Implement**

Add to the imports in `voisr/updater.py`:

```python
import hashlib
from pathlib import Path
```

Append:

```python
_CHUNK = 1024 * 1024


def _default_bytes_opener(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response:
        return response.read()


def download_to(url: str, dest, opener=None) -> Path:
    """Download to `dest`, writing a .part file first.

    The rename is the commit point: `dest` either does not exist or is a
    complete download. A truncated file at `dest` would later be executed.
    """
    opener = opener or _default_bytes_opener
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        part.write_bytes(opener(url))
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return dest


def verify_sha256(path, expected: str) -> bool:
    """Whether `path` hashes to `expected`. The build is unsigned, so this is
    the only integrity check between downloading and executing an .exe."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest() == str(expected).strip().lower()
```

- [ ] **Step 4: Run and watch them pass**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: PASS (21 tests)

- [ ] **Step 5: Commit**

```bash
git add voisr/updater.py tests/test_updater.py
git commit -m "feat: atomic installer download with sha256 verification"
```

---

### Task 6: UpdateChecker state machine

**Files:**
- Modify: `voisr/updater.py`
- Test: `tests/test_updater.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updater.py`:

```python
from voisr.updater import UpdateChecker


def _checker(tmp_path, *, manifest=None, enabled=True, payload=b"installer",
             sha=None, spawned=None, current="1.0.0"):
    """An UpdateChecker with every collaborator faked: no network, no spawn."""
    if manifest is None:
        manifest = Manifest.from_json(json.dumps(_manifest_dict(
            sha256=hashlib.sha256(payload).hexdigest())))
    if sha is not None:
        manifest = Manifest(manifest.version, manifest.url, sha, manifest.notes)
    calls = {"fetch": 0, "download": 0}

    def fake_fetch(url, opener=None):
        calls["fetch"] += 1
        return manifest

    def fake_download(url, dest, opener=None):
        calls["download"] += 1
        return download_to(url, dest, opener=lambda _u: payload)

    checker = UpdateChecker(
        current_version=current,
        updates_dir=tmp_path,
        config_reader=lambda: {"check_for_updates": enabled},
        fetch=fake_fetch,
        download=fake_download,
        spawn=(spawned.append if spawned is not None else (lambda argv: None)),
    )
    return checker, calls


def test_a_newer_release_ends_in_ready_with_the_installer_on_disk(tmp_path):
    checker, calls = _checker(tmp_path)
    result = checker.check_now()
    assert result["state"] == "ready"
    assert result["version"] == "1.1.0"
    assert checker.status()["notes"] == "Adds auto-update."
    assert (tmp_path / "VoisrSetup-1.1.0.exe").is_file()
    assert calls["download"] == 1


def test_the_same_version_is_not_an_update(tmp_path):
    checker, calls = _checker(tmp_path, current="1.1.0")
    result = checker.check_now()
    assert result["state"] == "idle"
    assert result["up_to_date"] is True
    assert calls["download"] == 0


def test_no_releases_yet_is_not_an_error(tmp_path):
    checker, _ = _checker(tmp_path)
    checker._fetch = lambda url, opener=None: None
    result = checker.check_now()
    assert result["state"] == "idle"
    assert result["up_to_date"] is True


def test_a_hash_mismatch_deletes_the_download_and_never_offers_it(tmp_path):
    checker, _ = _checker(tmp_path, sha="b" * 64)
    result = checker.check_now()
    assert result["state"] == "idle"
    assert "checksum" in result["error"].lower()
    assert list(tmp_path.iterdir()) == []
    assert checker.status()["state"] == "idle"


def test_the_toggle_being_off_means_no_network_call_at_all(tmp_path):
    checker, calls = _checker(tmp_path, enabled=False)
    result = checker.check_now()
    assert result["state"] == "idle"
    assert calls["fetch"] == 0


def test_check_now_forced_ignores_the_toggle(tmp_path):
    # The Settings "Check now" button must work even with auto-check off.
    checker, calls = _checker(tmp_path, enabled=False)
    result = checker.check_now(force=True)
    assert result["state"] == "ready"
    assert calls["fetch"] == 1


def test_an_already_downloaded_installer_is_not_fetched_twice(tmp_path):
    checker, calls = _checker(tmp_path)
    checker.check_now()
    checker.check_now()
    assert calls["download"] == 1


def test_stale_installers_for_other_versions_are_pruned(tmp_path):
    (tmp_path / "VoisrSetup-0.9.0.exe").write_bytes(b"old")
    checker, _ = _checker(tmp_path)
    checker.check_now()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["VoisrSetup-1.1.0.exe"]


def test_install_spawns_the_installer_silently_and_relaunches(tmp_path):
    spawned = []
    checker, _ = _checker(tmp_path, spawned=spawned)
    checker.check_now()
    result = checker.install()
    assert result["ok"] is True
    assert checker.status()["state"] == "installing"
    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[0].endswith("VoisrSetup-1.1.0.exe")
    # /RELAUNCH=1 is required: the installer's [Run] entry is skipifsilent,
    # so without it a silent update would leave Voisr closed.
    assert argv[1:] == ["/SILENT", "/NORESTART", "/RELAUNCH=1"]


def test_install_before_an_update_is_ready_is_refused(tmp_path):
    spawned = []
    checker, _ = _checker(tmp_path, spawned=spawned)
    result = checker.install()
    assert "error" in result
    assert spawned == []


def test_a_failed_spawn_is_reported_and_leaves_the_app_running(tmp_path):
    def boom(argv):
        raise OSError("access denied")

    checker, _ = _checker(tmp_path)
    checker._spawn = boom
    checker.check_now()
    result = checker.install()
    assert "error" in result
    assert checker.status()["state"] == "ready"
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: FAIL — `ImportError: cannot import name 'UpdateChecker'`

- [ ] **Step 3: Implement**

Add to the imports in `voisr/updater.py`:

```python
import subprocess
import threading
```

Append:

```python
def _default_spawn(argv) -> None:
    """Start the installer detached, so it outlives the app that spawns it."""
    subprocess.Popen(
        argv,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


class UpdateChecker:
    """Owns the update schedule and state. The UI reads `status()` and calls
    `check_now()` / `install()`; it never sequences the steps itself.

    States: idle -> checking -> downloading -> ready -> installing. Any
    failure returns to idle with `error` set.
    """

    INTERVAL_S = 24 * 60 * 60
    STARTUP_DELAY_S = 10

    def __init__(self, current_version, updates_dir, config_reader,
                 fetch=fetch_manifest, download=download_to,
                 verify=verify_sha256, spawn=None, manifest_url=MANIFEST_URL):
        self._current = current_version
        self._dir = Path(updates_dir)
        self._config_reader = config_reader
        self._fetch = fetch
        self._download = download
        self._verify = verify
        self._spawn = spawn or _default_spawn
        self._manifest_url = manifest_url
        self._lock = threading.Lock()
        self._timer = None
        self._state = "idle"
        self._manifest = None
        self._installer = None
        self._error = None

    # -- state ----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "current_version": self._current,
                "version": self._manifest.version if self._manifest else None,
                "notes": self._manifest.notes if self._manifest else None,
                "error": self._error,
                "enabled": self._enabled(),
            }

    def _enabled(self) -> bool:
        try:
            return bool(self._config_reader().get("check_for_updates", True))
        except Exception:
            return True

    def _set(self, state, *, error=None) -> None:
        with self._lock:
            self._state = state
            self._error = error

    # -- scheduling -----------------------------------------------------
    def start(self) -> None:
        """First check after STARTUP_DELAY_S, then every INTERVAL_S of uptime.
        An interval timer, not a wall clock, so there is no missed-check
        catch-up to reason about -- a nightly shutdown gets the launch check."""
        self._arm(self.STARTUP_DELAY_S)

    def _arm(self, delay) -> None:
        timer = threading.Timer(delay, self._tick)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _tick(self) -> None:
        try:
            self.check_now()
        except Exception as e:  # a background check must never kill its thread
            print(f"voisr: update check crashed: {e}", flush=True)
        self._arm(self.INTERVAL_S)

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    # -- the check ------------------------------------------------------
    def check_now(self, force: bool = False) -> dict:
        if not force and not self._enabled():
            return {"state": "idle", "enabled": False}

        self._set("checking")
        manifest = self._fetch(self._manifest_url)
        if manifest is None or not is_newer(manifest.version, self._current):
            self._set("idle")
            return {"state": "idle", "up_to_date": True}

        self._set("downloading")
        with self._lock:
            self._manifest = manifest
        dest = self._dir / f"VoisrSetup-{manifest.version}.exe"

        # Both _verify calls must sit inside this try. verify_sha256 opens the
        # file, so it raises OSError on a missing or locked one -- and an
        # OSError escaping here would leave the state machine stuck at
        # "downloading" with error=None until the next 24h tick, with the
        # mismatch branch below never running. A file we cannot hash is
        # unverified, which is not the same as a mismatch: only a real
        # mismatch should delete the file and log loudly.
        try:
            if not dest.is_file() or not self._verify(dest, manifest.sha256):
                dest.unlink(missing_ok=True)
                self._download(manifest.url, dest)
                if not self._verify(dest, manifest.sha256):
                    # Never execute an installer we cannot vouch for.
                    dest.unlink(missing_ok=True)
                    print("voisr: update checksum mismatch, discarded", flush=True)
                    self._set("idle",
                              error="Update checksum did not match; discarded.")
                    return self.status()
        except Exception as e:
            self._set("idle", error=f"Could not fetch the update: {e}")
            return self.status()

        self._prune(keep=dest)
        with self._lock:
            self._installer = dest
        self._set("ready")
        return self.status()

    def _prune(self, keep: Path) -> None:
        for path in self._dir.glob("VoisrSetup-*.exe*"):
            if path != keep:
                try:
                    path.unlink()
                except OSError:
                    pass  # a locked leftover is not worth failing the update

    # -- applying -------------------------------------------------------
    def install(self) -> dict:
        with self._lock:
            installer, state = self._installer, self._state
        if state != "ready" or installer is None:
            return {"error": "No verified update is ready to install."}
        try:
            self._spawn([str(installer), "/SILENT", "/NORESTART", "/RELAUNCH=1"])
        except Exception as e:
            self._set("ready", error=f"Could not start the installer: {e}")
            return {"error": str(e)}
        self._set("installing")
        return {"ok": True, "version": self.status()["version"]}
```

- [ ] **Step 4: Run and watch them pass**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: PASS (32 tests)

- [ ] **Step 5: Run the whole Python suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: `427 passed` (396 existing + 31 new; the count is 32 minus Task 1's test already included)

- [ ] **Step 6: Commit**

```bash
git add voisr/updater.py tests/test_updater.py
git commit -m "feat: UpdateChecker schedule and state machine"
```

---

### Task 7: Config default and the Api bridge

**Files:**
- Modify: `voisr/app.py:27-36`
- Modify: `voisr/ui.py` (the `Api` class)
- Test: `tests/test_updater.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updater.py`:

```python
from voisr.app import DEFAULT_CONFIG, load_config


def test_update_checking_is_on_by_default():
    assert DEFAULT_CONFIG["check_for_updates"] is True


def test_an_existing_config_without_the_key_still_checks(tmp_path):
    # Upgrading users have a config.json predating this feature.
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"hotkey": "ctrl_r"}), encoding="utf-8")
    assert load_config(p)["check_for_updates"] is True


def test_a_disabled_config_is_respected(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"check_for_updates": False}), encoding="utf-8")
    assert load_config(p)["check_for_updates"] is False
```

- [ ] **Step 2: Run and watch them fail**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -k default -v`
Expected: FAIL — `KeyError: 'check_for_updates'`

- [ ] **Step 3: Add the config default**

In `voisr/app.py`, add to `DEFAULT_CONFIG` after `"appearance": "system",`:

```python
    "check_for_updates": True,
```

- [ ] **Step 4: Run and watch them pass**

Run: `venv\Scripts\python -m pytest tests/test_updater.py -v`
Expected: PASS (35 tests)

- [ ] **Step 5: Add the four bridge methods**

In `voisr/ui.py`, add this import alongside the other `voisr` imports:

```python
from voisr.updater import UpdateChecker
```

In `Api.__init__`, after the existing attribute setup, add:

```python
        # None until attach_update_checker(); the dashboard tolerates that so
        # a headless/test Api needs no updater.
        self._updates = None
```

Then add these methods next to `set_launch_at_login` (around line 720), matching its `{"ok": ...}` / `{"error": ...}` shape:

```python
    def attach_update_checker(self, checker):
        self._updates = checker

    def get_update_status(self):
        if not self._updates:
            return {"state": "idle", "enabled": False,
                    "current_version": voisr.__version__}
        return self._updates.status()

    def check_for_updates_now(self):
        """Settings' "Check now". Forced, so it works with auto-check off,
        and it reports failures inline because the user is waiting."""
        if not self._updates:
            return {"error": "Updates are unavailable in this build."}
        return self._updates.check_now(force=True)

    def install_update(self):
        if not self._updates:
            return {"error": "Updates are unavailable in this build."}
        return self._updates.install()

    def set_check_for_updates(self, enabled):
        self.set_config({"check_for_updates": bool(enabled)})
        return {"ok": True, "enabled": bool(enabled)}
```

Add `import voisr` at the top of `voisr/ui.py` if it is not already imported.

- [ ] **Step 6: Construct and start the checker where the window is created**

In `voisr/ui.py`'s `main()`, after the `Api` instance exists and before the webview loop starts, add:

```python
    updates = UpdateChecker(
        current_version=voisr.__version__,
        updates_dir=app_data_dir() / "updates",
        config_reader=api.get_config,
    )
    api.attach_update_checker(updates)
    updates.start()
```

Import `app_data_dir` from `voisr.paths` if `main()` does not already have it.

- [ ] **Step 7: Verify nothing regressed**

Run: `venv\Scripts\python -m pytest -q`
Expected: `430 passed`

- [ ] **Step 8: Commit**

```bash
git add voisr/app.py voisr/ui.py tests/test_updater.py
git commit -m "feat: expose update state and controls over the dashboard bridge"
```

---

### Task 8: Installer support for silent update

**Files:**
- Modify: `build/voisr.iss`

- [ ] **Step 1: Add `/RELAUNCH` and `CloseApplications`**

In `build/voisr.iss`'s `[Setup]` section, after `WizardStyle=modern`, add:

```
; A silent update spawns this installer while Voisr is still exiting. Restart
; Manager closes anything still holding files under {app}; it works unelevated
; and acts without prompting under /SILENT.
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll
RestartApplications=no
; Refuse to run two copies of Setup at once. Without this, two concurrent
; silent installs can write into {app} simultaneously and corrupt the install.
; UpdateChecker.install() is guarded against double-spawning, but that guard
; is per-process: an installed copy and a dev copy share %LOCALAPPDATA%\Voisr
; (see the README), so the mutex is the only cross-process protection.
SetupMutex=VoisrSetupMutex
```

Replace the `[Run]` section with:

```
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
; The entry above is skipifsilent, so a silent update would finish with Voisr
; closed. voisr/updater.py passes /RELAUNCH=1 to ask for a restart, and only
; then does this second entry fire.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: IsRelaunchRequested
```

In the `[Code]` section, add:

```
// True only when started with /RELAUNCH=1, which voisr/updater.py passes for
// an unattended update. Absent for a normal interactive install.
function IsRelaunchRequested(): Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
```

- [ ] **Step 2: Rebuild the installer**

Run: `powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1`
Expected: `Successful compile`, then `==> Done: ...VoisrSetup.exe`

- [ ] **Step 3: Verify a silent install relaunches the app**

```bash
build\Output\VoisrSetup.exe /SILENT /NORESTART /RELAUNCH=1
```

Then check the app came back:

Run: `Start-Sleep -Seconds 20; Get-Process Voisr -ErrorAction SilentlyContinue | Select-Object Id, MainWindowTitle`
Expected: one process listed. If empty, `IsRelaunchRequested` is not firing — verify with `/LOG="%TEMP%\voisr-install.log"` and search the log for `RELAUNCH`.

- [ ] **Step 4: Verify a silent install *without* the switch does not relaunch**

Close Voisr, then:

```bash
build\Output\VoisrSetup.exe /SILENT /NORESTART
```

Run: `Start-Sleep -Seconds 20; Get-Process Voisr -ErrorAction SilentlyContinue`
Expected: no output — the switch, not the silent flag, controls relaunch.

- [ ] **Step 5: Commit**

```bash
git add build/voisr.iss
git commit -m "feat: installer relaunch switch and file-lock handling for silent updates"
```

---

### Task 9: Publishing a release

**Files:**
- Create: `scripts/publish_release.ps1`
- Modify: `scripts/build_release.ps1`

- [ ] **Step 1: Emit the manifest and hash from the build**

In `scripts/build_release.ps1`, replace the final `Write-Host "==> Done: ..."` line with:

```powershell
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
# that did not. -Encoding utf8NoBOM would do it on PowerShell 6+, which is
# not what this machine runs.
[System.IO.File]::WriteAllText(
    $manifestPath, $manifestJson, (New-Object System.Text.UTF8Encoding($false)))

$sizeMb = [math]::Round((Get-Item $installer).Length / 1MB, 1)
Write-Host "==> Done: $installer ($sizeMb MB)"
Write-Host "    sha256:   $sha"
Write-Host "    manifest: $manifestPath"
if (-not $env:VOISR_RELEASE_NOTES) {
    Write-Warning "VOISR_RELEASE_NOTES was not set, so latest.json has empty notes. The updater rejects a manifest with an empty field -- set it and rebuild before publishing."
}
```

- [ ] **Step 2: Create the publish script**

Create `scripts/publish_release.ps1`:

```powershell
# Publishes build\Output\VoisrSetup.exe + latest.json as a GitHub release on
# the public release channel. Run scripts\build_release.ps1 first.
#
# The manifest is what installed copies poll, so it is uploaded last: a
# release carrying a manifest whose installer is not yet attached would make
# every client fail its download until the upload finished.
param(
    [string]$Repo = "andr26j/voisr-releases"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $root "build\Output"
$installer = Join-Path $outDir "VoisrSetup.exe"
$manifest = Join-Path $outDir "latest.json"

foreach ($f in @($installer, $manifest)) {
    if (-not (Test-Path $f)) { throw "$f missing -- run scripts\build_release.ps1 first" }
}

$data = Get-Content $manifest -Raw | ConvertFrom-Json
if (-not $data.notes) { throw "latest.json has empty notes; the updater rejects that manifest. Set VOISR_RELEASE_NOTES and rebuild." }
$tag = "v$($data.version)"

# Verify the manifest describes the installer actually being uploaded.
$actual = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $data.sha256) { throw "sha256 mismatch: latest.json says $($data.sha256), installer is $actual. Rebuild." }

if (gh release view $tag --repo $Repo 2>$null) {
    throw "$tag already exists on $Repo. Bump __version__ in voisr\__init__.py and rebuild."
}

Write-Host "==> Creating $tag on $Repo"
gh release create $tag $installer --repo $Repo --title $tag --notes $data.notes
if ($LASTEXITCODE -ne 0) { throw "gh release create failed (exit $LASTEXITCODE)" }

Write-Host "==> Attaching latest.json"
gh release upload $tag $manifest --repo $Repo
if ($LASTEXITCODE -ne 0) { throw "gh release upload failed (exit $LASTEXITCODE)" }

Write-Host "==> Published: https://github.com/$Repo/releases/tag/$tag"
Write-Host "    Clients poll: https://github.com/$Repo/releases/latest/download/latest.json"
```

- [ ] **Step 3: Verify both scripts parse**

```bash
powershell -NoProfile -Command "$e=$null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts\publish_release.ps1), [ref]$null, [ref]$e) | Out-Null; if ($e.Count) { $e | ForEach-Object { $_.Message } } else { 'parse OK' }"
```

Expected: `parse OK`

- [ ] **Step 4: Verify the manifest the build produces is one the client accepts**

```bash
powershell -NoProfile -Command "$env:VOISR_RELEASE_NOTES='Adds auto-update.'; powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1"
```

Then feed the real file through the real validator — the round trip is the point:

```bash
venv\Scripts\python -c "from voisr.updater import Manifest; m=Manifest.from_json(open('build/Output/latest.json',encoding='utf-8').read()); print('accepted:', m.version, m.sha256[:12])"
```

Expected: `accepted: 1.0.0 <12 hex chars>`

Note this reads with plain `utf-8`, not `utf-8-sig`, deliberately: it is the check that the writer above really produced a BOM-free file. If it raises `Unexpected UTF-8 BOM`, the `WriteAllText` call was changed back to `Set-Content -Encoding utf8` — fix the writer rather than this command. (`_default_text_opener` decodes with `utf-8-sig`, so a BOM would not break a real client; it would just mean the two ends disagree, and this is the only place that catches that.)

- [ ] **Step 5: Commit**

```bash
git add scripts/publish_release.ps1 scripts/build_release.ps1
git commit -m "feat: publish releases with a verified update manifest"
```

---

### Task 10: Settings — toggle, Check now, and the false privacy claim

**Files:**
- Modify: `voisr/web/index.html:300-315`
- Modify: `voisr/web/js/views/settings.js`
- Modify: `voisr/web/js/api.js`
- Test: `tests/js/update.test.mjs`

- [ ] **Step 1: Write the failing test**

Create `tests/js/update.test.mjs`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { loadVoisr } from "./load.mjs";

test("settings.js exposes render/init after adding update controls", () => {
  const env = loadVoisr(["js/ui.js", "js/views/settings.js"]);
  const settings = env.V.views.settings;
  assert.equal(typeof settings.init, "function");
  assert.equal(typeof settings.render, "function");
});
```

- [ ] **Step 2: Run and watch it pass trivially, then add the real assertions**

Run: `node --test tests/js/update.test.mjs`
Expected: PASS (this is the load guard; the behaviour tests come in Task 11)

- [ ] **Step 3: Add the settings markup and fix the false claim**

In `voisr/web/index.html`, replace the privacy `setting-desc` at line 304 — it currently says "No network calls, no accounts, no telemetry", which stops being true the moment update checking ships:

```html
            <p class="setting-desc">Audio is recorded to a temp file, transcribed by whisper.cpp on this PC, then deleted. No accounts, no telemetry. The only network call is a version check to GitHub, which sends no audio, text, or identifying data.</p>
```

Then insert two rows before the closing `</div>` of that section (after the `clear-history` row):

```html
        <div class="setting-row">
          <div>
            <h3>Automatically check for updates</h3>
            <p class="setting-desc">Once a day, ask GitHub whether a newer Voisr has been released. Downloads happen in the background; nothing installs until you click Install.</p>
          </div>
          <label class="switch">
            <input type="checkbox" id="update-toggle">
            <span class="slider" aria-hidden="true"></span>
          </label>
        </div>
        <div class="setting-row">
          <div>
            <h3>Version</h3>
            <p class="setting-desc"><span id="app-version">—</span><span id="update-result"></span></p>
          </div>
          <button type="button" id="update-check" class="btn-secondary">Check now</button>
        </div>
```

- [ ] **Step 4: Wire the controls**

Three constraints from the Task 6 review that this wiring must respect:

- **Render the banner from `state`, not `version`.** `UpdateChecker` never clears `_manifest`, so `status()` can report a `version` for a release that is no longer offered. Only `state === "ready"` means installable.
- **If you ever toggle the checker on and off, use `stop()` then `start()` — never `start()` twice.** A second `start()` without an intervening `stop()` arms an untracked timer.
- **`check_now` may return `already_checking: true`** when a background check is mid-flight. That is not a completed check; render the in-progress state rather than reporting "up to date".

In `voisr/web/js/views/settings.js`, inside `render()` next to the existing `$("#launch-toggle").checked = ...` line, add:

```javascript
    const upd = await V.api().get_update_status();
    $("#update-toggle").checked = cfg.check_for_updates !== false;
    $("#app-version").textContent = `Voisr ${upd.current_version || "—"}`;
```

In `init()`, next to the `launch-toggle` listener, add:

```javascript
    document.getElementById("update-toggle").addEventListener("change", async (e) => {
      const res = await V.ui.tryApi(
        V.api().set_check_for_updates(e.target.checked), "Save update setting");
      // Leaving the switch showing a state that was never saved is its own
      // bug, so put it back on failure -- same as the launch toggle.
      if (!res) { e.target.checked = !e.target.checked; return; }
      V.ui.toast(e.target.checked ? "Update checks on" : "Update checks off");
    });

    document.getElementById("update-check").addEventListener("click", async () => {
      const out = document.getElementById("update-result");
      out.textContent = " — checking…";
      const res = await V.ui.tryApi(V.api().check_for_updates_now(), "Check for updates");
      if (!res) { out.textContent = ""; return; }
      if (res.error) out.textContent = ` — ${res.error}`;
      else if (res.state === "ready") out.textContent = ` — ${res.version} ready to install`;
      else out.textContent = " — you're up to date";
    });
```

- [ ] **Step 5: Add the dev mocks**

In `voisr/web/js/api.js`, add `check_for_updates: true` to `mockCfg`, then add inside the `window.pywebview = { api: {` object:

```javascript
      // mirrors Api.get_update_status()/install_update()/etc in voisr/ui.py.
      get_update_status: async () => ({
        state: "ready", current_version: "1.0.0", version: "1.1.0",
        notes: "Adds auto-update.", error: null, enabled: true,
      }),
      check_for_updates_now: async () => ({
        state: "ready", current_version: "1.0.0", version: "1.1.0",
        notes: "Adds auto-update.", error: null, enabled: true,
      }),
      install_update: async () => ({ ok: true, version: "1.1.0" }),
      set_check_for_updates: async (enabled) => {
        mockCfg.check_for_updates = !!enabled;
        return { ok: true, enabled: !!enabled };
      },
```

- [ ] **Step 6: Run the JS suite**

Run: `node --test tests/js/*.test.mjs`
Expected: `pass 134`, `fail 0`

- [ ] **Step 7: Commit**

```bash
git add voisr/web/index.html voisr/web/js/views/settings.js voisr/web/js/api.js tests/js/update.test.mjs
git commit -m "feat: update settings controls, and correct the privacy copy"
```

---

### Task 11: The Home banner

**Files:**
- Modify: `voisr/web/index.html`
- Modify: `voisr/web/js/views/home.js`
- Test: `tests/js/update.test.mjs`

- [ ] **Step 1: Write the failing tests**

Append to `tests/js/update.test.mjs`:

```javascript
import { makeNode } from "./dom.mjs";

function homeEnv(status) {
  const env = loadVoisr(["js/ui.js", "js/views/home.js"]);
  const banner = makeNode("div");
  const nodes = {
    "update-banner": banner,
    "update-banner-text": makeNode("span"),
    "update-install": makeNode("button"),
  };
  const realGet = globalThis.document.getElementById;
  globalThis.document.getElementById = (id) =>
    nodes[id] || realGet.call(globalThis.document, id);
  const calls = [];
  globalThis.window.Voisr.api = () => ({
    get_update_status: async () => status,
    install_update: async () => { calls.push("install"); return { ok: true }; },
  });
  return { env, nodes, calls };
}

test("a ready update renders the banner with its version and notes", async () => {
  const { env, nodes } = homeEnv({
    state: "ready", version: "1.1.0", notes: "Adds auto-update.",
  });
  await env.V.views.home.renderUpdateBanner();
  assert.equal(nodes["update-banner"].hidden, false);
  assert.match(nodes["update-banner-text"].textContent, /1\.1\.0/);
  assert.match(nodes["update-banner-text"].textContent, /Adds auto-update/);
});

test("no update means no banner", async () => {
  const { env, nodes } = homeEnv({ state: "idle", version: null });
  await env.V.views.home.renderUpdateBanner();
  assert.equal(nodes["update-banner"].hidden, true);
});

test("a failed status check hides the banner instead of throwing", async () => {
  const { env, nodes } = homeEnv(null);
  globalThis.window.Voisr.api = () => ({
    get_update_status: async () => { throw new Error("bridge down"); },
  });
  await assert.doesNotReject(() => env.V.views.home.renderUpdateBanner());
  assert.equal(nodes["update-banner"].hidden, true);
});

test("dismissing hides the banner and it does not come back this session", async () => {
  const { env, nodes } = homeEnv({
    state: "ready", version: "1.1.0", notes: "Adds auto-update.",
  });
  await env.V.views.home.renderUpdateBanner();
  env.V.views.home.dismissUpdateBanner();
  assert.equal(nodes["update-banner"].hidden, true);
  await env.V.views.home.renderUpdateBanner();
  assert.equal(nodes["update-banner"].hidden, true);
});
```

- [ ] **Step 2: Run and watch them fail**

Run: `node --test tests/js/update.test.mjs`
Expected: FAIL — `env.V.views.home.renderUpdateBanner is not a function`

- [ ] **Step 3: Add the banner markup**

In `voisr/web/index.html`, immediately inside the Home `<section>` opening tag (before its first child), add:

```html
      <div id="update-banner" class="update-banner" hidden>
        <span id="update-banner-text"></span>
        <button type="button" id="update-install" class="btn-primary">Install</button>
        <button type="button" id="update-dismiss" class="btn-ghost" aria-label="Dismiss">×</button>
      </div>
```

- [ ] **Step 4: Implement the banner logic**

In `voisr/web/js/views/home.js`, inside the IIFE add:

```javascript
  // Session-scoped: a dismissed banner returns on the next launch while the
  // update is still pending, so it can be ignored today without being lost.
  let updateDismissed = false;

  async function renderUpdateBanner() {
    const banner = document.getElementById("update-banner");
    if (!banner) return;
    let status;
    try {
      status = await V.api().get_update_status();
    } catch {
      // A background concern must never take the dashboard down with it.
      banner.hidden = true;
      return;
    }
    if (updateDismissed || !status || status.state !== "ready" || !status.version) {
      banner.hidden = true;
      return;
    }
    const notes = status.notes ? ` — ${status.notes}` : "";
    document.getElementById("update-banner-text").textContent =
      `Version ${status.version} is ready${notes}`;
    banner.hidden = false;
  }

  function dismissUpdateBanner() {
    updateDismissed = true;
    const banner = document.getElementById("update-banner");
    if (banner) banner.hidden = true;
  }
```

Inside `home.js`'s `init()`, add:

```javascript
    document.getElementById("update-install").addEventListener("click", async () => {
      // Say "Installing" BEFORE the call, never after it. A successful
      // install_update() kills the app ~0.75s later, and pywebview's
      // evaluate_js blocks on a semaphore that destroy_window does not
      // release -- so if Application.Exit() wins the race, this promise
      // never settles and anything in a .then() never runs. Only the error
      // path can rely on code after the await.
      V.ui.toast("Installing update — Voisr will restart");
      const res = await V.ui.tryApi(V.api().install_update(), "Install update");
      if (res && res.error) V.ui.toast(`Update failed: ${res.error}`);
    });
    document.getElementById("update-dismiss")
      .addEventListener("click", dismissUpdateBanner);
```

Call `renderUpdateBanner()` from `render()` (fire it alongside `renderHero()`, and let its own catch handle failure), and add both functions to the object exported on `V.views.home`.

- [ ] **Step 5: Run and watch them pass**

Run: `node --test tests/js/update.test.mjs`
Expected: PASS (5 tests)

- [ ] **Step 6: Run both full suites**

Run: `node --test tests/js/*.test.mjs`
Expected: `pass 138`, `fail 0`

Run: `venv\Scripts\python -m pytest -q`
Expected: `430 passed`

- [ ] **Step 7: Commit**

```bash
git add voisr/web/index.html voisr/web/js/views/home.js tests/js/update.test.mjs
git commit -m "feat: dashboard banner for a ready update"
```

---

### Task 12: Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the release process**

In `README.md`, after the "Building a release" section, add:

````markdown
## Publishing a release

1. Bump `__version__` in `voisr/__init__.py`.
2. Build, with the notes the updater will show in its banner:

```powershell
$env:VOISR_RELEASE_NOTES = "What changed, in one line."
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

3. Publish to the public release channel:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish_release.ps1
```

Releases live in [andr26j/voisr-releases](https://github.com/andr26j/voisr-releases) — public so installed copies can fetch updates without a token, while this source repo stays private. It holds only `VoisrSetup.exe` and `latest.json`.

`publish_release.ps1` refuses to publish if the tag exists, if `notes` is empty, or if the installer's SHA256 disagrees with `latest.json`. Installed copies verify that hash before running anything, so a mismatch means every client rejects the update.
````

- [ ] **Step 2: Document updating from the user's side**

Under "Configuration (`config.json`)", add `"check_for_updates": true` to the example JSON, then add after the example:

```markdown
### Updating

Voisr checks for a new release 10 seconds after launch and every 24 hours,
downloads it in the background, and shows an **Install** banner on the
dashboard. Nothing installs until you click it, and the update needs no admin
rights. Turn checking off in Settings → Privacy & data, where **Check now**
also works on demand.

This version check is the only network call Voisr makes. It sends no audio, no
text, and nothing identifying — it fetches a small JSON file describing the
latest release. Downloads are verified against a published SHA256 before
anything is executed.
```

- [ ] **Step 3: Remove auto-update from the future-work list**

In "Possible future work", delete:

```markdown
- **Auto-update.** Employees currently reinstall manually.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: release publishing and the update flow"
```

---

### Task 13: End-to-end verification against two real releases

Nothing above proves the whole path works — the unit tests use fakes by design. This task is manual and needs two published releases.

**Files:** none (verification only)

- [ ] **Step 1: Publish 1.0.0**

With `__version__ = "1.0.0"`:

```bash
powershell -ExecutionPolicy Bypass -File scripts\publish_release.ps1
```

Expected: `==> Published: https://github.com/andr26j/voisr-releases/releases/tag/v1.0.0`

- [ ] **Step 2: Confirm the manifest is publicly fetchable with no auth**

```bash
powershell -NoProfile -Command "(Invoke-WebRequest 'https://github.com/andr26j/voisr-releases/releases/latest/download/latest.json' -UseBasicParsing).Content"
```

Expected: the JSON, with `version` `1.0.0`. A 404 here means the asset name is wrong or the release is a draft.

- [ ] **Step 2b: Confirm tray Quit actually exits the process**

Do this before anything else, because the whole update flow depends on it and it is **pre-existing behaviour nobody has verified**.

A code review of `voisr/ui.py` could not confirm from source that `menu_quit` fully exits. `main()` calls `widget.create()` *before* `webview.create_window(dashboard)`, so the pill window holds pywebview's `uid == 'master'`, and winforms' `on_close` calls `Application.Exit()` only when `len(BrowserView.instances) == 0`. The pill is never destroyed anywhere in `voisr/`. If that reading is right, `webview.start()` never returns and the process survives.

Launch Voisr, then choose **Quit Voisr** from the tray menu.

```bash
powershell -NoProfile -Command "Start-Sleep -Seconds 5; Get-Process Voisr -ErrorAction SilentlyContinue | Select-Object Id, MainWindowTitle"
```

Expected: no output — the process is gone.

If a process remains, tray Quit is broken independently of auto-update, and the post-install shutdown is relying on its hard-exit backstop every time rather than as a fallback. Fix `menu_quit` to destroy the pill window as well, and re-run.

- [ ] **Step 3: Install 1.0.0 and confirm no update is offered**

Run `build\Output\VoisrSetup.exe`, open the dashboard, Settings → Privacy & data → **Check now**.
Expected: `Voisr 1.0.0 — you're up to date`, and no banner on Home.

- [ ] **Step 4: Publish 1.0.1**

Bump `__version__` to `"1.0.1"`, then:

```bash
powershell -NoProfile -Command "$env:VOISR_RELEASE_NOTES='Test release for auto-update.'; powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1"
```

```bash
powershell -ExecutionPolicy Bypass -File scripts\publish_release.ps1
```

- [ ] **Step 5: Verify the installed 1.0.0 finds it, installs, and relaunches**

In the still-installed 1.0.0, click **Check now**.
Expected: `1.0.1 ready to install`, and a Home banner reading `Version 1.0.1 is ready — Test release for auto-update.`

Click **Install**.
Expected: Voisr exits, the installer runs with no UI and no admin prompt, Voisr reappears within about a minute.

Confirm the new version took:

```bash
powershell -NoProfile -Command "(Get-Item \"$env:LOCALAPPDATA\Programs\Voisr\Voisr.exe\").VersionInfo.ProductVersion"
```

Expected: `1.0.1`

- [ ] **Step 6: Confirm user data survived the update**

```bash
powershell -NoProfile -Command "Get-Item \"$env:LOCALAPPDATA\Voisr\history.jsonl\", \"$env:LOCALAPPDATA\Voisr\config.json\" | Select-Object Name, Length, LastWriteTime"
```

Expected: both present, history unchanged in length. An update that eats dictation history is a critical bug — `{app}` and `%LOCALAPPDATA%\Voisr` must stay separate.

- [ ] **Step 7: Confirm a tampered manifest is rejected**

Edit the `latest.json` asset on the 1.0.1 release so `sha256` is wrong, then click **Check now** in an installed 1.0.0.
Expected: the check reports a checksum error, `%LOCALAPPDATA%\Voisr\updates\` is left empty, and no banner appears. Restore the correct manifest afterwards.

- [ ] **Step 8: Commit the version bump**

```bash
git add voisr/__init__.py
git commit -m "chore: release 1.0.1"
```

---

## Self-review against the spec

| Spec section | Task |
| --- | --- |
| Release channel + manifest URL | 3, 9, 13 |
| Manifest schema, all fields required | 3, 9 |
| Version single source of truth | 1 |
| `parse_version` / `is_newer` | 1, 2 |
| `fetch_manifest` | 4 |
| `download_to`, `.part` handling | 5 |
| `verify_sha256` | 5 |
| `UpdateChecker` states | 6 |
| Applying via `/SILENT /NORESTART /RELAUNCH=1` | 6, 8 |
| `CloseApplications` safety net | 8 |
| Storage in `%LOCALAPPDATA%\Voisr\updates\`, pruning | 6, 7 |
| `check_for_updates` config + default | 7 |
| Banner, no modals | 11 |
| Settings toggle, Check now, version | 10 |
| Failure table (404/timeout/JSON/hash/spawn) | 4, 5, 6 |
| Schedule (10s, then 24h interval) | 6 |
| Security: hash before execute, URL pinning | 3, 5, 6, 13 |
| Testing (Python + JS) | 2–7, 10, 11 |
| Docs | 12 |

**Gap found and closed while reviewing:** the spec did not mention that
`index.html:304` advertises "No network calls" — false once this ships.
Task 10 corrects it.

**Type consistency:** `UpdateChecker.status()` returns the same keys the JS
reads (`state`, `current_version`, `version`, `notes`, `error`, `enabled`) in
Tasks 6, 7, 10, and 11. Bridge method names match between `voisr/ui.py`
(Task 7), the dev mocks (Task 10), and the views (Tasks 10, 11).
