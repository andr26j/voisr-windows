"""Ported from the macOS `tests/test_history.py` (56 tests). See the
Task 2 triage report in the Phase 2 plan for the keep/rewrite/delete
breakdown of every test. In short:

- Pure-logic and thread-concurrency tests port unchanged.
- Tests that monkeypatched `fcntl.flock` are rewritten against
  `voisr.filelock`'s `msvcrt.locking` equivalent.
- Tests exercising `os.fork`, POSIX permission bits (0600/0700, umask),
  symlink/dir_fd races, or mac-internal fd-caching machinery are deleted:
  none of that exists in the Windows port (see module docstring in
  voisr/history.py).
"""
import json
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

import voisr.filelock as filelock_module
import voisr.history as history_module
from voisr.history import HistoryStore

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_WORKER = str(Path(__file__).resolve().parent / "_history_worker.py")
_PYTHON = sys.executable


def _spawn_worker(path, prefix, count):
    return subprocess.Popen([_PYTHON, _WORKER, REPO_ROOT, str(path), prefix, str(count)])


def test_add_and_list(tmp_path):
    h = HistoryStore(tmp_path / "h.jsonl")
    h.add("Hello world today", duration_s=2.0)
    items = h.list()
    assert len(items) == 1
    assert items[0]["text"] == "Hello world today"
    assert items[0]["words"] == 3
    assert "id" in items[0] and "ts" in items[0]


def test_list_newest_first(tmp_path):
    h = HistoryStore(tmp_path / "h.jsonl")
    h.add("first", 1.0)
    h.add("second", 1.0)
    assert h.list()[0]["text"] == "second"


def test_delete(tmp_path):
    h = HistoryStore(tmp_path / "h.jsonl")
    h.add("keep", 1.0)
    victim = h.add("remove", 1.0)["id"]
    h.delete(victim)
    assert [i["text"] for i in h.list()] == ["keep"]


def test_stats(tmp_path):
    h = HistoryStore(tmp_path / "h.jsonl")
    h.add("one two three four five six", duration_s=3.0)  # 6 words in 3s = 120 wpm
    s = h.stats()
    assert s["total_words"] == 6
    assert s["dictations"] == 1
    assert s["avg_wpm"] == 120
    assert s["streak_days"] == 1


def test_corrupt_line_is_skipped(tmp_path):
    h = HistoryStore(tmp_path / "h.jsonl")
    h.add("good", 1.0)
    with open(h.path, "a") as f:
        f.write('{"torn line\n')  # crash mid-write
    h.add("after", 1.0)
    assert [i["text"] for i in h.list()] == ["after", "good"]


def test_wrong_shape_legacy_values_are_ignored_and_valid_records_round_trip(tmp_path):
    path = tmp_path / "h.jsonl"
    oldest = {
        "id": "oldest-id",
        "ts": 1_700_000_000.0,
        "text": "oldest dictation",
        "words": 2,
        "duration_s": 1.5,
    }
    legacy = {
        "id": "legacy-id",
        "ts": 1_700_000_001.0,
        "text": "legacy dictation",
        "words": 2,
        "duration_s": 1.5,
    }
    invalid_values = [
        [],
        None,
        "not a record",
        {"id": "missing-fields"},
        {"id": "bad-ts", "ts": "now", "text": "text", "words": 1,
         "duration_s": 1.0},
        {"id": "bad-words", "ts": 1.0, "text": "text", "words": True,
         "duration_s": 1.0},
        {"id": "bad-duration", "ts": 1.0, "text": "text", "words": 1,
         "duration_s": -1.0},
    ]
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in [oldest, *invalid_values, legacy]),
        encoding="utf-8",
    )
    store = HistoryStore(path)

    assert [item["id"] for item in store.list()] == ["legacy-id", "oldest-id"]
    assert store.stats()["total_words"] == 4
    store.delete("not-present")
    added = store.add("new dictation", 1.0)
    store.delete(added["id"])

    assert path.read_text(encoding="utf-8") == (
        json.dumps(oldest) + "\n" + json.dumps(legacy) + "\n"
    )


