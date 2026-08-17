"""beets import integration and staging pre-flight.

``beets_import_paths`` resolves the Python environment that owns Beets and
launches it through this package's isolated bridge.

Two non-obvious behaviours kept here:

- The on-disk override yaml is removed on every exit path (success,
  failure, KeyboardInterrupt). Otherwise a leftover override could
  silently affect a manual ``beet`` invocation later.
- ``clear_scan_caches()`` runs after import so post-import path lookups
  see the file move beets just performed (the in-memory listing cache
  would otherwise still point at the empty staging dir).
"""

import codecs
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import secrets
import select
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    ManagedImportEvidence,
    ManagedMapping,
    RecoveryOwner,
)
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.integrations import staging as staging_mod
from qobuz_librarian.interrupts import run_sigint_deferred
from qobuz_librarian.library import tags as tags_mod
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.library.sqlite_atomic import (
    AtomicSQLiteWrite,
    _SQLiteDatabaseExclusion,
    inspect_sqlite_source,
)
from qobuz_librarian.recovery import (
    decode_recovery_json,
    normalise_recovery_owner,
)
from qobuz_librarian.ui_cli import errors
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.errors import EXIT_GENERAL, die
from qobuz_librarian.ui_cli.logging import (
    log,
    report_progress,
    vlog,
    wrap_thread_target,
)

_SYSTEM_POPEN = subprocess.Popen
_MANAGED_CARRIER_RETIREMENT_LOCK = threading.RLock()

try:
    from mutagen.flac import FLAC as _MutagenFLAC  # noqa: F401

    HAVE_MUTAGEN = True
except Exception:
    HAVE_MUTAGEN = False


def _yaml_sq(value):
    """Emit *value* as a safe YAML single-quoted scalar.

    Single-quoted style is the simplest YAML scalar with no backslash
    escaping. The only metacharacter is the single quote itself, doubled
    to escape. Newlines aren't valid in these values; collapse to spaces
    so a stray one can't fold into a second YAML line.
    """
    s = str(value).replace("\r", " ").replace("\n", " ")
    return "'" + s.replace("'", "''") + "'"


def _merge_relative_parts(root, value):
    root = Path(os.path.abspath(os.fspath(root)))
    value = Path(os.path.abspath(os.fspath(value)))
    try:
        relative = value.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        return None
    return tuple(relative.parts)


def _merge_directory_matches(directory_fd, parent_fd, name, expected=None):
    try:
        held = os.fstat(directory_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode):
        return False
    held_identity = _ownership_identity(held)
    if held_identity != _ownership_identity(named):
        return False
    return expected is None or (
        stat.S_ISDIR(expected.st_mode) and held_identity == _ownership_identity(expected)
    )


def _open_exact_merge_dir(parent_fd, name, expected=None):
    child = _open_ownership_dir(name, dir_fd=parent_fd)
    if _merge_directory_matches(child, parent_fd, name, expected):
        return child
    os.close(child)
    raise OSError("split-folder directory changed while it was opened")


