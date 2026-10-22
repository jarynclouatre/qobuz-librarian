"""Write-through SQLite persistence for the job registry.

Without this, the registry + work queues in ``jobs.py`` are in-memory only:
a container restart (compose update, OOM, host reboot) silently drops every
queued/running download (orphaning staging) and throws away a completed
scan's AWAITING_REVIEW candidates, losing minutes of API work, and the user
re-scans from artist #1.

With this module:

* Job state is mirrored into ``DATA_DIR/jobs.db`` on every meaningful
  transition (add, RUNNING start, AWAITING_REVIEW, approve, terminal).
* On startup, ``restore`` reloads the rows into the registry:
  - DONE / FAILED / CANCELED come back as historical entries browsable
    in the History view (see ``history_page`` / ``history_count``).
  - AWAITING_REVIEW comes back with candidates intact so the user can
    still approve. The execute function is resolved from ``execute_kind``
    via a lookup table the caller provides; closures aren't serialisable.
  - PENDING / RUNNING from the prior session are marked
    FAILED("interrupted on restart; submit again") so the user sees
    them rather than them silently vanishing.

Terminal log lines are persisted with the finished job. Live progress is not:
it changes too often and cannot be resumed after a process restart.

If the SQLite file can't be opened (read-only volume, disk full), ordinary
history updates degrade to a no-op.  Job admission is stricter: work that can
change the library is not allowed to enter a worker unless its owner row is
durable first.
"""
import bisect
import copy
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from array import array
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Optional

from qobuz_librarian import config as cfg
from qobuz_librarian import download_result, redaction
from qobuz_librarian.completion import RecoveryOwner, normalise_album_id
from qobuz_librarian.library import candidate_premise

_log = logging.getLogger("qobuz_librarian")
_lock = threading.Lock()
_disabled = False
_conn: Optional[sqlite3.Connection] = None
_schema_ready = False
_admission_ready = False
_previous_write_at: Optional[float] = None
# jobs.db itself is not a readable database, as opposed to a folder that
# can't be written.
_damaged = False
# The db opened fine but a write later failed (typically a full disk).
_warned_write_failure = False

FINISHED_HISTORY_KEEP = 1000

# An approved run of these kinds holds its picks until it starts, so a cancel
# or a restart before then puts them back into the review.
REVIEW_PICK_KINDS = ("library", "new_releases", "upgrade", "downsample",
                     "repair", "collection_restore")
_REVIEW_PICK_MARKS = ",".join("?" for _ in REVIEW_PICK_KINDS)


@dataclass(frozen=True)
class RecoveryResolutionPlan:
    """Exact jobs.db snapshots to retire after one proved manual restore."""

    location: str
    receipt: dict | None
    rows: tuple[tuple[str, str], ...]


_RECOVERY_FIELDS = {
    "version", "kind", "status", "location", "album_dir", "stage",
    "reason", "complete", "requested", "backed_up", "receipt",
}


def _valid_recovery(record) -> bool:
    return (
        type(record) is dict
        and set(record) == _RECOVERY_FIELDS
        and record.get("version") == 1
        and record.get("kind") in ("repair-backup", "migration")
        and record.get("status") == "retained"
        and isinstance(record.get("location"), str)
        and bool(record["location"])
        and isinstance(record.get("album_dir"), str)
        and bool(record["album_dir"])
        and isinstance(record.get("stage"), str)
        and isinstance(record.get("reason"), str)
        and type(record.get("complete")) is bool
        and type(record.get("requested")) is int
        and type(record.get("backed_up")) is int
        and 0 <= record["backed_up"] <= record["requested"]
        and (record.get("receipt") is None
             or type(record.get("receipt")) is dict)
    )


def _decode_recoveries(value) -> tuple[list[dict], bool]:
    """Return validated recovery records and whether the payload is unreadable."""
    try:
        recoveries = json.loads(value or "[]")
    except (TypeError, ValueError):
        return [], True
    if (
        type(recoveries) is not list
        or any(not _valid_recovery(record) for record in recoveries)
    ):
        return [], True
    return recoveries, False


def _decode_execute_args(value) -> tuple[dict, bool]:
    """Return saved retry details and whether they are unreadable."""
    try:
        args = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}, True
    if type(args) is not dict:
        return {}, True
    return args, False


def _decode_object(value) -> tuple[dict, bool]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}, True
    if type(decoded) is not dict:
        return {}, True
    return decoded, False


def _decode_single(value) -> tuple[dict, bool]:
    single, unreadable = _decode_object(value)
    if unreadable:
        return {}, True
    text_fields = (
        "album_id", "track_id", "dir", "isrc", "title", "artist",
        "album", "owned_root",
    )
    if (
        any(
            key in single
            and single[key] is not None
            and type(single[key]) is not str
            for key in text_fields
        )
        or (
            "owned_path" in single
            and single["owned_path"] is not None
            and type(single["owned_path"]) is not dict
        )
        or (
            "catalog_cleanup" in single
            and single["catalog_cleanup"] is not None
            and type(single["catalog_cleanup"]) is not dict
        )
        or any(
            key in single and type(single[key]) is not bool
            for key in ("marked", "removed")
        )
    ):
        return {}, True
    return single, False


def _artist_of(row):
    payload = row.get("payload") if isinstance(row, dict) else None
    key = payload.get("_artist_dir") if isinstance(payload, dict) else None
    return key if isinstance(key, str) and key else None


def _shared_premises(rows):
    """Keep one copy of an artist's receipt instead of one on every row.

    A row's receipt seals its whole artist folder, and every missing album
    under one artist carries the same one. Repeating it per row put a large
    saved review beyond what the container can read back.
    """
    premises = {}
    for row in rows:
        key = _artist_of(row)
        value = (row.get("payload") or {}).get("_premise") if key else None
        if (key and key not in premises and isinstance(value, dict)
                and value.get("kind") == "missing"):
            premises[key] = value
    return premises


def _expand_candidates(value):
    """Put each row's receipt back. Reviews saved as a bare list still load."""
    if not isinstance(value, dict) or value.get("version") != 2:
        return value
    premises = value.get("premises")
    rows = value.get("rows")
    if not isinstance(premises, dict) or not isinstance(rows, list):
        return None
    return [candidate_premise.restore_candidate(
                row, premises.get(_artist_of(row)))
            if isinstance(row, dict) else row
            for row in rows]


# Statuses whose review rows are read back. Other jobs keep none on disk.
_ROW_STATUSES = ("awaiting_review", "pending", "running")
_NO_ROWS = ((), {})


def _decode_candidates(value, status, stored=_NO_ROWS) -> tuple[list[dict], bool]:
    """A saved review's rows, from the jobs row's own column when an older
    build left them there, otherwise from ``stored``: the rows and premises
    read from their tables, or None when they could not be parsed."""
    if status not in _ROW_STATUSES:
        return [], False
    try:
        if (value or "[]") != "[]":
            candidates = _expand_candidates(json.loads(value))
        elif stored is None:
            return [], True
        else:
            candidates = _expand_candidates(
                {"version": 2, "premises": stored[1], "rows": list(stored[0])})
    except (TypeError, ValueError):
        return [], True
    if type(candidates) is not list:
        return [], True
    for candidate in candidates:
        if type(candidate) is not dict:
            return [], True
        cid = candidate.get("cid")
        seq = candidate.get("seq")
        payload = candidate.get("payload")
        selected = candidate.get("selected")
        if (
            not isinstance(cid, str)
            or not cid
            or (seq is not None and type(seq) is not int)
            or (payload is not None and type(payload) is not dict)
            or (
                "selected" in candidate
                and type(selected) is not bool
            )
            or any(
                value is not None and type(value) is not str
                for value in (
                    candidate.get("kind"),
                    candidate.get("title"),
                    candidate.get("artist"),
                    candidate.get("detail"),
                )
            )
        ):
            return [], True
    return candidates, False


_UNREADABLE_LOG_LINE = "[Saved activity log could not be read.]"


def decode_log_lines(value) -> tuple[list[str], bool]:
    try:
        lines = json.loads(value or "[]")
    except (TypeError, ValueError):
        return [_UNREADABLE_LOG_LINE], True
    if type(lines) is not list or any(type(line) is not str for line in lines):
        return [_UNREADABLE_LOG_LINE], True
    return lines, False


# A download that imported with tracks missing was saved as failed before
# tracks_missing was recorded; its message still opens with the count. A
# message about lossy-only tracks is left out: a download that imported
# nothing still opens with that one.
_SAVED_MISSING_RE = re.compile(r"(\d+) tracks? (?:is|are) still missing")


def _tracks_missing(value, status, error) -> int:
    if type(value) is int and value > 0:
        return value
    match = (
        _SAVED_MISSING_RE.match(error)
        if status == "failed" and isinstance(error, str) else None
    )
    return int(match.group(1)) if match else 0


def _decode_timestamp(value) -> float | None:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value < 32_503_680_000
    ):
        return None
    return float(value)


def _note_write_failure(what: str, e: Exception) -> None:
    global _warned_write_failure
    if not _warned_write_failure:
        _warned_write_failure = True
        _log.info("job persistence write failed (%s); jobs may not survive a "
                  "restart until the volume recovers: %s", what, e)
    else:
        _log.debug("%s failed: %s", what, e)


