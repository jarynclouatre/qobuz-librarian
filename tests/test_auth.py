"""Tests for qobuz_librarian.api.auth - pattern detection, token I/O."""
import tomllib
from unittest.mock import patch

import pytest

from qobuz_librarian.api.auth import (
    AccessBlock,
    NoCredsError,
    QobuzAccess,
    credentials_from_values,
    detect_auth_lost,
    detect_rate_limited,
    load_qobuz_token,
    qobuz_capability,
    read_qobuz_credentials,
    sync_streamrip_creds_from_env,
    write_streamrip_creds,
)


def test_detect_auth_lost_only_fires_on_http_401():
    # Real auth-lost signal.
    assert detect_auth_lost("Error: http 401 from endpoint") is True
    # "401" appearing in track titles or counts must not trigger - that
    # would falsely tear down credentials mid-download.
    assert detect_auth_lost("Downloaded track 401 - Song Title.flac") is False
    assert detect_auth_lost("Downloading track 401 of 500") is False
    assert detect_auth_lost("") is False
    # A real album title containing "Unauthorized" must not tear down creds
    # mid-download (streamrip echoes titles in its progress output).
    assert detect_auth_lost(
        "Downloading The Unauthorized Biography of Reinhold Messner") is False
    # But an actual 401 Unauthorized error line still fires.
    assert detect_auth_lost(
        "HTTPError: 401 Client Error: Unauthorized for url") is True
    # Streamrip exhausting its retries reads as throttling; one retry does not.
    assert detect_rate_limited("Persistent error downloading track 'X', skipping") is True
    assert detect_rate_limited("Error downloading track 'X', retrying") is False


def test_load_qobuz_token_happy_and_error_paths(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[qobuz]\nuse_auth_token = true\n'
        'email_or_userid = "12345"\n'
        'password_or_token = "mytoken"\n'
    )
    with patch("qobuz_librarian.config.STREAMRIP_CONFIG", cfg):
        assert load_qobuz_token() == ("12345", "mytoken")

    # A missing file and garbage TOML both raise NoCredsError so the caller
    # can route the user to Settings.
    for content in (None, "this is not toml ===]]] [[[ ==="):
        if content is None:
            cfg.unlink()
        else:
            cfg.write_text(content)
        with patch("qobuz_librarian.config.STREAMRIP_CONFIG", cfg):
            with pytest.raises(NoCredsError):
                load_qobuz_token()


def test_credentials_separate_saved_reads_catalogue_and_download_access(
        tmp_path, monkeypatch):
    from qobuz_librarian import config

    monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "token-only")
    monkeypatch.setattr(config, "QOBUZ_USER_ID", "")
    monkeypatch.setattr(config, "STREAMRIP_CONFIG", tmp_path / "missing.toml")

    credentials = read_qobuz_credentials()
    assert credentials.configured is True
    assert credentials.downloader_ready is False
    assert "token-only" not in credentials.generation
    assert credentials.token.credential_generation == credentials.generation

    assert qobuz_capability(
        QobuzAccess.SAVED_READ, credentials, auth_valid=False
    ).allowed is True
    catalogue = qobuz_capability(
        QobuzAccess.CATALOGUE_ACTION, credentials, auth_valid=None
    )
    assert catalogue.allowed is True
    assert catalogue.live_check_required is True
    download = qobuz_capability(
        QobuzAccess.DOWNLOAD_ACTION, credentials, auth_valid=True
    )
    assert download.allowed is False
    assert download.block is AccessBlock.ADD_USER_ID

    rejected = qobuz_capability(
        QobuzAccess.CATALOGUE_ACTION, credentials, auth_valid=False
    )
    assert rejected.allowed is False
    assert rejected.block is AccessBlock.RECONNECT_QOBUZ

    ready = credentials_from_values("user-1", "token-only", source="env")
    assert ready.generation != credentials.generation
    assert qobuz_capability(
        QobuzAccess.DOWNLOAD_ACTION, ready, auth_valid=True
    ).allowed is True


def test_sync_streamrip_creds_from_env_writes_and_stays_idempotent(tmp_path, monkeypatch):
    from qobuz_librarian import config
    cfg_path = tmp_path / "streamrip" / "config.toml"
    monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "tok-abc")
    monkeypatch.setattr(config, "QOBUZ_USER_ID", "42")
    monkeypatch.setattr(config, "STREAMRIP_CONFIG", cfg_path)
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "staging")

    assert sync_streamrip_creds_from_env() is True
    data = tomllib.loads(cfg_path.read_text())
    assert data["qobuz"]["password_or_token"] == "tok-abc"
    assert data["qobuz"]["email_or_userid"] == "42"
    assert data["qobuz"]["use_auth_token"] is True
    # Streamrip 2.2 expects a [qobuz.secrets] table and a misc.version field.
    assert "secrets" in data["qobuz"] and "version" in data["misc"]
    # Second call is a no-op when nothing changed.
    assert sync_streamrip_creds_from_env() is None
    # A token rotation must rewrite the file.
    monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "tok-new")
    assert sync_streamrip_creds_from_env() is True
    assert tomllib.loads(cfg_path.read_text())["qobuz"]["password_or_token"] == "tok-new"


def test_failed_streamrip_credential_publish_keeps_the_prior_file(
        tmp_path, monkeypatch):
    import os

    from qobuz_librarian import config

    cfg_path = tmp_path / "streamrip" / "config.toml"
    monkeypatch.setattr(config, "STREAMRIP_CONFIG", cfg_path)
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "staging")
    assert write_streamrip_creds("prior-user", "prior-token") is True
    prior_bytes = cfg_path.read_bytes()
    real_replace = os.replace

    def fail_target_replace(source, destination):
        if destination == cfg_path:
            raise OSError("injected credential publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_target_replace)

    assert write_streamrip_creds("new-user", "new-token") is False
    assert cfg_path.read_bytes() == prior_bytes
    assert cfg_path.stat().st_mode & 0o777 == 0o600
    assert not list(cfg_path.parent.glob(".streamrip.*.tmp"))
