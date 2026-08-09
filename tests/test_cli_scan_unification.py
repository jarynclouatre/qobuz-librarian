import re
from pathlib import Path
from types import SimpleNamespace

import pytest


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

    monkeypatch.setattr(new_releases, "load_qobuz_token",
                        lambda: ("uid", "tok"))
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

    monkeypatch.setattr(new_releases, "load_qobuz_token", lambda: ("uid", "tok"))
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

    monkeypatch.setattr(new_releases, "load_qobuz_token", lambda: ("uid", "tok"))
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


@pytest.mark.parametrize(
    "result",
    [
        {"result": "failed", "imported": False},
        {
            "result": "downloaded",
            "imported": True,
            "upgrade_unverified": True,
            "catalogue_unverified": True,
        },
        {
            "result": "downloaded",
            "imported": True,
            "quality_verdict": {"under": True, "recovered": False},
        },
        {
            "result": "downloaded",
            "imported": True,
            "recovery_unverified": True,
        },
        {
            "result": "already_complete",
            "consolidation_interrupted": True,
        },
    ],
)
def test_cli_album_attention_returns_nonzero(monkeypatch, result):
    from qobuz_librarian.modes import album

    args = SimpleNamespace(query=["Artist", "Album"], yes=True)
    monkeypatch.setattr(album, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(album, "resolve_album_from_args",
                        lambda *_args: {"id": "album-id"})
    monkeypatch.setattr(album, "_download_album_now",
                        lambda *_args, **_kwargs: result)

    assert album.run_album_mode(args, "tok") == 1


def test_album_gap_walk_fails_when_download_work_is_retained(
        tmp_path, monkeypatch):
    from qobuz_librarian.modes import walk

    artist = tmp_path / "Artist"
    artist.mkdir()
    args = SimpleNamespace(consolidate=True, dry_run=False, yes=True)
    saved_queues = []
    cleared = []
    answers = iter(["", ""])

    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))
    monkeypatch.setattr(walk, "list_library_artists", lambda: [artist])
    monkeypatch.setattr(walk, "list_artist_album_dirs", lambda *_args: [])
    monkeypatch.setattr(walk, "load_album_walk_seen", lambda: set())
    monkeypatch.setattr(walk, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(walk, "_consolidation_disabled_notice",
                        lambda *_args: None)
    monkeypatch.setattr(
        walk,
        "_queue_saver",
        lambda _mode: lambda items: saved_queues.append(list(items)),
    )

    def queue_one_album(*_args, shared_queue, **_kwargs):
        shared_queue.append({"album": "pending"})
        return ([],)

    monkeypatch.setattr(walk, "run_artist_gap_fill", queue_one_album)
    monkeypatch.setattr(
        walk,
        "_execute_download_queue",
        lambda *_args, **_kwargs: ([{"result": "failed", "n_ok": 0}], False),
    )
    monkeypatch.setattr(walk, "clear_pending_queue",
                        lambda: cleared.append(True))

    assert walk.run_album_walk_mode(args, "tok") == 1
    assert saved_queues[-1] == [{"album": "pending"}]
    assert cleared == []


def test_library_walk_fails_when_a_download_is_only_partial(
        tmp_path, monkeypatch):
    from qobuz_librarian.modes import walk

    artist = tmp_path / "Artist"
    artist.mkdir()
    args = SimpleNamespace(
        consolidate=True,
        dry_run=False,
        no_catalog=True,
        yes=False,
    )
    answers = iter(["", "y"])

    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))
    monkeypatch.setattr(walk, "list_library_artists", lambda: [artist])
    monkeypatch.setattr(walk, "load_walk_seen", lambda: set())
    monkeypatch.setattr(walk, "_flush_stdin", lambda: None)
    monkeypatch.setattr(walk, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(walk, "record_walk_seen", lambda *_args: None)
    monkeypatch.setattr(walk, "_consolidation_disabled_notice",
                        lambda *_args: None)
    monkeypatch.setattr(walk, "confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(walk, "_queue_saver", lambda _mode: lambda _items: None)

    def queue_one_album(*_args, shared_queue, **_kwargs):
        shared_queue.append({"album": "partial"})
        return ([], [], set(), {}, "artist-id", [])

    def finish_partially(queue, *_args, **_kwargs):
        queue.clear()
        return [{"result": "partial", "n_ok": 1}], True

    monkeypatch.setattr(walk, "run_artist_gap_fill", queue_one_album)
    monkeypatch.setattr(walk, "_execute_download_queue", finish_partially)
    monkeypatch.setattr(walk, "clear_pending_queue", lambda: None)

    assert walk.run_walk_queued_mode(args, "tok") == 1


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param("failed", id="failed-result"),
        pytest.param(KeyboardInterrupt(), id="interrupted"),
    ],
)
def test_library_repair_sweep_returns_nonzero_for_unfinished_work(
        tmp_path, monkeypatch, outcome):
    from qobuz_librarian.modes import repair

    artist = tmp_path / "Artist"
    album_dir = artist / "One"
    album_dir.mkdir(parents=True)
    args = SimpleNamespace(no_upgrade=False, yes=True)

    monkeypatch.setattr(repair, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(repair, "_prompt_library_album_for_repair",
                        lambda *_args: ("__ALL__", None))
    monkeypatch.setattr(repair, "_all_library_album_dirs",
                        lambda: [(artist, album_dir)])

    def scan(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(repair, "_scan_report_repair", scan)

    assert repair.run_album_repair_mode(args, "tok") == 1
    assert args.no_upgrade is False


def test_cli_artist_failure_returns_nonzero(tmp_path, monkeypatch, caplog):
    from qobuz_librarian.modes import artist

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    args = SimpleNamespace(consolidate=False, yes=True, no_catalog=True)

    monkeypatch.setattr(artist, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(artist, "banner", lambda *_args, **_kwargs: None)

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        assert artist.run_artist_mode("Various Artists", args, "tok") == 1
    assert any(record.levelname == "WARNING" for record in caplog.records)
    caplog.clear()

    monkeypatch.setattr(artist, "resolve_artist_dir", lambda _name: artist_dir)
    monkeypatch.setattr(
        artist,
        "run_artist_gap_fill",
        lambda *_args, **_kwargs: (
            [{"result": "failed", "imported": False}],
            {},
            set(),
            set(),
            "artist-id",
            [],
        ),
    )

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        assert artist.run_artist_mode("Artist", args, "tok") == 1

    assert "failed:" in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)

    caplog.clear()
    args.no_catalog = False
    monkeypatch.setattr(
        artist,
        "run_artist_gap_fill",
        lambda *_args, **_kwargs: ([], {}, set(), set(), "artist-id", []),
    )
    monkeypatch.setattr(
        artist,
        "run_artist_missing_albums",
        lambda *_args, **_kwargs: (0, True),
    )

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        assert artist.run_artist_mode("Artist", args, "tok") == 1
    assert any(
        record.levelname == "WARNING" and "Step 2" in record.getMessage()
        for record in caplog.records
    )


def test_direct_artist_dispatch_propagates_the_mode_exit_code(monkeypatch):
    from qobuz_librarian import cli
    from qobuz_librarian.api import auth as api_auth
    from qobuz_librarian.library import flac_cache, repair_cache
    from qobuz_librarian.modes import artist
    from qobuz_librarian.queue.startup_recovery import StartupRecoveryStatus
    from qobuz_librarian.web import settings_store

    args = SimpleNamespace(
        verbose=False,
        quiet=False,
        no_color=False,
        reset_walk_seen=False,
        dry_run=False,
        migrate=False,
        lyrics_walk=False,
        check_new_releases=False,
        downsample_walk=False,
        artist="Artist",
        library_walk=False,
        album_gaps=False,
        repair=False,
        upgrade_walk=False,
        query=[],
        consolidate=False,
    )
    monkeypatch.setattr(settings_store, "load", lambda: None)
    monkeypatch.setattr(cli, "parse_args", lambda: args)
    monkeypatch.setattr(cli, "attach_file_handler", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.cfg, "validate_storage_roots", lambda: None)
    monkeypatch.setattr(cli, "banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "acquire_run_lock", lambda: object())
    monkeypatch.setattr(
        cli,
        "_startup_recovery_status",
        lambda: StartupRecoveryStatus.CLEAR,
    )
    monkeypatch.setattr(cli, "cleanup_old_upgrade_backups", lambda: 0)
    monkeypatch.setattr(cli, "_prune_lyric_state_orphans", lambda: None)
    monkeypatch.setattr(flac_cache, "prune_missing", lambda: 0)
    monkeypatch.setattr(repair_cache, "prune_expired", lambda: 0)
    monkeypatch.setattr(cli, "check_rip", lambda: None)
    monkeypatch.setattr(cli, "check_media_tools", lambda: None)
    monkeypatch.setattr(cli, "require_music_root", lambda: None)
    monkeypatch.setattr(api_auth, "verify_streamrip_downloads_folder", lambda: None)
    monkeypatch.setattr(api_auth, "sync_streamrip_creds_from_env", lambda: True)
    monkeypatch.setattr(cli, "load_qobuz_token", lambda: ("uid", "tok"))
    monkeypatch.setattr(cli, "streamrip_quality_cap", lambda: (24, 192000))
    monkeypatch.setattr(artist, "run_artist_mode", lambda *_args: 1)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
