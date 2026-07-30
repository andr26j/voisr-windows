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
