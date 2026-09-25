"""Persistent cache of parsed FLAC tags, keyed on path + mtime + size.

Every library scan re-parses every audio file with mutagen; on a large library
that's tens of thousands of reads redone each run even when nothing changed.
Caching the parsed tags against the file's mtime (nanoseconds) and size means an
unchanged file costs one ``stat()`` and a SQLite lookup instead of a full parse.
A file edited or replaced changes its mtime or size, invalidating the entry, so
in normal use there are no stale tags. The one gap is a retag that preserves
BOTH mtime and size (e.g. an mtime restored with ``touch -r``), which keeps the
cached tags until the file next changes. Delete the db to force a re-parse.
"""
import atexit
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.cache_db import CacheDB
from qobuz_librarian.ui_cli.logging import vlog

_db = CacheDB(
    "flac_cache.db", "flac cache", (
        "CREATE TABLE IF NOT EXISTS files "
        "(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, "
        "payload TEXT NOT NULL)",
    ),
    enabled=lambda: cfg.FLAC_CACHE_ENABLED,
)

# Buffered-write state.
_PENDING_LOCK = threading.Lock()
_PENDING_ROWS: dict[str, tuple] = {}  # path → (mtime_ns, size, payload_json)
_PENDING_LIMIT = 500
# Counts committed changes to the table. Anything holding a memoized view of
# these rows (the Library page's census) reads it to tell whether its numbers
# still describe what is stored.
_writes = 0


def signature(path):
    """``(mtime_ns, size)`` for ``path``, or None if it can't be stat'd - the
    key that detects a file changing out from under a stored entry."""
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def get(path) -> dict | None:
    """Cached tags for ``path`` if the file is unchanged since they were stored.

    Checks the in-memory write buffer first: a ``put()`` between two scan
    passes hasn't been flushed yet, but the second pass shouldn't have to
    re-parse the file just because the row is still in RAM.
    """
    if not _db.ensure():
        return None
    sig = signature(path)
    if sig is None:
        return None
    mtime_ns, size = sig
    p = str(path)
    with _PENDING_LOCK:
        buffered = _PENDING_ROWS.get(p)
    if buffered is not None:
        b_mtime, b_size, b_payload = buffered
        if b_mtime == mtime_ns and b_size == size:
            try:
                payload = json.loads(b_payload)
            except (ValueError, TypeError):
                return None
            return payload if isinstance(payload, dict) else None
        return None
    try:
        row = _db.conn().execute(
            "SELECT mtime_ns, size, payload FROM files WHERE path = ?",
            (p,)).fetchone()
    except sqlite3.Error as e:
        vlog(f"flac cache read failed: {e}")
        _db.handle_db_error(e)
        return None
    if not row or row[0] != mtime_ns or row[1] != size:
        return None
    try:
        payload = json.loads(row[2])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def put(path, payload, sig=None) -> None:
    """Store parsed tags for ``path``."""
    if not isinstance(payload, dict) or not _db.ensure():
        return
    if sig is None:
        sig = signature(path)
    if sig is None:
        return
    mtime_ns, size = sig
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    with _PENDING_LOCK:
        _PENDING_ROWS[str(path)] = (mtime_ns, size, data)
        full = len(_PENDING_ROWS) >= _PENDING_LIMIT
    if full:
        flush_pending()


