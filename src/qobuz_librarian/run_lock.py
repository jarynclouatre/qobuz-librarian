"""Single-instance run lock shared by the CLI and web worker.

Two writers into /staging at the same time corrupts both their downloads,
so only one Qobuz Librarian "run" (CLI invocation or web worker process)
holds the lock at any time. Uses fcntl.flock - kernel releases the lock
on process exit (including SIGKILL), so there's no stale-lock cleanup.

The lock file lives in DATA_DIR (a shared volume in Docker) so a
``docker compose run --rm qobuz-librarian cli ...`` from a separate
container is blocked while the web container holds the lock.
"""
import fcntl
import os
import stat
import threading
import weakref
from pathlib import Path
from typing import Optional, TextIO

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli import colors
from qobuz_librarian.ui_cli import logging as cli_logging
from qobuz_librarian.ui_cli.colors import C


class LockBusy(Exception):
    """Raised by acquire() when another holder has the lock.

    The other-process PID is on ``self.pid`` when readable, else "?".
    """

    def __init__(self, pid: str = "?"):
        super().__init__(f"another run is active (pid {pid})")
        self.pid = pid


def _lock_path_parts(value):
    raw = os.fspath(value)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise OSError("invalid run-lock path")
    path = Path(os.path.abspath(raw))
    try:
        parts = path.relative_to(Path(os.path.sep)).parts
    except ValueError:
        raise OSError("run-lock path has no stable filesystem anchor") from None
    if not parts:
        raise OSError("run-lock path must name a file")
    return path, tuple(parts)


def _directory_matches(descriptor, parent_descriptor, name):
    try:
        held = os.fstat(descriptor)
        named = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(held.st_mode)
        and stat.S_ISDIR(named.st_mode)
        and (int(held.st_dev), int(held.st_ino))
        == (int(named.st_dev), int(named.st_ino))
    )


def _parent_chain_intact(chain, names):
    if len(chain) != len(names) + 1:
        return False
    try:
        root_held = os.fstat(chain[0])
        root_named = os.stat(os.path.sep, follow_symlinks=False)
    except OSError:
        return False
    if (
        not stat.S_ISDIR(root_held.st_mode)
        or not stat.S_ISDIR(root_named.st_mode)
        or (int(root_held.st_dev), int(root_held.st_ino))
        != (int(root_named.st_dev), int(root_named.st_ino))
    ):
        return False
    return all(
        _directory_matches(chain[index + 1], chain[index], name)
        for index, name in enumerate(names)
    )


