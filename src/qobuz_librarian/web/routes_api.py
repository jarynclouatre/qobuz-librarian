"""JSON and live-progress endpoints under /api."""
import asyncio
import hashlib
import logging
import queue

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import job_persistence, runtime
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


def _finished_search_downloads() -> tuple[set[str], set[tuple[str, str]]]:
    """Albums and tracks a finished download has put on disk in full.

    Search rows poll for their own state, and a row whose download has just
    ended is no longer queued. Without this it falls back to offering the same
    download again, on an album the app has just fetched. Only jobs that landed
    everything count, so a partial fill keeps its download button.
    """
    albums = set()
    tracks = set()
    for job in job_mgr.registry.all():
        if not job.landed_complete or job.status not in job_mgr.TERMINAL:
            continue
        single = getattr(job, "single", None) or {}
        track_id = str(single.get("track_id") or "")
        album_id = str(single.get("album_id") or job.album_id or "")
        if not album_id:
            continue
        if track_id:
            tracks.add((album_id, track_id))
        else:
            albums.add(album_id)
    return albums, tracks


# Empty 500ms ticks before we emit a ping heartbeat. It keeps reverse proxies
# from dropping the EventSource on a quiet scan, and it is a named event rather
# than an SSE comment so the browser can see it: a socket that dies without
# closing raises no error, and silence between pings is the only signal the
# page has that the stream is gone.
_SSE_HEARTBEAT_TICKS = cfg.SSE_HEARTBEAT_TICKS


def _stream_session_active(request: Request) -> bool:
    if web_auth.auth_disabled():
        return True
    token = request.cookies.get(web_auth.SESSION_COOKIE)
    return bool(token) and web_auth.verify_session(token)


@router.get("/api/diagnostics", response_class=HTMLResponse)
async def api_diagnostics(request: Request):
    """Htmx partial that returns just the diagnostics list items for the Recheck button."""
    loop = asyncio.get_running_loop()
    return HTMLResponse(await loop.run_in_executor(
        None, runtime._diagnostics_fragment, request))


