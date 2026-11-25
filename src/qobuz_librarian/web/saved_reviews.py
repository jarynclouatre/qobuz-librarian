"""Upgrade and Downsample reviews rebuilt from saved scan results."""
import hashlib
import json
import threading

from fastapi.responses import RedirectResponse

from qobuz_librarian.integrations import downsample_engine
from qobuz_librarian.library import candidate_premise, downsample_state, generation_state
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import (
    job_labels,
    job_persistence,
    job_runs,
    qobuz_access,
    review_badges,
    runtime,
    settings_store,
    storage,
    write_gate,
)
from qobuz_librarian.web import jobs as job_mgr


def _upgrade_unavailable_response():
    review_badges.clear_ready("upgrade")
    return RedirectResponse(url="/", status_code=303)


def _upgrade_state_summary():
    state = upgrade_state.load()
    authority = generation_state.load()
    generation = int(authority.get("generation") or 0)
    output = generation_state.output_state("upgrade", authority)
    # Upgrade cannot answer without a scanned baseline. Downsample can, so its
    # twin below deliberately leaves this term out.
    snapshot_current = bool(
        generation > 0
        and int(state.get("generation") or 0) == generation
        and output.get("status") == "current"
        and int(output.get("revision") or 0) == int(state.get("revision") or 0)
    )
    complete = bool(state.get("complete") and snapshot_current)
    candidates = (
        _visible_saved_review_candidates("upgrade", state.get("candidates") or [])
        if complete else [])
    kept = review_badges.undiscarded("upgrade", state, candidates)
    discarded = bool(candidates) and not kept
    held_back = len(candidates) - len(kept)
    candidates = kept
    saved_quality_signature = str(state.get("quality_signature") or "")
    current_quality_signature = _effective_upgrade_quality_signature()
    scanned_at = state.get("scanned_at")
    return {
        "complete": complete,
        "discarded": discarded,
        "held_back": held_back,
        "candidates": candidates,
        "count": len(candidates),
        "quality_signature": saved_quality_signature,
        "generation": generation,
        "status": (
            "current" if complete
            else "baseline_missing" if not generation
            else "stale"
        ),
        "stale": bool(
            generation
            and (
                not complete
                or saved_quality_signature != current_quality_signature
            )
        ),
        # Which staleness cause applies, so the page can name it
        # instead of offering the user every one and letting them guess.
        "stale_cause": (
            "quality" if complete
            else "update" if upgrade_state.saved_by_older_version()
            else "library"
        ),
        # Results are listed only once complete, and only then given an age.
        "updated": (job_labels._format_age(scanned_at)
                    if complete and scanned_at else None),
        "updated_at": state.get("updated_at"),
    }


def _effective_upgrade_quality_signature():
    values = settings_store.current()
    return upgrade_state.quality_signature(
        values.get("STREAMRIP_QUALITY"),
        values.get("PREFER_HIRES"),
        values.get("DOWNSAMPLE_HIRES_ENABLED"),
    )


def _downsample_state_summary():
    state = downsample_state.load()
    authority = generation_state.load()
    generation = int(authority.get("generation") or 0)
    output = generation_state.output_state("downsample", authority)
    # No generation > 0 term here on purpose. A standalone Downsample refresh
    # targets generation 0 when no baseline scan has ever run, and that is the
    # whole point of Downsample working on its own. Do not harmonise this with
    # the Upgrade twin above.
    snapshot_current = bool(
        int(state.get("generation") or 0) == generation
        and output.get("status") == "current"
        and int(output.get("revision") or 0) == int(state.get("revision") or 0)
    )
    complete = bool(state.get("complete") and snapshot_current)
    candidates = (
        _visible_saved_review_candidates("downsample", state.get("candidates") or [])
        if complete else [])
    kept = review_badges.undiscarded("downsample", state, candidates)
    discarded = bool(candidates) and not kept
    held_back = len(candidates) - len(kept)
    candidates = kept
    scanned_at = state.get("scanned_at")
    return {
        "complete": complete,
        "discarded": discarded,
        "held_back": held_back,
        "candidates": candidates,
        "count": len(candidates),
        "generation": generation,
        # Two statuses where Upgrade has three: needing no baseline, Downsample
        # has no baseline_missing case to report. It has no quality signature to
        # age out either, so a saved run is stale only when it did not finish.
        "status": "current" if complete else "stale",
        "stale": bool(state.get("updated_at") and not complete),
        "updated": job_labels._format_age(scanned_at) if scanned_at else None,
        "scanned_at": scanned_at,
        "updated_at": state.get("updated_at"),
    }


