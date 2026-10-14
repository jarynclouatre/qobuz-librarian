"""Tests for backup/restore safety and consolidation helpers."""
import os
import shutil
import time
from pathlib import Path

import pytest

from qobuz_librarian.library.backup import (
    backup_album_dir,
    backup_gap_fill_files,
    cleanup_old_upgrade_backups,
    restore_gap_fill_backup,
    restore_upgrade_backup,
)
from qobuz_librarian.modes.consolidate import (
    execute_consolidation,
    match_sibling_track,
)


def _need_audio_tools():
    if not (shutil.which("ffmpeg") and shutil.which("flac")):
        pytest.skip("ffmpeg/flac not available")


def _real_flac(path, *, seconds=2):
    """Encode a short white-noise FLAC that actually decodes with ``flac -t``."""
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anoisesrc=duration={seconds}:color=white:amplitude=0.5",
         "-ac", "2", "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
         str(path)], check=True)


def _seal_test_backup(bk, path, origin, *, kind="gap-fill", complete=True,
                      owner=None):
    """Give a hand-built fixture the same carried receipt production uses."""
    descriptor = bk._open_backup_directory(path)
    try:
        if bk._named_entry_missing(descriptor, bk._ORIGIN_SIDECAR):
            assert bk._write_backup_origin_durable(descriptor, origin)
        manifest = bk._tree_manifest(descriptor)
        assert manifest is not None
        result = bk._seal_backup_result(
            path,
            descriptor,
            origin,
            kind=kind,
            complete=complete,
            requested=len(manifest),
            backed_up=len(manifest),
            owner=owner,
        )
        assert result.receipt is not None
        return result
    finally:
        os.close(descriptor)


def test_backup_refuses_anything_it_cannot_protect_and_leaves_the_album(
        tmp_path, monkeypatch):
    """A cross-filesystem copy corrupted at the same size or left unflushed, a
    symlinked folder, a reserved sidecar name inside the album and an origin
    that cannot be recorded each refuse the backup and leave the album as it
    was."""
    import qobuz_librarian.library.backup as bk

    def use(root, music=None):
        monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", root / "backups")
        monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music or root)
        return root

    root = use(tmp_path / "corrupt")
    album = root / "Album (2026)"
    album.mkdir(parents=True)
    original = b"REAL-FLAC-AUDIO-CONTENT"
    (album / "01.flac").write_bytes(original)
    real_copytree = shutil.copytree

    def corrupt_copytree(src, dst, *a, **k):
        real_copytree(src, dst, *a, **k)
        for f in (root / "backups").rglob("*"):
            if f.is_file():
                f.write_bytes(b"\x00" * f.stat().st_size)
        return dst

    with monkeypatch.context() as patch:
        patch.setattr(bk, "_same_filesystem", lambda a, b: False)
        patch.setattr(bk.shutil, "copytree", corrupt_copytree)
        assert backup_album_dir(album) is None
    assert (album / "01.flac").read_bytes() == original

    root = use(tmp_path / "unflushed")
    album = root / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"audio-bytes")
    with monkeypatch.context() as patch:
        patch.setattr(bk, "_same_filesystem", lambda a, b: False)
        patch.setattr(bk, "_fsync_exact_tree", lambda *_a, **_k: False)
        assert backup_album_dir(album) is None
    assert (album / "01.flac").read_bytes() == b"audio-bytes"

    root = use(tmp_path / "linked")
    album = root / "My Album"
    album.mkdir(parents=True)
    (album / "track.flac").write_bytes(b"audio")
    bp = backup_album_dir(album)
    assert bp is not None and bp.exists() and not album.exists()
    target = root / "real_album"
    target.mkdir()
    (target / "track.flac").write_bytes(b"audio")
    link = root / "linked_album"
    link.symlink_to(target)
    assert backup_album_dir(link) is None
    assert target.exists()

    root = tmp_path / "reserved"
    use(root, root / "music")
    album = root / "music" / "Artist" / "Album"
    (album / "Disc 1").mkdir(parents=True)
    (album / "01.flac").write_bytes(b"audio")
    reserved = album / "Disc 1" / bk._RECEIPT_SIDECAR
    reserved.write_text("user content", encoding="utf-8")
    assert bk.backup_album_dir(album) is None
    assert reserved.read_text(encoding="utf-8") == "user content"
    assert (album / "01.flac").read_bytes() == b"audio"

    root = use(tmp_path / "no-origin")
    album = root / "music" / "Album (2020)"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"audio-1")
    (album / "02.flac").write_bytes(b"audio-2")
    with monkeypatch.context() as patch:
        patch.setattr(bk, "_write_backup_origin", lambda bp, origin: False)
        assert bk.backup_album_dir(album) is None
        assert (album / "01.flac").read_bytes() == b"audio-1"
        assert (album / "02.flac").exists()
        backups = root / "backups"
        assert not backups.exists() or not any(backups.glob("*"))
        g1 = album / "01.flac"
        before = g1.read_bytes()
        assert bk.backup_gap_fill_files([str(g1)], album) is None
        assert g1.exists() and g1.read_bytes() == before


def test_whole_album_backup_breaks_hardlinks_and_reads_each_file_once(
        tmp_path, monkeypatch):
    """A backup of hardlinked tracks gets its own inode per file, and one
    upgrade read the replaced album about forty times between its backup and
    the backup's removal. A file is read once, under its writer lease, and
    later proofs reuse that digest while its inode is unchanged."""
    import qobuz_librarian.library.backup as bk

    music = tmp_path / "music"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")

    linked = music / "Artist" / "Linked"
    linked.mkdir(parents=True)
    first = linked / "01.flac"
    second = linked / "02.flac"
    first.write_bytes(b"shared inode")
    os.link(first, second)
    result = bk.backup_album_dir(linked)
    assert result is not None and result.complete
    backed_first = result.path / "01.flac"
    backed_second = result.path / "02.flac"
    assert backed_first.read_bytes() == backed_second.read_bytes() == b"shared inode"
    assert backed_first.stat().st_ino != backed_second.stat().st_ino
    assert backed_first.stat().st_nlink == backed_second.stat().st_nlink == 1

    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    tracks = ("01.flac", "02.flac")
    for name in tracks:
        (album / name).write_bytes(name.encode() * 1000)
    audio = {(album / name).stat().st_ino for name in tracks}
    reads = []
    real_read = bk._read_file_digest_fd

    def counted(descriptor):
        reads.append(os.fstat(descriptor).st_ino)
        return real_read(descriptor)

    def track_reads():
        return sorted(inode for inode in reads if inode in audio)

    monkeypatch.setattr(bk, "_read_file_digest_fd", counted)
    monkeypatch.setattr(bk, "_SETTLED_NS", 0)

    result = bk.backup_album_dir(album)
    assert result is not None and result.complete
    assert track_reads() == sorted(audio)
    album.mkdir()
    for name in tracks:
        (album / name).write_bytes(b"new " + name.encode() * 2000)
    audio |= {(album / name).stat().st_ino for name in tracks}
    receipt = bk.capture_album_source_receipt(album)
    assert bk.dispose_backup(
        result, replacement_path=album, expected_replacement_receipt=receipt,
        replacement_validator=lambda _replacement, _backup: True)
    assert not result.path.exists()
    # The new tracks once for their receipt; disposal reads neither side.
    assert track_reads() == sorted(audio)


