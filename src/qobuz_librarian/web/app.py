"""FastAPI web application for Qobuz Librarian."""
import asyncio
import concurrent.futures
import copy
import ctypes
import errno
import hashlib
import html
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from qobuz_librarian import __version__, state_file
from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthEvidence,
    AuthLost,
    AuthOutcome,
    CredentialChanged,
    DownloaderNotReady,
    NoCredsError,
    QobuzAccess,
    QobuzEntitlementError,
    QobuzUnavailable,
    credentials_from_values,
    qobuz_capability,
)
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web.csrf import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    StripServerHeaderMiddleware,
)

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
_STARTUP_RECOVERY_LOCK = threading.RLock()
# Set before lifespan shutdown starts so no new mutating request can register
# while the workers and request-owned library operations are draining.
_SHUTTING_DOWN = False
# Persisted Web jobs are restored only after this process holds exact write
# authority.
_JOBS_RESTORED = False
_JOBS_RESTORE_LOCK = threading.Lock()
# Tri-state result of the startup token probe.
_TOKEN_VALID: bool | None = None
_TOKEN_GENERATION: str | None = None
_AUTH_LOSS_NOTIFIED_GENERATIONS: set[str] = set()
_CREDENTIAL_LOCK = threading.RLock()


def _run_lock_intact() -> bool:
    intact = getattr(_RUN_LOCK_HANDLE, "intact", None)
    return callable(intact) and intact() is True


def _beets_runtime_diagnostic() -> tuple[str | None, str]:
    """Distinguish an absent launcher from a failed runtime verification."""
    from qobuz_librarian.integrations import beets as beets_mod

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
            "Could not verify a Beets 2.12.0 runtime and readable "
            f"configuration using {runtime.python}",
        )
    return runtime.python, runtime.python


def _recover_startup_queue(authority):
    from qobuz_librarian.completion import (
        CompletionOrigin,
        CompletionOriginKind,
        RecoveryOwner,
    )
    from qobuz_librarian.queue.startup_recovery import recover_startup_state
    from qobuz_librarian.web import job_persistence

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
        from qobuz_librarian.completion import normalise_album_id

        planned_album = (
            planned.get("album") if isinstance(planned, dict) else None
        )
        if (
            type(origin) is not CompletionOrigin
            or type(owner) is not RecoveryOwner
            or normalise_album_id(
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

    return recover_startup_state(
        authority=authority,
        acknowledge_completion=_acknowledge,
    )


def _record_startup_recovery(authority):
    global _STARTUP_RECOVERY_RESULT, _STARTUP_RECOVERY_UNKNOWN
    with _STARTUP_RECOVERY_LOCK:
        _STARTUP_RECOVERY_UNKNOWN = True
        result = _recover_startup_queue(authority)
        _STARTUP_RECOVERY_RESULT = result
        _STARTUP_RECOVERY_UNKNOWN = False
        job_mgr.set_durable_recovery_job_id(_startup_recovery_web_job_id())
        return _STARTUP_RECOVERY_RESULT


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
        from qobuz_librarian.completion import (
            RecoveryOwner,
            parse_completion_input_record,
        )
        from qobuz_librarian.queue import journal as queue_state

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
        completion_input = parse_completion_input_record(
            queued_item.completion_input,
            expected_owner=RecoveryOwner(
                recovery_item.operation_id,
                recovery_item.item_id,
            ),
        )
        if completion_input is None:
            return None
        from qobuz_librarian.completion import normalise_album_id

        planned_album = queued_item.planned.get("album")
        if normalise_album_id(
            planned_album.get("id") if isinstance(planned_album, dict) else None
        ) != normalise_album_id(completion_input.expectation.album_id):
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
        from qobuz_librarian.completion import normalise_album_id
        from qobuz_librarian.web import job_persistence

        row = job_persistence.load_one(job_id)
        planned_album = queued_item.planned.get("album")
        if (
            row is None
            or normalise_album_id(row.get("album_id"))
            != normalise_album_id(
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
        from qobuz_librarian.completion import normalise_album_id
        if (
            loaded_journal.mode != f"web-job:{job.id}"
            or len(loaded_journal.items) != 1
        ):
            return False
        planned_album = queued_item.planned.get("album")
        planned_id = normalise_album_id(
            planned_album.get("id") if isinstance(planned_album, dict) else None
        )
        job_id = normalise_album_id(job.album_id)
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
            from qobuz_librarian.queue import journal as queue_state

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
    from qobuz_librarian.queue.startup_recovery import (
        BlockedItemSettlementAction,
        BlockedItemSettlementStatus,
        settle_blocked_item,
    )

    if type(action) is not BlockedItemSettlementAction:
        raise ValueError("a blocked-item settlement action is required")
    with _STARTUP_RECOVERY_LOCK:
        if not _run_lock_intact():
            return False, "The single-writer safety lock is unavailable."
        try:
            recovery = _record_startup_recovery(_RUN_LOCK_HANDLE)
        except Exception:
            return False, "The saved recovery state could not be checked safely."
        if (
            _recovery_status_value(recovery) != "attention_required"
            or not _durable_recovery_matches_job(job)
        ):
            return False, "The blocked recovery does not match this exact download."
        binding = _startup_recovery_binding()
        if binding is None:
            return False, "The blocked recovery identity could not be verified."
        recovery_item = binding[0]
        try:
            settled = settle_blocked_item(
                authority=_RUN_LOCK_HANDLE,
                operation_id=recovery_item.operation_id,
                item_id=recovery_item.item_id,
                action=action,
            )
        except Exception:
            logging.getLogger("qobuz_librarian").exception(
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
            return False, "The settled recovery could not be verified safely."
        if action is BlockedItemSettlementAction.RETRY:
            if (
                _recovery_status_value(refreshed) != "resume_required"
                or not _durable_recovery_matches_job(job)
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
    recovery_item, _journal, _queued_item, _origin = binding
    try:
        from qobuz_librarian.web import job_persistence

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
    }


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
        logging.getLogger("qobuz_librarian").warning(
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
    from qobuz_librarian.completion import normalise_album_id
    from qobuz_librarian.web import job_persistence

    album_id = normalise_album_id(getattr(job, "album_id", None))
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
    from qobuz_librarian.web import job_persistence

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
    _log = logging.getLogger("qobuz_librarian")
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
    return (
        f'<div class="ql-notice ql-notice-{kind}" '
        f'data-flash data-flash-kind="{kind}">{body}</div>'
    )


def _download_fragment(kind: str, body: str, outcome: str) -> HTMLResponse:
    return HTMLResponse(
        _ql_notice_html(kind, body),
        headers={"X-QL-Download-Outcome": outcome},
    )


def _download_error_message(exc, fallback: str) -> str:
    """Give download preparation failures the same Web-facing diagnosis."""
    from qobuz_librarian.api.auth import (
        QobuzError,
        friendly_qobuz_error,
    )

    if isinstance(exc, asyncio.TimeoutError):
        return "Timed out reaching the Qobuz API. Try again."
    if isinstance(exc, QobuzUnavailable):
        return (
            "Qobuz is temporarily unavailable (network or rate limit). "
            "Try again shortly."
        )
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
        if friendly_qobuz_error(exc).startswith("HTTP 404"):
            return "No album with that id. Check the URL or use Search."
        return "Couldn't reach the Qobuz API. Check the container's network."
    return fallback


def _qobuz_action_error_message(exc, *, unchanged=False) -> str:
    """Short action-gate copy shared by scans and review approvals."""
    if isinstance(exc, NoCredsError):
        message = "Connect Qobuz in Settings."
    elif isinstance(exc, AuthLost):
        message = "Qobuz rejected the saved token. Reconnect in Settings."
    elif isinstance(exc, DownloaderNotReady):
        message = (
            "Your Qobuz token works, but downloads also need your Qobuz "
            "user ID. Add it in Settings."
        )
    elif isinstance(exc, CredentialChanged):
        message = "Qobuz credentials changed while this was starting. Try again."
    elif isinstance(exc, QobuzEntitlementError):
        message = "Your Qobuz account cannot perform this action."
    else:
        message = "Qobuz could not be reached. Try again."
    if unchanged:
        message += " Nothing changed."
    return message


def _authorize_qobuz_live(access: QobuzAccess, *, expected_generation=""):
    """Run the bounded uncached check used before a Web action is admitted."""
    from qobuz_librarian.api.client import authorize_qobuz_action, call_within

    return call_within(
        cfg.WEB_TEST_AUTH_TIMEOUT,
        authorize_qobuz_action,
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
    from qobuz_librarian.api.auth import read_qobuz_credentials

    return bool(generation) and read_qobuz_credentials().generation == generation


def _job_admission_response(request):
    """Explain a refused jobs.db admission without claiming work was queued."""
    message = job_mgr.JOB_ADMISSION_ERROR
    if _is_htmx(request):
        return HTMLResponse(
            _ql_notice_html("error", html.escape(message)),
            status_code=200,
        )
    return RedirectResponse(
        url="/queue?error=" + urllib.parse.quote(message),
        status_code=303,
    )


def _writes_paused_notice(*, durable_resume_job_id: str | None = None,
                          log_details: bool = False):
    """Why downloads and scans are paused, in the user's words, or None.

    One source for both the 503 a blocked request gets and the notice the
    dashboard shows, so the two cannot drift into naming different causes.
    ``log_details`` is for the request path only: the dashboard reads this on
    every load and must not write a log line each time.
    """
    reason = ""
    action = None
    if _CLI_MODE:
        reason = "Terminal mode is holding the library."
        msg = ("Terminal (CLI) mode is on, so downloads and scans are paused "
               "here. Resume on Settings → Mode (Resume web app).")
        action = {"href": "/settings#mode", "label": "Open Settings"}
    elif _LOCK_BUSY_PID is not None:
        reason = "Another Qobuz Librarian run is using the library."
        msg = ("Another Qobuz Librarian run is active. Downloads and scans are "
               "paused so only one process writes to the library at a time. "
               "Stop the other run first, then restart Qobuz Librarian.")
    elif (unwritable := _unwritable_volumes()):
        reason = "A folder Qobuz Librarian must write to is read-only."
        action = {"href": "/settings#diagnostics-list", "label": "Open Diagnostics"}
        msg = (f"Required {plural(len(unwritable), 'volume')} not "
               "writable: "
               f"{', '.join(unwritable)}. On a NAS, set "
               "PUID/PGID to the share owner and confirm the host "
               "directories exist. Downloads can't run until fixed.")
    elif _LOCK_UNENFORCEABLE:
        reason = "The data folder can't hold the safety lock."
        msg = ("The data folder can't hold the single-writer safety lock "
               "(read-only, or a mount without file locking), so a second "
               "run writing the library at the same time would go unnoticed. "
               "Downloads and scans are paused. Move the data folder to a "
               "writable filesystem that supports file locking, then restart.")
    elif _STARTUP_RECOVERY_UNKNOWN:
        reason = "Interrupted work could not be checked safely."
        msg = (
            "Qobuz Librarian acquired the safety lock but could not read its "
            "saved recovery state. The lock was released, downloads and scans "
            "remain paused, and the app will retry automatically. Check the "
            "data-folder permissions if this notice remains."
        )
    elif not _run_lock_intact():
        reason = "The safety lock that keeps one writer at a time was lost."
        msg = ("The single-writer safety lock was lost. Downloads and scans "
               "are paused so another process cannot write to the library "
               "at the same time. Restart Qobuz Librarian before continuing.")
    elif (_startup_recovery_status_value() == "attention_required"):
        relocation = _post_import_relocation_recovery()
        if relocation is not None:
            from qobuz_librarian.queue.startup_recovery import (
                POST_IMPORT_RELOCATION_LOG_ENTRY,
            )

            paths = "; ".join(str(path) for path in relocation.paths)
            # relocation.reason is str(exc) from the relocation code; an
            # internal diagnostic, not an explanation. It belongs in the log,
            # which this message points at; the user gets what happened to
            # their music and what to do.
            if log_details:
                logging.getLogger("qobuz_librarian").warning(
                    "post-import relocation recovery: %s (paths: %s)",
                    relocation.reason or "reason not reported",
                    paths or "none reported")
            reason = "A move of album folders inside your library was interrupted."
            msg = (
                "Qobuz Librarian can't confirm that move finished, so downloads "
                "and scans are paused and your files are left exactly as they "
                "are. Nothing has been lost. "
                + (f"The folders involved: {paths}. " if paths else "")
                + "Restart Qobuz Librarian; if this screen comes back, the "
                f"“{POST_IMPORT_RELOCATION_LOG_ENTRY}” entry in the container "
                "log has the technical detail."
            )
        else:
            reason = "An interrupted download couldn't be verified."
            # A terminal download never became a web job, so it has no History
            # row and no Retry button. Point each origin at the surface that
            # can settle it, the way the resume_required branch below does.
            origin = _startup_recovery_origin_value()
            paused = ("Downloads and scans are paused, and its saved queue and "
                      "staged files were left unchanged. ")
            if origin == "cli":
                action = {"href": "/settings#mode", "label": "Open Settings"}
                msg = ("An interrupted terminal download could not be verified "
                       "safely. " + paused + "Switch to terminal mode in "
                       "Settings and run Qobuz Librarian there; it offers to "
                       "settle this.")
            elif origin == "web-job":
                msg = ("An interrupted download could not be verified safely. "
                       + paused + "Open that download from Queue or History "
                       "and use Retry to settle it; if it stays blocked, check "
                       "the application log.")
            else:
                msg = ("An interrupted download could not be verified safely. "
                       + paused + "Settle it from the interface it was started "
                       "in; if it stays blocked, check the application log.")
    elif (
        _startup_recovery_status_value() == "resume_required"
        and not _durable_resume_allowed(durable_resume_job_id or "")
    ):
        reason = "An interrupted download is waiting to be settled."
        origin = _startup_recovery_origin_value()
        if origin == "cli":
            action = {"href": "/settings#mode", "label": "Open Settings"}
            msg = ("An interrupted terminal download has saved recovery state. "
                   "Other library changes are paused. Switch to terminal mode "
                   "in Settings, then resume that download there.")
        elif origin == "web-job":
            msg = ("An interrupted download has saved recovery state. Other "
                   "library changes are paused until that exact download is "
                   "retried from Queue or History.")
        else:
            msg = ("An interrupted download has saved recovery state. Other "
                   "library changes are paused until that exact download is "
                   "resumed from the interface where it started.")
    else:
        return None
    return {"reason": reason, "msg": msg, "action": action}


def _lock_busy_response(request, *, durable_resume_job_id: str | None = None):
    """Return a 503 response if web writes are paused, else None."""
    notice = _writes_paused_notice(
        durable_resume_job_id=durable_resume_job_id,
        log_details=True,
    )
    if notice is None:
        return None
    unwritable_now = _unwritable_volumes()
    if _is_htmx(request):
        return HTMLResponse(
            _ql_notice_html("error", html.escape(notice["msg"])),
            status_code=200)
    return _tr(request, "lock_busy.html",
               {"msg": notice["msg"], "reason": notice["reason"],
                "action": notice["action"],
                # "Try again" only helps where retrying can succeed; the rest
                # need something fixed first and the button was false comfort.
                "can_retry": _LOCK_BUSY_PID is not None or bool(unwritable_now)},
               status_code=503)


def _web_writes_paused() -> bool:
    """True when destructive web work must not run: the same conditions
    _lock_busy_response answers 503 for, as one predicate for the AUTOMATIC
    triggers (dashboard new-release check, library-scan resume) that have no
    request to bounce. Any trigger checking only part of this list quietly
    re-opens the hole the pause exists to close."""
    return (
        _SHUTTING_DOWN
        or _CLI_MODE
        or _LOCK_BUSY_PID is not None
        or bool(_unwritable_volumes())
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
    # The label (the container-internal mount name) is what the operator
    # checks in their compose.yaml; the resolved cfg path is what's tested.
    for label, path in (("STAGING_DIR", cfg.STAGING_DIR),
                        ("MUSIC_ROOT", cfg.MUSIC_ROOT)):
        p = Path(path)
        unreachable = not p.exists()
        not_a_dir = p.exists() and not p.is_dir()
        unwritable = p.exists() and p.is_dir() and not os.access(str(p), os.W_OK)
        if unreachable or not_a_dir or unwritable:
            problems.append(
                f"{label}={path!s}"
                + (" (missing)" if unreachable
                   else " (not a directory)" if not_a_dir
                   else " (read-only)"))
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
        logging.getLogger("qobuz_librarian").exception(
            "couldn't release a rejected Web run-lock lease"
        )


def _recover_under_web_run_lock(lease, *, restore_jobs: bool = True):
    """Publish a lease only while recovery and saved jobs are reconciled."""
    global _RUN_LOCK_HANDLE
    _RUN_LOCK_HANDLE = lease
    try:
        result = _record_startup_recovery(lease)
        from qobuz_librarian.library import generation_state

        publication_recovery = (
            generation_state.reconcile_interrupted_library_publication(lease)
        )
        if publication_recovery is None:
            raise RuntimeError(
                "interrupted Library publication state could not be saved"
            )
        if publication_recovery:
            logging.getLogger("qobuz_librarian").warning(
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


async def _retry_web_run_lock(run_lock, log, *, delay: float = 30) -> None:
    """Retry a busy lock and an acquired lease whose recovery read failed."""
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _LOCK_UNENFORCEABLE
    while _LOCK_BUSY_PID is not None or _STARTUP_RECOVERY_UNKNOWN:
        await asyncio.sleep(delay)
        with _auto_check_lock:
            if _CLI_MODE:
                return
        try:
            lease = run_lock.acquire()
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
    from qobuz_librarian.library.candidate_premise import validate_all

    validate_all(chosen)
    token = _review_download_token(job)
    # The live request above can take long enough for a local edit to land.
    # Repeat the exact receipt check before the flow reaches its first backup.
    validate_all(chosen)
    return execute(job, chosen, token)


def _resume_album_download(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_albums)


def _resume_upgrade(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_upgrades)


def _resume_repair(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_repairs)


def _resume_migration(job, args):
    from qobuz_librarian.web import flows
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
    from qobuz_librarian.web import flows

    def execute(j, chosen):
        from qobuz_librarian.library.candidate_premise import validate_all

        validate_all(chosen)
        return flows.execute_downsamples(
            j,
            chosen,
            token=None,
            keep_originals=_job_downsample_keep_originals(j),
        )

    return execute


def _upgrade_available(creds_ok: bool | None = None) -> bool:
    return bool(getattr(cfg, "UPGRADE_SCAN_ENABLED", True))


def _upgrade_unavailable_response():
    from qobuz_librarian.web import review_badges
    review_badges.clear_ready("upgrade")
    return RedirectResponse(url="/", status_code=303)


def _upgrade_state_summary():
    from qobuz_librarian.library import generation_state
    from qobuz_librarian.quality import upgrade_state

    state = upgrade_state.load()
    authority = generation_state.load()
    generation = int(authority.get("generation") or 0)
    output = generation_state.output_state("upgrade", authority)
    snapshot_current = bool(
        generation > 0
        and int(state.get("generation") or 0) == generation
        and output.get("status") == "current"
        and int(output.get("revision") or 0) == int(state.get("revision") or 0)
    )
    complete = bool(state.get("complete") and snapshot_current)
    candidates = (
        _visible_saved_review_candidates("upgrade", state.get("candidates") or [])
        if complete else [])
    saved_quality_signature = str(state.get("quality_signature") or "")
    current_quality_signature = _effective_upgrade_quality_signature()
    updated_at = state.get("updated_at")
    return {
        "complete": complete,
        "candidates": candidates,
        "count": len(candidates),
        "quality_signature": saved_quality_signature,
        "generation": generation,
        "status": (
            "current" if complete
            else "baseline_missing" if not generation
            else "stale"
        ),
        "stale": bool(
            generation
            and (
                not complete
                or saved_quality_signature != current_quality_signature
            )
        ),
        "updated": _format_age(updated_at) if updated_at else None,
    }


def _effective_upgrade_quality_signature():
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import settings_store

    values = settings_store.current()
    return upgrade_state.quality_signature(
        values.get("STREAMRIP_QUALITY"),
        values.get("PREFER_HIRES"),
    )


def _downsample_state_summary():
    from qobuz_librarian.library import downsample_state, generation_state

    state = downsample_state.load()
    authority = generation_state.load()
    generation = int(authority.get("generation") or 0)
    output = generation_state.output_state("downsample", authority)
    snapshot_current = bool(
        int(state.get("generation") or 0) == generation
        and output.get("status") == "current"
        and int(output.get("revision") or 0) == int(state.get("revision") or 0)
    )
    complete = bool(state.get("complete") and snapshot_current)
    candidates = (
        _visible_saved_review_candidates("downsample", state.get("candidates") or [])
        if complete else [])
    updated_at = state.get("updated_at")
    return {
        "complete": complete,
        "candidates": candidates,
        "count": len(candidates),
        "generation": generation,
        "status": "current" if complete else "stale",
        "stale": bool(state.get("updated_at") and not complete),
        "updated": _format_age(updated_at) if updated_at else None,
    }


def _visible_saved_review_candidates(surface, candidates):
    if surface == "upgrade":
        from qobuz_librarian.quality import upgrade_state
        return upgrade_state.visible_candidates({
            "complete": True,
            "candidates": list(candidates or []),
        })
    if surface == "downsample":
        from qobuz_librarian.library import downsample_state
        return downsample_state.visible_candidates({
            "complete": True,
            "candidates": list(candidates or []),
        })
    return list(candidates or [])


def _saved_review_row(surface, spec):
    if surface == "downsample":
        payload = spec.get("payload") or {}
        row_payload = {
            "album_dir": spec.get("album_dir") or payload.get("album_dir") or "",
            "est_saving": spec.get("est_saving") or payload.get("est_saving") or 0,
        }
        premise = spec.get("_premise") or payload.get("_premise")
        if premise is not None:
            row_payload["_premise"] = premise
    else:
        row_payload = spec.get("payload") or {}
    return {
        "title": spec.get("title") or "?",
        "artist": spec.get("artist") or "",
        "detail": spec.get("detail") or "",
        "payload": row_payload,
    }


def _saved_review_key(surface, spec):
    return json.dumps(
        _saved_review_row(surface, spec),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _saved_review_claim_key(surface, spec):
    """Return the stable identity of one executable saved-state action."""
    row = _saved_review_row(surface, spec)
    payload = row["payload"]
    if surface == "upgrade":
        album_id = payload.get("album_id")
        if (isinstance(album_id, (str, int))
                and not isinstance(album_id, bool)
                and str(album_id).strip()):
            return surface, "album_id", str(album_id).strip()
    elif surface == "downsample":
        album_dir = payload.get("album_dir")
        if isinstance(album_dir, str) and album_dir:
            return surface, "album_dir", album_dir
    return surface, "row", _saved_review_key(surface, row)


def _saved_review_signature(surface, state):
    rows = []
    for spec in state.get("candidates") or []:
        rows.append(_saved_review_row(surface, spec))
    rows.sort(key=lambda row: json.dumps(
        row, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    signature_data = {"rows": rows}
    if surface == "upgrade":
        signature_data["quality_signature"] = str(
            state.get("quality_signature") or ""
        )
    raw = json.dumps(signature_data, sort_keys=True, ensure_ascii=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _saved_review_specs_from_job(surface, job):
    with job._lock:
        candidates = list(job.candidates)
    specs = []
    for c in candidates:
        payload = c.get("payload") or {}
        if surface == "downsample":
            spec = {
                "title": c.get("title") or "?",
                "artist": c.get("artist") or "",
                "detail": c.get("detail") or "",
                "album_dir": payload.get("album_dir") or "",
                "est_saving": payload.get("est_saving") or 0,
            }
            if payload.get("_premise") is not None:
                spec["_premise"] = payload["_premise"]
            specs.append(spec)
        else:
            specs.append({
                "title": c.get("title") or "?",
                "artist": c.get("artist") or "",
                "detail": c.get("detail") or "",
                "payload": payload,
            })
    return specs


def _existing_saved_review_job(surface, signature):
    review_jobs = [
        job for job in job_mgr.registry.all()
        if job.execute_kind == surface and job.status in job_mgr.ACTIVE
    ]
    for job in review_jobs:
        if (job.execute_kind == surface
                and getattr(job, "_saved_review_signature", None) == signature):
            current_signature = _saved_review_signature(
                surface, {
                    "candidates": _saved_review_specs_from_job(surface, job),
                    "quality_signature": (job.execute_args or {}).get(
                        "quality_signature", ""),
                })
            if current_signature == signature:
                return job
    for job in review_jobs:
        current_signature = _saved_review_signature(
            surface, {
                "candidates": _saved_review_specs_from_job(surface, job),
                "quality_signature": (job.execute_args or {}).get(
                    "quality_signature", ""),
            })
        if current_signature == signature:
            job._saved_review_signature = signature
            return job
    return None


def _active_saved_review_claim(surface, state):
    """Find a running review that already owns any requested action."""
    requested = {
        _saved_review_claim_key(surface, spec)
        for spec in state.get("candidates") or []
    }
    if not requested:
        return None
    executing = (job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING)
    for job in job_mgr.registry.all():
        if job.execute_kind != surface:
            continue
        with job._lock:
            if job.status not in executing:
                continue
            claimed = {
                _saved_review_claim_key(surface, candidate)
                for candidate in job.candidates
                if candidate.get("selected")
            }
        if requested.intersection(claimed):
            return job
    return None


_SAVED_REVIEW_TITLES = {
    "upgrade": "Upgrade candidates",
    "downsample": "Downsample candidates",
}
_SAVED_REVIEW_LOCK = threading.RLock()


def _stale_saved_review_job(surface):
    review_jobs = [
        job for job in job_mgr.registry.awaiting_review()
        if job.execute_kind == surface
    ]
    for job in reversed(review_jobs):
        if (getattr(job, "_saved_review_signature", None) is not None
                or job.title == _SAVED_REVIEW_TITLES.get(surface)):
            return job
    return None


def _candidate_from_saved_spec(surface, spec, *, cid, seq, selected):
    row = _saved_review_row(surface, spec)
    return {
        "cid": cid,
        "seq": seq,
        "kind": surface,
        "title": row["title"],
        "artist": row["artist"],
        "detail": row["detail"],
        "payload": row["payload"],
        "selected": bool(selected),
    }


def _sync_saved_review_job(job, surface, state, signature):
    """Bring an existing saved-state review job back in line after restore/hide.

    The job is the user's live review session, so preserve ticks for candidates
    that still exist and add restored saved candidates unticked.
    """
    desired = list(state.get("candidates") or [])

    def _sync():
        # A review that already left AWAITING_REVIEW must not be replaced
        # underneath its approval.
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return False
        existing_raw_by_key = {
            _saved_review_key(surface, c): c
            for c in job.candidates
        }
        next_seq = max(
            [int(c.get("seq", -1)) for c in job.candidates
             if str(c.get("seq", "")).lstrip("-").isdigit()] + [-1]
        ) + 1
        if job._cand_seq < next_seq:
            job._cand_seq = next_seq
        rebuilt = []
        for spec in desired:
            key = _saved_review_key(surface, spec)
            old = existing_raw_by_key.get(key)
            if old is not None and old.get("cid") is not None:
                cid = old["cid"]
                seq = old.get("seq")
                if not isinstance(seq, int):
                    seq = job._cand_seq
                    job._cand_seq += 1
                selected = bool(old.get("selected"))
            else:
                cid = f"c{job._cand_seq}"
                seq = job._cand_seq
                job._cand_seq += 1
                selected = False
            rebuilt.append(
                _candidate_from_saved_spec(
                    surface, spec, cid=cid, seq=seq, selected=selected)
            )
        job.candidates = rebuilt
        job._saved_review_signature = signature
        if surface == "upgrade":
            job.execute_args = {
                **(job.execute_args or {}),
                "quality_signature": str(state.get("quality_signature") or ""),
            }
        n = len(rebuilt)
        if surface == "downsample":
            job.summary = f"{n} album{'s' if n != 1 else ''} can be downsampled."
        else:
            job.summary = (
                f"{n} upgrade candidate{'s' if n != 1 else ''} ready to review.")
        return True

    from qobuz_librarian.web import job_persistence
    saved, synced = job_persistence.persist_review_mutation(job, _sync)
    if not saved:
        job.notify_review_changed("save_failed")
        return None
    if not synced:
        return job
    job.notify_review_changed()
    return job


def _publish_saved_review(job):
    """Admit one reconstructed review before exposing it in memory."""
    from qobuz_librarian.web import job_persistence

    if _web_writes_paused():
        return False
    if not job_persistence.admit(job):
        return False
    job_mgr.registry.add(job)
    return True


def _sync_saved_review_before_approve(job):
    """Confirm saved state still matches without mutating the parked review."""
    surface = job.execute_kind
    if surface not in _SAVED_REVIEW_TITLES:
        return job
    if (getattr(job, "_saved_review_signature", None) is None
            and job.title != _SAVED_REVIEW_TITLES.get(surface)):
        return job
    with _SAVED_REVIEW_LOCK:
        state = (
            _upgrade_state_summary()
            if surface == "upgrade"
            else _downsample_state_summary()
        )
        current = {
            "candidates": state["candidates"] if state.get("complete") else [],
        }
        if surface == "upgrade":
            current["quality_signature"] = state.get("quality_signature", "")
        saved_signature = _saved_review_signature(surface, current)
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return job
        review_signature = _saved_review_signature(
            surface,
            {
                "candidates": _saved_review_specs_from_job(surface, job),
                "quality_signature": (job.execute_args or {}).get(
                    "quality_signature", ""
                ),
            },
        )
        return job if review_signature == saved_signature else False


def _review_job_from_upgrade_state(state):
    with _SAVED_REVIEW_LOCK:
        claimed = _active_saved_review_claim("upgrade", state)
        if claimed is not None:
            return claimed
        signature = _saved_review_signature("upgrade", state)
        existing = _existing_saved_review_job("upgrade", signature)
        if existing is not None:
            return existing
        stale = _stale_saved_review_job("upgrade")
        if stale is not None:
            return _sync_saved_review_job(stale, "upgrade", state, signature)
        job = job_mgr.Job(title="Upgrade candidates")
        job.kind = "scan"
        job.execute_kind = "upgrade"
        job.execute_args = {
            "quality_signature": str(state.get("quality_signature") or ""),
        }
        job.review_verb = "Upgrade"
        job._saved_review_signature = signature
        job._execute_fn = _resume_upgrade(job, job.execute_args)
        for spec in state.get("candidates") or []:
            job.add_candidate(
                kind="upgrade",
                title=spec.get("title") or "?",
                artist=spec.get("artist") or "",
                detail=spec.get("detail") or "",
                payload=spec.get("payload") or {},
                selected=False,
            )
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        n = len(job.candidates)
        job.summary = f"{n} upgrade candidate{'s' if n != 1 else ''} ready to review."
        if not _publish_saved_review(job):
            return None
        return job


def _review_job_from_downsample_state(state):

    with _SAVED_REVIEW_LOCK:
        claimed = _active_saved_review_claim("downsample", state)
        if claimed is not None:
            return claimed
        signature = _saved_review_signature("downsample", state)
        existing = _existing_saved_review_job("downsample", signature)
        if existing is not None:
            return existing
        stale = _stale_saved_review_job("downsample")
        if stale is not None:
            return _sync_saved_review_job(stale, "downsample", state, signature)
        job = job_mgr.Job(title="Downsample candidates")
        job.kind = "scan"
        job.execute_kind = "downsample"
        job.review_verb = "Downsample"
        job._saved_review_signature = signature
        job._execute_fn = _resume_downsample(job, job.execute_args)
        for spec in state.get("candidates") or []:
            payload = {
                "album_dir": spec.get("album_dir") or "",
                "est_saving": spec.get("est_saving") or 0,
            }
            if spec.get("_premise") is not None:
                payload["_premise"] = spec["_premise"]
            job.add_candidate(
                kind="downsample",
                title=spec.get("title") or "?",
                artist=spec.get("artist") or "",
                detail=spec.get("detail") or "",
                payload=payload,
                selected=False,
            )
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        n = len(job.candidates)
        job.summary = f"{n} album{'s' if n != 1 else ''} can be downsampled."
        if not _publish_saved_review(job):
            return None
        return job


def _review_job_from_current_saved_state(surface):
    """Build or reuse one review from a state snapshot taken atomically.

    Hide and approval use the same lock. A delayed request therefore cannot
    reintroduce a candidate that was just hidden or create a second review for
    work that an existing job has already claimed.
    """
    with _SAVED_REVIEW_LOCK:
        if surface == "upgrade":
            state = _upgrade_state_summary()
            factory = _review_job_from_upgrade_state
        elif surface == "downsample":
            state = _downsample_state_summary()
            factory = _review_job_from_downsample_state
        else:
            raise ValueError("unsupported saved review surface")
        if not state["complete"] or not state["candidates"]:
            return None
        if surface == "upgrade" and state.get("stale"):
            return None
        return factory(state)


def _review_job_from_library_state():
    """Rebuild the parked Library review from the saved baseline scan when no
    live job holds the /library surface, so a review lost to a swept cancel,
    a discarded scan job, or a corrupt persisted row on restart comes back
    instead of stranding the user on 'Baseline ready' with no tabs. Mirrors
    the Upgrade/Downsample saved-state reconstruction; the live job stays
    primary (callers only reach here when _library_current_job() is None).
    """
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    with library_scan_state.review_state_lock(), _SAVED_REVIEW_LOCK:
        mstate = library_scan_state.kind_state("missing")
        if not mstate.get("complete"):
            return None
        full = library_scan_state.load()
        missing_generation = int(mstate.get("generation") or 0)
        # Generation is the durable review identity. Keep the timestamp
        # fallback only for a pre-generation saved snapshot.
        missing_updated = float(mstate.get("updated_at") or 0.0)
        retired_generation = int(full.get("review_retired_generation") or 0)
        retired_at = float(full.get("review_retired_at") or 0.0)
        if (
            missing_generation
            and retired_generation == missing_generation
        ) or (
            not missing_generation
            and retired_at
            and retired_at >= missing_updated
        ):
            return None
        hidden = hidden_mod.load()
        specs = []
        for name, entry in (mstate.get("artists") or {}).items():
            for spec in (entry or {}).get("candidates") or []:
                artist = spec.get("artist") or name
                title = spec.get("title") or ""
                if hidden_mod.is_hidden(
                        hidden_mod.SCOPE_MISSING,
                        artist,
                        title,
                        hidden,
                        year=(spec.get("payload") or {}).get("year"),
                ):
                    continue
                specs.append(spec)
        if not specs:
            return None
        existing = _library_current_job()  # re-check under the lock
        if existing is not None:
            return existing
        job = job_mgr.Job(title="Library scan")
        job.kind = "scan"
        job.execute_kind = "library"
        job.execute_args = {
            "_library_review_generation": (
                missing_generation if missing_generation else missing_updated
            ),
        }
        job._execute_fn = _resume_album_download(job, job.execute_args)
        for spec in specs:
            job.add_candidate(
                kind=spec.get("kind", "album"),
                title=spec.get("title") or "?",
                artist=spec.get("artist") or "",
                detail=spec.get("detail") or "",
                payload=spec.get("payload") or {},
                selected=False,
            )
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        from qobuz_librarian.web import flows
        job.summary = (flows.library_review_summary(job.candidates)
                       + ", from your last library scan.")
        if not _publish_saved_review(job):
            return None
        return job


# Names the persisted ``execute_kind`` strings so jobs survive a restart even
# though their original execute closure is gone.
_RESUME_EXECUTE: dict = {
    "library":      _resume_album_download,
    "new_releases": _resume_album_download,
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
        try:
            job_mgr.restore_jobs(
                _RESUME_EXECUTE,
                durable_recovery_clear=(
                    _startup_recovery_status_value() == "clear"
                ),
                durable_recovery_job_id=_startup_recovery_web_job_id(),
            )
        except Exception as exc:
            logging.getLogger("qobuz_librarian").warning(
                "couldn't restore prior jobs: %s. Starting fresh.",
                exc,
            )
        finally:
            # restore_jobs publishes into the registry only after it has built
            # the full batch.
            _JOBS_RESTORED = True


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    import logging
    import os
    import shutil
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE, _LOCK_UNENFORCEABLE
    global _SHUTTING_DOWN, _STARTUP_RECOVERY_RESULT, _STARTUP_RECOVERY_UNKNOWN
    global _JOBS_RESTORED
    _SHUTTING_DOWN = False
    with _JOBS_RESTORE_LOCK:
        _JOBS_RESTORED = False
    _STARTUP_RECOVERY_RESULT = None
    _STARTUP_RECOVERY_UNKNOWN = False
    job_mgr.set_durable_recovery_job_id(None)
    _log = logging.getLogger("qobuz_librarian")
    from qobuz_librarian.ui_cli.logging import attach_file_handler
    attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL)
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
                "The seeded web login could not be saved; refusing to start "
                "with an open first-run setup screen."
            )
        if cred_status == "applied":
            _log.info("Configured the web login from WEB_AUTH_USER / "
                      "WEB_AUTH_PASSWORD.")
        elif cred_status == "partial":
            _log.warning("Set both WEB_AUTH_USER and WEB_AUTH_PASSWORD to seed "
                         "the web login from the environment: only one was set.")
        elif cred_status == "failed":
            _log.warning("Couldn't write the web login from the environment; "
                         "the data volume may not be writable.")
        if not web_auth.credentials_configured():
            _log.warning(
                "No web login configured. The open /setup screen is reachable "
                "to whoever hits the port first, who would then own the admin "
                "account. Seed WEB_AUTH_USER / WEB_AUTH_PASSWORD (compose) to "
                "close this window, and complete setup promptly on a trusted "
                "network.")
    from qobuz_librarian import run_lock
    from qobuz_librarian.api.auth import sync_streamrip_creds_from_env
    from qobuz_librarian.web import settings_store
    settings_store.load()
    try:
        cfg.validate_storage_roots()
    except ValueError as exc:
        raise RuntimeError(f"invalid storage paths: {exc}") from None
    # If creds are provided via env vars, mirror them into the streamrip
    # config now so web-triggered downloads don't fail on streamrip's
    # interactive auth prompt (the env-var path doesn't otherwise reach
    # streamrip's own config file).
    if sync_streamrip_creds_from_env() is False:
        _log.warning("Couldn't write env credentials into the streamrip "
                     "config; web downloads may fail until creds are set "
                     "via the Settings page.")
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
            _RUN_LOCK_HANDLE = run_lock.acquire()
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
            asyncio.create_task(_retry_web_run_lock(run_lock, _log))
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
                from qobuz_librarian.library import flac_cache
                n_pruned = await asyncio.get_running_loop().run_in_executor(
                    None, flac_cache.prune_missing)
                if n_pruned:
                    _log.info("Pruned %d stale tag-cache entries.", n_pruned)
            except Exception as e:
                _log.debug("flac-cache prune error: %s", e)
            try:
                from qobuz_librarian.library import repair_cache
                repair_cache.prune_expired()
            except Exception as e:
                _log.debug("repair-cache prune error: %s", e)
        maintenance_task = None
        run_startup_maintenance = _has_startup_write_authority()
        if run_startup_maintenance:
            # The CLI runs these too. A browsing-only Web process must leave them
            # alone because the CLI or another Web process can own the library.
            try:
                from qobuz_librarian.library.backup import cleanup_old_upgrade_backups
                n = cleanup_old_upgrade_backups()
                if n:
                    _log.info(
                        "Cleaned up %s at startup.",
                        plural(n, "stale upgrade backup"),
                    )
            except Exception as e:
                _log.debug("upgrade-backup cleanup error at startup: %s", e)
            try:
                from qobuz_librarian.integrations.lyrics import _prune_lyric_state_orphans
                _prune_lyric_state_orphans()
            except Exception as e:
                _log.debug("lyric-state prune error at startup: %s", e)
        job_mgr.configure_staging_entry_guard(_staging_entry_allowed)
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
        from qobuz_librarian.api.auth import register_auth_state_listener
        register_auth_state_listener(_on_auth_state)

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
    from qobuz_librarian.api.client import probe_qobuz

    return probe_qobuz(token, report_auth=False)


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


def _recent_empty_hint() -> str:
    if not _read_creds().get("auth_token"):
        return "Set up Qobuz before searching."
    return "Search above to find an artist, album, or track."


async def _probe_token():
    """One-shot startup check that the saved token still authenticates.

    Sets ``_TOKEN_VALID`` to True/False/None: None means the result is
    inconclusive (no token saved, or the probe couldn't reach Qobuz), so
    the dashboard treats it as "don't nag yet."
    """
    credentials = _credentials_snapshot()
    if not credentials.configured:
        return
    from qobuz_librarian.api.client import call_within
    try:
        verdict = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, lambda: call_within(cfg.WEB_TEST_AUTH_TIMEOUT,
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


app = FastAPI(title="Qobuz Librarian", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)

# AuthMiddleware is added first so it ends up innermost, so it runs after the
# CSRF middleware, which keeps CSRF validation on the login/setup POSTs and
# lets the redirects it returns pick up the security headers.
app.add_middleware(web_auth.AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(StripServerHeaderMiddleware)

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
    from qobuz_librarian.web import settings_store

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
        if p.is_dir():
            return True
        return not p.parent.is_dir()
    except OSError:
        return True


def _recovery_missing(recovery) -> bool:
    """Whether an exact recovery folder is gone under a mounted parent."""
    return not _recovery_on_disk(recovery)


templates.env.globals["recovery_on_disk"] = _recovery_on_disk


def _fmt_clock(ts):
    from datetime import datetime
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
        if not bits or not rate:
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
    """Drop a trailing "see the log" from a message when there is no log.

    Log lines live in memory, so anything that outlived a restart has none, and
    the pointer was printed directly above "No log output was retained for this
    job."
    """
    if log_lines:
        return message
    return _LOG_POINTER_RE.sub("", message or "").strip() or message


templates.env.globals["fmt_clock"] = _fmt_clock
templates.env.globals["fmt_elapsed"] = _fmt_elapsed
templates.env.globals["quality_shortfall_view"] = _quality_shortfall_view
templates.env.filters["strip_log_pointer"] = _strip_log_pointer
# Whether to show a Log out control. True only when auth is on and set up.
templates.env.globals["auth_active"] = web_auth.auth_active

static_dir = _here / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the app icon at the well-known path so the browser's automatic
    /favicon.ico probe (allowlisted past auth in web/auth.py) doesn't 404. The
    HTML pages also carry a <link rel="icon">; this covers the bare probe.
    The 192px icon, not the 512px one: a favicon renders at 16-32px and the
    full-size PNG is dead weight on every cold load."""
    return FileResponse(static_dir / "icon-192.png", media_type="image/png")


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


# Bake the asset version into the worker so its cache name changes whenever
# the served assets change.
_SW_JS = (static_dir / "sw.js").read_text(encoding="utf-8").replace(
    "__APP_VERSION__", _ASSET_VERSION)


@app.get("/sw.js")
async def service_worker():
    return Response(
        _SW_JS,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/healthz")
async def healthz():
    """Cheap process liveness probe for uptime monitors."""
    return JSONResponse({"ok": True})


@app.head("/healthz")
async def healthz_head():
    """Uptime monitors HEAD before GET, so return a body-less 200 so they
    don't mark the service down on a 405."""
    return Response(status_code=200)


@app.get("/readyz")
async def readyz():
    status_code, report = _readiness_report()
    return JSONResponse(report, status_code=status_code)


@app.head("/readyz")
async def readyz_head():
    status_code, _report = _readiness_report()
    return Response(status_code=status_code)


@app.head("/queue")
async def queue_head():
    return Response(status_code=200)


@app.head("/settings")
async def settings_head():
    return Response(status_code=200)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if not web_auth.credentials_configured():
        return RedirectResponse(url="/setup", status_code=303)
    cookie = request.cookies.get(web_auth.SESSION_COOKIE)
    if cookie and web_auth.verify_session(cookie):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"error": "",
                 "next_path": web_auth.safe_next_path(
                     request.query_params.get("next"))})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(""),
                       password: str = Form(""), next: str = Form("")):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if not web_auth.credentials_configured():
        return RedirectResponse(url="/setup", status_code=303)
    # Where to land after signing in: the deep link that bounced here, kept
    # through failed attempts and re-validated so the form can't smuggle in an
    # off-site redirect.
    next_path = web_auth.safe_next_path(next)
    ip = (request.client.host if request.client else "") or "unknown"
    # A request already carrying a valid session is provably the logged-in user,
    # not the brute-forcer the throttle exists to stop, so exempt it so a remote
    # flood of failed logins for the admin username can't lock the real admin out.
    cookie = request.cookies.get(web_auth.SESSION_COOKIE)
    has_session = bool(cookie) and web_auth.verify_session(cookie)
    if not has_session and not web_auth.check_login_rate_limit(ip, username):
        # The throttle is checked before the password is verified on purpose, so
        # a correct password can't clear it. Say how long is actually left, and
        # name the way out: the counters live in memory, so a restart clears
        # them and the owner of the box can always do that.
        left = web_auth.login_lockout_remaining(ip, username)
        mins = max(1, (left + 59) // 60)
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": f"Too many failed attempts. Try again in "
                              f"{mins} minute{'s' if mins != 1 else ''}, or "
                              f"restart Qobuz Librarian to clear it.",
                     "username": username.strip(),
                     "next_path": next_path},
            status_code=429)
    # A submission that could never succeed shouldn't cost a strike: an empty
    # field is a slip, not an attempt, and five of them locked the owner out.
    if not username.strip() or not password:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Enter your username and password.",
                     "username": username.strip(),
                     "next_path": next_path},
            status_code=400)
    # Offload the 600k-round PBKDF2 to a thread so one login attempt can't stall
    # the single-worker event loop (health, API and SSE all freeze during a KDF
    # that runs on the loop thread).
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None, web_auth.verify_login, username.strip(), password)
    if not ok:
        web_auth.record_login_failure(ip, username)
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Incorrect username or password.",
                     # Keep what they typed, as the setup screen already does.
                     "username": username.strip(),
                     "next_path": next_path},
            status_code=401)
    web_auth.clear_login_failures(ip, username)
    resp = RedirectResponse(url=next_path or "/", status_code=303)
    try:
        web_auth.set_session_cookie(resp, request)
    except web_auth.SessionPersistenceError:
        logging.getLogger("qobuz_librarian").warning(
            "Couldn't persist a new web session; login refused.")
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Couldn't save your session. Check that the "
                              "data volume is writable, then try again.",
                     "username": username.strip(),
                     "next_path": next_path},
            status_code=503)
    return resp


@app.post("/logout")
async def logout(request: Request):
    # Revoke the session server-side, not just the browser cookie; otherwise a
    # captured cookie value stays valid for its full 30-day lifetime.
    if not web_auth.revoke_session(
        request.cookies.get(web_auth.SESSION_COOKIE)
    ):
        return render_error_page(
            request,
            503,
            "Couldn't log out",
            "The session store couldn't be saved, so nothing changed. Check "
            "that the data volume is writable, then try logging out again.",
        )
    resp = RedirectResponse(url="/login", status_code=303)
    resp.headers["Cache-Control"] = "no-store"
    web_auth.clear_session_cookie(resp)
    return resp


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if web_auth.credentials_configured():
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="setup.html",
                                      context={"error": "", "username": ""})


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, username: str = Form(""),
                       password: str = Form(""), confirm: str = Form("")):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if web_auth.credentials_configured():
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"setup_conflict": True}, status_code=409)
    user = username.strip()
    if not user:
        err = "Pick a username."
    elif password_error := web_auth.new_password_error(user, password):
        err = password_error
    elif password != confirm:
        err = "The two passwords don't match."
    else:
        err = ""
    if err:
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"error": err, "username": user}, status_code=400)
    # First-run setup is unauthenticated by necessity (no creds exist yet), so
    # whoever reaches the open port first claims admin.
    _ip = (request.client.host if request.client else "") or "unknown"
    import logging as _logging
    _logging.getLogger("qobuz_librarian").warning(
        "First-run /setup creating admin account from %s (username=%r).",
        _ip, user)
    if not web_auth.set_credentials(user, password):
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"error": "Couldn't save the login: the data volume "
                              "isn't writable. Check PUID/PGID and volume "
                              "permissions.", "username": user},
            status_code=500)
    resp = RedirectResponse(url="/", status_code=303)
    try:
        web_auth.set_session_cookie(resp, request)
    except web_auth.SessionPersistenceError:
        logging.getLogger("qobuz_librarian").warning(
            "Couldn't persist the first web session; setup login was saved.")
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Your login was created, but its session couldn't "
                              "be saved. Check that the data volume is writable, "
                              "then sign in.",
                     "username": user,
                     "next_path": ""},
            status_code=503)
    return resp


