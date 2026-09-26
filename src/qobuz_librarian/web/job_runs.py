"""The work each job runs: album downloads, requeued Search downloads and approved reviews."""
import logging
from pathlib import Path

from qobuz_librarian import download_result
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.api import search as qobuz_search
from qobuz_librarian.api.auth import CredentialChanged, QobuzAccess
from qobuz_librarian.library import candidate_premise, catalog
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.modes import process as process_mode
from qobuz_librarian.queue import builder as queue_builder
from qobuz_librarian.queue import durable_album
from qobuz_librarian.queue import executor as queue_executor
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import (
    download_outcomes,
    flows,
    qobuz_access,
    queue_recovery,
    runtime,
    storage,
    track_downloads,
)
from qobuz_librarian.web import jobs as job_mgr

_log = logging.getLogger("qobuz_librarian")


def _review_download_token(job):
    expected_generation = str(
        (job.execute_args or {}).get("_credential_generation") or ""
    )
    return qobuz_access._authorize_qobuz_live(
        QobuzAccess.DOWNLOAD_ACTION,
        expected_generation=expected_generation,
    ).token


def _run_review_with_live_download(job, chosen, execute):
    """Recheck both local receipts and the bound credential at worker start."""
    candidate_premise.validate_all(chosen)
    token = _review_download_token(job)
    # The live request above can take long enough for a local edit to land.
    # Repeat the exact receipt check before the flow reaches its first backup.
    candidate_premise.validate_all(chosen)
    return execute(job, chosen, token)


def _resume_album_download(job, _args):
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_albums)


def _resume_upgrade(job, _args):
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_upgrades)


def _resume_repair(job, _args):
    return lambda j, chosen: _run_review_with_live_download(
        j, chosen, flows.execute_repairs)


def _resume_migration(job, args):
    dest = args.get("dest", "")
    in_place = bool(args.get("in_place"))
    src = args.get("src")
    allow_low_space = bool(args.get("allow_low_space"))
    return lambda j, chosen: flows.execute_migration(
        j, chosen, dest, in_place=in_place,
        src=Path(src) if src else None, allow_low_space=allow_low_space)


def _job_downsample_keep_originals(job):
    value = (job.execute_args or {}).get("keep_originals")
    return value if type(value) is bool else None


def _resume_downsample(job, _args):
    def execute(j, chosen):
        candidate_premise.validate_all(chosen)
        return flows.execute_downsamples(
            j,
            chosen,
            token=None,
            keep_originals=_job_downsample_keep_originals(j),
        )

    return execute


# Names the persisted ``execute_kind`` strings so jobs survive a restart even
# though their original execute closure is gone.
_RESUME_EXECUTE: dict = {
    "library":      _resume_album_download,
    "new_releases": _resume_album_download,
    "collection_restore": _resume_album_download,
    "upgrade":      _resume_upgrade,
    "repair":       _resume_repair,
    "migration":    _resume_migration,
    "downsample":   _resume_downsample,
}


