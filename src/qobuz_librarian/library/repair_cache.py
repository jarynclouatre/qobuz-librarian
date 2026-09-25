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
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.cache_db import CacheDB
from qobuz_librarian.ui_cli.logging import vlog

_db = CacheDB(
    "repair_cache.db", "repair cache", (
        "CREATE TABLE IF NOT EXISTS tracks "
        "(isrc TEXT PRIMARY KEY, stored_at INTEGER NOT NULL, "
        "payload TEXT NOT NULL)",
    ),
    enabled=lambda: cfg.REPAIR_CACHE_ENABLED,
)


def get_track(isrc) -> dict | None:
    """The cached Qobuz track for ``isrc`` if one was stored within
    REPAIR_CACHE_TTL_DAYS, else None so the caller does a live lookup."""
    if not isrc or not _db.ensure():
        return None
    try:
        row = _db.conn().execute(
            "SELECT stored_at, payload FROM tracks WHERE isrc = ?",
            (isrc,)).fetchone()
    except sqlite3.Error as e:
        vlog(f"repair cache read failed: {e}")
        _db.handle_db_error(e)
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
    if not _track_matches_isrc(isrc, track) or not _db.ensure():
        return
    try:
        data = json.dumps(track)
    except (TypeError, ValueError):
        return
    try:
        conn = _db.conn()
        conn.execute(
            "INSERT OR REPLACE INTO tracks (isrc, stored_at, payload) "
            "VALUES (?, ?, ?)", (isrc, int(time.time()), data))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"repair cache write failed: {e}")
        _db.handle_db_error(e)


def prune_expired(force: bool = False) -> int:
    """Drop entries past the TTL so the db stays proportional to the library.

    Throttled to once a day - a CLI session that opens and closes repeatedly
    shouldn't re-walk the table each time. A TTL of 0 (keep forever) prunes
    nothing. Returns the number removed.
    """
    if not _db.ensure():
        return 0
    ttl = float(cfg.REPAIR_CACHE_TTL_DAYS) * 86400
    if ttl <= 0:
        return 0
    stamp = Path(str(cfg.DATA_DIR)) / ".repair_cache_prune"
    if not force and stamp.exists():
        try:
            if 0 <= (time.time() - stamp.stat().st_mtime) < 86400:
                return 0
        except OSError:
            pass
    cutoff = int(time.time() - ttl)
    try:
        conn = _db.conn()
        cur = conn.execute("DELETE FROM tracks WHERE stored_at < ?", (cutoff,))
        conn.commit()
        removed = cur.rowcount or 0
    except sqlite3.Error as e:
        vlog(f"repair cache prune failed: {e}")
        _db.handle_db_error(e)
        return 0
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
    return removed


def _reset_for_tests() -> None:
    _db.reset_for_tests()
