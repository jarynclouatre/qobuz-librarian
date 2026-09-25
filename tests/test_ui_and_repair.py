"""Tests for the repair sweep. The bulk of the coverage here is the data-safety
machinery around repair: truncated originals are backed up before a re-rip, and
the backup is only dropped once the refills are proven back in place and
re-verified. A still-short re-rip keeps the backup, and a failed download puts
the originals back.
"""
import os
from argparse import Namespace

# ── Repair scan: resume from an interrupted sweep ──────────────────────


def test_no_isrc_redownload_failure_restores_original_folder(tmp_path, monkeypatch):
    from qobuz_librarian.library.backup import BackupResult
    from qobuz_librarian.web import flows
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    backup_dir = tmp_path / "backup"
    restored = {}
    monkeypatch.setattr(flows, "get_album", lambda *a: {"id": "x"})
    backup = BackupResult(
        backup_dir,
        complete=True,
        receipt={},
        requested=1,
        backed_up=1,
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.backup.backup_album_dir", lambda d: backup)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.beets.capture_beets_album_entries",
        lambda _directory: object(),
    )
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": False, "n_ok": 0})
    monkeypatch.setattr("qobuz_librarian.library.backup.restore_upgrade_backup",
                        lambda bp, d: restored.update(bp=bp, dir=d) or True)
    res = flows._redownload_damaged_album(
        {"album_dir": str(album_dir), "album_id": "x"}, "token")
    assert res["n_ok"] == 0
    assert restored == {"bp": backup, "dir": album_dir}


# ── Repair: relocate refilled tracks back to the album folder ─────────

def _repair_relocation_dirs(tmp_path, monkeypatch):
    from qobuz_librarian.modes import repair

    music_root = tmp_path / "Music"
    album_dir = music_root / "Artist" / "First Fires (2013)"
    landed_dir = music_root / "Artist" / "The North Borders (2013)"
    album_dir.mkdir(parents=True)
    landed_dir.mkdir()
    monkeypatch.setattr(repair.cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(repair.cfg, "BEETS_DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(
        repair, "_read_repair_isrc", lambda _fd: "GBCFB1300101")
    return repair, album_dir, landed_dir


def _receipt_identity(path):
    value = os.stat(path, follow_symlinks=False)
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "modified_ns": value.st_mtime_ns,
        "changed_ns": value.st_ctime_ns,
    }


def _sealed_import_receipt(
        root, files, album_scope, *, relatives=None, scope_relative=None,
        created_directories=None):
    relative_values = (
        relatives
        if relatives is not None
        else [path.relative_to(root).as_posix() for path in files]
    )
    scope_value = (
        scope_relative
        if scope_relative is not None
        else album_scope.relative_to(root).as_posix()
    )
    if created_directories is None:
        created_directories = [(scope_value, album_scope)]
    created_records = [
        {"relative": relative, **_receipt_identity(path)}
        for relative, path in created_directories
    ]
    return {
        "version": 1,
        "root": str(root),
        "root_identity": _receipt_identity(root),
        "sealed": True,
        "items": [
            {
                "relative": relative,
                "file": _receipt_identity(path),
                "album_scope": {
                    "relative": scope_value,
                    "directory": _receipt_identity(album_scope),
                },
                "created_directories": [
                    dict(record) for record in created_records
                ],
            }
            for path, relative in zip(files, relative_values)
        ],
    }


def test_repair_leaves_a_preexisting_track_sharing_the_recording_alone(tmp_path, monkeypatch):
    # A track that was already in the target dir's sibling album under the
    # same ISRC must NOT be moved; it isn't a refill, it's an existing copy.
    repair, album_dir, owned_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    owned = owned_dir / "01 - First Fires.flac"
    owned.write_bytes(b"already-here")
    refill = owned_dir / "02 - First Fires refill.flac"
    refill.write_bytes(b"receipt-owned-refill")

    moved = repair._relocate_refilled_into_album_dir(
        album_dir,
        owned_dir,
        {"GBCFB1300101"},
        before_names={"01 - First Fires.flac"},
        ownership_receipt=_sealed_import_receipt(
            repair.cfg.MUSIC_ROOT, [refill], owned_dir),
        expected_refills=1,
    )
    assert moved == 1 and owned.read_bytes() == b"already-here"
    assert not (album_dir / "01 - First Fires.flac").exists()
    assert (album_dir / refill.name).read_bytes() == b"receipt-owned-refill"


# ── Repair: backup resolution branches (the core data-safety machinery) ─

