import json

import pytest

import voisr
from voisr.updater import Manifest, RELEASE_URL_PREFIX, is_newer, parse_version


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
    assert m.sha256 == GOOD_SHA
    assert m.notes == "Adds auto-update."


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


def test_manifest_rejects_a_malformed_sha256():
    for bad in ["", "abc", "z" * 64, GOOD_SHA[:-1]]:
        with pytest.raises(ValueError):
            Manifest.from_json(json.dumps(_manifest_dict(sha256=bad)))


def test_manifest_rejects_an_unparseable_version():
    with pytest.raises(ValueError):
        Manifest.from_json(json.dumps(_manifest_dict(version="next")))


def test_manifest_rejects_a_non_object_payload():
    with pytest.raises(ValueError):
        Manifest.from_json("[]")