@router.get("/api/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def _generator():
        # Reconnect quickly so a backgrounded tab's progress bar catches up to
        # the live count soon after it's brought back to the foreground.
        yield "retry: 750\n\n"
        if not _stream_session_active(request):
            yield "event: auth\ndata: signed_out\n\n"
            return
        if (job.status in job_mgr.TERMINAL
                or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
            replay = (
                job.log_lines[-job.REPLAY_TAIL:]
                if job.REPLAY_TAIL > 0
                else ()
            )
            for line in replay:
                if not _stream_session_active(request):
                    yield "event: auth\ndata: signed_out\n\n"
                    return
                escaped = line.replace("\n", " ").replace("\r", "")
                yield f"data: {escaped}\n\n"
            if not _stream_session_active(request):
                yield "event: auth\ndata: signed_out\n\n"
                return
            yield f"event: done\ndata: {job.status.value}\n\n"
            return
        sub = job.subscribe()
        empty_ticks = 0
        try:
            while not runtime._STOP_SIGNALLED.is_set():
                if not _stream_session_active(request):
                    yield "event: auth\ndata: signed_out\n\n"
                    break
                try:
                    line = sub.get_nowait()
                    if not _stream_session_active(request):
                        yield "event: auth\ndata: signed_out\n\n"
                        break
                    empty_ticks = 0
                    if line == job_mgr.STREAM_END:
                        yield f"event: done\ndata: {job.status.value}\n\n"
                        break
                    if line.startswith(job_mgr.PROGRESS_PREFIX):
                        yield ("event: progress\ndata: "
                               + line[len(job_mgr.PROGRESS_PREFIX):] + "\n\n")
                        continue
                    if line.startswith(job_mgr.REVIEW_CHANGED):
                        continue  # review-sync nudge, handled by the review stream
                    escaped = line.replace("\n", " ").replace("\r", "")
                    yield f"data: {escaped}\n\n"
                except queue.Empty:
                    if (job.status in job_mgr.TERMINAL
                            or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                        yield f"event: done\ndata: {job.status.value}\n\n"
                        break
                    await asyncio.sleep(0.5)
                    empty_ticks += 1
                    if empty_ticks >= _SSE_HEARTBEAT_TICKS:
                        empty_ticks = 0
                        yield "event: ping\ndata: 1\n\n"
                except Exception:
                    _log.exception(
                        "SSE stream error for job %s", job.id)
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/jobs/{job_id}/review-stream")
async def job_review_stream(request: Request, job_id: str):
    """Live channel for an awaiting-review page: emits `event: review` whenever
    selection or candidates change (a tick/untick/hide in this or another tab),
    so every open view stays in sync. Closes once the job leaves review (the
    page then reloads to show the executing/finished state). Separate from the
    progress stream, which closes the moment a scan finishes."""
    # Only a LIVE job (in the registry) has a producer that fans out review
    # nudges; a historical/evicted review still renders and saves selection via
    # the disk fallback, but can't receive live cross-tab updates, so end its
    # stream cleanly rather than 404 (which surfaces as a console error) or hold
    # a socket that never gets a nudge.
    job = job_mgr.registry.get(job_id)

    async def _generator():
        yield "retry: 1000\n\n"
        if not _stream_session_active(request):
            yield "event: auth\ndata: signed_out\n\n"
            return
        if job is None or job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            yield "event: closed\ndata: inactive\n\n"
            return
        sub = job.subscribe()
        empty_ticks = 0
        try:
            while not runtime._STOP_SIGNALLED.is_set():
                if not _stream_session_active(request):
                    yield "event: auth\ndata: signed_out\n\n"
                    break
                try:
                    line = sub.get_nowait()
                    if not _stream_session_active(request):
                        yield "event: auth\ndata: signed_out\n\n"
                        break
                    if line.startswith(job_mgr.REVIEW_CHANGED):
                        # The data names the originating tab (or "changed" for a
                        # server-side sync) so that tab can skip reloading a DOM
                        # its own action already brought up to date.
                        origin = line[len(job_mgr.REVIEW_CHANGED):]
                        yield f"event: review\ndata: {origin or 'changed'}\n\n"
                    # All other fanned-out lines (log/progress/end) are ignored
                    # here; this channel only carries review-sync nudges.
                except queue.Empty:
                    if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
                        yield f"event: closed\ndata: {job.status.value}\n\n"
                        break
                    await asyncio.sleep(0.5)
                    empty_ticks += 1
                    if empty_ticks >= _SSE_HEARTBEAT_TICKS:
                        empty_ticks = 0
                        yield "event: ping\ndata: 1\n\n"
                except Exception:
                    _log.exception(
                        "review event stream failed for job %s", job.id)
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _job_to_dict(job, *, log_tail: int = 50):
    out = {
        "id": job.id,
        "status": job.status.value,
        "title": job.title,
        "edition": job.edition,
        "display_title": job.display_title,
        "artist": job.artist,
        "album_id": job.album_id,
        "summary": job.summary,
        "error": job.error,
        "quality_shortfall": job.quality_shortfall,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
    if log_tail:
        out["log_lines"] = job.log_lines[-log_tail:]
    return out


@router.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        # A finished job evicted past MAX_FINISHED is still on disk; fall back
        # to the archive so a poller gets its terminal status instead of a 404
        # (mirrors how GET /jobs/{job_id} rehydrates from history).
        job = job_mgr.load_historical_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_dict(job)


@router.get("/api/queue/count")
async def queue_count():
    """Live count of in-flight jobs (pending/scanning/running; parked reviews
    have their own dots) so the nav Queue badge stays in sync without a page
    reload. The badge is otherwise server-rendered once per page, which left it
    stale (e.g. reading "1" next to an empty Queue) after a job finished while
    you sat on another page."""
    active = [j for j in job_mgr.registry.pending_and_running()
              if j.status != job_mgr.JobStatus.AWAITING_REVIEW]
    revision = "\n".join(sorted(
        f"{j.id}:{j.status.value}:{len(j.candidates or [])}"
        for j in active
    ))
    return JSONResponse({
        "count": len(active),
        "running": any(
            j.status in (job_mgr.JobStatus.RUNNING, job_mgr.JobStatus.SCANNING)
            for j in active
        ),
        "signature": hashlib.sha256(revision.encode("utf-8")).hexdigest()[:16],
        # Status alone: the signature above moves every time a scan adds a
        # candidate, which would redraw the Queue on every poll for hours.
        "rows": runtime._queue_rows_signature(active),
        # Carried on the same poll so the nav's warning dot appears the moment
        # a job needs the user, not at their next full page load.
        "attention": job_persistence.attention_count(),
    })


@router.get("/api/search/availability")
async def search_availability():
    albums, tracks, scanning_albums = runtime._active_search_downloads()
    owned_albums, owned_tracks = _finished_search_downloads()
    # A re-queued album is active work again, so it reads as queued, not owned.
    owned_albums.difference_update(albums)
    owned_tracks.difference_update(tracks)
    return JSONResponse({
        "queued": sorted(
            [f"album-{album_id}" for album_id in albums]
            + [f"track-{album_id}-{track_id}"
               for album_id, track_id in tracks]
        ),
        "scanning": sorted(
            f"album-{album_id}" for album_id in scanning_albums),
        "owned": sorted(
            [f"album-{album_id}" for album_id in owned_albums]
            + [f"track-{album_id}-{track_id}"
               for album_id, track_id in owned_tracks]
        ),
    })


@router.get("/api/jobs")
async def jobs_list(status: str = "", limit: int = 50):
    """List jobs as JSON. Optional `status` filter ('pending', 'running',
    'awaiting_review', 'scanning', 'done', 'failed', 'canceled').
    `limit` caps the response, most recent first.

    Live (non-terminal) jobs come from the in-memory registry. Terminal jobs
    (done/failed/canceled) come from the registry too, but it only keeps the
    most-recent MAX_FINISHED of them, so we also reach into the on-disk archive
    to surface jobs evicted past that cap; otherwise `status=done` could never
    return anything older than the last ~50 finishes."""
    wanted = status.strip().lower() or None
    if wanted is not None:
        valid = {s.value for s in job_mgr.JobStatus}
        if wanted not in valid:
            raise HTTPException(status_code=400,
                                detail="Unknown status filter")
    cap = max(1, min(limit, 500))
    terminal_values = {s.value for s in job_mgr.TERMINAL}
    want_terminal = wanted in terminal_values if wanted else True

    matching = []
    seen = set()
    for j in reversed(job_mgr.registry.all()):
        if wanted and j.status.value != wanted:
            continue
        matching.append(_job_to_dict(j, log_tail=0))
        seen.add(j.id)
        if len(matching) >= cap:
            break

    # The registry only holds the newest MAX_FINISHED terminal jobs; the
    # archive keeps far more.
    if want_terminal and len(matching) < cap:
        # Filter in SQL: fetching the newest `cap` rows and filtering here
        # would return too few (or none) whenever those rows are mostly other
        # statuses, even though older matching history exists.
        for row in job_persistence.history_page(cap, 0, status=wanted):
            if row["id"] in seen:
                continue
            matching.append({
                "id": row["id"],
                "status": row["status"],
                "title": row["title"],
                "edition": row["edition"],
                "display_title": job_mgr.release_title(
                    row["title"], row["edition"]
                ),
                "artist": row["artist"],
                "album_id": row["album_id"] or None,
                "error": row["error"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            })
            seen.add(row["id"])
            if len(matching) >= cap:
                break

    return JSONResponse({"jobs": matching, "count": len(matching)})
