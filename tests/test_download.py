import os
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import download as dl


def _album(tracks):
    return {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
            "tracks": {"items": tracks}}


def _patch(monkeypatch, *, rip, added, cleanup, cancel=False):
    from qobuz_librarian.integrations.staging import StagedFile

    class _Run:
        path = Path("/")

        @staticmethod
        def to_record():
            return {"path": "/", "root_identity": [0, 0, 0, 0, 0, 0]}

    def sealed_added(snapshot):
        receipts = []
        for item in added(snapshot):
            if isinstance(item, StagedFile):
                receipts.append(item)
                continue
            path = Path(item)
            value = os.stat(path, follow_symlinks=False)
            receipts.append(StagedFile(path, (
                value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns,
            )))
        return receipts

    monkeypatch.setattr(dl, "rip_url", rip)
    monkeypatch.setattr(dl, "files_added_since", sealed_added)
    monkeypatch.setattr(dl, "cleanup_lossy", cleanup)
    monkeypatch.setattr(dl, "create_staging_run", lambda: _Run())
    monkeypatch.setattr(dl, "capture_staging_run", lambda _run: object())
    monkeypatch.setattr(dl, "snapshot_staging", lambda: set())
    monkeypatch.setattr(dl, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(dl, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(dl, "detect_rate_limited", lambda _o: False)
    monkeypatch.setattr(dl, "is_cancel_requested", lambda: cancel)
    monkeypatch.setattr(dl.time, "sleep", lambda _s: None)


def test_full_album_with_present_tracks_counts_fail_against_total(monkeypatch, tmp_path):
    tracks = [{"id": i, "title": f"T{i}", "track_number": i} for i in range(1, 11)]
    tracks[8]["title"] = "T1"
    missing = tracks[3:]
    present = tracks[:4]
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    landed = [tmp_path / f"{i:02d} - T{i}.flac" for i in range(1, 9)]
    for p in landed:
        p.write_bytes(b"x")
    _patch(monkeypatch,
           rip=lambda *a, **k: (0, ""),
           added=lambda _s: landed,
           cleanup=lambda f: (list(f), [], []))
    monkeypatch.setattr(dl, "backup_gap_fill_files", lambda paths, d: None)
    monkeypatch.setattr(dl, "read_album_dir", lambda d: [])

    r = dl.run_album_download(album=_album(tracks), missing=missing,
                              present=present, album_dir=album, snapshot=set())
    assert r["download_full_album"] is True
    assert r["n_ok"] == 8
    assert r["n_fail"] == 2
    assert r["failed_tracks"] == ["T1", "T10"]
    assert r["n_ok"] + r["n_lossy"] + r["n_fail"] == 10


def test_lossy_track_retried_once_and_recovers(monkeypatch, tmp_path):
    tracks = [
        {"id": 101, "title": "Song", "media_number": 1, "track_number": 1},
        {"id": 202, "title": "Song", "media_number": 2, "track_number": 1},
    ]
    disc_1 = tmp_path / "Disc 1"
    disc_2 = tmp_path / "Disc 2"
    disc_1.mkdir()
    disc_2.mkdir()
    rejected = disc_1 / "01 - Song.mp3"
    recovered = disc_1 / "01 - Song.flac"
    track_b = disc_2 / "01 - Song.flac"
    track_b.write_bytes(b"x")
    rejected.write_bytes(b"lossy")
    rips = []

    def rip(url, **_k):
        rips.append(url)
        if "track/101" in url:
            recovered.write_bytes(b"x")
        return (0, "")

    deltas = iter([[rejected, track_b], [recovered]])
    cleans = iter([([track_b], [rejected], []), ([recovered], [], [])])
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path)
    _patch(monkeypatch, rip=rip,
           added=lambda _s: next(deltas, []),
           cleanup=lambda _f: next(cleans, ([], [], [])))
    monkeypatch.setattr(dl, "snapshot_staging", lambda: {track_b})

    album = _album(tracks)
    r = dl.run_album_download(album=album, missing=tracks, present=[],
                              album_dir=None, snapshot=set())

    assert rips == ["https://play.qobuz.com/album/ALB",
                    "https://play.qobuz.com/track/101"]
    assert (r["n_ok"], r["n_lossy"], r["n_fail"]) == (2, 0, 0)


def test_full_album_backs_up_present_tracks_before_rip(monkeypatch, tmp_path):
    from qobuz_librarian.library.backup import library_backup_matches_intent

    create_owned_staging_run = dl.create_staging_run
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01 - owned.flac"
    owned.write_bytes(b"the-owned-original")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "staging")
    tracks = [{"id": i, "title": f"T{i}"} for i in range(1, 6)]
    owner = {"operation_id": "a" * 64, "item_id": "b" * 64}
    events = []
    records = {}

    def checkpoint(payload):
        events.append(payload["kind"])
        if payload["kind"] == "gap-fill":
            records["intent"] = payload
            assert owned.exists()
            assert not Path(payload["path"]).exists()
        elif payload["kind"] == "library-backup-carrier":
            records["carrier"] = payload["carrier"]
            assert not owned.exists()

    def rip(*_args, **_kwargs):
        events.append("rip")
        assert not owned.exists()
        return (0, "")

    _patch(monkeypatch,
           rip=rip,
           added=lambda _s: [],
           cleanup=lambda f, **_kw: (list(f), [], []))
    monkeypatch.setattr(dl, "create_staging_run", create_owned_staging_run)
    monkeypatch.setattr(dl, "read_album_dir", lambda _d: [{"path": str(owned)}])
    monkeypatch.setattr(dl, "find_extras_in_existing", lambda *a, **k: [])

    result = {}
    dl.run_album_download(album=_album(tracks), missing=tracks[1:],
                          present=[tracks[0]], album_dir=album_dir, snapshot=set(),
                          result=result, recovery_owner=owner,
                          recovery_checkpoint=checkpoint,
                          required_backup_kind="gap-fill")

    bp = result["gap_fill_backup_path"]
    assert bp is not None and bp.complete and not owned.exists()
    assert any(f.read_bytes() == b"the-owned-original" for f in bp.rglob("*"))
    # Nothing lands from the fake album rip, so per-track retries follow it.
    assert events[:4] == [
        "gap-fill",
        "library-backup-carrier",
        "staging-run",
        "rip",
    ]
    assert library_backup_matches_intent(
        records["carrier"],
        records["intent"],
        expected_owner=owner,
    )


