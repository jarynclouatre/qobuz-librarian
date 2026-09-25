"""Ownership records for downloaded files and their safe removal."""
import ctypes
import errno
import logging
import os
import re
import secrets
import stat
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion

_log = logging.getLogger("qobuz_librarian")


def _file_identity(st) -> list[int]:
    return [int(st.st_dev), int(st.st_ino)]


def _owned_file_identity(st) -> dict[str, int]:
    # Inodes can be recycled after a file is replaced.
    return {
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "modified_ns": int(st.st_mtime_ns),
        "changed_ns": int(st.st_ctime_ns),
    }


def _owned_directory_cleanup_entry(st, *, created) -> dict[str, int | bool]:
    return {
        **_owned_file_identity(st),
        "created": created is True,
    }


_OWNERSHIP_IDENTITY_FIELDS = (
    "device",
    "inode",
    "size",
    "modified_ns",
    "changed_ns",
)


def _ownership_device_inode_matches(st, expected):
    return (
        isinstance(expected, dict)
        and type(expected.get("device")) is int
        and type(expected.get("inode")) is int
        and [expected["device"], expected["inode"]] == _file_identity(st)
    )


def _ownership_identity_matches(st, expected):
    actual = _owned_file_identity(st)
    return (
        isinstance(expected, dict)
        and all(type(expected.get(field)) is int
                for field in _OWNERSHIP_IDENTITY_FIELDS)
        and all(actual[field] == expected[field]
                for field in _OWNERSHIP_IDENTITY_FIELDS)
    )


