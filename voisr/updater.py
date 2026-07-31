"""Auto-update: check a published manifest, fetch the installer, hand off.

Every step takes its collaborator as an argument -- the HTTP opener, the
downloader, the process spawner -- so the whole module is testable without
network access and without ever executing an installer.
"""

import hashlib
import json
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


def parse_version(text) -> tuple[int, ...]:
    """'1.10.0' -> (1, 10, 0). Tolerates a leading 'v'."""
    parts = str(text).strip().lstrip("vV").split(".")
    if not parts or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(p) for p in parts)


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


# The public release channel. Source stays in a private repo; a token shipped
# inside the bundle would be extractable by anyone who installs it.
MANIFEST_URL = (
    "https://github.com/andr26j/voisr-releases/releases/latest/download/latest.json"
)
RELEASE_URL_PREFIX = (
    "https://github.com/andr26j/voisr-releases/releases/download/"
)
_HEX = frozenset("0123456789abcdef")

# A real manifest is ~200 bytes. Capping the raw payload rejects things like
# `"[" * 200000` before it reaches json.loads and blows the decoder's
# recursion limit -- RecursionError is not a ValueError, and callers only
# catch ValueError to treat a bad manifest as "no update". Checked in bytes
# at the network read and via len(raw) in from_json -- for str, len() counts
# characters, never more than the underlying UTF-8 byte count, so the cap is
# never looser there than at the network layer.
_MAX_MANIFEST_BYTES = 8192


@dataclass(frozen=True)
class Manifest:
    version: str
    url: str
    sha256: str
    notes: str

    @classmethod
    def from_json(cls, raw) -> "Manifest":
        """Parse and fully validate a manifest. `raw` must be JSON text
        (str or bytes) -- anything else, and anything suspect once parsed,
        raises ValueError. Callers rely on that being the *only* exception
        type this can raise, so they can turn it into "no update", never a
        partial one, without a second net to catch something else."""
        if not isinstance(raw, (str, bytes)):
            # Reject non-text up front rather than passing it through to the
            # field coercions below: str(x) on an attacker- or bug-supplied
            # object can run arbitrary __str__ and raise something other
            # than ValueError, breaking the contract this docstring makes.
            raise ValueError(f"manifest must be JSON text, got {type(raw).__name__}")
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds maximum size")
        try:
            data = json.loads(raw)
        except RecursionError:
            # Belt-and-braces: the size cap above already rejects payloads
            # deep enough to matter today, so this path isn't reachable at
            # the current cap. Kept in case that cap is ever raised.
            raise ValueError("manifest is too deeply nested to parse")
        if not isinstance(data, dict):
            # Still needed even though raw is now guaranteed text: "[]" is
            # valid JSON text that decodes to a non-dict.
            raise ValueError("manifest must be a JSON object")

        for field in ("version", "url", "notes"):
            if not data.get(field):
                raise ValueError(f"manifest missing {field}")

        # sha256 gets its own presence check below (via the length test) so
        # that an absent or empty digest reports the specific hex-format
        # rule instead of a generic "missing" message.
        sha = str(data.get("sha256") or "").strip().lower()
        if len(sha) != 64 or not set(sha) <= _HEX:
            raise ValueError("sha256 must be 64 hex characters")

        # Canonicalise rather than store the raw text: a later task derives
        # a filename and a cache key from this string, so "v1.1.0" and
        # " 1.1.0 " must collapse to the same value as "1.1.0".
        version = ".".join(str(n) for n in parse_version(data["version"]))

        url = str(data["url"])
        if not url.isascii() or any(c.isspace() or ord(c) < 0x20 for c in url):
            # Caught early: http.client raises InvalidURL (not ValueError,
            # not OSError) for embedded CRLF/whitespace, which would escape
            # the "treat any bad manifest as ValueError" contract below.
            raise ValueError(f"url has invalid characters: {url!r}")

        # Structural allow-list on scheme/host/path, not a raw substring
        # check: percent-encoding (e.g. "%2e%2e") survives a plain ".." in
        # url test and would otherwise reach the traversal check unnoticed.
        # Note what this does and doesn't buy us: it pins the *first* hop
        # only -- urlopen follows redirects without re-validating the host,
        # so a response from this URL could still redirect elsewhere. And
        # the sha256 check is integrity (the download matches this
        # document), not authenticity (this document is the real one):
        # url and sha256 both come from the same manifest, so whoever can
        # tamper with one can tamper with the other to match.
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != "github.com"
                or not url.startswith(RELEASE_URL_PREFIX)
                or ".." in unquote(parts.path)):
            raise ValueError(f"url is outside the release channel: {url}")

        return cls(version=version, url=url, sha256=sha,
                   notes=str(data["notes"]))


_TIMEOUT_S = 10


