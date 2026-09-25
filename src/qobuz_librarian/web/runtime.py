"""Shared state of the running Web app, and the helpers that use it."""
import asyncio
import copy
import hashlib
import html
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import stat
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from qobuz_librarian import (
    __version__,
    completion,
    download,
    download_result,
    raise_open_file_limit,
    redaction,
    run_lock,
)
from qobuz_librarian import config as cfg
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api import lastfm
from qobuz_librarian.api import search as qobuz_search
from qobuz_librarian.api.auth import (
    AuthEvidence,
    AuthLost,
    AuthOutcome,
    CredentialChanged,
    DownloaderNotReady,
    NoCredsError,
    QobuzAccess,
    QobuzEntitlementError,
    QobuzError,
    QobuzUnavailable,
    credentials_from_values,
    qobuz_capability,
)
from qobuz_librarian.completion import CompletionOrigin, CompletionOriginKind, RecoveryOwner
from qobuz_librarian.integrations import beets as beets_mod
from qobuz_librarian.integrations import lyrics as lyrics_mode
from qobuz_librarian.integrations import staging as staging_mod
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import (
    candidate_premise,
    catalog,
    collection_snapshot,
    flac_cache,
    generation_state,
    new_releases,
    post_import_relocation,
    repair_cache,
    scan_checkpoint,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.post_import_relocation import PostImportRelocationAttention
from qobuz_librarian.modes import process as process_mode
from qobuz_librarian.queue import builder as queue_builder
from qobuz_librarian.queue import durable_album, startup_recovery
from qobuz_librarian.queue import executor as queue_executor
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue.startup_recovery import (
    POST_IMPORT_RELOCATION_LOG_ENTRY,
    BlockedItemSettlementAction,
    BlockedItemSettlementStatus,
)
from qobuz_librarian.ui_cli import logging as cli_logging
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import flows, job_persistence, owned_paths, review_badges, settings_store
from qobuz_librarian.web import jobs as job_mgr

_log = logging.getLogger("qobuz_librarian")

# Held for the lifetime of the web process. Module-level so Python won't
# garbage-collect it (which would silently release the flock).
_RUN_LOCK_HANDLE = None
# Set when run_lock.acquire() fails at startup: the holder's PID, used by
# every destructive route to refuse new work.
_LOCK_BUSY_PID = None
# True when the web app has deliberately released the run-lock so the terminal
# (CLI) can use it. Set by the Settings "Mode" toggle, or at startup when
# QL_CLI_ONLY is set.
_CLI_MODE = False
# run_lock.acquire() returned None: the data dir can't ENFORCE the single-
# writer lock (unwritable path, or a mount without file locking).
_LOCK_UNENFORCEABLE = False
# Exact result from the most recent run-lock acquisition. Startup recovery is
# inspection/reconciliation only; it never starts a download or import.
_STARTUP_RECOVERY_RESULT = None
# True while an authoritative refresh is running or after it raised.
_STARTUP_RECOVERY_UNKNOWN = False
_STARTUP_RECOVERY_REFRESHING = False
_STARTUP_RECOVERY_LOCK = threading.RLock()
# Set before lifespan shutdown starts so no new mutating request can register
# while the workers and request-owned library operations are draining.
_SHUTTING_DOWN = False
# Set the moment a stop signal arrives. The server waits for open requests
# before it shuts the app down, and a live-progress stream never ends by
# itself, so the streams close on this and running work starts to wind down.
_STOP_SIGNALLED = threading.Event()
# Persisted Web jobs are restored only after this process holds exact write
# authority.
_JOBS_RESTORED = False
_JOBS_RESTORE_LOCK = threading.Lock()
_TOKEN_VALID: bool | None = None
_TOKEN_GENERATION: str | None = None
_AUTH_LOSS_NOTIFIED_GENERATIONS: set[str] = set()
_CREDENTIAL_LOCK = threading.RLock()


def run_lock_handle():
    return _RUN_LOCK_HANDLE


def set_run_lock_handle(handle) -> None:
    global _RUN_LOCK_HANDLE
    _RUN_LOCK_HANDLE = handle


def lock_busy_pid():
    return _LOCK_BUSY_PID


def set_lock_busy_pid(pid) -> None:
    global _LOCK_BUSY_PID
    _LOCK_BUSY_PID = pid


def cli_mode() -> bool:
    return _CLI_MODE


def set_cli_mode(on: bool) -> None:
    global _CLI_MODE
    _CLI_MODE = on


def lock_unenforceable() -> bool:
    return _LOCK_UNENFORCEABLE


def set_lock_unenforceable(on: bool) -> None:
    global _LOCK_UNENFORCEABLE
    _LOCK_UNENFORCEABLE = on


def shutting_down() -> bool:
    return _SHUTTING_DOWN


def startup_recovery_result():
    return _STARTUP_RECOVERY_RESULT


def set_token_state(valid: bool | None, generation: str | None) -> None:
    global _TOKEN_VALID, _TOKEN_GENERATION
    _TOKEN_VALID = valid
    _TOKEN_GENERATION = generation


def _run_lock_intact() -> bool:
    intact = getattr(_RUN_LOCK_HANDLE, "intact", None)
    return callable(intact) and intact() is True


def _beets_runtime_diagnostic() -> tuple[str | None, str]:
    """Distinguish an absent launcher from a failed runtime verification."""
    configured = getattr(cfg, "BEETS_PYTHON", "")
    discovered = None if configured else shutil.which("beet")
    try:
        candidate = beets_mod._beets_python_from_launcher()
    except (OSError, TypeError, ValueError):
        candidate = None
    if candidate is None:
        if configured or discovered:
            return (
                None,
                "The Beets launcher could not be resolved to a verifiable "
                "Python executable",
            )
        return (
            None,
            "No Beets launcher was found on PATH and BEETS_PYTHON is unset",
        )
    runtime = beets_mod._checked_beets_runtime(candidate)
    if runtime is None:
        return (
            None,
            "The configured Beets launcher could not be verified as an "
            "executable Python runtime",
        )
    if beets_mod._configured_beets_plugins(runtime) is None:
        return (
            None,
            "Could not verify a Beets 2.14.1 runtime and readable "
            f"configuration using {runtime.python}",
        )
    return runtime.python, runtime.python


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
        if not _run_lock_intact():
            return False
        _record_startup_recovery(_RUN_LOCK_HANDLE)
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
            and _run_lock_intact()
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
        if not _run_lock_intact():
            return False, "The run lock is unavailable."
        try:
            recovery = _record_startup_recovery(_RUN_LOCK_HANDLE)
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
                    authority=_RUN_LOCK_HANDLE,
                    operation_id=recovery_item.operation_id,
                    item_id=recovery_item.item_id,
                )
            else:
                settled = startup_recovery.settle_blocked_item(
                    authority=_RUN_LOCK_HANDLE,
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
            refreshed = _record_startup_recovery(_RUN_LOCK_HANDLE)
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
    if not _run_lock_intact():
        return False
    try:
        _record_startup_recovery(_RUN_LOCK_HANDLE)
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


def _settled_completion_response(request, job):
    """Take the completed-download lane when a refused settlement has already
    cleared the recovery it refused over.

    `settle_blocked_item` only settles a pre-launch abort, so it refuses a
    download that imported and then stranded a file in staging, but it parks
    that staging first, which is the one thing the recovery was waiting on.
    Re-read the recovery and the completion record it just moved, or the reply
    describes a state this request has already left behind.
    """
    if not _run_lock_intact():
        _log.info("Retry %s: no completed-download lane; run lock not held.",
                  job.id)
        return None
    try:
        recovery = _record_startup_recovery(_RUN_LOCK_HANDLE)
    except Exception:
        _log.warning("Retry %s: no completed-download lane; the recovery "
                     "record could not be re-read.", job.id, exc_info=True)
        return None
    status = _recovery_status_value(recovery)
    completed = _durable_completion_status(job)
    if status != "clear" or completed is not True:
        _log.info("Retry %s: no completed-download lane; recovery is %s and "
                  "the download's completion record reads %s.",
                  job.id, status, completed)
        return None
    _log.info("Retry %s: the refused settlement had already cleared the "
              "recovery, so taking the completed-download lane.", job.id)
    busy = _lock_busy_response(request)
    if busy is not None:
        _log.info("Retry %s: the completed-download lane stopped; another "
                  "process holds the run lock.", job.id)
        return busy
    if not _reconcile_acknowledged_job(
        job,
        "Download completed. Retry cleared the leftover that was blocking it.",
    ):
        _log.warning("Retry %s: the completed download could not be written "
                     "to History.", job.id)
        return _durable_recovery_response(
            request,
            "The completed download could not be saved to History. No "
            "download was started. Check the data-folder permissions, then "
            "restart Qobuz Librarian.",
        )
    _log.info("Retry %s: settled as a completed download.", job.id)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


def _durable_recovery_response(request, message: str):
    if _is_htmx(request):
        return HTMLResponse(
            _ql_notice_html("error", html.escape(message)),
            status_code=200,
        )
    # can_retry: these messages end in "restart Qobuz Librarian", and after a
    # restart a reload is exactly the next step; without it the page offered
    # no way forward at all.
    return _tr(request, "lock_busy.html", {"msg": message, "can_retry": True},
               status_code=503)


def _ql_notice_html(kind: str, body: str) -> str:
    # The copy sits in one span: the notice box is a flex row, and bare text
    # beside a link would be spaced apart as separate flex items.
    return (
        f'<div class="ql-notice ql-notice-{kind}" '
        f'data-flash data-flash-kind="{kind}"><span>{body}</span></div>'
    )


def _download_error_message(exc, fallback: str) -> str:
    """Give download preparation failures the same Web-facing diagnosis."""
    if isinstance(exc, asyncio.TimeoutError):
        return "Timed out reaching the Qobuz API. Try again."
    if isinstance(exc, QobuzUnavailable):
        return str(exc)
    if isinstance(exc, AuthLost):
        return "Qobuz rejected the saved token. Reconnect in Settings."
    if isinstance(exc, DownloaderNotReady):
        return (
            "Your Qobuz token works, but downloads also need your Qobuz "
            "user ID. Add it in Settings."
        )
    if isinstance(exc, CredentialChanged):
        return "Qobuz credentials changed while this was starting. Try again."
    if isinstance(exc, QobuzEntitlementError):
        return "Your Qobuz account cannot perform this download."
    if isinstance(exc, QobuzError):
        if api_auth.friendly_qobuz_error(exc).startswith("HTTP 404"):
            return "No album with that id. Check the URL or use Search."
        return "Qobuz answered with an error. Try again."
    return fallback


def _authorize_qobuz_live(access: QobuzAccess, *, expected_generation=""):
    """Run the bounded uncached check used before a Web action is admitted."""
    return api_client.call_within(
        cfg.WEB_TEST_AUTH_TIMEOUT,
        api_client.authorize_qobuz_action,
        access,
        expected_generation=expected_generation,
        auth_valid=_token_valid_for(),
    )


async def _authorize_qobuz_for_web(access: QobuzAccess, *,
                                    expected_generation=""):
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: _authorize_qobuz_live(
                access,
                expected_generation=expected_generation,
            ),
        ),
        timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
    )


def _credential_generation_is_active(generation: str) -> bool:
    return bool(generation) and api_auth.read_qobuz_credentials().generation == generation


def _job_admission_response(request):
    """Explain a refused jobs.db admission without claiming work was queued."""
    message = job_mgr.JOB_ADMISSION_ERROR
    if _is_htmx(request):
        return HTMLResponse(
            _ql_notice_html("error", html.escape(message)),
            status_code=200,
        )
    return RedirectResponse(
        url="/queue?error=" + _notice_key(message),
        status_code=303,
    )


def _writes_paused_notice(*, durable_resume_job_id: str | None = None,
                          queue_behind_job=None,
                          log_details: bool = False):
    """Why downloads and scans are paused, in the user's words, or None.

    One source for both the 503 a blocked request gets and the notice the
    dashboard shows, so the two cannot drift into naming different causes.
    ``log_details`` is for the request path only: the dashboard reads this on
    every load and must not write a log line each time. ``queue_behind_job``
    is the one job allowed past another download's recovery, to wait its turn
    rather than be refused.
    """
    _await_recovery_refresh()
    reason = ""
    action = None
    settle = None
    give_up = None
    if _CLI_MODE:
        reason = "Terminal mode is holding the library."
        msg = ("Terminal (CLI) mode is on, so downloads and scans are paused "
               "here. Resume on Settings → Mode (Resume web app).")
        action = {"href": "/settings#mode", "label": "Open Settings"}
    elif _LOCK_BUSY_PID is not None:
        reason = "Another Qobuz Librarian run is using the library."
        msg = ("Another Qobuz Librarian run is active. Downloads and scans are "
               "paused. Stop the other run first, then restart Qobuz "
               "Librarian.")
    elif (unwritable := _unwritable_volumes()):
        reason = "A folder Qobuz Librarian must write to is read-only."
        action = {"href": "/settings#diagnostics", "label": "Open Diagnostics"}
        msg = ("Qobuz Librarian can't write where it needs to: "
               f"{'; '.join(unwritable)}. On a NAS, set "
               "PUID/PGID to the share owner and confirm the host "
               "directories exist. Downloads can't run until fixed.")
    elif _LOCK_UNENFORCEABLE and isinstance(run_lock.unavailable_reason,
                                            PermissionError):
        reason = "The run lock file can't be opened."
        msg = (f"Qobuz Librarian can't open {cfg.LOCK_FILE}: permission "
               "denied. Downloads and scans are paused. Set its owner to the "
               "user Qobuz Librarian runs as (PUID and PGID in Docker), then "
               "restart.")
    elif _LOCK_UNENFORCEABLE:
        reason = "The data folder can't hold the run lock."
        msg = ("The data folder can't hold the run lock (read-only, or a "
               "mount without file locking). Downloads and "
               "scans are paused. Move the data folder to a writable "
               "filesystem that supports file locking, then restart.")
    elif not _data_dir_available():
        # The folder was fine at startup, so the lock checks above all pass and
        # nothing else notices. Without this branch the readiness check knows
        # the app cannot save anything while every page still looks normal.
        reason = "Qobuz Librarian can't write to its data folder."
        action = {"href": "/settings#diagnostics", "label": "Open Diagnostics"}
        msg = ("Qobuz Librarian can't write to its data folder, so downloads "
               "and scans are paused and nothing new can be saved. Check that "
               "the folder "
               "still exists and that Qobuz Librarian can write to it; the "
               "app picks it up again on its own.")
    elif _STARTUP_RECOVERY_REFRESHING:
        reason = "Interrupted work is being checked."
        msg = ("Qobuz Librarian is checking for interrupted downloads, so "
               "downloads and scans wait until it finishes.")
    elif _STARTUP_RECOVERY_UNKNOWN:
        reason = "Interrupted work could not be checked safely."
        msg = (
            "Qobuz Librarian took the run lock but could not read its saved "
            "recovery state. The lock was released, downloads and scans stay "
            "paused, and the app tries again on its own. Check the "
            "data-folder permissions if this notice remains."
        )
    elif not _run_lock_intact():
        reason = "The run lock was lost."
        msg = ("The run lock was lost, so downloads and scans are paused. "
               "Restart Qobuz Librarian.")
    elif not job_mgr.job_persistence.ready_for_admission():
        jobs_db = job_mgr.job_persistence.database_path()
        if job_mgr.job_persistence.database_damaged():
            reason = "The Queue and History file is damaged."
            msg = (
                f"Qobuz Librarian can't read {jobs_db}, so downloads and scans "
                "are paused before any work starts. Nothing new was queued. "
                f"Move it, and any {jobs_db.name}-wal and {jobs_db.name}-shm "
                "beside it, out of the data folder and restart Qobuz "
                "Librarian; History and any review waiting in it will be lost."
            )
        else:
            reason = "Queue and History cannot be saved to the data folder."
            action = {"href": "/settings#diagnostics",
                      "label": "Open Diagnostics"}
            msg = (
                f"Qobuz Librarian can't open or write {jobs_db}, so downloads "
                "and scans are paused before any work starts. Nothing new was "
                "queued. Check the permissions of that file and the data "
                "folder, and free space, then restart Qobuz Librarian."
            )
    elif (_startup_recovery_status_value() == "attention_required"):
        relocation = _post_import_relocation_recovery()
        if relocation is not None:
            paths = "; ".join(str(path) for path in relocation.paths)
            # relocation.reason is str(exc) from the relocation code; an
            # internal diagnostic, not an explanation. It belongs in the log,
            # which this message points at; the user gets what happened to
            # their music and what to do.
            if log_details:
                _log.warning(
                    "post-import relocation recovery: %s (paths: %s)",
                    relocation.reason or "reason not reported",
                    paths or "none reported")
            reason = "A move of album folders inside your library was interrupted."
            msg = (
                "Qobuz Librarian can't confirm that move finished, so downloads "
                "and scans are paused and your files are left exactly as they "
                "are. "
                + (f"The folders involved: {paths}. " if paths else "")
                + "Restart Qobuz Librarian; if this screen comes back, the "
                f"“{POST_IMPORT_RELOCATION_LOG_ENTRY}” entry in the container "
                "log has the technical detail."
            )
        elif _unreadable_queue_paths() is not None:
            files = "; ".join(str(path) for path in _unreadable_queue_paths())
            reason = "The saved download queue can't be read."
            msg = (
                "Downloads and scans are paused and nothing was changed. "
                + (f"The file involved: {files}. " if files else "")
                + "Fix its permissions, or if it is damaged, move it out of "
                "the data folder (the downloads it listed will need queuing "
                "again), then restart Qobuz Librarian."
            )
        elif _recovery_pause_is_another_download(queue_behind_job):
            return None
        else:
            reason = "An interrupted download couldn't be verified."
            # A terminal download never became a web job, so it has no History
            # row and no Retry button. Point each origin at the surface that
            # can settle it, the way the resume_required branch below does.
            origin = _startup_recovery_origin_value()
            named = _startup_recovery_album_label()
            of_album = f" of “{named}”" if named else ""
            paused = ("Downloads and scans are paused, and its saved queue and "
                      "staged files were left unchanged. ")
            held_job_id = _startup_recovery_web_job_id()
            partial = startup_recovery.partial_import_note(
                _STARTUP_RECOVERY_RESULT)
            if partial is not None:
                msg = (f"{partial} Downloads and scans are paused until it "
                       "is given up, which keeps the tracks Beets moved and "
                       "sets the rest aside in Settings > Diagnostics.")
                if origin == "cli":
                    settle = _terminal_recovery_offer()
                elif held_job_id is not None:
                    action = {"href": f"/jobs/{held_job_id}",
                              "label": "Open that download"}
            elif origin != "cli" and held_job_id is not None:
                action = {"href": f"/jobs/{held_job_id}",
                          "label": "Open that download"}
                give_up = _web_give_up_offer(held_job_id)
                msg = ("Downloads and scans are paused until the interrupted "
                       f"download{of_album} is retried or given up. Its saved "
                       "queue and staged files were left unchanged.")
            elif origin == "cli":
                settle = _terminal_recovery_offer()
                if settle is not None:
                    # Not everyone who starts a download in a terminal wants to
                    # go back to one to get out of it, and giving up on the
                    # album is the one decision that lifts the pause on its own.
                    action = {"href": "/queue", "label": "Settle it"}
                    if settle["imported"]:
                        msg = (f"An interrupted terminal download{of_album} "
                               "stopped after Beets had filed it, and it "
                               "could not be verified. Downloads and scans "
                               "are paused. Giving up on it clears the pause "
                               "and keeps what Beets filed in your library. "
                               "Checking it again needs terminal mode in "
                               "Settings.")
                    else:
                        msg = (f"An interrupted terminal download{of_album} "
                               "could not be verified safely. " + paused +
                               "Giving up on it clears the pause, and because "
                               "nothing reached your library the album is "
                               "still there to download again from the "
                               "Library review or a search. Trying that same "
                               "download again needs terminal mode in "
                               "Settings.")
                else:
                    action = {"href": "/settings#mode", "label": "Open Settings"}
                    msg = (f"An interrupted terminal download{of_album} could "
                           "not be verified safely. " + paused + "Switch to "
                           "terminal mode in Settings and run Qobuz Librarian "
                           "there; it offers to settle this.")
            else:
                msg = (f"An interrupted download{of_album} could not be "
                       "verified safely. " + paused + "Settle it from the "
                       "interface it was started in; if it stays blocked, "
                       "check the application log.")
    elif (
        _startup_recovery_status_value() == "resume_required"
        and not _durable_resume_allowed(durable_resume_job_id or "")
        and not _recovery_pause_is_another_download(queue_behind_job)
    ):
        reason = "An interrupted download is waiting to be settled."
        origin = _startup_recovery_origin_value()
        named = _startup_recovery_album_label()
        of_album = f" of “{named}”" if named else ""
        held_job_id = _startup_recovery_web_job_id()
        if origin == "cli":
            action = {"href": "/settings#mode", "label": "Open Settings"}
            msg = (f"An interrupted terminal download{of_album} has saved "
                   "recovery state. Other library changes are paused. Switch "
                   "to terminal mode in Settings, then resume that download "
                   "there.")
        elif held_job_id is not None:
            # The saved origin is missing on records written before it
            # existed, and it decided the wording; the job named by the saved
            # mode is the one to open, so that is what the notice offers.
            action = {"href": f"/jobs/{held_job_id}",
                      "label": "Open that download"}
            give_up = _web_give_up_offer(held_job_id)
            msg = ("Downloads and scans are paused until the interrupted "
                   f"download{of_album} is retried or given up.")
        else:
            msg = (f"An interrupted download{of_album} has saved recovery "
                   "state. Other library changes are paused until that exact "
                   "download is resumed from the interface where it started.")
    else:
        return None
    return {"reason": reason, "msg": msg, "action": action, "settle": settle,
            "give_up": give_up}


