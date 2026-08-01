"""Tests for the repair sweep/scanner and a few CLI entry points. The bulk of
the coverage here is the data-safety machinery around repair: truncated
originals are backed up before a re-rip, and the backup is only dropped once the
refills are proven back in place and re-verified. An outage or a still-short
re-rip must keep the backup rather than lose the only good copy.
"""
import os
from argparse import Namespace
from unittest.mock import patch

import pytest

from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs

# ── scan_dir_for_isrc_repairs: the truncation gates ────────────────────

def _track(isrc="GB1234567890", length=240.0, path="/music/track.flac", **kw):
    return {"isrc": isrc, "length": length, "title": "Track", "path": path,
            "sample_rate": 44100, "bits": 16, "channels": 2, "tracknumber": 1, **kw}


def test_scan_isrc_repairs_truncation_gates(tmp_path):
    # Both gates (duration mismatch + decode) must fire for a "verified truncated".
    # The stub's title matches the file's ("Track"), because a catalogue title
    # naming a different song withdraws the single-track refill on purpose and
    # that identity rule is covered in test_repair_accuracy, not here.
    source = tmp_path / "track.flac"
    source.write_bytes(b"held source")
    track = _track(length=169.0, path=str(source))
    qt = {"duration": 200.0, "title": "Track", "track_number": 1}
    with patch("qobuz_librarian.repair_log._read_held_audio_meta", return_value=track), \
         patch("qobuz_librarian.repair_log._qobuz_track_by_isrc", return_value=qt):
        assert len(scan_dir_for_isrc_repairs(tmp_path, "token")["verified_truncated"]) == 1

    # Zero Qobuz duration → no reliable comparison → don't flag healthy files.
    with patch("qobuz_librarian.repair_log._read_held_audio_meta",
               return_value=_track(length=10.0, path=str(source))), \
         patch("qobuz_librarian.repair_log._qobuz_track_by_isrc",
               return_value={"duration": 0, "title": "Track", "track_number": 1}), \
         patch("qobuz_librarian.repair_log._flac_decode_ok", return_value=True):
        assert scan_dir_for_isrc_repairs(tmp_path, "token")["verified_ok"] == 1

    # No Qobuz duration BUT decode probe fails → flag corruption.
    bad = _track(length=0.0, path=str(source))
    with patch("qobuz_librarian.repair_log._read_held_audio_meta", return_value=bad), \
         patch("qobuz_librarian.repair_log._qobuz_track_by_isrc",
               return_value={"duration": 0, "title": "Track", "track_number": 1}), \
         patch("qobuz_librarian.repair_log._flac_decode_ok", return_value=False):
        assert len(scan_dir_for_isrc_repairs(tmp_path, "token")["verified_truncated"]) == 1


# ── Repair scan: resume from an interrupted sweep ──────────────────────

