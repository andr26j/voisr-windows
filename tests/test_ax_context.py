"""Component A: what may be read from the focused field, and what must not.
The Mac's tests, in UI Automation's terms (see tests/fake_ax.py)."""
import os
import re
import threading
import time
from pathlib import Path

import pytest

from tests.fake_ax import FakeAX, FakeElement, textbox
from voisr.ax_context import (DENYLIST, MAX_VALUE_CHARS, UNREADABLE, AXWorker,
                              FocusContext, FocusTracker, SessionFocus, _split_at_utf16,
                              capture_focus, denied)

ROOT = Path(__file__).resolve().parent.parent
TEXT_ATTRS = {"Value", "SelectedText", "SelectedTextRange", "Title", "Name"}


def test_splits_at_the_caret_and_reads_the_window():
    label = FakeElement(ControlType="Text", Name="Priya Raman")
    window = FakeElement(ControlType="Window", Title="Inbox",
                         Children=[FakeElement(ControlType="Group",
                                               Children=[label])])
    box = textbox("hello there world", caret=6, selection=5, Window=window)
    focus = capture_focus(FakeAX(box))
    assert focus.readable and focus.caret_known and not focus.secure
    assert (focus.before_text, focus.selected_text, focus.after_text) == (
        "hello ", "there", " world")
    assert focus.window_title == "Inbox"
    assert focus.nearby_texts == ("Priya Raman",)
    assert focus.value == "hello there world"
    assert focus.element_id == box.id


def test_excerpts_stop_at_500_on_a_word_boundary():
    before = "word " * 300
    box = textbox(before + "after " * 300, caret=len(before))
    focus = capture_focus(FakeAX(box))
    assert len(focus.before_text) <= 500
    assert focus.before_text.startswith("word")
    assert len(focus.after_text) <= 500
    assert focus.after_text.endswith("after")


@pytest.mark.parametrize("value, unit_caret, expected_before", [
    ("abc def", 3, "abc"),
    ("😀 hi there", 3, "😀 "),                # the emoji is two UTF-16 units
    ("a😀b😀c", 6, "a😀b😀"),
])
def test_caret_offsets_are_utf16_units(value, unit_caret, expected_before):
    assert _split_at_utf16(value, unit_caret, 0)[0] == expected_before


def _assert_read_no_text(ax):
    assert not TEXT_ATTRS & {a for _, a in ax.calls}, ax.calls


@pytest.mark.parametrize("role", ["Edit", "Document", "ComboBox", "Custom"])
def test_a_password_field_reads_nothing(role):
    # Value is set so a read would really return it
    box = FakeElement(ControlType=role, IsPassword=True, Value="hunter2",
                      SelectedText="hunter2", SelectedTextRange=(0, 7),
                      Window=FakeElement(ControlType="Window", Title="Log in"))
    ax = FakeAX(box)
    focus = capture_focus(ax)
    assert focus.secure and not focus.readable
    assert focus.value is None and focus.before_text == focus.after_text == ""
    assert focus.window_title == "" and focus.nearby_texts == ()
    _assert_read_no_text(ax)
    assert "hunter2" not in repr(focus)


def test_a_role_that_cannot_be_read_means_no_text_is_asked_for():
    box = FakeElement(Value="maybe a password")
    ax = FakeAX(box)
    focus = capture_focus(ax)
    assert not focus.readable and focus.reason == "unreadable"
    _assert_read_no_text(ax)


def test_a_password_state_that_cannot_be_read_means_no_text_is_asked_for():
    box = textbox("maybe a password")
    del box.attrs["IsPassword"]
    ax = FakeAX(box)
    focus = capture_focus(ax, textbox_only=True)
    assert not focus.readable and focus.reason == "unreadable" and not focus.secure
    _assert_read_no_text(ax)


@pytest.mark.parametrize("app_id", [
    "1password.exe", "bitwarden.exe", "keepassxc.exe", "keepass.exe",
    "lastpass.exe", "dashlane.exe", "nordpass.exe", "proton pass.exe",
    "keeperpasswordmanager.exe", "credentialuibroker.exe", "consent.exe",
    "systemsettings.exe", "logonui.exe",
])
def test_the_denylist_makes_no_uia_call_at_all(app_id):
    ax = FakeAX(textbox("secret"), bundle_id=app_id)
    focus = capture_focus(ax)
    assert focus.secure and focus.value is None
    assert ax.calls == []


def test_a_password_managers_panel_is_denied_when_another_app_is_in_front():
    # quick-access panels take focus without becoming the foreground app
    box = textbox("secret item")
    ax = FakeAX(box, bundle_id="chrome.exe")
    ax.owner_bundle_id = "1password.exe"
    focus = capture_focus(ax)
    assert focus.secure and focus.value is None
    assert ("Value" not in {a for _, a in ax.calls})


