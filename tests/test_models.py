import hashlib
import http.server
import threading
import time
from pathlib import Path

import pytest

from voisr import models
from voisr.models import Model, ModelDownloader, REGISTRY
from voisr.transcriber import resolve_model

PAYLOAD = bytes(range(256)) * 1200   # 307 200 bytes, no two offsets alike


def _model(payload=PAYLOAD, sha=None):
    return Model("fake.en", "whisper.cpp", "Fake", "d", "ggml-fake.en.bin",
                 len(payload), sha or hashlib.sha256(payload).hexdigest(),
                 4, "MIT")


@pytest.fixture
def server():
    """Local HTTP server. Set .mode: ok | short | slow | no-range | lie-range.
    Honours a Range request unless told not to; .ranges records each one."""
    state = {"mode": "ok", "payload": PAYLOAD, "ranges": [], "status": None}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if state["status"]:
                self.send_error(state["status"])
                return
            body = state["payload"]
            mode = state["mode"]
            asked = self.headers.get("Range")
            state["ranges"].append(asked)
            start = 0
            if asked and mode not in ("no-range",):
                start = int(asked.split("=")[1].rstrip("-"))
                shown = start + 1 if mode == "lie-range" else start
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {shown}-{len(body) - 1}/{len(body)}")
                body = body[start:]
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if mode == "short":
                self.wfile.write(body[: len(body) // 2])
                self.wfile.flush()
                self.close_connection = True
                return  # connection closes with bytes missing
            if mode == "slow":
                for i in range(0, len(body), 10_000):
                    try:
                        self.wfile.write(body[i:i + 10_000])
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.05)
                return
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()


def _downloader(tmp_path, server, model, **kw):
    kw.setdefault("free_bytes_fn", lambda: 10 ** 10)
    return ModelDownloader(tmp_path, server["url"], registry=(model,), **kw)


def _names(path):
    return sorted(p.name for p in Path(path).iterdir())


# ---- registry ----

def test_registry_entries_are_well_formed():
    ids = [m.id for m in REGISTRY]
    assert len(set(ids)) == len(ids)
    for m in REGISTRY:
        assert m.filename == f"ggml-{m.id}.bin"
        assert len(m.sha256) == 64 and int(m.sha256, 16) >= 0
        assert m.bytes > 0 and m.min_memory_gb > 0
    assert {"base.en", "small.en"} <= set(ids)


def test_the_bundled_model_is_the_registry_base_en():
    # build_release.ps1 bundles ggml-base.en.bin: the same file the registry
    # describes, so Settings shows it as installed rather than as Custom
    base = models.get_model("base.en")
    assert base.filename == "ggml-base.en.bin" and base.bytes == 147_964_211
    assert "Comes with Voisr" in base.description


def test_registry_is_frozen():
    with pytest.raises(Exception):
        REGISTRY[0].bytes = 1


def test_benchmarks_are_empty_until_measured():
    assert models.BENCHMARKS == {}
    assert all(r["benchmark"] is None
               for r in models.list_models(Path("/nonexistent"), {}))


def test_downloads_go_to_the_per_user_folder_not_the_bundle():
    from voisr import paths
    assert models.MODELS_DIR == paths.user_models_dir()
    assert models.BUNDLED_DIR == paths.models_dir()


# ---- resolve_model / discovery ----

def test_resolve_model_prefers_configured_then_base_then_small(tmp_path):
    (tmp_path / "ggml-small.en.bin").write_bytes(b"x")
    assert resolve_model("base.en", tmp_path).name == "ggml-small.en.bin"
    (tmp_path / "ggml-base.en.bin").write_bytes(b"x")
    # a downloaded small.en is never picked over the bundled default by itself
    assert resolve_model(None, tmp_path).name == "ggml-base.en.bin"
    assert resolve_model("small.en", tmp_path).name == "ggml-small.en.bin"
    assert resolve_model("no-such-model", tmp_path).name == "ggml-base.en.bin"


def test_resolve_model_knows_registry_and_custom_names(tmp_path):
    (tmp_path / "ggml-medium.en-q5_0.bin").write_bytes(b"x")
    (tmp_path / "ggml-mine.bin").write_bytes(b"x")
    assert resolve_model("medium.en-q5_0", tmp_path).name == "ggml-medium.en-q5_0.bin"
    assert resolve_model("mine", tmp_path).name == "ggml-mine.bin"


def test_resolve_model_with_nothing_installed_names_base(tmp_path):
    assert resolve_model("whatever", tmp_path).name == "ggml-base.en.bin"


def test_resolve_model_finds_downloads_then_the_bundled_default(tmp_path):
    user, bundled = tmp_path / "user", tmp_path / "bundled"
    user.mkdir(); bundled.mkdir()
    (bundled / "ggml-base.en.bin").write_bytes(b"x")
    assert resolve_model(None, user, bundled) == bundled / "ggml-base.en.bin"
    (user / "ggml-small.en.bin").write_bytes(b"x")
    assert resolve_model("small.en", user, bundled) == user / "ggml-small.en.bin"
    assert resolve_model(None, user, bundled) == bundled / "ggml-base.en.bin"
    # missing everywhere: named where the default ships, for the error
    (bundled / "ggml-base.en.bin").unlink()
    (user / "ggml-small.en.bin").unlink()
    assert resolve_model(None, user, bundled) == bundled / "ggml-base.en.bin"


def test_a_downloaded_copy_wins_over_the_bundled_one(tmp_path):
    user, bundled = tmp_path / "user", tmp_path / "bundled"
    user.mkdir(); bundled.mkdir()
    for d in (user, bundled):
        (d / "ggml-base.en.bin").write_bytes(b"x")
    assert models.installed_paths(user, bundled)["base.en"] == user / "ggml-base.en.bin"
    rows = {r["id"]: r for r in models.list_models(user, {}, bundled_dir=bundled)}
    assert rows["base.en"]["installed"] and not rows["base.en"]["bundled"]


def test_bundled_rows_say_so(tmp_path):
    user, bundled = tmp_path / "user", tmp_path / "bundled"
    user.mkdir(); bundled.mkdir()
    (bundled / "ggml-base.en.bin").write_bytes(b"x")
    (user / "ggml-small.en.bin").write_bytes(b"x")
    rows = {r["id"]: r for r in models.list_models(user, {}, bundled_dir=bundled)}
    assert rows["base.en"]["bundled"] and rows["base.en"]["installed"]
    assert rows["small.en"]["installed"] and not rows["small.en"]["bundled"]
    assert not rows["medium.en-q5_0"]["installed"] and not rows["medium.en-q5_0"]["bundled"]


def test_custom_discovery_skips_part_files_and_registry_files(tmp_path):
    (tmp_path / "ggml-mine.bin").write_bytes(b"12345")
    (tmp_path / "ggml-big.bin.part").write_bytes(b"x")
    (tmp_path / "ggml-small.en.bin").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    assert [p.name for p in models.discover_custom(tmp_path)] == ["ggml-mine.bin"]
    rows = {r["id"]: r for r in models.list_models(tmp_path, {})}
    assert rows["mine"]["custom"] and rows["mine"]["installed"]
    assert rows["mine"]["bytes"] == 5
    assert rows["small.en"]["installed"] and not rows["base.en"]["installed"]
    assert "big" not in rows


# ---- PC checks ----

def test_memory_warning_and_nothing_blocked():
    m = Model("a", "e", "A", "d", "ggml-a.bin", 1, "0" * 64, 12, "MIT")
    blocked, warning = models.restriction(m, {"memory_gb": 8})
    assert blocked is None and "this PC has 8 GB" in warning
    assert models.restriction(m, {"memory_gb": 16}) == (None, None)
    assert models.restriction(m, {"memory_gb": None}) == (None, None)


def test_system_info_is_cached_and_survives_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(models, "_total_memory_bytes",
                        lambda: calls.append("mem") or 16 * models.GIB)

    def broken():
        calls.append("cpu")
        raise OSError("no registry")
    monkeypatch.setattr(models, "_cpu_name", broken)
    models.system_info.cache_clear()
    try:
        assert models.system_info() == {"memory_gb": 16.0, "chip": None}
        models.system_info()
        assert calls == ["mem", "cpu"]  # read once
    finally:
        models.system_info.cache_clear()


# ---- disk space ----

def test_disk_check_refuses_with_a_clear_message():
    m = _model()
    msg = models.check_disk_space(m, free_bytes=1000)
    assert "Not enough disk space" in msg and "Fake" in msg
    assert models.check_disk_space(m, free_bytes=10 ** 10) is None


def test_disk_check_counts_what_a_kept_part_already_holds():
    m = _model()
    need = models.DISK_HEADROOM_BYTES + 1000
    assert models.check_disk_space(m, free_bytes=need, have=m.bytes - 1000) is None
    assert models.check_disk_space(m, free_bytes=need) is not None


def test_download_refused_when_it_will_not_fit(tmp_path, server):
    d = _downloader(tmp_path, server, _model(), free_bytes_fn=lambda: 1000)
    res = d.start("fake.en", {})
    assert "Not enough disk space" in res["error"]
    assert list(tmp_path.iterdir()) == []


# ---- downloads ----

def test_download_verifies_and_renames(tmp_path, server):
    d = _downloader(tmp_path, server, _model())
    assert d.start("fake.en", {}) == {"ok": True}
    d.join(10)
    assert d.progress()["state"] == "done"
    assert (tmp_path / "ggml-fake.en.bin").read_bytes() == PAYLOAD
    assert not (tmp_path / "ggml-fake.en.bin.part").exists()
    assert server["ranges"] == [None]


def test_checksum_failure_removes_the_part_and_leaves_no_bin(tmp_path, server):
    d = _downloader(tmp_path, server, _model(sha="0" * 64))
    d.start("fake.en", {})
    d.join(10)
    p = d.progress()
    assert p["state"] == "error" and "Checksum" in p["error"]
    assert list(tmp_path.iterdir()) == []


def test_interrupted_download_leaves_no_bin_and_keeps_its_part(tmp_path, server):
    server["mode"] = "short"
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    d.join(10)
    p = d.progress()
    assert p["state"] == "error" and "Download again" in p["error"]
    assert _names(tmp_path) == ["ggml-fake.en.bin.part"]
    assert (tmp_path / "ggml-fake.en.bin.part").read_bytes() == PAYLOAD[: len(PAYLOAD) // 2]


def test_downloading_again_carries_on_from_the_kept_part(tmp_path, server):
    half = len(PAYLOAD) // 2
    (tmp_path / "ggml-fake.en.bin.part").write_bytes(PAYLOAD[:half])
    d = _downloader(tmp_path, server, _model())
    assert d.progress() == {"state": "idle"}
    d.start("fake.en", {})
    assert d.progress()["done"] == half      # shown at once, not from zero
    d.join(10)
    assert d.progress()["state"] == "done"
    assert server["ranges"] == [f"bytes={half}-"]
    assert (tmp_path / "ggml-fake.en.bin").read_bytes() == PAYLOAD
    assert _names(tmp_path) == ["ggml-fake.en.bin"]


def test_a_server_that_ignores_the_range_starts_over(tmp_path, server):
    (tmp_path / "ggml-fake.en.bin.part").write_bytes(b"garbage")
    server["mode"] = "no-range"
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    d.join(10)
    assert d.progress()["state"] == "done"
    assert (tmp_path / "ggml-fake.en.bin").read_bytes() == PAYLOAD


def test_a_resume_at_the_wrong_offset_is_discarded(tmp_path, server):
    (tmp_path / "ggml-fake.en.bin.part").write_bytes(PAYLOAD[:1000])
    server["mode"] = "lie-range"
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    d.join(10)
    assert d.progress()["state"] == "error"
    assert list(tmp_path.iterdir()) == []


def test_a_corrupt_kept_part_fails_the_checksum_then_starts_clean(tmp_path, server):
    (tmp_path / "ggml-fake.en.bin.part").write_bytes(b"\xff" * 1000)
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    d.join(10)
    assert "Checksum" in d.progress()["error"]
    assert list(tmp_path.iterdir()) == []
    d.start("fake.en", {})
    d.join(10)
    assert (tmp_path / "ggml-fake.en.bin").read_bytes() == PAYLOAD


def test_a_complete_kept_part_is_only_verified(tmp_path, server):
    (tmp_path / "ggml-fake.en.bin.part").write_bytes(PAYLOAD)
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    d.join(10)
    assert d.progress()["state"] == "done" and server["ranges"] == []


def test_an_oversized_kept_part_is_thrown_away(tmp_path, server):
    (tmp_path / "ggml-fake.en.bin.part").write_bytes(PAYLOAD + b"x")
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    d.join(10)
    assert d.progress()["state"] == "done" and server["ranges"] == [None]


def test_an_http_error_discards_the_part(tmp_path, server):
    (tmp_path / "ggml-fake.en.bin.part").write_bytes(PAYLOAD[:1000])
    server["status"] = 404
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    d.join(10)
    assert "404" in d.progress()["error"]
    assert list(tmp_path.iterdir()) == []


def test_another_models_leftover_part_is_removed_at_start(tmp_path, server):
    other = Model("other", "e", "O", "d", "ggml-other.bin", 9, "0" * 64, 4, "MIT")
    (tmp_path / "ggml-other.bin.part").write_bytes(b"x" * 5)
    d = ModelDownloader(tmp_path, server["url"], registry=(_model(), other),
                        free_bytes_fn=lambda: 10 ** 10)
    d.start("fake.en", {})
    d.join(10)
    assert _names(tmp_path) == ["ggml-fake.en.bin"]


def test_wrong_size_is_rejected(tmp_path, server):
    server["payload"] = PAYLOAD + b"extra"
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    d.join(10)
    assert d.progress()["state"] == "error"
    assert list(tmp_path.iterdir()) == []


def test_cancel_mid_download_leaves_no_bin_and_keeps_the_part(tmp_path, server):
    server["mode"] = "slow"
    d = _downloader(tmp_path, server, _model())
    d.start("fake.en", {})
    deadline = time.time() + 5
    while d.progress().get("done", 0) == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert d.active_id() == "fake.en"
    d.cancel()
    assert d.progress()["state"] == "cancelled"
    assert _names(tmp_path) == ["ggml-fake.en.bin.part"]  # Quit doesn't lose it


def test_only_one_download_at_a_time(tmp_path, server):
    server["mode"] = "slow"
    d = _downloader(tmp_path, server, _model())
    assert d.start("fake.en", {}) == {"ok": True}
    assert "already running" in d.start("fake.en", {})["error"]
    d.cancel()


def test_unknown_and_installed_models_are_refused(tmp_path, server):
    d = _downloader(tmp_path, server, _model())
    assert "Unknown" in d.start("nope", {})["error"]
    (tmp_path / "ggml-fake.en.bin").write_bytes(b"x")
    assert "already installed" in d.start("fake.en", {})["error"]


def test_a_model_that_ships_with_voisr_is_already_installed(tmp_path, server):
    user, bundled = tmp_path / "user", tmp_path / "bundled"
    user.mkdir(); bundled.mkdir()
    (bundled / "ggml-fake.en.bin").write_bytes(b"x")
    d = ModelDownloader(user, server["url"], registry=(_model(),),
                        free_bytes_fn=lambda: 10 ** 10, bundled_dir=bundled)
    assert "already installed" in d.start("fake.en", {})["error"]


def test_the_rename_waits_out_a_scanner(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.part", tmp_path / "a.bin"
    src.write_bytes(b"x")
    real = Path.replace
    tries = []

    def flaky(self, target):
        tries.append(1)
        if len(tries) < 3:
            raise PermissionError(32, "in use by another process")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    models._replace(src, dst, delay=0)
    assert dst.read_bytes() == b"x" and len(tries) == 3


def test_delete_file_waits_then_gives_up(tmp_path, monkeypatch):
    path = tmp_path / "m.bin"
    path.write_bytes(b"x")

    def locked(self, missing_ok=False):
        raise PermissionError(32, "in use")
    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(PermissionError):
        models.delete_file(path, tries=3, delay=0)


def test_ssl_context_verifies_with_the_store_and_certifi():
    import ssl
    certifi = pytest.importorskip("certifi")
    ctx = models.ssl_context()
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname
    assert "certifi" in certifi.where()
    assert ctx.cert_store_stats()["x509_ca"] > 0


def test_requirements_pin_certifi():
    reqs = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    assert any(l.startswith("certifi==") for l in reqs.splitlines())