def _tr(request, name, context, *, status_code=200, review_badge_ack=None):
    """TemplateResponse wrapper for Starlette 1.0+ signature.

    The navbar badge is computed once per full-page render and injected via
    context; partial-fragment renders skip this entirely. A route that already
    fetched the active job list for its own template (`/queue`, the dashboard)
    can pass it as `pending` and the badge derives from that, with no second
    `pending_and_running()` call on the same render.
    """
    if "pending_job_count" not in context or "queue_has_running" not in context:
        active = context.get("pending") or job_mgr.registry.pending_and_running()
        # The badge counts work in flight, not parked reviews; those sit for
        # weeks by design and have their own review-ready dots, so counting
        # them would pin a permanent "1" to the Queue tab.
        in_flight = [j for j in active
                     if j.status != job_mgr.JobStatus.AWAITING_REVIEW]
        context.setdefault("pending_job_count", len(in_flight))
        context.setdefault(
            "queue_has_running",
            any(j.status.value in ('running', 'scanning') for j in in_flight),
        )
    context.setdefault("cli_mode", _CLI_MODE)
    context.setdefault("lock_unenforceable", _LOCK_UNENFORCEABLE)
    # Every tool page offered its Start button while writes were paused and let
    # the POST bounce the user onto a 503. Refuse at offer time, not submit time.
    context.setdefault("writes_paused", _web_writes_paused())
    # Terminal mode is one of eight causes, so carry the true one rather than
    # letting each gated control name the same guess.
    if context["writes_paused"] and "writes_paused_reason" not in context:
        paused = _writes_paused_notice()
        context["writes_paused_reason"] = (
            paused["reason"] if paused else "Downloads and scans are paused."
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
    from qobuz_librarian.web import review_badges
    if review_badge_ack:
        surface, generation = review_badge_ack
        if (surface in review_badges.SURFACES
                and (surface != "upgrade" or context["upgrade_available"])):
            review_badges.mark_seen(surface, generation)
    badges = review_badges.snapshot()
    if not context["upgrade_available"]:
        badges = dict(badges)
        badges["upgrade"] = False
    context.setdefault("nav_review_badges", badges)
    # Finished jobs flagged for review (e.g. a quality shortfall) keep a
    # warning dot on the Queue nav until each flagged job page is opened.
    from qobuz_librarian.web import job_persistence
    context.setdefault("history_attention", job_persistence.attention_count())
    if name in {"job.html", "_job_body.html"}:
        job = context.get("job")
        context.setdefault(
            "downsample_originals_choice",
            (
                _downsample_originals_choice()
                if getattr(job, "execute_kind", "") == "downsample"
                else None
            ),
        )
    if name in {"job.html", "_job_body.html", "history.html"}:
        context.setdefault(
            "durable_recovery_control",
            _durable_recovery_control(),
        )
    if name in {"job.html", "_job_body.html", "queue.html"}:
        context.setdefault(
            "cancel_protected_job_id",
            job_mgr.durable_recovery_job_id(),
        )
    return templates.TemplateResponse(request=request, name=name,
                                      context=context, status_code=status_code)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


async def _initial_artist_search_html(request: Request, query: str) -> str:
    """Render artist-name search results for dashboard links with ?kind=artist&q=."""
    query = str(query or "").strip()[:200]
    if not query:
        return ""
    artist_results = []
    error = None
    try:
        from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
        from qobuz_librarian.api.client import call_within
        from qobuz_librarian.api.search import search_artists
        token = _get_token()
        loop = asyncio.get_running_loop()
        artist_raw = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_within(
                    cfg.WEB_FETCH_TIMEOUT,
                    search_artists,
                    query,
                    token,
                    limit=cfg.ARTIST_LOOKUP_LIMIT,
                ),
            ),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        for a in artist_raw:
            if not a.get("id"):
                continue
            img = a.get("image") or {}
            cover = ""
            if isinstance(img, dict):
                cover = img.get("small") or img.get("thumbnail") or ""
            artist_results.append({
                "id": a.get("id"),
                "name": a.get("name") or "?",
                "cover": cover if str(cover).startswith(
                    "https://static.qobuz.com/") else "",
            })
    except (SystemExit, NoCredsError):
        error = "No Qobuz credentials set. Visit Settings."
    except AuthLost:
        error = "Token is expired or invalid. Update it in Settings."
    except QobuzUnavailable:
        error = ("Qobuz is temporarily unavailable (network or rate limit). "
                 "Try again shortly.")
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."
    except QobuzError:
        error = "Search failed. Try again."
    except Exception:
        import logging
        logging.getLogger("qobuz_librarian").exception(
            "initial artist search failed for %r", query)
        error = "Search failed. Try again."
    # This render bypasses _tr, and an absent writes_paused is falsey, which
    # would offer a live Download control on a paused app the moment this path
    # grows album results.
    paused = _web_writes_paused()
    notice = _writes_paused_notice() if paused else None
    return templates.env.get_template("_search_results.html").render(
        request=request,
        q=query,
        results=[],
        album_groups=[],
        artist_results=artist_results,
        selected_artist=None,
        error=error,
        kind="artist",
        creds_ok=bool(_read_creds().get("auth_token")),
        qobuz_ready=_qobuz_ready(),
        page="search",
        writes_paused=paused,
        writes_paused_reason=(
            notice["reason"] if notice else "Downloads and scans are paused."
        ),
    )


def render_error_page(request, code, title, msg):
    """Render the app's styled error page from routes or middleware."""
    return _tr(request, "error.html",
               {"code": code, "title": title, "msg": msg}, status_code=code)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render a styled page for a mistyped/stale URL instead of a bare
    ``{"detail": "Not Found"}``. API routes and every non-404 status keep the
    JSON shape callers expect."""
    if exc.status_code == 404 and not request.scope["path"].startswith("/api/"):
        return _tr(request, "error.html", {
            "code": 404,
            "title": "Page not found",
            "msg": "That page doesn't exist. The link may have moved or been "
                   "mistyped.",
        }, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request,
                                        exc: RequestValidationError):
    """A mangled query param (``/library?page=abc``) renders the styled error
    page instead of dumping framework validation JSON into the browser. API
    routes keep the JSON detail machine callers want."""
    if not request.scope["path"].startswith("/api/"):
        return _tr(request, "error.html", {
            "code": 400,
            "title": "Bad request",
            "msg": "That address has an invalid value in it. Check the link "
                   "and try again.",
        }, status_code=400)
    return JSONResponse({"detail": exc.errors()}, status_code=422)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """An uncaught route error renders the styled page for browser paths instead
    of FastAPI's bare JSON 500. API routes keep JSON. The detail is logged, never
    shown, since it can carry internals."""
    import logging
    logging.getLogger("qobuz_librarian").exception(
        "Unhandled error on %s", request.scope.get("path", "?"))
    if not request.scope["path"].startswith("/api/"):
        return _tr(request, "error.html", {
            "code": 500,
            "title": "Something went wrong",
            "msg": "An unexpected error happened on the server. Try again, or "
                   "check the container logs if it keeps happening.",
        }, status_code=500)
    return JSONResponse({"detail": "internal server error"}, status_code=500)


# Serialises the dedupe-check-then-submit in queue_download: the network
# get_album() await between the early check and the submit leaves a window where
# two requests for one album both pass the check and queue it twice.
_DOWNLOAD_SUBMIT_LOCK = threading.Lock()


def _find_job_touching_album(album_id: str, skip_single_track: bool = False):
    """Return a pending/running job that already covers album_id, either as
    its direct subject or as one of its candidates.

    Parked reviews don't count: an album merely listed among a review's
    candidates isn't queued for anything, so refusing an explicit download
    with "already queued" over it would be false, and with a whole-library
    review parked, its candidates are exactly the albums the user is most
    likely to search for. Approve re-checks the disk and drops candidates
    that landed in the meantime, so downloading now can't double up later.

    ``skip_single_track`` ignores one-track downloads, so a full-album
    download doesn't fold onto a job that only downloaded one track."""
    for j in job_mgr.registry.pending_and_running():
        if j.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        if skip_single_track and (getattr(j, "single", None) or {}).get("track_id"):
            continue
        if j.album_id == album_id:
            return j
        # Snapshot: a SCANNING job appends to candidates from the worker thread,
        # and iterating it live can raise "list changed size during iteration".
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
    identical one; a normal full-album download folds onto another full-album job
    (or a scan candidate the user is about to review), but not onto a one-track
    download from the same album."""
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


def _same_edition_is_complete(album: dict) -> bool:
    """Prove that this exact release year is already complete on disk.

    The ordinary album resolver may fall back to a similarly named folder.
    That is useful for gap detection, but it is not enough to refuse a
    deliberate second edition. Require the submitted release year to match
    the resolved folder before comparing its complete track list.
    """
    from qobuz_librarian.library.catalog import (
        _dir_year,
        album_year,
        compute_missing,
        find_album_dir_filesystem,
        find_existing_tracks,
    )

    try:
        folder = find_album_dir_filesystem(album)
        release_year = album_year(album)
        if (
            folder is None
            or not release_year
            or str(_dir_year(folder.name) or "") != str(release_year)
        ):
            return False
        existing, _ = find_existing_tracks(album, album_dir=folder)
        wanted = (album.get("tracks") or {}).get("items") or []
        return bool(existing and wanted) and not compute_missing(
            wanted, existing)[0]
    except Exception:
        return False


def _staging_album_count() -> int:
    """Album folders left in staging by an interrupted import. The CLI warns
    about these at startup (`_check_staging_occupied`); the web has no such
    signal, so a crash mid-import leaves web-only users with no idea files are
    stranded. Only meaningful when nothing is actively writing; the caller
    suppresses the banner while a job is running."""
    try:
        return sum(
            1 for d in cfg.STAGING_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError:
        return 0


@app.head("/")
async def dashboard_head():
    """Uptime monitors / curl -I hit HEAD before GET; serve a body-less 200
    so they don't get a 405 and mark the service down."""
    return Response(status_code=200)


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


def _existing_new_release_check():
    """An active or awaiting-review new-release check, or None, so a second one
    isn't stacked on top of one already queued or waiting for review."""
    for j in job_mgr.registry.pending_and_running():  # ACTIVE incl awaiting_review
        if getattr(j, "execute_kind", "") == "new_releases":
            return j
    return None


def _start_new_release_check(credentials):
    """Submit a whole-library new-release check and return the job (or the one
    already queued). Shared by the manual Library-page option and the automatic
    dashboard trigger."""
    with _auto_check_lock:
        # The run-lock may have been handed to the terminal mid-submit (this
        # can run in an executor for POST /library).
        if _web_writes_paused():
            return None
        existing = _existing_new_release_check()
        if existing is not None:
            return existing
        from qobuz_librarian.web import flows
        job = job_mgr.Job(title="New-release check")
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


def _new_release_review():
    """The awaiting-review new-release check for the dashboard badge, if any."""
    for j in job_mgr.registry.awaiting_review():
        if getattr(j, "execute_kind", "") == "new_releases":
            return {"id": j.id, "count": len(j.candidates)}
    return None


def _maybe_auto_check_new_releases():
    """Quietly run the new-release check on dashboard load when it's due.

    Read-only (it only parks a review list, never downloads), so it's safe to
    fire from a GET. Skipped when the check is off, the token is missing or
    known-bad, the CLI holds the lock, another job is actively working, a
    new-release list is already awaiting review, or the interval hasn't elapsed.
    """
    if cfg.NEW_RELEASE_CHECK_INTERVAL <= 0 or _web_writes_paused():
        return
    # Don't bother (or thrash) when there's no token, or one we already know
    # Qobuz is rejecting; it would just fail on the first call every load.
    if not _qobuz_ready():
        return
    from qobuz_librarian.library import new_releases
    # Only after a full library scan has established the baseline; otherwise the
    # check would crawl every artist just to record a starting point and surface
    # nothing. A completed library scan seeds it (flows.scan_library).
    if not new_releases.is_baseline_complete():
        return
    # And never ahead of an interrupted library scan waiting to resume: finishing
    # that takes priority (it's what the user's resume needs the scan lane for),
    # and a delta check can wait until the library is whole again.
    from qobuz_librarian.library import scan_checkpoint
    if scan_checkpoint.pending() is not None:
        return
    # Avoid a network probe until the interval says a run is due. This first
    # read is repeated under the lock after the probe.
    last = new_releases.last_run()
    if last is not None and (time.time() - last) < cfg.NEW_RELEASE_CHECK_INTERVAL:
        return
    try:
        credentials = _authorize_qobuz_live(QobuzAccess.CATALOGUE_ACTION)
    except (
        NoCredsError,
        AuthLost,
        QobuzUnavailable,
        QobuzEntitlementError,
        CredentialChanged,
    ):
        return
    # Serialise the check-and-submit so two concurrent dashboard loads can't
    # both pass the gate and queue the check twice.
    with _auto_check_lock:
        active = job_mgr.registry.pending_and_running()
        working = any(j.status != job_mgr.JobStatus.AWAITING_REVIEW for j in active)
        pending_check = any(getattr(j, "execute_kind", "") == "new_releases"
                            for j in active)
        if working or pending_check:
            return
        last = new_releases.last_run()
        if last is not None and (time.time() - last) < cfg.NEW_RELEASE_CHECK_INTERVAL:
            return
        # Stamp the attempt before submitting: the scan only advances the stamp
        # on a clean finish, so without this a failed/cancelled run would re-fire
        # on every load.
        new_releases.touch_run()
        _start_new_release_check(credentials)


_ANY_TARGET = object()


def _scan_target(job) -> str:
    """The slice of the library a scan covers: a single artist (the per-artist
    routes set ``job.artist``) or "" for a whole-library sweep. Dedup compares on
    this so re-scanning one artist folds onto / supersedes only that artist's own
    in-flight scan or parked review, never a different artist's, and never the
    whole-library pass. Case/whitespace-folded so "Bonobo" re-scans "bonobo"."""
    return (getattr(job, "artist", "") or "").strip().casefold()


def _active_scan(*kinds, statuses=("pending", "scanning"), target=_ANY_TARGET):
    """A job of one of the given execute_kinds in one of ``statuses``, or None,
    folding a double-submitted pass onto the one already in flight instead of
    stacking duplicate work. Defaults to the scan phase: a scan keeps its
    execute_kind through the post-review download (which runs as ``running``),
    so matching only pending/scanning lets a deliberate re-scan still queue
    behind a batch that's downloading. Run-to-completion jobs with no review
    (lyrics) pass their own running phase instead. ``target`` restricts the match
    to one artist's scan (or the whole-library pass); the default matches any."""
    for j in job_mgr.registry.pending_and_running():
        if getattr(j, "execute_kind", "") in kinds and j.status.value in statuses:
            if target is _ANY_TARGET or _scan_target(j) == target:
                return j
    return None


def _queue_wait(job):
    """Describe what a PENDING job is waiting behind on its worker lane, so the
    UI can explain the wait instead of showing a bare "Queued". Scans share one
    worker and downloads another (see web/jobs.py), so a job only waits behind
    others in its OWN lane (job.kind: "scan" | "download"). ``position`` counts
    how many run before it (the one holding the worker + any earlier-queued).
    Returns {"ahead_title", "lane", "position"} or None when nothing's ahead,
    i.e. it's about to start, so there's nothing to explain."""
    if job.status != job_mgr.JobStatus.PENDING:
        return None
    holder = None
    ahead = 0
    for j in job_mgr.registry.all():
        if j.id == job.id or j.kind != job.kind:
            continue
        if j.status in (job_mgr.JobStatus.SCANNING, job_mgr.JobStatus.RUNNING):
            holder = j  # the job actually occupying this lane's worker right now
        elif (j.status == job_mgr.JobStatus.PENDING
              and (j.created_at or 0) < (job.created_at or 0)):
            ahead += 1
    if holder is None and ahead == 0:
        return None
    return {
        "ahead_title": holder.title if holder else "",
        "lane": job.kind,
        "position": ahead + (1 if holder else 0),
    }


def _repair_current_job():
    """The repair job that owns the /repair surface right now: the most recent
    repair job still pending / scanning / awaiting-review / running. None means
    the surface is idle (show the start-or-resume form). This is what lets
    /repair stay the single authoritative repair page across every phase instead
    of handing a parked review off to /jobs/{id}, and it's why a review is never
    hidden behind a "Start scan" button that would silently discard it."""
    states = (job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
              job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.RUNNING,
              # A run that failed or was cancelled is part of the phase set too.
              # Dropping it sent the page back to its idle launcher on the next
              # reload, where the freshness line, which counts only clean
              # passes, then reported a scan from weeks earlier.
              job_mgr.JobStatus.FAILED, job_mgr.JobStatus.CANCELED)
    cur = None
    for j in job_mgr.registry.all():
        if getattr(j, "execute_kind", "") != "repair" or j.status not in states:
            continue
        if cur is None or (j.created_at or 0) >= (cur.created_at or 0):
            cur = j
    return cur


# Library follows the same single-surface rule as Repair: the scan, its live
# progress, and the parked Missing Albums / Gap Fill review all live on
# /library, never handed off to /jobs/{id} under the Queue nav.
_LIBRARY_SURFACE_KINDS = ("library", "new_releases")
_QOBUZ_REVIEW_KINDS = ("library", "new_releases", "upgrade", "repair")
_PREMISE_REVIEW_KINDS = _QOBUZ_REVIEW_KINDS + ("downsample",)


def _library_current_job():
    """The baseline scan that owns the /library surface right now (still
    pending / scanning / awaiting-review / running), or None when the surface
    is idle and shows the launcher. New-release checks never own it; their
    results live on their own job page, so the
    Missing Albums / Gap Fill review can't be displaced by an overnight
    check. A parked review outranks running work: after a tab-scoped download
    splits the review, the user stays on the tab still waiting for them while
    the download runs in the queue."""
    states = (job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
              job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.RUNNING)
    cur = None
    for j in job_mgr.registry.all():
        if (getattr(j, "execute_kind", "") != "library"
                or j.status not in states):
            continue
        if cur is None:
            cur = j
            continue
        j_rev = j.status == job_mgr.JobStatus.AWAITING_REVIEW
        cur_rev = cur.status == job_mgr.JobStatus.AWAITING_REVIEW
        if (j_rev, (j.created_at or 0)) >= (cur_rev, (cur.created_at or 0)):
            cur = j
    return cur


async def _submit_scan_deduped_async(job, scan_fn, execute_fn, *kinds, **kw):
    """Run _submit_scan_deduped off the event loop.

    It takes _auto_check_lock, which dashboard executor threads can hold across
    small (possibly NAS-backed) reads, so the loop must not block on it; the
    same reason POST /library offloads its submit. Every async scan route goes
    through this instead of calling _submit_scan_deduped directly on the loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _submit_scan_deduped(job, scan_fn, execute_fn, *kinds, **kw))


def _submit_scan_deduped(job, scan_fn, execute_fn, *kinds, statuses=("pending", "scanning")):
    """Submit a scan only if one of ``kinds`` isn't already active, atomically.

    Checking _active_scan and submitting in one locked step closes the window
    where two near-simultaneous POSTs (a double-click, or the auto-trigger
    landing with a manual click) both pass the check and stack duplicate scans.
    Returns the job to redirect to: the new one, or the in-flight duplicate,
    or None when web writes were paused between the route's opening gate and
    here (a set_mode CLI handoff landing mid-request; see job_approve)."""
    with _auto_check_lock:
        if _web_writes_paused():
            return None
        target = _scan_target(job)
        existing = _active_scan(*kinds, statuses=statuses, target=target)
        if existing is not None:
            return existing
        # A re-scan supersedes the same artist's stale parked review (or the
        # whole-library pass's) instead of stacking a second one: the fresh
        # scan re-derives that target's candidates, so the old awaiting-review
        # result is obsolete, and parked reviews never self-clear so without
        # this they pile up forever.
        stale_reviews = [
            old for old in job_mgr.registry.awaiting_review()
            if (getattr(old, "execute_kind", "") in kinds
                and _scan_target(old) == target)
        ]
        submitted = job_mgr.submit_scan(job, scan_fn, execute_fn)
        if submitted is None:
            return None
        # Only discard the prior review after the replacement has a durable
        # owner row.
        for old in stale_reviews:
            job_mgr.cancel_review(old)
        return submitted


def _scan_submission_failure_response(request, destination):
    """Explain a refused durable admission unless the CLI owns the lock."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    separator = "&" if "?" in destination else "?"
    return RedirectResponse(
        url=destination + separator + "error=" + urllib.parse.quote(
            job_mgr.JOB_ADMISSION_ERROR
        ),
        status_code=303,
    )


def _active_library_scan():
    """A library scan that's already pending/crawling, or None."""
    return _active_scan("library")


def _library_scan_state():
    """Whether a whole-library scan has something valid to scan."""
    root = Path(cfg.MUSIC_ROOT)
    if not root.exists():
        return {
            "ready": False,
            "count": 0,
            "message": (
                f"{root} does not exist. Choose the location that contains your artist folders."
            ),
        }
    if not root.is_dir():
        return {
            "ready": False,
            "count": 0,
            "message": (
                f"{root} is not a folder. Choose the location that contains your artist folders."
            ),
        }
    from qobuz_librarian.library.scanner import list_library_artists
    artists = list_library_artists()
    if not artists:
        return {
            "ready": False,
            "count": 0,
            "message": (
                f"No artist folders with audio were found in {root}. Choose the location that contains your artist folders."
            ),
        }
    return {"ready": True, "count": len(artists), "message": ""}


def _truthful_library_generation():
    """Expose an interrupted publication honestly even without write authority."""
    from qobuz_librarian.library import generation_state

    state = generation_state.load()
    if not generation_state.library_publication_incomplete(state):
        return state
    state = copy.deepcopy(state)
    latest = state.setdefault("latest_attempt", {})
    latest["status"] = "incomplete"
    latest["message"] = (
        "The completed catalogue crawl was interrupted before its Library "
        "view was published."
    )
    return state


def _start_library_scan(credentials, partial_only=False, force_full=False):
    """Submit a library scan and return the job. Shared by the Library page and
    the automatic first-run/resume trigger. scan_library resumes from a matching
    checkpoint on its own, so this is the same call whether starting or resuming.

    Deduped under the lock: if a library scan is already crawling, return it
    instead of stacking a second one (the manual button and the auto trigger can
    both land here at once)."""
    with _auto_check_lock:
        # Re-check the pause predicate under the lock (see
        # _start_new_release_check).
        if _web_writes_paused():
            return None
        existing = _active_library_scan()
        if existing is not None:
            return existing
        from qobuz_librarian.web import flows
        title = "Gap Fill scan" if partial_only else "Library scan"
        job = job_mgr.Job(title=title)
        job.execute_kind = "library"

        def _scan(j):
            active = _authorize_qobuz_live(
                QobuzAccess.CATALOGUE_ACTION,
                expected_generation=credentials.generation,
            )
            flows.scan_library(j, active.token, partial_only=partial_only,
                               force_full=force_full)
            _fold_into_parked_library_review(j)

        return job_mgr.submit_scan(
            job,
            _scan,
            _resume_album_download(job, job.execute_args),
        )


def _fold_into_parked_library_review(job):
    """A refresh that finishes while a Missing Albums / Gap Fill review is
    parked folds its finds into that review instead of parking a second one,
    the Library review is one living thing that updates in place, and a
    refresh must never wipe the picks already made there. The scan job then
    completes with a summary, so Queue/History records the refresh without
    ever becoming a second review surface."""
    if (job.status not in (job_mgr.JobStatus.SCANNING, job_mgr.JobStatus.RUNNING)
            or job.cancel_requested):
        return
    parked = None
    for other in job_mgr.registry.awaiting_review():
        if (getattr(other, "execute_kind", "") != "library"
                or other.id == job.id):
            continue
        if parked is None or (other.created_at or 0) > (parked.created_at or 0):
            parked = other
    if parked is None:
        return
    from qobuz_librarian.web import flows, review_badges
    with job._lock:
        cands = list(job.candidates)
    folded = flows.fold_new_candidates(
        parked,
        cands,
        review_generation=(job.execute_args or {}).get(
            "_library_review_generation"
        ),
    )
    if folded is False:
        job.status = job_mgr.JobStatus.FAILED
        job.error = (
            "The refreshed Library review couldn't be saved to the data "
            "folder. Its existing picks are untouched. Check the data "
            "volume, then refresh again."
        )
        job.summary = "Library refresh stopped because its results could not be saved."
        job.push_line(job.error)
        return
    if folded is None:
        # The review was approved or discarded while the refresh ran, so leave
        # the scan's candidates alone so they park as their own review.
        return
    # Use the counts from the locked mutation itself. Complete ownership is
    # reconciled at approval, when exact edition tracks can be compared safely.
    added, updated = folded
    # Open review pages re-fetch on this nudge; without it the fold is
    # invisible until a manual reload (and "Refreshing…" never resolves).
    parked.notify_review_changed()
    if added or updated:
        # Fresh reviewable results landed in the parked review, so light the
        # Library dot again until the user opens it.
        review_badges.mark_ready("library")
    with job._lock:
        job.candidates = []
    bits = []
    if added:
        bits.append(f"Folded {added} new find{'s' if added != 1 else ''} "
                    "into the open Library review.")
    if updated:
        bits.append(
            f"Updated {updated} changed item{'s' if updated != 1 else ''} "
            "in the open Library review."
        )
    unchecked = getattr(job, "_unchecked_artists", 0)
    if not bits:
        if unchecked:
            bits.append("No new finds from the artists that could be checked.")
        else:
            bits.append("No new finds. The open Library review is up to date.")
    if unchecked:
        bits.append(f"{unchecked} artist{'s' if unchecked != 1 else ''} "
                    "couldn't be checked; scan again to resume from where it "
                    "left off.")
    if job.candidate_cap_hit or parked.candidate_cap_hit:
        bits.append("The scan hit the result cap, so some finds may not be "
                    "listed.")
    job.summary = " ".join(bits)
    job.push_line(job.summary)
    job.status = job_mgr.JobStatus.DONE


def _maybe_resume_library_scan():
    """Resume an interrupted library scan when the app is idle, driving it to
    completion across restarts.

    A FRESH first scan is NOT auto-started; the dashboard offers it as a choice
    (see ``offer_baseline``) so a brand-new user isn't hit with a long,
    network-heavy job unprompted. Once they start one and it gets interrupted, it
    leaves a checkpoint and resumes from here. Off entirely via AUTO_LIBRARY_SCAN.
    """
    if not cfg.AUTO_LIBRARY_SCAN or _web_writes_paused():
        return
    if not _qobuz_ready():
        return
    from qobuz_librarian.library import generation_state, scan_checkpoint
    if generation_state.baseline_complete():
        return
    try:
        credentials = _authorize_qobuz_live(QobuzAccess.CATALOGUE_ACTION)
    except (
        NoCredsError,
        AuthLost,
        QobuzUnavailable,
        QobuzEntitlementError,
        CredentialChanged,
    ):
        return
    with _auto_check_lock:
        if any(j.status != job_mgr.JobStatus.AWAITING_REVIEW
               for j in job_mgr.registry.pending_and_running()):
            return  # something already working
        cp = scan_checkpoint.pending()
        if cp is not None:
            _start_library_scan(
                credentials,
                partial_only=(cp["kind"] == "partial"),
            )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = "", kind: str = "artist",
                    artist_id: str = "", artist_name: str = ""):
    from qobuz_librarian import config as _cfg
    active_jobs = [j for j in job_mgr.registry.pending_and_running()
                   if j.status.value in ('running', 'scanning')]

    # These all read the (often NAS / network-mounted) data + music volumes,
    # the fetch log, the creds file, the lyric-retry file, and a staging
    # iterdir().
    def _gather_disk_state():
        from qobuz_librarian.integrations.lyrics import load_lyric_retry
        from qobuz_librarian.library import generation_state, new_releases, scan_checkpoint
        # These read state files and may submit a background job, so they run
        # here (off the event loop) alongside the other disk work.
        _maybe_resume_library_scan()
        _maybe_auto_check_new_releases()
        library_scan_state = _library_scan_state()
        library_generation = _truthful_library_generation()
        return {
            "new_release_review": _new_release_review(),
            # First run offers the baseline scan as a Run/Skip choice rather than
            # auto-starting it; suppress the offer once the user skips it (the
            # dismiss marker) or turns it off via AUTO_LIBRARY_SCAN.
            "offer_baseline": (cfg.AUTO_LIBRARY_SCAN
                               and not new_releases.auto_scan_attempted()),
            # First-run setup banner: shown until a full library scan has
            # seeded the new-release baseline.
            "baseline_complete": generation_state.baseline_complete(),
            "setup_scanning": _active_library_scan() is not None,
            "library_scan_state": library_scan_state,
            # An interrupted gap-scan, surfaced on the dashboard the way
            # /library already does, gated on no scan running.
            "library_resume": (
                lambda cp, generation: (
                    cp
                    if (
                        cp is not None
                        and _active_library_scan() is None
                        and (
                            not int(generation.get("generation") or 0)
                            or str(
                                (generation.get("latest_attempt") or {}).get(
                                    "status"
                                )
                            ) in {"running", "failed", "incomplete"}
                        )
                    )
                    else None
                )
            )(scan_checkpoint.pending(), library_generation),
            # First-run nudge: a fresh install has no creds, so every search/scan
            # would fail cryptically, so surface it up front. Filesystem-only.
            "creds_ok": bool(_read_creds().get("auth_token")),
            "qobuz_ready": _qobuz_ready(),
            "lyric_retry_count":
                len(load_lyric_retry()) if _cfg.LYRIC_RETRY_FILE.exists() else 0,
            "staging_album_count": 0 if active_jobs else _staging_album_count(),
            # A store that couldn't be read was kept aside and the run fell back
            # to defaults, and only the container log said so, which nobody reads.
            "corrupt_stores": state_file.preserved_corrupt_stores(),
            # Says the pause here rather than leaving it to the 503 a press
            # earns. It probes the volumes, so it belongs off the event loop.
            "writes_paused_notice": _writes_paused_notice(),
        }

    loop = asyncio.get_running_loop()
    disk = await loop.run_in_executor(None, _gather_disk_state)
    search_kind = str(kind or "").strip().lower()
    if search_kind not in ("artist", "album", "track"):
        search_kind = "artist"
    search_q = str(q or "").strip()[:200]
    search_artist_id = str(artist_id or "").strip()[:64]
    search_artist_name = str(artist_name or "").strip()[:200]
    initial_search_results = ""
    if search_kind == "artist" and search_q and not search_artist_id:
        initial_search_results = await _initial_artist_search_html(request, search_q)
    return _tr(request, "index.html", {
        "active_jobs": active_jobs,
        "pending": job_mgr.registry.pending_and_running(),
        "review": job_mgr.registry.awaiting_review(),
        "creds_token_valid": _token_valid_for(),
        "search_q": search_q,
        "search_kind": search_kind,
        "search_artist_id": search_artist_id,
        "search_artist_name": search_artist_name,
        # Album/track deep links can't be pre-rendered the way artist ones are
        # (their pipeline lives in POST /search), so the form submits itself on
        # load instead of sitting prefilled and inert. An artist's album list is
        # the same case: it needs the artist id, which only that pipeline reads.
        "auto_search": bool(search_q) and (
            search_kind in ("album", "track") or bool(search_artist_id)),
        "initial_search_results": initial_search_results,
        "page": "dashboard",
        **disk,
    })