def _call_repair_album_dir(tmp_path, monkeypatch, *, n_ok, n_fail, imported,
                           present=True, intact=True, recovery_checkpoint=None,
                           execute_calls=None, relocation_error=None,
                           retire=None):
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian import repair_log

    if retire is not None:
        monkeypatch.setattr(repair_mod, "retire_verified_repair_backup",
                            lambda _backup: retire)

    album_dir = tmp_path / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.REPAIR_LOG_PATH", tmp_path / "repair.log")
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})
    # Parent-album resolution prefers the folder match; with none, it falls
    # back to the most-common ISRC album (get_album above).
    monkeypatch.setattr(repair_mod, "find_qobuz_album_for_dir",
                        lambda *a, **k: None)

    def fake_execute(queue, args, token):
        from qobuz_librarian.library.candidate_premise import validate_premise

        if execute_calls is not None:
            execute_calls.append(queue)
        for qi in queue:
            validate_premise(qi["_source_premise"])
            qi["n_ok"] = n_ok
            qi["n_fail"] = n_fail
            qi["imported"] = imported

    monkeypatch.setattr(repair_mod, "_execute_download_queue", fake_execute)
    def relocate(*_args, **_kwargs):
        if relocation_error is not None:
            raise relocation_error
        return 0

    monkeypatch.setattr(
        repair_mod, "_relocate_refilled_into_album_dir", relocate)
    monkeypatch.setattr(repair_mod, "append_repair_log", lambda e: True)
    # The dummy file isn't a real FLAC, so drive the post-refill verification
    # gate directly: `present` = the refilled tracks returned to album_dir,
    # `intact` = the re-scan found them no longer truncated.
    monkeypatch.setattr(repair_mod, "_refills_present_in", lambda d, w, b: present)
    monkeypatch.setattr(repair_mod, "_refills_intact", lambda d, w, t, b: intact)

    with repair_log._HeldRepairSource(album_dir, track) as source:
        source_receipt = source.source_receipt
    vt = [{"path": str(track), "title": "Track 01", "isrc": "USRC11111111",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0,
           "source_receipt": source_receipt}]
    args = Namespace(force=False, yes=True, prefer_hires=False, consolidate=False, no_upgrade=False)
    return repair_mod.repair_album_dir(
        album_dir,
        vt,
        "Artist",
        args,
        "tok",
        recovery_checkpoint=recovery_checkpoint,
    ), tmp_path


def _backup_files(tmp_path):
    root = tmp_path / "backups"
    return list(root.rglob("*")) if root.exists() else []


def test_repair_counts_a_verified_refill_and_settles_its_backup(
        tmp_path, monkeypatch):
    # This fixture album never receives a superseding track, so the real
    # retirement proof refuses and the backup is kept; the repair still
    # counts; the kept backup rides along for the summary's recovery tail.
    result, p = _call_repair_album_dir(tmp_path / "kept", monkeypatch,
                                       n_ok=1, n_fail=0, imported=True,
                                       present=True, intact=True)
    assert [f for f in _backup_files(p) if f.is_file()]
    assert result["n_ok"] == 1
    assert result["imported"] is True
    assert result["backup"] is not None

    # When the originals' backup is provably superseded it is retired and
    # the recovery record resolves.
    result, p = _call_repair_album_dir(tmp_path / "ok", monkeypatch,
                                       n_ok=1, n_fail=0, imported=True,
                                       present=True, intact=True, retire=True)
    assert result["n_ok"] == 1
    assert result["imported"] is True
    assert result["backup"] is None

    # Re-downloaded but still truncated (a short re-rip passing the decode
    # gate): the originals' backup is KEPT, not deleted on presence alone, and
    # the repair isn't reported as a success.
    result, p = _call_repair_album_dir(tmp_path / "short", monkeypatch,
                                       n_ok=1, n_fail=0, imported=True,
                                       present=True, intact=False)
    assert [f for f in _backup_files(p) if f.is_file()]
    assert result["n_ok"] == 0

    # Silent beets failure (downloads succeeded but import didn't, so nothing
    # returned to the folder): roll back to the pre-repair originals.
    result, p = _call_repair_album_dir(tmp_path / "silent", monkeypatch,
                                       n_ok=1, n_fail=0, imported=False,
                                       present=False)
    assert [f for f in _backup_files(p) if f.is_file()] == []
    assert (p / "Artist" / "Album (2020)" / "01 - Track.flac").exists()
    assert result["imported"] is False

    # Failed downloads put the originals back too.
    _, p = _call_repair_album_dir(tmp_path / "failed", monkeypatch,
                                  n_ok=0, n_fail=1, imported=False)
    assert [f for f in _backup_files(p) if f.is_file()] == []
    assert (p / "Artist" / "Album (2020)" / "01 - Track.flac").exists()


# ── Scan-report-repair classifications ──────────────────────────────────


