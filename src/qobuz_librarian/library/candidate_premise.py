"""Tie saved review choices to the files that were actually scanned.

A review may sit for weeks before it changes an album. Its saved tree receipt
lets the app notice files that changed in the meantime. Older rows without a
valid receipt, or with one from an earlier version, must be refreshed.

The receipt records names, sizes, times, inodes and directory incarnations
and reads no file, so checking a whole artist folder costs one walk of it.
The backup code hashes what it moves at the moment it moves it.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.library.backup import (
    _SIDECARS,
    _album_tree_identity,
    _close_descriptors,
    _open_backup_source,
    _path_directory_generations,
    canonical_album_source_receipt,
    canonical_album_tree_identity,
    capture_album_tree_identity,
    capture_music_root_identity,
    first_symlink,
)

PREMISE_VERSION = 2
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

    def __init__(self, message, *, cause="changed"):
        super().__init__(message)
        self.cause = cause


class StaleCandidateIds(set):
    def __init__(self):
        super().__init__()
        self.counts = {"changed": 0, "unreadable": 0, "older": 0, "link": 0}


def _canonical_receipt(value, *, origin: str, version: int = PREMISE_VERSION):
    """JSON-detach receipt tuples, then require backup.py's closed schema.

    Version 1 premises held the exact hashed receipt. They still load, so a
    saved queue or recovery record can be read, but validation refuses them.
    """
    try:
        detached = json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    canonical = (
        canonical_album_source_receipt if version == 1
        else canonical_album_tree_identity
    )
    return canonical(detached, expected_origin=origin)


def canonical_premise(value) -> dict | None:
    """Validate a saved premise outside its review-row wrapper."""
    if (
        type(value) is not dict
        or set(value) != {"version", "kind", "path", "receipt"}
        or type(value.get("version")) is not int
        or value["version"] not in (1, PREMISE_VERSION)
        or value.get("kind") not in _KINDS
        or type(value.get("path")) is not str
        or not os.path.isabs(value["path"])
        or os.path.abspath(value["path"]) != value["path"]
    ):
        return None
    receipt = _canonical_receipt(
        value.get("receipt"), origin=value["path"], version=value["version"])
    if receipt is None:
        return None
    return {
        "version": value["version"],
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
    receipt = capture_album_tree_identity(Path(requested))
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
    """Compare durable folder identities while allowing mount-ID renumbering."""
    stable_fields = ("version", "origin", "tree")
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


def same_files(first, second) -> bool:
    """Whether two saved seals hold the same files, however the mounts were
    numbered when each was taken."""
    first = canonical_premise(first)
    return first is not None and _durable_premises_match(
        first, canonical_premise(second))


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


ABSENT_CONTAINER_KEY = "_absent_container"


def _plain_folder_name(value) -> str:
    """A single folder name, or "" for anything that could reach elsewhere."""
    if type(value) is not str or not value or value in (".", ".."):
        return ""
    return value if value == Path(value).name else ""


def capture_absent(artist_name) -> dict | None:
    """Seal what a missing-album row can prove with no artist folder at all.

    A collection restore names artists the library may have no folder for, so
    there is no tree to receipt. Nothing local can be lost by fetching into a
    folder that does not exist, and the album's own reappearance is caught at
    download time, so what this holds is the one fact that still matters: the
    music folder is the same mounted directory throughout.
    """
    name = _plain_folder_name(artist_name)
    identity = capture_music_root_identity() if name else None
    return None if identity is None else {"artist": name,
                                          "music_root": identity}


def _canonical_absent(candidate: dict):
    """Read a saved absent-folder seal, or None when the row isn't one."""
    value = (candidate.get("payload") or {}).get(ABSENT_CONTAINER_KEY)
    if type(value) is not dict or set(value) != {"artist", "music_root"}:
        return None
    if expected_kind(candidate) != "missing":
        return None
    name = _plain_folder_name(value["artist"])
    identity = value["music_root"]
    if (not name or type(identity) is not list or len(identity) != 7
            or any(type(field) is not int for field in identity)):
        return None
    return name, identity


