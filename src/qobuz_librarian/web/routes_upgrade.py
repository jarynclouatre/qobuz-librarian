"""Routes for the Upgrade page."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.web import hidden_pages, runtime, saved_reviews

router = APIRouter()


@router.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    creds_ok = runtime._creds_ok()
    # Without credentials the page still renders, showing the connect card,
    # bouncing to Search reads as a broken button.
    if not getattr(cfg, "UPGRADE_SCAN_ENABLED", True):
        return saved_reviews._upgrade_unavailable_response()
    state = saved_reviews._upgrade_state_summary()
    return runtime._tr(request, "upgrade.html", {
        "creds_ok": creds_ok, "qobuz_ready": runtime._qobuz_ready(), "page": "upgrade",
        "upgrade_state": state,
        "last_run": runtime._tool_last_run_age("library"),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_UPGRADE)})


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
    busy = runtime._lock_busy_response(request)
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
