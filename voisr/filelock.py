"""Windows advisory file lock, exclusive across processes.

Uses ``msvcrt.locking`` with ``LK_NBLCK`` (non-blocking exclusive lock)
retried in a sleep loop until acquired or ``timeout`` elapses. Chosen over
``win32file.LockFileEx`` because it is stdlib-only — no pywin32 dependency
for a single byte-range lock — and ``msvcrt.locking`` calls the same
underlying ``LockFileEx`` API, so the exclusion semantics are identical.

The lock is a byte-range lock on byte 0 of a sidecar file. Windows byte-range
locks are enforced per open file HANDLE, not per process, so two handles in
the *same* process (e.g. two threads, each opening their own handle) are
mutually exclusive too — which is why reentrancy has to be handled
explicitly: a second ``FileLock`` in the *same thread* tracks depth instead
of opening a second handle and deadlocking against itself.
"""

import msvcrt
import threading
import time
from pathlib import Path

_LOCK_BYTE_COUNT = 1
_POLL_INTERVAL_S = 0.05

_local = threading.local()


class LockTimeout(Exception):
    """Raised when a lock could not be acquired within ``timeout``."""


class FileLock:
    """Context manager for an exclusive, cross-process advisory lock.

    ``with FileLock(path):`` blocks until the lock is acquired.
    ``with FileLock(path, timeout=5):`` raises ``LockTimeout`` after 5s.
    Reentrant within a single thread; two threads (or two processes) still
    exclude each other.
    """

    def __init__(self, path, timeout: float | None = None):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None
        self._key = str(self.path)
        self._reentrant = False

    def __enter__(self) -> "FileLock":
        state = getattr(_local, "locks", None)
        if state is None:
            state = {}
            _local.locks = state

        entry = state.get(self._key)
        if entry is not None:
            fh, depth = entry
            state[self._key] = (fh, depth + 1)
            self._reentrant = True
            return self

        fh = open(self.path, "a+b")
        try:
            fh.seek(0, 2)
            if fh.tell() == 0:
                # LockFileEx requires a byte to exist at the locked region.
                fh.write(b"\0")
                fh.flush()

            deadline = None if self.timeout is None else time.monotonic() + self.timeout
            while True:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTE_COUNT)
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise LockTimeout(f"timed out waiting for lock: {self.path}")
                    time.sleep(_POLL_INTERVAL_S)
        except BaseException:
            fh.close()
            raise

        self._fh = fh
        self._reentrant = False
        state[self._key] = (fh, 1)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        state = _local.locks
        fh, depth = state[self._key]
        if depth > 1:
            state[self._key] = (fh, depth - 1)
            return
        del state[self._key]
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTE_COUNT)
        finally:
            fh.close()
