"""The microphone probe can't always tell (the privacy switches unset, the
device busy while it opens a stream). A clip with real audio settles it,
since a switched-off or dead microphone sends only zeros."""
from tests.test_silence_pipeline import dictate, make_api, noise, tone

from voisr import permissions


def mic_probe(monkeypatch, answer):
    def probe():
        if answer is None:
            raise OSError("device busy")  # states() reads a failure as unknown
        return answer
    monkeypatch.setattr(permissions, "DEFAULT_PROBES", {"microphone": probe})


def mic(api):
    return {p["key"]: p for p in api.get_permissions()["panes"]}["microphone"]["granted"]


def test_an_unknown_mic_is_granted_once_it_has_been_heard(tmp_path, monkeypatch):
    mic_probe(monkeypatch, None)
    api, _ = make_api(tmp_path, noise(0.5, 10) + tone(1.0, 3000) + noise(0.5, 10, seed=2))
    assert mic(api) is None
    dictate(api)
    assert mic(api) is True
    api.close()


def test_a_clip_too_quiet_to_be_speech_still_proves_the_mic_works(tmp_path, monkeypatch):
    mic_probe(monkeypatch, None)
    api, _ = make_api(tmp_path, noise(2.0, 10))
    out = dictate(api)
    assert "didn't catch that" in out["error"]
    assert mic(api) is True
    api.close()


def test_a_silent_mic_proves_nothing(tmp_path, monkeypatch):
    mic_probe(monkeypatch, None)
    api, _ = make_api(tmp_path, [0] * 32000)
    dictate(api)
    assert mic(api) is None
    api.close()


def test_a_known_refusal_is_never_overruled(tmp_path, monkeypatch):
    mic_probe(monkeypatch, None)
    api, _ = make_api(tmp_path, noise(0.5, 10) + tone(1.0, 3000))
    dictate(api)
    mic_probe(monkeypatch, False)
    assert mic(api) is False
    api.close()