def test_repair_scan_resumes_from_checkpoint(tmp_path, monkeypatch):
    """An interrupted repair sweep skips the artists already checked, restores
    the albums it flagged, and clears the checkpoint when it finishes cleanly."""
    from qobuz_librarian.library import scan_checkpoint
    from qobuz_librarian.web import flows
    monkeypatch.setattr("qobuz_librarian.config.SCAN_CHECKPOINT_FILE", tmp_path / "cp.json")

    flagged = {"kind": "repair", "title": "Old Album", "artist": "Artist A",
               "detail": "1 truncated track", "selected": True,
               "payload": {"album_dir": str(tmp_path / "Artist A" / "Old Album"),
                           "artist_name": "Artist A",
                           "verified_truncated": [{"path": "x.flac"}]}}
    scan_checkpoint.save("repair", {"Artist A"}, [flagged], {})

    (tmp_path / "Artist A").mkdir()
    (tmp_path / "Artist B" / "New Album").mkdir(parents=True)
    artists = [tmp_path / "Artist A", tmp_path / "Artist B"]

    class _Job:
        def __init__(self):
            self.candidates = []
            self.cancel_requested = False
        def add_candidate(self, **kw):
            self.candidates.append(dict(kw))
        def push_progress(self, *a, **k):
            pass
    job = _Job()

    checked = []
    def fake_scan(album_dir, token, deep=False):
        checked.append(album_dir.name)
        return {"verified_truncated": [], "verified_ok": 1, "no_isrc_tag": []}

    with patch.object(flows, "list_library_artists", return_value=artists), \
         patch.object(flows, "list_artist_album_dirs",
                      side_effect=lambda d: [p for p in d.iterdir() if p.is_dir()]), \
         patch.object(flows, "clear_scan_caches"), \
         patch("qobuz_librarian.repair_log.scan_dir_for_isrc_repairs", side_effect=fake_scan):
        flows.scan_repairs(job, "token")

    assert checked == ["New Album"]                                  # Artist A skipped
    assert any(c["title"] == "Old Album" for c in job.candidates)    # prior flag restored
    assert scan_checkpoint.load("repair") is None                    # cleared on clean finish


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
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": False, "n_ok": 0})
    monkeypatch.setattr("qobuz_librarian.library.backup.restore_upgrade_backup",
                        lambda bp, d: restored.update(bp=bp, dir=d) or True)
    res = flows._redownload_damaged_album(
        {"album_dir": str(album_dir), "album_id": "x"}, "token")
    assert res["n_ok"] == 0
    assert restored == {"bp": backup, "dir": album_dir}


def test_no_isrc_redownload_keeps_an_unprovable_backup(
        tmp_path, monkeypatch):
    # A verified re-download retires the original album's backup only when it
    # can be proven redundant; this fixture's backup can't be, so it stays,
    # and no disposal transaction may even start for it.
    from qobuz_librarian.library.backup import BackupResult
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "01.flac").write_bytes(b"original")
    backup = BackupResult(
        backup_dir,
        complete=True,
        receipt={"kind": "upgrade"},
        requested=1,
        backed_up=1,
    )
    recoveries = []
    monkeypatch.setattr(flows, "get_album", lambda *a: {"id": "x"})
    monkeypatch.setattr(
        "qobuz_librarian.library.backup.backup_album_dir",
        lambda _directory: backup,
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.backup.dispose_backup",
        lambda *_args, **_kwargs: pytest.fail(
            "Repair must not delete the retained original album"),
    )
    monkeypatch.setattr(
        "qobuz_librarian.library.backup.pin_unverified_upgrade_backup",
        lambda *_args, **_kwargs: True,
    )

    def process_after_recovery(*_args, **_kwargs):
        assert recoveries and recoveries[-1].stage == "backup"
        return {"imported": True, "n_ok": 1, "n_fail": 0}

    monkeypatch.setattr(
        "qobuz_librarian.modes.process.process_album",
        process_after_recovery,
    )
    monkeypatch.setattr(
        "qobuz_librarian.modes.process._upgrade_replacement_verified",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "qobuz_librarian.modes.process._carry_non_audio_from_backup",
        lambda *_args: (album_dir, {"exact": "replacement"}),
    )

    result = flows._redownload_damaged_album(
        {"album_dir": str(album_dir), "album_id": "x"},
        "token",
        recovery_checkpoint=lambda recovery: recoveries.append(recovery) or True,
    )

    assert "repair_unverified" not in result
    assert backup_dir.is_dir()
    assert recoveries[-1].retained is True
    assert recoveries[-1].backup is backup


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


