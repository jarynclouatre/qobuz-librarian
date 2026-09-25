"""Env-var validation: a bad value falls back loudly instead of surfacing
later as an opaque download/lyrics failure."""
import os

import pytest

import qobuz_librarian.config as cfg


def test_env_bool_empty_string_means_unset(monkeypatch):
    # compose's `${PREFER_HIRES:-}` resolves to "" - that must mean "use the
    # default", not silently flip the flag off.
    monkeypatch.setenv("PREFER_HIRES", "")
    assert cfg._env_bool("PREFER_HIRES", True) is True


def test_resolve_secret_reads_token_from_a_file(monkeypatch, tmp_path):
    # Docker-secret style: the token lives in a file, not the environment, so
    # it stays out of `docker inspect`. The trailing newline a file carries must
    # be stripped. The resolved value is NOT written back to os.environ - doing
    # so re-exported the secret into every subprocess the app spawns.
    # Empty (compose's `${VAR:-}`) means "unset" to the resolver.
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN", "")
    token_file = tmp_path / "qobuz_token"
    token_file.write_text("tok-from-file\n")
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN_FILE", str(token_file))
    assert cfg._resolve_secret("QOBUZ_USER_AUTH_TOKEN") == "tok-from-file"
    # Must NOT leak the secret into the process environment.
    assert os.environ.get("QOBUZ_USER_AUTH_TOKEN") == ""


def test_storage_roots_must_be_separate_non_nested_directories(
        tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", music / "staging")
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")

    with pytest.raises(ValueError, match="separate, non-nested"):
        cfg.validate_storage_roots()
    # A symlink to the music folder is the music folder.
    staging_alias = tmp_path / "staging-alias"
    staging_alias.symlink_to(music, target_is_directory=True)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging_alias)
    with pytest.raises(ValueError, match="separate, non-nested"):
        cfg.validate_storage_roots()
