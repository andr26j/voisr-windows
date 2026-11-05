"""The real whisper-server.exe and a real model: speech comes back as text,
the prompt reaches the server, nothing listens beyond loopback, Quit frees
the memory, and the server dies with Voisr even when Voisr is killed.

Needs bin\\whisper-server.exe (scripts\\fetch_whisper.ps1) and a base.en model
(VOISR_TEST_MODEL, else models\\ggml-base.en.bin), so it is marked slow. The
speech is VOISR_TEST_WAV (CI uses whisper.cpp's samples/jfk.wav) and, for the
prompt test, Windows' own SAPI voice. The model is only read."""
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from voisr.paths import find_binary, models_dir
from voisr.whisper_server import WhisperServer

MODEL = Path(os.environ.get("VOISR_TEST_MODEL") or models_dir() / "ggml-base.en.bin")
JFK = os.environ.get("VOISR_TEST_WAV")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "win32", reason="the Windows build"),
    pytest.mark.skipif(not find_binary("whisper-server") or not MODEL.is_file(),
                       reason="needs whisper-server.exe and the base.en model"),
]

SENTENCE = "We hired Zorblax Quindleton to build the Kestrelway dashboard."
PROMPT = "Glossary: Zorblaks Kwindelton, Kestrelwey."

# SAPI straight to 16 kHz mono 16-bit, so no ffmpeg is needed.
_SAPI = """
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile("{path}", $f)
$s.Speak("{text}")
$s.Dispose()
"""


def sapi_wav(tmp_path, text):
    path = tmp_path / "sapi.wav"
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         _SAPI.format(path=str(path), text=text)],
        capture_output=True, text=True)
    if proc.returncode != 0 or not path.is_file() or path.stat().st_size < 8000:
        pytest.skip(f"no SAPI voice here: {proc.stderr.strip()[:200]}")
    return path


def speech(tmp_path):
    if JFK and Path(JFK).is_file():
        return Path(JFK)
    return sapi_wav(tmp_path, "And so my fellow Americans, ask not what your "
                              "country can do for you.")


def alive(pid):
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        return False
    try:
        return k.WaitForSingleObject(handle, 0) == 0x102
    finally:
        k.CloseHandle(handle)


def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def listening(pid):
    """Local addresses `pid` listens on, from netstat."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                         capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[4] == str(pid):
            found.append(parts[1])
    return found


def test_speech_comes_back_as_text_warm_and_from_the_cli(tmp_path):
    from voisr.transcriber import transcribe_routed
    wav = speech(tmp_path)
    with wave.open(str(wav), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    server = WhisperServer()
    try:
        started = time.monotonic()
        warm, info = transcribe_routed(wav, MODEL, server=server)
        first_s = time.monotonic() - started
        started = time.monotonic()
        again, _ = transcribe_routed(wav, MODEL, server=server)
        warm_s = time.monotonic() - started
    finally:
        server.shutdown()
    started = time.monotonic()
    cli, cli_info = transcribe_routed(wav, MODEL)
    cli_s = time.monotonic() - started
    print(f"first {first_s:.2f} s, warm {warm_s:.2f} s, cli {cli_s:.2f} s")
    print("warm:", warm, "\ncli: ", cli)
    assert info["mode"] == "warm" and cli_info["mode"] == "cli"
    assert "model_wait" in info["timings"]
    for text in (warm, again, cli):
        assert "country" in text.lower(), text


def test_prompt_reaches_the_server_and_biases_spelling(tmp_path):
    wav = sapi_wav(tmp_path, SENTENCE)
    server = WhisperServer()
    try:
        plain = [server.transcribe(wav, MODEL)[0] for _ in range(2)]
        primed = [server.transcribe(wav, MODEL, prompt=PROMPT)[0] for _ in range(2)]
    finally:
        server.shutdown()
    print("without prompt:", plain)
    print("with prompt:   ", primed)
    assert not any("Kwindelton" in t or "Zorblaks" in t for t in plain)
    assert all("Kwindelton" in t or "Zorblaks" in t or "Kestrelwey" in t for t in primed)


def test_server_listens_on_loopback_only_and_quit_frees_it(tmp_path):
    wav = speech(tmp_path)
    server = WhisperServer()
    server.transcribe(wav, MODEL)
    pid = server.loaded_pid()
    assert pid and alive(pid)
    addresses = listening(pid)
    assert addresses and all(a.startswith("127.0.0.1:") for a in addresses), addresses
    server.shutdown()
    assert wait_for(lambda: not alive(pid), 6)


CHILD = """
import sys, time
from pathlib import Path
from voisr.whisper_server import WhisperServer
s = WhisperServer()
s.prewarm(Path(sys.argv[1]))
while not s.loaded_pid():
    time.sleep(0.05)
print(s.loaded_pid(), flush=True)
time.sleep(600)
"""


def test_a_killed_voisr_leaves_no_whisper_server():
    parent = subprocess.Popen([sys.executable, "-c", CHILD, str(MODEL)],
                              stdout=subprocess.PIPE, text=True, cwd=os.getcwd())
    pid = None
    try:
        pid = int(parent.stdout.readline())
        assert alive(pid)
        parent.kill()                       # TerminateProcess: no cleanup runs
        parent.wait(10)
        assert wait_for(lambda: not alive(pid), 8), "whisper-server outlived Voisr"
    finally:
        if parent.poll() is None:
            parent.kill()
        if pid and alive(pid):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def test_idle_unload_frees_the_server(tmp_path):
    wav = speech(tmp_path)
    server = WhisperServer(poll=0.05)
    try:
        server.transcribe(wav, MODEL)
        pid = server.loaded_pid()
        assert pid
        server._unload = "min2"
        server._last_activity -= 121          # two minutes idle, without waiting
        assert wait_for(lambda: not alive(pid), 6)
        assert server.loaded_pid() is None
    finally:
        server.shutdown()
