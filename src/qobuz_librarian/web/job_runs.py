"""The work each job runs: album downloads, requeued Search downloads and approved reviews."""
import logging
from pathlib import Path

from qobuz_librarian import download_result
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.api import search as qobuz_search
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    QobuzAccess,
    QobuzUnavailable,
)
from qobuz_librarian.library import candidate_premise, catalog
from qobuz_librarian.modes import process as process_mode
from qobuz_librarian.queue import builder as queue_builder
from qobuz_librarian.queue import durable_album
from qobuz_librarian.queue import executor as queue_executor
from qobuz_librarian.web import (
    download_outcomes,
    flows,
    job_persistence,
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
    """Recheck the bound credential, then the local receipts, at worker start."""
    token = _review_download_token(job)
    # The live request can take long enough for a local edit to land, so the
    # files are checked after it. A Cancel during the check reaches execute,
    # which stops before its first pick and returns them all to the review.
    candidate_premise.validate_all(
        chosen, cancelled=lambda: job.cancel_requested)
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
        candidate_premise.validate_all(
            chosen, cancelled=lambda: j.cancel_requested)
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
            active = qobuz_access._authorize_download_start(
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
            separate = treat_as_new
            if not treat_as_new and catalog.is_lossless_album(album):
                existing, album_dir, missing, present = catalog.find_edition_tracks(album)
                other = catalog.other_version_dir(album)
                separate = bool(missing and other and album_dir is None)
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
                    exact_edition=True,
                )
                if not separate and durable_album.plan_durable_new_album(candidate, args) is not None:
                    durable_item = candidate
            saved_queue = durable_item is not None
            marker = job_persistence.SAVED_QUEUE
            if bool((j.execute_args or {}).get(marker)) != saved_queue:
                with j._lock:
                    marked = dict(j.execute_args or {})
                    marked.pop(marker, None)
                    j.execute_args = (
                        {**marked, marker: True} if saved_queue else marked)
                if job_persistence.persist(j) is not True:
                    raise RuntimeError(
                        "The download could not be saved to History, so it "
                        "was not started.")
            if durable_item is None:
                r = process_mode.process_album(album, args, allow_force=False,
                                  already_confirmed=True, token=active_token,
                                  treat_as_new=separate, exact_edition=True) or {}
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
                            queue_recovery._record_startup_recovery(
                                runtime._RUN_LOCK_HANDLE, settle=True)
                    except BaseException as refresh_exc:
                        _log.warning(
                            "couldn't refresh durable Web recovery after an "
                            "executor failure: %s",
                            refresh_exc,
                        )
                    queue_recovery.take_settled_downloads([j.id])
                    raise

                refresh_failed = False
                recovery = None
                try:
                    if runtime._run_lock_intact():
                        # A download this run left blocked is settled here, as
                        # start-up would, but never queued again.
                        recovery = queue_recovery._record_startup_recovery(
                            runtime._RUN_LOCK_HANDLE, settle=True)
                    else:
                        refresh_failed = True
                except Exception as exc:
                    refresh_failed = True
                    _log.warning(
                        "couldn't refresh durable Web recovery after the "
                        "executor returned: %s",
                        exc,
                    )
                settled = queue_recovery.take_settled_downloads([j.id]).get(j.id)
                recovery_status = getattr(
                    getattr(recovery, "status", None), "value", None)
                clear = recovery_status == "clear" and not refresh_failed
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
                    and clear
                )
                if accepted:
                    r = result
                    durable_completion_settled = True
                elif (
                    queue_recovery._durable_completion_status(j) is True
                    and clear
                    and runtime._run_lock_intact()
                    and queue_recovery._reconcile_acknowledged_job(j)
                ):
                    # Completion crossed its durable Web acknowledgement
                    # boundary even though the executor's return was not a
                    # normal drained result.
                    return
                elif clear and (settled is None or settled.outcome == "requeue"):
                    # Nothing is left waiting on recovery, so this is an
                    # ordinary failed, cancelled or undeliverable download.
                    r = result or {}
                else:
                    r = result or {}
                    durable_failure = True
                    j.status = job_mgr.JobStatus.FAILED
                    if clear:
                        j.attention = "processing"
                        j.error = settled.reason
                    else:
                        j.attention = "recovery"
                        j.error = (
                            "This download couldn't be cleared safely, so "
                            "downloads and scans are paused and its files were "
                            "left as they are. Qobuz Librarian tries again "
                            "when it restarts."
                        )
        status, attention = download_result.download_job_outcome(r)
        if durable_failure:
            pass
        elif attention:
            download_outcomes._mark_download_attention(j, r)
        elif status == "failed":
            j.status = job_mgr.JobStatus.FAILED
            j.error = download_outcomes.download_error(r)
        elif r.get("imported") and r.get("n_fail", 0) > 0:
            j.error = download_outcomes.download_error(r)
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
                claims_album=True,
            )
            # A parked library review may still offer this album, so drop it
            # there so the stale review can't download it a second time.
            flows.prune_library_review_candidates(album)
            retryable, _ = download_result.incomplete_track_counts(r)
            _, _, lossy_only, _ = download_result.track_shortfall(r)
            # Nothing missing but tracks Qobuz does not stream is what search
            # calls "Owned"; any other gap leaves the row offering the
            # download it still needs.
            j.landed_complete = not retryable and not lossy_only
            if retryable:
                flows._fold_partial_gap_fill(
                    album, (album.get("artist") or {}).get("name") or "",
                    retryable, album_dir=r.get("dir"))
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
        or set(args) - {"new_edition", "restart_requeues",
                        job_persistence.SAVED_QUEUE}
    ):
        return None
    album_id = str(job.album_id)
    track_id = str((job.single or {}).get("track_id") or "")
    if job.single and not track_id:
        return None
    treat_as_new = bool(args.get("new_edition"))

    def run(j):
        token = qobuz_access._authorize_download_start().token
        try:
            album = qobuz_search.get_album(album_id, token)
        except (AuthLost, QobuzUnavailable) as exc:
            raise job_mgr.WaitForQobuz() from exc
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
