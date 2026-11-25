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
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from qobuz_librarian import completion, disk_support
from qobuz_librarian import config as cfg
from qobuz_librarian.api import search as qobuz_search
from qobuz_librarian.api.auth import CredentialChanged, NoCredsError, QobuzAccess
from qobuz_librarian.integrations import beets as beets_mod
from qobuz_librarian.integrations import downsample_engine
from qobuz_librarian.library import (
    backup,
    candidate_premise,
    collection_snapshot,
    generation_state,
    library_scan_state,
    missing_types,
    new_releases,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import (
    download_admission,
    download_outcomes,
    flows,
    job_labels,
    job_persistence,
    job_runs,
    owned_paths,
    qobuz_access,
    queue_recovery,
    refusals,
    rendering,
    review_badges,
    review_pages,
    runtime,
    saved_reviews,
    settings_store,
    storage,
    track_downloads,
    write_gate,
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
# Reviews of the library as it stands, which an empty music folder that once
# held albums cannot act on. A restore is what refills one.
_MUSIC_ROOT_REVIEW_KINDS = ("library", "new_releases", "upgrade", "repair",
                            "downsample")
# What the work each review starts needs of the disks it writes to.
_REVIEW_DISK_FEATURES = {
    "library": ("downloads", "picks"),
    "new_releases": ("downloads", "picks"),
    "collection_restore": ("downloads", "picks"),
    "repair": ("downloads", "repair", "picks"),
    "upgrade": ("upgrade",),
    "downsample": ("downsample", "picks"),
}


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str,
                   stale: bool = False, noselection: bool = False, page: int = 1,
                   error: str = "", q: str = "", tab: str = "",
                   waiting: bool = False, history_page: int = 1):
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return RedirectResponse(
                url="/queue?error=" + rendering._notice_key(
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
    if (job.status == job_mgr.JobStatus.AWAITING_REVIEW
            and job.execute_kind in ("upgrade", "downsample")
            and job_mgr.registry.get(job.id) is job):
        loop = asyncio.get_running_loop()
        shown_at = await loop.run_in_executor(
            None, lambda: saved_reviews.review_location(job))
        if shown_at:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(url=shown_at + query, status_code=303)
    review_badge_ack = _review_badge_ack_for(job)
    nav_page, return_href, _return_label = job_labels._job_nav_destination(job)
    if return_href == "/queue/history" and history_page > 1:
        return_href = f"/queue/history?p={history_page}"
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
                job, rendering._recovery_missing),
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
    ctx = {"job": job, "page": nav_page, "job_return_href": return_href,
           "shown_attention": shown_attention,
           "stale": stale, "noselection": noselection,
           "waiting": waiting,
           "error": rendering._notice_text(error),
           "new_release_state": new_release_state,
           "queue_wait": job_labels._queue_wait(job),
           "JobStatus": job_mgr.JobStatus}
    ctx.update(review_pages._review_context(job, page, q, tab))
    return rendering._tr(
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
           "queue_wait": job_labels._queue_wait(job)}
    ctx.update(review_pages._review_context(job, page))
    return rendering._tr(
        request, "_job_body.html", ctx, review_badge_ack=review_badge_ack
    )


@router.get("/jobs/{job_id}/review", response_class=HTMLResponse)
async def job_review_page(request: Request, job_id: str, page: int = 1,
                          q: str = "", tab: str = "", through: bool = False,
                          anchor: str = ""):
    """Review fragment, optionally including earlier loaded pages."""
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
    review_badge_ack = _review_badge_ack_for(job)
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus}
    ctx.update(review_pages._review_context(
        job, page, q, tab, through=through, anchor=anchor))
    return rendering._tr(
        request, "_review_page.html", ctx,
        review_badge_ack=review_badge_ack,
    )


def _build_unapproved_review(job, tab, *, admission_filter=None,
                             discard_ids=()):
    """Keep only the admitted picks (ticked, in the approved tab, passed by
    ``admission_filter``) on ``job`` and move every other candidate but
    ``discard_ids`` to a new parked review. The caller holds ``job._lock`` and
    admits both jobs before publishing either. Returns the unpublished parked
    job, or None, leaving ``job`` unchanged, when there is nothing to move."""
    in_tab = flows.review_tab_filter(tab)
    keep, split = [], []
    discard_ids = set(discard_ids)
    for c in job.candidates:
        if c.get("cid") in discard_ids:
            continue
        selected = in_tab(c) and c.get("selected")
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
    factory = job_runs._RESUME_EXECUTE.get(other.execute_kind)
    if factory is not None:
        other._execute_fn = factory(other, other.execute_args)
    return other


@router.post("/jobs/{job_id}/approve")
async def job_approve(request: Request, job_id: str):
    response = await _approve(request, job_id)
    # A Library review is worked through a tab and a filter at a time, so the
    # page an approval lands on keeps the ones it was pressed on.
    location = response.headers.get("location") or ""
    if location == "/library" or location.startswith("/library?"):
        form = await request.form()
        place = review_pages.library_place(form.get("tab"), form.get("q"))
        if place:
            response.headers["location"] = (
                location + ("&" if "?" in location else "?") + place)
    return response


def _error_redirect(dest, message, suffix=""):
    return RedirectResponse(
        url=dest + "?error=" + rendering._notice_key(message) + suffix,
        status_code=303,
    )


def _review_dest(job):
    # These stay on their single surfaces through the executing phase; every
    # other kind (new-release checks included) keeps using the job page.
    if job.execute_kind in ("library", "repair", "upgrade", "downsample"):
        return f"/{job.execute_kind}"
    return f"/jobs/{job.id}"


def _selected_candidates(job, tab):
    in_tab = flows.review_tab_filter(tab)
    with job._lock:
        candidates = [
            c for c in job.candidates
            if c.get("selected") and in_tab(c)
        ]
        return copy.deepcopy(candidates)


async def _stale_premise_candidates(loop, job, picked):
    """The ticked candidates whose folders changed since the review was
    built. A Library review forgets their artists' scan fingerprints."""
    stale = await loop.run_in_executor(
        None,
        # Admission only: a fresh unshared check runs again before
        # anything is written, so one seal per artist is enough here.
        lambda: candidate_premise.stale_candidate_ids(
            picked, share_artist_captures=True),
    )
    if stale and job.execute_kind == "library":
        await loop.run_in_executor(
            None, lambda: library_scan_state.forget_fingerprints(
                (c.get("payload") or {}).get("_artist_dir")
                or c.get("artist")
                for c in picked
                if c.get("cid") in stale))
    return stale


async def _all_stale_redirect(loop, job, dest, stale, picked):
    folders = await loop.run_in_executor(
        None, lambda: _library_folders_changed(job, stale.counts, picked))
    if folders:
        return _error_redirect(dest, folders, "&offer_refresh=1")
    stale_message = await loop.run_in_executor(
        None, lambda: _all_stale_message_for(job, stale.counts, picked))
    return _error_redirect(dest, stale_message)


def _downsample_originals_answer(request, job, form, dest, picked,
                                 selection_token):
    """What this approval does with the originals, as (response, keep,
    choice to save). A response ends the approval there."""
    # current() includes a saved value waiting for another lane to
    # finish. Bind that value to this approval below.
    choice = rendering._downsample_originals_choice()
    rendered_choice = (
        form.get("downsample_policy") or ""
    ).strip().lower()
    current_choice = choice if choice in ("keep", "delete") else ""
    if (
        rendered_choice not in ("", "keep", "delete")
        or rendered_choice != current_choice
    ):
        return _error_redirect(
            dest,
            "The keep-or-delete setting changed after this review "
            "was shown. No music files were changed; review the "
            "updated warning and approve again."
        ), None, ""
    choice_to_save = ""
    if choice not in ("keep", "delete"):
        choice = (form.get("keep_choice") or "").strip().lower()
        if choice in ("keep", "delete"):
            choice_to_save = choice
        else:
            return rendering._tr(request, "downsample_keep_choice.html", {
                "job": job, "page": "downsample",
                "picked": len(picked),
                "picked_albums": flows.named_albums(picked),
                "downsample_originals_choice": current_choice,
                "review_selection_token": selection_token,
                "backup_retention_days":
                    cfg.UPGRADE_BACKUP_RETENTION_DAYS}), None, ""
    return None, choice == "keep", choice_to_save


def _recheck_selection(job, tab, selection_token, selected_ids, stale_premise,
                       credentials):
    """Check the ticked candidates again under the approval locks. Returns
    the status that refuses the approval, or None, and the ticked candidates
    whose folders changed."""
    def all_stale(candidates):
        return {c.get("cid") for c in candidates} <= stale_premise

    current_selected = _selected_candidates(job, tab)
    if (selection_token is not None
            and selection_token != flows.review_selection_token(job, tab)):
        return "review_changed", stale_premise
    if {c.get("cid") for c in current_selected} != selected_ids:
        return "review_changed", stale_premise
    if job.execute_kind in _PREMISE_REVIEW_KINDS:
        # Still admission: the worker's unshared check runs before
        # anything is written, so one seal per artist is enough here.
        stale_premise = candidate_premise.stale_candidate_ids(
            current_selected, share_artist_captures=True)
        if all_stale(current_selected):
            return "all_candidates_stale", stale_premise
    if (credentials is not None
            and not qobuz_access._credential_generation_is_active(
                credentials.generation)):
        return "credential_changed", stale_premise
    if (job.execute_kind in ("upgrade", "downsample")
            and job.status == job_mgr.JobStatus.AWAITING_REVIEW):
        synced_job = saved_reviews._sync_saved_review_before_approve(job)
        if synced_job is not job:
            return "review_changed", stale_premise
        current_selected = _selected_candidates(job, tab)
        if not current_selected:
            return job_mgr.APPROVAL_NO_SELECTION, stale_premise
        if job.execute_kind in _PREMISE_REVIEW_KINDS:
            stale_premise = candidate_premise.stale_candidate_ids(
                current_selected, share_artist_captures=True)
            if all_stale(current_selected):
                return "all_candidates_stale", stale_premise
    return None, stale_premise


def _admission_filter(job, tab, stale_owned, stale_premise,
                      already_downloading):
    """The filter an approval admits ticked candidates through. An album a
    running download already covers is added to ``already_downloading``."""
    decisions = {}
    in_approved_tab = flows.review_tab_filter(tab)

    def selection_filter(candidate):
        key = candidate.get("cid")
        if not isinstance(key, str) or not key:
            return False
        if key in decisions:
            return decisions[key]
        if (key in stale_owned or key in stale_premise
                or not in_approved_tab(candidate)):
            decisions[key] = False
            return False
        if job.execute_kind in (
                "library", "new_releases", "collection_restore"):
            album_id = completion.normalise_album_id(
                (candidate.get("payload") or {}).get("album_id")
            )
            if album_id is None:
                decisions[key] = False
                return False
            admitted = download_admission._duplicate_download_job(album_id) is None
            if not admitted:
                already_downloading.append(candidate)
        else:
            admitted = True
        decisions[key] = admitted
        return admitted

    return selection_filter


def _unapproved_review_builder(tab, selection_filter, discard_ids):
    def split_review(review_job):
        remnant = _build_unapproved_review(
            review_job,
            tab,
            admission_filter=selection_filter,
            discard_ids=discard_ids,
        )
        if remnant is not None:
            remnant.execute_args.pop(
                "_credential_generation", None)
        # Whole review ticked → retire the worked-through baseline
        # after success instead of rebuilding its old candidates.
        if review_job.execute_kind == "library":
            review_job._consumed_whole_review = remnant is None
        return remnant

    return split_review


def _approve_with_run_args(job, *, credentials, downsample_keep_originals,
                           downsample_choice_to_save,
                           migration_allow_low_space, split_review,
                           selection_filter):
    """Put this approval's answers into the job's run arguments and approve
    it. The old arguments go back unless the approval went through."""
    previous_migration_args = None
    previous_migration_execute = None
    previous_downsample_args = None
    previous_qobuz_args = None
    previous_qobuz_execute = None
    downsample_args_changed = False
    qobuz_args_changed = False
    if credentials is not None:
        with job._lock:
            if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
                previous_qobuz_args = job.execute_args
                previous_qobuz_execute = job._execute_fn
                job.execute_args = {
                    **(job.execute_args or {}),
                    "_credential_generation": credentials.generation,
                }
                factory = job_runs._RESUME_EXECUTE.get(job.execute_kind)
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
                job._execute_fn = job_runs._resume_downsample(
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
        execute_args["allow_low_space"] = migration_allow_low_space
        execute_fn = job_runs._resume_migration(job, execute_args)
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
                job._execute_fn = job_runs._resume_downsample(
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


_APPROVAL_REFUSALS = {
    "review_changed":
        "That review changed while it was being approved. Check "
        "the selections and try again.",
    "downsample_policy_failed":
        "Couldn't save the keep-or-delete choice. No music files "
        "were changed; check the data folder and try again.",
    "sync_failed":
        "The refreshed review could not be saved. Check the data "
        "folder and try again.",
}


def _approved_redirect(dest, approved, skip_q, already_downloading,
                       stale_premise, picked):
    running_q = ""
    if already_downloading:
        names = flows.named_albums(already_downloading)
        running_q = "&error=" + rendering._notice_key(
            f"{names} {'is' if len(already_downloading) == 1 else 'are'} "
            "already downloading, so "
            f"{'it was' if len(already_downloading) == 1 else 'they were'} "
            "not started again.")
    if approved is job_mgr.APPROVAL_NO_SELECTION:
        if running_q:
            return RedirectResponse(
                url=f"{dest}?{running_q[1:]}{skip_q}", status_code=303)
        return RedirectResponse(url=f"{dest}?noselection=1{skip_q}",
                                status_code=303)
    flag = "approved=1" if approved else "stale=1"
    local_stale = len(stale_premise)
    local_stale_q = ""
    if local_stale and approved:
        skipped = [c for c in picked if c.get("cid") in stale_premise]
        skipped_names = flows.named_albums(skipped)
        folders = _changed_artist_folders(skipped)
        local_stale_q = "&error=" + rendering._notice_key(
            f"Started the rest. {folders[:1].upper()}{folders[1:]} changed on "
            f"disk after this review was built, so {skipped_names} "
            f"{'was' if local_stale == 1 else 'were'} skipped and left ticked."
            if folders else
            f"Started the rest. {skipped_names} changed on disk after this "
            f"review was built, so "
            f"{'it was' if local_stale == 1 else 'they were'} skipped and "
            "left ticked."
        )
        # One answer, not two: the green "Queued" banner would say what this
        # sentence opens with. Only when this click is the one that started
        # the work; if another click got there first, that is what the page
        # has to say instead.
        flag = ""
    return RedirectResponse(
        url=f"{dest}?{flag}{skip_q}{local_stale_q or running_q}".replace(
            "?&", "?"),
        status_code=303,
    )


async def _approve(request: Request, job_id: str):
    job = job_mgr.registry.get(job_id)
    downloads = write_gate._review_downloads(job)
    busy = refusals._lock_busy_response(request, downloads=downloads)
    if busy is not None:
        return busy
    if not job:
        return _error_redirect("/queue", "That review is no longer open.")
    # A parked review can outlive its feature: Upgrade can be switched off
    # after its review parks, and the downsample engine can vanish across a
    # restart.
    if job.execute_kind == "upgrade" and not runtime._upgrade_available():
        return saved_reviews._upgrade_unavailable_response()
    if job.execute_kind == "downsample":
        if not downsample_engine.HAVE_DOWNSAMPLE:
            return RedirectResponse(url="/downsample", status_code=303)
    dest = _review_dest(job)
    if (job.status == job_mgr.JobStatus.AWAITING_REVIEW
            and job.execute_kind == "upgrade"
            and (job.execute_args or {}).get("quality_signature")
            != saved_reviews._effective_upgrade_quality_signature()):
        return _error_redirect(
            dest,
            "Download quality changed since this Upgrade review was "
            "built. Run a Library refresh before approving it.")
    # A library review approves per tab: the button acts on the tab the user
    # is looking at, and only that tab.
    form = await request.form()
    selection_token = form.get("selection_token")
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
        return _error_redirect(
            dest, f"Confirm the low-space risk before approving this {action}.")
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
        in_tab = flows.review_tab_filter(tab)
        with job._lock:
            selected_candidate_ids = {
                c.get("cid") for c in job.candidates
                if c.get("selected") and in_tab(c)
            }
        with job._lock:
            selected_candidate_snapshot = copy.deepcopy([
                c for c in job.candidates
                if c.get("cid") in selected_candidate_ids
            ])
        if not selected_candidate_ids:
            return RedirectResponse(url=f"{dest}?noselection=1",
                                    status_code=303)
        disk_refusal = await loop.run_in_executor(
            None, lambda: disk_support.refusal(
                *_REVIEW_DISK_FEATURES.get(job.execute_kind, ())))
        if disk_refusal:
            return _error_redirect(dest, disk_refusal)
        if job.execute_kind in _MUSIC_ROOT_REVIEW_KINDS:
            # Said before the folder checks below, which would call every
            # album changed and advise a refresh that cannot run.
            root_state, recorded = await loop.run_in_executor(
                None, collection_snapshot.music_root_write_state)
            if root_state != "ready":
                return _error_redirect(
                    dest, storage._music_write_target_message(
                        root_state, recorded, diagnostic=True))
        if job.execute_kind in _PREMISE_REVIEW_KINDS:
            stale_premise_candidate_ids = await _stale_premise_candidates(
                loop, job, selected_candidate_snapshot)
            if selected_candidate_ids <= stale_premise_candidate_ids:
                return await _all_stale_redirect(
                    loop, job, dest, stale_premise_candidate_ids,
                    selected_candidate_snapshot)
        # Only now that the run is going to happen: the keep-vs-delete answer is
        # a standing policy saved to Settings, so asking and saving it before
        # anything checks that an album is ticked lets a no-op approve change
        # what every future downsample does with your originals.
        if job.execute_kind == "downsample":
            (response, downsample_keep_originals,
             downsample_choice_to_save) = _downsample_originals_answer(
                request, job, form, dest, selected_candidate_snapshot,
                selection_token)
            if response is not None:
                return response

    authorized_credentials = None
    stale_owned_candidate_ids = set()
    if (job.status == job_mgr.JobStatus.AWAITING_REVIEW
            and job.execute_kind in _QOBUZ_REVIEW_KINDS):
        try:
            authorized_credentials = await qobuz_access._authorize_qobuz_for_web(
                QobuzAccess.DOWNLOAD_ACTION
            )
        except qobuz_access._QOBUZ_ACTION_ERRORS as exc:
            return _error_redirect(
                dest, job_mgr.qobuz_action_error_message(exc))
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
    # does not carry every checkbox in the form), so approve runs against
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
            download_admission._DOWNLOAD_SUBMIT_LOCK,
            qobuz_access._CREDENTIAL_LOCK,
            job._review_action_lock,
            missing_types._lock,
        ):
            if (write_gate._web_downloads_paused() if downloads
                    else write_gate._web_writes_paused()):
                return "paused"
            if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
                return False
            refusal, stale_premise_candidate_ids = _recheck_selection(
                job, tab, selection_token, selected_candidate_ids,
                stale_premise_candidate_ids, authorized_credentials)
            if refusal is not None:
                return refusal
            # A review action consumes only the ticked picks: park everything
            # else (plus the inactive Library tab) as its own living review so
            # one partial batch cannot eat unreviewed candidates.
            already_downloading.clear()
            selection_filter = _admission_filter(
                job, tab, stale_owned_candidate_ids,
                stale_premise_candidate_ids, already_downloading)
            split_review = None
            if ((job.execute_kind in _PREMISE_REVIEW_KINDS
                    or stale_premise_candidate_ids)
                    and job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                split_review = _unapproved_review_builder(
                    tab, selection_filter, stale_owned_candidate_ids)
            return _approve_with_run_args(
                job,
                credentials=authorized_credentials,
                downsample_keep_originals=downsample_keep_originals,
                downsample_choice_to_save=downsample_choice_to_save,
                migration_allow_low_space=bool(
                    migration_low_space_required
                    and migration_low_space_accepted),
                split_review=split_review,
                selection_filter=selection_filter,
            )

    approved = await loop.run_in_executor(None, _split_and_approve)
    if approved == "all_candidates_stale":
        return await _all_stale_redirect(
            loop, job, dest, stale_premise_candidate_ids,
            selected_candidate_snapshot)
    if (isinstance(approved, tuple)
            and len(approved) == 2
            and approved[0] == "candidate_stale"):
        return _error_redirect(dest, approved[1])
    if isinstance(approved, str) and approved in _APPROVAL_REFUSALS:
        return _error_redirect(dest, _APPROVAL_REFUSALS[approved])
    if approved == "credential_changed":
        return _error_redirect(
            dest, job_mgr.qobuz_action_error_message(CredentialChanged()))
    if approved == "paused":
        busy = refusals._lock_busy_response(request, downloads=downloads)
        if busy is not None:
            return busy
        return RedirectResponse(url=dest, status_code=303)
    if approved is None:
        return _error_redirect(dest, job_mgr.JOB_ADMISSION_ERROR, _skip_q)
    return _approved_redirect(
        dest, approved, _skip_q, already_downloading,
        stale_premise_candidate_ids, selected_candidate_snapshot)


# Kinds whose review screen has server-backed per-candidate selection.
_SELECTABLE_KINDS = review_pages._TRIAGE_KINDS + (
    "repair", "migration", "collection_restore",
)


def _rebuild_results_hint(execute_kind):
    """Where the results behind a review are rebuilt.

    A message telling the user to refresh has to name the control that does,
    or it sends them back to press the same button for the same failure.
    """
    if execute_kind == "downsample":
        return ("Refresh results with the button beside the Downsample title, "
                "then try again.")
    if execute_kind in ("library", "upgrade"):
        return ("Refresh the Library, which rebuilds these results, then try "
                "again.")
    if execute_kind == "repair":
        return ("Start a new scan with the button beside the Repair title, "
                "then try again.")
    if execute_kind == "new_releases":
        return "Run Check new releases on the Library page, then try again."
    if execute_kind == "collection_restore":
        return ("Upload the backup file again from Settings, then try "
                "again.")
    return "Rebuild these results before trying again."


def _changed_folder_names(candidates):
    """The artist folders behind ``candidates`` when every one is a missing
    album, whose files are its artist folder's, or []."""
    if not candidates or any(candidate_premise.expected_kind(c) != "missing"
                             for c in candidates):
        return []
    names = sorted({Path(candidate_premise.expected_path(c)).name
                    for c in candidates})
    return [] if "" in names else names


def _library_folders_changed(job, counts, candidates):
    """The Library refusal when nothing but the artist folders changed, which
    the page pairs with a Refresh button, or ""."""
    changed = counts.get("changed", 0)
    if (job.execute_kind != "library" or changed != len(candidates)
            or changed != sum(counts.values())):
        return ""
    names = _changed_folder_names(candidates)
    if len(names) == 1:
        return f"The {names[0]} folder changed since the last scan."
    if names:
        return f"{len(names)} artist folders changed since the last scan."
    return ""


def _changed_artist_folders(candidates):
    """"the Chiodos folder" when every one of ``candidates`` is a missing
    album, whose files are its artist folder's, or ""."""
    names = _changed_folder_names(candidates)
    if not names:
        return ""
    if len(names) == 1:
        return f"the {names[0]} folder"
    if len(names) <= 3:
        return f"the {', '.join(names[:-1])} and {names[-1]} folders"
    return f"{len(names)} artist folders"


def _all_candidates_stale_message(execute_kind, counts, candidates=()):
    if counts.get("unsupported", 0):
        refusal = disk_support.refusal("picks")
        if refusal:
            return refusal
    causes = []
    changed = counts.get("changed", 0)
    folders = (_changed_artist_folders(candidates)
               if changed == len(candidates) else "")
    if folders:
        causes.append(f"{folders[:1].upper()}{folders[1:]} changed on disk "
                      "after this review was built")
    elif changed:
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
    if counts.get("link", 0):
        links = []
        for path in dict.fromkeys(
                candidate_premise.expected_path(c) for c in candidates):
            link = backup.first_symlink(path) if path else None
            if link:
                links.append(f"{link} in {Path(path).name}")
        if links:
            one = len(links) == 1
            named = "; ".join(links[:3])
            if len(links) > 3:
                named += f" and {len(links) - 3} more"
            causes.append(
                f"{named} {'is a symbolic link' if one else 'are symbolic links'}, "
                "which can't be checked or backed up; replace "
                f"{'it' if one else 'them'} with a copy of the file")
    return ", ".join(causes) + ". " + _rebuild_results_hint(execute_kind)


def _all_stale_message_for(job, counts, candidates=()):
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
            return ("The music folder is not the one this review was built "
                    "against. Check that your library is mounted, then try "
                    "again.")
    return _all_candidates_stale_message(job.execute_kind, counts, candidates)


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
    payload = review_pages._selection_tallies(job)
    payload["reclaimable_label"] = (format_size(payload["reclaimable"])
                                    if payload["reclaimable"] else "")
    if persist_failed:
        payload["persist_failed"] = True
    return payload


def _filtered_selection_payload(job, query, tab):
    """Counts for selection controls scoped to the active filter."""
    q = (query or "").strip().lower()
    in_tab = flows.review_tab_filter(tab)
    with job._lock:
        shown = [bool(c.get("selected")) for c in job.candidates
                 if in_tab(c) and (not q or flows.candidate_matches_query(c, q))]
    selected = sum(shown)
    return {
        "filtered_total": len(shown),
        "filtered_selected": selected,
        "filtered_rest": len(shown) - selected,
    }


def _set_all_selected_with_membership(job, on, cids):
    """Apply a bulk choice and return the candidate IDs it covered."""
    wanted = set(cids) if cids is not None else None
    accepted_cids = []
    changed = 0
    with job._review_action_lock, job._lock:
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
    q = (form.get("q") or "").strip()
    tab = (form.get("tab") or "").strip()

    # Off the request loop: every count is a pass over the whole review.
    def _tick():
        changed = job.set_selected(cid, on)
        if changed is None:
            return None, None
        payload = _selection_payload(job)
        if q:
            payload.update(_filtered_selection_payload(job, q, tab))
        return changed, payload

    loop = asyncio.get_running_loop()
    changed, payload = await loop.run_in_executor(None, _tick)
    if changed is None:
        return JSONResponse(
            {"error": "review is no longer awaiting selection"},
            status_code=409,
        )
    if changed:
        # Coalesced save: a save encodes the whole review to find what
        # changed, so the tap must not wait for (or even schedule) one.
        job_mgr.persist_soon(job)
        job.notify_review_changed(_review_origin(request))
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

    # Off the request loop, like the save: both pass over the whole review.
    def _choose():
        chosen = cids
        if (scope == "artist" or (scope == "page" and page_artists)
                or (cids is None and (tab_scoped or q))):
            in_tab = flows.review_tab_filter(tab if tab_scoped else "")
            with job._lock:
                chosen = [c["cid"] for c in job.candidates
                          if (scope != "artist" or (c.get("artist") or "") == artist)
                          and (scope != "page"
                               or not page_artists
                               or (c.get("artist") or "") in page_artists)
                          and in_tab(c)
                          and (not q or flows.candidate_matches_query(c, q))]
        return _set_all_selected_with_membership(job, on, chosen)

    def _counts():
        payload = _selection_payload(job, persist_failed=persist_failed)
        if q:
            payload.update(_filtered_selection_payload(job, q, tab))
        return payload

    persist_failed = False
    loop = asyncio.get_running_loop()
    changed, accepted_cids = await loop.run_in_executor(None, _choose)
    if changed is None:
        return JSONResponse(
            {"error": "review is no longer awaiting selection"},
            status_code=409,
        )
    if changed:
        # Unlike a single tap, a bulk choice is one infrequent operation whose
        # success needs to mean its complete result is durable.
        saved = await loop.run_in_executor(
            None, lambda: job_persistence.persist(job))
        if job._retired:
            return JSONResponse(
                {"error": "review is no longer awaiting selection"},
                status_code=409,
            )
        persist_failed = not saved
        job.notify_review_changed(_review_origin(request))
    payload = await loop.run_in_executor(None, _counts)
    payload["accepted_cids"] = accepted_cids
    return JSONResponse(payload)


@router.post("/jobs/{job_id}/missing-types")
async def job_missing_types(request: Request, job_id: str):
    """Switch a release type in or out of Missing Albums, or bonus versions
    in or out of Gap Fill. The choice is saved for every Library review and
    CLI run. Nothing is scanned again: the review holds every row and lists
    what is switched on."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind != "library":
        return JSONResponse({"error": "not found"}, status_code=404)
    form = await request.form()
    release_type = (form.get("type") or "").strip()
    if release_type not in missing_types.SWITCHES:
        return JSONResponse({"error": "unknown release type"}, status_code=400)
    on = (form.get("on") or "").strip().lower() in ("1", "true", "on", "yes")
    loop = asyncio.get_running_loop()
    switched = await loop.run_in_executor(
        None, lambda: missing_types.switch(release_type, on))
    if switched is None:
        return JSONResponse(
            {"error": "The switch could not be saved. Check the data folder."},
            status_code=503)
    job.notify_review_changed(_review_origin(request))
    return JSONResponse(
        await loop.run_in_executor(None, lambda: _selection_payload(job)))


@router.get("/jobs/{job_id}/review-group-items", response_class=HTMLResponse)
async def job_review_group_items(request: Request, job_id: str,
                                 artist: str = "", tab: str = "", q: str = ""):
    """Render one artist's current rows when a collapsed group is opened."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return HTMLResponse("", status_code=404)
    if job.execute_kind != "library" or tab not in ("missing", "gaps"):
        tab = ""
    groups = review_pages._review_artist_groups(
        job, query=q, tab=tab, artist=artist)
    items = next((rows for name, rows in groups if name == artist), [])
    return rendering._tr(request, "_review_group_items.html", {
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
    if not album_dir:
        return Response(status_code=404)
    return rendering._library_art_response(album_dir)


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
        # The filter narrows what the button sits next to, so it has to narrow
        # what the button takes; without it, one tap on a filtered row dismisses
        # every album by that artist, including the ones the filter is hiding.
        q = (form.get("q") or "").strip()
        # Selection is server-backed, so hide keeps this artist's ticked albums
        # and drops the rest, with no form keep-set, which under pagination would
        # only carry the visible page and clobber other pages' selections.
        scope = review_pages._hide_scope(job.execute_kind)

        def _dismiss():
            with saved_reviews._SAVED_REVIEW_LOCK:
                n = flows.dismiss_albums(
                    job, [artist], scope=scope, tab=tab, query=q,
                    selection_token=form.get("selection_token"))
            if n is None or n is False:
                return n, None, [], {}
            # Dismissing the last album completes the review, so drop
            # AWAITING_REVIEW: the dashboard "new releases" banner clears and
            # this page stops showing an empty "awaiting review".
            finalized = job_mgr.finalize_review_if_empty(job)
            if finalized is not False:
                return n, finalized, [], {}
            # Re-render what the filter shows, not the whole artist, so the
            # group that swaps in matches the list the user is looking at.
            groups = review_pages._review_artist_groups(
                job, query=q, tab=tab, artist=artist)
            remaining = next((rows for name, rows in groups if name == artist), [])
            # Counts include pages that have not been loaded.
            counts = _selection_payload(job)
            counts["hidden_total"] = hidden_mod.count(scope)
            if q:
                counts.update(_filtered_selection_payload(job, q, tab))
            return n, finalized, remaining, counts

        loop = asyncio.get_running_loop()
        try:
            n, finalized, remaining, counts = await loop.run_in_executor(
                None, _dismiss)
        except OSError as e:
            # Nothing changed server-side; the non-2xx keeps htmx from
            # swapping the rows away and the error toast reads this body.
            return HTMLResponse(str(e), status_code=500)
        if n is False:
            return HTMLResponse(
                "The dismissal could not be saved to the Library review. "
                "Check the data folder and try again.",
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
        if finalized is None:
            return HTMLResponse(
                "The dismissal was saved, but the finished review could not "
                "be recorded. Check the data folder and reload.",
                status_code=503,
            )
        if finalized:
            # HX-Refresh reloads to the finished view.
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        if remaining:
            resp = rendering._tr(request, "_review_group.html",
                       {"job": job, "artist": artist, "items": remaining,
                        "triage": True, "open": True, "review_tab": tab,
                        "review_query": q})
        else:
            resp = HTMLResponse("")  # whole artist hidden, outerHTML drops it
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
    # An active filter narrows the dismissal to the rows it shows, same
    # what-you-see-is-what-you-act-on rule as select-all.
    q = (form.get("q") or "").strip().lower()
    in_tab = flows.review_tab_filter(tab)

    # Offload: this can touch the whole review (a hidden-store write and a
    # persist), which would block the event loop and stall every SSE stream
    # for a large scan.
    def _dismiss_all():
        with job._lock:
            artists = {c.get("artist") or "" for c in job.candidates
                       if not c.get("selected") and in_tab(c)
                       and (not q or flows.candidate_matches_query(c, q))}
        with saved_reviews._SAVED_REVIEW_LOCK:
            hidden = flows.dismiss_albums(
                job, artists, scope=scope, tab=tab, query=q,
                selection_token=form.get("selection_token"))
        if hidden is None or hidden is False:
            return hidden, {}
        payload = _selection_payload(job)
        payload["hidden"] = hidden
        payload["hidden_total"] = hidden_mod.count(scope)
        if q:
            payload.update(_filtered_selection_payload(job, q, tab))
        finalized = job_mgr.finalize_review_if_empty(job)
        payload["review_done"] = finalized is True
        if finalized is None:
            payload["finalize_failed"] = True
        return hidden, payload

    loop = asyncio.get_running_loop()
    try:
        hidden_count, payload = await loop.run_in_executor(None, _dismiss_all)
    except OSError as e:
        # The store write failed before anything left the review.
        return JSONResponse({"error": str(e), "hidden": 0}, status_code=500)
    if hidden_count is None:
        return JSONResponse(
            {
                "error": "That review changed before dismissal completed.",
                "hidden": 0,
            },
            status_code=409,
        )
    if hidden_count is False:
        return JSONResponse(
            {
                "error": "The Library review could not be saved.",
                "hidden": 0,
            },
            status_code=503,
        )
    if hidden_count:
        job.notify_review_changed(_review_origin(request))
    return JSONResponse(payload)


_RETRY_RETURN_KEYS = ("p", "jp", "attention")


def _retry_return_url(raw: object, **extra: str) -> str | None:
    """The History or Queue page a Retry was clicked on, or None for anything
    else.

    The page sends its own address with the click so the outcome arrives where
    the user is already looking instead of dragging them into the job card.
    Only those two surfaces are honoured and only History's own paging keys
    ride along, so a forged field cannot aim the redirect somewhere else.
    """
    path, _, query = (raw if isinstance(raw, str) else "").partition("?")
    if path == "/queue":
        # The retried job shows in the list itself; only a refusal needs words.
        error = extra.get("error")
        return path + ("?" + urllib.parse.urlencode({"error": error})
                       if error else "")
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
            error = rendering._notice_key(error)
        dest = _retry_return_url(return_to, started=started, error=error)
        if dest is None:
            dest = (f"/jobs/{started}" if started
                    else f"/jobs/{urllib.parse.quote(job_id)}?error={error}")
        return RedirectResponse(url=dest, status_code=303)

    job = job_mgr.registry.get(job_id) or job_mgr.load_historical_job(job_id)
    if not job:
        return _land(error="That job is no longer in History.")
    retried_as = (job.execute_args or {}).get("retried_as")
    if retried_as:
        return _land(started=str(retried_as))
    if ((job.status not in (job_mgr.JobStatus.FAILED,
                            job_mgr.JobStatus.CANCELED)
         and not job.tracks_missing)
            or not job.album_id
            or (job.execute_args or {}).get("retry_disabled") == "terminal"):
        return _land(error="Nothing to retry for that job.")
    if job.execute_args_unreadable:
        return _land(
            error="The saved details needed to retry that job couldn't be "
                  "read. Start the download again from Search or Library.",
        )
    if (job.execute_args or {}).get("retry_disabled") == "lossy":
        return _land(
            error="Qobuz cannot supply the missing tracks, so this album "
                  "needs another source and cannot be retried here.",
        )
    if (job.execute_args or {}).get("retry_disabled") == "backup":
        return _land(
            error="This album has a kept backup. Review it under "
                  "Settings → Diagnostics before starting the album again.",
        )
    if (
        getattr(job, "_preserve_persisted_single", False) is True
        or getattr(job, "_single_undo_unavailable", False) is True
    ):
        return refusals._durable_recovery_response(
            request,
            "This track's saved recovery state is uncertain, so Retry is "
            "paused. No download was started. Restart Qobuz Librarian.",
        )
    try:
        credentials = await qobuz_access._authorize_qobuz_for_web(
            QobuzAccess.DOWNLOAD_ACTION
        )
    except qobuz_access._QOBUZ_ACTION_ERRORS as exc:
        message = job_mgr.qobuz_action_error_message(exc)
        return _land(error=message)

    completion_acknowledged = queue_recovery._durable_completion_status(job)
    if completion_acknowledged is None:
        return refusals._durable_recovery_response(
            request,
            "The saved completion record could not be checked. Check the "
            "data-folder permissions, then restart Qobuz Librarian.",
        )
    busy = refusals._lock_busy_response(request, downloads=True)
    if busy is not None:
        return busy
    if completion_acknowledged:
        if not queue_recovery._reconcile_acknowledged_job(job):
            return refusals._durable_recovery_response(
                request,
                "The completed download could not be saved to History. No "
                "download was started. Check the data-folder permissions, "
                "then restart Qobuz Librarian.",
            )
        return _land(started=job.id)
    album_id = job.album_id
    retry_as_new = bool((job.execute_args or {}).get("new_edition"))
    retry_track_id = str((job.single or {}).get("track_id") or "")
    duplicate = download_admission._duplicate_download_job(
        album_id, retry_track_id, retry_as_new,
    )
    if duplicate:
        return _land(started=duplicate.id)
    try:
        loop = asyncio.get_running_loop()
        token = credentials.token
        album = await qobuz_access._qobuz_call(
            qobuz_search.get_album, album_id, token)
        same_edition_complete = bool(
            retry_as_new
            and await loop.run_in_executor(
                None, lambda: download_admission._same_edition_is_complete(album)
            )
        )
        # Re-check under the submit lock.
        with download_admission._DOWNLOAD_SUBMIT_LOCK, qobuz_access._CREDENTIAL_LOCK:
            if not qobuz_access._credential_generation_is_active(credentials.generation):
                message = job_mgr.qobuz_action_error_message(CredentialChanged())
                return _land(error=message)
            duplicate = download_admission._duplicate_download_job(
                album_id, retry_track_id, retry_as_new,
            )
            if duplicate:
                return _land(started=duplicate.id)
            # set_mode could have handed the lock to the terminal during the
            # get_album await above; re-check inside the submit lock (as
            # queue_download does) so a retry can't start a job after the CLI
            # handoff.
            busy = refusals._lock_busy_response(request, downloads=True)
            if busy is not None:
                return busy
            if same_edition_complete:
                return _land(
                    error="This edition is already in your library. "
                          "Nothing to retry.",
                )
            title = album.get("title") or job.title or "?"
            artist = (album.get("artist") or {}).get("name") or job.artist or "?"
            # A failed single-track download carries job.album_id (so Retry shows up),
            # but _make_download_run would download the whole album. Rebuild it as
            # the same one-track run instead.
            single = job.single
            track = None
            if single and single.get("track_id"):
                tid = str(single.get("track_id"))
                track = next(
                    (t for t in (album.get("tracks") or {}).get("items") or []
                     if str(t.get("id")) == tid), None)
            if track is not None:
                run = track_downloads._make_single_track_run(album, track, token)
            elif single and single.get("track_id"):
                # The original was a single-track download but that track is no
                # longer on Qobuz, so do NOT silently re-download the whole album.
                return _land(
                    error="That track is no longer on Qobuz. Nothing to retry.")
            else:
                # Carry the "get this edition too" override across the retry,
                # without it the rebuilt run sees the album as already owned
                # and skips the download the user explicitly asked for.
                run = job_runs._make_download_run(
                    album,
                    token,
                    treat_as_new=retry_as_new,
                )
            edition = str(
                ((track.get("version") if track is not None else None)
                 or album.get("version") or job.edition or "")
            ).strip()
            new_job = job_mgr.Job(
                title=(track.get("title") or title)
                if track is not None else title,
                artist=artist,
                album_id=album_id,
                edition=edition,
            )
            if track is not None:
                # Seed the same two keys /download does at submit and let
                # the run fill in the rest. The old job's Undo record is
                # not copied: this job may download nothing (the "you already
                # have this" early return never touches j.single).
                new_job.single = {
                    "album_id": album_id,
                    "track_id": str(single.get("track_id")),
                }
            elif retry_as_new:
                new_job.execute_args = {"new_edition": True}
            if cover := flows._album_cover(album):
                new_job.execute_args["cover"] = cover
            if job_mgr.submit(new_job, run) is None:
                return refusals._job_admission_response(request)
            # The retry is the answer to the failure, so the old row stops
            # offering Retry and holding the Queue warning dot.
            if job_persistence.mark_retried(job.id, new_job.id):
                with job._lock:
                    job.execute_args = {
                        **(job.execute_args or {}),
                        "retry_disabled": "retried",
                        "retried_as": new_job.id,
                    }
            attention = job.attention
            if attention and attention != "recovery" and (
                job_persistence.acknowledge_attention(job.id, attention)
            ):
                with job._lock:
                    if job.attention == attention:
                        job.attention = ""
        return _land(started=new_job.id)
    except NoCredsError as exc:
        message = job_mgr.qobuz_action_error_message(exc)
        return _land(error=message)
    except Exception as exc:
        _log.warning("couldn't prepare retry for job %s", job.id, exc_info=True)
        message = download_outcomes._download_error_message(
            exc,
            "Couldn't prepare this retry. Try again.",
        )
        return _land(error=message)


def _undone_summary(single, outcome):
    if outcome.get("single_kept"):
        return (f"Removed “{single.get('title')}”. Other tracks you took "
                "from this album stay.")
    return f"Removed “{single.get('title')}”."


def _undo_root(single):
    """The folder a track grab's Undo record is relative to, or None when
    the music folder has moved since."""
    owned_root = single.get("owned_root")
    if owned_root is not None:
        current_root = os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
        if (
            not isinstance(owned_root, str)
            or os.path.abspath(owned_root) != current_root
        ):
            return None
        return Path(current_root)
    return Path(single["dir"]) if isinstance(single.get("dir"), str) else None


def _holds_audio(folder, but=None):
    try:
        with os.scandir(folder) as entries:
            return any(
                entry.name != but
                and entry.name.lower().endswith(cfg.AUDIO_EXTS)
                for entry in entries)
    except OSError:
        return True


def _keep_shared_artwork(root, single):
    """Leave the cover a grab brought while another track in its folder
    still uses it; the album's last Undo removes it."""
    owned = single.get("owned_path")
    if not isinstance(owned, dict) or not isinstance(owned.get("relative"), str):
        return
    companions = owned.get("companions") or []
    artwork = [
        c for c in companions
        if isinstance(c, dict) and c.get("kind") == "artwork"
        and "deletion" not in c
    ]
    track = root / owned["relative"]
    if not artwork or not _holds_audio(track.parent, but=track.name):
        return
    rest = [c for c in companions if c is not artwork[0]]
    owned = {key: value for key, value in owned.items() if key != "companions"}
    if rest:
        owned["companions"] = rest
    single["owned_path"] = owned
    single["kept_artwork"] = artwork[0]


def _clear_after_earlier_grabs(folder, others):
    """Once a folder holds no track, remove the covers earlier Undos left in
    it and the folders those grabs created, if empty."""
    if _holds_audio(folder):
        return
    for single in others:
        root = _undo_root(single) if single.get("removed") is True else None
        if root is None:
            continue
        kept = single.get("kept_artwork")
        if isinstance(kept, dict):
            owned_paths._unlink_owned_path(root, copy.deepcopy(kept))
        owned = single.get("owned_path")
        cleanup = owned.get("directory_cleanup") if isinstance(owned, dict) else None
        if isinstance(cleanup, dict):
            owned = copy.deepcopy(owned)
            owned["directory_cleanup"]["complete"] = False
            owned_paths._remove_created_folders(root, owned, None)


class _UndoRun:
    """One Undo of a track grab, from its saved record to the page it lands on."""

    def __init__(self, request, job_id):
        self.request = request
        # The single payload is persisted, so Undo keeps working after the job
        # ages out of the registry, and the file checks below already handle a track
        # that vanished in the meantime.
        self.job = job_mgr.registry.get(job_id) or job_mgr.load_historical_job(job_id)
        undo_uncertain = bool(
            self.job
            and (
                getattr(self.job, "_preserve_persisted_single", False) is True
                or getattr(self.job, "_single_undo_unavailable", False) is True
            )
        )
        self.info = (
            dict(self.job.single or {})
            if self.job and not undo_uncertain
            else {}
        )
        self.catalog_cleanup = self.info.get("catalog_cleanup")
        self.cleanup_retry = (
            type(self.catalog_cleanup) is dict
            and self.catalog_cleanup.get("pending") is True
            and isinstance(self.catalog_cleanup.get("path"), str)
            and bool(self.catalog_cleanup["path"])
        )
        self.undo_outcome = {}

    def _refuse(self):
        if (
            not self.job
            or not self.info.get("dir")
            or (self.info.get("removed") and not self.cleanup_retry)
        ):
            if rendering._is_htmx(self.request):
                if self.job:
                    return rendering._tr(self.request, "_job_body.html", {"job": self.job})
                return HTMLResponse("", headers={"HX-Redirect": "/queue"})
            msg = ("That job is no longer in History." if not self.job
                   else "Nothing to undo for that job.")
            return RedirectResponse(
                url="/queue?error=" + rendering._notice_key(msg), status_code=303)
        return None

    def _refresh_after_undo(self):
        artist = self.info.get("artist") or ""
        album = {
            "title": self.info.get("album") or "",
            "artist": {"name": artist},
        }
        try:
            flows._refresh_after_local_album_change(
                album,
                {"dir": self.info.get("dir") or ""},
                fallback_artist=artist,
                token=qobuz_access._get_optional_token(),
                args=flows.build_args(),
            )
        except Exception as exc:
            _log.info(
                "quality state refresh after undo skipped: %s", exc)
        # A Gap Fill row the grab left no longer matches its folder.
        flows.prune_library_review_candidates({"id": self.info.get("album_id")})

    def _clear_deliberate_single_mark(self) -> bool:
        """Whether the exact suppression mark is gone from the saved store."""
        if not self.info.get("marked"):
            return True
        if job_persistence.single_mark_still_used(self.info.get("album_id"), self.job.id):
            self.undo_outcome["single_kept"] = True
            return True
        try:
            hidden_mod.unmark_single(
                self.info.get("artist") or "",
                self.info.get("album") or "",
                year=self.info.get("year"),
                album_id=self.info.get("album_id"),
            )
            store = hidden_mod.load()
            return not hidden_mod.is_single(
                self.info.get("artist") or "",
                self.info.get("album") or "",
                store,
                year=self.info.get("year"),
                album_id=self.info.get("album_id"),
            )
        except (OSError, TypeError, ValueError):
            return False

    def _reverse(self):
        if self.cleanup_retry:
            catalog_result = beets_mod.forget_beets_entries(
                [Path(self.catalog_cleanup["path"])]
            )
            self.undo_outcome["single_mark_complete"] = (
                self._clear_deliberate_single_mark()
            )
            return None, catalog_result
        d = _undo_root(self.info)
        if d is None:
            return None, None
        _keep_shared_artwork(d, self.info)
        # Every intent and state/identity refresh reaches the durable job row
        # while the direct-operation lock still excludes another library
        # writer.
        self.job.single = self.info

        def _persist_progress():
            return job_persistence.persist(self.job)

        removed = owned_paths._unlink_owned_path(
            d,
            self.info.get("owned_path"),
            progress=_persist_progress,
            outcome_out=self.undo_outcome,
        )
        catalog_result = None
        if removed is not None:
            _clear_after_earlier_grabs(
                removed.parent,
                job_persistence.other_album_grabs(self.info.get("album_id"), self.job.id))
            catalog_result = beets_mod.forget_beets_entries([removed])
            self.undo_outcome["single_mark_complete"] = (
                self._clear_deliberate_single_mark()
            )
        return removed, catalog_result

    def _refuse_gate(self, state):
        if state == "paused":
            paused = refusals._lock_busy_response(self.request)
            if paused is not None:
                return paused
            return rendering._tr(self.request, "lock_busy.html", {
                "msg": "Library writes were paused before Undo could start."
            }, status_code=503)
        if state == "busy":
            holder = job_mgr.staging_holder()
            msg = (f"{holder} is using the library right now. Try Undo again "
                   "when it finishes." if holder else
                   "Another job is using the library right now. Try Undo again "
                   "in a moment.")
            if rendering._is_htmx(self.request):
                return HTMLResponse(
                    f'<div id="job-content">'
                    f'{rendering._ql_notice_html("warning", html.escape(msg))}</div>')
            return rendering._tr(self.request, "lock_busy.html", {"msg": msg}, status_code=503)
        return None

    def _refuse_read_only(self):
        if not storage.music_mounted_read_only():
            return None
        msg = ("Your music folder is mounted read-only, so Undo can't "
               "remove anything.")
        if rendering._is_htmx(self.request):
            return HTMLResponse(
                f'<div id="job-content">'
                f'{rendering._ql_notice_html("warning", html.escape(msg))}</div>')
        return rendering._tr(self.request, "lock_busy.html", {"msg": msg}, status_code=503)

    def _denied_folder(self):
        """The folder holding the track when the app may not write to it."""
        root = _undo_root(self.info)
        owned = self.info.get("owned_path")
        if root is None or not isinstance(owned, dict):
            return None
        folder = (root / str(owned.get("relative", ""))).parent
        if folder.is_dir() and not os.access(folder, os.W_OK | os.X_OK):
            return folder
        return None

    async def run(self):
        refused = self._refuse() or self._refuse_read_only()
        if refused is not None:
            return refused
        # Register under the same gate as the CLI handoff before taking the
        # staging mutex.
        self.loop = asyncio.get_running_loop()
        state, operation_token, self.lock = await self.loop.run_in_executor(
            None, lambda: write_gate._begin_direct_library_operation("Undo"))
        refused = self._refuse_gate(state)
        if refused is not None:
            return refused
        self.lock_held = True
        try:
            await self._settle()
            return await self._finish()
        finally:
            if self.lock_held:
                self.lock.release()
            job_mgr.end_library_operation(operation_token)

    async def _settle(self):
        removed, catalog_result = await self.loop.run_in_executor(None, self._reverse)
        catalog_complete = bool(
            getattr(catalog_result, "complete", catalog_result)
        )
        single_mark_complete = self.undo_outcome.get(
            "single_mark_complete", True
        )
        if removed is not None or self.cleanup_retry:
            # The track is off the disk again, so search must stop calling it
            # yours.
            self.job.landed_complete = False
        self.refresh_needed = False
        if self.cleanup_retry:
            self._settle_cleanup_retry(catalog_complete, single_mark_complete)
        elif removed is not None:
            self._settle_removed(removed, catalog_complete, single_mark_complete)
        else:
            await self._settle_kept()

    def _settle_cleanup_retry(self, catalog_complete, single_mark_complete):
        if catalog_complete and single_mark_complete:
            completed = {**self.info, "removed": True}
            completed.pop("catalog_cleanup", None)
            self.job.single = completed
            if self.job.attention == "catalog":
                self.job.attention = ""
            self.job.summary = _undone_summary(self.info, self.undo_outcome)
        elif catalog_complete:
            self.job.single = {**self.info, "removed": False}
            self.job.single.pop("catalog_cleanup", None)
            if self.job.attention == "catalog":
                self.job.attention = ""
            self.job.summary = (
                f"Removed “{self.info.get('title')}”, but couldn't save the "
                "single mark cleanup. Retry Undo."
            )
        else:
            self.job.attention = "catalog"
            self.job.summary = (
                f"Removed “{self.info.get('title')}”, but Beets still lists it. "
                "Retry Beets cleanup."
            )

    def _settle_removed(self, removed, catalog_complete, single_mark_complete):
        self.refresh_needed = True
        if catalog_complete and single_mark_complete:
            self.job.single = {**self.info, "removed": True}
            self.job.summary = _undone_summary(self.info, self.undo_outcome)
        elif catalog_complete:
            self.job.single = {**self.info, "removed": False}
            self.job.summary = (
                f"Removed “{self.info.get('title')}”, but couldn't save the "
                "single mark cleanup. Retry Undo."
            )
        else:
            self.job.single = {
                **self.info,
                "removed": True,
                "catalog_cleanup": {
                    "pending": True,
                    "path": str(removed),
                },
            }
            self.job.attention = "catalog"
            self.job.summary = (
                f"Removed “{self.info.get('title')}”, but Beets still lists it. "
                "Retry Beets cleanup."
            )

    async def _settle_kept(self):
        # If the whole recorded directory is gone, clearing the single
        # mark cannot delete anything.
        dir_gone = (
            self.info.get("owned_root") is None
            and not Path(self.info["dir"]).exists()
        )
        if dir_gone:
            single_mark_complete = await self.loop.run_in_executor(
                None, self._clear_deliberate_single_mark
            )
            self.refresh_needed = True
            self.job.single = {
                **self.info,
                "removed": bool(single_mark_complete),
            }
            if single_mark_complete and (not self.info.get("marked")
                                         or self.undo_outcome.get("single_kept")):
                self.job.summary = f"“{self.info.get('title')}” was already gone."
            elif single_mark_complete:
                self.job.summary = (f"“{self.info.get('title')}” was already gone; "
                                    "cleared the single mark.")
            else:
                self.job.summary = (
                    f"“{self.info.get('title')}” was already gone, but couldn't "
                    "save the single mark cleanup. Retry Undo."
                )
        else:
            denied = (None if self.undo_outcome.get("files_complete")
                      else self._denied_folder())
            if denied is not None:
                folder, _is_host = storage._resolve_host_path(str(denied))
                self.job.summary = (
                    f"Undo couldn't remove “{self.info.get('title')}”: the "
                    f"app can't write to {folder}.")
            elif self.undo_outcome.get("files_complete"):
                self.job.summary = (
                    f"Removed “{self.info.get('title')}”, but couldn't "
                    "finish cleaning up its folders. Try Undo again.")
            elif self.undo_outcome.get("removed_files"):
                self.job.summary = (
                    "Part of Undo completed, but couldn't finish "
                    f"removing “{self.info.get('title')}”. Try Undo again.")
            elif self.undo_outcome.get("held_files"):
                self.job.summary = (
                    "Undo set the downloaded copy of "
                    f"“{self.info.get('title')}” aside, but couldn't finish "
                    "removing it. Try Undo again.")
            elif self.undo_outcome.get("undo_started"):
                self.job.summary = (
                    "Undo started but couldn't finish removing "
                    f"“{self.info.get('title')}”. Try Undo again.")
            else:
                self.job.summary = (
                    "Couldn't verify the downloaded copy of "
                    f"“{self.info.get('title')}”. Nothing was removed; delete "
                    "it manually if needed.")

    async def _finish(self):
        # Persist while the direct-operation registration still holds the web
        # run lock; a restart must not resurrect an Undo that already removed a
        # file or cleared its single mark.
        saved = await self.loop.run_in_executor(
            None, lambda: job_persistence.persist(self.job)
        )
        if not saved:
            msg = (
                "Undo changed the saved track state, but its final record "
                "couldn't be saved. Retry Undo before clearing History."
            )
            self.job.single = self.info
            self.job.summary = msg
            if rendering._is_htmx(self.request):
                return rendering._tr(self.request, "_job_body.html", {"job": self.job})
            return rendering._tr(self.request, "lock_busy.html", {
                "reason": "Undo needs attention",
                "msg": msg,
                "action": {"href": f"/jobs/{self.job.id}", "label": "Retry Undo"},
            }, status_code=503)
        self.lock.release()
        self.lock_held = False
        if self.refresh_needed:
            await self.loop.run_in_executor(None, self._refresh_after_undo)
        if rendering._is_htmx(self.request):
            return rendering._tr(self.request, "_job_body.html", {"job": self.job})
        return RedirectResponse(url=f"/jobs/{self.job.id}", status_code=303)


@router.post("/jobs/{job_id}/undo")
async def job_undo(request: Request, job_id: str):
    """Reverse a single-track download whose exact owned path is still bound."""
    # Undo deletes files and touches the beets DB, so it needs the same run-lock
    # gate every other mutating route has; the in-process staging lock below
    # can't keep it off the library while a CLI session or another instance
    # holds the cross-process lock.
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        if rendering._is_htmx(request):
            return HTMLResponse(
                f'<div id="job-content">{busy.body.decode()}</div>')
        return busy
    return await _UndoRun(request, job_id).run()


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
    # Offload: cancelling a parked review runs cancel_review -> persist, which
    # removes every saved row of the review and would block the event loop
    # and stall every SSE stream for a large one.
    loop = asyncio.get_running_loop()
    canceled = await loop.run_in_executor(
        None, lambda: job_mgr.request_cancel(job)
    )
    if not canceled:
        if job.importing:
            message = "Import has started and cannot be stopped."
        elif job.status in job_mgr.TERMINAL:
            message = "That job had already finished."
        else:
            message = "The change could not be saved to the data folder."
        dest = "/queue" if return_to_queue else f"/jobs/{job_id}"
        return RedirectResponse(
            url=dest + "?error=" + rendering._notice_key(message),
            status_code=303,
        )
    if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
        # Back in its review, which for these two is the job page itself.
        if job.execute_kind in ("new_releases", "collection_restore"):
            dest = f"/jobs/{job_id}"
        else:
            dest = job_labels._job_nav_destination(job)[1]
        return RedirectResponse(url=dest, status_code=303)
    if return_to_queue:
        return RedirectResponse(url="/queue", status_code=303)
    if job.execute_kind in job_labels._JOB_NAV_SURFACES:
        dest = job_labels._job_nav_destination(job)[1]
        # Both land on /library with no other sign the discard happened; a
        # review that was there a second ago is just gone otherwise.
        if was_review and job.execute_kind in ("library", "new_releases"):
            label = ("New releases review" if job.execute_kind == "new_releases"
                     else "Library review")
            dest += "?notice=" + rendering._notice_key(f"{label} discarded.")
    else:
        dest = "/queue" if (was_review or was_pending) else f"/jobs/{job_id}"
    return RedirectResponse(url=dest, status_code=303)
