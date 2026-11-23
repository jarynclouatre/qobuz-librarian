"""Routes for the Lyrics page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.library import lyrics as library_lyrics
from qobuz_librarian.web import (
    flows,
    job_labels,
    job_persistence,
    refusals,
    rendering,
    scans,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()


def _stopped_line(summary):
    """A stopped scan's summary without the "Stopped" the chip beside it
    already says."""
    line = (summary or "").removeprefix(job_persistence.RESTART_INTERRUPTED).strip()
    for said, plain in ((f"{library_lyrics.STOPPED}checking ", "Checked "),
                        (f"{library_lyrics.STOPPED}reading ", "Read ")):
        if line.startswith(said):
            return f"{plain}{line[len(said):]} The next scan carries on from there."
    return line or "The next scan carries on from where this one stopped."


@router.get("/lyrics", response_class=HTMLResponse)
async def lyrics_page(request: Request):
    names = lyric_fetch.usable_providers(cfg.LYRICS_PROVIDERS)
    if len(names) > 1:
        providers = f"From {', '.join(names[:-1])} and {names[-1]}."
    elif names:
        providers = f"From {names[0]}."
    else:
        providers = "No known lyric provider is set."
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
    lyrics_stopped = (
        latest_lyrics
        if latest_lyrics is not None
        and latest_lyrics.status == job_mgr.JobStatus.CANCELED
        else None
    )
    running = scans._active_scan(
        "lyrics", statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING))
    return rendering._tr(request, "lyrics.html", {
        "page": "lyrics",
        "have_lyrics": lyric_fetch.AVAILABLE,
        "last_run": job_labels._tool_last_run_age("lyrics"),
        # A scan in flight shows its progress in place of the launcher.
        "lyrics_running": running,
        "queue_wait": job_labels._queue_wait(running) if running else None,
        "lyrics_failed": lyrics_failed,
        "lyrics_stopped": lyrics_stopped,
        "lyrics_stopped_line": _stopped_line(lyrics_stopped.summary) if lyrics_stopped else "",
        "lyrics_done": (
            latest_lyrics
            if latest_lyrics is not None
            and latest_lyrics.status == job_mgr.JobStatus.DONE
            else None),
        "lyrics_summary": (
            latest_lyrics.summary
            if latest_lyrics is not None
            and latest_lyrics.status == job_mgr.JobStatus.DONE
            else ""),
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
        return RedirectResponse(url="/lyrics", status_code=303)
    job = job_mgr.Job(title="Lyrics scan")
    job.execute_kind = "lyrics"
    submitted = job_mgr.submit(
        job,
        lambda j: flows.run_library_lyrics(j, rescan=rescan, synced_only=synced_only),
    )
    if submitted is None:
        return scans._scan_submission_failure_response(request, "/lyrics")
    return RedirectResponse(url="/lyrics", status_code=303)
