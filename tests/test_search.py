"""Tests for qobuz_librarian.api.search - strict ISRC matching (album repair
depends on it), pagination, and the album cache."""

from unittest.mock import patch

import pytest

from qobuz_librarian.api.auth import QobuzError
from qobuz_librarian.api.search import find_qobuz_track_by_isrc


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


def test_get_album_fetches_every_page_of_a_long_album(tmp_path, monkeypatch):
    # A box set longer than one page arrived as its first page, and gap fill
    # and downloads both read that page as the whole album.
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache, search

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    album_cache._reset_for_tests()
    try:
        tracks = [{"id": n, "title": f"Track {n}"} for n in range(600)]
        served = []

        def paged_get(_endpoint, params, _token):
            offset = params.get("offset", 0)
            served.append(offset)
            held = [] if (params["album_id"] == "SHORT" and offset) else \
                tracks[offset:offset + 100]
            return {"id": params["album_id"], "title": "Box",
                    "tracks": {"items": held, "total": 600, "offset": offset}}

        monkeypatch.setattr(search, "qobuz_get", paged_get)
        box = search.get_album("BOX", "tok")
        assert [t["id"] for t in box["tracks"]["items"]] == list(range(600))
        assert served == [0, 100, 200, 300, 400, 500]

        assert search.get_album("BOX", "tok") == box
        assert len(served) == 6

        # A server that stops short leaves the album unusable rather than
        # passing 100 tracks off as all 600.
        served.clear()
        with pytest.raises(QobuzError):
            search.get_album("SHORT", "tok")
        assert served == [0, 100]
        assert album_cache.get("SHORT") is None
    finally:
        album_cache._reset_for_tests()
