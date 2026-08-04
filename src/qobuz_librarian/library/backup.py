"""Upgrade and gap-fill backup/restore functions."""
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.integrations.rip import flac_audio_ok
from qobuz_librarian.interrupts import run_sigint_deferred
from qobuz_librarian.library.scanner import iter_tree_no_symlinks
from qobuz_librarian.recovery import (
    decode_recovery_json,
    normalise_recovery_owner,
    recovery_owner_matches,
)
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog


@dataclass(frozen=True, eq=False)
class BackupResult(os.PathLike):
    """A backup path plus the transaction state that produced it.

    It remains path-like for older read/restore call sites.  Code that is
    about to mutate anything after a backup must inspect ``complete`` first;
    an incomplete result deliberately exposes the recovery location instead
    of disguising a partial move as either success or no backup at all.
    """

    path: Path
    complete: bool
    receipt: dict | None
    requested: int
    backed_up: int

    def __fspath__(self):
        return os.fspath(self.path)

    def __str__(self):
        return str(self.path)

    def __truediv__(self, value):
        return self.path / value

    def __getattr__(self, name):
        return getattr(self.path, name)

    def __eq__(self, other):
        if isinstance(other, BackupResult):
            return self.path == other.path
        try:
            return self.path == Path(other)
        except (TypeError, ValueError):
            return False

    def __hash__(self):
        return hash(self.path)


@dataclass(frozen=True)
class BackupDisposalReconciliation:
    """Result of adopting one deterministic post-crash quarantine."""

    state: str
    quarantine_path: Path | None = None


@dataclass(frozen=True)
class IncompleteUpgradeRestoreOutcome:
    """Truthful result of reconciling an interrupted whole-album backup.

    ``restored`` counts files published by this call, ``already_present``
    counts writer-held files that already matched the carried original, and
    ``unresolved`` counts originals that were not proved safe.  A false
    ``backup_disposed`` means the recovery copy remains authoritative even
    when every file is now present (for example, a final fsync was uncertain).
    """

    restored: int
    already_present: int
    unresolved: int
    backup_disposed: bool


def _list_tree(root: Path):
    """Every entry under ``root``, or None when the walk couldn't cover the
    whole tree. rglob swallows subtree listing failures, so a partial walk can
    pass for a complete one — every caller here draws a keep-or-delete
    conclusion from the listing, and "couldn't see it" must never count as
    "isn't there"."""
    if not root.exists():
        # A tree that isn't there holds nothing to lose — distinct from one
        # that exists but can't be read (os.walk reports the latter as an
        # error, and "couldn't see it" must not read as "empty").
        return []
    entries = []
    errors = []
    try:
        for f in iter_tree_no_symlinks(root, errors=errors):
            entries.append(f)
    except OSError:
        return None
    if errors:
        return None
    return entries


def _backup_dir_name(album_dir: Path, *, kind: str = "") -> str:
    # Shared name for upgrade and gap-fill backup dirs:
    # "<ymd>_<hms>_<micro>[_<kind>]_<safe>". Microseconds keep two backups of
    # the same album in the same wall-clock second from colliding into one dir
    # (which mkdir(exist_ok=True) would silently merge, mixing two operations'
    # files and breaking the 1:1 backup→restore mapping).
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe = re.sub(r"[^\w\-_. ]", "_", album_dir.name)[:80]
    infix = f"{kind}_" if kind else ""
    return f"{ts}_{infix}{safe}"


def _upgrade_backup_path_for(album_dir: Path) -> Path:
    cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return cfg.UPGRADE_BACKUP_DIR / _backup_dir_name(album_dir)


def _same_filesystem(a: Path, b: Path) -> bool:
    """True if a and b live on the same filesystem (same st_dev).

    Inside Docker, /music and /upgrade_backups are separate bind mounts
    so they get different st_dev even when the host paths share a disk —
    that's what makes the cross-fs path the common case for image users.
    Walks up to the nearest existing ancestor on either side so this can
    answer before either dir actually exists.
    """
    def _existing_ancestor(p: Path) -> Path:
        cur = p
        while not cur.exists() and cur != cur.parent:
            cur = cur.parent
        return cur
    try:
        return os.stat(_existing_ancestor(a)).st_dev == os.stat(_existing_ancestor(b)).st_dev
    except OSError:
        return False


_RENAME_NOREPLACE = 1


def _open_backup_directory(path, *, dir_fd=None):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow directory access is unavailable")
    flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    return os.open(os.fspath(path), flags, dir_fd=dir_fd)


def _same_directory(left, right) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (int(left.st_dev), int(left.st_ino))
        == (int(right.st_dev), int(right.st_ino))
    )


def _entry_identity(value):
    return (
        stat.S_IFMT(value.st_mode),
        int(value.st_dev),
        int(value.st_ino),
    )


def _same_entry(left, right) -> bool:
    return _entry_identity(left) == _entry_identity(right)


def _named_entry_matches(parent_fd, name, entry_fd) -> bool:
    try:
        return _same_entry(
            os.fstat(entry_fd),
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False),
        )
    except (OSError, TypeError, ValueError):
        return False


