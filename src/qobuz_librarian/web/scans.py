"""Finding, starting and deduplicating scan jobs."""
import asyncio
import time
from pathlib import Path

from fastapi.responses import RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import QobuzAccess
from qobuz_librarian.library import (
    collection_snapshot,
    missing_types,
    scan_checkpoint,
    scanner,
)
from qobuz_librarian.library import unreadable_artists as unreadable_artists_mod
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import (
    flows,
    job_persistence,
    job_runs,
    qobuz_access,
    refusals,
    rendering,
    review_badges,
    review_pages,
    runtime,
    storage,
    write_gate,
)
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
        if write_gate._web_writes_paused():
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
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        return busy
    separator = "&" if "?" in destination else "?"
    return RedirectResponse(
        url=destination + separator + "error=" + rendering._notice_key(
            job_mgr.JOB_ADMISSION_ERROR
        ),
        status_code=303,
    )


def _active_library_scan():
    """A library scan that's already pending/crawling, or None."""
    return _active_scan("library")


def _library_resume_offer(generation):
    """The interrupted Library scan's checkpoint to offer a resume from, or
    None. Offered while no Library scan is pending or crawling, when no
    generation has finished or the latest attempt is running, failed or
    incomplete."""
    if _active_library_scan() is not None:
        return None
    latest = (generation.get("latest_attempt") or {}).get("status")
    if (int(generation.get("generation") or 0)
            and latest not in {"running", "failed", "incomplete"}):
        return None
    return scan_checkpoint.pending()


def _music_root_problem():
    """Why the music folder cannot be read as a folder, or ""."""
    root = Path(cfg.MUSIC_ROOT)
    hint = storage._music_root_hint()
    try:
        if not root.exists():
            return f"{root} does not exist. {hint}"
        if not root.is_dir():
            return f"{root} is not a folder. {hint}"
    except OSError:
        return f"{root} could not be read. {hint}"
    return ""


# The last folder check, keyed on the music folder's own listing.
_last_folder_check = None
# How long a clean folder check stands in for a new one: an artist folder
# that becomes unreadable or loses its audio leaves the music folder's own
# listing as it was.
_CLEAN_CHECK_SECONDS = 180.0


def _library_scan_state():
    """Whether a whole-library scan has something valid to scan. While a
    Library scan reads every folder anyway, pages reuse the last check until
    an artist folder is added, removed or renamed. Otherwise a clean check is
    reused for a few minutes, until the app writes to the library, so a
    problem shows and a fixed one clears soon after."""
    global _last_folder_check
    try:
        st = Path(cfg.MUSIC_ROOT).stat()
        key = (str(cfg.MUSIC_ROOT), st.st_dev, st.st_ino, st.st_mtime_ns)
    except OSError:
        key = None
    now = time.monotonic()
    cleared = scanner.scan_caches_cleared()
    last = _last_folder_check
    if key is not None and last is not None and last[0] == key:
        _key, state, checked_at, cleared_then = last
        if _active_library_scan() is not None or (
                state["ready"] and not state["message"]
                and cleared_then == cleared
                and now - checked_at < _CLEAN_CHECK_SECONDS):
            return state
    state = _check_library_folders()
    _last_folder_check = (key, state, now, cleared)
    return state


def _check_library_folders():
    root = Path(cfg.MUSIC_ROOT)
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
                "message": storage._music_write_target_message(
                    write_state, recorded, diagnostic=True),
            }
        if storage._docker_made_folder(root):
            return {
                "ready": False,
                "empty": False,
                "count": 0,
                "message": "The music folder " + storage.DOCKER_MADE
                + ". Check QL_MUSIC_DIR in your .env.",
            }
        if cfg.in_container():
            elsewhere = ("If your music is already on this machine, map its "
                         f"folder onto {root} with QL_MUSIC_DIR in your .env.")
        else:
            elsewhere = ("If your music is elsewhere, set MUSIC_ROOT to the "
                         "folder that holds your artist folders.")
        return {
            "ready": False,
            "empty": True,
            "count": 0,
            "message": f"Download an album to start. {elsewhere}",
        }
    return {"ready": True, "empty": False, "count": len(artists),
            "message": ""}


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
        if write_gate._web_writes_paused():
            return None
        existing = _active_library_scan()
        if existing is not None:
            return existing
        title = "Gap Fill scan" if partial_only else "Library scan"
        job = job_mgr.Job(title=title)
        job.execute_kind = "library"

        def _scan(j):
            active = qobuz_access._authorize_qobuz_live(
                QobuzAccess.CATALOGUE_ACTION,
                expected_generation=credentials.generation,
            )
            flows.scan_library(j, active.token, partial_only=partial_only,
                               force_full=force_full)
            _fold_into_parked_library_review(j)

        return job_mgr.submit_scan(
            job,
            _scan,
            job_runs._resume_album_download(job, job.execute_args),
        )


