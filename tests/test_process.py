from pathlib import Path
from types import SimpleNamespace

import pytest


def _patch_download_receipts(monkeypatch, download, added):
    from qobuz_librarian.integrations.staging import StagedFile

    class _Run:
        path = Path("/")

        @staticmethod
        def to_record():
            return {"path": "/", "root_identity": [0, 0, 0, 0, 0, 0]}

    def sealed_added(_snapshot):
        receipts = []
        for item in added:
            value = item.stat()
            receipts.append(StagedFile(item, (
                value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns,
            )))
        return receipts

    monkeypatch.setattr(download, "files_added_since", sealed_added)
    monkeypatch.setattr(download, "create_staging_run", lambda: _Run())
    monkeypatch.setattr(
        download,
        "capture_staging_run",
        lambda _run: SimpleNamespace(files=(object(),)),
    )


def _args(**over):
    base = dict(force=False, yes=True, no_import=False, dry_run=False,
                verbose=False, consolidate=False, no_upgrade=False,
                no_downsample=True,
                auto_upgrade=False, prefer_hires=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_bound_download_rechecks_access_before_staging(monkeypatch):
    from qobuz_librarian.api import client
    from qobuz_librarian.api.auth import DownloaderNotReady, credentials_from_values
    from qobuz_librarian.modes import process as proc

    credentials = credentials_from_values("", "token", source="streamrip")
    album = {
        "id": "album",
        "title": "Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": [{"id": "track", "title": "Track"}]},
    }
    staging_calls = []

    monkeypatch.setattr(proc, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _album: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda tracks, existing: (tracks, []))
    monkeypatch.setattr(proc, "print_album_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        proc,
        "staging_preflight",
        lambda _args: staging_calls.append(True),
    )
    monkeypatch.setattr(
        client,
        "authorize_qobuz_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(DownloaderNotReady()),
    )

    with pytest.raises(DownloaderNotReady):
        proc.process_album(
            album,
            _args(),
            already_confirmed=True,
            token=credentials.token,
        )

    assert staging_calls == []


def test_direct_download_refuses_files_changed_after_confirmation(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    source = album_dir / "01.flac"
    source.write_bytes(b"reviewed bytes")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    album = {
        "id": "album",
        "title": "Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": [
            {"id": "owned", "title": "Owned"},
            {"id": "missing", "title": "Missing"},
        ]},
    }
    existing = [{"title": "Owned", "path": str(source)}]

    monkeypatch.setattr(proc, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(
        proc,
        "find_existing_tracks",
        lambda _album: (existing, album_dir),
    )
    monkeypatch.setattr(
        proc,
        "compute_missing",
        lambda tracks, _existing: ([tracks[1]], [tracks[0]]),
    )
    monkeypatch.setattr(proc, "print_album_summary", lambda *_a, **_kw: None)

    def confirm(*_args, **_kwargs):
        source.write_bytes(b"replacement bytes")
        return True

    monkeypatch.setattr(proc, "confirm", confirm)
    monkeypatch.setattr(proc, "staging_preflight", lambda _args: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "log_fetch", lambda _row: None)
    monkeypatch.setattr(
        proc,
        "run_album_download",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("download started against changed files")
        ),
    )

    result = proc.process_album(album, _args(yes=False), token="tok")

    assert result["result"] == "stale_candidate"
    assert source.read_bytes() == b"replacement bytes"


def test_direct_missing_album_refuses_album_added_after_confirmation(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    music = tmp_path / "music"
    music.mkdir()
    appeared = music / "Artist" / "Album"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    album = {
        "id": "album",
        "title": "Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": [{"id": "track", "title": "Track"}]},
    }

    monkeypatch.setattr(proc, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _album: ([], None))
    monkeypatch.setattr(
        proc,
        "compute_missing",
        lambda tracks, _existing: (tracks, []),
    )
    monkeypatch.setattr(
        proc,
        "find_album_dir_filesystem",
        lambda _album: appeared if appeared.exists() else None,
    )
    monkeypatch.setattr(proc, "print_album_summary", lambda *_a, **_kw: None)

    def confirm(*_args, **_kwargs):
        appeared.mkdir(parents=True)
        (appeared / "01.flac").write_bytes(b"added elsewhere")
        return True

    monkeypatch.setattr(proc, "confirm", confirm)
    monkeypatch.setattr(proc, "staging_preflight", lambda _args: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "log_fetch", lambda _row: None)
    monkeypatch.setattr(
        proc,
        "run_album_download",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("download started after album appeared")
        ),
    )

    result = proc.process_album(album, _args(yes=False), token="tok")

    assert result["result"] == "stale_candidate"
    assert (appeared / "01.flac").read_bytes() == b"added elsewhere"


