"""Album repair mode: ISRC-anchored refill of truncated FLACs."""
import errno
import json
import os
import secrets
import sqlite3
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
from qobuz_librarian.api.search import get_album
from qobuz_librarian.library.backup import (
    BackupResult,
    _normalise_gap_fill_expected_receipts,
    backup_gap_fill_files,
    pin_unverified_upgrade_backup,
    restore_gap_fill_backup,
    retire_verified_repair_backup,
    warn_pin_failed,
)
from qobuz_librarian.library.catalog import (
    _close_migration_database_anchor,
    _fsync_migration_directories,
    _migration_database_anchor_matches,
    _migration_entry_record,
    _migration_name_missing,
    _migration_named_directory_matches,
    _migration_named_entry_matches,
    _MigrationEntryPreserved,
    _norm_isrc,
    _open_migration_database_anchor,
    _open_migration_directory,
    _open_migration_entry,
    _open_or_publish_migration_directory_at,
    _preflight_migration_database_anchor,
    _prepare_migration_database_transaction_name,
    _remove_empty_migration_directory_at,
    _rename_noreplace_at,
    _require_migration_delete_journal_mode,
    _require_no_migration_items_triggers,
    _rollback_migration_rename,
    find_album_dir_filesystem,
    find_qobuz_album_for_dir,
)
from qobuz_librarian.library.discovery import resolve_artist_dir
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
    read_album_dir,
)
from qobuz_librarian.library.sqlite_atomic import AtomicSQLiteWrite
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.executor import _execute_download_queue
from qobuz_librarian.repair_log import append_repair_log, scan_dir_for_isrc_repairs
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.errors import EXIT_AUTH, EXIT_GENERAL, die
from qobuz_librarian.ui_cli.logging import log, vlog


@dataclass(frozen=True)
class RepairRecovery:
    """The exact backup that must remain reachable until Repair settles it."""

    backup: BackupResult
    album_dir: Path
    stage: str
    reason: str
    retained: bool = True

    def as_record(self):
        receipt = self.backup.receipt
        if receipt is not None:
            receipt = json.loads(json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ))
        return {
            "version": 1,
            "kind": "repair-backup",
            "status": "retained" if self.retained else "resolved",
            "location": str(self.backup.path),
            "album_dir": str(Path(self.album_dir)),
            "stage": str(self.stage),
            "reason": str(self.reason),
            "complete": self.backup.complete is True,
            "requested": int(self.backup.requested),
            "backed_up": int(self.backup.backed_up),
            "receipt": receipt,
        }


class RepairRecoveryRequired(Exception):
    """Carry an exact retained backup across an exceptional Repair exit."""

    def __init__(self, recovery, cause):
        self.recovery = recovery
        self.cause = cause
        super().__init__(str(cause) or cause.__class__.__name__)


def _format_mmss(secs):
    """Format a duration in seconds as m:ss (e.g. 163.4 → '2:43')."""
    s = max(0, int(round(float(secs or 0))))
    return f"{s // 60}:{s % 60:02d}"


try:
    from mutagen.flac import FLAC as _FLAC
except Exception:
    _FLAC = None


def _snapshot_flac_metadata(path):
    """Full Vorbis-comment + embedded-picture snapshot of a FLAC, or None when
    it can't be read (mutagen missing, not a FLAC, unreadable)."""
    if _FLAC is None or not path:
        return None
    try:
        f = _FLAC(path)
    except Exception:
        return None
    return {
        "tags": {k: list(v) for k, v in (f.tags or {}).items()},
        "pictures": list(f.pictures),
    }


def _restore_flac_metadata(path, snap):
    """Replace a refilled FLAC's tags + embedded art with the snapshot from the
    truncated original it replaces, so only the audio changed, album, track
    number, title and art stay exactly as the user had them. Returns True on a
    successful write."""
    if _FLAC is None or not snap:
        return False
    try:
        f = _FLAC(path)
    except Exception:
        return False
    if f.tags is None:
        f.add_tags()
    else:
        f.tags.clear()
    for k, vals in snap["tags"].items():
        f.tags[k] = vals
    # Only swap embedded art when the original actually had some. A truncated
    # original downloaded without art (or with less) must not strip the
    # freshly-downloaded refill's Qobuz cover, that would downgrade the result.
    pics_ok = True
    if snap["pictures"]:
        f.clear_pictures()
        for pic in snap["pictures"]:
            try:
                f.add_picture(pic)
            except Exception:
                # Keep going, losing one malformed picture mustn't abort the
                # tag carry, but report the restore incomplete so the backup
                # (which still holds the original art) isn't deleted on it.
                pics_ok = False
    try:
        f.save()
    except Exception:
        return False
    return pics_ok


def _backup_source_by_isrc(verified_truncated, album_dir, backup_path):
    """Map each truncated track's ISRC to where its originals now live in the
    backup dir, so the refill can inherit the original's tags + embedded art
    by reading from disk at retag time. backup_gap_fill_files preserves each
    file's path relative to album_dir, so the backup copy is at the same
    relative spot.
    """
    out = {}
    for b in verified_truncated:
        isrc = _norm_isrc(b.get("isrc"))
        src = b.get("path") or ""
        if not isrc or not src:
            continue
        try:
            rel = Path(src).relative_to(album_dir)
        except ValueError:
            rel = Path(Path(src).name)
        cand = backup_path / rel
        if cand.exists():
            out.setdefault(isrc, []).append(cand)
    for paths in out.values():
        paths.sort()
    return out


def _retag_refills_in_staging(staged_dirs, source_by_isrc):
    """Carry the truncated originals' tags + art onto the matching refills (by
    ISRC) while they're still in staging, before beets files them. A
    recording that also appears on a compilation is downloaded by its track ID
    (so the audio is the right recording) but tagged by Qobuz for whatever
    album it files that ISRC under; without this the refill would import as
    the compilation, contradicting the folder it lives in.
    """
    if _FLAC is None:
        return set(source_by_isrc)
    if not source_by_isrc:
        return set()
    failed = set()
    remaining = {isrc: list(paths) for isrc, paths in source_by_isrc.items()}
    for d in staged_dirs:
        for fp in sorted(Path(d).rglob("*.flac")):
            try:
                isrc = _norm_isrc((_FLAC(fp).get("isrc") or [""])[0])
            except Exception:
                continue
            srcs = remaining.get(isrc)
            if not srcs:
                continue
            src = srcs.pop(0)
            snap = _snapshot_flac_metadata(src)
            if snap and _restore_flac_metadata(fp, snap):
                log.info(fmt(C.GRAY,
                    f"  ⤷  Kept original tags on refilled "
                    f"{truncate(fp.name, 40)}"))
            else:
                failed.add(isrc)
    failed.update(isrc for isrc, srcs in remaining.items() if srcs)
    return failed


def _make_retag_callback(retag_sources, retag_failed):
    """The pre-import retag hook the executor calls on the staged refills.

    An exception inside the carry surfaces in the executor's catch-and-log,
    but the carry state is then unknown, so every source is recorded as
    failed BEFORE the attempt and only the confirmed non-failures are cleared
    after. Without that, the backup resolution would read the empty set as
    "all tags carried" and delete the only copy of the originals' metadata."""
    def _retag_callback(staged_dirs):
        retag_failed.update(retag_sources)
        done = _retag_refills_in_staging(staged_dirs, retag_sources)
        retag_failed.intersection_update(done)
    return _retag_callback


def _resolve_parent_album(album_dir, artist_name, verified_truncated,
                          wanted_isrcs, token):
    """Pick the Qobuz album that should drive where the refill is filed. Qobuz
    files a recording under whichever album it returns first for the ISRC,
    often a compilation the track also appears on, so the most-common ISRC
    album can be the wrong edition.
    """
    try:
        match = find_qobuz_album_for_dir(
            album_dir, artist_name, token,
            prefer_hires=cfg.PREFER_HIRES, target_dir=album_dir)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        match = None
    if match and wanted_isrcs:
        have = {_norm_isrc(t.get("isrc"))
                for t in (match.get("tracks") or {}).get("items") or []}
        if wanted_isrcs.issubset(have):
            return match

    parent_ids = []
    for b in verified_truncated:
        aid = (b["qobuz_track"].get("album") or {}).get("id")
        if aid:
            parent_ids.append(aid)
    if parent_ids:
        most_common_aid = Counter(parent_ids).most_common(1)[0][0]
        try:
            album = get_album(most_common_aid, token)
            if album:
                return album
        except QobuzError as e:
            log.info(fmt(C.GRAY,
                f"  (parent-album lookup failed: {e}; using the folder name)"))

    return {
        "id": "repair",
        "title": album_dir.name,
        "artist": {"name": artist_name},
        "tracks": {"items": []},
    }


_REPAIR_TREE_MAX_DEPTH = 8


class _RepairAnchorChanged(OSError):
    """A retained repair path no longer has its original public name."""


class _RepairDatabaseCompensationFailed(OSError):
    """A committed beets path update could not be restored exactly."""


class _RepairRelocationUncertain(OSError):
    """The repair's file/database placement can no longer be proven."""

    def __init__(self, message, *, recovery_location=None):
        self.recovery_location = None
        if recovery_location is not None:
            location = Path(recovery_location)
            raw_location = os.fspath(location)
            if (
                location.is_absolute()
                and raw_location != "/proc/self/fd"
                and not raw_location.startswith("/proc/self/fd/")
            ):
                self.recovery_location = raw_location
        super().__init__(message)


class _RepairRelocationFailed(OSError):
    """A refill move failed, but its original placement was restored."""


def _same_file_identity(left, right):
    return (
        stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and (int(left.st_dev), int(left.st_ino))
        == (int(right.st_dev), int(right.st_ino))
    )


def _same_repair_file_content(before, after):
    return all(before[index] == after[index] for index in (0, 1, 2, 3, 5))


class _HeldMusicRoot:
    def __init__(self, path):
        self.path = Path(os.path.abspath(os.fsdecode(os.fspath(path))))
        self._fds = []
        self._names = []
        try:
            self._fds.append(_open_migration_directory(os.sep))
            for name in self.path.parts[1:]:
                child = _open_migration_directory(
                    name, dir_fd=self._fds[-1])
                if not _migration_named_directory_matches(
                        self._fds[-1], name, child):
                    os.close(child)
                    raise _RepairAnchorChanged(
                        "music root changed while it was opened")
                self._names.append(name)
                self._fds.append(child)
        except BaseException:
            self.close()
            raise
        if not self.matches():
            self.close()
            raise _RepairAnchorChanged(
                "music root changed while it was opened")

    @property
    def fd(self):
        return self._fds[-1]

    def matches(self):
        if not self._fds:
            return False
        try:
            if not _same_file_identity(
                    os.fstat(self._fds[0]),
                    os.stat(os.sep, follow_symlinks=False)):
                return False
        except (OSError, TypeError, ValueError):
            return False
        return all(
            _migration_named_directory_matches(parent, name, child)
            for parent, name, child in zip(
                self._fds, self._names, self._fds[1:])
        )

    def close(self):
        for descriptor in reversed(self._fds):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._fds.clear()