def music_root_matches(identity) -> bool:
    """Whether the music folder is still the directory incarnation `identity`.

    `identity` must be the 7-int list capture_music_root_identity returns;
    anything else, or a folder that cannot be opened, is not a match.
    """
    if (type(identity) is not list or len(identity) != 7
            or any(type(field) is not int for field in identity)):
        return False
    current = capture_music_root_identity()
    return (current is not None
            and _durable_generations_match([identity], [current]))


def _validate_absent(saved) -> dict:
    name, identity = saved
    if not music_root_matches(identity):
        raise CandidateStale(
            "The music folder is not the one this review was built against. "
            "Check that your library is mounted, then build the review again; "
            "nothing was changed."
        )
    return {
        "version": PREMISE_VERSION,
        "kind": "missing",
        "path": os.path.abspath(os.fspath(cfg.MUSIC_ROOT / name)),
        "receipt": None,
    }


def restore_candidate(candidate: dict, premise) -> dict:
    payload = candidate.get("payload") or {}
    if (
        premise is None
        or not isinstance(payload, dict)
        or "_premise" in payload
    ):
        return candidate
    return {**candidate, "payload": {**payload, "_premise": premise}}


def compact_candidate(candidate: dict, premise) -> dict:
    payload = candidate.get("payload") or {}
    if premise is None or not isinstance(payload, dict):
        return candidate
    payload = dict(payload)
    value = payload.get("_premise")
    if value is premise or value == premise:
        payload.pop("_premise")
    elif "_premise" not in payload:
        # A failed capture must not inherit another row's receipt.
        payload["_premise"] = None
    return {**candidate, "payload": payload}


def restore_artist(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("candidates"), list):
        return entry
    restored = dict(entry)
    premise = restored.pop("_premise", None)
    restored["candidates"] = [
        restore_candidate(candidate, premise)
        if isinstance(candidate, dict) else candidate
        for candidate in entry["candidates"]
    ]
    return restored


def compact_artist(entry):
    """Store a shared missing-album premise on its artist snapshot."""
    entry = restore_artist(entry)
    if not isinstance(entry, dict) or not isinstance(entry.get("candidates"), list):
        return entry
    premise = None
    for candidate in entry["candidates"]:
        payload = candidate.get("payload") if isinstance(candidate, dict) else None
        value = payload.get("_premise") if isinstance(payload, dict) else None
        if isinstance(value, dict) and value.get("kind") == "missing":
            premise = value
            break
    if premise is None:
        return entry
    return {
        **entry,
        "_premise": premise,
        "candidates": [
            compact_candidate(candidate, premise)
            if isinstance(candidate, dict) else candidate
            for candidate in entry["candidates"]
        ],
    }


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
    if premise is None or premise["version"] != PREMISE_VERSION:
        raise CandidateStale(
            "This queued action is too old to verify against the current "
            "files. Refresh or rescan before changing music files.",
            cause="older",
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
    """Derive per-file backup receipts from one canonical tree seal."""
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
            }
            if "sha256" in snapshot:
                receipts[relative]["sha256"] = snapshot["sha256"]
        return receipts
    except (KeyError, TypeError, ValueError):
        return None


def _refuse_unsealed(candidate, premise, links=None):
    """Refuse a row saved without a current seal, naming the symbolic link
    that kept its folder from being sealed when there is one. ``links``
    remembers each folder's answer across one sweep of rows."""
    path = expected_path(candidate)
    link = None
    if premise is None and path:
        links = {} if links is None else links
        if path not in links:
            links[path] = first_symlink(path)
        link = links[path]
    if link:
        raise CandidateStale(
            f"{link} in {Path(path).name} is a symbolic link, which can't be "
            "checked or backed up safely. Replace it with a copy of its file, "
            "then refresh; nothing was changed.",
            cause="link",
        )
    raise CandidateStale(
        "This saved review is too old to check against the current files. "
        "Refresh it before changing music files.",
        cause="older",
    )


