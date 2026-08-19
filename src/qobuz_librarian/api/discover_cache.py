"""Disk cache for everything the Discover tab fetches.

Three kinds of thing are kept, each with its own retention:

  lastfm       one row per Last.fm answer: an artist's similar list, an
               artist's tags, a page of a tag's albums or artists. Similarity
               barely moves, so these last a fortnight, and they are what makes
               a rebuild after adding one artist cost one request instead of
               six hundred.
  resolutions  the Qobuz artist or album a Last.fm name turned out to be. A
               name Qobuz does not carry is stored as a miss, so an artist
               Qobuz will never have is not searched for again on every build.
  feeds        the assembled, ranked feed, stamped with the signature of the
               library it was built from. A changed library retires it.

SQLite rather than one JSON file because these are hundreds of small keyed
payloads written one at a time while the feed builds, and a JSON file would be
rewritten in full for each one. Like the album cache this is derived data: if
the file is corrupt it is deleted and rebuilt, and losing it costs time, never
correctness.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.logging import vlog

# How long each kind of row stays usable.
SIMILAR_TTL    = 14 * 86400
TAGS_TTL       = 14 * 86400
RESOLUTION_TTL = 30 * 86400
FEED_TTL       = 7 * 86400
# Favourites are the user's own list and change the moment they star something
# in Qobuz, so this one is short. Rebuilding it costs one request.
FAVOURITES_TTL = 300

# Stored in place of a resolution when Qobuz has nothing for that name.
_MISS = {"miss": True}

_init_lock = threading.Lock()
_initialized = False
# Bumped when a corrupt db is discarded, so a thread holding a connection to
# the deleted inode reopens instead of writing into nothing.
_generation = 0
_local = threading.local()


def _db_path() -> Path:
    return Path(str(cfg.DATA_DIR)) / "discover_cache.db"


def _is_corrupt_error(e: sqlite3.Error) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in
               ("malformed", "not a database", "file is encrypted"))


def _discard_corrupt_db() -> bool:
    db = _db_path()
    cleared = False
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        try:
            p.unlink()
            cleared = True
        except FileNotFoundError:
            pass
        except OSError as e:
            vlog(f"couldn't clear corrupt discover cache {p.name}: {e}")
            return False
    if cleared:
        vlog("discover cache was corrupt - rebuilt from scratch")
    return cleared


def _handle_db_error(e: sqlite3.Error) -> None:
    """Recover from a corrupt db noticed by a read or a write. Page corruption
    passes connect and CREATE TABLE and only surfaces on a row access, which
    _ensure never re-checks."""
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
        if _initialized and _discard_corrupt_db():
            _initialized = False
            _generation += 1


def _ensure() -> bool:
    """Create the tables once. False means carry on without a cache."""
    global _initialized
    if _initialized:
        return True
    with _init_lock:
        if _initialized:
            return True
        try:
            _db_path().parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            vlog(f"discover cache dir unavailable ({e}); proceeding without it")
            return False
        for attempt in (1, 2):
            try:
                conn = sqlite3.connect(str(_db_path()), timeout=5)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS lastfm "
                        "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS resolutions "
                        "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS feeds "
                        "(kind TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                        "library_sig TEXT, built_at REAL)")
                    conn.commit()
                finally:
                    conn.close()
                _initialized = True
                return True
            except sqlite3.Error as e:
                if attempt == 1 and _is_corrupt_error(e) and _discard_corrupt_db():
                    continue
                vlog(f"discover cache init failed ({e}); proceeding without it")
                return False
        return False


def _conn() -> sqlite3.Connection:
    """Connection scoped to the calling thread; SQLite connections can't cross
    threads, and the builder writes from its own. synchronous drops to NORMAL
    because a row lost to a crash is refetched, not lost."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) != _generation:
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


# ── Key shapes ────────────────────────────────────────────────────────────────
# Built here so the writer and the reader can't drift apart: a key typo would
# show up as a cache that never hits, which looks like Last.fm being slow.
def similar_key(artist_key: str) -> str:
    return f"similar:{artist_key}"


def tags_key(artist_key: str) -> str:
    return f"toptags:{artist_key}"


