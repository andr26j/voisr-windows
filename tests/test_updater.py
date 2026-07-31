import pytest

import voisr
from voisr.updater import is_newer, parse_version


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
