"""First run: who sees it, and what remembers it.

Windows has no TCC prompt to hold back, so unlike the Mac this is only the
decision and its memory; the page (js/views/first-run.js) does the rest."""
import json

from voisr.history import HistoryStore
from voisr.ui import BRIDGE_METHODS, Api


def _api(tmp_path, **kwargs):
    return Api(config_path=tmp_path / "config.json",
               history=HistoryStore(tmp_path / "data" / "history.jsonl"), **kwargs)


def _saved(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


def test_a_fresh_install_needs_it_and_says_so_on_disk(tmp_path):
    api = _api(tmp_path)
    assert api.first_run_pending() is True
    assert _saved(tmp_path)["first_run_done"] is False


def test_quitting_midway_cant_make_it_look_like_an_existing_install(tmp_path):
    api = _api(tmp_path)
    api.first_run_pending()
    api.set_config({"hotkey": "alt_r"})  # what step 2 can write
    assert _api(tmp_path).first_run_pending() is True


def test_an_existing_config_skips_it_and_stays_unwritten(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hotkey": "ctrl_r"}))
    before = path.read_bytes()
    assert _api(tmp_path).first_run_pending() is False
    assert path.read_bytes() == before


def test_history_with_no_config_skips_it_and_writes_nothing(tmp_path):
    # someone who dictated for months and never changed a setting has no config
    history = tmp_path / "data" / "history.jsonl"
    history.parent.mkdir()
    history.write_text('{"id": "a", "text": "hello"}\n')
    assert _api(tmp_path).first_run_pending() is False
    assert not (tmp_path / "config.json").exists()


def test_finishing_is_remembered(tmp_path):
    api = _api(tmp_path)
    api.first_run_pending()
    assert "error" not in api.set_config({"first_run_done": True})
    assert api.get_first_run() == {"pending": False}
    assert _api(tmp_path).first_run_pending() is False


def test_a_bad_value_cant_be_saved_or_reopen_it(tmp_path):
    api = _api(tmp_path)
    assert "error" in api.set_config({"first_run_done": "yes"})
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"first_run_done": "yes"}))
    assert _api(tmp_path).first_run_pending() is False  # dropped, so it's an existing file


def test_the_key_stays_out_of_the_defaults():
    # in DEFAULT_CONFIG, every existing install would read as not finished
    from voisr.app import DEFAULT_CONFIG
    assert "first_run_done" not in DEFAULT_CONFIG


def test_an_unwritable_config_still_shows_first_run(tmp_path, monkeypatch):
    api = _api(tmp_path)

    def refuse(cfg):
        raise OSError("read-only")
    monkeypatch.setattr(api, "_write_config_atomic", refuse)
    assert api.first_run_pending() is True


def test_the_page_can_ask(tmp_path):
    assert "get_first_run" in BRIDGE_METHODS
    assert _api(tmp_path).get_first_run() == {"pending": True}


def test_dismissed_banners_are_saved_by_name_and_nothing_else(tmp_path):
    api = _api(tmp_path)
    assert api.get_config()["dismissed_banners"] == []
    assert "error" not in api.set_config({"dismissed_banners": ["dictionary", "snippets"]})
    assert _saved(tmp_path)["dismissed_banners"] == ["dictionary", "snippets"]
    for bad in ("dictionary", [1], None, {"dictionary": True}):
        assert "error" in api.set_config({"dismissed_banners": bad}), bad