def test_replace_failure_preserves_history_bytes_and_removes_temp_file(tmp_path,
                                                                         monkeypatch):
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    store.add("committed", 1.0)
    before = path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(history_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.add("not committed", 1.0)

    assert path.read_bytes() == before
    assert list(tmp_path.glob(".h.jsonl.*.tmp")) == []


class _LockedThenReadable:
    """Stands in for HistoryStore.path: raises the transient Windows lock a
    set number of times, then reads for real."""

    def __init__(self, real, failures):
        self._real = real
        self.remaining = failures
        self.attempts = 0
        self.name = real.name

    def read_text(self, encoding=None):
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise PermissionError(5, "Access is denied")
        return self._real.read_text(encoding=encoding)


def test_read_retries_a_transiently_locked_file(tmp_path):
    """Characterization guard for the retry _read has always had, so folding
    it onto one shared budget with the write path cannot quietly change it."""
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    store.add("committed", 1.0)
    store.path = _LockedThenReadable(path, failures=3)

    assert [i["text"] for i in store.list()] == ["committed"]
    assert store.path.attempts == 4


def test_read_gives_up_on_a_permanently_locked_file(tmp_path, monkeypatch):
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    store.add("committed", 1.0)
    store.path = _LockedThenReadable(path, failures=10**6)
    sleeps = []
    monkeypatch.setattr(history_module.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError):
        store.list()

    assert store.path.attempts == 20
    assert set(sleeps) == {0.01}


def test_read_of_a_missing_file_is_empty_and_not_retried(tmp_path, monkeypatch):
    """FileNotFoundError is not the transient lock -- an absent history is a
    legitimate empty history and must answer on the first attempt."""
    store = HistoryStore(tmp_path / "h.jsonl")
    sleeps = []
    monkeypatch.setattr(history_module.time, "sleep", sleeps.append)

    assert store.list() == []
    assert sleeps == []


def test_replace_retries_a_transiently_locked_destination(tmp_path, monkeypatch):
    """Windows Defender and the search indexer briefly hold a file that was
    just written, which surfaces as PermissionError/WinError 5 from
    os.replace. That is the identical external, momentary condition _read
    already retries -- but on the write side it loses the dictation, and
    history is the only copy when pasting is blocked."""
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    real_replace = history_module.os.replace
    attempts = 0

    def locked_twice(src, dst, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(history_module.os, "replace", locked_twice)

    store.add("survives a locked destination", 1.0)

    assert attempts == 3
    assert [i["text"] for i in store.list()] == ["survives a locked destination"]
    assert list(tmp_path.glob(".h.jsonl.*.tmp")) == []


def test_replace_retry_budget_matches_the_read_path(tmp_path, monkeypatch):
    """A destination locked for longer than the budget must still fail loudly
    and leave the committed history byte-identical -- a retry that gave up
    silently would be worse than the crash it replaced."""
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    store.add("committed", 1.0)
    before = path.read_bytes()
    attempts = 0
    sleeps = []

    def always_locked(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(history_module.os, "replace", always_locked)
    monkeypatch.setattr(history_module.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError):
        store.add("not committed", 1.0)

    assert attempts == 20  # same budget as _read's retry loop
    assert sleeps and set(sleeps) == {0.01}  # backs off, never spins
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".h.jsonl.*.tmp")) == []


def test_a_non_permission_replace_failure_is_not_retried(tmp_path, monkeypatch):
    """Only the transient lock is retried. A genuine OSError (full disk, bad
    path) is not going to clear by waiting, so it must surface at once."""
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    attempts = 0

    def fail_replace(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError("replace failed")

    monkeypatch.setattr(history_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.add("not committed", 1.0)

    assert attempts == 1


def test_pre_replace_fsync_failure_preserves_history_bytes_and_removes_temp_file(
        tmp_path, monkeypatch):
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    store.add("committed", 1.0)
    before = path.read_bytes()
    original_fsync = history_module.os.fsync
    calls = 0

    def fail_first_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temp sync failed")
        original_fsync(fd)

    monkeypatch.setattr(history_module.os, "fsync", fail_first_fsync)

    with pytest.raises(OSError, match="temp sync failed"):
        store.add("not committed", 1.0)

    assert path.read_bytes() == before
    assert list(tmp_path.glob(".h.jsonl.*.tmp")) == []


def test_huge_integer_timestamp_is_ignored_without_bricking_history_operations(
        tmp_path):
    path = tmp_path / "h.jsonl"
    valid = {
        "id": "valid-id",
        "ts": 1_700_000_000.0,
        "text": "valid record",
        "words": 2,
        "duration_s": 1.0,
    }
    huge_timestamp = {**valid, "id": "huge-id", "ts": 10 ** 1000}
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in (valid, huge_timestamp)),
        encoding="utf-8",
    )
    store = HistoryStore(path)

    assert [item["id"] for item in store.list()] == ["valid-id"]
    assert store.stats()["dictations"] == 1
    store.delete("valid-id")
    added = store.add("after huge timestamp", 1.0)
    store.delete(added["id"])

    assert store.list() == []


def test_unrepresentable_timestamp_stays_in_history_but_not_streak_days(tmp_path):
    path = tmp_path / "h.jsonl"
    valid = {
        "id": "valid-id",
        "ts": 1_700_000_000.0,
        "text": "valid record",
        "words": 2,
        "duration_s": 1.0,
    }
    distant = {**valid, "id": "distant-id", "ts": 1e308,
               "text": "distant record", "words": 3}
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in (valid, distant)),
        encoding="utf-8",
    )
    store = HistoryStore(path)

    assert [item["id"] for item in store.list()] == ["distant-id", "valid-id"]
    stats = store.stats()
    assert stats["total_words"] == 5
    assert stats["dictations"] == 2
    assert stats["avg_wpm"] == 150
    assert stats["streak_days"] == 0
    assert "total_seconds" in stats
    assert "time_saved_seconds" in stats
    assert "daily" in stats
    store.delete("valid-id")
    added = store.add("after distant timestamp", 1.0)
    store.delete(added["id"])

    assert [item["id"] for item in store.list()] == ["distant-id"]


