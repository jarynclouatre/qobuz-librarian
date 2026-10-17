"""Discover cache tests."""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.api import discover_cache as dc


@pytest.fixture(autouse=True)
def _own_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    dc._reset_for_tests()
    yield
    dc._reset_for_tests()


def test_a_corrupt_database_is_discarded_and_rebuilt():
    # An unclean container or NAS power-off leaves page corruption that only
    # surfaces on a row access. Discover must lose the cache, not the tab.
    key = dc.similar_key("sleep")
    dc.put_lastfm(key, [{"name": "Om", "match": 0.8}])
    dc._reset_for_tests()
    db = cfg.DATA_DIR / "discover_cache.db"
    db.write_bytes(b"this is not a database" * 100)
    assert dc.get_lastfm(key, dc.SIMILAR_TTL) is None
    dc.put_lastfm(key, [{"name": "Sleep", "match": 0.7}])
    assert dc.get_lastfm(key, dc.SIMILAR_TTL) == [{"name": "Sleep", "match": 0.7}]


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_finished_pool_workers_leave_no_cache_files_open():
    # Every scan starts a new worker pool, and the web process runs for
    # weeks: a finished worker's connection must not wait for a collection.
    key = dc.similar_key("sleep")
    for _ in range(3):
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda _: dc.get_lastfm(key, dc.SIMILAR_TTL), range(8)))
    db = str(cfg.DATA_DIR / "discover_cache.db")
    open_files = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            open_files.append(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            pass
    assert not [f for f in open_files if f.startswith(db)]
