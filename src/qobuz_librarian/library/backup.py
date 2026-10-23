"""Upgrade and gap-fill backup/restore functions."""
import ctypes
import errno
import filecmp
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian import disk_support
from qobuz_librarian.integrations.rip import flac_audio_ok
from qobuz_librarian.library import scanner
from qobuz_librarian.library.scanner import iter_tree_no_symlinks
from qobuz_librarian.recovery import (
    decode_recovery_json,
    normalise_recovery_owner,
    recovery_owner_matches,
)
from qobuz_librarian.safefile import (
    AT_FDCWD,
    copy_file,
    copy_metadata,
    delete_tree_verified,
    delete_verified,
    fsync_dir,
    fsync_dirs,
    fsync_tree,
    require_noreplace_rename,
    snapshot_tree,
    stat_identity,
    write_atomic,
)
from qobuz_librarian.safefile import digest_fd as _read_file_digest_fd
from qobuz_librarian.safefile import named_entry_matches as _named_entry_matches
from qobuz_librarian.safefile import named_entry_missing as _named_entry_missing
from qobuz_librarian.safefile import rename_noreplace as _rename_noreplace_at
from qobuz_librarian.safefile import same_directory as _same_directory
from qobuz_librarian.ui_cli import errors as errors_mod
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
    """Truthful result of reconciling an interrupted whole-album backup."""

    restored: int
    already_present: int
    unresolved: int
    backup_disposed: bool


def _list_tree(root: Path):
    """List every entry, returning None if any part is unreadable."""
    try:
        if not stat.S_ISDIR(root.stat().st_mode):
            return None
    except FileNotFoundError:
        return []
    except OSError:
        return None
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
    # Names are limited in bytes; keep room for the stamp and the prefixes.
    safe = safe.encode()[:150].decode(errors="ignore")
    infix = f"{kind}_" if kind else ""
    return f"{ts}_{infix}{safe}"


def _upgrade_backup_path_for(album_dir: Path) -> Path:
    cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return cfg.UPGRADE_BACKUP_DIR / _backup_dir_name(album_dir)


