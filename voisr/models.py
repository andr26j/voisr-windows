"""Speech model registry, install state and downloads.

Every model Voisr offers is described here: the file it lives in, its exact
size and SHA-256 (from the whisper.cpp model repo's published metadata), and
what it needs from the PC.

Two folders hold models. ggml-base.en.bin ships inside Voisr (BUNDLED_DIR,
read-only, replaced by every install and update) and is the default. Every
other model is a download the person chooses in Settings, saved per user in
%LOCALAPPDATA%\\Voisr\\models (MODELS_DIR), which updates never touch. A file
in MODELS_DIR wins over a bundled one of the same name.

``BENCHMARKS`` is empty on purpose. No corpus has been recorded yet, so no
speed or accuracy figure exists. When one does, add an entry keyed by model id
and the Settings list will show it; nothing else changes.
"""
from __future__ import annotations

import functools
import hashlib
import http.client
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from voisr.paths import models_dir as _bundled_models_dir
from voisr.paths import user_models_dir

MODELS_DIR = user_models_dir()
BUNDLED_DIR = _bundled_models_dir()
DOWNLOAD_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

# Free space left over after a download, so the disk isn't filled to the brim.
DISK_HEADROOM_BYTES = 512 * 1024 * 1024
GIB = 1024 ** 3


@dataclass(frozen=True)
class Model:
    id: str
    engine: str
    label: str
    description: str
    filename: str
    bytes: int
    sha256: str
    min_memory_gb: int       # conservative, NOT measured: see BENCHMARKS
    license: str


# Sizes and checksums are the LFS metadata of huggingface.co/ggerganov/
# whisper.cpp (tree API, 2026-11-03). Weights are OpenAI Whisper's, MIT.
REGISTRY: tuple[Model, ...] = (
    Model("base.en", "whisper.cpp", "Base (English)",
          "The smallest of the four. English only. Comes with Voisr; the default.",
          "ggml-base.en.bin", 147_964_211,
          "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
          4, "MIT"),
    Model("small.en", "whisper.cpp", "Small (English)",
          "A step up in size from Base. English only.",
          "ggml-small.en.bin", 487_614_201,
          "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
          4, "MIT"),
    Model("medium.en-q5_0", "whisper.cpp", "Medium (English, compressed)",
          "A larger model, compressed (5-bit) to save disk. English only.",
          "ggml-medium.en-q5_0.bin", 539_225_533,
          "76733e26ad8fe1c7a5bf7531a9d41917b2adc0f20f2e4f5531688a8c6cd88eb0",
          12, "MIT"),
    Model("large-v3-turbo-q5_0", "whisper.cpp", "Large v3 Turbo (compressed)",
          "The largest model offered, compressed (5-bit). Multilingual model, "
          "always run in English.",
          "ggml-large-v3-turbo-q5_0.bin", 574_041_195,
          "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
          12, "MIT"),
)

# Measured figures, keyed by model id, e.g.
#   "small.en": {"wer": 0.08, "median_s": 0.9, "peak_mb": 900}
# Empty until the benchmark corpus is recorded (scripts/bench_models.py).
BENCHMARKS: dict[str, dict] = {}

_BY_ID = {m.id: m for m in REGISTRY}
_FILENAMES = {m.filename for m in REGISTRY}


def get_model(model_id) -> Model | None:
    return _BY_ID.get(model_id)


def filename_for(model_id: str) -> str:
    """Registered file name, else the ggml-<name>.bin convention that custom
    and pre-registry configs use."""
    m = _BY_ID.get(model_id)
    return m.filename if m else f"ggml-{model_id}.bin"


def id_from_path(path) -> str:
    name = Path(path).name
    for m in REGISTRY:
        if m.filename == name:
            return m.id
    return Path(path).stem.removeprefix("ggml-")


def search_dirs(models_dir: Path = MODELS_DIR, bundled_dir: Path | None = None) -> list[Path]:
    """Where to look, most specific first: the person's own folder, then what
    ships with Voisr."""
    dirs = [Path(models_dir)]
    if bundled_dir is not None and Path(bundled_dir) != dirs[0]:
        dirs.append(Path(bundled_dir))
    return dirs


def discover_custom(models_dir: Path = MODELS_DIR) -> list[Path]:
    """Other ggml-*.bin files dropped into a models folder. .part files never
    match the glob; registry files are listed as themselves, not as Custom."""
    d = Path(models_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("ggml-*.bin")
                  if p.is_file() and p.name not in _FILENAMES)


def installed_paths(models_dir: Path = MODELS_DIR,
                    bundled_dir: Path | None = None) -> dict[str, Path]:
    """id -> path for every usable model on disk, registry and custom. A file
    in models_dir wins over a bundled file of the same name."""
    found = {}
    for d in reversed(search_dirs(models_dir, bundled_dir)):
        for m in REGISTRY:
            if (d / m.filename).is_file():
                found[m.id] = d / m.filename
        for p in discover_custom(d):
            found[id_from_path(p)] = p
    return found