def _make_download_run(
    album,
    token,
    *,
    treat_as_new=False,
    durable_planned=None,
):
    """Return the run(j) callable used by both queue_download and job_retry.

    treat_as_new downloads the album as a brand-new one even if a different
    edition is already owned: the "get this edition too" path.
    """

    expected_generation = api_auth.token_credential_generation(token)

    def run(j):
        storage._require_music_write_target_for_job()
        active = None
        active_token = token
        if getattr(token, "credential_generation", ""):
            active = qobuz_access._authorize_qobuz_live(
                QobuzAccess.DOWNLOAD_ACTION,
                expected_generation=expected_generation,
            )
            active_token = active.token
        args = flows.build_args()
        flows._note_staging_wait(j, "Downloading", 0, 1)
        durable_failure = False
        durable_completion_settled = False
        with job_mgr.staging_lock():
            with qobuz_access._CREDENTIAL_LOCK:
                if (active is not None
                        and not qobuz_access._credential_generation_is_active(
                            active.generation)):
                    raise CredentialChanged(
                        "Qobuz credentials changed before the download began."
                    )
            durable_item = None
            if durable_planned is not None:
                if treat_as_new:
                    raise ValueError(
                        "a saved durable retry cannot change edition intent"
                    )
                durable_item = queue_state._deserialize_queue_item(
                    durable_planned
                )
                if durable_item["album"] != album:
                    raise ValueError(
                        "the saved durable retry album changed before execution"
                    )
            elif not treat_as_new and catalog.is_lossless_album(album):
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                existing, album_dir = catalog.find_existing_tracks(album)
                missing, present = catalog.compute_missing(qobuz_tracks, existing)
                candidate = queue_builder._build_queue_item(
                    album=album,
                    album_dir=album_dir,
                    label=(
                        f"{(album.get('artist') or {}).get('name') or '?'}"
                        f", {album.get('title') or '?'}"
                    ),
                    missing=missing,
                    present=present,
                    upgrade_only=False,
                    auto_upgrade=False,
                )
                if durable_album.plan_durable_new_album(candidate, args) is not None:
                    durable_item = candidate
            if durable_item is None:
                r = process_mode.process_album(album, args, allow_force=False,
                                  already_confirmed=True, token=active_token,
                                  treat_as_new=treat_as_new) or {}
            else:
                try:
                    results, drained = queue_executor._execute_download_queue(
                        [durable_item],
                        args,
                        active_token,
                        consolidate_duplicates=False,
                    )
                except BaseException:
                    # The durable executor can change the saved queue before
                    # raising.
                    try:
                        if runtime._run_lock_intact():
                            queue_recovery._record_startup_recovery(runtime._RUN_LOCK_HANDLE)
                    except BaseException as refresh_exc:
                        _log.warning(
                            "couldn't refresh durable Web recovery after an "
                            "executor failure: %s",
                            refresh_exc,
                        )
                    raise

                refresh_failed = False
                recovery = None
                try:
                    if runtime._run_lock_intact():
                        recovery = queue_recovery._record_startup_recovery(
                            runtime._RUN_LOCK_HANDLE)
                    else:
                        refresh_failed = True
                except Exception as exc:
                    refresh_failed = True
                    _log.warning(
                        "couldn't refresh durable Web recovery after the "
                        "executor returned: %s",
                        exc,
                    )
                recovery_status = getattr(
                    getattr(recovery, "status", None), "value", None)
                result = (
                    results[0]
                    if type(results) is list
                    and len(results) == 1
                    and type(results[0]) is dict
                    else None
                )
                accepted = (
                    drained is True
                    and result is not None
                    and result.get("imported") is True
                    and recovery_status == "clear"
                    and not refresh_failed
                )
                # A cancel and an undeliverable album both end with the partial
                # download discarded and nothing left waiting on recovery, so
                # neither is a durable failure.
                cancelled_clean = (
                    result is not None
                    and result.get("result") in ("cancelled", "incomplete")
                    and recovery_status == "clear"
                    and not refresh_failed
                )
                if accepted or cancelled_clean:
                    r = result
                    durable_completion_settled = accepted
                else:
                    r = result or {}
                    durable_failure = True
                    completion_acknowledged = queue_recovery._durable_completion_status(j)
                    # `recovery_status` is process-wide, so on its own it fails
                    # a download whose own completion is acknowledged because
                    # some other item's recovery is outstanding. Whose recovery
                    # it is decides; the completion proof is only read.
                    recovery_is_this_job = queue_recovery._startup_recovery_web_job_id() == j.id
                    if (
                        completion_acknowledged is True
                        and (recovery_status == "clear"
                             or not recovery_is_this_job)
                        and runtime._run_lock_intact()
                        and queue_recovery._reconcile_acknowledged_job(j)
                    ):
                        # Completion crossed its durable Web acknowledgement
                        # boundary even though the executor's return was not a
                        # normal drained result.
                        return
                    retryable = (
                        completion_acknowledged is False
                        and result is not None
                        and result.get("result") == "retry"
                        and recovery_status == "resume_required"
                        and queue_recovery._durable_recovery_matches_job(j)
                    )
                    # History and the job page render Retry for whichever job
                    # HOLDS the durable recovery control, which is wider than
                    # `retryable`: an attention stop holds it too. Choosing the
                    # copy on the narrower test printed "cleared under Settings
                    # > Diagnostics" directly beside a working Retry button, and
                    # Diagnostics has no control for this; it only checks
                    # volumes, binaries and upgrade backups.
                    control = queue_recovery._durable_recovery_control()
                    holds_control = bool(control and control["job_id"] == j.id)
                    j.status = job_mgr.JobStatus.FAILED
                    if retryable:
                        j.attention = ""
                        j.error = (
                            "This download stopped before anything was "
                            "imported, so downloads and scans are paused. Use "
                            "Retry to download it again, or Give up on this "
                            "album to drop it and carry on."
                        )
                    elif holds_control:
                        j.attention = "recovery"
                        j.error = (
                            "This download couldn't be confirmed as finished "
                            "cleanly, so downloads are paused. Use Retry on "
                            "this job to settle it, or Give up on this album "
                            "to discard it and carry on."
                        )
                    else:
                        # The recovery is held by a different job, so no Retry
                        # is rendered here; send them to the one that has it.
                        j.attention = "recovery"
                        j.error = (
                            "This download couldn't be confirmed as finished "
                            "cleanly, so downloads are paused. Open the "
                            "download holding the recovery from Queue or "
                            "History and use Retry to settle it."
                        )
        status, attention = download_result.download_job_outcome(r)
        if durable_failure:
            pass
        elif attention:
            download_outcomes._mark_download_attention(j, r)
        elif status == "failed":
            j.status = job_mgr.JobStatus.FAILED
            retryable, lossy_only = download_result.incomplete_track_counts(r)
            if r.get("rate_limited"):
                j.error = "Qobuz rate-limited this download. Try again later."
            elif r.get("result") == "incomplete":
                j.error = download_outcomes._undeliverable_album_error(r, album)
            elif r.get("n_fail"):
                j.error = f"{plural(r['n_fail'], 'track')} failed. See job log."
            elif r.get("n_ok"):
                j.error = "Downloaded, but the import failed. See job log."
            elif retryable:
                j.error = (
                    f"{plural(retryable, 'track')} did not arrive as a "
                    "complete file, so nothing was added to your library. "
                    "The job log names them.")
            elif lossy_only:
                j.error = (
                    f"Qobuz offered {plural(lossy_only, 'track')} only in a "
                    "lossy format, so nothing was added to your library.")
            elif r.get("result") in download_outcomes._DOWNLOAD_SUMMARY_LABELS:
                j.error = download_outcomes._DOWNLOAD_SUMMARY_LABELS[r["result"]]
            else:
                j.error = "No tracks were retrieved. The job log says why."
        elif r.get("imported") and r.get("n_fail", 0) > 0:
            j.error = f"{plural(r['n_fail'], 'track')} failed. See job log."
        # Surface a one-line outcome here so the /jobs page tells the user what
        # happened without expanding the log.
        summary = download_outcomes._summarize_download_result(r)
        if summary:
            j.summary = summary
        # Claiming/completing the album the normal way graduates it out of the
        # "downloaded single" state, so the rest stops being suppressed in scans.
        if r.get("imported"):
            flows._refresh_after_local_album_change(
                album,
                r,
                fallback_artist=(album.get("artist") or {}).get("name"),
                token=active_token,
                args=args,
                upgrade=True,
                downsample=True,
            )
            # A parked library review may still offer this album, so drop it
            # there so the stale review can't download it a second time.
            flows.prune_library_review_candidates(album)
            retryable, lossy_only = download_result.incomplete_track_counts(r)
            # Nothing missing at all is what search calls "Owned"; a gap of
            # either kind leaves the row offering the download it still needs.
            j.landed_complete = not retryable and not lossy_only
            if retryable:
                flows._fold_partial_gap_fill(
                    album, (album.get("artist") or {}).get("name") or "",
                    retryable)
            hidden_mod.unmark_single(
                (album.get("artist") or {}).get("name") or "?",
                album.get("title") or "?",
                album_id=album.get("id"),
            )
        if durable_completion_settled:
            # Exact completion, external acknowledgement, carrier retirement,
            # and journal cleanup all finished before the executor returned.
            # Close the Web state under the same lock as request_cancel so a
            # late click cannot relabel the completed library mutation.
            with j._lock:
                if j.status is job_mgr.JobStatus.RUNNING:
                    j.cancel_requested = False
                    j.status = job_mgr.JobStatus.DONE
    return run


def _requeued_download_run(job):
    """The run for a Search download still queued when the app stopped, or
    None for any other job.

    Its album and token lived only in memory, so both are fetched again when
    its turn comes.
    """
    args = job.execute_args or {}
    if (
        not job.album_id
        or job.kind != "download"
        or job.execute_kind
        or job.recoveries
        or job.execute_args_unreadable
        or set(args) - {"new_edition"}
    ):
        return None
    album_id = str(job.album_id)
    track_id = str((job.single or {}).get("track_id") or "")
    if job.single and not track_id:
        return None
    treat_as_new = bool(args.get("new_edition"))

    def run(j):
        token = qobuz_access._authorize_qobuz_live(QobuzAccess.DOWNLOAD_ACTION).token
        album = qobuz_search.get_album(album_id, token)
        if not track_id:
            return _make_download_run(album, token, treat_as_new=treat_as_new)(j)
        track = next(
            (t for t in (album.get("tracks") or {}).get("items") or []
             if str(t.get("id")) == track_id),
            None,
        )
        if track is None:
            raise RuntimeError("That track isn't on this album any more.")
        return track_downloads._make_single_track_run(album, track, token)(j)

    return run
