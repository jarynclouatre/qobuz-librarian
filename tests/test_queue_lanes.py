"""Which lane a queued album takes, with startup recovery running for real.

tests/test_queue.py stubs plan_durable_new_album, recover_startup_state and
queue_item_may_create_library_backup for every executor test, so the interlock
between a saved queue and the executor is never exercised there. These tests
leave all three alone.
"""
from argparse import Namespace
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.queue import executor
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue.builder import _build_queue_item


@pytest.fixture
def lease():
    held = run_lock.current_lease()
    if held is not None:
        yield held
        return
    acquired = run_lock.acquire()
    try:
        yield acquired
    finally:
        acquired.close()


@pytest.fixture
def stub_download(monkeypatch, tmp_path):
    """Run the legacy lane end to end without touching Qobuz or beets."""
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)

    landed = []

    def fake_download(item):
        landed.append(item["label"])
        item["n_ok"] = len(item["missing"])
        album_dir = staging / item["label"]
        album_dir.mkdir(parents=True, exist_ok=True)
        (album_dir / "01.flac").write_bytes(b"")

    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "backup_album_dir", lambda _d, **_kw: None)
    monkeypatch.setattr(
        executor,
        "_validate_queue_item_premise",
        lambda _item: None,
    )
    monkeypatch.setattr(executor, "_download_for_queue_item", fake_download)
    monkeypatch.setattr(
        executor, "_staged_album_dirs", lambda item: [staging / item["label"]])
    monkeypatch.setattr(
        executor, "_run_pre_import_hooks_for_dirs", lambda *_a, **_kw: ([], 0))
    monkeypatch.setattr(executor, "beets_import_albums", lambda _dirs: "ok")
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(
        executor, "retire_empty_download_staging", lambda _item: True)
    monkeypatch.setattr(
        executor, "retain_download_staging", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        executor,
        "_resolve_queue_item",
        lambda item, args, imported, *, authority=None: {
            "dir": item["album_dir"],
            "imported": imported,
            "result": "downloaded" if imported else "failed",
            "n_ok": item.get("n_ok", 0),
            "n_fail": 0,
            "n_lossy": 0,
            "auto_upgrade": bool(item.get("auto_upgrade")),
        },
    )
    return landed


def _album(track_count=12):
    items = [
        {"id": 100 + i, "title": f"Track {i + 1}", "track_number": i + 1,
         "media_number": 1, "duration": 200, "streamable": True}
        for i in range(track_count)
    ]
    return {
        "id": "album-1", "title": "Album", "artist": {"name": "Artist"},
        "tracks": {"items": items}, "tracks_count": track_count,
        "maximum_bit_depth": 24, "maximum_sampling_rate": 96,
        "hires": True, "streamable": True,
    }


def _args():
    return Namespace(dry_run=False, no_import=False, consolidate=False,
                     no_downsample=False, force=False, yes=True,
                     no_upgrade=False, quality=None)


def test_walk_flush_runs_items_the_durable_lane_cannot_plan(lease, stub_download):
    """The album walk saves its queue and then flushes it, so every item
    resolves an owner for its own pending entry. A small gap fill has no
    durable plan; refusing it there stopped the flush and left the saved queue
    failing the same way on every launch."""
    album = _album()
    tracks = album["tracks"]["items"]
    item = _build_queue_item(
        album=album, album_dir=Path("/music/Artist/Album"), label="gap",
        missing=tracks[:2], present=tracks[2:],
        upgrade_only=False, auto_upgrade=False)
    queue = [item]
    queue_state.save_pending_queue(queue, mode="album_walk")

    results, drained = executor._execute_download_queue(
        queue, _args(), "token",
        on_progress=lambda: queue_state.save_pending_queue(
            queue, mode="album_walk"))

    assert stub_download == ["gap"]
    assert [r["imported"] for r in results] == [True]
    assert drained is True
    assert queue == []
    loaded = queue_state.load_queue_journal()
    assert loaded.status is queue_state.QueueLoadStatus.ABSENT or not (
        loaded.journal.items)


