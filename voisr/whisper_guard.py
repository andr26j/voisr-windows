"""Makes sure whisper-server dies with Voisr: a Win32 job object.

whisper-server is a C++ program that cannot watch its parent, so a Voisr that
crashes, is ended from Task Manager or quits without stopping it would leave
it holding hundreds of MB. Every server is started inside one job object that
Voisr creates with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and never closes. The
only handle to it is Voisr's own, and it is not inheritable, so when Voisr's
process ends for any reason the kernel closes the handle and kills everything
in the job. Nothing has to run for that to happen, so unlike the Mac's guard
process there is no state file and no stale-server cleanup at startup.

The server is created suspended and only resumed once it is in the job, so
there is no moment in which Voisr could die and leave it running outside.
If it can't be put in the job it is killed at once, and the caller falls
back to whisper-cli.

The job also sets JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION: a crashing
server just ends, with no Windows Error Reporting dialog on the desktop.
"""
import ctypes
import subprocess
import threading
from ctypes import wintypes

CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004

JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD)]


_k32 = None


def _kernel32():
    """kernel32 with argument types set, so 64-bit handles aren't cut to
    32 bits. Loaded on first use: importing this module never touches Win32."""
    global _k32
    if _k32 is not None:
        return _k32
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H, B, D, P = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p
    for name, res, args in (
            ("CreateJobObjectW", H, [P, wintypes.LPCWSTR]),
            ("SetInformationJobObject", B, [H, ctypes.c_int, P, D]),
            ("QueryInformationJobObject", B, [H, ctypes.c_int, P, D, P]),
            ("AssignProcessToJobObject", B, [H, H]),
            ("IsProcessInJob", B, [H, H, ctypes.POINTER(B)]),
            ("OpenProcess", H, [D, B, D]),
            ("OpenThread", H, [D, B, D]),
            ("ResumeThread", D, [H]),
            ("CloseHandle", B, [H]),
            ("GetCurrentProcess", H, []),
            ("CreateToolhelp32Snapshot", H, [D, D]),
            ("Thread32First", B, [H, ctypes.POINTER(THREADENTRY32)]),
            ("Thread32Next", B, [H, ctypes.POINTER(THREADENTRY32)])):
        fn = getattr(k, name)
        fn.restype, fn.argtypes = res, args
    _k32 = k
    return k


def _error(what):
    code = ctypes.get_last_error()
    return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")


class Job:
    """One Win32 job object. kill_on_close: closing the last handle to it,
    including the kernel doing so when this process ends, kills every
    process inside."""

    def __init__(self, kill_on_close=True):
        k = _kernel32()
        handle = k.CreateJobObjectW(None, None)  # no name, not inheritable
        if not handle:
            raise _error("CreateJobObject")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        if kill_on_close:
            flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = flags
        if not k.SetInformationJobObject(
                handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info), ctypes.sizeof(info)):
            error = _error("SetInformationJobObject")
            k.CloseHandle(handle)
            raise error
        self.handle = handle

    def assign_pid(self, pid: int) -> None:
        k = _kernel32()
        process = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise _error("OpenProcess")
        try:
            if not k.AssignProcessToJobObject(self.handle, process):
                raise _error("AssignProcessToJobObject")
        finally:
            k.CloseHandle(process)

    def assign_current(self) -> None:
        """Put this process in the job; its children then start inside it."""
        k = _kernel32()
        if not k.AssignProcessToJobObject(self.handle, k.GetCurrentProcess()):
            raise _error("AssignProcessToJobObject")

    def contains(self, pid: int) -> bool:
        k = _kernel32()
        process = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            raise _error("OpenProcess")
        try:
            result = wintypes.BOOL()
            if not k.IsProcessInJob(process, self.handle, ctypes.byref(result)):
                raise _error("IsProcessInJob")
            return bool(result.value)
        finally:
            k.CloseHandle(process)

    def peak_process_memory(self) -> int:
        """Most committed memory any one process in the job has used, bytes."""
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not _kernel32().QueryInformationJobObject(
                self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info), ctypes.sizeof(info), None):
            raise _error("QueryInformationJobObject")
        return int(info.PeakProcessMemoryUsed)

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle:
            _kernel32().CloseHandle(handle)


def resume_process(pid: int) -> int:
    """Resume every thread of a process created suspended. Returns how many
    were resumed; raises if none was."""
    k = _kernel32()
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise _error("CreateToolhelp32Snapshot")
    resumed = 0
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        more = k.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = k.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if k.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed += 1
                    finally:
                        k.CloseHandle(thread)
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            more = k.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    if not resumed:
        raise OSError(f"could not resume process {pid}")
    return resumed


_job = None
_job_lock = threading.Lock()


def voisr_job() -> Job:
    """The one kill-on-close job of this Voisr process. Never closed: the
    kernel closes it when the process ends, which is the point."""
    global _job
    with _job_lock:
        if _job is None:
            _job = Job(kill_on_close=True)
        return _job


def spawn(command, job=None) -> subprocess.Popen:
    """Start `command` with no console window and no inherited pipes, inside
    `job` (default: voisr_job()), and only then let it run."""
    job = job if job is not None else voisr_job()
    popen = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=CREATE_NO_WINDOW | CREATE_SUSPENDED)
    try:
        job.assign_pid(popen.pid)
        resume_process(popen.pid)
    except BaseException:
        popen.kill()   # TerminateProcess works on a suspended process too
        popen.wait()
        raise
    return popen
