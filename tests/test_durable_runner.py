from argparse import Namespace
from contextlib import contextmanager

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.completion import (
    AlbumInventory,
    CompletionOrigin,
    CompletionOriginKind,
    DownloadCounts,
    DownloadCoverage,
    LandingReceipt,
    ManagedImportEvidence,
    ManagedMapping,
    RecoveryOwner,
    StagedBinding,
    TrackQuality,
    assess_completion,
)
from qobuz_librarian.integrations import beets as beets_module
from qobuz_librarian.integrations.beets import (
    ManagedBeetsImportResult,
    ManagedCarrierRetirementOutcome,
    ManagedCarrierRetirementResult,
)
from qobuz_librarian.integrations.staging import (
    StagingReferenceInspection,
    StagingReferenceStatus,
)
from qobuz_librarian.queue import durable_runner
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.durable_album import (
    plan_durable_new_album,
)


@pytest.fixture
def authority(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "LOCK_FILE", tmp_path / "run.lock")
    lease = run_lock.acquire()
    try:
        yield lease
    finally:
        lease.close()


def _single_track_item(label):
    track = {"id": "101", "media_number": 1, "track_number": 1}
    return _build_queue_item(
        album={
            "id": "42",
            "title": label,
            "maximum_bit_depth": 24,
            "maximum_sampling_rate": 96,
            "tracks": {"items": [track]},
        },
        album_dir=None,
        label=label,
        missing=[track],
        present=[],
        upgrade_only=False,
        auto_upgrade=False,
        quality=4,
    )




def test_download_quarantine_checkpoint_is_persisted_before_error_propagates(
    tmp_path, monkeypatch, authority
):
    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    item = _single_track_item("Interrupted Album")
    args = Namespace(no_import=False)
    plan = plan_durable_new_album(item, args)
    assert plan is not None

    class DownloadStopped(RuntimeError):
        pass

    monkeypatch.setattr(durable_runner, "snapshot_staging", lambda: set())

    def stop_after_quarantine(**kwargs):
        journal = queue_state.list_queue_journals()[0].journal
        owner = journal.items[0].completion_input["owner"]
        kwargs["recovery_checkpoint"](
            {
                "version": 1,
                "kind": "staging-run",
                "owner": owner,
                "record": {
                    "version": 2,
                    "path": str(staging / (".qobuz-run-" + "a" * 24)),
                    "root_identity": [1, 10, 0o40700, 0, 1, 2],
                    "owner": owner,
                },
            }
        )
        kwargs["recovery_checkpoint"](
            {
                "version": 2,
                "kind": "rejected",
                "path": str(staging / cfg.BEETS_RETRY_DIR / "rejected.flac"),
                "owner": owner,
                "sealed_manifest": {
                    "identity": [1, 12, 0o100600, 20, 21, 22],
                    "sha256": "d" * 64,
                },
            }
        )
        raise DownloadStopped("network stopped")

    monkeypatch.setattr(durable_runner, "run_album_download", stop_after_quarantine)
    monkeypatch.setattr(durable_runner, "retire_empty_download_staging", lambda *a, **k: False)
    monkeypatch.setattr(durable_runner, "retain_download_staging", lambda *a, **k: True)

    with pytest.raises(DownloadStopped, match="network stopped"):
        durable_runner.execute_durable_new_album(
            [item],
            item,
            args,
            plan=plan,
            origin=CompletionOrigin(CompletionOriginKind.CLI, "album queue"),
            mode="cli:album",
            authority=authority,
        )

    journal = queue_state.list_queue_journals()[0].journal
    saved = journal.items[0]
    assert saved.phase is queue_state.QueuePhase.BLOCKED
    assert tuple(ref.kind for ref in saved.recovery_references) == (
        "download-staging-run",
        "staging-group",
    )