def _same_filesystem(a: Path, b: Path) -> bool:
    """True if a and b live on the same filesystem (same st_dev).

    Inside Docker, /music and /upgrade_backups are separate bind mounts
    so they get different st_dev even when the host paths share a disk -
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


def _open_backup_directory(path, *, dir_fd=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.open(os.fspath(path), flags, dir_fd=dir_fd)


def _entry_identity(value):
    return (
        stat.S_IFMT(value.st_mode),
        int(value.st_dev),
        int(value.st_ino),
    )


def _same_entry(left, right) -> bool:
    return _entry_identity(left) == _entry_identity(right)


_named_directory_matches = functools.partial(
    _named_entry_matches, directory=True)


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
    try:
        fsync_dirs(*descriptors)
    except OSError:
        return False
    return True


def _backup_root_is_public(backup_root: Path, backup_root_fd) -> bool:
    try:
        return _same_directory(
            os.fstat(backup_root_fd),
            os.stat(backup_root, follow_symlinks=False),
        )
    except (OSError, TypeError, ValueError):
        return False


def _reserved_backup_entry_present(source_fd) -> bool:
    entries = _list_tree(_held_directory_path(source_fd))
    if entries is None:
        return True
    try:
        return any(entry.name in _SIDECARS for entry in entries)
    except OSError:
        return True


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
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | os.O_CLOEXEC,
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


def _open_regular_file_at(parent_fd, name):
    flags = (os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
             | os.O_NONBLOCK)
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


def _copy_owner(value):
    """The owner and group a copy of this file or folder can be given.

    Only root can give a copy another user's uid or a group it is not in.
    Any other app user keeps the copy its own, in the source's group when it
    belongs to that group, so a library owned by another account still backs
    up and restores.
    """
    uid, gid = int(value.st_uid), int(value.st_gid)
    if os.geteuid() == 0:
        return uid, gid
    if gid != os.getegid() and gid not in os.getgroups():
        gid = os.getegid()
    return os.geteuid(), gid


def _fidelity_fd(descriptor, *, include_times=True):
    value = os.fstat(descriptor)
    uid, gid = _copy_owner(value)
    result = {
        "type": stat.S_IFMT(value.st_mode),
        "mode": stat.S_IMODE(value.st_mode),
        "uid": uid,
        "gid": gid,
        "xattrs": _xattrs_fd(descriptor),
    }
    if include_times:
        result.update({
            "atime_ns": int(value.st_atime_ns),
            "mtime_ns": int(value.st_mtime_ns),
        })
    return result


def _tree_fidelity_snapshot(root_fd, tree):
    """Fidelity receipt for an open folder's tree, without following links.

    Each file's sha256 comes from its entry in ``tree``, an
    ``_exact_tree_snapshot``; a file that no longer matches it gives None."""
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
                known = tree["files"].get(key)
                file_fd = _open_regular_file_at(directory_fd, name)
                try:
                    current = _fidelity_fd(file_fd, include_times=False)
                    now = os.fstat(file_fd)
                    if (known is None or _file_identity(now)
                            != _fidelity_without(known, "sha256")):
                        return False
                    current.update({
                        "size": int(now.st_size),
                        "mtime_ns": int(now.st_mtime_ns),
                        "sha256": known["sha256"],
                        "links": int(now.st_nlink),
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


def _normalise_gap_fill_expected_receipts(receipts, *, allow_identity=False):
    """Check per-file receipts; ``allow_identity`` also takes them without sha256."""
    if not isinstance(receipts, dict) or not receipts:
        raise OSError("gap-fill expected receipts are missing")
    identity_fields = _GAP_FILL_RECEIPT_FIELDS - {"sha256"}
    normalised = {}
    for raw_relative, raw_receipt in receipts.items():
        parts = _gap_fill_receipt_relative(raw_relative)
        if (
            not isinstance(raw_receipt, dict)
            or not (
                set(raw_receipt) == _GAP_FILL_RECEIPT_FIELDS
                or allow_identity and set(raw_receipt) == identity_fields
            )
            or any(
                type(raw_receipt[field]) is not int
                for field in identity_fields
            )
            or raw_receipt["type"] != stat.S_IFREG
            or "sha256" in raw_receipt
            and not _receipt_digest_valid(raw_receipt["sha256"])
        ):
            raise OSError("gap-fill expected receipt is malformed")
        relative = "/".join(parts)
        if relative in normalised:
            raise OSError("gap-fill expected receipt path is duplicated")
        normalised[relative] = dict(raw_receipt)
    return normalised


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
    try:
        snapshot = _exact_tree_snapshot(album_fd)
        if snapshot is None:
            return None
        fidelity = _tree_fidelity_snapshot(album_fd, snapshot)
        if fidelity is None:
            return None
        generations = _tree_directory_generations(album_fd, snapshot)
        path_generations = _path_directory_generations(descriptors)
        if (
            not _backup_source_is_public(music_root, parts, descriptors)
            or _exact_tree_snapshot(album_fd, digests=False)
                != _tree_identity(snapshot)
            or _tree_fidelity_snapshot(album_fd, snapshot) != fidelity
            or _tree_directory_generations(album_fd, snapshot) != generations
            or _path_directory_generations(descriptors) != path_generations
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


def capture_album_tree_identity(album_dir):
    """Seal a folder by its names, sizes, times, inodes and directories.

    No file is opened or read. Any write to a file moves its ctime, which
    user space cannot set, so a later capture that compares equal proves the
    folder is unchanged. Moving or deleting files still needs the exact
    receipt from capture_album_source_receipt.
    """
    opened = _open_backup_source(Path(album_dir))
    if opened is None:
        return None
    public, music_root, parts, descriptors = opened
    try:
        tree = _exact_tree_snapshot(descriptors[-1], digests=False)
        if tree is None or any(
                PurePosixPath(relative).name in _SIDECARS
                for relative in (*tree["directories"], *tree["files"])):
            return None
        receipt = {
            "version": 1,
            "origin": os.fspath(public),
            "tree": tree,
            "directory_generations": _tree_directory_generations(
                descriptors[-1], tree),
            "path_generations": _path_directory_generations(descriptors),
        }
        if not _backup_source_is_public(music_root, parts, descriptors):
            return None
        return receipt
    except (OSError, TypeError, ValueError):
        return None
    finally:
        _close_descriptors(descriptors)


def album_tree_identity(receipt):
    """The part of an exact source receipt that capture_album_tree_identity seals."""
    return {
        "version": receipt["version"],
        "origin": receipt["origin"],
        "tree": _tree_identity(receipt["tree"]),
        "directory_generations": receipt["directory_generations"],
        "path_generations": receipt["path_generations"],
    }


def capture_folder_generations(path):
    """The folder at ``path`` made absolute, with the generation of each
    folder from MUSIC_ROOT down to it, opened without following links; None
    when it is not such a folder."""
    opened = _open_backup_source(Path(path))
    if opened is None:
        return None
    public, _music_root, _parts, descriptors = opened
    try:
        return public, _path_directory_generations(descriptors)
    finally:
        _close_descriptors(descriptors)


def capture_music_root_identity():
    """The music root's own directory incarnation, with nothing walked.

    A restore row for an artist the library has no folder for has no tree to
    seal a receipt over. What still has to hold before it fetches anything is
    that the music folder is the same mounted directory it was reviewed
    against; an unmounted share would otherwise take the whole rebuild onto
    the container's own disk.
    """
    from qobuz_librarian.library.migrate import _directory_identity

    try:
        descriptor = _open_backup_directory(
            Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT))))
    except OSError:
        return None
    try:
        return _directory_identity(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)


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


def _snapshot_file(directory_fd, name):
    descriptor = _open_regular_file_at(directory_fd, name)
    try:
        identity = _file_identity(os.fstat(descriptor))
        digest = _file_digest_fd(descriptor)
        if _file_identity(os.fstat(descriptor)) != identity:
            raise OSError(errno.EAGAIN, "file changed while it was read")
        return {**identity, "sha256": digest}
    finally:
        os.close(descriptor)


def _file_identity(value):
    return {
        "identity": list(_entry_identity(value)),
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
    }


def _tree_identity(snapshot):
    """A snapshot without its content digests, as ``digests=False`` takes it."""
    return {
        **snapshot,
        "files": {
            relative: _fidelity_without(value, "sha256")
            for relative, value in snapshot["files"].items()
        },
    }


def _exact_tree_snapshot(root_fd, *, ignore_root_names=(), digests=True,
                         sealed=None):
    """Describe every exact regular file and directory below a held root.

    ``digests=False`` reads no file: each one is described by its inode,
    size, mtime and ctime alone. A file whose inode, size and times still
    equal its entry in the ``sealed`` snapshot keeps that entry's digest
    unread, since any write would have moved its ctime.
    """
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
                identity = _file_identity(value)
                known = (sealed or {}).get("files", {}).get(key)
                try:
                    if not digests:
                        snapshot["files"][key] = identity
                    elif (type(known) is dict and "sha256" in known
                            and _fidelity_without(known, "sha256") == identity):
                        snapshot["files"][key] = {
                            **identity, "sha256": known["sha256"]}
                    else:
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


def _tree_matches_a_copy_of_itself(current, expected) -> bool:
    """Match a sealed tree against a byte-identical copy of the same files.

    A snapshot restore or a moved data volume rewrites every inode while the
    bytes stay the same. Name, size and sha256 prove these are the files the
    app put here; the inode only ever added "and the same filesystem object",
    which no copy preserves and no restore needs.
    """
    if type(current) is not dict or type(expected) is not dict:
        return False
    if (
        type(current.get("directories")) is not dict
        or type(expected.get("directories")) is not dict
        or set(current["directories"]) != set(expected["directories"])
        or type(current.get("files")) is not dict
        or type(expected.get("files")) is not dict
        or set(current["files"]) != set(expected["files"])
    ):
        return False
    volatile = ("identity", "mtime_ns", "changed_ns")
    for name, value in current["files"].items():
        original = expected["files"][name]
        if type(value) is not dict or type(original) is not dict:
            return False
        if (_fidelity_without(value, *volatile)
                != _fidelity_without(original, *volatile)):
            return False
    return True


def _receipt_matches_a_copy_of_itself(current, expected) -> bool:
    """Match persisted receipts across byte-identical copies.

    Reopening may adopt a restored copy; live checks retain their held identities.
    """
    if type(current) is not dict or type(expected) is not dict:
        return False
    return (
        _fidelity_without(current, "tree", "receipt_identity")
            == _fidelity_without(expected, "tree", "receipt_identity")
        and _tree_matches_a_copy_of_itself(
            current.get("tree"), expected.get("tree"))
    )


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


# Digests of files whose ctime was already older than the filesystem's
# timestamp granularity when they were read, and that did not change while
# they were read. A later write, or a new file on the reused inode number,
# moves the ctime, so these stay good after the file is closed while the
# size and times match. A ctime on a whole second may come from a
# filesystem that keeps only seconds. Only local Linux filesystems keep a
# ctime no one can set: FAT and exFAT keep none on disk, and FUSE and
# network mounts report whatever they are given.
_SETTLED_DIGESTS = {}
_SETTLED_DIGESTS_LIMIT = 16384
_SETTLED_NS = 50_000_000
_SETTLED_WHOLE_SECOND_NS = 2_000_000_000


def _file_digest_fd(descriptor):
    read_ns = time.time_ns()
    value = os.fstat(descriptor)
    times = (int(value.st_size), int(value.st_mtime_ns), int(value.st_ctime_ns))
    key = (int(value.st_dev), int(value.st_ino))
    settled = _SETTLED_DIGESTS.get(key)
    if settled is not None and settled[0] == times:
        return settled[1]
    digest = _read_file_digest_fd(descriptor)
    after = os.fstat(descriptor)
    changed = times[2]
    settle = (_SETTLED_WHOLE_SECOND_NS if changed % 1_000_000_000 == 0
              else _SETTLED_NS)
    if (
        (int(after.st_size), int(after.st_mtime_ns),
         int(after.st_ctime_ns)) == times
        and changed < read_ns - settle
        and _keeps_own_ctime(descriptor)
    ):
        if len(_SETTLED_DIGESTS) >= _SETTLED_DIGESTS_LIMIT:
            _SETTLED_DIGESTS.clear()
        _SETTLED_DIGESTS[key] = (times, digest)
    return digest


# Local filesystems: every write to a file there changes its ctime.
_LOCAL_FILESYSTEM_MAGIC = frozenset({
    0x0000ADF5,  # ADOS
    0x0000EF53,  # ext2/ext3/ext4
    0x01021994,  # tmpfs
    0x24051905,  # UBIFS
    0x2FC12FC1,  # ZFS
    0x3153464A,  # JFS
    0x3434,  # NILFS
    0x52654973,  # ReiserFS
    0x58465342,  # XFS
    0x794C7630,  # overlayfs backed by a local lower filesystem
    0x858458F6,  # ramfs
    0x9123683E,  # Btrfs
    0xF2F52010,  # F2FS
})


class _LinuxStatFS(ctypes.Structure):
    _fields_ = (
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    )


def _keeps_own_ctime(descriptor) -> bool:
    fstatfs = ctypes.CDLL(None, use_errno=True).fstatfs
    fstatfs.argtypes = (ctypes.c_int, ctypes.POINTER(_LinuxStatFS))
    fstatfs.restype = ctypes.c_int
    value = _LinuxStatFS()
    if fstatfs(descriptor, ctypes.byref(value)):
        return False
    return (int(value.f_type) & 0xFFFFFFFF) in _LOCAL_FILESYSTEM_MAGIC


def _file_digest(path: Path) -> str:
    """sha256 hex of a file's bytes, read in chunks so a large FLAC isn't loaded
    into memory all at once."""
    with open(path, "rb") as fh:
        return _file_digest_fd(fh.fileno())


_XATTR_UNSUPPORTED = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}


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

# Dropped into the backup that holds a replacement an upgrade could not
# verify, after the original album was put back in its place. It is a
# download, not the user's album, and Diagnostics says so.
_SET_ASIDE_SENTINEL = ".ql_set_aside_replacement"

# Dropped into a backup that exists only as an undo window - the downsample
# keep-originals copy. Its origin deliberately holds the SMALLER rewrite, so
# the content-presence proof below can never call it redundant; without this
# marker the sweep would keep it forever and the diagnostics would nag about
# a state the user asked for. It inverts the default: age alone reaps it.
_REAP_AFTER_RETENTION_SENTINEL = ".ql_reap_after_retention"

# Added to a downsample's kept originals once the rewrite is done. It names
# the STREAMINFO of each file the album was left holding, so a restore can
# tell the album still has what Downsample wrote and not a later download.
_DOWNSAMPLE_OUTPUTS_SENTINEL = ".ql_downsample_outputs"

# Added to the backup a consolidation moves a duplicate folder's tracks into.
# Its origin is the duplicate folder, which no longer holds them, so the
# content proof can never pass there. The marker names the album kept in its
# place and the track standing in for each file, and the age sweep proves
# against those instead.
_CONSOLIDATED_SENTINEL = ".ql_consolidated"

# Whole-album copies are sealed before their public source is retired.  The
# immutable receipt therefore starts incomplete and this receipt-bound marker
# is the only persisted transition to a caller-visible complete result.
_UPGRADE_RETIREMENT_COMPLETE_SENTINEL = ".ql_upgrade_retirement_complete"

# Earlier versions wrote this marker, naming where the album was left, when
# its removal from the library could not be proved complete. A backup
# without it is found where it is.
_UPGRADE_RECOVERY_STATE_SENTINEL = ".ql_upgrade_recovery_state"

# Written into an upgrade's backup before its download is imported. It names
# the album's beets entries and the download's staged folders, so a start-up
# after a hard stop can finish the upgrade or undo it.
_UPGRADE_PENDING_SENTINEL = ".ql_upgrade_pending"

# Earlier versions wrote these receipt-bound records into an upgrade's backup
# while carrying its other files into the new album. A backup holding them
# still validates and stays out of the age sweep.
_COMPANION_CARRY_INTENT_SENTINEL = ".ql_companion_carry_intent"
_COMPANION_CARRY_READY_SENTINEL = ".ql_companion_carry_ready"
_COMPANION_CARRY_COMMITTED_SENTINEL = ".ql_companion_carry_committed"

# Immutable ownership receipt for exact disposal.  It binds the backup root,
# the receipt inode itself, and every regular file and directory below it.  A
# copied or replaced tree therefore cannot borrow a stale receipt and be
# mistaken for the backup this process created.
_RECEIPT_SIDECAR = ".ql_backup_receipt"
# A receipt takes about 750 bytes a file, so this fits albums of some 80,000
# files, while a damaged one is still not read whole into memory.
_RECORD_READ_BYTES = 64 * 1024 * 1024
_RECEIPT_VERSION = 1
_OWNED_RECEIPT_VERSION = 2

# Written beside a whole-album backup before the album leaves its folder and
# removed once the album is whole in its folder again or the backup holds its
# origin and receipt. One left behind means a stop cut the backup short; it
# names the backup and the album's sealed tree, which is what
# put_back_interrupted_backups proves the album against.
_BACKUP_INTENT_PREFIX = ".ql-backup-intent-"

# Names the album folder a restore renamed back into the library until the
# backup's own files inside it are removed.
_PUT_BACK_PREFIX = ".ql-put-back-"

# The copy of an album to another filesystem, named after its backup, until
# it is complete and renamed to the backup's own name.
_PARTIAL_COPY_PREFIX = ".ql-backup-copy-"

# A backup being deleted is renamed to this prefix and its own name, or for a
# queue job the name its journal saved, so it is no longer listed as one.
_LEFTOVER_PREFIX = ".ql-dispose-backup-"


def _hidden_name(name: str, prefix=_LEFTOVER_PREFIX) -> str:
    # A long album name may leave no room for the prefix; its end goes, and
    # the stamp and kind at its front stay.
    while len(os.fsencode(prefix + name)) > 255:
        name = name[:-1]
    return prefix + name


# Files a backup carries that aren't backed-up tracks.
_SIDECARS = (
    _ORIGIN_SIDECAR,
    _PARTIAL_RESTORE_SENTINEL,
    _UNVERIFIED_UPGRADE_SENTINEL,
    _SET_ASIDE_SENTINEL,
    _REAP_AFTER_RETENTION_SENTINEL,
    _DOWNSAMPLE_OUTPUTS_SENTINEL,
    _CONSOLIDATED_SENTINEL,
    _UPGRADE_RETIREMENT_COMPLETE_SENTINEL,
    _UPGRADE_RECOVERY_STATE_SENTINEL,
    _UPGRADE_PENDING_SENTINEL,
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
    _SET_ASIDE_SENTINEL,
    _DOWNSAMPLE_OUTPUTS_SENTINEL,
    _CONSOLIDATED_SENTINEL,
    _UPGRADE_RETIREMENT_COMPLETE_SENTINEL,
    _UPGRADE_RECOVERY_STATE_SENTINEL,
    _UPGRADE_PENDING_SENTINEL,
    _COMPANION_CARRY_INTENT_SENTINEL,
    _COMPANION_CARRY_READY_SENTINEL,
    _COMPANION_CARRY_COMMITTED_SENTINEL,
)


def _write_backup_receipt(directory_fd, origin, *, kind, complete,
                          requested, backed_up, source_receipt=None,
                          owner=None, sealed=None):
    if not _named_entry_missing(directory_fd, _RECEIPT_SIDECAR):
        return None
    tree = _exact_tree_snapshot(
        directory_fd,
        ignore_root_names=(_RECEIPT_SIDECAR, *_OPTIONAL_RECEIPT_MARKERS),
        sealed=sealed,
    )
    if tree is None:
        return None
    descriptor = None
    try:
        descriptor = os.open(
            _RECEIPT_SIDECAR,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | os.O_CLOEXEC,
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
        if len(data) > _RECORD_READ_BYTES:
            log.info(fmt(C.RED,
                f"  ✗  {origin} has too many files for its backup record "
                f"({int(requested)}); nothing was backed up."))
            raise ValueError("backup receipt is too large to read back")
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
                os.unlink(_RECEIPT_SIDECAR, dir_fd=directory_fd)
                fsync_dirs(directory_fd)
            except OSError:
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


def _receipt_file_snapshot_schema_valid(value, *, digests=True) -> bool:
    fields = {"identity", "size", "mtime_ns", "changed_ns"}
    return (
        type(value) is dict
        and set(value) == (fields | {"sha256"} if digests else fields)
        and _receipt_identity_schema_valid(
            value["identity"], stat.S_IFREG)
        and type(value["size"]) is int
        and value["size"] >= 0
        and type(value["mtime_ns"]) is int
        and type(value["changed_ns"]) is int
        and (not digests or _receipt_digest_valid(value["sha256"]))
    )


def _tree_snapshot_schema_valid(value, *, digests=True) -> bool:
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
            and _receipt_file_snapshot_schema_valid(snapshot, digests=digests)
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


def _album_tree_identity_schema_valid(receipt, origin=None, *,
                                     digests=False) -> bool:
    fields = {
        "version", "origin", "tree", "directory_generations",
        "path_generations",
    }
    if (
        type(receipt) is not dict
        or set(receipt) != (fields | {"fidelity"} if digests else fields)
        or type(receipt["version"]) is not int
        or receipt["version"] != 1
        or type(receipt["origin"]) is not str
        or not receipt["origin"]
        or "\x00" in receipt["origin"]
        or not _tree_snapshot_schema_valid(receipt["tree"], digests=digests)
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
    generations = receipt["directory_generations"]
    directory_keys = set(tree["directories"]) | {""}
    if (
        set(generations) != directory_keys
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
    return receipt["path_generations"][-1] == generations[""]


def _source_receipt_nested_schema_valid(receipt, origin=None) -> bool:
    if (
        not _album_tree_identity_schema_valid(receipt, origin, digests=True)
        or type(receipt["fidelity"]) is not dict
        or set(receipt["fidelity"]) != {"directories", "files"}
        or type(receipt["fidelity"]["directories"]) is not dict
        or type(receipt["fidelity"]["files"]) is not dict
    ):
        return False

    tree = receipt["tree"]
    fidelity = receipt["fidelity"]
    directory_keys = set(tree["directories"]) | {""}
    file_keys = set(tree["files"])
    if (
        set(fidelity["directories"]) != directory_keys
        or set(fidelity["files"]) != file_keys
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
    ):
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
    return True


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
        if value.st_size <= 0 or value.st_size > _RECORD_READ_BYTES:
            return None
        raw = os.pread(descriptor, value.st_size + 1, 0)
        if len(raw) != value.st_size:
            return None
        receipt = decode_recovery_json(raw, max_bytes=_RECORD_READ_BYTES)
        if (
            not _backup_receipt_value_schema_valid(receipt)
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
            sealed=receipt["tree"],
        )
        reopened = dict(
            receipt, tree=current, receipt_identity=list(_entry_identity(value)))
        if not _receipt_matches_a_copy_of_itself(reopened, receipt):
            return None
        return reopened
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _seal_backup_result(path, directory_fd, origin, *, kind, complete,
                        requested, backed_up, source_receipt=None, owner=None,
                        folder=None):
    """Flush the backup to disk and write its receipt. ``folder`` is where
    the backup is while it is still being copied, else ``path``."""
    durable_snapshot = _exact_tree_snapshot(
        directory_fd,
        ignore_root_names=(_RECEIPT_SIDECAR, *_OPTIONAL_RECEIPT_MARKERS),
    )
    if (
        durable_snapshot is None
        or not fsync_tree(path if folder is None else folder)
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
        sealed=durable_snapshot,
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


def canonical_album_tree_identity(value, *, expected_origin=None):
    """Return one closed-schema folder identity, or ``None`` when malformed."""
    if not _album_tree_identity_schema_valid(value, expected_origin):
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
    return f"{_LEFTOVER_PREFIX}{hashlib.sha256(material).hexdigest()}"


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
    """Reopen a journal backup at its saved path, accepting an identical copy."""
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
        or not _receipt_matches_a_copy_of_itself(
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
    return _backup_entry_absent(canonical["path"])


def _backup_entry_absent(path) -> bool:
    root_fd = None
    try:
        backup_root = Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))
        backup_path = Path(path)
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


def library_backup_origin_state(value, *, expected_owner=None):
    """Compare a carried backup's origin with the files its receipt holds.

    "restored" means every backed-up file is at the origin with its receipt
    bytes, "conflict" that something else sits where one belongs (for a whole
    album, any other content in the folder than untouched files of the album
    it was), and "clear" that a restore would replace nothing. None when the
    origin cannot be read.
    """
    canonical = canonical_library_backup_record(
        value,
        expected_owner=expected_owner,
    )
    if canonical is None:
        return None
    manifest = {
        tuple(PurePosixPath(relative).parts): (
            snapshot["size"], snapshot["sha256"])
        for relative, snapshot in canonical["receipt"]["tree"]["files"].items()
        if PurePosixPath(relative).name not in _SIDECARS
    }
    origin = Path(canonical["origin"])
    opened = _open_rooted_directory(cfg.MUSIC_ROOT, origin)
    if opened is None:
        try:
            if (
                _rooted_path_parts(cfg.MUSIC_ROOT, origin) is None
                or os.path.lexists(origin)
            ):
                return None
        except (OSError, ValueError):
            return None
        return "clear"
    descriptors = opened[-1]
    album_fd = descriptors[-1]
    matched = 0
    try:
        for relative, (size, digest) in manifest.items():
            try:
                parents, _parent_fd, file_fd = _open_tree_file(
                    album_fd, relative)
            except FileNotFoundError:
                continue
            try:
                if (
                    os.fstat(file_fd).st_size != size
                    or _file_digest_fd(file_fd) != digest
                ):
                    return "conflict"
            finally:
                os.close(file_fd)
                _close_descriptors(parents)
            matched += 1
        if matched == len(manifest):
            return "restored"
        if canonical["kind"] == "upgrade":
            source_receipt = canonical["receipt"].get("source_receipt")
            # What a stop left of the album while it was deleted after its
            # copy is filled in.
            if source_receipt is not None and _album_untouched(
                    origin, source_receipt):
                return "clear"
            with os.scandir(album_fd) as entries:
                if next(entries, None) is not None:
                    return "conflict"
        return "clear"
    except OSError:
        return None
    finally:
        _close_descriptors(descriptors)


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
            # A marker this receipt already owns keeps the backup whatever
            # note it was written with.
            return existing == expected or _receipt_owned_marker(
                directory_fd, name, backup.receipt)
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


def reconcile_library_backup_disposal(
        carrier_record, disposal_record, *, expected_owner=None):
    """Finish deleting a queue backup a hard stop left under its hidden name.

    Its journal saved the backup's files before the rename, once the
    validator had proved them redundant. Only those files and the backup's
    own marker files are deleted; anything else stays, and so does the
    hidden folder."""
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
    residue = Path(carrier["path"]).parent / disposal["quarantine_name"]
    if not os.path.lexists(residue):
        return BackupDisposalReconciliation("none", residue)
    current = snapshot_tree(residue)
    if current is None:
        return BackupDisposalReconciliation("attention", residue)
    saved = disposal["snapshot"]
    authorised = {}
    for relative, value in current.items():
        known = saved["files"].get(relative)
        if (
            (stat.S_ISDIR(value[2]) and relative in saved["directories"])
            or relative in _OPTIONAL_RECEIPT_MARKERS
            or (known is not None
                and (stat.S_IFMT(value[2]), *value[:2], *value[3:5]) == (
                    *known["identity"], known["size"], known["mtime_ns"]))
        ):
            authorised[relative] = value
    if delete_tree_verified(residue, authorised):
        return BackupDisposalReconciliation("attention", residue)
    return BackupDisposalReconciliation("disposed", residue)


def _delete_backup_folder(path: Path, snapshot, hidden_name=None) -> bool:
    """Delete what of the backup at ``path`` still matches ``snapshot``. A
    listed backup is first renamed to ``hidden_name`` beside it, so it is no
    longer listed as a backup.

    Whatever is left stays under the hidden name, which Diagnostics lists
    as an unfinished deletion."""
    if hidden_name is not None:
        try:
            parent_fd = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                _rename_noreplace_at(
                    parent_fd, path.name, parent_fd, hidden_name)
            finally:
                os.close(parent_fd)
        except OSError:
            return False
        path = path.parent / hidden_name
    left = delete_tree_verified(path, snapshot)
    if left:
        log.info(fmt(
            C.YELLOW,
            f"  ⚠  Stopped deleting the backup {backup_name(path)}: some of "
            "it changed while it was being deleted, so what is left was kept "
            f"at {path}."))
    return not left


def _proven_redundant(folder, backup_path, replacement_path,
                      expected_replacement_receipt, validator):
    """A snapshot of ``folder`` once ``validator`` proves the backup at
    ``backup_path`` redundant, both stayed as they were meanwhile and the
    replacement is on disk, else None."""
    try:
        replacement = Path(os.path.abspath(os.fspath(replacement_path)))
        snapshot = snapshot_tree(folder)
        if snapshot is None or _same_entry(
                os.lstat(replacement), os.lstat(backup_path)):
            return None
        if not validator(replacement, backup_path):
            return None
        replacement_now = capture_album_tree_identity(replacement)
        if (
            replacement_now is None
            or replacement_now != album_tree_identity(
                expected_replacement_receipt)
            or snapshot_tree(folder) != snapshot
            or not fsync_tree(replacement)
        ):
            return None
    except (OSError, TypeError, ValueError, KeyError, shutil.Error):
        return None
    return snapshot


def dispose_backup(
        backup,
        *,
        replacement_path=None,
        expected_replacement_receipt=None,
        replacement_validator=None,
        expected_owner=None,
        expected_disposal_record=None,
) -> bool:
    """Delete one complete backup once the caller's validator proves it
    redundant.

    The validator is called as ``validator(replacement_path, backup_path)``.
    The backup goes only while its files still match the snapshot taken
    before the validator ran, and while the replacement still matches the
    receipt the caller captured. A queue disposal also needs the backup to
    match the record its journal saved. Any doubt keeps the backup.

    What is left of a backup whose deletion was cut short, as
    ``removal_candidate`` reads it, is deleted where it is on the same
    proof.
    """
    if (isinstance(backup, BackupResult)
            and (top := _leftover_top(backup.path)) is not None):
        if (
            expected_owner is not None
            or expected_disposal_record is not None
            or not backup.complete
            or replacement_path is None
            or expected_replacement_receipt is None
            or not callable(replacement_validator)
            or _unrecorded_file(backup) is not None
        ):
            return False
        if not _backed_up_entries(backup.path):
            # Only its markers are left, so there is nothing to prove.
            snapshot = snapshot_tree(top)
            return snapshot is not None and _delete_backup_folder(top, snapshot)
        snapshot = _proven_redundant(
            top, backup.path, replacement_path,
            expected_replacement_receipt, replacement_validator)
        return snapshot is not None and _delete_backup_folder(top, snapshot)
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
    directory_fd = opened[-1][-1]
    try:
        receipt = backup.receipt
        current = _receipt_disposal_snapshot(directory_fd, receipt)
        hidden_name = _library_backup_disposal_quarantine_name(
            receipt, expected_owner)
        if current is None or hidden_name is None:
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
            if (
                disposal is None
                or disposal["quarantine_name"] != hidden_name
                or not _tree_matches_ignoring_ctime(
                    current,
                    _authorised_disposal_remainder_snapshot(
                        directory_fd, current, disposal["snapshot"], receipt),
                )
            ):
                return False
        else:
            hidden_name = _hidden_name(backup.path.name)
    except (OSError, TypeError, ValueError):
        return False
    finally:
        _close_descriptors(opened[-1])

    snapshot = _proven_redundant(
        backup.path, backup.path, replacement_path,
        expected_replacement_receipt, replacement_validator)
    return snapshot is not None and _delete_backup_folder(
        backup.path, snapshot, hidden_name)


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


def _remove_earlier_carry_folder(backup_fd, receipt, album, album_receipt):
    """Remove the folder an earlier version's carry left beside the album.

    Its name comes from the record that carry wrote in the backup, and it
    goes only while every file in it has the bytes of one in the album or
    the backup.
    """
    try:
        note = _receipt_marker_note(
            backup_fd, _COMPANION_CARRY_INTENT_SENTINEL, receipt)
        intent = decode_recovery_json(note) if note is not None else None
        name = intent.get("workspace_name") if isinstance(
            intent, dict) else None
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"\.ql-companion-carry-[0-9a-f]{32}", name)
            or intent.get("origin") != os.fspath(album)
        ):
            return
        workspace = album.parent / name
        snapshot = _plain_tree(workspace)
        copies = {
            value["sha256"]
            for tree in (album_receipt["tree"], receipt["tree"])
            for value in tree["files"].values()
        }
        if snapshot is not None and all(
                stat.S_ISDIR(value[2])
                or _file_digest(workspace / relative) in copies
                for relative, value in snapshot.items()):
            delete_tree_verified(workspace, snapshot)
    except (OSError, TypeError, ValueError, KeyError):
        return


def _case_alias(album, names, name):
    """The one entry of ``names``, the album's listing, that differs from
    ``name`` only in case and that the album folder also finds under
    ``name``: one file on a case-insensitive disk. None otherwise."""
    folded = name.casefold()
    found = [other for other in names
             if other != name and other.casefold() == folded]
    if len(found) != 1:
        return None
    try:
        given = os.lstat(os.path.join(album, name))
        listed = os.lstat(os.path.join(album, found[0]))
    except OSError:
        return None
    if (given.st_dev, given.st_ino) != (listed.st_dev, listed.st_ino):
        return None
    return found[0]


def carry_backup_companions(
        backup,
        replacement_path,
        *,
        expected_replacement_receipt=None,
        expected_owner=None,
        skip_conflicts=False,
        renamed=None,
):
    """Copy the backup's non-audio files the album lacks into it, replacing
    nothing.

    ``backup`` must be a complete ``BackupResult``, and the expected
    replacement receipt the album's as it was before this call: when either
    changed since, nothing is copied. A companion the album holds with the
    same bytes counts as carried, so running it again after a hard stop
    finishes the job; the partial copies that stop left in the album are
    removed first. A different file or a folder at a companion's name, or a
    file where one of its folders goes, is left untouched and keeps the
    backup: it returns ``None`` unless ``skip_conflicts`` is set, in which
    case the other companions are still carried; ``companion_conflicts``
    names what stayed behind. ``renamed`` maps a companion to the name it
    takes in the album, such as the lyrics of a track that has another name
    there. On success, returns the album's new receipt for the later disposal
    check. Any doubt returns ``None`` and leaves the backup.
    """
    if not _backup_owner_authorized(backup, expected_owner):
        return None
    opened = _validated_backup_result(backup, require_complete=True)
    if opened is None:
        return None
    backup_fd = opened[-1][-1]
    try:
        album = _library_path(replacement_path)
        expected = _canonical_receipt(expected_replacement_receipt)
        if (
            album is None
            or expected is None
            or _same_entry(os.fstat(backup_fd), os.lstat(album))
            or capture_album_tree_identity(album)
                != album_tree_identity(expected)
        ):
            return None
        _remove_partial_copies(album)
        replacement_files = {
            relative: value
            for relative, value in expected["tree"]["files"].items()
            if not _COPY_TEMP.fullmatch(PurePosixPath(relative).name)
        }
        backup_files = backup.receipt["tree"]["files"]
        companions = [
            relative
            for relative in backup_files
            if (
                PurePosixPath(relative).name not in _SIDECARS
                and Path(PurePosixPath(relative).name).suffix.lower()
                    not in cfg.AUDIO_EXTS
            )
        ]
        targets = {relative: (renamed or {}).get(relative, relative)
                   for relative in companions}
        missing_files = []
        for relative_text in sorted(companions):
            target_text = targets[relative_text]
            existing = replacement_files.get(target_text)
            if existing is None and (alias := _case_alias(
                    album, replacement_files, target_text)):
                targets[relative_text] = alias
                existing = replacement_files[alias]
            if existing is not None:
                if existing["sha256"] != backup_files[relative_text]["sha256"]:
                    if skip_conflicts:
                        continue
                    return None
                continue
            if target_text in expected["tree"]["directories"]:
                if skip_conflicts:
                    continue
                return None
            missing_files.append(relative_text)

        carried_files = []
        for relative_text in missing_files:
            parts = _companion_relative_parts(targets[relative_text])
            parents = ["/".join(parts[:depth])
                       for depth in range(1, len(parts))]
            if any(parent in replacement_files for parent in parents):
                if skip_conflicts:
                    continue
                return None
            carried_files.append(relative_text)

        for relative_text in carried_files:
            target = _library_path(
                album / targets[relative_text], create_parents=True)
            if target is None:
                return None
            copy_file(backup.path / relative_text, target,
                      sha256=backup_files[relative_text]["sha256"])
        result = capture_album_source_receipt(album)
        if result is None:
            return None
        tree = result["tree"]
        files = dict(tree["files"])
        carried = {targets[relative]: files.pop(targets[relative], {}).get(
                       "sha256")
                   for relative in carried_files}
        if (
            files != replacement_files
            or carried != {targets[relative]: backup_files[relative]["sha256"]
                           for relative in carried_files}
            or tree["root_identity"] != expected["tree"]["root_identity"]
            or any(tree["directories"].get(relative) != value
                   for relative, value
                   in expected["tree"]["directories"].items())
        ):
            return None
        _remove_earlier_carry_folder(backup_fd, backup.receipt, album, result)
        return result
    except (OSError, TypeError, ValueError, KeyError):
        return None
    finally:
        _close_descriptors(opened[-1])


def companion_conflicts(backup, replacement_receipt, renamed=None):
    """The backup's non-audio files that the album holds different bytes for,
    under the name ``renamed`` gives them there.

    After a carry with ``skip_conflicts`` these exist only in the backup.
    """
    try:
        backup_files = backup.receipt["tree"]["files"]
        album_files = replacement_receipt["tree"]["files"]
        album = replacement_receipt["origin"]

        def held(relative):
            name = (renamed or {}).get(relative, relative)
            if name not in album_files:
                name = _case_alias(album, album_files, name) or name
            return album_files.get(name) or {}

        return sorted(
            relative for relative, expected in backup_files.items()
            if PurePosixPath(relative).name not in _SIDECARS
            and Path(PurePosixPath(relative).name).suffix.lower()
                not in cfg.AUDIO_EXTS
            and held(relative).get("sha256") != expected["sha256"]
        )
    except (AttributeError, KeyError, TypeError):
        return None


def _write_backup_origin(bp: Path, origin: Path) -> bool:
    """Write the protective origin sidecar. Returns True only if it's actually
    on disk afterwards - the sidecar is the sole signal that keeps the age sweep
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
    _SET_ASIDE_SENTINEL,
    _COMPANION_CARRY_INTENT_SENTINEL,
    _COMPANION_CARRY_READY_SENTINEL,
    _COMPANION_CARRY_COMMITTED_SENTINEL,
)


def backup_keep_markers_present(bp) -> bool:
    """True when an explicit keep pin protects this backup from the age sweep."""
    for marker in _KEEP_MARKERS:
        try:
            if stat.S_ISREG((Path(bp) / marker).stat().st_mode):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _backup_safe_to_reap(bp: Path) -> bool:
    """False when a keep pin holds the backup or its origin is unrecorded."""
    return (not backup_keep_markers_present(bp)
            and _read_backup_origin(bp) is not None)


def _kept_for_lyrics(refused):
    """Why the age sweep keeps a backup whose tracks alone have their lyrics,
    from the checks in ``refused``, or None."""
    if not refused or refused[-1].reason != "lyrics":
        return None
    return (f"the lyrics of {refused[-1].file} are only in it, and removing "
            "it in Diagnostics copies them onto the album first")


