"""The Dictionary and Snippets pages call these by the Mac's names and
signatures. They join BRIDGE_METHODS with the pages' JS, which pins that
tuple to what the page calls (tests/test_js_bridge_surface.py)."""
import inspect

import pytest

from voisr.ui import Api

PAGE_METHODS = {
    "get_suggestions": [],
    "accept_suggestion": ["pair_id"],
    "dismiss_suggestion": ["pair_id"],
    "get_snippets": [],
    "save_snippet": ["old_trigger", "trigger", "text"],
    "delete_snippet": ["trigger"],
}


@pytest.mark.parametrize("name, params", PAGE_METHODS.items())
def test_the_pages_methods_have_the_macs_names_and_signatures(name, params):
    method = getattr(Api, name)
    assert not name.startswith("_") and callable(method)
    assert list(inspect.signature(method).parameters)[1:] == params
