import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian.api.auth import credentials_from_values


@pytest.mark.parametrize("user_id", ["", "user"])
def test_cli_repair_scan_uses_catalogue_access_until_replacement(
        monkeypatch, user_id):
    from qobuz_librarian import cli
    from qobuz_librarian.api import auth, client
    from qobuz_librarian.api.auth import DownloaderNotReady, QobuzAccess
    from qobuz_librarian.library import flac_cache, repair_cache
    from qobuz_librarian.modes import repair
    from qobuz_librarian.web import settings_store

    accesses = []
    credentials = credentials_from_values(user_id, "token", source="streamrip")

    def authorize(access, **kwargs):
        accesses.append(access)
        if access is QobuzAccess.DOWNLOAD_ACTION:
            raise DownloaderNotReady
        return credentials

    monkeypatch.setattr(sys, "argv", ["qobuz-librarian", "--repair", "--dry-run"])
    monkeypatch.setattr(settings_store, "load", lambda: None)
    monkeypatch.setattr(cli.cfg, "validate_storage_roots", lambda: None)
    monkeypatch.setattr(cli, "attach_file_handler", lambda *a, **k: None)
    monkeypatch.setattr(cli, "banner", lambda *a, **k: None)
    monkeypatch.setattr(cli, "acquire_run_lock", lambda: object())
    monkeypatch.setattr(cli, "cleanup_old_upgrade_backups", lambda: 0)
    monkeypatch.setattr(cli, "_prune_lyric_state_orphans", lambda: None)
    monkeypatch.setattr(flac_cache, "prune_missing", lambda: None)
    monkeypatch.setattr(repair_cache, "prune_expired", lambda: None)
    monkeypatch.setattr(
        cli,
        "check_rip",
        lambda: (_ for _ in ()).throw(
            AssertionError("catalogue-only Repair must not invoke streamrip")
        ),
    )
    monkeypatch.setattr(
        cli,
        "check_media_tools",
        lambda: (_ for _ in ()).throw(
            AssertionError("catalogue-only Repair must not need download tools")
        ),
    )
    monkeypatch.setattr(cli, "require_music_root", lambda: None)
    monkeypatch.setattr(
        auth,
        "verify_streamrip_downloads_folder",
        lambda: (_ for _ in ()).throw(
            AssertionError("catalogue-only Repair must not inspect streamrip")
        ),
    )
    monkeypatch.setattr(
        auth,
        "sync_streamrip_creds_from_env",
        lambda: (_ for _ in ()).throw(
            AssertionError("catalogue-only Repair must not configure streamrip")
        ),
    )
    monkeypatch.setattr(cli, "streamrip_quality_cap", lambda: (24, 192000))
    monkeypatch.setattr(client, "authorize_qobuz_action", authorize)
    monkeypatch.setattr(repair, "run_album_repair_mode", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_STARTUP_RECOVERY_RESULT", None)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert accesses == [QobuzAccess.CATALOGUE_ACTION]


def _bind_current_upgrade(monkeypatch, upgrade, state):
    state["generation"] = 1
    state["revision"] = 2
    state["quality_signature"] = upgrade.upgrade_state.quality_signature()
    monkeypatch.setattr(
        upgrade.generation_state,
        "load",
        lambda: {
            "generation": 1,
            "outputs": {
                "upgrade": {
                    "generation": 1,
                    "revision": 2,
                    "status": "current",
                    "complete": True,
                },
            },
        },
    )


def _accept_upgrade_candidate_premises(monkeypatch, upgrade):
    monkeypatch.setattr(
        upgrade.candidate_premise,
        "validate",
        lambda _candidate: {"kind": "upgrade", "receipt": {"test": True}},
    )


def test_upgrade_walk_ignores_hidden_saved_candidates(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.modes import upgrade

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    hidden.hide(hidden.SCOPE_UPGRADE, [("Artist", "Hidden", "")])
    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )
    state = {
        "complete": True,
        "candidates": [
            {
                "artist": "Artist",
                "title": "Hidden",
                "detail": "CD -> 24-bit / 96 kHz",
                "payload": {
                    "album_id": "hidden",
                    "title_similarity": 1.0,
                    "needed_edition_swap": False,
                },
            },
            {
                "artist": "Artist",
                "title": "Visible",
                "detail": "CD -> 24-bit / 96 kHz",
                "payload": {
                    "album_id": "visible",
                    "title_similarity": 1.0,
                    "needed_edition_swap": False,
                },
            },
        ],
    }
    _bind_current_upgrade(monkeypatch, upgrade, state)
    _accept_upgrade_candidate_premises(monkeypatch, upgrade)
    processed = []

    monkeypatch.setattr(upgrade.upgrade_state, "load", lambda: state)
    monkeypatch.setattr(upgrade, "get_album",
                        lambda album_id, token: {"id": album_id, "title": album_id})
    monkeypatch.setattr(upgrade, "process_album",
                        lambda album, *a, **kw: processed.append(album["id"])
                        or {"imported": True})
    monkeypatch.setattr(upgrade, "find_album_dir_filesystem", lambda album: None)
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_: None)

    upgrade.run_upgrade_walk_mode(args, "tok")

    assert processed == ["visible"]


