import pytest

from voisr.pasteboard import (
    CF_LOCALE,
    CF_OEMTEXT,
    CF_TEXT,
    CF_UNICODETEXT,
    ClipboardPreservationError,
    PasteboardItemSnapshot,
    PasteboardSnapshot,
    WindowsClipboard,
    is_synthesized,
)


class FakeWin32:
    """Stand-in for the Win32 clipboard surface WindowsClipboard calls."""

    def __init__(self, formats=None, sequence=100):
        self.formats = dict(formats or {CF_UNICODETEXT: "hello".encode("utf-16-le")})
        self.sequence = sequence
        self.open_failures = 0
        self.opened = 0
        self.closed = 0
        self.emptied = 0
        self.null_handles = set()
        self.names = {}

    def open(self):
        if self.open_failures:
            self.open_failures -= 1
            return False
        self.opened += 1
        return True

    def close(self):
        self.closed += 1

    def empty(self):
        self.emptied += 1
        self.formats.clear()
        self.sequence += 1

    def enum_formats(self):
        return list(self.formats)

    def get_data(self, fmt):
        if fmt in self.null_handles:
            return None
        return self.formats[fmt]

    def set_data(self, fmt, payload):
        self.formats[fmt] = payload
        return True

    def get_sequence(self):
        return self.sequence

    def format_name(self, fmt):
        return self.names.get(fmt)


def make(win32):
    return WindowsClipboard(win32=win32)


def test_synthesized_text_formats_are_recognised():
    assert is_synthesized(CF_TEXT)
    assert is_synthesized(CF_OEMTEXT)
    assert is_synthesized(CF_LOCALE)
    assert not is_synthesized(CF_UNICODETEXT)


def test_snapshot_captures_sequence_and_payload():
    win32 = FakeWin32()
    snap = make(win32).snapshot()
    assert snap.change_count == 100
    assert snap.items[0].payloads[0][0] == str(CF_UNICODETEXT)
    assert win32.closed == win32.opened


def test_snapshot_skips_synthesized_formats():
    win32 = FakeWin32({CF_UNICODETEXT: b"a", CF_TEXT: b"a", CF_LOCALE: b"x"})
    snap = make(win32).snapshot()
    captured = {p[0] for p in snap.items[0].payloads}
    assert captured == {str(CF_UNICODETEXT)}


def test_snapshot_marks_partial_on_null_handle():
    win32 = FakeWin32({CF_UNICODETEXT: b"a", 49161: b"b"})
    win32.null_handles.add(49161)
    snap = make(win32).snapshot()
    assert snap.partial is True
    captured = {p[0] for p in snap.items[0].payloads}
    assert captured == {str(CF_UNICODETEXT)}


def test_snapshot_is_not_partial_when_everything_read():
    assert make(FakeWin32()).snapshot().partial is False


def test_open_retries_then_succeeds():
    win32 = FakeWin32()
    win32.open_failures = 2
    assert make(win32).snapshot().change_count == 100
    assert win32.opened == 1


def test_open_exhaustion_raises_preservation_error():
    win32 = FakeWin32()
    win32.open_failures = 99
    with pytest.raises(ClipboardPreservationError, match="could not be opened"):
        make(win32).snapshot()


def test_sequence_change_during_capture_raises():
    win32 = FakeWin32({CF_UNICODETEXT: b"a"})
    clip = make(win32)
    original = win32.get_data

    def racing(fmt):
        win32.sequence += 1
        return original(fmt)

    win32.get_data = racing
    with pytest.raises(ClipboardPreservationError, match="changed during capture"):
        clip.snapshot()


def test_is_owned_compares_sequence():
    win32 = FakeWin32()
    clip = make(win32)
    assert clip.is_owned(100) is True
    assert clip.is_owned(99) is False


def test_write_text_returns_new_sequence_and_sets_unicode():
    win32 = FakeWin32()
    clip = make(win32)
    snap = clip.snapshot()
    count = clip.write_text_if_unchanged("bye", snap)
    assert count == win32.sequence
    assert win32.formats[CF_UNICODETEXT] == "bye".encode("utf-16-le") + b"\x00\x00"


def test_written_text_is_null_terminated():
    """CF_UNICODETEXT must be null-terminated or readers drop the last
    character — observed truncating 'TEMPORARY-VOISR' to 'TEMPORARY-VOIS'."""
    win32 = FakeWin32()
    clip = make(win32)
    snap = clip.snapshot()
    clip.write_text_if_unchanged("hello", snap)
    assert win32.formats[CF_UNICODETEXT].endswith(b"\x00\x00")
    assert win32.formats[CF_UNICODETEXT].decode("utf-16-le").rstrip("\x00") == "hello"


def test_write_text_refuses_when_clipboard_changed():
    win32 = FakeWin32()
    clip = make(win32)
    snap = clip.snapshot()
    win32.sequence += 1
    with pytest.raises(ClipboardPreservationError, match="changed before"):
        clip.write_text_if_unchanged("bye", snap)


def test_restore_puts_every_captured_format_back():
    win32 = FakeWin32({CF_UNICODETEXT: b"a", 49161: b"rich"})
    clip = make(win32)
    snap = clip.snapshot()
    count = clip.write_text_if_unchanged("temp", snap)
    assert clip.restore_if_unchanged(snap, count) is True
    assert win32.formats[49161] == b"rich"


def test_restore_skips_when_someone_else_took_the_clipboard():
    win32 = FakeWin32()
    clip = make(win32)
    snap = clip.snapshot()
    count = clip.write_text_if_unchanged("temp", snap)
    win32.sequence += 5
    assert clip.restore_if_unchanged(snap, count) is False


def test_empty_snapshot_restores_without_error():
    win32 = FakeWin32({})
    clip = make(win32)
    snap = clip.snapshot()
    count = clip.write_text_if_unchanged("temp", snap)
    assert clip.restore_if_unchanged(snap, count) is True


def test_snapshot_flags_a_password_manager_secret():
    exclude = 0xC123
    win32 = FakeWin32({CF_UNICODETEXT: "hunter2".encode("utf-16-le"), exclude: b""})
    win32.names[exclude] = "ExcludeClipboardContentFromMonitorProcessing"
    assert make(win32).snapshot().concealed is True


def test_snapshot_of_plain_text_is_not_concealed():
    assert make(FakeWin32()).snapshot().concealed is False
