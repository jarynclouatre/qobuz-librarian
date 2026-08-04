"""Exact settlement for owner-bound queue library backups."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qobuz_librarian.completion import RecoveryOwner
from qobuz_librarian.integrations.beets import retire_backup_beets_entries
from qobuz_librarian.library.backup import (
    capture_album_source_receipt,
    carry_backup_companions,
    dispose_backup,
    library_backup_disposal_record,
    library_backup_record_absent,
    load_library_backup_record,
    pin_unverified_upgrade_backup,
    reconcile_library_backup_disposal,
    warn_pin_failed,
)
from qobuz_librarian.library.catalog import folder_holds_all_tracks
from qobuz_librarian.queue import journal as queue_state

_BACKUP_INTENT = "library-backup-intent"
_BACKUP_CARRIER = "library-backup"
_BACKUP_SETTLEMENT = "library-backup-settlement"
_BACKUP_KINDS = frozenset({
    _BACKUP_INTENT,
    _BACKUP_CARRIER,
    _BACKUP_SETTLEMENT,
})


class LibraryBackupResolutionStatus(str, Enum):
    """Outcome of one non-downloading backup settlement step."""

    NONE = "none"
    READY = "ready"
    SETTLED = "settled"
    ATTENTION = "attention"


class LibraryBackupPersistenceError(OSError):
    """The exact backup transition could not be committed durably."""


@dataclass(frozen=True, slots=True)
class LibraryBackupResolution:
    journal: queue_state.QueueJournal
    status: LibraryBackupResolutionStatus
    reason: str | None = None


def _owner_record(owner: RecoveryOwner) -> dict[str, str]:
    if type(owner) is not RecoveryOwner:
        raise ValueError("an exact recovery owner is required")
    return {
        "operation_id": owner.operation_id,
        "item_id": owner.item_id,
    }


def _item(journal, item_id):
    return next(
        (item for item in journal.items if item.item_id == item_id),
        None,
    )


def _backup_references(item):
    if item is None:
        return ()
    return tuple(
        reference
        for reference in item.recovery_references
        if reference.kind in _BACKUP_KINDS
    )


def _attention(journal, reason):
    return LibraryBackupResolution(
        journal,
        LibraryBackupResolutionStatus.ATTENTION,
        reason,
    )


def _pin(backup, owner, reason, authority_check):
    if backup is None:
        return
    authority_check()
    pinned = pin_unverified_upgrade_backup(
        backup,
        reason,
        expected_owner=owner,
    )
    authority_check()
    if not pinned:
        warn_pin_failed(backup.path)


def _replacement_validator(kind, album):
    if kind == "upgrade":
        from qobuz_librarian.modes.process import _upgrade_trees_verified

        return _upgrade_trees_verified
    if kind == "gap-fill":
        tracks = (
            (album.get("tracks") or {}).get("items")
            if type(album) is dict
            else None
        )
        if not isinstance(tracks, list) or not tracks:
            return None
        return lambda replacement, _backup: folder_holds_all_tracks(
            replacement,
            tracks,
            destructive=True,
        )
    return None


def prepare_library_backup_settlement(
    journal,
    item_id,
    owner,
    album,
    replacement_path,
    *,
    authority_check,
):
    """Carry companions and persist exact disposal authority before deletion."""
    if not callable(authority_check):
        raise ValueError("authority_check must be callable")
    owner_data = _owner_record(owner)
    current = _item(journal, item_id)
    references = _backup_references(current)
    if not references:
        return LibraryBackupResolution(
            journal,
            LibraryBackupResolutionStatus.NONE,
        )
    if len(references) != 1:
        return _attention(journal, "library-backup-state-ambiguous")
    reference = references[0]
    if reference.kind == _BACKUP_SETTLEMENT:
        if reference.data.get("disposal") is not None:
            return LibraryBackupResolution(
                journal,
                LibraryBackupResolutionStatus.READY,
            )
        if reference.data.get("version") != 1:
            return _attention(
                journal, "library-backup-disposal-authority-unavailable")
        authority_check()
        legacy_backup = load_library_backup_record(
            reference.data["carrier"],
            expected_owner=owner_data,
        )
        authority_check()
        if legacy_backup is None:
            return _attention(
                journal, "library-backup-legacy-carrier-unavailable")
        disposal_record = library_backup_disposal_record(
            legacy_backup,
            expected_owner=owner_data,
        )
        authority_check()
        if disposal_record is None:
            _pin(
                legacy_backup,
                owner_data,
                "queue backup kept; legacy disposal state could not be sealed",
                authority_check,
            )
            return _attention(
                journal, "library-backup-disposal-unsettled")
        try:
            upgraded = queue_state.upgrade_library_backup_settlement(
                journal,
                item_id,
                reference,
                disposal_record=disposal_record,
            )
        except (OSError, queue_state.QueueJournalError) as exc:
            raise LibraryBackupPersistenceError(
                "legacy library backup disposal authority could not be persisted"
            ) from exc
        authority_check()
        return LibraryBackupResolution(
            upgraded,
            LibraryBackupResolutionStatus.READY,
        )
    if reference.kind != _BACKUP_CARRIER:
        return _attention(journal, "library-backup-intent-unsettled")
    if current.phase is not queue_state.QueuePhase.RESOLVING:
        return _attention(journal, "library-backup-carrier-unsettled")

    authority_check()
    backup = load_library_backup_record(
        reference.data,
        expected_owner=owner_data,
    )
    authority_check()
    if backup is None:
        return _attention(journal, "library-backup-carrier-unavailable")
    if backup.complete is not True:
        _pin(
            backup,
            owner_data,
            "queue backup kept; source retirement did not finish exactly",
            authority_check,
        )
        return _attention(journal, "library-backup-incomplete")

    try:
        replacement = Path(os.path.abspath(os.fspath(replacement_path)))
    except (OSError, TypeError, ValueError):
        _pin(
            backup,
            owner_data,
            "queue backup kept; replacement path was unavailable",
            authority_check,
        )
        return _attention(journal, "library-backup-replacement-unavailable")
    validator = _replacement_validator(reference.data["kind"], album)
    if validator is None:
        _pin(
            backup,
            owner_data,
            "queue backup kept; replacement scope could not be verified",
            authority_check,
        )
        return _attention(journal, "library-backup-replacement-unverified")

    authority_check()
    if not validator(replacement, backup.path):
        authority_check()
        _pin(
            backup,
            owner_data,
            "queue backup kept; replacement did not prove the original safe",
            authority_check,
        )
        return _attention(journal, "library-backup-replacement-unverified")
    authority_check()
    replacement_receipt = capture_album_source_receipt(replacement)
    authority_check()
    if replacement_receipt is None:
        _pin(
            backup,
            owner_data,
            "queue backup kept; replacement could not be held exactly",
            authority_check,
        )
        return _attention(journal, "library-backup-replacement-unavailable")

    if reference.data["kind"] == "upgrade":
        authority_check()
        replacement_receipt = carry_backup_companions(
            backup,
            replacement,
            expected_replacement_receipt=replacement_receipt,
            expected_owner=owner_data,
        )
        authority_check()
        if replacement_receipt is None:
            _pin(
                backup,
                owner_data,
                "queue backup kept; companion files could not be carried exactly",
                authority_check,
            )
            return _attention(journal, "library-backup-companions-unsettled")
        if not validator(replacement, backup.path):
            authority_check()
            _pin(
                backup,
                owner_data,
                "queue backup kept; replacement changed during companion carry",
                authority_check,
            )
            return _attention(journal, "library-backup-replacement-unverified")
        authority_check()

    authority_check()
    disposal_record = library_backup_disposal_record(
        backup,
        expected_owner=owner_data,
    )
    authority_check()
    if disposal_record is None:
        _pin(
            backup,
            owner_data,
            "queue backup kept; exact disposal state could not be sealed",
            authority_check,
        )
        return _attention(journal, "library-backup-disposal-unsettled")

    try:
        saved = queue_state.begin_library_backup_settlement(
            journal,
            item_id,
            reference,
            replacement_path=os.fspath(replacement),
            replacement_receipt=replacement_receipt,
            disposal_record=disposal_record,
        )
    except (OSError, queue_state.QueueJournalError) as exc:
        raise LibraryBackupPersistenceError(
            "library backup disposal authority could not be persisted"
        ) from exc
    authority_check()
    return LibraryBackupResolution(
        saved,
        LibraryBackupResolutionStatus.READY,
    )


def finish_library_backup_settlement(
    journal,
    item_id,
    owner,
    album,
    *,
    authority_check,
):
    """Dispose one exact backup, or keep its settlement durably blocked."""
    if not callable(authority_check):
        raise ValueError("authority_check must be callable")
    owner_data = _owner_record(owner)
    current = _item(journal, item_id)
    references = _backup_references(current)
    if not references:
        return LibraryBackupResolution(
            journal,
            LibraryBackupResolutionStatus.SETTLED,
        )
    if len(references) != 1 or references[0].kind != _BACKUP_SETTLEMENT:
        return _attention(journal, "library-backup-settlement-unavailable")
    settlement = references[0]
    data = settlement.data
    if data.get("disposal") is None:
        return _attention(
            journal, "library-backup-disposal-authority-unavailable")
    replacement = Path(data["replacement_path"])
    validator = _replacement_validator(data["carrier"]["kind"], album)
    if validator is None:
        return _attention(journal, "library-backup-replacement-unverified")

    authority_check()
    current_receipt = capture_album_source_receipt(replacement)
    authority_check()
    if current_receipt != data["replacement_receipt"]:
        backup = load_library_backup_record(
            data["carrier"],
            expected_owner=owner_data,
        )
        _pin(
            backup,
            owner_data,
            "queue backup kept; replacement changed before final disposal",
            authority_check,
        )
        return _attention(journal, "library-backup-replacement-changed")

    backup = load_library_backup_record(
        data["carrier"],
        expected_owner=owner_data,
    )
    authority_check()
    if not retire_backup_beets_entries(
        data["carrier"],
        replacement,
        data["replacement_receipt"],
    ):
        authority_check()
        _pin(
            backup,
            owner_data,
            "queue backup kept; the replaced Beets entries could not be "
            "retired safely",
            authority_check,
        )
        return _attention(journal, "library-backup-catalogue-unsettled")
    authority_check()

    disposal_reconciliation = reconcile_library_backup_disposal(
        data["carrier"],
        data["disposal"],
        replacement_path=replacement,
        expected_replacement_receipt=data["replacement_receipt"],
        expected_owner=owner_data,
    )
    authority_check()
    if disposal_reconciliation.state == "attention":
        location = disposal_reconciliation.quarantine_path
        reason = "library-backup-disposal-quarantine"
        if location is not None:
            reason = f"{reason}:{location}"
        return _attention(journal, reason)

    if backup is None:
        if not library_backup_record_absent(
            data["carrier"],
            expected_owner=owner_data,
        ):
            return _attention(journal, "library-backup-carrier-unavailable")
        authority_check()
        if data["carrier"]["kind"] == "upgrade":
            if disposal_reconciliation.state not in {"disposed", "none"}:
                # The exact settlement and replacement remain actionable
                # evidence, but the old-vs-new semantic comparator cannot be
                # rerun once the authorised backup deletion has crossed its
                # final crash window.
                return _attention(
                    journal,
                    "upgrade-backup-disposal-needs-confirmation",
                )
        elif not validator(replacement, None):
            authority_check()
            return _attention(journal, "library-backup-replacement-unverified")
        authority_check()
    else:
        if backup.complete is not True:
            _pin(
                backup,
                owner_data,
                "queue backup kept; source retirement did not finish exactly",
                authority_check,
            )
            return _attention(journal, "library-backup-incomplete")
        authority_check()
        disposed = dispose_backup(
            backup,
            replacement_path=replacement,
            expected_replacement_receipt=data["replacement_receipt"],
            replacement_validator=validator,
            expected_owner=owner_data,
            expected_disposal_record=data.get("disposal"),
        )
        authority_check()
        if not disposed:
            _pin(
                backup,
                owner_data,
                "queue backup kept; final exact replacement proof did not hold",
                authority_check,
            )
            return _attention(journal, "library-backup-disposal-unsettled")

    try:
        saved = queue_state.finish_library_backup_settlement(
            journal,
            item_id,
            settlement,
        )
    except (OSError, queue_state.QueueJournalError) as exc:
        raise LibraryBackupPersistenceError(
            "library backup settlement could not be persisted"
        ) from exc
    authority_check()
    return LibraryBackupResolution(
        saved,
        LibraryBackupResolutionStatus.SETTLED,
    )
