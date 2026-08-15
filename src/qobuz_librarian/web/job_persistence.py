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
import copy
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Optional

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import RecoveryOwner, normalise_album_id

_log = logging.getLogger("qobuz_librarian")
_lock = threading.Lock()
_disabled = False
_conn: Optional[sqlite3.Connection] = None
_schema_ready = False
_admission_ready = False
# The db opened fine but a write later failed (typically a full disk).
_warned_write_failure = False


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


def _valid_repair_recovery(record) -> bool:
    return (
        type(record) is dict
        and set(record) == _RECOVERY_FIELDS
        and record.get("version") == 1
        and record.get("kind") == "repair-backup"
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
        or any(not _valid_repair_recovery(record) for record in recoveries)
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


def _get_conn() -> Optional[sqlite3.Connection]:
    """Return the persistent WAL connection, opening it on first call.

    Returns None and disables further attempts when the volume isn't
    writable. The in-memory registry is still correct; the user just
    forgoes restart durability rather than seeing a stream of OSError
    on every status change.
    """
    global _disabled, _conn, _schema_ready, _admission_ready
    if _disabled:
        return None
    if _conn is not None:
        return _conn
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_path()), timeout=5.0,
                                check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
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
    edition       TEXT NOT NULL DEFAULT ''
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

_POST_IMPORT_RELOCATION_HANDOFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS post_import_relocation_handoffs (
    job_id         TEXT NOT NULL,
    operation_id   TEXT NOT NULL UNIQUE,
    job_created_at REAL NOT NULL,
    album_id       TEXT NOT NULL,
    handoff_hash   TEXT NOT NULL,
    acknowledged_at REAL NOT NULL
)
"""


_SCHEMA_VERSION = 7


def init() -> None:
    """Create the schema (and run additive migrations). Safe to call repeatedly."""
    global _schema_ready, _admission_ready
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(_SCHEMA)
            conn.execute(_DURABLE_COMPLETION_SCHEMA)
            conn.execute(_POST_IMPORT_RELOCATION_HANDOFF_SCHEMA)
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
            ):
                if column not in cols:
                    conn.execute(
                        f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != _SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
            _schema_ready = True
            _admission_ready = True
        except sqlite3.Error as e:
            _rollback_failed_write(conn)
            _schema_ready = False
            _admission_ready = False
            # A transient/locked/full/corrupt jobs.db here would otherwise
            # propagate out of restore_jobs() into the caller's broad
            # "couldn't restore prior jobs; starting fresh" handler, masking
            # a recoverable condition and then leaving every later persist()
            # silently non- durable.
            _log.warning("job persistence schema/migration failed; running "
                         "without crash durability until the volume recovers "
                         "and the app restarts: %s", e)


def ready_for_admission() -> bool:
    """Whether the schema opened successfully and durable jobs can be saved."""
    with _lock:
        return _schema_ready and _admission_ready and not _disabled


_PERSIST_SQL = (
    "INSERT OR REPLACE INTO jobs "
    "(id, title, artist, album_id, kind, status, phase, candidates, "
    " error, summary, review_verb, execute_kind, execute_args, "
    " created_at, finished_at, single, attention, recoveries, log_lines, "
    " quality_shortfall, edition) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_CURRENT_JOB_SINGLE = object()
# jobs.py imports this module, so the names live here rather than reaching
# back for its TERMINAL set; _TERMINAL_SQL is built from the same tuple.
_TERMINAL_STATUSES = ("done", "failed", "canceled")


def _job_values(job, *, single=_CURRENT_JOB_SINGLE):
    """Serialise one job while its lock is held, or return None."""
    try:
        candidates_json = json.dumps(job.candidates or [], default=str)
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
        # Only a finished job's log is stored: a running one is on screen live,
        # and re-serialising a growing blob on every snapshot is waste.
        status_value = (
            job.status.value if hasattr(job.status, "value") else str(job.status)
        )
        log_json = json.dumps(
            job.persisted_log_lines_locked()
            if status_value in _TERMINAL_STATUSES else [],
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
    return (
        job.id, job.title or "", job.artist or "",
        job.album_id or "", job.kind or "download",
        job.status.value if hasattr(job.status, "value") else str(job.status),
        job.phase or "",
        candidates_json,
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
    )


def _persist_locked(job, *, admission=False) -> bool:
    """Write one ordinary snapshot while the caller holds ``job._lock``."""
    global _admission_ready
    values = _job_values(job)
    if values is None:
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            if admission:
                _admission_ready = False
            return False
        try:
            conn.execute(_PERSIST_SQL, values)
            conn.commit()
            if admission:
                _admission_ready = True
            return True
        except sqlite3.Error as e:
            _rollback_failed_write(conn)
            if admission:
                _admission_ready = False
            _note_write_failure(f"persist {job.id}", e)
            return False


def persist(job) -> bool:
    """Write the job's current state to disk. Return whether it reached disk."""
    # Keep the job lock through both the preservation decision and the
    # database commit.
    with job._lock:
        if getattr(job, "_preserve_persisted_single", False) is True:
            return _persist_preserving_single_locked(job)
        return _persist_locked(job)