def _retry_can_queue(job) -> bool:
    """Whether Retry would queue this job behind the paused recovery."""
    if isinstance(job, dict):
        job = (job_mgr.registry.get(job["id"])
               or job_mgr.load_historical_job(job["id"]))
    return (
        _recovery_pause_is_another_download(job)
        and _writes_paused_notice(queue_behind_job=job) is None
    )


def _lock_busy_response(request, *, durable_resume_job_id: str | None = None,
                        queue_behind_job=None):
    """Return a 503 response if web writes are paused, else None."""
    notice = _writes_paused_notice(
        durable_resume_job_id=durable_resume_job_id,
        queue_behind_job=queue_behind_job,
        log_details=True,
    )
    if notice is None:
        return None
    unwritable_now = _unwritable_volumes()
    if _is_htmx(request):
        response = HTMLResponse(
            _ql_notice_html("error", html.escape(notice["msg"])),
            status_code=200)
        if request.headers.get("HX-Target") == "diagnostics-list":
            # The list is the whole Diagnostics panel; the notice goes above
            # it rather than in its place.
            response.headers["HX-Reswap"] = "beforebegin"
        return response
    return _tr(request, "lock_busy.html",
               {"msg": notice["msg"], "reason": notice["reason"],
                "action": notice["action"],
                # "Try again" only helps where retrying can succeed; the rest
                # need something fixed first and the button was false comfort.
                "can_retry": (_LOCK_BUSY_PID is not None
                              or bool(unwritable_now)
                              or not _data_dir_available()
                              or _STARTUP_RECOVERY_REFRESHING)},
               status_code=503)


def _web_writes_paused() -> bool:
    """True when destructive web work must not run: the same conditions
    _lock_busy_response answers 503 for, as one predicate for the AUTOMATIC
    triggers (dashboard new-release check, library-scan resume) that have no
    request to bounce. Any trigger checking only part of this list quietly
    re-opens the hole the pause exists to close."""
    _await_recovery_refresh()
    return (
        _SHUTTING_DOWN
        or _CLI_MODE
        or _LOCK_BUSY_PID is not None
        or bool(_unwritable_volumes())
        or not _data_dir_available()
        or not job_mgr.job_persistence.ready_for_admission()
        or _LOCK_UNENFORCEABLE
        or not _run_lock_intact()
        or _startup_recovery_status_value() in {
            "attention_required",
            "resume_required",
        }
    )


def _unwritable_volumes() -> list[str]:
    """Live probe of the critical mounts; empty means writes may run.

    Probed on every gated attempt, so fixing ownership on the host opens
    the gate without a container restart; the Diagnostics page re-checks
    live, and the gate has to agree with it. Opt-in via env so tests and
    dev runs without /staging or /music mounted don't trip on it; the
    bundled compose sets it to 1."""
    raw_check_volumes = os.environ.get("QL_CHECK_VOLUMES")
    if raw_check_volumes is None:
        return []
    if not cfg._env_bool("QL_CHECK_VOLUMES", True):
        return []
    problems = []
    # Named the way the operator would recognise the folder, not the compose
    # env var; the path shown is the host one, since a container path means
    # nothing on the host that actually needs fixing.
    for friendly, path in (("Staging area", cfg.STAGING_DIR),
                           ("Music library", cfg.MUSIC_ROOT)):
        p = Path(path)
        unreachable = not p.exists()
        not_a_dir = p.exists() and not p.is_dir()
        unwritable = p.exists() and p.is_dir() and not os.access(str(p), os.W_OK)
        if unreachable or not_a_dir or unwritable:
            display, _ = _resolve_host_path(str(path))
            problems.append(
                f"{friendly} ({display})"
                + (" is missing" if unreachable
                   else " is not a folder" if not_a_dir
                   else " is read-only"))
    return problems


def _data_dir_available() -> bool:
    path = Path(cfg.DATA_DIR)
    try:
        return path.is_dir() and os.access(
            path, os.R_OK | os.W_OK | os.X_OK)
    except OSError:
        return False


def _readiness_report() -> tuple[int, dict]:
    failed = []
    if (
        not web_auth.auth_disabled()
        and web_auth.creds_file_present_but_unreadable()
    ):
        failed.append("credentials")
    if not _data_dir_available():
        failed.append("data")
    if (
        not _CLI_MODE
        and _run_lock_intact()
        and not job_mgr.job_persistence.ready_for_admission()
    ):
        failed.append("job_persistence")
    if _LOCK_UNENFORCEABLE or (
        not _CLI_MODE
        and _LOCK_BUSY_PID is None
        and not _run_lock_intact()
    ):
        failed.append("run_lock")
    if _SHUTTING_DOWN:
        failed.append("shutting_down")
    if failed:
        return 503, {"ok": False, "status": "not_ready", "checks": failed}

    degraded = []
    if _CLI_MODE:
        degraded.append("terminal_mode")
    if _LOCK_BUSY_PID is not None:
        degraded.append("other_writer")
    if _unwritable_volumes():
        degraded.append("write_volumes")
    if _startup_recovery_status_value() in {
        "attention_required",
        "resume_required",
    }:
        degraded.append("recovery")
    if degraded:
        return 200, {"ok": True, "status": "degraded", "checks": degraded}
    return 200, {"ok": True, "status": "ready"}


def _has_startup_write_authority() -> bool:
    """Whether this Web process owns the real single-writer boundary."""
    return _run_lock_intact() and not _web_writes_paused()


def _shutdown_web_mutations() -> None:
    """Quiesce every Web writer before releasing the process run lock."""
    global _RUN_LOCK_HANDLE
    job_mgr.stop_worker()
    job_mgr.configure_staging_entry_guard(None)
    job_mgr.configure_held_release(None)
    if _RUN_LOCK_HANDLE is not None:
        try:
            _RUN_LOCK_HANDLE.close()
        except OSError:
            pass
        _RUN_LOCK_HANDLE = None


async def _finish_web_lifespan(
    ticker,
    lock_retry_task,
    maintenance_task,
    token_probe_task,
) -> None:
    """Stop background work, then release the run lock after all writers."""
    global _SHUTTING_DOWN
    with _auto_check_lock:
        _SHUTTING_DOWN = True
    # First, so running work unwinds while the rest shuts down.
    job_mgr.stop_for_restart()
    for task in (ticker, lock_retry_task, token_probe_task):
        if task is not None:
            task.cancel()
    try:
        for task in (ticker, lock_retry_task, token_probe_task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        if maintenance_task is not None:
            await maintenance_task
    finally:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _shutdown_web_mutations)


def _close_web_run_lock(lease) -> None:
    """Release a candidate Web lease without hiding a close failure."""
    try:
        lease.close()
    except OSError:
        _log.exception(
            "couldn't release a rejected Web run-lock lease"
        )


def _recover_under_web_run_lock(lease, *, restore_jobs: bool = True):
    """Publish a lease only while recovery and saved jobs are reconciled."""
    global _RUN_LOCK_HANDLE
    _RUN_LOCK_HANDLE = lease
    try:
        settings_store.reload_from_disk()
        result = _record_startup_recovery(lease)

        publication_recovery = (
            generation_state.reconcile_interrupted_library_publication(lease)
        )
        if publication_recovery is None:
            raise RuntimeError(
                "interrupted Library publication state could not be saved"
            )
        if publication_recovery:
            _log.warning(
                "Recovered a Library crawl interrupted before its saved view "
                "was published."
            )
        if restore_jobs:
            _restore_jobs_once()
        return result
    except BaseException:
        _RUN_LOCK_HANDLE = None
        _close_web_run_lock(lease)
        raise


async def _retry_web_run_lock(log, *, delay: float = 30) -> None:
    """Retry a busy lock and an acquired lease whose recovery read failed."""
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _LOCK_UNENFORCEABLE
    while _LOCK_BUSY_PID is not None or _STARTUP_RECOVERY_UNKNOWN:
        await asyncio.sleep(delay)
        with _auto_check_lock:
            if _CLI_MODE:
                return
        try:
            lease = run_lock.acquire("web")
        except run_lock.LockBusy as busy:
            with _auto_check_lock:
                if _CLI_MODE:
                    return
                _LOCK_BUSY_PID = busy.pid
            continue

        with _auto_check_lock:
            if _CLI_MODE:
                if lease is not None:
                    _close_web_run_lock(lease)
                return
            if lease is None:
                _RUN_LOCK_HANDLE = None
                _LOCK_BUSY_PID = None
                _LOCK_UNENFORCEABLE = True
                log.error(
                    "Run-lock became unenforceable; download/scan endpoints "
                    "paused until a lock-capable data folder is available "
                    "and the app is restarted."
                )
                return
            try:
                result = _recover_under_web_run_lock(lease)
            except Exception:
                _LOCK_BUSY_PID = None
                _LOCK_UNENFORCEABLE = False
                log.exception(
                    "Lock acquired, but durable recovery could not be read; "
                    "the lease was released and Web will retry."
                )
                continue
            _LOCK_UNENFORCEABLE = False
            _LOCK_BUSY_PID = None
        log.info(
            "Lock acquired; durable queue startup state: %s.",
            result.status.value,
        )
        return


def _review_download_token(job):
    expected_generation = str(
        (job.execute_args or {}).get("_credential_generation") or ""
    )
    return _authorize_qobuz_live(
        QobuzAccess.DOWNLOAD_ACTION,
        expected_generation=expected_generation,
    ).token


def _run_review_with_live_download(job, chosen, execute):
    """Recheck both local receipts and the bound credential at worker start."""
    candidate_premise.validate_all(chosen)
    token = _review_download_token(job)
    # The live request above can take long enough for a local edit to land.
    # Repeat the exact receipt check before the flow reaches its first backup.
    candidate_premise.validate_all(chosen)
    return execute(job, chosen, token)


def _resume_album_download(job, _args):
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_albums)


def _resume_upgrade(job, _args):
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_upgrades)


def _resume_repair(job, _args):
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_repairs)


def _resume_migration(job, args):
    dest = args.get("dest", "")
    in_place = bool(args.get("in_place"))
    src = args.get("src")
    allow_low_space = bool(args.get("allow_low_space"))
    return lambda j, chosen: flows.execute_migration(
        j, chosen, dest, in_place=in_place,
        src=Path(src) if src else None, allow_low_space=allow_low_space)


def _job_downsample_keep_originals(job):
    value = (job.execute_args or {}).get("keep_originals")
    return value if type(value) is bool else None


def _resume_downsample(job, _args):
    def execute(j, chosen):
        candidate_premise.validate_all(chosen)
        return flows.execute_downsamples(
            j,
            chosen,
            token=None,
            keep_originals=_job_downsample_keep_originals(j),
        )

    return execute


def _upgrade_available(creds_ok: bool | None = None) -> bool:
    return bool(getattr(cfg, "UPGRADE_SCAN_ENABLED", True))


# Names the persisted ``execute_kind`` strings so jobs survive a restart even
# though their original execute closure is gone.
_RESUME_EXECUTE: dict = {
    "library":      _resume_album_download,
    "new_releases": _resume_album_download,
    "collection_restore": _resume_album_download,
    "upgrade":      _resume_upgrade,
    "repair":       _resume_repair,
    "migration":    _resume_migration,
    "downsample":   _resume_downsample,
}


def _restore_jobs_once() -> None:
    global _JOBS_RESTORED
    with _JOBS_RESTORE_LOCK:
        if _JOBS_RESTORED:
            return
        # Ahead of the restore, which would otherwise reopen a gone backup as a
        # failure pointing at the notice this settles away, and ahead of the
        # first page render, which carries the attention count.
        _retire_gone_recoveries(job_persistence.recovery_history())
        try:
            job_mgr.restore_jobs(
                _RESUME_EXECUTE,
                durable_recovery_clear=(
                    _startup_recovery_status_value() == "clear"
                ),
                durable_recovery_job_id=_startup_recovery_web_job_id(),
                requeue=_requeued_download_run,
            )
        except Exception as exc:
            _log.warning(
                "couldn't restore prior jobs: %s. Starting fresh.",
                exc,
            )
        finally:
            # restore_jobs publishes into the registry only after it has built
            # the full batch.
            _JOBS_RESTORED = True


def _scrub_stored_credentials(logger) -> None:
    """One pass over everything written before the masking existed. A Qobuz
    error names the URL it called, and that URL carries the account email and
    the auth token, so stored job records and the app's own log files can still
    hold a working credential after an upgrade. Runs before the log handler is
    attached, so rewriting a log file cannot cut a handler off from it, and the
    marker keeps it to one pass: nothing written afterwards can carry a secret.
    """
    marker = cfg.DATA_DIR / ".credential_scrub"
    try:
        if marker.exists():
            return
    except OSError:
        return
    complete = True
    try:
        # Reading them registers the live values, so a token logged with no
        # parameter name beside it is masked too.
        api_auth.read_qobuz_credentials()
    except Exception:
        complete = False
    rows = 0
    try:
        job_persistence.init()
        scrubbed = job_persistence.scrub_stored_secrets()
        if scrubbed is None:
            complete = False
        else:
            rows = scrubbed
    except Exception:
        complete = False
    files = 0
    try:
        targets = list(cfg.DATA_DIR.glob("qobuz-librarian*.log*"))
    except OSError:
        complete = False
        targets = []
    targets.append(cfg.FETCH_LOG_FILE)
    for path in targets:
        try:
            scrubbed = redaction.scrub_file(path)
            if scrubbed is None:
                complete = False
            elif scrubbed:
                files += 1
        except Exception:
            complete = False
    if rows or files:
        logger.info(
            f"Masked account details in {rows} stored job record(s) and "
            f"{files} log file(s) written by an earlier version.")
    if complete:
        try:
            marker.touch()
        except OSError:
            pass
    else:
        logger.warning(
            "Stored credential cleanup was incomplete and will retry next start.")


