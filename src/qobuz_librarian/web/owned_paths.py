"""Ownership records for downloaded files and their safe removal."""
import errno
import logging
import os
import re
import secrets
import stat
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.safefile import (
    AT_FDCWD,
    delete_verified,
    fsync_dir,
    rename_noreplace,
    stat_identity,
)
from qobuz_librarian.safefile import identity_record as _owned_file_identity

_log = logging.getLogger("qobuz_librarian")


def _file_identity(st) -> list[int]:
    return [int(st.st_dev), int(st.st_ino)]


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
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
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


def _ownership_stable_matches(st, expected):
    return (
        stat.S_ISREG(st.st_mode)
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


def _lstat_or_none(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _owned_chain(root, owned):
    """Where one entry sits: the folder its chain hangs from, the folders
    below that down to the entry's own, the leaf, how many of the folders
    are above the root, and the cleanup records when they are valid."""
    relative = owned.get("relative")
    directories = owned.get("directories")
    if not isinstance(relative, str) or not isinstance(directories, list):
        return None
    root, rel = _owned_relative(Path(root), Path(root) / relative)
    if rel is None or len(directories) != len(rel.parts):
        return None
    cleanup = _directory_cleanup_records(owned, len(directories))
    if cleanup is not None and cleanup[0] > 0:
        music_root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        if (cleanup[0] >= len(root.parents)
                or root.parents[cleanup[0]] != music_root):
            cleanup = None
    parent_count, records = cleanup or (0, None)
    try:
        anchor = root.parents[parent_count]
    except IndexError:
        return None
    folders = []
    for part in (*root.relative_to(anchor).parts, *rel.parts[:-1]):
        folders.append((folders[-1] if folders else anchor) / part)
    return anchor, folders, parent_count, records, folders[-1] / rel.parts[-1]


def _owned_leaf(root, entry):
    """Find what is left of one entry's file, checked against its record.

    Every folder above it must still be a real folder, and from the root
    down the one recorded. Returns the path to delete, under the file's own
    name or the hidden name a 1.3 Undo moved it to, its stat, and the
    entry's hidden folder. Path and stat are None when the file is already
    gone after an Undo began. None when anything disagrees with the
    record."""
    chain = _owned_chain(root, entry)
    deletion = _owned_deletion_record(entry, "file")
    if (
        chain is None
        or deletion is _INVALID_OWNED_DELETION
        or not _valid_ownership_identity(entry.get("file"))
    ):
        return None
    anchor, folders, parent_count, _records, leaf = chain
    quarantine = (
        leaf.parent / deletion["quarantine"] if deletion is not None else None)
    try:
        for index, path in enumerate([anchor, *folders]):
            current = os.lstat(path)
            if not stat.S_ISDIR(current.st_mode) or (
                index > parent_count
                and _file_identity(current)
                != entry["directories"][index - parent_count - 1]
            ):
                return None
        public = _lstat_or_none(leaf)
        held = _lstat_or_none(quarantine / "held") if quarantine else None
    except OSError:
        return None
    if public is not None and held is not None:
        return None
    if public is not None:
        if not _ownership_identity_matches(public, entry["file"]):
            return None
        return leaf, public, quarantine
    if held is not None:
        # A 1.3 Undo stopped before it recorded the move keeps the ctime
        # the move changed, so that file is matched without it.
        if not (
            deletion["state"] == "held"
            and _ownership_identity_matches(held, entry["file"])
            or deletion["state"] == "intent"
            and _ownership_stable_matches(held, entry["file"])
        ):
            return None
        return quarantine / "held", held, quarantine
    if deletion is None:
        return None
    return None, None, quarantine


def _remove_owned_leaf(root, entry, progress):
    """Delete one entry's file and record it removed, saving the intent to
    delete it first."""
    deletion = _begin_owned_deletion(entry, "file", progress)
    if deletion is None:
        return False
    found = _owned_leaf(root, entry)
    if found is None:
        return False
    path, current, quarantine = found
    if path is not None and not delete_verified(path, stat_identity(current)):
        return False
    try:
        os.rmdir(quarantine)
        fsync_dir(quarantine.parent)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    deletion["state"] = "removed"
    return _record_owned_progress(progress)


def _remove_created_folder(anchor, folders, records):
    """Remove the last of `folders` while it is empty and still the folder
    the grab created. True once it is gone, False when the cleanup stops
    there for good, None when it should be tried again."""
    *ancestors, folder = folders
    record = records[-1]
    deletion = _owned_deletion_record(record, "dir")
    if deletion is _INVALID_OWNED_DELETION:
        return None
    try:
        for path, expected in zip(
                [anchor, *ancestors], [None, *records[:-1]], strict=True):
            current = os.lstat(path)
            if not stat.S_ISDIR(current.st_mode) or (
                expected is not None
                and not _ownership_device_inode_matches(current, expected)
            ):
                return False
        public = _lstat_or_none(folder)
        if deletion is not None:
            # A 1.3 Undo renamed the folder to a hidden name before removing
            # it. Anything put in it since goes back under the folder's name.
            quarantine = folder.parent / deletion["quarantine"]
            try:
                os.rmdir(quarantine)
                fsync_dir(folder.parent)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if (
                    exc.errno not in (errno.ENOTEMPTY, errno.EEXIST)
                    or public is not None
                    or not _ownership_device_inode_matches(
                        os.lstat(quarantine), record)
                ):
                    return None
                try:
                    rename_noreplace(AT_FDCWD, quarantine, AT_FDCWD, folder)
                except OSError:
                    return None
                fsync_dir(folder.parent)
                return False
        if public is None:
            return True
        if (
            not stat.S_ISDIR(public.st_mode)
            or not _ownership_device_inode_matches(public, record)
        ):
            return False
        os.rmdir(folder)
        fsync_dir(folder.parent)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        return False if exc.errno in (errno.ENOTEMPTY, errno.EEXIST) else None


def _remove_created_folders(root, owned, progress):
    """Remove the folders the grab created, deepest first, stopping at the
    first one that holds something else or is not the folder it made.
    False when that should be tried again."""
    chain = _owned_chain(root, owned)
    if chain is None or chain[3] is None:
        return True
    anchor, folders, _parent_count, records, _leaf = chain
    cleanup = owned["directory_cleanup"]
    complete = cleanup.get("complete", False)
    if type(complete) is not bool:
        return False
    if complete:
        return True
    for index in reversed(range(len(records))):
        if records[index]["created"] is not True:
            break
        removed = _remove_created_folder(
            anchor, folders[:index + 1], records[:index + 1])
        if removed is None:
            return False
        if not removed:
            break
    cleanup["complete"] = True
    return _record_owned_progress(progress)


def _unlink_owned_path(root, owned, *, progress=None, outcome_out=None):
    """Remove exact owned leaves, then the empty folders the grab created."""
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

    pending = [
        entry for entry in [*companions, owned]
        if (_owned_deletion_record(entry, "file") or {}).get("state")
        != "removed"
    ]
    # Nothing is deleted unless every file left still matches its record.
    if any(_owned_leaf(root, entry) is None for entry in pending):
        return None
    for entry in pending:
        if not _remove_owned_leaf(root, entry, progress):
            _update_outcome()
            return None

    cleanup_complete = _remove_created_folders(root, owned, progress)
    _update_outcome(cleanup_pending=not cleanup_complete)
    if not cleanup_complete:
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