def _open_directory_nofollow(path, *, dir_fd=None):
    """Open one real directory without following a symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow directory access is unavailable")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, dir_fd=dir_fd)


def _owned_relative(root: Path, path: Path):
    root = Path(os.path.abspath(os.fspath(root)))
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        rel = path.relative_to(root)
    except ValueError:
        return root, None
    if not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        return root, None
    return root, rel


def _bind_owned_path(
    root,
    path,
    *,
    expected_file=None,
    expected_root=None,
    created_directories=None,
):
    """Bind one proven file and the positive directory-creation evidence."""
    root, rel = _owned_relative(Path(root), Path(path))
    if rel is None:
        return None
    created_records = {}
    for record in created_directories or ():
        relative_value = (
            record.get("relative") if isinstance(record, dict) else None)
        if (
            not isinstance(record, dict)
            or not all(
                type(record.get(field)) is int
                for field in _OWNERSHIP_IDENTITY_FIELDS
            )
            or not isinstance(relative_value, str)
            or "\x00" in relative_value
            or os.path.isabs(relative_value)
            or any(
                part in ("", ".", "..")
                for part in relative_value.split(os.sep)
            )
        ):
            return None
        _, created_relative = _owned_relative(
            root, root / relative_value)
        if created_relative is None:
            return None
        key = created_relative.as_posix()
        if key in created_records:
            return None
        created_records[key] = record
    opened = []
    seen_created_records = set()
    try:
        current = _open_directory_nofollow(root)
        opened.append(current)
        current_stat = os.fstat(current)
        if expected_root is not None and not _ownership_device_inode_matches(
            current_stat, expected_root
        ):
            return None
        directories = [_file_identity(current_stat)]
        cleanup_directories = [
            _owned_directory_cleanup_entry(current_stat, created=False)
        ]
        for index, part in enumerate(rel.parts[:-1], start=1):
            current = _open_directory_nofollow(part, dir_fd=current)
            opened.append(current)
            current_stat = os.fstat(current)
            directories.append(_file_identity(current_stat))
            relative_key = Path(*rel.parts[:index]).as_posix()
            created_record = created_records.get(relative_key)
            created_identity = (
                {
                    field: created_record[field]
                    for field in _OWNERSHIP_IDENTITY_FIELDS
                }
                if created_record is not None
                else None
            )
            if (
                created_record is not None
                and not _ownership_identity_matches(
                    current_stat, created_identity)
            ):
                return None
            if created_record is not None:
                seen_created_records.add(relative_key)
            cleanup_directories.append(_owned_directory_cleanup_entry(
                current_stat,
                created=created_record is not None,
            ))
        leaf = os.stat(rel.parts[-1], dir_fd=current, follow_symlinks=False)
        if not stat.S_ISREG(leaf.st_mode):
            return None
        if expected_file is not None and not _ownership_identity_matches(
            leaf, expected_file
        ):
            return None
        if seen_created_records != set(created_records):
            return None
        return {
            "relative": rel.as_posix(),
            "directories": directories,
            "file": _owned_file_identity(leaf),
            "directory_cleanup": {
                "version": 1,
                "parent_count": 0,
                "directories": cleanup_directories,
            },
        }
    except (OSError, TypeError, ValueError):
        return None
    finally:
        for fd in reversed(opened):
            try:
                os.close(fd)
            except OSError:
                pass


def _directory_cleanup_records(owned, expected_count):
    cleanup = owned.get("directory_cleanup")
    if (not isinstance(cleanup, dict)
            or type(cleanup.get("version")) is not int
            or cleanup.get("version") != 1):
        return None
    parent_count = cleanup.get("parent_count")
    if type(parent_count) is not int or parent_count < 0:
        return None
    records = cleanup.get("directories")
    if (not isinstance(records, list)
            or len(records) != parent_count + expected_count):
        return None
    for record in records:
        if not isinstance(record, dict) or type(record.get("created")) is not bool:
            return None
        for key in ("device", "inode", "size", "modified_ns", "changed_ns"):
            if type(record.get(key)) is not int:
                return None
    return parent_count, records


def _directory_cleanup_entry_matches(st, record):
    return all(
        record[key] == value
        for key, value in _owned_file_identity(st).items()
    )


def _close_owned_unlink_plan(plan):
    if not isinstance(plan, dict):
        return
    for descriptor in reversed(plan.get("opened", ())):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _ownership_rename_noreplace(
        first_parent_fd, first, second_parent_fd, second):
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError(
            errno.ENOTSUP, "atomic no-overwrite rename is unavailable") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
            first_parent_fd,
            os.fsencode(first),
            second_parent_fd,
            os.fsencode(second),
            1,
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


_INVALID_OWNED_DELETION = object()
_OWNED_QUARANTINE_NAME = re.compile(
    r"^\.ql-undo-(?:file|dir)-[0-9a-f]{32}$")
def _owned_deletion_record(value, kind):
    deletion = value.get("deletion") if isinstance(value, dict) else None
    if deletion is None:
        # Missing paths require a durable record created before deletion.
        if isinstance(value, dict) and "leaf_removed" in value:
            return _INVALID_OWNED_DELETION
        return None
    if (
        not isinstance(deletion, dict)
        or type(deletion.get("version")) is not int
        or deletion.get("version") != 1
        or deletion.get("state") not in ("intent", "held", "removed")
        or not isinstance(deletion.get("quarantine"), str)
        or not _OWNED_QUARANTINE_NAME.fullmatch(deletion["quarantine"])
        or not deletion["quarantine"].startswith(f".ql-undo-{kind}-")
    ):
        return _INVALID_OWNED_DELETION
    return deletion


def _record_owned_progress(progress):
    if progress is None:
        return True
    try:
        return progress() is not False
    except Exception:
        _log.exception("couldn't save Undo progress")
        return False


def _begin_owned_deletion(value, kind, progress):
    deletion = _owned_deletion_record(value, kind)
    if deletion is _INVALID_OWNED_DELETION:
        return None
    if deletion is None:
        deletion = {
            "version": 1,
            "state": "intent",
            "quarantine": f".ql-undo-{kind}-{secrets.token_hex(16)}",
        }
        value["deletion"] = deletion
    # Confirm the complete current record before every retry. A failed prior
    # persist must not leave a later call free to mutate from memory alone.
    if not _record_owned_progress(progress):
        return None
    return deletion


def _ownership_stable_matches(st, expected, *, directory=False):
    wanted_mode = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        wanted_mode(st.st_mode)
        and isinstance(expected, dict)
        and all(
            type(expected.get(field)) is int
            for field in ("device", "inode", "size", "modified_ns")
        )
        and all(
            _owned_file_identity(st)[field] == expected[field]
            for field in ("device", "inode", "size", "modified_ns")
        )
    )


def _fsync_owned_directories(*descriptors):
    synced = set()
    for descriptor in descriptors:
        if descriptor is None:
            continue
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode):
            raise OSError(errno.ENOTDIR, "Undo anchor is not a directory")
        identity = (int(current.st_dev), int(current.st_ino))
        if identity in synced:
            continue
        os.fsync(descriptor)
        synced.add(identity)


def _owned_unlink_plan(root, owned):
    """Open one leaf chain and accept only a proven resumable state."""
    if not isinstance(owned, dict):
        return None
    relative = owned.get("relative")
    directories = owned.get("directories")
    file_identity = owned.get("file")
    deletion = _owned_deletion_record(owned, "file")
    if (
        not isinstance(relative, str)
        or not isinstance(directories, list)
        or not _valid_ownership_identity(file_identity)
        or deletion is _INVALID_OWNED_DELETION
        or (isinstance(deletion, dict)
            and deletion.get("state") == "removed")
    ):
        return None
    root, rel = _owned_relative(Path(root), Path(root) / relative)
    if rel is None or len(directories) != len(rel.parts):
        return None
    if not all(
        isinstance(expected, list)
        and len(expected) == 2
        and all(type(value) is int for value in expected)
        for expected in directories
    ):
        return None

    cleanup = _directory_cleanup_records(owned, len(directories))
    if cleanup is not None:
        parent_count, cleanup_records = cleanup
        try:
            cleanup_anchor = root.parents[parent_count]
        except IndexError:
            cleanup = None
        else:
            music_root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
            if parent_count > 0 and cleanup_anchor != music_root:
                cleanup = None
    if cleanup is None:
        parent_count = 0
        cleanup_records = None

    opened = []
    try:
        if root == root.parent:
            current = _open_directory_nofollow(root)
            opened.append(current)
            directory_fds = [current]
            parent_fds = [None]
            directory_names = [root.name]
            cleanup_records = None
        else:
            anchor = root.parents[parent_count]
            current = _open_directory_nofollow(anchor)
            opened.append(current)
            directory_fds = []
            parent_fds = []
            directory_names = []
            chain_names = [*root.relative_to(anchor).parts, *rel.parts[:-1]]
            for part in chain_names:
                parent = current
                current = _open_directory_nofollow(part, dir_fd=current)
                opened.append(current)
                parent_fds.append(parent)
                directory_fds.append(current)
                directory_names.append(part)

        owned_directory_fds = directory_fds[parent_count:]
        if len(owned_directory_fds) != len(directories):
            raise ValueError("owned directory chain length changed")
        for fd, expected in zip(owned_directory_fds, directories, strict=True):
            if _file_identity(os.fstat(fd)) != expected:
                raise ValueError("owned directory identity changed")
        cleanup_matches = (
            [
                _directory_cleanup_entry_matches(os.fstat(fd), record)
                for fd, record in zip(directory_fds, cleanup_records, strict=True)
            ]
            if parent_fds[0] is not None and cleanup_records is not None
            else None
        )
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file access is unavailable")
        leaf_fd = None
        try:
            leaf_fd = os.open(
                rel.parts[-1],
                os.O_RDONLY
                | nofollow
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current,
            )
        except FileNotFoundError:
            if deletion is None:
                raise ValueError("owned leaf disappeared without deletion intent")
        if leaf_fd is not None:
            opened.append(leaf_fd)
            leaf = os.fstat(leaf_fd)
            named_leaf = os.stat(
                rel.parts[-1], dir_fd=current, follow_symlinks=False)
            if _owned_file_identity(leaf) != _owned_file_identity(named_leaf):
                raise ValueError("owned leaf name changed")
            if _owned_file_identity(leaf) != file_identity:
                raise ValueError("owned leaf identity changed")
        return {
            "root": root,
            "relative": rel,
            "path": root / rel,
            "opened": opened,
            "leaf_parent": current,
            "leaf_name": rel.parts[-1],
            "leaf_fd": leaf_fd,
            "owned": owned,
            "file": file_identity,
            "deletion": deletion,
            "directory_fds": directory_fds,
            "parent_fds": parent_fds,
            "directory_names": directory_names,
            "cleanup_records": cleanup_records,
            "cleanup_matches": cleanup_matches,
        }
    except (OSError, TypeError, ValueError):
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None


def _refresh_owned_cleanup_records(plan):
    """Refresh exact directory proofs changed by this Undo operation."""
    records = plan.get("cleanup_records") if isinstance(plan, dict) else None
    matches = plan.get("cleanup_matches") if isinstance(plan, dict) else None
    if records is None or matches is None:
        return
    for directory_fd, record, matched in zip(
            plan["directory_fds"], records, matches, strict=True):
        # Never turn a directory that was already changed before this Undo
        # attempt into one of our own mutations.
        if not matched:
            continue
        try:
            current = os.fstat(directory_fd)
        except OSError:
            continue
        if _file_identity(current) != [record["device"], record["inode"]]:
            continue
        record.update(_owned_file_identity(current))


def _open_owned_leaf_quarantine(plan):
    deletion = plan.get("deletion")
    if not isinstance(deletion, dict):
        return {
            "missing": True,
            "fd": None,
            "held_fd": None,
            "held_full_match": False,
            "held_stable_match": False,
        }
    name = deletion["quarantine"]
    try:
        quarantine_fd = _open_directory_nofollow(
            name, dir_fd=plan["leaf_parent"])
    except FileNotFoundError:
        return {
            "missing": True,
            "fd": None,
            "held_fd": None,
            "held_full_match": False,
            "held_stable_match": False,
        }
    except OSError:
        return None
    held_fd = None
    try:
        current = os.fstat(quarantine_fd)
        named = os.stat(
            name, dir_fd=plan["leaf_parent"], follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or _owned_file_identity(current) != _owned_file_identity(named)
        ):
            raise ValueError("quarantine directory changed")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file access is unavailable")
        try:
            held_fd = os.open(
                "held",
                os.O_RDONLY
                | nofollow
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=quarantine_fd,
            )
        except FileNotFoundError:
            held_fd = None
        held_identity = None
        held_full_match = False
        held_stable_match = False
        if held_fd is not None:
            held = os.fstat(held_fd)
            named_held = os.stat(
                "held", dir_fd=quarantine_fd, follow_symlinks=False)
            if _owned_file_identity(held) != _owned_file_identity(named_held):
                raise ValueError("quarantined leaf changed")
            held_identity = _owned_file_identity(held)
            held_full_match = _ownership_identity_matches(
                held, plan["file"])
            held_stable_match = _ownership_stable_matches(
                held, plan["file"])
        return {
            "missing": False,
            "fd": quarantine_fd,
            "held_fd": held_fd,
            "held_identity": held_identity,
            "held_full_match": held_full_match,
            "held_stable_match": held_stable_match,
        }
    except (OSError, TypeError, ValueError):
        if held_fd is not None:
            try:
                os.close(held_fd)
            except OSError:
                pass
        try:
            os.close(quarantine_fd)
        except OSError:
            pass
        return None
    except BaseException:
        if held_fd is not None:
            try:
                os.close(held_fd)
            except OSError:
                pass
        try:
            os.close(quarantine_fd)
        except OSError:
            pass
        raise


def _close_owned_quarantine(snapshot):
    if not isinstance(snapshot, dict):
        return
    for key in ("held_fd", "fd"):
        descriptor = snapshot.get(key)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _owned_unlink_leaf_matches(plan):
    deletion = plan.get("deletion")
    snapshot = _open_owned_leaf_quarantine(plan)
    if snapshot is None:
        return False
    try:
        public_exists = plan.get("leaf_fd") is not None
        held_exists = snapshot.get("held_fd") is not None
        if public_exists and held_exists:
            return False
        if deletion is None:
            return public_exists and not held_exists
        if held_exists:
            if public_exists:
                return False
            if deletion.get("state") == "intent":
                return snapshot.get("held_stable_match") is True
            if deletion.get("state") == "held":
                return snapshot.get("held_full_match") is True
            return False
        return True
    finally:
        _close_owned_quarantine(snapshot)


def _remove_owned_leaf_quarantine(plan, snapshot):
    if snapshot.get("missing"):
        return True
    if snapshot.get("held_fd") is not None:
        return False
    try:
        held = os.fstat(snapshot["fd"])
        named = os.stat(
            plan["deletion"]["quarantine"],
            dir_fd=plan["leaf_parent"],
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(named.st_mode)
            or _owned_file_identity(held) != _owned_file_identity(named)
        ):
            return False
        os.rmdir(
            plan["deletion"]["quarantine"],
            dir_fd=plan["leaf_parent"],
        )
        _fsync_owned_directories(plan["leaf_parent"])
        return True
    except (OSError, TypeError, ValueError):
        return False


def _restore_owned_leaf_from_intent(
        plan, snapshot, progress, cleanup_plan):
    """Restore a rename-interrupted leaf without authorising its deletion."""
    if (
        snapshot.get("held_fd") is None
        or snapshot.get("held_stable_match") is not True
        or plan["deletion"].get("state") != "intent"
    ):
        return {"status": "held", "mutated": False}
    previous_file = dict(plan["file"])
    moved = False
    try:
        _ownership_rename_noreplace(
            snapshot["fd"],
            "held",
            plan["leaf_parent"],
            plan["leaf_name"],
        )
        moved = True
        restored = os.fstat(snapshot["held_fd"])
        named = os.stat(
            plan["leaf_name"],
            dir_fd=plan["leaf_parent"],
            follow_symlinks=False,
        )
        if (
            _owned_file_identity(restored) != _owned_file_identity(named)
            or not _ownership_stable_matches(restored, plan["file"])
        ):
            raise OSError("restored Undo leaf changed")
        _fsync_owned_directories(snapshot["fd"], plan["leaf_parent"])
        plan["owned"]["file"] = _owned_file_identity(restored)
        plan["file"] = plan["owned"]["file"]
        plan["deletion"]["state"] = "intent"
        _refresh_owned_cleanup_records(cleanup_plan)
        if _record_owned_progress(progress):
            return {"status": "restored", "mutated": True}
        try:
            _ownership_rename_noreplace(
                plan["leaf_parent"],
                plan["leaf_name"],
                snapshot["fd"],
                "held",
            )
            held_again = os.fstat(snapshot["held_fd"])
            named_again = os.stat(
                "held", dir_fd=snapshot["fd"], follow_symlinks=False)
            if (
                _owned_file_identity(held_again)
                != _owned_file_identity(named_again)
                or not _ownership_stable_matches(
                    held_again, previous_file)
            ):
                raise OSError("rolled-back Undo leaf changed")
            _fsync_owned_directories(
                snapshot["fd"], plan["leaf_parent"])
            plan["owned"]["file"] = previous_file
            plan["file"] = previous_file
            plan["deletion"]["state"] = "intent"
            _refresh_owned_cleanup_records(cleanup_plan)
            return {"status": "held", "mutated": True}
        except (OSError, TypeError, ValueError):
            return {"status": "restored", "mutated": True}
    except (OSError, TypeError, ValueError):
        if moved:
            try:
                _ownership_rename_noreplace(
                    plan["leaf_parent"],
                    plan["leaf_name"],
                    snapshot["fd"],
                    "held",
                )
                _fsync_owned_directories(
                    snapshot["fd"], plan["leaf_parent"])
            except OSError:
                pass
        _refresh_owned_cleanup_records(cleanup_plan)
        _record_owned_progress(progress)
        return {"status": "held", "mutated": moved}


def _quarantine_owned_leaf(plan, *, progress=None, cleanup_plan=None):
    """Durably remove one proved leaf through its persisted private name."""
    cleanup_plan = cleanup_plan or plan
    snapshot = None
    lease_fd = plan.get("leaf_fd")
    if lease_fd is None and isinstance(plan.get("deletion"), dict):
        snapshot = _open_owned_leaf_quarantine(plan)
        if snapshot is None:
            return {"status": "refused", "mutated": False}
        lease_fd = snapshot.get("held_fd")
    exclusion = (
        acquire_inode_write_exclusion(lease_fd)
        if lease_fd is not None
        else None
    )
    if lease_fd is not None and exclusion is None:
        _close_owned_quarantine(snapshot)
        return {"status": "refused", "mutated": False}

    try:
        deletion = _begin_owned_deletion(plan["owned"], "file", progress)
    except BaseException:
        if exclusion is not None:
            exclusion.close()
        _close_owned_quarantine(snapshot)
        raise
    if deletion is None:
        if exclusion is not None:
            exclusion.close()
        _close_owned_quarantine(snapshot)
        return {"status": "refused", "mutated": False}
    plan["deletion"] = deletion

    if snapshot is None:
        try:
            snapshot = _open_owned_leaf_quarantine(plan)
        except BaseException:
            if exclusion is not None:
                exclusion.close()
            raise
    if snapshot is None:
        if exclusion is not None:
            exclusion.close()
        return {"status": "refused", "mutated": False}
    try:
        if exclusion is not None and not exclusion.intact():
            return {"status": "refused", "mutated": False}
        public_exists = plan.get("leaf_fd") is not None
        held_exists = snapshot.get("held_fd") is not None
        if public_exists and held_exists:
            return {"status": "refused", "mutated": False}

        if not public_exists and held_exists:
            if (
                deletion.get("state") == "intent"
                and snapshot.get("held_stable_match") is True
            ):
                return _restore_owned_leaf_from_intent(
                    plan, snapshot, progress, cleanup_plan)
            if (
                deletion.get("state") != "held"
                or snapshot.get("held_full_match") is not True
            ):
                return {"status": "held", "mutated": False}

        if not public_exists and not held_exists:
            if not snapshot.get("missing"):
                if not _remove_owned_leaf_quarantine(plan, snapshot):
                    return {"status": "held", "mutated": False}
                _refresh_owned_cleanup_records(cleanup_plan)
            else:
                try:
                    _fsync_owned_directories(plan["leaf_parent"])
                except OSError:
                    return {"status": "held", "mutated": False}
            deletion["state"] = "removed"
            _refresh_owned_cleanup_records(cleanup_plan)
            if not _record_owned_progress(progress):
                return {"status": "held", "mutated": True}
            return {"status": "removed", "mutated": True}

        if public_exists and snapshot.get("missing"):
            try:
                os.mkdir(
                    deletion["quarantine"],
                    0o700,
                    dir_fd=plan["leaf_parent"],
                )
                _fsync_owned_directories(plan["leaf_parent"])
            except OSError:
                return {"status": "refused", "mutated": False}
            _refresh_owned_cleanup_records(cleanup_plan)
            if not _record_owned_progress(progress):
                return {"status": "restored", "mutated": True}
            _close_owned_quarantine(snapshot)
            snapshot = _open_owned_leaf_quarantine(plan)
            if snapshot is None or snapshot.get("missing"):
                return {"status": "refused", "mutated": True}

        if public_exists:
            previous_file = dict(plan["file"])
            moved_public = False
            validated_moved = False
            rolled_back = False
            try:
                current = os.fstat(plan["leaf_fd"])
                named_current = os.stat(
                    plan["leaf_name"],
                    dir_fd=plan["leaf_parent"],
                    follow_symlinks=False,
                )
                if (
                    exclusion is None
                    or not exclusion.intact()
                    or _owned_file_identity(current)
                    != _owned_file_identity(named_current)
                    or not _ownership_identity_matches(
                        current, plan["file"])
                ):
                    raise ValueError(
                        "public leaf changed after deletion intent")
                _ownership_rename_noreplace(
                    plan["leaf_parent"],
                    plan["leaf_name"],
                    snapshot["fd"],
                    "held",
                )
                moved_public = True
                moved = os.fstat(plan["leaf_fd"])
                named = os.stat(
                    "held", dir_fd=snapshot["fd"], follow_symlinks=False)
                if (
                    _owned_file_identity(moved) != _owned_file_identity(named)
                    or not _ownership_stable_matches(moved, plan["file"])
                ):
                    raise ValueError("public leaf changed at quarantine")
                validated_moved = True
                _fsync_owned_directories(
                    snapshot["fd"], plan["leaf_parent"])
            except (OSError, TypeError, ValueError):
                if moved_public:
                    try:
                        _ownership_rename_noreplace(
                            snapshot["fd"],
                            "held",
                            plan["leaf_parent"],
                            plan["leaf_name"],
                        )
                        restored = os.fstat(plan["leaf_fd"])
                        named_restored = os.stat(
                            plan["leaf_name"],
                            dir_fd=plan["leaf_parent"],
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(restored)
                            != _owned_file_identity(named_restored)
                            or not _ownership_stable_matches(
                                restored, plan["file"])
                        ):
                            raise OSError("restored Undo leaf changed")
                        _fsync_owned_directories(
                            snapshot["fd"], plan["leaf_parent"])
                        rolled_back = True
                    except (OSError, TypeError, ValueError):
                        pass
                if validated_moved and rolled_back:
                    plan["owned"]["file"] = _owned_file_identity(restored)
                    plan["file"] = plan["owned"]["file"]
                    deletion["state"] = "intent"
                _refresh_owned_cleanup_records(cleanup_plan)
                persisted = _record_owned_progress(progress)
                if validated_moved and rolled_back and not persisted:
                    refreshed_file = dict(plan["file"])
                    try:
                        current = os.fstat(plan["leaf_fd"])
                        named_current = os.stat(
                            plan["leaf_name"],
                            dir_fd=plan["leaf_parent"],
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(current)
                            != _owned_file_identity(named_current)
                            or not _ownership_identity_matches(
                                current, refreshed_file)
                        ):
                            raise OSError("restored Undo leaf changed")
                        _ownership_rename_noreplace(
                            plan["leaf_parent"],
                            plan["leaf_name"],
                            snapshot["fd"],
                            "held",
                        )
                        held_again = os.fstat(plan["leaf_fd"])
                        named_again = os.stat(
                            "held",
                            dir_fd=snapshot["fd"],
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(held_again)
                            != _owned_file_identity(named_again)
                            or not _ownership_stable_matches(
                                held_again, refreshed_file)
                        ):
                            raise OSError("re-quarantined Undo leaf changed")
                        _fsync_owned_directories(
                            snapshot["fd"], plan["leaf_parent"])
                        plan["owned"]["file"] = previous_file
                        plan["file"] = previous_file
                        deletion["state"] = "intent"
                        _refresh_owned_cleanup_records(cleanup_plan)
                    except (OSError, TypeError, ValueError):
                        pass
                return {
                    "status": "refused",
                    "mutated": moved_public,
                }
            plan["owned"]["file"] = _owned_file_identity(moved)
            plan["file"] = plan["owned"]["file"]
            deletion["state"] = "held"
            _refresh_owned_cleanup_records(cleanup_plan)
            if not _record_owned_progress(progress):
                return {"status": "held", "mutated": True}
            _close_owned_quarantine(snapshot)
            snapshot = _open_owned_leaf_quarantine(plan)
            if snapshot is None or snapshot.get("held_fd") is None:
                return {"status": "refused", "mutated": True}
        elif deletion.get("state") != "held":
            deletion["state"] = "held"
            plan["owned"]["file"] = snapshot["held_identity"]
            plan["file"] = plan["owned"]["file"]
            _refresh_owned_cleanup_records(cleanup_plan)
            if not _record_owned_progress(progress):
                return {"status": "held", "mutated": False}

        try:
            held_now = os.fstat(snapshot["held_fd"])
            named_held_now = os.stat(
                "held", dir_fd=snapshot["fd"], follow_symlinks=False)
            if (
                exclusion is None
                or not exclusion.intact()
                or _owned_file_identity(held_now)
                != _owned_file_identity(named_held_now)
                or not _ownership_identity_matches(
                    held_now, plan["file"])
            ):
                return {"status": "held", "mutated": False}
            if exclusion is None or not exclusion.intact():
                return {"status": "held", "mutated": False}
            os.unlink("held", dir_fd=snapshot["fd"])
            _fsync_owned_directories(snapshot["fd"])
        except (OSError, TypeError, ValueError):
            # Keep the exact leaf under its persisted private name.
            return {"status": "held", "mutated": False}

        _refresh_owned_cleanup_records(cleanup_plan)
        if not _record_owned_progress(progress):
            return {"status": "held", "mutated": True}
        try:
            os.close(snapshot["held_fd"])
        except OSError:
            pass
        snapshot["held_fd"] = None
        if not _remove_owned_leaf_quarantine(plan, snapshot):
            return {"status": "held", "mutated": True}
        deletion["state"] = "removed"
        _refresh_owned_cleanup_records(cleanup_plan)
        if not _record_owned_progress(progress):
            return {"status": "held", "mutated": True}
        return {"status": "removed", "mutated": True}
    finally:
        if exclusion is not None:
            exclusion.close()
        _close_owned_quarantine(snapshot)


def _owned_directory_chain(root, owned):
    relative = owned.get("relative") if isinstance(owned, dict) else None
    directories = owned.get("directories") if isinstance(owned, dict) else None
    if not isinstance(relative, str) or not isinstance(directories, list):
        return None
    root, rel = _owned_relative(Path(root), Path(root) / relative)
    if rel is None or len(directories) != len(rel.parts) or root == root.parent:
        return None
    cleanup = _directory_cleanup_records(owned, len(directories))
    if cleanup is None:
        return None
    parent_count, records = cleanup
    try:
        anchor = root.parents[parent_count]
    except IndexError:
        return None
    music_root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    if parent_count > 0 and anchor != music_root:
        return None
    names = [*root.relative_to(anchor).parts, *rel.parts[:-1]]
    if len(names) != len(records):
        return None
    return {
        "root": root,
        "relative": rel,
        "anchor": anchor,
        "names": names,
        "records": records,
        "cleanup": owned["directory_cleanup"],
    }


def _refresh_open_directory_records(opened, *, include_last=True):
    entries = opened if include_last else opened[:-1]
    for descriptor, record, matched in entries:
        if not matched:
            continue
        try:
            current = os.fstat(descriptor)
        except OSError:
            continue
        if _file_identity(current) == [record["device"], record["inode"]]:
            record.update(_owned_file_identity(current))


def _restore_owned_directory(
        parent_fd, name, held_fd, record, deletion, opened, progress,
        *, terminal_skip=False):
    previous_identity = {
        field: record[field] for field in _OWNERSHIP_IDENTITY_FIELDS
    }
    previous_state = deletion["state"]
    try:
        _ownership_rename_noreplace(
            parent_fd, deletion["quarantine"], parent_fd, name)
    except OSError:
        record.update(_owned_file_identity(os.fstat(held_fd)))
        deletion["state"] = "held"
        _refresh_open_directory_records(opened)
        _record_owned_progress(progress)
        return "retry"
    try:
        _fsync_owned_directories(parent_fd)
    except OSError:
        state = "intent"
        try:
            _ownership_rename_noreplace(
                parent_fd, name, parent_fd, deletion["quarantine"])
            _fsync_owned_directories(parent_fd)
            state = "held"
        except OSError:
            pass
        record.update(_owned_file_identity(os.fstat(held_fd)))
        deletion["state"] = state
        _refresh_open_directory_records(opened)
        _record_owned_progress(progress)
        return "retry"
    try:
        restored = os.fstat(held_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _owned_file_identity(restored) != _owned_file_identity(named)
            or not stat.S_ISDIR(restored.st_mode)
            or (
                not terminal_skip
                and not _ownership_stable_matches(
                    restored, record, directory=True)
            )
        ):
            _refresh_open_directory_records(opened, include_last=False)
            _record_owned_progress(progress)
            return "retry"
        record.update(_owned_file_identity(restored))
        deletion["state"] = "intent"
        _refresh_open_directory_records(opened)
        persisted = _record_owned_progress(progress)
        if persisted:
            return "skipped" if terminal_skip else "retry"
        try:
            _ownership_rename_noreplace(
                parent_fd, name, parent_fd, deletion["quarantine"])
            held_again = os.fstat(held_fd)
            named_again = os.stat(
                deletion["quarantine"],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                _owned_file_identity(held_again)
                != _owned_file_identity(named_again)
                or not _ownership_stable_matches(
                    held_again, previous_identity, directory=True)
            ):
                raise OSError("rolled-back Undo directory changed")
            _fsync_owned_directories(parent_fd)
            record.update(previous_identity)
            deletion["state"] = previous_state
            _refresh_open_directory_records(opened, include_last=False)
        except (OSError, TypeError, ValueError):
            pass
        return "retry"
    except (OSError, TypeError, ValueError):
        return "retry"


def _cleanup_one_owned_directory(chain, index, progress):
    records = chain["records"]
    record = records[index]
    deletion = _owned_deletion_record(record, "dir")
    if deletion is _INVALID_OWNED_DELETION:
        return "refused"
    opened_fds = []
    opened_records = []
    restored_public_skip = False
    try:
        current = _open_directory_nofollow(chain["anchor"])
        opened_fds.append(current)
        parent_fd = current
        held_fd = None
        public_exists = False
        for current_index, (name, current_record) in enumerate(zip(
                chain["names"][:index + 1], records[:index + 1], strict=True)):
            parent_fd = current
            try:
                current = _open_directory_nofollow(name, dir_fd=parent_fd)
            except FileNotFoundError:
                if current_index != index:
                    return "refused"
                public_exists = False
                held_fd = None
                break
            opened_fds.append(current)
            current_stat = os.fstat(current)
            full_match = _directory_cleanup_entry_matches(
                current_stat, current_record)
            opened_records.append((current, current_record, full_match))
            if current_index < index:
                # Ancestors can legitimately change when a sibling album or
                # disc is added.
                if _file_identity(current_stat) != [
                    current_record["device"], current_record["inode"]
                ]:
                    return "skipped"
                continue
            if not full_match:
                # A public directory whose durable proof no longer matches is
                # ambiguous after a rollback or restart.
                if (
                    isinstance(deletion, dict)
                    and deletion.get("state") == "held"
                    and _ownership_stable_matches(
                        current_stat, current_record, directory=True)
                ):
                    # A crash after a no-overwrite private→public restore can
                    # leave only ctime changed.
                    restored_public_skip = True
                else:
                    return "refused" if deletion is not None else "skipped"
            public_exists = True
            held_fd = current

        if deletion is None and not public_exists:
            return "refused"
        deletion = _begin_owned_deletion(record, "dir", progress)
        if deletion is None:
            return "retry"

        quarantine_fd = None
        try:
            quarantine_fd = _open_directory_nofollow(
                deletion["quarantine"], dir_fd=parent_fd)
        except FileNotFoundError:
            quarantine_fd = None
        except OSError:
            return "refused"
        if quarantine_fd is not None:
            opened_fds.append(quarantine_fd)
            quarantined = os.fstat(quarantine_fd)
            named_quarantine = os.stat(
                deletion["quarantine"],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                _owned_file_identity(quarantined)
                != _owned_file_identity(named_quarantine)
            ):
                return "refused"
            if public_exists:
                return "refused"
            if not _ownership_identity_matches(quarantined, record):
                if (
                    deletion.get("state") == "intent"
                    and _ownership_stable_matches(
                        quarantined, record, directory=True)
                ):
                    return _restore_owned_directory(
                        parent_fd,
                        chain["names"][index],
                        quarantine_fd,
                        record,
                        deletion,
                        opened_records,
                        progress,
                    )
                return "refused"
            held_fd = quarantine_fd

        if restored_public_skip:
            return "skipped" if quarantine_fd is None else "refused"

        if not public_exists and quarantine_fd is None:
            try:
                _fsync_owned_directories(parent_fd)
            except OSError:
                return "retry"
            deletion["state"] = "removed"
            _refresh_open_directory_records(opened_records)
            return "removed" if _record_owned_progress(progress) else "retry"

        if public_exists and quarantine_fd is None:
            previous_identity = {
                field: record[field]
                for field in _OWNERSHIP_IDENTITY_FIELDS
            }
            previous_state = deletion["state"]
            moved_public = False
            validated_moved = False
            rolled_back = False
            try:
                current = os.fstat(held_fd)
                named_current = os.stat(
                    chain["names"][index],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    _owned_file_identity(current)
                    != _owned_file_identity(named_current)
                    or not _directory_cleanup_entry_matches(
                        current, record)
                ):
                    raise ValueError(
                        "public directory changed after deletion intent")
                _ownership_rename_noreplace(
                    parent_fd,
                    chain["names"][index],
                    parent_fd,
                    deletion["quarantine"],
                )
                moved_public = True
                moved = os.fstat(held_fd)
                named = os.stat(
                    deletion["quarantine"],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    _owned_file_identity(moved) != _owned_file_identity(named)
                    or not _ownership_stable_matches(
                        moved, record, directory=True)
                ):
                    raise ValueError("public directory changed at quarantine")
                validated_moved = True
                _fsync_owned_directories(parent_fd)
            except (OSError, TypeError, ValueError):
                if moved_public:
                    try:
                        _ownership_rename_noreplace(
                            parent_fd,
                            deletion["quarantine"],
                            parent_fd,
                            chain["names"][index],
                        )
                        restored = os.fstat(held_fd)
                        named_restored = os.stat(
                            chain["names"][index],
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(restored)
                            != _owned_file_identity(named_restored)
                            or not _ownership_stable_matches(
                                restored, record, directory=True)
                        ):
                            raise OSError("restored Undo directory changed")
                        _fsync_owned_directories(parent_fd)
                        rolled_back = True
                    except (OSError, TypeError, ValueError):
                        pass
                if validated_moved and rolled_back:
                    record.update(_owned_file_identity(restored))
                    deletion["state"] = "intent"
                    _refresh_open_directory_records(opened_records)
                else:
                    _refresh_open_directory_records(
                        opened_records, include_last=False)
                persisted = _record_owned_progress(progress)
                if validated_moved and rolled_back and not persisted:
                    refreshed_identity = {
                        field: record[field]
                        for field in _OWNERSHIP_IDENTITY_FIELDS
                    }
                    try:
                        current = os.fstat(held_fd)
                        named_current = os.stat(
                            chain["names"][index],
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(current)
                            != _owned_file_identity(named_current)
                            or not _ownership_identity_matches(
                                current, refreshed_identity)
                        ):
                            raise OSError("restored Undo directory changed")
                        _ownership_rename_noreplace(
                            parent_fd,
                            chain["names"][index],
                            parent_fd,
                            deletion["quarantine"],
                        )
                        held_again = os.fstat(held_fd)
                        named_again = os.stat(
                            deletion["quarantine"],
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(held_again)
                            != _owned_file_identity(named_again)
                            or not _ownership_stable_matches(
                                held_again,
                                refreshed_identity,
                                directory=True,
                            )
                        ):
                            raise OSError(
                                "re-quarantined Undo directory changed")
                        _fsync_owned_directories(parent_fd)
                        record.update(previous_identity)
                        deletion["state"] = previous_state
                        _refresh_open_directory_records(
                            opened_records, include_last=False)
                    except (OSError, TypeError, ValueError):
                        pass
                return "retry"
            record.update(_owned_file_identity(moved))
            deletion["state"] = "held"
            _refresh_open_directory_records(opened_records)
            if not _record_owned_progress(progress):
                return "retry"
        elif deletion["state"] != "held":
            record.update(_owned_file_identity(os.fstat(held_fd)))
            deletion["state"] = "held"
            _refresh_open_directory_records(opened_records)
            if not _record_owned_progress(progress):
                return "retry"

        try:
            held_now = os.fstat(held_fd)
            named_now = os.stat(
                deletion["quarantine"],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                _owned_file_identity(held_now)
                != _owned_file_identity(named_now)
                or not _ownership_identity_matches(held_now, record)
            ):
                return "refused"
            os.rmdir(deletion["quarantine"], dir_fd=parent_fd)
            _fsync_owned_directories(parent_fd)
        except OSError as exc:
            return _restore_owned_directory(
                parent_fd,
                chain["names"][index],
                held_fd,
                record,
                deletion,
                opened_records,
                progress,
                terminal_skip=exc.errno in (errno.ENOTEMPTY, errno.EEXIST),
            )
        deletion["state"] = "removed"
        _refresh_open_directory_records(
            opened_records, include_last=not public_exists)
        return "removed" if _record_owned_progress(progress) else "retry"
    except (OSError, TypeError, ValueError):
        return "refused"
    finally:
        for descriptor in reversed(opened_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _cleanup_owned_directories(root, owned, progress):
    chain = _owned_directory_chain(root, owned)
    if chain is None:
        return True
    complete = chain["cleanup"].get("complete", False)
    if type(complete) is not bool:
        return False
    if complete:
        return True
    created_tail = []
    for index in reversed(range(len(chain["records"]))):
        if chain["records"][index]["created"] is not True:
            break
        created_tail.append(index)
    if not created_tail:
        chain["cleanup"]["complete"] = True
        return _record_owned_progress(progress)

    while True:
        candidate = None
        for index in created_tail:
            deletion = _owned_deletion_record(chain["records"][index], "dir")
            if deletion is _INVALID_OWNED_DELETION:
                return False
            if deletion is None or deletion["state"] != "removed":
                candidate = index
                break
        if candidate is None:
            chain["cleanup"]["complete"] = True
            return _record_owned_progress(progress)
        outcome = _cleanup_one_owned_directory(chain, candidate, progress)
        if outcome == "removed":
            continue
        if outcome == "skipped":
            chain["cleanup"]["complete"] = True
            return _record_owned_progress(progress)
        return False


def _unlink_owned_path(root, owned, *, progress=None, outcome_out=None):
    """Remove exact owned leaves, then retry exact created-folder cleanup."""
    if not isinstance(owned, dict):
        return None
    companions = owned.get("companions", [])
    if not isinstance(companions, list) or len(companions) > 2:
        return None
    root, main_relative = _owned_relative(
        Path(root), Path(root) / str(owned.get("relative", "")))
    if main_relative is None:
        return None
    expected_companion = main_relative.with_suffix(".lrc").as_posix()
    seen_kinds = set()
    seen_relatives = {main_relative.as_posix()}
    for companion in companions:
        kind = companion.get("kind") if isinstance(companion, dict) else None
        relative = companion.get("relative") if isinstance(companion, dict) else None
        companion_relative = (
            _strict_ownership_relative(root, relative)
            if isinstance(relative, str) else None
        )
        if (
            not isinstance(companion, dict)
            or companion.get("companions")
            or kind not in ("lyrics", "artwork")
            or kind in seen_kinds
            or companion_relative is None
            or companion_relative.as_posix() in seen_relatives
        ):
            return None
        if kind == "lyrics" and relative != expected_companion:
            return None
        if (
            kind == "artwork"
            and (
                len(companion_relative.parent.parts)
                >= len(main_relative.parts)
                or main_relative.parts[:len(companion_relative.parent.parts)]
                != companion_relative.parent.parts
            )
        ):
            return None
        seen_kinds.add(kind)
        seen_relatives.add(companion_relative.as_posix())

    entries = [owned, *companions]
    deletion_records = [
        _owned_deletion_record(entry, "file") for entry in entries]
    if any(record is _INVALID_OWNED_DELETION for record in deletion_records):
        return None

    def _update_outcome(**extra):
        current = [
            _owned_deletion_record(entry, "file") for entry in entries]
        removed_count = sum(
            isinstance(record, dict) and record["state"] == "removed"
            for record in current
        )
        held_count = sum(
            isinstance(record, dict) and record["state"] == "held"
            for record in current
        )
        if isinstance(outcome_out, dict):
            outcome_out.update({
                "files_complete": removed_count == len(entries),
                "removed_files": removed_count,
                "held_files": held_count,
                "undo_started": any(
                    isinstance(record, dict) for record in current),
                **extra,
            })

    # A retry may begin after an earlier attempt durably removed a companion.
    _update_outcome()
    if (
        deletion_records[0] is not None
        and deletion_records[0]["state"] == "removed"
        and any(record is None or record["state"] != "removed"
                for record in deletion_records[1:])
    ):
        return None

    plans = []
    plan_by_entry = {}
    try:
        for entry, deletion in zip(entries, deletion_records, strict=True):
            if deletion is not None and deletion["state"] == "removed":
                continue
            plan = _owned_unlink_plan(root, entry)
            if plan is None:
                return None
            plans.append(plan)
            plan_by_entry[id(entry)] = plan
        if not all(_owned_unlink_leaf_matches(plan) for plan in plans):
            return None
        main_plan = plan_by_entry.get(id(owned))
        for entry in [*companions, owned]:
            deletion = _owned_deletion_record(entry, "file")
            if isinstance(deletion, dict) and deletion["state"] == "removed":
                continue
            plan = plan_by_entry[id(entry)]
            result = _quarantine_owned_leaf(
                plan,
                progress=progress,
                cleanup_plan=main_plan or plan,
            )
            if result["status"] != "removed":
                _update_outcome()
                return None
    except (OSError, TypeError, ValueError):
        return None
    finally:
        for plan in reversed(plans):
            _close_owned_unlink_plan(plan)

    files_complete = all(
        isinstance(_owned_deletion_record(entry, "file"), dict)
        and _owned_deletion_record(entry, "file")["state"] == "removed"
        for entry in entries
    )
    cleanup_complete = (
        files_complete and _cleanup_owned_directories(root, owned, progress))
    _update_outcome(
        cleanup_pending=files_complete and not cleanup_complete)
    if not files_complete or not cleanup_complete:
        return None
    return root / main_relative


def _valid_ownership_identity(value):
    return (
        isinstance(value, dict)
        and all(type(value.get(field)) is int
                for field in _OWNERSHIP_IDENTITY_FIELDS)
    )


def _strict_ownership_relative(root, value):
    if (
        not isinstance(value, str)
        or "\x00" in value
        or os.path.isabs(value)
    ):
        return None
    parts = value.split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        return None
    _, relative = _owned_relative(root, root / value)
    return relative