def test_force_stops_after_an_interrupted_backup(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"original")
    retained = SimpleNamespace(
        complete=False,
        path=tmp_path / "backups" / "Album",
    )
    recovered = []

    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _album: album_dir)
    monkeypatch.setattr(proc, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(proc, "backup_album_dir", lambda _path: retained)
    monkeypatch.setattr(
        proc,
        "_recover_incomplete_upgrade_backup",
        lambda backup, original, **kwargs: recovered.append(
            (backup, original, kwargs["operation"])),
    )

    outcome = proc.force_cleanup_preflight({}, _args(force=True))

    assert outcome is None
    assert recovered == [
        (retained, album_dir, "forced re-download backup"),
    ]


def test_partial_retention_moves_only_the_recorded_download_run(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.download import retain_download_staging
    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    owned = staging_tx.create_staging_run()
    concurrent = staging_tx.create_staging_run()
    owned_file = owned.path / "Artist" / "Album" / "01.flac"
    concurrent_file = concurrent.path / "Other" / "Album" / "01.flac"
    owned_file.parent.mkdir(parents=True)
    concurrent_file.parent.mkdir(parents=True)
    owned_file.write_bytes(b"owned partial")
    concurrent_file.write_bytes(b"concurrent run")
    result = {"_staging_run": owned.to_record()}

    assert retain_download_staging(result, label="incomplete-replacement")
    assert not owned.path.exists()
    assert concurrent_file.read_bytes() == b"concurrent run"
    groups = staging_tx.list_groups(kind="interrupted")
    assert len(groups) == 1
    assert list(groups[0].trees[0].path.rglob("*.flac"))[0].read_bytes() == (
        b"owned partial"
    )


def test_completed_run_retirement_reclaims_nested_empty_directories(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.download import retire_empty_download_staging
    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    run = staging_tx.create_staging_run()
    (run.path / "Artist" / "Album" / "Disc 2").mkdir(parents=True)

    assert retire_empty_download_staging({"_staging_run": run.to_record()})
    assert not run.path.exists()
    assert staging_tx.list_groups(kind="interrupted") == []


def test_cancel_after_download_skips_beets_import(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "A", "track_number": 1},
              {"id": 2, "title": "B", "track_number": 2}]
    album = {"id": "X", "title": "Alb", "artist": {"name": "Ar"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: True)

    def fake_download(**kw):
        kw["result"].update(n_ok=2, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0,
                            gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)
    monkeypatch.setattr(proc, "validated_staged_album_dirs", lambda _result: [staging])

    beets_runs = []
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: beets_runs.append(1) or True)

    result = proc.process_album(album, _args(), token="tok")

    assert result["result"] == "cancelled"
    assert result["imported"] is False
    assert beets_runs == []


def test_partial_upgrade_reports_a_backup_that_could_not_be_restored(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"original")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    backup = SimpleNamespace(
        complete=True,
        path=tmp_path / "backups" / "Album",
    )
    tracks = [
        {"id": 1, "title": "A", "track_number": 1},
        {"id": 2, "title": "B", "track_number": 2},
    ]
    album = {
        "id": "upgrade-partial",
        "title": "Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": tracks},
    }
    existing = [{"title": "A", "path": str(album_dir / "01.flac")}]

    monkeypatch.setattr(proc, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(
        proc, "find_existing_tracks", lambda _album: (existing, album_dir)
    )
    monkeypatch.setattr(
        proc, "compute_missing", lambda _tracks, _existing: ([], list(tracks))
    )
    monkeypatch.setattr(
        proc,
        "compare_album_quality",
        lambda _existing, _album: {
            "classification": "all_lower",
            "n_unknown": 0,
            "qobuz_quality": (24, 96000),
        },
    )
    monkeypatch.setattr(proc, "find_extras_in_existing", lambda *_a: [])
    monkeypatch.setattr(proc, "existing_track_quality", lambda _track: (16, 44100))
    monkeypatch.setattr(proc, "capture_beets_album_entries", lambda _path: [])
    backup_receipts = []

    def seal_backup(_path, **kwargs):
        backup_receipts.append(kwargs.get("expected_receipt"))
        return backup

    monkeypatch.setattr(
        proc,
        "backup_album_dir",
        seal_backup,
    )
    monkeypatch.setattr(proc, "staging_preflight", lambda _args: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "print_album_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(proc, "retain_download_staging", lambda *_a, **_k: True)
    monkeypatch.setattr(proc, "restore_upgrade_backup", lambda *_a, **_k: False)
    monkeypatch.setattr(proc, "pin_unverified_upgrade_backup", lambda *_a, **_k: True)
    monkeypatch.setattr(proc, "log_fetch", lambda _row: None)

    def partial_download(**kwargs):
        kwargs["result"].update(
            n_ok=1,
            n_fail=1,
            n_lossy=0,
            failed_tracks=["B"],
            lossy_tracks=[],
            broken_tracks=[],
            elapsed=0.0,
            gap_fill_backup_path=None,
        )

    monkeypatch.setattr(proc, "run_album_download", partial_download)

    result = proc.process_album(
        album,
        _args(auto_upgrade=True),
        already_confirmed=True,
        upgrade_only=True,
        token="tok",
    )

    assert result["result"] == "partial"
    assert result["imported"] is False
    assert result["upgrade_unverified"] is True
    assert backup_receipts and backup_receipts[0] is not None


def test_treat_as_new_downloads_an_owned_album_as_a_separate_edition(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "A", "track_number": 1},
              {"id": 2, "title": "B", "track_number": 2}]
    album = {"id": "ED99", "title": "Abbey Road", "artist": {"name": "The Beatles"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}
    owned_dir = tmp_path / "The Beatles" / "Abbey Road (1969)"

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (list(tracks), owned_dir))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: owned_dir)
    monkeypatch.setattr(proc, "compute_missing",
                        lambda q, e: ([], list(q)) if e else (list(q), []))
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 0))
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)

    def fake_download(**kw):
        kw["result"].update(n_ok=2, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0, gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)

    consolidate_seen = []
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [staging])
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: consolidate_seen.append(k.get("consolidate")) or True)

    assert proc.process_album(album, _args(), token="tok")["result"] == "already_complete"
    assert consolidate_seen == []

    monkeypatch.setattr(
        proc,
        "consolidate_albums",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    interrupted = proc.process_album(
        album, _args(consolidate=True), token="tok"
    )
    assert interrupted["result"] == "already_complete"
    assert interrupted["consolidation_interrupted"] is True

    result = proc.process_album(album, _args(), token="tok", treat_as_new=True)
    assert result.get("imported") is True
    assert consolidate_seen == [False]


def test_auto_downsample_marks_imported_folder_when_album_resolver_misses(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staged_album = staging / "Bill Evans" / "Waltz For Debby"
    staged_album.mkdir(parents=True)
    final_dir = tmp_path / "music" / "Bill Evans" / "Waltz For Debby (2023)"
    final_dir.mkdir(parents=True)
    (final_dir / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "My Foolish Heart", "track_number": 1}]
    album = {"id": "q8m2", "title": "Waltz For Debby",
             "artist": {"name": "Bill Evans Trio"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 192.0,
             "tracks": {"items": tracks}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [staged_album])
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc,
        "_pre_import_staging_hooks",
        lambda _a, _dirs=None: executor.PreImportHooksResult(
            [],
            1,
            errors=1,
            saved_bytes=512,
            flush_warnings=1,
            cancelled=True,
        ),
    )
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    fetch_log = []
    monkeypatch.setattr(proc, "log_fetch", fetch_log.append)
    monkeypatch.setattr(proc, "warn_if_download_truncated", lambda *a, **k: None)
    monkeypatch.setattr(proc, "verify_and_recover",
                        lambda *a, **k: {"under": False, "recovered": False,
                                         "retried": False, "staged_dirs": [staged_album]})
    monkeypatch.setattr(proc, "track_signatures_for_album_dirs",
                        lambda _dirs: ["sig"], raising=False)
    monkeypatch.setattr(proc, "find_album_dir_by_track_signatures",
                        lambda _sigs: final_dir, raising=False)
    marked = []
    monkeypatch.setattr(proc, "mark_local_album_capped",
                        lambda path, qobuz_album=None: marked.append(path))

    def fake_download(**kw):
        kw["result"].update(n_ok=1, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0,
                            gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)

    result = proc.process_album(album, _args(), token="tok")

    assert result["imported"] is True
    assert result["result"] == "partial"
    assert result["downsample_errors"] == 1
    assert result["downsample_saved_bytes"] == 512
    assert result["downsample_flush_warnings"] == 1
    assert result["downsample_cancelled"] is True
    assert fetch_log[-1]["downsample_saved_bytes"] == 512
    assert marked == [final_dir]




def test_self_heal_retry_signatures_use_fresh_staged_dirs(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    first_staged = staging / "first" / "Album"
    retry_staged = staging / "retry" / "Album"
    first_staged.mkdir(parents=True)
    retry_staged.mkdir(parents=True)
    old_dir = tmp_path / "music" / "Artist" / "Album"
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old")
    final_dir = tmp_path / "music" / "Artist" / "Album (2026)"
    final_dir.mkdir(parents=True)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "A", "track_number": 1}]
    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 192.0,
             "tracks": {"items": tracks}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: old_dir)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [first_staged])
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 1))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "warn_if_download_truncated", lambda *a, **k: None)
    monkeypatch.setattr(proc, "verify_and_recover",
                        lambda *a, **k: {"under": False, "recovered": True,
                                         "retried": True,
                                         "staged_dirs": [retry_staged]})

    signature_dirs = []

    def fake_signatures(dirs):
        signature_dirs.append(list(dirs))
        return ["sig"] if list(dirs) == [retry_staged] else []

    monkeypatch.setattr(proc, "track_signatures_for_album_dirs", fake_signatures,
                        raising=False)
    monkeypatch.setattr(proc, "find_album_dir_by_track_signatures",
                        lambda _sigs: final_dir, raising=False)
    marked = []
    monkeypatch.setattr(proc, "mark_local_album_capped",
                        lambda path, qobuz_album=None: marked.append(path))

    def fake_download(**kw):
        kw["result"].update(n_ok=1, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0,
                            gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)

    result = proc.process_album(
        album,
        _args(),
        token="tok",
        treat_as_new=True,
    )

    assert result["imported"] is True
    assert signature_dirs == [[retry_staged]]
    assert marked == [final_dir]


