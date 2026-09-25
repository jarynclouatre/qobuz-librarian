"""Routes for job pages and their reviews."""
import asyncio
import copy
import html
import json
import logging
import os
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from qobuz_librarian import completion
from qobuz_librarian import config as cfg
from qobuz_librarian.api import search as qobuz_search
from qobuz_librarian.api.auth import CredentialChanged, NoCredsError, QobuzAccess
from qobuz_librarian.integrations import beets as beets_mod
from qobuz_librarian.integrations import downsample_engine
from qobuz_librarian.library import candidate_premise, generation_state, new_releases
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.queue.startup_recovery import BlockedItemSettlementAction
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import (
    flows,
    job_persistence,
    owned_paths,
    review_badges,
    review_pages,
    runtime,
    saved_reviews,
    settings_store,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


# Library follows the same single-surface rule as Repair: the scan, its live
# progress, and the parked Missing Albums / Gap Fill review all live on
# /library, never handed off to /jobs/{id} under the Queue nav.
_LIBRARY_SURFACE_KINDS = ("library", "new_releases")
_QOBUZ_REVIEW_KINDS = ("library", "new_releases", "upgrade", "repair",
                       "collection_restore")
_PREMISE_REVIEW_KINDS = _QOBUZ_REVIEW_KINDS + ("downsample",)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str,
                   stale: bool = False, noselection: bool = False, page: int = 1,
                   error: str = "", q: str = "", tab: str = "",
                   waiting: bool = False):
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return RedirectResponse(
                url="/queue?error=" + runtime._notice_key(
                    "That job is no longer in History."),
                status_code=303)
    if job.execute_kind == "library" and job.status not in job_mgr.TERMINAL:
        # /library is the single Library review surface (launcher, live scan,
        # and the parked review all render there), so a library-kind job gets
        # its own page only once it has finished, for its outcome and log.
        params = {}
        if tab:
            params["tab"] = tab
        if page and page != 1:
            params["page"] = str(page)
        if q:
            params["q"] = q
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        return RedirectResponse(url=f"/library{query}", status_code=303)
    review_badge_ack = _review_badge_ack_for(job)
    nav_page, _return_href, _return_label = runtime._job_nav_destination(job)
    shown_attention = job.attention
    if job.attention and job.attention not in ("recovery", "catalog"):
        # Opening the page is the acknowledgement: the History chip and the
        # nav's warning dot stand down once the user has seen the job.
        attention = job.attention
        loop = asyncio.get_running_loop()
        acknowledged = await loop.run_in_executor(
            None,
            lambda: job_persistence.acknowledge_attention(job.id, attention),
        )
        if acknowledged:
            with job._lock:
                if job.attention == attention:
                    job.attention = ""
    if job.attention == "recovery" and job.recoveries:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: job_persistence.acknowledge_missing_recoveries(
                job, runtime._recovery_missing),
        )
    new_release_state = {"stale": False, "reason": ""}
    if (
        job.execute_kind == "new_releases"
        and job.status == job_mgr.JobStatus.AWAITING_REVIEW
    ):

        output = generation_state.output_state("new_releases")
        current = new_releases.is_baseline_complete()
        new_release_state = {
            "stale": not current,
            "reason": str(output.get("reason") or ""),
        }
    ctx = {"job": job, "page": nav_page, "shown_attention": shown_attention,
           "stale": stale, "noselection": noselection,
           "waiting": waiting,
           "error": runtime._notice_text(error),
           "new_release_state": new_release_state,
           "queue_wait": runtime._queue_wait(job),
           "JobStatus": job_mgr.JobStatus}
    ctx.update(review_pages._review_context(job, page, q, tab))
    return runtime._tr(
        request, "job.html", ctx, review_badge_ack=review_badge_ack
    )


def _review_badge_ack_for(job):
    if (job.execute_kind not in review_badges.SURFACES
            or job.status != job_mgr.JobStatus.AWAITING_REVIEW
            or not job.selection_counts()["total"]):
        return None
    return (
        job.execute_kind,
        review_badges.ready_generation(job.execute_kind),
    )


@router.get("/jobs/{job_id}/content", response_class=HTMLResponse)
async def job_content(request: Request, job_id: str, page: int = 1,
                      embedded: bool = False):
    """The job page's state-specific body, on its own. The live page swaps
    this in when the SSE stream reports the job finished, so the terminal
    view has one render path, the server's, instead of a faked-up bar.
    ``embedded`` mirrors the embedding surface's flag (Library/Repair render
    the body under their own page heading), so the swapped-in body doesn't
    reintroduce the job header the full-page render suppressed."""
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
    review_badge_ack = _review_badge_ack_for(job)
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus,
           "embedded_surface": embedded,
           "queue_wait": runtime._queue_wait(job)}
    ctx.update(review_pages._review_context(job, page))
    return runtime._tr(
        request, "_job_body.html", ctx, review_badge_ack=review_badge_ack
    )


@router.get("/jobs/{job_id}/review", response_class=HTMLResponse)
async def job_review_page(request: Request, job_id: str, page: int = 1,
                          q: str = "", tab: str = ""):
    """One page of the paginated review list (groups + pager + summary), for
    Prev/Next, the whole-set artist filter, and a library review's tab switch.
    Rendered from saved selection flags, so ticks persist and span pages."""
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
    review_badge_ack = _review_badge_ack_for(job)
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus}
    ctx.update(review_pages._review_context(job, page, q, tab))
    return runtime._tr(
        request, "_review_page.html", ctx,
        review_badge_ack=review_badge_ack,
    )


def _build_unapproved_review(job, tab, *, admission_filter=None,
                             discard_ids=()):
    """Before approving a Qobuz album review: move every candidate
    that ISN'T being downloaded right now into its own parked review, so a
    partial download consumes ONLY the ticked picks. Everything else stays in
    the living review:
    the unticked candidates, plus (on a tab-scoped approve) the whole tab the
    user isn't looking at, ticks and all. A final admission filter may also
    keep a selected candidate parked when another job claimed it just before
    approval. ``discard_ids`` removes candidates already proven complete on
    disk as part of the same durable transition. The caller holds ``job._lock``
    and durably admits both jobs before publishing either transition. Returns
    the new unpublished parked job, or None when every candidate is being
    used."""
    tab_scoped = tab in ("missing", "gaps")
    gap_active = tab == "gaps"
    keep, split = [], []
    discard_ids = set(discard_ids)
    for c in job.candidates:
        if c.get("cid") in discard_ids:
            continue
        in_scope = (not tab_scoped) or (flows.is_gap_candidate(c) == gap_active)
        selected = in_scope and c.get("selected")
        admitted = selected and (
            admission_filter is None or admission_filter(c)
        )
        (keep if admitted else split).append(c)
    if not split:
        return None
    job.candidates = keep
    other = job_mgr.Job(title=job.title, kind=job.kind,
                        execute_kind=job.execute_kind,
                        execute_args=dict(job.execute_args or {}),
                        review_verb=job.review_verb,
                        status=job_mgr.JobStatus.AWAITING_REVIEW)
    other.candidates = split  # cids, seqs, and saved ticks ride along
    other.summary = flows.split_review_summary(other.execute_kind, split)
    other.sync_cand_seq()
    factory = runtime._RESUME_EXECUTE.get(other.execute_kind)
    if factory is not None:
        other._execute_fn = factory(other, other.execute_args)
    return other


