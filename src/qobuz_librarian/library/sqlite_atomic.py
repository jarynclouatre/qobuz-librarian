"""Identity-bound copy-on-write publication for a local SQLite database.

This module requires Linux's default Unix SQLite VFS and a local filesystem
that supports hardlinks, file leases, ``renameat2(RENAME_EXCHANGE)``, xattrs,
and directory fsync.  It coordinates normal SQLite clients and the
application's own filesystem mutations.  It deliberately refuses unsupported
or ambiguous states; it is not a defence against an arbitrary same-user
process directly rewriting database bytes or names outside SQLite.
"""

import base64
import ctypes
import errno
import fcntl
import hashlib
import io
import json
import os
import secrets
import signal
import sqlite3
import stat
import struct
import sys
import threading
import time
import weakref

from qobuz_librarian.dirfd import digest_fd as _descriptor_digest
from qobuz_librarian.file_exclusion import _LeaseSignalTarget

_RENAME_EXCHANGE = 2
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_RECEIPT_NAME = "receipt.json"
_RECEIPT_VERSION = 1
_MAX_RECEIPT_BYTES = 64 * 1024
_LEASE_LIMIT_SECONDS = 5.0
_F_SETOWN_EX = 15
_F_OWNER_TID = 0
_SOURCE_LEASE = "source"
_WORK_LEASE = "work"
_CHILD_LEASE_NAME = ".qobuz-librarian-beets.lock"
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
_EARLY_PRIVATE_NAMES = frozenset({
    "source.db",
    "work.db",
    "source.db-journal",
    "source.db-wal",
    "source.db-shm",
    "work.db-journal",
    "work.db-wal",
    "work.db-shm",
})


class SQLitePublicationUncertain(OSError):
    """The exact database published at the configured leaf is unknown."""


class SQLiteRecoveryRequired(OSError):
    """A retained transaction cannot be classified without operator review."""


def _identity(value):
    return int(value.st_dev), int(value.st_ino)


def _named_entry_matches(directory_fd, name, descriptor, *, directory=False):
    try:
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        expected_type(held.st_mode)
        and expected_type(named.st_mode)
        and _identity(held) == _identity(named)
    )


def _name_missing(directory_fd, name):
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def canonical_sidecars_clear(anchor):
    if anchor is None:
        return True
    try:
        parent_fd = anchor["parent_chain"][-1]
        name = anchor["name"]
    except (KeyError, IndexError, TypeError):
        return False
    return all(
        _name_missing(parent_fd, f"{name}{suffix}")
        for suffix in _SIDECAR_SUFFIXES
    )


def _read_xattrs(descriptor):
    try:
        names = os.listxattr(descriptor)
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return None
        raise
    return {name: os.getxattr(descriptor, name) for name in names}


def _encoded_xattrs(descriptor):
    values = _read_xattrs(descriptor)
    if values is None:
        return None
    return [
        [
            base64.b64encode(os.fsencode(name)).decode("ascii"),
            base64.b64encode(value).decode("ascii"),
        ]
        for name, value in sorted(values.items(), key=lambda item: os.fsencode(
            item[0]))
    ]


def _stable_descriptor_fields(value):
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
        "links": int(value.st_nlink),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "mode": int(stat.S_IMODE(value.st_mode)),
    }


def _record_fields(value):
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "mode": int(stat.S_IMODE(value.st_mode)),
    }


def _descriptor_record(descriptor):
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("SQLite recovery entry is not a regular file")
    xattrs = _encoded_xattrs(descriptor)
    digest = _descriptor_digest(descriptor)
    after = os.fstat(descriptor)
    if _stable_descriptor_fields(before) != _stable_descriptor_fields(after):
        raise OSError("SQLite database changed while it was fingerprinted")
    if xattrs != _encoded_xattrs(descriptor):
        raise OSError("SQLite database metadata changed while it was fingerprinted")
    return {
        **_record_fields(after),
        "sha256": digest,
        "xattrs": xattrs,
    }


def _record_is_valid(value):
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "size",
        "uid",
        "gid",
        "mode",
        "sha256",
        "xattrs",
    }:
        return False
    integers = (
        "device", "inode", "size", "uid", "gid", "mode",
    )
    if any(type(value[name]) is not int or value[name] < 0 for name in integers):
        return False
    if (
        not isinstance(value["sha256"], str)
        or len(value["sha256"]) != 64
        or any(character not in "0123456789abcdef"
               for character in value["sha256"])
    ):
        return False
    xattrs = value["xattrs"]
    if xattrs is not None and (
        not isinstance(xattrs, list)
        or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
            for item in xattrs
        )
    ):
        return False
    return True


def _record_matches(descriptor, expected):
    try:
        current = _record_fields(os.fstat(descriptor))
        if any(current[name] != expected[name] for name in current):
            return False
        return _descriptor_record(descriptor) == expected
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _receipt_bytes(payload):
    encoded_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    envelope = {
        "payload": payload,
        "sha256": hashlib.sha256(encoded_payload).hexdigest(),
    }
    return json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")


def _receipt_payload_is_valid(payload, anchor, private_name):
    try:
        parent = os.fstat(anchor["parent_chain"][-1])
    except (OSError, KeyError, IndexError, TypeError):
        return False
    return (
        isinstance(payload, dict)
        and set(payload) == {
            "version", "state", "private_name", "database_name",
            "parent_device", "parent_inode", "original", "work",
        }
        and type(payload["version"]) is int
        and payload["version"] == _RECEIPT_VERSION
        and payload["state"] == "prepared"
        and isinstance(payload["state"], str)
        and payload["private_name"] == private_name
        and isinstance(payload["private_name"], str)
        and payload["database_name"] == anchor["name"]
        and isinstance(payload["database_name"], str)
        and type(payload["parent_device"]) is int
        and type(payload["parent_inode"]) is int
        and payload["parent_device"] == int(parent.st_dev)
        and payload["parent_inode"] == int(parent.st_ino)
        and _record_is_valid(payload["original"])
        and _record_is_valid(payload["work"])
        and payload["original"]["device"] == payload["work"]["device"]
        and (
            payload["original"]["device"], payload["original"]["inode"]
        ) != (payload["work"]["device"], payload["work"]["inode"])
    )


def _read_receipt(directory_fd, anchor, private_name):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(_RECEIPT_NAME, flags, dir_fd=directory_fd)
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_size > _MAX_RECEIPT_BYTES:
            raise OSError("SQLite recovery receipt is not a bounded regular file")
        data = b""
        while len(data) <= _MAX_RECEIPT_BYTES:
            block = os.read(descriptor, min(
                8192, _MAX_RECEIPT_BYTES + 1 - len(data)))
            if not block:
                break
            data += block
        if len(data) > _MAX_RECEIPT_BYTES:
            raise OSError("SQLite recovery receipt is too large")
    finally:
        os.close(descriptor)
    try:
        envelope = json.loads(data.decode("ascii"))
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
            raise ValueError
        payload = envelope["payload"]
        expected = _receipt_bytes(payload)
        if expected != data or not _receipt_payload_is_valid(
                payload, anchor, private_name):
            raise ValueError
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise OSError("SQLite recovery receipt is malformed") from None
    return payload


class _SQLiteDatabaseExclusionState:
    def __init__(self):
        self.record = None
        self.reference = weakref.ref(self)


