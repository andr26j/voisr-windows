import subprocess
from pathlib import Path

from voisr.models import BUNDLED_DIR, MODELS_DIR, filename_for, search_dirs
from voisr.paths import find_binary

# The model that ships with Voisr; the default (kept for compat).
MODEL_PATH = BUNDLED_DIR / "ggml-base.en.bin"

# Fallback order when the configured model is missing. base.en ships with
# Voisr, so an install always finds it first; small.en only covers a
# checkout set up with it alone. A model downloaded from Settings is never
# chosen automatically: the person picks it with "Use".
FALLBACK_MODELS = ["base.en", "small.en"]

# Without this every dictation flashes a console window over the user's app.
CREATE_NO_WINDOW = 0x08000000


class TranscriptionError(RuntimeError):
    pass


def _timeout_for(wav_path) -> float:
    """A hung whisper-cli held the queue (and Quit) forever. Allow two
    minutes plus twice the audio length: real CPU runs take a fraction of
    the audio length, so this only ever catches a hang."""
    try:
        seconds = Path(wav_path).stat().st_size / 32000   # 16 kHz mono int16
    except OSError:
        seconds = 0
    return 120 + 2 * seconds


def resolve_model(name: str | None = None, models_dir: Path | None = None,
                  bundled_dir: Path | None = None) -> Path:
    """Best model on disk: the configured one (a registry id or the name of a
    custom ggml-<name>.bin), else base.en, else small.en.

    With no models_dir, looks in the per-user downloads folder and then in
    the models that ship with Voisr. With one, looks there (and in
    bundled_dir, when given) only."""
    if models_dir is None:
        models_dir, bundled_dir = MODELS_DIR, BUNDLED_DIR
    dirs = search_dirs(models_dir, bundled_dir)
    candidates = ([name] if name else []) + FALLBACK_MODELS
    for n in candidates:
        for d in dirs:
            p = d / filename_for(n)
            if p.is_file():
                return p
    # Nothing at all: name where the default should be, for the error.
    return dirs[-1] / filename_for(FALLBACK_MODELS[0])


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
           "--beam-size", "5",
           # English only. A multilingual model (large-v3-turbo) would
           # otherwise auto-detect and can transcribe in another language.
           "-l", "en"]
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