def test_gap_fill_restore_puts_back_the_backup_over_a_partial_or_corrupt_track(
        tmp_path, monkeypatch):
    """A partial track is overwritten while a larger good one is kept, a larger
    but corrupt one is not kept, and a download cleared while its backup stays
    hands the backup over so Settings' Restore, which names no owner, can
    still put the tracks back."""
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path)
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path)

    owner = {"operation_id": "a" * 64, "item_id": "b" * 64}
    d = tmp_path / "d"
    (d / "bk").mkdir(parents=True)
    (d / "album").mkdir()
    (d / "bk" / "01.flac").write_bytes(b"original")
    backup = _seal_test_backup(bk, d / "bk", d / "album", owner=owner)
    assert bk.restore_gap_fill_backup(backup, d / "album") == 0
    assert bk.pin_unverified_upgrade_backup(backup, "kept", expected_owner=owner)
    assert bk.release_backup_owner(backup, expected_owner=owner)
    released = bk.load_backup_result(d / "bk")
    assert bk.restore_gap_fill_backup(
        released, d / "album", keep_larger_dst=False) == 1
    assert (d / "album" / "01.flac").read_bytes() == b"original"

    a = tmp_path / "a"
    (a / "bk").mkdir(parents=True)
    (a / "album").mkdir()
    (a / "bk" / "01.flac").write_bytes(b"FULL-ORIGINAL-CONTENT-XXXXXX")
    (a / "album" / "01.flac").write_bytes(b"partial")
    backup = _seal_test_backup(bk, a / "bk", a / "album")
    assert bk.restore_gap_fill_backup(backup, a / "album") == 1
    assert (a / "album" / "01.flac").read_bytes() == b"FULL-ORIGINAL-CONTENT-XXXXXX"

    _need_audio_tools()
    b = tmp_path / "b"
    (b / "bk").mkdir(parents=True)
    (b / "album").mkdir()
    (b / "bk" / "01.flac").write_bytes(b"trunc")
    _real_flac(b / "album" / "01.flac")
    good = (b / "album" / "01.flac").read_bytes()
    backup = _seal_test_backup(bk, b / "bk", b / "album")
    assert bk.restore_gap_fill_backup(backup, b / "album") == 1
    assert (b / "album" / "01.flac").read_bytes() == good
    assert not (b / "bk").exists()

    c = tmp_path / "c"
    (c / "bk").mkdir(parents=True)
    (c / "album").mkdir()
    _real_flac(c / "bk" / "01.flac", seconds=2)
    good = (c / "bk" / "01.flac").read_bytes()
    (c / "album" / "01.flac").write_bytes(b"\x00" * (len(good) + 4096))
    backup = _seal_test_backup(bk, c / "bk", c / "album")
    assert bk.restore_gap_fill_backup(backup, c / "album") == 1
    assert (c / "album" / "01.flac").read_bytes() == good


def test_age_sweep_removes_only_backups_it_can_prove_redundant(tmp_path, monkeypatch):
    """Dated backups past retention go and a hand-named one stays, with a
    throttle between unforced sweeps; a backup with no proof is listed and
    kept; a pinned unverified upgrade backup is never swept."""
    import qobuz_librarian.library.backup as bk

    root = tmp_path / "dated"
    backup_root = root / "backups"
    backup_root.mkdir(parents=True)
    music = root / "music"
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", backup_root)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", root)
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", music)

    def _make_completed(name):
        bp = backup_root / name
        bp.mkdir()
        (bp / "01.flac").write_bytes(b"a" * 100)
        origin = music / name.split("_", 2)[-1]
        origin.mkdir(parents=True, exist_ok=True)
        (origin / "01.flac").write_bytes(b"a" * 9000)
        bk._write_backup_origin(bp, origin)
        descriptor = bk._open_backup_directory(bp)
        try:
            assert bk._seal_backup_result(
                bp, descriptor, origin, kind="upgrade", complete=True,
                requested=1, backed_up=1,
            ).complete
        finally:
            os.close(descriptor)
        return bp

    with monkeypatch.context() as patch:
        patch.setattr(bk, "flac_audio_ok", lambda _path: True)
        patch.setattr(bk, "_audio_duration_seconds", lambda _path: 60.0)
        old = _make_completed("20200101_120000_old_album")
        legacy = backup_root / "my_hand_restored_album"
        legacy.mkdir()
        os.utime(legacy, (0, 0))

        assert cleanup_old_upgrade_backups(retention_days=1) == 1
        assert not old.exists() and legacy.exists()

        old = _make_completed("20200101_120000_old_album")
        assert cleanup_old_upgrade_backups(retention_days=1) == 0
        assert old.exists()
        assert cleanup_old_upgrade_backups(retention_days=1, force=True) == 1

    root = tmp_path / "naked"
    backup_root = root / "backups"
    backup_root.mkdir(parents=True)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backup_root)
    monkeypatch.setattr(bk.cfg, "DATA_DIR", root)
    bp = backup_root / "20200101_000000_naked"
    bp.mkdir()
    (bp / "01.flac").write_bytes(b"x" * 5000)
    assert not bk._backup_safe_to_reap(bp)
    removed = bk.cleanup_old_upgrade_backups(retention_days=1, force=True)
    assert bp.exists() and (bp / "01.flac").exists()
    assert removed == 0
    assert any(path == bp and not result.removable
               for path, _origin, result in bk.list_retained_backups())

    _need_audio_tools()
    root = tmp_path / "pinned"
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", root)
    monkeypatch.setattr(bk.cfg, "DATA_DIR", root / "data")
    (root / "data").mkdir(parents=True)

    album = root / "music" / "Album (2020)"
    album.mkdir(parents=True)
    _real_flac(album / "01 - A.flac")
    shutil.copyfile(album / "01 - A.flac", album / "02 - B.flac")
    bp = bk.backup_album_dir(album)
    assert bp is not None

    album.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bp / "01 - A.flac", album / "01 - A.flac")
    shutil.copyfile(bp / "02 - B.flac", album / "02 - B.flac")
    assert bk._backup_safe_to_reap(bp)
    bk.pin_unverified_upgrade_backup(bp)
    assert not bk._backup_safe_to_reap(bp)

    aged = bp.with_name("20200101_000000_aged")
    bp.rename(aged)
    assert bk.cleanup_old_upgrade_backups(force=True) == 0
    assert (aged / "01 - A.flac").exists()


