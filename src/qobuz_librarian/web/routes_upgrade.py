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
    review_pages,
    runtime,
    saved_reviews,
    scans,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()


@router.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request, page: int = 1, q: str = "",
                       stale: bool = False, noselection: bool = False):
    if not runtime._upgrade_available():
        return saved_reviews._upgrade_unavailable_response()
    badge_generation = review_badges.ready_generation("upgrade")
    loop = asyncio.get_running_loop()
    review = await loop.run_in_executor(
        None, lambda: saved_reviews.page_review("upgrade"))
    state = saved_reviews._upgrade_state_summary()
    latest = scans._last_finished("upgrade")
    stopped = (latest if latest and latest.phase == "execute" and (
        latest.interrupted_by_restart
        or latest.status == job_mgr.JobStatus.CANCELED) else None)
    running = scans._active_scan("upgrade", statuses=(
        job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING))
    if stopped and (
            (state.get("updated_at") or 0) > (stopped.finished_at or stopped.created_at)
            or running):
        stopped = None
    ctx = {
        "creds_ok": qobuz_access._creds_ok(), "qobuz_ready": qobuz_access._qobuz_ready(), "page": "upgrade",
        "upgrade_state": state,
        "upgrade_stopped": stopped,
        "upgrade_running": running,
        "review_job": None if running else review,
        "error": rendering._notice_text(request.query_params.get("error")),
        "stale": stale, "noselection": noselection,
        "ended_run": scans._ended_run(request, "upgrade"),
        "queue_wait": job_labels._queue_wait(running) if running else None,
        "last_run": job_labels._tool_last_run_age("library"),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_UPGRADE)}
    if ctx["review_job"]:
        ctx.update(review_pages._review_context(review, page, q))
    return rendering._tr(request, "upgrade.html", ctx,
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
    await loop.run_in_executor(
        None, lambda: saved_reviews._review_job_from_current_saved_state("upgrade"))
    return RedirectResponse(url="/upgrade", status_code=303)