def _visible_saved_review_candidates(surface, candidates):
    if surface == "upgrade":
        return upgrade_state.visible_candidates({
            "complete": True,
            "candidates": list(candidates or []),
        })
    if surface == "downsample":
        return downsample_state.visible_candidates({
            "complete": True,
            "candidates": list(candidates or []),
        })
    return list(candidates or [])


def _saved_review_row(surface, spec):
    if surface == "downsample":
        payload = spec.get("payload") or {}
        row_payload = {
            "album_dir": spec.get("album_dir") or payload.get("album_dir") or "",
            "est_saving": spec.get("est_saving") or payload.get("est_saving") or 0,
        }
        for key in ("album", "year"):
            if spec.get(key) or payload.get(key):
                row_payload[key] = spec.get(key) or payload.get(key)
        premise = spec.get("_premise") or payload.get("_premise")
        if premise is not None:
            row_payload["_premise"] = premise
    else:
        row_payload = spec.get("payload") or {}
    return {
        "title": spec.get("title") or "?",
        "artist": spec.get("artist") or "",
        "detail": spec.get("detail") or "",
        "payload": row_payload,
    }


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))


def _saved_review_key(surface, spec):
    """The album folder a saved row acts on. A refresh seals it again and
    may rewrite its details, and the album stays the same."""
    row = _saved_review_row(surface, spec)
    payload = row["payload"]
    if surface == "upgrade" and payload.get("album_id") and payload.get("album_dir"):
        return "album", str(payload["album_id"]), payload["album_dir"]
    if surface == "downsample" and payload.get("album_dir"):
        return "album", payload["album_dir"]
    return "row", _canonical_json(row)


def _saved_review_claim_key(surface, spec):
    """Return the stable identity of one executable saved-state action."""
    row = _saved_review_row(surface, spec)
    payload = row["payload"]
    if surface == "upgrade":
        album_id = payload.get("album_id")
        if (isinstance(album_id, (str, int))
                and not isinstance(album_id, bool)
                and str(album_id).strip()):
            return surface, "album_id", str(album_id).strip()
    elif surface == "downsample":
        album_dir = payload.get("album_dir")
        if isinstance(album_dir, str) and album_dir:
            return surface, "album_dir", album_dir
    return surface, "row", _canonical_json(row)


def _saved_review_signature(surface, state):
    rows = []
    for spec in state.get("candidates") or []:
        rows.append(_saved_review_row(surface, spec))
    rows.sort(key=_canonical_json)
    signature_data = {"rows": rows}
    if surface == "upgrade":
        signature_data["quality_signature"] = str(
            state.get("quality_signature") or ""
        )
    raw = _canonical_json(signature_data)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _saved_review_specs_from_job(surface, job):
    with job._lock:
        candidates = list(job.candidates)
    specs = []
    for c in candidates:
        payload = c.get("payload") or {}
        if surface == "downsample":
            spec = {
                "title": c.get("title") or "?",
                "artist": c.get("artist") or "",
                "detail": c.get("detail") or "",
                "album_dir": payload.get("album_dir") or "",
                "est_saving": payload.get("est_saving") or 0,
            }
            for key in ("album", "year"):
                if payload.get(key):
                    spec[key] = payload[key]
            if payload.get("_premise") is not None:
                spec["_premise"] = payload["_premise"]
            specs.append(spec)
        else:
            specs.append({
                "title": c.get("title") or "?",
                "artist": c.get("artist") or "",
                "detail": c.get("detail") or "",
                "payload": payload,
            })
    return specs