def test_cancelled_download_discards_staging_and_clears_journal(
    tmp_path, monkeypatch, authority
):
    # A user cancel is deliberate: the partial rip is thrown away, including
    # a group parked for a rejected broken track, and the journal item is
    # settled, so later downloads never wait on a restart.
    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    item = _single_track_item("Cancelled Album")
    args = Namespace(no_import=False)
    plan = plan_durable_new_album(item, args)
    assert plan is not None

    monkeypatch.setattr(durable_runner, "snapshot_staging", lambda: set())
    rejected_path = str(staging / cfg.BEETS_RETRY_DIR / ".rejected-cafe")

    def cancelled_download(**kwargs):
        journal = queue_state.list_queue_journals()[0].journal
        owner = journal.items[0].completion_input["owner"]
        kwargs["result"]["_staging_run"] = {
            "version": 2,
            "path": str(staging / (".qobuz-run-" + "a" * 24)),
            "root_identity": [1, 10, 0o40700, 0, 1, 2],
            "owner": owner,
        }
        kwargs["recovery_checkpoint"](
            {
                "version": 1,
                "kind": "staging-run",
                "owner": owner,
                "record": kwargs["result"]["_staging_run"],
            }
        )
        kwargs["recovery_checkpoint"](
            {
                "version": 2,
                "kind": "rejected",
                "path": rejected_path,
                "owner": owner,
                "sealed_manifest": {
                    "identity": [1, 12, 0o100600, 20, 21, 22],
                    "sha256": "d" * 64,
                },
            }
        )

    run_discarded = []
    group_discards = []
    monkeypatch.setattr(durable_runner, "run_album_download", cancelled_download)
    monkeypatch.setattr(durable_runner, "is_cancel_requested", lambda: True)
    monkeypatch.setattr(
        durable_runner,
        "discard_download_staging",
        lambda result, **kwargs: run_discarded.append(result) or True,
    )
    monkeypatch.setattr(durable_runner, "discard_group", lambda *a, **k: False)
    monkeypatch.setattr(
        durable_runner,
        "discard_file_group",
        lambda path, **kwargs: group_discards.append(str(path)) or True,
    )
    absent = StagingReferenceInspection(StagingReferenceStatus.ABSENT)
    present = StagingReferenceInspection(StagingReferenceStatus.MATCH)
    monkeypatch.setattr(durable_runner, "inspect_staging_run_reference", lambda *_: absent)
    monkeypatch.setattr(
        durable_runner,
        "inspect_staging_group_reference",
        lambda *_: absent if group_discards else present,
    )

    result = durable_runner.execute_durable_new_album(
        [item],
        item,
        args,
        plan=plan,
        origin=CompletionOrigin(CompletionOriginKind.CLI, "album queue"),
        mode="cli:album",
        authority=authority,
    )

    assert result.status is durable_runner.DurableAlbumStatus.CANCELLED
    assert run_discarded == [item]
    assert group_discards == [rejected_path]
    assert queue_state.list_queue_journals() == ()


def test_failed_download_leaving_only_art_stays_retryable(
    tmp_path, monkeypatch, authority
):
    # A failed rip that leaves nothing but redownloaded art (a failed upgrade
    # is the common case) is disposed and retried instead of blocking the
    # queue for recovery.
    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    item = _single_track_item("Art Only Album")
    args = Namespace(no_import=False)
    plan = plan_durable_new_album(item, args)
    assert plan is not None

    monkeypatch.setattr(durable_runner, "snapshot_staging", lambda: set())

    def incomplete_download(**kwargs):
        journal = queue_state.list_queue_journals()[0].journal
        owner = journal.items[0].completion_input["owner"]
        kwargs["recovery_checkpoint"](
            {
                "version": 1,
                "kind": "staging-run",
                "owner": owner,
                "record": {
                    "version": 2,
                    "path": str(staging / (".qobuz-run-" + "a" * 24)),
                    "root_identity": [1, 10, 0o40700, 0, 1, 2],
                    "owner": owner,
                },
            }
        )

    disposed = []
    monkeypatch.setattr(durable_runner, "run_album_download", incomplete_download)
    monkeypatch.setattr(
        durable_runner, "retire_empty_download_staging", lambda *a, **k: False
    )
    monkeypatch.setattr(
        durable_runner,
        "retire_download_staging_after_import",
        lambda result, **kwargs: disposed.append(result) or True,
    )
    absent = StagingReferenceInspection(StagingReferenceStatus.ABSENT)
    monkeypatch.setattr(durable_runner, "inspect_staging_run_reference", lambda *_: absent)
    monkeypatch.setattr(durable_runner, "inspect_staging_group_reference", lambda *_: absent)

    result = durable_runner.execute_durable_new_album(
        [item],
        item,
        args,
        plan=plan,
        origin=CompletionOrigin(CompletionOriginKind.CLI, "album queue"),
        mode="cli:album",
        authority=authority,
    )

    assert result.status is durable_runner.DurableAlbumStatus.RETRY
    assert disposed == [item]
    saved = queue_state.list_queue_journals()[0].journal.items[0]
    assert saved.phase is queue_state.QueuePhase.PENDING
    assert saved.recovery_references == ()


