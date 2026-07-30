# Voisr Windows — History Durability Implementation Plan (Phase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A crash-safe, concurrency-safe history store for dictations, backing the dashboard built in Phase 3.

**Architecture:** Port `history.py` from the macOS app, replacing its POSIX durability layer with Windows primitives. The public interface and every behavioural guarantee stay identical; only the mechanism changes. Records are JSON lines appended to `%LOCALAPPDATA%\Voisr\history.jsonl`.

**Tech Stack:** Python 3.13, `msvcrt.locking` / `LockFileEx` via pywin32, `os.replace` (atomic on Windows).

**Depends on:** Phase 0–1 (`docs/superpowers/plans/2026-07-28-core-dictation-loop.md`), complete.

**macOS reference repo, `$MAC`:** clone `andr26j/voisr-mac`. Referenced throughout.

---

## Why this is a rewrite, not a copy

`$MAC/voisr/history.py` is 503 lines built on POSIX primitives that do not exist on Windows:

| macOS line | Uses | Windows |
|---|---|---|
| 2, 330, 338 | `fcntl.flock` | no such API |
| 25–27, 133–134 | `os.O_DIRECTORY`, `O_NOFOLLOW`, `O_NONBLOCK` | flags do not exist |
| 210 | `os.register_at_fork` | no fork on Windows |
| 391, 397 | `os.fsync(parent_fd)` | cannot fsync a directory |
| 171, 295, 306, 329, 387 | `os.fchmod(0o600)` | no-op; Windows uses ACLs |

The rewrite is **simpler** than the original. No fork means the entire `register_at_fork` / `_FORK_EPOCH` / `_OPEN_LOCK_FDS` machinery is deleted. `%LOCALAPPDATA%` is already per-user ACL'd, so the `0600` handling is redundant.

**Expected size: ~503 → ~300 lines.** If your implementation is materially larger, you have probably ported machinery that Windows does not need — stop and report.

## Guarantees that must survive

These come from `$MAC/docs/superpowers/plans/2026-07-23-atomic-history-durability.md`. Read that document before starting. Every one of these must hold on Windows:

1. **Atomic append.** A dictation is either fully in history or not there at all. No torn or partial JSON lines, ever — including if the process is killed mid-write.
2. **Crash durability.** A record reported as saved survives a power loss. Data is flushed to disk before `add()` returns.
3. **Concurrent-process safety.** Two Voisr processes writing at once must not interleave or corrupt records.
4. **Concurrent-thread safety.** The queue writes from worker threads; the dashboard reads from the UI thread.
5. **Corrupt-line tolerance.** One malformed line must not make the whole history unreadable. Skip it, keep the rest.
6. **Mutation atomicity.** `delete` and `clear` rewrite the file; a crash mid-rewrite must leave either the old or the new file intact, never a truncated one. Use `os.replace`, which is atomic on Windows.

## Public interface — unchanged from macOS

`$MAC/voisr/history.py` line 217 onward. Do not alter these signatures; Phase 3's dashboard calls them.

```python
class HistoryStore:
    def __init__(self, path): ...
    def close(self) -> None: ...
    def add(self, text: str, duration_s: float) -> dict: ...
    def list(self) -> list[dict]: ...
    def delete(self, item_id: str) -> None: ...
    def restore(self, item: dict) -> dict: ...
    def clear(self) -> int: ...
    def stats(self) -> dict: ...
```

Read the macOS implementations of each to understand the exact record shape, the `stats()` computation (it uses `Fraction` and a 40 wpm typing baseline for "time saved"), and the 14-day sparkline buckets. Those are pure logic and port unchanged.

---

## File Structure

| File | Responsibility |
|---|---|
| `voisr/filelock.py` | Windows advisory file lock, context-manager interface. New — isolated so it is testable alone. |
| `voisr/history.py` | Record store: append, read, mutate, stats |
| `tests/test_filelock.py` | Lock acquisition, contention, release-on-exception |
| `tests/test_history.py` | Ported behavioural suite |

