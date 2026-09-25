"""Finding, starting and deduplicating scan jobs."""
import asyncio
import copy
from pathlib import Path

from fastapi.responses import RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import QobuzAccess
from qobuz_librarian.library import collection_snapshot, generation_state, scanner
from qobuz_librarian.library import unreadable_artists as unreadable_artists_mod
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import flows, review_badges, runtime
from qobuz_librarian.web import jobs as job_mgr

_ANY_TARGET = object()


def _scan_target(job) -> str:
    """The artist a scan covers, or "" for the whole library."""
    return (job.artist or "").strip().casefold()


def _active_scan(*kinds, statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING),
                 target=_ANY_TARGET):
    """A job of one of the given execute_kinds in one of ``statuses``, or None,
    folding a double-submitted pass onto the one already in flight instead of
    stacking duplicate work."""
    for j in job_mgr.registry.pending_and_running():
        if j.execute_kind in kinds and j.status in statuses:
            if target is _ANY_TARGET or _scan_target(j) == target:
                return j
    return None


def _last_finished(kind, statuses=job_mgr.TERMINAL):
    """The most recently stopped job of one execute_kind, or None."""
    latest = None
    for j in job_mgr.registry.all():
        if j.execute_kind != kind or j.status not in statuses:
            continue
        if (latest is None
                or (j.finished_at or j.created_at or 0)
                >= (latest.finished_at or latest.created_at or 0)):
            latest = j
    return latest


async def _submit_scan_deduped_async(job, scan_fn, execute_fn, *kinds, **kw):
    """Run _submit_scan_deduped off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _submit_scan_deduped(job, scan_fn, execute_fn, *kinds, **kw))


def _submit_scan_deduped(job, scan_fn, execute_fn, *kinds,
                         statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING)):
    """Submit a scan only if one of ``kinds`` isn't already active, atomically.

    Checking _active_scan and submitting in one locked step closes the window
    where two near-simultaneous POSTs (a double-click, or the auto-trigger
    landing with a manual click) both pass the check and stack duplicate scans.
    Returns the job to redirect to: the new one, or the in-flight duplicate,
    or None when web writes were paused between the route's opening gate and
    here (a set_mode CLI handoff landing mid-request; see job_approve)."""
    with runtime._auto_check_lock:
        if runtime._web_writes_paused():
            return None
        target = _scan_target(job)
        existing = _active_scan(*kinds, statuses=statuses, target=target)
        if existing is not None:
            return existing
        # A re-scan supersedes the same artist's stale parked review (or the
        # whole-library pass's) instead of stacking a second one: the fresh
        # scan re-derives that target's candidates, so the old awaiting-review
        # result is obsolete, and parked reviews never self-clear so without
        # this they pile up forever.
        stale_reviews = [
            old for old in job_mgr.registry.awaiting_review()
            if (old.execute_kind in kinds
                and _scan_target(old) == target)
        ]
        submitted = job_mgr.submit_scan(job, scan_fn, execute_fn)
        if submitted is None:
            return None
        # Only discard the prior review after the replacement has a durable
        # owner row.
        for old in stale_reviews:
            job_mgr.cancel_review(old)
        return submitted


def _scan_submission_failure_response(request, destination):
    """Explain a refused durable admission unless the CLI owns the lock."""
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    separator = "&" if "?" in destination else "?"
    return RedirectResponse(
        url=destination + separator + "error=" + runtime._notice_key(
            job_mgr.JOB_ADMISSION_ERROR
        ),
        status_code=303,
    )


def _active_library_scan():
    """A library scan that's already pending/crawling, or None."""
    return _active_scan("library")


def _music_root_problem():
    """Why the music folder cannot be read as a folder, or ""."""
    root = Path(cfg.MUSIC_ROOT)
    hint = runtime._music_root_hint()
    try:
        if not root.exists():
            return f"{root} does not exist. {hint}"
        if not root.is_dir():
            return f"{root} is not a folder. {hint}"
    except OSError:
        return f"{root} could not be read. {hint}"
    return ""


def _library_scan_state():
    """Whether a whole-library scan has something valid to scan."""
    root = Path(cfg.MUSIC_ROOT)
    hint = runtime._music_root_hint()
    problem = _music_root_problem()
    if problem:
        return {
            "ready": False,
            "empty": False,
            "count": 0,
            "message": problem,
        }
    unreadable = []
    artists = scanner.list_library_artists(
        on_artist_error=lambda path, error: unreadable.append(path.name))
    if unreadable:
        return {
            "ready": bool(artists), "empty": False, "count": len(artists),
            "message": "Unreadable artist folders: " + ", ".join(unreadable)
            + ". Check folder permissions and retry.",
        }
    if not artists:
        # An empty top level is a fresh install until a collection backup says
        # the root once held albums, which usually means a dropped mount. The
        # same signal the download path explains in its own words.
        write_state, recorded = collection_snapshot.music_root_write_state()
        if write_state != "ready":
            return {
                "ready": False,
                "empty": False,
                "count": 0,
                "message": runtime._music_write_target_message(
                    write_state, recorded, diagnostic=True),
            }
        return {
            "ready": False,
            "empty": True,
            "count": 0,
            "message": (
                f"No artist folders with audio were found in {root}. {hint}"
            ),
        }
    return {"ready": True, "empty": False, "count": len(artists),
            "message": ""}


