"""A kept-loaded whisper.cpp server, so a dictation doesn't reload the model.

Voisr starts one `whisper-server.exe` for the configured model when a
recording is known to be a real dictation, sends each clip to it over a
loopback-only connection, and unloads it after an idle timeout. This follows
Handy's behaviour: load on key press, in the background, in parallel with
capture; an idle watcher that never fires during a recording or
transcription; "Immediately" unloads after each dictation (here: never
starts a server, so the per-dictation whisper-cli path stays exactly as it
was).

Rules this module keeps:
  * The server listens on 127.0.0.1 only, on a free port. Requests use
    http.client directly, which never consults system proxy settings.
    Loopback-only also means Windows Firewall never asks about it.
  * Every wait is bounded. Any failure raises ServerError and the caller falls
    back to whisper-cli for that dictation. After a failure the server is not
    retried for FAILURE_COOLDOWN_S, so one broken server costs one slow
    dictation, not all of them.
  * The server dies with Voisr: it runs inside a kill-on-close job object
    (whisper_guard.py), so a crash, Task Manager or a missed shutdown can't
    leave it holding the model in memory. There is nothing left over to
    clean up at the next start.
  * No console window: the server is created with CREATE_NO_WINDOW.

Logging goes to voisr.log (updater._log): print() is silent in the windowed
build. Rows hold reasons and exit codes, never audio or text.
"""
import http.client
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

from voisr.paths import find_binary

# Handy's ModelUnloadTimeout choices; "Immediately" keeps per-dictation cli.
DEFAULT_UNLOAD = "min5"
UNLOAD_SECONDS = {
    "never": None, "immediately": 0,
    "min2": 120, "min5": 300, "min10": 600, "min15": 900, "hour1": 3600,
}

READY_TIMEOUT_S = 30.0       # model load, bounded
WAIT_FOR_READY_S = 15.0      # most a release waits for a still-loading server
FAILURE_COOLDOWN_S = 60.0
IDLE_POLL_S = 5.0
# Windows takes about two seconds to refuse a connection to a loopback port
# nobody listens on yet (it retries the SYN), so a probe that waited that
# long would notice a loaded model up to that much late. A connect to a
# listening port takes microseconds; the answer itself gets the full timeout.
PROBE_CONNECT_S = 0.2


class ServerError(RuntimeError):
    """The warm path failed; the caller falls back to whisper-cli."""


def _log(message: str) -> None:
    try:
        from voisr.updater import _log as log
        log(f"whisper-server: {message}")
    except Exception:
        pass


def valid_unload(setting) -> bool:
    """A known unload choice. Anything else, including an unhashable value
    from a hand-edited config, is not."""
    return isinstance(setting, str) and setting in UNLOAD_SECONDS


def unload_seconds(setting) -> int | None:
    """Idle seconds before unloading; None = never. Unknown values read as the
    default, so a hand-edited config can't disable the unload."""
    if not valid_unload(setting):
        setting = DEFAULT_UNLOAD
    return UNLOAD_SECONDS[setting]


