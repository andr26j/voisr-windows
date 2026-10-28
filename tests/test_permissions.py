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


def _switches(machine, user, desktop):
    """A fake ConsentStore reader: the device-wide switch (HKLM), "let apps
    access" (HKCU) and "let desktop apps access" (HKCU NonPackaged)."""
    import winreg

    def read(hive, subkey):
        if hive == winreg.HKEY_LOCAL_MACHINE:
            return machine
        return desktop if subkey.endswith("NonPackaged") else user
    return read


def test_microphone_switch_is_on_when_every_switch_allows():
    assert permissions.microphone_switch(_switches("Allow", "Allow", "Allow")) is True


def test_any_switch_off_means_the_microphone_is_off():
    for values in (("Deny", "Allow", "Allow"), ("Allow", "Deny", "Allow"),
                   ("Allow", "Allow", "Deny")):
        assert permissions.microphone_switch(_switches(*values)) is False, values


def test_a_missing_switch_is_unknown_never_on():
    assert permissions.microphone_switch(_switches("Allow", None, "Allow")) is None


def test_a_switched_off_microphone_is_a_known_denial_without_opening_a_stream(monkeypatch):
    monkeypatch.setattr(permissions, "microphone_switch", lambda: False)
    opened = []
    monkeypatch.setattr(permissions, "microphone_granted", lambda: opened.append(1) or True)
    assert permissions.microphone_state() is False
    assert opened == []