def test_self_heal_retry_no_files_imports_first_staged_rip(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    first_staged = staging / "Artist" / "Album"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 192.0,
             "tracks": {"items": [{"id": 1, "title": "A", "track_number": 1}]}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 0))
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "warn_if_download_truncated", lambda *a, **k: None)
    monkeypatch.setattr(proc, "track_signatures_for_album_dirs", lambda _d: [])
    monkeypatch.setattr(
        proc, "validated_staged_album_dirs",
        lambda _result: [first_staged] if first_staged.exists() else [],
    )

    imported = []
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: imported.append(
                            first_staged / "01.flac") or True)

    def fake_download(**kw):
        result = kw["result"]
        if kw.get("quality") == 4:
            result.update(n_ok=0, n_fail=1, n_lossy=0, failed_tracks=["A"],
                          lossy_tracks=[], broken_tracks=[], elapsed=0.0,
                          gap_fill_backup_path=None)
        else:
            first_staged.mkdir(parents=True, exist_ok=True)
            (first_staged / "01.flac").write_bytes(b"first")
            result.update(n_ok=1, n_fail=0, n_lossy=0, failed_tracks=[],
                          lossy_tracks=[], broken_tracks=[], elapsed=0.0,
                          gap_fill_backup_path=None)
        return result

    def fake_verify(_album, _staged_dirs, *, redownload_at_max, **_kw):
        dirs = redownload_at_max()
        return {"under": True, "recovered": False, "retried": True,
                "n_below": 1, "served": (16, 44100),
                "target": (24, 96000), "staged_dirs": dirs}

    monkeypatch.setattr(proc, "run_album_download", fake_download)
    monkeypatch.setattr(proc, "verify_and_recover", fake_verify)

    result = proc.process_album(album, _args(), token="tok")

    assert result["imported"] is True
    assert result["result"] == "partial"
    assert imported == [first_staged / "01.flac"]
    assert (first_staged / "01.flac").read_bytes() == b"first"