def test_repair_relocation_refuses_a_symlinked_refill_folder(
        tmp_path, monkeypatch):
    from qobuz_librarian.modes import repair

    music_root = tmp_path / "Music"
    album_dir = music_root / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    refill = outside / "01.flac"
    refill.write_bytes(b"outside")
    landed_dir = music_root / "Artist" / "Refill"
    landed_dir.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(repair.cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(repair.cfg, "BEETS_DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(
        repair, "_read_repair_isrc", lambda _fd: "GBCFB1300101")

    with pytest.raises(repair._RepairRelocationUncertain):
        repair._relocate_refilled_into_album_dir(
            album_dir,
            landed_dir,
            {"GBCFB1300101"},
            before_names=set(),
            ownership_receipt=_sealed_import_receipt(
                music_root,
                [refill],
                outside,
                relatives=["Artist/Refill/01.flac"],
                scope_relative="Artist/Refill",
            ),
            expected_refills=1,
        )
    assert refill.read_bytes() == b"outside"
    assert not (album_dir / refill.name).exists()


def test_refills_intact_requires_every_wanted_isrc_to_reverify(tmp_path, monkeypatch):
    # Before the truncated originals' backup is trusted as redundant, the
    # rebuilt folder is re-scanned and EVERY backed-up ISRC must positively
    # re-verify.
    from collections import Counter

    from qobuz_librarian.modes import repair
    wanted = Counter({"GBCFB1300101": 1, "USRC11700001": 1})

    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs":
                                         Counter({"gbcfb1300101": 1, "USRC1-17-00001": 1})})
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is True

    # One ISRC didn't re-verify → keep the backup.
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs": Counter({"GBCFB1300101": 1})})
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is False


def test_refills_intact_counts_duplicate_isrcs_as_a_multiset(tmp_path, monkeypatch):
    # Two truncated files can share one ISRC (a .1.flac collision pair, or the
    # same recording on two discs).
    from collections import Counter

    from qobuz_librarian.modes import repair
    wanted = Counter({"GBCFB1300101": 2})

    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs": Counter({"gbcfb1300101": 1})})
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is False

    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs": Counter({"gbcfb1300101": 2})})
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is True


def test_refills_intact_propagates_qobuz_outage(tmp_path, monkeypatch):
    # A token loss or Qobuz outage during re-verification must propagate, not
    # collapse to "still truncated": an outage is not a verdict on the refill.
    from collections import Counter

    from qobuz_librarian.modes import repair

    wanted = Counter({"GBCFB1300101": 1})

    def raise_authlost(*a, **k):
        raise repair.AuthLost("token lost")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", raise_authlost)
    with pytest.raises(repair.AuthLost):
        repair._refills_intact(tmp_path, wanted, "tok", Counter())

    def raise_unavailable(*a, **k):
        raise repair.QobuzUnavailable("upstream down")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", raise_unavailable)
    with pytest.raises(repair.QobuzUnavailable):
        repair._refills_intact(tmp_path, wanted, "tok", Counter())


def test_refills_intact_keeps_backup_on_an_unexpected_rescan_error(tmp_path, monkeypatch):
    # Any non-outage failure of the re-scan stays conservative: return False so
    # the caller keeps the backup rather than delete originals on an error we
    # can't interpret.
    from collections import Counter

    from qobuz_librarian.modes import repair

    wanted = Counter({"GBCFB1300101": 1})

    def boom(*a, **k):
        raise ValueError("malformed scan result")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", boom)
    assert repair._refills_intact(tmp_path, wanted, "tok", Counter()) is False


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


# ── CLI parse_args guards ───────────────────────────────────────────────

def _parse_argv(argv):
    import sys

    from qobuz_librarian.cli import parse_args
    with patch.object(sys, "argv", ["qobuz-librarian", *argv]):
        return parse_args()