def _repair_relative_parts(root, path):
    absolute = Path(os.path.abspath(os.fsdecode(os.fspath(path))))
    try:
        relative = absolute.relative_to(root.path)
    except ValueError as exc:
        raise OSError("repair path is outside the music root") from exc
    if not relative.parts:
        raise OSError("repair path cannot be the music root")
    if any(part in ("", ".", "..") for part in relative.parts):
        raise OSError("repair path is malformed")
    return tuple(relative.parts)


def _repair_paths_same(left, right):
    try:
        return (
            os.path.abspath(os.fsdecode(os.fspath(left)))
            == os.path.abspath(os.fsdecode(os.fspath(right)))
        )
    except (OSError, TypeError, ValueError):
        return False


class _HeldRepairDirectory:
    def __init__(self, root, parts):
        self.root = root
        self.parts = ()
        self._fds = [os.dup(root.fd)]
        self._names = []
        try:
            for name in parts:
                self.open_child(name)
        except BaseException:
            self.close()
            raise
        if not self.matches():
            self.close()
            raise _RepairAnchorChanged(
                "repair directory changed while it was opened")

    @property
    def fd(self):
        return self._fds[-1]

    def open_child(self, name):
        child = _open_migration_directory(name, dir_fd=self.fd)
        if not _migration_named_directory_matches(self.fd, name, child):
            os.close(child)
            raise _RepairAnchorChanged(
                "repair directory changed while it was opened")
        self._names.append(name)
        self._fds.append(child)
        self.parts = (*self.parts, name)

    def publish_child(self, name, creation_journal):
        child, was_created = _open_or_publish_migration_directory_at(
            self.fd, name, created_out=creation_journal)
        if not _migration_named_directory_matches(self.fd, name, child):
            os.close(child)
            raise _RepairAnchorChanged(
                "repair directory changed while it was created")
        self._names.append(name)
        self._fds.append(child)
        self.parts = (*self.parts, name)
        return was_created

    def matches(self):
        return self.root.matches() and all(
            _migration_named_directory_matches(parent, name, child)
            for parent, name, child in zip(
                self._fds, self._names, self._fds[1:])
        )

    def close(self):
        for descriptor in reversed(self._fds):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._fds.clear()


def _require_repair_anchors(*chains):
    if not all(chain.matches() for chain in chains):
        raise _RepairAnchorChanged(
            "repair directories changed during relocation")


def _read_repair_isrc(file_fd):
    if _FLAC is None:
        return ""
    try:
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            value = (_FLAC(stream).get("isrc") or [""])[0]
    except Exception:
        return ""
    return _norm_isrc(value)


def _walk_held_repair_tree(landed, visit, directory_fd=None, relative=(),
                           links=()):
    if len(relative) > _REPAIR_TREE_MAX_DEPTH:
        raise OSError("refill folder is nested too deeply")
    current_fd = landed.fd if directory_fd is None else directory_fd

    def anchors_match():
        return landed.matches() and all(
            _migration_named_directory_matches(parent, name, child)
            for parent, name, child in links
        )

    if not anchors_match():
        raise _RepairAnchorChanged(
            "refill folder changed during discovery")
    names = sorted(os.listdir(current_fd))
    for name in names:
        if not anchors_match():
            raise _RepairAnchorChanged(
                "refill folder changed during discovery")
        named = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
        if stat.S_ISDIR(named.st_mode):
            child = _open_migration_directory(name, dir_fd=current_fd)
            try:
                if not _migration_named_directory_matches(
                        current_fd, name, child):
                    raise _RepairAnchorChanged(
                        "refill folder changed during discovery")
                _walk_held_repair_tree(
                    landed,
                    visit,
                    child,
                    (*relative, name),
                    (*links, (current_fd, name, child)),
                )
                if not _migration_named_directory_matches(
                        current_fd, name, child):
                    raise _RepairAnchorChanged(
                        "refill folder changed during discovery")
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(named.st_mode):
            raise OSError(errno.EINVAL,
                          "refill folder contains an unsafe entry", name)
        file_fd = _open_migration_entry(current_fd, name)
        try:
            identity = _migration_entry_record(os.fstat(file_fd))
            if identity != _migration_entry_record(named):
                raise _RepairAnchorChanged(
                    "refill file changed during discovery")
            visit((*relative, name), file_fd, identity)
            if (
                not anchors_match()
                or not _migration_named_entry_matches(
                    current_fd, name, file_fd)
                or _migration_entry_record(os.fstat(file_fd)) != identity
            ):
                raise _RepairAnchorChanged(
                    "refill file changed during discovery")
        finally:
            os.close(file_fd)
    if not anchors_match():
        raise _RepairAnchorChanged(
            "refill folder changed during discovery")


def _secure_refill_names(path):
    root = landed = None
    names = set()
    try:
        root = _HeldMusicRoot(cfg.MUSIC_ROOT)
        landed = _HeldRepairDirectory(
            root, _repair_relative_parts(root, path))

        def collect(relative, _file_fd, _identity):
            if relative[-1].lower().endswith(".flac"):
                names.add(relative[-1])

        _walk_held_repair_tree(landed, collect)
        return names
    finally:
        if landed is not None:
            landed.close()
        if root is not None:
            root.close()


_REPAIR_RECEIPT_IDENTITY_FIELDS = {
    "device",
    "inode",
    "size",
    "modified_ns",
    "changed_ns",
}


def _repair_receipt_identity(value, *, entry_mode):
    if (
        not isinstance(value, dict)
        or set(value) != _REPAIR_RECEIPT_IDENTITY_FIELDS
        or not all(type(value.get(field)) is int
                   for field in _REPAIR_RECEIPT_IDENTITY_FIELDS)
    ):
        raise OSError("repair import receipt has an invalid identity")
    return (
        value["device"],
        value["inode"],
        value["size"],
        value["modified_ns"],
        value["changed_ns"],
        entry_mode,
    )


