"""UI-free restart recovery for durable queue operations."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
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
    clear_stopped_import_folders,
    forget_staged_beets_rows,
    inspect_managed_carrier,
    inspect_managed_reservation,
    managed_sources_untouched,
    reopen_managed_evidence,
    retire_prelaunch_managed_carrier,
)
from qobuz_librarian.integrations.staging import (
    StagingReferenceStatus,
    bind_unclaimed_staging_run,
    capture_file,
    capture_staging_run,
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
    release_backup_owner,
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
    restore_library_backup,
)
from qobuz_librarian.queue.post_import_finalizer import (
    finalize_carrier_retirement,
    plan_post_import_action,
)
from qobuz_librarian.run_lock import RunLockLease
from qobuz_librarian.ui_cli.errors import plural

log = logging.getLogger("qobuz_librarian")

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
BLOCKED_DOWNLOAD_LOG_ENTRY = (
    "Interrupted download recovery needs attention"
)
UNREADABLE_QUEUE_LOG_ENTRY = "The saved download queue could not be read"


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
class SettledDownload:
    """What start-up did with one download a restart cut off.

    ``outcome`` is "requeue" when nothing of it reached the library and it
    can run again, "checked" when Beets had filed it and it is being proved
    again, "kept" when what Beets filed stays without that proof, and
    "set-aside" when Beets stopped part way and the rest was set aside.
    """

    operation_id: str
    mode: str
    item_id: str
    album_id: str | None
    label: str
    outcome: str
    reason: str


@dataclass(frozen=True, slots=True)
class StartupRecoveryResult:
    status: StartupRecoveryStatus
    items: tuple[StartupRecoveryItem, ...] = ()
    reason: str | None = None
    post_import_relocation: RelocationRecoveryResult | None = None
    paths: tuple[Path, ...] = ()
    settled: tuple[SettledDownload, ...] = ()


@dataclass(frozen=True, slots=True)
class BlockedItemSettlementResult:
    status: BlockedItemSettlementStatus
    reason: str


class _AuthorityLost(RuntimeError):
    pass


class _SettlementJournalFailure(RuntimeError):
    """A committed operator decision could not be written back durably."""


def _require_authority(authority: RunLockLease) -> None:
    run_lock.require_authority(authority, _AuthorityLost)


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


def _unreadable_staged_path(paths):
    """The first entry under ``paths`` that can't be opened for reading, or
    the first path when each one opens."""
    roots = [Path(path) for path in paths if isinstance(path, (str, Path))]
    for root in roots:
        folders = [root]
        while folders:
            folder = folders.pop()
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                return folder
            for name in names:
                path = folder / name
                if path.is_dir() and not path.is_symlink():
                    folders.append(path)
                    continue
                try:
                    os.close(os.open(
                        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK))
                except OSError:
                    return path
    return roots[0] if roots else None


def _check_staged(paths):
    path = _unreadable_staged_path(paths)
    return "" if path is None else f" Check {path}."


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
        "queue backup kept - restart recovery requires attention",
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


def unclaimed_staging_run_names():
    """Run roots no saved queue claims, or None when a queue cannot be read."""
    loads = queue_state.list_queue_journals()
    if any(loaded.status is queue_state.QueueLoadStatus.BLOCKED for loaded in loads):
        return None
    return _unclaimed_staging_run_names(_sorted_journals(loads))


def saved_queue_operation_ids():
    """Every saved queue's operation ID, or None when a queue cannot be read."""
    loads = queue_state.list_queue_journals()
    if any(loaded.status is queue_state.QueueLoadStatus.BLOCKED for loaded in loads):
        return None
    return {journal.operation_id for journal in _sorted_journals(loads)}


