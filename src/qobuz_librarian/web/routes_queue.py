"""Routes for the Queue and History pages."""
import asyncio
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from qobuz_librarian.download_result import download_notes_in
from qobuz_librarian.web import (
    download_outcomes,
    job_labels,
    job_persistence,
    queue_recovery,
    refusals,
    rendering,
    review_pages,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


@router.head("/queue")
async def queue_head():
    return Response(status_code=200)


# Enough of each to read on a phone without pushing the live rows away.
_QUEUE_ATTENTION = 5
_QUEUE_FINISHED = 8
_JOB_ID_RE = re.compile(r"[0-9a-f]{6,64}")


# A cancelled row's chip already says these.
_PLAIN_CANCELS = (
    job_mgr.CANCELLED_UNSTARTED,
    download_outcomes._DOWNLOAD_SUMMARY_LABELS["cancelled"],
)


def _stamp(rows):
    for r in rows:
        ts = r.get("finished_at") or r.get("created_at")
        r["when"], r["when_exact"] = job_labels._when_label(ts)
        r["download_notes"] = download_notes_in(r.get("summary"))
        if r.get("status") == "canceled" and r.get("summary") in _PLAIN_CANCELS:
            r["summary"] = ""
        elif r.get("status") == "awaiting_review":
            # A parked review's count changes as it is worked; its own
            # page has the live one.
            r["summary"] = review_pages.review_summary_without_count(
                r.get("execute_kind"), r.get("summary"))
        else:
            r["summary"] = review_pages.summary_without_old_advice(
                r.get("execute_kind"), r.get("summary"))
    return rows


def _queue_attention():
    """The finished jobs that need attention, as many as the Queue shows, and
    how many more History holds. Listing them is what acknowledges them, as on
    History's own attention list."""
    recoveries = rendering._retire_gone_recoveries(
        job_persistence.recovery_history(attention_only=True))
    for row in recoveries:
        row["bulk"] = True
    rows = (recoveries + job_persistence.history_page(
        _QUEUE_ATTENTION, 0, exclude_recoveries=True, attention_only=True)
    )[:_QUEUE_ATTENTION]
    total = len(recoveries) + job_persistence.history_count(
        exclude_recoveries=True, attention_only=True)
    job_persistence.acknowledge_listed_attention([r["id"] for r in rows])
    return _stamp(rows), max(0, total - len(rows))


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, error: str = "", notice: str = "",
                     shown: str = ""):
    """The Queue tab: finished jobs that need attention, jobs in flight
    (pending / scanning / running), and the latest finished work. Parked
    reviews ride along in ``pending`` but the template and the badge both
    filter them out; they render on their own surfaces. The whole record of
    finished jobs is the History tab.

    The page redraws its live part as jobs move. ``shown`` names the attention
    rows already on the page above it, so a redraw does not list them a second
    time under Finished."""
    pending = job_mgr.registry.pending_and_running()
    loop = asyncio.get_running_loop()
    attention, attention_more = [], 0
    if rendering._is_htmx(request):
        shown_ids = [i for i in shown.split(",") if _JOB_ID_RE.fullmatch(i)]
    else:
        attention, attention_more = await loop.run_in_executor(
            None, _queue_attention)
        shown_ids = [row["id"] for row in attention]
    finished = await loop.run_in_executor(
        None, lambda: _stamp(job_persistence.history_page(
            _QUEUE_FINISHED, 0, work_only=True, exclude_ids=shown_ids)))
    return rendering._tr(request, "queue.html", {
        "pending": pending,
        "queue_rows_signature": job_labels._queue_rows_signature(pending),
        # Why a waiting job hasn't started, where that is more than its place
        # in the list. None for anything already running.
        "queue_waits": {j.id: job_labels._queue_wait(j) for j in pending},
        "attention_jobs": attention,
        "attention_more": attention_more,
        "finished_jobs": finished,
        "queue_src": "/queue" + (
            "?shown=" + ",".join(shown_ids) if shown_ids else ""),
        "error": rendering._notice_text(error),
        "notice": rendering._notice_text(notice),
        "page": "queue",
        "active_tab": "queue",
    })


_HISTORY_PER_PAGE = 25


