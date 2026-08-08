"""Tests for qobuz_librarian.api.search - strict ISRC matching (album repair
depends on it), result extraction, pagination, and the album/catalog cache."""

from unittest.mock import patch

import pytest

from qobuz_librarian.api.auth import QobuzError
from qobuz_librarian.api.search import (
    find_qobuz_track_by_isrc,
    get_artist_albums,
    search_albums,
    search_artists,
    search_tracks,
)


def _track(isrc=None, **kwargs):
    t = {"id": 123, "title": "Test Track", "duration": 200}
    if isrc is not None:
        t["isrc"] = isrc
    t.update(kwargs)
    return t


def test_find_qobuz_track_by_isrc_is_strict():
    # Hyphens and case are folded, but matching is otherwise exact. Album repair
    # would refill the wrong recording if a substring/prefix counted.
    with patch("qobuz_librarian.api.search.search_tracks",
               return_value=[_track(isrc="USRC1234567")]):
        assert find_qobuz_track_by_isrc("US-RC1-23-4567", "tok")["isrc"] == "USRC1234567"
    for result_isrc in ("USRC12345678", "USRC1234567X"):  # extra digit / suffix
        with patch("qobuz_librarian.api.search.search_tracks",
                   return_value=[_track(isrc=result_isrc)]):
            assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None
    # A track with no ISRC field is skipped, and the first exact match in
    # result order wins.
    ordered = [_track(isrc="OTHER12345", id=0), _track(isrc="USRC1234567", id=111),
               _track(isrc="USRC1234567", id=222)]
    with patch("qobuz_librarian.api.search.search_tracks", return_value=ordered):
        assert find_qobuz_track_by_isrc("USRC1234567", "tok")["id"] == 111


def test_search_helpers_extract_items_or_empty():
    with patch("qobuz_librarian.api.search.qobuz_get",
               return_value={"albums": {"items": [{"id": 1}, {"id": 2}]}}):
        assert search_albums("q", "tok") == [{"id": 1}, {"id": 2}]
    with patch("qobuz_librarian.api.search.qobuz_get",
               return_value={"tracks": {"items": [{"id": 1}]}}):
        assert search_tracks("q", "tok") == [{"id": 1}]
    with patch("qobuz_librarian.api.search.qobuz_get",
               return_value={"artists": {"items": [{"id": 1}]}}):
        assert search_artists("q", "tok") == [{"id": 1}]
    # A response missing the items envelope yields an empty list, not a KeyError.
    with patch("qobuz_librarian.api.search.qobuz_get", return_value={}):
        assert search_albums("q", "tok") == []


def test_search_guards_malformed_bodies():
    # A non-dict top-level body (a CDN/proxy error page served as a JSON list or
    # string) becomes the QobuzError callers already handle, not a raw .get crash.
    for bad in (["x"], "error", 42, None):
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=bad):
            try:
                search_albums("q", "tok")
            except QobuzError:
                pass
            else:
                raise AssertionError(f"expected QobuzError for body {bad!r}")
    # A truthy but non-dict envelope (albums is a list/string) must yield [] via
    # the envelope guard rather than crashing on .get("items").
    with patch("qobuz_librarian.api.search.qobuz_get", return_value={"albums": ["x"]}):
        assert search_albums("q", "tok") == []
    with patch("qobuz_librarian.api.search.qobuz_get", return_value={"tracks": "oops"}):
        assert search_tracks("q", "tok") == []


def test_artist_search_rejects_malformed_success_payloads():
    # A clean empty items list is a real no-match. Missing/wrong envelopes and
    # non-object entries are incomplete HTTP-200 responses, not the same fact.
    bad_payloads = (
        {},
        {"artists": []},
        {"artists": {"items": "oops"}},
        {"artists": {"items": [None]}},
    )
    for payload in bad_payloads:
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=payload):
            with pytest.raises(QobuzError):
                search_artists("q", "tok")

    with patch("qobuz_librarian.api.search.qobuz_get",
               return_value={"artists": {"items": []}}):
        assert search_artists("q", "tok") == []