@router.post("/jobs/{job_id}/approve")
async def job_approve(request: Request, job_id: str):
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(
            url="/queue?error=" + runtime._notice_key(
                "That review is no longer open."),
            status_code=303)
    # A parked review can outlive its feature: credentials can be pulled after
    # an upgrade review parks, and the downsample engine can vanish across a
    # restart.
    if job.execute_kind == "upgrade" and not runtime._upgrade_available():
        return saved_reviews._upgrade_unavailable_response()
    if job.execute_kind == "downsample":
        if not downsample_engine.HAVE_DOWNSAMPLE:
            return RedirectResponse(url="/downsample", status_code=303)
    # Repair and Library stay on their single surfaces through the executing
    # phase; every other kind (new-release checks included) keeps using the
    # job page.
    if job.execute_kind == "repair":
        dest = "/repair"
    elif job.execute_kind == "library":
        dest = "/library"
    else:
        dest = f"/jobs/{job_id}"
    if (job.status == job_mgr.JobStatus.AWAITING_REVIEW
            and job.execute_kind == "upgrade"
            and (job.execute_args or {}).get("quality_signature")
            != saved_reviews._effective_upgrade_quality_signature()):
        return RedirectResponse(
            url=f"/jobs/{job.id}?error=" + runtime._notice_key(
                "Download quality changed since this Upgrade review was "
                "built. Run a Library refresh before approving it."
            ),
            status_code=303,
        )
    # A library review approves per tab: the button acts on the tab the user
    # is looking at, and only that tab.
    form = await request.form()
    migration_low_space_required = bool(
        job.execute_kind == "migration"
        and (job.execute_args or {}).get("requires_low_space_override")
    )
    migration_low_space_accepted = form.get("allow_low_space") == "on"
    if (
        job.status == job_mgr.JobStatus.AWAITING_REVIEW
        and migration_low_space_required
        and not migration_low_space_accepted
    ):
        action = ("in-place move" if (job.execute_args or {}).get("in_place")
                  else "copy")
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(
                f"Confirm the low-space risk before approving this {action}. "
                "Your review is untouched."
            ),
            status_code=303,
        )
    tab = (form.get("tab") or "").strip()
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    loop = asyncio.get_running_loop()
    selected_candidate_ids = set()
    selected_candidate_snapshot = []
    stale_premise_candidate_ids = set()
    # Ticked albums a download already running covers, left ticked.
    already_downloading = []
    downsample_keep_originals = None
    downsample_choice_to_save = ""
    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
        if tab:
            gap_active = tab == "gaps"
            with job._lock:
                selected_candidate_ids = {
                    c.get("cid") for c in job.candidates
                    if (c.get("selected")
                        and flows.is_gap_candidate(c) == gap_active)
                }
        else:
            with job._lock:
                selected_candidate_ids = {
                    c.get("cid") for c in job.candidates if c.get("selected")
                }
        with job._lock:
            selected_candidate_snapshot = copy.deepcopy([
                c for c in job.candidates
                if c.get("cid") in selected_candidate_ids
            ])
        has_pick = bool(selected_candidate_ids)
        if not has_pick:
            return RedirectResponse(url=f"{dest}?noselection=1",
                                    status_code=303)
        if job.execute_kind in _PREMISE_REVIEW_KINDS:
            stale_premise_candidate_ids = await loop.run_in_executor(
                None,
                # Admission only: a fresh unshared check runs again before
                # anything is written, so one seal per artist is enough here.
                lambda: candidate_premise.stale_candidate_ids(
                    selected_candidate_snapshot, share_artist_captures=True),
            )
            if selected_candidate_ids <= stale_premise_candidate_ids:
                stale_message = await loop.run_in_executor(
                    None, lambda: _all_stale_message_for(
                        job, stale_premise_candidate_ids.counts))
                return RedirectResponse(
                    url=dest + "?error=" + runtime._notice_key(stale_message),
                    status_code=303,
                )
        # Only now that the run is going to happen: the keep-vs-delete answer is
        # a standing policy saved to Settings, so asking and saving it before
        # anything checks that an album is ticked lets a no-op approve change
        # what every future downsample does with your originals.
        if job.execute_kind == "downsample":
            # current() includes a saved value waiting for another lane to
            # finish. Bind that value to this approval below.
            choice = runtime._downsample_originals_choice()
            rendered_choice = (
                form.get("downsample_policy") or ""
            ).strip().lower()
            current_choice = choice if choice in ("keep", "delete") else ""
            if (
                rendered_choice not in ("", "keep", "delete")
                or rendered_choice != current_choice
            ):
                return RedirectResponse(
                    url=dest + "?error=" + runtime._notice_key(
                        "The keep-or-delete setting changed after this review "
                        "was shown. No music files were changed; review the "
                        "updated warning and approve again."
                    ),
                    status_code=303,
                )
            if choice not in ("keep", "delete"):
                choice = (form.get("keep_choice") or "").strip().lower()
                if choice in ("keep", "delete"):
                    downsample_choice_to_save = choice
                else:
                    return runtime._tr(request, "downsample_keep_choice.html", {
                        "job": job, "page": "downsample",
                        "picked": len(selected_candidate_snapshot),
                        "picked_albums":
                            _named_albums(selected_candidate_snapshot),
                        "downsample_originals_choice": current_choice,
                        "backup_retention_days":
                            cfg.UPGRADE_BACKUP_RETENTION_DAYS})
            downsample_keep_originals = choice == "keep"

    authorized_credentials = None
    stale_owned_candidate_ids = set()
    if (job.status == job_mgr.JobStatus.AWAITING_REVIEW
            and job.execute_kind in _QOBUZ_REVIEW_KINDS):
        try:
            authorized_credentials = await runtime._authorize_qobuz_for_web(
                QobuzAccess.DOWNLOAD_ACTION
            )
        except runtime._QOBUZ_ACTION_ERRORS as exc:
            message = job_mgr.qobuz_action_error_message(exc, unchanged=True)
            return RedirectResponse(
                url=dest + "?error=" + runtime._notice_key(message),
                status_code=303,
            )
        if job.execute_kind in _LIBRARY_SURFACE_KINDS:
            stale_owned_candidate_ids = await loop.run_in_executor(
                None,
                lambda: flows.owned_missing_candidate_ids(
                    job,
                    authorized_credentials.token,
                    candidate_ids=(
                        selected_candidate_ids
                        - stale_premise_candidate_ids
                    ),
                ),
            )
    skipped = len(stale_owned_candidate_ids)
    _skip_q = f"&skipped={skipped}" if skipped else ""
    # Selection is saved server-side as the user ticks (the paginated review
    # no longer carries every checkbox in the form), so approve runs against
    # the saved flags; passing None keeps them as-is rather than reading the
    # form.
    def _split_and_approve():
        nonlocal stale_premise_candidate_ids

        # Atomic recheck right before anything is consumed: the route's
        # opening gate ran before several awaits (form parsing, disk probes),
        # and set_mode('cli') can hand the run lock to the terminal inside
        # that window, this not-yet-approved review is invisible to its
        # active-job check, so approving after the handoff would start
        # destructive work with the single-writer guard off.
        with (
            saved_reviews._SAVED_REVIEW_LOCK,
            runtime._auto_check_lock,
            runtime._DOWNLOAD_SUBMIT_LOCK,
            runtime._CREDENTIAL_LOCK,
            job._review_action_lock,
        ):
            if runtime._web_writes_paused():
                return "paused"
            if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
                return False

            def current_selected_candidates():
                with job._lock:
                    candidates = [
                        c for c in job.candidates if c.get("selected")
                    ]
                    if tab:
                        gap_active = tab == "gaps"
                        candidates = [
                            c for c in candidates
                            if flows.is_gap_candidate(c) == gap_active
                        ]
                    return copy.deepcopy(candidates)

            current_selected = current_selected_candidates()
            if {
                c.get("cid") for c in current_selected
            } != selected_candidate_ids:
                return "review_changed"
            if job.execute_kind in _PREMISE_REVIEW_KINDS:
                # Still admission: the worker's unshared check runs before
                # anything is written, so one seal per artist is enough here.
                stale_premise_candidate_ids = candidate_premise.stale_candidate_ids(
                    current_selected, share_artist_captures=True)
                if {
                    c.get("cid") for c in current_selected
                } <= stale_premise_candidate_ids:
                    return "all_candidates_stale"
            if (authorized_credentials is not None
                    and not runtime._credential_generation_is_active(
                        authorized_credentials.generation)):
                return "credential_changed"
            if (job.execute_kind in ("upgrade", "downsample")
                    and job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                synced_job = saved_reviews._sync_saved_review_before_approve(job)
                if synced_job is not job:
                    return "review_changed"
                current_selected = current_selected_candidates()
                if not current_selected:
                    return job_mgr.APPROVAL_NO_SELECTION
                if job.execute_kind in _PREMISE_REVIEW_KINDS:
                    stale_premise_candidate_ids = candidate_premise.stale_candidate_ids(
                        current_selected, share_artist_captures=True)
                    if {
                        c.get("cid") for c in current_selected
                    } <= stale_premise_candidate_ids:
                        return "all_candidates_stale"
            # A review action consumes only the ticked picks: park everything
            # else (plus the inactive Library tab) as its own living review so
            # one partial batch cannot eat unreviewed candidates.
            split_review = None
            admission_decisions = {}
            already_downloading.clear()

            def selection_filter(candidate):
                key = candidate.get("cid")
                if not isinstance(key, str) or not key:
                    return False
                if key in admission_decisions:
                    return admission_decisions[key]
                if key in stale_owned_candidate_ids:
                    admission_decisions[key] = False
                    return False
                if key in stale_premise_candidate_ids:
                    admission_decisions[key] = False
                    return False
                if tab:
                    gap_active = tab == "gaps"
                    if flows.is_gap_candidate(candidate) != gap_active:
                        admission_decisions[key] = False
                        return False
                if job.execute_kind in (
                        "library", "new_releases", "collection_restore"):
                    album_id = completion.normalise_album_id(
                        (candidate.get("payload") or {}).get("album_id")
                    )
                    if album_id is None:
                        admission_decisions[key] = False
                        return False
                    admitted = runtime._duplicate_download_job(album_id) is None
                    if not admitted:
                        already_downloading.append(candidate)
                else:
                    admitted = True
                admission_decisions[key] = admitted
                return admitted

            if ((job.execute_kind in _PREMISE_REVIEW_KINDS
                    or stale_premise_candidate_ids)
                    and job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                def split_review(review_job):
                    remnant = _build_unapproved_review(
                        review_job,
                        tab,
                        admission_filter=selection_filter,
                        discard_ids=stale_owned_candidate_ids,
                    )
                    if remnant is not None:
                        remnant.execute_args.pop(
                            "_credential_generation", None)
                    # Whole review ticked → retire the worked-through baseline
                    # after success instead of rebuilding its old candidates.
                    if review_job.execute_kind == "library":
                        review_job._consumed_whole_review = remnant is None
                    return remnant

            previous_migration_args = None
            previous_migration_execute = None
            previous_downsample_args = None
            previous_qobuz_args = None
            previous_qobuz_execute = None
            downsample_args_changed = False
            qobuz_args_changed = False
            if authorized_credentials is not None:
                with job._lock:
                    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
                        previous_qobuz_args = job.execute_args
                        previous_qobuz_execute = job._execute_fn
                        job.execute_args = {
                            **(job.execute_args or {}),
                            "_credential_generation":
                                authorized_credentials.generation,
                        }
                        factory = runtime._RESUME_EXECUTE.get(job.execute_kind)
                        if factory is not None:
                            job._execute_fn = factory(job, job.execute_args)
                        qobuz_args_changed = True
            if (
                job.execute_kind == "downsample"
                and downsample_keep_originals is not None
            ):
                with job._lock:
                    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
                        previous_downsample_args = job.execute_args
                        job.execute_args = {
                            **(job.execute_args or {}),
                            "keep_originals": downsample_keep_originals,
                        }
                        job._execute_fn = runtime._resume_downsample(
                            job, job.execute_args)
                        downsample_args_changed = True
            if downsample_choice_to_save:
                saved, _warnings = settings_store.save({
                    "DOWNSAMPLE_KEEP_ORIGINALS": downsample_choice_to_save,
                })
                if saved is not True:
                    return "downsample_policy_failed"
            if job.execute_kind == "migration":
                execute_args = dict(job.execute_args or {})
                # Old parked reviews may still carry the former launcher
                # checkbox. Only an acknowledgement submitted beside the
                # measured short-space review can enable the override now.
                execute_args["allow_low_space"] = bool(
                    migration_low_space_required
                    and migration_low_space_accepted
                )
                execute_fn = runtime._resume_migration(job, execute_args)
                with job._lock:
                    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
                        previous_migration_args = job.execute_args
                        previous_migration_execute = job._execute_fn
                        job.execute_args = execute_args
                        job._execute_fn = execute_fn
            approved = None
            try:
                approved = job_mgr.approve(
                    job,
                    None,
                    split_review=split_review,
                    selection_filter=selection_filter,
                )
                return approved
            finally:
                if downsample_args_changed and approved is not True:
                    with job._lock:
                        job.execute_args = previous_downsample_args
                        job._execute_fn = runtime._resume_downsample(
                            job, job.execute_args or {})
                if qobuz_args_changed and approved is not True:
                    with job._lock:
                        job.execute_args = previous_qobuz_args
                        job._execute_fn = previous_qobuz_execute
                if (
                    previous_migration_args is not None
                    and approved is not True
                ):
                    with job._lock:
                        job.execute_args = previous_migration_args
                        job._execute_fn = previous_migration_execute

    approved = await loop.run_in_executor(None, _split_and_approve)
    if approved == "all_candidates_stale":
        stale_message = await loop.run_in_executor(
            None, lambda: _all_stale_message_for(
                job, stale_premise_candidate_ids.counts))
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(stale_message),
            status_code=303,
        )
    if (isinstance(approved, tuple)
            and len(approved) == 2
            and approved[0] == "candidate_stale"):
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(approved[1]),
            status_code=303,
        )
    if approved == "review_changed":
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(
                "That review changed while approval was being checked. "
                "Nothing changed; review the current selections and try again."
            ),
            status_code=303,
        )
    if approved == "downsample_policy_failed":
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(
                "Couldn't save the keep-or-delete choice. No music files "
                "were changed; check the data folder and try again."
            ),
            status_code=303,
        )
    if approved == "credential_changed":
        message = job_mgr.qobuz_action_error_message(
            CredentialChanged(),
            unchanged=True,
        )
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(message),
            status_code=303,
        )
    if approved == "sync_failed":
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(
                "The refreshed review could not be saved. Your existing "
                "choices are untouched; check the data folder and try again."
            ),
            status_code=303,
        )
    if approved == "paused":
        busy = runtime._lock_busy_response(request)
        if busy is not None:
            return busy
        return RedirectResponse(url=dest, status_code=303)
    if approved is None:
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(
                job_mgr.JOB_ADMISSION_ERROR
            ) + _skip_q,
            status_code=303,
        )
    running_q = ""
    if already_downloading:
        names = _named_albums(already_downloading)
        running_q = "&error=" + runtime._notice_key(
            f"{names} {'is' if len(already_downloading) == 1 else 'are'} "
            "already downloading, so "
            f"{'it was' if len(already_downloading) == 1 else 'they were'} "
            "not started again.")
    if approved is job_mgr.APPROVAL_NO_SELECTION:
        if running_q:
            return RedirectResponse(
                url=f"{dest}?{running_q[1:]}{_skip_q}", status_code=303)
        return RedirectResponse(url=f"{dest}?noselection=1{_skip_q}",
                                status_code=303)
    flag = "approved=1" if approved else "stale=1"
    local_stale = len(stale_premise_candidate_ids)
    local_stale_q = ""
    if local_stale and approved:
        skipped_names = _named_albums([
            c for c in selected_candidate_snapshot
            if c.get("cid") in stale_premise_candidate_ids
        ])
        local_stale_q = "&error=" + runtime._notice_key(
            f"Started the rest. {skipped_names} changed on disk after this "
            f"review was built, so "
            f"{'it was' if local_stale == 1 else 'they were'} skipped and "
            "left ticked."
        )
        # One answer, not two: the green "Queued" banner said the same thing
        # this sentence opens with, stacked above it. Only when this click is
        # the one that started the work; if another click got there first, that
        # is what the page has to say instead.
        flag = ""
    return RedirectResponse(
        url=f"{dest}?{flag}{_skip_q}{local_stale_q or running_q}".replace(
            "?&", "?"),
        status_code=303,
    )