def _named_directory_matches(parent_fd, name, directory_fd) -> bool:
    try:
        held = os.fstat(directory_fd)
        return stat.S_ISDIR(held.st_mode) and _same_entry(
            held, os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    except (OSError, TypeError, ValueError):
        return False


def _named_entry_missing(parent_fd, name) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return False


def _open_entry_at(parent_fd, name):
    """Hold any exact directory entry, including a symlink, without following."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    path_only = getattr(os, "O_PATH", None)
    if nofollow is None or path_only is None:
        raise OSError("safe no-follow entry access is unavailable")
    descriptor = os.open(
        os.fspath(name),
        path_only | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        if not _named_entry_matches(parent_fd, name, descriptor):
            raise OSError("entry changed while it was opened")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _rename_noreplace_at(source_fd, source_name, destination_fd,
                         destination_name) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
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
            os.fsencode(source_name),
            int(destination_fd),
            os.fsencode(destination_name),
            _RENAME_NOREPLACE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination_name))


def _rename_exact_noreplace_at(source_fd, source_name, destination_fd,
                               destination_name, expected_fd):
    """Move one held inode and reconcile even a post-success BaseException.

    Returns an exception raised *after* the kernel completed the rename, so
    the caller can record its forward state before rolling back or propagating
    cancellation.  Exceptions raised before the move are re-raised directly.
    """
    if (
        not _named_entry_matches(source_fd, source_name, expected_fd)
        or not _named_entry_missing(destination_fd, destination_name)
    ):
        raise OSError("exact no-replace rename precondition changed")
    deferred = None
    try:
        _rename_noreplace_at(
            source_fd, source_name, destination_fd, destination_name)
    except BaseException as exc:
        deferred = exc
    forward = (
        _named_entry_missing(source_fd, source_name)
        and _named_entry_matches(
            destination_fd, destination_name, expected_fd)
    )
    if forward:
        commit_exception = None
        try:
            if not _fsync_directory_fds(source_fd, destination_fd):
                raise OSError("exact rename could not be committed")
        except BaseException as exc:
            commit_exception = exc
        return deferred or commit_exception
    # The public source may have been replaced between the precondition check
    # and renameat2.  In that case renameat2 moved the replacement, not our
    # held inode.  Put that exact unexpected entry back without overwrite;
    # never absorb it into an app-owned backup.
    if _named_entry_missing(source_fd, source_name):
        unexpected_fd = None
        try:
            unexpected_fd = _open_entry_at(
                destination_fd, destination_name)
            rollback_exception = None
            try:
                _rename_noreplace_at(
                    destination_fd,
                    destination_name,
                    source_fd,
                    source_name,
                )
            except BaseException as exc:
                rollback_exception = exc
            if (
                _named_entry_missing(
                    destination_fd, destination_name)
                and _named_entry_matches(
                    source_fd, source_name, unexpected_fd)
            ):
                _fsync_directory_fds(source_fd, destination_fd)
                raise OSError(
                    "source was replaced during exact rename; the "
                    "replacement was restored") from (
                        deferred or rollback_exception)
        except FileNotFoundError:
            pass
        finally:
            if unexpected_fd is not None:
                os.close(unexpected_fd)
    unchanged = (
        _named_entry_matches(source_fd, source_name, expected_fd)
        and _named_entry_missing(destination_fd, destination_name)
    )
    if deferred is not None and unchanged:
        raise deferred
    raise OSError("exact rename outcome could not be reconciled") from deferred


def _unlink_exact_at(parent_fd, name, expected_fd):
    """Unlink one exact held regular file; return a post-success exception."""
    if not _named_entry_matches(parent_fd, name, expected_fd):
        raise OSError("exact unlink precondition changed")
    deferred = None
    try:
        os.unlink(name, dir_fd=parent_fd)
    except BaseException as exc:
        deferred = exc
    if _named_entry_missing(parent_fd, name):
        commit_exception = None
        try:
            if not _fsync_directory_fds(parent_fd):
                raise OSError("exact unlink could not be committed")
        except BaseException as exc:
            commit_exception = exc
        return deferred or commit_exception
    if deferred is not None and _named_entry_matches(
            parent_fd, name, expected_fd):
        raise deferred
    raise OSError("exact unlink outcome could not be reconciled") from deferred


def _rmdir_exact_at(parent_fd, name, expected_fd):
    """Remove one exact empty held directory; return a post-success exception."""
    if not _named_directory_matches(parent_fd, name, expected_fd):
        raise OSError("exact rmdir precondition changed")
    deferred = None
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except BaseException as exc:
        deferred = exc
    if _named_entry_missing(parent_fd, name):
        commit_exception = None
        try:
            if not _fsync_directory_fds(parent_fd):
                raise OSError("exact rmdir could not be committed")
        except BaseException as exc:
            commit_exception = exc
        return deferred or commit_exception
    if deferred is not None and _named_directory_matches(
            parent_fd, name, expected_fd):
        raise deferred
    raise OSError("exact rmdir outcome could not be reconciled") from deferred


def _open_backup_source(album_dir: Path):
    try:
        root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        public = Path(os.path.abspath(os.fspath(album_dir)))
        if os.path.commonpath((os.fspath(root), os.fspath(public))) != os.fspath(root):
            return None
        relative = os.path.relpath(public, root)
        parts = tuple(Path(relative).parts)
        if relative == "." or any(part in ("", ".", "..") for part in parts):
            return None
    except (OSError, TypeError, ValueError):
        return None

    descriptors = []
    try:
        root_fd = _open_backup_directory(root)
        descriptors.append(root_fd)
        if not _same_directory(
                os.fstat(root_fd), os.stat(root, follow_symlinks=False)):
            raise OSError("music root changed while it was opened")
        for part in parts:
            child_fd = _open_backup_directory(part, dir_fd=descriptors[-1])
            try:
                if not _named_directory_matches(
                        descriptors[-1], part, child_fd):
                    raise OSError("album path changed while it was opened")
            except BaseException:
                os.close(child_fd)
                raise
            descriptors.append(child_fd)
        return public, root, parts, descriptors
    except BaseException as exc:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, (OSError, TypeError, ValueError)):
            return None
        raise


def _backup_source_is_public(root, parts, descriptors, *, include_album=True):
    expected = len(parts) + 1
    if len(descriptors) != expected:
        return False
    try:
        if not _same_directory(
                os.fstat(descriptors[0]),
                os.stat(root, follow_symlinks=False)):
            return False
        limit = len(parts) if include_album else len(parts) - 1
        for index in range(limit):
            if not _named_directory_matches(
                    descriptors[index], parts[index], descriptors[index + 1]):
                return False
    except (OSError, TypeError, ValueError):
        return False
    return True


def _held_directory_path(directory_fd) -> Path:
    return Path("/proc") / str(os.getpid()) / "fd" / str(directory_fd)


def _fsync_directory_fds(*descriptors) -> bool:
    seen = set()
    for descriptor in descriptors:
        try:
            value = os.fstat(descriptor)
            if not stat.S_ISDIR(value.st_mode):
                return False
            identity = (int(value.st_dev), int(value.st_ino))
            if identity in seen:
                continue
            os.fsync(descriptor)
            seen.add(identity)
        except OSError:
            return False
    return True


def _reserve_backup_quarantine(parent_fd, prefix, *, exact_name=None):
    names = (
        (exact_name,)
        if exact_name is not None
        else tuple(f".{prefix}-{secrets.token_hex(16)}" for _ in range(16))
    )
    for name in names:
        if (
            type(name) is not str
            or not name.startswith(f".{prefix}-")
            or "/" in name
            or "\x00" in name
        ):
            raise ValueError("backup quarantine name is invalid")
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            if exact_name is not None:
                raise OSError("private backup quarantine already exists")
            continue
        quarantine_fd = None
        try:
            quarantine_fd = _open_backup_directory(name, dir_fd=parent_fd)
            if not _named_directory_matches(parent_fd, name, quarantine_fd):
                raise OSError("backup quarantine changed while it was opened")
            if not _fsync_directory_fds(parent_fd):
                raise OSError("backup quarantine could not be committed")
            return name, quarantine_fd
        except BaseException:
            if (
                quarantine_fd is not None
                and _named_directory_matches(parent_fd, name, quarantine_fd)
            ):
                try:
                    _rmdir_exact_at(parent_fd, name, quarantine_fd)
                except BaseException:
                    pass
            if quarantine_fd is not None:
                os.close(quarantine_fd)
            raise
    raise OSError("could not reserve a private backup quarantine")


def _restore_quarantined_entry(quarantine_fd, parent_fd, name,
                               expected_fd) -> bool:
    if not _named_entry_missing(parent_fd, name):
        return False
    try:
        # A cancellation delivered after the kernel move is reconciled as a
        # successful rollback.  The caller will still propagate its original
        # cancellation once the exact public entry is safe again.
        _rename_exact_noreplace_at(
            quarantine_fd, "held", parent_fd, name, expected_fd)
    except BaseException:
        return False
    return (
        _named_entry_matches(parent_fd, name, expected_fd)
        and _named_entry_missing(quarantine_fd, "held")
        and _fsync_directory_fds(quarantine_fd, parent_fd)
    )


def _remove_exact_tree_at(parent_fd, name, expected_fd, *, prefix,
                          commit_guard=None, expected_snapshot=None,
                          held_files=None, quarantine_name=None,
                          disposal_manifest=None) -> bool:
    """Remove exactly one held tree, never a replacement at the same path."""
    if not _named_directory_matches(parent_fd, name, expected_fd):
        return False
    snapshot = expected_snapshot or _exact_tree_snapshot(expected_fd)
    if (
        snapshot is None
        or _exact_tree_snapshot(expected_fd) != snapshot
        or held_files is not None
        and not _held_snapshot_files_intact(
            expected_fd, snapshot, held_files)
    ):
        return False
    manifest = None
    if disposal_manifest is not None:
        manifest = _canonical_ownerless_disposal_manifest(
            disposal_manifest,
            expected_quarantine_name=quarantine_name,
        )
        try:
            backup_root = Path(
                os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))
            parent_is_backup_root = _same_directory(
                os.fstat(parent_fd),
                os.stat(backup_root, follow_symlinks=False),
            )
        except (OSError, TypeError, ValueError):
            parent_is_backup_root = False
        if (
            manifest is None
            or manifest["carrier_name"] != name
            or manifest["snapshot"] != snapshot
            or not parent_is_backup_root
        ):
            return False
    reserved_quarantine_name = None
    quarantine_fd = None
    manifest_fd = None
    manifest_present = False
    moved = False
    deletion_started = False
    try:
        reserved_quarantine_name, quarantine_fd = _reserve_backup_quarantine(
            parent_fd, prefix, exact_name=quarantine_name)
        if manifest is not None:
            manifest_fd = _write_ownerless_disposal_manifest_at(
                quarantine_fd, manifest)
            if manifest_fd is None:
                return False
            manifest_present = True
        move_exception = _rename_exact_noreplace_at(
            parent_fd, name, quarantine_fd, "held", expected_fd)
        moved = True
        if move_exception is not None:
            if _restore_quarantined_entry(
                    quarantine_fd, parent_fd, name, expected_fd):
                moved = False
            raise move_exception
        if _exact_tree_snapshot(expected_fd) != snapshot:
            if _restore_quarantined_entry(
                    quarantine_fd, parent_fd, name, expected_fd):
                moved = False
            return False
        if commit_guard is not None:
            try:
                guarded = commit_guard()
            except BaseException:
                if _restore_quarantined_entry(
                        quarantine_fd, parent_fd, name, expected_fd):
                    moved = False
                raise
            if not guarded:
                if _restore_quarantined_entry(
                        quarantine_fd, parent_fd, name, expected_fd):
                    moved = False
                return False

        deletion_started = True
        deferred = _delete_exact_tree_contents(
            expected_fd,
            snapshot,
            held=held_files,
            commit_guard=commit_guard,
        )
        root_exception = _rmdir_exact_at(
            quarantine_fd, "held", expected_fd)
        moved = False
        manifest_exception = None
        if manifest_present:
            manifest_exception = _unlink_exact_at(
                quarantine_fd, _DISPOSAL_MANIFEST, manifest_fd)
            manifest_present = False
        if not _named_directory_matches(
                parent_fd, reserved_quarantine_name, quarantine_fd):
            return False
        quarantine_exception = _rmdir_exact_at(
            parent_fd, reserved_quarantine_name, quarantine_fd)
        reserved_quarantine_name = None
        deferred = (
            deferred
            or root_exception
            or manifest_exception
            or quarantine_exception
        )
        if deferred is not None:
            raise deferred
        return True
    except BaseException as exc:
        rollback_safe = True
        if quarantine_name is not None and deletion_started:
            try:
                rollback_safe = (
                    _exact_tree_snapshot(expected_fd) == snapshot
                    and (
                        held_files is None
                        or _held_snapshot_files_intact(
                            expected_fd, snapshot, held_files)
                    )
                )
            except (OSError, TypeError, ValueError):
                rollback_safe = False
        if (
            moved
            and rollback_safe
            and quarantine_fd is not None
            and _restore_quarantined_entry(
                quarantine_fd, parent_fd, name, expected_fd)
        ):
            moved = False
        if isinstance(exc, (OSError, TypeError, ValueError, shutil.Error)):
            return False
        log.info(fmt(
            C.YELLOW,
            "\n  ⚠  Backup removal was interrupted after its exact state "
            "was reconciled."))
        raise
    finally:
        if (
            quarantine_fd is not None
            and manifest_fd is not None
            and manifest_present
            and not moved
        ):
            try:
                _unlink_exact_at(
                    quarantine_fd, _DISPOSAL_MANIFEST, manifest_fd)
                manifest_present = False
            except BaseException:
                pass
        if (
            quarantine_fd is not None
            and reserved_quarantine_name is not None
            and not moved
        ):
            try:
                _rmdir_exact_at(
                    parent_fd, reserved_quarantine_name, quarantine_fd)
            except BaseException:
                pass
        if manifest_fd is not None:
            try:
                os.close(manifest_fd)
            except OSError:
                pass
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass


def _backup_root_is_public(backup_root: Path, backup_root_fd) -> bool:
    try:
        return _same_directory(
            os.fstat(backup_root_fd),
            os.stat(backup_root, follow_symlinks=False),
        )
    except (OSError, TypeError, ValueError):
        return False


def _restore_exact_backup_move(backup_root_fd, backup_name, source_parent_fd,
                               source_name, source_fd) -> bool:
    if (
        not _named_entry_missing(source_parent_fd, source_name)
        or not _named_directory_matches(
            backup_root_fd, backup_name, source_fd)
    ):
        return False
    try:
        _rename_exact_noreplace_at(
            backup_root_fd,
            backup_name,
            source_parent_fd,
            source_name,
            source_fd,
        )
    except BaseException:
        return False
    return (
        _named_directory_matches(source_parent_fd, source_name, source_fd)
        and _named_entry_missing(backup_root_fd, backup_name)
        and _fsync_directory_fds(backup_root_fd, source_parent_fd)
    )


def _reserved_backup_entry_present(source_fd) -> bool:
    entries = _list_tree(_held_directory_path(source_fd))
    if entries is None:
        return True
    try:
        return any(entry.name in _SIDECARS for entry in entries)
    except OSError:
        return True


def _remove_written_origin(source_fd) -> bool:
    descriptor = None
    try:
        descriptor = _open_regular_file_at(source_fd, _ORIGIN_SIDECAR)
        return _remove_exact_file_at(
            source_fd,
            _ORIGIN_SIDECAR,
            descriptor,
            prefix="ql-origin-remove",
        )
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _remove_written_receipt(source_fd) -> bool:
    descriptor = None
    try:
        descriptor = _open_regular_file_at(source_fd, _RECEIPT_SIDECAR)
        return _remove_exact_file_at(
            source_fd,
            _RECEIPT_SIDECAR,
            descriptor,
            prefix="ql-receipt-remove",
        )
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_backup_origin_durable(directory_fd, origin: Path) -> bool:
    if not _write_backup_origin(directory_fd, origin):
        return False
    sidecar_fd = None
    try:
        sidecar_fd = _open_regular_file_at(directory_fd, _ORIGIN_SIDECAR)
        os.fsync(sidecar_fd)
        return (
            _named_entry_matches(
                directory_fd, _ORIGIN_SIDECAR, sidecar_fd)
            and _fsync_directory_fds(directory_fd)
        )
    except OSError:
        return False
    finally:
        if sidecar_fd is not None:
            os.close(sidecar_fd)


def _write_text_noreplace_at(directory_fd, name, value) -> bool:
    descriptor = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return False
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        data = value.encode("utf-8")
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("metadata write made no progress")
            offset += written
        os.fsync(descriptor)
        return (
            _named_entry_matches(directory_fd, name, descriptor)
            and _fsync_directory_fds(directory_fd)
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _rooted_path_parts(root: Path, path: Path, *, allow_root=False):
    try:
        root = Path(os.path.abspath(os.fspath(root)))
        public = Path(os.path.abspath(os.fspath(path)))
        if os.path.commonpath((os.fspath(root), os.fspath(public))) != os.fspath(root):
            return None
        relative = os.path.relpath(public, root)
        if relative == ".":
            return (public, root, ()) if allow_root else None
        parts = tuple(Path(relative).parts)
        if any(part in ("", ".", "..") for part in parts):
            return None
        return public, root, parts
    except (OSError, TypeError, ValueError):
        return None


def _open_rooted_directory(root: Path, path: Path, *, create=False,
                           allow_root=False):
    rooted = _rooted_path_parts(root, path, allow_root=allow_root)
    if rooted is None:
        return None
    public, root, parts = rooted
    descriptors = []
    try:
        root_fd = _open_backup_directory(root)
        descriptors.append(root_fd)
        if not _same_directory(
                os.fstat(root_fd), os.stat(root, follow_symlinks=False)):
            raise OSError("root directory changed while it was opened")
        for part in parts:
            try:
                child_fd = _open_backup_directory(part, dir_fd=descriptors[-1])
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, dir_fd=descriptors[-1])
                if not _fsync_directory_fds(descriptors[-1]):
                    raise OSError("created directory could not be committed")
                child_fd = _open_backup_directory(part, dir_fd=descriptors[-1])
            try:
                if not _named_directory_matches(
                        descriptors[-1], part, child_fd):
                    raise OSError("directory path changed while it was opened")
            except BaseException:
                os.close(child_fd)
                raise
            descriptors.append(child_fd)
        return public, root, parts, descriptors
    except BaseException as exc:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, (OSError, TypeError, ValueError)):
            return None
        raise


def _close_descriptors(descriptors) -> None:
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _open_relative_directories(base_fd, parts, *, create=False):
    descriptors = []
    parent_fd = base_fd
    try:
        for part in parts:
            if part in ("", ".", ".."):
                raise OSError("unsafe relative directory")
            try:
                child_fd = _open_backup_directory(part, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, dir_fd=parent_fd)
                if not _fsync_directory_fds(parent_fd):
                    raise OSError("created directory could not be committed")
                child_fd = _open_backup_directory(part, dir_fd=parent_fd)
            try:
                if not _named_directory_matches(parent_fd, part, child_fd):
                    raise OSError("relative directory changed while it was opened")
            except BaseException:
                os.close(child_fd)
                raise
            descriptors.append(child_fd)
            parent_fd = child_fd
        return descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _relative_directories_are_named(base_fd, parts, descriptors) -> bool:
    if len(parts) != len(descriptors):
        return False
    parent_fd = base_fd
    for part, descriptor in zip(parts, descriptors):
        if not _named_directory_matches(parent_fd, part, descriptor):
            return False
        parent_fd = descriptor
    return True


def _open_regular_file_at(parent_fd, name):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file access is unavailable")
    flags = (os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NONBLOCK", 0))
    descriptor = os.open(os.fspath(name), flags, dir_fd=parent_fd)
    try:
        held = os.fstat(descriptor)
        if (
            not stat.S_ISREG(held.st_mode)
            or not _named_entry_matches(parent_fd, name, descriptor)
        ):
            raise OSError("file changed while it was opened")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file_digest_fd(descriptor) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _xattrs_fd(descriptor):
    """Return every supported extended attribute on one held inode."""
    try:
        names = sorted(os.listxattr(descriptor))
    except OSError as exc:
        if exc.errno in _XATTR_UNSUPPORTED:
            return {}
        raise
    values = {}
    for name in names:
        values[name] = os.getxattr(descriptor, name)
    return values


def _fidelity_fd(descriptor, *, include_times=True):
    value = os.fstat(descriptor)
    result = {
        "type": stat.S_IFMT(value.st_mode),
        "mode": stat.S_IMODE(value.st_mode),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "xattrs": _xattrs_fd(descriptor),
    }
    if include_times:
        result.update({
            "atime_ns": int(value.st_atime_ns),
            "mtime_ns": int(value.st_mtime_ns),
        })
    return result


def _copy_fidelity_fd(source_fd, destination_fd, *, adopt_owner=False) -> None:
    """Copy and verify ownership, permissions, times, xattrs and ACL xattrs.

    ``adopt_owner`` keeps the destination's own ownership instead of the
    source's — the app can't chown a copy of a file it doesn't own, and a
    backup copy the app owns is what lets the rest of that transaction hold
    ordinary leases."""
    source = _fidelity_fd(source_fd)
    destination = os.fstat(destination_fd)
    if adopt_owner:
        source["uid"] = int(destination.st_uid)
        source["gid"] = int(destination.st_gid)
    if (int(destination.st_uid), int(destination.st_gid)) != (
            source["uid"], source["gid"]):
        os.fchown(destination_fd, source["uid"], source["gid"])
    os.fchmod(destination_fd, source["mode"])
    existing = _xattrs_fd(destination_fd)
    for name in existing.keys() - source["xattrs"].keys():
        os.removexattr(destination_fd, name)
    for name, value in source["xattrs"].items():
        os.setxattr(destination_fd, name, value)
    os.utime(
        destination_fd,
        ns=(source["atime_ns"], source["mtime_ns"]),
    )
    if _fidelity_fd(destination_fd) != source:
        raise OSError("copied file metadata did not match its held source")


def _tree_fidelity_snapshot(root_fd):
    """Fidelity receipt for a held regular-file tree, without following links."""
    snapshot = {"directories": {}, "files": {}}

    def _walk(directory_fd, prefix):
        directory = _fidelity_fd(directory_fd, include_times=False)
        directory["mtime_ns"] = int(os.fstat(directory_fd).st_mtime_ns)
        snapshot["directories"]["/".join(prefix)] = directory
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            if name in ("", ".", ".."):
                return False
            relative = prefix + (name,)
            key = "/".join(relative)
            value = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(value.st_mode):
                child_fd = _open_backup_directory(name, dir_fd=directory_fd)
                try:
                    if (
                        not _named_directory_matches(directory_fd, name, child_fd)
                        or not _walk(child_fd, relative)
                    ):
                        return False
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(value.st_mode):
                file_fd = _open_regular_file_at(directory_fd, name)
                try:
                    current = _fidelity_fd(file_fd, include_times=False)
                    current.update({
                        "size": int(os.fstat(file_fd).st_size),
                        "mtime_ns": int(os.fstat(file_fd).st_mtime_ns),
                        "sha256": _file_digest_fd(file_fd),
                        "links": int(os.fstat(file_fd).st_nlink),
                    })
                    snapshot["files"][key] = current
                finally:
                    os.close(file_fd)
            else:
                return False
        return True

    try:
        return snapshot if _walk(root_fd, ()) else None
    except (OSError, TypeError, ValueError):
        return None


def _tree_has_multiply_linked_file(snapshot) -> bool:
    return snapshot is None or any(
        value.get("links", 1) != 1 for value in snapshot["files"].values())


def _regular_file_receipt(descriptor, digest):
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        raise OSError("receipt source is not a regular file")
    return {
        "identity": _entry_identity(value),
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
        "sha256": digest,
    }


_GAP_FILL_RECEIPT_FIELDS = {
    "type",
    "device",
    "inode",
    "size",
    "mtime_ns",
    "ctime_ns",
    "sha256",
}


def _gap_fill_file_receipt(descriptor, digest=None):
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode):
        raise OSError("gap-fill receipt source is not a regular file")
    if digest is None:
        digest = _file_digest_fd(descriptor)
    return {
        "type": stat.S_IFMT(value.st_mode),
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
        "sha256": digest,
    }


def _gap_fill_receipt_relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise OSError("gap-fill receipt has an invalid relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise OSError("gap-fill receipt has an invalid relative path")
    return tuple(relative.parts)


def _normalise_gap_fill_expected_receipts(receipts):
    if not isinstance(receipts, dict) or not receipts:
        raise OSError("gap-fill expected receipts are missing")
    normalised = {}
    for raw_relative, raw_receipt in receipts.items():
        parts = _gap_fill_receipt_relative(raw_relative)
        if (
            not isinstance(raw_receipt, dict)
            or set(raw_receipt) != _GAP_FILL_RECEIPT_FIELDS
            or any(
                type(raw_receipt[field]) is not int
                for field in _GAP_FILL_RECEIPT_FIELDS - {"sha256"}
            )
            or raw_receipt["type"] != stat.S_IFREG
            or not isinstance(raw_receipt["sha256"], str)
            or len(raw_receipt["sha256"]) != 64
            or any(
                char not in "0123456789abcdef"
                for char in raw_receipt["sha256"]
            )
        ):
            raise OSError("gap-fill expected receipt is malformed")
        relative = "/".join(parts)
        if relative in normalised:
            raise OSError("gap-fill expected receipt path is duplicated")
        normalised[relative] = dict(raw_receipt)
    return normalised


def capture_gap_fill_source_receipt(file_path, album_dir):
    """Seal one verified source to an album-relative path and exact inode."""
    opened = _open_backup_source(Path(album_dir))
    if opened is None:
        return None
    public, music_root, album_parts, album_fds = opened
    source_parents = []
    source_fd = None
    try:
        rooted = _rooted_path_parts(public, Path(file_path))
        if rooted is None:
            return None
        _source_path, _album_root, file_parts = rooted
        if file_parts[-1] in _SIDECARS:
            return None
        source_parents = _open_relative_directories(
            album_fds[-1], file_parts[:-1], create=False)
        source_parent_fd = (
            source_parents[-1] if source_parents else album_fds[-1])
        source_fd = _open_regular_file_at(source_parent_fd, file_parts[-1])
        receipt = _gap_fill_file_receipt(source_fd)
        if (
            receipt != _gap_fill_file_receipt(source_fd)
            or not _named_entry_matches(
                source_parent_fd, file_parts[-1], source_fd)
            or not _backup_source_is_public(
                music_root, album_parts, album_fds)
            or not _relative_directories_are_named(
                album_fds[-1], file_parts[:-1], source_parents)
        ):
            return None
        return {
            "relative": "/".join(file_parts),
            "file": receipt,
        }
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        _close_descriptors(source_parents)
        _close_descriptors(album_fds)


def _restore_exact_entry_move(source_parent_fd, source_name,
                              destination_parent_fd, destination_name,
                              expected_fd) -> bool:
    if (
        not _named_entry_missing(destination_parent_fd, destination_name)
        or not _named_entry_matches(source_parent_fd, source_name, expected_fd)
    ):
        return False
    try:
        _rename_exact_noreplace_at(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            expected_fd,
        )
    except BaseException:
        return False
    return (
        _named_entry_missing(source_parent_fd, source_name)
        and _named_entry_matches(
            destination_parent_fd, destination_name, expected_fd)
        and _fsync_directory_fds(
            source_parent_fd, destination_parent_fd)
    )


def _serializable_tree_fidelity(snapshot):
    if snapshot is None:
        return None
    converted = {"directories": {}, "files": {}}
    for group in converted:
        for relative, metadata in snapshot[group].items():
            value = dict(metadata)
            value["xattrs"] = {
                name: payload.hex()
                for name, payload in metadata["xattrs"].items()
            }
            converted[group][relative] = value
    return converted


def _tree_directory_generations(root_fd, snapshot):
    # Migration already carries the project's fail-closed Linux statx proof.
    # Import lazily to avoid making backup module loading depend on migration's
    # optional metadata integrations.
    from qobuz_librarian.library.migrate import _directory_identity

    generations = {"": _directory_identity(root_fd)}
    for relative_text in snapshot["directories"]:
        relative = tuple(PurePosixPath(relative_text).parts)
        descriptors = _open_relative_directories(
            root_fd, relative, create=False)
        try:
            generations[relative_text] = _directory_identity(descriptors[-1])
        finally:
            _close_descriptors(descriptors)
    return generations


def _path_directory_generations(descriptors):
    """Strong incarnation proof for MUSIC_ROOT and every opened descendant."""
    from qobuz_librarian.library.migrate import _directory_identity

    return [_directory_identity(descriptor) for descriptor in descriptors]


def _album_source_receipt_from_opened(public, music_root, parts, descriptors):
    album_fd = descriptors[-1]
    snapshot = _exact_tree_snapshot(album_fd)
    fidelity = _tree_fidelity_snapshot(album_fd)
    if snapshot is None or fidelity is None:
        return None
    held = None
    try:
        generations = _tree_directory_generations(album_fd, snapshot)
        path_generations = _path_directory_generations(descriptors)
        held = {}
        _hold_snapshot_files(album_fd, snapshot, held)
        if (
            not _backup_source_is_public(music_root, parts, descriptors)
            or _exact_tree_snapshot(album_fd) != snapshot
            or _tree_fidelity_snapshot(album_fd) != fidelity
            or _tree_directory_generations(album_fd, snapshot) != generations
            or _path_directory_generations(descriptors) != path_generations
            or not _held_snapshot_files_intact(album_fd, snapshot, held)
        ):
            return None
        return {
            "version": 1,
            "origin": os.fspath(public),
            "tree": snapshot,
            "fidelity": _serializable_tree_fidelity(fidelity),
            "directory_generations": generations,
            "path_generations": path_generations,
        }
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if held is not None:
            _release_held_snapshot_files(held)


def capture_album_source_receipt(album_dir):
    """Seal an exact, serializable receipt for later whole-album retirement."""
    opened = _open_backup_source(Path(album_dir))
    if opened is None:
        return None
    public, music_root, parts, descriptors = opened
    try:
        if _reserved_backup_entry_present(descriptors[-1]):
            return None
        return _album_source_receipt_from_opened(
            public, music_root, parts, descriptors)
    finally:
        _close_descriptors(descriptors)


def _fidelity_without_root_sidecars(snapshot):
    """The copy carries the tracks, never the backup's own root metadata."""
    if snapshot is None:
        return None
    return {
        "directories": snapshot["directories"],
        "files": {
            relative: value
            for relative, value in snapshot["files"].items()
            if PurePosixPath(relative).parent != PurePosixPath(".")
            or PurePosixPath(relative).name not in _SIDECARS
        },
    }


def _tree_copy_fidelity_matches(source, destination) -> bool:
    if source is None or destination is None:
        return False
    if source["directories"] != destination["directories"]:
        return False
    if set(source["files"]) != set(destination["files"]):
        return False
    for relative, expected in source["files"].items():
        copied = destination["files"][relative]
        if copied.get("links") != 1:
            return False
        if {
            key: value for key, value in copied.items() if key != "links"
        } != {
            key: value for key, value in expected.items() if key != "links"
        }:
            return False
    return True


def _copy_tree_directory_fidelity(source_root_fd, destination_root_fd) -> bool:
    snapshot = _exact_tree_snapshot(source_root_fd)
    if snapshot is None:
        return False
    try:
        paths = [tuple(PurePosixPath(value).parts)
                 for value in snapshot["directories"]]
        paths.append(())
        for relative in sorted(paths, key=len, reverse=True):
            source_parents = _open_relative_directories(
                source_root_fd, relative, create=False)
            destination_parents = _open_relative_directories(
                destination_root_fd, relative, create=False)
            source_fd = source_parents[-1] if source_parents else source_root_fd
            destination_fd = (
                destination_parents[-1]
                if destination_parents else destination_root_fd)
            try:
                _copy_fidelity_fd(source_fd, destination_fd)
                if not _fsync_directory_fds(destination_fd):
                    return False
            finally:
                _close_descriptors(destination_parents)
                _close_descriptors(source_parents)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _remove_exact_file_at(parent_fd, name, expected_fd, *, prefix,
                          commit_guard=None) -> bool:
    """Unlink one held regular file through a private exact quarantine."""
    try:
        held = os.fstat(expected_fd)
    except OSError:
        return False
    if (
        not stat.S_ISREG(held.st_mode)
        or not _named_entry_matches(parent_fd, name, expected_fd)
    ):
        return False
    quarantine_name = None
    quarantine_fd = None
    moved = False
    deferred = None
    try:
        quarantine_name, quarantine_fd = _reserve_backup_quarantine(
            parent_fd, prefix)
        move_exception = _rename_exact_noreplace_at(
            parent_fd, name, quarantine_fd, "held", expected_fd)
        moved = True
        if move_exception is not None:
            if _restore_quarantined_entry(
                    quarantine_fd, parent_fd, name, expected_fd):
                moved = False
            raise move_exception
        if (
            not _named_entry_missing(parent_fd, name)
            or not _named_entry_matches(quarantine_fd, "held", expected_fd)
        ):
            if _restore_quarantined_entry(
                    quarantine_fd, parent_fd, name, expected_fd):
                moved = False
            return False
        if not _fsync_directory_fds(parent_fd, quarantine_fd):
            if _restore_quarantined_entry(
                    quarantine_fd, parent_fd, name, expected_fd):
                moved = False
            return False
        if commit_guard is not None:
            try:
                guarded = commit_guard()
            except Exception:
                guarded = False
            if not guarded:
                if _restore_quarantined_entry(
                        quarantine_fd, parent_fd, name, expected_fd):
                    moved = False
                return False
        deferred = _unlink_exact_at(quarantine_fd, "held", expected_fd)
        moved = False
        if not _named_directory_matches(
                parent_fd, quarantine_name, quarantine_fd):
            return False
        rmdir_exception = _rmdir_exact_at(
            parent_fd, quarantine_name, quarantine_fd)
        quarantine_name = None
        if deferred is None:
            deferred = rmdir_exception
        if deferred is not None:
            raise deferred
        return True
    except BaseException as exc:
        if (
            moved
            and quarantine_fd is not None
            and _restore_quarantined_entry(
                quarantine_fd, parent_fd, name, expected_fd)
        ):
            moved = False
        if isinstance(exc, (OSError, TypeError, ValueError, shutil.Error)):
            return False
        raise
    finally:
        if (
            quarantine_fd is not None
            and quarantine_name is not None
            and not moved
        ):
            try:
                _rmdir_exact_at(parent_fd, quarantine_name, quarantine_fd)
            except BaseException:
                pass
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass


class _CopyPublication:
    """Caller-owned resources for one no-replace copy publication.

    The caller creates this object before invoking the copy helper.  The
    helper installs the held inode here before its first namespace
    publication, so an asynchronous exception at the helper return boundary
    cannot orphan an unowned descriptor, lease, or published name.
    """

    def __init__(self, destination_parent_fd, destination_name):
        self.destination_parent_fd = destination_parent_fd
        self.destination_name = destination_name
        self.temporary_name = None
        self._file = None
        self._lease = None
        self._locations = [
            (destination_parent_fd, destination_name),
        ]
        self.committed = False
        self.cleanup_exception = None

    @property
    def descriptor(self):
        if self._file is None or self._file.closed:
            return None
        return self._file.fileno()

    @property
    def lease(self):
        return self._lease

    def reserve_temporary(self, temporary_name) -> None:
        self.temporary_name = temporary_name
        self.add_location(self.destination_parent_fd, temporary_name)

    def clear_temporary_reservation(self, temporary_name) -> None:
        if self._file is not None or self.temporary_name != temporary_name:
            return
        self.temporary_name = None
        try:
            self._locations.remove(
                (self.destination_parent_fd, temporary_name))
        except ValueError:
            pass

    def add_location(self, parent_fd, name) -> None:
        location = (parent_fd, name)
        if location not in self._locations:
            self._locations.append(location)

    def bind_file(self, file_object, temporary_name) -> None:
        # Classify the namespace before the final ownership-transfer store.
        # Until ``_file`` is assigned the helper still owns and cleans the
        # FileIO object; after it is assigned this object has every fact needed
        # to reconcile the exact private name.
        self.reserve_temporary(temporary_name)
        self._file = file_object

    def release_file(self) -> None:
        file_object = self._file
        if file_object is None:
            return
        try:
            file_object.close()
        finally:
            if file_object.closed:
                self._file = None

    def bind_lease(self, lease) -> None:
        self._lease = lease

    def owns_file(self, file_object) -> bool:
        return self._file is file_object

    def matches(self, parent_fd, name) -> bool:
        descriptor = self.descriptor
        return (
            descriptor is not None
            and _named_entry_matches(parent_fd, name, descriptor)
        )

    def named_locations(self):
        return [
            (parent_fd, name)
            for parent_fd, name in self._locations
            if self.matches(parent_fd, name)
        ]

    def intact(self) -> bool:
        return (
            self.descriptor is not None
            and self._lease is not None
            and self._lease.intact()
        )

    def reconcile_private_cleanup(self) -> bool:
        """Durably remove only this exact still-private temporary name."""
        descriptor = self.descriptor
        if self.temporary_name is None:
            return self.cleanup_exception is None
        if descriptor is None:
            if (
                _named_entry_missing(
                    self.destination_parent_fd, self.temporary_name)
                and _fsync_directory_fds(self.destination_parent_fd)
            ):
                self.cleanup_exception = None
                return True
            self.cleanup_exception = OSError(
                "reserved private copy name could not be reconciled exactly")
            return False
        if self.matches(
                self.destination_parent_fd, self.destination_name):
            return False
        self.cleanup_exception = OSError(
            "private copy cleanup has not been durably reconciled")
        if self.matches(
                self.destination_parent_fd, self.temporary_name):
            if _remove_exact_file_at(
                    self.destination_parent_fd,
                    self.temporary_name,
                    descriptor,
                    prefix="ql-copy-cleanup",
            ):
                self.cleanup_exception = None
                return True
        if (
            _named_entry_missing(
                self.destination_parent_fd, self.temporary_name)
            and _fsync_directory_fds(self.destination_parent_fd)
        ):
            # A failed unlink parent-fsync is uncertain, not success.  This
            # explicit second durability gate is what resolves that state;
            # otherwise the caller keeps its recovery tree/receipt path.
            self.cleanup_exception = None
            return True
        return False

    def close(self) -> None:
        lease = self._lease
        file_object = self._file
        try:
            if lease is not None:
                lease.close()
                self._lease = None
        finally:
            if file_object is not None:
                try:
                    file_object.close()
                finally:
                    if file_object.closed:
                        self._file = None


def _release_copy_publication(publication) -> None:
    """Reconcile a private temp, then release its stable held resources."""
    try:
        publication.reconcile_private_cleanup()
    finally:
        publication.close()


def _copy_file_noreplace_at(source_fd, publication, *, adopt_owner=False):
    """Durably copy one held file into a private directory without overwrite.

    The caller-owned ``publication`` receives the read-only descriptor and
    writer exclusion before the namespace commit.  Only the digest is
    returned, closing the CALL-to-STORE ownership gap at every caller.
    """
    destination_parent_fd = publication.destination_parent_fd
    destination_name = publication.destination_name
    source_value = os.fstat(source_fd)
    if not stat.S_ISREG(source_value.st_mode):
        raise OSError("backup source is not a regular file")
    source_digest = _file_digest_fd(source_fd)
    source_fidelity = _fidelity_fd(source_fd, include_times=False)
    copy_fidelity = dict(source_fidelity)
    if adopt_owner:
        copy_fidelity["uid"] = os.geteuid()
        copy_fidelity["gid"] = os.getegid()
    source_mtime_ns = int(os.fstat(source_fd).st_mtime_ns)
    temporary_name = None
    writable_file = None
    destination_file = None
    destination_lease = None
    published = False
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file creation is unavailable")
        for _ in range(16):
            temporary_name = f".ql-copy-{secrets.token_hex(16)}"
            publication.reserve_temporary(temporary_name)
            try:
                writable_file = io.FileIO(
                    temporary_name,
                    "x+b",
                    opener=lambda path, flags: os.open(
                        path,
                        flags | nofollow | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=destination_parent_fd,
                    ),
                )
                publication.bind_file(writable_file, temporary_name)
                if publication.owns_file(writable_file):
                    writable_file = None
                break
            except FileExistsError:
                publication.clear_temporary_reservation(temporary_name)
                temporary_name = None
        if publication.descriptor is None or temporary_name is None:
            raise OSError("could not reserve a private backup file")

        writable_fd = publication.descriptor
        offset = 0
        while True:
            chunk = os.pread(source_fd, 1024 * 1024, offset)
            if not chunk:
                break
            written = 0
            while written < len(chunk):
                count = os.write(writable_fd, chunk[written:])
                if count <= 0:
                    raise OSError("backup copy made no write progress")
                written += count
            offset += len(chunk)
        _copy_fidelity_fd(source_fd, writable_fd, adopt_owner=adopt_owner)
        os.fsync(writable_fd)
        if (
            _file_digest_fd(writable_fd) != source_digest
            or _file_digest_fd(source_fd) != source_digest
            or _fidelity_fd(source_fd, include_times=False) != source_fidelity
            or _fidelity_fd(
                writable_fd, include_times=False) != copy_fidelity
            or int(os.fstat(source_fd).st_mtime_ns) != source_mtime_ns
            or int(os.fstat(writable_fd).st_mtime_ns) != source_mtime_ns
            or os.fstat(writable_fd).st_nlink != 1
        ):
            raise OSError("backup copy did not faithfully match its held source")

        destination_file = io.FileIO(
            temporary_name,
            "rb",
            opener=lambda path, flags: os.open(
                path,
                flags | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=destination_parent_fd,
            ),
        )
        destination_fd = destination_file.fileno()
        if not _same_entry(
                os.fstat(destination_fd), os.fstat(writable_fd)):
            raise OSError("private backup inode changed while reopened")
        publication.release_file()
        publication.bind_file(destination_file, temporary_name)
        if publication.owns_file(destination_file):
            destination_file = None
        destination_lease = acquire_inode_write_exclusion(
            publication.descriptor)
        if destination_lease is None or not destination_lease.intact():
            raise OSError("private backup copy could not be writer-excluded")
        publication.bind_lease(destination_lease)
        if publication.lease is destination_lease:
            destination_lease = None
        if (
            _file_digest_fd(publication.descriptor) != source_digest
            or _file_digest_fd(source_fd) != source_digest
            or _fidelity_fd(source_fd, include_times=False) != source_fidelity
            or _fidelity_fd(
                publication.descriptor,
                include_times=False) != copy_fidelity
            or int(os.fstat(source_fd).st_mtime_ns) != source_mtime_ns
            or int(os.fstat(
                publication.descriptor).st_mtime_ns) != source_mtime_ns
            or os.fstat(publication.descriptor).st_nlink != 1
        ):
            raise OSError("writer-excluded backup copy changed before publication")
        deferred = _rename_exact_noreplace_at(
            destination_parent_fd,
            temporary_name,
            destination_parent_fd,
            destination_name,
            publication.descriptor,
        )
        published = True
        if deferred is not None:
            raise deferred
        if (
            not _named_entry_missing(destination_parent_fd, temporary_name)
            or not _named_entry_matches(
                destination_parent_fd, destination_name, destination_fd)
            or not _fsync_directory_fds(destination_parent_fd)
            or not publication.intact()
        ):
            raise OSError("backup copy could not be committed safely")
        publication.committed = True
        return source_digest
    except BaseException as exc:
        if (
            destination_file is not None
            and publication.owns_file(destination_file)
        ):
            destination_file = None
        if publication.matches(
                destination_parent_fd, destination_name):
            reconciled_publication = False
            try:
                reconciled_publication = (
                    publication.intact()
                    and _file_digest_fd(publication.descriptor)
                        == source_digest
                    and _file_digest_fd(source_fd) == source_digest
                    and _fidelity_fd(
                        source_fd, include_times=False) == source_fidelity
                    and _fidelity_fd(
                        publication.descriptor,
                        include_times=False) == copy_fidelity
                    and int(os.fstat(source_fd).st_mtime_ns)
                        == source_mtime_ns
                    and int(os.fstat(
                        publication.descriptor).st_mtime_ns)
                        == source_mtime_ns
                    and os.fstat(publication.descriptor).st_nlink == 1
                    and _named_entry_missing(
                        destination_parent_fd, temporary_name)
                    and _fsync_directory_fds(destination_parent_fd)
                )
            except (OSError, TypeError, ValueError):
                reconciled_publication = False
            if reconciled_publication:
                publication.committed = True
                if isinstance(exc, (OSError, TypeError, ValueError)):
                    return source_digest
            raise
        if destination_file is not None:
            destination_fd = destination_file.fileno()
            candidate = destination_name if published else temporary_name
            if (
                candidate is not None
                and _named_entry_matches(
                    destination_parent_fd, candidate, destination_fd)
                and (
                    not published
                    or destination_lease is not None
                    and destination_lease.intact()
                )
            ):
                try:
                    cleanup_exception = _unlink_exact_at(
                        destination_parent_fd, candidate, destination_fd)
                    if publication.cleanup_exception is None:
                        publication.cleanup_exception = cleanup_exception
                except BaseException:
                    pass
        elif publication.descriptor is not None:
            # Once the exact final name exists it is preserved.  Removing it
            # and then losing the parent-fsync result could resurrect an
            # unreceipted public copy after a crash.  Callers can now bind the
            # exact inode into their existing recovery/receipt path.
            if not publication.matches(
                    destination_parent_fd, destination_name):
                try:
                    publication.reconcile_private_cleanup()
                except BaseException:
                    pass
        if destination_lease is not None:
            destination_lease.close()
        if destination_file is not None:
            destination_file.close()
        if writable_file is not None:
            writable_fd = writable_file.fileno()
            if (
                temporary_name is not None
                and _named_entry_matches(
                    destination_parent_fd, temporary_name, writable_fd)
            ):
                try:
                    _unlink_exact_at(
                        destination_parent_fd, temporary_name, writable_fd)
                except BaseException:
                    pass
            writable_file.close()
        raise


def _scan_backup_tree(root_fd):
    """Return regular backup paths without following links, or None.

    Reserved metadata files are allowed only at the backup root and are not
    returned as tracks. A nested reserved name or any link/special entry is
    ambiguous user data, so the backup is preserved for manual recovery.
    """
    paths = []

    def _walk(directory_fd, prefix):
        try:
            with os.scandir(directory_fd) as iterator:
                entries = list(iterator)
        except OSError:
            return False
        for entry in entries:
            name = entry.name
            if name in ("", ".", ".."):
                return False
            try:
                value = entry.stat(follow_symlinks=False)
            except OSError:
                return False
            relative = prefix + (name,)
            if name in _SIDECARS:
                if prefix or not stat.S_ISREG(value.st_mode):
                    return False
                continue
            if stat.S_ISDIR(value.st_mode):
                try:
                    child_fd = _open_backup_directory(name, dir_fd=directory_fd)
                except OSError:
                    return False
                try:
                    if (
                        not _named_directory_matches(
                            directory_fd, name, child_fd)
                        or not _walk(child_fd, relative)
                    ):
                        return False
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(value.st_mode):
                paths.append(relative)
            else:
                return False
        return True

    return paths if _walk(root_fd, ()) else None


def _open_tree_file(root_fd, relative):
    parent_descriptors = _open_relative_directories(
        root_fd, relative[:-1], create=False)
    parent_fd = parent_descriptors[-1] if parent_descriptors else root_fd
    try:
        file_fd = _open_regular_file_at(parent_fd, relative[-1])
    except BaseException:
        _close_descriptors(parent_descriptors)
        raise
    return parent_descriptors, parent_fd, file_fd


def _tree_manifest(root_fd):
    paths = _scan_backup_tree(root_fd)
    if paths is None:
        return None
    manifest = {}
    try:
        for relative in paths:
            parents, _parent_fd, file_fd = _open_tree_file(root_fd, relative)
            try:
                value = os.fstat(file_fd)
                manifest[relative] = (
                    int(value.st_size),
                    _file_digest_fd(file_fd),
                )
            finally:
                os.close(file_fd)
                _close_descriptors(parents)
    except OSError:
        return None
    return manifest


def _snapshot_file(directory_fd, name):
    descriptor = _open_regular_file_at(directory_fd, name)
    try:
        receipt = _regular_file_receipt(
            descriptor, _file_digest_fd(descriptor))
        return {
            "identity": list(receipt["identity"]),
            "size": receipt["size"],
            "mtime_ns": receipt["mtime_ns"],
            "changed_ns": receipt["changed_ns"],
            "sha256": receipt["sha256"],
        }
    finally:
        os.close(descriptor)


def _exact_tree_snapshot(root_fd, *, ignore_root_names=()):
    """Describe every exact regular file and directory below a held root."""
    ignored = set(ignore_root_names)
    snapshot = {
        "root_identity": list(_entry_identity(os.fstat(root_fd))),
        "directories": {},
        "files": {},
    }

    def _walk(directory_fd, prefix):
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            return False
        for entry in entries:
            name = entry.name
            if name in ("", ".", ".."):
                return False
            if not prefix and name in ignored:
                continue
            relative = prefix + (name,)
            key = "/".join(relative)
            try:
                value = entry.stat(follow_symlinks=False)
            except OSError:
                return False
            if stat.S_ISDIR(value.st_mode):
                child_fd = None
                try:
                    child_fd = _open_backup_directory(
                        name, dir_fd=directory_fd)
                    if not _named_directory_matches(
                            directory_fd, name, child_fd):
                        return False
                    snapshot["directories"][key] = list(
                        _entry_identity(os.fstat(child_fd)))
                    if not _walk(child_fd, relative):
                        return False
                except OSError:
                    return False
                finally:
                    if child_fd is not None:
                        os.close(child_fd)
            elif stat.S_ISREG(value.st_mode):
                try:
                    snapshot["files"][key] = _snapshot_file(
                        directory_fd, name)
                except OSError:
                    return False
            else:
                # A link, FIFO, socket, or device cannot be bound to a regular
                # held descriptor for exact disposal.  Preserve the tree.
                return False
        return True

    try:
        return snapshot if _walk(root_fd, ()) else None
    except (OSError, TypeError, ValueError):
        return None


def _tree_matches_ignoring_ctime(current, expected) -> bool:
    """Match sealed trees across chown/chmod events, which bump only ctime."""
    if type(current) is not dict or type(expected) is not dict:
        return False
    if (
        current.get("root_identity") != expected.get("root_identity")
        or current.get("directories") != expected.get("directories")
        or type(current.get("files")) is not dict
        or type(expected.get("files")) is not dict
        or set(current["files"]) != set(expected["files"])
    ):
        return False
    for name, value in current["files"].items():
        original = expected["files"][name]
        if type(value) is not dict or type(original) is not dict:
            return False
        if (
            _fidelity_without(value, "changed_ns")
            != _fidelity_without(original, "changed_ns")
        ):
            return False
    return True


def _receipt_matches_ignoring_ctime(current, expected) -> bool:
    """Equate receipts whose sealed trees differ only in file ctimes."""
    if type(current) is not dict or type(expected) is not dict:
        return False
    return (
        _fidelity_without(current, "tree")
            == _fidelity_without(expected, "tree")
        and _tree_matches_ignoring_ctime(
            current.get("tree"), expected.get("tree"))
    )


def _run_backup_sigint_deferred(callback):
    """Defer Ctrl-C only across one raw-resource adoption."""
    return run_sigint_deferred(
        callback, detail="interrupt-safe snapshot access is unavailable")


def _hold_snapshot_files(root_fd, snapshot, held):
    """Hold and writer-exclude every exact regular file in a snapshot."""
    if type(held) is not dict or held:
        raise ValueError("snapshot file owner must be an empty dictionary")
    try:
        for relative_text, expected in snapshot["files"].items():
            relative = tuple(PurePosixPath(relative_text).parts)
            item = {
                "relative": relative,
                "parents": [],
                "parent_fd": root_fd,
                "descriptor": None,
                "lease": None,
            }

            def open_and_adopt():
                parents, parent_fd, descriptor = _open_tree_file(
                    root_fd, relative)
                item.update({
                    "relative": relative,
                    "parents": parents,
                    "parent_fd": parent_fd,
                    "descriptor": descriptor,
                })
                held[relative_text] = item

            _run_backup_sigint_deferred(open_and_adopt)

            def acquire_and_adopt():
                item["lease"] = acquire_inode_write_exclusion(
                    item["descriptor"])

            _run_backup_sigint_deferred(acquire_and_adopt)
            lease = item["lease"]
            if lease is None or not lease.intact():
                raise OSError(
                    "snapshotted file has an active or uncertain writer")
            if _snapshot_file(item["parent_fd"], relative[-1]) != expected:
                raise OSError("snapshotted file changed before exclusion")
        if _exact_tree_snapshot(root_fd) != snapshot:
            raise OSError("tree changed while writer exclusions were acquired")
        return held
    except BaseException as exc:
        try:
            _release_held_snapshot_files(held)
        except BaseException as cleanup_exc:
            if not isinstance(cleanup_exc, Exception):
                if hasattr(cleanup_exc, "add_note"):
                    cleanup_exc.add_note(
                        f"snapshot hold also failed: {exc}")
                raise
            if hasattr(exc, "add_note"):
                exc.add_note(
                    f"snapshot hold cleanup also failed: {cleanup_exc}")
        raise


def _release_held_snapshot_files_uninterruptibly(held) -> None:
    error = None

    def remember(candidate):
        nonlocal error
        if error is None or (
            isinstance(error, Exception)
            and not isinstance(candidate, Exception)
        ):
            error = candidate

    while held:
        _relative, item = held.popitem()
        publication = item.get("publication")
        item["publication"] = None
        lease = item.get("lease")
        item["lease"] = None
        descriptor = item.get("descriptor")
        item["descriptor"] = None
        parents = item.get("parents")
        item["parents"] = []

        if publication is not None:
            try:
                _release_copy_publication(publication)
            except BaseException as exc:
                remember(exc)
        else:
            if lease is not None:
                try:
                    lease.close()
                except BaseException as exc:
                    remember(exc)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    remember(exc)
        if isinstance(parents, list):
            while parents:
                parent = parents.pop()
                try:
                    os.close(parent)
                except BaseException as exc:
                    remember(exc)

    if error is not None:
        raise error.with_traceback(error.__traceback__)


def _release_held_snapshot_files(held) -> None:
    """Retire every held resource without losing ownership to Ctrl-C."""
    active = sys.exception()
    try:
        _run_backup_sigint_deferred(
            lambda: _release_held_snapshot_files_uninterruptibly(held))
    except BaseException as cleanup_exc:
        if (
            active is not None
            and not isinstance(active, Exception)
            and isinstance(cleanup_exc, Exception)
        ):
            if hasattr(active, "add_note"):
                active.add_note(
                    f"snapshot cleanup also failed: {cleanup_exc}")
            return
        raise


def _held_snapshot_files_intact(root_fd, snapshot, held) -> bool:
    if set(held) != set(snapshot["files"]):
        return False
    for relative_text, expected in snapshot["files"].items():
        item = held[relative_text]
        if (
            not item["lease"].intact()
            or not _relative_directories_are_named(
                root_fd, item["relative"][:-1], item["parents"])
            or not _named_entry_matches(
                item["parent_fd"],
                item["relative"][-1],
                item["descriptor"],
            )
        ):
            return False
        try:
            if _snapshot_file(
                    item["parent_fd"], item["relative"][-1]) != expected:
                return False
        except OSError:
            return False
    return True


def _fsync_exact_tree(root_fd, snapshot=None, held=None) -> bool:
    """Durably flush one exact held tree, including every payload inode."""
    snapshot = snapshot or _exact_tree_snapshot(root_fd)
    if snapshot is None:
        return False
    local_held = None
    try:
        if held is None:
            local_held = {}
            _hold_snapshot_files(root_fd, snapshot, local_held)
            held = local_held
        if not _held_snapshot_files_intact(root_fd, snapshot, held):
            return False
        for item in held.values():
            try:
                os.fsync(item["descriptor"])
            except OSError:
                return False
        for relative_text in sorted(
                snapshot["directories"], key=lambda value: value.count("/"),
                reverse=True):
            relative = tuple(PurePosixPath(relative_text).parts)
            parents = _open_relative_directories(root_fd, relative, create=False)
            try:
                directory_fd = parents[-1]
                if (
                    list(_entry_identity(os.fstat(directory_fd)))
                        != snapshot["directories"][relative_text]
                    or not _fsync_directory_fds(directory_fd)
                ):
                    return False
            finally:
                _close_descriptors(parents)
        return (
            _held_snapshot_files_intact(root_fd, snapshot, held)
            and _fsync_directory_fds(root_fd)
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if local_held is not None:
            _release_held_snapshot_files(local_held)


def _delete_exact_tree_contents(root_fd, snapshot, *, held=None,
                                commit_guard=None):
    """Delete only entries named by an already-verified exact snapshot."""
    if (
        _exact_tree_snapshot(root_fd) != snapshot
        or held is not None
        and not _held_snapshot_files_intact(root_fd, snapshot, held)
    ):
        raise OSError("tree changed before exact disposal")
    deferred = None
    unlinked_identities = set()
    for relative_text, expected in sorted(
            snapshot["files"].items(),
            key=lambda item: (item[0].count("/"), item[0]),
            reverse=True):
        relative = tuple(PurePosixPath(relative_text).parts)
        item = held.get(relative_text) if held is not None else None
        parents = []
        descriptor = None
        try:
            if item is None:
                parents = _open_relative_directories(
                    root_fd, relative[:-1], create=False)
                parent_fd = parents[-1] if parents else root_fd
                descriptor = _open_regular_file_at(parent_fd, relative[-1])
            else:
                parent_fd = item["parent_fd"]
                descriptor = item["descriptor"]
                if not item["lease"].intact():
                    raise OSError("writer exclusion broke during exact disposal")
            current = _snapshot_file(parent_fd, relative[-1])
            identity = tuple(expected["identity"])
            hardlink_ctime_only = (
                identity in unlinked_identities
                and {
                    key: value for key, value in current.items()
                    if key != "changed_ns"
                } == {
                    key: value for key, value in expected.items()
                    if key != "changed_ns"
                }
            )
            if current != expected and not hardlink_ctime_only:
                raise OSError("file changed during exact disposal")
            if commit_guard is not None and not commit_guard():
                raise OSError("replacement proof changed during exact disposal")
            unlink_exception = _unlink_exact_at(
                parent_fd, relative[-1], descriptor)
            unlinked_identities.add(identity)
            if deferred is None:
                deferred = unlink_exception
        finally:
            if descriptor is not None and item is None:
                os.close(descriptor)
            _close_descriptors(parents)

    for relative_text, expected_identity in sorted(
            snapshot["directories"].items(),
            key=lambda item: (item[0].count("/"), item[0]),
            reverse=True):
        relative = tuple(PurePosixPath(relative_text).parts)
        parents = _open_relative_directories(
            root_fd, relative[:-1], create=False)
        parent_fd = parents[-1] if parents else root_fd
        directory_fd = None
        try:
            directory_fd = _open_backup_directory(
                relative[-1], dir_fd=parent_fd)
            if (
                list(_entry_identity(os.fstat(directory_fd)))
                    != expected_identity
                or not _named_directory_matches(
                    parent_fd, relative[-1], directory_fd)
            ):
                raise OSError("directory changed during exact disposal")
            rmdir_exception = _rmdir_exact_at(
                parent_fd, relative[-1], directory_fd)
            if deferred is None:
                deferred = rmdir_exception
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            _close_descriptors(parents)
    return deferred


def _copy_tree_manifest_at(source_root_fd, destination_root_fd, manifest) -> bool:
    source_fidelity = _tree_fidelity_snapshot(source_root_fd)
    if source_fidelity is None:
        return False
    try:
        for relative, (expected_size, expected_digest) in manifest.items():
            source_parents, _source_parent_fd, source_fd = _open_tree_file(
                source_root_fd, relative)
            destination_parents = []
            publication = None
            try:
                source_value = os.fstat(source_fd)
                if (
                    int(source_value.st_size) != expected_size
                    or _file_digest_fd(source_fd) != expected_digest
                    or not _relative_directories_are_named(
                        source_root_fd,
                        relative[:-1],
                        source_parents,
                    )
                ):
                    return False
                destination_parents = _open_relative_directories(
                    destination_root_fd, relative[:-1], create=True)
                destination_parent_fd = (
                    destination_parents[-1]
                    if destination_parents else destination_root_fd)
                publication = _CopyPublication(
                    destination_parent_fd, relative[-1])
                copied_digest = _copy_file_noreplace_at(
                    source_fd, publication)
                destination_fd = publication.descriptor
                destination_lease = publication.lease
                if (
                    not destination_lease.intact()
                    or copied_digest != expected_digest
                    or _file_digest_fd(destination_fd) != expected_digest
                    or not _relative_directories_are_named(
                        destination_root_fd,
                        relative[:-1],
                        destination_parents,
                    )
                    or not _named_entry_matches(
                        destination_parent_fd,
                        relative[-1],
                        destination_fd,
                    )
                ):
                    return False
            finally:
                if publication is not None:
                    _release_copy_publication(publication)
                if source_fd is not None:
                    os.close(source_fd)
                _close_descriptors(destination_parents)
                _close_descriptors(source_parents)
        if not _copy_tree_directory_fidelity(
                source_root_fd, destination_root_fd):
            return False
        destination_fidelity = _tree_fidelity_snapshot(destination_root_fd)
        return (
            _tree_manifest(source_root_fd) == manifest
            and _tree_manifest(destination_root_fd) == manifest
            and _tree_fidelity_snapshot(source_root_fd) == source_fidelity
            and _tree_copy_fidelity_matches(
                _fidelity_without_root_sidecars(source_fidelity),
                destination_fidelity)
            and _fsync_directory_fds(destination_root_fd)
        )
    except (OSError, TypeError, ValueError):
        return False


def _tree_stats(d: Path):
    """(file_count, total_bytes) for tree d, or None on any stat/walk error."""
    n_files = 0
    n_bytes = 0
    entries = _list_tree(d)
    if entries is None:
        return None
    try:
        for f in entries:
            if f.is_file():
                n_files += 1
                try:
                    n_bytes += f.stat().st_size
                except OSError:
                    return None
    except OSError:
        return None
    return (n_files, n_bytes)


def _file_digest(path: Path) -> str:
    """sha256 hex of a file's bytes, read in chunks so a large FLAC isn't loaded
    into memory all at once."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_XATTR_UNSUPPORTED = {errno.EINVAL, errno.ENOTSUP,
                      getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def _fsync(path: Path) -> bool:
    """Force a file's bytes (or a directory's entries) to stable storage, and
    report whether the flush is trustworthy. A copy that's read back for
    hashing only proves the bytes are in the page cache; forcing them to disk
    before the original is deleted is what makes a verified copy survive a
    delayed-writeback failure (ENOSPC/EIO during the lazy flush).
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _fsync_tree(root: Path) -> bool:
    """fsync every file and directory under ``root`` (and root itself) so a
    verified copytree is durable before the source it mirrors is removed.
    Returns False when any flush genuinely failed (see _fsync) — the copy may
    exist only in the page cache, so a caller about to delete the source must
    keep it instead."""
    entries = _list_tree(root)
    if entries is None:
        return False
    ok = True
    try:
        for f in entries:
            if not f.is_symlink():
                ok = _fsync(f) and ok
    except OSError:
        ok = False
    return _fsync(root) and ok


def replacement_tree_durable(root: Path) -> bool:
    """Flush a completed replacement tree and the directory that names it.

    Call this after the final mutation and before deleting an original or
    backup. A logical scan can prove the files are present, but only this gate
    proves their bytes and directory entries are no longer just cached writes.
    """
    if root is None:
        return False
    try:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            return False
    except OSError:
        return False
    return _fsync_tree(root) and _fsync(root.parent)


def _tree_digest(d: Path):
    """{relative-path: (size, sha256)} for every regular file under tree d, or
    None on any stat/read error, an unexpected special file, or a symlink
    whose target points outside the tree. Content-verifies a cross-filesystem
    copy before the source is deleted: a same-size copy that differs by even
    one byte — a transfer glitch, a partial write that got re-padded — fails
    the match, so it can't pass as a valid backup and let the original be
    removed.
    """
    out = {}
    entries = _list_tree(d)
    if entries is None:
        return None
    try:
        base = d.resolve()
        for f in entries:
            rel = str(f.relative_to(d))
            if f.is_symlink():
                target = os.readlink(str(f))
                resolved = (Path(target) if os.path.isabs(target)
                            else f.parent / target).resolve()
                try:
                    resolved.relative_to(base)
                except ValueError:
                    return None  # target escapes the tree — can't verify its bytes
                out[rel] = ("symlink", target)
                continue
            if f.is_dir():
                continue
            if not f.is_file():
                return None  # socket/fifo/device — refuse rather than guess
            try:
                out[rel] = (f.stat().st_size, _file_digest(f))
            except OSError:
                return None
    except OSError:
        return None
    return out


# A backup records the folder it was taken from, so a sweep can tell a backup
# whose operation completed (origin rebuilt → safe to reap) from one orphaned by
# a hard kill that skipped the caller's restore/delete (origin still short → the
# backup may be the only copy). Lives inside the backup dir so rmtree clears it
# for free; restore strips it so it never lands in the live library.
_ORIGIN_SIDECAR = ".ql_backup_origin"

# Dropped into a backup when a restore left some originals behind (a partial
# restore). The leftover originals are the ONLY copy of those tracks; this marker
# says "never reap, the user must reconcile by hand." Content-presence reaping
# already keeps such a backup (the un-restored tracks aren't back at the origin),
# so this is an explicit belt-and-braces signal, not the sole protection.
_PARTIAL_RESTORE_SENTINEL = ".ql_partial_restore"

# Dropped into an upgrade backup kept because the re-rip couldn't be verified
# as complete (e.g. a track came back truncated-but-decodable, so playtime
# dropped). The backup is then the only fully-verified copy.
_UNVERIFIED_UPGRADE_SENTINEL = ".ql_upgrade_unverified"

# Dropped into a backup that exists only as an undo window — the downsample
# keep-originals copy. Its origin deliberately holds the SMALLER rewrite, so
# the content-presence proof below can never call it redundant; without this
# marker the sweep would keep it forever and the diagnostics would nag about
# a state the user asked for. It inverts the default: age alone reaps it.
_REAP_AFTER_RETENTION_SENTINEL = ".ql_reap_after_retention"

# Whole-album copies are sealed before their public source is retired.  The
# immutable receipt therefore starts incomplete and this receipt-bound marker
# is the only persisted transition to a caller-visible complete result.
_UPGRADE_RETIREMENT_COMPLETE_SENTINEL = ".ql_upgrade_retirement_complete"

# When source retirement cannot be proved complete, this marker binds the
# exact namespace state that an explicit recovery may reconcile later.
_UPGRADE_RECOVERY_STATE_SENTINEL = ".ql_upgrade_recovery_state"

# A companion carry is recoverable across an asynchronous interruption only
# when the retained backup says exactly what was planned and which private
# inodes were prepared before anything is published into the replacement.
# These immutable, receipt-bound records remain with the backup until its
# later exact disposal.
_COMPANION_CARRY_INTENT_SENTINEL = ".ql_companion_carry_intent"
_COMPANION_CARRY_READY_SENTINEL = ".ql_companion_carry_ready"
_COMPANION_CARRY_COMMITTED_SENTINEL = ".ql_companion_carry_committed"

# Immutable ownership receipt for exact disposal.  It binds the backup root,
# the receipt inode itself, and every regular file and directory below it.  A
# copied or replaced tree therefore cannot borrow a stale receipt and be
# mistaken for the backup this process created.
_RECEIPT_SIDECAR = ".ql_backup_receipt"
_RECEIPT_VERSION = 1
_OWNED_RECEIPT_VERSION = 2

# An ownerless disposal has no queue journal to adopt its private rename after
# a hard kill.  This record is committed inside the deterministic quarantine
# before that rename so the retention sweep can either restore the exact full
# carrier or leave an incomplete residue visibly quarantined for attention.
_DISPOSAL_MANIFEST = ".ql_disposal_manifest"
_DISPOSAL_MANIFEST_VERSION = 1
_MAX_DISPOSAL_MANIFEST_BYTES = 4 * 1024 * 1024

# Files a backup carries that aren't backed-up tracks.
_SIDECARS = (
    _ORIGIN_SIDECAR,
    _PARTIAL_RESTORE_SENTINEL,
    _UNVERIFIED_UPGRADE_SENTINEL,
    _REAP_AFTER_RETENTION_SENTINEL,
    _UPGRADE_RETIREMENT_COMPLETE_SENTINEL,
    _UPGRADE_RECOVERY_STATE_SENTINEL,
    _COMPANION_CARRY_INTENT_SENTINEL,
    _COMPANION_CARRY_READY_SENTINEL,
    _COMPANION_CARRY_COMMITTED_SENTINEL,
    _RECEIPT_SIDECAR,
)

# These may be added after the immutable ownership receipt is published. Their
# contents are bound to that receipt's random token; they are therefore kept
# outside the immutable payload snapshot rather than invalidating it.
_OPTIONAL_RECEIPT_MARKERS = (
    _PARTIAL_RESTORE_SENTINEL,
    _UNVERIFIED_UPGRADE_SENTINEL,
    _UPGRADE_RETIREMENT_COMPLETE_SENTINEL,
    _UPGRADE_RECOVERY_STATE_SENTINEL,
    _COMPANION_CARRY_INTENT_SENTINEL,
    _COMPANION_CARRY_READY_SENTINEL,
    _COMPANION_CARRY_COMMITTED_SENTINEL,
)


def _write_backup_receipt(directory_fd, origin, *, kind, complete,
                          requested, backed_up, source_receipt=None,
                          owner=None):
    if not _named_entry_missing(directory_fd, _RECEIPT_SIDECAR):
        return None
    tree = _exact_tree_snapshot(
        directory_fd,
        ignore_root_names=(_RECEIPT_SIDECAR, *_OPTIONAL_RECEIPT_MARKERS),
    )
    if tree is None:
        return None
    descriptor = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return None
        descriptor = os.open(
            _RECEIPT_SIDECAR,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        receipt = {
            "version": (
                _OWNED_RECEIPT_VERSION
                if owner is not None else _RECEIPT_VERSION
            ),
            "token": secrets.token_hex(32),
            "kind": str(kind),
            "complete": bool(complete),
            "origin": os.fspath(origin),
            "requested": int(requested),
            "backed_up": int(backed_up),
            "receipt_identity": list(
                _entry_identity(os.fstat(descriptor))),
            "tree": tree,
        }
        if owner is not None:
            receipt["owner"] = normalise_recovery_owner(owner)
        if source_receipt is not None:
            source_receipt = _canonical_receipt(source_receipt)
            if source_receipt is None:
                raise ValueError("source receipt is not serializable")
            receipt["source_receipt"] = source_receipt
        data = json.dumps(
            receipt, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("backup receipt write made no progress")
            offset += written
        os.fsync(descriptor)
        if (
            not _named_entry_matches(
                directory_fd, _RECEIPT_SIDECAR, descriptor)
            or not _fsync_directory_fds(directory_fd)
        ):
            raise OSError("backup receipt could not be committed")
        return receipt
    except BaseException as exc:
        if (
            descriptor is not None
            and _named_entry_matches(
                directory_fd, _RECEIPT_SIDECAR, descriptor)
        ):
            try:
                _remove_exact_file_at(
                    directory_fd,
                    _RECEIPT_SIDECAR,
                    descriptor,
                    prefix="ql-receipt-remove",
                )
            except BaseException:
                pass
        if not isinstance(
                exc,
                (OSError, TypeError, ValueError, json.JSONDecodeError),
        ):
            raise
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _receipt_path_key_valid(value, *, allow_root=False) -> bool:
    if type(value) is not str or "\x00" in value:
        return False
    if value == "":
        return allow_root
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and all(part not in ("", ".", "..") for part in path.parts)
        and "/".join(path.parts) == value
    )


def _receipt_identity_schema_valid(value, entry_type) -> bool:
    return (
        type(value) is list
        and len(value) == 3
        and all(type(item) is int for item in value)
        and value[0] == entry_type
        and value[1] >= 0
        and value[2] >= 0
    )


def _receipt_digest_valid(value) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _receipt_file_snapshot_schema_valid(value) -> bool:
    return (
        type(value) is dict
        and set(value) == {
            "identity", "size", "mtime_ns", "changed_ns", "sha256"}
        and _receipt_identity_schema_valid(
            value["identity"], stat.S_IFREG)
        and type(value["size"]) is int
        and value["size"] >= 0
        and type(value["mtime_ns"]) is int
        and type(value["changed_ns"]) is int
        and _receipt_digest_valid(value["sha256"])
    )


def _tree_snapshot_schema_valid(value) -> bool:
    if (
        type(value) is not dict
        or set(value) != {"root_identity", "directories", "files"}
        or not _receipt_identity_schema_valid(
            value["root_identity"], stat.S_IFDIR)
        or type(value["directories"]) is not dict
        or type(value["files"]) is not dict
    ):
        return False
    directories = value["directories"]
    files = value["files"]
    if set(directories) & set(files):
        return False
    if not (
        all(
            _receipt_path_key_valid(relative)
            and _receipt_identity_schema_valid(identity, stat.S_IFDIR)
            for relative, identity in directories.items()
        )
        and all(
            _receipt_path_key_valid(relative)
            and _receipt_file_snapshot_schema_valid(snapshot)
            for relative, snapshot in files.items()
        )
    ):
        return False
    for relative in (*directories, *files):
        parts = PurePosixPath(relative).parts
        for length in range(1, len(parts)):
            if "/".join(parts[:length]) not in directories:
                return False
    return True


def _serialized_xattrs_schema_valid(value) -> bool:
    if type(value) is not dict:
        return False
    return all(
        type(name) is str
        and bool(name)
        and "\x00" not in name
        and type(payload) is str
        and len(payload) % 2 == 0
        and all(character in "0123456789abcdef" for character in payload)
        for name, payload in value.items()
    )


def _directory_fidelity_schema_valid(value) -> bool:
    return (
        type(value) is dict
        and set(value) == {
            "type", "mode", "uid", "gid", "xattrs", "mtime_ns"}
        and type(value["type"]) is int
        and value["type"] == stat.S_IFDIR
        and type(value["mode"]) is int
        and 0 <= value["mode"] <= 0o7777
        and type(value["uid"]) is int
        and value["uid"] >= 0
        and type(value["gid"]) is int
        and value["gid"] >= 0
        and _serialized_xattrs_schema_valid(value["xattrs"])
        and type(value["mtime_ns"]) is int
    )


def _file_fidelity_schema_valid(value) -> bool:
    return (
        type(value) is dict
        and set(value) == {
            "type", "mode", "uid", "gid", "xattrs", "size",
            "mtime_ns", "sha256", "links",
        }
        and type(value["type"]) is int
        and value["type"] == stat.S_IFREG
        and type(value["mode"]) is int
        and 0 <= value["mode"] <= 0o7777
        and type(value["uid"]) is int
        and value["uid"] >= 0
        and type(value["gid"]) is int
        and value["gid"] >= 0
        and _serialized_xattrs_schema_valid(value["xattrs"])
        and type(value["size"]) is int
        and value["size"] >= 0
        and type(value["mtime_ns"]) is int
        and _receipt_digest_valid(value["sha256"])
        and type(value["links"]) is int
        and value["links"] > 0
    )


def _directory_generation_schema_valid(value) -> bool:
    return (
        type(value) is list
        and len(value) == 7
        and all(type(item) is int for item in value)
        and value[0] == stat.S_IFDIR
        and value[1] >= 0
        and value[2] >= 0
        and value[3] == stat.S_IFDIR | stat.S_IMODE(value[3])
        and 0 <= value[5] < 1_000_000_000
        and value[6] > 0
    )


def _source_receipt_nested_schema_valid(receipt, origin=None) -> bool:
    if (
        type(receipt) is not dict
        or set(receipt) != {
            "version", "origin", "tree", "fidelity",
            "directory_generations", "path_generations",
        }
        or type(receipt["version"]) is not int
        or receipt["version"] != 1
        or type(receipt["origin"]) is not str
        or not receipt["origin"]
        or "\x00" in receipt["origin"]
        or not _tree_snapshot_schema_valid(receipt["tree"])
        or type(receipt["fidelity"]) is not dict
        or set(receipt["fidelity"]) != {"directories", "files"}
        or type(receipt["fidelity"]["directories"]) is not dict
        or type(receipt["fidelity"]["files"]) is not dict
        or type(receipt["directory_generations"]) is not dict
        or type(receipt["path_generations"]) is not list
        or not receipt["path_generations"]
    ):
        return False
    try:
        if origin is not None and receipt["origin"] != os.fspath(origin):
            return False
    except (OSError, TypeError, ValueError):
        return False

    tree = receipt["tree"]
    fidelity = receipt["fidelity"]
    generations = receipt["directory_generations"]
    directory_keys = set(tree["directories"]) | {""}
    file_keys = set(tree["files"])
    if (
        set(fidelity["directories"]) != directory_keys
        or set(fidelity["files"]) != file_keys
        or set(generations) != directory_keys
        or not all(
            _receipt_path_key_valid(relative, allow_root=True)
            and _directory_fidelity_schema_valid(value)
            for relative, value in fidelity["directories"].items()
        )
        or not all(
            _receipt_path_key_valid(relative)
            and _file_fidelity_schema_valid(value)
            for relative, value in fidelity["files"].items()
        )
        or not all(
            _receipt_path_key_valid(relative, allow_root=True)
            and _directory_generation_schema_valid(value)
            for relative, value in generations.items()
        )
        or not all(
            _directory_generation_schema_valid(value)
            for value in receipt["path_generations"]
        )
    ):
        return False

    for relative in directory_keys:
        identity = (
            tree["root_identity"] if relative == ""
            else tree["directories"][relative]
        )
        if generations[relative][:3] != identity:
            return False
    for relative in file_keys:
        snapshot = tree["files"][relative]
        metadata = fidelity["files"][relative]
        if (
            metadata["size"] != snapshot["size"]
            or metadata["mtime_ns"] != snapshot["mtime_ns"]
            or metadata["sha256"] != snapshot["sha256"]
        ):
            return False
    return receipt["path_generations"][-1] == generations[""]


def _backup_receipt_schema_valid(receipt) -> bool:
    """Accept the legacy schema and the exact owned kind/source schemas."""
    fields = {
        "version", "token", "kind", "complete", "origin",
        "requested", "backed_up", "receipt_identity", "tree",
    }
    if type(receipt) is not dict or type(receipt.get("version")) is not int:
        return False
    if (
        not _receipt_identity_schema_valid(
            receipt.get("receipt_identity"), stat.S_IFREG)
        or not _tree_snapshot_schema_valid(receipt.get("tree"))
    ):
        return False
    if receipt["version"] == _RECEIPT_VERSION:
        if set(receipt) == fields:
            return True
        return (
            set(receipt) == fields | {"source_receipt"}
            and _source_receipt_nested_schema_valid(
                receipt["source_receipt"], receipt.get("origin"))
        )
    if receipt["version"] != _OWNED_RECEIPT_VERSION:
        return False
    try:
        owner = normalise_recovery_owner(receipt.get("owner"))
        if owner is None or owner != receipt["owner"]:
            return False
    except (KeyError, ValueError):
        return False
    if receipt.get("kind") == "upgrade":
        return (
            set(receipt) == fields | {"owner", "source_receipt"}
            and _source_receipt_nested_schema_valid(
                receipt["source_receipt"], receipt.get("origin"))
        )
    if receipt.get("kind") == "gap-fill":
        return set(receipt) == fields | {"owner"}
    return False


def _backup_receipt_value_schema_valid(receipt) -> bool:
    return (
        _backup_receipt_schema_valid(receipt)
        and _receipt_digest_valid(receipt["token"])
        and type(receipt["kind"]) is str
        and type(receipt["complete"]) is bool
        and type(receipt["origin"]) is str
        and bool(receipt["origin"])
        and "\x00" not in receipt["origin"]
        and type(receipt["requested"]) is int
        and receipt["requested"] >= 0
        and type(receipt["backed_up"]) is int
        and 0 <= receipt["backed_up"] <= receipt["requested"]
    )


def _read_backup_receipt(directory_fd):
    descriptor = None
    try:
        descriptor = _open_regular_file_at(
            directory_fd, _RECEIPT_SIDECAR)
        value = os.fstat(descriptor)
        if value.st_size <= 0 or value.st_size > 1024 * 1024:
            return None
        raw = os.pread(descriptor, value.st_size + 1, 0)
        if len(raw) != value.st_size:
            return None
        receipt = decode_recovery_json(raw)
        if (
            not _backup_receipt_value_schema_valid(receipt)
            or receipt["receipt_identity"]
                != list(_entry_identity(value))
            or not _named_entry_matches(
                directory_fd, _RECEIPT_SIDECAR, descriptor)
        ):
            return None
        if (
            "source_receipt" in receipt
            and _canonical_receipt(receipt["source_receipt"])
                != receipt["source_receipt"]
        ):
            return None
        current = _exact_tree_snapshot(
            directory_fd,
            ignore_root_names=(
                _RECEIPT_SIDECAR, *_OPTIONAL_RECEIPT_MARKERS),
        )
        if current != receipt["tree"]:
            if not _tree_matches_ignoring_ctime(current, receipt["tree"]):
                return None
            # Ownership or permission fixes bump every ctime under the sealed
            # values; adopt the live tree so exact checks bind to it from here.
            receipt["tree"] = current
        return receipt
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _seal_backup_result(path, directory_fd, origin, *, kind, complete,
                        requested, backed_up, source_receipt=None, owner=None):
    durable_snapshot = _exact_tree_snapshot(
        directory_fd,
        ignore_root_names=(_RECEIPT_SIDECAR, *_OPTIONAL_RECEIPT_MARKERS),
    )
    if (
        durable_snapshot is None
        or not _fsync_exact_tree(directory_fd)
    ):
        return BackupResult(
            Path(path),
            complete=False,
            receipt=None,
            requested=int(requested),
            backed_up=int(backed_up),
        )
    receipt = _write_backup_receipt(
        directory_fd,
        origin,
        kind=kind,
        complete=complete,
        requested=requested,
        backed_up=backed_up,
        source_receipt=source_receipt,
        owner=owner,
    )
    return BackupResult(
        Path(path),
        complete=bool(complete and receipt is not None),
        receipt=receipt,
        requested=int(requested),
        backed_up=int(backed_up),
    )


def load_backup_result(path, *, expected_owner=None):
    """Load and validate a persisted backup transaction receipt."""
    if expected_owner is not None:
        try:
            expected_owner = normalise_recovery_owner(expected_owner)
        except ValueError:
            return None
    opened = _open_rooted_directory(cfg.UPGRADE_BACKUP_DIR, Path(path))
    if opened is None:
        return None
    _public, _root, _parts, descriptors = opened
    try:
        receipt = _read_backup_receipt(descriptors[-1])
        if receipt is None:
            return None
        if (
            expected_owner is not None
            and not recovery_owner_matches(
                receipt.get("owner"), expected_owner)
        ):
            return None
        complete = _effective_backup_complete(descriptors[-1], receipt)
        if complete is None:
            return None
        return BackupResult(
            Path(path),
            complete=complete,
            receipt=receipt,
            requested=receipt["requested"],
            backed_up=receipt["backed_up"],
        )
    finally:
        _close_descriptors(descriptors)


def canonical_album_source_receipt(value, *, expected_origin=None):
    """Return one closed-schema album receipt, or ``None`` when malformed."""
    if not _source_receipt_nested_schema_valid(value, expected_origin):
        return None
    canonical = _canonical_receipt(value)
    return canonical if canonical == value else None


def canonical_library_backup_intent(value, *, expected_owner=None):
    """Validate the durable hand-off made before library source mutation."""
    try:
        owner = normalise_recovery_owner(expected_owner)
        supplied_owner = normalise_recovery_owner(value.get("owner"))
    except (AttributeError, ValueError):
        return None
    if owner is None or supplied_owner != owner:
        return None
    common = {"version", "kind", "owner", "path", "origin"}
    kind = value.get("kind")
    extra = (
        {"source_receipt"}
        if kind == "upgrade"
        else {"source_receipts"}
        if kind == "gap-fill"
        else None
    )
    path = value.get("path")
    origin = value.get("origin")
    try:
        backup_root = Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))
        backup_path = Path(path)
        origin_path = Path(origin)
    except (OSError, TypeError, ValueError):
        return None
    if (
        extra is None
        or type(value) is not dict
        or set(value) != common | extra
        or type(value.get("version")) is not int
        or value["version"] != 1
        or type(path) is not str
        or not os.path.isabs(path)
        or os.path.abspath(path) != path
        or backup_path.parent != backup_root
        or type(origin) is not str
        or not os.path.isabs(origin)
        or os.path.abspath(origin) != origin
        or "\x00" in path
        or "\x00" in origin
        or not origin_path.name
    ):
        return None
    if kind == "upgrade":
        source = canonical_album_source_receipt(
            value.get("source_receipt"),
            expected_origin=origin,
        )
        if source is None:
            return None
        payload = dict(value)
        payload["source_receipt"] = source
    else:
        try:
            sources = _normalise_gap_fill_expected_receipts(
                value.get("source_receipts")
            )
        except OSError:
            return None
        if sources != value.get("source_receipts"):
            return None
        payload = dict(value)
        payload["source_receipts"] = sources
    payload["owner"] = dict(owner)
    return payload


