import atexit
import math
import operator
import threading
import time
import wave

# The mic's own audio thread stops its stream: the callback raises
# CallbackAbort once a stop is asked for, and PortAudio's host thread (MME or
# WASAPI) stops the device itself and reports it through finished_callback.
# The stop()/close() that follow then find a stream that is already stopped.
# On the Mac this avoids a CoreAudio deadlock between the stopping thread and
# the IO thread; Windows has no such lock order, but a stop from outside can
# still hang in a driver (waveInReset on a dropped Bluetooth or USB device),
# and the self-stop keeps that call off the common path.
#
# A stop that still sticks for STOP_TIMEOUT_S can't be interrupted, so the
# stream is abandoned and new recordings are refused: opening the same device
# again could hang the same way, and there is no fallback recorder on Windows.
# If the stuck close finishes later, the stream is closed after all and
# recording works again; otherwise only a restart clears it.
_mic_wedged = threading.Event()
_abandoned_streams = []  # kept referenced: collecting one could close it
_wedge_lock = threading.Lock()

WEDGED_NOTICE = ("The microphone got stuck. Your dictation is safe. "
                 "Quit and reopen Voisr to fix it.")
WEDGED_START_ERROR = "the microphone got stuck — quit and reopen Voisr to fix it"


def _log(message: str) -> None:
    """voisr.log, as everything else: print() is silent in the windowed
    build. Never raises."""
    try:
        from voisr.updater import _log as log
        log(f"audio: {message}")
    except Exception:
        pass


def _notify_wedged() -> None:
    """Best effort. The next press shows WEDGED_START_ERROR as well."""
    _log(WEDGED_NOTICE)


def _abandon_stream(stream) -> None:
    with _wedge_lock:
        _abandoned_streams.append(stream)
        first = not _mic_wedged.is_set()
        _mic_wedged.set()
    if not first:
        return
    try:
        # sounddevice's exit handler terminates PortAudio, which closes every
        # open stream, this one included: Quit would hang on the same stop
        import sounddevice as sd
        atexit.unregister(sd._exit_handler)
    except Exception:
        pass
    _notify_wedged()


def _abandoned_stream_closed(stream) -> None:
    """A stuck close finished after all: once none is left, the mic is free."""
    with _wedge_lock:
        try:
            _abandoned_streams.remove(stream)
        except ValueError:
            return
        if _abandoned_streams:
            return
        _mic_wedged.clear()
    _log("the stuck mic stop finished; recording works again")

# Speaking voice fills the meter: a quiet room sits under the floor, a raised
# voice reaches the ceiling. Linear in decibels between the two.
LEVEL_FLOOR_DBFS = -55.0
LEVEL_CEIL_DBFS = -12.0


def level_from_int16(block) -> float:
    """Loudness of one mono int16 block as 0..1 (floor -> 0, ceiling -> 1).

    Plain Python, so it needs no numpy: about 0.02 ms for a 512-sample
    block, small next to the block's 32 ms of audio."""
    samples = memoryview(block).cast("B").cast("h")
    count = len(samples)
    if not count:
        return 0.0
    mean_square = sum(map(operator.mul, samples, samples)) / count
    if mean_square <= 0:
        return 0.0
    dbfs = 10 * math.log10(mean_square / (32768.0 * 32768.0))
    level = (dbfs - LEVEL_FLOOR_DBFS) / (LEVEL_CEIL_DBFS - LEVEL_FLOOR_DBFS)
    return min(1.0, max(0.0, level))


def _default_stream_factory(samplerate, callback, finished_callback):
    import sounddevice as sd
    return sd.RawInputStream(samplerate=samplerate, channels=1, dtype="int16",
                             callback=callback,
                             finished_callback=finished_callback)


