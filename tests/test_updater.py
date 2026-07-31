import hashlib
import json
import threading

import pytest

import voisr
import voisr.updater as updater
from voisr.updater import (
    MANIFEST_URL,
    Manifest,
    RELEASE_URL_PREFIX,
    UpdateChecker,
    download_to,
    fetch_manifest,
    is_newer,
    parse_version,
    verify_sha256,
)


def test_package_exposes_a_parseable_version():
    assert isinstance(voisr.__version__, str)
    assert parse_version(voisr.__version__) >= (1, 0, 0)


def test_parse_version_rejects_junk():
    for bad in ["", "abc", "1.x.0", "1..0", None]:
        with pytest.raises(ValueError):
            parse_version(bad)


def test_is_newer_compares_numerically_not_as_text():
    # The bug this guards: "1.10.0" < "1.9.0" as strings.
    assert is_newer("1.10.0", "1.9.0")
    assert not is_newer("1.9.0", "1.10.0")


def test_is_newer_is_false_for_equal_and_older():
    assert not is_newer("1.0.0", "1.0.0")
    assert not is_newer("0.9.0", "1.0.0")


def test_is_newer_pads_shorter_versions():
    assert not is_newer("1.1", "1.1.0")
    assert is_newer("1.1.1", "1.1")


GOOD_SHA = "a" * 64


def _manifest_dict(**overrides):
    data = {
        "version": "1.1.0",
        "url": RELEASE_URL_PREFIX + "v1.1.0/VoisrSetup.exe",
        "sha256": GOOD_SHA,
        "notes": "Adds auto-update.",
    }
    data.update(overrides)
    return data


def test_manifest_parses_a_good_payload():
    m = Manifest.from_json(json.dumps(_manifest_dict()))
    assert m.version == "1.1.0"
    assert m.url == RELEASE_URL_PREFIX + "v1.1.0/VoisrSetup.exe"
    assert m.sha256 == GOOD_SHA
    assert m.notes == "Adds auto-update."


def test_manifest_normalises_sha256_to_lowercase():
    # The download step later compares this against hexdigest(), which is
    # always lowercase -- an uppercase manifest digest must still match.
    m = Manifest.from_json(json.dumps(_manifest_dict(sha256=GOOD_SHA.upper())))
    assert m.sha256 == GOOD_SHA


def test_manifest_canonicalises_the_version_string():
    # "v1.1.0" and " 1.1.0 " must collapse to the same stored value as
    # "1.1.0" -- later tasks use this string as both a filename fragment
    # and a cache key, so two spellings of one release must not diverge.
    for raw in ("v1.1.0", " 1.1.0 "):
        m = Manifest.from_json(json.dumps(_manifest_dict(version=raw)))
        assert m.version == "1.1.0"


def test_manifest_requires_every_field():
    for missing in ("version", "url", "sha256", "notes"):
        data = _manifest_dict()
        del data[missing]
        with pytest.raises(ValueError, match=missing):
            Manifest.from_json(json.dumps(data))


def test_manifest_rejects_a_url_outside_the_release_channel():
    # A tampered manifest must not be able to point the installer download
    # at an attacker's host.
    for bad in [
        "https://evil.example.com/VoisrSetup.exe",
        "http://github.com/andr26j/voisr-releases/releases/download/v1/VoisrSetup.exe",
        "https://github.com/andr26j/voisr-releases/../evil/VoisrSetup.exe",
    ]:
        with pytest.raises(ValueError, match="release channel"):
            Manifest.from_json(json.dumps(_manifest_dict(url=bad)))


def test_manifest_rejects_a_percent_encoded_traversal_in_the_url():
    # A plain ".." substring check is blind to percent-encoding -- both of
    # these decode to a path that escapes the release-download prefix.
    for bad in [
        RELEASE_URL_PREFIX + "%2e%2e/%2e%2e/evil/x.exe",
        RELEASE_URL_PREFIX + "%2e%2e%2f%2e%2e%2fevil/x.exe",
    ]:
        with pytest.raises(ValueError, match="release channel"):
            Manifest.from_json(json.dumps(_manifest_dict(url=bad)))


