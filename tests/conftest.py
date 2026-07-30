import subprocess

import pytest

from voisr.audio import to_wav16k

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