def _merge_name_missing(parent_fd, name):
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _merge_name_matches(parent_fd, name, held_fd, *, regular=False):
    try:
        held = os.fstat(held_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (not regular or stat.S_ISREG(held.st_mode)) and _ownership_identity(
        held
    ) == _ownership_identity(named)


def _merge_entry_location(parent_fd, name):
    try:
        parent = os.readlink(f"/proc/self/fd/{parent_fd}")
    except OSError:
        return str(name)
    return os.path.join(parent, os.fsdecode(name))


def _report_preserved_merge_entry(parent_fd, name, *, restored, durable):
    location = _merge_entry_location(parent_fd, name)
    if restored:
        detail = (
            "restored a late replacement but could not confirm its durability at"
            if not durable
            else "restored a late replacement at"
        )
    else:
        detail = (
            "preserved a late replacement for manual recovery at"
            if durable
            else "left a late replacement in uncertain recovery at"
        )
    log.info(fmt(C.YELLOW, f"  ⚠  merge: {detail} {location}."))


def _restore_or_preserve_unexpected_merge_entry(parent_fd, public_name, private_name):
    """Never silently hide a late cooperative-writer entry in quarantine."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        _report_preserved_merge_entry(
            parent_fd, private_name, restored=False, durable=False
        )
        return False
    descriptor = None

    def layout():
        public_exact = descriptor is not None and _merge_name_matches(
            parent_fd, public_name, descriptor
        )
        private_exact = descriptor is not None and _merge_name_matches(
            parent_fd, private_name, descriptor
        )
        if public_exact and _merge_name_missing(parent_fd, private_name):
            return "public"
        if private_exact:
            return "private"
        return "changed"

    def seal():
        try:
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return True
        except OSError:
            return False

    try:
        descriptor = os.open(
            private_name,
            os.O_RDONLY
            | os.O_NONBLOCK
            | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        held = os.fstat(descriptor)
        if not (stat.S_ISREG(held.st_mode) or stat.S_ISDIR(held.st_mode)) or not (
            _merge_name_matches(parent_fd, private_name, descriptor)
        ):
            raise OSError("late split-folder entry changed while it was captured")
    except OSError:
        durable = False
        try:
            os.fsync(parent_fd)
            durable = True
        except OSError:
            pass
        _report_preserved_merge_entry(
            parent_fd, private_name, restored=False, durable=durable
        )
        if descriptor is not None:
            os.close(descriptor)
        return False

    try:
        try:
            _rename_ownership_noreplace(
                parent_fd, private_name, parent_fd, public_name
            )
            if layout() != "public":
                raise OSError("late split-folder entry changed while it was restored")
            if not seal():
                _report_preserved_merge_entry(
                    parent_fd, public_name, restored=True, durable=False
                )
                return False
            return True
        except BaseException as exc:
            state = layout()
            if state == "private":
                durable = seal()
                _report_preserved_merge_entry(
                    parent_fd, private_name, restored=False, durable=durable
                )
            elif state == "public":
                durable = seal()
                if not durable:
                    _report_preserved_merge_entry(
                        parent_fd, public_name, restored=True, durable=False
                    )
            else:
                _report_preserved_merge_entry(
                    parent_fd, private_name, restored=False, durable=False
                )
            if isinstance(exc, OSError):
                return False
            if state == "public":
                exc.add_note("the late split-folder entry was restored before interruption")
            elif state == "private":
                exc.add_note(
                    "the late split-folder entry remains at the reported recovery path"
                )
            else:
                exc.add_note("the late split-folder entry recovery state is uncertain")
            raise
    finally:
        os.close(descriptor)


def _remove_exact_empty_merge_dir(parent_fd, name, directory_fd):
    """Privately quarantine and remove only the exact held empty directory."""
    quarantine = f".qobuz-merge-cleanup-{secrets.token_hex(16)}"

    def layout():
        public_exact = _merge_directory_matches(directory_fd, parent_fd, name)
        private_exact = _merge_directory_matches(directory_fd, parent_fd, quarantine)
        if public_exact and _merge_name_missing(parent_fd, quarantine):
            return "public"
        if private_exact and _merge_name_missing(parent_fd, name):
            return "private"
        if _merge_name_missing(parent_fd, name) and _merge_name_missing(parent_fd, quarantine):
            try:
                if os.fstat(directory_fd).st_nlink == 0:
                    return "removed"
            except OSError:
                pass
        return "changed"

    def restore_private():
        state = layout()
        if state == "public":
            return True
        if state != "private":
            return False
        try:
            _rename_ownership_noreplace(parent_fd, quarantine, parent_fd, name)
            os.fsync(parent_fd)
            return layout() == "public"
        except BaseException as exc:
            restored = layout() == "public"
            if isinstance(exc, OSError):
                return restored
            if not restored:
                exc.add_note(
                    "the interrupted empty-directory cleanup left the exact "
                    "directory in private recovery state"
                )
            raise

    try:
        if not _merge_directory_matches(directory_fd, parent_fd, name) or os.listdir(directory_fd):
            return False
        _rename_ownership_noreplace(parent_fd, name, parent_fd, quarantine)
        if not _merge_directory_matches(directory_fd, parent_fd, quarantine):
            if not _merge_name_missing(parent_fd, quarantine):
                _restore_or_preserve_unexpected_merge_entry(
                    parent_fd, name, quarantine
                )
            raise OSError("split-folder directory changed during cleanup")
        if os.listdir(directory_fd):
            raise OSError("split-folder directory changed during cleanup")
        os.rmdir(quarantine, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    except BaseException as exc:
        state = layout()
        if state == "private":
            restored = restore_private()
            state = "public" if restored else layout()
        if isinstance(exc, OSError):
            return False
        if state not in {"public", "removed"}:
            exc.add_note(
                "the interrupted empty-directory cleanup preserved an "
                "uncertain private recovery state"
            )
        raise


def _create_exact_merge_dir(parent_fd, name):
    """Publish a held directory without adopting a concurrently-created one."""
    temporary = f".qobuz-merge-{secrets.token_hex(8)}"
    child = None
    try:
        os.mkdir(temporary, 0o755, dir_fd=parent_fd)
        child = _open_exact_merge_dir(parent_fd, temporary)
        _rename_ownership_noreplace(parent_fd, temporary, parent_fd, name)
        if not _merge_directory_matches(child, parent_fd, name):
            raise OSError("split-folder directory changed while it was published")
        os.fsync(child)
        os.fsync(parent_fd)
        return child
    except BaseException:
        if child is None:
            try:
                child = _open_exact_merge_dir(parent_fd, temporary)
            except OSError:
                child = None
        if child is not None:
            if _merge_directory_matches(child, parent_fd, name):
                cleanup_name = name
            elif _merge_directory_matches(child, parent_fd, temporary):
                cleanup_name = temporary
            else:
                cleanup_name = None
            if cleanup_name is not None:
                _remove_exact_empty_merge_dir(parent_fd, cleanup_name, child)
            os.close(child)
        raise


def _open_merge_chain(root_fd, parts, *, create=False, required_device=None):
    """Open one root-relative directory chain without following symlinks."""
    chain = [os.dup(root_fd)]
    created = [False]
    try:
        for part in parts:
            if required_device is not None and int(os.fstat(chain[-1]).st_dev) != int(
                required_device
            ):
                raise OSError(errno.EXDEV, "directory chain crosses filesystems")
            was_created = False
            try:
                child = _open_exact_merge_dir(chain[-1], part)
            except FileNotFoundError:
                if not create:
                    raise
                child = _create_exact_merge_dir(chain[-1], part)
                was_created = True
            if required_device is not None and int(os.fstat(child).st_dev) != int(required_device):
                os.close(child)
                raise OSError(errno.EXDEV, "directory chain crosses filesystems")
            chain.append(child)
            created.append(was_created)
        return chain, created
    except BaseException:
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _merge_chain_is_named(chain, names):
    if len(chain) != len(names) + 1:
        return False
    try:
        for index, name in enumerate(names):
            held = os.fstat(chain[index + 1])
            named = os.stat(name, dir_fd=chain[index], follow_symlinks=False)
            if (
                not stat.S_ISDIR(held.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (int(held.st_dev), int(held.st_ino)) != (int(named.st_dev), int(named.st_ino))
            ):
                return False
    except OSError:
        return False
    return True


def _merge_absolute_parts(value):
    raw = os.fspath(value)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise OSError("invalid absolute path")
    absolute = Path(os.path.abspath(raw))
    try:
        relative = absolute.relative_to(Path(os.path.sep))
    except ValueError:
        raise OSError("path has no stable filesystem anchor") from None
    return absolute, tuple(relative.parts)


def _open_absolute_merge_chain(value, *, create=False):
    """Hold every no-follow component from `/` through one directory."""
    _absolute, names = _merge_absolute_parts(value)
    anchor = _open_ownership_dir(os.path.sep)
    try:
        chain, _created = _open_merge_chain(anchor, names, create=create)
    finally:
        os.close(anchor)
    if not _merge_chain_is_named(chain, names):
        for descriptor in reversed(chain):
            os.close(descriptor)
        raise OSError("absolute directory chain changed while it was opened")
    return chain, names


def _open_beets_database_anchor():
    """Bind the configured database parent chain and exact public leaf."""
    configured = getattr(cfg, "BEETS_DB_PATH", None)
    if not configured:
        return None
    database, parts = _merge_absolute_parts(configured)
    if not parts:
        raise OSError("invalid beets database path")
    parent_chain, parent_names = _open_absolute_merge_chain(database.parent)
    descriptor = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe beets database access is unavailable")
        try:
            descriptor = os.open(
                database.name,
                os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_chain[-1],
            )
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None and not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("beets database is not a regular file")
        anchor = {
            "path": database,
            "parent_chain": parent_chain,
            "parent_names": parent_names,
            "name": database.name,
            "descriptor": descriptor,
        }
        if not _beets_database_anchor_matches(anchor):
            raise OSError("configured beets database changed while it was opened")
        return anchor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        for directory_fd in reversed(parent_chain):
            os.close(directory_fd)
        raise


def _beets_database_anchor_matches(anchor):
    if anchor is None:
        return True
    try:
        configured = getattr(cfg, "BEETS_DB_PATH", None)
        if not configured:
            return False
        current, _parts = _merge_absolute_parts(configured)
        if current != anchor["path"] or not _merge_chain_is_named(
            anchor["parent_chain"], anchor["parent_names"]
        ):
            return False
        descriptor = anchor["descriptor"]
        if descriptor is None:
            return _merge_name_missing(anchor["parent_chain"][-1], anchor["name"])
        return _merge_name_matches(
            anchor["parent_chain"][-1],
            anchor["name"],
            descriptor,
            regular=True,
        )
    except (OSError, TypeError, ValueError):
        return False


def _close_beets_database_anchor(anchor):
    if anchor is None:
        return
    descriptor = anchor.get("descriptor")
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            pass
    for directory_fd in reversed(anchor.get("parent_chain", ())):
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _bootstrap_beets_database_anchor(anchor):
    """Create the configured database once without adopting a racing file."""
    if anchor is None or anchor["descriptor"] is not None:
        return
    if not _beets_database_anchor_matches(anchor):
        raise OSError("configured beets database changed before creation")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe beets database creation is unavailable")
    parent_fd = anchor["parent_chain"][-1]
    temporary = f".qobuz-beets-bootstrap-{secrets.token_hex(16)}"
    descriptor = None
    connection = None
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, 0o600)
        if not _merge_name_matches(parent_fd, temporary, descriptor, regular=True) or not (
            _beets_database_anchor_matches(anchor)
        ):
            raise OSError("configured beets database changed during creation")
        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=rw&vfs=unix",
            uri=True,
            timeout=timeout,
        )
        journal_mode = connection.execute("PRAGMA journal_mode=MEMORY").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "memory":
            raise sqlite3.DatabaseError("could not initialise the beets database safely")
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()
        connection = None
        os.fsync(descriptor)
        if not _merge_name_matches(parent_fd, temporary, descriptor, regular=True) or not (
            _beets_database_anchor_matches(anchor)
        ):
            raise OSError("configured beets database changed during creation")

        try:
            _rename_ownership_noreplace(parent_fd, temporary, parent_fd, anchor["name"])
        except FileExistsError as exc:
            raise OSError("configured beets database appeared before creation") from exc
        anchor["descriptor"] = descriptor
        os.fsync(parent_fd)
        if not _beets_database_anchor_matches(anchor):
            raise OSError("configured beets database changed after creation")
    except BaseException:
        if descriptor is not None and anchor["descriptor"] is None:
            if _merge_name_matches(parent_fd, anchor["name"], descriptor, regular=True):
                anchor["descriptor"] = descriptor
            elif _merge_name_matches(parent_fd, temporary, descriptor, regular=True):
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except OSError:
                    pass
            if anchor["descriptor"] is None:
                os.close(descriptor)
        raise
    finally:
        if connection is not None:
            connection.close()


def _require_no_items_triggers(connection):
    trigger = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name = 'items' COLLATE NOCASE LIMIT 1"
    ).fetchone()
    if trigger is not None:
        raise sqlite3.DatabaseError("custom items triggers make path updates unverifiable")


def _require_delete_journal_mode(connection):
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
        raise sqlite3.DatabaseError("safe path updates require SQLite delete-journal mode")




def _beets_database_connection_path(anchor):
    """Keep SQLite recovery beside the exact canonical public database."""
    return f"/proc/self/fd/{anchor['parent_chain'][-1]}/{anchor['name']}"


def _preflight_beets_database_anchor(anchor, anchors_match=None):
    """Reject an unsafe DB before the first associated filesystem mutation."""
    if anchor is None:
        return
    if not _beets_database_anchor_matches(anchor):
        raise OSError("configured beets database changed before the move")
    if anchor["descriptor"] is None:
        raise OSError("configured beets database does not exist")
    try:
        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        inspect_sqlite_source(
            anchor,
            _beets_database_anchor_matches,
            lambda connection: (
                _require_no_items_triggers(connection),
                _require_delete_journal_mode(connection),
            ),
            connect=sqlite3.connect,
            timeout=timeout,
            anchors_match=anchors_match,
        )
    except sqlite3.Error as exc:
        raise OSError(f"beets database preflight failed: {exc}") from exc


def _merge_relative_directory_matches(root_fd, parts, expected_fd):
    """Prove one held directory is still named by an exact root-relative chain."""
    chain = []
    try:
        chain, _ = _open_merge_chain(root_fd, parts)
        return _merge_chain_is_named(chain, parts) and _ownership_identity(
            os.fstat(chain[-1])
        ) == _ownership_identity(os.fstat(expected_fd))
    except OSError:
        return False
    finally:
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass




def _rollback_exact_merge_entry(source_fd, source_name, destination_fd, destination_name, held_fd):
    """Restore one exact moved entry without overwriting either name."""

    def layout():
        source_exact = _merge_name_matches(source_fd, source_name, held_fd, regular=True)
        destination_exact = _merge_name_matches(
            destination_fd, destination_name, held_fd, regular=True
        )
        if source_exact and _merge_name_missing(destination_fd, destination_name):
            return "source"
        if destination_exact and _merge_name_missing(source_fd, source_name):
            return "destination"
        return "changed"

    state = layout()
    if state == "source":
        return True
    if state != "destination":
        return False
    try:
        _rename_ownership_noreplace(destination_fd, destination_name, source_fd, source_name)
        if layout() != "source":
            return False
        os.fsync(destination_fd)
        os.fsync(source_fd)
        return True
    except BaseException as exc:
        restored = layout() == "source"
        if isinstance(exc, OSError):
            return restored
        if restored:
            exc.add_note("the exact merge entry was restored before the interruption")
        else:
            exc.add_note(
                "the interrupted rollback preserved the exact entry at its last proved name"
            )
        raise




def _hold_merge_directory(records, parent_fd, name, directory_fd, parent_parts):
    parent_copy = directory_copy = None
    try:
        parent_copy = os.dup(parent_fd)
        directory_copy = os.dup(directory_fd)
        records.append(
            {
                "parent_fd": parent_copy,
                "directory_fd": directory_copy,
                "name": name,
                "parent_parts": tuple(parent_parts),
            }
        )
    except OSError:
        for descriptor in (directory_copy, parent_copy):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def _remove_held_merge_directories(root_fd, records):
    for record in sorted(records, key=lambda value: len(value["parent_parts"]), reverse=True):
        if _merge_relative_directory_matches(root_fd, record["parent_parts"], record["parent_fd"]):
            _remove_exact_empty_merge_dir(
                record["parent_fd"],
                record["name"],
                record["directory_fd"],
            )


def _close_held_merge_directories(records):
    for record in records:
        for key in ("directory_fd", "parent_fd"):
            try:
                os.close(record[key])
            except OSError:
                pass
    records.clear()




def _under_retry_dir(p):
    """True when p sits under STAGING_DIR/<BEETS_RETRY_DIR>/. Used to skip
    the parked-album quarantine when sweeping or counting staging."""
    try:
        rel = p.relative_to(cfg.STAGING_DIR)
    except ValueError:
        return False
    return rel.parts and rel.parts[0] == cfg.BEETS_RETRY_DIR


def _is_isolated_run_name(name):
    prefix = ".qobuz-run-"
    nonce = name[len(prefix) :] if name.startswith(prefix) else ""
    return len(nonce) == 24 and all(char in "0123456789abcdef" for char in nonce)


def _under_isolated_run(p):
    """True for a per-rip hidden root that bulk staging work must ignore."""
    try:
        first = p.relative_to(cfg.STAGING_DIR).parts[0]
    except (IndexError, ValueError):
        return False
    return _is_isolated_run_name(first)


def _beets_progress_album(line, staging_root):
    stripped = line.strip()
    prefix = os.fspath(staging_root).rstrip("/") + "/"
    if not stripped.startswith(prefix):
        return None
    parts = stripped[len(prefix) :].split("/")
    if parts and _is_isolated_run_name(parts[0]):
        parts = parts[1:]
    album = parts[0].split(" (", 1)[0].strip() if parts else ""
    if not album or album in (".", "..") or _is_isolated_run_name(album):
        return "Importing album"
    return album


def _prepare_staging_tags(
    roots=None,
    *,
    managed_bindings=None,
    allow_managed_rewrite=False,
    authority_check=None,
):
    """Set aside untagged FLACs and clean the survivors' path tags in a
    single pass, so each file is opened by mutagen once rather than twice.

    Unreadable or untagged files move by exact identity to private staging
    recovery, never to an inferred public pathname. The rest get whitespace
    and quotes trimmed from the tags Beets uses to build library paths.
    Returns the list of original paths that were set aside.

    Without mutagen every read fails; rather than quarantine the whole download
    as "untagged" and hand beets an empty dir, leave the files untouched.

    ``roots`` scopes the scan to those directories; None scans the whole
    STAGING_DIR for the preflight-driven full-batch import.
    """
    moved = []
    if allow_managed_rewrite and managed_bindings is None:
        raise ValueError("managed tag rewrites require exact bindings")
    if authority_check is not None and not callable(authority_check):
        raise ValueError("managed tag rewrite authority check must be callable")
    if authority_check is not None:
        authority_check()
    if not HAVE_MUTAGEN:
        if managed_bindings is not None:
            return tuple(dict(record) for record in managed_bindings)
        return moved
    from mutagen.flac import FLAC

    managed_by_path = None
    refreshed = []
    if managed_bindings is not None:
        managed_by_path = {record["path"]: record for record in managed_bindings}
        if len(managed_by_path) != len(managed_bindings):
            raise ValueError("managed beets bindings are not unique")
    scan_roots = roots if roots else [cfg.STAGING_DIR]
    try:
        flacs = []
        for r in scan_roots:
            flacs.extend(r.rglob("*.flac"))
        # Parked albums waiting on a manual retry shouldn't be re-quarantined
        # or re-tag-cleaned on every batch.
        flacs = [f for f in flacs if not _under_retry_dir(f)]
    except OSError:
        return moved
    uncertain_quarantines = 0
    for f in flacs:
        if authority_check is not None:
            authority_check()
        managed = managed_by_path.get(str(Path(f))) if managed_by_path is not None else None
        if managed_by_path is not None and managed is None:
            raise OSError("managed beets preflight found unbound audio")
        receipt = staging_mod.capture_file(
            f,
            expected=(tuple(managed["identity"]) if managed is not None else None),
        )
        if receipt is None:
            if managed is not None:
                raise OSError("managed beets source changed during preflight")
            continue
        inspection_chain = []
        inspection_fd = None
        try:
            if managed is None:
                tags = FLAC(str(f))
            else:
                inspection_chain, inspection_names = _open_absolute_merge_chain(Path(f).parent)
                nofollow = getattr(os, "O_NOFOLLOW", None)
                if nofollow is None:
                    raise OSError("safe staged-file inspection is unavailable")
                inspection_fd = os.open(
                    Path(f).name,
                    os.O_RDONLY
                    | nofollow
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=inspection_chain[-1],
                )
                if _staged_identity(os.fstat(inspection_fd)) != receipt.identity:
                    raise OSError("managed beets source changed before inspection")
                tags = FLAC(f"/proc/self/fd/{inspection_fd}")
                if (
                    _staged_identity(os.fstat(inspection_fd)) != receipt.identity
                    or not _merge_chain_is_named(inspection_chain, inspection_names)
                    or not _merge_name_matches(
                        inspection_chain[-1],
                        Path(f).name,
                        inspection_fd,
                        regular=True,
                    )
                ):
                    raise OSError("managed beets source changed during inspection")
        except Exception as exc:
            if managed is not None:
                raise OSError("managed beets source failed tag inspection") from exc
            tags = None
        finally:
            if inspection_fd is not None:
                os.close(inspection_fd)
            for descriptor in reversed(inspection_chain):
                os.close(descriptor)
        aa = al = ""
        if tags is not None:
            aa = (tags.get("albumartist") or tags.get("artist") or [""])[0].strip()
            al = (tags.get("album") or [""])[0].strip()
        if tags is None or not aa or not al:
            if managed is not None:
                raise OSError("managed beets source failed tag preflight")
            try:
                dest = staging_mod.quarantine_file(receipt, ".untagged")
                if dest is not None:
                    moved.append(f)
                    if not dest.durable:
                        uncertain_quarantines += 1
                else:
                    vlog(f"couldn't safely quarantine {f.name}: it changed")
            except OSError as e:
                vlog(f"couldn't quarantine {f.name}: {e}")
            continue
        changed = False
        for key in ("album", "albumartist", "artist", "title"):
            vals = tags.get(key)
            if not vals:
                continue
            cleaned = [tags_mod.clean_qobuz_string(v) for v in vals]
            if cleaned != list(vals):
                tags[key] = cleaned
                changed = True
        if changed and managed is not None and not allow_managed_rewrite:
            raise OSError("managed beets source requires a tag-clean rewrite")
        if changed:
            # Mutagen may shift audio frames when a larger comment block no
            # longer fits its padding. Use the shared durable replacement so
            # the staged copy is flushed before the import can move it.
            expected_identity = {
                "device": receipt.identity[0],
                "inode": receipt.identity[1],
                "size": receipt.identity[3],
                "modified_ns": receipt.identity[4],
                "changed_ns": receipt.identity[5],
            }
            try:

                def authorised_commit():
                    if authority_check is not None:
                        authority_check()
                    return True

                if authority_check is not None:
                    authority_check()
                lyric_fetch.save_flac_tags(
                    tags,
                    f,
                    expected_identity=expected_identity,
                    commit_guard=authorised_commit if managed is not None else None,
                )
                if authority_check is not None:
                    authority_check()
            except Exception as e:
                if managed is not None:
                    raise OSError("managed beets tag-clean rewrite failed") from e
                vlog(f"couldn't rewrite cleaned tags on {f.name}: {e}")
        if managed is not None:
            refreshed_receipt = staging_mod.capture_file(
                f,
                expected=None if changed else receipt.identity,
            )
            if refreshed_receipt is None:
                raise OSError("managed beets source changed during preflight")
            refreshed.append(
                {
                    "slot": managed["slot"],
                    "path": str(refreshed_receipt.path),
                    "identity": list(refreshed_receipt.identity),
                }
            )
    if moved:
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  Set aside {len(moved)} untagged file(s) in private "
                "staging recovery.\n"
                "     (each file has its own recovery record; cancelled-rip "
                "leftovers and files needing a manual retag were not deleted).",
            )
        )
        if uncertain_quarantines:
            log.info(
                fmt(
                    C.YELLOW,
                    f"  ⚠  Directory sync could not be confirmed for "
                    f"{uncertain_quarantines} recovery file(s); their current "
                    "private locations remain recorded.",
                )
            )
    if managed_by_path is not None:
        for record in managed_by_path.values():
            if Path(record["path"]).suffix.lower() == ".flac":
                continue
            receipt = staging_mod.capture_file(record["path"], expected=tuple(record["identity"]))
            if receipt is None:
                raise OSError("managed beets source changed during preflight")
            refreshed.append(
                {
                    "slot": record["slot"],
                    "path": str(receipt.path),
                    "identity": list(receipt.identity),
                }
            )
        if len(refreshed) != len(managed_by_path):
            raise OSError("managed beets preflight did not bind every source")
        # The scan walks the staging tree in directory order; the caller
        # compares this against the catalogue-ordered bindings, so return the
        # records in the order they were handed in rather than readdir order.
        order = {record["slot"]: index
                 for index, record in enumerate(managed_bindings)}
        refreshed.sort(key=lambda record: order[record["slot"]])
        return tuple(refreshed)
    return moved


def prepare_managed_staging_tags(
    roots,
    bindings,
    *,
    authority_check,
):
    """Clean exact staged path tags before managed Beets carrier creation."""
    if not callable(authority_check):
        raise ValueError("managed tag rewrite requires an authority check")
    return _prepare_staging_tags(
        roots,
        managed_bindings=bindings,
        allow_managed_rewrite=True,
        authority_check=authority_check,
    )


_OWNERSHIP_PLUGIN = "qobuz_ownership"
_ART_GUARD_PLUGIN = "qobuz_art_guard"
_OWNERSHIP_FD_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_FD"
_OWNERSHIP_NONCE_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_NONCE"
_OWNERSHIP_SOURCE_ROOTS_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_SOURCE_ROOTS"
_OWNERSHIP_MODE_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_MODE"
_OWNERSHIP_ENV_KEYS = (
    _OWNERSHIP_FD_ENV,
    _OWNERSHIP_NONCE_ENV,
    _OWNERSHIP_SOURCE_ROOTS_ENV,
    _OWNERSHIP_MODE_ENV,
)
_MANAGED_OWNERSHIP_MODE = "managed-v3"
_MANAGED_SNAPSHOT_VERSION = 3
_LEGACY_MANAGED_SNAPSHOT_VERSION = 2
_OWNERSHIP_MAX_SOURCE_ROOTS = 16
_OWNERSHIP_MAX_BYTES = 1024 * 1024
_OWNERSHIP_MARKER = ".qobuz-librarian-owner"
_OWNERSHIP_IDENTITY_FIELDS = (
    "device",
    "inode",
    "size",
    "modified_ns",
    "changed_ns",
)
_RENAME_NOREPLACE = 1


@dataclass(frozen=True)
class ManagedBeetsImportResult:
    kind: str
    carrier: dict | None
    evidence: dict | None = None
    spawned: bool = False
    reservation: dict | None = None


def _ownership_identity(value):
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
    }


def _valid_ownership_identity(value):
    return isinstance(value, dict) and all(
        type(value.get(field)) is int for field in _OWNERSHIP_IDENTITY_FIELDS
    )


def _staged_identity(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _valid_staged_identity(value):
    return (
        isinstance(value, list)
        and len(value) == 6
        and all(type(part) is int and part >= 0 for part in value)
    )


def _valid_managed_ownership_identity(value):
    return (
        type(value) is dict
        and set(value) == set(_OWNERSHIP_IDENTITY_FIELDS)
        and all(
            type(value[field]) is int and value[field] >= 0 for field in _OWNERSHIP_IDENTITY_FIELDS
        )
    )


def _managed_payload_hash(payload):
    unsigned = dict(payload)
    unsigned.pop("hash", None)
    encoded = json.dumps(
        unsigned,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _valid_managed_binding(value):
    return (
        type(value) is dict
        and set(value) == {"slot", "path", "identity"}
        and isinstance(value.get("slot"), str)
        and 0 < len(value["slot"]) <= 256
        and "\x00" not in value["slot"]
        and isinstance(value.get("path"), str)
        and bool(value["path"])
        and "\x00" not in value["path"]
        and os.path.isabs(value["path"])
        and _valid_staged_identity(value.get("identity"))
    )


def _normalise_managed_roots(album_dirs):
    try:
        values = tuple(album_dirs)
    except Exception:
        return None
    if not 0 < len(values) <= _OWNERSHIP_MAX_SOURCE_ROOTS:
        return None
    roots = []
    for value in values:
        try:
            raw = os.fspath(value)
            if isinstance(raw, bytes):
                raw = os.fsdecode(raw)
        except (TypeError, UnicodeError, ValueError):
            return None
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            return None
        root = Path(os.path.abspath(raw))
        if root == root.parent:
            return None
        roots.append(root)
    for index, root in enumerate(roots):
        for other in roots[:index]:
            if root == other or root in other.parents or other in root.parents:
                return None
    return tuple(roots)


def _normalise_managed_bindings(bindings, roots):
    if not isinstance(bindings, (list, tuple)) or not bindings:
        raise ValueError("managed beets bindings are unavailable")
    records = []
    slots = set()
    paths = set()
    nodes = set()
    for value in bindings:
        if not _valid_managed_binding(value):
            raise ValueError("managed beets binding is malformed")
        frozen = tuple(value["identity"])
        receipt = staging_mod.capture_file(value["path"], expected=frozen)
        if receipt is None or receipt.identity != frozen:
            raise OSError("managed beets source changed")
        record = {
            "slot": value["slot"],
            "path": str(receipt.path),
            "identity": list(receipt.identity),
        }
        node = tuple(record["identity"][:2])
        if record["slot"] in slots or record["path"] in paths or node in nodes:
            raise ValueError("managed beets bindings are not unique")
        slots.add(record["slot"])
        paths.add(record["path"])
        nodes.add(node)
        records.append(record)

    audio = {}
    for root in roots:
        tree = staging_mod.capture_tree(root)
        if tree is None:
            raise OSError("managed beets source tree changed")
        for relative, identity in tree.files:
            path = tree.path / relative
            if path.suffix.lower() not in cfg.AUDIO_EXTS:
                continue
            absolute = str(path)
            if absolute in audio:
                raise ValueError("managed beets roots overlap")
            audio[absolute] = identity
    expected = {record["path"]: tuple(record["identity"]) for record in records}
    if audio != expected:
        raise OSError("managed beets sources are incomplete or unbound")
    return tuple(records)


def _valid_managed_cleanup_record(value):
    return (
        type(value) is dict
        and set(value)
        == {
            "relative",
            "temporary_relative",
            "directory",
            "marker",
        }
        and _ownership_relative_parts(value.get("relative")) is not None
        and _ownership_relative_parts(value.get("temporary_relative")) is not None
        and _valid_managed_ownership_identity(value.get("directory"))
        and (value.get("marker") is None or _valid_managed_ownership_identity(value.get("marker")))
    )


def _valid_managed_mapping(value):
    return (
        type(value) is dict
        and set(value) == {"slot", "source", "destination"}
        and isinstance(value.get("slot"), str)
        and _valid_managed_binding(
            {
                "slot": value.get("slot"),
                **(value.get("source") if type(value.get("source")) is dict else {}),
            }
        )
        and type(value.get("source")) is dict
        and set(value["source"]) == {"path", "identity"}
        and type(value.get("destination")) is dict
        and set(value["destination"]) == {"path", "identity"}
        and _ownership_relative_parts(value["destination"].get("path")) is not None
        and _valid_managed_ownership_identity(value["destination"].get("identity"))
    )


def _valid_managed_snapshot(value):
    fields = {
        "version",
        "generation",
        "previous_hash",
        "hash",
        "nonce",
        "owner",
        "root",
        "root_identity",
        "sealed",
        "intent",
        "mappings",
        "cleanup_directories",
    }
    try:
        owner = normalise_recovery_owner(value.get("owner"))
    except (AttributeError, ValueError):
        return False
    return (
        type(value) is dict
        and set(value) == fields
        and value.get("version")
        in {
            _LEGACY_MANAGED_SNAPSHOT_VERSION,
            _MANAGED_SNAPSHOT_VERSION,
        }
        and type(value.get("version")) is int
        and type(value.get("generation")) is int
        and value["generation"] >= 0
        and (
            value.get("previous_hash") is None
            or (
                isinstance(value.get("previous_hash"), str)
                and len(value["previous_hash"]) == 64
                and all(char in "0123456789abcdef" for char in value["previous_hash"])
            )
        )
        and isinstance(value.get("hash"), str)
        and len(value["hash"]) == 64
        and all(char in "0123456789abcdef" for char in value["hash"])
        and value["hash"] == _managed_payload_hash(value)
        and isinstance(value.get("nonce"), str)
        and len(value["nonce"]) == 64
        and all(char in "0123456789abcdef" for char in value["nonce"])
        and owner is not None
        and value["owner"] == owner
        and isinstance(value.get("root"), str)
        and "\x00" not in value["root"]
        and os.path.isabs(value["root"])
        and os.path.abspath(value["root"]) == value["root"]
        and _valid_managed_ownership_identity(value.get("root_identity"))
        and type(value.get("sealed")) is bool
        and isinstance(value.get("intent"), list)
        and bool(value["intent"])
        and all(_valid_managed_binding(item) for item in value["intent"])
        and isinstance(value.get("mappings"), list)
        and all(_valid_managed_mapping(item) for item in value["mappings"])
        and isinstance(value.get("cleanup_directories"), list)
        and all(_valid_managed_cleanup_record(item) for item in value["cleanup_directories"])
    )


def _ownership_relative_parts(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    raw = os.fsencode(value)
    if os.path.isabs(raw):
        return None
    parts = raw.split(os.fsencode(os.sep))
    if len(parts) > 32 or any(part in (b"", b".", b"..") or b"\x00" in part for part in parts):
        return None
    return parts


def _open_ownership_dir(path, *, dir_fd=None):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe directory access is unavailable")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, dir_fd=dir_fd)


def _rename_ownership_noreplace(source_fd, source, destination_fd, destination):
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


def _ownership_marker_token(nonce, relative):
    return hashlib.sha256(nonce.encode("ascii") + b"\0" + os.fsencode(relative)).hexdigest()


def _write_managed_snapshot(descriptor, payload):
    payload = dict(payload)
    payload["hash"] = _managed_payload_hash(payload)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    current_size = os.fstat(descriptor).st_size
    if current_size + len(encoded) > _OWNERSHIP_MAX_BYTES:
        raise OSError("managed beets evidence is too large")
    offset = 0
    while offset < len(encoded):
        written = os.pwrite(descriptor, encoded[offset:], current_size + offset)
        if written <= 0:
            raise OSError("managed beets evidence write failed")
        offset += written
    os.fsync(descriptor)
    return payload["hash"]


def _managed_carrier_reference(capture):
    return {
        "version": 2,
        "path": str(capture["path"]),
        "device": capture["device"],
        "inode": capture["inode"],
        "parent_device": capture["parent_device"],
        "parent_inode": capture["parent_inode"],
        "nonce": capture["nonce"],
        "owner": dict(capture["owner"]),
    }


def _managed_carrier_reservation(capture):
    return {
        "version": 1,
        "path": str(capture["path"]),
        "parent_device": capture["parent_device"],
        "parent_inode": capture["parent_inode"],
        "nonce": capture["nonce"],
        "owner": dict(capture["owner"]),
    }


def _prepare_managed_override(capture, plugin_config):
    """Create a sealed anonymous config that cannot survive this process."""
    required = (
        "memfd_create",
        "MFD_CLOEXEC",
        "MFD_ALLOW_SEALING",
    )
    if any(not hasattr(os, name) for name in required):
        raise OSError("anonymous managed Beets configuration is unavailable")
    seal_names = (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_GETFD",
        "FD_CLOEXEC",
        "F_SEAL_SEAL",
        "F_SEAL_SHRINK",
        "F_SEAL_GROW",
        "F_SEAL_WRITE",
    )
    if any(not hasattr(fcntl, name) for name in seal_names):
        raise OSError("sealed managed Beets configuration is unavailable")

    base_flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    descriptor = None
    try:
        try:
            descriptor = os.memfd_create(
                "qobuz-managed-beets-config",
                flags=base_flags | 0x0008,  # Linux MFD_NOEXEC_SEAL.
            )
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            descriptor = os.memfd_create(
                "qobuz-managed-beets-config",
                flags=base_flags,
            )
        if descriptor < 3:
            raise OSError("managed Beets configuration requires standard descriptors")
        payload = _build_import_override_yaml(
            plugin_config,
            ownership_enabled=True,
        ).encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.pwrite(descriptor, payload[offset:], offset)
            if written <= 0:
                raise OSError("managed Beets configuration write failed")
            offset += written
        os.fchmod(descriptor, 0o400)
        if os.pread(descriptor, len(payload) + 1, 0) != payload:
            raise OSError("managed Beets configuration changed while writing")
        seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        held = os.fstat(descriptor)
        actual_seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != 0o400
            or held.st_size != len(payload)
            or actual_seals & seals != seals
            or fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC != fcntl.FD_CLOEXEC
            or os.pread(descriptor, len(payload) + 1, 0) != payload
        ):
            raise OSError("managed Beets configuration could not be sealed")
        override_path = Path(f"/proc/self/fd/{descriptor}")
        capture["_override_fd"] = descriptor
        return override_path
    except BaseException:
        if capture.get("_override_fd") == descriptor:
            capture["_override_fd"] = None
        if descriptor is not None:
            os.close(descriptor)
        raise


def _managed_parent_namespace_matches(capture):
    """Prove the held carrier parent still has its complete absolute name."""
    try:
        chain = capture["_parent_chain"]
        names = capture["_parent_names"]
        if (
            not isinstance(chain, (list, tuple))
            or not chain
            or not isinstance(names, tuple)
            or len(chain) != len(names) + 1
        ):
            return False
        _parent, expected_names = _merge_absolute_parts(capture["parent"])
        parent_stat = os.fstat(chain[-1])
        return (
            names == expected_names
            and int(parent_stat.st_dev) == capture["parent_device"]
            and int(parent_stat.st_ino) == capture["parent_inode"]
            and _merge_chain_is_named(chain, names)
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def _managed_carrier_namespace_matches(capture):
    """Prove the held carrier still has its complete public absolute name."""
    try:
        carrier = Path(os.path.abspath(os.fspath(capture["path"])))
        manifest = capture["_file"]
        return (
            carrier.parent == Path(capture["parent"])
            and _managed_parent_namespace_matches(capture)
            and _merge_name_matches(
                capture["_parent_chain"][-1],
                carrier.name,
                manifest.fileno(),
                regular=True,
            )
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def _managed_root_namespace_matches(capture):
    """Prove the held library root still has its complete absolute name."""
    try:
        chain = capture["_root_chain"]
        names = capture["_root_names"]
        if (
            not isinstance(chain, (list, tuple))
            or not chain
            or not isinstance(names, tuple)
            or len(chain) != len(names) + 1
        ):
            return False
        root, expected_names = _merge_absolute_parts(capture["root"])
        held = os.fstat(chain[-1])
        return (
            root == Path(capture["root"])
            and names == expected_names
            and stat.S_ISDIR(held.st_mode)
            and int(held.st_dev) == capture["root_identity"]["device"]
            and int(held.st_ino) == capture["root_identity"]["inode"]
            and _merge_chain_is_named(chain, names)
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


def _managed_destination_matches(root, root_identity, destination):
    chain = []
    leaf_fd = None
    try:
        chain, root_names = _open_absolute_merge_chain(root)
        held_root = os.fstat(chain[-1])
        if (
            not stat.S_ISDIR(held_root.st_mode)
            or int(held_root.st_dev) != root_identity["device"]
            or int(held_root.st_ino) != root_identity["inode"]
        ):
            return False
        parts = _ownership_relative_parts(destination["path"])
        if not parts:
            return False
        for part in parts[:-1]:
            chain.append(_open_ownership_dir(part, dir_fd=chain[-1]))
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return False
        leaf_fd = os.open(
            parts[-1],
            os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=chain[-1],
        )
        held = os.fstat(leaf_fd)
        expected = destination["identity"]
        return (
            stat.S_ISREG(held.st_mode)
            and _ownership_identity(held) == expected
            and _merge_chain_is_named(chain, (*root_names, *parts[:-1]))
            and _merge_name_matches(chain[-1], parts[-1], leaf_fd, regular=True)
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if leaf_fd is not None:
            try:
                os.close(leaf_fd)
            except OSError:
                pass
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _managed_album_directory_identity(root, root_identity, relative):
    chain = []
    try:
        chain, root_names = _open_absolute_merge_chain(root)
        held_root = os.fstat(chain[-1])
        if (
            not stat.S_ISDIR(held_root.st_mode)
            or int(held_root.st_dev) != root_identity["device"]
            or int(held_root.st_ino) != root_identity["inode"]
        ):
            return None
        parts = _ownership_relative_parts(relative)
        if not parts:
            return None
        for part in parts:
            chain.append(_open_ownership_dir(part, dir_fd=chain[-1]))
        held = os.fstat(chain[-1])
        if not stat.S_ISDIR(held.st_mode) or not _merge_chain_is_named(
            chain, (*root_names, *parts)
        ):
            return None
        identity = _ownership_identity(held)
        return tuple(identity[field] for field in _OWNERSHIP_IDENTITY_FIELDS)
    except (OSError, TypeError, ValueError):
        return None
    finally:
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _managed_catalogue_album(connection, destinations, root):
    """Identify the one album with one row for every managed destination."""
    item_columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    album_columns = {row[1] for row in connection.execute("PRAGMA table_info(albums)")}
    if not {"path", "album_id"}.issubset(item_columns) or "id" not in album_columns:
        return None
    destinations = frozenset(destinations)
    if not destinations or any(
        type(destination) is not str or not destination
        for destination in destinations
    ):
        return None
    root_path = os.path.abspath(root)
    destination_counts = {}
    album_paths = {}
    for raw, album_id in connection.execute("SELECT path, album_id FROM items"):
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            return None
        absolute = os.path.abspath(
            raw if os.path.isabs(raw) else os.path.join(root_path, raw)
        )
        try:
            relative = Path(absolute).relative_to(root_path).as_posix()
        except ValueError:
            return None
        valid_album_id = type(album_id) is int and album_id > 0
        if relative in destinations and not valid_album_id:
            return None
        if valid_album_id:
            album_paths.setdefault(album_id, []).append(relative)
            if relative in destinations:
                counts = destination_counts.setdefault(album_id, {})
                counts[relative] = counts.get(relative, 0) + 1
    if any(
        count > 1
        for counts in destination_counts.values()
        for count in counts.values()
    ):
        return None
    candidates = [
        album_id
        for album_id, counts in destination_counts.items()
        if set(counts) == destinations
        and all(count == 1 for count in counts.values())
    ]
    if len(candidates) != 1:
        return None
    album_id = candidates[0]
    album_rows = connection.execute(
        "SELECT id FROM albums WHERE id = ?", (album_id,)
    ).fetchall()
    if album_rows != [(album_id,)]:
        return None
    paths = album_paths.get(album_id)
    if not paths or len(paths) != len(set(paths)):
        return None
    return album_id, tuple(paths)


def _managed_album_boundary(destinations, root, root_identity, *, database_anchor=None):
    """Bind imported destinations to one live Beets album directory."""
    current_anchor = None
    anchor = database_anchor
    try:
        if anchor is None:
            current_anchor = _open_beets_database_anchor()
            anchor = current_anchor
        if (
            anchor is None
            or anchor.get("descriptor") is None
            or not _beets_database_anchor_matches(anchor)
        ):
            return None

        def inspect(connection):
            root_path = os.path.abspath(root)
            catalogue_album = _managed_catalogue_album(
                connection, destinations, root_path
            )
            if catalogue_album is None:
                return None
            _album_id, paths = catalogue_album
            directories = {str((Path(root_path) / relative).parent) for relative in paths}
            if len(directories) == 1:
                album_directory = next(iter(directories))
            else:
                disc_parents = {_consolidation_disc_parent(directory) for directory in directories}
                if None in disc_parents or len(disc_parents) != 1:
                    return None
                album_directory = next(iter(disc_parents))
            album_path = Path(album_directory).relative_to(root_path).as_posix()
            album_identity = _managed_album_directory_identity(root_path, root_identity, album_path)
            if album_identity is None:
                return None
            return album_path, album_identity

        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        return inspect_sqlite_source(
            anchor,
            _beets_database_anchor_matches,
            inspect,
            connect=sqlite3.connect,
            timeout=timeout,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if current_anchor is not None:
            _close_beets_database_anchor(current_anchor)


def _managed_database_matches(database_anchor, destinations, root):
    current_anchor = None
    anchor = database_anchor
    try:
        if anchor is None:
            return False
        if anchor.get("descriptor") is None:
            current_anchor = _open_beets_database_anchor()
            anchor = current_anchor
        elif not _beets_database_anchor_matches(anchor):
            return False
        if (
            anchor is None
            or anchor.get("descriptor") is None
            or not _beets_database_anchor_matches(anchor)
        ):
            return False
        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        return bool(
            inspect_sqlite_source(
                anchor,
                _beets_database_anchor_matches,
                lambda connection: _managed_catalogue_album(
                    connection, destinations, root
                )
                is not None,
                connect=sqlite3.connect,
                timeout=timeout,
            )
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        if current_anchor is not None:
            _close_beets_database_anchor(current_anchor)


def _read_managed_ownership_evidence(capture, *, database_anchor=None):
    if not isinstance(capture, dict):
        return None
    manifest = capture.get("_file")
    try:
        descriptor = manifest.fileno()
        held = os.fstat(descriptor)
        if not _managed_carrier_namespace_matches(capture) or not _managed_root_namespace_matches(
            capture
        ):
            return None
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != 0o600
            or int(held.st_dev) != capture.get("device")
            or int(held.st_ino) != capture.get("inode")
            or held.st_size <= 0
            or held.st_size > _OWNERSHIP_MAX_BYTES
        ):
            return None
        raw = os.pread(descriptor, held.st_size + 1, 0)
        if len(raw) != held.st_size or not raw.endswith(b"\n"):
            return None
        lines = raw.splitlines()
        if not lines or len(lines) > 10_000 or any(not line for line in lines):
            return None
        snapshots = [decode_recovery_json(line, max_bytes=_OWNERSHIP_MAX_BYTES) for line in lines]
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if not all(_valid_managed_snapshot(item) for item in snapshots):
        return None
    for line, snapshot in zip(lines, snapshots):
        canonical = json.dumps(
            snapshot,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if line != canonical:
            return None
    first = snapshots[0]
    if (
        first["generation"] != 0
        or first["previous_hash"] is not None
        or first["sealed"] is not False
        or first["mappings"]
        or first["cleanup_directories"]
        or first["nonce"] != capture.get("nonce")
        or first["owner"] != capture.get("owner")
        or first["root"] != capture.get("root")
        or first["root_identity"] != capture.get("root_identity")
        or first["intent"] != capture.get("intent")
    ):
        return None
    intent_slots = set()
    intent_paths = set()
    intent_nodes = set()
    for binding in first["intent"]:
        node = tuple(binding["identity"][:2])
        if (
            binding["slot"] in intent_slots
            or binding["path"] in intent_paths
            or node in intent_nodes
        ):
            return None
        intent_slots.add(binding["slot"])
        intent_paths.add(binding["path"])
        intent_nodes.add(node)
    declarations = {item["slot"]: item for item in first["intent"]}
    previous = None
    prior_mappings = {}
    for generation, snapshot in enumerate(snapshots):
        if (
            snapshot["generation"] != generation
            or snapshot["previous_hash"] != previous
            or snapshot["nonce"] != first["nonce"]
            or snapshot["owner"] != first["owner"]
            or snapshot["root"] != first["root"]
            or snapshot["root_identity"] != first["root_identity"]
            or snapshot["intent"] != first["intent"]
            or (snapshot["sealed"] and snapshot is not snapshots[-1])
        ):
            return None
        current_mappings = {}
        destination_paths = set()
        destination_nodes = set()
        for mapping in snapshot["mappings"]:
            destination = mapping["destination"]
            node = (
                destination["identity"]["device"],
                destination["identity"]["inode"],
            )
            if (
                mapping["slot"] in current_mappings
                or destination["path"] in destination_paths
                or node in destination_nodes
                or declarations.get(mapping["slot"])
                != {
                    "slot": mapping["slot"],
                    **mapping["source"],
                }
            ):
                return None
            current_mappings[mapping["slot"]] = mapping
            destination_paths.add(destination["path"])
            destination_nodes.add(node)
        cleanup_paths = set()
        temporary_paths = set()
        cleanup_nodes = set()
        for record in snapshot["cleanup_directories"]:
            node = (
                record["directory"]["device"],
                record["directory"]["inode"],
            )
            if (
                record["relative"] == record["temporary_relative"]
                or record["relative"] in cleanup_paths
                or record["temporary_relative"] in temporary_paths
                or node in cleanup_nodes
            ):
                return None
            cleanup_paths.add(record["relative"])
            temporary_paths.add(record["temporary_relative"])
            cleanup_nodes.add(node)
        if any(current_mappings.get(slot) != mapping for slot, mapping in prior_mappings.items()):
            return None
        prior_mappings = current_mappings
        previous = snapshot["hash"]
    final = snapshots[-1]
    if final["sealed"] is not True:
        return None
    mapping_slots = set()
    destinations = set()
    destination_nodes = set()
    for mapping in final["mappings"]:
        destination = mapping["destination"]
        node = (
            destination["identity"]["device"],
            destination["identity"]["inode"],
        )
        if (
            mapping["slot"] in mapping_slots
            or destination["path"] in destinations
            or node in destination_nodes
            or declarations.get(mapping["slot"])
            != {
                "slot": mapping["slot"],
                **mapping["source"],
            }
            or not _managed_destination_matches(final["root"], final["root_identity"], destination)
        ):
            return None
        mapping_slots.add(mapping["slot"])
        destinations.add(destination["path"])
        destination_nodes.add(node)
    if mapping_slots != intent_slots or len(final["mappings"]) != len(first["intent"]):
        return None
    for source in first["intent"]:
        try:
            os.stat(source["path"], follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        return None
    if not _managed_database_matches(database_anchor, destinations, final["root"]):
        return None
    return (
        final
        if (
            _managed_carrier_namespace_matches(capture) and _managed_root_namespace_matches(capture)
        )
        else None
    )


def _managed_completion_from_snapshot(carrier, evidence, *, database_anchor=None):
    try:
        if (
            type(carrier) is not dict
            or type(evidence) is not dict
            or not _valid_managed_snapshot(evidence)
            or evidence.get("sealed") is not True
            or set(carrier)
            != {
                "version",
                "path",
                "device",
                "inode",
                "parent_device",
                "parent_inode",
                "nonce",
                "owner",
            }
            or carrier["version"] not in {1, 2}
            or type(carrier["version"]) is not int
            or not isinstance(carrier["path"], str)
            or not os.path.isabs(carrier["path"])
            or os.path.abspath(carrier["path"]) != carrier["path"]
            or any(
                type(carrier[field]) is not int or carrier[field] < 0
                for field in (
                    "device",
                    "inode",
                    "parent_device",
                    "parent_inode",
                )
            )
            or carrier["nonce"] != evidence["nonce"]
            or carrier["owner"] != evidence["owner"]
            or (
                carrier["version"] == 2
                and Path(carrier["path"]).name != f".qobuz-managed-beets-{carrier['nonce']}.jsonl"
            )
        ):
            return None
        owner = normalise_recovery_owner(evidence["owner"])
        if owner is None:
            return None
        root_identity = evidence["root_identity"]
        immutable_owner = RecoveryOwner(
            operation_id=owner["operation_id"],
            item_id=owner["item_id"],
        )
        mappings = []
        slots = set()
        source_paths = set()
        source_nodes = set()
        destination_paths = set()
        destination_nodes = set()
        declarations = {binding["slot"]: binding for binding in evidence["intent"]}
        if len(declarations) != len(evidence["intent"]):
            return None
        for value in evidence["mappings"]:
            source = value["source"]
            destination = value["destination"]
            source_identity = tuple(source["identity"])
            destination_identity = destination["identity"]
            destination_node = (
                destination_identity["device"],
                destination_identity["inode"],
            )
            if (
                value["slot"] in slots
                or source["path"] in source_paths
                or source_identity[:2] in source_nodes
                or destination["path"] in destination_paths
                or destination_node in destination_nodes
                or declarations.get(value["slot"])
                != {
                    "slot": value["slot"],
                    **source,
                }
            ):
                return None
            slots.add(value["slot"])
            source_paths.add(source["path"])
            source_nodes.add(source_identity[:2])
            destination_paths.add(destination["path"])
            destination_nodes.add(destination_node)
            mappings.append(
                ManagedMapping(
                    slot=value["slot"],
                    source_path=source["path"],
                    source_identity=source_identity,
                    destination_path=destination["path"],
                    destination_identity=(
                        destination_identity["device"],
                        destination_identity["inode"],
                        destination_identity["size"],
                        destination_identity["modified_ns"],
                        destination_identity["changed_ns"],
                    ),
                )
            )
        if slots != set(declarations):
            return None
        album_boundary = _managed_album_boundary(
            destination_paths,
            evidence["root"],
            root_identity,
            database_anchor=database_anchor,
        )
        if album_boundary is None:
            return None
        album_path, album_identity = album_boundary
        return ManagedImportEvidence(
            owner=immutable_owner,
            library_root=evidence["root"],
            library_root_identity=(
                root_identity["device"],
                root_identity["inode"],
                root_identity["size"],
                root_identity["modified_ns"],
                root_identity["changed_ns"],
            ),
            album_path=album_path,
            album_identity=album_identity,
            manifest_hash=evidence["hash"],
            mappings=tuple(mappings),
        )
    except (KeyError, TypeError, ValueError):
        return None


def managed_completion_evidence(result):
    """Copy one accepted sealed managed result into immutable proof input."""
    if (
        type(result) is not ManagedBeetsImportResult
        or result.kind != "ok"
        or result.spawned is not True
    ):
        return None
    return _managed_completion_from_snapshot(result.carrier, result.evidence)


class ManagedEvidenceUnavailable(OSError):
    """A persisted managed import cannot provide live completion authority."""


class ManagedCarrierInspectionOutcome(str, Enum):
    SEALED = "sealed"
    UNSEALED_ORIGIN = "unsealed_origin"
    UNSEALED_ACTIVITY = "unsealed_activity"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ManagedCarrierInspectionResult:
    outcome: ManagedCarrierInspectionOutcome
    manifest_hash: str | None = None


class ManagedReservationInspectionOutcome(str, Enum):
    ABSENT = "absent"
    ORIGIN = "origin"
    ACTIVITY = "activity"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ManagedReservationInspectionResult:
    outcome: ManagedReservationInspectionOutcome
    carrier: dict | None = None
    manifest_hash: str | None = None


class ManagedCarrierRetirementOutcome(str, Enum):
    RETIRED = "retired"
    ALREADY_ABSENT = "already_absent"
    REFUSED = "refused"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ManagedCarrierRetirementResult:
    outcome: ManagedCarrierRetirementOutcome
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ManagedCarrierHistory:
    final_snapshot: dict
    generation_count: int
    intent_slots: frozenset
    mapped_slots: frozenset


def _managed_carrier_stat(manifest):
    value = os.fstat(manifest.fileno())
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_uid),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _close_managed_descriptor(descriptor):
    os.close(descriptor)


def _open_managed_carrier_file(parent_fd, name):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ManagedEvidenceUnavailable("safe managed completion carrier access is unavailable")

    def opener(_path, flags):
        return os.open(
            name,
            flags | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )

    return open(name, "rb", buffering=0, opener=opener)


def _managed_carrier_history(
    manifest,
    *,
    parent_fd,
    name,
    reference,
    expected_owner,
):
    """Validate canonical history without consulting mutable landings."""
    try:
        descriptor = manifest.fileno()
        before = _managed_carrier_stat(manifest)
        if (
            not stat.S_ISREG(before[2])
            or before[3] != os.geteuid()
            or stat.S_IMODE(before[2]) != 0o600
            or before[4] != 1
            or before[0] != reference["device"]
            or before[1] != reference["inode"]
            or before[5] <= 0
            or before[5] > _OWNERSHIP_MAX_BYTES
            or not _merge_name_matches(parent_fd, name, descriptor, regular=True)
        ):
            return None
        raw = os.pread(descriptor, before[5] + 1, 0)
        after = _managed_carrier_stat(manifest)
        if before != after or len(raw) != before[5] or not raw.endswith(b"\n"):
            return None
        lines = raw.splitlines()
        if not lines or len(lines) > 10_000 or any(not line for line in lines):
            return None
        snapshots = [decode_recovery_json(line, max_bytes=_OWNERSHIP_MAX_BYTES) for line in lines]
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if not all(_valid_managed_snapshot(snapshot) for snapshot in snapshots):
        return None
    for line, snapshot in zip(lines, snapshots):
        canonical = json.dumps(
            snapshot,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if line != canonical:
            return None
    first = snapshots[0]
    if (
        first["generation"] != 0
        or first["previous_hash"] is not None
        or first["sealed"] is not False
        or first["mappings"]
        or first["cleanup_directories"]
        or first["nonce"] != reference["nonce"]
        or first["owner"] != expected_owner
    ):
        return None
    intent_slots = set()
    intent_paths = set()
    intent_nodes = set()
    for binding in first["intent"]:
        node = tuple(binding["identity"][:2])
        if (
            binding["slot"] in intent_slots
            or binding["path"] in intent_paths
            or node in intent_nodes
        ):
            return None
        intent_slots.add(binding["slot"])
        intent_paths.add(binding["path"])
        intent_nodes.add(node)
    declarations = {item["slot"]: item for item in first["intent"]}
    previous_hash = None
    previous_mappings = {}
    for generation, snapshot in enumerate(snapshots):
        if (
            snapshot["generation"] != generation
            or snapshot["version"] != first["version"]
            or snapshot["previous_hash"] != previous_hash
            or snapshot["nonce"] != first["nonce"]
            or snapshot["owner"] != first["owner"]
            or snapshot["root"] != first["root"]
            or snapshot["root_identity"] != first["root_identity"]
            or snapshot["intent"] != first["intent"]
            or snapshot["sealed"]
            and snapshot is not snapshots[-1]
        ):
            return None
        current_mappings = {}
        destination_paths = set()
        destination_nodes = set()
        for mapping in snapshot["mappings"]:
            destination = mapping["destination"]
            node = (
                destination["identity"]["device"],
                destination["identity"]["inode"],
            )
            if (
                mapping["slot"] in current_mappings
                or destination["path"] in destination_paths
                or node in destination_nodes
                or declarations.get(mapping["slot"])
                != {
                    "slot": mapping["slot"],
                    **mapping["source"],
                }
            ):
                return None
            current_mappings[mapping["slot"]] = mapping
            destination_paths.add(destination["path"])
            destination_nodes.add(node)
        cleanup_paths = set()
        temporary_paths = set()
        cleanup_nodes = set()
        for record in snapshot["cleanup_directories"]:
            node = (
                record["directory"]["device"],
                record["directory"]["inode"],
            )
            if (
                record["relative"] == record["temporary_relative"]
                or record["relative"] in cleanup_paths
                or record["temporary_relative"] in temporary_paths
                or node in cleanup_nodes
            ):
                return None
            cleanup_paths.add(record["relative"])
            temporary_paths.add(record["temporary_relative"])
            cleanup_nodes.add(node)
        if any(
            current_mappings.get(slot) != mapping for slot, mapping in previous_mappings.items()
        ):
            return None
        previous_mappings = current_mappings
        previous_hash = snapshot["hash"]
    if first["version"] == _MANAGED_SNAPSHOT_VERSION and len(snapshots) > 1:
        launch = snapshots[1]
        if launch["sealed"] is not False or launch["mappings"] or launch["cleanup_directories"]:
            return None
    return _ManagedCarrierHistory(
        final_snapshot=snapshots[-1],
        generation_count=len(snapshots),
        intent_slots=frozenset(intent_slots),
        mapped_slots=frozenset(previous_mappings),
    )


def _managed_retirement_history(
    manifest,
    *,
    parent_fd,
    name,
    reference,
    expected_owner,
    expected_manifest_hash,
):
    history = _managed_carrier_history(
        manifest,
        parent_fd=parent_fd,
        name=name,
        reference=reference,
        expected_owner=expected_owner,
    )
    if history is None:
        return None
    final = history.final_snapshot
    if (
        final["sealed"] is not True
        or final["hash"] != expected_manifest_hash
        or history.mapped_slots != history.intent_slots
        or len(final["mappings"]) != len(final["intent"])
    ):
        return None
    return final


def _managed_prelaunch_retirement_history(
    manifest,
    *,
    parent_fd,
    name,
    reference,
    expected_owner,
    expected_manifest_hash,
):
    history = _managed_carrier_history(
        manifest,
        parent_fd=parent_fd,
        name=name,
        reference=reference,
        expected_owner=expected_owner,
    )
    if history is None:
        return None
    final = history.final_snapshot
    if (
        final["version"] != _MANAGED_SNAPSHOT_VERSION
        or history.generation_count != 1
        or final["generation"] != 0
        or final["sealed"] is not False
        or final["hash"] != expected_manifest_hash
        or final["mappings"]
        or final["cleanup_directories"]
        or history.mapped_slots
    ):
        return None
    return final


def _managed_carrier_parameters(carrier_reference, expected_owner):
    if type(expected_owner) is RecoveryOwner:
        expected_owner = {
            "operation_id": expected_owner.operation_id,
            "item_id": expected_owner.item_id,
        }
    try:
        expected_owner = normalise_recovery_owner(expected_owner)
    except ValueError:
        expected_owner = None
    reference_fields = {
        "version",
        "path",
        "device",
        "inode",
        "parent_device",
        "parent_inode",
        "nonce",
        "owner",
    }
    if (
        expected_owner is None
        or type(carrier_reference) is not dict
        or set(carrier_reference) != reference_fields
        or type(carrier_reference.get("version")) is not int
        or carrier_reference["version"] not in {1, 2}
        or any(
            type(carrier_reference.get(field)) is not int or carrier_reference[field] < 0
            for field in (
                "device",
                "inode",
                "parent_device",
                "parent_inode",
            )
        )
        or not isinstance(carrier_reference.get("nonce"), str)
        or len(carrier_reference["nonce"]) != 64
        or any(value not in "0123456789abcdef" for value in carrier_reference["nonce"])
    ):
        return None
    try:
        reference_owner = normalise_recovery_owner(carrier_reference.get("owner"))
    except ValueError:
        return None
    if reference_owner != expected_owner:
        return None
    raw_path = carrier_reference.get("path")
    if (
        not isinstance(raw_path, str)
        or not os.path.isabs(raw_path)
        or os.path.abspath(raw_path) != raw_path
    ):
        return None
    carrier_path = Path(raw_path)
    try:
        configured_database = os.fspath(cfg.BEETS_DB_PATH)
        if not configured_database:
            return None
        database = Path(os.path.abspath(configured_database))
    except (OSError, TypeError, ValueError):
        return None
    parent = database.parent
    if (
        carrier_path.parent != parent
        or not carrier_path.name.startswith(".qobuz-managed-beets-")
        or not carrier_path.name.endswith(".jsonl")
        or len(carrier_path.name) <= len(".qobuz-managed-beets-.jsonl")
    ):
        return None
    if (
        carrier_reference["version"] == 2
        and carrier_path.name != f".qobuz-managed-beets-{carrier_reference['nonce']}.jsonl"
    ):
        return None
    return (
        {**carrier_reference, "owner": dict(reference_owner)},
        dict(expected_owner),
        carrier_path,
        parent,
    )


def _managed_reservation_parameters(reservation, expected_owner):
    if type(expected_owner) is RecoveryOwner:
        expected_owner = {
            "operation_id": expected_owner.operation_id,
            "item_id": expected_owner.item_id,
        }
    try:
        expected_owner = normalise_recovery_owner(expected_owner)
        reference_owner = normalise_recovery_owner(reservation.get("owner"))
        database = Path(os.path.abspath(os.fspath(cfg.BEETS_DB_PATH)))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    fields = {
        "version",
        "path",
        "parent_device",
        "parent_inode",
        "nonce",
        "owner",
    }
    if (
        expected_owner is None
        or type(reservation) is not dict
        or set(reservation) != fields
        or type(reservation.get("version")) is not int
        or reservation["version"] != 1
        or any(
            type(reservation.get(field)) is not int or reservation[field] < 0
            for field in ("parent_device", "parent_inode")
        )
        or not isinstance(reservation.get("nonce"), str)
        or len(reservation["nonce"]) != 64
        or any(value not in "0123456789abcdef" for value in reservation["nonce"])
        or reference_owner != expected_owner
    ):
        return None
    parent = database.parent
    path = parent / f".qobuz-managed-beets-{reservation['nonce']}.jsonl"
    if reservation.get("path") != str(path):
        return None
    return (
        {**reservation, "owner": dict(reference_owner)},
        dict(expected_owner),
        path,
        parent,
    )


def _managed_retirement_parameters(
    carrier_reference,
    expected_owner,
    expected_manifest_hash,
    quarantine_name,
):
    parameters = _managed_carrier_parameters(carrier_reference, expected_owner)
    if (
        parameters is None
        or not isinstance(expected_manifest_hash, str)
        or len(expected_manifest_hash) != 64
        or any(value not in "0123456789abcdef" for value in expected_manifest_hash)
    ):
        return None
    prefix = ".qobuz-retire-managed-beets-"
    if (
        not isinstance(quarantine_name, str)
        or not quarantine_name.startswith(prefix)
        or len(quarantine_name) != len(prefix) + 64
        or any(value not in "0123456789abcdef" for value in quarantine_name[len(prefix) :])
    ):
        return None
    return parameters


def _inspect_managed_carrier_with_interrupts_deferred(
    carrier_reference,
    expected_owner,
):
    unavailable = ManagedCarrierInspectionResult(ManagedCarrierInspectionOutcome.UNAVAILABLE)
    parameters = _managed_carrier_parameters(carrier_reference, expected_owner)
    if parameters is None:
        return unavailable
    reference, owner, carrier_path, parent = parameters
    parent_chain = []
    parent_names = ()
    carrier = None
    exclusion = None
    caught = None

    def finish_cleanup():
        error = None

        def remember(exc):
            nonlocal error
            error = _prefer_managed_cleanup_error(error, exc)

        nonlocal exclusion
        current_exclusion = exclusion
        exclusion = None
        if current_exclusion is not None:
            try:
                current_exclusion.close()
            except BaseException as exc:
                remember(exc)
        nonlocal carrier
        current_carrier = carrier
        carrier = None
        if current_carrier is not None:
            try:
                current_carrier.close()
            except BaseException as exc:
                remember(exc)
        for descriptor in reversed(parent_chain):
            try:
                _close_managed_descriptor(descriptor)
            except BaseException as exc:
                remember(exc)
        parent_chain.clear()
        return error

    try:
        parent_chain, parent_names = _open_absolute_merge_chain(parent)
        parent_stat = os.fstat(parent_chain[-1])
        if (
            int(parent_stat.st_dev) != reference["parent_device"]
            or int(parent_stat.st_ino) != reference["parent_inode"]
        ):
            return unavailable
        carrier = _open_managed_carrier_file(parent_chain[-1], carrier_path.name)
        exclusion = acquire_inode_write_exclusion(carrier.fileno())
        if exclusion is None or not exclusion.intact():
            return unavailable
        history = _managed_carrier_history(
            carrier,
            parent_fd=parent_chain[-1],
            name=carrier_path.name,
            reference=reference,
            expected_owner=owner,
        )
        if history is None or not exclusion.intact():
            return unavailable
        final = history.final_snapshot
        if final["sealed"] is True:
            if history.mapped_slots != history.intent_slots or len(final["mappings"]) != len(
                final["intent"]
            ):
                return unavailable
            outcome = ManagedCarrierInspectionOutcome.SEALED
        elif final["version"] == _MANAGED_SNAPSHOT_VERSION and history.generation_count == 1:
            outcome = ManagedCarrierInspectionOutcome.UNSEALED_ORIGIN
        else:
            outcome = ManagedCarrierInspectionOutcome.UNSEALED_ACTIVITY
        held = os.fstat(carrier.fileno())
        if (
            not exclusion.intact()
            or not _merge_chain_is_named(parent_chain, parent_names)
            or int(held.st_dev) != reference["device"]
            or int(held.st_ino) != reference["inode"]
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != 0o600
            or held.st_nlink != 1
            or not _merge_name_matches(
                parent_chain[-1], carrier_path.name, carrier.fileno(), regular=True
            )
        ):
            return unavailable
        return ManagedCarrierInspectionResult(outcome, final["hash"])
    except (OSError, TypeError, ValueError):
        return unavailable
    except BaseException as exc:
        caught = exc
        raise
    finally:
        try:
            cleanup_error = _run_managed_sigint_deferred(finish_cleanup)
        except BaseException as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            if caught is None:
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            if not isinstance(cleanup_error, Exception):
                if hasattr(cleanup_error, "add_note"):
                    cleanup_error.add_note(f"managed carrier inspection also failed: {caught}")
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            if hasattr(caught, "add_note"):
                caught.add_note(f"managed carrier cleanup also failed: {cleanup_error}")


def inspect_managed_carrier(carrier_reference, expected_owner):
    """Classify one exact carrier without changing persisted state."""
    with _MANAGED_CARRIER_RETIREMENT_LOCK:
        return _run_managed_sigint_deferred(
            lambda: _inspect_managed_carrier_with_interrupts_deferred(
                carrier_reference, expected_owner
            )
        )


def _inspect_managed_reservation_with_interrupts_deferred(reservation, expected_owner):
    unavailable = ManagedReservationInspectionResult(
        ManagedReservationInspectionOutcome.UNAVAILABLE
    )
    parameters = _managed_reservation_parameters(reservation, expected_owner)
    if parameters is None:
        return unavailable
    reservation, owner, carrier_path, parent = parameters
    parent_chain = []
    carrier = None
    try:
        parent_chain, parent_names = _open_absolute_merge_chain(parent)
        parent_stat = os.fstat(parent_chain[-1])
        if (
            int(parent_stat.st_dev) != reservation["parent_device"]
            or int(parent_stat.st_ino) != reservation["parent_inode"]
            or not _merge_chain_is_named(parent_chain, parent_names)
        ):
            return unavailable
        try:
            carrier = _open_managed_carrier_file(parent_chain[-1], carrier_path.name)
        except FileNotFoundError:
            quarantine_name = f".qobuz-retire-managed-beets-{reservation['nonce']}"
            if _merge_chain_is_named(parent_chain, parent_names) and _merge_name_missing(
                parent_chain[-1], quarantine_name
            ):
                return ManagedReservationInspectionResult(
                    ManagedReservationInspectionOutcome.ABSENT
                )
            return unavailable
        held = os.fstat(carrier.fileno())
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != 0o600
            or held.st_nlink != 1
            or not _merge_chain_is_named(parent_chain, parent_names)
            or not _merge_name_matches(
                parent_chain[-1], carrier_path.name, carrier.fileno(), regular=True
            )
        ):
            return unavailable
        reference = {
            "version": 2,
            "path": str(carrier_path),
            "device": int(held.st_dev),
            "inode": int(held.st_ino),
            "parent_device": reservation["parent_device"],
            "parent_inode": reservation["parent_inode"],
            "nonce": reservation["nonce"],
            "owner": dict(owner),
        }
    except (OSError, TypeError, ValueError):
        return unavailable
    finally:
        if carrier is not None:
            carrier.close()
        for descriptor in reversed(parent_chain):
            os.close(descriptor)

    inspected = inspect_managed_carrier(reference, owner)
    if inspected.outcome is ManagedCarrierInspectionOutcome.UNSEALED_ORIGIN:
        outcome = ManagedReservationInspectionOutcome.ORIGIN
    elif inspected.outcome in {
        ManagedCarrierInspectionOutcome.UNSEALED_ACTIVITY,
        ManagedCarrierInspectionOutcome.SEALED,
    }:
        outcome = ManagedReservationInspectionOutcome.ACTIVITY
    else:
        return unavailable
    return ManagedReservationInspectionResult(
        outcome,
        reference,
        inspected.manifest_hash,
    )


def inspect_managed_reservation(reservation, expected_owner):
    """Classify the exact reserved carrier name without adopting neighbours."""
    with _MANAGED_CARRIER_RETIREMENT_LOCK:
        try:
            return _run_managed_sigint_deferred(
                lambda: _inspect_managed_reservation_with_interrupts_deferred(
                    reservation, expected_owner
                )
            )
        except (OSError, TypeError, ValueError):
            return ManagedReservationInspectionResult(
                ManagedReservationInspectionOutcome.UNAVAILABLE
            )


def _owner_only_private_directory_mode(mode):
    """Accept 0700 plus the setgid bit inherited from a shared parent."""
    return stat.S_IMODE(mode) in (0o700, stat.S_ISGID | 0o700)


def _retire_managed_carrier_with_interrupts_deferred(
    carrier_reference,
    expected_owner,
    expected_manifest_hash,
    quarantine_name,
    *,
    prelaunch=False,
):
    parameters = _managed_retirement_parameters(
        carrier_reference,
        expected_owner,
        expected_manifest_hash,
        quarantine_name,
    )
    if parameters is None:
        return ManagedCarrierRetirementResult(
            ManagedCarrierRetirementOutcome.REFUSED,
            "managed carrier retirement input is malformed",
        )
    reference, owner, carrier_path, parent = parameters
    parent_chain = []
    parent_names = ()
    opened_files = []
    private_fd = None
    exclusion = None
    caught = None
    namespace_mutated = False
    private_name = "carrier.jsonl"

    def parent_namespace_is_exact():
        if not parent_chain:
            return False
        try:
            held = os.fstat(parent_chain[-1])
        except OSError:
            return False
        return (
            int(held.st_dev) == reference["parent_device"]
            and int(held.st_ino) == reference["parent_inode"]
            and _merge_chain_is_named(parent_chain, parent_names)
        )

    def parent_namespace_changed(reason):
        return ManagedCarrierRetirementResult(
            (
                ManagedCarrierRetirementOutcome.UNCERTAIN
                if namespace_mutated
                else ManagedCarrierRetirementOutcome.REFUSED
            ),
            reason,
        )

    def expected_history(selected, *, parent_fd, name):
        validator = (
            _managed_prelaunch_retirement_history if prelaunch else _managed_retirement_history
        )
        return validator(
            selected,
            parent_fd=parent_fd,
            name=name,
            reference=reference,
            expected_owner=owner,
            expected_manifest_hash=expected_manifest_hash,
        )

    def open_candidate(parent_fd, name):
        try:
            candidate = _open_managed_carrier_file(parent_fd, name)
        except FileNotFoundError:
            return None
        except OSError:
            return False
        opened_files.append(candidate)
        return candidate

    def exact_name(parent_fd, name, candidate):
        if candidate is None or candidate is False:
            return False
        try:
            held = os.fstat(candidate.fileno())
            return (
                stat.S_ISREG(held.st_mode)
                and held.st_uid == os.geteuid()
                and stat.S_IMODE(held.st_mode) == 0o600
                and held.st_nlink == 1
                and int(held.st_dev) == reference["device"]
                and int(held.st_ino) == reference["inode"]
                and _merge_name_matches(parent_fd, name, candidate.fileno(), regular=True)
            )
        except OSError:
            return False

    def private_group_is_exact():
        if private_fd is None:
            return _merge_name_missing(parent_chain[-1], quarantine_name)
        try:
            held = os.fstat(private_fd)
            return (
                stat.S_ISDIR(held.st_mode)
                and held.st_uid == os.geteuid()
                and _owner_only_private_directory_mode(held.st_mode)
                and _merge_directory_matches(
                    private_fd,
                    parent_chain[-1],
                    quarantine_name,
                )
            )
        except OSError:
            return False

    def open_private_group():
        try:
            descriptor = _open_exact_merge_dir(parent_chain[-1], quarantine_name)
        except FileNotFoundError:
            return None
        except OSError:
            return False
        held = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(held.st_mode)
            or held.st_uid != os.geteuid()
            or not _owner_only_private_directory_mode(held.st_mode)
        ):
            os.close(descriptor)
            return False
        return descriptor

    def create_private_group():
        nonlocal namespace_mutated, private_fd
        try:
            if not parent_namespace_is_exact():
                return False
            os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_chain[-1])
            namespace_mutated = True
            private_fd = _open_exact_merge_dir(parent_chain[-1], quarantine_name)
            held = os.fstat(private_fd)
            if (
                held.st_uid != os.geteuid()
                or not _owner_only_private_directory_mode(held.st_mode)
                or not private_group_is_exact()
            ):
                return False
            os.fsync(parent_chain[-1])
            return parent_namespace_is_exact()
        except OSError:
            return False

    def private_entries():
        if private_fd is None or not private_group_is_exact():
            return None
        try:
            return os.listdir(private_fd)
        except OSError:
            return None

    def layout(selected):
        try:
            public_exact = _merge_name_matches(
                parent_chain[-1], carrier_path.name, selected.fileno(), regular=True
            )
            private_exact = (
                private_fd is not None
                and private_group_is_exact()
                and _merge_name_matches(private_fd, private_name, selected.fileno(), regular=True)
            )
            private_missing = (
                private_fd is None and _merge_name_missing(parent_chain[-1], quarantine_name)
            ) or (
                private_fd is not None
                and private_group_is_exact()
                and _merge_name_missing(private_fd, private_name)
            )
            held = os.fstat(selected.fileno())
        except OSError:
            return "changed"
        if public_exact and private_missing:
            return "public"
        if private_exact and not public_exact:
            return "private"
        if private_missing and held.st_nlink == 0:
            return "removed"
        return "changed"

    def restore_unexpected_private():
        nonlocal namespace_mutated
        if (
            not parent_namespace_is_exact()
            or not _merge_name_missing(parent_chain[-1], carrier_path.name)
            or private_fd is None
            or not private_group_is_exact()
            or _merge_name_missing(private_fd, private_name)
        ):
            return False
        unexpected = None
        try:
            unexpected = _open_managed_carrier_file(private_fd, private_name)
            opened_files.append(unexpected)
            if not parent_namespace_is_exact():
                return False
            _rename_ownership_noreplace(
                private_fd,
                private_name,
                parent_chain[-1],
                carrier_path.name,
            )
            restored = _merge_name_matches(
                parent_chain[-1], carrier_path.name, unexpected.fileno(), regular=True
            ) and _merge_name_missing(private_fd, private_name)
            if restored:
                namespace_mutated = True
                if not parent_namespace_is_exact():
                    return False
                os.fsync(private_fd)
                if not parent_namespace_is_exact():
                    return False
                os.fsync(parent_chain[-1])
                if not parent_namespace_is_exact():
                    return False
            return restored and parent_namespace_is_exact()
        except OSError:
            return False

    def finish_cleanup():
        error = None

        def remember(exc):
            nonlocal error
            error = _prefer_managed_cleanup_error(error, exc)

        nonlocal exclusion
        current_exclusion = exclusion
        exclusion = None
        if current_exclusion is not None:
            try:
                current_exclusion.close()
            except BaseException as exc:
                remember(exc)
        for candidate in reversed(opened_files):
            try:
                candidate.close()
            except BaseException as exc:
                remember(exc)
        opened_files.clear()
        nonlocal private_fd
        current_private_fd = private_fd
        private_fd = None
        if current_private_fd is not None:
            try:
                _close_managed_descriptor(current_private_fd)
            except BaseException as exc:
                remember(exc)
        for descriptor in reversed(parent_chain):
            try:
                _close_managed_descriptor(descriptor)
            except BaseException as exc:
                remember(exc)
        parent_chain.clear()
        return error

    try:
        parent_chain, parent_names = _open_absolute_merge_chain(parent)
        if not parent_namespace_is_exact():
            return parent_namespace_changed("managed carrier parent changed")
        public = open_candidate(parent_chain[-1], carrier_path.name)
        opened_private = open_private_group()
        if opened_private is False:
            return ManagedCarrierRetirementResult(
                ManagedCarrierRetirementOutcome.REFUSED,
                "managed carrier private recovery changed",
            )
        private_fd = opened_private
        private = open_candidate(private_fd, private_name) if private_fd is not None else None
        public_exact = exact_name(parent_chain[-1], carrier_path.name, public)
        private_exact = exact_name(private_fd, private_name, private)
        if public is None and private is None:
            if not parent_namespace_is_exact():
                return parent_namespace_changed(
                    "managed carrier parent changed before absence recovery"
                )
            entries = private_entries() if private_fd is not None else []
            if entries is None or entries:
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.REFUSED,
                    "managed carrier private recovery changed",
                )
            try:
                if private_fd is not None:
                    os.fsync(private_fd)
                    if not parent_namespace_is_exact():
                        return parent_namespace_changed(
                            "managed carrier parent changed during absence recovery"
                        )
                    if not private_group_is_exact():
                        raise OSError("managed carrier quarantine changed")
                    if not parent_namespace_is_exact():
                        return parent_namespace_changed(
                            "managed carrier parent changed before quarantine cleanup"
                        )
                    os.rmdir(quarantine_name, dir_fd=parent_chain[-1])
                    namespace_mutated = True
                os.fsync(parent_chain[-1])
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed during absence recovery"
                    )
            except OSError:
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.UNCERTAIN,
                    "managed carrier absence could not be committed",
                )
            if (
                not _merge_name_missing(parent_chain[-1], carrier_path.name)
                or not _merge_name_missing(parent_chain[-1], quarantine_name)
                or not parent_namespace_is_exact()
            ):
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed before absence was confirmed"
                    )
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.UNCERTAIN,
                    "managed carrier reappeared during retirement",
                )
            if not parent_namespace_is_exact():
                return parent_namespace_changed(
                    "managed carrier parent changed before absence was confirmed"
                )
            return ManagedCarrierRetirementResult(ManagedCarrierRetirementOutcome.ALREADY_ABSENT)
        if private_exact:
            selected = private
        elif public_exact and private is None:
            selected = public
        else:
            return ManagedCarrierRetirementResult(
                ManagedCarrierRetirementOutcome.REFUSED,
                "managed carrier name or identity changed",
            )
        exclusion = acquire_inode_write_exclusion(selected.fileno())
        if exclusion is None or not exclusion.intact():
            return ManagedCarrierRetirementResult(
                ManagedCarrierRetirementOutcome.REFUSED,
                "managed carrier writer exclusion is unavailable",
            )
        selected_name = private_name if private_exact else carrier_path.name
        selected_parent_fd = private_fd if private_exact else parent_chain[-1]
        history = expected_history(
            selected,
            parent_fd=selected_parent_fd,
            name=selected_name,
        )
        if not parent_namespace_is_exact():
            return parent_namespace_changed(
                "managed carrier parent changed during history validation"
            )
        if history is None or not exclusion.intact():
            return ManagedCarrierRetirementResult(
                ManagedCarrierRetirementOutcome.REFUSED,
                "managed carrier history changed",
            )
        if selected_name == carrier_path.name:
            if private_fd is None:
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed before private recovery creation"
                    )
                if not create_private_group():
                    if not parent_namespace_is_exact():
                        return parent_namespace_changed(
                            "managed carrier parent changed during private recovery creation"
                        )
                    return ManagedCarrierRetirementResult(
                        ManagedCarrierRetirementOutcome.UNCERTAIN,
                        "managed carrier private recovery could not be created",
                    )
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed during private recovery creation"
                    )
            if private_entries() != [] or layout(selected) != "public":
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.REFUSED,
                    "managed carrier private recovery changed",
                )
            if not parent_namespace_is_exact():
                return parent_namespace_changed(
                    "managed carrier parent changed before quarantine move"
                )
            deferred = None
            try:
                _rename_ownership_noreplace(
                    parent_chain[-1],
                    carrier_path.name,
                    private_fd,
                    private_name,
                )
            except BaseException as exc:
                deferred = exc
            current_layout = layout(selected)
            if current_layout == "private":
                namespace_mutated = True
            if not parent_namespace_is_exact():
                if deferred is not None and not isinstance(deferred, Exception):
                    if hasattr(deferred, "add_note"):
                        deferred.add_note("managed carrier was retained in private recovery")
                    raise deferred
                return parent_namespace_changed(
                    "managed carrier parent changed during quarantine move"
                )
            if current_layout != "private":
                restored = restore_unexpected_private()
                if deferred is not None and not isinstance(deferred, Exception):
                    if hasattr(deferred, "add_note"):
                        deferred.add_note("managed carrier retirement did not take ownership")
                    raise deferred
                return ManagedCarrierRetirementResult(
                    (
                        ManagedCarrierRetirementOutcome.REFUSED
                        if restored or layout(selected) == "public"
                        else ManagedCarrierRetirementOutcome.UNCERTAIN
                    ),
                    "managed carrier quarantine move was not exact",
                )
            try:
                os.fsync(private_fd)
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed while quarantine was committed"
                    )
                os.fsync(parent_chain[-1])
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed while quarantine was committed"
                    )
            except OSError:
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.UNCERTAIN,
                    "managed carrier quarantine move could not be committed",
                )
            if deferred is not None:
                if not isinstance(deferred, Exception):
                    if hasattr(deferred, "add_note"):
                        deferred.add_note("managed carrier was retained in private recovery")
                    raise deferred
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.UNCERTAIN,
                    "managed carrier move reported an uncertain result",
                )
        history = expected_history(
            selected,
            parent_fd=private_fd,
            name=private_name,
        )
        if not parent_namespace_is_exact():
            return parent_namespace_changed(
                "managed carrier parent changed during private recovery validation"
            )
        if history is None or not exclusion.intact() or layout(selected) != "private":
            return ManagedCarrierRetirementResult(
                ManagedCarrierRetirementOutcome.REFUSED,
                "managed carrier private recovery changed",
            )
        if not parent_namespace_is_exact():
            return parent_namespace_changed("managed carrier parent changed before carrier unlink")
        deferred = None
        try:
            os.unlink(private_name, dir_fd=private_fd)
        except BaseException as exc:
            deferred = exc
        current_layout = layout(selected)
        if current_layout == "removed":
            namespace_mutated = True
        if not parent_namespace_is_exact():
            if deferred is not None and not isinstance(deferred, Exception):
                if hasattr(deferred, "add_note"):
                    deferred.add_note(
                        (
                            "managed carrier was unlinked before interruption"
                            if current_layout == "removed"
                            else "managed carrier remains in private recovery"
                        )
                    )
                raise deferred
            return parent_namespace_changed("managed carrier parent changed during carrier unlink")
        if current_layout == "removed":
            try:
                os.fsync(private_fd)
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed while carrier unlink was committed"
                    )
                if private_entries() != [] or not private_group_is_exact():
                    raise OSError("managed carrier quarantine changed")
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed before quarantine cleanup"
                    )
                os.rmdir(quarantine_name, dir_fd=parent_chain[-1])
                namespace_mutated = True
                os.fsync(parent_chain[-1])
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed while retirement was committed"
                    )
            except OSError:
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.UNCERTAIN,
                    "managed carrier unlink could not be committed",
                )
            if (
                not _merge_name_missing(parent_chain[-1], carrier_path.name)
                or not _merge_name_missing(parent_chain[-1], quarantine_name)
                or not parent_namespace_is_exact()
            ):
                if not parent_namespace_is_exact():
                    return parent_namespace_changed(
                        "managed carrier parent changed before retirement was confirmed"
                    )
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.UNCERTAIN,
                    "managed carrier namespace changed during retirement",
                )
            if deferred is not None:
                if not isinstance(deferred, Exception):
                    if hasattr(deferred, "add_note"):
                        deferred.add_note("managed carrier was unlinked before interruption")
                    raise deferred
                return ManagedCarrierRetirementResult(
                    ManagedCarrierRetirementOutcome.UNCERTAIN,
                    "managed carrier unlink reported an uncertain result",
                )
            if not parent_namespace_is_exact():
                return parent_namespace_changed(
                    "managed carrier parent changed before retirement was confirmed"
                )
            return ManagedCarrierRetirementResult(ManagedCarrierRetirementOutcome.RETIRED)
        if deferred is not None and not isinstance(deferred, Exception):
            if hasattr(deferred, "add_note"):
                deferred.add_note("managed carrier remains in private recovery")
            raise deferred
        return ManagedCarrierRetirementResult(
            ManagedCarrierRetirementOutcome.REFUSED,
            "managed carrier could not be unlinked exactly",
        )
    except (OSError, TypeError, ValueError):
        return ManagedCarrierRetirementResult(
            ManagedCarrierRetirementOutcome.REFUSED,
            "managed carrier retirement could not be opened safely",
        )
    except BaseException as exc:
        caught = exc
        raise
    finally:
        try:
            cleanup_error = _run_managed_sigint_deferred(finish_cleanup)
        except BaseException as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            if caught is None:
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            if not isinstance(cleanup_error, Exception):
                if hasattr(cleanup_error, "add_note"):
                    cleanup_error.add_note(f"managed carrier retirement also failed: {caught}")
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            if hasattr(caught, "add_note"):
                caught.add_note(f"managed carrier cleanup also failed: {cleanup_error}")


def retire_managed_carrier(
    carrier_reference,
    expected_owner,
    expected_manifest_hash,
    quarantine_name,
):
    """Retire one exact sealed carrier, retaining ambiguity for recovery."""
    with _MANAGED_CARRIER_RETIREMENT_LOCK:
        return _run_managed_sigint_deferred(
            lambda: _retire_managed_carrier_with_interrupts_deferred(
                carrier_reference,
                expected_owner,
                expected_manifest_hash,
                quarantine_name,
            )
        )


def retire_prelaunch_managed_carrier(
    carrier_reference,
    expected_owner,
    expected_manifest_hash,
):
    """Retire only an exact generation-zero carrier from before launch."""
    try:
        nonce = carrier_reference["nonce"]
    except (KeyError, TypeError):
        nonce = None
    quarantine_name = f".qobuz-retire-managed-beets-{nonce}" if isinstance(nonce, str) else ""
    with _MANAGED_CARRIER_RETIREMENT_LOCK:
        return _run_managed_sigint_deferred(
            lambda: _retire_managed_carrier_with_interrupts_deferred(
                carrier_reference,
                expected_owner,
                expected_manifest_hash,
                quarantine_name,
                prelaunch=True,
            )
        )


def _prefer_managed_cleanup_error(current, candidate):
    if candidate is None:
        return current
    if current is None or (isinstance(current, Exception) and not isinstance(candidate, Exception)):
        return candidate
    return current


def _run_managed_sigint_deferred(callback):
    """Run one raw-descriptor ownership handoff before delivering Ctrl-C."""
    return run_sigint_deferred(
        callback, unavailable=ManagedEvidenceUnavailable,
        detail="interrupt-safe managed completion access is unavailable")


class _ManagedEvidenceLifetime:
    """One-shot owner for every resource held by a restart proof."""

    def __init__(
        self,
        *,
        capture,
        database_anchor,
        database_exclusion,
        root_chain,
        parent_chain,
    ):
        self.capture = capture
        self.database_anchor = database_anchor
        self.database_exclusion = database_exclusion
        self.root_chain = root_chain
        self.parent_chain = parent_chain
        self.closed = False

    def close(self):
        if self.closed:
            return None
        error = None

        def remember(exc):
            nonlocal error
            error = _prefer_managed_cleanup_error(error, exc)

        exclusion = self.database_exclusion
        self.database_exclusion = None
        if exclusion is not None:
            try:
                remember(exclusion.release())
            except BaseException as exc:
                remember(exc)

        anchor = self.database_anchor
        self.database_anchor = None
        if anchor is not None:
            descriptor = anchor.get("descriptor")
            anchor["descriptor"] = None
            if descriptor is not None:
                try:
                    _close_managed_descriptor(descriptor)
                except BaseException as exc:
                    remember(exc)
            descriptors = tuple(reversed(anchor.get("parent_chain", ())))
            anchor["parent_chain"] = []
            for descriptor in descriptors:
                try:
                    _close_managed_descriptor(descriptor)
                except BaseException as exc:
                    remember(exc)

        carrier = self.capture.get("_file")
        self.capture["_file"] = None
        if carrier is not None:
            try:
                carrier.close()
            except BaseException as exc:
                remember(exc)

        descriptors = tuple(reversed(self.root_chain))
        self.root_chain.clear()
        for descriptor in descriptors:
            try:
                _close_managed_descriptor(descriptor)
            except BaseException as exc:
                remember(exc)

        descriptors = tuple(reversed(self.parent_chain))
        self.parent_chain.clear()
        for descriptor in descriptors:
            try:
                _close_managed_descriptor(descriptor)
            except BaseException as exc:
                remember(exc)

        # Ownership is retired before every potentially interruptible close.
        # A real POSIX close may have succeeded before delivering an async
        # exception, so retrying the same raw fd could close an unrelated,
        # subsequently reused descriptor.
        self.closed = True
        return error


def _finalize_managed_evidence_lifetime(lifetime):
    try:
        _run_managed_sigint_deferred(lifetime.close)
    except BaseException:
        pass


class ManagedEvidenceLease:
    """Keep restart evidence and its filesystem/database anchors live."""

    def __init__(
        self,
        *,
        carrier,
        capture,
        database_anchor,
        database_exclusion,
        root_chain,
        parent_chain,
    ):
        self._carrier = carrier
        self._capture = capture
        self._database_anchor = database_anchor
        self._database_exclusion = database_exclusion
        self._root_chain = root_chain
        self._parent_chain = parent_chain
        lifetime = _ManagedEvidenceLifetime(
            capture=capture,
            database_anchor=database_anchor,
            database_exclusion=database_exclusion,
            root_chain=root_chain,
            parent_chain=parent_chain,
        )
        self._lifetime = lifetime
        self._finalizer = weakref.finalize(self, _finalize_managed_evidence_lifetime, lifetime)
        self._closed = False
        self._closing = False
        self._failed = False
        self._evidence = None

    @property
    def evidence(self):
        if self._closed or self._closing or self._failed or self._evidence is None:
            raise ManagedEvidenceUnavailable("managed completion evidence is not available")
        return self._evidence

    def revalidate(self):
        """Repeat the full carrier, destination, database, and album proof."""
        if self._closed or self._closing or self._failed:
            raise ManagedEvidenceUnavailable("managed completion evidence lease is unavailable")
        try:
            return self._revalidate_once()
        except BaseException:
            self._failed = True
            raise

    def _revalidate_once(self):
        before = _managed_carrier_stat(self._capture["_file"])
        if before[4] != 1:
            raise ManagedEvidenceUnavailable("managed completion carrier has another public link")
        snapshot = _read_managed_ownership_evidence(
            self._capture,
            database_anchor=self._database_anchor,
        )
        if snapshot is None:
            raise ManagedEvidenceUnavailable(
                "managed completion evidence changed while it was read"
            )
        evidence = _managed_completion_from_snapshot(
            self._carrier,
            snapshot,
            database_anchor=self._database_anchor,
        )
        after = _managed_carrier_stat(self._capture["_file"])
        if before != after:
            raise ManagedEvidenceUnavailable(
                "managed completion evidence changed while it was verified"
            )
        if evidence is None:
            raise ManagedEvidenceUnavailable("managed completion evidence could not be verified")
        if self._evidence is not None and evidence != self._evidence:
            raise ManagedEvidenceUnavailable("managed completion evidence changed during recovery")
        self._evidence = evidence
        return evidence

    def close(self):
        if self._closed:
            return
        # Revoke authority before the potentially interruptible cleanup call.
        # The lifetime remains finalised if control leaves here.
        self._closing = True
        error = None
        try:
            error = _run_managed_sigint_deferred(self._lifetime.close)
        finally:
            self._closed = self._lifetime.closed
            if self._closed and self._finalizer.alive:
                self._finalizer.detach()
        if error is not None:
            raise error.with_traceback(error.__traceback__)


def _open_managed_evidence_lease_with_interrupts_deferred(carrier_reference, expected_owner):
    parent_chain = []
    root_chain = []
    carrier_file = None
    database_anchor = None
    exclusion = _SQLiteDatabaseExclusion()
    lease = None
    try:
        if type(expected_owner) is RecoveryOwner:
            expected_owner = {
                "operation_id": expected_owner.operation_id,
                "item_id": expected_owner.item_id,
            }
        expected_owner = normalise_recovery_owner(expected_owner)
        if (
            expected_owner is None
            or type(carrier_reference) is not dict
            or set(carrier_reference)
            != {
                "version",
                "path",
                "device",
                "inode",
                "parent_device",
                "parent_inode",
                "nonce",
                "owner",
            }
            or type(carrier_reference.get("version")) is not int
            or carrier_reference["version"] not in {1, 2}
            or any(
                type(carrier_reference.get(field)) is not int or carrier_reference[field] < 0
                for field in (
                    "device",
                    "inode",
                    "parent_device",
                    "parent_inode",
                )
            )
            or not isinstance(carrier_reference.get("nonce"), str)
            or len(carrier_reference["nonce"]) != 64
            or any(value not in "0123456789abcdef" for value in carrier_reference["nonce"])
            or normalise_recovery_owner(carrier_reference.get("owner")) != expected_owner
        ):
            raise ManagedEvidenceUnavailable("managed completion carrier reference is malformed")

        raw_path = carrier_reference.get("path")
        if (
            not isinstance(raw_path, str)
            or not os.path.isabs(raw_path)
            or os.path.abspath(raw_path) != raw_path
        ):
            raise ManagedEvidenceUnavailable("managed completion carrier path is malformed")
        carrier_path = Path(raw_path)
        database = Path(os.path.abspath(os.fspath(cfg.BEETS_DB_PATH)))
        parent = database.parent
        if (
            carrier_path.parent != parent
            or not carrier_path.name.startswith(".qobuz-managed-beets-")
            or not carrier_path.name.endswith(".jsonl")
            or len(carrier_path.name) <= len(".qobuz-managed-beets-.jsonl")
        ):
            raise ManagedEvidenceUnavailable(
                "managed completion carrier is outside its private namespace"
            )
        if (
            carrier_reference["version"] == 2
            and carrier_path.name != f".qobuz-managed-beets-{carrier_reference['nonce']}.jsonl"
        ):
            raise ManagedEvidenceUnavailable("managed completion carrier name is malformed")

        parent_chain, parent_names = _open_absolute_merge_chain(parent)
        parent_stat = os.fstat(parent_chain[-1])
        if (
            int(parent_stat.st_dev) != carrier_reference["parent_device"]
            or int(parent_stat.st_ino) != carrier_reference["parent_inode"]
        ):
            raise ManagedEvidenceUnavailable("managed completion carrier parent changed")
        carrier_file = _open_managed_carrier_file(parent_chain[-1], carrier_path.name)
        held_before = os.fstat(carrier_file.fileno())
        if (
            not stat.S_ISREG(held_before.st_mode)
            or held_before.st_uid != os.geteuid()
            or stat.S_IMODE(held_before.st_mode) != 0o600
            or held_before.st_nlink != 1
            or int(held_before.st_dev) != carrier_reference["device"]
            or int(held_before.st_ino) != carrier_reference["inode"]
            or held_before.st_size <= 0
            or held_before.st_size > _OWNERSHIP_MAX_BYTES
            or not _merge_name_matches(
                parent_chain[-1],
                carrier_path.name,
                carrier_file.fileno(),
                regular=True,
            )
        ):
            raise ManagedEvidenceUnavailable("managed completion carrier identity changed")
        raw = os.pread(carrier_file.fileno(), held_before.st_size + 1, 0)
        held_after = os.fstat(carrier_file.fileno())
        if (
            _ownership_identity(held_before) != _ownership_identity(held_after)
            or len(raw) != held_before.st_size
            or not raw.endswith(b"\n")
        ):
            raise ManagedEvidenceUnavailable(
                "managed completion carrier changed while it was opened"
            )
        lines = raw.splitlines()
        if not lines or len(lines) > 10_000:
            raise ManagedEvidenceUnavailable("managed completion carrier history is malformed")
        first = decode_recovery_json(lines[0], max_bytes=_OWNERSHIP_MAX_BYTES)
        canonical_first = json.dumps(
            first,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if (
            lines[0] != canonical_first
            or not _valid_managed_snapshot(first)
            or first["generation"] != 0
            or first["previous_hash"] is not None
            or first["sealed"] is not False
            or first["mappings"]
            or first["cleanup_directories"]
            or first["nonce"] != carrier_reference["nonce"]
            or first["owner"] != expected_owner
        ):
            raise ManagedEvidenceUnavailable("managed completion carrier origin is malformed")

        configured_root = os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
        if not isinstance(configured_root, str) or first["root"] != configured_root:
            raise ManagedEvidenceUnavailable("managed completion library root changed")
        root_chain, root_names = _open_absolute_merge_chain(configured_root)
        root_stat = os.fstat(root_chain[-1])
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or int(root_stat.st_dev) != first["root_identity"]["device"]
            or int(root_stat.st_ino) != first["root_identity"]["inode"]
        ):
            raise ManagedEvidenceUnavailable("managed completion library identity changed")

        capture = {
            "_file": carrier_file,
            "path": carrier_path,
            "device": carrier_reference["device"],
            "inode": carrier_reference["inode"],
            "parent": str(parent),
            "parent_device": carrier_reference["parent_device"],
            "parent_inode": carrier_reference["parent_inode"],
            "nonce": carrier_reference["nonce"],
            "owner": dict(expected_owner),
            "root": configured_root,
            "root_identity": first["root_identity"],
            "intent": first["intent"],
            "_parent_chain": parent_chain,
            "_parent_names": parent_names,
            "_root_chain": root_chain,
            "_root_names": root_names,
        }
        if not _managed_carrier_namespace_matches(capture) or not _managed_root_namespace_matches(
            capture
        ):
            raise ManagedEvidenceUnavailable("managed completion namespace changed")

        database_anchor = _open_beets_database_anchor()
        if database_anchor is None or database_anchor.get("descriptor") is None:
            raise ManagedEvidenceUnavailable("managed completion database is unavailable")
        exclusion.acquire(database_anchor["parent_chain"][-1])
        _preflight_beets_database_anchor(database_anchor)
        lease = ManagedEvidenceLease(
            carrier={
                **carrier_reference,
                "owner": dict(expected_owner),
            },
            capture=capture,
            database_anchor=database_anchor,
            database_exclusion=exclusion,
            root_chain=root_chain,
            parent_chain=parent_chain,
        )
        lease.revalidate()
        return lease
    except BaseException as exc:
        cleanup_error = None
        if lease is not None:
            try:
                lease.close()
            except BaseException as close_exc:
                cleanup_error = close_exc
        else:
            lifetime = _ManagedEvidenceLifetime(
                capture={"_file": carrier_file},
                database_anchor=database_anchor,
                database_exclusion=exclusion,
                root_chain=root_chain,
                parent_chain=parent_chain,
            )
            cleanup_error = lifetime.close()
        if cleanup_error is not None and hasattr(exc, "add_note"):
            exc.add_note(f"managed evidence cleanup also failed: {cleanup_error}")
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            if hasattr(cleanup_error, "add_note"):
                cleanup_error.add_note(f"managed evidence opening also failed: {exc}")
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        if isinstance(exc, ManagedEvidenceUnavailable):
            raise
        if isinstance(exc, (OSError, sqlite3.Error, TypeError, ValueError)):
            raise ManagedEvidenceUnavailable(
                "managed completion evidence could not be reopened"
            ) from None
        raise


def _open_managed_evidence_lease(carrier_reference, expected_owner):
    """Install the lease owner before a pending Ctrl-C can take effect."""

    def open_lease():
        return _open_managed_evidence_lease_with_interrupts_deferred(
            carrier_reference, expected_owner
        )

    return _run_managed_sigint_deferred(open_lease)


@contextmanager
def reopen_managed_evidence(carrier_reference, expected_owner):
    """Hold and repeatedly validate one persisted managed import."""
    lease = _open_managed_evidence_lease(carrier_reference, expected_owner)
    caught = None
    try:
        yield lease
    except BaseException as exc:
        caught = exc
        raise
    finally:
        try:
            lease.close()
        except BaseException as exc:
            if caught is None:
                raise
            if not isinstance(exc, Exception):
                if hasattr(exc, "add_note"):
                    exc.add_note(f"managed evidence body also failed: {caught}")
                raise
            if hasattr(caught, "add_note"):
                caught.add_note(f"managed evidence cleanup also failed: {exc}")


def _managed_generation_zero_matches(capture):
    try:
        manifest = capture["_file"]
        descriptor = manifest.fileno()
        held = os.fstat(descriptor)
        if not _managed_carrier_namespace_matches(capture) or not _managed_root_namespace_matches(
            capture
        ):
            return False
        raw = os.pread(descriptor, held.st_size + 1, 0)
        lines = raw.splitlines()
        snapshot = decode_recovery_json(lines[0], max_bytes=_OWNERSHIP_MAX_BYTES)
        return (
            len(lines) == 1
            and raw.endswith(b"\n")
            and len(raw) == held.st_size
            and stat.S_ISREG(held.st_mode)
            and held.st_uid == os.geteuid()
            and stat.S_IMODE(held.st_mode) == 0o600
            and int(held.st_dev) == capture["device"]
            and int(held.st_ino) == capture["inode"]
            and _valid_managed_snapshot(snapshot)
            and snapshot["version"] == _MANAGED_SNAPSHOT_VERSION
            and snapshot["generation"] == 0
            and snapshot["previous_hash"] is None
            and snapshot["sealed"] is False
            and not snapshot["mappings"]
            and not snapshot["cleanup_directories"]
            and snapshot["nonce"] == capture["nonce"]
            and snapshot["owner"] == capture["owner"]
            and snapshot["root"] == capture["root"]
            and snapshot["root_identity"] == capture["root_identity"]
            and snapshot["intent"] == capture["intent"]
            and _managed_carrier_namespace_matches(capture)
            and _managed_root_namespace_matches(capture)
        )
    except (IndexError, KeyError, OSError, TypeError, ValueError):
        return False


def _managed_launch_boundary_matches(capture):
    try:
        manifest = capture["_file"]
        descriptor = manifest.fileno()
        held = os.fstat(descriptor)
        if not _managed_carrier_namespace_matches(capture) or not _managed_root_namespace_matches(
            capture
        ):
            return False
        raw = os.pread(descriptor, held.st_size + 1, 0)
        lines = raw.splitlines()
        if (
            len(lines) != 2
            or not raw.endswith(b"\n")
            or len(raw) != held.st_size
            or not stat.S_ISREG(held.st_mode)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != 0o600
            or int(held.st_dev) != capture["device"]
            or int(held.st_ino) != capture["inode"]
        ):
            return False
        origin, launch = (
            decode_recovery_json(line, max_bytes=_OWNERSHIP_MAX_BYTES) for line in lines
        )
        if not all(_valid_managed_snapshot(value) for value in (origin, launch)):
            return False
        expected_launch = {
            **origin,
            "generation": 1,
            "previous_hash": origin["hash"],
        }
        expected_launch["hash"] = _managed_payload_hash(expected_launch)
        return (
            origin["version"] == _MANAGED_SNAPSHOT_VERSION
            and origin["generation"] == 0
            and origin["previous_hash"] is None
            and origin["sealed"] is False
            and not origin["mappings"]
            and not origin["cleanup_directories"]
            and launch == expected_launch
            and launch["hash"] == capture["last_hash"]
            and origin["nonce"] == capture["nonce"]
            and origin["owner"] == capture["owner"]
            and origin["root"] == capture["root"]
            and origin["root_identity"] == capture["root_identity"]
            and origin["intent"] == capture["intent"]
            and _managed_carrier_namespace_matches(capture)
            and _managed_root_namespace_matches(capture)
        )
    except (IndexError, KeyError, OSError, TypeError, ValueError):
        return False


def _append_managed_launch_boundary(capture):
    if not _managed_generation_zero_matches(capture):
        raise OSError("managed Beets origin changed before launch")
    launch = {
        "version": _MANAGED_SNAPSHOT_VERSION,
        "generation": 1,
        "previous_hash": capture["last_hash"],
        "nonce": capture["nonce"],
        "owner": dict(capture["owner"]),
        "root": capture["root"],
        "root_identity": dict(capture["root_identity"]),
        "sealed": False,
        "intent": [dict(record) for record in capture["intent"]],
        "mappings": [],
        "cleanup_directories": [],
    }
    capture["last_hash"] = _write_managed_snapshot(capture["_file"].fileno(), launch)
    if not _managed_launch_boundary_matches(capture):
        raise OSError("managed Beets launch boundary could not be verified")


def _open_ownership_parent(root_fd, parts):
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = _open_ownership_dir(part, dir_fd=current)
            closing = current
            current = child
            os.close(closing)
        return current
    except Exception:
        os.close(current)
        raise


def _read_ownership_marker(directory_fd, expected_token, marker=None):
    if marker is not None and not _valid_ownership_identity(marker):
        return False
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return False
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        marker_fd = os.open(_OWNERSHIP_MARKER, flags, dir_fd=directory_fd)
        try:
            marker_stat = os.fstat(marker_fd)
            if not stat.S_ISREG(marker_stat.st_mode) or (
                marker is not None and _ownership_identity(marker_stat) != marker
            ):
                return False
            raw = os.pread(marker_fd, 129, 0)
            return raw == (expected_token + "\n").encode("ascii")
        finally:
            os.close(marker_fd)
    except (OSError, TypeError, ValueError):
        return False


def _read_exact_marker(directory_fd, marker, expected_token):
    return _read_ownership_marker(directory_fd, expected_token, marker)


def _remove_exact_ownership_marker(directory_fd, marker, expected_token):
    """Remove only the recorded marker from an already-proved directory."""
    if not _valid_ownership_identity(marker):
        return False
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return False
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    marker_fd = None
    try:
        marker_fd = os.open(_OWNERSHIP_MARKER, flags, dir_fd=directory_fd)
        marker_stat = os.fstat(marker_fd)
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or _ownership_identity(marker_stat) != marker
            or os.pread(marker_fd, 129, 0) != (expected_token + "\n").encode("ascii")
        ):
            return False
        named = os.stat(
            _OWNERSHIP_MARKER,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if _ownership_identity(named) != marker:
            return False
        os.unlink(_OWNERSHIP_MARKER, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if marker_fd is not None:
            try:
                os.close(marker_fd)
            except OSError:
                pass


def _reclaim_ownership_directories(root_fd, records, nonce):
    """Remove only exact, empty directories from one failed ownership run."""
    if not isinstance(records, list) or len(records) > 64:
        return 0
    prepared = []
    for record in records:
        if not isinstance(record, dict):
            continue
        relative = record.get("relative")
        parts = _ownership_relative_parts(relative)
        temporary = record.get("temporary_relative")
        temporary_parts = _ownership_relative_parts(temporary)
        identity = record.get("directory")
        marker = record.get("marker")
        if (
            not parts
            or not temporary_parts
            or parts[:-1] != temporary_parts[:-1]
            or not temporary_parts[-1].startswith(b".qobuz-ownership-")
            or not _valid_ownership_identity(identity)
            or (marker is not None and not _valid_ownership_identity(marker))
        ):
            continue
        expected_token = _ownership_marker_token(nonce, relative)
        cleanup_names = tuple(
            os.fsencode(f".qobuz-ownership-cleanup-{phase}-{expected_token}")
            for phase in ("a", "b")
        )
        prepared.append(
            (len(parts), parts, temporary_parts, identity, marker, expected_token, cleanup_names)
        )

    removed = 0
    for _, parts, temporary_parts, identity, marker, token, cleanup_names in sorted(
        prepared, key=lambda value: value[0], reverse=True
    ):
        parent_fd = None
        directory_fd = None
        quarantine_fd = None
        quarantine = None
        original_name = None
        moved_to_quarantine = False
        try:
            parent_fd = _open_ownership_parent(root_fd, parts)
            for candidate in (parts[-1], temporary_parts[-1], *cleanup_names):
                try:
                    candidate_fd = _open_ownership_dir(candidate, dir_fd=parent_fd)
                except OSError:
                    continue
                candidate_stat = os.fstat(candidate_fd)
                if (
                    int(candidate_stat.st_dev) == identity["device"]
                    and int(candidate_stat.st_ino) == identity["inode"]
                ):
                    directory_fd = candidate_fd
                    original_name = candidate
                    break
                os.close(candidate_fd)
            if directory_fd is None:
                continue

            entries = os.listdir(directory_fd)
            marker_present = marker is not None
            if marker is None:
                if not entries and _ownership_identity(os.fstat(directory_fd)) == identity:
                    marker_present = False
                elif entries == [_OWNERSHIP_MARKER] and _read_ownership_marker(directory_fd, token):
                    # The process can be killed after fsyncing its nonce marker
                    # but before appending that marker's identity to the shared
                    # receipt. The marker capability plus the held directory's
                    # device/inode closes that durable transition window.
                    marker_present = True
                else:
                    continue
            elif not entries:
                # The plug-in durably unlinks and fsyncs its marker before it
                # can append the marker=None directory identity. A kill in
                # that narrow window leaves the old marker receipt naming this
                # exact dev/inode, but the directory is already empty.
                marker_present = False
            elif entries != [_OWNERSHIP_MARKER] or not _read_exact_marker(
                directory_fd, marker, token
            ):
                continue

            # Both private phase names are derivable from the durable receipt.
            # Always rotate to the other one before rmdir: after a process
            # death the next attempt can find the exact directory and still
            # gets a fresh no-replace name-swap boundary.
            quarantine = cleanup_names[1] if original_name == cleanup_names[0] else cleanup_names[0]
            _rename_ownership_noreplace(parent_fd, original_name, parent_fd, quarantine)
            moved_to_quarantine = True
            quarantine_fd = _open_ownership_dir(quarantine, dir_fd=parent_fd)
            quarantine_stat = os.fstat(quarantine_fd)
            if (
                int(quarantine_stat.st_dev) != identity["device"]
                or int(quarantine_stat.st_ino) != identity["inode"]
            ):
                raise OSError("ownership directory changed during cleanup")

            if marker_present:
                if os.listdir(quarantine_fd) != [_OWNERSHIP_MARKER] or not _read_ownership_marker(
                    quarantine_fd, token, marker
                ):
                    raise OSError("ownership marker changed during cleanup")
                marker_identity = marker
                if marker_identity is None:
                    marker_stat = os.stat(
                        _OWNERSHIP_MARKER,
                        dir_fd=quarantine_fd,
                        follow_symlinks=False,
                    )
                    marker_identity = _ownership_identity(marker_stat)
                if not _remove_exact_ownership_marker(quarantine_fd, marker_identity, token):
                    raise OSError("ownership marker changed during cleanup")
            if os.listdir(quarantine_fd):
                raise OSError("ownership directory is not empty")
            os.rmdir(quarantine, dir_fd=parent_fd)
            quarantine = None
            os.fsync(parent_fd)
            removed += 1
        except (OSError, TypeError, ValueError):
            if (
                moved_to_quarantine
                and quarantine is not None
                and parent_fd is not None
                and original_name
            ):
                try:
                    _rename_ownership_noreplace(parent_fd, quarantine, parent_fd, original_name)
                    quarantine = None
                except OSError:
                    pass
        finally:
            for descriptor in (quarantine_fd, directory_fd, parent_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
    return removed


def _strip_ownership_markers(root_fd, records, nonce):
    """Strip exact run markers while preserving every other directory entry."""
    if not isinstance(records, list) or len(records) > 64:
        return 0
    prepared = []
    for record in records:
        if not isinstance(record, dict):
            continue
        relative = record.get("relative")
        parts = _ownership_relative_parts(relative)
        temporary_parts = _ownership_relative_parts(record.get("temporary_relative"))
        identity = record.get("directory")
        marker = record.get("marker")
        if (
            not parts
            or not temporary_parts
            or parts[:-1] != temporary_parts[:-1]
            or not temporary_parts[-1].startswith(b".qobuz-ownership-")
            or not _valid_ownership_identity(identity)
            or not _valid_ownership_identity(marker)
        ):
            continue
        prepared.append(
            (
                len(parts),
                parts,
                temporary_parts,
                identity,
                marker,
                _ownership_marker_token(nonce, relative),
            )
        )

    stripped = 0
    for _, parts, temporary_parts, identity, marker, token in sorted(
        prepared, key=lambda value: value[0], reverse=True
    ):
        parent_fd = None
        directory_fd = None
        try:
            parent_fd = _open_ownership_parent(root_fd, parts)
            for candidate in (parts[-1], temporary_parts[-1]):
                try:
                    candidate_fd = _open_ownership_dir(candidate, dir_fd=parent_fd)
                except OSError:
                    continue
                candidate_stat = os.fstat(candidate_fd)
                if (
                    int(candidate_stat.st_dev) == identity["device"]
                    and int(candidate_stat.st_ino) == identity["inode"]
                ):
                    directory_fd = candidate_fd
                    break
                os.close(candidate_fd)
            if directory_fd is not None and _remove_exact_ownership_marker(
                directory_fd, marker, token
            ):
                stripped += 1
        except (OSError, TypeError, ValueError):
            pass
        finally:
            for descriptor in (directory_fd, parent_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
    return stripped


_SUPPORTED_BEETS_VERSION = "2.12.0"
_BEETS_CONFIG_PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class _BeetsRuntime:
    python: str
    link_identity: tuple
    target_identity: tuple


def _beets_runtime_identity(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_uid),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _checked_beets_runtime(value):
    try:
        raw = os.fspath(value)
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        if not isinstance(raw, str) or not os.path.isabs(raw) or "\x00" in raw:
            return None
        python = os.path.normpath(raw)
        link = os.lstat(python)
        target = os.stat(python)
        if (
            not (stat.S_ISREG(link.st_mode) or stat.S_ISLNK(link.st_mode))
            or not stat.S_ISREG(target.st_mode)
            or target.st_mode & 0o111 == 0
            or not os.access(python, os.X_OK)
        ):
            return None
        return _BeetsRuntime(
            python,
            _beets_runtime_identity(link),
            _beets_runtime_identity(target),
        )
    except (OSError, TypeError, ValueError):
        return None


def _beets_runtime_matches(runtime):
    if not isinstance(runtime, _BeetsRuntime):
        return False
    try:
        link = os.lstat(runtime.python)
        target = os.stat(runtime.python)
        return (
            _beets_runtime_identity(link) == runtime.link_identity
            and _beets_runtime_identity(target) == runtime.target_identity
            and stat.S_ISREG(target.st_mode)
            and os.access(runtime.python, os.X_OK)
        )
    except OSError:
        return False


def _append_managed_launch_boundary_for_runtime(capture):
    runtime = capture.get("_beets_runtime")
    if not _beets_runtime_matches(runtime):
        raise OSError("Beets runtime changed before launch")
    _append_managed_launch_boundary(capture)


def _require_beets_runtime(runtime):
    if not _beets_runtime_matches(runtime):
        raise OSError("Beets runtime changed before launch")


def _beets_python_from_launcher():
    configured = getattr(cfg, "BEETS_PYTHON", "")
    if configured:
        return os.fspath(configured)
    launcher = shutil.which("beet")
    if not launcher:
        return None
    descriptor = None
    try:
        script = os.path.realpath(launcher)
        descriptor = os.open(
            script,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode):
            return None
        raw = os.pread(descriptor, 4097, 0)
        first, separator, _remainder = raw.partition(b"\n")
        if not separator or len(first) > 4096 or not first.startswith(b"#!"):
            return None
        tokens = shlex.split(first[2:].decode("utf-8", "strict"))
    except (OSError, TypeError, UnicodeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not tokens:
        return None
    if os.path.basename(tokens[0]) == "env":
        values = tokens[1:]
        if values[:1] == ["-S"]:
            values = values[1:]
        if len(values) != 1:
            return None
        return shutil.which(values[0])
    if len(tokens) != 1 or not os.path.isabs(tokens[0]):
        return None
    return tokens[0]


def _resolve_beets_runtime():
    return _checked_beets_runtime(_beets_python_from_launcher())


def beets_runtime_path():
    """Return the verified Python launcher configured for Beets, if any."""
    runtime = _resolve_beets_runtime()
    if runtime is None or _configured_beets_plugins(runtime) is None:
        return None
    return runtime.python


def _managed_beets_entrypoint():
    return Path(os.path.abspath(__file__)).with_name("managed_beets.py")


def _configured_beets_plugins(runtime=None):
    """Inspect config inside Beets' own environment without loading plugins."""
    runtime = runtime or _resolve_beets_runtime()
    if runtime is None or not _beets_runtime_matches(runtime):
        return None
    config_dir = os.path.abspath(os.fspath(cfg.BEETS_CONFIG_DIR))
    environment = {**os.environ, "BEETSDIR": config_dir}
    for variable in _OWNERSHIP_ENV_KEYS:
        environment.pop(variable, None)
    try:
        completed = subprocess.run(
            [
                runtime.python,
                "-I",
                str(_managed_beets_entrypoint()),
                "--inspect-config",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=30,
            check=False,
        )
        output = completed.stdout
        if (
            completed.returncode != 0
            or not isinstance(output, bytes)
            or len(output) > 64 * 1024
            or len(output.splitlines()) != 1
        ):
            return None
        payload = json.loads(output)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "version",
            "beets_version",
            "python",
            "plugins",
            "plugin_paths",
            "disabled",
            "musicbrainz_enabled",
        }
        or payload["version"] != _BEETS_CONFIG_PROTOCOL_VERSION
        or payload["beets_version"] != _SUPPORTED_BEETS_VERSION
        or not isinstance(payload["python"], list)
        or len(payload["python"]) != 2
        or any(type(value) is not int for value in payload["python"])
        or tuple(payload["python"]) < (3, 12)
        or not (
            payload["musicbrainz_enabled"] is None or type(payload["musicbrainz_enabled"]) is bool
        )
    ):
        return None
    limits = {
        "plugins": (256, 256),
        "plugin_paths": (256, 4096),
        "disabled": (256, 256),
    }
    for key, (count, length) in limits.items():
        values = payload[key]
        if (
            not isinstance(values, list)
            or len(values) > count
            or any(not isinstance(value, str) or len(value) > length for value in values)
        ):
            return None
    return {
        "plugins": payload["plugins"],
        "plugin_paths": payload["plugin_paths"],
        "disabled": payload["disabled"],
        "musicbrainz_enabled": payload["musicbrainz_enabled"],
    }