def test_cross_fs_restore_verifies_the_music_and_ignores_backup_sidecars(
        tmp_path, monkeypatch):
    """Every upgrade backup carries its origin and receipt sidecars, and a
    failed upgrade adds pin markers. The staged cross-filesystem restore
    copies only the music, so verifying the copy must not count the backup's
    own root metadata against it, and a staged copy that does not match
    keeps the backup."""
    import qobuz_librarian.library.backup as bkmod

    root = tmp_path / "sidecars"
    root.mkdir()
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", root)
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", root)
    backup = root / "backup"
    backup.mkdir()
    (backup / "track1.flac").write_bytes(b"a" * 50_000)
    (backup / "cover.jpg").write_bytes(b"art")
    original = root / "Album"
    backup_result = _seal_test_backup(bkmod, backup, original, kind="upgrade")
    assert bkmod.pin_unverified_upgrade_backup(backup_result)
    monkeypatch.setattr(bkmod, "_same_filesystem", lambda *_: False)

    assert restore_upgrade_backup(backup_result, original) is True
    assert (original / "track1.flac").read_bytes() == b"a" * 50_000
    assert (original / "cover.jpg").read_bytes() == b"art"
    assert not (original / bkmod._RECEIPT_SIDECAR).exists()
    assert not backup.exists()

    root = tmp_path / "corrupt"
    root.mkdir()
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", root)
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", root)
    backup = root / "backup"
    backup.mkdir()
    (backup / "track1.flac").write_bytes(b"a" * 50_000)
    (backup / "track2.flac").write_bytes(b"b" * 50_000)
    original = root / "Album"
    backup_result = _seal_test_backup(
        bkmod, backup, original, kind="upgrade")
    real_copy_tree = bkmod._copy_tree_manifest_at

    def corrupt_staged_copy(source_fd, destination_fd, manifest, held):
        copied = real_copy_tree(source_fd, destination_fd, manifest, held)
        if copied:
            staged = bkmod._held_directory_path(destination_fd) / "track1.flac"
            staged.with_name("corrupt").write_bytes(b"corrupt")
            os.replace(staged.with_name("corrupt"), staged)
        return copied

    monkeypatch.setattr(
        bkmod, "_copy_tree_manifest_at", corrupt_staged_copy)

    assert restore_upgrade_backup(backup_result, original) is False
    assert backup.exists()
    assert (backup / "track1.flac").exists() and (backup / "track2.flac").exists()
    assert not original.exists()
    assert not list(root.glob(".ql-restore-stage-*"))


def test_backup_receipt_survives_metadata_drift_and_a_copied_volume(
        tmp_path, monkeypatch):
    """A chown or chmod sweep bumps every file's ctime, and restoring a NAS
    snapshot, rsyncing the data volume or moving the disk gives every file a
    new inode, all without touching content. The receipt bound those, so the
    backup became unloadable and Restore and Remove refused it forever. A
    real content change still refuses."""
    import qobuz_librarian.library.backup as bkmod

    root = tmp_path / "ctime"
    root.mkdir()
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", root)
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", root)
    backup = root / "backup"
    backup.mkdir()
    (backup / "track1.flac").write_bytes(b"a" * 50_000)
    (backup / "cover.jpg").write_bytes(b"art")
    original = root / "Album"
    backup_result = _seal_test_backup(bkmod, backup, original, kind="upgrade")

    owner = {"operation_id": "a" * 64, "item_id": "b" * 64}
    owned = root / "backup2"
    owned.mkdir()
    (owned / "track1.flac").write_bytes(b"c" * 50_000)
    owned_fd = bkmod._open_backup_directory(owned)
    try:
        assert bkmod._write_backup_origin_durable(owned_fd, root / "Album2")
        owned_result = bkmod._seal_backup_result(
            owned, owned_fd, root / "Album2",
            kind="gap-fill", complete=True, requested=1, backed_up=1,
            owner=owner)
    finally:
        os.close(owned_fd)
    assert owned_result.receipt is not None
    record = bkmod.library_backup_record(owned_result, expected_owner=owner)
    assert record is not None

    time.sleep(0.05)
    for entry in (backup, *backup.rglob("*"), owned, *owned.rglob("*")):
        os.chmod(entry, entry.stat().st_mode)
    sealed = backup_result.receipt["tree"]["files"]["track1.flac"]["changed_ns"]
    assert (backup / "track1.flac").stat().st_ctime_ns != sealed

    reloaded = bkmod.load_backup_result(backup_result.path)
    assert reloaded is not None and reloaded.complete
    assert bkmod.load_library_backup_record(
        record, expected_owner=owner) is not None

    assert restore_upgrade_backup(reloaded, original) is True
    assert (original / "track1.flac").read_bytes() == b"a" * 50_000
    assert not backup.exists()

    sealed2 = owned_result.receipt["tree"]["files"]["track1.flac"]
    (owned / "track1.flac").write_bytes(b"d" * 50_000)
    os.utime(owned / "track1.flac",
             ns=(sealed2["mtime_ns"], sealed2["mtime_ns"]))
    assert bkmod.load_backup_result(owned, expected_owner=owner) is None

    root = tmp_path / "copied"
    root.mkdir()
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", root)
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", root)
    backup = root / "backup"
    backup.mkdir()
    (backup / "track1.flac").write_bytes(b"a" * 50_000)
    (backup / "cover.jpg").write_bytes(b"art")
    original = root / "Album"
    _seal_test_backup(bkmod, backup, original, kind="upgrade")

    moved = root / "moved"
    shutil.copytree(backup, moved)
    assert ((moved / "track1.flac").stat().st_ino
            != (backup / "track1.flac").stat().st_ino)

    carried = bkmod.load_backup_result(moved)
    assert carried is not None and carried.complete
    assert restore_upgrade_backup(carried, original) is True
    assert (original / "track1.flac").read_bytes() == b"a" * 50_000

    changed = root / "changed"
    shutil.copytree(backup, changed)
    (changed / "track1.flac").write_bytes(b"b" * 50_000)
    assert bkmod.load_backup_result(changed) is None


