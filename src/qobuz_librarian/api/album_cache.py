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
    return payload if isinstance(payload, dict) else None


# Cap the albums table so it can't grow without bound over a library's
# lifetime.
_CACHE_MAX_ALBUMS = 10000
_TRIM_EVERY = 200
_puts_since_trim = 0


def _trim_albums() -> None:
    try:
        conn = _db.conn()
        conn.execute(
            "DELETE FROM albums WHERE id NOT IN "
            "(SELECT id FROM albums ORDER BY fetched_at DESC LIMIT ?)",
            (_CACHE_MAX_ALBUMS,))
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
            "SELECT payload, fetched_at FROM catalogs WHERE key = ?",
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
        conn.execute(
            "INSERT OR REPLACE INTO catalogs (key, payload, fetched_at) "
            "VALUES (?, ?, ?)", (str(key), data, time.time()))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"catalog cache write failed: {e}")
        _db.handle_db_error(e)


def _reset_for_tests() -> None:
    _db.reset_for_tests()
