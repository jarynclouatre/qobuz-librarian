"""UI-free restart recovery for durable queue operations."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    CompletionOriginKind,
    RecoveryOwner,
    completion_input_ready,
    parse_completion_input_record,
)
from qobuz_librarian.completion_live import (
    LiveCompletionUnavailable,
    capture_new_album_completion,
)
from qobuz_librarian.integrations.beets import (
    ManagedCarrierInspectionOutcome,
    ManagedCarrierRetirementOutcome,
    ManagedEvidenceUnavailable,
    ManagedReservationInspectionOutcome,
    inspect_managed_carrier,
    inspect_managed_reservation,
    reopen_managed_evidence,
    retire_prelaunch_managed_carrier,
)
from qobuz_librarian.integrations.staging import (
    StagingReferenceStatus,
    bind_unclaimed_staging_run,
    discard_file_group,
    discard_group,
    inspect_staging_group_reference,
    inspect_staging_run_reference,
    isolated_staging_run_names,
    retain_staging_run,
    staging_run_from_record,
)
from qobuz_librarian.library.backup import (
    library_backup_record,
    load_backup_result,
    load_library_backup_record,
    pin_unverified_upgrade_backup,
    warn_pin_failed,
)
from qobuz_librarian.library.post_import_relocation import (
    RelocationRecoveryResult,
    RelocationRecoveryStatus,
    reconcile_post_import_relocations,
)
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue.library_backup_recovery import (
    LibraryBackupResolutionStatus,
    finish_library_backup_settlement,
    prepare_library_backup_settlement,
)
from qobuz_librarian.queue.post_import_finalizer import (
    finalize_carrier_retirement,
    plan_post_import_action,
)
from qobuz_librarian.run_lock import RunLockLease

_STAGING_RUN_KIND = "download-staging-run"
_STAGING_GROUP_KIND = "staging-group"
_STAGING_KINDS = frozenset({_STAGING_RUN_KIND, _STAGING_GROUP_KIND})
_STAGING_BLOCK_REASONS = frozenset(
    f"{kind}-{suffix}"
    for kind in _STAGING_KINDS
    for suffix in ("present", "changed", "unavailable")
)
_MANAGED_RESERVATION_KIND = "managed-beets-reservation"
_MANAGED_CARRIER_KIND = "managed-beets"
_MANAGED_SETTLEMENT_KIND = "managed-beets-prelaunch-settlement"
_MANAGED_KINDS = frozenset(
    {
        _MANAGED_RESERVATION_KIND,
        _MANAGED_CARRIER_KIND,
        _MANAGED_SETTLEMENT_KIND,
    }
)
_LIBRARY_BACKUP_INTENT_KIND = "library-backup-intent"
_LIBRARY_BACKUP_CARRIER_KIND = "library-backup"
_LIBRARY_BACKUP_SETTLEMENT_KIND = "library-backup-settlement"
_LIBRARY_BACKUP_KINDS = frozenset({
    _LIBRARY_BACKUP_INTENT_KIND,
    _LIBRARY_BACKUP_CARRIER_KIND,
    _LIBRARY_BACKUP_SETTLEMENT_KIND,
})
POST_IMPORT_RELOCATION_LOG_ENTRY = (
    "Post-import folder-move recovery needs attention"
)


class StartupRecoveryStatus(str, Enum):
    CLEAR = "clear"
    RESUME_REQUIRED = "resume_required"
    ATTENTION_REQUIRED = "attention_required"


class StartupRecoveryAction(str, Enum):
    PENDING = "pending"
    RESUME_DOWNLOAD = "resume_download"
    RESUME_IMPORT = "resume_import"
    FINALISE_COMPLETION = "finalise_completion"
    BLOCKED = "blocked"


class BlockedItemSettlementAction(str, Enum):
    RETRY = "retry"
    DISCARD = "discard"


class BlockedItemSettlementStatus(str, Enum):
    RETRYABLE = "retryable"
    DISCARDED = "discarded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class StartupRecoveryItem:
    operation_id: str
    item_id: str
    mode: str
    phase: queue_state.QueuePhase
    action: StartupRecoveryAction
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StartupRecoveryResult:
    status: StartupRecoveryStatus
    items: tuple[StartupRecoveryItem, ...] = ()
    reason: str | None = None
    post_import_relocation: RelocationRecoveryResult | None = None


@dataclass(frozen=True, slots=True)
class BlockedItemSettlementResult:
    status: BlockedItemSettlementStatus
    reason: str


class _AuthorityLost(RuntimeError):
    pass


class _SettlementJournalFailure(RuntimeError):
    """A committed operator decision could not be written back durably."""


def _post_import_relocation_handoff_matches(operation_id, handoff):
    """Consult jobs.db without making the UI persistence layer a hard import."""
    try:
        from qobuz_librarian.web import job_persistence

        job_persistence.init()
        return job_persistence.post_import_relocation_handoff_persisted(
            operation_id,
            handoff,
        )
    except Exception:
        return None


def _combined_post_import_relocation_handoff_matches(operation_id, handoff):
    """Accept either durable queue or Web ownership of a relocation handoff."""
    queue_match = queue_state.post_import_relocation_handoff_matches(
        operation_id,
        handoff,
    )
    if queue_match is True:
        return True
    web_match = _post_import_relocation_handoff_matches(operation_id, handoff)
    if web_match is True:
        return True
    if queue_match is None or web_match is None:
        return None
    return False


def _require_authority(authority: RunLockLease) -> None:
    if type(authority) is not RunLockLease or authority.intact() is not True:
        raise _AuthorityLost


def _sorted_journals(loads):
    journals = [
        loaded.journal
        for loaded in loads
        if loaded.status is queue_state.QueueLoadStatus.READY and loaded.journal is not None
    ]
    return tuple(sorted(journals, key=lambda journal: journal.operation_id))


def _references(item, kinds):
    return tuple(
        reference
        for reference in item.recovery_references
        if type(reference) is queue_state.RecoveryReference and reference.kind in kinds
    )


def _single_reference(item, kind):
    matches = _references(item, {kind})
    return matches[0] if len(matches) == 1 else None


def _staging_references(item):
    return _references(item, _STAGING_KINDS)


def _pin_library_backup_for_attention(authority, journal, item):
    references = _references(item, _LIBRARY_BACKUP_KINDS)
    if len(references) != 1:
        return
    reference = references[0]
    owner = {
        "operation_id": journal.operation_id,
        "item_id": item.item_id,
    }
    _require_authority(authority)
    if reference.kind == _LIBRARY_BACKUP_INTENT_KIND:
        backup = load_backup_result(
            reference.data["path"],
            expected_owner=owner,
        )
    elif reference.kind == _LIBRARY_BACKUP_CARRIER_KIND:
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
    if backup is None:
        return
    pinned = pin_unverified_upgrade_backup(
        backup,
        "queue backup kept — restart recovery requires attention",
        expected_owner=owner,
    )
    _require_authority(authority)
    if not pinned:
        warn_pin_failed(backup.path)


def _find_item(journal, item_id):
    return next(value for value in journal.items if value.item_id == item_id)


def _classify(journals) -> tuple[StartupRecoveryItem, ...]:
    classified = []
    for journal in journals:
        for item in sorted(journal.items, key=lambda value: value.item_id):
            reason = item.block_reason
            if item.phase is queue_state.QueuePhase.PENDING:
                action = StartupRecoveryAction.PENDING
            elif item.phase is queue_state.QueuePhase.ACTIVE:
                if _staging_references(item):
                    action = StartupRecoveryAction.BLOCKED
                    reason = reason or "staging-recovery-unsettled"
                elif _references(item, _LIBRARY_BACKUP_KINDS):
                    action = StartupRecoveryAction.BLOCKED
                    reason = reason or "library-backup-recovery-unsettled"
                elif _single_reference(item, _MANAGED_RESERVATION_KIND) is not None:
                    action = StartupRecoveryAction.BLOCKED
                    reason = reason or "managed-reservation-unsettled"
                elif item.recovery_references:
                    action = StartupRecoveryAction.BLOCKED
                    reason = reason or "recovery-reference-invalid"
                else:
                    parsed = parse_completion_input_record(
                        item.completion_input,
                        expected_owner=RecoveryOwner(
                            journal.operation_id,
                            item.item_id,
                        ),
                    )
                    if parsed is not None and completion_input_ready(parsed):
                        action = StartupRecoveryAction.BLOCKED
                        reason = "staging-recovery-missing"
                    elif parsed is not None and not parsed.lineages and parsed.counts is None:
                        action = StartupRecoveryAction.RESUME_DOWNLOAD
                    elif parsed is not None:
                        action = StartupRecoveryAction.BLOCKED
                        reason = "completion-input-incomplete"
                    else:
                        action = StartupRecoveryAction.BLOCKED
                        reason = "completion-input-invalid"
            elif item.phase in {
                queue_state.QueuePhase.RESOLVING,
                queue_state.QueuePhase.COMPLETE,
            }:
                action = StartupRecoveryAction.FINALISE_COMPLETION
            else:
                action = StartupRecoveryAction.BLOCKED
                reason = reason or "recovery-incomplete"
            classified.append(
                StartupRecoveryItem(
                    operation_id=journal.operation_id,
                    item_id=item.item_id,
                    mode=journal.mode,
                    phase=item.phase,
                    action=action,
                    reason=reason,
                )
            )
    return tuple(classified)


def _block_inconsistent_active(authority, journals):
    for journal in journals:
        for item in sorted(journal.items, key=lambda value: value.item_id):
            if item.phase is not queue_state.QueuePhase.ACTIVE or item.recovery_references:
                continue
            parsed = parse_completion_input_record(
                item.completion_input,
                expected_owner=RecoveryOwner(journal.operation_id, item.item_id),
            )
            if parsed is None:
                reason = "completion-input-invalid"
            elif completion_input_ready(parsed):
                reason = "staging-recovery-missing"
            elif parsed.lineages or parsed.counts is not None:
                reason = "completion-input-incomplete"
            else:
                continue
            _require_authority(authority)
            queue_state.transition_journal_item(
                journal,
                item.item_id,
                queue_state.QueuePhase.BLOCKED,
                block_reason=reason,
            )
            _require_authority(authority)
            return True
    return False


def _load_namespace(authority):
    _require_authority(authority)
    loads = queue_state.list_queue_journals()
    _require_authority(authority)
    return loads


def _unclaimed_staging_run_names(journals):
    staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
    claimed = set()
    for journal in journals:
        for item in journal.items:
            for reference in _references(item, {_STAGING_RUN_KIND}):
                path = reference.data.get("path")
                if not isinstance(path, str):
                    continue
                candidate = Path(os.path.abspath(path))
                if candidate.parent == staging:
                    claimed.add(candidate.name)
    return tuple(
        name for name in isolated_staging_run_names() if name not in claimed)


def _retain_unclaimed_staging_runs(authority, names) -> bool:
    """Park run roots no journal claims; the terminal lane leaves them behind.

    The boot holder owns the run lock, so no live rip can be writing these.
    A refusal (a stuck writer still holds a descriptor) falls back to the
    attention stop instead of touching the tree.
    """
    settled = True
    for name in names:
        _require_authority(authority)
        run = bind_unclaimed_staging_run(name)
        try:
            group = (
                retain_staging_run(run, label="abandoned")
                if run is not None else None
            )
        except OSError:
            group = None
        if group is None:
            settled = False
            continue
        logging.getLogger("qobuz_librarian").warning(
            "An unclaimed download folder %s was left in staging; it was "
            "moved to %s for review.",
            name,
            group.path,
        )
    return settled


def _staging_inspection(reference, owner):
    if reference.kind == _STAGING_RUN_KIND:
        return inspect_staging_run_reference(reference.data, owner)
    if reference.kind == _STAGING_GROUP_KIND:
        return inspect_staging_group_reference(reference.data, owner)
    return None


def _staging_block_reason(reference, inspection):
    status = getattr(inspection, "status", None)
    suffix = {
        StagingReferenceStatus.MATCH: "present",
        StagingReferenceStatus.CHANGED: "changed",
        StagingReferenceStatus.UNAVAILABLE: "unavailable",
    }.get(status, "unavailable")
    return f"{reference.kind}-{suffix}"


def _phase_after_staging_block(item):
    if (
        item.phase is not queue_state.QueuePhase.BLOCKED
        or item.block_reason not in _STAGING_BLOCK_REASONS
        or _staging_references(item)
    ):
        return None
    managed = _references(item, _MANAGED_KINDS)
    backups = _references(item, _LIBRARY_BACKUP_KINDS)
    if (
        len(managed) > 1
        or len(backups) > 1
        or len(managed) + len(backups) != len(item.recovery_references)
    ):
        return None
    if managed and managed[0].kind == _MANAGED_CARRIER_KIND:
        if backups and backups[0].kind not in {
            _LIBRARY_BACKUP_CARRIER_KIND,
            _LIBRARY_BACKUP_SETTLEMENT_KIND,
        }:
            return None
        return queue_state.QueuePhase.RESOLVING
    if (
        (not managed or managed[0].kind == _MANAGED_RESERVATION_KIND)
        and (
            not backups
            or backups[0].kind in {
                _LIBRARY_BACKUP_INTENT_KIND,
                _LIBRARY_BACKUP_CARRIER_KIND,
            }
        )
    ):
        return queue_state.QueuePhase.ACTIVE
    return None


def _restore_phase_after_staging_block(authority, journal, item):
    phase = _phase_after_staging_block(item)
    if phase is None:
        return journal, item, False
    _require_authority(authority)
    current = queue_state.transition_journal_item(
        journal,
        item.item_id,
        phase,
    )
    _require_authority(authority)
    return current, _find_item(current, item.item_id), True


def _reconcile_item_staging(
    authority,
    journal,
    item,
    *,
    block_unsettled,
):
    references = _staging_references(item)
    if not references:
        return journal, item, None, False

    owner = {
        "operation_id": journal.operation_id,
        "item_id": item.item_id,
    }
    absent = []
    unsettled = []
    for reference in references:
        _require_authority(authority)
        inspection = _staging_inspection(reference, owner)
        _require_authority(authority)
        if getattr(inspection, "status", None) is StagingReferenceStatus.ABSENT:
            absent.append(reference)
        else:
            unsettled.append((reference, inspection))

    changed = False
    current = journal
    current_item = item
    if absent:
        _require_authority(authority)
        current = queue_state.reconcile_staging_references(
            current,
            item.item_id,
            tuple(absent),
        )
        _require_authority(authority)
        current_item = _find_item(current, item.item_id)
        changed = True

    reason = None
    if unsettled:
        reason = _staging_block_reason(*unsettled[0])
        if block_unsettled and (
            current_item.phase is not queue_state.QueuePhase.BLOCKED
            or current_item.block_reason != reason
        ):
            _pin_library_backup_for_attention(
                authority,
                current,
                current_item,
            )
            _require_authority(authority)
            current = queue_state.transition_journal_item(
                current,
                item.item_id,
                queue_state.QueuePhase.BLOCKED,
                block_reason=reason,
            )
            _require_authority(authority)
            current_item = _find_item(current, item.item_id)
            changed = True
    elif block_unsettled:
        current, current_item, restored = _restore_phase_after_staging_block(
            authority,
            current,
            current_item,
        )
        changed = changed or restored
    return current, current_item, reason, changed


def _recover_staging_references(authority, journals):
    changed = False
    for journal in journals:
        current = journal
        for item_id in sorted(item.item_id for item in journal.items):
            item = _find_item(current, item_id)
            if not _staging_references(item):
                current, _item, item_changed = _restore_phase_after_staging_block(
                    authority,
                    current,
                    item,
                )
                changed = changed or item_changed
                continue
            current, _item, _reason, item_changed = _reconcile_item_staging(
                authority,
                current,
                item,
                block_unsettled=True,
            )
            changed = changed or item_changed
    return changed


def _completion_input(journal, item):
    owner = RecoveryOwner(journal.operation_id, item.item_id)
    completion_input = parse_completion_input_record(
        item.completion_input,
        expected_owner=owner,
    )
    if completion_input is None or not completion_input_ready(completion_input):
        raise queue_state.QueueJournalBlocked("recovery completion input is unavailable")
    download = completion_input.download_coverage()
    if download is None:
        raise queue_state.QueueJournalBlocked("recovery download coverage is unavailable")
    return owner, completion_input, download


def _needs_cli_completion_caller(
    journal,
    item,
    acknowledge_completion,
):
    if callable(acknowledge_completion):
        return False
    try:
        _owner, completion_input, _download = _completion_input(journal, item)
    except (TypeError, ValueError, queue_state.QueueJournalError):
        return False
    return completion_input.origin.kind is CompletionOriginKind.CLI


@contextmanager
def _capture_live_completion(journal, item, *, expected_manifest_hash=None):
    owner, completion_input, download = _completion_input(journal, item)
    reference = _single_reference(item, _MANAGED_CARRIER_KIND)
    if reference is None:
        raise ManagedEvidenceUnavailable("exact managed carrier recovery state is unavailable")
    with reopen_managed_evidence(reference.data, owner) as managed_lease:
        managed = managed_lease.revalidate()
        if expected_manifest_hash is not None and managed.manifest_hash != expected_manifest_hash:
            raise ManagedEvidenceUnavailable("managed carrier changed after inspection")
        with capture_new_album_completion(
            managed_lease=managed_lease,
            owner=owner,
            expectation=completion_input.expectation,
            download=download,
        ) as live_lease:
            evidence = live_lease.revalidate()
            yield managed_lease, live_lease, evidence


def _block_resolving(authority, journal, item, reason):
    _pin_library_backup_for_attention(authority, journal, item)
    _require_authority(authority)
    queue_state.transition_journal_item(
        journal,
        item.item_id,
        queue_state.QueuePhase.BLOCKED,
        block_reason=reason,
    )
    _require_authority(authority)


def _recover_complete(authority, journal, item, _acknowledge_completion):
    with _capture_live_completion(journal, item) as (
        _managed_lease,
        live_lease,
        evidence,
    ):
        _require_authority(authority)
        post_dir = Path(evidence.library_root) / evidence.album_path
        post_import_action = plan_post_import_action(
            journal,
            item.item_id,
            post_dir,
            authority=authority,
        )
        _require_authority(authority)
        live_lease.revalidate()
        saved = queue_state.commit_recovered_completed_item_removal(
            journal,
            item_id=item.item_id,
            live_evidence=evidence,
            post_import_action=post_import_action,
        )
        live_lease.revalidate()
        _require_authority(authority)
        return saved


def _recover_resolving(authority, journal, item, _acknowledge_completion):
    owner = RecoveryOwner(journal.operation_id, item.item_id)
    reference = _single_reference(item, _MANAGED_CARRIER_KIND)
    if reference is None:
        _block_resolving(
            authority,
            journal,
            item,
            "managed-carrier-reference-unavailable",
        )
        return False
    inspection = inspect_managed_carrier(reference.data, owner)
    outcome = inspection.outcome
    if outcome is not ManagedCarrierInspectionOutcome.SEALED:
        reasons = {
            ManagedCarrierInspectionOutcome.UNSEALED_ORIGIN: "managed-carrier-unsealed-origin",
            ManagedCarrierInspectionOutcome.UNSEALED_ACTIVITY: "managed-carrier-unsealed-activity",
            ManagedCarrierInspectionOutcome.UNAVAILABLE: "managed-carrier-unavailable",
        }
        _block_resolving(
            authority,
            journal,
            item,
            reasons.get(outcome, "managed-carrier-unavailable"),
        )
        return False
    completed = None
    current_journal = journal
    try:
        post_dir = None
        backup_references = _references(item, _LIBRARY_BACKUP_KINDS)
        if (
            len(backup_references) == 1
            and backup_references[0].kind == _LIBRARY_BACKUP_CARRIER_KIND
        ):
            _require_authority(authority)
            with reopen_managed_evidence(reference.data, owner) as managed_lease:
                managed = managed_lease.revalidate()
                if managed.manifest_hash != inspection.manifest_hash:
                    raise ManagedEvidenceUnavailable(
                        "managed carrier changed after inspection"
                    )
                post_dir = Path(managed.library_root) / managed.album_path
            _require_authority(authority)

        backup_resolution = prepare_library_backup_settlement(
            current_journal,
            item.item_id,
            owner,
            item.planned["album"],
            post_dir,
            authority_check=lambda: _require_authority(authority),
        )
        current_journal = backup_resolution.journal
        if backup_resolution.status is LibraryBackupResolutionStatus.ATTENTION:
            _block_resolving(
                authority,
                current_journal,
                _find_item(current_journal, item.item_id),
                backup_resolution.reason or "library-backup-unsettled",
            )
            return False

        with _capture_live_completion(
            current_journal,
            _find_item(current_journal, item.item_id),
            expected_manifest_hash=inspection.manifest_hash,
        ) as (managed_lease, live_lease, evidence):
            _require_authority(authority)
            backup_resolution = finish_library_backup_settlement(
                current_journal,
                item.item_id,
                owner,
                item.planned["album"],
                authority_check=lambda: _require_authority(authority),
            )
            current_journal = backup_resolution.journal
            if backup_resolution.status is LibraryBackupResolutionStatus.ATTENTION:
                _block_resolving(
                    authority,
                    current_journal,
                    _find_item(current_journal, item.item_id),
                    backup_resolution.reason
                    or "library-backup-settlement-unavailable",
                )
                return False
            if (
                managed_lease.revalidate().manifest_hash
                != inspection.manifest_hash
                or live_lease.revalidate() != evidence
            ):
                raise ManagedEvidenceUnavailable(
                    "managed completion changed during backup settlement"
                )
            _require_authority(authority)
            completed = queue_state.transition_journal_item(
                current_journal,
                item.item_id,
                queue_state.QueuePhase.COMPLETE,
                completion_evidence=evidence.to_record(),
            )
            if (
                managed_lease.revalidate().manifest_hash != inspection.manifest_hash
                or live_lease.revalidate() != evidence
            ):
                raise ManagedEvidenceUnavailable(
                    "managed completion changed during journal publication"
                )
            _require_authority(authority)
            post_dir = Path(evidence.library_root) / evidence.album_path
            post_import_action = plan_post_import_action(
                completed,
                item.item_id,
                post_dir,
                authority=authority,
            )
            _require_authority(authority)
            live_lease.revalidate()
            queue_state.commit_recovered_completed_item_removal(
                completed,
                item_id=item.item_id,
                live_evidence=evidence,
                post_import_action=post_import_action,
            )
            live_lease.revalidate()
            _require_authority(authority)
        return True
    except (LiveCompletionUnavailable, ManagedEvidenceUnavailable):
        if completed is not None:
            raise
        _block_resolving(
            authority,
            current_journal,
            _find_item(current_journal, item.item_id),
            "managed-completion-proof-unavailable",
        )
        return False


def _recover_active_reservations(authority, journals):
    changed = False
    reasons = {
        ManagedReservationInspectionOutcome.ABSENT: "managed-reservation-absent",
        ManagedReservationInspectionOutcome.ORIGIN: "managed-reservation-origin",
        ManagedReservationInspectionOutcome.ACTIVITY: "managed-reservation-activity",
        ManagedReservationInspectionOutcome.UNAVAILABLE: "managed-reservation-unavailable",
    }
    for journal in journals:
        current = journal
        for item in sorted(journal.items, key=lambda value: value.item_id):
            if item.phase is not queue_state.QueuePhase.ACTIVE:
                continue
            reference = _single_reference(item, _MANAGED_RESERVATION_KIND)
            if reference is None:
                continue
            owner = RecoveryOwner(journal.operation_id, item.item_id)
            _require_authority(authority)
            inspection = inspect_managed_reservation(reference.data, owner)
            _require_authority(authority)
            reason = reasons.get(inspection.outcome)
            if reason is None:
                reason = "managed-reservation-unavailable"
            current = queue_state.transition_journal_item(
                current,
                item.item_id,
                queue_state.QueuePhase.BLOCKED,
                block_reason=reason,
            )
            _require_authority(authority)
            changed = True
    return changed


def _recover_active_library_backups(authority, journals):
    """Adopt an exact backup carrier, then stop before any work can resume."""
    changed = False
    for journal in journals:
        current = journal
        for item_id in sorted(item.item_id for item in journal.items):
            item = _find_item(current, item_id)
            if item.phase is not queue_state.QueuePhase.ACTIVE:
                continue
            references = _references(item, _LIBRARY_BACKUP_KINDS)
            if len(references) != 1:
                continue
            reference = references[0]
            if reference.kind not in {
                _LIBRARY_BACKUP_INTENT_KIND,
                _LIBRARY_BACKUP_CARRIER_KIND,
            }:
                continue

            owner = RecoveryOwner(current.operation_id, item_id)
            owner_data = {
                "operation_id": owner.operation_id,
                "item_id": owner.item_id,
            }
            backup = None
            reason = "library-backup-carrier-unavailable"
            if reference.kind == _LIBRARY_BACKUP_INTENT_KIND:
                _require_authority(authority)
                backup = load_backup_result(
                    reference.data["path"],
                    expected_owner=owner_data,
                )
                _require_authority(authority)
                record = library_backup_record(
                    backup,
                    expected_owner=owner_data,
                )
                if record is None:
                    reason = "library-backup-intent-unsettled"
                else:
                    _require_authority(authority)
                    current = queue_state.promote_library_backup_carrier(
                        current,
                        item_id,
                        reference,
                        record,
                    )
                    _require_authority(authority)
                    item = _find_item(current, item_id)
                    reference = _single_reference(
                        item,
                        _LIBRARY_BACKUP_CARRIER_KIND,
                    )
                    changed = True
            else:
                _require_authority(authority)
                backup = load_library_backup_record(
                    reference.data,
                    expected_owner=owner_data,
                )
                _require_authority(authority)

            if backup is not None:
                _require_authority(authority)
                pinned = pin_unverified_upgrade_backup(
                    backup,
                    "queue backup kept — restart recovery needs exact review",
                    expected_owner=owner_data,
                )
                _require_authority(authority)
                if not pinned:
                    warn_pin_failed(backup.path)
                reason = (
                    "library-backup-carrier-awaiting-recovery"
                    if backup.complete is True
                    else "library-backup-incomplete"
                )

            _require_authority(authority)
            current = queue_state.transition_journal_item(
                current,
                item_id,
                queue_state.QueuePhase.BLOCKED,
                block_reason=reason,
            )
            _require_authority(authority)
            changed = True
    return changed


def _settled_result(action):
    if action is BlockedItemSettlementAction.RETRY:
        return BlockedItemSettlementResult(
            BlockedItemSettlementStatus.RETRYABLE,
            "The item is ready to retry.",
        )
    return BlockedItemSettlementResult(
        BlockedItemSettlementStatus.DISCARDED,
        "The blocked item was discarded safely.",
    )


def _blocked_settlement(reason):
    return BlockedItemSettlementResult(
        BlockedItemSettlementStatus.BLOCKED,
        reason,
    )


def _continue_prelaunch_settlement(
    authority,
    journal,
    item,
    *,
    requested_action=None,
):
    reference = _single_reference(item, _MANAGED_SETTLEMENT_KIND)
    if reference is None:
        return _blocked_settlement("This item has no authorised pre-launch settlement to continue.")
    try:
        action = BlockedItemSettlementAction(reference.data["action"])
        carrier = reference.data["carrier"]
        manifest_hash = reference.data["manifest_hash"]
    except (KeyError, TypeError, ValueError):
        return _blocked_settlement(
            "The saved settlement authority is invalid, so the item remains blocked."
        )
    if requested_action is not None and action is not requested_action:
        return _blocked_settlement(
            "This item already has a different settlement decision in progress."
        )
    owner = RecoveryOwner(journal.operation_id, item.item_id)
    _require_authority(authority)
    retirement = retire_prelaunch_managed_carrier(
        carrier,
        owner,
        manifest_hash,
    )
    _require_authority(authority)
    if retirement.outcome not in {
        ManagedCarrierRetirementOutcome.RETIRED,
        ManagedCarrierRetirementOutcome.ALREADY_ABSENT,
    }:
        return _blocked_settlement(
            "The pre-launch Beets state could not be retired safely. The item remains blocked."
        )
    try:
        queue_state._finish_prelaunch_managed_settlement(
            journal,
            item_id=item.item_id,
            settlement_reference=reference,
            action=action.value,
        )
    except (OSError, queue_state.QueueJournalError) as exc:
        raise _SettlementJournalFailure(
            "The pre-launch state was retired, but its journal update failed."
        ) from exc
    _require_authority(authority)
    return _settled_result(action)


def _discard_parked_item_staging(authority, journal, item):
    """Throw away a blocked item's staged download on user authority."""
    owner = {"operation_id": journal.operation_id, "item_id": item.item_id}
    for reference in _staging_references(item):
        _require_authority(authority)
        inspection = _staging_inspection(reference, owner)
        _require_authority(authority)
        status = getattr(inspection, "status", None)
        if status is StagingReferenceStatus.ABSENT:
            continue
        if status is not StagingReferenceStatus.MATCH:
            return None
        if reference.kind == _STAGING_RUN_KIND:
            # A crash leaves the run root itself behind, never parked. Park
            # it the way a deliberate stop would — the park refuses while any
            # writer still holds a descriptor — then discard the parked copy.
            # The item stays BLOCKED throughout: that is the phase a settlement
            # runs in, and a crash mid-park leaves it in the state the next
            # recovery pass already looks for.
            run = staging_run_from_record(reference.data)
            if run is None:
                return None

            def checkpoint(record, item_id=item.item_id):
                nonlocal journal
                journal = queue_state.append_staging_group_intent(
                    journal,
                    item_id,
                    record,
                )

            _require_authority(authority)
            retained = retain_staging_run(
                run,
                label="discarded",
                on_intent=checkpoint,
            )
            _require_authority(authority)
            if retained is None or not discard_group(
                retained, expected_owner=owner
            ):
                return None
            continue
        path = reference.data.get("path")
        if not isinstance(path, str):
            return None
        _require_authority(authority)
        discarded = discard_group(
            Path(path), expected_owner=owner
        ) or discard_file_group(Path(path), expected_owner=owner)
        _require_authority(authority)
        if not discarded:
            return None
    current, current_item, reason, _changed = _reconcile_item_staging(
        authority,
        journal,
        _find_item(journal, item.item_id),
        block_unsettled=False,
    )
    if reason is not None:
        return None
    return current, current_item


