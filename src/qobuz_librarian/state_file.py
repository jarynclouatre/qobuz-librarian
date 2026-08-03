"""Reading the JSON state files without erasing them.

Every store under DATA_DIR is used as a load-modify-save cycle. A loader that
answers "empty" for a file it could not parse hands the caller a blank store,
and the next save writes that blank over the file — so one truncated write
costs the whole thing with nothing on screen to say so. Reading through here
moves the unreadable copy aside as `….corrupt` and says what may have been
reset, which leaves it recoverable and the user told.

A file that is present but unreadable (permissions, a failing disk) is left
alone: the rename would fail for the same reason and the content is probably
intact, so that case only warns and the run treats the store as empty.
"""
import fcntl
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("qobuz_librarian")


@contextmanager
def store_lock(path):
    """Hold an exclusive cross-process lock around one store's
    load-modify-save. The web worker and the CLI are separate processes, so a
    threading.Lock can't serialise them; an flock on a sidecar lock file
    does. Best-effort — when the lock file can't even be opened, proceed
    unlocked rather than refuse the save."""
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    fh = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w", encoding="utf-8")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        if fh is not None:
            fh.close()
            fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


def write_json(path, data, *, indent=2, ensure_ascii=True, separators=None):
    """Atomically replace the store at `path` with `data` as JSON.

    A unique temp file in the store's own directory, fsynced before the
    rename: a fixed ".tmp" name lets two processes clobber each other's
    half-written file into place, and an unsynced rename can leave an empty
    store after a power cut — the very corruption load_json_object exists to
    survive. Raises OSError; callers keep their own policy for surfacing it."""
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


def preserve_corrupt(path, what, reason, lost):
    """Move `path` aside as `….corrupt` and warn. Best-effort — a rename that
    can't happen (read-only volume) still warns."""
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
        where = (f"the unreadable copy is kept at {dest.name} — recover from "
                 "it if you need what was in it")
    except OSError:
        where = "the unreadable copy could not be moved aside"
    log.warning("%s was corrupt (%s); %s may have been reset and %s.",
                what, reason, lost, where)


_CORRUPT_SUFFIX_RE = re.compile(r"\.corrupt(\.\d+)?$")


def preserved_corrupt_stores():
    """The names of the `….corrupt` copies preserve_corrupt kept in DATA_DIR.

    Preserving the file and warning satisfies half this module's promise; the
    warning goes to the log, which a web user never reads, so the store silently
    reverts to defaults instead. Listing the kept copies lets the UI say so —
    and deleting them clears it, since the list is the whole state.
    """
    from qobuz_librarian import config as cfg

    try:
        return sorted(
            entry.name
            for entry in Path(cfg.DATA_DIR).iterdir()
            if entry.is_file() and _CORRUPT_SUFFIX_RE.search(entry.name)
        )
    except OSError:
        return []


def load_json_object(path, what, lost):
    """The store at `path` as a dict, or None when there is nothing usable —
    missing, unreadable, or corrupt. A corrupt file is preserved first."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("%s could not be read (%s); treating it as empty for this "
                    "run.", what, e)
        return None
    try:
        data = json.loads(raw)
    except ValueError as e:
        preserve_corrupt(path, what, e, lost)
        return None
    if not isinstance(data, dict):
        preserve_corrupt(path, what, "top-level value is not a JSON object",
                         lost)
        return None
    return data