def test_upgrade_walk_refuses_a_changed_saved_candidate(
        monkeypatch, tmp_path, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library.candidate_premise import capture
    from qobuz_librarian.modes import upgrade

    root = tmp_path / "music"
    album_dir = root / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    track = album_dir / "01.flac"
    track.write_bytes(b"reviewed audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    premise = capture("upgrade", album_dir)
    state = {
        "complete": True,
        "candidates": [{
            "artist": "Artist",
            "title": "Album",
            "detail": "CD -> 24-bit / 96 kHz",
            "payload": {
                "album_id": "alb-1",
                "album_dir": str(album_dir),
                "_premise": premise,
                "title_similarity": 1.0,
                "needed_edition_swap": False,
            },
        }],
    }
    _bind_current_upgrade(monkeypatch, upgrade, state)
    monkeypatch.setattr(upgrade.upgrade_state, "load", lambda: state)
    monkeypatch.setattr(upgrade.hidden_mod, "load", lambda: {})
    monkeypatch.setattr(
        upgrade,
        "get_album",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a stale candidate must be refused before Qobuz")
        ),
    )
    monkeypatch.setattr(
        upgrade,
        "process_album",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a stale candidate must not reach file work")
        ),
    )
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_args: None)
    track.write_bytes(b"changed after the Library review")

    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )
    with caplog.at_level("INFO", logger="qobuz_librarian"):
        result = upgrade.run_upgrade_walk_mode(args, "token")

    assert result == upgrade.EXIT_GENERAL
    assert "changed after the Library refresh" in caplog.text


def test_upgrade_walk_carries_the_reviewed_receipt_to_the_backup_gate(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import upgrade

    root = tmp_path / "music"
    album_dir = root / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    state = {
        "complete": True,
        "candidates": [{
            "artist": "Artist",
            "title": "Album",
            "detail": "CD -> 24-bit / 96 kHz",
            "payload": {
                "album_id": "alb-1",
                "album_dir": str(album_dir),
                "_premise": {"proof": "saved"},
                "title_similarity": 1.0,
                "needed_edition_swap": False,
            },
        }],
    }
    _bind_current_upgrade(monkeypatch, upgrade, state)
    receipt = {"tree": "reviewed"}
    monkeypatch.setattr(upgrade.upgrade_state, "load", lambda: state)
    monkeypatch.setattr(upgrade.hidden_mod, "load", lambda: {})
    monkeypatch.setattr(
        upgrade.candidate_premise,
        "validate",
        lambda _candidate: {"kind": "upgrade", "receipt": receipt},
    )
    monkeypatch.setattr(
        upgrade,
        "get_album",
        lambda *_args, **_kwargs: {"id": "alb-1", "title": "Album"},
    )
    observed = []

    def process(*_args, **kwargs):
        observed.append(kwargs.get("expected_album_receipt"))
        return {"result": "upgrade_only_no_op", "imported": False}

    monkeypatch.setattr(upgrade, "process_album", process)
    monkeypatch.setattr(
        upgrade,
        "_refresh_saved_state_after_upgrade",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_args: None)
    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )

    assert upgrade.run_upgrade_walk_mode(args, "token") == 0
    assert observed == [receipt]


