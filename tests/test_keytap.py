import ctypes

import pytest

import voisr.keytap as keytap
from voisr.keytap import WindowsKeyListener


class FakeHookStruct:
    def __init__(self, vk, extra=0):
        self.vkCode = vk
        self.dwExtraInfo = extra


def install(monkeypatch, listener, struct):
    """Drive the listener's hook callback directly, without a real hook."""
    monkeypatch.setattr(keytap, "_struct_from_lparam", lambda lparam: struct)
    monkeypatch.setattr(keytap.user32, "CallNextHookEx",
                        lambda *args: 0, raising=False)
    return listener._callback


def test_keydown_is_reported(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(0, keytap.WM_KEYDOWN, 0)
    assert seen == [(0xA3, "down")]


def test_keyup_is_reported(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(0, keytap.WM_KEYUP, 0)
    assert seen == [(0xA3, "up")]


def test_syskeydown_is_reported_as_down(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA5))
    cb(0, keytap.WM_SYSKEYDOWN, 0)
    assert seen == [(0xA5, "down")]


def test_our_own_injected_events_are_ignored(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    struct = FakeHookStruct(0x56, extra=keytap.VOISR_SIGNATURE)
    cb = install(monkeypatch, listener, struct)
    cb(0, keytap.WM_KEYDOWN, 0)
    assert seen == []


def test_other_apps_injected_events_are_not_ignored(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0x56, extra=0xDEAD))
    cb(0, keytap.WM_KEYDOWN, 0)
    assert seen == [(0x56, "down")]


def test_negative_ncode_is_passed_through_untouched(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(-1, keytap.WM_KEYDOWN, 0)
    assert seen == []


def test_handler_exception_never_escapes_the_callback(monkeypatch):
    def explode(vk, kind):
        raise RuntimeError("handler bug")

    listener = WindowsKeyListener(explode)
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(0, keytap.WM_KEYDOWN, 0)  # must not raise — a raise kills the hook


def test_unknown_message_is_ignored(monkeypatch):
    seen = []
    listener = WindowsKeyListener(lambda vk, kind: seen.append((vk, kind)))
    cb = install(monkeypatch, listener, FakeHookStruct(0xA3))
    cb(0, 0x9999, 0)
    assert seen == []


def test_a_hook_that_cannot_be_installed_settles_as_failed(monkeypatch):
    # The watcher respawns on "failed"; a listener stuck in "starting" after
    # a refused install would never be retried.
    monkeypatch.setattr(keytap.user32, "SetWindowsHookExW", lambda *a: None)
    listener = keytap.WindowsKeyListener(lambda vk, kind: None)
    assert listener.state == "starting"
    listener.start()
    assert listener.wait_started(2) == "failed"
    listener.stop()