def _truthful_library_generation():
    """Show an interrupted review save before startup recovery can run."""
    state = generation_state.load()
    if not generation_state.library_publication_incomplete(state):
        return state
    state = copy.deepcopy(state)
    latest = state.setdefault("latest_attempt", {})
    latest["status"] = "incomplete"
    latest["message"] = (
        "The Library scan finished checking the catalogue, but stopped "
        "before its review was saved."
    )
    return state


def _start_library_scan(credentials, partial_only=False, force_full=False):
    """Submit a library scan and return the job. Shared by the Library page and
    the automatic first-run/resume trigger. scan_library resumes from a matching
    checkpoint on its own, so this is the same call whether starting or resuming.

    Deduped under the lock: if a library scan is already crawling, return it
    instead of stacking a second one (the manual button and the auto trigger can
    both land here at once)."""
    with runtime._auto_check_lock:
        # Re-check the pause predicate under the lock (see
        # _start_new_release_check).
        if runtime._web_writes_paused():
            return None
        existing = _active_library_scan()
        if existing is not None:
            return existing
        title = "Gap Fill scan" if partial_only else "Library scan"
        job = job_mgr.Job(title=title)
        job.execute_kind = "library"

        def _scan(j):
            active = runtime._authorize_qobuz_live(
                QobuzAccess.CATALOGUE_ACTION,
                expected_generation=credentials.generation,
            )
            flows.scan_library(j, active.token, partial_only=partial_only,
                               force_full=force_full)
            _fold_into_parked_library_review(j)

        return job_mgr.submit_scan(
            job,
            _scan,
            runtime._resume_album_download(job, job.execute_args),
        )


def _fold_into_parked_library_review(job):
    """A refresh that finishes while a Library review is parked folds its
    finds into it; the scan job then finishes with a summary."""
    if (job.status not in (job_mgr.JobStatus.SCANNING, job_mgr.JobStatus.RUNNING)
            or job.cancel_requested):
        return
    parked = None
    for other in job_mgr.registry.awaiting_review():
        if (other.execute_kind != "library"
                or other.id == job.id):
            continue
        if parked is None or (other.created_at or 0) > (parked.created_at or 0):
            parked = other
    if parked is None:
        return
    with job._lock:
        cands = list(job.candidates)
    folded = flows.fold_new_candidates(
        parked,
        cands,
        review_generation=(job.execute_args or {}).get(
            "_library_review_generation"
        ),
        coverage=job.scan_coverage,
    )
    if folded is False:
        job.status = job_mgr.JobStatus.FAILED
        job.error = (
            "The refreshed Library review couldn't be saved to the data "
            "folder. Its existing picks are untouched. Check the data "
            "volume, then refresh again."
        )
        job.summary = "Library refresh stopped because its results could not be saved."
        job.push_line(job.error)
        return
    if folded is None:
        # The review was approved or discarded while the refresh ran, so leave
        # the scan's candidates alone so they park as their own review.
        return
    # Use the counts from the locked mutation itself. Complete ownership is
    # reconciled at approval, when exact edition tracks can be compared safely.
    added, updated = folded
    # Open review pages re-fetch on this nudge; without it the fold is
    # invisible until a manual reload (and "Refreshing…" never resolves).
    parked.notify_review_changed()
    if added or (updated and parked.candidates):
        # Fresh reviewable results landed in the parked review, so light the
        # Library dot again until the user opens it.
        review_badges.mark_ready("library")
    with job._lock:
        job.candidates = []
    bits = []
    if added:
        bits.append(f"Added {plural(added, 'new find')} to the open "
                    "Library review.")
    if updated and not parked.candidates:
        bits.append("Nothing left to review.")
    elif updated:
        bits.append(
            f"Updated {plural(updated, 'changed item')} in the open Library "
            "review."
        )
    unchecked = job.unchecked_artists
    if not bits:
        if unchecked:
            bits.append("No new finds from the artists that could be checked.")
        else:
            bits.append("No new finds. The open Library review is up to date.")
    if unchecked:
        bits.append(f"{plural(unchecked, 'artist')} couldn't be checked; "
                    "scan again to resume from where it left off.")
    elif (left_out := unreadable_artists_mod.load()):
        bits.append(f"{plural(len(left_out), 'artist folder')} couldn't be "
                    "read and "
                    f"{'was' if len(left_out) == 1 else 'were'} left out.")
    if job.candidate_cap_hit or parked.candidate_cap_hit:
        bits.append("The scan hit the result cap, so some finds are not "
                    "listed.")
    job.summary = " ".join(bits)
    job.push_line(job.summary)
    job.status = job_mgr.JobStatus.DONE