def _settle_unstarted_download(authority, journal, item, action):
    """Finish settling a download that blocked before any library mutation."""
    frozen = parse_completion_input_record(
        item.completion_input,
        expected_owner=RecoveryOwner(journal.operation_id, item.item_id),
    )
    if frozen is None or frozen.lineages or frozen.counts is not None:
        return _blocked_settlement(
            "This item has no exact pre-launch Beets state to settle."
        )
    _require_authority(authority)
    journal = queue_state.transition_journal_item(
        journal,
        item.item_id,
        queue_state.QueuePhase.ACTIVE,
    )
    _require_authority(authority)
    if action is BlockedItemSettlementAction.RETRY:
        return _settled_result(action)
    if len(journal.items) != 1 or journal.retirements:
        return _blocked_settlement(
            "Only a single-download operation can be discarded here."
        )
    try:
        _require_authority(authority)
        journal = queue_state.reset_unstarted_item_to_pending(journal, item.item_id)
        _require_authority(authority)
        queue_state.clear_queue_journal(journal.operation_id, explicit_discard=True)
        _require_authority(authority)
    except (OSError, ValueError, queue_state.QueueJournalError):
        return _blocked_settlement(
            "The discarded download could not be cleared from the saved queue."
        )
    return _settled_result(action)


def settle_blocked_item(
    *,
    authority: RunLockLease,
    operation_id: str,
    item_id: str,
    action: BlockedItemSettlementAction,
) -> BlockedItemSettlementResult:
    """Retry or discard a live-proved pre-launch abort: a parked download
    or a managed Beets state that never launched."""
    if type(action) is not BlockedItemSettlementAction:
        raise ValueError("action must be retry or discard")
    try:
        _require_authority(authority)
        loaded = queue_state.load_queue_journal(operation_id)
        _require_authority(authority)
        journal = loaded.journal
        if loaded.status is not queue_state.QueueLoadStatus.READY or journal is None:
            return _blocked_settlement("The saved queue operation could not be loaded safely.")
        item = next(
            (value for value in journal.items if value.item_id == item_id),
            None,
        )
        if item is None:
            return _blocked_settlement("The blocked item no longer exists.")
        if item.phase is not queue_state.QueuePhase.BLOCKED:
            return _blocked_settlement("Only a blocked item can be settled.")

        journal, item, staging_reason, _changed = _reconcile_item_staging(
            authority,
            journal,
            item,
            block_unsettled=False,
        )
        if staging_reason is not None:
            settled_staging = _discard_parked_item_staging(authority, journal, item)
            if settled_staging is None:
                return _blocked_settlement(
                    "Staged download recovery state is still present or could not "
                    "be proved absent, so this item remains blocked."
                )
            journal, item = settled_staging

        managed_references = _references(item, _MANAGED_KINDS)
        reference = managed_references[0] if len(managed_references) == 1 else None
        if reference is None:
            if item.recovery_references:
                return _blocked_settlement(
                    "This item has no exact pre-launch Beets state to settle."
                )
            return _settle_unstarted_download(authority, journal, item, action)
        if reference.kind == _MANAGED_SETTLEMENT_KIND:
            return _continue_prelaunch_settlement(
                authority,
                journal,
                item,
                requested_action=action,
            )

        owner = RecoveryOwner(journal.operation_id, item.item_id)
        carrier = None
        manifest_hash = None
        if reference.kind == _MANAGED_RESERVATION_KIND:
            _require_authority(authority)
            inspection = inspect_managed_reservation(reference.data, owner)
            _require_authority(authority)
            if inspection.outcome is ManagedReservationInspectionOutcome.ABSENT:
                queue_state._commit_absent_managed_settlement(
                    journal,
                    item_id=item.item_id,
                    reservation=reference,
                    action=action.value,
                )
                _require_authority(authority)
                return _settled_result(action)
            if inspection.outcome is ManagedReservationInspectionOutcome.ORIGIN:
                carrier = inspection.carrier
                manifest_hash = inspection.manifest_hash
            elif inspection.outcome is ManagedReservationInspectionOutcome.ACTIVITY:
                return _blocked_settlement(
                    "Beets may have started or changed the library, so this item remains blocked."
                )
            else:
                return _blocked_settlement(
                    "The app could not prove that Beets stayed pre-launch, so "
                    "this item remains blocked."
                )
        elif reference.kind == _MANAGED_CARRIER_KIND:
            _require_authority(authority)
            inspection = inspect_managed_carrier(reference.data, owner)
            _require_authority(authority)
            if inspection.outcome is ManagedCarrierInspectionOutcome.UNSEALED_ORIGIN:
                carrier = reference.data
                manifest_hash = inspection.manifest_hash
            elif inspection.outcome in {
                ManagedCarrierInspectionOutcome.SEALED,
                ManagedCarrierInspectionOutcome.UNSEALED_ACTIVITY,
            }:
                return _blocked_settlement(
                    "Beets may have started or changed the library, so this item remains blocked."
                )
            else:
                return _blocked_settlement(
                    "The app could not prove that Beets stayed pre-launch, so "
                    "this item remains blocked."
                )
        else:
            return _blocked_settlement("This blocked item is not an exact pre-launch Beets abort.")

        if type(carrier) is not dict or not isinstance(manifest_hash, str):
            return _blocked_settlement(
                "The app could not prove the exact pre-launch Beets state, so "
                "this item remains blocked."
            )
        committed = queue_state._begin_prelaunch_managed_settlement(
            journal,
            item_id=item.item_id,
            source_reference=reference,
            carrier=carrier,
            manifest_hash=manifest_hash,
            action=action.value,
        )
        _require_authority(authority)
        committed_item = next(value for value in committed.items if value.item_id == item.item_id)
        return _continue_prelaunch_settlement(
            authority,
            committed,
            committed_item,
            requested_action=action,
        )
    except _AuthorityLost:
        return _blocked_settlement("Run-lock authority was lost, so the item remains blocked.")