def canonical_library_backup_record(value, *, expected_owner=None):
    """Validate a journal copy of one exact owner-bound ``BackupResult``."""
    try:
        owner = normalise_recovery_owner(expected_owner)
        supplied_owner = normalise_recovery_owner(value.get("owner"))
    except (AttributeError, ValueError):
        return None
    fields = {
        "version", "kind", "owner", "path", "origin", "complete",
        "requested", "backed_up", "receipt",
    }
    receipt = value.get("receipt") if type(value) is dict else None
    path = value.get("path") if type(value) is dict else None
    try:
        backup_root = Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))
        backup_path = Path(path)
    except (OSError, TypeError, ValueError):
        return None
    if (
        owner is None
        or supplied_owner != owner
        or type(value) is not dict
        or set(value) != fields
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("kind") not in {"upgrade", "gap-fill"}
        or type(path) is not str
        or not os.path.isabs(path)
        or os.path.abspath(path) != path
        or backup_path.parent != backup_root
        or not _backup_receipt_value_schema_valid(receipt)
        or receipt.get("version") != _OWNED_RECEIPT_VERSION
        or not recovery_owner_matches(receipt.get("owner"), owner)
        or value["kind"] != receipt["kind"]
        or value.get("origin") != receipt["origin"]
        or type(value.get("complete")) is not bool
        or (
            receipt["kind"] != "upgrade"
            and value["complete"] != receipt["complete"]
        )
        or (receipt["complete"] is True and value["complete"] is not True)
        or type(value.get("requested")) is not int
        or value["requested"] != receipt["requested"]
        or type(value.get("backed_up")) is not int
        or value["backed_up"] != receipt["backed_up"]
    ):
        return None
    payload = dict(value)
    payload["owner"] = dict(owner)
    payload["receipt"] = json.loads(json.dumps(receipt, allow_nan=False))
    return payload


def _library_backup_disposal_quarantine_name(receipt, expected_owner):
    """Derive one private name from an immutable backup receipt."""
    if not isinstance(receipt, dict) or not _receipt_digest_valid(
            receipt.get("token")):
        return None
    version = receipt.get("version")
    if version == _OWNED_RECEIPT_VERSION:
        try:
            owner = normalise_recovery_owner(expected_owner)
            receipt_owner = normalise_recovery_owner(receipt.get("owner"))
        except ValueError:
            return None
        if owner is None or receipt_owner != owner:
            return None
    elif version == _RECEIPT_VERSION:
        if expected_owner is not None or "owner" in receipt:
            return None
        owner = None
    else:
        return None
    material = json.dumps(
        {"owner": owner, "token": receipt["token"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f".ql-dispose-backup-{hashlib.sha256(material).hexdigest()}"


def _disposal_snapshot_matches_receipt(snapshot, receipt) -> bool:
    """Bind one exact disposable tree to its immutable backup receipt."""
    if (
        not _backup_receipt_value_schema_valid(receipt)
        or not _tree_snapshot_schema_valid(snapshot)
    ):
        return False
    immutable = receipt["tree"]
    special = {_RECEIPT_SIDECAR, *_OPTIONAL_RECEIPT_MARKERS}
    extra_files = set(snapshot["files"]) - set(immutable["files"])
    return not (
        snapshot["root_identity"] != immutable["root_identity"]
        or snapshot["directories"] != immutable["directories"]
        or any(
            PurePosixPath(name).parts[0] in special
            for name in immutable["files"]
        )
        or any(
            PurePosixPath(name).parts[0] in special
            for name in immutable["directories"]
        )
        or not set(immutable["files"]) <= set(snapshot["files"])
        or any(
            type(snapshot["files"].get(name)) is not dict
            or _fidelity_without(snapshot["files"][name], "changed_ns")
                != _fidelity_without(expected, "changed_ns")
            for name, expected in immutable["files"].items()
        )
        or _RECEIPT_SIDECAR not in extra_files
        or not extra_files <= special
        or snapshot["files"][_RECEIPT_SIDECAR]["identity"]
            != receipt["receipt_identity"]
    )


def _canonical_ownerless_disposal_manifest(
        value, *, expected_quarantine_name=None):
    """Validate the closed, root-bound record for a direct disposal."""
    if type(value) is not dict:
        return None
    fields = {
        "version",
        "carrier_name",
        "carrier_path",
        "quarantine_name",
        "receipt",
        "snapshot",
    }
    receipt = value.get("receipt")
    snapshot = value.get("snapshot")
    carrier_name = value.get("carrier_name")
    carrier_path = value.get("carrier_path")
    quarantine_name = value.get("quarantine_name")
    try:
        backup_root = Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))
        carrier = Path(carrier_path)
    except (OSError, TypeError, ValueError):
        return None
    expected_name = _library_backup_disposal_quarantine_name(receipt, None)
    if (
        set(value) != fields
        or type(value.get("version")) is not int
        or value["version"] != _DISPOSAL_MANIFEST_VERSION
        or type(carrier_name) is not str
        or not carrier_name
        or carrier_name in {".", ".."}
        or carrier_name.startswith(".")
        or PurePosixPath(carrier_name).parts != (carrier_name,)
        or "\x00" in carrier_name
        or type(carrier_path) is not str
        or not os.path.isabs(carrier_path)
        or os.path.abspath(carrier_path) != carrier_path
        or carrier.parent != backup_root
        or carrier.name != carrier_name
        or not _backup_receipt_value_schema_valid(receipt)
        or receipt.get("version") != _RECEIPT_VERSION
        or "owner" in receipt
        or not _disposal_snapshot_matches_receipt(snapshot, receipt)
        or quarantine_name != expected_name
        or expected_quarantine_name is not None
        and quarantine_name != expected_quarantine_name
    ):
        return None
    try:
        canonical = json.loads(json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return canonical if canonical == value else None


def _read_ownerless_disposal_manifest_at(
        quarantine_fd, *, expected_quarantine_name=None):
    """Open and validate one exact no-follow outer disposal manifest."""
    descriptor = None
    try:
        descriptor = _open_regular_file_at(
            quarantine_fd, _DISPOSAL_MANIFEST)
        value = os.fstat(descriptor)
        if (
            value.st_size <= 0
            or value.st_size > _MAX_DISPOSAL_MANIFEST_BYTES
        ):
            raise OSError("disposal manifest size is invalid")
        raw = os.pread(descriptor, value.st_size + 1, 0)
        if len(raw) != value.st_size:
            raise OSError("disposal manifest changed while it was read")
        parsed = decode_recovery_json(
            raw, max_bytes=_MAX_DISPOSAL_MANIFEST_BYTES)
        manifest = _canonical_ownerless_disposal_manifest(
            parsed,
            expected_quarantine_name=expected_quarantine_name,
        )
        if (
            manifest is None
            or not _named_entry_matches(
                quarantine_fd, _DISPOSAL_MANIFEST, descriptor)
        ):
            raise OSError("disposal manifest is invalid")
        return manifest, descriptor
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        if descriptor is not None:
            os.close(descriptor)
        return None, None


def _write_ownerless_disposal_manifest_at(quarantine_fd, manifest):
    """Commit one strict manifest and return its exact held descriptor."""
    canonical = _canonical_ownerless_disposal_manifest(manifest)
    if canonical is None:
        return None
    try:
        payload = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    if len(payload.encode("utf-8")) > _MAX_DISPOSAL_MANIFEST_BYTES:
        return None
    descriptor = None
    committed = False
    try:
        if not _write_text_noreplace_at(
                quarantine_fd, _DISPOSAL_MANIFEST, payload):
            return None
        descriptor = _open_regular_file_at(
            quarantine_fd, _DISPOSAL_MANIFEST)
        value = os.fstat(descriptor)
        raw = os.pread(descriptor, value.st_size + 1, 0)
        persisted = decode_recovery_json(
            raw, max_bytes=_MAX_DISPOSAL_MANIFEST_BYTES)
        if (
            len(raw) != value.st_size
            or persisted != canonical
            or not _named_entry_matches(
                quarantine_fd, _DISPOSAL_MANIFEST, descriptor)
            or not _fsync_directory_fds(quarantine_fd)
        ):
            raise OSError("disposal manifest did not persist exactly")
        committed = True
        return descriptor
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        return None
    finally:
        if not committed:
            if descriptor is None:
                try:
                    descriptor = _open_regular_file_at(
                        quarantine_fd, _DISPOSAL_MANIFEST)
                except OSError:
                    descriptor = None
            if descriptor is not None and _named_entry_matches(
                    quarantine_fd, _DISPOSAL_MANIFEST, descriptor):
                try:
                    _unlink_exact_at(
                        quarantine_fd, _DISPOSAL_MANIFEST, descriptor)
                except BaseException:
                    pass
            if descriptor is not None:
                os.close(descriptor)


def canonical_library_backup_disposal_record(
        value, carrier_record, *, expected_owner=None):
    """Validate exact, durable authority for one backup disposal attempt."""
    carrier = canonical_library_backup_record(
        carrier_record,
        expected_owner=expected_owner,
    )
    if carrier is None or type(value) is not dict:
        return None
    receipt = carrier["receipt"]
    snapshot = value.get("snapshot")
    expected_name = _library_backup_disposal_quarantine_name(
        receipt, expected_owner)
    if (
        set(value) != {"version", "quarantine_name", "snapshot"}
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("quarantine_name") != expected_name
        or not _disposal_snapshot_matches_receipt(snapshot, receipt)
    ):
        return None
    try:
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def library_backup_record(backup, *, expected_owner=None):
    """Seal a live ``BackupResult`` into the queue journal's closed schema."""
    if not isinstance(backup, BackupResult) or backup.receipt is None:
        return None
    value = {
        "version": 1,
        "kind": backup.receipt.get("kind"),
        "owner": backup.receipt.get("owner"),
        "path": os.fspath(backup.path),
        "origin": backup.receipt.get("origin"),
        "complete": backup.complete,
        "requested": backup.requested,
        "backed_up": backup.backed_up,
        "receipt": backup.receipt,
    }
    canonical = canonical_library_backup_record(
        value,
        expected_owner=expected_owner,
    )
    if canonical is None:
        return None
    reopened = load_backup_result(
        backup.path,
        expected_owner=expected_owner,
    )
    if (
        reopened is None
        or reopened.complete != backup.complete
        or reopened.requested != backup.requested
        or reopened.backed_up != backup.backed_up
        or not _receipt_matches_ignoring_ctime(
            reopened.receipt, backup.receipt)
    ):
        return None
    return canonical


def load_library_backup_record(value, *, expected_owner=None):
    """Reopen a journal carrier only when its exact receipt still matches."""
    canonical = canonical_library_backup_record(
        value,
        expected_owner=expected_owner,
    )
    if canonical is None:
        return None
    reopened = load_backup_result(
        canonical["path"],
        expected_owner=expected_owner,
    )
    if (
        reopened is None
        or reopened.complete != canonical["complete"]
        or reopened.requested != canonical["requested"]
        or reopened.backed_up != canonical["backed_up"]
        or not _receipt_matches_ignoring_ctime(
            reopened.receipt, canonical["receipt"])
    ):
        return None
    return reopened


def library_backup_record_absent(value, *, expected_owner=None) -> bool:
    """Prove an exact carried backup name is absent from the real backup root."""
    canonical = canonical_library_backup_record(
        value,
        expected_owner=expected_owner,
    )
    if canonical is None:
        return False
    root_fd = None
    try:
        backup_root = Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))
        backup_path = Path(canonical["path"])
        if backup_path.parent != backup_root:
            return False
        root_fd = _open_backup_directory(backup_root)
        return (
            _backup_root_is_public(backup_root, root_fd)
            and _named_entry_missing(root_fd, backup_path.name)
            and _backup_root_is_public(backup_root, root_fd)
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if root_fd is not None:
            os.close(root_fd)


def library_backup_matches_intent(backup_record, intent, *, expected_owner=None):
    """Bind a returned or restart-adopted carrier to its pre-mutation intent."""
    carrier = canonical_library_backup_record(
        backup_record,
        expected_owner=expected_owner,
    )
    planned = canonical_library_backup_intent(
        intent,
        expected_owner=expected_owner,
    )
    if (
        carrier is None
        or planned is None
        or carrier["kind"] != planned["kind"]
        or carrier["path"] != planned["path"]
        or carrier["origin"] != planned["origin"]
    ):
        return False
    receipt = carrier["receipt"]
    if carrier["kind"] == "upgrade":
        return receipt.get("source_receipt") == planned["source_receipt"]
    sources = planned["source_receipts"]
    files = {
        relative: snapshot
        for relative, snapshot in receipt["tree"]["files"].items()
        if relative not in _SIDECARS
    }
    if (
        carrier["requested"] != len(sources)
        or carrier["backed_up"] != len(files)
        or not set(files) <= set(sources)
    ):
        return False
    return all(
        files[relative]["size"] == sources[relative]["size"]
        and files[relative]["sha256"] == sources[relative]["sha256"]
        for relative in files
    )


def _validated_backup_result(backup, *, origin=None, kinds=None,
                             require_complete=False):
    if not isinstance(backup, BackupResult) or backup.receipt is None:
        return None
    opened = _open_rooted_directory(
        cfg.UPGRADE_BACKUP_DIR, backup.path)
    if opened is None:
        return None
    _public, _root, _parts, descriptors = opened
    receipt = _read_backup_receipt(descriptors[-1])
    effective_complete = (
        _effective_backup_complete(descriptors[-1], receipt)
        if receipt is not None else None
    )
    supplied_receipt_valid = (
        _backup_receipt_value_schema_valid(backup.receipt)
        and type(backup.requested) is int
        and backup.requested >= 0
        and type(backup.backed_up) is int
        and 0 <= backup.backed_up <= backup.requested
        and type(backup.complete) is bool
    )
    valid = (
        supplied_receipt_valid
        and receipt is not None
        and effective_complete is not None
        and _receipt_matches_ignoring_ctime(receipt, backup.receipt)
        and receipt["requested"] == backup.requested
        and receipt["backed_up"] == backup.backed_up
        and effective_complete is backup.complete
        and (not require_complete or effective_complete is True)
        and (kinds is None or receipt["kind"] in kinds)
    )
    if origin is not None:
        try:
            expected_origin = os.fspath(Path(os.path.abspath(os.fspath(origin))))
        except (OSError, TypeError, ValueError):
            valid = False
        else:
            valid = valid and receipt["origin"] == expected_origin
    if not valid:
        _close_descriptors(descriptors)
        return None
    return opened


def _backup_owner_authorized(backup, expected_owner) -> bool:
    """Require the exact owner before an owned receipt may be mutated."""
    if not isinstance(backup, BackupResult) or not isinstance(
            backup.receipt, dict):
        return False
    version = backup.receipt.get("version")
    if type(version) is not int:
        return False
    if version == _RECEIPT_VERSION:
        return expected_owner is None and "owner" not in backup.receipt
    if version != _OWNED_RECEIPT_VERSION:
        return False
    try:
        expected_owner = normalise_recovery_owner(expected_owner)
    except ValueError:
        return False
    return recovery_owner_matches(
        backup.receipt.get("owner"), expected_owner)


def _read_exact_text_at(directory_fd, name, *, max_bytes=1024 * 1024):
    descriptor = _open_regular_file_at(directory_fd, name)
    try:
        value = os.fstat(descriptor)
        if value.st_size < 0 or value.st_size > max_bytes:
            return None
        raw = os.pread(descriptor, value.st_size + 1, 0)
        if len(raw) != value.st_size:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(descriptor)


def _marker_text(receipt, note) -> str:
    return json.dumps(
        {"token": receipt["token"], "note": str(note)},
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_receipt_marker(backup, name, note) -> bool:
    opened = _validated_backup_result(backup)
    if opened is None or name not in _OPTIONAL_RECEIPT_MARKERS:
        return False
    _public, _root, _parts, descriptors = opened
    directory_fd = descriptors[-1]
    try:
        expected = _marker_text(backup.receipt, note)
        try:
            existing = _read_exact_text_at(directory_fd, name)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            return existing == expected
        if not _named_entry_missing(directory_fd, name):
            return False
        if not _write_text_noreplace_at(directory_fd, name, expected):
            return False
        return (
            _read_exact_text_at(directory_fd, name) == expected
            and _receipt_matches_ignoring_ctime(
                _read_backup_receipt(directory_fd), backup.receipt)
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        _close_descriptors(descriptors)


def _receipt_owned_marker(directory_fd, name, receipt) -> bool:
    try:
        raw = _read_exact_text_at(directory_fd, name)
    except FileNotFoundError:
        return False
    if raw is None:
        return False
    try:
        value = decode_recovery_json(raw)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and set(value) == {"token", "note"}
        and value["token"] == receipt["token"]
        and isinstance(value["note"], str)
    )


def _receipt_marker_note(directory_fd, name, receipt):
    """Return one receipt-bound marker note, or None for absent/invalid."""
    try:
        raw = _read_exact_text_at(directory_fd, name)
        value = decode_recovery_json(raw)
    except (FileNotFoundError, TypeError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"token", "note"}
        or value.get("token") != receipt.get("token")
        or not isinstance(value.get("note"), str)
    ):
        return None
    return value["note"]


def _effective_backup_complete(directory_fd, receipt):
    """Derive durable transaction completion from an immutable receipt."""
    try:
        if receipt["kind"] != "upgrade" or receipt["complete"] is True:
            return receipt["complete"]
        marker_missing = _named_entry_missing(
            directory_fd, _UPGRADE_RETIREMENT_COMPLETE_SENTINEL)
        if marker_missing:
            return False
        if not _receipt_owned_marker(
                directory_fd,
                _UPGRADE_RETIREMENT_COMPLETE_SENTINEL,
                receipt):
            return None
        # A recovery-state marker and a completion marker describe mutually
        # exclusive namespace outcomes; never guess if both are present.
        if not _named_entry_missing(
                directory_fd, _UPGRADE_RECOVERY_STATE_SENTINEL):
            return None
        return True
    except (OSError, TypeError, ValueError):
        return None


def _receipt_disposal_snapshot(directory_fd, receipt):
    expected = receipt.get("tree")
    if not isinstance(expected, dict):
        return None
    snapshot = {
        "root_identity": list(expected.get("root_identity", ())),
        "directories": dict(expected.get("directories", {})),
        "files": {
            name: dict(value)
            for name, value in expected.get("files", {}).items()
        },
    }
    try:
        snapshot["files"][_RECEIPT_SIDECAR] = _snapshot_file(
            directory_fd, _RECEIPT_SIDECAR)
        for marker in _OPTIONAL_RECEIPT_MARKERS:
            if _named_entry_missing(directory_fd, marker):
                continue
            if not _receipt_owned_marker(directory_fd, marker, receipt):
                return None
            snapshot["files"][marker] = _snapshot_file(directory_fd, marker)
    except (OSError, TypeError, ValueError):
        return None
    return snapshot


def library_backup_disposal_record(backup, *, expected_owner=None):
    """Seal the exact tree and deterministic quarantine before journal use."""
    if not _backup_owner_authorized(backup, expected_owner):
        return None
    opened = _validated_backup_result(backup, require_complete=True)
    if opened is None:
        return None
    _public, _root, _parts, descriptors = opened
    try:
        snapshot = _receipt_disposal_snapshot(
            descriptors[-1], backup.receipt)
        if snapshot is None or _exact_tree_snapshot(descriptors[-1]) != snapshot:
            return None
        carrier = canonical_library_backup_record(
            {
                "version": 1,
                "kind": backup.receipt.get("kind"),
                "owner": backup.receipt.get("owner"),
                "path": os.fspath(backup.path),
                "origin": backup.receipt.get("origin"),
                "complete": backup.complete,
                "requested": backup.requested,
                "backed_up": backup.backed_up,
                "receipt": backup.receipt,
            },
            expected_owner=expected_owner,
        )
        if carrier is None:
            return None
        value = {
            "version": 1,
            "quarantine_name": _library_backup_disposal_quarantine_name(
                backup.receipt, expected_owner),
            "snapshot": snapshot,
        }
        return canonical_library_backup_disposal_record(
            value,
            carrier,
            expected_owner=expected_owner,
        )
    finally:
        _close_descriptors(descriptors)


def _canonical_receipt(receipt):
    """Detach a caller-owned receipt before it becomes deletion authority."""
    try:
        value = json.loads(json.dumps(receipt, sort_keys=True))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _album_proof_is_intact(proof) -> bool:
    try:
        root_fd = proof["descriptors"][-1]
        snapshot = proof["receipt"]["tree"]
        return (
            _backup_source_is_public(
                proof["music_root"],
                proof["parts"],
                proof["descriptors"],
            )
            and _exact_tree_snapshot(root_fd) == snapshot
            and _serializable_tree_fidelity(
                _tree_fidelity_snapshot(root_fd)
            ) == proof["receipt"]["fidelity"]
            and _tree_directory_generations(
                root_fd, snapshot
            ) == proof["receipt"]["directory_generations"]
            and _path_directory_generations(
                proof["descriptors"]
            ) == proof["receipt"]["path_generations"]
            and _held_snapshot_files_intact(
                root_fd, snapshot, proof["held"])
        )
    except (OSError, TypeError, ValueError):
        return False


def _hold_album_proof(path, expected_receipt):
    """Bind a public album receipt and exclude writers from every file."""
    expected = _canonical_receipt(expected_receipt)
    if expected is None:
        return None
    opened = _open_backup_source(Path(path))
    if opened is None:
        return None
    public, music_root, parts, descriptors = opened
    held = None
    try:
        root_fd = descriptors[-1]
        snapshot = _exact_tree_snapshot(root_fd)
        fidelity = _tree_fidelity_snapshot(root_fd)
        if snapshot is None or fidelity is None:
            raise OSError("replacement tree could not be sealed")
        current = {
            "version": 1,
            "origin": os.fspath(public),
            "tree": snapshot,
            "fidelity": _serializable_tree_fidelity(fidelity),
            "directory_generations": _tree_directory_generations(
                root_fd, snapshot),
            "path_generations": _path_directory_generations(descriptors),
        }
        if current != expected:
            raise OSError("replacement receipt no longer matches")
        held = {}
        _hold_snapshot_files(root_fd, snapshot, held)
        proof = {
            "public": public,
            "music_root": music_root,
            "parts": parts,
            "descriptors": descriptors,
            "held": held,
            "receipt": expected,
        }
        if (
            not _album_proof_is_intact(proof)
            or not _fsync_exact_tree(root_fd, snapshot, held)
            or not _album_proof_is_intact(proof)
        ):
            raise OSError("replacement could not be held durably")
        return proof
    except (OSError, TypeError, ValueError):
        if held is not None:
            _release_held_snapshot_files(held)
        _close_descriptors(descriptors)
        return None
    except BaseException:
        if held is not None:
            _release_held_snapshot_files(held)
        _close_descriptors(descriptors)
        raise


def _release_album_proof(proof) -> None:
    if proof is None:
        return
    _release_held_snapshot_files(proof["held"])
    _close_descriptors(proof["descriptors"])


def _directory_entry_names(directory_fd):
    try:
        with os.scandir(directory_fd) as iterator:
            return tuple(sorted(entry.name for entry in iterator))
    except OSError:
        return None


def _tree_snapshot_is_exact_remainder(current, expected) -> bool:
    """Accept only entries retained from one persisted exact tree."""
    if (
        not _tree_snapshot_schema_valid(current)
        or not _tree_snapshot_schema_valid(expected)
        or current["root_identity"] != expected["root_identity"]
        or not set(current["directories"]) <= set(expected["directories"])
        or not set(current["files"]) <= set(expected["files"])
        or any(
            identity != expected["directories"].get(name)
            for name, identity in current["directories"].items()
        )
    ):
        return False
    for name, snapshot in current["files"].items():
        original = expected["files"].get(name)
        if snapshot == original:
            continue
        if (
            original is None
            or _fidelity_without(snapshot, "changed_ns")
                != _fidelity_without(original, "changed_ns")
        ):
            return False
    return True


def _authorised_disposal_remainder_snapshot(
        directory_fd, current, persisted, receipt):
    """Extend authority only for later receipt-owned optional markers."""
    if (
        not _tree_snapshot_schema_valid(current)
        or not _tree_snapshot_schema_valid(persisted)
    ):
        return None
    expected = json.loads(json.dumps(persisted, sort_keys=True))
    extra = set(current["files"]) - set(expected["files"])
    if not extra <= set(_OPTIONAL_RECEIPT_MARKERS):
        return None
    for name in extra:
        if not _receipt_owned_marker(directory_fd, name, receipt):
            return None
        expected["files"][name] = dict(current["files"][name])
    return expected


def _remove_ownerless_disposal_wrapper(
        root_fd, quarantine_name, quarantine_fd, manifest_fd) -> None:
    """Remove the outer manifest last, then its now-empty wrapper."""
    manifest_exception = _unlink_exact_at(
        quarantine_fd, _DISPOSAL_MANIFEST, manifest_fd)
    wrapper_exception = _rmdir_exact_at(
        root_fd, quarantine_name, quarantine_fd)
    deferred = manifest_exception or wrapper_exception
    if deferred is not None:
        raise deferred


def _public_ownerless_carrier_matches_quarantine(
        root_fd, quarantine_name) -> bool:
    """Bind an empty pre-manifest reservation to its still-public carrier."""
    try:
        with os.scandir(root_fd) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError:
        return False
    for name in names:
        if name.startswith("."):
            continue
        carrier_fd = None
        try:
            carrier_fd = _open_backup_directory(name, dir_fd=root_fd)
            if not _named_directory_matches(root_fd, name, carrier_fd):
                continue
            receipt = _read_backup_receipt(carrier_fd)
            if (
                receipt is not None
                and receipt.get("version") == _RECEIPT_VERSION
                and "owner" not in receipt
                and _library_backup_disposal_quarantine_name(receipt, None)
                    == quarantine_name
            ):
                return True
        except OSError:
            continue
        finally:
            if carrier_fd is not None:
                os.close(carrier_fd)
    return False


def _reconcile_ownerless_disposal_residue(root_fd, quarantine_name):
    """Adopt one direct-disposal wrapper without following any path entry."""
    quarantine_fd = None
    manifest_fd = None
    public_fd = None
    held_fd = None
    held_files = None
    manifest = None
    try:
        backup_root = Path(os.path.abspath(os.fspath(
            cfg.UPGRADE_BACKUP_DIR)))
        if not _backup_root_is_public(backup_root, root_fd):
            return "attention", None
        quarantine_fd = _open_backup_directory(
            quarantine_name, dir_fd=root_fd)
        if not _named_directory_matches(
                root_fd, quarantine_name, quarantine_fd):
            return "attention", None
        manifest, manifest_fd = _read_ownerless_disposal_manifest_at(
            quarantine_fd,
            expected_quarantine_name=quarantine_name,
        )
        if manifest is None:
            if (
                _directory_entry_names(quarantine_fd) == ()
                and _public_ownerless_carrier_matches_quarantine(
                    root_fd, quarantine_name)
                and _named_directory_matches(
                    root_fd, quarantine_name, quarantine_fd)
                and _directory_entry_names(quarantine_fd) == ()
                and _backup_root_is_public(backup_root, root_fd)
            ):
                deferred = _rmdir_exact_at(
                    root_fd, quarantine_name, quarantine_fd)
                if deferred is not None:
                    raise deferred
                return "none", None
            return "unmanaged", None
        carrier_name = manifest["carrier_name"]
        manifest_only = (_DISPOSAL_MANIFEST,)
        manifest_and_held = tuple(sorted(
            (_DISPOSAL_MANIFEST, "held")))
        names = _directory_entry_names(quarantine_fd)
        if names not in (manifest_only, manifest_and_held):
            return "attention", manifest

        public_missing = _named_entry_missing(root_fd, carrier_name)
        if names == manifest_only:
            if not public_missing:
                public_fd = _open_backup_directory(
                    carrier_name, dir_fd=root_fd)
                if (
                    not _named_directory_matches(
                        root_fd, carrier_name, public_fd)
                    or not _tree_matches_ignoring_ctime(
                        _exact_tree_snapshot(public_fd),
                        manifest["snapshot"],
                    )
                ):
                    return "attention", manifest
            if (
                not _backup_root_is_public(backup_root, root_fd)
                or _directory_entry_names(quarantine_fd) != manifest_only
                or not _named_entry_matches(
                    quarantine_fd, _DISPOSAL_MANIFEST, manifest_fd)
            ):
                return "attention", manifest
            _remove_ownerless_disposal_wrapper(
                root_fd, quarantine_name, quarantine_fd, manifest_fd)
            os.close(manifest_fd)
            manifest_fd = None
            return (
                "disposed" if public_missing else "restored",
                manifest,
            )

        if not public_missing:
            return "attention", manifest
        held_fd = _open_backup_directory("held", dir_fd=quarantine_fd)
        current = _exact_tree_snapshot(held_fd)
        expected = manifest["snapshot"]
        if (
            not _named_directory_matches(
                quarantine_fd, "held", held_fd)
            or current is None
            or not _tree_snapshot_is_exact_remainder(current, expected)
            or not _tree_matches_ignoring_ctime(current, expected)
        ):
            return "attention", manifest
        held_files = {}
        _hold_snapshot_files(held_fd, current, held_files)
        if (
            not _held_snapshot_files_intact(
                held_fd, current, held_files)
            or not _fsync_exact_tree(held_fd, current, held_files)
            or _exact_tree_snapshot(held_fd) != current
            or _directory_entry_names(quarantine_fd)
                != manifest_and_held
            or not _named_entry_matches(
                quarantine_fd, _DISPOSAL_MANIFEST, manifest_fd)
            or not _named_entry_missing(root_fd, carrier_name)
            or not _backup_root_is_public(backup_root, root_fd)
        ):
            return "attention", manifest
        if not _restore_quarantined_entry(
                quarantine_fd, root_fd, carrier_name, held_fd):
            return "attention", manifest
        if (
            not _backup_root_is_public(backup_root, root_fd)
            or not _named_directory_matches(root_fd, carrier_name, held_fd)
            or not _tree_matches_ignoring_ctime(
                _exact_tree_snapshot(held_fd), expected)
            or _directory_entry_names(quarantine_fd) != manifest_only
        ):
            return "attention", manifest
        _remove_ownerless_disposal_wrapper(
            root_fd, quarantine_name, quarantine_fd, manifest_fd)
        os.close(manifest_fd)
        manifest_fd = None
        return "restored", manifest
    except (OSError, TypeError, ValueError, shutil.Error):
        return "attention", manifest
    finally:
        if held_files is not None:
            _release_held_snapshot_files(held_files)
        if held_fd is not None:
            os.close(held_fd)
        if public_fd is not None:
            os.close(public_fd)
        if manifest_fd is not None:
            os.close(manifest_fd)
        if quarantine_fd is not None:
            os.close(quarantine_fd)


def _reconcile_ownerless_disposal_residues():
    """Restore exact full direct-disposal residues before retention runs."""
    restored = set()
    root_fd = None
    try:
        backup_root = Path(os.path.abspath(os.fspath(
            cfg.UPGRADE_BACKUP_DIR)))
        root_fd = _open_backup_directory(backup_root)
        if not _backup_root_is_public(backup_root, root_fd):
            return restored
        with os.scandir(root_fd) as iterator:
            names = sorted(entry.name for entry in iterator)
        for name in names:
            if re.fullmatch(r"\.ql-dispose-backup-[0-9a-f]{64}", name) is None:
                continue
            state, manifest = _reconcile_ownerless_disposal_residue(
                root_fd, name)
            if state == "restored" and manifest is not None:
                restored.add(manifest["carrier_name"])
            elif state == "attention" and manifest is not None:
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  Keeping interrupted backup disposal {name!r}: "
                    f"its residue for {manifest['carrier_path']} is partial "
                    "or changed and needs recovery attention.",
                ))
        return restored
    except (OSError, TypeError, ValueError):
        return restored
    finally:
        if root_fd is not None:
            os.close(root_fd)


