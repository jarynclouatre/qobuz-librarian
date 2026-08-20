"""On-disk cache of Qobuz ISRC→track lookups - the one network cost of a repair scan.

A repair scan resolves every track to its exact Qobuz recording by ISRC to
compare durations, and that lookup (one search per track) is both the slow part
and the only part that touches the network. An ISRC names a recording for good,
so the result is safe to remember: a re-scan, and any album that shares the same
ISRC, skips the lookup. The file itself is still decode-tested fresh on every
scan, so corruption that turns up on disk later is always caught - only the
network round trip is cached, never a verdict about a file.

An entry is reused while it is younger than ``REPAIR_CACHE_TTL_DAYS`` so a
remembered track still re-checks against Qobuz that often, in case its catalogue
entry changed; a TTL of 0 keeps entries until the db is deleted. Only a positive
hit is stored - a lookup that found nothing (a transient outage, a delisted
track) is never cached, so a hiccup can't freeze a "no match" in place. Set
``REPAIR_CACHE_ENABLED=false`` to disable; delete the db to drop everything.
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
_generation = 0
_local = threading.local()


def _db_path():
    return Path(str(cfg.DATA_DIR)) / "repair_cache.db"


def _is_corrupt_error(e: sqlite3.Error) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in
               ("malformed", "not a database", "file is encrypted"))


def _discard_corrupt_db() -> bool:
    """Delete a malformed cache db (+ WAL sidecars). The cache is derived data -
    losing it just makes the next scan look tracks up again, which beats a
    permanently dead cache. Returns True if anything was cleared."""
    db = _db_path()
    cleared = False
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        try:
            p.unlink()
            cleared = True
        except FileNotFoundError:
            pass
        except OSError as e:
            vlog(f"couldn't clear corrupt repair cache {p.name}: {e}")
            return False
    if cleared:
        vlog("repair cache was corrupt - rebuilt from scratch")
    return cleared


def _handle_db_error(e: sqlite3.Error) -> None:
    """Drop this thread's connection and, on a corrupt-db error, discard the
    malformed file and bump the generation so the other scan workers reopen
    against the rebuilt db rather than keep writing into the deleted inode."""
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
            import logging
            logging.getLogger("qobuz_librarian").info(
                "repair cache was corrupt - discarded; it rebuilds on next scan")


def _ensure() -> bool:
    global _initialized
    if not cfg.REPAIR_CACHE_ENABLED:
        return False
    if _initialized:
        return True
    with _init_lock:
        if _initialized:
            return True
        try:
            _db_path().parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            vlog(f"repair cache dir unavailable ({e}); proceeding without it")
            return False
        for attempt in (1, 2):
            try:
                conn = sqlite3.connect(str(_db_path()), timeout=5)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS tracks "
                        "(isrc TEXT PRIMARY KEY, stored_at INTEGER NOT NULL, "
                        "payload TEXT NOT NULL)")
                    conn.commit()
                finally:
                    conn.close()
                _initialized = True
                return True
            except sqlite3.Error as e:
                if attempt == 1 and _is_corrupt_error(e) and _discard_corrupt_db():
                    continue
                vlog(f"repair cache init failed ({e}); proceeding without it")
                return False
        return False


def _conn():
    """Connection scoped to the calling thread (SQLite connections can't be
    shared across the scan's worker threads). Reopened when a corrupt-db recovery
    on another thread has bumped the generation, so a worker mid-scan stops
    writing into the discarded file. synchronous=NORMAL - a row lost to a crash
    just costs one fresh lookup next run."""
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


def get_track(isrc) -> dict | None:
    """The cached Qobuz track for ``isrc`` if one was stored within
    REPAIR_CACHE_TTL_DAYS, else None so the caller does a live lookup."""
    if not isrc or not _ensure():
        return None
    try:
        row = _conn().execute(
            "SELECT stored_at, payload FROM tracks WHERE isrc = ?",
            (isrc,)).fetchone()
    except sqlite3.Error as e:
        vlog(f"repair cache read failed: {e}")
        _handle_db_error(e)
        return None
    if not row:
        return None
    try:
        stored_at = float(row[0])
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(stored_at) or stored_at < 0:
        return None
    ttl = float(cfg.REPAIR_CACHE_TTL_DAYS) * 86400
    if ttl > 0 and (time.time() - stored_at) > ttl:
        return None
    try:
        track = json.loads(row[1])
    except (ValueError, TypeError):
        return None
    return track if _track_matches_isrc(isrc, track) else None


def _normalise_isrc(value) -> str:
    return str(value or "").replace("-", "").upper().strip()


def _track_matches_isrc(isrc, track) -> bool:
    if not isinstance(track, dict):
        return False
    track_id = track.get("id")
    if (
        isinstance(track_id, bool)
        or not isinstance(track_id, (str, int))
        or not str(track_id).strip()
    ):
        return False
    expected = _normalise_isrc(isrc)
    return bool(expected and _normalise_isrc(track.get("isrc")) == expected)


def put_track(isrc, track) -> None:
    """Remember a positive ISRC→track lookup. A None/empty result is never stored
    so a transient miss can't later be served as a stable 'no match'."""
    if not _track_matches_isrc(isrc, track) or not _ensure():
        return
    try:
        data = json.dumps(track)
    except (TypeError, ValueError):
        return
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO tracks (isrc, stored_at, payload) "
            "VALUES (?, ?, ?)", (isrc, int(time.time()), data))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"repair cache write failed: {e}")
        _handle_db_error(e)


def prune_expired(force: bool = False) -> int:
    """Drop entries past the TTL so the db stays proportional to the library.

    Throttled to once a day - a CLI session that opens and closes repeatedly
    shouldn't re-walk the table each time. A TTL of 0 (keep forever) prunes
    nothing. Returns the number removed.
    """
    if not _ensure():
        return 0
    ttl = float(cfg.REPAIR_CACHE_TTL_DAYS) * 86400
    if ttl <= 0:
        return 0
    stamp = Path(str(cfg.DATA_DIR)) / ".repair_cache_prune"
    if not force and stamp.exists():
        try:
            if (time.time() - stamp.stat().st_mtime) < 86400:
                return 0
        except OSError:
            pass
    cutoff = int(time.time() - ttl)
    try:
        conn = _conn()
        cur = conn.execute("DELETE FROM tracks WHERE stored_at < ?", (cutoff,))
        conn.commit()
        removed = cur.rowcount or 0
    except sqlite3.Error as e:
        vlog(f"repair cache prune failed: {e}")
        _handle_db_error(e)
        return 0
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
    return removed


def _reset_for_tests() -> None:
    global _initialized, _generation
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None
    _local.generation = None
    _initialized = False
    _generation = 0
