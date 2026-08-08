
import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
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
from qobuz_librarian.library.post_import_relocation import (
    capture_post_import_relocation_expectation,
)
from qobuz_librarian.queue import journal, post_import_finalizer
from qobuz_librarian.queue.builder import _build_queue_item


def _queue_item(*, album_dir=None):
    return _build_queue_item(
        album={"id": "1", "title": "Album"},
        album_dir=album_dir,
        label="Album",
        missing=[],
        present=[],
        upgrade_only=False,
        auto_upgrade=False,
    )


def _completion_input(
    saved,
    item_id,
    staging,
    *,
    complete=False,
    origin_kind=CompletionOriginKind.CLI,
    origin_reference="test-queue",
):
    slot = "qobuz:track-1"
    return CompletionInput(
        owner=RecoveryOwner(saved.operation_id, item_id),
        origin=CompletionOrigin(origin_kind, origin_reference),
        expectation=CompletionExpectation(
            album_id="1",
            scope=CompletionScope.ALBUM,
            catalogue_slots=(slot,),
            requested_slots=(slot,),
            quality_targets=(QualityTarget(slot, 16, 44_100),),
        ),
        effective_tier=2,
        lineages=(
            (SourceLineage(
                slot=slot,
                origin=StagedReceipt(
                    path=str(staging / f"{item_id}.flac"),
                    identity=(1, 2, 3, 4, 5, 6),
                ),
            ),)
            if complete
            else ()
        ),
        counts=DownloadCounts() if complete else None,
    )


def _completion_evidence(
    saved,
    item_id,
    staging,
    music,
    *,
    album_path="Artist, Other/Album",
    origin_kind=CompletionOriginKind.CLI,
    origin_reference="test-queue",
):
    completion_input = _completion_input(
        saved,
        item_id,
        staging,
        complete=True,
        origin_kind=origin_kind,
        origin_reference=origin_reference,
    )
    lineage = completion_input.lineages[0]
    destination_identity = (2, 111, 100, 1_000, 3_000)
    album_identity = (2, 99, 0, 900, 2_900)
    destination_path = f"{album_path}/01 Test.flac"
    landing = LandingReceipt(
        lineage.slot,
        destination_path,
        destination_identity,
        "d" * 64,
    )
    assessment = assess_completion(
        owner=completion_input.owner,
        expectation=completion_input.expectation,
        download=completion_input.download_coverage(),
        managed=ManagedImportEvidence(
            owner=completion_input.owner,
            library_root=str(music),
            library_root_identity=(2, 10, 0, 1_000, 2_000),
            album_path=album_path,
            album_identity=album_identity,
            manifest_hash="c" * 64,
            mappings=(ManagedMapping(
                lineage.slot,
                lineage.current.path,
                lineage.current.identity,
                destination_path,
                destination_identity,
            ),),
        ),
        landings=(landing,),
        inventory=AlbumInventory(
            path=album_path,
            identity=album_identity,
            audio=(landing,),
        ),
        quality=(TrackQuality(
            slot=lineage.slot,
            path=destination_path,
            identity=destination_identity,
            sha256=landing.sha256,
            served_bits=16,
            served_rate=44_100,
        ),),
    )
    assert assessment.evidence is not None
    return assessment.evidence


def _managed_reference(saved, item_id, tmp_path):
    return journal.RecoveryReference(
        name="managed-import",
        kind="managed-beets",
        data={
            "version": 1,
            "path": str(tmp_path / "managed-carrier.jsonl"),
            "device": 1,
            "inode": 2,
            "parent_device": 3,
            "parent_inode": 4,
            "nonce": "b" * 64,
            "owner": {
                "operation_id": saved.operation_id,
                "item_id": item_id,
            },
        },
    )