def _rollback_failed_write(conn) -> None:
    """End a rejected write so a later commit cannot publish it by accident.

    Callers hold ``_lock``. If SQLite cannot roll the transaction back, drop
    the poisoned connection; closing it is the final rollback boundary and a
    later operation may reopen the database cleanly.
    """
    global _conn
    try:
        conn.rollback()
        return
    except sqlite3.Error:
        pass
    try:
        conn.close()
    except sqlite3.Error:
        pass
    if _conn is conn:
        _conn = None


def _path():
    return cfg.DATA_DIR / "jobs.db"


def previous_write_at() -> Optional[float]:
    """When the previous run last wrote the archive, or None on a first run."""
    return _previous_write_at


def _get_conn() -> Optional[sqlite3.Connection]:
    """Return the persistent WAL connection, opening it on first call.

    Returns None and disables further attempts when the volume isn't
    writable. The in-memory registry is still correct; the user just
    forgoes restart durability rather than seeing a stream of OSError
    on every status change.
    """
    global _disabled, _conn, _schema_ready, _admission_ready, _previous_write_at
    global _damaged
    if _disabled:
        return None
    if _conn is not None:
        return _conn
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _previous_write_at = max(
            (path.stat().st_mtime for path in (_path(), _path().with_suffix(".db-wal"))
             if path.exists()),
            default=None,
        )
        _conn = sqlite3.connect(str(_path()), timeout=5.0,
                                check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        # A big review's save left a WAL that size on disk for good.
        _conn.execute("PRAGMA journal_size_limit=4194304")
        return _conn
    except (OSError, sqlite3.Error) as e:
        failed = _conn
        _conn = None
        if failed is not None:
            try:
                failed.close()
            except sqlite3.Error:
                pass
        _log.info("job persistence disabled (%s); jobs won't survive restart.", e)
        _damaged = _is_damage(e)
        _disabled = True
        _schema_ready = False
        _admission_ready = False
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    artist        TEXT NOT NULL DEFAULT '',
    album_id      TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'download',
    status        TEXT NOT NULL,
    phase         TEXT NOT NULL DEFAULT '',
    candidates    TEXT NOT NULL DEFAULT '[]',
    error         TEXT,
    summary       TEXT NOT NULL DEFAULT '',
    review_verb   TEXT NOT NULL DEFAULT 'Download',
    execute_kind  TEXT NOT NULL DEFAULT '',
    execute_args  TEXT NOT NULL DEFAULT '{}',
    created_at    REAL,
    finished_at   REAL,
    single        TEXT NOT NULL DEFAULT '{}',
    attention     TEXT NOT NULL DEFAULT '',
    recoveries    TEXT NOT NULL DEFAULT '[]',
    log_lines     TEXT NOT NULL DEFAULT '[]',
    quality_shortfall TEXT NOT NULL DEFAULT '{}',
    edition       TEXT NOT NULL DEFAULT '',
    tracks_missing INTEGER NOT NULL DEFAULT 0
)
"""

# A review's rows, one each, so a change rewrites the rows it touched and
# not the list. ``pos`` orders them and leaves gaps so a row can be added
# between two others without renumbering the rest. Each row's tick is kept
# apart from its body, so selecting a whole review rewrites only the ticks.
# Bodies and premises run to kilobytes, which pack better in rowid tables.
_CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_candidates (
    job_id TEXT NOT NULL,
    cid    TEXT NOT NULL,
    pos    INTEGER NOT NULL,
    body   TEXT NOT NULL,
    PRIMARY KEY (job_id, cid)
)
"""

_TICK_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_ticks (
    job_id   TEXT NOT NULL,
    cid      TEXT NOT NULL,
    selected INTEGER,
    PRIMARY KEY (job_id, cid)
) WITHOUT ROWID
"""

# The receipt every missing album of one artist shares (_shared_premises).
_PREMISE_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_premises (
    job_id TEXT NOT NULL,
    artist TEXT NOT NULL,
    body   TEXT NOT NULL,
    PRIMARY KEY (job_id, artist)
)
"""

_DURABLE_COMPLETION_SCHEMA = """
CREATE TABLE IF NOT EXISTS durable_job_completions (
    job_id        TEXT NOT NULL,
    operation_id  TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    job_created_at REAL NOT NULL,
    album_id      TEXT NOT NULL,
    completion_hash TEXT NOT NULL,
    acknowledged_at REAL NOT NULL,
    PRIMARY KEY (job_id, operation_id, item_id)
)
"""


_SCHEMA_VERSION = 8


def scrub_stored_secrets() -> int | None:
    """Mask account secrets in rows written before the masking existed, and
    report how many rows changed. A provider error carrying the signed-in URL
    reached job.error, job.summary and the stored activity log, so an upgrade
    that only fixed new writes would leave every existing archive readable."""
    columns = ("summary", "error", "log_lines", "phase", "candidates",
               "execute_args", "single", "recoveries")
    changed = 0
    with _lock:
        conn = _get_conn()
        if conn is None or not _schema_ready:
            return None
        try:
            rows = conn.execute(
                f"SELECT id, {', '.join(columns)} FROM jobs").fetchall()
            for row in rows:
                cleaned = [redaction.redact(value) if isinstance(value, str)
                           else value for value in row[1:]]
                if list(row[1:]) == cleaned:
                    continue
                assignments = ", ".join(f"{c}=?" for c in columns)
                conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?",
                             (*cleaned, row[0]))
                changed += 1
            touched = set()
            for table, key in (("job_candidates", "cid"),
                               ("job_premises", "artist")):
                cleaned = []
                for job_id, name, body in conn.execute(
                        f"SELECT job_id, {key}, body FROM {table}"):
                    clean = (redaction.redact(body) if isinstance(body, str)
                             else body)
                    if clean != body:
                        cleaned.append((clean, job_id, name))
                        touched.add(job_id)
                if cleaned:
                    conn.executemany(
                        f"UPDATE {table} SET body=? WHERE job_id=? AND {key}=?",
                        cleaned)
            changed += len(touched)
            conn.commit()
            for job_id in touched:
                _saved_rows.pop(job_id, None)
        except sqlite3.Error:
            _rollback_failed_write(conn)
            return None
    return changed


