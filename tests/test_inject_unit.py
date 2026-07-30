import subprocess

import pytest

import voisr.inject as inject
from voisr.pasteboard import (
    ClipboardPreservationError,
    PasteDeliveryError,
    PasteboardSnapshot,
)


class FakeAdapter:
    def __init__(self):
        self.snapshot_value = PasteboardSnapshot(4, ())
        self.owner_count = 6
        self.restore_result = True
        self.calls = []
        self.snapshot_error = None
        self.restore_error = None
        self.owned = True
        self.owned_error = None

    def snapshot(self):
        self.calls.append("snapshot")
        if self.snapshot_error:
            raise self.snapshot_error
        return self.snapshot_value

    def write_text_if_unchanged(self, text, snapshot):
        self.calls.append(("write", text, snapshot))
        return self.owner_count

    def restore_if_unchanged(self, snapshot, owner_count):
        self.calls.append(("restore", snapshot, owner_count))
        if self.restore_error:
            raise self.restore_error
        return self.restore_result

    def is_owned(self, owner_count):
        self.calls.append(("is_owned", owner_count))
        if self.owned_error:
            raise self.owned_error
        return self.owned


def test_successful_paste_restores_after_grace():
    adapter = FakeAdapter()
    slept = []

    assert inject._paste_with_adapter(
        "hello",
        adapter=adapter,
        post_ctrl_v=lambda: True,
        fallback_ctrl_v=lambda: False,
        sleeper=slept.append,
    ) is True

    assert slept == [inject.PASTE_GRACE_S]
    assert adapter.calls[-1] == (
        "restore", adapter.snapshot_value, adapter.owner_count)


def test_lost_ownership_before_cmd_v_never_posts_or_restores_external_clipboard():
    adapter = FakeAdapter()
    adapter.owned = False
    posted = []

    with pytest.raises(PasteDeliveryError) as caught:
        inject._paste_with_adapter(
            "hello", adapter=adapter, post_ctrl_v=lambda: posted.append(True),
            fallback_ctrl_v=lambda: False, sleeper=lambda delay: None,
        )

    assert caught.value.pasted is False
    assert caught.value.fallback_safe is False
    assert posted == []
    assert adapter.calls[-1][0] == "restore"


def test_ownership_check_failure_repairs_without_posting():
    adapter = FakeAdapter()
    adapter.owned_error = ClipboardPreservationError("generation unavailable")
    posted = []

    with pytest.raises(ClipboardPreservationError):
        inject._paste_with_adapter(
            "hello", adapter=adapter, post_ctrl_v=lambda: posted.append(True),
            fallback_ctrl_v=lambda: False, sleeper=lambda delay: None,
        )

    assert posted == []
    assert adapter.calls[-1][0] == "restore"


@pytest.mark.parametrize("repair_error", [KeyboardInterrupt(), SystemExit()])
def test_ownership_cleanup_process_control_outranks_original_error(
        repair_error):
    adapter = FakeAdapter()
    original = OSError("generation unavailable")
    adapter.owned_error = original
    adapter.restore_error = repair_error

    with pytest.raises(type(repair_error)) as caught:
        inject._paste_with_adapter(
            "hello", adapter=adapter, post_ctrl_v=lambda: True,
            fallback_ctrl_v=lambda: False, sleeper=lambda delay: None,
        )

    assert caught.value is repair_error
    assert caught.value.__cause__ is original


def test_external_change_skips_restore_without_failing_paste():
    adapter = FakeAdapter()
    adapter.restore_result = False

    assert inject._paste_with_adapter(
        "hello", adapter=adapter, post_ctrl_v=lambda: True,
        fallback_ctrl_v=lambda: False, sleeper=lambda delay: None,
    ) is True


