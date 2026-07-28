import pytest

import voisr.app as app
from voisr.app import DictationSession, load_config
from voisr.pasteboard import PasteDeliveryError


def test_load_config_uses_defaults_when_absent(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["hotkey"] == "ctrl_r"
    assert cfg["paste"] is True


def test_load_config_merges_over_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"hotkey": "f13"}')
    cfg = load_config(path)
    assert cfg["hotkey"] == "f13"
    assert cfg["paste"] is True


def test_load_config_survives_corrupt_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    assert load_config(path)["hotkey"] == "ctrl_r"


class FakeRecorder:
    def __init__(self):
        self.started = self.stopped = False

    def start(self, path):
        self.started = True

    def stop(self):
        self.stopped = True
        return ""

    def cancel(self):
        pass


def session(monkeypatch, transcribe=None, deliver=None):
    recorder = FakeRecorder()
    monkeypatch.setattr(app, "transcribe", transcribe or (lambda wav, **kw: "raw text"))
    delivered = []
    monkeypatch.setattr(app, "paste_at_cursor",
                        deliver or (lambda text, **kw: delivered.append(text) or True))
    return DictationSession(recorder=recorder, config={"paste": True, "dictionary": {}}), delivered


def test_press_starts_the_recorder(monkeypatch, tmp_path):
    s, _ = session(monkeypatch)
    s.press(tmp_path / "a.wav")
    assert s.recorder.started


def test_release_transcribes_cleans_and_delivers(monkeypatch, tmp_path):
    s, delivered = session(monkeypatch, transcribe=lambda wav, **kw: "um hello world")
    s.press(tmp_path / "a.wav")
    s.release()
    assert delivered == ["Hello world"]


def test_release_without_press_is_a_noop(monkeypatch, tmp_path):
    s, delivered = session(monkeypatch)
    s.release()
    assert delivered == []


def test_empty_transcript_is_not_delivered(monkeypatch, tmp_path):
    s, delivered = session(monkeypatch, transcribe=lambda wav, **kw: "   ")
    s.press(tmp_path / "a.wav")
    s.release()
    assert delivered == []


def test_blocked_paste_is_reported_not_swallowed(monkeypatch, tmp_path):
    def blocked(text, **kw):
        raise PasteDeliveryError("paste was blocked; clipboard preserved")

    s, _ = session(monkeypatch, deliver=blocked)
    s.press(tmp_path / "a.wav")
    result = s.release()
    assert result.blocked is True
    assert result.text == "Raw text"
