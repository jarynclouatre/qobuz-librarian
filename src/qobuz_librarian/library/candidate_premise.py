"""Tie saved review choices to the files that were actually scanned.

A review may sit for weeks before it changes an album. Its saved tree receipt
lets the app notice files that changed in the meantime. Older rows without a
valid receipt must be refreshed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from qobuz_librarian.library.backup import (
    _close_descriptors,
    _open_backup_source,
    _path_directory_generations,
    canonical_album_source_receipt,
    capture_album_source_receipt,
)

PREMISE_VERSION = 1
_KINDS = {
    "missing",
    "gap-fill",
    "upgrade",
    "downsample",
    "repair",
    "repair-redownload",
}


class CandidateStale(Exception):
    """The saved review no longer matches the local files."""


def _canonical_receipt(value, *, origin: str):
    """JSON-detach receipt tuples, then require backup.py's closed schema."""
    try:
        detached = json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return canonical_album_source_receipt(
        detached,
        expected_origin=origin,
    )


def canonical_premise(value) -> dict | None:
    """Validate a saved premise outside its review-row wrapper."""
    if (
        type(value) is not dict
        or set(value) != {"version", "kind", "path", "receipt"}
        or type(value.get("version")) is not int
        or value["version"] != PREMISE_VERSION
        or value.get("kind") not in _KINDS
        or type(value.get("path")) is not str
        or not os.path.isabs(value["path"])
        or os.path.abspath(value["path"]) != value["path"]
    ):
        return None
    receipt = _canonical_receipt(value.get("receipt"), origin=value["path"])
    if receipt is None:
        return None
    return {
        "version": PREMISE_VERSION,
        "kind": value["kind"],
        "path": value["path"],
        "receipt": receipt,
    }


def capture(kind: str, path) -> dict | None:
    """Capture the current music tree, or return ``None`` if it is uncertain."""
    if kind not in _KINDS:
        raise ValueError("candidate premise kind is invalid")
    try:
        requested = os.path.abspath(os.fspath(Path(path)))
    except (OSError, TypeError, ValueError):
        return None
    receipt = capture_album_source_receipt(Path(requested))
    if receipt is None:
        return None
    origin = receipt.get("origin") if isinstance(receipt, dict) else None
    if not isinstance(origin, str) or origin != requested:
        return None
    canonical = _canonical_receipt(receipt, origin=origin)
    if canonical is None:
        return None
    return {
        "version": PREMISE_VERSION,
        "kind": kind,
        "path": origin,
        "receipt": canonical,
    }


def _normalised_generation_evidence(receipt: dict):
    """Make namespace-local mount numbers comparable across restarts.

    Linux mount IDs identify boundaries within one mount namespace, but Docker
    assigns new numbers when it recreates a container.  Persisted review
    receipts therefore compare the *shape* of those boundaries while retaining
    every filesystem and directory-incarnation field.  A nested mount that
    appears, disappears, or moves still changes this evidence and fails closed.
    """
    mount_labels = {}

    def normalise(identity):
        mount_id = identity[6]
        if mount_id not in mount_labels:
            mount_labels[mount_id] = len(mount_labels)
        return [*identity[:6], mount_labels[mount_id]]

    path_generations = [
        normalise(identity)
        for identity in receipt["path_generations"]
    ]
    directory_generations = {
        relative: normalise(receipt["directory_generations"][relative])
        for relative in sorted(receipt["directory_generations"])
    }
    return path_generations, directory_generations


def durable_receipts_match(saved: dict, current: dict) -> bool:
    """Compare exact durable trees while allowing mount-ID renumbering."""
    stable_fields = ("version", "origin", "tree", "fidelity")
    try:
        return (
            all(saved[field] == current[field] for field in stable_fields)
            and _normalised_generation_evidence(saved)
            == _normalised_generation_evidence(current)
        )
    except (IndexError, KeyError, TypeError):
        return False


def _durable_premises_match(saved: dict, current: dict | None) -> bool:
    return (
        current is not None
        and saved["version"] == current.get("version")
        and saved["kind"] == current.get("kind")
        and saved["path"] == current.get("path")
        and durable_receipts_match(saved["receipt"], current["receipt"])
    )


def _durable_generations_match(saved, current) -> bool:
    """Compare path generations while preserving mount boundaries."""
    def normalise(generations):
        mount_labels = {}
        result = []
        for identity in generations:
            mount_id = identity[6]
            if mount_id not in mount_labels:
                mount_labels[mount_id] = len(mount_labels)
            result.append([*identity[:6], mount_labels[mount_id]])
        return result

    try:
        return normalise(saved) == normalise(current)
    except (IndexError, TypeError):
        return False


