import subprocess

import pytest

import voisr.ui as ui
from voisr.audio import to_wav16k


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