def _job_review_signature(surface, job):
    return _saved_review_signature(surface, {
        "candidates": _saved_review_specs_from_job(surface, job),
        "quality_signature": (job.execute_args or {}).get(
            "quality_signature", ""),
    })


def _existing_saved_review_job(surface, signature):
    matching = [
        job for job in job_mgr.registry.all()
        if job.execute_kind == surface and job.status in job_mgr.ACTIVE
        and _job_review_signature(surface, job) == signature
    ]
    for job in matching:
        if getattr(job, "_saved_review_signature", None) == signature:
            return job
    if not matching:
        return None
    matching[0]._saved_review_signature = signature
    return matching[0]


def _active_saved_review_claim(surface, state):
    """Find a running review that already owns any requested action."""
    requested = {
        _saved_review_claim_key(surface, spec)
        for spec in state.get("candidates") or []
    }
    if not requested:
        return None
    executing = (job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING)
    for job in job_mgr.registry.all():
        if job.execute_kind != surface:
            continue
        with job._lock:
            if job.status not in executing:
                continue
            claimed = {
                _saved_review_claim_key(surface, candidate)
                for candidate in job.candidates
                if candidate.get("selected")
            }
        if requested.intersection(claimed):
            return job
    return None


_SAVED_REVIEW_TITLES = {
    "upgrade": "Albums to upgrade",
    "downsample": "Albums to downsample",
}
# A review parked under the earlier titles is still in jobs.db under them, and
# a restart drops the in-memory signature that would otherwise identify it, so
# the old name has to be recognised or the page publishes a second review
# beside the one already there.
_SAVED_REVIEW_TITLES_PRIOR = {
    "upgrade": "Upgrade candidates",
    "downsample": "Downsample candidates",
}
_SAVED_REVIEW_LOCK = threading.RLock()


def _saved_state_summary(surface):
    if surface == "upgrade":
        return _upgrade_state_summary()
    return _downsample_state_summary()


def _saved_review_summary(surface, count):
    return f"{plural(count, 'album')} can be {surface}d."


def _is_saved_review(job):
    surface = job.execute_kind
    return (getattr(job, "_saved_review_signature", None) is not None
            or job.title in (_SAVED_REVIEW_TITLES.get(surface),
                             _SAVED_REVIEW_TITLES_PRIOR.get(surface)))


def _parked_reviews(surface):
    """The surface's parked reviews, newest first."""
    return [job for job in reversed(job_mgr.registry.awaiting_review())
            if job.execute_kind == surface]


def _stale_saved_review_job(surface):
    for job in _parked_reviews(surface):
        # A finished Downsample refresh saved what its own review lists.
        if (_is_saved_review(job)
                or surface == "downsample" and not job.unchecked_artists):
            return job
    return None


def _candidate_from_saved_spec(surface, spec, *, cid, seq, selected):
    row = _saved_review_row(surface, spec)
    return {
        "cid": cid,
        "seq": seq,
        "kind": surface,
        "title": row["title"],
        "artist": row["artist"],
        "detail": row["detail"],
        "payload": row["payload"],
        "selected": bool(selected),
    }


def _same_offer(shown, saved):
    """Whether a saved row offers what the review showed, on the same files."""
    def offer(row):
        payload = {key: value for key, value in (row.get("payload") or {}).items()
                   if key != "_premise"}
        return row.get("title"), row.get("artist"), row.get("detail"), payload

    if offer(shown) != offer(saved):
        return False
    before = (shown.get("payload") or {}).get("_premise")
    after = (saved.get("payload") or {}).get("_premise")
    return before == after or candidate_premise.same_files(before, after)