_SQLITE_DATABASE_EXCLUSIONS = {}
_SQLITE_DATABASE_EXCLUSIONS_LOCK = threading.RLock()


def _open_sqlite_child_lease(record):
    """Open the named lock whose open description may cross a child exec."""
    descriptor = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError(
                errno.ENOSYS,
                "SQLite child exclusion is unavailable",
            )
        descriptor = os.open(
            _CHILD_LEASE_NAME,
            os.O_CREAT | os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=record["descriptor"],
        )
        held = os.fstat(descriptor)
        parent = os.fstat(record["descriptor"])
        named = os.stat(
            _CHILD_LEASE_NAME,
            dir_fd=record["descriptor"],
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(held.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or held.st_uid != os.geteuid()
            or held.st_nlink != 1
            or int(held.st_dev) != int(parent.st_dev)
            or _identity(held) != _identity(named)
        ):
            raise OSError("SQLite child exclusion file is unsafe")
        if stat.S_IMODE(held.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
            held = os.fstat(descriptor)
            if stat.S_IMODE(held.st_mode) != 0o600:
                raise OSError("SQLite child exclusion permissions are unsafe")
        os.fsync(descriptor)
        os.fsync(record["descriptor"])
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _probe_sqlite_child_lease(record):
    descriptor = _open_sqlite_child_lease(record)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise OSError(
                    errno.EBUSY,
                    "SQLite database is busy in a managed child",
                ) from exc
            raise
    finally:
        os.close(descriptor)


def _live_sqlite_database_exclusion_states(record):
    tokens = record["tokens"]
    expired = {
        reference for reference in tokens
        if reference() is None
    }
    tokens.difference_update(expired)
    return tokens


def _retire_other_clean_sqlite_database_exclusions(keep):
    """Keep one reusable descriptor without retrying an ambiguous close."""
    for key, record in tuple(_SQLITE_DATABASE_EXCLUSIONS.items()):
        if record is keep:
            continue
        if (
            _live_sqlite_database_exclusion_states(record)
            or record["locked"]
            or record["transition_pending"]
        ):
            continue
        if _SQLITE_DATABASE_EXCLUSIONS.get(key) is not record:
            continue
        descriptor = record["descriptor"]
        del _SQLITE_DATABASE_EXCLUSIONS[key]
        record["descriptor"] = None
        # The descriptor is already unlocked and no longer discoverable.  Make
        # one retirement attempt only: close(2) may consume its raw number even
        # when it reports an error, so that number must never be retried.
        os.close(descriptor)


def _release_sqlite_database_exclusion_state(state):
    record = state.record
    if record is None:
        return None
    if record["pid"] != os.getpid():
        state.record = None
        return None
    try:
        with _SQLITE_DATABASE_EXCLUSIONS_LOCK:
            record = state.record
            if record is None:
                return None
            tokens = _live_sqlite_database_exclusion_states(record)
            reference = state.reference
            pending = record["transition_pending"]
            if pending is not None and tokens:
                if any(pending is token for token in tokens):
                    if pending is not reference:
                        raise OSError(
                            errno.EBUSY,
                            "SQLite database exclusion transition is pending",
                        )
                else:
                    # A nested transition removed its token before being
                    # interrupted.  It never reached the kernel unlock, so a
                    # remaining token can safely normalise that stale marker.
                    record["transition_pending"] = None
            if reference not in tokens:
                if tokens:
                    state.record = None
                    return None
            elif len(tokens) > 1:
                record["transition_pending"] = reference
                tokens.discard(reference)
                record["transition_pending"] = None
                state.record = None
                return None
            record["transition_pending"] = reference
            _retire_other_clean_sqlite_database_exclusions(record)
            # Keep the exact directory descriptor reusable after release.
            # Making close(2) the release gate has an unprovable error
            # boundary: it may consume the descriptor before reporting an
            # interruption, so retrying its raw number could close an
            # unrelated descriptor.
            fcntl.flock(record["descriptor"], fcntl.LOCK_UN)
            tokens.discard(reference)
            record["owner"] = None
            record["locked"] = False
            record["transition_pending"] = None
            state.record = None
            return None
    except BaseException as exc:
        return exc


def _finalize_sqlite_database_exclusion_state(state):
    try:
        _release_sqlite_database_exclusion_state(state)
    except Exception:
        pass


def _reset_sqlite_database_exclusions_after_fork():
    global _SQLITE_DATABASE_EXCLUSIONS_LOCK
    records = tuple(_SQLITE_DATABASE_EXCLUSIONS.values())
    _SQLITE_DATABASE_EXCLUSIONS.clear()
    _SQLITE_DATABASE_EXCLUSIONS_LOCK = threading.RLock()
    for record in records:
        try:
            # The child inherited the parent's open-file description. Closing
            # its copy leaves the parent's lock intact; LOCK_UN here would not.
            os.close(record["descriptor"])
        except OSError:
            pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        after_in_child=_reset_sqlite_database_exclusions_after_fork)


class _SQLiteDatabaseExclusion:
    """Cooperatively serialize access to one SQLite database directory."""

    def __init__(self):
        self._state = _SQLiteDatabaseExclusionState()
        self._finalizer = weakref.finalize(
            self,
            _finalize_sqlite_database_exclusion_state,
            self._state,
        )
        self.release_error = None

    def acquire(self, parent_fd):
        if self._state.record is not None:
            raise OSError("SQLite database exclusion is already active")
        if not self._finalizer.alive:
            self._finalizer = weakref.finalize(
                self,
                _finalize_sqlite_database_exclusion_state,
                self._state,
            )
        self.release_error = None
        if not hasattr(fcntl, "flock") or os.name != "posix":
            raise OSError(
                errno.ENOSYS,
                "SQLite database exclusion is unavailable",
            )
        parent = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent.st_mode):
            raise OSError("SQLite database parent is not a directory")
        key = _identity(parent)
        current_thread = threading.current_thread()

        with _SQLITE_DATABASE_EXCLUSIONS_LOCK:
            record = _SQLITE_DATABASE_EXCLUSIONS.get(key)
            if record is not None:
                try:
                    held = os.fstat(record["descriptor"])
                except (OSError, TypeError) as exc:
                    raise OSError(
                        "SQLite database exclusion descriptor is unavailable"
                    ) from exc
                if (
                    record["pid"] != os.getpid()
                    or not stat.S_ISDIR(held.st_mode)
                    or _identity(held) != key
                    or record["identity"] != key
                ):
                    raise OSError(
                        "SQLite database exclusion descriptor changed")
                if not _live_sqlite_database_exclusion_states(record):
                    # A finalizer may have been interrupted after the kernel
                    # released this lock but before local state was cleared.
                    # Repeating LOCK_UN on the stable cached descriptor is safe
                    # and normalises that transition before it can be reused.
                    if record["locked"] or record["transition_pending"]:
                        record["transition_pending"] = self._state.reference
                        fcntl.flock(record["descriptor"], fcntl.LOCK_UN)
                        record["owner"] = None
                        record["locked"] = False
                        record["transition_pending"] = None
            _retire_other_clean_sqlite_database_exclusions(record)
            if record is None:
                record = {
                    "descriptor": None,
                    "identity": key,
                    "pid": os.getpid(),
                    "owner": None,
                    "tokens": set(),
                    "locked": False,
                    "transition_pending": None,
                }
                descriptor = None
                try:
                    descriptor = os.open(
                        f"/proc/self/fd/{parent_fd}",
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_CLOEXEC", 0),
                    )
                    record["descriptor"] = descriptor
                    held = os.fstat(descriptor)
                    if (
                        not stat.S_ISDIR(held.st_mode)
                        or _identity(held) != key
                    ):
                        raise OSError(
                            "SQLite database parent changed while reopened")
                    os.set_inheritable(descriptor, False)
                    _SQLITE_DATABASE_EXCLUSIONS[key] = record
                except BaseException:
                    if (
                        descriptor is not None
                        and _SQLITE_DATABASE_EXCLUSIONS.get(key) is not record
                    ):
                        os.close(descriptor)
                    raise
            tokens = _live_sqlite_database_exclusion_states(record)
            if tokens:
                pending = record["transition_pending"]
                if pending is not None:
                    if any(pending is token for token in tokens):
                        raise OSError(
                            errno.EBUSY,
                            "SQLite database is busy in another operation",
                        )
                    record["transition_pending"] = None
                if (
                    record["owner"] is not current_thread
                    or not record["locked"]
                ):
                    raise OSError(
                        errno.EBUSY,
                        "SQLite database is busy in another operation",
                    )
                record["transition_pending"] = self._state.reference
                try:
                    self._state.record = record
                    tokens.add(self._state.reference)
                    record["transition_pending"] = None
                    return self
                except BaseException:
                    _release_sqlite_database_exclusion_state(self._state)
                    raise

            record["locked"] = False
            record["owner"] = None
            record["transition_pending"] = self._state.reference

            try:
                self._state.record = record
                tokens.add(self._state.reference)
                record["owner"] = current_thread
                fcntl.flock(
                    record["descriptor"],
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                _probe_sqlite_child_lease(record)
                record["locked"] = True
                record["transition_pending"] = None
                return self
            except BaseException as exc:
                cleanup_error = _release_sqlite_database_exclusion_state(
                    self._state)
                if cleanup_error is not None and hasattr(exc, "add_note"):
                    exc.add_note(
                        "SQLite database exclusion cleanup also failed")
                if (
                    isinstance(exc, OSError)
                    and exc.errno in {errno.EACCES, errno.EAGAIN}
                ):
                    raise OSError(
                        errno.EBUSY,
                        "SQLite database is busy in another process",
                    ) from exc
                raise

    def release(self):
        self.release_error = _release_sqlite_database_exclusion_state(
            self._state)
        if self._state.record is None and self._finalizer.alive:
            self._finalizer.detach()
        return self.release_error

    def duplicate_for_child(self):
        """Acquire a separately provable lock description for one child."""
        with _SQLITE_DATABASE_EXCLUSIONS_LOCK:
            record = self._state.record
            if record is None or record["pid"] != os.getpid():
                raise OSError("SQLite database exclusion is not active")
            tokens = _live_sqlite_database_exclusion_states(record)
            if (
                self._state.reference not in tokens
                or not record["locked"]
                or record["transition_pending"]
            ):
                raise OSError("SQLite database exclusion is not stable")
            descriptor = _open_sqlite_child_lease(record)
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                return descriptor
            except OSError as exc:
                os.close(descriptor)
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise OSError(
                        errno.EBUSY,
                        "SQLite database is busy in a managed child",
                    ) from exc
                raise

    def retire_child_duplicate(self, descriptor):
        """Consume a child lease and prove no inherited reference remains."""
        error = None
        probe = None
        proven = False
        try:
            with _SQLITE_DATABASE_EXCLUSIONS_LOCK:
                record = self._state.record
                if record is None or record["pid"] != os.getpid():
                    raise OSError(
                        "SQLite database exclusion is not active")
                held = os.fstat(descriptor)
                named = os.stat(
                    _CHILD_LEASE_NAME,
                    dir_fd=record["descriptor"],
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(held.st_mode)
                    or _identity(held) != _identity(named)
                ):
                    raise OSError(
                        "SQLite child exclusion descriptor changed")
                closing = descriptor
                descriptor = None
                # Never retry an ambiguous close by descriptor number. Linux
                # releases that number early, so a retry could close an
                # unrelated descriptor opened by an intervening handler.
                os.close(closing)
                probe = _open_sqlite_child_lease(record)
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        raise OSError(
                            errno.EBUSY,
                            "a detached managed child is still active; "
                            "restart required",
                        ) from exc
                    raise
                closing = probe
                probe = None
                os.close(closing)
                proven = True
        except BaseException as exc:
            error = exc
        finally:
            if descriptor is not None:
                closing = descriptor
                descriptor = None
                try:
                    os.close(closing)
                except BaseException as exc:
                    if error is None:
                        error = exc
            if probe is not None:
                closing = probe
                probe = None
                try:
                    os.close(closing)
                except BaseException as exc:
                    if error is None:
                        error = exc
        return error, proven and error is None


class _FileLeaseState:
    def __init__(self):
        self.records = []
        self.signal_reference = None
        self.started = None


def _release_file_lease_state(state):
    error = None
    for record in tuple(reversed(state.records)):
        if record["file"].closed:
            state.records.remove(record)
            continue
        try:
            descriptor = record["file"].fileno()
        except BaseException as exc:
            if error is None:
                error = exc
            continue
        if record["unlock_pending"]:
            try:
                fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
                record["unlock_pending"] = False
            except OSError as exc:
                # Linux reports EAGAIN when an earlier unlock succeeded but
                # its return was lost to an asynchronous exception.  In that
                # state there is no matching lease left to release.
                if exc.errno == errno.EAGAIN:
                    record["unlock_pending"] = False
                elif error is None:
                    error = exc
            except BaseException as exc:
                if error is None:
                    error = exc
        if not record["unlock_pending"]:
            try:
                record["file"].close()
            except BaseException as exc:
                if error is None:
                    error = exc
            if record["file"].closed:
                state.records.remove(record)

    if not state.records and state.signal_reference is not None:
        try:
            state.signal_reference.close()
        except BaseException as exc:
            if error is None:
                error = exc
        if not state.signal_reference.finalizer_alive:
            state.signal_reference = None
    if not state.records and state.signal_reference is None:
        state.started = None
    return error


def _finalize_file_lease_state(state):
    try:
        _release_file_lease_state(state)
    except Exception:
        pass


class _FileLeases:
    """Short-lived Linux leases with separate, synchronously observed breaks."""

    def __init__(self):
        self._state = _FileLeaseState()
        self._finalizer = weakref.finalize(
            self, _finalize_file_lease_state, self._state)
        self.release_error = None
        self._acquisition_started = False

    @property
    def roles(self):
        return {record["role"] for record in self._state.records}

    @property
    def started(self):
        return self._state.started

    def add(self, descriptor, lease_type, role):
        if self._acquisition_started:
            raise OSError("SQLite lease acquisition is already active")
        if role in self.roles:
            raise OSError("SQLite lease roles must be distinct")
        mode = "rb" if lease_type == fcntl.F_RDLCK else "r+b"
        held = io.FileIO(
            f"/proc/self/fd/{descriptor}",
            mode,
            opener=lambda _path, _flags: os.dup(descriptor),
        )
        record = None
        try:
            if _identity(os.fstat(held.fileno())) != _identity(
                    os.fstat(descriptor)):
                raise OSError("SQLite lease descriptor changed while duplicated")
            record = {
                "file": held,
                "lease_type": lease_type,
                "role": role,
                "unlock_pending": False,
            }
            self._state.records.append(record)
        except BaseException:
            if record not in self._state.records:
                held.close()
            raise

    def acquire(self):
        required = (
            hasattr(fcntl, "F_SETLEASE")
            and hasattr(fcntl, "F_GETLEASE")
            and hasattr(fcntl, "F_SETSIG")
        )
        if not required or os.name != "posix" or not os.path.isdir("/proc/self/fd"):
            raise OSError("safe SQLite publication requires Linux file leases")
        if self._acquisition_started or not self._state.records:
            raise OSError("SQLite lease acquisition is not available")
        self._acquisition_started = True
        try:
            signal_reference = _LeaseSignalTarget.acquire()
            if signal_reference is None or not signal_reference.usable():
                raise OSError("SQLite lease signal receiver is unavailable")
            self._state.signal_reference = signal_reference
            owner = struct.pack(
                "=ii", _F_OWNER_TID, signal_reference.tid)
            for record in self._state.records:
                descriptor = record["file"].fileno()
                fcntl.fcntl(descriptor, _F_SETOWN_EX, owner)
                fcntl.fcntl(descriptor, fcntl.F_SETSIG, signal.SIGIO)
                record["unlock_pending"] = True
                try:
                    fcntl.fcntl(
                        descriptor, fcntl.F_SETLEASE, record["lease_type"])
                except OSError as exc:
                    if exc.errno != errno.EINTR:
                        record["unlock_pending"] = False
                    raise OSError(
                        exc.errno,
                        f"SQLite {record['role']} lease could not be acquired",
                    ) from exc
                if fcntl.fcntl(
                        descriptor, fcntl.F_GETLEASE) != record["lease_type"]:
                    raise OSError("SQLite file lease was not retained")
            self._state.started = time.monotonic()
        except BaseException:
            try:
                self.release()
            except BaseException:
                pass
            raise

    def pending(self, role=None):
        reference = self._state.signal_reference
        if reference is None or not reference.usable():
            return True
        records = (
            self._state.records
            if role is None
            else [
                record for record in self._state.records
                if record["role"] == role
            ]
        )
        if role is not None and not records:
            return True
        try:
            return any(
                record["file"].closed
                or fcntl.fcntl(
                    record["file"].fileno(), fcntl.F_GETLEASE)
                != record["lease_type"]
                for record in records
            )
        except (OSError, TypeError, ValueError):
            return True

    def quiet(self):
        return (
            self.started is not None
            and time.monotonic() - self.started <= _LEASE_LIMIT_SECONDS
            and not self.pending()
        )

    def release(self):
        self.release_error = _release_file_lease_state(self._state)
        if (
            not self._state.records
            and self._state.signal_reference is None
            and self._finalizer.alive
        ):
            self._finalizer.detach()
        return self.release_error


def _recovery_prefix(anchor):
    try:
        name = anchor["name"]
    except (KeyError, TypeError):
        raise OSError("bound SQLite database name is unavailable") from None
    if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
        raise OSError("bound SQLite database name is invalid")
    digest = hashlib.sha256(os.fsencode(name)).hexdigest()
    return f".qobuz-sqlite-{digest}-"


def _open_regular(directory_fd, name):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise SQLiteRecoveryRequired(
            "SQLite recovery state contains a non-regular entry")
    return descriptor


def _owner_only_private_directory_mode(mode):
    """Accept 0700 plus the setgid bit inherited from a shared parent."""
    return stat.S_IMODE(mode) in (0o700, stat.S_ISGID | 0o700)


def _open_recovery_directory(parent_fd, name):
    descriptor = os.open(
        name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    value = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or not _owner_only_private_directory_mode(value.st_mode)
        or not _named_entry_matches(
            parent_fd, name, descriptor, directory=True)
    ):
        os.close(descriptor)
        raise SQLiteRecoveryRequired(
            "SQLite recovery directory ownership or identity is ambiguous")
    return descriptor


def _remove_empty_recovery_directory(parent_fd, name, directory_fd):
    if os.listdir(directory_fd) or not _named_entry_matches(
            parent_fd, name, directory_fd, directory=True):
        return False
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)
    return True


