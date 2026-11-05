"""The job object that ends whisper-server with Voisr, for real: plain
Python sleepers stand in for the server, so this needs Windows but no
whisper build or model."""
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 job objects")

SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]


def alive(pid):
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED
    if not handle:
        return False
    try:
        return k.WaitForSingleObject(handle, 0) == 0x102   # WAIT_TIMEOUT: still running
    finally:
        k.CloseHandle(handle)


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_a_spawned_process_runs_inside_the_job(tmp_path):
    from voisr import whisper_guard
    marker = tmp_path / "ran"
    job = whisper_guard.Job()
    try:
        popen = whisper_guard.spawn(
            [sys.executable, "-c",
             f"open(r'{marker}', 'w').close(); import time; time.sleep(600)"], job)
        try:
            assert job.contains(popen.pid)
            # created suspended: this only appears once it was resumed
            assert wait_for(marker.exists), "the process never ran"
        finally:
            popen.kill()
            popen.wait()
    finally:
        job.close()


def test_closing_the_job_kills_what_is_inside():
    from voisr import whisper_guard
    job = whisper_guard.Job()
    popen = whisper_guard.spawn(SLEEPER, job)
    try:
        assert popen.poll() is None
        job.close()
        assert popen.wait(10) is not None
    finally:
        if popen.poll() is None:
            popen.kill()


PARENT = """
import sys, time
from voisr import whisper_guard
p = whisper_guard.spawn([sys.executable, "-c", "import time; time.sleep(600)"])
print(p.pid, flush=True)
time.sleep(600)
"""


def test_the_server_dies_when_voisr_is_killed():
    # what a crash or Task Manager does: no Python code runs in the parent
    parent = subprocess.Popen([sys.executable, "-c", PARENT],
                              stdout=subprocess.PIPE, text=True)
    child = None
    try:
        child = int(parent.stdout.readline())
        assert alive(child)
        parent.kill()                       # TerminateProcess
        parent.wait(10)
        assert wait_for(lambda: not alive(child)), "the server outlived Voisr"
    finally:
        if parent.poll() is None:
            parent.kill()
        if child and alive(child):
            subprocess.run(["taskkill", "/F", "/PID", str(child)],
                           capture_output=True)


def test_the_job_is_shared_and_kept_for_the_life_of_the_process():
    from voisr import whisper_guard
    assert whisper_guard.voisr_job() is whisper_guard.voisr_job()


def test_a_command_that_cannot_start_raises_and_leaves_nothing(tmp_path):
    from voisr import whisper_guard
    with pytest.raises(OSError):
        whisper_guard.spawn([str(tmp_path / "no-such.exe")])


def test_a_failed_assignment_kills_the_suspended_process(monkeypatch):
    from voisr import whisper_guard
    seen = []

    class Refusing(whisper_guard.Job):
        def assign_pid(self, pid):
            seen.append(pid)
            raise OSError(5, "Access is denied.")

    job = Refusing()
    try:
        with pytest.raises(OSError):
            whisper_guard.spawn(SLEEPER, job)
        assert seen and wait_for(lambda: not alive(seen[0]))
    finally:
        job.close()


def test_peak_memory_is_read_from_the_job():
    from voisr import whisper_guard
    job = whisper_guard.Job()
    popen = whisper_guard.spawn(
        [sys.executable, "-c", "b = bytearray(64 * 1024 * 1024)"], job)
    try:
        popen.wait(30)
        assert job.peak_process_memory() >= 64 * 1024 * 1024
    finally:
        job.close()
