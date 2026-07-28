"""Deliver dictated text to the frontmost app.

_paste_with_adapter and its helpers are ported unchanged from the macOS app:
the ordering and error-chaining contract there is exactly right and its tests
came across with it. Only the injection primitives are Windows-specific.
"""
import ctypes
import time
from ctypes import wintypes

from voisr.pasteboard import (
    CF_UNICODETEXT,
    ClipboardPreservationError,
    PasteDeliveryError,
    WindowsClipboard,
    _Win32Clipboard,
)

PASTE_GRACE_S = 0.3

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# Tags our own synthetic events so voisr.keytap can ignore them. LLKHF_INJECTED
# is not usable for this — it is set by every injecting app, not just ours.
VOISR_SIGNATURE = 0x564F4953  # 'VOIS'

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56

MODIFIER_KEYS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)

TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    # Padded to MOUSEINPUT's size (the largest INPUT union member) so this
    # struct's layout matches the real Win32 INPUT — SendInput validates
    # cbSize against the native struct size and rejects anything smaller.
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(vk: int, flags: int) -> None:
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0,
                          dwExtraInfo=VOISR_SIGNATURE)
    if not user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)):
        raise OSError(f"SendInput failed: {ctypes.get_last_error()}")


def _key_down(vk: int) -> None:
    _send(vk, 0)


def _key_up(vk: int) -> None:
    _send(vk, KEYEVENTF_KEYUP)


def _held_modifiers() -> list[int]:
    """Modifiers physically down right now. Push-to-talk means the user is
    holding a key while we inject, and a held Alt turns Ctrl+V into something
    else entirely — AltGr+Ctrl+V on many layouts."""
    return [vk for vk in MODIFIER_KEYS
            if user32.GetAsyncKeyState(vk) & 0x8000]


def _send_ctrl_v() -> bool:
    _key_down(VK_CONTROL)
    try:
        _key_down(VK_V)
        _key_up(VK_V)
    finally:
        _key_up(VK_CONTROL)
    return True


def _process_integrity_level(pid: int) -> int | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0,
                                     ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer,
                                            size, ctypes.byref(size)):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        count = advapi32.GetSidSubAuthorityCount(sid)[0]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        return int(advapi32.GetSidSubAuthority(sid, count - 1)[0])
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def foreground_is_elevated() -> bool:
    """True when the focused window outranks us, meaning SendInput will be
    silently swallowed by UIPI. Unknown is treated as not-elevated: guessing
    'blocked' would refuse pastes that would have worked."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        theirs = _process_integrity_level(pid.value)
        ours = _process_integrity_level(kernel32.GetCurrentProcessId())
        if theirs is None or ours is None:
            return False
        return theirs > ours
    except Exception:
        return False


def _post_ctrl_v() -> bool:
    """Inject Ctrl+V. Returns False when UIPI would swallow it — SendInput
    reports success in that case, so checking beforehand is the only way to
    avoid claiming a paste that never happened."""
    if foreground_is_elevated():
        return False
    held = _held_modifiers()
    for vk in held:
        _key_up(vk)
    try:
        return bool(_send_ctrl_v())
    finally:
        for vk in held:
            _key_down(vk)


def _fallback_ctrl_v() -> bool:
    """No second injection path exists on Windows. macOS falls back from
    CGEvent to AppleScript; here a failed SendInput means blocked, full stop.
    Kept so _paste_with_adapter's signature matches the ported tests."""
    return False


