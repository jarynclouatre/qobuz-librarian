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
    with _PENDING_LOCK:
        if not _PENDING_ROWS:
            return
        snapshot = dict(_PENDING_ROWS)
        rows = [(p, m, s, d) for p, (m, s, d) in snapshot.items()]
    if not _db.ensure():
        return
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


def _gone(path) -> bool:
    """Only a path that no longer exists counts as gone: a file under a
    folder that can't be read right now keeps its row."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _library_folders():
    """The names at the top of MUSIC_ROOT other than NAS and Windows system
    folders, or None when it can't be listed."""
    from qobuz_librarian.library import scanner
    try:
        names = os.listdir(cfg.MUSIC_ROOT)
    except OSError:
        return None
    return set(names) - scanner.SYSTEM_FOLDER_NAMES


def prune_missing(force: bool = False) -> int:
    """Drop rows whose file is gone, keeping the db proportional to the library.

    Keying on absolute path means every upgrade-replace, move, or consolidation
    leaves the old path's row orphaned, so the table would otherwise grow
    without bound. Throttled to once a day - a CLI session that opens and closes
    repeatedly shouldn't re-walk the whole table each time - and skipped when
    MUSIC_ROOT is absent so an unmounted library volume can't wipe the cache.

    A row under MUSIC_ROOT whose artist folder isn't one the root lists goes
    too: one in a recycle bin, or one read through another case of the
    folder's name on a case-insensitive disk, which the census would count a
    second time.
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
    root = str(cfg.MUSIC_ROOT).rstrip("/") + "/"
    listed = _library_folders()

    def stale(path):
        unlisted = (listed is not None and path.startswith(root)
                    and path[len(root):].split("/", 1)[0] not in listed)
        return unlisted or _gone(path)

    try:
        conn = _db.conn()
        gone = [(p,) for (p,) in conn.execute("SELECT path FROM files")
                if stale(p)]
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


# One row per cached file under MUSIC_ROOT: its tier, split at 48 kHz (the
# rate Downsample works above), and the bytes a downsample would free. The
# resampler targets the integer-ratio family: 88.2/176.4 land on 44.1,
# everything else on 48. SQLite does the walk, so a page asking for the
# census is not held up by tens of thousands of rows coming back one at a
# time.
_CENSUS_ROWS = """
WITH docs AS (
    SELECT rowid AS seq, substr(path, length(:root) + 1) AS rest,
           CASE WHEN json_valid(payload) THEN payload END AS doc
    FROM files
    WHERE substr(path, 1, length(:root)) = :root
), meta AS (
    SELECT seq, rest,
           max(coalesce(CAST(json_extract(doc, '$.bits') AS INTEGER), 0), 0) AS bits,
           max(coalesce(CAST(json_extract(doc, '$.sample_rate') AS INTEGER), 0), 0) AS sr,
           max(coalesce(CAST(json_extract(doc, '$.size') AS INTEGER), 0), 0) AS size
    FROM docs
    WHERE json_type(doc) = 'object'
      AND NOT coalesce(json_extract(doc, '$.__neg__'), 0)
), tiered AS (
    SELECT seq, rest, sr, size,
           CASE WHEN bits = 0 OR sr = 0 THEN 'unknown'
                WHEN sr > 96000 THEN 'hires192'
                WHEN sr > 48000 THEN 'hires96'
                WHEN bits <= 16 THEN 'cd'
                ELSE 'hires48' END AS tier,
           CASE WHEN sr % 44100 = 0 THEN 44100 ELSE 48000 END AS target
    FROM meta
), rows AS (
    SELECT seq, rest, size, tier,
           CASE WHEN tier IN ('hires48', 'hires96', 'hires192') AND sr > target
                THEN CAST(size * (1 - CAST(target AS REAL) / sr) AS INTEGER)
                ELSE 0 END AS reclaim
    FROM tiered
)
"""


def census():
    """Aggregate the cached tag rows under MUSIC_ROOT into a quality census:
    per-tier track counts and bytes, hi-res bytes per artist, and a rough
    downsample-reclaim figure. Reads only rows the scanner already stored,
    so it costs two table walks inside SQLite and no file I/O; rows from
    before the cache carried sizes count toward their tier but not the byte
    totals. Returns None when the cache is off or holds nothing."""
    if not _db.ensure():
        return None
    flush_pending()
    tiers = {"cd": [0, 0], "hires48": [0, 0], "hires96": [0, 0],
             "hires192": [0, 0], "unknown": [0, 0]}
    # Rows land for every file whose tags were read, which includes staging
    # runs and upgrade backups. Only the library counts as what's on disk;
    # without this a download or an upgrade inflates the census by its own
    # working copies.
    params = {"root": str(cfg.MUSIC_ROOT).rstrip("/") + "/"}
    try:
        conn = _db.conn()
        totals = conn.execute(
            _CENSUS_ROWS + "SELECT tier, count(*), sum(size), sum(reclaim) "
            "FROM rows GROUP BY tier", params).fetchall()
        top = conn.execute(
            _CENSUS_ROWS + "SELECT CASE WHEN instr(rest, '/') "
            "THEN substr(rest, 1, instr(rest, '/') - 1) ELSE rest END AS artist, "
            "sum(size) AS bytes, min(seq) AS first FROM rows "
            "WHERE tier IN ('hires48', 'hires96', 'hires192') "
            "GROUP BY artist HAVING artist != '' "
            "ORDER BY bytes DESC, first LIMIT 5",
            params).fetchall()
    except sqlite3.Error as e:
        vlog(f"flac cache census read failed: {e}")
        _db.handle_db_error(e)
        return None
    reclaim = 0
    for tier, n, size, freed in totals:
        tiers[tier] = [n, size]
        reclaim += freed
    total_n = sum(v[0] for v in tiers.values())
    if not total_n:
        return None
    return {
        "tiers": tiers,
        "total_tracks": total_n,
        "total_bytes": sum(v[1] for v in tiers.values()),
        "top_hires_artists": [(artist, size) for artist, size, _first in top],
        "reclaim_bytes": reclaim,
    }


def _reset_for_tests() -> None:
    global _writes
    _writes = 0
    _db.reset_for_tests()
    with _PENDING_LOCK:
        _PENDING_ROWS.clear()
