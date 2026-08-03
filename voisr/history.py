"""Append-only JSONL store of dictations with derived usage stats.

Ported from the macOS `voisr/history.py`. The public interface, record
shape, and every durability guarantee are unchanged; only the mechanism
differs. macOS used `fcntl.flock` plus a POSIX dir_fd/O_NOFOLLOW dance to
defend against symlink races and to make directory-mode/fork edge cases
safe. None of that exists on Windows:

- Cross-process exclusion comes from `voisr.filelock.FileLock`
  (`msvcrt.locking`) instead of `fcntl.flock`.
- `%LOCALAPPDATA%` is already per-user ACL'd, so there is no `0600`/`0700`
  chmod dance to port.
- There is no `fork()` on Windows, so the fork-safety machinery
  (`register_at_fork`, tracked lock fds, epoch bookkeeping) is deleted
  outright rather than ported.
- `os.fsync` cannot target a directory handle on Windows, so durability
  relies on fsync-ing the temp file before `os.replace`, which is atomic
  on Windows (`MoveFileEx` + `MOVEFILE_REPLACE_EXISTING`).
- Windows lets an external scanner (Defender, the search indexer) briefly
  lock a file that was just written, which has no macOS equivalent. Both
  the read and the commit wait that out via `_retry_while_locked`.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

from voisr.filelock import FileLock


# Windows Defender and the search indexer can hold a history file open for a
# moment right after it is written, surfacing as PermissionError/WinError 5 on
# both the read and the commit. It is external and momentary, not corruption,
# so both paths wait it out on this one shared budget (~200ms) rather than
# drifting apart -- an unretried commit loses the dictation outright.
_LOCK_RETRIES = 20
_LOCK_RETRY_DELAY_S = 0.01


def _retry_while_locked(operation):
    """Return `operation()`, retrying while it raises PermissionError.

    Re-raises the last PermissionError once the budget is spent: a lock that
    outlasts it is no longer plausibly transient, and failing loudly beats
    giving up silently. Only PermissionError is retried -- a full disk or a
    bad path will not clear by waiting and must surface at once.
    """
    last_error = None
    for _ in range(_LOCK_RETRIES):
        try:
            return operation()
        except PermissionError as error:
            last_error = error
            time.sleep(_LOCK_RETRY_DELAY_S)
    raise last_error


def _finite_number(value) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if not isinstance(value, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _finite_timestamp_number(value) -> bool:
    if not _finite_number(value):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _valid_record(item) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and bool(item["id"])
        and _finite_timestamp_number(item.get("ts"))
        and isinstance(item.get("text"), str)
        and isinstance(item.get("words"), int)
        and not isinstance(item["words"], bool)
        and item["words"] >= 0
        and _finite_number(item.get("duration_s"))
        and item["duration_s"] >= 0
    )


class HistoryStore:
    """Append-only JSONL store of dictations with derived usage stats."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def close(self) -> None:
        """No-op: the store holds no open handles between calls."""

    def _mutation_lock(self) -> FileLock:
        return FileLock(self.lock_path)

    def _read(self) -> list[dict]:
        # A file that was just replaced (os.replace onto self.path) can be
        # transiently locked — see _retry_while_locked. An absent history is
        # not that condition: it is a legitimately empty history, so
        # FileNotFoundError answers on the first attempt instead of waiting
        # out a budget that can never help.
        try:
            contents = _retry_while_locked(
                lambda: self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        items = []
        for line in contents.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _valid_record(item):
                items.append(item)
        return items

    def _write_atomic(self, items: list[dict]) -> None:
        payload = "".join(json.dumps(item) + "\n" for item in items).encode("utf-8")
        directory = self.path.parent
        temp_path = None
        fd = -1
        try:
            while True:
                candidate = directory / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
                try:
                    fd = os.open(str(candidate),
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    temp_path = candidate
                    break
                except FileExistsError:
                    continue
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
            os.close(fd)
            fd = -1
            # The commit hits the same transient lock the read path waits out
            # (measured ~1 write in 5 on Windows). Unretried it loses the
            # dictation outright, and history is the only copy when pasting is
            # blocked. os.replace itself stays atomic either way, so a retry
            # can never publish a half-written file.
            _retry_while_locked(lambda: os.replace(temp_path, self.path))
            temp_path = None
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def _mutate(self, change):
        with self._mutation_lock():
            items = self._read()
            result = change(items)
            self._write_atomic(items)
            return result

    def add(self, text: str, duration_s: float) -> dict:
        if not _finite_number(duration_s) or duration_s < 0:
            raise ValueError("duration_s must be a finite, nonnegative number")
        item = {"id": uuid.uuid4().hex, "ts": time.time(),
                "text": text, "words": len(text.split()), "duration_s": duration_s}

        def append(items):
            items.append(item)
            return item

        return self._mutate(append)

    def list(self) -> list[dict]:
        return sorted(self._read(), key=lambda i: i["ts"], reverse=True)

    def delete(self, item_id: str) -> None:
        def remove(items):
            items[:] = [item for item in items if item["id"] != item_id]

        self._mutate(remove)

    def restore(self, item: dict) -> dict:
        """Re-insert a previously deleted record, keeping its id and ts.
        Used by the dashboard's undo-delete. Idempotent."""
        def put_back(items):
            if any(i["id"] == item["id"] for i in items):
                return item
            items.append(dict(item))
            return item

        return self._mutate(put_back)

    def clear(self) -> int:
        """Remove every record in a single atomic write. Deleting row by row
        would rewrite the whole file once per row."""
        def wipe(items):
            count = len(items)
            items[:] = []
            return count

        return self._mutate(wipe)

    def _daily_series(self, items, days: int = 14) -> list[dict]:
        """Word counts per day for the last `days` days, oldest first,
        zero-filled. Entries outside the window are ignored."""
        today = date.today()
        window = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]
        counts = {d: 0 for d in window}
        for item in items:
            try:
                d = date.fromtimestamp(item["ts"])
            except (OverflowError, OSError, ValueError):
                continue
            if d in counts:
                counts[d] += item["words"]
        return [{"date": d.isoformat(), "words": counts[d]} for d in window]

    def stats(self) -> dict:
        items = self._read()
        if not items:
            return {"total_words": 0, "dictations": 0, "avg_wpm": 0,
                     "streak_days": 0, "total_seconds": 0,
                     "time_saved_seconds": 0,
                     "daily": self._daily_series([])}
        total_words = sum(i["words"] for i in items)
        total_time = sum((Fraction(i["duration_s"]) for i in items), Fraction())
        # typing baseline: 40 words per minute
        typed_time = Fraction(total_words * 60, 40)
        saved = typed_time - total_time
        if saved < 0:
            saved = Fraction(0)
        total_seconds = total_time
        if not total_time:
            total_time = Fraction(1, 1_000_000_000)
        days = set()
        for item in items:
            try:
                days.add(date.fromtimestamp(item["ts"]))
            except (OverflowError, OSError, ValueError):
                continue
        streak, d = 0, date.today()
        while d in days:
            streak += 1
            d -= timedelta(days=1)
        return {"total_words": total_words, "dictations": len(items),
                "avg_wpm": round(Fraction(total_words * 60, 1) / total_time),
                "streak_days": streak,
                "total_seconds": round(total_seconds),
                "time_saved_seconds": round(saved),
                "daily": self._daily_series(items)}
