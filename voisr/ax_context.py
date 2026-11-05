"""What is focused and what it says, read through UI Automation.

Ported from the macOS `voisr/ax_context.py`, which reads the Accessibility
API (AX); the name is kept so the two apps' modules line up. Everything here
is platform-neutral: the reads go through an adapter, and the real one,
`voisr.uia.UIAAdapter`, speaks UI Automation. Attribute names and roles are
UIA's: a role is a control type's programmatic name ("Edit", "Document").

Raw text from here lives in memory for one dictation and is never logged or
written to disk: FocusContext's repr leaves it out, and errors are reduced to a
reason word. The adapter can only read: it never sets a value, a selection or
focus in another app.

Nothing here runs on the hotkey path. Captures run on one AXWorker thread;
callers that wait do so through Future.result(timeout).
"""
import os
import queue as queue_module
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

DEFAULT_BUDGET_S = 0.15
EXCERPT_CHARS = 500
MAX_VALUE_CHARS = 15_000
NEARBY_MAX_NODES = 200
NEARBY_MAX_STRINGS = 10
NEARBY_MAX_CHARS = 2_000

# UIA control types. A Document is Word's page, Windows 11 Notepad's text and
# a rich text box; a web page's is one too, but read-only (see read_textbox).
EDITABLE_ROLES = frozenset({"Edit", "Document", "ComboBox"})
STATIC_TEXT_ROLES = frozenset({"Text"})

# Never read, whatever the field says. Lowercase process image names; an entry
# ending in "*" is a family of names.
DENYLIST = (
    # Windows' own credential, sign-in and settings surfaces
    "consent.exe", "credentialuibroker.exe", "logonui.exe", "lockapp.exe",
    "systemsettings.exe", "securityhealthhost.exe",
    # password managers and authenticators
    "1password*", "bitwarden*", "lastpass*", "dashlane*", "keepass*",
    "keeper*", "nordpass*", "proton pass*", "protonpass*", "roboform*",
    "enpass*", "authy*", "oktaverify*", "pwsafe*", "stpass*",
)

# Absence proves nothing here (see the spec's C): terminals collapse or wrap
# what they receive, and web editors rewrite it.
TERMINALS = frozenset({
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe",
    "powershell.exe", "pwsh.exe", "wsl.exe", "mintty.exe", "alacritty.exe",
    "wezterm-gui.exe", "hyper.exe", "tabby.exe", "warp.exe", "putty.exe",
    "kitty.exe", "ghostty.exe",
})
BROWSERS = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "arc.exe", "chromium.exe", "iexplore.exe", "waterfox.exe",
    "librewolf.exe", "zen.exe", "floorp.exe", "thorium.exe",
    # WebView2's content process: a web page inside some other app
    "msedgewebview2.exe",
})
# Processes that only host another app's window or web content. The app the
# user sees is the foreground one, so that is what gets named.
HOST_PROCESSES = frozenset({"msedgewebview2.exe", "applicationframehost.exe"})


class _Unreadable:
    """An attribute that could not be read. Not the same as an empty one."""

    def __repr__(self):
        return "UNREADABLE"

    def __bool__(self):
        return False


UNREADABLE = _Unreadable()


def denied(app_id) -> bool:
    if not app_id:
        return False
    app_id = app_id.lower()
    return any(app_id.startswith(entry[:-1]) if entry.endswith("*")
               else app_id == entry for entry in DENYLIST)


@dataclass(frozen=True)
class FocusContext:
    # The app's process image name, lowercase ("notepad.exe"). Called bundle_id
    # as on the Mac, so the code that uses it reads the same on both.
    bundle_id: str | None = None
    app_name: str | None = None
    role: str | None = None
    element_id: str | None = None
    secure: bool = False
    readable: bool = False
    # why there is no text: no_focused_element, not_editable, unreadable,
    # too_long, secure, timeout, self
    reason: str | None = None
    caret_known: bool = False
    trust_absence: bool = True  # a missing paste means it really didn't land
    window_title: str = field(default="", repr=False)
    before_text: str = field(default="", repr=False)
    after_text: str = field(default="", repr=False)
    selected_text: str = field(default="", repr=False)
    nearby_texts: tuple[str, ...] = field(default=(), repr=False)
    value: str | None = field(default=None, repr=False)
    element: Any = field(default=None, repr=False, compare=False)


