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


def test_lastfm_rows_round_trip_and_expire():
    key = dc.similar_key("electricwizard")
    dc.put_lastfm(key, [{"name": "Sleep", "match": 0.9}])
    assert dc.get_lastfm(key, dc.SIMILAR_TTL) == [{"name": "Sleep", "match": 0.9}]
    # Past its TTL the row is not served...
    assert dc.get_lastfm(key, -1) is None
    # ...unless the caller asks for it anyway, which is what happens when
    # Last.fm can't be reached to fetch a fresh one.
    assert dc.get_lastfm(key, -1, allow_stale=True) == [
        {"name": "Sleep", "match": 0.9}]


def test_a_cached_miss_is_distinguishable_from_never_asked():
    # The difference decides whether the builder spends a Qobuz search on this
    # name again, so it has to survive the round trip.
    key = dc.artist_resolution_key("bandthatisnotonqobuz")
    dc.put_resolution_miss(key)
    saved = dc.get_resolution(key)
    assert dc.is_miss(saved)
    assert not dc.is_miss(dc.get_resolution(dc.artist_resolution_key("other")))
    dc.put_resolution(dc.artist_resolution_key("sleep"), {"id": "123"})
    assert not dc.is_miss(dc.get_resolution(dc.artist_resolution_key("sleep")))


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
