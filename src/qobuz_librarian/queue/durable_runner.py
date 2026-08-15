"""Crash-safe execution for exact full-album queue replacements."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qobuz_librarian.completion import (
    CompletionOrigin,
    RecoveryOwner,
    SourceTransitionKind,
    StagedBinding,
)
from qobuz_librarian.completion_live import capture_new_album_completion
from qobuz_librarian.download import (
    discard_download_staging,
    exact_download_coverage,
    refresh_staged_track_bindings,
    retain_download_staging,
    retire_download_staging_after_import,
    retire_empty_download_staging,
    run_album_download,
    staged_track_bindings,
    validated_staged_album_dirs,
)
from qobuz_librarian.integrations.beets import (
    ManagedCarrierRetirementOutcome,
    beets_import_managed,
    managed_completion_evidence,
    prepare_managed_staging_tags,
    reopen_managed_evidence,
)
from qobuz_librarian.integrations.rip import is_cancel_requested, snapshot_staging
from qobuz_librarian.integrations.staging import (
    StagingReferenceStatus,
    discard_file_group,
    discard_group,
    inspect_staging_group_reference,
    inspect_staging_run_reference,
    isolated_staging_run_names,
)
from qobuz_librarian.library.backup import (
    backup_album_dir,
    library_backup_record,
    load_backup_result,
    load_library_backup_record,
    pin_unverified_upgrade_backup,
    warn_pin_failed,
)
from qobuz_librarian.quality.verify import verify_and_recover
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue.durable_album import (
    DurableNewAlbumPlan,
    advance_completion_sources,
    completion_input_from_download,
    initial_completion_input,
    managed_binding_records,
    managed_completion_input,
    plan_durable_new_album,
)
from qobuz_librarian.queue.library_backup_recovery import (
    LibraryBackupPersistenceError,
    LibraryBackupResolutionStatus,
    finish_library_backup_settlement,
    prepare_library_backup_settlement,
)
from qobuz_librarian.queue.post_import_finalizer import (
    finalize_carrier_retirement,
    plan_post_import_action,
)
from qobuz_librarian.run_lock import RunLockLease

_STAGING_RUN = "download-staging-run"
_STAGING_GROUP = "staging-group"
_MANAGED_RESERVATION = "managed-beets-reservation"
_MANAGED_CARRIER = "managed-beets"
_LIBRARY_BACKUP_INTENT = "library-backup-intent"
_LIBRARY_BACKUP_CARRIER = "library-backup"
_LIBRARY_BACKUP_SETTLEMENT = "library-backup-settlement"


class DurableAlbumStatus(str, Enum):
    """Result of one journal-owned execution attempt."""

    COMPLETE = "complete"
    RETRY = "retry"
    ATTENTION = "attention"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DurableAlbumResult:
    status: DurableAlbumStatus
    reason: str | None = None
    post_dir: Path | None = None
    operation_id: str | None = None
    item_id: str | None = None


class DurableAlbumUnavailable(RuntimeError):
    """The durable namespace could not safely admit new mutation."""


def _require_authority(authority: RunLockLease) -> None:
    if type(authority) is not RunLockLease or not authority.intact():
        raise DurableAlbumUnavailable("run-lock authority is unavailable")


def _require_current_plan(item, args, plan: DurableNewAlbumPlan) -> None:
    if type(plan) is not DurableNewAlbumPlan or plan_durable_new_album(item, args) != plan:
        raise DurableAlbumUnavailable("the album no longer matches the durable download plan")


def _owner_record(owner: RecoveryOwner) -> dict[str, str]:
    return {
        "operation_id": owner.operation_id,
        "item_id": owner.item_id,
    }


def _load_exact(operation_id: str) -> queue_state.QueueJournal:
    loaded = queue_state.load_queue_journal(operation_id)
    if loaded.status is not queue_state.QueueLoadStatus.READY or loaded.journal is None:
        raise DurableAlbumUnavailable(
            loaded.reason or "the durable queue operation could not be loaded"
        )
    return loaded.journal


def _journal_item(journal, item_id):
    value = _find_journal_item(journal, item_id)
    if value is None:
        raise DurableAlbumUnavailable("the durable queue item disappeared")
    return value


def _find_journal_item(journal, item_id):
    return next(
        (item for item in journal.items if item.item_id == item_id),
        None,
    )


def _claim_pending_item(
    item,
    *,
    mode: str,
    authority: RunLockLease,
    resume_owner: RecoveryOwner | None = None,
):
    _require_authority(authority)
    if resume_owner is not None and type(resume_owner) is not RecoveryOwner:
        raise ValueError("resume_owner must be an exact RecoveryOwner")
    planned = queue_state._serialize_queue_item(item)
    loads = queue_state.list_queue_journals()
    _require_authority(authority)
    if any(load.status is queue_state.QueueLoadStatus.BLOCKED for load in loads):
        raise DurableAlbumUnavailable("saved queue recovery needs attention")
    journals = tuple(
        load.journal
        for load in loads
        if load.status is queue_state.QueueLoadStatus.READY and load.journal is not None
    )
    if any(journal.retirements for journal in journals):
        raise DurableAlbumUnavailable(
            "another durable queue item must be recovered before new work"
        )
    if resume_owner is not None:
        target_journal = next(
            (journal for journal in journals if journal.operation_id == resume_owner.operation_id),
            None,
        )
        target = (
            _find_journal_item(target_journal, resume_owner.item_id)
            if target_journal is not None
            else None
        )
        if (
            target_journal is None
            or target is None
            or target_journal.mode != mode
            or target.planned != planned
            or target.phase not in {queue_state.QueuePhase.PENDING, queue_state.QueuePhase.ACTIVE}
            or any(
                entry.phase is not queue_state.QueuePhase.PENDING
                for journal in journals
                for entry in journal.items
                if (
                    journal.operation_id != resume_owner.operation_id
                    or entry.item_id != resume_owner.item_id
                )
            )
        ):
            raise DurableAlbumUnavailable("the exact durable queue item cannot safely resume")
        return target_journal, target
    if any(journal.items for journal in journals):
        raise DurableAlbumUnavailable(
            "a saved durable queue item must be resumed by its exact identity"
        )
    journal = queue_state.create_queue_journal([item], mode=mode)
    _require_authority(authority)
    journal = queue_state.save_queue_journal(journal)
    _require_authority(authority)
    return journal, journal.items[0]


def _block(
    operation_id: str,
    item_id: str,
    reason: str,
    *,
    authority: RunLockLease,
):
    _require_authority(authority)
    journal = _load_exact(operation_id)
    item = _journal_item(journal, item_id)
    if item.phase is queue_state.QueuePhase.BLOCKED:
        return journal
    if item.phase not in {
        queue_state.QueuePhase.ACTIVE,
        queue_state.QueuePhase.RESOLVING,
    }:
        raise DurableAlbumUnavailable(f"cannot block durable item from {item.phase.value}")
    owner = {"operation_id": operation_id, "item_id": item_id}
    backup_references = tuple(
        reference
        for reference in item.recovery_references
        if reference.kind in {
            _LIBRARY_BACKUP_INTENT,
            _LIBRARY_BACKUP_CARRIER,
            _LIBRARY_BACKUP_SETTLEMENT,
        }
    )
    backup = None
    if len(backup_references) == 1:
        reference = backup_references[0]
        if reference.kind == _LIBRARY_BACKUP_INTENT:
            backup = load_backup_result(
                reference.data["path"],
                expected_owner=owner,
            )
        elif reference.kind == _LIBRARY_BACKUP_CARRIER:
            backup = load_library_backup_record(
                reference.data,
                expected_owner=owner,
            )
        else:
            backup = load_library_backup_record(
                reference.data["carrier"],
                expected_owner=owner,
            )
    _require_authority(authority)
    if backup is not None:
        pinned = pin_unverified_upgrade_backup(
            backup,
            "queue backup kept; durable work requires attention",
            expected_owner=owner,
        )
        _require_authority(authority)
        if not pinned:
            warn_pin_failed(backup.path)
    blocked = queue_state.transition_journal_item(
        journal,
        item_id,
        queue_state.QueuePhase.BLOCKED,
        block_reason=reason,
    )
    _require_authority(authority)
    return blocked


def _reset_unstarted(
    operation_id: str,
    item_id: str,
    *,
    authority: RunLockLease,
):
    _require_authority(authority)
    journal = _load_exact(operation_id)
    reset = queue_state.reset_unstarted_item_to_pending(journal, item_id)
    _require_authority(authority)
    return reset


def _staging_references(journal, item_id):
    item = _journal_item(journal, item_id)
    return tuple(
        reference
        for reference in item.recovery_references
        if reference.kind in {_STAGING_RUN, _STAGING_GROUP}
    )


def _reconcile_absent_staging(
    journal,
    item_id,
    owner,
    *,
    authority: RunLockLease,
):
    references = _staging_references(journal, item_id)
    if not references:
        return None
    owner_data = _owner_record(owner)
    for reference in references:
        _require_authority(authority)
        if reference.kind == _STAGING_RUN:
            inspected = inspect_staging_run_reference(reference.data, owner_data)
        else:
            inspected = inspect_staging_group_reference(reference.data, owner_data)
        _require_authority(authority)
        if inspected.status is not StagingReferenceStatus.ABSENT:
            return None
    reconciled = queue_state.reconcile_staging_references(
        journal,
        item_id,
        references,
    )
    _require_authority(authority)
    return reconciled


def _retry_or_attention_after_download(
    *,
    operation_id: str,
    item_id: str,
    result: dict,
    owner: RecoveryOwner,
    checkpoint_group,
    reason: str,
    authority: RunLockLease,
) -> DurableAlbumResult:
    # An empty run crossed the durable creation gate but contains no user data.
    # Retire and reconcile that exact root first so a transient pre-download
    # failure can remain an ordinary retry instead of a permanent block.
    try:
        _require_authority(authority)
        retired_empty = retire_empty_download_staging(
            result,
            recovery_checkpoint=checkpoint_group,
        )
        _require_authority(authority)
    except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
        retired_empty = False
    if not retired_empty:
        # A failed rip can leave nothing but redownloaded art (a failed
        # upgrade is the common case); that is not recoverable user data,
        # so dispose it and keep the item retryable instead of blocking.
        try:
            _require_authority(authority)
            retired_empty = retire_download_staging_after_import(
                result,
                recovery_checkpoint=checkpoint_group,
            )
            _require_authority(authority)
        except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
            retired_empty = False
    if retired_empty:
        journal = _load_exact(operation_id)
        reconciled = _reconcile_absent_staging(
            journal,
            item_id,
            owner,
            authority=authority,
        )
        if reconciled is not None:
            try:
                _require_authority(authority)
                queue_state.reset_unstarted_item_to_pending(reconciled, item_id)
                _require_authority(authority)
            except (OSError, ValueError, queue_state.QueueJournalError):
                pass
            else:
                return DurableAlbumResult(
                    DurableAlbumStatus.RETRY,
                    reason,
                    operation_id=operation_id,
                    item_id=item_id,
                )
    try:
        _require_authority(authority)
        retain_download_staging(
            result,
            label=reason,
            recovery_checkpoint=checkpoint_group,
        )
        _require_authority(authority)
    except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
        pass
    journal = _load_exact(operation_id)
    references = _staging_references(journal, item_id)
    if references:
        _block(operation_id, item_id, reason, authority=authority)
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            reason,
            operation_id=operation_id,
            item_id=item_id,
        )
    # A downloader can create and then reclaim an empty run without crossing
    # a durable mutation boundary. Only that exact initial state is retryable.
    try:
        _reset_unstarted(operation_id, item_id, authority=authority)
    except (OSError, ValueError, queue_state.QueueJournalError):
        _block(operation_id, item_id, reason, authority=authority)
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            reason,
            operation_id=operation_id,
            item_id=item_id,
        )
    return DurableAlbumResult(
        DurableAlbumStatus.RETRY,
        reason,
        operation_id=operation_id,
        item_id=item_id,
    )


def _cancel_after_download(
    *,
    operation_id: str,
    item_id: str,
    result: dict,
    owner: RecoveryOwner,
    checkpoint_group,
    authority: RunLockLease,
) -> DurableAlbumResult | None:
    # A cancel is a deliberate stop, not a crash: throw the partial download
    # away: the run root and any groups it parked, such as a rejected
    # broken track. Settle the journal item so the queue never waits on
    # recovery. Anything that cannot be proved settled falls back to the
    # blocking path.
    try:
        _require_authority(authority)
        discard_download_staging(result, recovery_checkpoint=checkpoint_group)
        _require_authority(authority)
    except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
        return None
    journal = _load_exact(operation_id)
    owner_data = _owner_record(owner)
    for reference in _staging_references(journal, item_id):
        if reference.kind != _STAGING_GROUP:
            continue
        _require_authority(authority)
        inspected = inspect_staging_group_reference(reference.data, owner_data)
        _require_authority(authority)
        if inspected.status is StagingReferenceStatus.ABSENT:
            continue
        if inspected.status is not StagingReferenceStatus.MATCH:
            return None
        path = reference.data.get("path")
        if not isinstance(path, str):
            return None
        _require_authority(authority)
        discarded = discard_group(
            Path(path), expected_owner=owner_data
        ) or discard_file_group(Path(path), expected_owner=owner_data)
        _require_authority(authority)
        if not discarded:
            return None
    if _staging_references(journal, item_id):
        journal = _reconcile_absent_staging(
            journal,
            item_id,
            owner,
            authority=authority,
        )
        if journal is None:
            return None
    if _journal_item(journal, item_id).recovery_references:
        return None
    try:
        _require_authority(authority)
        journal = queue_state.reset_unstarted_item_to_pending(journal, item_id)
        _require_authority(authority)
        queue_state.clear_queue_journal(operation_id, explicit_discard=True)
        _require_authority(authority)
    except (OSError, ValueError, queue_state.QueueJournalError):
        return None
    return DurableAlbumResult(
        DurableAlbumStatus.CANCELLED,
        "cancelled",
        operation_id=operation_id,
        item_id=item_id,
    )


def execute_durable_new_album(
    queue: list,
    item: dict,
    args,
    *,
    plan: DurableNewAlbumPlan,
    origin: CompletionOrigin,
    mode: str,
    authority: RunLockLease,
    resume_owner: RecoveryOwner | None = None,
    prepare_staged=None,
    acknowledge_completion=None,
) -> DurableAlbumResult:
    """Run one exact full-album replacement without ambiguous replay."""
    _require_authority(authority)
    _require_current_plan(item, args, plan)
    journal, journal_item = _claim_pending_item(
        item,
        mode=mode,
        authority=authority,
        resume_owner=resume_owner,
    )
    owner = RecoveryOwner(journal.operation_id, journal_item.item_id)
    initial = initial_completion_input(plan, owner, origin)
    if journal_item.phase is queue_state.QueuePhase.ACTIVE:
        if (
            journal_item.recovery_references
            or journal_item.block_reason is not None
            or journal_item.completion_evidence is not None
            or journal_item.completion_input != initial.to_record()
        ):
            raise DurableAlbumUnavailable(
                "the matching durable queue item cannot safely resume its download"
            )
    _require_authority(authority)
    if isolated_staging_run_names():
        raise DurableAlbumUnavailable("an unclaimed download staging run needs attention")
    if journal_item.phase is queue_state.QueuePhase.PENDING:
        journal = queue_state.transition_journal_item(
            journal,
            journal_item.item_id,
            queue_state.QueuePhase.ACTIVE,
            completion_input=initial,
            multi_artist_filing=bool(
                getattr(args, "migrate_multi_artist", False)
            ),
        )
        _require_authority(authority)
    operation_id = journal.operation_id
    item_id = journal_item.item_id
    backup_intent = None
    backup_carrier = None

    def checkpoint_group(record):
        nonlocal journal
        _require_authority(authority)
        if (
            type(record) is not dict
            or record.get("version") != 2
            or record.get("owner") != _owner_record(owner)
        ):
            raise ValueError("staging recovery checkpoint is malformed")
        journal = queue_state.append_staging_group_intent(
            journal,
            item_id,
            record,
        )
        _require_authority(authority)

    def checkpoint_backup_intent(record):
        nonlocal journal, backup_intent
        _require_authority(authority)
        if (
            type(record) is not dict
            or record.get("version") != 1
            or record.get("kind") not in {"upgrade", "gap-fill"}
            or record.get("kind") != plan.library_backup_kind
            or record.get("owner") != _owner_record(owner)
        ):
            raise ValueError("library backup intent is malformed")
        journal = queue_state.append_library_backup_intent(
            journal,
            item_id,
            record,
        )
        _require_authority(authority)
        matches = tuple(
            reference
            for reference in _journal_item(journal, item_id).recovery_references
            if reference.kind == _LIBRARY_BACKUP_INTENT
        )
        if len(matches) != 1:
            raise DurableAlbumUnavailable(
                "library backup intent was not committed exactly"
            )
        backup_intent = matches[0]

    def checkpoint_backup_carrier(payload):
        nonlocal journal, backup_carrier
        _require_authority(authority)
        if (
            type(payload) is not dict
            or set(payload) != {"version", "kind", "owner", "carrier"}
            or payload.get("version") != 1
            or payload.get("kind") != "library-backup-carrier"
            or payload.get("owner") != _owner_record(owner)
            or type(payload.get("carrier")) is not dict
            or backup_intent is None
        ):
            raise ValueError("library backup carrier is malformed")
        journal = queue_state.promote_library_backup_carrier(
            journal,
            item_id,
            backup_intent,
            payload["carrier"],
        )
        _require_authority(authority)
        matches = tuple(
            reference
            for reference in _journal_item(journal, item_id).recovery_references
            if reference.kind == _LIBRARY_BACKUP_CARRIER
        )
        if len(matches) != 1:
            raise DurableAlbumUnavailable(
                "library backup carrier was not committed exactly"
            )
        backup_carrier = matches[0]

    def checkpoint_staging(payload):
        nonlocal journal
        _require_authority(authority)
        if type(payload) is dict and payload.get("version") == 2:
            checkpoint_group(payload)
            return
        if (
            type(payload) is dict
            and payload.get("version") == 1
            and payload.get("kind") in {"upgrade", "gap-fill"}
        ):
            checkpoint_backup_intent(payload)
            return
        if (
            type(payload) is dict
            and payload.get("version") == 1
            and payload.get("kind") == "library-backup-carrier"
        ):
            checkpoint_backup_carrier(payload)
            return
        if (
            type(payload) is not dict
            or set(payload) != {"version", "kind", "owner", "record"}
            or payload.get("version") != 1
            or payload.get("kind") != "staging-run"
            or payload.get("owner") != _owner_record(owner)
            or type(payload.get("record")) is not dict
        ):
            raise ValueError("download staging checkpoint is malformed")
        journal = queue_state.append_staging_run_reference(
            journal,
            item_id,
            payload["record"],
        )
        _require_authority(authority)

    if plan.library_backup_kind == "upgrade":
        _require_authority(authority)
        _require_current_plan(item, args, plan)
        backup = backup_album_dir(
            Path(item["album_dir"]),
            expected_receipt=item.get("_validated_source_receipt"),
            owner=_owner_record(owner),
            on_intent=checkpoint_backup_intent,
        )
        _require_authority(authority)
        if backup is None:
            reason = (
                "library-backup-intent-unsettled"
                if backup_intent is not None
                else "library-backup-unavailable"
            )
            if backup_intent is None:
                _reset_unstarted(
                    operation_id,
                    item_id,
                    authority=authority,
                )
                return DurableAlbumResult(
                    DurableAlbumStatus.RETRY,
                    reason,
                    operation_id=operation_id,
                    item_id=item_id,
                )
            _block(operation_id, item_id, reason, authority=authority)
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                operation_id=operation_id,
                item_id=item_id,
            )
        carrier_record = library_backup_record(
            backup,
            expected_owner=_owner_record(owner),
        )
        if carrier_record is None:
            _require_authority(authority)
            pinned = pin_unverified_upgrade_backup(
                backup,
                "queue backup kept; its exact carrier could not be reopened",
                expected_owner=_owner_record(owner),
            )
            _require_authority(authority)
            if not pinned:
                warn_pin_failed(backup.path)
            reason = "library-backup-carrier-unavailable"
            _block(operation_id, item_id, reason, authority=authority)
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                operation_id=operation_id,
                item_id=item_id,
            )
        checkpoint_backup_carrier({
            "version": 1,
            "kind": "library-backup-carrier",
            "owner": _owner_record(owner),
            "carrier": carrier_record,
        })
        item["backup_path"] = backup
        if backup.complete is not True:
            _require_authority(authority)
            pinned = pin_unverified_upgrade_backup(
                backup,
                "queue backup kept; source retirement did not finish exactly",
                expected_owner=_owner_record(owner),
            )
            _require_authority(authority)
            if not pinned:
                warn_pin_failed(backup.path)
            reason = "library-backup-incomplete"
            _block(operation_id, item_id, reason, authority=authority)
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                operation_id=operation_id,
                item_id=item_id,
            )

    _require_authority(authority)
    item["snapshot_before"] = snapshot_staging()
    _require_authority(authority)
    _require_current_plan(item, args, plan)

    try:
        _require_authority(authority)
        _require_current_plan(item, args, plan)
        run_album_download(
            album=item["album"],
            missing=item["missing"],
            present=item["present"],
            album_dir=item.get("album_dir"),
            snapshot=item["snapshot_before"],
            existing=(
                None if plan.library_backup_kind == "gap-fill" else []
            ),
            quality=plan.effective_tier,
            upgrade_only=bool(item.get("upgrade_only")),
            force_track_by_track=False,
            result=item,
            recovery_owner=_owner_record(owner),
            recovery_checkpoint=checkpoint_staging,
            required_backup_kind=plan.library_backup_kind,
            expected_gap_fill_receipts=item.get(
                "_validated_gap_fill_receipts"
            ),
        )
        _require_authority(authority)
    except Exception:
        _retry_or_attention_after_download(
            operation_id=operation_id,
            item_id=item_id,
            result=item,
            owner=owner,
            checkpoint_group=checkpoint_group,
            reason="download-interrupted",
            authority=authority,
        )
        raise
    except BaseException:
        _retry_or_attention_after_download(
            operation_id=operation_id,
            item_id=item_id,
            result=item,
            owner=owner,
            checkpoint_group=checkpoint_group,
            reason="download-interrupted",
            authority=authority,
        )
        raise

    if plan.library_backup_kind is not None:
        live_backup = item.get(
            "backup_path"
            if plan.library_backup_kind == "upgrade"
            else "gap_fill_backup_path"
        )
        live_record = library_backup_record(
            live_backup,
            expected_owner=_owner_record(owner),
        )
        if (
            backup_carrier is None
            or live_record is None
            or live_record != backup_carrier.data
        ):
            reason = "library-backup-carrier-unsettled"
            _block(operation_id, item_id, reason, authority=authority)
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                operation_id=operation_id,
                item_id=item_id,
            )
    elif backup_intent is not None or backup_carrier is not None:
        reason = "unexpected-library-backup-state"
        _block(operation_id, item_id, reason, authority=authority)
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            reason,
            operation_id=operation_id,
            item_id=item_id,
        )

    coverage = exact_download_coverage(item, item["album"])
    ready_input = (
        completion_input_from_download(initial, coverage) if coverage is not None else None
    )
    if ready_input is None:
        if is_cancel_requested():
            settled = _cancel_after_download(
                operation_id=operation_id,
                item_id=item_id,
                result=item,
                owner=owner,
                checkpoint_group=checkpoint_group,
                authority=authority,
            )
            if settled is not None:
                return settled
        return _retry_or_attention_after_download(
            operation_id=operation_id,
            item_id=item_id,
            result=item,
            owner=owner,
            checkpoint_group=checkpoint_group,
            reason="download-incomplete",
            authority=authority,
        )
    _require_authority(authority)
    journal = queue_state.transition_journal_item(
        _load_exact(operation_id),
        item_id,
        queue_state.QueuePhase.ACTIVE,
        completion_input=ready_input,
    )
    _require_authority(authority)

    try:
        album_dirs = validated_staged_album_dirs(item)
        quality = verify_and_recover(
            item["album"],
            album_dirs,
            redownload_at_max=lambda: (),
            effective_tier=plan.effective_tier,
            allow_retry=False,
        )
        if quality["under"]:
            raise OSError("the staged album is below its selected quality")
        bindings = staged_track_bindings(item)
    except (OSError, TypeError, ValueError):
        return _retry_or_attention_after_download(
            operation_id=operation_id,
            item_id=item_id,
            result=item,
            owner=owner,
            checkpoint_group=checkpoint_group,
            reason="staged-album-unverified",
            authority=authority,
        )
    binding_records = managed_binding_records(ready_input)
    if binding_records is None or tuple(bindings) != binding_records:
        return _retry_or_attention_after_download(
            operation_id=operation_id,
            item_id=item_id,
            result=item,
            owner=owner,
            checkpoint_group=checkpoint_group,
            reason="staged-bindings-changed",
            authority=authority,
        )

    def checkpoint_sources(kind):
        nonlocal journal, ready_input, binding_records
        _require_authority(authority)
        if type(kind) is not SourceTransitionKind:
            raise ValueError("staged source transition is invalid")
        records = refresh_staged_track_bindings(item)
        try:
            current_bindings = tuple(
                StagedBinding(
                    record["slot"],
                    record["path"],
                    tuple(record["identity"]),
                )
                for record in records
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OSError("staged source transition is malformed") from exc
        advanced = advance_completion_sources(ready_input, current_bindings, kind)
        if advanced is None:
            raise OSError("staged source transition could not be proved")
        journal = queue_state.transition_journal_item(
            _load_exact(operation_id),
            item_id,
            queue_state.QueuePhase.ACTIVE,
            completion_input=advanced,
        )
        _require_authority(authority)
        ready_input = advanced
        binding_records = managed_binding_records(ready_input)
        if binding_records is None:
            raise OSError("staged source transition lost completion lineage")

    if prepare_staged is not None:
        if not callable(prepare_staged):
            raise ValueError("prepare_staged must be callable")
        try:
            _require_authority(authority)
            prepared = prepare_staged(album_dirs, checkpoint_sources)
            _require_authority(authority)
            try:
                lyric_sigs, resampled = prepared
            except (TypeError, ValueError) as exc:
                raise OSError("staged preparation result is malformed") from exc
            item["_durable_lyric_sigs"] = lyric_sigs
            item["resampled_n"] = resampled
            item["downsample_errors"] = getattr(prepared, "errors", 0)
            item["downsample_saved_bytes"] = getattr(
                prepared, "saved_bytes", 0
            )
            item["downsample_flush_warnings"] = getattr(
                prepared, "flush_warnings", 0
            )
            item["downsample_cancelled"] = bool(
                getattr(prepared, "cancelled", False)
            )
            if validated_staged_album_dirs(item) != album_dirs:
                raise OSError("staged album roots changed during preparation")
            current_records = tuple(staged_track_bindings(item))
            if binding_records is None or current_records != binding_records:
                raise OSError("prepared staged bindings were not checkpointed")
        except Exception:
            return _retry_or_attention_after_download(
                operation_id=operation_id,
                item_id=item_id,
                result=item,
                owner=owner,
                checkpoint_group=checkpoint_group,
                reason="staged-preparation-unsettled",
                authority=authority,
            )
        except BaseException:
            _retry_or_attention_after_download(
                operation_id=operation_id,
                item_id=item_id,
                result=item,
                owner=owner,
                checkpoint_group=checkpoint_group,
                reason="staged-preparation-interrupted",
                authority=authority,
            )
            raise

    try:
        cleaned_bindings = prepare_managed_staging_tags(
            album_dirs,
            binding_records,
            authority_check=lambda: _require_authority(authority),
        )
        if tuple(cleaned_bindings) != binding_records:
            checkpoint_sources(SourceTransitionKind.BEETS_TAG_CLEAN)
            if tuple(cleaned_bindings) != binding_records:
                raise OSError("tag-clean source checkpoint changed")
    except Exception:
        return _retry_or_attention_after_download(
            operation_id=operation_id,
            item_id=item_id,
            result=item,
            owner=owner,
            checkpoint_group=checkpoint_group,
            reason="staged-tag-clean-unsettled",
            authority=authority,
        )
    except BaseException:
        _retry_or_attention_after_download(
            operation_id=operation_id,
            item_id=item_id,
            result=item,
            owner=owner,
            checkpoint_group=checkpoint_group,
            reason="staged-tag-clean-interrupted",
            authority=authority,
        )
        raise

    reservation = None
    carrier = None

    def checkpoint_reservation(payload):
        nonlocal journal, reservation
        _require_authority(authority)
        if (
            type(payload) is not dict
            or set(payload) != {"version", "kind", "owner", "reservation"}
            or payload.get("version") != 1
            or payload.get("kind") != _MANAGED_RESERVATION
            or payload.get("owner") != _owner_record(owner)
            or type(payload.get("reservation")) is not dict
        ):
            raise ValueError("managed import reservation is malformed")
        reference = queue_state.RecoveryReference(
            "managed-import",
            _MANAGED_RESERVATION,
            payload["reservation"],
        )
        journal = queue_state.reserve_managed_carrier(
            journal,
            item_id,
            reference,
        )
        _require_authority(authority)
        reservation = reference

    def checkpoint_carrier(payload):
        nonlocal journal, carrier
        _require_authority(authority)
        if (
            type(payload) is not dict
            or set(payload) != {"version", "kind", "owner", "carrier"}
            or payload.get("version") != 1
            or payload.get("kind") != _MANAGED_CARRIER
            or payload.get("owner") != _owner_record(owner)
            or type(payload.get("carrier")) is not dict
            or reservation is None
        ):
            raise ValueError("managed import carrier is malformed")
        reference = queue_state.RecoveryReference(
            "managed-import",
            _MANAGED_CARRIER,
            payload["carrier"],
        )
        journal = queue_state.promote_managed_carrier(
            journal,
            item_id,
            reservation,
            reference,
            ready_input,
        )
        _require_authority(authority)
        carrier = reference

    try:
        _require_authority(authority)
        imported = beets_import_managed(
            album_dirs,
            binding_records,
            owner=_owner_record(owner),
            on_reservation=checkpoint_reservation,
            on_intent=checkpoint_carrier,
            authority_check=lambda: _require_authority(authority),
        )
        _require_authority(authority)
    except BaseException:
        _block(
            operation_id,
            item_id,
            "managed-import-interrupted",
            authority=authority,
        )
        raise

    journal = _load_exact(operation_id)
    current = _journal_item(journal, item_id)
    persisted_carriers = tuple(
        reference for reference in current.recovery_references if reference.kind == _MANAGED_CARRIER
    )
    managed = managed_completion_evidence(imported)
    sealed_input = managed_completion_input(ready_input, managed) if managed is not None else None
    if (
        imported.kind != "ok"
        or imported.spawned is not True
        or managed is None
        or sealed_input != ready_input
        or carrier is None
        or persisted_carriers != (carrier,)
        or imported.carrier != carrier.data
    ):
        _block(
            operation_id,
            item_id,
            "managed-import-unsettled",
            authority=authority,
        )
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            "managed-import-unsettled",
            operation_id=operation_id,
            item_id=item_id,
        )

    try:
        _require_authority(authority)
        retired = retire_empty_download_staging(
            item,
            recovery_checkpoint=checkpoint_group,
        )
        _require_authority(authority)
    except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
        retired = False
    if not retired:
        try:
            _require_authority(authority)
            retired = retire_download_staging_after_import(
                item,
                recovery_checkpoint=checkpoint_group,
            )
            _require_authority(authority)
        except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
            retired = False
    journal = _load_exact(operation_id)
    journal = (
        _reconcile_absent_staging(
            journal,
            item_id,
            owner,
            authority=authority,
        )
        if retired
        else None
    )
    if journal is None:
        _block(
            operation_id,
            item_id,
            "download-staging-unsettled",
            authority=authority,
        )
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            "download-staging-unsettled",
            operation_id=operation_id,
            item_id=item_id,
        )

    current = _journal_item(journal, item_id)
    expected_references = (
        (backup_carrier, carrier)
        if backup_carrier is not None
        else (carrier,)
    )
    if current.recovery_references != expected_references:
        _block(
            operation_id,
            item_id,
            "recovery-reference-unsettled",
            authority=authority,
        )
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            "recovery-reference-unsettled",
            operation_id=operation_id,
            item_id=item_id,
        )

    post_dir = Path(managed.library_root) / managed.album_path
    backup_resolution = prepare_library_backup_settlement(
        journal,
        item_id,
        owner,
        item["album"],
        post_dir,
        authority_check=lambda: _require_authority(authority),
    )
    if backup_resolution.status is LibraryBackupResolutionStatus.ATTENTION:
        reason = backup_resolution.reason or "library-backup-unsettled"
        _block(operation_id, item_id, reason, authority=authority)
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            reason,
            post_dir,
            operation_id,
            item_id,
        )
    journal = backup_resolution.journal

    refreshed_managed = managed_completion_evidence(imported)
    stable_managed = (
        refreshed_managed is not None
        and refreshed_managed.owner == managed.owner
        and refreshed_managed.library_root == managed.library_root
        and refreshed_managed.library_root_identity
            == managed.library_root_identity
        and refreshed_managed.album_path == managed.album_path
        and refreshed_managed.manifest_hash == managed.manifest_hash
        and refreshed_managed.mappings == managed.mappings
    )
    if not stable_managed or (
        plan.library_backup_kind != "upgrade"
        and refreshed_managed != managed
    ):
        reason = "managed-completion-changed-during-backup-resolution"
        _block(operation_id, item_id, reason, authority=authority)
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            reason,
            post_dir,
            operation_id,
            item_id,
        )
    managed = refreshed_managed
    post_dir = Path(managed.library_root) / managed.album_path
    try:
        _require_authority(authority)
        backup_resolution = finish_library_backup_settlement(
            journal,
            item_id,
            owner,
            item["album"],
            authority_check=lambda: _require_authority(authority),
        )
        if (
            backup_resolution.status
            is LibraryBackupResolutionStatus.ATTENTION
        ):
            reason = (
                backup_resolution.reason
                or "library-backup-settlement-unavailable"
            )
            _block(
                operation_id,
                item_id,
                reason,
                authority=authority,
            )
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                post_dir,
                operation_id,
                item_id,
            )
        journal = backup_resolution.journal
        settled_managed = managed_completion_evidence(imported)
        if settled_managed != managed:
            reason = "managed-completion-changed-during-backup-resolution"
            _block(operation_id, item_id, reason, authority=authority)
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                post_dir,
                operation_id,
                item_id,
            )
        managed = settled_managed
        post_dir = Path(managed.library_root) / managed.album_path
        with reopen_managed_evidence(carrier.data, owner) as managed_lease:
            reopened = managed_lease.revalidate()
            if reopened != managed:
                raise DurableAlbumUnavailable("managed completion changed before publication")
            with capture_new_album_completion(
                managed_lease=managed_lease,
                owner=owner,
                expectation=ready_input.expectation,
                download=ready_input.download_coverage(),
            ) as live_lease:
                evidence = live_lease.revalidate()
                _require_authority(authority)
                managed_lease.revalidate()
                live_lease.revalidate()
                _require_authority(authority)
                journal = queue_state.transition_journal_item(
                    journal,
                    item_id,
                    queue_state.QueuePhase.COMPLETE,
                    completion_evidence=evidence.to_record(),
                )
                _require_authority(authority)
                if managed_lease.revalidate() != managed:
                    raise DurableAlbumUnavailable("managed completion changed during publication")
                live_lease.revalidate()
                _require_authority(authority)
                post_import_action = plan_post_import_action(
                    journal,
                    item_id,
                    post_dir,
                    authority=authority,
                )
                _require_authority(authority)
                live_lease.revalidate()
                journal = queue_state.commit_completed_item_removal(
                    journal,
                    queue,
                    item_id=item_id,
                    caller_item=item,
                    live_evidence=evidence,
                    post_import_action=post_import_action,
                )
                _require_authority(authority)
                live_lease.revalidate()
    except LibraryBackupPersistenceError:
        raise
    except Exception:
        # The queue-removal commit deliberately replaces the item with a
        # retirement record.  A final lease check can therefore fail after the
        # item is already, correctly, absent; recovery must preserve that
        # committed state instead of treating the absence as corruption.
        current = _find_journal_item(_load_exact(operation_id), item_id)
        if current is not None and current.phase is queue_state.QueuePhase.RESOLVING:
            _block(
                operation_id,
                item_id,
                "completion-proof-unavailable",
                authority=authority,
            )
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            "completion-proof-unavailable",
            post_dir,
            operation_id,
            item_id,
        )

    try:
        _require_authority(authority)
        journal, final_path, retirement = finalize_carrier_retirement(
            journal,
            item_id,
            authority=authority,
            acknowledge_completion=acknowledge_completion,
        )
        _require_authority(authority)
    except (OSError, ValueError, queue_state.QueueJournalError):
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            "managed-carrier-retirement-unsettled",
            post_dir,
            operation_id,
            item_id,
        )
    if final_path is not None:
        post_dir = final_path
    if retirement.outcome not in {
        ManagedCarrierRetirementOutcome.RETIRED,
        ManagedCarrierRetirementOutcome.ALREADY_ABSENT,
    }:
        return DurableAlbumResult(
            DurableAlbumStatus.ATTENTION,
            "managed-carrier-retirement-unsettled",
            post_dir,
            operation_id,
            item_id,
        )
    if not journal.items and not journal.retirements:
        try:
            _require_authority(authority)
            queue_state.clear_queue_journal(operation_id)
            _require_authority(authority)
        except (OSError, ValueError, queue_state.QueueJournalError):
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                "completed-journal-cleanup-unsettled",
                post_dir,
                operation_id,
                item_id,
            )
    return DurableAlbumResult(
        DurableAlbumStatus.COMPLETE,
        post_dir=post_dir,
        operation_id=operation_id,
        item_id=item_id,
    )