def _watch_stop_signal(loop) -> None:
    """Close live streams and start winding down work on SIGTERM, then let
    the server's own handler begin its shutdown."""
    if threading.current_thread() is not threading.main_thread():
        return
    previous = signal.getsignal(signal.SIGTERM)
    if not callable(previous):
        return

    def _on_stop(signum, frame):
        _STOP_SIGNALLED.set()
        loop.call_soon_threadsafe(
            lambda: loop.run_in_executor(None, job_mgr.stop_for_restart))
        previous(signum, frame)

    signal.signal(signal.SIGTERM, _on_stop)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE, _LOCK_UNENFORCEABLE
    global _SHUTTING_DOWN, _STARTUP_RECOVERY_RESULT, _STARTUP_RECOVERY_UNKNOWN
    global _JOBS_RESTORED
    raise_open_file_limit()
    _SHUTTING_DOWN = False
    _STOP_SIGNALLED.clear()
    _watch_stop_signal(asyncio.get_running_loop())
    with _JOBS_RESTORE_LOCK:
        _JOBS_RESTORED = False
    _STARTUP_RECOVERY_RESULT = None
    _STARTUP_RECOVERY_UNKNOWN = False
    job_mgr.set_durable_recovery_job_id(None)
    _scrub_stored_credentials(_log)
    cli_logging.attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL)
    if web_auth.auth_disabled():
        _log.warning("[warn] WEB_AUTH=none: the web UI is unauthenticated, do not "
                     "expose to an untrusted network")
    else:
        try:
            cred_status = web_auth.apply_env_credentials()
        except web_auth.CredentialSeedError as exc:
            raise RuntimeError(str(exc)) from None
        except web_auth.PasswordRejected as exc:
            raise RuntimeError(
                f"WEB_AUTH_PASSWORD was rejected: {exc}") from None
        if (
            cred_status in {"partial", "failed"}
            and not web_auth.credentials_configured()
        ):
            if cred_status == "partial":
                raise RuntimeError(
                    "Incomplete web login seed: set both WEB_AUTH_USER and "
                    "WEB_AUTH_PASSWORD (or WEB_AUTH_PASSWORD_FILE)."
                )
            raise RuntimeError(
                "The seeded web login could not be saved to "
                f"{cfg.WEB_AUTH_FILE}; refusing to start with an open "
                "first-run setup screen."
            )
        if cred_status == "applied":
            _log.info("Configured the web login from WEB_AUTH_USER / "
                      "WEB_AUTH_PASSWORD.")
        elif cred_status == "kept":
            _log.info("Kept the password set in Settings. Change "
                      "WEB_AUTH_PASSWORD and restart to reset it.")
        elif cred_status == "partial":
            _log.warning("Set both WEB_AUTH_USER and WEB_AUTH_PASSWORD to seed "
                         "the web login from the environment: only one was set.")
        elif cred_status == "failed":
            _log.warning("Couldn't write the web login from the environment; "
                         "the data volume may not be writable.")
        if web_auth.creds_file_present_but_unreadable():
            _log.warning(
                "The web login in %s can't be read, so every page will answer "
                "503. Set WEB_AUTH_USER / WEB_AUTH_PASSWORD and restart to "
                "replace it, or stop the app and delete the file to set a new "
                "login.", cfg.WEB_AUTH_FILE)
        elif not web_auth.credentials_configured():
            _log.warning(
                "No web login configured. The open /setup screen is reachable "
                "to whoever hits the port first, who would then own the admin "
                "account. Seed WEB_AUTH_USER / WEB_AUTH_PASSWORD (compose) to "
                "close this window, and complete setup promptly on a trusted "
                "network.")
    settings_store.load()
    try:
        cfg.validate_storage_roots()
    except ValueError as exc:
        raise RuntimeError(f"invalid storage paths: {exc}") from None
    # If creds are provided via env vars, mirror them into the streamrip
    # config now so web-triggered downloads don't fail on streamrip's
    # interactive auth prompt (the env-var path doesn't otherwise reach
    # streamrip's own config file).
    if api_auth.sync_streamrip_creds_from_env() is False:
        _log.warning("Couldn't write env credentials into the streamrip "
                     "config; web downloads may fail until creds are set "
                     "via the Settings page.")
    api_auth.enforce_streamrip_disc_folders()
    # Acquire the shared run lock so separate CLI runs cannot overlap Web work.
    if os.environ.get("QL_CLI_ONLY", "").strip().lower() in ("1", "true", "yes", "on"):
        # Terminal-first deployment leaves the lock free for a CLI process.
        _CLI_MODE = True
        _LOCK_BUSY_PID = None
        _STARTUP_RECOVERY_RESULT = None
        _STARTUP_RECOVERY_UNKNOWN = False
        _log.info("QL_CLI_ONLY set: starting in terminal (CLI) mode; the web "
                  "app holds no lock and download/scan endpoints are paused.")
    else:
        _CLI_MODE = False
        try:
            _RUN_LOCK_HANDLE = run_lock.acquire("web")
            _LOCK_BUSY_PID = None
            if _RUN_LOCK_HANDLE is None:
                # None isn't success: the lock can't be ENFORCED here, and
                # storing it as acquired would leave the corruption guard
                # silently off.
                _LOCK_UNENFORCEABLE = True
                _log.error(
                    "STARTUP: the data dir can't hold the single-writer lock; "
                    "download/scan endpoints paused. Move the data folder to "
                    "a writable filesystem with file locking, then restart "
                    "the app.")
            else:
                _LOCK_UNENFORCEABLE = False
                result = _recover_under_web_run_lock(
                    _RUN_LOCK_HANDLE,
                    restore_jobs=False,
                )
                _log.info(
                    "Durable queue startup state: %s.",
                    result.status.value,
                )
        except run_lock.LockBusy as busy:
            _LOCK_BUSY_PID = busy.pid
            _log.error(
                "STARTUP: another Qobuz Librarian run holds the lock (pid %s). "
                "Background task will retry acquisition every 30s; in the "
                "meantime download/scan endpoints will return 503.",
                busy.pid,
            )

    lock_retry_task = None
    maintenance_task = None
    ticker = None
    token_probe_task = None
    try:
        lock_retry_task = (
            asyncio.create_task(_retry_web_run_lock(_log))
            if _LOCK_BUSY_PID is not None
            else None
        )

        problems = _unwritable_volumes()
        if problems:
            _log.error("STARTUP: critical volumes not usable: %s. Write "
                       "endpoints will return 503 until the mounts are "
                       "fixed.", problems)
        # Heavy, throttled maintenance: prune_missing() stats every cached file
        # (100k+ on a NAS library), so run it in the background instead of blocking
        # the app from serving its first request.
        async def _bg_prune_flac_cache():
            try:
                n_pruned = await asyncio.get_running_loop().run_in_executor(
                    None, flac_cache.prune_missing)
                if n_pruned:
                    _log.info("Pruned %d stale tag-cache entries.", n_pruned)
            except Exception as e:
                _log.debug("flac-cache prune error: %s", e)
            try:
                repair_cache.prune_expired()
            except Exception as e:
                _log.debug("repair-cache prune error: %s", e)
        maintenance_task = None
        run_startup_maintenance = _has_startup_write_authority()
        if run_startup_maintenance:
            # The CLI runs these too. A browsing-only Web process must leave them
            # alone because the CLI or another Web process can own the library.
            try:
                n = backup_mod.cleanup_old_upgrade_backups()
                if n:
                    _log.info(
                        "Cleaned up %s at startup.",
                        plural(n, "stale upgrade backup"),
                    )
            except Exception as e:
                _log.debug("upgrade-backup cleanup error at startup: %s", e)
            try:
                lyrics_mode._prune_lyric_state_orphans()
            except Exception as e:
                _log.debug("lyric-state prune error at startup: %s", e)
        job_mgr.configure_staging_entry_guard(_staging_entry_allowed)
        job_mgr.configure_held_release(lambda: not _web_writes_paused())
        job_mgr.start_worker()
        if run_startup_maintenance:
            maintenance_token = job_mgr.begin_library_operation(
                "Startup maintenance")
            if maintenance_token is None:
                raise RuntimeError("Web workers stopped during startup")

            async def _registered_cache_prune():
                try:
                    await _bg_prune_flac_cache()
                finally:
                    job_mgr.end_library_operation(maintenance_token)

            maintenance_task = asyncio.create_task(_registered_cache_prune())
        if not shutil.which("rip"):
            _log.warning("`rip` (streamrip) not found in PATH; downloads will fail")
        _beets_python, beets_failure = _beets_runtime_diagnostic()
        if _beets_python is None:
            _log.warning("%s; imports will fail", beets_failure)
        if not shutil.which("flac"):
            _log.warning("`flac` not found; FLAC integrity checks fall back to a size heuristic")
        if not shutil.which("ffmpeg"):
            _log.warning("`ffmpeg` not found; hi-res downsampling disabled")
        # A second Web process must not rebadge the first process's live jobs
        # as failed merely because it cannot take the run lock.
        if _run_lock_intact():
            _restore_jobs_once()
        # Probe the saved token against Qobuz so a stale slot (non-empty but
        # not actually authenticated) surfaces in the dashboard banner rather
        # than failing the user's first search.
        token_probe_task = asyncio.create_task(_probe_token())
        # Keep the dashboard banner honest after startup: any in-session 401 from
        # the API client flips _TOKEN_VALID to False here, so a token that expires
        # mid-session shows "saved token isn't authenticating" immediately instead
        # of leaving stale green until the user happens to retry the failed action.
        api_auth.register_auth_state_listener(_on_auth_state)

        # The dashboard kicks off _maybe_auto_check_new_releases on load, but
        # a headless box nobody opens would never check at all, making
        # NEW_RELEASE_CHECK_INTERVAL a dead letter exactly where it matters
        # most.
        async def _auto_check_ticker():
            loop = asyncio.get_running_loop()
            while True:
                await asyncio.sleep(900)
                try:
                    await loop.run_in_executor(None, _maybe_auto_check_new_releases)
                except Exception as e:
                    _log.warning("background new-release tick failed: %s", e)
                if not run_startup_maintenance:
                    continue
                # Retention is a promise made in the diagnostics list ("cleared
                # automatically after N days"), and a container that is never
                # restarted would never keep it. The sweep stamps itself and
                # returns early inside a day, so ticking it is nearly free.
                try:
                    swept = await loop.run_in_executor(
                        None, _sweep_upgrade_backups)
                    if swept:
                        _log.info("Cleaned up %s.",
                                  plural(swept, "stale upgrade backup"))
                except Exception as e:
                    _log.debug("upgrade-backup cleanup error: %s", e)

        # Always armed: the helper reads NEW_RELEASE_CHECK_INTERVAL live, so a
        # Settings change (off to on, or a new interval) takes effect at the next
        # tick without a restart.
        ticker = asyncio.create_task(_auto_check_ticker())
        if cfg.NEW_RELEASE_CHECK_INTERVAL > 0:
            hours = max(1, round(cfg.NEW_RELEASE_CHECK_INTERVAL / 3600))
            _log.info("New-release checks run in the background every "
                      "%s; set NEW_RELEASE_CHECK_INTERVAL=0 to turn them off.",
                      plural(hours, "hour"))
        yield
    finally:
        await _finish_web_lifespan(
            ticker, lock_retry_task, maintenance_task, token_probe_task)


def _classify_token(token):
    """Test a token without publishing evidence for an unsaved credential."""
    return api_client.probe_qobuz(token, report_auth=False)


def _on_auth_state(evidence: AuthEvidence) -> None:
    """Apply evidence only when it belongs to the active saved credential."""
    global _TOKEN_GENERATION, _TOKEN_VALID
    if evidence.outcome not in {
        AuthOutcome.ACCEPTED,
        AuthOutcome.REJECTED,
        AuthOutcome.ENTITLEMENT,
    }:
        return
    with _auto_check_lock:
        if _SHUTTING_DOWN:
            return
        credentials = _credentials_snapshot()
        if not credentials.configured \
                or evidence.generation != credentials.generation:
            return
        valid = evidence.outcome in {
            AuthOutcome.ACCEPTED,
            AuthOutcome.ENTITLEMENT,
        }
        _TOKEN_VALID = valid
        _TOKEN_GENERATION = evidence.generation
        if (not valid and evidence.generation
                not in _AUTH_LOSS_NOTIFIED_GENERATIONS):
            _AUTH_LOSS_NOTIFIED_GENERATIONS.add(evidence.generation)
            job_mgr.fire_auth_lost_hook()


def _token_valid_for(credentials=None) -> bool | None:
    credentials = credentials or _credentials_snapshot()
    if not credentials.configured:
        return None
    if _TOKEN_GENERATION is not None \
            and _TOKEN_GENERATION != credentials.generation:
        return None
    return _TOKEN_VALID


def _qobuz_access(access: QobuzAccess):
    credentials = _credentials_snapshot()
    return qobuz_capability(
        access,
        credentials,
        auth_valid=_token_valid_for(credentials),
    )


def _qobuz_ready() -> bool:
    """True when Qobuz-dependent UI actions are worth offering."""
    return _qobuz_access(QobuzAccess.CATALOGUE_ACTION).allowed


async def _probe_token():
    """One-shot startup check that the saved token still authenticates.

    Sets ``_TOKEN_VALID`` to True/False/None: None means the result is
    inconclusive (no token saved, or the probe couldn't reach Qobuz), so
    the dashboard treats it as "don't nag yet."
    """
    credentials = _credentials_snapshot()
    if not credentials.configured:
        return
    try:
        verdict = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, lambda: api_client.call_within(cfg.WEB_TEST_AUTH_TIMEOUT,
                                          _classify_token,
                                          credentials.token)),
            timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        verdict = AuthOutcome.TEMPORARY
    if verdict in {
        AuthOutcome.ACCEPTED,
        AuthOutcome.REJECTED,
        AuthOutcome.ENTITLEMENT,
    }:
        _on_auth_state(AuthEvidence(credentials.generation, verdict))

_here = Path(__file__).parent
templates = Jinja2Templates(directory=str(_here / "templates"))


templates.env.globals["app_version"] = __version__
templates.env.globals["repo_url"] = "https://github.com/jarynclouatre/qobuz-librarian"
templates.env.globals["release_title"] = job_mgr.release_title
# Server epoch at render, so a live elapsed clock can tick from a client-side
# baseline instead of trusting the browser's wall clock against a server epoch.
templates.env.globals["now_ts"] = time.time
# Callable, not a snapshot: the toggle lives in Settings and the downsample
# warnings have to describe whichever mode is active when the page renders.
def _downsample_originals_choice():
    return settings_store.current().get("DOWNSAMPLE_KEEP_ORIGINALS")


templates.env.globals["keeps_ds_originals"] = (
    lambda: _downsample_originals_choice() == "keep"
)
templates.env.globals["ds_originals_chosen"] = (
    lambda: _downsample_originals_choice() in ("keep", "delete")
)
templates.env.globals["backup_retention_days"] = cfg.UPGRADE_BACKUP_RETENTION_DAYS


def _recovery_on_disk(recovery) -> bool:
    """Whether a Repair job's kept-originals folder is still where its record
    says. Drives the job page's pointer honesty: Settings → Diagnostics only
    lists folders it can see, so a job must not send the user there for one
    that is gone. Only a folder whose PARENT is present but which itself
    isn't counts as gone: an unmounted volume makes the whole tree
    disappear without any OSError, and that must read as "can't tell", not
    as licence to clear the alarm."""
    try:
        p = Path(str((recovery or {}).get("location") or ""))
        if recovery.get("kind") == "migration":
            # The kept file can sit in a private folder removed with it, so
            # the album folder is what shows the library is still mounted.
            anchor = str(recovery.get("album_dir") or "")
            return p.exists() or not (
                p.parent.is_dir() or (anchor and Path(anchor).is_dir()))
        if p.is_dir():
            return True
        return not p.parent.is_dir()
    except OSError:
        return True


def _recovery_missing(recovery) -> bool:
    """Whether an exact recovery folder is gone under a mounted parent."""
    return not _recovery_on_disk(recovery)


templates.env.globals["recovery_on_disk"] = _recovery_on_disk


def _retire_gone_recoveries(rows: list[dict]) -> list[dict]:
    """Drop the History recoveries whose kept folders are confirmed gone.

    The job page checks the disk; History only read the record, so a backup
    restored or cleaned up elsewhere stayed pinned to page one under a red
    chip, pointing at a Diagnostics panel with nothing in it. Retiring the
    record where History reads it settles both screens, and the nav's
    attention count with them, without a press per stale scan."""
    kept = []
    for row in rows:
        if (
            row.get("attention") == "recovery"
            and row.get("recoveries")
            and not any(_recovery_on_disk(r) for r in row["recoveries"])
        ):
            job = (job_mgr.registry.get(row["id"])
                   or job_mgr.load_historical_job(row["id"]))
            if job is not None and job_persistence.acknowledge_missing_recoveries(
                job, _recovery_missing
            ):
                continue
        kept.append(row)
    return kept