def test_manifest_rejects_control_characters_and_whitespace_in_the_url():
    # http.client raises InvalidURL (not ValueError) for embedded CRLF or
    # whitespace -- these must be rejected here, before that call ever
    # happens, or a bad manifest would escape the ValueError contract.
    for bad in [
        RELEASE_URL_PREFIX + "v1/x.exe https://evil.com/y.exe",
        RELEASE_URL_PREFIX + "v1/x.exe\r\nHost: evil.com",
    ]:
        with pytest.raises(ValueError):
            Manifest.from_json(json.dumps(_manifest_dict(url=bad)))


def test_manifest_rejects_a_malformed_sha256():
    for bad in ["", "abc", "z" * 64, GOOD_SHA[:-1]]:
        with pytest.raises(ValueError, match="64 hex"):
            Manifest.from_json(json.dumps(_manifest_dict(sha256=bad)))


def test_manifest_rejects_an_unparseable_version():
    with pytest.raises(ValueError):
        Manifest.from_json(json.dumps(_manifest_dict(version="next")))


def test_manifest_rejects_a_non_object_payload():
    with pytest.raises(ValueError):
        Manifest.from_json("[]")


def test_manifest_rejects_a_deeply_nested_payload():
    # This is caught by the size cap, not by the RecursionError handler --
    # "[" * 200000 is well over _MAX_MANIFEST_BYTES before json.loads ever
    # runs. Pinning the message keeps the test honest about which guard it
    # actually exercises.
    with pytest.raises(ValueError, match="exceeds maximum size"):
        Manifest.from_json("[" * 200000)


def test_manifest_rejects_a_pre_parsed_payload():
    # from_json's contract is "JSON text in, ValueError on anything bad" --
    # a pre-parsed dict must be rejected up front, not passed through to the
    # str(...) coercions further down, where a hostile or buggy __str__
    # could raise something other than ValueError.
    with pytest.raises(ValueError, match="JSON text"):
        Manifest.from_json({"version": "1.0.0"})


def test_fetch_manifest_returns_a_parsed_manifest():
    captured = {}

    def opener(url):
        captured["url"] = url
        return json.dumps(_manifest_dict())

    m = fetch_manifest("https://example.test/latest.json", opener=opener)
    assert m.version == "1.1.0"
    assert captured["url"] == "https://example.test/latest.json"


def test_fetch_manifest_returns_none_when_there_are_no_releases_yet():
    # A 404 is the normal "nothing published" state, not an error.
    def opener(url):
        raise OSError("HTTP Error 404: Not Found")

    assert fetch_manifest("https://example.test/latest.json", opener=opener) is None


def test_fetch_manifest_returns_none_on_network_failure():
    def opener(url):
        raise TimeoutError("timed out")

    assert fetch_manifest("https://example.test/latest.json", opener=opener) is None


def test_fetch_manifest_returns_none_on_malformed_payload():
    for payload in ["not json", "{}", json.dumps(_manifest_dict(url="https://evil.test/x.exe"))]:
        assert fetch_manifest("https://example.test/latest.json",
                              opener=lambda url, p=payload: p) is None


def test_fetch_manifest_defaults_to_manifest_url():
    # Cheap protection against a future refactor defaulting the wrong
    # constant in (e.g. RELEASE_URL_PREFIX) by copy-paste.
    captured = {}

    def opener(url):
        captured["url"] = url
        return json.dumps(_manifest_dict())

    fetch_manifest(opener=opener)
    assert captured["url"] == MANIFEST_URL