def _build_import_override_yaml(plugin_config=None, *, ownership_enabled=False):
    """The beets config override the import runs with, as a YAML string. Forces
    the keys the import contract depends on: library/directory, non-
    interactive, non-incremental, autotagging and tag writes off, and move on.
    It lets
    everything else fall through to the user's config.yaml.
    """
    import re as _re

    database_path = os.path.abspath(os.fspath(cfg.BEETS_DB_PATH))
    music_root = os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
    override_yaml = (
        f"library: {_yaml_sq(database_path)}\n"
        f"directory: {_yaml_sq(music_root)}\n"
        "import:\n"
        "  quiet: yes\n"
        "  incremental: no\n"
        "  autotag: no\n"
        "  write: no\n"
        "  move: yes\n"
        # Pin duplicate_action: merge for OUR importer (separate from the
        # user's own `beet` usage, which still reads their config): a
        # `duplicate_action: remove` in config.yaml would have beets delete
        # the pre-existing album's files off disk on a gap-fill collision.
        "  duplicate_action: merge\n"
    )
    # Path templates are deployer-supplied and can contain single quotes
    # (e.g. `$albumartist's stuff/$album`); _yaml_sq keeps the scalar safe.
    _paths = []
    if cfg.BEETS_PATH_DEFAULT:
        _paths.append(f"  default: {_yaml_sq(cfg.BEETS_PATH_DEFAULT)}\n")
    if cfg.BEETS_PATH_SINGLETON:
        _paths.append(f"  singleton: {_yaml_sq(cfg.BEETS_PATH_SINGLETON)}\n")
    if cfg.BEETS_PATH_COMP:
        _paths.append(f"  comp: {_yaml_sq(cfg.BEETS_PATH_COMP)}\n")
    if _paths:
        override_yaml += "paths:\n" + "".join(_paths)

    # fetchart saves a cover file; embedart embeds it into the tracks. ARTWORK
    # picks which. Start from the user's plugin override (or the seeded
    # `fetchart` default) and add what the art mode needs.
    preserve_effective_plugins = plugin_config is not None and not cfg.BEETS_PLUGINS
    if preserve_effective_plugins:
        _plugins = list(plugin_config["plugins"])
    else:
        _plugins = list(cfg.BEETS_PLUGINS) if cfg.BEETS_PLUGINS else ["fetchart"]
    _art = getattr(cfg, "ARTWORK", "sidecar")
    # fetchart saves cover.jpg for sidecar too, so a custom BEETS_PLUGINS list
    # that omits it (and replaces config.yaml's) mustn't silently turn art off.
    if (
        not preserve_effective_plugins
        and _art in ("sidecar", "embed", "both")
        and "fetchart" not in _plugins
    ):
        _plugins.append("fetchart")
    if _art in ("embed", "both"):
        for _p in ("fetchart", "embedart"):
            if _p not in _plugins:
                _plugins.append(_p)
        # auto: embed after fetchart fetches the cover. remove_art_file drops
        # the on-disk cover for embed-only; both keeps it.
        override_yaml += (
            f"embedart:\n  auto: yes\n  remove_art_file: {'yes' if _art == 'embed' else 'no'}\n"
        )
    plugin_paths = [str(Path(__file__).parent / "beets_plugins")]
    if plugin_config is not None:
        plugin_paths.extend(plugin_config["plugin_paths"])
    unique_paths = list(dict.fromkeys(plugin_paths))
    override_yaml += "pluginpath:\n" + "".join(f"  - {_yaml_sq(path)}\n" for path in unique_paths)
    if plugin_config is not None:
        if plugin_config["musicbrainz_enabled"] is True and "musicbrainz" not in _plugins:
            _plugins.append("musicbrainz")
        disabled = set(plugin_config["disabled"])
    else:
        disabled = set()
    disabled.discard(_ART_GUARD_PLUGIN)
    if ownership_enabled:
        disabled.discard(_OWNERSHIP_PLUGIN)
    _plugins = [plugin for plugin in _plugins if plugin not in disabled]
    override_yaml += (
        "disabled_plugins: [" + ", ".join(_yaml_sq(plugin) for plugin in sorted(disabled)) + "]\n"
    )

    # Emitting `plugins:` replaces the config.yaml list, so re-add inline;
    # the seeded path template's `multidisc` field comes from it, and dropping
    # it would flatten multi-disc albums into one folder. The art guard is
    # always loaded after fetchart; the one-run ownership hook, when requested,
    # remains last so its manifest cannot be pre-empted by another plugin.
    if not preserve_effective_plugins and "inline" not in _plugins:
        _plugins.append("inline")
    _plugins = [
        plugin for plugin in _plugins if plugin not in {_ART_GUARD_PLUGIN, _OWNERSHIP_PLUGIN}
    ]
    _plugins.append(_ART_GUARD_PLUGIN)
    if ownership_enabled:
        _plugins.append(_OWNERSHIP_PLUGIN)
    # Plugin names must be plain identifiers. Anything else would break the
    # YAML structure (and likely isn't a real beets plugin anyway).
    safe_plugins = [p for p in _plugins if _re.match(r"^[A-Za-z0-9_]+$", p)]
    if safe_plugins:
        override_yaml += f"plugins: [{', '.join(safe_plugins)}]\n"
    return override_yaml


