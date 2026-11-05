"""UI Automation, read-only: the Windows adapter behind voisr.ax_context.

UIAutomationCore's own COM interfaces (UIA3), called through ctypes. Not the
managed System.Windows.Automation client that pythonnet could load: that one
has no way to bound a call, so an app that has stopped answering holds the
read for as long as UIA's default transaction timeout (20 s), where the paste
check has 150 ms. IUIAutomation2's connection and transaction timeouts are the
equivalent of the Mac's AXUIElementSetMessagingTimeout, and are set to what
is left of the budget before every call. The managed client also routes
classic controls through its own client-side proxies and has no TextPattern2.
Not comtypes either: it writes the wrappers it generates from the type library
to a cache that a frozen app has to ship or rebuild, and this needs a dozen
methods of eight interfaces. Their slots below are UIAutomationClient.h's
(COM vtables never change), and tests/test_uia_real.py calls every one of them
against real controls.

Read-only by construction: the setters that sit beside these slots (SetFocus,
ValuePattern.SetValue, TextRange.Select) are never named here.

Threading: UIA clients belong in the multithreaded apartment. AXWorker joins
it on each of its threads, and everything here that touches COM runs on one of
them. Element references that outlive a call are released there too: one
dropped on another thread is queued, and the next read releases it.
"""
import ctypes
import os
import threading
import time
import uuid
from collections import deque
from ctypes import POINTER, byref, c_int, c_long, c_ushort, c_void_p, wintypes
from pathlib import Path

from voisr.ax_context import MAX_VALUE_CHARS, NEARBY_MAX_NODES, UNREADABLE

HRESULT = ctypes.HRESULT

_ole32 = ctypes.WinDLL("ole32")
_oleaut32 = ctypes.WinDLL("oleaut32")
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


CLSID_CUIAutomation8 = _guid("e22ad333-b25f-460c-83d0-0581107395c9")
CLSID_CUIAutomation = _guid("ff48dba4-60ef-4201-aa87-54103eef594e")
IID_IUIAutomation2 = _guid("34723aff-0c9d-49d0-9896-7ab52df8cd8a")
IID_IUIAutomation = _guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")
IID_IUIAutomationValuePattern = _guid("a94cd8b1-0844-4cd6-9d2d-640537ab39e9")
IID_IUIAutomationTextPattern = _guid("32eba289-3583-42c9-9c59-3b6d9a1e9b6a")
IID_IUIAutomationTextPattern2 = _guid("506a921a-fcc9-409f-b23b-37eb74106872")
CLSCTX_INPROC_SERVER = 0x1

# Property, pattern and control type ids (UIAutomationClient.h)
UIA_RuntimeIdPropertyId = 30000
UIA_ProcessIdPropertyId = 30002
UIA_ControlTypePropertyId = 30003
UIA_NamePropertyId = 30005
UIA_IsPasswordPropertyId = 30019
UIA_NativeWindowHandlePropertyId = 30020
UIA_IsTextPatternAvailablePropertyId = 30040
UIA_IsValuePatternAvailablePropertyId = 30043
UIA_ValueIsReadOnlyPropertyId = 30046
UIA_IsTextPattern2AvailablePropertyId = 30119
UIA_ValuePatternId = 10002
UIA_TextPatternId = 10014
UIA_TextPattern2Id = 10024
TreeScope_Element = 0x1
TreeScope_Children = 0x2
TextPatternRangeEndpoint_Start = 0
TextPatternRangeEndpoint_End = 1

CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}

# The cached properties each kind of read asks for, by the names the adapter
# answers to. No text in any of them: a field's text is asked for only once
# its IsPassword has been seen to be False.
_FOCUS_PROPERTIES = {
    "ControlType": UIA_ControlTypePropertyId,
    "IsPassword": UIA_IsPasswordPropertyId,
    "ProcessId": UIA_ProcessIdPropertyId,
    "NativeWindowHandle": UIA_NativeWindowHandlePropertyId,
    "RuntimeId": UIA_RuntimeIdPropertyId,
    "IsReadOnly": UIA_ValueIsReadOnlyPropertyId,
    "IsValuePatternAvailable": UIA_IsValuePatternAvailablePropertyId,
    "IsTextPatternAvailable": UIA_IsTextPatternAvailablePropertyId,
    "IsTextPattern2Available": UIA_IsTextPattern2AvailablePropertyId,
}
# A label's text is its Name; a field's Name is its label, never its text.
_NODE_PROPERTIES = {
    "ControlType": UIA_ControlTypePropertyId,
    "IsPassword": UIA_IsPasswordPropertyId,
    "Name": UIA_NamePropertyId,
    "IsReadOnly": UIA_ValueIsReadOnlyPropertyId,
}