def reconcile_library_backup_disposal(
        carrier_record,
        disposal_record,
        *,
        replacement_path,
        expected_replacement_receipt,
        expected_owner=None,
):
    """Adopt and finish, restore, or refuse one post-crash quarantine."""
    carrier = canonical_library_backup_record(
        carrier_record,
        expected_owner=expected_owner,
    )
    disposal = canonical_library_backup_disposal_record(
        disposal_record,
        carrier_record,
        expected_owner=expected_owner,
    )
    if carrier is None or disposal is None:
        return BackupDisposalReconciliation("attention")
    try:
        backup_root = Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))
        backup_path = Path(carrier["path"])
        replacement = Path(os.path.abspath(os.fspath(replacement_path)))
    except (OSError, TypeError, ValueError):
        return BackupDisposalReconciliation("attention")
    quarantine_name = disposal["quarantine_name"]
    quarantine_path = backup_root / quarantine_name
    if backup_path.parent != backup_root:
        return BackupDisposalReconciliation("attention", quarantine_path)

    root_fd = None
    quarantine_fd = None
    public_backup_fd = None
    held_fd = None
    held_files = None
    replacement_proof = None
    try:
        root_fd = _open_backup_directory(backup_root)
        if not _backup_root_is_public(backup_root, root_fd):
            return BackupDisposalReconciliation(
                "attention", quarantine_path)
        if _named_entry_missing(root_fd, quarantine_name):
            return BackupDisposalReconciliation("none", quarantine_path)
        quarantine_fd = _open_backup_directory(
            quarantine_name, dir_fd=root_fd)
        if not _named_directory_matches(
                root_fd, quarantine_name, quarantine_fd):
            return BackupDisposalReconciliation(
                "attention", quarantine_path)

        names = _directory_entry_names(quarantine_fd)
        if names not in ((), ("held",)):
            return BackupDisposalReconciliation(
                "attention", quarantine_path)
        if not _named_entry_missing(root_fd, backup_path.name):
            if names:
                return BackupDisposalReconciliation(
                    "attention", quarantine_path)
            public_backup_fd = _open_backup_directory(
                backup_path.name, dir_fd=root_fd)
            public_snapshot = _exact_tree_snapshot(public_backup_fd)
            public_expected = _authorised_disposal_remainder_snapshot(
                public_backup_fd,
                public_snapshot,
                disposal["snapshot"],
                carrier["receipt"],
            )
            if (
                not _named_directory_matches(
                    root_fd, backup_path.name, public_backup_fd)
                or not _tree_matches_ignoring_ctime(
                    public_snapshot, public_expected)
            ):
                return BackupDisposalReconciliation(
                    "attention", quarantine_path)
            deferred = _rmdir_exact_at(
                root_fd, quarantine_name, quarantine_fd)
            if deferred is not None:
                raise deferred
            return BackupDisposalReconciliation("none", quarantine_path)
        replacement_proof = _hold_album_proof(
            replacement, expected_replacement_receipt)

        if not names:
            if (
                replacement_proof is None
                or not _album_proof_is_intact(replacement_proof)
                or not _named_directory_matches(
                    root_fd, quarantine_name, quarantine_fd)
                or not _named_entry_missing(root_fd, backup_path.name)
            ):
                return BackupDisposalReconciliation(
                    "attention", quarantine_path)
            deferred = _rmdir_exact_at(
                root_fd, quarantine_name, quarantine_fd)
            if deferred is not None:
                raise deferred
            return BackupDisposalReconciliation(
                "disposed", quarantine_path)

        held_fd = _open_backup_directory("held", dir_fd=quarantine_fd)
        current = _exact_tree_snapshot(held_fd)
        expected = _authorised_disposal_remainder_snapshot(
            held_fd,
            current,
            disposal["snapshot"],
            carrier["receipt"],
        )
        if (
            not _named_directory_matches(quarantine_fd, "held", held_fd)
            or current is None
            or expected is None
            or not _tree_snapshot_is_exact_remainder(current, expected)
        ):
            return BackupDisposalReconciliation(
                "attention", quarantine_path)

        held_files = {}
        _hold_snapshot_files(held_fd, current, held_files)
        if (
            not _named_directory_matches(quarantine_fd, "held", held_fd)
            or _exact_tree_snapshot(held_fd) != current
            or _directory_entry_names(quarantine_fd) != ("held",)
            or not _named_entry_missing(root_fd, backup_path.name)
        ):
            return BackupDisposalReconciliation(
                "attention", quarantine_path)

        if replacement_proof is None:
            if not _tree_matches_ignoring_ctime(current, expected):
                return BackupDisposalReconciliation(
                    "attention", quarantine_path)
            if not _restore_quarantined_entry(
                    quarantine_fd, root_fd, backup_path.name, held_fd):
                return BackupDisposalReconciliation(
                    "attention", quarantine_path)
            deferred = _rmdir_exact_at(
                root_fd, quarantine_name, quarantine_fd)
            if deferred is not None:
                raise deferred
            return BackupDisposalReconciliation(
                "restored", quarantine_path)

        def commit_guard():
            return (
                _album_proof_is_intact(replacement_proof)
                and _backup_root_is_public(backup_root, root_fd)
                and _named_entry_missing(root_fd, backup_path.name)
                and _named_directory_matches(
                    root_fd, quarantine_name, quarantine_fd)
                and _named_directory_matches(
                    quarantine_fd, "held", held_fd)
                and _directory_entry_names(quarantine_fd) == ("held",)
            )

        if not commit_guard():
            return BackupDisposalReconciliation(
                "attention", quarantine_path)
        deferred = _delete_exact_tree_contents(
            held_fd,
            current,
            held=held_files,
            commit_guard=commit_guard,
        )
        root_exception = _rmdir_exact_at(quarantine_fd, "held", held_fd)
        quarantine_exception = _rmdir_exact_at(
            root_fd, quarantine_name, quarantine_fd)
        deferred = deferred or root_exception or quarantine_exception
        if deferred is not None:
            raise deferred
        return BackupDisposalReconciliation("disposed", quarantine_path)
    except (OSError, TypeError, ValueError, shutil.Error):
        return BackupDisposalReconciliation("attention", quarantine_path)
    finally:
        if held_files is not None:
            _release_held_snapshot_files(held_files)
        _release_album_proof(replacement_proof)
        if held_fd is not None:
            os.close(held_fd)
        if public_backup_fd is not None:
            os.close(public_backup_fd)
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        if root_fd is not None:
            os.close(root_fd)


def _descriptor_alias_binding(path: Path, directory_fd):
    """Seal one validator-facing symlink to its exact held directory."""
    descriptor = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        path_only = getattr(os, "O_PATH", None)
        if nofollow is None or path_only is None:
            return None
        descriptor = os.open(
            path,
            path_only | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        linked = os.fstat(descriptor)
        target = os.readlink(path)
        expected_target = os.fspath(_held_directory_path(directory_fd))
        if (
            not stat.S_ISLNK(linked.st_mode)
            or not _same_entry(
                linked, os.stat(path, follow_symlinks=False))
            or target != expected_target
            or not _same_directory(os.stat(path), os.fstat(directory_fd))
        ):
            raise OSError("descriptor alias is not exact")
        binding = descriptor, target
        descriptor = None
        return binding
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _descriptor_alias_is_intact(path: Path, directory_fd, binding) -> bool:
    if binding is None:
        return False
    try:
        alias_fd, target = binding
        linked = os.stat(path, follow_symlinks=False)
        return (
            stat.S_ISLNK(linked.st_mode)
            and _same_entry(linked, os.fstat(alias_fd))
            and os.readlink(path) == target
            and _same_directory(os.stat(path), os.fstat(directory_fd))
        )
    except (OSError, TypeError, ValueError):
        return False


def _release_descriptor_alias(binding) -> None:
    if binding is None:
        return
    try:
        os.close(binding[0])
    except OSError:
        pass


def dispose_backup(
        backup,
        *,
        replacement_path=None,
        expected_replacement_receipt=None,
        replacement_validator=None,
        expected_owner=None,
        expected_disposal_record=None,
) -> bool:
    """Dispose one exact carried backup after live semantic revalidation.

    The validator receives private, uniquely-named descriptor-root paths as
    ``validator(replacement_view, backup_view)``.  Existing path-based media
    checks can therefore inspect the exact open directories without reopening
    either mutable public path, and every file stays writer-excluded.  Unique
    names also prevent path-keyed media caches from reusing an earlier proof.
    Both namespaces are rechecked after validation and through every unlink.
    Missing proof, a raw backup path, or any uncertainty keeps the backup.
    """
    if (
        not _backup_owner_authorized(backup, expected_owner)
        or replacement_path is None
        or expected_replacement_receipt is None
        or not callable(replacement_validator)
    ):
        return False
    opened = _validated_backup_result(backup, require_complete=True)
    if opened is None:
        return False
    _public, backup_root, parts, descriptors = opened
    directory_fd = descriptors[-1]
    replacement_proof = None
    held_backup = None
    proof_workspace = None
    replacement_alias = None
    backup_alias = None
    try:
        replacement_proof = _hold_album_proof(
            replacement_path, expected_replacement_receipt)
        if replacement_proof is None:
            return False
        if _same_directory(
                os.fstat(directory_fd),
                os.fstat(replacement_proof["descriptors"][-1])):
            return False

        receipt = backup.receipt
        snapshot = _receipt_disposal_snapshot(directory_fd, receipt)
        if snapshot is None or _exact_tree_snapshot(directory_fd) != snapshot:
            return False
        quarantine_name = _library_backup_disposal_quarantine_name(
            receipt, expected_owner)
        if quarantine_name is None:
            return False
        disposal_manifest = None
        if (
            expected_disposal_record is None
            and receipt.get("version") == _RECEIPT_VERSION
            and "owner" not in receipt
        ):
            disposal_manifest = _canonical_ownerless_disposal_manifest({
                "version": _DISPOSAL_MANIFEST_VERSION,
                "carrier_name": parts[-1],
                "carrier_path": os.fspath(backup.path),
                "quarantine_name": quarantine_name,
                "receipt": receipt,
                "snapshot": snapshot,
            })
            if disposal_manifest is None:
                return False
        if expected_disposal_record is not None:
            carrier = canonical_library_backup_record(
                {
                    "version": 1,
                    "kind": receipt.get("kind"),
                    "owner": receipt.get("owner"),
                    "path": os.fspath(backup.path),
                    "origin": receipt.get("origin"),
                    "complete": backup.complete,
                    "requested": backup.requested,
                    "backed_up": backup.backed_up,
                    "receipt": receipt,
                },
                expected_owner=expected_owner,
            )
            disposal = canonical_library_backup_disposal_record(
                expected_disposal_record,
                carrier,
                expected_owner=expected_owner,
            )
            authorised_snapshot = (
                None
                if disposal is None
                else _authorised_disposal_remainder_snapshot(
                    directory_fd,
                    snapshot,
                    disposal["snapshot"],
                    receipt,
                )
            )
            if not _tree_matches_ignoring_ctime(
                    snapshot, authorised_snapshot):
                return False
            if disposal["quarantine_name"] != quarantine_name:
                return False
        held_backup = {}
        _hold_snapshot_files(directory_fd, snapshot, held_backup)
        if (
            not _held_snapshot_files_intact(
                directory_fd, snapshot, held_backup)
            or not _album_proof_is_intact(replacement_proof)
        ):
            return False

        proof_workspace = tempfile.TemporaryDirectory(
            prefix="qobuz-librarian-disposal-proof-")
        view_root = Path(proof_workspace.name)
        replacement_view = view_root / "replacement"
        backup_view = view_root / "backup"
        os.symlink(
            _held_directory_path(replacement_proof["descriptors"][-1]),
            replacement_view,
            target_is_directory=True,
        )
        os.symlink(
            _held_directory_path(directory_fd),
            backup_view,
            target_is_directory=True,
        )
        replacement_alias = _descriptor_alias_binding(
            replacement_view, replacement_proof["descriptors"][-1])
        backup_alias = _descriptor_alias_binding(
            backup_view, directory_fd)
        if (
            replacement_alias is None
            or backup_alias is None
        ):
            return False
        try:
            semantically_redundant = bool(replacement_validator(
                replacement_view, backup_view))
        except (OSError, TypeError, ValueError, shutil.Error):
            return False
        if (
            not semantically_redundant
            or not _descriptor_alias_is_intact(
                replacement_view,
                replacement_proof["descriptors"][-1],
                replacement_alias,
            )
            or not _descriptor_alias_is_intact(
                backup_view, directory_fd, backup_alias)
            or not _album_proof_is_intact(replacement_proof)
            or not _held_snapshot_files_intact(
                directory_fd, snapshot, held_backup)
        ):
            return False

        # The descriptor-root aliases are no longer needed once semantic
        # validation and both exact rechecks have passed.  Remove this private
        # workspace before the irreversible backup disposal so even a cleanup
        # interruption leaves the complete backup untouched.
        proof_workspace.cleanup()
        proof_workspace = None

        return _remove_exact_tree_at(
            descriptors[-2],
            parts[-1],
            directory_fd,
            prefix="ql-dispose-backup",
            expected_snapshot=snapshot,
            held_files=held_backup,
            quarantine_name=quarantine_name,
            disposal_manifest=disposal_manifest,
            commit_guard=lambda: (
                _album_proof_is_intact(replacement_proof)
                and _backup_source_is_public(
                    backup_root, parts, descriptors, include_album=False)
            ),
        )
    except (OSError, TypeError, ValueError, shutil.Error):
        return False
    finally:
        _release_descriptor_alias(replacement_alias)
        _release_descriptor_alias(backup_alias)
        if proof_workspace is not None:
            proof_workspace.cleanup()
        if held_backup is not None:
            _release_held_snapshot_files(held_backup)
        _release_album_proof(replacement_proof)
        _close_descriptors(descriptors)


def _companion_marker_value(directory_fd, name, receipt):
    """Return ``(present, value)`` for one receipt-bound JSON marker."""
    descriptor = None
    try:
        if _named_entry_missing(directory_fd, name):
            return False, None
        descriptor = _open_regular_file_at(directory_fd, name)
        os.fsync(descriptor)
        if (
            not _named_entry_matches(directory_fd, name, descriptor)
            or not _fsync_directory_fds(directory_fd)
        ):
            return True, None
        marker_stat = os.fstat(descriptor)
        if marker_stat.st_size < 0 or marker_stat.st_size > 1024 * 1024:
            return True, None
        raw = os.pread(descriptor, marker_stat.st_size + 1, 0)
        if len(raw) != marker_stat.st_size:
            return True, None
        marker = decode_recovery_json(raw)
        if (
            not isinstance(marker, dict)
            or set(marker) != {"token", "note"}
            or marker.get("token") != receipt.get("token")
            or not isinstance(marker.get("note"), str)
        ):
            return True, None
        value = decode_recovery_json(marker["note"])
        if (
            not isinstance(value, dict)
            or _canonical_receipt(value) != value
        ):
            return True, None
        return True, value
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        return True, None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_companion_marker(backup, name, value) -> bool:
    try:
        note = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    if not _write_receipt_marker(backup, name, note):
        return False
    opened = _validated_backup_result(backup, require_complete=True)
    if opened is None:
        return False
    try:
        present, persisted = _companion_marker_value(
            opened[-1][-1], name, backup.receipt)
        return present and persisted == value
    finally:
        _close_descriptors(opened[-1])


def _companion_relative_parts(value):
    if not isinstance(value, str):
        return None
    path = PurePosixPath(value)
    parts = tuple(path.parts)
    if (
        not parts
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in parts)
        or "/".join(parts) != value
    ):
        return None
    return parts


def _companion_marker_digest(value) -> str:
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _serializable_directory_fidelity_fd(descriptor):
    value = _fidelity_fd(descriptor, include_times=False)
    value["xattrs"] = {
        name: payload.hex() for name, payload in value["xattrs"].items()
    }
    value["mtime_ns"] = int(os.fstat(descriptor).st_mtime_ns)
    return value


def _companion_directory_fidelity_matches(descriptor, expected, *,
                                           include_mtime=True) -> bool:
    try:
        current = _serializable_directory_fidelity_fd(descriptor)
        if not include_mtime:
            current = _fidelity_without(current, "mtime_ns")
            expected = _fidelity_without(expected, "mtime_ns")
        return current == expected
    except (OSError, TypeError, ValueError):
        return False


def _companion_file_identity_matches(descriptor, prepared) -> bool:
    """Match a prepared file after rename without relying on mutable ctime."""
    try:
        current = _snapshot_file_from_descriptor(descriptor)
        expected = prepared["snapshot"]
        return (
            _fidelity_without(current, "changed_ns")
                == _fidelity_without(expected, "changed_ns")
            and _serializable_fidelity_fd(descriptor)
                == prepared["fidelity"]
            and int(os.fstat(descriptor).st_nlink) == 1
        )
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _snapshot_file_from_descriptor(descriptor):
    receipt = _regular_file_receipt(
        descriptor, _file_digest_fd(descriptor))
    return {
        "identity": list(receipt["identity"]),
        "size": receipt["size"],
        "mtime_ns": receipt["mtime_ns"],
        "changed_ns": receipt["changed_ns"],
        "sha256": receipt["sha256"],
    }


