"""The interrupted download a restart found in the durable queue, and its controls."""
import logging
import os
import threading
import time

from qobuz_librarian import completion
from qobuz_librarian.completion import CompletionOrigin, CompletionOriginKind, RecoveryOwner
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue import startup_recovery
from qobuz_librarian.queue.startup_recovery import (
    BlockedItemSettlementAction,
    BlockedItemSettlementStatus,
)
from qobuz_librarian.web import job_persistence, runtime
from qobuz_librarian.web import jobs as job_mgr

_log = logging.getLogger("qobuz_librarian")


# Exact result from the most recent run-lock acquisition. Startup recovery is
# inspection/reconciliation only; it never starts a download or import.
_STARTUP_RECOVERY_RESULT = None


# True while an authoritative refresh is running or after it raised.
_STARTUP_RECOVERY_UNKNOWN = False


_STARTUP_RECOVERY_REFRESHING = False


_STARTUP_RECOVERY_LOCK = threading.RLock()


def startup_recovery_result():
    return _STARTUP_RECOVERY_RESULT


def _recover_startup_queue(authority):
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
    )


def _record_startup_recovery(authority):
    global _STARTUP_RECOVERY_RESULT, _STARTUP_RECOVERY_UNKNOWN
    global _STARTUP_RECOVERY_REFRESHING
    with _STARTUP_RECOVERY_LOCK:
        _STARTUP_RECOVERY_UNKNOWN = True
        _STARTUP_RECOVERY_REFRESHING = True
        try:
            result = _recover_startup_queue(authority)
        finally:
            _STARTUP_RECOVERY_REFRESHING = False
        _STARTUP_RECOVERY_RESULT = result
        _STARTUP_RECOVERY_UNKNOWN = False
        job_mgr.set_durable_recovery_job_id(_startup_recovery_web_job_id())
        return _STARTUP_RECOVERY_RESULT


def _await_recovery_refresh(timeout: float = 1.0) -> None:
    """Let a refresh running in another thread finish before its outcome is
    read. One follows every download and is quick; a page drawn in the middle
    of it reported a failed read."""
    if _STARTUP_RECOVERY_REFRESHING and _STARTUP_RECOVERY_LOCK.acquire(
            timeout=timeout):
        _STARTUP_RECOVERY_LOCK.release()


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


def _startup_recovery_binding():
    """Load the one exact queue item behind the current recovery result."""
    if _startup_recovery_status_value() not in {
        "attention_required",
        "resume_required",
    }:
        return None
    items = getattr(_STARTUP_RECOVERY_RESULT, "items", ())
    if len(items) != 1:
        return None
    recovery_item = items[0]
    try:
        loaded = queue_state.load_queue_journal(recovery_item.operation_id)
        if (
            loaded.status is not queue_state.QueueLoadStatus.READY
            or loaded.journal is None
            or loaded.journal.operation_id != recovery_item.operation_id
            or loaded.journal.mode != recovery_item.mode
        ):
            return None
        matches = tuple(
            item
            for item in loaded.journal.items
            if item.item_id == recovery_item.item_id
        )
        if len(matches) != 1:
            return None
        queued_item = matches[0]
        if queued_item.phase is not recovery_item.phase:
            return None
        if queued_item.completion_input is None:
            if (
                queued_item.phase is queue_state.QueuePhase.PENDING
                and getattr(recovery_item.action, "value", None) == "pending"
                and not queued_item.recovery_references
                and queued_item.block_reason is None
                and queued_item.completion_evidence is None
            ):
                return recovery_item, loaded.journal, queued_item, None
            return None
        completion_input = completion.parse_completion_input_record(
            queued_item.completion_input,
            expected_owner=RecoveryOwner(
                recovery_item.operation_id,
                recovery_item.item_id,
            ),
        )
        if completion_input is None:
            return None

        planned_album = queued_item.planned.get("album")
        if completion.normalise_album_id(
            planned_album.get("id") if isinstance(planned_album, dict) else None
        ) != completion.normalise_album_id(completion_input.expectation.album_id):
            return None
        return recovery_item, loaded.journal, queued_item, completion_input.origin
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _startup_recovery_origin_value() -> str | None:
    """Read the exact saved completion origin when one has been frozen."""
    binding = _startup_recovery_binding()
    if binding is None or binding[3] is None:
        return None
    return binding[3].kind.value


