"""Tests for qobuz_librarian.api.client - status handling, retry/backoff, UA."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from qobuz_librarian.api.auth import (
    AuthLost,
    AuthOutcome,
    CredentialChanged,
    DownloaderNotReady,
    QobuzAccess,
    QobuzError,
    QobuzUnavailable,
    credentials_from_values,
)
from qobuz_librarian.api.client import (
    _retry_after,
    authorize_bound_download,
    authorize_qobuz_action,
    bind_download_preflight,
    probe_qobuz,
    qobuz_get,
    request_deadline,
)


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


def test_qobuz_get_reports_generation_bound_auth_evidence(monkeypatch):
    from qobuz_librarian.api import auth

    credentials = credentials_from_values("user", "tok", source="streamrip")
    seen = []
    monkeypatch.setattr(auth, "_auth_state_listeners", [seen.append])
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"ok": True})
        qobuz_get("album/search", {}, credentials.token)
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(400, text="bad request")
        with pytest.raises(QobuzError):
            qobuz_get("album/search", {}, credentials.token)
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(403, text="subscription")
        with pytest.raises(QobuzError):
            qobuz_get("album/get", {"album_id": "nope"}, credentials.token)
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(401)
        with pytest.raises(AuthLost):
            qobuz_get("album/search", {}, credentials.token)
    assert [e.generation for e in seen] == [credentials.generation] * 3
    assert [e.outcome for e in seen] == [
        AuthOutcome.ACCEPTED,
        AuthOutcome.ENTITLEMENT,
        AuthOutcome.REJECTED,
    ]


def test_unsaved_settings_probe_publishes_no_auth_evidence(monkeypatch):
    from qobuz_librarian.api import auth

    credentials = credentials_from_values("new-user", "new-token")
    seen = []
    monkeypatch.setattr(auth, "_auth_state_listeners", [seen.append])
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(401)
        assert probe_qobuz(
            credentials.token,
            report_auth=False,
        ) is AuthOutcome.REJECTED
    assert seen == []


def test_shared_probe_does_not_call_http_400_a_rejected_token():
    with patch("qobuz_librarian.api.client._get_session") as session:
        session.return_value.get.return_value = _response(400, text="bad request")
        assert probe_qobuz("token") is AuthOutcome.INCONCLUSIVE


def test_action_authorization_live_checks_before_reporting_missing_identity(
        monkeypatch):
    credentials = credentials_from_values("", "token", source="env")
    monkeypatch.setattr(
        "qobuz_librarian.api.client.load_qobuz_credentials",
        lambda: credentials,
    )
    with patch("qobuz_librarian.api.client._get_session") as session:
        session.return_value.get.return_value = _response(200, {"ok": True})
        with pytest.raises(DownloaderNotReady):
            authorize_qobuz_action(QobuzAccess.DOWNLOAD_ACTION)
    assert session.return_value.get.call_count == 1


def test_bound_download_runs_and_carries_local_preflight(monkeypatch):
    from qobuz_librarian.api import client

    credentials = credentials_from_values("user", "token", source="env")
    events = []

    def authorize(access, **kwargs):
        events.append(("authorize", access, kwargs["expected_generation"]))
        return credentials

    token = bind_download_preflight(
        credentials.token,
        lambda: events.append(("preflight",)),
    )
    monkeypatch.setattr(client, "authorize_qobuz_action", authorize)

    admitted = authorize_bound_download(token)
    carried = authorize_bound_download(admitted, prepare=False)

    assert carried == credentials.token
    assert events == [
        ("authorize", QobuzAccess.DOWNLOAD_ACTION, credentials.generation),
        ("preflight",),
        ("authorize", QobuzAccess.DOWNLOAD_ACTION, credentials.generation),
    ]


def test_action_authorization_does_not_trust_cached_rejection(monkeypatch):
    credentials = credentials_from_values("user", "token", source="streamrip")
    monkeypatch.setattr(
        "qobuz_librarian.api.client.load_qobuz_credentials",
        lambda: credentials,
    )
    monkeypatch.setattr(
        "qobuz_librarian.api.client.read_qobuz_credentials",
        lambda: credentials,
    )
    with patch("qobuz_librarian.api.client._get_session") as session:
        session.return_value.get.return_value = _response(200, {"ok": True})
        authorized = authorize_qobuz_action(
            QobuzAccess.CATALOGUE_ACTION,
            auth_valid=False,
        )
    assert authorized == credentials
    assert session.return_value.get.call_count == 1


def test_action_authorization_rejects_a_generation_change(monkeypatch):
    before = credentials_from_values("user", "old", source="streamrip")
    after = credentials_from_values("user", "new", source="streamrip")
    monkeypatch.setattr(
        "qobuz_librarian.api.client.load_qobuz_credentials",
        lambda: before,
    )
    monkeypatch.setattr(
        "qobuz_librarian.api.client.read_qobuz_credentials",
        lambda: after,
    )
    with patch("qobuz_librarian.api.client._get_session") as session:
        session.return_value.get.return_value = _response(200, {"ok": True})
        with pytest.raises(CredentialChanged):
            authorize_qobuz_action(QobuzAccess.CATALOGUE_ACTION)


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