`filelock.py` is separated deliberately: it is the only part touching Win32, it is the part most likely to be wrong, and isolating it means it can be hammered independently of history semantics.

---

## Task 1: `voisr/filelock.py` — Windows advisory file lock

**Files:** Create `voisr/filelock.py`, test `tests/test_filelock.py`

- [ ] **Step 1: Write the failing tests**

```python
import threading
import time

import pytest

from voisr.filelock import FileLock, LockTimeout


def test_lock_acquires_and_releases(tmp_path):
    path = tmp_path / "h.lock"
    with FileLock(path):
        pass
    with FileLock(path):
        pass  # second acquisition proves the first released


def test_lock_is_exclusive_across_threads(tmp_path):
    path = tmp_path / "h.lock"
    order = []
    holding = threading.Event()
    release = threading.Event()

    def first():
        with FileLock(path):
            order.append("first-in")
            holding.set()
            release.wait(5)
            order.append("first-out")

    t = threading.Thread(target=first)
    t.start()
    holding.wait(5)

    def second():
        with FileLock(path):
            order.append("second-in")

    t2 = threading.Thread(target=second)
    t2.start()
    time.sleep(0.2)
    assert "second-in" not in order  # blocked while first holds it
    release.set()
    t.join(5)
    t2.join(5)
    assert order == ["first-in", "first-out", "second-in"]


def test_lock_released_when_body_raises(tmp_path):
    path = tmp_path / "h.lock"
    with pytest.raises(ValueError):
        with FileLock(path):
            raise ValueError("boom")
    with FileLock(path):
        pass  # must not deadlock


def test_lock_is_reentrant_within_a_thread(tmp_path):
    path = tmp_path / "h.lock"
    with FileLock(path):
        with FileLock(path):
            pass


def test_timeout_raises_rather_than_hanging(tmp_path):
    path = tmp_path / "h.lock"
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with FileLock(path):
            holding.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    holding.wait(5)
    try:
        with pytest.raises(LockTimeout):
            with FileLock(path, timeout=0.3):
                pass
    finally:
        release.set()
        t.join(5)


def test_lock_file_is_created_if_absent(tmp_path):
    path = tmp_path / "sub" / "h.lock"
    path.parent.mkdir()
    with FileLock(path):
        assert path.exists()
```

- [ ] **Step 2: Run them, observe failure**

Run: `./venv/Scripts/python -m pytest tests/test_filelock.py -v`
Expected: `ModuleNotFoundError: No module named 'voisr.filelock'`

- [ ] **Step 3: Implement `voisr/filelock.py`**

You own the implementation. Requirements:

- Context manager: `with FileLock(path, timeout=None):`
- Exclusive across **processes**, not just threads. `msvcrt.locking` with `LK_NBLCK` in a retry loop, or `win32file.LockFileEx` with `LOCKFILE_EXCLUSIVE_LOCK`. Either is acceptable; pick one and explain why in a module docstring.
- **Reentrant within a thread.** `msvcrt.locking` on the same handle from the same process does not block, but two separate `FileLock` objects in one thread must not deadlock. Track depth per thread.
- `LockTimeout` raised when `timeout` elapses. `timeout=None` waits indefinitely.
- The lock file handle must be closed on release, including when the body raises.
- No busy-spin at 100% CPU — sleep between retries.

**A caution learned the hard way on this project:** every `ctypes`/pywin32 call needs its argument types right. Seven real bugs in Phase 1 came from Win32 calls that unit tests could not catch because they mocked the boundary. These tests exercise the real lock, so they will catch a broken one — but only if you actually run them.

- [ ] **Step 4: Run the tests**

Expected: 6 passed.

- [ ] **Step 5: Verify cross-PROCESS exclusion**

The tests above only prove thread exclusion. Write a temporary script (delete after; do not commit) that spawns two real `subprocess` Python children, each acquiring the same lock and appending a marker to a shared file with a sleep inside the critical section. Run 20 iterations. Confirm the critical sections never interleave.

Report the actual result. If they interleave, the lock is not doing its job and the entire durability story is void — report BLOCKED rather than proceeding.

- [ ] **Step 6: Commit**