def test_gap_fill_backup_restored_when_track_returns_lossy(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian import download as dl
    from qobuz_librarian.modes import process as proc

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01 - T1.flac"
    owned.write_bytes(b"the-owned-original")
    existing = [{"path": str(owned), "title": "T1", "tracknumber": 1, "discnumber": 1}]

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": i, "title": f"T{i}", "track_number": i} for i in range(1, 6)]
    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}
    missing = tracks[1:]
    new_flacs = [staging / f"0{i} - T{i}.flac" for i in range(2, 6)]
    for f in new_flacs:
        f.write_bytes(b"\x00" * 1000)
    lossy_rip = staging / "01 - T1.mp3"
    lossy_rip.write_bytes(b"lossy")

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (existing, album_dir))
    monkeypatch.setattr(proc, "compute_missing", lambda _q, _e: (missing, [tracks[0]]))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 0))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [staging])

    monkeypatch.setattr(dl, "rip_url", lambda *a, **k: (0, ""))
    _patch_download_receipts(monkeypatch, dl, [*new_flacs, lossy_rip])
    monkeypatch.setattr(
        dl, "cleanup_lossy",
        lambda files: (
            [file for file in files if file.suffix == ".flac"],
            [file for file in files if file.suffix == ".mp3"],
            [],
        ),
    )
    monkeypatch.setattr(dl, "snapshot_staging", lambda: set())
    monkeypatch.setattr(dl, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(dl, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(dl, "detect_rate_limited", lambda _o: False)
    monkeypatch.setattr(dl, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(dl, "find_extras_in_existing", lambda *a, **k: [])

    proc.process_album(album, _args(), token="tok")

    assert owned.exists()
    assert owned.read_bytes() == b"the-owned-original"


def test_staging_overflow_under_yes_exits_general_not_auth(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets as beets_mod
    from qobuz_librarian.integrations import rip as rip_mod
    from qobuz_librarian.ui_cli.errors import EXIT_GENERAL

    staging = tmp_path / "staging"
    staging.mkdir()
    for i in range(3):
        (staging / f"leftover{i}.flac").write_bytes(b"x")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "LEFTOVER_WARN_LIMIT", 1)
    monkeypatch.setattr(rip_mod, "cleanup_staging_residue", lambda: 0)

    with pytest.raises(SystemExit) as exc:
        beets_mod.staging_preflight(SimpleNamespace(yes=True))
    assert exc.value.code == EXIT_GENERAL


def test_upgrade_verification_keeps_backup_when_replacement_is_short(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [
        {"title": "T1", "length": 200.0, "bits": 16,
         "sample_rate": 44100, "channels": 2,
         "discnumber": 1, "tracknumber": 1},
        {"title": "T2", "length": 180.0, "bits": 16,
         "sample_rate": 44100, "channels": 2,
         "discnumber": 1, "tracknumber": 2},
        {"title": "T3", "length": 240.0, "bits": 16,
         "sample_rate": 44100, "channels": 2,
         "discnumber": 1, "tracknumber": 3},
    ]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)

    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else original[:2])
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False
    truncated = [
        dict(original[0]),
        {**original[1], "length": 20.0},
        dict(original[2]),
    ]
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else truncated)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    complete = tmp_path / "Complete"
    complete.mkdir()
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: complete)
    monkeypatch.setattr(proc, "read_album_dir", lambda _f, walk_errors=None: list(original))
    assert proc._upgrade_replacement_verified({"id": "x"}, complete, backup) is True