def test_fetch_manifest_tolerates_a_utf8_bom(monkeypatch):
    # PowerShell 5.1's Set-Content -Encoding utf8 writes a BOM, so a real
    # latest.json arrives with one. Without utf-8-sig every published
    # release is silently ignored as malformed. This exercises the real
    # _default_text_opener decode path, not an injected opener, since that
    # is exactly where the bug lived.
    payload = b"\xef\xbb\xbf" + json.dumps(_manifest_dict()).encode("utf-8")

    class FakeResponse:
        def read(self, *args):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        updater.urllib.request, "urlopen",
        lambda url, timeout=None: FakeResponse(),
    )

    m = fetch_manifest("https://example.test/latest.json")
    assert m.version == "1.1.0"


def test_download_writes_the_payload_and_leaves_no_part_file(tmp_path):
    dest = tmp_path / "VoisrSetup-1.1.0.exe"
    download_to("https://example.test/x.exe", dest, opener=lambda url: b"payload")
    assert dest.read_bytes() == b"payload"
    assert list(tmp_path.iterdir()) == [dest]


def test_interrupted_download_leaves_no_partial_file_behind(tmp_path):
    # A half-written installer that looked complete would be executed.
    dest = tmp_path / "VoisrSetup-1.1.0.exe"

    def opener(url):
        raise OSError("connection reset")

    with pytest.raises(OSError):
        download_to("https://example.test/x.exe", dest, opener=opener)
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_creates_the_destination_directory(tmp_path):
    dest = tmp_path / "updates" / "VoisrSetup-1.1.0.exe"
    download_to("https://example.test/x.exe", dest, opener=lambda url: b"x")
    assert dest.is_file()


def test_verify_sha256_accepts_a_matching_digest(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"payload")
    assert verify_sha256(f, hashlib.sha256(b"payload").hexdigest())


def test_verify_sha256_rejects_a_mismatch(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"payload")
    assert not verify_sha256(f, "b" * 64)


def test_verify_sha256_ignores_digest_case_and_whitespace(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    assert verify_sha256(f, f"  {digest.upper()}  ")


def test_verify_sha256_streams_a_file_larger_than_one_chunk(tmp_path):
    # _CHUNK exists so verify_sha256 streams the file rather than holding it
    # in memory whole -- this pins that the digest covers every chunk, not
    # just the first, since a loop that only hashed the first _CHUNK would
    # otherwise pass every other test in this file undetected.
    data = b"x" * (3 * 1024 * 1024) + b"y" * 12345
    f = tmp_path / "a.bin"
    f.write_bytes(data)
    assert verify_sha256(f, hashlib.sha256(data).hexdigest())


def test_download_accepts_an_opener_that_streams_chunks(tmp_path):
    # The real opener yields pieces instead of returning one bytes object
    # (see _default_bytes_opener); download_to must produce the same file
    # either way.
    dest = tmp_path / "VoisrSetup-1.1.0.exe"

    def opener(url):
        yield b"pay"
        yield b"load"

    download_to("https://example.test/x.exe", dest, opener=opener)
    assert dest.read_bytes() == b"payload"


def test_download_overwrites_an_existing_destination(tmp_path):
    dest = tmp_path / "VoisrSetup-1.1.0.exe"
    dest.write_bytes(b"stale installer from a previous version")
    download_to("https://example.test/x.exe", dest, opener=lambda url: b"new payload")
    assert dest.read_bytes() == b"new payload"


def test_download_succeeds_despite_a_stale_part_file_from_a_crashed_run(tmp_path):
    dest = tmp_path / "VoisrSetup-1.1.0.exe"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"leftover from a process that crashed mid-download")
    download_to("https://example.test/x.exe", dest, opener=lambda url: b"payload")
    assert dest.read_bytes() == b"payload"
    assert list(tmp_path.iterdir()) == [dest]


