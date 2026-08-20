"""Persistent cache of parsed FLAC tags, keyed on path + mtime + size.

Every library scan re-parses every audio file with mutagen; on a large library
that's tens of thousands of reads redone each run even when nothing changed.
Caching the parsed tags against the file's mtime (nanoseconds) and size means an
unchanged file costs one ``stat()`` and a SQLite lookup instead of a full parse.
A file edited or replaced changes its mtime or size, invalidating the entry, so
in normal use there are no stale tags. The one gap is a retag that preserves
BOTH mtime and size (e.g. an mtime restored with ``touch -r``), which keeps the
cached tags until the file next changes. Set ``FLAC_CACHE_ENABLED=false`` to
disable; delete the db to force a re-parse.
"""
import atexit
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.logging import vlog

_init_lock = threading.Lock()
_initialized = False
# Bumped when a corrupt db is discarded; _conn() reopens a thread's connection
# when its generation lags, so a sibling libscan worker stops writing into the
# deleted inode after another thread rebuilt the db.
_generation = 0
_local = threading.local()

# Buffered-write state.
_PENDING_LOCK = threading.Lock()
_PENDING_ROWS: dict[str, tuple] = {}  # path → (mtime_ns, size, payload_json)
_PENDING_LIMIT = 500
# Counts committed changes to the table. Anything holding a memoized view of
# these rows (the Library page's census) reads it to tell whether its numbers
# still describe what is stored.
_writes = 0


def _db_path():
    return Path(str(cfg.DATA_DIR)) / "flac_cache.db"


def _is_corrupt_error(e: sqlite3.Error) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in
               ("malformed", "not a database", "file is encrypted"))


def _discard_corrupt_db() -> bool:
    """Delete a malformed cache db (+ WAL sidecars). The cache is derived data -
    losing it just makes the next scan re-parse, which beats a permanently dead
    cache that re-parses every file forever. Returns True if anything cleared."""
    db = _db_path()
    cleared = False
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        try:
            p.unlink()
            cleared = True
        except FileNotFoundError:
            pass
        except OSError as e:
            vlog(f"couldn't clear corrupt flac cache {p.name}: {e}")
            return False
    if cleared:
        vlog("flac cache was corrupt - rebuilt from scratch")
    return cleared


def _handle_db_error(e: sqlite3.Error) -> None:
    """Drop this thread's connection and, on a corrupt-db error, discard the
    malformed file so the next _ensure() rebuilds - the same recovery
    album_cache has. SQLite data-page corruption can pass connect + CREATE TABLE
    and only surface on a row read, which would otherwise leave the cache
    permanently dead (every scan re-parsing every file)."""
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
                "flac cache was corrupt - discarded; it rebuilds on next scan")


def _ensure() -> bool:
    global _initialized
    if not cfg.FLAC_CACHE_ENABLED:
        return False
    if _initialized:
        return True
    with _init_lock:
        if _initialized:
            return True
        try:
            _db_path().parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            vlog(f"flac cache dir unavailable ({e}); proceeding without it")
            return False
        for attempt in (1, 2):
            try:
                conn = sqlite3.connect(str(_db_path()), timeout=5)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS files "
                        "(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, "
                        "payload TEXT NOT NULL)")
                    conn.commit()
                finally:
                    conn.close()
                _initialized = True
                return True
            except sqlite3.Error as e:
                # A corrupt db is the one error we can fix: drop it and retry
                # once. Anything else (locked, full, unwritable) isn't ours.
                if attempt == 1 and _is_corrupt_error(e) and _discard_corrupt_db():
                    continue
                vlog(f"flac cache init failed ({e}); proceeding without it")
                return False
        return False