def _companion_intent_is_valid(intent, backup, replacement_path) -> bool:
    try:
        if (
            not isinstance(intent, dict)
            or set(intent) != {
                "version", "transaction", "backup_token", "origin",
                "pre_receipt", "workspace_name", "directories", "files",
            }
            or intent["version"] != 1
            or intent["backup_token"] != backup.receipt["token"]
            or not isinstance(intent["transaction"], str)
            or len(intent["transaction"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in intent["transaction"])
            or intent["origin"] != os.fspath(Path(
                os.path.abspath(os.fspath(replacement_path))))
            or not _source_receipt_shape_is_valid(
                intent["pre_receipt"], intent["origin"])
            or not isinstance(intent["workspace_name"], str)
            or not intent["workspace_name"].startswith(
                ".ql-companion-carry-")
            or "/" in intent["workspace_name"]
            or not isinstance(intent["directories"], list)
            or not isinstance(intent["files"], list)
            or not intent["files"]
        ):
            return False

        pre_tree = intent["pre_receipt"]["tree"]
        backup_files = backup.receipt["tree"]["files"]
        seen_targets = set(pre_tree["directories"]) | set(
            pre_tree["files"])
        seen_stages = set()
        prepared_directories = set()
        for index, item in enumerate(intent["directories"]):
            if (
                not isinstance(item, dict)
                or set(item) != {
                    "relative", "stage_name", "fidelity"}
                or _companion_relative_parts(item["relative"]) is None
                or item["relative"] in seen_targets
                or item["stage_name"] != f"d{index:04d}"
                or item["stage_name"] in seen_stages
                or not isinstance(item["fidelity"], dict)
            ):
                return False
            parts = _companion_relative_parts(item["relative"])
            parent = "/".join(parts[:-1])
            if parent and (
                parent not in pre_tree["directories"]
                and parent not in prepared_directories
            ):
                return False
            seen_targets.add(item["relative"])
            seen_stages.add(item["stage_name"])
            prepared_directories.add(item["relative"])

        for index, item in enumerate(intent["files"]):
            if (
                not isinstance(item, dict)
                or set(item) != {
                    "relative", "stage_name", "source_snapshot",
                    "source_fidelity"}
                or _companion_relative_parts(item["relative"]) is None
                or item["relative"] in seen_targets
                or item["stage_name"] != f"f{index:04d}"
                or item["stage_name"] in seen_stages
                or item["relative"] not in backup_files
                or Path(PurePosixPath(item["relative"]).name).suffix.lower()
                    in cfg.AUDIO_EXTS
                or not isinstance(item["source_snapshot"], dict)
                or not isinstance(item["source_fidelity"], dict)
            ):
                return False
            parts = _companion_relative_parts(item["relative"])
            parent = "/".join(parts[:-1])
            if parent and (
                parent not in pre_tree["directories"]
                and parent not in prepared_directories
            ):
                return False
            expected = backup_files[item["relative"]]
            if (
                item["source_snapshot"].get("sha256")
                    != expected.get("sha256")
                or item["source_snapshot"].get("size")
                    != expected.get("size")
            ):
                return False
            seen_targets.add(item["relative"])
            seen_stages.add(item["stage_name"])
        return True
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _companion_ready_is_valid(ready, intent) -> bool:
    try:
        if (
            not isinstance(ready, dict)
            or set(ready) != {
                "version", "transaction", "intent_sha256", "workspace",
                "directories", "files",
            }
            or ready["version"] != 1
            or ready["transaction"] != intent["transaction"]
            or ready["intent_sha256"] != _companion_marker_digest(intent)
            or not isinstance(ready["workspace"], dict)
            or set(ready["workspace"]) != {"identity", "generation"}
            or not isinstance(ready["directories"], list)
            or not isinstance(ready["files"], list)
            or len(ready["directories"]) != len(intent["directories"])
            or len(ready["files"]) != len(intent["files"])
        ):
            return False
        for planned, prepared in zip(
                intent["directories"], ready["directories"]):
            if (
                not isinstance(prepared, dict)
                or set(prepared) != {
                    "relative", "stage_name", "identity", "generation"}
                or prepared["relative"] != planned["relative"]
                or prepared["stage_name"] != planned["stage_name"]
                or not isinstance(prepared["identity"], list)
                or not isinstance(prepared["generation"], list)
            ):
                return False
        for planned, prepared in zip(intent["files"], ready["files"]):
            if (
                not isinstance(prepared, dict)
                or set(prepared) != {
                    "relative", "stage_name", "snapshot", "fidelity"}
                or prepared["relative"] != planned["relative"]
                or prepared["stage_name"] != planned["stage_name"]
                or prepared["fidelity"] != planned["source_fidelity"]
                or _fidelity_without(
                    prepared["snapshot"], "identity", "changed_ns")
                    != _fidelity_without(
                        planned["source_snapshot"],
                        "identity", "changed_ns")
            ):
                return False
        return True
    except (TypeError, ValueError, KeyError):
        return False


def _companion_committed_is_valid(committed, intent, ready) -> bool:
    try:
        return (
            isinstance(committed, dict)
            and set(committed) == {
                "version", "transaction", "ready_sha256", "post_receipt"}
            and committed["version"] == 1
            and committed["transaction"] == intent["transaction"]
            and committed["ready_sha256"]
                == _companion_marker_digest(ready)
            and _source_receipt_shape_is_valid(
                committed["post_receipt"], intent["origin"])
        )
    except (TypeError, ValueError, KeyError):
        return False


def _companion_tree_matches(opened, intent, ready, public_relatives,
                            *, include_directory_mtime) -> bool:
    """Validate the old album plus only exact READY-bound publications."""
    try:
        public, music_root, parts, descriptors = opened
        album_fd = descriptors[-1]
        pre = intent["pre_receipt"]
        pre_tree = pre["tree"]
        snapshot = _exact_tree_snapshot(album_fd)
        fidelity = _serializable_tree_fidelity(
            _tree_fidelity_snapshot(album_fd))
        if snapshot is None or fidelity is None:
            return False
        directory_ready = {
            item["relative"]: item for item in ready["directories"]}
        file_ready = {item["relative"]: item for item in ready["files"]}
        published_directories = set(directory_ready) & public_relatives
        published_files = set(file_ready) & public_relatives
        if public_relatives != published_directories | published_files:
            return False
        if (
            snapshot["root_identity"] != pre_tree["root_identity"]
            or set(snapshot["directories"])
                != set(pre_tree["directories"]) | published_directories
            or set(snapshot["files"])
                != set(pre_tree["files"]) | published_files
            or any(
                snapshot["directories"].get(relative) != expected
                for relative, expected in pre_tree["directories"].items())
            or any(
                snapshot["files"].get(relative) != expected
                for relative, expected in pre_tree["files"].items())
        ):
            return False
        for relative in published_directories:
            if snapshot["directories"][relative] != directory_ready[
                    relative]["identity"]:
                return False
        for relative in published_files:
            if _fidelity_without(
                    snapshot["files"][relative], "changed_ns"
            ) != _fidelity_without(
                    file_ready[relative]["snapshot"], "changed_ns"):
                return False

        expected_directory_keys = (
            set(pre["fidelity"]["directories"])
            | published_directories)
        if (
            set(fidelity["directories"]) != expected_directory_keys
            or set(fidelity["files"])
                != set(pre["fidelity"]["files"]) | published_files
            or any(
                fidelity["files"].get(relative) != expected
                for relative, expected in pre["fidelity"]["files"].items())
        ):
            return False
        planned_directories = {
            item["relative"]: item for item in intent["directories"]}
        for relative in expected_directory_keys:
            expected = (
                pre["fidelity"]["directories"].get(relative)
                or planned_directories[relative]["fidelity"])
            current = fidelity["directories"][relative]
            if not include_directory_mtime:
                expected = _fidelity_without(expected, "mtime_ns")
                current = _fidelity_without(current, "mtime_ns")
            if current != expected:
                return False
        for relative in published_files:
            if fidelity["files"][relative] != file_ready[
                    relative]["fidelity"]:
                return False

        expected_generations = dict(pre["directory_generations"])
        expected_generations.update({
            relative: directory_ready[relative]["generation"]
            for relative in published_directories
        })
        return (
            _backup_source_is_public(music_root, parts, descriptors)
            and _tree_directory_generations(album_fd, snapshot)
                == expected_generations
            and _path_directory_generations(descriptors)
                == pre["path_generations"]
            and os.fspath(public) == intent["origin"]
        )
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _restore_companion_parent_mtime(parent_fd, relative_text, intent) -> bool:
    try:
        expected = intent["pre_receipt"]["fidelity"][
            "directories"].get(relative_text)
        if expected is None:
            expected = next(
                item["fidelity"] for item in intent["directories"]
                if item["relative"] == relative_text)
        if not _companion_directory_fidelity_matches(
                parent_fd, expected, include_mtime=False):
            return False
        current = os.fstat(parent_fd)
        os.utime(
            parent_fd,
            ns=(int(current.st_atime_ns), int(expected["mtime_ns"])),
        )
        return (
            _companion_directory_fidelity_matches(parent_fd, expected)
            and _fsync_directory_fds(parent_fd)
        )
    except (OSError, TypeError, ValueError, KeyError, StopIteration):
        return False


def _cleanup_companion_workspace(parent_fd, name, workspace_fd) -> bool:
    try:
        snapshot = _exact_tree_snapshot(workspace_fd)
        if snapshot is None:
            return False
        held = {}
        _hold_snapshot_files(workspace_fd, snapshot, held)
        try:
            return _remove_exact_tree_at(
                parent_fd,
                name,
                workspace_fd,
                prefix="ql-companion-workspace-cleanup",
                expected_snapshot=snapshot,
                held_files=held,
            )
        finally:
            _release_held_snapshot_files(held)
    except (OSError, TypeError, ValueError, shutil.Error):
        return False


def _prepare_companion_ready(backup, replacement_path, intent):
    """Populate the intent-bound private workspace and persist exact READY."""
    backup_opened = _validated_backup_result(
        backup, require_complete=True)
    if backup_opened is None:
        return None
    backup_fd = backup_opened[-1][-1]
    replacement_proof = None
    backup_snapshot = None
    held_backup = None
    workspace_fd = None
    publications = []
    ready_durable = False
    try:
        intent_present, persisted_intent = _companion_marker_value(
            backup_fd, _COMPANION_CARRY_INTENT_SENTINEL, backup.receipt)
        ready_present, _persisted_ready = _companion_marker_value(
            backup_fd, _COMPANION_CARRY_READY_SENTINEL, backup.receipt)
        if (
            not intent_present
            or persisted_intent != intent
            or ready_present
            or not _companion_intent_is_valid(
                intent, backup, replacement_path)
        ):
            return None
        replacement_proof = _hold_album_proof(
            replacement_path, intent["pre_receipt"])
        if replacement_proof is None:
            return None
        parent_fd = replacement_proof["descriptors"][-2]
        if not _named_entry_missing(parent_fd, intent["workspace_name"]):
            return None

        backup_snapshot = _receipt_disposal_snapshot(
            backup_fd, backup.receipt)
        if (
            backup_snapshot is None
            or _exact_tree_snapshot(backup_fd) != backup_snapshot
        ):
            return None
        held_backup = {}
        _hold_snapshot_files(backup_fd, backup_snapshot, held_backup)
        for item in intent["files"]:
            source = held_backup.get(item["relative"])
            if (
                source is None
                or not source["lease"].intact()
                or _snapshot_file_from_descriptor(source["descriptor"])
                    != item["source_snapshot"]
                or _serializable_fidelity_fd(source["descriptor"])
                    != item["source_fidelity"]
            ):
                return None
        if not _album_proof_is_intact(replacement_proof):
            return None

        os.mkdir(
            intent["workspace_name"], mode=0o700, dir_fd=parent_fd)
        workspace_fd = _open_backup_directory(
            intent["workspace_name"], dir_fd=parent_fd)
        if (
            not _named_directory_matches(
                parent_fd, intent["workspace_name"], workspace_fd)
            or not _fsync_directory_fds(parent_fd)
        ):
            raise OSError("companion workspace could not be committed")

        prepared_directories = []
        for planned in intent["directories"]:
            os.mkdir(planned["stage_name"], mode=0o700, dir_fd=workspace_fd)
            descriptor = _open_backup_directory(
                planned["stage_name"], dir_fd=workspace_fd)
            try:
                if (
                    not _named_directory_matches(
                        workspace_fd, planned["stage_name"], descriptor)
                    or not _apply_serialized_directory_fidelity(
                        descriptor, planned["fidelity"], restore_mtime=True)
                    or not _fsync_directory_fds(descriptor, workspace_fd)
                ):
                    raise OSError(
                        "prepared companion directory was not durable")
                prepared_directories.append({
                    "relative": planned["relative"],
                    "stage_name": planned["stage_name"],
                    "identity": list(_entry_identity(os.fstat(descriptor))),
                    "generation": _path_directory_generations(
                        [descriptor])[-1],
                })
            finally:
                os.close(descriptor)

        prepared_files = []
        for planned in intent["files"]:
            publication = _CopyPublication(
                workspace_fd, planned["stage_name"])
            publications.append(publication)
            digest = _copy_file_noreplace_at(
                held_backup[planned["relative"]]["descriptor"],
                publication,
            )
            if digest != planned["source_snapshot"]["sha256"]:
                raise OSError("prepared companion digest changed")
            prepared_files.append({
                "relative": planned["relative"],
                "stage_name": planned["stage_name"],
                "snapshot": _snapshot_file_from_descriptor(
                    publication.descriptor),
                "fidelity": _serializable_fidelity_fd(
                    publication.descriptor),
            })
        ready = {
            "version": 1,
            "transaction": intent["transaction"],
            "intent_sha256": _companion_marker_digest(intent),
            "workspace": {
                "identity": list(_entry_identity(os.fstat(workspace_fd))),
                "generation": _path_directory_generations(
                    [workspace_fd])[-1],
            },
            "directories": prepared_directories,
            "files": prepared_files,
        }
        if not _companion_ready_is_valid(ready, intent):
            raise OSError("prepared companion state was not exact")
        try:
            ready_durable = _write_companion_marker(
                backup, _COMPANION_CARRY_READY_SENTINEL, ready)
        except BaseException:
            present, persisted = _companion_marker_value(
                backup_fd,
                _COMPANION_CARRY_READY_SENTINEL,
                backup.receipt,
            )
            ready_durable = present and persisted == ready
            raise
        return ready if ready_durable else None
    except (OSError, TypeError, ValueError, shutil.Error):
        return None
    finally:
        for publication in reversed(publications):
            _release_copy_publication(publication)
        if workspace_fd is not None:
            if not ready_durable:
                _cleanup_companion_workspace(
                    replacement_proof["descriptors"][-2],
                    intent["workspace_name"],
                    workspace_fd,
                )
            os.close(workspace_fd)
        if held_backup is not None:
            _release_held_snapshot_files(held_backup)
        _release_album_proof(replacement_proof)
        _close_descriptors(backup_opened[-1])


def _open_companion_units(opened, parent_fd, workspace_fd, intent, ready):
    album_fd = opened[-1][-1]
    units = []
    try:
        planned_by_relative = {
            item["relative"]: item
            for item in (*intent["directories"], *intent["files"])
        }
        prepared_items = [
            ("directory", item) for item in ready["directories"]]
        prepared_items.extend(("file", item) for item in ready["files"])
        for kind, prepared in prepared_items:
            relative = _companion_relative_parts(prepared["relative"])
            target_parents = []
            target_fd = None
            stage_fd = None
            target_parent_fd = None
            lease = None
            handed_off = False
            try:
                try:
                    target_parents = _open_relative_directories(
                        album_fd, relative[:-1], create=False)
                    target_parent_fd = (
                        target_parents[-1] if target_parents else album_fd)
                    try:
                        target_fd = (
                            _open_backup_directory(
                                relative[-1], dir_fd=target_parent_fd)
                            if kind == "directory"
                            else _open_regular_file_at(
                                target_parent_fd, relative[-1])
                        )
                    except FileNotFoundError:
                        if not _named_entry_missing(
                                target_parent_fd, relative[-1]):
                            raise OSError(
                                "companion target is an unmarked entry")
                except FileNotFoundError:
                    _close_descriptors(target_parents)
                    target_parents = []
                    target_parent_fd = None
                if workspace_fd is not None:
                    try:
                        stage_fd = (
                            _open_backup_directory(
                                prepared["stage_name"], dir_fd=workspace_fd)
                            if kind == "directory"
                            else _open_regular_file_at(
                                workspace_fd, prepared["stage_name"])
                        )
                    except FileNotFoundError:
                        if not _named_entry_missing(
                                workspace_fd, prepared["stage_name"]):
                            raise OSError(
                                "companion workspace entry changed type")
                if target_fd is not None and stage_fd is not None:
                    raise OSError(
                        "prepared companion exists in two namespaces")
                if target_fd is None and stage_fd is None:
                    raise OSError("prepared companion was lost")

                descriptor = (
                    target_fd if target_fd is not None else stage_fd)
                location = (
                    "public" if target_fd is not None else "workspace")
                planned = planned_by_relative[prepared["relative"]]
                if kind == "directory":
                    if (
                        list(_entry_identity(os.fstat(descriptor)))
                            != prepared["identity"]
                        or _path_directory_generations([descriptor])[-1]
                            != prepared["generation"]
                        or not _companion_directory_fidelity_matches(
                            descriptor,
                            planned["fidelity"],
                            include_mtime=location == "workspace",
                        )
                        or location == "workspace"
                        and _exact_tree_snapshot(descriptor) != {
                            "root_identity": prepared["identity"],
                            "directories": {},
                            "files": {},
                        }
                    ):
                        raise OSError(
                            "prepared companion directory changed")
                else:
                    if (
                        location == "workspace"
                        and _snapshot_file_from_descriptor(descriptor)
                            != prepared["snapshot"]
                        or not _companion_file_identity_matches(
                            descriptor, prepared)
                    ):
                        raise OSError("prepared companion file changed")
                    lease = acquire_inode_write_exclusion(descriptor)
                    if lease is None or not lease.intact():
                        raise OSError(
                            "prepared companion file has a writer")
                units.append({
                    "kind": kind,
                    "prepared": prepared,
                    "relative": relative,
                    "location": location,
                    "descriptor": descriptor,
                    "lease": lease,
                    "target_parents": target_parents,
                    "target_parent_fd": target_parent_fd,
                })
                handed_off = True
            finally:
                if not handed_off:
                    if lease is not None:
                        lease.close()
                    for descriptor in (stage_fd, target_fd):
                        if descriptor is not None:
                            try:
                                os.close(descriptor)
                            except OSError:
                                pass
                    _close_descriptors(target_parents)
            if target_fd is not None:
                stage_fd = None
            else:
                target_fd = None
                _close_descriptors(target_parents)
                units[-1]["target_parents"] = []
                units[-1]["target_parent_fd"] = None
        return units
    except BaseException:
        _close_companion_units(units)
        raise


def _close_companion_units(units) -> None:
    for unit in reversed(units):
        if unit["lease"] is not None:
            unit["lease"].close()
        os.close(unit["descriptor"])
        _close_descriptors(unit["target_parents"])


def _resume_companion_carry(backup, replacement_path,
                            expected_replacement_receipt):
    backup_opened = _validated_backup_result(
        backup, require_complete=True)
    if backup_opened is None:
        return None
    backup_fd = backup_opened[-1][-1]
    album_opened = None
    workspace_fd = None
    units = []
    held_album = None
    held_backup = None
    try:
        intent_present, intent = _companion_marker_value(
            backup_fd, _COMPANION_CARRY_INTENT_SENTINEL, backup.receipt)
        ready_present, ready = _companion_marker_value(
            backup_fd, _COMPANION_CARRY_READY_SENTINEL, backup.receipt)
        committed_present, committed = _companion_marker_value(
            backup_fd, _COMPANION_CARRY_COMMITTED_SENTINEL, backup.receipt)
        if (
            not intent_present
            or not ready_present
            or intent is None
            or ready is None
            or not _companion_intent_is_valid(
                intent, backup, replacement_path)
            or not _companion_ready_is_valid(ready, intent)
            or committed_present and (
                committed is None
                or not _companion_committed_is_valid(
                    committed, intent, ready))
        ):
            return None
        supplied = _canonical_receipt(expected_replacement_receipt)
        if supplied != intent["pre_receipt"] and (
            committed is None
            or supplied != committed["post_receipt"]
        ):
            return None

        album_opened = _open_backup_source(Path(replacement_path))
        if album_opened is None:
            return None
        album_fd = album_opened[-1][-1]
        parent_fd = album_opened[-1][-2]
        try:
            workspace_fd = _open_backup_directory(
                intent["workspace_name"], dir_fd=parent_fd)
        except FileNotFoundError:
            workspace_fd = None
        if workspace_fd is not None and (
            not _named_directory_matches(
                parent_fd, intent["workspace_name"], workspace_fd)
            or list(_entry_identity(os.fstat(workspace_fd)))
                != ready["workspace"]["identity"]
            or _path_directory_generations([workspace_fd])[-1]
                != ready["workspace"]["generation"]
        ):
            return None

        backup_snapshot = _receipt_disposal_snapshot(
            backup_fd, backup.receipt)
        if (
            backup_snapshot is None
            or _exact_tree_snapshot(backup_fd) != backup_snapshot
        ):
            return None
        held_backup = {}
        _hold_snapshot_files(backup_fd, backup_snapshot, held_backup)
        for planned in intent["files"]:
            source = held_backup.get(planned["relative"])
            if (
                source is None
                or not source["lease"].intact()
                or _snapshot_file_from_descriptor(source["descriptor"])
                    != planned["source_snapshot"]
                or _serializable_fidelity_fd(source["descriptor"])
                    != planned["source_fidelity"]
            ):
                return None

        if committed is not None:
            proof = _hold_album_proof(
                replacement_path, committed["post_receipt"])
            try:
                all_public = {
                    item["relative"]
                    for item in (*ready["directories"], *ready["files"])
                }
                if (
                    proof is None
                    or not _companion_tree_matches(
                        album_opened,
                        intent,
                        ready,
                        all_public,
                        include_directory_mtime=True,
                    )
                ):
                    return None
            finally:
                _release_album_proof(proof)
            if workspace_fd is not None and not _cleanup_companion_workspace(
                    parent_fd, intent["workspace_name"], workspace_fd):
                return None
            return _canonical_receipt(committed["post_receipt"])

        units = _open_companion_units(
            album_opened, parent_fd, workspace_fd, intent, ready)
        public_relatives = {
            unit["prepared"]["relative"]
            for unit in units if unit["location"] == "public"
        }
        album_snapshot = _exact_tree_snapshot(album_fd)
        if album_snapshot is None:
            return None
        held_album = {}
        _hold_snapshot_files(album_fd, album_snapshot, held_album)
        if not _companion_tree_matches(
                album_opened,
                intent,
                ready,
                public_relatives,
                include_directory_mtime=False,
        ):
            return None

        for unit in units:
            if unit["location"] == "public":
                continue
            if workspace_fd is None:
                return None
            if not _companion_tree_matches(
                    album_opened,
                    intent,
                    ready,
                    public_relatives,
                    include_directory_mtime=False,
            ):
                return None
            target_parents = _open_relative_directories(
                album_fd, unit["relative"][:-1], create=False)
            target_parent_fd = (
                target_parents[-1] if target_parents else album_fd)
            try:
                if not _named_entry_missing(
                        target_parent_fd, unit["relative"][-1]):
                    return None
                deferred = _rename_exact_noreplace_at(
                    workspace_fd,
                    unit["prepared"]["stage_name"],
                    target_parent_fd,
                    unit["relative"][-1],
                    unit["descriptor"],
                )
                if not (
                    _named_entry_missing(
                        workspace_fd, unit["prepared"]["stage_name"])
                    and _named_entry_matches(
                        target_parent_fd,
                        unit["relative"][-1],
                        unit["descriptor"],
                    )
                ):
                    return None
                unit["location"] = "public"
                public_relatives.add(unit["prepared"]["relative"])
                parent_relative = "/".join(unit["relative"][:-1])
                if not _restore_companion_parent_mtime(
                        target_parent_fd, parent_relative, intent):
                    return None
                if deferred is not None:
                    raise deferred
            finally:
                _close_descriptors(target_parents)

        # A fatal signal may have landed after the exact rename but before
        # that target parent's pre-transaction mtime was restored.  Once the
        # READY-bound inode and the otherwise exact tree have been proved,
        # restoring this one known mutation makes forward adoption complete.
        for unit in units:
            target_parents = _open_relative_directories(
                album_fd, unit["relative"][:-1], create=False)
            target_parent_fd = (
                target_parents[-1] if target_parents else album_fd)
            try:
                if (
                    not _named_entry_matches(
                        target_parent_fd,
                        unit["relative"][-1],
                        unit["descriptor"],
                    )
                    or not _restore_companion_parent_mtime(
                        target_parent_fd,
                        "/".join(unit["relative"][:-1]),
                        intent,
                    )
                ):
                    return None
            finally:
                _close_descriptors(target_parents)

        if not _companion_tree_matches(
                album_opened,
                intent,
                ready,
                public_relatives,
                include_directory_mtime=True,
        ):
            return None
        post_receipt = _album_source_receipt_from_opened(*album_opened)
        if post_receipt is None:
            return None
        committed = {
            "version": 1,
            "transaction": intent["transaction"],
            "ready_sha256": _companion_marker_digest(ready),
            "post_receipt": post_receipt,
        }
        if (
            not _companion_committed_is_valid(committed, intent, ready)
            or not _write_companion_marker(
                backup,
                _COMPANION_CARRY_COMMITTED_SENTINEL,
                committed,
            )
        ):
            return None
        if workspace_fd is not None and not _cleanup_companion_workspace(
                parent_fd, intent["workspace_name"], workspace_fd):
            return None
        return _canonical_receipt(post_receipt)
    except (OSError, TypeError, ValueError, shutil.Error):
        return None
    finally:
        if held_album is not None:
            _release_held_snapshot_files(held_album)
        _close_companion_units(units)
        if held_backup is not None:
            _release_held_snapshot_files(held_backup)
        if workspace_fd is not None:
            os.close(workspace_fd)
        if album_opened is not None:
            _close_descriptors(album_opened[-1])
        _close_descriptors(backup_opened[-1])


def carry_backup_companions(
        backup,
        replacement_path,
        *,
        expected_replacement_receipt=None,
        expected_owner=None,
):
    """Carry absent non-audio payload from an exact backup without overwrite.

    ``backup`` must be the carried complete ``BackupResult`` and the expected
    replacement receipt must describe the exact public album before this call.
    Both trees stay writer-excluded.  Existing byte-identical companions count
    as already carried; every conflict is left untouched and keeps the backup.
    On success, returns the replacement's new receipt for the later semantic
    disposal gate.  Any uncertainty returns ``None`` and leaves the backup.
    """
    if not _backup_owner_authorized(backup, expected_owner):
        return None
    opened = _validated_backup_result(backup, require_complete=True)
    if opened is None:
        return None
    backup_fd = opened[-1][-1]
    replacement_proof = None
    held_backup = None
    intent = None
    try:
        intent_present, intent = _companion_marker_value(
            backup_fd, _COMPANION_CARRY_INTENT_SENTINEL, backup.receipt)
        ready_present, ready = _companion_marker_value(
            backup_fd, _COMPANION_CARRY_READY_SENTINEL, backup.receipt)
        committed_present, committed = _companion_marker_value(
            backup_fd, _COMPANION_CARRY_COMMITTED_SENTINEL, backup.receipt)
        if intent_present:
            if (
                intent is None
                or not _companion_intent_is_valid(
                    intent, backup, replacement_path)
                or committed_present and not ready_present
                or ready_present and ready is None
                or committed_present and committed is None
            ):
                return None
        elif ready_present or committed_present:
            return None

        if intent is None:
            replacement_proof = _hold_album_proof(
                replacement_path, expected_replacement_receipt)
            if replacement_proof is None:
                return None
            replacement_fd = replacement_proof["descriptors"][-1]
            if _same_directory(
                    os.fstat(backup_fd), os.fstat(replacement_fd)):
                return None
            backup_snapshot = _receipt_disposal_snapshot(
                backup_fd, backup.receipt)
            if (
                backup_snapshot is None
                or _exact_tree_snapshot(backup_fd) != backup_snapshot
            ):
                return None
            held_backup = {}
            _hold_snapshot_files(backup_fd, backup_snapshot, held_backup)
            backup_fidelity = _serializable_tree_fidelity(
                _tree_fidelity_snapshot(backup_fd))
            if backup_fidelity is None:
                return None
            companions = [
                relative
                for relative in backup.receipt["tree"]["files"]
                if (
                    PurePosixPath(relative).name not in _SIDECARS
                    and Path(PurePosixPath(relative).name).suffix.lower()
                        not in cfg.AUDIO_EXTS
                )
            ]
            missing_files = []
            for relative_text in sorted(companions):
                source = held_backup.get(relative_text)
                existing = replacement_proof["held"].get(relative_text)
                if (
                    source is None
                    or not source["lease"].intact()
                    or _snapshot_file_from_descriptor(source["descriptor"])
                        != backup_snapshot["files"][relative_text]
                ):
                    return None
                if existing is not None:
                    if _file_digest_fd(existing["descriptor"]) != (
                            backup_snapshot["files"][relative_text][
                                "sha256"]):
                        return None
                    continue
                if relative_text in replacement_proof[
                        "receipt"]["tree"]["directories"]:
                    return None
                missing_files.append(relative_text)
            if not missing_files:
                return _canonical_receipt(replacement_proof["receipt"])

            pre_directories = set(
                replacement_proof["receipt"]["tree"]["directories"])
            missing_directories = set()
            for relative_text in missing_files:
                parts = _companion_relative_parts(relative_text)
                for depth in range(1, len(parts)):
                    relative_directory = "/".join(parts[:depth])
                    if relative_directory in pre_directories:
                        continue
                    if relative_directory in replacement_proof[
                            "receipt"]["tree"]["files"]:
                        return None
                    missing_directories.add(relative_directory)
            directory_items = []
            for index, relative_text in enumerate(sorted(
                    missing_directories,
                    key=lambda value: (value.count("/"), value))):
                fidelity = backup_fidelity["directories"].get(relative_text)
                if fidelity is None:
                    return None
                directory_items.append({
                    "relative": relative_text,
                    "stage_name": f"d{index:04d}",
                    "fidelity": fidelity,
                })
            file_items = []
            for index, relative_text in enumerate(missing_files):
                source = held_backup[relative_text]
                file_items.append({
                    "relative": relative_text,
                    "stage_name": f"f{index:04d}",
                    "source_snapshot": _snapshot_file_from_descriptor(
                        source["descriptor"]),
                    "source_fidelity": _serializable_fidelity_fd(
                        source["descriptor"]),
                })
            intent = {
                "version": 1,
                "transaction": secrets.token_hex(32),
                "backup_token": backup.receipt["token"],
                "origin": os.fspath(replacement_proof["public"]),
                "pre_receipt": _canonical_receipt(
                    replacement_proof["receipt"]),
                "workspace_name": (
                    f".ql-companion-carry-{secrets.token_hex(16)}"),
                "directories": directory_items,
                "files": file_items,
            }
            if (
                not _companion_intent_is_valid(
                    intent, backup, replacement_path)
                or not _write_companion_marker(
                    backup,
                    _COMPANION_CARRY_INTENT_SENTINEL,
                    intent,
                )
            ):
                return None
        elif _canonical_receipt(expected_replacement_receipt) != (
                intent["pre_receipt"]):
            if not ready_present or committed is None or (
                    _canonical_receipt(expected_replacement_receipt)
                    != committed["post_receipt"]):
                return None
    except (OSError, TypeError, ValueError, shutil.Error):
        return None
    finally:
        if held_backup is not None:
            _release_held_snapshot_files(held_backup)
        _release_album_proof(replacement_proof)
        _close_descriptors(opened[-1])

    if not ready_present:
        ready = _prepare_companion_ready(backup, replacement_path, intent)
        if ready is None:
            return None
    return _resume_companion_carry(
        backup,
        replacement_path,
        expected_replacement_receipt,
    )


def _write_backup_origin(bp: Path, origin: Path) -> bool:
    """Write the protective origin sidecar. Returns True only if it's actually
    on disk afterwards — the sidecar is the sole signal that keeps the age sweep
    from reaping a backup that's the only surviving copy, so a caller about to
    delete the original must treat a False here as a backup failure, not ignore
    it."""
    directory_fd = None
    try:
        if isinstance(bp, int):
            directory_fd = os.dup(bp)
        else:
            directory_fd = _open_backup_directory(bp)
            if not _same_directory(
                    os.fstat(directory_fd),
                    os.stat(bp, follow_symlinks=False)):
                return False
        return _write_text_noreplace_at(
            directory_fd, _ORIGIN_SIDECAR, str(origin))
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _read_backup_origin(bp: Path):
    f = bp / _ORIGIN_SIDECAR
    try:
        return Path(f.read_text(encoding="utf-8").strip()) if f.is_file() else None
    except OSError:
        return None


_KEEP_MARKERS = (
    _PARTIAL_RESTORE_SENTINEL,
    _UNVERIFIED_UPGRADE_SENTINEL,
    _COMPANION_CARRY_INTENT_SENTINEL,
    _COMPANION_CARRY_READY_SENTINEL,
    _COMPANION_CARRY_COMMITTED_SENTINEL,
)


def backup_keep_markers_present(bp) -> bool:
    """True when an explicit keep pin protects this backup from the age sweep."""
    return any((Path(bp) / marker).is_file() for marker in _KEEP_MARKERS)


def _backup_safe_to_reap(bp: Path) -> bool:
    """True ONLY when ``bp`` is provably redundant — every track it holds is
    confirmed back at its origin. The age sweep reaps on this, so the burden of
    proof is on "safe to delete", not on "must keep": any uncertainty (no
    sidecar, unreadable origin, a keep marker, a track not proven back) means we
    cannot prove redundancy and the backup is KEPT.

    Redundancy is proved by CONTENT, not file count: every track in the backup
    must be back at the origin under the same relative path and at least as
    large. A bare count match is fooled when restored or gap-filled files
    inflate the origin's count while one of the backup's own tracks is still
    missing or short there — exactly the case that strands the only good copy.

    This is deliberately the inverse of a "protect if marked" scheme. A backup
    can become the only surviving copy whenever the originals were moved into it
    and not fully put back, and the protective sidecar/sentinel writes are
    best-effort — on the exact filesystem failures that strand a sole copy
    (ENOSPC, RO remount, EACCES) those writes can themselves fail. Making
    "keep" the default means no protective write has to succeed for the data to
    be safe; the worst case of a missing marker is a stranded backup the user
    clears by hand, never silent loss."""
    # Explicit keep markers: a partial restore, or an upgrade kept because it
    # couldn't be verified complete — never reap either.
    if backup_keep_markers_present(bp):
        return False
    entries = _list_tree(bp)
    if entries is None:
        return False                       # can't read the backup → can't prove redundant
    try:
        tracks = [f for f in entries if f.is_file() and f.name not in _SIDECARS]
    except OSError:
        return False
    if not tracks:
        return True                        # holds no tracks → nothing to lose → reap the husk
    origin = _read_backup_origin(bp)
    if origin is None or not origin.exists():
        return False                       # can't locate origin → can't prove redundant
    for f in tracks:
        try:
            dst = origin / f.relative_to(bp)
            if not dst.is_file() or dst.stat().st_size < f.stat().st_size:
                return False               # this track isn't proven back at the origin
        except OSError:
            return False
    return True


def pin_unverified_upgrade_backup(backup: BackupResult,
                                  note: str | None = None, *,
                                  expected_owner=None) -> bool:
    """Mark a backup as never-reap: it holds something the age sweep's
    redundancy proof can't see. Content-presence reaping already keeps a
    backup whose tracks aren't all proven back at the origin, but a same-path,
    same-or-larger file at the origin defeats the byte check — a hi-res re-rip
    after an unverified upgrade, or a repair refill whose original tags/art
    survive only in the backup. For exactly those, this marker is the ONLY
    protection, so a failed or unflushed write returns False and the caller
    must warn the user the backup is unprotected — swallowing it (ENOSPC is
    likeliest right after a download) leaves the sole copy one age sweep from
    deletion with no sign anything is wrong. ``note`` names the reason when
    the default upgrade wording doesn't fit."""
    if not _backup_owner_authorized(backup, expected_owner):
        return False
    return _write_receipt_marker(
        backup,
        _UNVERIFIED_UPGRADE_SENTINEL,
        note or "upgrade kept — replacement not verified complete; "
                "the only full copy",
    )


def warn_pin_failed(bp: Path) -> None:
    """One shared warning for a failed pin: every caller just decided the
    backup holds a sole copy, so the message has to say the auto-clean
    protection did NOT take."""
    log.info(fmt(C.RED,
        f"  ✗  Couldn't write the keep marker on this backup — the "
        f"scheduled auto-clean may treat it as redundant and remove it.\n"
        f"     Copy what you need out of {bp} now."))


# Memoize the orphan walk: every settings load/submit and the dashboard call
# _diagnostics(), which calls this, and it rglob-walks every retained backup
# to content-check redundancy. A burst of those hits (form POST → redirect →
# dashboard render) would otherwise re-walk the whole backup tree each time.
_ONLY_COPY_TTL_SEC = 10.0
_only_copy_cache: tuple[float, float, list] | None = None
# Executor threads (retention) and the web diagnostic both call
# find_only_copy_backups; the lock keeps the memo read-modify-write atomic and
# stops two callers re-walking the tree at once on a cache miss.
_only_copy_lock = threading.Lock()


def find_only_copy_backups():
    """Backups whose recorded origin is gone or still short of them — orphaned
    by a hard kill that skipped the caller's restore/delete. Retention keeps
    these; the web diagnostic surfaces them so the user can recover or clear
    them (each holds the origin path in its sidecar).

    Memoized for a few seconds keyed on the backup dir's mtime — see
    _ONLY_COPY_TTL_SEC — so repeated diagnostics don't each re-walk the tree."""
    global _only_copy_cache
    if not cfg.UPGRADE_BACKUP_DIR.exists():
        with _only_copy_lock:
            _only_copy_cache = None
        return []
    try:
        dir_mtime = cfg.UPGRADE_BACKUP_DIR.stat().st_mtime
    except OSError:
        dir_mtime = 0.0
    now = time.time()
    with _only_copy_lock:
        cached = _only_copy_cache
        if (cached is not None and cached[1] == dir_mtime
                and now - cached[0] < _ONLY_COPY_TTL_SEC):
            return cached[2]

        out = []
        try:
            for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
                if not entry.is_dir():
                    continue
                # A genuine mid-copy '.partial' (sidecar not yet written) isn't a
                # real backup; a committed backup whose album name merely ends in
                # '.partial' DOES carry the origin sidecar and must still surface.
                if entry.name.endswith(".partial") and not (entry / _ORIGIN_SIDECAR).is_file():
                    continue
                # A deliberate undo copy (downsample originals): its origin
                # holds the smaller rewrite on purpose, so it always looks
                # "not redundant" here — but it isn't orphaned, and the
                # diagnostics list it feeds shows it separately.
                if (entry / _REAP_AFTER_RETENTION_SENTINEL).is_file():
                    continue
                if not _backup_safe_to_reap(entry):
                    origin_root = entry
                    manifest = None
                    if entry.name.startswith(".ql-dispose-backup-"):
                        held = entry / "held"
                        if held.is_dir():
                            origin_root = held
                        manifest = _ownerless_disposal_manifest_for_path(
                            entry)
                    origin = _read_backup_origin(origin_root)
                    if origin is None and manifest is not None:
                        origin = Path(manifest["receipt"]["origin"])
                    out.append((entry, origin))
        except OSError:
            pass
        _only_copy_cache = (now, dir_mtime, out)
        return out


def _ownerless_disposal_manifest_for_path(path):
    """Read one direct-child wrapper's manifest through no-follow handles."""
    opened = _open_rooted_directory(
        cfg.UPGRADE_BACKUP_DIR, Path(path))
    if opened is None:
        return None
    _public, _root, parts, descriptors = opened
    manifest_fd = None
    try:
        if len(parts) != 1:
            return None
        manifest, manifest_fd = _read_ownerless_disposal_manifest_at(
            descriptors[-1],
            expected_quarantine_name=parts[0],
        )
        return manifest
    finally:
        if manifest_fd is not None:
            os.close(manifest_fd)
        _close_descriptors(descriptors)


def _source_receipt_shape_is_valid(receipt, origin=None) -> bool:
    """Structural gate for the pre-retirement album authority in a backup."""
    return _source_receipt_nested_schema_valid(receipt, origin)


def _source_parent_is_bound(music_root, source_parts, parent_fds,
                            source_receipt) -> bool:
    """Prove every ancestor is the same statx incarnation as preflight."""
    try:
        expected = source_receipt["path_generations"]
        return (
            len(expected) == len(source_parts) + 1
            and len(parent_fds) == len(source_parts)
            and _backup_source_is_public(
                music_root, source_parts[:-1], parent_fds)
            and _path_directory_generations(parent_fds) == expected[:-1]
        )
    except (OSError, TypeError, ValueError):
        return False


def _fidelity_without(value, *names):
    return {key: item for key, item in value.items() if key not in names}


def _source_root_is_bound(source_fd, source_receipt) -> bool:
    try:
        return _path_directory_generations([source_fd])[-1] == (
            source_receipt["path_generations"][-1])
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _retirement_recovery_state(
        source_parent_fd,
        source_name,
        source_fd,
        source_receipt,
        *,
        moved_to_backup=False,
):
    """Describe exactly where a failed whole-album retirement left its root."""
    try:
        if _named_directory_matches(
                source_parent_fd, source_name, source_fd):
            if _source_root_is_bound(source_fd, source_receipt):
                return {"version": 1, "location": "public"}
            return None

        with os.scandir(source_parent_fd) as iterator:
            names = sorted(
                entry.name for entry in iterator
                if entry.name.startswith(".ql-backup-remove-")
            )
        for quarantine_name in names:
            quarantine_fd = None
            held_fd = None
            try:
                quarantine_fd = _open_backup_directory(
                    quarantine_name, dir_fd=source_parent_fd)
                if not _named_directory_matches(
                        source_parent_fd, quarantine_name, quarantine_fd):
                    continue
                held_fd = _open_backup_directory("held", dir_fd=quarantine_fd)
                if (
                    not _same_directory(
                        os.fstat(held_fd), os.fstat(source_fd))
                    or not _named_directory_matches(
                        quarantine_fd, "held", held_fd)
                    or not _source_root_is_bound(held_fd, source_receipt)
                ):
                    continue
                return {
                    "version": 1,
                    "location": "hidden",
                    "quarantine_name": quarantine_name,
                    "quarantine_generation": _path_directory_generations(
                        [quarantine_fd])[-1],
                }
            except (OSError, TypeError, ValueError):
                continue
            finally:
                if held_fd is not None:
                    os.close(held_fd)
                if quarantine_fd is not None:
                    os.close(quarantine_fd)

        if (
            _named_entry_missing(source_parent_fd, source_name)
            and (moved_to_backup or int(os.fstat(source_fd).st_nlink) == 0)
        ):
            return {"version": 1, "location": "absent"}
    except (OSError, TypeError, ValueError):
        pass
    return None


def _write_upgrade_recovery_state(backup, state) -> bool:
    if state is None:
        return False
    try:
        note = json.dumps(state, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return _write_receipt_marker(
        backup, _UPGRADE_RECOVERY_STATE_SENTINEL, note)


def _read_upgrade_recovery_state(directory_fd, receipt):
    note = _receipt_marker_note(
        directory_fd, _UPGRADE_RECOVERY_STATE_SENTINEL, receipt)
    if note is None:
        return None
    try:
        state = decode_recovery_json(note)
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("version") != 1:
        return None
    location = state.get("location")
    if location in {"public", "absent"} and set(state) == {
            "version", "location"}:
        return state
    if (
        location == "hidden"
        and set(state) == {
            "version", "location", "quarantine_name",
            "quarantine_generation",
        }
        and isinstance(state["quarantine_name"], str)
        and state["quarantine_name"].startswith(".ql-backup-remove-")
        and isinstance(state["quarantine_generation"], list)
    ):
        return state
    return None


def _upgrade_recovery_carrier(path, directory_fd, *, requested,
                              backed_up=None):
    """Return a non-authoritative path carrier when owned data was retained."""
    receipt = _read_backup_receipt(directory_fd)
    if receipt is not None:
        return BackupResult(
            Path(path),
            complete=False,
            receipt=receipt,
            requested=receipt["requested"],
            backed_up=receipt["backed_up"],
        )
    if backed_up is None:
        manifest = _tree_manifest(directory_fd)
        backed_up = len(manifest) if manifest is not None else 0
    return BackupResult(
        Path(path),
        complete=False,
        receipt=None,
        requested=int(requested),
        backed_up=int(backed_up),
    )


def _complete_upgrade_backup(backup):
    if (
        not isinstance(backup, BackupResult)
        or backup.complete
        or backup.receipt is None
        or backup.receipt.get("kind") != "upgrade"
        or backup.receipt.get("complete") is not False
    ):
        return None
    if not _write_receipt_marker(
            backup,
            _UPGRADE_RETIREMENT_COMPLETE_SENTINEL,
            "whole-album source retirement committed",
    ):
        return None
    completed = BackupResult(
        backup.path,
        complete=True,
        receipt=backup.receipt,
        requested=backup.requested,
        backed_up=backup.backed_up,
    )
    opened = _validated_backup_result(
        completed, kinds={"upgrade"}, require_complete=True)
    if opened is None:
        return None
    _public, _root, _parts, descriptors = opened
    _close_descriptors(descriptors)
    return completed


def backup_album_dir(album_dir: Path, *, expected_receipt=None, owner=None,
                     on_intent=None):
    """Move album_dir to a timestamped, receipt-bound backup.

    Returns a complete ``BackupResult`` on success, an incomplete result when
    this call owns retained recovery data, or ``None`` only when no recovery
    was retained. The source is held through a complete no-follow chain beneath
    MUSIC_ROOT. Cross-filesystem backups copy from that held directory, then
    move the exact source into a private quarantine before recursive removal.
    """
    owner = normalise_recovery_owner(owner)
    if (owner is None) != (on_intent is None):
        raise ValueError("owned backups require an intent checkpoint")
    if on_intent is not None and not callable(on_intent):
        raise ValueError("backup intent checkpoint must be callable")

    opened = _open_backup_source(Path(album_dir))
    if opened is None:
        log.info(fmt(
            C.RED,
            f"  ✗  Refusing to back up an album outside the real, no-follow "
            f"music tree: {album_dir}."))
        return None
    public, music_root, source_parts, source_fds = opened
    source_parent_fd = source_fds[-2]
    source_fd = source_fds[-1]
    source_name = source_parts[-1]
    backup_root_fd = None
    committed_fd = None
    partial_fd = None
    source_snapshot = None
    source_fidelity = None
    source_generations = None
    source_path_generations = None
    held_source_files = None
    committed_snapshot = None
    held_committed_files = None
    held_committed_payload = None
    try:
        if _reserved_backup_entry_present(source_fd):
            log.info(fmt(
                C.RED,
                f"  ✗  Refusing to back up {public}: it contains a reserved "
                "backup metadata name."))
            return None
        source_snapshot = _exact_tree_snapshot(source_fd)
        source_fidelity = _tree_fidelity_snapshot(source_fd)
        try:
            source_generations = _tree_directory_generations(
                source_fd, source_snapshot) if source_snapshot else None
            source_path_generations = _path_directory_generations(source_fds)
        except OSError:
            source_generations = None
            source_path_generations = None
        if (
            source_snapshot is None
            or source_fidelity is None
            or source_generations is None
            or source_path_generations is None
        ):
            log.info(fmt(
                C.RED,
                f"  ✗  Couldn't seal the exact source tree at {public}; "
                "leaving it in place."))
            return None
        current_source_receipt = {
            "version": 1,
            "origin": os.fspath(public),
            "tree": source_snapshot,
            "fidelity": _serializable_tree_fidelity(source_fidelity),
            "directory_generations": source_generations,
            "path_generations": source_path_generations,
        }
        if expected_receipt is not None and expected_receipt != current_source_receipt:
            log.info(fmt(
                C.RED,
                f"  ✗  {public} changed after it was selected; leaving "
                "the current tree untouched."))
            return None
        try:
            held_source_files = {}
            _hold_snapshot_files(
                source_fd, source_snapshot, held_source_files)
        except OSError as exc:
            log.info(fmt(
                C.RED,
                f"  ✗  Couldn't exclude writers from {public} ({exc}); "
                "leaving it in place."))
            return None
        try:
            source_still_sealed = (
                _tree_directory_generations(source_fd, source_snapshot)
                    == source_generations
                and _path_directory_generations(source_fds)
                    == source_path_generations
                and _tree_fidelity_snapshot(source_fd) == source_fidelity
                and _held_snapshot_files_intact(
                    source_fd, source_snapshot, held_source_files)
            )
        except (OSError, TypeError, ValueError):
            source_still_sealed = False
        if not source_still_sealed:
            log.info(fmt(
                C.RED,
                f"  ✗  {public} changed while its transaction was "
                "sealed; leaving it untouched."))
            return None
        try:
            bp = _upgrade_backup_path_for(public)
            backup_root = Path(os.path.abspath(os.fspath(bp.parent)))
            backup_root_fd = _open_backup_directory(backup_root)
            if not _backup_root_is_public(backup_root, backup_root_fd):
                raise OSError("backup directory changed while it was opened")
        except OSError as exc:
            log.info(fmt(C.RED, f"  ✗  Couldn't prepare backup path: {exc}."))
            return None

        if owner is not None:
            source_intent = _canonical_receipt(current_source_receipt)
            if source_intent is None:
                raise OSError("backup source intent could not be serialized")
            on_intent({
                "version": 1,
                "kind": "upgrade",
                "owner": dict(owner),
                "path": os.fspath(bp),
                "origin": os.fspath(public),
                "source_receipt": source_intent,
            })

        if (
            _same_filesystem(public, backup_root)
            and not _tree_has_multiply_linked_file(source_fidelity)
        ):
            if not _backup_source_is_public(
                    music_root, source_parts, source_fds):
                log.info(fmt(C.RED, "  ✗  Album path changed before backup."))
                return None
            try:
                move_exception = _rename_exact_noreplace_at(
                    source_parent_fd,
                    source_name,
                    backup_root_fd,
                    bp.name,
                    source_fd,
                )
                if move_exception is not None:
                    _restore_exact_backup_move(
                        backup_root_fd,
                        bp.name,
                        source_parent_fd,
                        source_name,
                        source_fd,
                    )
                    raise move_exception
            except (KeyboardInterrupt, SystemExit):
                raise
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    from qobuz_librarian.ui_cli.errors import oserr_hint
                    log.info(fmt(
                        C.RED,
                        f"  ✗  Could not back up {public}: "
                        f"{exc}.{oserr_hint(exc)}"))
                    if _named_directory_matches(
                            backup_root_fd, bp.name, source_fd):
                        return _upgrade_recovery_carrier(
                            bp,
                            source_fd,
                            requested=len(source_snapshot["files"]),
                        )
                    return None
            else:
                try:
                    moved_fd = _open_backup_directory(
                        bp.name, dir_fd=backup_root_fd)
                except OSError:
                    restored = _restore_exact_backup_move(
                        backup_root_fd,
                        bp.name,
                        source_parent_fd,
                        source_name,
                        source_fd,
                    )
                    log.info(fmt(
                        C.RED,
                        "  ✗  Moved album could not be verified; "
                        + (
                            "the exact source was restored."
                            if restored
                            else f"the exact move remains at {bp}."
                        )))
                    if not restored:
                        return _upgrade_recovery_carrier(
                            bp,
                            source_fd,
                            requested=len(source_snapshot["files"]),
                        )
                    return None
                try:
                    if not _same_directory(
                            os.fstat(moved_fd), os.fstat(source_fd)):
                        _restore_exact_backup_move(
                            backup_root_fd,
                            bp.name,
                            source_parent_fd,
                            source_name,
                            moved_fd,
                        )
                        log.info(fmt(
                            C.RED,
                            "  ✗  Album path changed during backup; the "
                            "replacement was preserved."))
                        return None
                finally:
                    os.close(moved_fd)

                if (
                    not _named_entry_missing(source_parent_fd, source_name)
                    or not _named_directory_matches(
                        backup_root_fd, bp.name, source_fd)
                    or not _backup_source_is_public(
                        music_root,
                        source_parts,
                        source_fds,
                        include_album=False,
                    )
                    or not _source_parent_is_bound(
                        music_root,
                        source_parts,
                        source_fds[:-1],
                        current_source_receipt,
                    )
                    or not _backup_root_is_public(
                        backup_root, backup_root_fd)
                    or not _fsync_directory_fds(
                        source_parent_fd, backup_root_fd)
                ):
                    restored = _restore_exact_backup_move(
                        backup_root_fd,
                        bp.name,
                        source_parent_fd,
                        source_name,
                        source_fd,
                    )
                    log.info(fmt(C.RED, "  ✗  Backup move could not be verified."))
                    if not restored and _named_directory_matches(
                            backup_root_fd, bp.name, source_fd):
                        return _upgrade_recovery_carrier(
                            bp,
                            source_fd,
                            requested=len(source_snapshot["files"]),
                        )
                    return None

                result = None
                try:
                    if (
                        _reserved_backup_entry_present(source_fd)
                        or not _held_snapshot_files_intact(
                            source_fd, source_snapshot, held_source_files)
                    ):
                        raise OSError("source tree changed during backup")
                    if not _write_backup_origin_durable(source_fd, public):
                        raise OSError("backup origin could not be committed")
                    manifest = _tree_manifest(source_fd)
                    if manifest is None:
                        raise OSError("backup payload could not be verified")
                    result = _seal_backup_result(
                        bp,
                        source_fd,
                        public,
                        kind="upgrade",
                        complete=False,
                        requested=len(manifest),
                        backed_up=len(manifest),
                        source_receipt=current_source_receipt,
                        owner=owner,
                    )
                    if result.receipt is None:
                        raise OSError(
                            "backup ownership receipt could not be committed")
                    if (
                        not _named_entry_missing(
                            source_parent_fd, source_name)
                        or not _named_directory_matches(
                            backup_root_fd, bp.name, source_fd)
                        or not _backup_source_is_public(
                            music_root,
                            source_parts,
                            source_fds,
                            include_album=False,
                        )
                        or not _source_parent_is_bound(
                            music_root,
                            source_parts,
                            source_fds[:-1],
                            current_source_receipt,
                        )
                        or not _backup_root_is_public(
                            backup_root, backup_root_fd)
                        or _read_backup_receipt(source_fd) != result.receipt
                    ):
                        raise OSError(
                            "album or library path changed after backup")
                    completed = _complete_upgrade_backup(result)
                    if completed is None:
                        raise OSError(
                            "backup completion marker could not be committed")
                    return completed
                except BaseException as exc:
                    authoritative_result = (
                        result is not None
                        and result.receipt is not None
                        and _read_backup_receipt(source_fd) == result.receipt
                    )
                    restored = False
                    if not authoritative_result:
                        controls_removed = (
                            _remove_written_receipt(source_fd)
                            and _remove_written_origin(source_fd)
                        )
                        if controls_removed:
                            restored = _restore_exact_backup_move(
                                backup_root_fd,
                                bp.name,
                                source_parent_fd,
                                source_name,
                                source_fd,
                            )
                    elif _source_parent_is_bound(
                            music_root,
                            source_parts,
                            source_fds[:-1],
                            current_source_receipt):
                        state = _retirement_recovery_state(
                            source_parent_fd,
                            source_name,
                            source_fd,
                            current_source_receipt,
                            moved_to_backup=True,
                        )
                        _write_upgrade_recovery_state(result, state)
                    if restored:
                        log.info(fmt(
                            C.YELLOW,
                            "  ⚠  Backup did not commit; the exact album "
                            "was restored."))
                    else:
                        log.info(fmt(
                            C.RED,
                            f"  ✗  Backup could not finish cleanly; the "
                            f"exact held tree remains discoverable at {bp}."))
                    if not isinstance(exc, OSError):
                        raise
                    if restored:
                        return None
                    return _upgrade_recovery_carrier(
                        bp,
                        source_fd,
                        requested=len(source_snapshot["files"]),
                    )

        held_source = _held_directory_path(source_fd)
        src_stats = _tree_stats(held_source)
        if src_stats is None:
            log.info(fmt(
                C.RED,
                f"  ✗  Couldn't stat source tree at {public}; refusing to "
                "back up."))
            return None
        n_files, total_bytes = src_stats
        src_digest = _tree_digest(held_source)
        if (
            src_digest is None
            or not _held_snapshot_files_intact(
                source_fd, source_snapshot, held_source_files)
        ):
            log.info(fmt(
                C.RED,
                f"  ✗  Couldn't read source tree at {public} (unreadable or "
                "a special file); refusing to back up."))
            return None
        log.info(fmt(
            C.GRAY,
            f"  ⤷  Cross-filesystem backup: copying {n_files} file(s) / "
            f"{total_bytes / 1024 / 1024:.1f} MB to {backup_root}…"))

        partial_name = f".ql-backup-copy-{secrets.token_hex(16)}"
        partial_path = _held_directory_path(backup_root_fd) / partial_name
        try:
            shutil.copytree(
                os.fspath(held_source),
                os.fspath(partial_path),
                symlinks=True,
            )
            partial_fd = _open_backup_directory(
                partial_name, dir_fd=backup_root_fd)
            held_partial = _held_directory_path(partial_fd)
            copied_fidelity = _tree_fidelity_snapshot(partial_fd)
            if (
                _tree_digest(held_partial) != src_digest
                or _tree_digest(held_source) != src_digest
                or _tree_fidelity_snapshot(source_fd) != source_fidelity
                or not _tree_copy_fidelity_matches(
                    source_fidelity, copied_fidelity)
                or not _held_snapshot_files_intact(
                    source_fd, source_snapshot, held_source_files)
            ):
                raise OSError("backup copy did not match the held source")
            committed_payload_snapshot = _exact_tree_snapshot(partial_fd)
            if committed_payload_snapshot is None:
                raise OSError("backup copy could not be snapshotted")
            held_committed_payload = {}
            _hold_snapshot_files(
                partial_fd,
                committed_payload_snapshot,
                held_committed_payload,
            )
            if not _fsync_exact_tree(
                    partial_fd,
                    committed_payload_snapshot,
                    held_committed_payload,
            ):
                raise OSError("backup copy could not be flushed to disk")
            move_exception = _rename_exact_noreplace_at(
                backup_root_fd,
                partial_name,
                backup_root_fd,
                bp.name,
                partial_fd,
            )
            committed_fd = partial_fd
            partial_fd = None
            if move_exception is not None:
                raise move_exception
            if (
                not _named_directory_matches(
                    backup_root_fd, bp.name, committed_fd)
                or not _named_entry_missing(backup_root_fd, partial_name)
                or not _fsync_directory_fds(backup_root_fd)
            ):
                raise OSError("backup could not be committed safely")
        except (KeyboardInterrupt, SystemExit):
            log.info(fmt(
                C.YELLOW,
                f"\n  ⚠  Backup interrupted mid-copy. Original at {public} "
                "is intact; removing the partial backup."))
            if partial_fd is None:
                try:
                    partial_fd = _open_backup_directory(
                        partial_name, dir_fd=backup_root_fd)
                except OSError:
                    pass
            if partial_fd is not None:
                _remove_exact_tree_at(
                    backup_root_fd,
                    partial_name,
                    partial_fd,
                    prefix="ql-backup-partial",
                )
            if (
                committed_fd is not None
                and _backup_source_is_public(
                    music_root, source_parts, source_fds)
                and _tree_digest(held_source) == src_digest
            ):
                _remove_exact_tree_at(
                    backup_root_fd,
                    bp.name,
                    committed_fd,
                    prefix="ql-backup-interrupted",
                )
            raise
        except (OSError, shutil.Error) as exc:
            log.info(fmt(C.RED, f"  ✗  Cross-filesystem backup failed: {exc}."))
            if partial_fd is None:
                try:
                    partial_fd = _open_backup_directory(
                        partial_name, dir_fd=backup_root_fd)
                except OSError:
                    pass
            if partial_fd is not None:
                _remove_exact_tree_at(
                    backup_root_fd,
                    partial_name,
                    partial_fd,
                    prefix="ql-backup-partial",
                )
            source_unchanged = (
                _backup_source_is_public(
                    music_root, source_parts, source_fds)
                and _source_parent_is_bound(
                    music_root, source_parts, source_fds[:-1],
                    current_source_receipt)
                and _source_root_is_bound(
                    source_fd, current_source_receipt)
                and _tree_digest(held_source) == src_digest
            )
            discarded = False
            if committed_fd is not None and source_unchanged:
                discarded = _remove_exact_tree_at(
                    backup_root_fd,
                    bp.name,
                    committed_fd,
                    prefix="ql-backup-discard",
                )
            if committed_fd is not None and not discarded:
                return _upgrade_recovery_carrier(
                    bp,
                    committed_fd,
                    requested=len(src_digest),
                )
            return None

        if (
            _reserved_backup_entry_present(source_fd)
            or _reserved_backup_entry_present(committed_fd)
        ):
            source_is_public = (
                _backup_source_is_public(
                    music_root, source_parts, source_fds)
                and _source_parent_is_bound(
                    music_root, source_parts, source_fds[:-1],
                    current_source_receipt)
                and _source_root_is_bound(
                    source_fd, current_source_receipt)
            )
            discarded = source_is_public and _remove_exact_tree_at(
                backup_root_fd,
                bp.name,
                committed_fd,
                prefix="ql-backup-discard",
            )
            log.info(fmt(
                C.RED,
                "  ✗  Reserved backup metadata appeared during the copy; "
                + (
                    "the original remains in place and the copy was discarded."
                    if discarded
                    else f"the exact copy is retained at {bp}."
                )))
            if discarded:
                return None
            return _upgrade_recovery_carrier(
                bp,
                committed_fd,
                requested=len(src_digest),
            )

        if not _write_backup_origin_durable(committed_fd, public):
            source_unchanged = (
                _backup_source_is_public(
                    music_root, source_parts, source_fds)
                and _source_parent_is_bound(
                    music_root, source_parts, source_fds[:-1],
                    current_source_receipt)
                and _source_root_is_bound(
                    source_fd, current_source_receipt)
                and _tree_digest(held_source) == src_digest
            )
            discarded = source_unchanged and _remove_exact_tree_at(
                backup_root_fd,
                bp.name,
                committed_fd,
                prefix="ql-backup-discard",
            )
            log.info(fmt(
                C.RED,
                "  ✗  Backup copied but couldn't record its origin; "
                + (
                    "the original remains in place and the copy was discarded."
                    if discarded
                    else f"the exact copy is retained at {bp}."
                )))
            if discarded:
                return None
            return _upgrade_recovery_carrier(
                bp,
                committed_fd,
                requested=len(src_digest),
            )

        result = _seal_backup_result(
            bp,
            committed_fd,
            public,
            kind="upgrade",
            complete=False,
            requested=len(src_digest),
            backed_up=len(src_digest),
            source_receipt=current_source_receipt,
            owner=owner,
        )
        if result.receipt is None:
            source_unchanged = (
                _backup_source_is_public(
                    music_root, source_parts, source_fds)
                and _source_parent_is_bound(
                    music_root, source_parts, source_fds[:-1],
                    current_source_receipt)
                and _source_root_is_bound(
                    source_fd, current_source_receipt)
                and _tree_digest(held_source) == src_digest
            )
            discarded = source_unchanged and _remove_exact_tree_at(
                backup_root_fd,
                bp.name,
                committed_fd,
                prefix="ql-backup-discard",
            )
            log.info(fmt(
                C.RED,
                "  ✗  Backup copied but its exact ownership receipt could "
                "not be sealed; "
                + (
                    "the original remains in place and the copy was discarded."
                    if discarded
                    else f"the exact copy is retained at {bp}."
                )))
            if discarded:
                return None
            return _upgrade_recovery_carrier(
                bp,
                committed_fd,
                requested=len(src_digest),
                backed_up=len(src_digest),
            )

        def _retained_incomplete_result():
            if _source_parent_is_bound(
                    music_root,
                    source_parts,
                    source_fds[:-1],
                    current_source_receipt):
                state = _retirement_recovery_state(
                    source_parent_fd,
                    source_name,
                    source_fd,
                    current_source_receipt,
                )
                _write_upgrade_recovery_state(result, state)
            return result

        committed_snapshot = _receipt_disposal_snapshot(
            committed_fd, result.receipt)
        if (
            committed_snapshot is None
            or _exact_tree_snapshot(committed_fd) != committed_snapshot
        ):
            log.info(fmt(
                C.RED,
                "  ✗  Backup receipt changed before source retirement; "
                f"the original remains in place and the copy is at {bp}."))
            return _retained_incomplete_result()
        try:
            held_committed_files = {}
            _hold_snapshot_files(
                committed_fd, committed_snapshot, held_committed_files)
        except OSError as exc:
            log.info(fmt(
                C.RED,
                "  ✗  Couldn't exclude writers from the committed backup "
                f"({exc}); the original remains in place and the copy is "
                f"at {bp}."))
            return _retained_incomplete_result()
        if not _held_snapshot_files_intact(
                committed_fd, committed_snapshot, held_committed_files):
            return _retained_incomplete_result()
        _release_held_snapshot_files(held_committed_payload)
        held_committed_payload = None

        if (
            not _backup_source_is_public(
                music_root, source_parts, source_fds)
            or not _source_parent_is_bound(
                music_root,
                source_parts,
                source_fds[:-1],
                current_source_receipt,
            )
            or not _source_root_is_bound(
                source_fd, current_source_receipt)
            or _tree_digest(held_source) != src_digest
            or _read_backup_receipt(committed_fd) != result.receipt
            or not _held_snapshot_files_intact(
                committed_fd, committed_snapshot, held_committed_files)
            or not _named_directory_matches(
                backup_root_fd, bp.name, committed_fd)
            or not _backup_root_is_public(backup_root, backup_root_fd)
        ):
            log.info(fmt(
                C.RED,
                "  ✗  Album or library path changed while it was copied; "
                f"the replacement was left alone and the backup retained at {bp}."))
            return _retained_incomplete_result()

        try:
            removed = _remove_exact_tree_at(
                source_parent_fd,
                source_name,
                source_fd,
                prefix="ql-backup-remove",
                commit_guard=lambda: (
                    _held_snapshot_files_intact(
                        committed_fd,
                        committed_snapshot,
                        held_committed_files,
                    )
                    and _read_backup_receipt(committed_fd) == result.receipt
                    and _backup_source_is_public(
                        music_root,
                        source_parts,
                        source_fds,
                        include_album=False,
                    )
                    and _source_parent_is_bound(
                        music_root,
                        source_parts,
                        source_fds[:-1],
                        current_source_receipt,
                    )
                    and _named_directory_matches(
                        backup_root_fd, bp.name, committed_fd)
                    and _backup_root_is_public(
                        backup_root, backup_root_fd)
                ),
                expected_snapshot=source_snapshot,
                held_files=held_source_files,
            )
        except BaseException:
            _retained_incomplete_result()
            log.info(fmt(
                C.YELLOW,
                f"     The incomplete recovery remains at {bp}."))
            raise
        if not removed:
            log.info(fmt(
                C.RED,
                "  ✗  Backup succeeded but the exact source could not be "
                f"removed safely. The backup is retained at {bp}."))
            return _retained_incomplete_result()
        if (
            not _named_entry_missing(source_parent_fd, source_name)
            or not _backup_source_is_public(
                music_root,
                source_parts,
                source_fds,
                include_album=False,
            )
            or not _source_parent_is_bound(
                music_root,
                source_parts,
                source_fds[:-1],
                current_source_receipt,
            )
            or not _named_directory_matches(
                backup_root_fd, bp.name, committed_fd)
            or not _backup_root_is_public(backup_root, backup_root_fd)
        ):
            log.info(fmt(
                C.RED,
                "  ✗  The public album path changed as backup finished; "
                f"nothing at that path was touched and the backup is at {bp}."))
            return _retained_incomplete_result()
        completed = _complete_upgrade_backup(result)
        if completed is None:
            log.info(fmt(
                C.RED,
                "  ✗  Source retirement finished, but its durable completion "
                f"marker could not be committed. Recovery remains at {bp}."))
            return _retained_incomplete_result()
        return completed
    finally:
        if held_committed_files is not None:
            _release_held_snapshot_files(held_committed_files)
        if held_committed_payload is not None:
            _release_held_snapshot_files(held_committed_payload)
        if held_source_files is not None:
            _release_held_snapshot_files(held_source_files)
        if partial_fd is not None:
            try:
                os.close(partial_fd)
            except OSError:
                pass
        if committed_fd is not None:
            try:
                os.close(committed_fd)
            except OSError:
                pass
        if backup_root_fd is not None:
            try:
                os.close(backup_root_fd)
            except OSError:
                pass
        for descriptor in reversed(source_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass


def backup_gap_fill_files(file_paths, album_dir: Path, *,
                          expected_receipts=None, owner=None,
                          on_intent=None):
    """Back up selected files while every accepted source is writer-held.

    A verified source remains excluded from writers from receipt validation
    until a durable backup receipt exists.  Same-filesystem, single-link files
    move atomically; multiply-linked and cross-filesystem files become distinct
    fidelity-checked copies before the exact source link is removed.  An
    interruption after any forward step is reconciled into a durable partial
    receipt before cancellation is propagated.
    """
    owner = normalise_recovery_owner(owner)
    if (owner is None) != (on_intent is None):
        raise ValueError("owned backups require an intent checkpoint")
    if on_intent is not None and not callable(on_intent):
        raise ValueError("backup intent checkpoint must be callable")

    file_paths = list(file_paths)
    requested_count = len(file_paths)
    if not file_paths:
        return None
    expected = None
    if expected_receipts is not None:
        try:
            expected = _normalise_gap_fill_expected_receipts(
                expected_receipts)
        except OSError as exc:
            log.info(fmt(
                C.RED,
                f"  ✗  Refusing malformed gap-fill source receipts "
                f"({exc}); leaving every source file in place."))
            return None

    opened = _open_backup_source(Path(album_dir))
    if opened is None:
        log.info(fmt(
            C.RED,
            f"  ✗  Refusing to back up files outside the real, no-follow "
            f"music tree: {album_dir}."))
        return None
    public, music_root, album_parts, album_fds = opened
    album_fd = album_fds[-1]
    backup_root_fd = None
    backup_fd = None
    bp = None
    records = []
    completed = []

    def _source_intact(record):
        try:
            return (
                (record["source_lease"] is None
                 or record["source_lease"].intact())
                and _named_entry_matches(
                    record["source_parent_fd"],
                    record["parts"][-1],
                    record["source_fd"],
                )
                and _gap_fill_file_receipt(
                    record["source_fd"]
                ) == record["receipt"]
                and _relative_directories_are_named(
                    album_fd,
                    record["parts"][:-1],
                    record["source_parents"],
                )
                and _backup_source_is_public(
                    music_root, album_parts, album_fds)
            )
        except (OSError, TypeError, ValueError):
            return False

    def _backup_intact(record):
        try:
            return (
                record.get("backup_fd") is not None
                and record.get("backup_lease") is not None
                and record["backup_lease"].intact()
                and _named_entry_matches(
                    record["backup_parent_fd"],
                    record["parts"][-1],
                    record["backup_fd"],
                )
                and _file_digest_fd(record["backup_fd"])
                    == record["digest"]
                and _relative_directories_are_named(
                    backup_fd,
                    record["parts"][:-1],
                    record["backup_parents"],
                )
                and _named_directory_matches(
                    backup_root_fd, bp.name, backup_fd)
                and _backup_root_is_public(
                    backup_root, backup_root_fd)
            )
        except (OSError, TypeError, ValueError):
            return False

    def _refresh_remaining_hardlink_receipts(removed_record):
        """Account only for the ctime change caused by our own link removal."""
        expected_identity = (
            removed_record["receipt"]["type"],
            removed_record["receipt"]["device"],
            removed_record["receipt"]["inode"],
        )
        try:
            for other in records:
                if other is removed_record or other in completed:
                    continue
                prior = other["receipt"]
                identity = (
                    prior["type"], prior["device"], prior["inode"])
                if identity != expected_identity:
                    continue
                current = _gap_fill_file_receipt(other["source_fd"])
                if {
                    key: value for key, value in current.items()
                    if key != "ctime_ns"
                } != {
                    key: value for key, value in prior.items()
                    if key != "ctime_ns"
                }:
                    return False
                other["receipt"] = current
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _seal_retained(*, complete):
        manifest = _tree_manifest(backup_fd)
        backed_up = len(manifest) if manifest is not None else 0
        if backed_up == 0:
            return None
        result = None
        try:
            result = _seal_backup_result(
                bp,
                backup_fd,
                public,
                kind="gap-fill",
                complete=complete,
                requested=requested_count,
                backed_up=backed_up,
                owner=owner,
            )
        except BaseException:
            receipt = _read_backup_receipt(backup_fd)
            if receipt is not None:
                result = BackupResult(
                    bp,
                    complete=receipt["complete"],
                    receipt=receipt,
                    requested=receipt["requested"],
                    backed_up=receipt["backed_up"],
                )
            else:
                try:
                    result = _seal_backup_result(
                        bp,
                        backup_fd,
                        public,
                        kind="gap-fill",
                        complete=False,
                        requested=requested_count,
                        backed_up=backed_up,
                        owner=owner,
                    )
                except BaseException:
                    result = None
            raise
        return result

    try:
        seen = set()
        for raw_path in file_paths:
            rooted = _rooted_path_parts(public, Path(raw_path))
            if rooted is None or rooted[2][-1] in _SIDECARS:
                log.info(fmt(
                    C.RED,
                    f"  ✗  Refusing unsafe gap-fill source {raw_path}; "
                    "leaving every source file in place."))
                return None
            _source_path, _album_root, parts = rooted
            relative_text = "/".join(parts)
            if relative_text in seen:
                log.info(fmt(
                    C.RED,
                    "  ✗  A gap-fill source path is duplicated; leaving "
                    "every source file in place."))
                return None
            seen.add(relative_text)

            source_parents = []
            source_fd = None
            source_lease = None
            try:
                source_parents = _open_relative_directories(
                    album_fd, parts[:-1], create=False)
                source_parent_fd = (
                    source_parents[-1] if source_parents else album_fd)
                source_fd = _open_regular_file_at(
                    source_parent_fd, parts[-1])
                digest = _file_digest_fd(source_fd)
                receipt = _gap_fill_file_receipt(source_fd, digest)
                if expected is not None and expected.get(relative_text) != receipt:
                    raise OSError("verified source receipt changed")
                # A file the app user doesn't own can't carry a write lease.
                # With a sealed receipt pinning its exact content the copy
                # route below still moves it safely — every gate re-hashes
                # against the receipt, and the copy the app makes is its own,
                # so the rest of the transaction holds ordinary leases.
                source_lease = acquire_inode_write_exclusion(source_fd)
                if source_lease is not None and not source_lease.intact():
                    raise OSError("source has an active or uncertain writer")
                if source_lease is None and expected is None:
                    if os.fstat(source_fd).st_uid != os.geteuid():
                        raise OSError(
                            "the app does not own this file — check "
                            "ownership and PUID")
                    raise OSError("source has an active or uncertain writer")
                records.append({
                    "parts": parts,
                    "relative": relative_text,
                    "source_parents": source_parents,
                    "source_parent_fd": source_parent_fd,
                    "source_fd": source_fd,
                    "source_lease": source_lease,
                    "digest": digest,
                    "receipt": receipt,
                    "backup_parents": [],
                    "backup_parent_fd": None,
                    "backup_fd": None,
                    "backup_lease": None,
                    "backup_publication": None,
                })
                source_parents = []
                source_fd = None
                source_lease = None
            except FileNotFoundError:
                if expected is not None:
                    log.info(fmt(
                        C.RED,
                        f"  ✗  {parts[-1]} disappeared after verification; "
                        "leaving every remaining source in place."))
                    return None
            except OSError as exc:
                log.info(fmt(
                    C.RED,
                    f"  ✗  Couldn't bind {parts[-1]} for an exact backup "
                    f"({exc}); leaving every source file in place."))
                return None
            finally:
                if source_lease is not None:
                    source_lease.close()
                if source_fd is not None:
                    os.close(source_fd)
                _close_descriptors(source_parents)

        if expected is not None and seen != set(expected):
            log.info(fmt(
                C.RED,
                "  ✗  Gap-fill receipts do not cover exactly the requested "
                "files; leaving every source file in place."))
            return None
        if not records or not all(_source_intact(record) for record in records):
            log.info(fmt(
                C.RED,
                "  ✗  A gap-fill source changed while writer exclusions "
                "were acquired; leaving the public tree untouched."))
            return None

        try:
            cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup_root = Path(os.path.abspath(
                os.fspath(cfg.UPGRADE_BACKUP_DIR)))
            backup_root_fd = _open_backup_directory(backup_root)
            if not _backup_root_is_public(backup_root, backup_root_fd):
                raise OSError("backup directory changed while it was opened")
            bp = backup_root / _backup_dir_name(public, kind="gapfill")
        except (OSError, shutil.Error) as exc:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Couldn't prepare a protected gap-fill backup "
                f"({exc}); leaving every source file in place."))
            return None

        if owner is not None:
            on_intent({
                "version": 1,
                "kind": "gap-fill",
                "owner": dict(owner),
                "path": os.fspath(bp),
                "origin": os.fspath(public),
                "source_receipts": {
                    record["relative"]: dict(record["receipt"])
                    for record in records
                },
            })

        try:
            os.mkdir(bp.name, dir_fd=backup_root_fd)
            backup_fd = _open_backup_directory(
                bp.name, dir_fd=backup_root_fd)
            if (
                not _named_directory_matches(
                    backup_root_fd, bp.name, backup_fd)
                or not _fsync_directory_fds(backup_root_fd)
            ):
                raise OSError("gap-fill backup directory could not be committed")
            if not _write_backup_origin_durable(backup_fd, public):
                raise OSError("backup origin could not be committed")
        except (OSError, shutil.Error) as exc:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Couldn't create a protected gap-fill backup "
                f"({exc}); leaving every source file in place."))
            if (
                backup_fd is not None
                and _scan_backup_tree(backup_fd) == []
            ):
                _remove_exact_tree_at(
                    backup_root_fd,
                    bp.name,
                    backup_fd,
                    prefix="ql-gap-empty",
                )
            return None

        pending_exception = None
        fatal_change = False
        for record in records:
            destination_parents = []
            publication = None
            destination_parent_fd = None
            try:
                if not _source_intact(record):
                    raise OSError("source changed before backup")
                parts = record["parts"]
                destination_parents = _open_relative_directories(
                    backup_fd, parts[:-1], create=True)
                destination_parent_fd = (
                    destination_parents[-1]
                    if destination_parents else backup_fd)

                moved = False
                move_exception = None
                if (
                    record["source_lease"] is not None
                    and _same_filesystem(public, backup_root)
                    and os.fstat(record["source_fd"]).st_nlink == 1
                ):
                    try:
                        move_exception = _rename_exact_noreplace_at(
                            record["source_parent_fd"],
                            parts[-1],
                            destination_parent_fd,
                            parts[-1],
                            record["source_fd"],
                        )
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                    else:
                        moved = True

                if moved:
                    record.update({
                        "backup_parents": destination_parents,
                        "backup_parent_fd": destination_parent_fd,
                        "backup_fd": record["source_fd"],
                        "backup_lease": record["source_lease"],
                    })
                    destination_parents = []
                    if not (
                        _named_entry_missing(
                            record["source_parent_fd"], parts[-1])
                        and _named_entry_matches(
                            destination_parent_fd, parts[-1],
                            record["source_fd"],
                        )
                        and _backup_intact(record)
                        and _fsync_directory_fds(
                            record["source_parent_fd"], destination_parent_fd,
                        )
                    ):
                        raise OSError("moved backup could not be verified")
                    completed.append(record)
                    if move_exception is not None:
                        pending_exception = move_exception
                        fatal_change = True
                        break
                    continue

                publication = _CopyPublication(
                    destination_parent_fd, parts[-1])
                copied_digest = _copy_file_noreplace_at(
                    record["source_fd"],
                    publication,
                    adopt_owner=record["source_lease"] is None,
                )
                destination_fd = publication.descriptor
                destination_lease = publication.lease
                if (
                    not destination_lease.intact()
                    or copied_digest != record["digest"]
                    or not _source_intact(record)
                ):
                    raise OSError("copied backup could not be writer-excluded")
                record.update({
                    "backup_parents": destination_parents,
                    "backup_parent_fd": destination_parent_fd,
                    "backup_fd": destination_fd,
                    "backup_lease": destination_lease,
                    "backup_publication": publication,
                })
                destination_parents = []
                publication = None
                if not _backup_intact(record):
                    raise OSError("copied backup changed before source removal")

                removed = _remove_exact_file_at(
                    record["source_parent_fd"],
                    parts[-1],
                    record["source_fd"],
                    prefix="ql-gap-remove",
                    commit_guard=lambda: (
                        (record["source_lease"] is None
                         or record["source_lease"].intact())
                        and _backup_intact(record)
                    ),
                )
                if not removed:
                    if _source_intact(record):
                        if _remove_exact_file_at(
                            record["backup_parent_fd"],
                            parts[-1],
                            record["backup_fd"],
                            prefix="ql-gap-discard",
                        ):
                            _release_copy_publication(
                                record["backup_publication"])
                            _close_descriptors(record["backup_parents"])
                            record.update({
                                "backup_parents": [],
                                "backup_parent_fd": None,
                                "backup_fd": None,
                                "backup_lease": None,
                                "backup_publication": None,
                            })
                            if expected is None:
                                continue
                    raise OSError("exact source removal could not be reconciled")
                if not _refresh_remaining_hardlink_receipts(record):
                    raise OSError(
                        "a remaining hardlink changed after source removal")
                completed.append(record)
            except BaseException as exc:
                if (
                    publication is not None
                    and destination_parent_fd is not None
                    and publication.matches(
                        destination_parent_fd, record["parts"][-1])
                ):
                    record.update({
                        "backup_parents": destination_parents,
                        "backup_parent_fd": destination_parent_fd,
                        "backup_fd": publication.descriptor,
                        "backup_lease": publication.lease,
                        "backup_publication": publication,
                    })
                    destination_parents = []
                    publication = None
                fatal_change = True
                pending_exception = exc
                log.info(fmt(
                    C.RED,
                    f"  ✗  {record['parts'][-1]} could not be backed up "
                    f"exactly; retained work will be sealed at {bp}."))
                break
            finally:
                if publication is not None:
                    _release_copy_publication(publication)
                _close_descriptors(destination_parents)

        manifest = _tree_manifest(backup_fd)
        backed_up = len(manifest) if manifest is not None else 0
        all_backups_intact = all(_backup_intact(record) for record in completed)
        complete = (
            not fatal_change
            and pending_exception is None
            and len(completed) == requested_count
            and backed_up == requested_count
            and all_backups_intact
        )
        result = _seal_retained(complete=complete) if backed_up else None
        if not backed_up:
            if _scan_backup_tree(backup_fd) == []:
                _remove_exact_tree_at(
                    backup_root_fd,
                    bp.name,
                    backup_fd,
                    prefix="ql-gap-empty",
                )
            if pending_exception is not None and not isinstance(
                    pending_exception,
                    (OSError, TypeError, ValueError, shutil.Error)):
                raise pending_exception
            return None
        if pending_exception is not None:
            if not isinstance(
                    pending_exception,
                    (OSError, TypeError, ValueError, shutil.Error)):
                raise pending_exception
        if not complete:
            log.info(fmt(
                C.RED,
                "  ✗  Gap-fill backup did not complete. Any exact moved "
                f"copies are retained at {bp}; no replacement work may "
                "continue."))
            return result or BackupResult(
                bp,
                complete=False,
                receipt=None,
                requested=requested_count,
                backed_up=backed_up,
            )
        if not completed or result is None:
            return None
        return result
    except BaseException:
        if backup_fd is not None and bp is not None:
            manifest = _tree_manifest(backup_fd)
            if manifest:
                try:
                    _seal_retained(complete=False)
                except BaseException:
                    pass
            elif _scan_backup_tree(backup_fd) == []:
                try:
                    _remove_exact_tree_at(
                        backup_root_fd,
                        bp.name,
                        backup_fd,
                        prefix="ql-gap-empty",
                    )
                except BaseException:
                    pass
        raise
    finally:
        for record in reversed(records):
            backup_publication = record.get("backup_publication")
            backup_lease = record.get("backup_lease")
            if backup_publication is not None:
                _release_copy_publication(backup_publication)
            elif (
                backup_lease is not None
                and backup_lease is not record.get("source_lease")
            ):
                backup_lease.close()
            backup_descriptor = record.get("backup_fd")
            if (
                backup_publication is None
                and
                backup_descriptor is not None
                and backup_descriptor != record.get("source_fd")
            ):
                os.close(backup_descriptor)
            _close_descriptors(record.get("backup_parents", ()))
            source_lease = record.get("source_lease")
            if source_lease is not None:
                source_lease.close()
            source_descriptor = record.get("source_fd")
            if source_descriptor is not None:
                os.close(source_descriptor)
            _close_descriptors(record.get("source_parents", ()))
        if backup_fd is not None:
            os.close(backup_fd)
        if backup_root_fd is not None:
            os.close(backup_root_fd)
        _close_descriptors(album_fds)