def test_keyboard_interrupt_during_download_still_cleans_up_the_part_file(tmp_path):
    dest = tmp_path / "VoisrSetup-1.1.0.exe"

    def opener(url):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        download_to("https://example.test/x.exe", dest, opener=opener)
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_a_body_over_the_size_ceiling(tmp_path):
    # Small injected ceiling stands in for _MAX_INSTALLER_BYTES so the test
    # doesn't need to write hundreds of megabytes to exercise the check.
    dest = tmp_path / "VoisrSetup-1.1.0.exe"

    def opener(url):
        yield b"x" * 10

    with pytest.raises(OSError):
        download_to("https://example.test/x.exe", dest, opener=opener, max_bytes=5)
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_default_opener_reports_a_content_length_mismatch_as_incomplete(monkeypatch, tmp_path):
    # A dropped connection truncates the body without raising. Unchecked,
    # that would surface later only as a sha256 mismatch -- which tells the
    # user their download may have been tampered with, when their
    # connection merely dropped. This exercises the real
    # _default_bytes_opener, not an injected opener, since only it sees the
    # Content-Length header.
    remaining = [b"short"]

    class FakeHeaders:
        def get(self, name, default=None):
            return "100" if name == "Content-Length" else default

    class FakeResponse:
        headers = FakeHeaders()

        def read(self, size=None):
            chunk, remaining[0] = remaining[0][:size], remaining[0][size:]
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        updater.urllib.request, "urlopen",
        lambda url, timeout=None: FakeResponse(),
    )

    dest = tmp_path / "VoisrSetup-1.1.0.exe"
    with pytest.raises(OSError, match="incomplete"):
        download_to("https://example.test/x.exe", dest)
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def _checker(tmp_path, *, manifest=None, enabled=True, payload=b"installer",
             sha=None, spawned=None, current="1.0.0"):
    """An UpdateChecker with every collaborator faked: no network, no spawn."""
    if manifest is None:
        manifest = Manifest.from_json(json.dumps(_manifest_dict(
            sha256=hashlib.sha256(payload).hexdigest())))
    if sha is not None:
        manifest = Manifest(manifest.version, manifest.url, sha, manifest.notes)
    calls = {"fetch": 0, "download": 0}

    def fake_fetch(url, opener=None):
        calls["fetch"] += 1
        return manifest

    def fake_download(url, dest, opener=None):
        calls["download"] += 1
        return download_to(url, dest, opener=lambda _u: payload)

    checker = UpdateChecker(
        current_version=current,
        updates_dir=tmp_path,
        config_reader=lambda: {"check_for_updates": enabled},
        fetch=fake_fetch,
        download=fake_download,
        spawn=(spawned.append if spawned is not None else (lambda argv: None)),
    )
    return checker, calls


def test_a_newer_release_ends_in_ready_with_the_installer_on_disk(tmp_path):
    checker, calls = _checker(tmp_path)
    result = checker.check_now()
    assert result["state"] == "ready"
    assert result["version"] == "1.1.0"
    assert checker.status()["notes"] == "Adds auto-update."
    assert (tmp_path / "VoisrSetup-1.1.0.exe").is_file()
    assert calls["download"] == 1


def test_the_same_version_is_not_an_update(tmp_path):
    checker, calls = _checker(tmp_path, current="1.1.0")
    result = checker.check_now()
    assert result["state"] == "idle"
    assert result["up_to_date"] is True
    assert calls["download"] == 0


def test_no_releases_yet_is_not_an_error(tmp_path):
    checker, _ = _checker(tmp_path)
    checker._fetch = lambda url, opener=None: None
    result = checker.check_now()
    assert result["state"] == "idle"
    assert result["up_to_date"] is True


def test_a_hash_mismatch_deletes_the_download_and_never_offers_it(tmp_path):
    checker, _ = _checker(tmp_path, sha="b" * 64)
    result = checker.check_now()
    assert result["state"] == "idle"
    assert "checksum" in result["error"].lower()
    assert list(tmp_path.iterdir()) == []
    assert checker.status()["state"] == "idle"