def _repair_receipt_parts(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise OSError("repair import receipt has an invalid path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise OSError("repair import receipt has an invalid path")
    return tuple(relative.parts)


def _repair_receipt_items(root, receipt, expected_refills):
    try:
        receipt_root = Path(os.path.abspath(os.fspath(receipt["root"])))
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise OSError("repair import receipt has an invalid root") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("version") != 1
        or receipt.get("sealed") is not True
        or receipt_root != root.path
        or not root.matches()
    ):
        raise OSError("repair import receipt is not sealed for this library")
    root_identity = _repair_receipt_identity(
        receipt.get("root_identity"), entry_mode=stat.S_IFDIR)
    current_root = _migration_entry_record(os.fstat(root.fd))
    if root_identity[:2] != current_root[:2]:
        raise OSError("repair import receipt names a different library")
    items = receipt.get("items")
    if (
        not isinstance(items, list)
        or not items
        or (
            expected_refills is not None
            and len(items) != expected_refills
        )
    ):
        raise OSError("repair import receipt does not cover every refill")

    parsed = []
    created_directories = {}
    seen_paths = set()
    seen_files = set()
    for item in items:
        if not isinstance(item, dict):
            raise OSError("repair import receipt contains an invalid item")
        parts = _repair_receipt_parts(item.get("relative"))
        identity = _repair_receipt_identity(
            item.get("file"), entry_mode=stat.S_IFREG)
        scope = item.get("album_scope")
        if not isinstance(scope, dict):
            raise OSError("repair import receipt has no album scope")
        scope_parts = _repair_receipt_parts(scope.get("relative"))
        scope_identity = _repair_receipt_identity(
            scope.get("directory"), entry_mode=stat.S_IFDIR)
        if (
            len(scope_parts) >= len(parts)
            or parts[:len(scope_parts)] != scope_parts
            or parts in seen_paths
            or identity[:2] in seen_files
        ):
            raise OSError("repair import receipt contains conflicting items")
        seen_paths.add(parts)
        seen_files.add(identity[:2])
        created = item.get("created_directories")
        if not isinstance(created, list):
            raise OSError(
                "repair import receipt has invalid created directories")
        for record in created:
            if (
                not isinstance(record, dict)
                or set(record) != {
                    "relative", *_REPAIR_RECEIPT_IDENTITY_FIELDS
                }
            ):
                raise OSError(
                    "repair import receipt has invalid created directories")
            created_parts = _repair_receipt_parts(record.get("relative"))
            created_identity = _repair_receipt_identity(
                {
                    field: record[field]
                    for field in _REPAIR_RECEIPT_IDENTITY_FIELDS
                },
                entry_mode=stat.S_IFDIR,
            )
            if (
                len(created_parts) >= len(parts)
                or parts[:len(created_parts)] != created_parts
            ):
                raise OSError(
                    "repair import receipt has an unrelated created directory")
            existing = created_directories.get(created_parts)
            if existing is not None and existing != created_identity:
                raise OSError(
                    "repair import receipt has conflicting created directories")
            created_directories[created_parts] = created_identity
        parsed.append({
            "source_parts": parts[:-1],
            "relative": parts[len(scope_parts):],
            "landed_parts": scope_parts,
            "landed_identity": scope_identity,
            "name": parts[-1],
            "identity": identity,
        })
    return parsed, created_directories


def _discover_refill_files(root, receipt, wanted_isrcs, before_names,
                           expected_refills=None, before_parts=None):
    records = []
    landed = {}
    created = {}
    try:
        items, created_receipts = _repair_receipt_items(
            root, receipt, expected_refills)
        for parts, identity in created_receipts.items():
            directory = _HeldRepairDirectory(root, parts)
            if _migration_entry_record(os.fstat(directory.fd)) != identity:
                directory.close()
                raise _RepairAnchorChanged(
                    "receipt-created refill directory changed after sealing")
            created[parts] = directory
        for item in items:
            name = item["name"]
            # before_names is the census of one folder, so it can only speak
            # about files IN that folder. Matching on the bare name alone
            # rejected a refill that had landed somewhere else entirely just
            # because an unrelated album held a track of the same name, which
            # is the normal case for a track that appears on two records.
            # before_parts None means the folder is unknown, so stay strict.
            already_there = name in before_names and (
                before_parts is None or item["source_parts"] == before_parts)
            if not name.lower().endswith(".flac") or already_there:
                raise OSError(
                    "repair import receipt does not identify a new FLAC")
            scope = landed.get(item["landed_parts"])
            if scope is None:
                scope = _HeldRepairDirectory(root, item["landed_parts"])
                if (
                    _migration_entry_record(os.fstat(scope.fd))[:2]
                    != item["landed_identity"][:2]
                ):
                    scope.close()
                    raise _RepairAnchorChanged(
                        "repair import album changed after receipt sealing")
                landed[item["landed_parts"]] = scope

            source = file_fd = None
            try:
                source = _HeldRepairDirectory(root, item["source_parts"])
                file_fd = _open_migration_entry(source.fd, name)
                if (
                    not source.matches()
                    or not scope.matches()
                    or not _migration_named_entry_matches(
                        source.fd, name, file_fd)
                    or _migration_entry_record(os.fstat(file_fd))
                        != item["identity"]
                    or _read_repair_isrc(file_fd) not in wanted_isrcs
                    or _migration_entry_record(os.fstat(file_fd))
                        != item["identity"]
                ):
                    raise _RepairAnchorChanged(
                        "receipt-owned refill changed while it was selected")
                records.append({
                    "relative": item["relative"],
                    "source": source,
                    "file_fd": file_fd,
                    "identity": item["identity"],
                    "landed": scope,
                })
                source = file_fd = None
            finally:
                if file_fd is not None:
                    os.close(file_fd)
                if source is not None:
                    source.close()
        return records, list(landed.values()), list(created.values())
    except BaseException:
        _close_refill_records(records)
        for scope in landed.values():
            scope.close()
        for directory in created.values():
            directory.close()
        raise


def _close_refill_records(records):
    for record in records:
        try:
            os.close(record["file_fd"])
        except OSError:
            pass
        record["source"].close()
    records.clear()


def _cleanup_created_repair_dirs(created):
    for record in reversed(created):
        try:
            _remove_empty_migration_directory_at(
                record["parent_fd"], record["name"], record["directory_fd"])
        except OSError:
            pass
    _close_created_repair_dirs(created)


def _close_created_repair_dirs(created):
    for record in created:
        for key in ("directory_fd", "parent_fd"):
            try:
                os.close(record[key])
            except OSError:
                pass
    created.clear()


def _open_repair_destination(root, album_parts, relative_parent, source,
                             album, created):
    destination = _HeldRepairDirectory(root, album_parts)
    try:
        if not _same_file_identity(
                os.fstat(destination.fd), os.fstat(album.fd)):
            raise _RepairAnchorChanged(
                "repair album changed before relocation")
        for name in relative_parent:
            _require_repair_anchors(source, album, destination)
            destination.publish_child(name, created)
            _require_repair_anchors(source, album, destination)
        return destination
    except BaseException:
        destination.close()
        raise


def _repair_sql_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def _repair_item_rows_match(connection, columns, rows):
    if not rows:
        return True
    quoted = ", ".join(map(_repair_sql_identifier, columns))
    select_one = f"SELECT {quoted} FROM items WHERE rowid = ?"
    try:
        return all(
            connection.execute(select_one, (rowid,)).fetchone() == expected
            for rowid, expected in rows.items()
        )
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return False


def _compensate_repair_item_rows(database_anchor, columns, original_rows,
                                  expected_rows, anchors_match):
    """Restore exact rows and report the state after every commit outcome."""
    if not original_rows:
        return "original", None
    quoted = ", ".join(map(_repair_sql_identifier, columns))
    select_one = f"SELECT {quoted} FROM items WHERE rowid = ?"
    path_index = columns.index("path")
    deferred = None
    transaction = None
    source_state = "unknown"
    try:
        if not anchors_match():
            raise _RepairAnchorChanged(
                "repair paths changed before database compensation")
        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        transaction = AtomicSQLiteWrite(
            database_anchor,
            _migration_database_anchor_matches,
            connect=sqlite3.connect,
            timeout=timeout,
        )
        connection = transaction.open()
        connection.execute("BEGIN IMMEDIATE")
        if not anchors_match():
            raise _RepairAnchorChanged(
                "repair paths changed during database compensation")
        _require_no_migration_items_triggers(connection)
        _require_migration_delete_journal_mode(connection)
        current_rows = {
            rowid: connection.execute(select_one, (rowid,)).fetchone()
            for rowid in expected_rows
        }
        if all(
                current_rows[rowid] == original
                for rowid, original in original_rows.items()):
            connection.rollback()
            source_state = "original"
        elif any(
            current_rows[rowid] != expected
            for rowid, expected in expected_rows.items()
        ):
            connection.rollback()
        else:
            source_state = "forward"
            updates_exact = True
            for rowid, expected in expected_rows.items():
                original = original_rows[rowid]
                updated = connection.execute(
                    "UPDATE items SET path = ? "
                    "WHERE rowid = ? AND path = ?",
                    (original[path_index], rowid, expected[path_index]),
                )
                if updated.rowcount != 1:
                    updates_exact = False
                    break
            if updates_exact:
                updates_exact = not any(
                    connection.execute(
                        select_one, (rowid,)
                    ).fetchone() != original
                    for rowid, original in original_rows.items()
                )
            if not updates_exact:
                source_state = "unknown"
                connection.rollback()
            else:
                if not anchors_match():
                    raise _RepairAnchorChanged(
                        "repair paths changed during database compensation")
                transaction.commit_and_publish(
                    anchors_match,
                    lambda current: _repair_item_rows_match(
                        current, columns, original_rows),
                )
    except BaseException as exc:
        deferred = exc
    finally:
        if transaction is not None:
            try:
                transaction.close()
            except BaseException as exc:
                if deferred is None or (
                    isinstance(deferred, Exception)
                    and not isinstance(exc, Exception)
                ):
                    deferred = exc
                elif hasattr(deferred, "add_note"):
                    deferred.add_note(
                        "SQLite transaction cleanup also failed")
            published = transaction.published
            durable = transaction.durable
            uncertain = transaction.uncertain
        else:
            published = durable = uncertain = False
    if published and durable and not uncertain and anchors_match():
        return "original", deferred
    if not published and not uncertain and anchors_match():
        if source_state == "original":
            return "original", deferred
        if source_state == "forward":
            return "forward", deferred
    return "unknown", deferred


def _sync_repair_beets_row(old_relative, new_relative, anchors_match, state,
                           database_anchor=None):
    state.setdefault("forward_authoritative", False)
    state.setdefault("rollback_safe", True)
    state.setdefault("uncertain", False)
    owns_database_anchor = database_anchor is None
    if database_anchor is None:
        database_anchor = _open_migration_database_anchor()
        _preflight_migration_database_anchor(database_anchor, anchors_match)
        _prepare_migration_database_transaction_name(database_anchor)

    def current_anchors_match():
        return (
            _migration_database_anchor_matches(database_anchor)
            and anchors_match()
        )

    connection = None
    transaction = None
    original_rows = {}
    expected_rows = {}
    columns = ()
    try:
        if not current_anchors_match():
            raise _RepairAnchorChanged(
                "repair paths changed before database sync")
        if database_anchor is None or database_anchor["descriptor"] is None:
            if not current_anchors_match():
                raise _RepairAnchorChanged(
                    "repair paths changed during database sync")
            return
        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        transaction = AtomicSQLiteWrite(
            database_anchor,
            _migration_database_anchor_matches,
            connect=sqlite3.connect,
            timeout=timeout,
        )
        connection = transaction.open()
        if not current_anchors_match():
            raise _RepairAnchorChanged(
                "repair paths changed during database sync")
        connection.execute("BEGIN IMMEDIATE")
        if not current_anchors_match():
            raise _RepairAnchorChanged(
                "repair paths changed during database sync")
        _require_no_migration_items_triggers(connection)
        _require_migration_delete_journal_mode(connection)
        columns = tuple(
            row[1] for row in connection.execute(
                "PRAGMA table_info(items)").fetchall()
        )
        if "path" not in columns:
            raise OSError("beets items table has no path column")
        quoted = ", ".join(map(_repair_sql_identifier, columns))
        select_one = f"SELECT {quoted} FROM items WHERE rowid = ?"
        old_path = os.fsencode(os.fspath(Path(*old_relative)))
        new_path = os.fsencode(os.fspath(Path(*new_relative)))
        path_index = columns.index("path")
        rows = connection.execute(
            f"SELECT rowid, {quoted} FROM items WHERE path = ?",
            (old_path,),
        ).fetchall()
        for rowid, *values in rows:
            values = tuple(values)
            current = connection.execute(
                select_one, (rowid,)).fetchone()
            if current != values:
                raise OSError("beets row changed during repair sync")
            updated = connection.execute(
                "UPDATE items SET path = ? "
                "WHERE rowid = ? AND path = ?",
                (new_path, rowid, old_path),
            )
            if updated.rowcount != 1:
                raise OSError("beets row changed during repair sync")
            changed = list(values)
            changed[path_index] = new_path
            original_rows[rowid] = values
            expected_rows[rowid] = tuple(changed)
        if any(
            connection.execute(select_one, (rowid,)).fetchone() != expected
            for rowid, expected in expected_rows.items()
        ):
            raise OSError("beets row changed during repair sync")
        if not expected_rows:
            connection.rollback()
            if not current_anchors_match():
                raise _RepairAnchorChanged(
                    "repair paths changed during database sync")
            return
        if not current_anchors_match():
            raise _RepairAnchorChanged(
                "repair paths changed during database sync")
        transaction.commit_and_publish(
            current_anchors_match,
            lambda current: _repair_item_rows_match(
                current, columns, expected_rows),
        )
        if not current_anchors_match():
            raise _RepairAnchorChanged(
                "repair paths changed during database sync")
        state["forward_authoritative"] = bool(expected_rows)
        state["rollback_safe"] = not expected_rows
    except BaseException as exc:
        if transaction is not None:
            transaction.close()
            published = transaction.published
            durable = transaction.durable
            uncertain = transaction.uncertain
            transaction = None
            connection = None
            if uncertain or (published and not durable):
                state["uncertain"] = True
                state["rollback_safe"] = False
                if isinstance(exc, Exception):
                    raise _RepairDatabaseCompensationFailed(
                        "beets row publication is uncertain"
                    ) from exc
            elif published:
                state["forward_authoritative"] = True
                state["rollback_safe"] = False
                outcome, deferred = _compensate_repair_item_rows(
                    database_anchor,
                    columns,
                    original_rows,
                    expected_rows,
                    current_anchors_match,
                )
                if outcome == "original":
                    state["forward_authoritative"] = False
                    state["rollback_safe"] = True
                    if deferred is not None:
                        raise deferred
                elif outcome == "forward":
                    state["forward_authoritative"] = True
                    state["rollback_safe"] = False
                    if deferred is not None:
                        raise deferred
                    raise _RepairDatabaseCompensationFailed(
                        "beets row could not be restored after repair sync"
                    ) from exc
                else:
                    state["uncertain"] = True
                    state["rollback_safe"] = False
                    if deferred is not None and not isinstance(
                            deferred, Exception):
                        raise deferred
                    raise _RepairDatabaseCompensationFailed(
                        "beets row state is unknown after repair sync"
                    ) from (deferred or exc)
            else:
                state["forward_authoritative"] = False
                state["rollback_safe"] = True
        if isinstance(exc, sqlite3.Error):
            raise OSError(f"beets DB repair sync failed: {exc}") from exc
        raise
    finally:
        if transaction is not None:
            transaction.close()
        if owns_database_anchor:
            _close_migration_database_anchor(database_anchor)


def _repair_source_quarantine_state(source, name, private_name, file_fd):
    expected_private = _migration_named_entry_matches(
        source.fd, private_name, file_fd)
    expected_public = _migration_named_entry_matches(
        source.fd, name, file_fd)
    if expected_private and _migration_name_missing(source.fd, name):
        return "quarantined"
    if expected_public and _migration_name_missing(source.fd, private_name):
        return "source"
    if not _migration_name_missing(source.fd, private_name):
        unexpected_fd = None
        try:
            unexpected_fd = _open_migration_entry(source.fd, private_name)
            if _rollback_migration_rename(
                    source.fd, name,
                    source.fd, private_name, unexpected_fd):
                return "foreign-restored"
        except OSError:
            pass
        finally:
            if unexpected_fd is not None:
                os.close(unexpected_fd)
    return "changed"


def _reserve_repair_source_name(source):
    for _ in range(16):
        name = f".qobuz-repair-source-{secrets.token_hex(12)}"
        if _migration_name_missing(source.fd, name):
            return name
    raise OSError("could not reserve a private repair source name")


def _move_refill_file(root, album_parts, album, record, database_anchor):
    source = record["source"]
    file_fd = record["file_fd"]
    relative = record["relative"]
    name = relative[-1]
    destination = None
    created = []
    private_name = None
    quarantine_started = False
    quarantined = False
    rename_started = False
    renamed = False
    database_state = {
        "forward_authoritative": False,
        "rollback_safe": True,
        "uncertain": False,
    }
    try:
        destination = _open_repair_destination(
            root, album_parts, relative[:-1], source, album, created)
        _require_repair_anchors(source, album, destination)
        if (
            not _migration_named_entry_matches(source.fd, name, file_fd)
            or _migration_entry_record(os.fstat(file_fd))
                != record["identity"]
        ):
            raise _RepairAnchorChanged(
                "refill file changed before relocation")
        if not _migration_name_missing(destination.fd, name):
            _cleanup_created_repair_dirs(created)
            return False

        private_name = _reserve_repair_source_name(source)
        quarantine_started = True
        _rename_noreplace_at(
            source.fd, name, source.fd, private_name)
        quarantine_state = _repair_source_quarantine_state(
            source, name, private_name, file_fd)
        if quarantine_state != "quarantined":
            raise _RepairAnchorChanged(
                "refill source changed while it was quarantined")
        quarantined = True
        _fsync_migration_directories(source.fd)
        if (
            not source.matches()
            or not album.matches()
            or not destination.matches()
            or not _migration_named_entry_matches(
                source.fd, private_name, file_fd)
            or not _migration_name_missing(source.fd, name)
        ):
            raise _RepairAnchorChanged(
                "refill source changed during quarantine")

        rename_started = True
        _rename_noreplace_at(
            source.fd, private_name, destination.fd, name)
        renamed = True
        quarantined = False
        moved_identity = _migration_entry_record(os.fstat(file_fd))
        if (
            not source.matches()
            or not album.matches()
            or not destination.matches()
            or not _migration_name_missing(source.fd, name)
            or not _migration_name_missing(source.fd, private_name)
            or not _migration_named_entry_matches(
                destination.fd, name, file_fd)
            or not _same_repair_file_content(
                record["identity"], moved_identity)
        ):
            raise _RepairAnchorChanged(
                "repair paths changed during relocation")
        _fsync_migration_directories(source.fd, destination.fd)

        def moved_anchors_match():
            return (
                source.matches()
                and album.matches()
                and destination.matches()
                and _migration_database_anchor_matches(database_anchor)
                and _migration_name_missing(source.fd, name)
                and _migration_name_missing(source.fd, private_name)
                and _migration_named_entry_matches(
                    destination.fd, name, file_fd)
                and _migration_entry_record(os.fstat(file_fd))
                    == moved_identity
            )

        if not moved_anchors_match():
            raise _RepairAnchorChanged(
                "repair paths changed during relocation")
        _sync_repair_beets_row(
            (*source.parts, name),
            (*destination.parts, name),
            moved_anchors_match,
            database_state,
            database_anchor,
        )
        return True
    except BaseException as exc:
        if quarantine_started and not quarantined and not renamed:
            quarantine_state = _repair_source_quarantine_state(
                source, name, private_name, file_fd)
            if quarantine_state == "quarantined":
                quarantined = True
            elif quarantine_state not in ("source", "foreign-restored"):
                raise _RepairRelocationUncertain(
                    "refilled track source changed during quarantine"
                ) from exc
            elif quarantine_state == "foreign-restored":
                raise _RepairRelocationUncertain(
                    "a replacement appeared at the refill source"
                ) from exc
        if rename_started and not renamed and destination is not None:
            moved_forward = (
                _migration_name_missing(source.fd, private_name)
                and _migration_named_entry_matches(
                    destination.fd, name, file_fd)
            )
            stayed_source = (
                _migration_named_entry_matches(
                    source.fd, private_name, file_fd)
                and _migration_name_missing(destination.fd, name)
            )
            if moved_forward:
                renamed = True
                quarantined = False
            elif not stayed_source:
                raise _RepairRelocationUncertain(
                    "refilled track placement changed during relocation"
                ) from exc
        preserve_forward = (
            database_state["forward_authoritative"]
            or database_state["uncertain"]
            or isinstance(exc, _RepairDatabaseCompensationFailed)
        )
        restored = not renamed and not quarantined
        if renamed and not preserve_forward:
            restored = _rollback_migration_rename(
                source.fd,
                name,
                destination.fd,
                name,
                file_fd,
            )
            restored = (
                restored
                and source.matches()
                and album.matches()
                and destination.matches()
                and _migration_named_entry_matches(
                    source.fd, name, file_fd)
                and _migration_name_missing(destination.fd, name)
            )
        elif quarantined and not preserve_forward:
            restored = _rollback_migration_rename(
                source.fd,
                name,
                source.fd,
                private_name,
                file_fd,
            )
            restored = (
                restored
                and source.matches()
                and album.matches()
                and _migration_named_entry_matches(
                    source.fd, name, file_fd)
                and _migration_name_missing(source.fd, private_name)
            )
        if restored and not preserve_forward:
            _cleanup_created_repair_dirs(created)
        if preserve_forward or not restored:
            raise _RepairRelocationUncertain(
                "refilled track placement could not be safely restored"
            ) from exc
        if isinstance(exc, _RepairAnchorChanged):
            raise _RepairRelocationUncertain(str(exc)) from exc
        if renamed and isinstance(exc, OSError):
            raise _RepairRelocationFailed(str(exc)) from exc
        raise
    finally:
        _close_created_repair_dirs(created)
        if destination is not None:
            destination.close()


def _remove_exact_empty_landed_dir(landed):
    if not landed.matches():
        raise _RepairRelocationUncertain(
            "refill folder changed before cleanup")
    if os.listdir(landed.fd):
        return
    parent_fd = landed._fds[-2]
    name = landed._names[-1]
    removed = _remove_empty_migration_directory_at(
        parent_fd, name, landed.fd)
    if removed:
        if (
            not landed.root.matches()
            or not _migration_name_missing(parent_fd, name)
        ):
            raise _RepairRelocationUncertain(
                "refill folder changed during cleanup")
    elif not landed.matches():
        raise _RepairRelocationUncertain(
            "refill folder changed during cleanup")


def _relocate_refilled_into_album_dir(
        album_dir, landed_dir, wanted_isrcs, before_names, *,
        ownership_receipt=None, expected_refills=None,
        held_root=None, held_album=None, before_dir=None):
    """Move only exact files named by the sealed Beets import receipt."""
    del landed_dir  # The sealed receipt, not a cache/path guess, owns sources.
    root = held_root
    album = held_album
    owns_root = root is None
    owns_album = album is None
    database_anchor = None
    records = []
    landed_scopes = []
    created_directories = []
    try:
        if root is None:
            root = _HeldMusicRoot(cfg.MUSIC_ROOT)
        album_parts = _repair_relative_parts(root, album_dir)
        if album is None:
            album = _HeldRepairDirectory(root, album_parts)
        if (
            not _repair_paths_same(root.path, cfg.MUSIC_ROOT)
            or tuple(album.parts) != album_parts
            or not root.matches()
            or not album.matches()
        ):
            raise _RepairAnchorChanged(
                "repair target changed before refill relocation")
        before_parts = None
        if before_dir is not None:
            try:
                before_parts = _repair_relative_parts(root, before_dir)
            except OSError:
                before_parts = None
        records, landed_scopes, created_directories = _discover_refill_files(
            root,
            ownership_receipt,
            wanted_isrcs,
            before_names,
            expected_refills,
            before_parts=before_parts,
        )

        def preflight_anchors_match():
            return (
                root.matches()
                and album.matches()
                and all(scope.matches() for scope in landed_scopes)
                and all(
                    directory.matches()
                    for directory in created_directories
                )
                and all(
                    record["source"].matches()
                    and _migration_named_entry_matches(
                        record["source"].fd,
                        record["relative"][-1],
                        record["file_fd"],
                    )
                    and _migration_entry_record(
                        os.fstat(record["file_fd"])) == record["identity"]
                    for record in records
                )
            )

        database_anchor = _open_migration_database_anchor()
        _preflight_migration_database_anchor(
            database_anchor, preflight_anchors_match)
        _prepare_migration_database_transaction_name(database_anchor)
        if not preflight_anchors_match():
            raise _RepairAnchorChanged(
                "repair inputs changed after database preflight")
        moved = 0
        for record in records:
            if _move_refill_file(
                    root, album_parts, album, record, database_anchor):
                moved += 1
        if moved:
            for directory in sorted(
                    created_directories,
                    key=lambda value: len(value.parts),
                    reverse=True):
                if tuple(directory.parts) != album_parts:
                    _remove_exact_empty_landed_dir(directory)
            log.info(fmt(C.GRAY,
                f"  ⤷  Returned {moved} refilled track(s) to "
                f"{truncate(Path(album_dir).name, 40)}"))
        return moved
    except _MigrationEntryPreserved as exc:
        raise _RepairRelocationUncertain(
            "a refill entry was preserved for review",
            recovery_location=exc.location,
        ) from exc
    except (_RepairRelocationUncertain, _RepairRelocationFailed):
        raise
    except (_RepairAnchorChanged, OSError) as exc:
        raise _RepairRelocationUncertain(
            f"refill paths could not be safely retained: {exc}") from exc
    finally:
        _close_migration_database_anchor(database_anchor)
        _close_refill_records(records)
        for scope in landed_scopes:
            scope.close()
        for directory in created_directories:
            directory.close()
        if owns_album and album is not None:
            album.close()
        if owns_root and root is not None:
            root.close()


def _refills_present_in(album_dir, wanted_counts, baseline_counts):
    """True once each wanted ISRC has its refills back ON TOP of the files
    that already carried it.

    ``wanted_counts`` is a Counter of ISRC → how many truncated originals with
    that ISRC went to backup; ``baseline_counts`` is the ISRC census of what
    remained on disk after that move (None = the baseline couldn't be read,
    so the result is unverifiable and never True). A plain set membership test would pass when
    only ONE of two same-ISRC originals came back, and a bare count would let
    a healthy pre-existing file sharing the ISRC vouch for a refill that never
    returned, either way the backup holding the second file gets deleted. So
    require baseline + wanted of each ISRC, counted."""
    if not wanted_counts:
        return True
    if baseline_counts is None:
        return False
    present = Counter(_norm_isrc(et.get("isrc")) for et in read_album_dir(album_dir))
    return all(present.get(isrc, 0) >= baseline_counts.get(isrc, 0) + n
               for isrc, n in wanted_counts.items())


def _refills_intact(album_dir, wanted_counts, token, baseline_counts):
    """True only when none of the refilled ISRCs is still flagged truncated by
    a fresh duration scan. A presence check can't tell a complete refill from a
    short-but-decodable one, the exact failure repair exists to fix, so the
    rebuilt folder is re-verified against the same gate before the originals'
    backup is trusted as redundant. Caller guarantees wanted_counts is
    non-empty; AuthLost / QobuzUnavailable propagate so a transient outage
    can't read as "still truncated"."""
    if baseline_counts is None:
        return False
    try:
        scan = scan_dir_for_isrc_repairs(
            album_dir, token, deep=True, only_isrcs=set(wanted_counts))
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        return False
    # Require POSITIVE re-verification: every wanted ISRC must have re-matched
    # a Qobuz recording AND passed the truncation gate. Checking only "not
    # flagged truncated" was unsafe, a wanted ISRC whose post-repair lookup
    # transiently returned nothing (4xx, index lag, strict-equality miss)
    # lands in isrc_no_match, NOT verified_truncated, so it would read as
    # intact and the only good-enough original's backup would be deleted while
    # the refill is still short.
    verified_ok = Counter()
    for isrc, n in dict(scan.get("verified_ok_isrcs") or {}).items():
        verified_ok[_norm_isrc(isrc)] += n
    return all(verified_ok.get(isrc, 0) >= baseline_counts.get(isrc, 0) + n
               for isrc, n in wanted_counts.items())


def _prompt_library_album_for_repair(args, token):
    """Library-scoped picker for repair mode. Returns (artist_dir, album_dir)."""
    print()
    log.info(fmt(C.GRAY, "  Tip: '?' lists artists; '*' scans your whole library."))
    artist_dir = None
    while artist_dir is None:
        try:
            r = input(fmt(C.CYAN,
                "  Artist (blank/q to return, '*' for whole library): ")).strip()
        except EOFError:
            return None, None
        if not r or r.lower() in ("q", "quit", "exit"):
            return None, None
        if r in ("*", "all") or r.lower() == "library":
            # Sentinel: caller sweeps every album under MUSIC_ROOT.
            return "__ALL__", None
        if r == "?":
            artists = list_library_artists()
            if not artists:
                log.info(fmt(C.YELLOW, "  No artist directories found."))
                continue
            log.info(fmt(C.GRAY, f"  {len(artists)} artist(s) in library:"))
            for d in artists[:50]:
                log.info(fmt(C.GRAY, f"    • {d.name}"))
            if len(artists) > 50:
                log.info(fmt(C.GRAY, f"    ... and {len(artists) - 50} more"))
            continue
        artist_dir = resolve_artist_dir(r)
        if artist_dir is None:
            log.info(fmt(C.YELLOW,
                f"  ⚠  No artist matches {r!r} (try '?' for the list)."))

    albums = list_artist_album_dirs(artist_dir)
    if not albums:
        log.info(fmt(C.YELLOW,
            f"  ⚠  No album folders under {artist_dir.name}."))
        return None, None

    log.info(fmt(C.BOLD + C.CYAN,
        f"\n  Albums by {artist_dir.name} ({len(albums)}):"))
    for i, d in enumerate(albums, 1):
        log.info(fmt(C.WHITE, f"   {i:>3}.  {truncate(d.name, 72)}"))

    while True:
        try:
            r = input(fmt(C.CYAN,
                f"  Pick album (1-{len(albums)}, q to cancel): ")).strip()
        except EOFError:
            return None, None
        if not r or r.lower() in ("q", "quit", "exit"):
            return None, None
        try:
            idx = int(r)
        except ValueError:
            log.info(fmt(C.GRAY, "  Enter a number, or q."))
            continue
        if not 1 <= idx <= len(albums):
            log.info(fmt(C.GRAY,
                f"  Out of range, pick 1-{len(albums)}."))
            continue
        album_dir = albums[idx - 1]
        break

    return artist_dir, album_dir


def _verified_repair_source_receipts(verified_truncated, album_dir):
    """Require the exact album-relative files sealed by the repair scan."""
    album = Path(os.path.abspath(os.fsdecode(os.fspath(album_dir))))
    receipts = {}
    for entry in verified_truncated:
        try:
            source = Path(os.path.abspath(
                os.fsdecode(os.fspath(entry["path"]))))
            sealed = entry["source_receipt"]
            relative = sealed["relative"]
            parts = _repair_receipt_parts(relative)
            file_receipt = sealed["file"]
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise OSError(
                "repair scan has no exact source receipt; rescan the album"
            ) from exc
        if source != album.joinpath(*parts) or relative in receipts:
            raise OSError(
                "repair scan source receipt does not match the selected album"
            )
        receipts[relative] = file_receipt
    if len(receipts) != len(verified_truncated) or not receipts:
        raise OSError("repair scan source receipts are incomplete")
    return _normalise_gap_fill_expected_receipts(receipts)


def repair_album_dir(album_dir, verified_truncated, artist_name, args, token,
                     *, recovery_checkpoint=None):
    """Back up truncated originals, ISRC-refill them, resolve the backup.

    Non-interactive and self-contained, the CLI repair mode and the web
    Repair flow both call this so there is one implementation of the
    risky part. Forces no-upgrade for the run so a quality delta can never
    escalate a surgical repair into a full wipe-and-replace. Auth lost
    before the backup is taken aborts with the originals untouched;
    exceptional exits after backup carry the exact retained recovery. Returns
    {"n_ok", "n_fail", "backup"}. ``backup`` is the exact carried
    ``BackupResult`` while recovery data remains, never just its pathname.
    """
    saved_no_upgrade = getattr(args, "no_upgrade", False)
    held_root = None
    held_album = None
    retained_recovery = None

    def checkpoint_recovery(backup, stage, reason, *, required=False):
        nonlocal retained_recovery
        recovery = RepairRecovery(
            backup=backup,
            album_dir=Path(album_dir),
            stage=stage,
            reason=reason,
        )
        retained_recovery = recovery
        if recovery_checkpoint is None:
            return recovery, True
        try:
            persisted = recovery_checkpoint(recovery) is True
        except Exception as exc:
            persisted = False
            log.info(fmt(C.RED,
                f"  ✗  Couldn't save the Repair recovery record ({exc})."))
        if required and not persisted:
            log.info(fmt(C.RED,
                "  ✗  Repair stopped before downloading replacements "
                "because its recovery record could not be saved."))
        return recovery, persisted

    def resolve_recovery(backup, reason):
        nonlocal retained_recovery
        recovery = RepairRecovery(
            backup=backup,
            album_dir=Path(album_dir),
            stage="resolved",
            reason=reason,
            retained=False,
        )
        retained_recovery = None
        if recovery_checkpoint is not None:
            try:
                recovery_checkpoint(recovery)
            except Exception:
                pass

    def recovery_result():
        return retained_recovery.backup if retained_recovery is not None else None

    def interrupt_with_recovery(cause, stage, reason):
        recovery, _ = checkpoint_recovery(
            backup_path, stage, reason)
        raise RepairRecoveryRequired(recovery, cause) from cause

    args.no_upgrade = True
    try:
        try:
            source_receipts = _verified_repair_source_receipts(
                verified_truncated, album_dir)
        except OSError as exc:
            log.info(fmt(C.RED,
                f"  ✗  Repair needs a fresh exact scan ({exc}); "
                "nothing was changed."))
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": None,
            }

        # ── Resolve the refill plan before touching any files ────────────
        # Everything Qobuz-side happens up front, while the truncated
        # originals are still in place. If the parent-album lookup hits a
        # transient outage the repair aborts cleanly with nothing moved,
        # rather than stranding the only copies in the backup dir.
        wanted_isrcs = {_norm_isrc(b.get("isrc")) for b in verified_truncated}
        wanted_isrcs.discard("")
        # Per-ISRC counts of the originals going to backup, the presence gate
        # needs the multiset, not just the set, so two same-ISRC truncated files
        # (a .1.flac collision pair, or the same recording on two discs) aren't
        # treated as repaired when only one refill lands.
        wanted_counts = Counter(_norm_isrc(b.get("isrc")) for b in verified_truncated)
        wanted_counts.pop("", None)

        album = _resolve_parent_album(album_dir, artist_name,
                                      verified_truncated, wanted_isrcs, token)
        # A synthetic fallback has no real catalog id, so it can't predict
        # an on-disk landing dir. Skip the prediction in that case.
        real_album_id = (album.get("id")
                         if album.get("id") not in (None, "repair") else None)

        qobuz_tracks_full = (album.get("tracks") or {}).get("items") or []
        missing_tracks = [b["qobuz_track"] for b in verified_truncated]

        present_synth = [{} for _ in range(max(
            0, len(qobuz_tracks_full) - len(missing_tracks)))]

        # force_track_by_track: repair must replace exactly the truncated
        # tracks, never re-rip the whole album URL. The flag makes this
        # un-bypassable regardless of the executor's missing/total ratio.
        qi = _build_queue_item(
            album=album,
            album_dir=album_dir,
            label=f"{artist_name} - {album_dir.name}  [repair]",
            missing=missing_tracks,
            present=present_synth,
            upgrade_only=False,
            auto_upgrade=False,
            force_track_by_track=True,
        )
        qi["_capture_import_ownership"] = True

        landed_pre = (find_album_dir_filesystem(album)
                      if real_album_id is not None else None)
        before_names = set()
        before_dir = None
        if landed_pre is not None and not _repair_paths_same(
                landed_pre, album_dir):
            try:
                before_names = _secure_refill_names(landed_pre)
                before_dir = landed_pre
            except OSError as exc:
                log.info(fmt(C.RED,
                    f"  ✗  The refill folder could not be read safely "
                    f"({exc}); nothing was changed."))
                return {
                    "n_ok": 0,
                    "n_fail": len(verified_truncated),
                    "imported": False,
                    "backup": None,
                }

        # Bind the configured library root and the selected album before the
        # first backup/download mutation. Path re-opening after a long import
        # cannot distinguish the original album from a same-name replacement.
        try:
            held_root = _HeldMusicRoot(cfg.MUSIC_ROOT)
            held_album = _HeldRepairDirectory(
                held_root, _repair_relative_parts(held_root, album_dir))
            _require_repair_anchors(held_album)
        except OSError as exc:
            if held_album is not None:
                held_album.close()
                held_album = None
            if held_root is not None:
                held_root.close()
                held_root = None
            log.info(fmt(C.RED,
                f"  ✗  The selected album could not be held safely "
                f"({exc}); nothing was changed."))
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": None,
            }

        # ── Back up the truncated originals (plan in hand) ───────────────
        broken_paths = [b["path"] for b in verified_truncated]
        backup_path = backup_gap_fill_files(
            broken_paths,
            album_dir,
            expected_receipts=source_receipts,
        )
        if backup_path is None:
            log.info(fmt(C.RED,
                "  ✗  Backup could not start, aborting repair without "
                "changing the selected files."))
            return {"n_ok": 0, "n_fail": len(verified_truncated), "backup": None}
        if getattr(backup_path, "complete", False) is not True:
            checkpoint_recovery(
                backup_path,
                "backup",
                "The backup stopped partway through; Repair is attempting "
                "to restore every moved original.",
            )
            restored = 0
            if (getattr(backup_path, "backed_up", 0) > 0
                    and held_album.matches()):
                try:
                    restored = restore_gap_fill_backup(backup_path, album_dir)
                except BaseException as exc:
                    recovery, _ = checkpoint_recovery(
                        backup_path,
                        "restore",
                        "The backup stopped partway through and automatic "
                        "restoration was interrupted.",
                    )
                    raise RepairRecoveryRequired(recovery, exc) from exc
            if (restored == getattr(backup_path, "backed_up", 0)
                    and restored > 0 and not backup_path.exists()):
                resolve_recovery(
                    backup_path,
                    "Every moved original was restored before Repair stopped.",
                )
                log.info(fmt(C.YELLOW,
                    "  ⚠  Backup stopped partway through; every moved original "
                    "was restored, so Repair was safely aborted."))
                return {
                    "n_ok": 0,
                    "n_fail": len(verified_truncated),
                    "imported": False,
                    "backup": None,
                }
            checkpoint_recovery(
                backup_path,
                "backup",
                "The backup stopped partway through and could not be fully "
                "restored.",
            )
            log.info(fmt(C.RED,
                "  ✗  Backup stopped partway through, Repair was aborted. "
                f"Restored {restored} moved file(s); remaining recovery files "
                f"are at:\n     {backup_path}"))
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": recovery_result(),
            }

        _initial_recovery, recovery_saved = checkpoint_recovery(
            backup_path,
            "backup",
            "Original files are held while replacement tracks are downloaded.",
            required=True,
        )
        if not recovery_saved:
            restored = 0
            if held_album.matches():
                try:
                    restored = restore_gap_fill_backup(backup_path, album_dir)
                except BaseException as exc:
                    recovery, _ = checkpoint_recovery(
                        backup_path,
                        "restore",
                        "Repair could not save its recovery record and "
                        "automatic restoration was interrupted.",
                    )
                    raise RepairRecoveryRequired(recovery, exc) from exc
            if (restored == backup_path.backed_up
                    and restored > 0 and not backup_path.exists()):
                resolve_recovery(
                    backup_path,
                    "Every moved original was restored after recovery "
                    "persistence failed.",
                )
                return {
                    "n_ok": 0,
                    "n_fail": len(verified_truncated),
                    "imported": False,
                    "backup": None,
                }
            checkpoint_recovery(
                backup_path,
                "backup",
                "Repair could not save its recovery record and automatic "
                "restore did not complete.",
            )
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": recovery_result(),
            }

        def pin_repair_recovery(note):
            if (backup_path.exists()
                    and not pin_unverified_upgrade_backup(backup_path, note)):
                warn_pin_failed(backup_path)

        if not held_album.matches():
            pin_repair_recovery(
                "repair backup kept, the selected album changed after backup")
            checkpoint_recovery(
                backup_path,
                "backup",
                "The selected album changed after its originals were backed up.",
            )
            log.info(fmt(C.RED,
                "  ✗  The selected album changed during backup; "
                f"the originals are preserved at:\n     {backup_path}"))
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": recovery_result(),
            }
        log.info(fmt(C.GRAY,
            f"  ⟳  Moved {len(verified_truncated)} broken file(s) "
            f"to backup: {backup_path.name}"))

        # Baseline: ISRC counts of what remains on disk now that the truncated
        # originals are in the backup. The presence/intact gates below compare
        # against baseline + wanted, so a healthy PRE-EXISTING file sharing a
        # refill's ISRC can't vouch for a refill that never came back.
        _bl_errs = []
        baseline_counts = Counter(
            _norm_isrc(et.get("isrc"))
            for et in read_album_dir(album_dir, walk_errors=_bl_errs))
        baseline_counts.pop("", None)
        if _bl_errs:
            baseline_counts = None

        # Carry the truncated originals' own tags + art onto the refills
        # before beets files them. The audio is fetched by track ID (the right
        # recording), but Qobuz returns its compilation/single album for that
        # ISRC, so without this the refill would import tagged + named for a
        # different album than the folder it lives in.
        retag_sources = _backup_source_by_isrc(
            verified_truncated, album_dir, backup_path)
        retag_failed = set()
        if retag_sources:
            qi["pre_import_retag"] = _make_retag_callback(
                retag_sources, retag_failed)

        try:
            _execute_download_queue([qi], args, token)
        except AuthLost as exc:
            # Don't auto-restore: a multi-track refill may have already written
            # good replacements, and moving the truncated originals back would
            # clobber them (the same reasoning as the interrupt branch below).
            # Preserve the backup and point the user at it.
            if backup_path and backup_path.exists():
                pin_repair_recovery(
                    "repair backup kept, authentication was lost mid-refill")
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Auth lost mid-refill, truncated originals preserved at:\n"
                    f"     {backup_path}\n"
                    f"     Re-run Repair once your token is refreshed, or restore "
                    f"them by hand."))
            interrupt_with_recovery(
                exc,
                "refill",
                "Authentication was lost while replacement tracks were "
                "being downloaded.",
            )
        except BaseException as exc:
            # Interrupted or an unexpected failure mid-refill. Don't auto-
            # restore: a partly-succeeded refill may already have written good
            # replacements, and moving the truncated originals back would
            # clobber them.
            if backup_path and backup_path.exists():
                pin_repair_recovery(
                    "repair backup kept, refill was interrupted")
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Repair interrupted, truncated originals preserved at:\n"
                    f"     {backup_path}\n"
                    f"     Re-run Repair to retry, or restore them by hand."))
            interrupt_with_recovery(
                exc,
                "refill",
                "Repair stopped while replacement tracks were being "
                "downloaded.",
            )

        if not held_album.matches():
            pin_repair_recovery(
                "repair backup kept, album or library root changed during "
                "refill")
            log.info(fmt(C.RED,
                "  ✗  The selected album or library root changed during "
                f"the refill. The originals are preserved at:\n     {backup_path}"))
            checkpoint_recovery(
                backup_path,
                "refill",
                "The selected album or library root changed while "
                "replacement tracks were downloaded.",
            )
            return {
                "n_ok": 0,
                "n_fail": max(qi.get("n_fail", 0), len(verified_truncated)),
                "imported": False,
                "backup": recovery_result(),
            }

        # ── Put the refill back where it belongs ─────────────────────────
        # beets files by tags, so a track whose canonical Qobuz album differs
        # from album_dir (EPs, compilations, bonus tracks) lands in a stray
        # folder. Move it home before judging success.
        landed_post = qi.get("_resolved_post_dir") or find_album_dir_filesystem(album)
        landed_post_path = Path(landed_post) if landed_post else None
        relocation_ok = True
        # When every refill failed to download there is no import receipt to
        # read, and asking for one reports the honest failure as "the final
        # location could not be proven" with a placement-stage recovery record.
        # Nothing landed, so nothing needs placing.
        nothing_landed = (
            qi.get("n_ok", 0) <= 0 and qi.get("_import_ownership") is None
        )
        try:
            if not nothing_landed:
                _relocate_refilled_into_album_dir(
                    album_dir,
                    landed_post_path,
                    wanted_isrcs,
                    before_names,
                    ownership_receipt=qi.get("_import_ownership"),
                    expected_refills=qi.get("n_ok", 0),
                    held_root=held_root,
                    held_album=held_album,
                    before_dir=before_dir,
                )
        except _RepairRelocationUncertain as exc:
            pin_repair_recovery(
                "repair backup kept, refill location could not be proven")
            recovery_reason = (
                "The replacement track's final location could not be proven."
            )
            preserved_note = ""
            if exc.recovery_location is not None:
                recovery_reason += (
                    " An additional refill entry was preserved at "
                    f"{exc.recovery_location}. Keep it until the album has "
                    "been reviewed."
                )
                preserved_note = (
                    "\n     An additional refill entry was kept for review "
                    f"at:\n     {exc.recovery_location}"
                )
            log.info(fmt(C.RED,
                f"  ✗  The refilled track's final location could not be "
                f"proven ({exc}). The originals are preserved at:\n"
                f"     {backup_path}{preserved_note}"))
            checkpoint_recovery(
                backup_path,
                "placement",
                recovery_reason,
            )
            return {
                "n_ok": 0,
                "n_fail": max(qi.get("n_fail", 0), len(verified_truncated)),
                "imported": False,
                "backup": recovery_result(),
            }
        except OSError as exc:
            relocation_ok = False
            log.info(fmt(C.YELLOW,
                f"  ⚠  The refill could not be returned safely ({exc}); "
                "the original will be restored."))
        except BaseException as exc:
            if backup_path and backup_path.exists():
                pin_repair_recovery(
                    "repair backup kept, refill placement was interrupted")
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Repair interrupted during refill placement, "
                    f"the originals are preserved at:\n     {backup_path}"))
            interrupt_with_recovery(
                exc,
                "placement",
                "Repair stopped while replacement tracks were being placed.",
            )

        n_fail_final = qi.get("n_fail", 0)
        n_ok_final = qi.get("n_ok", 0)
        download_clean = (relocation_ok and n_fail_final == 0 and n_ok_final > 0
                          and qi.get("imported", False))
        # "Back in place" = the refilled files returned to album_dir. Success
        # additionally requires they're verifiably NOT still truncated, a
        # presence check alone would let a short re-rip pass, and "no ISRC to
        # verify by" is unproven, not proven.
        back_in_place = download_clean and _refills_present_in(
            album_dir, wanted_counts, baseline_counts)
        if back_in_place and bool(wanted_isrcs):
            try:
                repaired = _refills_intact(album_dir, wanted_counts, token,
                                           baseline_counts)
            except (AuthLost, QobuzUnavailable) as exc:
                # _refills_intact verifies via Qobuz, so a token loss / outage
                # here aborts before the backup-resolution block below, leaving
                # the truncated originals orphaned in the backup dir with no
                # pointer for the user. Surface the location (same as the
                # download-phase AuthLost branch) before re-raising.
                if backup_path and backup_path.exists():
                    pin_repair_recovery(
                        "repair backup kept, final refill verification was "
                        "unavailable")
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Couldn't verify the re-downloaded tracks "
                        f"(auth lost / Qobuz unavailable), truncated originals "
                        f"preserved at:\n     {backup_path}\n"
                        f"     Re-run Repair once Qobuz is reachable, or restore "
                        f"them by hand."))
                interrupt_with_recovery(
                    exc,
                    "verification",
                    "Qobuz became unavailable before the replacement tracks "
                    "could be verified.",
                )
        else:
            repaired = False

        # ── Backup resolution ────────────────────────────────────────────
        if backup_path and backup_path.exists():
            if repaired and retag_failed:
                # The audio is verifiably fixed, but the originals' tags/art
                # couldn't be carried onto some refills, the backup holds the
                # only copy of that metadata, so keep it. Pin it too: the age
                # sweep proves redundancy by same-path same-or-larger bytes,
                # which the (usually larger) refill satisfies, without the pin
                # the only copy of those tags would be reaped on schedule.
                if not pin_unverified_upgrade_backup(
                        backup_path,
                        "repair kept, original tags/art not carried onto the "
                        "refill; this backup holds their only copy"):
                    warn_pin_failed(backup_path)
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Repaired, but the original tags/art couldn't be "
                    f"carried onto {len(retag_failed)} refilled track(s), "
                    f"keeping the backup; it still holds them.\n"
                    f"     Backup: {backup_path}"))
                checkpoint_recovery(
                    backup_path,
                    "verification",
                    "The replacement audio was verified, but the original "
                    "tags or artwork could not be carried onto it.",
                )
            elif repaired:
                if retire_verified_repair_backup(backup_path):
                    resolve_recovery(
                        backup_path,
                        "The replacement tracks verified, so the truncated "
                        "originals' backup was removed.",
                    )
                    log.info(fmt(C.GREEN,
                        "  ✓  Replacement tracks verified, removed the "
                        "truncated originals' backup."))
                else:
                    if not pin_unverified_upgrade_backup(
                            backup_path,
                            "repair backup kept, could not be proven "
                            "redundant after a verified refill"):
                        warn_pin_failed(backup_path)
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Replacement tracks verified, but the originals' "
                        "backup couldn't be proven redundant, keeping it:\n"
                        f"     {backup_path}"))
                    checkpoint_recovery(
                        backup_path,
                        "verification",
                        "The replacement tracks verified, but the originals' "
                        "backup could not be proven redundant.",
                    )
            elif back_in_place:
                # The re-downloaded tracks are physically back but couldn't be
                # verified as intact (still short of the listed length, or no
                # ISRC to check against). Keep the originals, deleting the
                # only other copy on a presence check alone is how a bad re-
                # rip silently replaces a good-enough track with a worse one.
                pin_repair_recovery(
                    "repair backup kept, replacement could not be verified "
                    "intact")
                log.info(fmt(C.YELLOW,
                    f"  ⚠  The re-download is also short of its listed length, "
                    f"so Qobuz's own copy looks no better than yours. The "
                    f"album now holds that download and your original is kept "
                    f"in the backup below; restore it from Settings, "
                    f"Diagnostics to put it back. Re-running won't change "
                    f"this. Backup:\n     {backup_path}"))
                checkpoint_recovery(
                    backup_path,
                    "verification",
                    "The replacement is in the album but could not be shown "
                    "to be any better than the original, which is kept here. "
                    "Restore puts the original back.",
                )
            elif n_fail_final > 0:
                pin_repair_recovery(
                    "repair backup kept, one or more refills failed")
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {n_fail_final} track(s) failed to re-download. "
                    f"Truncated originals preserved at:\n     {backup_path}"))
                checkpoint_recovery(
                    backup_path,
                    "refill",
                    "One or more replacement tracks failed to download.",
                )
            else:
                # Downloaded but the replacement never made it back into
                # album_dir (beets didn't import, or filed it where we
                # couldn't reclaim it). Restore so the album is at least back
                # to its pre-repair state instead of silently short a track.
                log.info(fmt(C.YELLOW,
                    "  ⚠  Refill didn't return to the album folder, "
                    "restoring truncated originals so it isn't left short."))
                try:
                    n_restored = restore_gap_fill_backup(
                        backup_path, album_dir)
                except BaseException as exc:
                    pin_repair_recovery(
                        "repair backup kept, automatic restore was "
                        "interrupted")
                    interrupt_with_recovery(
                        exc,
                        "restore",
                        "Automatic restoration stopped before every original "
                        "was returned.",
                    )
                if n_restored and not backup_path.exists():
                    resolve_recovery(
                        backup_path,
                        "The refill did not return to the album, so every "
                        "original was restored.",
                    )
                    log.info(fmt(C.GREEN,
                        "  ✓  Restored truncated originals; re-run Repair to retry."))
                else:
                    pin_repair_recovery(
                        "repair backup kept, automatic restore was partial or "
                        "failed")
                    log.info(fmt(C.RED,
                        f"  ✗  Restored {n_restored} track(s); remaining "
                        f"originals are at:\n     {backup_path}"))
                    checkpoint_recovery(
                        backup_path,
                        "restore",
                        "The refill did not return to the album and automatic "
                        "restoration was incomplete.",
                    )

        # ── Replaced-tracks log (only on a genuine, in-place repair) ──────
        # A verified refill is a repair whether or not the originals' backup
        # could also be retired; a kept backup rides along in "backup".
        if repaired:
            log_entries = [{
                "artist": artist_name,
                "album":  album_dir.name,
                "title":  b["title"],
            } for b in verified_truncated]
            if append_repair_log(log_entries):
                log.info(fmt(C.CYAN,
                    f"  📋  Logged {len(log_entries)} replaced track(s), "
                    f"refresh these albums on any offline client:\n"
                    f"     {cfg.REPAIR_LOG_PATH}"))

        return {
            "n_ok": n_ok_final if repaired else 0,
            "n_fail": (n_fail_final if repaired
                       else max(n_fail_final, len(verified_truncated))),
            "imported": repaired,
            "backup": recovery_result(),
        }
    finally:
        if held_album is not None:
            held_album.close()
        if held_root is not None:
            held_root.close()
        args.no_upgrade = saved_no_upgrade