def is_bundled(path, bundled_dir: Path | None) -> bool:
    """True for a file that ships with Voisr: it can't be deleted, and the
    next update would put it back anyway."""
    return bundled_dir is not None and Path(path).parent == Path(bundled_dir)


def ssl_context() -> ssl.SSLContext:
    """Windows' own certificate store, plus certifi's bundle on top.

    The store alone can fail on a fresh PC: Windows fetches root
    certificates on demand, but only for its own TLS stack, so a root that
    has never been needed yet isn't there for Python. certifi alone would
    drop a company's inspecting proxy, whose root is in the store. Both
    together cover both."""
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return context


def _total_memory_bytes() -> int | None:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys)


def _cpu_name() -> str | None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
        value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
    return " ".join(str(value).split()) or None


@functools.lru_cache(maxsize=1)
def system_info() -> dict:
    """Processor and memory of this PC, read at runtime. Either may be None."""
    memory_gb = None
    chip = None
    try:
        total = _total_memory_bytes()
        if total:
            memory_gb = round(total / GIB, 1)
    except Exception:
        pass
    try:
        chip = _cpu_name()
    except Exception:
        pass
    return {"memory_gb": memory_gb, "chip": chip}


def restriction(model: Model, system: dict) -> tuple[str | None, str | None]:
    """(blocked reason, warning) for this model on this PC. Every model runs
    on any x64 PC (whisper.cpp picks a CPU build at load), so nothing is
    blocked; one that wants more memory than the PC has only warns, since the
    requirement is a conservative estimate."""
    mem = system.get("memory_gb")
    if mem is not None and model.min_memory_gb > mem:
        return None, (f"Wants about {model.min_memory_gb} GB of memory; "
                      f"this PC has {mem:g} GB. It may run slowly.")
    return None, None


def list_models(models_dir: Path = MODELS_DIR, system: dict | None = None,
                active: str | None = None, bundled_dir: Path | None = None) -> list[dict]:
    system = system if system is not None else system_info()
    installed = installed_paths(models_dir, bundled_dir)
    rows = []
    for m in REGISTRY:
        blocked, warning = restriction(m, system)
        p = installed.get(m.id)
        rows.append({
            "id": m.id, "label": m.label, "description": m.description,
            "bytes": m.bytes, "installed": p is not None,
            "active": m.id == active, "custom": False,
            "bundled": p is not None and is_bundled(p, bundled_dir),
            "min_memory_gb": m.min_memory_gb,
            "license": m.license, "blocked": blocked, "warning": warning,
            "benchmark": BENCHMARKS.get(m.id),
        })
    for mid, p in installed.items():
        if mid in _BY_ID:
            continue
        rows.append({
            "id": mid, "label": mid, "description": "Your own model file.",
            "bytes": p.stat().st_size, "installed": True,
            "active": mid == active, "custom": True,
            "bundled": is_bundled(p, bundled_dir), "min_memory_gb": None,
            "license": None, "blocked": None, "warning": None, "benchmark": None,
        })
    return rows


def check_disk_space(model: Model, models_dir: Path = MODELS_DIR,
                     free_bytes: int | None = None, have: int = 0) -> str | None:
    """None if the model fits, else a message saying how much is missing.
    `have`: bytes of it already on disk from an interrupted download."""
    if free_bytes is None:
        d = Path(models_dir)
        d.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(d).free
    still = max(model.bytes - have, 0)
    need = still + DISK_HEADROOM_BYTES
    if free_bytes >= need:
        return None
    mb = lambda n: f"{n / 1024 ** 2:.0f} MB"
    return (f"Not enough disk space for {model.label}: it needs {mb(still)} "
            f"plus {mb(DISK_HEADROOM_BYTES)} to spare, and only "
            f"{mb(max(free_bytes, 0))} is free.")


def delete_file(path, tries: int = 20, delay: float = 0.05) -> None:
    """Remove a model file. Windows refuses while anything has it open: a
    scanner for a moment, or a whisper process still reading it in, so wait
    a second before giving up."""
    for attempt in range(tries):
        try:
            Path(path).unlink()
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay)


class DownloadCancelled(Exception):
    pass


class _Resumable(Exception):
    """The connection failed partway: the bytes so far are kept."""


def _replace(src: Path, dst: Path, tries: int = 20, delay: float = 0.05) -> None:
    """os.replace, waiting out a scanner (Defender, the indexer) that has the
    freshly written file open for a moment."""
    for attempt in range(tries):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay)