def test_the_toggle_being_off_means_no_network_call_at_all(tmp_path):
    checker, calls = _checker(tmp_path, enabled=False)
    result = checker.check_now()
    assert result["state"] == "idle"
    assert calls["fetch"] == 0


def test_check_now_forced_ignores_the_toggle(tmp_path):
    # The Settings "Check now" button must work even with auto-check off.
    checker, calls = _checker(tmp_path, enabled=False)
    result = checker.check_now(force=True)
    assert result["state"] == "ready"
    assert calls["fetch"] == 1


def test_an_already_downloaded_installer_is_not_fetched_twice(tmp_path):
    checker, calls = _checker(tmp_path)
    checker.check_now()
    checker.check_now()
    assert calls["download"] == 1


def test_stale_installers_for_other_versions_are_pruned(tmp_path):
    (tmp_path / "VoisrSetup-0.9.0.exe").write_bytes(b"old")
    checker, _ = _checker(tmp_path)
    checker.check_now()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["VoisrSetup-1.1.0.exe"]


def test_install_spawns_the_installer_silently_and_relaunches(tmp_path):
    spawned = []
    checker, _ = _checker(tmp_path, spawned=spawned)
    checker.check_now()
    result = checker.install()
    assert result["ok"] is True
    assert checker.status()["state"] == "installing"
    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[0].endswith("VoisrSetup-1.1.0.exe")
    # /RELAUNCH=1 is required: the installer's [Run] entry is skipifsilent,
    # so without it a silent update would leave Voisr closed.
    assert argv[1:] == ["/SILENT", "/NORESTART", "/RELAUNCH=1"]


def test_install_before_an_update_is_ready_is_refused(tmp_path):
    spawned = []
    checker, _ = _checker(tmp_path, spawned=spawned)
    result = checker.install()
    assert "error" in result
    assert spawned == []


def test_a_failed_spawn_is_reported_and_leaves_the_app_running(tmp_path):
    def boom(argv):
        raise OSError("access denied")

    checker, _ = _checker(tmp_path)
    checker._spawn = boom
    checker.check_now()
    result = checker.install()
    assert "error" in result
    assert checker.status()["state"] == "ready"


def _fake_timers(monkeypatch):
    """Record every armed timer instead of starting one. Scheduling tests must
    never wait out STARTUP_DELAY_S or leave a live thread behind."""
    armed = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.cancelled = delay, fn, False
            armed.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(updater.threading, "Timer", FakeTimer)
    return armed


def test_stop_is_not_undone_by_a_check_that_was_already_running(tmp_path,
                                                                monkeypatch):
    # _tick re-arms after every check, so a stop() landing while a check is
    # in flight would otherwise resurrect the schedule -- a leaked timer
    # thread every time Settings toggles auto-check off and on again.
    armed = _fake_timers(monkeypatch)

    checker, _ = _checker(tmp_path)
    checker.start()
    assert [t.delay for t in armed] == [UpdateChecker.STARTUP_DELAY_S]

    checker.stop()
    assert armed[0].cancelled

    checker._tick()  # the in-flight check finishing after stop()
    assert len(armed) == 1


def test_arming_a_second_time_cancels_the_timer_it_replaces(tmp_path,
                                                            monkeypatch):
    # An orphaned timer is worse than a leaked thread: untracked, so stop()
    # cannot cancel it, it would go on to run a real check and download after
    # the user switched auto-update off.
    armed = _fake_timers(monkeypatch)

    checker, _ = _checker(tmp_path)
    checker.start()
    checker.start()
    checker.stop()

    assert len(armed) == 2
    assert all(t.cancelled for t in armed)