class AXAdapter(Protocol):
    """Read-only, and every read reports failure as UNREADABLE, not a raise.

    Attributes: ControlType (str), IsPassword (bool), IsReadOnly (bool, or
    UNREADABLE with no value pattern), Value (str, at most MAX_VALUE_CHARS + 1
    characters), SelectedTextRange ((start, length) in UTF-16 units of Value),
    SelectedText (str), Window (the top-level window's element), Title (a
    window's title), Children (list of elements), Name (str)."""

    def frontmost(self) -> tuple[str | None, str | None, int | None]:
        """(app id, name, pid) of the foreground app."""

    def focused_element(self, timeout_s: float) -> Any:
        """The focused element, or UNREADABLE."""

    def attribute(self, element, name: str, timeout_s: float) -> Any:
        """A str, bool, list, element, (start, length) tuple, or UNREADABLE."""

    def owner(self, element) -> tuple[str | None, str | None, int | None]:
        """(app id, name, pid) of the process the element belongs to."""

    def element_id(self, element) -> str:
        """Stable for one element while it lives."""

    def is_electron(self, pid) -> bool:
        """Electron, CEF or WebView2: web content, whatever the app."""


def _split_at_utf16(value: str, location: int, length: int) -> tuple[str, str, str]:
    """UIA text ranges, like AX's, count UTF-16 units, Python strings count
    code points: split with emoji before the caret must not land one character
    off per emoji."""
    if value.isascii():
        end = location + length
        return value[:location], value[location:end], value[end:]
    units = value.encode("utf-16-le", "surrogatepass")
    start, end = location * 2, (location + length) * 2

    def part(data):
        return data.decode("utf-16-le", "ignore")

    return part(units[:start]), part(units[start:end]), part(units[end:])


def _trim_before(text: str, limit: int = EXCERPT_CHARS) -> str:
    """The last `limit` chars, cut back to start on a whole word."""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    for i, ch in enumerate(cut):
        if ch.isspace():
            return cut[i + 1:]
    return cut


