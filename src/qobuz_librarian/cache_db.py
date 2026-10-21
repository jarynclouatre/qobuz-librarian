"""Thread-local SQLite connections for persistent caches."""
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.logging import vlog

log = logging.getLogger("qobuz_librarian")


class _ThreadConnection:
    """One thread's connection, closed when the thread ends.

    A connection sits in a reference cycle with its statement cache, so a
    finished worker's connection would otherwise keep its database and WAL
    files open until the next garbage collection."""

    def __init__(self, conn: sqlite3.Connection, generation: int):
        self.conn = conn
        self.generation = generation

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    __del__ = close


class CacheDB:
    """Manage one cache database and recover from corrupt files."""

    def __init__(self, filename: str, label: str, schema: tuple[str, ...], *,
                 enabled: Callable[[], bool] | None = None):
        self._filename = filename
        self._label = label
        self._schema = schema
        self._enabled = enabled
        self._init_lock = threading.Lock()
        self._initialized = False
        self._generation = 0
        self._local = threading.local()

    def _db_path(self) -> Path:
        return Path(str(cfg.DATA_DIR)) / self._filename

    @staticmethod
    def _is_corrupt_error(e: sqlite3.Error) -> bool:
        msg = str(e).lower()
        return any(s in msg for s in
                   ("malformed", "not a database", "file is encrypted"))

    def _discard_corrupt_db(self) -> bool:
        """Remove a malformed cache database and its WAL sidecars."""
        db = self._db_path()
        cleared = False
        for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            try:
                p.unlink()
                cleared = True
            except FileNotFoundError:
                pass
            except OSError as e:
                vlog(f"couldn't clear corrupt {self._label} {p.name}: {e}")
                return False
        if cleared:
            vlog(f"{self._label} was corrupt - rebuilt from scratch")
        return cleared

    def handle_db_error(self, e: sqlite3.Error) -> None:
        """Close this thread's connection and discard a corrupt database."""
        self.close()
        if not self._is_corrupt_error(e):
            return
        with self._init_lock:
            if self._initialized and self._discard_corrupt_db():
                self._initialized = False
                self._generation += 1
                log.info("%s was corrupt - discarded; it rebuilds on next scan",
                         self._label)

    def ensure(self) -> bool:
        """Create the tables once, returning False if the cache is unavailable."""
        if self._enabled is not None and not self._enabled():
            return False
        if self._initialized:
            return True
        with self._init_lock:
            if self._initialized:
                return True
            try:
                self._db_path().parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                vlog(f"{self._label} dir unavailable ({e}); proceeding without it")
                return False
            for attempt in (1, 2):
                try:
                    conn = sqlite3.connect(str(self._db_path()), timeout=5)
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        for statement in self._schema:
                            conn.execute(statement)
                        conn.commit()
                    finally:
                        conn.close()
                    self._initialized = True
                    return True
                except sqlite3.Error as e:
                    if attempt == 1 and self._is_corrupt_error(e) and self._discard_corrupt_db():
                        continue
                    vlog(f"{self._label} init failed ({e}); proceeding without it")
                    return False
            return False

    def conn(self) -> sqlite3.Connection:
        """Return this thread's connection, reopening after corrupt-file recovery."""
        held = getattr(self._local, "held", None)
        if held is not None and held.generation != self._generation:
            self.close()
            held = None
        if held is None:
            conn = sqlite3.connect(str(self._db_path()), timeout=5)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=4194304")
            held = self._local.held = _ThreadConnection(conn, self._generation)
        return held.conn

    def close(self) -> None:
        """Close this thread's connection."""
        held = getattr(self._local, "held", None)
        self._local.held = None
        if held is not None:
            held.close()

    def reset_for_tests(self) -> None:
        """Close this thread's connection and reset database initialization."""
        self.close()
        self._initialized = False
        self._generation = 0