def _sync_saved_review_job(job, surface, state, signature):
    """Bring an existing saved-state review job back in line after restore/hide.

    The job is the user's live review session, so preserve ticks for albums
    still offered as they were shown, on the same files, and take the seal
    the saved results now hold. A row whose files or offer changed comes back
    unticked, and so do restored saved candidates.
    """
    desired = list(state.get("candidates") or [])

    def _sync():
        # A review that already left AWAITING_REVIEW must not be replaced
        # underneath its approval.
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return False
        existing_raw_by_key = {
            _saved_review_key(surface, c): c
            for c in job.candidates
        }
        next_seq = max(
            [int(c.get("seq", -1)) for c in job.candidates
             if str(c.get("seq", "")).lstrip("-").isdigit()] + [-1]
        ) + 1
        if job._cand_seq < next_seq:
            job._cand_seq = next_seq
        rebuilt = []
        for spec in desired:
            key = _saved_review_key(surface, spec)
            old = existing_raw_by_key.get(key)
            if old is not None and old.get("cid") is not None:
                cid = old["cid"]
                seq = old.get("seq")
                if not isinstance(seq, int):
                    seq = job._cand_seq
                    job._cand_seq += 1
                selected = bool(old.get("selected"))
            else:
                cid = f"c{job._cand_seq}"
                seq = job._cand_seq
                job._cand_seq += 1
                selected = False
            row = _candidate_from_saved_spec(
                surface, spec, cid=cid, seq=seq, selected=selected)
            if selected and not _same_offer(old, row):
                row["selected"] = False
            rebuilt.append(row)
        job.candidates = rebuilt
        job._saved_review_signature = signature
        if surface == "upgrade":
            job.execute_args = {
                **(job.execute_args or {}),
                "quality_signature": str(state.get("quality_signature") or ""),
            }
        job.title = _SAVED_REVIEW_TITLES[surface]
        job.summary = _saved_review_summary(surface, len(rebuilt))
        return True

    saved, synced = job_persistence.persist_review_mutation(job, _sync)
    if not saved:
        job.notify_review_changed("save_failed")
        return None
    if not synced:
        return job
    job.notify_review_changed()
    return job


def _publish_saved_review(job):
    """Admit one reconstructed review before exposing it in memory."""
    if write_gate._web_writes_paused():
        return False
    if not job_persistence.admit(job):
        return False
    job_mgr.registry.add(job)
    return True


def _sync_saved_review_before_approve(job):
    """Confirm saved state still matches without mutating the parked review."""
    surface = job.execute_kind
    if surface not in _SAVED_REVIEW_TITLES:
        return job
    if not _is_saved_review(job):
        return job
    with _SAVED_REVIEW_LOCK:
        state = _saved_state_summary(surface)
        current = {
            "candidates": state["candidates"] if state.get("complete") else [],
        }
        if surface == "upgrade":
            current["quality_signature"] = state.get("quality_signature", "")
        saved_signature = _saved_review_signature(surface, current)
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return job
        review_signature = _job_review_signature(surface, job)
        return job if review_signature == saved_signature else False


def open_saved_review(job):
    """The review to show for ``job`` opened by its own link, such as from
    History: a parked Upgrade or Downsample review brought in line with the
    saved results as the Review button on its page does, or the review that
    already is. Returns None when the saved review has no albums left."""
    surface = job.execute_kind
    if (surface not in _SAVED_REVIEW_TITLES
            or job.status != job_mgr.JobStatus.AWAITING_REVIEW
            or not _is_saved_review(job)):
        return job
    with _SAVED_REVIEW_LOCK:
        state = _saved_state_summary(surface)
        if (not state["complete"]
                or state.get("stale") and surface == "upgrade"
                or _active_saved_review_claim(surface, state) is not None):
            return job
        if not state["candidates"]:
            return None
        signature = _saved_review_signature(surface, state)
        existing = _existing_saved_review_job(surface, signature)
        if existing is not None:
            return existing
        return _sync_saved_review_job(job, surface, state, signature) or job