def _open_lock_parent(value):
    path, parts = _lock_path_parts(value)
    names = parts[:-1]
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe run-lock access is unavailable")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    chain = [os.open(os.path.sep, flags)]
    try:
        for name in names:
            try:
                descriptor = os.open(name, flags, dir_fd=chain[-1])
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o755, dir_fd=chain[-1])
                except FileExistsError:
                    pass
                descriptor = os.open(name, flags, dir_fd=chain[-1])
            if not _directory_matches(descriptor, chain[-1], name):
                os.close(descriptor)
                raise OSError("run-lock directory changed while it was opened")
            chain.append(descriptor)
        if not _parent_chain_intact(chain, names):
            raise OSError("run-lock directory changed while it was opened")
        return path, parts[-1], tuple(chain), names
    except BaseException:
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _read_lock_pid(parent_descriptor, name):
    descriptor = -1
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return "?"
        descriptor = os.open(
            name,
            os.O_RDONLY
            | nofollow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            return "?"
        raw = os.pread(descriptor, 64, 0).decode("ascii", "strict").strip()
        return raw if raw.isdigit() else "?"
    except (OSError, UnicodeError):
        return "?"
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _close_lock_resources(stream, descriptors):
    error = None
    if stream is not None:
        try:
            stream.close()
        except OSError as exc:
            error = exc
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError as exc:
            if error is None:
                error = exc
    return error


class RunLockLease:
    """Owned run-lock handle that can prove the public lock is still held."""

    def __init__(self, stream: TextIO, path: Path, leaf: str, chain, names):
        self._stream = stream
        self._path = path
        self._leaf = leaf
        self._parent_chain = chain
        self._parent_names = names
        self._owner_pid = os.getpid()
        value = os.fstat(stream.fileno())
        self._identity = (int(value.st_dev), int(value.st_ino))
        self._lock = threading.RLock()
        self._finalizer = weakref.finalize(
            self, _close_lock_resources, stream, chain)

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def intact(self) -> bool:
        """Revalidate the exact public inode and exclusive kernel lock."""
        with self._lock:
            if os.getpid() != self._owner_pid:
                return False
            try:
                configured, _parts = _lock_path_parts(cfg.LOCK_FILE)
            except OSError:
                return False
            if (
                self._stream.closed
                or configured != self._path
                or not _parent_chain_intact(
                    self._parent_chain, self._parent_names)
            ):
                return False
            probe = -1
            try:
                descriptor = self._stream.fileno()
                held = os.fstat(descriptor)
                parent_descriptor = self._parent_chain[-1]
                fcntl.flock(
                    parent_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                named = os.stat(
                    self._leaf,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(held.st_mode)
                    or not stat.S_ISREG(named.st_mode)
                    or held.st_nlink != 1
                    or named.st_nlink != 1
                    or (int(held.st_dev), int(held.st_ino)) != self._identity
                    or (int(named.st_dev), int(named.st_ino)) != self._identity
                ):
                    return False
                # Reasserting LOCK_EX on its own open-file description is a
                # no-op when intact and safely reacquires it if it was locally
                # dropped. A separate open must still be refused.
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                probe = os.open(
                    self._leaf,
                    os.O_RDWR
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=parent_descriptor,
                )
                probe_stat = os.fstat(probe)
                if (
                    not stat.S_ISREG(probe_stat.st_mode)
                    or (int(probe_stat.st_dev), int(probe_stat.st_ino))
                    != self._identity
                ):
                    return False
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    settled = os.stat(
                        self._leaf,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    current = os.fstat(descriptor)
                    return (
                        _parent_chain_intact(
                            self._parent_chain, self._parent_names)
                        and stat.S_ISREG(settled.st_mode)
                        and settled.st_nlink == 1
                        and (int(settled.st_dev), int(settled.st_ino))
                        == self._identity
                        and (int(current.st_dev), int(current.st_ino))
                        == self._identity
                    )
                return False
            except (OSError, TypeError, ValueError):
                return False
            finally:
                if probe >= 0:
                    try:
                        os.close(probe)
                    except OSError:
                        pass

    def close(self) -> None:
        with self._lock:
            error = self._finalizer() if self._finalizer.alive else None
        _unregister_current_lease(self)
        if error is not None:
            raise error


_CURRENT_LEASE_REGISTRATION: Optional[
    tuple[int, weakref.ReferenceType[RunLockLease]]
] = None


def _forget_current_lease(reference) -> None:
    global _CURRENT_LEASE_REGISTRATION
    registration = _CURRENT_LEASE_REGISTRATION
    if registration is not None and registration[1] is reference:
        _CURRENT_LEASE_REGISTRATION = None


def _register_current_lease(lease: RunLockLease) -> None:
    global _CURRENT_LEASE_REGISTRATION
    if type(lease) is not RunLockLease:
        raise TypeError("only an exact acquired run-lock lease may be registered")
    _CURRENT_LEASE_REGISTRATION = (
        os.getpid(),
        weakref.ref(lease, _forget_current_lease),
    )


def _unregister_current_lease(lease: RunLockLease) -> None:
    global _CURRENT_LEASE_REGISTRATION
    registration = _CURRENT_LEASE_REGISTRATION
    if registration is None:
        return
    try:
        registered = registration[1]()
    except TypeError:
        registered = None
    if registered is lease:
        _CURRENT_LEASE_REGISTRATION = None


def current_lease() -> Optional[RunLockLease]:
    """Return this process's exact live acquired lease, after revalidation."""
    global _CURRENT_LEASE_REGISTRATION
    registration = _CURRENT_LEASE_REGISTRATION
    if registration is None:
        return None
    owner_pid, reference = registration
    try:
        lease = reference()
    except TypeError:
        lease = None
    if (
        owner_pid != os.getpid()
        or type(lease) is not RunLockLease
        or not lease.intact()
    ):
        if _CURRENT_LEASE_REGISTRATION is registration:
            _CURRENT_LEASE_REGISTRATION = None
        return None
    return lease


def _warn_lockless(detail: str) -> None:
    """Explain that writes remain paused without the run-lock boundary."""
    import logging

    logging.getLogger("qobuz_librarian").warning(colors.fmt(
        C.YELLOW,
        f"  ⚠  {detail}; the single-instance lock is unavailable. Writes "
        "remain paused until the data folder supports file locking and the "
        "app restarts."))


def acquire() -> Optional[RunLockLease]:
    """Acquire the exact no-follow run lock, or report that it is unavailable."""
    try:
        path, leaf, chain, names = _open_lock_parent(cfg.LOCK_FILE)
    except OSError as exc:
        _warn_lockless(f"couldn't open run-lock at {cfg.LOCK_FILE} ({exc})")
        return None

    parent_descriptor = chain[-1]
    try:
        fcntl.flock(
            parent_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        other = _read_lock_pid(parent_descriptor, leaf)
        _close_lock_resources(None, chain)
        raise LockBusy(other)
    except OSError as exc:
        _close_lock_resources(None, chain)
        _warn_lockless(f"run-lock not enforceable on {path} ({exc})")
        return None

    descriptor = -1
    stream = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe run-lock access is unavailable")
        descriptor = os.open(
            leaf,
            os.O_CREAT
            | os.O_RDWR
            | nofollow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        held = os.fstat(descriptor)
        named = os.stat(
            leaf, dir_fd=parent_descriptor, follow_symlinks=False)
        identity = (int(held.st_dev), int(held.st_ino))
        if (
            not stat.S_ISREG(held.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or held.st_nlink != 1
            or named.st_nlink != 1
            or identity != (int(named.st_dev), int(named.st_ino))
            or not _parent_chain_intact(chain, names)
        ):
            raise OSError("run-lock namespace is unsafe")
        stream = os.fdopen(descriptor, "r+", encoding="utf-8")
        descriptor = -1
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = _read_lock_pid(parent_descriptor, leaf)
            _close_lock_resources(stream, chain)
            raise LockBusy(other) from None
    except LockBusy:
        raise
    except OSError as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _close_lock_resources(stream, chain)
        _warn_lockless(f"couldn't open run-lock at {path} ({exc})")
        return None

    try:
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        os.fsync(stream.fileno())
        os.fsync(parent_descriptor)
    except OSError as exc:
        cli_logging.vlog(f"run-lock: couldn't record PID ({exc}); lock is held regardless")

    lease = RunLockLease(stream, path, leaf, chain, names)
    if lease.intact():
        _register_current_lease(lease)
        return lease
    lease.close()
    _warn_lockless(f"run-lock changed during acquisition at {path}")
    return None