def test_upgrade_walk_refuses_incomplete_saved_state(monkeypatch, caplog):
    from qobuz_librarian.modes import upgrade

    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )
    monkeypatch.setattr(
        upgrade.upgrade_state,
        "load",
        lambda: {
            "complete": False,
            "candidates": [{
                "artist": "Artist",
                "title": "Partial",
                "detail": "stale",
                "payload": {"album_id": "alb-1"},
            }],
        },
    )
    monkeypatch.setattr(
        upgrade,
        "get_album",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("incomplete saved state should not be processed")),
    )

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        result = upgrade.run_upgrade_walk_mode(args, "tok")

    assert result == upgrade.EXIT_GENERAL
    assert "Run a Library refresh first." in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_upgrade_walk_refreshes_saved_state_after_success(tmp_path, monkeypatch):
    from qobuz_librarian.modes import upgrade

    artist_dir = tmp_path / "Artist"
    (artist_dir / "Album").mkdir(parents=True)
    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )
    state = {
        "complete": True,
        "candidates": [{
            "artist": "Artist",
            "title": "Album",
            "detail": "CD -> 24-bit / 96 kHz",
            "payload": {
                "album_id": "alb-1",
                "title_similarity": 1.0,
                "needed_edition_swap": False,
            },
        }],
    }
    _bind_current_upgrade(monkeypatch, upgrade, state)
    _accept_upgrade_candidate_premises(monkeypatch, upgrade)
    refreshed_upgrade = []
    refreshed_downsample = []
    badge_calls = []

    upgrade_loads = [state, {"candidates": []}]
    monkeypatch.setattr(
        upgrade.upgrade_state,
        "load",
        lambda: upgrade_loads.pop(0) if upgrade_loads else {"candidates": []},
    )
    monkeypatch.setattr(upgrade, "get_album",
                        lambda album_id, token: {"id": album_id, "title": "Album"})
    monkeypatch.setattr(upgrade, "process_album",
                        lambda album, *a, **kw: {"imported": True, "dir": artist_dir / "Album"})
    monkeypatch.setattr(upgrade, "load_capped", lambda: {})
    monkeypatch.setattr(upgrade.upgrade_state, "update_artist",
                        lambda ad, **kwargs: refreshed_upgrade.append(ad.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(upgrade.downsample_state, "update_artist",
                        lambda ad, **kwargs: refreshed_downsample.append(ad.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(upgrade.downsample_state, "load", lambda: {"candidates": []})
    monkeypatch.setattr(upgrade.review_badges, "set_ready",
                        lambda surface, ready: badge_calls.append((surface, ready)))
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_: None)

    upgrade.run_upgrade_walk_mode(args, "tok")

    assert refreshed_upgrade == ["Artist"]
    assert refreshed_downsample == ["Artist"]
    assert ("upgrade", False) in badge_calls
    assert ("downsample", False) in badge_calls


def test_upgrade_walk_marks_partial_cap_before_refresh(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import upgrade
    from qobuz_librarian.quality import decision

    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 192,
    }
    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )
    state = {
        "complete": True,
        "candidates": [{
            "artist": "Artist",
            "title": "Album",
            "detail": "CD -> 24-bit / 96 kHz",
            "payload": {
                "album_id": "alb-1",
                "title_similarity": 1.0,
                "needed_edition_swap": False,
            },
        }],
    }
    _bind_current_upgrade(monkeypatch, upgrade, state)
    _accept_upgrade_candidate_premises(monkeypatch, upgrade)
    refreshed_upgrade = []

    upgrade_loads = [state, {"candidates": []}]
    monkeypatch.setattr(
        upgrade.upgrade_state,
        "load",
        lambda: upgrade_loads.pop(0) if upgrade_loads else {"candidates": []},
    )
    monkeypatch.setattr(upgrade, "get_album", lambda album_id, token: album)
    monkeypatch.setattr(upgrade, "process_album",
                        lambda album, *a, **kw: {
                            "imported": True,
                            "result": "downloaded",
                            "dir": album_dir,
                            "quality_verdict": {
                                "under": True,
                                "recovered": False,
                                "n_below": 1,
                            },
                        })
    def refresh_upgrade(ad, **kwargs):
        assert decision.is_album_capped("alb-1", decision.load_capped())
        refreshed_upgrade.append(ad.name)
        return SimpleNamespace(complete=True, candidates=[])

    monkeypatch.setattr(upgrade.upgrade_state, "update_artist", refresh_upgrade)
    monkeypatch.setattr(upgrade.downsample_state, "update_artist",
                        lambda ad, **kwargs: SimpleNamespace(
                            complete=True, candidates=[]))
    monkeypatch.setattr(upgrade.downsample_state, "load", lambda: {"candidates": []})
    monkeypatch.setattr(upgrade.review_badges, "set_ready", lambda *a, **k: None)
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_: None)

    upgrade.run_upgrade_walk_mode(args, "tok")

    assert refreshed_upgrade == ["Artist"]


def test_upgrade_walk_reports_a_retained_backup(monkeypatch, caplog):
    from qobuz_librarian.modes import upgrade

    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )
    state = {
        "complete": True,
        "candidates": [{
            "artist": "Artist",
            "title": "Unflushed replacement",
            "detail": "CD -> 24-bit / 96 kHz",
            "payload": {"album_id": "alb-1"},
        }],
    }
    _bind_current_upgrade(monkeypatch, upgrade, state)
    _accept_upgrade_candidate_premises(monkeypatch, upgrade)
    monkeypatch.setattr(upgrade.upgrade_state, "load", lambda: state)
    monkeypatch.setattr(upgrade.hidden_mod, "load", lambda: {})
    monkeypatch.setattr(
        upgrade, "get_album", lambda *_a, **_k: {"id": "alb-1"}
    )
    outcome = {
        "result": "cancelled",
        "imported": False,
        "upgrade_unverified": True,
    }
    monkeypatch.setattr(
        upgrade, "process_album", lambda *_a, **_k: dict(outcome)
    )
    refreshed = []
    monkeypatch.setattr(
        upgrade,
        "_refresh_saved_state_after_upgrade",
        lambda *_a, **_k: refreshed.append(True),
    )
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_a: None)

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        result = upgrade.run_upgrade_walk_mode(args, "tok")

    assert result == upgrade.EXIT_GENERAL
    assert refreshed == []
    assert "upgraded tracks in 0 albums" in caplog.text
    assert "kept a safety backup" in caplog.text