@app.post("/lyric-retry")
async def lyric_retry(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    # A retry and a full backfill share the one lyric-state file, so they must
    # never run at once, so fold onto whichever lyrics pass is already in flight.
    existing = _active_scan("lyrics", statuses=("pending", "running"))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyric retry")
    job.execute_kind = "lyrics"
    if job_mgr.submit(job, lambda j: flows.run_lyric_retry(j)) is None:
        return _job_admission_response(request)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


def _qobuz_quality_bits_rate(primary: dict | None,
                             fallback: dict | None = None) -> tuple[int, int]:
    """Return Qobuz source quality as (bits, sample_rate_hz)."""
    primary = primary or {}
    fallback = fallback or {}
    bits = primary.get("maximum_bit_depth") or fallback.get("maximum_bit_depth") or 0
    rate = (primary.get("maximum_sampling_rate")
            or fallback.get("maximum_sampling_rate") or 0)
    try:
        bits_i = int(bits)
    except (TypeError, ValueError):
        bits_i = 0
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        rate_f = 0.0
    if 0 < rate_f < 1000:
        rate_f *= 1000
    return bits_i, int(round(rate_f))


def _qobuz_quality_short_label(primary: dict | None,
                               fallback: dict | None = None) -> str:
    bits, rate = _qobuz_quality_bits_rate(primary, fallback)
    if not bits or not rate:
        return ""
    from qobuz_librarian.quality.tiers import format_quality
    return format_quality(bits, rate)


@app.post("/search", response_class=HTMLResponse)
async def do_search(request: Request, q: str = Form("", max_length=500),
                    kind: str = Form("album"),
                    artist_id: str = Form(""),
                    artist_name: str = Form("")):
    results = []
    album_groups = []
    artist_results = []
    selected_artist = None
    error = None
    query = q.strip()
    kind_raw = str(kind).strip().lower()
    kind = kind_raw if kind_raw in ("artist", "track") else "album"
    artist_id = str(artist_id or "").strip()
    artist_name = str(artist_name or "").strip()
    if not _is_htmx(request):
        return RedirectResponse(url="/", status_code=303)
    if query:
        # Imported before the try so the except clauses below can always name
        # them, even if a failure happens before the request reaches the API.
        from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
        try:
            token = _get_token()
            from qobuz_librarian.api.search import (
                get_album,
                get_artist_albums,
                get_track,
                search_albums,
                search_artists,
                search_tracks,
            )
            from qobuz_librarian.cli import parse_qobuz_url
            from qobuz_librarian.library.catalog import (
                album_year,
                find_album_dir_filesystem,
            )

            # If the user pasted a Qobuz URL, the placeholder says we
            # handle it, so actually do so by fetching the album directly
            # instead of doing a text search on the URL string.
            try:
                _split = urllib.parse.urlsplit(query)
                netloc = _split.netloc.lower()
                is_qobuz_url = (_split.scheme in ("http", "https")
                                and (netloc == "qobuz.com"
                                     or netloc.endswith(".qobuz.com")))
            except ValueError:
                is_qobuz_url = False
            parsed = parse_qobuz_url(query) if is_qobuz_url else None
            raw = []
            loop = asyncio.get_running_loop()
            from qobuz_librarian.api.client import call_within
            if parsed and parsed[0] == "album" and kind == "track":
                # An album URL only resolves in Album mode; in Track mode it
                # would fetch the album and then be dropped as not-a-track,
                # leaving a blank "No results". Point the user at the toggle.
                error = ("That's an album URL. Switch to Album to download it, "
                         "or paste a single track to download one track.")
            elif parsed and parsed[0] == "album" and kind == "artist":
                error = "That's an album URL. Switch to Album to download it."
            elif parsed and parsed[0] == "album":
                try:
                    raw = [await asyncio.wait_for(
                        loop.run_in_executor(
                            None, lambda: call_within(
                                cfg.WEB_FETCH_TIMEOUT, get_album, parsed[1], token)
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )]
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
                except (AuthLost, QobuzUnavailable):
                    raise
                except QobuzError:
                    error = "Couldn't fetch that album. Check the URL."
                except Exception:
                    import logging
                    logging.getLogger("qobuz_librarian").exception(
                        "album fetch failed for %r", query)
                    error = "Couldn't fetch that album. Check the URL."
            elif parsed and parsed[0] == "track" and kind == "track":
                # Tracks mode: resolve the pasted track URL to that one track;
                # the track-results loop below renders it for a one-track download.
                try:
                    _t = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: call_within(
                            cfg.WEB_FETCH_TIMEOUT, get_track, parsed[1], token)),
                        timeout=cfg.WEB_FETCH_TIMEOUT)
                    raw = [_t] if _t else []
                    if not raw:
                        error = "Couldn't fetch that track. Check the URL."
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
                except (AuthLost, QobuzUnavailable):
                    raise
                except QobuzError:
                    error = "Couldn't fetch that track. Check the URL."
            elif parsed and parsed[0] == "track":
                # Album mode: a track URL -- point the user at the Track toggle
                # instead of the old (now false) "works on albums" message.
                error = ("That's a track URL. Switch to Track to download one "
                         "track, or paste the album URL in Album mode.")
            elif parsed:
                # Parsed as some other Qobuz URL kind (artist/playlist).
                if kind == "artist":
                    error = "Search artists by name. Paste album or track URLs only."
                else:
                    error = ("Only Qobuz album and track URLs are supported. "
                             "Search for an artist by name instead.")
            elif is_qobuz_url:
                # URL looks like qobuz.com but isn't a recognised format (e.g.
                # artist/interpreter or playlist page).
                if kind == "artist":
                    error = "Search artists by name. Paste album or track URLs only."
                else:
                    error = ("Only Qobuz album and track URLs are supported. "
                             "Search for an artist by name instead.")
            elif kind == "artist" and artist_id:
                try:
                    raw, artist_total = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                get_artist_albums,
                                artist_id,
                                token,
                                limit=cfg.ARTIST_CATALOG_LIMIT,
                            ),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                    selected_artist = {
                        "id": artist_id,
                        "name": artist_name or query,
                        "total": artist_total,
                        "shown": len(raw),
                    }
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
            elif kind == "artist":
                try:
                    artist_raw = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                search_artists,
                                query,
                                token,
                                limit=cfg.ARTIST_LOOKUP_LIMIT,
                            ),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                    for a in artist_raw:
                        if not a.get("id"):
                            continue
                        img = a.get("image") or {}
                        cover = ""
                        if isinstance(img, dict):
                            cover = img.get("small") or img.get("thumbnail") or ""
                        artist_results.append({
                            "id": a.get("id"),
                            "name": a.get("name") or "?",
                            "cover": cover if str(cover).startswith(
                                "https://static.qobuz.com/") else "",
                        })
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
            else:
                _search_fn = search_tracks if kind == "track" else search_albums
                try:
                    raw = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: call_within(cfg.WEB_FETCH_TIMEOUT, _search_fn,
                                                query, token, limit=cfg.SEARCH_LIMIT),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."

            _track_raws = []
            for t in (raw if kind == "track" else []):
                alb = t.get("album") or {}
                if not t.get("id") or not alb.get("id"):
                    continue
                _tbd, _tsr = _qobuz_quality_bits_rate(t, alb)
                _timg = alb.get("image") or {}
                _tcover = _timg.get("small") or _timg.get("thumbnail") or ""
                _perf = (t.get("performer") or {}).get("name")
                results.append({
                    "track_id":    t.get("id"),
                    "album_id":    alb.get("id"),
                    "title":       t.get("title") or "?",
                    "version":     t.get("version") or alb.get("version") or "",
                    "artist":      (alb.get("artist") or {}).get("name") or _perf or "?",
                    "album_title": alb.get("title") or "?",
                    "year":        album_year(alb) or "?",
                    "track_n":     t.get("track_number") or "?",
                    "total":       alb.get("tracks_count") or "?",
                    "quality":     _qobuz_quality_short_label(t, alb),
                    "hires":       _tbd >= 24,
                    "lossy":       _tbd == 0,
                    "bit_depth":   _tbd,
                    "sample_rate": _tsr,
                    "cover":       _tcover if _tcover.startswith(
                        "https://static.qobuz.com/") else "",
                    "owned":       False,
                })
                _track_raws.append(t)

            if _track_raws:
                def _annotate_owned_tracks():
                    from qobuz_librarian.library.catalog import (
                        compute_missing,
                        find_existing_tracks,
                    )

                    albums = {}
                    for res, track in zip(results, _track_raws):
                        album = track.get("album") or {}
                        album_id = str(album.get("id") or "")
                        group = albums.setdefault(
                            album_id, {"album": album, "results": []})
                        group["results"].append(res)

                    for album_id, group in albums.items():
                        try:
                            folder = find_album_dir_filesystem(group["album"])
                            if folder is None:
                                continue
                            exact_album = call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                get_album,
                                album_id,
                                token,
                            )
                            qobuz_tracks = (
                                (exact_album.get("tracks") or {}).get("items")
                                or []
                            )
                            if not qobuz_tracks:
                                continue
                            existing, _ = find_existing_tracks(
                                exact_album, album_dir=folder)
                            if not existing:
                                continue
                            _missing, present = compute_missing(
                                qobuz_tracks, existing)
                            present_ids = {
                                str(track.get("id"))
                                for track in present if track.get("id")
                            }
                            for res in group["results"]:
                                res["owned"] = str(res["track_id"]) in present_ids
                        except Exception:
                            pass

                try:
                    _own_timeout = 20
                    await asyncio.wait_for(
                        loop.run_in_executor(None, _annotate_owned_tracks),
                        timeout=_own_timeout)
                except asyncio.TimeoutError:
                    import logging
                    logging.getLogger("qobuz_librarian").warning(
                        "track ownership annotation timed out (%ss) for %r; "
                        "results shown without In-library marks",
                        _own_timeout,
                        query,
                    )
                except Exception:
                    import logging
                    logging.getLogger("qobuz_librarian").exception(
                        "track ownership annotation failed for %r", query)
            _album_raws = []
            for a in (raw if kind == "album" or selected_artist else []):
                if not a.get("id"):
                    continue
                _bd, _sr = _qobuz_quality_bits_rate(a)
                _img = a.get("image") or {}
                _cover = _img.get("small") or _img.get("thumbnail") or ""
                _qual = _qobuz_quality_short_label(a)
                results.append({
                    "id":      a.get("id"),
                    "title":   a.get("title") or "?",
                    "artist":  (a.get("artist") or {}).get("name") or "?",
                    "year":    album_year(a) or "?",
                    "tracks":  a.get("tracks_count") or "?",
                    "quality": _qual,
                    "hires":   _bd >= 24,
                    "lossy":   _bd == 0,
                    "bit_depth": _bd,
                    "sample_rate": _sr,
                    "cover":   _cover if _cover.startswith(
                        "https://static.qobuz.com/") else "",
                    "owned":   False,
                })
                _album_raws.append(a)

            # Flag results already in the library so search never offers a
            # plain Download on an album you own; the app is gap-fill, so
            # that would contradict its own purpose.
            if _album_raws:
                def _annotate_owned():
                    # Same filesystem resolver the download and scan paths use.
                    # "Owned" means COMPLETE, not "a folder with a file in it":
                    # a part-finished album reading "In library" loses both its
                    # checkbox and its download button, which is the gap-fill
                    # case this app exists for.
                    from qobuz_librarian.library.catalog import (
                        _dir_year,
                        compute_missing,
                        find_existing_tracks,
                    )
                    for res, alb in zip(results, _album_raws):
                        try:
                            folder = find_album_dir_filesystem(alb)
                            if folder is None:
                                continue
                            exact_album = call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                get_album,
                                alb["id"],
                                token,
                            )
                            qobuz_tracks = (
                                (exact_album.get("tracks") or {}).get("items")
                                or []
                            )
                            if not qobuz_tracks:
                                continue
                            existing, _ = find_existing_tracks(
                                exact_album, album_dir=folder)
                            if not existing:
                                continue
                            missing, present = compute_missing(
                                qobuz_tracks, existing)
                            res["disk_year"] = _dir_year(folder.name)
                            if missing:
                                res["partial"] = True
                                res["have_tracks"] = len(present)
                                res["want_tracks"] = len(qobuz_tracks)
                            else:
                                res["owned"] = True
                        except Exception:
                            pass
                try:
                    # Exact ownership may need the selected edition's track
                    # list after the cheap folder check. Keep the annotation
                    # bounded; search results are still useful without it.
                    _own_timeout = 20
                    await asyncio.wait_for(
                        loop.run_in_executor(None, _annotate_owned),
                        timeout=_own_timeout)
                except asyncio.TimeoutError:
                    import logging
                    logging.getLogger("qobuz_librarian").warning(
                        "ownership annotation timed out (%ss) for %r; results "
                        "shown without In-library marks", _own_timeout, query)
                except Exception:
                    import logging
                    logging.getLogger("qobuz_librarian").exception(
                        "ownership annotation failed for %r", query)

            # Collapse the flat result list into one row per album: a
            # remaster, deluxe, and box set of the same record group together
            # with the alternates tucked under the main row, instead of the
            # same album scattering down the page.
            if _album_raws:
                from qobuz_librarian.library.hidden import album_fingerprint
                from qobuz_librarian.library.tags import strip_leading_article
                by_key = {}
                for res, alb in zip(results, _album_raws):
                    ver = alb.get("version") or ""
                    identity_title = res["title"]
                    if ver:
                        identity_title += f" ({ver})"
                    fingerprint = album_fingerprint(
                        res["artist"], strip_leading_article(identity_title)
                    )
                    # Unknown identity must fail open into its own result.
                    # Sharing an empty fuzzy key hides unrelated releases.
                    key = (("album", fingerprint) if fingerprint else
                           ("release", str(res["id"])))
                    g = by_key.get(key)
                    if g is None:
                        g = dict(res, editions=[])
                        by_key[key] = g
                        album_groups.append(g)
                    # A complete edition outranks a part-finished one: if any
                    # edition of this record is whole on disk, search must not
                    # offer a plain Download for the record at all.
                    g["owned"] = g["owned"] or res["owned"]
                    if res.get("disk_year"):
                        g["disk_year"] = res["disk_year"]
                    # Each edition keeps its OWN title and its own ownership
                    # verdict. Sharing the group's title made a plain pressing
                    # render as the deluxe it was grouped under, right down to
                    # the download confirmation naming a record you had not
                    # picked; sharing one verdict put a count from one pressing
                    # beside the track total of another.
                    g["editions"].append({
                        "id": res["id"],
                        "title": res["title"],
                        "artist": res["artist"],
                        "version": (alb.get("version") or "").strip(),
                        "year": res["year"], "tracks": res["tracks"],
                        "quality": res["quality"], "hires": res["hires"],
                        "lossy": res["lossy"], "bit_depth": res["bit_depth"],
                        "sample_rate": res["sample_rate"],
                        "cover": res["cover"],
                        "owned": bool(res["owned"]),
                        "partial": bool(res.get("partial")),
                        "have_tracks": res.get("have_tracks"),
                        "want_tracks": res.get("want_tracks"),
                    })
                for g in album_groups:
                    eds = g["editions"]
                    # The row shows exactly one edition, so it has to be one the
                    # ownership check actually ran against: a complete copy
                    # first (that is the one you own, and the rest read as
                    # "other versions"), then a part-finished one, so the
                    # "N of M" beside it counts the same pressing the Download
                    # button would fetch.
                    rep_i = 0
                    owned = [i for i, e in enumerate(eds) if e["owned"]]
                    part = [i for i, e in enumerate(eds) if e["partial"]]
                    if owned:
                        rep_i = owned[0]
                        if g.get("disk_year"):
                            for i in owned:
                                if str(eds[i]["year"]) == str(g["disk_year"]):
                                    rep_i = i
                                    break
                    elif part:
                        rep_i = part[0]
                    if rep_i:
                        eds.insert(0, eds.pop(rep_i))
                    rep = eds[0]
                    for f in ("id", "title", "artist", "year", "tracks",
                              "quality", "hires", "lossy", "bit_depth",
                              "sample_rate", "cover", "version"):
                        g[f] = rep[f]
                    g["partial"] = rep["partial"] and not g["owned"]
                    g["have_tracks"] = rep["have_tracks"]
                    g["want_tracks"] = rep["want_tracks"]
                    g["others"] = eds[1:]
        except (SystemExit, NoCredsError):
            error = "No Qobuz credentials set. Visit Settings."
        except AuthLost:
            error = "Token is expired or invalid. Update it in Settings."
        except QobuzUnavailable:
            error = ("Qobuz is temporarily unavailable (network or rate "
                     "limit). Try again shortly.")
        except QobuzError:
            error = "Search failed. Try again."
        except Exception:
            import logging
            logging.getLogger("qobuz_librarian").exception(
                "search failed for %r", query)
            error = "Search failed. Try again."
    creds_ok = bool(_read_creds().get("auth_token"))
    ctx = {"q": query, "results": results, "album_groups": album_groups,
           "artist_results": artist_results, "selected_artist": selected_artist,
           "error": error, "kind": kind,
           "creds_ok": creds_ok, "qobuz_ready": _qobuz_ready(), "page": "search"}
    if _is_htmx(request):
        resp = _tr(request, "_search_results.html", ctx)
        # Put the search in the address bar. Without it a reload, or Back after
        # a look at the Queue, landed on the empty state with the query, the
        # album list and every tick gone. GET / rehydrates from these.
        if query:
            params = {"kind": kind, "q": query}
            if artist_id:
                params["artist_id"] = artist_id
                if artist_name:
                    params["artist_name"] = artist_name
            resp.headers["HX-Push-Url"] = "/?" + urllib.parse.urlencode(params)
        return resp
    return RedirectResponse(url="/", status_code=303)


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
    "upgrade_aborted_backup_failed": "Upgrade aborted: couldn't back up the original.",
    "not_imported": "Downloaded, but the import didn't land. Library unchanged.",
}


def _summarize_download_result(r):
    """One-line job summary from process_album's result dict.

    Picks a phrase per result kind for the documented non-success branches,
    or builds the "N tracks downloaded" tally for an actual rip. Returns
    "" if there's nothing useful to say (process_album returned None / {})."""
    from qobuz_librarian.download_result import incomplete_track_counts
    from qobuz_librarian.ui_cli.errors import plural

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
            retryable, lossy_only = incomplete_track_counts(r)
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


