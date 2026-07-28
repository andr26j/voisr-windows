import pytest

from voisr.keys import (
    DEFAULT_HOTKEY,
    hotkey_options,
    key_label,
    resolve_key,
)


def test_default_hotkey_is_right_ctrl():
    assert DEFAULT_HOTKEY == "ctrl_r"


def test_resolve_key_known_names():
    assert resolve_key("ctrl_r") == 0xA3
    assert resolve_key("ctrl_l") == 0xA2
    assert resolve_key("alt_r") == 0xA5
    assert resolve_key("shift_r") == 0xA1
    assert resolve_key("f13") == 0x7C


def test_resolve_key_is_case_insensitive():
    assert resolve_key("CTRL_R") == 0xA3


def test_resolve_key_unknown_returns_none():
    assert resolve_key("nope") is None


def test_key_label_round_trips():
    assert key_label(0xA3) == "Right Ctrl"
    assert key_label(0xA5) == "Right Alt"


def test_key_label_unknown_reports_hex_code():
    assert key_label(0x9F) == "VK 0x9F"


def test_hotkey_options_are_name_label_pairs():
    options = hotkey_options()
    assert {"name": "ctrl_r", "label": "Right Ctrl"} in options
    assert all(set(o) == {"name", "label"} for o in options)


def test_every_option_resolves():
    for option in hotkey_options():
        assert resolve_key(option["name"]) is not None


def test_labels_are_unique():
    labels = [o["label"] for o in hotkey_options()]
    assert len(labels) == len(set(labels))