def _trim_after(text: str, limit: int = EXCERPT_CHARS) -> str:
    """The first `limit` chars, cut back to end on a whole word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut) - 1, -1, -1):
        if cut[i].isspace():
            return cut[:i]
    return cut


def trust_absence(adapter, bundle_id, pid) -> bool:
    if bundle_id in TERMINALS or bundle_id in BROWSERS:
        return False
    try:
        return not adapter.is_electron(pid)
    except Exception:
        return False


def read_textbox(adapter, element, remaining: Callable[[], float]) -> dict:
    """The field's text as FocusContext fields. Control type and IsPassword
    come first: until it is known that the field isn't a password, no text is
    asked for, and one whose answer can't be had isn't read at all.
    `remaining` is the budget left in seconds; out of budget is a reason,
    never an empty string."""
    role = adapter.attribute(element, "ControlType", remaining())
    if role is UNREADABLE or not isinstance(role, str):
        return {"reason": "unreadable"}
    password = adapter.attribute(element, "IsPassword", remaining())
    if password is True:
        return {"role": role, "secure": True, "reason": "secure"}
    if password is not False:
        return {"role": role, "reason": "unreadable"}
    if role not in EDITABLE_ROLES:
        return {"role": role, "reason": "not_editable"}
    # A web page is a read-only Document; so is a read-only box
    if adapter.attribute(element, "IsReadOnly", remaining()) is True:
        return {"role": role, "reason": "not_editable"}
    if remaining() <= 0:
        return {"role": role, "reason": "timeout"}
    value = adapter.attribute(element, "Value", remaining())
    if value is UNREADABLE or not isinstance(value, str):
        reason = "timeout" if remaining() <= 0 else "unreadable"
        return {"role": role, "reason": reason}
    if len(value) > MAX_VALUE_CHARS:
        selected = adapter.attribute(element, "SelectedText", remaining())
        return {"role": role, "reason": "too_long",
                "selected_text": selected if isinstance(selected, str) else ""}
    fields = {"role": role, "readable": True, "value": value, "selected_text": ""}
    selection = adapter.attribute(element, "SelectedTextRange", remaining())
    if isinstance(selection, tuple) and len(selection) == 2:
        before, chosen, after = _split_at_utf16(value, *selection)
        fields.update(before_text=_trim_before(before),
                      after_text=_trim_after(after),
                      selected_text=chosen, caret_known=True)
    return fields


def _nearby_texts(adapter, element, remaining) -> tuple[str, tuple[str, ...]]:
    """(window title, static text from the focused window), breadth first.
    Static text only: other fields aren't read or descended into, and a
    password field is left alone entirely."""
    window = adapter.attribute(element, "Window", remaining())
    if window is UNREADABLE:
        return "", ()
    title = adapter.attribute(window, "Title", remaining())
    title = title if isinstance(title, str) else ""
    texts: list[str] = []
    total = 0
    queue = [window]
    seen_nodes = 0
    while (queue and seen_nodes < NEARBY_MAX_NODES and remaining() > 0
           and len(texts) < NEARBY_MAX_STRINGS and total < NEARBY_MAX_CHARS):
        node = queue.pop(0)
        seen_nodes += 1
        role = adapter.attribute(node, "ControlType", remaining())
        if role is UNREADABLE:
            continue
        if adapter.attribute(node, "IsPassword", remaining()) is not False:
            continue
        if role in STATIC_TEXT_ROLES:
            text = adapter.attribute(node, "Name", remaining())
            if isinstance(text, str) and text.strip():
                text = text.strip()[:NEARBY_MAX_CHARS - total]
                texts.append(text)
                total += len(text)
            continue
        if (role in EDITABLE_ROLES
                and adapter.attribute(node, "IsReadOnly", remaining()) is not True):
            continue  # another field: its text is its own
        children = adapter.attribute(node, "Children", remaining())
        if isinstance(children, (list, tuple)):
            queue.extend(children[:NEARBY_MAX_NODES])
    return title, tuple(texts)


def capture_focus(adapter, budget_s: float = DEFAULT_BUDGET_S, *,
                  textbox_only: bool = False,
                  clock: Callable[[], float] = time.monotonic) -> FocusContext:
    """Snapshot the focused field. Never raises."""
    try:
        return _capture(adapter, budget_s, textbox_only, clock)
    except Exception:
        return FocusContext(reason="unreadable")


def _capture(adapter, budget_s, textbox_only, clock) -> FocusContext:
    deadline = clock() + budget_s

    def remaining():
        return deadline - clock()

    bundle_id, app_name, pid = adapter.frontmost()
    if denied(bundle_id):
        return FocusContext(bundle_id=bundle_id, secure=True, reason="secure")
    if pid == os.getpid():
        # UIA into this process would wait on our own window's thread
        return FocusContext(bundle_id=bundle_id, app_name=app_name, reason="self")
    element = adapter.focused_element(remaining())
    if element is UNREADABLE or element is None:
        reason = "timeout" if remaining() <= 0 else "no_focused_element"
        return FocusContext(bundle_id=bundle_id, app_name=app_name, reason=reason)
    # A password manager's quick-access panel can take focus without becoming
    # the foreground app, so the foreground check alone isn't enough.
    owner_id, owner_name, owner_pid = adapter.owner(element)
    if denied(owner_id):
        return FocusContext(bundle_id=owner_id, secure=True, reason="secure")
    if owner_pid == os.getpid():
        return FocusContext(bundle_id=owner_id, app_name=owner_name, reason="self")
    if owner_id not in HOST_PROCESSES:
        bundle_id = owner_id or bundle_id
        app_name = owner_name or app_name
    fields = read_textbox(adapter, element, remaining)
    if fields.get("secure"):
        return FocusContext(bundle_id=bundle_id, app_name=app_name,
                            role=fields.get("role"), secure=True, reason="secure")
    title, nearby = "", ()
    if not textbox_only and remaining() > 0:
        title, nearby = _nearby_texts(adapter, element, remaining)
    return FocusContext(
        bundle_id=bundle_id, app_name=app_name,
        element_id=adapter.element_id(element), element=element,
        window_title=title, nearby_texts=nearby,
        trust_absence=(trust_absence(adapter, bundle_id, owner_pid or pid)
                       and owner_id not in BROWSERS),
        **fields)


class AXUnavailable(RuntimeError):
    pass


def _join_mta() -> None:
    """UI Automation clients belong in the multithreaded apartment: an STA
    thread would have to pump messages for UIA's calls back. Each worker
    thread joins it once. Off Windows there is nothing to join."""
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass


class AXWorker:
    """One thread for UIA work, one job at a time. UIA's transaction timeout
    should end every call, but a call that doesn't return anyway is abandoned
    after WEDGED_S and a new thread takes over the queue. A second wedged
    thread means something is badly wrong: jobs are refused until one comes
    back, so wedged threads can't pile up."""

    WEDGED_S = 2.0
    MAX_ABANDONED = 2

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 thread_setup: Callable[[], None] = _join_mta):
        self._clock = clock
        self._thread_setup = thread_setup
        self._lock = threading.Lock()
        self._queue = None
        self._thread = None
        self._running_since = None
        self._abandoned: set[threading.Thread] = set()
        self._closed = False

    def submit(self, fn) -> Future:
        """Never blocks. The Future fails with AXUnavailable when refused."""
        future = Future()
        with self._lock:
            if self._closed or not self._ready_locked():
                future.set_exception(AXUnavailable("UIA worker unavailable"))
                return future
            self._queue.put((fn, future))
        return future

    def _ready_locked(self) -> bool:
        self._abandoned = {t for t in self._abandoned if t.is_alive()}
        if (self._thread is not None and self._running_since is not None
                and self._clock() - self._running_since > self.WEDGED_S):
            self._abandoned.add(self._thread)
            self._thread = None
        if self._thread is None:
            if len(self._abandoned) >= self.MAX_ABANDONED:
                return False
            if self._queue is None:
                self._queue = queue_module.Queue()
            self._running_since = None
            self._thread = threading.Thread(
                target=self._run, args=(self._queue,), name="voisr-ax", daemon=True)
            self._thread.start()
        return True

    def _run(self, jobs) -> None:
        me = threading.current_thread()
        try:
            self._thread_setup()
        except Exception:
            pass
        while True:
            job = jobs.get()
            if job is None:
                return
            fn, future = job
            with self._lock:
                if self._thread is me:
                    self._running_since = self._clock()
            if future.set_running_or_notify_cancel():
                try:
                    result = fn()
                except BaseException as error:
                    future.set_exception(error)
                else:
                    future.set_result(result)
            with self._lock:
                if self._thread is not me:
                    return  # replaced while wedged; the new thread has the queue
                self._running_since = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._queue is not None:
                self._queue.put(None)


