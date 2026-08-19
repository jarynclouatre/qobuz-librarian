"""Tests for qobuz_librarian.library.recommendations - what gets suggested."""
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
    # Suggesting a record already on the shelf is the embarrassing failure, so
    # the check has to survive articles, case, punctuation and accents.
    owned = _library(["The Beatles", "Sigur Rós", "Godspeed You! Black Emperor",
                      "Death Cab for Cutie"])
    for name in ("The Beatles", "the beatles", "Beatles", "BEATLES",
                 "Sigur Ros", "sigur rós",
                 "Godspeed You Black Emperor",
                 "Death Cab For Cutie"):
        assert owned.owns(name), name



def test_a_name_that_normalizes_to_nothing_falls_back_to_exact_text():
    # Pure CJK names strip to an empty string, and comparing empty to empty
    # would match everything in the library.
    owned = rec.Library(keys=set(), raws=["椎名林檎", "相対性理論"],
                        seeds=["椎名林檎"], signature="sig")
    assert owned.owns("椎名林檎")
    assert not owned.owns("宇多田ヒカル")


def test_three_quiet_agreements_outrank_one_loud_one():
    # "Similar to my library" is not "similar to one artist in it": an artist
    # three of your records point at beats one a single record insists on.
    accumulated = {
        "Broad Agreement": {"score": 0.0, "seeds": []},
        "One Strong Seed": {"score": 0.0, "seeds": []},
    }
    for seed in ("A", "B", "C"):
        accumulated["Broad Agreement"]["score"] += 0.4
        accumulated["Broad Agreement"]["seeds"].append((seed, 0.4))
    accumulated["One Strong Seed"]["score"] = 1.0
    accumulated["One Strong Seed"]["seeds"].append(("D", 1.0))
    ranked = rec.rank_candidates(accumulated, _library([]))
    assert [c["name"] for c in ranked] == ["Broad Agreement", "One Strong Seed"]


def test_ranking_drops_what_is_already_owned():
    accumulated = {
        "Sleep": {"score": 5.0, "seeds": [("Electric Wizard", 1.0)]},
        "Om": {"score": 0.3, "seeds": [("Electric Wizard", 0.3)]},
    }
    ranked = rec.rank_candidates(accumulated, _library(["Sleep"]))
    assert [c["name"] for c in ranked] == ["Om"]



def test_a_card_names_its_strongest_seeds_and_never_repeats_one():
    candidate = {"name": "Om", "score": 2.0, "seeds": [
        ("Weak Match", 0.1), ("Strong Match", 0.9), ("Middle Match", 0.5),
        ("Strong Match", 0.8), ("Fourth Match", 0.4)]}
    assert rec._seed_names(candidate) == [
        "Strong Match", "Middle Match", "Fourth Match"]


def test_listener_tags_and_bare_decades_are_not_offered_as_genres():
    # The decade chips already cover eras, from real Qobuz release dates.
    for tag in ("seen live", "Favorites", "under 2000 listeners",
                "90s", "1990s", "80", "2000"):
        assert not rec._tag_is_musical(tag), tag
    for tag in ("doom metal", "shoegaze", "post-rock", "trip hop"):
        assert rec._tag_is_musical(tag), tag


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



def test_a_build_in_progress_beats_the_saved_copy():
    dc.put_feed(rec.SIMILAR, [{"name": "Saved"}], "sig")
    rec._builds[rec.SIMILAR] = rec._new_build()
    rec._publish(rec.SIMILAR, checked=7, total=100, items=[{"name": "Fresh"}])
    view = rec.feed_view(rec.SIMILAR, "sig")
    assert view["phase"] == "building"
    assert view["checked"] == 7 and view["total"] == 100
    assert view["items"] == [{"name": "Fresh"}]


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


def test_a_failed_build_that_already_found_something_keeps_it():
    dc.put_feed(rec.SIMILAR, [{"name": "Saved"}], "sig")
    rec._builds[rec.SIMILAR] = rec._new_build()
    rec._publish(rec.SIMILAR, phase="error", error="rate_limited",
                 items=[{"name": "Found before the limit"}])
    view = rec.feed_view(rec.SIMILAR, "sig")
    assert view["items"] == [{"name": "Found before the limit"}]
    assert view["stale"] is False


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


