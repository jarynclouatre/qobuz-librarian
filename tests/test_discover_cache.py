"""Tests for qobuz_librarian.api.discover_cache - retention, misses, self-heal."""
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


def test_feeds_carry_the_library_they_were_built_from():
    dc.put_feed("similar", [{"name": "Sleep"}], "sig-a")
    saved = dc.get_feed("similar")
    assert saved["payload"] == [{"name": "Sleep"}]
    assert saved["library_sig"] == "sig-a"
    assert saved["built_at"] > 0
    # Rebuilt against a different library, the row is replaced, not added to.
    dc.put_feed("similar", [{"name": "Om"}], "sig-b")
    saved = dc.get_feed("similar")
    assert saved["payload"] == [{"name": "Om"}]
    assert saved["library_sig"] == "sig-b"
    assert dc.get_feed("genres") is None




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


def test_an_unusable_data_dir_is_survived_not_raised(tmp_path, monkeypatch):
    # A missing or read-only data dir must degrade to no caching, because the
    # tab still works without it, only slower.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr(cfg, "DATA_DIR", blocked / "inside")
    dc._reset_for_tests()
    dc.put_lastfm(dc.similar_key("sleep"), [{"name": "Om"}])
    assert dc.get_lastfm(dc.similar_key("sleep"), dc.SIMILAR_TTL) is None
    assert dc.get_feed("similar") is None


def test_trimming_keeps_the_newest_rows():
    # A library that changes over years would otherwise keep a row for every
    # artist it ever held. The rows dropped are the ones a rebuild refetches.
    monkey = dc._MAX_ROWS
    try:
        dc._MAX_ROWS = 3
        for i in range(6):
            key = dc.similar_key(f"artist{i}")
            dc.put_lastfm(key, [{"name": str(i)}])
            # Stamp the age by hand: six writes in the same millisecond would
            # otherwise leave the order the trim depends on undecided.
            dc._conn().execute(
                "UPDATE lastfm SET fetched_at = ? WHERE key = ?", (1000.0 + i, key))
        dc._conn().commit()
        dc._trim()
        kept = [r[0] for r in dc._conn().execute(
            "SELECT key FROM lastfm ORDER BY fetched_at").fetchall()]
        assert kept == [dc.similar_key("artist3"), dc.similar_key("artist4"),
                        dc.similar_key("artist5")]
    finally:
        dc._MAX_ROWS = monkey


def test_unserialisable_payloads_are_dropped_not_raised():
    key = dc.similar_key("sleep")
    dc.put_lastfm(key, {"when": object()})
    assert dc.get_lastfm(key, dc.SIMILAR_TTL) is None
    dc.put_lastfm(key, "a bare string")
    assert dc.get_lastfm(key, dc.SIMILAR_TTL) is None


def test_a_read_error_that_is_not_corruption_leaves_the_cache_alone():
    dc.put_lastfm(dc.similar_key("sleep"), [{"name": "Om"}])
    err = sqlite3.OperationalError("database is locked")
    dc._handle_db_error(err)
    assert dc.get_lastfm(dc.similar_key("sleep"), dc.SIMILAR_TTL) == [{"name": "Om"}]