def test_saved_job_reopens_only_its_own_unchanged_restored_backup(
        tmp_path, monkeypatch):
    """A backup put back from a saved copy gets new inodes. The saved job
    still reopens it, but not once its content changed or another job's
    backup stands in its place."""
    import qobuz_librarian.library.backup as bk

    backup_root = tmp_path / "backups"
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backup_root)
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path)
    backup = backup_root / "album"
    (backup / "Disc 1").mkdir(parents=True)
    (backup / "Disc 1" / "track.flac").write_bytes(b"original audio")
    owner = {"operation_id": "a" * 64, "item_id": "b" * 64}
    sealed = _seal_test_backup(bk, backup, tmp_path / "Album", owner=owner)
    record = bk.library_backup_record(sealed, expected_owner=owner)
    assert record is not None

    saved_copy = tmp_path / "saved-copy"
    shutil.copytree(backup, saved_copy)
    # Keep the old inodes occupied so the restore cannot reuse them.
    backup.rename(tmp_path / "original-backup")
    shutil.copytree(saved_copy, backup)

    direct = bk.load_backup_result(sealed.path, expected_owner=owner)
    assert direct is not None
    reopened = bk.load_library_backup_record(record, expected_owner=owner)
    assert reopened is not None and reopened.complete
    assert reopened.receipt == direct.receipt
    assert reopened.receipt["owner"] == owner
    assert (reopened.receipt["tree"]["root_identity"]
            != record["receipt"]["tree"]["root_identity"])
    assert (reopened.receipt["receipt_identity"]
            != record["receipt"]["receipt_identity"])
    assert bk.library_backup_record(reopened, expected_owner=owner) is not None
    assert bk.library_backup_record(sealed, expected_owner=owner) is None
    assert bk._validated_backup_result(sealed) is None
    assert bk.load_library_backup_record(record) is None

    track = sealed.path / "Disc 1" / "track.flac"
    before = track.stat()
    track.write_bytes(b"modified audio")
    os.utime(track, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert bk.load_backup_result(sealed.path, expected_owner=owner) is None
    assert bk.load_library_backup_record(record, expected_owner=owner) is None

    other_owner = {"operation_id": "c" * 64, "item_id": owner["item_id"]}
    other_backup = tmp_path / "other-backup"
    (other_backup / "Disc 1").mkdir(parents=True)
    (other_backup / "Disc 1" / "track.flac").write_bytes(b"original audio")
    _seal_test_backup(bk, other_backup, tmp_path / "Album", owner=other_owner)
    shutil.rmtree(sealed.path)
    shutil.copytree(other_backup, sealed.path)

    assert bk.load_backup_result(sealed.path, expected_owner=other_owner) is not None
    assert bk.load_backup_result(sealed.path, expected_owner=owner) is None
    assert bk.load_library_backup_record(record, expected_owner=owner) is None
    assert bk.load_library_backup_record(record, expected_owner=other_owner) is None


def test_retire_verified_repair_backup_needs_superseding_tracks(tmp_path, monkeypatch):
    # After a verified refill the originals' backup may be retired only when
    # every file it holds has, at the same path, a decode-clean track of at
    # least its duration. Size may shrink - the backup holds the damaged
    # copies, and a corrupt original is often larger than its clean refill.
    _need_audio_tools()
    import qobuz_librarian.library.backup as bkmod
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", tmp_path / "music")
    album = tmp_path / "music" / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    _real_flac(album / "01.flac", seconds=2)
    quiet = tmp_path / "quiet.flac"
    import subprocess
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anoisesrc=duration=2:color=white:amplitude=0.05",
         "-ac", "2", "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
         str(quiet)], check=True)

    kept = tmp_path / "backups" / "kept"
    kept.mkdir(parents=True)
    shutil.copyfile(album / "01.flac", kept / "01.flac")
    (album / "01.flac").write_bytes(quiet.read_bytes())
    assert (album / "01.flac").stat().st_size < (kept / "01.flac").stat().st_size
    superseded = _seal_test_backup(bkmod, kept, album, kind="gap-fill")

    orphan = tmp_path / "backups" / "kept2"
    orphan.mkdir(parents=True)
    shutil.copyfile(kept / "01.flac", orphan / "02.flac")
    unmatched = _seal_test_backup(bkmod, orphan, album, kind="gap-fill")

    assert bkmod.retire_verified_repair_backup(unmatched) is False
    assert (orphan / "02.flac").exists()

    assert bkmod.retire_verified_repair_backup(superseded) is True
    assert not kept.exists()
    assert (album / "01.flac").read_bytes() == quiet.read_bytes()


