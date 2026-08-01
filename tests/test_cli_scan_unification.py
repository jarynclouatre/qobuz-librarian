import re
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_upgrade_walk_uses_saved_state_and_reports_failed_work(
        monkeypatch, caplog):
    from qobuz_librarian.modes import upgrade

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
                "title": "Album One",
                "detail": "CD -> 24-bit / 96 kHz",
                "payload": {
                    "album_id": "alb-1",
                    "title_similarity": 1.0,
                    "needed_edition_swap": False,
                },
            },
            {
                "artist": "Artist",
                "title": "Album Two",
                "detail": "CD -> 24-bit / 96 kHz",
                "payload": {
                    "album_id": "alb-2",
                    "title_similarity": 1.0,
                    "needed_edition_swap": False,
                },
            },
        ],
    }
    processed = []

    upgrade_loads = [state, {"candidates": []}]
    monkeypatch.setattr(
        upgrade.upgrade_state,
        "load",
        lambda: upgrade_loads.pop(0) if upgrade_loads else {"candidates": []},
    )
    monkeypatch.setattr(upgrade, "get_album",
                        lambda album_id, token: {"id": album_id, "title": "Album"})
    monkeypatch.setattr(upgrade, "process_album",
                        lambda album, *a, **kw: processed.append(album["id"])
                        or {"imported": album["id"] == "alb-1"})
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_: None)

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        result = upgrade.run_upgrade_walk_mode(args, "tok")

    assert processed == ["alb-1", "alb-2"]
    assert result == upgrade.EXIT_GENERAL
    assert "Upgrade walk finished with errors." in caplog.text
    assert "Upgrade walk complete." not in caplog.text
    assert args.consolidate is True


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
        upgrade.run_upgrade_walk_mode(args, "tok")

    assert "Run a Library refresh first." in caplog.text


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


