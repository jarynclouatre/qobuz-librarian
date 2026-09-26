"""Routes for the Downsample page."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian.integrations import downsample_engine
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.web import (
    flows,
    hidden_pages,
    job_labels,
    job_runs,
    qobuz_access,
    refusals,
    rendering,
    saved_reviews,
    scans,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()


@router.get("/downsample", response_class=HTMLResponse)
async def downsample_page(request: Request):
    state = saved_reviews._downsample_state_summary()
    # A fresh scan supersedes the parked review (see _submit_scan_deduped), so
    # the Refresh confirm must say so instead of quietly dropping the user's
    # ticks.
    review_parked = any(
        j.execute_kind == "downsample"
        for j in job_mgr.registry.awaiting_review())
    return rendering._tr(request, "downsample.html", {
        "page": "downsample",
        "have_downsample": downsample_engine.HAVE_DOWNSAMPLE,
        "creds_ok": qobuz_access._creds_ok(),
        "downsample_state": state,
        "review_parked": review_parked,
        # A standalone refresh in flight, so the page shows "scan running"
        # instead of the idle launcher.
        "downsample_running": scans._active_scan(
            "downsample",
            statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
                      job_mgr.JobStatus.RUNNING)),
        "last_run": job_labels._tool_last_run_age("downsample"),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_DOWNSAMPLE)})


@router.get("/downsample/hidden", response_class=HTMLResponse)
async def downsample_hidden(request: Request):
    return hidden_pages._hidden_view(request, hidden_mod.SCOPE_DOWNSAMPLE, page="downsample",
                        restore_action="/downsample/hidden/restore",
                        back_url="/downsample",
                        restore_all_action="/downsample/hidden/restore-all")


@router.post("/downsample/hidden/restore")
async def downsample_hidden_restore(request: Request):
    return await hidden_pages._restore_hidden(request, hidden_mod.SCOPE_DOWNSAMPLE,
                                 "/downsample/hidden")


@router.post("/downsample/hidden/restore-all")
async def downsample_hidden_restore_all(request: Request):
    return await hidden_pages._restore_hidden_all(
        request, hidden_mod.SCOPE_DOWNSAMPLE, "/downsample/hidden",
        "album kept hi-res", "albums kept hi-res")


@router.post("/downsample/review")
async def downsample_review(request: Request):
    # No credential check: downsampling only reads and rewrites local files.
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        return busy
    loop = asyncio.get_running_loop()
    job = await loop.run_in_executor(
        None, lambda: saved_reviews._review_job_from_current_saved_state("downsample"))
    if job is None:
        return RedirectResponse(url="/downsample", status_code=303)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@router.post("/downsample")
async def downsample_scan(request: Request):
    # No credential check: downsampling only reads and rewrites local files.
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        return busy
    job = job_mgr.Job(title="Downsample scan")
    job.execute_kind = "downsample"
    job.review_verb = "Downsample"  # the action rewrites files, not a download
    job = await scans._submit_scan_deduped_async(
        job,
        lambda j: flows.scan_downsamples(j),
        lambda j, chosen: flows.execute_downsamples(
            j,
            chosen,
            token=qobuz_access._get_optional_token(),
            keep_originals=job_runs._job_downsample_keep_originals(j),
        ),
        "downsample")
    if job is None:
        return scans._scan_submission_failure_response(request, "/downsample")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
