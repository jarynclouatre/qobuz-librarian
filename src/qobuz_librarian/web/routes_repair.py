"""Routes for the Repair page."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian import repair_log
from qobuz_librarian.api.auth import QobuzAccess
from qobuz_librarian.library import scan_checkpoint
from qobuz_librarian.web import flows, review_badges, review_pages, runtime, scans
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()


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
        if j.execute_kind != "repair":
            continue
        if cur is None or (j.created_at or 0) >= (cur.created_at or 0):
            cur = j
    # A run that finished after a failed or interrupted one supersedes it.
    return cur if cur is not None and cur.status in states else None


@router.get("/repair", response_class=HTMLResponse)
async def repair_page(request: Request, page: int = 1):
    badge_generation = review_badges.ready_generation("repair")
    creds_ok = runtime._creds_ok()
    # /repair is the SINGLE authoritative repair surface.
    rjob = _repair_current_job()
    ctx = {"creds_ok": creds_ok, "qobuz_ready": runtime._qobuz_ready(),
           "page": "repair", "repair_job": rjob,
           "error": runtime._notice_text(request.query_params.get("error")),
           "JobStatus": job_mgr.JobStatus}
    if rjob is not None:
        ctx["queue_wait"] = runtime._queue_wait(rjob)
        ctx.update(review_pages._review_context(rjob, page))
    # The launcher renders when the surface is idle AND under a run that failed
    # or was cancelled (see _repair_current_job, which keeps those on the page
    # on purpose). Both cases read these, so both have to be given them.
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
        ctx["last_run"] = runtime._tool_last_run_age("repair")
    badge_ack = None
    if (rjob is not None
            and rjob.status == job_mgr.JobStatus.AWAITING_REVIEW
            and (ctx.get("review_counts") or {}).get("total")):
        badge_ack = ("repair", badge_generation)
    return runtime._tr(request, "repair.html", ctx, review_badge_ack=badge_ack)


@router.post("/repair")
async def repair_scan(request: Request):
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        credentials = await runtime._authorize_qobuz_for_web(
            QobuzAccess.CATALOGUE_ACTION
        )
    except runtime._QOBUZ_ACTION_ERRORS as exc:
        msg = job_mgr.qobuz_action_error_message(exc, unchanged=True)
        return RedirectResponse(
            url="/repair?error=" + runtime._notice_key(msg), status_code=303)
    job = job_mgr.Job(title="Repair scan")
    job.execute_kind = "repair"
    job.review_verb = "Repair"  # the action refills damaged tracks, not a download
    def _scan(j):
        active = runtime._authorize_qobuz_live(
            QobuzAccess.CATALOGUE_ACTION,
            expected_generation=credentials.generation,
        )
        flows.scan_repairs(j, active.token)

    job = await scans._submit_scan_deduped_async(
        job,
        _scan,
        runtime._resume_repair(job, job.execute_args),
        "repair")
    if job is None:
        return scans._scan_submission_failure_response(request, "/repair")
    # Land back on /repair so the sweep is watched live right here; its card
    # streams each flagged album inline (and explains the wait if it's queued
    # behind another scan).
    return RedirectResponse(url="/repair", status_code=303)


@router.get("/repair/history", response_class=HTMLResponse)
async def repair_history(request: Request):
    """Tracks Repair has replaced in place."""
    # Walks lines on the data volume, so offload to match the dashboard's pattern
    # and keep the event loop free if the file is sizable.
    loop = asyncio.get_running_loop()
    entries = await loop.run_in_executor(
        None, lambda: repair_log.read_repair_log_entries(limit=500))
    return runtime._tr(request, "repair_history.html",
               {"page": "repair", "entries": entries})