def test_cli_queue_refresh_keeps_downloaded_album_identity(
        lease, stub_download, monkeypatch):
    album = _album()
    tracks = album["tracks"]["items"]
    item = _build_queue_item(
        album=album,
        album_dir=Path("/music/Artist/Album"),
        label="gap",
        missing=tracks[:2],
        present=tracks[2:],
        upgrade_only=False,
        auto_upgrade=False,
    )
    refreshed = []
    monkeypatch.setattr(
        executor,
        "_refresh_review_state_after_downloads",
        lambda changes, token, args, **kwargs: refreshed.append(
            (changes, token, args, kwargs)
        ),
    )
    args = _args()

    results, drained = executor._execute_download_queue(
        [item],
        args,
        "token",
        refresh_review=True,
    )

    assert drained is True
    assert [result["imported"] for result in results] == [True]
    assert len(refreshed) == 1
    changes, token, run_args, kwargs = refreshed[0]
    assert token == "token"
    assert run_args is args
    assert kwargs == {"allow_upgrade_refresh": True}
    assert changes == [{**results[0], "album": album}]


def test_cli_queue_refreshes_completed_items_before_auth_loss(
        lease, stub_download, monkeypatch):
    from qobuz_librarian.api.auth import AuthLost

    first_album = _album()
    second_album = _album() | {"id": "album-2", "title": "Second"}
    first_tracks = first_album["tracks"]["items"]
    second_tracks = second_album["tracks"]["items"]
    first = _build_queue_item(
        album=first_album,
        album_dir=Path("/music/Artist/Album"),
        label="first",
        missing=first_tracks[:2],
        present=first_tracks[2:],
        upgrade_only=False,
        auto_upgrade=False,
    )
    second = _build_queue_item(
        album=second_album,
        album_dir=Path("/music/Artist/Second"),
        label="second",
        missing=second_tracks[:2],
        present=second_tracks[2:],
        upgrade_only=False,
        auto_upgrade=False,
    )
    successful_download = executor._download_for_queue_item

    def download(item):
        if item["label"] == "second":
            raise AuthLost("token expired")
        successful_download(item)

    monkeypatch.setattr(executor, "_download_for_queue_item", download)
    refreshed = []
    monkeypatch.setattr(
        executor,
        "_refresh_review_state_after_downloads",
        lambda changes, token, args, **kwargs: refreshed.append(
            (changes, token, args, kwargs)
        ),
    )
    args = _args()

    with pytest.raises(AuthLost):
        executor._execute_download_queue(
            [first, second],
            args,
            "token",
            refresh_review=True,
        )

    assert len(refreshed) == 1
    changes, token, run_args, kwargs = refreshed[0]
    assert token == "token"
    assert run_args is args
    assert kwargs == {"allow_upgrade_refresh": False}
    assert len(changes) == 1
    assert changes[0]["album"] == first_album
    assert changes[0]["imported"] is True


def test_library_changing_item_without_a_plan_uses_the_legacy_lane(
        lease, stub_download, monkeypatch):
    """Replacing siblings and downsampled upgrades both fall outside the
    exact-recovery lane. They still have to run - the backup-and-restore path
    is what covered them before that lane existed."""
    album = _album()
    tracks = album["tracks"]["items"]
    siblings = _build_queue_item(
        album=album, album_dir=Path("/music/Artist/Album"), label="siblings",
        missing=tracks, present=[], upgrade_only=False, auto_upgrade=False,
        siblings_to_delete=[Path("/music/Artist/Album (dupe)")])
    assert executor.queue_item_may_create_library_backup(siblings) is True
    assert executor.plan_durable_new_album(siblings, _args()) is None
    results, _ = executor._execute_download_queue([siblings], _args(), "token")
    assert [r["imported"] for r in results] == [True]

    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", True)
    upgrade = _build_queue_item(
        album=album, album_dir=Path("/music/Artist/Album"), label="upgrade",
        missing=tracks, present=[], upgrade_only=False, auto_upgrade=True)
    assert executor.plan_durable_new_album(upgrade, _args()) is None
    results, _ = executor._execute_download_queue([upgrade], _args(), "token")
    assert [r["imported"] for r in results] == [True]
    assert stub_download == ["siblings", "upgrade"]


