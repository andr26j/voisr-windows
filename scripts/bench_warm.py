r"""Compare the per-dictation whisper-cli with the kept-loaded server.

    venv\Scripts\python.exe scripts\bench_warm.py [--model base.en] [--runs 5]

Uses synthetic speech (Windows' SAPI voice) of about 1, 5 and 30 seconds, so
the numbers are for synthetic clips, not human speech. Reads the model from
where the app looks (the downloads folder, then the models that ship with
Voisr) and never writes there. Starts one whisper-server on 127.0.0.1 and
stops it before exiting.

Memory: "server" is the warm server's private bytes and working set once
loaded, which is what it holds for as long as it stays loaded; "cli peak" is
the most memory one whisper-cli run committed (its job object's peak)."""
import argparse
import ctypes
import difflib
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voisr.models import BUNDLED_DIR, MODELS_DIR, filename_for  # noqa: E402
from voisr.transcriber import CREATE_NO_WINDOW, transcribe  # noqa: E402
from voisr.whisper_server import WhisperServer  # noqa: E402

PROMPT = "Glossary: Voisr, TradeMate."
CLIPS = {
    "1 s": "Send it to Sam.",
    "5 s": "Please email the team about the demo on Friday, and remind "
           "them to bring the signed contract.",
    "30 s": (
        "Good morning everyone. Thanks for joining the call on short notice. "
        "I want to walk through three things today. First, the Melbourne "
        "launch is on track, and the tram wrap artwork goes to print on "
        "Monday. Second, the new dashboard is almost ready, so please test "
        "your logins before Wednesday and report anything odd. Third, we "
        "need a volunteer to book the venue for the end of year dinner. "
        "Let me know by Thursday, and I will confirm the numbers with the "
        "caterer. Thanks again, and have a great week."),
}

# SAPI straight to 16 kHz mono 16-bit, so no ffmpeg is needed.
_SAPI = """
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile("{path}", $f)
$s.Speak("{text}")
$s.Dispose()
"""


def make_clip(text, workdir):
    wav = workdir / f"c{abs(hash(text))}.wav"
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    _SAPI.format(path=str(wav), text=text)],
                   check=True, capture_output=True)
    return wav


def seconds(wav):
    return wav.stat().st_size / 32000


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


def server_mb(pid):
    """(private MB, working set MB) of a running process, NaN if unreadable."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return float("nan"), float("nan")
    try:
        c = PROCESS_MEMORY_COUNTERS_EX()
        c.cb = ctypes.sizeof(c)
        if not k.K32GetProcessMemoryInfo(handle, ctypes.byref(c), c.cb):
            return float("nan"), float("nan")
        return c.PrivateUsage / 1048576, c.WorkingSetSize / 1048576
    finally:
        k.CloseHandle(handle)


def cli_peak_mb(model, wav):
    """Peak committed memory of one whisper-cli run, from its job object."""
    from voisr.paths import find_binary
    from voisr.whisper_guard import Job, resume_process
    job = Job()
    try:
        popen = subprocess.Popen(
            [find_binary("whisper-cli"), "-m", str(model), "-f", str(wav),
             "-l", "en", "--beam-size", "5", "--no-prints",
             "--output-txt", "--output-file", str(wav) + ".m"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | 0x4)    # CREATE_SUSPENDED
        job.assign_pid(popen.pid)
        resume_process(popen.pid)
        popen.wait()
        return job.peak_process_memory() / 1048576
    finally:
        job.close()
        Path(str(wav) + ".m.txt").unlink(missing_ok=True)


def norm(text):
    return " ".join(text.split())


def timed(fn):
    t = time.perf_counter()
    result = fn()
    return (time.perf_counter() - t) * 1000, result


def find_model(name, models_dir):
    dirs = [models_dir] if models_dir else [MODELS_DIR, BUNDLED_DIR]
    for d in dirs:
        p = Path(d) / filename_for(name)
        if p.is_file():
            return p
    sys.exit(f"model not installed: {filename_for(name)} in {', '.join(map(str, dirs))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--models-dir", type=Path)
    args = ap.parse_args()
    model = find_model(args.model, args.models_dir)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        clips = {k: make_clip(v, work) for k, v in CLIPS.items()}
        server = WhisperServer()
        rows = []
        try:
            for name, wav in clips.items():
                cli = [timed(lambda: transcribe(wav, model_path=model, prompt=PROMPT))
                       for _ in range(args.runs)]
                cli_text = cli[-1][1]
                # first dictation after an unload, with no head start at all
                first = []
                for _ in range(3):
                    server.unload()
                    first.append(timed(lambda: server.transcribe(
                        wav, model, prompt=PROMPT)))
                # first dictation when the load began 2 s earlier (a 2 s hold)
                head = []
                for _ in range(3):
                    server.unload()
                    server.prewarm(model)
                    time.sleep(2.0)
                    head.append(timed(lambda: server.transcribe(
                        wav, model, prompt=PROMPT)))
                server.transcribe(wav, model, prompt=PROMPT)   # ensure loaded
                warm = [timed(lambda: server.transcribe(wav, model, prompt=PROMPT))
                        for _ in range(args.runs)]
                warm_texts = [norm(r[0]) for _, r in warm]
                warm_text = warm[-1][1][0]
                private, resident = server_mb(server.loaded_pid())
                rows.append({
                    "clip": name, "secs": seconds(wav),
                    "cli_ms": statistics.median(t for t, _ in cli),
                    "warm_ms": statistics.median(t for t, _ in warm),
                    "first_ms": statistics.median(t for t, _ in first),
                    "head2_ms": statistics.median(t for t, _ in head),
                    "server_private": private, "server_ws": resident,
                    "cli_peak": cli_peak_mb(model, wav),
                    "same": sum(t == norm(cli_text) for t in warm_texts),
                    "runs": len(warm_texts),
                    "similarity": statistics.mean(
                        difflib.SequenceMatcher(None, norm(cli_text).split(),
                                                t.split()).ratio()
                        for t in warm_texts),
                    "cli_text": cli_text, "warm_text": warm_text})
        finally:
            server.shutdown()
    print(f"\nmodel {args.model}, {args.runs} runs, medians, SYNTHETIC clips (SAPI)\n")
    print("| clip | audio | whisper-cli | warm | first after unload (no head start) "
          "| first after unload (2 s head start) | server private / working set "
          "| cli peak | warm text identical to cli | word similarity |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['clip']} | {r['secs']:.1f} s | {r['cli_ms']:.0f} ms | "
              f"{r['warm_ms']:.0f} ms | {r['first_ms']:.0f} ms | {r['head2_ms']:.0f} ms | "
              f"{r['server_private']:.0f} / {r['server_ws']:.0f} MB | {r['cli_peak']:.0f} MB | "
              f"{r['same']}/{r['runs']} | {r['similarity']*100:.1f}% |")
    for r in rows:
        if r["same"] != r["runs"]:
            print(f"\nDIFF {r['clip']}\n cli : {r['cli_text']!r}\n warm: {r['warm_text']!r}")


if __name__ == "__main__":
    main()