def _fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def _fmt_elapsed(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _quality_shortfall_view(record):
    if not isinstance(record, dict) or record.get("version") != 1:
        return {}

    def label(value):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return ""
        bits, rate = value
        if (
            type(bits) not in (int, float)
            or type(rate) not in (int, float)
            or not math.isfinite(bits)
            or not math.isfinite(rate)
            or bits <= 0
            or rate <= 0
        ):
            return ""
        return f"{bits}-bit / {rate / 1000:g} kHz"

    target = label(record.get("target"))
    if not target:
        return {}
    source = label(record.get("source"))
    served = label(record.get("served"))
    n_below = record.get("n_below") or 0
    n_unknown = record.get("n_unknown") or 0
    affected = []
    if n_below:
        affected.append(
            f"{n_below} {'track was' if n_below == 1 else 'tracks were'} below target"
        )
    if n_unknown:
        affected.append(
            f"{n_unknown} {'track could' if n_unknown == 1 else 'tracks could'} not be measured"
        )
    return {
        "target": target,
        "source": source,
        "served": served,
        "affected": "; ".join(affected),
        "retry": (
            "The automatic highest-source retry still finished below target."
            if record.get("retried")
            else "No automatic retry was available for this download."
        ),
    }


_LOG_POINTER_RE = re.compile(r"\s*[;.]?\s*(see the log|see job log)\.?\s*$",
                             re.IGNORECASE)


def _strip_log_pointer(message, log_lines):
    """Drop a trailing "see the log" from a message when there is no log."""
    if log_lines:
        return message
    return _LOG_POINTER_RE.sub("", message or "").strip() or message


templates.env.globals["fmt_clock"] = _fmt_clock
templates.env.globals["fmt_elapsed"] = _fmt_elapsed
templates.env.globals["quality_shortfall_view"] = _quality_shortfall_view
templates.env.filters["strip_log_pointer"] = _strip_log_pointer
templates.env.globals["auth_active"] = web_auth.auth_active

static_dir = _here / "static"
static_dir.mkdir(exist_ok=True)


def _asset_version() -> str:
    """Cache-bust key derived from every file served under /static.

    The service worker handles that whole tree cache-first, so any changed,
    added, or removed file must rotate its cache. The semantic app_version is
    for display only.
    """
    h = hashlib.sha256()
    for path in sorted(static_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        relative = path.relative_to(static_dir).as_posix().encode("utf-8")
        h.update(relative)
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()[:12] or __version__


_ASSET_VERSION = _asset_version()
templates.env.globals["asset_version"] = _ASSET_VERSION


def _lockout_notice(ip, username="", *, after_failure=False) -> str:
    """How long the login throttle still refuses wrong guesses, or "" when it
    doesn't.

    The GET and the POST share it: a locked-out visitor was shown a normal
    form and only found out by filling it in and submitting again.
    """
    left = web_auth.login_lockout_remaining(ip, username)
    if left <= 0:
        return ""
    mins = max(1, (left + 59) // 60)
    lead = "" if after_failure else "Too many failed attempts. "
    return (f"{lead}Try again in {mins} minute{'s' if mins != 1 else ''}, or "
            "restart Qobuz Librarian to clear it.")


def _tr(request, name, context, *, status_code=200, review_badge_ack=None):
    """TemplateResponse wrapper for Starlette 1.0+ signature.

    The navbar badge is computed once per full-page render and injected via
    context; partial-fragment renders skip this entirely. A route that already
    fetched the active job list for its own template (`/queue`, the dashboard)
    can pass it as `pending` and the badge derives from that, with no second
    `pending_and_running()` call on the same render.
    """
    if "pending_job_count" not in context:
        active = context.get("pending") or job_mgr.registry.pending_and_running()
        # The badge counts work in flight, not parked reviews; those sit for
        # weeks by design and have their own review-ready dots, so counting
        # them would pin a permanent "1" to the Queue tab.
        in_flight = [j for j in active
                     if j.status != job_mgr.JobStatus.AWAITING_REVIEW]
        context.setdefault("pending_job_count", len(in_flight))
    context.setdefault("cli_mode", _CLI_MODE)
    context.setdefault("lock_unenforceable", _LOCK_UNENFORCEABLE)
    # Every tool page offered its Start button while writes were paused and let
    # the POST bounce the user onto a 503. Refuse at offer time, not submit time.
    context.setdefault("writes_paused", _web_writes_paused())
    # Terminal mode is one of eight causes, so carry the true one rather than
    # letting each gated control name the same guess. The full notice travels
    # with it: a greyed button explains itself in a title attribute, which a
    # phone never shows, so every page that greys something can say why.
    if context["writes_paused"]:
        paused = _writes_paused_notice()
        context.setdefault("writes_paused_notice", paused)
        context.setdefault(
            "writes_paused_reason",
            paused["reason"] if paused else "Downloads and scans are paused.",
        )
    # Error/utility renders (e.g. the 404 page) don't name a nav section; an
    # explicit empty page just leaves every nav link inactive instead of
    # relying on Jinja's undefined-is-falsey behaviour.
    context.setdefault("page", "")
    # Standing health the navbar surfaces on every page, not just the dashboard:
    # a rejected token (auth lost mid-session) and a lock held by another
    # instance both stop downloads, and a user on Search/Queue shouldn't only
    # find out when a job fails. Both are cheap module-level flags, no I/O.
    credentials = _credentials_snapshot()
    creds_ok = credentials.configured
    context.setdefault("qobuz_ready", _qobuz_ready())
    context.setdefault("health_qobuz_missing", not creds_ok)
    context.setdefault(
        "health_token_invalid",
        _token_valid_for(credentials) is False,
    )
    context.setdefault("health_lock_busy", bool(_LOCK_BUSY_PID))
    context.setdefault("upgrade_available", _upgrade_available(creds_ok))
    context.setdefault("discover_available", _discover_available(creds_ok))
    if review_badge_ack:
        surface, generation = review_badge_ack
        if (surface in review_badges.SURFACES
                and (surface != "upgrade" or context["upgrade_available"])):
            review_badges.mark_seen(surface, generation)
    badges = review_badges.snapshot()
    if not context["upgrade_available"]:
        badges = dict(badges)
        badges["upgrade"] = False
    if badges.get("upgrade") or badges.get("downsample"):
        # A dot promises candidates are ready. A saved view the generation
        # authority holds stale has none to show, so it must not carry one.
        # Library keeps its dot: its review still renders, with a caveat.
        authority = generation_state.load()
        badges = dict(badges)
        for surface in ("upgrade", "downsample"):
            if badges.get(surface) and not generation_state.output_is_current(
                surface, state=authority
            ):
                badges[surface] = False
    context.setdefault("nav_review_badges", badges)
    attention_count = job_persistence.attention_count()
    context.setdefault(
        "history_attention",
        ({"count": attention_count, "href": "/queue/history?attention=1"}
         if attention_count else None),
    )
    if name in {"job.html", "_job_body.html"}:
        job = context.get("job")
        holder_id = _startup_recovery_web_job_id()
        context.setdefault("recovery_holder_job_id", (
            holder_id
            if (job.kind == "download" and job.attention == "recovery"
                and not job.recoveries and holder_id != job.id)
            else None
        ))
        nav_page, return_href, return_label = _job_nav_destination(job)
        context.setdefault("job_nav_page", nav_page)
        context.setdefault("job_return_href", return_href)
        context.setdefault("job_return_label", return_label)
        context.setdefault("job_pending_review", _pending_new_release_review(job))
        context.setdefault(
            "downsample_originals_choice",
            (
                _downsample_originals_choice()
                if getattr(job, "execute_kind", "") == "downsample"
                else None
            ),
        )
    if name in {"job.html", "_job_body.html", "history.html"}:
        context.setdefault("retry_can_queue", _retry_can_queue)
        context.setdefault(
            "durable_recovery_control",
            _durable_recovery_control(),
        )
    if name in {"job.html", "_job_body.html", "queue.html", "index.html"}:
        context.setdefault(
            "cancel_protected_job_id",
            job_mgr.durable_recovery_job_id(),
        )
    return templates.TemplateResponse(request=request, name=name,
                                      context=context, status_code=status_code)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


# What a redirect tells the page it lands on. The URL carries a random key and
# the wording stays here, so a link cannot make a page show text the app never
# wrote.
_NOTICE_TTL = 600
_MAX_NOTICES = 256
_notices: dict[str, tuple[float, str]] = {}
_notices_lock = threading.Lock()


def _notice_key(text) -> str:
    """Hold ``text`` for the page a redirect lands on; returns its URL key."""
    now = time.monotonic()
    key = secrets.token_urlsafe(12)
    with _notices_lock:
        for old in [k for k, (until, _) in _notices.items() if until <= now]:
            del _notices[old]
        while len(_notices) >= _MAX_NOTICES:
            del _notices[next(iter(_notices))]
        _notices[key] = (now + _NOTICE_TTL, str(text))
    return key


def _notice_text(key) -> str:
    """The notice held under ``key``, or "" for anything else."""
    with _notices_lock:
        held = _notices.get(str(key or ""))
    if held is None or held[0] <= time.monotonic():
        return ""
    return held[1]


def render_error_page(request, code, title, msg):
    """Render the app's styled error page from routes or middleware.

    A visitor with no session gets the sign-in shell instead: the app shell
    would hand them the full nav and a Log out button with no way back to the
    login form. Every error render goes through here so the choice is made
    once, including the CSRF refusal, which is raised before the auth gate
    runs and so is the one page that can reach a signed-out browser.
    """
    target = web_auth.signed_out_target(request)
    if target:
        return templates.TemplateResponse(
            request=request, name="error_auth.html",
            context={"title": title, "msg": msg, "target": target,
                     "action": ("Set up your login"
                                if target == web_auth.SETUP_PATH
                                else "Back to sign in")},
            status_code=code)
    return _tr(request, "error.html",
               {"code": code, "title": title, "msg": msg}, status_code=code)


# Serialises the dedupe-check-then-submit in queue_download: the network
# get_album() await between the early check and the submit leaves a window where
# two requests for one album both pass the check and queue it twice.
_DOWNLOAD_SUBMIT_LOCK = threading.Lock()


def _find_job_touching_album(album_id: str, skip_single_track: bool = False):
    """Return a pending/running job that already covers album_id, either as
    its direct subject or as one of its candidates.

    Reviews don't count, parked or still being built: an album merely
    listed among a review's candidates isn't queued for anything, so
    refusing an explicit download with "already queued" over it would be
    false, and with a whole-library review its candidates are exactly the
    albums the user is most likely to search for. Approve re-checks the
    disk and drops candidates that landed in the meantime, so downloading
    now can't double up later.

    ``skip_single_track`` ignores one-track downloads, so a full-album
    download doesn't fold onto a job that only downloaded one track."""
    for j in job_mgr.registry.pending_and_running():
        if j.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        if skip_single_track and (getattr(j, "single", None) or {}).get("track_id"):
            continue
        if j.album_id == album_id:
            return j
        if j.status == job_mgr.JobStatus.SCANNING:
            # Still collecting proposals. They are no more queued than a
            # parked review's, and saying "already queued" over one told the
            # user their download had happened when nothing was queued.
            continue
        # Snapshot: an approved job appends to candidates from the worker
        # thread, and iterating it live can raise "list changed size".
        for cand in list(j.candidates or []):
            payload = cand.get("payload") or {}
            if payload.get("album_id") == album_id:
                return j
            qa = (payload.get("candidate") or {}).get("qobuz_album") or {}
            if qa.get("id") == album_id:
                return j
    return None


def _duplicate_download_job(album_id: str, track_id: str = "",
                            as_new_edition: bool = False):
    """The already-active job a new /download should fold onto, or None to let it
    queue. Matched by intent, not album id alone: "get this edition too" is a
    deliberate extra copy and never folds; a single-track download folds only onto an
    identical one; a normal full-album download folds onto another full-album job,
    but not onto a one-track download from the same album, and not onto a
    review's candidate, which is a proposal rather than queued work."""
    if as_new_edition:
        # "Get this edition too" is a deliberate extra copy of an owned album,
        # so it skips folding onto scans and normal downloads, but two
        # identical new-edition submits are the same tap twice, not two
        # deliberate editions. Fold onto an in-flight one.
        for j in job_mgr.registry.pending_and_running():
            if (j.album_id == album_id
                    and (getattr(j, "execute_args", None) or {}).get("new_edition")):
                return j
        return None
    if track_id:
        for j in job_mgr.registry.pending_and_running():
            s = getattr(j, "single", None) or {}
            if s.get("album_id") == album_id and s.get("track_id") == str(track_id):
                return j
        return None
    return _find_job_touching_album(album_id, skip_single_track=True)


def _active_search_downloads() -> tuple[
        set[str], set[tuple[str, str]], set[str]]:
    albums = set()
    tracks = set()
    scanning_albums = set()
    for job in job_mgr.registry.pending_and_running():
        if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        single = getattr(job, "single", None) or {}
        track_id = str(single.get("track_id") or "")
        album_id = str(single.get("album_id") or job.album_id or "")
        if album_id and track_id:
            tracks.add((album_id, track_id))
        elif album_id:
            albums.add(album_id)
        for candidate in list(job.candidates or []):
            payload = candidate.get("payload") or {}
            candidate_album = payload.get("album_id")
            if not candidate_album:
                candidate_album = (
                    (payload.get("candidate") or {}).get("qobuz_album") or {}
                ).get("id")
            if candidate_album:
                candidate_album = str(candidate_album)
                if job.status == job_mgr.JobStatus.SCANNING:
                    scanning_albums.add(candidate_album)
                elif candidate.get("selected"):
                    albums.add(candidate_album)
    scanning_albums.difference_update(albums)
    return albums, tracks, scanning_albums


def _album_tracks_complete(album: dict) -> bool:
    """Whether this payload carries the album's whole track list.

    A truncated list makes everything on disk look present. tracks.total
    counts the list itself; tracks_count is album metadata, used only when
    the payload carries no count of its own.
    """
    tracks = album.get("tracks") or {}
    items = tracks.get("items") or []
    total = tracks.get("total")
    if total is None:
        total = album.get("tracks_count")
    if not items or total is None:
        return False
    try:
        return int(total) == len(items) and int(tracks.get("offset") or 0) == 0
    except (TypeError, ValueError):
        return False


def _same_edition_is_complete(album: dict) -> bool:
    """Prove that this exact release year is already complete on disk.

    The ordinary album resolver may fall back to a similarly named folder.
    That is useful for gap detection, but it is not enough to refuse a
    deliberate second edition. Require the submitted release year to match
    the resolved folder before comparing its complete track list.
    """

    try:
        folder = catalog.find_album_dir_filesystem(album)
        release_year = catalog.album_year(album)
        if (
            folder is None
            or not release_year
            or str(catalog._dir_year(folder.name) or "") != str(release_year)
        ):
            return False
        existing, _ = catalog.find_existing_tracks(album, album_dir=folder)
        wanted = (album.get("tracks") or {}).get("items") or []
        return bool(existing and _album_tracks_complete(album)) and not catalog.compute_missing(
            wanted, existing)[0]
    except Exception:
        _log.exception(
            "edition ownership check failed for album %s", album.get("id"))
        return False


# Reentrant so the auto-triggers (which hold it) can call the _start_* helpers
# below (which re-acquire it).
_auto_check_lock = threading.RLock()


def _begin_direct_library_operation(label):
    """Atomically gate, register, and lock a request-owned library mutation."""
    with _auto_check_lock:
        if _web_writes_paused():
            return "paused", None, None
        token = job_mgr.begin_library_operation(label)
        if token is None:
            return "paused", None, None
        lock = job_mgr.staging_lock()
        if not lock.acquire(blocking=False):
            job_mgr.end_library_operation(token)
            return "busy", None, None
        return "ok", token, lock


def _sweep_upgrade_backups():
    """Expire old upgrade backups when nothing else is writing to the library.

    Gives up rather than waiting if the library is busy: the sweep only does
    real work once a day, and the next tick is fifteen minutes away.
    """
    state, operation_token, lock = _begin_direct_library_operation(
        "Backup retention")
    if state != "ok":
        return 0
    try:
        return backup_mod.cleanup_old_upgrade_backups()
    finally:
        lock.release()
        job_mgr.end_library_operation(operation_token)


def _active_new_release_check():
    """A new-release check queued or crawling right now, or None, so a second
    one isn't stacked on top of it. A check whose list is merely parked for
    review does NOT block a fresh one: the fresh check folds its finds into
    that list (flows._append_to_parked_new_release_review), so asking again is
    always allowed and never costs the user the ticks already made."""
    for j in job_mgr.registry.pending_and_running():
        if getattr(j, "execute_kind", "") != "new_releases":
            continue
        if j.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return j
    return None


def _pending_new_release_review(job):
    """The new-release review still parked after a partial download, for the
    finished job page to point at. New releases have no home surface of their
    own, so a review split off the batch the user approved was reachable only
    from the dashboard notice or History: the page they were standing on gave
    them no way back to the rest of their own results."""
    if getattr(job, "execute_kind", "") != "new_releases":
        return None
    if getattr(getattr(job, "status", None), "value", "") not in (
            "done", "failed", "canceled"):
        return None
    other = None
    for j in job_mgr.registry.awaiting_review():
        if getattr(j, "execute_kind", "") == "new_releases" and j.id != job.id:
            other = j
            break
    if other is None:
        return None
    return {
        "href": f"/jobs/{other.id}",
        "label": f"{plural(len(other.candidates), 'new release')} still to review",
    }


def _start_new_release_check(credentials):
    """Submit a whole-library new-release check and return the job (or the one
    already queued). Shared by the manual Library-page option and the automatic
    dashboard trigger."""
    with _auto_check_lock:
        # The run-lock may have been handed to the terminal mid-submit (this
        # can run in an executor for POST /library).
        if _web_writes_paused():
            return None
        existing = _active_new_release_check()
        if existing is not None:
            return existing
        job = job_mgr.Job(title="New releases")
        job.execute_kind = "new_releases"

        def _scan(j):
            active = _authorize_qobuz_live(
                QobuzAccess.CATALOGUE_ACTION,
                expected_generation=credentials.generation,
            )
            flows.scan_new_releases(j, active.token)

        return job_mgr.submit_scan(
            job,
            _scan,
            _resume_album_download(job, job.execute_args),
        )


# A failed live check holds off the next automatic one: five minutes, doubling
# per failure up to an hour. An outage then costs one check per window instead
# of one per dashboard load.
_AUTO_START_RETRY_FIRST = 300.0
_AUTO_START_RETRY_MAX = 3600.0
_auto_start_backoff = {"until": 0.0, "delay": 0.0}


def _auto_start_credentials():
    """The live Qobuz check before an automatic start, or None when it fails
    or a recent failure is still being backed off."""
    if time.time() < _auto_start_backoff["until"]:
        return None
    try:
        credentials = _authorize_qobuz_live(QobuzAccess.CATALOGUE_ACTION)
    except (
        NoCredsError,
        AuthLost,
        QobuzUnavailable,
        QobuzEntitlementError,
        CredentialChanged,
    ):
        delay = min(max(_auto_start_backoff["delay"] * 2,
                        _AUTO_START_RETRY_FIRST), _AUTO_START_RETRY_MAX)
        _auto_start_backoff.update(until=time.time() + delay, delay=delay)
        return None
    _auto_start_backoff.update(until=0.0, delay=0.0)
    return credentials


def _new_release_check_due():
    """Whether the automatic new-release check should run, from local state
    alone: nothing here touches the network."""
    if cfg.NEW_RELEASE_CHECK_INTERVAL <= 0 or _web_writes_paused():
        return False
    # Don't bother (or thrash) when there's no token, or one we already know
    # Qobuz is rejecting; it would just fail on the first call every load.
    if not _qobuz_ready():
        return False
    # Only after a full library scan has established the baseline; otherwise the
    # check would crawl every artist just to record a starting point and surface
    # nothing. A completed library scan seeds it (flows.scan_library).
    if not new_releases.is_baseline_complete():
        return False
    # And never ahead of an interrupted library scan waiting to resume: finishing
    # that takes priority (it's what the user's resume needs the scan lane for),
    # and a delta check can wait until the library is whole again.
    if scan_checkpoint.pending() is not None:
        return False
    if time.time() < _auto_start_backoff["until"]:
        return False
    # Avoid a network probe until the interval says a run is due. This first
    # read is repeated under the lock after the probe.
    return _new_release_interval_elapsed()


_new_release_submitted_at = 0.0


def _new_release_interval_elapsed():
    """A saved run time ahead of the clock counts as no run, and this
    process's own last submit counts even when the data folder could not
    take the stamp."""
    now = time.time()
    last = new_releases.last_run() or 0.0
    stamps = [t for t in (last, _new_release_submitted_at) if t <= now]
    return now - max(stamps, default=0.0) >= cfg.NEW_RELEASE_CHECK_INTERVAL


def _maybe_auto_check_new_releases():
    """Quietly run the new-release check on dashboard load when it's due.

    Read-only (it only parks a review list, never downloads), so it's safe to
    fire from a GET. Skipped when the check is off, the token is missing or
    known-bad, the CLI holds the lock, another job is actively working, or the
    interval hasn't elapsed. A list already parked for review does NOT stop it:
    the run folds its finds into that list, so the timer keeps the review
    current instead of going quiet until the list is cleared.
    """
    global _new_release_submitted_at
    if not _new_release_check_due():
        return
    credentials = _auto_start_credentials()
    if credentials is None:
        return
    # Serialise the check-and-submit so two concurrent dashboard loads can't
    # both pass the gate and queue the check twice.
    with _auto_check_lock:
        active = job_mgr.registry.pending_and_running()
        working = any(j.status != job_mgr.JobStatus.AWAITING_REVIEW for j in active)
        if working:
            return
        if not _new_release_interval_elapsed():
            return
        # Stamp the attempt before submitting: the scan only advances the stamp
        # on a clean finish, so without this a failed/cancelled run would re-fire
        # on every load.
        _new_release_submitted_at = time.time()
        new_releases.touch_run()
        _start_new_release_check(credentials)


def _queue_wait(job):
    """Describe what a PENDING job is waiting behind on its worker lane, so the
    UI can explain the wait instead of showing a bare "Queued". Scans share one
    worker and downloads another (see web/jobs.py), so a job only waits behind
    others in its OWN lane (job.kind: "scan" | "download"). ``position`` counts
    how many run before it (the one holding the worker + any earlier-queued).
    A job waiting behind an interrupted download instead carries
    ``paused_for``, because "starts automatically" is not true of it: nothing
    moves until that album is retried or given up.

    Returns {"ahead_title", "lane", "position", "paused_for"} or None when
    nothing's ahead, i.e. it's about to start, so there's nothing to explain."""
    if job.status != job_mgr.JobStatus.PENDING:
        return None
    holder = None
    ahead = 0
    for j in job_mgr.registry.all():
        if j.id == job.id or j.kind != job.kind:
            continue
        if j.status in (job_mgr.JobStatus.SCANNING, job_mgr.JobStatus.RUNNING):
            holder = j
        elif (j.status == job_mgr.JobStatus.PENDING
              and (j.created_at or 0) < (job.created_at or 0)):
            ahead += 1
    paused_for = None
    if _recovery_pause_is_another_download(job):
        paused_for = {
            "album": _startup_recovery_album_label(),
            "job_id": _startup_recovery_web_job_id(),
        }
    if holder is None and ahead == 0 and paused_for is None:
        return None
    return {
        "ahead_title": holder.title if holder else "",
        "lane": job.kind,
        "position": ahead + (1 if holder else 0),
        "paused_for": paused_for,
    }


def _music_root_hint() -> str:
    """Where the path in a music-folder message actually comes from. Inside the
    image it is the mount point, not anything the user typed, so naming the path
    alone sends them hunting for a folder their machine does not have."""
    if cfg.in_container():
        return ("That is the path inside the container: check which folder "
                "QL_MUSIC_DIR maps onto it in your .env.")
    return "Set MUSIC_ROOT to the folder that holds your artist folders."


def _music_write_target_message(state: str, recorded_albums: int = 0, *,
                                job_started: bool = False,
                                diagnostic: bool = False) -> str:
    root = Path(cfg.MUSIC_ROOT)
    hint = _music_root_hint()
    ending = "" if diagnostic else (
        "The download stopped before writing any files."
        if job_started else "Nothing was queued."
    )

    def finish(message):
        return f"{message} {ending}".rstrip()

    if state == "missing":
        return finish(f"{root} does not exist. {hint}")
    if state == "not_folder":
        return finish(f"{root} is not a folder. {hint}")
    if state == "unreadable":
        return finish(f"{root} could not be read. {hint}")
    if state == "backup_unreadable":
        return finish(
            f"No artist folders were found in {root}, and the last collection "
            "backup could not be read safely. Check that the music folder is "
            "mounted and that the collection-backup folder is readable."
        )
    if state == "recorded_empty":
        return finish(
            f"No artist folders were found in {root}, but the last collection "
            f"backup recorded {recorded_albums:,} "
            f"{'album' if recorded_albums == 1 else 'albums'}. Check that the "
            "music folder is mounted. If those albums really are gone, open "
            "Settings → Collection backup, choose Back up now, then Replace "
            "anyway before retrying."
        )
    return ""


def _require_music_write_target_for_job() -> None:
    state, recorded_albums = collection_snapshot.music_root_write_state()
    if state != "ready":
        raise RuntimeError(_music_write_target_message(
            state, recorded_albums, job_started=True))


def _qobuz_quality_bits_rate(primary: dict | None,
                             fallback: dict | None = None) -> tuple[int, int]:
    """Return Qobuz source quality as (bits, sample_rate_hz)."""
    primary = primary if isinstance(primary, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    bits = primary.get("maximum_bit_depth") or fallback.get("maximum_bit_depth") or 0
    rate = (primary.get("maximum_sampling_rate")
            or fallback.get("maximum_sampling_rate") or 0)
    try:
        bits_i = int(bits)
    except (TypeError, ValueError, OverflowError):
        bits_i = 0
    try:
        rate_f = float(rate)
    except (TypeError, ValueError, OverflowError):
        rate_f = 0.0
    if not math.isfinite(rate_f) or rate_f <= 0:
        rate_f = 0.0
    if bits_i <= 0:
        bits_i = 0
    if 0 < rate_f < 1000:
        rate_f *= 1000
    return bits_i, int(round(rate_f))


_DOWNLOAD_SUMMARY_LABELS = {
    "already_complete": "Album already complete. Nothing to download.",
    "skipped_already_higher_quality": "Skipped: the library already has higher quality.",
    "skipped_has_extras": "Skipped: the library copy includes extra tracks.",
    "upgrade_only_no_op": "Already at or above the target quality.",
    "upgrade_no_local_tracks": "This album isn't in your library any more.",
    "dry_run": "Dry run. Nothing downloaded.",
    "user_skipped": "Skipped at confirmation.",
    "lossy_only": "Qobuz only had lossy versions. Nothing downloaded.",
    "no_tracks": "Qobuz returned no tracks for this album.",
    "cancelled": "Cancelled. Nothing was imported.",
    "incomplete": "Qobuz couldn't deliver the whole album. Nothing was imported.",
    "upgrade_aborted_backup_failed": "Upgrade aborted: couldn't back up the original.",
    "stale_candidate": (
        "The album's local files changed or could not be read before the "
        "download started, so nothing was downloaded. Try again."
    ),
    "replacement_aborted_catalogue_failed": (
        "The album's Beets entries could not be read, so the replacement "
        "was not made. Your files are unchanged."
    ),
    "not_imported": "Downloaded, but the import didn't land. Library unchanged.",
}


def _summarize_download_result(r):
    """One-line job summary from process_album's result dict.

    Picks a phrase per result kind for the documented non-success branches,
    or builds the "N tracks downloaded" tally for an actual rip. Returns
    "" if there's nothing useful to say (process_album returned None / {})."""
    if not r:
        return ""
    kind = r.get("result")
    if kind == "cancelled" and (
        r.get("catalogue_unverified")
        or r.get("recovery_unverified")
        or r.get("upgrade_unverified")
    ):
        return "Cancelled. Nothing was imported. A safety backup was retained."
    if kind == "partial":
        landed = plural(r.get("n_ok", 0), "track")
        if not r.get("imported"):
            summary = (
                f"{landed} downloaded, but the incomplete album was not "
                "imported."
            )
            if (
                r.get("catalogue_unverified")
                or r.get("recovery_unverified")
                or r.get("upgrade_unverified")
            ):
                summary += " A safety backup was retained for review."
            return summary
        parts = [f"{landed} downloaded"]
        if r.get("catalogue_unverified"):
            parts.append("Beets catalogue needs attention; backup retained")
        elif r.get("recovery_unverified"):
            parts.append("recovery could not be verified; backup retained")
        elif r.get("upgrade_unverified"):
            parts.append("upgrade could not be verified; original backup retained")
        else:
            verdict = r.get("quality_verdict") or {}
            if verdict.get("under") and not verdict.get("recovered"):
                parts.append("highest-source retry remained below target quality")
            if r.get("downsample_errors"):
                parts.append(
                    f"{plural(r['downsample_errors'], 'file')} could not be "
                    "downsampled"
                )
            if r.get("downsample_flush_warnings"):
                parts.append(
                    f"{plural(r['downsample_flush_warnings'], 'rewritten file')} "
                    "could not be confirmed flushed"
                )
            if r.get("downsample_cancelled"):
                parts.append("post-download downsample stopped early")
            if r.get("consolidation_interrupted"):
                parts.append("duplicate cleanup stopped early")
            retryable, lossy_only = download_result.incomplete_track_counts(r)
            if r.get("siblings_preserved") and not (retryable or lossy_only):
                parts.append("sibling cleanup needs review")
        return ", ".join(parts) + "."
    if kind in _DOWNLOAD_SUMMARY_LABELS:
        return _DOWNLOAD_SUMMARY_LABELS[kind]
    if not r.get("imported"):
        return ""
    n_ok = r.get("n_ok", 0)
    n_fail = r.get("n_fail", 0)
    n_lossy = r.get("n_lossy", 0)
    parts = [f"{plural(n_ok, 'track')} downloaded"]
    if n_fail:
        parts.append(f"{n_fail} failed")
    if n_lossy:
        parts.append(f"{n_lossy} lossy-dropped")
    if r.get("catalogue_unverified"):
        parts.append("Beets catalogue needs attention; backup retained")
    elif r.get("recovery_unverified"):
        parts.append("recovery backup retained for review")
    elif r.get("upgrade_unverified"):
        parts.append("upgrade couldn't be verified; original kept")
    elif r.get("auto_upgrade"):
        parts.append("auto-upgrade verified")
    return ", ".join(parts) + "."


def _undeliverable_album_error(r, album=None):
    """Say how short the album came, and what the user can do about it."""
    if album is not None and download.disc_names_overwrite(album):
        return (
            "Tracks that share a number and title on different discs "
            "overwrote each other because disc_subdirectories is off in the "
            "streamrip config, so nothing was added to your library. Turn it "
            "on and try again."
        )
    landed = r.get("n_ok", 0)
    short = (
        r.get("n_fail", 0) + r.get("n_broken", 0) + r.get("n_lossy_only", 0)
    )
    if landed and short:
        opening = f"Qobuz delivered {landed} of {plural(landed + short, 'track')}"
    else:
        opening = "Qobuz couldn't deliver every track"
    return (
        f"{opening}, so nothing was added to your library and the part that "
        "downloaded was discarded. Try again later; if it stops at the same "
        "track every time, Qobuz can't serve that track."
    )


def _mark_download_attention(job, result):
    """Mark a job failed when download details still need attention."""
    retryable, lossy_only = download_result.incomplete_track_counts(result)
    status, kind = download_result.download_job_outcome(result)
    job.status = job_mgr.JobStatus(status)
    if kind == "backup":
        job.attention = "backup"
        if not isinstance(job.execute_args, dict):
            job.execute_args = {}
        job.execute_args["retry_disabled"] = "backup"
        if result.get("catalogue_unverified"):
            job.error = (
                "The album's Beets catalogue entries could not be reconciled "
                "safely. A backup was retained. Review it under Settings > "
                "Diagnostics before downloading this album again."
            )
        elif result.get("recovery_unverified"):
            job.error = (
                "The album recovery could not be verified complete. A backup "
                "was retained. Review it under Settings > Diagnostics before "
                "downloading this album again."
            )
        else:
            job.error = (
                "The replacement could not be verified complete. Your "
                "original was retained as a backup. Review it under Settings "
                "> Diagnostics before downloading this album again."
            )
        return
    if kind == "quality":
        job_mgr.record_quality_shortfall(job, result.get("quality_verdict"))
        job.error = (
            "The album downloaded, but it still finished below the target "
            "quality after the automatic retry."
        )
        return
    if kind == "processing":
        job.attention = "processing"
        messages = []
        if result.get("downsample_errors"):
            messages.append(
                f"{plural(result['downsample_errors'], 'file')} could not be "
                "downsampled"
            )
        if result.get("downsample_flush_warnings"):
            messages.append(
                f"{plural(result['downsample_flush_warnings'], 'rewritten file')} "
                "could not be confirmed flushed to disk"
            )
        if result.get("downsample_cancelled"):
            messages.append("post-download downsampling stopped early")
        if result.get("consolidation_interrupted"):
            messages.append("duplicate cleanup stopped early")
        if result.get("siblings_preserved"):
            messages.append("sibling cleanup needs review")
        detail = "; ".join(messages) or "post-download work did not finish"
        job.error = (
            f"The album imported, but {detail}. Check the job log before "
            "retrying."
        )
        return
    if kind == "lossy":
        job.attention = "lossy"
        job.execute_args["retry_disabled"] = "lossy"
        job.error = (
            f"{plural(lossy_only, 'track')} "
            f"{'is' if lossy_only == 1 else 'are'} only available "
            "lossy on Qobuz. The album is incomplete and needs another "
            "source."
        )
        return
    job.attention = "partial"
    if retryable:
        job.error = (
            f"{plural(retryable, 'track')} "
            f"{'is' if retryable == 1 else 'are'} still missing. "
            f"Retry fetches {'it' if retryable == 1 else 'them'}."
        )
        if lossy_only:
            job.error += (
                f" {plural(lossy_only, 'track')} can only be found "
                "lossy on Qobuz and needs another source."
            )
    else:
        job.error = (
            "The album imported, but the download reported unfinished work. "
            "Check the job log before retrying."
        )


def _make_download_run(
    album,
    token,
    *,
    treat_as_new=False,
    durable_planned=None,
):
    """Return the run(j) callable used by both queue_download and job_retry.

    treat_as_new downloads the album as a brand-new one even if a different
    edition is already owned: the "get this edition too" path.
    """

    expected_generation = api_auth.token_credential_generation(token)

    def run(j):
        _require_music_write_target_for_job()
        active = None
        active_token = token
        if getattr(token, "credential_generation", ""):
            active = _authorize_qobuz_live(
                QobuzAccess.DOWNLOAD_ACTION,
                expected_generation=expected_generation,
            )
            active_token = active.token
        args = flows.build_args()
        flows._note_staging_wait(j, "Downloading", 0, 1)
        durable_failure = False
        durable_completion_settled = False
        with job_mgr.staging_lock():
            with _CREDENTIAL_LOCK:
                if (active is not None
                        and not _credential_generation_is_active(
                            active.generation)):
                    raise CredentialChanged(
                        "Qobuz credentials changed before the download began."
                    )
            durable_item = None
            if durable_planned is not None:
                if treat_as_new:
                    raise ValueError(
                        "a saved durable retry cannot change edition intent"
                    )
                durable_item = queue_state._deserialize_queue_item(
                    durable_planned
                )
                if durable_item["album"] != album:
                    raise ValueError(
                        "the saved durable retry album changed before execution"
                    )
            elif not treat_as_new and catalog.is_lossless_album(album):
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                existing, album_dir = catalog.find_existing_tracks(album)
                missing, present = catalog.compute_missing(qobuz_tracks, existing)
                candidate = queue_builder._build_queue_item(
                    album=album,
                    album_dir=album_dir,
                    label=(
                        f"{(album.get('artist') or {}).get('name') or '?'}"
                        f", {album.get('title') or '?'}"
                    ),
                    missing=missing,
                    present=present,
                    upgrade_only=False,
                    auto_upgrade=False,
                )
                if durable_album.plan_durable_new_album(candidate, args) is not None:
                    durable_item = candidate
            if durable_item is None:
                r = process_mode.process_album(album, args, allow_force=False,
                                  already_confirmed=True, token=active_token,
                                  treat_as_new=treat_as_new) or {}
            else:
                try:
                    results, drained = queue_executor._execute_download_queue(
                        [durable_item],
                        args,
                        active_token,
                        consolidate_duplicates=False,
                    )
                except BaseException:
                    # The durable executor can change the saved queue before
                    # raising.
                    try:
                        if _run_lock_intact():
                            _record_startup_recovery(_RUN_LOCK_HANDLE)
                    except BaseException as refresh_exc:
                        _log.warning(
                            "couldn't refresh durable Web recovery after an "
                            "executor failure: %s",
                            refresh_exc,
                        )
                    raise

                refresh_failed = False
                recovery = None
                try:
                    if _run_lock_intact():
                        recovery = _record_startup_recovery(
                            _RUN_LOCK_HANDLE)
                    else:
                        refresh_failed = True
                except Exception as exc:
                    refresh_failed = True
                    _log.warning(
                        "couldn't refresh durable Web recovery after the "
                        "executor returned: %s",
                        exc,
                    )
                recovery_status = getattr(
                    getattr(recovery, "status", None), "value", None)
                result = (
                    results[0]
                    if type(results) is list
                    and len(results) == 1
                    and type(results[0]) is dict
                    else None
                )
                accepted = (
                    drained is True
                    and result is not None
                    and result.get("imported") is True
                    and recovery_status == "clear"
                    and not refresh_failed
                )
                # A cancel and an undeliverable album both end with the partial
                # download discarded and nothing left waiting on recovery, so
                # neither is a durable failure.
                cancelled_clean = (
                    result is not None
                    and result.get("result") in ("cancelled", "incomplete")
                    and recovery_status == "clear"
                    and not refresh_failed
                )
                if accepted or cancelled_clean:
                    r = result
                    durable_completion_settled = accepted
                else:
                    r = result or {}
                    durable_failure = True
                    completion_acknowledged = _durable_completion_status(j)
                    # `recovery_status` is process-wide, so on its own it fails
                    # a download whose own completion is acknowledged because
                    # some other item's recovery is outstanding. Whose recovery
                    # it is decides; the completion proof is only read.
                    recovery_is_this_job = _startup_recovery_web_job_id() == j.id
                    if (
                        completion_acknowledged is True
                        and (recovery_status == "clear"
                             or not recovery_is_this_job)
                        and _run_lock_intact()
                        and _reconcile_acknowledged_job(j)
                    ):
                        # Completion crossed its durable Web acknowledgement
                        # boundary even though the executor's return was not a
                        # normal drained result.
                        return
                    retryable = (
                        completion_acknowledged is False
                        and result is not None
                        and result.get("result") == "retry"
                        and recovery_status == "resume_required"
                        and _durable_recovery_matches_job(j)
                    )
                    # History and the job page render Retry for whichever job
                    # HOLDS the durable recovery control, which is wider than
                    # `retryable`: an attention stop holds it too. Choosing the
                    # copy on the narrower test printed "cleared under Settings
                    # > Diagnostics" directly beside a working Retry button, and
                    # Diagnostics has no control for this; it only checks
                    # volumes, binaries and upgrade backups.
                    control = _durable_recovery_control()
                    holds_control = bool(control and control["job_id"] == j.id)
                    j.status = job_mgr.JobStatus.FAILED
                    if retryable:
                        j.attention = ""
                        j.error = (
                            "This download stopped before anything was "
                            "imported, so downloads and scans are paused. Use "
                            "Retry to download it again, or Give up on this "
                            "album to drop it and carry on."
                        )
                    elif holds_control:
                        j.attention = "recovery"
                        j.error = (
                            "This download couldn't be confirmed as finished "
                            "cleanly, so downloads are paused. Use Retry on "
                            "this job to settle it, or Give up on this album "
                            "to discard it and carry on."
                        )
                    else:
                        # The recovery is held by a different job, so no Retry
                        # is rendered here; send them to the one that has it.
                        j.attention = "recovery"
                        j.error = (
                            "This download couldn't be confirmed as finished "
                            "cleanly, so downloads are paused. Open the "
                            "download holding the recovery from Queue or "
                            "History and use Retry to settle it."
                        )
        status, attention = download_result.download_job_outcome(r)
        if durable_failure:
            pass
        elif attention:
            _mark_download_attention(j, r)
        elif status == "failed":
            j.status = job_mgr.JobStatus.FAILED
            retryable, lossy_only = download_result.incomplete_track_counts(r)
            if r.get("rate_limited"):
                j.error = "Qobuz rate-limited this download. Try again later."
            elif r.get("result") == "incomplete":
                j.error = _undeliverable_album_error(r, album)
            elif r.get("n_fail"):
                j.error = f"{plural(r['n_fail'], 'track')} failed. See job log."
            elif r.get("n_ok"):
                j.error = "Downloaded, but the import failed. See job log."
            elif retryable:
                j.error = (
                    f"{plural(retryable, 'track')} did not arrive as a "
                    "complete file, so nothing was added to your library. "
                    "The job log names them.")
            elif lossy_only:
                j.error = (
                    f"Qobuz offered {plural(lossy_only, 'track')} only in a "
                    "lossy format, so nothing was added to your library.")
            elif r.get("result") in _DOWNLOAD_SUMMARY_LABELS:
                j.error = _DOWNLOAD_SUMMARY_LABELS[r["result"]]
            else:
                j.error = "No tracks were retrieved. The job log says why."
        elif r.get("imported") and r.get("n_fail", 0) > 0:
            j.error = f"{plural(r['n_fail'], 'track')} failed. See job log."
        # Surface a one-line outcome here so the /jobs page tells the user what
        # happened without expanding the log.
        summary = _summarize_download_result(r)
        if summary:
            j.summary = summary
        # Claiming/completing the album the normal way graduates it out of the
        # "downloaded single" state, so the rest stops being suppressed in scans.
        if r.get("imported"):
            flows._refresh_after_local_album_change(
                album,
                r,
                fallback_artist=(album.get("artist") or {}).get("name"),
                token=active_token,
                args=args,
                upgrade=True,
                downsample=True,
            )
            # A parked library review may still offer this album, so drop it
            # there so the stale review can't download it a second time.
            flows.prune_library_review_candidates(album)
            retryable, lossy_only = download_result.incomplete_track_counts(r)
            # Nothing missing at all is what search calls "Owned"; a gap of
            # either kind leaves the row offering the download it still needs.
            j.landed_complete = not retryable and not lossy_only
            if retryable:
                flows._fold_partial_gap_fill(
                    album, (album.get("artist") or {}).get("name") or "",
                    retryable)
            hidden_mod.unmark_single(
                (album.get("artist") or {}).get("name") or "?",
                album.get("title") or "?",
                album_id=album.get("id"),
            )
        if durable_completion_settled:
            # Exact completion, external acknowledgement, carrier retirement,
            # and journal cleanup all finished before the executor returned.
            # Close the Web state under the same lock as request_cancel so a
            # late click cannot relabel the completed library mutation.
            with j._lock:
                if j.status is job_mgr.JobStatus.RUNNING:
                    j.cancel_requested = False
                    j.status = job_mgr.JobStatus.DONE
    return run


def _single_ownership_item(payload):
    """Validate the sealed one-item evidence before touching the library."""
    root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
        or payload.get("sealed") is not True
        or payload.get("root") != str(root)
        or not owned_paths._valid_ownership_identity(payload.get("root_identity"))
    ):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1:
        return None
    item = items[0]
    if not isinstance(item, dict) or not owned_paths._valid_ownership_identity(item.get("file")):
        return None
    relative = item.get("relative")
    if not isinstance(relative, str):
        return None
    file_relative = owned_paths._strict_ownership_relative(root, relative)
    if file_relative is None:
        return None
    created = item.get("created_directories")
    if not isinstance(created, list):
        return None
    seen = set()
    for record in created:
        if not owned_paths._valid_ownership_identity(record):
            return None
        directory_relative = record.get("relative")
        if not isinstance(directory_relative, str):
            return None
        directory_relative = owned_paths._strict_ownership_relative(
            root, directory_relative)
        if (
            directory_relative is None
            or len(directory_relative.parts) >= len(file_relative.parts)
            or file_relative.parts[:len(directory_relative.parts)]
            != directory_relative.parts
        ):
            return None
        identity = (record["device"], record["inode"])
        if identity in seen:
            return None
        seen.add(identity)
    companions = item.get("companions")
    if not isinstance(companions, list) or len(companions) > 1:
        return None
    artwork = None
    if companions:
        receipt = companions[0]
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"kind", "relative", "file"}
            or receipt.get("kind") != "artwork"
            or not owned_paths._valid_ownership_identity(receipt.get("file"))
            or not isinstance(receipt.get("relative"), str)
        ):
            return None
        artwork_relative = owned_paths._strict_ownership_relative(
            root, receipt["relative"])
        if (
            artwork_relative is None
            or artwork_relative == file_relative
            or len(artwork_relative.parent.parts) >= len(file_relative.parts)
            or file_relative.parts[:len(artwork_relative.parent.parts)]
            != artwork_relative.parent.parts
        ):
            return None
        artwork = {
            "path": root / artwork_relative,
            "file_identity": receipt["file"],
            "relative": artwork_relative,
        }
    return {
        "root": root,
        "path": root / file_relative,
        "file_identity": item["file"],
        "root_identity": payload["root_identity"],
        "created_directories": created,
        "artwork": artwork,
    }


def _single_owned_path(
    payload,
    landed_dir=None,
    created_after_import=None,
    created_files_after_import=None,
):
    """Bind only the exact destination reported by the one-run beets hook."""
    item = _single_ownership_item(payload)
    if item is None:
        return None
    created_directories = list(item["created_directories"])
    for record in created_after_import or ():
        if not owned_paths._valid_ownership_identity(record):
            return None
        relative = record.get("relative")
        if (not isinstance(relative, str)
                or owned_paths._strict_ownership_relative(item["root"], relative) is None):
            return None
        created_directories.append(record)
    path = item["path"]
    owned = owned_paths._bind_owned_path(
        item["root"],
        path,
        expected_file=item["file_identity"],
        expected_root=item["root_identity"],
        created_directories=created_directories,
    )
    if owned is None:
        return None
    companions = []
    artwork = item.get("artwork")
    if artwork is not None:
        artwork_relative = artwork["relative"]
        artwork_created = []
        for record in created_directories:
            directory_relative = owned_paths._strict_ownership_relative(
                item["root"], record["relative"])
            if (
                directory_relative is not None
                and len(directory_relative.parts)
                < len(artwork_relative.parts)
                and artwork_relative.parts[:len(directory_relative.parts)]
                == directory_relative.parts
            ):
                artwork_created.append(record)
        companion = owned_paths._bind_owned_path(
            item["root"],
            artwork["path"],
            expected_file=artwork["file_identity"],
            expected_root=item["root_identity"],
            created_directories=artwork_created,
        )
        if companion is None:
            return None
        companion["kind"] = "artwork"
        companions.append(companion)
    for record in created_files_after_import or ():
        if (
            not isinstance(record, dict)
            or not owned_paths._valid_ownership_identity(record.get("file"))
            or not isinstance(record.get("path"), str)
        ):
            return None
        companion_path = Path(os.path.abspath(record["path"]))
        if companion_path != Path(path).with_suffix(".lrc"):
            return None
        companion = owned_paths._bind_owned_path(
            item["root"],
            companion_path,
            expected_file=record["file"],
            expected_root=item["root_identity"],
            created_directories=created_directories,
        )
        if companion is None:
            return None
        companion["kind"] = "lyrics"
        companions.append(companion)
    if len(companions) > 2:
        return None
    if companions:
        owned["companions"] = companions
    return (owned, path) if owned is not None else None


def _album_dir_for_owned_file(path, landed_dir):
    """Accept only the album scope already verified by the import pipeline."""
    if landed_dir is None:
        return None
    try:
        path = Path(os.path.abspath(os.fspath(path)))
        album_dir = Path(os.path.abspath(os.fspath(landed_dir)))
        relative = path.relative_to(album_dir)
    except (OSError, TypeError, ValueError):
        return None
    return album_dir if relative.parts else None


def _single_download_undo_snapshot(
    queue_item,
    landed_dir,
    *,
    album,
    track,
    artist,
    title,
    track_title,
):
    """Build one exact single-track Undo record from current ownership."""
    owned_path = None
    owned_file_path = None
    owned_binding = _single_owned_path(
        queue_item.get("_import_ownership"),
        landed_dir,
        queue_item.get("_import_ownership_created_directories"),
        queue_item.get("_import_ownership_created_files"),
    )
    if owned_binding is not None:
        owned_path, owned_file_path = owned_binding
        actual_album_dir = _album_dir_for_owned_file(
            owned_file_path, landed_dir
        )
        if actual_album_dir is None:
            owned_path = None
            owned_file_path = None
        else:
            landed_dir = actual_album_dir
    single = {
        "album_id": str(album.get("id") or ""),
        "track_id": str(track.get("id") or ""),
        "dir": str(landed_dir) if landed_dir else "",
        "isrc": track.get("isrc") or "",
        "track_no": track.get("track_number"),
        "disc_no": track.get("media_number") or 1,
        "title": track_title,
        "artist": artist,
        "album": title,
        "marked": False,
    }
    if owned_path is not None:
        single["owned_path"] = owned_path
        single["owned_root"] = str(
            Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        )
    return single, landed_dir, owned_path, owned_file_path


def _refresh_post_import_relocation_recovery(authority) -> bool:
    """Refresh the existing global write gate after a handoff interruption."""
    try:
        recovered = _record_startup_recovery(authority)
    except Exception:
        _log.exception("couldn't refresh recovery after track relocation")
        return False
    return _recovery_status_value(recovered) == "clear"


def _persist_single_download_undo(
    job,
    queue_item,
    *,
    ownership_valid,
    source_single=None,
    destination_single=None,
) -> None:
    """Commit a single-track Undo proof before its relocation can be retired."""
    operation_key = "_post_import_relocation_operation_id"
    if operation_key not in queue_item:
        with job._lock:
            preserve_existing = (
                getattr(job, "_preserve_persisted_single", False) is True
            )
            if ownership_valid and preserve_existing:
                job.__dict__.pop("_preserve_persisted_single", None)
        persisted = job_persistence.persist(job)
        if ownership_valid and persisted is not True:
            with job._lock:
                if preserve_existing:
                    job._preserve_persisted_single = True
            raise RuntimeError(
                "The downloaded track's Undo could not be saved to the data "
                "folder."
            )
        if ownership_valid and persisted is True:
            with job._lock:
                job.__dict__.pop("_preserve_persisted_single", None)
        return

    def restore_source(*, clear_preservation=False) -> None:
        with job._lock:
            if type(source_single) is dict:
                job.single = source_single
            if clear_preservation:
                job.__dict__.pop("_preserve_persisted_single", None)

    def accept_destination(single_snapshot) -> None:
        with job._lock:
            job.single = copy.deepcopy(single_snapshot)
            job.__dict__.pop("_single_undo_unavailable", None)
            job.__dict__.pop("_preserve_persisted_single", None)

    # Engage this before sealing or persisting the destination. Any ordinary
    # job save that was already waiting must preserve the durable source Undo.
    with job._lock:
        job._preserve_persisted_single = True
        single_snapshot = copy.deepcopy(destination_single)

    authority = run_lock.current_lease()
    operation_id = queue_item.get(operation_key)
    album_id = completion.normalise_album_id(job.album_id)
    planned_album = queue_item.get("album")
    planned_album_id = completion.normalise_album_id(
        planned_album.get("id") if isinstance(planned_album, dict) else None
    )
    single_album_id = completion.normalise_album_id(
        single_snapshot.get("album_id")
        if isinstance(single_snapshot, dict)
        else None
    )
    binding_valid = (
        ownership_valid is True
        and album_id == job.album_id
        and planned_album_id == album_id
        and single_album_id == album_id
    )
    if authority is None or not binding_valid:
        _refresh_post_import_relocation_recovery(authority)
        restore_source(clear_preservation=True)
        raise PostImportRelocationAttention(
            "The relocated track could not be bound to its exact Undo record."
        )

    consumer = {
        "kind": "web-single",
        "job_id": job.id,
        "job_created_at": job.created_at,
        "album_id": album_id,
    }
    try:
        handoff_hash = post_import_relocation.seal_post_import_relocation_handoff(
            operation_id,
            consumer=consumer,
            payload=single_snapshot,
            authority=authority,
        )
    except Exception as exc:
        _refresh_post_import_relocation_recovery(authority)
        restore_source(clear_preservation=True)
        raise PostImportRelocationAttention(
            "The relocated track's Undo could not be saved."
        ) from exc

    handoff = {"consumer": consumer, "hash": handoff_hash}
    persistence_error = None
    try:
        persisted = job_persistence.persist_post_import_relocation_handoff(
            job,
            operation_id=operation_id,
            handoff_hash=handoff_hash,
            single=single_snapshot,
        )
    except BaseException as exc:
        persisted = False
        persistence_error = exc
    if persisted is not True:
        # A failed return can still mean SQLite committed before reporting an
        # I/O error.
        queue_item["_post_import_relocation_handoff_unknown"] = True
        try:
            proof_before = (
                job_persistence.post_import_relocation_handoff_persisted(
                    operation_id,
                    handoff,
                )
            )
        except BaseException:
            _log.exception("couldn't verify track Undo before recovery")
            proof_before = None
        recovered_clear = _refresh_post_import_relocation_recovery(authority)
        try:
            proof_after = (
                job_persistence.post_import_relocation_handoff_persisted(
                    operation_id,
                    handoff,
                )
            )
        except BaseException:
            _log.exception("couldn't verify track Undo after recovery")
            proof_after = None
        if proof_before is True or proof_after is True:
            accept_destination(single_snapshot)
            queue_item.pop("_post_import_relocation_handoff_unknown", None)
            queue_item["_post_import_relocation_final_proven"] = True
            if recovered_clear:
                return
        elif proof_before is False or proof_after is False:
            queue_item.pop("_post_import_relocation_handoff_unknown", None)
            restore_source(clear_preservation=True)
        raise PostImportRelocationAttention(
            "The relocated track's Undo could not be saved to the data "
            "folder."
        ) from persistence_error

    accept_destination(single_snapshot)
    queue_item.pop("_post_import_relocation_handoff_unknown", None)
    queue_item["_post_import_relocation_final_proven"] = True

    try:
        post_import_relocation.acknowledge_post_import_relocation(
            operation_id,
            handoff_hash,
            authority=authority,
        )
    except Exception as exc:
        if _refresh_post_import_relocation_recovery(authority):
            return
        raise PostImportRelocationAttention(
            "The relocated track's durable Undo handoff needs recovery."
        ) from exc


def _requeued_download_run(job):
    """The run for a Search download still queued when the app stopped, or
    None for any other job.

    Its album and token lived only in memory, so both are fetched again when
    its turn comes.
    """
    args = job.execute_args or {}
    if (
        not job.album_id
        or job.kind != "download"
        or job.execute_kind
        or job.recoveries
        or job.execute_args_unreadable
        or set(args) - {"new_edition"}
    ):
        return None
    album_id = str(job.album_id)
    track_id = str((job.single or {}).get("track_id") or "")
    if job.single and not track_id:
        return None
    treat_as_new = bool(args.get("new_edition"))

    def run(j):
        token = _authorize_qobuz_live(QobuzAccess.DOWNLOAD_ACTION).token
        album = qobuz_search.get_album(album_id, token)
        if not track_id:
            return _make_download_run(album, token, treat_as_new=treat_as_new)(j)
        track = next(
            (t for t in (album.get("tracks") or {}).get("items") or []
             if str(t.get("id")) == track_id),
            None,
        )
        if track is None:
            raise RuntimeError("That track isn't on this album any more.")
        return _make_single_track_run(album, track, token)(j)

    return run


_SINGLE_TRACK_FAILURES = {
    "disk_full": "Out of disk space before the track landed. The job log says where.",
    "io_error": (
        "A storage error stopped the track before it landed. The job log has "
        "the error."
    ),
    "incomplete": (
        "The track arrived incomplete and was discarded. Retry fetches it again."
    ),
}


def _make_single_track_run(album, track, token):
    """Run a single-track download: download just ``track`` via the per-track
    queue path (the same isolation repair uses, never a whole-album rip)."""
    expected_generation = api_auth.token_credential_generation(token)

    def run(j):
        _require_music_write_target_for_job()
        active = None
        active_token = token
        if getattr(token, "credential_generation", ""):
            active = _authorize_qobuz_live(
                QobuzAccess.DOWNLOAD_ACTION,
                expected_generation=expected_generation,
            )
            active_token = active.token
        args = flows.build_args()
        artist = (album.get("artist") or {}).get("name") or "?"
        title = album.get("title") or "?"
        t_title = track.get("title") or "?"
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        existing, album_dir = catalog.find_existing_tracks(album)
        missing, _present = catalog.compute_missing(qobuz_tracks, existing)
        missing_ids = {str(t.get("id")) for t in missing}
        # Already own this exact track? Don't re-rip it; that just lands a beets
        # ".1.flac" duplicate beside the copy you have, and don't mark anything.
        if str(track.get("id")) not in missing_ids:
            j.summary = f"You already have “{t_title}”. Nothing downloaded."
            return
        qi = queue_builder._build_queue_item(
            album=album, album_dir=album_dir,
            label=f"{artist}, {t_title}  [single]",
            missing=[track], present=existing,
            upgrade_only=False, auto_upgrade=False,
            force_track_by_track=True,
        )
        qi["_capture_import_ownership"] = True
        qi["_defer_post_import_relocation_handoff"] = True
        flows._note_staging_wait(j, "Downloading", 0, 1)
        owned_path = None
        source_single = None
        with job_mgr.staging_lock():
            with _CREDENTIAL_LOCK:
                if (active is not None
                        and not _credential_generation_is_active(
                            active.generation)):
                    raise CredentialChanged(
                        "Qobuz credentials changed before the download began."
                    )
            try:
                queue_executor._execute_download_queue([qi], args, active_token)
                landed_dir = qi.get("_resolved_post_dir") or album_dir
                download_succeeded = (
                    qi.get("n_ok", 0) > 0
                    and qi.get("imported", False)
                    and qi.get("n_fail", 0) == 0
                )
                if download_succeeded:
                    single, landed_dir, owned_path, _owned_file_path = (
                        _single_download_undo_snapshot(
                            qi,
                            landed_dir,
                            album=album,
                            track=track,
                            artist=artist,
                            title=title,
                            track_title=t_title,
                        )
                    )
                    j.single = single
                    _persist_single_download_undo(
                        j,
                        qi,
                        ownership_valid=owned_path is not None,
                    )
                    source_single = copy.deepcopy(single)

                    if (
                        qi.get("_post_import_relocation_pending") is True
                        and owned_path is not None
                    ):

                        authority = run_lock.current_lease()
                        if authority is None:
                            raise RuntimeError(
                                "Automatic filing paused because write "
                                "authority could not be verified."
                            )
                        original_landed_dir = landed_dir
                        ownership_move = {}
                        operation_capture = {}
                        split_ownership_advanced = False
                        try:
                            migrated = queue_executor._reunite_split_album(
                                qi,
                                album_dir,
                                original_landed_dir,
                                authority=authority,
                                await_handoff=True,
                                operation_id_out=operation_capture,
                            )
                            split_ownership_advanced = (
                                "operation_id" in operation_capture
                            )
                            if (
                                not split_ownership_advanced
                                and getattr(args, "migrate_multi_artist", False)
                            ):
                                migrated = catalog.prompt_and_migrate_multi_artist_folder(
                                    album,
                                    args,
                                    ownership_move_out=ownership_move,
                                    operation_id_out=operation_capture,
                                    authority=authority,
                                    source_dir=original_landed_dir,
                                    await_handoff=True,
                                )
                        finally:
                            if "operation_id" in operation_capture:
                                qi["_post_import_relocation_operation_id"] = (
                                    operation_capture["operation_id"]
                                )

                        if "_post_import_relocation_operation_id" in qi:
                            if migrated is None:
                                raise RuntimeError(
                                    "The relocated track's destination was lost."
                                )
                            landed_dir = migrated
                            qi["_resolved_post_dir"] = landed_dir
                            if not split_ownership_advanced:
                                queue_executor._advance_import_ownership_after_relocation(
                                    qi,
                                    ownership_move,
                                )
                            single, landed_dir, owned_path, _owned_file_path = (
                                _single_download_undo_snapshot(
                                    qi,
                                    landed_dir,
                                    album=album,
                                    track=track,
                                    artist=artist,
                                    title=title,
                                    track_title=t_title,
                                )
                            )
                            with j._lock:
                                j._preserve_persisted_single = True
                                j.single = single
                            _persist_single_download_undo(
                                j,
                                qi,
                                ownership_valid=owned_path is not None,
                                source_single=source_single,
                                destination_single=single,
                            )
                        elif (
                            migrated is not None
                            and os.path.abspath(os.fspath(migrated))
                            != os.path.abspath(os.fspath(original_landed_dir))
                        ):
                            _refresh_post_import_relocation_recovery(authority)
                            raise RuntimeError(
                                "The relocated track's recovery record was "
                                "lost."
                            )
                        else:
                            landed_dir = original_landed_dir
                    qi.pop("_post_import_relocation_pending", None)
            except BaseException:
                if (
                    qi.get("_post_import_relocation_pending") is True
                    or "_post_import_relocation_operation_id" in qi
                ):

                    _refresh_post_import_relocation_recovery(
                        run_lock.current_lease()
                    )
                    with j._lock:
                        if (
                            type(source_single) is dict
                            and qi.get(
                                "_post_import_relocation_final_proven"
                            ) is not True
                            and qi.get(
                                "_post_import_relocation_handoff_unknown"
                            ) is not True
                        ):
                            j.single = source_single
                            j.__dict__.pop(
                                "_preserve_persisted_single", None
                            )
                raise
        if not download_succeeded:
            j.status = job_mgr.JobStatus.FAILED
            j.error = _SINGLE_TRACK_FAILURES.get(qi.get("result"))
            if not j.error:
                j.error = (
                    "Downloaded, but the import failed. See job log."
                    if qi.get("n_ok")
                    else "The track was not retrieved. The job log says why."
                )
            return
        j.landed_complete = True
        # The mark keeps Upgrade off a deliberate single whatever the toggle
        # says; the toggle only decides whether scans read it.
        marked = len(missing) > 1
        if marked:
            try:
                hidden_mod.mark_single(artist, title, catalog.album_year(album),
                                       album.get("id"))
                j.summary = (
                    f"Got “{t_title}”, filed under {artist} / {title}. "
                    + ("The rest of the album stays out of scans."
                       if cfg.SUPPRESS_SINGLE_TRACK_GAPS
                       else "Future scans can still offer the rest of the album.")
                )
            except OSError as e:
                # The track landed fine, so don't fail the job, but don't claim
                # the exclusion stuck either.
                marked = False
                j.summary = (f"Got “{t_title}”, filed under {artist} / {title}.")
                if cfg.SUPPRESS_SINGLE_TRACK_GAPS:
                    j.error = redaction.redact(
                        f"{e} The rest of the album may still show in scans.")
                elif not cfg.UPGRADE_SINGLES_ENABLED:
                    j.error = redaction.redact(
                        f"{e} Upgrade may still offer the whole album.")
                else:
                    j.error = redaction.redact(str(e))
        else:
            # This download completed the album, so it's a normal full album
            # now, so clear any single mark an earlier partial download left
            # behind.
            hidden_mod.unmark_single(
                artist,
                title,
                year=catalog.album_year(album),
                album_id=album.get("id"),
            )
            j.summary = (f"Got “{t_title}”; that completed {title}, so it's "
                         "filed as a full album.")
            # Complete means any parked Gap Fill candidate for it is stale.
            flows.prune_library_review_candidates(album)
        with j._lock:
            j.single["marked"] = marked
        if owned_path is None:
            j.summary += (
                " Undo isn't available because the downloaded file "
                "couldn't be verified."
            )
        job_persistence.persist(j)
        flows._refresh_after_local_album_change(
            album,
            {"dir": landed_dir},
            fallback_artist=artist,
            token=active_token,
            args=args,
            upgrade=True,
            downsample=True,
        )
    return run


_census_cache: tuple | None = None
_CENSUS_TTL = 300.0


def _is_mount_point(path) -> bool:
    """Whether ``path`` is the root of its own filesystem.

    Decides whether the free-space figure beside it covers the music alone or a
    disk shared with everything else on the machine, so the label can say which.
    """
    try:
        p = Path(path)
        return p.stat().st_dev != p.parent.stat().st_dev
    except OSError:
        return False


def _census_view():
    """Quality-census context for the Library page, shaped from the scan
    cache. One table walk over every cached tag row: cheap, but not
    per-request cheap on a big library, so the shaped result is memoized for
    a few minutes. None hides the panel (cache off, or nothing scanned yet)."""
    global _census_cache
    now = time.time()
    # A download or a downsample writes to the cache the moment it finishes,
    # here or in a terminal run, so the age of the memo is not enough on its
    # own: hold it only while the rows behind it have not changed.
    stamp = flac_cache.store_stamp()
    if (_census_cache is not None
            and _census_cache[2] == stamp
            and now - _census_cache[0] < _CENSUS_TTL):
        return _census_cache[1]
    raw = flac_cache.census()
    view = None
    if raw:
        labels = {
            "cd": "CD quality (16-bit / 44.1–48 kHz)",
            "hires96": "Hi-res up to 96 kHz",
            "hires192": "Hi-res up to 192 kHz",
            "unknown": "Other formats",
        }
        seg = {"cd": "cd", "hires96": "h96", "hires192": "h192",
               "unknown": "other"}
        total_bytes = raw["total_bytes"] or 1
        rows, bar = [], []
        for tier in ("cd", "hires96", "hires192", "unknown"):
            n, size = raw["tiers"][tier]
            if not n:
                continue
            rows.append({"key": seg[tier], "label": labels[tier],
                         "tracks": f"{n:,} track{'s' if n != 1 else ''}",
                         "size": format_size(size)})
            bar.append({"key": seg[tier],
                        "pct": max(1, round(100 * size / total_bytes))})
        view = {
            "total": f"{raw['total_tracks']:,} tracks · "
                     f"{format_size(raw['total_bytes'])}",
            "rows": rows,
            "bar": bar,
            "top": [{"name": a, "size": format_size(b)}
                    for a, b in raw["top_hires_artists"]],
            # Below ~100 MB the line is noise, not an offer.
            "reclaim": (format_size(raw["reclaim_bytes"])
                        if raw["reclaim_bytes"] >= 100 * 1024 * 1024 else ""),
        }
    # Re-read: census() drains any buffered writes first, so the count it was
    # actually built from is the one after that flush.
    _census_cache = (now, view, flac_cache.store_stamp())
    return view


def _discover_available(creds_ok: bool | None = None) -> bool:
    """Whether Discover has a Last.fm key to suggest from."""
    return lastfm.is_configured()


_JOB_NAV_SURFACES = {
    "library": ("library", "/library", "Back to Library"),
    "new_releases": ("library", "/library", "Back to Library"),
    "upgrade": ("upgrade", "/upgrade", "Back to Upgrade"),
    "downsample": ("downsample", "/downsample", "Back to Downsample"),
    "repair": ("repair", "/repair", "Back to Repair"),
    "lyrics": ("lyrics", "/lyrics", "Back to Lyrics"),
    "migration": ("settings", "/migrate", "Back to Migration"),
    "collection_snapshot": ("settings", "/settings", "Back to Settings"),
    "collection_restore": ("settings", "/settings", "Back to Settings"),
}


def _job_nav_destination(job) -> tuple[str, str, str]:
    destination = _JOB_NAV_SURFACES.get(getattr(job, "execute_kind", ""))
    if destination is not None:
        if destination[0] == "upgrade" and not _upgrade_available():
            return "queue", "/queue/history", "Back to History"
        return destination
    status = getattr(getattr(job, "status", None), "value", "")
    if status in {"done", "failed", "canceled"}:
        return "queue", "/queue/history", "Back to History"
    return "queue", "/queue", "Back to Queue"


# Cover files, in the order the app trusts them. beets writes cover.jpg for
# both sidecar and embed modes, and the rest are what libraries built by other
# tools carry.
_COVER_FILENAMES = ("cover.jpg", "cover.jpeg", "cover.png",
                    "folder.jpg", "folder.jpeg", "folder.png",
                    "front.jpg", "front.jpeg", "front.png")


def _local_album_art(album_dir):
    """The cover file sitting in an album folder, or None.

    Matched without regard to case, because a library built on a case-sensitive
    filesystem is full of Cover.jpg and Folder.jpg.
    """
    try:
        entries = {entry.name.lower(): entry
                   for entry in os.scandir(str(album_dir))
                   if entry.is_file()}
    except OSError:
        return None
    for filename in _COVER_FILENAMES:
        entry = entries.get(filename)
        if entry is not None:
            return Path(entry.path)
    return None


def _review_cover(job, candidate):
    """Where a review row's thumbnail comes from.

    Qobuz results carry a cover URL. Albums already on disk carry no URL at
    all, and the app was showing an empty tile for music whose artwork is
    sitting right there in the folder, so those are served from the folder.
    """
    payload = candidate.get("payload") or {}
    cover = payload.get("cover")
    if cover:
        return str(cover)
    album_dir = payload.get("album_dir")
    cid = candidate.get("cid")
    if album_dir and cid and _local_album_art(album_dir) is not None:
        return f"/jobs/{job.id}/art/{cid}"
    return ""


templates.env.globals["review_cover"] = _review_cover


def _queue_rows_signature(jobs):
    rows = "\n".join(sorted(
        f"{j.id}:{j.status.value}" for j in jobs
        if j.status != job_mgr.JobStatus.AWAITING_REVIEW
    ))
    return hashlib.sha256(rows.encode("utf-8")).hexdigest()[:16]


# What each kind of kept staging group is, in the user's terms. The keys are
# the manifest kinds written by the download, import and recovery paths.
_STAGING_LEFTOVER_KINDS = {
    "rejected": (
        "Lossy track set aside",
        "Qobuz served this track in a lossy format, so it was kept out of "
        "your library. It stays here in case a later attempt finds the "
        "lossless version.",
    ),
    "legacy-rejected": (
        "Lossy track set aside",
        "Qobuz served this track in a lossy format, so it was kept out of "
        "your library. It stays here in case a later attempt finds the "
        "lossless version.",
    ),
    "untagged": (
        "Untagged file set aside",
        "This file arrived without an album or artist tag, so it could not "
        "be filed.",
    ),
    "unimported": (
        "File that never imported",
        "The download finished but this file was not filed into the "
        "library.",
    ),
    "unresolved": (
        "File set aside",
        "Kept out of the library because it could not be filed.",
    ),
    "beets": (
        "Album waiting to be filed",
        "The tracks are downloaded; filing them failed. The next download "
        "run tries again on its own, with no re-download.",
    ),
    "interrupted": (
        "Files from a stopped download",
        "A download stopped part way and its files were kept so nothing "
        "already fetched is thrown away.",
    ),
}


def _album_name_from_path(path):
    """An album folder read back as a name: artist, then album."""
    parts = [part for part in Path(str(path)).parts if part not in ("/", "")]
    if len(parts) >= 2:
        return f"{parts[-2]} · {parts[-1]}"
    return parts[-1] if parts else ""


def _staging_display_name(path):
    """A staging path as something worth reading: artist, album and file, with
    the app's own private run folders left out of it."""
    try:
        relative = Path(str(path)).relative_to(Path(str(cfg.STAGING_DIR)))
    except ValueError:
        relative = Path(str(path))
    return " / ".join(
        part for part in relative.parts if not part.startswith("."))


def _staging_tree_contents(tree):
    """The deepest album folders one retained tree holds."""
    relatives = [rel for rel, _identity in tree.directories if rel]
    leaves = sorted(
        rel for rel in relatives
        if not any(other != rel and other.startswith(rel + "/")
                   for other in relatives)
    )
    if leaves:
        return ", ".join(leaf.replace("/", " / ") for leaf in leaves)
    return plural(len(tree.files), "file")


def _staging_leftovers():
    """Every group the app is holding in staging, as user-facing rows.

    Downloads warn that files are being kept, and until this there was
    nowhere to see what they are or get rid of them, so they accumulated
    unseen. Read-only: removing one is always a deliberate click.
    """
    leftovers = []
    for inspection in staging_mod.inspect_retry_groups():
        kind = inspection.kind or "unresolved"
        label, reason = _STAGING_LEFTOVER_KINDS.get(
            kind, _STAGING_LEFTOVER_KINDS["unresolved"])
        what = ""
        held = inspection.file_group or inspection.planned_file
        if held is not None:
            source = held.original or held.retained
            what = _staging_display_name(source)
        elif inspection.planned_trees and not any(
                tree.files or any(rel for rel, _identity in tree.directories)
                for tree in inspection.planned_trees):
            label = "Empty folder from a stopped download"
            reason = "The download stopped before any file arrived."
        elif inspection.planned_trees:
            what = ", ".join(
                _staging_tree_contents(tree)
                for tree in inspection.planned_trees
            )
        if inspection.status == "ready" and inspection.owner is None:
            removable, note = True, ""
        elif inspection.owner is not None:
            removable = False
            note = ("It is tied to a download that never finished, so the app "
                    "will not clear it on its own.")
        elif inspection.status == "malformed":
            removable = False
            note = ("The app's record of what it holds can't be read, so it "
                    "can't tell what is in there.")
        elif inspection.status == "incomplete":
            removable = False
            note = "Some of the files it recorded are already gone."
        else:
            removable = False
            note = ("Its contents changed since it was set aside, so the app "
                    "can't tell whether these are still the files it kept.")
        leftovers.append({
            "name": inspection.path.name,
            "label": f"{label}: {what}" if what else label,
            "reason": reason,
            "removable": removable,
            "note": note,
            "path": str(inspection.path),
        })
    return leftovers


def _diagnostics():
    """Read-only health checks surfaced on the Settings page."""
    checks = []

    def _tree_size(path) -> int:
        """Best-effort total bytes of regular files under a directory tree.

        Skips whatever it can't stat rather than giving up on the whole
        total: this feeds a rough disk-usage line, not a byte-exact figure.
        """
        total = 0
        try:
            for parent, _dirs, names in os.walk(path):
                for name in names:
                    try:
                        value = os.stat(os.path.join(parent, name),
                                        follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(value.st_mode):
                        total += int(value.st_size)
        except OSError:
            pass
        return total

    def _dir_check(label, path, *, want_writable, skip_names=(),
                   unit=("entry", "entries"), show_size=False):
        # The Library paths section above already resolves the same folders to
        # the host path Docker exposes them at; naming this one by its
        # container path made the two sections disagree about what to call
        # the same folder.
        p = Path(path)
        display, _is_host = _resolve_host_path(str(p))
        if not p.exists():
            hint = " (volume not mounted?)" if cfg.in_container() else ""
            checks.append({"label": label, "ok": False,
                           "detail": f"{display} does not exist{hint}"})
            return
        if not p.is_dir():
            checks.append({"label": label, "ok": False,
                           "detail": f"{display} exists but is not a directory"})
            return
        if want_writable and not os.access(p, os.W_OK):
            checks.append({"label": label, "ok": False,
                           "detail": f"{display} is not writable by the container user. "
                           "On a NAS, set PUID/PGID in .env to your media-share owner"})
            return
        try:
            n = sum(1 for entry in p.iterdir() if entry.name not in skip_names)
        except OSError as e:
            checks.append({"label": label, "ok": False,
                           "detail": f"{display} unreadable: {e}"})
            return
        size = f" · {format_size(_tree_size(p))}" if show_size else ""
        checks.append({"label": label, "ok": True, "mono": True,
                       "detail": f"{display}: {plural(n, *unit)}{size}"})

    # The panel exists to say what stops a scan or a download before one is
    # started, and a pause is the most direct reason there is. It was the one
    # thing missing: every row could read OK while nothing could run at all.
    paused = _writes_paused_notice()
    if paused is not None:
        # The banner at the top of this same page already carries the whole
        # sentence; the row names the cause so the panel reads as a checklist.
        checks.append({"label": "Writes paused", "ok": False,
                       "detail": paused["reason"]})
    else:
        checks.append({"label": "Writes on", "ok": True,
                       "detail": "Not paused"})

    music_state, recorded_albums = collection_snapshot.music_root_write_state()
    if music_state == "ready":
        _dir_check("Music library", cfg.MUSIC_ROOT, want_writable=True)
    else:
        checks.append({
            "label": "Music library",
            "ok": False,
            "detail": _music_write_target_message(
                music_state,
                recorded_albums,
                diagnostic=True,
            ),
        })
    # The app's own recovery folder is not a staging entry; counting it made a
    # pile of kept files read as one healthy item. It gets its own check below,
    # so this row says what it counted rather than "0 entries", which read as
    # an empty staging folder next to a warning about the files kept in it.
    _dir_check("Staging area", cfg.STAGING_DIR, want_writable=True,
               skip_names={cfg.BEETS_RETRY_DIR}, show_size=True,
               unit=("album waiting to import", "albums waiting to import"))
    _dir_check("Data folder", cfg.DATA_DIR, want_writable=True)
    # A single file left owned by another user, as a command run as root or a
    # PUID change leaves behind, passes the folder check above.
    try:
        locked = sorted(
            entry.name for entry in Path(cfg.DATA_DIR).iterdir()
            if not os.access(entry, os.R_OK | os.W_OK
                             | (os.X_OK if entry.is_dir() else 0)))
    except OSError:
        locked = []
    if locked:
        shown = ", ".join(locked[:5]) + (
            f" and {len(locked) - 5} more" if len(locked) > 5 else "")
        checks.append({"label": "Data folder files", "ok": False,
                       "detail": f"Not readable and writable by the container "
                       f"user: {shown}. Set their owner to PUID/PGID"})
    # A whole-directory count and size, the same shape as the Staging area
    # row above. The rows further down (Unfinished upgrade backups, Backups
    # needing review) each cover one problem subset; this is the total the
    # folder is actually holding.
    _dir_check("Upgrade backups", cfg.UPGRADE_BACKUP_DIR, want_writable=True,
               show_size=True, unit=("kept backup", "kept backups"))

    beets_db = Path(cfg.BEETS_DB_PATH)
    beets_db_display, _is_host = _resolve_host_path(str(beets_db))
    if beets_db.exists():
        ok = os.access(beets_db, os.R_OK)
        checks.append({"label": "Beets database", "ok": ok,
                       "detail": f"{beets_db_display}" if ok
                       else f"{beets_db_display} exists but is not readable"})
    elif beets_db.parent.exists():
        checks.append({"label": "Beets database", "ok": True,
                       "detail": f"{beets_db_display} (created on first import)"})
    else:
        parent_display, _is_host = _resolve_host_path(str(beets_db.parent))
        checks.append({"label": "Beets database", "ok": False,
                       "detail": f"{parent_display} does not exist"})

    missing_tool_fix = ("Pull the image again (docker compose pull)."
                        if cfg.in_container()
                        else "See Quick start in the README.")
    for binary in ("rip", "ffmpeg", "flac"):
        found = shutil.which(binary)
        checks.append({"label": f"{binary} binary",
                       "ok": bool(found),
                       "detail": found or f"{binary} was not found. "
                       f"{missing_tool_fix}"})
    beets_python, beets_detail = _beets_runtime_diagnostic()
    checks.append({
        "label": "Beets",
        "ok": beets_python is not None,
        "detail": beets_detail,
    })

    stranded = []
    stranded_error = False
    if cfg.UPGRADE_BACKUP_DIR.exists():
        try:
            for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
                if entry.is_dir() and (entry.suffix == ".partial"
                                       or entry.name == ".restore_trash"):
                    stranded.append(entry)
        except OSError as exc:
            stranded_error = True
            _log.warning(
                "couldn't inspect stranded upgrade backups: %s", exc)
    if stranded_error:
        checks.append({
            "label": "Unfinished upgrade backups",
            "ok": False,
            "detail": "Could not inspect this folder; its status is unknown.",
        })
    elif stranded:
        checks.append({"label": "Unfinished upgrade backups", "ok": False,
                       "detail": f"{len(stranded)} found in "
                                 f"{cfg.UPGRADE_BACKUP_DIR}; manual cleanup needed"})
    else:
        checks.append({"label": "Unfinished upgrade backups", "ok": True,
                       "detail": "none"})
    backup_dir = collection_snapshot.snapshot_dir()
    if cfg.collection_backup_dir_in_music(backup_dir):
        checks.append({
            "label": "Backup folder",
            "ok": False,
            "detail": f"{_resolve_host_path(str(backup_dir))[0]} is inside "
                      "the music folder, so a music disk that fails takes "
                      "the backups with it.",
        })

    inventory = {"orphans": [], "undo": [], "leftovers": []}
    try:
        # An upgrade's backup inside its retention window is expected, not a
        # fault; the age sweep settles it.
        inventory["orphans"] = [
            item for item in backup_mod.list_retained_backups()
            if not backup_mod.awaiting_retention(item[0])
        ]
    except Exception as exc:
        _log.warning(
            "couldn't inspect kept recovery backups: %s", exc)
        checks.append({
            "label": "Backups needing review",
            "ok": False,
            "detail": "Could not inspect kept backups; their status is unknown.",
        })
    orphans = inventory["orphans"]
    interrupted_disposals = [
        item for item in orphans
        if item[0].name.startswith(".ql-dispose-backup-")
    ]
    orphans = [
        item for item in orphans
        if not item[0].name.startswith(".ql-dispose-backup-")
        and not item[2].removable
    ]
    if interrupted_disposals:
        checks.append({
            "label": "Interrupted backup cleanup",
            "ok": False,
            "detail": f"{plural(len(interrupted_disposals), 'backup')} kept "
                      "recovery data; review the location shown below before "
                      "removing anything.",
        })
    if orphans:
        checks.append({"label": "Backups needing review", "ok": False,
                       "detail": f"{plural(len(orphans), 'backup')} "
                                 f"{'was' if len(orphans) == 1 else 'were'} "
                                 "kept. Restore or remove "
                                 f"{'it' if len(orphans) == 1 else 'them'} "
                                 "below."})
    elif not any(d["label"] == "Backups needing review" for d in checks):
        checks.append({"label": "Backups needing review", "ok": True,
                       "detail": "none"})

    # Counted separately from the backups above, which are a fault. These are
    # the undo copies a downsample was asked to keep, and leaving them out of
    # every count made the restore rows below look like unexplained extras.
    try:
        inventory["undo"] = backup_mod.list_undo_copies()
    except Exception as exc:
        _log.warning(
            "couldn't inspect retained hi-res originals: %s", exc)
        checks.append({
            "label": "Hi-res originals kept",
            "ok": False,
            "detail": "Could not inspect retained originals; their status is unknown.",
        })
    undo_copies = inventory["undo"]
    if undo_copies:
        checks.append({
            "label": "Hi-res originals kept",
            "ok": True,
            "detail": f"{plural(len(undo_copies), 'downsampled album')} can be "
                      f"put back, listed below. Cleared automatically after "
                      f"{plural(cfg.UPGRADE_BACKUP_RETENTION_DAYS, 'day')}.",
        })

    try:
        inventory["leftovers"] = _staging_leftovers()
    except Exception as exc:
        _log.warning(
            "couldn't inspect files kept in staging: %s", exc)
        checks.append({
            "label": "Files kept in staging",
            "ok": False,
            "detail": "Could not inspect kept staging files; their status is unknown.",
        })
    leftovers = inventory["leftovers"]
    if leftovers:
        checks.append({
            "label": "Files kept in staging",
            "ok": False,
            "detail": f"{plural(len(leftovers), 'item')} "
                      f"{'is' if len(leftovers) == 1 else 'are'} being held "
                      "outside your library. Review them below.",
        })
    elif not any(d["label"] == "Files kept in staging" for d in checks):
        checks.append({"label": "Files kept in staging", "ok": True,
                       "detail": "none"})
    return {"checks": checks, **inventory}


def _resolve_host_path(container_path: str) -> tuple[str, bool]:
    """Return (display_path, is_host_path) for a path inside the container.

    Walks /proc/self/mountinfo to find the longest-prefix bind mount, then
    appends the remaining suffix to the host source. Falls back to the
    container path when no bind mount covers it (anonymous volume) or the
    file isn't available (non-Linux).
    """
    container_path = str(container_path)
    try:
        with open("/proc/self/mountinfo") as f:
            entries = []
            for line in f:
                parts = line.split()
                if len(parts) < 5:
                    continue
                entries.append((parts[4], parts[3]))  # mount_point, host_root
    except OSError:
        return container_path, False
    best = None
    for mount_point, host_root in entries:
        if mount_point == "/":  # container rootfs, not a user bind mount
            continue
        if (container_path == mount_point
                or container_path.startswith(mount_point.rstrip("/") + "/")):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, host_root)
    if best is None:
        return container_path, False
    mount_point, host_root = best
    suffix = container_path[len(mount_point):]
    host_path = host_root.rstrip("/") + suffix if suffix else host_root
    return host_path, True


def _diagnostics_fragment(request: Request, diagnostics=None) -> str:
    """The diagnostics list items, plus a row per retained backup.

    Shared by the Settings page render, the Recheck partial, and the restore
    POST below, which re-renders the list in place so a restored backup
    disappears from it without a page reload."""
    if not isinstance(diagnostics, dict):
        report = _diagnostics()
        if diagnostics is not None:
            report["checks"] = diagnostics
    else:
        report = diagnostics
    checks = report["checks"]
    rows = []
    for d in checks:
        icon = "OK" if d["ok"] else "!"
        cls = "ql-diagnostic-status-ok" if d["ok"] else "ql-diagnostic-status-error"
        aria = "OK" if d["ok"] else "Needs attention"
        # A path-and-count line reads fine in the small monospace the rest of
        # the checks share; the sentences explaining an actual problem don't.
        detail_cls = "ql-diagnostic-detail ql-diagnostic-detail-mono" if d.get("mono") else "ql-diagnostic-detail"
        detail = f'<div class="{detail_cls}">{html.escape(d.get("detail") or "")}</div>' if d.get("detail") else ""
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status {cls}" aria-label="{aria}">{icon}</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">{html.escape(d["label"])}</div>{detail}</div>'
            f'</div>'
        )
    orphans = report["orphans"]
    tok = html.escape(request.state.csrf_token)
    for path, origin, classification in orphans:
        name = html.escape(path.name)
        if origin:
            dest_display, _is_host = _resolve_host_path(str(origin))
            dest = html.escape(dest_display)
            album = html.escape(_album_name_from_path(origin))
        else:
            dest = "its album folder"
            album = ""
        if path.name.startswith(".ql-dispose-backup-"):
            held = path / "held"
            try:
                location = (
                    held
                    if stat.S_ISDIR(
                        held.stat(follow_symlinks=False).st_mode)
                    else path
                )
            except OSError:
                location = path
            display_location, _is_host_path = _resolve_host_path(location)
            detail = (
                f"Recovery files for {dest} were kept at "
                f"{html.escape(display_location)}. Review them before removing "
                "anything."
            )
            rows.append(
                f'<div class="ql-diagnostic-row">'
                f'<span class="ql-diagnostic-status '
                f'ql-diagnostic-status-error" '
                f'aria-label="Needs attention">!</span>'
                f'<div class="min-w-0"><div '
                f'class="ql-diagnostic-label">Interrupted backup cleanup'
                f'</div><div class="ql-diagnostic-detail">{detail}</div>'
                f'</div></div>'
            )
            continue
        if backup_mod.is_set_aside_replacement(path):
            # Restore would put this download over the album's original.
            rows.append(
                f'<div class="ql-diagnostic-row" data-backup-status="set-aside">'
                f'<span class="ql-diagnostic-status ql-diagnostic-status-error" '
                f'aria-label="Needs attention">!</span>'
                f'<div class="min-w-0"><div class="ql-diagnostic-label">'
                f'Download set aside{f": {album}" if album else ""}</div>'
                f'<div class="ql-diagnostic-detail">An upgrade couldn\'t '
                f'verify this download, so the original album was put back in '
                f'{dest}.</div>'
                f'<form hx-post="/backups/discard-unchecked" '
                f'hx-target="#diagnostics-list" class="mt-2" data-busy-submit>'
                f'<input type="hidden" name="_csrf_token" value="{tok}">'
                f'<input type="hidden" name="backup" value="{name}">'
                f'<button type="submit" class="ql-btn ql-btn-sm" '
                f'data-confirm="Delete this download? The album keeps its '
                f'original files." data-confirm-action="Delete" '
                f'data-irreversible>Delete</button>'
                f'</form></div></div>'
            )
            continue
        reason = html.escape(classification.detail)
        status = "ok" if classification.removable else "error"
        icon = "OK" if classification.removable else "!"
        aria = "OK" if classification.removable else "Needs attention"
        where = dest if origin else "the album folder it came from"
        if re.match(r"^\d{8}_\d{6}(?:_\d{6})?_(?:gapfill|downsample)_", path.name):
            # These restore file by file and the backup wins each swap.
            restore_confirm = (
                f"Put these files back in {where}? Any file of the same name "
                "there is replaced and cannot be brought back.")
        else:
            restore_confirm = (
                f"Put this album back in {where}? Restore goes ahead only "
                "while that folder holds less than the backup, and replaces "
                "what it holds.")
        # Remove proves every file back first, so it can only succeed where
        # the listing could not finish its own check.
        remove_form = (
            f'<form hx-post="/backups/discard" hx-target="#diagnostics-list" data-busy-submit>'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Remove this backup? It is deleted only after '
            f'every file it holds is verified byte-for-byte back in {where}." '
            f'data-confirm-action="Remove" data-irreversible>Remove</button>'
            f'</form>'
        ) if classification.status != "retained" else ""
        rows.append(
            f'<div class="ql-diagnostic-row" data-backup-status="{classification.status}">'
            f'<span class="ql-diagnostic-status ql-diagnostic-status-{status}" aria-label="{aria}">{icon}</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">'
            f'Backup{f": {album}" if album else ""}</div>'
            f'<div class="ql-diagnostic-detail">{reason}</div>'
            f'<div class="mt-2 flex gap-2">'
            f'<form hx-post="/backups/restore" hx-target="#diagnostics-list" data-busy-submit>'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="{restore_confirm}" '
            f'data-confirm-action="Restore" data-irreversible>Restore</button>'
            f'</form>'
            f'{remove_form}'
            f'</div></div></div>'
        )
    undo = report["undo"]
    for path, origin in undo:
        name = html.escape(path.name)
        if origin:
            dest_display, _is_host = _resolve_host_path(str(origin))
            dest = html.escape(dest_display)
            album = html.escape(_album_name_from_path(origin))
        else:
            dest = "its album folder"
            album = ""
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status ql-diagnostic-status-ok" aria-label="OK">OK</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">'
            f'Hi-res originals kept{f": {album}" if album else ""}</div>'
            f'<div class="ql-diagnostic-detail">Copies of the files this album '
            f'had before it was downsampled, so the rewrite can be undone; '
            f'cleared automatically after '
            f'{plural(cfg.UPGRADE_BACKUP_RETENTION_DAYS, "day")}.</div>'
            f'<div class="mt-2 flex gap-2">'
            f'<form hx-post="/backups/restore" hx-target="#diagnostics-list" data-busy-submit>'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Put the hi-res originals of '
            f'{album or dest} back? This undoes the downsample." '
            f'data-confirm-action="Restore">Restore</button>'
            f'</form>'
            f'<form hx-post="/backups/release-originals" '
            f'hx-target="#diagnostics-list" data-busy-submit>'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Delete the hi-res originals of '
            f'{album or dest}? They are the only copies left at the original '
            f'quality, the album keeps its downsampled files, and the '
            f'downsample can no longer be undone." '
            f'data-confirm-action="Delete originals" data-irreversible>'
            f'Delete originals</button>'
            f'</form>'
            f'</div></div></div>'
        )
    leftovers = report["leftovers"]
    for leftover in leftovers:
        detail = html.escape(leftover["reason"])
        if leftover["note"]:
            detail += " " + html.escape(leftover["note"])
        name = html.escape(leftover["name"])
        if leftover["removable"]:
            action = (
                f'<form hx-post="/staging/discard" '
                f'hx-target="#diagnostics-list" class="mt-2" data-busy-submit>'
                f'<input type="hidden" name="_csrf_token" value="{tok}">'
                f'<input type="hidden" name="group" value="{name}">'
                f'<button type="submit" class="ql-btn ql-btn-sm" '
                f'data-confirm="Delete these files? They are not in your '
                f'library and this cannot be undone." '
                f'data-confirm-action="Remove" data-irreversible>Remove</button>'
                f'</form>'
            )
        else:
            # Nothing automatic will ever clear this row, so it needs the one
            # place to look and a way to end it from here.
            where, _is_host = _resolve_host_path(leftover["path"])
            where = html.escape(where)
            detail += f" Its folder is {where}."
            action = (
                f'<form hx-post="/staging/discard-unchecked" '
                f'hx-target="#diagnostics-list" class="mt-2" data-busy-submit>'
                f'<input type="hidden" name="_csrf_token" value="{tok}">'
                f'<input type="hidden" name="group" value="{name}">'
                f'<button type="submit" class="ql-btn ql-btn-sm" '
                f'data-confirm="Delete the files at {where} without checking '
                f'them? The app cannot confirm what they are, they are not in '
                f'your library, and this cannot be undone." '
                f'data-confirm-action="Delete anyway" '
                f'data-irreversible>Delete anyway</button>'
                f'</form>'
            )
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status ql-diagnostic-status-error" '
            f'aria-label="Needs attention">!</span>'
            f'<div class="min-w-0">'
            f'<div class="ql-diagnostic-label">'
            f'{html.escape(leftover["label"])}</div>'
            f'<div class="ql-diagnostic-detail">{detail}</div>'
            f'{action}</div></div>'
        )
    return "\n".join(rows)