def persist_review_mutation(job, mutation) -> tuple[bool, object]:
    """Save one candidate-list mutation or restore its exact prior state."""
    with job._review_action_lock, job._lock:
        had_saved_signature = hasattr(job, "_saved_review_signature")
        previous = (
            copy.deepcopy(job.candidates),
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
        if _persist_locked(job):
            return True, result
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


def _persist_preserving_single_locked(job, *, admission=False) -> bool:
    """Save while preserving Undo; the caller holds ``job._lock``."""
    global _admission_ready
    values = _job_values(job)
    if values is None:
        return False
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
            if type(durable_single) is dict:
                job.single = durable_single
                job.__dict__.pop("_single_undo_unavailable", None)
            else:
                job.single = {}
                job._single_undo_unavailable = True
            if admission:
                _admission_ready = True
            return True
        except sqlite3.Error as exc:
            _rollback_failed_write(conn)
            if admission:
                _admission_ready = False
            _note_write_failure(
                f"persist {job.id} while preserving its Undo record",
                exc,
            )
            job._single_undo_unavailable = True
            return False


def persist_preserving_single(job) -> bool:
    """Save job state without changing an indeterminate durable Undo record."""
    with job._lock:
        return _persist_preserving_single_locked(job)


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
        values = [_job_values(other) for other in ordered]
        if any(value is None for value in values):
            return False
        with _lock:
            conn = _get_conn()
            if conn is None:
                _admission_ready = False
                return False
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(_PERSIST_SQL, values)
                conn.commit()
                for other in ordered:
                    other._durability_required = True
                _admission_ready = True
                return True
            except sqlite3.Error as e:
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
    value = _job_values(job)
    if value is None or parked_job is job:
        return False
    with _lock:
        conn = _get_conn()
        if conn is None:
            _admission_ready = False
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_PERSIST_SQL, value)
            conn.execute(
                "DELETE FROM durable_job_completions WHERE job_id=?",
                (parked_job.id,),
            )
            conn.execute(
                "DELETE FROM post_import_relocation_handoffs WHERE job_id=?",
                (parked_job.id,),
            )
            conn.execute("DELETE FROM jobs WHERE id=?", (parked_job.id,))
            conn.commit()
            job._durability_required = True
            _admission_ready = True
            return True
        except sqlite3.Error as exc:
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
    """Durably retire exact Repair backups confirmed missing by the caller."""
    if not callable(is_missing):
        return False
    with job._lock:
        recoveries = list(getattr(job, "recoveries", []) or [])
        if (
            getattr(job, "attention", "") != "recovery"
            or not recoveries
            or any(not _valid_repair_recovery(record) for record in recoveries)
        ):
            return False
        try:
            if any(not is_missing(record) for record in recoveries):
                return False
        except Exception as exc:
            _log.debug("couldn't verify missing Repair recovery: %s", exc)
            return False

        resolution = (
            "The missing Repair backup was acknowledged; no kept originals "
            "remain."
            if len(recoveries) == 1
            else "The missing Repair backups were acknowledged; no kept "
                 "originals remain."
        )
        acknowledgement_lines = []
        for record in recoveries:
            line = (
                "Acknowledged missing Repair recovery folder: "
                f"{record['location']}"
            )
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
            return None
        try:
            return conn.execute(
                "SELECT 1 FROM durable_job_completions "
                "WHERE job_id=? AND job_created_at=? AND album_id=? LIMIT 1",
                (job_id, job_created_at, album_id),
            ).fetchone() is not None
        except sqlite3.Error as exc:
            _log.debug(
                "couldn't inspect durable completion for %s: %s",
                job_id,
                exc,
            )
            return None