def validate(candidate: dict, *, missing_premises=None, links=None) -> dict:
    """Check a saved review against the current files before using it."""
    absent = _canonical_absent(candidate)
    if absent is not None:
        return _validate_absent(absent)
    premise = canonical(candidate)
    if premise is None or premise["version"] != PREMISE_VERSION:
        _refuse_unsealed(candidate, premise, links)
    if premise["kind"] == "missing" and missing_premises is not None:
        path = premise["path"]
        if path not in missing_premises:
            missing_premises[path] = capture("missing", path)
        current = missing_premises[path]
    else:
        current = capture(premise["kind"], premise["path"])
    if current is None and os.path.lexists(premise["path"]):
        raise CandidateStale(
            "The local files could not be read. Refresh the review; nothing "
            "was changed.",
            cause="unreadable",
        )
    if not _durable_premises_match(premise, current):
        raise CandidateStale(
            "The local files changed after this review was built. Refresh the "
            "review; nothing was changed."
        )
    return current


def changed_on_disk(candidate: dict, missing_premises=None) -> bool:
    """Whether this row's files changed since its review was built.

    A folder that is not there is not taken for a change: an interrupted
    upgrade or repair leaves the album in its backup until it is put back.
    Nor is a row with no artist folder: its seal is only the music folder's
    identity, which a library that is not mounted yet fails too. Nor is a
    folder whose files keep their names, sizes and mtimes, as an album put
    back from a copy on another filesystem does; its row keeps its seal, so
    Start still asks for a refresh.
    """
    if _canonical_absent(candidate) is not None:
        return False
    path = expected_path(candidate)
    if path and not os.path.lexists(path):
        return False
    try:
        validate(candidate, missing_premises=missing_premises)
    except CandidateStale as exc:
        if exc.cause != "changed":
            return False
        premise = canonical(candidate)
        current = ((missing_premises or {}).get(premise["path"])
                   or capture(premise["kind"], premise["path"]))
        return (current is None
                or _names_sizes_times(current["receipt"]["tree"])
                != _names_sizes_times(premise["receipt"]["tree"]))
    return False


def resealed_after_put_back(value, backup_receipt, captures):
    """A fresh seal for a row sealed over an album as it was backed up, once
    the put-back proved every file's bytes against the backup, or None.

    A copy back from another filesystem gives every file a new inode, so the
    old seal would refuse an album whose bytes are proven the same. A row
    whose seal still holds, that was sealed over another version of the
    folder, or whose folder no longer holds the restored files by name, size
    and mtime, keeps its seal.
    """
    origin = backup_receipt.get("origin")
    if type(value) is not dict or value.get("path") != origin:
        return None
    premise = canonical_premise(value)
    if (premise is None or premise["version"] != PREMISE_VERSION
            or premise["kind"] == "missing" or premise["path"] != origin):
        return None
    try:
        source = backup_receipt["source_receipt"]
        sealed = _canonical_receipt(_album_tree_identity(source), origin=origin)
        backed_up = {
            relative: value["sha256"]
            for relative, value in backup_receipt["tree"]["files"].items()
            if Path(relative).name not in _SIDECARS
        }
        source_digests = {relative: value["sha256"]
                          for relative, value in source["tree"]["files"].items()}
    except (KeyError, TypeError):
        return None
    if (sealed is None or backed_up != source_digests
            or not durable_receipts_match(premise["receipt"], sealed)):
        return None
    kind = premise["kind"]
    if kind not in captures:
        captures[kind] = capture(kind, origin)
    fresh = captures[kind]
    if (fresh is None or _durable_premises_match(premise, fresh)
            or _names_sizes_times(fresh["receipt"]["tree"])
            != _names_sizes_times(source["tree"])):
        return None
    return fresh


def resealed_after_own_imports(value, fresh, folders):
    """``fresh``, a capture of a missing album's artist folder, as the new
    seal for a row sealed over it as ``value``, when everything sealed is
    unchanged and all that was added is the new top-level ``folders`` a run
    imported albums into. None otherwise, or when nothing changed."""
    premise = canonical_premise(value)
    if (premise is None or premise["version"] != PREMISE_VERSION
            or premise["kind"] != "missing" or fresh is None
            or _durable_premises_match(premise, fresh)):
        return None
    folders = set(folders)

    def outside(receipt):
        def keep(entries):
            return {relative: entry for relative, entry in entries.items()
                    if relative.split("/", 1)[0] not in folders}
        tree = receipt["tree"]
        return {**receipt,
                "tree": {**tree, "directories": keep(tree["directories"]),
                         "files": keep(tree["files"])},
                "directory_generations": keep(receipt["directory_generations"])}

    sealed = premise["receipt"]
    if premise["path"] != fresh["path"] or outside(sealed) != sealed:
        return None
    return fresh if durable_receipts_match(sealed, outside(fresh["receipt"])) else None


