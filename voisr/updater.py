"""Auto-update: check a published manifest, fetch the installer, hand off.

Every step takes its collaborator as an argument -- the HTTP opener, the
downloader, the process spawner -- so the whole module is testable without
network access and without ever executing an installer.
"""

import json
import urllib.request
from dataclasses import dataclass
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
# catch ValueError to treat a bad manifest as "no update".
_MAX_MANIFEST_BYTES = 8192


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
        if isinstance(raw, (str, bytes)) and len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds maximum size")
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except RecursionError:
            raise ValueError("manifest is too deeply nested to parse")
        if not isinstance(data, dict):
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
        # Deliberately broad: a background check must never kill its thread.
        # Narrower (e.g. OSError-only) would miss http.client.HTTPException
        # subclasses like InvalidURL/BadStatusLine, which urlopen can raise
        # for a malformed response and which are not OSError subclasses.
        # KeyboardInterrupt/SystemExit are BaseException, not Exception, so
        # they still propagate -- this only swallows failures, not shutdown.
        print(f"voisr: update check failed: {e}", flush=True)
        return None
    try:
        return Manifest.from_json(raw)
    except ValueError as e:
        # json.JSONDecodeError is already a ValueError subclass, and
        # from_json converts every other failure path (oversized payload,
        # missing fields, bad url, RecursionError) into ValueError too --
        # so ValueError is the only thing a malformed manifest can raise.
        print(f"voisr: ignoring malformed update manifest: {e}", flush=True)
        return None
