"""The responses a request gets when its work cannot start."""
import html
import logging

from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web import queue_recovery, rendering, runtime, storage, write_gate

_log = logging.getLogger("qobuz_librarian")


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


def _lock_busy_response(request, *, downloads: bool = False):
    """Return a 503 response if web writes are paused, else None."""
    notice = write_gate._writes_paused_notice(
        log_details=True,
        downloads=downloads,
    )
    if notice is None:
        return None
    unwritable_now = storage._unwritable_volumes()
    if rendering._is_htmx(request):
        if request.headers.get("HX-Target") == "diagnostics-list":
            # The list is the whole Diagnostics panel and stays as it is; the
            # notice goes to the toast, where its row's user can see it.
            return HTMLResponse(
                rendering._ql_toast_html(
                    "error", html.escape(notice["msg"]), "diagnostics"),
                headers={"HX-Reswap": "none"})
        return HTMLResponse(
            rendering._ql_notice_html("error", html.escape(notice["msg"])),
            status_code=200)
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