class StreamRecorder:
    """Push-to-talk recorder that opens the mic ONLY while recording.

    Opening a sounddevice stream costs ~80 ms — fast enough that capture
    starts effectively at the key-press (unlike ffmpeg's 0.6-0.9 s cold
    start) — while closing it on stop() means macOS's "microphone in use"
    indicator is off whenever you are not dictating, like Wispr.
    Same start(path)/stop() interface as voisr.recorder.Recorder.
    """

    SAMPLERATE = 16000
    MAX_SECONDS = 600  # bounds memory (~19 MB) and stops a stuck-key recording

    # A device still warming up sends exact zeros. Six seconds of nothing
    # else means the stream is wedged: reopen it once (spec item 5).
    ZERO_REOPEN_S = 6.0

    # How long to wait for a stopping stream before abandoning it. A healthy
    # stop takes about one audio block; this only ever catches a hung driver.
    STOP_TIMEOUT_S = 3.0
    # How long the audio thread gets to stop its own stream. A healthy one
    # does it within a block (MME's are the longest, about 50 ms at the
    # default latency); this much covers a stream still starting up. After
    # it, a device sending nothing is stopped from outside.
    SELF_STOP_WAIT_S = 0.5
    # PortAudio reports the stop from its host thread just before that thread
    # ends: let it finish before stop() and close() touch the stream.
    SELF_STOP_UNWIND_S = 0.02
    # A stop row goes to voisr.log only when it was out of the ordinary: from
    # outside, failed, slower than this, or finished after being abandoned.
    # Routine stops would add a log line to every dictation.
    SLOW_STOP_LOG_MS = 500

    def __init__(self, stream_factory=None, timer_factory=threading.Timer):
        self._lock = threading.Lock()
        self._frames = []
        self._recording = False
        self._out = None
        self._stream = None
        self._bytes = 0
        self._max_bytes = self.SAMPLERATE * 2 * self.MAX_SECONDS
        self._level = 0.0
        self._stream_factory = stream_factory or _default_stream_factory
        self._timer_factory = timer_factory
        # Held while the stream is opened, swapped or closed: the watchdog's
        # reopen runs on a timer thread and must never race stop().
        self._stream_lock = threading.Lock()
        self._generation = 0
        self._heard = False  # a non-zero sample arrived this recording
        self._reopened = False
        self._watchdog = None
        self._stop_requested = False
        self._stream_finished = threading.Event()

    @property
    def reopened(self) -> bool:
        """True once this recording's silent stream has been reopened."""
        return self._reopened

    @property
    def level(self) -> float:
        """Loudness of the latest captured block, 0..1; 0.0 when not
        recording. A plain float swap, so any thread may read it."""
        return self._level

    def _on_audio(self, indata, frames, time_info, status):
        if self._stop_requested:
            import sounddevice as sd
            raise sd.CallbackAbort
        if not self._recording:
            return
        chunk = bytes(indata)
        with self._lock:
            keep = self._bytes < self._max_bytes
            if keep:
                self._frames.append(chunk)
                self._bytes += len(chunk)
        if keep:
            if not self._heard and chunk.strip(b"\x00"):
                self._heard = True
            # Audio first, meter second: the meter is cosmetic, and an
            # exception escaping this callback would stop the stream.
            try:
                self._level = level_from_int16(chunk)
            except Exception:
                pass
            return
        # cap reached: stop capturing and release the mic (indicator off);
        # stop() still writes everything buffered so far
        import sounddevice as sd
        raise sd.CallbackStop

    def prime(self) -> None:
        """No-op: the mic is opened per-recording, never held open.

        (Kept for interface parity; the permission prompt now appears on
        the first real recording, and the mic indicator only shows then.)"""
        return

    def start(self, out_path) -> None:
        if self._recording:
            raise RuntimeError("already recording")
        if _mic_wedged.is_set():
            raise RuntimeError(WEDGED_START_ERROR)
        with self._lock:
            self._frames = []
            self._bytes = 0
        self._out = str(out_path)
        self._level = 0.0
        self._heard = False
        self._reopened = False
        self._generation += 1
        generation = self._generation
        self._recording = True
        # Open the mic only now — the indicator turns on here and off in stop()
        with self._stream_lock:
            self._stream = self._open_stream()
        watchdog = self._timer_factory(self.ZERO_REOPEN_S, self._reopen_if_silent,
                                       args=(generation,))
        watchdog.daemon = True
        self._watchdog = watchdog
        watchdog.start()

    def _open_stream(self):
        self._stop_requested = False
        finished = self._stream_finished = threading.Event()
        stream = self._stream_factory(self.SAMPLERATE, self._on_audio, finished.set)
        try:
            stream.start()
        except Exception:
            try:
                stream.close()
            except Exception:
                pass
            raise
        return stream

    def _reopen_if_silent(self, generation) -> None:
        """Timer thread: the device sent only exact zeros for ZERO_REOPEN_S.
        Reopen it once; if it stays silent, the speech gate says so on
        release. Never after stop()/cancel(), and never for an older
        recording: either would light the mic indicator for nobody."""
        with self._stream_lock:
            if (not self._recording or generation != self._generation
                    or self._heard or self._reopened):
                return
            self._reopened = True
            try:
                self._close_stream()
            except Exception:
                pass
            if _mic_wedged.is_set():
                return
            try:
                self._stream = self._open_stream()
            except Exception as error:
                _log(f"reopening the silent mic failed: {error}")
                return
        _log("the mic sent only silence for "
             f"{self.ZERO_REOPEN_S:.0f} s — reopened it once")

    def _stop_watchdog(self) -> None:
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.cancel()

    def stop(self) -> str:
        """Stop, close the mic, and write the WAV; returns '' (parity)."""
        if not self._recording:
            raise RuntimeError("not recording")
        self._recording = False
        self._stop_watchdog()
        with self._stream_lock:
            self._close_stream()
        # after the close: a callback already in flight can no longer write
        self._level = 0.0
        with self._lock:
            data = b"".join(self._frames)
            self._frames = []
            self._bytes = 0
        out, self._out = self._out, None
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.SAMPLERATE)
            wav.writeframes(data)
        return ""

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        began = time.monotonic()
        self._stop_requested = True
        stopped_itself = self._stream_finished.wait(self.SELF_STOP_WAIT_S)
        if stopped_itself:
            time.sleep(self.SELF_STOP_UNWIND_S)
        errors = []
        abandoned = threading.Event()

        def stop_and_close():
            try:
                try:
                    stream.stop()
                finally:
                    stream.close()
            except Exception as error:
                errors.append(error)
            # Timing only, never audio or text. Also runs after the stream
            # was abandoned: a stuck stop that logs nothing never finished,
            # and one that logs a long time was only slow.
            elapsed = (time.monotonic() - began) * 1000
            how = "" if stopped_itself else ", from outside"
            if errors:
                how += f", failed: {type(errors[0]).__name__}"
            if how or abandoned.is_set() or elapsed >= self.SLOW_STOP_LOG_MS:
                _log(f"mic stop {elapsed:.0f} ms{how}")
            if abandoned.is_set():
                _abandoned_stream_closed(stream)

        closer = threading.Thread(target=stop_and_close,
                                  name="voisr-mic-close", daemon=True)
        closer.start()
        closer.join(self.STOP_TIMEOUT_S)
        if closer.is_alive():
            # Set before the stream is listed, so a close that ends right
            # now still takes it off the list and clears the wedge.
            abandoned.set()
            _abandon_stream(stream)
            if not closer.is_alive():
                _abandoned_stream_closed(stream)
            return
        if errors:
            raise errors[0]

    def cancel(self) -> None:
        """Close the mic and discard buffered audio; safe to repeat."""
        self._recording = False
        self._stop_watchdog()
        try:
            with self._stream_lock:
                self._close_stream()
        except Exception:
            pass
        self._level = 0.0  # after the close, as in stop()
        with self._lock:
            self._frames = []
            self._bytes = 0
        self._out = None

    def close(self) -> None:
        self.cancel()