def _prepare_for_beets_run(roots=None, ownership_out=None, source_files_out=None):
    """Common setup shared by the whole-staging and per-album entry points:
    resolve Beets' own runtime, prep tags on the staged FLACs, and write the
    one-shot override yaml. Returns ``(override_path, cleanup_fn, runtime)``
    or three ``None`` values if a precondition failed."""
    runtime = _resolve_beets_runtime()
    if runtime is None:
        log.info(fmt(C.RED, "  ✗  A supported Beets Python runtime could not be found."))
        log.info(
            fmt(
                C.GRAY,
                "     Install beets 2.12.0 or set BEETS_PYTHON to its Python executable.",
            )
        )
        return None, None, None

    config_dir = Path(os.path.abspath(os.fspath(cfg.BEETS_CONFIG_DIR)))
    user_config = config_dir / "config.yaml"
    if not user_config.exists():
        log.info(fmt(C.RED, f"  ✗  beets config not found at {user_config}."))
        log.info(
            fmt(
                C.GRAY,
                "     The container seeds a default on first start; check that the config volume is mounted writably.",
            )
        )
        return None, None, None

    plugin_config = _configured_beets_plugins(runtime)
    if plugin_config is None:
        log.info(
            fmt(
                C.RED,
                "  ✗  The configured Python does not provide the supported Beets 2.12.0 runtime.",
            )
        )
        return None, None, None

    _prepare_staging_tags(roots=roots)

    if source_files_out is not None:
        source_files_out.clear()
        explicit_roots = bool(roots)
        scan_roots = roots if roots else [cfg.STAGING_DIR]
        try:
            candidates = []
            for root in scan_roots:
                candidates.extend(
                    path
                    for path in root.rglob("*")
                    if path.is_file() and (explicit_roots or not _under_retry_dir(path))
                )
            for path in candidates:
                receipt = staging_mod.capture_file(path)
                if receipt is not None:
                    source_files_out.append(receipt)
        except OSError:
            source_files_out.clear()

    ownership_enabled = ownership_out is not None and plugin_config is not None
    if ownership_out is not None:
        ownership_out.clear()

    override_path = None
    ownership_file = None
    try:
        config_parent = Path(os.path.abspath(os.fspath(cfg.BEETS_DB_PATH.parent)))
        config_parent.mkdir(parents=True, exist_ok=True)
        if ownership_enabled:
            ownership_file = tempfile.TemporaryFile(
                mode="w+b",
                prefix=".qobuz-ownership-",
                dir=config_parent,
            )
            ownership_out.update(
                {
                    "_file": ownership_file,
                    "fd": ownership_file.fileno(),
                    "nonce": secrets.token_hex(32),
                }
            )
        override_fd, override_name = tempfile.mkstemp(
            prefix=".beets-import-",
            suffix=".yaml",
            dir=config_parent,
        )
        override_path = Path(override_name)
        with os.fdopen(override_fd, "w", encoding="utf-8") as handle:
            handle.write(
                _build_import_override_yaml(
                    plugin_config,
                    ownership_enabled=ownership_enabled,
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Couldn't write beets override config: {e}.{errors.oserr_hint(e)}"))
        if override_path is not None:
            try:
                override_path.unlink(missing_ok=True)
            except OSError:
                pass
        if ownership_file is not None:
            ownership_file.close()
        if ownership_out is not None:
            ownership_out.clear()
        return None, None, None

    def _clean():
        if override_path:
            try:
                override_path.unlink(missing_ok=True)
            except OSError:
                pass
        if ownership_file is not None:
            ownership_file.close()

    return override_path, _clean, runtime


class _ManagedBeetsRunLifetime:
    """One-shot owner for descriptors held across one managed Beets run."""

    def __init__(self, capture):
        self.capture = capture
        self.closed = False

    def close(self):
        if self.closed:
            return
        cleanup_error = None

        def remember(exc):
            nonlocal cleanup_error
            cleanup_error = _prefer_managed_cleanup_error(cleanup_error, exc)

        override_descriptor = self.capture.get("_override_fd")
        self.capture["_override_fd"] = None
        if override_descriptor is not None:
            try:
                os.close(override_descriptor)
            except BaseException as exc:
                remember(exc)

        result_bearing = self.capture.get("result") is not None
        namespace_checked = self.capture.get("_cleanup_namespace_checked") is True
        namespace_error = self.capture.get("_cleanup_namespace_error")
        if result_bearing and not namespace_checked:
            if not _managed_root_namespace_matches(self.capture):
                namespace_error = OSError("managed beets library root changed before cleanup")

        root_descriptors = tuple(reversed(self.capture.get("_root_chain", ())))
        self.capture["_root_chain"] = ()
        for descriptor in root_descriptors:
            try:
                os.close(descriptor)
            except BaseException as exc:
                remember(exc)

        if result_bearing and not namespace_checked:
            if not _managed_carrier_namespace_matches(self.capture):
                namespace_error = namespace_error or OSError(
                    "managed beets carrier changed before cleanup"
                )
            self.capture["_cleanup_namespace_error"] = namespace_error
            self.capture["_cleanup_namespace_checked"] = True
        if namespace_error is not None:
            remember(namespace_error)

        carrier = self.capture.get("_file")
        self.capture["_file"] = None
        if carrier is not None:
            try:
                carrier.close()
            except BaseException as exc:
                remember(exc)

        parent_descriptors = tuple(reversed(self.capture.get("_parent_chain", ())))
        self.capture["_parent_chain"] = ()
        for descriptor in parent_descriptors:
            try:
                os.close(descriptor)
            except BaseException as exc:
                remember(exc)

        self.closed = True
        if cleanup_error is not None:
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)


def _finalize_managed_beets_run_lifetime(lifetime):
    try:
        _run_managed_sigint_deferred(lifetime.close)
    except BaseException:
        pass


class _ManagedBeetsRunCleanup:
    """Callable cleanup whose lifetime survives an interrupted handoff."""

    def __init__(self, lifetime):
        self._lifetime = lifetime
        self._finalizer = weakref.finalize(self, _finalize_managed_beets_run_lifetime, lifetime)

    def __call__(self):
        try:
            return _run_managed_sigint_deferred(self._lifetime.close)
        finally:
            if self._lifetime.closed:
                self._finalizer.detach()


def _prepare_managed_beets_run(roots, bindings, owner, *, on_reservation):
    cfg.validate_storage_roots()
    runtime = _resolve_beets_runtime()
    if runtime is None:
        return None
    config_dir = Path(os.path.abspath(os.fspath(cfg.BEETS_CONFIG_DIR)))
    user_config = config_dir / "config.yaml"
    if not user_config.exists():
        return None
    plugin_config = _configured_beets_plugins(runtime)
    if plugin_config is None:
        return None

    before = _normalise_managed_bindings(bindings, roots)
    intent = _prepare_staging_tags(
        roots=roots,
        managed_bindings=before,
    )
    intent = _normalise_managed_bindings(intent, roots)
    config_parent = Path(os.path.abspath(os.fspath(cfg.BEETS_DB_PATH.parent)))
    carrier_fd = None
    carrier_file = None
    carrier_path = None
    capture = None
    root_chain = []
    root_names = ()
    parent_chain = []
    parent_names = ()
    cleanup = None
    retain_chains = False
    try:
        parent_chain, parent_names = _open_absolute_merge_chain(config_parent, create=True)
        parent_fd = parent_chain[-1]
        root = os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
        root_chain, root_names = _open_absolute_merge_chain(root)
        root_fd = root_chain[-1]
        root_identity = _ownership_identity(os.fstat(root_fd))
        nonce = secrets.token_hex(32)
        carrier_path = config_parent / f".qobuz-managed-beets-{nonce}.jsonl"
        parent_stat = os.fstat(parent_fd)
        capture = {
            "_file": None,
            "_override_fd": None,
            "path": carrier_path,
            "nonce": nonce,
            "owner": dict(owner),
            "parent": str(config_parent),
            "parent_device": int(parent_stat.st_dev),
            "parent_inode": int(parent_stat.st_ino),
            "root": root,
            "root_identity": root_identity,
            "intent": [dict(record) for record in intent],
            "spawned": False,
            "_beets_runtime": runtime,
            "_parent_chain": parent_chain,
            "_parent_names": parent_names,
            "_root_chain": root_chain,
            "_root_names": root_names,
        }
        reservation = _managed_carrier_reservation(capture)
        capture["reservation"] = reservation
        on_reservation(
            {
                "version": 1,
                "kind": "managed-beets-reservation",
                "owner": dict(owner),
                "reservation": {
                    **reservation,
                    "owner": dict(reservation["owner"]),
                },
            }
        )
        if not _managed_parent_namespace_matches(capture) or not _managed_root_namespace_matches(
            capture
        ):
            raise OSError("managed Beets namespace changed after reservation")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe managed Beets carrier creation is unavailable")
        carrier_fd = os.open(
            carrier_path.name,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(carrier_fd, 0o600)
        carrier_file = os.fdopen(carrier_fd, "r+b", buffering=0)
        carrier_fd = None
        capture["_file"] = carrier_file
        held = os.fstat(carrier_file.fileno())
        capture.update(
            {
                "device": int(held.st_dev),
                "inode": int(held.st_ino),
            }
        )
        generation_zero = {
            "version": _MANAGED_SNAPSHOT_VERSION,
            "generation": 0,
            "previous_hash": None,
            "nonce": nonce,
            "owner": dict(owner),
            "root": root,
            "root_identity": root_identity,
            "sealed": False,
            "intent": [dict(record) for record in intent],
            "mappings": [],
            "cleanup_directories": [],
        }
        capture["last_hash"] = _write_managed_snapshot(carrier_file.fileno(), generation_zero)

        os.fsync(parent_fd)
        named = os.stat(
            carrier_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        held = os.fstat(carrier_file.fileno())
        if (
            not stat.S_ISREG(named.st_mode)
            or not stat.S_ISREG(held.st_mode)
            or named.st_uid != os.geteuid()
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(named.st_mode) != 0o600
            or stat.S_IMODE(held.st_mode) != 0o600
            or int(named.st_dev) != capture["device"]
            or int(named.st_ino) != capture["inode"]
            or int(held.st_dev) != capture["device"]
            or int(held.st_ino) != capture["inode"]
            or not _managed_carrier_namespace_matches(capture)
            or not _managed_root_namespace_matches(capture)
        ):
            raise OSError("managed beets carrier changed before checkpoint")
        reference = _managed_carrier_reference(capture)
        capture["reference"] = reference
        lifetime = _ManagedBeetsRunLifetime(capture)
        cleanup = _ManagedBeetsRunCleanup(lifetime)
        retain_chains = True
    except BaseException:
        descriptor = carrier_file.fileno() if carrier_file is not None else carrier_fd
        if carrier_path is not None and descriptor is not None:
            try:
                held = os.fstat(descriptor)
                parent_fd = parent_chain[-1]
                named = os.stat(
                    carrier_path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISREG(held.st_mode)
                    and stat.S_ISREG(named.st_mode)
                    and int(held.st_dev) == int(named.st_dev)
                    and int(held.st_ino) == int(named.st_ino)
                ):
                    os.unlink(carrier_path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except OSError:
                pass
        if carrier_file is not None:
            try:
                carrier_file.close()
            except BaseException:
                pass
        elif carrier_fd is not None:
            try:
                os.close(carrier_fd)
            except BaseException:
                pass
        raise
    finally:
        if not retain_chains:
            for descriptor in reversed((*root_chain, *parent_chain)):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    return cleanup, capture, intent, plugin_config


def _read_ownership_payload(capture):
    """Return a bounded manifest from our exact one-run nonce, or None."""
    if not isinstance(capture, dict):
        return None
    manifest = capture.get("_file")
    nonce = capture.get("nonce")
    if manifest is None or not isinstance(nonce, str):
        return None
    try:
        descriptor = manifest.fileno()
        manifest_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(manifest_stat.st_mode)
            or manifest_stat.st_uid != os.geteuid()
            or manifest_stat.st_mode & 0o077
            or manifest_stat.st_size > _OWNERSHIP_MAX_BYTES
        ):
            return None
        raw = os.pread(descriptor, manifest_stat.st_size + 1, 0)
        if len(raw) != manifest_stat.st_size:
            return None
        candidates = []
        for line in reversed(raw.splitlines()):
            try:
                candidates.append(json.loads(line.decode("ascii")))
            except (UnicodeError, ValueError):
                continue
    except (AttributeError, OSError):
        return None
    expected_root = os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
    for payload in candidates:
        if (
            isinstance(payload, dict)
            and type(payload.get("version")) is int
            and payload.get("version") == 1
            and payload.get("nonce") == nonce
            and type(payload.get("sealed")) is bool
            and payload.get("root") == expected_root
            and isinstance(payload.get("items"), list)
            and isinstance(payload.get("cleanup_directories", []), list)
        ):
            return payload
    return None


def _reclaim_ownership_capture(capture, payload=None):
    """Reclaim exact empty plug-in directories after an unaccepted run."""
    payload = payload or _read_ownership_payload(capture)
    if payload is None:
        return 0
    nonce = capture.get("nonce") if isinstance(capture, dict) else None
    if not isinstance(nonce, str):
        return 0
    root_fd = None
    try:
        root_fd = _open_ownership_dir(cfg.MUSIC_ROOT)
        root_stat = os.fstat(root_fd)
        root_identity = payload.get("root_identity")
        if (
            not _valid_ownership_identity(root_identity)
            or int(root_stat.st_dev) != root_identity["device"]
            or int(root_stat.st_ino) != root_identity["inode"]
        ):
            return 0
        records = payload.get("cleanup_directories", [])
        removed = _reclaim_ownership_directories(
            root_fd,
            records,
            nonce,
        )
        _strip_ownership_markers(root_fd, records, nonce)
        return removed
    except (OSError, TypeError, ValueError):
        return 0
    finally:
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass


# What fetchart will accept, from its own CONTENT_TYPES.
_ART_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def relocate_disc_album_artwork(album_dir) -> bool:
    """Move a staged album's root artwork beside the first disc's tracks.

    Beets gives a multi-disc album's import task the DISC directories as its
    source paths, and fetchart searches only those, so a cover sitting in the
    album root is never offered as a candidate. `beet import` moves only audio,
    so the cover is then left behind in staging. Any leftover keeps the
    download's staging run present, which the durable completion proof reads as
    a download that never finished. That stops the queue and reports the album
    as failed even though every track imported.

    The first disc folder is where Beets files this layout's album art anyway
    (Album.item_dir() is the directory of the album's first item), so moving it
    there keeps the art with the album without widening what the art guard is
    willing to publish. Single-disc albums are left alone: their album root is
    the item directory, so fetchart already sees the cover.
    """
    root = Path(album_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return False
    if any(p.suffix.lower() in cfg.AUDIO_EXTS and p.is_file() for p in entries):
        return False
    covers = sorted(
        p for p in entries if p.suffix.lower() in _ART_EXTS and p.is_file()
    )
    if not covers:
        return False
    disc_dirs = []
    for entry in sorted(entries):
        if not entry.is_dir():
            continue
        try:
            has_audio = any(
                child.suffix.lower() in cfg.AUDIO_EXTS and child.is_file()
                for child in entry.iterdir()
            )
        except OSError:
            continue
        if has_audio:
            disc_dirs.append(entry)
    if not disc_dirs:
        return False
    target = disc_dirs[0]
    moved = False
    for cover in covers:
        destination = target / cover.name
        if destination.exists():
            continue
        try:
            os.rename(cover, destination)
        except OSError as e:
            vlog(f"couldn't move {cover.name} beside the first disc: {e}")
            continue
        vlog(f"moved {cover.name} into {target.name} so beets can file it")
        moved = True
    return moved


def beets_import_paths(consolidate=True, *, source_files_out=None, album_dirs=None):
    """Run beets on explicit staged albums and return a success bool.

    With no explicit list, legacy preflight recovery derives only public audio
    directories. Hidden per-rip roots and durable retry recovery are never
    adopted by a whole-staging command.
    """
    roots = [Path(directory) for directory in (album_dirs or [])]
    if album_dirs is None:
        roots = sorted(
            {
                path.parent
                for path in cfg.STAGING_DIR.rglob("*")
                if path.is_file()
                and path.suffix.lower() in cfg.AUDIO_EXTS
                and not _under_retry_dir(path)
                and not _under_isolated_run(path)
            }
        )
    if not roots:
        return True
    prepared_sources = source_files_out if source_files_out is not None else []
    override_path, cleanup, runtime = _prepare_for_beets_run(
        roots=roots, source_files_out=prepared_sources
    )
    if cleanup is None:
        return False
    intended_audio = tuple(
        receipt for receipt in prepared_sources if receipt.path.suffix.lower() in cfg.AUDIO_EXTS
    )
    ok, _ = _beets_direct(
        override_path,
        cleanup,
        [str(directory) for directory in roots],
        intended_audio=intended_audio,
        beets_runtime=runtime,
    )
    if ok and consolidate:
        _consolidate_duplicate_albums()
    return ok


def beets_import_albums(album_dirs, *, ownership_out=None):
    """Run beets import scoped to ``album_dirs`` and return a tri-state code:

    - ``"ok"``: import succeeded.
    - ``"timeout"``: the idle-timeout guard fired; the executor decides
      whether to retry based on ``cfg.BEETS_MAX_ATTEMPTS``.
    - ``"error"``: any other failure (config missing, non-zero exit,
      silent skip). The executor won't retry these.

    Consolidation is left to the caller (run once per batch instead of per
    album, since the duplicate-album fold is a library-wide pass).
    """
    if not album_dirs:
        return "ok"
    prepared_sources = []
    override_path, cleanup, runtime = _prepare_for_beets_run(
        roots=album_dirs,
        ownership_out=ownership_out,
        source_files_out=prepared_sources,
    )
    if cleanup is None:
        return "error"
    intended_audio = tuple(
        receipt for receipt in prepared_sources if receipt.path.suffix.lower() in cfg.AUDIO_EXTS
    )
    ok, kind = _beets_direct(
        override_path,
        cleanup,
        [str(d) for d in album_dirs],
        ownership_capture=ownership_out,
        intended_audio=intended_audio,
        beets_runtime=runtime,
    )
    return "ok" if ok else kind


def beets_import_managed(
    album_dirs, bindings, *, owner, on_reservation, on_intent, authority_check=None
):
    """Run one non-retrying import with durable slot-to-file evidence."""
    owner = normalise_recovery_owner(owner)
    if owner is None:
        raise ValueError("managed beets import requires an owner")
    if not callable(on_reservation) or not callable(on_intent):
        raise ValueError("managed beets import requires reservation and intent checkpoints")
    if authority_check is not None and not callable(authority_check):
        raise ValueError("managed beets authority check must be callable")
    if authority_check is not None:
        authority_check()
    roots = _normalise_managed_roots(album_dirs)
    if roots is None:
        return ManagedBeetsImportResult("refused", None)
    try:
        prepared = _run_managed_sigint_deferred(
            lambda: _prepare_managed_beets_run(
                roots,
                bindings,
                owner,
                on_reservation=on_reservation,
            )
        )
    except Exception:
        return ManagedBeetsImportResult("refused", None)
    if prepared is None:
        return ManagedBeetsImportResult("refused", None)
    cleanup, capture, intent, plugin_config = prepared
    reference = capture["reference"]
    reservation = capture["reservation"]
    try:
        checkpoint_carrier = {
            **reference,
            "owner": dict(reference["owner"]),
        }
        on_intent(
            {
                "version": 1,
                "kind": "managed-beets",
                "owner": dict(owner),
                "carrier": checkpoint_carrier,
            }
        )
        if authority_check is not None:
            authority_check()
    except BaseException as exc:
        cleanup_error = _finish_beets_cleanup(cleanup)
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            if hasattr(cleanup_error, "add_note"):
                cleanup_error.add_note(f"beets intent callback also failed: {exc}")
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        if isinstance(exc, Exception):
            return ManagedBeetsImportResult("refused", reference, reservation=reservation)
        if cleanup_error is not None and hasattr(exc, "add_note"):
            exc.add_note(f"beets intent cleanup also failed: {cleanup_error}")
        raise
    override_path = None
    try:
        current_intent = _normalise_managed_bindings(intent, roots)
        if current_intent == intent:
            override_path = _prepare_managed_override(capture, plugin_config)
    except Exception:
        current_intent = None
    except BaseException as exc:
        cleanup_error = _finish_beets_cleanup(cleanup)
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            if hasattr(cleanup_error, "add_note"):
                cleanup_error.add_note(f"beets pre-spawn validation also failed: {exc}")
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        if cleanup_error is not None and hasattr(exc, "add_note"):
            exc.add_note(f"beets pre-spawn cleanup also failed: {cleanup_error}")
        raise
    if current_intent != intent:
        cleanup_error = _finish_beets_cleanup(cleanup)
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        return ManagedBeetsImportResult(
            "refused",
            reference,
            spawned=False,
            reservation=reservation,
        )
    intended_audio = tuple(intent)
    try:
        ok, _kind = _beets_direct(
            override_path,
            cleanup,
            [str(directory) for directory in roots],
            intended_audio=intended_audio,
            managed_capture=capture,
            before_managed_spawn=authority_check,
        )
    except Exception:
        spawned = capture.get("spawned") is True
        return ManagedBeetsImportResult(
            "uncertain" if spawned else "refused",
            reference,
            spawned=spawned,
            reservation=reservation,
        )
    evidence = capture.get("result") if ok else None
    spawned = capture.get("spawned") is True
    if not ok or evidence is None:
        return ManagedBeetsImportResult(
            "uncertain" if spawned else "refused",
            reference,
            spawned=spawned,
            reservation=reservation,
        )
    return ManagedBeetsImportResult(
        "ok",
        reference,
        evidence=evidence,
        spawned=spawned,
        reservation=reservation,
    )


def _wait_for_beets_completion(completion):
    error = None
    while True:
        try:
            if completion.is_set():
                return error
            completion.wait()
        except BaseException as exc:
            if error is None:
                error = exc


def _set_beets_event(event):
    error = None
    while True:
        try:
            event.set()
            if event.is_set():
                return error
        except BaseException as exc:
            if error is None:
                error = exc


def _finish_beets_thread(thread, completion):
    error = _wait_for_beets_completion(completion)
    while True:
        try:
            thread.join()
            return error
        except BaseException as exc:
            if error is None:
                error = exc


def _spawn_owned_beets_process(
    owner, args, *, database_exclusion=None, before_spawn=None, **options
):
    """Spawn under a guardian that remains until the child is reaped."""
    factory = subprocess.Popen
    process = None
    if factory is _SYSTEM_POPEN:
        options["start_new_session"] = True
        process = factory.__new__(factory)
        process._child_created = False

    spawn_completion = threading.Event()
    retire_request = threading.Event()
    retire_completion = threading.Event()
    owner.update(
        {
            "cancel": False,
            "error": None,
            "retained_lock_fd": None,
            "retired": False,
            "retirement_error": None,
            "spawn_attempted": False,
            "spawn_completion": spawn_completion,
            "retire_request": retire_request,
            "retire_completion": retire_completion,
        }
    )

    def _guardian():
        child_lock_fd = None
        retirement_proven = False
        try:
            if database_exclusion is not None:
                child_lock_fd = database_exclusion.duplicate_for_child()
                pass_fds = set(options.get("pass_fds", ()))
                pass_fds.add(child_lock_fd)
                options["pass_fds"] = tuple(sorted(pass_fds))
            if retire_request.is_set():
                owner["cancel"] = True
            if not owner["cancel"]:
                if before_spawn is not None:
                    before_spawn()
                owner["spawn_attempted"] = True
                if process is not None:
                    factory.__init__(process, args, **options)
                else:
                    owner["process"] = factory(args, **options)
        except BaseException as exc:
            owner["error"] = exc
        finally:
            completion_error = _set_beets_event(spawn_completion)
            if owner["error"] is None:
                owner["error"] = completion_error
            elif completion_error is not None and hasattr(owner["error"], "add_note"):
                owner["error"].add_note("beets spawn completion signalling also failed")

        try:
            if owner["error"] is None and not owner["cancel"]:
                wait_error = _wait_for_beets_completion(retire_request)
                if wait_error is not None:
                    owner["retirement_error"] = wait_error
            process_to_retire = owner.get("process")
            if process_to_retire is not None:
                retirement_error, retirement_proven = _retire_beets_process_now(
                    process_to_retire,
                    spawn_attempted=owner["spawn_attempted"],
                    require_group_drain=factory is _SYSTEM_POPEN,
                )
                if owner["retirement_error"] is None:
                    owner["retirement_error"] = retirement_error
                elif retirement_error is not None and hasattr(
                    owner["retirement_error"], "add_note"
                ):
                    owner["retirement_error"].add_note("beets process retirement also failed")
            else:
                retirement_proven = not owner["spawn_attempted"]
            if child_lock_fd is not None and retirement_proven:
                lease_error, retirement_proven = database_exclusion.retire_child_duplicate(
                    child_lock_fd
                )
                child_lock_fd = None
                if owner["retirement_error"] is None:
                    owner["retirement_error"] = lease_error
                elif lease_error is not None and hasattr(owner["retirement_error"], "add_note"):
                    owner["retirement_error"].add_note("beets child-lock retirement also failed")
        finally:
            if child_lock_fd is not None and retirement_proven:
                try:
                    os.close(child_lock_fd)
                except BaseException as exc:
                    if owner["retirement_error"] is None:
                        owner["retirement_error"] = exc
                    elif hasattr(owner["retirement_error"], "add_note"):
                        owner["retirement_error"].add_note(
                            "beets guardian lock cleanup also failed"
                        )
            elif child_lock_fd is not None:
                owner["retained_lock_fd"] = child_lock_fd
                child_lock_fd = None
            owner["retired"] = True
            completion_error = _set_beets_event(retire_completion)
            if owner["retirement_error"] is None:
                owner["retirement_error"] = completion_error
            elif completion_error is not None and hasattr(owner["retirement_error"], "add_note"):
                owner["retirement_error"].add_note(
                    "beets retirement completion signalling also failed"
                )

    thread = threading.Thread(target=_guardian, daemon=True)
    owner["thread"] = thread
    owner["process"] = process
    start_error = None
    try:
        thread.start()
    except BaseException as exc:
        start_error = exc

    wait_error = None
    if thread.ident is not None:
        wait_error = _wait_for_beets_completion(spawn_completion)

    error = start_error or wait_error or owner["error"]
    if error is not None:
        request_error = _set_beets_event(retire_request)
        retirement_error = None
        if thread.ident is not None:
            retirement_error = _finish_beets_thread(thread, retire_completion)
        else:
            owner["process"] = None
            owner["retired"] = True
            completion_error = _set_beets_event(retire_completion)
            if retirement_error is None:
                retirement_error = completion_error
        retirement_error = retirement_error or owner["retirement_error"] or request_error
        secondary = owner["error"]
        if secondary is not None and secondary is not error and hasattr(error, "add_note"):
            error.add_note(f"beets process creation also failed: {secondary}")
        if retirement_error is not None and hasattr(error, "add_note"):
            error.add_note(f"beets process cleanup also failed: {retirement_error}")
        raise error.with_traceback(error.__traceback__)
    return owner.get("process")


def _kill_beets_process_group(proc):
    if isinstance(proc, _SYSTEM_POPEN):
        pid = getattr(proc, "pid", None)
        if type(pid) is int and pid > 0:
            try:
                pgid = os.getpgid(pid)
                if pgid == pid and pgid != os.getpgrp():
                    os.killpg(pgid, signal.SIGKILL)
                    return pgid
            except OSError:
                pass
    proc.kill()
    return None


def _process_group_has_non_zombie_member(pgid):
    try:
        entries = os.scandir("/proc")
    except OSError:
        raise
    with entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                raw = Path(entry.path, "stat").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
            closing = raw.rfind(b")")
            fields = raw[closing + 1 :].split() if closing >= 0 else ()
            if len(fields) < 3:
                raise OSError(
                    errno.EIO,
                    "could not verify the retired beets process group",
                )
            try:
                member_group = int(fields[2])
            except ValueError as exc:
                raise OSError(
                    errno.EIO,
                    "could not verify the retired beets process group",
                ) from exc
            if member_group == pgid and fields[0] not in {b"Z", b"X"}:
                return True
    return False


def _wait_for_beets_process_group_exit(pgid):
    error = None
    deadline = time.monotonic() + 10
    while True:
        try:
            if not _process_group_has_non_zombie_member(pgid):
                return error, True
            if time.monotonic() >= deadline:
                return error or OSError(
                    errno.ETIMEDOUT,
                    "beets process group did not retire; restart required",
                ), False
            time.sleep(0.01)
        except Exception as exc:
            return error or exc, False
        except BaseException as exc:
            if error is None:
                error = exc


def _retire_beets_process_now(proc, *, spawn_attempted, require_group_drain):
    error = None
    killed = False
    group_drained = False
    process_group = None
    reaped = False
    while True:
        try:
            if isinstance(proc, _SYSTEM_POPEN) and getattr(proc, "pid", None) is None:
                if spawn_attempted:
                    return error or OSError(
                        errno.EBUSY,
                        "beets child identity is uncertain; restart required",
                    ), False
                return error, True
            if getattr(proc, "returncode", None) is not None:
                if require_group_drain and not group_drained:
                    process_group = getattr(proc, "pid", None)
                    if (
                        type(process_group) is not int
                        or process_group <= 0
                        or process_group == os.getpgrp()
                    ):
                        return error or OSError(
                            errno.EBUSY,
                            "beets process-group identity is uncertain; restart required",
                        ), False
                    group_error, group_drained = _wait_for_beets_process_group_exit(process_group)
                    if error is None:
                        error = group_error
                    elif group_error is not None and hasattr(error, "add_note"):
                        error.add_note("beets process-group verification also failed")
                if not group_drained and require_group_drain:
                    return error or OSError(
                        errno.EBUSY,
                        "beets process-group retirement is uncertain; restart required",
                    ), False
                return error, True
            if reaped:
                proven = not require_group_drain or group_drained
                if not proven and error is None:
                    error = OSError(
                        errno.EBUSY,
                        "beets process-group retirement is uncertain; restart required",
                    )
                return error, proven
            if not killed:
                process_group = _kill_beets_process_group(proc)
                killed = True
                continue
            if process_group is not None and not group_drained:
                group_error, group_drained = _wait_for_beets_process_group_exit(process_group)
                if error is None:
                    error = group_error
                elif group_error is not None and hasattr(error, "add_note"):
                    error.add_note("beets process-group verification also failed")
                if not group_drained:
                    return error, False
                continue
            proc.wait()
            reaped = True
        except ProcessLookupError:
            killed = True
        except Exception as exc:
            return error or exc, False
        except BaseException as exc:
            if error is None:
                error = exc


def _retire_beets_process(owner):
    if owner.get("retired"):
        return owner.get("retirement_error")
    request_error = _set_beets_event(owner["retire_request"])
    thread = owner.get("thread")
    if thread is None or thread.ident is None:
        owner["cancel"] = True
        owner["process"] = None
        owner["retired"] = True
        completion_error = _set_beets_event(owner["retire_completion"])
        return owner.get("retirement_error") or completion_error or request_error
    thread_error = _finish_beets_thread(thread, owner["retire_completion"])
    return owner["retirement_error"] or thread_error or request_error


def _close_beets_process_streams(proc):
    error = None
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        close = getattr(stream, "close", None)
        if not callable(close) or getattr(stream, "closed", False):
            continue
        try:
            close()
        except BaseException as exc:
            if error is None:
                error = exc
    return error


def _read_cancellable_beets_pipe(stream, stop, consume):
    """Drain one child pipe without depending on every writer reaching EOF."""
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, TypeError, ValueError):
        # Small process doubles in focused tests expose an iterator rather than
        # a real pipe. Keep that path simple while production uses pollable fds.
        for chunk in stream:
            if stop.is_set():
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            consume(chunk)
        return

    remaining_stop_drains = 16
    while True:
        stopping = stop.is_set()
        try:
            ready, _, _ = select.select([descriptor], [], [], 0 if stopping else 0.1)
        except (OSError, ValueError):
            return
        if not ready:
            if stopping:
                return
            continue
        try:
            chunk = os.read(descriptor, 64 * 1024)
        except BlockingIOError:
            continue
        except OSError:
            return
        if not chunk:
            return
        consume(chunk)
        if stopping:
            remaining_stop_drains -= 1
            if remaining_stop_drains <= 0:
                return


def _wait_owned_beets_process_without_reaping(process, timeout):
    """Wait for the session leader while retaining its process-group anchor."""
    if not isinstance(process, _SYSTEM_POPEN):
        process.wait(timeout=timeout)
        return

    deadline = None if timeout is None else time.monotonic() + timeout
    options = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        if os.waitid(os.P_PID, process.pid, options) is not None:
            return
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(0.01)


def _run_owned_beets_capture(args, *, env, timeout, database_exclusion, before_spawn=None):
    owner = {"process": None}
    caught = None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    reader_stop = threading.Event()
    readers = []
    reader_errors = []

    def start_reader(name, stream):
        done = threading.Event()

        def read_pipe():
            try:
                _read_cancellable_beets_pipe(stream, reader_stop, buffers[name].extend)
            except BaseException as exc:
                reader_errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=read_pipe, daemon=True)
        thread.start()
        readers.append((thread, done))

    try:
        process = _spawn_owned_beets_process(
            owner,
            args,
            database_exclusion=database_exclusion,
            before_spawn=before_spawn,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            start_reader("stdout", process.stdout)
            start_reader("stderr", process.stderr)
            _wait_owned_beets_process_without_reaping(process, timeout)
        except BaseException as exc:
            caught = exc
    except BaseException as exc:
        caught = exc

    retirement_error = None
    stream_error = None
    reader_stop.set()
    if "thread" in owner:
        retirement_error = _retire_beets_process(owner)
    for thread, done in readers:
        reader_error = _finish_beets_thread(thread, done)
        if reader_error is not None and not reader_errors:
            reader_errors.append(reader_error)
    process = owner.get("process")
    if process is not None:
        stream_error = _close_beets_process_streams(process)
    cleanup_error = (
        retirement_error or stream_error or (reader_errors[0] if reader_errors else None)
    )
    if caught is not None:
        if cleanup_error is not None and hasattr(caught, "add_note"):
            caught.add_note(f"beets process cleanup also failed: {cleanup_error}")
        raise caught.with_traceback(caught.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)
    return subprocess.CompletedProcess(
        args,
        process.returncode,
        bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
        bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
    )


def _wait_or_idle_kill(proc, last_output):
    """Block until proc exits. Raise subprocess.TimeoutExpired ONLY if it emits
    no output for cfg.BEETS_TIMEOUT seconds, indicating a genuinely hung import (DB
    lock, an unexpected prompt, a deadlocked plugin).
    """
    idle_limit = cfg.BEETS_TIMEOUT
    if isinstance(proc, _SYSTEM_POPEN):
        # Observe the session leader's exit without reaping it. Keeping that
        # zombie leader in the process table anchors its numeric process-group
        # identity until the guardian has drained the whole group; signalling a
        # PGID after wait() reaps the leader can instead hit an unrelated group
        # if the number is reused in the gap.
        wait_options = os.WEXITED | os.WNOHANG | os.WNOWAIT
        while True:
            result = os.waitid(os.P_PID, proc.pid, wait_options)
            if result is not None:
                return
            if idle_limit and idle_limit > 0 and (time.monotonic() - last_output[0] > idle_limit):
                raise subprocess.TimeoutExpired(proc.args, idle_limit)
            time.sleep(0.01)
    while True:
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            if idle_limit and idle_limit > 0 and (time.monotonic() - last_output[0] > idle_limit):
                raise  # no output for idle_limit s → assume hung
            # still running and recently active (or guard disabled) → wait


def _count_audio_under(paths, *, include_retry=False):
    """Total audio files under the given import paths, ignoring the parked-retry
    subtree. beets imports by moving files into the library, so a before/after
    count tells a real import (the staged tracks left) from an exit-0 run that
    moved nothing."""
    exts = set(cfg.AUDIO_EXTS)
    n = 0
    for p in paths:
        root = Path(p)
        if not root.exists():
            continue
        try:
            for f in root.rglob("*"):
                if (
                    f.is_file()
                    and f.suffix.lower() in exts
                    and (include_retry or not _under_retry_dir(f))
                ):
                    n += 1
        except OSError:
            continue
    return n


def _close_beets_prune_capture(capture):
    if not isinstance(capture, dict):
        return
    for root in capture.get("roots", ()):
        for descriptor in reversed(root.get("chain", ())):
            try:
                os.close(descriptor)
            except OSError:
                pass
    for descriptor in reversed(capture.get("staging_chain", ())):
        try:
            os.close(descriptor)
        except OSError:
            pass
    capture.clear()


def _capture_beets_prune_roots(paths):
    """Hold exact staging roots before Beets can rename or replace them."""
    try:
        staging_chain, staging_names = _open_absolute_merge_chain(cfg.STAGING_DIR)
    except OSError:
        return None
    capture = {
        "staging_chain": staging_chain,
        "staging_names": staging_names,
        "roots": [],
    }
    seen = set()
    try:
        staging = Path(os.path.abspath(os.fspath(cfg.STAGING_DIR)))
        staging_fd = staging_chain[-1]
        device = int(os.fstat(staging_fd).st_dev)
        for raw in paths:
            candidate = Path(os.path.abspath(os.fspath(raw)))
            if candidate == staging:
                parts = ()
            else:
                parts = _merge_relative_parts(staging, candidate)
                if parts is None:
                    continue
            if parts in seen or (parts and parts[0] == cfg.BEETS_RETRY_DIR):
                continue
            seen.add(parts)
            chain = []
            try:
                chain, _created = _open_merge_chain(
                    staging_fd,
                    parts,
                    required_device=device,
                )
                if not _merge_chain_is_named(chain, parts):
                    raise OSError("staging import root changed while it was opened")
            except OSError:
                for descriptor in reversed(chain):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                continue
            capture["roots"].append({"chain": chain, "parts": parts})
        return capture
    except BaseException:
        _close_beets_prune_capture(capture)
        raise


def _hold_beets_prune_descendants(records, directory_fd, parts, device):
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            child_stat = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(child_stat.st_mode) or int(child_stat.st_dev) != device:
                continue
            child = _open_exact_merge_dir(directory_fd, entry.name, child_stat)
            child_parts = (*parts, entry.name)
            try:
                if child_parts[0] == cfg.BEETS_RETRY_DIR:
                    continue
                _hold_beets_prune_descendants(
                    records,
                    child,
                    child_parts,
                    device,
                )
                # The first component is the private per-run root. Its owner
                # retires it separately after the exact import is accepted.
                if len(child_parts) > 1:
                    _hold_merge_directory(
                        records,
                        directory_fd,
                        entry.name,
                        child,
                        parts,
                    )
            finally:
                os.close(child)


def _prune_captured_beets_directories(capture):
    """Remove only held empty directories still named beneath staging."""
    if not isinstance(capture, dict):
        return
    staging_chain = capture.get("staging_chain", ())
    staging_names = capture.get("staging_names", ())
    if not staging_chain or not _merge_chain_is_named(staging_chain, staging_names):
        return
    staging_fd = staging_chain[-1]
    device = int(os.fstat(staging_fd).st_dev)
    for root in capture.get("roots", ()):
        chain = root.get("chain", ())
        parts = root.get("parts", ())
        records = []
        try:
            if (
                not chain
                or (int(os.fstat(chain[0]).st_dev), int(os.fstat(chain[0]).st_ino))
                != (int(os.fstat(staging_fd).st_dev), int(os.fstat(staging_fd).st_ino))
                or not _merge_chain_is_named(chain, parts)
            ):
                continue
            _hold_beets_prune_descendants(records, chain[-1], parts, device)
            # Include the explicit root and its ancestors below the first
            # staging component. Never remove that top-level per-run root.
            for index in range(2, len(chain)):
                _hold_merge_directory(
                    records,
                    chain[index - 1],
                    parts[index - 1],
                    chain[index],
                    parts[: index - 1],
                )
            _remove_held_merge_directories(staging_fd, records)
        except OSError:
            pass
        finally:
            _close_held_merge_directories(records)


def _finish_beets_cleanup(cleanup):
    interrupted = None
    while True:
        try:
            cleanup()
            return interrupted
        except Exception as exc:
            if interrupted is not None:
                if hasattr(interrupted, "add_note"):
                    interrupted.add_note(f"beets cleanup retry also failed: {exc}")
                return interrupted
            return exc
        except BaseException as exc:
            if interrupted is None:
                interrupted = exc


def _beets_direct(
    override_path,
    cleanup_fn,
    paths=None,
    *,
    ownership_capture=None,
    intended_audio=None,
    managed_capture=None,
    beets_runtime=None,
    before_managed_spawn=None,
):
    exclusion = _SQLiteDatabaseExclusion()
    database_anchor = None
    result = None
    caught = None
    database_ready = False
    try:
        database_anchor = _open_beets_database_anchor()
        if database_anchor is None:
            raise OSError("configured beets database is unavailable")
        exclusion.acquire(database_anchor["parent_chain"][-1])
        _bootstrap_beets_database_anchor(database_anchor)
        _preflight_beets_database_anchor(database_anchor)
        database_ready = True
        result = _beets_direct_guarded(
            override_path,
            paths,
            database_exclusion=exclusion,
            database_anchor=database_anchor,
            ownership_capture=ownership_capture,
            intended_audio=intended_audio,
            managed_capture=managed_capture,
            beets_runtime=beets_runtime,
            before_managed_spawn=before_managed_spawn,
        )
    except BaseException as exc:
        caught = exc

    cleanup_error = _finish_beets_cleanup(cleanup_fn)
    if cleanup_error is not None:
        if caught is not None and hasattr(caught, "add_note"):
            caught.add_note(f"beets run cleanup also failed: {cleanup_error}")
        else:
            caught = cleanup_error

    exclusion_error = exclusion.release()
    _close_beets_database_anchor(database_anchor)
    if exclusion_error is not None:
        if caught is not None and hasattr(caught, "add_note"):
            caught.add_note("beets database exclusion cleanup also failed")
        else:
            log.info(
                fmt(
                    C.RED,
                    "  ✗  The beets database exclusion could not be released; "
                    "the import was not accepted.",
                )
            )
            return False, "error"
    if caught is not None:
        if not database_ready and isinstance(
            caught, (OSError, sqlite3.Error, TypeError, ValueError)
        ):
            log.info(
                fmt(
                    C.RED,
                    f"  ✗  The beets database is not safe to open ({caught}).",
                )
            )
            return False, "error"
        raise caught.with_traceback(caught.__traceback__)
    return result


def _beets_direct_guarded(
    override_path,
    paths=None,
    *,
    database_exclusion,
    database_anchor=None,
    ownership_capture=None,
    intended_audio=None,
    managed_capture=None,
    beets_runtime=None,
    before_managed_spawn=None,
):
    """Call ``beet import`` as a direct subprocess (bundled mode), scoped to
    the given paths (default: whole STAGING_DIR). Returns
    ``(ok: bool, kind: str)`` where kind is ``"ok"`` / ``"timeout"`` /
    ``"error"`` so the caller can distinguish a retry-worthy idle-timeout
    from a permanent failure."""
    explicit_paths = bool(paths)
    if not paths:
        paths = [str(cfg.STAGING_DIR)]
    audio_before = _count_audio_under(paths, include_retry=explicit_paths)
    if intended_audio is not None:
        intended_audio = tuple(intended_audio)
        if managed_capture is not None:
            intended_valid = bool(intended_audio) and all(
                _valid_managed_binding(record)
                and staging_mod.capture_file(
                    record["path"],
                    expected=tuple(record["identity"]),
                )
                is not None
                for record in intended_audio
            )
        else:
            intended_valid = bool(intended_audio) and all(
                staging_mod.capture_file(receipt.path, expected=receipt.identity) is not None
                for receipt in intended_audio
            )
        if not intended_valid:
            log.info(
                fmt(
                    C.RED,
                    "  ✗  The exact staged tracks changed before beets started; "
                    "refusing to import them.",
                )
            )
            return False, "error"
    if isinstance(managed_capture, dict):
        runtime = managed_capture.get("_beets_runtime")
        if not _beets_runtime_matches(runtime):
            return False, "error"
        cmd = [
            runtime.python,
            "-I",
            str(_managed_beets_entrypoint()),
        ]
    else:
        runtime = beets_runtime
        if not _beets_runtime_matches(runtime):
            return False, "error"
        cmd = [
            runtime.python,
            "-I",
            str(_managed_beets_entrypoint()),
            "--run-beets",
        ]
    if override_path:
        cmd += ["-c", str(override_path)]
    cmd += ["import", *paths]

    # Total album count when the caller passed explicit paths; 0 (= indeterminate
    # progress bar) when the whole staging dir is being scanned.
    staging_root = str(cfg.STAGING_DIR).rstrip("/")
    explicit_album_paths = [p for p in paths if p.rstrip("/") != staging_root]
    total_albums = len(explicit_album_paths)
    report_progress("Importing into your library", 0, total_albums, "")
    log.info(fmt(C.CYAN, "  ⟳  Running beets import ..."))
    out_lines = []
    last_output = [time.monotonic()]
    # Captured by _reader for per-album progress updates. A list-of-1 lets the
    # nested function can mutate without `nonlocal`.
    last_album = [None]
    seen_albums = [0]

    config_dir = os.path.abspath(os.fspath(cfg.BEETS_CONFIG_DIR))
    beet_env = {**os.environ, "BEETSDIR": config_dir}
    for variable in _OWNERSHIP_ENV_KEYS:
        beet_env.pop(variable, None)
    popen_options = {}
    if isinstance(ownership_capture, dict):
        descriptor = ownership_capture.get("fd")
        nonce = ownership_capture.get("nonce")
        if type(descriptor) is int and descriptor >= 3 and isinstance(nonce, str):
            beet_env[_OWNERSHIP_FD_ENV] = str(descriptor)
            beet_env[_OWNERSHIP_NONCE_ENV] = nonce
            beet_env[_OWNERSHIP_SOURCE_ROOTS_ENV] = json.dumps(
                [os.path.abspath(os.fspath(path)) for path in paths]
            )
            popen_options["pass_fds"] = (descriptor,)
    if isinstance(managed_capture, dict):
        manifest = managed_capture.get("_file")
        nonce = managed_capture.get("nonce")
        override_descriptor = managed_capture.get("_override_fd")
        try:
            descriptor = manifest.fileno()
            override_stat = os.fstat(override_descriptor)
            required_seals = (
                fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
            )
            override_seals = fcntl.fcntl(override_descriptor, fcntl.F_GET_SEALS)
        except (AttributeError, OSError, TypeError):
            descriptor = None
            override_stat = None
            override_seals = 0
        managed_ready = (
            type(descriptor) is int
            and descriptor >= 3
            and type(override_descriptor) is int
            and override_descriptor >= 3
            and isinstance(nonce, str)
            and override_path == Path(f"/proc/self/fd/{override_descriptor}")
            and override_stat is not None
            and stat.S_ISREG(override_stat.st_mode)
            and override_stat.st_uid == os.geteuid()
            and stat.S_IMODE(override_stat.st_mode) == 0o400
            and override_stat.st_size > 0
            and override_seals & required_seals == required_seals
            and _managed_generation_zero_matches(managed_capture)
        )
        if managed_ready:
            beet_env[_OWNERSHIP_FD_ENV] = str(descriptor)
            beet_env[_OWNERSHIP_NONCE_ENV] = nonce
            beet_env[_OWNERSHIP_SOURCE_ROOTS_ENV] = json.dumps(
                [os.path.abspath(os.fspath(path)) for path in paths]
            )
            beet_env[_OWNERSHIP_MODE_ENV] = _MANAGED_OWNERSHIP_MODE
            pass_fds = set(popen_options.get("pass_fds", ()))
            pass_fds.add(descriptor)
            pass_fds.add(override_descriptor)
            popen_options["pass_fds"] = tuple(sorted(pass_fds))
        else:
            return False, "error"

    def _handle_beets_output_line(line):
        out_lines.append(line)
        stripped = line.strip()
        if not stripped:
            return
        # Staging-path echoes are too noisy for the log, but they're the
        # signal that tells us which album beets is on.
        album = _beets_progress_album(stripped, staging_root)
        if album is not None:
            if album != last_album[0]:
                last_album[0] = album
                seen_albums[0] += 1
                report_progress("Importing into your library", seen_albums[0], total_albums, album)
            return
        # Route through the shared logger so the web UI's SSE stream
        # sees beets output too (raw sys.stdout would only land in
        # docker logs, leaving the job page silent during import).
        log.info(fmt(C.GRAY, "    " + line.rstrip()))

    def _read_beets_output():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = [""]

        def consume(chunk):
            last_output[0] = time.monotonic()
            pending[0] += decoder.decode(chunk)
            while "\n" in pending[0]:
                line, pending[0] = pending[0].split("\n", 1)
                _handle_beets_output_line(line + "\n")

        _read_cancellable_beets_pipe(proc.stdout, reader_stop, consume)
        pending[0] += decoder.decode(b"", final=True)
        if pending[0]:
            _handle_beets_output_line(pending[0])

    reader_start = threading.Event()
    reader_done = threading.Event()
    reader_stop = threading.Event()
    reader_cancelled = [False]

    def _reader():
        try:
            reader_start.wait()
            if not reader_cancelled[0]:
                _read_beets_output()
        finally:
            reader_done.set()

    prune_capture = _capture_beets_prune_roots(paths)
    process_owner = {"process": None}
    proc = None
    reader = None
    wait_error = None
    retirement_error = None
    spawn_failed = False
    try:
        try:

            def prepare_managed_spawn():
                if before_managed_spawn is not None:
                    before_managed_spawn()
                _append_managed_launch_boundary_for_runtime(managed_capture)
                if before_managed_spawn is not None:
                    before_managed_spawn()

            def spawn():
                return _spawn_owned_beets_process(
                    process_owner,
                    cmd,
                    database_exclusion=database_exclusion,
                    before_spawn=(
                        prepare_managed_spawn
                        if isinstance(managed_capture, dict)
                        else (
                            (lambda: _require_beets_runtime(runtime))
                            if runtime is not None
                            else None
                        )
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=beet_env,
                    # Own session so a terminal Ctrl-C doesn't hit beets
                    # mid-import directly. The controlled path retires it.
                    start_new_session=True,
                    **popen_options,
                )

            proc = (
                _run_managed_sigint_deferred(spawn)
                if isinstance(managed_capture, dict)
                else spawn()
            )
        except BaseException as exc:
            wait_error = exc
            spawn_failed = True

        if wait_error is None:
            # Carry the spawning job's context onto the reader thread so its
            # output reaches the job-specific SSE stream.
            reader = threading.Thread(target=wrap_thread_target(_reader), daemon=True)
            try:
                reader.start()
                reader_start.set()
                _wait_or_idle_kill(proc, last_output)
            except BaseException as exc:
                if reader.ident is None:
                    reader_cancelled[0] = True
                reader_start.set()
                wait_error = exc
    finally:
        proc = process_owner.get("process")
        reader_stop.set()
        if "thread" in process_owner:
            retirement_error = _retire_beets_process(process_owner)
        if reader is not None and reader.ident is not None:
            reader_error = _finish_beets_thread(reader, reader_done)
            if retirement_error is None:
                retirement_error = reader_error
        if proc is not None:
            stream_error = _close_beets_process_streams(proc)
            if retirement_error is None:
                retirement_error = stream_error
        if isinstance(managed_capture, dict):
            managed_capture["spawned"] = process_owner.get("spawn_attempted") is True
        try:
            if wait_error is None and retirement_error is None:
                _prune_captured_beets_directories(prune_capture)
        finally:
            _close_beets_prune_capture(prune_capture)

    if retirement_error is not None:
        if wait_error is not None and hasattr(wait_error, "add_note"):
            wait_error.add_note(f"beets process cleanup also failed: {retirement_error}")
        else:
            wait_error = retirement_error
    if spawn_failed and isinstance(wait_error, OSError):
        log.info(
            fmt(
                C.RED,
                f"  ✗  Couldn't start the configured Beets runtime: {wait_error}",
            )
        )
        return False, "error"
    if isinstance(wait_error, subprocess.TimeoutExpired):
        # No output for BEETS_TIMEOUT s → genuinely hung (a slow but
        # progressing import keeps printing, so it never reaches here).
        # Left unbounded this freezes the single web job worker for good.
        _reclaim_ownership_capture(ownership_capture)
        clear_scan_caches()
        log.info(
            fmt(
                C.RED,
                f"  ✗  beets import produced no output for {cfg.BEETS_TIMEOUT}s "
                f"and was killed as hung. Files remain in staging for a "
                f"manual retry. (Raise/disable with BEETS_TIMEOUT.)",
            )
        )
        _report_staging_remnants()
        return False, "timeout"
    if wait_error is not None:
        _reclaim_ownership_capture(ownership_capture)
        clear_scan_caches()
        raise wait_error.with_traceback(wait_error.__traceback__)

    full_out = "".join(out_lines)
    ownership_payload = _read_ownership_payload(ownership_capture)
    ownership_result = (
        ownership_payload
        if ownership_payload is not None and ownership_payload.get("sealed") is True
        else None
    )
    managed_result = (
        _read_managed_ownership_evidence(
            managed_capture,
            database_anchor=database_anchor,
        )
        if managed_capture is not None
        else None
    )

    # beets moves audio out of staging into the library, so the honest success
    # signal is whether the staged tracks actually left, not a phrase in beets'
    # output. beets prints "Skipping." per item (a duplicate, an unreadable
    # file), so matching that text flagged a whole album failed when a single
    # track was skipped; an exit-0 run that moved nothing is the real silent skip.
    audio_after = _count_audio_under(paths, include_retry=explicit_paths)
    moved_any = audio_before > 0 and audio_after < audio_before
    # An explicit album import that finds zero audio (every track quarantined as
    # untagged before the run) imported nothing. It must not read as success, or
    # the executor marks a never-imported album done instead of parking/re-ripping.
    imported_nothing = proc.returncode == 0 and (
        (audio_before > 0 and not moved_any) or (explicit_paths and audio_before == 0)
    )
    receipt_incomplete = False
    if proc.returncode == 0 and intended_audio is not None:
        # A successful explicit import consumes every exact source name. A
        # receipt still present at that name, a replacement at the same name,
        # or any audio left anywhere below the explicit roots makes the whole
        # import fail closed.
        receipt_incomplete = audio_after != 0
        if not receipt_incomplete:
            for receipt in intended_audio:
                source_path = receipt["path"] if managed_capture is not None else receipt.path
                try:
                    os.stat(source_path, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    receipt_incomplete = True
                    break
                else:
                    receipt_incomplete = True
                    break
    managed_incomplete = managed_capture is not None and managed_result is None

    clear_scan_caches()

    if (
        proc.returncode == 0
        and not imported_nothing
        and not receipt_incomplete
        and not managed_incomplete
    ):
        if ownership_result is not None and isinstance(ownership_capture, dict):
            ownership_capture.clear()
            ownership_capture["result"] = ownership_result
        else:
            _reclaim_ownership_capture(ownership_capture, ownership_payload)
        if managed_result is not None and isinstance(managed_capture, dict):
            managed_capture["result"] = managed_result
        log.info(fmt(C.GREEN, "  ✓  beets import succeeded."))
        if audio_after:
            log.info(
                fmt(
                    C.GRAY,
                    f"     {audio_after} staged track(s) were not imported "
                    "and remain in staging.",
                )
            )
        return True, "ok"
    if managed_incomplete and proc.returncode == 0:
        log.info(
            fmt(
                C.RED,
                "  ✗  beets did not produce complete managed import evidence; "
                "the import was not accepted.",
            )
        )
        _report_staging_remnants()
        return False, "error"
    if receipt_incomplete:
        _reclaim_ownership_capture(ownership_capture, ownership_payload)
        log.info(
            fmt(
                C.RED,
                "  ✗  beets exited 0 but did not consume every exact staged "
                "track; the import was not accepted.",
            )
        )
        _report_staging_remnants()
        return False, "error"
    if imported_nothing:
        _reclaim_ownership_capture(ownership_capture, ownership_payload)
        log.info(fmt(C.RED, "  ✗  beets exited 0 but moved nothing into the library."))
        log.info(fmt(C.GRAY, "     Files remain in staging. Try manually:"))
        log.info(fmt(C.GRAY, f"     {' '.join(cmd)}"))
        _report_staging_remnants()
        return False, "error"
    _reclaim_ownership_capture(ownership_capture, ownership_payload)
    log.info(fmt(C.RED, f"  ✗  beets exited {proc.returncode}."))
    log.info(fmt(C.GRAY, f"     Config: {cfg.BEETS_CONFIG_DIR}/config.yaml"))
    if override_path:
        log.info(fmt(C.GRAY, f"     Override: {override_path}"))
    for line in full_out.splitlines()[-20:]:
        log.info(fmt(C.GRAY, f"    {line}"))
    _report_staging_remnants()
    return False, "error"


def _report_staging_remnants():
    """List the top-level album folders still in staging after a beets
    failure so the user knows exactly what didn't import. A bare
    "files remain in staging" line leaves them guessing across batches
    of 100+ albums."""
    try:
        folders = sorted(
            p for p in cfg.STAGING_DIR.iterdir() if p.is_dir() and p.name != cfg.BEETS_RETRY_DIR
        )
    except OSError:
        return
    if not folders:
        return
    log.info(fmt(C.GRAY, f"     Staged folders remaining ({len(folders)}):"))
    audio_exts = set(cfg.AUDIO_EXTS)
    for folder in folders[:20]:
        try:
            n_audio = sum(
                1 for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in audio_exts
            )
        except OSError:
            n_audio = 0
        log.info(fmt(C.GRAY, f"       · {folder.name} ({n_audio} track(s))"))
    if len(folders) > 20:
        log.info(fmt(C.GRAY, f"       … and {len(folders) - 20} more"))


def _untracked_reimport_file():
    return cfg.DATA_DIR / ".beets_untracked_reimport"


def _warn_legacy_untracked_library_dirs():
    """Surface recovery state left by the retired remove/re-import fold.

    Older releases could remove album rows and then fail before their matching
    ``beet import`` completed. Replaying those paths automatically is not safe:
    an as-is import can still discard database-only metadata. Keep the recovery
    file intact for a person to inspect instead.
    """
    f = _untracked_reimport_file()
    if not f.exists():
        return
    try:
        count = len(
            {line.strip() for line in f.read_text(encoding="utf-8").splitlines() if line.strip()}
        )
    except OSError:
        return
    if count:
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  Found {count} album path(s) in the legacy beets recovery "
                f"list at {f}. Automatic re-import is disabled because it can "
                "discard database-only metadata; review those paths manually.",
            )
        )


_CONSOLIDATION_PROTECT_ALL = object()


def _consolidation_absolute_path(value):
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("invalid consolidation path")
    return Path(os.path.abspath(raw))


def _consolidation_protected_paths(paths):
    if paths is _CONSOLIDATION_PROTECT_ALL:
        return paths
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = (paths,)
    protected = set()
    try:
        candidates = tuple(paths or ())
    except (TypeError, ValueError):
        return _CONSOLIDATION_PROTECT_ALL
    for path in candidates:
        try:
            protected.add(_consolidation_absolute_path(path))
        except (OSError, TypeError, ValueError):
            return _CONSOLIDATION_PROTECT_ALL
    return protected


def _consolidation_path_is_protected(path, protected_paths):
    protected = _consolidation_protected_paths(protected_paths)
    if protected is _CONSOLIDATION_PROTECT_ALL:
        return True
    try:
        candidate = _consolidation_absolute_path(path)
        for protected_path in protected:
            if (
                candidate == protected_path
                or candidate.is_relative_to(protected_path)
                or protected_path.is_relative_to(candidate)
            ):
                return True
    except (OSError, TypeError, ValueError):
        return True
    return False


def _sql_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def _consolidation_columns(connection, table):
    rows = connection.execute(f"PRAGMA table_info({_sql_identifier(table)})").fetchall()
    return tuple(row[1] for row in rows)


def _consolidation_row_key(row):
    """Keep SQLite storage classes distinct while comparing raw records."""
    return tuple((type(value), value) for value in row)


def _catalogue_item_path(value):
    raw = os.fspath(value)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("invalid beets item path")
    root = os.fspath(cfg.MUSIC_ROOT)
    if isinstance(root, bytes):
        root = os.fsdecode(root)
    if not isinstance(root, str) or not root or "\x00" in root:
        raise ValueError("invalid beets library path")
    root = os.path.abspath(root)
    if not os.path.isabs(raw):
        raw = os.path.join(root, raw)
    path = os.path.abspath(raw)
    try:
        relative = Path(path).relative_to(root)
    except ValueError:
        raise ValueError("beets item is outside the library") from None
    if not relative.parts:
        raise ValueError("invalid beets item path")
    return Path(path)


def _consolidation_item_dir(value):
    path = _catalogue_item_path(value)
    directory = path.parent
    if directory == path:
        raise ValueError("invalid beets album directory")
    return str(directory)


def _consolidation_disc_parent(directory):
    path = Path(directory)
    name = path.name.casefold()
    for prefix in ("disc", "cd"):
        if name.startswith(prefix):
            number = name[len(prefix) :].strip()
            if number.isascii() and number.isdigit() and int(number) > 0:
                return str(path.parent)
    return None


def _consolidation_rows_for_ids(connection, table, columns, identity_column, identities):
    identities = tuple(identities)
    if not identities:
        return []
    placeholders = ",".join("?" for _ in identities)
    selected = ",".join(_sql_identifier(column) for column in columns)
    order = _sql_identifier("id" if "id" in columns else columns[0])
    return connection.execute(
        f"SELECT {selected} FROM {_sql_identifier(table)} "
        f"WHERE {_sql_identifier(identity_column)} IN ({placeholders}) "
        f"ORDER BY {order}",
        identities,
    ).fetchall()


def _consolidation_album_attributes(connection, columns, album_ids):
    entity_index = columns.index("entity_id")
    structural = {columns.index("id"), entity_index}
    grouped = {album_id: set() for album_id in album_ids}
    rows = _consolidation_rows_for_ids(
        connection,
        "album_attributes",
        columns,
        "entity_id",
        album_ids,
    )
    for row in rows:
        album_id = row[entity_index]
        grouped.setdefault(album_id, set()).add(
            tuple(
                (type(value), value) for index, value in enumerate(row) if index not in structural
            )
        )
    return grouped, rows


@dataclass(frozen=True)
class BeetsAlbumSnapshot:
    item_columns: tuple
    item_rows: tuple
    item_attribute_columns: tuple
    item_attribute_rows: tuple
    album_columns: tuple
    album_rows: tuple
    album_attribute_columns: tuple
    album_attribute_rows: tuple

    @property
    def item_ids(self):
        if not self.item_columns:
            return ()
        index = self.item_columns.index("id")
        return tuple(row[index] for row in self.item_rows)

    @property
    def album_ids(self):
        if not self.album_columns:
            return ()
        index = self.album_columns.index("id")
        return tuple(row[index] for row in self.album_rows)


def _replacement_catalogue_columns(connection):
    known_tables = {
        "albums",
        "items",
        "album_attributes",
        "item_attributes",
        "migrations",
    }
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    required = {"albums", "items", "album_attributes", "item_attributes"}
    if not required.issubset(tables) or not tables.issubset(known_tables):
        raise sqlite3.DatabaseError("unrecognised beets database schema")
    for table in tables:
        if connection.execute(
            f"PRAGMA foreign_key_list({_sql_identifier(table)})"
        ).fetchone() is not None:
            raise sqlite3.DatabaseError("foreign keys make exact retirement unverifiable")
    if connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name IN ('albums', 'items', 'album_attributes', "
        "'item_attributes') LIMIT 1"
    ).fetchone() is not None:
        raise sqlite3.DatabaseError("custom triggers make exact retirement unverifiable")

    album_columns = _consolidation_columns(connection, "albums")
    item_columns = _consolidation_columns(connection, "items")
    album_attribute_columns = _consolidation_columns(connection, "album_attributes")
    item_attribute_columns = _consolidation_columns(connection, "item_attributes")
    if (
        "id" not in album_columns
        or not {"id", "album_id", "path"}.issubset(item_columns)
        or not {"id", "entity_id"}.issubset(album_attribute_columns)
        or not {"id", "entity_id"}.issubset(item_attribute_columns)
    ):
        raise sqlite3.DatabaseError("unrecognised beets database schema")
    return (
        album_columns,
        item_columns,
        album_attribute_columns,
        item_attribute_columns,
    )


def capture_beets_album_entries(album_dir):
    """Capture the exact Beets rows owned by one album folder."""
    try:
        root = _catalogue_item_path(Path(album_dir) / "placeholder").parent
    except (OSError, TypeError, ValueError):
        return None

    database_anchor = None
    try:
        database_anchor = _open_beets_database_anchor()
        if database_anchor is None:
            return None
        if database_anchor["descriptor"] is None:
            return BeetsAlbumSnapshot((), (), (), (), (), (), (), ())

        def inspect(connection):
            (
                album_columns,
                item_columns,
                album_attribute_columns,
                item_attribute_columns,
            ) = _replacement_catalogue_columns(connection)
            item_id_index = item_columns.index("id")
            item_path_index = item_columns.index("path")
            item_album_index = item_columns.index("album_id")
            selected = []
            for row in connection.execute(
                f"SELECT {','.join(_sql_identifier(column) for column in item_columns)} "
                "FROM items ORDER BY id"
            ):
                item_path = _catalogue_item_path(row[item_path_index])
                if item_path == root or root in item_path.parents:
                    item_id = row[item_id_index]
                    album_id = row[item_album_index]
                    if type(item_id) is not int or item_id <= 0:
                        raise sqlite3.DatabaseError("invalid beets item identity")
                    if album_id is not None and (type(album_id) is not int or album_id <= 0):
                        raise sqlite3.DatabaseError("invalid beets album identity")
                    selected.append(tuple(row))
            if len(selected) > 4096:
                raise sqlite3.DatabaseError("album has too many beets entries")

            item_ids = tuple(row[item_id_index] for row in selected)
            album_ids = tuple(
                sorted(
                    {
                        row[item_album_index]
                        for row in selected
                        if row[item_album_index] is not None
                    }
                )
            )
            item_attribute_rows = _consolidation_rows_for_ids(
                connection,
                "item_attributes",
                item_attribute_columns,
                "entity_id",
                item_ids,
            )
            album_rows = _consolidation_rows_for_ids(
                connection, "albums", album_columns, "id", album_ids
            )
            if len(album_rows) != len(album_ids):
                raise sqlite3.DatabaseError("beets album identity is incomplete")
            album_attribute_rows = _consolidation_rows_for_ids(
                connection,
                "album_attributes",
                album_attribute_columns,
                "entity_id",
                album_ids,
            )
            return BeetsAlbumSnapshot(
                tuple(item_columns),
                tuple(selected),
                tuple(item_attribute_columns),
                tuple(item_attribute_rows),
                tuple(album_columns),
                tuple(album_rows),
                tuple(album_attribute_columns),
                tuple(album_attribute_rows),
            )

        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        return inspect_sqlite_source(
            database_anchor,
            _beets_database_anchor_matches,
            inspect,
            connect=sqlite3.connect,
            timeout=timeout,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        _close_beets_database_anchor(database_anchor)


def retire_replaced_beets_entries(snapshot, replacement_dir, replacement_paths):
    """Retire only captured rows after proving the replacement catalogue."""
    if not isinstance(snapshot, BeetsAlbumSnapshot):
        return False
    try:
        replacement_root = _catalogue_item_path(Path(replacement_dir) / "placeholder").parent
        expected_paths = tuple(
            sorted({_catalogue_item_path(path) for path in replacement_paths})
        )
        if (
            not expected_paths
            or len(expected_paths) > 4096
            or any(path.parent != replacement_root and replacement_root not in path.parents
                   for path in expected_paths)
        ):
            return False
    except (OSError, TypeError, ValueError):
        return False

    database_anchor = None
    transaction = None
    try:
        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        database_anchor = _open_beets_database_anchor()
        if database_anchor is None or database_anchor["descriptor"] is None:
            return False
        transaction = AtomicSQLiteWrite(
            database_anchor,
            _beets_database_anchor_matches,
            connect=sqlite3.connect,
            timeout=timeout,
        )
        connection = transaction.open()
        connection.execute("BEGIN IMMEDIATE")
        (
            album_columns,
            item_columns,
            album_attribute_columns,
            item_attribute_columns,
        ) = _replacement_catalogue_columns(connection)
        if snapshot.item_columns and (
            tuple(item_columns) != snapshot.item_columns
            or tuple(item_attribute_columns) != snapshot.item_attribute_columns
            or tuple(album_columns) != snapshot.album_columns
            or tuple(album_attribute_columns) != snapshot.album_attribute_columns
        ):
            raise sqlite3.DatabaseError("beets schema changed during replacement")

        item_ids = snapshot.item_ids
        current_items = _consolidation_rows_for_ids(
            connection, "items", item_columns, "id", item_ids
        )
        if tuple(map(_consolidation_row_key, current_items)) != tuple(
            map(_consolidation_row_key, snapshot.item_rows)
        ):
            raise sqlite3.IntegrityError("captured beets items changed")
        current_item_attributes = _consolidation_rows_for_ids(
            connection,
            "item_attributes",
            item_attribute_columns,
            "entity_id",
            item_ids,
        )
        if tuple(map(_consolidation_row_key, current_item_attributes)) != tuple(
            map(_consolidation_row_key, snapshot.item_attribute_rows)
        ):
            raise sqlite3.IntegrityError("captured beets item metadata changed")

        item_id_index = item_columns.index("id")
        item_path_index = item_columns.index("path")
        captured_ids = set(item_ids)
        replacement_rows = {path: [] for path in expected_paths}
        for row in connection.execute(
            f"SELECT {','.join(_sql_identifier(column) for column in item_columns)} "
            "FROM items ORDER BY id"
        ):
            item_id = row[item_id_index]
            path = _catalogue_item_path(row[item_path_index])
            if item_id in captured_ids:
                continue
            if path in replacement_rows:
                replacement_rows[path].append(item_id)
            elif path == replacement_root or replacement_root in path.parents:
                raise sqlite3.IntegrityError("replacement catalogue has an unexpected item")
        if any(len(ids) != 1 for ids in replacement_rows.values()):
            raise sqlite3.IntegrityError("replacement catalogue is incomplete")

        retiring_album_ids = []
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            for album_id in snapshot.album_ids:
                remaining = connection.execute(
                    f"SELECT 1 FROM items WHERE album_id = ? "
                    f"AND id NOT IN ({placeholders}) LIMIT 1",
                    (album_id, *item_ids),
                ).fetchone()
                if remaining is None:
                    retiring_album_ids.append(album_id)

            if retiring_album_ids:
                album_id_index = snapshot.album_columns.index("id")
                expected_albums = tuple(
                    row
                    for row in snapshot.album_rows
                    if row[album_id_index] in retiring_album_ids
                )
                current_albums = _consolidation_rows_for_ids(
                    connection,
                    "albums",
                    album_columns,
                    "id",
                    retiring_album_ids,
                )
                if tuple(map(_consolidation_row_key, current_albums)) != tuple(
                    map(_consolidation_row_key, expected_albums)
                ):
                    raise sqlite3.IntegrityError("captured beets albums changed")
                expected_attributes = tuple(
                    row
                    for row in snapshot.album_attribute_rows
                    if row[snapshot.album_attribute_columns.index("entity_id")]
                    in retiring_album_ids
                )
                current_attributes = _consolidation_rows_for_ids(
                    connection,
                    "album_attributes",
                    album_attribute_columns,
                    "entity_id",
                    retiring_album_ids,
                )
                if tuple(map(_consolidation_row_key, current_attributes)) != tuple(
                    map(_consolidation_row_key, expected_attributes)
                ):
                    raise sqlite3.IntegrityError("captured beets album metadata changed")

            deleted_attributes = connection.execute(
                f"DELETE FROM item_attributes WHERE entity_id IN ({placeholders})",
                item_ids,
            )
            if deleted_attributes.rowcount != len(snapshot.item_attribute_rows):
                raise sqlite3.IntegrityError("captured beets item metadata changed")
            deleted_items = connection.execute(
                f"DELETE FROM items WHERE id IN ({placeholders})", item_ids
            )
            if deleted_items.rowcount != len(item_ids):
                raise sqlite3.IntegrityError("captured beets items changed")

        if retiring_album_ids:
            placeholders = ",".join("?" for _ in retiring_album_ids)
            expected_attribute_count = sum(
                1
                for row in snapshot.album_attribute_rows
                if row[snapshot.album_attribute_columns.index("entity_id")]
                in retiring_album_ids
            )
            deleted_attributes = connection.execute(
                f"DELETE FROM album_attributes WHERE entity_id IN ({placeholders})",
                retiring_album_ids,
            )
            if deleted_attributes.rowcount != expected_attribute_count:
                raise sqlite3.IntegrityError("captured beets album metadata changed")
            deleted_albums = connection.execute(
                f"DELETE FROM albums WHERE id IN ({placeholders})", retiring_album_ids
            )
            if deleted_albums.rowcount != len(retiring_album_ids):
                raise sqlite3.IntegrityError("captured beets albums changed")

        if not item_ids:
            connection.rollback()
            return True

        def validate(current):
            if current.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            if _consolidation_rows_for_ids(
                current, "items", item_columns, "id", item_ids
            ):
                return False
            counts = {path: 0 for path in expected_paths}
            for item_id, raw_path in current.execute("SELECT id, path FROM items"):
                if item_id in captured_ids:
                    return False
                path = _catalogue_item_path(raw_path)
                if path in counts:
                    counts[path] += 1
            return all(count == 1 for count in counts.values())

        transaction.commit_and_publish(
            lambda: _beets_database_anchor_matches(database_anchor), validate
        )
        clear_scan_caches()
        return True
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        outcome = (
            "the committed catalogue change may already be visible"
            if transaction is not None and transaction.published
            else "leaving the captured entries unchanged"
        )
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  Couldn't safely retire the replaced Beets entries ({exc}); "
                f"{outcome}.",
            )
        )
        return False
    finally:
        if transaction is not None:
            transaction.close()
        _close_beets_database_anchor(database_anchor)


def _receipt_audio_paths(receipt, root):
    """Resolve the audio paths sealed by one backup or replacement receipt."""
    if not isinstance(receipt, dict):
        return None
    try:
        files = receipt["tree"]["files"]
        if not isinstance(files, dict):
            return None
        root = _catalogue_item_path(Path(root) / "placeholder").parent
        paths = []
        for relative_text in files:
            if type(relative_text) is not str:
                return None
            relative = PurePosixPath(relative_text)
            if (
                not relative_text
                or relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.suffix.lower() not in cfg.AUDIO_EXTS
            ):
                continue
            path = _catalogue_item_path(
                root.joinpath(*relative.parts)
            )
            if path == root or root not in path.parents:
                return None
            paths.append(path)
        paths = tuple(sorted(set(paths)))
        return paths if paths else None
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _backup_receipt(value):
    if isinstance(value, dict):
        return value.get("receipt")
    return getattr(value, "receipt", None)


def retire_backup_beets_entries(backup, replacement_dir, replacement_receipt):
    """Retire exact old rows before disposing a moved-aside backup."""
    backup_receipt = _backup_receipt(backup)
    try:
        old_root = backup_receipt["origin"]
        old_paths = _receipt_audio_paths(backup_receipt, old_root)
        replacement_paths = _receipt_audio_paths(
            replacement_receipt, replacement_dir
        )
        if old_paths is None or replacement_paths is None:
            return False
        old_paths = frozenset(old_paths)
        replacement_paths = frozenset(replacement_paths)
        old_only_paths = old_paths - replacement_paths
        if (
            len(old_paths) > 4096
            or len(replacement_paths) > 4096
        ):
            return False
        if any(os.path.lexists(path) for path in old_only_paths):
            return False
        if any(
            path.is_symlink() or not path.is_file()
            for path in replacement_paths
        ):
            return False
    except (OSError, TypeError, ValueError, KeyError):
        return False

    database_anchor = None
    transaction = None
    try:
        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        database_anchor = _open_beets_database_anchor()
        if database_anchor is None or database_anchor["descriptor"] is None:
            return False
        transaction = AtomicSQLiteWrite(
            database_anchor,
            _beets_database_anchor_matches,
            connect=sqlite3.connect,
            timeout=timeout,
        )
        connection = transaction.open()
        connection.execute("BEGIN IMMEDIATE")
        (
            album_columns,
            item_columns,
            album_attribute_columns,
            item_attribute_columns,
        ) = _replacement_catalogue_columns(connection)
        item_id_index = item_columns.index("id")
        item_album_index = item_columns.index("album_id")
        item_path_index = item_columns.index("path")
        music_root = _catalogue_item_path(
            Path(cfg.MUSIC_ROOT) / "placeholder"
        ).parent
        replacement_relatives = {
            path.relative_to(music_root).as_posix()
            for path in replacement_paths
        }
        catalogue_album = _managed_catalogue_album(
            connection, replacement_relatives, music_root
        )
        if catalogue_album is None:
            raise sqlite3.IntegrityError("exact Beets replacement proof failed")
        replacement_album_id, _replacement_album_paths = catalogue_album
        retiring_ids = []
        retiring_album_ids = set()
        replacement_rows = {path: [] for path in replacement_paths}
        for row in connection.execute(
            f"SELECT {','.join(_sql_identifier(column) for column in item_columns)} "
            "FROM items ORDER BY id"
        ):
            item_id = row[item_id_index]
            path = _catalogue_item_path(row[item_path_index])
            if path not in old_paths and path not in replacement_rows:
                continue
            if type(item_id) is not int or item_id <= 0:
                raise sqlite3.DatabaseError("invalid beets item identity")
            album_id = row[item_album_index]
            if path in replacement_rows:
                if type(album_id) is not int or album_id <= 0:
                    raise sqlite3.DatabaseError("invalid beets album identity")
                replacement_rows[path].append((item_id, album_id))
            if path in old_paths and (
                path not in replacement_rows
                or album_id != replacement_album_id
            ):
                retiring_ids.append(item_id)
                if album_id is not None:
                    if type(album_id) is not int or album_id <= 0:
                        raise sqlite3.DatabaseError("invalid beets album identity")
                    retiring_album_ids.add(album_id)
        replacement_proven = all(
            sum(
                album_id == replacement_album_id
                for _item_id, album_id in rows
            )
            == 1
            and all(
                album_id == replacement_album_id or path in old_paths
                for _item_id, album_id in rows
            )
            for path, rows in replacement_rows.items()
        )
        if (
            len(retiring_ids) > 4096
            or len(retiring_ids) != len(set(retiring_ids))
            or not replacement_proven
        ):
            raise sqlite3.IntegrityError("exact Beets replacement proof failed")
        if not retiring_ids:
            connection.rollback()
            return True

        placeholders = ",".join("?" for _ in retiring_ids)
        expected_item_attributes = _consolidation_rows_for_ids(
            connection,
            "item_attributes",
            item_attribute_columns,
            "entity_id",
            retiring_ids,
        )
        deleted_attributes = connection.execute(
            f"DELETE FROM item_attributes WHERE entity_id IN ({placeholders})",
            retiring_ids,
        )
        if deleted_attributes.rowcount != len(expected_item_attributes):
            raise sqlite3.IntegrityError("Beets item metadata changed")
        deleted_items = connection.execute(
            f"DELETE FROM items WHERE id IN ({placeholders})",
            retiring_ids,
        )
        if deleted_items.rowcount != len(retiring_ids):
            raise sqlite3.IntegrityError("Beets items changed")

        empty_album_ids = []
        for album_id in sorted(retiring_album_ids):
            if connection.execute(
                "SELECT 1 FROM items WHERE album_id = ? LIMIT 1", (album_id,)
            ).fetchone() is None:
                empty_album_ids.append(album_id)
        if empty_album_ids:
            placeholders = ",".join("?" for _ in empty_album_ids)
            expected_album_attributes = _consolidation_rows_for_ids(
                connection,
                "album_attributes",
                album_attribute_columns,
                "entity_id",
                empty_album_ids,
            )
            deleted_attributes = connection.execute(
                f"DELETE FROM album_attributes WHERE entity_id IN ({placeholders})",
                empty_album_ids,
            )
            if deleted_attributes.rowcount != len(expected_album_attributes):
                raise sqlite3.IntegrityError("Beets album metadata changed")
            deleted_albums = connection.execute(
                f"DELETE FROM albums WHERE id IN ({placeholders})",
                empty_album_ids,
            )
            if deleted_albums.rowcount != len(empty_album_ids):
                raise sqlite3.IntegrityError("Beets albums changed")

        def validate(current):
            if current.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            counts = {path: [] for path in replacement_paths}
            for item_id, album_id, raw_path in current.execute(
                "SELECT id, album_id, path FROM items"
            ):
                path = _catalogue_item_path(raw_path)
                if item_id in retiring_ids or path in old_only_paths:
                    return False
                if path in counts:
                    counts[path].append(album_id)
            return all(
                album_ids == [replacement_album_id]
                for album_ids in counts.values()
            )

        transaction.commit_and_publish(
            lambda: _beets_database_anchor_matches(database_anchor), validate
        )
        clear_scan_caches()
        return True
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        outcome = (
            "the committed catalogue change may already be visible"
            if transaction is not None and transaction.published
            else "leaving the catalogue unchanged"
        )
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  Couldn't safely retire the backup's Beets entries ({exc}); "
                f"{outcome}.",
            )
        )
        return False
    finally:
        if transaction is not None:
            transaction.close()
        _close_beets_database_anchor(database_anchor)


