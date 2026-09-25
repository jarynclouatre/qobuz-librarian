"""Routes for the Queue and History pages."""
import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from qobuz_librarian.queue.startup_recovery import BlockedItemSettlementAction
from qobuz_librarian.web import job_persistence, runtime
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


@router.head("/queue")
async def queue_head():
    return Response(status_code=200)


@router.post("/queue/interrupted/discard")
async def discard_interrupted_terminal_download(request: Request):
    """Give up on an interrupted terminal download from the web.

    The download it settles was started in a terminal, so there is no job page
    to carry the usual controls and the reader was told to go and use a
    terminal instead. Giving up is the one decision that lifts the pause
    without a resume, and a resume can only be run by the interface that owns
    the saved queue, so that is what is offered here.
    """
    form = await request.form()
    offer = runtime._terminal_recovery_offer()
    if offer is None or (
        str(form.get("recovery_operation_id") or "") != offer["operation_id"]
        or str(form.get("recovery_item_id") or "") != offer["item_id"]
    ):
        return RedirectResponse(
            url="/queue?error=" + runtime._notice_key(
                "That interrupted download is no longer the one holding things "
                "up. Nothing was changed. Reload the page."),
            status_code=303)
    settled, reason = runtime._settle_blocked_recovery(
        BlockedItemSettlementAction.DISCARD)
    _log.info(
        "Give up on the interrupted terminal download: %s.",
        "succeeded" if settled
        else f"refused: {(reason or 'no reason given').rstrip('.')}")
    if not settled:
        return RedirectResponse(
            url="/queue?error=" + runtime._notice_key(
                reason or "The interrupted download could not be discarded."),
            status_code=303)
    # No album name here: the banner it was clicked from names it, and the
    # query the queue page reads is capped, so a long title cut the sentence
    # off mid-word.
    return RedirectResponse(
        url="/queue?notice=" + runtime._notice_key(
            f"Gave up on the interrupted download. {reason}"
            if offer["imported"] or offer["partial"] else
            "Gave up on the interrupted download. Nothing reached your "
            "library."),
        status_code=303)


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, error: str = "", notice: str = ""):
    """The Queue tab: jobs in flight (pending / scanning / running). Parked
    reviews ride along in ``pending`` but the template and the badge both
    filter them out; they render on their own surfaces. Finished jobs live in
    the History tab, which reads the durable archive rather than the capped
    in-memory set."""
    pending = job_mgr.registry.pending_and_running()
    protected_id = job_mgr.durable_recovery_job_id()
    return runtime._tr(request, "queue.html", {
        "pending": pending,
        "queue_rows_signature": runtime._queue_rows_signature(pending),
        # Per-pending-job "waiting behind X" explainer, the same one the single
        # job page shows, so the Queue list says why a job hasn't started
        # instead of a bare "Queued". None for anything already running.
        "queue_waits": {j.id: runtime._queue_wait(j) for j in pending},
        "queue_has_cancel_protected": any(
            j.id == protected_id for j in pending
        ),
        "error": runtime._notice_text(error),
        "notice": runtime._notice_text(notice),
        "page": "queue",
        "active_tab": "queue",
    })


# A page's worth is what a phone can scan in about five screens now that a
# job is one line rather than a card.
_HISTORY_PER_PAGE = 25
_HISTORY_BULK_CAP = 20