def test_replacement_catalogue_retires_only_captured_rows(monkeypatch, tmp_path):
    import os
    import sqlite3

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    old_files = [album / "01.flac", album / "02.flac"]
    for path in old_files:
        path.write_bytes(b"old")

    database = tmp_path / "beets.db"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    monkeypatch.setattr(cfg, "BEETS_TIMEOUT", 5)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute(
            "CREATE TABLE items ("
            "id INTEGER PRIMARY KEY, path BLOB NOT NULL, album_id INTEGER, title TEXT)"
        )
        connection.execute(
            "CREATE TABLE album_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, key TEXT, value TEXT)"
        )
        connection.execute(
            "CREATE TABLE item_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, key TEXT, value TEXT)"
        )
        connection.execute("INSERT INTO albums VALUES (10, 'Original')")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, 10, ?)",
            [
                (1, os.fsencode(old_files[0]), "Old one"),
                (2, os.fsencode(old_files[1]), "Old two"),
            ],
        )
        connection.execute(
            "INSERT INTO album_attributes VALUES (1, 10, 'source', 'old')"
        )
        connection.execute(
            "INSERT INTO item_attributes VALUES (1, 1, 'source', 'old')"
        )
    connection.close()

    snapshot = beets.capture_beets_album_entries(album)
    assert snapshot is not None

    backup = tmp_path / "backup"
    album.rename(backup)
    album.mkdir()
    replacement_files = [album / f"0{number}.flac" for number in range(1, 4)]
    for path in replacement_files:
        path.write_bytes(b"new")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO albums VALUES (20, 'Replacement')")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, 20, ?)",
            [
                (11, os.fsencode(replacement_files[0]), "New one"),
                (12, os.fsencode(replacement_files[1]), "New two"),
                (13, os.fsencode(replacement_files[2]), "New three"),
            ],
        )
        connection.execute(
            "INSERT INTO album_attributes VALUES (2, 20, 'source', 'new')"
        )
        connection.execute(
            "INSERT INTO item_attributes VALUES (2, 11, 'source', 'new')"
        )
    connection.close()

    assert beets.retire_replaced_beets_entries(
        snapshot, album, replacement_files
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM items ORDER BY id").fetchall() == [
            (11,), (12,), (13,)
        ]
        assert connection.execute("SELECT id FROM albums ORDER BY id").fetchall() == [
            (20,)
        ]
        assert connection.execute(
            "SELECT entity_id FROM item_attributes ORDER BY id"
        ).fetchall() == [(11,)]
        assert connection.execute(
            "SELECT entity_id FROM album_attributes ORDER BY id"
        ).fetchall() == [(20,)]
    connection.close()


