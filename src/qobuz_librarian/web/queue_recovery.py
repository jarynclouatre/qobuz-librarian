"""What the durable queue holds after a restart, and whether work may start."""
import logging
import os
import threading
import time

from qobuz_librarian import completion
from qobuz_librarian.completion import CompletionOrigin, CompletionOriginKind, RecoveryOwner
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue import startup_recovery
from qobuz_librarian.web import job_persistence, runtime
from qobuz_librarian.web import jobs as job_mgr

_log = logging.getLogger("qobuz_librarian")


# Exact result from the most recent run-lock acquisition. Startup recovery is
# inspection/reconciliation only; it never starts a download or import.
_STARTUP_RECOVERY_RESULT = None


# True while an authoritative refresh is running or after it raised.
_STARTUP_RECOVERY_UNKNOWN = False


_STARTUP_RECOVERY_REFRESHING = False


# Whether the running refresh began after a read had failed. Downloads and
# scans were paused before it began, so pages do not wait for it.
_STARTUP_RECOVERY_REREADING = False


_STARTUP_RECOVERY_LOCK = threading.RLock()


# What settling did with each web job's saved queue entry, by job id, until
# the job itself takes it.
_SETTLED_DOWNLOADS: dict = {}


def _recover_startup_queue(authority, *, settle=False):
    # COMPLETE/RESOLVING recovery retires its queue proof only after the exact
    # original Web job has durably acknowledged it.
    job_persistence.init()

    def _acknowledge(
        origin,
        owner,
        *,
        album_id,
        completion_hash,
        planned,
        post_dir,
    ):

        planned_album = (
            planned.get("album") if isinstance(planned, dict) else None
        )
        if (
            type(origin) is not CompletionOrigin
            or type(owner) is not RecoveryOwner
            or completion.normalise_album_id(
                planned_album.get("id")
                if isinstance(planned_album, dict)
                else None
            )
            != album_id
            or type(post_dir) is not str
            or not os.path.isabs(post_dir)
            or "\x00" in post_dir
        ):
            return False
        if origin.kind is CompletionOriginKind.CLI:
            return origin.reference == "download-queue"
        if (
            origin.kind is not CompletionOriginKind.WEB_JOB
            or not origin.reference
        ):
            return False
        return job_persistence.acknowledge_durable_completion(
            origin.reference,
            owner,
            album_id=album_id,
            completion_hash=completion_hash,
        )

    return startup_recovery.recover_startup_state(
        authority=authority,
        acknowledge_completion=_acknowledge,
        settle=settle,
    )


def _record_startup_recovery(authority, *, settle=False):
    global _STARTUP_RECOVERY_RESULT, _STARTUP_RECOVERY_UNKNOWN
    global _STARTUP_RECOVERY_REFRESHING, _STARTUP_RECOVERY_REREADING
    with _STARTUP_RECOVERY_LOCK:
        _STARTUP_RECOVERY_REREADING = _recovery_read_failed()
        _STARTUP_RECOVERY_UNKNOWN = True
        _STARTUP_RECOVERY_REFRESHING = True
        try:
            result = _recover_startup_queue(authority, settle=settle)
        finally:
            _STARTUP_RECOVERY_REFRESHING = False
        prefix = "web-job:"
        for settled in getattr(result, "settled", ()):
            if settled.mode.startswith(prefix):
                _SETTLED_DOWNLOADS[settled.mode[len(prefix):]] = settled
        _STARTUP_RECOVERY_RESULT = result
        _STARTUP_RECOVERY_UNKNOWN = False
        return _STARTUP_RECOVERY_RESULT


def settled_downloads() -> dict:
    """What settling did with each web job's saved queue entry, by job id."""
    with _STARTUP_RECOVERY_LOCK:
        return dict(_SETTLED_DOWNLOADS)


def take_settled_downloads(job_ids=None) -> dict:
    """Remove and return the settled entries of these jobs, or of all."""
    with _STARTUP_RECOVERY_LOCK:
        taken = {
            job_id: _SETTLED_DOWNLOADS.pop(job_id)
            for job_id in tuple(_SETTLED_DOWNLOADS if job_ids is None else job_ids)
            if job_id in _SETTLED_DOWNLOADS
        }
    return taken


def _unsettled_web_job_ids() -> frozenset:
    """The web jobs whose saved queue entry is still waiting on recovery."""
    prefix = "web-job:"
    return frozenset(
        item.mode[len(prefix):]
        for item in getattr(_STARTUP_RECOVERY_RESULT, "items", ())
        if item.mode.startswith(prefix)
    )


def _journaled_web_job_ids() -> frozenset | None:
    """The web jobs a saved queue names, or None when one cannot be read."""
    loads = queue_state.list_queue_journals()
    if any(loaded.status is queue_state.QueueLoadStatus.BLOCKED for loaded in loads):
        return None
    prefix = "web-job:"
    return frozenset(
        loaded.journal.mode[len(prefix):]
        for loaded in loads
        if loaded.journal is not None and loaded.journal.mode.startswith(prefix)
    )


