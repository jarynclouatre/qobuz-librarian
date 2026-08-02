"""Multi-provider lyrics fetcher.

The engine behind the import-time lyric hook and the library lyrics pass:
writes synced (or plain) lyrics into FLAC tags or .lrc sidecars.

  • Tries synced lyrics from each provider; rejects results whose timing
    doesn't fit the track length.
  • Falls back to plain lyrics across the providers; only writes plain
    lyrics when the track has no existing lyrics at all.
  • Per-run circuit breaker disables a provider after several consecutive
    connection-style failures.
  • State file tracks per-file status so subsequent passes only re-check
    tracks worth re-checking.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional

from qobuz_librarian import state_file
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion

# mutagen and syncedlyrics are decoupled on purpose: tag-only operations
# (classify/read/write embedded lyrics, .lrc sidecars) need only mutagen,
# while provider fetching needs syncedlyrics. Importing them together would
# null FLAC whenever the network lib is absent, silently disabling the
# mutagen-only paths too.
try:
    from mutagen.flac import FLAC
except Exception:  # mutagen missing — tag I/O unavailable
    FLAC = None  # type: ignore

try:
    import syncedlyrics
    AVAILABLE = FLAC is not None
    IMPORT_ERROR: Optional[Exception] = (
        None if AVAILABLE else ImportError("mutagen unavailable"))
except Exception as _e:  # missing deps shouldn't crash the ingest pipeline
    syncedlyrics = None  # type: ignore
    AVAILABLE = False
    IMPORT_ERROR = _e

# ── Defaults & tunables ──────────────────────────────────────────────────────
DEFAULT_PROVIDERS  = ["Lrclib", "NetEase", "Musixmatch"]
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / ".lyric_fetch_state.json"

# Per-file outcome codes are internal vocabulary; the lyrics pass shows its log
# to the user, so map them to plain phrases (a raw "write-error" or a
# "{'cached-synced': 4}" dict in the activity log reads like debug output).
_OUTCOME_LABELS = {
    "synced": "added synced lyrics", "plain": "added lyrics",
    "fetched": "added lyrics",
    "already-synced": "already had synced lyrics",
    "already-plain": "already had lyrics",
    "cached-synced": "already had synced lyrics",
    "cached-plain": "already had lyrics",
    "kept-existing-plain": "kept existing lyrics",
    "not_found": "no lyrics found", "not-found": "no lyrics found",
    "none": "no lyrics found",
    "providers-unavailable": "lyric providers unavailable",
    "write-error": "couldn't write lyrics",
    "unsafe-path": "refused an unsafe track path",
    "error": "error", "exception": "error",
    "skipped": "skipped", "skipped-long": "skipped (too long)",
    "skipped-tags": "skipped (missing tags)",
}


def _outcome_label(outcome):
    return _OUTCOME_LABELS.get(
        outcome, (outcome or "").replace("-", " ").replace("_", " "))

SYNCED_RE = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
LRC_TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

RECHECK_AFTER_DAYS = 30
MAX_TRACK_SECONDS  = 60 * 20

# Duration-fit tolerances for synced lyrics. The lower-bound check is
# deliberately loose: songs with long instrumental intros/outros legitimately
# leave a large gap between the last lyric timestamp and the track end, and a
# partial LRC is still useful.
LRC_OVERRUN_GRACE  = 15     # last timestamp may exceed track length by this much
LRC_MIN_COVERAGE   = 0.2    # last timestamp should reach at least this fraction
LRC_GAP_GRACE      = 240    # …unless the gap to track end is within this many seconds

# Provider circuit breaker. With ≥8 workers transient errors compound quickly,
# so the threshold is bumped from 3 to give providers more rope before being
# disabled.
PROVIDER_FAIL_THRESHOLD = 5
PROVIDER_COOLDOWN_SECONDS = 600  # 10 min before a disabled provider gets retried
_PROVIDER_ERROR_RE = re.compile(
    r"An error occurred|Connection refused|Max retries|Name or service not known|"
    r"timed out|TimeoutError|NewConnectionError|"
    r"\b429\b|Too Many Requests|rate[- ]?limit|"
    r"\b50[23]\b|Service Unavailable|Bad Gateway",
    re.IGNORECASE,
)
_dead_providers: dict[str, float] = {}   # provider -> epoch when disabled
_provider_fails: dict[str, int] = {}


def _is_provider_dead(prov: str, log: Optional[logging.Logger] = None) -> bool:
    """
    Return True if `prov` is currently in the cooldown window. If the cooldown
    has elapsed, clear the entry (and reset the strike count) so the next call
    actually queries the provider again. Caller must NOT hold _breaker_lock.
    """
    with _breaker_lock:
        disabled_at = _dead_providers.get(prov)
        if disabled_at is None:
            return False
        if time.time() - disabled_at >= PROVIDER_COOLDOWN_SECONDS:
            del _dead_providers[prov]
            _provider_fails[prov] = 0
            if log is not None:
                log.info("provider %s cooldown elapsed — re-enabling", prov)
            return False
        return True

# Locks for concurrent execution. _state_lock guards the shared state dict
# during save_state (iter races with mutation). _breaker_lock guards the
# circuit-breaker counters/sets that all worker threads share.
_state_lock = threading.Lock()
_breaker_lock = threading.Lock()


# ── Thread-safe provider error capture ───────────────────────────────────────
# syncedlyrics logs provider errors via Python's logging module. We capture
# warnings/errors per-thread so the circuit-breaker regex can see them, then
# silence the StreamHandlers syncedlyrics installs on each provider logger so
# they don't double-print to stderr during runs.
class _ChatterCapture(logging.Handler):
    """Buffers warning+ records into a thread-local list during begin/end."""
    _local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        buf = getattr(self._local, "buf", None)
        if buf is None:
            return
        try:
            buf.append(self.format(record))
        except Exception:
            pass

    @classmethod
    def begin(cls) -> None:
        cls._local.buf = []

    @classmethod
    def end(cls) -> str:
        buf = getattr(cls._local, "buf", None)
        cls._local.buf = None
        return "\n".join(buf or [])


_chatter_handler = _ChatterCapture(level=logging.WARNING)
_chatter_handler.setFormatter(logging.Formatter("%(message)s"))

if AVAILABLE:
    # Stop syncedlyrics' LRCProvider.__init__ from re-adding a StreamHandler
    # to each provider's named logger every time it's instantiated (which
    # happens once per syncedlyrics.search() call → handler list grows
    # unbounded and stderr fills with duplicate provider chatter).
    try:
        from syncedlyrics.providers.base import LRCProvider as _LRCProvider
        _orig_lrc_init = _LRCProvider.__init__

        def _quiet_lrc_init(self):  # type: ignore[no-redef]
            _orig_lrc_init(self)
            for h in list(self.logger.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    self.logger.removeHandler(h)

        _LRCProvider.__init__ = _quiet_lrc_init  # type: ignore[assignment]
    except Exception:
        pass

    # Route the syncedlyrics + per-provider loggers through our thread-local
    # capture handler. Disable propagation so they don't escape to root.
    for _name in ("syncedlyrics", "Lrclib", "NetEase", "Megalobiz",
                  "Musixmatch", "Genius"):
        _lg = logging.getLogger(_name)
        for _h in list(_lg.handlers):
            if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
                _lg.removeHandler(_h)
        _lg.propagate = False
        _lg.addHandler(_chatter_handler)
        _lg.setLevel(logging.WARNING)


# ── State ────────────────────────────────────────────────────────────────────
@dataclass
class TrackState:
    mtime: float = 0.0
    size: int = 0
    # synced | plain | not_found | transient | error | skipped | unsafe_path
    status: str = ""
    source: str = ""        # provider that succeeded
    last_seen: float = 0.0  # epoch of last attempt
    representations: str = ""  # embed | sidecar | both


def load_state(path: Path = DEFAULT_STATE_FILE) -> dict[str, TrackState]:
    raw = state_file.load_json_object(
        path, "the lyrics state file",
        "the record of which tracks have already been checked for lyrics")
    if raw is None:
        return {}
    # Strip legacy keys (e.g. 'attempts', removed in schema cleanup) so
    # existing state files load cleanly instead of raising TypeError.
    _known = TrackState.__dataclass_fields__
    return {k: TrackState(**{fk: fv for fk, fv in v.items() if fk in _known})
            for k, v in raw.items() if isinstance(v, dict)}


@contextmanager
def _state_file_lock(path: Path):
    """Hold an exclusive cross-process lock for the state file while reading +
    writing it. The state file is shared by a CLI import hook and the web
    worker (separate processes), so a threading.Lock can't serialise them; an
    flock on a sidecar lock file does. Best-effort — if the lock file can't be
    opened we proceed unlocked rather than block a lyric save."""
    with state_file.store_lock(path):
        yield


def _write_state_unlocked(state: dict[str, TrackState], path: Path) -> None:
    # Unique temp + atomic replace: a shared ".tmp" name let two concurrent
    # checkpoints (a web lyrics pass alongside a CLI import hook) clobber each
    # other's write, and a failed write left the temp orphaned beside the state.
    state_file.write_json(
        path, {k: v.__dict__ for k, v in state.items()},
        indent=0, separators=(",", ":"),
    )


def save_state(state: dict[str, TrackState], path: Path = DEFAULT_STATE_FILE) -> None:
    with _state_file_lock(path):
        _write_state_unlocked(state, path)


def update_state(mutator, path: Path = DEFAULT_STATE_FILE) -> None:
    """Atomically read-modify-write the state file under the cross-process lock.

    `mutator(state)` receives the freshly-loaded dict and mutates it in place.
    Loading and saving inside one lock hold is what makes a prune safe against a
    concurrent checkpoint: a plain load→modify→save (outside the lock) can write
    back a snapshot that drops entries another process added in between."""
    with _state_file_lock(path):
        state = load_state(path)
        mutator(state)
        _write_state_unlocked(state, path)


def prune_missing(state: dict[str, TrackState]) -> int:
    """Drop entries whose file no longer exists, mutating `state` in place.

    Keys are absolute paths, so a moved, renamed, or deleted track otherwise
    leaves an orphan that's reloaded and re-serialised on every walk — the JSON
    grows without bound and is parsed in full each run. Returns the count
    dropped. Mirrors flac_cache.prune_missing."""
    gone = [k for k in state if not os.path.exists(k)]
    for k in gone:
        del state[k]
    return len(gone)


# ── Lyrics classification & tag I/O ──────────────────────────────────────────
def classify(text: Optional[str]) -> str:
    if not text or not text.strip():
        return "none"
    return "synced" if SYNCED_RE.search(text) else "plain"


def get_existing_lyrics(f, path=None, include_sidecar=False, *,
                        parent_fd=None, parent_guard=None,
                        track_guard=None) -> Optional[str]:
    for key in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS"):
        if key in f.tags:
            v = f.tags[key]
            if v:
                return v[0]
    # Sidecars are only trusted through the exact no-follow parent and track
    # binding used by the caller. An unbound path lookup could follow an .lrc
    # symlink outside the owned library and falsely complete this track.
    if include_sidecar and path is not None:
        return _read_bound_sidecar(
            Path(path),
            parent_fd,
            parent_guard,
            track_guard,
        )
    return None


def _regular_identity(value):
    if not stat.S_ISREG(value.st_mode):
        return None
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
    }


def _guard_passes(guard) -> bool:
    if guard is None:
        return False
    try:
        return guard() is True
    except (OSError, TypeError, ValueError):
        return False


def _read_bound_sidecar(
        path: Path, parent_fd, parent_guard, track_guard) -> Optional[str]:
    """Read one exact regular .lrc beside one exact held track, or refuse it."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None or parent_fd is None:
        return None
    descriptor = None
    target = Path(path).with_suffix(".lrc")
    try:
        if not _guard_passes(parent_guard) or not _guard_passes(track_guard):
            return None
        flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(target.name, flags, dir_fd=parent_fd)
        frozen = _regular_identity(os.fstat(descriptor))
        if frozen is None or _named_identity(parent_fd, target.name) != frozen:
            return None
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if (
            not _guard_passes(parent_guard)
            or not _guard_passes(track_guard)
            or _regular_identity(os.fstat(descriptor)) != frozen
            or _named_identity(parent_fd, target.name) != frozen
        ):
            return None
        content = b"".join(chunks).decode("utf-8", errors="replace")
        return content if content.strip() else None
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _valid_regular_identity(value) -> bool:
    return (
        isinstance(value, dict)
        and all(
            isinstance(value.get(field), int)
            for field in (
                "device", "inode", "size", "modified_ns", "changed_ns")
        )
    )


