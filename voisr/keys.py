"""Windows virtual-key codes for the push-to-talk hotkey.

Right Ctrl is the default rather than macOS's right Alt: on many keyboard
layouts right Alt is AltGr, and holding it while we inject Ctrl+V produces a
different chord entirely.
"""

DEFAULT_HOTKEY = "ctrl_r"

# name -> (vk, label). Only keys that are safe to hold down while typing
# continues elsewhere, and that no common app claims.
KEYS = {
    "ctrl_r":  (0xA3, "Right Ctrl"),
    "ctrl_l":  (0xA2, "Left Ctrl"),
    "alt_r":   (0xA5, "Right Alt"),
    "alt_l":   (0xA4, "Left Alt"),
    "shift_r": (0xA1, "Right Shift"),
    "shift_l": (0xA0, "Left Shift"),
    "win_r":   (0x5C, "Right Win"),
    "caps":    (0x14, "Caps Lock"),
    "f13":     (0x7C, "F13"),
    "f14":     (0x7D, "F14"),
    "f15":     (0x7E, "F15"),
    "scroll":  (0x91, "Scroll Lock"),
    "pause":   (0x13, "Pause"),
}

_LABELS = {vk: label for vk, label in KEYS.values()}


def resolve_key(name: str):
    """Virtual-key code for a config hotkey name, or None if unknown."""
    entry = KEYS.get((name or "").lower())
    return entry[0] if entry else None


def key_label(vk: int) -> str:
    """Human-readable name for a virtual-key code."""
    return _LABELS.get(vk, f"VK 0x{vk:02X}")


def hotkey_options() -> list[dict]:
    """Selectable hotkeys for the settings UI, in declaration order."""
    return [{"name": name, "label": label} for name, (_, label) in KEYS.items()]