def _mark_download_attention(job, result):
    """Mark a job failed when download details still need attention."""
    from qobuz_librarian.download_result import (
        download_attention_kind,
        incomplete_track_counts,
    )
    from qobuz_librarian.ui_cli.errors import plural

    retryable, lossy_only = incomplete_track_counts(result)
    kind = download_attention_kind(result)
    job.status = job_mgr.JobStatus.FAILED
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
    from qobuz_librarian.api.auth import token_credential_generation

    expected_generation = token_credential_generation(token)

    def run(j):
        from qobuz_librarian.download_result import (
            download_attention_kind,
            incomplete_track_counts,
        )
        from qobuz_librarian.library.catalog import (
            compute_missing,
            find_existing_tracks,
            is_lossless_album,
        )
        from qobuz_librarian.modes.process import process_album
        from qobuz_librarian.queue.builder import _build_queue_item
        from qobuz_librarian.queue.durable_album import plan_durable_new_album
        from qobuz_librarian.queue.executor import _execute_download_queue
        from qobuz_librarian.ui_cli.errors import plural
        from qobuz_librarian.web.flows import (
            _note_staging_wait,
            _refresh_after_local_album_change,
            build_args,
        )
        active = None
        active_token = token
        if getattr(token, "credential_generation", ""):
            active = _authorize_qobuz_live(
                QobuzAccess.DOWNLOAD_ACTION,
                expected_generation=expected_generation,
            )
            active_token = active.token
        args = build_args()
        _note_staging_wait(j, "Downloading", 0, 1)
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
                from qobuz_librarian.queue import journal as queue_state

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
            elif not treat_as_new and is_lossless_album(album):
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                existing, album_dir = find_existing_tracks(album)
                missing, present = compute_missing(qobuz_tracks, existing)
                candidate = _build_queue_item(
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
                if plan_durable_new_album(candidate, args) is not None:
                    durable_item = candidate
            if durable_item is None:
                r = process_album(album, args, allow_force=False,
                                  already_confirmed=True, token=active_token,
                                  treat_as_new=treat_as_new) or {}
            else:
                try:
                    results, drained = _execute_download_queue(
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
                        logging.getLogger("qobuz_librarian").warning(
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
                    logging.getLogger("qobuz_librarian").warning(
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
                cancelled_clean = (
                    result is not None
                    and result.get("result") == "cancelled"
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
                            "This download stopped before it finished "
                            "importing. Everything it had was saved, so Retry "
                            "picks up where it left off."
                        )
                    elif holds_control:
                        j.attention = "recovery"
                        j.error = (
                            "This download couldn't be confirmed as finished "
                            "cleanly, so downloads are paused. Use Retry on "
                            "this job to settle it."
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
        benign = {"already_complete", "skipped_already_higher_quality",
                  "skipped_has_extras", "dry_run", "user_skipped",
                  "lossy_only", "no_tracks", "cancelled"}
        if durable_failure:
            pass
        elif download_attention_kind(r) == "backup":
            _mark_download_attention(j, r)
        elif r.get("result") == "partial" and r.get("imported"):
            _mark_download_attention(j, r)
        elif r.get("result") not in benign and not r.get("imported"):
            j.status = job_mgr.JobStatus.FAILED
            if r.get("n_fail"):
                j.error = f"{plural(r['n_fail'], 'track')} failed. See job log."
            elif r.get("n_ok"):
                j.error = "Downloaded, but the import failed. See job log."
            else:
                j.error = ("No tracks were retrieved. Qobuz may be rate-limiting "
                           "you, or the release is unavailable. Try again shortly.")
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
            _refresh_after_local_album_change(
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
            from qobuz_librarian.web.flows import (
                _fold_partial_gap_fill,
                prune_library_review_candidates,
            )
            prune_library_review_candidates(album)
            retryable, _lossy_only = incomplete_track_counts(r)
            if retryable:
                # Partial landing: the album is on disk with gaps.
                _fold_partial_gap_fill(
                    album, (album.get("artist") or {}).get("name") or "",
                    retryable)
            from qobuz_librarian.library import hidden as hidden_mod
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


def _file_identity(st) -> list[int]:
    return [int(st.st_dev), int(st.st_ino)]


def _owned_file_identity(st) -> dict[str, int]:
    # Inodes can be recycled after a file is replaced.
    return {
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "modified_ns": int(st.st_mtime_ns),
        "changed_ns": int(st.st_ctime_ns),
    }


def _owned_directory_cleanup_entry(st, *, created) -> dict[str, int | bool]:
    return {
        **_owned_file_identity(st),
        "created": created is True,
    }


_OWNERSHIP_IDENTITY_FIELDS = (
    "device",
    "inode",
    "size",
    "modified_ns",
    "changed_ns",
)


def _ownership_device_inode_matches(st, expected):
    return (
        isinstance(expected, dict)
        and type(expected.get("device")) is int
        and type(expected.get("inode")) is int
        and [expected["device"], expected["inode"]] == _file_identity(st)
    )


def _ownership_identity_matches(st, expected):
    actual = _owned_file_identity(st)
    return (
        isinstance(expected, dict)
        and all(type(expected.get(field)) is int
                for field in _OWNERSHIP_IDENTITY_FIELDS)
        and all(actual[field] == expected[field]
                for field in _OWNERSHIP_IDENTITY_FIELDS)
    )


def _open_directory_nofollow(path, *, dir_fd=None):
    """Open one real directory without following a symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow directory access is unavailable")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, dir_fd=dir_fd)


def _owned_relative(root: Path, path: Path):
    root = Path(os.path.abspath(os.fspath(root)))
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        rel = path.relative_to(root)
    except ValueError:
        return root, None
    if not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        return root, None
    return root, rel


def _bind_owned_path(
    root,
    path,
    *,
    expected_file=None,
    expected_root=None,
    created_directories=None,
):
    """Bind one proven file and the positive directory-creation evidence."""
    root, rel = _owned_relative(Path(root), Path(path))
    if rel is None:
        return None
    created_records = {}
    for record in created_directories or ():
        relative_value = (
            record.get("relative") if isinstance(record, dict) else None)
        if (
            not isinstance(record, dict)
            or not all(
                type(record.get(field)) is int
                for field in _OWNERSHIP_IDENTITY_FIELDS
            )
            or not isinstance(relative_value, str)
            or "\x00" in relative_value
            or os.path.isabs(relative_value)
            or any(
                part in ("", ".", "..")
                for part in relative_value.split(os.sep)
            )
        ):
            return None
        _, created_relative = _owned_relative(
            root, root / relative_value)
        if created_relative is None:
            return None
        key = created_relative.as_posix()
        if key in created_records:
            return None
        created_records[key] = record
    opened = []
    seen_created_records = set()
    try:
        current = _open_directory_nofollow(root)
        opened.append(current)
        current_stat = os.fstat(current)
        if expected_root is not None and not _ownership_device_inode_matches(
            current_stat, expected_root
        ):
            return None
        directories = [_file_identity(current_stat)]
        cleanup_directories = [
            _owned_directory_cleanup_entry(current_stat, created=False)
        ]
        for index, part in enumerate(rel.parts[:-1], start=1):
            current = _open_directory_nofollow(part, dir_fd=current)
            opened.append(current)
            current_stat = os.fstat(current)
            directories.append(_file_identity(current_stat))
            relative_key = Path(*rel.parts[:index]).as_posix()
            created_record = created_records.get(relative_key)
            created_identity = (
                {
                    field: created_record[field]
                    for field in _OWNERSHIP_IDENTITY_FIELDS
                }
                if created_record is not None
                else None
            )
            if (
                created_record is not None
                and not _ownership_identity_matches(
                    current_stat, created_identity)
            ):
                return None
            if created_record is not None:
                seen_created_records.add(relative_key)
            cleanup_directories.append(_owned_directory_cleanup_entry(
                current_stat,
                created=created_record is not None,
            ))
        leaf = os.stat(rel.parts[-1], dir_fd=current, follow_symlinks=False)
        if not stat.S_ISREG(leaf.st_mode):
            return None
        if expected_file is not None and not _ownership_identity_matches(
            leaf, expected_file
        ):
            return None
        if seen_created_records != set(created_records):
            return None
        return {
            "relative": rel.as_posix(),
            "directories": directories,
            "file": _owned_file_identity(leaf),
            "directory_cleanup": {
                "version": 1,
                "parent_count": 0,
                "directories": cleanup_directories,
            },
        }
    except (OSError, TypeError, ValueError):
        return None
    finally:
        for fd in reversed(opened):
            try:
                os.close(fd)
            except OSError:
                pass


def _directory_cleanup_records(owned, expected_count):
    cleanup = owned.get("directory_cleanup")
    if (not isinstance(cleanup, dict)
            or type(cleanup.get("version")) is not int
            or cleanup.get("version") != 1):
        return None
    parent_count = cleanup.get("parent_count")
    if type(parent_count) is not int or parent_count < 0:
        return None
    records = cleanup.get("directories")
    if (not isinstance(records, list)
            or len(records) != parent_count + expected_count):
        return None
    for record in records:
        if not isinstance(record, dict) or type(record.get("created")) is not bool:
            return None
        for key in ("device", "inode", "size", "modified_ns", "changed_ns"):
            if type(record.get(key)) is not int:
                return None
    return parent_count, records


def _directory_cleanup_entry_matches(st, record):
    return all(
        record[key] == value
        for key, value in _owned_file_identity(st).items()
    )


def _close_owned_unlink_plan(plan):
    if not isinstance(plan, dict):
        return
    for descriptor in reversed(plan.get("opened", ())):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _ownership_rename_noreplace(
        first_parent_fd, first, second_parent_fd, second):
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError(
            errno.ENOTSUP, "atomic no-overwrite rename is unavailable") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
            first_parent_fd,
            os.fsencode(first),
            second_parent_fd,
            os.fsencode(second),
            1,
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


_INVALID_OWNED_DELETION = object()
_OWNED_QUARANTINE_NAME = re.compile(
    r"^\.ql-undo-(?:file|dir)-[0-9a-f]{32}$")
def _owned_deletion_record(value, kind):
    deletion = value.get("deletion") if isinstance(value, dict) else None
    if deletion is None:
        # Missing paths require a durable record created before deletion.
        if isinstance(value, dict) and "leaf_removed" in value:
            return _INVALID_OWNED_DELETION
        return None
    if (
        not isinstance(deletion, dict)
        or type(deletion.get("version")) is not int
        or deletion.get("version") != 1
        or deletion.get("state") not in ("intent", "held", "removed")
        or not isinstance(deletion.get("quarantine"), str)
        or not _OWNED_QUARANTINE_NAME.fullmatch(deletion["quarantine"])
        or not deletion["quarantine"].startswith(f".ql-undo-{kind}-")
    ):
        return _INVALID_OWNED_DELETION
    return deletion


def _record_owned_progress(progress):
    if progress is None:
        return True
    try:
        return progress() is not False
    except Exception:
        return False


def _begin_owned_deletion(value, kind, progress):
    deletion = _owned_deletion_record(value, kind)
    if deletion is _INVALID_OWNED_DELETION:
        return None
    if deletion is None:
        deletion = {
            "version": 1,
            "state": "intent",
            "quarantine": f".ql-undo-{kind}-{secrets.token_hex(16)}",
        }
        value["deletion"] = deletion
    # Confirm the complete current record before every retry. A failed prior
    # persist must not leave a later call free to mutate from memory alone.
    if not _record_owned_progress(progress):
        return None
    return deletion


def _ownership_stable_matches(st, expected, *, directory=False):
    wanted_mode = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        wanted_mode(st.st_mode)
        and isinstance(expected, dict)
        and all(
            type(expected.get(field)) is int
            for field in ("device", "inode", "size", "modified_ns")
        )
        and all(
            _owned_file_identity(st)[field] == expected[field]
            for field in ("device", "inode", "size", "modified_ns")
        )
    )


def _fsync_owned_directories(*descriptors):
    synced = set()
    for descriptor in descriptors:
        if descriptor is None:
            continue
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode):
            raise OSError(errno.ENOTDIR, "Undo anchor is not a directory")
        identity = (int(current.st_dev), int(current.st_ino))
        if identity in synced:
            continue
        os.fsync(descriptor)
        synced.add(identity)


def _owned_unlink_plan(root, owned):
    """Open one leaf chain and accept only a proven resumable state."""
    if not isinstance(owned, dict):
        return None
    relative = owned.get("relative")
    directories = owned.get("directories")
    file_identity = owned.get("file")
    deletion = _owned_deletion_record(owned, "file")
    if (
        not isinstance(relative, str)
        or not isinstance(directories, list)
        or not _valid_ownership_identity(file_identity)
        or deletion is _INVALID_OWNED_DELETION
        or (isinstance(deletion, dict)
            and deletion.get("state") == "removed")
    ):
        return None
    root, rel = _owned_relative(Path(root), Path(root) / relative)
    if rel is None or len(directories) != len(rel.parts):
        return None
    if not all(
        isinstance(expected, list)
        and len(expected) == 2
        and all(type(value) is int for value in expected)
        for expected in directories
    ):
        return None

    cleanup = _directory_cleanup_records(owned, len(directories))
    if cleanup is not None:
        parent_count, cleanup_records = cleanup
        try:
            cleanup_anchor = root.parents[parent_count]
        except IndexError:
            cleanup = None
        else:
            music_root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
            if parent_count > 0 and cleanup_anchor != music_root:
                cleanup = None
    if cleanup is None:
        parent_count = 0
        cleanup_records = None

    opened = []
    try:
        if root == root.parent:
            current = _open_directory_nofollow(root)
            opened.append(current)
            directory_fds = [current]
            parent_fds = [None]
            directory_names = [root.name]
            cleanup_records = None
        else:
            anchor = root.parents[parent_count]
            current = _open_directory_nofollow(anchor)
            opened.append(current)
            directory_fds = []
            parent_fds = []
            directory_names = []
            chain_names = [*root.relative_to(anchor).parts, *rel.parts[:-1]]
            for part in chain_names:
                parent = current
                current = _open_directory_nofollow(part, dir_fd=current)
                opened.append(current)
                parent_fds.append(parent)
                directory_fds.append(current)
                directory_names.append(part)

        owned_directory_fds = directory_fds[parent_count:]
        if len(owned_directory_fds) != len(directories):
            raise ValueError("owned directory chain length changed")
        for fd, expected in zip(owned_directory_fds, directories, strict=True):
            if _file_identity(os.fstat(fd)) != expected:
                raise ValueError("owned directory identity changed")
        cleanup_matches = (
            [
                _directory_cleanup_entry_matches(os.fstat(fd), record)
                for fd, record in zip(directory_fds, cleanup_records, strict=True)
            ]
            if parent_fds[0] is not None and cleanup_records is not None
            else None
        )
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file access is unavailable")
        leaf_fd = None
        try:
            leaf_fd = os.open(
                rel.parts[-1],
                os.O_RDONLY
                | nofollow
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current,
            )
        except FileNotFoundError:
            if deletion is None:
                raise ValueError("owned leaf disappeared without deletion intent")
        if leaf_fd is not None:
            opened.append(leaf_fd)
            leaf = os.fstat(leaf_fd)
            named_leaf = os.stat(
                rel.parts[-1], dir_fd=current, follow_symlinks=False)
            if _owned_file_identity(leaf) != _owned_file_identity(named_leaf):
                raise ValueError("owned leaf name changed")
            if _owned_file_identity(leaf) != file_identity:
                raise ValueError("owned leaf identity changed")
        return {
            "root": root,
            "relative": rel,
            "path": root / rel,
            "opened": opened,
            "leaf_parent": current,
            "leaf_name": rel.parts[-1],
            "leaf_fd": leaf_fd,
            "owned": owned,
            "file": file_identity,
            "deletion": deletion,
            "directory_fds": directory_fds,
            "parent_fds": parent_fds,
            "directory_names": directory_names,
            "cleanup_records": cleanup_records,
            "cleanup_matches": cleanup_matches,
        }
    except (OSError, TypeError, ValueError):
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None


def _refresh_owned_cleanup_records(plan):
    """Refresh exact directory proofs changed by this Undo operation."""
    records = plan.get("cleanup_records") if isinstance(plan, dict) else None
    matches = plan.get("cleanup_matches") if isinstance(plan, dict) else None
    if records is None or matches is None:
        return
    for directory_fd, record, matched in zip(
            plan["directory_fds"], records, matches, strict=True):
        # Never turn a directory that was already changed before this Undo
        # attempt into one of our own mutations.
        if not matched:
            continue
        try:
            current = os.fstat(directory_fd)
        except OSError:
            continue
        if _file_identity(current) != [record["device"], record["inode"]]:
            continue
        record.update(_owned_file_identity(current))


def _open_owned_leaf_quarantine(plan):
    deletion = plan.get("deletion")
    if not isinstance(deletion, dict):
        return {
            "missing": True,
            "fd": None,
            "held_fd": None,
            "held_full_match": False,
            "held_stable_match": False,
        }
    name = deletion["quarantine"]
    try:
        quarantine_fd = _open_directory_nofollow(
            name, dir_fd=plan["leaf_parent"])
    except FileNotFoundError:
        return {
            "missing": True,
            "fd": None,
            "held_fd": None,
            "held_full_match": False,
            "held_stable_match": False,
        }
    except OSError:
        return None
    held_fd = None
    try:
        current = os.fstat(quarantine_fd)
        named = os.stat(
            name, dir_fd=plan["leaf_parent"], follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or _owned_file_identity(current) != _owned_file_identity(named)
        ):
            raise ValueError("quarantine directory changed")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file access is unavailable")
        try:
            held_fd = os.open(
                "held",
                os.O_RDONLY
                | nofollow
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=quarantine_fd,
            )
        except FileNotFoundError:
            held_fd = None
        held_identity = None
        held_full_match = False
        held_stable_match = False
        if held_fd is not None:
            held = os.fstat(held_fd)
            named_held = os.stat(
                "held", dir_fd=quarantine_fd, follow_symlinks=False)
            if _owned_file_identity(held) != _owned_file_identity(named_held):
                raise ValueError("quarantined leaf changed")
            held_identity = _owned_file_identity(held)
            held_full_match = _ownership_identity_matches(
                held, plan["file"])
            held_stable_match = _ownership_stable_matches(
                held, plan["file"])
        return {
            "missing": False,
            "fd": quarantine_fd,
            "held_fd": held_fd,
            "held_identity": held_identity,
            "held_full_match": held_full_match,
            "held_stable_match": held_stable_match,
        }
    except (OSError, TypeError, ValueError):
        if held_fd is not None:
            try:
                os.close(held_fd)
            except OSError:
                pass
        try:
            os.close(quarantine_fd)
        except OSError:
            pass
        return None
    except BaseException:
        if held_fd is not None:
            try:
                os.close(held_fd)
            except OSError:
                pass
        try:
            os.close(quarantine_fd)
        except OSError:
            pass
        raise


def _close_owned_quarantine(snapshot):
    if not isinstance(snapshot, dict):
        return
    for key in ("held_fd", "fd"):
        descriptor = snapshot.get(key)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _owned_unlink_leaf_matches(plan):
    deletion = plan.get("deletion")
    snapshot = _open_owned_leaf_quarantine(plan)
    if snapshot is None:
        return False
    try:
        public_exists = plan.get("leaf_fd") is not None
        held_exists = snapshot.get("held_fd") is not None
        if public_exists and held_exists:
            return False
        if deletion is None:
            return public_exists and not held_exists
        if held_exists:
            if public_exists:
                return False
            if deletion.get("state") == "intent":
                return snapshot.get("held_stable_match") is True
            if deletion.get("state") == "held":
                return snapshot.get("held_full_match") is True
            return False
        return True
    finally:
        _close_owned_quarantine(snapshot)


def _remove_owned_leaf_quarantine(plan, snapshot):
    if snapshot.get("missing"):
        return True
    if snapshot.get("held_fd") is not None:
        return False
    try:
        held = os.fstat(snapshot["fd"])
        named = os.stat(
            plan["deletion"]["quarantine"],
            dir_fd=plan["leaf_parent"],
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(named.st_mode)
            or _owned_file_identity(held) != _owned_file_identity(named)
        ):
            return False
        os.rmdir(
            plan["deletion"]["quarantine"],
            dir_fd=plan["leaf_parent"],
        )
        _fsync_owned_directories(plan["leaf_parent"])
        return True
    except (OSError, TypeError, ValueError):
        return False


def _restore_owned_leaf_from_intent(
        plan, snapshot, progress, cleanup_plan):
    """Restore a rename-interrupted leaf without authorising its deletion."""
    if (
        snapshot.get("held_fd") is None
        or snapshot.get("held_stable_match") is not True
        or plan["deletion"].get("state") != "intent"
    ):
        return {"status": "held", "mutated": False}
    previous_file = dict(plan["file"])
    moved = False
    try:
        _ownership_rename_noreplace(
            snapshot["fd"],
            "held",
            plan["leaf_parent"],
            plan["leaf_name"],
        )
        moved = True
        restored = os.fstat(snapshot["held_fd"])
        named = os.stat(
            plan["leaf_name"],
            dir_fd=plan["leaf_parent"],
            follow_symlinks=False,
        )
        if (
            _owned_file_identity(restored) != _owned_file_identity(named)
            or not _ownership_stable_matches(restored, plan["file"])
        ):
            raise OSError("restored Undo leaf changed")
        _fsync_owned_directories(snapshot["fd"], plan["leaf_parent"])
        plan["owned"]["file"] = _owned_file_identity(restored)
        plan["file"] = plan["owned"]["file"]
        plan["deletion"]["state"] = "intent"
        _refresh_owned_cleanup_records(cleanup_plan)
        if _record_owned_progress(progress):
            return {"status": "restored", "mutated": True}
        try:
            _ownership_rename_noreplace(
                plan["leaf_parent"],
                plan["leaf_name"],
                snapshot["fd"],
                "held",
            )
            held_again = os.fstat(snapshot["held_fd"])
            named_again = os.stat(
                "held", dir_fd=snapshot["fd"], follow_symlinks=False)
            if (
                _owned_file_identity(held_again)
                != _owned_file_identity(named_again)
                or not _ownership_stable_matches(
                    held_again, previous_file)
            ):
                raise OSError("rolled-back Undo leaf changed")
            _fsync_owned_directories(
                snapshot["fd"], plan["leaf_parent"])
            plan["owned"]["file"] = previous_file
            plan["file"] = previous_file
            plan["deletion"]["state"] = "intent"
            _refresh_owned_cleanup_records(cleanup_plan)
            return {"status": "held", "mutated": True}
        except (OSError, TypeError, ValueError):
            return {"status": "restored", "mutated": True}
    except (OSError, TypeError, ValueError):
        if moved:
            try:
                _ownership_rename_noreplace(
                    plan["leaf_parent"],
                    plan["leaf_name"],
                    snapshot["fd"],
                    "held",
                )
                _fsync_owned_directories(
                    snapshot["fd"], plan["leaf_parent"])
            except OSError:
                pass
        _refresh_owned_cleanup_records(cleanup_plan)
        _record_owned_progress(progress)
        return {"status": "held", "mutated": moved}


def _quarantine_owned_leaf(plan, *, progress=None, cleanup_plan=None):
    """Durably remove one proved leaf through its persisted private name."""
    cleanup_plan = cleanup_plan or plan
    snapshot = None
    lease_fd = plan.get("leaf_fd")
    if lease_fd is None and isinstance(plan.get("deletion"), dict):
        snapshot = _open_owned_leaf_quarantine(plan)
        if snapshot is None:
            return {"status": "refused", "mutated": False}
        lease_fd = snapshot.get("held_fd")
    exclusion = (
        acquire_inode_write_exclusion(lease_fd)
        if lease_fd is not None
        else None
    )
    if lease_fd is not None and exclusion is None:
        _close_owned_quarantine(snapshot)
        return {"status": "refused", "mutated": False}

    try:
        deletion = _begin_owned_deletion(plan["owned"], "file", progress)
    except BaseException:
        if exclusion is not None:
            exclusion.close()
        _close_owned_quarantine(snapshot)
        raise
    if deletion is None:
        if exclusion is not None:
            exclusion.close()
        _close_owned_quarantine(snapshot)
        return {"status": "refused", "mutated": False}
    plan["deletion"] = deletion

    if snapshot is None:
        try:
            snapshot = _open_owned_leaf_quarantine(plan)
        except BaseException:
            if exclusion is not None:
                exclusion.close()
            raise
    if snapshot is None:
        if exclusion is not None:
            exclusion.close()
        return {"status": "refused", "mutated": False}
    try:
        if exclusion is not None and not exclusion.intact():
            return {"status": "refused", "mutated": False}
        public_exists = plan.get("leaf_fd") is not None
        held_exists = snapshot.get("held_fd") is not None
        if public_exists and held_exists:
            return {"status": "refused", "mutated": False}

        if not public_exists and held_exists:
            if (
                deletion.get("state") == "intent"
                and snapshot.get("held_stable_match") is True
            ):
                return _restore_owned_leaf_from_intent(
                    plan, snapshot, progress, cleanup_plan)
            if (
                deletion.get("state") != "held"
                or snapshot.get("held_full_match") is not True
            ):
                return {"status": "held", "mutated": False}

        if not public_exists and not held_exists:
            if not snapshot.get("missing"):
                if not _remove_owned_leaf_quarantine(plan, snapshot):
                    return {"status": "held", "mutated": False}
                _refresh_owned_cleanup_records(cleanup_plan)
            else:
                try:
                    _fsync_owned_directories(plan["leaf_parent"])
                except OSError:
                    return {"status": "held", "mutated": False}
            deletion["state"] = "removed"
            _refresh_owned_cleanup_records(cleanup_plan)
            if not _record_owned_progress(progress):
                return {"status": "held", "mutated": True}
            return {"status": "removed", "mutated": True}

        if public_exists and snapshot.get("missing"):
            try:
                os.mkdir(
                    deletion["quarantine"],
                    0o700,
                    dir_fd=plan["leaf_parent"],
                )
                _fsync_owned_directories(plan["leaf_parent"])
            except OSError:
                return {"status": "refused", "mutated": False}
            _refresh_owned_cleanup_records(cleanup_plan)
            if not _record_owned_progress(progress):
                return {"status": "restored", "mutated": True}
            _close_owned_quarantine(snapshot)
            snapshot = _open_owned_leaf_quarantine(plan)
            if snapshot is None or snapshot.get("missing"):
                return {"status": "refused", "mutated": True}

        if public_exists:
            previous_file = dict(plan["file"])
            moved_public = False
            validated_moved = False
            rolled_back = False
            try:
                current = os.fstat(plan["leaf_fd"])
                named_current = os.stat(
                    plan["leaf_name"],
                    dir_fd=plan["leaf_parent"],
                    follow_symlinks=False,
                )
                if (
                    exclusion is None
                    or not exclusion.intact()
                    or _owned_file_identity(current)
                    != _owned_file_identity(named_current)
                    or not _ownership_identity_matches(
                        current, plan["file"])
                ):
                    raise ValueError(
                        "public leaf changed after deletion intent")
                _ownership_rename_noreplace(
                    plan["leaf_parent"],
                    plan["leaf_name"],
                    snapshot["fd"],
                    "held",
                )
                moved_public = True
                moved = os.fstat(plan["leaf_fd"])
                named = os.stat(
                    "held", dir_fd=snapshot["fd"], follow_symlinks=False)
                if (
                    _owned_file_identity(moved) != _owned_file_identity(named)
                    or not _ownership_stable_matches(moved, plan["file"])
                ):
                    raise ValueError("public leaf changed at quarantine")
                validated_moved = True
                _fsync_owned_directories(
                    snapshot["fd"], plan["leaf_parent"])
            except (OSError, TypeError, ValueError):
                if moved_public:
                    try:
                        _ownership_rename_noreplace(
                            snapshot["fd"],
                            "held",
                            plan["leaf_parent"],
                            plan["leaf_name"],
                        )
                        restored = os.fstat(plan["leaf_fd"])
                        named_restored = os.stat(
                            plan["leaf_name"],
                            dir_fd=plan["leaf_parent"],
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(restored)
                            != _owned_file_identity(named_restored)
                            or not _ownership_stable_matches(
                                restored, plan["file"])
                        ):
                            raise OSError("restored Undo leaf changed")
                        _fsync_owned_directories(
                            snapshot["fd"], plan["leaf_parent"])
                        rolled_back = True
                    except (OSError, TypeError, ValueError):
                        pass
                if validated_moved and rolled_back:
                    plan["owned"]["file"] = _owned_file_identity(restored)
                    plan["file"] = plan["owned"]["file"]
                    deletion["state"] = "intent"
                _refresh_owned_cleanup_records(cleanup_plan)
                persisted = _record_owned_progress(progress)
                if validated_moved and rolled_back and not persisted:
                    refreshed_file = dict(plan["file"])
                    try:
                        current = os.fstat(plan["leaf_fd"])
                        named_current = os.stat(
                            plan["leaf_name"],
                            dir_fd=plan["leaf_parent"],
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(current)
                            != _owned_file_identity(named_current)
                            or not _ownership_identity_matches(
                                current, refreshed_file)
                        ):
                            raise OSError("restored Undo leaf changed")
                        _ownership_rename_noreplace(
                            plan["leaf_parent"],
                            plan["leaf_name"],
                            snapshot["fd"],
                            "held",
                        )
                        held_again = os.fstat(plan["leaf_fd"])
                        named_again = os.stat(
                            "held",
                            dir_fd=snapshot["fd"],
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(held_again)
                            != _owned_file_identity(named_again)
                            or not _ownership_stable_matches(
                                held_again, refreshed_file)
                        ):
                            raise OSError("re-quarantined Undo leaf changed")
                        _fsync_owned_directories(
                            snapshot["fd"], plan["leaf_parent"])
                        plan["owned"]["file"] = previous_file
                        plan["file"] = previous_file
                        deletion["state"] = "intent"
                        _refresh_owned_cleanup_records(cleanup_plan)
                    except (OSError, TypeError, ValueError):
                        pass
                return {
                    "status": "refused",
                    "mutated": moved_public,
                }
            plan["owned"]["file"] = _owned_file_identity(moved)
            plan["file"] = plan["owned"]["file"]
            deletion["state"] = "held"
            _refresh_owned_cleanup_records(cleanup_plan)
            if not _record_owned_progress(progress):
                return {"status": "held", "mutated": True}
            _close_owned_quarantine(snapshot)
            snapshot = _open_owned_leaf_quarantine(plan)
            if snapshot is None or snapshot.get("held_fd") is None:
                return {"status": "refused", "mutated": True}
        elif deletion.get("state") != "held":
            deletion["state"] = "held"
            plan["owned"]["file"] = snapshot["held_identity"]
            plan["file"] = plan["owned"]["file"]
            _refresh_owned_cleanup_records(cleanup_plan)
            if not _record_owned_progress(progress):
                return {"status": "held", "mutated": False}

        try:
            held_now = os.fstat(snapshot["held_fd"])
            named_held_now = os.stat(
                "held", dir_fd=snapshot["fd"], follow_symlinks=False)
            if (
                exclusion is None
                or not exclusion.intact()
                or _owned_file_identity(held_now)
                != _owned_file_identity(named_held_now)
                or not _ownership_identity_matches(
                    held_now, plan["file"])
            ):
                return {"status": "held", "mutated": False}
            if exclusion is None or not exclusion.intact():
                return {"status": "held", "mutated": False}
            os.unlink("held", dir_fd=snapshot["fd"])
            _fsync_owned_directories(snapshot["fd"])
        except (OSError, TypeError, ValueError):
            # Keep the exact leaf under its persisted private name.
            return {"status": "held", "mutated": False}

        _refresh_owned_cleanup_records(cleanup_plan)
        if not _record_owned_progress(progress):
            return {"status": "held", "mutated": True}
        try:
            os.close(snapshot["held_fd"])
        except OSError:
            pass
        snapshot["held_fd"] = None
        if not _remove_owned_leaf_quarantine(plan, snapshot):
            return {"status": "held", "mutated": True}
        deletion["state"] = "removed"
        _refresh_owned_cleanup_records(cleanup_plan)
        if not _record_owned_progress(progress):
            return {"status": "held", "mutated": True}
        return {"status": "removed", "mutated": True}
    finally:
        if exclusion is not None:
            exclusion.close()
        _close_owned_quarantine(snapshot)


def _owned_directory_chain(root, owned):
    relative = owned.get("relative") if isinstance(owned, dict) else None
    directories = owned.get("directories") if isinstance(owned, dict) else None
    if not isinstance(relative, str) or not isinstance(directories, list):
        return None
    root, rel = _owned_relative(Path(root), Path(root) / relative)
    if rel is None or len(directories) != len(rel.parts) or root == root.parent:
        return None
    cleanup = _directory_cleanup_records(owned, len(directories))
    if cleanup is None:
        return None
    parent_count, records = cleanup
    try:
        anchor = root.parents[parent_count]
    except IndexError:
        return None
    music_root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    if parent_count > 0 and anchor != music_root:
        return None
    names = [*root.relative_to(anchor).parts, *rel.parts[:-1]]
    if len(names) != len(records):
        return None
    return {
        "root": root,
        "relative": rel,
        "anchor": anchor,
        "names": names,
        "records": records,
        "cleanup": owned["directory_cleanup"],
    }


def _refresh_open_directory_records(opened, *, include_last=True):
    entries = opened if include_last else opened[:-1]
    for descriptor, record, matched in entries:
        if not matched:
            continue
        try:
            current = os.fstat(descriptor)
        except OSError:
            continue
        if _file_identity(current) == [record["device"], record["inode"]]:
            record.update(_owned_file_identity(current))


def _restore_owned_directory(
        parent_fd, name, held_fd, record, deletion, opened, progress,
        *, terminal_skip=False):
    previous_identity = {
        field: record[field] for field in _OWNERSHIP_IDENTITY_FIELDS
    }
    previous_state = deletion["state"]
    try:
        _ownership_rename_noreplace(
            parent_fd, deletion["quarantine"], parent_fd, name)
    except OSError:
        record.update(_owned_file_identity(os.fstat(held_fd)))
        deletion["state"] = "held"
        _refresh_open_directory_records(opened)
        _record_owned_progress(progress)
        return "retry"
    try:
        _fsync_owned_directories(parent_fd)
    except OSError:
        state = "intent"
        try:
            _ownership_rename_noreplace(
                parent_fd, name, parent_fd, deletion["quarantine"])
            _fsync_owned_directories(parent_fd)
            state = "held"
        except OSError:
            pass
        record.update(_owned_file_identity(os.fstat(held_fd)))
        deletion["state"] = state
        _refresh_open_directory_records(opened)
        _record_owned_progress(progress)
        return "retry"
    try:
        restored = os.fstat(held_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _owned_file_identity(restored) != _owned_file_identity(named)
            or not stat.S_ISDIR(restored.st_mode)
            or (
                not terminal_skip
                and not _ownership_stable_matches(
                    restored, record, directory=True)
            )
        ):
            _refresh_open_directory_records(opened, include_last=False)
            _record_owned_progress(progress)
            return "retry"
        record.update(_owned_file_identity(restored))
        deletion["state"] = "intent"
        _refresh_open_directory_records(opened)
        persisted = _record_owned_progress(progress)
        if persisted:
            return "skipped" if terminal_skip else "retry"
        try:
            _ownership_rename_noreplace(
                parent_fd, name, parent_fd, deletion["quarantine"])
            held_again = os.fstat(held_fd)
            named_again = os.stat(
                deletion["quarantine"],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                _owned_file_identity(held_again)
                != _owned_file_identity(named_again)
                or not _ownership_stable_matches(
                    held_again, previous_identity, directory=True)
            ):
                raise OSError("rolled-back Undo directory changed")
            _fsync_owned_directories(parent_fd)
            record.update(previous_identity)
            deletion["state"] = previous_state
            _refresh_open_directory_records(opened, include_last=False)
        except (OSError, TypeError, ValueError):
            pass
        return "retry"
    except (OSError, TypeError, ValueError):
        return "retry"


def _cleanup_one_owned_directory(chain, index, progress):
    records = chain["records"]
    record = records[index]
    deletion = _owned_deletion_record(record, "dir")
    if deletion is _INVALID_OWNED_DELETION:
        return "refused"
    opened_fds = []
    opened_records = []
    restored_public_skip = False
    try:
        current = _open_directory_nofollow(chain["anchor"])
        opened_fds.append(current)
        parent_fd = current
        held_fd = None
        public_exists = False
        for current_index, (name, current_record) in enumerate(zip(
                chain["names"][:index + 1], records[:index + 1], strict=True)):
            parent_fd = current
            try:
                current = _open_directory_nofollow(name, dir_fd=parent_fd)
            except FileNotFoundError:
                if current_index != index:
                    return "refused"
                public_exists = False
                held_fd = None
                break
            opened_fds.append(current)
            current_stat = os.fstat(current)
            full_match = _directory_cleanup_entry_matches(
                current_stat, current_record)
            opened_records.append((current, current_record, full_match))
            if current_index < index:
                # Ancestors can legitimately change when a sibling album or
                # disc is added.
                if _file_identity(current_stat) != [
                    current_record["device"], current_record["inode"]
                ]:
                    return "skipped"
                continue
            if not full_match:
                # A public directory whose durable proof no longer matches is
                # ambiguous after a rollback or restart.
                if (
                    isinstance(deletion, dict)
                    and deletion.get("state") == "held"
                    and _ownership_stable_matches(
                        current_stat, current_record, directory=True)
                ):
                    # A crash after a no-overwrite private→public restore can
                    # leave only ctime changed.
                    restored_public_skip = True
                else:
                    return "refused" if deletion is not None else "skipped"
            public_exists = True
            held_fd = current

        if deletion is None and not public_exists:
            return "refused"
        deletion = _begin_owned_deletion(record, "dir", progress)
        if deletion is None:
            return "retry"

        quarantine_fd = None
        try:
            quarantine_fd = _open_directory_nofollow(
                deletion["quarantine"], dir_fd=parent_fd)
        except FileNotFoundError:
            quarantine_fd = None
        except OSError:
            return "refused"
        if quarantine_fd is not None:
            opened_fds.append(quarantine_fd)
            quarantined = os.fstat(quarantine_fd)
            named_quarantine = os.stat(
                deletion["quarantine"],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                _owned_file_identity(quarantined)
                != _owned_file_identity(named_quarantine)
            ):
                return "refused"
            if public_exists:
                return "refused"
            if not _ownership_identity_matches(quarantined, record):
                if (
                    deletion.get("state") == "intent"
                    and _ownership_stable_matches(
                        quarantined, record, directory=True)
                ):
                    return _restore_owned_directory(
                        parent_fd,
                        chain["names"][index],
                        quarantine_fd,
                        record,
                        deletion,
                        opened_records,
                        progress,
                    )
                return "refused"
            held_fd = quarantine_fd

        if restored_public_skip:
            return "skipped" if quarantine_fd is None else "refused"

        if not public_exists and quarantine_fd is None:
            try:
                _fsync_owned_directories(parent_fd)
            except OSError:
                return "retry"
            deletion["state"] = "removed"
            _refresh_open_directory_records(opened_records)
            return "removed" if _record_owned_progress(progress) else "retry"

        if public_exists and quarantine_fd is None:
            previous_identity = {
                field: record[field]
                for field in _OWNERSHIP_IDENTITY_FIELDS
            }
            previous_state = deletion["state"]
            moved_public = False
            validated_moved = False
            rolled_back = False
            try:
                current = os.fstat(held_fd)
                named_current = os.stat(
                    chain["names"][index],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    _owned_file_identity(current)
                    != _owned_file_identity(named_current)
                    or not _directory_cleanup_entry_matches(
                        current, record)
                ):
                    raise ValueError(
                        "public directory changed after deletion intent")
                _ownership_rename_noreplace(
                    parent_fd,
                    chain["names"][index],
                    parent_fd,
                    deletion["quarantine"],
                )
                moved_public = True
                moved = os.fstat(held_fd)
                named = os.stat(
                    deletion["quarantine"],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    _owned_file_identity(moved) != _owned_file_identity(named)
                    or not _ownership_stable_matches(
                        moved, record, directory=True)
                ):
                    raise ValueError("public directory changed at quarantine")
                validated_moved = True
                _fsync_owned_directories(parent_fd)
            except (OSError, TypeError, ValueError):
                if moved_public:
                    try:
                        _ownership_rename_noreplace(
                            parent_fd,
                            deletion["quarantine"],
                            parent_fd,
                            chain["names"][index],
                        )
                        restored = os.fstat(held_fd)
                        named_restored = os.stat(
                            chain["names"][index],
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(restored)
                            != _owned_file_identity(named_restored)
                            or not _ownership_stable_matches(
                                restored, record, directory=True)
                        ):
                            raise OSError("restored Undo directory changed")
                        _fsync_owned_directories(parent_fd)
                        rolled_back = True
                    except (OSError, TypeError, ValueError):
                        pass
                if validated_moved and rolled_back:
                    record.update(_owned_file_identity(restored))
                    deletion["state"] = "intent"
                    _refresh_open_directory_records(opened_records)
                else:
                    _refresh_open_directory_records(
                        opened_records, include_last=False)
                persisted = _record_owned_progress(progress)
                if validated_moved and rolled_back and not persisted:
                    refreshed_identity = {
                        field: record[field]
                        for field in _OWNERSHIP_IDENTITY_FIELDS
                    }
                    try:
                        current = os.fstat(held_fd)
                        named_current = os.stat(
                            chain["names"][index],
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(current)
                            != _owned_file_identity(named_current)
                            or not _ownership_identity_matches(
                                current, refreshed_identity)
                        ):
                            raise OSError("restored Undo directory changed")
                        _ownership_rename_noreplace(
                            parent_fd,
                            chain["names"][index],
                            parent_fd,
                            deletion["quarantine"],
                        )
                        held_again = os.fstat(held_fd)
                        named_again = os.stat(
                            deletion["quarantine"],
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            _owned_file_identity(held_again)
                            != _owned_file_identity(named_again)
                            or not _ownership_stable_matches(
                                held_again,
                                refreshed_identity,
                                directory=True,
                            )
                        ):
                            raise OSError(
                                "re-quarantined Undo directory changed")
                        _fsync_owned_directories(parent_fd)
                        record.update(previous_identity)
                        deletion["state"] = previous_state
                        _refresh_open_directory_records(
                            opened_records, include_last=False)
                    except (OSError, TypeError, ValueError):
                        pass
                return "retry"
            record.update(_owned_file_identity(moved))
            deletion["state"] = "held"
            _refresh_open_directory_records(opened_records)
            if not _record_owned_progress(progress):
                return "retry"
        elif deletion["state"] != "held":
            record.update(_owned_file_identity(os.fstat(held_fd)))
            deletion["state"] = "held"
            _refresh_open_directory_records(opened_records)
            if not _record_owned_progress(progress):
                return "retry"

        try:
            held_now = os.fstat(held_fd)
            named_now = os.stat(
                deletion["quarantine"],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                _owned_file_identity(held_now)
                != _owned_file_identity(named_now)
                or not _ownership_identity_matches(held_now, record)
            ):
                return "refused"
            os.rmdir(deletion["quarantine"], dir_fd=parent_fd)
            _fsync_owned_directories(parent_fd)
        except OSError as exc:
            return _restore_owned_directory(
                parent_fd,
                chain["names"][index],
                held_fd,
                record,
                deletion,
                opened_records,
                progress,
                terminal_skip=exc.errno in (errno.ENOTEMPTY, errno.EEXIST),
            )
        deletion["state"] = "removed"
        _refresh_open_directory_records(
            opened_records, include_last=not public_exists)
        return "removed" if _record_owned_progress(progress) else "retry"
    except (OSError, TypeError, ValueError):
        return "refused"
    finally:
        for descriptor in reversed(opened_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _cleanup_owned_directories(root, owned, progress):
    chain = _owned_directory_chain(root, owned)
    if chain is None:
        return True
    complete = chain["cleanup"].get("complete", False)
    if type(complete) is not bool:
        return False
    if complete:
        return True
    created_tail = []
    for index in reversed(range(len(chain["records"]))):
        if chain["records"][index]["created"] is not True:
            break
        created_tail.append(index)
    if not created_tail:
        chain["cleanup"]["complete"] = True
        return _record_owned_progress(progress)

    while True:
        candidate = None
        for index in created_tail:
            deletion = _owned_deletion_record(chain["records"][index], "dir")
            if deletion is _INVALID_OWNED_DELETION:
                return False
            if deletion is None or deletion["state"] != "removed":
                candidate = index
                break
        if candidate is None:
            chain["cleanup"]["complete"] = True
            return _record_owned_progress(progress)
        outcome = _cleanup_one_owned_directory(chain, candidate, progress)
        if outcome == "removed":
            continue
        if outcome == "skipped":
            chain["cleanup"]["complete"] = True
            return _record_owned_progress(progress)
        return False


def _unlink_owned_path(root, owned, *, progress=None, outcome_out=None):
    """Remove exact owned leaves, then retry exact created-folder cleanup."""
    if not isinstance(owned, dict):
        return None
    companions = owned.get("companions", [])
    if not isinstance(companions, list) or len(companions) > 2:
        return None
    root, main_relative = _owned_relative(
        Path(root), Path(root) / str(owned.get("relative", "")))
    if main_relative is None:
        return None
    expected_companion = main_relative.with_suffix(".lrc").as_posix()
    seen_kinds = set()
    seen_relatives = {main_relative.as_posix()}
    for companion in companions:
        kind = companion.get("kind") if isinstance(companion, dict) else None
        relative = companion.get("relative") if isinstance(companion, dict) else None
        companion_relative = (
            _strict_ownership_relative(root, relative)
            if isinstance(relative, str) else None
        )
        if (
            not isinstance(companion, dict)
            or companion.get("companions")
            or kind not in ("lyrics", "artwork")
            or kind in seen_kinds
            or companion_relative is None
            or companion_relative.as_posix() in seen_relatives
        ):
            return None
        if kind == "lyrics" and relative != expected_companion:
            return None
        if (
            kind == "artwork"
            and (
                len(companion_relative.parent.parts)
                >= len(main_relative.parts)
                or main_relative.parts[:len(companion_relative.parent.parts)]
                != companion_relative.parent.parts
            )
        ):
            return None
        seen_kinds.add(kind)
        seen_relatives.add(companion_relative.as_posix())

    entries = [owned, *companions]
    deletion_records = [
        _owned_deletion_record(entry, "file") for entry in entries]
    if any(record is _INVALID_OWNED_DELETION for record in deletion_records):
        return None

    def _update_outcome(**extra):
        current = [
            _owned_deletion_record(entry, "file") for entry in entries]
        removed_count = sum(
            isinstance(record, dict) and record["state"] == "removed"
            for record in current
        )
        held_count = sum(
            isinstance(record, dict) and record["state"] == "held"
            for record in current
        )
        if isinstance(outcome_out, dict):
            outcome_out.update({
                "files_complete": removed_count == len(entries),
                "removed_files": removed_count,
                "held_files": held_count,
                "undo_started": any(
                    isinstance(record, dict) for record in current),
                **extra,
            })

    # A retry may begin after an earlier attempt durably removed a companion.
    _update_outcome()
    if (
        deletion_records[0] is not None
        and deletion_records[0]["state"] == "removed"
        and any(record is None or record["state"] != "removed"
                for record in deletion_records[1:])
    ):
        return None

    plans = []
    plan_by_entry = {}
    try:
        for entry, deletion in zip(entries, deletion_records, strict=True):
            if deletion is not None and deletion["state"] == "removed":
                continue
            plan = _owned_unlink_plan(root, entry)
            if plan is None:
                return None
            plans.append(plan)
            plan_by_entry[id(entry)] = plan
        if not all(_owned_unlink_leaf_matches(plan) for plan in plans):
            return None
        main_plan = plan_by_entry.get(id(owned))
        for entry in [*companions, owned]:
            deletion = _owned_deletion_record(entry, "file")
            if isinstance(deletion, dict) and deletion["state"] == "removed":
                continue
            plan = plan_by_entry[id(entry)]
            result = _quarantine_owned_leaf(
                plan,
                progress=progress,
                cleanup_plan=main_plan or plan,
            )
            if result["status"] != "removed":
                _update_outcome()
                return None
    except (OSError, TypeError, ValueError):
        return None
    finally:
        for plan in reversed(plans):
            _close_owned_unlink_plan(plan)

    files_complete = all(
        isinstance(_owned_deletion_record(entry, "file"), dict)
        and _owned_deletion_record(entry, "file")["state"] == "removed"
        for entry in entries
    )
    cleanup_complete = (
        files_complete and _cleanup_owned_directories(root, owned, progress))
    _update_outcome(
        cleanup_pending=files_complete and not cleanup_complete)
    if not files_complete or not cleanup_complete:
        return None
    return root / main_relative


def _valid_ownership_identity(value):
    return (
        isinstance(value, dict)
        and all(type(value.get(field)) is int
                for field in _OWNERSHIP_IDENTITY_FIELDS)
    )


def _strict_ownership_relative(root, value):
    if (
        not isinstance(value, str)
        or "\x00" in value
        or os.path.isabs(value)
    ):
        return None
    parts = value.split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        return None
    _, relative = _owned_relative(root, root / value)
    return relative


def _single_ownership_item(payload):
    """Validate the sealed one-item evidence before touching the library."""
    root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
        or payload.get("sealed") is not True
        or payload.get("root") != str(root)
        or not _valid_ownership_identity(payload.get("root_identity"))
    ):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1:
        return None
    item = items[0]
    if not isinstance(item, dict) or not _valid_ownership_identity(item.get("file")):
        return None
    relative = item.get("relative")
    if not isinstance(relative, str):
        return None
    file_relative = _strict_ownership_relative(root, relative)
    if file_relative is None:
        return None
    created = item.get("created_directories")
    if not isinstance(created, list):
        return None
    seen = set()
    for record in created:
        if not _valid_ownership_identity(record):
            return None
        directory_relative = record.get("relative")
        if not isinstance(directory_relative, str):
            return None
        directory_relative = _strict_ownership_relative(
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
            or not _valid_ownership_identity(receipt.get("file"))
            or not isinstance(receipt.get("relative"), str)
        ):
            return None
        artwork_relative = _strict_ownership_relative(
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
        if not _valid_ownership_identity(record):
            return None
        relative = record.get("relative")
        if (not isinstance(relative, str)
                or _strict_ownership_relative(item["root"], relative) is None):
            return None
        created_directories.append(record)
    path = item["path"]
    owned = _bind_owned_path(
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
            directory_relative = _strict_ownership_relative(
                item["root"], record["relative"])
            if (
                directory_relative is not None
                and len(directory_relative.parts)
                < len(artwork_relative.parts)
                and artwork_relative.parts[:len(directory_relative.parts)]
                == directory_relative.parts
            ):
                artwork_created.append(record)
        companion = _bind_owned_path(
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
            or not _valid_ownership_identity(record.get("file"))
            or not isinstance(record.get("path"), str)
        ):
            return None
        companion_path = Path(os.path.abspath(record["path"]))
        if companion_path != Path(path).with_suffix(".lrc"):
            return None
        companion = _bind_owned_path(
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
    from qobuz_librarian import run_lock
    from qobuz_librarian.completion import normalise_album_id
    from qobuz_librarian.library.post_import_relocation import (
        PostImportRelocationAttention,
        acknowledge_post_import_relocation,
        seal_post_import_relocation_handoff,
    )
    from qobuz_librarian.web import job_persistence

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
                "The downloaded track's Undo record could not be saved durably."
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
    album_id = normalise_album_id(job.album_id)
    planned_album = queue_item.get("album")
    planned_album_id = normalise_album_id(
        planned_album.get("id") if isinstance(planned_album, dict) else None
    )
    single_album_id = normalise_album_id(
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
        handoff_hash = seal_post_import_relocation_handoff(
            operation_id,
            consumer=consumer,
            payload=single_snapshot,
            authority=authority,
        )
    except Exception as exc:
        _refresh_post_import_relocation_recovery(authority)
        restore_source(clear_preservation=True)
        raise PostImportRelocationAttention(
            "The relocated track's Undo handoff could not be sealed."
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
            "The relocated track's Undo record could not be saved durably."
        ) from persistence_error

    accept_destination(single_snapshot)
    queue_item.pop("_post_import_relocation_handoff_unknown", None)
    queue_item["_post_import_relocation_final_proven"] = True

    try:
        acknowledge_post_import_relocation(
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


def _make_single_track_run(album, track, token):
    """Run a single-track download: download just ``track`` via the per-track
    queue path (the same isolation repair uses, never a whole-album rip)."""
    from qobuz_librarian.api.auth import token_credential_generation

    expected_generation = token_credential_generation(token)

    def run(j):
        from qobuz_librarian.library import hidden as hidden_mod
        from qobuz_librarian.library.catalog import (
            album_year,
            compute_missing,
            find_existing_tracks,
            prompt_and_migrate_multi_artist_folder,
        )
        from qobuz_librarian.queue.builder import _build_queue_item
        from qobuz_librarian.queue.executor import (
            _advance_import_ownership_after_relocation,
            _execute_download_queue,
            _reunite_split_album,
        )
        from qobuz_librarian.ui_cli.errors import plural
        from qobuz_librarian.web import job_persistence
        from qobuz_librarian.web.flows import (
            _note_staging_wait,
            _refresh_after_local_album_change,
            build_args,
        )
        active = None
        active_token = token
        if getattr(token, "credential_generation", ""):
            active = _authorize_qobuz_live(
                QobuzAccess.DOWNLOAD_ACTION,
                expected_generation=expected_generation,
            )
            active_token = active.token
        args = build_args()
        artist = (album.get("artist") or {}).get("name") or "?"
        title = album.get("title") or "?"
        t_title = track.get("title") or "?"
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        existing, album_dir = find_existing_tracks(album)
        missing, _present = compute_missing(qobuz_tracks, existing)
        missing_ids = {str(t.get("id")) for t in missing}
        # Already own this exact track? Don't re-rip it; that just lands a beets
        # ".1.flac" duplicate beside the copy you have, and don't mark anything.
        if str(track.get("id")) not in missing_ids:
            j.summary = f"You already have “{t_title}”. Nothing downloaded."
            return
        qi = _build_queue_item(
            album=album, album_dir=album_dir,
            label=f"{artist}, {t_title}  [single]",
            missing=[track], present=existing,
            upgrade_only=False, auto_upgrade=False,
            force_track_by_track=True,
        )
        qi["_capture_import_ownership"] = True
        qi["_defer_post_import_relocation_handoff"] = True
        _note_staging_wait(j, "Downloading", 0, 1)
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
                _execute_download_queue([qi], args, active_token)
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
                        from qobuz_librarian import run_lock

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
                            migrated = _reunite_split_album(
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
                                migrated = prompt_and_migrate_multi_artist_folder(
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
                                _advance_import_ownership_after_relocation(
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
                                "The relocated track's recovery identity was lost."
                            )
                        else:
                            landed_dir = original_landed_dir
                    qi.pop("_post_import_relocation_pending", None)
            except BaseException:
                if (
                    qi.get("_post_import_relocation_pending") is True
                    or "_post_import_relocation_operation_id" in qi
                ):
                    from qobuz_librarian import run_lock

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
            if qi.get("n_fail"):
                j.error = f"{plural(qi.get('n_fail', 1), 'track')} failed"
            elif qi.get("n_ok"):
                j.error = "Downloaded, but the import failed. See job log."
            else:
                j.error = ("Couldn't retrieve the track. Qobuz may be rate-limiting "
                           "you, or it's unavailable. Try again shortly.")
            return
        # Only mark it a single when explicitly configured.
        marked = bool(cfg.SUPPRESS_SINGLE_TRACK_GAPS and len(missing) > 1)
        if marked:
            try:
                hidden_mod.mark_single(artist, title, album_year(album),
                                       album.get("id"))
                j.summary = (f"Got “{t_title}”, filed under {artist} / {title}. "
                             "The rest of the album stays out of scans.")
            except OSError as e:
                # The track landed fine, so don't fail the job, but don't claim
                # the exclusion stuck either.
                marked = False
                j.summary = (f"Got “{t_title}”, filed under {artist} / {title}.")
                j.error = (f"{e} The rest of the album may still show "
                           "in scans.")
        elif len(missing) > 1:
            hidden_mod.unmark_single(
                artist,
                title,
                year=album_year(album),
                album_id=album.get("id"),
            )
            j.summary = (f"Got “{t_title}”, filed under {artist} / {title}. "
                         "Future scans can still offer the rest of the album.")
        else:
            # This download completed the album, so it's a normal full album
            # now, so clear any single mark an earlier partial download left
            # behind.
            hidden_mod.unmark_single(
                artist,
                title,
                year=album_year(album),
                album_id=album.get("id"),
            )
            j.summary = (f"Got “{t_title}”; that completed {title}, so it's "
                         "filed as a full album.")
            # Complete means any parked Gap Fill candidate for it is stale.
            from qobuz_librarian.web.flows import prune_library_review_candidates
            prune_library_review_candidates(album)
        with j._lock:
            j.single["marked"] = marked
        if owned_path is None:
            j.summary += (
                " Undo isn't available because the downloaded file "
                "couldn't be verified."
            )
        job_persistence.persist(j)
        _refresh_after_local_album_change(
            album,
            {"dir": landed_dir},
            fallback_artist=artist,
            token=active_token,
            args=args,
            upgrade=True,
            downsample=True,
        )
    return run


@app.post("/download", response_class=HTMLResponse)
async def queue_download(request: Request, album_id: str = Form(""),
                         as_new_edition: str = Form(""),
                         track_id: str = Form("")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    album_id = album_id.strip()
    track_id = track_id.strip()
    if not album_id:
        msg = "Missing album id."
        if _is_htmx(request):
            # 200, not 400: htmx only swaps 2xx/3xx responses, so a 400
            # fragment is silently dropped and the user sees no feedback.
            return _download_fragment("error", html.escape(msg), "failed")
        return RedirectResponse(url="/queue?error=" + urllib.parse.quote(msg),
                                status_code=303)
    # "Get this edition too": download a different edition of an album the
    # user already owns, as a separate album.
    download_as_new_edition = str(as_new_edition).strip().lower() in (
        "1", "true", "yes", "on")
    # Refuse true duplicates (same album already active or pending), but only
    # of the SAME intent (see _duplicate_download_job).
    existing = _duplicate_download_job(album_id, track_id, download_as_new_edition)
    if existing:
        if _is_htmx(request):
            return _download_fragment(
                "warning",
                f'Already queued. <a href="/jobs/{existing.id}" '
                f'class="ql-inline-link">view job</a>.',
                "duplicate",
            )
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    try:
        credentials = await _authorize_qobuz_for_web(
            QobuzAccess.DOWNLOAD_ACTION
        )
        token = credentials.token
        from qobuz_librarian.api.client import call_within
        from qobuz_librarian.api.search import get_album
        loop = asyncio.get_running_loop()
        album = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_within(cfg.WEB_FETCH_TIMEOUT, get_album, album_id, token)),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        if download_as_new_edition and await loop.run_in_executor(
            None, lambda: _same_edition_is_complete(album)
        ):
            msg = "This edition is already in your library."
            if _is_htmx(request):
                return _download_fragment(
                    "warning", html.escape(msg), "owned")
            return RedirectResponse(
                url="/queue?error=" + urllib.parse.quote(msg), status_code=303)
        if not download_as_new_edition and track_id:
            # A single track was excluded from the guard entirely, so nothing
            # checked whether that track was already on disk before fetching it
            # again. Ask about the one track, not the whole album.
            def _track_already_there():
                from qobuz_librarian.library.catalog import (
                    compute_missing,
                    find_album_dir_filesystem,
                    find_existing_tracks,
                )
                try:
                    album_dir = find_album_dir_filesystem(album)
                except Exception:
                    return False
                if album_dir is None:
                    return False
                try:
                    existing_tracks, _ = find_existing_tracks(album, album_dir=album_dir)
                except Exception:
                    return False
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                if not (existing_tracks and qobuz_tracks):
                    return False
                _missing, present = compute_missing(qobuz_tracks, existing_tracks)
                return any(str(t.get("id") or "") == track_id for t in present)

            if await loop.run_in_executor(None, _track_already_there):
                msg = "That track is already in your library."
                if _is_htmx(request):
                    return _download_fragment(
                        "warning", html.escape(msg), "owned")
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(msg), status_code=303)

        if not download_as_new_edition and not track_id:
            def _already_complete():
                from qobuz_librarian.library.catalog import (
                    compute_missing,
                    find_album_dir_filesystem,
                    find_existing_tracks,
                )
                try:
                    album_dir = find_album_dir_filesystem(album)
                except Exception:
                    return False
                if album_dir is None:
                    return False
                try:
                    # Already resolved above; pass it through so we don't repeat
                    # the cached-subdir scan + fuzzy fallback for the same album.
                    existing_tracks, _ = find_existing_tracks(album, album_dir=album_dir)
                except Exception:
                    existing_tracks = []
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                # Only count it complete when nothing's missing.
                return bool(existing_tracks and qobuz_tracks) and not (
                    compute_missing(qobuz_tracks, existing_tracks)[0])

            # Resolving the album folder walks the (often NAS-mounted) library,
            # so keep it off the event loop; otherwise a large library stalls
            # every other request while this one request blocks.
            if await loop.run_in_executor(None, _already_complete):
                msg = "This album is already in your library."
                if _is_htmx(request):
                    # Offer the deliberate second-edition path instead of a
                    # dead end: a remaster or a different mix can be kept
                    # alongside the owned copy (it imports into its own (year)
                    # folder).
                    aid = html.escape(album_id)
                    return HTMLResponse(
                        f'<div class="ql-download-choice">'
                        f'<div class="ql-download-choice-copy">'
                        f'<p>{html.escape(msg)}</p>'
                        f'<span>A remaster or different mix downloads into its '
                        f'own folder, kept alongside the existing library copy; '
                        f'same-year editions may merge in your player.</span></div>'
                        f'<form hx-post="/download" hx-target="#download-toast" '
                        f'hx-swap="innerHTML">'
                        f'<input type="hidden" name="album_id" value="{aid}">'
                        f'<input type="hidden" name="as_new_edition" value="1">'
                        f'<button type="submit" class="ql-btn ql-btn-primary ql-btn-sm '
                        f'w-full sm:w-auto whitespace-nowrap">'
                        f'Download this edition anyway</button></form></div>',
                        headers={"X-QL-Download-Outcome": "owned"},
                    )
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(msg),
                    status_code=303)
        title  = album.get("title") or "?"
        artist = (album.get("artist") or {}).get("name") or "?"
        single_track = None
        if track_id:
            _tracks = (album.get("tracks") or {}).get("items") or []
            single_track = next(
                (t for t in _tracks if str(t.get("id")) == track_id), None)
            if single_track is None:
                msg = "That track isn't on this album."
                if _is_htmx(request):
                    # 200, not 400: htmx drops non-2xx/3xx fragments, so a 400
                    # here renders nothing. The notice conveys the failure.
                    return _download_fragment(
                        "error", html.escape(msg), "failed")
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(msg), status_code=303)
        job = job_mgr.Job(
            title=(single_track.get("title") or title) if single_track else title,
            artist=artist,
            album_id=album_id,
            edition=str(
                (
                    (single_track.get("version") if single_track else None)
                    or album.get("version")
                    or ""
                )
            ).strip(),
        )
        if single_track:
            # Flagging it now (before the run fills in the undo details) is what
            # tells the UI to hide Cancel on this job: a one-track download is done
            # before you could catch it.
            job.single = {"album_id": album_id, "track_id": str(track_id)}
        if download_as_new_edition:
            # Retry rebuilds the run from the persisted job, so the edition
            # override has to live on the job. Closure-only, a retried "get
            # this edition too" would fall back to the owned-album skip.
            job.execute_args = {"new_edition": True}

        # Re-check under the lock right before submitting: closes the race with
        # a concurrent /download for the same album across the get_album await.
        with _DOWNLOAD_SUBMIT_LOCK, _CREDENTIAL_LOCK:
            dup = _duplicate_download_job(album_id, track_id, download_as_new_edition)
            if dup:
                if _is_htmx(request):
                    return _download_fragment(
                        "warning",
                        f'Already queued. <a href="/jobs/{dup.id}" '
                        f'class="ql-inline-link">view job</a>.',
                        "duplicate",
                    )
                return RedirectResponse(url=f"/jobs/{dup.id}", status_code=303)
            # Re-check the run-lock right before submitting.
            busy = _lock_busy_response(request)
            if busy is not None:
                return busy
            if not _credential_generation_is_active(credentials.generation):
                raise CredentialChanged(
                    "Qobuz credentials changed before the download was queued."
                )
            run_fn = (_make_single_track_run(album, single_track, token)
                      if single_track
                      else _make_download_run(
                          album, token, treat_as_new=download_as_new_edition))
            if job_mgr.submit(job, run_fn) is None:
                return _job_admission_response(request)
        if _is_htmx(request):
            response = _tr(request, "_job_queued.html", {"job": job})
            response.headers["X-QL-Download-Outcome"] = "queued"
            return response
        # Land on the new job's page so the user sees their download starting.
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    except NoCredsError as exc:
        msg = _qobuz_action_error_message(exc, unchanged=True)
        if _is_htmx(request):
            return _download_fragment("error", html.escape(msg), "failed")
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    except Exception as e:
        user_msg = _download_error_message(
            e,
            "Couldn't queue download. Try again.",
        )
        if _is_htmx(request):
            return _download_fragment(
                "error", html.escape(user_msg), "failed")
        msg = urllib.parse.quote(user_msg, safe="")
        return RedirectResponse(url=f"/queue?error={msg}", status_code=303)


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
    if _census_cache is not None and now - _census_cache[0] < _CENSUS_TTL:
        return _census_cache[1]
    from qobuz_librarian.library import flac_cache
    from qobuz_librarian.ui_cli.colors import format_size
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
    _census_cache = (now, view)
    return view


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request, page: int = 1, tab: str = ""):
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import scan_checkpoint
    from qobuz_librarian.web import review_badges
    badge_generation = review_badges.ready_generation("library")
    creds_ok = bool(_read_creds().get("auth_token"))
    from qobuz_librarian.library import generation_state, new_releases
    notice_bits = []
    _skipped = request.query_params.get("skipped", "")
    if _skipped.isdigit() and int(_skipped):
        n = int(_skipped)
        notice_bits.append(
            f"{n} album{'s' if n != 1 else ''} already in your library, skipped.")
    if request.query_params.get("noselection"):
        notice_bits.append("Nothing else is selected on that tab."
                           if notice_bits else
                           "Nothing is selected on that tab yet.")
    elif request.query_params.get("approved"):
        notice_bits.append("Download started. It's running in the queue.")
    notice = " ".join(notice_bits)
    library_generation = _truthful_library_generation()
    ctx = {
        "creds_ok": creds_ok, "qobuz_ready": _qobuz_ready(), "page": "library",
        "library_scan_state": _library_scan_state(),
        "library_notice": notice,
        "error": request.query_params.get("error", ""),
        # Freshness line: when a full gap scan last completed, and whether one
        # ever has (the new-release baseline is only seeded by a clean finish).
        "last_full_scan": _last_scan_age(),
        "baseline_complete": generation_state.baseline_complete(
            library_generation
        ),
        "library_baseline_exists": (
            generation_state.library_snapshot_available(library_generation)
        ),
        "new_release_baseline_complete": new_releases.is_baseline_complete(),
        "library_generation": library_generation,
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_MISSING),
        # Why a finished review retired ("discarded" / "worked_through" / ""),
        # so the finished-state card reads right.
        "library_review_retired_reason": "",
        "JobStatus": job_mgr.JobStatus,
        # Drives the header's quiet refresh: hidden while a crawl is already
        # under way (the "Refreshing…" note takes its place over a parked
        # review; a bare scan shows its own progress body).
        "library_refresh_running": _active_scan(
            "library", statuses=("pending", "scanning", "running")) is not None,
    }
    # Single-surface rule (same as /repair): a scan in flight or a parked
    # review renders inline right here, so results never hide behind the
    # launcher and never live under the Queue nav.
    ljob = _library_current_job()
    if ljob is None and ctx["library_baseline_exists"]:
        # No live job holds the surface, but the baseline is complete, so rebuild
        # the parked review from saved scan state so post-baseline ALWAYS shows
        # the Missing Albums / Gap Fill tabs, never "Baseline ready" with none.
        # Off the loop: a large library builds thousands of candidate dicts.
        loop = asyncio.get_running_loop()
        ljob = await loop.run_in_executor(None, _review_job_from_library_state)
    ctx["library_job"] = ljob
    ctx["census"] = None
    if ljob is not None:
        ctx["queue_wait"] = _queue_wait(ljob)
        # A full load has to be able to land on either tab: the address is the
        # only thing a reload or a bookmark still carries.
        ctx.update(_review_context(ljob, page, tab=tab))
        ctx["library_resume"] = None
    else:
        # Resume hint: only when an interrupted baseline checkpoint exists and
        # nothing is running above.
        latest_status = str(
            (ctx["library_generation"].get("latest_attempt") or {}).get(
                "status"
            )
            or "never"
        )
        cp = (
            scan_checkpoint.pending()
            if (
                not int(ctx["library_generation"].get("generation") or 0)
                or latest_status in {"running", "failed", "incomplete"}
            )
            else None
        )
        ctx["library_resume"] = cp if cp is not None else None
        # Finished-state copy: the "Review complete" vs "Review discarded"
        # card keys off why the review retired.
        if ctx["library_baseline_exists"]:
            from qobuz_librarian.library import library_scan_state
            ctx["library_review_retired_reason"] = (
                library_scan_state.load().get("review_retired_reason") or "")
    # The census renders whenever the page is calm (no job, or a parked
    # review below it), so it doesn't blink in and out with review state.
    if ctx["library_baseline_exists"] and (
            ljob is None or ljob.status == job_mgr.JobStatus.AWAITING_REVIEW):
        loop = asyncio.get_running_loop()
        ctx["census"] = await loop.run_in_executor(None, _census_view)
    badge_ack = None
    if (ljob is not None
            and ljob.status == job_mgr.JobStatus.AWAITING_REVIEW
            and (ctx.get("review_counts") or {}).get("total")):
        badge_ack = ("library", badge_generation)
    return _tr(request, "library.html", ctx, review_badge_ack=badge_ack)


@app.get("/library/refresh-note", response_class=HTMLResponse)
async def library_refresh_note(request: Request):
    """Fragment behind the header's refresh control. The "Refreshing…" note
    polls this while a refresh runs, so it swaps back to the idle icon when
    the scan ends instead of sitting there forever; the idle icon itself
    never polls."""
    from qobuz_librarian.library import generation_state, new_releases
    loop = asyncio.get_running_loop()
    def _context():
        library_generation = generation_state.load()
        return {
            "qobuz_ready": _qobuz_ready(),
            "baseline_complete": generation_state.baseline_complete(
                library_generation
            ),
            "library_baseline_exists": (
                generation_state.library_snapshot_available(
                    library_generation
                )
            ),
            "new_release_baseline_complete": (
                new_releases.is_baseline_complete()
            ),
            "library_generation": library_generation,
            "library_scan_state": _library_scan_state(),
            "library_job": _library_current_job(),
            "JobStatus": job_mgr.JobStatus,
            "library_refresh_running": _active_scan(
                "library",
                statuses=("pending", "scanning", "running"),
            ) is not None,
        }

    ctx = await loop.run_in_executor(None, _context)
    return _tr(request, "_library_refresh.html", ctx)


@app.post("/library")
async def library_scan(
    request: Request,
    mode: str = Form("missing_albums"),
    force_full: str = Form(""),
):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    mode_norm = (mode or "").strip().lower()
    # Run the submit off the event loop: it takes _auto_check_lock, which the
    # dashboard auto-triggers can hold across small data-volume reads, and the
    # loop shouldn't block on a (possibly NAS) mount, same reason the dashboard
    # does its disk work in an executor.
    loop = asyncio.get_running_loop()
    if mode_norm == "new_releases":
        # A new-release check compares the catalog against the baseline a completed
        # library scan builds; with no baseline there's nothing to compare against,
        # so it would crawl every artist, surface nothing, and (the old bug) flip
        # the baseline "done", stranding an interrupted library scan's resume.
        # Refuse and point at a library scan instead of running that empty crawl.
        from qobuz_librarian.library import new_releases as _nr
        if not _nr.is_baseline_complete():
            msg = "Run a full library scan first."
            if _is_htmx(request):
                return HTMLResponse(
                    f'<div class="ql-flash ql-flash-warning" data-flash><span>{html.escape(msg)}</span></div>',
                    status_code=200)
            return RedirectResponse(
                url="/library?error=" + urllib.parse.quote(msg), status_code=303)
        existing = _existing_new_release_check()
        if existing is not None:
            return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
        try:
            credentials = await _authorize_qobuz_for_web(
                QobuzAccess.CATALOGUE_ACTION
            )
        except (
            NoCredsError,
            AuthLost,
            QobuzUnavailable,
            QobuzEntitlementError,
            CredentialChanged,
            asyncio.TimeoutError,
        ) as exc:
            msg = _qobuz_action_error_message(exc, unchanged=True)
            if _is_htmx(request):
                return HTMLResponse(
                    _ql_notice_html("error", html.escape(msg)),
                    status_code=200,
                )
            return RedirectResponse(
                url="/library?error=" + urllib.parse.quote(msg),
                status_code=303,
            )
        # Same job the dashboard auto-check submits; its own execute_kind so
        # the review screen badges the new releases (left un-ticked).
        job = await loop.run_in_executor(
            None,
            lambda: _start_new_release_check(credentials),
        )
        if job is None:
            return _scan_submission_failure_response(request, "/library")
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    scan_state = _library_scan_state()
    if not scan_state["ready"]:
        msg = scan_state["message"]
        if _is_htmx(request):
            return HTMLResponse(
                f'<div class="ql-flash ql-flash-warning" data-flash><span>{html.escape(msg)}</span></div>',
                status_code=200)
        return RedirectResponse(
            url="/library?error=" + urllib.parse.quote(msg), status_code=303)
    existing = _active_library_scan()
    if existing is not None:
        return RedirectResponse(url="/library", status_code=303)
    try:
        credentials = await _authorize_qobuz_for_web(
            QobuzAccess.CATALOGUE_ACTION
        )
    except (
        NoCredsError,
        AuthLost,
        QobuzUnavailable,
        QobuzEntitlementError,
        CredentialChanged,
        asyncio.TimeoutError,
    ) as exc:
        msg = _qobuz_action_error_message(exc, unchanged=True)
        if _is_htmx(request):
            return HTMLResponse(
                _ql_notice_html("error", html.escape(msg)),
                status_code=200,
            )
        return RedirectResponse(
            url="/library?error=" + urllib.parse.quote(msg), status_code=303)
    # "library" (not "album") so the review screen knows this is the paced triage
    # surface; both modes run the same album executor and resume from a matching
    # checkpoint if one's waiting (see _start_library_scan / scan_library).
    force_full_scan = str(force_full or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    job = await loop.run_in_executor(
        None,
        lambda: _start_library_scan(
            credentials,
            partial_only=(mode_norm == "partial_fill"),
            force_full=force_full_scan,
        ),
    )
    if job is None:
        return _scan_submission_failure_response(request, "/library")
    # Land back on /library, where the scan is watched and reviewed right here.
    return RedirectResponse(url="/library", status_code=303)


@app.post("/library/skip-setup")
async def skip_baseline_setup(request: Request):
    """Dismiss the first-run baseline-scan offer on the dashboard. The scan stays
    available any time from the Library page; this just stops the dashboard from
    offering it on every load."""
    from qobuz_librarian.library import new_releases
    new_releases.note_auto_scan_attempted()
    return RedirectResponse(url="/", status_code=303)


def _hidden_view(request, scope, *, page, restore_action, back_url,
                 restore_all_action=None):
    from qobuz_librarian.library import hidden as hidden_mod
    groups = hidden_mod.hidden_by_artist(scope)
    total_entries = sum(len(g["albums"]) for g in groups)

    q = (request.query_params.get("q") or "").strip()[:200]
    if q:
        needle = q.lower()
        matched = []
        for g in groups:
            if needle in g["artist"].lower():
                matched.append(g)
                continue
            albums = [a for a in g["albums"]
                      if needle in a["title"].lower()
                      or any(needle in t.lower() for t in a["others"])]
            if albums:
                matched.append({
                    "artist": g["artist"], "albums": albums,
                    "rows": sum(1 + len(a["others"]) for a in albums)})
        groups = matched

    # Whole artists per page, same budgets as the review pages; this page
    # once shipped its entire set as one 639 KB document.
    pages = []
    cur, cur_rows = [], 0
    for g in groups:
        if cur and (len(cur) >= REVIEW_PAGE_ARTISTS
                    or cur_rows + g["rows"] > REVIEW_PAGE_CANDIDATES):
            pages.append(cur)
            cur, cur_rows = [], 0
        cur.append(g)
        cur_rows += g["rows"]
    if cur:
        pages.append(cur)
    n_pages = max(1, len(pages))
    try:
        pg = int(request.query_params.get("p") or 1)
    except ValueError:
        pg = 1
    pg = max(1, min(pg, n_pages))

    return _tr(request, "hidden.html", {
        "page": page, "scope": scope, "back_url": back_url,
        "restore_action": restore_action,
        "restore_all_action": restore_all_action,
        "restore_all_count": total_entries,
        "notice": request.query_params.get("notice", ""),
        "hidden_q": q,
        "hidden_total_artists": len(groups),
        "hidden_page": pg, "hidden_pages": n_pages,
        "groups": pages[pg - 1] if pages else []})


async def _restore_hidden_all(request, scope, dest, what):
    """Scope-wide Bring all back for the Dismissed pages. The library scope
    has its own richer endpoint (it also lifts a retired review); this covers
    the Upgrade and Downsample scopes, whose reviews re-derive from saved
    state at read time."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import hidden as hidden_mod
    loop = asyncio.get_running_loop()
    try:
        changed = await loop.run_in_executor(
            None, lambda: hidden_mod.restore_all(scope))
    except OSError as e:
        return RedirectResponse(
            url=dest + "?notice=" + urllib.parse.quote(str(e)),
            status_code=303)
    msg = f"Brought every {what} back." if changed else "Nothing to bring back."
    return RedirectResponse(
        url=dest + "?notice=" + urllib.parse.quote(msg), status_code=303)


async def _restore_hidden(request, scope, redirect):
    # Mutates the hidden store, so it honours the run-lock like every other
    # state-changing POST: a restore mustn't race a CLI run or another job.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import hidden as hidden_mod
    form = await request.form()
    artists = form.getlist("artist")[:10000]
    fingerprints = form.getlist("fingerprint")[:10000]
    try:
        if artists:
            hidden_mod.restore(scope, artists)
        if fingerprints:
            hidden_mod.restore_albums(scope, fingerprints)
    except OSError as e:
        # Store write failed; nothing was restored; say so instead of
        # rendering the rows gone until the next reload.
        return RedirectResponse(
            url=redirect + "?notice=" + urllib.parse.quote(str(e)),
            status_code=303)
    if scope == hidden_mod.SCOPE_MISSING and (artists or fingerprints):
        # Upgrade/Downsample re-derive their reviews from saved state at read
        # time, so restore takes effect there on its own.
        from qobuz_librarian.library import library_scan_state
        from qobuz_librarian.web import flows
        loop = asyncio.get_running_loop()
        rejoined = await loop.run_in_executor(
            None, lambda: flows.refold_restored_missing(artists, fingerprints))
        if rejoined is False:
            msg = ("Restored, but the open Library review couldn't be saved. "
                   "Check the data folder, then refresh the review.")
        elif rejoined is None:
            # No live parked review to fold into.
            lifted = await loop.run_in_executor(
                None, library_scan_state.clear_review_retired)
            msg = ("Restored, back in the Library review." if lifted
                   else "Restored. They return the next time the library scans.")
        elif rejoined:
            msg = (f"Restored {rejoined}, back in the Library review."
                   if rejoined != 1 else "Restored, back in the Library review.")
        else:
            msg = "Restored. Nothing needs adding to the Library review."
        return RedirectResponse(
            url=redirect + "?notice=" + urllib.parse.quote(msg),
            status_code=303)
    return RedirectResponse(url=redirect, status_code=303)


@app.get("/library/hidden", response_class=HTMLResponse)
async def library_hidden(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_MISSING, page="library",
                        restore_action="/library/hidden/restore", back_url="/library",
                        restore_all_action="/library/hidden/restore-all")


@app.post("/library/hidden/restore")
async def library_hidden_restore(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_MISSING, "/library/hidden")


@app.post("/library/hidden/restore-all")
async def library_hidden_restore_all(request: Request):
    """Bring the whole dismissed set back from the Dismissed page. Unlike the
    finished-state /library/bring-back-all this can run with a live review
    parked, so the restored candidates are folded back into it, clearing the
    store alone would leave them invisible until a future scan most users
    never run."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows
    loop = asyncio.get_running_loop()

    try:
        artists = await loop.run_in_executor(
            None, lambda: hidden_mod.take_all(hidden_mod.SCOPE_MISSING))
    except OSError as e:
        return RedirectResponse(
            url="/library/hidden?notice=" + urllib.parse.quote(str(e)),
            status_code=303)
    if not artists:
        return RedirectResponse(
            url="/library/hidden?notice=" + urllib.parse.quote(
                "Nothing to bring back."), status_code=303)
    rejoined = await loop.run_in_executor(
        None, lambda: flows.refold_restored_missing(artists, []))
    if rejoined is False:
        msg = ("Brought everything back, but the open Library review couldn't "
               "be saved. Check the data folder, then refresh the review.")
    elif rejoined is None:
        lifted = await loop.run_in_executor(
            None, library_scan_state.clear_review_retired)
        msg = ("Brought everything back to the Library review." if lifted
               else "Brought everything back. It returns the next time the "
                    "library scans.")
    elif rejoined:
        msg = "Brought everything back to the Library review."
    else:
        msg = ("Brought everything back. Nothing needs adding to the "
               "Library review.")
    return RedirectResponse(
        url="/library/hidden?notice=" + urllib.parse.quote(msg),
        status_code=303)


@app.post("/library/bring-back-all")
async def library_bring_back_all(request: Request):
    """Finished-state 'Bring all back': un-hide every dismissed missing/gap
    album and lift a retired review, so the whole set returns. The /library
    reload rebuilds the review from saved state (albums since downloaded are
    dropped as owned), which is why nothing needs folding here; the finished
    state is only reachable with no live review parked."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    loop = asyncio.get_running_loop()

    def _bring_back():
        restored = hidden_mod.restore_all(hidden_mod.SCOPE_MISSING)
        lifted = library_scan_state.clear_review_retired()
        return restored or lifted

    try:
        changed = await loop.run_in_executor(None, _bring_back)
    except OSError as e:
        return RedirectResponse(
            url="/library?notice=" + urllib.parse.quote(str(e)),
            status_code=303)
    msg = ("Brought your dismissed results back to the Library review."
           if changed else "Nothing to bring back.")
    return RedirectResponse(
        url="/library?notice=" + urllib.parse.quote(msg), status_code=303)


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    creds_ok = bool(_read_creds().get("auth_token"))
    # Without credentials the page still renders, showing the connect card,
    # bouncing to Search reads as a broken button.
    if not getattr(cfg, "UPGRADE_SCAN_ENABLED", True):
        return _upgrade_unavailable_response()
    state = _upgrade_state_summary()
    return _tr(request, "upgrade.html", {
        "creds_ok": creds_ok, "qobuz_ready": _qobuz_ready(), "page": "upgrade",
        "upgrade_state": state,
        "last_run": _tool_last_run_age("library"),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_UPGRADE)})


@app.get("/upgrade/hidden", response_class=HTMLResponse)
async def upgrade_hidden(request: Request):
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_UPGRADE, page="upgrade",
                        restore_action="/upgrade/hidden/restore", back_url="/upgrade",
                        restore_all_action="/upgrade/hidden/restore-all")


@app.post("/upgrade/hidden/restore")
async def upgrade_hidden_restore(request: Request):
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_UPGRADE, "/upgrade/hidden")


@app.post("/upgrade/hidden/restore-all")
async def upgrade_hidden_restore_all(request: Request):
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden_all(
        request, hidden_mod.SCOPE_UPGRADE, "/upgrade/hidden",
        "dismissed album")


@app.post("/upgrade/review")
async def upgrade_review(request: Request):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    if not _upgrade_available():
        return _upgrade_unavailable_response()
    loop = asyncio.get_running_loop()
    job = await loop.run_in_executor(
        None, lambda: _review_job_from_current_saved_state("upgrade"))
    if job is None:
        return RedirectResponse(url="/upgrade", status_code=303)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/downsample", response_class=HTMLResponse)
async def downsample_page(request: Request):
    from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE
    from qobuz_librarian.library import hidden as hidden_mod
    state = _downsample_state_summary()
    # A fresh scan supersedes the parked review (see _submit_scan_deduped), so
    # the Refresh confirm must say so instead of quietly dropping the user's
    # ticks.
    review_parked = any(
        getattr(j, "execute_kind", "") == "downsample"
        for j in job_mgr.registry.awaiting_review())
    return _tr(request, "downsample.html", {
        "page": "downsample",
        "have_downsample": HAVE_DOWNSAMPLE,
        "creds_ok": bool(_read_creds().get("auth_token")),
        "downsample_state": state,
        "review_parked": review_parked,
        # A standalone refresh in flight, so the page shows "scan running"
        # instead of the idle launcher (which read as if nothing was happening).
        "downsample_running": _active_scan(
            "downsample", statuses=("pending", "scanning", "running")),
        "last_run": _tool_last_run_age("downsample"),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_DOWNSAMPLE)})


@app.get("/downsample/hidden", response_class=HTMLResponse)
async def downsample_hidden(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_DOWNSAMPLE, page="downsample",
                        restore_action="/downsample/hidden/restore",
                        back_url="/downsample",
                        restore_all_action="/downsample/hidden/restore-all")


@app.post("/downsample/hidden/restore")
async def downsample_hidden_restore(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_DOWNSAMPLE,
                                 "/downsample/hidden")


@app.post("/downsample/hidden/restore-all")
async def downsample_hidden_restore_all(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden_all(
        request, hidden_mod.SCOPE_DOWNSAMPLE, "/downsample/hidden",
        "album kept hi-res")


@app.post("/downsample/review")
async def downsample_review(request: Request):
    # No credential check: downsampling only reads and rewrites local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    loop = asyncio.get_running_loop()
    job = await loop.run_in_executor(
        None, lambda: _review_job_from_current_saved_state("downsample"))
    if job is None:
        return RedirectResponse(url="/downsample", status_code=303)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/downsample")
async def downsample_scan(request: Request):
    # No credential check: downsampling only reads and rewrites local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Downsample scan")
    job.execute_kind = "downsample"
    job.review_verb = "Downsample"  # the action rewrites files, not a download
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_downsamples(j),
        lambda j, chosen: flows.execute_downsamples(
            j,
            chosen,
            token=_get_optional_token(),
            keep_originals=_job_downsample_keep_originals(j),
        ),
        "downsample")
    if job is None:
        return _scan_submission_failure_response(request, "/downsample")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/repair", response_class=HTMLResponse)
async def repair_page(request: Request, page: int = 1):
    from qobuz_librarian.library import scan_checkpoint
    from qobuz_librarian.web import review_badges
    badge_generation = review_badges.ready_generation("repair")
    creds_ok = bool(_read_creds().get("auth_token"))
    # /repair is the SINGLE authoritative repair surface.
    rjob = _repair_current_job()
    ctx = {"creds_ok": creds_ok, "qobuz_ready": _qobuz_ready(),
           "page": "repair", "repair_job": rjob,
           "error": request.query_params.get("error", ""),
           "JobStatus": job_mgr.JobStatus}
    if rjob is not None:
        ctx["queue_wait"] = _queue_wait(rjob)
        ctx.update(_review_context(rjob, page))
    # The launcher renders when the surface is idle AND under a run that failed
    # or was cancelled (see _repair_current_job, which keeps those on the page
    # on purpose). Both cases read these, so both have to be given them: when
    # only the idle branch set them, a failed run showed its own finish time
    # above the words "No repair scan has finished yet", and an interrupted
    # sweep was never offered its resume.
    if rjob is None or rjob.status in (job_mgr.JobStatus.FAILED,
                                       job_mgr.JobStatus.CANCELED):
        # Offer a resume only for a genuinely interrupted sweep (a stale
        # checkpoint), not one left by a run that's still active above.
        cp = scan_checkpoint.load("repair")
        if cp is None:
            ctx["repair_resume"] = None
        else:
            bundles = cp.get("artists") or {}
            found = sum(
                len(value.get("candidates") or [])
                for value in bundles.values()
                if isinstance(value, dict)
            )
            ctx["repair_resume"] = {
                "saved": len(bundles),
                "found": found,
            }
        ctx["last_run"] = _tool_last_run_age("repair")
    badge_ack = None
    if (rjob is not None
            and rjob.status == job_mgr.JobStatus.AWAITING_REVIEW
            and (ctx.get("review_counts") or {}).get("total")):
        badge_ack = ("repair", badge_generation)
    return _tr(request, "repair.html", ctx, review_badge_ack=badge_ack)


@app.post("/repair")
async def repair_scan(request: Request):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        credentials = await _authorize_qobuz_for_web(
            QobuzAccess.CATALOGUE_ACTION
        )
    except (
        NoCredsError,
        AuthLost,
        QobuzUnavailable,
        QobuzEntitlementError,
        CredentialChanged,
        asyncio.TimeoutError,
    ) as exc:
        msg = _qobuz_action_error_message(exc, unchanged=True)
        return RedirectResponse(
            url="/repair?error=" + urllib.parse.quote(msg), status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Repair scan")
    job.execute_kind = "repair"
    job.review_verb = "Repair"  # the action refills damaged tracks, not a download
    def _scan(j):
        active = _authorize_qobuz_live(
            QobuzAccess.CATALOGUE_ACTION,
            expected_generation=credentials.generation,
        )
        flows.scan_repairs(j, active.token)

    job = await _submit_scan_deduped_async(
        job,
        _scan,
        _resume_repair(job, job.execute_args),
        "repair")
    if job is None:
        return _scan_submission_failure_response(request, "/repair")
    # Land back on /repair so the sweep is watched live right here; its card
    # streams each flagged album inline (and explains the wait if it's queued
    # behind another scan).
    return RedirectResponse(url="/repair", status_code=303)


@app.get("/repair/history", response_class=HTMLResponse)
async def repair_history(request: Request):
    """Show what Repair has refilled in place, so the user knows which albums
    to refresh on an offline-sync client that may still serve the old broken
    file. The log itself is append-only on disk (DATA_DIR); this is read-only."""
    from qobuz_librarian.repair_log import read_repair_log_entries
    # Walks lines on the data volume, so offload to match the dashboard's pattern
    # and keep the event loop free if the file is sizable.
    loop = asyncio.get_running_loop()
    entries = await loop.run_in_executor(
        None, lambda: read_repair_log_entries(limit=500))
    return _tr(request, "repair_history.html",
               {"page": "repair", "entries": entries})


@app.get("/lyrics", response_class=HTMLResponse)
async def lyrics_page(request: Request):
    from qobuz_librarian.integrations.lyric_fetch import AVAILABLE
    providers = ", ".join(cfg.LYRICS_PROVIDERS) or "Lrclib, NetEase, Musixmatch"
    lyrics_format = (cfg.LYRICS_FORMAT or "embed").lower()
    lyrics_format_label = {
        "embed": "Embedded tags",
        "sidecar": ".lrc sidecar files",
        "both": "Embedded tags and .lrc files",
    }.get(lyrics_format, lyrics_format)
    latest_lyrics = None
    for job in job_mgr.registry.finished():
        if (getattr(job, "execute_kind", "") == "lyrics"
                and (latest_lyrics is None
                     or (job.finished_at or job.created_at or 0)
                     >= (latest_lyrics.finished_at
                         or latest_lyrics.created_at or 0))):
            latest_lyrics = job
    lyrics_failed = (
        latest_lyrics
        if latest_lyrics is not None
        and latest_lyrics.status == job_mgr.JobStatus.FAILED
        else None
    )
    return _tr(request, "lyrics.html", {
        "page": "lyrics",
        "have_lyrics": AVAILABLE,
        "creds_ok": bool(_read_creds().get("auth_token")),
        "last_run": _tool_last_run_age("lyrics"),
        # A library-wide lyrics scan in flight, so the page says so instead of
        # showing the idle "Ready · Start lyrics scan" launcher while one runs.
        "lyrics_running": _active_scan(
            "lyrics", statuses=("pending", "running")),
        "lyrics_failed": lyrics_failed,
        "lyrics_format": lyrics_format_label,
        "providers": providers,
    })


@app.post("/lyrics")
async def lyrics_scan(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    form = await request.form()
    rescan = bool(form.get("rescan"))
    synced_only = bool(form.get("synced_only"))
    # Re-check after the form await: set_mode can flip to CLI mode inside that
    # yield, and everything from here to submit runs without yielding, so this
    # read-and-submit is atomic against the on-loop mode flip (same pattern as
    # queue_download).
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    existing = _active_scan("lyrics", statuses=("pending", "running"))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyrics scan")
    job.execute_kind = "lyrics"
    submitted = job_mgr.submit(
        job,
        lambda j: flows.run_library_lyrics(j, rescan=rescan, synced_only=synced_only),
    )
    if submitted is None:
        return _scan_submission_failure_response(request, "/lyrics")
    return RedirectResponse(url=f"/jobs/{submitted.id}", status_code=303)


def _migrate_checks(src, dest):
    import os

    from qobuz_librarian.library.migrate import _existing_ancestor
    checks = []
    for label, path in (("Source folder", src), ("Destination folder", dest)):
        if not path:
            checks.append({"label": label, "ok": False, "detail": "not set"})
            continue
        p = Path(path)
        is_dest = label.startswith("Destination")
        if not p.exists():
            # The migration creates the destination tree, so a not-yet-created
            # dest is fine as long as a writable ancestor exists to land it in.
            anc = _existing_ancestor(p) if is_dest else None
            if is_dest and anc and os.access(str(anc), os.W_OK):
                checks.append({"label": label, "ok": True,
                               "detail": f"{p} (will be created under {anc})"})
            elif is_dest:
                checks.append({"label": label, "ok": False,
                               "detail": f"{p} can't be created. Nearest existing "
                                         f"folder {anc or p.anchor} is not writable"})
            else:
                checks.append({"label": label, "ok": False, "detail": f"{p} does not exist"})
        elif not p.is_dir():
            checks.append({"label": label, "ok": False, "detail": f"{p} is not a directory"})
        elif not os.access(str(p), os.R_OK):
            checks.append({"label": label, "ok": False, "detail": f"{p} is not readable"})
        elif is_dest and not os.access(str(p), os.W_OK):
            checks.append({"label": label, "ok": False, "detail": f"{p} is not writable"})
        else:
            checks.append({"label": label, "ok": True, "detail": str(p)})
    return checks


@app.get("/migrate", response_class=HTMLResponse)
async def migrate_page(request: Request):
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    return _tr(request, "migrate.html", {
        # No nav item of its own; it's reached from Settings, so Settings
        # stays lit. The paths surface through migrate_checks, not directly.
        "page": "settings",
        "configured": bool(src and dest),
        "migrate_checks": _migrate_checks(src, dest),
    })


@app.post("/migrate")
async def migrate_scan(request: Request):
    # No credential check: migration only reads and reorganises local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import migrate as engine
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    form = await request.form()
    use_acoustid = form.get("acoustid") == "on"
    in_place = form.get("in_place") == "on"
    if not src or not dest:
        err = ("Set MIGRATE_SRC and MIGRATE_DEST: the source library and "
               "the destination for the organised copy, then try again.")
    else:
        err = engine.validate_paths(Path(src), Path(dest), in_place=in_place)
    if err:
        return _tr(request, "migrate.html", {
            "page": "settings",
            "configured": bool(src and dest), "error": err,
            "migrate_checks": _migrate_checks(src, dest)})
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Library migration")
    job.review_verb = "Move" if in_place else "Copy"
    job.execute_kind = "migration"
    # src is persisted so a resume after restart can still prune the emptied
    # source folders on an in-place move (the live execute below gets it too).
    job.execute_args = {"dest": str(dest), "in_place": bool(in_place),
                        "src": str(src), "allow_low_space": False}
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_migration(j, src, dest, use_acoustid=use_acoustid,
                                       in_place=in_place),
        lambda j, chosen: flows.execute_migration(j, chosen, dest,
                                                  in_place=in_place, src=src,
                                                  allow_low_space=False),
        "migration")
    if job is None:
        return _scan_submission_failure_response(request, "/migrate")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str, approved: bool = False,
                   stale: bool = False, noselection: bool = False, page: int = 1,
                   error: str = "", q: str = "", tab: str = ""):
    job = job_mgr.registry.get(job_id)
    historical = False
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return RedirectResponse(
                url="/queue?error=" + urllib.parse.quote(
                    "That job is no longer in the record."),
                status_code=303)
        historical = True
    review_badge_ack = _review_badge_ack_for(job)
    # An upgrade/downsample job page is that tool's surface, so its nav item
    # lights up. Candidate rendering handles the review badge separately.
    nav_page = (job.execute_kind
                if job.execute_kind in ("upgrade", "downsample") else "queue")
    if nav_page == "upgrade" and not _upgrade_available():
        nav_page = "queue"
    if job.attention and job.attention not in ("recovery", "catalog"):
        # Opening the page is the acknowledgement: the History chip and the
        # nav's warning dot stand down once the user has seen the job.
        attention = job.attention
        from qobuz_librarian.web import job_persistence
        loop = asyncio.get_running_loop()
        acknowledged = await loop.run_in_executor(
            None,
            lambda: job_persistence.acknowledge_attention(job.id, attention),
        )
        if acknowledged:
            with job._lock:
                if job.attention == attention:
                    job.attention = ""
    new_release_state = {"stale": False, "reason": ""}
    if (
        job.execute_kind == "new_releases"
        and job.status == job_mgr.JobStatus.AWAITING_REVIEW
    ):
        from qobuz_librarian.library import generation_state, new_releases

        output = generation_state.output_state("new_releases")
        current = new_releases.is_baseline_complete()
        new_release_state = {
            "stale": not current,
            "reason": str(output.get("reason") or ""),
        }
    ctx = {"job": job, "page": nav_page,
           "approved": approved, "stale": stale, "noselection": noselection,
           "error": error, "historical": historical,
           "new_release_state": new_release_state,
           "queue_wait": _queue_wait(job),
           "JobStatus": job_mgr.JobStatus}
    ctx.update(_review_context(job, page, q, tab))
    return _tr(
        request, "job.html", ctx, review_badge_ack=review_badge_ack
    )


def _review_context(job, page=1, query="", tab=""):
    """Template vars for a paginated awaiting-review body: the current page's
    artist groups, the page number/count, and the authoritative whole-set
    counts. Cheap no-op for non-review states (no candidates → one empty page).

    A library review always splits into its two tabs, Missing Albums and Gap
    Fill, and ``tab`` picks one. With no explicit pick, land on Missing Albums
    unless it's empty and Gap Fill isn't. Other review kinds render untabbed.
    """
    from qobuz_librarian.ui_cli.colors import format_size
    tab_counts = None
    if job.execute_kind == "library":
        totals = _review_tab_totals(job)
        if totals["missing"] or totals["gaps"]:
            tab_counts = totals
    if tab_counts:
        if tab not in ("missing", "gaps"):
            tab = ("gaps" if tab_counts["gaps"] and not tab_counts["missing"]
                   else "missing")
    else:
        tab = ""
    groups = _review_artist_groups(job, query, tab)
    page_groups, page, n_pages = _paginate_groups(groups, page)
    counts = job.selection_counts()
    from qobuz_librarian.library import hidden as hidden_mod
    return {
        "review_groups": page_groups,
        "review_page": page,
        "review_pages": n_pages,
        "review_query": query,
        # What "Dismiss unselected" would actually take under this filter, so
        # the button cannot quote the tab total while acting on a subset.
        "review_filtered_rest": _filtered_rest_of(groups),
        "review_tab": tab,
        "review_tab_counts": tab_counts,
        "review_hidden_count": hidden_mod.count(_hide_scope(job.execute_kind)),
        "review_counts": counts,
        "review_reclaimable_label": (format_size(counts["reclaimable"])
                                     if counts["reclaimable"] else ""),
        "review_page_size": REVIEW_PAGE_ARTISTS,
    }


def _review_badge_ack_for(job):
    from qobuz_librarian.web import review_badges

    if (job.execute_kind not in review_badges.SURFACES
            or job.status != job_mgr.JobStatus.AWAITING_REVIEW
            or not job.selection_counts()["total"]):
        return None
    return (
        job.execute_kind,
        review_badges.ready_generation(job.execute_kind),
    )


@app.get("/jobs/{job_id}/content", response_class=HTMLResponse)
async def job_content(request: Request, job_id: str, page: int = 1,
                      embedded: bool = False):
    """The job page's state-specific body, on its own. The live page swaps
    this in when the SSE stream reports the job finished, so the terminal
    view has one render path, the server's, instead of a faked-up bar.
    ``embedded`` mirrors the embedding surface's flag (Library/Repair render
    the body under their own page heading), so the swapped-in body doesn't
    reintroduce the job header the full-page render suppressed."""
    job = job_mgr.registry.get(job_id)
    historical = False
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
        historical = True
    review_badge_ack = _review_badge_ack_for(job)
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus,
           "historical": historical,
           "embedded_surface": embedded,
           "queue_wait": _queue_wait(job)}
    ctx.update(_review_context(job, page))
    return _tr(
        request, "_job_body.html", ctx, review_badge_ack=review_badge_ack
    )


@app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
async def job_review_page(request: Request, job_id: str, page: int = 1,
                          q: str = "", tab: str = ""):
    """One page of the paginated review list (groups + pager + summary), for
    Prev/Next, the whole-set artist filter, and a library review's tab switch.
    Rendered from saved selection flags, so ticks persist and span pages."""
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
    review_badge_ack = _review_badge_ack_for(job)
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus}
    ctx.update(_review_context(job, page, q, tab))
    return _tr(
        request, "_review_page.html", ctx,
        review_badge_ack=review_badge_ack,
    )


