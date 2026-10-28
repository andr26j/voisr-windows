import functools
import subprocess

import pytest

import voisr.ui as ui
from voisr.audio import to_wav16k


@pytest.fixture(autouse=True)
def _no_real_paste(monkeypatch):
    """An Api built in a test must never paste into the user's focused
    window or overwrite their clipboard. The release path pastes from a
    background thread after the test has already passed, so a missing
    paste_fn typed fake transcripts into whatever app was in front. An Api
    built without paste_fn / clipboard_fn gets inert ones; a test that
    needs the real thing passes it explicitly."""
    from voisr.ui import Api

    real_init = Api.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("paste_fn", lambda text: True)
        kwargs.setdefault("clipboard_fn", lambda text: None)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Api, "__init__", init)


@pytest.fixture(autouse=True)
def _no_tap_gate(monkeypatch):
    """Most HotkeyService tests press and release within microseconds and
    expect a dictation, which the 500 ms tap gate would hold back as a tap.
    They get the gate switched off; a test of the gate passes tap_gate_s."""
    from voisr.ui import HotkeyService

    real_init = HotkeyService.__init__

    @functools.wraps(real_init)  # the real signature stays inspectable
    def init(self, *args, **kwargs):
        kwargs.setdefault("tap_gate_s", 0)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(HotkeyService, "__init__", init)


class FakeTimer:
    """Stands in for threading.Timer and never runs anything by itself.

    Records what was armed so a test can assert on it and then invoke .fn()
    deliberately, which keeps the shutdown path deterministic and free of
    real delays.
    """

    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self.started = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        pass


@pytest.fixture(autouse=True)
def _no_real_shutdown(monkeypatch):
    """Keep Api's post-install shutdown out of the test process.

    Api._shutdown_soon's real collaborators are a live threading.Timer and
    os._exit(0). A test that reached them would hard-exit pytest from a
    background timer thread seconds later -- and because os._exit(0) exits
    *successfully*, pytest would report a green run with every remaining
    test silently unrun. A suite that passes while testing less than it
    claims is a worse failure than a crash, so this is guarded suite-wide
    rather than per-module: any Api, in any test file, built any way.

    setdefault, so a test that injects its own factory or exit_fn to observe
    the shutdown still wins.
    """
    real_init = ui.Api.__init__

    def guarded(self, *args, **kwargs):
        kwargs.setdefault("timer_factory", FakeTimer)
        kwargs.setdefault("exit_fn",
                          lambda: pytest.fail("the hard exit fired"))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(ui.Api, "__init__", guarded)

# SAPI writes 16-bit PCM WAV directly, so ffmpeg only has to resample.
_SAPI = """
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToWaveFile("{path}")
$s.Speak("{text}")
$s.Dispose()
"""


@pytest.fixture
def spoken_wav(tmp_path):
    def make(text: str):
        raw = tmp_path / "speech_raw.wav"
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _SAPI.format(path=str(raw), text=text.replace('"', ""))],
            check=True, capture_output=True)
        return to_wav16k(raw, tmp_path / "speech.wav")

    return make
