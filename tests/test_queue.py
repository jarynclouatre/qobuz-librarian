"""Tests for queue/builder.py and queue/persistence.py."""
import errno
from argparse import Namespace
from datetime import datetime
from pathlib import Path

import pytest

from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.persistence import (
    QueueLoadStatus,
    _deserialize_queue_item,
    _serialize_queue_item,
    clear_pending_queue,
    load_pending_queue,
    offer_resume_pending_queue,
    save_pending_queue,
)


def _qitem(title="Test", **overrides):
    defaults = dict(
        album={"id": "1", "title": title},
        album_dir=Path(f"/music/{title.lower()}"),
        label=title, missing=[], present=[],
        upgrade_only=False, auto_upgrade=False,
    )
    defaults.update(overrides)
    return _build_queue_item(**defaults)


@pytest.fixture(autouse=True)
def _legacy_executor_runtime(monkeypatch):
    from qobuz_librarian import run_lock
    from qobuz_librarian.queue import executor
    from qobuz_librarian.queue.startup_recovery import (
        StartupRecoveryResult,
        StartupRecoveryStatus,
    )

    lease = run_lock.current_lease()
    acquired_here = lease is None
    if acquired_here:
        lease = run_lock.acquire()
    assert lease is not None
    monkeypatch.setattr(executor, "plan_durable_new_album", lambda *_a, **_k: None)
    monkeypatch.setattr(
        executor,
        "queue_item_may_create_library_backup",
        lambda _item: False,
    )
    monkeypatch.setattr(
        executor,
        "recover_startup_state",
        lambda **_kw: StartupRecoveryResult(StartupRecoveryStatus.CLEAR),
    )
    try:
        yield
    finally:
        if acquired_here:
            lease.close()


def test_queue_item_round_trip_preserves_exact_local_premise(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    source = album_dir / "01.flac"
    source.write_bytes(b"reviewed bytes")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)

    item = _qitem(album_dir=album_dir, present=[{"id": "track-1"}])
    item["n_ok"], item["imported"] = 5, True
    restored = _deserialize_queue_item(_serialize_queue_item(item))

    assert restored["_source_premise"] == item["_source_premise"]
    assert restored["_source_premise"]["path"] == str(album_dir)
    assert restored["_gap_fill_receipts"] == item["_gap_fill_receipts"]
    assert restored["_gap_fill_receipts"]["01.flac"]["sha256"]
    # Runtime accounting is per-run state and resets on restore.
    assert restored["n_ok"] == 0 and restored["imported"] is False
    # An older saved item without that proof is refused.
    planned = _serialize_queue_item(item)
    planned.pop("source_premise")
    with pytest.raises(ValueError):
        _deserialize_queue_item(planned)


def test_executor_refuses_changed_queued_album_before_download(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    source = album_dir / "01.flac"
    source.write_bytes(b"reviewed bytes")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    item = _qitem(
        album_dir=album_dir,
        missing=[{"id": "track-2"}],
        present=[{"id": "track-1"}],
    )
    source.write_bytes(b"replacement bytes")

    file_preflight = []
    downloads = []
    monkeypatch.setattr(
        executor,
        "staging_preflight",
        lambda _args: file_preflight.append(True),
    )
    monkeypatch.setattr(
        executor,
        "_download_for_queue_item",
        lambda _item: downloads.append(True),
    )
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)

    queue = [item]
    results, drained = executor._execute_download_queue(
        queue,
        Namespace(
            dry_run=False,
            no_import=True,
            no_downsample=True,
            consolidate=False,
        ),
        token=None,
    )

    assert drained is True
    assert queue == []
    assert file_preflight == [True]
    assert downloads == []
    assert results[-1]["result"] == "stale_candidate"
    assert source.read_bytes() == b"replacement bytes"


