"""Tests for qobuz_librarian.api.client - status handling, retry/backoff, UA."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
from qobuz_librarian.api.client import _retry_after, qobuz_get, request_deadline


def _response(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.text = text
    r.headers = {}
    return r


def test_qobuz_get_maps_status_codes():
    # 200 → parsed JSON; 401 → AuthLost (so creds get torn down); a network
    # failure that outlasts the retries → QobuzUnavailable, the "service is down,
    # retry later" signal that callers must not mistake for a genuine no-match.
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"albums": {"items": []}})
        assert qobuz_get("album/search", {"query": "x"}, "tok") == {"albums": {"items": []}}
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(401)
        with pytest.raises(AuthLost):
            qobuz_get("album/search", {}, "bad")
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.side_effect = requests.RequestException("timeout")
        with pytest.raises(QobuzUnavailable):
            qobuz_get("album/search", {}, "tok")


def test_qobuz_get_retries_429_but_not_404():
    # 429 backs off and retries; a 404 is a definitive answer (QobuzError, the
    # caller may read it as "no such album") and must not be retried.
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.side_effect = [_response(429), _response(429),
                                             _response(200, {"ok": True})]
        assert qobuz_get("album/search", {}, "tok") == {"ok": True}
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(404, text="missing")
        with pytest.raises(QobuzError):
            qobuz_get("album/get", {"album_id": "nope"}, "tok")
        assert sess.return_value.get.call_count == 1
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(429)
        with pytest.raises(QobuzUnavailable):
            qobuz_get("album/search", {}, "tok")
        assert not isinstance(QobuzUnavailable(), QobuzError)  # distinct signals


def test_qobuz_get_reports_token_validity(monkeypatch):
    # The dashboard banner listens for auth state.
    from qobuz_librarian.api import auth
    seen = []
    monkeypatch.setattr(auth, "_auth_state_listeners", [seen.append])
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"ok": True})
        qobuz_get("album/search", {}, "tok")
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(404, text="not found")
        with pytest.raises(QobuzError):
            qobuz_get("album/get", {"album_id": "nope"}, "tok")
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(401)
        with pytest.raises(AuthLost):
            qobuz_get("album/search", {}, "bad")
    assert seen == [True, True, False]


def test_retry_after_header_parsing():
    from types import SimpleNamespace

    def resp(val):
        return SimpleNamespace(headers={} if val is None else {"Retry-After": val})

    assert _retry_after(resp(None)) is None      # no header
    assert _retry_after(resp("abc")) is None     # non-numeric
    assert _retry_after(resp("nan")) is None      # NaN rejected, not run through the clamp
    assert _retry_after(resp("0")) == 0.0         # retry-immediately stays 0.0, not None
    assert _retry_after(resp("-3")) == 0.0        # clamped to the floor
    assert _retry_after(resp("999")) == 30.0      # clamped to the 30 s ceiling
    assert _retry_after(resp("12")) == 12.0




def test_expired_request_deadline_starts_no_provider_request():
    with patch("qobuz_librarian.api.client._get_session") as session:
        with request_deadline(0):
            with pytest.raises(QobuzUnavailable, match="timed out"):
                qobuz_get("album/get", {"album_id": "1"}, "tok")
        session.assert_not_called()
