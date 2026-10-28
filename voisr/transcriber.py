import subprocess
from pathlib import Path

from voisr.paths import find_binary
from voisr.paths import models_dir as _models_dir

MODELS_DIR = _models_dir()
MODEL_PATH = MODELS_DIR / "ggml-base.en.bin"

# best available first: small.en is a large accuracy jump over base.en
PREFERRED_MODELS = ["small.en", "base.en"]

# Without this every dictation flashes a console window over the user's app.
CREATE_NO_WINDOW = 0x08000000


class TranscriptionError(RuntimeError):
    pass


def _timeout_for(wav_path) -> float:
    """A hung whisper-cli held the queue (and Quit) forever. Allow two
    minutes plus twice the audio length: real CUDA runs take a
    fraction of the audio length, so this only ever catches a hang."""
    try:
        seconds = Path(wav_path).stat().st_size / 32000   # 16 kHz mono int16
    except OSError:
        seconds = 0
    return 120 + 2 * seconds


def resolve_model(name: str | None = None, models_dir: Path = MODELS_DIR) -> Path:
    """Best model on disk: the configured one, else small.en, else base.en."""
    candidates = ([name] if name else []) + PREFERRED_MODELS
    for n in candidates:
        p = Path(models_dir) / f"ggml-{n}.bin"
        if p.exists():
            return p
    return Path(models_dir) / f"ggml-{PREFERRED_MODELS[-1]}.bin"


def transcribe(wav_path, model_path=None, whisper_bin=None, prompt=None) -> str:
    """Transcribe a 16 kHz mono WAV with whisper-cli.exe, return plain text.

    prompt: optional context string (e.g. the user's dictionary terms) that
    biases decoding toward the right spellings of names and jargon."""
    whisper = whisper_bin or find_binary("whisper-cli")
    if not whisper:
        raise TranscriptionError(
            "whisper-cli.exe not found — run: powershell -File scripts/setup.ps1")
    model = Path(model_path) if model_path else resolve_model()
    if not model.exists():
        raise TranscriptionError(f"model missing: {model} — run scripts/setup.ps1")
    out_prefix = str(wav_path) + ".out"
    cmd = [whisper, "-m", str(model), "-f", str(wav_path),
           "--output-txt", "--output-file", out_prefix, "--no-prints",
           "--beam-size", "5"]
    if prompt:
        cmd += ["--prompt", prompt]
    timeout = _timeout_for(wav_path)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              creationflags=CREATE_NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TranscriptionError(f"whisper-cli timed out after {timeout:.0f} s")
    if proc.returncode != 0:
        raise TranscriptionError(proc.stderr.strip() or "whisper-cli failed")
    txt = Path(out_prefix + ".txt")
    text = txt.read_text(encoding="utf-8").strip()
    txt.unlink(missing_ok=True)
    return text