_REPAIR_AUTH_LOST = ("\n✗  Auth lost. Set QOBUZ_USER_AUTH_TOKEN, or open the "
                     "Settings page in the web UI to update your token.\n")


def _report_repair_recovery(backup, *, reason=""):
    """Tell an interactive caller exactly where retained originals remain."""
    if not isinstance(backup, BackupResult):
        return False
    count = backup.backed_up
    requested = backup.requested
    detail = f"{count} of {requested} original file(s)"
    if reason:
        log.info(fmt(C.YELLOW, f"  ⚠  {reason}"))
    log.info(fmt(C.YELLOW,
        f"  ⚠  Recovery needed: {detail} remain at:\n"
        f"     {backup.path}\n"
        "     Keep this folder until the album has been checked."))
    return True


def _scan_report_repair(album_dir, artist_name, args, token, deep=True,
                        quiet=False):
    """Scan one album dir by ISRC, report, confirm, and repair. Returns
    "repaired" | "clean" | "attention" | "skipped" | "failed" | "recovery".
    """
    if not quiet:
        section(f"Repair scan, {truncate(album_dir.name, 60)}")
        log.info(fmt(C.GRAY,
            "  Resolving every FLAC by ISRC against Qobuz "
            "(no album-edition guessing) …"))

    scan = scan_dir_for_isrc_repairs(album_dir, token, deep=deep)
    verified_truncated = scan["verified_truncated"]
    verified_ok        = scan["verified_ok"]
    isrc_no_match      = scan["isrc_no_match"]
    no_isrc_tag        = scan["no_isrc_tag"]
    isrc_mismatch      = scan.get("isrc_mismatch") or []

    if (quiet and not verified_truncated and not isrc_mismatch and not any(
            x.get("diagnostic") for x in (*isrc_no_match, *no_isrc_tag))):
        return "clean"
    if quiet:
        # Commit the caller's \r progress line and head the report.
        print()
        log.info(fmt(C.BOLD + C.WHITE,
            f"  {artist_name} - {truncate(album_dir.name, 50)}"))

    log.info(fmt(C.GRAY,
        f"  {verified_ok} verified ok  ·  "
        f"{len(verified_truncated)} verified truncated  ·  "
        f"{len(isrc_no_match)} ISRC has no Qobuz match  ·  "
        f"{len(no_isrc_tag)} no ISRC tag  ·  "
        f"{len(isrc_mismatch)} ISRC names another song"))

    if isrc_no_match:
        log.info(fmt(C.GRAY,
            "\n  Skipped (ISRC tag present but no Qobuz match, "
            "Apple Music import or removed from Qobuz?):"))
        for x in isrc_no_match[:10]:
            diag = x.get("diagnostic")
            if diag:
                log.info(fmt(C.YELLOW,
                    f"    • {truncate(x['title'], 50)}  "
                    f"[isrc={x['isrc']}], {diag}"))
            else:
                log.info(fmt(C.GRAY,
                    f"    • {truncate(x['title'], 50)}  "
                    f"[isrc={x['isrc']}]"))
        if len(isrc_no_match) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(isrc_no_match) - 10} more"))
    if no_isrc_tag:
        log.info(fmt(C.GRAY,
            "\n  Skipped (no ISRC tag, can't verify recording "
            "identity, refusing to guess):"))
        for x in no_isrc_tag[:10]:
            diag = x.get("diagnostic")
            if diag:
                log.info(fmt(C.YELLOW,
                    f"    • {truncate(x['title'], 60)}: {diag}"))
            else:
                log.info(fmt(C.GRAY,
                    f"    • {truncate(x['title'], 60)}"))
        if len(no_isrc_tag) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(no_isrc_tag) - 10} more"))

    # Two different outcomes share this bucket, so they can't share one
    # heading: a short file is genuinely left alone, a damaged one still needs
    # dealing with and just can't be refilled a track at a time.
    mismatch_damaged = [x for x in isrc_mismatch if x.get("diagnostic")]
    mismatch_short = [x for x in isrc_mismatch if not x.get("diagnostic")]
    unresolved_damage = bool(
        mismatch_damaged
        or any(x.get("diagnostic") for x in (*isrc_no_match, *no_isrc_tag))
    )

    def _mismatch_row(x, colour):
        log.info(
            fmt(colour, f"    • {truncate(x.get('local_title') or '?', 40)}")
            + fmt(C.GRAY,
                f"   {_format_mmss(x['file_length']):>5}"
                f" / {_format_mmss(x['qobuz_duration']):>5}"
                f"  matched \"{truncate(x.get('title') or '?', 30)}\""
                f"  [isrc={x.get('isrc')}]"))

    if mismatch_damaged:
        log.info(fmt(C.YELLOW,
            "\n  Damaged, but the ISRC names a different song, so a "
            "single-track refill would fetch that song instead. Re-download "
            "the whole album, or fix the ISRC tag first:"))
        for x in mismatch_damaged[:10]:
            _mismatch_row(x, C.YELLOW)
        if len(mismatch_damaged) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(mismatch_damaged) - 10} more"))

    if mismatch_short:
        log.info(fmt(C.GRAY,
            "\n  Left alone (runs short, but its ISRC names a different "
            "song, so the length proves nothing):"))
        for x in mismatch_short[:10]:
            _mismatch_row(x, C.WHITE)
        if len(mismatch_short) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(mismatch_short) - 10} more"))

    if not verified_truncated:
        if unresolved_damage:
            log.info(fmt(
                C.YELLOW,
                "\n  ⚠  Damage was found, but no affected track was safe "
                "to refill automatically. Follow the guidance above.",
            ))
        elif not quiet:
            log.info(fmt(C.GREEN,
                "\n  ✓  No verified-truncated tracks. Nothing to repair."))
        return "attention" if unresolved_damage else "clean"

    log.info(fmt(C.YELLOW + C.BOLD,
        f"\n  ⚠  {len(verified_truncated)} truncated file(s) "
        "(ISRC-verified, safe to repair):"))
    log.info(fmt(C.GRAY,
        "    track  title                                              "
        "  on-disk / Qobuz   ISRC"))
    for b in verified_truncated:
        qt = b["qobuz_track"]
        # Name the row after the file on disk. The matched recording's own
        # title is the right thing to re-download and the wrong thing to label
        # a file with, because a mis-tagged ISRC makes them different songs.
        title = b.get("local_title") or b["title"]
        ver = qt.get("version") or ""
        if ver and ver.lower() not in title.lower():
            title = f"{title} ({ver})"
        tnum = b.get("local_track_number") or b["track_number"]
        tnum_str = f"#{tnum:>2}" if tnum else "   "
        log.info(
            fmt(C.WHITE, f"    {tnum_str}  {truncate(title, 50):<50}")
            + fmt(C.GRAY,
                f"   {_format_mmss(b['file_length']):>5}"
                f" / {_format_mmss(b['qobuz_duration']):>5}"
                f"  {b['isrc']}"))

    # Repair moves the truncated originals aside before re-ripping, so it has to
    # stop here under --dry-run, reaching repair_album_dir would mutate the
    # filesystem (and an interrupt mid-repair could strand the originals).
    if getattr(args, "dry_run", False):
        log.info(fmt(C.YELLOW,
            f"\n  --dry-run: would re-download {len(verified_truncated)} "
            "ISRC-verified track(s). Nothing changed."))
        return "skipped"

    if not args.yes:
        try:
            r = input(fmt(C.CYAN,
                f"\n  Re-download {len(verified_truncated)} "
                "ISRC-verified track(s)? [y/N]: ")
            ).strip().lower()
        except EOFError:
            r = ""
        if r not in ("y", "yes"):
            log.info(fmt(C.GRAY, "  Skipped."))
            return "skipped"

    try:
        result = repair_album_dir(
            album_dir, verified_truncated, artist_name, args, token)
    except RepairRecoveryRequired as exc:
        _report_repair_recovery(
            exc.recovery.backup,
            reason=exc.recovery.reason,
        )
        raise exc.cause
    if result and _report_repair_recovery(result.get("backup")):
        return "recovery"
    if (result
            and result.get("n_ok", 0) > 0
            and result.get("n_fail", 0) == 0
            and result.get("imported")
            and not result.get("repair_unverified")):
        return "repaired"
    return "failed"