def test_each_kind_of_failure_is_reported_as_itself():
    # The page says something different for each: a rejected key is the
    # operator's to fix, a rate limit resumes later, an outage shows the saved
    # suggestions instead.
    from qobuz_librarian.api.auth import QobuzUnavailable
    from qobuz_librarian.api.lastfm import (
        LastfmKeyRejected,
        LastfmRateLimited,
        LastfmUnavailable,
    )

    cases = [
        (LastfmKeyRejected("nope"), "key"),
        (LastfmRateLimited("slow down"), "rate_limited"),
        (LastfmUnavailable("down"), "unavailable"),
        (QobuzUnavailable("down"), "qobuz"),
        (RuntimeError("something else"), "other"),
    ]
    for raised, expected in cases:
        def worker(kind, exc=raised):
            raise exc

        rec._builds["t"] = rec._new_build()
        rec._run("t", worker, ())
        status = rec.build_status("t")
        assert status["phase"] == "error"
        assert status["error"] == expected


def test_a_name_qobuz_cannot_serve_is_skipped_and_the_next_promoted():
    # A card with nothing behind it would be a download button that leads
    # nowhere, so the ranking is worked down instead.
    served = {"Second": {"id": "2", "name": "Second"}}
    monkeyed = []

    def fake_card(candidate, token):
        monkeyed.append(candidate["name"])
        hit = served.get(candidate["name"])
        return {"name": hit["name"]} if hit else None

    original = rec._artist_card
    rec._artist_card = fake_card
    try:
        ranked = [{"name": n, "score": 1.0, "seeds": []}
                  for n in ("First", "Second", "Third")]
        cards = rec._artist_cards(ranked, token="tok", want=1)
    finally:
        rec._artist_card = original
    assert cards == [{"name": "Second"}]
    assert monkeyed == ["First", "Second"]


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


def test_a_resolved_artist_is_only_searched_for_once(monkeypatch):
    calls = []

    def once(query, token, limit=None):
        calls.append(query)
        return [{"id": "42", "name": "Om", "albums_count": 6}]

    monkeypatch.setattr(rec, "search_artists", once)
    assert rec.resolve_artist("Om", "tok") == {"id": "42", "name": "Om"}
    assert rec.resolve_artist("Om", "tok") == {"id": "42", "name": "Om"}
    assert calls == ["Om"]


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


def test_the_library_signature_tracks_the_library(monkeypatch):
    monkeypatch.setattr(rec, "cached_artist_resolutions", lambda: {})
    monkeypatch.setattr(rec, "list_library_artists",
                        lambda: [Path("/music/Sleep"), Path("/music/Om")])
    first = rec.read_library().signature
    monkeypatch.setattr(rec, "list_library_artists",
                        lambda: [Path("/music/Om"), Path("/music/Sleep")])
    assert rec.read_library().signature == first  # order is not a change
    monkeypatch.setattr(rec, "list_library_artists",
                        lambda: [Path("/music/Sleep"), Path("/music/Om"),
                                 Path("/music/Earth")])
    assert rec.read_library().signature != first


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


def test_a_build_that_never_reached_lastfm_stops_at_the_first_refusal(monkeypatch):
    # Three timeouts per artist at ten seconds each would leave the page
    # counting to nothing for a minute and a half before saying why.
    from qobuz_librarian.api.lastfm import LastfmUnavailable

    asked = []

    def down(name, **kw):
        asked.append(name)
        raise LastfmUnavailable("down")

    monkeypatch.setattr(rec, "get_similar_artists", down)
    owned = _library(["Sleep", "Om", "Earth", "Bongzilla"])
    rec._builds[rec.SIMILAR] = rec._new_build()
    rec._run(rec.SIMILAR, rec._similar_worker, ("tok", owned))
    assert asked == ["Sleep"]
    assert rec.build_status(rec.SIMILAR)["error"] == "unavailable"