def test_pending_queue_round_trips_and_clears(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    monkeypatch.setattr("qobuz_librarian.config.QUEUE_JOURNAL_DIR", tmp_path / "journals")
    save_pending_queue([_qitem(title="Album A")], mode="album_walk")
    items, mode, saved_at = load_pending_queue()
    assert len(items) == 1 and items[0]["album"]["title"] == "Album A"
    assert mode == "album_walk"
    datetime.fromisoformat(saved_at)        # saved_at is valid ISO
    clear_pending_queue(explicit_discard=True)
    assert load_pending_queue().status is QueueLoadStatus.ABSENT
    # A power cut can leave the file truncated; load discards it, not raise.
    qfile.write_text('{"version": 1, "items": [{"al', encoding="utf-8")
    assert load_pending_queue() == (None, None, None)


def test_resume_keeps_pending_file_when_not_drained(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    monkeypatch.setattr("qobuz_librarian.config.QUEUE_JOURNAL_DIR", tmp_path / "journals")
    save_pending_queue([_qitem()], mode="walk_queue")
    monkeypatch.setattr("qobuz_librarian.queue.executor._execute_download_queue",
                        lambda items, args, token, **kw: ([], False))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    offer_resume_pending_queue(Namespace(), "tok")
    assert load_pending_queue()[0]  # albums left to retry must survive the resume


def test_a_closed_input_does_not_resume_the_saved_queue(tmp_path, monkeypatch):
    """The prompt defaults to yes on Enter, and a closed input was read as
    Enter, so a cron or piped run started downloading a saved queue nobody had
    asked it to."""
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    monkeypatch.setattr("qobuz_librarian.config.QUEUE_JOURNAL_DIR", tmp_path / "journals")
    save_pending_queue([_qitem()], mode="walk_queue")
    started = []
    monkeypatch.setattr("qobuz_librarian.queue.executor._execute_download_queue",
                        lambda items, args, token, **kw: (started.append(items), ([], False))[1])

    def _closed(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _closed)
    assert offer_resume_pending_queue(Namespace(), "tok") is False
    assert started == []
    assert load_pending_queue()[0]


def test_executor_gap_fill_backup_restored_when_track_returns_lossy(monkeypatch, tmp_path):
    """Queue-mode gap-fill backs up present tracks before re-ripping."""
    from qobuz_librarian.library import backup as bkmod
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "02 - kept.flac").write_bytes(b"\x00" * 1000)
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    owned = album_dir / "01 - owned.flac"
    owned.write_bytes(b"the-owned-original")
    gfb = bkmod.backup_gap_fill_files([str(owned)], album_dir)
    assert gfb is not None and not owned.exists()

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir,
        "backup_path": None,
        "gap_fill_backup_path": gfb,
        "siblings_to_delete": [],
        "n_ok": 1, "n_fail": 0, "n_lossy": 1,
        "auto_upgrade": False,
    }
    args = Namespace(no_import=False, consolidate=False)
    result = executor._resolve_queue_item(item, args, imported_globally=True)

    assert owned.exists()
    assert owned.read_bytes() == b"the-owned-original"
    assert result["result"] == "partial"
    assert result["n_lossy_only"] == 1


def test_executor_upgrade_runs_completeness_gate_before_dropping_backup(monkeypatch, tmp_path):
    # The artist/upgrade walks bulk-upgrade through this executor, so it must run
    # the same completeness gate process.py does: a decode-clean import whose
    # rebuilt folder isn't verifiably as complete as the backup KEEPS the backup.
    from qobuz_librarian.modes import process as proc
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"new")
    backup = tmp_path / "backups" / "Album.bak"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"old")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir, "backup_path": backup, "gap_fill_backup_path": None,
        "siblings_to_delete": [], "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(no_import=False, consolidate=False)

    carried = []
    disposed = []
    monkeypatch.setattr(proc, "_carry_non_audio_from_backup",
                        lambda *_a, **_k: carried.append(True))
    monkeypatch.setattr(executor, "dispose_backup",
                        lambda *_a, **_k: disposed.append(True) or True)
    monkeypatch.setattr(executor, "pin_unverified_upgrade_backup",
                        lambda *_a, **_k: True)
    monkeypatch.setattr(executor, "retire_backup_beets_entries",
                        lambda *_a, **_k: True)
    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: False)
    unverified = executor._resolve_queue_item(
        item, args, imported_globally=True
    )
    assert not carried and not disposed
    assert unverified["result"] == "partial"
    assert unverified["upgrade_unverified"] is True

    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: True)
    monkeypatch.setattr(proc, "_carry_non_audio_from_backup",
                        lambda *_a, **_k: (album_dir, {"sealed": True}, []))
    item["backup_path"] = backup
    item["upgrade_unverified"] = False
    verified = executor._resolve_queue_item(
        item, args, imported_globally=True
    )
    assert disposed == [True]
    assert verified["result"] == "downloaded"