def test_refill_gates_require_refills_on_top_of_the_baseline(tmp_path, monkeypatch):
    # A healthy PRE-EXISTING file sharing the wanted ISRC (a twin on another
    # disc that was never truncated) must not vouch for a refill that never
    # came back: both gates count against the post-backup baseline, and an
    # unreadable baseline (None) is unverifiable, never a pass.
    from collections import Counter

    from qobuz_librarian.modes import repair
    wanted = Counter({"USRC11111111": 1})
    baseline = Counter({"USRC11111111": 1})

    # Only the healthy twin is on disk; the refill is absent.
    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: [{"isrc": "USRC11111111"}])
    assert repair._refills_present_in(tmp_path, wanted, baseline) is False
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs":
                                         Counter({"USRC11111111": 1})})
    assert repair._refills_intact(tmp_path, wanted, "tok", baseline) is False

    # Twin plus the returned refill: both gates clear.
    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: [{"isrc": "USRC11111111"},
                                   {"isrc": "USRC11111111"}])
    assert repair._refills_present_in(tmp_path, wanted, baseline) is True
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs":
                                         Counter({"USRC11111111": 2})})
    assert repair._refills_intact(tmp_path, wanted, "tok", baseline) is True

    assert repair._refills_present_in(tmp_path, wanted, None) is False
    assert repair._refills_intact(tmp_path, wanted, "tok", None) is False

    # Every wanted ISRC has to re-verify, not just one of them.
    both = Counter({"USRC11111111": 1, "GBCFB1300101": 1})
    assert repair._refills_intact(tmp_path, both, "tok", Counter()) is False
    # A re-scan error nobody can interpret keeps the backup.
    def boom(*_a, **_k):
        raise ValueError("malformed scan result")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", boom)
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is False


def test_backup_sources_keep_both_same_isrc_originals(tmp_path):
    # Two originals can share an ISRC with distinct disc/track tags and art;
    # collapsing them to one path stamps one twin's metadata onto both refills
    # and lets the "successful" repair delete the other's only copy.
    from qobuz_librarian.modes import repair

    album = tmp_path / "Album"
    (album / "CD 2").mkdir(parents=True)
    bk = tmp_path / "bk"
    (bk / "CD 2").mkdir(parents=True)
    (bk / "01 - Song.flac").write_bytes(b"a")
    (bk / "CD 2" / "01 - Song.flac").write_bytes(b"b")
    vt = [{"isrc": "USRC11111111", "path": str(album / "01 - Song.flac")},
          {"isrc": "USRC11111111", "path": str(album / "CD 2" / "01 - Song.flac")}]

    out = repair._backup_source_by_isrc(vt, album, bk)
    assert out == {"USRC11111111": [bk / "01 - Song.flac",
                                    bk / "CD 2" / "01 - Song.flac"]}


def test_repair_pins_the_backup_when_the_tag_carry_fails(tmp_path, monkeypatch):
    # Audio verifiably repaired but the originals' tags couldn't be carried:
    # the backup is kept AND pinned: the age sweep proves redundancy by
    # same-path same-or-larger bytes, which the refill satisfies, so without
    # the pin the only copy of those tags is reaped on schedule.
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian import repair_log
    from qobuz_librarian.library import backup

    album_dir = tmp_path / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr(repair_mod.cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.REPAIR_LOG_PATH", tmp_path / "repair.log")
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})
    monkeypatch.setattr(repair_mod, "find_qobuz_album_for_dir", lambda *a, **k: None)

    def fake_execute(queue, args, token):
        for qi in queue:
            qi["n_ok"] = 1
            qi["n_fail"] = 0
            qi["imported"] = True
            retag = qi.get("pre_import_retag")
            if callable(retag):
                # No staged refill carries any tags, so the whole carry fails.
                retag([])

    monkeypatch.setattr(repair_mod, "_execute_download_queue", fake_execute)
    monkeypatch.setattr(
        repair_mod, "_relocate_refilled_into_album_dir", lambda *a, **k: 0)
    monkeypatch.setattr(repair_mod, "append_repair_log", lambda e: True)
    monkeypatch.setattr(repair_mod, "_refills_present_in", lambda d, w, b: True)
    monkeypatch.setattr(repair_mod, "_refills_intact", lambda d, w, t, b: True)

    with repair_log._HeldRepairSource(album_dir, track) as source:
        source_receipt = source.source_receipt
    vt = [{"path": str(track), "title": "Track 01", "isrc": "USRC11111111",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0,
           "source_receipt": source_receipt}]
    args = Namespace(force=False, yes=True, prefer_hires=False,
                     consolidate=False, no_upgrade=False)
    repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")

    backups = tmp_path / "backups"
    pins = list(backups.rglob(backup._UNVERIFIED_UPGRADE_SENTINEL))
    assert pins, "the kept backup must carry a never-reap pin"
    kept = list(backups.rglob("01 - Track.flac"))
    assert kept and kept[0].read_bytes() == b"\x00" * 200