def test_gap_fill_backup_copies_an_unleased_source_and_returns_a_partial_one(
        tmp_path, monkeypatch):
    """A repair source the app doesn't own can't carry a write lease. With the
    scan's sealed receipt pinning its content, the backup takes the copy route
    instead of giving up; without a receipt the refusal stands. A backup cut
    short by a track replaced mid-move still returns its recovery path."""
    import qobuz_librarian.library.backup as bkmod

    root = tmp_path / "unleased"
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", root / "music")
    album = root / "music" / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    source = album / "01 - Damaged.flac"
    source.write_bytes(b"damaged audio bytes")

    fd = os.open(source, os.O_RDONLY)
    try:
        sealed = bkmod._gap_fill_file_receipt(fd)
    finally:
        os.close(fd)
    expected = bkmod._normalise_gap_fill_expected_receipts(
        {"01 - Damaged.flac": sealed})

    real_acquire = bkmod.acquire_inode_write_exclusion
    denied = source.stat().st_ino

    def deny_source(descriptor):
        if os.fstat(descriptor).st_ino == denied:
            return None
        return real_acquire(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(bkmod, "acquire_inode_write_exclusion", deny_source)

        result = bkmod.backup_gap_fill_files(
            [source], album, expected_receipts=expected)
        assert result is not None and result.complete
        assert not source.exists()
        assert (result.path / "01 - Damaged.flac").read_bytes() == (
            b"damaged audio bytes")

        other = album / "02 - Other.flac"
        other.write_bytes(b"other")
        denied = other.stat().st_ino
        assert bkmod.backup_gap_fill_files([other], album) is None
        assert other.read_bytes() == b"other"

    root = tmp_path / "partial"
    music = root / "music"
    backups = root / "backups"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"first original")
    second.write_bytes(b"second original")
    displaced = root / "displaced-second.flac"
    monkeypatch.setattr(bkmod.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", backups)
    rename_noreplace = bkmod._rename_noreplace_at
    replaced = False

    def replace_second(source_fd, source_name, destination_fd,
                       destination_name):
        nonlocal replaced
        if source_name == "02.flac" and not replaced:
            replaced = True
            second.rename(displaced)
            second.write_bytes(b"second replacement")
        return rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(bkmod, "_rename_noreplace_at", replace_second)

    result = bkmod.backup_gap_fill_files([first, second], album)

    assert result is not None
    assert result.complete is False
    assert result.path.exists()
    assert not first.exists()
    assert second.read_bytes() == b"second replacement"
    assert displaced.read_bytes() == b"second original"
    assert any(
        path.read_bytes() == b"first original"
        for path in result.path.rglob("01.flac")
    )


def _render_backup_diagnostics(rows):
    import types

    from qobuz_librarian.web import diagnostics

    request = types.SimpleNamespace(state=types.SimpleNamespace(csrf_token=""))
    return diagnostics._diagnostics_fragment(request, {
        "checks": [], "orphans": rows, "undo": [], "leftovers": [],
    })


def test_diagnostics_backup_rows_are_rechecked_before_deletion(tmp_path, monkeypatch):
    """A backup whose companion file differs from the library's is listed as
    retained and never removed; a removable one is checked again before
    Remove deletes it, and one past the read budget shows as unverified."""
    import qobuz_librarian.library.backup as bk

    _need_audio_tools()

    def stage(name, origin_booklet, kind="gap-fill"):
        root = tmp_path / name / "backups"
        backup = root / "kept"
        backup.mkdir(parents=True)
        origin = tmp_path / name / "music" / "Album"
        origin.mkdir(parents=True)
        monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", root)
        monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", tmp_path / name / "music")
        monkeypatch.setattr(bk, "_only_copy_cache", None)
        _real_flac(backup / "01.flac")
        shutil.copyfile(backup / "01.flac", origin / "01.flac")
        (backup / "booklet.pdf").write_bytes(b"original booklet")
        (origin / "booklet.pdf").write_bytes(origin_booklet)
        return backup, origin, _seal_test_backup(bk, backup, origin, kind=kind)

    backup, origin, candidate = stage("different", b"modified booklet")
    [(path, album, result)] = bk.list_retained_backups()
    assert (path, album) == (backup, origin)
    assert result.status == "retained"
    assert result.reason == "different"
    assert result.file == "booklet.pdf"
    rendered = _render_backup_diagnostics([(path, album, result)])
    assert 'data-backup-status="retained"' in rendered
    assert result.file in rendered
    assert not result.removable
    assert bk._backup_safe_to_reap(backup) is False
    assert bk._dispose_retention_candidate(candidate) is False
    assert bk.discard_redundant_backup(backup) is False
    assert (backup / "booklet.pdf").read_bytes() == b"original booklet"

    backup, origin, _candidate = stage("removable", b"original booklet")
    [(path, album, result)] = bk.list_retained_backups()
    assert (path, album) == (backup, origin)
    assert result.removable
    rendered = _render_backup_diagnostics([(path, album, result)])
    assert 'data-backup-status="removable"' in rendered
    assert bk._backup_safe_to_reap(backup) is True
    (origin / "booklet.pdf").write_bytes(b"modified booklet")
    assert bk.list_retained_backups()[0][2].removable
    assert bk.discard_redundant_backup(backup) is False
    assert backup.exists()

    (origin / "booklet.pdf").write_bytes(b"original booklet")
    monkeypatch.setattr(bk, "_BACKUP_LISTING_READ_BYTES", 0)
    monkeypatch.setattr(bk, "_only_copy_cache", None)
    result = bk.list_retained_backups()[0][2]
    assert result.status == "unverified"
    assert result.reason == "budget"
    rendered = _render_backup_diagnostics([(path, album, result)])
    assert 'data-backup-status="unverified"' in rendered
    assert bk.discard_redundant_backup(backup) is True
    assert not backup.exists()
    assert (origin / "booklet.pdf").read_bytes() == b"original booklet"

    # An upgrade's backup differs from the album by design; Remove is offered
    # and goes once every track has a verified replacement. Another album
    # backup whose tracks differ has no Remove, which would refuse it.
    monkeypatch.setattr(bk, "_BACKUP_LISTING_READ_BYTES", 1 << 30)
    backup, origin, _candidate = stage("refilled", b"original booklet")
    _real_flac(origin / "01.flac", seconds=3)
    [(path, album, result)] = bk.list_retained_backups()
    assert (result.status, result.file) == ("retained", "01.flac")
    assert 'hx-post="/backups/discard"' not in _render_backup_diagnostics([(path, album, result)])
    assert bk.discard_replaced_upgrade_backup(backup) is False
    backup, origin, _candidate = stage("upgraded", b"original booklet", kind="upgrade")
    _real_flac(origin / "01.flac", seconds=3)
    [(path, album, result)] = bk.list_retained_backups()
    assert (result.status, result.file) == ("retained", "01.flac")
    assert 'hx-post="/backups/discard"' in _render_backup_diagnostics([(path, album, result)])
    assert bk.discard_redundant_backup(backup) is False
    assert bk.discard_replaced_upgrade_backup(backup) is True
    assert not backup.exists()


def test_gap_fill_restore_keeps_the_backup_whenever_it_cannot_finish(
        tmp_path, monkeypatch):
    """A backup walk that comes back partial, a read-only album folder, an
    interrupt mid-copy and a directory flush that fails each leave the backup
    holding the only copy; a partial track is still replaced from it."""
    import qobuz_librarian.library.backup as bkmod

    backup = tmp_path / "walk" / "backup"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"the-only-copy")
    walk_album = tmp_path / "walk" / "Album"
    backup_result = _seal_test_backup(bkmod, backup, walk_album)
    with monkeypatch.context() as patch:
        patch.setattr(bkmod, "_exact_tree_snapshot", lambda *_a, **_k: None)
        assert bkmod.restore_gap_fill_backup(
            backup_result, walk_album, keep_larger_dst=False) == 0
    assert (backup / "01.flac").read_bytes() == b"the-only-copy"

    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)

    if os.geteuid() != 0:
        album = tmp_path / "ro_album"
        album.mkdir()
        (album / "t.flac").write_bytes(b"original")
        bp = backup_gap_fill_files([str(album / "t.flac")], album)
        os.chmod(album, 0o500)
        try:
            assert restore_gap_fill_backup(bp, album) == 0
        finally:
            os.chmod(album, 0o700)
        assert bp.exists()

    album2 = tmp_path / "partial_album"
    album2.mkdir()
    track = album2 / "t.flac"
    track.write_bytes(b"the-good-original")
    bp = backup_gap_fill_files([str(track)], album2)
    track.write_bytes(b"partial-junk")
    assert restore_gap_fill_backup(bp, album2) == 1
    assert track.read_bytes() == b"the-good-original"
    assert not bp.exists()

    album3 = tmp_path / "ki_album"
    album3.mkdir()
    tr = album3 / "t.flac"
    tr.write_bytes(b"precious-audio")
    bp = backup_gap_fill_files([str(tr)], album3)
    with monkeypatch.context() as patch:
        patch.setattr(
            bkmod,
            "_copy_file_noreplace_at",
            lambda *args: (_ for _ in ()).throw(KeyboardInterrupt),
        )
        with pytest.raises(KeyboardInterrupt):
            restore_gap_fill_backup(bp, album3)
    assert bp.exists() and len(list(bp.rglob("*.flac"))) == 1
    assert not list(album3.rglob("*.restore_tmp"))

    monkeypatch.setattr(bkmod.cfg, "UPGRADE_BACKUP_DIR", tmp_path)
    album = tmp_path / "Album"
    album.mkdir()
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "01 - Track.flac").write_bytes(b"the-only-copy")
    backup_result = _seal_test_backup(bkmod, bk, album)
    monkeypatch.setattr(bkmod, "_fsync",
                        lambda p: not Path(p).is_dir())
    n = bkmod.restore_gap_fill_backup(
        backup_result, album, keep_larger_dst=False)
    assert n == 0
    assert bk.exists()
    assert (bk / "01 - Track.flac").read_bytes() == b"the-only-copy"
    assert (bk / bkmod._PARTIAL_RESTORE_SENTINEL).is_file()


