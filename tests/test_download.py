import os
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import download as dl


def _album(tracks):
    return {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
            "tracks": {"items": tracks}}


def test_match_key_from_stem_keys_a_star_track_to_its_title():
    from qobuz_librarian.library.tags import normalize, strip_edition_suffix

    assert dl.match_key_from_stem("01. ★") == normalize(strip_edition_suffix("★"))
    assert dl.match_key_from_stem("03 - Changes") == normalize(
        strip_edition_suffix("Changes"))


def test_conflicting_file_identity_does_not_fall_back_to_title(monkeypatch, tmp_path):
    track = {"id": 1, "title": "Song", "media_number": 1, "track_number": 1}
    path = tmp_path / "01 - Song.flac"
    path.write_bytes(b"audio")
    monkeypatch.setattr(dl, "read_audio_meta", lambda _path: {
        "title": "Song", "discnumber": 1, "tracknumber": 2,
    })

    identities = dl._capture_file_identities([path], [track])
    assert dl._pair_files_to_tracks([path], [track], identities, [track]) == []


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

    records = r["_staged_track_bindings"]
    # track_b landed first, but bindings must come out in catalogue order -
    # the durable runner compares them against the journal's lineages, which
    # are always catalogue-ordered.
    assert [(record["slot"], record["path"]) for record in records] == [
        ("qobuz:101", str(recovered)),
        ("qobuz:202", str(track_b)),
    ]

    class _CapturedRun:
        path = tmp_path
        files = tuple(
            (str(Path(record["path"]).relative_to(tmp_path)),
             tuple(record["identity"]))
            for record in records
        )

    monkeypatch.setattr(dl, "staging_run_from_record", lambda _value: object())
    monkeypatch.setattr(dl, "capture_staging_run", lambda _run: _CapturedRun())
    assert dl.staged_track_bindings(r) == tuple(records)
    assert dl.exact_download_coverage(r, album).counts.broken == 0

    extra = tmp_path / "unbound.flac"
    extra.write_bytes(b"extra")
    value = os.stat(extra, follow_symlinks=False)
    _CapturedRun.files += ((
        extra.name,
        (value.st_dev, value.st_ino, value.st_mode, value.st_size,
         value.st_mtime_ns, value.st_ctime_ns),
    ),)
    with pytest.raises(OSError, match="does not match"):
        dl.staged_track_bindings(r)


@pytest.mark.parametrize("total,missing,expect_full", [
    (100, 69, False),   # 0.69 → per-track
    (100, 70, True),    # 0.70 → full-album
    (5, 3, False),      # below the max(4, …) floor → per-track
    (5, 4, True),       # hits the floor of 4 → full-album
])
def test_strategy_full_vs_per_track_boundary(monkeypatch, total, missing, expect_full):
    tracks = [{"id": i, "title": f"T{i}"} for i in range(total)]
    urls = []
    _patch(monkeypatch,
           rip=lambda url, **_k: (urls.append(url), (0, ""))[1],
           added=lambda _s: [],
           cleanup=lambda f: (list(f), [], []))

    dl.run_album_download(album=_album(tracks), missing=tracks[:missing],
                          present=[{}], album_dir=None, snapshot=set())

    assert any("/album/" in u for u in urls) is expect_full


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
    assert events == [
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

    with pytest.raises(OSError, match="Partial pre-download backup"):
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


def test_discard_download_staging_removes_partial_run(monkeypatch, tmp_path):
    # A user cancel throws the partial rip away so the queue can move on -
    # leftover audio must not hold the run for recovery like a crash does.
    from qobuz_librarian.integrations.staging import create_staging_run

    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path)

    run = create_staging_run()
    album = run.path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Song.flac").write_bytes(b"partial audio")
    (album / "cover.jpg").write_bytes(b"art")
    assert dl.discard_download_staging({"_staging_run": run.to_record()}) is True
    assert not run.path.exists()
    retry_dir = tmp_path / cfg.BEETS_RETRY_DIR
    assert not retry_dir.exists() or not any(retry_dir.iterdir())


def test_snapshot_staging_skips_the_beets_retry_tree(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import rip

    staging = tmp_path / "staging"
    (staging / ".beets_retry" / "ParkedArt" / "ParkedAlbum").mkdir(parents=True)
    (staging / ".beets_retry" / "ParkedArt" / "ParkedAlbum" / "1.flac").write_bytes(b"x")
    (staging / "Artist" / "Album").mkdir(parents=True)
    (staging / "Artist" / "Album" / "1.flac").write_bytes(b"x")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    snap = rip.snapshot_staging()
    paths = {str(p.relative_to(staging)) for p in snap}
    assert paths == {"Artist/Album/1.flac"}

    (staging / "Artist" / "Album" / "2.flac").write_bytes(b"y")
    (staging / ".beets_retry" / "ParkedArt" / "ParkedAlbum" / "2.flac").write_bytes(b"y")
    added = {str(p.relative_to(staging)) for p in rip.files_added_since(snap)}
    assert added == {"Artist/Album/2.flac"}


def test_wrong_same_title_retry_file_does_not_clear_reject(monkeypatch, tmp_path):
    tracks = [
        {"id": 101, "title": "Song", "media_number": 1, "track_number": 1},
        {"id": 202, "title": "Song", "media_number": 2, "track_number": 1},
    ]
    disc_1 = tmp_path / "Disc 1"
    disc_2 = tmp_path / "Disc 2"
    wrong_retry_dir = tmp_path / "retry" / "Disc 1"
    disc_1.mkdir()
    disc_2.mkdir()
    wrong_retry_dir.mkdir(parents=True)
    clean = disc_1 / "01 - Song.flac"
    rejected = disc_2 / "01 - Song.mp3"
    wrong_retry = wrong_retry_dir / "01 - Song.flac"
    clean.write_bytes(b"clean")
    rejected.write_bytes(b"lossy")
    wrong_retry.write_bytes(b"wrong disc")
    rips = []

    _patch(
        monkeypatch,
        rip=lambda url, **_k: (rips.append(url), (0, ""))[1],
        added=lambda _s, deltas=iter([[clean, rejected], [wrong_retry]]): next(deltas, []),
        cleanup=lambda _f, outcomes=iter([
            ([clean], [rejected], []), ([wrong_retry], [], []),
        ]): next(outcomes, ([], [], [])),
    )

    result = dl.run_album_download(
        album=_album(tracks), missing=tracks, present=[], album_dir=None,
        snapshot=set(),
    )

    assert rips == ["https://play.qobuz.com/album/ALB",
                    "https://play.qobuz.com/track/202"]
    assert (result["n_ok"], result["n_lossy"], result["n_fail"]) == (1, 1, 0)