def flush_pending() -> None:
    """Drain any buffered ``put`` writes in one ``executemany`` transaction.

    Called automatically when the buffer hits ``_PENDING_LIMIT``, by
    ``scanner.clear_scan_caches`` at scan-end, and at process exit via
    ``atexit``. A crash mid-scan loses at most ``_PENDING_LIMIT`` entries -
    the next scan re-parses them (no data loss, just rework). Idempotent;
    a no-op when the buffer is empty.
    """
    if not _db.ensure():
        return
    with _PENDING_LOCK:
        if not _PENDING_ROWS:
            return
        snapshot = dict(_PENDING_ROWS)
        rows = [(p, m, s, d) for p, (m, s, d) in snapshot.items()]
    try:
        conn = _db.conn()
        conn.executemany(
            "INSERT OR REPLACE INTO files (path, mtime_ns, size, payload) "
            "VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"flac cache batch write failed: {e}")
        _db.handle_db_error(e)
        return  # keep the buffered rows so the next flush retries them
    global _writes
    _writes += 1
    # Commit succeeded: drop exactly the rows we wrote, preserving any a
    # concurrent put() has replaced in the meantime.
    with _PENDING_LOCK:
        for p, val in snapshot.items():
            if _PENDING_ROWS.get(p) == val:
                del _PENDING_ROWS[p]


# Flush whatever's still in the buffer when the process exits cleanly - a CLI
# run that ends without a clear_scan_caches call (a quick --search, a forced
# exit between scan phases) would otherwise drop its last partial batch.
atexit.register(flush_pending)


def prune_missing(force: bool = False) -> int:
    """Drop rows whose file is gone, keeping the db proportional to the library.

    Keying on absolute path means every upgrade-replace, move, or consolidation
    leaves the old path's row orphaned, so the table would otherwise grow
    without bound. Throttled to once a day - a CLI session that opens and closes
    repeatedly shouldn't re-walk the whole table each time - and skipped when
    MUSIC_ROOT is absent so an unmounted library volume can't wipe the cache.
    """
    if not _db.ensure() or not cfg.MUSIC_ROOT.exists():
        return 0
    stamp = Path(str(cfg.DATA_DIR)) / ".flac_cache_prune"
    if not force and stamp.exists():
        try:
            if 0 <= (time.time() - stamp.stat().st_mtime) < 86400:
                return 0
        except OSError:
            pass
    # See buffered rows: a prune right after a partial scan should still drop
    # paths whose files are now gone, even if the put() for them hasn't been
    # flushed yet.
    flush_pending()
    try:
        conn = _db.conn()
        gone = [(p,) for (p,) in conn.execute("SELECT path FROM files")
                if not os.path.exists(p)]
        if gone:
            conn.executemany("DELETE FROM files WHERE path = ?", gone)
            conn.commit()
            global _writes
            _writes += 1
    except sqlite3.Error as e:
        vlog(f"flac cache prune failed: {e}")
        _db.handle_db_error(e)
        return 0
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
    return len(gone)


def store_stamp():
    """A value that changes whenever the stored rows may have."""
    version = 0
    if _db.ensure():
        try:
            row = _db.conn().execute("PRAGMA data_version").fetchone()
            version = row[0] if row else 0
        except sqlite3.Error as e:
            vlog(f"flac cache stamp read failed: {e}")
            _db.handle_db_error(e)
    return (_writes, version)


def census():
    """Aggregate the cached tag rows under MUSIC_ROOT into a quality census:
    per-tier track counts and bytes, hi-res bytes per artist, and a rough
    downsample-reclaim figure. Reads only rows the scanner already stored,
    so it costs one table
    walk and no file I/O; rows from before the cache carried sizes count
    toward their tier but not the byte totals. Returns None when the cache is
    off or holds nothing."""
    if not _db.ensure():
        return None
    flush_pending()
    tiers = {"cd": [0, 0], "hires96": [0, 0], "hires192": [0, 0],
             "unknown": [0, 0]}
    artists: dict = {}
    reclaim = 0
    music_root = str(cfg.MUSIC_ROOT).rstrip("/") + "/"
    try:
        rows = _db.conn().execute("SELECT path, payload FROM files").fetchall()
    except sqlite3.Error as e:
        vlog(f"flac cache census read failed: {e}")
        _db.handle_db_error(e)
        return None
    def nonnegative_int(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(parsed, 0)

    for path, payload in rows:
        # Rows land for every file whose tags were read, which includes staging
        # runs and upgrade backups. Only the library counts as what's on disk;
        # without this a download or an upgrade inflates the census by its own
        # working copies.
        if not isinstance(path, str) or not path.startswith(music_root):
            continue
        try:
            meta = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(meta, dict) or meta.get("__neg__"):
            continue
        bits = nonnegative_int(meta.get("bits"))
        sr = nonnegative_int(meta.get("sample_rate"))
        size = nonnegative_int(meta.get("size"))
        if not bits or not sr:
            tier = "unknown"
        elif bits <= 16:
            tier = "cd"
        elif sr <= 96000:
            tier = "hires96"
        else:
            tier = "hires192"
        tiers[tier][0] += 1
        tiers[tier][1] += size
        if tier in ("hires96", "hires192"):
            # The integer-ratio family the resampler targets: 88.2/176.4 land
            # on 44.1, everything else on 48.
            target = 44100 if sr % 44100 == 0 else 48000
            if sr > target:
                reclaim += int(size * (1 - target / sr))
            artist = path[len(music_root):].split("/", 1)[0]
            if artist:
                artists[artist] = artists.get(artist, 0) + size
    total_n = sum(v[0] for v in tiers.values())
    if not total_n:
        return None
    return {
        "tiers": tiers,
        "total_tracks": total_n,
        "total_bytes": sum(v[1] for v in tiers.values()),
        "top_hires_artists": sorted(artists.items(), key=lambda kv: -kv[1])[:5],
        "reclaim_bytes": reclaim,
    }


def _reset_for_tests() -> None:
    global _writes
    _writes = 0
    _db.reset_for_tests()
    with _PENDING_LOCK:
        _PENDING_ROWS.clear()