def _all_library_album_dirs():
    """Every (artist_dir, album_dir) under MUSIC_ROOT, artist-sorted."""
    pairs = []
    for adir in list_library_artists():
        for aldir in list_artist_album_dirs(adir):
            pairs.append((adir, aldir))
    return pairs


def run_album_repair_mode(args, token, *, loop=False):
    """ISRC-anchored refill of truncated FLACs, replaces are matched on
    ISRC so the new file is the same recording as the old one.

    `'*'` at the artist prompt sweeps the whole library. --no-upgrade is
    forced on so the upgrade path can't escalate to wipe-and-replace.
    """
    saved_no_upgrade = getattr(args, "no_upgrade", False)
    args.no_upgrade = True  # repair = surgical; never silently upgrade the rest

    try:
        while True:
            clear_scan_caches()
            try:
                artist_dir, album_dir = _prompt_library_album_for_repair(
                    args, token)
            except AuthLost:
                die(fmt(C.RED, _REPAIR_AUTH_LOST), EXIT_AUTH)

            if artist_dir == "__ALL__":
                targets = _all_library_album_dirs()
                if not targets:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  No album folders found under the music root."))
                    if not loop:
                        return 0
                    continue
                section(f"Repair scan, whole library "
                        f"({len(targets)} album(s))")
                log.info(fmt(C.GRAY,
                    "  Albums with no damaged tracks are skipped silently. "
                    "Ctrl-C to stop."))
                tally = {
                    "repaired": 0,
                    "clean": 0,
                    "attention": 0,
                    "skipped": 0,
                    "failed": 0,
                    "recovery": 0,
                }
                interrupted = False
                try:
                    for i, (adir, aldir) in enumerate(targets, 1):
                        _line = (f"  [{i}/{len(targets)}] Scanning "
                                 f"{truncate(adir.name, 28)} - "
                                 f"{truncate(aldir.name, 30)}…")
                        if sys.stdout.isatty():
                            print(f"\r{_line:<90}", end="", flush=True)
                        else:
                            vlog(_line.strip())
                        try:
                            status = _scan_report_repair(
                                aldir, adir.name, args, token,
                                deep=True, quiet=True)
                        except AuthLost:
                            print()
                            die(fmt(C.RED, _REPAIR_AUTH_LOST), EXIT_AUTH)
                        except QobuzUnavailable:
                            # Finish the progress line before the transient
                            # abort propagates to the clean EXIT_TRANSIENT stop.
                            print()
                            raise
                        tally[status] = tally.get(status, 0) + 1
                except KeyboardInterrupt:
                    interrupted = True
                    print()
                    log.info(fmt(C.YELLOW,
                        "  Interrupted, stopping library repair sweep."))
                else:
                    if sys.stdout.isatty():
                        print(f"\r{' ' * 90}\r", end="", flush=True)
                section("Library repair summary")
                _summary = (f"  repaired: {tally['repaired']}  ·  "
                            f"clean: {tally['clean']}  ·  "
                            f"skipped: {tally['skipped']}")
                if tally["attention"]:
                    _summary += f"  ·  needs attention: {tally['attention']}"
                if tally["failed"]:
                    _summary += f"  ·  failed: {tally['failed']}"
                if tally["recovery"]:
                    _summary += f"  ·  recovery needed: {tally['recovery']}"
                needs_attention = (
                    interrupted
                    or any(tally[key] for key in (
                        "attention", "failed", "recovery"
                    ))
                )
                (log.warning if needs_attention else log.info)(
                    fmt(C.YELLOW if needs_attention else C.GRAY, _summary)
                )
                if not loop:
                    return EXIT_GENERAL if needs_attention else 0
                continue

            if album_dir is None:
                return 0

            try:
                status = _scan_report_repair(
                    album_dir, artist_dir.name, args, token
                )
            except AuthLost:
                die(fmt(C.RED, _REPAIR_AUTH_LOST), EXIT_AUTH)

            if not loop:
                needs_attention = status in {"attention", "failed", "recovery"}
                if needs_attention:
                    log.warning(fmt(
                        C.YELLOW,
                        "  ⚠  Repair needs attention; review the result "
                        "above before retrying.",
                    ))
                return EXIT_GENERAL if needs_attention else 0
    finally:
        args.no_upgrade = saved_no_upgrade