def _build_unapproved_review(job, tab, *, admission_filter=None,
                             discard_ids=()):
    """Before approving a library or new-release review: move every candidate
    that ISN'T being downloaded right now into its own parked review, so a
    partial download consumes ONLY the ticked picks. Everything else stays in
    the living review:
    the unticked candidates, plus (on a tab-scoped approve) the whole tab the
    user isn't looking at, ticks and all. A final admission filter may also
    keep a selected candidate parked when another job claimed it just before
    approval. ``discard_ids`` removes candidates already proven complete on
    disk as part of the same durable transition. The caller holds ``job._lock``
    and durably admits both jobs before publishing either transition. Returns
    the new unpublished parked job, or None when every candidate is being
    used."""
    from qobuz_librarian.web import flows
    tab_scoped = tab in ("missing", "gaps")
    gap_active = tab == "gaps"
    keep, split = [], []
    discard_ids = set(discard_ids)
    for c in job.candidates:
        if c.get("cid") in discard_ids:
            continue
        in_scope = (not tab_scoped) or (flows.is_gap_candidate(c) == gap_active)
        selected = in_scope and c.get("selected")
        admitted = selected and (
            admission_filter is None or admission_filter(c)
        )
        (keep if admitted else split).append(c)
    if not split:
        return None
    job.candidates = keep
    other = job_mgr.Job(title=job.title, kind=job.kind,
                        execute_kind=job.execute_kind,
                        execute_args=dict(job.execute_args or {}),
                        review_verb=job.review_verb,
                        status=job_mgr.JobStatus.AWAITING_REVIEW)
    other.candidates = split  # cids, seqs, and saved ticks ride along
    other.sync_cand_seq()
    factory = _RESUME_EXECUTE.get(other.execute_kind)
    if factory is not None:
        other._execute_fn = factory(other, other.execute_args)
    return other