def test_backup_disposal_needs_an_exact_complete_proof(tmp_path, monkeypatch):
    """A replacement tree standing at the backup's path is not disposed of;
    Remove deletes only after exact digests match on both sides, pin or no
    pin, since a same-size origin file could be a different rendition; and
    a backup listing that fails partway never proves a backup redundant."""
    import qobuz_librarian.library.backup as bk

    root = tmp_path / "replaced"
    music = root / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "01.flac"
    track.write_bytes(b"original")
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    result = bk.backup_gap_fill_files([track], album)
    assert result is not None and result.complete
    held = root / "held-backup"
    result.path.rename(held)
    shutil.copytree(held, result.path)
    assert bk.dispose_backup(result) is False
    assert (result.path / "01.flac").read_bytes() == b"original"
    assert (held / "01.flac").read_bytes() == b"original"

    root = tmp_path / "pinned"
    root.mkdir()
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", root)
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", root / "music")
    origin = root / "music" / "Artist" / "Album (1969)"
    origin.mkdir(parents=True)
    (origin / "01.flac").write_bytes(b"x" * 40_000)
    (origin / "cover.jpg").write_bytes(b"art")
    backup = root / "kept"
    backup.mkdir()
    (backup / "01.flac").write_bytes(b"y" * 40_000)
    (backup / "cover.jpg").write_bytes(b"art")
    result = _seal_test_backup(bk, backup, origin, kind="upgrade")
    assert bk.pin_unverified_upgrade_backup(result)
    assert bk.backup_keep_markers_present(backup)
    assert bk.discard_redundant_backup(backup) is False
    assert (backup / "01.flac").read_bytes() == b"y" * 40_000
    (origin / "01.flac").write_bytes(b"y" * 40_000)
    assert bk.discard_redundant_backup(backup) is True
    assert not backup.exists()
    assert (origin / "01.flac").read_bytes() == b"y" * 40_000
    assert (origin / "cover.jpg").read_bytes() == b"art"

    root = tmp_path / "listing"
    monkeypatch.setattr(bk.cfg, "MUSIC_ROOT", root / "music")
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    real_stat = os.stat
    for suffix in ("", "/02.flac"):
        album = bk.cfg.MUSIC_ROOT / "Artist" / ("Root" if not suffix else "File")
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"kept")
        (album / "02.flac").write_bytes(b"only copy")
        backup = bk.backup_album_dir(album)
        assert backup is not None and backup.complete
        album.mkdir()
        (album / "01.flac").write_bytes(b"kept")
        faults = []

        def unreadable(path, *args, **kwargs):
            if not isinstance(path, int):
                value = os.fspath(path)
                if ("/qobuz-librarian-disposal-proof-" in value
                        and value.endswith("/backup" + suffix)):
                    faults.append(path)
                    raise OSError(5, "I/O error", value)
            return real_stat(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "stat", unreadable)
            if suffix:
                removed = bk.discard_redundant_backup(backup.path)
            else:
                removed = bk._dispose_retention_candidate(
                    backup, allow_smaller_audio=True)
        assert faults
        assert removed is False
        assert (backup.path / "02.flac").read_bytes() == b"only copy"


def test_match_sibling_track_needs_duration_and_agreeing_identifiers():
    """A same-titled track is no duplicate without a duration that confirms
    it, nor when recording ids or ISRCs disagree, are one-sided or are
    placeholders."""
    t = lambda **kw: {"isrc": "", "mb_trackid": "", "title": "Intro",
                      "discnumber": 1, "tracknumber": 0, "length": 0.0,
                      "size": 0, **kw}
    assert match_sibling_track(t(length=30.0), [t(length=30.4)]) is None
    assert match_sibling_track(
        t(tracknumber=1, length=30.0),
        [t(tracknumber=1, length=30.4)],
    ) is not None
    assert match_sibling_track(t(length=30.0), [t(length=120.0)]) is None
    assert match_sibling_track(t(length=0.0, size=5_000_000),
                               [t(length=0.0, size=5_050_000)]) is None
    tag = lambda n, ln: {"isrc": "", "mb_trackid": "", "title": "Song",
                         "discnumber": 1, "tracknumber": n, "length": ln, "size": 0}
    assert match_sibling_track(tag(3, 200.0), [tag(3, 260.0)]) is None
    assert match_sibling_track(tag(3, 200.0), [tag(3, 200.5)]) is not None
    assert match_sibling_track(tag(3, 200.0), [tag(4, 200.0)]) is None
    assert match_sibling_track(
        tag(3, 200.0),
        [tag(3, 200.0), {**tag(3, 200.0), "title": "Different"}],
    ) is None
    assert match_sibling_track(
        {**tag(3, 200.0), "title": "東京 (Remaster)"},
        [{**tag(3, 200.0), "title": "大阪 (Remaster)"}],
    ) is None
    assert match_sibling_track(
        {**tag(3, 200.0), "title": "東京 [Remaster]"},
        [{**tag(3, 200.0), "title": "大阪 [Remaster]"}],
    ) is None
    assert match_sibling_track(
        {**tag(3, 200.0), "tracknumber": True},
        [tag(1, 200.0)],
    ) is None
    assert match_sibling_track(
        {**tag(3, 200.0), "length": True},
        [{**tag(3, 200.0), "length": 1.0}],
    ) is None

    recording_one = "12345678-1234-1234-1234-1234567890ab"
    recording_one_compact = "123456781234123412341234567890ab"
    recording_two = "abcdefab-cdef-abcd-efab-cdefabcdefab"
    sibling = {
        "isrc": "",
        "mb_trackid": recording_one,
        "title": "Same title",
        "discnumber": 1,
        "tracknumber": 2,
        "length": 180.0,
    }
    primary = [{
        **sibling,
        "mb_trackid": recording_two,
    }]

    assert match_sibling_track(
        sibling, [{**sibling, "mb_trackid": recording_one_compact}]
    ) is not None
    assert match_sibling_track(sibling, primary) is None
    assert match_sibling_track(
        {**sibling, "isrc": "USAAA1234567"},
        [{**primary[0], "isrc": "USAAA1234567"}],
    ) is None
    assert match_sibling_track(
        {**sibling, "mb_trackid": recording_one},
        [{**sibling, "mb_trackid": ""}],
    ) is None
    assert match_sibling_track(
        {**sibling, "mb_trackid": ""},
        [{**sibling, "mb_trackid": recording_one}],
    ) is None
    assert match_sibling_track(
        {**sibling, "isrc": "USAAA1234567", "mb_trackid": ""},
        [{**sibling, "isrc": "", "mb_trackid": ""}],
    ) is None
    assert match_sibling_track(
        {**sibling, "isrc": "", "mb_trackid": ""},
        [{**sibling, "isrc": "USAAA1234567", "mb_trackid": ""}],
    ) is None
    assert match_sibling_track(
        {**sibling, "isrc": "N/A", "mb_trackid": ""},
        [{**sibling, "isrc": "N/A", "mb_trackid": ""}],
    ) is None
    assert match_sibling_track(
        {**sibling, "isrc": "", "mb_trackid": "N/A"},
        [{**sibling, "isrc": "", "mb_trackid": "N/A"}],
    ) is None
    for field, placeholder in (
        ("isrc", "000000000000"),
        ("mb_trackid", "00000000-0000-0000-0000-000000000000"),
    ):
        assert match_sibling_track(
            {**sibling, "isrc": "", "mb_trackid": "", field: placeholder},
            [{**sibling, "isrc": "", "mb_trackid": "", field: placeholder,
              "title": "Different"}],
        ) is None