def test_the_denylist_matches_whole_names_and_families_only():
    assert denied("1Password.exe") and denied("KeePassXC.exe")
    assert not denied("notconsent.exe")
    assert not denied("consent.exe.evil-lookalike")
    assert not denied(None)
    assert any(entry.endswith("*") for entry in DENYLIST)


@pytest.mark.parametrize("attrs, reason", [
    ({"ControlType": "Edit"}, "unreadable"),                # no Value
    ({"ControlType": "Edit", "Value": 3}, "unreadable"),    # not text
    ({"ControlType": "Button", "Value": "OK"}, "not_editable"),
    ({"ControlType": "Edit", "IsReadOnly": True, "Value": "x"}, "not_editable"),
    # a web page: a Document, but not one anyone types in
    ({"ControlType": "Document", "IsReadOnly": True, "Value": "page"}, "not_editable"),
])
def test_an_unreadable_field_is_unreadable_not_empty(attrs, reason):
    focus = capture_focus(FakeAX(FakeElement(**attrs)))
    assert focus.readable is False
    assert focus.value is None, "must not look like an empty field"
    assert focus.reason == reason


@pytest.mark.parametrize("role", ["Edit", "Document", "ComboBox"])
def test_every_editable_role_is_read(role):
    focus = capture_focus(FakeAX(textbox("some text", role=role, IsReadOnly=False)))
    assert focus.readable and focus.value == "some text"


def test_a_truly_empty_field_is_readable_and_empty():
    focus = capture_focus(FakeAX(textbox("")))
    assert focus.readable is True and focus.value == ""


def test_no_caret_from_a_field_that_has_only_a_value():
    box = textbox("text")
    del box.attrs["SelectedTextRange"]
    focus = capture_focus(FakeAX(box))
    assert focus.readable and not focus.caret_known
    assert focus.before_text == focus.after_text == ""


def test_no_focused_element():
    focus = capture_focus(FakeAX(None))
    assert focus.reason == "no_focused_element" and not focus.readable


def test_too_long_keeps_only_the_selection():
    box = textbox("x" * (MAX_VALUE_CHARS + 1), SelectedText="pick")
    ax = FakeAX(box)
    focus = capture_focus(ax)
    assert focus.reason == "too_long" and focus.value is None
    assert focus.selected_text == "pick"
    assert not focus.readable and not focus.caret_known


def test_voisrs_own_process_is_not_read():
    ax = FakeAX(textbox("mine"), pid=os.getpid())
    focus = capture_focus(ax)
    assert focus.reason == "self"
    assert ax.calls == []


def test_a_field_owned_by_voisrs_process_is_not_read():
    class Own(FakeAX):
        def owner(self, element):
            return "voisr.exe", "Voisr", os.getpid()

    ax = Own(textbox("mine"))
    assert capture_focus(ax).reason == "self"
    _assert_read_no_text(ax)


@pytest.mark.parametrize("host", ["msedgewebview2.exe", "applicationframehost.exe"])
def test_a_host_process_is_named_by_the_app_in_front(host):
    ax = FakeAX(textbox("text"), bundle_id="olk.exe")
    ax.owner_bundle_id = host
    focus = capture_focus(ax)
    assert focus.bundle_id == "olk.exe"


def test_web_content_in_another_app_is_not_trusted_to_be_empty():
    ax = FakeAX(textbox("text"), bundle_id="olk.exe")
    ax.owner_bundle_id = "msedgewebview2.exe"
    assert capture_focus(ax).trust_absence is False


@pytest.mark.parametrize("app_id, electron, trusted", [
    ("notepad.exe", False, True),
    ("chrome.exe", False, False),
    ("windowsterminal.exe", False, False),
    ("slack.exe", True, False),
])
def test_where_a_missing_paste_can_be_trusted(app_id, electron, trusted):
    focus = capture_focus(FakeAX(textbox("text"), bundle_id=app_id, electron=electron))
    assert focus.trust_absence is trusted


def test_capture_never_raises():
    class Broken(FakeAX):
        def focused_element(self, timeout_s):
            raise RuntimeError("private text in an error")

    focus = capture_focus(Broken())
    assert focus.readable is False and focus.reason == "unreadable"
    assert "private" not in repr(focus)


def test_repr_never_shows_text():
    window = FakeElement(ControlType="Window", Title="Secret title",
                         Children=[FakeElement(ControlType="Text",
                                               Name="Secret label")])
    box = textbox("Secret body", caret=6, Window=window)
    assert "Secret" not in repr(capture_focus(FakeAX(box)))


