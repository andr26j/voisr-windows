"""Microphone permission probe and its Settings deep link.

Windows doesn't gate Input Monitoring or Accessibility the way macOS does, so
microphone is the only permission worth probing here.

The probe returns True (granted), False (denied), or raises -- callers get
None for "unknown". Unknown must never be presented as granted: a wrongly
green chip is how someone spends twenty minutes debugging a dead hotkey.
"""

import subprocess

PANES = {
    "microphone": ("Microphone", "ms-settings:privacy-microphone"),
}

ORDER = ["microphone"]


def microphone_granted() -> bool:
    """Open a brief input stream; if it succeeds, we have mic access.

    RawInputStream is used (not InputStream) so this probe doesn't pull in
    numpy, which isn't otherwise a project dependency.
    """
    import sounddevice as sd

    with sd.RawInputStream(channels=1):
        pass
    return True


DEFAULT_PROBES = {
    "microphone": microphone_granted,
}


def states(probes=None) -> list[dict]:
    probes = probes if probes is not None else DEFAULT_PROBES
    out = []
    for key in ORDER:
        label, url = PANES[key]
        probe = probes.get(key)
        try:
            granted = bool(probe()) if probe is not None else None
        except Exception:
            granted = None  # unknown, never granted
        out.append({"key": key, "label": label,
                    "granted": granted, "settings_url": url})
    return out


def settings_url(key: str):
    pane = PANES.get(key)
    return pane[1] if pane else None


def open_pane(key: str, opener=None) -> bool:
    """Open a pane by key. The key is validated against PANES so caller
    input can never reach the shell/URL launcher unvalidated."""
    url = settings_url(key)
    if url is None:
        return False
    if opener is None:
        def opener(u):
            try:
                import os
                os.startfile(u)
            except Exception:
                subprocess.run(["cmd", "/c", "start", "", u], check=True)
    try:
        opener(url)
    except Exception:
        return False
    return True