def _conn():
    """Connection scoped to the calling thread.

    A scan reads tens of thousands of files; opening a fresh connection per
    lookup costs ~20x the lookup it's meant to make cheap, so each thread
    keeps one (SQLite connections can't be shared across threads). synchronous
    is dropped to NORMAL - this is a self-invalidating cache, so a row lost to
    a crash is just re-parsed next scan, and the per-write fsync it avoids is
    otherwise the bulk of a cold scan's caching cost.
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


def signature(path):
    """``(mtime_ns, size)`` for ``path``, or None if it can't be stat'd - the
    key that detects a file changing out from under a stored entry. Capture it
    BEFORE reading a file you intend to ``put()``, so an edit landing between the
    read and the store doesn't pair the file's new mtime with the old tags."""
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
    if not _ensure():
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
        row = _conn().execute(
            "SELECT mtime_ns, size, payload FROM files WHERE path = ?",
            (p,)).fetchone()
    except sqlite3.Error as e:
        vlog(f"flac cache read failed: {e}")
        _handle_db_error(e)
        return None
    if not row or row[0] != mtime_ns or row[1] != size:
        return None
    try:
        payload = json.loads(row[2])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def put(path, payload, sig=None) -> None:
    """Store parsed tags for ``path``. Pass ``sig`` from ``signature(path)``
    captured before the file was read; otherwise a file edited during the parse
    is recorded with its new mtime but the pre-edit tags and served stale until
    it changes again. Falls back to statting now when the caller omits it.

    Writes are buffered and flushed in batches (see ``flush_pending``) so a
    cold library scan doesn't commit once per file. ``get()`` reads the
    buffer before falling back to disk, so a `put()` immediately followed by
    `get()` on the same path is still a hit - the put→get visibility scans
    depend on is preserved across the buffering boundary.
    """
    if not isinstance(payload, dict) or not _ensure():
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
    if not _ensure():
        return
    with _PENDING_LOCK:
        if not _PENDING_ROWS:
            return
        snapshot = dict(_PENDING_ROWS)
        rows = [(p, m, s, d) for p, (m, s, d) in snapshot.items()]
    try:
        conn = _conn()
        conn.executemany(
            "INSERT OR REPLACE INTO files (path, mtime_ns, size, payload) "
            "VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"flac cache batch write failed: {e}")
        _handle_db_error(e)
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
    if not _ensure() or not cfg.MUSIC_ROOT.exists():
        return 0
    stamp = Path(str(cfg.DATA_DIR)) / ".flac_cache_prune"
    if not force and stamp.exists():
        try:
            if (time.time() - stamp.stat().st_mtime) < 86400:
                return 0
        except OSError:
            pass
    # See buffered rows: a prune right after a partial scan should still drop
    # paths whose files are now gone, even if the put() for them hasn't been
    # flushed yet.
    flush_pending()
    try:
        conn = _conn()
        gone = [(p,) for (p,) in conn.execute("SELECT path FROM files")
                if not os.path.exists(p)]
        if gone:
            conn.executemany("DELETE FROM files WHERE path = ?", gone)
            conn.commit()
            global _writes
            _writes += 1
    except sqlite3.Error as e:
        vlog(f"flac cache prune failed: {e}")
        _handle_db_error(e)
        return 0
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
    return len(gone)


def store_stamp():
    """A value that changes whenever the stored rows may have.

    A caller memoizing a view of these rows compares this to know its numbers
    are still the stored ones. This process's own write count is not enough on
    its own: a terminal run writes the same file from another process, and
    SQLite's data_version is what moves when it does.
    """
    version = 0
    if _ensure():
        try:
            row = _conn().execute("PRAGMA data_version").fetchone()
            version = row[0] if row else 0
        except sqlite3.Error as e:
            vlog(f"flac cache stamp read failed: {e}")
            _handle_db_error(e)
    return (_writes, version)


def census():
    """Aggregate the cached tag rows under MUSIC_ROOT into a quality census:
    per-tier track counts and bytes, hi-res bytes per artist, and a rough
    downsample-reclaim figure. Reads only rows the scanner already stored,
    so it costs one table
    walk and no file I/O; rows from before the cache carried sizes count
    toward their tier but not the byte totals. Returns None when the cache is
    off or holds nothing."""
    if not _ensure():
        return None
    flush_pending()
    tiers = {"cd": [0, 0], "hires96": [0, 0], "hires192": [0, 0],
             "unknown": [0, 0]}
    artists: dict = {}
    reclaim = 0
    music_root = str(cfg.MUSIC_ROOT).rstrip("/") + "/"
    try:
        rows = _conn().execute("SELECT path, payload FROM files").fetchall()
    except sqlite3.Error as e:
        vlog(f"flac cache census read failed: {e}")
        _handle_db_error(e)
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
    global _initialized, _generation, _writes
    _writes = 0
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
    _local.generation = None
    _initialized = False
    _generation = 0
    with _PENDING_LOCK:
        _PENDING_ROWS.clear()