@pytest.mark.parametrize(
    "failure",
    ["import", "cleanup"],
)
def test_executor_restores_or_keeps_upgrade_backup_after_import_failure(
        monkeypatch, tmp_path, failure):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations.staging import create_staging_run
    from qobuz_librarian.queue import executor

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    original = album_dir / "01.flac"
    original.write_bytes(b"original")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "FETCH_LOG_FILE", tmp_path / "fetch.jsonl")
    item = _qitem(
        album={
            "id": "A",
            "title": "Album",
            "artist": {"name": "Artist"},
            "tracks": {"items": []},
        },
        album_dir=album_dir,
        auto_upgrade=True,
    )
    queue = [item]

    monkeypatch.setattr(executor, "staging_preflight", lambda _args: None)
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(
        executor, "_reimport_parked_albums", lambda: (False, [])
    )
    run = create_staging_run()
    item["_staging_run"] = run.to_record()
    staged_album = run.path / "Album"

    def download(_item):
        staged_album.mkdir()
        (staged_album / "01.flac").write_bytes(b"replacement")
        _item.update(
            n_ok=1,
            n_fail=0,
            n_lossy=0,
            failed_tracks=[],
            lossy_tracks=[],
            broken_tracks=[],
            elapsed=0.0,
        )

    monkeypatch.setattr(executor, "_download_for_queue_item", download)
    monkeypatch.setattr(
        executor, "_staged_album_dirs", lambda _item: [staged_album]
    )
    monkeypatch.setattr(
        executor,
        "verify_and_recover",
        lambda _album, dirs, **_kwargs: {
            "under": False,
            "recovered": False,
            "retried": False,
            "staged_dirs": dirs,
        },
    )
    monkeypatch.setattr(
        executor, "_run_pre_import_hooks_for_dirs", lambda _dirs, _args: ([], 0)
    )
    monkeypatch.setattr(
        executor, "track_signatures_for_album_dirs", lambda _dirs: []
    )

    def import_album(_dirs, **_kwargs):
        if failure == "import":
            raise OSError(errno.ENOSPC, "beets storage failure")
        staged_album.rename(album_dir)
        return True

    monkeypatch.setattr(executor, "_import_album_with_retry", import_album)
    monkeypatch.setattr(
        executor, "retain_download_staging", lambda *_args, **_kwargs: True
    )
    if failure == "cleanup":
        monkeypatch.setattr(
            executor,
            "retire_download_staging_after_import",
            lambda _item: (_ for _ in ()).throw(
                OSError(errno.ENOSPC, "cannot retire imported staging")
            ),
        )

    def execute():
        return executor._execute_download_queue(
            queue,
            Namespace(
                dry_run=False,
                no_import=False,
                no_downsample=True,
                consolidate=False,
            ),
            token="tok",
        )

    if failure == "import":
        _results, drained = execute()
        assert drained is False
        assert item["result"] == "disk_full"
        assert original.read_bytes() == b"original"
        assert not item["backup_path"].path.exists()
    else:
        with pytest.raises(OSError, match="cannot retire imported staging"):
            execute()
        assert item["result"] == "import_failed"
        assert original.read_bytes() == b"replacement"
        assert item["backup_path"].path.exists()
        assert item["upgrade_unverified"] is True
    assert queue == [item]