def _fold_duplicate_album_group(
    connection,
    album_ids,
    album_columns,
    item_columns,
    album_attribute_columns,
    item_attribute_columns,
):
    """Fold one exact duplicate group without touching files or tags."""
    if not 1 < len(album_ids) <= 64:
        return False
    album_rows = _consolidation_rows_for_ids(connection, "albums", album_columns, "id", album_ids)
    if len(album_rows) != len(album_ids):
        return False

    album_id_index = album_columns.index("id")
    added_index = album_columns.index("added")
    artpath_index = album_columns.index("artpath")
    rows_by_id = {row[album_id_index]: row for row in album_rows}
    if set(rows_by_id) != set(album_ids):
        return False

    fixed_indexes = tuple(
        index
        for index, column in enumerate(album_columns)
        if column not in {"id", "added", "artpath"}
    )
    fixed = {tuple((type(row[index]), row[index]) for index in fixed_indexes) for row in album_rows}
    if len(fixed) != 1:
        return False

    added = {}
    for album_id, row in rows_by_id.items():
        value = row[added_index]
        if type(value) not in (int, float) or value != value:
            return False
        added[album_id] = value
    keeper_id = min(album_ids, key=lambda album_id: (added[album_id], album_id))
    losing_ids = tuple(album_id for album_id in album_ids if album_id != keeper_id)

    nonnull_art = {
        (type(row[artpath_index]), row[artpath_index])
        for row in album_rows
        if row[artpath_index] is not None
    }
    if len(nonnull_art) > 1:
        return False
    keeper_art = rows_by_id[keeper_id][artpath_index]
    if nonnull_art and (type(keeper_art), keeper_art) not in nonnull_art:
        return False

    attributes, album_attribute_rows = _consolidation_album_attributes(
        connection, album_attribute_columns, album_ids
    )
    keeper_attributes = attributes.get(keeper_id, set())
    if any(
        not attributes.get(album_id, set()).issubset(keeper_attributes) for album_id in losing_ids
    ):
        return False

    item_rows = _consolidation_rows_for_ids(
        connection, "items", item_columns, "album_id", album_ids
    )
    item_id_index = item_columns.index("id")
    item_album_index = item_columns.index("album_id")
    if not item_rows or len(item_rows) > 4096:
        return False
    item_ids = tuple(row[item_id_index] for row in item_rows)
    if len(set(item_ids)) != len(item_ids):
        return False
    item_attribute_rows = _consolidation_rows_for_ids(
        connection,
        "item_attributes",
        item_attribute_columns,
        "entity_id",
        item_ids,
    )

    placeholders = ",".join("?" for _ in losing_ids)
    moved_count = sum(1 for row in item_rows if row[item_album_index] in losing_ids)
    updated = connection.execute(
        f"UPDATE items SET album_id = ? WHERE album_id IN ({placeholders})",
        (keeper_id, *losing_ids),
    )
    if updated.rowcount != moved_count:
        raise sqlite3.IntegrityError("duplicate album item set changed")

    losing_attribute_count = sum(
        1
        for row in album_attribute_rows
        if row[album_attribute_columns.index("entity_id")] in losing_ids
    )
    deleted_attributes = connection.execute(
        f"DELETE FROM album_attributes WHERE entity_id IN ({placeholders})",
        losing_ids,
    )
    if deleted_attributes.rowcount != losing_attribute_count:
        raise sqlite3.IntegrityError("duplicate album attributes changed")
    deleted_albums = connection.execute(
        f"DELETE FROM albums WHERE id IN ({placeholders})",
        losing_ids,
    )
    if deleted_albums.rowcount != len(losing_ids):
        raise sqlite3.IntegrityError("duplicate album rows changed")

    current_items = _consolidation_rows_for_ids(connection, "items", item_columns, "id", item_ids)
    expected_items = []
    for row in item_rows:
        expected = list(row)
        expected[item_album_index] = keeper_id
        expected_items.append(tuple(expected))
    if tuple(map(_consolidation_row_key, current_items)) != tuple(
        map(_consolidation_row_key, expected_items)
    ):
        raise sqlite3.IntegrityError("duplicate album item data changed")
    current_item_attributes = _consolidation_rows_for_ids(
        connection,
        "item_attributes",
        item_attribute_columns,
        "entity_id",
        item_ids,
    )
    if tuple(map(_consolidation_row_key, current_item_attributes)) != tuple(
        map(_consolidation_row_key, item_attribute_rows)
    ):
        raise sqlite3.IntegrityError("duplicate item metadata changed")
    current_albums = _consolidation_rows_for_ids(
        connection, "albums", album_columns, "id", album_ids
    )
    if len(current_albums) != 1 or _consolidation_row_key(
        current_albums[0]
    ) != _consolidation_row_key(rows_by_id[keeper_id]):
        raise sqlite3.IntegrityError("duplicate keeper metadata changed")
    current_album_attributes = _consolidation_rows_for_ids(
        connection,
        "album_attributes",
        album_attribute_columns,
        "entity_id",
        album_ids,
    )
    expected_album_attributes = [
        row
        for row in album_attribute_rows
        if row[album_attribute_columns.index("entity_id")] == keeper_id
    ]
    if tuple(map(_consolidation_row_key, current_album_attributes)) != tuple(
        map(_consolidation_row_key, expected_album_attributes)
    ):
        raise sqlite3.IntegrityError("duplicate album metadata changed")
    return True


