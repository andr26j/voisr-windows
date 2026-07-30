import subprocess
from pathlib import Path

import pytest

import voisr.transcriber as transcriber
from voisr.transcriber import TranscriptionError, resolve_model, transcribe


def test_resolve_model_prefers_named(tmp_path):
    (tmp_path / "ggml-small.en.bin").write_bytes(b"")
    (tmp_path / "ggml-base.en.bin").write_bytes(b"")
    assert resolve_model("base.en", tmp_path) == tmp_path / "ggml-base.en.bin"


def test_resolve_model_prefers_small_when_unnamed(tmp_path):
    (tmp_path / "ggml-small.en.bin").write_bytes(b"")
    (tmp_path / "ggml-base.en.bin").write_bytes(b"")
    assert resolve_model(None, tmp_path) == tmp_path / "ggml-small.en.bin"


def test_resolve_model_falls_back_to_base_path(tmp_path):
    assert resolve_model(None, tmp_path) == tmp_path / "ggml-base.en.bin"


def test_missing_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "find_binary", lambda name: None)
    with pytest.raises(TranscriptionError, match="whisper-cli.exe not found"):
        transcribe(tmp_path / "a.wav")


def test_missing_model_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    with pytest.raises(TranscriptionError, match="model missing"):
        transcribe(tmp_path / "a.wav", model_path=tmp_path / "absent.bin")


def test_transcribe_reads_output_and_cleans_up(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    model = tmp_path / "m.bin"
    model.write_bytes(b"")
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        Path(str(wav) + ".out.txt").write_text("  hello world  ")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    monkeypatch.setattr(transcriber.subprocess, "run", fake_run)

    assert transcribe(wav, model_path=model) == "hello world"
    assert not Path(str(wav) + ".out.txt").exists()
    assert "--beam-size" in recorded["cmd"]


def test_transcribe_passes_prompt(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    model = tmp_path / "m.bin"
    model.write_bytes(b"")
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        Path(str(wav) + ".out.txt").write_text("x")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    monkeypatch.setattr(transcriber.subprocess, "run", fake_run)

    transcribe(wav, model_path=model, prompt="TradeMate")
    assert "--prompt" in recorded["cmd"]
    assert "TradeMate" in recorded["cmd"]


def test_nonzero_exit_raises(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    model = tmp_path / "m.bin"
    model.write_bytes(b"")

    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    monkeypatch.setattr(
        transcriber.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "whisper failed"))

    with pytest.raises(TranscriptionError, match="whisper failed"):
        transcribe(wav, model_path=model)


def test_subprocess_hides_console_window(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    model = tmp_path / "m.bin"
    model.write_bytes(b"")
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded.update(kwargs)
        Path(str(wav) + ".out.txt").write_text("x")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(transcriber, "find_binary", lambda name: "whisper-cli.exe")
    monkeypatch.setattr(transcriber.subprocess, "run", fake_run)

    transcribe(wav, model_path=model)
    assert recorded["creationflags"] == transcriber.CREATE_NO_WINDOW