def uses_server(setting) -> bool:
    return setting != "immediately"


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _multipart(fields: dict, wav_bytes: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                     f'name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
                 + wav_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _http_probe(port: int, timeout: float) -> bool:
    """True once the server answers HTTP. whisper-server (v1.9.1) loads the
    model before it binds its port, so an answer means the model is loaded."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port,
                                          timeout=min(timeout, PROBE_CONNECT_S))
        try:
            conn.connect()
            conn.sock.settimeout(timeout)
            conn.request("GET", "/")
            conn.getresponse().read()
            return True
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return False


def _http_infer(port: int, wav_bytes: bytes, fields: dict, timeout: float) -> str:
    body, ctype = _multipart(fields, wav_bytes)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", "/inference", body=body,
                     headers={"Content-Type": ctype})
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as error:
        raise ServerError(f"server request failed: {error}") from error
    finally:
        conn.close()
    try:
        data = json.loads(raw)
        if resp.status != 200 or "error" in data:
            raise ServerError(f"server error: {data.get('error', resp.status)}")
        return data["text"].strip()
    except (ValueError, KeyError, AttributeError, TypeError) as error:
        raise ServerError(f"bad server response: {raw[:80]!r}") from error


def _spawn_in_job(command):
    from voisr import whisper_guard
    return whisper_guard.spawn(command)


class _Process:
    """A running server, inside Voisr's job object."""

    def __init__(self, popen, port, model):
        self.popen, self.port, self.model = popen, port, model
        self.inflight = 0       # requests being served (WhisperServer's lock)
        self.retired = False    # unloaded while a request was in flight

    def alive(self) -> bool:
        return self.popen.poll() is None

    def exit_code(self):
        return self.popen.poll()

    def stop(self, grace: float = 5.0) -> None:
        """TerminateProcess: the server has nothing to save, and its memory
        is free the moment it returns. Popen holds the process handle, so
        the pid can't have been reused by anything else."""
        try:
            self.popen.terminate()
        except OSError:
            pass
        try:
            self.popen.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            # can't happen short of a hung kernel; the job ends it at exit
            _log("did not exit after TerminateProcess")


def _describe_exit(code) -> str:
    """An NTSTATUS in hex, as Windows documents them: 0xc0000135 is a DLL
    missing beside the exe, 0xc000001d a CPU without the instructions the
    build needs. Small codes are whisper-server's own (1: port in use,
    3: model failed to load)."""
    if code is None:
        return "running"
    if isinstance(code, int) and not 0 <= code <= 255:
        return f"exit code {code & 0xFFFFFFFF:#010x}"
    return f"exit code {code}"


class WhisperServer:
    """One kept-loaded whisper-server for one model at a time.

    prewarm() never blocks. transcribe() blocks for a bounded time and
    raises ServerError on any failure. set_busy() marks a recording or
    transcription in flight, which holds off the idle unload.

    An unload that is on purpose (idle, a new model, a new setting) is not a
    failure: it starts no cooldown, and a request already being served is
    left to finish before its server is stopped. Each unload bumps `_gen`, so
    a loader thread that started before it can tell it is stale and stops
    its own process; a later request always gets a loader of the current
    generation."""

    def __init__(self, *, find=find_binary, spawn=_spawn_in_job,
                 probe=_http_probe, infer=_http_infer, port_fn=free_port,
                 clock=time.monotonic, ready_timeout=READY_TIMEOUT_S,
                 wait_for_ready=WAIT_FOR_READY_S, poll=IDLE_POLL_S,
                 cooldown=FAILURE_COOLDOWN_S):
        self._find, self._spawn = find, spawn
        self._probe, self._infer, self._port_fn = probe, infer, port_fn
        self._clock = clock
        self._ready_timeout = ready_timeout
        self._wait_for_ready = wait_for_ready
        self._poll = poll
        self._cooldown = cooldown
        self._cond = threading.Condition()
        self._proc = None            # _Process, once started
        self._ready = False          # the model answered
        self._gen = 0                # bumped by every unload
        self._loading = {}           # model path -> generation of its loader
        self._failed_at = None
        self._unload = DEFAULT_UNLOAD
        self._busy = False
        self._last_activity = clock()
        self._closed = False
        self._watcher = None

    # -- public ---------------------------------------------------------

    def start_cleanup(self) -> list[int]:
        """Nothing to do on Windows: a server can't outlive the Voisr that
        started it (see whisper_guard.py). Kept so callers match the Mac."""
        return []

    def configure(self, unload) -> None:
        """Apply the unload setting; 'immediately' unloads right now (after
        any request in flight)."""
        with self._cond:
            self._unload = unload if valid_unload(unload) else DEFAULT_UNLOAD
            self._cond.notify_all()
            off = not uses_server(self._unload)
        if off:
            self.unload()

    def set_busy(self, busy: bool) -> None:
        with self._cond:
            if self._busy and not busy:
                self._last_activity = self._clock()
            self._busy = bool(busy)

    def prewarm(self, model_path, unload=None) -> None:
        """Start loading `model_path` in the background. Returns at once."""
        if unload is not None:
            self.configure(unload)
        with self._cond:
            if not uses_server(self._unload) or self._closed \
                    or self._in_cooldown():
                return
            self._failed_at = None
            self._last_activity = self._clock()
            self._ensure_loading_locked(str(model_path))

    def loaded_pid(self):
        """Pid of the server that is loaded and answering, else None."""
        with self._cond:
            proc = self._proc
            if proc is not None and self._ready and proc.alive():
                return getattr(proc.popen, "pid", None)
        return None

    def transcribe(self, wav_path, model_path, prompt=None):
        """Text for `wav_path` from the warm server, loading it if needed.
        Returns (text, info) with info {"model_wait": ms}. Raises ServerError.
        The unload setting is not taken from the caller: a queued dictation
        carries the setting from when it began, and must not overwrite a
        newer one (the Api calls configure() when Settings changes)."""
        model_path = str(model_path)
        if not uses_server(self._unload):
            raise ServerError("kept-loaded mode is off")
        if not Path(model_path).is_file():
            raise ServerError(f"model missing: {model_path}")
        started = self._clock()
        deadline = started + self._wait_for_ready
        with self._cond:
            if self._closed:
                raise ServerError("server shut down")
            if self._in_cooldown():
                raise ServerError("server recently failed; using whisper-cli")
            self._failed_at = None
            self._last_activity = started
            while True:
                proc = self._proc
                if self._closed:
                    raise ServerError("server shut down")
                if (proc is not None and proc.model == model_path
                        and self._ready and proc.alive()):
                    break
                if self._failed_at is not None:
                    raise ServerError("server failed to load")
                # an unload since we began leaves no loader: make sure one runs
                self._ensure_loading_locked(model_path)
                left = deadline - self._clock()
                if left <= 0:
                    # This dictation goes through whisper-cli, but the load
                    # carries on under its own, longer limit (ready_timeout),
                    # so the next one finds the model loaded. Stopping it here
                    # meant a model slower to load than this wait never got
                    # warm: every release killed it and started a cooldown.
                    raise ServerError("server still loading; using whisper-cli")
                self._cond.wait(min(left, 0.25))
            proc.inflight += 1
            port = proc.port
        waited = (self._clock() - started) * 1000
        try:
            wav = Path(wav_path).read_bytes()
            # The same decode settings whisper-cli runs with.
            # token_timestamps must be off: this server build turns them on
            # unless told otherwise, which wraps every segment at 60
            # characters, mid-word ("Dash" / "board").
            fields = {"response_format": "json", "language": "en",
                      "beam_size": "5", "best_of": "5", "temperature": "0.0",
                      "token_timestamps": "false"}
            if prompt:
                fields["prompt"] = prompt
            # Bounded tighter than whisper-cli's two minutes: a hung server
            # holds the queue for this long before the cli fallback runs.
            text = self._infer(port, wav, fields, 30 + 2 * len(wav) / 32000)
        except ServerError as error:
            self._request_failed(proc, error)
            raise
        except OSError as error:
            raise ServerError(f"cannot read clip: {error}") from error
        finally:
            self._request_done(proc)
        return text, {"model_wait": waited}

    def unload(self) -> None:
        """Free the server's memory. A request in flight finishes first; the
        server is then stopped by whoever finishes it. Safe to call any time."""
        with self._cond:
            proc = self._detach_locked()
        self._stop_or_retire(proc)

    def forget_model(self, model_path) -> None:
        """A model file is being deleted or replaced: drop it if loaded."""
        model_path = str(model_path)
        with self._cond:
            hit = ((self._proc is not None and self._proc.model == model_path)
                   or model_path in self._loading)
        if hit:
            self.unload()

    def shutdown(self) -> None:
        """Quit: stop the server now, in-flight request or not."""
        with self._cond:
            self._closed = True
            proc = self._detach_locked()
            self._cond.notify_all()
        if proc is not None:
            proc.stop()

    # -- internals ------------------------------------------------------

    def _in_cooldown(self) -> bool:
        return (self._failed_at is not None
                and self._clock() - self._failed_at < self._cooldown)

    def _detach_locked(self):
        """Forget the current server and invalidate its loader. Returns the
        process to stop, or None. Never starts a cooldown."""
        proc, self._proc = self._proc, None
        self._ready = False
        self._gen += 1
        self._cond.notify_all()
        return proc

    def _stop_or_retire(self, proc) -> None:
        if proc is None:
            return
        with self._cond:
            if proc.inflight:
                proc.retired = True   # the last request out stops it
                return
        proc.stop()

    def _request_done(self, proc) -> None:
        with self._cond:
            proc.inflight -= 1
            self._last_activity = self._clock()
            stop = proc.retired and proc.inflight == 0
        if stop:
            proc.stop()

    def _request_failed(self, proc, error) -> None:
        """A request to `proc` failed. If it is still the current server that
        is a real failure (cooldown, stop it). If it was unloaded meanwhile,
        the failure is ours: the caller falls back without a cooldown."""
        with self._cond:
            if self._proc is not proc:
                return
            self._failed_at = self._clock()
            detached = self._detach_locked()
        _log(f"request failed ({error}); using whisper-cli for "
             f"{self._cooldown:.0f} s")
        if detached is not None:
            detached.stop()

    def _ensure_loading_locked(self, model: str) -> None:
        """Make sure `model` is loaded or has a live loader. Lock held."""
        proc = self._proc
        if proc is not None and proc.model == model and proc.alive():
            return
        if self._loading.get(model) == self._gen:
            return   # a loader of the current generation is on it
        if any(m != model and g == self._gen for m, g in self._loading.items()):
            self._gen += 1   # a loader for another model: make it stale
        if proc is not None:   # another model, or a server that died
            old = self._detach_locked()
            threading.Thread(target=self._stop_or_retire, args=(old,),
                             daemon=True).start()
        self._loading[model] = self._gen
        threading.Thread(target=self._load, args=(model, self._gen),
                         name="whisper-server-load", daemon=True).start()

    def _load(self, model_path: str, gen: int) -> None:
        try:
            self._load_one(model_path, gen)
        finally:
            with self._cond:
                if self._loading.get(model_path) == gen:
                    del self._loading[model_path]
                self._cond.notify_all()

    def _current(self, gen) -> bool:
        return not self._closed and self._gen == gen

    def _load_one(self, model_path: str, gen: int) -> None:
        binary = self._find("whisper-server")
        if not binary:
            self._load_failed("whisper-server.exe not found", gen)
            return
        try:
            port = self._port_fn()
            command = [binary, "-m", model_path, "--host", "127.0.0.1",
                       "--port", str(port), "-l", "en", "--beam-size", "5"]
            popen = self._spawn(command)
        except (OSError, ServerError) as error:
            self._load_failed(f"cannot start it: {error}", gen)
            return
        proc = _Process(popen, port, model_path)
        with self._cond:
            stale = not self._current(gen)
            if not stale:
                self._proc, self._ready = proc, False
        if stale:
            proc.stop()
            return
        loading_from = self._clock()
        deadline = loading_from + self._ready_timeout
        while True:
            with self._cond:
                if self._proc is not proc:   # unloaded or replaced meanwhile
                    return
            if not proc.alive():
                self._load_failed("it exited while loading "
                                  f"({_describe_exit(proc.exit_code())})", gen, proc)
                return
            if self._probe(port, 1.0):
                break
            if self._clock() >= deadline:
                self._load_failed("the model load timed out", gen, proc)
                return
            time.sleep(0.05)
        with self._cond:
            if self._proc is not proc:
                return
            self._ready = True
            self._failed_at = None
            self._last_activity = self._clock()
            self._cond.notify_all()
        self._ensure_watcher()

    def _load_failed(self, why: str, gen: int, proc=None) -> None:
        """A load failed. Only the current generation's failure counts; a
        loader that was unloaded underneath has nothing to report."""
        with self._cond:
            current = self._current(gen)
            if current:
                self._failed_at = self._clock()
                if proc is None or self._proc is proc:
                    self._detach_locked()
            self._cond.notify_all()
        if current:
            _log(f"unavailable ({why}); using whisper-cli")
        if proc is not None:
            proc.stop()

    def _ensure_watcher(self) -> None:
        with self._cond:
            if self._watcher is not None and self._watcher.is_alive():
                return
            self._watcher = threading.Thread(
                target=self._watch, name="whisper-server-idle", daemon=True)
            self._watcher.start()

    def _watch(self) -> None:
        """Handy's idle watcher: unload once idle longer than the limit, and
        never while a recording or transcription is in flight. The decision
        and the detach happen under one lock hold, so a request or activity
        that arrives meanwhile is never cut off."""
        while True:
            with self._cond:
                if self._closed or self._proc is None:
                    self._watcher = None
                    return
                self._cond.wait(self._poll)
                if self._closed or self._proc is None:
                    self._watcher = None
                    return
                proc = self._proc
                if self._busy or proc.inflight:
                    self._last_activity = self._clock()
                    continue
                limit = unload_seconds(self._unload)
                due = (not proc.alive()) or (
                    limit is not None
                    and self._clock() - self._last_activity > limit)
                if not due:
                    continue
                self._detach_locked()
            proc.stop()
