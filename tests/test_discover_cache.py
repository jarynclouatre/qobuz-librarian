"""Discover cache tests."""
import sqlite3

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
