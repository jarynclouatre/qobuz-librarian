"""Routes for the Repair page."""
import asyncio
import time
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian import repair_log
from qobuz_librarian.api.auth import QobuzAccess
from qobuz_librarian.library import scan_checkpoint
from qobuz_librarian.web import (
    flows,
    job_labels,
    job_persistence,
    job_runs,
    qobuz_access,
    refusals,
    rendering,
    review_badges,
    review_pages,
    scans,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()

_HISTORY_ROWS = 200


def _repair_current_job():
    """The repair job that owns the /repair surface right now: the most recent
    repair job still pending / scanning / awaiting-review / running. None means
    the surface is idle (show the start-or-resume form). This is what lets
    /repair stay the single authoritative repair page across every phase instead
    of handing a parked review off to /jobs/{id}, and it's why a review is never
    hidden behind a "Start scan" button that would silently discard it.
    A run of some of the flagged albums is shown over the newer review it
    left the rest in, until it ends."""
    active = (job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
              job_mgr.JobStatus.RUNNING)
    states = (*active, job_mgr.JobStatus.AWAITING_REVIEW,
              # A run that failed or was cancelled is part of the phase set
              # too, or the page would go back to its idle launcher on the next
              # reload.
              job_mgr.JobStatus.FAILED, job_mgr.JobStatus.CANCELED)
    repairs = [j for j in job_mgr.registry.all() if j.execute_kind == "repair"]
    cur = None
    for j in [j for j in repairs if j.status in active] or repairs:
        if cur is None or (j.created_at or 0) >= (cur.created_at or 0):
            cur = j
    # A run that finished after a failed or interrupted one supersedes it.
    return cur if cur is not None and cur.status in states else None


@router.get("/repair", response_class=HTMLResponse)
async def repair_page(request: Request, page: int = 1):
    badge_generation = review_badges.ready_generation("repair")
    creds_ok = qobuz_access._creds_ok()
    # /repair is the SINGLE authoritative repair surface.
    rjob = _repair_current_job()
    ctx = {"creds_ok": creds_ok, "qobuz_ready": qobuz_access._qobuz_ready(),
           "page": "repair", "repair_job": rjob,
           "error": rendering._notice_text(request.query_params.get("error")),
           "JobStatus": job_mgr.JobStatus}
    ended = scans._ended_run(request, "repair", (
        job_mgr.JobStatus.DONE, job_mgr.JobStatus.FAILED,
        job_mgr.JobStatus.CANCELED))
    if ended is not rjob:
        ctx["ended_run"] = ended
    if rjob is not None:
        ctx["queue_wait"] = job_labels._queue_wait(rjob)
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
            total = cp["meta"].get("artists_total")
            ctx["repair_resume"] = {
                "saved": len(bundles),
                "total": total if type(total) is int else None,
                "found": found,
            }
        # The failed or interrupted job shown above is the latest word; a scan
        # that finished before it started is not news beside it.
        last_scan = job_persistence.last_finished_at("repair")
        if last_scan is not None and (
                rjob is None or last_scan >= (rjob.created_at or 0)):
            ctx["last_run"] = job_labels._format_age(last_scan)
        latest = max((j for j in job_mgr.registry.all()
                      if j.execute_kind == "repair"),
                     key=lambda j: j.created_at or 0, default=None)
        # Only a scan's own outcome; an approved run's would read as the scan's.
        if (latest is not None and latest.status == job_mgr.JobStatus.DONE
                and latest.phase == "scan"):
            ctx["last_summary"] = latest.summary
    badge_ack = None
    if (rjob is not None
            and rjob.status == job_mgr.JobStatus.AWAITING_REVIEW
            and (ctx.get("review_counts") or {}).get("total")):
        badge_ack = ("repair", badge_generation)
    return rendering._tr(request, "repair.html", ctx, review_badge_ack=badge_ack)


@router.post("/repair")
async def repair_scan(request: Request):
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        credentials = await qobuz_access._authorize_qobuz_for_web(
            QobuzAccess.CATALOGUE_ACTION
        )
    except qobuz_access._QOBUZ_ACTION_ERRORS as exc:
        msg = job_mgr.qobuz_action_error_message(exc)
        return RedirectResponse(
            url="/repair?error=" + rendering._notice_key(msg), status_code=303)
    job = job_mgr.Job(title="Repair scan")
    job.execute_kind = "repair"
    job.review_verb = "Repair"  # the action refills damaged tracks, not a download
    def _scan(j):
        active = qobuz_access._authorize_qobuz_live(
            QobuzAccess.CATALOGUE_ACTION,
            expected_generation=credentials.generation,
        )
        flows.scan_repairs(j, active.token)

    job = await scans._submit_scan_deduped_async(
        job,
        _scan,
        job_runs._resume_repair(job, job.execute_args),
        "repair")
    if job is None:
        return scans._scan_submission_failure_response(request, "/repair")
    # Land back on /repair so the sweep is watched live right here; its card
    # streams each flagged album inline (and explains the wait if it's queued
    # behind another scan).
    return RedirectResponse(url="/repair", status_code=303)


def _repair_history():
    """The Repair history page's rows and totals."""
    repairs = repair_log.read_repair_log_albums()
    context = {
        "track_count": sum(len(r["tracks"]) for r in repairs),
        "album_count": len({(r["artist"], r["album"]) for r in repairs}),
        "more": len(repairs) > _HISTORY_ROWS,
        "repairs": repairs[:_HISTORY_ROWS],
    }
    for r in context["repairs"]:
        try:
            ts = time.mktime(time.strptime(r["when"], "%Y-%m-%d %H:%M"))
            r["when"], r["when_exact"] = job_labels._when_label(ts)
        except (ValueError, OverflowError):
            pass
        if rendering._local_album_art(
                Path(cfg.MUSIC_ROOT) / r["artist"] / r["album"]) is not None:
            r["cover"] = "/repair/history/art?" + urllib.parse.urlencode(
                {"artist": r["artist"], "album": r["album"]})
    return context


@router.get("/repair/history", response_class=HTMLResponse)
async def repair_history(request: Request):
    """Albums Repair has replaced tracks in."""
    # Walks lines on the data volume and looks in each album folder for a cover.
    loop = asyncio.get_running_loop()
    context = await loop.run_in_executor(None, _repair_history)
    return rendering._tr(request, "repair_history.html",
               {"page": "repair", **context})


@router.get("/repair/history/art")
def repair_history_art(artist: str, album: str):
    return rendering._library_art_response(Path(cfg.MUSIC_ROOT) / artist / album)