# Kinds whose review screen has server-backed per-candidate selection.
_SELECTABLE_KINDS = review_pages._TRIAGE_KINDS + (
    "repair", "migration", "collection_restore",
)


def _rebuild_results_hint(execute_kind):
    """Where the results behind a review are rebuilt.

    The review screen itself carries no refresh control, so a message telling
    the user to refresh has to name the page that does, or it sends them back
    to press the same button for the same failure.
    """
    if execute_kind == "downsample":
        return "Refresh results on the Downsample page, then try again."
    if execute_kind in ("library", "upgrade"):
        return ("Refresh the Library, which rebuilds these results, then try "
                "again.")
    if execute_kind == "repair":
        return "Start a new scan on the Repair page, then try again."
    if execute_kind == "new_releases":
        return "Run Check new releases on the Library page, then try again."
    if execute_kind == "collection_restore":
        return ("Upload the backup file again from Settings, then try "
                "again.")
    return "Rebuild these results before trying again."


def _all_candidates_stale_message(execute_kind, counts):
    causes = []
    changed = counts.get("changed", 0)
    if changed:
        causes.append(
            f"{plural(changed, 'album')} changed on disk after this review was built")
    unreadable = counts.get("unreadable", 0)
    if unreadable:
        label = str(unreadable) if causes else plural(unreadable, "album")
        causes.append(f"{label} could not be read")
    older = counts.get("older", 0)
    if older:
        label = str(older) if causes else plural(older, "album")
        causes.append(f"{label} {'comes' if older == 1 else 'come'} from an older review")
    return (", ".join(causes) + ". Nothing was started. "
            + _rebuild_results_hint(execute_kind))


def _all_stale_message_for(job, counts):
    """The refusal when every selected candidate went stale.

    A restore review records which music-folder incarnation it was read
    against. When the folder is no longer that one - unmounted, or a different
    disk on the same path - "changed on disk, upload again" is the wrong
    advice: re-uploading would diff the backup against the wrong folder too.
    Mounting the library back makes this same review valid again.
    """
    if job.execute_kind == "collection_restore":
        stored = (job.execute_args
                  if isinstance(job.execute_args, dict) else {}).get("music_root")
        if stored is not None and not candidate_premise.music_root_matches(stored):
            return ("Nothing was started. The music folder is not the one "
                    "this review was built against. Check that your library "
                    "is mounted, then try again; the review is still here.")
    return _all_candidates_stale_message(job.execute_kind, counts)


def _album_label(candidate):
    """One album named the way the review names it."""
    artist = (candidate.get("artist") or "").strip()
    title = (candidate.get("title") or "?").strip()
    return f"{artist} · {title}" if artist else title


def _named_albums(candidates, limit=3):
    """Up to ``limit`` album names, with a count standing in for the rest.

    A message about particular albums has to say which ones. A bare number
    leaves the user reading a review of hundreds looking for the one that
    moved.
    """
    names = [_album_label(c) for c in candidates]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) <= limit:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def _review_origin(request) -> str:
    """The requesting tab's self-assigned id, for review-changed fan-outs."""
    raw = request.headers.get("X-QL-Origin", "")
    return "".join(c for c in raw if c.isalnum())[:32]


def _get_reviewable_job(job_id):
    """A job from the live registry, or rehydrated from disk if it has been evicted,
    so a restored awaiting-review job's selection and pager work, and an evicted
    (terminal) job's review page still renders and pages."""
    job = job_mgr.registry.get(job_id)
    if job is None:
        job = job_mgr.load_historical_job(job_id)
    return job


