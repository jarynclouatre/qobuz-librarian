import json
from dataclasses import replace
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    AlbumInventory,
    CompletionExpectation,
    CompletionInput,
    CompletionOrigin,
    CompletionOriginKind,
    CompletionScope,
    DownloadCounts,
    LandingReceipt,
    ManagedImportEvidence,
    ManagedMapping,
    QualityTarget,
    RecoveryOwner,
    SourceLineage,
    StagedReceipt,
    TrackQuality,
    assess_completion,
)
from qobuz_librarian.queue import journal as journal_module
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.persistence import (
    QueueJournalBlocked,
    QueueLoadStatus,
    QueuePhase,
    RecoveryReference,
    clear_queue_journal,
    commit_completed_item_removal,
    commit_recovered_completed_item_removal,
    create_queue_journal,
    list_queue_journals,
    load_pending_queue,
    load_queue_journal,
    process_carrier_retirement,
    save_queue_journal,
    transition_journal_item,
)


def _queue_item(title="Test"):
    return _build_queue_item(
        album={"id": "1", "title": title},
        album_dir=Path(f"/music/{title.lower()}"),
        label=title,
        missing=[],
        present=[],
        upgrade_only=False,
        auto_upgrade=False,
    )


def _completion_input(journal, item_id, *, complete=False):
    slots = ("qobuz:track-1",)
    lineages = ()
    counts = None
    if complete:
        lineages = (SourceLineage(
            slot=slots[0],
            origin=StagedReceipt(
                path=f"/staging/{item_id}.flac",
                identity=(1, 2, 3, 4, 5, 6),
            ),
        ),)
        counts = DownloadCounts()
    return CompletionInput(
        owner=RecoveryOwner(journal.operation_id, item_id),
        origin=CompletionOrigin(CompletionOriginKind.CLI, "test-queue"),
        expectation=CompletionExpectation(
            album_id="1",
            scope=CompletionScope.ALBUM,
            catalogue_slots=slots,
            requested_slots=slots,
            quality_targets=(QualityTarget(slots[0], 16, 44_100),),
        ),
        effective_tier=2,
        lineages=lineages,
        counts=counts,
    )


def _completion_evidence(journal, item_id):
    completion_input = _completion_input(journal, item_id, complete=True)
    lineage = completion_input.lineages[0]
    source = lineage.current
    destination = (2, 111, 100, 1_000, 3_000)
    album_identity = (2, 99, 0, 900, 2_900)
    destination_path = "Artist/Album/01 Test.flac"
    landing = LandingReceipt(
        lineage.slot,
        destination_path,
        destination,
        "d" * 64,
    )
    assessment = assess_completion(
        owner=completion_input.owner,
        expectation=completion_input.expectation,
        download=completion_input.download_coverage(),
        managed=ManagedImportEvidence(
            owner=completion_input.owner,
            library_root="/music",
            library_root_identity=(2, 10, 0, 1_000, 2_000),
            album_path="Artist/Album",
            album_identity=album_identity,
            manifest_hash="c" * 64,
            mappings=(ManagedMapping(
                lineage.slot,
                source.path,
                source.identity,
                destination_path,
                destination,
            ),),
        ),
        landings=(landing,),
        inventory=AlbumInventory(
            path="Artist/Album",
            identity=album_identity,
            audio=(landing,),
        ),
        quality=(TrackQuality(
            slot=lineage.slot,
            path=destination_path,
            identity=destination,
            sha256=landing.sha256,
            served_bits=16,
            served_rate=44_100,
        ),),
    )
    assert assessment.evidence is not None
    return assessment.evidence


def _managed_reference(journal, item_id):
    return RecoveryReference(
        name="managed-import",
        kind="managed-beets",
        data={
            "version": 1,
            "path": "/private/managed-carrier.jsonl",
            "device": 1,
            "inode": 2,
            "parent_device": 3,
            "parent_inode": 4,
            "nonce": "d" * 64,
            "owner": {
                "operation_id": journal.operation_id,
                "item_id": item_id,
            },
        },
    )


@pytest.fixture
def queue_paths(tmp_path, monkeypatch):
    journals = tmp_path / ".qobuz_queue_journals"
    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", journals)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    return journals