def _startup_recovery_album_label() -> str:
    """Name the album whose interrupted download is holding everything else.

    The pause notice knew the pause existed but never what it was about, so a
    user reading it had no way to tell which download the terminal would offer
    to settle, or whether it was one they still wanted.
    """
    binding = _startup_recovery_binding()
    if binding is None:
        return ""
    planned = getattr(binding[2], "planned", None) or {}
    album = planned.get("album")
    if not isinstance(album, dict):
        return str(planned.get("label") or "")
    title = str(album.get("title") or "").strip()
    artist = str(((album.get("artist") or {}) or {}).get("name") or "").strip()
    if title and artist:
        return f"{artist} · {title}"
    return title or str(planned.get("label") or "")


def _terminal_recovery_offer():
    """The blocked terminal download the web may settle, or None.

    A download started in the terminal never became a web job, so nothing on
    these pages could act on it and the notice could only send the reader back
    to a terminal. The identity comes from the same binding the terminal
    settles against, so the web can only ever offer this on the exact block the
    terminal would have offered.
    """
    if _post_import_relocation_recovery() is not None:
        return None
    binding = startup_recovery.blocked_settlement_binding(
        _STARTUP_RECOVERY_RESULT)
    if binding is None:
        return None
    item, label, settleable = binding
    return {
        "operation_id": item.operation_id,
        "item_id": item.item_id,
        "album": _startup_recovery_album_label() or label,
        "imported": settleable == startup_recovery.SETTLEABLE_IMPORTED,
        "partial": settleable == startup_recovery.SETTLEABLE_PARTIAL,
    }


def _startup_recovery_web_job_id() -> str | None:
    binding = _startup_recovery_binding()
    if binding is None:
        return None
    recovery_item, _journal, queued_item, origin = binding
    mode = getattr(recovery_item, "mode", None)
    prefix = "web-job:"
    if not isinstance(mode, str) or not mode.startswith(prefix):
        return None
    job_id = mode[len(prefix):]
    if not job_id:
        return None
    try:
        row = job_persistence.load_one(job_id)
        planned_album = queued_item.planned.get("album")
        if (
            row is None
            or completion.normalise_album_id(row.get("album_id"))
            != completion.normalise_album_id(
                planned_album.get("id")
                if isinstance(planned_album, dict)
                else None
            )
        ):
            return None
    except (OSError, TypeError, ValueError):
        return None
    if origin is None:
        return job_id if getattr(queued_item.phase, "value", None) == "pending" else None
    if origin.kind.value != "web-job" or origin.reference != job_id:
        return None
    return job_id


def _durable_resume_allowed(job_id: str, *, refresh: bool = False) -> bool:
    if refresh:
        if not runtime._run_lock_intact():
            return False
        _record_startup_recovery(runtime._RUN_LOCK_HANDLE)
    return (
        isinstance(job_id, str)
        and job_id == _startup_recovery_web_job_id()
    )