def test_upgrade_walk_refuses_when_upgrade_disabled(monkeypatch, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import upgrade

    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(
        upgrade.upgrade_state,
        "load",
        lambda: (_ for _ in ()).throw(
            AssertionError("disabled upgrade mode should not load state")),
    )

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        upgrade.run_upgrade_walk_mode(args, "tok")

    assert "Upgrade scanning is turned off." in caplog.text


def test_cli_new_release_check_refuses_without_baseline(monkeypatch):
    from qobuz_librarian.modes import new_releases

    monkeypatch.setattr(
        new_releases,
        "authorize_qobuz_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Qobuz should not be checked without a baseline")
        ),
    )
    monkeypatch.setattr(new_releases.new_releases_mod,
                        "is_baseline_complete", lambda: False)
    monkeypatch.setattr(new_releases, "list_library_artists",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("new-release crawl should not start")))

    with pytest.raises(SystemExit):
        new_releases.run_check_new_releases_mode(
            SimpleNamespace(dry_run=False))


@pytest.mark.parametrize(
    ("dry_run", "save_result", "expected"),
    ((True, None, "no changes saved"),
     (False, False, "couldn't be saved")),
)
def test_cli_new_release_summary_matches_persistence(
        tmp_path, monkeypatch, caplog, dry_run, save_result, expected):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import new_releases

    artist = tmp_path / "Artist"
    artist.mkdir()
    saved = []

    monkeypatch.setattr(
        new_releases,
        "authorize_qobuz_action",
        lambda *_args, **_kwargs: credentials_from_values("uid", "tok"),
    )
    monkeypatch.setattr(new_releases.new_releases_mod,
                        "is_baseline_complete", lambda: True)
    monkeypatch.setattr(new_releases, "list_library_artists", lambda: [artist])
    monkeypatch.setattr(new_releases.new_releases_mod, "load", lambda: {
        "seen": {"artist-id": ["old"]},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT) - 1,
    })
    monkeypatch.setattr(
        new_releases,
        "find_new_releases_for_artist",
        lambda *_args, **_kwargs: SimpleNamespace(
            artist_id="artist-id",
            fetch_failed=False,
            current_ids=["old", "new"],
            new_gaps=[],
            artist_name="Artist",
        ),
    )
    monkeypatch.setattr(
        new_releases.new_releases_mod,
        "mark_run",
        lambda *_args, **_kwargs: saved.append(True) or save_result,
    )

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        exit_code = new_releases.run_check_new_releases_mode(
            SimpleNamespace(dry_run=dry_run))

    assert expected in caplog.text
    assert bool(saved) is not dry_run
    assert exit_code == (1 if save_result is False else 0)


