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