def _selection_payload(job, *, persist_failed=False):
    """JSON the selection/hide endpoints return so every open tab can refresh
    its counts from the server instead of recounting a partial DOM."""
    c = job.selection_counts()
    payload = {
        "selected": c["selected"],
        "total": c["total"],
        "artists": c["artists"],
        "reclaimable": c["reclaimable"],
        "reclaimable_label": format_size(c["reclaimable"]) if c["reclaimable"] else "",
    }
    if persist_failed:
        payload["persist_failed"] = True
    if job.execute_kind == "library":
        totals = review_pages._review_tab_totals(job)
        payload["missing_total"] = totals["missing"]
        payload["gap_total"] = totals["gaps"]
        payload["missing_selected"] = totals["missing_selected"]
        payload["gap_selected"] = totals["gaps_selected"]
    return payload


def _filtered_selection_payload(job, query, tab):
    """Counts for selection controls scoped to the active filter."""
    groups = review_pages._review_artist_groups(job, query, tab)
    total = sum(len(rows) for _artist, rows in groups)
    rest = review_pages._filtered_rest_of(groups)
    return {
        "filtered_total": total,
        "filtered_selected": total - rest,
        "filtered_rest": rest,
    }


def _set_all_selected_with_membership(job, on, cids):
    """Apply a bulk choice and return the candidate IDs it covered."""
    wanted = set(cids) if cids is not None else None
    accepted_cids = []
    changed = 0
    with job._lock:
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return None, []
        for candidate in job.candidates:
            cid = candidate.get("cid")
            if wanted is not None and cid not in wanted:
                continue
            accepted_cids.append(cid)
            if bool(candidate.get("selected")) != bool(on):
                candidate["selected"] = bool(on)
                changed += 1
    return changed, accepted_cids


@router.post("/jobs/{job_id}/select")
async def job_select(request: Request, job_id: str):
    """Persist a single tick/untick. The review page doesn't rely on the posted
    checkboxes (pagination means most aren't in the DOM), so each toggle saves
    immediately and the saved flags are the source of truth at download."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return JSONResponse({"error": "not found"}, status_code=404)
    form = await request.form()
    cid = (form.get("cid") or "").strip()
    on = (form.get("checked") or "").strip().lower() in ("1", "true", "on", "yes")
    changed = job.set_selected(cid, on)
    if changed is None:
        return JSONResponse(
            {"error": "review is no longer awaiting selection"},
            status_code=409,
        )
    if changed:
        # Coalesced save: the candidate list is multi-MB on a big library, so
        # the tap must not wait for (or even schedule) a full serialize+write.
        job_mgr.persist_soon(job)
        job.notify_review_changed(_review_origin(request))
    payload = _selection_payload(job)
    q = (form.get("q") or "").strip()
    if q:
        payload.update(_filtered_selection_payload(
            job, q, (form.get("tab") or "").strip()))
    return JSONResponse(payload)


@router.post("/jobs/{job_id}/select-all")
async def job_select_all(request: Request, job_id: str):
    """Bulk select/deselect across the whole view, one page, or one artist."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return JSONResponse({"error": "not found"}, status_code=404)
    form = await request.form()
    on = (form.get("on") or "").strip().lower() in ("1", "true", "on", "yes")
    scope = (form.get("scope") or "all").strip().lower()
    if scope not in ("all", "page", "artist"):
        return JSONResponse({"error": "invalid scope"}, status_code=400)
    cids = form.getlist("cid")[:100000] if scope == "page" else None
    # Tab and filter scoping: on a library review, select-all flips only the
    # active tab's candidates, never the tab the user can't see.
    tab = (form.get("tab") or "").strip()
    q = (form.get("q") or "").strip().lower()
    artist = (form.get("artist") or "").strip()
    page_artists = set(form.getlist("artist")[:review_pages.REVIEW_PAGE_ARTISTS])
    tab_scoped = (job.execute_kind == "library" and tab in ("missing", "gaps"))
    if (scope == "artist" or (scope == "page" and page_artists)
            or (cids is None and (tab_scoped or q))):
        gap_active = tab == "gaps"
        with job._lock:
            cids = [c["cid"] for c in job.candidates
                    if (scope != "artist" or (c.get("artist") or "") == artist)
                    and (scope != "page"
                         or not page_artists
                         or (c.get("artist") or "") in page_artists)
                    and (not tab_scoped
                         or flows.is_gap_candidate(c) == gap_active)
                    and (not q or flows.candidate_matches_query(c, q))]
    persist_failed = False
    changed, accepted_cids = _set_all_selected_with_membership(job, on, cids)
    if changed is None:
        return JSONResponse(
            {"error": "review is no longer awaiting selection"},
            status_code=409,
        )
    if changed:
        # Unlike a single tap, a bulk choice is one infrequent operation whose
        # success needs to mean its complete result is durable.
        loop = asyncio.get_running_loop()
        saved = await loop.run_in_executor(
            None, lambda: job_persistence.persist(job))
        persist_failed = not saved
        job.notify_review_changed(_review_origin(request))
    payload = _selection_payload(job, persist_failed=persist_failed)
    payload["accepted_cids"] = accepted_cids
    if q:
        payload.update(_filtered_selection_payload(job, q, tab))
    return JSONResponse(payload)