def test_stats_empty(tmp_path):
    s = HistoryStore(tmp_path / "h.jsonl").stats()
    assert s["total_words"] == 0
    assert s["dictations"] == 0
    assert s["avg_wpm"] == 0
    assert s["streak_days"] == 0
    assert s["total_seconds"] == 0
    assert s["time_saved_seconds"] == 0
    assert len(s["daily"]) == 14


def test_two_instances_in_threads_do_not_lose_additions(tmp_path):
    path = tmp_path / "h.jsonl"
    start = threading.Event()

    def add(text):
        start.wait()
        HistoryStore(path).add(text, 1.0)

    threads = [
        threading.Thread(target=add, args=(text,))
        for text in ("first", "second")
    ]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert {item["text"] for item in HistoryStore(path).list()} == {
        "first", "second"
    }


def test_independent_process_writers_keep_every_record(tmp_path):
    """Rewritten from macOS's fork-based `_add_many` helper: Windows has no
    fork(), so each writer is a real subprocess instead."""
    path = tmp_path / "h.jsonl"
    processes = [_spawn_worker(path, f"p{index}", 10) for index in range(8)]
    try:
        for process in processes:
            assert process.wait(timeout=30) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    items = HistoryStore(path).list()
    assert len(items) == 80
    assert len({item["text"] for item in items}) == 80
    assert all(isinstance(json.loads(line), dict)
               for line in path.read_text().splitlines())