def test_upgrade_verification_rejects_a_masked_per_track_downgrade(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [{"title": "T1", "length": 200.0,
                 "bits": 24, "sample_rate": 48000, "channels": 2,
                 "discnumber": 1, "tracknumber": 1},
                {"title": "T2", "length": 180.0,
                 "bits": 24, "sample_rate": 48000, "channels": 2,
                 "discnumber": 1, "tracknumber": 2}]
    replacement = [{"title": "T1", "length": 200.0,
                    "bits": 24, "sample_rate": 96000, "channels": 2,
                    "discnumber": 1, "tracknumber": 1},
                   {"title": "T2", "length": 180.0,
                    "bits": 16, "sample_rate": 44100, "channels": 2,
                    "discnumber": 1, "tracknumber": 2}]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else replacement)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    fixed = [{"title": "T1", "length": 200.0,
              "bits": 24, "sample_rate": 96000, "channels": 2,
              "discnumber": 1, "tracknumber": 1},
             {"title": "T2", "length": 180.0,
              "bits": 24, "sample_rate": 48000, "channels": 2,
              "discnumber": 1, "tracknumber": 2}]
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else fixed)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is True

    def verifies(old_tracks, new_tracks):
        old_tracks = [
            {"discnumber": 1, "tracknumber": index, **track}
            for index, track in enumerate(old_tracks, 1)
        ]
        new_tracks = [
            {"discnumber": 1, "tracknumber": index, **track}
            for index, track in enumerate(new_tracks, 1)
        ]
        monkeypatch.setattr(
            proc,
            "read_album_dir",
            lambda f, walk_errors=None: (
                old_tracks if f == backup else new_tracks
            ),
        )
        return proc._upgrade_replacement_verified(
            {"id": "x"}, post, backup)

    stereo = {"channels": 2, "length": 100.0}
    assert verifies(
        [{"title": "T1", "bits": 16, "sample_rate": 192000, **stereo}],
        [{"title": "T1", "bits": 24, "sample_rate": 96000, **stereo}],
    ) is False
    assert verifies(
        [{"title": "T1", "bits": 24, "sample_rate": 96000,
          "channels": 6, "length": 100.0}],
        [{"title": "T1", "bits": 24, "sample_rate": 96000, **stereo}],
    ) is False
    assert verifies(
        [
            {"title": "T1", "bits": 16, "sample_rate": 44100, **stereo},
            {"title": "T2", "bits": 16, "sample_rate": 44100, **stereo},
        ],
        [
            {"title": "T1", "bits": 24, "sample_rate": 96000,
             "channels": 2, "length": 50.0},
            {"title": "T2", "bits": 24, "sample_rate": 96000,
             "channels": 2, "length": 150.0},
        ],
    ) is False
    assert verifies(
        [{"title": "T1", "bits": 16, "sample_rate": 44100, **stereo}],
        [{"title": "T1", "bits": 0, "sample_rate": 96000, **stereo}],
    ) is False
    assert verifies(
        [{"title": "T1", "isrc": "USAAA1234567", "bits": 16,
          "sample_rate": 44100, **stereo}],
        [{"title": "T1", "isrc": "USBBB1234568", "bits": 24,
          "sample_rate": 96000, **stereo}],
    ) is False

    identities = {
        "isrc": "USAAA1234567",
        "mb_trackid": "123456781234123412341234567890ab",
    }
    for identity_field, identity in identities.items():
        assert verifies(
            [{"title": "T1", identity_field: identity,
              "bits": 16, "sample_rate": 44100, **stereo}],
            [{"title": "T1", identity_field: "",
              "bits": 24, "sample_rate": 96000, **stereo}],
        ) is False
        assert verifies(
            [{"title": "T1", identity_field: "N/A",
              "bits": 16, "sample_rate": 44100, **stereo}],
            [{"title": "T1", identity_field: "N/A",
              "bits": 24, "sample_rate": 96000, **stereo}],
        ) is False

    for identity_field, placeholder in (
        ("isrc", "000000000000"),
        ("mb_trackid", "00000000-0000-0000-0000-000000000000"),
    ):
        assert verifies(
            [{"title": "Original", identity_field: placeholder,
              "bits": 16, "sample_rate": 44100, **stereo}],
            [{"title": "Different", identity_field: placeholder,
              "bits": 24, "sample_rate": 96000, **stereo}],
        ) is False

    for old_title, new_title in (
        ("東京 (Remaster)", "大阪 (Remaster)"),
        ("東京 [Remaster]", "大阪 [Remaster]"),
    ):
        assert verifies(
            [{"title": old_title, "bits": 16,
              "sample_rate": 44100, **stereo}],
            [{"title": new_title, "bits": 24,
              "sample_rate": 96000, **stereo}],
        ) is False

    assert verifies(
        [{"title": "T1", "bits": 16, "sample_rate": 44100,
          "channels": 2, "length": True}],
        [{"title": "T1", "bits": 24, "sample_rate": 96000,
          "channels": 2, "length": 1.0}],
    ) is False

    assert verifies(
        [{"title": "T1", "isrc": "US-AAA-12-34567",
          "bits": 16, "sample_rate": 44100, **stereo}],
        [{"title": "T1", "isrc": "USAAA1234567",
          "bits": 24, "sample_rate": 96000, **stereo}],
    ) is True
    assert verifies(
        [{"title": "T1",
          "mb_trackid": "12345678-1234-1234-1234-1234567890ab",
          "bits": 16, "sample_rate": 44100, **stereo}],
        [{"title": "T1",
          "mb_trackid": "123456781234123412341234567890ab",
          "bits": 24, "sample_rate": 96000, **stereo}],
    ) is True

    assert verifies(
        [{"title": "T1", "bits": 16, "sample_rate": 44100, **stereo}],
        [{"title": "T1 (Remaster)", "bits": 24,
          "sample_rate": 96000, **stereo}],
    ) is False

    assert verifies(
        [
            {"title": "Same", "bits": 16, "sample_rate": 44100, **stereo},
            {"title": "Same", "bits": 16, "sample_rate": 44100, **stereo},
        ],
        [
            {"title": "Same", "tracknumber": 1,
             "bits": 24, "sample_rate": 96000, **stereo},
            {"title": "Same", "tracknumber": 1,
             "bits": 24, "sample_rate": 96000, **stereo},
        ],
    ) is False