def _restate_parked_status(parked, unchecked, status):
    """Library shows the open review's summary, so it says what the newest
    scan left unchecked or unsaved, not what an earlier one did."""
    summary = str(parked.summary or "")
    count = review_pages._COUNT_ONLY_SUMMARY.match(summary)
    if count is None:
        return
    restated = " ".join([summary[:count.end()].rstrip(".") + "."] + status)
    if restated == summary and parked.unchecked_artists == unchecked:
        return

    def _restate():
        parked.summary = restated
        flows._record_unchecked_artists(parked, unchecked)

    with job_mgr._library_review_state_guard(parked):
        job_persistence.persist_review_mutation(parked, _restate)


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
    added, updated, removed, held_back, fold_left_out, notes = folded
    # The review keeps these lines only while it waits; the refresh's own log
    # is stored when it finishes, so History still has them after a restart.
    for line in notes:
        job.push_line(line)
    held = sum(held_back.values())
    listed = added - held
    # Open review pages re-fetch on this nudge; without it the fold is
    # invisible until a manual reload (and "Refreshing…" never resolves).
    parked.notify_review_changed()
    if listed or ((updated or removed) and parked.candidates):
        # Fresh reviewable results landed in the parked review, so light the
        # Library dot again until the user opens it.
        review_badges.mark_ready("library")
    with job._lock:
        job.candidates = []
    bits = []
    under = missing_types.named(held_back) if held else ""
    if listed and held:
        bits.append(f"Added {plural(listed, 'new find')} to the open Library "
                    f"review, and {held} more under {under}.")
    elif listed:
        bits.append(f"Added {plural(listed, 'new find')} to the open "
                    "Library review.")
    elif held:
        bits.append(f"Added {plural(held, 'new find')} to the open Library "
                    f"review under {under}.")
    if not parked.candidates:
        bits.append("Nothing left to review.")
    elif updated or removed:
        changes = [f"{verb} {plural(n, 'album')}" for verb, n in (
            ("updated", updated), ("removed", removed)) if n]
        bits.append(f"{' and '.join(changes).capitalize()} in the open "
                    "Library review.")
    unchecked = job.unchecked_artists
    upgrades = getattr(job, "upgrade_finds", 0)
    caveats = list(getattr(job, "scan_caveats", ()))
    if not bits and not upgrades:
        if unchecked:
            bits.append("No new finds from the artists that could be checked.")
        elif caveats:
            bits.append("No new finds.")
        else:
            bits.append("No new finds. The open Library review is up to date.")
    if upgrades:
        bits.append(flows.upgrade_finds_note(upgrades))
    status = []
    if unchecked:
        status.append(f"{plural(unchecked, 'artist')} couldn't be checked; "
                      "scan again to resume from where it left off.")
    elif (left_out := unreadable_artists_mod.load()):
        status.append(f"{plural(len(left_out), 'artist folder')} couldn't be "
                      "read and "
                      f"{'was' if len(left_out) == 1 else 'were'} left out.")
    no_room = dict(job.scan_left_out)
    for folder, n in fold_left_out.items():
        no_room[folder] = no_room.get(folder, 0) + n
    if no_room:
        status.append(flows.left_out_note(no_room, parked.CANDIDATE_CAP))
    status.extend(caveats)
    bits.extend(status)
    _restate_parked_status(parked, unchecked, status)
    job.summary = " ".join(bits)
    job.push_line(job.summary)
    job.status = job_mgr.JobStatus.DONE
