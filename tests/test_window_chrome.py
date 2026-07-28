from voisr.window_chrome import apply_chrome


class FakeDwm:
    def __init__(self, fail_attributes=()):
        self.calls = []
        self.fail = set(fail_attributes)

    def set_attribute(self, hwnd, attribute, value):
        self.calls.append((attribute, value))
        return 0 if attribute not in self.fail else 1


def test_dark_mode_is_applied():
    dwm = FakeDwm()
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result["dark_titlebar"] is True
    assert (20, 1) in dwm.calls


def test_light_mode_clears_dark_titlebar():
    dwm = FakeDwm()
    apply_chrome(1234, dark=False, dwm=dwm)
    assert (20, 0) in dwm.calls


def test_backdrop_failure_reports_no_vibrancy():
    dwm = FakeDwm(fail_attributes={38})
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result["vibrancy"] is False


def test_backdrop_success_reports_vibrancy():
    result = apply_chrome(1234, dark=True, dwm=FakeDwm())
    assert result["vibrancy"] is True


def test_dark_titlebar_failure_is_reported_not_raised():
    dwm = FakeDwm(fail_attributes={20})
    result = apply_chrome(1234, dark=True, dwm=dwm)
    assert result["dark_titlebar"] is False


def test_exception_never_propagates():
    class Exploding:
        def set_attribute(self, *args):
            raise OSError("dwmapi missing")

    result = apply_chrome(1234, dark=True, dwm=Exploding())
    assert result == {"dark_titlebar": False, "vibrancy": False}