def test_delete_waiting_on_another_instance_keeps_both_mutations(tmp_path, monkeypatch):
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    store.add("keep", 1.0)
    victim_id = store.add("remove", 1.0)["id"]
    adder = HistoryStore(path)
    deleter = HistoryStore(path)
    write_entered = threading.Event()
    release_write = threading.Event()
    delete_attempted = threading.Event()
    add_done = threading.Event()
    delete_done = threading.Event()
    errors = []
    original_write = adder._write_atomic
    original_lock = deleter._mutation_lock

    def paused_write(items):
        write_entered.set()
        assert release_write.wait(timeout=2)
        original_write(items)

    def signaling_lock():
        delete_attempted.set()
        return original_lock()

    monkeypatch.setattr(adder, "_write_atomic", paused_write)
    monkeypatch.setattr(deleter, "_mutation_lock", signaling_lock)

    def add_new():
        try:
            adder.add("new", 1.0)
        except BaseException as error:
            errors.append(error)
        finally:
            add_done.set()

    def delete_victim():
        try:
            deleter.delete(victim_id)
        except BaseException as error:
            errors.append(error)
        finally:
            delete_done.set()

    add_thread = threading.Thread(target=add_new)
    delete_thread = threading.Thread(target=delete_victim)
    add_thread.start()
    assert write_entered.wait(timeout=2)
    delete_thread.start()
    assert delete_attempted.wait(timeout=2)
    try:
        assert not delete_done.is_set()
    finally:
        release_write.set()

    for thread, done in ((add_thread, add_done), (delete_thread, delete_done)):
        assert done.wait(timeout=2)
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert not errors
    assert {item["text"] for item in store.list()} == {"keep", "new"}


def test_reader_sees_complete_snapshot_while_atomic_write_is_paused(tmp_path,
                                                                     monkeypatch):
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    store.add("old", 1.0)
    writer = HistoryStore(path)
    write_entered = threading.Event()
    release_write = threading.Event()
    write_done = threading.Event()
    errors = []
    original_write = writer._write_atomic

    def paused_write(items):
        write_entered.set()
        assert release_write.wait(timeout=2)
        original_write(items)

    monkeypatch.setattr(writer, "_write_atomic", paused_write)

    def add_new():
        try:
            writer.add("new", 1.0)
        except BaseException as error:
            errors.append(error)
        finally:
            write_done.set()

    thread = threading.Thread(target=add_new)
    thread.start()
    assert write_entered.wait(timeout=2)
    try:
        before_replace = [item["text"] for item in store.list()]
    finally:
        release_write.set()

    assert write_done.wait(timeout=2)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not errors
    after_replace = [item["text"] for item in store.list()]
    assert before_replace == ["old"]
    assert after_replace == ["new", "old"]


def test_nested_mutation_lock_reuses_one_lock_acquisition(tmp_path, monkeypatch):
    """Rewritten from macOS's test_nested_mutation_lock_reuses_one_sidecar_fd,
    which monkeypatched fcntl.flock/os.open to assert nesting
    `_mutation_lock()` opens only one fd. Windows equivalent: assert nesting
    only calls the real msvcrt exclusive-lock syscall once, proving
    voisr.filelock's reentrancy is used rather than a second, self-deadlocking
    acquisition."""
    store = HistoryStore(tmp_path / "h.jsonl")
    lock_calls = []
    original_locking = filelock_module.msvcrt.locking

    def counting_locking(fd, mode, nbytes):
        if mode == filelock_module.msvcrt.LK_NBLCK:
            lock_calls.append(fd)
        return original_locking(fd, mode, nbytes)

    monkeypatch.setattr(filelock_module.msvcrt, "locking", counting_locking)

    with store._mutation_lock():
        with store._mutation_lock():
            pass

    assert len(lock_calls) == 1


