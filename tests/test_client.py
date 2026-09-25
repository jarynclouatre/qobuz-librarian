"""Tests for qobuz_librarian.api.client - status handling and retry."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from qobuz_librarian.api.auth import (
    AuthLost,
    AuthOutcome,
    CredentialChanged,
    QobuzAccess,
    QobuzUnavailable,
    credentials_from_values,
)
from qobuz_librarian.api.client import (
    authorize_qobuz_action,
    probe_qobuz,
    qobuz_get,
)


def _response(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.text = text
    r.headers = {}
    return r


def test_qobuz_get_maps_status_codes():
    # 200 → parsed JSON; 429 backs off and retries; 401 → AuthLost (so creds
    # get torn down); a network failure that outlasts the retries →
    # QobuzUnavailable, the "service is down, retry later" signal that callers
    # must not mistake for a genuine no-match.
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"albums": {"items": []}})
        assert qobuz_get("album/search", {"query": "x"}, "tok") == {"albums": {"items": []}}
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.side_effect = [_response(429), _response(200, {"ok": True})]
        assert qobuz_get("album/search", {}, "tok") == {"ok": True}
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(401)
        with pytest.raises(AuthLost):
            qobuz_get("album/search", {}, "bad")
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.side_effect = requests.RequestException("timeout")
        with pytest.raises(QobuzUnavailable):
            qobuz_get("album/search", {}, "tok")
    # A 400 from the login probe is not a rejected token.
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(400, text="bad request")
        assert probe_qobuz("token") is AuthOutcome.INCONCLUSIVE


def test_queued_action_rejects_changed_credentials_before_network(monkeypatch):
    before = credentials_from_values("user", "old", source="streamrip")
    after = credentials_from_values("user", "new", source="streamrip")
    monkeypatch.setattr(
        "qobuz_librarian.api.client.load_qobuz_credentials",
        lambda: after,
    )

    with patch("qobuz_librarian.api.client._get_session") as session:
        with pytest.raises(CredentialChanged):
            authorize_qobuz_action(
                QobuzAccess.CATALOGUE_ACTION,
                expected_generation=before.generation,
            )

    session.assert_not_called()