@dataclass
class SessionFocus:
    """The captures of one dictation. Their Futures are checked, not waited
    for, so a slow app costs a dictation its context and nothing else."""
    press: Future | None = None
    release: Future | None = None
    press_ms: float = 0.0
    release_ms: float = 0.0
    retired: bool = False  # the dictation is over; a capture still queued skips

    @staticmethod
    def _finished(future) -> FocusContext | None:
        if future is None or not future.done() or future.cancelled():
            return None
        try:
            return future.result(timeout=0)
        except Exception:
            return None

    def press_now(self) -> FocusContext | None:
        return self._finished(self.press)

    def release_now(self) -> FocusContext | None:
        return self._finished(self.release)

    def for_terms(self) -> FocusContext | None:
        """The capture to pick terms from. Any secure capture means none: the
        user was in a password field while speaking. Else the first whose field
        was readable, and failing that the press capture, whose window title
        and nearby labels may still be good. A failed capture is a truthy
        object, so `press or release` would never reach the good one."""
        captures = [c for c in (self.press_now(), self.release_now()) if c is not None]
        if any(c.secure for c in captures):
            return None
        for capture in captures:
            if capture.readable:
                return capture
        return captures[0] if captures else None

    def for_app(self) -> FocusContext | None:
        """The capture that names the app, the release one first as the paste
        goes there. One that couldn't tell (no app id) doesn't count."""
        for capture in (self.release_now(), self.press_now()):
            if capture is not None and capture.bundle_id:
                return capture
        return None

    def ax_ms(self) -> float:
        return self.press_ms + self.release_ms