@pytest.fixture
def queue_paths(tmp_path, monkeypatch):
    music = tmp_path / "music"
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    staging = tmp_path / "staging"
    source.mkdir(parents=True)
    staging.mkdir()
    (source / "01 Test.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "PENDING_QUEUE_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    return tmp_path, music, source, destination, staging


def _completed_journal(
    queue_paths,
    *,
    planned_album_dir=None,
    completion_album_path="Artist, Other/Album",
    origin_kind=CompletionOriginKind.CLI,
    origin_reference="test-queue",
):
    tmp_path, music, _source, _destination, staging = queue_paths
    pending = journal.save_queue_journal(
        journal.create_queue_journal(
            [_queue_item(album_dir=planned_album_dir)], mode="test"
        )
    )
    item_id = pending.items[0].item_id
    active = journal.transition_journal_item(
        pending,
        item_id,
        journal.QueuePhase.ACTIVE,
        completion_input=_completion_input(
            pending,
            item_id,
            staging,
            origin_kind=origin_kind,
            origin_reference=origin_reference,
        ),
        multi_artist_filing=True,
    )
    resolving = journal.transition_journal_item(
        active,
        item_id,
        journal.QueuePhase.RESOLVING,
        completion_input=_completion_input(
            active,
            item_id,
            staging,
            complete=True,
            origin_kind=origin_kind,
            origin_reference=origin_reference,
        ),
        recovery_references=(_managed_reference(active, item_id, tmp_path),),
    )
    evidence = _completion_evidence(
        resolving,
        item_id,
        staging,
        music,
        album_path=completion_album_path,
        origin_kind=origin_kind,
        origin_reference=origin_reference,
    )
    complete = journal.transition_journal_item(
        resolving,
        item_id,
        journal.QueuePhase.COMPLETE,
        completion_evidence=evidence.to_record(),
    )
    return complete, item_id, evidence


def test_completed_removal_and_action_handoff_are_one_durable_state_machine(
    queue_paths, monkeypatch
):
    _tmp_path, _music, source, destination, _staging = queue_paths
    complete, item_id, evidence = _completed_journal(queue_paths)
    authority = run_lock.acquire()
    try:
        expectation = capture_post_import_relocation_expectation(
            source, destination, authority=authority
        )
    finally:
        authority.close()
    action_id = "a" * 64
    action = {
        "action_id": action_id,
        "kind": "whole-album",
        "source": str(source),
        "destination": str(destination),
        "expectation": expectation,
        "phase": "planned",
        "relocation_operation_id": None,
        "handoff_hash": None,
    }

    real_write = journal._write_durable_json
    monkeypatch.setattr(
        journal,
        "_write_durable_json",
        lambda *_args: (_ for _ in ()).throw(OSError("injected write failure")),
    )
    with pytest.raises(OSError, match="injected write failure"):
        journal.commit_recovered_completed_item_removal(
            complete,
            item_id=item_id,
            live_evidence=evidence,
            post_import_action=action,
        )
    unchanged = journal.load_queue_journal(complete.operation_id).journal
    assert unchanged is not None
    assert len(unchanged.items) == 1
    assert unchanged.retirements == ()

    monkeypatch.setattr(journal, "_write_durable_json", real_write)
    retired = journal.commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=evidence,
        post_import_action=action,
    )
    assert retired.items == ()
    assert retired.retirements[0].action is not None
    assert retired.retirements[0].completion_acknowledged is False
    with pytest.raises(journal.QueueJournalBlocked, match="post-import"):
        journal.process_carrier_retirement(retired, item_id=item_id)

    relocation_id = "e" * 64
    handoff_hash = "f" * 64
    handoff = {
        "consumer": {
            "kind": "queue-completion",
            "queue_operation_id": retired.operation_id,
            "item_id": item_id,
            "action_id": action_id,
        },
        "hash": handoff_hash,
    }
    assert not journal.post_import_relocation_handoff_matches(
        relocation_id, handoff
    )
    handed_off = journal.checkpoint_post_import_action_handoff(
        retired,
        item_id=item_id,
        action_id=action_id,
        operation_id=relocation_id,
        handoff_hash=handoff_hash,
    )
    assert journal.post_import_relocation_handoff_matches(
        relocation_id, handoff
    )
    assert not journal.post_import_relocation_handoff_matches(
        "0" * 64, handoff
    )

    committed = journal.commit_post_import_action(
        handed_off,
        item_id=item_id,
        action_id=action_id,
        operation_id=relocation_id,
        handoff_hash=handoff_hash,
        final_path=destination,
    )
    assert committed.retirements[0].final_path == str(destination)
    assert journal.post_import_relocation_handoff_matches(
        relocation_id, handoff
    )
    cleared = journal.clear_committed_post_import_action(
        committed,
        item_id=item_id,
        action_id=action_id,
    )
    assert cleared.retirements[0].action is None
    assert not journal.post_import_relocation_handoff_matches(
        relocation_id, handoff
    )
    with pytest.raises(journal.QueueJournalBlocked, match="post-import"):
        journal.process_carrier_retirement(cleared, item_id=item_id)

    acknowledged = journal.acknowledge_carrier_retirement_completion(
        cleared,
        item_id=item_id,
    )
    from qobuz_librarian.integrations import beets

    outcome = beets.ManagedCarrierRetirementResult(
        beets.ManagedCarrierRetirementOutcome.ALREADY_ABSENT
    )
    monkeypatch.setattr(beets, "retire_managed_carrier", lambda *_args: outcome)
    settled, result = journal.process_carrier_retirement(
        acknowledged,
        item_id=item_id,
    )
    assert result is outcome
    assert settled.retirements == ()


