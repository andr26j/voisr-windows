# Voisr auto-update — design

Date: 2026-07-31
Status: approved, ready for implementation planning

## Problem

Employees reinstall Voisr by hand. Nobody does, so everybody runs a stale
build. The app must notice a new release, fetch it, and install it without
admin rights and without interrupting dictation.

## Constraints that shape the design

- **The installer is per-user and unelevated** (`PrivilegesRequired=lowest`).
  Anything the updater does must work without admin.
- **The build is unsigned.** There is no code-signing certificate, so the
  published SHA256 is the only integrity check between downloading an `.exe`
  and executing it. Verification is not optional.
- **The source repo is private.** The app cannot authenticate to it, and a
  token shipped inside the bundle is extractable by anyone who installs it.
- **The product promise is that nothing leaves the machine.** An update check
  is a network call and must be honestly documented and switchable.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Where updates are hosted | Public repo `andr26j/voisr-releases` | Source stays private, no token ships, free, one command to publish |
| How the user is told | Dashboard banner with an Install button | Never interrupts dictation; the user picks when the app restarts |
| Automatic checking | On by default, toggle in Settings | Employees stay current without defeating the local-only claim |
| How the update is applied | Re-run the full installer | Reuses the tested installer; a small download is not worth a class of silent update-breaking bugs |

Rejected: delta updates (complexity for a cost nobody feels), rollback,
staged rollout, release channels. All revisitable; none needed now.

## Release channel

`https://github.com/andr26j/voisr-releases`, public, holding no source — only
per-release assets:

- `VoisrSetup.exe`
- `latest.json`

The client fetches the manifest from the redirecting *download* URL, not the
API:

```
https://github.com/andr26j/voisr-releases/releases/latest/download/latest.json
```

This matters: the unauthenticated GitHub API allows 60 requests/hour per IP,
and an office behind one NAT address would exhaust that. The download URL has
no such limit and needs no token.

Manifest schema — every field required, unknown fields ignored:

```json
{
  "version": "1.1.0",
  "url": "https://github.com/andr26j/voisr-releases/releases/download/v1.1.0/VoisrSetup.exe",
  "sha256": "9f2b...",
  "notes": "One-line human summary shown in the banner."
}
```

With no releases published the URL returns 404. That is the normal
"no update" state, not an error, and must not surface to the user.

## Version as a single source of truth

`voisr/__init__.py` gains `__version__ = "1.0.0"`.

`scripts/build_release.ps1` reads it and passes `/DMyAppVersion=<version>` to
ISCC; `build/voisr.iss` drops its hardcoded `MyAppVersion`. The app and the
installer then cannot disagree about what version they are, which is the bug
that makes update loops (reinstalling the same build forever) possible.

## Components

### `voisr/updater.py`

Split so every piece is testable without network access or spawning an
installer. Collaborators are injected, matching how the existing tests
monkeypatch `subprocess` and `find_binary`.

| Unit | Responsibility |
| --- | --- |
| `parse_version(text) -> tuple[int, ...]` | `"1.10.0"` → `(1, 10, 0)`; raises on junk |
| `is_newer(remote, local) -> bool` | Numeric tuple compare, so `1.10.0 > 1.9.0` |
| `Manifest` | Frozen dataclass; `from_json` validates required fields |
| `fetch_manifest(url, opener) -> Manifest \| None` | `None` on 404/timeout/bad JSON |
| `download_to(url, dest, opener) -> Path` | Writes `dest.part`, renames on completion |
| `verify_sha256(path, expected) -> bool` | Streamed hashing |
| `UpdateChecker` | Owns schedule and state; calls the above |

`UpdateChecker` states: `idle` → `checking` → `downloading` → `ready` →
`installing`, plus `idle` again after any failure. The UI reads this state;
it never drives the sequence itself.

### Applying the update

On **Install**:

1. Spawn `VoisrSetup.exe /SILENT /NORESTART /RELAUNCH=1` detached.
2. Exit the app immediately so its files unlock.

Two changes make this reliable:

- **`/RELAUNCH=1`.** The current `[Run]` entry carries `skipifsilent`, so a
  silent install would finish with Voisr closed. A second `[Run]` entry gated
  on `Check: IsRelaunchRequested` (reading `{param:RELAUNCH|0}`) starts the
  app again after a silent update, and only then.
- **`CloseApplications=yes`.** Restart Manager closes anything still holding
  files in `{app}`, works unelevated, and acts automatically under `/SILENT`.
  This is the safety net for the race between the app exiting and the
  installer reaching the file-copy step.

### Storage

Downloads land in `%LOCALAPPDATA%\Voisr\updates\VoisrSetup-<version>.exe` —
beside `config.json` and `history.jsonl`, outside `{app}`, so an install
cannot delete an update mid-download. Stale files for versions other than
the pending one are removed on each successful check.

## Configuration

`config.json` gains one key:

```json
{ "check_for_updates": true }
```

