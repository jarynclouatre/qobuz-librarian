"""Crash-recoverable post-import album-folder relocation.

The filesystem and Beets cannot share one transaction.  This module therefore
keeps the source authoritative while complete independent copies are prepared,
records every no-overwrite publication durably, and uses the exact Beets rows
as the commit decision.  Restart recovery rolls exact additions back while the
old rows remain, or finishes source cleanup after the new rows are durable.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import secrets
import sqlite3
import stat
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import normalise_album_id
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.interrupts import run_sigint_deferred
from qobuz_librarian.library.catalog import (
    _close_migration_database_anchor,
    _migration_database_anchor_matches,
    _open_migration_database_anchor,
)
from qobuz_librarian.library.post_import_relocation_db import (
    DatabaseRowsState,
    capture_relocation_rows,
    classify_relocation_rows,
    publish_relocation_rows,
    rollback_relocation_rows,
)
from qobuz_librarian.library.sqlite_atomic import (
    SQLitePublicationUncertain,
    SQLiteRecoveryRequired,
    inspect_sqlite_source,
    reconcile_atomic_sqlite,
)
from qobuz_librarian.recovery import decode_recovery_json
from qobuz_librarian.run_lock import RunLockLease

_NAMESPACE_NAME = ".qobuz_post_import_relocations"
_LOCK_NAME = ".lock"
_WORKSPACE_PREFIX = ".qobuz-librarian-relocation-"
_JOURNAL_VERSION = 1
_EXPECTATION_VERSION = 1
_MAX_JOURNAL_BYTES = 1024 * 1024
_MAX_TREE_ENTRIES = 10_000
_MAX_TREE_DEPTH = 16
_COPY_BLOCK_SIZE = 1024 * 1024
_RENAME_NOREPLACE = 1
_HEX_64 = frozenset("0123456789abcdef")
_THREAD_LOCK = threading.RLock()


class RelocationKind(str, Enum):
    WHOLE_ALBUM = "whole-album"
    SPLIT_GAP_FILL = "split-gap-fill"


class RelocationRecoveryStatus(str, Enum):
    CLEAR = "clear"
    ATTENTION_REQUIRED = "attention-required"


@dataclass(frozen=True, slots=True)
class RelocationRecoveryResult:
    status: RelocationRecoveryStatus
    reason: str | None = None
    paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class RelocationResult:
    destination: Path
    published_files: int
    operation_id: str | None = None
    ownership_receipt: dict | None = None
    changed: bool = False
    reason: str | None = None


class PostImportRelocationUnavailable(OSError):
    """The automatic relocation refused without risking user data."""


class PostImportRelocationAttention(OSError):
    """Durable relocation evidence must be reviewed before more writes."""

    def __init__(self, message, *, paths=()):
        self.paths = tuple(Path(path) for path in paths)
        super().__init__(errno.EBUSY, message)


def _checkpoint(_name: str) -> None:
    """A no-op boundary that child-process kill regressions may replace."""


def _require_authority(authority: RunLockLease) -> None:
    if type(authority) is not RunLockLease or authority.intact() is not True:
        raise PostImportRelocationUnavailable(
            errno.EBUSY,
            "the shared run lock was lost before the folder move",
        )


def _namespace_path() -> Path:
    return Path(cfg.DATA_DIR) / _NAMESPACE_NAME


def _valid_operation_id(value) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX_64 for character in value)
    )


def _valid_relative(value, *, album=False, allow_root=False) -> tuple[str, ...]:
    if type(value) is not str or "\x00" in value:
        raise ValueError("relocation path is invalid")
    if allow_root and value == "":
        return ()
    path = PurePosixPath(value)
    parts = path.parts
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in ("", ".", "..") for part in parts)
        or album and len(parts) != 2
    ):
        raise ValueError("relocation path is outside the expected album scope")
    return tuple(parts)


def _absolute_album_path(value) -> tuple[Path, Path, tuple[str, str]]:
    root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    path = Path(os.path.abspath(os.fspath(value)))
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise PostImportRelocationUnavailable(
            errno.EINVAL, "album folder is outside the configured music library"
        ) from None
    parts = _valid_relative(relative.as_posix(), album=True)
    return root, path, (parts[0], parts[1])


def _open_directory(value, *, dir_fd=None) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow directory access is unavailable")
    return os.open(
        value,
        os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0),
        dir_fd=dir_fd,
    )


def _open_regular(value, *, dir_fd=None) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file access is unavailable")
    descriptor = os.open(
        value,
        os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=dir_fd,
    )
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("relocation entry is not a regular file")
    return descriptor


def _open_entry_handle(value, *, dir_fd=None) -> int:
    path_only = getattr(os, "O_PATH", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if path_only is None or nofollow is None:
        raise OSError("safe no-follow entry access is unavailable")
    return os.open(
        value,
        path_only | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=dir_fd,
    )


def _entry_identity(value) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _named_entry_matches(parent_fd, name, descriptor) -> bool:
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_IFMT(held.st_mode) == stat.S_IFMT(named.st_mode)
        and _entry_identity(held) == _entry_identity(named)
    )


def _named_matches(parent_fd, name, descriptor, *, directory=False) -> bool:
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    return expected(held.st_mode) and expected(named.st_mode) and (
        _entry_identity(held) == _entry_identity(named)
    )


def _name_missing(parent_fd, name) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _run_descriptor_sigint_deferred(callback):
    """Finish one descriptor ownership transition before delivering Ctrl-C."""
    return run_sigint_deferred(
        callback, detail="interrupt-safe relocation descriptor access is unavailable")


def _close_descriptor_state(descriptors) -> None:
    interrupted = None
    while descriptors:
        descriptor = descriptors.pop()
        try:
            os.close(descriptor)
        except OSError:
            pass
        except BaseException as exc:
            if interrupted is None:
                interrupted = exc
    if interrupted is not None:
        raise interrupted


def _finalize_descriptor_state(descriptors) -> None:
    try:
        _run_descriptor_sigint_deferred(
            lambda: _close_descriptor_state(descriptors)
        )
    except BaseException:
        pass


class _DescriptorOwner:
    """Finalizable owner for one returned descriptor group."""

    def __init__(self):
        self._descriptors = []
        self._visible = []
        self._finalizer = weakref.finalize(
            self,
            _finalize_descriptor_state,
            self._descriptors,
        )

    def adopt(self, descriptor, *, visible=False):
        if not self._finalizer.alive:
            raise RuntimeError("descriptor owner is closed")
        self._descriptors.append(descriptor)
        if visible:
            self._visible.append(descriptor)
        return descriptor

    def open(self, opener, *, visible=False):
        def open_and_adopt():
            descriptor = opener()
            owned_before = len(self._descriptors)
            try:
                return self.adopt(descriptor, visible=visible)
            except BaseException:
                if len(self._descriptors) == owned_before:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                raise

        return _run_descriptor_sigint_deferred(open_and_adopt)

    def close(self) -> None:
        try:
            _run_descriptor_sigint_deferred(
                lambda: _close_descriptor_state(self._descriptors)
            )
        finally:
            self._visible.clear()
            if not self._descriptors:
                self._finalizer.detach()

    def __getitem__(self, index):
        return self._visible[index]

    def __iter__(self):
        return iter(self._visible)

    def __len__(self):
        return len(self._visible)


def _close_all(descriptors) -> None:
    if descriptors is None:
        return
    if isinstance(descriptors, _DescriptorOwner):
        descriptors.close()
        return
    for descriptor in reversed(tuple(descriptors)):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _open_music_root(expected: Path | None = None) -> tuple[int, _DescriptorOwner]:
    root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    if expected is not None and root != expected:
        raise OSError("configured music root changed")
    owner = _DescriptorOwner()
    descriptor = owner.open(lambda: _open_directory(root))
    try:
        named = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode) or _entry_identity(named) != _entry_identity(
            os.fstat(descriptor)
        ):
            raise OSError("configured music root changed while it was opened")
        return descriptor, owner
    except BaseException:
        _close_all(owner)
        raise


def _open_relative_directories(root_fd, parts, *, create=False) -> _DescriptorOwner:
    descriptors = _DescriptorOwner()
    parent_fd = root_fd
    try:
        for part in parts:
            try:
                child = descriptors.open(
                    lambda: _open_directory(part, dir_fd=parent_fd),
                    visible=True,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
                child = descriptors.open(
                    lambda: _open_directory(part, dir_fd=parent_fd),
                    visible=True,
                )
            if not _named_matches(parent_fd, part, child, directory=True):
                raise OSError("relocation directory changed while it was opened")
            parent_fd = child
        return descriptors
    except BaseException:
        _close_all(descriptors)
        raise


def _stat_record(value) -> dict:
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
        "mode": int(stat.S_IMODE(value.st_mode)),
    }


def _digest_fd(descriptor) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        block = os.pread(descriptor, _COPY_BLOCK_SIZE, offset)
        if not block:
            return digest.hexdigest()
        digest.update(block)
        offset += len(block)


def _file_record(descriptor) -> dict:
    before = os.fstat(descriptor)
    digest = _digest_fd(descriptor)
    after = os.fstat(descriptor)
    if _stat_record(before) != _stat_record(after) or not stat.S_ISREG(after.st_mode):
        raise OSError("relocation source file changed while it was read")
    return {**_stat_record(after), "sha256": digest}


def _snapshot_tree(root_fd) -> dict:
    counter = 0
    snapshot = {"root": _stat_record(os.fstat(root_fd)), "directories": {}, "files": {}}

    def walk(directory_fd, prefix, depth):
        nonlocal counter
        if depth > _MAX_TREE_DEPTH:
            raise OSError("album folder is nested too deeply to move safely")
        before = _stat_record(os.fstat(directory_fd))
        names = sorted(os.listdir(directory_fd))
        for name in names:
            if type(name) is not str or name in ("", ".", "..") or "\x00" in name:
                raise OSError("album folder contains an invalid entry name")
            counter += 1
            if counter > _MAX_TREE_ENTRIES:
                raise OSError("album folder has too many entries to journal safely")
            relative = prefix + (name,)
            key = PurePosixPath(*relative).as_posix()
            value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(value.st_mode):
                child = _open_directory(name, dir_fd=directory_fd)
                try:
                    if not _named_matches(directory_fd, name, child, directory=True):
                        raise OSError("album directory changed while it was opened")
                    snapshot["directories"][key] = _stat_record(os.fstat(child))
                    walk(child, relative, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(value.st_mode):
                child = _open_regular(name, dir_fd=directory_fd)
                try:
                    if not _named_matches(directory_fd, name, child):
                        raise OSError("album file changed while it was opened")
                    snapshot["files"][key] = _file_record(child)
                finally:
                    os.close(child)
            else:
                raise OSError("album folder contains a link or special entry")
        after = _stat_record(os.fstat(directory_fd))
        if before != after or sorted(os.listdir(directory_fd)) != names:
            raise OSError("album directory changed while it was scanned")

    walk(root_fd, (), 0)
    snapshot["root"] = _stat_record(os.fstat(root_fd))
    return snapshot


def _directory_record_equivalent(left, right) -> bool:
    return all(left.get(key) == right.get(key) for key in ("device", "inode", "mode"))


def _same_node_record(left, right) -> bool:
    return all(left.get(key) == right.get(key) for key in ("device", "inode"))


def _tree_equivalent(left, right, *, renamed_root=False) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if renamed_root:
        root_matches = _directory_record_equivalent(left.get("root", {}), right.get("root", {}))
    else:
        root_matches = left.get("root") == right.get("root")
    return (
        root_matches
        and left.get("directories") == right.get("directories")
        and left.get("files") == right.get("files")
    )


def _snapshot_matches(root_fd, expected, *, renamed_root=False) -> bool:
    try:
        return _tree_equivalent(
            _snapshot_tree(root_fd), expected, renamed_root=renamed_root
        )
    except (OSError, TypeError, ValueError):
        return False


def _record_matches_fd(descriptor, expected) -> bool:
    try:
        return _file_record(descriptor) == expected
    except (OSError, TypeError, ValueError):
        return False


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


_STAT_RECORD_KEYS = frozenset({
    "device",
    "inode",
    "size",
    "modified_ns",
    "changed_ns",
    "mode",
})
_DIRECTORY_IDENTITY_KEYS = frozenset({"device", "inode", "mode"})
_EXPECTATION_KEYS = frozenset({
    "version",
    "music_root",
    "root_identity",
    "source",
    "source_artist_identity",
    "source_snapshot",
    "destination",
    "destination_artist_identity",
    "destination_snapshot",
})


def _stat_record_schema_valid(value, *, file=False) -> bool:
    expected = _STAT_RECORD_KEYS | ({"sha256"} if file else set())
    if type(value) is not dict or set(value) != expected:
        return False
    if any(type(value[key]) is not int for key in _STAT_RECORD_KEYS):
        return False
    if (
        value["device"] < 0
        or value["inode"] < 0
        or value["size"] < 0
        or value["mode"] < 0
        or value["mode"] > 0o7777
    ):
        return False
    return not file or _valid_operation_id(value["sha256"])


def _directory_identity_schema_valid(value) -> bool:
    return (
        type(value) is dict
        and set(value) == _DIRECTORY_IDENTITY_KEYS
        and all(type(value[key]) is int for key in _DIRECTORY_IDENTITY_KEYS)
        and value["device"] >= 0
        and value["inode"] >= 0
        and 0 <= value["mode"] <= 0o7777
    )


def _snapshot_schema_valid(value) -> bool:
    if (
        type(value) is not dict
        or set(value) != {"root", "directories", "files"}
        or not _stat_record_schema_valid(value["root"])
        or type(value["directories"]) is not dict
        or type(value["files"]) is not dict
        or len(value["directories"]) + len(value["files"]) > _MAX_TREE_ENTRIES
        or set(value["directories"]) & set(value["files"])
    ):
        return False
    directories = set(value["directories"])
    for relative, record in (
        *value["directories"].items(),
        *value["files"].items(),
    ):
        try:
            parts = _valid_relative(relative)
        except ValueError:
            return False
        if len(parts) > _MAX_TREE_DEPTH or (
            len(parts) > 1
            and PurePosixPath(*parts[:-1]).as_posix() not in directories
        ):
            return False
        if not _stat_record_schema_valid(
            record, file=relative in value["files"]
        ):
            return False
    return True


def _directory_identity(record) -> dict:
    return {key: record[key] for key in ("device", "inode", "mode")}


def _directory_identity_matches_fd(descriptor, expected) -> bool:
    try:
        return _directory_identity(
            _stat_record(os.fstat(descriptor))
        ) == expected
    except (OSError, TypeError, ValueError):
        return False


def _expectation_record(
    root,
    root_fd,
    source_path,
    source_artist_record,
    source_snapshot,
    destination_path,
    destination_artist_record,
    destination_snapshot,
) -> dict:
    return {
        "version": _EXPECTATION_VERSION,
        "music_root": os.fspath(root),
        "root_identity": _directory_identity(_stat_record(os.fstat(root_fd))),
        "source": os.fspath(source_path),
        "source_artist_identity": _directory_identity(source_artist_record),
        "source_snapshot": source_snapshot,
        "destination": os.fspath(destination_path),
        "destination_artist_identity": (
            None
            if destination_artist_record is None
            else _directory_identity(destination_artist_record)
        ),
        "destination_snapshot": destination_snapshot,
    }


def canonical_post_import_relocation_expectation(
    value, *, source=None, destination=None
) -> dict | None:
    """Return one closed-schema relocation expectation without touching disk."""
    if type(value) is not dict or set(value) != _EXPECTATION_KEYS:
        return None
    try:
        root, recorded_source, _source_parts = _absolute_album_path(
            value.get("source")
        )
        destination_root, recorded_destination, _destination_parts = (
            _absolute_album_path(value.get("destination"))
        )
        if source is not None:
            source_root, expected_source, _parts = _absolute_album_path(source)
            if source_root != root or expected_source != recorded_source:
                return None
        if destination is not None:
            expected_root, expected_destination, _parts = _absolute_album_path(
                destination
            )
            if (
                expected_root != root
                or expected_destination != recorded_destination
            ):
                return None
        encoded = _canonical_json(value)
        canonical = json.loads(encoded)
    except (OSError, TypeError, ValueError, UnicodeError):
        return None
    destination_identity = value.get("destination_artist_identity")
    destination_snapshot = value.get("destination_snapshot")
    if (
        value.get("version") != _EXPECTATION_VERSION
        or value.get("music_root") != os.fspath(root)
        or destination_root != root
        or recorded_source == recorded_destination
        or value.get("source") != os.fspath(recorded_source)
        or value.get("destination") != os.fspath(recorded_destination)
        or not _directory_identity_schema_valid(value.get("root_identity"))
        or not _directory_identity_schema_valid(
            value.get("source_artist_identity")
        )
        or not _snapshot_schema_valid(value.get("source_snapshot"))
        or destination_identity is not None
        and not _directory_identity_schema_valid(destination_identity)
        or destination_snapshot is not None
        and not _snapshot_schema_valid(destination_snapshot)
        or destination_snapshot is not None and destination_identity is None
        or canonical != value
    ):
        return None
    return canonical


def _validated_handoff_consumer(value) -> dict:
    if type(value) is not dict:
        raise ValueError("relocation handoff consumer is invalid")
    if value.get("kind") == "queue-completion":
        if set(value) != {
            "kind",
            "queue_operation_id",
            "item_id",
            "action_id",
        } or not all(
            _valid_operation_id(value.get(field))
            for field in ("queue_operation_id", "item_id", "action_id")
        ):
            raise ValueError("relocation handoff consumer is invalid")
        return {
            "kind": "queue-completion",
            "queue_operation_id": value["queue_operation_id"],
            "item_id": value["item_id"],
            "action_id": value["action_id"],
        }
    if set(value) != {
        "kind",
        "job_id",
        "job_created_at",
        "album_id",
    }:
        raise ValueError("relocation handoff consumer is invalid")
    job_id = value.get("job_id")
    created_at = value.get("job_created_at")
    album_id = value.get("album_id")
    if (
        value.get("kind") != "web-single"
        or type(job_id) is not str
        or not job_id
        or "\x00" in job_id
        or type(created_at) not in (int, float)
        or not math.isfinite(created_at)
        or normalise_album_id(album_id) != album_id
    ):
        raise ValueError("relocation handoff consumer is invalid")
    return {
        "kind": "web-single",
        "job_id": job_id,
        "job_created_at": created_at,
        "album_id": album_id,
    }


def _validated_handoff(value) -> dict | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"consumer", "hash"}:
        raise ValueError("relocation handoff is invalid")
    consumer = _validated_handoff_consumer(value.get("consumer"))
    digest = value.get("hash")
    if not _valid_operation_id(digest):
        raise ValueError("relocation handoff is invalid")
    return {"consumer": consumer, "hash": digest}


def _handoff_digest(consumer, payload) -> tuple[dict, str]:
    consumer = _validated_handoff_consumer(consumer)
    if type(payload) is not dict:
        raise ValueError("relocation handoff payload is invalid")
    try:
        encoded_payload = _canonical_json(payload)
        if json.loads(encoded_payload) != payload:
            raise ValueError("relocation handoff payload is not canonical JSON")
        encoded = _canonical_json({"consumer": consumer, "payload": payload})
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("relocation handoff payload is invalid") from exc
    return consumer, hashlib.sha256(encoded).hexdigest()


def _journal_bytes(payload) -> bytes:
    encoded = _canonical_json(payload)
    return _canonical_json({
        "checksum": hashlib.sha256(encoded).hexdigest(),
        "payload": payload,
    })


def _decode_journal(raw) -> dict:
    envelope = decode_recovery_json(raw, max_bytes=_MAX_JOURNAL_BYTES)
    if type(envelope) is not dict or set(envelope) != {"checksum", "payload"}:
        raise ValueError("relocation journal envelope is invalid")
    checksum = envelope["checksum"]
    payload = envelope["payload"]
    if (
        type(checksum) is not str
        or len(checksum) != 64
        or any(character not in _HEX_64 for character in checksum)
        or not isinstance(payload, dict)
        or not secrets.compare_digest(
            checksum, hashlib.sha256(_canonical_json(payload)).hexdigest()
        )
    ):
        raise ValueError("relocation journal checksum is invalid")
    return payload


def _open_configured_directory(path) -> tuple[int, _DescriptorOwner]:
    candidate = Path(os.path.abspath(os.fspath(path)))
    owner = _DescriptorOwner()
    descriptor = owner.open(lambda: _open_directory(candidate))
    try:
        named = os.stat(candidate, follow_symlinks=False)
        held = os.fstat(descriptor)
        if not stat.S_ISDIR(named.st_mode) or _entry_identity(named) != _entry_identity(
            held
        ):
            raise OSError("configured data directory changed while it was opened")
        return descriptor, owner
    except BaseException:
        _close_all(owner)
        raise


@contextmanager
def _locked_namespace(*, create):
    """Open the private journal namespace without following a replacement link."""
    data_path = Path(os.path.abspath(os.fspath(cfg.DATA_DIR)))
    try:
        data_fd, data_owner = _open_configured_directory(data_path)
    except FileNotFoundError:
        if not create:
            yield None
            return
        data_path.mkdir(parents=True, mode=0o700, exist_ok=True)
        data_fd, data_owner = _open_configured_directory(data_path)

    namespace_fd = lock_fd = None
    try:
        try:
            namespace_fd = _open_directory(_NAMESPACE_NAME, dir_fd=data_fd)
        except FileNotFoundError:
            if not create:
                yield None
                return
            os.mkdir(_NAMESPACE_NAME, 0o700, dir_fd=data_fd)
            os.fsync(data_fd)
            namespace_fd = _open_directory(_NAMESPACE_NAME, dir_fd=data_fd)
        if not _named_matches(data_fd, _NAMESPACE_NAME, namespace_fd, directory=True):
            raise OSError("relocation journal directory changed while it was opened")
        lock_fd = os.open(
            _LOCK_NAME,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=namespace_fd,
        )
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise OSError("relocation journal lock is unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not _named_matches(namespace_fd, _LOCK_NAME, lock_fd):
            raise OSError("relocation journal lock changed while it was acquired")
        yield namespace_fd
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        if namespace_fd is not None:
            os.close(namespace_fd)
        _close_all(data_owner)


def _journal_name(operation_id) -> str:
    if not _valid_operation_id(operation_id):
        raise ValueError("relocation operation ID is invalid")
    return f"{operation_id}.json"


def _temporary_operation_id(name) -> str:
    prefix = ".tmp-"
    expected_length = len(prefix) + 64 + 1 + 24
    if (
        type(name) is not str
        or len(name) != expected_length
        or not name.startswith(prefix)
        or name[len(prefix) + 64] != "-"
    ):
        raise ValueError("relocation journal temporary name is invalid")
    operation_id = name[len(prefix) : len(prefix) + 64]
    token = name[len(prefix) + 65 :]
    if not _valid_operation_id(operation_id) or any(
        character not in _HEX_64 for character in token
    ):
        raise ValueError("relocation journal temporary name is invalid")
    return operation_id


def _read_journal(namespace_fd, operation_id) -> dict:
    name = _journal_name(operation_id)
    descriptor = _open_regular(name, dir_fd=namespace_fd)
    try:
        if not _named_matches(namespace_fd, name, descriptor):
            raise OSError("relocation journal changed while it was opened")
        value = os.fstat(descriptor)
        if value.st_nlink != 1 or value.st_size > _MAX_JOURNAL_BYTES:
            raise ValueError("relocation journal is unsafe or too large")
        raw = b""
        while len(raw) <= _MAX_JOURNAL_BYTES:
            block = os.read(descriptor, min(65536, _MAX_JOURNAL_BYTES + 1 - len(raw)))
            if not block:
                break
            raw += block
        return _decode_journal(raw)
    finally:
        os.close(descriptor)


def _write_journal(namespace_fd, payload, *, initial=False) -> None:
    operation_id = payload.get("operation_id") if isinstance(payload, dict) else None
    name = _journal_name(operation_id)
    raw = _journal_bytes(payload)
    if len(raw) > _MAX_JOURNAL_BYTES:
        raise OSError(errno.EFBIG, "relocation journal is too large")
    temporary = f".tmp-{operation_id}-{secrets.token_hex(12)}"
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=namespace_fd,
        )
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("relocation journal write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if initial:
            _rename_noreplace(namespace_fd, temporary, namespace_fd, name)
        else:
            current = _read_journal(namespace_fd, operation_id)
            if current.get("operation_id") != operation_id:
                raise OSError("relocation journal changed before update")
            os.replace(temporary, name, src_dir_fd=namespace_fd, dst_dir_fd=namespace_fd)
        os.fsync(namespace_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=namespace_fd)
        except FileNotFoundError:
            pass


def _unlink_journal(namespace_fd, operation_id) -> None:
    name = _journal_name(operation_id)
    descriptor = _open_regular(name, dir_fd=namespace_fd)
    try:
        if not _named_matches(namespace_fd, name, descriptor):
            raise OSError("relocation journal changed before cleanup")
        os.unlink(name, dir_fd=namespace_fd)
        os.fsync(namespace_fd)
    finally:
        os.close(descriptor)


def _rename_noreplace(source_fd, source, destination_fd, destination) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    if renameat2(
        int(source_fd),
        os.fsencode(source),
        int(destination_fd),
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination))


def _audio_relative(relative) -> bool:
    return PurePosixPath(relative).suffix.lower() in cfg.AUDIO_EXTS


def _subtree_snapshot(snapshot, relative) -> dict:
    prefix = f"{relative}/" if relative else ""
    root_record = (
        snapshot["root"] if not relative else snapshot["directories"][relative]
    )
    return {
        "root": root_record,
        "directories": {
            key[len(prefix):]: value
            for key, value in snapshot["directories"].items()
            if key.startswith(prefix) and key != relative
        },
        "files": {
            key[len(prefix):]: value
            for key, value in snapshot["files"].items()
            if key.startswith(prefix)
        },
    }


def _build_plan(source, destination) -> dict:
    """Select only additions; pre-existing destination names are never replaced."""
    if destination is None:
        unit = {
            "index": 0,
            "kind": "tree",
            "relative": "",
            "source_record": _subtree_snapshot(source, ""),
        }
        return {
            "units": [unit],
            "duplicates": [],
            "selected_files": sorted(source["files"]),
            "conflicts": [],
        }

    units = []
    duplicates = []
    selected = []
    conflicts = []
    destination_files = destination["files"]
    destination_directories = destination["directories"]

    def add_tree(relative):
        unit = {
            "index": len(units),
            "kind": "tree",
            "relative": relative,
            "source_record": _subtree_snapshot(source, relative),
        }
        units.append(unit)
        prefix = f"{relative}/"
        selected.extend(
            key for key in source["files"] if key.startswith(prefix)
        )

    def walk(relative=""):
        prefix = f"{relative}/" if relative else ""
        child_directories = sorted(
            key for key in source["directories"]
            if key.startswith(prefix) and "/" not in key[len(prefix):]
        )
        child_files = sorted(
            key for key in source["files"]
            if key.startswith(prefix) and "/" not in key[len(prefix):]
        )
        for key in child_directories:
            if key in destination_files:
                conflicts.append(key)
            elif key not in destination_directories:
                add_tree(key)
            else:
                walk(key)
        for key in child_files:
            if key not in destination_files and key not in destination_directories:
                units.append({
                    "index": len(units),
                    "kind": "file",
                    "relative": key,
                    "source_record": source["files"][key],
                })
                selected.append(key)
                continue
            if key in destination_files:
                source_record = source["files"][key]
                destination_record = destination_files[key]
                identical = (
                    source_record.get("size") == destination_record.get("size")
                    and source_record.get("sha256") == destination_record.get("sha256")
                )
                if identical and not _audio_relative(key):
                    duplicates.append(key)
                    selected.append(key)
                    continue
            conflicts.append(key)

    walk()
    return {
        "units": units,
        "duplicates": sorted(duplicates),
        "selected_files": sorted(set(selected)),
        "conflicts": sorted(conflicts),
    }


def _relative_parent(root_fd, relative, *, create=False):
    parts = _valid_relative(relative, allow_root=True)
    chain = _open_relative_directories(root_fd, parts[:-1], create=create)
    return (chain[-1] if chain else root_fd), (parts[-1] if parts else None), chain


def _open_relative_directory(root_fd, relative) -> tuple[int, _DescriptorOwner]:
    parts = _valid_relative(relative, allow_root=True)
    if not parts:
        chain = _DescriptorOwner()
        return chain.open(lambda: os.dup(root_fd)), chain
    chain = _open_relative_directories(root_fd, parts)
    return chain[-1], chain


def _open_relative_file(root_fd, relative) -> tuple[int, _DescriptorOwner]:
    parent_fd, name, chain = _relative_parent(root_fd, relative)
    try:
        descriptor = chain.open(lambda: _open_regular(name, dir_fd=parent_fd))
        if not _named_matches(parent_fd, name, descriptor):
            raise OSError("relocation file changed while it was opened")
        return descriptor, chain
    except BaseException:
        _close_all(chain)
        raise


def _copy_file(
    source_fd,
    destination_parent_fd,
    destination_name,
    expected,
    *,
    container=None,
) -> dict:
    if not _record_matches_fd(source_fd, expected):
        raise OSError("relocation source changed before it was copied")
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if container is None:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(
        destination_name, flags, 0o600, dir_fd=destination_parent_fd
    )
    try:
        if (
            not _named_matches(destination_parent_fd, destination_name, descriptor)
            or container is not None
            and not _directory_record_equivalent(
                _stat_record(os.fstat(descriptor)), container
            )
        ):
            raise OSError("relocation copy container changed")
        os.ftruncate(descriptor, 0)
        offset = 0
        while offset < expected["size"]:
            block = os.pread(
                source_fd,
                min(_COPY_BLOCK_SIZE, expected["size"] - offset),
                offset,
            )
            if not block:
                raise OSError("relocation source ended while it was copied")
            written_offset = 0
            while written_offset < len(block):
                written = os.write(descriptor, block[written_offset:])
                if written <= 0:
                    raise OSError("relocation copy made no progress")
                written_offset += written
            offset += len(block)
        if os.pread(source_fd, 1, offset):
            raise OSError("relocation source grew while it was copied")
        os.fchmod(descriptor, expected["mode"])
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    os.utime(
        destination_name,
        ns=(expected["modified_ns"], expected["modified_ns"]),
        dir_fd=destination_parent_fd,
        follow_symlinks=False,
    )
    descriptor = _open_regular(destination_name, dir_fd=destination_parent_fd)
    try:
        if not _named_matches(destination_parent_fd, destination_name, descriptor):
            raise OSError("relocation copy changed before it was sealed")
        copied = _file_record(descriptor)
        if (
            copied["size"] != expected["size"]
            or copied["sha256"] != expected["sha256"]
            or copied["mode"] != expected["mode"]
            or copied["modified_ns"] != expected["modified_ns"]
            or not _record_matches_fd(source_fd, expected)
        ):
            raise OSError("relocation copy did not match its source")
        # ``utime`` happened after the data fsync above.  Flush that final
        # inode-metadata change too before the journal may call this copy
        # durable.
        os.fsync(descriptor)
        return copied
    finally:
        os.close(descriptor)


def _immediate_children(snapshot, relative, field):
    prefix = f"{relative}/" if relative else ""
    return {
        key[len(prefix):]: value
        for key, value in snapshot[field].items()
        if key.startswith(prefix) and "/" not in key[len(prefix):]
    }


def _copy_tree_contents(source_fd, destination_fd, snapshot, relative="") -> None:
    directories = _immediate_children(snapshot, relative, "directories")
    files = _immediate_children(snapshot, relative, "files")
    expected_names = set(directories) | set(files)
    if set(os.listdir(source_fd)) != expected_names:
        raise OSError("relocation source changed during tree copy")
    for name, expected in sorted(files.items()):
        child = _open_regular(name, dir_fd=source_fd)
        try:
            if not _named_matches(source_fd, name, child):
                raise OSError("relocation source file changed during tree copy")
            _copy_file(child, destination_fd, name, expected)
        finally:
            os.close(child)
    for name, expected in sorted(directories.items()):
        source_child = _open_directory(name, dir_fd=source_fd)
        destination_child = None
        try:
            if not _named_matches(source_fd, name, source_child, directory=True):
                raise OSError("relocation source directory changed during tree copy")
            os.mkdir(name, 0o700, dir_fd=destination_fd)
            destination_child = _open_directory(name, dir_fd=destination_fd)
            if not _named_matches(
                destination_fd, name, destination_child, directory=True
            ):
                raise OSError("relocation copy directory changed during creation")
            child_relative = f"{relative}/{name}" if relative else name
            _copy_tree_contents(
                source_child, destination_child, snapshot, child_relative
            )
            os.fchmod(destination_child, expected["mode"])
            os.fsync(destination_child)
            os.utime(
                name,
                ns=(expected["modified_ns"], expected["modified_ns"]),
                dir_fd=destination_fd,
                follow_symlinks=False,
            )
            # The timestamp update follows the earlier directory fsync.
            os.fsync(destination_child)
        finally:
            if destination_child is not None:
                os.close(destination_child)
            os.close(source_child)
    os.fsync(destination_fd)


def _workspace_name(operation_id) -> str:
    if not _valid_operation_id(operation_id):
        raise ValueError("relocation operation ID is invalid")
    return f"{_WORKSPACE_PREFIX}{operation_id}"


def _create_workspace(root_fd, operation_id) -> tuple[str, int, dict]:
    name = _workspace_name(operation_id)
    os.mkdir(name, 0o700, dir_fd=root_fd)
    descriptor = _open_directory(name, dir_fd=root_fd)
    try:
        if not _named_matches(root_fd, name, descriptor, directory=True):
            raise OSError("relocation workspace changed during creation")
        os.fsync(descriptor)
        os.fsync(root_fd)
        record = _stat_record(os.fstat(descriptor))
        return name, descriptor, record
    except BaseException:
        os.close(descriptor)
        raise


def _open_workspace(root_fd, operation_id, expected) -> int:
    name = _workspace_name(operation_id)
    descriptor = _open_directory(name, dir_fd=root_fd)
    try:
        if (
            not _named_matches(root_fd, name, descriptor, directory=True)
            or not _directory_record_equivalent(
                _stat_record(os.fstat(descriptor)), expected
            )
        ):
            raise OSError("relocation workspace identity changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _unit_name(index) -> str:
    if type(index) is not int or index < 0 or index >= _MAX_TREE_ENTRIES:
        raise ValueError("relocation unit index is invalid")
    return f"unit-{index:05d}"


def _retired_source_file_name(index) -> str:
    if type(index) is not int or index < 0 or index >= _MAX_TREE_ENTRIES:
        raise ValueError("relocation source file index is invalid")
    return f"source-file-{index:05d}"


def _retired_source_directory_name(index) -> str:
    if type(index) is not int or index < 0 or index > _MAX_TREE_ENTRIES:
        raise ValueError("relocation source directory index is invalid")
    return f"source-directory-{index:05d}"


def _restore_private_entry(
    private_fd,
    private_name,
    public_fd,
    public_name,
    descriptor,
) -> bool:
    if (
        _named_entry_matches(public_fd, public_name, descriptor)
        and _name_missing(private_fd, private_name)
    ):
        return True
    if (
        not _name_missing(public_fd, public_name)
        or not _named_entry_matches(private_fd, private_name, descriptor)
    ):
        return False
    try:
        _rename_noreplace(
            private_fd,
            private_name,
            public_fd,
            public_name,
        )
    except BaseException:
        pass
    restored = (
        _named_entry_matches(public_fd, public_name, descriptor)
        and _name_missing(private_fd, private_name)
    )
    if restored:
        os.fsync(private_fd)
        os.fsync(public_fd)
    return restored


def _move_exact_to_private(
    public_fd,
    public_name,
    private_fd,
    private_name,
    descriptor,
) -> bool:
    if (
        not _named_entry_matches(public_fd, public_name, descriptor)
        or not _name_missing(private_fd, private_name)
    ):
        return False
    deferred = None
    try:
        _rename_noreplace(
            public_fd,
            public_name,
            private_fd,
            private_name,
        )
    except BaseException as exc:
        deferred = exc

    if _named_entry_matches(private_fd, private_name, descriptor):
        try:
            os.fsync(public_fd)
            os.fsync(private_fd)
        except BaseException as exc:
            deferred = deferred or exc
        if deferred is None:
            return True
        if not _restore_private_entry(
            private_fd,
            private_name,
            public_fd,
            public_name,
            descriptor,
        ):
            raise PostImportRelocationAttention(
                "an interrupted source retirement could not be restored"
            ) from deferred
        if isinstance(deferred, Exception):
            return False
        raise deferred

    if _name_missing(public_fd, public_name):
        unexpected = None
        try:
            unexpected = _open_entry_handle(private_name, dir_fd=private_fd)
            if not _named_entry_matches(private_fd, private_name, unexpected):
                raise OSError("relocation source replacement changed")
            if _restore_private_entry(
                private_fd,
                private_name,
                public_fd,
                public_name,
                unexpected,
            ):
                if deferred is not None and not isinstance(deferred, Exception):
                    raise deferred
                return False
            raise PostImportRelocationAttention(
                "a source replacement could not be restored after retirement"
            )
        finally:
            if unexpected is not None:
                os.close(unexpected)
    if deferred is not None and not isinstance(deferred, Exception):
        raise deferred
    return False


def _prepare_units(source_fd, workspace_fd, payload, namespace_fd) -> None:
    for unit in payload["plan"]["units"]:
        index = unit["index"]
        name = _unit_name(index)
        payload["copying_index"] = index
        _write_journal(namespace_fd, payload)
        source_relative = unit["relative"]
        if unit["kind"] == "tree":
            os.mkdir(name, 0o700, dir_fd=workspace_fd)
            destination = _open_directory(name, dir_fd=workspace_fd)
            try:
                if not _named_matches(workspace_fd, name, destination, directory=True):
                    raise OSError("relocation unit changed during creation")
                unit["container"] = _stat_record(os.fstat(destination))
                _write_journal(namespace_fd, payload)
                _checkpoint("after-copy-container")
                source, chain = _open_relative_directory(source_fd, source_relative)
                try:
                    expected = unit["source_record"]
                    if not _snapshot_matches(source, expected):
                        raise OSError("relocation source tree changed before copy")
                    _copy_tree_contents(source, destination, expected)
                    os.fchmod(destination, expected["root"]["mode"])
                    os.fsync(destination)
                    if not _snapshot_matches(source, expected):
                        raise OSError("relocation source tree changed during copy")
                finally:
                    _close_all(chain)
                prepared = _snapshot_tree(destination)
            finally:
                os.close(destination)
        elif unit["kind"] == "file":
            descriptor = os.open(
                name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=workspace_fd,
            )
            try:
                unit["container"] = _stat_record(os.fstat(descriptor))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(workspace_fd)
            _write_journal(namespace_fd, payload)
            _checkpoint("after-copy-container")
            source, chain = _open_relative_file(source_fd, source_relative)
            try:
                prepared = _copy_file(
                    source,
                    workspace_fd,
                    name,
                    unit["source_record"],
                    container=unit["container"],
                )
            finally:
                _close_all(chain)
        else:
            raise ValueError("relocation unit type is invalid")
        _checkpoint("after-copy-data")
        os.fsync(workspace_fd)
        unit["prepared_record"] = prepared
        payload["copying_index"] = None
        _write_journal(namespace_fd, payload)


def _file_record_equivalent(left, right, *, renamed=False) -> bool:
    fields = ("device", "inode", "size", "modified_ns", "mode", "sha256")
    if not all(left.get(field) == right.get(field) for field in fields):
        return False
    return renamed or left.get("changed_ns") == right.get("changed_ns")


def _unit_matches(parent_fd, name, unit, *, renamed=False) -> bool:
    try:
        if unit["kind"] == "tree":
            descriptor = _open_directory(name, dir_fd=parent_fd)
            try:
                if not _named_matches(parent_fd, name, descriptor, directory=True):
                    return False
                return _snapshot_matches(
                    descriptor, unit["prepared_record"], renamed_root=renamed
                )
            finally:
                os.close(descriptor)
        descriptor = _open_regular(name, dir_fd=parent_fd)
        try:
            if not _named_matches(parent_fd, name, descriptor):
                return False
            return _file_record_equivalent(
                _file_record(descriptor), unit["prepared_record"], renamed=renamed
            )
        finally:
            os.close(descriptor)
    except (OSError, KeyError, TypeError, ValueError):
        return False


def _expected_destination_directory(payload, relative):
    initial = payload.get("destination_snapshot")
    if isinstance(initial, dict):
        if not relative:
            return initial.get("root")
        expected = initial.get("directories", {}).get(relative)
        if isinstance(expected, dict):
            return expected
    for unit in payload.get("plan", {}).get("units", ()):
        if unit.get("kind") != "tree" or not isinstance(
            unit.get("prepared_record"), dict
        ):
            continue
        unit_relative = unit.get("relative")
        if relative == unit_relative:
            return unit["prepared_record"].get("root")
        prefix = f"{unit_relative}/" if unit_relative else ""
        if relative.startswith(prefix):
            nested = relative[len(prefix) :]
            expected = unit["prepared_record"].get("directories", {}).get(nested)
            if isinstance(expected, dict):
                return expected
    return None


def _destination_chain_matches(root_fd, payload, parts, chain) -> bool:
    try:
        destination_parts = _valid_relative(payload["destination"], album=True)
        if (
            len(parts) != len(chain)
            or not parts
            or tuple(parts[:2]) != destination_parts[: len(parts[:2])]
        ):
            return False
        for index, (name, descriptor) in enumerate(zip(parts, chain, strict=True)):
            parent_fd = root_fd if index == 0 else chain[index - 1]
            if not _named_matches(parent_fd, name, descriptor, directory=True):
                return False
            expected = (
                payload.get("destination_artist_record")
                if index == 0
                else _expected_destination_directory(
                    payload,
                    (
                        PurePosixPath(*parts[2 : index + 1]).as_posix()
                        if parts[2 : index + 1]
                        else ""
                    ),
                )
            )
            if not isinstance(expected, dict) or not _directory_record_equivalent(
                _stat_record(os.fstat(descriptor)), expected
            ):
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _open_destination_parent(root_fd, payload, unit_relative):
    destination_parts = _valid_relative(payload["destination"], album=True)
    if unit_relative:
        unit_parts = _valid_relative(unit_relative)
        parent_parts = destination_parts + unit_parts[:-1]
        name = unit_parts[-1]
    else:
        parent_parts = destination_parts[:-1]
        name = destination_parts[-1]
    chain = _open_relative_directories(root_fd, parent_parts)
    if not _destination_chain_matches(root_fd, payload, parent_parts, chain):
        _close_all(chain)
        raise OSError("relocation destination ancestry changed")
    return chain[-1], name, chain


def _ensure_destination_artist(root_fd, payload, namespace_fd) -> None:
    artist = _valid_relative(payload["destination"], album=True)[0]
    expected = payload.get("destination_artist_record")
    try:
        descriptor = _open_directory(artist, dir_fd=root_fd)
    except FileNotFoundError:
        if expected is not None:
            raise OSError("relocation destination artist disappeared") from None
        os.mkdir(artist, 0o755, dir_fd=root_fd)
        os.fsync(root_fd)
        descriptor = _open_directory(artist, dir_fd=root_fd)
        if not _named_matches(root_fd, artist, descriptor, directory=True):
            os.close(descriptor)
            raise OSError("relocation destination artist changed during creation")
        expected = _stat_record(os.fstat(descriptor))
        payload["destination_artist_record"] = expected
        payload["created_artist"] = expected
        _write_journal(namespace_fd, payload)
    try:
        if (
            not isinstance(expected, dict)
            or not _named_matches(root_fd, artist, descriptor, directory=True)
            or not _directory_record_equivalent(
                _stat_record(os.fstat(descriptor)), expected
            )
        ):
            raise OSError("relocation destination artist changed")
    finally:
        os.close(descriptor)


def _unit_layout(
    root_fd, workspace_fd, payload, unit
) -> tuple[str, int, str, _DescriptorOwner]:
    private_name = _unit_name(unit["index"])
    destination_parent, destination_name, chain = _open_destination_parent(
        root_fd, payload, unit["relative"]
    )
    private = _unit_matches(workspace_fd, private_name, unit)
    public = _unit_matches(
        destination_parent, destination_name, unit, renamed=True
    )
    private_missing = _name_missing(workspace_fd, private_name)
    public_missing = _name_missing(destination_parent, destination_name)
    if private and public_missing:
        state = "private"
    elif public and private_missing:
        state = "public"
    else:
        state = "unknown"
    return state, destination_parent, destination_name, chain


def _publish_units(root_fd, workspace_fd, payload) -> None:
    for unit in payload["plan"]["units"]:
        state, destination_parent, destination_name, chain = _unit_layout(
            root_fd, workspace_fd, payload, unit
        )
        try:
            if state != "private":
                raise OSError("relocation publication layout is ambiguous")
            private_name = _unit_name(unit["index"])
            try:
                _rename_noreplace(
                    workspace_fd,
                    private_name,
                    destination_parent,
                    destination_name,
                )
            except BaseException:
                state_after, _, _, nested_chain = _unit_layout(
                    root_fd, workspace_fd, payload, unit
                )
                _close_all(nested_chain)
                if state_after != "public":
                    raise
            os.fsync(workspace_fd)
            os.fsync(destination_parent)
            state_after, _, _, nested_chain = _unit_layout(
                root_fd, workspace_fd, payload, unit
            )
            _close_all(nested_chain)
            if state_after != "public":
                raise OSError("relocation publication could not be proved")
        finally:
            _close_all(chain)


def _all_unit_states(root_fd, workspace_fd, payload) -> list[str]:
    states = []
    for unit in payload["plan"]["units"]:
        try:
            state, _, _, chain = _unit_layout(root_fd, workspace_fd, payload, unit)
        except OSError:
            states.append("unknown")
            continue
        _close_all(chain)
        states.append(state)
    return states


def _rollback_units(root_fd, workspace_fd, payload) -> None:
    for unit in reversed(payload["plan"]["units"]):
        state, destination_parent, destination_name, chain = _unit_layout(
            root_fd, workspace_fd, payload, unit
        )
        try:
            if state == "private":
                continue
            if state != "public":
                raise PostImportRelocationAttention(
                    "a published relocation entry changed before recovery",
                    paths=(
                        Path(cfg.MUSIC_ROOT) / payload["destination"] / unit["relative"],
                    ),
                )
            private_name = _unit_name(unit["index"])
            try:
                _rename_noreplace(
                    destination_parent,
                    destination_name,
                    workspace_fd,
                    private_name,
                )
            except BaseException:
                state_after, _, _, nested_chain = _unit_layout(
                    root_fd, workspace_fd, payload, unit
                )
                _close_all(nested_chain)
                if state_after != "private":
                    raise
            os.fsync(destination_parent)
            os.fsync(workspace_fd)
        finally:
            _close_all(chain)


def _delete_tree_contents(directory_fd, snapshot) -> None:
    if not _snapshot_matches(directory_fd, snapshot, renamed_root=True):
        raise OSError("private relocation tree changed before cleanup")
    for relative in sorted(snapshot["files"], key=lambda value: value.count("/"), reverse=True):
        parent_fd, name, chain = _relative_parent(directory_fd, relative)
        descriptor = None
        try:
            descriptor = _open_regular(name, dir_fd=parent_fd)
            if (
                not _named_matches(parent_fd, name, descriptor)
                or not _file_record_equivalent(
                    _file_record(descriptor), snapshot["files"][relative], renamed=True
                )
            ):
                raise OSError("private relocation file changed before cleanup")
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            _close_all(chain)
    for relative in sorted(
        snapshot["directories"], key=lambda value: value.count("/"), reverse=True
    ):
        parent_fd, name, chain = _relative_parent(directory_fd, relative)
        child = None
        try:
            child = _open_directory(name, dir_fd=parent_fd)
            expected = snapshot["directories"][relative]
            if (
                not _named_matches(parent_fd, name, child, directory=True)
                or not _directory_record_equivalent(
                    _stat_record(os.fstat(child)), expected
                )
                or os.listdir(child)
            ):
                raise OSError("private relocation directory changed before cleanup")
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            if child is not None:
                os.close(child)
            _close_all(chain)


def _remove_unit(workspace_fd, unit) -> None:
    name = _unit_name(unit["index"])
    if unit["kind"] == "file":
        descriptor = _open_regular(name, dir_fd=workspace_fd)
        try:
            if (
                not _named_matches(workspace_fd, name, descriptor)
                or not _file_record_equivalent(
                    _file_record(descriptor), unit["prepared_record"], renamed=True
                )
            ):
                raise OSError("private relocation file changed before cleanup")
            os.unlink(name, dir_fd=workspace_fd)
            os.fsync(workspace_fd)
        finally:
            os.close(descriptor)
        return
    descriptor = _open_directory(name, dir_fd=workspace_fd)
    try:
        if not _named_matches(workspace_fd, name, descriptor, directory=True):
            raise OSError("private relocation tree changed before cleanup")
        _delete_tree_contents(descriptor, unit["prepared_record"])
        if os.listdir(descriptor):
            raise OSError("private relocation tree is not empty after cleanup")
        os.rmdir(name, dir_fd=workspace_fd)
        os.fsync(workspace_fd)
    finally:
        os.close(descriptor)


def _remove_workspace(root_fd, workspace_fd, payload) -> None:
    for unit in payload["plan"]["units"]:
        name = _unit_name(unit["index"])
        if _name_missing(workspace_fd, name):
            continue
        if "prepared_record" not in unit:
            container = unit.get("container")
            if container is None:
                raise OSError("unfinished relocation copy has no identity proof")
            value = os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)
            if not _same_node_record(_stat_record(value), container):
                raise OSError("unfinished relocation copy changed")
            if stat.S_ISDIR(value.st_mode):
                descriptor = _open_directory(name, dir_fd=workspace_fd)
                try:
                    _remove_incomplete_tree(descriptor)
                    if os.listdir(descriptor):
                        raise OSError("unfinished relocation tree is not empty")
                finally:
                    os.close(descriptor)
                os.rmdir(name, dir_fd=workspace_fd)
            elif stat.S_ISREG(value.st_mode):
                os.unlink(name, dir_fd=workspace_fd)
            else:
                raise OSError("unfinished relocation copy cannot be reclaimed safely")
            os.fsync(workspace_fd)
            continue
        _remove_unit(workspace_fd, unit)
    if os.listdir(workspace_fd):
        raise OSError("relocation workspace contains an unexpected entry")
    name = _workspace_name(payload["operation_id"])
    if not _named_matches(root_fd, name, workspace_fd, directory=True):
        raise OSError("relocation workspace changed before removal")
    os.rmdir(name, dir_fd=root_fd)
    os.fsync(root_fd)


def _remove_incomplete_tree(directory_fd, *, depth=0) -> None:
    """Reclaim only regular entries inside a positively identified private copy."""
    if depth > _MAX_TREE_DEPTH:
        raise OSError("unfinished relocation tree is nested too deeply")
    for name in sorted(os.listdir(directory_fd)):
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(value.st_mode):
            descriptor = _open_regular(name, dir_fd=directory_fd)
            try:
                if not _named_matches(directory_fd, name, descriptor):
                    raise OSError("unfinished relocation file changed")
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(descriptor)
        elif stat.S_ISDIR(value.st_mode):
            descriptor = _open_directory(name, dir_fd=directory_fd)
            try:
                if not _named_matches(directory_fd, name, descriptor, directory=True):
                    raise OSError("unfinished relocation directory changed")
                _remove_incomplete_tree(descriptor, depth=depth + 1)
                if os.listdir(descriptor):
                    raise OSError("unfinished relocation directory is not empty")
                os.rmdir(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(descriptor)
        else:
            raise OSError("unfinished relocation copy contains a link or special entry")


def _remove_created_artist_if_empty(root_fd, payload) -> None:
    expected = payload.get("created_artist")
    if not isinstance(expected, dict):
        return
    artist = _valid_relative(payload["destination"], album=True)[0]
    try:
        descriptor = _open_directory(artist, dir_fd=root_fd)
    except FileNotFoundError:
        return
    try:
        if (
            _named_matches(root_fd, artist, descriptor, directory=True)
            and _directory_record_equivalent(_stat_record(os.fstat(descriptor)), expected)
            and not os.listdir(descriptor)
        ):
            os.rmdir(artist, dir_fd=root_fd)
            os.fsync(root_fd)
    finally:
        os.close(descriptor)


def _path_mapping(root: Path, payload) -> dict[str, str]:
    source = root / payload["source"]
    destination = root / payload["destination"]
    return {
        os.fspath(source / relative): os.fspath(destination / relative)
        for relative in payload["plan"]["selected_files"]
    }


def _capture_database(anchor, root, payload, authority_matches) -> dict:
    if anchor is None or anchor.get("descriptor") is None:
        raise OSError("an existing Beets database is required for album relocation")
    mapping = _path_mapping(root, payload)
    required = [
        source for source in mapping if _audio_relative(Path(source).name)
    ]
    timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
    timeout = float(timeout) if timeout and timeout > 0 else 5.0
    return inspect_sqlite_source(
        anchor,
        _migration_database_anchor_matches,
        lambda connection: capture_relocation_rows(
            connection,
            mapping,
            music_root=root,
            required_item_sources=required,
        ),
        connect=sqlite3.connect,
        timeout=timeout,
        anchors_match=authority_matches,
    )


def _database_state(anchor, capture, authority_matches=None):
    if anchor is None or anchor.get("descriptor") is None:
        raise OSError("the Beets database needed for relocation recovery is missing")
    timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
    timeout = float(timeout) if timeout and timeout > 0 else 5.0
    return inspect_sqlite_source(
        anchor,
        _migration_database_anchor_matches,
        lambda connection: classify_relocation_rows(connection, capture),
        connect=sqlite3.connect,
        timeout=timeout,
        anchors_match=authority_matches,
    )


def _location(relative, record) -> dict:
    return {
        "relative": PurePosixPath(relative).as_posix(),
        **{
            key: record[key]
            for key in (
                "device",
                "inode",
                "size",
                "modified_ns",
                "changed_ns",
            )
        },
    }


def _tree_unit_directories(unit) -> list[str]:
    if unit["kind"] != "tree":
        return []
    relative = unit["relative"]
    nested = unit["prepared_record"]["directories"]
    values = [relative]
    values.extend(
        f"{relative}/{child}" if relative else child for child in nested
    )
    return values


def _build_ownership_receipt(root_fd, payload) -> dict:
    source_base = payload["source"]
    destination_base = payload["destination"]
    source = payload["source_snapshot"]
    public = payload["public_snapshot"]
    duplicates = set(payload["plan"]["duplicates"])
    file_changes = []
    released_files = []
    for relative in payload["plan"]["selected_files"]:
        before = _location(f"{source_base}/{relative}", source["files"][relative])
        if relative in duplicates:
            released_files.append(before)
        else:
            file_changes.append({
                "before": before,
                "after": _location(
                    f"{destination_base}/{relative}", public["files"][relative]
                ),
            })

    created_relatives = set()
    directory_changes = []
    for unit in payload["plan"]["units"]:
        for relative in _tree_unit_directories(unit):
            source_record = source["root"] if not relative else source["directories"][relative]
            destination_record = (
                public["root"] if not relative else public["directories"][relative]
            )
            destination_relative = (
                destination_base if not relative else f"{destination_base}/{relative}"
            )
            directory_changes.append({
                "before": _location(
                    source_base if not relative else f"{source_base}/{relative}",
                    source_record,
                ),
                "after": _location(destination_relative, destination_record),
            })
            created_relatives.add(relative)

    created_directories = []
    created_artist = payload.get("created_artist")
    if isinstance(created_artist, dict):
        artist = _valid_relative(destination_base, album=True)[0]
        descriptor = _open_directory(artist, dir_fd=root_fd)
        try:
            if not _named_matches(root_fd, artist, descriptor, directory=True):
                raise OSError("created destination artist changed")
            created_directories.append(
                _location(artist, _stat_record(os.fstat(descriptor)))
            )
        finally:
            os.close(descriptor)
    for relative in sorted(created_relatives, key=lambda value: (value.count("/"), value)):
        record = public["root"] if not relative else public["directories"][relative]
        path = destination_base if not relative else f"{destination_base}/{relative}"
        created_directories.append(_location(path, record))
    return {
        "version": 2,
        "source": os.fspath(Path(cfg.MUSIC_ROOT) / source_base),
        "destination": os.fspath(Path(cfg.MUSIC_ROOT) / destination_base),
        "file_changes": file_changes,
        "directory_changes": directory_changes,
        "created_directories": created_directories,
        "released_files": released_files,
    }


def _destination_snapshot(root_fd, payload) -> dict:
    destination_relative = payload["destination"]
    descriptor, chain = _open_relative_directory(root_fd, destination_relative)
    try:
        parts = _valid_relative(destination_relative, album=True)
        if not _destination_chain_matches(root_fd, payload, parts, chain):
            raise OSError("relocation destination ancestry changed")
        return _snapshot_tree(descriptor)
    finally:
        _close_all(chain)


def _source_snapshot_matches(root_fd, payload) -> bool:
    try:
        descriptor, chain = _open_relative_directory(root_fd, payload["source"])
    except OSError:
        return False
    try:
        return _snapshot_matches(descriptor, payload["source_snapshot"])
    finally:
        _close_all(chain)


def _public_snapshot_matches(root_fd, payload) -> bool:
    expected = payload.get("public_snapshot")
    if not isinstance(expected, dict):
        return False
    try:
        descriptor, chain = _open_relative_directory(root_fd, payload["destination"])
    except OSError:
        return False
    try:
        parts = _valid_relative(payload["destination"], album=True)
        return _destination_chain_matches(
            root_fd, payload, parts, chain
        ) and _snapshot_matches(descriptor, expected)
    finally:
        _close_all(chain)


def _journal_matches(namespace_fd, payload) -> bool:
    try:
        return _read_journal(namespace_fd, payload["operation_id"]) == payload
    except (OSError, TypeError, ValueError):
        return False


def _publication_authority_matches(
    authority, root_fd, namespace_fd, database_anchor, payload
) -> bool:
    try:
        if (
            type(authority) is not RunLockLease
            or authority.intact() is not True
            or not _migration_database_anchor_matches(database_anchor)
            or not _journal_matches(namespace_fd, payload)
            or not _source_snapshot_matches(root_fd, payload)
            or not _public_snapshot_matches(root_fd, payload)
        ):
            return False
        workspace_fd = _open_workspace(
            root_fd, payload["operation_id"], payload["workspace_record"]
        )
        try:
            return all(
                state == "public"
                for state in _all_unit_states(root_fd, workspace_fd, payload)
            )
        finally:
            os.close(workspace_fd)
    except (OSError, TypeError, ValueError):
        return False


def _exact_destination_file_matches(root_fd, payload, relative) -> bool:
    destination_relative = f"{payload['destination']}/{relative}"
    destination_fd = None
    destination_chain = []
    try:
        destination_fd, destination_chain = _open_relative_file(
            root_fd, destination_relative
        )
        destination_parent = (
            destination_chain[-1] if destination_chain else root_fd
        )
        destination_name = _valid_relative(destination_relative)[-1]
        expected = payload["public_snapshot"]["files"][relative]
        return _named_matches(
            destination_parent, destination_name, destination_fd
        ) and _record_matches_fd(destination_fd, expected)
    except (OSError, KeyError, TypeError, ValueError):
        return False
    finally:
        _close_all(destination_chain)


def _unlink_exact_source_file(
    root_fd,
    workspace_fd,
    payload,
    relative,
    index,
) -> bool:
    source_relative = f"{payload['source']}/{relative}"
    destination_relative = f"{payload['destination']}/{relative}"
    retired_name = _retired_source_file_name(index)
    source_fd = destination_fd = retired_fd = None
    source_chain = destination_chain = []
    source_exclusion = destination_exclusion = None
    expected_source = payload["source_snapshot"]["files"][relative]
    expected_destination = payload["public_snapshot"]["files"][relative]

    if workspace_fd is not None and not _name_missing(workspace_fd, retired_name):
        try:
            retired_fd = _open_regular(retired_name, dir_fd=workspace_fd)
            destination_fd, destination_chain = _open_relative_file(
                root_fd, destination_relative
            )
            destination_parent = (
                destination_chain[-1] if destination_chain else root_fd
            )
            destination_name = _valid_relative(destination_relative)[-1]
            source_exclusion = acquire_inode_write_exclusion(retired_fd)
            destination_exclusion = acquire_inode_write_exclusion(destination_fd)
            if source_exclusion is None or destination_exclusion is None:
                return False
            if (
                not source_exclusion.intact()
                or not destination_exclusion.intact()
                or not _named_matches(workspace_fd, retired_name, retired_fd)
                or not _file_record_equivalent(
                    _file_record(retired_fd), expected_source, renamed=True
                )
                or not _named_matches(
                    destination_parent, destination_name, destination_fd
                )
                or not _record_matches_fd(destination_fd, expected_destination)
            ):
                return False
            os.unlink(retired_name, dir_fd=workspace_fd)
            os.fsync(workspace_fd)
            if (
                not destination_exclusion.intact()
                or not _named_matches(
                    destination_parent, destination_name, destination_fd
                )
                or not _record_matches_fd(destination_fd, expected_destination)
            ):
                raise PostImportRelocationAttention(
                    "relocation destination changed during source cleanup",
                    paths=(Path(cfg.MUSIC_ROOT) / destination_relative,),
                )
            return True
        except PostImportRelocationAttention:
            raise
        except (OSError, TypeError, ValueError):
            return False
        finally:
            if destination_exclusion is not None:
                destination_exclusion.close()
            if source_exclusion is not None:
                source_exclusion.close()
            if retired_fd is not None:
                os.close(retired_fd)
            _close_all(destination_chain)

    try:
        source_fd, source_chain = _open_relative_file(root_fd, source_relative)
    except FileNotFoundError:
        return _exact_destination_file_matches(root_fd, payload, relative)
    if workspace_fd is None:
        _close_all(source_chain)
        return False
    try:
        destination_fd, destination_chain = _open_relative_file(
            root_fd, destination_relative
        )
        source_parent = source_chain[-1] if source_chain else root_fd
        destination_parent = (
            destination_chain[-1] if destination_chain else root_fd
        )
        source_name = _valid_relative(source_relative)[-1]
        destination_name = _valid_relative(destination_relative)[-1]
        if (
            (
                _entry_identity(os.fstat(source_parent))
                == _entry_identity(os.fstat(destination_parent))
                and source_name == destination_name
            )
            or not _named_matches(
                destination_parent, destination_name, destination_fd
            )
            or not _record_matches_fd(source_fd, expected_source)
            or not _record_matches_fd(destination_fd, expected_destination)
        ):
            return False
        source_exclusion = acquire_inode_write_exclusion(source_fd)
        destination_exclusion = acquire_inode_write_exclusion(destination_fd)
        if source_exclusion is None or destination_exclusion is None:
            return False
        if (
            not source_exclusion.intact()
            or not destination_exclusion.intact()
            or not _named_matches(source_parent, source_name, source_fd)
            or not _named_matches(
                destination_parent, destination_name, destination_fd
            )
            or not _record_matches_fd(source_fd, expected_source)
            or not _record_matches_fd(destination_fd, expected_destination)
        ):
            return False
        if not _move_exact_to_private(
            source_parent,
            source_name,
            workspace_fd,
            retired_name,
            source_fd,
        ):
            return False
        if (
            not source_exclusion.intact()
            or not destination_exclusion.intact()
            or not _named_matches(workspace_fd, retired_name, source_fd)
            or not _file_record_equivalent(
                _file_record(source_fd), expected_source, renamed=True
            )
            or not _named_matches(
                destination_parent, destination_name, destination_fd
            )
            or not _record_matches_fd(destination_fd, expected_destination)
        ):
            restored = _restore_private_entry(
                workspace_fd,
                retired_name,
                source_parent,
                source_name,
                source_fd,
            )
            raise PostImportRelocationAttention(
                (
                    "relocation destination changed during source cleanup"
                    if restored
                    else "retired relocation source requires recovery"
                ),
                paths=(
                    Path(cfg.MUSIC_ROOT) / source_relative,
                    Path(cfg.MUSIC_ROOT) / destination_relative,
                ),
            )
        os.unlink(retired_name, dir_fd=workspace_fd)
        os.fsync(workspace_fd)
        if (
            not destination_exclusion.intact()
            or not _named_matches(
                destination_parent, destination_name, destination_fd
            )
            or not _record_matches_fd(destination_fd, expected_destination)
        ):
            raise PostImportRelocationAttention(
                "relocation destination changed during source cleanup",
                paths=(Path(cfg.MUSIC_ROOT) / destination_relative,),
            )
        return True
    except PostImportRelocationAttention:
        raise
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if destination_exclusion is not None:
            destination_exclusion.close()
        if source_exclusion is not None:
            source_exclusion.close()
        _close_all(destination_chain)
        _close_all(source_chain)


def _source_directory_should_be_empty(payload, relative) -> bool:
    prefix = f"{relative}/" if relative else ""
    selected = set(payload["plan"]["selected_files"])
    remaining_files = set(payload["source_snapshot"]["files"]) - selected
    if any(path.startswith(prefix) for path in remaining_files):
        return False
    source_directories = set(payload["source_snapshot"]["directories"])
    conflict_directories = source_directories.intersection(
        payload["plan"]["conflicts"]
    )
    return not any(
        conflict == relative or conflict.startswith(prefix)
        or bool(relative) and relative.startswith(f"{conflict}/")
        for conflict in conflict_directories
    )


def _delete_retired_empty_source_directory(
    parent_fd,
    name,
    workspace_fd,
    retired_name,
    descriptor,
) -> bool:
    try:
        os.rmdir(retired_name, dir_fd=workspace_fd)
        os.fsync(workspace_fd)
        return True
    except BaseException as exc:
        if _restore_private_entry(
            workspace_fd,
            retired_name,
            parent_fd,
            name,
            descriptor,
        ):
            if isinstance(exc, Exception):
                return False
            raise exc
        raise PostImportRelocationAttention(
            "retired relocation source directory requires recovery"
        ) from exc


def _remove_exact_empty_source_directory(
    parent_fd,
    name,
    workspace_fd,
    retired_name,
    expected,
) -> bool:
    if workspace_fd is not None and not _name_missing(workspace_fd, retired_name):
        descriptor = _open_directory(retired_name, dir_fd=workspace_fd)
        try:
            if (
                not _named_matches(
                    workspace_fd, retired_name, descriptor, directory=True
                )
                or not _directory_record_equivalent(
                    _stat_record(os.fstat(descriptor)), expected
                )
                or os.listdir(descriptor)
            ):
                return False
            return _delete_retired_empty_source_directory(
                parent_fd,
                name,
                workspace_fd,
                retired_name,
                descriptor,
            )
        finally:
            os.close(descriptor)

    if _name_missing(parent_fd, name):
        return True
    if workspace_fd is None:
        return False
    descriptor = _open_directory(name, dir_fd=parent_fd)
    try:
        if (
            not _named_matches(parent_fd, name, descriptor, directory=True)
            or not _directory_record_equivalent(
                _stat_record(os.fstat(descriptor)), expected
            )
            or os.listdir(descriptor)
        ):
            return False
        if not _move_exact_to_private(
            parent_fd,
            name,
            workspace_fd,
            retired_name,
            descriptor,
        ):
            return False
        if (
            not _named_matches(
                workspace_fd, retired_name, descriptor, directory=True
            )
            or not _directory_record_equivalent(
                _stat_record(os.fstat(descriptor)), expected
            )
            or os.listdir(descriptor)
        ):
            raise PostImportRelocationAttention(
                "retired relocation source directory requires recovery"
            )
        return _delete_retired_empty_source_directory(
            parent_fd,
            name,
            workspace_fd,
            retired_name,
            descriptor,
        )
    finally:
        os.close(descriptor)


def _remove_empty_source_directories(
    root_fd, workspace_fd, payload
) -> tuple[Path, ...]:
    source_parts = _valid_relative(payload["source"], album=True)
    failures = []
    directories = sorted(
        payload["source_snapshot"]["directories"],
        key=lambda value: (value.count("/"), value),
        reverse=True,
    )
    directories.append("")
    for index, relative in enumerate(directories):
        if failures:
            break
        if not _source_directory_should_be_empty(payload, relative):
            continue
        absolute_relative = (
            payload["source"] if not relative else f"{payload['source']}/{relative}"
        )
        try:
            parent_fd, name, chain = _relative_parent(root_fd, absolute_relative)
        except FileNotFoundError:
            continue
        except OSError:
            failures.append(Path(cfg.MUSIC_ROOT) / absolute_relative)
            continue
        try:
            expected = (
                payload["source_snapshot"]["root"]
                if not relative
                else payload["source_snapshot"]["directories"][relative]
            )
            if not _remove_exact_empty_source_directory(
                parent_fd,
                name,
                workspace_fd,
                _retired_source_directory_name(index),
                expected,
            ):
                failures.append(Path(cfg.MUSIC_ROOT) / absolute_relative)
        except FileNotFoundError:
            pass
        except PostImportRelocationAttention:
            raise
        except OSError:
            failures.append(Path(cfg.MUSIC_ROOT) / absolute_relative)
        finally:
            _close_all(chain)
    if failures:
        return tuple(dict.fromkeys(failures))
    artist = source_parts[0]
    descriptor = None
    try:
        expected = payload.get("source_artist_record")
        retired_artist = workspace_fd is not None and not _name_missing(
            workspace_fd, "source-artist"
        )
        if retired_artist:
            if (
                not isinstance(expected, dict)
                or not _remove_exact_empty_source_directory(
                    root_fd,
                    artist,
                    workspace_fd,
                    "source-artist",
                    expected,
                )
            ):
                failures.append(Path(cfg.MUSIC_ROOT) / artist)
            return tuple(dict.fromkeys(failures))
        descriptor = _open_directory(artist, dir_fd=root_fd)
        matches = (
            isinstance(expected, dict)
            and _named_matches(root_fd, artist, descriptor, directory=True)
            and _directory_record_equivalent(_stat_record(os.fstat(descriptor)), expected)
        )
        if matches and not os.listdir(descriptor):
            os.close(descriptor)
            descriptor = None
            if not _remove_exact_empty_source_directory(
                root_fd,
                artist,
                workspace_fd,
                "source-artist",
                expected,
            ):
                failures.append(Path(cfg.MUSIC_ROOT) / artist)
        elif not matches:
            failures.append(Path(cfg.MUSIC_ROOT) / artist)
    except FileNotFoundError:
        pass
    except PostImportRelocationAttention:
        raise
    except OSError:
        failures.append(Path(cfg.MUSIC_ROOT) / artist)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return tuple(dict.fromkeys(failures))


def _cleanup_source(root_fd, workspace_fd, payload) -> None:
    if not _public_snapshot_matches(root_fd, payload):
        raise PostImportRelocationAttention(
            "relocated files changed before source cleanup",
            paths=(Path(cfg.MUSIC_ROOT) / payload["destination"],),
        )
    failed_files = []
    for index, relative in enumerate(payload["plan"]["selected_files"]):
        if not _unlink_exact_source_file(
            root_fd, workspace_fd, payload, relative, index
        ):
            failed_files.append(Path(cfg.MUSIC_ROOT) / payload["source"] / relative)
    if failed_files:
        raise PostImportRelocationAttention(
            "relocation source file cleanup could not be proved",
            paths=failed_files,
        )
    failed_directories = _remove_empty_source_directories(
        root_fd, workspace_fd, payload
    )
    if failed_directories:
        raise PostImportRelocationAttention(
            "relocation source directory cleanup could not be proved",
            paths=failed_directories,
        )
    if not _public_snapshot_matches(root_fd, payload):
        raise PostImportRelocationAttention(
            "relocation destination changed during source cleanup",
            paths=(Path(cfg.MUSIC_ROOT) / payload["destination"],),
        )


def _root_matches(root_fd, expected: Path) -> bool:
    try:
        configured = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        named = os.stat(expected, follow_symlinks=False)
        held = os.fstat(root_fd)
        return (
            configured == expected
            and stat.S_ISDIR(named.st_mode)
            and _entry_identity(named) == _entry_identity(held)
        )
    except (OSError, TypeError, ValueError):
        return False


def _initial_destination_matches(root_fd, payload) -> bool:
    expected = payload["destination_snapshot"]
    artist = _valid_relative(payload["destination"], album=True)[0]
    expected_artist = payload.get("destination_artist_record")
    try:
        artist_fd = _open_directory(artist, dir_fd=root_fd)
    except FileNotFoundError:
        if expected_artist is not None:
            return False
        artist_fd = None
    except OSError:
        return False
    if artist_fd is not None:
        try:
            if (
                not isinstance(expected_artist, dict)
                or not _named_matches(root_fd, artist, artist_fd, directory=True)
                or not _directory_record_equivalent(
                    _stat_record(os.fstat(artist_fd)), expected_artist
                )
            ):
                return False
        finally:
            os.close(artist_fd)
    try:
        descriptor, chain = _open_relative_directory(root_fd, payload["destination"])
    except FileNotFoundError:
        return expected is None
    except OSError:
        return False
    try:
        parts = _valid_relative(payload["destination"], album=True)
        return (
            expected is not None
            and _destination_chain_matches(root_fd, payload, parts, chain)
            and _snapshot_matches(descriptor, expected)
        )
    finally:
        _close_all(chain)


def _source_artist_matches(root_fd, payload) -> bool:
    artist = _valid_relative(payload["source"], album=True)[0]
    expected = payload.get("source_artist_record")
    try:
        descriptor = _open_directory(artist, dir_fd=root_fd)
    except OSError:
        return False
    try:
        return (
            isinstance(expected, dict)
            and _named_matches(root_fd, artist, descriptor, directory=True)
            and _directory_record_equivalent(
                _stat_record(os.fstat(descriptor)), expected
            )
        )
    finally:
        os.close(descriptor)


def capture_post_import_relocation_expectation(
    source, destination, *, authority: RunLockLease
) -> dict:
    """Capture exact source and destination state for a durable filing action."""
    _require_authority(authority)
    root, source_path, source_parts = _absolute_album_path(source)
    destination_root, destination_path, destination_parts = (
        _absolute_album_path(destination)
    )
    if root != destination_root or source_path == destination_path:
        raise ValueError("relocation source and destination are invalid")

    with _THREAD_LOCK:
        root_fd = source_fd = destination_fd = None
        root_owner = None
        source_chain = destination_chain = []
        try:
            root_fd, root_owner = _open_music_root(root)
            source_relative = PurePosixPath(*source_parts).as_posix()
            destination_relative = PurePosixPath(*destination_parts).as_posix()
            source_fd, source_chain = _open_relative_directory(
                root_fd, source_relative
            )
            source_snapshot = _snapshot_tree(source_fd)
            source_artist_record = _stat_record(os.fstat(source_chain[0]))
            try:
                destination_fd, destination_chain = _open_relative_directory(
                    root_fd, destination_relative
                )
            except FileNotFoundError:
                destination_fd = None
                destination_snapshot = None
                try:
                    destination_artist_fd = _open_directory(
                        destination_parts[0], dir_fd=root_fd
                    )
                except FileNotFoundError:
                    destination_artist_record = None
                else:
                    try:
                        if not _named_matches(
                            root_fd,
                            destination_parts[0],
                            destination_artist_fd,
                            directory=True,
                        ):
                            raise OSError(
                                "relocation destination artist changed "
                                "during expectation capture"
                            )
                        destination_artist_record = _stat_record(
                            os.fstat(destination_artist_fd)
                        )
                    finally:
                        os.close(destination_artist_fd)
            else:
                destination_snapshot = _snapshot_tree(destination_fd)
                destination_artist_record = _stat_record(
                    os.fstat(destination_chain[0])
                )

            probe = {
                "source": source_relative,
                "source_snapshot": source_snapshot,
                "destination": destination_relative,
                "destination_snapshot": destination_snapshot,
                "destination_artist_record": destination_artist_record,
            }
            if (
                not _root_matches(root_fd, root)
                or not _named_matches(
                    root_fd, source_parts[0], source_chain[0], directory=True
                )
                or not _source_snapshot_matches(root_fd, probe)
                or not _initial_destination_matches(root_fd, probe)
            ):
                raise PostImportRelocationUnavailable(
                    errno.ESTALE,
                    "relocation inputs changed during expectation capture",
                )
            _require_authority(authority)
            expectation = _expectation_record(
                root,
                root_fd,
                source_path,
                source_artist_record,
                source_snapshot,
                destination_path,
                destination_artist_record,
                destination_snapshot,
            )
            canonical = canonical_post_import_relocation_expectation(
                expectation,
                source=source_path,
                destination=destination_path,
            )
            if canonical is None:
                raise OSError("relocation expectation could not be encoded")
            return canonical
        finally:
            _close_all(destination_chain)
            _close_all(source_chain)
            _close_all(root_owner)


def _base_authority_matches(authority, root, root_fd, database_anchor, payload) -> bool:
    try:
        return (
            type(authority) is RunLockLease
            and authority.intact() is True
            and _root_matches(root_fd, root)
            and _migration_database_anchor_matches(database_anchor)
            and _source_artist_matches(root_fd, payload)
            and _source_snapshot_matches(root_fd, payload)
            and _initial_destination_matches(root_fd, payload)
        )
    except (OSError, TypeError, ValueError):
        return False


def _validate_payload(payload, *, operation_id=None) -> None:
    legacy_required = {
        "version",
        "operation_id",
        "phase",
        "kind",
        "music_root",
        "source",
        "destination",
        "source_snapshot",
        "source_artist_record",
        "destination_artist_record",
        "destination_snapshot",
        "plan",
        "database_capture",
        "workspace_record",
        "copying_index",
        "created_artist",
        "public_snapshot",
        "ownership_receipt",
        "await_handoff",
        "handoff",
    }
    required = legacy_required | {"retain_completion"}
    retain_completion = (
        payload.get("retain_completion", False)
        if isinstance(payload, dict)
        else False
    )
    if (
        type(payload) is not dict
        or frozenset(payload)
        not in {frozenset(legacy_required), frozenset(required)}
        or payload.get("version") != _JOURNAL_VERSION
        or not _valid_operation_id(payload.get("operation_id"))
        or operation_id is not None and payload.get("operation_id") != operation_id
        or payload.get("phase")
        not in {
            "intent",
            "prepared",
            "published",
            "awaiting-handoff",
            "committed",
            "retained",
        }
        or payload.get("kind") not in {kind.value for kind in RelocationKind}
        or payload.get("music_root") != os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
        or payload.get("destination_artist_record") is not None
        and not isinstance(payload.get("destination_artist_record"), dict)
        or type(payload.get("await_handoff")) is not bool
        or type(retain_completion) is not bool
        or retain_completion and not payload.get("await_handoff")
        or not payload.get("await_handoff") and payload.get("handoff") is not None
        or payload.get("phase") == "awaiting-handoff"
        and not payload.get("await_handoff")
        or payload.get("handoff") is not None
        and payload.get("phase")
        not in {"awaiting-handoff", "committed", "retained"}
        or payload.get("phase") == "retained"
        and (
            not retain_completion
            or not payload.get("await_handoff")
            or payload.get("handoff") is None
        )
    ):
        raise ValueError("relocation journal payload is invalid")
    _validated_handoff(payload.get("handoff"))
    _valid_relative(payload["source"], album=True)
    _valid_relative(payload["destination"], album=True)
    if payload["source"] == payload["destination"]:
        raise ValueError("relocation source and destination are identical")
    plan = payload["plan"]
    if (
        not isinstance(plan, dict)
        or set(plan) != {"units", "duplicates", "selected_files", "conflicts"}
        or not isinstance(plan["units"], list)
        or not isinstance(plan["duplicates"], list)
        or not isinstance(plan["selected_files"], list)
        or not isinstance(plan["conflicts"], list)
        or len(plan["units"]) > _MAX_TREE_ENTRIES
    ):
        raise ValueError("relocation journal plan is invalid")
    for index, unit in enumerate(plan["units"]):
        if (
            not isinstance(unit, dict)
            or unit.get("index") != index
            or unit.get("kind") not in {"file", "tree"}
            or "relative" not in unit
            or "source_record" not in unit
            or not set(unit).issubset({
                "index",
                "kind",
                "relative",
                "source_record",
                "container",
                "prepared_record",
            })
        ):
            raise ValueError("relocation journal unit is invalid")
        _valid_relative(unit["relative"], allow_root=unit["kind"] == "tree")
    for field in ("duplicates", "selected_files", "conflicts"):
        values = plan[field]
        if len(values) != len(set(values)):
            raise ValueError("relocation journal paths are duplicated")
        for value in values:
            _valid_relative(value)


def _database_recovery_state(database_anchor, payload, authority_matches):
    reconcile_atomic_sqlite(database_anchor, _migration_database_anchor_matches)
    if not _migration_database_anchor_matches(database_anchor):
        raise OSError("Beets database changed during relocation recovery")
    return _database_state(
        database_anchor, payload["database_capture"], authority_matches
    )


def _rollback_database_publication(database_anchor, payload, authority_matches):
    timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
    timeout = float(timeout) if timeout and timeout > 0 else 5.0
    state = rollback_relocation_rows(
        database_anchor,
        _migration_database_anchor_matches,
        authority_matches,
        payload["database_capture"],
        connect=sqlite3.connect,
        timeout=timeout,
    )
    if state not in {DatabaseRowsState.OLD, DatabaseRowsState.EMPTY}:
        raise OSError("Beets path rollback did not complete")
    return state


def _handoff_owner_decision(payload, handoff_matches) -> bool:
    handoff = payload.get("handoff")
    if handoff is None:
        return False
    if not callable(handoff_matches):
        raise PostImportRelocationAttention(
            "the deferred relocation owner could not be checked"
        )
    try:
        decision = handoff_matches(payload["operation_id"], handoff)
    except Exception as exc:
        raise PostImportRelocationAttention(
            "the deferred relocation owner could not be checked"
        ) from exc
    if decision is not True and decision is not False:
        raise PostImportRelocationAttention(
            "the deferred relocation owner could not be checked"
        )
    return decision


def _recover_payload(
    authority,
    root,
    root_fd,
    namespace_fd,
    database_anchor,
    payload,
    *,
    handoff_matches=None,
) -> str:
    _validate_payload(payload)
    _require_authority(authority)
    retain_completion = payload.get("retain_completion", False)
    workspace_record = payload.get("workspace_record")
    if not isinstance(workspace_record, dict):
        workspace_name = _workspace_name(payload["operation_id"])
        if not _name_missing(root_fd, workspace_name):
            descriptor = _open_directory(workspace_name, dir_fd=root_fd)
            try:
                if not _named_matches(root_fd, workspace_name, descriptor, directory=True):
                    raise PostImportRelocationAttention(
                        "an unfinished relocation workspace changed"
                    )
                if os.listdir(descriptor):
                    raise PostImportRelocationAttention(
                        "an unfinished relocation workspace is not empty"
                    )
                os.rmdir(workspace_name, dir_fd=root_fd)
                os.fsync(root_fd)
            finally:
                os.close(descriptor)
        _remove_created_artist_if_empty(root_fd, payload)
        _unlink_journal(namespace_fd, payload["operation_id"])
        return "rolled-back"

    workspace_name = _workspace_name(payload["operation_id"])
    if _name_missing(root_fd, workspace_name):
        authority_matches = lambda: (
            type(authority) is RunLockLease
            and authority.intact() is True
            and _root_matches(root_fd, root)
            and _migration_database_anchor_matches(database_anchor)
            and _journal_matches(namespace_fd, payload)
        )
        database_state = _database_recovery_state(
            database_anchor, payload, authority_matches
        )
        phase = payload["phase"]
        if retain_completion and phase in {"committed", "retained"}:
            if not _handoff_owner_decision(payload, handoff_matches):
                raise PostImportRelocationAttention(
                    "the retained relocation owner no longer matches"
                )
        elif payload["await_handoff"] and phase != "committed":
            handoff = payload["handoff"]
            if handoff is not None:
                decision = _handoff_owner_decision(payload, handoff_matches)
                if decision is True:
                    raise PostImportRelocationAttention(
                        "a persisted relocation handoff lost its recovery workspace"
                    )
            if database_state in {
                DatabaseRowsState.NEW,
                DatabaseRowsState.EMPTY,
            }:
                raise PostImportRelocationAttention(
                    "a deferred relocation lost its private recovery workspace",
                    paths=(root / payload["source"], root / payload["destination"]),
                )
        forward = database_state is DatabaseRowsState.NEW or (
            database_state is DatabaseRowsState.EMPTY
            and phase in {"committed", "retained"}
        )
        rollback = database_state is DatabaseRowsState.OLD or (
            database_state is DatabaseRowsState.EMPTY
            and phase not in {"committed", "retained"}
        )
        if forward and _public_snapshot_matches(root_fd, payload):
            _cleanup_source(root_fd, None, payload)
            if retain_completion:
                if phase != "retained":
                    payload["phase"] = "retained"
                    _write_journal(namespace_fd, payload)
                _checkpoint("after-retained-completion")
                return "retained"
            _unlink_journal(namespace_fd, payload["operation_id"])
            return "committed"
        if (
            rollback
            and _source_snapshot_matches(root_fd, payload)
            and _initial_destination_matches(root_fd, payload)
        ):
            _remove_created_artist_if_empty(root_fd, payload)
            _unlink_journal(namespace_fd, payload["operation_id"])
            return "rolled-back"
        raise PostImportRelocationAttention(
            "relocation recovery evidence is incomplete after workspace cleanup",
            paths=(root / payload["source"], root / payload["destination"]),
        )

    workspace_fd = _open_workspace(
        root_fd, payload["operation_id"], workspace_record
    )
    try:
        units = payload["plan"]["units"]
        prepared = all("prepared_record" in unit for unit in units)
        phase = payload["phase"]
        if (
            phase in {"intent", "prepared"}
            and payload.get("destination_artist_record") is None
        ):
            if any(
                "prepared_record" in unit
                and not _unit_matches(
                    workspace_fd, _unit_name(unit["index"]), unit
                )
                for unit in units
            ):
                raise PostImportRelocationAttention(
                    "an unpublished relocation copy changed while the app was stopped",
                    paths=(root / payload["source"],),
                )
            _remove_workspace(root_fd, workspace_fd, payload)
            workspace_fd = None
            _unlink_journal(namespace_fd, payload["operation_id"])
            return "rolled-back"
        states = _all_unit_states(root_fd, workspace_fd, payload) if prepared else []
        if any(state == "unknown" for state in states):
            raise PostImportRelocationAttention(
                "relocation files changed while the app was stopped",
                paths=(root / payload["source"], root / payload["destination"]),
            )

        if phase in {"intent", "prepared"} and not prepared:
            _remove_workspace(root_fd, workspace_fd, payload)
            workspace_fd = None
            _remove_created_artist_if_empty(root_fd, payload)
            _unlink_journal(namespace_fd, payload["operation_id"])
            return "rolled-back"

        if phase in {"intent", "prepared"} and all(
            state == "private" for state in states
        ):
            _remove_workspace(root_fd, workspace_fd, payload)
            workspace_fd = None
            _remove_created_artist_if_empty(root_fd, payload)
            _unlink_journal(namespace_fd, payload["operation_id"])
            return "rolled-back"

        authority_matches = lambda: (
            type(authority) is RunLockLease
            and authority.intact() is True
            and _root_matches(root_fd, root)
            and _migration_database_anchor_matches(database_anchor)
            and _journal_matches(namespace_fd, payload)
        )
        database_state = _database_recovery_state(
            database_anchor, payload, authority_matches
        )
        if database_state is DatabaseRowsState.MIXED:
            raise PostImportRelocationAttention(
                "Beets contains a mixture of old and new relocation paths"
            )

        retained_terminal = (
            retain_completion and phase in {"committed", "retained"}
        )
        deferred = (
            payload["await_handoff"]
            and phase not in {"committed", "retained"}
        )
        if retained_terminal:
            if not _handoff_owner_decision(payload, handoff_matches):
                raise PostImportRelocationAttention(
                    "the retained relocation owner no longer matches"
                )
            if database_state is DatabaseRowsState.OLD:
                raise PostImportRelocationAttention(
                    "Beets was restored behind a retained relocation"
                )
            forward = database_state in {
                DatabaseRowsState.NEW,
                DatabaseRowsState.EMPTY,
            }
        elif deferred:
            handoff = payload["handoff"]
            if handoff is None:
                decision = False
            else:
                decision = _handoff_owner_decision(payload, handoff_matches)
            if decision is True:
                if database_state is DatabaseRowsState.OLD:
                    raise PostImportRelocationAttention(
                        "Beets was restored behind a persisted relocation handoff"
                    )
                if units and not all(state == "public" for state in states):
                    raise PostImportRelocationAttention(
                        "a deferred relocation publication is incomplete"
                    )
                if not _publication_authority_matches(
                    authority,
                    root_fd,
                    namespace_fd,
                    database_anchor,
                    payload,
                ):
                    raise PostImportRelocationAttention(
                        "a deferred relocation changed while its owner was checked"
                    )
                payload["phase"] = "committed"
                _write_journal(namespace_fd, payload)
                phase = "committed"
                forward = True
            else:
                if database_state in {
                    DatabaseRowsState.NEW,
                    DatabaseRowsState.EMPTY,
                }:
                    if units and not all(state == "public" for state in states):
                        raise PostImportRelocationAttention(
                            "a deferred relocation publication is incomplete"
                        )
                    rollback_authority = lambda: _publication_authority_matches(
                        authority,
                        root_fd,
                        namespace_fd,
                        database_anchor,
                        payload,
                    )
                    database_state = _rollback_database_publication(
                        database_anchor, payload, rollback_authority
                    )
                forward = False
        elif database_state is DatabaseRowsState.NEW:
            forward = True
        elif database_state is DatabaseRowsState.EMPTY:
            forward = phase in {"committed", "retained"}
        elif database_state is DatabaseRowsState.OLD:
            if phase in {"committed", "retained"}:
                raise PostImportRelocationAttention(
                    "the Beets database was restored behind a committed relocation"
                )
            forward = False

        if forward:
            if units and not all(state == "public" for state in states):
                raise PostImportRelocationAttention(
                    "Beets points to relocated files whose publication is incomplete"
                )
            _cleanup_source(root_fd, workspace_fd, payload)
            _remove_workspace(root_fd, workspace_fd, payload)
            workspace_fd = None
            if retain_completion:
                payload["phase"] = "retained"
                _write_journal(namespace_fd, payload)
                _checkpoint("after-retained-completion")
                return "retained"
            _unlink_journal(namespace_fd, payload["operation_id"])
            return "committed"

        _rollback_units(root_fd, workspace_fd, payload)
        _remove_workspace(root_fd, workspace_fd, payload)
        workspace_fd = None
        _remove_created_artist_if_empty(root_fd, payload)
        _unlink_journal(namespace_fd, payload["operation_id"])
        return "rolled-back"
    finally:
        if workspace_fd is not None:
            os.close(workspace_fd)


def _clean_journal_temps(namespace_fd, root_fd) -> None:
    names = sorted(os.listdir(namespace_fd))
    canonical_ids = {
        name[:-5]
        for name in names
        if name.endswith(".json") and _valid_operation_id(name[:-5])
    }
    for name in names:
        if not name.startswith(".tmp-"):
            continue
        operation_id = _temporary_operation_id(name)
        descriptor = _open_regular(name, dir_fd=namespace_fd)
        try:
            if not _named_matches(namespace_fd, name, descriptor):
                raise OSError("relocation journal temporary changed")
            if operation_id not in canonical_ids and not _name_missing(
                root_fd, _workspace_name(operation_id)
            ):
                raise PostImportRelocationAttention(
                    "a relocation has a workspace but no canonical journal"
                )
            os.unlink(name, dir_fd=namespace_fd)
            os.fsync(namespace_fd)
        finally:
            os.close(descriptor)


def reconcile_post_import_relocations(
    *, authority: RunLockLease, handoff_matches=None
) -> RelocationRecoveryResult:
    """Resolve every durable relocation before queue or repair work starts."""
    try:
        _require_authority(authority)
        with _THREAD_LOCK, _locked_namespace(create=False) as namespace_fd:
            if namespace_fd is None:
                return RelocationRecoveryResult(RelocationRecoveryStatus.CLEAR)
            root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
            root_fd, root_owner = _open_music_root(root)
            try:
                _clean_journal_temps(namespace_fd, root_fd)
                names = set(os.listdir(namespace_fd))
                unexpected = {
                    name
                    for name in names
                    if name != _LOCK_NAME
                    and not (name.endswith(".json") and _valid_operation_id(name[:-5]))
                }
                journals = sorted(
                    name for name in names
                    if name.endswith(".json") and _valid_operation_id(name[:-5])
                )
                if unexpected or len(journals) > 1:
                    raise PostImportRelocationAttention(
                        "the relocation recovery directory contains unexpected state",
                        paths=(_namespace_path(),),
                    )
                for name in journals:
                    operation_id = name[:-5]
                    payload = _read_journal(namespace_fd, operation_id)
                    _validate_payload(payload, operation_id=operation_id)
                    database_anchor = _open_migration_database_anchor()
                    try:
                        _recover_payload(
                            authority,
                            root,
                            root_fd,
                            namespace_fd,
                            database_anchor,
                            payload,
                            handoff_matches=handoff_matches,
                        )
                    finally:
                        _close_migration_database_anchor(database_anchor)
                return RelocationRecoveryResult(RelocationRecoveryStatus.CLEAR)
            finally:
                _close_all(root_owner)
    except PostImportRelocationAttention as exc:
        return RelocationRecoveryResult(
            RelocationRecoveryStatus.ATTENTION_REQUIRED,
            str(exc),
            exc.paths,
        )
    except (
        OSError,
        sqlite3.Error,
        SQLitePublicationUncertain,
        SQLiteRecoveryRequired,
        ValueError,
    ) as exc:
        return RelocationRecoveryResult(
            RelocationRecoveryStatus.ATTENTION_REQUIRED,
            str(exc),
            (_namespace_path(),),
        )


def _require_handoff_publication(
    authority, root_fd, namespace_fd, database_anchor, payload
) -> None:
    authority_matches = lambda: _publication_authority_matches(
        authority,
        root_fd,
        namespace_fd,
        database_anchor,
        payload,
    )
    if not authority_matches():
        raise PostImportRelocationAttention(
            "the deferred relocation changed before its handoff was recorded"
        )
    state = _database_recovery_state(database_anchor, payload, authority_matches)
    if state not in {DatabaseRowsState.NEW, DatabaseRowsState.EMPTY}:
        raise PostImportRelocationAttention(
            "Beets no longer contains the deferred relocation"
        )
    if not authority_matches():
        raise PostImportRelocationAttention(
            "the deferred relocation changed while its handoff was recorded"
        )


def seal_post_import_relocation_handoff(
    operation_id,
    *,
    consumer,
    payload,
    authority: RunLockLease,
) -> str:
    """Durably bind one deferred relocation to its exact Web Undo record."""
    _require_authority(authority)
    if not _valid_operation_id(operation_id):
        raise ValueError("relocation operation id is invalid")
    consumer, digest = _handoff_digest(consumer, payload)
    with _THREAD_LOCK, _locked_namespace(create=False) as namespace_fd:
        if namespace_fd is None:
            raise PostImportRelocationUnavailable(
                errno.ENOENT, "the deferred relocation journal is missing"
            )
        root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        root_fd, root_owner = _open_music_root(root)
        database_anchor = None
        try:
            _clean_journal_temps(namespace_fd, root_fd)
            journal = _read_journal(namespace_fd, operation_id)
            _validate_payload(journal, operation_id=operation_id)
            if not journal["await_handoff"] or journal["phase"] != "awaiting-handoff":
                raise PostImportRelocationUnavailable(
                    errno.EBUSY, "the relocation is not awaiting a handoff"
                )
            queue_consumer = consumer["kind"] == "queue-completion"
            if journal.get("retain_completion", False) is not queue_consumer:
                raise PostImportRelocationUnavailable(
                    errno.EPERM,
                    "the relocation handoff consumer does not match its "
                    "completion mode",
                )
            expected = {"consumer": consumer, "hash": digest}
            existing = journal["handoff"]
            if existing is not None and existing != expected:
                raise PostImportRelocationUnavailable(
                    errno.EEXIST, "the relocation is sealed to another handoff"
                )
            database_anchor = _open_migration_database_anchor()
            _require_handoff_publication(
                authority, root_fd, namespace_fd, database_anchor, journal
            )
            if existing is None:
                journal["handoff"] = expected
                _write_journal(namespace_fd, journal)
                _require_handoff_publication(
                    authority, root_fd, namespace_fd, database_anchor, journal
                )
            return digest
        finally:
            _close_migration_database_anchor(database_anchor)
            _close_all(root_owner)


def acknowledge_post_import_relocation(
    operation_id,
    handoff_hash,
    *,
    authority: RunLockLease,
) -> None:
    """Commit one relocation after its exact durable owner is recorded."""
    _require_authority(authority)
    if not _valid_operation_id(operation_id) or not _valid_operation_id(
        handoff_hash
    ):
        raise ValueError("relocation acknowledgement is invalid")
    with _THREAD_LOCK, _locked_namespace(create=False) as namespace_fd:
        if namespace_fd is None:
            raise PostImportRelocationUnavailable(
                errno.ENOENT, "the deferred relocation journal is missing"
            )
        root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        root_fd, root_owner = _open_music_root(root)
        database_anchor = None
        try:
            _clean_journal_temps(namespace_fd, root_fd)
            payload = _read_journal(namespace_fd, operation_id)
            _validate_payload(payload, operation_id=operation_id)
            handoff = payload["handoff"]
            if (
                not payload["await_handoff"]
                or handoff is None
                or not secrets.compare_digest(handoff["hash"], handoff_hash)
                or payload["phase"]
                not in {"awaiting-handoff", "committed", "retained"}
            ):
                raise PostImportRelocationUnavailable(
                    errno.EPERM, "the relocation acknowledgement does not match"
                )
            database_anchor = _open_migration_database_anchor()
            if payload["phase"] == "awaiting-handoff":
                _require_handoff_publication(
                    authority, root_fd, namespace_fd, database_anchor, payload
                )
            outcome = _recover_payload(
                authority,
                root,
                root_fd,
                namespace_fd,
                database_anchor,
                payload,
                handoff_matches=lambda current_id, current_handoff: (
                    current_id == operation_id and current_handoff == handoff
                ),
            )
            expected_outcome = (
                "retained" if payload.get("retain_completion", False)
                else "committed"
            )
            if outcome != expected_outcome:
                raise PostImportRelocationAttention(
                    "the deferred relocation could not be committed"
                )
        finally:
            _close_migration_database_anchor(database_anchor)
            _close_all(root_owner)


def release_post_import_relocation(
    operation_id,
    handoff_hash,
    *,
    authority: RunLockLease,
) -> None:
    """Release one exact retained receipt after its owner settles the action."""
    _require_authority(authority)
    if not _valid_operation_id(operation_id) or not _valid_operation_id(
        handoff_hash
    ):
        raise ValueError("relocation release is invalid")
    with _THREAD_LOCK, _locked_namespace(create=False) as namespace_fd:
        if namespace_fd is None or _name_missing(
            namespace_fd, _journal_name(operation_id)
        ):
            return
        root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        root_fd, root_owner = _open_music_root(root)
        database_anchor = None
        try:
            _clean_journal_temps(namespace_fd, root_fd)
            payload = _read_journal(namespace_fd, operation_id)
            _validate_payload(payload, operation_id=operation_id)
            handoff = payload["handoff"]
            if (
                payload.get("retain_completion", False) is not True
                or payload["phase"] != "retained"
                or handoff is None
                or not secrets.compare_digest(handoff["hash"], handoff_hash)
            ):
                raise PostImportRelocationUnavailable(
                    errno.EPERM, "the retained relocation release does not match"
                )
            database_anchor = _open_migration_database_anchor()
            outcome = _recover_payload(
                authority,
                root,
                root_fd,
                namespace_fd,
                database_anchor,
                payload,
                handoff_matches=lambda current_id, current_handoff: (
                    current_id == operation_id and current_handoff == handoff
                ),
            )
            if outcome != "retained":
                raise PostImportRelocationAttention(
                    "the retained relocation could not be verified"
                )
            current = _read_journal(namespace_fd, operation_id)
            _validate_payload(current, operation_id=operation_id)
            if (
                current["phase"] != "retained"
                or current.get("retain_completion", False) is not True
                or current["handoff"] != handoff
            ):
                raise PostImportRelocationAttention(
                    "the retained relocation changed before release"
                )
            _unlink_journal(namespace_fd, operation_id)
        finally:
            _close_migration_database_anchor(database_anchor)
            _close_all(root_owner)


def relocate_post_import_album(
    source,
    destination,
    *,
    kind: RelocationKind,
    authority: RunLockLease,
    await_handoff: bool = False,
    retain_completion: bool = False,
    expected=None,
) -> RelocationResult:
    """Copy, publish, and commit one additive post-import relocation."""
    _require_authority(authority)
    if not isinstance(kind, RelocationKind):
        raise ValueError("relocation kind is invalid")
    if type(await_handoff) is not bool:
        raise ValueError("relocation handoff mode is invalid")
    if type(retain_completion) is not bool:
        raise ValueError("relocation retained completion mode is invalid")
    if retain_completion and not await_handoff:
        raise ValueError("retained completion requires a deferred handoff")
    root, source_path, source_parts = _absolute_album_path(source)
    destination_root, destination_path, destination_parts = _absolute_album_path(
        destination
    )
    if root != destination_root or source_path == destination_path:
        raise ValueError("relocation source and destination are invalid")
    canonical_expected = None
    if expected is not None:
        canonical_expected = canonical_post_import_relocation_expectation(
            expected,
            source=source_path,
            destination=destination_path,
        )
        if canonical_expected is None:
            raise ValueError("relocation expectation is invalid")
    recovery = reconcile_post_import_relocations(authority=authority)
    if recovery.status is not RelocationRecoveryStatus.CLEAR:
        raise PostImportRelocationAttention(
            recovery.reason or "a previous album relocation needs attention",
            paths=recovery.paths,
        )

    with _THREAD_LOCK:
        root_fd = source_fd = destination_fd = None
        root_owner = None
        source_chain = destination_chain = []
        database_anchor = None
        try:
            root_fd, root_owner = _open_music_root(root)
            source_fd, source_chain = _open_relative_directory(
                root_fd, PurePosixPath(*source_parts).as_posix()
            )
            source_snapshot = _snapshot_tree(source_fd)
            source_artist_fd = source_chain[0]
            source_artist_record = _stat_record(os.fstat(source_artist_fd))
            try:
                destination_fd, destination_chain = _open_relative_directory(
                    root_fd, PurePosixPath(*destination_parts).as_posix()
                )
            except FileNotFoundError:
                destination_fd = None
                destination_snapshot = None
                artist = destination_parts[0]
                try:
                    destination_artist_fd = _open_directory(artist, dir_fd=root_fd)
                except FileNotFoundError:
                    destination_artist_record = None
                else:
                    try:
                        if not _named_matches(
                            root_fd, artist, destination_artist_fd, directory=True
                        ):
                            raise OSError(
                                "relocation destination artist changed during preflight"
                            )
                        destination_artist_record = _stat_record(
                            os.fstat(destination_artist_fd)
                        )
                    finally:
                        os.close(destination_artist_fd)
            else:
                if _entry_identity(os.fstat(source_fd)) == _entry_identity(
                    os.fstat(destination_fd)
                ):
                    raise PostImportRelocationUnavailable(
                        errno.EINVAL,
                        "relocation source and destination resolve to the same album folder",
                    )
                destination_snapshot = _snapshot_tree(destination_fd)
                destination_artist_fd = destination_chain[0]
                if not _named_matches(
                    root_fd,
                    destination_parts[0],
                    destination_artist_fd,
                    directory=True,
                ):
                    raise OSError(
                        "relocation destination artist changed during preflight"
                    )
                destination_artist_record = _stat_record(
                    os.fstat(destination_artist_fd)
                )
                if os.fstat(destination_fd).st_dev != os.fstat(root_fd).st_dev:
                    raise OSError(errno.EXDEV, "relocation destination is on another filesystem")
            if canonical_expected is not None:
                current_expectation = _expectation_record(
                    root,
                    root_fd,
                    source_path,
                    source_artist_record,
                    source_snapshot,
                    destination_path,
                    destination_artist_record,
                    destination_snapshot,
                )
                if current_expectation != canonical_expected:
                    raise PostImportRelocationUnavailable(
                        errno.ESTALE,
                        "relocation inputs changed since the filing action "
                        "was recorded",
                    )
            plan = _build_plan(source_snapshot, destination_snapshot)
            if kind is RelocationKind.WHOLE_ALBUM and plan["conflicts"]:
                return RelocationResult(
                    source_path,
                    0,
                    changed=False,
                    reason="the destination contains conflicting names",
                )
            if not plan["selected_files"]:
                return RelocationResult(
                    (
                        source_path
                        if kind is RelocationKind.WHOLE_ALBUM
                        else destination_path
                    ),
                    0,
                    changed=False,
                    reason=(
                        "all destination names already exist"
                        if plan["conflicts"] else "there is nothing to relocate"
                    ),
                )
            database_anchor = _open_migration_database_anchor()
            operation_id = secrets.token_hex(32)
            payload = {
                "version": _JOURNAL_VERSION,
                "operation_id": operation_id,
                "phase": "intent",
                "kind": kind.value,
                "music_root": os.fspath(root),
                "source": PurePosixPath(*source_parts).as_posix(),
                "destination": PurePosixPath(*destination_parts).as_posix(),
                "source_snapshot": source_snapshot,
                "source_artist_record": source_artist_record,
                "destination_artist_record": destination_artist_record,
                "destination_snapshot": destination_snapshot,
                "plan": plan,
                "database_capture": None,
                "workspace_record": None,
                "copying_index": None,
                "created_artist": None,
                "public_snapshot": None,
                "ownership_receipt": None,
                "await_handoff": await_handoff,
                "retain_completion": retain_completion,
                "handoff": None,
            }
            initial_authority = lambda: (
                _base_authority_matches(
                    authority, root, root_fd, database_anchor, payload
                )
                and (
                    canonical_expected is None
                    or _directory_identity_matches_fd(
                        root_fd, canonical_expected["root_identity"]
                    )
                )
            )
            payload["database_capture"] = _capture_database(
                database_anchor, root, payload, initial_authority
            )
            if not initial_authority():
                raise OSError("relocation inputs changed during preflight")

            with _locked_namespace(create=True) as namespace_fd:
                _write_journal(namespace_fd, payload, initial=True)
                workspace_name, workspace_fd, workspace_record = _create_workspace(
                    root_fd, operation_id
                )
                del workspace_name
                payload["workspace_record"] = workspace_record
                _write_journal(namespace_fd, payload)
                committed = False
                try:
                    _prepare_units(source_fd, workspace_fd, payload, namespace_fd)
                    payload["phase"] = "prepared"
                    _write_journal(namespace_fd, payload)
                    if not initial_authority():
                        raise OSError("relocation inputs changed before publication")
                    _ensure_destination_artist(root_fd, payload, namespace_fd)
                    if not _source_snapshot_matches(root_fd, payload):
                        raise OSError("relocation source changed before publication")
                    _publish_units(root_fd, workspace_fd, payload)
                    payload["public_snapshot"] = _destination_snapshot(
                        root_fd, payload
                    )
                    payload["ownership_receipt"] = _build_ownership_receipt(
                        root_fd, payload
                    )
                    payload["phase"] = "published"
                    _write_journal(namespace_fd, payload)
                    _checkpoint("after-filesystem-publication")
                    authority_matches = lambda: _publication_authority_matches(
                        authority,
                        root_fd,
                        namespace_fd,
                        database_anchor,
                        payload,
                    )
                    timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
                    timeout = float(timeout) if timeout and timeout > 0 else 5.0
                    state = publish_relocation_rows(
                        database_anchor,
                        _migration_database_anchor_matches,
                        authority_matches,
                        payload["database_capture"],
                        connect=sqlite3.connect,
                        timeout=timeout,
                    )
                    if state not in {DatabaseRowsState.NEW, DatabaseRowsState.EMPTY}:
                        raise OSError("Beets path publication did not complete")
                    _checkpoint("after-database-publication")
                    if await_handoff:
                        payload["phase"] = "awaiting-handoff"
                        _write_journal(namespace_fd, payload)
                        return RelocationResult(
                            destination_path,
                            len(plan["selected_files"]),
                            operation_id,
                            payload["ownership_receipt"],
                            True,
                            (
                                f"kept {len(plan['conflicts'])} existing destination "
                                "name(s)"
                                if plan["conflicts"] else None
                            ),
                        )
                    payload["phase"] = "committed"
                    _write_journal(namespace_fd, payload)
                    committed = True
                    _cleanup_source(root_fd, workspace_fd, payload)
                    _remove_workspace(root_fd, workspace_fd, payload)
                    workspace_fd = None
                    _unlink_journal(namespace_fd, operation_id)
                    return RelocationResult(
                        destination_path,
                        len(plan["selected_files"]),
                        operation_id,
                        payload["ownership_receipt"],
                        True,
                        (
                            f"kept {len(plan['conflicts'])} existing destination "
                            "name(s)"
                            if plan["conflicts"] else None
                        ),
                    )
                except BaseException as exc:
                    if workspace_fd is not None:
                        os.close(workspace_fd)
                        workspace_fd = None
                    outcome = _recover_payload(
                        authority,
                        root,
                        root_fd,
                        namespace_fd,
                        database_anchor,
                        payload,
                    )
                    if outcome == "committed" and isinstance(exc, Exception):
                        return RelocationResult(
                            destination_path,
                            len(plan["selected_files"]),
                            operation_id,
                            payload["ownership_receipt"],
                            True,
                            "the move completed during recovery",
                        )
                    if committed and isinstance(exc, Exception):
                        raise PostImportRelocationAttention(
                            "the relocation committed but cleanup needs attention",
                            paths=(source_path, destination_path),
                        ) from exc
                    raise
                finally:
                    if workspace_fd is not None:
                        os.close(workspace_fd)
        finally:
            _close_migration_database_anchor(database_anchor)
            _close_all(destination_chain)
            _close_all(source_chain)
            _close_all(root_owner)


__all__ = [
    "PostImportRelocationAttention",
    "PostImportRelocationUnavailable",
    "RelocationKind",
    "RelocationRecoveryResult",
    "RelocationRecoveryStatus",
    "RelocationResult",
    "acknowledge_post_import_relocation",
    "canonical_post_import_relocation_expectation",
    "capture_post_import_relocation_expectation",
    "reconcile_post_import_relocations",
    "release_post_import_relocation",
    "relocate_post_import_album",
    "seal_post_import_relocation_handoff",
]