def test_upgrade_verification_keeps_backup_on_a_degraded_walk(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [{"length": 200.0}, {"length": 180.0}, {"length": 240.0}]

    def degraded_read(f, walk_errors=None):
        if f == backup:
            if walk_errors is not None:
                walk_errors.append("disc 2: EIO")
            return original[:1]          # two tracks unreadable this run
        return original[:2]              # the replacement landed short

    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir", degraded_read)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False


def test_gap_fill_partial_import_restores_backup_despite_extra_track(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian import download as dl
    from qobuz_librarian.modes import process as proc

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01 - T1.flac"
    owned.write_bytes(b"the-owned-original")
    (album_dir / "09 - Bonus.flac").write_bytes(b"\x00" * 500)
    existing = [{"path": str(owned), "title": "T1", "tracknumber": 1, "discnumber": 1}]

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": i, "title": f"T{i}", "track_number": i} for i in range(1, 6)]
    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}
    missing = tracks[1:]
    new_flacs = [staging / f"0{i} - T{i}.flac" for i in range(2, 6)]
    for f in new_flacs:
        f.write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (existing, album_dir))
    monkeypatch.setattr(proc, "compute_missing", lambda _q, _e: (missing, [tracks[0]]))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        proc, "_pre_import_staging_hooks", lambda _a, _dirs=None: ([], 0))
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)

    t1_rip = staging / "01 - T1.flac"
    t1_rip.write_bytes(b"\x00" * 1000)

    def fake_import(*_a, **_k):
        for f in new_flacs:
            (album_dir / f.name).write_bytes(f.read_bytes())
            f.unlink()
        return True
    monkeypatch.setattr(proc, "beets_import_paths", fake_import)
    monkeypatch.setattr(proc, "validated_staged_album_dirs",
                        lambda _result: [staging])

    monkeypatch.setattr(dl, "rip_url", lambda *a, **k: (0, ""))
    _patch_download_receipts(monkeypatch, dl, [*new_flacs, t1_rip])
    monkeypatch.setattr(dl, "cleanup_lossy", lambda f: (list(f), [], []))
    monkeypatch.setattr(dl, "snapshot_staging", lambda: set())
    monkeypatch.setattr(dl, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(dl, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(dl, "detect_rate_limited", lambda _o: False)
    monkeypatch.setattr(dl, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(dl, "find_extras_in_existing", lambda *a, **k: [])

    proc.process_album(album, _args(), token="tok")

    assert owned.read_bytes() == b"the-owned-original"


def test_upgrade_verification_rejects_a_swap_hidden_by_ranking(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [
        {"title": "Alpha", "length": 100.0, "bits": 16, "sample_rate": 44100},
        {"title": "Beta", "length": 100.0, "bits": 24, "sample_rate": 96000},
    ]
    replacement = [
        {"title": "Alpha", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "Beta", "length": 100.0, "bits": 16, "sample_rate": 44100},
    ]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else replacement)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False


def test_upgrade_verification_requires_every_original_identity(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [
        {"title": "Alpha", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "Beta", "length": 100.0, "bits": 24, "sample_rate": 96000},
    ]
    replacement = [
        {"title": "Alpha", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "Alpha", "length": 100.0, "bits": 24, "sample_rate": 96000},
    ]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(
        proc, "read_album_dir",
        lambda f, walk_errors=None: original if f == backup else replacement,
    )

    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False


def test_upgrade_verification_counts_an_unreadable_track_as_a_downgrade(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [
        {"title": "T1", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "T2", "length": 100.0, "bits": 24, "sample_rate": 96000},
    ]
    replacement = [
        {"title": "T1", "length": 100.0, "bits": 24, "sample_rate": 96000},
        {"title": "T2", "length": 100.0, "bits": 0, "sample_rate": 0},
    ]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else replacement)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False
