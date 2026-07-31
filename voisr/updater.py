"""Auto-update: check a published manifest, fetch the installer, hand off.

Every step takes its collaborator as an argument -- the HTTP opener, the
downloader, the process spawner -- so the whole module is testable without
network access and without ever executing an installer.
"""

import json
from dataclasses import dataclass


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