@app.post("/jobs/{job_id}/approve")
async def job_approve(request: Request, job_id: str):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(
                "That job is no longer in the record."),
            status_code=303)
    # A parked review can outlive its feature: credentials can be pulled after
    # an upgrade review parks, and the downsample engine can vanish across a
    # restart.
    if job.execute_kind == "upgrade" and not _upgrade_available():
        return _upgrade_unavailable_response()
    if job.execute_kind == "downsample":
        from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE
        if not HAVE_DOWNSAMPLE:
            return RedirectResponse(url="/downsample", status_code=303)
    # Repair and Library stay on their single surfaces through the executing
    # phase; every other kind (new-release checks included) keeps using the
    # job page.
    if job.execute_kind == "repair":
        dest = "/repair"
    elif job.execute_kind == "library":
        dest = "/library"
    else:
        dest = f"/jobs/{job_id}"
    if (job.status == job_mgr.JobStatus.AWAITING_REVIEW
            and job.execute_kind == "upgrade"
            and (job.execute_args or {}).get("quality_signature")
            != _effective_upgrade_quality_signature()):
        return RedirectResponse(
            url=f"/jobs/{job.id}?error=" + urllib.parse.quote(
                "Download quality changed since this Upgrade review was "
                "built. Run a Library refresh before approving it."
            ),
            status_code=303,
        )
    # A library review approves per tab: the button acts on the tab the user
    # is looking at, and only that tab.
    form = await request.form()
    migration_low_space_required = bool(
        job.execute_kind == "migration"
        and (job.execute_args or {}).get("in_place")
        and (job.execute_args or {}).get("requires_low_space_override")
    )
    migration_low_space_accepted = form.get("allow_low_space") == "on"
    if (
        job.status == job_mgr.JobStatus.AWAITING_REVIEW
        and migration_low_space_required
        and not migration_low_space_accepted
    ):
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(
                "Confirm the low-space risk before approving this in-place "
                "move. Your review is untouched."
            ),
            status_code=303,
        )
    tab = (form.get("tab") or "").strip()
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    loop = asyncio.get_running_loop()
    selected_candidate_ids = set()
    selected_candidate_snapshot = []
    stale_premise_candidate_ids = set()
    downsample_keep_originals = None
    downsample_choice_to_save = ""
    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
        from qobuz_librarian.web import flows
        if tab:
            gap_active = tab == "gaps"
            with job._lock:
                selected_candidate_ids = {
                    c.get("cid") for c in job.candidates
                    if (c.get("selected")
                        and flows.is_gap_candidate(c) == gap_active)
                }
        else:
            with job._lock:
                selected_candidate_ids = {
                    c.get("cid") for c in job.candidates if c.get("selected")
                }
        with job._lock:
            selected_candidate_snapshot = copy.deepcopy([
                c for c in job.candidates
                if c.get("cid") in selected_candidate_ids
            ])
        has_pick = bool(selected_candidate_ids)
        if not has_pick:
            return RedirectResponse(url=f"{dest}?noselection=1",
                                    status_code=303)
        if job.execute_kind in _PREMISE_REVIEW_KINDS:
            from qobuz_librarian.library.candidate_premise import (
                CandidateStale,
                validate_all,
            )

            def _stale_candidate_ids(candidates):
                stale = set()
                for candidate in candidates:
                    try:
                        validate_all([candidate])
                    except CandidateStale:
                        stale.add(candidate.get("cid"))
                return stale

            stale_premise_candidate_ids = await loop.run_in_executor(
                None,
                lambda: _stale_candidate_ids(selected_candidate_snapshot),
            )
            if selected_candidate_ids <= stale_premise_candidate_ids:
                return RedirectResponse(
                    url=dest + "?error=" + urllib.parse.quote(
                        "The selected local files changed after this review "
                        "was built, or the saved proof is too old. Refresh or "
                        "rescan before trying again. Nothing changed."
                    ),
                    status_code=303,
                )
        # Only now that the run is going to happen: the keep-vs-delete answer is
        # a standing policy saved to Settings, so asking and saving it before
        # anything checks that an album is ticked lets a no-op approve change
        # what every future downsample does with your originals.
        if job.execute_kind == "downsample":
            from qobuz_librarian.web import settings_store

            # current() includes a saved value waiting for another lane to
            # finish. Bind that value to this approval below.
            choice = _downsample_originals_choice()
            rendered_choice = (
                form.get("downsample_policy") or ""
            ).strip().lower()
            current_choice = choice if choice in ("keep", "delete") else ""
            if (
                rendered_choice not in ("", "keep", "delete")
                or rendered_choice != current_choice
            ):
                return RedirectResponse(
                    url=dest + "?error=" + urllib.parse.quote(
                        "The keep-or-delete setting changed after this review "
                        "was shown. No music files were changed; review the "
                        "updated warning and approve again."
                    ),
                    status_code=303,
                )
            if choice not in ("keep", "delete"):
                choice = (form.get("keep_choice") or "").strip().lower()
                if choice in ("keep", "delete"):
                    downsample_choice_to_save = choice
                else:
                    with job._lock:
                        picked = sum(
                            1 for c in job.candidates if c.get("selected")
                        )
                    return _tr(request, "downsample_keep_choice.html", {
                        "job": job, "page": "downsample", "picked": picked,
                        "downsample_originals_choice": current_choice,
                        "backup_retention_days":
                            cfg.UPGRADE_BACKUP_RETENTION_DAYS})
            downsample_keep_originals = choice == "keep"

    authorized_credentials = None
    stale_owned_candidate_ids = set()
    if (job.status == job_mgr.JobStatus.AWAITING_REVIEW
            and job.execute_kind in _QOBUZ_REVIEW_KINDS):
        try:
            authorized_credentials = await _authorize_qobuz_for_web(
                QobuzAccess.DOWNLOAD_ACTION
            )
        except (
            NoCredsError,
            AuthLost,
            QobuzUnavailable,
            QobuzEntitlementError,
            DownloaderNotReady,
            CredentialChanged,
            asyncio.TimeoutError,
        ) as exc:
            message = _qobuz_action_error_message(exc, unchanged=True)
            return RedirectResponse(
                url=dest + "?error=" + urllib.parse.quote(message),
                status_code=303,
            )
        if job.execute_kind in _LIBRARY_SURFACE_KINDS:
            from qobuz_librarian.web import flows

            stale_owned_candidate_ids = await loop.run_in_executor(
                None,
                lambda: flows.owned_missing_candidate_ids(
                    job,
                    authorized_credentials.token,
                    candidate_ids=(
                        selected_candidate_ids
                        - stale_premise_candidate_ids
                    ),
                ),
            )
    skipped = len(stale_owned_candidate_ids)
    _skip_q = f"&skipped={skipped}" if skipped else ""
    # Selection is saved server-side as the user ticks (the paginated review
    # no longer carries every checkbox in the form), so approve runs against
    # the saved flags; passing None keeps them as-is rather than reading the
    # form.
    def _split_and_approve():
        nonlocal stale_premise_candidate_ids

        from qobuz_librarian.completion import normalise_album_id

        # Atomic recheck right before anything is consumed: the route's
        # opening gate ran before several awaits (form parsing, disk probes),
        # and set_mode('cli') can hand the run lock to the terminal inside
        # that window, this not-yet-approved review is invisible to its
        # active-job check, so approving after the handoff would start
        # destructive work with the single-writer guard off.
        with (
            _SAVED_REVIEW_LOCK,
            _auto_check_lock,
            _DOWNLOAD_SUBMIT_LOCK,
            _CREDENTIAL_LOCK,
            job._review_action_lock,
        ):
            if _web_writes_paused():
                return "paused"
            if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
                return False

            def current_selected_candidates():
                with job._lock:
                    candidates = [
                        c for c in job.candidates if c.get("selected")
                    ]
                    if tab:
                        gap_active = tab == "gaps"
                        candidates = [
                            c for c in candidates
                            if flows.is_gap_candidate(c) == gap_active
                        ]
                    return copy.deepcopy(candidates)

            current_selected = current_selected_candidates()
            if {
                c.get("cid") for c in current_selected
            } != selected_candidate_ids:
                return "review_changed"
            if job.execute_kind in _PREMISE_REVIEW_KINDS:
                stale_premise_candidate_ids = _stale_candidate_ids(
                    current_selected)
                if {
                    c.get("cid") for c in current_selected
                } <= stale_premise_candidate_ids:
                    return "all_candidates_stale"
            if (authorized_credentials is not None
                    and not _credential_generation_is_active(
                        authorized_credentials.generation)):
                return "credential_changed"
            if (job.execute_kind in ("upgrade", "downsample")
                    and job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                synced_job = _sync_saved_review_before_approve(job)
                if synced_job is not job:
                    return "review_changed"
                current_selected = current_selected_candidates()
                if not current_selected:
                    return job_mgr.APPROVAL_NO_SELECTION
                if job.execute_kind in _PREMISE_REVIEW_KINDS:
                    stale_premise_candidate_ids = _stale_candidate_ids(
                        current_selected)
                    if {
                        c.get("cid") for c in current_selected
                    } <= stale_premise_candidate_ids:
                        return "all_candidates_stale"
            # A review action consumes only the ticked picks: park everything
            # else (plus the inactive Library tab) as its own living review so
            # one partial batch cannot eat unreviewed candidates.
            split_review = None
            admission_decisions = {}

            def selection_filter(candidate):
                key = candidate.get("cid")
                if not isinstance(key, str) or not key:
                    return False
                if key in admission_decisions:
                    return admission_decisions[key]
                if key in stale_owned_candidate_ids:
                    admission_decisions[key] = False
                    return False
                if key in stale_premise_candidate_ids:
                    admission_decisions[key] = False
                    return False
                if tab:
                    gap_active = tab == "gaps"
                    if flows.is_gap_candidate(candidate) != gap_active:
                        admission_decisions[key] = False
                        return False
                if job.execute_kind in ("library", "new_releases"):
                    album_id = normalise_album_id(
                        (candidate.get("payload") or {}).get("album_id")
                    )
                    if album_id is None:
                        admission_decisions[key] = False
                        return False
                    admitted = _duplicate_download_job(album_id) is None
                else:
                    admitted = True
                admission_decisions[key] = admitted
                return admitted

            if ((job.execute_kind in _PREMISE_REVIEW_KINDS
                    or stale_premise_candidate_ids)
                    and job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                def split_review(review_job):
                    remnant = _build_unapproved_review(
                        review_job,
                        tab,
                        admission_filter=selection_filter,
                        discard_ids=stale_owned_candidate_ids,
                    )
                    if remnant is not None:
                        remnant.execute_args.pop(
                            "_credential_generation", None)
                    # Whole review ticked → retire the worked-through baseline
                    # after success instead of rebuilding its old candidates.
                    if review_job.execute_kind == "library":
                        review_job._consumed_whole_review = remnant is None
                    return remnant

            previous_migration_args = None
            previous_migration_execute = None
            previous_downsample_args = None
            previous_qobuz_args = None
            previous_qobuz_execute = None
            downsample_args_changed = False
            qobuz_args_changed = False
            if authorized_credentials is not None:
                with job._lock:
                    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
                        previous_qobuz_args = job.execute_args
                        previous_qobuz_execute = job._execute_fn
                        job.execute_args = {
                            **(job.execute_args or {}),
                            "_credential_generation":
                                authorized_credentials.generation,
                        }
                        factory = _RESUME_EXECUTE.get(job.execute_kind)
                        if factory is not None:
                            job._execute_fn = factory(job, job.execute_args)
                        qobuz_args_changed = True
            if (
                job.execute_kind == "downsample"
                and downsample_keep_originals is not None
            ):
                with job._lock:
                    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
                        previous_downsample_args = job.execute_args
                        job.execute_args = {
                            **(job.execute_args or {}),
                            "keep_originals": downsample_keep_originals,
                        }
                        job._execute_fn = _resume_downsample(
                            job, job.execute_args)
                        downsample_args_changed = True
            if downsample_choice_to_save:
                saved, _warnings = settings_store.save({
                    "DOWNSAMPLE_KEEP_ORIGINALS": downsample_choice_to_save,
                })
                if saved is not True:
                    return "downsample_policy_failed"
            if job.execute_kind == "migration":
                execute_args = dict(job.execute_args or {})
                # Old parked reviews may still carry the former launcher
                # checkbox. Only an acknowledgement submitted beside the
                # measured short-space review can enable the override now.
                execute_args["allow_low_space"] = bool(
                    migration_low_space_required
                    and migration_low_space_accepted
                )
                execute_fn = _resume_migration(job, execute_args)
                with job._lock:
                    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
                        previous_migration_args = job.execute_args
                        previous_migration_execute = job._execute_fn
                        job.execute_args = execute_args
                        job._execute_fn = execute_fn
            approved = None
            try:
                approved = job_mgr.approve(
                    job,
                    None,
                    split_review=split_review,
                    selection_filter=selection_filter,
                )
                return approved
            finally:
                if downsample_args_changed and approved is not True:
                    with job._lock:
                        job.execute_args = previous_downsample_args
                        job._execute_fn = _resume_downsample(
                            job, job.execute_args or {})
                if qobuz_args_changed and approved is not True:
                    with job._lock:
                        job.execute_args = previous_qobuz_args
                        job._execute_fn = previous_qobuz_execute
                if (
                    previous_migration_args is not None
                    and approved is not True
                ):
                    with job._lock:
                        job.execute_args = previous_migration_args
                        job._execute_fn = previous_migration_execute

    approved = await loop.run_in_executor(None, _split_and_approve)
    if approved == "all_candidates_stale":
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(
                "The selected local files changed after this review was "
                "built, or the saved proof is too old. Refresh or rescan "
                "before trying again. Nothing changed."
            ),
            status_code=303,
        )
    if (isinstance(approved, tuple)
            and len(approved) == 2
            and approved[0] == "candidate_stale"):
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(approved[1]),
            status_code=303,
        )
    if approved == "review_changed":
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(
                "That review changed while approval was being checked. "
                "Nothing changed; review the current selections and try again."
            ),
            status_code=303,
        )
    if approved == "downsample_policy_failed":
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(
                "Couldn't save the keep-or-delete choice. No music files "
                "were changed; check the data folder and try again."
            ),
            status_code=303,
        )
    if approved == "credential_changed":
        message = _qobuz_action_error_message(
            CredentialChanged(),
            unchanged=True,
        )
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(message),
            status_code=303,
        )
    if approved == "sync_failed":
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(
                "The refreshed review could not be saved. Your existing "
                "choices are untouched; check the data folder and try again."
            ),
            status_code=303,
        )
    if approved == "paused":
        busy = _lock_busy_response(request)
        if busy is not None:
            return busy
        return RedirectResponse(url=dest, status_code=303)
    if approved is None:
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(
                job_mgr.JOB_ADMISSION_ERROR
            ) + _skip_q,
            status_code=303,
        )
    if approved is job_mgr.APPROVAL_NO_SELECTION:
        return RedirectResponse(url=f"{dest}?noselection=1{_skip_q}",
                                status_code=303)
    flag = "approved=1" if approved else "stale=1"
    local_stale = len(stale_premise_candidate_ids)
    local_stale_q = ""
    if local_stale:
        local_stale_q = "&error=" + urllib.parse.quote(
            f"Started the valid choices. {local_stale} selected "
            f"album{'s' if local_stale != 1 else ''} changed since this "
            "review and remain selected for a fresh scan."
        )
    return RedirectResponse(
        url=f"{dest}?{flag}{_skip_q}{local_stale_q}",
        status_code=303,
    )


# Review kinds that get the paced-triage surface (unticked and hideable). They
# share one review screen; hidden-store scope decides where dismissals land.
_TRIAGE_KINDS = ("library", "upgrade", "new_releases", "downsample")

# Kinds whose review screen has server-backed per-candidate selection.
_SELECTABLE_KINDS = _TRIAGE_KINDS + ("repair", "migration")


def _hide_scope(execute_kind):
    from qobuz_librarian.library import hidden as hidden_mod
    if execute_kind == "upgrade":
        return hidden_mod.SCOPE_UPGRADE
    if execute_kind == "downsample":
        return hidden_mod.SCOPE_DOWNSAMPLE
    return hidden_mod.SCOPE_MISSING


# Artist groups per review page.
REVIEW_PAGE_ARTISTS = 40
# Whole-group candidate budget per page; see _paginate_groups.
REVIEW_PAGE_CANDIDATES = 1500


def _artist_sort_key(name: str) -> str:
    """Order artists ignoring a leading article, so 'The Beatles' files under B
    (not T) and 'A Tribe Called Quest' under T, the way music libraries sort.
    Case-insensitive."""
    low = (name or "").strip().casefold()
    for art in ("the ", "a ", "an "):
        if low.startswith(art):
            return low[len(art):]
    return low


def _review_artist_groups(job, query="", tab=""):
    """Candidates grouped by artist for the review screen, in a deterministic
    order so pagination is stable across reloads. ``query`` filters across the
    WHOLE set (artist name or any album title), so the filter spans pages, not
    just the one on screen. ``tab`` narrows a library review to one side of its
    Missing Albums / Gap Fill split. Returns a list of (artist, items) pairs."""
    from qobuz_librarian.web import flows
    with job._lock:
        cands = list(job.candidates)
    q = (query or "").strip().lower()
    groups: dict = {}
    for c in cands:
        if tab and flows.is_gap_candidate(c) != (tab == "gaps"):
            continue
        artist = c.get("artist") or ""
        if q and not flows.candidate_matches_query(c, q):
            continue
        groups.setdefault(artist, []).append(c)
    # Sort groups by music-library order, tracks by their stable seq.
    ordered = []
    for artist in sorted(groups, key=_artist_sort_key):
        items = sorted(groups[artist], key=lambda c: c.get("seq", 0))
        ordered.append((artist, items))
    return ordered


def _paginate_groups(groups, page):
    """Slice artist groups into one page. Returns (page_groups, page, n_pages).
    ``page`` is clamped into range so a stale/empty page lands somewhere valid.

    Pages pack whole artist groups (so select-artist stays sane) up to
    REVIEW_PAGE_ARTISTS groups AND ~REVIEW_PAGE_CANDIDATES rows, counting
    artists alone let a page of prolific artists carry thousands of collapsed
    rows into the DOM (a 40-artist page measured 758KB HTML). A single group
    larger than the budget still gets its own page, whole."""
    pages = []
    cur, cur_rows = [], 0
    for g in groups:
        rows = len(g[1])
        if cur and (len(cur) >= REVIEW_PAGE_ARTISTS
                    or cur_rows + rows > REVIEW_PAGE_CANDIDATES):
            pages.append(cur)
            cur, cur_rows = [], 0
        cur.append(g)
        cur_rows += rows
    if cur:
        pages.append(cur)
    n_pages = max(1, len(pages))
    page = max(1, min(int(page or 1), n_pages))
    return (pages[page - 1] if pages else []), page, n_pages


def _review_origin(request) -> str:
    """The requesting tab's self-assigned id, for review-changed fan-outs.

    app.js mints one per page load and sends it on every review mutation, so
    the SSE nudge can name where a change came from and the originating tab can
    skip reloading itself (its DOM is already current, and reloading would swallow
    the user's next tick mid-swap). Clamped to a token-safe alphabet so a forged
    header can't smuggle SSE framing into the stream."""
    raw = request.headers.get("X-QL-Origin", "")
    return "".join(c for c in raw if c.isalnum())[:32]


def _get_reviewable_job(job_id):
    """A job from the live registry, or rehydrated from disk if it has been
    evicted, so a restored awaiting-review job's selection and pager work, and
    an evicted (terminal) job's review page still renders and pages. Ticks only
    persist for a job still in the registry; persist_soon no-ops on a rehydrated
    copy, but an evicted job is terminal and its review is read-only, so there's
    nothing to save. Returns None if it's nowhere."""
    job = job_mgr.registry.get(job_id)
    if job is None:
        job = job_mgr.load_historical_job(job_id)
    return job


def _selection_payload(job, *, persist_failed=False):
    """JSON the selection/hide endpoints return so every open tab can refresh
    its counts from the server instead of recounting a partial DOM."""
    from qobuz_librarian.ui_cli.colors import format_size
    c = job.selection_counts()
    payload = {
        "selected": c["selected"],
        "total": c["total"],
        "artists": c["artists"],
        "reclaimable": c["reclaimable"],
        "reclaimable_label": format_size(c["reclaimable"]) if c["reclaimable"] else "",
    }
    if persist_failed:
        payload["persist_failed"] = True
    if job.execute_kind == "library":
        totals = _review_tab_totals(job)
        payload["missing_total"] = totals["missing"]
        payload["gap_total"] = totals["gaps"]
        payload["missing_selected"] = totals["missing_selected"]
        payload["gap_selected"] = totals["gaps_selected"]
    return payload


def _filtered_rest_of(groups):
    return sum(1 for _artist, rows in groups
               for row in rows if not row.get("selected"))


def _filtered_rest(job, query, tab):
    """Unselected candidates under a review filter: what Dismiss unselected
    will take. The tick endpoints re-answer this so the button can't keep
    quoting the number from render time."""
    return _filtered_rest_of(_review_artist_groups(job, query, tab))


def _review_tab_totals(job):
    """Whole-set totals and selected counts behind a library review's Missing
    Albums / Gap Fill tabs, ignoring the page filter so the tab labels stay
    truthful. Selected counts feed the tab-scoped bulk bar: what the user sees
    on the active tab is exactly what Download/Dismiss will act on."""
    from qobuz_librarian.web import flows
    gaps = gaps_sel = missing_sel = 0
    with job._lock:
        total = len(job.candidates)
        for c in job.candidates:
            if flows.is_gap_candidate(c):
                gaps += 1
                gaps_sel += 1 if c.get("selected") else 0
            elif c.get("selected"):
                missing_sel += 1
    return {"missing": total - gaps, "gaps": gaps,
            "missing_selected": missing_sel, "gaps_selected": gaps_sel}


def _set_all_selected_with_membership(job, on, cids):
    """Apply a bulk choice and return the candidate IDs it covered."""
    wanted = set(cids) if cids is not None else None
    accepted_cids = []
    changed = 0
    with job._lock:
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return None, []
        for candidate in job.candidates:
            cid = candidate.get("cid")
            if wanted is not None and cid not in wanted:
                continue
            accepted_cids.append(cid)
            if bool(candidate.get("selected")) != bool(on):
                candidate["selected"] = bool(on)
                changed += 1
    return changed, accepted_cids


@app.post("/jobs/{job_id}/select")
async def job_select(request: Request, job_id: str):
    """Persist a single tick/untick. The review page doesn't rely on the posted
    checkboxes (pagination means most aren't in the DOM), so each toggle saves
    immediately and the saved flags are the source of truth at download."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return JSONResponse({"error": "not found"}, status_code=404)
    form = await request.form()
    cid = (form.get("cid") or "").strip()
    on = (form.get("checked") or "").strip().lower() in ("1", "true", "on", "yes")
    changed = job.set_selected(cid, on)
    if changed is None:
        return JSONResponse(
            {"error": "review is no longer awaiting selection"},
            status_code=409,
        )
    if changed:
        # Coalesced save: the candidate list is multi-MB on a big library, so
        # the tap must not wait for (or even schedule) a full serialize+write.
        job_mgr.persist_soon(job)
        job.notify_review_changed(_review_origin(request))
    payload = _selection_payload(job)
    q = (form.get("q") or "").strip()
    if q:
        payload["filtered_rest"] = _filtered_rest(
            job, q, (form.get("tab") or "").strip())
    return JSONResponse(payload)


@app.post("/jobs/{job_id}/select-all")
async def job_select_all(request: Request, job_id: str):
    """Bulk select/deselect across the whole view, one page, or one artist."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return JSONResponse({"error": "not found"}, status_code=404)
    form = await request.form()
    on = (form.get("on") or "").strip().lower() in ("1", "true", "on", "yes")
    scope = (form.get("scope") or "all").strip().lower()
    if scope not in ("all", "page", "artist"):
        return JSONResponse({"error": "invalid scope"}, status_code=400)
    cids = form.getlist("cid")[:100000] if scope == "page" else None
    # Tab and filter scoping: on a library review, select-all flips only the
    # active tab's candidates, never the tab the user can't see.
    tab = (form.get("tab") or "").strip()
    q = (form.get("q") or "").strip().lower()
    artist = (form.get("artist") or "").strip()
    page_artists = set(form.getlist("artist")[:REVIEW_PAGE_ARTISTS])
    tab_scoped = (job.execute_kind == "library" and tab in ("missing", "gaps"))
    if (scope == "artist" or (scope == "page" and page_artists)
            or (cids is None and (tab_scoped or q))):
        from qobuz_librarian.web import flows
        gap_active = tab == "gaps"
        with job._lock:
            cids = [c["cid"] for c in job.candidates
                    if (scope != "artist" or (c.get("artist") or "") == artist)
                    and (scope != "page"
                         or not page_artists
                         or (c.get("artist") or "") in page_artists)
                    and (not tab_scoped
                         or flows.is_gap_candidate(c) == gap_active)
                    and (not q or flows.candidate_matches_query(c, q))]
    persist_failed = False
    changed, accepted_cids = _set_all_selected_with_membership(job, on, cids)
    if changed is None:
        return JSONResponse(
            {"error": "review is no longer awaiting selection"},
            status_code=409,
        )
    if changed:
        # Unlike a single tap, a bulk choice is one infrequent operation whose
        # success needs to mean its complete result is durable.
        from qobuz_librarian.web import job_persistence
        loop = asyncio.get_running_loop()
        saved = await loop.run_in_executor(
            None, lambda: job_persistence.persist(job))
        persist_failed = not saved
        job.notify_review_changed(_review_origin(request))
    payload = _selection_payload(job, persist_failed=persist_failed)
    payload["accepted_cids"] = accepted_cids
    if q:
        payload["filtered_rest"] = _filtered_rest(job, q, tab)
    return JSONResponse(payload)