def release_backup_owner(backup: BackupResult, *, expected_owner) -> bool:
    """Hand a queue-owned backup over as an ordinary kept one.

    Settings restores and removes only backups no download owns, so a
    download that is cleared while its backup stays lets go of it here. The
    receipt keeps its token, so its markers, such as the keep pin, still
    belong to it.
    """
    if (
        expected_owner is None
        or not _backup_owner_authorized(backup, expected_owner)
    ):
        return False
    opened = _validated_backup_result(backup)
    if opened is None:
        return False
    _public, _root, _parts, descriptors = opened
    directory_fd = descriptors[-1]
    temporary = f".ql-receipt-{secrets.token_hex(12)}"
    descriptor = None
    try:
        current = _read_backup_receipt(directory_fd)
        if current is None or current.get("version") != _OWNED_RECEIPT_VERSION:
            return False
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        released = {
            key: value for key, value in current.items() if key != "owner"
        }
        released["version"] = _RECEIPT_VERSION
        released["receipt_identity"] = list(
            _entry_identity(os.fstat(descriptor)))
        data = json.dumps(
            released, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                return False
            offset += written
        os.fsync(descriptor)
        os.replace(
            temporary, _RECEIPT_SIDECAR,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
        if not _fsync_directory_fds(directory_fd):
            return False
        reread = _read_backup_receipt(directory_fd)
        return (
            reread is not None
            and reread.get("version") == _RECEIPT_VERSION
            and "owner" not in reread
            and reread.get("token") == current.get("token")
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        _close_descriptors(descriptors)


def pin_unverified_upgrade_backup(backup: BackupResult,
                                  note: str | None = None, *,
                                  expected_owner=None) -> bool:
    """Mark a backup the redundancy proof cannot protect: never reap it."""
    if not _backup_owner_authorized(backup, expected_owner):
        return False
    return _write_receipt_marker(
        backup,
        _UNVERIFIED_UPGRADE_SENTINEL,
        note or "upgrade kept - replacement not verified complete; "
                "the only full copy",
    )


def warn_pin_failed(bp: Path) -> None:
    """One shared warning for a failed pin: every caller just decided the
    backup holds a sole copy, so the message has to say the auto-clean
    protection did NOT take."""
    log.info(fmt(C.RED,
        f"  ✗  Couldn't write the keep marker on this backup - the "
        f"scheduled auto-clean may treat it as redundant and remove it.\n"
        f"     Copy what you need out of {bp} now."))


# Diagnostic results are advisory. The budget reserves expected hash reads,
# including receipt validation; metadata walks still cover every backup.
# Disposal never uses this cache or budget. Settings lists the backups on
# every view, so the budget stays small: content is read to prove it before
# a deletion, not to fill in a row.
_ONLY_COPY_TTL_SEC = 10.0
_BACKUP_LISTING_READ_BYTES = 8 * 1024 * 1024
_only_copy_cache: tuple[float, tuple, list] | None = None
_only_copy_lock = threading.Lock()
# What the last Remove found for each backup, with the files it looked at,
# so a row the listing can't check fully says why Remove refused it.
_removal_refusals: dict[Path, tuple[list, "BackupClassification"]] = {}


def _files_signature(*roots):
    """Each file's name, inode, size and times, or None when one can't be
    read."""
    signature = []
    try:
        for root in roots:
            entries = _list_tree(root) if root is not None else None
            if entries is None:
                return None
            for entry in entries:
                info = entry.lstat()
                signature.append((os.fspath(entry), info.st_ino, info.st_size,
                                  info.st_mtime_ns, info.st_ctime_ns))
    except OSError:
        return None
    return signature


def awaiting_retention(path: Path) -> bool:
    """Whether the age sweep will settle this backup on schedule.

    Inside the retention window, unpinned, and every file it holds back at its
    origin at least as large: an upgrade's own backup looks like this for its
    whole window. A consolidation's backup qualifies while the album it kept
    still holds the track standing in for each file. The sweep's proof decides
    it later; this reads metadata only.
    """
    m = re.match(r"^(\d{8}_\d{6})_", path.name)
    if m is None or backup_keep_markers_present(path):
        return False
    try:
        made = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return False
    if made < time.time() - cfg.UPGRADE_BACKUP_RETENTION_DAYS * 86400:
        return False
    if (path / _CONSOLIDATED_SENTINEL).is_file():
        named = consolidated_into(path)
        return named is not None and all(
            (named[0] / track).is_file() for track in named[1].values())
    origin = _read_backup_origin(path)
    entries = _list_tree(path)
    if origin is None or entries is None:
        return False
    try:
        for source in entries:
            info = source.stat()
            if not stat.S_ISREG(info.st_mode) or source.name in _SIDECARS:
                continue
            destination = origin / source.relative_to(path)
            if (not destination.is_file()
                    or destination.stat().st_size < info.st_size):
                return False
    except (OSError, ValueError):
        return False
    return True


def list_retained_backups():
    """Advisory removal classifications, including unverified and removable."""
    global _only_copy_cache
    if not cfg.UPGRADE_BACKUP_DIR.exists():
        with _only_copy_lock:
            _only_copy_cache = None
        return []
    try:
        dir_mtime = cfg.UPGRADE_BACKUP_DIR.stat().st_mtime_ns
    except OSError:
        dir_mtime = 0.0
    now = time.time()
    cache_key = (os.fspath(cfg.UPGRADE_BACKUP_DIR), dir_mtime)
    with _only_copy_lock:
        cached = _only_copy_cache
        if (cached is not None and cached[1] == cache_key
                and now - cached[0] < _ONLY_COPY_TTL_SEC):
            return cached[2]

        out = []
        budget = _BackupReadBudget(_BACKUP_LISTING_READ_BYTES)
        try:
            for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
                if not entry.is_dir():
                    continue
                if is_disposal_leftover(entry):
                    out.append((
                        entry,
                        _leftover_origin(entry),
                        BackupClassification("retained", "interrupted"),
                    ))
                    continue
                # A deliberate undo copy (downsample originals): its origin
                # holds the smaller rewrite on purpose, so it always looks
                # "not redundant" here - but it isn't orphaned, and the
                # diagnostics list it feeds shows it separately.
                if (entry / _REAP_AFTER_RETENTION_SENTINEL).is_file():
                    continue
                # An unfinished copy has no record to restore from; the
                # diagnostics list counts it with the other leftovers.
                if entry.name.startswith(_PARTIAL_COPY_PREFIX):
                    continue
                origin = (_read_backup_origin(entry)
                          or _intended_origin(entry))
                result = _classify_retained_backup(entry, origin, budget)
                remembered = _removal_refusals.get(entry)
                if (
                    result.status == "unverified"
                    and remembered is not None
                    and remembered[0] is not None
                    and remembered[0] == _files_signature(entry, origin)
                ):
                    result = remembered[1]
                out.append((entry, origin, result))
        except OSError:
            pass
        _only_copy_cache = (now, cache_key, out)
        return out


def _source_receipt_shape_is_valid(receipt, origin=None) -> bool:
    """Structural gate for the pre-retirement album authority in a backup."""
    return _source_receipt_nested_schema_valid(receipt, origin)


def _fidelity_without(value, *names):
    return {key: item for key, item in value.items() if key not in names}


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


def _complete_upgrade_backup(backup, *, set_aside=False):
    if (
        not isinstance(backup, BackupResult)
        or backup.complete
        or backup.receipt is None
        or backup.receipt.get("kind") != "upgrade"
        or backup.receipt.get("complete") is not False
    ):
        return None
    if set_aside and not mark_set_aside_replacement(backup):
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


def _write_backup_intent(backup_root_fd, backup_name, source_receipt) -> bool:
    source_receipt = _canonical_receipt(source_receipt)
    if source_receipt is None:
        return False
    return _write_text_noreplace_at(
        backup_root_fd,
        _BACKUP_INTENT_PREFIX + backup_name,
        json.dumps({
            "version": 1,
            "backup": backup_name,
            "source_receipt": source_receipt,
        }, sort_keys=True, separators=(",", ":")),
    )


def _read_backup_intent(backup_root_fd, name):
    """(backup name, source receipt) from one intent, or None."""
    try:
        raw = _read_exact_text_at(
            backup_root_fd, name, max_bytes=_RECORD_READ_BYTES)
        intent = json.loads(raw) if raw is not None else None
        source_receipt = intent["source_receipt"]
        if (
            intent["version"] != 1
            or name != _BACKUP_INTENT_PREFIX + intent["backup"]
            or not _source_receipt_shape_is_valid(source_receipt)
        ):
            return None
    except (OSError, TypeError, ValueError, KeyError):
        return None
    return intent["backup"], source_receipt


def _drop_backup_intent(backup_root_fd, name) -> None:
    try:
        os.unlink(name, dir_fd=backup_root_fd)
        _fsync_directory_fds(backup_root_fd)
    except OSError:
        pass


def _write_put_back_note(path: Path, origin: Path):
    """The name of the note that the backup at ``path`` is being renamed
    home to ``origin``, or None when it could not be written."""
    name = _PUT_BACK_PREFIX + secrets.token_hex(8)
    try:
        root_fd = _open_backup_directory(path.parent)
        try:
            folder_fd = _open_backup_directory(path.name, dir_fd=root_fd)
            try:
                folder = _path_directory_generations([folder_fd])[0]
            finally:
                os.close(folder_fd)
            written = _write_text_noreplace_at(
                root_fd,
                name,
                json.dumps({
                    "version": 1,
                    "backup": path.name,
                    "origin": os.fspath(origin),
                    "folder": folder,
                }, sort_keys=True, separators=(",", ":")),
            )
        finally:
            os.close(root_fd)
    except (OSError, TypeError, ValueError):
        return None
    return name if written else None


def _drop_put_back_note(name) -> None:
    try:
        os.unlink(cfg.UPGRADE_BACKUP_DIR / name)
    except OSError:
        return
    fsync_dir(cfg.UPGRADE_BACKUP_DIR)


def _remove_put_back_sidecars(album: Path) -> bool:
    """Remove the backup's own files from an album renamed back home, and
    say whether none is left."""
    for sidecar in _SIDECARS:
        try:
            value = os.lstat(album / sidecar)
        except OSError:
            continue
        if stat.S_ISREG(value.st_mode):
            delete_verified(album / sidecar, stat_identity(value))
    return not any(os.path.lexists(album / sidecar) for sidecar in _SIDECARS)


def _folder_incarnation(path):
    """The folder at ``path`` as inode and birth time, or None."""
    try:
        descriptor = _open_backup_directory(path)
    except OSError:
        return None
    try:
        return _incarnations(_path_directory_generations([descriptor]))
    except (OSError, TypeError, ValueError, IndexError):
        return None
    finally:
        os.close(descriptor)


def _finish_put_back(note_name) -> None:
    """Remove the backup's files from an album a hard stop caught between its
    rename home and their removal. Only the exact folder that was renamed is
    touched, known by its inode and birth time."""
    try:
        note = _read_small_json(cfg.UPGRADE_BACKUP_DIR / note_name)
        backup_name, origin = note["backup"], Path(note["origin"])
        folder = note["folder"]
        if note["version"] != 1 or "/" in backup_name or not origin.is_absolute():
            return
    except (TypeError, ValueError, KeyError):
        return
    if os.path.lexists(cfg.UPGRADE_BACKUP_DIR / backup_name):
        # The rename never happened, or was undone; the backup stays a backup.
        _drop_put_back_note(note_name)
        return
    if _library_path(origin) is None or not os.path.lexists(origin):
        if not os.path.lexists(origin) and origin.parent.is_dir():
            _drop_put_back_note(note_name)
        return
    try:
        recorded = _incarnations([folder])
    except (TypeError, IndexError):
        return
    current = _folder_incarnation(origin)
    if current is None:
        return
    if current != recorded:
        _drop_put_back_note(note_name)
        return
    # The album folder the restore had moved aside, which a stop left.
    aside = _restore_folders(backup_name, origin)[1]
    album = _plain_tree(origin)
    replaced = (_replaceable_partial(aside, _tree_bytes(album))
                if album is not None and os.path.lexists(aside) else None)
    if replaced is not None:
        delete_tree_verified(aside, replaced)
    if _remove_put_back_sidecars(origin) and not os.path.lexists(aside):
        _drop_put_back_note(note_name)


def _unwritable_folder(album: Path):
    """The first folder that removing ``album`` needs to change and the app
    may not write to: its parent, itself or a folder in it."""
    folders = [album.parent, album]
    folders += sorted(path for path in iter_tree_no_symlinks(album, errors=[])
                      if path.is_dir() and not path.is_symlink())
    for folder in folders:
        if not os.access(folder, os.W_OK | os.X_OK):
            return folder
    return None


def first_symlink(folder) -> str | None:
    """The first symbolic link below ``folder``, relative to it.

    A backup copies and seals only real files and folders, so one link, such
    as folder.jpg pointing at cover.jpg, keeps the whole album out of it.
    """
    root = Path(folder)
    for path in sorted(iter_tree_no_symlinks(root, errors=[])):
        if path.is_symlink():
            return path.relative_to(root).as_posix()
    return None


def backup_album_dir(album_dir: Path, *, expected_receipt=None, owner=None,
                     on_intent=None, set_aside=False):
    """Move album_dir into a timestamped backup sealed by its receipt.

    On the same filesystem the folder is renamed into the backup folder.
    Otherwise, or when a file in it has another hard link, every file is
    copied to a hidden folder there and checked against the receipt, which
    is sealed and renamed to the backup's name; then the album is deleted
    file by file while each is the one copied.

    Returns a complete ``BackupResult`` on success, an incomplete one when
    the backup holds the album, or part of it, that could not be finished,
    and ``None`` when the album was left as it was. With ``set_aside`` the
    backup is marked as a download an upgrade set aside, so it is never
    taken for the album.
    """
    owner = normalise_recovery_owner(owner)
    if (owner is None) != (on_intent is None):
        raise ValueError("owned backups require an intent checkpoint")
    if on_intent is not None and not callable(on_intent):
        raise ValueError("backup intent checkpoint must be callable")

    public = _library_path(album_dir)
    try:
        real_folder = public is not None and stat.S_ISDIR(
            os.lstat(public).st_mode)
    except OSError:
        real_folder = False
    if not real_folder:
        log.info(fmt(
            C.RED,
            f"  ✗  Refusing to back up an album outside the real, no-follow "
            f"music tree: {album_dir}."))
        return None
    snapshot = snapshot_tree(public)
    source_receipt = (capture_album_source_receipt(public)
                      if snapshot is not None else None)
    if source_receipt is None:
        link = first_symlink(public)
        if link:
            reason = (f"{link} is a symbolic link; replace it with a copy of "
                      "its file")
        elif any(os.path.basename(relative) in _SIDECARS
                 for relative in snapshot or ()):
            reason = "it contains a reserved backup metadata name"
        else:
            reason = "couldn't seal its exact tree"
        log.info(fmt(
            C.RED,
            f"  ✗  Couldn't back up {public}: {reason}. The album was left "
            "in place."))
        return None
    if expected_receipt is not None and expected_receipt not in (
            source_receipt, album_tree_identity(source_receipt)):
        log.info(fmt(
            C.RED,
            f"  ✗  {public} changed after it was selected; leaving "
            "the current tree untouched."))
        return None
    try:
        bp = _upgrade_backup_path_for(public)
        root_fd = _open_backup_directory(bp.parent)
    except OSError as exc:
        log.info(fmt(C.RED, f"  ✗  Couldn't prepare backup path: {exc}."))
        return None
    try:
        folder = _unwritable_folder(public)
        if folder is None and not os.access(bp.parent, os.W_OK | os.X_OK):
            folder = bp.parent
        if folder is not None:
            log.info(fmt(
                C.RED,
                f"  ✗  Couldn't back up {public}: the app can't write to "
                f"{folder}. It was left in place."))
            return None
        try:
            require_noreplace_rename(public.parent, bp.parent)
        except OSError as exc:
            log.info(fmt(
                C.RED,
                f"  ✗  Couldn't back up {public}: {exc.strerror or exc}. It "
                "was left in place."))
            return None
        intent = None
        if owner is not None:
            on_intent({
                "version": 1,
                "kind": "upgrade",
                "owner": dict(owner),
                "path": os.fspath(bp),
                "origin": os.fspath(public),
                "source_receipt": _canonical_receipt(source_receipt),
            })
        elif _write_backup_intent(root_fd, bp.name, source_receipt):
            intent = _BACKUP_INTENT_PREFIX + bp.name
        else:
            log.info(fmt(
                C.RED,
                f"  ✗  Couldn't record the backup of {public}; leaving it "
                "in place."))
            return None
        moved = False
        if _tree_has_multiply_linked_file(source_receipt["fidelity"]):
            why = "its files have other hard links"
        elif not _same_filesystem(public, bp.parent):
            why = "the backup folder is on another filesystem"
        else:
            # Docker mounts the library and the backup folder separately,
            # and a move between two mounts fails even on one disk.
            why = "the backup folder is mounted separately from the library"
            try:
                _rename_noreplace_at(AT_FDCWD, public, AT_FDCWD, bp)
                moved = True
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    log.info(fmt(
                        C.RED,
                        f"  ✗  Could not back up {public}: "
                        f"{exc}.{errors_mod.oserr_hint(exc)}"))
                    if intent is not None:
                        _drop_backup_intent(root_fd, intent)
                    return None
        if moved:
            result = _seal_moved_album(
                public, bp, source_receipt, owner=owner, set_aside=set_aside)
        else:
            result = _copy_album_to_backup(
                public, bp, snapshot, source_receipt, why, owner=owner,
                set_aside=set_aside)
        # Left behind, the intent puts the album back at the next start.
        if intent is not None and (result is None or result.receipt):
            _drop_backup_intent(root_fd, intent)
        return result
    finally:
        os.close(root_fd)


def _rename_backup_home(path: Path, origin: Path, own=(
        _RECEIPT_SIDECAR, _ORIGIN_SIDECAR)) -> bool:
    """Take the backup's ``own`` files out of the album folder renamed to
    ``path`` and rename it back to ``origin``; whether it is there."""
    try:
        if not all(_remove_own_file(path / name) for name in own):
            return False
        _rename_noreplace_at(AT_FDCWD, path, AT_FDCWD, origin)
        fsync_dir(path.parent)
        fsync_dir(origin.parent)
    except OSError:
        return False
    return True


def _seal_moved_album(public, bp, source_receipt, *, owner, set_aside):
    """Seal the album folder just renamed from ``public`` to ``bp``. One
    that changed, or could not be sealed, is renamed back."""
    files = len(source_receipt["tree"]["files"])
    wrote = False
    try:
        if _moved_album(bp, source_receipt) is not True:
            raise OSError(errno.EAGAIN, "it changed while it was moved")
        fsync_dir(public.parent, strict=True)
        fsync_dir(bp.parent, strict=True)
        folder_fd = _open_backup_directory(bp)
        try:
            wrote = True
            if not _write_backup_origin_durable(folder_fd, public):
                raise OSError(errno.EIO, "its origin could not be recorded")
            result = _seal_backup_result(
                bp, folder_fd, public, kind="upgrade", complete=False,
                requested=files, backed_up=files,
                source_receipt=source_receipt, owner=owner)
        finally:
            os.close(folder_fd)
        if result.receipt is None:
            raise OSError(errno.EIO, "its backup record could not be written")
        completed = _complete_upgrade_backup(result, set_aside=set_aside)
        if completed is None:
            raise OSError(errno.EIO, "its backup could not be marked finished")
        return completed
    except BaseException as exc:
        home = _rename_backup_home(bp, public, _SIDECARS if wrote else ())
        if isinstance(exc, OSError):
            log.info(fmt(
                C.RED,
                f"  ✗  Could not back up {public}: {exc.strerror or exc}."
                + (" It was left in place." if home else "")))
        if not home:
            log.info(fmt(
                C.RED,
                f"  ✗  {public} could not be moved back from its backup at "
                f"{bp}."))
        if not isinstance(exc, OSError):
            raise
        if home:
            return None
        return load_backup_result(bp) or BackupResult(
            bp, complete=False, receipt=None, requested=files,
            backed_up=files)


def _copy_album_to_backup(public, bp, snapshot, source_receipt, why, *,
                          owner, set_aside):
    """Copy the album at ``public`` to the backup ``bp`` and delete it from
    the library while it is the album ``snapshot`` and the receipt hold.
    ``why`` says in the log why it is not a move."""
    payload = backup_payload(source_receipt)
    partial = bp.parent / (_PARTIAL_COPY_PREFIX + bp.name)
    log.info(fmt(
        C.GRAY,
        f"  ⤷  Copying {errors_mod.plural(len(payload), 'file')} / "
        f"{sum(size for size, _digest in payload.values()) / 1024 / 1024:.1f}"
        f" MB to {bp.parent} instead of moving them, since {why}…"))
    if os.path.lexists(partial):
        log.info(fmt(C.RED, f"  ✗  {partial} is in the way; "
                     "leaving the album in place."))
        return None
    published = False
    try:
        _copy_backup_into(public, partial, payload,
                          source_receipt["tree"]["directories"])
        folder_fd = _open_backup_directory(partial)
        try:
            if not _write_backup_origin_durable(folder_fd, public):
                raise OSError(errno.EIO, "its origin could not be recorded")
            result = _seal_backup_result(
                bp, folder_fd, public, kind="upgrade", complete=False,
                requested=len(payload), backed_up=len(payload),
                source_receipt=source_receipt, owner=owner, folder=partial)
        finally:
            os.close(folder_fd)
        if result.receipt is None:
            raise OSError(errno.EIO, "its backup record could not be written")
        if backup_payload(result.receipt) != payload:
            raise OSError(errno.EIO, "its copy differs from it")
        if snapshot_tree(public) != snapshot:
            raise OSError(errno.EAGAIN, "it changed while it was copied")
        _rename_noreplace_at(AT_FDCWD, partial, AT_FDCWD, bp)
        published = True
        fsync_dir(bp.parent, strict=True)
    except BaseException as exc:
        left = []
        if not published and os.path.lexists(partial):
            left = delete_tree_verified(partial, snapshot_tree(partial) or {})
        if not isinstance(exc, OSError):
            log.info(fmt(
                C.YELLOW,
                f"\n  ⚠  Backup interrupted. {public} is as it "
                "was" + (f"; what is left of its copy is at {partial}."
                         if left else f" and its copy is at {bp}."
                         if published else " and its copy was removed.")))
            raise
        log.info(fmt(
            C.RED,
            f"  ✗  Could not back up {public}: {exc.strerror or exc}. It "
            "was left in place"
            + (f"; what is left of its copy is at {partial}." if left else
               "." if not published else
               f", and its copy is at {bp}.")))
        return result if published else None

    if snapshot_tree(public) != snapshot:
        log.info(fmt(
            C.RED,
            f"  ✗  {public} changed while it was copied to its backup at "
            f"{bp}; nothing was removed from it."))
        return result
    folder = _unwritable_folder(public)
    if folder is not None:
        log.info(fmt(
            C.RED,
            f"  ✗  {public} was copied to the backup but could not be "
            f"removed: the app can't write to {folder}."))
        return result
    try:
        left = delete_tree_verified(public, snapshot)
    except BaseException:
        log.info(fmt(
            C.YELLOW,
            f"     The album is safe in the backup at {bp}. The next run "
            "puts back what was removed unless the folder has changed "
            "since, and then the backup is kept."))
        raise
    if left:
        log.info(fmt(
            C.RED,
            f"  ✗  {public} changed while it was being removed after its "
            f"copy to {bp}; what is left of it stays."))
        return result
    completed = _complete_upgrade_backup(result, set_aside=set_aside)
    if completed is None:
        log.info(fmt(
            C.RED,
            f"  ✗  {public} was moved to its backup at {bp}, but the backup "
            "could not be marked finished."))
        return result
    return completed


def backup_gap_fill_files(file_paths, album_dir: Path, *,
                          expected_receipts=None, owner=None,
                          on_intent=None, kind="gapfill"):
    """Set the album's files ``file_paths`` aside in a backup sealed by its
    receipt, before a refill replaces them.

    Each file is copied into a hidden folder in the backup folder and
    checked. The receipt is written, the folder renamed to the backup's
    name, and then each track is deleted from the album while it is the
    file copied. A track that changed by then stops it: what was deleted is
    put back, and the album is left as it was.

    Returns a complete ``BackupResult``, or ``None`` when the tracks were
    left in the album or put back there; one that could not be put back
    stays in the backup, which is then kept. ``expected_receipts`` must
    match each file exactly.
    """
    owner = normalise_recovery_owner(owner)
    if (owner is None) != (on_intent is None):
        raise ValueError("owned backups require an intent checkpoint")
    if on_intent is not None and not callable(on_intent):
        raise ValueError("backup intent checkpoint must be callable")

    what = {"repair": "", "consolidate": "consolidation "}.get(kind, "gap-fill ")
    file_paths = list(file_paths)
    if not file_paths:
        return None
    expected = None
    if expected_receipts is not None:
        try:
            expected = _normalise_gap_fill_expected_receipts(
                expected_receipts, allow_identity=True)
        except OSError as exc:
            log.info(fmt(
                C.RED,
                f"  ✗  Refusing malformed {what}source receipts "
                f"({exc}); leaving every source file in place."))
            return None
    public = _library_path(album_dir)
    try:
        real_folder = public is not None and stat.S_ISDIR(
            os.lstat(public).st_mode)
    except OSError:
        real_folder = False
    if not real_folder:
        log.info(fmt(
            C.RED,
            f"  ✗  Refusing to back up files outside the real, no-follow "
            f"music tree: {album_dir}."))
        return None

    sources = {}
    for raw_path in file_paths:
        rooted = _rooted_path_parts(public, Path(raw_path))
        if (
            rooted is None
            or rooted[2][-1] in _SIDECARS
            or _library_path(rooted[0]) is None
        ):
            log.info(fmt(
                C.RED,
                f"  ✗  Refusing unsafe {what}source {raw_path}; "
                "leaving every source file in place."))
            return None
        path, _album, parts = rooted
        relative = "/".join(parts)
        if relative in sources:
            log.info(fmt(
                C.RED,
                f"  ✗  A {what}source path is duplicated; leaving "
                "every source file in place."))
            return None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                                 | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                value = os.fstat(descriptor)
                receipt = _gap_fill_file_receipt(descriptor)
            finally:
                os.close(descriptor)
            if expected is not None and (
                relative not in expected
                or expected[relative] != {
                    field: receipt[field] for field in expected[relative]}
            ):
                raise OSError("verified source receipt changed")
            if stat_identity(os.lstat(path)) != stat_identity(value):
                raise OSError("it changed while it was read")
        except FileNotFoundError:
            log.info(fmt(
                C.RED,
                f"  ✗  {parts[-1]} disappeared after it was checked; "
                "leaving every source file in place."))
            return None
        except OSError as exc:
            log.info(fmt(
                C.RED,
                f"  ✗  Couldn't bind {parts[-1]} for an exact backup "
                f"({exc}); leaving every source file in place."))
            return None
        sources[relative] = (path, value, receipt)
    if expected is not None and set(sources) != set(expected):
        log.info(fmt(
            C.RED,
            f"  ✗  The {what}receipts do not cover exactly the requested "
            "files; leaving every source file in place."))
        return None
    folder = next((path.parent for path, _value, _receipt in sources.values()
                   if not os.access(path.parent, os.W_OK | os.X_OK)), None)
    if folder is not None:
        log.info(fmt(
            C.RED,
            f"  ✗  Couldn't set aside the {what}tracks of {public}: the app "
            f"can't write to {folder}. Every track was left in place."))
        return None

    try:
        cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_root = Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))
        if not stat.S_ISDIR(os.lstat(backup_root).st_mode):
            raise OSError(errno.ENOTDIR, "not a folder")
    except OSError as exc:
        log.info(fmt(
            C.YELLOW,
            f"  ⚠  Couldn't prepare a protected {what}backup "
            f"({exc}); leaving every source file in place."))
        return None
    try:
        require_noreplace_rename(public, backup_root)
    except OSError as exc:
        log.info(fmt(
            C.RED,
            f"  ✗  Couldn't set aside the {what}tracks of {public}: "
            f"{exc.strerror or exc}. Every track was left in place."))
        return None
    bp = backup_root / _backup_dir_name(
        public, kind="repair" if kind == "repair" else "gapfill")
    partial = backup_root / (_PARTIAL_COPY_PREFIX + bp.name)
    if os.path.lexists(partial):
        log.info(fmt(C.RED, f"  ✗  {partial} is in the way; leaving every "
                     "source file in place."))
        return None
    if owner is not None:
        on_intent({
            "version": 1,
            "kind": "gap-fill",
            "owner": dict(owner),
            "path": os.fspath(bp),
            "origin": os.fspath(public),
            "source_receipts": {
                relative: dict(receipt)
                for relative, (_path, _value, receipt) in sources.items()
            },
        })
    payload = {
        relative: (receipt["size"], receipt["sha256"])
        for relative, (_path, _value, receipt) in sources.items()
    }
    published = False
    try:
        os.mkdir(partial, 0o700)
        # Written first, so the next start finds the album of a copy a stop
        # cut short.
        if not _write_backup_origin(partial, public):
            raise OSError(errno.EIO, "its origin could not be recorded")
        for relative, (path, _value, receipt) in sorted(sources.items()):
            target = partial / relative
            os.makedirs(target.parent, exist_ok=True)
            copy_file(path, target, sha256=receipt["sha256"])
        folder_fd = _open_backup_directory(partial)
        try:
            result = _seal_backup_result(
                bp, folder_fd, public, kind="gap-fill", complete=True,
                requested=len(sources), backed_up=len(sources), owner=owner,
                folder=partial)
        finally:
            os.close(folder_fd)
        if result.receipt is None:
            raise OSError(errno.EIO, "its backup record could not be written")
        if backup_payload(result.receipt) != payload:
            raise OSError(errno.EAGAIN, "a track changed while it was copied")
        for relative, (path, value, _receipt) in sources.items():
            if stat_identity(os.lstat(path)) != stat_identity(value):
                raise OSError(errno.EAGAIN,
                              f"{PurePosixPath(relative).name} changed "
                              "while it was copied")
        _rename_noreplace_at(AT_FDCWD, partial, AT_FDCWD, bp)
        published = True
        fsync_dir(backup_root, strict=True)
    except BaseException as exc:
        left = []
        if not published and os.path.lexists(partial):
            left = delete_tree_verified(partial, snapshot_tree(partial) or {})
        where = (f"; what is left of its copy is at {partial}" if left
                 else f", and its copy is at {bp}" if published else "")
        if not isinstance(exc, OSError):
            log.info(fmt(
                C.YELLOW,
                f"\n  ⚠  {(what + 'backup').capitalize()} interrupted. The album's "
                f"tracks were left in place{where}."))
            raise
        log.info(fmt(
            C.RED,
            f"  ✗  Couldn't back up the {what}tracks of {public}: "
            f"{exc.strerror or exc}. Every track was left in place{where}."))
        return None

    try:
        refused = next((
            relative for relative, (path, value, _receipt) in sources.items()
            if not delete_verified(path, stat_identity(value))), None)
    except BaseException:
        log.info(fmt(
            C.YELLOW,
            f"\n  ⚠  {(what + 'backup').capitalize()} interrupted. The tracks are "
            f"safe in the backup at {bp}; Restore in Diagnostics puts back "
            "any the album is missing."))
        raise
    if refused is None:
        return result
    log.info(fmt(
        C.RED,
        f"  ✗  {PurePosixPath(refused).name} changed, or could not be "
        "removed, after it was copied to the backup; putting back the "
        f"{what}tracks already removed."))
    restore_gap_fill_backup(
        result, public, keep_larger_dst=False, replace_dst=False,
        expected_owner=owner)
    return None


def list_undo_copies():
    """Deliberate undo copies (downsample originals) still inside their
    retention window, as (backup_path, origin, restorable). Shown on the
    diagnostics list - separate from the orphaned-backup alarm, because this
    state is one the user asked for, not a failure. ``restorable`` is whether
    the album still holds the files Downsample wrote."""
    out = []
    if not cfg.UPGRADE_BACKUP_DIR.exists():
        return out
    try:
        for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
            if (entry.is_dir() and not is_disposal_leftover(entry)
                    and (entry / _REAP_AFTER_RETENTION_SENTINEL).is_file()):
                out.append((
                    entry,
                    _read_backup_origin(entry),
                    downsample_outputs_in_place(load_backup_result(entry)),
                ))
    except OSError:
        pass
    return out


def _flac_streaminfo(path: Path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        head = os.pread(descriptor, 42, 0)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(head) < 42 or head[:4] != b"fLaC" or head[4] & 0x7F != 0:
        return None
    return head[8:42].hex()


def record_downsample_outputs(backup) -> bool:
    """Note, beside the kept originals, what the album holds after the
    rewrite, for ``downsample_outputs_in_place`` to compare against."""
    if not isinstance(backup, BackupResult) or backup.receipt is None:
        return False
    origin = Path(backup.receipt["origin"])
    outputs = {}
    for relative in backup_payload(backup.receipt):
        streaminfo = _flac_streaminfo(origin / relative)
        if streaminfo is None:
            return False
        outputs[relative] = streaminfo
    return _write_receipt_marker(
        backup, _DOWNSAMPLE_OUTPUTS_SENTINEL,
        json.dumps(outputs, sort_keys=True))


def _downsample_running_note() -> Path:
    return Path(cfg.DATA_DIR) / ".qobuz_downsample_running"


def note_downsample_running(backup) -> None:
    """Name the undo copy of the album Downsample is about to rewrite, so
    a hard stop before ``record_downsample_outputs`` is finished at the
    next start."""
    try:
        write_atomic(_downsample_running_note(), str(backup.path))
    except OSError as exc:
        vlog(f"couldn't note the running downsample: {exc}")


def record_interrupted_downsample():
    """Record what the album holds for the undo copy of a Downsample that
    did not finish, so Restore can put its originals back. Returns the
    album's folder when that copy holds its originals, else None."""
    try:
        path = Path(_downsample_running_note().read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None
    backup = load_backup_result(path)
    if backup is not None and backup.receipt["kind"] != "downsample":
        backup = None
    if (backup is not None and _downsample_outputs(backup) is None
            and not downsample_left_untouched(backup)):
        record_downsample_outputs(backup)
    try:
        _downsample_running_note().unlink(missing_ok=True)
    except OSError:
        pass
    return None if backup is None else Path(backup.receipt["origin"])


def _downsample_outputs(backup):
    opened = _validated_backup_result(backup, kinds={"downsample"})
    if opened is None:
        return None
    try:
        note = _receipt_marker_note(
            opened[-1][-1], _DOWNSAMPLE_OUTPUTS_SENTINEL, backup.receipt)
    finally:
        _close_descriptors(opened[-1])
    try:
        outputs = json.loads(note) if note is not None else None
    except ValueError:
        return None
    return outputs if isinstance(outputs, dict) else None


def downsample_outputs_in_place(backup) -> bool:
    """Whether the album still holds exactly the audio Downsample left it
    with, or the kept original a stopped put-back already returned. A later
    Upgrade, Repair or Gap Fill replaces those files, and the kept originals
    must not be put back over them. Tag edits keep the STREAMINFO, so
    lyrics and retagging don't count as a change."""
    outputs = _downsample_outputs(backup)
    if outputs is None:
        return False
    origin = Path(backup.receipt["origin"])
    kept = backup_payload(backup.receipt)
    return bool(kept) and all(
        outputs.get(relative) is not None
        and (_flac_streaminfo(origin / relative) == outputs[relative]
             or _matches(origin / relative, *kept[relative]))
        for relative in kept
    )


def restorable_after(backup, newer) -> bool:
    """Whether ``backup`` would pass ``downsample_outputs_in_place`` once the
    newer Downsample copy of the same album, restorable now, is put back."""
    outputs = _downsample_outputs(backup)
    if (outputs is None or not downsample_outputs_in_place(newer)
            or newer.receipt["origin"] != backup.receipt["origin"]):
        return False
    origin = Path(backup.receipt["origin"])
    kept = backup_payload(backup.receipt)
    returned = backup_payload(newer.receipt)

    def after(relative):
        if relative in returned:
            return _flac_streaminfo(newer.path / relative), returned[relative]
        return _flac_streaminfo(origin / relative), None

    def ok(relative):
        streaminfo, payload = after(relative)
        if streaminfo is not None and streaminfo == outputs.get(relative):
            return True
        if payload is not None:
            return tuple(payload) == tuple(kept[relative])
        return _matches(origin / relative, *kept[relative])

    return bool(kept) and all(
        outputs.get(relative) is not None and ok(relative) for relative in kept)


def downsample_left_untouched(backup) -> bool:
    """Whether the album still holds the audio of every kept original, as
    when a Downsample stopped before it rewrote any of them."""
    if not isinstance(backup, BackupResult) or backup.receipt is None:
        return False
    origin = Path(backup.receipt["origin"])
    kept = backup_payload(backup.receipt)
    return bool(kept) and all(
        (streaminfo := _flac_streaminfo(backup.path / relative)) is not None
        and _flac_streaminfo(origin / relative) == streaminfo
        for relative in kept
    )


def stash_downsample_originals(files, album_dir):
    """Copy the hi-res originals about to be rewritten in place into a
    timestamped backup, so the downsample can be undone until the retention
    sweep clears it. Returns
    ``(backup_result_or_None, set_of_files_copied, receipts)``, where
    ``receipts`` maps each copied source path to the regular-file identity,
    size, modification and change times, and digest its copy was checked
    against. The caller can bind that receipt immediately before rewriting
    and refuse a replaced or subsequently changed source.

    Copies, never moves - the originals stay put for the rewrite itself. The
    caller must leave any file NOT in the returned set untouched: the whole
    point of keep-originals is that nothing is rewritten without its copy, so
    a failed copy downgrades that file to "skipped", never to "unprotected".
    Each copy is content-verified (sha256) before it counts, because the undo
    path later trusts these bytes with an overwrite. A stop part way deletes
    the copies made so far."""
    files = [Path(f) for f in files]
    if not files:
        return None, set(), {}
    public = _library_path(album_dir)
    try:
        real_folder = public is not None and stat.S_ISDIR(
            os.lstat(public).st_mode)
    except OSError:
        real_folder = False
    if not real_folder:
        log.info(fmt(
            C.YELLOW,
            "  ⚠  Couldn't bind the album's real no-follow path; leaving "
            "every file untouched."))
        return None, set(), {}
    bp = None
    try:
        cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        bp = Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR))) / (
            _backup_dir_name(public, kind="downsample"))
        os.mkdir(bp)
        fsync_dir(bp.parent, strict=True)
        if not _write_backup_origin(bp, public):
            raise OSError("its origin could not be recorded")
    except OSError as exc:
        log.info(fmt(
            C.YELLOW,
            f"  ⚠  Couldn't create the keep-originals dir ({exc}); "
            "leaving every file untouched."))
        if bp is not None and os.path.lexists(bp):
            delete_tree_verified(bp, snapshot_tree(bp) or {})
        return None, set(), {}

    copied = set()
    receipts = {}
    payload = {}
    try:
        for source_path in files:
            rooted = _rooted_path_parts(public, source_path)
            if (
                rooted is None
                or rooted[2][-1] in _SIDECARS
                or _library_path(rooted[0]) is None
            ):
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  Couldn't safely bind {source_path.name}; it "
                    "will be left untouched."))
                continue
            source = rooted[0]
            target = bp.joinpath(*rooted[2])
            try:
                descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW
                                     | os.O_NONBLOCK | os.O_CLOEXEC)
                try:
                    value = os.fstat(descriptor)
                    receipt = _regular_file_receipt(
                        descriptor, _file_digest_fd(descriptor))
                finally:
                    os.close(descriptor)
                os.makedirs(target.parent, exist_ok=True)
                copy_file(source, target, sha256=receipt["sha256"])
                if stat_identity(os.lstat(source)) != stat_identity(value):
                    delete_verified(target, stat_identity(os.lstat(target)))
                    raise OSError("source changed while its copy was made")
            except OSError as exc:
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  Couldn't keep a copy of {source_path.name} "
                    f"({exc}); it will be left untouched."))
                continue
            copied.add(source_path)
            receipts[source_path] = receipt
            payload["/".join(rooted[2])] = (receipt["size"], receipt["sha256"])
        if not copied:
            delete_tree_verified(bp, snapshot_tree(bp) or {})
            return None, set(), {}
        folder_fd = _open_backup_directory(bp)
        try:
            if not _write_text_noreplace_at(
                folder_fd,
                _REAP_AFTER_RETENTION_SENTINEL,
                "downsample originals - an undo copy; the age sweep clears it "
                "after the retention window",
            ):
                # Without the marker this safely degrades to always-keep.
                vlog("couldn't mark the undo copy age-reapable")
            result = _seal_backup_result(
                bp, folder_fd, public, kind="downsample", complete=True,
                requested=len(copied), backed_up=len(copied))
        finally:
            os.close(folder_fd)
    except BaseException:
        # Only copies were made, and no original has been rewritten yet.
        delete_tree_verified(bp, snapshot_tree(bp) or {})
        raise
    if not result.complete:
        log.info(fmt(
            C.YELLOW,
            "  ⚠  The kept originals could not be verified. They remain "
            f"at {bp}, and no original will be rewritten."))
        return result, set(), {}
    if backup_payload(result.receipt) != payload:
        log.info(fmt(
            C.YELLOW,
            "  ⚠  A kept-original copy changed before its exact receipt "
            f"was bound. Recovery data remains at {bp}, but no source "
            "will be rewritten."))
        return result, set(), {}
    return result, copied, receipts