def _consolidate_duplicate_albums(protected_paths=None):
    """Fold provably identical album rows in one rollback-safe DB transaction.

    The fold updates only ``items.album_id`` and removes redundant album rows
    whose fixed fields and flexible metadata are already represented verbatim
    by the oldest row. It never invokes the importer, plugins, tag writers, or
    filesystem mutation APIs. Any uncertainty leaves the duplicates intact.
    """
    _warn_legacy_untracked_library_dirs()
    try:
        database = Path(os.fspath(cfg.BEETS_DB_PATH))
        if not database.is_file():
            return
    except (OSError, TypeError, ValueError):
        return

    protected = _consolidation_protected_paths(protected_paths)
    database_anchor = None
    transaction = None
    connection = None
    merged = 0
    try:
        timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        database_anchor = _open_beets_database_anchor()
        if database_anchor is None or database_anchor["descriptor"] is None:
            return
        transaction = AtomicSQLiteWrite(
            database_anchor,
            _beets_database_anchor_matches,
            connect=sqlite3.connect,
            timeout=timeout,
        )
        connection = transaction.open()
        connection.execute("BEGIN IMMEDIATE")

        known_tables = {
            "albums",
            "items",
            "album_attributes",
            "item_attributes",
            "migrations",
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables.issubset(known_tables):
            raise sqlite3.DatabaseError("unknown tables make a lossless fold unverifiable")
        for table in tables:
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({_sql_identifier(table)})"
            ).fetchone()
            if foreign_keys is not None:
                raise sqlite3.DatabaseError("foreign keys make a lossless fold unverifiable")

        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name IN ('albums', 'items', 'album_attributes', "
            "'item_attributes') LIMIT 1"
        ).fetchone()
        if triggers is not None:
            raise sqlite3.DatabaseError(
                "custom database triggers make a lossless fold unverifiable"
            )

        album_columns = _consolidation_columns(connection, "albums")
        item_columns = _consolidation_columns(connection, "items")
        album_attribute_columns = _consolidation_columns(connection, "album_attributes")
        item_attribute_columns = _consolidation_columns(connection, "item_attributes")
        if (
            not {"id", "added", "artpath"}.issubset(album_columns)
            or not {"id", "album_id", "path"}.issubset(item_columns)
            or not {"id", "entity_id", "key", "value"}.issubset(album_attribute_columns)
            or not {"id", "entity_id", "key", "value"}.issubset(item_attribute_columns)
        ):
            raise sqlite3.DatabaseError("unrecognised beets database schema")

        album_ids = {
            row[0] for row in connection.execute("SELECT id FROM albums") if type(row[0]) is int
        }
        album_directories = {}
        album_order = []
        unsafe_albums = set()
        seen_albums = set()
        for album_id, path in connection.execute(
            "SELECT album_id, path FROM items WHERE album_id IS NOT NULL ORDER BY id"
        ):
            if type(album_id) is not int or album_id not in album_ids:
                continue
            if album_id not in seen_albums:
                album_order.append(album_id)
                seen_albums.add(album_id)
            try:
                directory = _consolidation_item_dir(path)
            except (OSError, TypeError, ValueError):
                unsafe_albums.add(album_id)
                continue
            album_directories.setdefault(album_id, set()).add(directory)

        disc_directories = {}
        for album_id, directories in album_directories.items():
            if album_id in unsafe_albums:
                continue
            parents = {_consolidation_disc_parent(directory) for directory in directories}
            if None not in parents and len(parents) == 1:
                parent = next(iter(parents))
                disc_directories.setdefault(parent, set()).update(directories)
        confirmed_disc_parents = {
            parent for parent, directories in disc_directories.items() if len(directories) > 1
        }

        by_directory = {}
        for album_id in album_order:
            if album_id in unsafe_albums:
                continue
            directories = album_directories.get(album_id, set())
            if len(directories) == 1:
                directory = next(iter(directories))
                disc_parent = _consolidation_disc_parent(directory)
                scope = disc_parent if disc_parent in confirmed_disc_parents else directory
            else:
                parents = {_consolidation_disc_parent(directory) for directory in directories}
                if (
                    None in parents
                    or len(parents) != 1
                    or next(iter(parents)) not in confirmed_disc_parents
                ):
                    continue
                scope = next(iter(parents))
            by_directory.setdefault(scope, []).append(album_id)

        groups = [
            (directory, tuple(ids))
            for directory, ids in by_directory.items()
            if len(ids) > 1 and not any(album_id in unsafe_albums for album_id in ids)
        ]
        for directory, ids in groups:
            if _consolidation_path_is_protected(directory, protected):
                vlog(f"consolidation: keep ownership-protected folder {directory}")
                continue
            if _fold_duplicate_album_group(
                connection,
                ids,
                album_columns,
                item_columns,
                album_attribute_columns,
                item_attribute_columns,
            ):
                merged += 1

        if not merged:
            connection.rollback()
            return
        transaction.commit_and_publish(
            lambda: _beets_database_anchor_matches(database_anchor),
            lambda current: current.execute("PRAGMA quick_check").fetchone() == ("ok",),
        )
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        outcome = (
            "the committed replacement may already be visible"
            if transaction is not None and transaction.published
            else "leaving them unchanged"
        )
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  Couldn't safely fold duplicate beets entries ({exc}); {outcome}.",
            )
        )
        return
    finally:
        if transaction is not None:
            transaction.close()
        if database_anchor is not None:
            _close_beets_database_anchor(database_anchor)

    if merged:
        log.info(
            fmt(
                C.GRAY,
                f"  ⤷  Tidied {merged} album folder(s) beets had split into "
                "duplicate library entries.",
            )
        )
        clear_scan_caches()


