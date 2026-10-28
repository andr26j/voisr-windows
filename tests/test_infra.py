"""M4: the repo must be reproducible and runnable without a microphone."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_requirement_is_pinned():
    lines = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    assert lines, "requirements.txt is empty"
    unpinned = [l for l in lines if not re.match(r"^[A-Za-z0-9_.\-\[\]]+==\S+$", l)]
    assert unpinned == [], f"unpinned: {unpinned}"


# ---- L9 / L10 (2026-10-12 audit) ----

def test_an_api_built_in_a_test_cannot_paste_into_the_users_window(tmp_path):
    # an Api left on the real defaults pasted its fake transcript ("Hello",
    # "X") into whatever window had focus each time the suite ran
    from voisr.history import HistoryStore
    from voisr.inject import paste_at_cursor, set_clipboard
    from voisr.ui import Api

    api = Api(config_path=tmp_path / "config.json",
              history=HistoryStore(tmp_path / "h.jsonl"))
    assert api._paste_fn is not paste_at_cursor
    assert api._clipboard_fn is not set_clipboard