def list_undo_copies():
    """Deliberate undo copies (downsample originals) still inside their
    retention window, as (backup_path, origin). Shown on the diagnostics list
    with a Restore button — separate from the orphaned-backup alarm, because
    this state is one the user asked for, not a failure."""
    out = []
    if not cfg.UPGRADE_BACKUP_DIR.exists():
        return out
    try:
        for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
            if (entry.is_dir()
                    and (entry / _REAP_AFTER_RETENTION_SENTINEL).is_file()):
                out.append((entry, _read_backup_origin(entry)))
    except OSError:
        pass
    return out


def stash_downsample_originals(files, album_dir, *, include_identity_receipts=False):
    """Copy the hi-res originals about to be rewritten in place into a
    timestamped backup, so the downsample can be undone until the retention
    sweep clears it. Returns
    ``(backup_result_or_None, set_of_files_copied)``.

    With ``include_identity_receipts=True``, returns a third mapping from each
    copied source path to the exact regular-file identity, size, modification
    and change times, and digest verified across its durable copy. The caller
    can bind that receipt immediately before rewriting and refuse a replaced or
    subsequently changed source. The default keeps the existing two-value API.

    Copies, never moves — the originals stay put for the rewrite itself. The
    caller must leave any file NOT in the returned set untouched: the whole
    point of keep-originals is that nothing is rewritten without its copy, so
    a failed copy downgrades that file to "skipped", never to "unprotected".
    Each copy is content-verified (sha256) before it counts, because the undo
    path later trusts these bytes with an overwrite."""
    def _result(path, copied, receipts):
        if include_identity_receipts:
            return path, copied, receipts
        return path, copied

    files = [Path(f) for f in files]
    if not files:
        return _result(None, set(), {})
    opened = _open_backup_source(Path(album_dir))
    if opened is None:
        log.info(fmt(
            C.YELLOW,
            "  ⚠  Couldn't bind the album's real no-follow path; leaving "
            "every file untouched."))
        return _result(None, set(), {})
    public, music_root, album_parts, album_fds = opened
    album_fd = album_fds[-1]
    backup_root_fd = None
    backup_fd = None
    copied = set()
    receipts = {}
    retained_unbound_copy = False
    held_copies = []

    def _held_copies_match_receipt(result):
        if result is None or result.receipt is None:
            return False
        try:
            expected_files = result.receipt["tree"]["files"]
            for item in held_copies:
                relative_text = "/".join(item["relative"])
                expected = expected_files.get(relative_text)
                if (
                    expected is None
                    or not item["lease"].intact()
                    or not _relative_directories_are_named(
                        backup_fd,
                        item["relative"][:-1],
                        item["parents"],
                    )
                    or not _named_entry_matches(
                        item["parent_fd"],
                        item["relative"][-1],
                        item["descriptor"],
                    )
                    or _snapshot_file(
                        item["parent_fd"], item["relative"][-1]
                    ) != expected
                ):
                    return False
            return _read_backup_receipt(backup_fd) == result.receipt
        except (OSError, TypeError, ValueError):
            return False

    try:
        try:
            cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup_root = Path(os.path.abspath(
                os.fspath(cfg.UPGRADE_BACKUP_DIR)))
            backup_root_fd = _open_backup_directory(backup_root)
            if not _backup_root_is_public(backup_root, backup_root_fd):
                raise OSError("backup directory changed while it was opened")
            bp = backup_root / _backup_dir_name(public, kind="downsample")
            os.mkdir(bp.name, dir_fd=backup_root_fd)
            backup_fd = _open_backup_directory(
                bp.name, dir_fd=backup_root_fd)
            if (
                not _named_directory_matches(
                    backup_root_fd, bp.name, backup_fd)
                or not _fsync_directory_fds(backup_root_fd)
                or not _write_backup_origin_durable(backup_fd, public)
            ):
                raise OSError("keep-originals directory could not be committed")
        except OSError as exc:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Couldn't create the keep-originals dir ({exc}); "
                "leaving every file untouched."))
            if (
                backup_fd is not None
                and _scan_backup_tree(backup_fd) == []
            ):
                _remove_exact_tree_at(
                    backup_root_fd,
                    bp.name,
                    backup_fd,
                    prefix="ql-downsample-empty",
                )
            return _result(None, set(), {})

        for source_path in files:
            rooted = _rooted_path_parts(public, source_path)
            if rooted is None or rooted[2][-1] in _SIDECARS:
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  Couldn't safely bind {source_path.name}; it "
                    "will be left untouched."))
                continue
            relative = rooted[2]
            source_parents = []
            destination_parents = []
            source_fd = None
            publication = None
            destination_parent_fd = None
            try:
                source_parents = _open_relative_directories(
                    album_fd, relative[:-1], create=False)
                source_parent_fd = (
                    source_parents[-1] if source_parents else album_fd)
                source_fd = _open_regular_file_at(
                    source_parent_fd, relative[-1])
                source_digest = _file_digest_fd(source_fd)
                source_receipt = _regular_file_receipt(
                    source_fd, source_digest)

                destination_parents = _open_relative_directories(
                    backup_fd, relative[:-1], create=True)
                destination_parent_fd = (
                    destination_parents[-1]
                    if destination_parents else backup_fd)
                publication = _CopyPublication(
                    destination_parent_fd, relative[-1])
                copied_digest = _copy_file_noreplace_at(
                    source_fd, publication)
                destination_fd = publication.descriptor
                destination_lease = publication.lease
                exact_source = (
                    destination_lease.intact()
                    and copied_digest == source_digest
                    and _regular_file_receipt(
                        source_fd, _file_digest_fd(source_fd))
                    == source_receipt
                    and _named_entry_matches(
                        source_parent_fd, relative[-1], source_fd)
                    and _backup_source_is_public(
                        music_root, album_parts, album_fds)
                    and _relative_directories_are_named(
                        album_fd, relative[:-1], source_parents)
                    and _named_directory_matches(
                        backup_root_fd, bp.name, backup_fd)
                    and _relative_directories_are_named(
                        backup_fd, relative[:-1], destination_parents)
                    and _named_entry_matches(
                        destination_parent_fd,
                        relative[-1],
                        destination_fd,
                    )
                    and _file_digest_fd(destination_fd) == source_digest
                    and _fsync(_held_directory_path(destination_fd))
                    and _backup_root_is_public(
                        backup_root, backup_root_fd)
                )
                if not exact_source:
                    source_still_named = _named_entry_matches(
                        source_parent_fd, relative[-1], source_fd)
                    removed_copy = False
                    if source_still_named and destination_lease.intact():
                        removed_copy = _remove_exact_file_at(
                            destination_parent_fd,
                            relative[-1],
                            destination_fd,
                            prefix="ql-downsample-discard",
                        )
                    if (
                        not removed_copy
                        and _named_entry_matches(
                            destination_parent_fd,
                            relative[-1],
                            destination_fd,
                        )
                    ):
                        retained_unbound_copy = True
                    raise OSError("source changed while its copy was made")
                copied.add(source_path)
                receipts[source_path] = source_receipt
                held_copies.append({
                    "relative": relative,
                    "parents": destination_parents,
                    "parent_fd": destination_parent_fd,
                    "descriptor": destination_fd,
                    "lease": destination_lease,
                    "publication": publication,
                })
                destination_parents = []
                publication = None
            except BaseException as exc:
                if (
                    publication is not None
                    and destination_parent_fd is not None
                    and publication.matches(
                        destination_parent_fd, relative[-1])
                ):
                    held_copies.append({
                        "relative": relative,
                        "parents": destination_parents,
                        "parent_fd": destination_parent_fd,
                        "descriptor": publication.descriptor,
                        "lease": publication.lease,
                        "publication": publication,
                    })
                    destination_parents = []
                    publication = None
                    retained_unbound_copy = True
                elif publication is not None:
                    try:
                        reconciled_private = (
                            publication.reconcile_private_cleanup())
                    except BaseException:
                        reconciled_private = False
                    if not reconciled_private:
                        retained_unbound_copy = True
                if (
                    destination_parent_fd is not None
                    and not _named_entry_missing(
                        destination_parent_fd, relative[-1])
                ):
                    retained_unbound_copy = True
                if not isinstance(exc, (OSError, shutil.Error)):
                    raise
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  Couldn't keep a copy of {source_path.name} "
                    f"({exc}); it will be left untouched."))
            finally:
                if publication is not None:
                    _release_copy_publication(publication)
                if source_fd is not None:
                    os.close(source_fd)
                _close_descriptors(destination_parents)
                _close_descriptors(source_parents)

        if not copied and not retained_unbound_copy:
            if _scan_backup_tree(backup_fd) == []:
                _remove_exact_tree_at(
                    backup_root_fd,
                    bp.name,
                    backup_fd,
                    prefix="ql-downsample-empty",
                )
            return _result(None, set(), {})
        if (
            not retained_unbound_copy
            and _named_entry_missing(
                backup_fd, _REAP_AFTER_RETENTION_SENTINEL)
        ):
            marked = _write_text_noreplace_at(
                backup_fd,
                _REAP_AFTER_RETENTION_SENTINEL,
                "downsample originals — an undo copy; the age sweep clears it "
                "after the retention window",
            )
            if not marked:
                # Without the marker this safely degrades to always-keep.
                vlog("couldn't mark the undo copy age-reapable")
        retained_manifest = _tree_manifest(backup_fd)
        if retained_unbound_copy:
            retained_count = len(retained_manifest or ())
            result = _seal_backup_result(
                bp,
                backup_fd,
                public,
                kind="downsample",
                complete=False,
                requested=len(files),
                backed_up=min(len(files), retained_count),
            )
            log.info(fmt(
                C.YELLOW,
                "  ⚠  A source changed while its kept copy was made. "
                f"The recovery copy remains at {bp}; no source will be "
                "rewritten or age-reaped automatically."))
            return _result(result, set(), {})
        result = _seal_backup_result(
            bp,
            backup_fd,
            public,
            kind="downsample",
            complete=True,
            requested=len(copied),
            backed_up=len(copied),
        )
        if not result.complete:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  The kept originals could not be sealed to an exact "
                f"ownership receipt. They remain at {bp}, but no source "
                "will be rewritten."))
            return _result(result, set(), {})
        if not _held_copies_match_receipt(result):
            log.info(fmt(
                C.YELLOW,
                "  ⚠  A kept-original copy changed before its exact receipt "
                f"was bound. Recovery data remains at {bp}, but no source "
                "will be rewritten."))
            return _result(result, set(), {})
        return _result(result, copied, receipts)
    except BaseException:
        # Cancellation after a copy publication must not leave an unreceipted
        # recovery directory.  Seal whatever exact tree remains as incomplete;
        # the original exception is still propagated after this best effort.
        if backup_fd is not None:
            try:
                retained_manifest = _tree_manifest(backup_fd)
                if retained_manifest:
                    _seal_backup_result(
                        bp,
                        backup_fd,
                        public,
                        kind="downsample",
                        complete=False,
                        requested=len(files),
                        backed_up=min(len(files), len(retained_manifest)),
                    )
            except BaseException:
                pass
        raise
    finally:
        for item in reversed(held_copies):
            _release_copy_publication(item["publication"])
            _close_descriptors(item["parents"])
        if backup_fd is not None:
            os.close(backup_fd)
        if backup_root_fd is not None:
            os.close(backup_root_fd)
        _close_descriptors(album_fds)