def _review_job_from_state(surface, state, *, create=True):
    with _SAVED_REVIEW_LOCK:
        claimed = _active_saved_review_claim(surface, state)
        if claimed is not None:
            return claimed
        signature = _saved_review_signature(surface, state)
        existing = _existing_saved_review_job(surface, signature)
        if existing is not None:
            return existing
        stale = _stale_saved_review_job(surface)
        if stale is not None:
            return _sync_saved_review_job(stale, surface, state, signature)
        if not create:
            return None
        job = job_mgr.Job(title=_SAVED_REVIEW_TITLES[surface])
        job.kind = "scan"
        job.execute_kind = surface
        if surface == "upgrade":
            job.execute_args = {
                "quality_signature": str(state.get("quality_signature") or ""),
            }
        job.review_verb = surface.capitalize()
        job._saved_review_signature = signature
        job._execute_fn = job_runs._RESUME_EXECUTE[surface](job, job.execute_args)
        for spec in state.get("candidates") or []:
            job.add_candidate(
                kind=surface, selected=False, **_saved_review_row(surface, spec))
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        job.summary = _saved_review_summary(surface, len(job.candidates))
        if not _publish_saved_review(job):
            return None
        return job


def _review_job_from_current_saved_state(surface, *, create=True):
    """Build or reuse one review from a state snapshot taken atomically.

    Hide and approval use the same lock. A delayed request therefore cannot
    reintroduce a candidate that was just hidden or create a second review for
    work that an existing job has already claimed.
    """
    with _SAVED_REVIEW_LOCK:
        if surface not in _SAVED_REVIEW_TITLES:
            raise ValueError("unsupported saved review surface")
        state = _saved_state_summary(surface)
        if not state["complete"] or not state["candidates"]:
            return None
        if surface == "upgrade" and state.get("stale"):
            return None
        return _review_job_from_state(surface, state, create=create)


def _partial_refresh_review(state):
    """A Downsample refresh that could not check every artist saves nothing,
    so until a newer scan its own review is all that lists what it found."""
    for job in _parked_reviews("downsample"):
        if (job.unchecked_artists and not _is_saved_review(job)
                and job.created_at > (state["scanned_at"] or 0)):
            return job
    return None


def page_review(surface):
    """The parked review the Upgrade or Downsample page lists, built from the
    saved results if none is parked, or None for the page to say why not."""
    if surface == "upgrade":
        available = runtime._upgrade_available()
        create = qobuz_access._qobuz_ready()
    else:
        available = downsample_engine.HAVE_DOWNSAMPLE
        create = True
    if not available:
        return None
    create = (create and not storage.music_mounted_read_only()
              and not write_gate._web_writes_paused())
    with _SAVED_REVIEW_LOCK:
        if any(job.execute_kind == surface
               and job.status != job_mgr.JobStatus.AWAITING_REVIEW
               for job in job_mgr.registry.pending_and_running()):
            return None
        job = (_partial_refresh_review(_saved_state_summary(surface))
               if surface == "downsample" else None)
        job = job or _review_job_from_current_saved_state(
            surface, create=create)
    if job is None or job.status != job_mgr.JobStatus.AWAITING_REVIEW:
        return None
    return job


def review_location(job):
    """Where ``job``, a parked Upgrade or Downsample review, is shown when it
    is not its own job page, or None."""
    with _SAVED_REVIEW_LOCK:
        shown = page_review(job.execute_kind)
        if shown is not None and (shown is job or _is_saved_review(job)):
            return f"/{job.execute_kind}"
        shown = open_saved_review(job)
        if shown is None:
            return f"/{job.execute_kind}"
    return None if shown is job else f"/jobs/{shown.id}"
