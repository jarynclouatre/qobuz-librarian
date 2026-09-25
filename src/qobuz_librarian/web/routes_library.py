"""Routes for the Library page."""
import asyncio
import html
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    NoCredsError,
    QobuzAccess,
    QobuzEntitlementError,
    QobuzUnavailable,
)
from qobuz_librarian.library import (
    generation_state,
    library_scan_state,
    new_releases,
    scan_checkpoint,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import unreadable_artists as unreadable_artists_mod
from qobuz_librarian.web import (
    flows,
    hidden_pages,
    review_badges,
    review_pages,
    runtime,
    saved_reviews,
    scans,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()


def _review_job_from_library_state():
    """Rebuild the parked Library review from the saved baseline scan when no
    live job holds the /library surface, so a review lost to a swept cancel,
    a discarded scan job, or a corrupt persisted row on restart comes back
    instead of stranding the user on the finished status with no tabs. Mirrors
    the Upgrade/Downsample saved-state reconstruction; the live job stays
    primary (callers only reach here when _library_current_job() is None).
    """

    def _review_retired(state, missing):
        if not missing.get("complete"):
            return True
        missing_generation = int(missing.get("generation") or 0)
        # Generation is the durable review identity. Keep the timestamp
        # fallback only for a pre-generation saved snapshot.
        missing_updated = float(missing.get("updated_at") or 0.0)
        retired_generation = int(state.get("review_retired_generation") or 0)
        retired_at = float(state.get("review_retired_at") or 0.0)
        return bool((
            missing_generation
            and retired_generation == missing_generation
        ) or (
            not missing_generation
            and retired_at
            and retired_at >= missing_updated
        ))

    with library_scan_state.review_state_lock(), saved_reviews._SAVED_REVIEW_LOCK:
        # The header settles most calls without reading every saved row.
        header = library_scan_state.summary()
        if _review_retired(header, header["kinds"].get("missing") or {}):
            return None
        full = library_scan_state.load()
        mstate = library_scan_state.kind_state("missing", full)
        if _review_retired(full, mstate):
            return None
        missing_generation = int(mstate.get("generation") or 0)
        missing_updated = float(mstate.get("updated_at") or 0.0)
        hidden = hidden_mod.load()
        specs = []
        for name, entry in (mstate.get("artists") or {}).items():
            for spec in (entry or {}).get("candidates") or []:
                artist = spec.get("artist") or name
                title = spec.get("title") or ""
                if hidden_mod.is_hidden(
                        hidden_mod.SCOPE_MISSING,
                        artist,
                        title,
                        hidden,
                        year=(spec.get("payload") or {}).get("year"),
                ):
                    continue
                specs.append(spec)
        if not specs:
            return None
        existing = _library_current_job()  # re-check under the lock
        if existing is not None:
            return existing
        job = job_mgr.Job(title="Library scan")
        job.kind = "scan"
        job.execute_kind = "library"
        job.execute_args = {
            "_library_review_generation": (
                missing_generation if missing_generation else missing_updated
            ),
        }
        job._execute_fn = runtime._resume_album_download(job, job.execute_args)
        for spec in specs:
            job.add_candidate(
                kind=spec.get("kind", "album"),
                title=spec.get("title") or "?",
                artist=spec.get("artist") or "",
                detail=spec.get("detail") or "",
                payload=spec.get("payload") or {},
                selected=False,
            )
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        job.summary = (flows.library_review_summary(job.candidates)
                       + ", from your last library scan.")
        if not saved_reviews._publish_saved_review(job):
            return None
        return job


def _library_header_note():
    """What the header note beside the Library title should say, or None when
    nothing library-side is in flight.

    A parked review keeps the /library surface (see _library_current_job), so
    work crawling behind it has nowhere to show its progress card. This note
    carries it instead: without it a refresh that takes a quarter of an hour
    showed the word "Refreshing…" and nothing else, and the download phase that
    follows an approved review said "Refreshing…" as well.
    """
    job = scans._active_scan(
        "library", statuses=("pending", "scanning", "running"))
    if job is None:
        return None
    if job.status.value == "pending":
        return {"label": "Queued", "detail": "",
                "title": "Waits for the running job to finish."}
    if job.status.value == "running":
        return {"label": "Downloading…", "detail": "",
                "title": "Downloading the albums selected in the review."}
    total = int(getattr(job, "progress_total", 0) or 0)
    current = int(getattr(job, "progress_current", 0) or 0)
    unit = str(getattr(job, "progress_unit", "") or "").strip()
    detail = ""
    if total:
        detail = f"{current:,} of {total:,}"
        if unit:
            detail += f" {unit}s"
    return {"label": "Refreshing…", "detail": detail,
            "title": "Checking your folders for music added outside the app."}


def _last_finished_library_job():
    """The most recent library scan that has stopped, or None."""
    latest = None
    for j in job_mgr.registry.all():
        if getattr(j, "execute_kind", "") != "library":
            continue
        if j.status not in (job_mgr.JobStatus.DONE, job_mgr.JobStatus.FAILED):
            continue
        if (latest is None
                or (j.finished_at or 0) > (latest.finished_at or 0)):
            latest = j
    return latest


def _library_refresh_outcome():
    """What the library work that just ended actually did, or ""."""
    latest = _last_finished_library_job()
    if latest is None or latest.status is job_mgr.JobStatus.FAILED:
        return ""
    if time.time() - (latest.finished_at or 0) > 300:
        return ""
    return str(latest.summary or "").strip()


def _library_refresh_failure():
    """Why the last library scan stopped, while that is still the last thing
    a refresh did, or "".

    Library owns its whole lifecycle, so a refresh that could not run has to
    say so on this page. It used to arrive as the same passing note a
    successful refresh uses, which meant it faded after six seconds, never
    survived a reload, and existed in full only as a History row.
    """
    latest = _last_finished_library_job()
    if latest is None or latest.status is not job_mgr.JobStatus.FAILED:
        return ""
    # Any library work started or stopped since then supersedes it, whatever
    # became of that work.
    since = latest.finished_at or 0
    for j in job_mgr.registry.all():
        if (j is not latest
                and getattr(j, "execute_kind", "") == "library"
                and max(j.created_at or 0, j.started_at or 0,
                        j.finished_at or 0) > since):
            return ""
    return str(latest.error or latest.summary or "").strip()


def _library_current_job():
    """The baseline scan that owns the /library surface right now (still
    pending / scanning / awaiting-review / running), or None when the surface
    is idle and shows the launcher. New-release checks never own it; their
    results live on their own job page, so the
    Missing Albums / Gap Fill review can't be displaced by an overnight
    check. A parked review outranks running work: after a tab-scoped download
    splits the review, the user stays on the tab still waiting for them while
    the download runs in the queue."""
    states = (job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
              job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.RUNNING)
    cur = None
    for j in job_mgr.registry.all():
        if (getattr(j, "execute_kind", "") != "library"
                or j.status not in states):
            continue
        if cur is None:
            cur = j
            continue
        j_rev = j.status == job_mgr.JobStatus.AWAITING_REVIEW
        cur_rev = cur.status == job_mgr.JobStatus.AWAITING_REVIEW
        if (j_rev, (j.created_at or 0)) >= (cur_rev, (cur.created_at or 0)):
            cur = j
    return cur


def _library_page_context(page, tab, q):
    """Everything /library reads from the data and music volumes."""
    badge_generation = review_badges.ready_generation("library")
    # Albums a terminal run downloaded are still listed by the review this
    # process holds in memory until they are applied. Do it before anything
    # reads the review, so the counts and the tabs agree with the library.
    flows.apply_pending_review_removals()
    library_generation = scans._truthful_library_generation()
    ctx = {
        "creds_ok": bool(runtime._read_creds().get("auth_token")),
        "qobuz_ready": runtime._qobuz_ready(), "page": "library",
        "library_scan_state": scans._library_scan_state(),
        # Freshness line: when a full gap scan last completed, and whether one
        # ever has (the new-release baseline is only seeded by a clean finish).
        "last_full_scan": _last_scan_age(),
        "baseline_complete": generation_state.baseline_complete(
            library_generation
        ),
        "library_baseline_exists": (
            generation_state.library_snapshot_available(library_generation)
        ),
        "new_release_baseline_complete": new_releases.is_baseline_complete(),
        "library_generation": library_generation,
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_MISSING),
        # Why a finished review retired ("discarded" / "worked_through" / ""),
        # so the finished-state card reads right.
        "library_review_retired_reason": "",
        "JobStatus": job_mgr.JobStatus,
        # Drives the header's quiet refresh: hidden while a crawl is already
        # under way (the "Refreshing…" note takes its place over a parked
        # review; a bare scan shows its own progress body).
        "library_refresh_running": scans._active_scan(
            "library", statuses=("pending", "scanning", "running")) is not None,
        "library_header_note": _library_header_note(),
        "library_refresh_scanning": scans._active_scan(
            "library", statuses=("pending", "scanning")) is not None,
        "library_refresh_failure": _library_refresh_failure(),
        "unreadable_artists": unreadable_artists_mod.load(),
        "auto_library_scan": cfg.AUTO_LIBRARY_SCAN,
    }
    # Single-surface rule (same as /repair): a scan in flight or a parked
    # review renders inline right here, so results never hide behind the
    # launcher and never live under the Queue nav.
    ljob = _library_current_job()
    if ljob is None and ctx["library_baseline_exists"]:
        # No live job holds the surface, but the baseline is complete, so rebuild
        # the parked review from saved scan state so post-baseline ALWAYS shows
        # the Missing Albums / Gap Fill tabs, never "Baseline ready" with none.
        ljob = _review_job_from_library_state()
    ctx["library_job"] = ljob
    ctx["census"] = None
    # Resume hint: an interrupted scan's checkpoint, while no scan runs. A
    # parked review can sit above it, so it is not tied to an idle surface.
    latest_status = str(
        (ctx["library_generation"].get("latest_attempt") or {}).get(
            "status"
        )
        or "never"
    )
    ctx["library_resume"] = (
        scan_checkpoint.pending()
        if not ctx["library_refresh_scanning"] and (
            not int(ctx["library_generation"].get("generation") or 0)
            or latest_status in {"running", "failed", "incomplete"}
        )
        else None
    )
    if ljob is not None:
        ctx["queue_wait"] = runtime._queue_wait(ljob)
        # A full load has to be able to land on either tab: the address is the
        # only thing a reload or a bookmark still carries.
        ctx.update(review_pages._review_context(ljob, page, q, tab=tab))
    elif ctx["library_baseline_exists"]:
        # Finished-state copy: the "Review complete" vs "Review discarded"
        # card keys off why the review retired.
        ctx["library_review_retired_reason"] = (
            library_scan_state.summary()["review_retired_reason"])
    # The census renders whenever the page is calm (no job, or a parked
    # review below it), so it doesn't blink in and out with review state.
    if ctx["library_baseline_exists"] and (
            ljob is None or ljob.status == job_mgr.JobStatus.AWAITING_REVIEW):
        ctx["census"] = runtime._census_view()
    badge_ack = None
    if (ljob is not None
            and ljob.status == job_mgr.JobStatus.AWAITING_REVIEW
            and (ctx.get("review_counts") or {}).get("total")):
        badge_ack = ("library", badge_generation)
    return ctx, badge_ack


@router.get("/library", response_class=HTMLResponse)
async def library_page(request: Request, page: int = 1, tab: str = "",
                       q: str = ""):
    notice_bits = []
    _skipped = request.query_params.get("skipped", "")
    if _skipped.isdigit() and int(_skipped):
        n = int(_skipped)
        notice_bits.append(
            f"{n} album{'s' if n != 1 else ''} already in your library, skipped.")
    if request.query_params.get("noselection"):
        notice_bits.append("Nothing else is selected on that tab."
                           if notice_bits else
                           "Nothing is selected on that tab yet.")
    elif request.query_params.get("approved"):
        notice_bits.append("Download queued.")
    # Bring all back redirects here with what happened, a store-write failure
    # included. Without this the page dropped the message and a restore that
    # never ran looked exactly like one that worked.
    _notice = runtime._notice_text(request.query_params.get("notice"))
    if _notice:
        notice_bits.append(_notice)
    loop = asyncio.get_running_loop()
    ctx, badge_ack = await loop.run_in_executor(
        None, lambda: _library_page_context(page, tab, q))
    ctx["library_notice"] = " ".join(notice_bits)
    ctx["error"] = runtime._notice_text(request.query_params.get("error"))
    return runtime._tr(request, "library.html", ctx, review_badge_ack=badge_ack)


@router.get("/library/refresh-note", response_class=HTMLResponse)
async def library_refresh_note(request: Request):
    """Fragment behind the header's refresh control. The "Refreshing…" note
    polls this while a refresh runs, so it swaps back to the idle icon when
    the scan ends instead of sitting there forever; the idle icon itself
    never polls."""
    loop = asyncio.get_running_loop()
    def _context():
        library_generation = generation_state.load()
        return {
            "qobuz_ready": runtime._qobuz_ready(),
            "baseline_complete": generation_state.baseline_complete(
                library_generation
            ),
            "library_baseline_exists": (
                generation_state.library_snapshot_available(
                    library_generation
                )
            ),
            "new_release_baseline_complete": (
                new_releases.is_baseline_complete()
            ),
            "library_generation": library_generation,
            "library_scan_state": scans._library_scan_state(),
            "library_job": _library_current_job(),
            "JobStatus": job_mgr.JobStatus,
            "library_refresh_running": scans._active_scan(
                "library",
                statuses=("pending", "scanning", "running"),
            ) is not None,
            "library_header_note": _library_header_note(),
            "library_refresh_scanning": scans._active_scan(
                "library", statuses=("pending", "scanning")) is not None,
            # Only the poll says this, so it lands once, when the work it was
            # watching ends. A page load has the review itself to read.
            "library_refresh_outcome": _library_refresh_outcome(),
            # A failure is not a passing note, so the poll delivers it out of
            # band into the page body and the page renders it on every load
            # until a refresh actually gets somewhere.
            "library_refresh_failure": _library_refresh_failure(),
            "refresh_failure_oob": True,
        }

    ctx = await loop.run_in_executor(None, _context)
    return runtime._tr(request, "_library_refresh.html", ctx)


@router.post("/library")
async def library_scan(
    request: Request,
    mode: str = Form("missing_albums"),
    force_full: str = Form(""),
    return_to: str = Form(""),
):
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    mode_norm = (mode or "").strip().lower()
    # Settings' Force full rescan posts here too. A scan that actually starts
    # still lands the user on /library, where it runs and is watched (the
    # single scan surface); only a refusal that starts nothing sends them back
    # to where they clicked from instead of bouncing them off Settings with an
    # error about a page they weren't on.
    error_home = "/settings" if return_to == "/settings" else "/library"
    # Run the submit off the event loop: it takes _auto_check_lock, which the
    # dashboard auto-triggers can hold across small data-volume reads, and the
    # loop shouldn't block on a (possibly NAS) mount, same reason the dashboard
    # does its disk work in an executor.
    loop = asyncio.get_running_loop()
    if mode_norm == "new_releases":
        # A new-release check compares the catalog against the baseline a completed
        # library scan builds; with no baseline there's nothing to compare against,
        # so it would crawl every artist, surface nothing, and (the old bug) flip
        # the baseline "done", stranding an interrupted library scan's resume.
        # Refuse and point at a library scan instead of running that empty crawl.
        if not new_releases.is_baseline_complete():
            msg = "Run a full library scan first."
            if runtime._is_htmx(request):
                return HTMLResponse(
                    f'<div class="ql-flash ql-flash-warning" data-flash><span>{html.escape(msg)}</span></div>',
                    status_code=200)
            return RedirectResponse(
                url="/library?error=" + runtime._notice_key(msg), status_code=303)
        existing = runtime._active_new_release_check()
        if existing is not None:
            # A check already crawling. Land on it rather than starting a
            # second crawl over the same catalogue.
            return RedirectResponse(
                url=f"/jobs/{existing.id}?waiting=1", status_code=303)
        try:
            credentials = await runtime._authorize_qobuz_for_web(
                QobuzAccess.CATALOGUE_ACTION
            )
        except (
            NoCredsError,
            AuthLost,
            QobuzUnavailable,
            QobuzEntitlementError,
            CredentialChanged,
            asyncio.TimeoutError,
        ) as exc:
            msg = job_mgr._qobuz_action_error_message(exc, unchanged=True)
            if runtime._is_htmx(request):
                return HTMLResponse(
                    runtime._ql_notice_html("error", html.escape(msg)),
                    status_code=200,
                )
            return RedirectResponse(
                url="/library?error=" + runtime._notice_key(msg),
                status_code=303,
            )
        # Same job the dashboard auto-check submits; its own execute_kind so
        # the review screen badges the new releases (left un-ticked).
        job = await loop.run_in_executor(
            None,
            lambda: runtime._start_new_release_check(credentials),
        )
        if job is None:
            return scans._scan_submission_failure_response(request, "/library")
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    scan_state = scans._library_scan_state()
    if not scan_state["ready"]:
        msg = scan_state["message"]
        if runtime._is_htmx(request):
            return HTMLResponse(
                f'<div class="ql-flash ql-flash-warning" data-flash><span>{html.escape(msg)}</span></div>',
                status_code=200)
        return RedirectResponse(
            url=error_home + "?error=" + runtime._notice_key(msg), status_code=303)
    existing = scans._active_library_scan()
    if existing is not None:
        return RedirectResponse(url="/library", status_code=303)
    try:
        credentials = await runtime._authorize_qobuz_for_web(
            QobuzAccess.CATALOGUE_ACTION
        )
    except (
        NoCredsError,
        AuthLost,
        QobuzUnavailable,
        QobuzEntitlementError,
        CredentialChanged,
        asyncio.TimeoutError,
    ) as exc:
        msg = job_mgr._qobuz_action_error_message(exc, unchanged=True)
        if runtime._is_htmx(request):
            return HTMLResponse(
                runtime._ql_notice_html("error", html.escape(msg)),
                status_code=200,
            )
        return RedirectResponse(
            url=error_home + "?error=" + runtime._notice_key(msg), status_code=303)
    # "library" (not "album") so the review screen knows this is the paced triage
    # surface; both modes run the same album executor and resume from a matching
    # checkpoint if one's waiting (see _start_library_scan / scan_library).
    force_full_scan = str(force_full or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    job = await loop.run_in_executor(
        None,
        lambda: scans._start_library_scan(
            credentials,
            partial_only=(mode_norm == "partial_fill"),
            force_full=force_full_scan,
        ),
    )
    if job is None:
        return scans._scan_submission_failure_response(request, error_home)
    return RedirectResponse(url="/library", status_code=303)


@router.post("/library/skip-setup")
async def skip_baseline_setup(request: Request):
    """Dismiss the first-run baseline-scan offer on the dashboard."""
    new_releases.note_auto_scan_attempted()
    return RedirectResponse(url="/", status_code=303)


@router.get("/library/hidden", response_class=HTMLResponse)
async def library_hidden(request: Request):
    return hidden_pages._hidden_view(request, hidden_mod.SCOPE_MISSING, page="library",
                        restore_action="/library/hidden/restore", back_url="/library",
                        restore_all_action="/library/hidden/restore-all")


@router.post("/library/hidden/restore")
async def library_hidden_restore(request: Request):
    return await hidden_pages._restore_hidden(request, hidden_mod.SCOPE_MISSING, "/library/hidden")


@router.post("/library/hidden/restore-all")
async def library_hidden_restore_all(request: Request):
    """Bring the whole dismissed set back from the Dismissed page. Unlike the
    finished-state /library/bring-back-all this can run with a live review
    parked, so the restored candidates are folded back into it, clearing the
    store alone would leave them invisible until a future scan most users
    never run."""
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    loop = asyncio.get_running_loop()
    q, fingerprints = await hidden_pages._restore_hidden_filter(
        request, hidden_mod.SCOPE_MISSING)

    try:
        if fingerprints is None:
            artists = await loop.run_in_executor(
                None, lambda: hidden_mod.take_all(hidden_mod.SCOPE_MISSING))
            restored = bool(artists)
        else:
            artists = []
            restored = await loop.run_in_executor(
                None, lambda: hidden_mod.restore_albums(
                    hidden_mod.SCOPE_MISSING, fingerprints))
    except OSError as e:
        return RedirectResponse(
            url="/library/hidden" + hidden_pages._hidden_query(q, str(e)), status_code=303)
    if not restored:
        return RedirectResponse(
            url="/library/hidden" + hidden_pages._hidden_query(q, "Nothing to bring back."),
            status_code=303)
    rejoined = await loop.run_in_executor(
        None, lambda: flows.refold_restored_missing(artists, fingerprints or []))
    # Under a filter only part of the set moved, so the message says how much
    # rather than claiming everything came back.
    what = ("Brought everything back" if fingerprints is None else
            f"Brought back {restored} "
            f"{'album' if restored == 1 else 'albums'}")
    if rejoined is False:
        msg = (f"{what}, but the open Library review couldn't "
               "be saved. Check the data folder, then refresh the review.")
    elif rejoined is None:
        lifted = await loop.run_in_executor(
            None, library_scan_state.clear_review_retired)
        msg = (f"{what} to the Library review." if lifted
               else f"{what}. They return the next time the library scans.")
    elif rejoined:
        msg = f"{what} to the Library review."
    else:
        msg = f"{what}. Nothing needs adding to the Library review."
    return RedirectResponse(
        url="/library/hidden" + hidden_pages._hidden_query(q, msg), status_code=303)


@router.post("/library/bring-back-all")
async def library_bring_back_all(request: Request):
    """Finished-state 'Bring all back': un-hide every dismissed missing/gap
    album and lift a retired review, so the whole set returns. The /library
    reload rebuilds the review from saved state (albums since downloaded are
    dropped as owned), which is why nothing needs folding here; the finished
    state is only reachable with no live review parked."""
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    loop = asyncio.get_running_loop()

    def _bring_back():
        restored = hidden_mod.restore_all(hidden_mod.SCOPE_MISSING)
        lifted = library_scan_state.clear_review_retired()
        return restored or lifted

    try:
        changed = await loop.run_in_executor(None, _bring_back)
    except OSError as e:
        return RedirectResponse(
            url="/library?notice=" + runtime._notice_key(str(e)),
            status_code=303)
    msg = ("Brought your dismissed results back to the Library review."
           if changed else "Nothing to bring back.")
    return RedirectResponse(
        url="/library?notice=" + runtime._notice_key(msg), status_code=303)


def _last_scan_age() -> str | None:
    """Human-readable age of the last library/artist scan, or None."""
    try:
        ts = float(cfg.LAST_SCAN_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return runtime._format_age(ts)