def _retain_unclaimed_staging_runs(authority, names, *, discard) -> bool:
    """Clear run roots no journal claims; the terminal lane leaves them behind.

    The boot holder owns the run lock, so no live rip can be writing these.
    With ``discard`` a folder is removed once no Beets row points into it;
    otherwise, or when those rows can't be dropped, it is moved aside for
    review. A refusal (a file changed while it was checked) falls back to the
    attention stop instead of touching the tree.
    """
    settled = True
    for name in names:
        _require_authority(authority)
        run = bind_unclaimed_staging_run(name)
        current = capture_staging_run(run) if run is not None else None
        # The rows name the files where they are now, so they go first.
        forgotten = discard and current is not None and forget_staged_beets_rows(
            (current.path / relative for relative, _identity in current.files),
            folder=current.path,
        )
        _require_authority(authority)
        try:
            group = (
                retain_staging_run(
                    run, label="discarded" if forgotten else "abandoned")
                if current is not None else None
            )
        except OSError:
            group = None
        if group is None:
            settled = False
            continue
        if forgotten and discard_group(group):
            log.warning(
                "Removed %s, a download folder an interrupted run left in "
                "staging.",
                name,
            )
            continue
        log.warning(
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


def _waits_for_web_app(journal, retirement, acknowledge_completion):
    """Whether this finished download waits for the web app to record it in
    its job, which a run with no acknowledger cannot do."""
    if acknowledge_completion is not None or retirement.completion_acknowledged:
        return False
    completion_input = parse_completion_input_record(
        retirement.completion_input,
        expected_owner=RecoveryOwner(journal.operation_id, retirement.item_id),
    )
    return (
        completion_input is not None
        and completion_input.origin.kind is CompletionOriginKind.WEB_JOB
    )


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
        completion_evidence=None,
    )
    _require_authority(authority)


def _recover_complete(authority, journal, item, _acknowledge_completion):
    saved = None
    try:
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
    except (LiveCompletionUnavailable, ManagedEvidenceUnavailable):
        if saved is not None:
            raise
        _block_resolving(
            authority,
            journal,
            item,
            "completed-item-proof-unavailable",
        )
        return None


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

        with _capture_live_completion(
            current_journal,
            _find_item(current_journal, item.item_id),
            expected_manifest_hash=inspection.manifest_hash,
        ) as (managed_lease, live_lease, evidence):
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
                    "queue backup kept - restart recovery needs exact review",
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
            "The record of this Beets import could not be retired safely. The "
            "item remains blocked."
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
    present = []
    for reference in _staging_references(item):
        _require_authority(authority)
        inspection = _staging_inspection(reference, owner)
        _require_authority(authority)
        status = getattr(inspection, "status", None)
        if status is StagingReferenceStatus.MATCH:
            present.append(reference)
        elif status is not StagingReferenceStatus.ABSENT:
            return None
    for reference in present:
        if reference.kind == _STAGING_RUN_KIND:
            # A crash leaves the run root itself behind, never parked. Park
            # it the way a deliberate stop would - the park refuses if a file
            # changes while it is checked - then discard the parked copy.
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


def _put_back_library_backup(authority, journal, item):
    """Return the tracks a blocked download moved aside before settling it."""
    references = _references(item, _LIBRARY_BACKUP_KINDS)
    if not references:
        return journal, item
    paths = references[0].data.get("carrier", references[0].data)
    _require_authority(authority)
    restored = restore_library_backup(
        journal,
        item.item_id,
        RecoveryOwner(journal.operation_id, item.item_id),
        authority_check=lambda: _require_authority(authority),
    )
    _require_authority(authority)
    if restored.status is LibraryBackupResolutionStatus.SETTLED:
        return restored.journal, _find_item(restored.journal, item.item_id)
    reason = restored.reason
    if item.block_reason != reason:
        queue_state.transition_journal_item(
            journal,
            item.item_id,
            queue_state.QueuePhase.BLOCKED,
            block_reason=reason,
        )
        _require_authority(authority)
    if reason.startswith("library-backup-restore-conflict:"):
        return _blocked_settlement(
            f"A different file is already in {paths['origin']}, so the tracks "
            f"backed up to {paths['path']} were not put back and this item "
            "remains blocked. Move that file out of the album folder, then "
            "try again."
        )
    if reason.startswith("library-backup-origin-missing:"):
        return _blocked_settlement(
            f"{Path(paths['origin']).parent} is missing, so this item remains "
            f"blocked and anything it set aside stays in {paths['path']}. "
            "Check that the music folder is mounted; if it is and that "
            "folder was removed, create it again. Then restart Qobuz "
            "Librarian."
        )
    if reason == "library-backup-intent-unsettled":
        return _blocked_settlement(
            f"The backup this item began at {paths['path']} was not "
            "finished and could not be cleared, so this item remains "
            "blocked. Check that the backup folder is mounted; anything of "
            f"the album missing from {paths['origin']} is at that path. "
            "Then restart Qobuz Librarian."
        )
    return _blocked_settlement(
        f"The tracks backed up to {paths['path']} could not be put back into "
        f"{paths['origin']}, so this item remains blocked. Check that both "
        "folders are writable and have free space, then try again."
    )