def _durable_recovery_matches_job(job) -> bool:
    """Bind RESUME to one exact Web job and its one canonical album."""
    if type(job) is not job_mgr.Job or not _durable_resume_allowed(job.id):
        return False
    binding = _startup_recovery_binding()
    if binding is None:
        return False
    recovery_item, loaded_journal, queued_item, origin = binding
    try:
        if (
            loaded_journal.mode != f"web-job:{job.id}"
            or len(loaded_journal.items) != 1
        ):
            return False
        planned_album = queued_item.planned.get("album")
        planned_id = completion.normalise_album_id(
            planned_album.get("id") if isinstance(planned_album, dict) else None
        )
        job_id = completion.normalise_album_id(job.album_id)
        return (
            queued_item.item_id == recovery_item.item_id
            and planned_id is not None
            and planned_id == job_id
            and (
                origin is None
                or (
                    origin.kind.value == "web-job"
                    and origin.reference == job.id
                )
            )
            and runtime._run_lock_intact()
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _recovery_pause_is_another_download(job) -> bool:
    """True when the interrupted download holding everything up isn't this job.

    Its own Retry is the only control that settles it, so a Retry on any other
    album has nothing it can do about the pause and queues behind it instead.
    """
    if _startup_recovery_status_value() not in {
        "attention_required",
        "resume_required",
    }:
        return False
    if type(job) is not job_mgr.Job or getattr(job, "attention", "") == "recovery":
        return False
    if _STARTUP_RECOVERY_UNKNOWN or _post_import_relocation_recovery() is not None:
        return False
    if _startup_recovery_binding() is None:
        return False
    return _startup_recovery_web_job_id() != job.id


def _durable_recovery_planned(job):
    """Copy the validated saved plan for this exact Web resume, or refuse."""
    with _STARTUP_RECOVERY_LOCK:
        if (
            _startup_recovery_status_value() != "resume_required"
            or not _durable_recovery_matches_job(job)
        ):
            return None
        binding = _startup_recovery_binding()
        if binding is None:
            return None
        queued_item = binding[2]
        try:
            # Round-trip for a canonical copy, never a mutable reference
            # into cached recovery state.
            planned = queue_state._serialize_queue_item(
                queue_state._deserialize_queue_item(queued_item.planned)
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return planned if planned == queued_item.planned else None


def _settle_durable_web_recovery(job, action):
    """Settle only the exact blocked recovery owned by one durable Web job."""
    return _settle_blocked_recovery(action, job=job)


def _settle_blocked_recovery(action, *, job=None):
    """Settle the one blocked download, proving who owns it first.

    A Web job proves ownership by matching the saved queue entry exactly. A
    terminal download has no Web job to match, so the proof there is that no
    Web job claims it and the posted identity is still the blocked one. Both
    routes end in the same settlement call the terminal uses. A Web download
    still waiting on its own Retry has nothing staged, so giving it up only
    clears its saved queue entry, as the terminal does with a pending queue.
    """
    if type(action) is not BlockedItemSettlementAction:
        raise ValueError("a blocked-item settlement action is required")
    with _STARTUP_RECOVERY_LOCK:
        if not runtime._run_lock_intact():
            return False, "The run lock is unavailable."
        try:
            recovery = _record_startup_recovery(runtime._RUN_LOCK_HANDLE)
        except Exception:
            _log.exception(
                "couldn't check blocked recovery for job %s", getattr(job, "id", "?"))
            return False, "The saved recovery state could not be checked safely."
        # A job running again owns its saved queue entry.
        unstarted = (
            _recovery_status_value(recovery) == "resume_required"
            and action is BlockedItemSettlementAction.DISCARD
            and job is not None
            and job.status is job_mgr.JobStatus.FAILED
        )
        if _recovery_status_value(recovery) != "attention_required" and not unstarted:
            return False, "The blocked recovery does not match this exact download."
        if job is not None and not _durable_recovery_matches_job(job):
            return False, "The blocked recovery does not match this exact download."
        if job is None and _startup_recovery_web_job_id() is not None:
            return False, ("That download belongs to a job you can settle from "
                           "Queue or History.")
        binding = _startup_recovery_binding()
        if binding is None:
            return False, "The blocked recovery record could not be verified."
        recovery_item = binding[0]
        imported = startup_recovery.import_was_filed(
            recovery_item.operation_id, binding[2])
        try:
            if unstarted:
                settled = startup_recovery.discard_unstarted_item(
                    authority=runtime._RUN_LOCK_HANDLE,
                    operation_id=recovery_item.operation_id,
                    item_id=recovery_item.item_id,
                )
            else:
                settled = startup_recovery.settle_blocked_item(
                    authority=runtime._RUN_LOCK_HANDLE,
                    operation_id=recovery_item.operation_id,
                    item_id=recovery_item.item_id,
                    action=action,
                )
        except Exception:
            _log.exception(
                "settling the blocked recovery for job %s raised",
                getattr(job, "id", "?"),
            )
            return False, "The blocked recovery could not be settled safely."
        expected = (
            BlockedItemSettlementStatus.RETRYABLE
            if action is BlockedItemSettlementAction.RETRY
            else BlockedItemSettlementStatus.DISCARDED
        )
        if settled.status is not expected:
            return False, settled.reason
        try:
            refreshed = _record_startup_recovery(runtime._RUN_LOCK_HANDLE)
        except Exception:
            _log.exception(
                "couldn't verify settled recovery for job %s", getattr(job, "id", "?"))
            return False, "The settled recovery could not be verified safely."
        if action is BlockedItemSettlementAction.RETRY:
            if imported and _recovery_status_value(refreshed) == "attention_required":
                return False, (
                    "Beets filed this album, but it still couldn't be "
                    "verified. Give up on it to keep what Beets filed and let "
                    "downloads run again."
                )
            if _recovery_status_value(refreshed) != "resume_required" or (
                job is not None and not _durable_recovery_matches_job(job)
            ):
                return False, "The settled download is not safe to resume."
        elif _recovery_status_value(refreshed) != "clear":
            return False, "The discarded recovery did not clear completely."
        return True, settled.reason


def _durable_recovery_control():
    """Describe the one exact retry control safe to render, if any."""
    binding = _startup_recovery_binding()
    job_id = _startup_recovery_web_job_id()
    if binding is None or job_id is None:
        return None
    recovery_item, _journal, queued_item, _origin = binding
    try:
        row = job_persistence.load_one(job_id)
        if row is None or job_persistence.durable_completion_acknowledged(
            job_id,
            job_created_at=row.get("created_at"),
            album_id=row.get("album_id"),
        ) is not False:
            return None
    except (OSError, TypeError, ValueError):
        return None
    return {
        "job_id": job_id,
        "operation_id": recovery_item.operation_id,
        "item_id": recovery_item.item_id,
        "status": _startup_recovery_status_value(),
        "imported": startup_recovery.import_was_filed(
            recovery_item.operation_id, queued_item),
        "partial": startup_recovery.import_stopped_part_way(
            recovery_item.operation_id, queued_item),
    }


def _web_give_up_offer(job_id: str):
    """The Give up control for the paused banner, while that job is stopped."""
    control = _durable_recovery_control()
    if control is None or control["job_id"] != job_id:
        return None
    job = job_mgr.registry.get(job_id)
    if job is not None and job.status is not job_mgr.JobStatus.FAILED:
        return None
    return control


def _recovery_submission_matches(job, operation_id: str, item_id: str) -> bool:
    control = _durable_recovery_control()
    return bool(
        type(job) is job_mgr.Job
        and control is not None
        and control["job_id"] == job.id
        and control["operation_id"] == operation_id
        and control["item_id"] == item_id
    )


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
    status = _startup_recovery_status_value()
    return status == "clear" or (
        status == "resume_required"
        and _durable_recovery_matches_job(job)
    )


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


def _refresh_post_import_relocation_recovery(authority) -> bool:
    """Refresh the existing global write gate after a handoff interruption."""
    try:
        recovered = _record_startup_recovery(authority)
    except Exception:
        _log.exception("couldn't refresh recovery after track relocation")
        return False
    return _recovery_status_value(recovered) == "clear"