def _get_token():
    return api_auth.load_qobuz_token()[1]


def _get_optional_token():
    if not _read_creds().get("auth_token"):
        return None
    try:
        return _get_token()
    except Exception:
        return None


def _collection_backup_status():
    """What the Settings page shows about the collection snapshot."""
    state, latest = collection_snapshot.latest_status()
    info = {
        "folder": str(collection_snapshot.snapshot_dir()),
        "age": None,
        "counts": None,
        "held_back": None,
        "held_back_unreadable": False,
        "unreadable": state == "unreadable",
        "failed": None,
    }
    failure = collection_snapshot.last_failure()
    if failure is not None:
        info["failed"] = {"age": _format_age(failure[0]), "error": failure[1]}
    if isinstance(latest, dict):
        info["counts"] = latest.get("counts")
        stamp = latest.get("updated_at_epoch")
        if isinstance(stamp, (int, float)):
            info["age"] = _format_age(float(stamp))
    suspect = collection_snapshot.suspect_path()
    try:
        held = json.loads(suspect.read_text(encoding="utf-8"))
    except FileNotFoundError:
        held = None
    except (OSError, RecursionError, ValueError):
        held = None
        info["held_back_unreadable"] = True
    valid, _reason = collection_snapshot.validate_upload(
        held, allow_empty=True
    )
    if valid:
        info["held_back"] = held.get("counts")
    elif held is not None:
        info["held_back_unreadable"] = True
    return info