def test_durable_album_removes_queue_only_under_live_completion_proof(
    tmp_path, monkeypatch, authority
):
    staging = tmp_path / "staging"
    beets_dir = tmp_path / "beets"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "QUEUE_JOURNAL_DIR", tmp_path / "journals")
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", beets_dir / "library.db")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")

    item = _single_track_item("Safe Album")
    queue = [item]
    args = Namespace(no_import=False)
    plan = plan_durable_new_album(item, args)
    assert plan is not None

    slot = plan.expectation.catalogue_slots[0]
    run_root = staging / (".qobuz-run-" + "a" * 24)
    source_path = str(run_root / "Safe Album" / "01.flac")
    source_identity = (1, 2, 0o100600, 100, 200, 300)
    binding = StagedBinding(slot, source_path, source_identity)
    coverage = DownloadCoverage(
        album_id="42",
        catalogue_slots=(slot,),
        requested_slots=(slot,),
        bindings=(binding,),
        counts=DownloadCounts(),
    )
    observed = {"live_queue": [], "active_before_download": False}
    managed_holder = {}

    monkeypatch.setattr(durable_runner, "snapshot_staging", lambda: set())

    def fake_download(**kwargs):
        loads = queue_state.list_queue_journals()
        active = loads[0].journal.items[0]
        observed["active_before_download"] = (
            active.phase is queue_state.QueuePhase.ACTIVE
            and active.completion_input is not None
            and queue == [item]
        )
        owner = active.completion_input["owner"]
        kwargs["recovery_checkpoint"](
            {
                "version": 1,
                "kind": "staging-run",
                "owner": owner,
                "record": {
                    "version": 2,
                    "path": str(staging / (".qobuz-run-" + "a" * 24)),
                    "root_identity": [1, 10, 0o40700, 0, 1, 2],
                    "owner": owner,
                },
            }
        )

    monkeypatch.setattr(durable_runner, "run_album_download", fake_download)
    monkeypatch.setattr(durable_runner, "exact_download_coverage", lambda *_: coverage)
    monkeypatch.setattr(
        durable_runner, "validated_staged_album_dirs", lambda *_: [staging / "Safe Album"]
    )
    monkeypatch.setattr(
        durable_runner,
        "verify_and_recover",
        lambda *args, **kwargs: {"under": False},
    )
    monkeypatch.setattr(
        durable_runner,
        "staged_track_bindings",
        lambda *_: [{"slot": slot, "path": source_path, "identity": list(source_identity)}],
    )
    monkeypatch.setattr(
        durable_runner,
        "prepare_managed_staging_tags",
        lambda _roots, bindings, *, authority_check: authority_check() or tuple(bindings),
    )

    nonce = "b" * 64

    def fake_import(
        _album_dirs,
        bindings,
        *,
        owner,
        on_reservation,
        on_intent,
        authority_check,
    ):
        authority_check()
        reservation = {
            "version": 1,
            "path": str(beets_dir / f".qobuz-managed-beets-{nonce}.jsonl"),
            "parent_device": 4,
            "parent_inode": 5,
            "nonce": nonce,
            "owner": owner,
        }
        carrier = {
            "version": 2,
            "path": reservation["path"],
            "device": 6,
            "inode": 7,
            "parent_device": 4,
            "parent_inode": 5,
            "nonce": nonce,
            "owner": owner,
        }
        on_reservation(
            {
                "version": 1,
                "kind": "managed-beets-reservation",
                "owner": owner,
                "reservation": reservation,
            }
        )
        on_intent(
            {
                "version": 1,
                "kind": "managed-beets",
                "owner": owner,
                "carrier": carrier,
            }
        )
        managed = ManagedImportEvidence(
            owner=RecoveryOwner(owner["operation_id"], owner["item_id"]),
            library_root=str(tmp_path / "music"),
            library_root_identity=(8, 9, 0, 10, 11),
            album_path="Artist/Safe Album",
            album_identity=(8, 10, 0, 12, 13),
            manifest_hash="c" * 64,
            mappings=(
                ManagedMapping(
                    slot,
                    bindings[0]["path"],
                    tuple(bindings[0]["identity"]),
                    "Artist/Safe Album/01.flac",
                    (8, 11, 100, 14, 15),
                ),
            ),
        )
        managed_holder["value"] = managed
        return ManagedBeetsImportResult(
            "ok", carrier, evidence={"sealed": True}, spawned=True, reservation=reservation
        )

    monkeypatch.setattr(durable_runner, "beets_import_managed", fake_import)
    monkeypatch.setattr(
        durable_runner,
        "managed_completion_evidence",
        lambda _result: managed_holder["value"],
    )

    retirement_attempts = []

    def fake_retire_after_import(_result, *, recovery_checkpoint):
        retirement_attempts.append("after-import")
        journal = queue_state.list_queue_journals()[0].journal
        owner = journal.items[0].completion_input["owner"]
        recovery_checkpoint(
            {
                "version": 2,
                "kind": "interrupted",
                "path": str(staging / cfg.BEETS_RETRY_DIR / "owned"),
                "owner": owner,
                "sealed_manifest": {
                    "identity": [1, 12, 0o100600, 20, 21, 22],
                    "sha256": "d" * 64,
                },
            }
        )
        return True

    def fake_retire_empty(_result, *, recovery_checkpoint):
        retirement_attempts.append("empty")
        return False

    monkeypatch.setattr(
        durable_runner, "retire_empty_download_staging", fake_retire_empty
    )
    monkeypatch.setattr(
        durable_runner,
        "retire_download_staging_after_import",
        fake_retire_after_import,
    )
    absent = StagingReferenceInspection(StagingReferenceStatus.ABSENT)
    monkeypatch.setattr(durable_runner, "inspect_staging_run_reference", lambda *_: absent)
    monkeypatch.setattr(durable_runner, "inspect_staging_group_reference", lambda *_: absent)

    class ManagedLease:
        def revalidate(self):
            return managed_holder["value"]

    @contextmanager
    def fake_reopen(*_args, **_kwargs):
        yield ManagedLease()

    monkeypatch.setattr(durable_runner, "reopen_managed_evidence", fake_reopen)

    class LiveLease:
        def __init__(self, evidence):
            self.evidence = evidence

        def revalidate(self):
            observed["live_queue"].append(item in queue)
            return self.evidence

    @contextmanager
    def fake_capture(*, expectation, download, **_kwargs):
        managed = managed_holder["value"]
        mapping = managed.mappings[0]
        landing = LandingReceipt(
            mapping.slot,
            mapping.destination_path,
            mapping.destination_identity,
            "e" * 64,
        )
        target = expectation.quality_targets[0]
        assessment = assess_completion(
            owner=managed.owner,
            expectation=expectation,
            download=download,
            managed=managed,
            landings=(landing,),
            inventory=AlbumInventory(
                managed.album_path,
                managed.album_identity,
                (landing,),
            ),
            quality=(
                TrackQuality(
                    mapping.slot,
                    mapping.destination_path,
                    mapping.destination_identity,
                    landing.sha256,
                    target.bits,
                    target.rate,
                ),
            ),
        )
        assert assessment.evidence is not None
        yield LiveLease(assessment.evidence)

    monkeypatch.setattr(durable_runner, "capture_new_album_completion", fake_capture)
    monkeypatch.setattr(
        beets_module,
        "retire_managed_carrier",
        lambda *_args, **_kwargs: ManagedCarrierRetirementResult(
            ManagedCarrierRetirementOutcome.ALREADY_ABSENT
        ),
    )

    class Prepared:
        errors = 2
        saved_bytes = 512
        flush_warnings = 1
        cancelled = True

        def __iter__(self):
            yield []
            yield 1

    result = durable_runner.execute_durable_new_album(
        queue,
        item,
        args,
        plan=plan,
        origin=CompletionOrigin(CompletionOriginKind.CLI, "album queue"),
        mode="album",
        authority=authority,
        prepare_staged=lambda _dirs, _checkpoint: Prepared(),
    )

    assert result.status is durable_runner.DurableAlbumStatus.COMPLETE
    assert retirement_attempts == ["empty", "after-import"]
    assert observed["active_before_download"] is True
    assert observed["live_queue"] and observed["live_queue"][0] is True
    assert queue == []
    assert queue_state.list_queue_journals() == ()
    assert item["resampled_n"] == 1
    assert item["downsample_errors"] == 2
    assert item["downsample_saved_bytes"] == 512
    assert item["downsample_flush_warnings"] == 1
    assert item["downsample_cancelled"] is True
