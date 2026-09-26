"""Shared state of the running Web app: the run lock, terminal mode and shutdown."""
import threading

from qobuz_librarian import config as cfg
from qobuz_librarian.api import lastfm

# Held for the lifetime of the web process. Module-level so Python won't
# garbage-collect it (which would silently release the flock).
_RUN_LOCK_HANDLE = None
# Set when run_lock.acquire() fails at startup: the holder's PID, used by
# every destructive route to refuse new work.
_LOCK_BUSY_PID = None
# True when the web app has deliberately released the run-lock so the terminal
# (CLI) can use it. Set by the Settings "Mode" toggle, or at startup when
# QL_CLI_ONLY is set.
_CLI_MODE = False
# run_lock.acquire() returned None: the data dir can't ENFORCE the single-
# writer lock (unwritable path, or a mount without file locking).
_LOCK_UNENFORCEABLE = False
# Set before lifespan shutdown starts so no new mutating request can register
# while the workers and request-owned library operations are draining.
_SHUTTING_DOWN = False
# Set the moment a stop signal arrives. The server waits for open requests
# before it shuts the app down, and a live-progress stream never ends by
# itself, so the streams close on this and running work starts to wind down.
_STOP_SIGNALLED = threading.Event()
# Persisted Web jobs are restored only after this process holds exact write
# authority.
_JOBS_RESTORED = False
_JOBS_RESTORE_LOCK = threading.Lock()


def run_lock_handle():
    return _RUN_LOCK_HANDLE


def set_run_lock_handle(handle) -> None:
    global _RUN_LOCK_HANDLE
    _RUN_LOCK_HANDLE = handle


def lock_busy_pid():
    return _LOCK_BUSY_PID


def set_lock_busy_pid(pid) -> None:
    global _LOCK_BUSY_PID
    _LOCK_BUSY_PID = pid


def cli_mode() -> bool:
    return _CLI_MODE


def set_cli_mode(on: bool) -> None:
    global _CLI_MODE
    _CLI_MODE = on


def lock_unenforceable() -> bool:
    return _LOCK_UNENFORCEABLE


def set_lock_unenforceable(on: bool) -> None:
    global _LOCK_UNENFORCEABLE
    _LOCK_UNENFORCEABLE = on


def shutting_down() -> bool:
    return _SHUTTING_DOWN


def _run_lock_intact() -> bool:
    intact = getattr(_RUN_LOCK_HANDLE, "intact", None)
    return callable(intact) and intact() is True


def _upgrade_available() -> bool:
    return bool(getattr(cfg, "UPGRADE_SCAN_ENABLED", True))


# Reentrant so the auto-triggers (which hold it) can call the _start_* helpers
# (which re-acquire it).
_auto_check_lock = threading.RLock()


def _discover_available() -> bool:
    """Whether Discover has a Last.fm key to suggest from."""
    return lastfm.is_configured()