@router.get("/queue/history", response_class=HTMLResponse)
async def queue_history(
    request: Request,
    p: int = 1,
    error: str = "",
    started: str = "",
    attention: bool = False,
):
    """The History tab: every finished job, newest first, paged from jobs.db so
    the record outlives the in-memory cap (which only the Queue/SSE views use)."""
    # The attention list drains as it is read: the rows on the page stop
    # needing attention once it renders. Numbered pages over a shrinking set
    # walk past rows nobody saw, so it always shows the first page and the
    # next visit shows what is left.
    p = 1 if attention else max(1, p)

    def _load_page(page):
        recoveries = rendering._retire_gone_recoveries(
            job_persistence.recovery_history(attention_only=attention))
        for row in recoveries:
            row["bulk"] = True
        total = job_persistence.history_count(
            exclude_recoveries=True, attention_only=attention)
        pages = max(1, -(-total // _HISTORY_PER_PAGE))
        page = min(page, pages)
        rows = job_persistence.history_page(
            _HISTORY_PER_PAGE, (page - 1) * _HISTORY_PER_PAGE,
            exclude_recoveries=True, attention_only=attention)
        # A retained recovery is asking for a decision, so it stays pinned to
        # the first page rather than repeating under every one.
        if page == 1:
            rows = recoveries + rows
        return _stamp(rows), len(recoveries) + total, pages, page

    loop = asyncio.get_running_loop()
    rows, total, pages, p = await loop.run_in_executor(
        None, lambda: _load_page(p))
    if attention:
        # Only the rows this page shows, after the query that listed them.
        shown = [row.get("id") for row in rows]
        await loop.run_in_executor(
            None, lambda: job_persistence.acknowledge_listed_attention(shown))
    return rendering._tr(request, "history.html", {
        "page": "queue", "active_tab": "history",
        "jobs": rows, "total": total, "cur_page": p, "pages": pages,
        "attention_only": attention,
        "history_unavailable": not job_persistence.ready_for_admission(),
        "error": rendering._notice_text(error),
        # What a Retry from this page just started; Retry stays on History,
        # which links to the new job.
        "started_job": job_mgr.registry.get(started) if started else None,
    })


@router.post("/queue/clear")
async def queue_clear(request: Request):
    """Clear the History: drop finished/canceled/failed jobs from the registry
    and the full on-disk archive. In-flight jobs are untouched."""
    if queue_recovery._startup_recovery_status_value() is None:
        # Recovery was never checked because this app does not hold the run
        # lock; say why rather than blame an interrupted download.
        busy = refusals._lock_busy_response(request)
        if busy is not None:
            return busy

    def _clear():
        # Settling saved recovery holds this lock for as long as it takes;
        # wait for it off the event loop.
        with queue_recovery._STARTUP_RECOVERY_LOCK:
            if (queue_recovery._startup_recovery_status_value() != "clear"
                    and not queue_recovery._terminal_queue_waiting()):
                return "recovery"
            if not job_persistence.clear_history():
                return "unsaved"
            job_mgr.registry.clear_finished()
        return None

    refused = await asyncio.get_running_loop().run_in_executor(None, _clear)
    if refused == "recovery":
        return refusals._durable_recovery_response(
            request,
            "History cannot be cleared while an interrupted download still "
            "has saved recovery state.",
        )
    if refused == "unsaved":
        message = (
            "History couldn't be cleared from the data folder. Nothing "
            "was removed; check the data volume and try again."
        )
        return RedirectResponse(
            url="/queue/history?error=" + rendering._notice_key(message),
            status_code=303,
        )
    return RedirectResponse(url="/queue/history", status_code=303)


@router.post("/queue/cancel-pending")
async def queue_cancel_pending():
    # Parked reviews are exempt: the queue page does not show them, and a bulk
    # clear must never take something the user can't see.
    finishing = 0
    unsaved = 0
    for j in list(job_mgr.registry.pending_and_running()):
        if j.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        if not job_mgr.request_cancel(j):
            if j.importing:
                finishing += 1
            elif j.status in job_mgr.TERMINAL:
                continue
            else:
                unsaved += 1
    if finishing or unsaved:
        parts = ["Queue cleared where safe."]
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
            url="/queue?notice=" + rendering._notice_key(message), status_code=303
        )
    return RedirectResponse(url="/queue", status_code=303)