def test_parse_args_rejects_incompatible_flag_combos():
    # Each of these combos silently dropped one side before, so reject at parse.
    invalid = [
        ["--auto-safe", "Some Artist - Album"],
        ["--force", "--artist", "Radiohead"],
        ["--artist", ""],
        ["--artist", "   "],
        ["--no-catalog", "Some Artist - Album"],
        ["--include-comps", "--upgrade-walk"],
        ["--no-upgrade", "--upgrade-walk"],
        ["--include-singles", "--upgrade-walk"],
        ["--artist", "Radiohead", "--upgrade-walk"],
        ["--artist", "Four Tet", "some album"],
        ["--reset-walk-seen", "--artist", "Radiohead"],
        ["--reset-walk-seen", "Some Artist - Album"],
        ["--quiet"],
        # the local-only walk/migrate modes read none of these flags either
        ["--force", "--downsample-walk"],
        ["--include-singles", "--lyrics-walk"],
        ["--include-comps", "--migrate"],
        ["--no-catalog", "--lyrics-walk"],
    ]
    for argv in invalid:
        with pytest.raises(SystemExit):
            _parse_argv(argv)


# ── Repair: backup resolution branches (the core data-safety machinery) ─

def _call_repair_album_dir(tmp_path, monkeypatch, *, n_ok, n_fail, imported,
                           present=True, intact=True, recovery_checkpoint=None,
                           execute_calls=None, relocation_error=None,
                           retire=None):
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.library.backup import capture_gap_fill_source_receipt

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
        if execute_calls is not None:
            execute_calls.append(queue)
        for qi in queue:
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

    vt = [{"path": str(track), "title": "Track 01", "isrc": "USRC11111111",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0,
           "source_receipt": capture_gap_fill_source_receipt(
               track, album_dir)}]
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


def test_repair_backup_kept_when_downloads_fail_and_skipped_when_backup_fails(tmp_path, monkeypatch):
    # Downloads fail → backup is preserved for manual recovery.
    _call_repair_album_dir(tmp_path / "kept", monkeypatch, n_ok=0, n_fail=1, imported=False)
    assert _backup_files(tmp_path / "kept")

    # Backup itself fails → original must NOT be queued for replacement.
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.library.backup import (
        BackupResult,
        capture_gap_fill_source_receipt,
    )
    album_dir = tmp_path / "nb" / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr(repair_mod.cfg, "MUSIC_ROOT", tmp_path / "nb")
    monkeypatch.setattr(repair_mod, "find_qobuz_album_for_dir",
                        lambda *a, **k: None)
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})
    monkeypatch.setattr(
        repair_mod,
        "backup_gap_fill_files",
        lambda paths, d, **kwargs: None,
    )
    monkeypatch.setattr(repair_mod, "_execute_download_queue",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("must not run when backup fails")))
    vt = [{"path": str(track), "title": "Track 01",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0,
           "source_receipt": capture_gap_fill_source_receipt(
               track, album_dir)}]
    args = Namespace(force=False, yes=True, prefer_hires=False, consolidate=False, no_upgrade=False)
    res = repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")
    assert track.exists() and res["n_fail"] == len(vt)

    partial_dir = tmp_path / "nb" / "backups" / "partial"

    def partial_backup(_paths, directory, **_kwargs):
        partial_dir.mkdir(parents=True)
        track.replace(partial_dir / track.name)
        return BackupResult(
            partial_dir,
            complete=False,
            receipt={"kind": "gap-fill", "origin": str(directory)},
            requested=2,
            backed_up=1,
        )

    def restore_partial(carried, directory):
        assert carried.path == partial_dir
        (partial_dir / track.name).replace(directory / track.name)
        partial_dir.rmdir()
        return 1

    monkeypatch.setattr(repair_mod, "backup_gap_fill_files", partial_backup)
    monkeypatch.setattr(repair_mod, "restore_gap_fill_backup", restore_partial)
    res = repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")
    assert track.exists() and res["backup"] is None


# ── Walk-seen state: crash-safe atomic write ───────────────────────────