def test_cli_new_release_check_fails_when_an_artist_was_not_checked(
        tmp_path, monkeypatch, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import new_releases
    from qobuz_librarian.ui_cli import logging as cli_logging

    artist = tmp_path / "Artist"
    artist.mkdir()

    monkeypatch.setattr(
        new_releases,
        "authorize_qobuz_action",
        lambda *_args, **_kwargs: credentials_from_values("uid", "tok"),
    )
    monkeypatch.setattr(new_releases.new_releases_mod,
                        "is_baseline_complete", lambda: True)
    monkeypatch.setattr(new_releases, "list_library_artists", lambda: [artist])
    monkeypatch.setattr(new_releases.new_releases_mod, "load", lambda: {
        "seen": {"artist-id": ["old"]},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT),
    })
    monkeypatch.setattr(
        new_releases,
        "find_new_releases_for_artist",
        lambda *_args, **_kwargs: SimpleNamespace(
            artist_id="artist-id",
            fetch_failed=True,
            current_ids=[],
            new_gaps=[],
            artist_name="Artist",
        ),
    )
    monkeypatch.setattr(new_releases.new_releases_mod, "mark_run",
                        lambda *_args, **_kwargs: True)

    cli_logging.set_quiet(True)
    try:
        with caplog.at_level("INFO", logger="qobuz_librarian"):
            exit_code = new_releases.run_check_new_releases_mode(
                SimpleNamespace(dry_run=False))
    finally:
        cli_logging.set_quiet(False)

    assert exit_code == 1
    assert any(
        record.levelname == "WARNING" and "incomplete" in record.getMessage()
        for record in caplog.records
    )


