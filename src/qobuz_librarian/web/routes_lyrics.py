"""Routes for the Lyrics page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.web import flows, job_labels, qobuz_access, refusals, rendering, scans
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()


@router.get("/lyrics", response_class=HTMLResponse)
async def lyrics_page(request: Request):
    providers = ", ".join(cfg.LYRICS_PROVIDERS) or "Lrclib, NetEase, Musixmatch"
    lyrics_format = (cfg.LYRICS_FORMAT or "embed").lower()
    lyrics_format_label = {
        "embed": "Embedded tags",
        "sidecar": ".lrc sidecar files",
        "both": "Embedded tags and .lrc files",
    }.get(lyrics_format, lyrics_format)
    latest_lyrics = scans._last_finished("lyrics")
    lyrics_failed = (
        latest_lyrics
        if latest_lyrics is not None
        and latest_lyrics.status == job_mgr.JobStatus.FAILED
        else None
    )
    return rendering._tr(request, "lyrics.html", {
        "page": "lyrics",
        "have_lyrics": lyric_fetch.AVAILABLE,
        "creds_ok": qobuz_access._creds_ok(),
        "last_run": job_labels._tool_last_run_age("lyrics"),
        # A library-wide lyrics scan in flight, so the page says so instead of
        # showing the idle "Ready · Start lyrics scan" launcher while one runs.
        "lyrics_running": scans._active_scan(
            "lyrics", statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING)),
        "lyrics_failed": lyrics_failed,
        "lyrics_format": lyrics_format_label,
        "providers": providers,
    })


@router.post("/lyrics")
async def lyrics_scan(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        return busy
    form = await request.form()
    rescan = bool(form.get("rescan"))
    synced_only = bool(form.get("synced_only"))
    # Re-check after the form await: set_mode can flip to CLI mode inside that
    # yield, and everything from here to submit runs without yielding, so this
    # read-and-submit is atomic against the on-loop mode flip (same pattern as
    # queue_download).
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        return busy
    existing = scans._active_scan(
        "lyrics", statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    job = job_mgr.Job(title="Lyrics scan")
    job.execute_kind = "lyrics"
    submitted = job_mgr.submit(
        job,
        lambda j: flows.run_library_lyrics(j, rescan=rescan, synced_only=synced_only),
    )
    if submitted is None:
        return scans._scan_submission_failure_response(request, "/lyrics")
    return RedirectResponse(url=f"/jobs/{submitted.id}", status_code=303)