def test_walk_seen_records_idempotently_and_survives_a_crashed_rename(tmp_path, monkeypatch):
    import qobuz_librarian.modes.walk as walk_mod
    from qobuz_librarian.modes.walk import load_walk_seen, record_walk_seen
    f = tmp_path / "walk_seen.txt"
    monkeypatch.setattr("qobuz_librarian.config.WALK_SEEN_FILE", f)
    record_walk_seen("Radiohead")
    record_walk_seen("Radiohead")  # idempotent
    assert "radiohead" in load_walk_seen()
    prior = f.read_bytes()

    # If os.replace fails the file must not be half-written.
    monkeypatch.setattr(walk_mod.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("crashed")))
    record_walk_seen("Portishead")
    assert f.read_bytes() == prior
    assert load_walk_seen() == {"radiohead"}


# ── Scan-report-repair classifications ──────────────────────────────────

def _call_scan_report(tmp_path, monkeypatch, *, repair_result=None,
                     verified_truncated=None, yes=True, input_return="y"):
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.modes.repair import _scan_report_repair
    album_dir = tmp_path / "Artist" / "Album (2022)"
    album_dir.mkdir(parents=True)
    (album_dir / "01 Track.flac").write_bytes(b"\x00" * 200)
    if verified_truncated is None:
        verified_truncated = [{"path": str(album_dir / "01 Track.flac"),
                                "title": "Track 01", "isrc": "USRC12345678",
                                "track_number": 1, "file_length": 5.0,
                                "qobuz_duration": 180.0,
                                "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "A1"}}}]
    monkeypatch.setattr(repair_mod, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_truncated": verified_truncated,
                                         "verified_ok": 0, "isrc_no_match": [], "no_isrc_tag": []})
    if repair_result is not None:
        monkeypatch.setattr(repair_mod, "repair_album_dir", lambda *a, **k: repair_result)
    monkeypatch.setattr(repair_mod, "section", lambda *a: None)
    args = Namespace(force=False, yes=yes, prefer_hires=False, consolidate=False, no_upgrade=False)
    with patch("builtins.input", return_value=input_return):
        return _scan_report_repair(album_dir, "Artist", args, "tok")


def test_scan_report_classifies_repair_outcomes(tmp_path, monkeypatch):
    from qobuz_librarian.library.backup import BackupResult

    # Repair succeeds → "repaired".
    assert _call_scan_report(tmp_path / "ok", monkeypatch,
                             repair_result={"n_ok": 1, "n_fail": 0, "imported": True, "backup": None}) == "repaired"
    # Downloads succeeded but beets failed silently → classified as failure.
    assert _call_scan_report(tmp_path / "silent", monkeypatch,
                             repair_result={"n_ok": 1, "n_fail": 0, "imported": False, "backup": None}) == "failed"
    recovery = BackupResult(
        tmp_path / "kept-originals",
        complete=False,
        receipt={"kind": "gap-fill"},
        requested=2,
        backed_up=1,
    )
    assert _call_scan_report(
        tmp_path / "recovery",
        monkeypatch,
        repair_result={
            "n_ok": 0,
            "n_fail": 1,
            "imported": False,
            "backup": recovery,
        },
    ) == "recovery"
    # Nothing truncated → "clean".
    assert _call_scan_report(tmp_path / "clean", monkeypatch, verified_truncated=[]) == "clean"
    # User declines the prompt → "skipped".
    assert _call_scan_report(tmp_path / "skip", monkeypatch,
                             yes=False, input_return="n") == "skipped"


def test_execute_repairs_does_not_count_an_unverified_redownload_as_repaired(monkeypatch):
    # A whole-album re-download that imported but failed the completeness check
    # keeps the backup and must not render "Repaired 1/1"; the active copy is
    # an unverified, possibly incomplete replacement.
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as job_mgr

    class _Job:
        cancel_requested = False
        _progress_scope = None
        _imported_any = False
        summary = ""
        error = ""
        status = job_mgr.JobStatus.RUNNING

        def push_progress(self, *a, **k):
            pass

    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "build_args", lambda: Namespace())
    monkeypatch.setattr(flows, "_note_staging_wait", lambda *a, **k: None)
    callback_seen = False

    def unverified_redownload(_payload, _token, *, recovery_checkpoint=None):
        nonlocal callback_seen
        assert callable(recovery_checkpoint)
        callback_seen = True
        return {"imported": True, "n_ok": 8,
                "n_fail": 0, "repair_unverified": True}

    monkeypatch.setattr(
        flows, "_redownload_damaged_album", unverified_redownload)
    monkeypatch.setattr(flows.time, "sleep", lambda _s: None)

    job = _Job()
    chosen = [{"kind": "redownload", "title": "Album",
               "payload": {"artist_name": "Artist", "album_dir": "/x"}}]
    flows.execute_repairs(job, chosen, "tok")

    assert callback_seen
    assert "Repaired 0/1" in job.summary
    assert job.error
    assert job.status == job_mgr.JobStatus.FAILED


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