class ModelDownloader:
    """One download at a time, on its own thread. Bytes go to <file>.part,
    are hashed as they arrive, and are renamed to the .bin only when size and
    SHA-256 match, so no code path leaves a broken .bin behind.

    A download the connection drops, or that Quit cancels, keeps its .part:
    downloading again sends a Range request and carries on from there,
    after re-hashing what is already on disk. A server that ignores the
    Range starts it over. A bad checksum, an oversized file or an HTTP error
    discards the .part, and starting another model's download removes any
    other model's leftover .part, so at most one is ever on disk."""

    CHUNK = 1024 * 256

    def __init__(self, models_dir: Path = MODELS_DIR, base_url: str = DOWNLOAD_BASE,
                 free_bytes_fn=None, registry=REGISTRY, bundled_dir: Path | None = None):
        self._dir = Path(models_dir)
        self._bundled = bundled_dir
        self._base = base_url.rstrip("/")
        self._free_bytes_fn = free_bytes_fn
        self._registry = {m.id: m for m in registry}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state: dict = {}
        self._seq = 0  # one per download, so the page reports each outcome once

    def start(self, model_id: str, system: dict | None = None) -> dict:
        m = self._registry.get(model_id)
        if m is None:
            return {"error": f"Unknown model: {model_id}"}
        blocked, _ = restriction(m, system if system is not None else system_info())
        if blocked:
            return {"error": f"{m.label}: {blocked}"}
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"error": "Another download is already running."}
            if any((d / m.filename).is_file()
                   for d in search_dirs(self._dir, self._bundled)):
                return {"error": f"{m.label} is already installed."}
            self._remove_other_parts(m)
            part = self._dir / (m.filename + ".part")
            have = part.stat().st_size if part.is_file() else 0
            free = self._free_bytes_fn() if self._free_bytes_fn else None
            msg = check_disk_space(m, self._dir, free, have=min(have, m.bytes))
            if msg:
                return {"error": msg}
            self._cancel.clear()
            self._seq += 1
            self._state = {"id": m.id, "state": "downloading",
                           "done": min(have, m.bytes), "total": m.bytes,
                           "error": None, "seq": self._seq}
            self._thread = threading.Thread(target=self._run, args=(m,),
                                            name="model-download", daemon=True)
            self._thread.start()
        return {"ok": True}

    def progress(self) -> dict:
        with self._lock:
            return dict(self._state) if self._state else {"state": "idle"}

    def active_id(self) -> str | None:
        with self._lock:
            if self._state.get("state") == "downloading":
                return self._state["id"]
        return None

    def cancel(self, wait: float = 5.0):
        """Stop any download, keeping its .part to resume. Called on Quit."""
        self._cancel.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(wait)

    def join(self, timeout: float | None = None):
        t = self._thread
        if t is not None:
            t.join(timeout)

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _remove_other_parts(self, m: Model) -> None:
        for other in self._registry.values():
            if other.filename != m.filename:
                (self._dir / (other.filename + ".part")).unlink(missing_ok=True)

    def _hash_existing(self, part: Path, m: Model):
        """(digest, bytes) of a kept .part, or a fresh start if there is none
        or it can't be the start of this file."""
        digest = hashlib.sha256()
        if not part.is_file():
            return digest, 0
        size = part.stat().st_size
        if size > m.bytes:
            part.unlink()
            return digest, 0
        with open(part, "rb") as f:
            while True:
                if self._cancel.is_set():
                    raise DownloadCancelled()
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest, size

    def _fetch(self, m: Model, part: Path, digest, done: int):
        """Append the rest of the file to `part`. Returns (digest, bytes)."""
        headers = {"User-Agent": "Voisr"}
        if done:
            headers["Range"] = f"bytes={done}-"
        req = urllib.request.Request(f"{self._base}/{m.filename}", headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=30, context=ssl_context())
        except urllib.error.HTTPError:
            raise  # the server answered: not a dropped connection
        except (OSError, http.client.HTTPException) as error:
            raise _Resumable(str(error) or type(error).__name__) from error
        with resp:
            if done and resp.status == 206:
                expected = f"bytes {done}-"
                if not (resp.headers.get("Content-Range") or "").startswith(expected):
                    raise ValueError("The server resumed the download at the "
                                     "wrong place. It was discarded.")
                mode = "ab"
            else:
                digest, done, mode = hashlib.sha256(), 0, "wb"   # from the start
                self._set(done=0)
            with open(part, mode) as out:
                while True:
                    if self._cancel.is_set():
                        raise DownloadCancelled()
                    try:
                        chunk = resp.read(self.CHUNK)
                    except (OSError, http.client.HTTPException) as error:
                        raise _Resumable(str(error) or type(error).__name__) from error
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    self._set(done=done)
                    if done > m.bytes:
                        raise ValueError("The download is larger than expected.")
        return digest, done

    def _run(self, m: Model):
        part = self._dir / (m.filename + ".part")
        final = self._dir / m.filename
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            digest, done = self._hash_existing(part, m)
            self._set(done=done)
            if done < m.bytes:
                digest, done = self._fetch(m, part, digest, done)
            if done != m.bytes:
                raise _Resumable(f"got {done} of {m.bytes} bytes")
            if digest.hexdigest() != m.sha256:
                raise ValueError("Checksum mismatch: the file was corrupted "
                                 "or has changed. It was discarded.")
            _replace(part, final)
            self._set(state="done", done=m.bytes)
        except DownloadCancelled:
            self._set(state="cancelled")
        except _Resumable as error:
            self._set(state="error",
                      error=f"The download stopped ({error}). Download again "
                            "to carry on where it left off.")
        except Exception as error:
            part.unlink(missing_ok=True)
            self._set(state="error", error=str(error) or type(error).__name__)