def test_executor_upgrade_carries_non_audio_companions_from_backup(monkeypatch, tmp_path):
    # Regression: the bulk/web upgrade path (this executor) must carry non-
    # audio companions (booklets, scans, .cue/.log, and hand-placed art) out of
    # the backup into the rebuilt album before reaping it, exactly as the
    # single-album process.py path does.
    from qobuz_librarian.library.backup import backup_album_dir
    from qobuz_librarian.modes import process as proc
    from qobuz_librarian.queue import executor

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"old")
    (album_dir / "booklet.pdf").write_bytes(b"the-booklet")
    (album_dir / "scans").mkdir()
    (album_dir / "scans" / "front.jpg").write_bytes(b"art")
    monkeypatch.setattr(executor.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(executor.cfg, "UPGRADE_BACKUP_DIR",
                        tmp_path / "backups")
    backup = backup_album_dir(album_dir)
    assert backup is not None and backup.complete is True
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"new")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: True)
    monkeypatch.setattr(proc, "_upgrade_trees_verified", lambda *_a: True)
    monkeypatch.setattr(proc, "_carry_track_annotations", lambda *_a: True)
    monkeypatch.setattr(executor, "retire_backup_beets_entries",
                        lambda *_a, **_k: True)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir, "backup_path": backup, "gap_fill_backup_path": None,
        "siblings_to_delete": [], "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    # Backup reaped, but its non-audio companions carried into the live folder;
    # the upgraded audio is left untouched (the old copy is not carried back).
    assert not backup.path.exists()
    assert (album_dir / "booklet.pdf").read_bytes() == b"the-booklet"
    assert (album_dir / "scans" / "front.jpg").read_bytes() == b"art"
    assert (album_dir / "01.flac").read_bytes() == b"new"