def test_retag_marks_an_unconsumed_twin_source_failed(tmp_path, monkeypatch):
    # Two same-ISRC originals but only one refill surfaced in staging: one
    # original's tags/art never landed anywhere, so the ISRC must read as
    # failed and the backup (their only copy) kept.
    from qobuz_librarian.modes import repair

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "01 - Song.flac").write_bytes(b"x")
    monkeypatch.setattr(repair, "_FLAC", lambda fp: {"isrc": ["USRC11111111"]})
    monkeypatch.setattr(repair, "_snapshot_flac_metadata", lambda src: {"of": str(src)})
    monkeypatch.setattr(repair, "_restore_flac_metadata", lambda fp, snap: True)

    sources = {"USRC11111111": [tmp_path / "a.flac", tmp_path / "b.flac"]}
    failed = repair._retag_refills_in_staging([staged], sources)
    assert failed == {"USRC11111111"}


def test_retag_callback_records_total_failure_on_exception(monkeypatch):
    # The executor catches and logs a retag exception, so the carry state is
    # unknown to the backup resolution; every source must already be marked
    # failed, or the empty set reads as "all tags carried" and the only copy
    # of the originals' metadata is deleted.
    from pathlib import Path

    from qobuz_librarian.modes import repair

    failed = set()
    sources = {"ISRC1": [Path("/x")], "ISRC2": [Path("/y")]}

    def boom(_dirs, _sources):
        raise RuntimeError("tag write exploded")
    monkeypatch.setattr(repair, "_retag_refills_in_staging", boom)

    cb = repair._make_retag_callback(sources, failed)
    with pytest.raises(RuntimeError):
        cb([Path("/staged")])
    assert failed == {"ISRC1", "ISRC2"}


def test_repair_pins_the_backup_when_the_tag_carry_fails(tmp_path, monkeypatch):
    # Audio verifiably repaired but the originals' tags couldn't be carried:
    # the backup is kept AND pinned: the age sweep proves redundancy by
    # same-path same-or-larger bytes, which the refill satisfies, so without
    # the pin the only copy of those tags is reaped on schedule.
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.library.backup import (
        _UNVERIFIED_UPGRADE_SENTINEL,
        capture_gap_fill_source_receipt,
    )

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

    vt = [{"path": str(track), "title": "Track 01", "isrc": "USRC11111111",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0,
           "source_receipt": capture_gap_fill_source_receipt(
               track, album_dir)}]
    args = Namespace(force=False, yes=True, prefer_hires=False,
                     consolidate=False, no_upgrade=False)
    repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")

    backups = tmp_path / "backups"
    pins = list(backups.rglob(_UNVERIFIED_UPGRADE_SENTINEL))
    assert pins, "the kept backup must carry a never-reap pin"
    kept = list(backups.rglob("01 - Track.flac"))
    assert kept and kept[0].read_bytes() == b"\x00" * 200


