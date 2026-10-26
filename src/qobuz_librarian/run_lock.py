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
import weakref
from typing import Optional, TextIO

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli import colors
from qobuz_librarian.ui_cli import logging as cli_logging
from qobuz_librarian.ui_cli.colors import C


class LockBusy(Exception):
    """Raised by acquire() when another holder has the lock.

    The other-process PID is on ``self.pid`` when readable, else "?".
    """

    def __init__(self, pid: str = "?", holder: Optional[str] = None):
        super().__init__(f"another run is active (pid {pid})")
        self.pid = pid
        self.holder = holder


def _read_lock_holder(parent_descriptor, name):
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK,
            dir_fd=parent_descriptor,
        )
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            return "?", None
        raw = os.pread(descriptor, 64, 0).decode("ascii", "strict").strip()
        fields = raw.split(maxsplit=1)
        if not fields or not fields[0].isdigit():
            return "?", None
        return fields[0], fields[1] if len(fields) > 1 else None
    except (OSError, UnicodeError):
        return "?", None
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


def _identity(value) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


class RunLockLease:
    """Owned run-lock handle that can prove the public lock is still held."""

    def __init__(self, stream: TextIO, parent_fd: int, path: str):
        self._stream = stream
        self._path = path
        self._owner_pid = os.getpid()
        self._identity = _identity(os.fstat(stream.fileno()))
        self._finalizer = weakref.finalize(
            self, _close_lock_resources, stream, (parent_fd,))

    def intact(self) -> bool:
        """Check the configured lock file is still the one this process holds."""
        if os.getpid() != self._owner_pid or self._stream.closed:
            return False
        try:
            if os.path.abspath(cfg.LOCK_FILE) != self._path:
                return False
            held = os.fstat(self._stream.fileno())
            named = os.lstat(self._path)
        except (OSError, ValueError):
            return False
        return (
            stat.S_ISREG(named.st_mode)
            and _identity(held) == _identity(named) == self._identity
        )

    def close(self) -> None:
        global _current
        if _current is not None and _current[1]() is self:
            _current = None
        error = self._finalizer()
        if error is not None:
            raise error


def require_authority(authority: RunLockLease, error) -> None:
    if type(authority) is not RunLockLease or authority.intact() is not True:
        raise error


_current: Optional[tuple[int, weakref.ReferenceType[RunLockLease]]] = None


def web_paused_marker():
    """The file that is present while the web app has handed the lock to the
    terminal."""
    return cfg.DATA_DIR / ".qobuz_web_paused"


def current_lease() -> Optional[RunLockLease]:
    """Return this process's live acquired lease while it still holds the lock."""
    if _current is None:
        return None
    owner_pid, reference = _current
    lease = reference()
    if owner_pid != os.getpid() or lease is None or not lease.intact():
        return None
    return lease


# Why the last acquire() could not hold the lock, so a caller can tell a lock
# file owned by another user from a mount without file locking.
unavailable_reason: Optional[OSError] = None


def _warn_lockless(detail: str) -> None:
    """Explain that writes remain paused without the run-lock boundary."""
    import logging

    logging.getLogger("qobuz_librarian").warning(colors.fmt(
        C.YELLOW,
        f"  ⚠  {detail}; the single-instance lock is unavailable. Writes "
        "remain paused until the data folder supports file locking and the "
        "app restarts."))


def _second_open_refused(parent_fd: int, name: str) -> bool:
    """Whether flock is enforced here: a second open of the held file must not
    be able to lock it too."""
    try:
        probe = os.open(
            name,
            os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError:
        return False
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(probe)
    return False


def acquire(holder: Optional[str] = None) -> Optional[RunLockLease]:
    """Acquire the run lock, or report that it is unavailable."""
    global _current, unavailable_reason
    unavailable_reason = None
    path = os.path.abspath(cfg.LOCK_FILE)
    leaf = os.path.basename(path)
    try:
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        parent_fd = os.open(
            os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        _warn_lockless(f"couldn't open run-lock at {cfg.LOCK_FILE} ({exc})")
        unavailable_reason = exc
        return None

    # The folder is locked first: that is what still stops a second writer
    # after the lock file is deleted, and what earlier versions test.
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        other = _read_lock_holder(parent_fd, leaf)
        _close_lock_resources(None, (parent_fd,))
        raise LockBusy(*other) from None
    except OSError as exc:
        _close_lock_resources(None, (parent_fd,))
        _warn_lockless(f"run-lock not enforceable on {path} ({exc})")
        unavailable_reason = exc
        return None

    descriptor = -1
    stream = None
    try:
        descriptor = os.open(
            leaf,
            os.O_CREAT
            | os.O_RDWR
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK,
            0o600,
            dir_fd=parent_fd,
        )
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
            raise OSError("run-lock namespace is unsafe")
        stream = os.fdopen(descriptor, "r+", encoding="utf-8")
        descriptor = -1
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = _read_lock_holder(parent_fd, leaf)
            _close_lock_resources(stream, (parent_fd,))
            raise LockBusy(*other) from None
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        _close_lock_resources(stream, (parent_fd,))
        _warn_lockless(f"couldn't open run-lock at {path} ({exc})")
        unavailable_reason = exc
        return None

    try:
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        if holder:
            stream.write(f" {holder}")
        stream.flush()
        os.fsync(stream.fileno())
        os.fsync(parent_fd)
    except OSError as exc:
        cli_logging.vlog(f"run-lock: couldn't record PID ({exc}); lock is held regardless")

    lease = RunLockLease(stream, parent_fd, path)
    if not lease.intact():
        lease.close()
        _warn_lockless(f"run-lock changed during acquisition at {path}")
        return None
    if not _second_open_refused(parent_fd, leaf):
        lease.close()
        _warn_lockless(
            f"run-lock not enforceable on {path} (a second open could lock "
            "it too)")
        return None
    _current = (os.getpid(), weakref.ref(lease))
    return lease
