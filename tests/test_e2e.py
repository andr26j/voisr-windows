import pytest

from voisr.cleanup import clean
from voisr.transcriber import transcribe


@pytest.mark.slow
def test_speech_round_trips_to_text(spoken_wav):
    wav = spoken_wav("testing one two three")
    result = clean(transcribe(wav)).lower()
    assert result
    # whisper normalizes spoken numbers to digits ("testing one two three"
    # comes back as "testing 123."), so don't assert on the literal word.
    assert "testing" in result
    assert "three" in result or "3" in result


@pytest.mark.slow
def test_dictionary_is_applied_to_transcript(spoken_wav):
    wav = spoken_wav("i want to open trade mate")
    result = clean(transcribe(wav), dictionary={"trade mate": "TradeMate"})
    assert "TradeMate" in result