def test_blocked_paste_restores_then_raises_without_safe_fallback():
    adapter = FakeAdapter()

    with pytest.raises(PasteDeliveryError) as caught:
        inject._paste_with_adapter(
            "hello", adapter=adapter, post_ctrl_v=lambda: False,
            fallback_ctrl_v=lambda: False, sleeper=lambda delay: None,
        )

    assert caught.value.pasted is False
    assert caught.value.fallback_safe is False
    assert adapter.calls[-1][0] == "restore"


def test_restore_failure_reports_that_paste_already_landed():
    adapter = FakeAdapter()
    adapter.restore_error = OSError("restore unavailable")

    with pytest.raises(PasteDeliveryError) as caught:
        inject._paste_with_adapter(
            "hello", adapter=adapter, post_ctrl_v=lambda: True,
            fallback_ctrl_v=lambda: False, sleeper=lambda delay: None,
        )

    assert caught.value.pasted is True
    assert caught.value.fallback_safe is False
    assert "restore" in str(caught.value)


def test_snapshot_failure_never_attempts_write_or_paste():
    adapter = FakeAdapter()
    adapter.snapshot_error = ClipboardPreservationError("unreadable type")
    pasted = []

    with pytest.raises(ClipboardPreservationError):
        inject._paste_with_adapter(
            "hello", adapter=adapter,
            post_ctrl_v=lambda: pasted.append(True),
            fallback_ctrl_v=lambda: False, sleeper=lambda delay: None,
        )

    assert adapter.calls == ["snapshot"]
    assert pasted == []


def test_restore_disabled_leaves_text_for_manual_paste(monkeypatch):
    copied = []
    monkeypatch.setattr(inject, "set_clipboard", copied.append)
    monkeypatch.setattr(inject, "_post_ctrl_v", lambda: False)
    monkeypatch.setattr(inject, "_fallback_ctrl_v", lambda: False)

    assert inject.paste_at_cursor("manual", restore_clipboard=False) is False
    assert copied == ["manual"]


def test_restore_disabled_command_error_leaves_text_for_manual_paste(monkeypatch):
    copied = []
    monkeypatch.setattr(inject, "set_clipboard", copied.append)
    monkeypatch.setattr(
        inject, "_post_ctrl_v",
        lambda: (_ for _ in ()).throw(OSError("accessibility unavailable")),
    )

    assert inject.paste_at_cursor("manual", restore_clipboard=False) is False
    assert copied == ["manual"]


def test_paste_command_exception_restores_before_reporting():
    adapter = FakeAdapter()

    with pytest.raises(PasteDeliveryError, match="command failed"):
        inject._paste_with_adapter(
            "hello", adapter=adapter,
            post_ctrl_v=lambda: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(["osascript"], 2)),
            fallback_ctrl_v=lambda: False,
            sleeper=lambda delay: None,
        )

    assert adapter.calls[-1][0] == "restore"


def test_wait_exception_restores_then_reports_paste_landed():
    adapter = FakeAdapter()
    wait_error = RuntimeError("wait interrupted")

    with pytest.raises(PasteDeliveryError) as caught:
        inject._paste_with_adapter(
            "hello", adapter=adapter,
            post_ctrl_v=lambda: True,
            fallback_ctrl_v=lambda: False,
            sleeper=lambda delay: (_ for _ in ()).throw(wait_error),
        )

    assert caught.value.pasted is True
    assert caught.value.fallback_safe is False
    assert caught.value.__cause__ is wait_error
    assert adapter.calls[-1][0] == "restore"


def test_wait_and_restore_failures_both_report_paste_landed():
    adapter = FakeAdapter()
    adapter.restore_error = OSError("restore unavailable")
    wait_error = RuntimeError("wait interrupted")

    with pytest.raises(PasteDeliveryError) as caught:
        inject._paste_with_adapter(
            "hello", adapter=adapter,
            post_ctrl_v=lambda: True,
            fallback_ctrl_v=lambda: False,
            sleeper=lambda delay: (_ for _ in ()).throw(wait_error),
        )

    assert caught.value.pasted is True
    assert "wait interrupted" in str(caught.value)
    assert "restore unavailable" in str(caught.value)
    assert caught.value.__cause__ is wait_error
    assert adapter.calls[-1][0] == "restore"