def _format_age(ts: float) -> str:
    """Human-readable age of a past timestamp."""
    try:
        ts = float(ts)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(ts):
        return ""
    age = time.time() - ts
    if age < 120:
        return "just now"
    if age < 3600:
        return f"{int(age / 60)} min ago"
    if age < 86400:
        return f"{int(age / 3600)} hr ago"
    days = int(age / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _when_label(ts) -> tuple[str, str]:
    """(label, exact) pair for a history timestamp: relative while it's fresh
    (matching the "1 hr ago" the tool pages already speak), a short date once
    it isn't. The exact stamp goes in a tooltip for anyone who needs the
    minute."""
    if not ts:
        return "", ""
    exact = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    if time.time() - ts < 7 * 86400:
        return _format_age(ts), exact
    dt = datetime.fromtimestamp(ts)
    label = f"{dt.strftime('%b')} {dt.day}"
    if dt.year != datetime.now().year:
        label += f", {dt.year}"
    return label, exact


def _tool_last_run_age(execute_kind: str) -> str | None:
    """Age of a tool scan's last clean run, or None if it never finished."""
    ts = job_persistence.last_finished_at(execute_kind)
    return _format_age(ts) if ts is not None else None


def _read_creds():
    credentials = api_auth.read_qobuz_credentials()
    if not credentials.configured:
        return {}
    return {
        "user_id": credentials.user_id,
        "auth_token": credentials.token,
        "_generation": credentials.generation,
        "_source": credentials.source,
    }


def _credentials_snapshot():
    values = _read_creds()
    return credentials_from_values(
        values.get("user_id", ""),
        values.get("auth_token", ""),
        source=values.get("_source", "web"),
    )


def _write_creds(user_id, auth_token) -> bool:
    """Write credentials into the streamrip config."""
    return api_auth.write_streamrip_creds(user_id, auth_token)