def _settle_unstarted_download(authority, journal, item, action):
    """Finish settling a download that blocked before any library mutation."""
    frozen = parse_completion_input_record(
        item.completion_input,
        expected_owner=RecoveryOwner(journal.operation_id, item.item_id),
    )
    if frozen is None:
        return _blocked_settlement(
            "This item has no exact pre-launch Beets state to settle."
        )
    discard_one_of_several = (
        action is BlockedItemSettlementAction.DISCARD
        and (len(journal.items) != 1 or bool(journal.retirements))
    )
    if frozen.lineages or frozen.counts is not None or discard_one_of_several:
        # Staged but never reserved for Beets, or one item dropped from a
        # longer queue, with every reference already settled: nothing of it
        # is left to resume.
        _require_authority(authority)
        queue_state._commit_unreferenced_blocked_settlement(
            journal,
            item_id=item.item_id,
            action=action.value,
        )
        _require_authority(authority)
        return _settled_result(action)
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


def discard_unstarted_item(
    *,
    authority: RunLockLease,
    operation_id: str,
    item_id: str,
) -> BlockedItemSettlementResult:
    """Discard a single download that is waiting to resume but never staged
    anything. Only its saved queue entry is removed."""
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
            return _blocked_settlement("The interrupted item no longer exists.")
        if len(journal.items) != 1 or journal.retirements:
            return _blocked_settlement(
                "Only a single-download operation can be discarded here."
            )
        try:
            if item.phase is queue_state.QueuePhase.ACTIVE:
                journal = queue_state.reset_unstarted_item_to_pending(
                    journal, item.item_id
                )
                _require_authority(authority)
            queue_state.clear_queue_journal(journal.operation_id, explicit_discard=True)
            _require_authority(authority)
        except (KeyError, OSError, ValueError, queue_state.QueueJournalError):
            return _blocked_settlement(
                "The discarded download could not be cleared from the saved queue."
            )
        return _settled_result(BlockedItemSettlementAction.DISCARD)
    except _AuthorityLost:
        return _blocked_settlement("Run-lock authority was lost, so the item remains saved.")


_IMPORT_FILED = "filed"
_IMPORT_UNTOUCHED = "untouched"
_IMPORT_PARTIAL = "partial"


def _launched_import(operation_id, item):
    """How far the item's started Beets import got, from its carrier.

    Returns (state, manifest hash, snapshot), or None when the item holds
    no carrier of an import that started. Filed means Beets finished and
    sealed its record; untouched, that it stopped before moving any file;
    partial, anything between.
    """
    references = _references(item, {_MANAGED_CARRIER_KIND})
    if len(references) != 1:
        return None
    inspection = inspect_managed_carrier(
        references[0].data,
        RecoveryOwner(operation_id, item.item_id),
    )
    if inspection.outcome is ManagedCarrierInspectionOutcome.SEALED:
        state = _IMPORT_FILED
    elif inspection.outcome is not ManagedCarrierInspectionOutcome.UNSEALED_ACTIVITY:
        return None
    elif managed_sources_untouched(inspection.snapshot):
        state = _IMPORT_UNTOUCHED
    else:
        state = _IMPORT_PARTIAL
    return state, inspection.manifest_hash, inspection.snapshot


def _partial_import_reason(item, snapshot):
    """Where a download Beets stopped part-way through filing has left things."""
    intent = snapshot.get("intent") or ()
    staged = sum(
        capture_file(record["path"], expected=tuple(record["identity"])) is not None
        for record in intent
    )
    staging = next(
        (
            reference.data.get("path")
            for reference in _staging_references(item)
            if isinstance(reference.data.get("path"), str)
        ),
        None,
    )
    if staged == 0:
        reason = (
            "Beets moved this album's tracks into your library but stopped "
            "before it finished recording them."
        )
    else:
        reason = (
            "Beets stopped part-way through filing this album: "
            f"{staged} of {len(intent)} tracks are still in "
            f"{staging or 'staging'} and the others are in your library."
        )
    for reference in _references(item, _LIBRARY_BACKUP_KINDS):
        paths = reference.data.get("carrier", reference.data)
        path = paths.get("path") if isinstance(paths, dict) else None
        if isinstance(path, str):
            reason += f" The tracks it replaced are kept in {path}."
            break
    return reason