def restore_gap_fill_backup(backup, album_dir: Path,
                            *, keep_larger_dst: bool = True,
                            expected_owner=None) -> int:
    """Move every file in a carried gap-fill or downsample ``BackupResult``
    back under album_dir, preserving relative structure. Returns the number of
    files restored. Removes the exact backup dir on completion. Invalid,
    missing or raw-path backup arguments safely return 0.

    keep_larger_dst (default True, the repair caller): when a file already at
    the destination is >= the backup copy in bytes, keep it and discard the
    backup — valid for repair, where the backup is the truncated original and
    a larger dst is the good refill. Gap-fill callers pass False: there the
    backup IS the good original, so a larger-but-corrupt partial re-rip at dst
    must NOT win — always restore the backup.

    Crash-safe across filesystems: each file is copied into a private random
    workspace on the destination filesystem, then published without overwrite
    after any exact existing destination is held aside. The backup is removed
    only after the published inode and both no-follow directory chains still
    match. Public workspace-name collisions and replacements are left alone."""
    if not _backup_owner_authorized(backup, expected_owner):
        return 0
    backup_opened = _validated_backup_result(
        backup,
        origin=album_dir,
        kinds={"gap-fill", "downsample"},
    )
    if backup_opened is None:
        return 0
    backup_public, backup_root, backup_parts, backup_fds = backup_opened
    backup_fd = backup_fds[-1]
    album_opened = _open_rooted_directory(
        cfg.MUSIC_ROOT, Path(album_dir), create=True)
    if album_opened is None:
        _close_descriptors(backup_fds)
        log.info(fmt(C.RED,
            f"  ✗  Couldn't bind the real album path for restore.\n"
            f"     Backed-up tracks remain at: {backup_public}"))
        return 0
    album_public, music_root, album_parts, album_fds = album_opened
    album_fd = album_fds[-1]
    n_restored = 0
    n_failed = 0
    held_backup = None
    held_destinations = []

    def _destinations_intact():
        try:
            if not _backup_source_is_public(
                    music_root, album_parts, album_fds):
                return False
            for item in held_destinations:
                if (
                    not item["lease"].intact()
                    or not _relative_directories_are_named(
                        album_fd, item["relative"][:-1], item["parents"])
                    or not _named_entry_matches(
                        item["parent_fd"],
                        item["relative"][-1],
                        item["descriptor"],
                    )
                    or os.fstat(item["descriptor"]).st_size
                        < item["minimum_size"]
                    or item["digest"] is not None
                    and _file_digest_fd(item["descriptor"])
                        != item["digest"]
                ):
                    return False
            return True
        except (OSError, TypeError, ValueError):
            return False

    try:
        backup_snapshot = _receipt_disposal_snapshot(
            backup_fd, backup.receipt)
        if (
            backup_snapshot is None
            or _exact_tree_snapshot(backup_fd) != backup_snapshot
        ):
            log.info(fmt(C.RED + C.BOLD,
                f"  ✗  Couldn't fully read the backup to restore it. "
                f"Originals are PRESERVED at:\n     {backup_public}"))
            return 0
        held_backup = {}
        _hold_snapshot_files(backup_fd, backup_snapshot, held_backup)
        manifest = {
            tuple(PurePosixPath(relative).parts): (
                expected["size"], expected["sha256"])
            for relative, expected in backup.receipt["tree"]["files"].items()
            if PurePosixPath(relative).name not in _SIDECARS
        }

        for relative, (source_size, source_digest) in manifest.items():
            source_parents = []
            destination_parents = []
            source_fd = None
            existing_fd = None
            restored_fd = None
            workspace_fd = None
            workspace_name = None
            previous_moved = False
            published = False
            publication_verified = False
            publication = None
            existing_lease = None
            try:
                source_parents, source_parent_fd, source_fd = _open_tree_file(
                    backup_fd, relative)
                if (
                    os.fstat(source_fd).st_size != source_size
                    or _file_digest_fd(source_fd) != source_digest
                ):
                    raise OSError("backup file changed before restore")
                destination_parents = _open_relative_directories(
                    album_fd, relative[:-1], create=True)
                destination_parent_fd = (
                    destination_parents[-1]
                    if destination_parents else album_fd)
                try:
                    existing_fd = _open_regular_file_at(
                        destination_parent_fd, relative[-1])
                except FileNotFoundError:
                    existing_fd = None

                if (
                    keep_larger_dst
                    and existing_fd is not None
                    and os.fstat(existing_fd).st_size >= source_size
                ):
                    existing_lease = acquire_inode_write_exclusion(
                        existing_fd)
                    if (
                        existing_lease is None
                        or not existing_lease.intact()
                    ):
                        raise OSError(
                            "kept destination could not be writer-excluded")
                    if flac_audio_ok(
                            _held_directory_path(existing_fd)) is True:
                        if not (
                            _fsync(_held_directory_path(existing_fd))
                            and _fsync(_held_directory_path(
                                destination_parent_fd))
                            and _fsync_directory_fds(destination_parent_fd)
                        ):
                            raise OSError(
                                "kept destination could not be flushed")
                        held_destinations.append({
                            "relative": relative,
                            "parents": destination_parents,
                            "parent_fd": destination_parent_fd,
                            "descriptor": existing_fd,
                            "lease": existing_lease,
                            "minimum_size": source_size,
                            "digest": None,
                        })
                        destination_parents = []
                        existing_fd = None
                        existing_lease = None
                        n_restored += 1
                        continue

                workspace_name, workspace_fd = _reserve_backup_quarantine(
                    destination_parent_fd, "ql-restore-file")
                publication = _CopyPublication(workspace_fd, "restored")
                copied_digest = _copy_file_noreplace_at(
                    source_fd, publication)
                restored_fd = publication.descriptor
                destination_lease = publication.lease
                if copied_digest != source_digest:
                    raise OSError("restore copy did not match backup")
                if not destination_lease.intact():
                    raise OSError("restored copy could not be writer-excluded")

                if existing_fd is not None:
                    hold_exception = _rename_exact_noreplace_at(
                        destination_parent_fd,
                        relative[-1],
                        workspace_fd,
                        "previous",
                        existing_fd,
                    )
                    previous_moved = True
                    if hold_exception is not None:
                        if _restore_exact_entry_move(
                                workspace_fd,
                                "previous",
                                destination_parent_fd,
                                relative[-1],
                                existing_fd):
                            previous_moved = False
                        raise hold_exception
                    if (
                        not _named_entry_missing(
                            destination_parent_fd, relative[-1])
                        or not _named_entry_matches(
                            workspace_fd, "previous", existing_fd)
                        or not _fsync_directory_fds(
                            destination_parent_fd, workspace_fd)
                    ):
                        raise OSError("existing destination changed during restore")
                elif not _named_entry_missing(
                        destination_parent_fd, relative[-1]):
                    raise OSError("destination appeared during restore")

                publication.add_location(
                    destination_parent_fd, relative[-1])
                publish_exception = _rename_exact_noreplace_at(
                    workspace_fd,
                    "restored",
                    destination_parent_fd,
                    relative[-1],
                    restored_fd,
                )
                published = True
                if publish_exception is not None:
                    if _restore_exact_entry_move(
                            destination_parent_fd,
                            relative[-1],
                            workspace_fd,
                            "restored",
                            restored_fd):
                        published = False
                    if not published and previous_moved:
                        if _restore_exact_entry_move(
                                workspace_fd,
                                "previous",
                                destination_parent_fd,
                                relative[-1],
                                existing_fd):
                            previous_moved = False
                    raise publish_exception
                if not (
                    _named_entry_matches(
                        destination_parent_fd, relative[-1], restored_fd)
                    and _file_digest_fd(restored_fd) == source_digest
                    and destination_lease.intact()
                    and _named_entry_matches(
                        source_parent_fd, relative[-1], source_fd)
                    and _file_digest_fd(source_fd) == source_digest
                    and _backup_source_is_public(
                        music_root, album_parts, album_fds)
                    and _relative_directories_are_named(
                        album_fd, relative[:-1], destination_parents)
                    and _backup_source_is_public(
                        backup_root, backup_parts, backup_fds)
                    and _relative_directories_are_named(
                        backup_fd, relative[:-1], source_parents)
                    and _fsync_directory_fds(
                        destination_parent_fd, workspace_fd)
                    and _fsync(_held_directory_path(
                        destination_parent_fd))
                ):
                    raise OSError("restored destination could not be verified")
                publication_verified = True
                held_destinations.append({
                    "relative": relative,
                    "parents": destination_parents,
                    "parent_fd": destination_parent_fd,
                    "descriptor": restored_fd,
                    "lease": destination_lease,
                    "minimum_size": source_size,
                    "digest": source_digest,
                    "publication": publication,
                })
                destination_parents = []
                restored_fd = None
                publication = None
                n_restored += 1

                if (
                    previous_moved
                    and _named_entry_matches(
                        workspace_fd, "previous", existing_fd)
                ):
                    try:
                        unlink_exception = _unlink_exact_at(
                            workspace_fd, "previous", existing_fd)
                        previous_moved = False
                        if unlink_exception is not None:
                            raise unlink_exception
                    except OSError as exc:
                        log.info(fmt(
                            C.YELLOW,
                            "  ⚠  Restored the backup, but couldn't "
                            f"remove its private rollback copy: {exc}."))
            except BaseException as exc:
                if publication is not None:
                    restored_fd = publication.descriptor
                    destination_lease = publication.lease
                    if publication.matches(
                            destination_parent_fd, relative[-1]):
                        published = True
                    elif publication.matches(workspace_fd, "restored"):
                        published = False
                if (
                    published
                    and not publication_verified
                    and restored_fd is not None
                    and _named_entry_matches(
                        destination_parent_fd, relative[-1], restored_fd)
                    and _restore_exact_entry_move(
                        destination_parent_fd,
                        relative[-1],
                        workspace_fd,
                        "restored",
                        restored_fd,
                    )
                ):
                    published = False
                if not publication_verified and not published and previous_moved:
                    if _restore_exact_entry_move(
                            workspace_fd,
                            "previous",
                            destination_parent_fd,
                            relative[-1],
                            existing_fd):
                        previous_moved = False
                if not isinstance(exc, (OSError, shutil.Error)):
                    raise
                n_failed += 1
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  Couldn't restore {relative[-1]}: {exc}."))
            finally:
                if existing_lease is not None:
                    existing_lease.close()
                if (
                    publication is not None
                    and workspace_fd is not None
                    and publication.matches(workspace_fd, "restored")
                ):
                    try:
                        _remove_exact_file_at(
                            workspace_fd,
                            "restored",
                            publication.descriptor,
                            prefix="ql-restore-copy-cleanup",
                        )
                    except BaseException:
                        pass
                if publication is not None:
                    _release_copy_publication(publication)
                if existing_fd is not None:
                    os.close(existing_fd)
                if source_fd is not None:
                    os.close(source_fd)
                if workspace_fd is not None:
                    try:
                        with os.scandir(workspace_fd) as iterator:
                            empty = next(iterator, None) is None
                        if (
                            empty
                            and _named_directory_matches(
                                destination_parent_fd,
                                workspace_name,
                                workspace_fd,
                            )
                        ):
                            _rmdir_exact_at(
                                destination_parent_fd,
                                workspace_name,
                                workspace_fd,
                            )
                    except BaseException:
                        pass
                    os.close(workspace_fd)
                _close_descriptors(destination_parents)
                _close_descriptors(source_parents)

        if (
            n_failed
            or n_restored != len(manifest)
            or not _destinations_intact()
            or not _held_snapshot_files_intact(
                backup_fd, backup_snapshot, held_backup)
        ):
            _write_receipt_marker(
                backup,
                _PARTIAL_RESTORE_SENTINEL,
                "partial restore — un-restored originals are the only copy",
            )
            log.info(fmt(C.RED + C.BOLD,
                f"  ✗  Some tracks could NOT be restored. Originals are "
                f"PRESERVED at:\n     {backup_public}"))
        else:
            removed = _remove_exact_tree_at(
                backup_fds[-2],
                backup_parts[-1],
                backup_fd,
                prefix="ql-restored-backup",
                expected_snapshot=backup_snapshot,
                held_files=held_backup,
                commit_guard=lambda: (
                    _destinations_intact()
                    and _backup_source_is_public(
                        backup_root,
                        backup_parts,
                        backup_fds,
                        include_album=False,
                    )
                ),
            )
            if not removed:
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Restored files are safe, but their exact backup "
                    f"remains at {backup_public}."))
        return n_restored
    finally:
        for item in reversed(held_destinations):
            publication = item.get("publication")
            if publication is not None:
                _release_copy_publication(publication)
            else:
                item["lease"].close()
                os.close(item["descriptor"])
            _close_descriptors(item["parents"])
        if held_backup is not None:
            _release_held_snapshot_files(held_backup)
        _close_descriptors(album_fds)
        _close_descriptors(backup_fds)


def _serializable_fidelity_fd(descriptor):
    value = _fidelity_fd(descriptor, include_times=False)
    value["xattrs"] = {
        name: payload.hex() for name, payload in value["xattrs"].items()
    }
    current = os.fstat(descriptor)
    value.update({
        "size": int(current.st_size),
        "mtime_ns": int(current.st_mtime_ns),
        "sha256": _file_digest_fd(descriptor),
        "links": int(current.st_nlink),
    })
    return value


def _file_matches_source_fidelity(descriptor, expected) -> bool:
    try:
        return _fidelity_without(
            _serializable_fidelity_fd(descriptor), "links"
        ) == _fidelity_without(expected, "links")
    except (OSError, TypeError, ValueError):
        return False


def _apply_serialized_directory_fidelity(descriptor, expected, *,
                                          restore_mtime=False) -> bool:
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode):
            return False
        if (int(current.st_uid), int(current.st_gid)) != (
                expected["uid"], expected["gid"]):
            os.fchown(descriptor, expected["uid"], expected["gid"])
        os.fchmod(descriptor, expected["mode"])
        expected_xattrs = {
            name: bytes.fromhex(payload)
            for name, payload in expected["xattrs"].items()
        }
        current_xattrs = _xattrs_fd(descriptor)
        for name in current_xattrs.keys() - expected_xattrs.keys():
            os.removexattr(descriptor, name)
        for name, payload in expected_xattrs.items():
            os.setxattr(descriptor, name, payload)
        if restore_mtime:
            os.utime(
                descriptor,
                ns=(int(os.fstat(descriptor).st_atime_ns),
                    int(expected["mtime_ns"])),
            )
        current_fidelity = _fidelity_fd(descriptor, include_times=False)
        current_fidelity["xattrs"] = {
            name: payload.hex()
            for name, payload in current_fidelity["xattrs"].items()
        }
        return (
            current_fidelity == _fidelity_without(expected, "mtime_ns")
            and (not restore_mtime
                 or int(os.fstat(descriptor).st_mtime_ns)
                    == int(expected["mtime_ns"]))
        )
    except (OSError, TypeError, ValueError, KeyError):
        return False