@router.get("/queue/history", response_class=HTMLResponse)
async def queue_history(
    request: Request,
    p: int = 1,
    jp: int = 1,
    error: str = "",
    started: str = "",
    attention: bool = False,
):
    """The History tab: every finished job, newest first, paged from jobs.db so
    the record outlives the in-memory cap (which only the Queue/SSE views use).
    ``p`` walks the downloads table, ``jp`` the job cards above it, and each
    pager's links carry the other's page."""
    p = max(1, p)
    jp = max(1, jp)
    if attention:
        # This list drains as it is read: the rows on the page stop needing
        # attention once it renders. Numbered pages over a shrinking set walk
        # past rows nobody saw, so it always shows the first page and the next
        # visit shows what is left.
        p = jp = 1
    def _stamp(rows):
        for r in rows:
            ts = r.get("finished_at") or r.get("created_at")
            r["when"], r["when_exact"] = runtime._when_label(ts)
        return rows

    def _load_page(page, bulk_page):
        # Two layers: meaningful jobs as cards, plain downloads as the table
        # underneath. Both walk the archive a page at a time.
        recoveries = runtime._retire_gone_recoveries(
            job_persistence.recovery_history(attention_only=True)
            if attention else job_persistence.recovery_history()
        )
        recoveries = _stamp(recoveries)
        bulk_rest = job_persistence.history_count(
            bulk=True, exclude_recoveries=True, attention_only=attention)
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
                attention_only=attention,
            ))
        total = job_persistence.history_count(
            bulk=False, exclude_recoveries=True, attention_only=attention)
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
            attention_only=attention,
        ))
        return (bulk, bulk_total, bulk_page, bulk_pages,
                total, pages, page, rows)

    loop = asyncio.get_running_loop()
    (bulk_jobs, bulk_total, jp, bulk_pages,
     total, pages, p, rows) = await loop.run_in_executor(
        None, lambda: _load_page(p, jp))
    if attention:
        # Only the rows this page shows: clearing the backlog before the
        # query emptied the list the reader had just opened.
        shown = [row.get("id") for row in (*bulk_jobs, *rows)]
        await loop.run_in_executor(
            None, lambda: job_persistence.acknowledge_listed_attention(shown))
    return runtime._tr(request, "history.html", {
        "page": "queue", "active_tab": "history",
        "bulk_jobs": bulk_jobs, "jobs": rows,
        "bulk_total": bulk_total, "bulk_shown": len(bulk_jobs),
        "bulk_page": jp, "bulk_pages": bulk_pages,
        "cur_page": p, "pages": pages, "total": total,
        "attention_only": attention,
        "history_unavailable": not job_persistence.ready_for_admission(),
        "error": runtime._notice_text(error),
        # What a Retry from this page just did. Retry no longer opens the job
        # card, so the page it left the user on has to say what started and
        # offer the way in.
        "started_job": job_mgr.registry.get(started) if started else None,
    })


@router.post("/queue/clear")
async def queue_clear(request: Request):
    """Clear the History: drop finished/canceled/failed jobs from the registry
    and the full on-disk archive. In-flight jobs are untouched."""
    with runtime._STARTUP_RECOVERY_LOCK:
        if runtime._startup_recovery_status_value() != "clear":
            return runtime._durable_recovery_response(
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
                url="/queue/history?error=" + runtime._notice_key(message),
                status_code=303,
            )
        job_mgr.registry.clear_finished()
    return RedirectResponse(url="/queue/history", status_code=303)


@router.post("/queue/cancel-pending")
async def queue_cancel_pending():
    # Parked reviews are deliberately exempt: the queue page no longer shows
    # them, and a bulk clear must never take something the user can't see.
    protected = 0
    finishing = 0
    unsaved = 0
    for j in list(job_mgr.registry.pending_and_running()):
        if j.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        if not job_mgr.request_cancel(j):
            if job_mgr.cancel_is_protected(j):
                protected += 1
            elif j.importing:
                finishing += 1
            elif j.status in job_mgr.TERMINAL:
                continue
            else:
                unsaved += 1
    if protected or finishing or unsaved:
        parts = ["Queue cleared where safe."]
        if protected:
            parts.append("The interrupted-download recovery was not cancelled.")
        if finishing:
            noun = "job is" if finishing == 1 else "jobs are"
            parts.append(
                f"{finishing} {noun} already importing and will finish."
            )
        if unsaved:
            noun = "job" if unsaved == 1 else "jobs"
            parts.append(
                f"{unsaved} {noun} couldn't be cancelled because the update "
                "couldn't be saved."
            )
        message = " ".join(parts)
        return RedirectResponse(
            url="/queue?notice=" + runtime._notice_key(message), status_code=303
        )
    return RedirectResponse(url="/queue", status_code=303)