def restore_gap_fill_backup(backup, album_dir: Path,
                            *, keep_larger_dst: bool = True,
                            replace_dst: bool = True,
                            keep=frozenset(),
                            expected_owner=None) -> int:
    """Put every file of a gap-fill, repair or downsample backup back under
    album_dir at its own relative path, and return how many are there.

    A file the album already holds with the backup's bytes counts as put
    back. keep_larger_dst (default True, the repair caller): a file at the
    destination at least as large as the backup's and decoding cleanly is
    the good refill, so it stays and counts as put back. Gap-fill callers
    pass False: there the backup is the good original, so a larger but
    corrupt re-rip must not win. replace_dst=False never replaces a file
    already at the destination: any other file there fails that track.
    Files named in ``keep`` are left as the album holds them and the backup
    keeps its copy of them.

    Each file is copied under a temporary name beside its destination and
    checked against the receipt's sha256, then renamed to its name, over
    the file there only with replace_dst. The backup is deleted once every
    file is on disk and still the one put back; otherwise it stays, marked
    as a partial restore. Invalid backups return 0."""
    source = _restorable(backup, album_dir, expected_owner=expected_owner,
                         kinds={"gap-fill", "downsample"})
    if source is None:
        return 0
    backup_snapshot = snapshot_tree(source)
    if backup_snapshot is None:
        log.info(fmt(C.RED + C.BOLD,
            f"  ✗  Couldn't fully read the backup to restore it. "
            f"Originals are PRESERVED at:\n     {source}"))
        return 0
    album = _library_path(album_dir, create_parents=True)
    try:
        if album is not None and not os.path.lexists(album):
            os.mkdir(album)
            fsync_dir(album.parent)
        if album is None or not stat.S_ISDIR(os.lstat(album).st_mode):
            raise OSError
    except OSError:
        log.info(fmt(C.RED,
            f"  ✗  Couldn't bind the real album path for restore.\n"
            f"     Backed-up tracks remain at: {source}"))
        return 0
    payload = backup_payload(backup.receipt)
    found = {}
    for relative, (size, digest) in sorted(payload.items()):
        if relative in keep:
            continue
        target = album / relative
        try:
            if _library_path(target, create_parents=True) is None:
                raise OSError("its folder is not a real folder in the album")
            if not _matches(target, size, digest):
                _restore_file(source / relative, target, size, digest,
                              keep_larger_dst=keep_larger_dst,
                              replace_dst=replace_dst)
            elif not _fsync(target):
                raise OSError("it could not be flushed to disk")
            fsync_dir(target.parent, strict=True)
            found[relative] = stat_identity(os.lstat(target))
        except OSError as exc:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Couldn't restore {PurePosixPath(relative).name}: "
                f"{exc.strerror or exc}."))
    try:
        intact = all(stat_identity(os.lstat(album / relative)) == identity
                     for relative, identity in found.items())
    except OSError:
        intact = False
    if len(found) + len(keep & payload.keys()) != len(payload) or not intact:
        _write_receipt_marker(
            backup,
            _PARTIAL_RESTORE_SENTINEL,
            "partial restore - un-restored originals are the only copy",
        )
        log.info(fmt(C.RED + C.BOLD,
            f"  ✗  Some tracks could NOT be restored. Originals are "
            f"PRESERVED at:\n     {source}"))
        return len(found)
    if keep & payload.keys():
        return len(found)
    if not _delete_backup_folder(
            source, backup_snapshot, _hidden_name(source.name)):
        log.info(fmt(
            C.YELLOW,
            "  ⚠  Restored files are safe, but their exact backup "
            f"remains at {source}."))
    return len(found)


