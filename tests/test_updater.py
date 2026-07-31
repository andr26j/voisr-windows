import json

import pytest

import voisr
import voisr.updater as updater
from voisr.updater import (
    MANIFEST_URL,
    Manifest,
    RELEASE_URL_PREFIX,
    fetch_manifest,
    is_newer,
    parse_version,
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