def test_conflicting_planned_action_settles_as_an_exact_noop(
    queue_paths, monkeypatch
):
    tmp_path, _music, source, destination, _staging = queue_paths
    destination.mkdir(parents=True)
    (destination / "01 Test.flac").write_bytes(b"different audio")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)

    complete, item_id, evidence = _completed_journal(queue_paths)
    authority = run_lock.acquire()
    try:
        expectation = capture_post_import_relocation_expectation(
            source, destination, authority=authority
        )
        retired = journal.commit_recovered_completed_item_removal(
            complete,
            item_id=item_id,
            live_evidence=evidence,
            post_import_action={
                "action_id": "a" * 64,
                "kind": "whole-album",
                "source": str(source),
                "destination": str(destination),
                "expectation": expectation,
                "phase": "planned",
                "relocation_operation_id": None,
                "handoff_hash": None,
            },
        )
        settled = post_import_finalizer._settle_action(
            retired,
            retired.retirements[0],
            authority,
        )
    finally:
        authority.close()

    assert settled.retirements[0].action is None
    assert settled.retirements[0].final_path == str(source)
    assert (source / "01 Test.flac").read_bytes() == b"audio"
    assert (destination / "01 Test.flac").read_bytes() == b"different audio"


def test_completion_owner_refusal_keeps_retirement_for_exact_replay(
    queue_paths, monkeypatch
):
    complete, item_id, evidence = _completed_journal(
        queue_paths,
        origin_kind=CompletionOriginKind.WEB_JOB,
        origin_reference="web-job-1",
    )
    retired = journal.commit_recovered_completed_item_removal(
        complete,
        item_id=item_id,
        live_evidence=evidence,
        post_import_action=None,
    )
    carrier_calls = []
    from qobuz_librarian.integrations import beets

    outcome = beets.ManagedCarrierRetirementResult(
        beets.ManagedCarrierRetirementOutcome.ALREADY_ABSENT
    )
    monkeypatch.setattr(
        beets,
        "retire_managed_carrier",
        lambda *_args, **_kwargs: carrier_calls.append(item_id) or outcome,
    )
    acknowledgements = []

    def acknowledge(
        origin,
        owner,
        *,
        album_id,
        completion_hash,
        planned,
        post_dir,
    ):
        acknowledgements.append((
            origin,
            owner,
            album_id,
            completion_hash,
            planned,
            post_dir,
        ))
        return len(acknowledgements) > 1

    authority = run_lock.acquire()
    try:
        with pytest.raises(
            post_import_finalizer.PostImportFinalizationUnavailable,
            match="did not acknowledge",
        ):
            post_import_finalizer.finalize_carrier_retirement(
                retired,
                item_id,
                authority=authority,
                acknowledge_completion=acknowledge,
            )

        unchanged = journal.load_queue_journal(retired.operation_id).journal
        assert unchanged is not None
        assert unchanged.retirements[0].completion_acknowledged is False
        assert carrier_calls == []

        settled, final_path, result = (
            post_import_finalizer.finalize_carrier_retirement(
                unchanged,
                item_id,
                authority=authority,
                acknowledge_completion=acknowledge,
            )
        )
    finally:
        authority.close()

    assert len(acknowledgements) == 2
    assert acknowledgements[0] == acknowledgements[1]
    assert acknowledgements[0][0] == CompletionOrigin(
        CompletionOriginKind.WEB_JOB,
        "web-job-1",
    )
    assert acknowledgements[0][1] == RecoveryOwner(
        retired.operation_id,
        item_id,
    )
    assert acknowledgements[0][2] == "1"
    assert len(acknowledgements[0][3]) == 64
    assert final_path is not None
    assert result is outcome
    assert carrier_calls == [item_id]
    assert settled.retirements == ()