def test_unstarted_durable_claim_returns_to_pending_when_it_cannot_replan(
        lease, stub_download, monkeypatch):
    """Switching downsampling on between an interrupted durable download and
    its retry leaves an active claim the lane can no longer plan. Nothing was
    mutated, so the claim goes back to pending and the album runs."""
    from qobuz_librarian.completion import RecoveryOwner
    from qobuz_librarian.queue.durable_album import initial_completion_input

    album = _album()
    tracks = album["tracks"]["items"]
    item = _build_queue_item(
        album=album, album_dir=None, label="new",
        missing=tracks, present=[], upgrade_only=False, auto_upgrade=False)
    plan = executor.plan_durable_new_album(item, _args())
    assert plan is not None

    journal = queue_state.save_pending_queue([item], mode="album_walk")
    entry = journal.items[0]
    origin, _mode, _album_id, _ack = executor._durable_execution_origin()
    queue_state.transition_journal_item(
        journal, entry.item_id, queue_state.QueuePhase.ACTIVE,
        completion_input=initial_completion_input(
            plan,
            RecoveryOwner(journal.operation_id, entry.item_id),
            origin,
        ),
    )
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", True)
    assert executor.plan_durable_new_album(item, _args()) is None

    queue = [item]
    results, _ = executor._execute_download_queue(queue, _args(), "token")

    assert [r["imported"] for r in results] == [True]
    assert stub_download == ["new"]
    reloaded = queue_state.load_queue_journal(journal.operation_id)
    assert reloaded.journal.items[0].phase is queue_state.QueuePhase.PENDING


def test_durable_attention_stop_leaves_the_flush_returning(lease, stub_download):
    """A parked attention item stays blocked without failing the batch flush."""
    from qobuz_librarian.queue.durable_runner import (
        DurableAlbumResult,
        DurableAlbumStatus,
    )

    album = _album()
    tracks = album["tracks"]["items"]
    parked = _build_queue_item(
        album=album, album_dir=None, label="parked",
        missing=tracks, present=[], upgrade_only=False, auto_upgrade=False)
    waiting = _build_queue_item(
        album=_album() | {"id": "album-2", "title": "Second"},
        album_dir=None, label="waiting",
        missing=tracks, present=[], upgrade_only=False, auto_upgrade=False)
    queue = [parked, waiting]
    queue_state.save_pending_queue(queue, mode="album_walk")

    def block_and_park(_queue, _item, _args, *, resume_owner, **_kw):
        loaded = queue_state.load_queue_journal(resume_owner.operation_id)
        queue_state.transition_journal_item(
            loaded.journal, resume_owner.item_id,
            queue_state.QueuePhase.BLOCKED, block_reason="import-attention")
        return DurableAlbumResult(
            status=DurableAlbumStatus.ATTENTION, reason="import-attention")

    executor.execute_durable_new_album = block_and_park
    try:
        results, drained = executor._execute_download_queue(
            queue, _args(), "token",
            on_progress=lambda: queue_state.save_pending_queue(
                queue, mode="album_walk"))
    finally:
        del executor.execute_durable_new_album

    assert drained is False
    assert [r["result"] for r in results] == ["attention"]
    assert queue == [parked, waiting]
    loaded = queue_state.load_queue_journal()
    assert loaded.journal.items[0].phase is queue_state.QueuePhase.BLOCKED


def test_parked_album_is_not_reported_as_a_clean_run():
    """A parked album downloads every track before it stops, so the track tally
    reads zero failures and only the album tally can tell the run apart from a
    clean one."""
    from qobuz_librarian.ui_cli.colors import C

    colour, text = executor._queue_done_line(
        [{"result": "attention", "n_ok": 14, "n_fail": 0}], 1)

    assert colour == C.YELLOW
    assert "✓" not in text
    assert "1 album unfinished" in text

    colour, text = executor._queue_done_line(
        [{"result": "downloaded", "n_ok": 14, "n_fail": 0}], 1)

    assert colour == C.GREEN
    assert text.startswith("  ✓ Queue done: 1/1 albums OK")
    assert "unfinished" not in text
