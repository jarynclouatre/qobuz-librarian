"""Routes for the Upgrade page."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.web import (
    hidden_pages,
    job_labels,
    qobuz_access,
    refusals,
    rendering,
    review_badges,
    runtime,
    saved_reviews,
    scans,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()


@router.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    if not runtime._upgrade_available():
        return saved_reviews._upgrade_unavailable_response()
    badge_generation = review_badges.ready_generation("upgrade")
    state = saved_reviews._upgrade_state_summary()
    latest = scans._last_finished("upgrade")
    stopped = (latest if latest and latest.phase == "execute" and (
        latest.interrupted_by_restart
        or latest.status == job_mgr.JobStatus.CANCELED) else None)
    if stopped and (
            (state.get("updated_at") or 0) > (stopped.finished_at or stopped.created_at)
            or scans._active_scan("upgrade", statuses=(
                job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING))):
        stopped = None
    return rendering._tr(request, "upgrade.html", {
        "creds_ok": qobuz_access._creds_ok(), "qobuz_ready": qobuz_access._qobuz_ready(), "page": "upgrade",
        "upgrade_state": state,
        "upgrade_stopped": stopped,
        "last_run": job_labels._tool_last_run_age("library"),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_UPGRADE)},
        review_badge_ack=("upgrade", badge_generation))


@router.get("/upgrade/hidden", response_class=HTMLResponse)
async def upgrade_hidden(request: Request):
    if not runtime._upgrade_available():
        return saved_reviews._upgrade_unavailable_response()
    return hidden_pages._hidden_view(request, hidden_mod.SCOPE_UPGRADE, page="upgrade",
                        restore_action="/upgrade/hidden/restore", back_url="/upgrade",
                        restore_all_action="/upgrade/hidden/restore-all")


@router.post("/upgrade/hidden/restore")
async def upgrade_hidden_restore(request: Request):
    if not runtime._upgrade_available():
        return saved_reviews._upgrade_unavailable_response()
    return await hidden_pages._restore_hidden(request, hidden_mod.SCOPE_UPGRADE, "/upgrade/hidden")


@router.post("/upgrade/hidden/restore-all")
async def upgrade_hidden_restore_all(request: Request):
    if not runtime._upgrade_available():
        return saved_reviews._upgrade_unavailable_response()
    return await hidden_pages._restore_hidden_all(
        request, hidden_mod.SCOPE_UPGRADE, "/upgrade/hidden",
        "dismissed album", "dismissed albums")


@router.post("/upgrade/review")
async def upgrade_review(request: Request):
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        return busy
    if not runtime._upgrade_available():
        return saved_reviews._upgrade_unavailable_response()
    loop = asyncio.get_running_loop()
    job = await loop.run_in_executor(
        None, lambda: saved_reviews._review_job_from_current_saved_state("upgrade"))
    if job is None:
        return RedirectResponse(url="/upgrade", status_code=303)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