def test_cli_file_changes_keep_generation_state_truthful(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import (
        downsample_state,
        generation_state,
        hidden,
        library_scan_state,
        new_releases,
    )
    from qobuz_librarian.quality import decision, upgrade_state
    from qobuz_librarian.queue import executor
    from qobuz_librarian.web import review_badges

    monkeypatch.setattr(
        cfg,
        "LIBRARY_GENERATION_STATE_FILE",
        tmp_path / "generation.json",
    )
    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library.json")
    monkeypatch.setattr(cfg, "NEW_RELEASE_STATE_FILE", tmp_path / "releases.json")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    monkeypatch.setattr(
        cfg,
        "REVIEW_BADGE_STATE_FILE",
        tmp_path / "badges.json",
    )

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")
    album = {
        "id": "album-id",
        "artist": {"id": "artist-id", "name": "Artist"},
    }

    stale_attempt = generation_state.begin_attempt()
    scan_started_revision = generation_state.revision()
    executor._refresh_review_state_after_downloads(
        [{
            "album": album,
            "dir": album_dir,
            "imported": True,
            "n_ok": 1,
        }],
        "token",
        SimpleNamespace(),
    )
    assert generation_state.commit_catalog_generation(
        stale_attempt,
        expected_revision=scan_started_revision,
    ) is None
    assert generation_state.finish_attempt(
        stale_attempt,
        "incomplete",
        "A newer local file change superseded this crawl.",
    )

    attempt = generation_state.begin_attempt()
    publication = generation_state.commit_catalog_generation(attempt)
    generation = publication["generation"]
    library_revision = generation_state.reserve_revision()
    assert library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": "before-download",
                "candidates": [{
                    "artist": "Artist",
                    "title": "Album",
                    "payload": {"album_id": "album-id"},
                }],
                "artist_id": "artist-id",
                "catalog_ids": ["album-id"],
            },
        },
        complete=True,
        generation=generation,
        revision=library_revision,
    )
    assert new_releases.seed_baseline(
        {"artist-id": []},
        generation=generation,
    )
    assert upgrade_state.save(upgrade_state.RefreshResult(
        [],
        ["Artist"],
        {},
        True,
        {"Artist": "before-download"},
        quality_signature=upgrade_state.quality_signature(),
    ), generation=generation)
    assert downsample_state.save(downsample_state.RefreshResult(
        [],
        ["Artist"],
        {},
        True,
        {"Artist": "before-download"},
    ), generation=generation)
    assert all(
        generation_state.output_is_current(surface)
        for surface in generation_state.SURFACES
    )

    monkeypatch.setattr(hidden, "load", lambda: {})
    monkeypatch.setattr(decision, "load_capped", lambda: {})
    original_upgrade = upgrade_state.update_artist
    original_downsample = downsample_state.update_artist
    monkeypatch.setattr(
        upgrade_state,
        "update_artist",
        lambda artist_dir, **kwargs: original_upgrade(
            artist_dir,
            scan_artist=lambda _path: [],
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        downsample_state,
        "update_artist",
        lambda artist_dir, **kwargs: original_downsample(
            artist_dir,
            scan_artist=lambda _path: [],
            **kwargs,
        ),
    )
    monkeypatch.setattr(review_badges, "set_ready", lambda *_args: None)

    executor._refresh_review_state_after_downloads(
        [{
            "album": album,
            "dir": tmp_path / "old-filing" / "Album",
            "_resolved_post_dir": album_dir,
            "imported": True,
            "n_ok": 1,
        }],
        "token",
        SimpleNamespace(),
    )

    assert all(
        generation_state.output_is_current(surface)
        for surface in generation_state.SURFACES
    )
    candidates = library_scan_state.kind_state("missing")["artists"][
        "Artist"
    ]["candidates"]
    assert candidates == []
    assert new_releases.load()["seen"]["artist-id"] == ["album-id"]
    assert upgrade_state.load()["fingerprints"]["Artist"] != "before-download"
    assert downsample_state.load()["fingerprints"]["Artist"] != "before-download"

    executor._refresh_review_state_after_downloads(
        [{
            "album": album,
            "dir": album_dir,
            "imported": True,
            "n_ok": 1,
        }],
        "expired-token",
        SimpleNamespace(),
        allow_upgrade_refresh=False,
    )

    assert generation_state.output_state("upgrade")["status"] == "stale"
    assert generation_state.output_is_current("downsample")
    assert upgrade_state.save(upgrade_state.RefreshResult(
        [],
        ["Artist"],
        {},
        True,
        {"Artist": "after-auth-loss"},
        quality_signature=upgrade_state.quality_signature(),
    ), generation=generation)

    executor._refresh_review_state_after_downloads(
        [{
            "album": None,
            "dir": album_dir,
            "imported": True,
            "n_ok": 1,
        }],
        "token",
        SimpleNamespace(),
    )

    assert generation_state.output_state("library")["status"] == "stale"
    assert generation_state.output_state("new_releases")["status"] == "stale"
    assert generation_state.output_is_current("upgrade")
    assert generation_state.output_is_current("downsample")


@pytest.mark.parametrize("durable", [False, True])
def test_direct_cli_download_routes_success_to_saved_state(
        tmp_path, monkeypatch, durable):
    from qobuz_librarian.modes import album as album_mode

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "album-id",
        "title": "Album",
        "artist": {"id": "artist-id", "name": "Artist"},
        "tracks": {"items": [{"id": "track-id"}]},
    }
    result = {
        "dir": album_dir,
        "result": "downloaded",
        "imported": True,
        "n_ok": 1,
    }
    refreshed = []
    args = SimpleNamespace(
        dry_run=False,
        force=False,
        yes=True,
        no_import=False,
    )

    monkeypatch.setattr(album_mode, "is_lossless_album", lambda _album: durable)
    monkeypatch.setattr(album_mode, "process_album", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        album_mode,
        "_refresh_review_state_after_downloads",
        lambda changes, token, run_args: refreshed.append(
            (changes, token, run_args)
        ),
        raising=False,
    )
    if durable:
        candidate = {"album": album}
        monkeypatch.setattr(album_mode, "find_existing_tracks", lambda _album: ({}, None))
        monkeypatch.setattr(
            album_mode,
            "compute_missing",
            lambda _tracks, _existing: (album["tracks"]["items"], []),
        )
        monkeypatch.setattr(
            album_mode,
            "_build_queue_item",
            lambda **_kwargs: candidate,
        )
        monkeypatch.setattr(
            album_mode, "plan_durable_new_album", lambda *_args: object()
        )
        monkeypatch.setattr(
            album_mode,
            "_admit_new_queue_items",
            lambda _items, bound_token: bound_token,
        )
        monkeypatch.setattr(album_mode, "staging_preflight", lambda _args: None)
        monkeypatch.setattr(album_mode, "save_pending_queue", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(album_mode, "clear_pending_queue", lambda: None)
        monkeypatch.setattr(
            album_mode,
            "_execute_download_queue",
            lambda *_args, **_kwargs: ([result], True),
        )

    observed = album_mode._download_album_now(album, args, "token")

    assert observed == result
    assert len(refreshed) == 1
    changes, token, run_args = refreshed[0]
    assert token == "token"
    assert run_args is args
    assert changes == [{**result, "album": album}]


def test_direct_cli_download_admits_before_saving_queue(monkeypatch):
    from qobuz_librarian.api import client
    from qobuz_librarian.api.auth import (
        DownloaderNotReady,
        credentials_from_values,
    )
    from qobuz_librarian.modes import album as album_mode
    from qobuz_librarian.queue import executor

    album = {
        "id": "album-id",
        "title": "Album",
        "artist": {"id": "artist-id", "name": "Artist"},
        "tracks": {"items": [{"id": "track-id"}]},
    }
    candidate = {"album": album, "album_dir": None}
    args = SimpleNamespace(
        dry_run=False,
        force=False,
        yes=True,
        no_import=False,
    )
    side_effects = []

    monkeypatch.setattr(album_mode, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(
        album_mode,
        "find_existing_tracks",
        lambda _album: ([], None),
    )
    monkeypatch.setattr(
        album_mode,
        "compute_missing",
        lambda tracks, _existing: (tracks, []),
    )
    monkeypatch.setattr(
        album_mode,
        "_build_queue_item",
        lambda **_kwargs: candidate,
    )
    monkeypatch.setattr(
        album_mode, "plan_durable_new_album", lambda *_args: object()
    )
    monkeypatch.setattr(album_mode, "print_album_summary", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        executor,
        "_validate_queue_item_premise",
        lambda _item: side_effects.append("premise"),
    )
    monkeypatch.setattr(
        album_mode,
        "staging_preflight",
        lambda _args: side_effects.append("preflight"),
    )
    monkeypatch.setattr(
        album_mode,
        "save_pending_queue",
        lambda *_args, **_kwargs: side_effects.append("save"),
    )
    monkeypatch.setattr(
        album_mode,
        "_execute_download_queue",
        lambda *_args, **_kwargs: side_effects.append("execute"),
    )
    monkeypatch.setattr(
        client,
        "authorize_qobuz_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DownloaderNotReady()
        ),
    )
    token = credentials_from_values(
        "",
        "token",
        source="streamrip",
    ).token

    with pytest.raises(DownloaderNotReady):
        album_mode._download_album_now(album, args, token)

    assert side_effects == ["premise"]


def test_interactive_cli_queue_admits_before_append(monkeypatch):
    from qobuz_librarian.api import client
    from qobuz_librarian.api.auth import (
        DownloaderNotReady,
        credentials_from_values,
    )
    from qobuz_librarian.modes import album as album_mode
    from qobuz_librarian.queue import executor

    album = {
        "id": "album-id",
        "title": "Album",
        "artist": {"id": "artist-id", "name": "Artist"},
        "tracks": {"items": [{"id": "track-id"}]},
    }
    queue = []
    events = []

    monkeypatch.setattr(
        album_mode,
        "find_existing_tracks",
        lambda _album: ([], None),
    )
    monkeypatch.setattr(
        album_mode,
        "compute_missing",
        lambda tracks, _existing: (tracks, []),
    )
    monkeypatch.setattr(album_mode, "print_album_summary", lambda *_a, **_kw: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    monkeypatch.setattr(
        executor,
        "_validate_queue_item_premise",
        lambda _item: events.append("premise"),
    )
    monkeypatch.setattr(
        client,
        "authorize_qobuz_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DownloaderNotReady()
        ),
    )
    token = credentials_from_values(
        "",
        "token",
        source="streamrip",
    ).token

    with pytest.raises(DownloaderNotReady):
        album_mode._interactive_album_action(
            album,
            SimpleNamespace(force=False),
            token,
            queue,
            lambda: events.append("flush"),
        )

    assert queue == []
    assert events == ["premise"]


def test_cli_repair_routes_success_to_saved_state(tmp_path, monkeypatch):
    from qobuz_librarian.modes import repair

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    candidate = {
        "path": str(album_dir / "01.flac"),
        "title": "Track",
        "local_title": "Track",
        "track_number": 1,
        "local_track_number": 1,
        "file_length": 1.0,
        "qobuz_duration": 2.0,
        "isrc": "USRC11111111",
        "qobuz_track": {"id": "track-id", "title": "Track", "version": ""},
    }
    result = {
        "imported": True,
        "n_ok": 1,
        "n_fail": 0,
        "backup": None,
    }
    refreshed = []

    monkeypatch.setattr(
        repair,
        "scan_dir_for_isrc_repairs",
        lambda *_args, **_kwargs: {
            "verified_truncated": [candidate],
            "verified_ok": 0,
            "isrc_no_match": [],
            "no_isrc_tag": [],
            "isrc_mismatch": [],
        },
    )
    monkeypatch.setattr(repair, "repair_album_dir", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        repair,
        "_refresh_review_state_after_downloads",
        lambda changes, token, run_args: refreshed.append(
            (changes, token, run_args)
        ),
        raising=False,
    )
    args = SimpleNamespace(dry_run=False, yes=True)

    status = repair._scan_report_repair(
        album_dir,
        "Artist",
        args,
        "token",
    )

    assert status == "repaired"
    assert refreshed == [
        ([{**result, "album": None, "dir": album_dir}], "token", args)
    ]


def test_cli_album_failure_returns_nonzero(monkeypatch):
    from qobuz_librarian.modes import album

    args = SimpleNamespace(query=["Artist", "Album"], yes=True)
    monkeypatch.setattr(album, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(album, "resolve_album_from_args",
                        lambda *_args: {"id": "album-id"})
    monkeypatch.setattr(album, "_download_album_now",
                        lambda *_args, **_kwargs: {
                            "result": "failed",
                            "imported": False,
                        })

    assert album.run_album_mode(args, "tok") == 1