def expected_kind(candidate: dict) -> str:
    payload = candidate.get("payload") or {}
    kind = candidate.get("kind")
    if kind == "upgrade":
        return "upgrade"
    if kind == "downsample":
        return "downsample"
    if kind == "repair":
        return "repair"
    if kind == "redownload":
        return "repair-redownload"
    if payload.get("gap_fill"):
        return "gap-fill"
    return "missing"


def expected_path(candidate: dict, kind: str | None = None) -> str:
    payload = candidate.get("payload") or {}
    kind = kind or expected_kind(candidate)
    raw = (
        payload.get("_artist_dir_path")
        if kind == "missing"
        else payload.get("album_dir")
    )
    try:
        return os.path.abspath(os.fspath(Path(raw))) if raw else ""
    except (OSError, TypeError, ValueError):
        return ""


def canonical(candidate: dict) -> dict | None:
    """Read and validate a candidate's saved premise."""
    payload = candidate.get("payload") or {}
    value = payload.get("_premise")
    kind = expected_kind(candidate)
    path = expected_path(candidate, kind)
    premise = canonical_premise(value)
    if (
        premise is None
        or premise["kind"] != kind
        or premise["path"] != path
    ):
        return None
    return premise


def validate_premise(value) -> dict:
    """Check a queued or direct action against the current files."""
    premise = canonical_premise(value)
    if premise is None:
        raise CandidateStale(
            "This queued action is too old to verify against the current "
            "files. Refresh or rescan before changing music files."
        )
    current = capture(premise["kind"], premise["path"])
    if not _durable_premises_match(premise, current):
        raise CandidateStale(
            "The local files changed after this action was approved. Refresh "
            "or rescan; nothing was changed."
        )
    # Bind immediate file-changing work to the fresh namespace receipt.  The
    # persisted seal remains the durable reviewed evidence, while backup and
    # migration code continue to require raw, same-namespace equality.
    return current


def gap_fill_receipts(value) -> dict | None:
    """Derive exact per-file backup receipts from one canonical tree seal."""
    premise = canonical_premise(value)
    if premise is None:
        return None
    try:
        files = premise["receipt"]["tree"]["files"]
        receipts = {}
        for relative, snapshot in files.items():
            identity = snapshot["identity"]
            receipts[relative] = {
                "type": identity[0],
                "device": identity[1],
                "inode": identity[2],
                "size": snapshot["size"],
                "mtime_ns": snapshot["mtime_ns"],
                "ctime_ns": snapshot["changed_ns"],
                "sha256": snapshot["sha256"],
            }
        return receipts
    except (KeyError, TypeError, ValueError):
        return None


def validate(candidate: dict) -> dict:
    """Check a saved review against the current files before using it."""
    premise = canonical(candidate)
    if premise is None:
        raise CandidateStale(
            "This saved review predates local file receipts. Refresh it before "
            "changing music files."
        )
    current = capture(premise["kind"], premise["path"])
    if not _durable_premises_match(premise, current):
        raise CandidateStale(
            "The local files changed after this review was built. Refresh the "
            "review; nothing was changed."
        )
    return current


def validate_container(candidate: dict) -> dict:
    """Validate only the no-follow directory incarnation for a missing row.

    Several missing albums from one artist may be approved together. The first
    download legitimately changes that artist tree, but it must not authorize
    a replaced artist directory or a different mounted path for later picks.
    """
    premise = canonical(candidate)
    if premise is None:
        raise CandidateStale(
            "This saved review predates local file receipts. Refresh it before "
            "changing music files."
        )
    if premise["kind"] != "missing":
        return validate(candidate)
    opened = _open_backup_source(Path(premise["path"]))
    if opened is None:
        raise CandidateStale(
            "The local artist folder changed after this review was built. "
            "Refresh the review; nothing was changed."
        )
    public, _music_root, _parts, descriptors = opened
    try:
        current_generations = _path_directory_generations(descriptors)
    except OSError as exc:
        raise CandidateStale(
            "The local artist folder could not be verified. Refresh the "
            "review; nothing was changed."
        ) from exc
    finally:
        _close_descriptors(descriptors)
    if (
        str(public) != premise["path"]
        or not _durable_generations_match(
            premise["receipt"].get("path_generations"),
            current_generations,
        )
    ):
        raise CandidateStale(
            "The local artist folder changed after this review was built. "
            "Refresh the review; nothing was changed."
        )
    return premise


def validate_all(candidates) -> list[dict]:
    return [validate(candidate) for candidate in candidates]


def expected_album_receipt(candidate: dict) -> dict | None:
    premise = canonical(candidate)
    return None if premise is None else premise["receipt"]
