"""Reads and writes of the beets library database, in place.

Each write is one SQLite transaction on the real file, the way beets writes
it: a crash or a kill part-way leaves the old rows, and SQLite rolls back
the journal it left on the next open. A beets import waits `timeout()`
seconds on a lock; the app's own reads and writes wait up to BEETS_TIMEOUT.
"""
import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from qobuz_librarian import config as cfg

# Version 1.3.4 and earlier wrote each change to a copy in a private folder
# beside the database and swapped it in, under a lock file there; a hard stop
# could leave the folder.
_OLD_COPY_FILES = frozenset(
    f"{name}{suffix}"
    for name in ("source.db", "work.db")
    for suffix in ("", "-journal", "-wal", "-shm")
) | {"receipt.json"}
_OLD_BOOTSTRAP = re.compile(r"\.qobuz-beets-bootstrap-[0-9a-f]{32}")
_OLD_LOCK = ".qobuz-librarian-beets.lock"


def path():
    """The configured database, or None when there is none."""
    configured = getattr(cfg, "BEETS_DB_PATH", None)
    return Path(os.path.abspath(os.fspath(configured))) if configured else None


def timeout():
    """Seconds to wait on a database another program has locked: half the
    import's idle timeout, at most a minute."""
    idle = float(getattr(cfg, "BEETS_TIMEOUT", 0) or 0)
    return 60.0 if not idle or idle >= 120 else idle / 2


def _connect(database):
    # The app writes once an import is done and its files are in place, so
    # it waits out a long `beet update` rather than leave stale rows behind.
    # mode=rw never creates a database that is missing.
    wait = max(float(getattr(cfg, "BEETS_TIMEOUT", 0) or 0), timeout())
    return sqlite3.connect(
        f"file:{quote(os.fspath(database))}?mode=rw",
        uri=True,
        timeout=wait,
        isolation_level=None,
    )


@contextmanager
def transaction(database):
    """One write transaction, committed when the block ends.

    The block checks the rows it changes and raises to refuse; anything
    raised rolls back, and so does a block that rolls back itself."""
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        if connection.in_transaction:
            connection.execute("COMMIT")
    finally:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


def read(database, inspect):
    """Return `inspect(connection)` over one consistent read of the database."""
    connection = _connect(database)
    try:
        # A read-only open cannot roll back a journal a killed writer left,
        # so this one may, and refuses writes instead.
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        return inspect(connection)
    finally:
        connection.close()


def remove_old_copies():
    """Remove what earlier versions left beside the database: the copies a
    hard stop cut short and their lock file. Only while the database itself
    is there, so a copy is never the last one, and never anything it cannot
    tell is one of those. Returns how many copies it removed."""
    database = path()
    try:
        if database is None or not database.is_file() or not database.stat().st_size:
            return 0
        with os.scandir(database.parent) as entries:
            entries = list(entries)
    except OSError:
        return 0
    digest = hashlib.sha256(os.fsencode(database.name)).hexdigest()
    folder = re.compile(rf"\.qobuz-sqlite-{digest}-[0-9a-f]{{24}}")
    removed = 0
    for entry in entries:
        try:
            if folder.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                with os.scandir(entry.path) as inside:
                    files = list(inside)
                if not all(
                    item.name in _OLD_COPY_FILES and item.is_file(follow_symlinks=False)
                    for item in files
                ):
                    continue
                for item in files:
                    os.unlink(item.path)
                os.rmdir(entry.path)
                removed += 1
            elif (
                (entry.name == _OLD_LOCK and entry.stat(follow_symlinks=False).st_size == 0)
                or _OLD_BOOTSTRAP.fullmatch(entry.name)
            ) and entry.is_file(follow_symlinks=False):
                os.unlink(entry.path)
        except OSError:
            continue
    return removed