def _bind_consolidation_summary(summary, tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.modes import consolidate as consolidation

    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "beets.db")
    primary = tmp_path / "Primary"
    primary.mkdir(exist_ok=True)
    primary_seal = consolidation._seal_album(primary)
    sibling_seal = consolidation._seal_album(summary["dir"])
    consolidation._bind_summary(summary, primary_seal, sibling_seal)
    return primary_seal, sibling_seal, summary["_binding"]


def test_consolidation_never_groups_distinct_years_or_acts_in_a_dry_run(
        tmp_path, monkeypatch):
    """Two folders that BOTH carry a year and don't share one are distinct
    works with a similar name, and consolidation deletes "duplicate" tracks -
    grouping them would feed a real recording to the deleter. A one-sided year
    still groups: that's the same release re-tagged. A dry run looks nothing
    up and touches no file."""
    from argparse import Namespace

    import qobuz_librarian.config as cfg
    import qobuz_librarian.library.catalog as catmod
    from qobuz_librarian.modes import consolidate as c

    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "beets.db")
    artist = tmp_path / "Queen"
    primary = artist / "Live at Wembley 1990"
    other = artist / "Live at Wembley 1992"
    same = artist / "Live at Wembley"
    for d in (primary, other, same):
        d.mkdir(parents=True)

    sealed = c._seal_album(primary)
    found = []
    try:
        found = c._sealed_sibling_albums({"title": "Live at Wembley 1990"}, sealed)
        names = {sibling.path.name for sibling, _score in found}
    finally:
        for sibling, _score in found:
            sibling.close()
        sealed.close()
    assert "Live at Wembley 1992" not in names
    assert "Live at Wembley" in names

    def _boom(*a, **k):
        raise AssertionError("dry-run consolidation must not look up or touch files")
    monkeypatch.setattr(catmod, "find_album_dir_filesystem", _boom)

    album = {"id": "x", "title": "Revolver", "artist": {"name": "The Beatles"}}
    assert c.consolidate_albums(album, Namespace(dry_run=True, consolidate=True,
                                                 yes=False)) == 0


def test_execute_consolidation_backs_up_overlap_and_refuses_a_replacement(
        tmp_path, monkeypatch):
    """Overlapping tracks move to a recoverable backup with their Beets rows
    retired and the cover left alone; a track swapped for a same-name file
    after review is not touched."""
    import sqlite3

    import qobuz_librarian.config as cfg

    root = tmp_path / "moved"
    root.mkdir()
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    sib = root / "Album (Deluxe)"
    sib.mkdir()
    f1 = sib / "track.flac"
    f1.write_bytes(b"audio-1")
    f2 = sib / "other.flac"
    f2.write_bytes(b"audio-2")
    cover = sib / "cover.jpg"
    cover.write_bytes(b"album-art")
    database = root / "beets.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE albums ("
            "id INTEGER PRIMARY KEY, artpath BLOB)"
        )
        connection.execute(
            "CREATE TABLE items ("
            "id INTEGER PRIMARY KEY, path BLOB NOT NULL, album_id INTEGER)"
        )
        connection.execute(
            "CREATE TABLE album_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE item_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO albums (id, artpath) VALUES (10, ?)",
            (os.fsencode(cover),),
        )
        connection.executemany(
            "INSERT INTO items (id, path, album_id) VALUES (?, ?, 10)",
            ((1, os.fsencode(f1)), (2, os.fsencode(f2))),
        )
        connection.execute(
            "INSERT INTO album_attributes (id, entity_id, key, value) "
            "VALUES (1, 10, 'source', 'reviewed')"
        )
        connection.execute(
            "INSERT INTO item_attributes (id, entity_id, key, value) "
            "VALUES (1, 1, 'source', 'reviewed')"
        )
    connection.close()
    summary = {"dir": str(sib),
               "overlap": [({"path": str(f1)}, {}), ({"path": str(f2)}, {})],
               "unique": []}

    primary_seal, sibling_seal, binding = _bind_consolidation_summary(
        summary, root, monkeypatch)
    try:
        removed, n_fail = execute_consolidation(summary)
    finally:
        binding.close()
        sibling_seal.close()
        primary_seal.close()

    assert n_fail == 0
    assert sorted(p.name for p in removed) == ["other.flac", "track.flac"]
    assert not f1.exists() and not f2.exists()
    recovered = {p.name for p in (root / "backups").rglob("*.flac")}
    assert recovered == {"track.flac", "other.flac"}
    assert cover.read_bytes() == b"album-art"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM items").fetchall() == []
        assert connection.execute(
            "SELECT id FROM item_attributes"
        ).fetchall() == []
        assert connection.execute("SELECT id FROM albums").fetchall() == []
        assert connection.execute(
            "SELECT id FROM album_attributes"
        ).fetchall() == []
    connection.close()

    root = tmp_path / "replaced"
    root.mkdir()
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    sibling = root / "Album Deluxe"
    sibling.mkdir()
    reviewed = sibling / "01.flac"
    reviewed.write_bytes(b"reviewed original")
    summary = {
        "dir": sibling,
        "overlap": [({"path": str(reviewed)}, {})],
        "unique": [],
    }
    primary_seal, sibling_seal, binding = _bind_consolidation_summary(
        summary, root, monkeypatch)
    preserved = root / "reviewed.flac"
    reviewed.rename(preserved)
    reviewed.write_bytes(b"same-name replacement")
    try:
        removed, n_failed = execute_consolidation(summary)
    finally:
        binding.close()
        sibling_seal.close()
        primary_seal.close()

    assert removed == [] and n_failed == 1
    assert preserved.read_bytes() == b"reviewed original"
    assert reviewed.read_bytes() == b"same-name replacement"
    assert not (root / "backups").exists()