@dataclass(frozen=True)
class ForgetBeetsEntriesResult:
    complete: bool
    removed: int = 0


def forget_beets_entries(paths):
    """Drop beets DB entries for files that were deleted outside beets.
    Consolidation removes duplicate sibling tracks straight off disk; without
    this their rows linger in the beets library, so someone who also runs
    `beet` by hand sees ghost tracks until the next `beet update`. `beet
    remove` is used with exact item IDs rather than a direct sqlite delete so
    beets cleans up the album row and any flexible-attribute rows along with
    the item, leaving the database consistent. Resolving those IDs from the
    anchored database also handles root-relative rows when Beets' ordinary
    config names another mount alias for the same managed library.
    """
    paths = [str(p) for p in paths if str(p)]
    if not paths:
        return ForgetBeetsEntriesResult(True)

    def _ghost_warning(reason):
        # The files are already gone from disk, so a remove that can't run
        # leaves their rows behind as ghost entries in `beet ls` until a
        # hand-run `beet update` notices the missing files. Keep the warning
        # alongside the structured failure returned to the caller.
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  Couldn't drop the deleted track(s) from the beets library "
                f"({reason}); they'll linger as ghost entries until you run "
                f"`beet update`.",
            )
        )

    runtime = _resolve_beets_runtime()
    if runtime is None:
        _ghost_warning("a supported Beets runtime could not be found")
        return ForgetBeetsEntriesResult(False)
    config_dir = os.path.abspath(os.fspath(cfg.BEETS_CONFIG_DIR))
    beet_env = {**os.environ, "BEETSDIR": config_dir}
    for variable in _OWNERSHIP_ENV_KEYS:
        beet_env.pop(variable, None)
    base = [
        runtime.python,
        "-I",
        str(_managed_beets_entrypoint()),
        "--run-beets",
        "-l",
        os.path.abspath(os.fspath(cfg.BEETS_DB_PATH)),
    ]
    try:
        target_paths = frozenset(_catalogue_item_path(path) for path in paths)
    except (OSError, TypeError, ValueError) as exc:
        _ghost_warning(f"the deleted path is outside the managed library: {exc}")
        return ForgetBeetsEntriesResult(False)

    def _tracked_ids(stage):
        def _inspect(connection):
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(items)")
            }
            if not {"id", "path"}.issubset(columns):
                raise sqlite3.DatabaseError("beets items schema is incomplete")
            tracked = []
            seen_ids = set()
            for item_id, raw_path in connection.execute(
                "SELECT id, path FROM items ORDER BY id"
            ):
                if type(item_id) is not int or item_id <= 0 or item_id in seen_ids:
                    raise sqlite3.DatabaseError("beets item identity is invalid")
                seen_ids.add(item_id)
                try:
                    item_path = _catalogue_item_path(raw_path)
                except (OSError, TypeError, ValueError) as exc:
                    raise sqlite3.DatabaseError(
                        "beets item path is outside the managed library"
                    ) from exc
                if item_path in target_paths:
                    tracked.append(item_id)
            return tracked

        try:
            timeout = getattr(cfg, "BEETS_TIMEOUT", 5)
            timeout = float(timeout) if timeout and timeout > 0 else 5.0
            return inspect_sqlite_source(
                database_anchor,
                _beets_database_anchor_matches,
                _inspect,
                connect=sqlite3.connect,
                timeout=timeout,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            _ghost_warning(f"couldn't {stage} the library: {exc}")
            return None

    def _run_guarded():
        tracked = _tracked_ids("read")
        if tracked is None:
            return ForgetBeetsEntriesResult(False)
        if not tracked:
            return ForgetBeetsEntriesResult(True)
        query = []
        for item_id in tracked:
            if query:
                query.append(",")
            query.append(f"id:{item_id}")
        try:
            rm = _run_owned_beets_capture(
                base + ["remove", "-f", *query],
                env=beet_env,
                timeout=120,
                database_exclusion=exclusion,
                before_spawn=lambda: _require_beets_runtime(runtime),
            )
            # A nonzero exit is usually a transient DB lock. Retry once
            # before falling back to the manual-recovery message.
            if rm.returncode != 0:
                rm = _run_owned_beets_capture(
                    base + ["remove", "-f", *query],
                    env=beet_env,
                    timeout=120,
                    database_exclusion=exclusion,
                    before_spawn=lambda: _require_beets_runtime(runtime),
                )
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            _ghost_warning(f"beet remove couldn't run: {exc}")
            return ForgetBeetsEntriesResult(False)
        if rm.returncode != 0:
            _ghost_warning(f"beet remove exited {rm.returncode}")
            return ForgetBeetsEntriesResult(False)
        remaining = _tracked_ids("verify")
        if remaining is None:
            return ForgetBeetsEntriesResult(False)
        if remaining:
            _ghost_warning("beet remove left the deleted track(s) catalogued")
            return ForgetBeetsEntriesResult(False)
        return ForgetBeetsEntriesResult(True, len(tracked))

    exclusion = _SQLiteDatabaseExclusion()
    database_anchor = None
    result = ForgetBeetsEntriesResult(False)
    caught = None
    try:
        database_anchor = _open_beets_database_anchor()
        if database_anchor is None:
            raise OSError("configured beets database is unavailable")
        exclusion.acquire(database_anchor["parent_chain"][-1])
        _preflight_beets_database_anchor(database_anchor)
        result = _run_guarded()
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        _ghost_warning(f"couldn't read the library: {exc}")
    except BaseException as exc:
        caught = exc

    exclusion_error = exclusion.release()
    _close_beets_database_anchor(database_anchor)
    if exclusion_error is not None:
        _ghost_warning("the database exclusion could not be released safely")
        result = ForgetBeetsEntriesResult(False)
    if caught is not None:
        if exclusion_error is not None and hasattr(caught, "add_note"):
            caught.add_note("beets database exclusion cleanup also failed")
        raise caught.with_traceback(caught.__traceback__)
    return result


def staging_preflight(args):
    """Sweep streamrip non-audio residue, then handle any remaining leftovers."""
    from qobuz_librarian.integrations.rip import cleanup_staging_residue

    if not cfg.STAGING_DIR.exists():
        cfg.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        return

    n_swept = cleanup_staging_residue()
    if n_swept:
        vlog(f"staging_preflight: swept {n_swept} non-audio residue item(s)")

    isolated_runs = [
        entry
        for entry in cfg.STAGING_DIR.iterdir()
        if entry.is_dir() and _under_isolated_run(entry)
    ]
    if isolated_runs:
        log.info(
            fmt(
                C.YELLOW,
                f"\n  ⚠  {len(isolated_runs)} isolated interrupted download "
                f"run(s) remain in {cfg.STAGING_DIR}; they will not be imported "
                "automatically.",
            )
        )
    files = [
        f
        for f in cfg.STAGING_DIR.rglob("*")
        if (f.is_file() and not _under_retry_dir(f) and not _under_isolated_run(f))
    ]
    if not files:
        return

    log.info(
        fmt(C.YELLOW, f"\n  ⚠  Staging dir not empty: {len(files)} file(s) at {cfg.STAGING_DIR}.")
    )
    sample = [str(f.relative_to(cfg.STAGING_DIR)) for f in files[:5]]
    for s in sample:
        log.info(fmt(C.GRAY, f"     {s}"))
    if len(files) > 5:
        log.info(fmt(C.GRAY, f"     ... and {len(files) - 5} more"))

    if args.yes:
        log.info(
            fmt(
                C.RED + C.BOLD,
                "\n  ✗  Refusing --yes with unclassified staging leftovers. "
                "Review or import them explicitly first.",
            )
        )
        sys.exit(EXIT_GENERAL)

    print()
    log.info(fmt(C.CYAN, "  Options:"))
    log.info(fmt(C.WHITE, "    1) Run beets now to clear staging, then proceed"))
    log.info(fmt(C.WHITE, "    2) Abort"))
    log.info(fmt(C.WHITE, "    3) Proceed anyway (leave leftovers untouched)"))
    try:
        r = input(fmt(C.CYAN, "  Choice [1/2/3]: ")).strip()
    except EOFError:
        r = "2"
    if r == "1":
        # Snapshot each directory that directly holds public audio. Isolated
        # per-rip roots stay outside every hook and the explicit beets command.
        _pre_dirs = []
        _seen_dirs = set()
        for _file in files:
            if _file.suffix.lower() in cfg.AUDIO_EXTS and _file.parent not in _seen_dirs:
                _seen_dirs.add(_file.parent)
                _pre_dirs.append(_file.parent)
        from qobuz_librarian.integrations.downsample_engine import (
            HAVE_DOWNSAMPLE,
            downsample_dir,
        )

        if HAVE_DOWNSAMPLE and not getattr(args, "no_downsample", False):
            try:
                for _directory in _pre_dirs:
                    downsample_dir(_directory, verbose=True, base_dir=cfg.STAGING_DIR, log=log.info)
            except (KeyboardInterrupt, Exception) as _ce:
                if isinstance(_ce, KeyboardInterrupt):
                    sys.exit(EXIT_GENERAL)
                log.info(fmt(C.YELLOW, f"  ⚠  downsample hook failed during preflight: {_ce}."))
        try:
            from qobuz_librarian.integrations.lyrics import _run_lyric_hook

            for _directory in _pre_dirs:
                _run_lyric_hook(_directory)
        except (KeyboardInterrupt, Exception) as _le:
            if isinstance(_le, KeyboardInterrupt):
                sys.exit(EXIT_GENERAL)
            log.info(fmt(C.YELLOW, f"  ⚠  lyric hook failed during preflight: {_le}."))
        # Tag signatures taken before the import: the hook above only EMBEDS
        # lyrics. Sidecar/both modes finish in the post-import pass, same as
        # a queue flush, and the landed folders are only findable this way.
        from qobuz_librarian.library.catalog import (
            find_album_dir_by_track_signatures,
            track_signatures_for_album_dirs,
        )

        _sigs_by_dir = {d: track_signatures_for_album_dirs([d]) for d in _pre_dirs}
        sealed_sources = []
        if not beets_import_paths(source_files_out=sealed_sources, album_dirs=_pre_dirs):
            die(fmt(C.RED, "  beets import failed; aborting."), EXIT_GENERAL)
        _landed = []
        for _d, _sigs in _sigs_by_dir.items():
            if _d.exists() and _count_audio_under([_d]):
                continue
            try:
                _found = find_album_dir_by_track_signatures(_sigs)
            except Exception:
                _found = None
            if _found is not None and _found not in _landed:
                _landed.append(_found)
        if _landed:
            try:
                from qobuz_librarian.integrations.lyrics import (
                    write_post_import_sidecars,
                )

                write_post_import_sidecars(_landed)
            except Exception as _se:
                log.info(fmt(C.YELLOW, f"  ⚠  lyric sidecar pass failed during preflight: {_se}."))
        from qobuz_librarian.integrations.staging import (
            capture_file,
            quarantine_file,
        )

        quarantined = 0
        uncertain_quarantines = 0
        for receipt in sealed_sources:
            if capture_file(receipt.path, expected=receipt.identity) is None:
                continue
            result = quarantine_file(receipt, ".unimported")
            if result is not None:
                quarantined += 1
                if not result.durable:
                    uncertain_quarantines += 1
        leftover = [
            f for f in cfg.STAGING_DIR.rglob("*") if f.is_file() and not _under_retry_dir(f)
        ]
        if quarantined:
            log.info(
                fmt(
                    C.YELLOW,
                    f"  ⚠  Set aside {quarantined} exact unimported file(s) under "
                    f"{cfg.BEETS_RETRY_DIR}/.",
                )
            )
        if uncertain_quarantines:
            log.info(
                fmt(
                    C.YELLOW,
                    f"  ⚠  Directory sync could not be confirmed for "
                    f"{uncertain_quarantines} recovery file(s); their current "
                    "private locations remain recorded.",
                )
            )
        if leftover:
            log.info(
                fmt(
                    C.YELLOW,
                    f"  ⚠  Kept {len(leftover)} changed or unproved staging "
                    "file(s) in place for manual review.",
                )
            )
    elif r == "3":
        log.info(fmt(C.GRAY, "  Proceeding with leftovers in place."))
    else:
        die(fmt(C.GRAY, "  Aborted."), EXIT_GENERAL)