def restore_incomplete_upgrade_backup(
        backup,
        original_path: Path,
        *,
        expected_owner=None,
) -> IncompleteUpgradeRestoreOutcome | None:
    """Reconcile one receipt-bound incomplete whole-album backup.

    This deliberately refuses complete upgrades and every file-subset kind.
    The original ancestor chain and, when present, album root must be the same
    statx incarnations sealed before backup. Existing entries are never
    overwritten: exact originals count as already present and every conflict is
    reported unresolved. The backup is disposed only after every original file
    is writer-held, content/fidelity checked, and durably named at the origin.
    """
    if not _backup_owner_authorized(backup, expected_owner):
        return None
    backup_opened = _validated_backup_result(
        backup, origin=original_path, kinds={"upgrade"})
    if backup_opened is None or backup.complete:
        if backup_opened is not None:
            _close_descriptors(backup_opened[-1])
        return None
    backup_public, backup_root, backup_parts, backup_fds = backup_opened
    backup_fd = backup_fds[-1]
    source_receipt = backup.receipt.get("source_receipt")
    rooted = _rooted_path_parts(cfg.MUSIC_ROOT, Path(original_path))
    try:
        expected_origin = (
            os.fspath(rooted[0]) if rooted is not None else None)
    except (OSError, TypeError, ValueError):
        expected_origin = None
    if (
        rooted is None
        or not _source_receipt_shape_is_valid(
            source_receipt, expected_origin)
    ):
        _close_descriptors(backup_fds)
        return None
    original_public, music_root, source_parts = rooted
    state = _read_upgrade_recovery_state(backup_fd, backup.receipt)
    backup_snapshot = _receipt_disposal_snapshot(
        backup_fd, backup.receipt)
    if state is None or backup_snapshot is None:
        _close_descriptors(backup_fds)
        return None

    expected_files = source_receipt["tree"]["files"]
    expected_directories = source_receipt["tree"]["directories"]
    backup_payload = {
        relative: value
        for relative, value in backup.receipt["tree"]["files"].items()
        if PurePosixPath(relative).name not in _SIDECARS
    }
    total = len(expected_files)
    refused = IncompleteUpgradeRestoreOutcome(0, 0, total, False)
    if (
        set(backup_payload) != set(expected_files)
        or set(backup.receipt["tree"]["directories"])
            != set(expected_directories)
        or _exact_tree_snapshot(backup_fd) != backup_snapshot
    ):
        _close_descriptors(backup_fds)
        return None

    parent_opened = _open_rooted_directory(
        music_root,
        original_public.parent,
        allow_root=True,
    )
    if parent_opened is None:
        _close_descriptors(backup_fds)
        return refused
    _parent_public, _music_root, _parent_parts, parent_fds = parent_opened
    original_parent_fd = parent_fds[-1]
    original_name = source_parts[-1]
    if not _source_parent_is_bound(
            music_root, source_parts, parent_fds, source_receipt):
        _close_descriptors(parent_fds)
        _close_descriptors(backup_fds)
        return refused

    held_backup = None
    album_fd = None
    hidden_fd = None
    hidden_name = None
    stage_fd = None
    stage_name = None
    stage_published = False
    file_workspace_fd = None
    file_workspace_name = None
    destination_is_original_root = False
    held_destinations = {}
    created_directories = set()
    restored = 0
    already_present = 0

    def _public_album_is_bound():
        return (
            album_fd is not None
            and _named_directory_matches(
                original_parent_fd, original_name, album_fd)
            and _source_parent_is_bound(
                music_root, source_parts, parent_fds, source_receipt)
            and (
                not destination_is_original_root
                or _source_root_is_bound(album_fd, source_receipt)
            )
        )

    def _destination_file_is_intact(relative_text, item):
        expected = source_receipt["fidelity"]["files"][relative_text]
        return (
            item["lease"].intact()
            and _relative_directories_are_named(
                album_fd, item["relative"][:-1], item["parents"])
            and _named_entry_matches(
                item["parent_fd"],
                item["relative"][-1],
                item["descriptor"],
            )
            and _file_matches_source_fidelity(
                item["descriptor"], expected)
        )

    def _destinations_intact():
        try:
            return (
                _public_album_is_bound()
                and set(held_destinations) == set(expected_files)
                and all(
                    _destination_file_is_intact(relative, item)
                    for relative, item in held_destinations.items()
                )
            )
        except (OSError, TypeError, ValueError):
            return False

    try:
        held_backup = {}
        _hold_snapshot_files(backup_fd, backup_snapshot, held_backup)
        for relative_text, expected in expected_files.items():
            source = held_backup.get(relative_text)
            if (
                source is None
                or not source["lease"].intact()
                or backup_payload[relative_text].get("size")
                    != expected.get("size")
                or backup_payload[relative_text].get("sha256")
                    != expected.get("sha256")
                or not _file_matches_source_fidelity(
                    source["descriptor"],
                    source_receipt["fidelity"]["files"][relative_text],
                )
            ):
                return None

        try:
            candidate_fd = _open_backup_directory(
                original_name, dir_fd=original_parent_fd)
        except FileNotFoundError:
            candidate_fd = None
        except OSError:
            return refused
        if candidate_fd is not None:
            if not _source_root_is_bound(candidate_fd, source_receipt):
                os.close(candidate_fd)
                return refused
            album_fd = candidate_fd
            destination_is_original_root = True
        elif not _named_entry_missing(original_parent_fd, original_name):
            return refused

        if album_fd is None and state["location"] == "hidden":
            hidden_name = state["quarantine_name"]
            try:
                hidden_fd = _open_backup_directory(
                    hidden_name, dir_fd=original_parent_fd)
                if (
                    not _named_directory_matches(
                        original_parent_fd, hidden_name, hidden_fd)
                    or _path_directory_generations([hidden_fd])[-1]
                        != state["quarantine_generation"]
                ):
                    return refused
                album_fd = _open_backup_directory("held", dir_fd=hidden_fd)
            except OSError:
                return refused
            if (
                not _named_directory_matches(hidden_fd, "held", album_fd)
                or not _source_root_is_bound(album_fd, source_receipt)
                or not _named_entry_missing(
                    original_parent_fd, original_name)
            ):
                return refused
            move_exception = _rename_exact_noreplace_at(
                hidden_fd,
                "held",
                original_parent_fd,
                original_name,
                album_fd,
            )
            if not (
                _named_entry_missing(hidden_fd, "held")
                and _named_directory_matches(
                    original_parent_fd, original_name, album_fd)
                and _source_parent_is_bound(
                    music_root, source_parts, parent_fds, source_receipt)
                and _fsync_directory_fds(
                    hidden_fd, original_parent_fd)
            ):
                return refused
            destination_is_original_root = True
            # A deferred post-rename exception is reconciled by the exact
            # namespace checks above. The complete backup remains held until
            # the restored public files pass their own durability gate.
            del move_exception
            try:
                if _named_directory_matches(
                        original_parent_fd, hidden_name, hidden_fd):
                    deferred = _rmdir_exact_at(
                        original_parent_fd, hidden_name, hidden_fd)
                    if deferred is None:
                        hidden_name = None
            except OSError:
                pass

        if album_fd is None and state["location"] == "public":
            return refused
        if album_fd is None:
            if state["location"] != "absent":
                return refused
            stage_name, stage_fd = _reserve_backup_quarantine(
                original_parent_fd, "ql-incomplete-upgrade-restore")
            album_fd = stage_fd
            if not _apply_serialized_directory_fidelity(
                    album_fd,
                    source_receipt["fidelity"]["directories"][""],
            ):
                return refused
            created_directories.add("")

        blocked_directories = set()
        for relative_text in sorted(
                expected_directories,
                key=lambda value: (value.count("/"), value)):
            relative = tuple(PurePosixPath(relative_text).parts)
            descriptors = []
            created = False
            try:
                try:
                    descriptors = _open_relative_directories(
                        album_fd, relative, create=False)
                except FileNotFoundError:
                    descriptors = _open_relative_directories(
                        album_fd, relative, create=True)
                    created = True
                if created:
                    created_directories.add(relative_text)
                    if not _apply_serialized_directory_fidelity(
                            descriptors[-1],
                            source_receipt["fidelity"]["directories"][
                                relative_text],
                    ):
                        blocked_directories.add(relative_text)
            except OSError:
                blocked_directories.add(relative_text)
            finally:
                _close_descriptors(descriptors)

        for relative_text in sorted(expected_files):
            relative = tuple(PurePosixPath(relative_text).parts)
            if any(
                    relative_text == blocked
                    or relative_text.startswith(blocked + "/")
                    for blocked in blocked_directories):
                continue
            parents = []
            destination_fd = None
            lease = None
            publication = None
            copied = False
            try:
                parents = _open_relative_directories(
                    album_fd, relative[:-1], create=False)
                parent_fd = parents[-1] if parents else album_fd
                try:
                    destination_fd = _open_regular_file_at(
                        parent_fd, relative[-1])
                except FileNotFoundError:
                    if not _named_entry_missing(parent_fd, relative[-1]):
                        continue
                    copy_parent_fd = parent_fd
                    copy_name = relative[-1]
                    if destination_is_original_root:
                        if file_workspace_fd is None:
                            (file_workspace_name,
                             file_workspace_fd) = _reserve_backup_quarantine(
                                original_parent_fd,
                                "ql-incomplete-file-copy",
                            )
                        copy_parent_fd = file_workspace_fd
                        copy_name = f"restored-{len(held_destinations):04d}"
                    publication = _CopyPublication(copy_parent_fd, copy_name)
                    copied_digest = _copy_file_noreplace_at(
                        held_backup[relative_text]["descriptor"],
                        publication,
                    )
                    destination_fd = publication.descriptor
                    lease = publication.lease
                    if destination_is_original_root:
                        publication.add_location(parent_fd, relative[-1])
                        try:
                            deferred = _rename_exact_noreplace_at(
                                file_workspace_fd,
                                copy_name,
                                parent_fd,
                                relative[-1],
                                destination_fd,
                            )
                        except BaseException:
                            if publication.matches(
                                    parent_fd, relative[-1]):
                                _write_receipt_marker(
                                    backup,
                                    _PARTIAL_RESTORE_SENTINEL,
                                    "incomplete upgrade restore was "
                                    "interrupted after a file publication",
                                )
                            raise
                        if not (
                            _named_entry_missing(
                                file_workspace_fd, copy_name)
                            and _named_entry_matches(
                                parent_fd, relative[-1], destination_fd)
                            and _fsync_directory_fds(
                                file_workspace_fd, parent_fd)
                        ):
                            raise OSError(
                                "restored original publication was uncertain")
                        if deferred is not None:
                            if not isinstance(
                                    deferred,
                                    (OSError, TypeError, ValueError)):
                                _write_receipt_marker(
                                    backup,
                                    _PARTIAL_RESTORE_SENTINEL,
                                    "incomplete upgrade restore was "
                                    "interrupted after a file publication",
                                )
                                raise deferred
                    if copied_digest != expected_files[
                            relative_text]["sha256"]:
                        raise OSError("restored original digest changed")
                    copied = True
                if not copied:
                    lease = acquire_inode_write_exclusion(destination_fd)
                if (
                    lease is None
                    or not lease.intact()
                    or not _named_entry_matches(
                        parent_fd, relative[-1], destination_fd)
                    or not _file_matches_source_fidelity(
                        destination_fd,
                        source_receipt["fidelity"]["files"][relative_text],
                    )
                    or not held_backup[relative_text]["lease"].intact()
                    or not _public_album_is_bound()
                    and stage_fd is None
                ):
                    continue
                held_destinations[relative_text] = {
                    "relative": relative,
                    "parents": parents,
                    "parent_fd": parent_fd,
                    "descriptor": destination_fd,
                    "lease": lease,
                    "publication": publication,
                }
                parents = []
                destination_fd = None
                lease = None
                publication = None
                if copied:
                    restored += 1
                else:
                    already_present += 1
            except OSError:
                continue
            finally:
                if publication is not None:
                    _release_copy_publication(publication)
                else:
                    if lease is not None:
                        lease.close()
                    if destination_fd is not None:
                        os.close(destination_fd)
                _close_descriptors(parents)

        unresolved = total - len(held_destinations)
        if stage_fd is not None and unresolved:
            restored = 0
            already_present = 0
            _write_receipt_marker(
                backup,
                _PARTIAL_RESTORE_SENTINEL,
                "incomplete upgrade restore stage was not publishable",
            )
            return IncompleteUpgradeRestoreOutcome(
                restored, already_present, total, False)
        if unresolved:
            _write_receipt_marker(
                backup,
                _PARTIAL_RESTORE_SENTINEL,
                "incomplete upgrade restore retained unresolved originals",
            )
            return IncompleteUpgradeRestoreOutcome(
                restored, already_present, unresolved, False)

        for relative_text in sorted(
                created_directories,
                key=lambda value: (value.count("/"), value),
                reverse=True):
            descriptors = []
            try:
                if relative_text:
                    relative = tuple(PurePosixPath(relative_text).parts)
                    descriptors = _open_relative_directories(
                        album_fd, relative, create=False)
                    directory_fd = descriptors[-1]
                else:
                    directory_fd = album_fd
                if not _apply_serialized_directory_fidelity(
                        directory_fd,
                        source_receipt["fidelity"]["directories"][
                            relative_text],
                        restore_mtime=True,
                ):
                    return IncompleteUpgradeRestoreOutcome(
                        restored, already_present, 0, False)
            finally:
                _close_descriptors(descriptors)

        for item in held_destinations.values():
            try:
                os.fsync(item["descriptor"])
            except OSError:
                return IncompleteUpgradeRestoreOutcome(
                    restored, already_present, 0, False)
        directory_fds = [album_fd, original_parent_fd]
        for item in held_destinations.values():
            directory_fds.extend(item["parents"])
            directory_fds.append(item["parent_fd"])
        if not _fsync_directory_fds(*directory_fds):
            return IncompleteUpgradeRestoreOutcome(
                restored, already_present, 0, False)

        if stage_fd is not None:
            publish_exception = _rename_exact_noreplace_at(
                original_parent_fd,
                stage_name,
                original_parent_fd,
                original_name,
                stage_fd,
            )
            stage_published = True
            if not (
                _named_entry_missing(original_parent_fd, stage_name)
                and _named_directory_matches(
                    original_parent_fd, original_name, stage_fd)
                and _source_parent_is_bound(
                    music_root, source_parts, parent_fds, source_receipt)
                and _fsync_directory_fds(original_parent_fd)
            ):
                return IncompleteUpgradeRestoreOutcome(0, 0, total, False)
            del publish_exception

        if not _destinations_intact():
            return IncompleteUpgradeRestoreOutcome(
                restored, already_present, 0, False)
        removed = _remove_exact_tree_at(
            backup_fds[-2],
            backup_parts[-1],
            backup_fd,
            prefix="ql-restored-incomplete-upgrade",
            expected_snapshot=backup_snapshot,
            held_files=held_backup,
            commit_guard=lambda: (
                _destinations_intact()
                and _backup_source_is_public(
                    backup_root,
                    backup_parts,
                    backup_fds,
                    include_album=False,
                )
            ),
        )
        return IncompleteUpgradeRestoreOutcome(
            restored, already_present, 0, removed)
    except (OSError, TypeError, ValueError, shutil.Error):
        return IncompleteUpgradeRestoreOutcome(
            restored if stage_fd is None else 0,
            already_present if stage_fd is None else 0,
            total - len(held_destinations),
            False,
        )
    finally:
        for item in reversed(list(held_destinations.values())):
            publication = item.get("publication")
            if publication is not None:
                _release_copy_publication(publication)
            else:
                item["lease"].close()
                os.close(item["descriptor"])
            _close_descriptors(item["parents"])
        if held_backup is not None:
            _release_held_snapshot_files(held_backup)
        if file_workspace_fd is not None:
            _cleanup_companion_workspace(
                original_parent_fd,
                file_workspace_name,
                file_workspace_fd,
            )
            os.close(file_workspace_fd)
        if stage_fd is not None:
            if not stage_published:
                _remove_exact_tree_at(
                    original_parent_fd,
                    stage_name,
                    stage_fd,
                    prefix="ql-incomplete-upgrade-stage-cleanup",
                )
            os.close(stage_fd)
            album_fd = None
        elif album_fd is not None:
            os.close(album_fd)
        if hidden_fd is not None:
            os.close(hidden_fd)
        _close_descriptors(parent_fds)
        _close_descriptors(backup_fds)


def restore_upgrade_backup(
        backup, original_path: Path, *, expected_owner=None) -> bool:
    """Move a carried complete whole-album upgrade backup to its origin.

    If a partial download left a sparse album dir at original_path,
    automatically replace it with the backup (the backup is the only intact
    copy). File-subset gap-fill and downsample receipts are refused. Returns
    True on success.

    Compares backup vs partial on TOTAL BYTES, not file count: a partial
    download might have written the single largest track first (1 huge
    file) while the legitimate backup holds the rest (many smaller
    files). File-count alone would call the backup "bigger" and wipe the
    intact track. Bytes-based is what actually matters for "more data
    here than there".
    """
    if not _backup_owner_authorized(backup, expected_owner):
        return False
    backup_opened = _validated_backup_result(
        backup,
        origin=original_path,
        kinds={"upgrade"},
        require_complete=True,
    )
    if backup_opened is None:
        return False
    backup_public, backup_root, backup_parts, backup_fds = backup_opened
    backup_parent_fd = backup_fds[-2]
    backup_fd = backup_fds[-1]
    rooted_original = _rooted_path_parts(cfg.MUSIC_ROOT, Path(original_path))
    if rooted_original is None:
        _close_descriptors(backup_fds)
        return False
    original_public, music_root, original_parts = rooted_original
    parent_opened = _open_rooted_directory(
        music_root,
        original_public.parent,
        create=True,
        allow_root=True,
    )
    if parent_opened is None:
        _close_descriptors(backup_fds)
        return False
    _parent_public, _music_root, parent_parts, parent_fds = parent_opened
    original_parent_fd = parent_fds[-1]
    original_name = original_parts[-1]
    original_fd = None
    trash_name = None
    trash_fd = None
    partial_moved = False
    stage_name = None
    stage_fd = None
    stage_published = False
    restored_fd = None
    moved_backup = False
    restore_committed = False
    backup_snapshot = None
    held_backup = None
    original_snapshot = None
    held_original = None
    restored_snapshot = None
    held_restored = None

    def _parent_chains_are_public():
        return (
            _backup_source_is_public(
                music_root, parent_parts, parent_fds)
            and _backup_source_is_public(
                backup_root, backup_parts, backup_fds)
        )

    def _restore_partial_if_possible():
        nonlocal partial_moved
        if (
            partial_moved
            and _restore_quarantined_entry(
                trash_fd,
                original_parent_fd,
                original_name,
                original_fd,
            )
        ):
            partial_moved = False

    try:
        backup_snapshot = _receipt_disposal_snapshot(
            backup_fd, backup.receipt)
        if (
            backup_snapshot is None
            or _exact_tree_snapshot(backup_fd) != backup_snapshot
        ):
            raise OSError("backup tree could not be read safely")
        held_backup = {}
        _hold_snapshot_files(backup_fd, backup_snapshot, held_backup)
        backup_manifest = {
            tuple(PurePosixPath(relative).parts): (
                expected["size"], expected["sha256"])
            for relative, expected in backup.receipt["tree"]["files"].items()
            if PurePosixPath(relative).name not in _SIDECARS
        }
        backup_bytes = sum(size for size, _digest in backup_manifest.values())

        try:
            original_fd = _open_backup_directory(
                original_name, dir_fd=original_parent_fd)
        except FileNotFoundError:
            original_fd = None
        if original_fd is not None:
            if _reserved_backup_entry_present(original_fd):
                raise OSError(
                    "partial album contains a reserved backup metadata name")
            original_snapshot = _exact_tree_snapshot(original_fd)
            if original_snapshot is None:
                raise OSError("partial album tree could not be sealed")
            held_original = {}
            _hold_snapshot_files(
                original_fd, original_snapshot, held_original)
            original_manifest = _tree_manifest(original_fd)
            if (
                original_manifest is None
                or not _held_snapshot_files_intact(
                    original_fd, original_snapshot, held_original)
            ):
                raise OSError("partial album tree could not be read safely")
            original_bytes = sum(
                size for size, _digest in original_manifest.values())
            if backup_bytes <= original_bytes:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Cannot auto-restore: {original_public} has "
                    f"{len(original_manifest)} file(s) / "
                    f"{original_bytes / 1024 / 1024:.1f} MB (backup has "
                    f"{len(backup_manifest)} / "
                    f"{backup_bytes / 1024 / 1024:.1f} MB)."))
                return False
            trash_name, trash_fd = _reserve_backup_quarantine(
                original_parent_fd, "ql-restore-partial")
            move_exception = _rename_exact_noreplace_at(
                original_parent_fd,
                original_name,
                trash_fd,
                "held",
                original_fd,
            )
            partial_moved = True
            if move_exception is not None:
                raise move_exception
            if not (
                _named_entry_missing(original_parent_fd, original_name)
                and _named_directory_matches(trash_fd, "held", original_fd)
                and _tree_manifest(original_fd) == original_manifest
                and _held_snapshot_files_intact(
                    original_fd, original_snapshot, held_original)
                and _parent_chains_are_public()
                and _fsync_directory_fds(original_parent_fd, trash_fd)
            ):
                _restore_partial_if_possible()
                raise OSError("partial album changed while it was moved aside")
        elif not _named_entry_missing(original_parent_fd, original_name):
            raise OSError("restore destination is not a regular directory")

        if _same_filesystem(backup_public, original_public):
            try:
                move_exception = _rename_exact_noreplace_at(
                    backup_parent_fd,
                    backup_parts[-1],
                    original_parent_fd,
                    original_name,
                    backup_fd,
                )
                moved_backup = True
                if move_exception is not None:
                    _restore_exact_entry_move(
                        original_parent_fd,
                        original_name,
                        backup_parent_fd,
                        backup_parts[-1],
                        backup_fd,
                    )
                    raise move_exception
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise

        if moved_backup:
            candidate_fd = _open_backup_directory(
                original_name, dir_fd=original_parent_fd)
            try:
                if not _same_directory(
                        os.fstat(candidate_fd), os.fstat(backup_fd)):
                    _restore_exact_entry_move(
                        original_parent_fd,
                        original_name,
                        backup_parent_fd,
                        backup_parts[-1],
                        candidate_fd,
                    )
                    raise OSError("backup path changed during restore")
            finally:
                os.close(candidate_fd)
            restored_fd = backup_fd
            if not (
                _named_entry_missing(backup_parent_fd, backup_parts[-1])
                and _named_directory_matches(
                    original_parent_fd, original_name, restored_fd)
                and _tree_manifest(restored_fd) == backup_manifest
                and _held_snapshot_files_intact(
                    restored_fd, backup_snapshot, held_backup)
                and _backup_source_is_public(
                    music_root, parent_parts, parent_fds)
                and _backup_source_is_public(
                    backup_root,
                    backup_parts,
                    backup_fds,
                    include_album=False,
                )
                and _fsync_directory_fds(
                    original_parent_fd, backup_parent_fd)
            ):
                _restore_exact_entry_move(
                    original_parent_fd,
                    original_name,
                    backup_parent_fd,
                    backup_parts[-1],
                    restored_fd,
                )
                _restore_partial_if_possible()
                raise OSError("restored album could not be committed safely")
            restore_committed = True
        else:
            stage_name, stage_fd = _reserve_backup_quarantine(
                original_parent_fd, "ql-restore-stage")
            try:
                os.fchmod(stage_fd, stat.S_IMODE(os.fstat(backup_fd).st_mode))
            except OSError:
                pass
            if not (
                _copy_tree_manifest_at(
                    backup_fd, stage_fd, backup_manifest)
                and _tree_manifest(backup_fd) == backup_manifest
                and _held_snapshot_files_intact(
                    backup_fd, backup_snapshot, held_backup)
                and _parent_chains_are_public()
            ):
                _restore_partial_if_possible()
                raise OSError("restore copy did not match the held backup")
            restored_snapshot = _exact_tree_snapshot(stage_fd)
            if restored_snapshot is None:
                raise OSError("restore stage could not be sealed")
            held_restored = {}
            _hold_snapshot_files(
                stage_fd, restored_snapshot, held_restored)
            if not _fsync_exact_tree(
                    stage_fd, restored_snapshot, held_restored):
                raise OSError("restore stage could not be committed")
            publish_exception = _rename_exact_noreplace_at(
                original_parent_fd,
                stage_name,
                original_parent_fd,
                original_name,
                stage_fd,
            )
            stage_published = True
            restored_fd = stage_fd
            if publish_exception is not None:
                if _restore_exact_entry_move(
                        original_parent_fd,
                        original_name,
                        original_parent_fd,
                        stage_name,
                        restored_fd):
                    stage_published = False
                raise publish_exception
            if not (
                _named_entry_missing(original_parent_fd, stage_name)
                and _named_directory_matches(
                    original_parent_fd, original_name, restored_fd)
                and _tree_manifest(restored_fd) == backup_manifest
                and _held_snapshot_files_intact(
                    restored_fd, restored_snapshot, held_restored)
                and _named_directory_matches(
                    backup_parent_fd, backup_parts[-1], backup_fd)
                and _tree_manifest(backup_fd) == backup_manifest
                and _parent_chains_are_public()
                and _fsync_directory_fds(
                    original_parent_fd, backup_parent_fd)
            ):
                if _restore_exact_entry_move(
                        original_parent_fd,
                        original_name,
                        original_parent_fd,
                        stage_name,
                        restored_fd):
                    stage_published = False
                _restore_partial_if_possible()
                raise OSError("restored album could not be committed safely")
            # Publication is already exact and durable.  From this point an
            # interruption may retain a redundant backup, but must never roll
            # the good public album back to the old partial tree.
            restore_committed = True
            backup_removed = _remove_exact_tree_at(
                backup_parent_fd,
                backup_parts[-1],
                backup_fd,
                prefix="ql-restore-backup",
                expected_snapshot=backup_snapshot,
                held_files=held_backup,
                commit_guard=lambda: (
                    _named_directory_matches(
                        original_parent_fd, original_name, restored_fd)
                    and _tree_manifest(restored_fd) == backup_manifest
                    and _held_snapshot_files_intact(
                        restored_fd, restored_snapshot, held_restored)
                    and _backup_source_is_public(
                        music_root, parent_parts, parent_fds)
                    and _backup_source_is_public(
                        backup_root,
                        backup_parts,
                        backup_fds,
                        include_album=False,
                    )
                ),
            )
            if not backup_removed:
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  The restored album is safe, but the exact backup "
                    f"could not be disposed; it remains at {backup_public}."))

        # The restored tree is now exact and durable. Metadata belongs to the
        # backup container, not the live album; remove only the held regular
        # sidecars that are still named inside this exact directory.
        for sidecar in _SIDECARS:
            try:
                sidecar_fd = _open_regular_file_at(restored_fd, sidecar)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            try:
                _remove_exact_file_at(
                    restored_fd,
                    sidecar,
                    sidecar_fd,
                    prefix="ql-restore-metadata",
                    commit_guard=lambda: _named_directory_matches(
                        original_parent_fd, original_name, restored_fd),
                )
            finally:
                os.close(sidecar_fd)

        # Only after a complete filesystem commit may the exact old partial
        # tree be discarded.  Beets is deliberately not mutated by restored
        # public paths: those paths may already name replacement rows and
        # external remove hooks are not transaction-safe.
        if partial_moved:
            removed_partial = _remove_exact_tree_at(
                trash_fd,
                "held",
                original_fd,
                prefix="ql-restored-partial",
                expected_snapshot=original_snapshot,
                held_files=held_original,
                commit_guard=lambda: (
                    _named_directory_matches(
                        original_parent_fd, original_name, restored_fd)
                    and _tree_manifest(restored_fd) == backup_manifest
                    and _backup_source_is_public(
                        music_root, parent_parts, parent_fds)
                ),
            )
            if removed_partial:
                partial_moved = False
            else:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  The restored album is safe, but the old partial "
                    f"is retained in {trash_name}."))
        if original_fd is not None:
            log.info(fmt(C.GRAY,
                "     · If beets still lists the replaced partial import, "
                "run `beet update`; no path-based database removal was "
                "attempted automatically."))
        return True
    except BaseException as exc:
        if not restore_committed:
            if (
                stage_published
                and stage_fd is not None
                and _restore_exact_entry_move(
                    original_parent_fd,
                    original_name,
                    original_parent_fd,
                    stage_name,
                    stage_fd,
                )
            ):
                stage_published = False
            if (
                moved_backup
                and _restore_exact_entry_move(
                    original_parent_fd,
                    original_name,
                    backup_parent_fd,
                    backup_parts[-1],
                    backup_fd,
                )
            ):
                moved_backup = False
            _restore_partial_if_possible()
        if not isinstance(exc, (OSError, shutil.Error)):
            raise
        log.info(fmt(C.RED,
            f"  ✗  Restore failed: {exc}.\n"
            f"     Backup is preserved at: {backup_public}"))
        return False
    finally:
        if held_restored is not None:
            _release_held_snapshot_files(held_restored)
        if held_original is not None:
            _release_held_snapshot_files(held_original)
        if held_backup is not None:
            _release_held_snapshot_files(held_backup)
        if stage_fd is not None:
            if not stage_published:
                _remove_exact_tree_at(
                    original_parent_fd,
                    stage_name,
                    stage_fd,
                    prefix="ql-restore-stage-cleanup",
                )
            os.close(stage_fd)
        if trash_fd is not None:
            try:
                with os.scandir(trash_fd) as iterator:
                    empty = next(iterator, None) is None
                if (
                    empty
                    and _named_directory_matches(
                        original_parent_fd, trash_name, trash_fd)
                ):
                    _rmdir_exact_at(
                        original_parent_fd, trash_name, trash_fd)
            except BaseException:
                pass
            os.close(trash_fd)
        if original_fd is not None:
            os.close(original_fd)
        _close_descriptors(parent_fds)
        _close_descriptors(backup_fds)


def _audio_duration_seconds(path: Path):
    """Read media duration without consulting the public-path metadata cache."""
    try:
        from qobuz_librarian.library import scanner

        if not scanner.HAVE_MUTAGEN:
            return None
        parsed = scanner.mutagen.File(os.fspath(path), easy=True)
        duration = float(getattr(getattr(parsed, "info", None), "length", 0))
        return duration if duration > 0 else None
    except (OSError, TypeError, ValueError):
        return None
    except Exception:
        # Mutagen format parsers use several non-OSError exception types for a
        # truncated or malformed stream.  Unreadable means unverifiable here.
        return None


def _retention_view_is_redundant(
        replacement_view: Path,
        backup_view: Path,
        *,
        allow_smaller_audio=False,
) -> bool:
    """Semantic retention proof over descriptor-bound private views."""
    try:
        for source in backup_view.rglob("*"):
            if not source.is_file() or source.name in _SIDECARS:
                continue
            relative = source.relative_to(backup_view)
            destination = replacement_view / relative
            if not destination.is_file():
                return False
            if source.suffix.lower() in cfg.AUDIO_EXTS:
                source_duration = _audio_duration_seconds(source)
                destination_duration = _audio_duration_seconds(destination)
                if (
                    source_duration is None
                    or destination_duration is None
                    or destination_duration
                        + max(2.0, source_duration * 0.01)
                        < source_duration
                    or destination.suffix.lower() == ".flac"
                    and flac_audio_ok(destination) is not True
                    or not allow_smaller_audio
                    and destination.stat().st_size < source.stat().st_size
                ):
                    return False
            elif _file_digest(source) != _file_digest(destination):
                return False
        return True
    except (OSError, TypeError, ValueError):
        return False


def _dispose_retention_candidate(candidate, *, allow_smaller_audio=False):
    if not isinstance(candidate, BackupResult) or candidate.receipt is None:
        return False
    replacement = Path(candidate.receipt["origin"])
    replacement_receipt = capture_album_source_receipt(replacement)
    if replacement_receipt is None:
        return False
    return dispose_backup(
        candidate,
        replacement_path=replacement,
        expected_replacement_receipt=replacement_receipt,
        replacement_validator=lambda replacement_view, backup_view: (
            _retention_view_is_redundant(
                replacement_view,
                backup_view,
                allow_smaller_audio=allow_smaller_audio,
            )
        ),
    )


def retire_verified_repair_backup(backup) -> bool:
    """Dispose a repair's originals once each is verifiably superseded.

    The same proof the age sweep applies: every file the backup holds must
    have, at its exact path in the album, a decode-clean track of at least
    its duration. Size may shrink — the backup holds the damaged copies."""
    if (
        not isinstance(backup, BackupResult)
        or not isinstance(backup.receipt, dict)
        or not backup.receipt.get("origin")
    ):
        return False
    return _dispose_retention_candidate(backup, allow_smaller_audio=True)


def _views_are_byte_identical(replacement_view: Path, backup_view: Path) -> bool:
    """Every payload file present at the origin with an identical digest."""
    try:
        checked = False
        for source in backup_view.rglob("*"):
            if not source.is_file() or source.name in _SIDECARS:
                continue
            destination = replacement_view / source.relative_to(backup_view)
            if (
                not destination.is_file()
                or destination.stat().st_size != source.stat().st_size
                or _file_digest(source) != _file_digest(destination)
            ):
                return False
            checked = True
        return checked
    except (OSError, TypeError, ValueError):
        return False


def discard_redundant_backup(path) -> bool:
    """Dispose one retained backup whose files are all byte-identical at
    its recorded origin.

    The age sweep's size proof deliberately cannot override a keep pin — a
    same-path, same-or-larger origin file can hide a different rendition
    whose original survives only in the backup. A user-requested removal
    gets the stronger proof instead: exact digests on both sides, so
    nothing distinct can ever be deleted."""
    candidate = load_backup_result(Path(path))
    if candidate is None or candidate.receipt is None:
        return False
    replacement = Path(candidate.receipt["origin"])
    replacement_receipt = capture_album_source_receipt(replacement)
    if replacement_receipt is None:
        return False
    return dispose_backup(
        candidate,
        replacement_path=replacement,
        expected_replacement_receipt=replacement_receipt,
        replacement_validator=_views_are_byte_identical,
    )


def cleanup_old_upgrade_backups(retention_days: int | None = None,
                                force: bool = False) -> int:
    """Sweep upgrade-backup dir of anything older than retention_days.
    Called once at script startup. Returns count of dirs removed.

    Parses the timestamp prefix encoded in each backup dir's
    name (YYYYMMDD_HHMMSS_safe) instead of stat().st_mtime. shutil.move
    preserves the source's mtime, so a fresh backup of an old folder
    inherited that old mtime — and was being auto-deleted on the very
    next run despite being just minutes old. Skips (does not delete)
    backups whose names don't parse (legacy / hand-named / hand-restored).

    Stamps DATA_DIR/.last_backup_sweep and skips a re-sweep within 24h
    unless ``force=True`` — a CLI session that opens and closes ten times
    in a minute shouldn't stat the whole backup dir each time.
    """
    if retention_days is None:
        retention_days = cfg.UPGRADE_BACKUP_RETENTION_DAYS
    if not cfg.UPGRADE_BACKUP_DIR.exists():
        return 0
    # Crash adoption is recovery, not retention work.  It must run even when
    # the age sweep itself is throttled after an earlier startup today.
    restored_names = _reconcile_ownerless_disposal_residues()
    sweep_stamp = cfg.DATA_DIR / ".last_backup_sweep"
    if not force and sweep_stamp.exists():
        try:
            if (time.time() - sweep_stamp.stat().st_mtime) < 86400:
                return 0
        except OSError:
            pass
    cutoff = time.time() - (retention_days * 86400)
    n_removed = 0
    for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in restored_names:
            # A full carrier adopted from a hard-killed disposal must survive
            # this sweep.  Its ordinary age policy can be reconsidered on the
            # next run, after recovery is independently visible to the user.
            continue
        if entry.name.endswith(".partial") and not (entry / _ORIGIN_SIDECAR).is_file():
            # Stranded mid-copy dir from a hard kill during a cross-fs backup.
            # A committed backup ALWAYS writes the origin sidecar (even one
            # whose album name itself ends in '.partial'), so its absence
            # marks a never-finished copy.
            try:
                stale = (time.time() - entry.stat().st_mtime) > 3600
            except OSError:
                stale = True
            if stale:
                candidate = load_backup_result(entry)
                if (
                    candidate is not None
                    and _dispose_retention_candidate(candidate)
                ):
                    n_removed += 1
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Keeping unfinished backup {entry.name!r}: "
                        "it has no exact app-owned disposal receipt."))
            continue
        m = re.match(r"^(\d{8}_\d{6})_", entry.name)
        if not m:
            log.info(fmt(C.YELLOW,
                f"  ⚠  upgrade-backup dir {entry.name!r} has no timestamp "
                f"prefix; leaving it alone (manual removal required)."))
            continue
        try:
            ts = datetime.strptime(m.group(1),
                                   "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            log.info(fmt(C.YELLOW,
                f"  ⚠  upgrade-backup dir {entry.name!r} has an unparseable "
                f"timestamp prefix; leaving it alone."))
            continue
        if ts < cutoff:
            if (entry / _REAP_AFTER_RETENTION_SENTINEL).is_file():
                # A deliberate undo copy whose origin holds the smaller
                # rewrite ON PURPOSE — the redundancy proof below can never
                # pass for it, and age alone is its whole contract.
                candidate = load_backup_result(entry)
                if candidate is None:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Keeping undo backup {entry.name!r}: its "
                        "app-owned recovery record was unavailable."))
                elif _dispose_retention_candidate(
                        candidate, allow_smaller_audio=True):
                    n_removed += 1
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Keeping undo backup {entry.name!r}: safe "
                        "disposal could not be completed."))
                continue
            candidate = load_backup_result(entry)
            if candidate is None:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Keeping redundant backup {entry.name!r}: its "
                    "app-owned recovery record was unavailable."))
                continue
            if not _backup_safe_to_reap(entry):
                # We can't PROVE this backup is redundant (origin gone, a track
                # not back at it, unreadable, or an explicit keep marker), so it
                # may be the only copy of the tracks it holds. Retention must
                # never reap the last copy; keep it and let the web diagnostic
                # surface it for the user to reconcile (restore or remove).
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Keeping backup {entry.name!r} past retention — can't "
                    f"confirm its tracks are back in the original folder."))
                continue
            if _dispose_retention_candidate(candidate):
                n_removed += 1
            else:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Keeping redundant backup {entry.name!r}: safe "
                    "disposal could not be completed."))
    try:
        sweep_stamp.parent.mkdir(parents=True, exist_ok=True)
        sweep_stamp.touch()
    except OSError:
        pass
    return n_removed