def test_an_outage_partway_through_does_not_lose_the_work_already_done(monkeypatch):
    from qobuz_librarian.api.lastfm import LastfmUnavailable

    asked = []

    def flaky(name, **kw):
        asked.append(name)
        if len(asked) == 1:
            return [{"name": "Om", "match": 0.9}]
        raise LastfmUnavailable("down")

    monkeypatch.setattr(rec, "get_similar_artists", flaky)
    monkeypatch.setattr(rec, "_artist_cards", lambda ranked, token, want, progress=None: [])
    owned = _library(["Sleep", "Earth", "Bongzilla", "Weedeater", "Windhand"])
    rec._builds[rec.SIMILAR] = rec._new_build()
    rec._run(rec.SIMILAR, rec._similar_worker, ("tok", owned))
    # One success, then two tolerated refusals, then it stops on the third.
    assert asked == ["Sleep", "Earth", "Bongzilla", "Weedeater"]
    assert rec.build_status(rec.SIMILAR)["error"] == "unavailable"


def test_favourites_leaves_out_albums_already_in_the_library(monkeypatch):
    saved = [
        {"id": 1, "title": "Dummy", "artist": {"name": "Owned Band"}},
        {"id": 2, "title": "Wanted", "artist": {"name": "New Band"}},
    ]
    monkeypatch.setattr(rec, "get_user_favorites",
                        lambda token: saved)
    monkeypatch.setattr(rec, "_owned_on_disk",
                        lambda card: card["artist"] == "Owned Band")
    owned = rec.Library(set(), [], [], "sig")
    rec._builds["favourites"] = rec._new_build()
    rec._favourites_worker("favourites", "tok", owned)
    cards = rec.build_status("favourites")["items"]
    assert [c["title"] for c in cards] == ["Wanted"]


def test_a_short_release_is_left_out_of_a_suggestions_discography(monkeypatch):
    # Discover follows the missing-albums rule: releases under the track
    # threshold are noise, but an unknown track count is bad metadata, not a
    # single, and passes.
    rows = [
        {"id": "a1", "title": "Real Album", "tracks_count": 10,
         "artist": {"name": "Someone"},
         "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1},
        {"id": "s1", "title": "Some Single", "tracks_count": 1,
         "artist": {"name": "Someone"},
         "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1},
        {"id": "u1", "title": "No Count", "tracks_count": None,
         "artist": {"name": "Someone"},
         "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1},
    ]
    monkeypatch.setattr(rec, "get_artist_albums",
                        lambda artist_id, token: (rows, len(rows)))
    monkeypatch.setattr(cfg, "MISSING_ALBUMS_MIN_TRACKS", 4)
    got = {a["id"] for a in rec.artist_albums("42", "Someone", token=None)}
    assert got == {"a1", "u1"}


def test_a_genre_card_under_the_track_threshold_is_dropped(monkeypatch):
    monkeypatch.setattr(cfg, "MISSING_ALBUMS_MIN_TRACKS", 4)
    monkeypatch.setattr(rec, "get_tag_top_albums",
                        lambda tag, limit=None: [
                            {"artist": "A", "title": "Album"},
                            {"artist": "A", "title": "Single"},
                        ])
    resolved = {
        "Album": {"id": "a1", "title": "Album", "artist": "A", "tracks": 9,
                  "version": "", "year": 2020, "cover": "",
                  "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1},
        "Single": {"id": "s1", "title": "Single", "artist": "A", "tracks": 1,
                   "version": "", "year": 2021, "cover": "",
                   "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1},
    }
    monkeypatch.setattr(rec, "resolve_album",
                        lambda artist, title, token: resolved[title])
    monkeypatch.setattr(rec, "_owned_on_disk", lambda album: False)
    rec._genre_worker(rec.genre_feed_kind("tag"), None, _library(["B"]), "tag")
    feed = dc.get_feed(rec.genre_feed_kind("tag"))
    assert [a["id"] for a in feed["payload"]] == ["a1"]