def test_mutation_lock_is_released_even_when_unlock_fails(tmp_path, monkeypatch):
    """Rewritten from macOS's test_mutation_lock_closes_fd_when_unlock_fails,
    which monkeypatched fcntl.flock(LOCK_UN) to fail and asserted the fd was
    still closed. Windows equivalent: fail msvcrt.locking's unlock call and
    assert a fresh acquisition afterwards does not hang -- proving the lock
    handle and its reentrancy bookkeeping are still released."""
    store = HistoryStore(tmp_path / "h.jsonl")
    original_locking = filelock_module.msvcrt.locking

    def fail_unlock(fd, mode, nbytes):
        if mode == filelock_module.msvcrt.LK_UNLCK:
            raise OSError("unlock failed")
        return original_locking(fd, mode, nbytes)

    monkeypatch.setattr(filelock_module.msvcrt, "locking", fail_unlock)

    with pytest.raises(OSError, match="unlock failed"):
        with store._mutation_lock():
            pass

    monkeypatch.undo()  # restore the real msvcrt.locking

    done = threading.Event()

    def reacquire():
        with store._mutation_lock():
            pass
        done.set()

    t = threading.Thread(target=reacquire)
    t.start()
    assert done.wait(timeout=2)
    t.join(timeout=2)
    assert not t.is_alive()


def test_spawned_store_does_not_serialize_parent_fd_or_lock(tmp_path):
    """A HistoryStore holds no open handles between calls, so it is
    trivially picklable -- unlike macOS, which needed custom
    __getstate__/__setstate__ to drop a cached parent dir_fd before a
    multiprocessing 'spawn' child re-pickles it."""
    import multiprocessing

    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_add_from_store, args=(store, "spawned"))
    try:
        process.start()
        process.join(timeout=30)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    assert [item["text"] for item in store.list()] == ["spawned"]


def _add_from_store(store, text):
    store.add(text, 1.0)