class _BoundTrack:
    """An exact regular file held beneath an exact no-follow directory chain."""

    def __init__(self, path: Path, owned_root: Path):
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if nofollow is None or directory is None:
            raise OSError("safe no-follow file operations are unavailable")

        self.path = Path(os.path.abspath(os.fspath(path)))
        self.root = Path(os.path.abspath(os.fspath(owned_root)))
        try:
            relative = self.path.relative_to(self.root)
        except ValueError:
            raise OSError(
                f"{self.path}: track is outside the owned lyrics root") from None
        if not relative.parts or any(part in ("", ".", "..")
                                     for part in relative.parts):
            raise OSError(f"{self.path}: invalid track path")

        self._directory_fds: list[int] = []
        self._directory_names: list[str] = []
        self.track_fd: Optional[int] = None
        try:
            directory_flags = os.O_RDONLY | directory | nofollow
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            root_fd = os.open(str(self.root), directory_flags)
            root_stat = os.fstat(root_fd)
            named_root = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or not stat.S_ISDIR(named_root.st_mode)
                or root_stat.st_dev != named_root.st_dev
                or root_stat.st_ino != named_root.st_ino
            ):
                os.close(root_fd)
                raise OSError(f"{self.root}: owned lyrics root changed")
            self._directory_fds.append(root_fd)

            for part in relative.parts[:-1]:
                child_fd = os.open(
                    part, directory_flags, dir_fd=self._directory_fds[-1])
                held = os.fstat(child_fd)
                named = os.stat(
                    part,
                    dir_fd=self._directory_fds[-1],
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(held.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or held.st_dev != named.st_dev
                    or held.st_ino != named.st_ino
                ):
                    os.close(child_fd)
                    raise OSError(f"{self.path}: directory chain changed")
                self._directory_names.append(part)
                self._directory_fds.append(child_fd)

            track_flags = (
                os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
            track_flags |= getattr(os, "O_CLOEXEC", 0)
            self.track_fd = os.open(
                relative.parts[-1],
                track_flags,
                dir_fd=self._directory_fds[-1],
            )
            self.identity = _regular_identity(os.fstat(self.track_fd))
            if (
                self.identity is None
                or not self.chain_is_named()
                or self.named_identity() != self.identity
            ):
                raise OSError(f"{self.path}: track identity changed")
        except BaseException:
            self.close()
            raise

    @property
    def parent_fd(self) -> int:
        return self._directory_fds[-1]

    @property
    def name(self) -> str:
        return self.path.name

    def chain_is_named(self) -> bool:
        try:
            held_root = os.fstat(self._directory_fds[0])
            named_root = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(held_root.st_mode)
                or not stat.S_ISDIR(named_root.st_mode)
                or held_root.st_dev != named_root.st_dev
                or held_root.st_ino != named_root.st_ino
            ):
                return False
            for index, name in enumerate(self._directory_names, start=1):
                held = os.fstat(self._directory_fds[index])
                named = os.stat(
                    name,
                    dir_fd=self._directory_fds[index - 1],
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(held.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or held.st_dev != named.st_dev
                    or held.st_ino != named.st_ino
                ):
                    return False
        except (OSError, IndexError):
            return False
        return True

    def named_identity(self):
        try:
            return _named_identity(self.parent_fd, self.name)
        except OSError:
            return None

    def exact_track_is_named(self, track_fd=None, expected=None) -> bool:
        descriptor = self.track_fd if track_fd is None else track_fd
        frozen = self.identity if expected is None else expected
        if descriptor is None or frozen is None or not self.chain_is_named():
            return False
        try:
            return (
                _regular_identity(os.fstat(descriptor)) == frozen
                and self.named_identity() == frozen
            )
        except OSError:
            return False

    def open_named_track(self, expected):
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None or not self.chain_is_named():
            raise OSError(f"{self.path}: track directory changed")
        flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(self.name, flags, dir_fd=self.parent_fd)
        if not self.exact_track_is_named(descriptor, expected):
            os.close(descriptor)
            raise OSError(f"{self.path}: replacement identity changed")
        return descriptor

    def close(self) -> None:
        if self.track_fd is not None:
            try:
                os.close(self.track_fd)
            except OSError:
                pass
            self.track_fd = None
        for descriptor in reversed(self._directory_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._directory_fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _open_parent(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow file operations are unavailable")
    flags = os.O_RDONLY | directory | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(str(path.parent), flags)


def _parent_is_still_named(
        parent_fd: int, path: Path, parent_guard=None) -> bool:
    if parent_guard is not None:
        try:
            return parent_guard() is True
        except (OSError, TypeError, ValueError):
            return False
    try:
        held = os.fstat(parent_fd)
        named = os.stat(path.parent, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(named.st_mode)
        and held.st_dev == named.st_dev
        and held.st_ino == named.st_ino
    )


def _named_identity(parent_fd: int, name: str):
    value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return _regular_identity(value)


def _node_identity(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _named_node_identity(parent_fd: int, name: str):
    return _node_identity(os.stat(
        name, dir_fd=parent_fd, follow_symlinks=False))


def _renameat2(
        first_parent_fd: int,
        first: str,
        second_parent_fd: int,
        second: str,
        flags: int,
) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError(
            errno.ENOTSUP, "atomic exchange is unavailable") from None
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
            flags,
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _rename_exchange(parent_fd: int, first: str, second: str) -> None:
    _renameat2(parent_fd, first, parent_fd, second, 2)


def _rename_noreplace(
        first_parent_fd: int,
        first: str,
        second_parent_fd: int,
        second: str,
) -> None:
    _renameat2(first_parent_fd, first, second_parent_fd, second, 1)


def _make_temp(parent_fd: int, target_name: str):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file operations are unavailable")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)
    for _ in range(16):
        name = f"{target_name}.{secrets.token_hex(8)}.tmp"
        try:
            return name, os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
    raise FileExistsError(f"couldn't reserve a temporary file for {target_name}")


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _copy_file_fd(source_fd: int, target_fd: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    os.lseek(target_fd, 0, os.SEEK_SET)
    os.ftruncate(target_fd, 0)
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        _write_all(target_fd, chunk)


def _fd_has_content(fd: int, expected: bytes) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    offset = 0
    while offset < len(expected):
        chunk = os.read(fd, min(1024 * 1024, len(expected) - offset))
        if not chunk or chunk != expected[offset:offset + len(chunk)]:
            return False
        offset += len(chunk)
    return os.read(fd, 1) == b""


def _emit_receipt(output, receipt) -> None:
    if isinstance(output, dict):
        output.clear()
        output.update(receipt)
    elif isinstance(output, list):
        output.append(receipt)


def _unlink_held_name(
        parent_fd: int,
        name: str,
        held_fd: int,
        expected_identity=None,
        write_exclusion=None,
) -> bool:
    """Remove one held regular file without unlinking through its public name.

    Linux has no inode-conditional unlink. Move the named candidate into a
    freshly-created private directory first, then validate it there. A public
    replacement that wins before the move is restored with no-overwrite rename;
    one that appears afterward is never touched.
    """
    if held_fd is None:
        return False
    try:
        frozen = expected_identity or _regular_identity(os.fstat(held_fd))
        if (
            frozen is None
            or _regular_identity(os.fstat(held_fd)) != frozen
            or _named_identity(parent_fd, name) != frozen
        ):
            return False
    except OSError:
        return False

    directory = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or nofollow is None:
        return False
    quarantine_name = None
    quarantine_fd = None
    moved = False
    moved_node = None

    def _stable_named_node(parent, candidate):
        try:
            value = os.stat(candidate, dir_fd=parent, follow_symlinks=False)
            return (
                int(value.st_dev),
                int(value.st_ino),
                int(value.st_mode),
                int(value.st_size),
                int(value.st_mtime_ns),
            )
        except FileNotFoundError:
            return None
        except OSError:
            return False

    def _capture_quarantined_node():
        nonlocal moved_node
        if quarantine_fd is None:
            return False
        current = _stable_named_node(quarantine_fd, "held")
        if current not in (None, False):
            moved_node = current
            return True
        return False

    def _moved_node_is_public():
        return (
            moved_node is not None
            and _stable_named_node(quarantine_fd, "held") is None
            and _stable_named_node(parent_fd, name) == moved_node
        )

    try:
        for _ in range(16):
            candidate = f".ql-delete-{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            quarantine_name = candidate
            break
        if quarantine_name is None:
            return False
        quarantine_fd = os.open(
            quarantine_name,
            os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        try:
            _rename_noreplace(parent_fd, name, quarantine_fd, "held")
        except BaseException:
            # Python may deliver a signal after renameat2 completed but before
            # its wrapper returned. Derive the namespace outcome from the held
            # inode instead of trusting control flow.
            moved = _capture_quarantined_node()
            raise
        moved = _capture_quarantined_node()
        moved_identity = _regular_identity(os.fstat(held_fd))
        if (
            moved_identity is None
            or _named_identity(quarantine_fd, "held") != moved_identity
            or any(
                moved_identity[field] != frozen[field]
                for field in (
                    "device", "inode", "size", "modified_ns")
            )
        ):
            return False
        if write_exclusion is not None and not write_exclusion.intact():
            return False
        # The only remaining unlink is inside an unpredictable mode-0700
        # directory held open by this process, not through the mutable public
        # path that was validated above.
        try:
            os.unlink("held", dir_fd=quarantine_fd)
        except BaseException:
            # An interrupt after unlink is a completed forward operation. The
            # caller can no longer roll the old inode back, but this positively
            # prevents an empty private quarantine from being stranded.
            moved = _capture_quarantined_node()
            raise
        moved = False
        return True
    except OSError:
        return False
    finally:
        if moved and quarantine_fd is not None:
            try:
                _rename_noreplace(
                    quarantine_fd, "held", parent_fd, name)
                moved = False
            except OSError:
                # A new public file is never overwritten. The quarantined
                # candidate remains recoverable instead of being guessed at.
                moved = not _moved_node_is_public()
            except BaseException:
                # As above, a late signal can mean the restore completed. Keep
                # the derived state, then propagate the interruption.
                moved = not _moved_node_is_public()
                raise
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass
        if quarantine_name is not None and not moved:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except OSError:
                pass


def _exchange_existing(
        parent_fd: int,
        temp_name: str,
        target_name: str,
        temp_fd: int,
        expected_fd: int,
        expected_identity=None,
        new_identity=None,
        post_exchange_guard=None,
):
    path_flag = getattr(os, "O_PATH", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if path_flag is None or nofollow is None:
        raise OSError(errno.ENOTSUP, "safe atomic exchange is unavailable")

    frozen_expected = (
        expected_identity or _regular_identity(os.fstat(expected_fd)))
    frozen_new = new_identity or _regular_identity(os.fstat(temp_fd))
    exclusion = acquire_inode_write_exclusion(expected_fd)
    if exclusion is None:
        raise OSError(
            errno.EBUSY,
            f"{target_name}: target is open for writing or cannot be protected",
        )

    def restore_original_layout() -> bool:
        """Reconcile an exchange whose syscall or follow-up was interrupted."""
        try:
            old_node = _node_identity(os.fstat(expected_fd))
            new_node = _node_identity(os.fstat(temp_fd))
            named_target = _named_node_identity(parent_fd, target_name)
            named_temp = _named_node_identity(parent_fd, temp_name)
        except OSError:
            return False
        if named_target == old_node and named_temp == new_node:
            return True
        if named_target != new_node or named_temp != old_node:
            return False
        try:
            _rename_exchange(parent_fd, temp_name, target_name)
        except BaseException:
            # The exchange may have completed before Python delivered a signal.
            # Derive the outcome below instead of trusting the raised control
            # flow or replacing the original exception.
            pass
        try:
            restored_old_node = _node_identity(os.fstat(expected_fd))
            restored_new_node = _node_identity(os.fstat(temp_fd))
            restored = (
                _named_node_identity(parent_fd, target_name)
                == restored_old_node
                and _named_node_identity(parent_fd, temp_name)
                == restored_new_node
            )
            if restored and isinstance(new_identity, dict):
                # rename changes ctime. Refresh the caller-owned temporary
                # identity so its ordinary finally block can remove only this
                # exact rolled-back candidate instead of stranding it.
                refreshed_new = _regular_identity(os.fstat(temp_fd))
                if (
                    refreshed_new is None
                    or _named_identity(parent_fd, temp_name) != refreshed_new
                ):
                    return False
                new_identity.clear()
                new_identity.update(refreshed_new)
            return restored
        except OSError:
            # Cleanup must not replace the original exception. Both held file
            # descriptors remain open, and no unmatched public name is touched.
            return False

    def forward_layout_complete() -> bool:
        """Prove the new file committed after the exact old name was removed."""
        try:
            new_after = _regular_identity(os.fstat(temp_fd))
            old_after = _regular_identity(os.fstat(expected_fd))
            if new_after is None or old_after is None:
                return False
            try:
                _named_identity(parent_fd, temp_name)
            except FileNotFoundError:
                pass
            else:
                return False
            return (
                all(
                    new_after[field] == frozen_new[field]
                    for field in ("device", "inode", "size", "modified_ns")
                )
                and all(
                    old_after[field] == frozen_expected[field]
                    for field in ("device", "inode", "size", "modified_ns")
                )
                and _named_identity(parent_fd, target_name) == new_after
            )
        except OSError:
            return False

    swapped_fd = None
    try:
        if (
            frozen_expected is None
            or frozen_new is None
            or not exclusion.intact()
            or _regular_identity(os.fstat(expected_fd)) != frozen_expected
            or _regular_identity(os.fstat(temp_fd)) != frozen_new
            or _named_identity(parent_fd, target_name) != frozen_expected
            or _named_identity(parent_fd, temp_name) != frozen_new
        ):
            raise OSError(
                f"{target_name}: target changed before atomic replacement")

        if not exclusion.intact():
            raise OSError(
                f"{target_name}: target changed before atomic replacement")
        try:
            # A signal can be delivered after renameat2 completed but before its
            # wrapper returns, so every exceptional exit from this point first
            # derives the real layout from the two held identities.
            _rename_exchange(parent_fd, temp_name, target_name)
            swapped_fd = os.open(
                temp_name,
                path_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            new_after = _regular_identity(os.fstat(temp_fd))
            old_after = _regular_identity(os.fstat(expected_fd))
            swapped_identity = _regular_identity(os.fstat(swapped_fd))
            names_match = (
                exclusion.intact()
                and new_after is not None
                and old_after is not None
                and all(
                    new_after[field] == frozen_new[field]
                    for field in ("device", "inode", "size", "modified_ns")
                )
                and all(
                    old_after[field] == frozen_expected[field]
                    for field in ("device", "inode", "size", "modified_ns")
                )
                and _named_identity(parent_fd, target_name) == new_after
                and swapped_identity == old_after
                and _named_identity(parent_fd, temp_name) == old_after
            )
            guard_passed = names_match
            if guard_passed and post_exchange_guard is not None:
                try:
                    guard_passed = post_exchange_guard() is True
                except Exception:
                    guard_passed = False
            guard_passed = guard_passed and exclusion.intact()
            if guard_passed:
                if _unlink_held_name(
                        parent_fd,
                        temp_name,
                        expected_fd,
                        old_after,
                        write_exclusion=exclusion,
                ):
                    return new_after
            raise OSError(
                f"{target_name}: target changed during atomic replacement")
        except BaseException as exc:
            # Roll back whenever both exact names remain. If the old temporary
            # name was already unlinked, classify the exact forward layout so
            # the exceptional exit is never mistaken for an unknown swap.
            if not restore_original_layout() and forward_layout_complete():
                exc.add_note(
                    f"{target_name}: atomic replacement completed before "
                    "the interruption was delivered"
                )
            raise
        finally:
            if swapped_fd is not None:
                os.close(swapped_fd)
    finally:
        exclusion.close()


def save_flac_tags(
        f, path: Path, *, identity_change_out=None,
        directory_mutation_out=None, parent_fd=None,
        parent_guard=None, expected_identity=None, commit_guard=None) -> None:
    """Save changed FLAC tags through a durable same-directory replacement."""
    from qobuz_librarian.library.backup import _fsync

    path = Path(path)
    if isinstance(identity_change_out, dict):
        identity_change_out.clear()
    if isinstance(directory_mutation_out, dict):
        directory_mutation_out.clear()
    owned_parent_fd = (
        _open_parent(path) if parent_fd is None else os.dup(parent_fd)
    )
    source_fd = None
    temp_fd = None
    temp_name = None
    temp_identity = None

    def commit_is_allowed() -> bool:
        if commit_guard is None:
            return True
        try:
            return commit_guard() is True
        except Exception:
            return False

    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file operations are unavailable")
        if not commit_is_allowed():
            raise OSError(f"{path.name}: commit guard changed before tag rewrite")
        source_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        source_flags |= getattr(os, "O_CLOEXEC", 0)
        source_fd = os.open(path.name, source_flags, dir_fd=owned_parent_fd)
        before = _regular_identity(os.fstat(source_fd))
        if (
            before is None
            or (expected_identity is not None and before != expected_identity)
            or not _parent_is_still_named(
                owned_parent_fd, path, parent_guard)
            or _named_identity(owned_parent_fd, path.name) != before
            or not commit_is_allowed()
        ):
            raise OSError(f"{path.name}: source changed before tag rewrite")

        temp_name, temp_fd = _make_temp(owned_parent_fd, path.name)
        if isinstance(directory_mutation_out, dict):
            directory_mutation_out["mutated"] = True
        _copy_file_fd(source_fd, temp_fd)
        shutil.copystat(
            f"/proc/self/fd/{source_fd}", f"/proc/self/fd/{temp_fd}")
        if not commit_is_allowed():
            raise OSError(f"{path.name}: commit guard changed during tag rewrite")
        temp_path = Path(f"/proc/self/fd/{owned_parent_fd}/{temp_name}")
        f.save(str(temp_path))

        temp_identity = _regular_identity(os.fstat(temp_fd))
        if temp_identity is None or _named_identity(
                owned_parent_fd, temp_name) != temp_identity:
            raise OSError(f"{path.name}: temporary replacement changed")
        if not _fsync(temp_path):
            raise OSError(
                f"{path.name}: replacement couldn't be flushed to disk; "
                "original left untouched")
        if (
            not _parent_is_still_named(
                owned_parent_fd, path, parent_guard)
            or _regular_identity(os.fstat(source_fd)) != before
            or _named_identity(owned_parent_fd, path.name) != before
            or _regular_identity(os.fstat(temp_fd)) != temp_identity
            or _named_identity(owned_parent_fd, temp_name) != temp_identity
            or not commit_is_allowed()
        ):
            raise OSError(f"{path.name}: source changed during tag rewrite")

        after = _exchange_existing(
            owned_parent_fd,
            temp_name,
            path.name,
            temp_fd,
            source_fd,
            before,
            temp_identity,
            post_exchange_guard=commit_guard,
        )
        temp_name = None
        if (
            after is None
            or not _parent_is_still_named(
                owned_parent_fd, path, parent_guard)
            or _named_identity(owned_parent_fd, path.name) != after
            or not commit_is_allowed()
        ):
            raise OSError(f"{path.name}: replacement identity couldn't be proved")
        _emit_receipt(identity_change_out, {
            "path": os.path.abspath(os.fspath(path)),
            "before": before,
            "after": after,
        })
        if not _fsync(Path(f"/proc/self/fd/{owned_parent_fd}")):
            raise OSError(
                f"{path.name}: replacement completed, but the folder couldn't "
                "be flushed to disk; the change may not survive a power loss")
    finally:
        if temp_name is not None:
            _unlink_held_name(
                owned_parent_fd,
                temp_name,
                temp_fd,
                temp_identity,
            )
        if temp_fd is not None:
            os.close(temp_fd)
        if source_fd is not None:
            os.close(source_fd)
        os.close(owned_parent_fd)


def write_lyrics(f, content: str, *, path=None, **save_kwargs) -> None:
    # Vorbis comments are case-insensitive in mutagen, so assigning
    # "lyrics" already replaces any existing lyrics/LYRICS value — and
    # only the distinct `unsyncedlyrics` field needs explicit removal
    # (deleting it is likewise case-insensitive, so it also clears
    # UNSYNCEDLYRICS). The previous implementation deleted key "LYRICS"
    # *after* writing it which, being case-insensitive, wiped the lyrics
    # just written — embed/both silently stored nothing.
    if "unsyncedlyrics" in f.tags:
        del f.tags["unsyncedlyrics"]
    f.tags["lyrics"] = [content]
    save_flac_tags(
        f, Path(f.filename) if path is None else Path(path), **save_kwargs)


def write_sidecar(
        path: Path, content: str, *, creation_out=None,
        directory_mutation_out=None, parent_fd=None,
        parent_guard=None, track_guard=None, sidecar_identity_out=None) -> bool:
    """Write lyrics to a .lrc file next to the track (UTF-8).

    Never downgrades a user's sidecar: if a non-empty .lrc already exists and is
    SYNCED while the new content is not, keep the existing one. Bound discovery
    recognises the sidecar before any provider fetch, while this final check
    stops a plain result from overwriting a hand-synced .lrc. Return True when a
    sidecar was written and False when the better existing file was kept."""
    from qobuz_librarian.library.backup import _fsync

    path = Path(path)
    target = path.with_suffix(".lrc")
    if isinstance(creation_out, dict):
        creation_out.clear()
    if isinstance(directory_mutation_out, dict):
        directory_mutation_out.clear()
    if isinstance(sidecar_identity_out, (dict, list)):
        sidecar_identity_out.clear()
    owned_parent_fd = (
        _open_parent(target) if parent_fd is None else os.dup(parent_fd)
    )
    existing_fd = None
    existing_identity = None
    temp_fd = None
    temp_name = None
    temp_identity = None
    published_identity = None
    created_target = False
    receipt_emitted = False
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file operations are unavailable")
        if track_guard is not None and track_guard() is not True:
            raise OSError(f"{path.name}: track changed before sidecar write")
        existing_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        existing_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            existing_fd = os.open(
                target.name, existing_flags, dir_fd=owned_parent_fd)
        except FileNotFoundError:
            pass
        if existing_fd is not None:
            existing_identity = _regular_identity(os.fstat(existing_fd))
            if existing_identity is None:
                raise OSError(f"{target.name}: existing sidecar is not a file")
            os.lseek(existing_fd, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(existing_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            old = b"".join(chunks).decode("utf-8", errors="replace")
            if (
                old.strip()
                and classify(old) == "synced"
                and classify(content) != "synced"
            ):
                if (
                    not _guard_passes(track_guard)
                    or not _parent_is_still_named(
                        owned_parent_fd, target, parent_guard)
                    or _regular_identity(
                        os.fstat(existing_fd)) != existing_identity
                    or _named_identity(
                        owned_parent_fd, target.name) != existing_identity
                ):
                    raise OSError(
                        f"{target.name}: existing sidecar changed before keep")
                _emit_receipt(sidecar_identity_out, {
                    "path": os.path.abspath(os.fspath(target)),
                    "file": existing_identity,
                })
                return False

        if track_guard is not None and track_guard() is not True:
            raise OSError(f"{path.name}: track changed before sidecar write")
        temp_name, temp_fd = _make_temp(owned_parent_fd, target.name)
        if isinstance(directory_mutation_out, dict):
            directory_mutation_out["mutated"] = True
        intended_content = content.encode("utf-8")
        _write_all(temp_fd, intended_content)
        temp_path = Path(f"/proc/self/fd/{owned_parent_fd}/{temp_name}")
        if not _fsync(temp_path):
            raise OSError(
                f"{target.name}: replacement couldn't be flushed to disk; "
                "original left untouched")
        temp_identity = _regular_identity(os.fstat(temp_fd))
        if (
            temp_identity is None
            or (track_guard is not None and track_guard() is not True)
            or not _parent_is_still_named(
                owned_parent_fd, target, parent_guard)
            or _named_identity(owned_parent_fd, temp_name) != temp_identity
        ):
            raise OSError(f"{target.name}: temporary sidecar changed")

        if existing_fd is None:
            if _regular_identity(os.fstat(temp_fd)) != temp_identity:
                raise OSError(f"{target.name}: temporary sidecar changed")
            temp_mode = stat.S_IMODE(os.fstat(temp_fd).st_mode)
            _rename_noreplace(
                owned_parent_fd,
                temp_name,
                owned_parent_fd,
                target.name,
            )
            created_target = True
            temp_name = None
            created_stat = os.fstat(temp_fd)
            created = _regular_identity(created_stat)
            named_stat = os.stat(
                target.name,
                dir_fd=owned_parent_fd,
                follow_symlinks=False,
            )
            publication_is_exact = (
                created is not None
                and _regular_identity(named_stat) == created
                and stat.S_IMODE(created_stat.st_mode) == temp_mode
                and all(
                    created[field] == temp_identity[field]
                    for field in (
                        "device", "inode", "size", "modified_ns")
                )
                and _fd_has_content(temp_fd, intended_content)
            )
            if publication_is_exact:
                published_identity = created
            if (
                not publication_is_exact
                or (track_guard is not None and track_guard() is not True)
                or not _parent_is_still_named(
                    owned_parent_fd, target, parent_guard)
            ):
                raise OSError(
                    f"{target.name}: created sidecar changed after publication")
            _emit_receipt(creation_out, {
                "path": os.path.abspath(os.fspath(target)),
                "file": published_identity,
            })
            receipt_emitted = True
        else:
            if (
                _regular_identity(os.fstat(existing_fd)) != existing_identity
                or (track_guard is not None and track_guard() is not True)
                or not _parent_is_still_named(
                    owned_parent_fd, target, parent_guard)
                or _regular_identity(os.fstat(temp_fd)) != temp_identity
                or _named_identity(
                    owned_parent_fd, target.name) != existing_identity
            ):
                raise OSError(f"{target.name}: existing sidecar changed")
            replaced = _exchange_existing(
                owned_parent_fd,
                temp_name,
                target.name,
                temp_fd,
                existing_fd,
                existing_identity,
                temp_identity,
                post_exchange_guard=track_guard,
            )
            temp_name = None
            if (
                replaced is None
                or (track_guard is not None and track_guard() is not True)
                or not _parent_is_still_named(
                    owned_parent_fd, target, parent_guard)
                or _named_identity(
                    owned_parent_fd, target.name) != replaced
            ):
                raise OSError(
                    f"{target.name}: replacement identity couldn't be proved")
            published_identity = replaced

        if not _fsync(Path(f"/proc/self/fd/{owned_parent_fd}")):
            raise OSError(
                f"{target.name}: replacement completed, but the folder couldn't "
                "be flushed to disk; the change may not survive a power loss")
        if (
            (track_guard is not None and not _guard_passes(track_guard))
            or not _parent_is_still_named(
                owned_parent_fd, target, parent_guard)
            or published_identity is None
            or _regular_identity(os.fstat(temp_fd)) != published_identity
            or _named_identity(
                owned_parent_fd, target.name) != published_identity
        ):
            raise OSError(f"{target.name}: final sidecar identity changed")
        _emit_receipt(sidecar_identity_out, {
            "path": os.path.abspath(os.fspath(target)),
            "file": published_identity,
        })
        return True
    finally:
        if (
            created_target
            and not receipt_emitted
            and published_identity is not None
        ):
            _unlink_held_name(
                owned_parent_fd, target.name, temp_fd, published_identity)
        if temp_name is not None:
            _unlink_held_name(
                owned_parent_fd, temp_name, temp_fd, temp_identity)
        if temp_fd is not None:
            os.close(temp_fd)
        if existing_fd is not None:
            os.close(existing_fd)
        os.close(owned_parent_fd)


def write_output(
        path: Path, f, content: str, fmt: str, *, binding):
    """Persist lyrics per fmt: 'embed' (FLAC tag), 'sidecar' (.lrc), 'both'."""
    fmt = _normalise_lyrics_format(fmt)
    if not binding.exact_track_is_named():
        raise OSError(f"{path.name}: track changed before lyric write")

    final_identity = binding.identity
    replacement_fd = None
    if fmt in ("embed", "both"):
        change = {}
        write_lyrics(
            f,
            content,
            path=path,
            identity_change_out=change,
            parent_fd=binding.parent_fd,
            parent_guard=binding.chain_is_named,
            expected_identity=binding.identity,
        )
        if (
            change.get("path") != os.path.abspath(os.fspath(path))
            or change.get("before") != binding.identity
            or not _valid_regular_identity(change.get("after"))
        ):
            raise OSError(f"{path.name}: tag replacement wasn't proven")
        final_identity = change["after"]

    try:
        if fmt in ("sidecar", "both"):
            if final_identity == binding.identity:
                guarded_fd = binding.track_fd
            else:
                replacement_fd = binding.open_named_track(final_identity)
                guarded_fd = replacement_fd

            def track_guard():
                return binding.exact_track_is_named(
                    guarded_fd, final_identity)

            write_sidecar(
                path,
                content,
                parent_fd=binding.parent_fd,
                parent_guard=binding.chain_is_named,
                track_guard=track_guard,
            )
    finally:
        if replacement_fd is not None:
            os.close(replacement_fd)
    return final_identity


# Title suffixes that confuse provider matching. Strip Spotify-style
# "(Remastered 2009)", "(Album Version)", "(Live at Wembley)", "[Mono]",
# trailing " - 2009 Remaster", etc., before querying — providers index the
# canonical title.
_TITLE_NOISE_KEYWORDS = (
    "remaster", "remastered", "remix", "remixed", "re-recorded", "rerecorded",
    "album version", "single version", "radio edit", "radio version",
    "extended version", "extended mix", "edit", "demo", "live",
    "acoustic", "instrumental", "mono", "stereo",
    "bonus track", "bonus", "deluxe", "explicit", "clean version",
    "alternate take", "alternate version", "anniversary",
    "expanded edition", "anniversary edition",
)
_kw_alt = "|".join(re.escape(k) for k in _TITLE_NOISE_KEYWORDS)
_TITLE_NOISE_RE = re.compile(
    rf"\s*\([^()]*(?:{_kw_alt})[^()]*\)|"
    rf"\s*\[[^\[\]]*(?:{_kw_alt})[^\[\]]*\]|"
    rf"\s+-\s+[^-]*(?:{_kw_alt})[^-]*$",
    re.IGNORECASE,
)
del _kw_alt


def _clean_title(title: str) -> str:
    cleaned = title
    for _ in range(4):
        prev = cleaned
        cleaned = _TITLE_NOISE_RE.sub("", cleaned).strip()
        if cleaned == prev:
            break
    return cleaned or title.strip()


def build_query(f) -> Optional[str]:
    title  = (f.tags.get("title")  or [""])[0].strip()
    artist = (f.tags.get("artist") or f.tags.get("albumartist") or [""])[0].strip()
    if not title or not artist:
        return None
    return f"{_clean_title(title)} {artist}"


# ── Duration sanity check for synced LRCs ────────────────────────────────────
def lrc_max_seconds(text: str) -> Optional[float]:
    matches = LRC_TS_RE.findall(text)
    if not matches:
        return None
    best = 0.0
    for mm, ss, frac in matches:
        v = int(mm) * 60 + int(ss)
        if frac:
            v += int(frac.ljust(3, "0")) / 1000.0
        if v > best:
            best = v
    return best


def lrc_duration_sane(lyrics: str, track_seconds: float) -> tuple[bool, str]:
    """Reject LRCs whose timing clearly doesn't fit the track length."""
    if not track_seconds or track_seconds <= 0:
        return True, ""
    last = lrc_max_seconds(lyrics)
    if last is None:
        return True, ""
    if last > track_seconds + LRC_OVERRUN_GRACE:
        return False, f"LRC ends {last:.0f}s past track end ({track_seconds:.0f}s)"
    gap = track_seconds - last
    if last < track_seconds * LRC_MIN_COVERAGE and gap > LRC_GAP_GRACE:
        return False, f"LRC ends at {last:.0f}s, track is {track_seconds:.0f}s"
    return True, ""


# ── Provider query (with circuit breaker) ────────────────────────────────────
def _query_provider(query: str, prov: str, log: logging.Logger,
                    **kwargs) -> tuple[Optional[str], bool]:
    """
    Wrap syncedlyrics.search() for a single provider. Provider errors come
    through Python's logging module; _ChatterCapture buffers them per-thread
    so the circuit-breaker regex can scan them. After PROVIDER_FAIL_THRESHOLD
    strikes the provider is skipped for the rest of this run; any successful
    result clears strikes.

    Returns (result, failed_hard). failed_hard means the provider raised or
    logged a connection-style error — the query never got an answer, which is
    not the same fact as a clean "no lyrics here" and must not be recorded as
    one.
    """
    if _is_provider_dead(prov, log):
        return None, False
    _ChatterCapture.begin()
    raised = False
    try:
        result = syncedlyrics.search(query, providers=[prov], **kwargs)
    except Exception as e:
        log.debug("provider %s raised: %s", prov, e)
        result = None
        raised = True
    chatter = _ChatterCapture.end()
    # A provider that raises (rather than logging) is still a hard failure — it
    # must strike the breaker, or a broken provider is re-queried for every
    # track of a walk instead of being disabled after a few strikes.
    if raised or _PROVIDER_ERROR_RE.search(chatter):
        with _breaker_lock:
            n = _provider_fails.get(prov, 0) + 1
            _provider_fails[prov] = n
            first_line = chatter.splitlines()[0] if chatter else ""
            log.debug("provider %s soft-fail #%d: %s", prov, n, first_line)
            # Announce the disable once, on the strike that trips it. The
            # workers already past the _is_provider_dead check keep arriving
            # with a higher count, and each used to repeat the line with a
            # number that no longer matched the threshold being applied.
            if n >= PROVIDER_FAIL_THRESHOLD and prov not in _dead_providers:
                _dead_providers[prov] = time.time()
                log.info("disabling provider %s for %ds after %d consecutive "
                         "failures (will retry after cooldown)",
                         prov, PROVIDER_COOLDOWN_SECONDS,
                         PROVIDER_FAIL_THRESHOLD)
        return None, True
    if result:
        with _breaker_lock:
            _provider_fails[prov] = 0
    elif not _PROVIDER_ERROR_RE.search(chatter):
        # Clean "not found" — not a connection failure, reset any stale fail count.
        with _breaker_lock:
            _provider_fails[prov] = 0
    return result, False


def search_lyrics(
    query: str, providers: list[str], duration: float, log: logging.Logger,
    skip_plain: bool = False,
) -> tuple[Optional[str], Optional[str], str, int, int]:
    """
    Returns (lyrics, provider_name, kind, providers_tried, failed_hard) where
    kind is 'synced' or 'plain' and providers_tried counts queries actually
    attempted (skipping providers already disabled by the circuit breaker).
    failed_hard counts the attempts that never got an answer (raised, or a
    connection-style error). The caller uses providers_tried==0 and
    failed_hard==providers_tried to distinguish 'no provider has it' from 'no
    provider was reachable', so an outage doesn't poison state.
    """
    tried = 0
    hard = 0
    for prov in providers:
        if _is_provider_dead(prov, log):
            continue
        tried += 1
        result, failed = _query_provider(query, prov, log, synced_only=True)
        hard += failed
        if result and SYNCED_RE.search(result):
            ok, reason = lrc_duration_sane(result, duration)
            if not ok:
                log.info("rejected %s synced result: %s", prov, reason)
                continue
            return result, prov, "synced", tried, hard
    if skip_plain:
        return None, None, "", tried, hard
    for prov in providers:
        if _is_provider_dead(prov, log):
            continue
        tried += 1
        result, failed = _query_provider(query, prov, log, plain_only=True)
        hard += failed
        if result and result.strip():
            return result, prov, "plain", tried, hard
    return None, None, "", tried, hard


# ── Per-file processing & state-aware filter ─────────────────────────────────
def _normalise_lyrics_format(value: str) -> str:
    value = (value or "embed").strip().lower()
    return value if value in ("embed", "sidecar", "both") else "embed"


def _required_representations(value: str) -> tuple[str, ...]:
    value = _normalise_lyrics_format(value)
    if value == "both":
        return ("embed", "sidecar")
    return (value,)


def _representation_state(values: Iterable[str]) -> str:
    present = set(values)
    if {"embed", "sidecar"}.issubset(present):
        return "both"
    if "embed" in present:
        return "embed"
    if "sidecar" in present:
        return "sidecar"
    return ""


def _representations_cover(recorded: str, requested: str) -> bool:
    if recorded == "both":
        present = {"embed", "sidecar"}
    elif recorded in ("embed", "sidecar"):
        present = {recorded}
    else:
        present = set()
    return set(_required_representations(requested)).issubset(present)


def _best_existing_lyrics(*values: Optional[str]) -> Optional[str]:
    for wanted in ("synced", "plain"):
        for value in values:
            if classify(value) == wanted:
                return value
    return None


def should_process(
    path: Path,
    st: Optional[TrackState],
    rescan: bool,
    *,
    mtime: Optional[float] = None,
    size: Optional[int] = None,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
) -> bool:
    """
    Decide whether `path` is worth (re-)processing. mtime/size may be passed
    in by the caller (e.g. from a bulk listing) to avoid an extra path.stat().
    """
    if rescan or st is None:
        return True
    if mtime is None or size is None:
        try:
            stat = path.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            return False
    if int(mtime) != int(st.mtime) or size != st.size:
        return True
    if st.status == "synced":
        # A cached track stat cannot prove a sibling sidecar still exists. Reopen
        # final sidecar/both states through the held no-follow binding each run.
        if "sidecar" in _required_representations(lyrics_format):
            return True
        return not _representations_cover(
            st.representations, lyrics_format)
    if st.status == "plain":
        # --skip-plain caller: treat existing plain as final, don't try to
        # upgrade. Otherwise the default is to re-try every run hoping a
        # provider has gained synced lyrics for the track.
        if not skip_existing_plain:
            return True
        if "sidecar" in _required_representations(lyrics_format):
            return True
        return not _representations_cover(
            st.representations, lyrics_format)
    if st.status in ("not_found", "error", "skipped"):
        # "skipped" (long-track, missing-tags) expires like not_found
        # instead of being re-opened on every run.
        age_days = (time.time() - st.last_seen) / 86400
        return age_days >= RECHECK_AFTER_DAYS
    return True


def _commit(state: dict[str, TrackState], key: str, st: TrackState) -> None:
    with _state_lock:
        state[key] = st


def process_file(
    path: Path, state: dict[str, TrackState],
    providers: list[str], dry_run: bool, log: logging.Logger,
    owned_root: Path,
    synced_only: bool = False,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
) -> str:
    try:
        binding = _BoundTrack(path, owned_root)
    except (OSError, TypeError, ValueError) as e:
        log.warning("unsafe lyric path refused: %s — %s", path, e)
        if not dry_run:
            key = str(path)
            st = state.get(key) or TrackState()
            st.status = "unsafe_path"
            st.source = "outside-owned-root"
            st.last_seen = time.time()
            _commit(state, key, st)
        return "unsafe-path"

    with binding:
        return _process_bound_file(
            path,
            state,
            providers,
            dry_run,
            log,
            binding,
            synced_only=synced_only,
            skip_existing_plain=skip_existing_plain,
            lyrics_format=lyrics_format,
        )


def _process_bound_file(
    path: Path, state: dict[str, TrackState],
    providers: list[str], dry_run: bool, log: logging.Logger,
    binding: _BoundTrack,
    synced_only: bool = False,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
) -> str:
    key = str(path)
    st  = state.get(key) or TrackState()
    # A preview must not change the loaded state, including by mutating an
    # existing TrackState in place. Work on a detached copy and make each
    # commit below a no-op; fetch_for_paths also skips every disk checkpoint.
    if dry_run:
        st = replace(st)

        def commit(*_args, **_kwargs):
            return None
    else:
        commit = _commit

    try:
        f = FLAC(f"/proc/self/fd/{binding.track_fd}")
    except Exception as e:
        log.error("FLAC open failed: %s — %s", path, e)
        st.status = "error"
        st.last_seen = time.time()
        commit(state, key, st)
        return "error"

    track_stat = os.fstat(binding.track_fd)
    st.mtime = track_stat.st_mtime
    st.size = track_stat.st_size

    duration = getattr(f.info, "length", 0) or 0
    if duration > MAX_TRACK_SECONDS:
        st.status = "skipped"
        st.source = "long-track"
        st.last_seen = time.time()
        commit(state, key, st)
        return "skipped-long"

    requested_format = _normalise_lyrics_format(lyrics_format)
    required = _required_representations(requested_format)

    def track_guard():
        return binding.exact_track_is_named()

    embedded = get_existing_lyrics(f)
    sidecar = _read_bound_sidecar(
        path,
        binding.parent_fd,
        binding.chain_is_named,
        track_guard,
    )
    if not track_guard():
        log.warning("track changed during lyric discovery: %s", path)
        st.status = "transient"
        st.source = "track-changed"
        st.representations = ""
        st.last_seen = time.time()
        commit(state, key, st)
        return "write-error"
    representations = {"embed": embedded, "sidecar": sidecar}
    present = {
        name for name, value in representations.items()
        if classify(value) != "none"
    }
    missing = [name for name in required if name not in present]
    considered = (
        tuple(representations.values())
        if missing
        else tuple(representations[name] for name in required)
    )
    existing = _best_existing_lyrics(*considered)
    existing_kind = classify(existing)
    lyrics = None
    source = None
    kind = ""
    write_format = requested_format

    if missing and existing_kind != "none":
        # A requested representation is absent, but the other one already has
        # usable lyrics. Complete the configured format locally instead of
        # making a provider call or rewriting the representation we trust.
        lyrics = existing
        source = "existing-representation"
        kind = existing_kind
        write_format = missing[0]
        action = f"wrote-{kind}"
        st.last_seen = time.time()
    else:
        if not missing and existing_kind == "synced":
            st.status = "synced"
            st.representations = _representation_state(present)
            st.last_seen = time.time()
            commit(state, key, st)
            return "already-synced"

        if (
            not missing
            and existing_kind == "plain"
            and skip_existing_plain
        ):
            # --skip-plain: every requested representation exists and the user
            # opted out of an upgrade pass.
            st.status = "plain"
            st.representations = _representation_state(present)
            st.last_seen = time.time()
            commit(state, key, st)
            return "already-plain"

        query = build_query(f)
        if not query:
            st.status = "skipped"
            st.source = "missing-tags"
            st.representations = _representation_state(present)
            st.last_seen = time.time()
            commit(state, key, st)
            return "skipped-tags"

        # If the file already has plain lyrics, the plain-fallback pass is pure
        # waste: any plain result would be discarded below. The same shortcut
        # applies when the caller asked for synced-only.
        skip_plain = synced_only or existing_kind == "plain"
        lyrics, source, kind, providers_tried, failed_hard = search_lyrics(
            query, providers, duration, log, skip_plain=skip_plain,
        )
        st.last_seen = time.time()

        if not lyrics:
            st.representations = _representation_state(present)
            if providers_tried == 0 or failed_hard == providers_tried:
                # Either the circuit breaker had killed every provider before
                # this file's turn, or every attempt this file made died on a
                # connection-style failure. Neither is a verdict about the
                # track — "not found" here would suppress it for
                # RECHECK_AFTER_DAYS. Keep it immediately retryable.
                st.status = "transient"
                st.source = "providers-unavailable"
                commit(state, key, st)
                return "providers-unavailable"
            if existing_kind == "plain":
                st.status = "plain"
                st.source = "kept-existing"
                commit(state, key, st)
                return "kept-existing-plain"
            st.status = "not_found"
            st.source = ""
            commit(state, key, st)
            return "not-found"

        if kind == "synced":
            action = "wrote-synced"
        elif existing_kind == "none":
            action = "wrote-plain"
        else:
            st.status = "plain"
            st.source = "kept-existing"
            st.representations = _representation_state(present)
            commit(state, key, st)
            return "kept-existing-plain"

    if dry_run:
        st.status = kind
        st.source = f"{source} (dry-run)"
        commit(state, key, st)
        return f"dry:{action}"

    try:
        final_identity = write_output(
            path, f, lyrics, write_format, binding=binding)
    except Exception as e:
        log.error("write failed: %s — %s", path, e)
        if binding.exact_track_is_named():
            st.status = "error"
        else:
            # The provider may have taken seconds to answer. If the public
            # path changed in that interval, refuse this write but retry the
            # newly named track on the next pass instead of suppressing it for
            # the normal error backoff window.
            st.status = "transient"
            st.source = "track-changed"
            st.representations = ""
        commit(state, key, st)
        return "write-error"

    st.status = kind
    st.source = source
    if not _valid_regular_identity(final_identity):
        log.error("write completed without an exact file identity: %s", path)
        st.status = "error"
        st.representations = ""
        commit(state, key, st)
        return "write-error"
    st.mtime = final_identity["modified_ns"] / 1_000_000_000
    st.size = final_identity["size"]
    st.representations = _representation_state(
        present.union(_required_representations(write_format)))
    commit(state, key, st)
    return action


# ── High-level entry point ───────────────────────────────────────────────────
def fetch_for_paths(
    paths: Iterable[Path],
    providers: Optional[list[str]] = None,
    delay: float = 0.0,
    state_path: Path = DEFAULT_STATE_FILE,
    dry_run: bool = False,
    rescan: bool = False,
    log: Optional[logging.Logger] = None,
    save_every: int = 25,
    should_stop: Optional[Callable[[], bool]] = None,
    workers: int = 8,
    synced_only: bool = False,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    *,
    owned_root: Path,
) -> Counter:
    """
    Run the lyrics pipeline over `paths`. Returns Counter of outcome → n.
    State is loaded from / saved to `state_path` so re-runs only re-check
    tracks whose previous outcome warrants it. The per-run provider circuit
    breaker is reset on each call.

    `should_stop`, if supplied, is polled before each file and lets the
    caller stop cleanly (e.g. on SIGINT).

    `workers` controls per-file concurrency. Each file's work is dominated
    by network I/O (multi-provider HTTP), which releases the GIL — so
    threading is the right tool here. Default 8 is a reasonable balance
    against provider rate limits; raise it if your library is huge and
    your providers tolerate more parallelism, lower it (or set 1) to debug.
    """
    if log is None:
        log = logging.getLogger("lyric_fetch")
    if not AVAILABLE:
        log.warning("lyric_fetch unavailable: %s", IMPORT_ERROR)
        return Counter({"unavailable": 0})

    providers = providers or list(DEFAULT_PROVIDERS)
    with _breaker_lock:
        _dead_providers.clear()
        _provider_fails.clear()

    # Drop entries for files that have moved or gone since last run, so the state
    # can't grow without bound across a library's churn. Route through
    # update_state so the prune's read and write happen under one cross-process
    # lock — a plain load→save here would clobber entries a concurrent process
    # added in between.
    if not dry_run:
        update_state(prune_missing, state_path)
    state = load_state(state_path)
    candidates = [Path(p) for p in paths
                  if should_process(Path(p), state.get(str(p)), rescan,
                                    skip_existing_plain=skip_existing_plain,
                                    lyrics_format=lyrics_format)]

    counts: Counter = Counter()
    total = len(candidates)
    workers = max(1, int(workers))

    def run_one(fp: Path) -> str:
        try:
            outcome = process_file(
                fp, state, providers, dry_run, log,
                owned_root,
                synced_only=synced_only,
                skip_existing_plain=skip_existing_plain,
                lyrics_format=lyrics_format,
            )
        except Exception as e:
            log.exception("unexpected error on %s: %s", fp, e)
            outcome = "exception"
        if delay > 0 and outcome.startswith(("wrote-", "dry:", "not-found", "kept-existing")):
            time.sleep(delay)
        return outcome

    def checkpoint() -> None:
        if dry_run:
            return
        try:
            with _state_lock:
                # Merge into the on-disk state under the cross-process lock so a
                # concurrent writer's entries survive — a blind save would clobber
                # whatever another process (CLI import hook / other lane) wrote
                # since this run loaded its snapshot (see update_state's docstring).
                update_state(lambda disk: disk.update(state), state_path)
        except Exception as e:
            # An overnight run shouldn't die because the disk hiccupped on
            # one checkpoint write. Log and keep going — the next checkpoint
            # (or the final one) will retry.
            log.warning("checkpoint failed (continuing): %s", e)

    completed = 0
    stopped = False
    if workers == 1:
        for fp in candidates:
            if should_stop and should_stop():
                stopped = True
                break
            outcome = run_one(fp)
            completed += 1
            counts[outcome] += 1
            log.info("[%d/%d] %s — %s", completed, total, _outcome_label(outcome), fp.name)
            if progress_cb:
                progress_cb(completed, total, fp.name)
            if completed % save_every == 0:
                checkpoint()
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lyrics") as ex:
            remaining = iter(candidates)
            futures = {}

            def submit_available():
                nonlocal stopped
                while not stopped and len(futures) < workers:
                    if should_stop and should_stop():
                        stopped = True
                        return
                    try:
                        fp = next(remaining)
                    except StopIteration:
                        return
                    futures[ex.submit(run_one, fp)] = fp

            submit_available()
            try:
                while futures:
                    done, _ = wait(
                        tuple(futures), return_when=FIRST_COMPLETED)
                    for fut in done:
                        fp = futures.pop(fut)
                        try:
                            outcome = fut.result()
                        except Exception as e:
                            log.exception("worker raised on %s: %s", fp, e)
                            outcome = "exception"
                        # Every submitted worker is drained and counted. Once a
                        # stop is observed no replacement work is queued, so a
                        # returned summary cannot omit a worker that may write.
                        try:
                            completed += 1
                            counts[outcome] += 1
                            log.info("[%d/%d] %s — %s", completed, total, _outcome_label(outcome), fp.name)
                            if progress_cb:
                                progress_cb(completed, total, fp.name)
                            if completed % save_every == 0:
                                checkpoint()
                        except Exception as e:
                            log.exception("post-process error on %s: %s", fp, e)
                    if not stopped and should_stop and should_stop():
                        stopped = True
                    submit_available()
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                raise

    if stopped:
        counts["stopped"] = 1
        counts["stop-total"] = total
    checkpoint()
    return counts


# ── Scan-only indexer ────────────────────────────────────────────────────────
def index_existing(
    items: Iterable,
    *,
    owned_root: Path,
    state_path: Path = DEFAULT_STATE_FILE,
    log: Optional[logging.Logger] = None,
    workers: int = 64,
    save_every: int = 500,
    should_stop: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Counter:
    """
    Fast scan-only pass — open each FLAC, classify the existing lyrics tag,
    and write state entries for files that already have synced or plain
    lyrics. No provider calls, so workers can be cranked far higher than the
    network path tolerates. Use this to seed the state file after a
    fresh import so a subsequent normal run skips already-synced files
    instead of re-checking them.

    `items` is an iterable of either `Path` or `(Path, mtime, size)` tuples.
    Tuple hints are accepted for caller compatibility, but the held descriptor's
    verified stat remains authoritative at this mutation boundary.
    Files for which `state` already records a matching mtime+size are
    skipped without opening the FLAC, so re-running --index after adding
    new files is cheap.
    """
    if log is None:
        log = logging.getLogger("lyric_fetch")
    if not AVAILABLE:
        log.warning("lyric_fetch unavailable: %s", IMPORT_ERROR)
        return Counter({"unavailable": 0})

    normalized: list[tuple[Path, float, int]] = []
    for it in items:
        if isinstance(it, tuple):
            p, mt, sz = it
            normalized.append((Path(p), float(mt or 0), int(sz or 0)))
        else:
            normalized.append((Path(it), 0.0, 0))

    update_state(prune_missing, state_path)
    state = load_state(state_path)
    counts: Counter = Counter()
    total = len(normalized)
    workers = max(1, int(workers))

    def index_one(fp: Path, mt_hint: float, sz_hint: int) -> str:
        key = str(fp)
        try:
            binding = _BoundTrack(fp, owned_root)
        except (OSError, TypeError, ValueError) as e:
            log.warning("unsafe lyric index path refused: %s — %s", fp, e)
            return "unsafe-path"
        with binding:
            held = os.fstat(binding.track_fd)
            mtime, size = held.st_mtime, held.st_size
            cached = state.get(key)
            if (cached is not None
                    and int(mtime) == int(cached.mtime)
                    and size == cached.size
                    and cached.status in (
                        "synced", "plain", "not_found", "skipped")
                    and (
                        cached.status not in ("synced", "plain")
                        or _representations_cover(
                            cached.representations, "embed")
                    )):
                return f"cached-{cached.status}"
            try:
                f = FLAC(f"/proc/self/fd/{binding.track_fd}")
            except Exception as e:
                log.debug("FLAC open failed: %s — %s", fp, e)
                return "open-error"
            kind = classify(get_existing_lyrics(f))
            if kind == "none":
                # Don't write state for files with no lyrics — let the normal
                # run pick them up via should_process(st=None).
                return "no-lyrics"
            st = TrackState(
                mtime=mtime, size=size,
                status=kind, source="indexed",
                last_seen=time.time(),
                representations="embed",
            )
            _commit(state, key, st)
            return f"indexed-{kind}"

    def checkpoint() -> None:
        try:
            with _state_lock:
                # Merge into the on-disk state under the cross-process lock so a
                # concurrent writer's entries survive — a blind save would clobber
                # whatever another process (CLI import hook / other lane) wrote
                # since this run loaded its snapshot (see update_state's docstring).
                update_state(lambda disk: disk.update(state), state_path)
        except Exception as e:
            log.warning("checkpoint failed (continuing): %s", e)

    log.info("indexing %d files with %d workers (no provider calls)",
             total, workers)
    # Disk save is ~1MB JSON dump → expensive; progress log is cheap. Keep
    # them on separate cadences so the user sees movement at workers=32 but
    # we don't write the state file every few hundred ms.
    progress_every = max(1, min(250, total // 50 or 1))
    completed = 0
    stopped = False
    last_log = time.monotonic()
    if workers == 1:
        for fp, mt, sz in normalized:
            if should_stop and should_stop():
                stopped = True
                break
            outcome = index_one(fp, mt, sz)
            completed += 1
            counts[outcome] += 1
            if progress_cb:
                progress_cb(completed, total, fp.name)
            if completed % progress_every == 0:
                now = time.monotonic()
                rate = progress_every / max(0.001, now - last_log)
                last_log = now
                log.info("[%d/%d] · %.0f tracks/s", completed, total, rate)
            if completed % save_every == 0:
                checkpoint()
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lyrics-idx") as ex:
            remaining = iter(normalized)
            futures = {}

            def submit_available():
                nonlocal stopped
                while not stopped and len(futures) < workers:
                    if should_stop and should_stop():
                        stopped = True
                        return
                    try:
                        fp, mt, sz = next(remaining)
                    except StopIteration:
                        return
                    futures[ex.submit(index_one, fp, mt, sz)] = fp

            submit_available()
            try:
                while futures:
                    done, _ = wait(
                        tuple(futures), return_when=FIRST_COMPLETED)
                    for fut in done:
                        fp = futures.pop(fut)
                        try:
                            outcome = fut.result()
                        except Exception as e:
                            log.debug("worker raised on %s: %s", fp, e)
                            outcome = "exception"
                        completed += 1
                        counts[outcome] += 1
                        if progress_cb:
                            progress_cb(completed, total, fp.name)
                        if completed % progress_every == 0:
                            now = time.monotonic()
                            rate = progress_every / max(0.001, now - last_log)
                            last_log = now
                            log.info("[%d/%d] · %.0f tracks/s", completed, total, rate)
                        if completed % save_every == 0:
                            checkpoint()
                    if not stopped and should_stop and should_stop():
                        stopped = True
                    submit_available()
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                raise

    if stopped:
        counts["stopped"] = 1
        counts["stop-total"] = total
    checkpoint()
    return counts