def _restore_file(source: Path, target: Path, size, digest, *,
                  keep_larger_dst, replace_dst) -> None:
    """Copy one backed-up file to ``target``, as restore_gap_fill_backup
    describes; return when the file at ``target`` is the one to keep."""
    try:
        existing = os.lstat(target)
    except FileNotFoundError:
        copy_file(source, target, sha256=digest)
        return
    if not stat.S_ISREG(existing.st_mode) or not replace_dst:
        raise OSError("a different file is already at the destination")
    if (
        keep_larger_dst
        and existing.st_size >= size
        and flac_audio_ok(target) is True
    ):
        if stat_identity(os.lstat(target)) != stat_identity(existing):
            raise OSError("the album's file changed while it was checked")
        if not _fsync(target):
            raise OSError("the kept file could not be flushed to disk")
        return
    temporary = target.parent / f".ql-copy-{secrets.token_hex(16)}"
    copy_file(source, temporary, sha256=digest)
    try:
        if stat_identity(os.lstat(target)) != stat_identity(existing):
            raise OSError("the album's file changed during the restore")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def restore_incomplete_upgrade_backup(
        backup,
        original_path: Path,
        *,
        expected_owner=None,
) -> IncompleteUpgradeRestoreOutcome | None:
    """Put back an album from the whole-album backup a stop left unfinished.

    Complete upgrades and every file-subset kind are refused, and so is an
    album whose parent folders are not the ones it was backed up from. The
    album is taken where it is: at its origin while that is the folder the
    backup was taken from, or in the hidden folder an interrupted removal
    left beside it, which is renamed back first. Otherwise it is rebuilt
    beside the origin and renamed into place once whole. Each file it
    lacks is copied from the backup; one it holds with the receipt's bytes
    counts as present, and any other is reported unresolved and stays, and
    so does the backup. The backup is deleted only once every file is at
    the origin with its receipt's bytes and on disk.
    """
    source = _restorable(backup, original_path,
                         expected_owner=expected_owner, complete=False)
    if source is None:
        return None
    try:
        backup_fd = _open_backup_directory(source)
    except OSError:
        return None
    try:
        state = _read_upgrade_recovery_state(backup_fd, backup.receipt)
        if state is None and _named_entry_missing(
                backup_fd, _UPGRADE_RECOVERY_STATE_SENTINEL):
            # A hard stop records nothing; the album is found where it is.
            state = {"version": 1, "location": "unrecorded"}
    finally:
        os.close(backup_fd)
    rooted = _rooted_path_parts(cfg.MUSIC_ROOT, Path(original_path))
    source_receipt = backup.receipt.get("source_receipt")
    if (
        state is None
        or rooted is None
        or not _source_receipt_shape_is_valid(
            source_receipt, os.fspath(rooted[0]))
    ):
        return None
    origin = rooted[0]
    expected = {
        relative: (value["size"], value["sha256"])
        for relative, value in source_receipt["tree"]["files"].items()
    }
    directories = source_receipt["tree"]["directories"]
    payload = backup_payload(backup.receipt)
    backup_snapshot = snapshot_tree(source)
    if (
        payload != expected
        or set(backup.receipt["tree"]["directories"]) != set(directories)
        or backup_snapshot is None
    ):
        return None
    total = len(expected)
    refused = IncompleteUpgradeRestoreOutcome(0, 0, total, False)
    if not _album_parents_unchanged(backup.receipt):
        return refused

    album = None
    if os.path.lexists(origin):
        if _folder_incarnation(origin) != _incarnations(
                source_receipt["path_generations"][-1:]):
            return refused
        album = origin
    elif state["location"] == "public":
        return refused
    elif (remnant := _hidden_album_remnant(
            origin.parent, source_receipt)) is not None:
        try:
            _rename_noreplace_at(AT_FDCWD, remnant / "held", AT_FDCWD, origin)
            fsync_dir(origin.parent, strict=True)
        except OSError:
            return refused
        _remove_dir_quietly(remnant)
        album = origin
    elif state["location"] == "unrecorded":
        # A hard stop after the last file left its removal folder empty.
        for name in os.listdir(origin.parent):
            if name.startswith(".ql-backup-remove-"):
                _remove_dir_quietly(origin.parent / name)
    elif state["location"] != "absent":
        return refused
    stage = origin.parent / _hidden_name(source.name, ".ql-restore-stage-")
    created = []
    if album is None:
        if not _clear_restore_folder(stage, payload):
            return refused
        try:
            os.mkdir(stage, 0o700)
        except OSError:
            return refused
        album = stage
        created.append("")

    restored = 0
    present = 0
    found = {}
    try:
        _remove_partial_copies(album)
        for relative in sorted(
                directories, key=lambda value: (value.count("/"), value)):
            try:
                os.mkdir(album / relative)
                created.append(relative)
            except OSError:
                pass
        for relative, (size, digest) in sorted(expected.items()):
            target = album / relative
            try:
                if not os.path.lexists(target):
                    copy_file(source / relative, target, sha256=digest)
                    copied = True
                elif _matches(target, size, digest):
                    copied = False
                else:
                    continue
                found[relative] = stat_identity(os.lstat(target))
            except OSError:
                continue
            if copied:
                restored += 1
            else:
                present += 1

        unresolved = total - len(found)
        if unresolved:
            _write_receipt_marker(
                backup,
                _PARTIAL_RESTORE_SENTINEL,
                "incomplete upgrade restore stage was not publishable"
                if album == stage else
                "incomplete upgrade restore retained unresolved originals",
            )
            if album == stage:
                return refused
            return IncompleteUpgradeRestoreOutcome(
                restored, present, unresolved, False)
        for relative in reversed(created):
            _copy_folder_metadata(source / relative, album / relative)
        if not fsync_tree(album):
            return IncompleteUpgradeRestoreOutcome(
                restored, present, 0, False)
        if album == stage:
            _rename_noreplace_at(AT_FDCWD, stage, AT_FDCWD, origin)
            album = origin
            fsync_dir(origin.parent, strict=True)
        for relative, identity in found.items():
            if stat_identity(os.lstat(origin / relative)) != identity:
                return IncompleteUpgradeRestoreOutcome(
                    restored, present, 0, False)
        removed = _delete_backup_folder(
            source, backup_snapshot, _hidden_name(source.name))
        return IncompleteUpgradeRestoreOutcome(
            restored, present, 0, removed)
    except (OSError, TypeError, ValueError):
        if album == stage:
            return refused
        return IncompleteUpgradeRestoreOutcome(
            restored, present, total - len(found), False)
    finally:
        if album == stage:
            delete_tree_verified(stage, snapshot_tree(stage) or {})


def _remove_dir_quietly(path) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


def _remove_partial_copies(folder: Path) -> None:
    """Delete the partial copies a stop left in ``folder``, under the
    temporary name every copy into the library takes."""
    for relative, value in (snapshot_tree(folder) or {}).items():
        if stat.S_ISREG(value[2]) and _COPY_TEMP.fullmatch(
                os.path.basename(relative)):
            delete_verified(folder / relative, value)


# Set by the web app and the terminal: given the receipt of each backup a
# restore put back with every file proven against it.
on_restored = None


def _tell_restored(receipt, original_path) -> None:
    if on_restored is None:
        return
    try:
        on_restored(receipt)
    except Exception as exc:
        log.info(f"  The reviews of {original_path} could not be updated "
                 f"after the restore: {exc}")


def restore_upgrade_backup(
        backup, original_path: Path, *, expected_owner=None,
        replace_partial=True) -> bool:
    """Put a whole-album upgrade backup back at its origin, and tell
    ``on_restored`` once it is back.

    A complete backup moves back. An incomplete one, which a stop part way
    through the backup leaves, supplies the files the album is missing and
    replaces none: a file that differs keeps both copies, and the backup
    stays. One whose files the album holds, some under new names, is left
    alone, so no track ends up there twice.
    """
    if backup.complete:
        restored = _restore_upgrade_backup(
            backup, original_path, expected_owner=expected_owner,
            replace_partial=replace_partial)
    elif files_back_under_new_names(backup.path):
        log.info(fmt(C.YELLOW,
            f"  ⚠  {original_path} already has every file in the backup at "
            f"{backup.path}, some under new names; nothing was put back."))
        restored = False
    else:
        outcome = restore_incomplete_upgrade_backup(
            backup, original_path, expected_owner=expected_owner)
        restored = (
            outcome is not None
            and outcome.unresolved == 0
            and outcome.backup_disposed
        )
        if outcome is not None and outcome.unresolved:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {errors_mod.plural(outcome.unresolved, 'file')} of "
                f"{original_path} couldn't be put back from the backup, "
                "or differ from it; both copies were kept and the backup "
                f"stays at {backup.path}."))
    if restored:
        _tell_restored(backup.receipt, original_path)
    return restored


def dispose_put_back_upgrade_backup(
        backup, original_path: Path, *, expected_owner=None) -> bool:
    """Delete a complete upgrade backup once every file in it is back at
    ``original_path`` byte for byte, as a copy back stopped before it
    deleted the backup leaves them, and tell ``on_restored``. Without that
    proof, or if the deletion fails, both are kept and this says False."""
    receipt = capture_album_source_receipt(original_path)
    if receipt is None or not dispose_backup(
            backup,
            replacement_path=original_path,
            expected_replacement_receipt=receipt,
            replacement_validator=backup_files_preserved,
            expected_owner=expected_owner):
        return False
    _tell_restored(backup.receipt, original_path)
    return True


def backup_payload(receipt):
    """{relative path: (size, sha256)} of the files a whole-album receipt
    holds, without the backup's own files."""
    return {
        relative: (value["size"], value["sha256"])
        for relative, value in receipt["tree"]["files"].items()
        if PurePosixPath(relative).name not in _SIDECARS
    }


def _library_path(path, *, create_parents=False):
    """``path`` made absolute, when it lies below MUSIC_ROOT and every
    folder from MUSIC_ROOT to its parent is a real folder, not a link;
    missing ones are made with ``create_parents``. Else None."""
    rooted = _rooted_path_parts(cfg.MUSIC_ROOT, Path(path))
    if rooted is None:
        return None
    public, folder, parts = rooted
    try:
        for part in ("", *parts[:-1]):
            folder = folder / part
            try:
                value = os.lstat(folder)
            except FileNotFoundError:
                if not create_parents:
                    return None
                os.mkdir(folder)
                fsync_dir(folder.parent)
                value = os.lstat(folder)
            if not stat.S_ISDIR(value.st_mode):
                return None
    except OSError:
        return None
    return public


def library_parents_present(path) -> bool:
    """Whether every folder from MUSIC_ROOT to the parent of ``path`` is a
    real folder, without making any."""
    return _library_path(path) is not None


def _plain_tree(root):
    """snapshot_tree of ``root``, or None when anything in it is neither a
    file nor a folder."""
    snapshot = snapshot_tree(root)
    if snapshot is None or any(
            not stat.S_ISDIR(value[2]) and not stat.S_ISREG(value[2])
            for value in snapshot.values()):
        return None
    return snapshot


def _matches(path, size, digest) -> bool:
    """Whether ``path`` is a file, not a link, with this size and sha256."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                             | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        value = os.fstat(descriptor)
        return (stat.S_ISREG(value.st_mode) and value.st_size == size
                and _file_digest_fd(descriptor) == digest)
    except OSError:
        return False
    finally:
        os.close(descriptor)


_COPY_TEMP = re.compile(r"\.ql-copy-[0-9a-f]{32}")


def _clear_restore_folder(folder: Path, payload) -> bool:
    """Make way at ``folder``, a name a restore keeps for one backup. What a
    stop left there is deleted while each file in it is one of the backup's
    with its bytes, or a partial copy; anything else keeps it. Whether the
    name is free."""
    if not os.path.lexists(folder):
        return True
    snapshot = _plain_tree(folder)
    if snapshot is None:
        return False
    for relative, value in snapshot.items():
        if stat.S_ISDIR(value[2]) or _COPY_TEMP.fullmatch(
                os.path.basename(relative)):
            continue
        if relative not in payload or not _matches(
                folder / relative, *payload[relative]):
            return False
    return not delete_tree_verified(folder, snapshot)


def _copy_backup_into(source: Path, folder: Path, payload,
                      directories) -> None:
    """Copy the files of ``payload``, {relative path: (size, sha256)}, and
    the ``directories`` from ``source`` into the new folder ``folder``,
    each file checked against its sha256, then give the folders their
    source's modes and times and flush it all to disk."""
    os.mkdir(folder, 0o700)
    for relative in sorted(directories):
        os.makedirs(folder / relative, exist_ok=True)
    for relative, (_size, digest) in sorted(payload.items()):
        os.makedirs((folder / relative).parent, exist_ok=True)
        copy_file(source / relative, folder / relative, sha256=digest)
    for relative in sorted(directories, reverse=True):
        _copy_folder_metadata(source / relative, folder / relative)
    _copy_folder_metadata(source, folder)
    if not fsync_tree(folder):
        raise OSError("the copy could not be flushed to disk")


def _copy_folder_metadata(source: Path, destination: Path) -> None:
    try:
        source_fd = _open_backup_directory(source)
        try:
            destination_fd = _open_backup_directory(destination)
            try:
                copy_metadata(source_fd, destination_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
    except OSError:
        pass


def _restorable(backup, original_path, *, expected_owner, complete=None,
                kinds=("upgrade",)):
    """The folder of a backup of one of ``kinds`` whose files and receipt
    check out for a restore to ``original_path``, else None."""
    if not _backup_owner_authorized(backup, expected_owner):
        return None
    opened = _validated_backup_result(
        backup, origin=original_path, kinds=set(kinds),
        require_complete=complete)
    if opened is None:
        return None
    try:
        if (
            complete is not None and backup.complete is not complete
            or _receipt_disposal_snapshot(
                opened[-1][-1], backup.receipt) is None
        ):
            return None
    finally:
        _close_descriptors(opened[-1])
    return opened[0]


def _restore_upgrade_backup(
        backup, original_path: Path, *, expected_owner=None,
        replace_partial=True) -> bool:
    """Move a complete whole-album upgrade backup back to its origin.

    On the same filesystem the backup folder is renamed home. Otherwise
    its files are copied into a folder beside the origin, each checked
    against the receipt, which is renamed into place before the backup is
    deleted. Nothing is overwritten.

    If a partial download left an album folder at original_path, it is
    moved aside and replaced by the backup (the backup is the only intact
    copy), and deleted once the album is back, unless ``replace_partial``
    is False, which restores only into an empty place. Returns True on
    success.

    Compares backup vs partial on TOTAL BYTES, not file count: a partial
    download might have written the single largest track first (1 huge
    file) while the legitimate backup holds the rest (many smaller
    files). File-count alone would call the backup "bigger" and wipe the
    intact track. Bytes-based is what actually matters for "more data
    here than there".
    """
    source = _restorable(backup, original_path,
                         expected_owner=expected_owner, complete=True)
    if source is None:
        return False
    origin = _library_path(original_path, create_parents=True)
    if origin is None:
        return False
    payload = backup_payload(backup.receipt)
    backup_snapshot = snapshot_tree(source)
    stage = origin.parent / _hidden_name(source.name, ".ql-restore-stage-")
    aside = origin.parent / _hidden_name(source.name, ".ql-restore-partial-")
    backup_bytes = sum(size for size, _digest in payload.values())
    partial = None
    earlier = None
    moved_aside = False
    staged = False
    renamed = False
    published = False
    note = None
    try:
        if backup_snapshot is None:
            raise OSError("backup tree could not be read safely")
        if os.path.lexists(origin):
            if not replace_partial:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {origin} exists again; the backup stays "
                    f"at {source}."))
                return False
            partial = _plain_tree(origin)
            if partial is None or any(
                    os.path.basename(relative) in _SIDECARS
                    for relative in partial):
                raise OSError("partial album tree could not be read safely")
            partial_files = [value[3] for value in partial.values()
                             if stat.S_ISREG(value[2])]
            if backup_bytes <= sum(partial_files):
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Cannot auto-restore: {origin} has "
                    f"{errors_mod.plural(len(partial_files), 'file')} / "
                    f"{sum(partial_files) / 1024 / 1024:.1f} MB (backup has "
                    f"{len(payload)} / "
                    f"{backup_bytes / 1024 / 1024:.1f} MB)."))
                return False
        elif os.path.lexists(aside):
            # The album folder a stopped restore moved aside to replace it.
            earlier = _replaceable_partial(aside, backup_bytes)
            if earlier is None:
                raise OSError("an earlier restore's folder is in the way")
        if not _clear_restore_folder(stage, payload) or (
                partial is not None
                and not _clear_restore_folder(aside, payload)):
            raise OSError("an earlier restore's folder is in the way")
        if partial is not None:
            _rename_noreplace_at(AT_FDCWD, origin, AT_FDCWD, aside)
            moved_aside = True
            fsync_dir(origin.parent, strict=True)

        if _same_filesystem(source, origin):
            note = _write_put_back_note(source, origin)
            try:
                _rename_noreplace_at(AT_FDCWD, source, AT_FDCWD, origin)
                renamed = published = True
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
        if not published:
            staged = True
            _copy_backup_into(source, stage, payload,
                              backup.receipt["tree"]["directories"])
            _rename_noreplace_at(AT_FDCWD, stage, AT_FDCWD, origin)
            published = True
            # A copy the disk may not keep must not let the backup go.
            fsync_dir(origin.parent, strict=True)
        else:
            fsync_dir(origin.parent)
        fsync_dir(source.parent)
    except BaseException as exc:
        if moved_aside and not published:
            try:
                _rename_noreplace_at(AT_FDCWD, aside, AT_FDCWD, origin)
                fsync_dir(origin.parent)
            except OSError:
                pass
        if not isinstance(exc, (OSError, shutil.Error)):
            raise
        log.info(fmt(C.RED,
            f"  ✗  Restore failed: {exc}.\n"
            f"     Backup is preserved at: {source}"))
        return False
    finally:
        if staged and not published and os.path.lexists(stage):
            delete_tree_verified(stage, snapshot_tree(stage) or {})
        if note is not None and not renamed:
            _drop_put_back_note(note)

    # The old partial goes before the backup or its put-back note: what a
    # stop leaves of it is then still found from them, at the next start or
    # in Diagnostics.
    replaced = partial if partial is not None else earlier
    if replaced is not None and delete_tree_verified(aside, replaced):
        log.info(fmt(C.YELLOW,
            f"  ⚠  The restored album is safe, but the old partial "
            f"is retained in {aside}."))
    if renamed:
        if _remove_put_back_sidecars(origin) and note is not None:
            _drop_put_back_note(note)
    elif not _delete_backup_folder(
            source, backup_snapshot, _hidden_name(source.name)):
        log.info(fmt(
            C.YELLOW,
            "  ⚠  The restored album is safe, but the backup could not be "
            f"deleted; it remains at {source}."))
    # Beets is deliberately not mutated by restored public paths: those
    # paths may already name replacement rows and external remove hooks are
    # not transaction-safe.
    if partial is not None:
        log.info(fmt(C.GRAY,
            "     · If beets still lists the replaced partial import, "
            "run `beet update`; no path-based database removal was "
            "attempted automatically."))
    return True


def _replaceable_partial(folder: Path, replacing_bytes):
    """The snapshot of the album folder a restore moved aside to ``folder``,
    while it still holds fewer bytes than the album replacing it, as it did
    when it was moved, and none of a backup's own files. Else None."""
    snapshot = _plain_tree(folder)
    if snapshot is None or any(
            os.path.basename(relative) in _SIDECARS for relative in snapshot):
        return None
    if _tree_bytes(snapshot) >= replacing_bytes:
        return None
    return snapshot


