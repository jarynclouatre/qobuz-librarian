"""Tests for qobuz_librarian.api.lastfm - error-code mapping, retry, shapes."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from qobuz_librarian import config as cfg
from qobuz_librarian.api import lastfm
from qobuz_librarian.api.lastfm import (
    LastfmError,
    LastfmKeyRejected,
    LastfmRateLimited,
    LastfmUnavailable,
)


@pytest.fixture(autouse=True)
def _key_and_no_waiting(monkeypatch):
    """Every test runs with a key set and with both the rate-limiter gap and
    the retry backoff turned into no-ops, so the suite doesn't spend real
    seconds asleep."""
    monkeypatch.setattr(cfg, "LASTFM_API_KEY", "0" * 32)
    monkeypatch.setattr(lastfm, "_sleep", lambda seconds: None)
    lastfm._reset_for_tests()


def _response(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    if json_data is None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = json_data
    return r


def test_error_codes_map_to_their_own_exceptions():
    # Each code the builder reacts to differently gets its own type: 10/26 stop
    # the build, 29 pauses it, 8/11/16 fall back to saved suggestions, and
    # anything else is just an error.
    cases = [
        (10, LastfmKeyRejected),
        (26, LastfmKeyRejected),
        (29, LastfmRateLimited),
        (8, LastfmUnavailable),
        (11, LastfmUnavailable),
        (16, LastfmUnavailable),
        (7, LastfmError),
    ]
    for code, expected in cases:
        with patch.object(lastfm, "_get_session") as sess:
            sess.return_value.get.return_value = _response(
                403, {"error": code, "message": "no"})
            with pytest.raises(expected) as caught:
                lastfm.lastfm_get("artist.getSimilar", {"artist": "x"})
            assert caught.value.code == code
            # The specific types stay catchable as the general one.
            assert isinstance(caught.value, LastfmError)


def test_unknown_artist_is_an_empty_result_not_an_error():
    # Code 6 means Last.fm has never heard of the name. A library full of
    # obscure artists hits this constantly; it must not stop the build.
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(
            404, {"error": 6, "message": "The artist you supplied could not be found"})
        assert lastfm.get_similar_artists("Nobody At All") == []


def test_network_failure_outlasting_retries_is_unavailable():
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.side_effect = requests.ConnectionError("down")
        with pytest.raises(LastfmUnavailable):
            lastfm.lastfm_get("chart.getTopArtists", {})
        assert sess.return_value.get.call_count == 3


def test_network_failure_that_recovers_is_retried():
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.side_effect = [
            requests.Timeout("slow"),
            _response(200, {"ok": True}),
        ]
        assert lastfm.lastfm_get("chart.getTopArtists", {}) == {"ok": True}


def test_5xx_retries_but_403_does_not():
    # A 5xx is Last.fm having a moment; a 403 with no readable body is a
    # definitive refusal and retrying it just burns the rate limit.
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.side_effect = [_response(503), _response(503),
                                             _response(200, {"ok": True})]
        assert lastfm.lastfm_get("chart.getTopArtists", {}) == {"ok": True}
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(403)
        with pytest.raises(LastfmError):
            lastfm.lastfm_get("chart.getTopArtists", {})
        assert sess.return_value.get.call_count == 1


def test_http_429_is_rate_limited_even_without_an_error_body():
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(429)
        with pytest.raises(LastfmRateLimited):
            lastfm.lastfm_get("chart.getTopArtists", {})


def test_missing_key_is_reported_as_a_rejected_key():
    with patch.object(cfg, "LASTFM_API_KEY", ""):
        with pytest.raises(LastfmKeyRejected):
            lastfm.lastfm_get("chart.getTopArtists", {})
        assert lastfm.is_configured() is False



def test_similar_artists_parses_scores_and_survives_missing_ones():
    # match arrives as a string, and some rows have no match at all. An
    # unscored neighbour ranks last rather than vanishing or crashing.
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"similarartists": {
            "artist": [
                {"name": "Sleep", "match": "1"},
                {"name": "Om", "match": "0.4213"},
                {"name": "Bongzilla"},
                {"name": "  ", "match": "0.9"},
            ]}})
        got = lastfm.get_similar_artists("Electric Wizard")
    assert got == [
        {"name": "Sleep", "match": 1.0},
        {"name": "Om", "match": 0.4213},
        {"name": "Bongzilla", "match": 0.0},
    ]


def test_a_single_result_arrives_as_an_object_not_a_list():
    # Last.fm collapses a one-entry list into a bare object. Reading it as a
    # list would silently drop the only suggestion there was.
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"similarartists": {
            "artist": {"name": "Sleep", "match": "0.5"}}})
        assert lastfm.get_similar_artists("Electric Wizard") == [
            {"name": "Sleep", "match": 0.5}]


def test_album_rows_accept_both_artist_shapes():
    # tag.getTopAlbums nests the artist as an object; other endpoints send a
    # bare name. Rows missing either half are dropped rather than shown as a
    # card nothing can be downloaded from.
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"albums": {
            "album": [
                {"name": "Dopethrone", "artist": {"name": "Electric Wizard"}},
                {"name": "Jerusalem", "artist": "Sleep"},
                {"name": "Nameless"},
                {"artist": {"name": "Om"}},
            ]}})
        got = lastfm.get_tag_top_albums("doom metal")
    assert got == [
        {"artist": "Electric Wizard", "title": "Dopethrone"},
        {"artist": "Sleep", "title": "Jerusalem"},
    ]


def test_top_tags_keep_their_counts():
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"toptags": {
            "tag": [{"name": "doom metal", "count": 100},
                    {"name": "stoner rock", "count": "62"},
                    {"name": "seen live"}]}})
        assert lastfm.get_artist_top_tags("Electric Wizard") == [
            {"name": "doom metal", "count": 100},
            {"name": "stoner rock", "count": 62},
            {"name": "seen live", "count": 0},
        ]



def test_probe_key_separates_a_bad_key_from_lastfm_being_down():
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(
            200, {"artists": {"artist": []}})
        assert lastfm.probe_key() is True
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(403, {"error": 10})
        with pytest.raises(LastfmKeyRejected):
            lastfm.probe_key()
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.side_effect = requests.ConnectionError("down")
        with pytest.raises(LastfmUnavailable):
            lastfm.probe_key()


def test_the_key_is_registered_for_redaction():
    # Any log line or stored record carrying the key gets it masked.
    from qobuz_librarian import redaction

    lastfm.api_key()
    assert "0" * 32 not in redaction.redact("api_key=" + "0" * 32)
    assert "0" * 32 not in redaction.redact("the key is " + "0" * 32)


def test_requests_are_spaced_by_the_minimum_interval():
    # The gap is what keeps a several-hundred-artist build inside Last.fm's
    # limit, so it is enforced here rather than left to each caller.
    waits = []
    with patch.object(lastfm, "_sleep", waits.append), \
            patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"ok": True})
        lastfm._reset_for_tests()
        lastfm.lastfm_get("chart.getTopArtists", {})
        lastfm.lastfm_get("chart.getTopArtists", {})
    assert len(waits) == 1
    assert 0 < waits[0] <= lastfm._MIN_INTERVAL
