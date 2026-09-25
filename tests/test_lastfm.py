"""Last.fm client tests."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from qobuz_librarian import config as cfg
from qobuz_librarian.api import lastfm


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


def test_unknown_artist_is_an_empty_result_not_an_error():
    # Code 6 means Last.fm has never heard of the name. A library full of
    # obscure artists hits this constantly; it must not stop the build.
    with patch.object(lastfm, "_get_session") as sess:
        sess.return_value.get.return_value = _response(
            404, {"error": 6, "message": "The artist you supplied could not be found"})
        assert lastfm.get_similar_artists("Nobody At All") == []
        # A one-entry list arrives as a bare object and still counts.
        sess.return_value.get.return_value = _response(200, {"similarartists": {
            "artist": {"name": "Sleep", "match": "0.5"}}})
        assert lastfm.get_similar_artists("Electric Wizard") == [
            {"name": "Sleep", "match": 0.5}]