class FocusTracker:
    """Runs and keeps each dictation's captures, keyed by session sequence.
    `enabled` is read inside each job, so turning use_screen_context off stops
    even a capture already queued."""

    MAX_RETIRED = 64

    def __init__(self, adapter, enabled: Callable[[], bool], worker: AXWorker | None = None,
                 budget_s: float = DEFAULT_BUDGET_S,
                 clock: Callable[[], float] = time.monotonic):
        self.adapter = adapter
        self._enabled = enabled
        self.worker = worker or AXWorker(clock=clock)
        self._budget_s = budget_s
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[int, SessionFocus] = {}
        # Retired sequences, so a capture queued just after forget() can't
        # bring one back and keep its screen text until Quit. Bounded: session
        # sequences only go up, so everything at or below the floor is retired.
        self._retired: set[int] = set()
        self._retired_floor = 0

    def enabled(self) -> bool:
        try:
            return bool(self._enabled())
        except Exception:
            return False

    def _run_capture(self, state: SessionFocus, slot: str, textbox_only: bool):
        if state.retired or not self.enabled():
            return None
        started = self._clock()
        try:
            return capture_focus(self.adapter, self._budget_s,
                                 textbox_only=textbox_only, clock=self._clock)
        finally:
            setattr(state, f"{slot}_ms", (self._clock() - started) * 1000)

    def _state(self, sequence) -> SessionFocus | None:
        """None for a dictation that is already over."""
        if sequence <= self._retired_floor or sequence in self._retired:
            return None
        return self._sessions.setdefault(sequence, SessionFocus())

    def capture_press(self, sequence) -> None:
        """Once per dictation. Only queues; never blocks its caller."""
        with self._lock:
            state = self._state(sequence)
            if state is None or state.press is not None:
                return
            state.press = self.worker.submit(
                lambda: self._run_capture(state, "press", False))

    def capture_release(self, sequence) -> None:
        with self._lock:
            state = self._state(sequence)
            if state is None or state.release is not None:
                return
            state.release = self.worker.submit(
                lambda: self._run_capture(state, "release", True))

    def session(self, sequence) -> SessionFocus | None:
        with self._lock:
            return self._sessions.get(sequence)

    def forget(self, sequence) -> None:
        """The dictation is over; its screen text goes with it."""
        with self._lock:
            state = self._sessions.pop(sequence, None)
            if state is not None:
                state.retired = True
            self._retired.add(sequence)
            if len(self._retired) > self.MAX_RETIRED:
                oldest = min(self._retired)
                self._retired.discard(oldest)
                self._retired_floor = max(self._retired_floor, oldest)

    def read_now(self, budget_s: float) -> FocusContext | None:
        """A textbox capture on the worker, waited for at most budget_s. None
        when it couldn't be had in time."""
        future = self.worker.submit(lambda: capture_focus(
            self.adapter, budget_s, textbox_only=True, clock=self._clock))
        try:
            return future.result(timeout=budget_s)
        except (FutureTimeout, AXUnavailable):
            future.cancel()
            return None
        except Exception:
            return None

    def close(self) -> None:
        self.worker.close()


def make_ax_adapter():
    """The real adapter, or None when UI Automation can't be reached; then
    Voisr simply runs without screen context."""
    try:
        from voisr.uia import UIAAdapter
        return UIAAdapter()
    except Exception as error:
        # the log, not print: the shipped build has no console
        from voisr.updater import _log
        _log(f"screen context unavailable ({error.__class__.__name__}: {error})")
        return None