def test_keyboard_interrupt_restores_then_reraises_original():
    adapter = FakeAdapter()
    interrupt = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt) as caught:
        inject._paste_with_adapter(
            "hello", adapter=adapter,
            post_ctrl_v=lambda: True,
            fallback_ctrl_v=lambda: False,
            sleeper=lambda delay: (_ for _ in ()).throw(interrupt),
        )

    assert caught.value is interrupt
    assert adapter.calls[-1][0] == "restore"


def test_keyboard_interrupt_chains_restore_failure_without_replacement():
    adapter = FakeAdapter()
    adapter.restore_error = OSError("restore unavailable")
    interrupt = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt) as caught:
        inject._paste_with_adapter(
            "hello", adapter=adapter,
            post_ctrl_v=lambda: True,
            fallback_ctrl_v=lambda: False,
            sleeper=lambda delay: (_ for _ in ()).throw(interrupt),
        )

    assert caught.value is interrupt
    assert str(caught.value.__cause__) == "restore unavailable"
    assert adapter.calls[-1][0] == "restore"


def test_set_and_get_clipboard_round_trip(monkeypatch):
    store = {}
    monkeypatch.setattr(inject, "_write_text", lambda text: store.update(text=text))
    monkeypatch.setattr(inject, "_read_text", lambda: store.get("text", ""))
    inject.set_clipboard("new")
    assert inject.get_clipboard() == "new"


def test_paste_blocked_when_foreground_is_higher_integrity(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: True)
    sent = []
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: sent.append(True))
    assert inject._post_ctrl_v() is False
    assert sent == []


def test_paste_proceeds_when_foreground_is_same_integrity(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: True)
    assert inject._post_ctrl_v() is True


def test_held_modifiers_are_released_before_injection(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    held = [inject.VK_MENU, inject.VK_SHIFT]
    monkeypatch.setattr(inject, "_held_modifiers", lambda: list(held))
    released, restored, sent = [], [], []
    monkeypatch.setattr(inject, "_key_up", released.append)
    monkeypatch.setattr(inject, "_key_down", restored.append)
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: sent.append(True) or True)

    assert inject._post_ctrl_v() is True
    assert released == held
    assert sent == [True]
    assert restored == held


def test_injection_failure_still_restores_held_modifiers(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(inject, "_held_modifiers", lambda: [inject.VK_MENU])
    restored = []
    monkeypatch.setattr(inject, "_key_up", lambda vk: None)
    monkeypatch.setattr(inject, "_key_down", restored.append)
    monkeypatch.setattr(
        inject, "_send_ctrl_v",
        lambda: (_ for _ in ()).throw(OSError("SendInput failed")))

    with pytest.raises(OSError):
        inject._post_ctrl_v()
    assert restored == [inject.VK_MENU]


def test_paste_blocked_when_foreground_is_our_own_window(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(inject, "foreground_is_own_process", lambda: True)
    sent = []
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: sent.append(True))
    assert inject._post_ctrl_v() is False
    assert sent == []


def test_paste_proceeds_when_foreground_is_another_process(monkeypatch):
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(inject, "foreground_is_own_process", lambda: False)
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: True)
    assert inject._post_ctrl_v() is True


def test_own_process_check_failure_does_not_block_paste(monkeypatch):
    """Unknown must not mean blocked — that would refuse pastes that
    would have worked, which is worse than the rare self-paste."""
    monkeypatch.setattr(inject, "foreground_is_elevated", lambda: False)
    monkeypatch.setattr(
        inject, "_foreground_pid",
        lambda: (_ for _ in ()).throw(OSError("no window")))
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: True)
    assert inject._post_ctrl_v() is True
