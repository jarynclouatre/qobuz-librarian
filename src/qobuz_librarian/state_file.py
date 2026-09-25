"""Reading the JSON state files without erasing them.

Every store under DATA_DIR is used as a load-modify-save cycle. A loader that
answers "empty" for a file it could not parse hands the caller a blank store,
and the next save writes that blank over the file. One truncated write then
costs the whole thing with nothing on screen to say so. Reading through here
moves the unreadable copy aside as `….corrupt` and says what may have been
reset, which leaves it recoverable and the user told.

A file that is present but unreadable (permissions, a failing disk) is left
alone. The loader raises the read error so a caller cannot replace it with an
empty store.
"""
import fcntl
import json
import logging
import math
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from qobuz_librarian import config as cfg

log = logging.getLogger("qobuz_librarian")


def nonnegative_int(value):
    if value in (None, ""):
        return 0, True
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0, False
    return (parsed, True) if parsed >= 0 else (0, False)


def optional_time(value):
    if value in (None, ""):
        return None, True
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, False
    if not math.isfinite(parsed) or parsed < 0:
        return None, False
    return parsed, True


@contextmanager
def store_lock(path):
    """Hold an exclusive cross-process lock around one load-modify-save.

    The web worker and CLI are separate processes, so a threading lock cannot
    serialize them. The sidecar is part of the store's integrity boundary: if
    it cannot be opened or is not the exact regular file we locked, fail before
    the caller reads state rather than silently allowing a lost update.
    """
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = -1
    locked = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe state-lock access is unavailable")
        descriptor = os.open(
            lock_path,
            os.O_CREAT
            | os.O_RDWR
            | nofollow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        held = os.fstat(descriptor)
        named = os.stat(lock_path, follow_symlinks=False)
        identity = (int(held.st_dev), int(held.st_ino))
        if (
            not stat.S_ISREG(held.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or held.st_nlink != 1
            or named.st_nlink != 1
            or identity != (int(named.st_dev), int(named.st_ino))
        ):
            raise OSError("state-lock namespace is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True

        # The name may have changed while flock waited for another process.
        held = os.fstat(descriptor)
        named = os.stat(lock_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or (int(held.st_dev), int(held.st_ino))
            != (int(named.st_dev), int(named.st_ino))
        ):
            raise OSError("state-lock changed during acquisition")
        yield
    finally:
        if descriptor >= 0:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def file_identity(path):
    """The store's (device, inode, size, mtime), or None when it is absent."""
    try:
        return _identity(os.stat(path))
    except FileNotFoundError:
        return None


def write_json(path, data, *, indent=2, ensure_ascii=True, separators=None):
    """Atomically replace the store at `path` with `data` as JSON.

    A unique temp file in the store's own directory, fsynced before the
    rename: a fixed ".tmp" name lets two processes clobber each other's
    half-written file into place, and an unsynced rename can leave an empty
    store after a power cut. `load_json_object` exists to preserve that damaged
    store. Returns the new file's `file_identity`. Raises OSError; callers keep
    their own policy for surfacing it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii,
                      separators=separators)
            f.flush()
            os.fsync(f.fileno())
            identity = _identity(os.fstat(f.fileno()))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        # Best-effort: make the rename itself durable too.
        dfd = os.open(str(path.parent),
                      os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    return identity


def preserve_corrupt(path, what, reason, lost):
    """Move `path` aside as `….corrupt`, warn, and report whether it moved."""
    path = Path(path)
    dest = path.with_name(path.name + ".corrupt")
    # Never overwrite an earlier kept copy: the first preservation is the one
    # holding the real store, and a later corruption of the near-empty
    # replacement must not clobber it.
    n = 2
    while dest.exists():
        dest = path.with_name(f"{path.name}.corrupt.{n}")
        n += 1
    try:
        path.replace(dest)
        try:
            dest.with_name(dest.name + _CORRUPT_NOTE_SUFFIX).write_text(
                str(lost), encoding="utf-8")
        except OSError:
            pass
        where = (f"the unreadable copy is kept at {dest.name}; recover from "
                 "it if you need what was in it")
        log.warning("%s was corrupt (%s); %s may have been reset and %s.",
                    what, reason, lost, where)
        return True
    except OSError:
        log.warning("%s was corrupt (%s); it was left unchanged because the "
                    "unreadable copy could not be moved aside.", what, reason)
        return False


_CORRUPT_SUFFIX_RE = re.compile(r"\.corrupt(\.\d+)?$")
# Beside each kept copy, what its reset lost, for the notice to name.
_CORRUPT_NOTE_SUFFIX = ".about"
KEPT_CORRUPT_DIR = "unreadable-copies"


def preserved_corrupt_stores():
    """The names of the `….corrupt` copies preserve_corrupt kept in DATA_DIR.

    Preserving the file and warning satisfies half this module's promise; the
    warning goes to the log, which a web user never reads, so the store silently
    reverts to defaults instead. Listing the kept copies lets the UI say so,
    and deleting them clears it, since the list is the whole state.
    """
    try:
        return sorted(
            entry.name
            for entry in Path(cfg.DATA_DIR).iterdir()
            if entry.is_file() and _CORRUPT_SUFFIX_RE.search(entry.name)
        )
    except OSError:
        return []


def corrupt_store_details():
    """Each kept copy with the file it came from and what its reset lost."""
    details = []
    for name in preserved_corrupt_stores():
        try:
            lost = (Path(cfg.DATA_DIR) / (name + _CORRUPT_NOTE_SUFFIX)).read_text(
                encoding="utf-8").strip()
        except (OSError, ValueError):
            lost = ""
        details.append({"name": name, "lost": lost,
                        "original": _CORRUPT_SUFFIX_RE.sub("", name)})
    return details


def keep_corrupt_stores() -> bool:
    """Move every kept copy and its note into KEPT_CORRUPT_DIR, which the
    notice does not list, so it can be dismissed without deleting them."""
    kept = Path(cfg.DATA_DIR) / KEPT_CORRUPT_DIR
    ok = True
    for name in preserved_corrupt_stores():
        try:
            kept.mkdir(exist_ok=True)
            dest = kept / name
            n = 2
            while dest.exists():
                dest = kept / f"{name}.{n}"
                n += 1
            (Path(cfg.DATA_DIR) / name).replace(dest)
        except OSError:
            ok = False
            continue
        note = Path(cfg.DATA_DIR) / (name + _CORRUPT_NOTE_SUFFIX)
        try:
            note.replace(dest.with_name(dest.name + _CORRUPT_NOTE_SUFFIX))
        except OSError:
            pass
    return ok


def clear_corrupt_stores() -> bool:
    """Delete every `….corrupt` copy preserved_corrupt_stores lists.

    The notice already tells the operator to do exactly this; a web app with
    no shell access needs a button that does it, not just the sentence.
    Returns False if a copy could not be removed (the notice then stays up
    for what's left).
    """
    try:
        entries = list(Path(cfg.DATA_DIR).iterdir())
    except OSError:
        return False
    ok = True
    for entry in entries:
        if entry.is_file() and _CORRUPT_SUFFIX_RE.search(entry.name):
            try:
                entry.unlink()
            except OSError:
                ok = False
                continue
            try:
                entry.with_name(entry.name + _CORRUPT_NOTE_SUFFIX).unlink()
            except OSError:
                pass
    return ok


def load_json_object(path, what, lost):
    """Return the JSON object, or None for a missing or preserved corrupt file.

    Read failures raise so a load-modify-save caller cannot replace an
    unreadable store with defaults.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("%s could not be read (%s); leaving it unchanged.", what, e)
        raise
    try:
        text = raw.decode("utf-8")
        # A large store would otherwise sit in memory twice while it parses.
        del raw
        data = json.loads(text)
    except (RecursionError, ValueError) as e:
        if not preserve_corrupt(path, what, e, lost):
            raise
        return None
    if not isinstance(data, dict):
        reason = ValueError("top-level value is not a JSON object")
        if not preserve_corrupt(path, what, reason, lost):
            raise reason
        return None
    return data