def _await_recovery_refresh(timeout: float = 1.0) -> None:
    """Let a refresh running in another thread finish before its outcome is
    read. One follows every download and is quick; a page drawn in the middle
    of it reported a failed read. One that reads again after a failed read
    can settle as much as a start does, and is not waited for."""
    if (_STARTUP_RECOVERY_REFRESHING and not _STARTUP_RECOVERY_REREADING
            and _STARTUP_RECOVERY_LOCK.acquire(timeout=timeout)):
        _STARTUP_RECOVERY_LOCK.release()


def _recovery_read_failed() -> bool:
    """Whether the last read of the saved recovery state failed, rather than
    finding work that waits."""
    if _STARTUP_RECOVERY_REFRESHING:
        return False
    return _STARTUP_RECOVERY_UNKNOWN or getattr(
        _STARTUP_RECOVERY_RESULT, "reason", None
    ) == "startup-recovery-unsettled"


def _startup_recovery_status_value() -> str | None:
    if _STARTUP_RECOVERY_UNKNOWN:
        return "attention_required"
    return _recovery_status_value(_STARTUP_RECOVERY_RESULT)


def _post_import_relocation_recovery():
    if (
        _STARTUP_RECOVERY_UNKNOWN
        or getattr(_STARTUP_RECOVERY_RESULT, "reason", None)
        != "post-import-relocation-unsettled"
    ):
        return None
    return getattr(_STARTUP_RECOVERY_RESULT, "post_import_relocation", None)


def _unreadable_queue_paths():
    if (
        _STARTUP_RECOVERY_UNKNOWN
        or getattr(_STARTUP_RECOVERY_RESULT, "reason", None)
        != "queue-namespace-blocked"
    ):
        return None
    return getattr(_STARTUP_RECOVERY_RESULT, "paths", ())


def _recovery_status_value(result) -> str | None:
    status = getattr(result, "status", None)
    return getattr(status, "value", None)


# Reviews whose approval downloads albums. The terminal holds its repairs for
# a saved queue too.
_DOWNLOAD_REVIEW_KINDS = frozenset(
    {"library", "new_releases", "collection_restore", "repair"})


def _terminal_queue_waiting() -> int:
    """How many albums wait in a terminal queue that never started, or 0.

    The terminal holds only its downloads for such a queue, so the web holds
    only its own.
    """
    if _startup_recovery_status_value() != "resume_required":
        return 0
    items = getattr(_STARTUP_RECOVERY_RESULT, "items", ())
    if items and all(
        not item.mode.startswith("web-job:")
        and item.phase is queue_state.QueuePhase.PENDING
        for item in items
    ):
        return len(items)
    return 0


def _downloads_albums(job) -> bool:
    return job is not None and (
        bool(job.album_id) or job.execute_kind in _DOWNLOAD_REVIEW_KINDS)


def _startup_recovery_album_label() -> str:
    """Name the album whose interrupted download holds everything else."""
    blocked = [
        item for item in getattr(_STARTUP_RECOVERY_RESULT, "items", ())
        if item.phase is queue_state.QueuePhase.BLOCKED
    ]
    if len(blocked) != 1:
        return ""
    try:
        loaded = queue_state.load_queue_journal(blocked[0].operation_id)
    except (OSError, ValueError):
        return ""
    queued = next(
        (
            item for item in getattr(loaded.journal, "items", ())
            if item.item_id == blocked[0].item_id
        ),
        None,
    )
    planned = getattr(queued, "planned", None) or {}
    album = planned.get("album")
    if not isinstance(album, dict):
        return str(planned.get("label") or "")
    title = str(album.get("title") or "").strip()
    artist = str(((album.get("artist") or {}) or {}).get("name") or "").strip()
    if title and artist:
        return f"{artist} · {title}"
    return title or str(planned.get("label") or "")


def _staging_entry_allowed(job) -> bool:
    """Refresh recovery under the staging mutex before any mutation begins."""
    if not runtime._run_lock_intact():
        return False
    try:
        _record_startup_recovery(runtime._RUN_LOCK_HANDLE)
    except Exception as exc:
        _log.warning(
            "couldn't verify durable recovery at the staging boundary: %s",
            exc,
        )
        return False
    return _startup_recovery_status_value() == "clear" or (
        bool(_terminal_queue_waiting()) and not _downloads_albums(job))


def _durable_completion_status(job) -> bool | None:
    """Return the exact job/album acknowledgement state, or None on failure."""
    album_id = completion.normalise_album_id(getattr(job, "album_id", None))
    created_at = getattr(job, "created_at", None)
    if album_id is None or type(created_at) not in (int, float):
        return False
    return job_persistence.durable_completion_acknowledged(
        job.id,
        job_created_at=created_at,
        album_id=album_id,
    )


def _reconcile_acknowledged_job(job, summary: str | None = None) -> bool:
    """Make an externally completed exact job terminal and non-retryable."""
    with job._lock:
        job.status = job_mgr.JobStatus.DONE
        job.phase = ""
        job.error = None
        job.summary = job.summary or summary or (
            "Download completed before the restart."
        )
        job.attention = ""
        job.cancel_requested = False
        job.finished_at = job.finished_at or time.time()
    return job_persistence.persist(job)