def _tree_bytes(snapshot) -> int:
    return sum(value[3] for relative, value in snapshot.items()
               if stat.S_ISREG(value[2]) and relative not in _SIDECARS)


def _restore_folders(path, origin):
    """The folders a restore of the backup at ``path`` keeps beside its
    album: its copy of the backup and the album folder it moved aside."""
    name = Path(path).name
    return (origin.parent / _hidden_name(name, ".ql-restore-stage-"),
            origin.parent / _hidden_name(name, ".ql-restore-partial-"))


def _backups_and_origins():
    try:
        entries = sorted(cfg.UPGRADE_BACKUP_DIR.iterdir())
    except OSError:
        return []
    return [
        (entry, origin) for entry in entries
        if re.match(r"^\d{8}_\d{6}_", entry.name) and entry.is_dir()
        and (origin := _read_backup_origin(entry)) is not None
    ]


def settle_interrupted_restores() -> None:
    """Finish what a stop left of a restore. The album folder it moved
    aside goes back while its place is still empty, as it would had the
    restore failed, unless the put-back that follows takes that backup,
    which then replaces the folder as the restore meant to. Its copy of the
    backup is deleted while every file in it is the backup's own, and so is
    a partial file a stop left in the album under the temporary name every
    copy takes. Anything else stays, and Diagnostics lists it."""
    for path, origin in _backups_and_origins():
        if _library_path(origin) is not None:
            _remove_partial_copies(origin)
        stage, aside = _restore_folders(path, origin)
        if (
            os.path.lexists(aside)
            and not os.path.lexists(origin)
            and _library_path(origin) is not None
            # The put-back below finishes a restore it would make itself.
            and album_backup_to_put_back(path, origin) is None
        ):
            try:
                _rename_noreplace_at(AT_FDCWD, aside, AT_FDCWD, origin)
                fsync_dir(origin.parent)
            except OSError:
                pass
        if os.path.lexists(stage):
            backup = load_backup_result(path)
            if backup is not None and backup.receipt["kind"] == "upgrade":
                _clear_restore_folder(stage, backup_payload(backup.receipt))


def unfinished_restores():
    """The folders a stopped restore left beside albums that start-up
    could not settle."""
    found = [(path, origin) for path, origin in _backups_and_origins()]
    try:
        notes = sorted(name for name in os.listdir(cfg.UPGRADE_BACKUP_DIR)
                       if name.startswith(_PUT_BACK_PREFIX))
    except OSError:
        notes = []
    for name in notes:
        note = _read_small_json(cfg.UPGRADE_BACKUP_DIR / name)
        try:
            found.append((note["backup"], Path(note["origin"])))
        except (TypeError, KeyError, ValueError):
            pass
    return sorted({
        folder for path, origin in found
        for folder in _restore_folders(path, origin)
        if os.path.lexists(folder)
    })


def upgrade_backup_restorable(backup, original_path: Path) -> bool:
    """Whether restore_upgrade_backup's checks on the backup itself pass now."""
    return _restorable(backup, original_path, expected_owner=None,
                       complete=True) is not None


def mark_set_aside_replacement(backup) -> bool:
    """Label a backup as the download an upgrade put aside, not an album."""
    return _write_receipt_marker(
        backup,
        _SET_ASIDE_SENTINEL,
        "the download an upgrade could not verify; the original album was "
        "put back",
    )


def is_set_aside_replacement(path) -> bool:
    return (Path(path) / _SET_ASIDE_SENTINEL).is_file()


def mark_upgrade_pending(backup, note) -> bool:
    """Record in an upgrade's backup what finishing or undoing the upgrade
    needs, before its download is imported."""
    return _write_receipt_marker(backup, _UPGRADE_PENDING_SENTINEL, note)


def interrupted_upgrades():
    """(backup, note) for each upgrade a hard stop cut short after its
    download began to import, oldest first. A backup a saved download owns,
    or one kept for the user to look at, is left out."""
    found = []
    try:
        entries = sorted(cfg.UPGRADE_BACKUP_DIR.iterdir())
    except OSError:
        return found
    for entry in entries:
        if (
            is_disposal_leftover(entry)
            or not (entry / _UPGRADE_PENDING_SENTINEL).is_file()
            or any((entry / marker).exists() for marker in (
                _PARTIAL_RESTORE_SENTINEL, _UNVERIFIED_UPGRADE_SENTINEL,
                _SET_ASIDE_SENTINEL))
        ):
            continue
        backup = load_backup_result(entry)
        if (
            backup is None
            or not backup.complete
            or backup.receipt["kind"] != "upgrade"
            or not _backup_owner_authorized(backup, None)
        ):
            continue
        opened = _validated_backup_result(backup, require_complete=True)
        if opened is None:
            continue
        descriptors = opened[-1]
        try:
            note = _receipt_marker_note(
                descriptors[-1], _UPGRADE_PENDING_SENTINEL, backup.receipt)
        finally:
            _close_descriptors(descriptors)
        if note is not None:
            found.append((backup, note))
    return found


def _unfinished_upgrade(path) -> bool:
    """Whether this is an upgrade backup a stop left before the album's
    folder was retired. Metadata only."""
    try:
        receipt = json.loads(
            (path / _RECEIPT_SIDECAR).read_text(encoding="utf-8"))
        return (
            receipt["kind"] == "upgrade"
            and receipt["complete"] is False
            and not (path / _UPGRADE_RETIREMENT_COMPLETE_SENTINEL).is_file()
            and not is_set_aside_replacement(path)
        )
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _kept_by_file(path) -> bool:
    """Whether this is a gap-fill, repair or downsample backup, which holds
    only the files it set aside. Metadata only."""
    try:
        return json.loads((path / _RECEIPT_SIDECAR).read_text(
            encoding="utf-8"))["kind"] in {"gap-fill", "downsample"}
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _backup_stamp(name):
    match = re.match(r"^(\d{8}_\d{6})(?:_(\d{6}))?_", name)
    return None if match is None else (match.group(1), match.group(2) or "")


def _superseded(path, origin) -> bool:
    """Whether a later finished whole-album backup of the same album folder
    exists. It took the album as it was after this one, so this one must
    never be put back over it. A download set aside, or a backup still
    unfinished, holds no newer state of the album."""
    stamp = _backup_stamp(backup_name(path))
    try:
        for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
            later = _backup_stamp(entry.name)
            if (
                stamp is not None and later is not None and later > stamp
                and not re.match(
                    r"^\d{8}_\d{6}(?:_\d{6})?_(?:gapfill|downsample)_",
                    entry.name)
                and (entry / _UPGRADE_RETIREMENT_COMPLETE_SENTINEL).is_file()
                and not is_set_aside_replacement(entry)
                and _read_backup_origin(entry) == origin
            ):
                return True
    except OSError:
        return False
    return False


def _album_untouched(origin, source_receipt, *, whole=False) -> bool:
    """Whether everything in the album folder is a file or folder of its
    sealed tree, each file with the same size and sha256, and with
    ``whole`` none of it missing. A partial copy a stop left there, under
    the temporary name every copy takes, does not count."""
    current = _plain_tree(origin) if _library_path(origin) else None
    if current is None:
        return False
    try:
        sealed = source_receipt["tree"]
        folders = {relative for relative, value in current.items()
                   if stat.S_ISDIR(value[2])}
        files = {relative for relative in current
                 if relative not in folders
                 and not _COPY_TEMP.fullmatch(os.path.basename(relative))}
        if (
            not folders <= set(sealed["directories"])
            or not files <= set(sealed["files"])
            or whole and files != set(sealed["files"])
        ):
            return False
        return all(
            _matches(origin / relative, sealed["files"][relative]["size"],
                     sealed["files"][relative]["sha256"])
            for relative in files)
    except (TypeError, ValueError, KeyError):
        return False


def albums_left_in_backups():
    """(backup, album folder) for each whole-album backup whose folder is
    missing from the library, or missing files an unfinished backup holds,
    oldest first.

    A hard stop or Ctrl-C during an upgrade, a forced re-download or a
    whole-album repair leaves the album here with nothing, or only part of
    it, in its place, and so does a backup the start-up put-back could not
    finish. Downloads an upgrade set aside and file-by-file backups are left
    out, and so is an album whose parent folder is gone too, which is how an
    unmounted library reads. An unfinished backup counts beside its album
    only until a later backup of it exists. Metadata only.
    """
    found = []
    try:
        entries = sorted(cfg.UPGRADE_BACKUP_DIR.iterdir())
    except FileNotFoundError:
        return found
    for entry in entries:
        if (
            not re.match(r"^\d{8}_\d{6}_", entry.name)
            or not entry.is_dir()
            or (
                not (entry / _UPGRADE_RETIREMENT_COMPLETE_SENTINEL).is_file()
                and re.match(
                    r"^\d{8}_\d{6}(?:_\d{6})?_(?:gapfill|repair|downsample)_",
                    entry.name)
            )
            or is_set_aside_replacement(entry)
        ):
            continue
        origin = _read_backup_origin(entry) or _intended_origin(entry)
        if origin is None or not origin.parent.is_dir():
            continue
        if not os.path.lexists(origin) or (
            origin.is_dir()
            and (files_not_back_in_album(entry) or (0,))[0]
        ):
            found.append((entry, origin))
    return found


def repair_backups_to_fill_in():
    """(backup, album folder) for each Repair backup whose album a hard stop
    left without some of its tracks, oldest first.

    Repair moves the damaged tracks here before their replacements download,
    so a stop it could not handle leaves their places empty. Only a whole,
    unpinned backup whose album folder is still in the library counts.
    """
    found = []
    try:
        entries = sorted(cfg.UPGRADE_BACKUP_DIR.iterdir())
    except FileNotFoundError:
        return found
    for entry in entries:
        if (not re.match(r"^\d{8}_\d{6}(?:_\d{6})?_repair_", entry.name)
                or not entry.is_dir() or backup_keep_markers_present(entry)):
            continue
        candidate = load_backup_result(entry)
        if (candidate is None or not candidate.complete
                or candidate.receipt["kind"] != "gap-fill"
                or not _backup_owner_authorized(candidate, None)):
            continue
        origin = Path(candidate.receipt["origin"])
        if (origin.is_dir() and _library_path(origin) is not None
                and (files_not_back_in_album(entry) or (0,))[0]):
            found.append((candidate, origin))
    return found


def fill_in_from_repair_backup(backup, origin) -> int:
    """Put a Repair backup's tracks back into the places in ``origin`` that
    are empty, keeping any replacement that reached the album, and return
    how many are back."""
    return restore_gap_fill_backup(backup, origin, replace_dst=False,
                                   keep=files_replaced_in_album(backup))


def _intended_origin(path):
    try:
        root_fd = _open_backup_directory(path.parent)
    except OSError:
        return None
    try:
        intent = _read_backup_intent(root_fd, _BACKUP_INTENT_PREFIX + path.name)
    finally:
        os.close(root_fd)
    return None if intent is None else Path(intent[1]["origin"])


def unfinished_copy_album(path):
    """The album folder the unfinished backup copy at ``path`` was made of,
    or None when it can't be told."""
    path = Path(path)
    return _read_backup_origin(path) or _intended_origin(
        path.with_name(path.name.removeprefix(_PARTIAL_COPY_PREFIX)))


def _incarnations(generations):
    """Directory generations as inode and birth time, which survive the
    renumbered device and mount a restart can bring."""
    return [(value[2], value[4], value[5]) for value in generations]


def _album_parents_unchanged(receipt) -> bool:
    """Whether every folder from the music root down to the album's parent
    is the one the backup was taken from."""
    try:
        recorded = receipt["source_receipt"]["path_generations"][:-1]
        parent = Path(receipt["origin"]).parent
    except (KeyError, TypeError):
        return False
    opened = _open_rooted_directory(cfg.MUSIC_ROOT, parent, allow_root=True)
    if opened is None:
        return False
    descriptors = opened[-1]
    try:
        return _incarnations(_path_directory_generations(descriptors)) == (
            _incarnations(recorded))
    except (OSError, TypeError, ValueError, IndexError):
        return False
    finally:
        _close_descriptors(descriptors)


def album_backup_to_put_back(path, origin):
    """The backup an album listed by albums_left_in_backups can be restored
    from without asking, or None.

    It must be unpinned, owned by no saved download, the latest backup of
    the album, and beneath the same parent folders. The album's folder must
    still be missing, or for an unfinished backup hold nothing but untouched
    files of the sealed tree, so files the user renamed, changed or added
    since are never joined by the old ones. Anything else stays for the user
    in Diagnostics.
    """
    if backup_keep_markers_present(path) or os.path.lexists(origin) and (
            path / _UPGRADE_RETIREMENT_COMPLETE_SENTINEL).is_file():
        return None
    candidate = load_backup_result(path)
    if (
        candidate is None
        or candidate.receipt["kind"] != "upgrade"
        or candidate.receipt["origin"] != os.fspath(origin)
        or candidate.complete and os.path.lexists(origin)
        or not _backup_owner_authorized(candidate, None)
        or not _album_parents_unchanged(candidate.receipt)
        or _superseded(path, origin)
        or os.path.lexists(origin) and not _album_untouched(
            origin, candidate.receipt["source_receipt"])
    ):
        return None
    return candidate


def put_back_interrupted_backups():
    """Put back each album a hard stop left part way into its backup, and
    return the album folders put back.

    Only an album whose folder is still missing beneath the same parent
    folders is touched, or one a stop left part deleted after its copy. A
    backup that is the moved folder itself goes back by rename once every
    file in it has the inode, size and times sealed before the move. A copy
    on another filesystem supplies the files the album is missing, as
    Restore does for it, and the album's folder goes back in place. An album put back from a copy is passed to
    ``on_restored``. An album a restore had renamed home loses the backup's
    own files that a hard stop left inside it, and an unfinished backup of an
    album that is whole and untouched in its place is removed, as is an
    unfinished copy of tracks a Gap Fill or Repair was setting aside while
    its album still holds them.
    """
    put_back = []
    try:
        root_fd = _open_backup_directory(
            Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR))))
    except OSError:
        return put_back
    try:
        for name in sorted(os.listdir(root_fd)):
            if name.startswith(_PUT_BACK_PREFIX):
                _finish_put_back(name)
            elif name.startswith(_BACKUP_INTENT_PREFIX):
                origin = _put_back_interrupted_backup(root_fd, name)
                if origin is not None:
                    put_back.append(origin)
    except OSError:
        pass
    finally:
        os.close(root_fd)
    _clear_unfinished_backups_of_whole_albums()
    _clear_unfinished_track_copies()
    return put_back


def _clear_unfinished_track_copies():
    """Delete the unfinished copy a stop left of tracks being set aside for
    a Gap Fill, a Repair or a consolidation while every track in it is
    still in its album with the same bytes. No track leaves its album
    before its copy is finished, so such a copy holds only duplicates. One
    whose album no longer holds them all stays, and Diagnostics lists it."""
    try:
        names = sorted(os.listdir(cfg.UPGRADE_BACKUP_DIR))
    except OSError:
        return
    for name in names:
        if not re.match(re.escape(_PARTIAL_COPY_PREFIX)
                        + r"\d{8}_\d{6}_\d{6}_(?:gapfill|repair)_", name):
            continue
        partial = cfg.UPGRADE_BACKUP_DIR / name
        copied = _plain_tree(partial)
        if copied is None:
            continue
        tracks = {
            relative: value[3] for relative, value in copied.items()
            if stat.S_ISREG(value[2])
            and relative not in (_ORIGIN_SIDECAR, _RECEIPT_SIDECAR)
            and not _COPY_TEMP.fullmatch(os.path.basename(relative))
        }
        origin = _read_backup_origin(partial)
        try:
            duplicates = not tracks or (
                origin is not None
                and _library_path(origin) is not None
                and all(_matches(origin / relative, size,
                                 _file_digest(partial / relative))
                        for relative, size in tracks.items()))
        except OSError:
            duplicates = False
        if duplicates:
            delete_tree_verified(partial, copied)
        else:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Kept the unfinished backup copy at {partial}: its "
                "album no longer holds every track in it."))


def _clear_unfinished_backups_of_whole_albums():
    """Dispose of each unfinished whole-album backup whose album is whole and
    untouched in its place. It holds only duplicates, and left alone it
    could later put old files back into an album the user has changed."""
    try:
        entries = sorted(cfg.UPGRADE_BACKUP_DIR.iterdir())
    except OSError:
        return
    for entry in entries:
        if (
            not re.match(r"^\d{8}_\d{6}_", entry.name)
            or re.match(
                r"^\d{8}_\d{6}(?:_\d{6})?_(?:gapfill|repair|downsample)_",
                entry.name)
            or not _unfinished_upgrade(entry)
            or backup_keep_markers_present(entry)
        ):
            continue
        origin = _read_backup_origin(entry)
        if origin is None or files_not_back_in_album(entry) != (0, 0):
            continue
        candidate = load_backup_result(entry)
        if (
            candidate is not None
            and not candidate.complete
            and candidate.receipt["origin"] == os.fspath(origin)
            and _backup_owner_authorized(candidate, None)
            and _album_untouched(
                origin, candidate.receipt["source_receipt"], whole=True)
        ):
            restore_incomplete_upgrade_backup(candidate, origin)


def _put_back_interrupted_backup(root_fd, intent_name):
    intent = _read_backup_intent(root_fd, intent_name)
    if intent is None:
        return None
    backup_name, source_receipt = intent
    origin = Path(source_receipt["origin"])
    path = cfg.UPGRADE_BACKUP_DIR / backup_name
    backup = load_backup_result(path)
    ours = (
        backup is not None
        and not backup.complete
        and backup.receipt["origin"] == os.fspath(origin)
        and backup.receipt.get("source_receipt") == source_receipt
    )
    # A stop while the album was deleted after its copy leaves part of it.
    half = ours and os.path.lexists(origin) and not _album_untouched(
        origin, source_receipt, whole=True)
    if not half and (
        os.path.lexists(origin)
        or not os.path.lexists(path)
        or backup is not None and backup.complete
    ):
        # The album never left, or it waits in a complete backup, which the
        # put-back of albums_left_in_backups restores.
        if _partial_copy_settled(backup_name, origin, source_receipt):
            _drop_backup_intent(root_fd, intent_name)
        return None
    if _library_path(origin) is None or not _album_parents_unchanged(
            {"origin": os.fspath(origin), "source_receipt": source_receipt}):
        return None
    moved = _moved_album(path, source_receipt)
    restored = False
    if moved:
        restored = _rename_backup_home(path, origin)
    elif (
        moved is None
        and ours
        and album_backup_to_put_back(path, origin) is not None
    ):
        # The same put-back as a stop the app saw, which recorded where the
        # album was left.
        restored = restore_upgrade_backup(
            backup, origin, replace_partial=False)
    if not restored:
        return None
    _drop_backup_intent(root_fd, intent_name)
    return origin


def settle_library_backup_intent(value, *, expected_owner) -> bool:
    """Settle a backup a download planned but never sealed; True once
    nothing is left at its path.

    An album folder a stop left renamed there unsealed goes back while it
    is untouched beneath unchanged parent folders, and an unfinished copy
    of an album still whole in its place is removed. A sealed backup, or
    anything else there, is left alone.
    """
    intent = canonical_library_backup_intent(
        value, expected_owner=expected_owner)
    if intent is None:
        return False
    path = Path(intent["path"])
    origin = Path(intent["origin"])
    source_receipt = intent.get("source_receipt")
    if (
        source_receipt is not None
        and os.path.lexists(path)
        and load_backup_result(path) is None
        and _library_path(origin) is not None
        and _album_parents_unchanged(
            {"origin": intent["origin"], "source_receipt": source_receipt})
        and _moved_album(path, source_receipt) is True
    ):
        _rename_backup_home(path, origin)
    if not _backup_entry_absent(path):
        return False
    if source_receipt is not None:
        _partial_copy_settled(path.name, origin, source_receipt)
    return True


def _moved_album(path, source_receipt):
    """Whether the folder at ``path`` is the album's own folder, moved there
    whole: True when everything in it but the backup's origin and receipt
    is what was sealed before the move, by inode, size and times, False
    when it changed, and None when it is another folder, a copy."""
    try:
        if _folder_incarnation(path) != _incarnations(
                source_receipt["path_generations"][-1:]):
            return None
        sealed = source_receipt["tree"]
        current = snapshot_tree(path)
        if current is None:
            return False
        expected = {
            relative: (identity[0], identity[2])
            for relative, identity in sealed["directories"].items()
        }
        expected.update({
            relative: (value["identity"][0], value["identity"][2],
                       value["size"], value["mtime_ns"], value["changed_ns"])
            for relative, value in sealed["files"].items()
        })
        return expected == {
            relative: (stat.S_IFMT(value[2]), value[1])
            if stat.S_ISDIR(value[2]) else
            (stat.S_IFMT(value[2]), value[1], *value[3:])
            for relative, value in current.items()
            if relative not in (_ORIGIN_SIDECAR, _RECEIPT_SIDECAR)
        }
    except (OSError, TypeError, ValueError, KeyError, IndexError):
        return False


