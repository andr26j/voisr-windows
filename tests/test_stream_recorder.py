import time
import wave

import pytest

from voisr.stream_recorder import StreamRecorder


def wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _has_input_device() -> bool:
    """True when PortAudio can open a default capture device.

    Mirrors exactly what StreamRecorder.start does: sd.RawInputStream with no
    explicit device resolves the default input, and with none present that
    raises PortAudioError("Error querying device -1").
    """
    try:
        import sounddevice as sd
        sd.query_devices(kind="input")
        return True
    except Exception:
        return False


# The two tests below open the real microphone. On a machine without one they
# failed with PortAudioError, which reads as a code regression when it is only
# absent hardware -- an unplugged headset produced exactly that, and the red
# suite muddied a live incident. Skipping keeps "red" meaning "regression".
requires_microphone = pytest.mark.skipif(
    not _has_input_device(), reason="no audio input device available")


@requires_microphone
def test_captures_full_hold_duration_immediately(tmp_path):
    rec = StreamRecorder()
    try:
        wav = tmp_path / "a.wav"
        rec.start(wav)
        time.sleep(1.0)
        rec.stop()
        assert wav.exists()
        # a cold ffmpeg recorder loses 0.6-0.9s here; the stream must not
        assert wav_seconds(wav) >= 0.8
    finally:
        rec.close()


@requires_microphone
def test_back_to_back_recordings(tmp_path):
    rec = StreamRecorder()
    try:
        for name in ("a.wav", "b.wav"):
            wav = tmp_path / name
            rec.start(wav)
            time.sleep(0.5)
            rec.stop()
            assert wav_seconds(wav) >= 0.35
    finally:
        rec.close()


def test_cap_stops_buffering_and_releases_mic(tmp_path):
    import sounddevice as sd
    rec = StreamRecorder()
    rec._recording = True
    rec._out = str(tmp_path / "c.wav")
    rec._max_bytes = 8  # tiny cap so the test stays instant
    rec._on_audio(b"\x00" * 8, 4, None, None)
    with pytest.raises(sd.CallbackStop):   # sounddevice stops the stream on this
        rec._on_audio(b"\x00" * 8, 4, None, None)
    rec.stop()
    with wave.open(str(tmp_path / "c.wav"), "rb") as w:
        assert w.getnframes() == 4  # only the pre-cap audio was kept


def test_stop_without_start_raises(tmp_path):
    rec = StreamRecorder()
    try:
        with pytest.raises(RuntimeError):
            rec.stop()
    finally:
        rec.close()
