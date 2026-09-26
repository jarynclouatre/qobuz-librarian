"""Starting the new-release check by hand and on its schedule."""
import time

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    NoCredsError,
    QobuzAccess,
    QobuzEntitlementError,
    QobuzUnavailable,
)
from qobuz_librarian.library import new_releases, scan_checkpoint
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import flows, job_runs, qobuz_access, runtime, write_gate
from qobuz_librarian.web import jobs as job_mgr


def _active_new_release_check():
    """A new-release check queued or crawling right now, or None, so a second
    one isn't stacked on top of it. A check whose list is merely parked for
    review does NOT block a fresh one: the fresh check folds its finds into
    that list (flows._append_to_parked_new_release_review), so asking again is
    always allowed and never costs the user the ticks already made."""
    for j in job_mgr.registry.pending_and_running():
        if j.execute_kind != "new_releases":
            continue
        if j.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return j
    return None


def _pending_new_release_review(job):
    """The new-release review still parked after a partial download, for the
    finished job page to point at. New releases have no home surface of their
    own, so a review split off the batch the user approved was reachable only
    from the dashboard notice or History: the page they were standing on gave
    them no way back to the rest of their own results."""
    if job.execute_kind != "new_releases":
        return None
    if job.status not in job_mgr.TERMINAL:
        return None
    other = None
    for j in job_mgr.registry.awaiting_review():
        if j.execute_kind == "new_releases" and j.id != job.id:
            other = j
            break
    if other is None:
        return None
    return {
        "href": f"/jobs/{other.id}",
        "label": f"{plural(len(other.candidates), 'new release')} still to review",
    }


def _start_new_release_check(credentials):
    """Submit a whole-library new-release check and return the job (or the one
    already queued). Shared by the manual Library-page option and the automatic
    dashboard trigger."""
    with runtime._auto_check_lock:
        # The run-lock may have been handed to the terminal mid-submit (this
        # can run in an executor for POST /library).
        if write_gate._web_writes_paused():
            return None
        existing = _active_new_release_check()
        if existing is not None:
            return existing
        job = job_mgr.Job(title="New releases")
        job.execute_kind = "new_releases"

        def _scan(j):
            active = qobuz_access._authorize_qobuz_live(
                QobuzAccess.CATALOGUE_ACTION,
                expected_generation=credentials.generation,
            )
            flows.scan_new_releases(j, active.token)

        return job_mgr.submit_scan(
            job,
            _scan,
            job_runs._resume_album_download(job, job.execute_args),
        )


# A failed live check holds off the next automatic one: five minutes, doubling
# per failure up to an hour. An outage then costs one check per window instead
# of one per dashboard load.
_AUTO_START_RETRY_FIRST = 300.0
_AUTO_START_RETRY_MAX = 3600.0
_auto_start_backoff = {"until": 0.0, "delay": 0.0}


def _auto_start_credentials():
    """The live Qobuz check before an automatic start, or None when it fails
    or a recent failure is still being backed off."""
    if time.time() < _auto_start_backoff["until"]:
        return None
    try:
        credentials = qobuz_access._authorize_qobuz_live(QobuzAccess.CATALOGUE_ACTION)
    except (
        NoCredsError,
        AuthLost,
        QobuzUnavailable,
        QobuzEntitlementError,
        CredentialChanged,
    ):
        delay = min(max(_auto_start_backoff["delay"] * 2,
                        _AUTO_START_RETRY_FIRST), _AUTO_START_RETRY_MAX)
        _auto_start_backoff.update(until=time.time() + delay, delay=delay)
        return None
    _auto_start_backoff.update(until=0.0, delay=0.0)
    return credentials


def _new_release_check_due():
    """Whether the automatic new-release check should run, from local state
    alone: nothing here touches the network."""
    if cfg.NEW_RELEASE_CHECK_INTERVAL <= 0 or write_gate._web_writes_paused():
        return False
    # Don't bother (or thrash) when there's no token, or one we already know
    # Qobuz is rejecting; it would just fail on the first call every load.
    if not qobuz_access._qobuz_ready():
        return False
    # Only after a full library scan has established the baseline; otherwise the
    # check would crawl every artist just to record a starting point and surface
    # nothing. A completed library scan seeds it (flows.scan_library).
    if not new_releases.is_baseline_complete():
        return False
    # And never ahead of an interrupted library scan waiting to resume: finishing
    # that takes priority (it's what the user's resume needs the scan lane for),
    # and a delta check can wait until the library is whole again.
    if scan_checkpoint.pending() is not None:
        return False
    if time.time() < _auto_start_backoff["until"]:
        return False
    # Avoid a network probe until the interval says a run is due. This first
    # read is repeated under the lock after the probe.
    return _new_release_interval_elapsed()


_new_release_submitted_at = 0.0


def _new_release_interval_elapsed():
    """A saved run time ahead of the clock counts as no run, and this
    process's own last submit counts even when the data folder could not
    take the stamp."""
    now = time.time()
    last = new_releases.last_run() or 0.0
    stamps = [t for t in (last, _new_release_submitted_at) if t <= now]
    return now - max(stamps, default=0.0) >= cfg.NEW_RELEASE_CHECK_INTERVAL


def _maybe_auto_check_new_releases():
    """Quietly run the new-release check on dashboard load when it's due.

    Read-only (it only parks a review list, never downloads), so it's safe to
    fire from a GET. Skipped when the check is off, the token is missing or
    known-bad, the CLI holds the lock, another job is actively working, or the
    interval hasn't elapsed. A list already parked for review does NOT stop it:
    the run folds its finds into that list, so the timer keeps the review
    current instead of going quiet until the list is cleared.
    """
    global _new_release_submitted_at
    if not _new_release_check_due():
        return
    credentials = _auto_start_credentials()
    if credentials is None:
        return
    # Serialise the check-and-submit so two concurrent dashboard loads can't
    # both pass the gate and queue the check twice.
    with runtime._auto_check_lock:
        active = job_mgr.registry.pending_and_running()
        working = any(j.status != job_mgr.JobStatus.AWAITING_REVIEW for j in active)
        if working:
            return
        if not _new_release_interval_elapsed():
            return
        # Stamp the attempt before submitting: the scan only advances the stamp
        # on a clean finish, so without this a failed/cancelled run would re-fire
        # on every load.
        _new_release_submitted_at = time.time()
        new_releases.touch_run()
        _start_new_release_check(credentials)