```bash
git add voisr/filelock.py tests/test_filelock.py
git commit -m "feat: Windows advisory file lock for history durability"
```

---

## Task 2: Port the history test suite

Do this BEFORE writing `history.py`. The tests are the specification.

**Files:** Create `tests/test_history.py`

- [ ] **Step 1: Copy and triage the macOS suite**

Copy `$MAC/tests/test_history.py` (1329 lines, 56 test functions) to `tests/test_history.py`.

Then triage every test into one of three buckets and report the counts:

- **Keep unchanged** — behavioural tests of add/list/delete/restore/clear/stats, corrupt-line tolerance, concurrency. The large majority.
- **Rewrite** — tests that monkeypatch `history_module.fcntl.flock` (around $MAC lines 672–697). These test lock-failure handling, which is still a real requirement. Rewrite them against `voisr.filelock.FileLock` raising instead.
- **Delete** — tests that monkeypatch `history_module.os.fork` (around $MAC lines 741 and 837). Windows has no fork; the behaviour they guard cannot occur. Deleting them is correct, not a shortcut.

For each deleted test, state its name and one line on why it cannot apply.

- [ ] **Step 2: Run and confirm they fail for the right reason**

Run: `./venv/Scripts/python -m pytest tests/test_history.py -v`
Expected: collection error or mass failure — `voisr.history` does not exist yet. Report the actual output.

- [ ] **Step 3: Commit the tests alone**

```bash
git add tests/test_history.py
git commit -m "test: port history behavioural suite from macOS"
```

Committing failing tests separately makes the next commit's diff show exactly what made them pass.

---

## Task 3: `voisr/history.py` — the store

**Files:** Create `voisr/history.py`

- [ ] **Step 1: Read the macOS implementation**

Read `$MAC/voisr/history.py` in full, plus `$MAC/docs/superpowers/plans/2026-07-23-atomic-history-durability.md`.

Identify, and report before writing code:
- Which parts are pure logic that port unchanged (record shape, `stats()`, sparkline bucketing, `restore()` ordering)
- Which parts exist solely to satisfy POSIX constraints and should be deleted

- [ ] **Step 2: Implement**

Write `voisr/history.py` satisfying the ported test suite, using `voisr.filelock.FileLock` for cross-process safety and `os.replace` for atomic mutation.

Requirements:
- `add()` must `flush()` and `os.fsync()` the file handle before returning. Guarantee 2 depends on this.
- `delete()` and `clear()` write a temp file in the SAME directory, fsync it, then `os.replace` onto the target. A temp file on another volume makes `os.replace` non-atomic.
- Reads tolerate a corrupt line: skip it, keep going. Never raise on malformed history.
- No `fcntl`, no `O_NOFOLLOW`, no fork handling.
- Target ~300 lines. Report if you land materially above that.

- [ ] **Step 3: Run the ported suite**

Run: `./venv/Scripts/python -m pytest tests/test_history.py -v`
Expected: all pass. If a test fails, the default assumption is that the implementation is wrong, not the test — the macOS suite encodes real guarantees. Escalate rather than weakening a test.

- [ ] **Step 4: Run the full suite**

Run: `./venv/Scripts/python -m pytest -q`
Expected: 180 pre-existing + the new tests, all passing.

- [ ] **Step 5: Commit**

```bash
git add voisr/history.py
git commit -m "feat: crash-safe history store on Windows primitives"
```

---

## Task 4: Crash-durability verification

Unit tests cannot prove guarantee 1 or 2. This task does.

**Files:** none committed — temporary scripts only

- [ ] **Step 1: Kill-mid-write test**

Write a temporary script that:
1. Creates a store in a temp directory
2. Spawns a child process appending records in a tight loop
3. Kills the child with `TerminateProcess` (not a graceful signal) after ~200ms
4. Reopens the store and calls `list()`

Run 20 iterations. Assert every iteration: `list()` succeeds without raising, and every returned record is complete and well-formed. A torn final line must be tolerated, not fatal.

Report the actual pass rate. Anything below 20/20 is a real defect — investigate rather than rerunning until it passes.

