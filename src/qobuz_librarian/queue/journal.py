"""Durable state for queue operations that can change the music library."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    CompletionEvidence,
    CompletionInput,
    RecoveryOwner,
    completion_input_extends,
    completion_input_ready,
    normalise_album_id,
    parse_completion_input_record,
    parse_completion_record,
)
from qobuz_librarian.integrations.staging import (
    _staging_group_reference_parameters,
    staging_run_from_record,
)
from qobuz_librarian.library import candidate_premise
from qobuz_librarian.library.backup import (
    canonical_album_source_receipt,
    canonical_library_backup_disposal_record,
    canonical_library_backup_intent,
    canonical_library_backup_record,
    library_backup_matches_intent,
)
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.recovery import normalise_recovery_owner

_HEX_ID = re.compile(r"[0-9a-f]{64}\Z")
_PLANNED_KEYS = frozenset({
    "album",
    "album_dir",
    "label",
    "missing",
    "present",
    "upgrade_only",
    "auto_upgrade",
    "siblings_to_delete",
    "quality",
    "force_track_by_track",
    "source_premise",
})
_JOURNAL_KEYS = frozenset({
    "version",
    "operation_id",
    "generation",
    "saved_at",
    "mode",
    "count",
    "items",
    "retirement_count",
    "retirements",
    "legacy_source_sha256",
})
_LEGACY_ITEM_KEYS = frozenset({
    "item_id",
    "phase",
    "planned",
    "recovery_references",
    "block_reason",
    "completion_input",
    "completion_evidence",
})
_ITEM_KEYS = _LEGACY_ITEM_KEYS | {"multi_artist_filing"}
_REFERENCE_KEYS = frozenset({"name", "kind", "data"})
_POST_IMPORT_ACTION_KEYS = frozenset({
    "action_id",
    "kind",
    "source",
    "destination",
    "expectation",
    "phase",
    "relocation_operation_id",
    "handoff_hash",
})
_POST_IMPORT_ACTION_KINDS = frozenset({"split-gap-fill", "whole-album"})
_LEGACY_RETIREMENT_KEYS = frozenset({
    "item_id",
    "reference",
    "manifest_hash",
    "quarantine_name",
    "state",
})
_RETIREMENT_KEYS = _LEGACY_RETIREMENT_KEYS | {
    "planned",
    "completion_input",
    "completion_evidence",
    "final_path",
    "action",
    "completion_acknowledged",
}
_LEGACY_KEYS = frozenset({"version", "saved_at", "mode", "count", "items"})
_JOURNAL_LOCK_NAME = ".queue-journal.lock"
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_MANAGED_COMPLETION_REFERENCE_KIND = "managed-beets"
_MANAGED_RESERVATION_REFERENCE_KIND = "managed-beets-reservation"
_MANAGED_PRELAUNCH_SETTLEMENT_REFERENCE_KIND = (
    "managed-beets-prelaunch-settlement"
)
_MANAGED_PRELAUNCH_SETTLEMENT_REFERENCE_NAME = (
    "managed-prelaunch-settlement"
)
_MANAGED_PRELAUNCH_SETTLEMENT_ACTIONS = frozenset({"retry", "discard"})
_DOWNLOAD_STAGING_REFERENCE_KIND = "download-staging-run"
_STAGING_GROUP_REFERENCE_KIND = "staging-group"
_STAGING_REFERENCE_KINDS = frozenset({
    _DOWNLOAD_STAGING_REFERENCE_KIND,
    _STAGING_GROUP_REFERENCE_KIND,
})
_LIBRARY_BACKUP_INTENT_REFERENCE_KIND = "library-backup-intent"
_LIBRARY_BACKUP_CARRIER_REFERENCE_KIND = "library-backup"
_LIBRARY_BACKUP_SETTLEMENT_REFERENCE_KIND = "library-backup-settlement"
_LIBRARY_BACKUP_REFERENCE_NAME = "library-backup"
_LIBRARY_BACKUP_REFERENCE_KINDS = frozenset({
    _LIBRARY_BACKUP_INTENT_REFERENCE_KIND,
    _LIBRARY_BACKUP_CARRIER_REFERENCE_KIND,
    _LIBRARY_BACKUP_SETTLEMENT_REFERENCE_KIND,
})
_REFERENCE_KIND_ORDER = {
    _LIBRARY_BACKUP_INTENT_REFERENCE_KIND: 0,
    _LIBRARY_BACKUP_CARRIER_REFERENCE_KIND: 1,
    _LIBRARY_BACKUP_SETTLEMENT_REFERENCE_KIND: 2,
    _DOWNLOAD_STAGING_REFERENCE_KIND: 3,
    _STAGING_GROUP_REFERENCE_KIND: 4,
    _MANAGED_RESERVATION_REFERENCE_KIND: 5,
    _MANAGED_COMPLETION_REFERENCE_KIND: 6,
    _MANAGED_PRELAUNCH_SETTLEMENT_REFERENCE_KIND: 7,
}
_MANAGED_CARRIER_PREFIX = ".qobuz-managed-beets-"
_MANAGED_CARRIER_SUFFIX = ".jsonl"
_UNSET = object()
_NAMESPACE_THREAD_LOCK = threading.RLock()
_namespace_lock_depth = 0
_namespace_lock_descriptor: int | None = None
_namespace_lock_epoch = 0


def _reset_namespace_lock_after_fork() -> None:
    global _NAMESPACE_THREAD_LOCK, _namespace_lock_depth
    global _namespace_lock_descriptor, _namespace_lock_epoch

    if _namespace_lock_descriptor is not None:
        try:
            os.close(_namespace_lock_descriptor)
        except OSError:
            pass
    _NAMESPACE_THREAD_LOCK = threading.RLock()
    _namespace_lock_depth = 0
    _namespace_lock_descriptor = None
    _namespace_lock_epoch += 1


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_namespace_lock_after_fork)


class QueuePhase(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    RESOLVING = "resolving"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class CarrierRetirementState(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"


class PostImportActionPhase(str, Enum):
    PLANNED = "planned"
    HANDOFF = "handoff"
    COMMITTED = "committed"


class QueueLoadStatus(str, Enum):
    ABSENT = "absent"
    READY = "ready"
    BLOCKED = "blocked"


class QueueJournalError(RuntimeError):
    """Base error for journal validation or durable publication."""


class QueueJournalBlocked(QueueJournalError):
    """The saved operation cannot be changed without an explicit decision."""


class _QueueNamespaceBlocked(QueueJournalBlocked):
    """The dedicated journal directory contains ambiguous persisted state."""

    def __init__(self, message: str, paths: tuple[Path, ...]):
        super().__init__(message)
        self.paths = paths


@dataclass(frozen=True, slots=True)
class RecoveryReference:
    """Serialized recovery evidence; reopening it requires an executor check."""

    name: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JournalItem:
    """One persisted item; completion evidence is data, never a trusted verdict.

    Executor integration must validate the final evidence schema and recompute
    live completion before destructive continuation or journal removal.
    """

    item_id: str
    phase: QueuePhase
    planned: dict[str, Any]
    recovery_references: tuple[RecoveryReference, ...] = ()
    block_reason: str | None = None
    completion_input: dict[str, Any] | None = None
    completion_evidence: dict[str, Any] | None = None
    multi_artist_filing: bool = False


@dataclass(frozen=True, slots=True)
class PostImportAction:
    """One exact folder-layout action owned by a carrier retirement."""

    action_id: str
    kind: str
    source: str
    destination: str
    expectation: dict[str, Any]
    phase: PostImportActionPhase = PostImportActionPhase.PLANNED
    relocation_operation_id: str | None = None
    handoff_hash: str | None = None


@dataclass(frozen=True, slots=True)
class CarrierRetirement:
    """Durable cleanup authority retained after its queue item is removed."""

    item_id: str
    reference: RecoveryReference
    manifest_hash: str
    quarantine_name: str
    state: CarrierRetirementState = CarrierRetirementState.PUBLIC
    planned: dict[str, Any] | None = None
    completion_input: dict[str, Any] | None = None
    completion_evidence: dict[str, Any] | None = None
    final_path: str | None = None
    action: PostImportAction | None = None
    completion_acknowledged: bool = True


@dataclass(frozen=True, slots=True)
class QueueJournal:
    operation_id: str
    mode: str
    saved_at: str
    items: tuple[JournalItem, ...]
    generation: int = 0
    legacy_source_sha256: str | None = None
    retirements: tuple[CarrierRetirement, ...] = ()


@dataclass(frozen=True, slots=True, eq=False)
class QueueLoad:
    status: QueueLoadStatus
    journal: QueueJournal | None = None
    reason: str | None = None
    paths: tuple[Path, ...] = ()

    def as_legacy_tuple(self):
        """Keep tuple-unpacking callers safe until executor integration lands."""
        if (
            self.status is not QueueLoadStatus.READY
            or self.journal is None
            or self.journal.retirements
            or any(item.phase is not QueuePhase.PENDING for item in self.journal.items)
        ):
            return None, None, None
        items = [_deserialize_queue_item(item.planned) for item in self.journal.items]
        return items, self.journal.mode, self.journal.saved_at

    def __iter__(self):
        return iter(self.as_legacy_tuple())

    def __getitem__(self, index):
        return self.as_legacy_tuple()[index]

    def __eq__(self, other):
        if isinstance(other, tuple):
            return self.as_legacy_tuple() == other
        if not isinstance(other, QueueLoad):
            return NotImplemented
        return (
            self.status,
            self.journal,
            self.reason,
            self.paths,
        ) == (
            other.status,
            other.journal,
            other.reason,
            other.paths,
        )


def _new_id() -> str:
    return secrets.token_hex(32)


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _HEX_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")
    return value


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("saved_at must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("saved_at is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("saved_at must include a timezone")
    return value


def _validate_json_value(value: Any, field_name: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{field_name}[{index}]")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} contains a non-string object key")
            _validate_json_value(child, f"{field_name}.{key}")
        return
    raise ValueError(f"{field_name} contains unsupported data")


def _json_copy(value: Any) -> Any:
    _validate_json_value(value, "value")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _reject_duplicate_object_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"JSON object contains duplicate key {key!r}")
        value[key] = child
    return value


def _serialize_queue_item(item):
    """Pull the planned, JSON-safe inputs out of an in-memory queue item."""
    return _validate_planned({
        "album": item["album"],
        "album_dir": str(item["album_dir"]) if item["album_dir"] else None,
        "label": item["label"],
        "missing": item["missing"],
        "present": item["present"],
        "upgrade_only": item["upgrade_only"],
        "auto_upgrade": item["auto_upgrade"],
        "siblings_to_delete": [str(p) for p in item.get("siblings_to_delete") or []],
        "quality": item.get("quality"),
        "force_track_by_track": item.get("force_track_by_track", False),
        "source_premise": item.get("_source_premise"),
    })


def _deserialize_queue_item(data):
    """Rebuild only the planned queue inputs, with fresh runtime fields."""
    planned = _validate_planned(data)
    return _build_queue_item(
        album=planned["album"],
        album_dir=Path(planned["album_dir"]) if planned["album_dir"] else None,
        label=planned["label"],
        missing=planned["missing"],
        present=planned["present"],
        upgrade_only=planned["upgrade_only"],
        auto_upgrade=planned["auto_upgrade"],
        siblings_to_delete=[Path(path) for path in planned["siblings_to_delete"]],
        quality=planned["quality"],
        force_track_by_track=planned["force_track_by_track"],
        source_premise=planned["source_premise"],
    )


def _validate_planned(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PLANNED_KEYS:
        raise ValueError("planned queue inputs have an invalid schema")
    if not isinstance(value["album"], dict):
        raise ValueError("planned album must be an object")
    if value["album_dir"] is not None and not isinstance(value["album_dir"], str):
        raise ValueError("planned album_dir must be a string or null")
    if not isinstance(value["label"], str):
        raise ValueError("planned label must be a string")
    for name in ("missing", "present", "siblings_to_delete"):
        if not isinstance(value[name], list):
            raise ValueError(f"planned {name} must be a list")
    if any(not isinstance(path, str) for path in value["siblings_to_delete"]):
        raise ValueError("planned sibling paths must be strings")
    for name in ("upgrade_only", "auto_upgrade", "force_track_by_track"):
        if type(value[name]) is not bool:
            raise ValueError(f"planned {name} must be a boolean")
    if value["quality"] is not None and type(value["quality"]) is not int:
        raise ValueError("planned quality must be an integer or null")
    if value["source_premise"] is not None:
        premise = candidate_premise.canonical_premise(value["source_premise"])
        if premise is None or premise != value["source_premise"]:
            raise ValueError("planned source premise is malformed")
        if value["album_dir"] is None or premise["path"] != os.path.abspath(
            value["album_dir"]
        ):
            raise ValueError("planned source premise has the wrong path")
    _validate_json_value(value, "planned")
    return _json_copy(value)


def _reference_payload(reference: RecoveryReference) -> dict[str, Any]:
    return {
        "name": reference.name,
        "kind": reference.kind,
        "data": _json_copy(reference.data),
    }


def _parse_reference(value: Any) -> RecoveryReference:
    if not isinstance(value, dict) or set(value) != _REFERENCE_KEYS:
        raise ValueError("recovery reference has an invalid schema")
    name = value["name"]
    kind = value["kind"]
    data = value["data"]
    if not isinstance(name, str) or not name:
        raise ValueError("recovery reference name must be a non-empty string")
    if not isinstance(kind, str) or not kind:
        raise ValueError("recovery reference kind must be a non-empty string")
    if not isinstance(data, dict):
        raise ValueError("recovery reference data must be an object")
    return RecoveryReference(name=name, kind=kind, data=_json_copy(data))


def _staging_reference_name(kind: str, path: str) -> str:
    identity = hashlib.sha256(
        f"{kind}\0{path}".encode("utf-8", errors="strict")
    ).hexdigest()
    return f"{kind}:{identity}"


def _strict_staging_reference(
    reference: RecoveryReference,
    *,
    expected_operation_id: str,
    expected_item_id: str,
) -> bool:
    if (
        type(reference) is not RecoveryReference
        or reference.kind not in _STAGING_REFERENCE_KINDS
        or type(reference.data) is not dict
    ):
        return False
    data = reference.data
    try:
        owner = normalise_recovery_owner(data.get("owner"))
    except (AttributeError, ValueError):
        return False
    path = data.get("path")
    common = (
        type(data.get("version")) is int
        and isinstance(path, str)
        and os.path.isabs(path)
        and os.path.abspath(path) == path
        and owner == {
            "operation_id": expected_operation_id,
            "item_id": expected_item_id,
        }
        and reference.name == _staging_reference_name(reference.kind, path)
    )
    if not common:
        return False
    if reference.kind == _DOWNLOAD_STAGING_REFERENCE_KIND:
        run = staging_run_from_record(data)
        return (
            run is not None
            and str(run.path) == path
            and run.owner == owner
        )
    return _staging_group_reference_parameters(data, owner) is not None


def _staging_reference_identity(reference: RecoveryReference):
    owner = reference.data["owner"]
    return (
        reference.kind,
        reference.data["path"],
        owner["operation_id"],
        owner["item_id"],
    )


def _reference_sort_key(reference: RecoveryReference):
    path = reference.data.get("path")
    return (
        _REFERENCE_KIND_ORDER.get(reference.kind, len(_REFERENCE_KIND_ORDER)),
        reference.kind,
        path if isinstance(path, str) else "",
        reference.name,
    )


def _canonical_recovery_references(references):
    return tuple(sorted(references, key=_reference_sort_key))


def _canonical_library_album_path(value, field_name):
    if type(value) is not str or not os.path.isabs(value) or "\x00" in value:
        raise ValueError(f"{field_name} must be an absolute album path")
    root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    path = Path(os.path.abspath(value))
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError(f"{field_name} is outside the music library") from None
    if len(relative.parts) != 2 or os.fspath(path) != value:
        raise ValueError(f"{field_name} is not an exact album path")
    return value


def _post_import_action_payload(action: PostImportAction) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "kind": action.kind,
        "source": action.source,
        "destination": action.destination,
        "expectation": _json_copy(action.expectation),
        "phase": action.phase.value,
        "relocation_operation_id": action.relocation_operation_id,
        "handoff_hash": action.handoff_hash,
    }


def _parse_post_import_action(value: Any) -> PostImportAction:
    if type(value) is not dict or set(value) != _POST_IMPORT_ACTION_KEYS:
        raise ValueError("post-import action has an invalid schema")
    action_id = _validate_id(value["action_id"], "post-import action_id")
    kind = value["kind"]
    if kind not in _POST_IMPORT_ACTION_KINDS:
        raise ValueError("post-import action kind is invalid")
    source = _canonical_library_album_path(
        value["source"], "post-import source"
    )
    destination = _canonical_library_album_path(
        value["destination"], "post-import destination"
    )
    if source == destination:
        raise ValueError("post-import action paths are identical")
    # Late: cli.py loads this module on every command, and this one reaches the
    # catalogue and the Qobuz client. Only a post-import action needs it.
    from qobuz_librarian.library import post_import_relocation

    expectation = post_import_relocation.canonical_post_import_relocation_expectation(
        value["expectation"],
        source=source,
        destination=destination,
    )
    if expectation is None:
        raise ValueError("post-import action expectation is invalid")
    try:
        phase = PostImportActionPhase(value["phase"])
    except (TypeError, ValueError) as exc:
        raise ValueError("post-import action phase is invalid") from exc
    operation_id = value["relocation_operation_id"]
    handoff_hash = value["handoff_hash"]
    if phase is PostImportActionPhase.PLANNED:
        if operation_id is not None or handoff_hash is not None:
            raise ValueError("planned post-import action has a handoff")
    else:
        operation_id = _validate_id(
            operation_id, "post-import relocation operation_id"
        )
        handoff_hash = _validate_id(
            handoff_hash, "post-import handoff_hash"
        )
    return PostImportAction(
        action_id=action_id,
        kind=kind,
        source=source,
        destination=destination,
        expectation=expectation,
        phase=phase,
        relocation_operation_id=operation_id,
        handoff_hash=handoff_hash,
    )


def _retirement_payload(retirement: CarrierRetirement) -> dict[str, Any]:
    payload = {
        "item_id": retirement.item_id,
        "reference": _reference_payload(retirement.reference),
        "manifest_hash": retirement.manifest_hash,
        "quarantine_name": retirement.quarantine_name,
        "state": retirement.state.value,
    }
    if retirement.planned is None:
        if (
            retirement.completion_input is not None
            or retirement.completion_evidence is not None
            or retirement.final_path is not None
            or retirement.action is not None
            or retirement.completion_acknowledged is not True
        ):
            raise ValueError("legacy carrier retirement has new-format state")
        return payload
    payload.update({
        "planned": _validate_planned(retirement.planned),
        "completion_input": _json_copy(retirement.completion_input),
        "completion_evidence": _json_copy(retirement.completion_evidence),
        "final_path": retirement.final_path,
        "action": (
            None
            if retirement.action is None
            else _post_import_action_payload(retirement.action)
        ),
        "completion_acknowledged": retirement.completion_acknowledged,
    })
    return payload


def _strict_managed_reference(
    reference: RecoveryReference,
    *,
    expected_operation_id: str,
    expected_item_id: str,
) -> bool:
    if (
        type(reference) is not RecoveryReference
        or reference.kind != _MANAGED_COMPLETION_REFERENCE_KIND
    ):
        return False
    data = reference.data
    fields = {
        "version",
        "path",
        "device",
        "inode",
        "parent_device",
        "parent_inode",
        "nonce",
        "owner",
    }
    owner = None
    try:
        owner = normalise_recovery_owner(data.get("owner"))
    except (AttributeError, ValueError):
        pass
    common = (
        type(data) is dict
        and set(data) == fields
        and type(data.get("version")) is int
        and data["version"] in {1, 2}
        and isinstance(data.get("path"), str)
        and os.path.isabs(data["path"])
        and os.path.abspath(data["path"]) == data["path"]
        and all(
            type(data.get(field)) is int and data[field] >= 0
            for field in ("device", "inode", "parent_device", "parent_inode")
        )
        and isinstance(data.get("nonce"), str)
        and _HEX_ID.fullmatch(data["nonce"]) is not None
        and owner == {
            "operation_id": expected_operation_id,
            "item_id": expected_item_id,
        }
    )
    if not common:
        return False
    if data["version"] == 1:
        return True
    return data["path"] == _reserved_managed_carrier_path(data["nonce"])


def _reserved_managed_carrier_path(nonce: str) -> str | None:
    try:
        parent = Path(os.path.abspath(os.fspath(cfg.BEETS_DB_PATH.parent)))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return str(parent / f"{_MANAGED_CARRIER_PREFIX}{nonce}{_MANAGED_CARRIER_SUFFIX}")


def _strict_managed_reservation(
    reference: RecoveryReference,
    *,
    expected_operation_id: str,
    expected_item_id: str,
) -> bool:
    if (
        type(reference) is not RecoveryReference
        or reference.kind != _MANAGED_RESERVATION_REFERENCE_KIND
    ):
        return False
    data = reference.data
    fields = {
        "version",
        "path",
        "parent_device",
        "parent_inode",
        "nonce",
        "owner",
    }
    try:
        owner = normalise_recovery_owner(data.get("owner"))
    except (AttributeError, ValueError):
        owner = None
    return (
        type(data) is dict
        and set(data) == fields
        and type(data.get("version")) is int
        and data["version"] == 1
        and isinstance(data.get("nonce"), str)
        and _HEX_ID.fullmatch(data["nonce"]) is not None
        and isinstance(data.get("path"), str)
        and data["path"] == _reserved_managed_carrier_path(data["nonce"])
        and all(
            type(data.get(field)) is int and data[field] >= 0
            for field in ("parent_device", "parent_inode")
        )
        and owner == {
            "operation_id": expected_operation_id,
            "item_id": expected_item_id,
        }
    )


def _strict_managed_prelaunch_settlement(
    reference: RecoveryReference,
    *,
    expected_operation_id: str,
    expected_item_id: str,
) -> bool:
    """Validate durable authority to retire one exact pre-launch carrier."""
    if (
        type(reference) is not RecoveryReference
        or reference.name != _MANAGED_PRELAUNCH_SETTLEMENT_REFERENCE_NAME
        or reference.kind != _MANAGED_PRELAUNCH_SETTLEMENT_REFERENCE_KIND
    ):
        return False
    data = reference.data
    fields = {"version", "action", "carrier", "manifest_hash", "owner"}
    try:
        owner = normalise_recovery_owner(data.get("owner"))
    except (AttributeError, ValueError):
        owner = None
    carrier = RecoveryReference(
        name="managed-import",
        kind=_MANAGED_COMPLETION_REFERENCE_KIND,
        data=data.get("carrier") if type(data) is dict else {},
    )
    return (
        type(data) is dict
        and set(data) == fields
        and type(data.get("version")) is int
        and data["version"] == 1
        and type(data.get("action")) is str
        and data["action"] in _MANAGED_PRELAUNCH_SETTLEMENT_ACTIONS
        and isinstance(data.get("manifest_hash"), str)
        and _HEX_ID.fullmatch(data["manifest_hash"]) is not None
        and owner == {
            "operation_id": expected_operation_id,
            "item_id": expected_item_id,
        }
        and _strict_managed_reference(
            carrier,
            expected_operation_id=expected_operation_id,
            expected_item_id=expected_item_id,
        )
        and carrier.data["version"] == 2
    )


def _strict_library_backup_intent(
    reference: RecoveryReference,
    *,
    expected_operation_id: str,
    expected_item_id: str,
) -> bool:
    return (
        type(reference) is RecoveryReference
        and reference.name == _LIBRARY_BACKUP_REFERENCE_NAME
        and reference.kind == _LIBRARY_BACKUP_INTENT_REFERENCE_KIND
        and canonical_library_backup_intent(
            reference.data,
            expected_owner={
                "operation_id": expected_operation_id,
                "item_id": expected_item_id,
            },
        ) == reference.data
    )


def _strict_library_backup_carrier(
    reference: RecoveryReference,
    *,
    expected_operation_id: str,
    expected_item_id: str,
) -> bool:
    return (
        type(reference) is RecoveryReference
        and reference.name == _LIBRARY_BACKUP_REFERENCE_NAME
        and reference.kind == _LIBRARY_BACKUP_CARRIER_REFERENCE_KIND
        and canonical_library_backup_record(
            reference.data,
            expected_owner={
                "operation_id": expected_operation_id,
                "item_id": expected_item_id,
            },
        ) == reference.data
    )


def _strict_library_backup_settlement(
    reference: RecoveryReference,
    *,
    expected_operation_id: str,
    expected_item_id: str,
) -> bool:
    if (
        type(reference) is not RecoveryReference
        or reference.name != _LIBRARY_BACKUP_REFERENCE_NAME
        or reference.kind != _LIBRARY_BACKUP_SETTLEMENT_REFERENCE_KIND
    ):
        return False
    data = reference.data
    owner = {
        "operation_id": expected_operation_id,
        "item_id": expected_item_id,
    }
    common_fields = {
        "version", "owner", "carrier", "replacement_path",
        "replacement_receipt",
    }
    if (
        type(data) is not dict
        or type(data.get("version")) is not int
        or data["version"] not in {1, 2}
        or set(data) != (
            common_fields if data["version"] == 1
            else common_fields | {"disposal"}
        )
        or canonical_library_backup_record(
            data.get("carrier"),
            expected_owner=owner,
        ) != data.get("carrier")
        or type(data.get("replacement_path")) is not str
        or not os.path.isabs(data["replacement_path"])
        or os.path.abspath(data["replacement_path"])
            != data["replacement_path"]
        or canonical_album_source_receipt(
            data.get("replacement_receipt"),
            expected_origin=data["replacement_path"],
        ) != data.get("replacement_receipt")
        or data["version"] == 2
        and canonical_library_backup_disposal_record(
            data.get("disposal"),
            data.get("carrier"),
            expected_owner=owner,
        ) != data.get("disposal")
    ):
        return False
    try:
        supplied_owner = normalise_recovery_owner(data.get("owner"))
    except ValueError:
        return False
    return supplied_owner == owner


def _recovery_reference_inventory(
    references: tuple[RecoveryReference, ...],
    *,
    expected_operation_id: str,
    expected_item_id: str,
):
    if references != _canonical_recovery_references(references):
        raise ValueError("recovery references must use canonical ordering")
    names = [reference.name for reference in references]
    if len(names) != len(set(names)):
        raise ValueError("recovery reference names must be unique")

    staging = []
    reservations = []
    carriers = []
    settlements = []
    backup_intents = []
    backup_carriers = []
    backup_settlements = []
    staging_identities = set()
    for reference in references:
        if _strict_staging_reference(
            reference,
            expected_operation_id=expected_operation_id,
            expected_item_id=expected_item_id,
        ):
            identity = _staging_reference_identity(reference)
            if identity in staging_identities:
                raise ValueError("staging recovery references must be unique")
            staging_identities.add(identity)
            staging.append(reference)
        elif _strict_managed_reservation(
            reference,
            expected_operation_id=expected_operation_id,
            expected_item_id=expected_item_id,
        ):
            reservations.append(reference)
        elif _strict_managed_reference(
            reference,
            expected_operation_id=expected_operation_id,
            expected_item_id=expected_item_id,
        ):
            carriers.append(reference)
        elif _strict_managed_prelaunch_settlement(
            reference,
            expected_operation_id=expected_operation_id,
            expected_item_id=expected_item_id,
        ):
            settlements.append(reference)
        elif _strict_library_backup_intent(
            reference,
            expected_operation_id=expected_operation_id,
            expected_item_id=expected_item_id,
        ):
            backup_intents.append(reference)
        elif _strict_library_backup_carrier(
            reference,
            expected_operation_id=expected_operation_id,
            expected_item_id=expected_item_id,
        ):
            backup_carriers.append(reference)
        elif _strict_library_backup_settlement(
            reference,
            expected_operation_id=expected_operation_id,
            expected_item_id=expected_item_id,
        ):
            backup_settlements.append(reference)
        else:
            raise ValueError("journal item has an unsupported recovery reference")
    if len(reservations) + len(carriers) + len(settlements) > 1:
        raise ValueError("journal items may carry only one managed recovery reference")
    if len(backup_intents) + len(backup_carriers) + len(backup_settlements) > 1:
        raise ValueError("journal items may carry only one library backup reference")
    return {
        "staging": tuple(staging),
        "reservations": tuple(reservations),
        "carriers": tuple(carriers),
        "settlements": tuple(settlements),
        "backup_intents": tuple(backup_intents),
        "backup_carriers": tuple(backup_carriers),
        "backup_settlements": tuple(backup_settlements),
    }


def _parse_retirement(
    value: Any,
    *,
    expected_operation_id: str,
) -> CarrierRetirement:
    if type(value) is not dict:
        raise ValueError("carrier retirement has an invalid schema")
    keys = frozenset(value)
    if keys not in {_LEGACY_RETIREMENT_KEYS, _RETIREMENT_KEYS}:
        raise ValueError("carrier retirement has an invalid schema")
    item_id = _validate_id(value["item_id"], "retirement item_id")
    reference = _parse_reference(value["reference"])
    if not _strict_managed_reference(
        reference,
        expected_operation_id=expected_operation_id,
        expected_item_id=item_id,
    ):
        raise ValueError("carrier retirement reference is malformed")
    manifest_hash = _validate_id(value["manifest_hash"], "manifest_hash")
    quarantine_name = value["quarantine_name"]
    prefix = ".qobuz-retire-managed-beets-"
    if (
        not isinstance(quarantine_name, str)
        or not quarantine_name.startswith(prefix)
        or _HEX_ID.fullmatch(quarantine_name[len(prefix):]) is None
    ):
        raise ValueError("carrier retirement quarantine name is malformed")
    try:
        state = CarrierRetirementState(value["state"])
    except (TypeError, ValueError) as exc:
        raise ValueError("carrier retirement has an invalid state") from exc
    if keys == _LEGACY_RETIREMENT_KEYS:
        return CarrierRetirement(
            item_id=item_id,
            reference=reference,
            manifest_hash=manifest_hash,
            quarantine_name=quarantine_name,
            state=state,
        )

    planned = _validate_planned(value["planned"])
    owner = RecoveryOwner(expected_operation_id, item_id)
    completion_input = parse_completion_input_record(
        value["completion_input"], expected_owner=owner
    )
    if completion_input is None or not completion_input_ready(completion_input):
        raise ValueError("carrier retirement completion input is invalid")
    if (
        normalise_album_id(planned["album"].get("id"))
        != completion_input.expectation.album_id
    ):
        raise ValueError("carrier retirement album does not match completion input")
    completion_evidence = parse_completion_record(
        value["completion_evidence"],
        expected_owner=owner,
        expected_expectation=completion_input.expectation,
        expected_download=completion_input.download_coverage(),
    )
    if completion_evidence is None:
        raise ValueError("carrier retirement completion evidence is invalid")
    if completion_evidence.managed_manifest_hash != manifest_hash:
        raise ValueError("carrier retirement manifest does not match completion")
    original_path = _canonical_library_album_path(
        os.path.join(
            completion_evidence.library_root,
            completion_evidence.album_path,
        ),
        "carrier retirement completion path",
    )
    final_path = _canonical_library_album_path(
        value["final_path"], "carrier retirement final_path"
    )
    action = (
        None
        if value["action"] is None
        else _parse_post_import_action(value["action"])
    )
    acknowledged = value["completion_acknowledged"]
    if type(acknowledged) is not bool:
        raise ValueError("carrier retirement acknowledgement is invalid")
    if acknowledged and action is not None:
        raise ValueError("acknowledged carrier retirement still has an action")
    if action is not None:
        if action.kind == "whole-album":
            if action.source != original_path:
                raise ValueError(
                    "whole-album action does not start at its completion path"
                )
            expected_final = (
                action.destination
                if action.phase is PostImportActionPhase.COMMITTED
                else action.source
            )
        else:
            planned_album_dir = planned.get("album_dir")
            try:
                planned_album_dir = _canonical_library_album_path(
                    planned_album_dir,
                    "split action planned album path",
                )
            except ValueError as exc:
                raise ValueError(
                    "split action requires an exact planned album path"
                ) from exc
            if (
                action.destination != original_path
                or action.source != planned_album_dir
            ):
                raise ValueError(
                    "split action does not join its planned and completed paths"
                )
            expected_final = action.destination
        if final_path != expected_final:
            raise ValueError("carrier retirement path does not match its action")
    return CarrierRetirement(
        item_id=item_id,
        reference=reference,
        manifest_hash=manifest_hash,
        quarantine_name=quarantine_name,
        state=state,
        planned=planned,
        completion_input=completion_input.to_record(),
        completion_evidence=completion_evidence.to_record(),
        final_path=final_path,
        action=action,
        completion_acknowledged=acknowledged,
    )


def _item_payload(item: JournalItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "phase": item.phase.value,
        "planned": _validate_planned(item.planned),
        "recovery_references": [
            _reference_payload(reference) for reference in item.recovery_references
        ],
        "block_reason": item.block_reason,
        "completion_input": (
            None
            if item.completion_input is None
            else _json_copy(item.completion_input)
        ),
        "completion_evidence": (
            None
            if item.completion_evidence is None
            else _json_copy(item.completion_evidence)
        ),
        "multi_artist_filing": item.multi_artist_filing,
    }


def _parse_item(value: Any, *, expected_operation_id: str) -> JournalItem:
    if (
        not isinstance(value, dict)
        or frozenset(value) not in {_LEGACY_ITEM_KEYS, _ITEM_KEYS}
    ):
        raise ValueError("journal item has an invalid schema")
    multi_artist_filing = value.get("multi_artist_filing", False)
    if type(multi_artist_filing) is not bool:
        raise ValueError("multi_artist_filing must be a boolean")
    item_id = _validate_id(value["item_id"], "item_id")
    try:
        phase = QueuePhase(value["phase"])
    except (TypeError, ValueError) as exc:
        raise ValueError("journal item has an invalid phase") from exc
    references_value = value["recovery_references"]
    if not isinstance(references_value, list):
        raise ValueError("recovery_references must be a list")
    references = tuple(_parse_reference(reference) for reference in references_value)
    inventory = _recovery_reference_inventory(
        references,
        expected_operation_id=expected_operation_id,
        expected_item_id=item_id,
    )
    staging = inventory["staging"]
    reservation = (
        inventory["reservations"][0] if inventory["reservations"] else None
    )
    managed_carrier = inventory["carriers"][0] if inventory["carriers"] else None
    prelaunch_settlement = (
        inventory["settlements"][0] if inventory["settlements"] else None
    )
    backup_intent = (
        inventory["backup_intents"][0]
        if inventory["backup_intents"] else None
    )
    backup_carrier = (
        inventory["backup_carriers"][0]
        if inventory["backup_carriers"] else None
    )
    backup_settlement = (
        inventory["backup_settlements"][0]
        if inventory["backup_settlements"] else None
    )
    planned = _validate_planned(value["planned"])
    block_reason = value["block_reason"]
    if block_reason is not None and not isinstance(block_reason, str):
        raise ValueError("block_reason must be a string or null")
    if phase is QueuePhase.BLOCKED:
        if not block_reason:
            raise ValueError("blocked journal items require a reason")
    elif block_reason is not None:
        raise ValueError("only blocked journal items may have a block reason")
    completion_input = value["completion_input"]
    parsed_input = None
    if completion_input is not None:
        parsed_input = parse_completion_input_record(
            completion_input,
            expected_owner=RecoveryOwner(expected_operation_id, item_id),
        )
        if parsed_input is None:
            raise ValueError("completion_input is invalid")
        if (
            normalise_album_id(planned["album"].get("id"))
            != parsed_input.expectation.album_id
        ):
            raise ValueError("completion input album does not match the planned item")
        completion_input = parsed_input.to_record()
    completion_evidence = value["completion_evidence"]
    if completion_evidence is not None and not isinstance(completion_evidence, dict):
        raise ValueError("completion_evidence must be an object or null")
    if phase is QueuePhase.PENDING and references:
        raise ValueError("pending journal items cannot carry recovery state")
    if phase is QueuePhase.PENDING and completion_input is not None:
        raise ValueError("pending journal items cannot carry completion input")
    if phase is QueuePhase.PENDING and multi_artist_filing:
        raise ValueError("pending journal items cannot freeze filing policy")
    if phase is QueuePhase.ACTIVE and completion_input is None:
        raise ValueError("active journal items require completion input")
    if phase is QueuePhase.ACTIVE:
        if (
            managed_carrier is not None
            or prelaunch_settlement is not None
            or backup_settlement is not None
        ):
            raise ValueError(
                "active journal recovery state cannot carry resolved state"
            )
        if reservation is not None and (
            parsed_input is None or not completion_input_ready(parsed_input)
        ):
            raise ValueError(
                "active managed recovery requires complete source lineage"
            )
    if phase in (QueuePhase.RESOLVING, QueuePhase.COMPLETE):
        if parsed_input is None or not completion_input_ready(parsed_input):
            raise ValueError(
                f"{phase.value} journal items require complete source lineage"
            )
    if phase is QueuePhase.RESOLVING:
        if managed_carrier is None or reservation is not None:
            raise ValueError(
                "resolving journal items require one managed Beets carrier"
            )
        if backup_intent is not None:
            raise ValueError(
                "resolving journal items cannot carry an unpromoted backup intent"
            )
    if phase is QueuePhase.BLOCKED and references:
        if prelaunch_settlement is not None and staging:
            raise ValueError(
                "pre-launch settlement cannot carry staging recovery state"
            )
        if parsed_input is None:
            raise ValueError("blocked recovery state requires completion input")
        if (
            reservation is not None
            or managed_carrier is not None
            or prelaunch_settlement is not None
            or backup_settlement is not None
        ) and not completion_input_ready(parsed_input):
            raise ValueError(
                "blocked managed recovery requires complete source lineage"
            )
    if phase is not QueuePhase.COMPLETE and completion_evidence is not None:
        raise ValueError(
            f"{phase.value} journal items cannot carry completion evidence"
        )
    if phase is QueuePhase.COMPLETE:
        if (
            completion_evidence is None
            or managed_carrier is None
            or staging
            or reservation is not None
            or prelaunch_settlement is not None
            or backup_intent is not None
            or backup_carrier is not None
            or backup_settlement is not None
        ):
            raise ValueError(
                "complete journal items require completion evidence and one "
                "resolved managed Beets carrier"
            )
        parsed_evidence = parse_completion_record(
            completion_evidence,
            expected_owner=RecoveryOwner(expected_operation_id, item_id),
            expected_expectation=parsed_input.expectation,
            expected_download=parsed_input.download_coverage(),
        )
        if parsed_evidence is None:
            raise ValueError("complete journal item evidence is invalid")
        completion_evidence = parsed_evidence.to_record()
    return JournalItem(
        item_id=item_id,
        phase=phase,
        planned=planned,
        recovery_references=references,
        block_reason=block_reason,
        completion_input=completion_input,
        completion_evidence=completion_evidence,
        multi_artist_filing=multi_artist_filing,
    )


def _journal_payload(journal: QueueJournal) -> dict[str, Any]:
    return {
        "version": cfg.PENDING_QUEUE_VERSION,
        "operation_id": journal.operation_id,
        "generation": journal.generation,
        "saved_at": journal.saved_at,
        "mode": journal.mode,
        "count": len(journal.items),
        "items": [_item_payload(item) for item in journal.items],
        "retirement_count": len(journal.retirements),
        "retirements": [
            _retirement_payload(retirement)
            for retirement in journal.retirements
        ],
        "legacy_source_sha256": journal.legacy_source_sha256,
    }


def _parse_journal(value: Any, expected_operation_id: str) -> QueueJournal:
    if not isinstance(value, dict) or set(value) != _JOURNAL_KEYS:
        raise ValueError("queue journal has an invalid schema")
    if type(value["version"]) is not int or value["version"] != cfg.PENDING_QUEUE_VERSION:
        raise ValueError(f"queue journal version {value.get('version')!r} is unsupported")
    operation_id = _validate_id(value["operation_id"], "operation_id")
    if operation_id != expected_operation_id:
        raise ValueError("queue journal operation id does not match its filename")
    generation = value["generation"]
    if type(generation) is not int or generation < 0:
        raise ValueError("queue journal generation must be a non-negative integer")
    mode = value["mode"]
    if not isinstance(mode, str) or not mode:
        raise ValueError("queue journal mode must be a non-empty string")
    items_value = value["items"]
    if not isinstance(items_value, list):
        raise ValueError("queue journal items must be a list")
    if type(value["count"]) is not int or value["count"] != len(items_value):
        raise ValueError("queue journal count does not match its items")
    items = tuple(_parse_item(
        item, expected_operation_id=operation_id) for item in items_value)
    item_ids = [item.item_id for item in items]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("queue journal item ids must be unique")
    retirements_value = value["retirements"]
    if not isinstance(retirements_value, list):
        raise ValueError("queue journal retirements must be a list")
    if (
        type(value["retirement_count"]) is not int
        or value["retirement_count"] != len(retirements_value)
    ):
        raise ValueError(
            "queue journal retirement count does not match its retirements"
        )
    retirements = tuple(_parse_retirement(
        retirement,
        expected_operation_id=operation_id,
    ) for retirement in retirements_value)
    retirement_ids = [retirement.item_id for retirement in retirements]
    if (
        len(retirement_ids) != len(set(retirement_ids))
        or set(retirement_ids).intersection(item_ids)
    ):
        raise ValueError("queue journal retirement item ids must be unique")
    legacy_source_sha256 = value["legacy_source_sha256"]
    if legacy_source_sha256 is not None:
        _validate_id(legacy_source_sha256, "legacy_source_sha256")
    return QueueJournal(
        operation_id=operation_id,
        mode=mode,
        saved_at=_validate_timestamp(value["saved_at"]),
        items=items,
        generation=generation,
        legacy_source_sha256=legacy_source_sha256,
        retirements=retirements,
    )


def _fsync_directory(path: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise QueueJournalError(f"queue journal ancestor is not a directory: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _confirm_namespace_durable(directory: Path) -> None:
    """Fsync the journal directory and every relationship back to its root."""
    cursor = directory if directory.is_absolute() else directory.absolute()
    while True:
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise QueueJournalError(
                f"queue journal ancestor is not a directory: {cursor}"
            )
        _fsync_directory(cursor)
        parent = cursor.parent
        if parent == cursor:
            return
        cursor = parent


def _ensure_journal_dir() -> Path:
    directory = cfg.QUEUE_JOURNAL_DIR
    missing = []
    cursor = directory
    while not _path_exists(cursor):
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            raise QueueJournalError(
                f"queue journal directory has no existing ancestor: {directory}"
            )
        cursor = parent

    ancestor_info = cursor.lstat()
    if stat.S_ISLNK(ancestor_info.st_mode) or not stat.S_ISDIR(
        ancestor_info.st_mode
    ):
        raise QueueJournalError(
            f"queue journal ancestor is not a directory: {cursor}"
        )

    for path in reversed(missing):
        created = False
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise QueueJournalError(
                f"queue journal location is not a directory: {path}"
            )
        if created:
            _fsync_directory(path.parent)

    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise QueueJournalError(
            f"queue journal location is not a directory: {directory}"
        )
    # A previous mkdir may have succeeded just before its parent fsync failed.
    # Reconfirm the complete chain on every retry before any state is exposed.
    _confirm_namespace_durable(directory)
    return directory


def _open_namespace_lock() -> int:
    directory = _ensure_journal_dir()
    path = directory / _JOURNAL_LOCK_NAME
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = -1
    locked = False
    try:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise QueueJournalError(
                f"queue journal lock is not a private regular file: {path}"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        settled = os.fstat(descriptor)
        linked = path.lstat()
        if (
            not stat.S_ISREG(settled.st_mode)
            or settled.st_nlink != 1
            or not stat.S_ISREG(linked.st_mode)
            or linked.st_nlink != 1
            or linked.st_dev != settled.st_dev
            or linked.st_ino != settled.st_ino
        ):
            raise QueueJournalError(
                f"queue journal lock changed while it was opened: {path}"
            )
        os.fsync(descriptor)
        # This confirms both a newly linked lock and every ancestor that may
        # have survived an earlier mkdir/parent-fsync failure.
        _confirm_namespace_durable(directory)
        return descriptor
    except BaseException as exc:
        if locked:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, QueueJournalError):
            raise
        raise QueueJournalError(f"queue journal lock could not be established: {exc}") from exc


@contextmanager
def _journal_namespace_lock():
    global _namespace_lock_depth, _namespace_lock_descriptor

    entry_pid = os.getpid()
    entry_epoch = _namespace_lock_epoch
    thread_lock = _NAMESPACE_THREAD_LOCK
    thread_lock.acquire()
    try:
        outermost = _namespace_lock_depth == 0
        if outermost:
            _namespace_lock_descriptor = _open_namespace_lock()
        _namespace_lock_depth += 1
    except BaseException:
        thread_lock.release()
        raise
    try:
        yield
    finally:
        if os.getpid() == entry_pid and _namespace_lock_epoch == entry_epoch:
            try:
                _namespace_lock_depth -= 1
                if outermost:
                    descriptor = _namespace_lock_descriptor
                    _namespace_lock_descriptor = None
                    if descriptor is not None:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                        finally:
                            os.close(descriptor)
            finally:
                thread_lock.release()


def _locked_transaction(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _journal_namespace_lock():
            return function(*args, **kwargs)

    return locked


def _locked_queue_load(function):
    @wraps(function)
    def locked(*args, **kwargs):
        try:
            with _journal_namespace_lock():
                return function(*args, **kwargs)
        except (OSError, QueueJournalError) as exc:
            return QueueLoad(
                status=QueueLoadStatus.BLOCKED,
                reason=str(exc),
                paths=getattr(
                    exc,
                    "paths",
                    (cfg.QUEUE_JOURNAL_DIR / _JOURNAL_LOCK_NAME,),
                ),
            )

    return locked


def _locked_queue_list(function):
    @wraps(function)
    def locked(*args, **kwargs):
        try:
            with _journal_namespace_lock():
                return function(*args, **kwargs)
        except (OSError, QueueJournalError) as exc:
            return (QueueLoad(
                status=QueueLoadStatus.BLOCKED,
                reason=str(exc),
                paths=getattr(
                    exc,
                    "paths",
                    (cfg.QUEUE_JOURNAL_DIR / _JOURNAL_LOCK_NAME,),
                ),
            ),)

    return locked


def _journal_path(operation_id: str) -> Path:
    return cfg.QUEUE_JOURNAL_DIR / f"{_validate_id(operation_id, 'operation_id')}.json"


def _write_durable_json(path: Path, payload: dict[str, Any]) -> None:
    directory = _ensure_journal_dir()
    if path.parent != directory:
        raise QueueJournalError("queue journal path is outside its configured directory")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{path.stem}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            written = stream.write(encoded)
            if written != len(encoded):
                raise OSError("short write while saving queue journal")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_regular_file(path: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise QueueJournalError(f"queue journal is not a regular file: {path}")
        if opened.st_size > _MAX_JOURNAL_BYTES:
            raise QueueJournalError(
                f"queue journal exceeds {_MAX_JOURNAL_BYTES} bytes: {path}")
        raw = os.pread(descriptor, opened.st_size + 1, 0)
        settled = os.fstat(descriptor)
        if (
            len(raw) != opened.st_size
            or (
                settled.st_dev,
                settled.st_ino,
                settled.st_size,
                settled.st_mtime_ns,
                settled.st_ctime_ns,
            ) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
        ):
            raise QueueJournalError(
                f"queue journal changed while it was read: {path}")
        return raw
    finally:
        os.close(descriptor)


def _unlink_and_sync(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def create_queue_journal(items, *, mode: str) -> QueueJournal:
    """Create an uncommitted operation whose items are all still pending."""
    if not isinstance(mode, str) or not mode:
        raise ValueError("queue journal mode must be a non-empty string")
    operation_id = _new_id()
    while _path_exists(_journal_path(operation_id)):
        operation_id = _new_id()
    journal_items = tuple(
        JournalItem(
            item_id=_new_id(),
            phase=QueuePhase.PENDING,
            planned=_serialize_queue_item(item),
        )
        for item in items
    )
    return QueueJournal(
        operation_id=operation_id,
        mode=mode,
        saved_at=_utc_now(),
        items=journal_items,
    )


def _journal_snapshot_bytes(journal: QueueJournal) -> bytes:
    payload = _journal_payload(journal)
    validated = _parse_journal(payload, journal.operation_id)
    return json.dumps(
        _journal_payload(validated),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@_locked_transaction
def _publish_initial_journal(
    journal: QueueJournal,
    *,
    legacy_migration: bool,
) -> QueueJournal:
    _discover_journal_paths()
    if type(journal.generation) is not int or journal.generation < 0:
        raise ValueError("queue journal generation must be a non-negative integer")
    if journal.generation != 0:
        raise QueueJournalBlocked(
            "saved queue journal disappeared; refusing to recreate stale state"
        )
    if any(item.phase is not QueuePhase.PENDING for item in journal.items):
        raise QueueJournalBlocked(
            "a new queue journal may contain only pending items"
        )
    if journal.retirements:
        raise QueueJournalBlocked(
            "a new queue journal cannot manufacture carrier retirements"
        )
    if journal.legacy_source_sha256 is not None and not legacy_migration:
        raise QueueJournalBlocked(
            "a migration marker can be created only while migrating legacy state"
        )
    path = _journal_path(journal.operation_id)
    if _path_exists(path):
        raise QueueJournalBlocked(
            "queue journal already exists; use an authorised state transition"
        )
    saved = replace(journal, saved_at=_utc_now(), generation=1)
    payload = _journal_payload(saved)
    _parse_journal(payload, saved.operation_id)
    _write_durable_json(path, payload)
    return saved


@_locked_transaction
def save_queue_journal(journal: QueueJournal) -> QueueJournal:
    """Publish a new pending operation; existing state uses narrow transitions."""
    return _publish_initial_journal(journal, legacy_migration=False)


def _reload_matching_journal(
    previous: QueueJournal,
    *,
    allow_legacy_marker: bool = False,
) -> QueueJournal:
    _discover_journal_paths()
    path = _journal_path(previous.operation_id)
    if not _path_exists(path):
        raise QueueJournalBlocked(
            "saved queue journal disappeared; refusing to recreate stale state"
        )
    loaded = _load_journal_path(path, previous.operation_id)
    current = loaded.journal
    if loaded.status is not QueueLoadStatus.READY or current is None:
        raise QueueJournalBlocked(
            "existing queue journal is unreadable and will not be overwritten"
        )
    if current.legacy_source_sha256 is not None and not allow_legacy_marker:
        raise QueueJournalBlocked(
            "legacy migration must be resolved before queue state can change"
        )
    try:
        matches = _journal_snapshot_bytes(current) == _journal_snapshot_bytes(previous)
    except (AttributeError, RecursionError, TypeError, ValueError) as exc:
        raise QueueJournalBlocked("provided queue journal snapshot is invalid") from exc
    if not matches:
        raise QueueJournalBlocked(
            "stale queue journal snapshot; reload the current generation"
        )
    return current


@_locked_transaction
def _compare_and_commit(
    previous: QueueJournal,
    updated: QueueJournal,
    *,
    legacy_marker_clear: bool = False,
) -> QueueJournal:
    current = _reload_matching_journal(
        previous,
        allow_legacy_marker=legacy_marker_clear,
    )
    if legacy_marker_clear:
        if current.legacy_source_sha256 is None:
            raise QueueJournalBlocked("queue journal has no legacy migration marker")
        expected = replace(current, legacy_source_sha256=None)
        if _journal_snapshot_bytes(updated) != _journal_snapshot_bytes(expected):
            raise QueueJournalBlocked(
                "legacy cleanup may only clear the exact migration marker"
            )
    if (
        updated.operation_id != current.operation_id
        or type(updated.generation) is not int
        or updated.generation != current.generation
        or updated.saved_at != current.saved_at
    ):
        raise QueueJournalBlocked(
            "queue journal update is not based on the current saved snapshot"
        )
    saved = replace(
        updated,
        saved_at=_utc_now(),
        generation=current.generation + 1,
    )
    payload = _journal_payload(saved)
    _parse_journal(payload, saved.operation_id)
    _write_durable_json(_journal_path(saved.operation_id), payload)
    return saved


@_locked_transaction
def _load_journal_path(path: Path, operation_id: str) -> QueueLoad:
    try:
        raw = _read_regular_file(path)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
        journal = _parse_journal(value, operation_id)
    except (
        OSError,
        RecursionError,
        UnicodeError,
        json.JSONDecodeError,
        QueueJournalError,
        ValueError,
    ) as exc:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason=str(exc),
            paths=(path,),
        )
    return QueueLoad(
        status=QueueLoadStatus.READY,
        journal=journal,
        paths=(path,),
    )


def _discover_journal_paths() -> tuple[Path, ...]:
    directory = cfg.QUEUE_JOURNAL_DIR
    if not _path_exists(directory):
        return ()
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise QueueJournalError(f"queue journal location is not a directory: {directory}")
    journal_paths = []
    ambiguous_paths = []
    for path in sorted(directory.iterdir()):
        if path.name == _JOURNAL_LOCK_NAME:
            continue
        if (
            path.name.endswith(".json")
            and _HEX_ID.fullmatch(path.stem) is not None
        ):
            journal_paths.append(path)
        else:
            ambiguous_paths.append(path)
    if ambiguous_paths:
        raise _QueueNamespaceBlocked(
            "queue journal directory contains unrecognised persisted state",
            tuple(ambiguous_paths),
        )
    return tuple(journal_paths)


def _parse_legacy_queue(raw: bytes) -> tuple[list[dict[str, Any]], str, str]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy pending queue is unreadable") from exc
    if not isinstance(value, dict) or set(value) != _LEGACY_KEYS:
        raise ValueError("legacy pending queue has an invalid schema")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError(f"pending queue version {value.get('version')!r} is unsupported")
    mode = value["mode"]
    if not isinstance(mode, str) or not mode:
        raise ValueError("legacy pending queue mode must be a non-empty string")
    items = value["items"]
    if not isinstance(items, list):
        raise ValueError("legacy pending queue items must be a list")
    if type(value["count"]) is not int or value["count"] != len(items):
        raise ValueError("legacy pending queue count does not match its items")
    planned = [_validate_planned(item) for item in items]
    return planned, mode, _validate_timestamp(value["saved_at"])


@_locked_transaction
def _migrate_legacy_queue(path: Path) -> QueueLoad:
    operation_id = _new_id()
    while _path_exists(_journal_path(operation_id)):
        operation_id = _new_id()
    journal_path = _journal_path(operation_id)
    try:
        raw = _read_regular_file(path)
        planned, mode, saved_at = _parse_legacy_queue(raw)
        journal = QueueJournal(
            operation_id=operation_id,
            mode=mode,
            saved_at=saved_at,
            items=tuple(
                JournalItem(
                    item_id=_new_id(),
                    phase=QueuePhase.PENDING,
                    planned=item,
                )
                for item in planned
            ),
            legacy_source_sha256=hashlib.sha256(raw).hexdigest(),
        )
        journal = _publish_initial_journal(journal, legacy_migration=True)
    except (OSError, QueueJournalError, RecursionError, ValueError) as exc:
        paths = (path, journal_path) if _path_exists(journal_path) else (path,)
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason=str(exc),
            paths=paths,
        )
    return _complete_legacy_cleanup(path, journal_path, journal)


@_locked_transaction
def _complete_legacy_cleanup(
    legacy_path: Path,
    journal_path: Path,
    journal: QueueJournal,
) -> QueueLoad:
    """Expose migrated state only after legacy removal is durably settled."""
    try:
        journal = _reload_matching_journal(
            journal,
            allow_legacy_marker=True,
        )
        if journal.legacy_source_sha256 is None:
            raise QueueJournalBlocked(
                "legacy and per-operation queue state both exist"
            )
        if _path_exists(legacy_path):
            raw = _read_regular_file(legacy_path)
            planned, mode, _saved_at = _parse_legacy_queue(raw)
            if (
                journal.legacy_source_sha256 != hashlib.sha256(raw).hexdigest()
                or journal.mode != mode
                or any(item.phase is not QueuePhase.PENDING for item in journal.items)
                or [item.planned for item in journal.items] != planned
            ):
                raise QueueJournalBlocked(
                    "legacy and per-operation queue state both exist"
                )
            _unlink_and_sync(legacy_path)
        else:
            # A prior unlink may have succeeded before its parent fsync failed.
            _fsync_directory(legacy_path.parent)
        cleared = _compare_and_commit(
            journal,
            replace(journal, legacy_source_sha256=None),
            legacy_marker_clear=True,
        )
    except (OSError, QueueJournalError, RecursionError, ValueError) as exc:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            journal=journal,
            reason=str(exc),
            paths=(legacy_path, journal_path),
        )
    return QueueLoad(
        status=QueueLoadStatus.READY,
        journal=cleared,
        paths=(journal_path,),
    )


@_locked_transaction
def _finish_legacy_migration(legacy_path: Path, journal_path: Path) -> QueueLoad:
    if _HEX_ID.fullmatch(journal_path.stem) is None:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason="queue journal directory contains an invalid journal filename",
            paths=(legacy_path, journal_path),
        )
    loaded = _load_journal_path(journal_path, journal_path.stem)
    journal = loaded.journal
    if loaded.status is not QueueLoadStatus.READY or journal is None:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason=loaded.reason or "queue journal could not be loaded",
            paths=(legacy_path, journal_path),
        )
    return _complete_legacy_cleanup(legacy_path, journal_path, journal)


@_locked_transaction
def _load_visible_journal_path(path: Path, operation_id: str) -> QueueLoad:
    loaded = _load_journal_path(path, operation_id)
    journal = loaded.journal
    if (
        loaded.status is QueueLoadStatus.READY
        and journal is not None
        and journal.legacy_source_sha256 is not None
    ):
        return _complete_legacy_cleanup(cfg.PENDING_QUEUE_FILE, path, journal)
    return loaded


@_locked_queue_load
def load_queue_journal(operation_id: str | None = None) -> QueueLoad:
    """Load one exact operation, or the sole saved operation for compatibility.

    Namespace-wide activation starts with ``list_queue_journals``. An exact
    unmarked operation stays independent of a separate legacy job, while an
    exact marked operation must reconcile that legacy state before READY.
    """
    _discover_journal_paths()
    if operation_id is not None:
        try:
            path = _journal_path(operation_id)
        except ValueError as exc:
            return QueueLoad(status=QueueLoadStatus.BLOCKED, reason=str(exc))
        if not _path_exists(path):
            return QueueLoad(status=QueueLoadStatus.ABSENT)
        return _load_visible_journal_path(path, operation_id)

    legacy_path = cfg.PENDING_QUEUE_FILE
    try:
        journal_paths = _discover_journal_paths()
    except (OSError, QueueJournalError) as exc:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason=str(exc),
            paths=getattr(exc, "paths", (cfg.QUEUE_JOURNAL_DIR,)),
        )
    if _path_exists(legacy_path):
        if journal_paths:
            if len(journal_paths) == 1:
                return _finish_legacy_migration(legacy_path, journal_paths[0])
            return QueueLoad(
                status=QueueLoadStatus.BLOCKED,
                reason="legacy and per-operation queue state both exist",
                paths=(legacy_path, *journal_paths),
            )
        return _migrate_legacy_queue(legacy_path)
    if not journal_paths:
        return QueueLoad(status=QueueLoadStatus.ABSENT)

    invalid_names = [path for path in journal_paths if _HEX_ID.fullmatch(path.stem) is None]
    if invalid_names:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason="queue journal directory contains an invalid journal filename",
            paths=tuple(invalid_names),
        )
    if len(journal_paths) != 1:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason="multiple queue operations require an explicit operation id",
            paths=journal_paths,
        )
    path = journal_paths[0]
    return _load_visible_journal_path(path, path.stem)


@_locked_transaction
def _reconcile_legacy_for_listing(
    legacy_path: Path,
    journal_paths: tuple[Path, ...],
) -> QueueLoad:
    try:
        raw = _read_regular_file(legacy_path)
        _parse_legacy_queue(raw)
    except (OSError, QueueJournalError, RecursionError, ValueError) as exc:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason=str(exc),
            paths=(legacy_path, *journal_paths),
        )
    digest = hashlib.sha256(raw).hexdigest()
    markers = []
    ambiguous = []
    blocked = []
    for path in journal_paths:
        if _HEX_ID.fullmatch(path.stem) is None:
            ambiguous.append(path)
            continue
        loaded = _load_journal_path(path, path.stem)
        if loaded.status is not QueueLoadStatus.READY or loaded.journal is None:
            ambiguous.append(path)
        elif any(
            item.phase is QueuePhase.BLOCKED
            for item in loaded.journal.items
        ):
            blocked.append(path)
        elif loaded.journal.legacy_source_sha256 is not None:
            markers.append((path, loaded.journal))
    marker_paths = [path for path, _journal in markers]
    if ambiguous or blocked:
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason=(
                "legacy queue coexists with blocked, unreadable, or ambiguous "
                "journal state"
            ),
            paths=(legacy_path, *blocked, *ambiguous, *marker_paths),
        )
    if markers:
        if len(markers) == 1 and markers[0][1].legacy_source_sha256 == digest:
            return _finish_legacy_migration(legacy_path, markers[0][0])
        return QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason="legacy queue bytes do not match the saved migration marker",
            paths=(legacy_path, *marker_paths),
        )
    return _migrate_legacy_queue(legacy_path)


@_locked_queue_list
def list_queue_journals() -> tuple[QueueLoad, ...]:
    """Return every saved operation without collapsing multiple jobs together."""
    try:
        journal_paths = _discover_journal_paths()
    except (OSError, QueueJournalError) as exc:
        return (QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason=str(exc),
            paths=getattr(exc, "paths", (cfg.QUEUE_JOURNAL_DIR,)),
        ),)

    legacy_result = None
    if _path_exists(cfg.PENDING_QUEUE_FILE):
        legacy_result = _reconcile_legacy_for_listing(
            cfg.PENDING_QUEUE_FILE,
            journal_paths,
        )
        try:
            journal_paths = _discover_journal_paths()
        except (OSError, QueueJournalError) as exc:
            return (QueueLoad(
                status=QueueLoadStatus.BLOCKED,
                reason=str(exc),
                paths=getattr(exc, "paths", (cfg.QUEUE_JOURNAL_DIR,)),
            ),)

    loads = []
    blocked_paths = set()
    if legacy_result is not None and legacy_result.status is QueueLoadStatus.BLOCKED:
        loads.append(legacy_result)
        blocked_paths.update(legacy_result.paths)
    preloaded = []
    for path in journal_paths:
        if path in blocked_paths:
            continue
        if _HEX_ID.fullmatch(path.stem) is None:
            preloaded.append((path, QueueLoad(
                status=QueueLoadStatus.BLOCKED,
                reason="queue journal filename is invalid",
                paths=(path,),
            )))
        else:
            preloaded.append((path, _load_journal_path(path, path.stem)))

    marker_entries = [
        (path, loaded)
        for path, loaded in preloaded
        if loaded.status is QueueLoadStatus.READY
        and loaded.journal is not None
        and loaded.journal.legacy_source_sha256 is not None
    ]
    preflight_blocked = [
        path
        for path, loaded in preloaded
        if loaded.status is QueueLoadStatus.BLOCKED
        or loaded.journal is not None
        and any(item.phase is QueuePhase.BLOCKED for item in loaded.journal.items)
    ]
    if marker_entries and (
        len(marker_entries) != 1 or preflight_blocked or loads
    ):
        return (*loads, QueueLoad(
            status=QueueLoadStatus.BLOCKED,
            reason=(
                "legacy migration marker coexists with blocked or ambiguous "
                "journal state"
            ),
            paths=tuple(dict.fromkeys((
                *(path for path, _loaded in marker_entries),
                *preflight_blocked,
            ))),
        ))

    for path, loaded in preloaded:
        journal = loaded.journal
        if (
            loaded.status is QueueLoadStatus.READY
            and journal is not None
            and journal.legacy_source_sha256 is not None
        ):
            loads.append(_complete_legacy_cleanup(
                cfg.PENDING_QUEUE_FILE,
                path,
                journal,
            ))
        else:
            loads.append(loaded)
    return tuple(loads)


def _replace_pending_items(journal: QueueJournal, items) -> QueueJournal:
    if journal.retirements:
        raise QueueJournalBlocked(
            "pending items cannot replace unresolved carrier retirements"
        )
    if any(item.phase is not QueuePhase.PENDING for item in journal.items):
        raise QueueJournalBlocked("an active queue operation cannot be replaced as pending")
    available = list(journal.items)
    replacement = []
    for queue_item in items:
        planned = _serialize_queue_item(queue_item)
        match_index = next(
            (index for index, item in enumerate(available) if item.planned == planned),
            None,
        )
        if match_index is None:
            replacement.append(JournalItem(
                item_id=_new_id(),
                phase=QueuePhase.PENDING,
                planned=planned,
            ))
        else:
            replacement.append(available.pop(match_index))
    return replace(journal, items=tuple(replacement))


@_locked_transaction
def save_pending_queue(items, *, mode: str) -> QueueJournal:
    """Compatibility save for pending-only callers, backed by one v2 journal."""
    loaded = load_queue_journal()
    if loaded.status is QueueLoadStatus.BLOCKED:
        raise QueueJournalBlocked(loaded.reason or "saved queue state is blocked")
    if loaded.status is QueueLoadStatus.ABSENT:
        journal = create_queue_journal(items, mode=mode)
        return save_queue_journal(journal)
    else:
        journal = loaded.journal
        if journal is None:
            raise QueueJournalBlocked("saved queue state could not be loaded")
        if journal.mode != mode:
            raise QueueJournalBlocked(
                f"saved queue mode {journal.mode!r} does not match {mode!r}"
            )
        updated = _replace_pending_items(journal, items)
    return _compare_and_commit(journal, updated)


@_locked_transaction
def transition_journal_item(
    journal: QueueJournal,
    item_id: str,
    phase: QueuePhase,
    *,
    recovery_references: tuple[RecoveryReference, ...] | None = None,
    block_reason: str | None = None,
    completion_input: CompletionInput | dict[str, Any] | None | object = _UNSET,
    completion_evidence: dict[str, Any] | None | object = _UNSET,
    multi_artist_filing: bool | object = _UNSET,
) -> QueueJournal:
    """Copy and durably commit one phase change without mutating the old snapshot."""
    if not isinstance(phase, QueuePhase):
        raise ValueError("phase must be a QueuePhase")
    if recovery_references is not None:
        if type(recovery_references) is not tuple:
            raise ValueError("recovery_references must be an exact tuple")
        canonical_references = []
        for reference in recovery_references:
            if type(reference) is not RecoveryReference:
                raise ValueError(
                    "recovery references must be exact RecoveryReference values"
                )
            canonical_references.append(
                _parse_reference(_reference_payload(reference))
            )
        recovery_references = tuple(canonical_references)
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    allowed = {
        QueuePhase.PENDING: {QueuePhase.ACTIVE, QueuePhase.BLOCKED},
        QueuePhase.ACTIVE: {QueuePhase.ACTIVE, QueuePhase.RESOLVING, QueuePhase.BLOCKED},
        QueuePhase.RESOLVING: {
            QueuePhase.RESOLVING,
            QueuePhase.COMPLETE,
            QueuePhase.BLOCKED,
        },
        QueuePhase.BLOCKED: {
            QueuePhase.ACTIVE,
            QueuePhase.BLOCKED,
            QueuePhase.RESOLVING,
        },
        QueuePhase.COMPLETE: set(),
    }
    if phase not in allowed[target.phase]:
        raise QueueJournalBlocked(
            f"queue item cannot move from {target.phase.value} to {phase.value}"
        )
    target_inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    if target.recovery_references:
        if recovery_references is not None:
            raise QueueJournalBlocked(
                "persisted recovery state must be preserved exactly"
            )
        if target_inventory["reservations"] and phase not in {
            QueuePhase.ACTIVE,
            QueuePhase.BLOCKED,
        }:
            raise QueueJournalBlocked(
                "a managed Beets reservation must be promoted explicitly"
            )
        if (
            target_inventory["carriers"]
            or target_inventory["settlements"]
            or target_inventory["backup_settlements"]
        ) and phase is QueuePhase.ACTIVE:
            raise QueueJournalBlocked(
                "an item with resolved recovery state cannot return to active"
            )
    elif recovery_references:
        for reference in recovery_references:
            if (
                type(reference) is RecoveryReference
                and reference.kind in _STAGING_REFERENCE_KINDS
            ):
                raise QueueJournalBlocked(
                    "staging recovery must be checkpointed explicitly"
                )
            if (
                type(reference) is RecoveryReference
                and reference.kind == _MANAGED_RESERVATION_REFERENCE_KIND
            ):
                raise QueueJournalBlocked(
                    "managed Beets recovery must be reserved explicitly"
                )
            if (
                type(reference) is RecoveryReference
                and reference.kind == _MANAGED_COMPLETION_REFERENCE_KIND
                and type(reference.data) is dict
                and reference.data.get("version") == 2
            ):
                raise QueueJournalBlocked(
                    "an exact managed Beets carrier must be promoted from its reservation"
                )
            if (
                type(reference) is RecoveryReference
                and reference.kind in _LIBRARY_BACKUP_REFERENCE_KINDS
            ):
                raise QueueJournalBlocked(
                    "library backup recovery must be checkpointed explicitly"
                )
    if phase is QueuePhase.COMPLETE and recovery_references is not None:
        raise QueueJournalBlocked(
            "completion must preserve the exact resolving recovery reference"
        )
    previous_input = (
        None
        if target.completion_input is None
        else parse_completion_input_record(
            target.completion_input,
            expected_owner=RecoveryOwner(previous.operation_id, item_id),
        )
    )
    if completion_input is _UNSET:
        input_record = target.completion_input
        next_input = previous_input
    elif completion_input is None:
        input_record = None
        next_input = None
    else:
        if type(completion_input) is CompletionInput:
            input_record = completion_input.to_record()
        elif type(completion_input) is dict:
            input_record = _json_copy(completion_input)
        else:
            raise ValueError("completion_input must be a completion input record")
        next_input = parse_completion_input_record(
            input_record,
            expected_owner=RecoveryOwner(previous.operation_id, item_id),
        )
        if next_input is None:
            raise ValueError("completion_input is invalid")
        input_record = next_input.to_record()
    if previous_input is None:
        if next_input is not None and not (
            target.phase in (QueuePhase.PENDING, QueuePhase.BLOCKED)
            and phase is QueuePhase.ACTIVE
        ):
            raise QueueJournalBlocked(
                "completion input can be frozen only when work becomes active"
            )
    elif (
        next_input is None
        or next_input != previous_input
        and not (
            target.phase is QueuePhase.ACTIVE
            and phase in (QueuePhase.ACTIVE, QueuePhase.RESOLVING)
            and completion_input_extends(previous_input, next_input)
        )
    ):
        raise QueueJournalBlocked(
            "completion input may only append active source lineage"
        )
    if multi_artist_filing is _UNSET:
        filing_policy = target.multi_artist_filing
    else:
        if type(multi_artist_filing) is not bool:
            raise ValueError("multi_artist_filing must be a boolean")
        filing_policy = multi_artist_filing
        if filing_policy != target.multi_artist_filing and not (
            target.phase in (QueuePhase.PENDING, QueuePhase.BLOCKED)
            and phase is QueuePhase.ACTIVE
            and target.completion_input is None
        ):
            raise QueueJournalBlocked(
                "multi-artist filing policy can be frozen only when work "
                "first becomes active"
            )
    updated = replace(
        target,
        phase=phase,
        recovery_references=(
            target.recovery_references
            if recovery_references is None
            else tuple(recovery_references)
        ),
        block_reason=block_reason,
        completion_input=input_record,
        completion_evidence=(
            target.completion_evidence
            if completion_evidence is _UNSET
            else (
                None
                if completion_evidence is None
                else _json_copy(completion_evidence)
            )
        ),
        multi_artist_filing=filing_policy,
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


def _canonical_staging_reference(
    record: dict[str, Any],
    *,
    kind: str,
    operation_id: str,
    item_id: str,
) -> RecoveryReference:
    if type(record) is not dict:
        raise ValueError("staging recovery record must be an object")
    data = _json_copy(record)
    path = data.get("path")
    if not isinstance(path, str):
        raise ValueError("staging recovery path must be a string")
    reference = RecoveryReference(
        name=_staging_reference_name(kind, path),
        kind=kind,
        data=data,
    )
    canonical = _parse_reference(_reference_payload(reference))
    if not _strict_staging_reference(
        canonical,
        expected_operation_id=operation_id,
        expected_item_id=item_id,
    ):
        raise ValueError("staging recovery reference is malformed")
    return canonical


@_locked_transaction
def _append_staging_reference(
    journal: QueueJournal,
    item_id: str,
    record: dict[str, Any],
    *,
    kind: str,
) -> QueueJournal:
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    allowed_phases = {QueuePhase.ACTIVE}
    if kind == _STAGING_GROUP_REFERENCE_KIND:
        # Parking a group is also how an authorised recovery throws away a
        # blocked item's leftovers, and reconcile_staging_references already
        # treats BLOCKED as recovery-bearing work. Refusing it here left the
        # settle path bouncing the item through ACTIVE, which the resolved-state
        # rule above rightly refuses once a library mutation has landed - so an
        # item that imported and then stranded a file could never be settled.
        allowed_phases |= {QueuePhase.RESOLVING, QueuePhase.BLOCKED}
    if target.phase not in allowed_phases:
        raise QueueJournalBlocked(
            f"{kind} recovery cannot be checkpointed from {target.phase.value}"
        )
    canonical = _canonical_staging_reference(
        record,
        kind=kind,
        operation_id=previous.operation_id,
        item_id=item_id,
    )
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    existing = next(
        (
            reference
            for reference in inventory["staging"]
            if _staging_reference_identity(reference)
            == _staging_reference_identity(canonical)
        ),
        None,
    )
    if existing is not None:
        if existing == canonical:
            return previous
        raise QueueJournalBlocked(
            "staging recovery state changed at an already checkpointed path"
        )
    updated = replace(
        target,
        recovery_references=_canonical_recovery_references((
            *target.recovery_references,
            canonical,
        )),
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


def append_staging_run_reference(
    journal: QueueJournal,
    item_id: str,
    record: dict[str, Any],
) -> QueueJournal:
    """Checkpoint one exact owned download root before rip work continues."""
    return _append_staging_reference(
        journal,
        item_id,
        record,
        kind=_DOWNLOAD_STAGING_REFERENCE_KIND,
    )


def append_staging_group_intent(
    journal: QueueJournal,
    item_id: str,
    record: dict[str, Any],
) -> QueueJournal:
    """Checkpoint one sealed owned staging-group intent before its rename."""
    return _append_staging_reference(
        journal,
        item_id,
        record,
        kind=_STAGING_GROUP_REFERENCE_KIND,
    )


@_locked_transaction
def reconcile_staging_references(
    journal: QueueJournal,
    item_id: str,
    settled_references: tuple[RecoveryReference, ...],
) -> QueueJournal:
    """Forget only exact staging references already proved safely settled."""
    if type(settled_references) is not tuple or not settled_references:
        raise ValueError("settled staging references must be a non-empty tuple")
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    if target.phase not in {
        QueuePhase.ACTIVE,
        QueuePhase.RESOLVING,
        QueuePhase.BLOCKED,
    }:
        raise QueueJournalBlocked(
            "staging recovery can be reconciled only from recovery-bearing work"
        )
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    canonical_settled = tuple(
        _parse_reference(_reference_payload(reference))
        for reference in settled_references
    )
    if len({reference.name for reference in canonical_settled}) != len(
        canonical_settled
    ):
        raise ValueError("settled staging references must be unique")
    for reference in canonical_settled:
        if not _strict_staging_reference(
            reference,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        ):
            raise ValueError("settled staging reference is malformed")
        if reference not in inventory["staging"]:
            raise QueueJournalBlocked(
                "settled staging reference does not match persisted state"
            )
    remaining = tuple(
        reference
        for reference in target.recovery_references
        if reference not in canonical_settled
    )
    updated = replace(target, recovery_references=remaining)
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def append_library_backup_intent(
    journal: QueueJournal,
    item_id: str,
    record: dict[str, Any],
) -> QueueJournal:
    """Commit the owner-bound backup destination before source mutation."""
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    if (
        target.phase is not QueuePhase.ACTIVE
        or inventory["backup_intents"]
        or inventory["backup_carriers"]
        or inventory["backup_settlements"]
    ):
        raise QueueJournalBlocked(
            "a library backup intent can be added only to active unbound work"
        )
    owner = {"operation_id": previous.operation_id, "item_id": item_id}
    canonical = canonical_library_backup_intent(
        record,
        expected_owner=owner,
    )
    if canonical is None:
        raise ValueError("library backup intent is malformed")
    reference = RecoveryReference(
        _LIBRARY_BACKUP_REFERENCE_NAME,
        _LIBRARY_BACKUP_INTENT_REFERENCE_KIND,
        canonical,
    )
    updated = replace(
        target,
        recovery_references=_canonical_recovery_references((
            *target.recovery_references,
            reference,
        )),
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def promote_library_backup_carrier(
    journal: QueueJournal,
    item_id: str,
    intent: RecoveryReference,
    carrier_record: dict[str, Any],
) -> QueueJournal:
    """Replace one exact pre-mutation intent with its sealed carrier."""
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    canonical_intent = _parse_reference(_reference_payload(intent))
    if (
        target.phase is not QueuePhase.ACTIVE
        or inventory["backup_intents"] != (canonical_intent,)
        or not _strict_library_backup_intent(
            canonical_intent,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        )
    ):
        raise QueueJournalBlocked(
            "library backup carrier does not match its persisted intent"
        )
    owner = {"operation_id": previous.operation_id, "item_id": item_id}
    canonical_carrier = canonical_library_backup_record(
        carrier_record,
        expected_owner=owner,
    )
    if canonical_carrier is None or not library_backup_matches_intent(
        canonical_carrier,
        canonical_intent.data,
        expected_owner=owner,
    ):
        raise ValueError("library backup carrier is malformed or mismatched")
    carrier = RecoveryReference(
        _LIBRARY_BACKUP_REFERENCE_NAME,
        _LIBRARY_BACKUP_CARRIER_REFERENCE_KIND,
        canonical_carrier,
    )
    remaining = tuple(
        reference
        for reference in target.recovery_references
        if reference != canonical_intent
    )
    updated = replace(
        target,
        recovery_references=_canonical_recovery_references((
            *remaining,
            carrier,
        )),
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def begin_library_backup_settlement(
    journal: QueueJournal,
    item_id: str,
    carrier: RecoveryReference,
    *,
    replacement_path: str,
    replacement_receipt: dict[str, Any],
    disposal_record: dict[str, Any],
) -> QueueJournal:
    """Persist exact disposal authority before retiring a library backup."""
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    canonical_carrier = _parse_reference(_reference_payload(carrier))
    if (
        target.phase is not QueuePhase.RESOLVING
        or inventory["backup_carriers"] != (canonical_carrier,)
        or inventory["backup_intents"]
        or inventory["backup_settlements"]
        or inventory["staging"]
        or len(inventory["carriers"]) != 1
    ):
        raise QueueJournalBlocked(
            "library backup settlement requires one resolving exact carrier"
        )
    canonical_receipt = canonical_album_source_receipt(
        replacement_receipt,
        expected_origin=replacement_path,
    )
    if canonical_receipt is None:
        raise ValueError("library replacement receipt is malformed")
    owner = {"operation_id": previous.operation_id, "item_id": item_id}
    canonical_disposal = canonical_library_backup_disposal_record(
        disposal_record,
        canonical_carrier.data,
        expected_owner=owner,
    )
    if canonical_disposal is None:
        raise ValueError("library backup disposal authority is malformed")
    settlement = RecoveryReference(
        _LIBRARY_BACKUP_REFERENCE_NAME,
        _LIBRARY_BACKUP_SETTLEMENT_REFERENCE_KIND,
        {
            "version": 2,
            "owner": owner,
            "carrier": canonical_carrier.data,
            "replacement_path": replacement_path,
            "replacement_receipt": canonical_receipt,
            "disposal": canonical_disposal,
        },
    )
    remaining = tuple(
        reference
        for reference in target.recovery_references
        if reference != canonical_carrier
    )
    updated = replace(
        target,
        recovery_references=_canonical_recovery_references((
            *remaining,
            settlement,
        )),
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def upgrade_library_backup_settlement(
    journal: QueueJournal,
    item_id: str,
    settlement: RecoveryReference,
    *,
    disposal_record: dict[str, Any],
) -> QueueJournal:
    """Add exact disposal authority to one untouched legacy settlement."""
    previous = _reload_matching_journal(journal)
    target_index = next(
        (
            index
            for index, item in enumerate(previous.items)
            if item.item_id == item_id
        ),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    canonical = _parse_reference(_reference_payload(settlement))
    if (
        target.phase is not QueuePhase.RESOLVING
        or inventory["backup_settlements"] != (canonical,)
        or inventory["backup_intents"]
        or inventory["backup_carriers"]
        or inventory["staging"]
        or len(inventory["carriers"]) != 1
        or not _strict_library_backup_settlement(
            canonical,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        )
        or canonical.data.get("version") != 1
    ):
        raise QueueJournalBlocked(
            "legacy library backup settlement changed before upgrade"
        )
    owner = {"operation_id": previous.operation_id, "item_id": item_id}
    canonical_disposal = canonical_library_backup_disposal_record(
        disposal_record,
        canonical.data["carrier"],
        expected_owner=owner,
    )
    if canonical_disposal is None:
        raise ValueError("library backup disposal authority is malformed")
    upgraded = RecoveryReference(
        _LIBRARY_BACKUP_REFERENCE_NAME,
        _LIBRARY_BACKUP_SETTLEMENT_REFERENCE_KIND,
        {
            **canonical.data,
            "version": 2,
            "disposal": canonical_disposal,
        },
    )
    if not _strict_library_backup_settlement(
        upgraded,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    ):
        raise ValueError("upgraded library backup settlement is malformed")
    updated = replace(
        target,
        recovery_references=_canonical_recovery_references((
            *(
                reference
                for reference in target.recovery_references
                if reference != canonical
            ),
            upgraded,
        )),
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def finish_library_backup_settlement(
    journal: QueueJournal,
    item_id: str,
    settlement: RecoveryReference,
) -> QueueJournal:
    """Forget only the exact settlement whose carrier is proved retired."""
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    canonical = _parse_reference(_reference_payload(settlement))
    if (
        target.phase is not QueuePhase.RESOLVING
        or inventory["backup_settlements"] != (canonical,)
        or not _strict_library_backup_settlement(
            canonical,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        )
    ):
        raise QueueJournalBlocked(
            "library backup settlement changed before completion"
        )
    updated = replace(
        target,
        recovery_references=tuple(
            reference
            for reference in target.recovery_references
            if reference != canonical
        ),
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def reset_unstarted_item_to_pending(
    journal: QueueJournal,
    item_id: str,
) -> QueueJournal:
    """Reset only an ACTIVE item proven not to have crossed a mutation gate."""
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    frozen_input = parse_completion_input_record(
        target.completion_input,
        expected_owner=RecoveryOwner(previous.operation_id, item_id),
    )
    if (
        target.phase is not QueuePhase.ACTIVE
        or target.recovery_references
        or target.completion_evidence is not None
        or frozen_input is None
        or frozen_input.lineages
        or frozen_input.counts is not None
    ):
        raise QueueJournalBlocked(
            "only empty, unstarted active work can return to pending"
        )
    updated = replace(
        target,
        phase=QueuePhase.PENDING,
        completion_input=None,
        multi_artist_filing=False,
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def reserve_managed_carrier(
    journal: QueueJournal,
    item_id: str,
    reservation: RecoveryReference,
) -> QueueJournal:
    """Durably reserve the exact private carrier path before it is created."""
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    if (
        target.phase is not QueuePhase.ACTIVE
        or inventory["reservations"]
        or inventory["carriers"]
        or inventory["settlements"]
    ):
        raise QueueJournalBlocked(
            "a managed Beets carrier can be reserved only for active unreserved work"
        )
    expected_owner = RecoveryOwner(previous.operation_id, item_id)
    frozen_input = parse_completion_input_record(
        target.completion_input,
        expected_owner=expected_owner,
    )
    if frozen_input is None or not completion_input_ready(frozen_input):
        raise QueueJournalBlocked(
            "a managed Beets carrier requires complete source lineage"
        )
    if not _strict_managed_reservation(
        reservation,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    ):
        raise ValueError("managed Beets carrier reservation is malformed")
    canonical_reservation = _parse_reference(_reference_payload(reservation))
    updated = replace(
        target,
        recovery_references=_canonical_recovery_references((
            *target.recovery_references,
            canonical_reservation,
        )),
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def promote_managed_carrier(
    journal: QueueJournal,
    item_id: str,
    reservation: RecoveryReference,
    carrier: RecoveryReference,
    completion_input: CompletionInput | dict[str, Any],
) -> QueueJournal:
    """Publish an exact created carrier and enter the resolving phase."""
    previous = _reload_matching_journal(journal)
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    inventory = _recovery_reference_inventory(
        target.recovery_references,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    )
    if (
        target.phase is not QueuePhase.ACTIVE
        or len(inventory["reservations"]) != 1
        or inventory["backup_intents"]
        or inventory["backup_settlements"]
    ):
        raise QueueJournalBlocked(
            "a managed Beets carrier can be promoted only from its active reservation"
        )
    if not _strict_managed_reservation(
        reservation,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    ):
        raise ValueError("managed Beets carrier reservation is malformed")
    canonical_reservation = _parse_reference(_reference_payload(reservation))
    if inventory["reservations"] != (canonical_reservation,):
        raise QueueJournalBlocked(
            "managed Beets carrier reservation does not match persisted state"
        )
    if (
        not _strict_managed_reference(
            carrier,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        )
        or carrier.data["version"] != 2
    ):
        raise ValueError("exact managed Beets carrier reference is malformed")
    canonical_carrier = _parse_reference(_reference_payload(carrier))
    reservation_data = canonical_reservation.data
    carrier_data = canonical_carrier.data
    if any(
        carrier_data[field] != reservation_data[field]
        for field in (
            "path",
            "parent_device",
            "parent_inode",
            "nonce",
            "owner",
        )
    ):
        raise QueueJournalBlocked(
            "managed Beets carrier does not match its persisted reservation"
        )
    expected_owner = RecoveryOwner(previous.operation_id, item_id)
    previous_input = parse_completion_input_record(
        target.completion_input,
        expected_owner=expected_owner,
    )
    if type(completion_input) is CompletionInput:
        input_record = completion_input.to_record()
    elif type(completion_input) is dict:
        input_record = _json_copy(completion_input)
    else:
        raise ValueError("completion_input must be a completion input record")
    next_input = parse_completion_input_record(
        input_record,
        expected_owner=expected_owner,
    )
    if next_input is None:
        raise ValueError("completion_input is invalid")
    if (
        previous_input is None
        or not completion_input_extends(previous_input, next_input)
        or not completion_input_ready(next_input)
    ):
        raise QueueJournalBlocked(
            "completion input may only append complete source lineage"
        )
    updated = replace(
        target,
        phase=QueuePhase.RESOLVING,
        recovery_references=_canonical_recovery_references((
            *inventory["staging"],
            *inventory["backup_carriers"],
            canonical_carrier,
        )),
        completion_input=next_input.to_record(),
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def commit_completed_item_removal(
    journal: QueueJournal,
    caller_items: list,
    *,
    item_id: str,
    caller_item,
    live_evidence: CompletionEvidence,
    post_import_action: dict[str, Any] | None | object = _UNSET,
) -> QueueJournal:
    """Replace a completed entry with durable carrier-cleanup authority.

    The executor must first validate the evidence schema and recompute the live
    completion proof; persisted evidence alone never authorizes this call.
    """
    previous = _reload_matching_journal(journal)
    if type(caller_items) is not list:
        raise QueueJournalBlocked("completed caller queue must be a list")
    target_index, target, resolved_carrier, live_record = (
        _validated_completed_removal(previous, item_id, live_evidence)
    )
    if _serialize_queue_item(caller_item) != target.planned:
        raise QueueJournalBlocked("caller item does not match the completed journal item")
    caller_index = next(
        (index for index, item in enumerate(caller_items) if item is caller_item),
        None,
    )
    if caller_index is None:
        raise QueueJournalBlocked("completed caller item is not in the queue")
    saved = _commit_completed_retirement(
        previous,
        target_index=target_index,
        target=target,
        resolved_carrier=resolved_carrier,
        live_record=live_record,
        post_import_action=post_import_action,
    )
    del caller_items[caller_index]
    return saved


def _validated_completed_removal(previous, item_id, live_evidence):
    target_index = next(
        (index for index, item in enumerate(previous.items) if item.item_id == item_id),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    if target.phase is not QueuePhase.COMPLETE:
        raise QueueJournalBlocked("only a completed queue item can be removed")
    owner = RecoveryOwner(previous.operation_id, item_id)
    completion_input = parse_completion_input_record(
        target.completion_input,
        expected_owner=owner,
    )
    if completion_input is None or not completion_input_ready(completion_input):
        raise QueueJournalBlocked("completed removal input is invalid")
    if type(live_evidence) is not CompletionEvidence:
        raise QueueJournalBlocked("completed removal requires live completion evidence")
    try:
        live_record = live_evidence.to_record()
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise QueueJournalBlocked(
            "completed removal requires valid live completion evidence"
        ) from exc
    parsed_live = parse_completion_record(
        live_record,
        expected_owner=owner,
        expected_expectation=completion_input.expectation,
        expected_download=completion_input.download_coverage(),
    )
    if parsed_live != live_evidence or target.completion_evidence != live_record:
        raise QueueJournalBlocked(
            "live completion does not match the persisted completion proof"
        )
    resolved_carrier = (
        target.recovery_references[0]
        if len(target.recovery_references) == 1
        else None
    )
    if resolved_carrier is None:
        raise QueueJournalBlocked(
            "completed removal requires its exact managed carrier"
        )
    return target_index, target, resolved_carrier, live_record


def _commit_completed_retirement(
    previous,
    *,
    target_index,
    target,
    resolved_carrier,
    live_record,
    post_import_action,
):
    journal_items = list(previous.items)
    del journal_items[target_index]
    payload = {
        "item_id": target.item_id,
        "reference": _reference_payload(resolved_carrier),
        "manifest_hash": live_record["managed_manifest_hash"],
        "quarantine_name": (
            ".qobuz-retire-managed-beets-"
            f"{secrets.token_hex(32)}"
        ),
        "state": CarrierRetirementState.PUBLIC.value,
    }
    if post_import_action is not _UNSET:
        if post_import_action is not None and type(post_import_action) is not dict:
            raise ValueError("post_import_action must be an action record or null")
        original_path = _canonical_library_album_path(
            os.path.join(
                live_record["library"]["root"],
                live_record["library"]["album"]["path"],
            ),
            "completed album path",
        )
        action = (
            None
            if post_import_action is None
            else _parse_post_import_action(_json_copy(post_import_action))
        )
        action_matches = action is None or (
            action.phase is PostImportActionPhase.PLANNED
            and (
                action.kind == "whole-album"
                and action.source == original_path
                or action.kind == "split-gap-fill"
                and action.destination == original_path
                and target.planned.get("album_dir") is not None
                and action.source == target.planned["album_dir"]
            )
        )
        if not action_matches:
            raise QueueJournalBlocked(
                "post-import action does not match the completed album"
            )
        initial_final_path = (
            action.destination
            if action is not None and action.kind == "split-gap-fill"
            else original_path
        )
        payload.update({
            "planned": target.planned,
            "completion_input": target.completion_input,
            "completion_evidence": live_record,
            "final_path": initial_final_path,
            "action": (
                None if action is None else _post_import_action_payload(action)
            ),
            "completion_acknowledged": False,
        })
    retirement = _parse_retirement(
        payload,
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(
        previous,
        replace(
            previous,
            items=tuple(journal_items),
            retirements=(*previous.retirements, retirement),
        ),
    )


@_locked_transaction
def commit_recovered_completed_item_removal(
    journal: QueueJournal,
    *,
    item_id: str,
    live_evidence: CompletionEvidence,
    post_import_action: dict[str, Any] | None | object = _UNSET,
) -> QueueJournal:
    """Replace a recovered COMPLETE item without inventing a runtime queue."""
    previous = _reload_matching_journal(journal)
    target_index, target, resolved_carrier, live_record = (
        _validated_completed_removal(previous, item_id, live_evidence)
    )
    return _commit_completed_retirement(
        previous,
        target_index=target_index,
        target=target,
        resolved_carrier=resolved_carrier,
        live_record=live_record,
        post_import_action=post_import_action,
    )


def _retirement_target(previous: QueueJournal, item_id: str):
    _validate_id(item_id, "retirement item_id")
    retirement_index = next(
        (
            index
            for index, retirement in enumerate(previous.retirements)
            if retirement.item_id == item_id
        ),
        None,
    )
    if retirement_index is None:
        raise KeyError(f"unknown carrier retirement: {item_id}")
    return retirement_index, previous.retirements[retirement_index]


def _replace_retirement(
    previous: QueueJournal,
    retirement_index: int,
    retirement: CarrierRetirement,
) -> QueueJournal:
    retirements = list(previous.retirements)
    retirements[retirement_index] = _parse_retirement(
        _retirement_payload(retirement),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(
        previous,
        replace(previous, retirements=tuple(retirements)),
    )


@_locked_transaction
def clear_planned_noop_post_import_action(
    journal: QueueJournal,
    *,
    item_id: str,
    action_id: str,
) -> QueueJournal:
    """Clear only an exact planned action that safely changed nothing."""
    action_id = _validate_id(action_id, "post-import action_id")
    previous = _reload_matching_journal(journal)
    retirement_index, retirement = _retirement_target(previous, item_id)
    action = retirement.action
    expected_final = None
    if action is not None:
        expected_final = (
            action.source
            if action.kind == "whole-album"
            else action.destination
        )
    if (
        retirement.planned is None
        or action is None
        or action.action_id != action_id
        or action.phase is not PostImportActionPhase.PLANNED
        or retirement.final_path != expected_final
    ):
        raise QueueJournalBlocked(
            "post-import action is not the exact planned no-op"
        )
    return _replace_retirement(
        previous,
        retirement_index,
        replace(retirement, action=None),
    )


@_locked_transaction
def checkpoint_post_import_action_handoff(
    journal: QueueJournal,
    *,
    item_id: str,
    action_id: str,
    operation_id: str,
    handoff_hash: str,
) -> QueueJournal:
    """Bind a planned action to one exact retained relocation handoff."""
    action_id = _validate_id(action_id, "post-import action_id")
    operation_id = _validate_id(
        operation_id, "post-import relocation operation_id"
    )
    handoff_hash = _validate_id(handoff_hash, "post-import handoff_hash")
    previous = _reload_matching_journal(journal)
    retirement_index, retirement = _retirement_target(previous, item_id)
    action = retirement.action
    if (
        retirement.planned is None
        or action is None
        or action.action_id != action_id
        or action.phase is not PostImportActionPhase.PLANNED
    ):
        raise QueueJournalBlocked(
            "post-import action is not the exact planned action"
        )
    updated_action = replace(
        action,
        phase=PostImportActionPhase.HANDOFF,
        relocation_operation_id=operation_id,
        handoff_hash=handoff_hash,
    )
    return _replace_retirement(
        previous,
        retirement_index,
        replace(retirement, action=updated_action),
    )


@_locked_transaction
def commit_post_import_action(
    journal: QueueJournal,
    *,
    item_id: str,
    action_id: str,
    operation_id: str,
    handoff_hash: str,
    final_path,
) -> QueueJournal:
    """Commit the exact handed-off action and its resulting album path."""
    action_id = _validate_id(action_id, "post-import action_id")
    operation_id = _validate_id(
        operation_id, "post-import relocation operation_id"
    )
    handoff_hash = _validate_id(handoff_hash, "post-import handoff_hash")
    try:
        final_path = os.fspath(final_path)
    except TypeError as exc:
        raise ValueError("post-import final_path is invalid") from exc
    final_path = _canonical_library_album_path(
        final_path, "post-import final_path"
    )
    previous = _reload_matching_journal(journal)
    retirement_index, retirement = _retirement_target(previous, item_id)
    action = retirement.action
    if (
        retirement.planned is None
        or action is None
        or action.action_id != action_id
        or action.phase is not PostImportActionPhase.HANDOFF
        or action.relocation_operation_id != operation_id
        or action.handoff_hash != handoff_hash
        or action.destination != final_path
    ):
        raise QueueJournalBlocked(
            "post-import action handoff changed before commit"
        )
    return _replace_retirement(
        previous,
        retirement_index,
        replace(
            retirement,
            action=replace(action, phase=PostImportActionPhase.COMMITTED),
            final_path=final_path,
        ),
    )


@_locked_transaction
def clear_committed_post_import_action(
    journal: QueueJournal,
    *,
    item_id: str,
    action_id: str,
) -> QueueJournal:
    """Clear only the exact action whose retained receipt was released."""
    action_id = _validate_id(action_id, "post-import action_id")
    previous = _reload_matching_journal(journal)
    retirement_index, retirement = _retirement_target(previous, item_id)
    action = retirement.action
    if (
        retirement.planned is None
        or action is None
        or action.action_id != action_id
        or action.phase is not PostImportActionPhase.COMMITTED
        or retirement.final_path != action.destination
    ):
        raise QueueJournalBlocked(
            "post-import action is not the exact committed action"
        )
    return _replace_retirement(
        previous,
        retirement_index,
        replace(retirement, action=None),
    )


@_locked_transaction
def acknowledge_carrier_retirement_completion(
    journal: QueueJournal,
    *,
    item_id: str,
) -> QueueJournal:
    """Record that the caller durably accepted the final album path."""
    previous = _reload_matching_journal(journal)
    retirement_index, retirement = _retirement_target(previous, item_id)
    if retirement.planned is None:
        raise QueueJournalBlocked(
            "legacy carrier retirement has no completion acknowledgement"
        )
    if retirement.action is not None:
        raise QueueJournalBlocked(
            "carrier completion cannot be acknowledged before its action settles"
        )
    if retirement.completion_acknowledged:
        return previous
    return _replace_retirement(
        previous,
        retirement_index,
        replace(retirement, completion_acknowledged=True),
    )


def post_import_relocation_handoff_matches(operation_id, handoff):
    """Check whether one relocation handoff is still owned by queue state."""
    try:
        operation_id = _validate_id(
            operation_id, "post-import relocation operation_id"
        )
        if type(handoff) is not dict or set(handoff) != {"consumer", "hash"}:
            return False
        consumer = handoff["consumer"]
        handoff_hash = _validate_id(
            handoff["hash"], "post-import handoff_hash"
        )
        if (
            type(consumer) is not dict
            or set(consumer) != {
                "kind",
                "queue_operation_id",
                "item_id",
                "action_id",
            }
            or consumer["kind"] != "queue-completion"
        ):
            return False
        queue_operation_id = _validate_id(
            consumer["queue_operation_id"], "queue operation_id"
        )
        item_id = _validate_id(consumer["item_id"], "queue item_id")
        action_id = _validate_id(
            consumer["action_id"], "post-import action_id"
        )
    except (KeyError, TypeError, ValueError):
        return False

    try:
        loaded = load_queue_journal(queue_operation_id)
    except Exception:
        return None
    if loaded.status is QueueLoadStatus.BLOCKED:
        return None
    if loaded.status is not QueueLoadStatus.READY or loaded.journal is None:
        return False
    retirement = next(
        (
            value
            for value in loaded.journal.retirements
            if value.item_id == item_id
        ),
        None,
    )
    action = None if retirement is None else retirement.action
    return bool(
        action is not None
        and action.action_id == action_id
        and action.phase in {
            PostImportActionPhase.HANDOFF,
            PostImportActionPhase.COMMITTED,
        }
        and action.relocation_operation_id == operation_id
        and action.handoff_hash is not None
        and secrets.compare_digest(action.handoff_hash, handoff_hash)
    )


@_locked_transaction
def process_carrier_retirement(
    journal: QueueJournal,
    *,
    item_id: str,
):
    """Retire one durable carrier and acknowledge only a settled outcome."""
    previous = _reload_matching_journal(journal)
    retirement_index, retirement = _retirement_target(previous, item_id)
    if retirement.planned is not None and (
        retirement.action is not None
        or not retirement.completion_acknowledged
    ):
        raise QueueJournalBlocked(
            "carrier retirement must settle its post-import completion first"
        )

    # Late for the same reason: beets is the heaviest import in the package and
    # nothing but a carrier retirement wants it.
    from qobuz_librarian.integrations import beets as beets_mod
    from qobuz_librarian.integrations.beets import ManagedCarrierRetirementOutcome

    result = beets_mod.retire_managed_carrier(
        retirement.reference.data,
        RecoveryOwner(previous.operation_id, retirement.item_id),
        retirement.manifest_hash,
        retirement.quarantine_name,
    )
    if result.outcome not in {
        ManagedCarrierRetirementOutcome.RETIRED,
        ManagedCarrierRetirementOutcome.ALREADY_ABSENT,
    }:
        return previous, result
    retirements = list(previous.retirements)
    del retirements[retirement_index]
    saved = _compare_and_commit(
        previous,
        replace(previous, retirements=tuple(retirements)),
    )
    return saved, result


def _settlement_target(previous: QueueJournal, item_id: str):
    target_index = next(
        (
            index
            for index, item in enumerate(previous.items)
            if item.item_id == item_id
        ),
        None,
    )
    if target_index is None:
        raise KeyError(f"unknown queue journal item: {item_id}")
    target = previous.items[target_index]
    if target.phase is not QueuePhase.BLOCKED:
        raise QueueJournalBlocked("only a blocked queue item can be settled")
    return target_index, target


def _validate_settlement_action(action: str) -> str:
    if type(action) is not str or action not in (
        _MANAGED_PRELAUNCH_SETTLEMENT_ACTIONS
    ):
        raise ValueError("settlement action must be retry or discard")
    return action


def _canonical_prelaunch_settlement(
    *,
    operation_id: str,
    item_id: str,
    carrier: dict[str, Any],
    manifest_hash: str,
    action: str,
) -> RecoveryReference:
    action = _validate_settlement_action(action)
    _validate_id(manifest_hash, "manifest_hash")
    reference = RecoveryReference(
        name=_MANAGED_PRELAUNCH_SETTLEMENT_REFERENCE_NAME,
        kind=_MANAGED_PRELAUNCH_SETTLEMENT_REFERENCE_KIND,
        data={
            "version": 1,
            "action": action,
            "carrier": _json_copy(carrier),
            "manifest_hash": manifest_hash,
            "owner": {
                "operation_id": operation_id,
                "item_id": item_id,
            },
        },
    )
    canonical = _parse_reference(_reference_payload(reference))
    if not _strict_managed_prelaunch_settlement(
        canonical,
        expected_operation_id=operation_id,
        expected_item_id=item_id,
    ):
        raise ValueError("pre-launch managed settlement is malformed")
    return canonical


@_locked_transaction
def _begin_prelaunch_managed_settlement(
    journal: QueueJournal,
    *,
    item_id: str,
    source_reference: RecoveryReference,
    carrier: dict[str, Any],
    manifest_hash: str,
    action: str,
) -> QueueJournal:
    """Commit explicit settlement authority before private state is retired."""
    previous = _reload_matching_journal(journal)
    target_index, target = _settlement_target(previous, item_id)
    canonical_source = _parse_reference(_reference_payload(source_reference))
    if target.recovery_references != (canonical_source,):
        raise QueueJournalBlocked(
            "managed recovery state changed before settlement"
        )
    carrier_reference = RecoveryReference(
        name="managed-import",
        kind=_MANAGED_COMPLETION_REFERENCE_KIND,
        data=_json_copy(carrier),
    )
    if (
        not _strict_managed_reference(
            carrier_reference,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        )
        or carrier_reference.data["version"] != 2
    ):
        raise ValueError("pre-launch managed carrier is malformed")
    if _strict_managed_reservation(
        canonical_source,
        expected_operation_id=previous.operation_id,
        expected_item_id=item_id,
    ):
        if any(
            canonical_source.data[field] != carrier_reference.data[field]
            for field in (
                "path",
                "parent_device",
                "parent_inode",
                "nonce",
                "owner",
            )
        ):
            raise QueueJournalBlocked(
                "managed carrier does not match its blocked reservation"
            )
    elif (
        not _strict_managed_reference(
            canonical_source,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        )
        or canonical_source.data != carrier_reference.data
    ):
        raise QueueJournalBlocked(
            "blocked state is not the inspected managed carrier"
        )
    settlement = _canonical_prelaunch_settlement(
        operation_id=previous.operation_id,
        item_id=item_id,
        carrier=carrier_reference.data,
        manifest_hash=manifest_hash,
        action=action,
    )
    updated = replace(
        target,
        recovery_references=(settlement,),
        block_reason="managed-prelaunch-settlement-pending",
    )
    items = list(previous.items)
    items[target_index] = _parse_item(
        _item_payload(updated),
        expected_operation_id=previous.operation_id,
    )
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def _finish_prelaunch_managed_settlement(
    journal: QueueJournal,
    *,
    item_id: str,
    settlement_reference: RecoveryReference,
    action: str,
) -> QueueJournal:
    """Acknowledge one already-retired pre-launch carrier exactly once."""
    action = _validate_settlement_action(action)
    previous = _reload_matching_journal(journal)
    target_index, target = _settlement_target(previous, item_id)
    canonical = _parse_reference(_reference_payload(settlement_reference))
    if (
        not _strict_managed_prelaunch_settlement(
            canonical,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        )
        or target.recovery_references != (canonical,)
        or canonical.data["action"] != action
    ):
        raise QueueJournalBlocked(
            "pre-launch settlement authority changed before completion"
        )
    items = list(previous.items)
    if action == "retry":
        items[target_index] = _parse_item(
            _item_payload(replace(
                target,
                phase=QueuePhase.PENDING,
                recovery_references=(),
                block_reason=None,
                completion_input=None,
                multi_artist_filing=False,
            )),
            expected_operation_id=previous.operation_id,
        )
    else:
        del items[target_index]
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def _commit_absent_managed_settlement(
    journal: QueueJournal,
    *,
    item_id: str,
    reservation: RecoveryReference,
    action: str,
) -> QueueJournal:
    """Settle an exact reservation after live inspection proved both names absent."""
    action = _validate_settlement_action(action)
    previous = _reload_matching_journal(journal)
    target_index, target = _settlement_target(previous, item_id)
    canonical = _parse_reference(_reference_payload(reservation))
    if (
        not _strict_managed_reservation(
            canonical,
            expected_operation_id=previous.operation_id,
            expected_item_id=item_id,
        )
        or target.recovery_references != (canonical,)
    ):
        raise QueueJournalBlocked(
            "managed reservation changed before settlement"
        )
    items = list(previous.items)
    if action == "retry":
        items[target_index] = _parse_item(
            _item_payload(replace(
                target,
                phase=QueuePhase.PENDING,
                recovery_references=(),
                block_reason=None,
                completion_input=None,
                multi_artist_filing=False,
            )),
            expected_operation_id=previous.operation_id,
        )
    else:
        del items[target_index]
    return _compare_and_commit(previous, replace(previous, items=tuple(items)))


@_locked_transaction
def clear_queue_journal(
    operation_id: str | None = None,
    *,
    explicit_discard: bool = False,
) -> bool:
    """Remove only empty state unless the caller explicitly confirms discard."""
    loaded = load_queue_journal(operation_id)
    if loaded.status is QueueLoadStatus.ABSENT:
        return False
    if loaded.status is QueueLoadStatus.BLOCKED:
        raise QueueJournalBlocked(
            loaded.reason or "saved queue state is blocked"
        )
    journal = loaded.journal
    if journal is None:
        raise QueueJournalBlocked("saved queue state could not be loaded")
    if journal.retirements:
        raise QueueJournalBlocked(
            "unresolved carrier retirement cannot be discarded"
        )
    if any(item.phase is not QueuePhase.PENDING for item in journal.items):
        raise QueueJournalBlocked(
            "active or recovery-bearing queue state cannot be discarded"
        )
    if journal.items and not explicit_discard:
        raise QueueJournalBlocked("non-empty queue state requires explicit discard")
    _unlink_and_sync(_journal_path(journal.operation_id))
    return True


def clear_pending_queue(*, explicit_discard: bool = False) -> bool:
    """Compatibility clear for the sole pending queue operation."""
    return clear_queue_journal(explicit_discard=explicit_discard)
