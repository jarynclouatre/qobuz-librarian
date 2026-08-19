"""Env-var validation: a bad value falls back loudly instead of surfacing
later as an opaque download/lyrics failure."""
import os
import subprocess
import sys

import pytest

import qobuz_librarian.config as cfg


@pytest.mark.parametrize(("raw", "expected", "warns"), [
    (" DEBUG ", "DEBUG", False),
    ("DEBUGGING", "INFO", True),
    ("BASIC_FORMAT", "INFO", True),
])
def test_log_level_is_normalised_before_file_handler_setup(raw, expected, warns):
    code = """
import tempfile
from pathlib import Path
from qobuz_librarian import config
from qobuz_librarian.ui_cli.logging import attach_file_handler

with tempfile.TemporaryDirectory() as directory:
    attach_file_handler(Path(directory) / "app.log", config.LOG_LEVEL)
    print(repr(config.LOG_LEVEL))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "LOG_LEVEL": raw},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == repr(expected)
    assert ("LOG_LEVEL=" in result.stderr) is warns


def test_env_choice_falls_back_on_unknown_value(monkeypatch):
    monkeypatch.setenv("LYRICS_FORMAT", "lrc")
    assert cfg._env_choice("LYRICS_FORMAT", "embed",
                           ("embed", "sidecar", "both")) == "embed"


def test_env_bool_empty_string_means_unset(monkeypatch):
    # compose's `${PREFER_HIRES:-}` resolves to "" - that must mean "use the
    # default", not silently flip the flag off.
    monkeypatch.setenv("PREFER_HIRES", "")
    assert cfg._env_bool("PREFER_HIRES", True) is True


def test_env_num_min_floors_a_sub_minimum_count(monkeypatch):
    # A 0 or negative worker count would crash the thread-pool constructor at
    # import time; clamp to the floor so a typo can't take down the web app.
    monkeypatch.setenv("ARTIST_SCAN_WORKERS", "0")
    assert cfg._env_num_min("ARTIST_SCAN_WORKERS", 4, 1) == 1


def test_numeric_settings_reject_non_finite_values(monkeypatch):
    for value in ("nan", "inf", "-inf"):
        monkeypatch.setenv("LYRICS_PROVIDER_TIMEOUT", value)
        assert cfg._env_num_min("LYRICS_PROVIDER_TIMEOUT", 30.0, 1.0) == 30.0


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


@pytest.mark.parametrize(("music", "staging", "backups"), [
    ("root", "root", "backups"),
    ("root", "root/staging", "backups"),
    ("root/staging", "root", "backups"),
    ("music", "staging", "music/backups"),
    ("music/backups", "staging", "music"),
    ("music", "scratch", "scratch/backups"),
    ("music", "scratch/backups", "scratch"),
])
def test_storage_roots_must_be_separate_non_nested_directories(
        tmp_path, monkeypatch, music, staging, backups):
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / music)
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / staging)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / backups)

    with pytest.raises(ValueError, match="separate, non-nested"):
        cfg.validate_storage_roots()




def test_storage_roots_compare_resolved_symlink_targets(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    staging_alias = tmp_path / "staging-alias"
    staging_alias.symlink_to(music, target_is_directory=True)
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging_alias)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")

    with pytest.raises(ValueError, match="separate, non-nested"):
        cfg.validate_storage_roots()