def _check_import_again(authority, journal, item):
    """Send a finished import back to be proved once more."""
    _require_authority(authority)
    queue_state.transition_journal_item(
        journal,
        item.item_id,
        queue_state.QueuePhase.RESOLVING,
    )
    _require_authority(authority)
    return BlockedItemSettlementResult(
        BlockedItemSettlementStatus.RETRYABLE,
        "Beets finished filing this album; it will be checked again.",
    )


def _keep_backup_for_settings(authority, journal, item, note):
    """Pin the tracks a download moved aside and hand the backup to Settings,
    where it can be restored or removed once this download is cleared.

    Returns (reference, backup), (None, None) when the item holds no backup,
    or a refusal.
    """
    backups = _references(item, _LIBRARY_BACKUP_KINDS)
    if not backups:
        return None, None
    if len(backups) != 1 or backups[0].kind != _LIBRARY_BACKUP_CARRIER_KIND:
        return _blocked_settlement(
            "The tracks this download moved aside are still being settled, "
            "so this item remains blocked."
        )
    owner = {"operation_id": journal.operation_id, "item_id": item.item_id}
    data = backups[0].data
    _require_authority(authority)
    backup = load_library_backup_record(data, expected_owner=owner)
    keeper = owner
    if backup is None:
        # A settlement that stopped after handing the backup over.
        backup = load_backup_result(data.get("path", ""))
        receipt = data.get("receipt") if isinstance(data, dict) else None
        if (
            backup is None
            or "owner" in backup.receipt
            or not isinstance(receipt, dict)
            or backup.receipt.get("token") != receipt.get("token")
        ):
            return _blocked_settlement(
                "The tracks this download moved aside could not be found "
                "where they were kept, so this item remains blocked."
            )
        keeper = None
    if not pin_unverified_upgrade_backup(backup, note, expected_owner=keeper) or (
        keeper is not None
        and not release_backup_owner(backup, expected_owner=keeper)
    ):
        return _blocked_settlement(
            "The tracks this download moved aside could not be marked to "
            "keep, so this item remains blocked."
        )
    _require_authority(authority)
    return backups[0], backup


def _retire_cleared_download(authority, journal, item, reference, manifest_hash,
                             backup_reference):
    """Retire the Beets record of a cleared download and drop the item."""
    committed = queue_state._begin_prelaunch_managed_settlement(
        journal,
        item_id=item.item_id,
        source_reference=reference,
        carrier=reference.data,
        manifest_hash=manifest_hash,
        action=BlockedItemSettlementAction.DISCARD.value,
        kept_backup=backup_reference,
    )
    _require_authority(authority)
    return _continue_prelaunch_settlement(
        authority,
        committed,
        _find_item(committed, item.item_id),
        requested_action=BlockedItemSettlementAction.DISCARD,
    )


def _backup_sentence(backup):
    return (
        " The tracks it replaced are kept in a backup listed in Settings > "
        "Diagnostics, where Restore puts them back in "
        f"{backup.receipt['origin']}."
    )


def _keep_imported_files(authority, journal, item, reference, manifest_hash):
    """Clear a download Beets finished filing, leaving the library as it is."""
    kept = _keep_backup_for_settings(
        authority, journal, item,
        "queue backup kept - the download was cleared after Beets filed it",
    )
    if type(kept) is BlockedItemSettlementResult:
        return kept
    backup_reference, backup = kept
    settled = _retire_cleared_download(
        authority, journal, item, reference, manifest_hash, backup_reference)
    if settled.status is not BlockedItemSettlementStatus.DISCARDED:
        return settled
    reason = "The download was cleared; what Beets filed stays in your library."
    if backup is not None:
        reason += _backup_sentence(backup)
    return BlockedItemSettlementResult(BlockedItemSettlementStatus.DISCARDED, reason)