def test_durability_failures_leave_last_committed_snapshot(
    queue_paths, monkeypatch
):
    base_journals = queue_paths
    journals = base_journals.parent / "new-data" / "state" / "journals"
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", journals)
    synced = []
    real_fsync_directory = journal_module._fsync_directory
    durability = {
        "ancestor_failed": False,
        "post_replace": False,
        "post_replace_failed": False,
        "replaced": False,
    }

    def controlled_fsync(path):
        if path == journals.parent.parent.parent and not durability["ancestor_failed"]:
            durability["ancestor_failed"] = True
            raise OSError("ancestor relationship not synced")
        if (
            path == journals
            and durability["post_replace"]
            and durability["replaced"]
            and not durability["post_replace_failed"]
        ):
            durability["post_replace_failed"] = True
            raise OSError("replacement namespace not synced")
        real_fsync_directory(path)
        synced.append(path)

    monkeypatch.setattr(journal_module, "_fsync_directory", controlled_fsync)
    uncommitted = create_queue_journal(
        [_queue_item("First"), _queue_item("Second")],
        mode="album",
    )
    with pytest.raises(OSError, match="ancestor relationship"):
        save_queue_journal(uncommitted)

    assert not list(journals.glob("*.json"))

    real_replace = journal_module.os.replace

    def mark_replace(source, destination):
        real_replace(source, destination)
        durability["replaced"] = True

    durability["post_replace"] = True
    monkeypatch.setattr(journal_module.os, "replace", mark_replace)
    with pytest.raises(OSError, match="replacement namespace"):
        save_queue_journal(uncommitted)

    recovered = load_queue_journal(uncommitted.operation_id)
    pending = recovered.journal

    assert recovered.status is QueueLoadStatus.READY
    assert pending.generation == 1
    assert {
        journals.parent.parent.parent,
        journals.parent.parent,
        journals.parent,
        journals,
    }.issubset(synced)
    current = transition_journal_item(
        pending,
        pending.items[0].item_id,
        QueuePhase.ACTIVE,
        completion_input=_completion_input(pending, pending.items[0].item_id),
    )
    reloaded = load_queue_journal(pending.operation_id)
    assert reloaded.journal.generation == 2
    assert [item.phase for item in reloaded.journal.items] == [
        QueuePhase.ACTIVE,
        QueuePhase.PENDING,
    ]

    def fail_write(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(journal_module, "_write_durable_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        transition_journal_item(
            current,
            current.items[1].item_id,
            QueuePhase.ACTIVE,
            completion_input=_completion_input(
                current, current.items[1].item_id),
        )

    assert current.items[1].phase is QueuePhase.PENDING
    assert load_queue_journal(current.operation_id).journal == reloaded.journal


def test_malformed_v2_blocks_replay_and_requires_explicit_discard(queue_paths):
    journals = queue_paths
    operation_id = "a" * 64
    path = journals / f"{operation_id}.json"
    journals.mkdir()
    raw = b'{"version":2,"items":[{}]}'
    path.write_bytes(raw)

    lock_target = journals.parent / "wrong-lock-target"
    lock_target.write_text("do not lock this", encoding="utf-8")
    lock_path = journals / journal_module._JOURNAL_LOCK_NAME
    lock_path.symlink_to(lock_target)
    unsafe_lock = load_queue_journal(operation_id)
    assert unsafe_lock.status is QueueLoadStatus.BLOCKED
    assert lock_path.is_symlink()
    lock_path.unlink()

    loaded = load_queue_journal(operation_id)
    assert loaded.status is QueueLoadStatus.BLOCKED
    assert path.read_bytes() == raw
    with pytest.raises(QueueJournalBlocked):
        clear_queue_journal(operation_id, explicit_discard=True)
    assert path.read_bytes() == raw


def test_completed_removal_commits_first(queue_paths, monkeypatch):
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    save_queue_journal(create_queue_journal([_queue_item("Saved")], mode="walk_queue"))
    loaded = load_pending_queue()
    caller_items, _mode, _saved_at = loaded

    item_id = loaded.journal.items[0].item_id
    active = transition_journal_item(
        loaded.journal,
        item_id,
        QueuePhase.ACTIVE,
        completion_input=_completion_input(loaded.journal, item_id),
    )
    resolved_carrier = _managed_reference(loaded.journal, item_id)
    resolving = transition_journal_item(
        active,
        item_id,
        QueuePhase.RESOLVING,
        completion_input=_completion_input(active, item_id, complete=True),
        recovery_references=(resolved_carrier,),
    )
    with pytest.raises(QueueJournalBlocked, match="preserved exactly"):
        transition_journal_item(
            resolving,
            item_id,
            QueuePhase.COMPLETE,
            recovery_references=(),
            completion_evidence=_completion_evidence(resolving, item_id).to_record(),
        )
    live_evidence = _completion_evidence(resolving, item_id)
    completion_evidence = live_evidence.to_record()
    complete = transition_journal_item(
        resolving,
        item_id,
        QueuePhase.COMPLETE,
        completion_evidence=completion_evidence,
    )
    assert complete.items[0].recovery_references == (resolved_carrier,)
    real_write = journal_module._write_durable_json
    with pytest.raises(QueueJournalBlocked, match="live completion"):
        commit_completed_item_removal(
            complete,
            caller_items,
            item_id=item_id,
            caller_item=caller_items[0],
            live_evidence=replace(
                live_evidence,
                managed_manifest_hash="e" * 64,
            ),
            post_import_action=None,
        )
    assert len(caller_items) == 1
    monkeypatch.setattr(
        journal_module,
        "_write_durable_json",
        lambda _path, _payload: (_ for _ in ()).throw(OSError("commit failed")),
    )
    with pytest.raises(OSError, match="commit failed"):
        commit_recovered_completed_item_removal(
            complete,
            item_id=item_id,
            live_evidence=live_evidence,
            post_import_action=None,
        )
    assert len(caller_items) == 1
    retained = load_queue_journal(complete.operation_id).journal.items[0]
    assert retained.phase is QueuePhase.COMPLETE
    assert retained.recovery_references == (resolved_carrier,)
    assert load_queue_journal(complete.operation_id).journal.retirements == ()

    monkeypatch.setattr(journal_module, "_write_durable_json", real_write)
    emptied = commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=live_evidence,
        post_import_action=None,
    )
    assert emptied.items == ()
    assert len(emptied.retirements) == 1
    retirement = emptied.retirements[0]
    assert retirement.item_id == item_id
    assert retirement.reference == resolved_carrier
    assert retirement.manifest_hash == "c" * 64
    assert retirement.quarantine_name.startswith(
        ".qobuz-retire-managed-beets-"
    )
    assert len(caller_items) == 1
    reloaded_retirement = load_queue_journal(emptied.operation_id)
    assert reloaded_retirement.journal.retirements == (retirement,)
    assert reloaded_retirement.as_legacy_tuple() == (None, None, None)
    with pytest.raises(
        QueueJournalBlocked,
        match="retirement cannot be discarded",
    ):
        clear_queue_journal(emptied.operation_id, explicit_discard=True)
    emptied = journal_module.acknowledge_carrier_retirement_completion(
        emptied, item_id=item_id
    )
    retirement = emptied.retirements[0]

    from qobuz_librarian.integrations import beets

    uncertain = beets.ManagedCarrierRetirementResult(
        beets.ManagedCarrierRetirementOutcome.UNCERTAIN,
        "directory sync was interrupted",
    )
    monkeypatch.setattr(
        beets,
        "retire_managed_carrier",
        lambda *_args: uncertain,
    )
    retained_journal, retained_result = process_carrier_retirement(
        emptied,
        item_id=item_id,
    )
    assert retained_result is uncertain
    assert retained_journal.retirements == (retirement,)
    assert load_queue_journal(emptied.operation_id).journal.retirements == (
        retirement,
    )

    absent = beets.ManagedCarrierRetirementResult(
        beets.ManagedCarrierRetirementOutcome.ALREADY_ABSENT,
    )
    monkeypatch.setattr(
        beets,
        "retire_managed_carrier",
        lambda *_args: absent,
    )
    monkeypatch.setattr(journal_module, "_write_durable_json", real_write)
    settled, settled_result = process_carrier_retirement(
        emptied,
        item_id=item_id,
    )
    assert settled_result is absent
    assert settled.retirements == ()
    assert load_queue_journal(emptied.operation_id).journal.retirements == ()
