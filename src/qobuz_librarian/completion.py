"""Immutable evidence for proving one exact Qobuz import."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

StagedIdentity = tuple[int, int, int, int, int, int]
LibraryIdentity = tuple[int, int, int, int, int]


class CompletionScope(str, Enum):
    ALBUM = "album"
    REQUESTED = "requested"


class CompletionOriginKind(str, Enum):
    CLI = "cli"
    WEB_JOB = "web-job"


class SourceTransitionKind(str, Enum):
    DOWNSAMPLE = "downsample"
    LYRICS_TAG = "lyrics-tag"
    ORIGINAL_TAGS = "original-tags"
    REPAIR_RETAG = "repair-retag"
    BEETS_TAG_CLEAN = "beets-tag-clean"


class CompletionFailure(str, Enum):
    MALFORMED = "malformed"
    AMBIGUOUS_SLOT = "ambiguous-slot"
    SCOPE_MISMATCH = "scope-mismatch"
    DOWNLOAD_INCOMPLETE = "download-incomplete"
    DOWNLOAD_REMAINDER = "download-remainder"
    MANAGED_IMPORT_REQUIRED = "managed-import-required"
    OWNER_MISMATCH = "owner-mismatch"
    SOURCE_MISMATCH = "source-mismatch"
    DESTINATION_MISMATCH = "destination-mismatch"
    INVENTORY_MISMATCH = "inventory-mismatch"
    BASELINE_MISMATCH = "baseline-mismatch"
    QUALITY_MISMATCH = "quality-mismatch"
    RECOVERY_UNRESOLVED = "recovery-unresolved"


@dataclass(frozen=True, slots=True)
class RecoveryOwner:
    operation_id: str
    item_id: str


@dataclass(frozen=True, slots=True)
class QualityTarget:
    slot: str
    bits: int
    rate: int


@dataclass(frozen=True, slots=True)
class CompletionExpectation:
    album_id: str
    scope: CompletionScope
    catalogue_slots: tuple[str, ...]
    requested_slots: tuple[str, ...]
    quality_targets: tuple[QualityTarget, ...]
    baseline_slots: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DownloadCounts:
    failed: int = 0
    lossy: int = 0
    broken: int = 0
    unmatched: int = 0
    extra: int = 0


@dataclass(frozen=True, slots=True)
class StagedBinding:
    slot: str
    path: str
    identity: StagedIdentity


@dataclass(frozen=True, slots=True)
class DownloadCoverage:
    album_id: str
    catalogue_slots: tuple[str, ...]
    requested_slots: tuple[str, ...]
    bindings: tuple[StagedBinding, ...]
    counts: DownloadCounts


@dataclass(frozen=True, slots=True)
class CompletionOrigin:
    kind: CompletionOriginKind
    reference: str


@dataclass(frozen=True, slots=True)
class StagedReceipt:
    path: str
    identity: StagedIdentity


@dataclass(frozen=True, slots=True)
class SourceTransition:
    kind: SourceTransitionKind
    before: StagedReceipt
    after: StagedReceipt


@dataclass(frozen=True, slots=True)
class SourceLineage:
    slot: str
    origin: StagedReceipt
    transitions: tuple[SourceTransition, ...] = ()

    @property
    def current(self):
        return self.transitions[-1].after if self.transitions else self.origin


@dataclass(frozen=True, slots=True)
class CompletionInput:
    owner: RecoveryOwner
    origin: CompletionOrigin
    expectation: CompletionExpectation
    effective_tier: int
    lineages: tuple[SourceLineage, ...] = ()
    counts: DownloadCounts | None = None

    def to_record(self):
        return _completion_input_record(self)

    def download_coverage(self):
        if (
            not _valid_completion_input(self)
            or self.counts is None
            or tuple(lineage.slot for lineage in self.lineages)
            != self.expectation.requested_slots
        ):
            return None
        return DownloadCoverage(
            album_id=self.expectation.album_id,
            catalogue_slots=self.expectation.catalogue_slots,
            requested_slots=self.expectation.requested_slots,
            bindings=tuple(StagedBinding(
                slot=lineage.slot,
                path=lineage.current.path,
                identity=lineage.current.identity,
            ) for lineage in self.lineages),
            counts=self.counts,
        )


@dataclass(frozen=True, slots=True)
class ManagedMapping:
    slot: str
    source_path: str
    source_identity: StagedIdentity
    destination_path: str
    destination_identity: LibraryIdentity


@dataclass(frozen=True, slots=True)
class ManagedImportEvidence:
    owner: RecoveryOwner
    library_root: str
    library_root_identity: LibraryIdentity
    album_path: str
    album_identity: LibraryIdentity
    manifest_hash: str
    mappings: tuple[ManagedMapping, ...]


@dataclass(frozen=True, slots=True)
class LandingReceipt:
    slot: str
    path: str
    identity: LibraryIdentity
    sha256: str


@dataclass(frozen=True, slots=True)
class BaselineReceipt:
    slot: str
    path: str
    identity: LibraryIdentity
    sha256: str


@dataclass(frozen=True, slots=True)
class AlbumInventory:
    path: str
    identity: LibraryIdentity
    audio: tuple[LandingReceipt | BaselineReceipt, ...]


@dataclass(frozen=True, slots=True)
class TrackQuality:
    slot: str
    path: str
    identity: LibraryIdentity
    sha256: str
    served_bits: int
    served_rate: int


@dataclass(frozen=True, slots=True)
class CompletionEvidence:
    owner: RecoveryOwner
    expectation: CompletionExpectation
    library_root: str
    library_root_identity: LibraryIdentity
    album_path: str
    album_identity: LibraryIdentity
    managed_manifest_hash: str
    imports: tuple[ManagedMapping, ...]
    landings: tuple[LandingReceipt, ...]
    baseline: tuple[BaselineReceipt, ...]
    inventory: AlbumInventory | None
    quality: tuple[TrackQuality, ...]
    counts: DownloadCounts

    def to_record(self):
        return _completion_record(self)


@dataclass(frozen=True, slots=True)
class CompletionAssessment:
    evidence: CompletionEvidence | None = None
    failure: CompletionFailure | None = None

    def __post_init__(self):
        if (self.evidence is None) == (self.failure is None):
            raise ValueError(
                "completion assessment requires evidence or one failure")


_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_POSITION_SLOT = re.compile(r"position:[1-9][0-9]*:[1-9][0-9]*\Z")


def normalise_album_id(value) -> str | None:
    if type(value) is int:
        return str(value) if value > 0 else None
    if type(value) is not str:
        return None
    value = value.strip()
    if not value or len(value) > 256 or "\x00" in value:
        return None
    return value


def authoritative_slot(value) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        return False
    if "\x00" in value:
        return False
    if value.startswith("qobuz:"):
        identifier = value.removeprefix("qobuz:")
        return bool(identifier and identifier == identifier.strip())
    return _POSITION_SLOT.fullmatch(value) is not None


def authoritative_slots(values) -> bool:
    return (
        type(values) is tuple
        and bool(values)
        and len(set(values)) == len(values)
        and all(authoritative_slot(value) for value in values)
    )


def _valid_owner(owner) -> bool:
    return (
        type(owner) is RecoveryOwner
        and _HEX_64.fullmatch(owner.operation_id) is not None
        and _HEX_64.fullmatch(owner.item_id) is not None
    )


def _valid_identity(value, length) -> bool:
    return (
        type(value) is tuple
        and len(value) == length
        and all(type(part) is int and part >= 0 for part in value)
    )


def _valid_absolute_path(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and os.path.isabs(value)
        and os.path.abspath(value) == value
    )


def _valid_relative_path(value) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and bool(path.parts)
        and len(path.parts) <= 32
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _valid_hash(value) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _valid_counts(value) -> bool:
    return (
        type(value) is DownloadCounts
        and all(
            type(part) is int and part >= 0
            for part in (
                value.failed,
                value.lossy,
                value.broken,
                value.unmatched,
                value.extra,
            )
        )
    )


def _identity_record(identity):
    return {
        "device": identity[0],
        "inode": identity[1],
        "size": identity[2],
        "modified_ns": identity[3],
        "changed_ns": identity[4],
    }


def _identity_from_record(value):
    fields = ("device", "inode", "size", "modified_ns", "changed_ns")
    if type(value) is not dict or set(value) != set(fields):
        return None
    identity = tuple(value[field] for field in fields)
    return identity if _valid_identity(identity, 5) else None


def _staged_identity_from_record(value):
    if type(value) is not list:
        return None
    identity = tuple(value)
    return identity if _valid_identity(identity, 6) else None


def _plain_record_tree(value):
    pending = [(value, 0)]
    seen = set()
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > 100_000 or depth > 64:
            return False
        if type(current) is dict:
            marker = id(current)
            if marker in seen:
                return False
            seen.add(marker)
            if any(type(key) is not str for key in current):
                return False
            pending.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            marker = id(current)
            if marker in seen:
                return False
            seen.add(marker)
            pending.extend((item, depth + 1) for item in current)
        elif current is not None and type(current) not in (str, int):
            return False
    return True


def _valid_completion_origin(value) -> bool:
    return (
        type(value) is CompletionOrigin
        and type(value.kind) is CompletionOriginKind
        and isinstance(value.reference, str)
        and value.reference == value.reference.strip()
        and 0 < len(value.reference) <= 256
        and "\x00" not in value.reference
        and all(ord(character) >= 32 for character in value.reference)
    )


def _valid_input_expectation(value) -> bool:
    if (
        type(value) is not CompletionExpectation
        or value.scope is not CompletionScope.ALBUM
        or normalise_album_id(value.album_id) != value.album_id
        or not authoritative_slots(value.catalogue_slots)
        or value.requested_slots != value.catalogue_slots
        or value.baseline_slots != ()
        or type(value.quality_targets) is not tuple
        or tuple(target.slot for target in value.quality_targets)
        != value.catalogue_slots
    ):
        return False
    return all(
        type(target) is QualityTarget
        and type(target.bits) is int
        and target.bits > 0
        and type(target.rate) is int
        and target.rate > 0
        for target in value.quality_targets
    )


def _valid_staged_receipt(value) -> bool:
    return (
        type(value) is StagedReceipt
        and _valid_absolute_path(value.path)
        and _valid_identity(value.identity, 6)
    )


def _valid_source_lineage(value) -> bool:
    if (
        type(value) is not SourceLineage
        or not authoritative_slot(value.slot)
        or not _valid_staged_receipt(value.origin)
        or type(value.transitions) is not tuple
    ):
        return False
    current = value.origin
    for transition in value.transitions:
        if (
            type(transition) is not SourceTransition
            or type(transition.kind) is not SourceTransitionKind
            or transition.before != current
            or not _valid_staged_receipt(transition.after)
            or transition.after.path != value.origin.path
            or transition.after == transition.before
        ):
            return False
        current = transition.after
    return True


def _valid_completion_input(value) -> bool:
    if (
        type(value) is not CompletionInput
        or not _valid_owner(value.owner)
        or not _valid_completion_origin(value.origin)
        or not _valid_input_expectation(value.expectation)
        or type(value.effective_tier) is not int
        or value.effective_tier not in (2, 3, 4)
        or type(value.lineages) is not tuple
        or not all(_valid_source_lineage(item) for item in value.lineages)
        or value.counts is not None and not _valid_counts(value.counts)
    ):
        return False
    slots = tuple(item.slot for item in value.lineages)
    slot_set = set(slots)
    if (
        len(slot_set) != len(slots)
        or slots != tuple(
            slot for slot in value.expectation.catalogue_slots
            if slot in slot_set
        )
        or value.counts is not None
        and slots != value.expectation.requested_slots
    ):
        return False
    paths = {item.origin.path for item in value.lineages}
    origin_nodes = {item.origin.identity[:2] for item in value.lineages}
    current_nodes = {item.current.identity[:2] for item in value.lineages}
    return (
        len(paths) == len(value.lineages)
        and len(origin_nodes) == len(value.lineages)
        and len(current_nodes) == len(value.lineages)
    )


def _staged_receipt_record(value):
    return {"path": value.path, "identity": list(value.identity)}


def _source_lineage_record(value):
    return {
        "slot": value.slot,
        "origin": _staged_receipt_record(value.origin),
        "transitions": [{
            "kind": transition.kind.value,
            "before": _staged_receipt_record(transition.before),
            "after": _staged_receipt_record(transition.after),
        } for transition in value.transitions],
    }


def _completion_input_record(value):
    if not _valid_completion_input(value):
        raise ValueError("completion input is invalid")
    expectation = value.expectation
    return {
        "version": 1,
        "owner": {
            "operation_id": value.owner.operation_id,
            "item_id": value.owner.item_id,
        },
        "origin": {
            "kind": value.origin.kind.value,
            "reference": value.origin.reference,
        },
        "expectation": {
            "album_id": expectation.album_id,
            "scope": expectation.scope.value,
            "catalogue_slots": list(expectation.catalogue_slots),
            "requested_slots": list(expectation.requested_slots),
            "baseline_slots": list(expectation.baseline_slots),
            "quality_targets": [{
                "slot": target.slot,
                "bits": target.bits,
                "rate": target.rate,
            } for target in expectation.quality_targets],
        },
        "effective_tier": value.effective_tier,
        "download": {
            "counts": (
                None if value.counts is None else {
                    "failed": value.counts.failed,
                    "lossy": value.counts.lossy,
                    "broken": value.counts.broken,
                    "unmatched": value.counts.unmatched,
                    "extra": value.counts.extra,
                }
            ),
            "lineages": [
                _source_lineage_record(item) for item in value.lineages
            ],
        },
    }


def parse_completion_input_record(record, *, expected_owner=None):
    """Decode one canonical frozen plan and append-only source lineage."""
    if (
        type(record) is not dict
        or not _plain_record_tree(record)
        or set(record) != {
            "version", "owner", "origin", "expectation",
            "effective_tier", "download",
        }
        or type(record.get("version")) is not int
        or record["version"] != 1
        or expected_owner is not None
        and type(expected_owner) is not RecoveryOwner
    ):
        return None
    try:
        owner_value = record["owner"]
        origin_value = record["origin"]
        expectation_value = record["expectation"]
        download_value = record["download"]
        if (
            set(owner_value) != {"operation_id", "item_id"}
            or set(origin_value) != {"kind", "reference"}
            or set(expectation_value) != {
                "album_id", "scope", "catalogue_slots", "requested_slots",
                "baseline_slots", "quality_targets",
            }
            or set(download_value) != {"counts", "lineages"}
        ):
            return None
        owner = RecoveryOwner(
            owner_value["operation_id"], owner_value["item_id"])
        if expected_owner is not None and owner != expected_owner:
            return None
        origin = CompletionOrigin(
            CompletionOriginKind(origin_value["kind"]),
            origin_value["reference"],
        )
        expectation = CompletionExpectation(
            album_id=expectation_value["album_id"],
            scope=CompletionScope(expectation_value["scope"]),
            catalogue_slots=tuple(expectation_value["catalogue_slots"]),
            requested_slots=tuple(expectation_value["requested_slots"]),
            baseline_slots=tuple(expectation_value["baseline_slots"]),
            quality_targets=tuple(QualityTarget(
                target["slot"], target["bits"], target["rate"])
                for target in expectation_value["quality_targets"]
                if set(target) == {"slot", "bits", "rate"}
            ),
        )

        def receipt(value):
            if set(value) != {"path", "identity"}:
                raise ValueError("invalid staged receipt")
            return StagedReceipt(
                value["path"], tuple(value["identity"]))

        lineages = []
        for value in download_value["lineages"]:
            if set(value) != {"slot", "origin", "transitions"}:
                return None
            transitions = []
            for transition in value["transitions"]:
                if set(transition) != {"kind", "before", "after"}:
                    return None
                transitions.append(SourceTransition(
                    SourceTransitionKind(transition["kind"]),
                    receipt(transition["before"]),
                    receipt(transition["after"]),
                ))
            lineages.append(SourceLineage(
                value["slot"], receipt(value["origin"]),
                tuple(transitions),
            ))
        counts_value = download_value["counts"]
        counts = None
        if counts_value is not None:
            if set(counts_value) != {
                "failed", "lossy", "broken", "unmatched", "extra",
            }:
                return None
            counts = DownloadCounts(
                failed=counts_value["failed"],
                lossy=counts_value["lossy"],
                broken=counts_value["broken"],
                unmatched=counts_value["unmatched"],
                extra=counts_value["extra"],
            )
        value = CompletionInput(
            owner=owner,
            origin=origin,
            expectation=expectation,
            effective_tier=record["effective_tier"],
            lineages=tuple(lineages),
            counts=counts,
        )
    except (KeyError, TypeError, ValueError):
        return None
    return value if _valid_completion_input(
        value) and value.to_record() == record else None


def completion_input_extends(previous, current) -> bool:
    """Accept only an append-only advance of downloaded source authority."""
    if (
        not _valid_completion_input(previous)
        or not _valid_completion_input(current)
        or (
            previous.owner,
            previous.origin,
            previous.expectation,
            previous.effective_tier,
        ) != (
            current.owner,
            current.origin,
            current.expectation,
            current.effective_tier,
        )
        or previous.counts is not None
        and previous.counts != current.counts
    ):
        return False
    current_by_slot = {item.slot: item for item in current.lineages}
    for lineage in previous.lineages:
        advanced = current_by_slot.get(lineage.slot)
        if (
            advanced is None
            or advanced.origin != lineage.origin
            or advanced.transitions[:len(lineage.transitions)]
            != lineage.transitions
        ):
            return False
    return True


def completion_input_ready(value) -> bool:
    """Return whether every frozen slot has zero-remainder source lineage."""
    if not _valid_completion_input(value):
        return False
    coverage = value.download_coverage()
    return coverage is not None and not any((
        coverage.counts.failed,
        coverage.counts.lossy,
        coverage.counts.broken,
        coverage.counts.unmatched,
        coverage.counts.extra,
    ))


def _mapping_record(mapping, landing):
    return {
        "slot": mapping.slot,
        "source": {
            "path": mapping.source_path,
            "identity": list(mapping.source_identity),
        },
        "destination": {
            "path": mapping.destination_path,
            "identity": _identity_record(mapping.destination_identity),
            "sha256": landing.sha256,
        },
    }


def _baseline_record(receipt):
    return {
        "slot": receipt.slot,
        "destination": {
            "path": receipt.path,
            "identity": _identity_record(receipt.identity),
            "sha256": receipt.sha256,
        },
    }


def _quality_record(value, target):
    return {
        "slot": value.slot,
        "destination": {
            "path": value.path,
            "identity": _identity_record(value.identity),
            "sha256": value.sha256,
        },
        "served": {"bits": value.served_bits, "rate": value.served_rate},
        "target": {"bits": target.bits, "rate": target.rate},
    }


def _inventory_record(value):
    return {
        "path": value.path,
        "identity": _identity_record(value.identity),
        "audio": [
            {
                "slot": receipt.slot,
                "path": receipt.path,
                "identity": _identity_record(receipt.identity),
                "sha256": receipt.sha256,
            }
            for receipt in value.audio
        ],
    }


def _completion_record(evidence):
    landings = {receipt.slot: receipt for receipt in evidence.landings}
    targets = {
        target.slot: target for target in evidence.expectation.quality_targets
    }
    return {
        "version": 1,
        "owner": {
            "operation_id": evidence.owner.operation_id,
            "item_id": evidence.owner.item_id,
        },
        "scope": evidence.expectation.scope.value,
        "album_id": evidence.expectation.album_id,
        "catalogue_slots": list(evidence.expectation.catalogue_slots),
        "requested_slots": list(evidence.expectation.requested_slots),
        "baseline_slots": list(evidence.expectation.baseline_slots),
        "library": {
            "root": evidence.library_root,
            "identity": _identity_record(evidence.library_root_identity),
            "album": {
                "path": evidence.album_path,
                "identity": _identity_record(evidence.album_identity),
            },
        },
        "managed_manifest_hash": evidence.managed_manifest_hash,
        "imports": [
            _mapping_record(mapping, landings[mapping.slot])
            for mapping in evidence.imports
        ],
        "baseline": [
            _baseline_record(receipt) for receipt in evidence.baseline
        ],
        "inventory": (
            _inventory_record(evidence.inventory)
            if evidence.inventory is not None
            else None
        ),
        "quality": [
            _quality_record(value, targets[value.slot])
            for value in evidence.quality
        ],
        "counts": {
            "failed": evidence.counts.failed,
            "lossy": evidence.counts.lossy,
            "broken": evidence.counts.broken,
            "unmatched": evidence.counts.unmatched,
            "extra": evidence.counts.extra,
        },
    }


def parse_completion_record(
    record,
    *,
    expected_owner: RecoveryOwner,
    expected_expectation: CompletionExpectation,
    expected_download: DownloadCoverage,
) -> CompletionEvidence | None:
    """Decode one canonical record; live authority must be revalidated."""
    if (
        type(record) is not dict
        or not _plain_record_tree(record)
        or type(record.get("version")) is not int
        or record.get("version") != 1
        or type(expected_owner) is not RecoveryOwner
        or type(expected_expectation) is not CompletionExpectation
        or type(expected_download) is not DownloadCoverage
    ):
        return None
    try:
        owner_value = record["owner"]
        owner = RecoveryOwner(
            operation_id=owner_value["operation_id"],
            item_id=owner_value["item_id"],
        )
        expectation = CompletionExpectation(
            album_id=record["album_id"],
            scope=CompletionScope(record["scope"]),
            catalogue_slots=tuple(record["catalogue_slots"]),
            requested_slots=tuple(record["requested_slots"]),
            quality_targets=tuple(
                QualityTarget(
                    slot=value["slot"],
                    bits=value["target"]["bits"],
                    rate=value["target"]["rate"],
                )
                for value in record["quality"]
            ),
            baseline_slots=tuple(record["baseline_slots"]),
        )
        if owner != expected_owner or expectation != expected_expectation:
            return None

        library = record["library"]
        album = library["album"]
        root_identity = _identity_from_record(library["identity"])
        album_identity = _identity_from_record(album["identity"])
        if root_identity is None or album_identity is None:
            return None

        mappings = []
        landings = []
        bindings = []
        for value in record["imports"]:
            source = value["source"]
            destination = value["destination"]
            source_identity = _staged_identity_from_record(source["identity"])
            destination_identity = _identity_from_record(
                destination["identity"])
            if source_identity is None or destination_identity is None:
                return None
            mappings.append(ManagedMapping(
                slot=value["slot"],
                source_path=source["path"],
                source_identity=source_identity,
                destination_path=destination["path"],
                destination_identity=destination_identity,
            ))
            landings.append(LandingReceipt(
                slot=value["slot"],
                path=destination["path"],
                identity=destination_identity,
                sha256=destination["sha256"],
            ))
            bindings.append(StagedBinding(
                slot=value["slot"],
                path=source["path"],
                identity=source_identity,
            ))

        baseline = []
        for value in record["baseline"]:
            destination = value["destination"]
            identity = _identity_from_record(destination["identity"])
            if identity is None:
                return None
            baseline.append(BaselineReceipt(
                slot=value["slot"],
                path=destination["path"],
                identity=identity,
                sha256=destination["sha256"],
            ))

        imported_slots = {receipt.slot for receipt in landings}
        inventory_value = record["inventory"]
        inventory = None
        if inventory_value is not None:
            identity = _identity_from_record(inventory_value["identity"])
            if identity is None:
                return None
            inventory_audio = []
            for value in inventory_value["audio"]:
                receipt_identity = _identity_from_record(value["identity"])
                if receipt_identity is None:
                    return None
                receipt_type = (
                    LandingReceipt
                    if value["slot"] in imported_slots
                    else BaselineReceipt
                )
                inventory_audio.append(receipt_type(
                    slot=value["slot"],
                    path=value["path"],
                    identity=receipt_identity,
                    sha256=value["sha256"],
                ))
            inventory = AlbumInventory(
                path=inventory_value["path"],
                identity=identity,
                audio=tuple(inventory_audio),
            )

        quality = []
        for value in record["quality"]:
            destination = value["destination"]
            identity = _identity_from_record(destination["identity"])
            if identity is None:
                return None
            quality.append(TrackQuality(
                slot=value["slot"],
                path=destination["path"],
                identity=identity,
                sha256=destination["sha256"],
                served_bits=value["served"]["bits"],
                served_rate=value["served"]["rate"],
            ))

        counts_value = record["counts"]
        counts = DownloadCounts(
            failed=counts_value["failed"],
            lossy=counts_value["lossy"],
            broken=counts_value["broken"],
            unmatched=counts_value["unmatched"],
            extra=counts_value["extra"],
        )
        download = DownloadCoverage(
            album_id=expectation.album_id,
            catalogue_slots=expectation.catalogue_slots,
            requested_slots=expectation.requested_slots,
            bindings=tuple(bindings),
            counts=counts,
        )
        expected_bindings = {
            binding.slot: binding for binding in expected_download.bindings
        }
        actual_bindings = {
            binding.slot: binding for binding in download.bindings
        }
        if (
            (
                download.album_id,
                download.catalogue_slots,
                download.requested_slots,
                download.counts,
            ) != (
                expected_download.album_id,
                expected_download.catalogue_slots,
                expected_download.requested_slots,
                expected_download.counts,
            )
            or len(actual_bindings) != len(download.bindings)
            or len(expected_bindings) != len(expected_download.bindings)
            or actual_bindings != expected_bindings
        ):
            return None
        managed = ManagedImportEvidence(
            owner=owner,
            library_root=library["root"],
            library_root_identity=root_identity,
            album_path=album["path"],
            album_identity=album_identity,
            manifest_hash=record["managed_manifest_hash"],
            mappings=tuple(mappings),
        )
    except (KeyError, TypeError, ValueError):
        return None

    assessment = assess_completion(
        owner=owner,
        expectation=expectation,
        download=download,
        managed=managed,
        landings=tuple(landings),
        quality=tuple(quality),
        baseline=tuple(baseline),
        inventory=inventory,
    )
    evidence = assessment.evidence
    if evidence is None or evidence.to_record() != record:
        return None
    return evidence


def completion_acknowledgement_hash(
    completion_input: CompletionInput,
    completion_evidence: CompletionEvidence,
) -> str:
    """Bind an external acknowledgement to the complete canonical proof."""
    if (
        type(completion_input) is not CompletionInput
        or not completion_input_ready(completion_input)
        or type(completion_evidence) is not CompletionEvidence
    ):
        raise ValueError("completion acknowledgement requires exact evidence")
    download = completion_input.download_coverage()
    evidence_record = completion_evidence.to_record()
    if (
        download is None
        or parse_completion_record(
            evidence_record,
            expected_owner=completion_input.owner,
            expected_expectation=completion_input.expectation,
            expected_download=download,
        )
        != completion_evidence
    ):
        raise ValueError("completion acknowledgement evidence does not match")
    return hashlib.sha256(json.dumps(
        {
            "completion_input": completion_input.to_record(),
            "completion_evidence": evidence_record,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _failure(reason):
    return CompletionAssessment(failure=reason)


def assess_completion(
    *,
    owner: RecoveryOwner,
    expectation: CompletionExpectation,
    download: DownloadCoverage | None,
    managed: ManagedImportEvidence | None,
    landings: tuple[LandingReceipt, ...],
    quality: tuple[TrackQuality, ...],
    baseline: tuple[BaselineReceipt, ...] = (),
    inventory: AlbumInventory | None = None,
    recovery_references=(),
) -> CompletionAssessment:
    """Combine already live-validated inputs into one exact evidence record."""
    try:
        recovery_references = tuple(recovery_references)
    except (TypeError, ValueError):
        return _failure(CompletionFailure.MALFORMED)
    if recovery_references:
        return _failure(CompletionFailure.RECOVERY_UNRESOLVED)
    if (
        not _valid_owner(owner)
        or type(expectation) is not CompletionExpectation
        or type(expectation.scope) is not CompletionScope
        or normalise_album_id(expectation.album_id) != expectation.album_id
        or type(expectation.quality_targets) is not tuple
        or not all(
            type(target) is QualityTarget
            for target in expectation.quality_targets
        )
    ):
        return _failure(CompletionFailure.MALFORMED)
    if (
        not authoritative_slots(expectation.catalogue_slots)
        or not authoritative_slots(expectation.requested_slots)
        or type(expectation.baseline_slots) is not tuple
        or (
            expectation.baseline_slots
            and not authoritative_slots(expectation.baseline_slots)
        )
        or len({
            target.slot for target in expectation.quality_targets
        }) != len(expectation.quality_targets)
        or not all(
            authoritative_slot(target.slot)
            for target in expectation.quality_targets
        )
    ):
        return _failure(CompletionFailure.AMBIGUOUS_SLOT)
    if any(
        type(value) is not int or value <= 0
        for target in expectation.quality_targets
        for value in (target.bits, target.rate)
    ):
        return _failure(CompletionFailure.MALFORMED)

    catalogue = set(expectation.catalogue_slots)
    requested = set(expectation.requested_slots)
    baseline_slots = set(expectation.baseline_slots)
    if (
        not requested <= catalogue
        or not baseline_slots <= catalogue
        or requested & baseline_slots
        or expectation.requested_slots != tuple(
            slot for slot in expectation.catalogue_slots if slot in requested)
        or expectation.baseline_slots != tuple(
            slot
            for slot in expectation.catalogue_slots
            if slot in baseline_slots
        )
    ):
        return _failure(CompletionFailure.SCOPE_MISMATCH)
    if (
        expectation.scope is CompletionScope.ALBUM
        and requested | baseline_slots != catalogue
    ):
        return _failure(CompletionFailure.SCOPE_MISMATCH)
    quality_slots = (
        catalogue
        if expectation.scope is CompletionScope.ALBUM
        else requested
    )
    targets_by_slot = {
        target.slot: target for target in expectation.quality_targets
    }
    if (
        set(targets_by_slot) != quality_slots
        or tuple(target.slot for target in expectation.quality_targets)
        != tuple(
            slot
            for slot in expectation.catalogue_slots
            if slot in quality_slots
        )
    ):
        return _failure(CompletionFailure.QUALITY_MISMATCH)
    if (
        type(download) is not DownloadCoverage
        or download.album_id != expectation.album_id
        or download.catalogue_slots != expectation.catalogue_slots
        or download.requested_slots != expectation.requested_slots
        or type(download.bindings) is not tuple
        or not _valid_counts(download.counts)
    ):
        return _failure(CompletionFailure.DOWNLOAD_INCOMPLETE)
    if any((
        download.counts.failed,
        download.counts.lossy,
        download.counts.broken,
        download.counts.unmatched,
        download.counts.extra,
    )):
        return _failure(CompletionFailure.DOWNLOAD_REMAINDER)

    staged_by_slot = {}
    staged_paths = set()
    staged_nodes = set()
    for binding in download.bindings:
        if (
            type(binding) is not StagedBinding
            or not authoritative_slot(binding.slot)
            or not _valid_absolute_path(binding.path)
            or not _valid_identity(binding.identity, 6)
            or binding.slot in staged_by_slot
            or binding.path in staged_paths
            or binding.identity[:2] in staged_nodes
        ):
            return _failure(CompletionFailure.DOWNLOAD_INCOMPLETE)
        staged_by_slot[binding.slot] = binding
        staged_paths.add(binding.path)
        staged_nodes.add(binding.identity[:2])
    if set(staged_by_slot) != requested:
        return _failure(CompletionFailure.DOWNLOAD_INCOMPLETE)

    if type(managed) is not ManagedImportEvidence:
        return _failure(CompletionFailure.MANAGED_IMPORT_REQUIRED)
    if managed.owner != owner:
        return _failure(CompletionFailure.OWNER_MISMATCH)
    if (
        not _valid_absolute_path(managed.library_root)
        or not _valid_identity(managed.library_root_identity, 5)
        or not _valid_relative_path(managed.album_path)
        or not _valid_identity(managed.album_identity, 5)
        or not _valid_hash(managed.manifest_hash)
        or type(managed.mappings) is not tuple
    ):
        return _failure(CompletionFailure.MALFORMED)

    mappings_by_slot = {}
    source_paths = set()
    source_nodes = set()
    destination_paths = set()
    destination_nodes = set()
    for mapping in managed.mappings:
        if (
            type(mapping) is not ManagedMapping
            or not authoritative_slot(mapping.slot)
            or not _valid_absolute_path(mapping.source_path)
            or not _valid_identity(mapping.source_identity, 6)
            or not _valid_relative_path(mapping.destination_path)
            or not _valid_identity(mapping.destination_identity, 5)
            or mapping.slot in mappings_by_slot
            or mapping.source_path in source_paths
            or mapping.source_identity[:2] in source_nodes
            or mapping.destination_path in destination_paths
            or mapping.destination_identity[:2] in destination_nodes
        ):
            return _failure(CompletionFailure.SOURCE_MISMATCH)
        mappings_by_slot[mapping.slot] = mapping
        source_paths.add(mapping.source_path)
        source_nodes.add(mapping.source_identity[:2])
        destination_paths.add(mapping.destination_path)
        destination_nodes.add(mapping.destination_identity[:2])
    if set(mappings_by_slot) != requested:
        return _failure(CompletionFailure.SOURCE_MISMATCH)
    album_parts = PurePosixPath(managed.album_path).parts
    if any(
        len(parts := PurePosixPath(mapping.destination_path).parts)
        <= len(album_parts)
        or parts[:len(album_parts)] != album_parts
        for mapping in mappings_by_slot.values()
    ):
        return _failure(CompletionFailure.DESTINATION_MISMATCH)
    for slot, binding in staged_by_slot.items():
        mapping = mappings_by_slot[slot]
        if (
            mapping.source_path != binding.path
            or mapping.source_identity != binding.identity
        ):
            return _failure(CompletionFailure.SOURCE_MISMATCH)

    if type(landings) is not tuple:
        return _failure(CompletionFailure.DESTINATION_MISMATCH)
    landing_by_slot = {}
    landing_paths = set()
    landing_nodes = set()
    for receipt in landings:
        if (
            type(receipt) is not LandingReceipt
            or not authoritative_slot(receipt.slot)
            or not _valid_relative_path(receipt.path)
            or not _valid_identity(receipt.identity, 5)
            or not _valid_hash(receipt.sha256)
            or receipt.slot in landing_by_slot
            or receipt.path in landing_paths
            or receipt.identity[:2] in landing_nodes
        ):
            return _failure(CompletionFailure.DESTINATION_MISMATCH)
        landing_by_slot[receipt.slot] = receipt
        landing_paths.add(receipt.path)
        landing_nodes.add(receipt.identity[:2])
    if set(landing_by_slot) != requested:
        return _failure(CompletionFailure.DESTINATION_MISMATCH)
    for slot, mapping in mappings_by_slot.items():
        receipt = landing_by_slot[slot]
        if (
            receipt.path != mapping.destination_path
            or receipt.identity != mapping.destination_identity
        ):
            return _failure(CompletionFailure.DESTINATION_MISMATCH)

    if type(baseline) is not tuple:
        return _failure(CompletionFailure.BASELINE_MISMATCH)
    baseline_by_slot = {}
    baseline_paths = set()
    baseline_nodes = set()
    for receipt in baseline:
        if (
            type(receipt) is not BaselineReceipt
            or not authoritative_slot(receipt.slot)
            or not _valid_relative_path(receipt.path)
            or not _valid_identity(receipt.identity, 5)
            or not _valid_hash(receipt.sha256)
            or receipt.slot in baseline_by_slot
            or receipt.path in baseline_paths
            or receipt.identity[:2] in baseline_nodes
            or receipt.path in landing_paths
            or receipt.identity[:2] in landing_nodes
        ):
            return _failure(CompletionFailure.BASELINE_MISMATCH)
        baseline_by_slot[receipt.slot] = receipt
        baseline_paths.add(receipt.path)
        baseline_nodes.add(receipt.identity[:2])
    if set(baseline_by_slot) != baseline_slots:
        return _failure(CompletionFailure.BASELINE_MISMATCH)

    inventory_required = (
        expectation.scope is CompletionScope.ALBUM or bool(baseline_slots)
    )
    canonical_inventory = None
    if inventory is None:
        if inventory_required:
            return _failure(CompletionFailure.INVENTORY_MISMATCH)
    else:
        if (
            type(inventory) is not AlbumInventory
            or not _valid_relative_path(inventory.path)
            or not _valid_identity(inventory.identity, 5)
            or type(inventory.audio) is not tuple
            or inventory.path != managed.album_path
            or inventory.identity != managed.album_identity
        ):
            return _failure(CompletionFailure.INVENTORY_MISMATCH)
        inventory_by_slot = {}
        inventory_paths = set()
        inventory_nodes = set()
        album_parts = PurePosixPath(inventory.path).parts
        for receipt in inventory.audio:
            if (
                type(receipt) not in (LandingReceipt, BaselineReceipt)
                or not authoritative_slot(receipt.slot)
                or not _valid_relative_path(receipt.path)
                or not _valid_identity(receipt.identity, 5)
                or not _valid_hash(receipt.sha256)
                or receipt.slot in inventory_by_slot
                or receipt.path in inventory_paths
                or receipt.identity[:2] in inventory_nodes
            ):
                return _failure(CompletionFailure.INVENTORY_MISMATCH)
            receipt_parts = PurePosixPath(receipt.path).parts
            if (
                len(receipt_parts) <= len(album_parts)
                or receipt_parts[:len(album_parts)] != album_parts
            ):
                return _failure(CompletionFailure.INVENTORY_MISMATCH)
            inventory_by_slot[receipt.slot] = receipt
            inventory_paths.add(receipt.path)
            inventory_nodes.add(receipt.identity[:2])
        expected_inventory = {**baseline_by_slot, **landing_by_slot}
        if set(inventory_by_slot) != requested | baseline_slots:
            return _failure(CompletionFailure.INVENTORY_MISMATCH)
        for slot, receipt in inventory_by_slot.items():
            expected = expected_inventory[slot]
            if (
                type(receipt) is not type(expected)
                or receipt.path != expected.path
                or receipt.identity != expected.identity
                or receipt.sha256 != expected.sha256
            ):
                return _failure(CompletionFailure.INVENTORY_MISMATCH)
        inventory_order = [
            slot for slot in expectation.catalogue_slots
            if slot in requested | baseline_slots
        ]
        canonical_inventory = AlbumInventory(
            path=inventory.path,
            identity=inventory.identity,
            audio=tuple(inventory_by_slot[slot] for slot in inventory_order),
        )

    if type(quality) is not tuple:
        return _failure(CompletionFailure.QUALITY_MISMATCH)
    quality_by_slot = {}
    for value in quality:
        if (
            type(value) is not TrackQuality
            or not authoritative_slot(value.slot)
            or value.slot in quality_by_slot
            or not _valid_relative_path(value.path)
            or not _valid_identity(value.identity, 5)
            or not _valid_hash(value.sha256)
            or any(
                type(part) is not int or part <= 0
                for part in (
                    value.served_bits,
                    value.served_rate,
                )
            )
        ):
            return _failure(CompletionFailure.QUALITY_MISMATCH)
        quality_by_slot[value.slot] = value
    if set(quality_by_slot) != quality_slots:
        return _failure(CompletionFailure.QUALITY_MISMATCH)
    destinations_by_slot = {**baseline_by_slot, **landing_by_slot}
    for slot, value in quality_by_slot.items():
        receipt = destinations_by_slot[slot]
        if (
            value.path != receipt.path
            or value.identity != receipt.identity
            or value.sha256 != receipt.sha256
            or value.served_bits < targets_by_slot[slot].bits
            or value.served_rate < targets_by_slot[slot].rate
        ):
            return _failure(CompletionFailure.QUALITY_MISMATCH)

    requested_order = [
        slot for slot in expectation.catalogue_slots if slot in requested
    ]
    baseline_order = [
        slot for slot in expectation.catalogue_slots if slot in baseline_slots
    ]
    quality_order = [
        slot for slot in expectation.catalogue_slots if slot in quality_slots
    ]
    evidence = CompletionEvidence(
        owner=owner,
        expectation=expectation,
        library_root=managed.library_root,
        library_root_identity=managed.library_root_identity,
        album_path=managed.album_path,
        album_identity=managed.album_identity,
        managed_manifest_hash=managed.manifest_hash,
        imports=tuple(mappings_by_slot[slot] for slot in requested_order),
        landings=tuple(landing_by_slot[slot] for slot in requested_order),
        baseline=tuple(baseline_by_slot[slot] for slot in baseline_order),
        inventory=canonical_inventory,
        quality=tuple(quality_by_slot[slot] for slot in quality_order),
        counts=download.counts,
    )
    return CompletionAssessment(evidence=evidence)
