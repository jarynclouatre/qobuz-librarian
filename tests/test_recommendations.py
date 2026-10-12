"""Discover recommendation tests."""

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.api import discover_cache as dc
from qobuz_librarian.library import discovery
from qobuz_librarian.library import recommendations as rec


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    dc._reset_for_tests()
    rec._reset_for_tests()
    yield
    dc._reset_for_tests()
    rec._reset_for_tests()


def _library(names):
    """A Library built from folder names, without touching a disk."""
    return rec.Library(
        keys={k for k in (rec.normalize(n) for n in names) if k},
        raws=list(names),
        seeds=list(names),
        signature="sig",
    )


def test_owned_artists_are_recognised_through_spelling_variants():
    owned = _library(["The Beatles", "Sigur Rós"])
    for name in ("Beatles", "Sigur Ros"):
        assert owned.owns(name), name
    # Ranking drops an owned artist under either spelling.
    ranked = rec.rank_candidates({"Sigur Ros": {"score": 5.0, "seeds": [("Mogwai", 1.0)]},
                                  "Om": {"score": 0.3, "seeds": [("Mogwai", 0.3)]}}, owned)
    assert [c["name"] for c in ranked] == ["Om"]


def test_a_shared_name_goes_to_the_artist_whose_albums_lastfm_knows(monkeypatch):
    # Qobuz files a sleep-sounds catalogue and the band under the same name,
    # and the deeper catalogue is not the band.
    monkeypatch.setattr(rec, "search_artists", lambda name, token, limit: [
        {"id": 1, "name": "Sleep", "albums_count": 310},
        {"id": 2, "name": "Sleep", "albums_count": 14},
    ])
    catalogs = {
        1: [{"title": "Bird Songs for Babies Deep Sleep"}],
        2: [{"title": "Dopesmoker"}, {"title": "The Sciences"}],
    }
    monkeypatch.setattr(discovery, "get_artist_albums",
                        lambda artist_id, token, limit=None: (catalogs[artist_id], None))
    monkeypatch.setattr(rec, "get_artist_top_albums", lambda name, limit: [
        {"artist": "Sleep", "title": "Sleep's Holy Mountain"},
        {"artist": "Sleep", "title": "Dopesmoker"},
    ])
    assert rec.resolve_artist("Sleep", "token")["id"] == "2"


def test_genre_chips_read_artists_from_across_the_library(monkeypatch):
    names = sorted(f"{letter} Band {n}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                   for n in range(20))
    asked = []
    monkeypatch.setattr(rec, "get_artist_top_tags",
                        lambda name: asked.append(name) or [])
    rec._tags_worker(rec.TAGS, None, _library(names))
    assert len(asked) == rec._TAG_SAMPLE
    assert {names.index(name) * 4 // len(names) for name in asked} == {0, 1, 2, 3}