def test_strict_confirm_reasks_on_a_typo(monkeypatch):
    # The downsample keep-vs-delete answer is SAVED as the standing default,
    # so a typo must not read as "delete the originals from now on": strict
    # mode re-asks until it gets a real yes or no.
    from qobuz_librarian.ui_cli import prompts

    answers = iter(["maybe", "y"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert prompts.confirm("Keep?", default_yes=True, strict=True) is True

    answers = iter(["whatever", "n"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert prompts.confirm("Keep?", default_yes=True, strict=True) is False

    monkeypatch.setattr("builtins.input", lambda _p: "maybe")
    assert prompts.confirm("Keep?", default_yes=True) is False


def test_repair_with_no_successful_refill_reports_the_download_failure(
        tmp_path, monkeypatch):
    """A repair whose downloads all fail has no import receipt to read, so
    asking placement to prove itself turned the honest failure into "the final
    location could not be proven" with a placement-stage recovery record. The
    truthful branch was unreachable for a total failure."""
    import qobuz_librarian.modes.repair as repair_mod

    recoveries = []
    result, _root = _call_repair_album_dir(
        tmp_path, monkeypatch,
        n_ok=0, n_fail=1, imported=False, present=False, intact=False,
        relocation_error=repair_mod._RepairRelocationUncertain(
            "repair import receipt has an invalid root"),
        recovery_checkpoint=lambda recovery: recoveries.append(recovery) or True,
    )

    assert result["n_ok"] == 0 and result["n_fail"] == 1
    assert [r.stage for r in recoveries][-1] == "refill"
    assert not any("could not be proven" in r.reason for r in recoveries)


def test_refill_is_not_rejected_because_another_folder_holds_that_name(
        tmp_path, monkeypatch):
    """A track that appears on two records used to break its own repair.

    before_names is the census of ONE folder, the one the resolved parent
    album maps to. Comparing a receipt item's bare filename against it, with
    no check that the item landed there, rejected a refill that had gone
    somewhere else entirely because an unrelated album happened to hold a
    track of the same name. The repair was correct on disk and the user was
    told its location could not be proven.
    """
    repair, album_dir, landed_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    elsewhere = repair.cfg.MUSIC_ROOT / "Other Artist" / "Other Album (1995)"
    elsewhere.mkdir(parents=True)
    (elsewhere / "07 - Melissa Juice.flac").write_bytes(b"unrelated copy")
    refill = landed_dir / "07 - Melissa Juice.flac"
    refill.write_bytes(b"receipt-owned-refill")

    moved = repair._relocate_refilled_into_album_dir(
        album_dir,
        landed_dir,
        {"GBCFB1300101"},
        before_names={"07 - Melissa Juice.flac"},
        before_dir=elsewhere,
        ownership_receipt=_sealed_import_receipt(
            repair.cfg.MUSIC_ROOT, [refill], landed_dir),
        expected_refills=1,
    )
    assert moved == 1, "the refill landed outside the sampled folder"
    assert (album_dir / refill.name).read_bytes() == b"receipt-owned-refill"
    assert (elsewhere / "07 - Melissa Juice.flac").read_bytes() == (
        b"unrelated copy")


def test_refill_is_still_rejected_when_that_folder_already_held_it(
        tmp_path, monkeypatch):
    """The guard itself has to survive the fix above: a receipt naming a file
    that was already sitting in the sampled folder cannot be trusted as a
    fresh refill, because nothing distinguishes it from what was there."""
    repair, album_dir, landed_dir = _repair_relocation_dirs(
        tmp_path, monkeypatch)
    refill = landed_dir / "01 - First Fires.flac"
    refill.write_bytes(b"was-already-here")

    with pytest.raises(repair._RepairRelocationUncertain):
        repair._relocate_refilled_into_album_dir(
            album_dir,
            landed_dir,
            {"GBCFB1300101"},
            before_names={"01 - First Fires.flac"},
            before_dir=landed_dir,
            ownership_receipt=_sealed_import_receipt(
                repair.cfg.MUSIC_ROOT, [refill], landed_dir),
            expected_refills=1,
        )
    assert refill.read_bytes() == b"was-already-here"