_IUNKNOWN_RELEASE = 2
# IUIAutomation(2) slots
_UIA_ELEMENT_FROM_HANDLE_BUILD_CACHE = 10
_UIA_GET_FOCUSED_ELEMENT_BUILD_CACHE = 12
_UIA_CREATE_CACHE_REQUEST = 20
_UIA2_PUT_CONNECTION_TIMEOUT = 61
_UIA2_PUT_TRANSACTION_TIMEOUT = 63
# IUIAutomationElement slots
_EL_BUILD_UPDATED_CACHE = 9
_EL_GET_CACHED_PROPERTY_VALUE = 12
_EL_GET_CURRENT_PATTERN_AS = 14
_EL_GET_CACHED_CHILDREN = 19
# IUIAutomationCacheRequest slots
_CR_ADD_PROPERTY = 3
_CR_PUT_TREE_SCOPE = 7
# IUIAutomationValuePattern slots
_VP_GET_CURRENT_VALUE = 4
# IUIAutomationTextPattern(2) slots
_TP_GET_SELECTION = 5
_TP_GET_DOCUMENT_RANGE = 7
_TP2_GET_CARET_RANGE = 10
# IUIAutomationTextRange slots
_TR_CLONE = 3
_TR_GET_TEXT = 12
_TR_MOVE_ENDPOINT_BY_RANGE = 15
# IUIAutomationTextRangeArray and IUIAutomationElementArray slots
_ARR_GET_LENGTH = 3
_ARR_GET_ELEMENT = 4

VT_EMPTY, VT_I4, VT_BSTR, VT_BOOL, VT_ARRAY = 0, 3, 8, 11, 0x2000


class _VariantValue(ctypes.Union):
    _fields_ = [("llVal", ctypes.c_longlong), ("lVal", c_long),
                ("boolVal", ctypes.c_short), ("bstrVal", c_void_p),
                ("parray", c_void_p), ("_record", c_void_p * 2)]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", c_ushort), ("_r1", c_ushort), ("_r2", c_ushort),
                ("_r3", c_ushort), ("value", _VariantValue)]


_ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, wintypes.DWORD,
                                    POINTER(GUID), POINTER(c_void_p)]
_ole32.CoCreateInstance.restype = HRESULT
_ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = c_long  # S_FALSE and RPC_E_CHANGED_MODE are fine
_oleaut32.SysStringLen.argtypes = [c_void_p]
_oleaut32.SysStringLen.restype = ctypes.c_uint
_oleaut32.SysFreeString.argtypes = [c_void_p]
_oleaut32.SysFreeString.restype = None
_oleaut32.VariantClear.argtypes = [POINTER(VARIANT)]
_oleaut32.VariantClear.restype = HRESULT
_oleaut32.SafeArrayGetLBound.argtypes = [c_void_p, ctypes.c_uint, POINTER(c_long)]
_oleaut32.SafeArrayGetLBound.restype = HRESULT
_oleaut32.SafeArrayGetUBound.argtypes = [c_void_p, ctypes.c_uint, POINTER(c_long)]
_oleaut32.SafeArrayGetUBound.restype = HRESULT
_oleaut32.SafeArrayAccessData.argtypes = [c_void_p, POINTER(c_void_p)]
_oleaut32.SafeArrayAccessData.restype = HRESULT
_oleaut32.SafeArrayUnaccessData.argtypes = [c_void_p]
_oleaut32.SafeArrayUnaccessData.restype = HRESULT

_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SNAPSHOT_FRESH_S = 0.5  # Value and the selection read together are one read

_PROTOTYPES = {}


def _vcall(ptr, index, argtypes=(), *args, restype=HRESULT):
    """Call slot `index` of a COM object's vtable. A failed HRESULT raises
    OSError (ctypes' HRESULT does that), which callers turn into UNREADABLE."""
    key = (index, argtypes, restype)
    proto = _PROTOTYPES.get(key)
    if proto is None:
        proto = _PROTOTYPES[key] = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return proto(vtable[index])(ptr, *args)


