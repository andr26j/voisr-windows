from voisr.launch_agent import RUN_KEY, VALUE_NAME, disable, enable, is_enabled


class FakeRegistry:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def read(self, name):
        return self.values.get(name)

    def write(self, name, value):
        self.values[name] = value

    def remove(self, name):
        self.values.pop(name, None)


def test_disabled_when_value_absent():
    assert is_enabled(registry=FakeRegistry()) is False


def test_enabled_when_value_present():
    reg = FakeRegistry({VALUE_NAME: "anything"})
    assert is_enabled(registry=reg) is True


def test_enable_writes_a_value():
    reg = FakeRegistry()
    enable(registry=reg)
    assert VALUE_NAME in reg.values
    assert is_enabled(registry=reg) is True


def test_enable_command_launches_without_a_console_window():
    reg = FakeRegistry()
    enable(registry=reg)
    assert "pythonw" in reg.values[VALUE_NAME].lower()


def test_enable_command_is_quoted_for_paths_with_spaces():
    reg = FakeRegistry()
    enable(registry=reg)
    assert reg.values[VALUE_NAME].startswith('"')


def test_disable_removes_the_value():
    reg = FakeRegistry({VALUE_NAME: "x"})
    disable(registry=reg)
    assert is_enabled(registry=reg) is False


def test_disable_is_idempotent():
    reg = FakeRegistry()
    disable(registry=reg)
    disable(registry=reg)
    assert is_enabled(registry=reg) is False


def test_run_key_is_per_user_not_machine_wide():
    assert "HKEY_CURRENT_USER" in RUN_KEY or "HKCU" in RUN_KEY


def test_enable_launches_the_dashboard_not_the_cli():
    """The Run entry must start voisr.ui. voisr.app is the bare CLI loop with
    no window and no tray, so logging in would start something the user has
    no way to see or quit."""
    reg = FakeRegistry()
    enable(registry=reg)
    assert "-m voisr.ui" in reg.values[VALUE_NAME]
