"""The responses a request gets when its work cannot start."""
import html
import logging

from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web import queue_recovery, rendering, runtime, storage, write_gate

_log = logging.getLogger("qobuz_librarian")


def _settled_completion_response(request, job):
    """Take the completed-download lane when a refused settlement has already
    cleared the recovery it refused over.

    `settle_blocked_item` only settles a pre-launch abort, so it refuses a
    download that imported and then stranded a file in staging, but it parks
    that staging first, which is the one thing the recovery was waiting on.
    Re-read the recovery and the completion record it just moved, or the reply
    describes a state this request has already left behind.
    """
    if not runtime._run_lock_intact():
        _log.info("Retry %s: no completed-download lane; run lock not held.",
                  job.id)
        return None
    try:
        recovery = queue_recovery._record_startup_recovery(runtime._RUN_LOCK_HANDLE)
    except Exception:
        _log.warning("Retry %s: no completed-download lane; the recovery "
                     "record could not be re-read.", job.id, exc_info=True)
        return None
    status = queue_recovery._recovery_status_value(recovery)
    completed = queue_recovery._durable_completion_status(job)
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
    if not queue_recovery._reconcile_acknowledged_job(
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
    if rendering._is_htmx(request):
        return HTMLResponse(
            rendering._ql_notice_html("error", html.escape(message)),
            status_code=200,
        )
    # can_retry: these messages end in "restart Qobuz Librarian", and after a
    # restart a reload is exactly the next step; without it the page offered
    # no way forward at all.
    return rendering._tr(request, "lock_busy.html", {"msg": message, "can_retry": True},
               status_code=503)


def _job_admission_response(request):
    """Explain a refused jobs.db admission without claiming work was queued."""
    message = job_mgr.JOB_ADMISSION_ERROR
    if rendering._is_htmx(request):
        return HTMLResponse(
            rendering._ql_notice_html("error", html.escape(message)),
            status_code=200,
        )
    return RedirectResponse(
        url="/queue?error=" + rendering._notice_key(message),
        status_code=303,
    )


def _lock_busy_response(request, *, durable_resume_job_id: str | None = None,
                        queue_behind_job=None):
    """Return a 503 response if web writes are paused, else None."""
    notice = write_gate._writes_paused_notice(
        durable_resume_job_id=durable_resume_job_id,
        queue_behind_job=queue_behind_job,
        log_details=True,
    )
    if notice is None:
        return None
    unwritable_now = storage._unwritable_volumes()
    if rendering._is_htmx(request):
        response = HTMLResponse(
            rendering._ql_notice_html("error", html.escape(notice["msg"])),
            status_code=200)
        if request.headers.get("HX-Target") == "diagnostics-list":
            # The list is the whole Diagnostics panel; the notice goes above
            # it rather than in its place.
            response.headers["HX-Reswap"] = "beforebegin"
        return response
    return rendering._tr(request, "lock_busy.html",
               {"msg": notice["msg"], "reason": notice["reason"],
                "action": notice["action"],
                # "Try again" only helps where retrying can succeed; the rest
                # need something fixed first and the button was false comfort.
                "can_retry": (runtime._LOCK_BUSY_PID is not None
                              or bool(unwritable_now)
                              or not storage._data_dir_available()
                              or queue_recovery._STARTUP_RECOVERY_REFRESHING)},
               status_code=503)
