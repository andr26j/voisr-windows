"""Win32 clipboard with macOS-compatible preservation semantics.

GetClipboardSequenceNumber() is the direct analogue of NSPasteboard's
changeCount, so the ownership model from the macOS app transfers intact and
voisr.inject's paste state machine is unchanged.

Three Windows-only hazards are handled here:
  * OpenClipboard fails transiently whenever another process holds the
    clipboard, so every open is retried with backoff.
  * Delayed rendering: apps like Excel advertise formats with a NULL handle
    and only render on request, which can hang. Those are skipped and the
    snapshot is flagged partial rather than silently claimed lossless.
  * Windows synthesizes CF_TEXT/CF_OEMTEXT/CF_LOCALE from CF_UNICODETEXT.
    Re-setting them fights the ones Windows regenerates, so they are skipped.
"""
import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_ENHMETAFILE = 14
CF_UNICODETEXT = 13
CF_LOCALE = 16
CF_DIBV5 = 17

# Formats Windows generates from another format on the clipboard. Capturing
# and re-setting these conflicts with the OS-generated copies.
SYNTHESIZED_FORMATS = frozenset({
    CF_TEXT, CF_OEMTEXT, CF_LOCALE,      # from CF_UNICODETEXT
    CF_BITMAP, CF_PALETTE, CF_DIBV5,     # from CF_DIB
    CF_ENHMETAFILE,                      # from CF_METAFILEPICT
})

OPEN_RETRIES = 10
OPEN_BACKOFF_S = 0.02
GMEM_MOVEABLE = 0x0002


def is_synthesized(fmt: int) -> bool:
    return fmt in SYNTHESIZED_FORMATS


@dataclass(frozen=True)
class PasteboardItemSnapshot:
    payloads: tuple[tuple[str, bytes], ...]


# Password managers (KeePass, Bitwarden, 1Password) mark a secret by adding
# one of these registered formats alongside the text, so clipboard history
# and sync skip it.
CONCEALED_FORMAT_NAMES = frozenset({
    "ExcludeClipboardContentFromMonitorProcessing",
    "Clipboard Viewer Ignore",
})


@dataclass(frozen=True)
class PasteboardSnapshot:
    change_count: int
    items: tuple[PasteboardItemSnapshot, ...]
    partial: bool = field(default=False)
    concealed: bool = field(default=False)


class PasteDeliveryError(RuntimeError):
    def __init__(self, message: str, *, pasted: bool = False,
                 fallback_safe: bool = False):
        super().__init__(message)
        self.pasted = pasted
        self.fallback_safe = fallback_safe


class ClipboardPreservationError(PasteDeliveryError):
    pass


class _Win32Clipboard:
    """Thin ctypes surface. Isolated so tests can substitute a fake."""

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalSize.restype = ctypes.c_size_t
        self.kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self.kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self.user32.GetClipboardData.restype = wintypes.HANDLE
        self.user32.GetClipboardData.argtypes = [wintypes.UINT]
        self.user32.SetClipboardData.restype = wintypes.HANDLE
        self.user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    def open(self) -> bool:
        return bool(self.user32.OpenClipboard(None))

    def close(self) -> None:
        self.user32.CloseClipboard()

    def empty(self) -> None:
        self.user32.EmptyClipboard()

    def enum_formats(self) -> list[int]:
        formats, fmt = [], 0
        while True:
            fmt = self.user32.EnumClipboardFormats(fmt)
            if not fmt:
                return formats
            formats.append(fmt)

    def get_data(self, fmt: int) -> bytes | None:
        handle = self.user32.GetClipboardData(fmt)
        if not handle:
            return None
        size = self.kernel32.GlobalSize(handle)
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            self.kernel32.GlobalUnlock(handle)

    def set_data(self, fmt: int, payload: bytes) -> bool:
        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload) or 1)
        if not handle:
            return False
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            self.kernel32.GlobalUnlock(handle)
        return bool(self.user32.SetClipboardData(fmt, handle))

    def get_sequence(self) -> int:
        return int(self.user32.GetClipboardSequenceNumber())

    def format_name(self, fmt: int) -> str | None:
        """Name of a registered format; None for the predefined CF_* ones."""
        buffer = ctypes.create_unicode_buffer(256)
        length = self.user32.GetClipboardFormatNameW(fmt, buffer, len(buffer))
        return buffer.value if length else None


class WindowsClipboard:
    """Same interface as the macOS AppKitPasteboard: snapshot,
    write_text_if_unchanged, is_owned, restore_if_unchanged."""

    def __init__(self, win32=None):
        self._w = win32 if win32 is not None else _Win32Clipboard()

    def _open(self) -> None:
        for attempt in range(OPEN_RETRIES):
            if self._w.open():
                return
            time.sleep(OPEN_BACKOFF_S * (attempt + 1))
        raise ClipboardPreservationError(
            "clipboard could not be opened; another app is holding it")

    def snapshot(self) -> PasteboardSnapshot:
        self._open()
        try:
            sequence = self._w.get_sequence()
            payloads, partial, concealed = [], False, False
            for fmt in self._w.enum_formats():
                if self._w.format_name(fmt) in CONCEALED_FORMAT_NAMES:
                    concealed = True
                if is_synthesized(fmt):
                    continue
                data = self._w.get_data(fmt)
                if data is None:
                    partial = True  # delayed-render owner declined
                    continue
                payloads.append((str(fmt), data))
            if self._w.get_sequence() != sequence:
                raise ClipboardPreservationError("clipboard changed during capture")
        finally:
            self._w.close()
        items = (PasteboardItemSnapshot(tuple(payloads)),) if payloads else ()
        return PasteboardSnapshot(sequence, items, partial, concealed)

    def is_owned(self, expected_count) -> bool:
        try:
            return self._w.get_sequence() == expected_count
        except ClipboardPreservationError:
            raise
        except Exception as error:
            raise ClipboardPreservationError(
                "clipboard ownership check failed") from error

    def write_text_if_unchanged(self, text, snapshot) -> int:
        if self._w.get_sequence() != snapshot.change_count:
            raise ClipboardPreservationError(
                "clipboard changed before temporary write")
        self._open()
        try:
            self._w.empty()
            if not self._w.set_data(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00"):
                raise ClipboardPreservationError("temporary clipboard write failed")
        finally:
            self._w.close()
        # Read after close: CloseClipboard notifies clipboard-format
        # listeners, and any listener that reacts synchronously (clipboard
        # history services, sync utilities) can bump the sequence number
        # further. Reading before close would return a stale count that
        # never matches reality, making every restore falsely refuse.
        return self._w.get_sequence()

    def restore_if_unchanged(self, snapshot, expected_change_count) -> bool:
        if self._w.get_sequence() != expected_change_count:
            return False
        self._open()
        try:
            self._w.empty()
            for item in snapshot.items:
                for fmt, payload in item.payloads:
                    if not self._w.set_data(int(fmt), payload):
                        raise ClipboardPreservationError(
                            f"clipboard type could not be restored: {fmt}")
        finally:
            self._w.close()
        return True
