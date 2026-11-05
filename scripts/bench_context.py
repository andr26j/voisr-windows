"""What screen context costs on this PC. Prints lengths, reasons and times,
never text.

  python scripts\\bench_context.py            # against a test window of native boxes
  python scripts\\bench_context.py --focused  # against the field you click into

The default run opens tests\\_text_window.py in its own process (a Win32 Edit,
like any other app's), takes the foreground for a few seconds and briefly uses
the clipboard. It reports capture and verify reads, delivery with the feature
on and off, and a hung target (the window's thread asleep for 3 s).

--focused only reads: click into a field of the app you care about (Chrome,
Word, Teams, Outlook) within 5 seconds and leave it there. It shows what that
app gives (readable or why not, caret, terms) and how long reads take there,
which is what the 150 ms budget is measured against.
"""
import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voisr import speech_gate  # noqa: E402
from voisr.ax_context import AXWorker, FocusTracker, capture_focus  # noqa: E402
from voisr.context_terms import context_terms  # noqa: E402
from voisr.history import HistoryStore  # noqa: E402
from voisr.inject import paste_at_cursor  # noqa: E402
from voisr.uia import UIAAdapter  # noqa: E402
from voisr.ui import Api  # noqa: E402

WINDOW = ROOT / "tests" / "_text_window.py"
WM_APP_HANG = 0x8000 + 1
BUDGET_S = 0.15


def stats(name, values):
    values = sorted(values)
    p95 = values[max(0, int(len(values) * .95) - 1)]
    print(f"  {name:38s} n={len(values):3d} median={statistics.median(values):7.1f} ms  "
          f"p95={p95:7.1f}  max={values[-1]:7.1f}")


class Recorder:
    def start(self, path):
        self.path = path

    def stop(self):
        with wave.open(str(self.path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"\x10\x20" * 16000 * 3)
        return ""

    def cancel(self):
        pass

    def close(self):
        pass


def describe(focus):
    return (f"{focus.bundle_id or '?'}: role={focus.role} readable={focus.readable} "
            f"reason={focus.reason} caret={focus.caret_known} "
            f"trust_absence={focus.trust_absence} field={len(focus.value or '')} chars, "
            f"{len(focus.nearby_texts)} nearby labels")


def reads(adapter, label, runs=30):
    print(f"{label}:")
    worker = AXWorker()
    press, release, terms_ms = [], [], []
    focus = None
    try:
        for _ in range(runs):
            start = time.perf_counter()
            focus = worker.submit(lambda: capture_focus(adapter, BUDGET_S)).result(10)
            press.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            worker.submit(lambda: capture_focus(adapter, BUDGET_S, textbox_only=True)).result(10)
            release.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            terms = context_terms(focus)
            terms_ms.append((time.perf_counter() - start) * 1000)
    finally:
        worker.close()
    print(f"  {describe(focus)}, {len(terms)} terms")
    stats("press capture (with window walk)", press)
    stats("release / verify read (field only)", release)
    stats("term extraction", terms_ms)
    over = sum(ms > BUDGET_S * 1000 for ms in release)
    print(f"  field reads over the {BUDGET_S * 1000:.0f} ms budget: {over} of {runs}")


def delivery(adapter, tmp):
    print("Real Ctrl+V into the test window, whisper stubbed (history timings):")
    speech_gate.gate_wav = lambda wav: None  # the stub clip is no speech
    for label, ax in (("feature on", adapter), ("no adapter", None)):
        api = Api(config_path=tmp / f"{label}.json",
                  history=HistoryStore(tmp / f"{label}.jsonl"),
                  recorder_factory=Recorder, ax_adapter=ax,
                  transcribe_fn=lambda wav: "benchmark words", paste_fn=paste_at_cursor)
        walls = []
        try:
            for _ in range(8):
                api.start_recording(prewarm=False)
                api.prewarm_now()
                if api._focus is not None:
                    api._focus.worker.submit(lambda: None).result(10)
                start = time.perf_counter()
                out = api.stop_recording(paste=True)
                walls.append((time.perf_counter() - start) * 1000)
                if "error" in out:
                    print(f"  {label}: {out['error']}")
                    return
            items = api.get_history()
        finally:
            api.close()
        verdicts = sorted({item.get("delivery", "-") for item in items})
        print(f"  {label}: verdicts {verdicts}")
        stats(f"{label}: release to return", walls)
        for key in ("total", "paste", "verify", "ax_context"):
            values = [item["timings_ms"][key] for item in items
                      if key in item.get("timings_ms", {})]
            if values:
                stats(f"{label}: {key}", values)


def hung(adapter, main_hwnd):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                    wintypes.LPARAM]
    print("The same window, its thread asleep for 3 s:")
    tracker = FocusTracker(adapter, lambda: True)
    try:
        tracker.read_now(1.0)  # warm
        user32.PostMessageW(main_hwnd, WM_APP_HANG, 3000, 0)
        time.sleep(0.2)
        start = time.perf_counter()
        focus = tracker.read_now(BUDGET_S)
        waited = (time.perf_counter() - start) * 1000
        print(f"  paste check's read: {waited:.0f} ms, "
              f"{'unknown (no read in time)' if focus is None else describe(focus)}")
        start = time.perf_counter()
        later = tracker.read_now(BUDGET_S)
        print(f"  the next read: {(time.perf_counter() - start) * 1000:.0f} ms, "
              f"{'unknown' if later is None else 'reason=' + str(later.reason)}")
        time.sleep(3.2)
    finally:
        tracker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--focused", action="store_true",
                        help="read the field you click into, and nothing else")
    args = parser.parse_args()
    adapter = UIAAdapter()
    if args.focused:
        for left in range(5, 0, -1):
            print(f"Click into a field... {left}", flush=True)
            time.sleep(1)
        reads(adapter, "The focused field")
        return
    proc = subprocess.Popen([sys.executable, str(WINDOW), "--focus"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        info = json.loads(proc.stdout.readline())
        if not info["foreground"]:
            print("This desktop wouldn't give the test window the foreground.")
            return
        reads(adapter, "Test window, a Win32 Edit in another process")
        with tempfile.TemporaryDirectory() as tmp:
            delivery(adapter, Path(tmp))
        hung(adapter, info["main"])
    finally:
        proc.stdin.close()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