def _set_partial_import_aside(authority, journal, item, reference, launched):
    """Clear a download Beets stopped part-way through filing.

    What Beets moved stays in the library, rows pointing at files still in
    staging go, the rest is kept as an ordinary kept staging group, and any
    backup is handed to Settings. Each step is proved before the next; one
    that can't be leaves the item blocked.
    """
    _state, manifest_hash, snapshot = launched
    owner = {"operation_id": journal.operation_id, "item_id": item.item_id}
    intent = snapshot["intent"]
    staged = sum(
        capture_file(record["path"], expected=tuple(record["identity"])) is not None
        for record in intent
    )
    blocked = _partial_import_reason(item, snapshot)
    for reference_ in _staging_references(item):
        if reference_.kind != _STAGING_RUN_KIND:
            return _blocked_settlement(blocked)
    _require_authority(authority)
    if not clear_stopped_import_folders(snapshot):
        return _blocked_settlement(
            blocked + " The folders it made in your library could not be "
            "checked, so it stays blocked."
        )
    if not forget_staged_beets_rows(record["path"] for record in intent):
        return _blocked_settlement(
            blocked + " The records beets kept for the unfiled tracks could "
            "not be removed, so it stays blocked."
        )
    _require_authority(authority)
    kept = _keep_backup_for_settings(
        authority, journal, item,
        "queue backup kept - the download was cleared after Beets stopped "
        "part-way",
    )
    if type(kept) is BlockedItemSettlementResult:
        return kept
    backup_reference, backup = kept
    set_aside = False
    for staging in _staging_references(item):
        _require_authority(authority)
        inspection = _staging_inspection(staging, owner)
        status = getattr(inspection, "status", None)
        if status is StagingReferenceStatus.ABSENT:
            continue
        run = staging_run_from_record(staging.data)
        current = capture_staging_run(run) if run is not None else None
        if status is not StagingReferenceStatus.MATCH or current is None:
            return _blocked_settlement(
                blocked + _check_staged([staging.data.get("path")]))
        if not current.files:
            continue
        if retain_staging_run(run, label="stopped-import", release=True) is None:
            return _blocked_settlement(
                blocked + " The files still in staging could not be set "
                "aside, so it stays blocked."
                + _check_staged([staging.data.get("path")])
            )
        set_aside = True
    _require_authority(authority)
    journal, item, staging_reason, _changed = _reconcile_item_staging(
        authority, journal, item, block_unsettled=False,
    )
    if staging_reason is not None:
        # Only an empty run folder can still be there.
        discarded = _discard_parked_item_staging(authority, journal, item)
        if discarded is None:
            return _blocked_settlement(blocked + _check_staged(
                entry.data.get("path") for entry in _staging_references(item)))
        journal, item = discarded
    settled = _retire_cleared_download(
        authority, journal, item, _single_reference(item, _MANAGED_CARRIER_KIND),
        manifest_hash, backup_reference,
    )
    if settled.status is not BlockedItemSettlementStatus.DISCARDED:
        return settled
    moved = len(intent) - staged
    reason = (
        f"The download was cleared. Beets had moved {plural(moved, 'track')} "
        f"into your library, and {'it stays' if moved == 1 else 'they stay'} "
        "there."
    )
    listed = (
        "listed in Settings > Diagnostics as “Files from a stopped download”, "
        "where you can remove"
    )
    if set_aside and staged:
        reason += (
            f" The {plural(staged, 'track')} it hadn't filed "
            f"{'was' if staged == 1 else 'were'} set aside and "
            f"{'is' if staged == 1 else 'are'} {listed} "
            f"{'it' if staged == 1 else 'them'}."
        )
    elif set_aside:
        reason += f" What it left in staging was set aside and is {listed} it."
    if backup is not None:
        reason += _backup_sentence(backup)
    return BlockedItemSettlementResult(BlockedItemSettlementStatus.DISCARDED, reason)