def _remove_own_file(path) -> bool:
    """Unlink the backup's own file at ``path``; True once it is gone."""
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(value.st_mode) and delete_verified(
        path, stat_identity(value))


def _album_matches_receipt(origin, source_receipt) -> bool:
    """Whether the album folder holds every file of its sealed tree with the
    same size and sha256."""
    if _library_path(origin) is None or _plain_tree(origin) is None:
        return False
    try:
        return all(
            _matches(origin / relative, expected["size"], expected["sha256"])
            for relative, expected in source_receipt["tree"]["files"].items())
    except (TypeError, ValueError, KeyError):
        return False


def _partial_copy_settled(backup_name, origin, source_receipt):
    """Remove the unfinished copy a hard stop left of an album that is still
    whole in its place, partial files under the temporary name every copy
    takes included, and say whether none is left. One whose album changed
    is kept and said so."""
    partial = cfg.UPGRADE_BACKUP_DIR / (_PARTIAL_COPY_PREFIX + backup_name)
    if not os.path.lexists(partial):
        return True
    copied = _plain_tree(partial)
    if copied is not None and {
            relative for relative, value in copied.items()
            if not stat.S_ISDIR(value[2])
            and relative not in (_ORIGIN_SIDECAR, _RECEIPT_SIDECAR)
            and not _COPY_TEMP.fullmatch(os.path.basename(relative))
    } <= set(source_receipt["tree"]["files"]) and _album_matches_receipt(
            origin, source_receipt) and not delete_tree_verified(
            partial, copied):
        return True
    log.info(fmt(C.YELLOW,
        f"  ⚠  Kept the unfinished backup copy of {origin} at "
        f"{partial}: the album no longer holds every "
        "file it was copying."))
    return False


def _hidden_album_remnant(parent: Path, source_receipt):
    """The hidden removal folder beside the album that still holds the
    album's own folder, as ``held``, or None."""
    expected = _incarnations(source_receipt["path_generations"][-1:])
    try:
        names = sorted(name for name in os.listdir(parent)
                       if name.startswith(".ql-backup-remove-"))
    except OSError:
        return None
    for name in names:
        if (
            not os.path.islink(parent / name)
            and _folder_incarnation(parent / name / "held") == expected
        ):
            return parent / name
    return None


def _audio_duration_seconds(path: Path):
    """Read media duration without consulting the public-path metadata cache."""
    try:
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


def _read_tags(path: Path, **kwargs):
    """Parse a track's header blocks through a small read buffer, so the
    audio after them is not read."""
    with open(path, "rb", buffering=8192) as handle:
        return scanner.mutagen.File(handle, **kwargs)


def _audio_isrc(path: Path):
    try:
        parsed = _read_tags(path, easy=True)
        values = (parsed.tags or {}).get("isrc") if parsed is not None else None
    except Exception:
        return None
    value = str(values[0]).strip().upper() if values else ""
    return value or None


def _decoded_audio_digest(path: Path):
    """(rate, bits, channels, sha256 of the decoded samples) of a FLAC, or
    None when it does not decode."""
    try:
        info = scanner.mutagen.File(os.fspath(path)).info
        digest = hashlib.sha256()
        with subprocess.Popen(
                ["flac", "-d", "-s", "-c", "--force-raw-format",
                 "--endian=little", "--sign=signed", os.fspath(path)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, env=cfg.child_env()) as decoder:
            for chunk in iter(lambda: decoder.stdout.read(1 << 20), b""):
                digest.update(chunk)
        if decoder.returncode != 0:
            return None
        return (info.sample_rate, info.bits_per_sample, info.channels,
                digest.hexdigest())
    except Exception:
        return None


def _same_decoded_audio(source: Path, destination: Path) -> bool:
    if (source.suffix.lower() != ".flac"
            or destination.suffix.lower() != ".flac"):
        return False
    digest = _decoded_audio_digest(source)
    return digest is not None and digest == _decoded_audio_digest(destination)


def _lyrics_rank(path: Path) -> int:
    """0 for no lyrics, 1 for plain, 2 for synced, in the tags of any format
    or a .lrc beside the track."""
    from qobuz_librarian.integrations.lyric_fetch import classify, tag_lyric_values

    values = []
    try:
        values += tag_lyric_values(_read_tags(path))
    except Exception:
        pass
    lrc = path.with_suffix(".lrc")
    try:
        if lrc.is_file() and not lrc.is_symlink():
            values.append(lrc.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    return max((("none", "plain", "synced").index(classify(value))
                for value in values), default=0)


def lyrics_only_in_album(backup, album_dir) -> dict:
    """{file: lyrics} for each file of this backup whose album copy has
    better lyrics in its tags than the backed-up one, which putting the
    backup back would drop."""
    from qobuz_librarian.integrations.lyric_fetch import classify, tag_lyric_values

    rank = ("none", "plain", "synced").index
    found = {}
    for relative in backup_payload(backup.receipt):
        try:
            values = tag_lyric_values(_read_tags(Path(album_dir) / relative))
        except Exception:
            continue
        best = max(values, key=lambda value: rank(classify(value)), default="")
        if rank(classify(best)) > _lyrics_rank(backup.path / relative):
            found[relative] = best
    return found


def read_only_originals(backup, relatives) -> list:
    """The names of these files of the backup that would come back to the
    album read-only, so no lyrics could be written onto them."""
    names = []
    for relative in sorted(relatives):
        path = backup.path / relative
        try:
            if not os.stat(path).st_mode & 0o222 or not os.access(path, os.W_OK):
                names.append(path.name)
        except OSError:
            names.append(path.name)
    return names


def put_lyrics_back(album_dir, lyrics) -> list:
    """Write lyrics from ``lyrics_only_in_album`` onto the album's tracks
    again, and return the names of the tracks they could not be written to."""
    from qobuz_librarian.integrations import lyric_fetch

    failed = []
    for relative, text in sorted(lyrics.items()):
        track = Path(album_dir) / relative
        try:
            lyric_fetch.give_lyrics(
                track, cfg.MUSIC_ROOT, text, cfg.LYRICS_FORMAT)
        except Exception as exc:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Couldn't write the lyrics of {track.name} back: {exc}"))
            failed.append(track.name)
    return failed


def some_names(names) -> str:
    """The first three names, then how many more there are."""
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:3])} and {len(names) - 3} more"


def _carry_lyrics(source: Path, destination: Path, carried: list) -> bool:
    """Copy the lyrics only the backup's track has onto the album's, and
    add the album's track to ``carried`` when they were copied."""
    from qobuz_librarian.integrations import lyric_fetch

    if _lyrics_rank(source) <= _lyrics_rank(destination):
        return True
    refusal = disk_support.refusal("lyric_carry", *(
        ("lrc_replace",) if os.path.lexists(destination.with_suffix(".lrc"))
        else ()))
    if refusal:
        log.info(fmt(C.YELLOW, f"  ⚠  {refusal}"))
        return False
    try:
        lyric_fetch.carry_lyrics(
            source, cfg.UPGRADE_BACKUP_DIR, destination, cfg.MUSIC_ROOT,
            cfg.LYRICS_FORMAT)
    except Exception as exc:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't copy the lyrics of {source.name} onto "
            f"{destination}: {exc}"))
        return False
    if _lyrics_rank(destination) < _lyrics_rank(source):
        log.info(fmt(C.YELLOW,
            f"  ⚠  The lyrics of {source.name} were not written onto "
            f"{destination}; keeping the backup."))
        return False
    carried.append(destination.name)
    return True


def _same_recording(replacement_view: Path, source: Path, taken, *,
                    lossless=False):
    """An album track, not yet matched, carrying the backup file's ISRC, and
    with ``lossless`` a FLAC."""
    isrc = _audio_isrc(source)
    if isrc is None:
        return None
    entries = _list_tree(replacement_view)
    for candidate in sorted(entries or ()):
        if (
            candidate.is_file()
            and candidate.suffix.lower() in (
                (".flac",) if lossless else cfg.AUDIO_EXTS)
            and candidate.relative_to(replacement_view) not in taken
            and _audio_isrc(candidate) == isrc
        ):
            return candidate
    return None


@dataclass(frozen=True)
class BackupClassification:
    status: str
    reason: str
    file: str = ""

    @property
    def removable(self):
        return self.status == "removable"

    @property
    def detail(self):
        return {
            "identical": "Removable: every file matches its album copy. Removal checks again.",
            "replacement": "Removable: the album holds a verified copy of every track. Removal checks again.",
            "compare": "Not checked yet: Remove first checks that the album's copy of each track has the same audio."
            + (f" Then it copies the lyrics of this backup's {self.file} onto the same track in the album, so they are kept." if self.file else ""),
            "other_audio": f"Not removable: the album's copy of {self.file} has different audio from this backup's.",
            "lyrics": f"Removable: the album holds a verified copy of every track. Remove first copies the lyrics of this backup's {self.file} onto the same track in the album, so they are kept.",
            "missing": f"Not removable: {self.file} is missing from the album.",
            "unrecorded": f"Not removable: {self.file} is not one of the files this backup was made with.",
            "different": f"Not removable: {self.file} differs from the album copy.",
            "audio": f"Not removable: the album's {self.file} does not decode cleanly.",
            "length": f"Not removable: the length of {self.file} can't be read in the backup or the album.",
            "shorter": f"Not removable: the album's {self.file} is shorter than this backup's.",
            "smaller": f"Not removable: the album's {self.file} is smaller than this backup's.",
            "origin": "Not removable: the original album folder is unavailable.",
            "origin_missing": "Not removable: the original album folder is missing.",
            "album_metadata": "Not removable: the album contains backup recovery metadata.",
            "album_structure": "Unverified: the album contains unreadable or unsupported file entries.",
            "record": "Not removable: the backup recovery record could not be verified.",
            "incomplete": "Not removable: the backup operation is incomplete.",
            "owner": "Not removable here: this backup belongs to a saved recovery operation.",
            "empty": "Not removable: no backed-up files could be verified.",
            "unreadable": "Unverified: some files could not be read.",
            "budget": "Not checked yet: its files are too large to check while this page loads. Remove checks them first. If a track here has lyrics the album's copy lacks, Remove copies them onto it first, so they are kept.",
            "interrupted": "Backup cleanup was interrupted.",
        }[self.reason]


@dataclass
class _BackupReadBudget:
    remaining: int

    def reserve(self, size):
        if size > self.remaining:
            return False
        self.remaining -= size
        return True


def _classify_backup_contents(
        replacement_view: Path,
        backup_view: Path,
        *,
        audio_replacement=False,
        allow_smaller_audio=False,
        match_recordings=False,
        renamed=None,
        budget=None,
        same_audio=False,
        carry_lyrics=False,
        pairs=None,
):
    """Compare payloads; retention permits verified replacement audio.

    ``match_recordings`` lets a backed-up track whose name is gone from the
    album be matched to the album track with its ISRC, for a refill that the
    import filed under a different name. ``renamed`` maps each backed-up
    file to the album track that stands in for it; a file it leaves out is
    missing. ``same_audio`` requires each album track to decode to exactly
    the samples of the backed-up one. With ``carry_lyrics`` a .lrc beside a
    backed-up track counts as that track's lyrics, which Remove copies over,
    not as a file the album must hold. ``pairs`` collects each backed-up
    track with the album track that verified in its place."""
    try:
        entries = _list_tree(backup_view)
        if entries is None:
            return BackupClassification("unverified", "unreadable")
        files = []
        for entry in entries:
            mode = entry.stat().st_mode
            if stat.S_ISREG(mode):
                if entry.name not in _SIDECARS:
                    files.append(entry)
            elif not stat.S_ISDIR(mode):
                return BackupClassification("unverified", "unreadable")
        if carry_lyrics:
            files = [f for f in files if f.suffix.lower() != ".lrc" or not any(
                f.with_suffix(ext).is_file() for ext in cfg.AUDIO_EXTS)]
        files.sort(key=lambda f: (f.suffix.lower() in cfg.AUDIO_EXTS, str(f)))
        taken = {f.relative_to(backup_view) for f in files}
        skipped = False
        lyrics = []
        for source in files:
            relative = source.relative_to(backup_view)
            destination = replacement_view / relative
            if renamed is not None:
                track = renamed.get(relative.as_posix())
                destination = replacement_view / track if track else None
            if (
                destination is not None
                and not destination.is_file()
                and match_recordings
                and source.suffix.lower() in cfg.AUDIO_EXTS
            ):
                # A smaller track stands in only when it is lossless, so a
                # lossy copy never retires a FLAC kept here.
                destination = _same_recording(
                    replacement_view, source, taken,
                    lossless=allow_smaller_audio)
                if destination is not None:
                    taken.add(destination.relative_to(replacement_view))
            if destination is None or not destination.is_file():
                return BackupClassification("retained", "missing", str(relative))
            if audio_replacement and source.suffix.lower() in cfg.AUDIO_EXTS:
                if budget is not None and not budget.reserve(destination.stat().st_size):
                    skipped = True
                    continue
                same_size = destination.stat().st_size == source.stat().st_size
                if same_size and budget is not None and not budget.reserve(
                        source.stat().st_size):
                    skipped = True
                    continue
                if same_size and _file_digest(source) == _file_digest(destination):
                    # The album holds this very file, damaged or not, so the
                    # backup's copy adds nothing.
                    continue
                source_duration = _audio_duration_seconds(source)
                destination_duration = _audio_duration_seconds(destination)
                if source_duration is None or destination_duration is None:
                    return BackupClassification("retained", "length", str(relative))
                if (destination_duration + max(2.0, source_duration * 0.01)
                        < source_duration):
                    return BackupClassification("retained", "shorter", str(relative))
                if (destination.suffix.lower() == ".flac"
                        and flac_audio_ok(destination) is not True):
                    return BackupClassification("retained", "audio", str(relative))
                if (not allow_smaller_audio
                        and destination.stat().st_size < source.stat().st_size):
                    return BackupClassification("retained", "smaller", str(relative))
                if same_audio and not _same_decoded_audio(source, destination):
                    return BackupClassification("retained", "other_audio", str(relative))
                if _lyrics_rank(source) > _lyrics_rank(destination):
                    lyrics.append(str(relative))
                if pairs is not None:
                    pairs.append((source, destination))
            else:
                source_size = source.stat().st_size
                destination_size = destination.stat().st_size
                if source_size != destination_size:
                    return BackupClassification("retained", "different", str(relative))
                if budget is not None and not budget.reserve(source_size + destination_size):
                    skipped = True
                    continue
                if _file_digest(source) != _file_digest(destination):
                    return BackupClassification("retained", "different", str(relative))
        if skipped:
            return BackupClassification("unverified", "budget")
        if not files:
            return BackupClassification("retained", "empty")
        if lyrics:
            return BackupClassification("removable", "lyrics", some_names(lyrics))
        return BackupClassification(
            "removable", "replacement" if audio_replacement else "identical")
    except (OSError, TypeError, ValueError):
        return BackupClassification("unverified", "unreadable")


def _classify_retained_backup(path, origin, budget, *, compare_audio=False):
    # A consolidation's tracks stand in at the album it kept, under the names
    # it recorded; the folder they came from is gone or holds the rest.
    named = consolidated_into(path)
    album = named[0] if named else origin
    if album is None:
        return BackupClassification("retained", "origin")
    if not os.path.lexists(album):
        return BackupClassification("retained", "origin_missing")
    opened = _open_backup_source(album)
    if opened is None:
        return BackupClassification("retained", "origin")
    try:
        if _scan_backup_tree(opened[-1][-1]) is None:
            return BackupClassification("unverified", "album_structure")
        if _reserved_backup_entry_present(opened[-1][-1]):
            return BackupClassification("retained", "album_metadata")
    finally:
        _close_descriptors(opened[-1])
    result = _classify_backup_contents(
        album, path, budget=budget, renamed=named[1] if named else None)
    # An album track whose bytes differ, or that is filed under another name,
    # may be the replacement Remove accepts. Which proof applies is in the
    # receipt.
    replaced = (result.reason in {"different", "missing"}
                and Path(result.file).suffix.lower() in (*cfg.AUDIO_EXTS, ".lrc"))
    if not result.removable and not replaced:
        return result
    # Loading the sealed receipt hashes the backup again. Reserve that read
    # before loading it; large candidates stay explicitly unverified.
    entries = _list_tree(path)
    if entries is None:
        return BackupClassification("unverified", "unreadable")
    try:
        stats = [f.stat() for f in entries]
        size = sum(info.st_size for info in stats if stat.S_ISREG(info.st_mode))
        if not budget.reserve(size):
            # A track the album lacks by name and by ISRC is missing under
            # every proof, which names and tags show without that read.
            if replaced:
                lacking = _classify_backup_contents(
                    album, path, audio_replacement=True,
                    match_recordings=True, carry_lyrics=True,
                    renamed=named[1] if named else None,
                    budget=_BackupReadBudget(0))
                if lacking.reason == "missing":
                    return lacking
            return BackupClassification("unverified", "budget")
        candidate = removal_candidate(path)
        if candidate is None or candidate.receipt["origin"] != os.fspath(origin):
            return BackupClassification("retained", "record")
        if not candidate.complete:
            return BackupClassification("retained", "incomplete")
        if named and _sealed_consolidation(candidate) != named:
            return BackupClassification("retained", "record")
        leftover = _leftover_top(path) is not None
        if leftover and (stray := _unrecorded_file(candidate)) is not None:
            return BackupClassification("retained", "unrecorded", stray)
        if not leftover and not _backup_owner_authorized(candidate, None):
            return BackupClassification("retained", "owner")
        if replaced:
            target = _replacement_proof(candidate)
            if target is None:
                return result
            album, proof = target
            result = _classify_backup_contents(
                album, path, audio_replacement=True, budget=budget,
                carry_lyrics=True,
                **{**proof, "same_audio": compare_audio and proof.get("same_audio", False)})
            if not result.removable:
                return result
            # Decoding every track to compare it is Remove's job, not the
            # listing's.
            if proof.get("same_audio") and not compare_audio:
                return BackupClassification(
                    "unverified", "compare",
                    result.file if result.reason == "lyrics" else "")
        if leftover:
            return result
        opened = _open_rooted_directory(cfg.UPGRADE_BACKUP_DIR, path)
        if opened is None:
            return BackupClassification("retained", "record")
        try:
            if _receipt_disposal_snapshot(opened[-1][-1], candidate.receipt) is None:
                return BackupClassification("retained", "record")
        finally:
            _close_descriptors(opened[-1])
    except (OSError, TypeError, ValueError):
        return BackupClassification("unverified", "unreadable")
    return result


def _retention_view_is_redundant(
        replacement_view: Path, backup_view: Path, *, allow_smaller_audio=False,
        match_recordings=False, same_audio=False, carry_lyrics=False,
        renamed=None, refused=None,
) -> bool:
    result = _classify_backup_contents(
        replacement_view, backup_view, audio_replacement=True,
        allow_smaller_audio=allow_smaller_audio,
        match_recordings=match_recordings,
        renamed=renamed,
        same_audio=same_audio,
        carry_lyrics=carry_lyrics,
    )
    if refused is not None:
        refused.append(result)
    # Lyrics only the backup's track has go onto the album's first, which
    # Remove does; until then the backup is their only copy.
    return result.removable and result.reason != "lyrics"


def _dispose_retention_candidate(
        candidate, *, replacement=None, renamed=None, allow_smaller_audio=False,
        match_recordings=False, same_audio=False, carry_lyrics=False,
        refused=None):
    if not isinstance(candidate, BackupResult) or candidate.receipt is None:
        return False
    replacement = Path(replacement or candidate.receipt["origin"])
    replacement_receipt = capture_album_tree_identity(replacement)
    if replacement_receipt is None:
        return False
    preserve_originals = (
        candidate.receipt["kind"] == "gap-fill"
        and not allow_smaller_audio and not match_recordings
    )
    return dispose_backup(
        candidate,
        replacement_path=replacement,
        expected_replacement_receipt=replacement_receipt,
        replacement_validator=lambda replacement_view, backup_view: (
            backup_files_preserved(replacement_view, backup_view)
            if preserve_originals else _retention_view_is_redundant(
                replacement_view,
                backup_view,
                allow_smaller_audio=allow_smaller_audio,
                match_recordings=match_recordings,
                same_audio=same_audio,
                carry_lyrics=carry_lyrics,
                renamed=renamed,
                refused=refused,
            )
        ),
    )


def _consolidation_kept(note):
    """(album, {backed-up file: album track}) from a consolidation marker
    note, or None."""
    try:
        value = json.loads(note)
        album = Path(value["album"])
        kept = value["kept"]
    except (TypeError, ValueError, KeyError):
        return None
    if (
        not album.is_absolute()
        or not isinstance(kept, dict)
        or not kept
        or len(set(kept.values())) != len(kept)
        or not all(
            _receipt_path_key_valid(name) and _receipt_path_key_valid(track)
            for name, track in kept.items())
    ):
        return None
    return album, kept


def consolidated_into(path):
    """(album, {backed-up file: album track}) for the backup at ``path`` of
    a consolidation, from its marker, or None for any other backup."""
    try:
        note = json.loads((Path(path) / _CONSOLIDATED_SENTINEL).read_text(
            encoding="utf-8"))["note"]
    except (OSError, TypeError, ValueError, KeyError):
        return None
    return _consolidation_kept(note)