def _relocation_handoff_consumer(job) -> dict | None:
    try:
        job_id = job.id
        created_at = job.created_at
        album_id = job.album_id
    except AttributeError:
        return None
    if (
        type(job_id) is not str
        or not job_id
        or "\x00" in job_id
        or type(created_at) not in (int, float)
        or not math.isfinite(created_at)
        or normalise_album_id(album_id) != album_id
    ):
        return None
    return {
        "kind": "web-single",
        "job_id": job_id,
        "job_created_at": created_at,
        "album_id": album_id,
    }


def _valid_relocation_handoff_consumer(value) -> bool:
    return (
        type(value) is dict
        and set(value) == {
            "kind", "job_id", "job_created_at", "album_id",
        }
        and value.get("kind") == "web-single"
        and type(value.get("job_id")) is str
        and bool(value["job_id"])
        and "\x00" not in value["job_id"]
        and type(value.get("job_created_at")) in (int, float)
        and math.isfinite(value["job_created_at"])
        and normalise_album_id(value.get("album_id")) == value.get("album_id")
    )


def _relocation_handoff_hash(consumer, payload) -> str | None:
    if not _valid_relocation_handoff_consumer(consumer) or type(payload) is not dict:
        return None
    try:
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if json.loads(encoded_payload) != payload:
            return None
        encoded = json.dumps(
            {"consumer": consumer, "payload": payload},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def persist_post_import_relocation_handoff(
    job,
    *,
    operation_id: str,
    handoff_hash: str,
    single: dict,
) -> bool:
    """Atomically save one Web-single Undo snapshot and relocation handoff."""
    if (
        type(operation_id) is not str
        or re.fullmatch(r"[0-9a-f]{64}", operation_id) is None
        or type(handoff_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", handoff_hash) is None
        or type(single) is not dict
    ):
        return False
    try:
        single_snapshot = json.loads(json.dumps(
            single,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
    except (TypeError, ValueError, UnicodeError):
        return False
    if single_snapshot != single:
        return False
    with job._lock:
        consumer = _relocation_handoff_consumer(job)
        if consumer is None:
            return False
        if _relocation_handoff_hash(consumer, single_snapshot) != handoff_hash:
            return False
        values = _job_values(job, single=single_snapshot)
        if values is None:
            return False
        with _lock:
            conn = _get_conn()
            if conn is None:
                return False
            try:
                conn.execute("BEGIN IMMEDIATE")
                job_row = conn.execute(
                    "SELECT created_at, album_id, single FROM jobs WHERE id=?",
                    (consumer["job_id"],),
                ).fetchone()
                if (
                    job_row is None
                    or job_row[0] != consumer["job_created_at"]
                    or job_row[1] != consumer["album_id"]
                ):
                    conn.rollback()
                    return False
                existing = conn.execute(
                    "SELECT job_id, job_created_at, album_id, handoff_hash "
                    "FROM post_import_relocation_handoffs "
                    "WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                expected = (
                    consumer["job_id"],
                    consumer["job_created_at"],
                    consumer["album_id"],
                    handoff_hash,
                )
                if existing is not None:
                    try:
                        saved_single = json.loads(job_row[2])
                    except (TypeError, ValueError):
                        conn.rollback()
                        return False
                    if existing != expected or saved_single != single_snapshot:
                        conn.rollback()
                        return False
                conn.execute(_PERSIST_SQL, values)
                if existing is None:
                    conn.execute(
                        "INSERT INTO post_import_relocation_handoffs "
                        "(job_id, operation_id, job_created_at, album_id, "
                        "handoff_hash, acknowledged_at) VALUES (?,?,?,?,?,?)",
                        (
                            consumer["job_id"],
                            operation_id,
                            consumer["job_created_at"],
                            consumer["album_id"],
                            handoff_hash,
                            time.time(),
                        ),
                    )
                conn.commit()
                job.single = single_snapshot
                job.__dict__.pop("_single_undo_unavailable", None)
                return True
            except sqlite3.Error as exc:
                _rollback_failed_write(conn)
                _note_write_failure(
                    f"persist relocation handoff for {consumer['job_id']}",
                    exc,
                )
                return False


def post_import_relocation_handoff_persisted(
    operation_id: str,
    handoff: dict,
) -> bool | None:
    """Verify an exact sealed handoff; only definite absence permits rollback."""
    if (
        type(operation_id) is not str
        or re.fullmatch(r"[0-9a-f]{64}", operation_id) is None
        or type(handoff) is not dict
        or set(handoff) != {"consumer", "hash"}
        or not _valid_relocation_handoff_consumer(handoff.get("consumer"))
        or type(handoff.get("hash")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", handoff["hash"]) is None
    ):
        return None
    consumer = handoff["consumer"]
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT h.job_id, h.job_created_at, h.album_id, "
                "h.handoff_hash, j.created_at, j.album_id, j.single "
                "FROM post_import_relocation_handoffs AS h "
                "LEFT JOIN jobs AS j ON j.id=h.job_id "
                "WHERE h.operation_id=?",
                (operation_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            _log.debug(
                "couldn't inspect relocation handoff %s: %s",
                operation_id,
                exc,
            )
            return None
    if row is None:
        return False
    if (
        row[:4] != (
            consumer["job_id"],
            consumer["job_created_at"],
            consumer["album_id"],
            handoff["hash"],
        )
        or row[4] != consumer["job_created_at"]
        or row[5] != consumer["album_id"]
    ):
        return None
    try:
        single = json.loads(row[6])
    except (TypeError, ValueError):
        return None
    if _relocation_handoff_hash(consumer, single) != handoff["hash"]:
        return None
    return True


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
            or any(not _valid_repair_recovery(record) for record in records)
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
                        not _valid_repair_recovery(record)
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
            conn.execute(
                "DELETE FROM post_import_relocation_handoffs WHERE job_id=?",
                (job_id,),
            )
            conn.commit()
        except sqlite3.Error as exc:
            _rollback_failed_write(conn)
            _log.debug("delete job %s failed: %s", job_id, exc)


def load_one(job_id: str) -> Optional[dict]:
    """Return one persisted job by id (the same shape ``load_all`` yields
    per row), or None if it isn't on disk. Used by the read-only "this
    job was archived" page so a registry eviction doesn't make a job's
    history disappear from view."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT id, title, artist, album_id, kind, status, phase, "
                "candidates, error, summary, review_verb, execute_kind, "
                "execute_args, created_at, finished_at, single, attention, "
                "recoveries, log_lines, quality_shortfall, edition "
                "FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        except sqlite3.Error as e:
            _log.debug("load_one failed for %s: %s", job_id, e)
            return None
    if row is None:
        return None
    execute_args, execute_args_unreadable = _decode_execute_args(row[12])
    try:
        return {
            "id": row[0], "title": row[1], "artist": row[2], "album_id": row[3],
            "kind": row[4], "status": row[5], "phase": row[6],
            "candidates": json.loads(row[7] or "[]"),
            "error": row[8], "summary": row[9] or "", "review_verb": row[10] or "Download",
            "execute_kind": row[11] or "",
            "execute_args": execute_args,
            "execute_args_unreadable": execute_args_unreadable,
            "created_at": row[13], "finished_at": row[14],
            "single": json.loads(row[15] or "{}"),
            "attention": row[16] or "",
            "recoveries": json.loads(row[17] or "[]"),
            "log_lines": json.loads(row[18] or "[]"),
            "quality_shortfall": json.loads(row[19] or "{}"),
            "edition": row[20] or "",
        }
    except (ValueError, TypeError):
        return None


def prune_finished(keep: int, *, retain_job_id: str | None = None) -> None:
    """Drop the oldest terminal rows past ``keep`` so the archive doesn't
    grow without bound. Non-terminal jobs, unresolved Repair recoveries, and
    active single-track Undo records are never pruned here. Best-effort: a
    sqlite error logs and bows out."""
    if keep <= 0:
        return
    retained = (
        retain_job_id
        if isinstance(retain_job_id, str) and retain_job_id
        else None
    )
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
                if job_id == retained:
                    continue
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
            conn.execute(
                "DELETE FROM post_import_relocation_handoffs "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=post_import_relocation_handoffs.job_id)"
            )
            conn.commit()
        except sqlite3.Error as e:
            _rollback_failed_write(conn)
            _log.debug("prune_finished(%d) failed: %s", keep, e)


_TERMINAL_SQL = "status IN ({})".format(
    ", ".join(f"'{name}'" for name in _TERMINAL_STATUSES))
# The History view also lists parked reviews (with an Open link back to their
# surface), but clearing and pruning must never touch them.
_HISTORY_SQL = "status IN ('done', 'failed', 'canceled', 'awaiting_review')"


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
    History view. Lighter than ``load_all`` (no candidates/args): just what a
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
                "attention, recoveries, edition "
                "FROM jobs "
                f"WHERE {_HISTORY_SQL} {clause}"
                "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        except sqlite3.Error as e:
            _log.debug("history_page failed: %s", e)
            return []
    result = []
    for row in rows:
        recoveries, recovery_unreadable = _decode_recoveries(row[12])
        execute_args, execute_args_unreadable = _decode_execute_args(row[8])
        result.append({
            "id": row[0], "title": row[1] or "", "artist": row[2] or "",
            "album_id": row[3] or "", "status": row[4], "error": row[5],
            "summary": row[6] or "", "execute_kind": row[7] or "",
            "execute_args": execute_args,
            "execute_args_unreadable": execute_args_unreadable,
            "created_at": row[9], "finished_at": row[10],
            "attention": row[11] or "", "recoveries": recoveries,
            "edition": row[13] or "",
            "recovery_unreadable": recovery_unreadable,
        })
    return result


def recovery_history(*, attention_only: bool = False) -> list[dict]:
    """Unresolved recoveries, optionally limited to terminal attention."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            attention_clause = (
                f"AND {_TERMINAL_SQL} AND attention != '' "
                if attention_only else ""
            )
            rows = conn.execute(
                "SELECT id, title, artist, album_id, status, error, summary, "
                "execute_kind, execute_args, created_at, finished_at, "
                "attention, recoveries, edition "
                "FROM jobs "
                "WHERE COALESCE(recoveries, '[]') != '[]' "
                f"{attention_clause}"
                "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC"
            ).fetchall()
        except sqlite3.Error as exc:
            _log.debug("recovery_history failed: %s", exc)
            return []
    result = []
    for row in rows:
        recoveries, recovery_unreadable = _decode_recoveries(row[12])
        if not recoveries and not recovery_unreadable:
            continue
        execute_args, execute_args_unreadable = _decode_execute_args(row[8])
        result.append({
            "id": row[0], "title": row[1] or "", "artist": row[2] or "",
            "album_id": row[3] or "", "status": row[4], "error": row[5],
            "summary": row[6] or "", "execute_kind": row[7] or "",
            "execute_args": execute_args,
            "execute_args_unreadable": execute_args_unreadable,
            "created_at": row[9], "finished_at": row[10],
            "attention": row[11] or "", "recoveries": recoveries,
            "edition": row[13] or "",
            "recovery_unreadable": recovery_unreadable,
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
    return row[0] if row and row[0] is not None else None


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


def clear_history(*, retain_job_id: str | None = None) -> bool:
    """Delete ordinary finished jobs; retain recovery and Undo state."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return False
        try:
            retained = (
                retain_job_id
                if isinstance(retain_job_id, str) and retain_job_id
                else None
            )
            rows = conn.execute(
                f"SELECT id, recoveries, single FROM jobs WHERE {_TERMINAL_SQL}"
            ).fetchall()
            removable = []
            for job_id, raw_recoveries, raw_single in rows:
                if job_id == retained or (raw_recoveries or "[]") != "[]":
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
            conn.execute(
                "DELETE FROM post_import_relocation_handoffs "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id=post_import_relocation_handoffs.job_id)"
            )
            conn.commit()
            return True
        except sqlite3.Error as e:
            _rollback_failed_write(conn)
            _log.debug("clear_history failed: %s", e)
            return False


def load_all() -> list[dict]:
    """Return every persisted job as a plain dict; caller rehydrates into
    a Job. Returns [] when the db can't be opened."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT id, title, artist, album_id, kind, status, phase, "
                "candidates, error, summary, review_verb, execute_kind, "
                "execute_args, created_at, finished_at, single, attention, "
                "recoveries, log_lines, quality_shortfall, edition "
                "FROM jobs ORDER BY created_at"
            ).fetchall()
        except sqlite3.Error as e:
            _log.info("couldn't read jobs.db on startup (%s); starting fresh.", e)
            return []
    out = []
    for r in rows:
        try:
            recoveries, recovery_unreadable = _decode_recoveries(r[17])
            if recovery_unreadable:
                raise ValueError("recovery payload is invalid")
            execute_args, execute_args_unreadable = _decode_execute_args(r[12])
            # Only AWAITING_REVIEW jobs need their candidates on restore; all
            # other statuses are either rehydrated as live state (RUNNING →
            # FAILED) or displayed as history without candidates.
            candidates = json.loads(r[7] or "[]") if r[5] == "awaiting_review" else []
            out.append({
                "id": r[0], "title": r[1], "artist": r[2], "album_id": r[3],
                "kind": r[4], "status": r[5], "phase": r[6],
                "candidates": candidates,
                "error": r[8], "summary": r[9] or "", "review_verb": r[10] or "Download",
                "execute_kind": r[11] or "",
                "execute_args": execute_args,
                "execute_args_unreadable": execute_args_unreadable,
                "created_at": r[13], "finished_at": r[14],
                "single": json.loads(r[15] or "{}"),
                "attention": r[16] or "",
                "recoveries": recoveries,
                "log_lines": json.loads(r[18] or "[]"),
                "quality_shortfall": json.loads(r[19] or "{}"),
                "edition": r[20] or "",
            })
        except (ValueError, TypeError) as e:
            _log.info("skipping unreadable jobs.db row %s: %s", r[0], e)
    return out


def _reset_for_tests() -> None:
    """Test-only hook: drop the on-disk db so a fresh test starts clean."""
    global _disabled, _conn, _schema_ready, _admission_ready
    global _warned_write_failure
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
    p = _path()
    for q in (p, p.with_suffix(".db-wal"), p.with_suffix(".db-shm")):
        try:
            q.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