@router.get("/jobs/{job_id}/review-group-items", response_class=HTMLResponse)
async def job_review_group_items(request: Request, job_id: str,
                                 artist: str = "", tab: str = "", q: str = ""):
    """Render one artist's current rows when a collapsed group is opened."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return HTMLResponse("", status_code=404)
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    groups = review_pages._review_artist_groups(job, query=q, tab=tab)
    items = next((rows for name, rows in groups if name == artist), [])
    return runtime._tr(request, "_review_group_items.html", {
        "job": job, "items": items, "review_tab": tab,
    })


@router.get("/jobs/{job_id}/art/{cid}")
async def job_candidate_art(job_id: str, cid: str):
    """The cover file of a review row's album, straight from its folder.

    Only ever a path the app itself put in the candidate, and only ever a file
    inside the music library, so nothing here can be steered from outside.
    """
    job = job_mgr.registry.get(job_id)
    if not job:
        return Response(status_code=404)
    with job._lock:
        album_dir = next(
            ((c.get("payload") or {}).get("album_dir")
             for c in job.candidates if c.get("cid") == cid),
            None,
        )
    art = runtime._local_album_art(album_dir) if album_dir else None
    if art is None:
        return Response(status_code=404)
    try:
        inside = art.resolve().is_relative_to(Path(cfg.MUSIC_ROOT).resolve())
    except OSError:
        inside = False
    if not inside:
        return Response(status_code=404)
    return FileResponse(
        art,
        # Covers change when the album is re-imported, which also changes the
        # review it is shown in, so a few minutes of caching costs nothing.
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/jobs/{job_id}/hide", response_class=HTMLResponse)
async def job_hide(request: Request, job_id: str):
    """Dismiss an artist's albums from a triage scan (gap or upgrade).

    A triage action, not a download: it writes the durable hidden-store (in
    the scan's scope) and drops those candidates from the review list,
    returning just the affected artist's group (or empty if the whole artist is
    gone) for an htmx swap of that one group. Allowed while the scan is still
    running, and never lock-guarded, so dismissing stays available mid-scan and
    while a download holds the staging lock.
    """
    # Use the disk fallback like every other review endpoint so Hide keeps
    # working on a restored/archived awaiting-review job (registry.get alone
    # 404s once the job is evicted, while /select, /review and /content don't).
    job = _get_reviewable_job(job_id)
    if not job:
        return HTMLResponse("", status_code=404)
    if (job.execute_kind in review_pages._TRIAGE_KINDS and job.status in (
            job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)):
        form = await request.form()
        artist = (form.get("artist") or "").strip()
        # A library review split into Missing Albums / Gap Fill tabs scopes the
        # hide to the tab whose rows the button sat next to; the other tab's
        # candidates for this artist are untouched.
        tab = (form.get("tab") or "").strip()
        if job.execute_kind != "library" or tab not in ("missing", "gaps"):
            tab = ""
        gap_only = (tab == "gaps") if tab else None
        # The filter narrows what the button sits next to, so it has to narrow
        # what the button takes; without it, one tap on a filtered row dismisses
        # every album by that artist, including the ones the filter is hiding.
        q = (form.get("q") or "").strip()
        # Selection is server-backed, so hide keeps this artist's ticked albums
        # and drops the rest, with no form keep-set, which under pagination would
        # only carry the visible page and clobber other pages' selections.
        try:
            with saved_reviews._SAVED_REVIEW_LOCK:
                n = flows.dismiss_albums(job, artist,
                                         scope=review_pages._hide_scope(job.execute_kind),
                                         gap_only=gap_only,
                                         query=q)
        except OSError as e:
            # Nothing changed server-side; the non-2xx keeps htmx from
            # swapping the rows away and the error toast reads this body.
            return HTMLResponse(str(e), status_code=500)
        if n is False:
            return HTMLResponse(
                "The dismissal could not be saved to the Library review. "
                "Nothing was dismissed. Check the data folder and try again.",
                status_code=503,
            )
        if n is None:
            return HTMLResponse(
                "That review changed before the dismissal was saved. Reload "
                "the page and try again.",
                status_code=409,
            )
        if n:
            # Keep other open tabs in sync; the originator already gets the
            # swapped group + fresh counts from this response.
            job.notify_review_changed(_review_origin(request))
        # Dismissing the last album completes the review, so drop AWAITING_REVIEW
        # the dashboard "new releases" banner clears and this page stops showing an
        # empty "awaiting review". HX-Refresh reloads to the finished view.
        finalized = job_mgr.finalize_review_if_empty(job)
        if finalized is None:
            return HTMLResponse(
                "The dismissal was saved, but the finished review could not "
                "be recorded. Check the data folder and reload.",
                status_code=503,
            )
        if finalized:
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        # Re-render what the filter shows, not the whole artist, so the group
        # that swaps in matches the list the user is looking at.
        groups = review_pages._review_artist_groups(job, query=q, tab=tab)
        remaining = next((rows for name, rows in groups if name == artist), [])
        if remaining:
            resp = runtime._tr(request, "_review_group.html",
                       {"job": job, "artist": artist, "items": remaining,
                        "triage": True, "open": True, "review_tab": tab,
                        "review_query": q})
        else:
            resp = HTMLResponse("")  # whole artist hidden, outerHTML drops it
        if n:
            # Carry the fresh authoritative counts so the page updates the
            # summary/selected/reclaimable without recounting a partial DOM.

            counts = _selection_payload(job)
            counts["hidden_total"] = hidden_mod.count(
                review_pages._hide_scope(job.execute_kind))
            if q:
                counts.update(_filtered_selection_payload(job, q, tab))
            resp.headers["HX-Trigger-After-Swap"] = json.dumps(
                {"qlHidden": {"n": n, "counts": counts}})
        return resp
    return HTMLResponse("")


@router.post("/jobs/{job_id}/dismiss-rest")
async def job_dismiss_rest(request: Request, job_id: str):
    """Dismiss every album the user didn't pick: durable-hide all unselected
    candidates across the whole review at once, leaving just the keepers."""
    job = _get_reviewable_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not (job.execute_kind in review_pages._TRIAGE_KINDS and job.status in (
            job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)):
        return JSONResponse({"error": "not reviewable"}, status_code=404)

    scope = review_pages._hide_scope(job.execute_kind)
    # Tab scoping: "Dismiss unselected" on a library review only drops the
    # active tab's unselected candidates.
    form = await request.form()
    tab = (form.get("tab") or "").strip()
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    gap_only = (tab == "gaps") if tab else None
    # An active filter narrows the dismissal to the rows it shows, same
    # what-you-see-is-what-you-act-on rule as select-all.
    q = (form.get("q") or "").strip().lower()
    with job._lock:
        artists, seen = [], set()
        for c in job.candidates:
            if c.get("selected"):
                continue
            if gap_only is not None and flows.is_gap_candidate(c) != gap_only:
                continue
            if q and not flows.candidate_matches_query(c, q):
                continue
            name = c.get("artist") or ""
            if name not in seen:
                seen.add(name)
                artists.append(name)

    # Offload: this can touch the whole review (a hidden-store write per artist
    # plus a persist), which would block the event loop and stall every SSE
    # stream for a large scan.
    loop = asyncio.get_running_loop()
    done = {"n": 0, "stale": False, "save_failed": False}

    def _dismiss_all():
        with saved_reviews._SAVED_REVIEW_LOCK, job._review_action_lock:
            for a in artists:
                hidden = flows.dismiss_albums(
                    job,
                    a,
                    scope=scope,
                    gap_only=gap_only,
                    query=q,
                )
                if hidden is None:
                    done["stale"] = True
                    break
                if hidden is False:
                    done["save_failed"] = True
                    break
                done["n"] += hidden

    try:
        await loop.run_in_executor(None, _dismiss_all)
        hidden_count = done["n"]
    except OSError as e:
        # Mid-batch store failure: the artists already hidden stay hidden, the
        # rest are untouched, so report the failure instead of a count.
        if done["n"]:
            job.notify_review_changed()
        return JSONResponse({"error": str(e), "hidden": done["n"]},
                            status_code=500)
    if done["stale"]:
        if done["n"]:
            job.notify_review_changed()
        return JSONResponse(
            {
                "error": "That review changed before dismissal completed.",
                "hidden": done["n"],
            },
            status_code=409,
        )
    if done["save_failed"]:
        if done["n"]:
            job.notify_review_changed()
        return JSONResponse(
            {
                "error": "The Library review could not be saved.",
                "hidden": done["n"],
            },
            status_code=503,
        )
    if hidden_count:
        job.notify_review_changed(_review_origin(request))
    payload = _selection_payload(job)
    payload["hidden"] = hidden_count
    payload["hidden_total"] = hidden_mod.count(scope)
    if q:
        payload.update(_filtered_selection_payload(job, q, tab))
    finalized = job_mgr.finalize_review_if_empty(job)
    payload["review_done"] = finalized is True
    if finalized is None:
        payload["finalize_failed"] = True
    return JSONResponse(payload)


@router.post("/jobs/{job_id}/give-up")
async def job_give_up(request: Request, job_id: str):
    """Abandon a blocked download so downloads and scans can run again.

    Retry is the right first move, but a download can be stuck on something a
    retry repeats exactly, and until it is settled every download and scan
    stays paused. This throws the interrupted download away, keeps an album
    Beets finished filing in the library, and otherwise leaves the album to
    be started again whenever the user wants.
    """
    job = job_mgr.registry.get(job_id) or job_mgr.load_historical_job(job_id)
    if not job:
        return RedirectResponse(
            url="/queue?error=" + runtime._notice_key(
                "That job is no longer in History."),
            status_code=303)
    form = await request.form()
    if not runtime._recovery_submission_matches(
        job,
        str(form.get("recovery_operation_id") or ""),
        str(form.get("recovery_item_id") or ""),
    ):
        return runtime._durable_recovery_response(
            request,
            "That interrupted download is no longer the one holding things "
            "up. Nothing was changed. Reload the page.",
        )
    control = runtime._durable_recovery_control()
    imported = bool(control and (control.get("imported") or control.get("partial")))
    settled, reason = runtime._settle_durable_web_recovery(
        job,
        BlockedItemSettlementAction.DISCARD,
    )
    _log.info(
        "Give up %s: discarding the blocked download %s.", job.id,
        "succeeded" if settled
        else f"was refused: {(reason or 'no reason given').rstrip('.')}")
    if not settled:
        return runtime._durable_recovery_response(
            request,
            reason or "The interrupted download could not be discarded.",
        )
    with job._lock:
        job.attention = ""
        job.error = (
            f"Abandoned. {reason} Downloads and scans can run again."
            if imported else
            "Abandoned. The interrupted download was discarded and nothing "
            "was added to your library. Downloads and scans can run again, "
            "and you can start this album whenever you like."
        )
    if not job_persistence.persist(job):
        return runtime._durable_recovery_response(
            request,
            "The interrupted download was cleared and downloads can run "
            "again, but its outcome could not be saved to History. "
            + (reason if imported else "Nothing was added to your library.")
            + " Check the data-folder permissions before restarting Qobuz "
            "Librarian.",
        )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


_RETRY_RETURN_KEYS = ("p", "jp", "attention")


def _retry_return_url(raw: object, **extra: str) -> str | None:
    """The History page a Retry was clicked on, or None for anything else.

    History sends its own address with the click so the outcome arrives where
    the user is already looking instead of dragging them into the job card.
    Only that one surface is honoured and only its own paging keys ride along,
    so a forged field cannot aim the redirect somewhere else.
    """
    path, _, query = (raw if isinstance(raw, str) else "").partition("?")
    if path != "/queue/history":
        return None
    params = [(k, v) for k, v in urllib.parse.parse_qsl(query)
              if k in _RETRY_RETURN_KEYS]
    params += [(k, v) for k, v in extra.items() if v]
    return path + ("?" + urllib.parse.urlencode(params) if params else "")


@router.post("/jobs/{job_id}/retry")
async def job_retry(request: Request, job_id: str):
    # Retry rebuilds the download from the persisted album_id, so it works as
    # well for a job evicted from the registry (restart, or 50 jobs later) as
    # for a live one, so fall back to the archive instead of silently bouncing.
    # The form is read before the first refusal, not halfway down the route:
    # every answer below has to land where the button was clicked.
    form = await request.form()
    return_to = form.get("return_to")

    def _land(started: str = "", error: str = "") -> RedirectResponse:
        if error:
            error = runtime._notice_key(error)
        dest = _retry_return_url(return_to, started=started, error=error)
        if dest is None:
            dest = (f"/jobs/{started}" if started
                    else "/queue?error=" + error)
        return RedirectResponse(url=dest, status_code=303)

    job = job_mgr.registry.get(job_id) or job_mgr.load_historical_job(job_id)
    if not job:
        return _land(error="That job is no longer in History.")
    if (job.status != job_mgr.JobStatus.FAILED or not job.album_id
            or (job.execute_args or {}).get("retry_disabled") == "terminal"):
        return _land(error="Nothing to retry for that job.")
    if job.execute_args_unreadable:
        return _land(
            error="The saved details needed to retry that job couldn't be "
                  "read. Start the download again from Search or Library.",
        )
    if (job.execute_args or {}).get("retry_disabled") == "lossy":
        return _land(
            error="Qobuz only has the missing tracks in lossy quality. This "
                  "album needs another source, so it cannot be retried here.",
        )
    if (job.execute_args or {}).get("retry_disabled") == "backup":
        return _land(
            error="This album has a retained safety backup. Review it under "
                  "Settings > Diagnostics before starting the album again.",
        )
    if (
        getattr(job, "_preserve_persisted_single", False) is True
        or getattr(job, "_single_undo_unavailable", False) is True
    ):
        return runtime._durable_recovery_response(
            request,
            "This track's saved recovery state is uncertain, so Retry is "
            "paused. No download was started. Restart Qobuz Librarian.",
        )
    raw_recovery_operation = form.get("recovery_operation_id")
    raw_recovery_item = form.get("recovery_item_id")
    recovery_submission = (
        raw_recovery_operation is not None or raw_recovery_item is not None
    )
    recovery_operation_id = (
        raw_recovery_operation.strip()
        if isinstance(raw_recovery_operation, str)
        else ""
    )
    recovery_item_id = (
        raw_recovery_item.strip()
        if isinstance(raw_recovery_item, str)
        else ""
    )
    if recovery_submission and (
        not recovery_operation_id
        or not recovery_item_id
        or len(recovery_operation_id) > 128
        or len(recovery_item_id) > 128
    ):
        return runtime._durable_recovery_response(
            request,
            "That recovery Retry is incomplete or stale. No download was "
            "started. Reload this page and try again.",
        )

    try:
        credentials = await runtime._authorize_qobuz_for_web(
            QobuzAccess.DOWNLOAD_ACTION
        )
    except runtime._QOBUZ_ACTION_ERRORS as exc:
        message = job_mgr.qobuz_action_error_message(exc, unchanged=True)
        return _land(error=message)

    # A Retry is also the only user-triggered lane for an interrupted durable
    # Web download.
    if not runtime._run_lock_intact():
        busy = runtime._lock_busy_response(request)
        if busy is not None:
            return busy
        return runtime._durable_recovery_response(
            request,
            "The run lock could not be verified. No download was started. "
            "Restart Qobuz Librarian.",
        )
    try:
        recovery = runtime._record_startup_recovery(runtime.run_lock_handle())
    except Exception:
        _log.exception(
            "couldn't check recovery before retrying job %s", job.id)
        return runtime._durable_recovery_response(
            request,
            "The saved recovery state could not be checked safely. No "
            "download was started. Restart Qobuz Librarian.",
        )
    recovery_status = runtime._recovery_status_value(recovery)

    completion_acknowledged = runtime._durable_completion_status(job)
    if completion_acknowledged is None:
        return runtime._durable_recovery_response(
            request,
            "The saved completion record could not be checked safely. No "
            "download was started. Check the data-folder permissions, then "
            "restart Qobuz Librarian.",
        )
    if completion_acknowledged:
        if recovery_status != "clear":
            return runtime._durable_recovery_response(
                request,
                "This download is already recorded as complete, but its "
                "interrupted recovery proof is not settled yet. No download "
                "was started. Check the application log, then restart Qobuz "
                "Librarian.",
            )
        busy = runtime._lock_busy_response(request)
        if busy is not None:
            return busy
        if not runtime._reconcile_acknowledged_job(job):
            return runtime._durable_recovery_response(
                request,
                "The completed download could not be saved to History. No "
                "download was started. Check the data-folder permissions, "
                "then restart Qobuz Librarian.",
            )
        return _land(started=job.id)

    # A different album's unsettled recovery used to refuse this Retry
    # outright, leaving the second album nowhere to go. It waits for that one
    # to settle instead.
    queue_behind = runtime._recovery_pause_is_another_download(job)

    if recovery_submission:
        if not runtime._recovery_submission_matches(
            job,
            recovery_operation_id,
            recovery_item_id,
        ):
            return runtime._durable_recovery_response(
                request,
                "That interrupted-download Retry is stale. No download was "
                "started. Reload the job and use its current Retry button.",
            )
    elif (
        recovery_status != "clear" or job.attention == "recovery"
    ) and not queue_behind:
        return runtime._durable_recovery_response(
            request,
            "This download needs its exact recovery Retry control. No "
            "download was started. Reload the interrupted job and use Retry "
            "there.",
        )

    if (
        recovery_submission
        and
        recovery_status == "attention_required"
        and runtime._durable_recovery_matches_job(job)
    ):

        with runtime._CREDENTIAL_LOCK:
            if not runtime._credential_generation_is_active(credentials.generation):
                message = job_mgr.qobuz_action_error_message(
                    CredentialChanged(),
                    unchanged=True,
                )
                return _land(error=message)
            settled, reason = runtime._settle_durable_web_recovery(
                job,
                BlockedItemSettlementAction.RETRY,
            )
        _log.info(
            "Retry %s: settling the blocked download %s.", job.id,
            "succeeded" if settled
            else f"was refused: {(reason or 'no reason given').rstrip('.')}")
        if not settled:
            reconciled = runtime._settled_completion_response(request, job)
            if reconciled is not None:
                return reconciled
            return runtime._durable_recovery_response(
                request,
                reason or "The interrupted download remains blocked.",
            )
        recovery = runtime.startup_recovery_result()
        recovery_status = runtime._recovery_status_value(recovery)
    durable_resume = (
        recovery_status == "resume_required"
        and runtime._durable_recovery_matches_job(job)
    )
    if job.attention == "recovery" and not (
        recovery_status == "clear" or durable_resume
    ):
        return runtime._durable_recovery_response(
            request,
            "This download needs recovery attention and cannot be retried "
            "safely. No download was started. Check the application log, "
            "then restart Qobuz Librarian.",
        )
    if not queue_behind:
        if recovery_status == "attention_required":
            return runtime._durable_recovery_response(
                request,
                "The saved interrupted download needs recovery attention. No "
                "download was started. Check the application log, then restart "
                "Qobuz Librarian.",
            )
        if recovery_status == "resume_required" and not durable_resume:
            return runtime._durable_recovery_response(
                request,
                "Saved recovery belongs to a different or changed download. No "
                "download was started. Retry only the exact interrupted job.",
            )
        if recovery_status not in {"clear", "resume_required"}:
            return runtime._durable_recovery_response(
                request,
                "The saved recovery state could not be verified safely. No "
                "download was started. Restart Qobuz Librarian.",
            )
    busy = runtime._lock_busy_response(
        request,
        durable_resume_job_id=job.id if durable_resume else None,
        queue_behind_job=job if queue_behind else None,
    )
    if busy is not None:
        return busy
    album_id = job.album_id
    retry_as_new = bool((job.execute_args or {}).get("new_edition"))
    duplicate = runtime._find_job_touching_album(album_id)
    if duplicate:
        return _land(started=duplicate.id)
    try:
        loop = asyncio.get_running_loop()
        token = credentials.token
        album = None
        if not durable_resume:
            album = await runtime._qobuz_call(
                qobuz_search.get_album, album_id, token)
        same_edition_complete = bool(
            album is not None
            and retry_as_new
            and await loop.run_in_executor(
                None, lambda: runtime._same_edition_is_complete(album)
            )
        )
        # Re-check under the submit lock.
        with runtime._DOWNLOAD_SUBMIT_LOCK, runtime._CREDENTIAL_LOCK:
            if not runtime._credential_generation_is_active(credentials.generation):
                message = job_mgr.qobuz_action_error_message(
                    CredentialChanged(),
                    unchanged=True,
                )
                return _land(error=message)
            duplicate = runtime._find_job_touching_album(album_id)
            if duplicate:
                return _land(started=duplicate.id)
            # set_mode could have handed the lock to the terminal during the
            # get_album await above; re-check inside the submit lock (as
            # queue_download does) so a retry can't start a job after the CLI
            # handoff.
            if not runtime._run_lock_intact():
                busy = runtime._lock_busy_response(request)
                if busy is not None:
                    return busy
                return runtime._durable_recovery_response(
                    request,
                    "The run lock was lost while Retry was preparing. No "
                    "download was started. Restart Qobuz Librarian.",
                )
            try:
                recovery_now = runtime._record_startup_recovery(runtime.run_lock_handle())
            except Exception:
                _log.exception(
                    "couldn't recheck recovery while retrying job %s", job.id)
                return runtime._durable_recovery_response(
                    request,
                    "The saved recovery state changed while Retry was "
                    "preparing and could not be checked safely. No download "
                    "was started. Restart Qobuz Librarian.",
                )
            recovery_status_now = runtime._recovery_status_value(recovery_now)
            durable_resume_now = (
                recovery_status_now == "resume_required"
                and runtime._durable_recovery_matches_job(job)
            )
            if recovery_submission and not runtime._recovery_submission_matches(
                job,
                recovery_operation_id,
                recovery_item_id,
            ):
                return runtime._durable_recovery_response(
                    request,
                    "The interrupted download changed while Retry was "
                    "preparing. No download was started. Reload the job and "
                    "try again.",
                )
            acknowledged_now = runtime._durable_completion_status(job)
            if acknowledged_now is None:
                return runtime._durable_recovery_response(
                    request,
                    "The saved completion record could not be checked safely. "
                    "No download was started. Restart Qobuz Librarian.",
                )
            if acknowledged_now:
                if recovery_status_now == "clear" and (
                    runtime._reconcile_acknowledged_job(job)
                ):
                    return _land(started=job.id)
                return runtime._durable_recovery_response(
                    request,
                    "This download is already recorded as complete, but its "
                    "recovery could not be finalized safely. No download was "
                    "started. Restart Qobuz Librarian.",
                )
            queue_behind = runtime._recovery_pause_is_another_download(job)
            if job.attention == "recovery" and not (
                recovery_status_now == "clear" or durable_resume_now
            ):
                return runtime._durable_recovery_response(
                    request,
                    "This download needs recovery attention and cannot be "
                    "retried safely. No download was started. Restart Qobuz "
                    "Librarian.",
                )
            if not queue_behind:
                if recovery_status_now == "attention_required":
                    return runtime._durable_recovery_response(
                        request,
                        "The saved interrupted download needs recovery "
                        "attention. No download was started. Restart Qobuz "
                        "Librarian.",
                    )
                if (
                    recovery_status_now == "resume_required"
                    and not durable_resume_now
                ):
                    return runtime._durable_recovery_response(
                        request,
                        "The saved interrupted download no longer matches this "
                        "job. No download was started. Restart Qobuz Librarian.",
                    )
                if recovery_status_now not in {"clear", "resume_required"}:
                    return runtime._durable_recovery_response(
                        request,
                        "The saved recovery state could not be verified safely. "
                        "No download was started. Restart Qobuz Librarian.",
                    )
            if durable_resume and recovery_status_now == "clear":
                return runtime._durable_recovery_response(
                    request,
                    "The saved interrupted download changed while Retry was "
                    "preparing. No download was started. Restart Qobuz "
                    "Librarian.",
                )
            durable_resume = durable_resume_now
            if not durable_resume and same_edition_complete:
                return _land(
                    error="This edition is already in your library. "
                          "Nothing to retry.",
                )
            busy = runtime._lock_busy_response(
                request,
                durable_resume_job_id=job.id if durable_resume else None,
                queue_behind_job=job if queue_behind else None,
            )
            if busy is not None:
                return busy
            durable_planned = None
            if durable_resume:
                durable_planned = runtime._durable_recovery_planned(job)
                if durable_planned is None:
                    return runtime._durable_recovery_response(
                        request,
                        "The exact saved download plan could not be loaded "
                        "safely. No download was started. Restart Qobuz "
                        "Librarian.",
                    )
                album = durable_planned.get("album")
                if not isinstance(album, dict):
                    return runtime._durable_recovery_response(
                        request,
                        "The exact saved download plan is invalid. No "
                        "download was started. Restart Qobuz Librarian.",
                    )
            elif album is None:
                return runtime._durable_recovery_response(
                    request,
                    "The album lookup changed while Retry was preparing. No "
                    "download was started. Reload the job and try again.",
                )
            title = album.get("title") or job.title or "?"
            artist = (album.get("artist") or {}).get("name") or job.artist or "?"
            # A failed single-track download carries job.album_id (so Retry shows up),
            # but _make_download_run would download the whole album. Rebuild it as
            # the same one-track run instead.
            single = job.single
            track = None
            as_new = False
            if durable_resume and single and single.get("track_id"):
                return runtime._durable_recovery_response(
                    request,
                    "The saved full-album recovery does not match this "
                    "single-track job. No download was started. Restart "
                    "Qobuz Librarian.",
                )
            if single and single.get("track_id"):
                tid = str(single.get("track_id"))
                track = next(
                    (t for t in (album.get("tracks") or {}).get("items") or []
                     if str(t.get("id")) == tid), None)
            if track is not None:
                run = runtime._make_single_track_run(album, track, token)
            elif single and single.get("track_id"):
                # The original was a single-track download but that track is no
                # longer on Qobuz, so do NOT silently re-download the whole album.
                return _land(
                    error="That track is no longer on Qobuz. Nothing to retry.")
            else:
                # Carry the "get this edition too" override across the retry,
                # without it the rebuilt run sees the album as already owned
                # and skips the download the user explicitly asked for.
                if durable_planned is not None:
                    run = runtime._make_download_run(
                        album,
                        token,
                        durable_planned=durable_planned,
                    )
                else:
                    as_new = retry_as_new
                    run = runtime._make_download_run(
                        album,
                        token,
                        treat_as_new=as_new,
                    )
            edition = str(
                ((track.get("version") if track is not None else None)
                 or album.get("version") or job.edition or "")
            ).strip()
            if durable_resume:
                job.edition = edition
                if not job_mgr.resubmit_failed(job, run):
                    return runtime._durable_recovery_response(
                        request,
                        "The exact interrupted job could not be queued safely. "
                        "No download was started. Restart Qobuz Librarian.",
                    )
                new_job = job
            else:
                new_job = job_mgr.Job(
                    title=(track.get("title") or title)
                    if track is not None else title,
                    artist=artist,
                    album_id=album_id,
                    edition=edition,
                )
                if track is not None:
                    # Seed the same two keys /download does at submit and let
                    # the run fill in the rest. Copying the whole dict carried
                    # the old job's Undo record onto a job that may download
                    # nothing (the "you already have this" early return never
                    # touches j.single), so two jobs offered Undo of one file
                    # and one of them claimed it downloaded nothing.
                    new_job.single = {
                        "album_id": album_id,
                        "track_id": str(single.get("track_id")),
                    }
                if as_new:
                    new_job.execute_args = {"new_edition": True}
                submit = (job_mgr.submit_held if queue_behind
                          else job_mgr.submit)
                if submit(new_job, run) is None:
                    return runtime._job_admission_response(request)
                # The retry is the answer to the failure, so the old row stops
                # holding the Queue warning dot.
                attention = job.attention
                if attention and attention != "recovery" and (
                    job_persistence.acknowledge_attention(job.id, attention)
                ):
                    with job._lock:
                        if job.attention == attention:
                            job.attention = ""
        return _land(started=new_job.id)
    except NoCredsError as exc:
        message = job_mgr.qobuz_action_error_message(exc, unchanged=True)
        return _land(error=message)
    except Exception as exc:
        _log.warning("couldn't prepare retry for job %s", job.id, exc_info=True)
        message = runtime._download_error_message(
            exc,
            "Couldn't prepare this retry. Try again.",
        )
        return _land(error=message)


@router.post("/jobs/{job_id}/undo")
async def job_undo(request: Request, job_id: str):
    """Reverse a single-track download whose exact owned path is still bound."""
    # Undo deletes files and touches the beets DB, so it needs the same run-lock
    # gate every other mutating route has; the in-process staging lock below
    # can't keep it off the library while a CLI session or another instance
    # holds the cross-process lock.
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        if runtime._is_htmx(request):
            return HTMLResponse(
                f'<div id="job-content">{busy.body.decode()}</div>')
        return busy
    # The single payload is persisted, so Undo keeps working after the job
    # ages out of the registry, and the file checks below already handle a track
    # that vanished in the meantime.
    job = job_mgr.registry.get(job_id) or job_mgr.load_historical_job(job_id)
    undo_uncertain = bool(
        job
        and (
            getattr(job, "_preserve_persisted_single", False) is True
            or getattr(job, "_single_undo_unavailable", False) is True
        )
    )
    info = (
        dict(job.single or {})
        if job and not undo_uncertain
        else {}
    )
    catalog_cleanup = info.get("catalog_cleanup")
    cleanup_retry = (
        type(catalog_cleanup) is dict
        and catalog_cleanup.get("pending") is True
        and isinstance(catalog_cleanup.get("path"), str)
        and bool(catalog_cleanup["path"])
    )
    if (
        not job
        or not info.get("dir")
        or (info.get("removed") and not cleanup_retry)
    ):
        if runtime._is_htmx(request):
            if job:
                return runtime._tr(request, "_job_body.html", {"job": job})
            return HTMLResponse("", headers={"HX-Redirect": "/queue"})
        msg = ("That job is no longer in History." if not job
               else "Nothing to undo for that job.")
        return RedirectResponse(
            url="/queue?error=" + runtime._notice_key(msg), status_code=303)

    def _refresh_after_undo():
        artist = info.get("artist") or ""
        album = {
            "title": info.get("album") or "",
            "artist": {"name": artist},
        }
        try:
            flows._refresh_after_local_album_change(
                album,
                {"dir": info.get("dir") or ""},
                fallback_artist=artist,
                token=runtime._get_optional_token(),
                args=flows.build_args(),
                upgrade=True,
                downsample=True,
            )
        except Exception as exc:
            _log.info(
                "quality state refresh after undo skipped: %s", exc)

    undo_outcome = {}

    def _clear_deliberate_single_mark() -> bool:
        """Return only after the exact suppression mark is durably absent."""
        if not info.get("marked"):
            return True
        try:
            hidden_mod.unmark_single(
                info.get("artist") or "",
                info.get("album") or "",
                year=info.get("year"),
                album_id=info.get("album_id"),
            )
            store = hidden_mod.load()
            return not hidden_mod.is_single(
                info.get("artist") or "",
                info.get("album") or "",
                store,
                year=info.get("year"),
                album_id=info.get("album_id"),
            )
        except (OSError, TypeError, ValueError):
            return False

    def _reverse():
        if cleanup_retry:
            catalog_result = beets_mod.forget_beets_entries(
                [Path(catalog_cleanup["path"])]
            )
            undo_outcome["single_mark_complete"] = (
                _clear_deliberate_single_mark()
            )
            return None, catalog_result
        owned_root = info.get("owned_root")
        if owned_root is not None:
            current_root = os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
            if (
                not isinstance(owned_root, str)
                or os.path.abspath(owned_root) != current_root
            ):
                return None, None
            d = Path(current_root)
        else:
            d = Path(info["dir"])
        # Every intent and state/identity refresh reaches the durable job row
        # while the direct-operation lock still excludes another library
        # writer.
        job.single = info

        def _persist_progress():
            return job_persistence.persist(job)

        removed = owned_paths._unlink_owned_path(
            d,
            info.get("owned_path"),
            progress=_persist_progress,
            outcome_out=undo_outcome,
        )
        catalog_result = None
        if removed is not None:
            catalog_result = beets_mod.forget_beets_entries([removed])
            undo_outcome["single_mark_complete"] = (
                _clear_deliberate_single_mark()
            )
        return removed, catalog_result

    # Register under the same gate as the CLI handoff before taking the
    # staging mutex.
    loop = asyncio.get_running_loop()
    state, operation_token, lock = await loop.run_in_executor(
        None, lambda: runtime._begin_direct_library_operation("Undo"))
    if state == "paused":
        paused = runtime._lock_busy_response(request)
        if paused is not None:
            return paused
        return runtime._tr(request, "lock_busy.html", {
            "msg": "Library writes were paused before Undo could start."
        }, status_code=503)
    if state == "busy":
        holder = job_mgr.staging_holder()
        msg = (f"{holder} is using the library right now. Try Undo again "
               "when it finishes." if holder else
               "Another job is using the library right now. Try Undo again "
               "in a moment.")
        if runtime._is_htmx(request):
            return HTMLResponse(
                f'<div id="job-content">'
                f'{runtime._ql_notice_html("warning", html.escape(msg))}</div>')
        return runtime._tr(request, "lock_busy.html", {"msg": msg}, status_code=503)
    lock_held = True
    try:
        removed, catalog_result = await loop.run_in_executor(None, _reverse)
        catalog_complete = bool(
            getattr(catalog_result, "complete", catalog_result)
        )
        single_mark_complete = undo_outcome.get(
            "single_mark_complete", True
        )
        if removed is not None or cleanup_retry:
            # The track is off the disk again, so search must stop calling it
            # yours.
            job.landed_complete = False
        refresh_needed = False
        if cleanup_retry:
            if catalog_complete and single_mark_complete:
                completed = {**info, "removed": True}
                completed.pop("catalog_cleanup", None)
                job.single = completed
                if job.attention == "catalog":
                    job.attention = ""
                job.summary = (
                    f"Removed “{info.get('title')}” and undid the single."
                )
            elif catalog_complete:
                job.single = {**info, "removed": False}
                job.single.pop("catalog_cleanup", None)
                if job.attention == "catalog":
                    job.attention = ""
                job.summary = (
                    f"Removed “{info.get('title')}”, but couldn't save the "
                    "single mark cleanup. Retry Undo."
                )
            else:
                job.attention = "catalog"
                job.summary = (
                    f"Removed “{info.get('title')}”, but couldn't clear its "
                    "stale Beets catalogue entry. Retry catalogue cleanup."
                )
        elif removed is not None:
            refresh_needed = True
            if catalog_complete and single_mark_complete:
                job.single = {**info, "removed": True}
                job.summary = (
                    f"Removed “{info.get('title')}” and undid the single."
                )
            elif catalog_complete:
                job.single = {**info, "removed": False}
                job.summary = (
                    f"Removed “{info.get('title')}”, but couldn't save the "
                    "single mark cleanup. Retry Undo."
                )
            else:
                job.single = {
                    **info,
                    "removed": True,
                    "catalog_cleanup": {
                        "pending": True,
                        "path": str(removed),
                    },
                }
                job.attention = "catalog"
                job.summary = (
                    f"Removed “{info.get('title')}”, but couldn't clear its "
                    "stale Beets catalogue entry. Retry catalogue cleanup."
                )
        else:
            # If the whole recorded directory is gone, clearing the single
            # mark cannot delete anything.
            dir_gone = (
                info.get("owned_root") is None
                and not Path(info["dir"]).exists()
            )
            if dir_gone:
                single_mark_complete = await loop.run_in_executor(
                    None, _clear_deliberate_single_mark
                )
                refresh_needed = True
                job.single = {
                    **info,
                    "removed": bool(single_mark_complete),
                }
                if single_mark_complete:
                    job.summary = (f"“{info.get('title')}” was already gone; "
                                   "cleared the single mark.")
                else:
                    job.summary = (
                        f"“{info.get('title')}” was already gone, but couldn't "
                        "save the single mark cleanup. Retry Undo."
                    )
            else:
                if undo_outcome.get("files_complete"):
                    job.summary = (
                        f"Removed “{info.get('title')}”, but couldn't safely "
                        "finish cleaning up its folders. Try Undo again.")
                elif undo_outcome.get("removed_files"):
                    job.summary = (
                        "Part of Undo completed, but couldn't safely finish "
                        f"removing “{info.get('title')}”. Try Undo again.")
                elif undo_outcome.get("held_files"):
                    job.summary = (
                        "Undo safely set the downloaded copy of "
                        f"“{info.get('title')}” aside, but couldn't finish "
                        "removing it. Try Undo again.")
                elif undo_outcome.get("undo_started"):
                    job.summary = (
                        "Undo started but couldn't safely finish removing "
                        f"“{info.get('title')}”. Try Undo again.")
                else:
                    job.summary = (
                        "Couldn't safely verify the downloaded copy of "
                        f"“{info.get('title')}”. Nothing was removed; delete "
                        "it manually if needed.")
        # Persist while the direct-operation registration still holds the web
        # run lock; a restart must not resurrect an Undo that already removed a
        # file or cleared its single mark.
        saved = await loop.run_in_executor(
            None, lambda: job_persistence.persist(job)
        )
        if not saved:
            msg = (
                "Undo changed the saved track state, but its final record "
                "couldn't be saved. Retry Undo before clearing History."
            )
            job.single = info
            job.summary = msg
            if runtime._is_htmx(request):
                return runtime._tr(request, "_job_body.html", {"job": job})
            return runtime._tr(request, "lock_busy.html", {
                "reason": "Undo needs attention",
                "msg": msg,
                "action": {"href": f"/jobs/{job.id}", "label": "Retry Undo"},
            }, status_code=503)
        lock.release()
        lock_held = False
        if refresh_needed:
            await loop.run_in_executor(None, _refresh_after_undo)
        if runtime._is_htmx(request):
            return runtime._tr(request, "_job_body.html", {"job": job})
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    finally:
        if lock_held:
            lock.release()
        job_mgr.end_library_operation(operation_token)


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(
    request: Request,
    job_id: str,
    return_to: str = Form(""),
):
    return_to_queue = return_to == "/queue"
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    was_review = job.status == job_mgr.JobStatus.AWAITING_REVIEW
    was_pending = job.status == job_mgr.JobStatus.PENDING
    # Offload: cancelling a parked review runs cancel_review -> persist (a
    # json.dumps of the full candidate list + SQLite commit), which would block
    # the event loop and stall every SSE stream for a large review.
    loop = asyncio.get_running_loop()
    canceled = await loop.run_in_executor(
        None, lambda: job_mgr.request_cancel(job)
    )
    if not canceled:
        protected = job_mgr.cancel_is_protected(job)
        importing = job.importing
        if protected:
            message = "An interrupted-download recovery cannot be cancelled."
        elif importing:
            message = "Import has started and cannot be stopped."
        elif job.status in job_mgr.TERMINAL:
            message = "That job had already finished."
        else:
            message = "The cancel could not be saved to the data folder."
        dest = "/queue" if return_to_queue else f"/jobs/{job_id}"
        return RedirectResponse(
            url=dest + "?error=" + runtime._notice_key(message),
            status_code=303,
        )
    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
        return RedirectResponse(url=runtime._job_nav_destination(job)[1], status_code=303)
    if return_to_queue:
        return RedirectResponse(url="/queue", status_code=303)
    if job.execute_kind in runtime._JOB_NAV_SURFACES:
        dest = runtime._job_nav_destination(job)[1]
        # Both land on /library with no other sign the discard happened; a
        # review that was there a second ago is just gone otherwise.
        if was_review and job.execute_kind in ("library", "new_releases"):
            label = ("New releases review" if job.execute_kind == "new_releases"
                     else "Library review")
            dest += "?notice=" + runtime._notice_key(f"{label} discarded.")
    else:
        dest = "/queue" if (was_review or was_pending) else f"/jobs/{job_id}"
    return RedirectResponse(url=dest, status_code=303)
