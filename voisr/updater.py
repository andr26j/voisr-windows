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

from voisr.paths import app_data_dir


def _log(message: str) -> None:
    """Append a timestamped line to %LOCALAPPDATA%\\Voisr\\logs\\voisr.log.

    print() alone is not enough. build/voisr.spec sets console=False, so in
    the shipped build sys.stdout is None and CPython turns print() into a
    silent no-op -- no exception, no output, nowhere. Every failure this
    module deliberately logs and swallows (an unreachable manifest, a
    malformed one, a checksum mismatch) would therefore leave no trace at
    all in exactly the place those failures actually happen. The first field
    report of "updates don't work" needs one artifact to point at.

    Still prints too: that is what a dev console and the tests read.

    Best-effort by design -- logging must never be the reason an update
    fails, so an unwritable log directory is swallowed. Growth is not a
    concern worth rotating for: nothing here logs on the happy path, so a
    permanently broken channel costs a line a day.
    """
    try:
        print(f"voisr: {message}", flush=True)
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_dir / "voisr.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except Exception:
        # Wider than OSError, and print() is inside the try: logging is never
        # allowed to be the thing that breaks an update. Nothing here has a
        # caller that could do anything useful with a logging failure anyway.
        pass


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

    Note what that conflation costs: a caller cannot tell "nothing has been
    published" from "the network is down", so UpdateChecker reports every
    None as a failed check rather than as "up to date". That is the safe
    direction -- and once a channel has published anything, a 404 on it is a
    broken channel, not good news.
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
        _log(f"update check failed: {type(e).__name__}: {e}")
        return None
    try:
        return Manifest.from_json(raw)
    except ValueError as e:
        # ValueError is the only thing from_json can raise, by construction
        # (it rejects non-str/bytes input before any field coercion runs),
        # not merely by the happenstance of what today's opener returns.
        _log(f"ignoring malformed update manifest: {e}")
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
    flat 10ms, which totals 2.39s of waiting instead of 200ms. That budget is
    deliberately far wider than history.py's: measured against deny-all Win32
    handles, a flat 200ms succeeds at 100ms and 190ms of contention and fails
    from 250ms on, and an on-write scan of a 155 MB .exe is not reliably even
    a sub-second event the way a small JSONL is. Waiting costs nothing here (a
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
            time.sleep(min(0.01 * 2 ** (attempt // 3), 0.5))


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
    """Start the installer with no console of its own and in its own process
    group, so a Ctrl+C aimed at Voisr cannot reach it.

    Neither flag is what makes it outlive us -- on Windows a child always
    survives its parent -- so do not read survival into them, and do not add
    CREATE_BREAKAWAY_FROM_JOB chasing it.

    cwd is the installer's own directory (%LOCALAPPDATA%\\Voisr\\updates), not
    Voisr's: an inherited working directory of {app} would be an open handle
    on the very tree the installer is about to replace.

    Unelevated by design -- this relies on voisr.iss keeping
    PrivilegesRequired=lowest, or the silent install would stall on a UAC
    prompt nobody is watching.
    """
    subprocess.Popen(
        argv,
        cwd=str(Path(argv[0]).parent),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


class UpdateChecker:
    """Owns the update schedule and state. The UI reads `status()` and calls
    `check_now()` / `install()`; it never sequences the steps itself.

    States: idle -> checking -> downloading -> ready -> installing. Any
    failure returns to idle with `error` set.

    stop() cancels the schedule, not a check already running: one that was
    in flight still finishes and can land in `ready`, so the UI may raise an
    update banner moments after the user switched auto-update off.

    `installing` is terminal on purpose and nothing here leaves it. What ends
    the process is the installer: voisr.iss must set CloseApplications=yes
    (task 8) so Inno shuts Voisr down to unlock {app}, with /RELAUNCH=1
    bringing it back. Not the UI layer, and deliberately not a watchdog here
    -- but if that directive is dropped, this state becomes a dead end.
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
            # Any timer already armed is replaced, never merely dropped:
            # start() called twice would otherwise orphan the first one --
            # untracked, so a later stop() cannot cancel it, and it would go
            # on to run a full check and download after the user switched
            # auto-update off.
            old, self._timer = self._timer, None
            # Constructing a Timer starts nothing; the thread begins at
            # start(), which is deliberately outside the lock.
            timer = threading.Timer(delay, self._tick)
            timer.daemon = True
            self._timer = timer
        if old is not None:
            old.cancel()  # a no-op on one that has already fired
        timer.start()

    def _tick(self) -> None:
        try:
            self.check_now()
        except Exception as e:  # a background check must never kill its thread
            _log(f"update check crashed: {e}")
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
        no-update path adds `up_to_date: True` and a call that found another
        check already running adds `already_checking: True`. `force` is the
        Settings "Check now" button, which must work with auto-check off.
        """
        if not force and not self._enabled():
            return self.status()

        # Never restart the machine out from under an install. _check's first
        # act is to claim "checking", which would overwrite "installing" and
        # re-open install() for a second spawn of the same installer -- which
        # voisr.iss's SetupMutex then aborts, so the cost is a wasted spawn
        # rather than a corrupt {app}, but still worth refusing. Read the
        # field, then return outside the lock: status() takes the same
        # non-reentrant lock and would deadlock.
        with self._lock:
            installing = self._state == "installing"
        if installing:
            return self.status()

        # Single-flight. The interval timer and the Settings button can both
        # arrive here: two checks would stream into the same .part file, and
        # the loser's rename would then fail and report a spurious error over
        # the winner's freshly ready update. A caller that finds a check
        # already running gets the current state and lets it finish rather
        # than blocking a UI thread for the length of a download.
        if not self._check_lock.acquire(blocking=False):
            return {**self.status(), "already_checking": True}
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
        # Check and claim in one acquisition, exactly as install() does.
        # check_now's guard is only a fast path: it releases _lock before this
        # runs, so an install() landing in that gap would set "installing"
        # just for this transition to overwrite it -- and a state back at
        # "ready" re-opens the very second spawn install()'s own claim closes.
        # With both transitions compare-and-set on the same lock, neither can
        # start on top of the other.
        with self._lock:
            claimed = self._state != "installing"
            if claimed:
                self._state, self._error = "checking", None
        if not claimed:
            # Outside the with block: status() takes this same non-reentrant
            # lock and would deadlock inside it.
            return self.status()

        manifest = self._fetch(self._manifest_url)
        if manifest is None:
            # The fetch FAILED -- offline, timeout, DNS, a malformed manifest,
            # a 404. Emphatically not "up to date", which is a wrong answer
            # rather than a quiet one for someone who just clicked Check now,
            # and is why this cannot share the branch below. The state has to
            # survive too, not just the file: demoting a verified, downloaded
            # update to idle over a momentary blip takes the banner and the
            # Install button away for up to 24h while the installer sits on
            # disk unusable.
            with self._lock:
                # Only if the check is still ours -- an install() that landed
                # while we were on the network owns the state now.
                if self._state == "checking":
                    self._state = "ready" if self._installer else "idle"
            return {**self.status(), "check_failed": True}

        # Keyword arguments on purpose: both sides are plain version strings,
        # so swapping them would silently invert the test and produce an
        # update that never fires.
        if not is_newer(remote=manifest.version, local=self._current):
            # There is nothing to offer, so nothing on disk is worth keeping.
            # This is the path a freshly updated app takes on its first check,
            # and without it the ~155 MB installer that was just applied would
            # sit in %LOCALAPPDATA% forever. Reachable only with a manifest in
            # hand: a failed fetch returns above precisely so that it can
            # never trigger this and throw away a good download.
            self._prune(keep=None)
            self._set("idle")
            return {**self.status(), "up_to_date": True}

        self._set("downloading")
        with self._lock:
            self._manifest = manifest
        dest = self._dir / f"VoisrSetup-{manifest.version}.exe"
        # Before the download, not after it: pruning only on success leaves
        # ~155 MB of superseded installer behind on every failure path, and
        # this way the disk space is freed before we need 155 MB of it.
        self._prune(keep=dest)

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
                    _log("update checksum mismatch, discarded")
                    self._abandon("Update checksum did not match; discarded.")
                    return self.status()
        except Exception as e:
            self._abandon(f"Could not fetch the update: {e}")
            return self.status()

        with self._lock:
            self._installer = dest
        self._set("ready")
        return self.status()

    def _abandon(self, error: str) -> None:
        """Give up on the download in progress: idle, with no installer named.

        Clearing _installer is the point. It must never name a file that was
        just deleted -- the failed-fetch branch restores "ready" on the
        strength of that field, so a stale one resurrects a banner offering an
        installer that is not on disk, and the user's click gets a WinError 2.
        """
        with self._lock:
            self._state, self._error, self._installer = "idle", error, None

    def _prune(self, keep: Path | None) -> None:
        """Drop every installer but `keep` (pass None to drop all of them),
        including leftover .part files from a crashed download -- hence the
        trailing wildcard.

        Sweeping .part files that broadly is safe, but not because of
        single-flight: that lock is per-process, and two Voisr copies can
        share %LOCALAPPDATA%\\Voisr\\updates (the README describes running an
        installed copy alongside a dev one). What actually protects a live
        download is Windows: CPython opens the .part without
        FILE_SHARE_DELETE, so unlinking one that another process is still
        writing raises PermissionError, which the handler below swallows. Anyone
        adding a retry around that unlink, or widening the glob, removes the
        only thing standing between this and deleting a download in flight.

        That protection covers .part files only. A *completed* installer is
        held open by nobody, so this will happily delete one that another
        instance is currently offering as `ready` -- that instance's install()
        then fails with FileNotFoundError, which it handles by dropping back to
        idle and re-downloading. Wasted bytes, never a corrupt install.
        """
        for path in self._dir.glob("VoisrSetup-*.exe*"):
            if path != keep:
                try:
                    path.unlink()
                except OSError:
                    pass  # a locked leftover is not worth failing the update
        with self._lock:
            if self._installer is not None and self._installer != keep:
                # Whatever it named is gone now, so the field goes with it.
                # The invariant lives here, next to the deletion, rather than
                # at each call site: any future pruner gets it for free.
                self._installer = None

    # -- applying -------------------------------------------------------
    def _log_flag(self) -> list:
        """`["/LOG=<path>"]`, or `[]` if the directory cannot be made.

        The counterpart to /SUPPRESSMSGBOXES, and the only reason a failed
        silent install is diagnosable at all. Trace it without one: Setup
        fails a couple of seconds in -- PrepareToInstall cannot reach the
        network for the WebView2 bootstrapper, say, or Restart Manager finds a
        holder it cannot close -- /SUPPRESSMSGBOXES auto-answers the dialog,
        Setup exits non-zero, and its [Run] relaunch entry never fires.
        Nothing here notices: _default_spawn is DETACHED_PROCESS and never
        waits, and "installing" is terminal. The app then exits on schedule
        and the user watches Voisr vanish and not come back, with no dialog,
        no notification and nothing written down. On the next launch the
        checker finds the same update and offers it again -- an invisible
        loop. Inno's log records the exact Restart Manager failure and the
        text of every box /SUPPRESSMSGBOXES swallowed, which is the difference
        between a diagnosable first field failure and an unfalsifiable one.

        Deliberately beside the app data, never under {app}: the installer is
        in the middle of replacing that tree. And deliberately omitted rather
        than guessed at if the directory cannot be created -- Inno aborts
        Setup outright when it cannot open the log file it was handed, so a
        broken path here would turn "no log" into "no update".
        """
        log_dir = self._dir.parent / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _log(f"no installer log ({e})")
            return []
        return [f"/LOG={log_dir / f'install-{int(time.time())}.log'}"]

    def install(self) -> dict:
        # Check and claim in one acquisition. Spawning first and recording
        # "installing" afterwards leaves a window in which a second install()
        # still reads "ready" and spawns the same installer again. voisr.iss's
        # SetupMutex keeps that second Setup from writing into {app} alongside
        # the first, but it still costs a pointless spawn that aborts, so do
        # not lean on it. The state is also what makes the installer path
        # trustworthy, so both must be read together regardless.
        with self._lock:
            installer = self._installer
            version = self._manifest.version if self._manifest else None
            if self._state != "ready" or installer is None:
                return {"error": "No verified update is ready to install."}
            self._state, self._error = "installing", None
        try:
            # /SUPPRESSMSGBOXES is not redundant with /SILENT. /SILENT hides
            # the wizard, not Setup's modal dialogs: a SetupMutex collision
            # measured as an OK/Cancel box that hung for over 45s until it was
            # killed by hand, and Restart Manager raises its own box when it
            # cannot close an app. Nobody can click either -- this app is on
            # its way out -- so a modal here means the user watches Voisr
            # vanish and never come back. With this flag the same collision
            # defaults to Cancel and exits 1 in under two seconds, and the
            # update simply retries next cycle.
            self._spawn([str(installer), "/SILENT", "/SUPPRESSMSGBOXES",
                         "/NORESTART", "/RELAUNCH=1", *self._log_flag()])
        except Exception as e:
            with self._lock:
                # Only hand the state back if it is still ours to hand back.
                if self._state == "installing":
                    if isinstance(e, FileNotFoundError):
                        # Definitively gone rather than transiently unusable:
                        # another instance sharing the updates dir pruned it.
                        # Handing back "ready" would leave the user clicking
                        # Install on a dangling path until the next 24h tick,
                        # so drop to idle and let the next check re-download.
                        self._state, self._installer = "idle", None
                        self._error = ("The downloaded update is no longer "
                                       "available; it will be fetched again.")
                    else:
                        self._state = "ready"
                        self._error = f"Could not start the installer: {e}"
            return {"error": str(e)}
        return {"ok": True, "version": version}