def init() -> None:
    """Create the schema (and run additive migrations). Safe to call repeatedly."""
    global _schema_ready, _admission_ready, _damaged
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(_SCHEMA)
            conn.execute(_CANDIDATE_SCHEMA)
            conn.execute(_TICK_SCHEMA)
            conn.execute(_PREMISE_SCHEMA)
            conn.execute(_DURABLE_COMPLETION_SCHEMA)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_durable_job_completion_album "
                "ON durable_job_completions(job_id, job_created_at, album_id)"
            )
            # Terminal-row index: history_count() / history_page() /
            # prune_finished() all filter on status and order by finished_at.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_terminal "
                "ON jobs(status, finished_at, created_at)"
            )
            # load_all reads the jobs in this order without a sort.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)")
            # Ask the table what it has; user_version is only a stamp. Gating
            # these on the number leaves a db already carrying it unable to
            # gain a missing column, and every persist() then fails into
            # _note_write_failure, which swallows it and leaves a dead archive.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            for column, definition in (
                # Single-track download undo info, so a restart keeps the Undo
                # affordance on a finished one-track download.
                ("single", "TEXT NOT NULL DEFAULT '{}'"),
                # The finished-job needs-review marker, so the History chip and
                # nav dot survive a restart.
                ("attention", "TEXT NOT NULL DEFAULT ''"),
                # Exact retained Repair-backup state.
                ("recoveries", "TEXT NOT NULL DEFAULT '[]'"),
                # A finished job's activity log.
                ("log_lines", "TEXT NOT NULL DEFAULT '[]'"),
                ("quality_shortfall", "TEXT NOT NULL DEFAULT '{}'"),
                ("edition", "TEXT NOT NULL DEFAULT ''"),
                # How many tracks an imported album still lacks.
                ("tracks_missing", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in cols:
                    conn.execute(
                        f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != _SCHEMA_VERSION:
                # Another build may have deleted jobs without their rows.
                for table in _ROW_TABLES:
                    conn.execute(
                        f"DELETE FROM {table} WHERE job_id NOT IN "
                        "(SELECT id FROM jobs)")
            moved = _move_saved_reviews(conn)
            if version != _SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
            for job_id, saved in moved.items():
                _remember(job_id, saved)
            if moved:
                try:
                    # Give back the pages the old lists filled.
                    conn.execute("VACUUM")
                except sqlite3.Error as e:
                    _log.debug("jobs.db vacuum after moving reviews failed: %s",
                               e)
            _schema_ready = True
            _admission_ready = True
        except _WRITE_ERRORS as e:
            _rollback_failed_write(conn)
            _schema_ready = False
            _admission_ready = False
            _damaged = _is_damage(e)
            # A transient/locked/full/corrupt jobs.db here would otherwise
            # propagate out of restore_jobs() into the caller's broad
            # "couldn't restore prior jobs; starting fresh" handler, masking
            # a recoverable condition and then leaving every later persist()
            # silently non- durable.
            _log.warning("job persistence schema/migration failed; running "
                         "without crash durability until the volume recovers "
                         "and the app restarts: %s", e)


def _move_saved_reviews(conn) -> dict:
    """Move reviews an older build kept in the jobs row into their own tables,
    in the caller's transaction, and return what each now holds. A list that
    does not parse stays where it is and still loads as unreadable."""
    moved = {}
    legacy = [job_id for (job_id,) in conn.execute(
        "SELECT id FROM jobs WHERE candidates != '[]'")]
    for job_id in legacy:
        status, value = conn.execute(
            "SELECT status, candidates FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        rows, unreadable = _decode_candidates(value, status)
        # Rows are stored by id, so two sharing one would lose a row; such a
        # list keeps loading from the column as before.
        if unreadable or len({row["cid"] for row in rows}) != len(rows):
            continue
        # The jobs row is newer than any rows this id has in the table.
        _saved_rows.pop(job_id, None)
        moved[job_id] = _write_rows(conn, _encode_rows(
            job_id, status, rows, [row.get("selected") for row in rows],
            None))
        conn.execute("UPDATE jobs SET candidates='[]' WHERE id=?", (job_id,))
    return moved


def _is_damage(exc) -> bool:
    return getattr(exc, "sqlite_errorcode", None) in (
        sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT)


def _note_damage(exc) -> None:
    """Hold back new work when a read finds the file itself damaged.

    init() touches only the jobs table, so a bad page among the review rows
    first shows up here. Callers hold ``_lock``."""
    global _damaged, _admission_ready
    if _is_damage(exc):
        _damaged = True
        _admission_ready = False


def database_damaged() -> bool:
    """Whether jobs.db failed to open because the file itself is damaged."""
    return _damaged


def database_path():
    return _path()


def ready_for_admission() -> bool:
    """Whether the schema opened successfully and durable jobs can be saved."""
    with _lock:
        return (_schema_ready and _admission_ready and not _disabled
                and not _damaged)


_PERSIST_SQL = (
    "INSERT OR REPLACE INTO jobs "
    "(id, title, artist, album_id, kind, status, phase, candidates, "
    " error, summary, review_verb, execute_kind, execute_args, "
    " created_at, finished_at, single, attention, recoveries, log_lines, "
    " quality_shortfall, edition, tracks_missing) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_CURRENT_JOB_SINGLE = object()
# jobs.py imports this module, so the names live here rather than reaching
# back for its TERMINAL set; _TERMINAL_SQL is built from the same tuple.
_TERMINAL_STATUSES = ("done", "failed", "canceled")


def _job_values(job, *, single=_CURRENT_JOB_SINGLE):
    """Serialise one job while its lock is held, or return None."""
    snapshot = _job_snapshot(job, single=single)
    return None if snapshot is None else _encode_snapshot(job, snapshot)


def _job_snapshot(job, *, single=_CURRENT_JOB_SINGLE):
    """What one saved row needs, taken while ``job._lock`` is held.

    A review can hold a hundred thousand candidates, so they are kept as the
    list and each row's tick, the one field changed on a row in place, and
    encoded by _encode_snapshot, which does not need the lock.
    """
    if job._retired:
        return None
    job._persist_serial += 1
    try:
        execute_args_json = json.dumps(job.execute_args or {}, default=str)
        single_value = job.single if single is _CURRENT_JOB_SINGLE else single
        single_json = json.dumps(single_value or {}, default=str)
        # Recovery receipts are a safety boundary.
        recoveries_json = json.dumps(
            getattr(job, "recoveries", []) or [],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        # Only a finished or parked job's log is stored: a running one is on
        # screen live, and re-serialising a growing blob on every snapshot is
        # waste.
        status_value = (
            job.status.value if hasattr(job.status, "value") else str(job.status)
        )
        log_json = json.dumps(
            job.persisted_log_lines_locked()
            if status_value in _TERMINAL_STATUSES
            or status_value == "awaiting_review" else [],
            default=str,
        )
        quality_shortfall_json = json.dumps(
            getattr(job, "quality_shortfall", {}) or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as e:
        _note_write_failure(f"serialize {job.id}", e)
        return None
    rows = list(job.candidates or [])
    return job._persist_serial, rows, [row.get("selected") for row in rows], (
        job.id, job.title or "", job.artist or "",
        job.album_id or "", job.kind or "download",
        status_value,
        job.phase or "",
        # The rows live in job_candidates.
        "[]",
        job.error,
        job.summary or "",
        job.review_verb or "Download",
        job.execute_kind or "",
        execute_args_json,
        job.created_at,
        job.finished_at,
        single_json,
        getattr(job, "attention", "") or "",
        recoveries_json,
        log_json,
        quality_shortfall_json,
        str(getattr(job, "edition", "") or "").strip(),
        int(getattr(job, "tracks_missing", 0) or 0),
    )


_TICKS = (None, False, True)


class _RowTable:
    """Each saved row's digest, place and tick by cid, read like a dict of
    those tuples. Kept in a few flat buffers: three small objects a row,
    made again by every save after a scan, held on to memory the scan had
    freed."""

    __slots__ = ("_cids", "_digests", "_places", "_ticks")

    def __init__(self, rows):
        cids = sorted(rows)
        self._cids = cids
        self._digests = b"".join(rows[cid][0] for cid in cids)
        self._places = array("q", [rows[cid][1] for cid in cids])
        self._ticks = bytes(_TICKS.index(rows[cid][2]) for cid in cids)

    def get(self, cid, default=None):
        i = bisect.bisect_left(self._cids, cid)
        if i == len(self._cids) or self._cids[i] != cid:
            return default
        return (self._digests[i * _DIGEST_SIZE:(i + 1) * _DIGEST_SIZE],
                self._places[i], _TICKS[self._ticks[i]])

    def __iter__(self):
        return iter(self._cids)

    def __len__(self):
        return len(self._cids)


@dataclass(frozen=True)
class _SavedRows:
    """What the row tables hold for one job: each row's digest, place and
    tick by cid, and each premise's digest by artist."""

    rows: dict
    premises: dict


@dataclass(frozen=True)
class _RowsWrite:
    """One job's encoded rows, with the text of those ``base`` lacks or holds
    otherwise. ``source`` encodes them again against a newer base."""

    job_id: str
    base: Optional[_SavedRows]
    order: list
    bodies: dict
    premises: dict
    source: tuple


# What each job's rows are on disk, replaced whole after the commit that
# changed them, so a save can be compared against it without _lock. A job
# missing here has its rows replaced outright.
_saved_rows: dict[str, _SavedRows] = {}


def _remember(job_id, saved) -> None:
    """Note what a committed write left on disk; called under _lock."""
    if saved is not None and (saved.rows or saved.premises):
        _saved_rows[job_id] = saved
    else:
        _saved_rows.pop(job_id, None)

# Errors that end a write; encoding a review again under _lock can raise the
# last two.
_WRITE_ERRORS = (sqlite3.Error, TypeError, ValueError)

_POS_GAP = 1024


_DIGEST_SIZE = 16


def _digest(text) -> bytes:
    return hashlib.blake2b(text.encode(), digest_size=_DIGEST_SIZE).digest()


def _encode_rows(job_id, status, rows, ticks, base) -> _RowsWrite:
    if status not in _ROW_STATUSES:
        rows, ticks = [], []
    premises = _shared_premises(rows)
    known = base.rows if base is not None else {}
    order, bodies = [], {}
    for row, tick in zip(rows, ticks):
        if "selected" in row:
            row = dict(row)
            del row["selected"]
        premise = premises.get(_artist_of(row))
        if premise is not None:
            row = candidate_premise.compact_candidate(row, premise)
        text = json.dumps(row, default=str)
        digest = _digest(text)
        cid = row["cid"]
        order.append((cid, digest, None if tick is None else bool(tick)))
        if known.get(cid, (None,))[0] != digest:
            bodies[cid] = text
    shared = {}
    for artist, premise in premises.items():
        text = json.dumps(premise, default=str)
        digest = _digest(text)
        unchanged = base is not None and base.premises.get(artist) == digest
        shared[artist] = (digest, None if unchanged else text)
    return _RowsWrite(job_id, base, order, bodies, shared,
                      (status, rows, ticks))


def _positions(order, saved):
    """Each row's place. A saved row keeps its own while the order allows, so
    adding or removing rows moves no other row."""
    places = [None] * len(order)
    last = 0
    for i, (cid, *_rest) in enumerate(order):
        old = saved.get(cid)
        if old is not None and old[1] > last:
            places[i] = last = old[1]
    prev, i = 0, 0
    while i < len(places):
        if places[i] is not None:
            prev = places[i]
            i += 1
            continue
        end = i
        while end < len(places) and places[end] is None:
            end += 1
        if end == len(places):
            step = _POS_GAP
        else:
            step = (places[end] - prev) // (end - i + 1)
            if step < 1:
                return [(n + 1) * _POS_GAP for n in range(len(places))]
        for n in range(i, end):
            prev += step
            places[n] = prev
        i = end
    return places


def _write_rows(conn, write) -> _SavedRows:
    """Bring one job's saved rows to ``write`` in the open transaction and
    return what they hold once it commits."""
    current = _saved_rows.get(write.job_id)
    if current is not None and len(write.order) < len(current.rows) // 2:
        # Most rows are leaving, as when an approved run keeps only its
        # picks: clearing the job's rows and adding back the few is quicker
        # than deleting each.
        current = None
    if current is not write.base:
        write = _encode_rows(write.job_id, *write.source, current)
    job_id, base = write.job_id, write.base
    if base is None:
        _delete_rows(conn, (job_id,))
        base = _SavedRows({}, {})
    rows, changed, moved, ticked = {}, [], [], []
    for (cid, digest, tick), pos in zip(write.order,
                                        _positions(write.order, base.rows)):
        rows[cid] = (digest, pos, tick)
        old = base.rows.get(cid)
        if old is None or old[0] != digest:
            changed.append((job_id, cid, pos, write.bodies[cid]))
        elif old[1] != pos:
            moved.append((pos, job_id, cid))
        if old is None or old[2] != tick:
            ticked.append((job_id, cid, tick))
    gone = [(job_id, cid) for cid in base.rows if cid not in rows]
    conn.executemany(
        "DELETE FROM job_candidates WHERE job_id=? AND cid=?", gone)
    conn.executemany("DELETE FROM job_ticks WHERE job_id=? AND cid=?", gone)
    conn.executemany(
        "INSERT OR REPLACE INTO job_candidates (job_id, cid, pos, body) "
        "VALUES (?,?,?,?)", changed)
    conn.executemany(
        "UPDATE job_candidates SET pos=? WHERE job_id=? AND cid=?", moved)
    conn.executemany(
        "INSERT OR REPLACE INTO job_ticks (job_id, cid, selected) "
        "VALUES (?,?,?)", ticked)
    premises = {artist: digest
                for artist, (digest, _text) in write.premises.items()}
    conn.executemany(
        "DELETE FROM job_premises WHERE job_id=? AND artist=?",
        [(job_id, artist) for artist in base.premises
         if artist not in premises])
    conn.executemany(
        "INSERT OR REPLACE INTO job_premises (job_id, artist, body) "
        "VALUES (?,?,?)",
        [(job_id, artist, text)
         for artist, (_digest, text) in write.premises.items()
         if text is not None])
    return _SavedRows(_RowTable(rows), premises)


_ROW_TABLES = ("job_candidates", "job_ticks", "job_premises")


def _delete_rows(conn, job_ids) -> None:
    ids = [(job_id,) for job_id in job_ids]
    for table in _ROW_TABLES:
        conn.executemany(f"DELETE FROM {table} WHERE job_id=?", ids)


def _save_job(conn, values, rows) -> _SavedRows:
    """Write one job row and its review rows in the open transaction."""
    conn.execute(_PERSIST_SQL, values)
    return _write_rows(conn, rows)


_READ_BATCH = 2048


def _parse_batch(bodies, seen):
    """Parse saved rows as one document, so they share one copy of each key,
    and point repeated text, such as an artist's name, at one copy."""
    if any(type(body) is not str for body in bodies):
        raise TypeError("a saved row is not text")
    values = json.loads("[" + ",".join(bodies) + "]")
    # Damaged rows can still join into a list of the right length, so each
    # must also be one value on its own.
    if len(values) != len(bodies):
        raise ValueError("a saved row is not one value")
    for body in bodies:
        json.loads(body)
    for value in values:
        for part in (value, value.get("payload")) if type(value) is dict else ():
            if type(part) is dict:
                for key, text in part.items():
                    if type(text) is str:
                        part[key] = seen.setdefault(text, text)
    return values


def _read_rows(conn, job_id):
    """One job's saved rows in order and its premises, and what they hold;
    (None, None) when they do not parse."""
    found, premises, saved, shared = [], {}, {}, {}
    seen = {}
    try:
        ticks = {cid: None if selected is None else bool(selected)
                 for cid, selected in conn.execute(
                     "SELECT cid, selected FROM job_ticks WHERE job_id=?",
                     (job_id,))}
        cursor = conn.execute(
            "SELECT cid, pos, body FROM job_candidates WHERE job_id=?",
            (job_id,))
        while batch := cursor.fetchmany(_READ_BATCH):
            rows = _parse_batch([body for _cid, _pos, body in batch], seen)
            for (cid, pos, body), row in zip(batch, rows):
                cid = seen.setdefault(cid, cid)
                tick = ticks.get(cid)
                if tick is not None and type(row) is dict:
                    row["selected"] = tick
                found.append((pos, row))
                saved[cid] = (_digest(body), pos, tick)
        cursor = conn.execute(
            "SELECT artist, body FROM job_premises WHERE job_id=?",
            (job_id,))
        while batch := cursor.fetchmany(_READ_BATCH):
            values = _parse_batch([body for _artist, body in batch], seen)
            for (artist, body), value in zip(batch, values):
                premises[artist] = value
                shared[artist] = _digest(body)
        found.sort(key=lambda item: item[0])
        table = _RowTable(saved)
    except (TypeError, ValueError, OverflowError):
        return None, None
    return ([row for _pos, row in found], premises), _SavedRows(table, shared)


def _encode_snapshot(job, snapshot):
    """The row values and review rows for a _job_snapshot, or None."""
    _serial, rows, ticks, values = snapshot
    try:
        write = _encode_rows(values[0], values[5], rows, ticks,
                             _saved_rows.get(values[0]))
    except (TypeError, ValueError) as e:
        _note_write_failure(f"serialize {job.id}", e)
        return None
    return values, write


def _write_snapshot(job, snapshot) -> bool:
    """Encode and save a _job_snapshot without holding the job lock.

    A snapshot taken after this one wins, so this one is then not written; a
    job retired meanwhile, its row merged or deleted, is not written back.
    """
    encoded = _encode_snapshot(job, snapshot)
    if encoded is None:
        return False
    with job._lock:
        _lock.acquire()
        superseded = job._persist_serial != snapshot[0]
        retired = job._retired
    try:
        if retired:
            return False
        if superseded:
            return True
        conn = _get_conn()
        if conn is None:
            return False
        try:
            saved = _save_job(conn, *encoded)
            conn.commit()
            _remember(job.id, saved)
            return True
        except _WRITE_ERRORS as e:
            _rollback_failed_write(conn)
            _note_write_failure(f"persist {job.id}", e)
            return False
    finally:
        _lock.release()


_TERMINAL_RECORD_SQL = (
    "INSERT INTO jobs "
    "(id, title, artist, album_id, kind, execute_kind, status, summary, "
    " error, created_at, finished_at, edition, attention, quality_shortfall, "
    " execute_args, tracks_missing) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


# A terminal run that just finished can wait out the web app's long writes,
# which the five-second connection timeout cannot.
_TERMINAL_RECORD_WAIT = 30.0


def _insert_terminal_record(values, what):
    """Insert one row for finished terminal work and return its id."""
    init()
    deadline = time.monotonic() + _TERMINAL_RECORD_WAIT
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        draws = 0
        while draws < 3:
            job_id = uuid.uuid4().hex[:8]
            try:
                conn.execute(_TERMINAL_RECORD_SQL, (job_id, *values))
                conn.commit()
                return job_id
            except sqlite3.IntegrityError:
                # Same eight-character id space the web uses; a collision is
                # rare enough to just draw again.
                _rollback_failed_write(conn)
                draws += 1
            except sqlite3.Error as e:
                _rollback_failed_write(conn)
                busy = ((getattr(e, "sqlite_errorcode", None) or 0) & 0xFF
                        == sqlite3.SQLITE_BUSY)
                if busy and time.monotonic() < deadline:
                    time.sleep(0.5)
                    conn = _get_conn()
                    if conn is None:
                        return None
                    continue
                if busy:
                    _log.info("History entry for %s not saved: the web app "
                              "kept %s busy for %d seconds", what, _path(),
                              _TERMINAL_RECORD_WAIT)
                else:
                    _note_write_failure(f"record terminal {what}", e)
                return None
    return None


def record_terminal_download(
    *,
    album_id,
    artist,
    title,
    summary,
    started_at,
    finished_at,
    result,
    edition="",
    status=None,
    error=None,
):
    """Add one finished terminal download to the activity record and return
    its id.

    History reads this table, and the table is only ever written from live Job
    objects, which a terminal run does not have. Downloads made in the terminal
    were therefore missing from the record entirely. The row carries what a
    History entry shows and nothing that would offer a retry the row cannot
    service.
    """
    album_id = str(album_id or "").strip()
    if not album_id:
        return None
    outcome, attention = download_result.download_job_outcome(result)
    shortfall = (
        download_result.quality_shortfall_record(result.get("quality_verdict"))
        if attention == "quality" else {}
    )
    return _insert_terminal_record((
        str(title or ""), str(artist or ""), album_id, "download", "",
        status or outcome, str(summary or ""), error, started_at, finished_at,
        str(edition or "").strip(), attention, json.dumps(shortfall),
        json.dumps({"retry_disabled": "terminal"}),
        download_result.tracks_missing(result),
    ), f"download {album_id}")


def record_terminal_run(*, kind, title, status, summary, started_at,
                        finished_at, error=None):
    """Add one finished terminal Upgrade, Repair or Downsample run to the
    activity record, as the card its web run leaves, and return its id."""
    return _insert_terminal_record((
        str(title or ""), "", "", "scan", kind, status, str(summary or ""),
        error, started_at, finished_at, "", "", "{}",
        json.dumps({"retry_disabled": "terminal"}), 0,
    ), kind)


def _persist_locked(job, *, admission=False) -> bool:
    """Write one ordinary snapshot while the caller holds ``job._lock``."""
    global _admission_ready
    encoded = _job_values(job)
    if encoded is None:
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            if admission:
                _admission_ready = False
            return False
        try:
            saved = _save_job(conn, *encoded)
            conn.commit()
            _remember(job.id, saved)
            if admission:
                _admission_ready = True
            return True
        except _WRITE_ERRORS as e:
            _rollback_failed_write(conn)
            if admission:
                _admission_ready = False
            _note_write_failure(f"persist {job.id}", e)
            return False


def persist(job) -> bool:
    """Write the job's current state to disk. Return whether it reached disk."""
    with job._lock:
        if getattr(job, "_preserve_persisted_single", False) is True:
            # Keep the job lock through both the preservation decision and
            # the database commit.
            return _persist_preserving_single_locked(job)
        snapshot = _job_snapshot(job)
    return snapshot is not None and _write_snapshot(job, snapshot)


def persist_review_mutation(job, mutation) -> tuple[bool, object]:
    """Save one candidate-list mutation or restore its exact prior state.

    Mutations replace rows rather than edit them, so the prior list is kept
    without copying each row. The save runs once the review's locks are
    released, so ticks are not held up while a large review is written.
    """
    with job._review_action_lock, job._lock:
        if job._retired:
            return False, None
        had_saved_signature = hasattr(job, "_saved_review_signature")
        previous = (
            list(job.candidates),
            job._cand_seq,
            job._candidate_cap_noted,
            copy.deepcopy(job.execute_args),
            job.summary,
            had_saved_signature,
            getattr(job, "_saved_review_signature", None),
        )

        def _restore():
            (
                job.candidates,
                job._cand_seq,
                job._candidate_cap_noted,
                job.execute_args,
                job.summary,
                restore_saved_signature,
                saved_signature,
            ) = previous
            if restore_saved_signature:
                job._saved_review_signature = saved_signature
            else:
                job.__dict__.pop("_saved_review_signature", None)

        try:
            result = mutation()
        except BaseException:
            _restore()
            raise
        current = (
            job.candidates,
            job._cand_seq,
            job._candidate_cap_noted,
            job.execute_args,
            job.summary,
            hasattr(job, "_saved_review_signature"),
            getattr(job, "_saved_review_signature", None),
        )
        if current == previous:
            return True, result
        snapshot = _job_snapshot(job)
        changed = job.candidates
    if snapshot is not None and _write_snapshot(job, snapshot):
        return True, result
    with job._review_action_lock, job._lock:
        # A later change already built on this one keeps its own result.
        if job.candidates is changed:
            _restore()
    return False, result


def acknowledge_attention(job_id: str, expected_attention: str) -> bool:
    """Clear one non-recovery attention marker without rewriting the job."""
    if (
        not isinstance(job_id, str)
        or not job_id
        or not isinstance(expected_attention, str)
        or not expected_attention
        or expected_attention == "recovery"
    ):
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attention FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            current = row[0] or ""
            if not current:
                conn.rollback()
                return True
            if current != expected_attention:
                conn.rollback()
                return False
            changed = conn.execute(
                "UPDATE jobs SET attention='' WHERE id=? AND attention=?",
                (job_id, expected_attention),
            )
            if changed.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except sqlite3.Error as exc:
            _rollback_failed_write(conn)
            _note_write_failure(f"acknowledge attention for {job_id}", exc)
            return False


def mark_retried(job_id: str, retry_id: str) -> bool:
    """Record that a finished download was retried as ``retry_id``, so it no
    longer offers Retry itself."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT execute_args FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            args, unreadable = _decode_execute_args(row[0] if row else None)
            if row is None or unreadable:
                conn.rollback()
                return False
            args.update(retry_disabled="retried", retried_as=retry_id)
            conn.execute(
                "UPDATE jobs SET execute_args=? WHERE id=?",
                (json.dumps(args, default=str), job_id),
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            _rollback_failed_write(conn)
            _note_write_failure(f"mark {job_id} retried", exc)
            return False


def _persist_preserving_single_locked(job, *, admission=False) -> bool:
    """Save while preserving Undo; the caller holds ``job._lock``."""
    global _admission_ready
    encoded = _job_values(job)
    if encoded is None:
        return False
    values, rows = encoded
    with _lock:
        conn = _get_conn()
        if conn is None:
            if admission:
                _admission_ready = False
            job._single_undo_unavailable = True
            return False
        try:
            updated = conn.execute(
                "UPDATE jobs SET title=?, artist=?, kind=?, status=?, "
                "phase=?, candidates=?, error=?, summary=?, "
                "review_verb=?, execute_kind=?, execute_args=?, "
                "finished_at=?, attention=?, recoveries=? "
                "WHERE id=? AND created_at=? AND album_id=?",
                (
                    values[1], values[2], values[4], values[5], values[6],
                    values[7], values[8], values[9], values[10], values[11],
                    values[12], values[14], values[16], values[17], values[0],
                    values[13], values[3],
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                job._single_undo_unavailable = True
                return False
            saved = _write_rows(conn, rows)
            row = conn.execute(
                "SELECT single FROM jobs "
                "WHERE id=? AND created_at=? AND album_id=?",
                (values[0], values[13], values[3]),
            ).fetchone()
            try:
                durable_single = json.loads(row[0])
            except (IndexError, TypeError, ValueError):
                durable_single = None
            conn.commit()
            _remember(job.id, saved)
            if type(durable_single) is dict:
                job.single = durable_single
                job.__dict__.pop("_single_undo_unavailable", None)
            else:
                job.single = {}
                job._single_undo_unavailable = True
            if admission:
                _admission_ready = True
            return True
        except _WRITE_ERRORS as exc:
            _rollback_failed_write(conn)
            if admission:
                _admission_ready = False
            _note_write_failure(
                f"persist {job.id} while preserving its Undo record",
                exc,
            )
            job._single_undo_unavailable = True
            return False


def admit(job) -> bool:
    """Durably save a job before it may enter or start on a worker lane.

    This named boundary distinguishes mandatory admission from later history
    updates, whose callers may still degrade to the in-memory view.
    """
    with job._lock:
        if getattr(job, "_preserve_persisted_single", False) is True:
            saved = _persist_preserving_single_locked(job, admission=True)
        else:
            saved = _persist_locked(job, admission=True)
        if saved:
            job._durability_required = True
        return saved


def admit_review_transition(job, parked_jobs=()) -> bool:
    """Atomically save one locked approval and its unpublished remnants.

    The caller holds ``job._lock`` across the state change and this call. Each
    parked job is new and cannot yet be reached by another thread. Keeping that
    lock through the commit prevents Cancel from observing PENDING before the
    durable owner row exists.
    """
    global _admission_ready
    parked = sorted(
        {other.id: other for other in parked_jobs}.values(), key=lambda j: j.id
    )
    with ExitStack() as locks:
        for other in parked:
            locks.enter_context(other._lock)
        ordered = [job, *parked]
        encoded = [_job_values(other) for other in ordered]
        if any(value is None for value in encoded):
            return False
        with _lock:
            conn = _get_conn()
            if conn is None:
                _admission_ready = False
                return False
            try:
                conn.execute("BEGIN IMMEDIATE")
                saved = [_save_job(conn, *value) for value in encoded]
                conn.commit()
                for other, rows in zip(ordered, saved):
                    _remember(other.id, rows)
                    other._durability_required = True
                _admission_ready = True
                return True
            except _WRITE_ERRORS as e:
                _rollback_failed_write(conn)
                _admission_ready = False
                _note_write_failure("admit related jobs", e)
                return False


def restore_split_review(job, parked_job) -> bool:
    """Atomically restore one review and remove its unpublished remnant.

    The caller holds both job locks after rebuilding ``job.candidates``. This
    is the inverse of ``admit_review_transition`` for a remote failure that
    happened before any selected work changed files.
    """
    global _admission_ready
    encoded = _job_values(job)
    if encoded is None or parked_job is job:
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            _admission_ready = False
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            saved = _save_job(conn, *encoded)
            conn.execute(
                "DELETE FROM durable_job_completions WHERE job_id=?",
                (parked_job.id,),
            )
            conn.execute("DELETE FROM jobs WHERE id=?", (parked_job.id,))
            _delete_rows(conn, (parked_job.id,))
            conn.commit()
            _remember(job.id, saved)
            _saved_rows.pop(parked_job.id, None)
            job._durability_required = True
            _admission_ready = True
            return True
        except _WRITE_ERRORS as exc:
            _rollback_failed_write(conn)
            _admission_ready = False
            _note_write_failure("restore split review", exc)
            return False


def persist_recoveries(job) -> bool:
    """Durably replace only a job's exact Repair-recovery state.

    The Repair safety gate calls this before it starts downloading. Requiring
    an existing job row makes a lost initial job save fail closed instead of
    manufacturing a partial row that cannot describe the running operation.
    """
    with job._lock:
        try:
            recoveries_json = json.dumps(
                getattr(job, "recoveries", []) or [],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            job_id = job.id
            attention = getattr(job, "attention", "") or ""
        except (TypeError, ValueError) as exc:
            _note_write_failure(
                f"serialize Repair recovery for {getattr(job, 'id', '?')}",
                exc,
            )
            return False

        # Match persist(): the job lock stays held through the database commit
        # so this narrow writer cannot replay an older recovery snapshot after
        # manual Restore has retired it.
        with _lock:
            conn = _get_conn()
            if conn is None:
                return False
            try:
                changed = conn.execute(
                    "UPDATE jobs SET recoveries=?, attention=? WHERE id=?",
                    (recoveries_json, attention, job_id),
                )
                if changed.rowcount != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except sqlite3.Error as exc:
                _rollback_failed_write(conn)
                _note_write_failure(
                    f"persist Repair recovery for {job_id}", exc)
                return False


def acknowledge_missing_recoveries(job, is_missing) -> bool:
    """Durably retire exact kept recoveries confirmed missing by the caller."""
    if not callable(is_missing):
        return False
    with job._lock:
        recoveries = list(getattr(job, "recoveries", []) or [])
        if (
            getattr(job, "attention", "") != "recovery"
            or not recoveries
            or any(not _valid_recovery(record) for record in recoveries)
        ):
            return False
        try:
            if any(not is_missing(record) for record in recoveries):
                return False
        except Exception as exc:
            _log.debug("couldn't verify missing Repair recovery: %s", exc)
            return False

        migration = all(record["kind"] == "migration" for record in recoveries)
        if migration:
            resolution = (
                "That kept file is no longer there, so there is nothing left "
                "to review."
                if len(recoveries) == 1
                else "Those kept files are no longer there, so there is "
                     "nothing left to review."
            )
        else:
            resolution = (
                "That backup is no longer on disk, so there is nothing left to "
                "review."
                if len(recoveries) == 1
                else "Those backups are no longer on disk, so there is nothing "
                     "left to review."
            )
        acknowledgement_lines = []
        for record in recoveries:
            line = (
                "Cleared the record for a kept migration file that is gone: "
                if record["kind"] == "migration"
                else "Cleared the record for a Repair backup folder that is gone: "
            ) + record["location"]
            acknowledgement_lines.append(job._CTRL_RE.sub("", line))

        saved = False
        next_summary = ""
        with _lock:
            conn = _get_conn()
            if conn is not None:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        "SELECT recoveries, attention, summary, log_lines "
                        "FROM jobs WHERE id=?",
                        (job.id,),
                    ).fetchone()
                    persisted, unreadable = _decode_recoveries(
                        row[0] if row is not None else None
                    )
                    if (
                        row is None
                        or unreadable
                        or persisted != recoveries
                        or (row[1] or "") != "recovery"
                    ):
                        conn.rollback()
                    else:
                        archived_lines = json.loads(row[3] or "[]")
                        if type(archived_lines) is not list:
                            raise ValueError("saved job log is not a list")
                        archived_lines.extend(acknowledgement_lines)
                        if len(archived_lines) > job.LOG_PERSIST_CAP:
                            archived_lines = archived_lines[-job.LOG_PERSIST_CAP:]
                            archived_lines[0] = job._PERSIST_TRUNCATION_MARKER
                        log_json = json.dumps(archived_lines, default=str)
                        next_summary = (
                            f"{(row[2] or '').rstrip()} {resolution}"
                        ).lstrip()
                        changed = conn.execute(
                            "UPDATE jobs SET recoveries='[]', attention='', "
                            "summary=?, log_lines=? WHERE id=?",
                            (next_summary, log_json, job.id),
                        )
                        if changed.rowcount != 1:
                            conn.rollback()
                        else:
                            conn.commit()
                            saved = True
                except (sqlite3.Error, TypeError, ValueError) as exc:
                    _rollback_failed_write(conn)
                    _note_write_failure(
                        f"acknowledge missing Repair recovery for {job.id}",
                        exc,
                    )
        if not saved:
            return False
        job.attention = ""
        job.recoveries = []
        job.summary = next_summary
        job.log_lines.extend(acknowledgement_lines)
        job._trim_log_lines_locked()
        return True


def acknowledge_durable_completion(
    job_id: str,
    owner: RecoveryOwner,
    *,
    album_id: str,
    completion_hash: str,
) -> bool:
    """Record one exact queue completion before its journal proof is removed.

    This lives outside the mutable Job snapshot so a delayed ordinary status
    save cannot erase the acknowledgement. Repeating the same acknowledgement
    is safe; changing any of its exact completion evidence is refused.
    """
    if (
        type(job_id) is not str
        or not job_id
        or type(owner) is not RecoveryOwner
        or normalise_album_id(album_id) != album_id
        or type(completion_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", completion_hash) is None
    ):
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute(
                "SELECT created_at, album_id FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if (
                job_row is None
                or type(job_row[0]) not in (int, float)
                or normalise_album_id(job_row[1]) != album_id
            ):
                conn.rollback()
                return False
            existing = conn.execute(
                "SELECT job_created_at, album_id, completion_hash "
                "FROM durable_job_completions "
                "WHERE job_id=? AND operation_id=? AND item_id=?",
                (job_id, owner.operation_id, owner.item_id),
            ).fetchone()
            if existing is not None:
                matches = existing == (
                    job_row[0],
                    album_id,
                    completion_hash,
                )
                conn.commit() if matches else conn.rollback()
                return matches
            conn.execute(
                "INSERT INTO durable_job_completions "
                "(job_id, operation_id, item_id, job_created_at, album_id, "
                "completion_hash, acknowledged_at) VALUES (?,?,?,?,?,?,?)",
                (
                    job_id,
                    owner.operation_id,
                    owner.item_id,
                    job_row[0],
                    album_id,
                    completion_hash,
                    time.time(),
                ),
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            _rollback_failed_write(conn)
            _note_write_failure(
                f"acknowledge durable completion for {job_id}",
                exc,
            )
            return False


def durable_completion_acknowledged(
    job_id: str,
    *,
    job_created_at: float,
    album_id: str,
    strict: bool = False,
) -> bool | None:
    """Check an exact job incarnation and album, or None if DB is unavailable."""
    if (
        type(job_id) is not str
        or not job_id
        or type(job_created_at) not in (int, float)
        or normalise_album_id(album_id) != album_id
    ):
        return None
    with _lock:
        conn = _get_conn()
        if conn is None:
            if strict:
                raise RuntimeError("saved download completions are unavailable")
            return None
        try:
            return conn.execute(
                "SELECT 1 FROM durable_job_completions "
                "WHERE job_id=? AND job_created_at=? AND album_id=? LIMIT 1",
                (job_id, job_created_at, album_id),
            ).fetchone() is not None
        except sqlite3.Error as exc:
            if strict:
                raise
            _log.debug(
                "couldn't inspect durable completion for %s: %s",
                job_id,
                exc,
            )
            return None


def prepare_recovery_resolution(
        location: str, receipt: dict | None) -> RecoveryResolutionPlan | None:
    """Seal exact persisted Repair records before a manual Restore mutates."""
    try:
        location = str(location)
        if not location or "\x00" in location:
            return None
        canonical_receipt = json.loads(json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ))
        if canonical_receipt is not None and type(canonical_receipt) is not dict:
            return None
    except (TypeError, ValueError):
        return None

    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            rows = conn.execute(
                "SELECT id, recoveries FROM jobs "
                "WHERE COALESCE(recoveries, '[]') != '[]'"
            ).fetchall()
        except sqlite3.Error as exc:
            _log.debug("couldn't inspect Repair recovery records: %s", exc)
            return None

    matched = []
    for job_id, raw in rows:
        try:
            records = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if (
            type(records) is not list
            or any(not _valid_recovery(record) for record in records)
        ):
            return None
        if any(
            record["location"] == location
            and record["receipt"] == canonical_receipt
            for record in records
        ):
            matched.append((str(job_id), str(raw)))
    return RecoveryResolutionPlan(
        location=location,
        receipt=canonical_receipt,
        rows=tuple(matched),
    )


def resolve_recovery_resolution(
        plan: RecoveryResolutionPlan) -> dict[str, dict] | None:
    """CAS-remove the exact retained records after Restore succeeds."""
    if type(plan) is not RecoveryResolutionPlan:
        return None
    resolved = {}
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            conn.execute("BEGIN IMMEDIATE")
            for job_id, expected_raw in plan.rows:
                row = conn.execute(
                    "SELECT recoveries, attention FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if row is None or row[0] != expected_raw:
                    conn.rollback()
                    return None
                records = json.loads(expected_raw)
                if (
                    type(records) is not list
                    or any(
                        not _valid_recovery(record)
                        for record in records
                    )
                ):
                    conn.rollback()
                    return None
                remaining = [
                    record for record in records
                    if not (
                        record["location"] == plan.location
                        and record["receipt"] == plan.receipt
                    )
                ]
                if len(remaining) == len(records):
                    conn.rollback()
                    return None
                attention = row[1] or ""
                if not remaining and attention == "recovery":
                    attention = ""
                encoded = json.dumps(
                    remaining,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                changed = conn.execute(
                    "UPDATE jobs SET recoveries=?, attention=? WHERE id=?",
                    (encoded, attention, job_id),
                )
                if changed.rowcount != 1:
                    conn.rollback()
                    return None
                resolved[job_id] = {
                    "recoveries": remaining,
                    "attention": attention,
                }
            conn.commit()
            return resolved
        except (sqlite3.Error, TypeError, ValueError) as exc:
            _rollback_failed_write(conn)
            _note_write_failure("resolve Repair recovery", exc)
            return None


def delete(job_id: str) -> None:
    """Drop the row for a job pruned from the registry."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            conn.execute(
                "DELETE FROM durable_job_completions WHERE job_id=?",
                (job_id,),
            )
            _delete_rows(conn, (job_id,))
            conn.commit()
            _saved_rows.pop(job_id, None)
        except sqlite3.Error as exc:
            _rollback_failed_write(conn)
            _log.debug("delete job %s failed: %s", job_id, exc)


def load_one(job_id: str) -> Optional[dict]:
    """Return one persisted job with decoded logs, or None if absent."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT id, title, artist, album_id, kind, status, phase, "
                "candidates, error, summary, review_verb, execute_kind, "
                "execute_args, created_at, finished_at, single, attention, "
                "recoveries, log_lines, quality_shortfall, edition, "
                "tracks_missing "
                "FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            stored = (_read_rows(conn, job_id)[0]
                      if row is not None and row[5] in _ROW_STATUSES
                      and row[7] == "[]" else _NO_ROWS)
        except sqlite3.Error as e:
            _note_damage(e)
            _log.debug("load_one failed for %s: %s", job_id, e)
            return None
    if row is None:
        return None
    return _decode_row(row, decode_logs=True, stored=stored)


def _decode_row(r, *, decode_logs, stored=_NO_ROWS):
    """One saved row as a job dict, or None when its recoveries are unreadable.

    Without ``decode_logs`` the log stays encoded under ``log_lines_json``.
    ``stored`` is what _read_rows returned for its review rows."""
    recoveries, recovery_unreadable = _decode_recoveries(r[17])
    if recovery_unreadable:
        return None
    execute_args, execute_args_unreadable = _decode_execute_args(r[12])
    candidates, candidates_unreadable = _decode_candidates(r[7], r[5], stored)
    single, single_unreadable = _decode_single(r[15])
    quality_shortfall, _ = _decode_object(r[19])
    job = {
        "id": r[0], "title": r[1], "artist": r[2], "album_id": r[3],
        "kind": r[4], "status": r[5], "phase": r[6],
        "candidates": candidates,
        "candidates_unreadable": candidates_unreadable,
        "error": r[8], "summary": r[9] or "", "review_verb": r[10] or "Download",
        "execute_kind": r[11] or "",
        "execute_args": execute_args,
        "execute_args_unreadable": execute_args_unreadable,
        "created_at": _decode_timestamp(r[13]),
        "finished_at": _decode_timestamp(r[14]),
        "single": single,
        "single_unreadable": single_unreadable,
        "attention": r[16] or "",
        "recoveries": recoveries,
        "quality_shortfall": quality_shortfall,
        "edition": r[20] or "",
        "tracks_missing": _tracks_missing(r[21], r[5], r[8]),
    }
    if decode_logs:
        job["log_lines"], job["log_lines_unreadable"] = decode_log_lines(r[18])
    else:
        job["log_lines_json"] = r[18]
    return job


def prune_finished(keep: int) -> None:
    """Drop the oldest terminal rows past ``keep`` so the archive doesn't
    grow without bound. Non-terminal jobs, unresolved Repair recoveries, and
    active single-track Undo records are never pruned here. Best-effort: a
    sqlite error logs and bows out."""
    if keep <= 0:
        return
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, single FROM jobs "
                "WHERE status IN ('done', 'failed', 'canceled') "
                "AND COALESCE(recoveries, '[]') = '[]' "
                "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC"
            ).fetchall()
            ordinary_seen = 0
            removable = []
            for job_id, raw_single in rows:
                try:
                    single = json.loads(raw_single or "{}")
                except (TypeError, ValueError):
                    continue
                if type(single) is not dict or has_active_single_undo(single):
                    continue
                ordinary_seen += 1
                if ordinary_seen > keep:
                    removable.append((job_id,))
            conn.executemany("DELETE FROM jobs WHERE id=?", removable)
            conn.execute(
                "DELETE FROM durable_job_completions "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=durable_job_completions.job_id)"
            )
            _delete_rows(conn, (job_id for (job_id,) in removable))
            conn.commit()
            for (job_id,) in removable:
                _saved_rows.pop(job_id, None)
        except sqlite3.Error as e:
            _rollback_failed_write(conn)
            _log.debug("prune_finished(%d) failed: %s", keep, e)


_TERMINAL_SQL = "status IN ({})".format(
    ", ".join(f"'{name}'" for name in _TERMINAL_STATUSES))
# Parked reviews belong to their own pages. New-release, restore and
# migration reviews have no other page to return to, so History lists them
# with a link there. Clearing and pruning must never touch them.
_HISTORY_SQL = (
    "(status IN ('done', 'failed', 'canceled') OR "
    "(status = 'awaiting_review' AND execute_kind IN "
    "('new_releases', 'collection_restore', 'migration')))"
)

# Work cut short by a restart is saved as cancelled, or as failed when it
# keeps a Retry, and only the opening of its summary or error tells it apart
# from a stop or failure of its own.
RESTART_INTERRUPTED = "Interrupted by a restart."

# Saved with a Search download while it holds a saved queue entry, which the
# terminal can settle without the web app seeing it.
SAVED_QUEUE = "saved_queue"

# How many albums an Upgrade, Downsample, Repair or Library download run has
# finished, and of how many picked, saved with its details so a restart can
# say what it had already done.
ALBUMS_DONE = "_albums_done"
ALBUMS_PICKED = "_albums_picked"


def interrupted_by_restart(status: str, summary: str, error=None) -> bool:
    reason = summary if status == "canceled" else error
    return status in ("canceled", "failed") and (reason or "").startswith(
        RESTART_INTERRUPTED)


def record_stop(job) -> bool:
    """Save the log and stop time of a job the app stops mid-run, leaving its
    saved status for the next start to report as interrupted."""
    with job._lock:
        log_json = json.dumps(job.persisted_log_lines_locked(), default=str)
        values = (log_json, time.time(), job.id)
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            conn.execute(
                "UPDATE jobs SET log_lines=?, finished_at=? "
                "WHERE id=? AND status IN ('running', 'scanning')",
                values,
            )
            conn.commit()
            return True
        except sqlite3.Error as e:
            _rollback_failed_write(conn)
            _note_write_failure(f"record stop of {job.id}", e)
            return False


# History splits finished work into two layers: meaningful jobs get cards,
# plain downloads get a compact table. The kinds below are the card layer.
BULK_KINDS = ("library", "new_releases", "upgrade", "downsample", "repair",
              "lyrics", "migration")

_BULK_MARKS = ",".join("?" for _ in BULK_KINDS)


def _kind_clause(bulk):
    # Downloads always carry the album they fetched; scans and other bulk work
    # never do. That split survives legacy rows whose execute_kind is empty.
    if bulk is None:
        return "", ()
    if bulk:
        return (f"AND (execute_kind IN ({_BULK_MARKS}) "
                "OR COALESCE(album_id, '') = '') ", BULK_KINDS)
    return (f"AND execute_kind NOT IN ({_BULK_MARKS}) "
            "AND COALESCE(album_id, '') != '' ", BULK_KINDS)


def history_count(
        bulk: Optional[bool] = None, *, exclude_recoveries: bool = False,
        attention_only: bool = False) -> int:
    """How many finished jobs are on disk, for paginating the History view."""
    clause, params = _kind_clause(bulk)
    if exclude_recoveries:
        clause += "AND COALESCE(recoveries, '[]') = '[]' "
    if attention_only:
        clause += f"AND {_TERMINAL_SQL} AND attention != '' "
    with _lock:
        conn = _get_conn()
        if conn is None:
            return 0
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {_HISTORY_SQL} {clause}",
                params).fetchone()[0]
        except sqlite3.Error:
            return 0


def history_page(limit: int, offset: int,
                 bulk: Optional[bool] = None,
                 status: Optional[str] = None, *,
                 exclude_recoveries: bool = False,
                 attention_only: bool = False) -> list[dict]:
    """A page of finished jobs, newest first: the browsable record behind the
    History view. Lighter than ``load_all`` (no candidates or log lines): what a
    history row shows, plus the id to open the full job. The ``id`` tiebreaker
    keeps paging stable when finish times collide. ``bulk`` narrows to the
    card layer (True), the downloads table (False), or everything (None).
    ``status`` narrows in SQL. A caller filtering the returned page itself
    would silently lose older matching rows whenever the newest ``limit``
    rows are mostly other statuses."""
    clause, params = _kind_clause(bulk)
    if exclude_recoveries:
        clause += "AND COALESCE(recoveries, '[]') = '[]' "
    if attention_only:
        clause += f"AND {_TERMINAL_SQL} AND attention != '' "
    if status:
        clause += "AND status = ? "
        params = (*params, status)
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT id, title, artist, album_id, status, error, summary, "
                "execute_kind, execute_args, created_at, finished_at, "
                "attention, recoveries, edition, single, quality_shortfall, "
                "tracks_missing "
                "FROM jobs "
                f"WHERE {_HISTORY_SQL} {clause}"
                "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        except sqlite3.Error as e:
            _note_damage(e)
            _log.debug("history_page failed: %s", e)
            return []
    result = []
    for row in rows:
        recoveries, recovery_unreadable = _decode_recoveries(row[12])
        execute_args, execute_args_unreadable = _decode_execute_args(row[8])
        _single, single_unreadable = _decode_single(row[14])
        result.append({
            "id": row[0], "title": row[1] or "", "artist": row[2] or "",
            "album_id": row[3] or "", "status": row[4], "error": row[5],
            "summary": row[6] or "", "execute_kind": row[7] or "",
            "interrupted_by_restart": interrupted_by_restart(
                row[4], row[6], row[5]),
            "execute_args": execute_args,
            "execute_args_unreadable": execute_args_unreadable,
            "created_at": _decode_timestamp(row[9]),
            "finished_at": _decode_timestamp(row[10]),
            "attention": row[11] or "", "recoveries": recoveries,
            "edition": row[13] or "",
            "quality_shortfall": _decode_object(row[15])[0],
            "tracks_missing": _tracks_missing(row[16], row[4], row[5]),
            "recovery_unreadable": recovery_unreadable,
            "single_unreadable": single_unreadable,
        })
    return result


def recovery_history(*, attention_only: bool = False,
                     strict: bool = False) -> list[dict]:
    """Unresolved recoveries, optionally limited to terminal attention."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            if strict:
                raise RuntimeError("saved recovery history is unavailable")
            return []
        try:
            attention_clause = (
                f"AND {_TERMINAL_SQL} AND attention != '' "
                if attention_only else ""
            )
            rows = conn.execute(
                "SELECT id, title, artist, album_id, status, error, summary, "
                "execute_kind, execute_args, created_at, finished_at, "
                "attention, recoveries, edition, single "
                "FROM jobs "
                "WHERE COALESCE(recoveries, '[]') != '[]' "
                f"{attention_clause}"
                "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC"
            ).fetchall()
        except sqlite3.Error as exc:
            if strict:
                raise
            _log.debug("recovery_history failed: %s", exc)
            return []
    result = []
    for row in rows:
        recoveries, recovery_unreadable = _decode_recoveries(row[12])
        if not recoveries and not recovery_unreadable:
            continue
        execute_args, execute_args_unreadable = _decode_execute_args(row[8])
        _single, single_unreadable = _decode_single(row[14])
        result.append({
            "id": row[0], "title": row[1] or "", "artist": row[2] or "",
            "album_id": row[3] or "", "status": row[4], "error": row[5],
            "summary": row[6] or "", "execute_kind": row[7] or "",
            "interrupted_by_restart": interrupted_by_restart(
                row[4], row[6], row[5]),
            "execute_args": execute_args,
            "execute_args_unreadable": execute_args_unreadable,
            "created_at": _decode_timestamp(row[9]),
            "finished_at": _decode_timestamp(row[10]),
            "attention": row[11] or "", "recoveries": recoveries,
            "edition": row[13] or "",
            "recovery_unreadable": recovery_unreadable,
            "single_unreadable": single_unreadable,
        })
    return result


def last_finished_at(execute_kind: str) -> Optional[float]:
    """When a job of this ``execute_kind`` last finished cleanly, or None. Backs
    the per-tool "Last scan …" freshness line. Only DONE counts (a failed/cancelled
    run isn't a completed pass), and the archive outlives the in-memory cap, so the
    line survives restarts. Reads the durable jobs.db rather than a separate stamp
    file so there's nothing extra to keep in sync."""
    if not execute_kind:
        return None
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT MAX(finished_at) FROM jobs "
                "WHERE status='done' AND execute_kind=? AND finished_at IS NOT NULL",
                (execute_kind,),
            ).fetchone()
        except sqlite3.Error:
            return None
    return _decode_timestamp(row[0]) if row else None


def acknowledge_listed_attention(job_ids) -> int:
    """Clear the attention markers on the jobs just listed to the reader.

    Opening one job already acknowledges it; the list that gathers them did
    not. Recovery and catalog markers stand for outstanding work rather than
    unread news, so they are left as the single-job path leaves them.
    """
    ids = [str(job_id) for job_id in job_ids if job_id]
    if not ids:
        return 0
    with _lock:
        conn = _get_conn()
        if conn is None:
            return 0
        try:
            with conn:
                return conn.execute(
                    f"UPDATE jobs SET attention='' WHERE {_TERMINAL_SQL} "
                    "AND attention NOT IN ('', 'recovery', 'catalog') "
                    f"AND id IN ({','.join('?' * len(ids))})",
                    ids,
                ).rowcount
        except sqlite3.Error as exc:
            _note_write_failure("acknowledge listed attention", exc)
            return 0


def attention_count() -> int:
    """How many terminal jobs still need attention in History."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return 0
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {_TERMINAL_SQL} "
                "AND attention != ''"
            ).fetchone()[0]
        except sqlite3.Error:
            return 0


def has_active_single_undo(single) -> bool:
    """Whether a saved single-track result still has an action to finish."""
    cleanup = single.get("catalog_cleanup") if type(single) is dict else None
    cleanup_pending = (
        type(cleanup) is dict
        and cleanup.get("pending") is True
        and isinstance(cleanup.get("path"), str)
        and bool(cleanup["path"])
    )
    return (
        type(single) is dict
        and type(single.get("owned_path")) is dict
        and bool(single["owned_path"])
        and (single.get("removed") is not True or cleanup_pending)
    )


def other_album_grabs(album_id, job_id) -> list[dict]:
    """The saved Undo records of the album's other track grabs."""
    if not album_id:
        return []
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT single FROM jobs WHERE id != ?", (job_id,)
            ).fetchall()
        except sqlite3.Error:
            return []
    grabs = []
    for (raw_single,) in rows:
        try:
            single = json.loads(raw_single or "{}")
        except (TypeError, ValueError):
            continue
        if type(single) is dict and single.get("album_id") == album_id:
            grabs.append(single)
    return grabs


def single_mark_still_used(album_id, job_id) -> bool:
    """Whether another saved track grab from the album still holds its
    single mark and has its track in the library."""
    return any(
        single.get("marked") is True
        and single.get("removed") is not True
        and has_active_single_undo(single)
        for single in other_album_grabs(album_id, job_id))


def clear_history() -> bool:
    """Delete ordinary finished jobs; retain recovery and Undo state."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            rows = conn.execute(
                f"SELECT id, recoveries, single FROM jobs WHERE {_TERMINAL_SQL}"
            ).fetchall()
            removable = []
            for job_id, raw_recoveries, raw_single in rows:
                if (raw_recoveries or "[]") != "[]":
                    continue
                try:
                    single = json.loads(raw_single or "{}")
                except (TypeError, ValueError):
                    continue
                if type(single) is not dict or has_active_single_undo(single):
                    continue
                removable.append((job_id,))
            conn.executemany("DELETE FROM jobs WHERE id=?", removable)
            conn.execute(
                "DELETE FROM durable_job_completions "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=durable_job_completions.job_id)"
            )
            _delete_rows(conn, (job_id for (job_id,) in removable))
            conn.commit()
            for (job_id,) in removable:
                _saved_rows.pop(job_id, None)
            return True
        except sqlite3.Error as e:
            _rollback_failed_write(conn)
            _log.debug("clear_history failed: %s", e)
            return False


def _load_all_rows(conn):
    rows = conn.execute(
        "SELECT id, title, artist, album_id, kind, status, phase, "
        # A finished job never reopens its rows, so its text is not read
        # back. A run cut short returns its picks to the review.
        "CASE WHEN status='awaiting_review' OR (status IN "
        f"('pending', 'running') AND execute_kind IN ({_REVIEW_PICK_MARKS})) "
        "THEN candidates END, "
        "error, summary, review_verb, execute_kind, "
        "execute_args, created_at, finished_at, single, attention, "
        "recoveries, log_lines, quality_shortfall, edition, tracks_missing "
        "FROM jobs ORDER BY created_at",
        REVIEW_PICK_KINDS,
    ).fetchall()
    stored = {}
    for r in rows:
        if r[7] == "[]":
            stored[r[0]], saved = _read_rows(conn, r[0])
            _remember(r[0], saved)
    return rows, stored


def load_all() -> list[dict]:
    """Read saved jobs, leaving logs encoded until needed. Raise on failure."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            raise RuntimeError("saved jobs are unavailable")
        try:
            rows, stored = _load_all_rows(conn)
        except sqlite3.Error as e:
            _note_damage(e)
            raise
    out = []
    for r in rows:
        try:
            job = _decode_row(r, decode_logs=False,
                              stored=stored.get(r[0], _NO_ROWS))
            if job is None:
                raise ValueError("recovery payload is invalid")
        except (ValueError, TypeError) as e:
            _log.info("skipping unreadable jobs.db row %s: %s", r[0], e)
            continue
        out.append(job)
    return out


def _reset_for_tests() -> None:
    """Test-only hook: drop the on-disk db so a fresh test starts clean."""
    global _disabled, _conn, _schema_ready, _admission_ready
    global _warned_write_failure, _damaged
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    _disabled = False
    _schema_ready = False
    _admission_ready = False
    _warned_write_failure = False
    _damaged = False
    _saved_rows.clear()
    p = _path()
    for q in (p, p.with_suffix(".db-wal"), p.with_suffix(".db-shm")):
        try:
            q.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