def test_a_second_check_will_not_start_while_one_is_already_running(tmp_path):
    # Single-flight. Two checks would stream into the same .part file, and the
    # loser's failed rename would report a spurious error over the winner's
    # freshly ready update. Gated on Events, so there is nothing to wait for
    # and no sleep: the second call must return while the first is parked.
    started, release = threading.Event(), threading.Event()
    checker, calls = _checker(tmp_path)
    counted_fetch = checker._fetch

    def gated_fetch(url, opener=None):
        manifest = counted_fetch(url)
        started.set()
        release.wait(10)
        return manifest

    checker._fetch = gated_fetch
    worker = threading.Thread(target=checker.check_now)
    worker.start()
    try:
        assert started.wait(10)
        result = checker.check_now(force=True)
        assert result["already_checking"] is True
        assert result["state"] == "checking"
        assert calls["fetch"] == 1  # the second call never reached the network
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert calls["fetch"] == 1
    assert checker.status()["state"] == "ready"


def test_a_check_cannot_restart_the_machine_during_an_install(tmp_path):
    # _check's first act is _set("checking"), which would overwrite
    # "installing" -- and a state back at "ready" lets install() spawn the
    # same installer a second time, two silent Inno installs writing into
    # {app} at once.
    spawned = []
    checker, calls = _checker(tmp_path, spawned=spawned)
    checker.check_now()
    checker.install()

    result = checker.check_now(force=True)
    assert result["state"] == "installing"
    assert calls["fetch"] == 1  # the check was refused, not merely ignored
    assert checker.status()["state"] == "installing"

    assert "error" in checker.install()
    assert len(spawned) == 1


def test_an_unexpected_failure_mid_check_still_lands_in_idle(tmp_path):
    # _check guards the download itself; this pins the outer net. Without it a
    # collaborator raising before that point parks the machine in "checking"
    # with no error until the next 24h tick.
    checker, _ = _checker(tmp_path)

    def boom(url, opener=None):
        raise RuntimeError("collaborator exploded")

    checker._fetch = boom
    result = checker.check_now()
    assert result["state"] == "idle"
    assert "collaborator exploded" in result["error"]


def test_the_installer_is_spawned_detached_and_outside_the_app_directory(
        tmp_path, monkeypatch):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"], captured["kwargs"] = argv, kwargs
        return None

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    exe = tmp_path / "VoisrSetup-1.1.0.exe"
    updater._default_spawn([str(exe), "/SILENT"])

    assert captured["argv"] == [str(exe), "/SILENT"]
    flags = captured["kwargs"]["creationflags"]
    assert flags & updater.subprocess.DETACHED_PROCESS
    assert flags & updater.subprocess.CREATE_NEW_PROCESS_GROUP
    # Not Voisr's own directory: an inherited cwd of {app} would be an open
    # handle on the tree the installer is about to replace.
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_a_stale_installer_is_dropped_once_there_is_nothing_to_offer(tmp_path):
    # The path a freshly updated app takes on its first check. Without the
    # prune the ~155 MB installer that was just applied sits there forever.
    (tmp_path / "VoisrSetup-1.1.0.exe").write_bytes(b"already applied")
    checker, _ = _checker(tmp_path, current="1.1.0")
    checker.check_now()
    assert list(tmp_path.iterdir()) == []


def test_a_download_that_cannot_be_reached_still_clears_the_old_installer(
        tmp_path):
    # Pruning only on success leaves ~155 MB of superseded installer behind on
    # every failure path.
    (tmp_path / "VoisrSetup-0.9.0.exe").write_bytes(b"superseded")
    checker, _ = _checker(tmp_path)

    def boom(url, dest, opener=None):
        raise OSError("connection reset")

    checker._download = boom
    result = checker.check_now()
    assert result["state"] == "idle"
    assert list(tmp_path.iterdir()) == []


def test_an_offline_check_keeps_a_download_that_is_already_on_disk(tmp_path):
    # The counterpart to the prune above: a moment offline must not throw away
    # a good installer and make the next check re-burn 155 MB.
    stale = tmp_path / "VoisrSetup-1.1.0.exe"
    stale.write_bytes(b"installer")
    checker, _ = _checker(tmp_path)
    checker._fetch = lambda url, opener=None: None
    checker.check_now()
    assert stale.is_file()