def test_the_walk_is_bounded_and_skips_password_fields_and_other_fields():
    secret = FakeElement(ControlType="Text", Name="in a password subtree")
    secure = FakeElement(ControlType="Group", IsPassword=True, Children=[secret])
    field = FakeElement(ControlType="Edit", Value="another field's text",
                        Children=[FakeElement(ControlType="Text",
                                              Name="another field's text")])
    labels = [FakeElement(ControlType="Text", Name=f"label {i}")
              for i in range(50)]
    window = FakeElement(ControlType="Window", Children=[secure, field, *labels])
    ax = FakeAX(textbox("x", Window=window))
    focus = capture_focus(ax)
    assert len(focus.nearby_texts) == 10
    assert "in a password subtree" not in focus.nearby_texts
    assert "another field's text" not in focus.nearby_texts
    assert (field, "Value") not in ax.calls

    wide = FakeElement(ControlType="Window", Children=[
        FakeElement(ControlType="Group") for _ in range(1000)])
    ax = FakeAX(textbox("x", Window=wide))
    capture_focus(ax, budget_s=5.0)
    assert sum(1 for _, a in ax.calls if a == "ControlType") <= 201  # 200 + the field


def test_the_walk_reads_the_labels_of_a_web_page_but_not_its_fields():
    page = FakeElement(ControlType="Document", IsReadOnly=True, Children=[
        FakeElement(ControlType="Text", Name="Shipping to Northcote"),
        FakeElement(ControlType="Edit", Children=[
            FakeElement(ControlType="Text", Name="typed in a field")])])
    window = FakeElement(ControlType="Window", Children=[page])
    focus = capture_focus(FakeAX(textbox("x", Window=window)))
    assert focus.nearby_texts == ("Shipping to Northcote",)


def test_the_budget_ends_the_walk():
    now = [0.0]

    class Slow(FakeAX):
        def attribute(self, element, name, timeout_s):
            now[0] += 0.02
            return super().attribute(element, name, timeout_s)

    window = FakeElement(ControlType="Window", Children=[
        FakeElement(ControlType="Text", Name=f"t{i}") for i in range(100)])
    ax = Slow(textbox("x", Window=window))
    capture_focus(ax, budget_s=0.15, clock=lambda: now[0])
    assert now[0] < 0.3


def test_the_adapter_has_no_way_to_change_another_app():
    """The setters sit beside the slots uia.py calls: SetFocus is
    IUIAutomationElement's 3, SetValue is ValuePattern's 3, and Select,
    AddToSelection, RemoveFromSelection and ScrollIntoView are TextRange's
    16-19. None of them may be a slot it calls."""
    source = (ROOT / "voisr" / "uia.py").read_text()
    slots = {name: int(number) for name, number in
             re.findall(r"^(_[A-Z0-9]+_[A-Z0-9_]+) = (\d+)$", source, re.MULTILINE)}
    assert slots, "the slot constants moved"
    setters = {"_EL_": {3}, "_VP_": {3}, "_TR_": {16, 17, 18, 19}}
    for name, number in slots.items():
        for prefix, forbidden in setters.items():
            if name.startswith(prefix):
                assert number not in forbidden, name
    # every vtable call goes through a named slot, never a bare number
    assert not re.search(r"_vcall\([^,]+, \d", source)
    assert not re.search(r"_out\([^,]+, \d", source)


def test_worker_runs_jobs_and_reports_errors():
    worker = AXWorker()
    assert worker.submit(lambda: 5).result(timeout=2) == 5
    with pytest.raises(ValueError):
        worker.submit(lambda: (_ for _ in ()).throw(ValueError())).result(timeout=2)
    worker.close()


def test_each_worker_thread_is_set_up_once_before_its_jobs():
    calls = []
    worker = AXWorker(thread_setup=lambda: calls.append(threading.current_thread().name))
    assert worker.submit(lambda: threading.current_thread().name).result(timeout=2) == "voisr-ax"
    worker.submit(lambda: None).result(timeout=2)
    assert calls == ["voisr-ax"]
    worker.close()


def test_a_wedged_call_is_abandoned_and_refused_when_it_keeps_happening():
    release = threading.Event()
    now = [0.0]
    worker = AXWorker(clock=lambda: now[0])
    started = threading.Event()

    def wedge():
        started.set()
        release.wait(5)

    worker.submit(wedge)
    assert started.wait(2)
    now[0] += worker.WEDGED_S + 1
    # a new thread takes over: later work still runs
    assert worker.submit(lambda: "fine").result(timeout=2) == "fine"
    started.clear()
    worker.submit(wedge)
    assert started.wait(2)
    now[0] += worker.WEDGED_S + 1
    refused = worker.submit(lambda: "no")
    with pytest.raises(Exception):
        refused.result(timeout=2)
    release.set()
    time.sleep(0.05)
    assert worker.submit(lambda: "back").result(timeout=2) == "back"
    worker.close()