- [ ] **Step 2: Concurrent-writer test**

Two child processes each append 200 records with distinct ID prefixes. Afterwards assert exactly 400 records, no duplicates, none malformed.

Report actual counts.

- [ ] **Step 3: Report**

Both results go in the task report. Delete the scripts; commit nothing.

---

## Task 5: Wire history into the dictation loop

Right now `app.py` transcribes and pastes but records nothing.

**Files:** Modify `voisr/app.py`, `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py`:

```python
def test_successful_dictation_is_recorded(monkeypatch, tmp_path):
    recorded = []

    class FakeHistory:
        def add(self, text, duration_s):
            recorded.append((text, duration_s))
            return {"id": "x", "text": text}

    s, _ = session(monkeypatch, transcribe=lambda wav, **kw: "hello world")
    s.history = FakeHistory()
    s.press(tmp_path / "a.wav")
    s.release()
    assert recorded and recorded[0][0] == "Hello world"


def test_empty_transcript_is_not_recorded(monkeypatch, tmp_path):
    recorded = []

    class FakeHistory:
        def add(self, text, duration_s):
            recorded.append(text)
            return {}

    s, _ = session(monkeypatch, transcribe=lambda wav, **kw: "   ")
    s.history = FakeHistory()
    s.press(tmp_path / "a.wav")
    s.release()
    assert recorded == []


def test_blocked_paste_is_still_recorded(monkeypatch, tmp_path):
    """A blocked paste still produced a transcript; losing it would be the
    worst outcome, since the user cannot re-speak what they already said."""
    recorded = []

    class FakeHistory:
        def add(self, text, duration_s):
            recorded.append(text)
            return {}

    def blocked(text, **kw):
        raise PasteDeliveryError("paste was blocked; clipboard preserved")

    s, _ = session(monkeypatch, deliver=blocked)
    s.history = FakeHistory()
    s.press(tmp_path / "a.wav")
    s.release()
    assert recorded == ["Raw text"]


def test_history_failure_never_loses_the_paste(monkeypatch, tmp_path):
    """History is secondary. If it fails, the dictation must still land."""
    class ExplodingHistory:
        def add(self, text, duration_s):
            raise OSError("disk full")

    s, delivered = session(monkeypatch, transcribe=lambda wav, **kw: "hello")
    s.history = ExplodingHistory()
    s.press(tmp_path / "a.wav")
    result = s.release()
    assert delivered == ["Hello"]
    assert result.delivered is True
```

- [ ] **Step 2: Run them, observe failure**

Run: `./venv/Scripts/python -m pytest tests/test_app.py -v`

- [ ] **Step 3: Implement**

Modify `DictationSession`:
- Accept a `history` argument in `__init__`, defaulting to a `HistoryStore` at `ensure_app_data_dir() / "history.jsonl"`
- Record the press timestamp so `duration_s` is real, not zero
- Record after a successful transcript, INCLUDING when the paste was blocked
- Wrap the `history.add` call so a failure is logged and swallowed — a full disk must not cost the user their dictation

- [ ] **Step 4: Run the full suite**

- [ ] **Step 5: Manual check**

Run `./venv/Scripts/python -m voisr.app`, dictate once into Notepad, quit, then:

```bash
type "%LOCALAPPDATA%\Voisr\history.jsonl"
```

Confirm one well-formed JSON record with the right text and a non-zero duration. Report the actual line.

- [ ] **Step 6: Commit**

```bash
git add voisr/app.py tests/test_app.py
git commit -m "feat: record dictations to history"
```

---

## Done criteria

- [ ] Full suite green
- [ ] Cross-process lock exclusion verified with real subprocesses
- [ ] 20/20 kill-mid-write iterations leave history readable
- [ ] 400/400 concurrent records land intact
- [ ] A real dictation appears in `%LOCALAPPDATA%\Voisr\history.jsonl`
- [ ] `history.py` is materially smaller than the macOS original

## Next

`docs/superpowers/plans/2026-07-28-dashboard.md` (Phase 3) — the actual UI, and where history becomes visible.