def test_lyrics_failures_are_reported_by_cli_and_web(monkeypatch, caplog):
    from contextlib import nullcontext

    from qobuz_librarian.library import lyrics as library_lyrics
    from qobuz_librarian.modes import lyrics as lyrics_mode
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as job_mod

    result = {
        "total": 10,
        "processed": 7,
        "wrote-synced": 1,
        "already-plain": 1,
        "not-found": 1,
        "skipped-tags": 1,
        "skipped-long": 1,
        "unsafe-path": 1,
        "providers-unavailable": 1,
    }
    args = SimpleNamespace(
        dry_run=False,
        lyrics_rescan=False,
        lyrics_synced_only=False,
    )
    monkeypatch.setattr(lyrics_mode, "HAVE_LYRICS", True)
    monkeypatch.setattr(lyrics_mode, "banner", lambda *_: None)
    monkeypatch.setattr(lyrics_mode, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(lyrics_mode, "run_library_lyrics", lambda **_kw: result)

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        exit_code = lyrics_mode.run_library_lyrics_mode(args)

    assert exit_code == lyrics_mode.EXIT_GENERAL
    assert "Lyrics pass finished with errors." in caplog.text
    assert "missing" in caplog.text and "longer than 20 minutes" in caplog.text
    assert "refused as unsafe" in caplog.text
    assert "Lyrics pass complete." not in caplog.text

    monkeypatch.setattr(library_lyrics, "HAVE_LYRICS", True)
    monkeypatch.setattr(
        library_lyrics, "run_library_lyrics", lambda **_kw: result)
    monkeypatch.setattr(job_mod, "staging_lock", lambda: nullcontext())
    monkeypatch.setattr(job_mod, "set_staging_holder", lambda *_: None)
    job = SimpleNamespace(
        cancel_requested=False,
        error="",
        status=job_mod.JobStatus.RUNNING,
        summary="",
    )

    flows.run_library_lyrics(job)

    assert job.status == job_mod.JobStatus.FAILED
    assert "1 track missing tags" in job.summary
    assert "1 track too long" in job.summary
    assert "1 unsafe path refused" in job.summary
    assert "3 skipped (already checked)" in job.summary


def test_lyrics_interrupt_reports_saved_partial_result(monkeypatch, caplog):
    import signal

    from qobuz_librarian.modes import lyrics as lyrics_mode

    args = SimpleNamespace(
        dry_run=False,
        lyrics_rescan=True,
        lyrics_synced_only=False,
    )

    def stopped_run(**kwargs):
        signal.raise_signal(signal.SIGINT)
        assert kwargs["should_stop"]()
        return {
            "total": 10,
            "processed": 2,
            "stop_total": 4,
            "stop_stage": "fetch",
            "stopped": 1,
            "wrote-synced": 1,
            "write-error": 1,
        }

    monkeypatch.setattr(lyrics_mode, "HAVE_LYRICS", True)
    monkeypatch.setattr(lyrics_mode, "banner", lambda *_: None)
    monkeypatch.setattr(lyrics_mode, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(lyrics_mode, "run_library_lyrics", stopped_run)

    with caplog.at_level("INFO", logger="qobuz_librarian"):
        exit_code = lyrics_mode.run_library_lyrics_mode(args)

    assert exit_code == lyrics_mode.EXIT_GENERAL
    assert "Lyrics pass stopped early." in caplog.text
    assert "2 of 4 tracks checked" in caplog.text
    assert "6 skipped (already checked)" in caplog.text
    assert "2 left for the next run" in caplog.text
    assert "1 track hit an error" in caplog.text
    assert "Lyrics pass complete." not in caplog.text


def test_cli_help_and_menu_fit_a_narrow_terminal(monkeypatch, capsys):
    import builtins
    import sys

    from qobuz_librarian import cli
    from qobuz_librarian.ui_cli import menu

    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setattr(cli.cfg, "STAGING_DIR", Path("/staging"))
    monkeypatch.setattr(cli, "_in_container", lambda: True)
    monkeypatch.setattr(
        cli, "_compose_service_name", lambda: "qobuz-librarian")
    monkeypatch.setattr(sys, "argv", ["qobuz-librarian", "--help"])
    with pytest.raises(SystemExit) as stopped:
        cli.parse_args()
    help_lines = capsys.readouterr().out.splitlines()

    assert stopped.value.code == 0
    assert max(map(len, help_lines)) <= 40
    assert any(line.rstrip().endswith("\\") for line in help_lines)
    help_text = " ".join(line.strip() for line in help_lines)
    assert "step 2" not in help_text
    assert help_text.count("missing album suggestions") == 3
    assert "docker compose run --rm" in help_text
    copyable_help = help_text.replace("\\ ", "")
    assert "qobuz-librarian beet import /staging" in copyable_help

    records = []
    monkeypatch.setattr(menu.log, "info", records.append)
    monkeypatch.setattr(
        builtins, "input", lambda *_: (_ for _ in ()).throw(EOFError))

    menu.interactive_session_mode()

    visible = [
        re.sub(r"\x1b\[[0-9;]*m", "", line)
        for record in records
        for line in str(record).splitlines()
    ]
    assert max(map(len, visible)) <= 40


def test_downsample_walk_reports_incomplete_and_updates_checked_artist(
        monkeypatch, caplog):
    from qobuz_librarian.library.downsample import DownsampleCandidate
    from qobuz_librarian.modes import downsample

    args = SimpleNamespace(dry_run=False, yes=False)
    artist_dir = Path("/music/Artist")
    denied_dir = Path("/music/Denied")
    candidate = DownsampleCandidate(
        album_dir=artist_dir / "Album",
        artist="Artist",
        title="Album",
        n_hires=1,
        n_flac=1,
        source_rates=[96000],
        target_rates=[48000],
        est_saving=123,
    )
    refresh_calls = []
    update_calls = []
    badge_calls = []

    def fake_refresh(artists, **kwargs):
        refresh_calls.append(list(artists))
        return downsample.downsample_state.RefreshResult(
            [candidate],
            ["Artist", "Denied"],
            {"Denied": "no access"},
            False,
        )

    monkeypatch.setattr(downsample, "HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(downsample, "list_library_artists",
                        lambda: [artist_dir, denied_dir])
    monkeypatch.setattr(downsample.downsample_state, "refresh_for_artists",
                        fake_refresh)
    monkeypatch.setattr(downsample.downsample_state, "update_artist",
                        lambda ad, **kwargs: update_calls.append(ad.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(downsample.downsample_state, "load",
                        lambda: {"candidates": []})
    monkeypatch.setattr(downsample.review_badges, "set_ready",
                        lambda surface, ready: badge_calls.append((surface, ready)))
    monkeypatch.setattr(downsample, "downsample_dir",
                        lambda *a, **k: {"resampled": 1, "saved_bytes": 100, "errors": 0})
    monkeypatch.setattr(downsample, "confirm", lambda *a, **k: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    result = downsample.run_downsample_walk_mode(args)

    assert result != 0
    assert refresh_calls == [[artist_dir, denied_dir]]
    assert update_calls == ["Artist"]
    assert badge_calls == [("downsample", False)]
    assert "1 artist couldn't be checked" in caplog.text
    assert "Downsample walk complete" not in caplog.text


def test_downsample_walk_marks_success_and_reports_file_failure(
        tmp_path, monkeypatch, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library.downsample import DownsampleCandidate
    from qobuz_librarian.modes import downsample
    from qobuz_librarian.quality import decision

    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    args = SimpleNamespace(dry_run=False, yes=False)
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    candidate = DownsampleCandidate(
        album_dir=album_dir,
        artist="Artist",
        title="Album",
        n_hires=1,
        n_flac=1,
        source_rates=[96000],
        target_rates=[48000],
        est_saving=123,
    )

    monkeypatch.setattr(downsample, "HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(downsample, "list_library_artists",
                        lambda: [artist_dir])
    monkeypatch.setattr(
        downsample.downsample_state,
        "refresh_for_artists",
        lambda artists, **kwargs:
        downsample.downsample_state.RefreshResult(
            [candidate], ["Artist"], {}, True),
    )
    monkeypatch.setattr(downsample.downsample_state, "update_artist",
                        lambda *a, **k: SimpleNamespace(
                            complete=True, candidates=[]))
    monkeypatch.setattr(downsample.downsample_state, "load",
                        lambda: {"candidates": []})
    monkeypatch.setattr(downsample.review_badges, "set_ready",
                        lambda *a, **k: None)
    monkeypatch.setattr(downsample, "downsample_dir",
                        lambda *a, **k: {"resampled": 1, "saved_bytes": 100, "errors": 0})
    monkeypatch.setattr(downsample, "confirm", lambda *a, **k: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    result = downsample.run_downsample_walk_mode(args)

    assert result == 0
    assert decision.is_local_album_capped(album_dir, decision.load_capped())

    caplog.clear()
    monkeypatch.setattr(
        downsample,
        "downsample_dir",
        lambda *a, **k: {
            "resampled": 0,
            "saved_bytes": 0,
            "errors": 1,
            "flush_warnings": 0,
        },
    )

    result = downsample.run_downsample_walk_mode(args)

    assert result != 0
    assert "Downsample walk finished with errors" in caplog.text
    assert "Downsample walk complete" not in caplog.text


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
        new_releases.run_check_new_releases_mode(SimpleNamespace(dry_run=dry_run))

    assert expected in caplog.text
    assert bool(saved) is not dry_run
