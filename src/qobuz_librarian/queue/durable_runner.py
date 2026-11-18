"""Crash-safe execution for exact full-album queue replacements."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qobuz_librarian import run_lock
from qobuz_librarian.completion import (
    CompletionOrigin,
    RecoveryOwner,
    SourceTransitionKind,
    StagedBinding,
)
from qobuz_librarian.completion_live import capture_new_album_completion
from qobuz_librarian.download import (
    clear_attempts_after_import,
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
from qobuz_librarian.quality.verify import rip_shortfall, verify_and_recover
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue import startup_recovery
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
    restore_library_backup,
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
    INCOMPLETE = "incomplete"


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
    run_lock.require_authority(
        authority, DurableAlbumUnavailable("run-lock authority is unavailable"))


def _require_current_plan(item, args, plan: DurableNewAlbumPlan) -> None:
    if type(plan) is not DurableNewAlbumPlan or plan_durable_new_album(item, args) != plan:
        raise DurableAlbumUnavailable("the album no longer matches the durable download plan")


def _owner_record(owner: RecoveryOwner) -> dict[str, str]:
    return owner.to_record()


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
            raise DurableAlbumUnavailable("the exact durable queue item cannot resume")
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


def _restore_library_backup(operation_id, item_id, owner, result, *, authority):
    _require_authority(authority)
    restored = restore_library_backup(
        _load_exact(operation_id),
        item_id,
        owner,
        authority_check=lambda: _require_authority(authority),
    )
    if restored.status is LibraryBackupResolutionStatus.SETTLED:
        result["backup_path"] = result["gap_fill_backup_path"] = None
    return restored


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
    # Nothing reached Beets, so tracks moved aside for this download go back
    # whatever becomes of the staged part. A restore that cannot finish keeps
    # the backup and the block, and settling the block tries it again.
    try:
        restored = _restore_library_backup(
            operation_id, item_id, owner, result, authority=authority)
    except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
        restored = None
    if (
        restored is not None
        and restored.status is LibraryBackupResolutionStatus.ATTENTION
    ):
        reason = restored.reason
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
    # broken track, and put back any tracks it moved aside. Settle the
    # journal item so the queue never waits on recovery. Anything that
    # cannot be proved settled falls back to the blocking path.
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
    try:
        restored = _restore_library_backup(
            operation_id, item_id, owner, result, authority=authority)
    except (OSError, ValueError, queue_state.QueueJournalError):
        return None
    if restored.status is LibraryBackupResolutionStatus.ATTENTION:
        return None
    journal = restored.journal
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


def _abandon_blocked_item(
    *,
    operation_id: str,
    item_id: str,
    result: dict,
    reason: str,
    fallback: DurableAlbumResult,
    authority: RunLockLease,
) -> DurableAlbumResult:
    """Discard a download that blocked and cannot be retried into anything new.

    A block pauses every download and scan until someone settles it, and the
    rip that caused this one already ran to its own end, so a retry repeats the
    same request. Throw the partial away and clear the recovery instead of
    waiting for a person to notice. This is the same settlement the interface
    offers by hand, so a library backup that cannot be put back still keeps
    the item blocked rather than losing what it holds. Once settled, the
    result gives ``reason``, what stopped the download, even when the block
    was for a put-back that has since worked.
    """
    try:
        settled = startup_recovery.settle_blocked_item(
            authority=authority,
            operation_id=operation_id,
            item_id=item_id,
            action=startup_recovery.BlockedItemSettlementAction.DISCARD,
        )
    except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
        return fallback
    if settled.status is not startup_recovery.BlockedItemSettlementStatus.DISCARDED:
        return fallback
    result["backup_path"] = result["gap_fill_backup_path"] = None
    return DurableAlbumResult(
        DurableAlbumStatus.INCOMPLETE,
        reason,
        operation_id=operation_id,
        item_id=item_id,
    )


class _DurableAlbumRun:
    """One album's journal-owned download, import and completion."""

    def __init__(self, queue, item, args, *, plan, origin, mode, authority,
                 resume_owner, prepare_staged, acknowledge_completion):
        self.queue = queue
        self.item = item
        self.args = args
        self.plan = plan
        self.origin = origin
        self.mode = mode
        self.authority = authority
        self.resume_owner = resume_owner
        self.prepare_staged = prepare_staged
        self.acknowledge_completion = acknowledge_completion
        self.backup_intent = None
        self.backup_carrier = None
        self.reservation = None
        self.carrier = None

    def run(self) -> DurableAlbumResult:
        self._claim()
        for step in (self._back_up_originals, self._download,
                     self._check_library_backup, self._record_coverage,
                     self._verify_staged, self._run_staged_preparation,
                     self._prepare_tags, self._import, self._retire_staging,
                     self._prepare_backup_settlement,
                     self._publish_completion):
            stopped = step()
            if stopped is not None:
                return stopped
        return self._finish()

    def _claim(self):
        _require_authority(self.authority)
        _require_current_plan(self.item, self.args, self.plan)
        self.journal, journal_item = _claim_pending_item(
            self.item,
            mode=self.mode,
            authority=self.authority,
            resume_owner=self.resume_owner,
        )
        self.owner = RecoveryOwner(self.journal.operation_id, journal_item.item_id)
        self.initial = initial_completion_input(self.plan, self.owner, self.origin)
        if journal_item.phase is queue_state.QueuePhase.ACTIVE:
            if (
                journal_item.recovery_references
                or journal_item.block_reason is not None
                or journal_item.completion_evidence is not None
                or journal_item.completion_input != self.initial.to_record()
            ):
                raise DurableAlbumUnavailable(
                    "the matching durable queue item cannot resume its download"
                )
        _require_authority(self.authority)
        if isolated_staging_run_names():
            raise DurableAlbumUnavailable("an unclaimed download staging run needs attention")
        if journal_item.phase is queue_state.QueuePhase.PENDING:
            self.journal = queue_state.transition_journal_item(
                self.journal,
                journal_item.item_id,
                queue_state.QueuePhase.ACTIVE,
                completion_input=self.initial,
                multi_artist_filing=bool(
                    getattr(self.args, "migrate_multi_artist", False)
                ),
            )
            _require_authority(self.authority)
        self.operation_id = self.journal.operation_id
        self.item_id = journal_item.item_id

    def _checkpoint_group(self, record):
        _require_authority(self.authority)
        if (
            type(record) is not dict
            or record.get("version") != 2
            or record.get("owner") != _owner_record(self.owner)
        ):
            raise ValueError("staging recovery checkpoint is malformed")
        self.journal = queue_state.append_staging_group_intent(
            self.journal,
            self.item_id,
            record,
        )
        _require_authority(self.authority)

    def _checkpoint_backup_intent(self, record):
        _require_authority(self.authority)
        if (
            type(record) is not dict
            or record.get("version") != 1
            or record.get("kind") not in {"upgrade", "gap-fill"}
            or record.get("kind") != self.plan.library_backup_kind
            or record.get("owner") != _owner_record(self.owner)
        ):
            raise ValueError("library backup intent is malformed")
        self.journal = queue_state.append_library_backup_intent(
            self.journal,
            self.item_id,
            record,
        )
        _require_authority(self.authority)
        matches = tuple(
            reference
            for reference in _journal_item(self.journal, self.item_id).recovery_references
            if reference.kind == _LIBRARY_BACKUP_INTENT
        )
        if len(matches) != 1:
            raise DurableAlbumUnavailable(
                "library backup intent was not committed exactly"
            )
        self.backup_intent = matches[0]

    def _checkpoint_backup_carrier(self, payload):
        _require_authority(self.authority)
        if (
            type(payload) is not dict
            or set(payload) != {"version", "kind", "owner", "carrier"}
            or payload.get("version") != 1
            or payload.get("kind") != "library-backup-carrier"
            or payload.get("owner") != _owner_record(self.owner)
            or type(payload.get("carrier")) is not dict
            or self.backup_intent is None
        ):
            raise ValueError("library backup carrier is malformed")
        self.journal = queue_state.promote_library_backup_carrier(
            self.journal,
            self.item_id,
            self.backup_intent,
            payload["carrier"],
        )
        _require_authority(self.authority)
        matches = tuple(
            reference
            for reference in _journal_item(self.journal, self.item_id).recovery_references
            if reference.kind == _LIBRARY_BACKUP_CARRIER
        )
        if len(matches) != 1:
            raise DurableAlbumUnavailable(
                "library backup carrier was not committed exactly"
            )
        self.backup_carrier = matches[0]

    def _checkpoint_staging(self, payload):
        _require_authority(self.authority)
        if type(payload) is dict and payload.get("version") == 2:
            self._checkpoint_group(payload)
            return
        if (
            type(payload) is dict
            and payload.get("version") == 1
            and payload.get("kind") in {"upgrade", "gap-fill"}
        ):
            self._checkpoint_backup_intent(payload)
            return
        if (
            type(payload) is dict
            and payload.get("version") == 1
            and payload.get("kind") == "library-backup-carrier"
        ):
            self._checkpoint_backup_carrier(payload)
            return
        if (
            type(payload) is not dict
            or set(payload) != {"version", "kind", "owner", "record"}
            or payload.get("version") != 1
            or payload.get("kind") != "staging-run"
            or payload.get("owner") != _owner_record(self.owner)
            or type(payload.get("record")) is not dict
        ):
            raise ValueError("download staging checkpoint is malformed")
        self.journal = queue_state.append_staging_run_reference(
            self.journal,
            self.item_id,
            payload["record"],
        )
        _require_authority(self.authority)

    def _back_up_originals(self):
        if self.plan.library_backup_kind == "upgrade":
            _require_authority(self.authority)
            _require_current_plan(self.item, self.args, self.plan)
            backup = backup_album_dir(
                Path(self.item["album_dir"]),
                expected_receipt=self.item.get("_validated_source_receipt"),
                owner=_owner_record(self.owner),
                on_intent=self._checkpoint_backup_intent,
            )
            _require_authority(self.authority)
            if backup is None:
                reason = "library-backup-unavailable"
                if self.backup_intent is not None:
                    # The album was left in place, so once its planned backup is
                    # settled this is an ordinary retry.
                    try:
                        restored = _restore_library_backup(
                            self.operation_id, self.item_id, self.owner, self.item,
                            authority=self.authority)
                    except (OSError, ValueError, queue_state.QueueJournalError):
                        restored = None
                    if (
                        restored is None
                        or restored.status
                        is not LibraryBackupResolutionStatus.SETTLED
                    ):
                        reason = "library-backup-intent-unsettled"
                        if restored is not None and restored.reason:
                            reason = restored.reason
                        _block(self.operation_id, self.item_id, reason, authority=self.authority)
                        return DurableAlbumResult(
                            DurableAlbumStatus.ATTENTION,
                            reason,
                            operation_id=self.operation_id,
                            item_id=self.item_id,
                        )
                _reset_unstarted(
                    self.operation_id,
                    self.item_id,
                    authority=self.authority,
                )
                return DurableAlbumResult(
                    DurableAlbumStatus.RETRY,
                    reason,
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                )
            carrier_record = library_backup_record(
                backup,
                expected_owner=_owner_record(self.owner),
            )
            if carrier_record is None:
                _require_authority(self.authority)
                pinned = pin_unverified_upgrade_backup(
                    backup,
                    "queue backup kept; its exact carrier could not be reopened",
                    expected_owner=_owner_record(self.owner),
                )
                _require_authority(self.authority)
                if not pinned:
                    warn_pin_failed(backup.path)
                reason = "library-backup-carrier-unavailable"
                _block(self.operation_id, self.item_id, reason, authority=self.authority)
                return DurableAlbumResult(
                    DurableAlbumStatus.ATTENTION,
                    reason,
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                )
            self._checkpoint_backup_carrier({
                "version": 1,
                "kind": "library-backup-carrier",
                "owner": _owner_record(self.owner),
                "carrier": carrier_record,
            })
            self.item["backup_path"] = backup
            if backup.complete is not True:
                _require_authority(self.authority)
                pinned = pin_unverified_upgrade_backup(
                    backup,
                    "queue backup kept; source retirement did not finish exactly",
                    expected_owner=_owner_record(self.owner),
                )
                _require_authority(self.authority)
                if not pinned:
                    warn_pin_failed(backup.path)
                reason = "library-backup-incomplete"
                _block(self.operation_id, self.item_id, reason, authority=self.authority)
                return DurableAlbumResult(
                    DurableAlbumStatus.ATTENTION,
                    reason,
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                )

    def _download(self):
        _require_authority(self.authority)
        self.item["snapshot_before"] = snapshot_staging()
        _require_authority(self.authority)
        _require_current_plan(self.item, self.args, self.plan)

        try:
            _require_authority(self.authority)
            _require_current_plan(self.item, self.args, self.plan)
            run_album_download(
                album=self.item["album"],
                missing=self.item["missing"],
                present=self.item["present"],
                album_dir=self.item.get("album_dir"),
                snapshot=self.item["snapshot_before"],
                existing=(
                    None if self.plan.library_backup_kind == "gap-fill" else []
                ),
                quality=self.plan.effective_tier,
                upgrade_only=bool(self.item.get("upgrade_only")),
                force_track_by_track=False,
                exact_edition=self.item.get("exact_edition", False),
                result=self.item,
                recovery_owner=_owner_record(self.owner),
                recovery_checkpoint=self._checkpoint_staging,
                required_backup_kind=self.plan.library_backup_kind,
                expected_gap_fill_receipts=self.item.get(
                    "_validated_gap_fill_receipts"
                ),
            )
            _require_authority(self.authority)
        except BaseException:
            _retry_or_attention_after_download(
                operation_id=self.operation_id,
                item_id=self.item_id,
                result=self.item,
                owner=self.owner,
                checkpoint_group=self._checkpoint_group,
                reason="download-interrupted",
                authority=self.authority,
            )
            raise

    def _check_library_backup(self):
        if self.plan.library_backup_kind is not None:
            live_backup = self.item.get(
                "backup_path"
                if self.plan.library_backup_kind == "upgrade"
                else "gap_fill_backup_path"
            )
            live_record = library_backup_record(
                live_backup,
                expected_owner=_owner_record(self.owner),
            )
            if (
                self.backup_carrier is None
                or live_record is None
                or live_record != self.backup_carrier.data
            ):
                reason = "library-backup-carrier-unsettled"
                _block(self.operation_id, self.item_id, reason, authority=self.authority)
                return DurableAlbumResult(
                    DurableAlbumStatus.ATTENTION,
                    reason,
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                )
        elif self.backup_intent is not None or self.backup_carrier is not None:
            reason = "unexpected-library-backup-state"
            _block(self.operation_id, self.item_id, reason, authority=self.authority)
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                operation_id=self.operation_id,
                item_id=self.item_id,
            )

    def _record_coverage(self):
        coverage = exact_download_coverage(self.item, self.item["album"])
        self.ready_input = (
            completion_input_from_download(self.initial, coverage) if coverage is not None else None
        )
        if self.ready_input is None:
            if is_cancel_requested():
                settled = _cancel_after_download(
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                    result=self.item,
                    owner=self.owner,
                    checkpoint_group=self._checkpoint_group,
                    authority=self.authority,
                )
                if settled is not None:
                    return settled
            outcome = _retry_or_attention_after_download(
                operation_id=self.operation_id,
                item_id=self.item_id,
                result=self.item,
                owner=self.owner,
                checkpoint_group=self._checkpoint_group,
                reason="download-incomplete",
                authority=self.authority,
            )
            if outcome.status is not DurableAlbumStatus.ATTENTION:
                return outcome
            return _abandon_blocked_item(
                operation_id=self.operation_id,
                item_id=self.item_id,
                result=self.item,
                reason="download-incomplete",
                fallback=outcome,
                authority=self.authority,
            )
        _require_authority(self.authority)
        self.journal = queue_state.transition_journal_item(
            _load_exact(self.operation_id),
            self.item_id,
            queue_state.QueuePhase.ACTIVE,
            completion_input=self.ready_input,
        )
        _require_authority(self.authority)

    def _verify_staged(self):
        try:
            self.album_dirs = validated_staged_album_dirs(self.item)
            if rip_shortfall(self.album_dirs, self.item["album"], effective_tier=2)["under"]:
                raise OSError("the staged album is below CD quality")
            self.item["quality_verdict"] = verify_and_recover(
                self.item["album"],
                self.album_dirs,
                effective_tier=self.plan.effective_tier,
                allow_retry=False,
                exempt=self.item.get("_cd_quality_paths", ()),
            )
            bindings = staged_track_bindings(self.item)
        except (OSError, TypeError, ValueError):
            return _retry_or_attention_after_download(
                operation_id=self.operation_id,
                item_id=self.item_id,
                result=self.item,
                owner=self.owner,
                checkpoint_group=self._checkpoint_group,
                reason="staged-album-unverified",
                authority=self.authority,
            )
        if self.plan.library_backup_kind is not None:
            from qobuz_librarian.modes import process

            _require_authority(self.authority)
            backup = load_library_backup_record(
                self.backup_carrier.data, expected_owner=_owner_record(self.owner),
            )
            # Beets files a copy over the user's tracks only when it holds up
            # against them, track by track; otherwise they go back.
            if (
                backup is not None
                and len(self.album_dirs) == 1
                and not process._upgrade_trees_verified(self.album_dirs[0], backup.path)
            ):
                outcome = _retry_or_attention_after_download(
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                    result=self.item,
                    owner=self.owner,
                    checkpoint_group=self._checkpoint_group,
                    reason="upgrade-unverified",
                    authority=self.authority,
                )
                if outcome.status is not DurableAlbumStatus.ATTENTION:
                    return outcome
                return _abandon_blocked_item(
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                    result=self.item,
                    reason="upgrade-unverified",
                    fallback=outcome,
                    authority=self.authority,
                )
        self.binding_records = managed_binding_records(self.ready_input)
        if self.binding_records is None or tuple(bindings) != self.binding_records:
            return _retry_or_attention_after_download(
                operation_id=self.operation_id,
                item_id=self.item_id,
                result=self.item,
                owner=self.owner,
                checkpoint_group=self._checkpoint_group,
                reason="staged-bindings-changed",
                authority=self.authority,
            )

    def _checkpoint_sources(self, kind):
        _require_authority(self.authority)
        if type(kind) is not SourceTransitionKind:
            raise ValueError("staged source transition is invalid")
        records = refresh_staged_track_bindings(self.item)
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
        advanced = advance_completion_sources(self.ready_input, current_bindings, kind)
        if advanced is None:
            raise OSError("staged source transition could not be proved")
        self.journal = queue_state.transition_journal_item(
            _load_exact(self.operation_id),
            self.item_id,
            queue_state.QueuePhase.ACTIVE,
            completion_input=advanced,
        )
        _require_authority(self.authority)
        self.ready_input = advanced
        self.binding_records = managed_binding_records(self.ready_input)
        if self.binding_records is None:
            raise OSError("staged source transition lost completion lineage")

    def _run_staged_preparation(self):
        if self.prepare_staged is not None:
            if not callable(self.prepare_staged):
                raise ValueError("prepare_staged must be callable")
            try:
                _require_authority(self.authority)
                prepared = self.prepare_staged(self.album_dirs, self._checkpoint_sources)
                _require_authority(self.authority)
                resampled = getattr(prepared, "resampled", None)
                if not isinstance(resampled, int):
                    raise OSError("staged preparation result is malformed")
                self.item["resampled_n"] = resampled
                self.item["downsample_errors"] = getattr(prepared, "errors", 0)
                self.item["downsample_saved_bytes"] = getattr(
                    prepared, "saved_bytes", 0
                )
                self.item["downsample_flush_warnings"] = getattr(
                    prepared, "flush_warnings", 0
                )
                self.item["downsample_cancelled"] = bool(
                    getattr(prepared, "cancelled", False)
                )
                if validated_staged_album_dirs(self.item) != self.album_dirs:
                    raise OSError("staged album roots changed during preparation")
                current_records = tuple(staged_track_bindings(self.item))
                if self.binding_records is None or current_records != self.binding_records:
                    raise OSError("prepared staged bindings were not checkpointed")
            except Exception:
                return _retry_or_attention_after_download(
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                    result=self.item,
                    owner=self.owner,
                    checkpoint_group=self._checkpoint_group,
                    reason="staged-preparation-unsettled",
                    authority=self.authority,
                )
            except BaseException:
                _retry_or_attention_after_download(
                    operation_id=self.operation_id,
                    item_id=self.item_id,
                    result=self.item,
                    owner=self.owner,
                    checkpoint_group=self._checkpoint_group,
                    reason="staged-preparation-interrupted",
                    authority=self.authority,
                )
                raise

    def _prepare_tags(self):
        try:
            if self.plan.library_backup_kind == "upgrade":
                from qobuz_librarian.modes import process

                _require_authority(self.authority)
                backup = load_library_backup_record(
                    self.backup_carrier.data, expected_owner=_owner_record(self.owner),
                )
                if backup is None or len(self.album_dirs) != 1:
                    raise OSError("upgrade originals could not be read")
                if not process._track_annotations_preserved(backup.path, self.album_dirs[0]):
                    if not process._carry_track_annotations(backup.path, self.album_dirs[0]):
                        raise OSError("original track tags could not be carried")
                    self._checkpoint_sources(SourceTransitionKind.ORIGINAL_TAGS)
                if (load_library_backup_record(
                        self.backup_carrier.data, expected_owner=_owner_record(self.owner),
                    ) is None
                        or not process._track_annotations_preserved(
                            backup.path, self.album_dirs[0])):
                    raise OSError("original track tags could not be verified")
                process._keep_own_cover(backup.path, self.album_dirs)
                _require_authority(self.authority)
            cleaned_bindings = prepare_managed_staging_tags(
                self.album_dirs,
                self.binding_records,
                authority_check=lambda: _require_authority(self.authority),
            )
            if tuple(cleaned_bindings) != self.binding_records:
                self._checkpoint_sources(SourceTransitionKind.BEETS_TAG_CLEAN)
                if tuple(cleaned_bindings) != self.binding_records:
                    raise OSError("tag-clean source checkpoint changed")
        except Exception:
            return _retry_or_attention_after_download(
                operation_id=self.operation_id,
                item_id=self.item_id,
                result=self.item,
                owner=self.owner,
                checkpoint_group=self._checkpoint_group,
                reason="staged-tag-clean-unsettled",
                authority=self.authority,
            )
        except BaseException:
            _retry_or_attention_after_download(
                operation_id=self.operation_id,
                item_id=self.item_id,
                result=self.item,
                owner=self.owner,
                checkpoint_group=self._checkpoint_group,
                reason="staged-tag-clean-interrupted",
                authority=self.authority,
            )
            raise

    def _checkpoint_reservation(self, payload):
        _require_authority(self.authority)
        if (
            type(payload) is not dict
            or set(payload) != {"version", "kind", "owner", "reservation"}
            or payload.get("version") != 1
            or payload.get("kind") != _MANAGED_RESERVATION
            or payload.get("owner") != _owner_record(self.owner)
            or type(payload.get("reservation")) is not dict
        ):
            raise ValueError("managed import reservation is malformed")
        reference = queue_state.RecoveryReference(
            "managed-import",
            _MANAGED_RESERVATION,
            payload["reservation"],
        )
        self.journal = queue_state.reserve_managed_carrier(
            self.journal,
            self.item_id,
            reference,
        )
        _require_authority(self.authority)
        self.reservation = reference

    def _checkpoint_carrier(self, payload):
        _require_authority(self.authority)
        if (
            type(payload) is not dict
            or set(payload) != {"version", "kind", "owner", "carrier"}
            or payload.get("version") != 1
            or payload.get("kind") != _MANAGED_CARRIER
            or payload.get("owner") != _owner_record(self.owner)
            or type(payload.get("carrier")) is not dict
            or self.reservation is None
        ):
            raise ValueError("managed import carrier is malformed")
        reference = queue_state.RecoveryReference(
            "managed-import",
            _MANAGED_CARRIER,
            payload["carrier"],
        )
        self.journal = queue_state.promote_managed_carrier(
            self.journal,
            self.item_id,
            self.reservation,
            reference,
            self.ready_input,
        )
        _require_authority(self.authority)
        self.carrier = reference

    def _import(self):
        try:
            _require_authority(self.authority)
            replaced = (
                {} if self.item.get("album_dir") is None
                else {"album_dir": self.item["album_dir"]}
            )
            self.imported = beets_import_managed(
                self.album_dirs,
                self.binding_records,
                owner=_owner_record(self.owner),
                on_reservation=self._checkpoint_reservation,
                on_intent=self._checkpoint_carrier,
                authority_check=lambda: _require_authority(self.authority),
                **replaced,
            )
            _require_authority(self.authority)
        except BaseException:
            _block(
                self.operation_id,
                self.item_id,
                "managed-import-interrupted",
                authority=self.authority,
            )
            raise

        self.journal = _load_exact(self.operation_id)
        current = _journal_item(self.journal, self.item_id)
        persisted_carriers = tuple(
            reference for reference in current.recovery_references if reference.kind == _MANAGED_CARRIER
        )
        self.managed = managed_completion_evidence(self.imported)
        sealed_input = managed_completion_input(self.ready_input, self.managed) if self.managed is not None else None
        if (
            self.imported.kind != "ok"
            or self.imported.spawned is not True
            or self.managed is None
            or sealed_input != self.ready_input
            or self.carrier is None
            or persisted_carriers != (self.carrier,)
            or self.imported.carrier != self.carrier.data
        ):
            _block(
                self.operation_id,
                self.item_id,
                "managed-import-unsettled",
                authority=self.authority,
            )
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                "managed-import-disk-full" if self.imported.disk_full
                else "managed-import-unsettled",
                operation_id=self.operation_id,
                item_id=self.item_id,
            )

    def _retire_staging(self):
        try:
            _require_authority(self.authority)
            retired = retire_empty_download_staging(
                self.item,
                recovery_checkpoint=self._checkpoint_group,
            )
            _require_authority(self.authority)
        except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
            retired = False
        if not retired:
            try:
                _require_authority(self.authority)
                retired = retire_download_staging_after_import(
                    self.item,
                    recovery_checkpoint=self._checkpoint_group,
                )
                _require_authority(self.authority)
            except (OSError, TypeError, ValueError, queue_state.QueueJournalError):
                retired = False
        self.journal = _load_exact(self.operation_id)
        self.journal = (
            _reconcile_absent_staging(
                self.journal,
                self.item_id,
                self.owner,
                authority=self.authority,
            )
            if retired
            else None
        )
        if self.journal is None:
            _block(
                self.operation_id,
                self.item_id,
                "download-staging-unsettled",
                authority=self.authority,
            )
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                "download-staging-unsettled",
                operation_id=self.operation_id,
                item_id=self.item_id,
            )

        current = _journal_item(self.journal, self.item_id)
        expected_references = (
            (self.backup_carrier, self.carrier)
            if self.backup_carrier is not None
            else (self.carrier,)
        )
        if current.recovery_references != expected_references:
            _block(
                self.operation_id,
                self.item_id,
                "recovery-reference-unsettled",
                authority=self.authority,
            )
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                "recovery-reference-unsettled",
                operation_id=self.operation_id,
                item_id=self.item_id,
            )

    def _prepare_backup_settlement(self):
        self.post_dir = Path(self.managed.library_root) / self.managed.album_path
        backup_resolution = prepare_library_backup_settlement(
            self.journal,
            self.item_id,
            self.owner,
            self.item["album"],
            self.post_dir,
            authority_check=lambda: _require_authority(self.authority),
        )
        if backup_resolution.status is LibraryBackupResolutionStatus.ATTENTION:
            reason = backup_resolution.reason or "library-backup-unsettled"
            _block(self.operation_id, self.item_id, reason, authority=self.authority)
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                self.post_dir,
                self.operation_id,
                self.item_id,
            )
        self.journal = backup_resolution.journal

        refreshed_managed = managed_completion_evidence(self.imported)
        stable_managed = (
            refreshed_managed is not None
            and refreshed_managed.owner == self.managed.owner
            and refreshed_managed.library_root == self.managed.library_root
            and refreshed_managed.library_root_identity
                == self.managed.library_root_identity
            and refreshed_managed.album_path == self.managed.album_path
            and refreshed_managed.manifest_hash == self.managed.manifest_hash
            and refreshed_managed.mappings == self.managed.mappings
        )
        if not stable_managed or (
            self.plan.library_backup_kind != "upgrade"
            and refreshed_managed != self.managed
        ):
            reason = "managed-completion-changed-during-backup-resolution"
            _block(self.operation_id, self.item_id, reason, authority=self.authority)
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                reason,
                self.post_dir,
                self.operation_id,
                self.item_id,
            )
        self.managed = refreshed_managed
        self.post_dir = Path(self.managed.library_root) / self.managed.album_path

    def _publish_completion(self):
        try:
            _require_authority(self.authority)
            backup_resolution = finish_library_backup_settlement(
                self.journal,
                self.item_id,
                self.owner,
                self.item["album"],
                authority_check=lambda: _require_authority(self.authority),
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
                    self.operation_id,
                    self.item_id,
                    reason,
                    authority=self.authority,
                )
                return DurableAlbumResult(
                    DurableAlbumStatus.ATTENTION,
                    reason,
                    self.post_dir,
                    self.operation_id,
                    self.item_id,
                )
            self.journal = backup_resolution.journal
            settled_managed = managed_completion_evidence(self.imported)
            if settled_managed != self.managed:
                reason = "managed-completion-changed-during-backup-resolution"
                _block(self.operation_id, self.item_id, reason, authority=self.authority)
                return DurableAlbumResult(
                    DurableAlbumStatus.ATTENTION,
                    reason,
                    self.post_dir,
                    self.operation_id,
                    self.item_id,
                )
            self.managed = settled_managed
            self.post_dir = Path(self.managed.library_root) / self.managed.album_path
            with reopen_managed_evidence(self.carrier.data, self.owner) as managed_lease:
                if managed_lease.evidence != self.managed:
                    raise DurableAlbumUnavailable("managed completion changed before publication")
                with capture_new_album_completion(
                    managed_lease=managed_lease,
                    owner=self.owner,
                    expectation=self.ready_input.expectation,
                    download=self.ready_input.download_coverage(),
                ) as live_lease:
                    evidence = live_lease.revalidate()
                    _require_authority(self.authority)
                    self.journal = queue_state.transition_journal_item(
                        self.journal,
                        self.item_id,
                        queue_state.QueuePhase.COMPLETE,
                        completion_evidence=evidence.to_record(),
                    )
                    _require_authority(self.authority)
                    live_lease.revalidate()
                    _require_authority(self.authority)
                    post_import_action = plan_post_import_action(
                        self.journal,
                        self.item_id,
                        self.post_dir,
                        authority=self.authority,
                    )
                    _require_authority(self.authority)
                    live_lease.revalidate()
                    self.journal = queue_state.commit_completed_item_removal(
                        self.journal,
                        self.queue,
                        item_id=self.item_id,
                        caller_item=self.item,
                        live_evidence=evidence,
                        post_import_action=post_import_action,
                    )
                    _require_authority(self.authority)
                    live_lease.revalidate()
        except LibraryBackupPersistenceError:
            raise
        except Exception:
            # The queue-removal commit deliberately replaces the item with a
            # retirement record.  A final lease check can therefore fail after the
            # item is already, correctly, absent; recovery must preserve that
            # committed state instead of treating the absence as corruption.
            current = _find_journal_item(_load_exact(self.operation_id), self.item_id)
            if current is not None and current.phase is queue_state.QueuePhase.RESOLVING:
                _block(
                    self.operation_id,
                    self.item_id,
                    "completion-proof-unavailable",
                    authority=self.authority,
                )
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                "completion-proof-unavailable",
                self.post_dir,
                self.operation_id,
                self.item_id,
            )

    def _finish(self):
        try:
            _require_authority(self.authority)
            self.journal, final_path, retirement = finalize_carrier_retirement(
                self.journal,
                self.item_id,
                authority=self.authority,
                acknowledge_completion=self.acknowledge_completion,
            )
            _require_authority(self.authority)
        except (OSError, ValueError, queue_state.QueueJournalError):
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                "managed-carrier-retirement-unsettled",
                self.post_dir,
                self.operation_id,
                self.item_id,
            )
        if final_path is not None:
            self.post_dir = final_path
        if retirement.outcome not in {
            ManagedCarrierRetirementOutcome.RETIRED,
            ManagedCarrierRetirementOutcome.ALREADY_ABSENT,
        }:
            return DurableAlbumResult(
                DurableAlbumStatus.ATTENTION,
                "managed-carrier-retirement-unsettled",
                self.post_dir,
                self.operation_id,
                self.item_id,
            )
        if not self.journal.items and not self.journal.retirements:
            try:
                _require_authority(self.authority)
                queue_state.clear_queue_journal(self.operation_id)
                _require_authority(self.authority)
            except (OSError, ValueError, queue_state.QueueJournalError):
                return DurableAlbumResult(
                    DurableAlbumStatus.ATTENTION,
                    "completed-journal-cleanup-unsettled",
                    self.post_dir,
                    self.operation_id,
                    self.item_id,
                )
        clear_attempts_after_import(self.item)
        return DurableAlbumResult(
            DurableAlbumStatus.COMPLETE,
            post_dir=self.post_dir,
            operation_id=self.operation_id,
            item_id=self.item_id,
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
    return _DurableAlbumRun(
        queue, item, args, plan=plan, origin=origin, mode=mode,
        authority=authority, resume_owner=resume_owner,
        prepare_staged=prepare_staged,
        acknowledge_completion=acknowledge_completion,
    ).run()