def settle_blocked_item(
    *,
    authority: RunLockLease,
    operation_id: str,
    item_id: str,
    action: BlockedItemSettlementAction,
) -> BlockedItemSettlementResult:
    """Retry or discard one blocked download. One that never reached Beets,
    or whose import moved nothing, is settled as if Beets never ran, and the
    tracks it moved aside go back first. One Beets finished can be kept or
    checked again, and one it stopped part-way through can only be cleared,
    keeping what it moved."""
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

        _require_authority(authority)
        launched = _launched_import(journal.operation_id, item)
        _require_authority(authority)
        if launched is not None and launched[0] == _IMPORT_PARTIAL:
            carrier = _single_reference(item, _MANAGED_CARRIER_KIND)
            if action is BlockedItemSettlementAction.DISCARD and carrier is not None:
                return _set_partial_import_aside(
                    authority, journal, item, carrier, launched)
            return _blocked_settlement(
                _partial_import_reason(item, launched[2]) + " It is not "
                "downloaded again."
            )
        managed_references = _references(item, _MANAGED_KINDS)
        if len(managed_references) > 1:
            return _blocked_settlement(
                "This blocked item is not an exact pre-launch Beets abort.")
        reference = managed_references[0] if managed_references else None
        owner = RecoveryOwner(journal.operation_id, item.item_id)
        # Nothing is discarded or put back until the app knows Beets left the
        # library alone, or finished with it.
        reservation = None
        if reference is not None and reference.kind == _MANAGED_RESERVATION_KIND:
            _require_authority(authority)
            reservation = inspect_managed_reservation(reference.data, owner)
            _require_authority(authority)
            if reservation.outcome is ManagedReservationInspectionOutcome.ACTIVITY:
                return _blocked_settlement(
                    "Beets may have started or changed the library, so this item remains blocked."
                )
            if reservation.outcome not in {
                ManagedReservationInspectionOutcome.ABSENT,
                ManagedReservationInspectionOutcome.ORIGIN,
            }:
                return _blocked_settlement(
                    "The app could not prove that Beets stayed pre-launch, so "
                    "this item remains blocked."
                )
        elif (
            reference is not None
            and reference.kind == _MANAGED_CARRIER_KIND
            and launched is None
        ):
            _require_authority(authority)
            inspection = inspect_managed_carrier(reference.data, owner)
            _require_authority(authority)
            if inspection.outcome is not ManagedCarrierInspectionOutcome.UNSEALED_ORIGIN:
                return _blocked_settlement(
                    "The app could not prove that Beets stayed pre-launch, so "
                    "this item remains blocked."
                )
        if launched is not None and launched[0] == _IMPORT_UNTOUCHED:
            # Beets adds an album's rows before it moves the files; rows for
            # files still in staging are dropped so nothing points at them.
            if not forget_staged_beets_rows(
                record["path"] for record in launched[2]["intent"]
            ):
                return _blocked_settlement(
                    "Beets recorded this album's tracks before it stopped, and "
                    "those records could not be removed, so this item remains "
                    "blocked."
                )
            _require_authority(authority)
        if (
            (launched is None or launched[0] != _IMPORT_FILED)
            and (reference is None or reference.kind != _MANAGED_SETTLEMENT_KIND)
        ):
            restored = _put_back_library_backup(authority, journal, item)
            if type(restored) is BlockedItemSettlementResult:
                return restored
            journal, item = restored
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
                    + _check_staged(
                        entry.data.get("path")
                        for entry in _staging_references(item))
                )
            journal, item = settled_staging

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

        carrier = None
        manifest_hash = None
        if reference.kind == _MANAGED_RESERVATION_KIND:
            if reservation.outcome is ManagedReservationInspectionOutcome.ABSENT:
                queue_state._commit_absent_managed_settlement(
                    journal,
                    item_id=item.item_id,
                    reservation=reference,
                    action=action.value,
                )
                _require_authority(authority)
                return _settled_result(action)
            carrier = reservation.carrier
            manifest_hash = reservation.manifest_hash
        elif reference.kind == _MANAGED_CARRIER_KIND:
            _require_authority(authority)
            inspection = inspect_managed_carrier(reference.data, owner)
            _require_authority(authority)
            state = (
                launched[0]
                if launched is not None
                and launched[1] == inspection.manifest_hash
                else None
            )
            if (
                inspection.outcome is ManagedCarrierInspectionOutcome.UNSEALED_ORIGIN
                or state == _IMPORT_UNTOUCHED
            ):
                # Beets either never started or stopped before it moved a
                # file, and any rows it added are gone: the library is as it
                # was.
                carrier = reference.data
                manifest_hash = inspection.manifest_hash
            elif state == _IMPORT_FILED and action is BlockedItemSettlementAction.DISCARD:
                return _keep_imported_files(
                    authority, journal, item, reference, inspection.manifest_hash
                )
            elif state == _IMPORT_FILED:
                return _check_import_again(authority, journal, item)
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


def _settled_download(journal, item, outcome, reason=""):
    album = item.planned.get("album")
    album = album if isinstance(album, dict) else {}
    album_id = album.get("id")
    return SettledDownload(
        operation_id=journal.operation_id,
        mode=journal.mode,
        item_id=item.item_id,
        album_id=None if album_id is None else str(album_id),
        label=str(album.get("title") or item.planned.get("label") or "download"),
        outcome=outcome,
        reason=reason,
    )