def _release(ptr) -> None:
    if ptr:
        _vcall(ptr, _IUNKNOWN_RELEASE, restype=ctypes.c_ulong)


def _out(ptr, index, *args, argtypes=()) -> int | None:
    """A method whose last parameter is an interface out-pointer: returns the
    new reference (the caller owns it) or None for a NULL one."""
    result = c_void_p()
    _vcall(ptr, index, (*argtypes, POINTER(c_void_p)), *args, byref(result))
    return result.value


def _bstr(ptr) -> str:
    """The text of a BSTR, which is then freed. Embedded NULs survive."""
    if not ptr:
        return ""
    try:
        return ctypes.wstring_at(ptr, _oleaut32.SysStringLen(ptr))
    finally:
        _oleaut32.SysFreeString(ptr)


def _variant_value(var: VARIANT):
    """A property's value, then the VARIANT is cleared. UIA's "not supported"
    marker is an IUnknown, and anything else unexpected is UNREADABLE too."""
    try:
        vt = var.vt
        if vt == VT_BOOL:
            return var.value.boolVal != 0
        if vt == VT_I4:
            return int(var.value.lVal)
        if vt == VT_BSTR:
            ptr = var.value.bstrVal
            return ctypes.wstring_at(ptr, _oleaut32.SysStringLen(ptr)) if ptr else ""
        if vt == VT_ARRAY | VT_I4 and var.value.parray:
            return _int_array(var.value.parray)
        return UNREADABLE
    finally:
        _oleaut32.VariantClear(byref(var))


def _int_array(parray) -> tuple[int, ...]:
    low, high, data = c_long(), c_long(), c_void_p()
    _oleaut32.SafeArrayGetLBound(parray, 1, byref(low))
    _oleaut32.SafeArrayGetUBound(parray, 1, byref(high))
    count = high.value - low.value + 1
    if count <= 0:
        return ()
    _oleaut32.SafeArrayAccessData(parray, byref(data))
    try:
        return tuple(ctypes.cast(data, POINTER(c_long))[:count])
    finally:
        _oleaut32.SafeArrayUnaccessData(parray)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


# References dropped on a thread that isn't a UIA worker, for the next read
# to release in the multithreaded apartment.
_PENDING_RELEASE: deque = deque()


def _drain_releases() -> None:
    while True:
        try:
            ptr = _PENDING_RELEASE.popleft()
        except IndexError:
            return
        try:
            _release(ptr)
        except Exception:
            pass


class UIAElement:
    """One counted reference to an IUIAutomationElement, with the properties
    that were cached when it was fetched. Holds no text but its read
    snapshot, which is replaced on every read."""

    def __init__(self, ptr: int, props: dict, hwnd: int | None = None):
        self.ptr = ptr
        self.props = props
        self.hwnd = hwnd  # for a window: its handle, for the title
        self.snapshot = None  # (taken at, the last text read) for SNAPSHOT_FRESH_S

    def __del__(self):
        ptr, self.ptr = self.ptr, None
        if ptr:
            try:
                _PENDING_RELEASE.append(ptr)
            except Exception:
                pass  # interpreter shutdown

    def __repr__(self):
        return f"UIAElement({self.props.get('ControlType')!r})"


def _window_pid(hwnd) -> int | None:
    pid = wintypes.DWORD()
    if not _user32.GetWindowThreadProcessId(hwnd, byref(pid)):
        return None
    return pid.value or None


def process_image(pid) -> str | None:
    """The full path of a process's executable, or None."""
    if not pid:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, byref(size)):
            return None
        return buffer.value
    finally:
        _kernel32.CloseHandle(handle)


def _describe(pid) -> tuple[str | None, str | None, int | None]:
    """(app id, name, pid): the image name in lowercase, and its stem."""
    if not pid:
        return None, None, None
    path = process_image(pid)
    if not path:
        return None, None, pid
    image = os.path.basename(path)
    return image.lower(), os.path.splitext(image)[0], pid


def _window_text(hwnd) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _hosted_app_pid(frame) -> int | None:
    """A Store app's window is ApplicationFrameHost's frame around the app's
    own window: the first child owned by another process is the app."""
    frame_pid = _window_pid(frame)
    found = []

    def visit(child, _):
        pid = _window_pid(child)
        if pid and pid != frame_pid:
            found.append(pid)
            return False
        return True

    _user32.EnumChildWindows(frame, _WNDENUMPROC(visit), 0)
    return found[0] if found else None