def _close_descriptors(descriptors):
    for descriptor in descriptors.values():
        try:
            os.close(descriptor)
        except OSError:
            pass


def _open_known_recovery_entries(directory_fd, entries):
    descriptors = {}
    try:
        for name in sorted(entries):
            descriptors[name] = _open_regular(directory_fd, name)
        return descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _lease_recovery_inodes(owner, canonical_descriptor, entry_descriptors):
    canonical_identity = _identity(os.fstat(canonical_descriptor))
    other = None
    for name in ("source.db", "work.db"):
        descriptor = entry_descriptors.get(name)
        if descriptor is None:
            continue
        identity = _identity(os.fstat(descriptor))
        if identity == canonical_identity:
            continue
        if other is not None and identity != _identity(os.fstat(other)):
            raise SQLiteRecoveryRequired(
                "SQLite recovery state refers to too many database identities")
        other = descriptor
    if other is not None:
        owner.add(other, fcntl.F_RDLCK, _WORK_LEASE)
    owner.add(canonical_descriptor, fcntl.F_RDLCK, _SOURCE_LEASE)
    owner.acquire()


def _unlink_bound_entry(directory_fd, name, descriptor):
    if not _named_entry_matches(directory_fd, name, descriptor):
        raise SQLiteRecoveryRequired(
            "SQLite recovery entry changed during cleanup")
    os.unlink(name, dir_fd=directory_fd)
    if not _name_missing(directory_fd, name):
        raise SQLiteRecoveryRequired(
            "SQLite recovery entry survived exact cleanup")