def _settle_choice(journal, item, tried):
    """The settlement a blocked download gets on its own, and its outcome.

    One Beets stopped part way through keeps what it moved. One Beets filed
    is checked again first and kept as it is if that fails, and one whose
    album changed after it was recorded as finished is kept at once.
    Anything else never reached the library, so its partial download goes:
    a terminal queue keeps the album to resume, and a web job's entry is
    dropped so the job can be queued again with a fresh plan.
    """
    launched = _launched_import(journal.operation_id, item)
    if launched is not None and launched[0] == _IMPORT_PARTIAL:
        return BlockedItemSettlementAction.DISCARD, "set-aside"
    if launched is not None and launched[0] == _IMPORT_FILED:
        if (
            BlockedItemSettlementAction.RETRY in tried
            or item.block_reason == "completed-item-proof-unavailable"
        ):
            return BlockedItemSettlementAction.DISCARD, "kept"
        return BlockedItemSettlementAction.RETRY, "checked"
    if journal.mode.startswith("web-job:"):
        return BlockedItemSettlementAction.DISCARD, "requeue"
    return BlockedItemSettlementAction.RETRY, "requeue"


def _settle_next_blocked(authority, journals, tried, outcomes):
    """Settle one blocked item not yet tried with the action it needs.

    Returns False once every blocked item has had each settlement it can
    get, so a refusal ends in attention instead of a loop.
    """
    for journal in journals:
        for item in sorted(journal.items, key=lambda value: value.item_id):
            if item.phase is not queue_state.QueuePhase.BLOCKED:
                continue
            key = (journal.operation_id, item.item_id)
            while True:
                _require_authority(authority)
                action, outcome = _settle_choice(journal, item, tried.get(key, ()))
                if action in tried.get(key, ()):
                    break
                tried[key] = (*tried.get(key, ()), action)
                settled = settle_blocked_item(
                    authority=authority,
                    operation_id=journal.operation_id,
                    item_id=item.item_id,
                    action=action,
                )
                if settled.status is not BlockedItemSettlementStatus.BLOCKED:
                    outcomes[key] = _settled_download(
                        journal, item, outcome,
                        "" if outcome == "requeue" else settled.reason)
                    return True
                log.warning(
                    "%s: %s: %s",
                    BLOCKED_DOWNLOAD_LOG_ENTRY,
                    item.planned.get("label") or item.item_id,
                    settled.reason,
                )
                loaded = queue_state.load_queue_journal(journal.operation_id)
                current = loaded.journal
                item = next(
                    (
                        value
                        for value in (current.items if current is not None else ())
                        if value.item_id == item.item_id
                    ),
                    None,
                )
                if item is None or item.phase is not queue_state.QueuePhase.BLOCKED:
                    return True
                journal = current
    return False


def _requeue_unstarted(authority, journals, outcomes):
    """Return downloads that never staged anything to their queue.

    A web job's saved entry is dropped so the job can be queued again, and
    a terminal one goes back to pending for the resume prompt.
    """
    changed = False
    classified = {
        (entry.operation_id, entry.item_id): entry.action
        for entry in _classify(journals)
    }
    for journal in journals:
        for item in journal.items:
            key = (journal.operation_id, item.item_id)
            action = classified.get(key)
            if journal.mode.startswith("web-job:"):
                if (
                    len(journal.items) != 1
                    or journal.retirements
                    or action not in {
                        StartupRecoveryAction.PENDING,
                        StartupRecoveryAction.RESUME_DOWNLOAD,
                    }
                ):
                    continue
                settled = discard_unstarted_item(
                    authority=authority,
                    operation_id=journal.operation_id,
                    item_id=item.item_id,
                )
                if settled.status is not BlockedItemSettlementStatus.DISCARDED:
                    continue
            elif action is StartupRecoveryAction.RESUME_DOWNLOAD:
                _require_authority(authority)
                queue_state.reset_unstarted_item_to_pending(journal, item.item_id)
                _require_authority(authority)
            else:
                continue
            outcomes[key] = _settled_download(journal, item, "requeue")
            changed = True
    return changed