def test_submit_never_blocks_on_a_hung_call():
    release = threading.Event()
    worker = AXWorker()
    worker.submit(lambda: release.wait(5))
    started = time.monotonic()
    for _ in range(5):
        worker.submit(lambda: None)
    assert time.monotonic() - started < 0.1
    release.set()
    worker.close()


def test_tracker_does_nothing_when_turned_off():
    ax = FakeAX(textbox("text"))
    tracker = FocusTracker(ax, enabled=lambda: False)
    tracker.capture_press(1)
    state = tracker.session(1)
    assert state.press.result(timeout=2) is None
    assert ax.calls == [] and ax.front_calls == 0
    tracker.close()


def test_tracker_read_now_is_bounded_against_a_hung_app():
    release = threading.Event()

    class Hung(FakeAX):
        def focused_element(self, timeout_s):
            release.wait(5)  # as if UIA's transaction timeout were ignored
            return UNREADABLE

    tracker = FocusTracker(Hung(), enabled=lambda: True)
    started = time.monotonic()
    assert tracker.read_now(0.05) is None
    assert time.monotonic() - started < 0.5
    release.set()
    tracker.close()


def test_tracker_forgets_a_finished_dictation():
    tracker = FocusTracker(FakeAX(textbox("text")), enabled=lambda: True)
    tracker.capture_press(7)
    assert tracker.session(7) is not None
    tracker.forget(7)
    assert tracker.session(7) is None
    tracker.close()


def test_a_capture_after_forget_cannot_bring_the_session_back():
    ax = FakeAX(textbox("text"))
    tracker = FocusTracker(ax, enabled=lambda: True)
    tracker.forget(7)
    tracker.capture_press(7)
    tracker.capture_release(7)
    assert tracker.session(7) is None and tracker._sessions == {}
    tracker.worker.submit(lambda: None).result(timeout=2)
    assert ax.calls == [] and ax.front_calls == 0
    tracker.close()


def test_a_capture_queued_before_forget_reads_nothing():
    release = threading.Event()
    ax = FakeAX(textbox("text"))
    tracker = FocusTracker(ax, enabled=lambda: True)
    tracker.worker.submit(lambda: release.wait(5))   # keeps the next job queued
    tracker.capture_press(3)
    tracker.forget(3)
    release.set()
    tracker.worker.submit(lambda: None).result(timeout=2)
    assert ax.calls == [] and ax.front_calls == 0
    tracker.close()


def test_retired_tracking_stays_bounded_and_still_refuses_old_sequences():
    tracker = FocusTracker(FakeAX(textbox("text")), enabled=lambda: True)
    for sequence in range(1, 1000):
        tracker.forget(sequence)
    assert len(tracker._retired) <= tracker.MAX_RETIRED
    tracker.capture_press(1)     # long gone
    tracker.capture_press(999)
    assert tracker._sessions == {}
    tracker.capture_press(1000)  # a new dictation still works
    assert tracker.session(1000) is not None
    tracker.close()


def _done(capture):
    from concurrent.futures import Future
    future = Future()
    future.set_result(capture)
    return future


GOOD = FocusContext(bundle_id="good.exe", readable=True, value="x")
FAILED = FocusContext(reason="timeout")
NOT_EDITABLE = FocusContext(bundle_id="web.exe", reason="not_editable", window_title="T")
SECURE = FocusContext(bundle_id="pw.exe", secure=True, reason="secure")


@pytest.mark.parametrize("press, release, expected", [
    (GOOD, None, GOOD),
    (None, GOOD, GOOD),
    (FAILED, GOOD, GOOD),            # the failed press mustn't hide a good release
    (GOOD, FAILED, GOOD),
    (NOT_EDITABLE, FAILED, NOT_EDITABLE),   # its labels are still worth having
    (SECURE, GOOD, None),
    (GOOD, SECURE, None),
    (None, None, None),
])
def test_which_capture_gives_the_terms(press, release, expected):
    state = SessionFocus(press=_done(press) if press else None,
                         release=_done(release) if release else None)
    assert state.for_terms() == expected


@pytest.mark.parametrize("press, release, expected", [
    (GOOD, NOT_EDITABLE, NOT_EDITABLE),
    (GOOD, FAILED, GOOD),            # release couldn't tell: not its app
    (FAILED, FAILED, None),
    (GOOD, SECURE, SECURE),          # the caller leaves a secure one unnamed
])
def test_which_capture_names_the_app(press, release, expected):
    state = SessionFocus(press=_done(press), release=_done(release))
    assert state.for_app() == expected


def test_focuscontext_defaults_to_unreadable():
    focus = FocusContext()
    assert not focus.readable and focus.value is None
