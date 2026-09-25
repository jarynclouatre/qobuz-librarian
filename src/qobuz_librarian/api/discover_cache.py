"""SQLite cache for Last.fm results and assembled Discover feeds."""
import json
import math
import sqlite3
import time

from qobuz_librarian.cache_db import CacheDB
from qobuz_librarian.ui_cli.logging import vlog

# How long each kind of row stays usable.
SIMILAR_TTL    = 14 * 86400
TAGS_TTL       = 14 * 86400
RESOLUTION_TTL = 30 * 86400
FEED_TTL       = 7 * 86400
# Favourites are the user's own list and change the moment they star something
# in Qobuz, so this one is short. Rebuilding it costs one request.
FAVOURITES_TTL = 300

_MAX_FEEDS = 100

# Stored in place of a resolution when Qobuz has nothing for that name.
_MISS = {"miss": True}

_db = CacheDB(
    "discover_cache.db", "discover cache", (
        "CREATE TABLE IF NOT EXISTS lastfm "
        "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)",
        "CREATE TABLE IF NOT EXISTS resolutions "
        "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)",
        "CREATE TABLE IF NOT EXISTS feeds "
        "(kind TEXT PRIMARY KEY, payload TEXT NOT NULL, "
        "library_sig TEXT, built_at REAL)",
    ),
)


# Built here so the writer and the reader can't drift apart: a key typo would
# show up as a cache that never hits, which looks like Last.fm being slow.
def similar_key(artist_key: str) -> str:
    return f"similar:{artist_key}"


def tags_key(artist_key: str) -> str:
    return f"toptags:{artist_key}"


def tag_albums_key(tag: str, page: int) -> str:
    return f"tagalbums:{tag}:{page}"


def artist_resolution_key(artist_key: str) -> str:
    return f"artist:{artist_key}"


def album_resolution_key(artist_key: str, title_key: str) -> str:
    return f"album:{artist_key}|{title_key}"


def _read(table: str, id_column: str, key: str, ttl_seconds: float,
          allow_stale: bool):
    if not key or not _db.ensure():
        return None
    try:
        row = _db.conn().execute(
            f"SELECT payload, fetched_at FROM {table} WHERE {id_column} = ?",
            (str(key),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"discover cache read failed: {e}")
        _db.handle_db_error(e)
        return None
    if not row:
        return None
    if not allow_stale:
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
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def _write(table: str, id_column: str, key: str, payload) -> None:
    if not key or not isinstance(payload, (dict, list)) or not _db.ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _db.conn()
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({id_column}, payload, fetched_at) "
            "VALUES (?, ?, ?)", (str(key), data, time.time()))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"discover cache write failed: {e}")
        _db.handle_db_error(e)
        return
    _count_put()


def get_lastfm(key: str, ttl_seconds: float, *, allow_stale: bool = False):
    """A saved Last.fm answer, or None. ``allow_stale`` ignores the age, which
    is how an expired row still fills the page when Last.fm is unreachable."""
    payload = _read("lastfm", "key", key, ttl_seconds, allow_stale)
    if (
        not isinstance(payload, list)
        or any(not isinstance(row, dict) for row in payload)
    ):
        return None
    return payload


def put_lastfm(key: str, payload) -> None:
    _write("lastfm", "key", key, payload)


def get_resolution(key: str, ttl_seconds: float = RESOLUTION_TTL,
                   *, allow_stale: bool = False):
    """What a name resolved to on Qobuz."""
    payload = _read("resolutions", "key", key, ttl_seconds, allow_stale)
    return payload if isinstance(payload, dict) else None


def put_resolution(key: str, payload) -> None:
    _write("resolutions", "key", key, payload)


def put_resolution_miss(key: str) -> None:
    """Remember that Qobuz has nothing under this name."""
    _write("resolutions", "key", key, dict(_MISS))


def is_miss(payload) -> bool:
    return isinstance(payload, dict) and payload.get("miss") is True


def get_feed(kind: str) -> dict | None:
    """The saved feed and the two facts that decide whether it can still be
    used: which library it was built from, and when.

    Returned whole rather than filtered by age here, because freshness is a
    page-level decision: a feed too old to serve straight is still what gets
    shown, with a notice, when Last.fm can't be reached to rebuild it.
    """
    if not kind or not _db.ensure():
        return None
    try:
        row = _db.conn().execute(
            "SELECT payload, library_sig, built_at FROM feeds WHERE kind = ?",
            (str(kind),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"discover cache feed read failed: {e}")
        _db.handle_db_error(e)
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, list):
        return None
    if kind == "tags":
        valid_items = all(isinstance(item, str) for item in payload)
    else:
        valid_items = all(isinstance(item, dict) for item in payload)
    if not valid_items:
        return None
    try:
        built_at = float(row[2] or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(built_at) or built_at < 0:
        return None
    return {"payload": payload,
            "library_sig": str(row[1] or ""),
            "built_at": built_at}


def put_feed(kind: str, payload, library_sig: str) -> None:
    if not kind or not isinstance(payload, (dict, list)) or not _db.ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _db.conn()
        conn.execute(
            "INSERT OR REPLACE INTO feeds (kind, payload, library_sig, built_at) "
            "VALUES (?, ?, ?, ?)",
            (str(kind), data, str(library_sig or ""), time.time()))
        conn.execute(
            "DELETE FROM feeds WHERE kind NOT IN "
            "(SELECT kind FROM feeds ORDER BY built_at DESC, kind DESC LIMIT ?)",
            (_MAX_FEEDS,))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"discover cache feed write failed: {e}")
        _db.handle_db_error(e)


# A library that changes over years would otherwise leave a row behind for
# every artist it ever held. Oldest rows go first; they are the ones a rebuild
# would have refetched anyway.
_MAX_ROWS = 20000
_TRIM_EVERY = 500
_puts_since_trim = 0


def _count_put() -> None:
    global _puts_since_trim
    _puts_since_trim += 1
    if _puts_since_trim >= _TRIM_EVERY:
        _puts_since_trim = 0
        _trim()


def _trim() -> None:
    try:
        conn = _db.conn()
        for table in ("lastfm", "resolutions"):
            conn.execute(
                f"DELETE FROM {table} WHERE key NOT IN "
                f"(SELECT key FROM {table} ORDER BY fetched_at DESC LIMIT ?)",
                (_MAX_ROWS,))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"discover cache trim failed: {e}")


def _reset_for_tests() -> None:
    global _puts_since_trim
    _db.reset_for_tests()
    _puts_since_trim = 0