def test_full_album_does_not_rip_after_a_partial_present_track_backup(
        monkeypatch, tmp_path):
    from qobuz_librarian.library.backup import BackupResult

    tracks = [{"id": i, "title": f"T{i}"} for i in range(1, 6)]
    owned = tmp_path / "01.flac"
    owned.write_bytes(b"owned")
    retained = tmp_path / "partial-backup"
    retained.mkdir()
    partial = BackupResult(retained, False, None, 2, 1)
    rip_calls = []
    monkeypatch.setattr(dl, "backup_gap_fill_files", lambda *_a: partial)
    monkeypatch.setattr(dl, "find_extras_in_existing", lambda *_a: [])
    monkeypatch.setattr(dl, "rip_url", lambda *_a, **_k: rip_calls.append(1))
    result = {}

    with pytest.raises(OSError):
        dl.run_album_download(
            album=_album(tracks), missing=tracks[1:], present=[tracks[0]],
            album_dir=tmp_path, snapshot=set(),
            existing=[{"path": str(owned)}], result=result,
        )

    assert not rip_calls
    assert result["gap_fill_backup_path"] is partial


def test_same_title_twin_failure_stays_failed(monkeypatch, tmp_path):
    tracks = [{"id": 1, "title": "Song", "track_number": 1},
              {"id": 2, "title": "Song", "track_number": 2}]
    landed = tmp_path / "01 - Song.flac"
    retried = []

    def rip(url, **_k):
        if "track/1" in url:
            landed.write_bytes(b"x")
            return (0, "")
        retried.append(url)
        return (1, "boom")

    _patch(monkeypatch, rip=rip,
           added=lambda s: [p for p in (landed,) if p.exists() and p not in s],
           cleanup=lambda f: (list(f), [], []))
    monkeypatch.setattr(dl, "snapshot_staging",
                        lambda: {p for p in (landed,) if p.exists()})

    r = dl.run_album_download(album=_album(tracks), missing=tracks, present=[],
                              album_dir=None, snapshot=set(),
                              force_track_by_track=True)

    assert len(retried) == 2
    assert r["n_ok"] == 1
    assert r["n_fail"] == 1
    assert r["failed_tracks"] == ["Song"]


def test_retire_download_staging_after_import_sweeps_leftover_art(monkeypatch, tmp_path):
    # A gap-fill stages the album cover next to the track; merging into an
    # album that already has art leaves the cover in the run. That is not an
    # incomplete import: accept it and dispose the run. Leftover audio is.
    from qobuz_librarian.integrations.staging import create_staging_run

    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path)

    run = create_staging_run()
    album = run.path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "cover.jpg").write_bytes(b"art")
    assert dl.retire_download_staging_after_import({"_staging_run": run.to_record()}) is True
    assert not run.path.exists()

    run2 = create_staging_run()
    album2 = run2.path / "Artist" / "Album"
    album2.mkdir(parents=True)
    (album2 / "01 - Song.flac").write_bytes(b"audio")
    assert dl.retire_download_staging_after_import({"_staging_run": run2.to_record()}) is False
    assert (album2 / "01 - Song.flac").exists()
    # A user cancel discards the partial rip instead of holding it for recovery.
    assert dl.discard_download_staging({"_staging_run": run2.to_record()}) is True
    assert not run2.path.exists()
