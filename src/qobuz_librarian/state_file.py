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
import json
import logging
from pathlib import Path

log = logging.getLogger("qobuz_librarian")


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