@app.get("/jobs/{job_id}/review-group-items", response_class=HTMLResponse)
async def job_review_group_items(request: Request, job_id: str,
                                 artist: str = "", tab: str = "", q: str = ""):
    """Render one artist's current rows when a collapsed group is opened."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return HTMLResponse("", status_code=404)
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    groups = _review_artist_groups(job, query=q, tab=tab)
    items = next((rows for name, rows in groups if name == artist), [])
    return _tr(request, "_review_group_items.html", {
        "job": job, "items": items, "review_tab": tab,
    })


@app.post("/jobs/{job_id}/hide", response_class=HTMLResponse)
async def job_hide(request: Request, job_id: str):
    """Dismiss an artist's albums from a triage scan (gap or upgrade).

    A triage action, not a download: it writes the durable hidden-store (in
    the scan's scope) and drops those candidates from the review list,
    returning just the affected artist's group (or empty if the whole artist is
    gone) for an htmx swap of that one group. Allowed while the scan is still
    running, and never lock-guarded, so dismissing stays available mid-scan and
    while a download holds the staging lock.
    """
    # Use the disk fallback like every other review endpoint so Hide keeps
    # working on a restored/archived awaiting-review job (registry.get alone
    # 404s once the job is evicted, while /select, /review and /content don't).
    job = _get_reviewable_job(job_id)
    if not job:
        return HTMLResponse("", status_code=404)
    if (job.execute_kind in _TRIAGE_KINDS and job.status in (
            job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)):
        from qobuz_librarian.web import flows
        form = await request.form()
        artist = (form.get("artist") or "").strip()
        # A library review split into Missing Albums / Gap Fill tabs scopes the
        # hide to the tab whose rows the button sat next to; the other tab's
        # candidates for this artist are untouched.
        tab = (form.get("tab") or "").strip()
        if job.execute_kind != "library" or tab not in ("missing", "gaps"):
            tab = ""
        gap_only = (tab == "gaps") if tab else None
        # The filter narrows what the button sits next to, so it has to narrow
        # what the button takes; without it, one tap on a filtered row dismisses
        # every album by that artist, including the ones the filter is hiding.
        q = (form.get("q") or "").strip()
        # Selection is server-backed, so hide keeps this artist's ticked albums
        # and drops the rest, with no form keep-set, which under pagination would
        # only carry the visible page and clobber other pages' selections.
        try:
            with _SAVED_REVIEW_LOCK:
                n = flows.dismiss_albums(job, artist,
                                         scope=_hide_scope(job.execute_kind),
                                         gap_only=gap_only,
                                         query=q)
        except OSError as e:
            # Nothing changed server-side; the non-2xx keeps htmx from
            # swapping the rows away and the error toast reads this body.
            return HTMLResponse(str(e), status_code=500)
        if n is False:
            return HTMLResponse(
                "The dismissal could not be saved to the Library review. "
                "Nothing was dismissed. Check the data folder and try again.",
                status_code=503,
            )
        if n is None:
            return HTMLResponse(
                "That review changed before the dismissal was saved. Reload "
                "the page and try again.",
                status_code=409,
            )
        if n:
            # Keep other open tabs in sync; the originator already gets the
            # swapped group + fresh counts from this response.
            job.notify_review_changed(_review_origin(request))
        # Dismissing the last album completes the review, so drop AWAITING_REVIEW
        # the dashboard "new releases" banner clears and this page stops showing an
        # empty "awaiting review". HX-Refresh reloads to the finished view.
        finalized = job_mgr.finalize_review_if_empty(job)
        if finalized is None:
            return HTMLResponse(
                "The dismissal was saved, but the finished review could not "
                "be recorded. Check the data folder and reload.",
                status_code=503,
            )
        if finalized:
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        # Re-render what the filter shows, not the whole artist, so the group
        # that swaps in matches the list the user is looking at.
        groups = _review_artist_groups(job, query=q, tab=tab)
        remaining = next((rows for name, rows in groups if name == artist), [])
        if remaining:
            resp = _tr(request, "_review_group.html",
                       {"job": job, "artist": artist, "items": remaining,
                        "triage": True, "open": True, "review_tab": tab,
                        "review_query": q})
        else:
            resp = HTMLResponse("")  # whole artist hidden, outerHTML drops it
        if n:
            # Carry the fresh authoritative counts so the page updates the
            # summary/selected/reclaimable without recounting a partial DOM.
            import json as _json

            from qobuz_librarian.library import hidden as hidden_mod
            counts = _selection_payload(job)
            counts["hidden_total"] = hidden_mod.count(
                _hide_scope(job.execute_kind))
            resp.headers["HX-Trigger"] = _json.dumps(
                {"qlHidden": {"n": n, "counts": counts}})
        return resp
    return HTMLResponse("")


@app.post("/jobs/{job_id}/dismiss-rest")
async def job_dismiss_rest(request: Request, job_id: str):
    """Dismiss every album the user didn't pick: durable-hide all unselected
    candidates across the whole review at once, leaving just the keepers."""
    job = _get_reviewable_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not (job.execute_kind in _TRIAGE_KINDS and job.status in (
            job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)):
        return JSONResponse({"error": "not reviewable"}, status_code=404)

    from qobuz_librarian.web import flows
    scope = _hide_scope(job.execute_kind)
    # Tab scoping: "Dismiss unselected" on a library review only drops the
    # active tab's unselected candidates.
    form = await request.form()
    tab = (form.get("tab") or "").strip()
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    gap_only = (tab == "gaps") if tab else None
    # An active filter narrows the dismissal to the rows it shows, same
    # what-you-see-is-what-you-act-on rule as select-all.
    q = (form.get("q") or "").strip().lower()
    # Snapshot the artists that still have an unticked album.
    with job._lock:
        artists, seen = [], set()
        for c in job.candidates:
            if c.get("selected"):
                continue
            if gap_only is not None and flows.is_gap_candidate(c) != gap_only:
                continue
            if q and not flows.candidate_matches_query(c, q):
                continue
            name = c.get("artist") or ""
            if name not in seen:
                seen.add(name)
                artists.append(name)

    # Offload: this can touch the whole review (a hidden-store write per artist
    # plus a persist), which would block the event loop and stall every SSE
    # stream for a large scan.
    loop = asyncio.get_running_loop()
    done = {"n": 0, "stale": False, "save_failed": False}

    def _dismiss_all():
        with _SAVED_REVIEW_LOCK, job._review_action_lock:
            for a in artists:
                hidden = flows.dismiss_albums(
                    job,
                    a,
                    scope=scope,
                    gap_only=gap_only,
                    query=q,
                )
                if hidden is None:
                    done["stale"] = True
                    break
                if hidden is False:
                    done["save_failed"] = True
                    break
                done["n"] += hidden

    try:
        await loop.run_in_executor(None, _dismiss_all)
        hidden_count = done["n"]
    except OSError as e:
        # Mid-batch store failure: the artists already hidden stay hidden, the
        # rest are untouched, so report the failure instead of a count.
        if done["n"]:
            job.notify_review_changed()
        return JSONResponse({"error": str(e), "hidden": done["n"]},
                            status_code=500)
    if done["stale"]:
        if done["n"]:
            job.notify_review_changed()
        return JSONResponse(
            {
                "error": "That review changed before dismissal completed.",
                "hidden": done["n"],
            },
            status_code=409,
        )
    if done["save_failed"]:
        if done["n"]:
            job.notify_review_changed()
        return JSONResponse(
            {
                "error": "The Library review could not be saved.",
                "hidden": done["n"],
            },
            status_code=503,
        )
    if hidden_count:
        job.notify_review_changed(_review_origin(request))
    from qobuz_librarian.library import hidden as hidden_mod
    payload = _selection_payload(job)
    payload["hidden"] = hidden_count
    payload["hidden_total"] = hidden_mod.count(scope)
    finalized = job_mgr.finalize_review_if_empty(job)
    payload["review_done"] = finalized is True
    if finalized is None:
        payload["finalize_failed"] = True
    return JSONResponse(payload)


@app.post("/jobs/{job_id}/acknowledge-recovery")
async def job_acknowledge_recovery(request: Request, job_id: str):
    """Retire exact Repair recovery records whose folders are confirmed gone."""
    from qobuz_librarian.web import job_persistence
    job = job_mgr.registry.get(job_id) or job_mgr.load_historical_job(job_id)
    if not job:
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(
                "That job is no longer in the record."),
            status_code=303)
    job_persistence.acknowledge_missing_recoveries(job, _recovery_missing)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/retry")
async def job_retry(request: Request, job_id: str):
    # Retry rebuilds the download from the persisted album_id, so it works as
    # well for a job evicted from the registry (restart, or 50 jobs later) as
    # for a live one, so fall back to the archive instead of silently bouncing.
    job = job_mgr.registry.get(job_id) or job_mgr.load_historical_job(job_id)
    if not job:
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(
                "That job is no longer in the record."),
            status_code=303)
    if job.status != job_mgr.JobStatus.FAILED or not job.album_id:
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(
                "Nothing to retry for that job."),
            status_code=303)
    if job.execute_args_unreadable:
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(
                "The saved details needed to retry that job couldn't be read. "
                "Start the download again from Search or Library."),
            status_code=303,
        )
    if (job.execute_args or {}).get("retry_disabled") == "lossy":
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(
                "Qobuz only has the missing tracks in lossy quality. This "
                "album needs another source, so it cannot be retried here."),
            status_code=303,
        )
    if (job.execute_args or {}).get("retry_disabled") == "backup":
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(
                "This album has a retained safety backup. Review it under "
                "Settings > Diagnostics before starting the album again."),
            status_code=303,
        )
    if (
        getattr(job, "_preserve_persisted_single", False) is True
        or getattr(job, "_single_undo_unavailable", False) is True
    ):
        return _durable_recovery_response(
            request,
            "This track's saved recovery state is uncertain, so Retry is "
            "paused. No download was started. Restart Qobuz Librarian.",
        )
    form = await request.form()
    raw_recovery_operation = form.get("recovery_operation_id")
    raw_recovery_item = form.get("recovery_item_id")
    recovery_submission = (
        raw_recovery_operation is not None or raw_recovery_item is not None
    )
    recovery_operation_id = (
        raw_recovery_operation.strip()
        if isinstance(raw_recovery_operation, str)
        else ""
    )
    recovery_item_id = (
        raw_recovery_item.strip()
        if isinstance(raw_recovery_item, str)
        else ""
    )
    if recovery_submission and (
        not recovery_operation_id
        or not recovery_item_id
        or len(recovery_operation_id) > 128
        or len(recovery_item_id) > 128
    ):
        return _durable_recovery_response(
            request,
            "That recovery Retry is incomplete or stale. No download was "
            "started. Reload this page and try again.",
        )

    try:
        credentials = await _authorize_qobuz_for_web(
            QobuzAccess.DOWNLOAD_ACTION
        )
    except (
        NoCredsError,
        AuthLost,
        QobuzUnavailable,
        QobuzEntitlementError,
        DownloaderNotReady,
        CredentialChanged,
        asyncio.TimeoutError,
    ) as exc:
        message = _qobuz_action_error_message(exc, unchanged=True)
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(message),
            status_code=303,
        )

    # A Retry is also the only user-triggered lane for an interrupted durable
    # Web download.
    if not _run_lock_intact():
        busy = _lock_busy_response(request)
        if busy is not None:
            return busy
        return _durable_recovery_response(
            request,
            "The single-writer safety lock could not be verified. No download "
            "was started. Restart Qobuz Librarian.",
        )
    try:
        recovery = _record_startup_recovery(_RUN_LOCK_HANDLE)
    except Exception:
        return _durable_recovery_response(
            request,
            "The saved recovery state could not be checked safely. No "
            "download was started. Restart Qobuz Librarian.",
        )
    recovery_status = _recovery_status_value(recovery)

    completion_acknowledged = _durable_completion_status(job)
    if completion_acknowledged is None:
        return _durable_recovery_response(
            request,
            "The saved completion record could not be checked safely. No "
            "download was started. Check the data-folder permissions, then "
            "restart Qobuz Librarian.",
        )
    if completion_acknowledged:
        if recovery_status != "clear":
            return _durable_recovery_response(
                request,
                "This download is already recorded as complete, but its "
                "interrupted recovery proof is not settled yet. No download "
                "was started. Check the application log, then restart Qobuz "
                "Librarian.",
            )
        busy = _lock_busy_response(request)
        if busy is not None:
            return busy
        if not _reconcile_acknowledged_job(job):
            return _durable_recovery_response(
                request,
                "The completed download could not be saved to History. No "
                "download was started. Check the data-folder permissions, "
                "then restart Qobuz Librarian.",
            )
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

    if recovery_submission:
        if not _recovery_submission_matches(
            job,
            recovery_operation_id,
            recovery_item_id,
        ):
            return _durable_recovery_response(
                request,
                "That interrupted-download Retry is stale. No download was "
                "started. Reload the job and use its current Retry button.",
            )
    elif recovery_status != "clear" or job.attention == "recovery":
        return _durable_recovery_response(
            request,
            "This download needs its exact recovery Retry control. No "
            "download was started. Reload the interrupted job and use Retry "
            "there.",
        )

    if (
        recovery_submission
        and
        recovery_status == "attention_required"
        and _durable_recovery_matches_job(job)
    ):
        from qobuz_librarian.queue.startup_recovery import (
            BlockedItemSettlementAction,
        )

        with _CREDENTIAL_LOCK:
            if not _credential_generation_is_active(credentials.generation):
                message = _qobuz_action_error_message(
                    CredentialChanged(),
                    unchanged=True,
                )
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(message),
                    status_code=303,
                )
            settled, reason = _settle_durable_web_recovery(
                job,
                BlockedItemSettlementAction.RETRY,
            )
        logging.getLogger("qobuz_librarian").info(
            "Retry %s: settling the blocked download %s.", job.id,
            "succeeded" if settled
            else f"was refused: {(reason or 'no reason given').rstrip('.')}")
        if not settled:
            reconciled = _settled_completion_response(request, job)
            if reconciled is not None:
                return reconciled
            return _durable_recovery_response(
                request,
                reason or "The interrupted download remains blocked.",
            )
        recovery = _STARTUP_RECOVERY_RESULT
        recovery_status = _recovery_status_value(recovery)
    durable_resume = (
        recovery_status == "resume_required"
        and _durable_recovery_matches_job(job)
    )
    if job.attention == "recovery" and not (
        recovery_status == "clear" or durable_resume
    ):
        return _durable_recovery_response(
            request,
            "This download needs recovery attention and cannot be retried "
            "safely. No download was started. Check the application log, "
            "then restart Qobuz Librarian.",
        )
    if recovery_status == "attention_required":
        return _durable_recovery_response(
            request,
            "The saved interrupted download needs recovery attention. No "
            "download was started. Check the application log, then restart "
            "Qobuz Librarian.",
        )
    if recovery_status == "resume_required" and not durable_resume:
        return _durable_recovery_response(
            request,
            "Saved recovery belongs to a different or changed download. No "
            "download was started. Retry only the exact interrupted job.",
        )
    if recovery_status not in {"clear", "resume_required"}:
        return _durable_recovery_response(
            request,
            "The saved recovery state could not be verified safely. No "
            "download was started. Restart Qobuz Librarian.",
        )
    busy = _lock_busy_response(
        request,
        durable_resume_job_id=job.id if durable_resume else None,
    )
    if busy is not None:
        return busy
    album_id = job.album_id
    retry_as_new = bool((job.execute_args or {}).get("new_edition"))
    duplicate = _find_job_touching_album(album_id)
    if duplicate:
        return RedirectResponse(url=f"/jobs/{duplicate.id}", status_code=303)
    try:
        token = credentials.token
        album = None
        if not durable_resume:
            from qobuz_librarian.api.client import call_within
            from qobuz_librarian.api.search import get_album

            loop = asyncio.get_running_loop()
            album = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: call_within(
                        cfg.WEB_FETCH_TIMEOUT,
                        get_album,
                        album_id,
                        token,
                    ),
                ),
                timeout=cfg.WEB_FETCH_TIMEOUT,
            )
        same_edition_complete = bool(
            album is not None
            and retry_as_new
            and await loop.run_in_executor(
                None, lambda: _same_edition_is_complete(album)
            )
        )
        # Re-check under the submit lock.
        with _DOWNLOAD_SUBMIT_LOCK, _CREDENTIAL_LOCK:
            if not _credential_generation_is_active(credentials.generation):
                message = _qobuz_action_error_message(
                    CredentialChanged(),
                    unchanged=True,
                )
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(message),
                    status_code=303,
                )
            duplicate = _find_job_touching_album(album_id)
            if duplicate:
                return RedirectResponse(url=f"/jobs/{duplicate.id}", status_code=303)
            # set_mode could have handed the lock to the terminal during the
            # get_album await above; re-check inside the submit lock (as
            # queue_download does) so a retry can't start a job after the CLI
            # handoff.
            if not _run_lock_intact():
                busy = _lock_busy_response(request)
                if busy is not None:
                    return busy
                return _durable_recovery_response(
                    request,
                    "The single-writer safety lock was lost while Retry was "
                    "preparing. No download was started. Restart Qobuz "
                    "Librarian.",
                )
            try:
                recovery_now = _record_startup_recovery(_RUN_LOCK_HANDLE)
            except Exception:
                return _durable_recovery_response(
                    request,
                    "The saved recovery state changed while Retry was "
                    "preparing and could not be checked safely. No download "
                    "was started. Restart Qobuz Librarian.",
                )
            recovery_status_now = _recovery_status_value(recovery_now)
            durable_resume_now = (
                recovery_status_now == "resume_required"
                and _durable_recovery_matches_job(job)
            )
            if recovery_submission and not _recovery_submission_matches(
                job,
                recovery_operation_id,
                recovery_item_id,
            ):
                return _durable_recovery_response(
                    request,
                    "The interrupted download changed while Retry was "
                    "preparing. No download was started. Reload the job and "
                    "try again.",
                )
            acknowledged_now = _durable_completion_status(job)
            if acknowledged_now is None:
                return _durable_recovery_response(
                    request,
                    "The saved completion record could not be checked safely. "
                    "No download was started. Restart Qobuz Librarian.",
                )
            if acknowledged_now:
                if recovery_status_now == "clear" and (
                    _reconcile_acknowledged_job(job)
                ):
                    return RedirectResponse(
                        url=f"/jobs/{job.id}", status_code=303)
                return _durable_recovery_response(
                    request,
                    "This download is already recorded as complete, but its "
                    "recovery could not be finalized safely. No download was "
                    "started. Restart Qobuz Librarian.",
                )
            if job.attention == "recovery" and not (
                recovery_status_now == "clear" or durable_resume_now
            ):
                return _durable_recovery_response(
                    request,
                    "This download needs recovery attention and cannot be "
                    "retried safely. No download was started. Restart Qobuz "
                    "Librarian.",
                )
            if recovery_status_now == "attention_required":
                return _durable_recovery_response(
                    request,
                    "The saved interrupted download needs recovery attention. "
                    "No download was started. Restart Qobuz Librarian.",
                )
            if (
                recovery_status_now == "resume_required"
                and not durable_resume_now
            ):
                return _durable_recovery_response(
                    request,
                    "The saved interrupted download no longer matches this "
                    "job. No download was started. Restart Qobuz Librarian.",
                )
            if recovery_status_now not in {"clear", "resume_required"}:
                return _durable_recovery_response(
                    request,
                    "The saved recovery state could not be verified safely. "
                    "No download was started. Restart Qobuz Librarian.",
                )
            if durable_resume and recovery_status_now == "clear":
                return _durable_recovery_response(
                    request,
                    "The saved interrupted download changed while Retry was "
                    "preparing. No download was started. Restart Qobuz "
                    "Librarian.",
                )
            durable_resume = durable_resume_now
            if not durable_resume and same_edition_complete:
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(
                        "This edition is already in your library. Nothing to retry."
                    ),
                    status_code=303,
                )
            busy = _lock_busy_response(
                request,
                durable_resume_job_id=job.id if durable_resume else None,
            )
            if busy is not None:
                return busy
            durable_planned = None
            if durable_resume:
                durable_planned = _durable_recovery_planned(job)
                if durable_planned is None:
                    return _durable_recovery_response(
                        request,
                        "The exact saved download plan could not be loaded "
                        "safely. No download was started. Restart Qobuz "
                        "Librarian.",
                    )
                album = durable_planned.get("album")
                if not isinstance(album, dict):
                    return _durable_recovery_response(
                        request,
                        "The exact saved download plan is invalid. No "
                        "download was started. Restart Qobuz Librarian.",
                    )
            elif album is None:
                return _durable_recovery_response(
                    request,
                    "The album lookup changed while Retry was preparing. No "
                    "download was started. Reload the job and try again.",
                )
            title = album.get("title") or job.title or "?"
            artist = (album.get("artist") or {}).get("name") or job.artist or "?"
            # A failed single-track download carries job.album_id (so Retry shows up),
            # but _make_download_run would download the whole album. Rebuild it as
            # the same one-track run instead.
            single = getattr(job, "single", None)
            track = None
            as_new = False
            if durable_resume and single and single.get("track_id"):
                return _durable_recovery_response(
                    request,
                    "The saved full-album recovery does not match this "
                    "single-track job. No download was started. Restart "
                    "Qobuz Librarian.",
                )
            if single and single.get("track_id"):
                tid = str(single.get("track_id"))
                track = next(
                    (t for t in (album.get("tracks") or {}).get("items") or []
                     if str(t.get("id")) == tid), None)
            if track is not None:
                run = _make_single_track_run(album, track, token)
            elif single and single.get("track_id"):
                # The original was a single-track download but that track is no
                # longer on Qobuz, so do NOT silently re-download the whole album.
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(
                        "That track is no longer on Qobuz. Nothing to retry."),
                    status_code=303)
            else:
                # Carry the "get this edition too" override across the retry,
                # without it the rebuilt run sees the album as already owned
                # and skips the download the user explicitly asked for.
                if durable_planned is not None:
                    run = _make_download_run(
                        album,
                        token,
                        durable_planned=durable_planned,
                    )
                else:
                    as_new = retry_as_new
                    run = _make_download_run(
                        album,
                        token,
                        treat_as_new=as_new,
                    )
            edition = str(
                ((track.get("version") if track is not None else None)
                 or album.get("version") or job.edition or "")
            ).strip()
            if durable_resume:
                job.edition = edition
                if not job_mgr.resubmit_failed(job, run):
                    return _durable_recovery_response(
                        request,
                        "The exact interrupted job could not be queued safely. "
                        "No download was started. Restart Qobuz Librarian.",
                    )
                new_job = job
            else:
                new_job = job_mgr.Job(
                    title=(track.get("title") or title)
                    if track is not None else title,
                    artist=artist,
                    album_id=album_id,
                    edition=edition,
                )
                if track is not None:
                    # Seed the same two keys /download does at submit and let
                    # the run fill in the rest. Copying the whole dict carried
                    # the old job's Undo record onto a job that may download
                    # nothing (the "you already have this" early return never
                    # touches j.single), so two jobs offered Undo of one file
                    # and one of them claimed it downloaded nothing.
                    new_job.single = {
                        "album_id": album_id,
                        "track_id": str(single.get("track_id")),
                    }
                if as_new:
                    new_job.execute_args = {"new_edition": True}
                if job_mgr.submit(new_job, run) is None:
                    return _job_admission_response(request)
        return RedirectResponse(url=f"/jobs/{new_job.id}", status_code=303)
    except NoCredsError as exc:
        message = _qobuz_action_error_message(exc, unchanged=True)
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(message), status_code=303
        )
    except Exception as exc:
        message = _download_error_message(
            exc,
            "Couldn't prepare this retry. Try again.",
        )
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(message),
            status_code=303,
        )


@app.post("/jobs/{job_id}/undo")
async def job_undo(request: Request, job_id: str):
    """Reverse a single-track download whose exact owned path is still bound."""
    # Undo deletes files and touches the beets DB, so it needs the same run-lock
    # gate every other mutating route has; the in-process staging lock below
    # can't keep it off the library while a CLI session or another instance
    # holds the cross-process lock.
    busy = _lock_busy_response(request)
    if busy is not None:
        if _is_htmx(request):
            return HTMLResponse(
                f'<div id="job-content">{busy.body.decode()}</div>')
        return busy
    # The single payload is persisted, so Undo keeps working after the job
    # ages out of the registry, and the file checks below already handle a track
    # that vanished in the meantime.
    job = job_mgr.registry.get(job_id) or job_mgr.load_historical_job(job_id)
    undo_uncertain = bool(
        job
        and (
            getattr(job, "_preserve_persisted_single", False) is True
            or getattr(job, "_single_undo_unavailable", False) is True
        )
    )
    info = (
        dict(getattr(job, "single", None) or {})
        if job and not undo_uncertain
        else {}
    )
    catalog_cleanup = info.get("catalog_cleanup")
    cleanup_retry = (
        type(catalog_cleanup) is dict
        and catalog_cleanup.get("pending") is True
        and isinstance(catalog_cleanup.get("path"), str)
        and bool(catalog_cleanup["path"])
    )
    if (
        not job
        or not info.get("dir")
        or (info.get("removed") and not cleanup_retry)
    ):
        if _is_htmx(request):
            if job:
                return _tr(request, "_job_body.html", {"job": job})
            return HTMLResponse("", headers={"HX-Redirect": "/queue"})
        msg = ("That job is no longer in the record." if not job
               else "Nothing to undo for that job.")
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote(msg), status_code=303)

    def _refresh_after_undo():
        import logging

        from qobuz_librarian.web import flows

        artist = info.get("artist") or ""
        album = {
            "title": info.get("album") or "",
            "artist": {"name": artist},
        }
        try:
            flows._refresh_after_local_album_change(
                album,
                {"dir": info.get("dir") or ""},
                fallback_artist=artist,
                token=_get_optional_token(),
                args=flows.build_args(),
                upgrade=True,
                downsample=True,
            )
        except Exception as exc:
            logging.getLogger("qobuz_librarian").info(
                "quality state refresh after undo skipped: %s", exc)

    undo_outcome = {}

    def _clear_deliberate_single_mark() -> bool:
        """Return only after the exact suppression mark is durably absent."""
        if not info.get("marked"):
            return True
        from qobuz_librarian.library import hidden as hidden_mod
        try:
            hidden_mod.unmark_single(
                info.get("artist") or "",
                info.get("album") or "",
                year=info.get("year"),
                album_id=info.get("album_id"),
            )
            store = hidden_mod.load()
            return not hidden_mod.is_single(
                info.get("artist") or "",
                info.get("album") or "",
                store,
                year=info.get("year"),
                album_id=info.get("album_id"),
            )
        except (OSError, TypeError, ValueError):
            return False

    def _reverse():
        from pathlib import Path

        from qobuz_librarian.integrations.beets import forget_beets_entries
        from qobuz_librarian.web import job_persistence
        if cleanup_retry:
            catalog_result = forget_beets_entries(
                [Path(catalog_cleanup["path"])]
            )
            undo_outcome["single_mark_complete"] = (
                _clear_deliberate_single_mark()
            )
            return None, catalog_result
        owned_root = info.get("owned_root")
        if owned_root is not None:
            current_root = os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
            if (
                not isinstance(owned_root, str)
                or os.path.abspath(owned_root) != current_root
            ):
                return None, None
            d = Path(current_root)
        else:
            d = Path(info["dir"])
        # Every intent and state/identity refresh reaches the durable job row
        # while the direct-operation lock still excludes another library
        # writer.
        job.single = info

        def _persist_progress():
            return job_persistence.persist(job)

        removed = _unlink_owned_path(
            d,
            info.get("owned_path"),
            progress=_persist_progress,
            outcome_out=undo_outcome,
        )
        catalog_result = None
        if removed is not None:
            catalog_result = forget_beets_entries([removed])
            undo_outcome["single_mark_complete"] = (
                _clear_deliberate_single_mark()
            )
        return removed, catalog_result

    # Register under the same gate as the CLI handoff before taking the
    # staging mutex.
    loop = asyncio.get_running_loop()
    state, operation_token, lock = await loop.run_in_executor(
        None, lambda: _begin_direct_library_operation("Undo"))
    if state == "paused":
        paused = _lock_busy_response(request)
        if paused is not None:
            return paused
        return _tr(request, "lock_busy.html", {
            "msg": "Library writes were paused before Undo could start."
        }, status_code=503)
    if state == "busy":
        holder = job_mgr.staging_holder()
        msg = (f"{holder} is using the library right now. Try Undo again "
               "when it finishes." if holder else
               "Another job is using the library right now. Try Undo again "
               "in a moment.")
        if _is_htmx(request):
            return HTMLResponse(
                f'<div id="job-content">'
                f'{_ql_notice_html("warning", html.escape(msg))}</div>')
        return _tr(request, "lock_busy.html", {"msg": msg}, status_code=503)
    lock_held = True
    try:
        removed, catalog_result = await loop.run_in_executor(None, _reverse)
        catalog_complete = bool(
            getattr(catalog_result, "complete", catalog_result)
        )
        single_mark_complete = undo_outcome.get(
            "single_mark_complete", True
        )
        refresh_needed = False
        if cleanup_retry:
            if catalog_complete and single_mark_complete:
                completed = {**info, "removed": True}
                completed.pop("catalog_cleanup", None)
                job.single = completed
                if job.attention == "catalog":
                    job.attention = ""
                job.summary = (
                    f"Removed “{info.get('title')}” and undid the single."
                )
            elif catalog_complete:
                job.single = {**info, "removed": False}
                job.single.pop("catalog_cleanup", None)
                if job.attention == "catalog":
                    job.attention = ""
                job.summary = (
                    f"Removed “{info.get('title')}”, but couldn't save the "
                    "single mark cleanup. Retry Undo."
                )
            else:
                job.attention = "catalog"
                job.summary = (
                    f"Removed “{info.get('title')}”, but couldn't clear its "
                    "stale Beets catalogue entry. Retry catalogue cleanup."
                )
        elif removed is not None:
            refresh_needed = True
            if catalog_complete and single_mark_complete:
                job.single = {**info, "removed": True}
                job.summary = (
                    f"Removed “{info.get('title')}” and undid the single."
                )
            elif catalog_complete:
                job.single = {**info, "removed": False}
                job.summary = (
                    f"Removed “{info.get('title')}”, but couldn't save the "
                    "single mark cleanup. Retry Undo."
                )
            else:
                job.single = {
                    **info,
                    "removed": True,
                    "catalog_cleanup": {
                        "pending": True,
                        "path": str(removed),
                    },
                }
                job.attention = "catalog"
                job.summary = (
                    f"Removed “{info.get('title')}”, but couldn't clear its "
                    "stale Beets catalogue entry. Retry catalogue cleanup."
                )
        else:
            # If the whole recorded directory is gone, clearing the single
            # mark cannot delete anything.
            from pathlib import Path as _Path
            dir_gone = (
                info.get("owned_root") is None
                and not _Path(info["dir"]).exists()
            )
            if dir_gone:
                single_mark_complete = await loop.run_in_executor(
                    None, _clear_deliberate_single_mark
                )
                refresh_needed = True
                job.single = {
                    **info,
                    "removed": bool(single_mark_complete),
                }
                if single_mark_complete:
                    job.summary = (f"“{info.get('title')}” was already gone; "
                                   "cleared the single mark.")
                else:
                    job.summary = (
                        f"“{info.get('title')}” was already gone, but couldn't "
                        "save the single mark cleanup. Retry Undo."
                    )
            else:
                if undo_outcome.get("files_complete"):
                    job.summary = (
                        f"Removed “{info.get('title')}”, but couldn't safely "
                        "finish cleaning up its folders. Try Undo again.")
                elif undo_outcome.get("removed_files"):
                    job.summary = (
                        "Part of Undo completed, but couldn't safely finish "
                        f"removing “{info.get('title')}”. Try Undo again.")
                elif undo_outcome.get("held_files"):
                    job.summary = (
                        "Undo safely set the downloaded copy of "
                        f"“{info.get('title')}” aside, but couldn't finish "
                        "removing it. Try Undo again.")
                elif undo_outcome.get("undo_started"):
                    job.summary = (
                        "Undo started but couldn't safely finish removing "
                        f"“{info.get('title')}”. Try Undo again.")
                else:
                    job.summary = (
                        "Couldn't safely verify the downloaded copy of "
                        f"“{info.get('title')}”. Nothing was removed; delete "
                        "it manually if needed.")
        # Persist while the direct-operation registration still holds the web
        # run lock; a restart must not resurrect an Undo that already removed a
        # file or cleared its single mark.
        from qobuz_librarian.web import job_persistence
        saved = await loop.run_in_executor(
            None, lambda: job_persistence.persist(job)
        )
        if not saved:
            msg = (
                "Undo changed the saved track state, but its final record "
                "couldn't be saved. Retry Undo before clearing History."
            )
            if _is_htmx(request):
                return HTMLResponse(
                    f'<div id="job-content">'
                    f'{_ql_notice_html("warning", html.escape(msg))}</div>',
                    status_code=503,
                )
            return _tr(request, "lock_busy.html", {
                "reason": "Undo needs attention",
                "msg": msg,
                "action": {"href": f"/jobs/{job.id}", "label": "Retry Undo"},
            }, status_code=503)
        lock.release()
        lock_held = False
        if refresh_needed:
            await loop.run_in_executor(None, _refresh_after_undo)
        if _is_htmx(request):
            return _tr(request, "_job_body.html", {"job": job})
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    finally:
        if lock_held:
            lock.release()
        job_mgr.end_library_operation(operation_token)


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(
    request: Request,
    job_id: str,
    return_to: str = Form(""),
):
    return_to_queue = return_to == "/queue"
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    was_review = job.status == job_mgr.JobStatus.AWAITING_REVIEW
    was_pending = job.status == job_mgr.JobStatus.PENDING
    # Offload: cancelling a parked review runs cancel_review -> persist (a
    # json.dumps of the full candidate list + SQLite commit), which would block
    # the event loop and stall every SSE stream for a large review.
    loop = asyncio.get_running_loop()
    protected = job_mgr.cancel_is_protected(job)
    canceled = await loop.run_in_executor(
        None, lambda: job_mgr.request_cancel(job)
    )
    if not canceled:
        if protected:
            message = (
                "This interrupted-download recovery cannot be canceled until "
                "its saved step settles."
            )
        else:
            message = (
                "That job could not be canceled. Its state may have changed, "
                "or the update could not be saved."
            )
        dest = "/queue" if return_to_queue else f"/jobs/{job_id}"
        return RedirectResponse(
            url=dest + "?error=" + urllib.parse.quote(message),
            status_code=303,
        )
    if return_to_queue:
        return RedirectResponse(url="/queue", status_code=303)
    # Repair stays on its single surface either way (idle start form once the
    # cancel lands).
    if job.execute_kind == "repair":
        dest = "/repair"
    elif job.execute_kind in _LIBRARY_SURFACE_KINDS:
        dest = "/library"
    elif was_review and job.execute_kind in ("upgrade", "downsample"):
        dest = f"/{job.execute_kind}"
    elif was_review and job.execute_kind == "migration":
        dest = "/migrate"
    else:
        dest = "/queue" if (was_review or was_pending) else f"/jobs/{job_id}"
    return RedirectResponse(url=dest, status_code=303)


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, error: str = "", notice: str = ""):
    """The Queue tab: jobs in flight (pending / scanning / running / awaiting
    review). Finished jobs live in the History tab, which reads the durable
    archive rather than the capped in-memory set."""
    pending = job_mgr.registry.pending_and_running()
    protected_id = job_mgr.durable_recovery_job_id()
    return _tr(request, "queue.html", {
        "pending": pending,
        # Per-pending-job "waiting behind X" explainer, the same one the single
        # job page shows, so the Queue list says why a job hasn't started
        # instead of a bare "Queued". None for anything already running.
        "queue_waits": {j.id: _queue_wait(j) for j in pending},
        "queue_has_cancel_protected": any(
            j.id == protected_id for j in pending
        ),
        "error": error[:200],
        "notice": notice[:200],
        "page": "queue",
        "active_tab": "queue",
    })


_HISTORY_PER_PAGE = 30
_HISTORY_BULK_CAP = 40


@app.get("/queue/history", response_class=HTMLResponse)
async def queue_history(
    request: Request,
    p: int = 1,
    jp: int = 1,
    error: str = "",
):
    """The History tab: every finished job, newest first, paged from jobs.db so
    the record outlives the in-memory cap (which only the Queue/SSE views use).
    ``p`` walks the downloads table, ``jp`` the job cards above it, and each
    pager's links carry the other's page."""
    from qobuz_librarian.web import job_persistence
    p = max(1, p)
    jp = max(1, jp)

    def _stamp(rows):
        for r in rows:
            ts = r.get("finished_at") or r.get("created_at")
            r["when"], r["when_exact"] = _when_label(ts)
        return rows

    def _load_page(page, bulk_page):
        # Two layers: meaningful jobs as cards, plain downloads as the table
        # underneath. Both walk the archive a page at a time.
        recoveries = _stamp(job_persistence.recovery_history())
        bulk_rest = job_persistence.history_count(
            bulk=True, exclude_recoveries=True)
        bulk_pages = max(
            1, (bulk_rest + _HISTORY_BULK_CAP - 1) // _HISTORY_BULK_CAP)
        bulk_page = min(max(1, bulk_page), bulk_pages)
        # A retained recovery is asking for a decision, so it stays pinned to
        # the first page rather than repeating under every one.
        bulk = (recoveries if bulk_page == 1 else []) + _stamp(
            job_persistence.history_page(
                _HISTORY_BULK_CAP,
                (bulk_page - 1) * _HISTORY_BULK_CAP,
                bulk=True,
                exclude_recoveries=True,
            ))
        total = job_persistence.history_count(
            bulk=False, exclude_recoveries=True)
        # Count the archive, not the cards that happened to render: the card
        # layer is capped, so a headline built from it under-reported the
        # history by however much it had dropped.
        bulk_total = len(recoveries) + bulk_rest
        pages = max(1, (total + _HISTORY_PER_PAGE - 1) // _HISTORY_PER_PAGE)
        page = min(max(1, page), pages)
        rows = _stamp(job_persistence.history_page(
            _HISTORY_PER_PAGE,
            (page - 1) * _HISTORY_PER_PAGE,
            bulk=False,
            exclude_recoveries=True,
        ))
        return (bulk, bulk_total, bulk_page, bulk_pages,
                total, pages, page, rows)

    loop = asyncio.get_running_loop()
    (bulk_jobs, bulk_total, jp, bulk_pages,
     total, pages, p, rows) = await loop.run_in_executor(
        None, lambda: _load_page(p, jp))
    return _tr(request, "history.html", {
        "page": "queue", "active_tab": "history",
        "bulk_jobs": bulk_jobs, "jobs": rows,
        "bulk_total": bulk_total, "bulk_shown": len(bulk_jobs),
        "bulk_page": jp, "bulk_pages": bulk_pages,
        "cur_page": p, "pages": pages, "total": total,
        "error": error[:200],
    })


@app.post("/queue/clear")
async def queue_clear(request: Request):
    """Clear the History: drop finished/canceled/failed jobs from the registry
    and the full on-disk archive. In-flight jobs are untouched."""
    from qobuz_librarian.web import job_persistence
    with _STARTUP_RECOVERY_LOCK:
        if _startup_recovery_status_value() != "clear":
            return _durable_recovery_response(
                request,
                "History cannot be cleared while an interrupted download still "
                "has saved recovery state. Retry or settle that download first.",
            )
        retained_job_id = job_mgr.durable_recovery_job_id()
        if not job_persistence.clear_history(retain_job_id=retained_job_id):
            message = (
                "History couldn't be cleared from the data folder. Nothing "
                "was removed; check the data volume and try again."
            )
            return RedirectResponse(
                url="/queue/history?error=" + urllib.parse.quote(message),
                status_code=303,
            )
        job_mgr.registry.clear_finished()
    return RedirectResponse(url="/queue/history", status_code=303)


@app.post("/queue/cancel-pending")
async def queue_cancel_pending():
    # Parked reviews are deliberately exempt: the queue page no longer shows
    # them, and a bulk clear must never take something the user can't see.
    protected = 0
    unsaved = 0
    for j in list(job_mgr.registry.pending_and_running()):
        if j.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        was_protected = job_mgr.cancel_is_protected(j)
        if not job_mgr.request_cancel(j):
            if was_protected:
                protected += 1
            else:
                unsaved += 1
    if protected or unsaved:
        parts = ["Queue cleared where safe."]
        if protected:
            parts.append(
                "The interrupted-download recovery stays in place until its "
                "saved step settles."
            )
        if unsaved:
            noun = "job" if unsaved == 1 else "jobs"
            parts.append(
                f"{unsaved} {noun} couldn't be canceled because the update "
                "couldn't be saved."
            )
        message = " ".join(parts)
        return RedirectResponse(
            url="/queue?notice=" + urllib.parse.quote(message), status_code=303
        )
    return RedirectResponse(url="/queue", status_code=303)


def _diagnostics():
    """Read-only health checks surfaced on the Settings page."""
    import os
    import shutil as _sh

    checks = []

    def _dir_check(label, path, *, want_writable):
        p = Path(path)
        if not p.exists():
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} does not exist (volume not mounted?)"})
            return
        if not p.is_dir():
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} exists but is not a directory"})
            return
        if want_writable and not os.access(p, os.W_OK):
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} is not writable by the container user. "
                           "On a NAS, set PUID/PGID in .env to your media-share owner"})
            return
        try:
            n = sum(1 for _ in p.iterdir())
        except OSError as e:
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} unreadable: {e}"})
            return
        checks.append({"label": label, "ok": True,
                       "detail": f"{p}: {n} entr{'y' if n == 1 else 'ies'}"})

    _dir_check("Music library", cfg.MUSIC_ROOT, want_writable=True)
    _dir_check("Staging area", cfg.STAGING_DIR, want_writable=True)

    beets_db = Path(cfg.BEETS_DB_PATH)
    if beets_db.exists():
        ok = os.access(beets_db, os.R_OK)
        checks.append({"label": "Beets database", "ok": ok,
                       "detail": f"{beets_db}" if ok
                       else f"{beets_db} exists but is not readable"})
    elif beets_db.parent.exists():
        checks.append({"label": "beets DB (BEETS_DB_PATH)", "ok": True,
                       "detail": f"{beets_db} (created on first import)"})
    else:
        checks.append({"label": "beets DB (BEETS_DB_PATH)", "ok": False,
                       "detail": f"{beets_db.parent} does not exist"})

    for binary in ("rip", "ffmpeg", "flac"):
        found = _sh.which(binary)
        checks.append({"label": f"{binary} binary",
                       "ok": bool(found),
                       "detail": found or f"{binary} not on PATH. "
                       "Rebuild the image (docker compose build)"})
    beets_python, beets_detail = _beets_runtime_diagnostic()
    checks.append({
        "label": "Beets 2.12.0 runtime",
        "ok": beets_python is not None,
        "detail": beets_detail,
    })

    stranded = []
    if cfg.UPGRADE_BACKUP_DIR.exists():
        try:
            for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
                if entry.is_dir() and (entry.suffix == ".partial"
                                       or entry.name == ".restore_trash"):
                    stranded.append(entry)
        except OSError:
            pass
    if stranded:
        checks.append({"label": "Stranded upgrade backups", "ok": False,
                       "detail": f"{len(stranded)} found in "
                                 f"{cfg.UPGRADE_BACKUP_DIR}; manual cleanup needed"})
    else:
        checks.append({"label": "Stranded upgrade backups", "ok": True,
                       "detail": "none"})

    # Backups whose original is still missing the tracks they hold: orphaned
    # by a hard kill that skipped the restore/delete.
    try:
        from qobuz_librarian.library.backup import find_only_copy_backups
        orphans = find_only_copy_backups()
    except Exception:
        orphans = []
    interrupted_disposals = [
        item for item in orphans
        if item[0].name.startswith(".ql-dispose-backup-")
    ]
    orphans = [
        item for item in orphans
        if not item[0].name.startswith(".ql-dispose-backup-")
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
    else:
        checks.append({"label": "Backups needing review", "ok": True,
                       "detail": "none"})
    return checks


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


def _settings_response(request, *, saved=False, queued=False, connected=False,
                       unverified=False, error="", mode="", user_id=None,
                       auth_token_prefill="", diagnostics=None, warnings=None,
                       quality_note=False):
    from qobuz_librarian.ui_cli.colors import format_size
    from qobuz_librarian.web import settings_store
    creds = _read_creds()
    values = settings_store.current()
    # If credentials come from environment or a secret-file declaration,
    # anything saved via the form lacks authority, so let the user know.
    import os
    creds_from_env = _qobuz_token_is_env_owned()
    cli_only_env = os.environ.get("QL_CLI_ONLY", "").strip().lower() in (
        "1", "true", "yes", "on")
    # Two separate facts. disk_usage() measures the FILESYSTEM the music folder
    # sits on, never the folder. It was labelled "Music folder: 3.31 TB used"
    # while the folder held 1.6 MB. The library's own size comes from the census
    # the Library page already shows, and the volume figure is labelled by what
    # it actually covers: its own mount (the usual Docker bind, or a dataset
    # with a quota) or a disk shared with everything else on the machine.
    music_storage = None
    try:
        du = shutil.disk_usage(cfg.MUSIC_ROOT)

        music_storage = {
            "free": format_size(du.free), "total": format_size(du.total),
            "pct": round(du.used / du.total * 100, 1) if du.total else 0,
            "own_volume": _is_mount_point(cfg.MUSIC_ROOT),
        }
    except OSError:
        pass
    census = _census_view()
    library_size = census.get("total") if census else ""
    return _tr(request, "settings.html", {
        "music_storage": music_storage,
        "library_size": library_size,
        # True once Qobuz has accepted the saved token, False once it has
        # rejected it, None when it has never been asked.
        "token_verified": _token_valid_for(),
        "user_id": creds.get("user_id", "") if user_id is None else user_id,
        "auth_token_set": bool(creds.get("auth_token")),
        "downloader_ready": bool(
            creds.get("auth_token") and creds.get("user_id")
        ),
        "auth_token_prefill": auth_token_prefill,
        "creds_from_env": creds_from_env,
        "env_user_id_set": bool(
            cfg.QOBUZ_USER_ID or os.environ.get("QOBUZ_USER_ID", "").strip()
        ),
        "cli_only_env": cli_only_env,
        "mode_changed": (mode or "").strip().lower(),
        "saved": saved,
        "queued": queued,
        "quality_note": quality_note,
        "connected": connected,
        "unverified": unverified,
        "error": error,
        "warnings": warnings or [],
        "page": "settings",
        "library_paths": [
            {"label": label, "container": cp,
             "host": host, "resolved": resolved}
            for label, cp in (
                ("Music library", cfg.MUSIC_ROOT),
                ("Staging area", cfg.STAGING_DIR),
                ("Beets database", cfg.BEETS_DB_PATH),
                ("Streamrip config", cfg.STREAMRIP_CONFIG),
            )
            for host, resolved in [_resolve_host_path(cp)]
        ],
        "behavior_fields": settings_store.BEHAVIOR_FIELDS,
        "text_fields": settings_store.TEXT_FIELDS,
        "option_labels": settings_store.ENUM_OPTION_LABELS,
        "behavior": values,
        # The worst store to lose without being told: quality tier and
        # downsample policy revert to the env defaults and this page then shows
        # them as if they were chosen.
        "corrupt_stores": state_file.preserved_corrupt_stores(),
        "diagnostics_html": _diagnostics_fragment(request, diagnostics),
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = False,
                        queued: bool = False, connected: bool = False,
                        unverified: bool = False, error: str = "",
                        mode: str = "", quality_note: bool = False):
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, _diagnostics)
    return _settings_response(request, saved=saved, queued=queued,
                              connected=connected, unverified=unverified,
                              error=error, mode=mode, diagnostics=diags,
                              quality_note=quality_note)


def _streamrip_has_userid() -> bool:
    """True if the streamrip config carries a non-empty user id, so `rip` can
    actually authenticate a download. A token-only env (QOBUZ_USER_AUTH_TOKEN set,
    QOBUZ_USER_ID unset) has none until the id is set or creds are saved, even
    though the app's own Qobuz API calls work from the token alone."""
    from qobuz_librarian.api.auth import read_qobuz_credentials

    return read_qobuz_credentials().downloader_ready


