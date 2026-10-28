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


_CONSENT_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                r"\CapabilityAccessManager\ConsentStore\microphone")


def _consent_value(hive, subkey):
    """The "Value" string under a ConsentStore key, or None if absent."""
    import winreg
    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _ = winreg.QueryValueEx(key, "Value")
    except OSError:
        return None
    return value if isinstance(value, str) else None


def microphone_switch(read=None):
    """Whether Windows' privacy switches let Voisr use the microphone: False
    when any of them is off, True when all are on, None when unknown.

    Cheap (three registry reads), so the hotkey checks it on every press.
    Three switches gate a desktop app: the device-wide one (HKLM), "Let
    apps access your microphone" (HKCU), and "Let desktop apps access your
    microphone" (HKCU ...\\NonPackaged). Any one off hands Voisr silence."""
    import winreg
    if read is None:
        read = _consent_value
    values = [read(winreg.HKEY_LOCAL_MACHINE, _CONSENT_KEY),
              read(winreg.HKEY_CURRENT_USER, _CONSENT_KEY),
              read(winreg.HKEY_CURRENT_USER, _CONSENT_KEY + r"\NonPackaged")]
    if "Deny" in values:
        return False
    if all(v == "Allow" for v in values):
        return True
    return None


def microphone_state():
    """The privacy switches first: a switched-off one is a known denial, and
    opening a stream would only hand back silence. Otherwise the stream
    probe decides."""
    if microphone_switch() is False:
        return False
    return microphone_granted()


DEFAULT_PROBES = {
    "microphone": microphone_state,
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