def recover_startup_state(
    *,
    authority: RunLockLease,
    acknowledge_completion=None,
) -> StartupRecoveryResult:
    """Settle only exact restart evidence; never run downloads or imports."""
    try:
        _require_authority(authority)
        relocation = reconcile_post_import_relocations(
            authority=authority,
            handoff_matches=_combined_post_import_relocation_handoff_matches,
        )
        _require_authority(authority)
        if relocation.status is not RelocationRecoveryStatus.CLEAR:
            paths = "; ".join(os.fspath(path) for path in relocation.paths)
            logging.getLogger("qobuz_librarian").error(
                "%s: %s. Paths needing attention: %s.",
                POST_IMPORT_RELOCATION_LOG_ENTRY,
                relocation.reason or "reason not reported",
                paths or "none reported",
            )
            return StartupRecoveryResult(
                StartupRecoveryStatus.ATTENTION_REQUIRED,
                reason="post-import-relocation-unsettled",
                post_import_relocation=relocation,
            )
        while True:
            loads = _load_namespace(authority)
            if any(loaded.status is queue_state.QueueLoadStatus.BLOCKED for loaded in loads):
                return StartupRecoveryResult(
                    StartupRecoveryStatus.ATTENTION_REQUIRED,
                    reason="queue-namespace-blocked",
                )
            journals = _sorted_journals(loads)
            _require_authority(authority)
            if _recover_active_library_backups(authority, journals):
                continue
            unclaimed = _unclaimed_staging_run_names(journals)
            if unclaimed:
                _require_authority(authority)
                if _retain_unclaimed_staging_runs(authority, unclaimed):
                    continue
                return StartupRecoveryResult(
                    StartupRecoveryStatus.ATTENTION_REQUIRED,
                    reason="unclaimed-staging-run",
                )
            _require_authority(authority)
            if _recover_staging_references(authority, journals):
                continue
            settlement = next(
                (
                    (journal, item)
                    for journal in journals
                    for item in sorted(journal.items, key=lambda value: value.item_id)
                    if (
                        item.phase is queue_state.QueuePhase.BLOCKED
                        and _single_reference(item, _MANAGED_SETTLEMENT_KIND) is not None
                    )
                ),
                None,
            )
            if settlement is not None:
                settled = _continue_prelaunch_settlement(
                    authority,
                    *settlement,
                )
                if settled.status in {
                    BlockedItemSettlementStatus.RETRYABLE,
                    BlockedItemSettlementStatus.DISCARDED,
                }:
                    continue
                return StartupRecoveryResult(
                    StartupRecoveryStatus.ATTENTION_REQUIRED,
                    _classify(journals),
                    "prelaunch-settlement-unsettled",
                )
            classified = _classify(journals)
            if any(item.phase is queue_state.QueuePhase.BLOCKED for item in classified):
                if _recover_active_reservations(authority, journals):
                    loads = _load_namespace(authority)
                    journals = _sorted_journals(loads)
                    classified = _classify(journals)
                return StartupRecoveryResult(
                    StartupRecoveryStatus.ATTENTION_REQUIRED,
                    classified,
                    "queue-item-blocked",
                )

            retirement = next(
                (
                    (journal, item)
                    for journal in journals
                    for item in sorted(
                        journal.retirements,
                        key=lambda value: value.item_id,
                    )
                ),
                None,
            )
            if retirement is not None:
                journal, item = retirement
                _require_authority(authority)
                _saved, _final_path, result = finalize_carrier_retirement(
                    journal,
                    item.item_id,
                    authority=authority,
                    acknowledge_completion=acknowledge_completion,
                )
                _require_authority(authority)
                if result.outcome not in {
                    ManagedCarrierRetirementOutcome.RETIRED,
                    ManagedCarrierRetirementOutcome.ALREADY_ABSENT,
                }:
                    return StartupRecoveryResult(
                        StartupRecoveryStatus.ATTENTION_REQUIRED,
                        classified,
                        "carrier-retirement-unsettled",
                    )
                continue

            complete = next(
                (
                    (journal, item)
                    for journal in journals
                    for item in sorted(journal.items, key=lambda value: value.item_id)
                    if item.phase is queue_state.QueuePhase.COMPLETE
                ),
                None,
            )
            if complete is not None:
                if _needs_cli_completion_caller(
                    *complete,
                    acknowledge_completion,
                ):
                    return StartupRecoveryResult(
                        StartupRecoveryStatus.RESUME_REQUIRED,
                        _classify(journals),
                    )
                _recover_complete(
                    authority,
                    *complete,
                    acknowledge_completion,
                )
                continue

            resolving = next(
                (
                    (journal, item)
                    for journal in journals
                    for item in sorted(journal.items, key=lambda value: value.item_id)
                    if item.phase is queue_state.QueuePhase.RESOLVING
                ),
                None,
            )
            if resolving is not None:
                if _needs_cli_completion_caller(
                    *resolving,
                    acknowledge_completion,
                ):
                    return StartupRecoveryResult(
                        StartupRecoveryStatus.RESUME_REQUIRED,
                        _classify(journals),
                    )
                if _recover_resolving(
                    authority,
                    *resolving,
                    acknowledge_completion,
                ):
                    continue
                loads = _load_namespace(authority)
                journals = _sorted_journals(loads)
                _recover_active_reservations(authority, journals)
                loads = _load_namespace(authority)
                journals = _sorted_journals(loads)
                return StartupRecoveryResult(
                    StartupRecoveryStatus.ATTENTION_REQUIRED,
                    _classify(journals),
                    "queue-item-blocked",
                )

            if _recover_active_reservations(authority, journals):
                loads = _load_namespace(authority)
                journals = _sorted_journals(loads)
                return StartupRecoveryResult(
                    StartupRecoveryStatus.ATTENTION_REQUIRED,
                    _classify(journals),
                    "queue-item-blocked",
                )

            if _block_inconsistent_active(authority, journals):
                continue

            empty = next(
                (journal for journal in journals if not journal.items and not journal.retirements),
                None,
            )
            if empty is not None:
                _require_authority(authority)
                queue_state.clear_queue_journal(empty.operation_id)
                _require_authority(authority)
                continue

            classified = _classify(journals)
            if classified:
                return StartupRecoveryResult(
                    StartupRecoveryStatus.RESUME_REQUIRED,
                    classified,
                )
            return StartupRecoveryResult(StartupRecoveryStatus.CLEAR)
    except _AuthorityLost:
        return StartupRecoveryResult(
            StartupRecoveryStatus.ATTENTION_REQUIRED,
            reason="run-lock-authority-lost",
        )
    except (
        IndexError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        queue_state.QueueJournalError,
    ):
        return StartupRecoveryResult(
            StartupRecoveryStatus.ATTENTION_REQUIRED,
            reason="startup-recovery-unsettled",
        )
