"""Standalone worker for cross-process HistoryStore tests.

Windows has no fork(), so tests that need independent OS processes writing
to the same history file spawn this script via subprocess instead of the
macOS suite's `multiprocessing.get_context("fork")` helpers.

Usage: python _history_worker.py <repo_root> <history_path> <prefix> <count>
Appends `count` records named "<prefix>-0" .. "<prefix>-<count-1>".
"""
import sys

sys.path.insert(0, sys.argv[1])

from voisr.history import HistoryStore  # noqa: E402

_, _repo_root, _path, _prefix, _count = sys.argv
store = HistoryStore(_path)
for _i in range(int(_count)):
    store.add(f"{_prefix}-{_i}", 1.0)
