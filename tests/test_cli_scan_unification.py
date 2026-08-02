from pathlib import Path
from types import SimpleNamespace

import pytest


def test_upgrade_walk_uses_saved_upgrade_state(monkeypatch):
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
            "title": "Album",
            "detail": "CD -> 24-bit / 96 kHz",
            "payload": {
                "album_id": "alb-1",
                "title_similarity": 1.0,
                "needed_edition_swap": False,
            },
        }],
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
                        or {"imported": True})
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_: None)

    upgrade.run_upgrade_walk_mode(args, "tok")

    assert processed == ["alb-1"]
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


def test_downsample_walk_updates_only_affected_artist_after_success(monkeypatch):
    from qobuz_librarian.library.downsample import DownsampleCandidate
    from qobuz_librarian.modes import downsample

    args = SimpleNamespace(dry_run=False, yes=False)
    artist_dir = Path("/music/Artist")
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
            ["Artist"],
            {},
            True,
        )

    monkeypatch.setattr(downsample, "HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(downsample, "list_library_artists",
                        lambda: [artist_dir])
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

    downsample.run_downsample_walk_mode(args)

    assert refresh_calls == [[artist_dir]]
    assert update_calls == ["Artist"]
    assert badge_calls == [("downsample", False)]


def test_downsample_walk_marks_successful_album_locally_capped(
        tmp_path, monkeypatch):
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

    downsample.run_downsample_walk_mode(args)

    assert decision.is_local_album_capped(album_dir, decision.load_capped())


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