def _write_text(text: str) -> None:
    """Unconditional clipboard write, no preservation. Used only on the
    no-restore path; the preserving path goes through the adapter."""
    win32 = _Win32Clipboard()
    clip = WindowsClipboard(win32=win32)
    clip._open()
    try:
        win32.empty()
        # Null terminator is mandatory — without it readers drop the last
        # character. Same requirement as pasteboard.write_text_if_unchanged.
        if not win32.set_data(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00"):
            raise ClipboardPreservationError("clipboard write failed")
    finally:
        win32.close()


def _read_text() -> str:
    win32 = _Win32Clipboard()
    clip = WindowsClipboard(win32=win32)
    clip._open()
    try:
        data = win32.get_data(CF_UNICODETEXT)
    finally:
        win32.close()
    return data.decode("utf-16-le").rstrip("\x00") if data else ""


def set_clipboard(text: str) -> None:
    _write_text(text)


def get_clipboard() -> str:
    return _read_text()


def _restore_after_failure(adapter, snapshot, owner_count, error, *,
                           message, pasted):
    try:
        adapter.restore_if_unchanged(snapshot, owner_count)
    except Exception as restore_error:
        if isinstance(error, Exception):
            raise PasteDeliveryError(
                f"{message} and clipboard restore failed: {error}; "
                f"{restore_error}",
                pasted=pasted,
            ) from error
        raise error from restore_error
    if isinstance(error, Exception):
        raise PasteDeliveryError(
            f"{message}: {error}", pasted=pasted) from error
    raise error


def _restore_after_ownership_check_failure(adapter, snapshot, owner_count,
                                           error):
    try:
        adapter.restore_if_unchanged(snapshot, owner_count)
    except BaseException as restore_error:
        if not isinstance(error, Exception):
            raise error from restore_error
        if not isinstance(restore_error, Exception):
            raise restore_error from error
        raise ClipboardPreservationError(
            "clipboard ownership check and restore failed") from error
    if isinstance(error, ClipboardPreservationError):
        raise error
    if isinstance(error, Exception):
        raise ClipboardPreservationError(
            "clipboard ownership check failed") from error
    raise error


def _paste_with_adapter(text, *, adapter, post_ctrl_v,
                        fallback_ctrl_v, sleeper) -> bool:
    snapshot = adapter.snapshot()
    owner_count = adapter.write_text_if_unchanged(text, snapshot)
    try:
        owned = adapter.is_owned(owner_count)
    except BaseException as error:
        _restore_after_ownership_check_failure(
            adapter, snapshot, owner_count, error)
    if not owned:
        try:
            adapter.restore_if_unchanged(snapshot, owner_count)
        except Exception as error:
            raise PasteDeliveryError(
                f"clipboard ownership lost and restore failed: {error}",
                pasted=False,
            ) from error
        raise PasteDeliveryError(
            "clipboard ownership lost before paste", pasted=False)
    try:
        pasted = post_ctrl_v() or fallback_ctrl_v()
    except BaseException as error:
        _restore_after_failure(
            adapter, snapshot, owner_count, error,
            message="paste command failed", pasted=False)
    if not pasted:
        try:
            adapter.restore_if_unchanged(snapshot, owner_count)
        except Exception as restore_error:
            raise PasteDeliveryError(
                f"paste was blocked and clipboard restore failed: "
                f"{restore_error}",
                pasted=False,
            ) from restore_error
        raise PasteDeliveryError(
            "paste was blocked; clipboard preserved", pasted=False)
    try:
        sleeper(PASTE_GRACE_S)
    except BaseException as error:
        _restore_after_failure(
            adapter, snapshot, owner_count, error,
            message="dictation pasted but clipboard wait failed", pasted=True)
    try:
        adapter.restore_if_unchanged(snapshot, owner_count)
    except Exception as error:
        raise PasteDeliveryError(
            f"dictation pasted but clipboard restore failed: {error}",
            pasted=True,
        ) from error
    return True


def paste_at_cursor(text: str, restore_clipboard: bool = True) -> bool:
    """Put text on the clipboard and paste it into the frontmost app.

    With restoration enabled (the default), a blocked paste raises a
    preservation-aware PasteDeliveryError. Set restore_clipboard=False to
    leave text available for a manual paste and receive False when blocked.
    """
    if not restore_clipboard:
        set_clipboard(text)
        try:
            return _post_ctrl_v() or _fallback_ctrl_v()
        except Exception:
            return False
    return _paste_with_adapter(
        text,
        adapter=WindowsClipboard(),
        post_ctrl_v=_post_ctrl_v,
        fallback_ctrl_v=_fallback_ctrl_v,
        sleeper=time.sleep,
    )