def _cleanup_recovery_directory(
        parent_fd, private_name, directory_fd, entry_descriptors):
    current = set(os.listdir(directory_fd))
    if current != set(entry_descriptors):
        raise SQLiteRecoveryRequired(
            "SQLite recovery state changed during cleanup")
    ordered = sorted(current - {"source.db", _RECEIPT_NAME})
    if "source.db" in current:
        ordered.append("source.db")
    if _RECEIPT_NAME in current:
        ordered.append(_RECEIPT_NAME)
    for name in ordered:
        _unlink_bound_entry(directory_fd, name, entry_descriptors[name])
    os.fsync(directory_fd)
    if os.listdir(directory_fd) or not _named_entry_matches(
            parent_fd, private_name, directory_fd, directory=True):
        raise SQLiteRecoveryRequired(
            "SQLite recovery directory changed during cleanup")
    os.rmdir(private_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _classify_recovery_directory(
        anchor, private_name, directory_fd, entry_names):
    allowed = _EARLY_PRIVATE_NAMES | {_RECEIPT_NAME}
    if not entry_names.issubset(allowed):
        raise SQLiteRecoveryRequired(
            "SQLite recovery state contains an unfamiliar entry")
    if any(
        f"{database}{suffix}" in entry_names
        for database in ("source.db", "work.db")
        for suffix in _SIDECAR_SUFFIXES
    ):
        raise SQLiteRecoveryRequired(
            "SQLite recovery state contains a private journal")
    descriptors = _open_known_recovery_entries(directory_fd, entry_names)
    leases = _FileLeases()
    try:
        canonical_descriptor = anchor["descriptor"]
        _lease_recovery_inodes(leases, canonical_descriptor, descriptors)
        if not leases.quiet():
            raise SQLiteRecoveryRequired(
                "SQLite recovery state is in use by another process")

        if _RECEIPT_NAME not in entry_names:
            source = descriptors.get("source.db")
            if (
                source is None
                or _identity(os.fstat(source))
                != _identity(os.fstat(canonical_descriptor))
            ):
                raise SQLiteRecoveryRequired(
                    "unfinished SQLite preparation cannot be tied to the database")
            state = "original"
        else:
            try:
                payload = _read_receipt(
                    directory_fd, anchor, private_name)
            except OSError as exc:
                raise SQLiteRecoveryRequired(
                    "SQLite recovery receipt is malformed") from exc
            canonical_is_original = _record_matches(
                canonical_descriptor, payload["original"])
            canonical_is_work = _record_matches(
                canonical_descriptor, payload["work"])
            if canonical_is_original == canonical_is_work:
                raise SQLiteRecoveryRequired(
                    "SQLite recovery database fingerprint is ambiguous")
            state = "original" if canonical_is_original else "published"
            expected = {
                "source.db": payload["original"],
                "work.db": (
                    payload["work"] if state == "original"
                    else payload["original"]
                ),
            }
            if any(
                name in descriptors
                and not _record_matches(descriptors[name], record)
                for name, record in expected.items()
            ):
                raise SQLiteRecoveryRequired(
                    "SQLite recovery retained-file fingerprint is ambiguous")
            try:
                receipt_unchanged = _read_receipt(
                    directory_fd, anchor, private_name) == payload
            except OSError as exc:
                raise SQLiteRecoveryRequired(
                    "SQLite recovery receipt changed during classification"
                ) from exc
            if not receipt_unchanged:
                raise SQLiteRecoveryRequired(
                    "SQLite recovery receipt changed during classification")

        if not leases.quiet():
            raise SQLiteRecoveryRequired(
                "SQLite recovery state was opened during classification")
        _cleanup_recovery_directory(
            anchor["parent_chain"][-1],
            private_name,
            directory_fd,
            descriptors,
        )
        if not leases.quiet():
            raise SQLiteRecoveryRequired(
                "SQLite recovery state was opened during cleanup")
        return state
    finally:
        release_error = None
        release_error = leases.release()
        _close_descriptors(descriptors)
        if release_error is not None:
            # Cleanup may already have completed. The caller still must not
            # proceed without knowing that all kernel coordination ended.
            raise SQLiteRecoveryRequired(
                "SQLite recovery lease could not be released") from release_error


def reconcile_atomic_sqlite(anchor, anchor_matches):
    """Resolve one exact retained transaction before SQLite opens the database."""
    if anchor is None or anchor.get("descriptor") is None:
        raise OSError("a bound SQLite database is required")
    if not anchor_matches(anchor) or not canonical_sidecars_clear(anchor):
        raise SQLiteRecoveryRequired(
            "configured SQLite database is not safe to recover")
    parent_fd = anchor["parent_chain"][-1]
    prefix = _recovery_prefix(anchor)
    substantive = []
    for name in sorted(
            entry for entry in os.listdir(parent_fd) if entry.startswith(prefix)):
        try:
            directory_fd = _open_recovery_directory(parent_fd, name)
        except (NotADirectoryError, FileNotFoundError, OSError) as exc:
            if isinstance(exc, SQLiteRecoveryRequired):
                raise
            raise SQLiteRecoveryRequired(
                "SQLite recovery namespace contains an ambiguous entry") from exc
        try:
            entries = set(os.listdir(directory_fd))
            if not entries:
                _remove_empty_recovery_directory(parent_fd, name, directory_fd)
            else:
                substantive.append((name, entries))
        finally:
            os.close(directory_fd)
    if len(substantive) > 1:
        raise SQLiteRecoveryRequired(
            "multiple unfinished SQLite publications require operator review")
    state = None
    if substantive:
        private_name, prior_entries = substantive[0]
        directory_fd = _open_recovery_directory(parent_fd, private_name)
        try:
            entries = set(os.listdir(directory_fd))
            if entries != prior_entries:
                raise SQLiteRecoveryRequired(
                    "SQLite recovery state changed while it was scanned")
            state = _classify_recovery_directory(
                anchor, private_name, directory_fd, entries)
        finally:
            os.close(directory_fd)
    remaining = sorted(
        entry for entry in os.listdir(parent_fd) if entry.startswith(prefix))
    if remaining:
        raise SQLiteRecoveryRequired(
            "SQLite recovery namespace changed during reconciliation")
    if not anchor_matches(anchor) or not canonical_sidecars_clear(anchor):
        raise SQLiteRecoveryRequired(
            "configured SQLite database changed during reconciliation")
    return state


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


def _filesystem_magic(descriptor):
    try:
        fstatfs = ctypes.CDLL(None, use_errno=True).fstatfs
    except AttributeError as exc:
        raise OSError("safe SQLite publication requires Linux fstatfs") from exc
    fstatfs.argtypes = (ctypes.c_int, ctypes.POINTER(_LinuxStatFS))
    fstatfs.restype = ctypes.c_int
    value = _LinuxStatFS()
    if fstatfs(descriptor, ctypes.byref(value)):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(value.f_type) & 0xFFFFFFFF


def _require_supported_environment(anchor):
    if os.name != "posix" or not os.path.isdir("/proc/self/fd"):
        raise OSError(
            "safe SQLite publication requires Linux and /proc file descriptors")
    parent_fd = anchor["parent_chain"][-1]
    descriptor = anchor["descriptor"]
    if (
        _filesystem_magic(parent_fd) not in _LOCAL_FILESYSTEM_MAGIC
        or _filesystem_magic(descriptor) not in _LOCAL_FILESYSTEM_MAGIC
        or os.fstat(parent_fd).st_dev != os.fstat(descriptor).st_dev
    ):
        raise OSError(
            "safe SQLite publication requires a supported local filesystem")
    if _read_xattrs(descriptor) is None:
        raise OSError("safe SQLite publication requires filesystem xattrs")
    proc_value = os.stat(
        f"/proc/self/fd/{descriptor}", follow_symlinks=True)
    if _identity(proc_value) != _identity(os.fstat(descriptor)):
        raise OSError("safe SQLite publication cannot bind /proc descriptors")
    os.fsync(parent_fd)


def _sqlite_uri(directory_fd, name, mode):
    return (
        f"file:/proc/self/fd/{directory_fd}/{name}"
        f"?mode={mode}&vfs=unix"
    )


def _copy_security_metadata(source_descriptor, destination_descriptor):
    source = os.fstat(source_descriptor)
    os.fchown(destination_descriptor, source.st_uid, source.st_gid)
    os.fchmod(destination_descriptor, stat.S_IMODE(source.st_mode))
    source_xattrs = _read_xattrs(source_descriptor)
    destination_xattrs = _read_xattrs(destination_descriptor)
    if (source_xattrs is None) != (destination_xattrs is None):
        raise OSError("SQLite database xattr support changed while cloning")
    if source_xattrs is not None:
        for name in set(destination_xattrs) - set(source_xattrs):
            os.removexattr(destination_descriptor, name)
        for name, value in source_xattrs.items():
            if destination_xattrs.get(name) != value:
                os.setxattr(destination_descriptor, name, value)
    _require_security_metadata(
        source_descriptor, destination_descriptor, source_xattrs)


def _require_security_metadata(
        source_descriptor, destination_descriptor, expected_xattrs=None):
    source = os.fstat(source_descriptor)
    destination = os.fstat(destination_descriptor)
    if (
        (source.st_uid, source.st_gid, stat.S_IMODE(source.st_mode))
        != (
            destination.st_uid,
            destination.st_gid,
            stat.S_IMODE(destination.st_mode),
        )
    ):
        raise OSError("SQLite database access metadata changed while cloning")
    if expected_xattrs is None:
        expected_xattrs = _read_xattrs(source_descriptor)
    current_xattrs = _read_xattrs(destination_descriptor)
    if expected_xattrs != current_xattrs:
        raise OSError("SQLite database xattrs changed while cloning")


def _rename_exchange(source_fd, source, destination_fd, destination):
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
    if renameat2(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        _RENAME_EXCHANGE,
    ):
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))