def test_invalid_add_duration_raises_without_changing_history_bytes(tmp_path):
    path = tmp_path / "h.jsonl"
    store = HistoryStore(path)
    store.add("committed", 1.0)
    before = path.read_bytes()

    for duration_s in (-1, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            store.add("not committed", duration_s)

    assert path.read_bytes() == before


def test_stats_handles_huge_words_and_subnormal_durations_exactly(tmp_path):
    words = 10 ** 4_000
    duration_s = float.fromhex("0x0.0000000000001p-1022")
    path = tmp_path / "h.jsonl"
    path.write_text(json.dumps({
        "id": "huge",
        "ts": 1.0,
        "text": "x",
        "words": words,
        "duration_s": duration_s,
    }) + "\n", encoding="utf-8")

    stats = HistoryStore(path).stats()

    assert stats["total_words"] == words
    assert stats["avg_wpm"] == round(Fraction(words * 60, 1) / Fraction(duration_s))


def test_literal_tilde_path_is_not_expanded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    supplied_path = Path("~") / "h.jsonl"

    store = HistoryStore(supplied_path)
    store.add("literal tilde", 1.0)

    assert store.path == supplied_path
    assert (tmp_path / "~" / "h.jsonl").exists()


def test_stats_reports_time_saved_against_forty_wpm(tmp_path):
    store = HistoryStore(tmp_path / "h.jsonl")
    # 80 words spoken in 30s. Typing 80 words at 40wpm would take 120s.
    store.add("word " * 80, duration_s=30.0)
    stats = store.stats()
    assert stats["total_seconds"] == 30
    assert stats["time_saved_seconds"] == 90


def test_stats_clamps_time_saved_at_zero(tmp_path):
    store = HistoryStore(tmp_path / "h.jsonl")
    # 4 words in 60s is far slower than typing: must report 0, never negative.
    store.add("one two three four", duration_s=60.0)
    assert store.stats()["time_saved_seconds"] == 0


def test_stats_empty_history_has_all_keys(tmp_path):
    stats = HistoryStore(tmp_path / "h.jsonl").stats()
    assert stats["total_seconds"] == 0
    assert stats["time_saved_seconds"] == 0
    assert len(stats["daily"]) == 14
    assert all(d["words"] == 0 for d in stats["daily"])


def test_stats_daily_is_fourteen_days_zero_filled_ending_today(tmp_path):
    store = HistoryStore(tmp_path / "h.jsonl")
    store.add("alpha beta", duration_s=1.0)
    daily = store.stats()["daily"]
    assert len(daily) == 14
    assert daily[-1]["date"] == date.today().isoformat()
    assert daily[-1]["words"] == 2
    assert daily[0]["date"] == (date.today() - timedelta(days=13)).isoformat()
    assert daily[0]["words"] == 0


def test_stats_daily_ignores_entries_older_than_the_window(tmp_path):
    store = HistoryStore(tmp_path / "h.jsonl")
    item = store.add("old words here", duration_s=1.0)

    def age(items):
        items[0]["ts"] = time.time() - 40 * 86400

    store._mutate(age)
    stats = store.stats()
    assert sum(d["words"] for d in stats["daily"]) == 0
    assert stats["total_words"] == 3  # totals still count it


def test_restore_puts_a_deleted_item_back_with_its_identity(tmp_path):
    store = HistoryStore(tmp_path / "h.jsonl")
    item = store.add("bring me back", duration_s=2.0)
    store.delete(item["id"])
    assert store.list() == []
    store.restore(item)
    back = store.list()
    assert len(back) == 1
    assert back[0]["id"] == item["id"]
    assert back[0]["ts"] == item["ts"]
    assert back[0]["text"] == "bring me back"


def test_restore_returns_the_record_to_its_original_position(tmp_path):
    """Verify that restore() returns a deleted record to its original position,
    not to the top of the list. With three records, the restored middle record
    must land in the middle again, not at either end."""
    store = HistoryStore(tmp_path / "h.jsonl")

    # Add three records with distinct, increasing timestamps
    oldest_ts = 1_000_000.0
    middle_ts = 1_000_001.0
    newest_ts = 1_000_002.0

    # Add oldest
    oldest_id = store.add("oldest text", duration_s=1.0)["id"]
    def set_oldest_ts(items):
        for item in items:
            if item["id"] == oldest_id:
                item["ts"] = oldest_ts
    store._mutate(set_oldest_ts)

    # Add middle
    middle_id = store.add("middle text", duration_s=1.0)["id"]
    def set_middle_ts(items):
        for item in items:
            if item["id"] == middle_id:
                item["ts"] = middle_ts
    store._mutate(set_middle_ts)

    # Add newest
    newest_id = store.add("newest text", duration_s=1.0)["id"]
    def set_newest_ts(items):
        for item in items:
            if item["id"] == newest_id:
                item["ts"] = newest_ts
    store._mutate(set_newest_ts)

    # Verify all three are present, sorted by ts descending (newest first)
    before_delete = store.list()
    assert len(before_delete) == 3
    assert [item["id"] for item in before_delete] == [newest_id, middle_id, oldest_id]
    assert [item["ts"] for item in before_delete] == [newest_ts, middle_ts, oldest_ts]

    # Capture the middle item before deletion
    middle_to_restore = next(item for item in before_delete if item["id"] == middle_id)

    # Delete the middle record
    store.delete(middle_id)
    assert len(store.list()) == 2
    assert [item["id"] for item in store.list()] == [newest_id, oldest_id]

    # Restore it
    store.restore(middle_to_restore)

    # Verify it's back in the middle position with original timestamp
    after_restore = store.list()
    assert len(after_restore) == 3
    assert [item["id"] for item in after_restore] == [newest_id, middle_id, oldest_id]
    assert [item["ts"] for item in after_restore] == [newest_ts, middle_ts, oldest_ts]

    # Verify the restored record has the original timestamp, not a fresh one
    restored_middle = next(item for item in after_restore if item["id"] == middle_id)
    assert restored_middle["ts"] == middle_ts


def test_restore_is_a_no_op_when_the_id_is_already_present(tmp_path):
    store = HistoryStore(tmp_path / "h.jsonl")
    item = store.add("only once", duration_s=1.0)
    store.restore(item)
    assert len(store.list()) == 1


def test_clear_removes_everything_in_one_write_and_reports_the_count(tmp_path):
    store = HistoryStore(tmp_path / "h.jsonl")
    for n in range(3):
        store.add(f"entry {n}", duration_s=1.0)
    assert store.clear() == 3
    assert store.list() == []
    assert store.stats()["total_words"] == 0


def test_clear_on_empty_history_returns_zero(tmp_path):
    assert HistoryStore(tmp_path / "h.jsonl").clear() == 0