def _qobuz_token_is_env_owned() -> bool:
    """Whether environment configuration owns the Qobuz token slot.

    The *_FILE declaration counts even when its secret mount is temporarily
    unreadable. Treating that state as form-owned permits a shadow credential
    that silently loses authority when the mount recovers or the app restarts.
    """
    return bool(
        cfg.QOBUZ_USER_AUTH_TOKEN
        or os.environ.get("QOBUZ_USER_AUTH_TOKEN", "").strip()
        or os.environ.get("QOBUZ_USER_AUTH_TOKEN_FILE", "").strip()
    )


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request, user_id: str = Form(""), auth_token: str = Form("")):
    global _TOKEN_GENERATION, _TOKEN_VALID
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, _diagnostics)
    existing = _read_creds()
    # Environment and secret-file credentials are authoritative for the live
    # process. Refuse a form value that claims to replace one: writing it only
    # to streamrip would report success while the app kept using the env value,
    # and startup sync would overwrite the shadow value again. A token-only env
    # may still use this page to supply streamrip's required user id.
    env_owned = _qobuz_token_is_env_owned()
    env_token = cfg.QOBUZ_USER_AUTH_TOKEN
    env_user_id = cfg.QOBUZ_USER_ID
    if env_owned and (
        not env_token
        or (auth_token.strip() and auth_token.strip() != env_token)
        or (env_user_id and user_id.strip() and user_id.strip() != env_user_id)
    ):
        return _settings_response(
            request,
            error="envcreds",
            user_id=existing.get("user_id", ""),
            auth_token_prefill="",
            diagnostics=diags,
        )
    # First-run with empty inputs: nothing to save and no creds to keep,
    # bounce back with a banner rather than writing blanks and flashing green.
    if not auth_token.strip() and not user_id.strip() \
            and not existing.get("auth_token") \
            and not cfg.QOBUZ_USER_AUTH_TOKEN:
        return RedirectResponse(url="/settings?error=empty", status_code=303)
    # Blank means "keep the existing value": the fields are not pre-filled,
    # so an empty submission must not wipe a previously-saved credential.
    if not auth_token.strip() and not user_id.strip() and cfg.QOBUZ_USER_AUTH_TOKEN:
        # Blank submit with an env token = "keep the env creds".
        return RedirectResponse(url="/settings?connected=1", status_code=303)
    new_token = auth_token.strip() or existing.get("auth_token", "")
    new_uid = user_id.strip() or existing.get("user_id", "")
    if new_uid and not new_token:
        return _settings_response(request, error="empty",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    # Check the token with Qobuz *before* writing it.
    verdict = AuthOutcome.TEMPORARY
    if new_token:
        from qobuz_librarian.api.client import call_within
        probe = credentials_from_values(
            new_uid,
            new_token,
            source="env" if env_owned else "streamrip",
        )
        try:
            verdict = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: call_within(cfg.WEB_TEST_AUTH_TIMEOUT,
                                              _classify_token, probe.token)),
                timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            verdict = AuthOutcome.TEMPORARY
    if verdict == AuthOutcome.REJECTED:
        # Re-render with the real token still in the (password-type, so
        # visually masked) field so the user can fix a paste slip without
        # re-typing it, same as the needuser/empty/creds branches.
        return _settings_response(request, error="rejected",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    if (verdict in {AuthOutcome.TEMPORARY, AuthOutcome.INCONCLUSIVE}
            and new_token and _token_valid_for() is True
            and new_token != existing.get("auth_token", "")):
        # Couldn't check it, and the token already saved is one that has
        # authenticated. Overwriting a known-good credential with an unproven
        # one, and then reporting "Connected", is how a working install
        # became a broken one during a network blip. A save that keeps the
        # same token (blank field, or a user-id-only edit) overwrites
        # nothing and passes.
        return _settings_response(request, error="unreachable",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    with _CREDENTIAL_LOCK:
        active_credentials = _credentials_snapshot()
        candidate_credentials = credentials_from_values(
            new_uid,
            new_token,
            source="env" if env_owned else "streamrip",
        )
        credential_work_running = any(
            (getattr(job, "execute_kind", "") or "download")
            not in {"downsample", "lyrics", "migration"}
            for job in job_mgr.registry.executing()
        )
        if (
            candidate_credentials.generation
            != active_credentials.generation
            and credential_work_running
        ):
            return _settings_response(
                request,
                error="credsbusy",
                user_id=user_id.strip(),
                auth_token_prefill=auth_token.strip(),
                diagnostics=diags,
            )
        ok = _write_creds(new_uid, new_token)
        if not ok:
            return _settings_response(request, error="creds",
                                      user_id=user_id.strip(),
                                      auth_token_prefill=auth_token.strip(),
                                      diagnostics=diags)
        saved_credentials = _credentials_snapshot()
        if verdict not in {AuthOutcome.ACCEPTED, AuthOutcome.ENTITLEMENT}:
            _TOKEN_VALID = None
            _TOKEN_GENERATION = saved_credentials.generation
    if verdict in {AuthOutcome.ACCEPTED, AuthOutcome.ENTITLEMENT}:
        _on_auth_state(AuthEvidence(saved_credentials.generation, verdict))
    suffix = (
        "&unverified=1"
        if verdict in {AuthOutcome.TEMPORARY, AuthOutcome.INCONCLUSIVE}
        else ""
    )
    return RedirectResponse(url=f"/settings?connected=1{suffix}", status_code=303)


@app.post("/settings/behavior", response_class=HTMLResponse)
async def save_behavior(request: Request):
    from qobuz_librarian.web import settings_store
    form = await request.form()
    def _posted_bool(key):
        return form.get(key, "").strip().lower() not in (
            "0", "false", "off", "no", ""
        )
    # The real Settings form ships a hidden form_complete=1 marker.
    is_complete = "form_complete" in form
    if is_complete:
        values = {k: (_posted_bool(k) if k in form else False)
                  for k in settings_store.BEHAVIOR_KEYS}
    else:
        values = {k: _posted_bool(k)
                  for k in settings_store.BEHAVIOR_KEYS if k in form}
    # Text/enum/list fields: take whatever the form posted; absent =
    # leave unchanged (don't wipe a previously-set value).
    for k in settings_store.TEXT_KEYS:
        if k in form:
            values[k] = form.get(k, "")
    effective_before = settings_store.current()
    quality_before = (
        str(effective_before.get("STREAMRIP_QUALITY", "")),
        bool(effective_before.get("PREFER_HIRES", False)),
    )
    ok, warnings = settings_store.save(values)
    if ok is None:
        return RedirectResponse(
            url="/settings?error=invalidsettings#behaviour", status_code=303)
    # A quality-policy change leaves a parked/saved Upgrade review promising
    # targets the settings no longer produce.
    quality_note = False
    effective_after = settings_store.current()
    if quality_before != (
        str(effective_after.get("STREAMRIP_QUALITY", "")),
        bool(effective_after.get("PREFER_HIRES", False)),
    ):
        from qobuz_librarian.quality import upgrade_state
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, upgrade_state.load)
        quality_note = bool((state or {}).get("candidates"))
    # Durable publication is the settings store's admission point; failure
    # leaves both the live config and any deferred overlay unchanged.
    if not ok:
        return RedirectResponse(url="/settings?error=persist#behaviour", status_code=303)
    if warnings:
        # Re-render in place so we can name exactly which entries were dropped
        # (a misspelt provider, an uninstalled beets plugin) without smuggling
        # user-typed values through the redirect URL.
        loop = asyncio.get_running_loop()
        diags = await loop.run_in_executor(None, _diagnostics)
        return _settings_response(request, saved=True,
                                  queued=settings_store._any_active_job(),
                                  warnings=warnings, diagnostics=diags,
                                  quality_note=quality_note)
    suffix = "&queued=1" if settings_store._any_active_job() else ""
    if quality_note:
        suffix += "&quality_note=1"
    return RedirectResponse(url=f"/settings?saved=1{suffix}#behaviour", status_code=303)


@app.post("/settings/mode")
async def set_mode(request: Request, target: str = Form("")):
    """Hand the run-lock to the terminal (CLI), or take it back for the web.

    Switching to CLI is refused while a download/scan is active: releasing the
    lock under a running job would let the CLI race the worker over /staging.
    """
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE, _creds_cache, \
        _LOCK_UNENFORCEABLE
    from qobuz_librarian import run_lock
    want = (target or "").strip().lower()
    if want == "cli":
        # Flip to CLI mode first so a /download or scan POST landing during
        # the handoff is refused (503) instead of slipping past the check and
        # racing the CLI over /staging once we release the lock below.
        with _auto_check_lock:
            _CLI_MODE = True

        def _handoff():
            global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE
            with _auto_check_lock:
                # Only work in flight blocks the handoff; the race this
                # guards against is the CLI and a running worker sharing
                # /staging.
                jobs_active = any(
                    j.status != job_mgr.JobStatus.AWAITING_REVIEW
                    for j in job_mgr.registry.pending_and_running()
                )
                if jobs_active or job_mgr.active_library_operations():
                    _CLI_MODE = False  # no transfer happened; stay in web mode
                    return False
                if _RUN_LOCK_HANDLE is not None:
                    try:
                        _RUN_LOCK_HANDLE.close()  # closing releases the flock
                    except OSError:
                        pass
                    _RUN_LOCK_HANDLE = None
                _LOCK_BUSY_PID = None
                return True

        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, _handoff):
            return RedirectResponse(url="/settings?error=" + urllib.parse.quote(
                "Finish or cancel the running library work before handing off to the "
                "terminal."), status_code=303)
        return RedirectResponse(url="/settings?mode=cli", status_code=303)
    if want == "nolock":
        # Durable recovery cannot inspect or reconcile saved work without
        # exact single-writer authority.
        return RedirectResponse(
            url="/settings?error=" + urllib.parse.quote(
                "The safety lock is required. Fix the data-folder filesystem "
                "or permissions, then restart Qobuz Librarian."),
            status_code=303,
        )
    if want == "web":
        with _auto_check_lock:
            prior_cli_mode = _CLI_MODE
        try:
            lease = run_lock.acquire()
            if lease is None:
                # Can't enforce the lock, same stance as startup: pause
                # destructive routes until the filesystem can enforce it.
                with _auto_check_lock:
                    _RUN_LOCK_HANDLE = None
                    _LOCK_BUSY_PID = None
                    _LOCK_UNENFORCEABLE = True
                    _CLI_MODE = False
            else:
                try:
                    with _auto_check_lock:
                        _recover_under_web_run_lock(lease)
                        _LOCK_BUSY_PID = None
                        _LOCK_UNENFORCEABLE = False
                        _CLI_MODE = False
                except Exception:
                    with _auto_check_lock:
                        _RUN_LOCK_HANDLE = None
                        _LOCK_BUSY_PID = None
                        _LOCK_UNENFORCEABLE = False
                        _CLI_MODE = prior_cli_mode
                    logging.getLogger("qobuz_librarian").exception(
                        "couldn't resume Web mode because durable recovery "
                        "could not be read"
                    )
                    return RedirectResponse(
                        url="/settings?error=" + urllib.parse.quote(
                            "Saved recovery state could not be checked. Web "
                            "mode stayed paused and its safety lock was "
                            "released; check the data-folder permissions, "
                            "then try again."
                        ),
                        status_code=303,
                    )
            # The CLI may have changed the saved token while it held the lock;
            # drop the cached creds so the banner reflects what's on disk now.
            _creds_cache = None
            return RedirectResponse(url="/settings?mode=web", status_code=303)
        except run_lock.LockBusy:
            # A CLI session still holds the lock, so we can't take it back yet.
            return RedirectResponse(url="/settings?error=" + urllib.parse.quote(
                "The terminal is still using it. Finish your CLI command, then "
                "resume."), status_code=303)
    return RedirectResponse(url="/settings", status_code=303)


# Empty 500ms ticks before we emit a `: ping` heartbeat to keep reverse
# proxies from dropping the EventSource on a quiet scan.
_SSE_HEARTBEAT_TICKS = cfg.SSE_HEARTBEAT_TICKS

# Dedicated thread pool for SSE waits so a long-running scan with many
# tabs open doesn't starve /search and /download on the default executor.
_SSE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=cfg.SSE_MAX_WORKERS, thread_name_prefix="sse")


def _diagnostics_fragment(request: Request, checks: list | None = None) -> str:
    """The diagnostics list items, plus a Restore row per orphaned backup.

    Shared by the Settings page render, the Recheck partial, and the restore
    POST below, which re-renders the list in place so a restored backup
    disappears from it without a page reload."""
    if checks is None:
        checks = _diagnostics()
    rows = []
    for d in checks:
        icon = "OK" if d["ok"] else "!"
        cls = "ql-diagnostic-status-ok" if d["ok"] else "ql-diagnostic-status-error"
        aria = "OK" if d["ok"] else "Needs attention"
        detail = f'<div class="ql-diagnostic-detail">{html.escape(d.get("detail") or "")}</div>' if d.get("detail") else ""
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status {cls}" aria-label="{aria}">{icon}</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">{html.escape(d["label"])}</div>{detail}</div>'
            f'</div>'
        )
    try:
        from qobuz_librarian.library.backup import (
            backup_keep_markers_present,
            find_only_copy_backups,
        )
        orphans = find_only_copy_backups()
    except Exception:
        orphans = []
    tok = html.escape(request.state.csrf_token)
    for path, origin in orphans:
        name = html.escape(path.name)
        dest = html.escape(str(origin)) if origin else "its album folder"
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
        try:
            pinned = backup_keep_markers_present(path)
        except OSError:
            pinned = False
        reason = (
            "Kept because an upgrade or restore couldn't be verified "
            f"complete; its files may already be back at {dest}."
            if pinned
            else f"Holds files that aren't confirmed back at {dest}."
        )
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status ql-diagnostic-status-error" aria-label="Needs attention">!</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">Backup: {name}</div>'
            f'<div class="ql-diagnostic-detail">{reason}</div>'
            f'<div class="mt-2 flex gap-2">'
            f'<form hx-post="/backups/restore" hx-target="#diagnostics-list">'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Move these files back to {dest}?" '
            f'data-confirm-action="Restore">Restore</button>'
            f'</form>'
            f'<form hx-post="/backups/discard" hx-target="#diagnostics-list">'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Remove this backup? It is deleted only after '
            f'every file it holds is verified byte-for-byte back at {dest}." '
            f'data-confirm-action="Remove">Remove</button>'
            f'</form>'
            f'</div></div></div>'
        )
    try:
        from qobuz_librarian.library.backup import list_undo_copies
        undo = list_undo_copies()
    except Exception:
        undo = []
    for path, origin in undo:
        name = html.escape(path.name)
        dest = html.escape(str(origin)) if origin else "its album folder"
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status ql-diagnostic-status-ok" aria-label="OK">OK</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">Downsample originals retained</div>'
            f'<div class="ql-diagnostic-detail">Hi-res copies kept so the rewrite '
            f'can be undone; cleared automatically after '
            f'{plural(cfg.UPGRADE_BACKUP_RETENTION_DAYS, "day")}.</div>'
            f'<form hx-post="/backups/restore" hx-target="#diagnostics-list" class="mt-2">'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Put the hi-res originals back at {dest}? '
            f'This undoes the downsample." '
            f'data-confirm-action="Restore">Restore</button>'
            f'</form></div></div>'
        )
    return "\n".join(rows)


@app.get("/api/diagnostics", response_class=HTMLResponse)
async def api_diagnostics(request: Request):
    """Htmx partial that returns just the diagnostics list items for the Recheck button."""
    loop = asyncio.get_running_loop()
    return HTMLResponse(await loop.run_in_executor(
        None, _diagnostics_fragment, request))


def _restore_backup_sync(request: Request, backup: str) -> str:
    from qobuz_librarian.library.backup import (
        load_backup_result,
        restore_gap_fill_backup,
        restore_upgrade_backup,
    )
    name = (backup or "").strip()
    base = Path(str(cfg.UPGRADE_BACKUP_DIR))
    target = base / name
    # The form posts a bare directory name; anything path-shaped (separators,
    # dot-dirs) is someone probing, not a backup this page listed.
    if (not name or name != Path(name).name or name.startswith(".")
            or not target.is_dir()):
        return (_ql_notice_html("error", "That backup isn't there anymore. "
                                "it may already be restored or cleaned up.")
                + _diagnostics_fragment(request))
    state, operation_token, lock = _begin_direct_library_operation(
        "Backup restore")
    if state == "paused":
        return (_ql_notice_html(
                    "warning", "Library writes were paused before Restore "
                    "could start. Resume the web app, then try again.")
                + _diagnostics_fragment(request))
    if state == "busy":
        return (_ql_notice_html("warning", "A job is working in the library "
                                "right now. Try again once it finishes.")
                + _diagnostics_fragment(request))
    try:
        carried = load_backup_result(target)
        receipt = carried.receipt if carried is not None else None
        try:
            origin = Path(receipt["origin"])
            kind = receipt["kind"]
        except (KeyError, TypeError, ValueError):
            carried = None
        if carried is None:
            note = _ql_notice_html(
                "error", "This backup changed or its recovery record is "
                "invalid, so it was left untouched.")
        elif (
            resolution_plan := job_mgr.prepare_recovery_resolution(
                str(carried.path), carried.receipt)
        ) is None:
            note = _ql_notice_html(
                "error", "The saved recovery records could not be checked, "
                "so this backup was left untouched. Check that the data "
                "volume is writable, then try again.")
        elif kind in {"gap-fill", "downsample"}:
            # These backups hold the good originals; the destination may hold a
            # partial or a rewritten copy, so the backup always wins the swap.
            n = restore_gap_fill_backup(
                carried, origin, keep_larger_dst=False)
            if n and kind == "downsample":
                # Undoing a downsample has to undo everything it recorded, not
                # just the files: the album is no longer shrunk, so the cap that
                # hides it from Upgrade must go, and the saved candidate counts
                # for that artist are now wrong on both tool pages.
                from qobuz_librarian.quality.decision import clear_local_album_cap
                try:
                    clear_local_album_cap(origin)
                except OSError:
                    logging.getLogger("qobuz_librarian").exception(
                        "couldn't clear the downsample cap for %s", origin)
                try:
                    from qobuz_librarian.web import flows
                    flows._refresh_downsample_artist_state(Path(origin).parent)
                except Exception:
                    logging.getLogger("qobuz_librarian").exception(
                        "downsample state refresh failed after undo")
                try:
                    from qobuz_librarian.library import generation_state
                    if generation_state.output_is_current("upgrade"):
                        generation_state.mark_output_status(
                            "upgrade",
                            "stale",
                            reason=(
                                "Upgrade needs refresh after Downsample was "
                                "undone."
                            ),
                        )
                except Exception:
                    logging.getLogger("qobuz_librarian").exception(
                        "upgrade state invalidation failed after undo")
            if n and not carried.exists():
                if job_mgr.resolve_recovery_resolution(resolution_plan):
                    note = _ql_notice_html(
                        "success", f"Restored {plural(n, 'file')} to "
                        f"{html.escape(str(origin))}.")
                else:
                    note = _ql_notice_html(
                        "error", "The files were restored, but their saved "
                        "recovery status could not be updated. History will "
                        "continue to flag the recovery; do not run Restore "
                        "again until the data volume has been checked.")
            elif n:
                note = _ql_notice_html(
                    "warning", f"Restored {plural(n, 'file')}; the rest "
                    "couldn't be "
                    "moved and stay in the backup.")
            else:
                note = _ql_notice_html(
                    "error", "Nothing could be restored. The backup is "
                    "untouched; check the log.")
        elif kind == "upgrade":
            ok = restore_upgrade_backup(carried, origin)
            if ok and job_mgr.resolve_recovery_resolution(resolution_plan):
                note = _ql_notice_html(
                    "success", f"Restored the album to "
                    f"{html.escape(str(origin))}.")
            elif ok:
                note = _ql_notice_html(
                    "error", "The album was restored, but its saved recovery "
                    "status could not be updated. History will continue to "
                    "flag the recovery; do not run Restore again until the "
                    "data volume has been checked.")
            else:
                note = _ql_notice_html(
                    "error", "Couldn't restore automatically. The backup "
                    "is untouched; the log has the manual command.")
        else:
            note = _ql_notice_html(
                "error", "This backup has an unsupported recovery record, so "
                "it was left untouched.")
    finally:
        lock.release()
        job_mgr.end_library_operation(operation_token)
    return note + _diagnostics_fragment(request)


@app.post("/backups/restore", response_class=HTMLResponse)
async def restore_backup(request: Request, backup: str = Form("")):
    """Move an orphaned backup's files home. The button on the diagnostics list."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    loop = asyncio.get_running_loop()
    return HTMLResponse(await loop.run_in_executor(
        None, _restore_backup_sync, request, backup))


def _discard_backup_sync(request: Request, backup: str) -> str:
    from qobuz_librarian.library.backup import (
        discard_redundant_backup,
        load_backup_result,
    )
    name = (backup or "").strip()
    base = Path(str(cfg.UPGRADE_BACKUP_DIR))
    target = base / name
    if (not name or name != Path(name).name or name.startswith(".")
            or not target.is_dir()):
        return (_ql_notice_html("error", "That backup isn't there anymore. "
                                "it may already be restored or cleaned up.")
                + _diagnostics_fragment(request))
    state, operation_token, lock = _begin_direct_library_operation(
        "Backup removal")
    if state == "paused":
        return (_ql_notice_html(
                    "warning", "Library writes were paused before Remove "
                    "could start. Resume the web app, then try again.")
                + _diagnostics_fragment(request))
    if state == "busy":
        return (_ql_notice_html("warning", "A job is working in the library "
                                "right now. Try again once it finishes.")
                + _diagnostics_fragment(request))
    try:
        carried = load_backup_result(target)
        if carried is None or carried.receipt is None:
            note = _ql_notice_html(
                "error", "This backup changed or its recovery record is "
                "invalid, so it was left untouched.")
        elif (
            resolution_plan := job_mgr.prepare_recovery_resolution(
                str(carried.path), carried.receipt)
        ) is None:
            note = _ql_notice_html(
                "error", "The saved recovery records could not be checked, "
                "so this backup was left untouched. Check that the data "
                "volume is writable, then try again.")
        elif discard_redundant_backup(target):
            dest = html.escape(str(carried.receipt.get("origin", "")))
            if job_mgr.resolve_recovery_resolution(resolution_plan):
                note = _ql_notice_html(
                    "success", "Removed the backup. Every file it held is "
                    f"verified present at {dest}.")
            else:
                note = _ql_notice_html(
                    "error", "The backup was removed, but its saved recovery "
                    "status could not be updated. History may keep flagging "
                    "the recovery until the data volume has been checked.")
        else:
            note = _ql_notice_html(
                "error", "Couldn't verify every file is back byte-for-byte, "
                "so the backup was left untouched. Restore is the safe way "
                "to bring its files home.")
    finally:
        lock.release()
        job_mgr.end_library_operation(operation_token)
    return note + _diagnostics_fragment(request)


@app.post("/backups/discard", response_class=HTMLResponse)
async def discard_backup(request: Request, backup: str = Form("")):
    """Delete a kept backup once its files are verified home. The Remove button on the diagnostics list."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    loop = asyncio.get_running_loop()
    return HTMLResponse(await loop.run_in_executor(
        None, _discard_backup_sync, request, backup))


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def _generator():
        import logging as _logging
        import queue as _queue
        # Reconnect quickly so a backgrounded tab's progress bar catches up to
        # the live count soon after it's brought back to the foreground.
        yield "retry: 750\n\n"
        if (job.status in job_mgr.TERMINAL
                or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
            replay = (
                job.log_lines[-job.REPLAY_TAIL:]
                if job.REPLAY_TAIL > 0
                else ()
            )
            for line in replay:
                escaped = line.replace("\n", " ").replace("\r", "")
                yield f"data: {escaped}\n\n"
            yield f"event: done\ndata: {job.status.value}\n\n"
            return
        sub = job.subscribe()
        loop = asyncio.get_running_loop()
        empty_ticks = 0
        try:
            while True:
                try:
                    line = await loop.run_in_executor(
                        _SSE_EXECUTOR, lambda: sub.get(timeout=0.5))
                    empty_ticks = 0
                    if line == job_mgr.STREAM_END:
                        yield f"event: done\ndata: {job.status.value}\n\n"
                        break
                    if line.startswith(job_mgr.PROGRESS_PREFIX):
                        yield ("event: progress\ndata: "
                               + line[len(job_mgr.PROGRESS_PREFIX):] + "\n\n")
                        continue
                    if line.startswith(job_mgr.REVIEW_CHANGED):
                        continue  # review-sync nudge, handled by the review stream
                    escaped = line.replace("\n", " ").replace("\r", "")
                    yield f"data: {escaped}\n\n"
                except _queue.Empty:
                    if (job.status in job_mgr.TERMINAL
                            or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                        yield f"event: done\ndata: {job.status.value}\n\n"
                        break
                    empty_ticks += 1
                    if empty_ticks >= _SSE_HEARTBEAT_TICKS:
                        empty_ticks = 0
                        yield ": ping\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logging.getLogger("qobuz_librarian").exception(
                        "SSE stream error for job %s", job.id)
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{job_id}/review-stream")
async def job_review_stream(job_id: str):
    """Live channel for an awaiting-review page: emits `event: review` whenever
    selection or candidates change (a tick/untick/hide in this or another tab),
    so every open view stays in sync. Closes once the job leaves review (the
    page then reloads to show the executing/finished state). Separate from the
    progress stream, which closes the moment a scan finishes."""
    # Only a LIVE job (in the registry) has a producer that fans out review
    # nudges; a historical/evicted review still renders and saves selection via
    # the disk fallback, but can't receive live cross-tab updates, so end its
    # stream cleanly rather than 404 (which surfaces as a console error) or hold
    # a socket that never gets a nudge.
    job = job_mgr.registry.get(job_id)

    async def _generator():
        import queue as _queue
        yield "retry: 1000\n\n"
        if job is None or job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            yield "event: closed\ndata: inactive\n\n"
            return
        sub = job.subscribe()
        loop = asyncio.get_running_loop()
        empty_ticks = 0
        try:
            while True:
                try:
                    line = await loop.run_in_executor(
                        _SSE_EXECUTOR, lambda: sub.get(timeout=0.5))
                    if line.startswith(job_mgr.REVIEW_CHANGED):
                        # The data names the originating tab (or "changed" for a
                        # server-side sync) so that tab can skip reloading a DOM
                        # its own action already brought up to date.
                        origin = line[len(job_mgr.REVIEW_CHANGED):]
                        yield f"event: review\ndata: {origin or 'changed'}\n\n"
                    # All other fanned-out lines (log/progress/end) are ignored
                    # here; this channel only carries review-sync nudges.
                except _queue.Empty:
                    if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
                        yield f"event: closed\ndata: {job.status.value}\n\n"
                        break
                    empty_ticks += 1
                    if empty_ticks >= _SSE_HEARTBEAT_TICKS:
                        empty_ticks = 0
                        yield ": ping\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _job_to_dict(job, *, log_tail: int = 50):
    out = {
        "id": job.id,
        "status": job.status.value,
        "title": job.title,
        "edition": job.edition,
        "display_title": job.display_title,
        "artist": job.artist,
        "album_id": getattr(job, "album_id", None),
        "summary": job.summary,
        "error": job.error,
        "quality_shortfall": getattr(job, "quality_shortfall", {}),
        "created_at": getattr(job, "created_at", None),
        "finished_at": getattr(job, "finished_at", None),
    }
    if log_tail:
        out["log_lines"] = job.log_lines[-log_tail:]
    return out


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        # A finished job evicted past MAX_FINISHED is still on disk; fall back
        # to the archive so a poller gets its terminal status instead of a 404
        # (mirrors how GET /jobs/{job_id} rehydrates from history).
        job = job_mgr.load_historical_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_dict(job)


@app.get("/api/queue/count")
async def queue_count():
    """Live count of in-flight jobs (pending/scanning/running; parked reviews
    have their own dots) so the nav Queue badge stays in sync without a page
    reload. The badge is otherwise server-rendered once per page, which left it
    stale (e.g. reading "1" next to an empty Queue) after a job finished while
    you sat on another page."""
    active = [j for j in job_mgr.registry.pending_and_running()
              if j.status != job_mgr.JobStatus.AWAITING_REVIEW]
    return JSONResponse({
        "count": len(active),
        "running": any(j.status.value in ("running", "scanning") for j in active),
    })


@app.get("/api/jobs")
async def jobs_list(status: str = "", limit: int = 50):
    """List jobs as JSON. Optional `status` filter ('pending', 'running',
    'awaiting_review', 'scanning', 'done', 'failed', 'canceled').
    `limit` caps the response, most recent first.

    Live (non-terminal) jobs come from the in-memory registry. Terminal jobs
    (done/failed/canceled) come from the registry too, but it only keeps the
    most-recent MAX_FINISHED of them, so we also reach into the on-disk archive
    to surface jobs evicted past that cap; otherwise `status=done` could never
    return anything older than the last ~50 finishes."""
    wanted = status.strip().lower() or None
    if wanted is not None:
        valid = {s.value for s in job_mgr.JobStatus}
        if wanted not in valid:
            raise HTTPException(status_code=400,
                                detail="Unknown status filter")
    cap = max(1, min(limit, 500))
    terminal_values = {s.value for s in job_mgr.TERMINAL}
    want_terminal = wanted in terminal_values if wanted else True

    matching = []
    seen = set()
    for j in reversed(job_mgr.registry.all()):
        if wanted and j.status.value != wanted:
            continue
        matching.append(_job_to_dict(j, log_tail=0))
        seen.add(j.id)
        if len(matching) >= cap:
            break

    # The registry only holds the newest MAX_FINISHED terminal jobs; the
    # archive keeps far more.
    if want_terminal and len(matching) < cap:
        from qobuz_librarian.web import job_persistence
        # Filter in SQL: fetching the newest `cap` rows and filtering here
        # would return too few (or none) whenever those rows are mostly other
        # statuses, even though older matching history exists.
        for row in job_persistence.history_page(cap, 0, status=wanted):
            if row["id"] in seen:
                continue
            matching.append({
                "id": row["id"],
                "status": row["status"],
                "title": row["title"],
                "edition": row["edition"],
                "display_title": job_mgr.release_title(
                    row["title"], row["edition"]
                ),
                "artist": row["artist"],
                "album_id": row["album_id"] or None,
                "error": row["error"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            })
            seen.add(row["id"])
            if len(matching) >= cap:
                break

    return JSONResponse({"jobs": matching, "count": len(matching)})


def _get_token():
    from qobuz_librarian.api.auth import load_qobuz_token
    return load_qobuz_token()[1]


def _get_optional_token():
    if not _read_creds().get("auth_token"):
        return None
    try:
        return _get_token()
    except Exception:
        return None


def _format_age(ts: float) -> str:
    """Human-readable age of a past timestamp."""
    import time as _time
    age = _time.time() - ts
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
    from datetime import datetime
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


def _last_scan_age() -> str | None:
    """Human-readable age of the last library/artist scan, or None."""
    try:
        ts = float(cfg.LAST_SCAN_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return _format_age(ts)


def _last_new_release_check_age() -> str | None:
    """Human-readable age of the last new-release check, or None. Gives the
    dashboard a sibling indicator to the existing 'last library scan' line so
    the user can see how fresh the auto-check's signal is."""
    from qobuz_librarian.library import new_releases
    ts = new_releases.last_run()
    return _format_age(ts) if ts is not None else None


def _tool_last_run_age(execute_kind: str) -> str | None:
    """Age of the last clean run of a tool scan, or None if it's never
    finished, so a tool page can show "Last scan 3 days ago" instead of
    looking identical to a first visit."""
    from qobuz_librarian.web import job_persistence
    ts = job_persistence.last_finished_at(execute_kind)
    return _format_age(ts) if ts is not None else None


def _no_creds_response(request):
    """Return a 303 redirect (or htmx fragment) when no credentials are set."""
    if _is_htmx(request):
        return HTMLResponse(
            _ql_notice_html(
                "error",
                'No Qobuz credentials set. Visit '
                '<a href="/settings" class="ql-inline-link">Settings</a>.',
            ),
            status_code=200)
    return RedirectResponse(url="/settings?error=creds", status_code=303)


def _read_creds():
    from qobuz_librarian.api.auth import read_qobuz_credentials

    credentials = read_qobuz_credentials()
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
    """Write credentials into the streamrip config. Returns False if the
    config volume isn't writable (NAS perms) so the Settings page can show
    a clear message rather than 500ing.

    Delegates to qobuz_librarian.api.auth.write_streamrip_creds so the web
    Settings path and the env-var sync share one credential writer."""
    from qobuz_librarian.api.auth import write_streamrip_creds
    return write_streamrip_creds(user_id, auth_token)


def start():
    import uvicorn
    # server_header=False mirrors the --no-server-header the Docker entrypoint
    # passes, so the installed qobuz-librarian-web entrypoint doesn't advertise
    # "Server: uvicorn" (a free hint to anyone scanning for framework CVEs).
    uvicorn.run("qobuz_librarian.web.app:app", host=cfg.WEB_HOST,
                port=cfg.WEB_PORT, workers=1, server_header=False)
