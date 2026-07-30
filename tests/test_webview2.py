from voisr.webview2 import DOWNLOAD_URL, is_runtime_installed


class FakeRegistry:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def read(self, key):
        return self.values.get(key)


def test_absent_runtime_is_detected():
    assert is_runtime_installed(registry=FakeRegistry()) is False


def test_present_runtime_is_detected():
    reg = FakeRegistry({"pv": "150.0.4078.99"})
    assert is_runtime_installed(registry=reg) is True


def test_empty_version_counts_as_absent():
    """A present key with an empty pv is how an uninstalled runtime can
    look; treating that as installed would give a blank window."""
    assert is_runtime_installed(registry=FakeRegistry({"pv": ""})) is False


def test_zero_version_counts_as_absent():
    assert is_runtime_installed(registry=FakeRegistry({"pv": "0.0.0.0"})) is False


def test_registry_failure_assumes_installed():
    """Unknown must not block launch: a false negative would refuse to
    start on a machine where the runtime is fine."""
    class Exploding:
        def read(self, key):
            raise OSError("no registry")

    assert is_runtime_installed(registry=Exploding()) is True


def test_download_url_is_the_official_evergreen_bootstrapper():
    assert "go.microsoft.com" in DOWNLOAD_URL or "developer.microsoft.com" in DOWNLOAD_URL
