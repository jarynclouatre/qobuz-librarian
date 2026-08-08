"""Exact receipts for staging files and parked import trees."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.recovery import (
    decode_recovery_json,
    normalise_recovery_owner,
    recovery_owner_matches,
)

_MANIFEST = ".qobuz-librarian-retry.json"
_RENAME_NOREPLACE = 1
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_TREE_ENTRIES = 100_000
_RUN_DIR_RE = re.compile(r"^\.qobuz-run-[0-9a-f]{24}$")
_TREE_GROUP_KINDS = {"beets", "quality", "interrupted"}
_FILE_GROUP_KINDS = {
    "rejected",
    "untagged",
    "unimported",
    "unresolved",
    "legacy-rejected",
}


class StagingReferenceStatus(str, Enum):
    """Read-only state of one exact persisted staging reference."""

    MATCH = "match"
    ABSENT = "absent"
    CHANGED = "changed"
    UNAVAILABLE = "unavailable"


def isolated_staging_run_names() -> tuple[str, ...]:
    """List private run-root names so restart admission can fail closed."""
    staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
    try:
        descriptor = os.open(staging, _directory_flags())
    except FileNotFoundError:
        return ()
    try:
        return tuple(sorted(name for name in os.listdir(descriptor) if _RUN_DIR_RE.fullmatch(name)))
    finally:
        os.close(descriptor)


def _identity(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _same_node(left, right):
    return left[:2] == right[:2]


def _same_file_after_move(before, after):
    # rename(2) changes ctime on the same inode. Everything else remains part
    # of the sealed file identity and must still agree.
    return before[:5] == after[:5]


def _name_missing_at(parent_fd, name):
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _absolute_staging_path(path):
    staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = candidate.relative_to(staging)
    except ValueError:
        raise OSError(f"{candidate}: path is outside staging") from None
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise OSError(f"{candidate}: invalid staging path")
    return staging, candidate, relative


def _directory_flags():
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow directory access is unavailable")
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _regular_flags():
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file access is unavailable")
    return os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_staging_chain(path, *, directory):
    staging, candidate, relative = _absolute_staging_path(path)
    chain = []
    leaf_fd = None
    try:
        chain.append(os.open(staging, _directory_flags()))
        stop = len(relative.parts) if directory else len(relative.parts) - 1
        for part in relative.parts[:stop]:
            chain.append(os.open(part, _directory_flags(), dir_fd=chain[-1]))
        if directory:
            leaf_fd = chain[-1]
        else:
            leaf_fd = os.open(relative.parts[-1], _regular_flags(), dir_fd=chain[-1])
        if not _chain_is_named(staging, relative, chain, directory=directory):
            raise OSError(f"{candidate}: directory chain changed")
        return staging, candidate, relative, chain, leaf_fd
    except BaseException:
        if leaf_fd is not None and (not directory or leaf_fd not in chain):
            try:
                os.close(leaf_fd)
            except OSError:
                pass
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _chain_is_named(staging, relative, chain, *, directory):
    try:
        held_root = os.fstat(chain[0])
        named_root = os.stat(staging, follow_symlinks=False)
        if not stat.S_ISDIR(named_root.st_mode) or not _same_node(
            _identity(held_root), _identity(named_root)
        ):
            return False
        directory_parts = relative.parts if directory else relative.parts[:-1]
        for index, part in enumerate(directory_parts, start=1):
            held = os.fstat(chain[index])
            named = os.stat(part, dir_fd=chain[index - 1], follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode) or not _same_node(_identity(held), _identity(named)):
                return False
        return True
    except (OSError, IndexError):
        return False


def _close_chain(chain, leaf_fd=None):
    if leaf_fd is not None and leaf_fd not in chain:
        try:
            os.close(leaf_fd)
        except OSError:
            pass
    for descriptor in reversed(chain):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _inspect_staging_entry(path, *, directory):
    """Classify one named staging entry without following path symlinks."""
    chain = []
    leaf_fd = None
    try:
        staging, _candidate, relative = _absolute_staging_path(path)
        try:
            chain.append(os.open(staging, _directory_flags()))
        except FileNotFoundError:
            try:
                os.stat(staging, follow_symlinks=False)
            except FileNotFoundError:
                return StagingReferenceStatus.ABSENT, None
            except OSError:
                return StagingReferenceStatus.UNAVAILABLE, None
            return StagingReferenceStatus.UNAVAILABLE, None

        root_relative = PurePosixPath()
        if not _chain_is_named(staging, root_relative, chain, directory=True):
            return StagingReferenceStatus.UNAVAILABLE, None

        for index, part in enumerate(relative.parts[:-1], start=1):
            parent_relative = PurePosixPath(*relative.parts[: index - 1])
            try:
                descriptor = os.open(part, _directory_flags(), dir_fd=chain[-1])
            except FileNotFoundError:
                if _chain_is_named(
                    staging, parent_relative, chain, directory=True
                ) and _name_missing_at(chain[-1], part):
                    return StagingReferenceStatus.ABSENT, None
                return StagingReferenceStatus.UNAVAILABLE, None
            except OSError:
                return StagingReferenceStatus.UNAVAILABLE, None
            chain.append(descriptor)
            current_relative = PurePosixPath(*relative.parts[:index])
            if not _chain_is_named(staging, current_relative, chain, directory=True):
                return StagingReferenceStatus.UNAVAILABLE, None

        parent_relative = PurePosixPath(*relative.parts[:-1])
        name = relative.parts[-1]
        try:
            named = os.stat(name, dir_fd=chain[-1], follow_symlinks=False)
        except FileNotFoundError:
            if _chain_is_named(
                staging, parent_relative, chain, directory=True
            ) and _name_missing_at(chain[-1], name):
                return StagingReferenceStatus.ABSENT, None
            return StagingReferenceStatus.UNAVAILABLE, None
        except OSError:
            return StagingReferenceStatus.UNAVAILABLE, None

        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(named.st_mode):
            return StagingReferenceStatus.CHANGED, _identity(named)
        try:
            flags = _directory_flags() if directory else _regular_flags()
            leaf_fd = os.open(name, flags, dir_fd=chain[-1])
        except FileNotFoundError:
            if _chain_is_named(
                staging, parent_relative, chain, directory=True
            ) and _name_missing_at(chain[-1], name):
                return StagingReferenceStatus.ABSENT, None
            return StagingReferenceStatus.UNAVAILABLE, None
        except OSError:
            return StagingReferenceStatus.UNAVAILABLE, None

        held = os.fstat(leaf_fd)
        held_identity = _identity(held)
        if _identity(named) != held_identity:
            return StagingReferenceStatus.CHANGED, held_identity
        if not _chain_is_named(staging, parent_relative, chain, directory=True):
            return StagingReferenceStatus.UNAVAILABLE, None
        try:
            final_named = os.stat(name, dir_fd=chain[-1], follow_symlinks=False)
        except FileNotFoundError:
            return StagingReferenceStatus.ABSENT, None
        except OSError:
            return StagingReferenceStatus.UNAVAILABLE, None
        if _identity(final_named) != held_identity:
            return StagingReferenceStatus.CHANGED, held_identity
        return StagingReferenceStatus.MATCH, held_identity
    except (OSError, TypeError, ValueError, IndexError):
        return StagingReferenceStatus.UNAVAILABLE, None
    finally:
        _close_chain(chain, leaf_fd)


@dataclass(frozen=True, eq=False)
class StagedFile:
    path: Path
    identity: tuple[int, int, int, int, int, int]

    def __fspath__(self):
        return os.fspath(self.path)

    def __str__(self):
        return str(self.path)

    def __hash__(self):
        return hash(self.path)

    def __eq__(self, other):
        if isinstance(other, StagedFile):
            return self.path == other.path and self.identity == other.identity
        try:
            return self.path == Path(other)
        except (TypeError, ValueError):
            return False

    def __getattr__(self, name):
        return getattr(self.path, name)


@dataclass(frozen=True)
class TreeReceipt:
    path: Path
    original_relative: str
    directories: tuple[tuple[str, tuple[int, int, int, int, int, int]], ...]
    files: tuple[tuple[str, tuple[int, int, int, int, int, int]], ...]

    @property
    def root_identity(self):
        return dict(self.directories)[""]

    def moved_to(self, path):
        current = capture_tree(path, original_relative=self.original_relative)
        if current is None or not self.same_contents(current, moved=True):
            return None
        return current

    def same_contents(self, other, *, moved=False):
        if not isinstance(other, TreeReceipt):
            return False
        left_dirs = dict(self.directories)
        right_dirs = dict(other.directories)
        if set(left_dirs) != set(right_dirs) or self.files != other.files:
            return False
        if moved:
            if not _same_file_after_move(left_dirs[""], right_dirs[""]):
                return False
            left_dirs.pop("")
            right_dirs.pop("")
        return left_dirs == right_dirs


@dataclass(frozen=True)
class ParkedGroup:
    path: Path
    kind: str
    trees: tuple[TreeReceipt, ...]
    owner: dict | None = None


@dataclass(frozen=True)
class FileGroup:
    path: Path
    kind: str
    retained: StagedFile
    original: StagedFile | None
    owner: dict | None = None


@dataclass(frozen=True, eq=False)
class QuarantineResult(os.PathLike):
    """Current location and durability of one quarantined staging file."""

    path: Path
    durable: bool

    def __fspath__(self):
        return os.fspath(self.path)

    def __str__(self):
        return str(self.path)

    def __truediv__(self, value):
        return self.path / value

    def __getattr__(self, name):
        return getattr(self.path, name)

    def __eq__(self, other):
        if isinstance(other, QuarantineResult):
            return self.path == other.path
        try:
            return self.path == Path(other)
        except (TypeError, ValueError):
            return False

    def __hash__(self):
        return hash(self.path)


@dataclass(frozen=True)
class RetryGroupInspection:
    """Read-only status for one private retry child."""

    path: Path
    status: str
    kind: str | None = None
    owner: dict | None = None
    planned_trees: tuple[TreeReceipt, ...] = ()
    current_trees: tuple[TreeReceipt | None, ...] = ()
    file_group: FileGroup | None = None
    reason: str = ""


@dataclass(frozen=True)
class StagingRun:
    path: Path
    root_identity: tuple[int, int, int, int, int, int]
    owner: dict | None = None

    def to_record(self):
        record = {
            "path": str(self.path),
            "root_identity": list(self.root_identity),
        }
        if self.owner is not None:
            record.update(
                {
                    "version": 2,
                    "owner": normalise_recovery_owner(self.owner),
                }
            )
        return record


@dataclass(frozen=True, slots=True)
class StagingReferenceInspection:
    status: StagingReferenceStatus
    evidence: TreeReceipt | ParkedGroup | FileGroup | None = None
    reason: str = ""


class StagingSnapshot(set):
    def __init__(self, files=()):
        super().__init__(entry.path for entry in files)
        self.identities = {entry.path: entry.identity for entry in files}


def capture_file(path, *, expected=None):
    chain = []
    leaf_fd = None
    try:
        staging, candidate, relative, chain, leaf_fd = _open_staging_chain(path, directory=False)
        held = os.fstat(leaf_fd)
        named = os.stat(relative.parts[-1], dir_fd=chain[-1], follow_symlinks=False)
        frozen = _identity(held)
        if (
            not stat.S_ISREG(held.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or held.st_nlink != 1
            or named.st_nlink != 1
            or _identity(named) != frozen
            or (expected is not None and frozen != expected)
            or not _chain_is_named(staging, relative, chain, directory=False)
        ):
            return None
        return StagedFile(candidate, frozen)
    except (OSError, TypeError, ValueError):
        return None
    finally:
        _close_chain(chain, leaf_fd)


def _capture_tree_at(directory_fd, relative, directories, files):
    if len(directories) + len(files) >= _MAX_TREE_ENTRIES:
        raise OSError("staging tree is too large to seal")
    directories.append((relative, _identity(os.fstat(directory_fd))))
    for name in sorted(os.listdir(directory_fd)):
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        child_relative = PurePosixPath(relative, name).as_posix()
        if relative == "":
            child_relative = name
        if stat.S_ISDIR(value.st_mode):
            child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
            try:
                if not _same_node(_identity(value), _identity(os.fstat(child_fd))):
                    raise OSError("staging directory changed while sealing")
                _capture_tree_at(child_fd, child_relative, directories, files)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(value.st_mode):
            child_fd = os.open(name, _regular_flags(), dir_fd=directory_fd)
            try:
                held = os.fstat(child_fd)
                if (
                    value.st_nlink != 1
                    or held.st_nlink != 1
                    or _identity(value) != _identity(held)
                ):
                    raise OSError("staging file changed while sealing")
                files.append((child_relative, _identity(held)))
            finally:
                os.close(child_fd)
        else:
            raise OSError("staging tree contains an unsupported entry")


def capture_tree(path, *, original_relative=None):
    chain = []
    try:
        staging, candidate, relative, chain, _ = _open_staging_chain(path, directory=True)
        directories = []
        files = []
        _capture_tree_at(chain[-1], "", directories, files)
        if not _chain_is_named(staging, relative, chain, directory=True):
            return None
        original = original_relative if original_relative is not None else relative.as_posix()
        return TreeReceipt(
            candidate,
            original,
            tuple(directories),
            tuple(files),
        )
    except (OSError, TypeError, ValueError):
        return None
    finally:
        _close_chain(chain)


def tree_matches(receipt):
    current = capture_tree(receipt.path, original_relative=receipt.original_relative)
    return current is not None and receipt.same_contents(current)


def _rename_noreplace(source_fd, source, destination_fd, destination):
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
    if (
        renameat2(
            source_fd,
            os.fsencode(source),
            destination_fd,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fsdecode(source))


class _RacedStagingEntryRetained(OSError):
    """A raced public entry remains at a reported private recovery name."""


def _named_entry_matches(parent_fd, name, descriptor):
    try:
        held = _identity(os.fstat(descriptor))
        named = _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        return _same_node(held, named)
    except OSError:
        return False


def _open_exact_entry(parent_fd, name):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    path_only = getattr(os, "O_PATH", None)
    if nofollow is None or path_only is None:
        raise OSError("safe exact staging-entry access is unavailable")
    descriptor = os.open(
        name,
        path_only | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        if not _named_entry_matches(parent_fd, name, descriptor):
            raise OSError("staging entry changed while it was opened")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _fsync_move_parents(source_parent_fd, destination_parent_fd):
    for descriptor in dict.fromkeys((source_parent_fd, destination_parent_fd)):
        os.fsync(descriptor)


def _fsync_staging_directory(path):
    staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    chain = []
    try:
        if candidate == staging:
            chain.append(os.open(staging, _directory_flags()))
            held = _identity(os.fstat(chain[-1]))
            named = _identity(os.stat(staging, follow_symlinks=False))
            if not stat.S_ISDIR(named[2]) or not _same_node(held, named):
                raise OSError("staging root changed while it was opened")
        else:
            _, _, _, chain, _ = _open_staging_chain(candidate, directory=True)
        os.fsync(chain[-1])
    finally:
        _close_chain(chain)


def _raced_entry_error(source_path, destination_path, detail):
    return _RacedStagingEntryRetained(
        f"{destination_path}: raced staging entry retained in private staging "
        f"recovery ({detail}); public name {source_path} was not overwritten"
    )


def _restore_raced_entry(
    source_parent_fd,
    source_name,
    destination_parent_fd,
    destination_name,
    *,
    source_path,
    destination_path,
):
    """Restore the exact entry captured by a raced path-based rename.

    The destination name is private and unpredictable, and callers invoke
    this only after proving it was absent immediately before renameat2. This
    preserves the existing threat boundary: it does not try to defend a
    private random name against hostile guessing.
    """
    descriptor = None
    deferred = None
    try:
        try:
            descriptor = _open_exact_entry(destination_parent_fd, destination_name)
        except FileNotFoundError:
            return False
        except OSError as exc:
            if _name_missing_at(destination_parent_fd, destination_name):
                return False
            raise _raced_entry_error(
                source_path, destination_path, "the retained entry could not be inspected"
            ) from exc

        if _name_missing_at(source_parent_fd, source_name):
            try:
                _rename_noreplace(
                    destination_parent_fd,
                    destination_name,
                    source_parent_fd,
                    source_name,
                )
            except BaseException as exc:
                deferred = exc

        restored = (
            _named_entry_matches(source_parent_fd, source_name, descriptor)
            and _name_missing_at(destination_parent_fd, destination_name)
        )
        sync_error = None
        if restored:
            try:
                _fsync_move_parents(source_parent_fd, destination_parent_fd)
            except BaseException as exc:
                sync_error = exc
            if deferred is not None and not isinstance(deferred, OSError):
                if sync_error is not None:
                    deferred.add_note(
                        "the raced staging entry was restored but its parent "
                        "directories could not be committed"
                    )
                raise deferred
            if sync_error is not None and not isinstance(sync_error, OSError):
                raise sync_error
            return sync_error is None

        retained = _named_entry_matches(destination_parent_fd, destination_name, descriptor)
        if retained:
            try:
                _fsync_move_parents(source_parent_fd, destination_parent_fd)
            except BaseException as exc:
                sync_error = exc
            if deferred is not None and not isinstance(deferred, OSError):
                deferred.add_note(
                    f"the raced staging entry remains at {destination_path}"
                )
                raise deferred
            if sync_error is not None and not isinstance(sync_error, OSError):
                sync_error.add_note(
                    f"the raced staging entry remains at {destination_path}"
                )
                raise sync_error
            error = _raced_entry_error(
                source_path, destination_path, "the public name was occupied or unavailable"
            )
            if sync_error is not None:
                error.add_note("the private recovery directory could not be committed")
            if isinstance(deferred, OSError):
                raise error from deferred
            if isinstance(sync_error, OSError):
                raise error from sync_error
            raise error

        if deferred is not None and not isinstance(deferred, OSError):
            deferred.add_note(
                f"inspect both {source_path} and {destination_path}; the raced "
                "staging entry could not be reconciled"
            )
            raise deferred
        error = _raced_entry_error(
            source_path, destination_path, "its exact name could not be reconciled"
        )
        if isinstance(deferred, OSError):
            raise error from deferred
        raise error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_retry_root():
    staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
    staging.mkdir(parents=True, exist_ok=True)
    staging_fd = os.open(staging, _directory_flags())
    retry_name = cfg.BEETS_RETRY_DIR
    try:
        try:
            os.mkdir(retry_name, mode=0o700, dir_fd=staging_fd)
            os.fsync(staging_fd)
        except FileExistsError:
            pass
        retry_fd = os.open(retry_name, _directory_flags(), dir_fd=staging_fd)
        return staging_fd, retry_fd, staging / retry_name
    except BaseException:
        os.close(staging_fd)
        raise


def create_staging_run(*, owner=None, on_created=None):
    """Create one unpredictable output root owned by a single rip operation."""
    owner = normalise_recovery_owner(owner)
    if (owner is None) != (on_created is None):
        raise ValueError("owned staging runs require a creation checkpoint")
    if on_created is not None and not callable(on_created):
        raise ValueError("staging creation checkpoint must be callable")

    staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
    staging.mkdir(parents=True, exist_ok=True)
    staging_fd = os.open(staging, _directory_flags())
    try:
        for _ in range(16):
            name = f".qobuz-run-{secrets.token_hex(12)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=staging_fd)
                os.fsync(staging_fd)
                receipt = capture_tree(staging / name)
                if receipt is None:
                    raise OSError("couldn't seal the staging run directory")
                run = StagingRun(receipt.path, receipt.root_identity, owner)
                if on_created is not None:
                    try:
                        on_created(run.to_record())
                    except BaseException as exc:
                        removed = False
                        try:
                            named = os.stat(
                                name,
                                dir_fd=staging_fd,
                                follow_symlinks=False,
                            )
                            if _identity(named) == receipt.root_identity:
                                os.rmdir(name, dir_fd=staging_fd)
                                os.fsync(staging_fd)
                                removed = True
                        except OSError:
                            pass
                        if not removed:
                            exc.add_note(
                                "the uncheckpointed staging run changed; its "
                                "private root was retained for review"
                            )
                        raise
                return run
            except FileExistsError:
                continue
        raise OSError("couldn't allocate a staging run directory")
    finally:
        os.close(staging_fd)


def staging_run_from_record(value):
    if not isinstance(value, dict):
        return None
    owner = None
    if set(value) == {"path", "root_identity"}:
        pass
    elif set(value) == {"version", "path", "root_identity", "owner"}:
        if type(value.get("version")) is not int or value["version"] != 2:
            return None
        try:
            owner = normalise_recovery_owner(value.get("owner"))
        except ValueError:
            return None
        if owner != value.get("owner"):
            return None
    else:
        return None
    identity = value.get("root_identity")
    if (
        not isinstance(identity, list)
        or len(identity) != 6
        or not all(type(part) is int for part in identity)
    ):
        return None
    try:
        _staging, path, relative = _absolute_staging_path(value["path"])
    except (OSError, TypeError, ValueError):
        return None
    if len(relative.parts) != 1 or not _RUN_DIR_RE.fullmatch(path.name):
        return None
    return StagingRun(path, tuple(identity), owner)


def bind_unclaimed_staging_run(name):
    """Bind one journal-less run root so restart recovery can retain it."""
    if not isinstance(name, str) or _RUN_DIR_RE.fullmatch(name) is None:
        return None
    staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
    receipt = capture_tree(staging / name)
    if receipt is None:
        return None
    return StagingRun(receipt.path, receipt.root_identity, None)


def capture_staging_run(run):
    if isinstance(run, dict):
        run = staging_run_from_record(run)
    if not isinstance(run, StagingRun):
        return None
    current = capture_tree(run.path)
    if (
        current is None
        # A rip necessarily changes the directory's size and timestamps.
        # Its device, inode, type, and permissions must remain the exact
        # run root that we created; the populated tree is sealed below.
        or run.root_identity[:3] != current.root_identity[:3]
    ):
        return None
    return current


def inspect_staging_run_reference(reference, expected_owner):
    """Inspect one exact owned run root without mutating staging."""
    unavailable = StagingReferenceInspection(StagingReferenceStatus.UNAVAILABLE, reason="reference")
    try:
        expected_owner = normalise_recovery_owner(expected_owner)
    except ValueError:
        return unavailable
    run = staging_run_from_record(reference)
    if (
        expected_owner is None
        or type(reference) is not dict
        or not isinstance(reference.get("path"), str)
        or not os.path.isabs(reference["path"])
        or os.path.abspath(reference["path"]) != reference["path"]
        or run is None
        or str(run.path) != reference["path"]
        or not recovery_owner_matches(run.owner, expected_owner)
    ):
        return unavailable

    current = capture_staging_run(run)
    if current is not None:
        return StagingReferenceInspection(StagingReferenceStatus.MATCH, evidence=current)

    status, identity = _inspect_staging_entry(run.path, directory=True)
    if status is StagingReferenceStatus.ABSENT:
        return StagingReferenceInspection(status, reason="path")
    if status is StagingReferenceStatus.CHANGED:
        return StagingReferenceInspection(status, reason="identity")
    if status is StagingReferenceStatus.MATCH:
        if identity is None or run.root_identity[:3] != identity[:3]:
            return StagingReferenceInspection(StagingReferenceStatus.CHANGED, reason="identity")
        return StagingReferenceInspection(StagingReferenceStatus.UNAVAILABLE, reason="inspection")
    return StagingReferenceInspection(status, reason="inspection")


def _release_tree_exclusions(held):
    for descriptor, exclusion, _identity_record in reversed(held):
        exclusion.close()
        try:
            os.close(descriptor)
        except OSError:
            pass


def _tree_exclusions_intact(held):
    try:
        return all(
            exclusion.intact() and _identity(os.fstat(descriptor)) == identity_record
            for descriptor, exclusion, identity_record in held
        )
    except OSError:
        return False


def _acquire_tree_exclusions(tree):
    """Hold every exact regular-file inode against a late/stuck writer."""
    held = []
    try:
        for relative, identity_record in tree.files:
            path = tree.path / relative
            chain = []
            descriptor = None
            try:
                _, _, _, chain, descriptor = _open_staging_chain(path, directory=False)
                if _identity(os.fstat(descriptor)) != identity_record:
                    _release_tree_exclusions(held)
                    return None
                exclusion = acquire_inode_write_exclusion(descriptor)
                if exclusion is None or not exclusion.intact():
                    if exclusion is not None:
                        exclusion.close()
                    _release_tree_exclusions(held)
                    return None
                held.append((descriptor, exclusion, identity_record))
                descriptor = None
            finally:
                _close_chain(chain, descriptor)
        if not _tree_exclusions_intact(held) or not tree_matches(tree):
            _release_tree_exclusions(held)
            return None
        return held
    except BaseException:
        _release_tree_exclusions(held)
        raise


def retain_staging_run(run, *, label="interrupted", on_intent=None):
    """Move one quiescent exact run tree outside import for later review."""
    if isinstance(run, dict):
        run = staging_run_from_record(run)
    if not isinstance(run, StagingRun):
        return None
    try:
        owner = normalise_recovery_owner(run.owner)
    except ValueError:
        return None
    if (owner is None) != (on_intent is None):
        raise ValueError("owned staging retention requires an intent checkpoint")
    if on_intent is not None and not callable(on_intent):
        raise ValueError("staging retention checkpoint must be callable")

    current = capture_staging_run(run)
    if current is None:
        return None
    exclusions = _acquire_tree_exclusions(current)
    if exclusions is None:
        # A stuck streamrip child may still have a writable descriptor. Leave
        # the hidden run at its exact public name; it remains ineligible for
        # import and can be retained after the writer exits.
        return None
    try:
        return _retain_staging_run_locked(current, label, exclusions, owner, on_intent)
    finally:
        _release_tree_exclusions(exclusions)


def _retain_staging_run_locked(current, label, exclusions, owner, on_intent):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:48] or "interrupted"
    group = _new_private_group(f"{stamp}-{safe}")
    destination_name = f"000-{current.path.name}"
    destination = group / destination_name
    planned = TreeReceipt(
        destination,
        current.original_relative,
        current.directories,
        current.files,
    )

    # This is deliberately unlike the general multi-tree park transaction:
    # an interrupted rip may never get another in-process cleanup pass. Seal
    # and fsync its exact destination receipt before rename(2), so a fatal
    # signal after the move can never create an unmanifested private tree.
    if not _write_manifest(group, "interrupted", [planned], owner=owner):
        try:
            group.rmdir()
        except OSError:
            pass
        return None

    def discard_unused_marker():
        # A completed rollback changes only the root directory ctime. Require
        # the same inode, mode, size, mtime, and every sealed descendant.
        if current.moved_to(current.path) is None:
            return False
        marker = capture_file(group / _MANIFEST)
        if marker is None or not remove_file(marker):
            return False
        try:
            group.rmdir()
            return True
        except OSError:
            return False

    if on_intent is not None:
        try:
            on_intent(_group_intent_record(group, "interrupted", owner))
        except BaseException:
            discard_unused_marker()
            raise

    if not _tree_exclusions_intact(exclusions) or not tree_matches(current):
        discard_unused_marker()
        return None

    try:
        moved = _move_tree(current, group, destination_name)
    except BaseException as exc:
        recovered = load_group(group, kind="interrupted", expected_owner=owner)
        if recovered is not None:
            exc.add_note("the interrupted download is durably recorded in private staging recovery")
        elif not discard_unused_marker():
            exc.add_note(
                "the interrupted download retention stopped in a safe "
                "refusal state; review staging before another import"
            )
        raise

    recovered = load_group(group, kind="interrupted", expected_owner=owner)
    if recovered is not None:
        if not _tree_exclusions_intact(exclusions):
            raise OSError(
                "interrupted download was retained after a writer requested "
                "the sealed files; review its recovery record"
            )
        # Flush both renamed directory entries after the already-durable
        # manifest. If either flush is unavailable, the sealed transaction is
        # still discoverable and refuses unsafe automatic import after restart.
        for directory in (current.path.parent, group):
            chain = []
            try:
                _, _, _, chain, _ = _open_staging_chain(directory, directory=True)
                os.fsync(chain[-1])
            except OSError:
                pass
            finally:
                _close_chain(chain)
        return recovered
    if moved is None:
        discard_unused_marker()
        return None
    # The exact tree did move, but an externally changed marker no longer
    # proves it. Never claim success or roll it into automatic import.
    raise OSError("interrupted download was retained but its recovery receipt changed")


def _new_private_group(prefix):
    staging_fd = retry_fd = None
    try:
        staging_fd, retry_fd, retry_root = _open_retry_root()
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", prefix)[:48] or "staging"
        for _ in range(16):
            name = f"{safe}-{secrets.token_hex(12)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=retry_fd)
                os.fsync(retry_fd)
                return retry_root / name
            except FileExistsError:
                continue
        raise OSError("couldn't allocate a private staging directory")
    finally:
        for descriptor in (retry_fd, staging_fd):
            if descriptor is not None:
                os.close(descriptor)


def _is_private_retry_group(path):
    retry_root = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR))) / cfg.BEETS_RETRY_DIR
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = candidate.relative_to(retry_root)
    except ValueError:
        return False
    return len(relative.parts) == 1 and relative.parts[0] not in ("", ".", "..")


def _move_file(receipt, destination, destination_name=None):
    destination = Path(destination)
    destination_name = destination_name or receipt.path.name
    source_chain = destination_chain = []
    source_fd = None
    move_started = False

    def layout():
        if source_fd is None or not source_chain or not destination_chain:
            return "unchanged"
        try:
            held = _identity(os.fstat(source_fd))
            source_named = _identity(
                os.stat(
                    receipt.path.name,
                    dir_fd=source_chain[-1],
                    follow_symlinks=False,
                )
            )
        except OSError:
            source_named = None
            try:
                held = _identity(os.fstat(source_fd))
            except OSError:
                return "changed"
        try:
            destination_named = _identity(
                os.stat(
                    destination_name,
                    dir_fd=destination_chain[-1],
                    follow_symlinks=False,
                )
            )
        except OSError:
            destination_named = None
        source_exact = source_named == held and _same_file_after_move(receipt.identity, held)
        destination_exact = destination_named == held and _same_file_after_move(
            receipt.identity, held
        )
        if source_exact and _name_missing_at(destination_chain[-1], destination_name):
            return "source"
        if destination_exact and _name_missing_at(source_chain[-1], receipt.path.name):
            return "destination"
        return "changed"

    def restore_source():
        state = layout()
        if state == "source":
            return True
        if state != "destination":
            return False
        try:
            _rename_noreplace(
                destination_chain[-1], destination_name, source_chain[-1], receipt.path.name
            )
            if layout() != "source":
                return False
            _fsync_move_parents(source_chain[-1], destination_chain[-1])
            return True
        except BaseException as exc:
            restored = layout() == "source"
            committed = False
            if restored:
                try:
                    _fsync_move_parents(source_chain[-1], destination_chain[-1])
                    committed = True
                except BaseException as sync_exc:
                    if not isinstance(sync_exc, OSError):
                        raise
            if isinstance(exc, OSError):
                return restored and committed
            if not restored or not committed:
                exc.add_note(
                    "the interrupted staging-file rollback retained the "
                    "exact file at its last proved name"
                )
            raise

    try:
        staging, _, source_relative, source_chain, source_fd = _open_staging_chain(
            receipt.path, directory=False
        )
        held = _identity(os.fstat(source_fd))
        named = _identity(
            os.stat(source_relative.parts[-1], dir_fd=source_chain[-1], follow_symlinks=False)
        )
        if (
            held != receipt.identity
            or named != receipt.identity
            or not _chain_is_named(staging, source_relative, source_chain, directory=False)
        ):
            return None
        _, _, destination_relative, destination_chain, _ = _open_staging_chain(
            destination, directory=True
        )
        if not _name_missing_at(destination_chain[-1], destination_name):
            return None
        move_started = True
        _rename_noreplace(
            source_chain[-1], source_relative.parts[-1], destination_chain[-1], destination_name
        )
        landed = os.stat(destination_name, dir_fd=destination_chain[-1], follow_symlinks=False)
        if not _same_file_after_move(
            receipt.identity, _identity(landed)
        ) or not _same_file_after_move(receipt.identity, _identity(os.fstat(source_fd))):
            raise OSError("staging file changed during move")
        return destination / destination_name
    except BaseException as exc:
        state = layout()
        safe = state in {"source", "unchanged"}
        if state == "destination":
            safe = restore_source()
        elif move_started and state == "changed":
            safe = _restore_raced_entry(
                source_chain[-1],
                receipt.path.name,
                destination_chain[-1],
                destination_name,
                source_path=receipt.path,
                destination_path=destination / destination_name,
            )
        if isinstance(exc, _RacedStagingEntryRetained):
            raise
        if isinstance(exc, (OSError, TypeError, ValueError)):
            return None
        if not safe:
            exc.add_note(
                "the interrupted staging-file move preserved the exact file at its last proved name"
            )
        raise
    finally:
        _close_chain(source_chain, source_fd)
        _close_chain(destination_chain)


def remove_file(receipt):
    receipt = receipt if isinstance(receipt, StagedFile) else capture_file(receipt)
    if receipt is None:
        return False
    private = _new_private_group(".cleanup")
    source_chain = destination_chain = []
    source_fd = None
    exclusion = None
    move_started = False
    moved = False
    completed = False
    durable = False
    restored = False

    def named_identity(parent_fd, name):
        try:
            return _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        except FileNotFoundError:
            return None
        except OSError:
            return False

    def layout():
        if source_fd is None or not source_chain or not destination_chain:
            return "unchanged"
        try:
            held = _identity(os.fstat(source_fd))
        except OSError:
            return "changed"
        if not _same_file_after_move(receipt.identity, held):
            return "changed"
        source_named = named_identity(source_chain[-1], receipt.path.name)
        private_named = named_identity(destination_chain[-1], "entry")
        if source_named == held and private_named is None:
            return "source"
        if private_named == held and source_named != held:
            return "private"
        if moved and private_named is None and source_named != held:
            return "removed"
        return "changed"

    def restore_source():
        state = layout()
        if state == "source":
            return True
        if state != "private":
            return False
        try:
            _rename_noreplace(
                destination_chain[-1],
                "entry",
                source_chain[-1],
                receipt.path.name,
            )
            if layout() != "source":
                return False
            _fsync_move_parents(source_chain[-1], destination_chain[-1])
            return True
        except BaseException as exc:
            recovered = layout() == "source"
            committed = False
            if recovered:
                try:
                    _fsync_move_parents(source_chain[-1], destination_chain[-1])
                    committed = True
                except BaseException as sync_exc:
                    if not isinstance(sync_exc, OSError):
                        raise
            if isinstance(exc, OSError):
                return recovered and committed
            if not recovered or not committed:
                exc.add_note(
                    "the interrupted staging cleanup retained the exact file "
                    "at its last proved name"
                )
            raise

    try:
        staging, _, source_relative, source_chain, source_fd = _open_staging_chain(
            receipt.path, directory=False
        )
        held = _identity(os.fstat(source_fd))
        named = named_identity(source_chain[-1], source_relative.parts[-1])
        if (
            held != receipt.identity
            or named != receipt.identity
            or not _chain_is_named(staging, source_relative, source_chain, directory=False)
        ):
            return False

        exclusion = acquire_inode_write_exclusion(source_fd)
        if exclusion is None:
            return False
        if (
            not exclusion.intact()
            or _identity(os.fstat(source_fd)) != receipt.identity
            or named_identity(source_chain[-1], source_relative.parts[-1]) != receipt.identity
            or not _chain_is_named(staging, source_relative, source_chain, directory=False)
        ):
            return False

        _, _, _, destination_chain, _ = _open_staging_chain(private, directory=True)
        if not exclusion.intact() or layout() != "source":
            return False
        if not _name_missing_at(destination_chain[-1], "entry"):
            return False
        move_started = True
        try:
            _rename_noreplace(
                source_chain[-1],
                source_relative.parts[-1],
                destination_chain[-1],
                "entry",
            )
        except BaseException:
            moved = layout() == "private"
            raise
        moved = layout() == "private"
        if not moved or not exclusion.intact():
            raise OSError("staging file changed during cleanup move")

        private_identity = _identity(os.fstat(source_fd))
        if (
            named_identity(destination_chain[-1], "entry") != private_identity
            or not _same_file_after_move(receipt.identity, private_identity)
            or not exclusion.intact()
        ):
            raise OSError("staging file changed during cleanup move")

        # Commit both sides of the cross-directory rename before destroying
        # its only remaining name. A failed flush rolls the exact entry back.
        _fsync_move_parents(source_chain[-1], destination_chain[-1])

        # The unpredictable mode-0700 directory is now the only name for the
        # exact held inode. The lease still excludes every existing or newly
        # opened writer while the final identity gate and unlink complete.
        if (
            _identity(os.fstat(source_fd)) != private_identity
            or named_identity(destination_chain[-1], "entry") != private_identity
            or not exclusion.intact()
        ):
            raise OSError("staging file changed before cleanup unlink")
        try:
            os.unlink("entry", dir_fd=destination_chain[-1])
        except BaseException:
            completed = named_identity(destination_chain[-1], "entry") is None
            moved = not completed
            raise
        completed = True
        moved = False
        os.fsync(destination_chain[-1])
        durable = True
        return True
    except BaseException as exc:
        state = layout()
        if state == "removed":
            completed = True
            moved = False
        elif not completed and state == "private":
            restored = restore_source()
            moved = layout() == "private"
        elif not completed and move_started and state == "changed":
            restored = _restore_raced_entry(
                source_chain[-1],
                receipt.path.name,
                destination_chain[-1],
                "entry",
                source_path=receipt.path,
                destination_path=private / "entry",
            )
            moved = False
        elif state in {"source", "unchanged"}:
            restored = True
            moved = False
        if isinstance(exc, _RacedStagingEntryRetained):
            raise
        if isinstance(exc, (OSError, TypeError, ValueError)):
            return completed and durable
        if completed:
            exc.add_note("the staging cleanup completed before the interruption was delivered")
        elif not restored:
            exc.add_note(
                "the interrupted staging cleanup retained the exact file in private recovery"
            )
        raise
    finally:
        if exclusion is not None:
            exclusion.close()
        _close_chain(source_chain, source_fd)
        _close_chain(destination_chain)
        if not moved:
            try:
                private.rmdir()
            except OSError:
                pass


def _file_group_kind(label):
    value = re.sub(r"[^a-z0-9-]+", "", str(label).lower().lstrip("."))
    return value if value in _FILE_GROUP_KINDS else "unresolved"


def quarantine_file(receipt, label="rejected", *, owner=None, on_intent=None):
    """Retain one exact file and report its location and final sync state."""
    owner = normalise_recovery_owner(owner)
    if (owner is None) != (on_intent is None):
        raise ValueError("owned file retention requires an intent checkpoint")
    if on_intent is not None and not callable(on_intent):
        raise ValueError("file retention checkpoint must be callable")

    receipt = receipt if isinstance(receipt, StagedFile) else capture_file(receipt)
    if receipt is None:
        return None
    private = _new_private_group(label)
    destination = private / receipt.path.name
    kind = _file_group_kind(label)

    def move_is_durable():
        durable = True
        for directory in (receipt.path.parent, private):
            try:
                _fsync_staging_directory(directory)
            except OSError:
                durable = False
        return durable

    if not _write_file_manifest(private, kind, receipt, destination, owner=owner):
        try:
            private.rmdir()
        except OSError:
            pass
        return None
    if on_intent is not None:
        try:
            on_intent(_group_intent_record(private, kind, owner))
        except BaseException:
            marker = capture_file(private / _MANIFEST)
            if (
                capture_file(receipt.path, expected=receipt.identity) is not None
                and marker is not None
                and remove_file(marker)
            ):
                try:
                    private.rmdir()
                except OSError:
                    pass
            raise
    try:
        moved = _move_file(receipt, private)
    except BaseException:
        # A fatal signal may land after rename but before _move_file reports it.
        # Keep a valid manifested private result; remove the prewritten marker
        # only when the exact original is positively back at its public name.
        if (
            load_file_group(private, kind=kind, expected_owner=owner) is None
            and capture_file(receipt.path, expected=receipt.identity) is not None
        ):
            marker = capture_file(private / _MANIFEST)
            if marker is not None:
                remove_file(marker)
            try:
                private.rmdir()
            except OSError:
                pass
        raise
    if moved is None:
        recovered = load_file_group(private, kind=kind, expected_owner=owner)
        if recovered is not None:
            return QuarantineResult(recovered.retained.path, move_is_durable())
        # Do not erase uncertain private state. The manifest is removed only
        # after proving the exact source was restored to its original name.
        if capture_file(receipt.path, expected=receipt.identity) is None:
            return None
        marker = capture_file(private / _MANIFEST)
        if marker is not None:
            remove_file(marker)
        try:
            private.rmdir()
        except OSError:
            pass
        return None
    # The manifest was durable before the rename. Flush both names' parents so
    # a successful return also makes the private retention durable.
    return QuarantineResult(moved, move_is_durable())


def _move_tree(receipt, destination, destination_name):
    destination = Path(destination)
    source_chain = destination_chain = []
    destination_path = destination / destination_name
    destination_is_private = _is_private_retry_group(destination)
    move_started = False

    def layout():
        if len(source_chain) < 2 or not destination_chain:
            return "unchanged"
        source_exact = receipt.moved_to(receipt.path) is not None
        destination_exact = receipt.moved_to(destination_path) is not None
        if source_exact and _name_missing_at(destination_chain[-1], destination_name):
            return "source"
        if destination_exact and _name_missing_at(source_chain[-2], receipt.path.name):
            return "destination"
        return "changed"

    def restore_source():
        state = layout()
        if state == "source":
            return True
        if state != "destination":
            return False
        try:
            _rename_noreplace(
                destination_chain[-1], destination_name, source_chain[-2], receipt.path.name
            )
            if layout() != "source":
                return False
            _fsync_move_parents(source_chain[-2], destination_chain[-1])
            return True
        except BaseException as exc:
            restored = layout() == "source"
            committed = False
            if restored:
                try:
                    _fsync_move_parents(source_chain[-2], destination_chain[-1])
                    committed = True
                except BaseException as sync_exc:
                    if not isinstance(sync_exc, OSError):
                        raise
            if isinstance(exc, OSError):
                return restored and committed
            if not restored or not committed:
                exc.add_note(
                    "the interrupted staging-tree rollback retained the "
                    "exact tree at its last proved name"
                )
            raise

    try:
        staging, _, source_relative, source_chain, _ = _open_staging_chain(
            receipt.path, directory=True
        )
        if not tree_matches(receipt):
            return None
        _, _, _, destination_chain, _ = _open_staging_chain(destination, directory=True)
        if not _name_missing_at(destination_chain[-1], destination_name):
            return None
        move_started = True
        _rename_noreplace(
            source_chain[-2], source_relative.parts[-1], destination_chain[-1], destination_name
        )
        landed = receipt.moved_to(destination_path)
        if landed is None:
            raise OSError("staging tree changed during move")
        return landed
    except BaseException as exc:
        state = layout()
        safe = state in {"source", "unchanged"}
        if state == "destination":
            safe = restore_source()
        elif move_started and destination_is_private and state == "changed":
            safe = _restore_raced_entry(
                source_chain[-2],
                receipt.path.name,
                destination_chain[-1],
                destination_name,
                source_path=receipt.path,
                destination_path=destination_path,
            )
        if isinstance(exc, _RacedStagingEntryRetained):
            raise
        if isinstance(exc, (OSError, TypeError, ValueError)):
            return None
        if not safe:
            exc.add_note(
                "the interrupted staging-tree move preserved the exact tree at its last proved name"
            )
        raise
    finally:
        _close_chain(source_chain)
        _close_chain(destination_chain)


def _tree_to_json(receipt):
    return {
        "parked": receipt.path.relative_to(cfg.STAGING_DIR).as_posix(),
        "original": receipt.original_relative,
        "directories": [[relative, list(identity)] for relative, identity in receipt.directories],
        "files": [[relative, list(identity)] for relative, identity in receipt.files],
    }


def _tree_from_json(value):
    if not isinstance(value, dict) or set(value) != {"parked", "original", "directories", "files"}:
        return None
    for key in ("parked", "original"):
        raw = value.get(key)
        if (
            not isinstance(raw, str)
            or not raw
            or "\x00" in raw
            or PurePosixPath(raw).is_absolute()
            or any(part in ("", ".", "..") for part in PurePosixPath(raw).parts)
        ):
            return None

    def records(raw):
        if not isinstance(raw, list) or len(raw) > _MAX_TREE_ENTRIES:
            return None
        out = []
        relative_names = set()
        for entry in raw:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not isinstance(entry[1], list)
                or len(entry[1]) != 6
                or not all(type(part) is int for part in entry[1])
            ):
                return None
            relative = entry[0]
            if relative and (
                PurePosixPath(relative).is_absolute()
                or any(part in ("", ".", "..") for part in PurePosixPath(relative).parts)
            ):
                return None
            if relative in relative_names:
                return None
            relative_names.add(relative)
            out.append((relative, tuple(entry[1])))
        return tuple(out)

    directories = records(value["directories"])
    files = records(value["files"])
    if (
        directories is None
        or files is None
        or sum(relative == "" for relative, _identity in directories) != 1
        or set(dict(directories)) & set(dict(files))
    ):
        return None
    return TreeReceipt(
        Path(cfg.STAGING_DIR) / PurePosixPath(value["parked"]),
        value["original"],
        directories,
        files,
    )


def _file_to_json(original, parked):
    return {
        "parked": parked.relative_to(cfg.STAGING_DIR).as_posix(),
        "original": (
            original.path.relative_to(cfg.STAGING_DIR).as_posix() if original is not None else None
        ),
        "identity": list(
            original.identity if original is not None else capture_file(parked).identity
        ),
    }


def _valid_relative_path(value):
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and not PurePosixPath(value).is_absolute()
        and not any(part in ("", ".", "..") for part in PurePosixPath(value).parts)
    )


def _file_plan_from_json(value):
    if not isinstance(value, dict) or set(value) != {"parked", "original", "identity"}:
        return None
    if not _valid_relative_path(value.get("parked")):
        return None
    original = value.get("original")
    if original is not None and not _valid_relative_path(original):
        return None
    identity = value.get("identity")
    if (
        not isinstance(identity, list)
        or len(identity) != 6
        or not all(type(part) is int for part in identity)
    ):
        return None
    parked_path = Path(cfg.STAGING_DIR) / PurePosixPath(value["parked"])
    planned = StagedFile(parked_path, tuple(identity))
    original_receipt = None
    if original is not None:
        original_receipt = StagedFile(
            Path(cfg.STAGING_DIR) / PurePosixPath(original), tuple(identity)
        )
    return planned, original_receipt


def _write_manifest_payload(group, value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > _MAX_MANIFEST_BYTES:
        return False
    directory_fd = descriptor = None
    temporary = f".manifest-{secrets.token_hex(12)}"
    try:
        directory_fd = os.open(group, _directory_flags())
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _rename_noreplace(directory_fd, temporary, directory_fd, _MANIFEST)
        os.fsync(directory_fd)
        return True
    except OSError:
        if directory_fd is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def _group_intent_record(group, kind, owner):
    owner = normalise_recovery_owner(owner)
    status, payload, sealed_manifest, _reason = _inspect_group_manifest_payload(group)
    try:
        tree_plan = _tree_plan_from_payload(group, payload)
        file_plan = _file_plan_from_payload(group, payload)
    except (OSError, TypeError, ValueError, RecursionError):
        tree_plan = file_plan = None
    if (
        owner is None
        or status is not StagingReferenceStatus.MATCH
        or sealed_manifest is None
        or (tree_plan is None) == (file_plan is None)
    ):
        raise OSError("couldn't seal the staging recovery manifest")
    plan = tree_plan if tree_plan is not None else file_plan
    if plan.kind != kind or not recovery_owner_matches(plan.owner, owner):
        raise OSError("staging recovery manifest ownership changed")
    return {
        "version": 2,
        "kind": kind,
        "owner": dict(owner),
        "path": os.fspath(group),
        "sealed_manifest": sealed_manifest,
    }


def _manifest_owner(payload, content_key):
    version = payload.get("version") if isinstance(payload, dict) else None
    if type(version) is not int:
        return False, None
    fields = {"version", "kind", content_key}
    if version == 1:
        return set(payload) == fields, None
    if version != 2 or set(payload) != fields | {"owner"}:
        return False, None
    try:
        owner = normalise_recovery_owner(payload.get("owner"))
    except ValueError:
        return False, None
    return (owner is not None and owner == payload.get("owner")), owner


def _write_manifest(group, kind, trees, *, owner=None):
    owner = normalise_recovery_owner(owner)
    payload = {
        "version": 2 if owner is not None else 1,
        "kind": kind,
        "trees": [_tree_to_json(tree) for tree in trees],
    }
    if owner is not None:
        payload["owner"] = owner
    return _write_manifest_payload(group, payload)


def _write_file_manifest(group, kind, original, parked, *, owner=None):
    if type(kind) is not str or kind not in _FILE_GROUP_KINDS:
        return False
    try:
        entry = _file_to_json(original, parked)
    except (OSError, TypeError, ValueError, AttributeError):
        return False
    owner = normalise_recovery_owner(owner)
    payload = {
        "version": 2 if owner is not None else 1,
        "kind": kind,
        "files": [entry],
    }
    if owner is not None:
        payload["owner"] = owner
    return _write_manifest_payload(group, payload)


def _rollback_parked(group, receipts, parked):
    """Best-effort rollback for an interrupted or incomplete park.

    A signal can arrive after rename(2) succeeded but before _move_tree returns.
    Discover that exact inode at its private destination before rolling the
    already-moved trees back.  A colliding public name is never overwritten;
    in that case the private recovery copy is deliberately retained.
    """
    known_paths = {tree.path for tree in parked}
    for index, receipt in enumerate(receipts):
        candidate = group / f"{index:03d}-{receipt.path.name}"
        if candidate in known_paths:
            continue
        moved = receipt.moved_to(candidate)
        if moved is not None:
            parked.append(moved)
            known_paths.add(candidate)

    restored_all = True
    for tree in reversed(parked):
        destination = Path(cfg.STAGING_DIR) / tree.original_relative
        if (
            destination.exists()
            or not destination.parent.is_dir()
            or _move_tree(tree, destination.parent, destination.name) is None
        ):
            restored_all = False

    if not restored_all:
        return False

    marker = capture_file(group / _MANIFEST)
    if marker is not None and not remove_file(marker):
        return False
    try:
        group.rmdir()
    except OSError:
        # A temporary manifest or other uncertain residue is safer retained.
        return False
    return True


def park_trees(paths, label, *, kind="beets", expected=None, owner=None, on_intent=None):
    owner = normalise_recovery_owner(owner)
    if (owner is None) != (on_intent is None):
        raise ValueError("owned tree parking requires an intent checkpoint")
    if on_intent is not None and not callable(on_intent):
        raise ValueError("tree parking checkpoint must be callable")

    paths = [Path(path) for path in paths]
    if not paths or type(kind) is not str or kind not in _TREE_GROUP_KINDS:
        return None
    receipts = list(expected or ())
    if receipts:
        if [receipt.path for receipt in receipts] != paths:
            return None
        if not all(tree_matches(receipt) for receipt in receipts):
            return None
    else:
        receipts = [capture_tree(path) for path in paths]
        if any(receipt is None for receipt in receipts):
            return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:48] or "album"
    group = _new_private_group(f"{stamp}-{safe}")
    planned = [
        TreeReceipt(
            group / f"{index:03d}-{receipt.path.name}",
            receipt.original_relative,
            receipt.directories,
            receipt.files,
        )
        for index, receipt in enumerate(receipts)
    ]
    parked = []
    try:
        if not _write_manifest(group, kind, planned, owner=owner):
            _rollback_parked(group, receipts, parked)
            return None
        if on_intent is not None:
            on_intent(_group_intent_record(group, kind, owner))
        for index, receipt in enumerate(receipts):
            destination_name = f"{index:03d}-{receipt.path.name}"
            moved = _move_tree(receipt, group, destination_name)
            if moved is None:
                _rollback_parked(group, receipts, parked)
                return None
            parked.append(moved)
    except BaseException:
        _rollback_parked(group, receipts, parked)
        raise
    return ParkedGroup(group, kind, tuple(parked), owner)


def _read_manifest_payload(group):
    group = Path(group)
    directory_fd = descriptor = None
    try:
        retry_root = Path(cfg.STAGING_DIR) / cfg.BEETS_RETRY_DIR
        relative = group.relative_to(retry_root)
        if len(relative.parts) != 1:
            return None
        directory_fd = os.open(group, _directory_flags())
        descriptor = os.open(_MANIFEST, _regular_flags(), dir_fd=directory_fd)
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_size <= 0
            or value.st_size > _MAX_MANIFEST_BYTES
        ):
            return None
        raw = os.pread(descriptor, value.st_size + 1, 0)
        named = os.stat(_MANIFEST, dir_fd=directory_fd, follow_symlinks=False)
        if len(raw) != value.st_size or _identity(named) != _identity(value):
            return None
        return retry_root, relative, decode_recovery_json(raw, max_bytes=_MAX_MANIFEST_BYTES)
    except (OSError, TypeError, ValueError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def _inspect_group_manifest_payload(group):
    """Read one manifest through its held, verified staging directory chain."""
    chain = []
    descriptor = None
    try:
        try:
            staging, _candidate, relative, chain, _ = _open_staging_chain(group, directory=True)
        except (OSError, TypeError, ValueError):
            status, _identity_record = _inspect_staging_entry(group, directory=True)
            return status, None, None, "path"

        try:
            descriptor = os.open(_MANIFEST, _regular_flags(), dir_fd=chain[-1])
        except FileNotFoundError:
            if _chain_is_named(staging, relative, chain, directory=True) and _name_missing_at(
                chain[-1], _MANIFEST
            ):
                return StagingReferenceStatus.CHANGED, None, None, "manifest"
            return StagingReferenceStatus.UNAVAILABLE, None, None, "inspection"
        except OSError:
            try:
                named = os.stat(_MANIFEST, dir_fd=chain[-1], follow_symlinks=False)
            except FileNotFoundError:
                return StagingReferenceStatus.CHANGED, None, None, "manifest"
            except OSError:
                return StagingReferenceStatus.UNAVAILABLE, None, None, "inspection"
            if not stat.S_ISREG(named.st_mode):
                return StagingReferenceStatus.CHANGED, None, None, "manifest"
            return StagingReferenceStatus.UNAVAILABLE, None, None, "inspection"

        before = os.fstat(descriptor)
        before_identity = _identity(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAX_MANIFEST_BYTES
        ):
            return StagingReferenceStatus.CHANGED, None, None, "manifest"
        raw = os.pread(descriptor, before.st_size + 1, 0)
        if len(raw) != before.st_size:
            return StagingReferenceStatus.CHANGED, None, None, "manifest"
        try:
            payload = decode_recovery_json(raw, max_bytes=_MAX_MANIFEST_BYTES)
        except (TypeError, ValueError, RecursionError):
            return StagingReferenceStatus.CHANGED, None, None, "manifest"
        after = os.fstat(descriptor)
        try:
            named = os.stat(_MANIFEST, dir_fd=chain[-1], follow_symlinks=False)
        except FileNotFoundError:
            return StagingReferenceStatus.CHANGED, None, None, "manifest"
        if _identity(after) != before_identity or _identity(named) != before_identity:
            return StagingReferenceStatus.CHANGED, None, None, "manifest"
        if not _chain_is_named(staging, relative, chain, directory=True):
            return StagingReferenceStatus.UNAVAILABLE, None, None, "inspection"
        sealed_manifest = {
            "identity": list(before_identity),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        return StagingReferenceStatus.MATCH, payload, sealed_manifest, ""
    except (OSError, TypeError, ValueError, RecursionError):
        return StagingReferenceStatus.UNAVAILABLE, None, None, "inspection"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _close_chain(chain)


def _tree_plan_from_payload(group, payload):
    valid_manifest, owner = _manifest_owner(payload, "trees")
    kind = payload.get("kind") if isinstance(payload, dict) else None
    if (
        not valid_manifest
        or type(kind) is not str
        or kind not in _TREE_GROUP_KINDS
        or not isinstance(payload.get("trees"), list)
    ):
        return None
    trees = tuple(_tree_from_json(item) for item in payload["trees"])
    if (
        not trees
        or any(tree is None for tree in trees)
        or any(tree.path.parent != group for tree in trees)
        or len({tree.path for tree in trees}) != len(trees)
        or len({tree.original_relative for tree in trees}) != len(trees)
    ):
        return None
    return ParkedGroup(group, kind, trees, owner)


def _file_plan_from_payload(group, payload):
    valid_manifest, owner = _manifest_owner(payload, "files")
    kind = payload.get("kind") if isinstance(payload, dict) else None
    if (
        not valid_manifest
        or type(kind) is not str
        or kind not in _FILE_GROUP_KINDS
        or not isinstance(payload.get("files"), list)
        or len(payload["files"]) != 1
    ):
        return None
    record = _file_plan_from_json(payload["files"][0])
    if record is None:
        return None
    planned, original = record
    if planned.path.parent != group:
        return None
    return FileGroup(group, kind, planned, original, owner)


def _staging_group_reference_parameters(reference, expected_owner):
    try:
        expected_owner = normalise_recovery_owner(expected_owner)
        reference_owner = normalise_recovery_owner(reference.get("owner"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    sealed_manifest = reference.get("sealed_manifest")
    sealed_identity = sealed_manifest.get("identity") if type(sealed_manifest) is dict else None
    sealed_hash = sealed_manifest.get("sha256") if type(sealed_manifest) is dict else None
    if (
        expected_owner is None
        or reference_owner != expected_owner
        or type(reference) is not dict
        or set(reference)
        != {
            "version",
            "kind",
            "owner",
            "path",
            "sealed_manifest",
        }
        or type(reference.get("version")) is not int
        or reference["version"] != 2
        or type(reference.get("kind")) is not str
        or reference["kind"] not in _TREE_GROUP_KINDS | _FILE_GROUP_KINDS
        or not isinstance(reference.get("path"), str)
        or not os.path.isabs(reference["path"])
        or os.path.abspath(reference["path"]) != reference["path"]
        or type(sealed_manifest) is not dict
        or set(sealed_manifest) != {"identity", "sha256"}
        or not isinstance(sealed_identity, list)
        or len(sealed_identity) != 6
        or not all(type(part) is int for part in sealed_identity)
        or not isinstance(sealed_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", sealed_hash) is None
    ):
        return None
    try:
        staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
        retry_root = staging / cfg.BEETS_RETRY_DIR
        group = Path(reference["path"])
        _root, canonical_group, relative = _absolute_staging_path(group)
    except (OSError, TypeError, ValueError):
        return None
    if (
        canonical_group != group
        or group.parent != retry_root
        or len(relative.parts) != 2
        or relative.parts[0] != cfg.BEETS_RETRY_DIR
    ):
        return None
    return (
        group,
        reference["kind"],
        dict(expected_owner),
        {
            "identity": list(sealed_identity),
            "sha256": sealed_hash,
        },
    )


def _inspect_planned_tree(tree):
    current = capture_tree(tree.path, original_relative=tree.original_relative)
    if current is not None:
        if tree.same_contents(current, moved=True):
            return StagingReferenceStatus.MATCH, current
        return StagingReferenceStatus.CHANGED, None
    status, identity = _inspect_staging_entry(tree.path, directory=True)
    if status in {
        StagingReferenceStatus.ABSENT,
        StagingReferenceStatus.CHANGED,
    }:
        return StagingReferenceStatus.CHANGED, None
    if status is StagingReferenceStatus.MATCH:
        if identity is not None and not _same_file_after_move(tree.root_identity, identity):
            return StagingReferenceStatus.CHANGED, None
        return StagingReferenceStatus.UNAVAILABLE, None
    return status, None


def _inspect_planned_file(file):
    current = capture_file(file.path)
    if current is not None:
        if _same_file_after_move(file.identity, current.identity):
            return StagingReferenceStatus.MATCH, current
        return StagingReferenceStatus.CHANGED, None
    status, identity = _inspect_staging_entry(file.path, directory=False)
    if status in {
        StagingReferenceStatus.ABSENT,
        StagingReferenceStatus.CHANGED,
    }:
        return StagingReferenceStatus.CHANGED, None
    if status is StagingReferenceStatus.MATCH:
        if identity is not None and not _same_file_after_move(file.identity, identity):
            return StagingReferenceStatus.CHANGED, None
        return StagingReferenceStatus.UNAVAILABLE, None
    return status, None


def inspect_staging_group_reference(reference, expected_owner):
    """Inspect one exact owned retry group and its manifested contents."""
    parameters = _staging_group_reference_parameters(reference, expected_owner)
    if parameters is None:
        return StagingReferenceInspection(StagingReferenceStatus.UNAVAILABLE, reason="reference")
    group, expected_kind, expected_owner, expected_manifest = parameters

    path_status, _identity_record = _inspect_staging_entry(group, directory=True)
    if path_status is not StagingReferenceStatus.MATCH:
        return StagingReferenceInspection(path_status, reason="path")

    manifest_status, payload, sealed_manifest, reason = _inspect_group_manifest_payload(group)
    if manifest_status is not StagingReferenceStatus.MATCH:
        return StagingReferenceInspection(manifest_status, reason=reason)
    if sealed_manifest != expected_manifest:
        return StagingReferenceInspection(StagingReferenceStatus.CHANGED, reason="manifest")
    try:
        tree_plan = _tree_plan_from_payload(group, payload)
        file_plan = _file_plan_from_payload(group, payload)
    except (OSError, TypeError, ValueError, RecursionError):
        tree_plan = file_plan = None
    if (tree_plan is None) == (file_plan is None):
        return StagingReferenceInspection(StagingReferenceStatus.CHANGED, reason="manifest")
    plan = tree_plan if tree_plan is not None else file_plan
    if plan.kind != expected_kind or not recovery_owner_matches(plan.owner, expected_owner):
        return StagingReferenceInspection(StagingReferenceStatus.CHANGED, reason="manifest")

    if tree_plan is not None:
        current_trees = []
        for tree in tree_plan.trees:
            status, current = _inspect_planned_tree(tree)
            if status is not StagingReferenceStatus.MATCH:
                reason = "contents" if status is StagingReferenceStatus.CHANGED else "inspection"
                return StagingReferenceInspection(status, reason=reason)
            current_trees.append(current)
        evidence = ParkedGroup(
            group,
            tree_plan.kind,
            tuple(current_trees),
            tree_plan.owner,
        )
    else:
        status, current = _inspect_planned_file(file_plan.retained)
        if status is not StagingReferenceStatus.MATCH:
            reason = "contents" if status is StagingReferenceStatus.CHANGED else "inspection"
            return StagingReferenceInspection(status, reason=reason)
        evidence = FileGroup(
            group,
            file_plan.kind,
            current,
            file_plan.original,
            file_plan.owner,
        )
    return StagingReferenceInspection(StagingReferenceStatus.MATCH, evidence=evidence)


def _load_tree_plan(group):
    loaded = _read_manifest_payload(group)
    if loaded is None:
        return None
    _retry_root, _relative, payload = loaded
    return _tree_plan_from_payload(Path(group), payload)


def _retry_path_is_missing(path):
    try:
        os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _inspect_retry_group(group, expected_owner):
    try:
        expected_owner = normalise_recovery_owner(expected_owner)
    except ValueError:
        return RetryGroupInspection(group, "blocked", reason="owner")
    loaded = _read_manifest_payload(group)
    if loaded is None:
        return RetryGroupInspection(group, "malformed", reason="manifest")
    _retry_root, _relative, payload = loaded

    try:
        tree_plan = _tree_plan_from_payload(group, payload)
        file_plan = _file_plan_from_payload(group, payload)
    except (OSError, TypeError, ValueError, RecursionError):
        return RetryGroupInspection(group, "malformed", reason="manifest")
    if (tree_plan is None) == (file_plan is None):
        return RetryGroupInspection(group, "malformed", reason="manifest")
    plan = tree_plan if tree_plan is not None else file_plan
    if expected_owner is not None and not recovery_owner_matches(plan.owner, expected_owner):
        return RetryGroupInspection(group, "blocked", kind=plan.kind, reason="owner")

    if tree_plan is not None:
        current = []
        changed = False
        for tree in tree_plan.trees:
            value = capture_tree(tree.path, original_relative=tree.original_relative)
            if value is None:
                if not _retry_path_is_missing(tree.path):
                    changed = True
                current.append(None)
            elif tree.same_contents(value, moved=True):
                current.append(value)
            else:
                current.append(value)
                changed = True
        status = (
            "blocked"
            if changed
            else "ready"
            if all(value is not None for value in current)
            else "incomplete"
        )
        return RetryGroupInspection(
            group,
            status,
            kind=tree_plan.kind,
            owner=tree_plan.owner,
            planned_trees=tree_plan.trees,
            current_trees=tuple(current),
            reason="changed" if changed else "",
        )

    current = capture_file(file_plan.retained.path)
    if current is None:
        status = "incomplete" if _retry_path_is_missing(file_plan.retained.path) else "blocked"
        return RetryGroupInspection(
            group,
            status,
            kind=file_plan.kind,
            owner=file_plan.owner,
            reason="" if status == "incomplete" else "changed",
        )
    if not _same_file_after_move(file_plan.retained.identity, current.identity):
        return RetryGroupInspection(
            group,
            "blocked",
            kind=file_plan.kind,
            owner=file_plan.owner,
            reason="changed",
        )
    ready = FileGroup(group, file_plan.kind, current, file_plan.original, file_plan.owner)
    return RetryGroupInspection(
        group,
        "ready",
        kind=file_plan.kind,
        owner=file_plan.owner,
        file_group=ready,
    )


def inspect_retry_group(group, *, expected_owner=None):
    """Inspect one retry child without mutating or adopting its contents."""
    try:
        group = Path(group)
        return _inspect_retry_group(group, expected_owner)
    except (OSError, TypeError, ValueError, RecursionError):
        try:
            group = Path(group)
        except (OSError, TypeError, ValueError, RecursionError):
            group = Path(cfg.STAGING_DIR) / cfg.BEETS_RETRY_DIR / ".malformed"
        return RetryGroupInspection(group, "malformed", reason="manifest")


def inspect_retry_groups(*, expected_owner=None):
    """Return the read-only status of every direct retry child."""
    retry_root = Path(cfg.STAGING_DIR) / cfg.BEETS_RETRY_DIR
    try:
        candidates = sorted(retry_root.iterdir())
    except OSError:
        return []
    inspections = []
    for candidate in candidates:
        try:
            inspection = inspect_retry_group(candidate, expected_owner=expected_owner)
        except (OSError, TypeError, ValueError, RecursionError):
            inspection = RetryGroupInspection(candidate, "malformed", reason="manifest")
        inspections.append(inspection)
    return inspections


def load_group(group, *, kind=None, expected_owner=None):
    inspection = inspect_retry_group(group, expected_owner=expected_owner)
    if (
        inspection.status != "ready"
        or not inspection.planned_trees
        or kind is not None
        and inspection.kind != kind
    ):
        return None
    if any(tree is None for tree in inspection.current_trees):
        return None
    return ParkedGroup(
        inspection.path,
        inspection.kind,
        tuple(inspection.current_trees),
        inspection.owner,
    )


def list_groups(*, kind="beets", expected_owner=None):
    retry_root = Path(cfg.STAGING_DIR) / cfg.BEETS_RETRY_DIR
    try:
        candidates = sorted(retry_root.iterdir())
    except OSError:
        return []
    groups = []
    for candidate in candidates:
        group = load_group(candidate, kind=kind, expected_owner=expected_owner)
        if group is not None:
            groups.append(group)
    return groups


def load_file_group(group, *, kind=None, expected_owner=None):
    inspection = inspect_retry_group(group, expected_owner=expected_owner)
    if (
        inspection.status != "ready"
        or inspection.file_group is None
        or kind is not None
        and inspection.kind != kind
    ):
        return None
    return inspection.file_group


def list_file_groups(*, kind=None, expected_owner=None):
    retry_root = Path(cfg.STAGING_DIR) / cfg.BEETS_RETRY_DIR
    try:
        candidates = sorted(retry_root.iterdir())
    except OSError:
        return []
    groups = []
    for candidate in candidates:
        group = load_file_group(candidate, kind=kind, expected_owner=expected_owner)
        if group is not None:
            groups.append(group)
    return groups


def discard_file_group(group, *, expected_owner=None):
    group = load_file_group(
        group.path if isinstance(group, FileGroup) else group,
        expected_owner=expected_owner,
    )
    if (
        group is None
        or (group.owner is not None and expected_owner is None)
        or not remove_file(group.retained)
    ):
        return False
    marker = capture_file(group.path / _MANIFEST)
    if marker is None or not remove_file(marker):
        return False
    try:
        group.path.rmdir()
        return True
    except OSError:
        return False


def discard_quarantined_file(receipt, *, kind="rejected", expected_owner=None):
    receipt = receipt if isinstance(receipt, StagedFile) else None
    if receipt is None:
        return False
    matches = [
        group
        for group in list_file_groups(kind=kind, expected_owner=expected_owner)
        if group.original is not None
        and group.original.path == receipt.path
        and group.original.identity == receipt.identity
    ]
    return len(matches) == 1 and discard_file_group(matches[0], expected_owner=expected_owner)


_LEGACY_FILE_GROUP_RE = re.compile(r"^\.(?P<kind>rejected)-[0-9a-f]{24}$")


def reconcile_legacy_file_groups():
    """Manifest strict old one-file reject groups without deleting payloads."""
    retry_root = Path(cfg.STAGING_DIR) / cfg.BEETS_RETRY_DIR
    try:
        candidates = sorted(retry_root.iterdir())
    except OSError:
        return 0
    reconciled = 0
    for candidate in candidates:
        if not _LEGACY_FILE_GROUP_RE.fullmatch(candidate.name):
            continue
        try:
            value = os.stat(candidate, follow_symlinks=False)
            if (
                not stat.S_ISDIR(value.st_mode)
                or value.st_uid != os.geteuid()
                or stat.S_IMODE(value.st_mode) & 0o077
            ):
                continue
        except OSError:
            continue
        tree = capture_tree(candidate)
        if (
            tree is None
            or len(tree.directories) != 1
            or len(tree.files) != 1
            or tree.files[0][0] == _MANIFEST
        ):
            continue
        relative, identity = tree.files[0]
        retained = capture_file(candidate / relative, expected=identity)
        if retained is None:
            continue
        if _write_file_manifest(candidate, "legacy-rejected", None, retained.path):
            reconciled += 1
    return reconciled


def _remove_empty_directory(path, expected):
    current = capture_tree(path)
    if (
        current is None
        or current.files
        or len(current.directories) != 1
        or not _same_node(current.root_identity, expected)
    ):
        return False
    private = _new_private_group(".empty")
    moved = _move_tree(current, private, "entry")
    if moved is None:
        try:
            private.rmdir()
        except OSError:
            pass
        return False
    try:
        (private / "entry").rmdir()
        private.rmdir()
        return True
    except BaseException as exc:
        retained = capture_tree(private / "entry", original_relative=moved.original_relative)
        restored = False
        completed = False
        private_fd = None
        try:
            private_fd = os.open(private, _directory_flags())
            completed = retained is None and _name_missing_at(private_fd, "entry")
        except FileNotFoundError:
            completed = True
        except OSError:
            pass
        finally:
            if private_fd is not None:
                os.close(private_fd)
        if retained is not None and moved.same_contents(retained, moved=True):
            restored = _move_tree(retained, Path(path).parent, Path(path).name) is not None
        if completed or restored:
            try:
                private.rmdir()
            except OSError:
                pass
        if isinstance(exc, OSError):
            return completed
        if not completed and not restored:
            exc.add_note(
                "the interrupted empty-directory cleanup retained the exact "
                "directory in private recovery"
            )
        raise


def _prune_tree_directories(receipt):
    directories = dict(receipt.directories)
    for relative in sorted(
        directories, key=lambda value: len(PurePosixPath(value).parts), reverse=True
    ):
        path = receipt.path if not relative else receipt.path / relative
        if path.exists() and not _remove_empty_directory(path, directories[relative]):
            return False
    return not receipt.path.exists()


def discard_group(group, *, expected_owner=None):
    group = load_group(
        group.path if isinstance(group, ParkedGroup) else group,
        expected_owner=expected_owner,
    )
    if (
        group is None
        or (group.owner is not None and expected_owner is None)
        or not all(tree_matches(tree) for tree in group.trees)
    ):
        return False
    for tree in group.trees:
        for relative, identity in tree.files:
            receipt = capture_file(tree.path / relative, expected=identity)
            if receipt is None or not remove_file(receipt):
                return False
        if not _prune_tree_directories(tree):
            return False
    marker = capture_file(group.path / _MANIFEST)
    if marker is None or not remove_file(marker):
        return False
    try:
        group.path.rmdir()
        return True
    except OSError:
        return False


def restore_group(group, *, expected_owner=None):
    group = load_group(
        group.path if isinstance(group, ParkedGroup) else group,
        expected_owner=expected_owner,
    )
    if (
        group is None
        or (group.owner is not None and expected_owner is None)
        or not all(tree_matches(tree) for tree in group.trees)
    ):
        return None
    destinations = [Path(cfg.STAGING_DIR) / tree.original_relative for tree in group.trees]
    if any(destination.exists() or not destination.parent.is_dir() for destination in destinations):
        return None
    restored = []

    def rollback():
        complete = True
        for original, moved in reversed(restored):
            try:
                if (
                    original.path.exists()
                    or _move_tree(moved, original.path.parent, original.path.name) is None
                ):
                    complete = False
            except BaseException:
                complete = False
        return complete

    partial_restore = False
    try:
        for tree, destination in zip(group.trees, destinations):
            moved = _move_tree(tree, destination.parent, destination.name)
            if moved is None:
                if not rollback():
                    partial_restore = True
                    break
                return None
            restored.append((tree, moved))
    except BaseException as exc:
        if not rollback():
            exc.add_note(
                "the interrupted staging restore kept every exact tree at "
                "its last proved public or private name"
            )
        raise
    if partial_restore:
        raise OSError(
            "staging group was partially restored and could not be rolled "
            "back; every exact tree remains at its last proved public or "
            "private name"
        )
    marker = capture_file(group.path / _MANIFEST)
    if marker is not None:
        remove_file(marker)
    try:
        group.path.rmdir()
    except OSError:
        pass
    return destinations


def discard_received_audio_trees(paths, files):
    """Discard only retry audio proved by this run, then reclaim empty dirs.

    The current tree is sealed before any mutation.  Unknown companions,
    missing receipts, and changed identities refuse the whole operation so a
    fallback keeps both the first rip and the uncertain retry state.
    """
    receipts = [capture_tree(path) for path in paths]
    if any(receipt is None for receipt in receipts):
        return False

    owned = {
        file.path: file.identity
        for file in files
        if isinstance(file, StagedFile) and file.path.suffix.lower() in cfg.AUDIO_EXTS
    }
    current = {}
    for tree in receipts:
        for relative, identity in tree.files:
            path = tree.path / relative
            if path.suffix.lower() not in cfg.AUDIO_EXTS:
                return False
            current[path] = identity

    if not current or current != owned:
        return False

    sealed = []
    for path, identity in current.items():
        receipt = capture_file(path, expected=identity)
        if receipt is None:
            return False
        sealed.append(receipt)
    for receipt in sealed:
        if not remove_file(receipt):
            return False
    for tree in receipts:
        _prune_tree_directories(tree)
    return all(not tree.path.exists() for tree in receipts)


def _parked_group_matches_plan(group, plan):
    if (
        not isinstance(group, ParkedGroup)
        or not isinstance(plan, ParkedGroup)
        or group.path != plan.path
        or group.kind != plan.kind
        or group.owner != plan.owner
        or len(group.trees) != len(plan.trees)
    ):
        return False
    return all(
        supplied.path == planned.path
        and supplied.original_relative == planned.original_relative
        and planned.same_contents(supplied, moved=True)
        for supplied, planned in zip(group.trees, plan.trees)
    )


def reconcile_imported_group(group, *, expected_owner=None):
    """Reconcile one sealed retry tree after beets returns.

    Only exact recorded audio is allowed to disappear. Replacements, unknown
    additions, missing companions, and partially moved albums are retained as
    manual recovery state; an exact empty husk is reclaimed without recursion.
    """
    if not isinstance(group, ParkedGroup) or len(group.trees) != 1:
        return {"moved_audio": False, "cleared": False, "reason": "invalid"}
    try:
        expected_owner = normalise_recovery_owner(expected_owner)
    except ValueError:
        return {"moved_audio": False, "cleared": False, "reason": "owner"}
    plan = _load_tree_plan(group.path)
    if plan is None or not _parked_group_matches_plan(group, plan):
        return {"moved_audio": False, "cleared": False, "reason": "invalid"}
    if (
        plan.owner is None
        and expected_owner is not None
        or plan.owner is not None
        and not recovery_owner_matches(plan.owner, expected_owner)
    ):
        return {"moved_audio": False, "cleared": False, "reason": "owner"}

    tree = plan.trees[0]
    current = capture_tree(tree.path, original_relative=tree.original_relative)
    if current is None:
        return {"moved_audio": False, "cleared": False, "reason": "changed"}

    expected = dict(tree.files)
    present = dict(current.files)
    unknown = set(present) - set(expected)
    changed = bool(unknown)
    missing_audio = []
    remaining_audio = []
    for relative, identity in expected.items():
        path = tree.path / relative
        current_identity = present.get(relative)
        is_audio = path.suffix.lower() in cfg.AUDIO_EXTS
        if current_identity is None:
            if is_audio:
                missing_audio.append(relative)
            else:
                changed = True
        elif current_identity != identity:
            changed = True
        elif is_audio:
            remaining_audio.append(relative)

    moved_audio = bool(missing_audio)
    if changed:
        return {
            "moved_audio": moved_audio,
            "cleared": False,
            "reason": "changed",
        }
    if remaining_audio:
        return {
            "moved_audio": moved_audio,
            "cleared": False,
            "reason": "tracks",
        }
    if current.files:
        return {
            "moved_audio": moved_audio,
            "cleared": False,
            "reason": "companions",
        }
    marker = capture_file(group.path / _MANIFEST)
    fresh_plan = _load_tree_plan(group.path)
    if (
        marker is None
        or fresh_plan != plan
        or capture_file(group.path / _MANIFEST, expected=marker.identity) is None
    ):
        return {
            "moved_audio": moved_audio,
            "cleared": False,
            "reason": "changed",
        }
    if not moved_audio or not _prune_tree_directories(current):
        return {
            "moved_audio": moved_audio,
            "cleared": False,
            "reason": "changed",
        }

    if not remove_file(marker):
        return {
            "moved_audio": True,
            "cleared": False,
            "reason": "changed",
        }
    try:
        group.path.rmdir()
    except OSError:
        return {
            "moved_audio": True,
            "cleared": False,
            "reason": "changed",
        }
    return {"moved_audio": True, "cleared": True, "reason": ""}