def recover_startup_state(
    *,
    authority: RunLockLease,
    acknowledge_completion=None,
    settle: bool = False,
) -> StartupRecoveryResult:
    """Settle only exact restart evidence; never run downloads or imports.

    With ``settle``, a download a restart cut off is settled on its own as
    well: see ``_settle_choice``. Only a block nothing can be proved about
    still ends in attention.
    """
    tried = {}
    outcomes = {}
    result = _recover_startup_state(
        authority, acknowledge_completion, settle, tried, outcomes)
    if not outcomes:
        return result
    return replace(result, settled=tuple(outcomes.values()))


def _recover_startup_state(
    authority, acknowledge_completion, settle, tried, outcomes,
) -> StartupRecoveryResult:
    try:
        _require_authority(authority)
        relocation = reconcile_post_import_relocations(
            authority=authority,
            handoff_matches=queue_state.post_import_relocation_handoff_matches,
        )
        _require_authority(authority)
        if relocation.status is not RelocationRecoveryStatus.CLEAR:
            paths = "; ".join(os.fspath(path) for path in relocation.paths)
            log.error(
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
            blocked = [loaded for loaded in loads
                       if loaded.status is queue_state.QueueLoadStatus.BLOCKED]
            if blocked:
                log.error(
                    "%s: %s.",
                    UNREADABLE_QUEUE_LOG_ENTRY,
                    "; ".join(
                        f"{', '.join(map(os.fspath, loaded.paths)) or 'queue'}: "
                        f"{loaded.reason or 'reason not reported'}"
                        for loaded in blocked
                    ),
                )
                return StartupRecoveryResult(
                    StartupRecoveryStatus.ATTENTION_REQUIRED,
                    reason="queue-namespace-blocked",
                    paths=tuple(dict.fromkeys(
                        Path(path) for loaded in blocked for path in loaded.paths)),
                )
            journals = _sorted_journals(loads)
            _require_authority(authority)
            if _recover_active_library_backups(authority, journals):
                continue
            unclaimed = _unclaimed_staging_run_names(journals)
            if unclaimed:
                _require_authority(authority)
                if _retain_unclaimed_staging_runs(
                        authority, unclaimed, discard=settle):
                    continue
                staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
                left = [staging / name for name in unclaimed
                        if os.path.lexists(staging / name)]
                log.warning(
                    "%s: a download folder in staging no saved queue claims "
                    "could not be cleared: %s.%s",
                    BLOCKED_DOWNLOAD_LOG_ENTRY,
                    ", ".join(path.name for path in left),
                    _check_staged(left),
                )
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
                log.warning(
                    "%s: %s: %s",
                    BLOCKED_DOWNLOAD_LOG_ENTRY,
                    settlement[1].planned.get("label") or settlement[1].item_id,
                    settled.reason,
                )
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
                if settle and _settle_next_blocked(
                        authority, journals, tried, outcomes):
                    continue
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
                if _waits_for_web_app(journal, item, acknowledge_completion):
                    return StartupRecoveryResult(
                        StartupRecoveryStatus.ATTENTION_REQUIRED,
                        classified,
                        "web-completion-unacknowledged",
                    )
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
                    log.warning(
                        "%s: the record of a finished Beets import could not "
                        "be retired: %s",
                        BLOCKED_DOWNLOAD_LOG_ENTRY,
                        result.reason or result.outcome.value,
                    )
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
                key = (resolving[0].operation_id, resolving[1].item_id)
                tried[key] = (*tried.get(key, ()), BlockedItemSettlementAction.RETRY)
                loads = _load_namespace(authority)
                journals = _sorted_journals(loads)
                _recover_active_reservations(authority, journals)
                loads = _load_namespace(authority)
                journals = _sorted_journals(loads)
                if settle and _settle_next_blocked(
                        authority, journals, tried, outcomes):
                    continue
                return StartupRecoveryResult(
                    StartupRecoveryStatus.ATTENTION_REQUIRED,
                    _classify(journals),
                    "queue-item-blocked",
                )

            if _recover_active_reservations(authority, journals):
                loads = _load_namespace(authority)
                journals = _sorted_journals(loads)
                if settle and _settle_next_blocked(
                        authority, journals, tried, outcomes):
                    continue
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

            if settle and _requeue_unstarted(authority, journals, outcomes):
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
        sqlite3.Error,
        queue_state.QueueJournalError,
    ) as exc:
        log.error("%s: %s.", BLOCKED_DOWNLOAD_LOG_ENTRY, exc)
        return StartupRecoveryResult(
            StartupRecoveryStatus.ATTENTION_REQUIRED,
            reason="startup-recovery-unsettled",
        )