def _sealed_file(identity):
    if identity is None:
        return None
    return {
        "identity": [stat.S_IFREG, identity["device"], identity["inode"]],
        "size": identity["size"],
        "mtime_ns": identity["modified_ns"],
        "changed_ns": identity["changed_ns"],
    }


def sealer_after_writes(writes):
    """``resealed_after_writes`` for one set of writes, sharing captures."""
    captures = {}
    return lambda value: resealed_after_writes(value, writes, captures)


def resealed_after_writes(value, writes, captures):
    """A fresh seal for a row whose folder changed only by ``writes``, the
    app's own file writes as ``{path: (before, after)}``, or None.

    Every written file must have been exactly as sealed before the write and
    be exactly as written now, and nothing else in the folder may differ, so
    a hand edit anywhere in it still refuses the row.
    """
    premise = canonical_premise(value)
    if premise is None or premise["version"] != PREMISE_VERSION:
        return None
    root = Path(premise["path"])
    touched = {}
    for path, change in writes.items():
        path = Path(path)
        if path != root and path.is_relative_to(root):
            touched[path.relative_to(root).as_posix()] = change
    if not touched:
        return None
    key = (premise["kind"], premise["path"])
    if key not in captures:
        captures[key] = capture(*key)
    fresh = captures[key]
    if fresh is None or _durable_premises_match(premise, fresh):
        return None
    sealed = premise["receipt"]
    files = dict(sealed["tree"]["files"])
    try:
        for relative, (before, after) in touched.items():
            if files.get(relative) != _sealed_file(before):
                return None
            if after is None:
                files.pop(relative, None)
            else:
                files[relative] = _sealed_file(after)
    except (KeyError, TypeError, ValueError):
        return None
    expected = {**sealed, "tree": {**sealed["tree"], "files": files}}
    return fresh if durable_receipts_match(expected, fresh["receipt"]) else None


def _names_sizes_times(tree):
    return {relative: (value["size"], value["mtime_ns"])
            for relative, value in tree["files"].items()}


def validate_container(candidate: dict) -> dict:
    """Validate only the no-follow directory incarnation for a missing row.

    Several missing albums from one artist may be approved together. The first
    download legitimately changes that artist tree, but it must not authorize
    a replaced artist directory or a different mounted path for later picks.
    """
    absent = _canonical_absent(candidate)
    if absent is not None:
        return _validate_absent(absent)
    premise = canonical(candidate)
    if premise is None or premise["version"] != PREMISE_VERSION:
        _refuse_unsealed(candidate, premise)
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
            "review; nothing was changed.",
            cause="unreadable",
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


def validate_all(candidates, *, cancelled=None) -> bool:
    """Check every row against its files as they are right now.

    Rows naming the same folder with the same saved receipt pass or fail
    together, so they share one check: an artist with many picks is walked
    once. Returns False when ``cancelled`` stopped it between rows.
    """
    checked = {}
    for candidate in candidates:
        if cancelled is not None and cancelled():
            return False
        payload = candidate.get("payload") or {}
        saved = [payload.get("_premise"), payload.get(ABSENT_CONTAINER_KEY)]
        seen = checked.setdefault(
            (expected_kind(candidate), expected_path(candidate)), [])
        if saved not in seen:
            validate(candidate)
            seen.append(saved)
    return True


def stale_candidate_ids(candidates, *, share_artist_captures=False) -> StaleCandidateIds:
    """Find rows that no longer match their files.

    ``share_artist_captures`` seals each artist once for the whole sweep,
    which is only safe where a fresh unshared check follows before anything
    is written.
    """
    missing_premises = {} if share_artist_captures else None
    links = {}
    stale = StaleCandidateIds()
    for candidate in candidates:
        try:
            validate(candidate, missing_premises=missing_premises, links=links)
        except CandidateStale as exc:
            stale.add(candidate.get("cid"))
            stale.counts[exc.cause] += 1
    return stale


def expected_album_receipt(candidate: dict) -> dict | None:
    premise = canonical(candidate)
    return None if premise is None else premise["receipt"]