def _sealed_consolidation(candidate):
    """consolidated_into for a backup whose marker its receipt seals."""
    opened = _open_rooted_directory(cfg.UPGRADE_BACKUP_DIR, candidate.path)
    if opened is None:
        return None
    try:
        note = _receipt_marker_note(
            opened[-1][-1], _CONSOLIDATED_SENTINEL, candidate.receipt)
    except OSError:
        return None
    finally:
        _close_descriptors(opened[-1])
    return _consolidation_kept(note)


def mark_consolidated(backup, album, kept) -> bool:
    """Name the album a consolidation kept and, for each file in its backup,
    the track there that stands in for it, so the age sweep can clear it."""
    return _write_receipt_marker(
        backup,
        _CONSOLIDATED_SENTINEL,
        json.dumps({"album": os.fspath(album), "kept": kept}, sort_keys=True),
    )


def _dispose_consolidation_backup(candidate, refused=None) -> bool:
    """Dispose a consolidation's backup once the album it kept still holds,
    for every file, a decode-clean track standing in for it at least as long.
    """
    named = _sealed_consolidation(candidate)
    return named is not None and _dispose_retention_candidate(
        candidate, replacement=named[0], renamed=named[1],
        allow_smaller_audio=True, refused=refused)


def release_undo_copy(path, carried=None) -> bool:
    """Drop a downsample's kept originals now, because the user asked.

    Retention has two conditions: the album must still hold every track the
    copies do, at its place or under another name with its ISRC, and enough
    days must have passed. This applies the first one,
    exactly as the sweep does, and lets the user waive the second. Lyrics
    only the originals have are copied onto the album's tracks first, as
    Remove does, and those tracks are added to ``carried``. It refuses
    anything that is not one of those deliberate undo copies, so it can never
    stand in for the proof a real backup has to pass.
    """
    candidate = removal_candidate(path)
    if (candidate is None or not isinstance(candidate.receipt, dict)
            or not candidate.receipt.get("origin")
            or not (candidate.path / _REAP_AFTER_RETENTION_SENTINEL).is_file()):
        return False
    return _carry_lyrics_and_dispose(
        candidate, Path(candidate.receipt["origin"]),
        {"allow_smaller_audio": True, "match_recordings": True},
        [] if carried is None else carried)


def retire_verified_repair_backup(backup) -> bool:
    """Dispose a repair's originals once each is verifiably superseded.

    The same proof the age sweep applies: every file the backup holds must
    have in the album a decode-clean track of at least its duration, at its
    exact path or, when Beets filed the refill under another name, carrying
    its ISRC. Size may shrink - the backup holds the damaged copies."""
    if (
        not isinstance(backup, BackupResult)
        or not isinstance(backup.receipt, dict)
        or not backup.receipt.get("origin")
    ):
        return False
    return _dispose_retention_candidate(
        backup, allow_smaller_audio=True, match_recordings=True)


def backup_files_preserved(replacement_view: Path, backup_view: Path) -> bool:
    return _classify_backup_contents(replacement_view, backup_view).removable


def discard_redundant_backup(path) -> bool:
    """Dispose one retained backup whose files are all byte-identical at
    its recorded origin.

    Unlike retention, removal requires exact digests even for audio. The
    classification is recomputed on held files, never read from Diagnostics.
    """
    candidate = removal_candidate(path)
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
        replacement_validator=backup_files_preserved,
    )


def _replacement_proof(candidate):
    """The album that may stand in for this backup's audio and how, as
    arguments for the retention proof, or None where only identical files
    will do.

    A consolidation's tracks stand in at the album it kept, under the names
    it recorded, at any size, as the age sweep proves them. An upgrade,
    Gap Fill and Repair download the track again, which Beets may file
    under another name, so its ISRC finds it too; an upgrade's track must
    not shrink. Repair kept damaged copies, so the proof Repair applies to
    its own backup holds, whatever their size. Gap Fill kept the user's own
    tracks, which go only when the album's copy decodes to the same samples.
    """
    named = _sealed_consolidation(candidate)
    if named is not None:
        return named[0], {"renamed": named[1], "allow_smaller_audio": True}
    origin = Path(candidate.receipt["origin"])
    kind = candidate.receipt.get("kind")
    if kind == "upgrade":
        return origin, {"match_recordings": True}
    if kind != "gap-fill":
        return None
    if is_repair_backup(candidate.path):
        return origin, {"match_recordings": True, "allow_smaller_audio": True}
    return origin, {"match_recordings": True, "allow_smaller_audio": True,
                    "same_audio": True}


def discard_replaced_backup(path, carried=None) -> bool:
    """Dispose one upgrade, gap-fill, repair or consolidation backup whose
    album holds a verified replacement for every file it kept, because the
    user asked.

    The new download's bytes differ, so the byte-identical proof can never
    pass. Each kept track needs a decode-clean track standing in for it in
    the album, as ``_replacement_proof`` describes. Lyrics only the backup's
    track has are copied onto the album's first, and the album's tracks
    that got them are added to ``carried``; when that fails, the backup
    stays.
    """
    candidate = removal_candidate(path)
    if candidate is None or not isinstance(candidate.receipt, dict):
        return False
    target = _replacement_proof(candidate)
    if target is None:
        return False
    album, proof = target
    return _carry_lyrics_and_dispose(
        candidate, album, proof, [] if carried is None else carried)


def _carry_lyrics_and_dispose(candidate, album, proof, carried) -> bool:
    """Copy the lyrics only the backup's tracks have onto those in
    ``album`` that stand in for them by ``proof``, adding each to
    ``carried``, then dispose the backup once it holds nothing else the
    album lacks."""
    pairs = []
    if not _classify_backup_contents(
            album, candidate.path, audio_replacement=True, carry_lyrics=True,
            pairs=pairs, **proof).removable:
        return False
    if not all(_carry_lyrics(source, destination, carried)
               for source, destination in pairs):
        return False
    return _dispose_retention_candidate(
        candidate, replacement=album, carry_lyrics=True, **proof)


def removal_refusal(path):
    """Why Remove keeps this backup, from a full check without the listing's
    read limit. The listing's cached answer goes, and until its files change
    the row says this instead of that it was not checked."""
    global _only_copy_cache
    path = Path(path)
    if (top := _leftover_top(path)) is not None:
        path, origin = _leftover_root(top), _leftover_origin(top)
    else:
        origin = _read_backup_origin(path)
    result = _classify_retained_backup(
        path, origin, _BackupReadBudget(sys.maxsize), compare_audio=True)
    with _only_copy_lock:
        _only_copy_cache = None
        _removal_refusals[path] = (_files_signature(path, origin), result)
    return result


def album_tree_size(path):
    """(file count, byte total) for one album folder, or None if unreadable.

    Restore refuses when the album folder already holds at least as much as
    the backup, which is what an upgrade that already landed looks like. The
    page needs the same two figures to say so in words instead of sending the
    user to the log.
    """
    root = os.fspath(path)
    files = 0
    total = 0
    try:
        for parent, _dirs, names in os.walk(root):
            for name in names:
                # The app's own sidecars at the backup root are not tracks,
                # and the guard's manifest does not count them either.
                if parent == root and name in _SIDECARS:
                    continue
                try:
                    value = os.stat(os.path.join(parent, name),
                                    follow_symlinks=False)
                except OSError:
                    return None
                if not stat.S_ISREG(value.st_mode):
                    continue
                files += 1
                total += int(value.st_size)
    except OSError:
        return None
    return files, total


def files_replaced_in_album(backup) -> set:
    """The files of this backup whose place in its album now holds another
    file, as a refill that landed there does."""
    origin = Path(backup.receipt["origin"])
    return {relative for relative, (size, digest)
            in backup_payload(backup.receipt).items()
            if (origin / relative).is_file()
            and not _matches(origin / relative, size, digest)}


def album_folder_replaced(backup) -> bool:
    """Whether the album's folder is now another folder than the one this
    backup was taken from, as a later download makes it."""
    try:
        recorded = backup.receipt["source_receipt"]["path_generations"][-1:]
        origin = Path(backup.receipt["origin"])
    except (KeyError, TypeError, ValueError):
        return False
    opened = _open_rooted_directory(cfg.MUSIC_ROOT, origin)
    if opened is None:
        return False
    descriptors = opened[-1]
    try:
        return _incarnations(_path_directory_generations(descriptors[-1:])) != (
            _incarnations(recorded))
    except (OSError, TypeError, ValueError, IndexError):
        return False
    finally:
        _close_descriptors(descriptors)


def files_not_back_in_album(path, *, compare=False, exact=False):
    """(missing, different) for the files this backup holds: how many have
    nothing at their place in the album it came from, nor a file of the same
    size under another name, so the backup is their only copy, and with
    ``compare`` how many have a different file there. With ``exact`` a file
    under another name counts only when its bytes are the same.

    None when that can't be told: the album isn't known or the backup can't
    be read. Beside its album folder only a gap-fill, repair or downsample
    backup, or an unfinished upgrade backup that no later backup of the
    album replaced, counts: a finished one's album holds the new download,
    whose names may differ, and a download an upgrade set aside holds none
    of the user's files.
    """
    counts = _files_in_album(path, compare=compare, exact=exact)
    return None if counts is None else counts[:2]


def files_back_under_new_names(path) -> bool:
    """Whether every file this backup holds that is missing by name from its
    album is there, byte for byte, under another name."""
    counts = _files_in_album(path, exact=True)
    return counts is not None and counts[0] == 0 and counts[2] > 0


def _files_in_album(path, *, compare=False, exact=False):
    """(missing, different, renamed) for files_not_back_in_album."""
    if (top := _leftover_top(path)) is not None:
        target, origin = _leftover_root(top), _leftover_origin(top)
        if beside := _beside_held(top):
            return len(beside), 0, 0
    else:
        target = Path(path)
        origin = _read_backup_origin(target) or _intended_origin(target)
    if origin is None:
        return None
    # What is left of a backup has no Restore, so every file of it counts.
    if top is None and origin.is_dir() and not _kept_by_file(target) and (
            not _unfinished_upgrade(target)
            or _superseded(target, origin)):
        return 0, 0, 0
    entries = _list_tree(target)
    if entries is None:
        return None
    missing = different = renamed = 0
    album_sizes = None
    try:
        for source in entries:
            relative = source.relative_to(target)
            if (
                not stat.S_ISREG(source.lstat().st_mode)
                or len(relative.parts) == 1 and source.name in _SIDECARS
            ):
                continue
            destination = origin / relative
            if not destination.is_file():
                if album_sizes is None:
                    album_sizes = {}
                    for entry in _list_tree(origin) or ():
                        info = entry.lstat()
                        if stat.S_ISREG(info.st_mode):
                            album_sizes.setdefault(
                                info.st_size, []).append(entry)
                if any(
                        not exact
                        or filecmp.cmp(source, entry, shallow=False)
                        for entry in album_sizes.get(
                            source.stat().st_size, ())):
                    renamed += 1
                else:
                    missing += 1
            elif compare and not filecmp.cmp(
                    source, destination, shallow=False):
                different += 1
    except OSError:
        return None
    return missing, different, renamed


def album_folder_gone(path) -> bool:
    """Whether the album folder a finished backup was taken from, or for a
    consolidation the album it kept, is gone from a music folder that still
    holds other folders: it was deleted, moved or renamed, not unmounted.
    Nothing can prove such a backup redundant again. Metadata only."""
    path = Path(path)
    if (_leftover_top(path) is not None or _unfinished_upgrade(path)
            or (path / _UPGRADE_PENDING_SENTINEL).exists()):
        return False
    named = consolidated_into(path)
    album = named[0] if named else _read_backup_origin(path)
    root = Path(cfg.MUSIC_ROOT)
    if album is None or os.path.lexists(album) or not album.is_relative_to(root):
        return False
    try:
        with os.scandir(root) as entries:
            return any(entry.is_dir(follow_symlinks=False)
                       and not scanner.is_library_system_folder(entry.name)
                       for entry in entries)
    except OSError:
        return False


def discard_backup_unchecked(path, *, album_gone=False) -> bool:
    """Delete a kept backup without checking anything, because the user said so.

    Every automatic route can refuse: an unreadable recovery record, files
    that are not byte-identical at the origin, an album folder that now holds
    more than the backup does. Each refusal is right, and together they left
    the row a permanent dead end with a standing alarm beside it. This is the
    deliberate override behind an explicit warning, not a shortcut anything
    else may take. A backup holding files the album doesn't have is their
    only copy, so it is never deleted this way, unless with ``album_gone``
    the user was told its album folder is gone and that it may be the only
    copy, and no saved recovery operation owns it.
    """
    target = Path(path)
    if album_gone:
        candidate = load_backup_result(target)
        if (not album_folder_gone(target) or candidate is None
                or not _backup_owner_authorized(candidate, None)):
            return False
    else:
        counts = files_not_back_in_album(target, exact=True)
        if counts is not None and counts[0]:
            return False
    if (top := _leftover_top(target)) is not None:
        snapshot = snapshot_tree(top)
        return snapshot is not None and _delete_backup_folder(top, snapshot)
    snapshot = snapshot_tree(target)
    return snapshot is not None and _delete_backup_folder(
        target, snapshot, _hidden_name(target.name))


def is_disposal_leftover(path) -> bool:
    """Whether this is what is left of a backup whose deletion did not
    finish. Every listing takes it as that first, whatever it holds."""
    return Path(path).name.startswith(_LEFTOVER_PREFIX)


def _leftover_top(path):
    """The folder in the backup folder that a backup being deleted was
    renamed to, given it or the backup inside it, else None."""
    path = Path(os.path.abspath(os.fspath(path)))
    if path.name == "held" and is_disposal_leftover(path.parent):
        path = path.parent
    if (is_disposal_leftover(path) and path.parent
            == Path(os.path.abspath(os.fspath(cfg.UPGRADE_BACKUP_DIR)))):
        return path
    return None


def _leftover_root(top) -> Path:
    # Version 1.3.4 and earlier left the backup inside, as "held", beside a
    # manifest.
    held = Path(top) / "held"
    if (held.is_dir() and not held.is_symlink()
            and not os.path.lexists(Path(top) / _RECEIPT_SIDECAR)):
        return held
    return Path(top)


def _read_small_json(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        size = os.fstat(descriptor).st_size
        if size > _RECORD_READ_BYTES:
            return None
        return decode_recovery_json(os.pread(descriptor, size + 1, 0),
                                    max_bytes=_RECORD_READ_BYTES)
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        os.close(descriptor)


def _leftover_manifest(top) -> dict:
    """What version 1.3.4 recorded beside the backup it was deleting."""
    value = _read_small_json(Path(top) / ".ql_disposal_manifest")
    return value if isinstance(value, dict) else {}


def _leftover_receipt(top):
    root = _leftover_root(top)
    receipt = _read_small_json(root / _RECEIPT_SIDECAR)
    if not _backup_receipt_value_schema_valid(receipt) and root != Path(top):
        receipt = _leftover_manifest(top).get("receipt")
    return receipt if _backup_receipt_value_schema_valid(receipt) else None


def _leftover_origin(top):
    origin = _read_backup_origin(_leftover_root(top))
    if origin is None and (receipt := _leftover_receipt(top)) is not None:
        origin = Path(receipt["origin"])
    return origin


def is_repair_backup(path) -> bool:
    """Whether the backup at ``path`` holds the damaged tracks a Repair
    replaced."""
    return bool(re.match(r"^\d{8}_\d{6}_\d{6}_repair_", backup_name(path)))


def backup_name(path) -> str:
    """The name the backup at ``path`` is listed under, or was before a
    deletion of it was cut short."""
    top = _leftover_top(path)
    if top is None:
        return Path(path).name
    if _leftover_root(top) != top:
        name = _leftover_manifest(top).get("carrier_name")
        return name if isinstance(name, str) and name else top.name
    return top.name[len(_LEFTOVER_PREFIX):]


def removal_candidate(path):
    """The backup Remove checks at ``path``.

    What is left of one whose deletion did not finish is checked as a backup
    of its kind, from the record it still holds; the file list that record
    sealed no longer matches, so it is not compared. None without a readable
    record."""
    top = _leftover_top(path)
    if top is None:
        return load_backup_result(path)
    receipt = _leftover_receipt(top)
    if receipt is None:
        return None
    root = _leftover_root(top)
    try:
        descriptor = _open_backup_directory(root)
    except OSError:
        return None
    try:
        complete = _effective_backup_complete(descriptor, receipt)
    finally:
        os.close(descriptor)
    if complete is None:
        return None
    return BackupResult(root, complete=complete, receipt=receipt,
                        requested=receipt["requested"],
                        backed_up=receipt["backed_up"])


def _backed_up_entries(root):
    """What below ``root`` is neither a folder nor one of its own markers,
    by lstat, or None when it can't be read."""
    entries = snapshot_tree(root)
    return None if entries is None else {
        relative: entry for relative, entry in entries.items()
        if not stat.S_ISDIR(entry[2]) and relative not in (
            _RECEIPT_SIDECAR, *_SIDECARS, *_OPTIONAL_RECEIPT_MARKERS)}


def _beside_held(top) -> list:
    """What a 1.3.4 leftover holds beside its backup and manifest, which no
    record covers."""
    if _leftover_root(top) == Path(top):
        return []
    try:
        return sorted(name for name in os.listdir(top)
                      if name not in ("held", ".ql_disposal_manifest"))
    except OSError:
        return ["."]


def _unrecorded_file(backup):
    """The first file in what is left of a backup that its record does not
    list at that size, else None."""
    if beside := _beside_held(_leftover_top(backup.path)):
        return beside[0]
    files = backup.receipt["tree"].get("files", {})
    entries = _backed_up_entries(backup.path)
    if entries is None or not isinstance(files, dict):
        return "."
    for relative, entry in sorted(entries.items()):
        recorded = files.get(relative)
        if (not stat.S_ISREG(entry[2]) or not isinstance(recorded, dict)
                or recorded.get("size") != entry[3]):
            return relative
    return None


def leftover_holds_originals(path) -> bool:
    """Whether what is left of a backup is a downsample's kept originals."""
    top = _leftover_top(path)
    return top is not None and (
        _leftover_root(top) / _REAP_AFTER_RETENTION_SENTINEL).is_file()


def cleanup_old_upgrade_backups(retention_days: int | None = None,
                                force: bool = False) -> int:
    """Sweep upgrade-backup dir of anything older than retention_days.
    Called at startup and, in the Web app, on a background tick, so a
    container that never restarts still expires them. Returns count of
    dirs removed.

    Parses the timestamp prefix encoded in each backup dir's
    name (YYYYMMDD_HHMMSS_safe) instead of stat().st_mtime. shutil.move
    preserves the source's mtime, so a fresh backup of an old folder
    inherited that old mtime - and was being auto-deleted on the very
    next run despite being just minutes old. Skips (does not delete)
    backups whose names don't parse (legacy / hand-named / hand-restored).

    Stamps DATA_DIR/.last_backup_sweep and skips a re-sweep within 24h
    unless ``force=True`` - a CLI session that opens and closes ten times
    in a minute shouldn't stat the whole backup dir each time.
    """
    if retention_days is None:
        retention_days = cfg.UPGRADE_BACKUP_RETENTION_DAYS
    if not cfg.UPGRADE_BACKUP_DIR.exists():
        return 0
    sweep_stamp = cfg.DATA_DIR / ".last_backup_sweep"
    if not force and sweep_stamp.exists():
        try:
            if 0 <= (time.time() - sweep_stamp.stat().st_mtime) < 86400:
                return 0
        except OSError:
            pass
    cutoff = time.time() - (retention_days * 86400)
    n_removed = 0
    for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
        if not entry.is_dir() or is_disposal_leftover(entry):
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
                # rewrite ON PURPOSE - the redundancy proof below can never
                # pass for it, and age alone is its whole contract.
                candidate = load_backup_result(entry)
                refused = []
                if candidate is None:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Keeping undo backup {entry.name!r}: its "
                        "app-owned recovery record was unavailable."))
                elif _dispose_retention_candidate(
                        candidate, allow_smaller_audio=True,
                        match_recordings=True, refused=refused):
                    n_removed += 1
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Keeping undo backup {entry.name!r}: "
                        + (_kept_for_lyrics(refused)
                           or "safe disposal could not be completed") + "."))
                continue
            if (
                (entry / _CONSOLIDATED_SENTINEL).is_file()
                and not backup_keep_markers_present(entry)
            ):
                candidate = load_backup_result(entry)
                refused = []
                if (
                    candidate is not None
                    and _dispose_consolidation_backup(candidate, refused)
                ):
                    n_removed += 1
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Keeping consolidation backup {entry.name!r}: "
                        + (_kept_for_lyrics(refused)
                           or "the album it kept could not be shown to hold "
                           "a track for each of its files") + "."))
                continue
            candidate = load_backup_result(entry)
            if candidate is None:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Keeping backup {entry.name!r}: its "
                    "app-owned recovery record was unavailable."))
                continue
            refused = []
            if (
                _backup_safe_to_reap(entry)
                and _dispose_retention_candidate(candidate, refused=refused)
            ):
                n_removed += 1
            else:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Keeping backup {entry.name!r} past retention: "
                    + (_kept_for_lyrics(refused)
                       or "it is pinned or its replacement could not be "
                       "verified") + "."))
    try:
        sweep_stamp.parent.mkdir(parents=True, exist_ok=True)
        sweep_stamp.touch()
    except OSError:
        pass
    return n_removed