class UIAAdapter:
    """The real thing. Construction only checks that UIAutomationCore loads:
    the COM object is made on the first read, on a UIA worker thread, so the
    app's main thread keeps whatever apartment its window needs."""

    def __init__(self):
        ctypes.WinDLL("UIAutomationCore")
        self._lock = threading.Lock()
        self._uia = None
        self._has_timeouts = False
        self._requests: dict[str, int] = {}
        self._electron: dict[str, bool] = {}

    # -- setup -------------------------------------------------------------

    def _automation(self) -> int:
        with self._lock:
            if self._uia is None:
                # S_FALSE when this thread already joined; a thread that is
                # an STA still works, it just isn't where UIA is happiest
                _ole32.CoInitializeEx(None, 0)
                self._uia, self._has_timeouts = self._create()
                self._requests = {
                    "focus": self._cache_request(self._uia, _FOCUS_PROPERTIES,
                                                 TreeScope_Element),
                    "node": self._cache_request(self._uia, _NODE_PROPERTIES,
                                                TreeScope_Element),
                    "children": self._cache_request(
                        self._uia, _NODE_PROPERTIES,
                        TreeScope_Element | TreeScope_Children),
                }
            return self._uia

    @staticmethod
    def _create() -> tuple[int, bool]:
        """CUIAutomation8 for its timeouts (Windows 8 and later), else the
        original, whose calls can't be bounded."""
        out = c_void_p()
        try:
            _ole32.CoCreateInstance(byref(CLSID_CUIAutomation8), None,
                                    CLSCTX_INPROC_SERVER, byref(IID_IUIAutomation2),
                                    byref(out))
            return out.value, True
        except OSError:
            _ole32.CoCreateInstance(byref(CLSID_CUIAutomation), None,
                                    CLSCTX_INPROC_SERVER, byref(IID_IUIAutomation),
                                    byref(out))
            return out.value, False

    @staticmethod
    def _cache_request(uia, properties: dict, scope: int) -> int:
        request = _out(uia, _UIA_CREATE_CACHE_REQUEST)
        for property_id in properties.values():
            _vcall(request, _CR_ADD_PROPERTY, (c_int,), property_id)
        _vcall(request, _CR_PUT_TREE_SCOPE, (c_int,), scope)
        return request

    def _prepare(self, timeout_s: float) -> int:
        """Ready for a call that may take at most timeout_s. Returns the
        IUIAutomation pointer."""
        _drain_releases()
        uia = self._automation()
        if self._has_timeouts:
            ms = max(1, int(timeout_s * 1000))
            _vcall(uia, _UIA2_PUT_CONNECTION_TIMEOUT, (wintypes.DWORD,), ms)
            _vcall(uia, _UIA2_PUT_TRANSACTION_TIMEOUT, (wintypes.DWORD,), ms)
        return uia

    @staticmethod
    def _wrap(ptr, properties: dict, hwnd=None) -> UIAElement:
        props = {}
        for name, property_id in properties.items():
            var = VARIANT()
            try:
                _vcall(ptr, _EL_GET_CACHED_PROPERTY_VALUE, (c_int, POINTER(VARIANT)),
                       property_id, byref(var))
            except OSError:
                props[name] = UNREADABLE
                continue
            props[name] = _variant_value(var)
        if isinstance(props.get("ControlType"), int):
            props["ControlType"] = CONTROL_TYPES.get(props["ControlType"],
                                                     str(props["ControlType"]))
        return UIAElement(ptr, props, hwnd)

    # -- AXAdapter ---------------------------------------------------------

    def frontmost(self):
        """No UIA: the foreground window and its process."""
        try:
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return None, None, None
            app = _describe(_window_pid(hwnd))
            if app[0] == "applicationframehost.exe":
                hosted = _hosted_app_pid(hwnd)
                if hosted:
                    app = _describe(hosted)
            return app
        except Exception:
            return None, None, None

    def focused_element(self, timeout_s):
        if timeout_s <= 0:
            return UNREADABLE
        try:
            uia = self._prepare(timeout_s)
            ptr = _out(uia, _UIA_GET_FOCUSED_ELEMENT_BUILD_CACHE, self._requests["focus"],
                       argtypes=(c_void_p,))
        except OSError:
            return UNREADABLE
        return self._wrap(ptr, _FOCUS_PROPERTIES) if ptr else UNREADABLE

    def element_from_handle(self, hwnd, timeout_s):
        """A window's element as focused_element would describe it. For the
        tests, and for the Window attribute (with the node properties)."""
        return self._from_handle(hwnd, timeout_s, "focus", _FOCUS_PROPERTIES)

    def _from_handle(self, hwnd, timeout_s, request, properties):
        if timeout_s <= 0 or not hwnd:
            return UNREADABLE
        try:
            uia = self._prepare(timeout_s)
            ptr = _out(uia, _UIA_ELEMENT_FROM_HANDLE_BUILD_CACHE, wintypes.HWND(hwnd),
                       self._requests[request], argtypes=(wintypes.HWND, c_void_p))
        except OSError:
            return UNREADABLE
        return self._wrap(ptr, properties, hwnd) if ptr else UNREADABLE

    def attribute(self, element, name, timeout_s):
        if timeout_s <= 0 or not isinstance(element, UIAElement) or not element.ptr:
            return UNREADABLE
        if name in ("ControlType", "IsPassword", "IsReadOnly", "Name"):
            return element.props.get(name, UNREADABLE)
        if name == "Title":
            return _window_text(element.hwnd) if element.hwnd else UNREADABLE
        try:
            if name == "Window":
                return self._window(element, timeout_s)
            self._prepare(timeout_s)
            if name == "Value":
                snapshot = self._read_text(element)
                return snapshot["value"] if snapshot else UNREADABLE
            if name in ("SelectedTextRange", "SelectedText"):
                snapshot = element.snapshot
                if snapshot is None or time.monotonic() - snapshot[0] > SNAPSHOT_FRESH_S:
                    snapshot = (time.monotonic(), self._read_text(element))
                text = snapshot[1]
                if not text:
                    return UNREADABLE
                key = "selection" if name == "SelectedTextRange" else "selected"
                return text[key] if text[key] is not None else UNREADABLE
            if name == "Children":
                return self._children(element)
        except OSError:
            return UNREADABLE
        return UNREADABLE

    def owner(self, element):
        pid = element.props.get("ProcessId") if isinstance(element, UIAElement) else None
        if not isinstance(pid, int):
            return None, None, None
        return _describe(pid)

    def element_id(self, element):
        props = element.props if isinstance(element, UIAElement) else {}
        runtime_id = props.get("RuntimeId")
        if not isinstance(runtime_id, tuple) or not runtime_id:
            return f"el-{id(element)}"
        return f"{props.get('ProcessId')}:" + ".".join(str(n) for n in runtime_id)

    def is_electron(self, pid):
        """Electron, CEF or WebView2: all ship Chromium's resource packs beside
        the executable, or are WebView2's own content process."""
        path = process_image(pid)
        if not path:
            return False
        if path not in self._electron:
            folder = Path(path).parent
            self._electron[path] = (
                os.path.basename(path).lower() == "msedgewebview2.exe"
                or (folder / "resources" / "app.asar").exists()
                or (folder / "chrome_100_percent.pak").exists())
        return self._electron[path]

    # -- reads -------------------------------------------------------------

    def _pattern(self, element, pattern_id, iid) -> int | None:
        return _out(element.ptr, _EL_GET_CURRENT_PATTERN_AS, pattern_id, byref(iid),
                    argtypes=(c_int, POINTER(GUID)))

    def _read_text(self, element) -> dict | None:
        """{"value", "selection", "selected"}: the field's text and where the
        selection sits in it, as one read. None when neither pattern answers."""
        props = element.props
        text = None
        if props.get("IsTextPatternAvailable") is True:
            text = self._read_text_pattern(element)
        if text is None and props.get("IsValuePatternAvailable") is True:
            text = self._read_value_pattern(element)
        element.snapshot = (time.monotonic(), text)
        return text

    def _read_value_pattern(self, element) -> dict | None:
        pattern = self._pattern(element, UIA_ValuePatternId, IID_IUIAutomationValuePattern)
        if not pattern:
            return None
        try:
            raw = c_void_p()
            _vcall(pattern, _VP_GET_CURRENT_VALUE, (POINTER(c_void_p),), byref(raw))
            value = _bstr(raw.value)
        finally:
            _release(pattern)
        # no caret without the text pattern
        return {"value": value[:MAX_VALUE_CHARS + 1], "selection": None, "selected": None}

    @staticmethod
    def _range_text(text_range, limit) -> str:
        raw = c_void_p()
        _vcall(text_range, _TR_GET_TEXT, (c_int, POINTER(c_void_p)), limit, byref(raw))
        return _bstr(raw.value)

    def _caret(self, element, pattern) -> int | None:
        """The selection's first range, or with none, TextPattern2's caret
        range. A new reference, or None."""
        return self._selection_range(pattern) or self._caret_range(element)

    @staticmethod
    def _selection_range(pattern) -> int | None:
        try:
            ranges = _out(pattern, _TP_GET_SELECTION)
        except OSError:
            return None  # a field that doesn't do selections
        if not ranges:
            return None
        try:
            count = c_int()
            _vcall(ranges, _ARR_GET_LENGTH, (POINTER(c_int),), byref(count))
            if count.value > 0:
                return _out(ranges, _ARR_GET_ELEMENT, 0, argtypes=(c_int,))
            return None
        finally:
            _release(ranges)

    def _caret_range(self, element) -> int | None:
        if element.props.get("IsTextPattern2Available") is not True:
            return None
        second = self._pattern(element, UIA_TextPattern2Id, IID_IUIAutomationTextPattern2)
        if not second:
            return None
        try:
            active, caret = wintypes.BOOL(), c_void_p()
            _vcall(second, _TP2_GET_CARET_RANGE, (POINTER(wintypes.BOOL), POINTER(c_void_p)),
                   byref(active), byref(caret))
            return caret.value
        finally:
            _release(second)

    def _read_text_pattern(self, element) -> dict | None:
        pattern = self._pattern(element, UIA_TextPatternId, IID_IUIAutomationTextPattern)
        if not pattern:
            return None
        document = caret = before = None
        try:
            document = _out(pattern, _TP_GET_DOCUMENT_RANGE)
            if not document:
                return None
            value = self._range_text(document, MAX_VALUE_CHARS + 1)
            result = {"value": value, "selection": None, "selected": None}
            caret = self._caret(element, pattern)
            if not caret:
                return result
            selected = self._range_text(caret, MAX_VALUE_CHARS + 1)
            result["selected"] = selected
            if len(value) > MAX_VALUE_CHARS:
                return result
            # the document from its start to the selection's start
            before = _out(document, _TR_CLONE)
            _vcall(before, _TR_MOVE_ENDPOINT_BY_RANGE, (c_int, c_void_p, c_int),
                   TextPatternRangeEndpoint_End, caret, TextPatternRangeEndpoint_Start)
            head = self._range_text(before, MAX_VALUE_CHARS + 1)
            # Ranges of one document that don't add up are a read that raced
            # an edit: the caret is unknown, not guessed.
            if value.startswith(head) and value.startswith(selected, len(head)):
                result["selection"] = (_utf16_len(head), _utf16_len(selected))
            return result
        finally:
            for ptr in (before, caret, document, pattern):
                if ptr:
                    _release(ptr)

    def _window(self, element, timeout_s):
        """The top-level window the element is in. A web page's elements have
        no window handle of their own; the foreground window holds them."""
        hwnd = element.props.get("NativeWindowHandle")
        if not isinstance(hwnd, int) or not hwnd:
            hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return UNREADABLE
        root = _user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        return self._from_handle(root, timeout_s, "node", _NODE_PROPERTIES)

    def _children(self, element) -> list:
        updated = _out(element.ptr, _EL_BUILD_UPDATED_CACHE, self._requests["children"],
                       argtypes=(c_void_p,))
        if not updated:
            return []
        try:
            children = _out(updated, _EL_GET_CACHED_CHILDREN)
        finally:
            _release(updated)
        if not children:
            return []
        try:
            count = c_int()
            _vcall(children, _ARR_GET_LENGTH, (POINTER(c_int),), byref(count))
            out = []
            for i in range(min(count.value, NEARBY_MAX_NODES)):
                child = _out(children, _ARR_GET_ELEMENT, i, argtypes=(c_int,))
                if child:
                    out.append(self._wrap(child, _NODE_PROPERTIES))
            return out
        finally:
            _release(children)
