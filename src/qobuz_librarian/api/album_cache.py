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

from qobuz_librarian import config as cfg
from qobuz_librarian.cache_db import CacheDB
from qobuz_librarian.ui_cli.logging import vlog

_db = CacheDB(
    "album_cache.db", "album cache", (
        "CREATE TABLE IF NOT EXISTS albums "
        "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)",
        # Artist catalogs change when new releases drop, so unlike
        # album track lists they're served with a TTL (see get_catalog).
        "CREATE TABLE IF NOT EXISTS catalogs "
        "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)",
        # When a row was last used or confirmed unchanged. Kept apart from
        # the payloads: SQLite rewrites a whole row to change one column, and
        # a new-release check confirming 2,000 catalogs rewrote them all.
        "CREATE TABLE IF NOT EXISTS stamps "
        "(kind TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL, "
        "PRIMARY KEY (kind, key)) WITHOUT ROWID",
    ),
    enabled=lambda: cfg.ALBUM_CACHE_ENABLED,
)


def get(album_id) -> dict | None:
    if not album_id or not _db.ensure():
        return None
    try:
        row = _db.conn().execute(
            "SELECT payload FROM albums WHERE id = ?", (str(album_id),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"album cache read failed: {e}")
        _db.handle_db_error(e)
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    with _used_lock:
        _used.add(str(album_id))
        due = len(_used) >= _TRIM_EVERY
    if due:
        _record_use()
    return payload


# Cap the albums table so it can't grow without bound over a library's
# lifetime. A row's fetched_at moves forward whenever it is read, and only
# rows unused for _KEEP_USED_SECONDS count against the cap, so a library
# scanned at least that often keeps every album it owns however many.
_CACHE_MAX_ALBUMS = 10000
_KEEP_USED_SECONDS = 60 * 86400
_TRIM_EVERY = 200
_puts_since_trim = 0
_used = set()
_used_lock = threading.Lock()


def _record_use() -> None:
    with _used_lock:
        ids = list(_used)
        _used.clear()
    if not ids:
        return
    now = time.time()
    try:
        conn = _db.conn()
        conn.executemany(
            "INSERT OR REPLACE INTO stamps (kind, key, at) VALUES ('album', ?, ?)",
            [(album_id, now) for album_id in ids])
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"album cache write failed: {e}")
        _db.handle_db_error(e)


def _trim_albums() -> None:
    _record_use()
    try:
        conn = _db.conn()
        used = ("SELECT a.id, MAX(COALESCE(a.fetched_at, 0), COALESCE(s.at, 0)) "
                "AS at FROM albums a LEFT JOIN stamps s "
                "ON s.kind = 'album' AND s.key = a.id")
        conn.execute(
            f"DELETE FROM albums WHERE id IN (SELECT id FROM ({used}) "
            "WHERE at < ? AND id NOT IN "
            f"(SELECT id FROM ({used}) ORDER BY at DESC LIMIT ?))",
            (time.time() - _KEEP_USED_SECONDS, _CACHE_MAX_ALBUMS))
        conn.execute("DELETE FROM stamps WHERE kind = 'album' AND key NOT IN "
                     "(SELECT id FROM albums)")
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"album cache trim failed: {e}")


def put(album_id, payload) -> None:
    global _puts_since_trim
    if not album_id or not isinstance(payload, dict) or not _db.ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _db.conn()
        conn.execute(
            "INSERT OR REPLACE INTO albums (id, payload, fetched_at) "
            "VALUES (?, ?, ?)", (str(album_id), data, time.time()))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"album cache write failed: {e}")
        _db.handle_db_error(e)
        return
    _puts_since_trim += 1
    if _puts_since_trim >= _TRIM_EVERY:
        _puts_since_trim = 0
        _trim_albums()


def get_catalog(key, ttl_seconds) -> dict | None:
    """Cached artist-catalog payload for ``key`` if newer than ``ttl_seconds``.

    A non-positive TTL always misses, which disables catalog caching while
    leaving the (immutable) album cache on."""
    if not key or ttl_seconds <= 0 or not _db.ensure():
        return None
    try:
        row = _db.conn().execute(
            "SELECT c.payload, MAX(COALESCE(c.fetched_at, 0), COALESCE(s.at, 0)) "
            "FROM catalogs c LEFT JOIN stamps s "
            "ON s.kind = 'catalog' AND s.key = c.key WHERE c.key = ?",
            (str(key),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"catalog cache read failed: {e}")
        _db.handle_db_error(e)
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
    if not key or not isinstance(payload, dict) or not _db.ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _db.conn()
        row = conn.execute("SELECT payload FROM catalogs WHERE key = ?",
                           (str(key),)).fetchone()
        if row and row[0] == data:
            conn.execute(
                "INSERT OR REPLACE INTO stamps (kind, key, at) "
                "VALUES ('catalog', ?, ?)", (str(key), time.time()))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO catalogs (key, payload, fetched_at) "
                "VALUES (?, ?, ?)", (str(key), data, time.time()))
            conn.execute("DELETE FROM stamps WHERE kind = 'catalog' AND key = ?",
                         (str(key),))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"catalog cache write failed: {e}")
        _db.handle_db_error(e)


def _reset_for_tests() -> None:
    _used.clear()
    _db.reset_for_tests()
