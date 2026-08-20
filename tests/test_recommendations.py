"""Discover recommendation tests."""
import time
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.api import discover_cache as dc
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
    owned = _library(["The Beatles", "Sigur Rós", "Godspeed You! Black Emperor",
                      "Death Cab for Cutie"])
    for name in ("The Beatles", "the beatles", "Beatles", "BEATLES",
                 "Sigur Ros", "sigur rós",
                 "Godspeed You Black Emperor",
                 "Death Cab For Cutie"):
        assert owned.owns(name), name


def test_ranking_drops_what_is_already_owned():
    accumulated = {
        "Sleep": {"score": 5.0, "seeds": [("Electric Wizard", 1.0)]},
        "Om": {"score": 0.3, "seeds": [("Electric Wizard", 0.3)]},
    }
    ranked = rec.rank_candidates(accumulated, _library(["Sleep"]))
    assert [c["name"] for c in ranked] == ["Om"]


def test_a_saved_feed_is_retired_when_the_library_changes():
    dc.put_feed(rec.SIMILAR, [{"name": "Om"}], "sig-old")
    ready = rec.feed_view(rec.SIMILAR, "sig-old")
    assert ready["phase"] == "ready"
    assert ready["items"] == [{"name": "Om"}]
    # A download has landed since; the suggestions were computed against a
    # library that no longer exists, so they are not offered as current.
    stale = rec.feed_view(rec.SIMILAR, "sig-new")
    assert stale["phase"] == "idle"
    assert stale["items"] == []


def test_a_failed_build_falls_back_to_the_saved_copy():
    # Last.fm being unreachable shows last time's suggestions, marked as such,
    # rather than an empty page.
    dc.put_feed(rec.SIMILAR, [{"name": "Saved"}], "sig-old")
    rec._builds[rec.SIMILAR] = rec._new_build()
    rec._publish(rec.SIMILAR, phase="error", error="unavailable")
    view = rec.feed_view(rec.SIMILAR, "sig-new")
    assert view["phase"] == "error"
    assert view["error"] == "unavailable"
    assert view["items"] == [{"name": "Saved"}]
    assert view["stale"] is True


def test_opening_the_page_again_does_not_start_a_second_build():
    # Every poll calls through here; a build per poll would flood Last.fm.
    import threading

    release = threading.Event()
    started = []

    def worker(kind):
        started.append(kind)
        release.wait(5)
        rec._publish(kind, phase="ready")

    rec.start_build("t", worker)
    rec.start_build("t", worker)
    rec.start_build("t", worker)
    assert rec.build_status("t")["phase"] == "building"
    release.set()
    assert started == ["t"]


def test_a_qobuz_outage_is_never_written_down_as_no_such_artist(monkeypatch):
    # A cached miss stops the name being searched for again for a month. Only
    # a real "no such artist" earns one.
    from qobuz_librarian.api.auth import QobuzError

    def blew_up(query, token, limit=None):
        raise QobuzError("HTTP 500 from artist/search")

    monkeypatch.setattr(rec, "search_artists", blew_up)
    assert rec.resolve_artist("Om", "tok") is None
    key = dc.artist_resolution_key(rec.normalize("Om"))
    assert dc.get_resolution(key) is None

    monkeypatch.setattr(rec, "search_artists", lambda q, t, limit=None: [])
    assert rec.resolve_artist("Om", "tok") is None
    assert dc.is_miss(dc.get_resolution(key))


def test_the_library_picture_prefers_the_name_qobuz_uses(monkeypatch):
    # Last.fm spells artists the way Qobuz does far more often than the way a
    # folder on disk does, so the resolved name is what gets asked about - and
    # both names are recognised coming back.
    monkeypatch.setattr(rec, "list_library_artists",
                        lambda: [Path("/music/Beatles, The"), Path("/music/Sleep")])
    monkeypatch.setattr(rec, "cached_artist_resolutions",
                        lambda: {"Beatles, The": ["123", "The Beatles"]})
    owned = rec.read_library()
    assert "The Beatles" in owned.seeds
    assert "Sleep" in owned.seeds
    assert owned.owns("The Beatles")
    assert owned.owns("Beatles, The")
    assert owned.signature


def test_a_failed_build_is_left_alone_before_anything_retries_it():
    # Without this, every reopened page relaunched a build against the same
    # dead key, which is both useless and the fastest way to earn a rate limit.
    starts = []

    def worker(kind):
        starts.append(kind)
        raise RuntimeError("no")

    rec.start_build("t", worker)
    for _ in range(500):
        if rec.build_status("t")["phase"] == "error":
            break
        time.sleep(0.002)
    rec.start_build("t", worker)
    rec.start_build("t", worker)
    assert starts == ["t"]
    # Once the cooldown has passed it tries again.
    rec._builds["t"]["finished_at"] -= rec._RETRY_AFTER_ERROR + 1
    rec.start_build("t", worker)
    for _ in range(500):
        if len(starts) > 1:
            break
        time.sleep(0.002)
    assert starts == ["t", "t"]