@pytest.mark.parametrize("failure_point", ("before-publication", "after-publication"))
def test_consolidation_publication_failure_keeps_recovery_and_reports_failure(
        tmp_path, monkeypatch, failure_point):
    import sqlite3

    import qobuz_librarian.config as cfg
    from qobuz_librarian.modes import consolidate as consolidation

    root = tmp_path / failure_point
    sibling = root / "Album Deluxe"
    sibling.mkdir(parents=True)
    reviewed = sibling / "01.flac"
    reviewed.write_bytes(b"reviewed audio")
    database = root / "beets.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, artpath BLOB)"
        )
        connection.execute(
            "CREATE TABLE items ("
            "id INTEGER PRIMARY KEY, path BLOB NOT NULL, album_id INTEGER)"
        )
        connection.execute(
            "CREATE TABLE album_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE item_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO albums VALUES (10, NULL)")
        connection.execute(
            "INSERT INTO items VALUES (1, ?, 10)",
            (os.fsencode(reviewed),),
        )
    connection.close()

    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", root / "backups")
    summary = {
        "dir": sibling,
        "overlap": [({"path": str(reviewed)}, {})],
        "unique": [],
    }
    primary_seal, sibling_seal, binding = _bind_consolidation_summary(
        summary, root, monkeypatch)
    real_publish = consolidation.AtomicSQLiteWrite.commit_and_publish

    def fail_publication(transaction, *args, **kwargs):
        if failure_point == "after-publication":
            real_publish(transaction, *args, **kwargs)
        raise OSError(f"injected {failure_point} failure")

    monkeypatch.setattr(
        consolidation.AtomicSQLiteWrite,
        "commit_and_publish",
        fail_publication,
    )
    try:
        removed, n_failed = execute_consolidation(summary)
    finally:
        binding.close()
        sibling_seal.close()
        primary_seal.close()

    assert removed == [] and n_failed == 1
    assert not reviewed.exists()
    assert [path.read_bytes() for path in (root / "backups").rglob("*.flac")] == [
        b"reviewed audio"
    ]
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT id FROM items").fetchall()
    assert rows == ([] if failure_point == "after-publication" else [(1,)])


def test_consolidation_backup_expires_only_while_the_primary_keeps_its_tracks(
        tmp_path, monkeypatch):
    # The duplicate folder a consolidation empties never matches its backup
    # again, so the age sweep kept every consolidation backup forever.
    _need_audio_tools()
    import qobuz_librarian.config as cfg
    import qobuz_librarian.library.backup as bk

    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    primary = tmp_path / "Primary"
    sibling = tmp_path / "Album Deluxe"
    overlap = []
    for name in ("01 - One.flac", "02 - Two.flac"):
        _real_flac(primary / name)
        _real_flac(sibling / f"A{name}")
        overlap.append(({"path": str(sibling / f"A{name}")},
                        {"path": str(primary / name)}))
    summary = {"dir": sibling, "overlap": overlap, "unique": []}
    primary_seal, sibling_seal, binding = _bind_consolidation_summary(
        summary, tmp_path, monkeypatch)
    try:
        removed, n_failed = execute_consolidation(summary)
    finally:
        binding.close()
        sibling_seal.close()
        primary_seal.close()
    assert len(removed) == 2 and n_failed == 0

    [backup] = (tmp_path / "backups").iterdir()
    assert bk.cleanup_old_upgrade_backups(retention_days=1, force=True) == 0
    aged = backup.with_name("20200101_000000_" + backup.name.split("_", 3)[-1])
    backup.rename(aged)
    (primary / "02 - Two.flac").rename(tmp_path / "away.flac")
    assert bk.cleanup_old_upgrade_backups(retention_days=1, force=True) == 0
    assert (aged / "A02 - Two.flac").exists()
    (tmp_path / "away.flac").rename(primary / "02 - Two.flac")
    assert bk.cleanup_old_upgrade_backups(retention_days=1, force=True) == 1
    assert not aged.exists()


def test_downsample_stash_marks_the_undo_window_that_release_requires(
        tmp_path, monkeypatch):
    """The stash copies the originals and marks the undo window. Deleting kept
    originals early skips the age half of retention, so it must not become a
    way past the proof an ordinary backup has to pass first."""
    from qobuz_librarian.library import backup as bk
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
    album = tmp_path / "music" / "A" / "Album (2020)"
    (album / "CD1").mkdir(parents=True)
    f1 = album / "01.flac"
    f1.write_bytes(b"HIRES-1")
    f2 = album / "CD1" / "02.flac"
    f2.write_bytes(b"HIRES-2")
    bp, copied = bk.stash_downsample_originals([f1, f2], album)
    assert copied == {f1, f2}
    assert (bp / "01.flac").read_bytes() == b"HIRES-1"
    assert (bp / "CD1" / "02.flac").read_bytes() == b"HIRES-2"
    assert (bp / bk._REAP_AFTER_RETENTION_SENTINEL).is_file()
    assert bk._read_backup_origin(bp) == album
    assert f1.read_bytes() == b"HIRES-1"

    album = tmp_path / "music" / "A" / "Album (2021)"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"HIRES-1")
    carried = backup_album_dir(album)
    assert carried is not None
    assert not (carried.path / bk._REAP_AFTER_RETENTION_SENTINEL).exists()
    assert bk.release_undo_copy(carried.path) is False
    assert (carried.path / "01.flac").read_bytes() == b"HIRES-1"