def test_executor_per_album_isolation_one_album_failure_keeps_others(monkeypatch, tmp_path):
    """The whole point of the per-album pipeline: a beets failure on
    album N leaves albums 1..N-1 already imported and N+1..end still
    importable, instead of taking the whole batch down. The failing album's
    staged dir is parked under BEETS_RETRY_DIR for an import-only retry, while
    its queue entry stays pending until that import is actually accepted."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    items = []
    for tag in ("A", "B", "C"):
        items.append({
            "album": {"id": tag, "title": f"Album {tag}",
                      "artist": {"name": f"Artist-{tag}"},
                      "tracks": {"items": []}},
            "album_dir": None,
            "auto_upgrade": False,
            "missing": [], "present": [], "upgrade_only": False,
            "label": tag,
            "n_ok": 1, "n_fail": 0, "n_lossy": 0,
            "failed_tracks": [], "lossy_tracks": [],
            "rate_limited": False, "elapsed": 0.0,
        })

    def fake_download(item):
        tag = item["label"]
        d = staging / f"Artist-{tag}" / f"Album {tag}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "01.flac").write_bytes(b"")

    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "_download_for_queue_item", fake_download)
    monkeypatch.setattr(
        executor,
        "_staged_album_dirs",
        lambda item: [
            staging / f"Artist-{item['label']}" / f"Album {item['label']}"
        ],
    )
    monkeypatch.setattr(executor, "_run_pre_import_hooks_for_dirs",
                        lambda _d, _a: ([], 0))
    # Item B fails beets ("error" = non-retryable); A and C succeed.
    by_label = {"A": "ok", "B": "error", "C": "ok"}
    seen = []

    def fake_import(album_dirs):
        # Map back to the item by its album-dir grandparent name ("Artist-X").
        artist = album_dirs[0].parent.name.split("-")[-1]
        seen.append(artist)
        return by_label[artist]

    monkeypatch.setattr(executor, "beets_import_albums", fake_import)
    monkeypatch.setattr(
        executor, "retire_empty_download_staging", lambda _item: True)
    monkeypatch.setattr(
        executor, "retain_download_staging", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(executor, "_resolve_queue_item",
                        lambda item, args, imported_globally, *, authority=None: {
                            "dir": item["album_dir"], "imported": imported_globally,
                            "result": "downloaded" if imported_globally else "failed",
                            "n_ok": item.get("n_ok", 0),
                            "n_fail": item.get("n_fail", 0),
                            "n_lossy": item.get("n_lossy", 0),
                            "auto_upgrade": False,
                        })

    args = Namespace(dry_run=False, no_import=False, no_downsample=True,
                     consolidate=False)
    results, drained = executor._execute_download_queue(items, args, token=None)

    assert seen == ["A", "B", "C"]
    assert [r["imported"] for r in results] == [True, False, True]
    # B's staged folder got parked; A and C's are still where the test left
    # them (beets would have moved them in real life, but we stubbed it out).
    assert not (staging / "Artist-B" / "Album B").exists()
    from qobuz_librarian.integrations.staging import list_groups
    parked = [
        tree for group in list_groups(kind="beets") for tree in group.trees
        if tree.original_relative.endswith("Album B")
    ]
    assert len(parked) == 1
    assert [item["label"] for item in items] == ["B"]
    assert drained is False


def test_executor_stops_when_queue_progress_cannot_be_persisted(
        monkeypatch, tmp_path):
    """A failed journal commit must stop before the next album starts."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    queue = [
        _qitem("first", album_dir=None),
        _qitem("second", album_dir=None),
    ]
    started = []
    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: (False, []))
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)
    def download(item):
        started.append(item["label"])
        item["n_ok"] = 1

    monkeypatch.setattr(executor, "_download_for_queue_item", download)
    monkeypatch.setattr(
        executor,
        "_resolve_queue_item",
        lambda item, _args, _imported: {"result": item["label"]},
    )

    args = Namespace(
        dry_run=False,
        no_import=True,
        no_downsample=True,
        consolidate=False,
    )

    def fail_save():
        raise OSError("journal write failed")

    with pytest.raises(OSError, match="journal write failed"):
        executor._execute_download_queue(
            queue,
            args,
            token=None,
            on_progress=fail_save,
        )

    assert started == ["first"]


def test_reimport_parked_albums_clears_moved_and_keeps_skipped(monkeypatch, tmp_path):
    """A parked album is cleared only when its audio actually leaves disk on the
    retry import. A beets run that exits 0 while skipping the album (e.g. a
    library duplicate) leaves the files in place. The parked copy must be kept,
    not deleted on the strength of the exit code, since it's the only copy."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations.staging import park_trees
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    good_source = staging / "Good Album"
    skipped_source = staging / "Dup Album"
    good_source.mkdir(parents=True)
    skipped_source.mkdir(parents=True)
    good_flac = good_source / "01.flac"
    skipped_flac = skipped_source / "01.flac"
    good_flac.write_bytes(b"flac")
    skipped_flac.write_bytes(b"flac")
    good_group = park_trees([good_source], "good")
    skipped_group = park_trees([skipped_source], "skipped")
    assert good_group is not None and skipped_group is not None
    good = good_group.path
    skipped = skipped_group.path
    good_flac = good_group.trees[0].path / "01.flac"
    skipped_flac = skipped_group.trees[0].path / "01.flac"

    def fake_import(dirs):
        # beets moves audio into the library on a real import; simulate that for
        # the good album and leave the skipped one's files where they are.
        if dirs[0] == good_group.trees[0].path:
            good_flac.unlink()
        return "ok"  # Exit 0 either way; the disk decides cleanup.
    monkeypatch.setattr(executor, "beets_import_albums", fake_import)

    assert executor._reimport_parked_albums()[0] is True
    assert not good.exists()           # audio moved out → parking dir cleared
    assert skipped.exists()            # files remain → kept parked, not deleted
    assert skipped_flac.exists()       # the only copy of the skipped track survives
