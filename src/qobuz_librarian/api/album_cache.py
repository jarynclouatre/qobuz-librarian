"""Persistent cache of ``get_album`` responses, keyed on album id.

A Qobuz album's metadata and track list don't change, so a fetched album is safe
to keep indefinitely. Library scans call ``get_album`` once per owned album to
materialise its track list (``get_artist_albums`` returns counts, not items), and
that per-album call is the dominant API cost of a scan. Serving it from a local
SQLite lookup turns a re-scan of an unchanged library from minutes of round-trips
into milliseconds.

SQLite (not one big JSON) so a few thousand albums are written incrementally and
read by id without rewriting the whole file, and so a scan's parallel workers can
each hold their own connection. Delete the db file to force a full refresh.
"""
import json
import math
import sqlite3
import threading
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.logging import vlog

_init_lock = threading.Lock()
_initialized = False
# Bumped when a corrupt db is discarded; _conn() reopens a thread's connection
# when its generation lags, so a sibling scan worker stops writing into the
# deleted inode after another thread rebuilt the db.
_generation = 0
_local = threading.local()


def _db_path() -> Path:
    return Path(str(cfg.DATA_DIR)) / "album_cache.db"


def _is_corrupt_error(e: sqlite3.Error) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in
               ("malformed", "not a database", "file is encrypted"))


def _discard_corrupt_db() -> bool:
    """Remove a malformed cache db (and its WAL sidecars). Returns True if
    anything was cleared. The cache is derived data - losing it just makes
    the next scan refetch from Qobuz, which beats a permanently dead cache."""
    db = _db_path()
    cleared = False
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        try:
            p.unlink()
            cleared = True
        except FileNotFoundError:
            pass
        except OSError as e:
            vlog(f"couldn't clear corrupt album cache {p.name}: {e}")
            return False
    if cleared:
        vlog("album cache was corrupt - rebuilt from scratch")
    return cleared


def _handle_db_error(e: sqlite3.Error) -> None:
    """Recover from a corrupt-db error surfaced by a read/write. SQLite data-page
    corruption (common after an unclean NAS/container power off, the
    deployment this app targets) often passes connect + 'CREATE TABLE IF NOT
    EXISTS' and only surfaces as 'database disk image is malformed' on a later
    row access - which _ensure() never re-checks.
    """
    global _initialized, _generation
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None
    if not _is_corrupt_error(e):
        return
    with _init_lock:
        # First thread to notice clears the file and resets the init flag; a
        # concurrent rebuild (which re-sets _initialized) isn't torn down.
        if _initialized and _discard_corrupt_db():
            _initialized = False
            _generation += 1
            import logging
            logging.getLogger("qobuz_librarian").info(
                "album cache was corrupt - discarded; it rebuilds on next scan")


def _ensure() -> bool:
    """Create the tables once. Returns False if the cache can't be used."""
    global _initialized
    if not cfg.ALBUM_CACHE_ENABLED:
        return False
    if _initialized:
        return True
    with _init_lock:
        if _initialized:
            return True
        try:
            _db_path().parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            vlog(f"album cache dir unavailable ({e}); proceeding without it")
            return False
        for attempt in (1, 2):
            try:
                conn = sqlite3.connect(str(_db_path()), timeout=5)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS albums "
                        "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)")
                    # Artist catalogs change when new releases drop, so unlike
                    # album track lists they're served with a TTL (see get_catalog).
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS catalogs "
                        "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)")
                    conn.commit()
                finally:
                    conn.close()
                _initialized = True
                return True
            except sqlite3.Error as e:
                # A corrupt db is the one error we can fix: drop it and retry
                # once.
                if attempt == 1 and _is_corrupt_error(e) and _discard_corrupt_db():
                    continue
                vlog(f"album cache init failed ({e}); proceeding without it")
                return False
        return False


def _conn() -> sqlite3.Connection:
    """Connection scoped to the calling thread.

    A scan looks up one cached album per owned album; opening a fresh connection
    per lookup costs many times the lookup it's meant to make cheap, so each
    thread keeps one (SQLite connections can't be shared across threads).
    synchronous is dropped to NORMAL - the cache is derived data, so a row lost
    to an OS crash is just refetched from Qobuz on the next scan.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) != _generation:
        # Another thread discarded a corrupt db; drop this stale handle so we
        # don't keep writing into the deleted inode.
        try:
            conn.close()
        except sqlite3.Error:
            pass
        conn = None
        _local.conn = None
    if conn is None:
        conn = sqlite3.connect(str(_db_path()), timeout=5)
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
        _local.generation = _generation
    return conn


def get(album_id) -> dict | None:
    if not album_id or not _ensure():
        return None
    try:
        row = _conn().execute(
            "SELECT payload FROM albums WHERE id = ?", (str(album_id),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"album cache read failed: {e}")
        _handle_db_error(e)
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


# Cap the albums table so it can't grow without bound over a library's
# lifetime.
_CACHE_MAX_ALBUMS = 10000
_TRIM_EVERY = 200
_puts_since_trim = 0


def _trim_albums() -> None:
    try:
        conn = _conn()
        conn.execute(
            "DELETE FROM albums WHERE id NOT IN "
            "(SELECT id FROM albums ORDER BY fetched_at DESC LIMIT ?)",
            (_CACHE_MAX_ALBUMS,))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"album cache trim failed: {e}")


def put(album_id, payload) -> None:
    global _puts_since_trim
    if not album_id or not isinstance(payload, dict) or not _ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO albums (id, payload, fetched_at) "
            "VALUES (?, ?, ?)", (str(album_id), data, time.time()))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"album cache write failed: {e}")
        _handle_db_error(e)
        return
    _puts_since_trim += 1
    if _puts_since_trim >= _TRIM_EVERY:
        _puts_since_trim = 0
        _trim_albums()


def get_catalog(key, ttl_seconds) -> dict | None:
    """Cached artist-catalog payload for ``key`` if newer than ``ttl_seconds``.

    A non-positive TTL always misses, which disables catalog caching while
    leaving the (immutable) album cache on."""
    if not key or ttl_seconds <= 0 or not _ensure():
        return None
    try:
        row = _conn().execute(
            "SELECT payload, fetched_at FROM catalogs WHERE key = ?",
            (str(key),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"catalog cache read failed: {e}")
        _handle_db_error(e)
        return None
    if not row:
        return None
    try:
        fetched_at = float(row[1] or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(fetched_at)
        or (time.time() - fetched_at) > ttl_seconds
    ):
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def put_catalog(key, payload) -> None:
    if not key or not isinstance(payload, dict) or not _ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO catalogs (key, payload, fetched_at) "
            "VALUES (?, ?, ?)", (str(key), data, time.time()))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"catalog cache write failed: {e}")
        _handle_db_error(e)


def _reset_for_tests() -> None:
    global _initialized, _generation
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
    _local.generation = None
    _initialized = False
    _generation = 0
