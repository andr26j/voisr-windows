"""The UI Automation adapter against real native text boxes in another
process (tests/_text_window.py): what it reads, what it refuses to read, and
that a hung app costs a read, never a wrong answer."""
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="real UI Automation needs Windows")

if sys.platform == "win32":
    from voisr.ax_context import (EDITABLE_ROLES, AXWorker, FocusTracker,
                                  _nearby_texts, capture_focus, read_textbox)
    from voisr.uia import UIAAdapter

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                    wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                    wintypes.LPARAM]

WINDOW = Path(__file__).with_name("_text_window.py")
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_CHAR = 0x0102
EM_GETSEL = 0x00B0
EM_SETSEL = 0x00B1
WM_APP_HANG = 0x8000 + 1
BUDGET_S = 2.0  # a CI runner's first read of a window can be slow


class TextWindow:
    def __init__(self, *args):
        self.proc = subprocess.Popen([sys.executable, str(WINDOW), *args],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True)
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the test window didn't start")
        self.info = json.loads(line)

    def __getitem__(self, name):
        return self.info[name]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(5)
        except Exception:
            self.proc.kill()


def text_of(hwnd) -> str:
    """WM_GETTEXT, as GetWindowText won't fetch another process's box."""
    length = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(hwnd, WM_GETTEXT, length + 1, ctypes.addressof(buffer))
    return buffer.value


def set_text(hwnd, text):
    buffer = ctypes.create_unicode_buffer(text)
    user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.addressof(buffer))


def type_into(hwnd, text):
    """Typing as the box sees it: one WM_CHAR per UTF-16 unit."""
    units = text.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(units), 2):
        user32.PostMessageW(hwnd, WM_CHAR, int.from_bytes(units[i:i + 2], "little"), 0)
    deadline = time.monotonic() + 5
    while text_of(hwnd) != text and time.monotonic() < deadline:
        time.sleep(0.02)
    assert text_of(hwnd) == text


def select(hwnd, start, end):
    user32.SendMessageW(hwnd, EM_SETSEL, start, end)


def selection_of(hwnd):
    packed = user32.SendMessageW(hwnd, EM_GETSEL, 0, 0)
    return packed & 0xFFFF, (packed >> 16) & 0xFFFF


@pytest.fixture(scope="module")
def window():
    win = TextWindow()
    yield win
    win.close()


@pytest.fixture(scope="module")
def adapter():
    return UIAAdapter()


@pytest.fixture(scope="module")
def worker():
    w = AXWorker()
    yield w
    w.close()


def on_worker(worker, fn):
    return worker.submit(fn).result(timeout=15)


def read(worker, adapter, hwnd, budget=BUDGET_S):
    def job():
        deadline = time.monotonic() + budget
        element = adapter.element_from_handle(hwnd, budget)
        return read_textbox(adapter, element, lambda: deadline - time.monotonic())
    return on_worker(worker, job)


class Spy:
    """The real adapter, with every attribute asked for written down."""

    def __init__(self, real):
        self.real = real
        self.asked = []

    def attribute(self, element, name, timeout_s):
        self.asked.append(name)
        return self.real.attribute(element, name, timeout_s)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_it_reads_typed_text_and_the_caret(window, adapter, worker):
    edit = window["edit"]
    set_text(edit, "")
    type_into(edit, "hello world")
    select(edit, 5, 5)
    fields = read(worker, adapter, edit)
    assert fields["role"] in EDITABLE_ROLES
    assert fields["readable"] is True and fields["value"] == "hello world"
    assert fields["caret_known"] is True
    assert fields["before_text"] == "hello" and fields["after_text"] == " world"
    assert fields["selected_text"] == ""
    # read-only: the box is as it was
    assert text_of(edit) == "hello world" and selection_of(edit) == (5, 5)


def test_a_selection_splits_in_utf16_units_past_an_emoji(window, adapter, worker):
    edit = window["edit"]
    set_text(edit, "")
    type_into(edit, "a😀b chosen c")
    select(edit, 5, 11)  # UTF-16 units: the emoji is two
    fields = read(worker, adapter, edit)
    assert fields["value"] == "a😀b chosen c"
    assert fields["before_text"] == "a😀b " and fields["selected_text"] == "chosen"
    assert fields["after_text"] == " c"
    assert selection_of(edit) == (5, 11)


def test_a_password_box_is_never_asked_for_its_text(window, adapter, worker):
    spy = Spy(adapter)
    fields = read(worker, spy, window["password"])
    assert fields == {"role": "Edit", "secure": True, "reason": "secure"}
    assert not {"Value", "SelectedText", "SelectedTextRange"} & set(spy.asked)


def test_a_read_only_box_is_not_editable(window, adapter, worker):
    fields = read(worker, adapter, window["readonly"])
    assert fields["reason"] == "not_editable"
    assert "value" not in fields


def test_a_multiline_box_reads_its_lines(window, adapter, worker):
    box = window["multiline"]
    set_text(box, "first line\r\nsecond line")
    select(box, 12, 12)  # after "first line\r\n"
    fields = read(worker, adapter, box)
    assert fields["value"] == "first line\r\nsecond line"
    assert fields["before_text"] == "first line\r\n"
    assert fields["after_text"] == "second line"