Added to `DEFAULT_CONFIG` in `voisr/app.py`, so an absent key falls back to
`true` exactly as the existing keys do (`load_config` starts from the
defaults and `update()`s the file over them). Exposed as
"Automatically check for updates" in Settings → Privacy & Data, alongside a
**Check now** button that works regardless of the toggle.

## User interface

- **Banner**, dashboard Home: `Version X.Y.Z is ready` + the manifest's
  `notes` + **Install** + dismiss. Dismissal lasts the session; the banner
  returns next launch while the update is still pending.
- **Settings**: the toggle, the current version, and **Check now**, which
  reports its result inline (`You're up to date` / the failure reason).
- **No modal dialogs, ever.** A background check that fails is invisible.

## Failure handling

Every background failure logs and retries on the next cycle. None of them
shows a dialog.

| Failure | Behaviour |
| --- | --- |
| 404 (no releases yet) | Treated as "up to date" |
| Timeout, DNS, offline | Silent; retry next cycle |
| Malformed JSON or missing field | Silent; manifest ignored |
| `version` not newer | Nothing happens |
| SHA256 mismatch | Delete the file, log loudly, never offer it |
| Download interrupted | `.part` file discarded; retried next cycle |
| Installer fails to spawn | Banner reports it inline; app keeps running |

A manual **Check now** surfaces the reason instead of staying silent, because
the user is waiting on an answer.

## Schedule

10 seconds after launch, then every 24 hours of uptime — an interval timer,
not a wall-clock schedule, so there is no missed-check catch-up to reason
about. A machine shut down nightly gets its check from the launch trigger
instead. The 10-second delay keeps startup — already carrying model load and
WebView2 init — free of network work.

## Security

- SHA256 verified before the update is *offered*, not at the moment it is
  executed: a download that fails the check is deleted and never reaches
  `ready`, but `install()` re-reads the path and spawns without re-hashing,
  and a banner can sit un-clicked for days in between. This is the whole
  integrity story while the build is unsigned, so the check cannot be
  skipped or made best-effort.
  Re-verifying on the click was considered and rejected: the gap crosses no
  privilege boundary. Anything able to swap the file in
  `%LOCALAPPDATA%\Voisr\updates` can equally rewrite the app's own code in
  `%LOCALAPPDATA%\Voisr\app`, so the re-check would buy no defence against
  the only attacker who can reach it. Non-adversarial corruption is caught
  anyway — Inno Setup binaries self-check a CRC and refuse to run — and a
  vanished file is already handled (`install()` drops to `idle` and
  re-downloads).
- HTTPS only; an `http://` URL in a manifest is rejected.
- The manifest's `url` must point at the `github.com/andr26j/voisr-releases/`
  release path. This pins only the *first* hop: `urlopen` follows redirects
  without re-validating the host, so the bytes can still end up coming from
  wherever GitHub's redirector sends them. That's fine — it means trusting
  GitHub, not the manifest — but it's a property of relying on GitHub, not
  something the URL check itself enforces.
- SHA256 gives integrity, not authenticity. The hash and the `url` come from
  the same manifest document, so anyone who can tamper with one can tamper
  with the other to match. The hash defends against transport corruption and
  against the download being swapped independently of the manifest; it is
  not a second, independent factor against a compromised manifest.

## Files touched

New:
- `voisr/updater.py`
- `scripts/publish_release.ps1`
- `tests/test_updater.py`

Changed:
- `voisr/__init__.py` — `__version__`
- `voisr/app.py` — `check_for_updates` default
- `voisr/ui.py` — wire `UpdateChecker`, expose state to the web layer
- `voisr/web/index.html`, `voisr/web/js/*` — banner and settings control
- `build/voisr.iss` — version from define, `/RELAUNCH`, `CloseApplications`
- `scripts/build_release.ps1` — read `__version__`, pass `/DMyAppVersion`,
  emit `latest.json` and the SHA256
- `README.md` — release process, the network call, the toggle

## Testing

Python, `tests/test_updater.py`, fake opener and fake spawner throughout — no
network, no installer execution:

- `parse_version` on valid and malformed input
- `is_newer`: `1.10.0 > 1.9.0`, equal versions, downgrade rejected
- `fetch_manifest`: 404, timeout, malformed JSON, missing field → `None`
- Manifest rejected for `http://` and for a non-release-path URL
- `verify_sha256`: match and mismatch
- Mismatch deletes the download and leaves state `idle`
- `.part` file discarded on interrupted download
- Toggle off means no network call at all
- Install spawns the expected argv, including `/RELAUNCH=1`

JS, `tests/js/`: banner renders from state, Install calls the bridge once,
dismiss hides it, bridge failure surfaces rather than blanking the pane —
matching the existing history-pane tests.

Not covered by automated tests, verified by hand once: an end-to-end update
between two real releases, since it needs published GitHub releases and a
real installer run.

## Out of scope

Delta or patch updates; rollback; release channels or staged rollout;
in-app release notes beyond the one-line `notes`; updating the model
independently of the app; code signing (tracked separately — it would let
the updater verify a signature instead of relying only on the hash).