def _default_text_opener(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response:
        # Bounded read: an oversized body is truncated at the network layer
        # rather than fully buffered first, so the +1 keeps from_json's own
        # over-cap rejection reachable instead of silently parsing a body
        # truncated to exactly the limit.
        raw = response.read(_MAX_MANIFEST_BYTES + 1)
        # utf-8-sig strips a leading UTF-8 BOM when present and is
        # byte-identical to utf-8 when absent. PowerShell 5.1's
        # `Set-Content -Encoding utf8` (used to publish latest.json) writes
        # a BOM, and plain utf-8 leaves it in place, which makes json.loads
        # raise on every real release -- fetch_manifest would then log and
        # silently treat every published update as "nothing to see".
        return raw.decode("utf-8-sig")


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
        # Deliberately broad: a background check must never kill its thread.
        # Narrower (e.g. OSError-only) would miss http.client.HTTPException
        # subclasses like InvalidURL/BadStatusLine, which urlopen can raise
        # for a malformed response and which are not OSError subclasses.
        # KeyboardInterrupt/SystemExit are BaseException, not Exception, so
        # they still propagate -- this only swallows failures, not shutdown.
        # Tag the exception type: a TypeError from a broken custom opener
        # otherwise looks identical in the log to a real network failure.
        print(f"voisr: update check failed: {type(e).__name__}: {e}", flush=True)
        return None
    try:
        return Manifest.from_json(raw)
    except ValueError as e:
        # ValueError is the only thing from_json can raise, by construction
        # (it rejects non-str/bytes input before any field coercion runs),
        # not merely by the happenstance of what today's opener returns.
        print(f"voisr: ignoring malformed update manifest: {e}", flush=True)
        return None


_CHUNK = 1024 * 1024

# A generous ceiling on the installer itself, not a precise one: once the
# download streams (see _default_bytes_opener below), a runaway body fills
# the system drive under %LOCALAPPDATA% rather than exhausting RAM, so this
# exists to bound that blast radius cheaply rather than to pin an exact size.
_MAX_INSTALLER_BYTES = 512 * 1024 * 1024


def _default_bytes_opener(url: str):
    """Yield the body in _CHUNK pieces, then check the byte count against
    any advertised Content-Length.

    A generator, not one big bytes: response.read() with a known
    Content-Length runs through http.client._safe_read, which accumulates
    into a BytesIO and then calls .getvalue() -- a second full copy. For a
    ~155 MB installer that peaks at roughly 2x the payload size in memory
    for no benefit, while this app is doing the check in the background of
    a live session. Streaming keeps memory near _CHUNK regardless of
    installer size.

    The Content-Length check exists because a dropped connection truncates
    the body without raising: unchecked, that surfaces later only as a
    sha256 mismatch, which reads to the user as "this may have been
    tampered with" when their connection merely dropped.
    """
    with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response:
        expected = response.headers.get("Content-Length")
        expected = int(expected) if expected is not None else None
        received = 0
        while chunk := response.read(_CHUNK):
            received += len(chunk)
            yield chunk
        if expected is not None and received != expected:
            raise OSError(
                f"incomplete download: got {received} of {expected} bytes"
            )


def _chunks(payload):
    """An opener may return the whole body at once (what every test here
    injects) or an iterable of pieces (what the real one yields, to avoid
    holding a ~155 MB installer in memory twice). Normalise to pieces."""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return (payload,)
    return payload


def _replace_with_retry(part: Path, dest: Path) -> None:
    """Retry past a transient lock -- Defender or the search indexer opening
    the freshly written file before the rename can complete.

    Same 20 attempts as history.py's read path, but escalating rather than a
    flat 10ms, which totals ~1.1s of waiting instead of 200ms. That budget is
    deliberately far wider than history.py's: measured against deny-all Win32
    handles, a flat 200ms succeeds at 100ms and 190ms of contention and fails
    from 250ms on, and a 155 MB .exe being scanned on write is not reliably a
    sub-200ms event the way a small JSONL is. Waiting costs nothing here (a
    background thread that just spent minutes downloading), while losing the
    race costs a permanently non-updating install that re-fetches 155 MB every
    24h forever, with the completed download sitting unused in a .part file.
    """
    for attempt in range(20):
        try:
            part.replace(dest)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(min(0.01 * 2 ** (attempt // 4), 0.5))


def download_to(url: str, dest: Path | str, opener=None,
                max_bytes: int = _MAX_INSTALLER_BYTES) -> Path:
    """Download to `dest`, writing a .part file first.

    The rename is the commit point: `dest` either does not exist or holds
    everything the opener produced -- complete as far as the opener
    reported, not a guarantee about the remote artifact itself. (A single
    unbounded response.read() incidentally caught a truncated body via
    stdlib's IncompleteRead; chunked reads at _CHUNK don't get that for
    free, which is why _default_bytes_opener carries its own
    Content-Length check instead.) verify_sha256 is what actually vouches
    for the bytes on disk.
    """
    opener = opener or _default_bytes_opener
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with open(part, "wb") as f:
            total = 0
            for chunk in _chunks(opener(url)):
                total += len(chunk)
                if total > max_bytes:
                    raise OSError(
                        f"download exceeds maximum size of {max_bytes} bytes"
                    )
                f.write(chunk)
    except BaseException:
        # Best-effort cleanup: if unlink itself fails (e.g. the .part file
        # is transiently locked by antivirus scanning it, per the Windows
        # flake in history.py), that secondary failure must not replace the
        # exception the caller actually needs to see.
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _replace_with_retry(part, dest)
    return dest


def verify_sha256(path: Path | str, expected: str) -> bool:
    """Whether `path` hashes to `expected`. The build is unsigned, so this is
    the only integrity check between downloading and executing an .exe.

    Raises OSError if `path` cannot be read; a caller that cannot hash a
    file must treat it as unverified, not as a mismatch -- returning False
    would make a transient lock indistinguishable from a real hash failure,
    and the mismatch branch is expected to delete the file and log loudly.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest() == str(expected).strip().lower()


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
        # Two locks with different jobs. _lock is only ever held for field
        # access, never across I/O, so status() is always instant. _check_lock
        # serialises whole check sequences and is therefore held for as long
        # as a download takes -- which is exactly why it cannot be _lock.
        self._lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._timer = None
        self._stopped = False
        self._state = "idle"
        self._manifest = None
        self._installer = None
        self._error = None

    # -- state ----------------------------------------------------------
    def status(self) -> dict:
        """A snapshot the UI can render. `version`/`notes` describe the latest
        release this checker has seen, whatever state it is in -- only `state`
        says whether an update is actually installable."""
        # _enabled() reads the config from disk; never hold the lock across
        # I/O. threading.Lock is not reentrant, so anything that reached back
        # into this checker from the config reader would deadlock, and the
        # dashboard polls status(), which would queue behind every file read.
        enabled = self._enabled()
        with self._lock:
            state, manifest, error = self._state, self._manifest, self._error
        # self._current never changes, and Manifest is frozen, so reading
        # their fields outside the lock cannot tear.
        return {
            "state": state,
            "current_version": self._current,
            "version": manifest.version if manifest else None,
            "notes": manifest.notes if manifest else None,
            "error": error,
            "enabled": enabled,
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
        with self._lock:
            self._stopped = False
        self._arm(self.STARTUP_DELAY_S)

    def _arm(self, delay) -> None:
        with self._lock:
            if self._stopped:
                # _tick re-arms unconditionally, so a stop() that lands while
                # a check is in flight would otherwise bring the cancelled
                # schedule back to life -- one leaked timer thread per off/on
                # cycle of the setting. Checked under the same lock stop() sets
                # the flag under, so a stop() racing an _arm either loses (and
                # cancels the timer this just stored) or wins (and this bails).
                return
            # Constructing a Timer starts nothing; the thread begins at
            # start(), which is deliberately outside the lock.
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
        with self._lock:
            self._stopped = True
            timer, self._timer = self._timer, None
        if timer:
            timer.cancel()

    # -- the check ------------------------------------------------------
    def check_now(self, force: bool = False) -> dict:
        """Run a full check now. Returns the same shape as `status()`; the
        no-update path adds `up_to_date: True`. `force` is the Settings
        "Check now" button, which must work with auto-check switched off.
        """
        if not force and not self._enabled():
            return self.status()

        # Single-flight. The interval timer and the Settings button can both
        # arrive here: two checks would stream into the same .part file, and
        # the loser's rename would then fail and report a spurious error over
        # the winner's freshly ready update. A caller that finds a check
        # already running gets the current state and lets it finish rather
        # than blocking a UI thread for the length of a download.
        if not self._check_lock.acquire(blocking=False):
            return self.status()
        try:
            return self._check()
        except Exception as e:
            # Belt-and-braces around the handling inside _check: anything
            # unexpected escaping it (a broken injected collaborator, an
            # unparseable current version) must still land in idle with an
            # error, or the machine parks in "checking" until the next tick.
            self._set("idle", error=f"Update check failed: {e}")
            return self.status()
        finally:
            self._check_lock.release()

    def _check(self) -> dict:
        self._set("checking")
        manifest = self._fetch(self._manifest_url)
        # Keyword arguments on purpose: both sides are plain version strings,
        # so swapping them would silently invert the test and produce an
        # update that never fires.
        if manifest is None or not is_newer(remote=manifest.version,
                                           local=self._current):
            self._set("idle")
            return {**self.status(), "up_to_date": True}

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
        """Drop every installer but `keep`, including leftover .part files
        from a crashed download (hence the trailing wildcard). Safe to sweep
        that broadly because check_now is single-flight: no other download can
        be in progress, so the only .part files here are abandoned ones."""
        for path in self._dir.glob("VoisrSetup-*.exe*"):
            if path != keep:
                try:
                    path.unlink()
                except OSError:
                    pass  # a locked leftover is not worth failing the update

    # -- applying -------------------------------------------------------
    def install(self) -> dict:
        # One acquisition for all three: the state is what makes the installer
        # path trustworthy, so they must be read together.
        with self._lock:
            installer, state = self._installer, self._state
            version = self._manifest.version if self._manifest else None
        if state != "ready" or installer is None:
            return {"error": "No verified update is ready to install."}
        try:
            self._spawn([str(installer), "/SILENT", "/NORESTART", "/RELAUNCH=1"])
        except Exception as e:
            self._set("ready", error=f"Could not start the installer: {e}")
            return {"error": str(e)}
        self._set("installing")
        return {"ok": True, "version": version}