def _backup_target(connection):
    target = connection
    seen = set()
    while not isinstance(target, sqlite3.Connection):
        marker = id(target)
        if marker in seen or not hasattr(target, "connection"):
            raise TypeError("SQLite connection wrapper has no backup target")
        seen.add(marker)
        target = target.connection
    return target


class AtomicSQLiteWrite:
    """Write a private clone, then atomically publish the committed database.

    The canonical database is held under a read/write SQLite reservation but is
    never modified. SQLite's rollback journal therefore remains inside a private
    directory with the work database. Only a closed, journal-free database is
    exchanged into the canonical name.
    """

    def __init__(
            self, anchor, anchor_matches, *, connect=sqlite3.connect,
            timeout=5.0):
        if anchor is None or anchor.get("descriptor") is None:
            raise OSError("a bound SQLite database is required")
        self.anchor = anchor
        self.anchor_matches = anchor_matches
        self.connect = connect
        self.timeout = timeout
        self.parent_fd = anchor["parent_chain"][-1]
        self.original_descriptor = anchor["descriptor"]
        self._database_exclusion = _SQLiteDatabaseExclusion()
        self.private_name = None
        self.private_fd = None
        self.source_retained = False
        self.work_descriptor = None
        self.published_descriptor = None
        self.receipt_descriptor = None
        self.receipt_payload = None
        self.lock_connection = None
        self.source_connection = None
        self.connection = None
        self.durable = False
        self.uncertain = False
        self.close_error = None
        self._anchor_rebound = False

    @property
    def published(self):
        if self._anchor_rebound:
            return True
        descriptor = self.published_descriptor
        return (
            descriptor is not None
            and self.anchor.get("descriptor") == descriptor
        )

    def _source_matches(self):
        return (
            self.private_fd is not None
            and _named_entry_matches(
                self.private_fd, "source.db", self.original_descriptor)
        )

    def _work_matches(self):
        return (
            self.private_fd is not None
            and self.work_descriptor is not None
            and _named_entry_matches(
                self.private_fd, "work.db", self.work_descriptor)
        )

    def _private_directory_matches(self):
        return (
            self.private_name is not None
            and self.private_fd is not None
            and _named_entry_matches(
                self.parent_fd,
                self.private_name,
                self.private_fd,
                directory=True,
            )
        )

    def _source_layout_matches(self):
        return (
            self._source_binding_matches()
            and self._work_matches()
        )

    def _source_binding_matches(self):
        return (
            self.anchor_matches(self.anchor)
            and canonical_sidecars_clear(self.anchor)
            and self._source_matches()
            and self._private_directory_matches()
        )

    def _reserve_private_directory(self):
        prefix = _recovery_prefix(self.anchor)
        for _attempt in range(16):
            name = f"{prefix}{secrets.token_hex(12)}"
            try:
                os.mkdir(name, 0o700, dir_fd=self.parent_fd)
            except FileExistsError:
                continue
            descriptor = None
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=self.parent_fd,
                )
                if not _named_entry_matches(
                        self.parent_fd, name, descriptor, directory=True):
                    raise OSError(
                        "private SQLite transaction directory changed")
                self.private_name = name
                self.private_fd = descriptor
                os.fsync(self.private_fd)
                os.fsync(self.parent_fd)
                return
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    os.rmdir(name, dir_fd=self.parent_fd)
                except OSError:
                    pass
                raise
        raise OSError("could not reserve a private SQLite transaction directory")

    def open_source(self):
        if self.lock_connection is not None or self.published:
            raise OSError("SQLite publication transaction is already active")
        try:
            self._database_exclusion.acquire(self.parent_fd)
            _require_supported_environment(self.anchor)
            reconcile_atomic_sqlite(self.anchor, self.anchor_matches)
            if not self.anchor_matches(self.anchor):
                raise OSError(
                    "configured SQLite database changed before cloning")
            self._reserve_private_directory()
            os.link(
                self.anchor["name"],
                "source.db",
                src_dir_fd=self.parent_fd,
                dst_dir_fd=self.private_fd,
                follow_symlinks=False,
            )
            self.source_retained = True
            if not self._source_binding_matches():
                raise OSError("configured SQLite database changed while retained")
            os.fsync(self.private_fd)

            self.lock_connection = self.connect(
                _sqlite_uri(self.private_fd, "source.db", "rw"),
                uri=True,
                timeout=self.timeout,
            )
            journal_mode = self.lock_connection.execute(
                "PRAGMA journal_mode").fetchone()
            if (
                journal_mode is None
                or str(journal_mode[0]).casefold() != "delete"
            ):
                raise sqlite3.DatabaseError(
                    "safe publication requires SQLite delete-journal mode")
            self.lock_connection.execute("BEGIN IMMEDIATE")
            if (
                not self._source_binding_matches()
                or not _name_missing(self.private_fd, "source.db-journal")
                or not _name_missing(self.private_fd, "source.db-wal")
                or not _name_missing(self.private_fd, "source.db-shm")
            ):
                raise OSError("configured SQLite database changed while locked")
            return self.lock_connection
        except BaseException:
            self.close()
            raise

    def open(self):
        if self.connection is not None or self.published:
            raise OSError("SQLite publication transaction is already active")
        try:
            self.open_source()

            self.source_connection = self.connect(
                _sqlite_uri(self.private_fd, "source.db", "ro"),
                uri=True,
                timeout=self.timeout,
            )
            self.work_descriptor = os.open(
                "work.db",
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=self.private_fd,
            )
            self.connection = self.connect(
                _sqlite_uri(self.private_fd, "work.db", "rw"),
                uri=True,
                timeout=self.timeout,
            )
            journal_mode = self.connection.execute(
                "PRAGMA journal_mode").fetchone()
            if (
                journal_mode is None
                or str(journal_mode[0]).casefold() != "delete"
            ):
                raise sqlite3.DatabaseError(
                    "safe publication requires SQLite delete-journal mode")
            if not self._source_layout_matches():
                raise OSError("SQLite transaction bindings changed before cloning")
            self.source_connection.backup(_backup_target(self.connection))
            if not self._source_layout_matches():
                raise OSError("SQLite transaction bindings changed during cloning")
            _copy_security_metadata(
                self.original_descriptor, self.work_descriptor)
            os.fsync(self.work_descriptor)
            os.fsync(self.private_fd)
            self.source_connection.close()
            self.source_connection = None
            return self.connection
        except BaseException:
            self.close()
            raise

    def _publication_layout(self):
        if (
            self._source_matches()
            and _named_entry_matches(
                self.parent_fd,
                self.anchor["name"],
                self.work_descriptor,
            )
            and _named_entry_matches(
                self.private_fd,
                "work.db",
                self.original_descriptor,
            )
        ):
            return "published"
        if (
            self._source_matches()
            and _named_entry_matches(
                self.parent_fd,
                self.anchor["name"],
                self.original_descriptor,
            )
            and self._work_matches()
        ):
            return "original"
        return "uncertain"

    def _private_sidecars_clear(self):
        return all(
            _name_missing(self.private_fd, f"{database}{suffix}")
            for database in ("source.db", "work.db")
            for suffix in _SIDECAR_SUFFIXES
        )

    def _receipt_matches(self):
        if self.receipt_payload is None:
            return False
        try:
            return _read_receipt(
                self.private_fd, self.anchor, self.private_name,
            ) == self.receipt_payload
        except OSError:
            return False

    def _prepared_layout_matches(self):
        return (
            self._source_layout_matches()
            and self._private_sidecars_clear()
            and self._receipt_matches()
            and _record_matches(
                self.original_descriptor, self.receipt_payload["original"])
            and _record_matches(
                self.work_descriptor, self.receipt_payload["work"])
        )

    def _write_prepared_receipt(self):
        if self.receipt_descriptor is not None or self.receipt_payload is not None:
            raise OSError("SQLite publication receipt already exists")
        parent = os.fstat(self.parent_fd)
        payload = {
            "version": _RECEIPT_VERSION,
            "state": "prepared",
            "private_name": self.private_name,
            "database_name": self.anchor["name"],
            "parent_device": int(parent.st_dev),
            "parent_inode": int(parent.st_ino),
            "original": _descriptor_record(self.original_descriptor),
            "work": _descriptor_record(self.work_descriptor),
        }
        data = _receipt_bytes(payload)
        if len(data) > _MAX_RECEIPT_BYTES:
            raise OSError("SQLite publication receipt is too large")
        descriptor = os.open(
            _RECEIPT_NAME,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=self.private_fd,
        )
        self.receipt_descriptor = descriptor
        offset = 0
        try:
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise OSError("SQLite publication receipt write stalled")
                offset += written
            os.fsync(descriptor)
            os.fsync(self.private_fd)
            os.fsync(self.parent_fd)
            self.receipt_payload = payload
            if not self._receipt_matches():
                raise OSError("SQLite publication receipt was not durable and exact")
        except BaseException:
            self.uncertain = True
            raise

    def _close_source_reservation(self):
        connection = self.lock_connection
        if connection is None:
            raise OSError("SQLite source reservation is not active")
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        self.lock_connection = None

    def _adopt_published_anchor(self):
        if self._anchor_rebound:
            self.published_descriptor = None
            return
        descriptor = self.published_descriptor
        if descriptor is None:
            raise SQLitePublicationUncertain(
                "published SQLite descriptor was not reserved")
        if self.anchor.get("descriptor") != descriptor:
            self.anchor["descriptor"] = descriptor
        if self.anchor.get("descriptor") != descriptor:
            raise SQLitePublicationUncertain(
                "published SQLite descriptor was not adopted")
        self._anchor_rebound = True
        self.published_descriptor = None

    def _restore_foreign_exchange(self):
        """Undo an exchange that raced a foreign replacement of the leaf."""
        if not (
            _named_entry_matches(
                self.parent_fd, self.anchor["name"], self.work_descriptor)
            and self._source_matches()
            and self._private_directory_matches()
            and not _named_entry_matches(
                self.private_fd, "work.db", self.original_descriptor)
        ):
            return False
        foreign_descriptor = None
        restored = False
        try:
            foreign_descriptor = _open_regular(self.private_fd, "work.db")
            if _identity(os.fstat(foreign_descriptor)) in {
                _identity(os.fstat(self.original_descriptor)),
                _identity(os.fstat(self.work_descriptor)),
            }:
                return False
            _rename_exchange(
                self.private_fd,
                "work.db",
                self.parent_fd,
                self.anchor["name"],
            )
            os.fsync(self.private_fd)
            os.fsync(self.parent_fd)
            restored = self._work_matches() and _named_entry_matches(
                self.parent_fd,
                self.anchor["name"],
                foreign_descriptor,
            )
        except BaseException:
            return False
        finally:
            if foreign_descriptor is not None:
                os.close(foreign_descriptor)
        return restored

    def _best_effort_durable_published(self):
        try:
            self._adopt_published_anchor()
            os.fsync(self.private_fd)
            os.fsync(self.parent_fd)
            self.durable = True
        except BaseException as exc:
            self._remember_close_error(exc)

    def _classify_exchange_failure(self, leases):
        layout = self._publication_layout()
        try:
            work_pending = leases.pending(_WORK_LEASE)
        except BaseException as exc:
            self._remember_close_error(exc)
            work_pending = True
        if layout == "published":
            self._best_effort_durable_published()
            return
        if layout == "original":
            if work_pending:
                self.uncertain = True
            return
        if not work_pending and self._restore_foreign_exchange():
            self.uncertain = True
            return
        if _named_entry_matches(
                self.parent_fd, self.anchor["name"], self.work_descriptor):
            self._best_effort_durable_published()
        self.uncertain = True

    def commit_and_publish(self, anchors_match, verify=None):
        if self.connection is None or self.work_descriptor is None:
            raise OSError("SQLite publication transaction is not open")
        if not anchors_match() or not self._source_layout_matches():
            raise OSError("transaction anchors changed before SQLite commit")
        self.connection.commit()
        if self.connection.in_transaction:
            raise sqlite3.DatabaseError("SQLite transaction remained open")
        if verify is not None and not verify(self.connection):
            raise sqlite3.DatabaseError("committed SQLite rows were not exact")
        if not anchors_match() or not self._source_layout_matches():
            raise OSError("transaction anchors changed after SQLite commit")
        self.connection.close()
        self.connection = None
        for suffix in _SIDECAR_SUFFIXES:
            if not _name_missing(self.private_fd, f"work.db{suffix}"):
                raise sqlite3.DatabaseError(
                    "private SQLite recovery was incomplete before publication")
        os.fsync(self.work_descriptor)
        os.fsync(self.private_fd)
        _require_security_metadata(
            self.original_descriptor, self.work_descriptor)
        if not anchors_match() or not self._source_layout_matches():
            raise OSError("transaction anchors changed before SQLite publication")
        self._write_prepared_receipt()
        self.published_descriptor = os.dup(self.work_descriptor)
        self._close_source_reservation()

        leases = _FileLeases()
        lease_requests = (
            (
                self.original_descriptor,
                fcntl.F_RDLCK,
                _SOURCE_LEASE,
            ),
            (
                self.work_descriptor,
                fcntl.F_WRLCK,
                _WORK_LEASE,
            ),
        )
        caught = None
        classification_error = None
        release_error = None
        exchange_started = False
        try:
            try:
                for request in lease_requests:
                    leases.add(*request)
                leases.acquire()
                prepared = self._prepared_layout_matches()
                work_pending = leases.pending(_WORK_LEASE)
                source_pending = leases.pending(_SOURCE_LEASE)
                if work_pending:
                    self.uncertain = True
                    raise OSError(
                        "private SQLite replacement was opened during publication")
                if source_pending:
                    raise OSError(
                        "SQLite source was opened during the publication handoff")
                if not prepared:
                    self.uncertain = True
                    raise OSError(
                        "SQLite publication changed during the lock handoff")
                if not anchors_match() or not leases.quiet():
                    raise OSError(
                        "SQLite publication changed during the lock handoff")
                exchange_started = True
                _rename_exchange(
                    self.private_fd,
                    "work.db",
                    self.parent_fd,
                    self.anchor["name"],
                )
                if self._publication_layout() != "published":
                    raise SQLitePublicationUncertain(
                        "SQLite publication changed during the atomic exchange")
                self._adopt_published_anchor()
                os.fsync(self.private_fd)
                os.fsync(self.parent_fd)
                self.durable = True
                if not anchors_match():
                    raise OSError(
                        "transaction anchors changed after SQLite publication")
            except BaseException as exc:
                caught = exc
                if exchange_started:
                    try:
                        self._classify_exchange_failure(leases)
                    except BaseException as classify_exc:
                        self.uncertain = True
                        classification_error = classify_exc
        finally:
            try:
                release_error = leases.release()
            except BaseException as exc:
                release_error = exc
                self.uncertain = True
        if release_error is not None:
            self.uncertain = True
            if caught is None:
                raise SQLitePublicationUncertain(
                    "SQLite publication lease could not be released"
                ) from release_error
        if classification_error is not None and caught is None:
            raise SQLitePublicationUncertain(
                "SQLite publication state could not be classified"
            ) from classification_error
        if caught is not None:
            if classification_error is not None and isinstance(caught, Exception):
                caught.add_note(
                    "SQLite publication recovery classification also failed")
            raise caught.with_traceback(caught.__traceback__)

    def _unlink_exact(self, name, descriptor):
        if not _named_entry_matches(self.private_fd, name, descriptor):
            return False
        os.unlink(name, dir_fd=self.private_fd)
        return _name_missing(self.private_fd, name)

    def _remember_close_error(self, error):
        if self.close_error is None:
            self.close_error = error
        self.uncertain = True

    def _cleanup_known_transaction(self):
        if not self._private_sidecars_clear():
            raise SQLiteRecoveryRequired(
                "private SQLite recovery journal requires operator review")
        expected = {}
        if self.work_descriptor is not None:
            expected["work.db"] = (
                self.original_descriptor if self.published
                else self.work_descriptor
            )
        if self.source_retained:
            if not self._source_matches():
                raise SQLiteRecoveryRequired(
                    "private SQLite source changed before cleanup")
            expected["source.db"] = self.original_descriptor
        if self.receipt_descriptor is not None:
            if not self._receipt_matches():
                raise SQLiteRecoveryRequired(
                    "SQLite publication receipt changed before cleanup")
            expected[_RECEIPT_NAME] = self.receipt_descriptor
        if set(os.listdir(self.private_fd)) != set(expected):
            raise SQLiteRecoveryRequired(
                "private SQLite transaction contains an unfamiliar entry")

        for name in ("work.db", "source.db"):
            descriptor = expected.get(name)
            if descriptor is not None and not self._unlink_exact(
                    name, descriptor):
                raise SQLiteRecoveryRequired(
                    "private SQLite transaction changed during cleanup")
        os.fsync(self.private_fd)
        receipt = expected.get(_RECEIPT_NAME)
        if receipt is not None:
            if not self._unlink_exact(_RECEIPT_NAME, receipt):
                raise SQLiteRecoveryRequired(
                    "SQLite publication receipt changed during cleanup")
            os.fsync(self.private_fd)

    def close(self):
        active_error = sys.exception()
        cleanup_error = None

        def remember(error):
            nonlocal cleanup_error
            if (
                cleanup_error is None
                or (
                    isinstance(cleanup_error, Exception)
                    and not isinstance(error, Exception)
                )
            ):
                cleanup_error = error
            self._remember_close_error(error)

        if self.source_connection is not None:
            try:
                self.source_connection.close()
            except BaseException as exc:
                remember(exc)
            self.source_connection = None
        if self.connection is not None:
            try:
                if self.connection.in_transaction:
                    self.connection.rollback()
            except BaseException as exc:
                remember(exc)
            try:
                self.connection.close()
            except BaseException as exc:
                remember(exc)
            self.connection = None
        if self.lock_connection is not None:
            try:
                if self.lock_connection.in_transaction:
                    self.lock_connection.rollback()
            except BaseException as exc:
                remember(exc)
            try:
                self.lock_connection.close()
            except BaseException as exc:
                remember(exc)
            self.lock_connection = None

        if self.private_fd is not None:
            if not self.uncertain:
                try:
                    self._cleanup_known_transaction()
                except BaseException as exc:
                    remember(exc)

        if self.receipt_descriptor is not None:
            try:
                os.close(self.receipt_descriptor)
            except OSError:
                pass
            self.receipt_descriptor = None

        try:
            published = self.published
        except BaseException as exc:
            remember(exc)
            published = False
        if published:
            self._anchor_rebound = True
            if self.original_descriptor is not None:
                try:
                    os.close(self.original_descriptor)
                except OSError:
                    pass
                self.original_descriptor = None
            self.published_descriptor = None
        elif self.published_descriptor is not None:
            try:
                os.close(self.published_descriptor)
            except OSError:
                pass
            self.published_descriptor = None
        if self.work_descriptor is not None:
            try:
                os.close(self.work_descriptor)
            except OSError:
                pass
            self.work_descriptor = None

        if self.private_fd is not None:
            try:
                if self._private_directory_matches() and not os.listdir(
                        self.private_fd):
                    os.rmdir(self.private_name, dir_fd=self.parent_fd)
                    os.fsync(self.parent_fd)
            except OSError:
                pass
            try:
                os.close(self.private_fd)
            except OSError:
                pass
            self.private_fd = None

        exclusion_error = self._database_exclusion.release()
        if exclusion_error is not None:
            remember(exclusion_error)

        if cleanup_error is None:
            return
        if active_error is not None:
            if (
                isinstance(active_error, Exception)
                and not isinstance(cleanup_error, Exception)
            ):
                if hasattr(cleanup_error, "add_note"):
                    cleanup_error.add_note(
                        "SQLite transaction also failed before cleanup; "
                        "publication state is uncertain"
                    )
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            if hasattr(active_error, "add_note"):
                active_error.add_note(
                    "SQLite transaction cleanup also failed; "
                    "publication state is uncertain"
                )
            return
        if not isinstance(cleanup_error, Exception):
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        raise SQLitePublicationUncertain(
            "SQLite transaction cleanup could not be completed"
        ) from cleanup_error


def inspect_sqlite_source(
        anchor, anchor_matches, inspect, *, connect=sqlite3.connect,
        timeout=5.0, anchors_match=None):
    """Inspect the exact source inode without entering its recovery namespace."""
    if anchors_match is not None and not anchors_match():
        raise OSError("transaction anchors changed before SQLite inspection")
    transaction = AtomicSQLiteWrite(
        anchor,
        anchor_matches,
        connect=connect,
        timeout=timeout,
    )
    try:
        connection = transaction.open_source()
        if anchors_match is not None and not anchors_match():
            raise OSError("transaction anchors changed during SQLite inspection")
        result = inspect(connection)
        if (
            not transaction._source_binding_matches()
            or (anchors_match is not None and not anchors_match())
        ):
            raise OSError("transaction anchors changed after SQLite inspection")
        return result
    finally:
        transaction.close()
