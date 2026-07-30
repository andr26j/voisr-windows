import voisr.permissions as permissions
from voisr.permissions import open_pane, settings_url, states


def test_states_reports_only_microphone():
    result = states(probes={"microphone": lambda: True})
    assert [p["key"] for p in result] == ["microphone"]


def test_granted_probe_is_reported_granted():
    assert states(probes={"microphone": lambda: True})[0]["granted"] is True


def test_denied_probe_is_reported_denied():
    assert states(probes={"microphone": lambda: False})[0]["granted"] is False


def test_probe_exception_is_unknown_never_granted():
    def explode():
        raise OSError("no device")

    assert states(probes={"microphone": explode})[0]["granted"] is None


def test_states_includes_a_settings_url():
    assert states(probes={"microphone": lambda: True})[0]["settings_url"] \
        == "ms-settings:privacy-microphone"


def test_settings_url_for_unknown_key_is_none():
    assert settings_url("accessibility") is None


def test_open_pane_rejects_unknown_key():
    assert open_pane("../../evil") is False


def test_open_pane_opens_known_key():
    opened = []
    assert open_pane("microphone", opener=opened.append) is True
    assert opened == ["ms-settings:privacy-microphone"]


def test_open_pane_survives_opener_failure():
    def explode(url):
        raise OSError("no shell")

    assert open_pane("microphone", opener=explode) is False