def test_get_artist_albums_paginates_and_stops_early():
    page1 = {"albums": {"items": [{"id": i} for i in range(100)], "total": 105}}
    page2 = {"albums": {"items": [{"id": i} for i in range(100, 105)]}}
    page3 = {"albums": {"items": []}}
    with patch("qobuz_librarian.api.search.qobuz_get", side_effect=[page1, page2, page3]):
        items, total = get_artist_albums("artist123", "tok")
    assert len(items) == 105 and total == 105
    # A short first page (fewer than the page size) stops without a second call.
    short = {"albums": {"items": [{"id": i} for i in range(50)], "total": 200}}
    with patch("qobuz_librarian.api.search.qobuz_get", return_value=short):
        items, _ = get_artist_albums("artist123", "tok", limit=500)
    assert len(items) == 50


def test_get_artist_albums_advances_past_malformed_page_entries():
    # A full raw page that mixes in a few malformed (non-dict) entries must
    # NOT look like the end of the discography.
    page1 = {"albums": {"items": [{"id": i} for i in range(99)] + ["oops"],
                        "total": 101}}
    page2 = {"albums": {"items": [{"id": 99}, {"id": 100}]}}
    with patch("qobuz_librarian.api.search.qobuz_get",
               side_effect=[page1, page2]) as gg:
        items, total = get_artist_albums("artist_malformed", "tok")
    # 101 usable dict albums, both pages fetched (filtered-count logic stopped
    # after page 1 with only 99 and never made the second call).
    assert total == 101
    assert [a["id"] for a in items] == list(range(101))
    assert gg.call_count == 2
    # The second call asked for the offset AFTER the full raw page (100), not 99.
    assert gg.call_args_list[1].args[1]["offset"] == 100


def test_get_artist_albums_does_not_cache_a_short_fetch(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache, search
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    monkeypatch.setattr(cfg, "ARTIST_CATALOG_CACHE_TTL", 3600)
    album_cache._reset_for_tests()
    try:
        # Qobuz says the artist has 105 albums but hands back 100 and then an
        # empty page, a transient short read.
        page1 = {"albums": {"items": [{"id": i} for i in range(100)], "total": 105}}
        empty = {"albums": {"items": []}}
        with patch("qobuz_librarian.api.search.qobuz_get", side_effect=[page1, empty]):
            items, total = search.get_artist_albums("ART_SHORT", "tok", limit=500)
        assert len(items) == 100 and total == 105
        full = {"albums": {"items": [{"id": i} for i in range(105)], "total": 105}}
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=full) as gg:
            items2, _ = search.get_artist_albums("ART_SHORT", "tok", limit=500)
        assert gg.called and len(items2) == 105
    finally:
        album_cache._reset_for_tests()


def test_get_album_cached_by_id(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache, search
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    album_cache._reset_for_tests()
    try:
        calls = {"n": 0}

        def fake_get(endpoint, params, token):
            calls["n"] += 1
            return {"id": params["album_id"], "title": "X",
                    "tracks": {"items": [{"id": 1, "title": "T"}]}}

        monkeypatch.setattr(search, "qobuz_get", fake_get)
        # An album's track list is immutable → the second fetch is served from cache.
        a1 = search.get_album("ALB1", "tok")
        a2 = search.get_album("ALB1", "tok")
        assert calls["n"] == 1 and a1 == a2 and a1["id"] == "ALB1"
    finally:
        album_cache._reset_for_tests()


def test_get_album_refuses_cross_edition_provider_identity(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache, search

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    album_cache._reset_for_tests()
    try:
        monkeypatch.setattr(
            search,
            "qobuz_get",
            lambda *_args, **_kwargs: {
                "id": "OTHER-EDITION",
                "title": "X",
                "tracks": {"items": [{"id": 1, "title": "T"}]},
            },
        )
        with pytest.raises(QobuzError, match="OTHER-EDITION"):
            search.get_album("REQUESTED-EDITION", "tok")
        assert album_cache.get("REQUESTED-EDITION") is None
    finally:
        album_cache._reset_for_tests()