def tag_albums_key(tag: str, page: int) -> str:
    return f"tagalbums:{tag}:{page}"


def tag_artists_key(tag: str, page: int) -> str:
    return f"tagartists:{tag}:{page}"


def artist_resolution_key(artist_key: str) -> str:
    return f"artist:{artist_key}"


def album_resolution_key(artist_key: str, title_key: str) -> str:
    return f"album:{artist_key}|{title_key}"


# ── Rows ──────────────────────────────────────────────────────────────────────
def _read(table: str, id_column: str, key: str, ttl_seconds: float,
          allow_stale: bool):
    if not key or not _ensure():
        return None
    try:
        row = _conn().execute(
            f"SELECT payload, fetched_at FROM {table} WHERE {id_column} = ?",
            (str(key),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"discover cache read failed: {e}")
        _handle_db_error(e)
        return None
    if not row:
        return None
    if not allow_stale and (time.time() - (row[1] or 0)) > ttl_seconds:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def _write(table: str, id_column: str, key: str, payload) -> None:
    if not key or not isinstance(payload, (dict, list)) or not _ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _conn()
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({id_column}, payload, fetched_at) "
            "VALUES (?, ?, ?)", (str(key), data, time.time()))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"discover cache write failed: {e}")
        _handle_db_error(e)
        return
    _count_put()


def get_lastfm(key: str, ttl_seconds: float, *, allow_stale: bool = False):
    """A saved Last.fm answer, or None. ``allow_stale`` ignores the age, which
    is how an expired row still fills the page when Last.fm is unreachable."""
    return _read("lastfm", "key", key, ttl_seconds, allow_stale)


def put_lastfm(key: str, payload) -> None:
    _write("lastfm", "key", key, payload)


def get_resolution(key: str, ttl_seconds: float = RESOLUTION_TTL,
                   *, allow_stale: bool = False):
    """What a name resolved to on Qobuz. A cached miss comes back as a miss,
    not as None, so the caller can tell "asked, nothing there" from "never
    asked"."""
    return _read("resolutions", "key", key, ttl_seconds, allow_stale)


def put_resolution(key: str, payload) -> None:
    _write("resolutions", "key", key, payload)


def put_resolution_miss(key: str) -> None:
    """Remember that Qobuz has nothing under this name, so a library full of
    artists Qobuz doesn't carry doesn't re-search for them every build."""
    _write("resolutions", "key", key, dict(_MISS))


def is_miss(payload) -> bool:
    return isinstance(payload, dict) and payload.get("miss") is True


# ── Feeds ─────────────────────────────────────────────────────────────────────
def get_feed(kind: str) -> dict | None:
    """The saved feed and the two facts that decide whether it can still be
    used: which library it was built from, and when.

    Returned whole rather than filtered by age here, because freshness is a
    page-level decision: a feed too old to serve straight is still what gets
    shown, with a notice, when Last.fm can't be reached to rebuild it.
    """
    if not kind or not _ensure():
        return None
    try:
        row = _conn().execute(
            "SELECT payload, library_sig, built_at FROM feeds WHERE kind = ?",
            (str(kind),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"discover cache feed read failed: {e}")
        _handle_db_error(e)
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    return {"payload": payload,
            "library_sig": str(row[1] or ""),
            "built_at": float(row[2] or 0)}


def put_feed(kind: str, payload, library_sig: str) -> None:
    if not kind or not isinstance(payload, (dict, list)) or not _ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO feeds (kind, payload, library_sig, built_at) "
            "VALUES (?, ?, ?, ?)",
            (str(kind), data, str(library_sig or ""), time.time()))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"discover cache feed write failed: {e}")
        _handle_db_error(e)


# ── Housekeeping ──────────────────────────────────────────────────────────────
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
        conn = _conn()
        for table in ("lastfm", "resolutions"):
            conn.execute(
                f"DELETE FROM {table} WHERE key NOT IN "
                f"(SELECT key FROM {table} ORDER BY fetched_at DESC LIMIT ?)",
                (_MAX_ROWS,))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"discover cache trim failed: {e}")


def _reset_for_tests() -> None:
    global _initialized, _generation, _puts_since_trim
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
    _local.generation = None
    _initialized = False
    _generation = 0
    _puts_since_trim = 0