def test_a_rich_edit_box_reads_through_its_own_provider(window, adapter, worker):
    rich = window["rich"]
    set_text(rich, "")
    type_into(rich, "rich text here")
    select(rich, 4, 4)
    fields = read(worker, adapter, rich)
    assert fields["role"] in EDITABLE_ROLES
    assert fields["value"].rstrip("\r\n") == "rich text here"
    assert fields["caret_known"] is True
    assert fields["before_text"] == "rich"


def test_the_caret_range_of_text_pattern2_where_offered(window, adapter, worker):
    """GetSelection is tried first; this calls GetCaretRange directly on any
    box that offers TextPattern2, so that slot is exercised too."""
    from voisr import uia

    def job():
        found = {}
        for name in ("edit", "rich", "multiline"):
            element = adapter.element_from_handle(window[name], BUDGET_S)
            if element.props.get("IsTextPattern2Available") is not True:
                continue
            adapter._prepare(BUDGET_S)
            caret = adapter._caret_range(element)
            found[name] = caret is not None
            uia._release(caret)
        return found

    found = on_worker(worker, job)
    print(f"TextPattern2 offered by: {sorted(found) or 'none'}")
    assert all(found.values())


def test_window_title_and_a_nearby_label(window, adapter, worker):
    def job():
        deadline = time.monotonic() + BUDGET_S
        element = adapter.element_from_handle(window["edit"], BUDGET_S)
        return _nearby_texts(adapter, element, lambda: deadline - time.monotonic())

    title, nearby = on_worker(worker, job)
    assert title == "Voisr UIA test window"
    assert "Invoice for Tareq Haddad" in nearby
    # other boxes' text is theirs, and a password is no one's
    assert not any("hunter2" in text or "fixed text" in text for text in nearby)


def test_element_ids_are_stable_for_one_box_and_differ_between_boxes(window, adapter, worker):
    def job():
        one = adapter.element_from_handle(window["edit"], BUDGET_S)
        again = adapter.element_from_handle(window["edit"], BUDGET_S)
        other = adapter.element_from_handle(window["rich"], BUDGET_S)
        return adapter.element_id(one), adapter.element_id(again), adapter.element_id(other)

    one, again, other = on_worker(worker, job)
    assert one == again != other
    assert adapter.owner(on_worker(worker, lambda: adapter.element_from_handle(
        window["edit"], BUDGET_S)))[2] == window["pid"]


def test_a_hung_app_costs_the_read_within_the_budget(window, adapter, worker):
    """The box's thread sleeps 3 s. The paste check's read must come back in
    its budget, and as unknown, not as an empty or unchanged field."""
    edit = window["edit"]
    set_text(edit, "")
    type_into(edit, "before the hang")
    element = on_worker(worker, lambda: adapter.element_from_handle(edit, BUDGET_S))
    user32.PostMessageW(window["main"], WM_APP_HANG, 3000, 0)
    time.sleep(0.2)  # let the hang begin
    started = time.monotonic()

    def job():
        deadline = time.monotonic() + 0.15
        return read_textbox(adapter, element, lambda: deadline - time.monotonic())

    future = worker.submit(job)
    try:
        fields = future.result(timeout=0.15 + 0.5)
    except Exception:
        fields = None  # still waiting: unknown all the same
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert fields is None or not fields.get("readable")
    # and UIA's own timeouts ended the call, so the worker isn't wedged
    finished = future.result(timeout=5)
    took = time.monotonic() - started
    assert not finished.get("readable"), finished
    assert took < 1.5, f"the read itself took {took:.2f} s: UIA's timeouts didn't end it"
    time.sleep(max(0.0, 3.3 - (time.monotonic() - started)))  # the window answers again


@pytest.fixture
def focused_window():
    win = TextWindow("--focus")
    if not win["foreground"]:
        win.close()
        pytest.skip("this desktop wouldn't give the test window the foreground")
    yield win
    win.close()


def test_a_capture_of_the_focused_box(focused_window, adapter, worker):
    edit = focused_window["edit"]
    type_into(edit, "dear Tareq")
    select(edit, 4, 4)
    focus = on_worker(worker, lambda: capture_focus(adapter, BUDGET_S))
    assert focus.bundle_id == Path(sys.executable).name.lower()
    assert focus.readable and focus.value == "dear Tareq"
    assert focus.before_text == "dear" and focus.after_text == " Tareq"
    assert focus.window_title == "Voisr UIA test window"
    assert "Invoice for Tareq Haddad" in focus.nearby_texts
    assert focus.trust_absence is True


def test_a_real_paste_is_verified(focused_window, adapter):
    """The real paste path, clipboard and Ctrl+V, into the focused box, with
    the paste check reading it back."""
    from voisr import inject
    from voisr.paste_verify import check_delivery

    edit = focused_window["edit"]
    type_into(edit, "note: ")
    tracker = FocusTracker(adapter, lambda: True)
    try:
        before = tracker.read_now(BUDGET_S)
        assert before is not None and before.readable
        verdicts = []

        def verifier(old_clipboard):
            after = tracker.read_now(BUDGET_S)
            verdict = check_delivery(before, after, "dictated words", old_clipboard)
            verdicts.append(verdict)
            return verdict.missed

        assert inject.paste_at_cursor("dictated words", verifier=verifier) is True
        assert text_of(edit) == "note: dictated words"
        assert [v.status for v in verdicts] == ["verified"]
    finally:
        tracker.close()
