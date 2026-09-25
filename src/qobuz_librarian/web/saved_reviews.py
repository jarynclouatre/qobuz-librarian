"""Upgrade and Downsample reviews rebuilt from saved scan results."""
import hashlib
import json
import threading

from fastapi.responses import RedirectResponse

from qobuz_librarian.library import downsample_state, generation_state
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.web import job_persistence, review_badges, runtime, settings_store
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
    saved_quality_signature = str(state.get("quality_signature") or "")
    current_quality_signature = _effective_upgrade_quality_signature()
    updated_at = state.get("updated_at")
    return {
        "complete": complete,
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
        # Which of the two staleness causes applies, so the page can name it
        # instead of offering the user both and letting them guess.
        "stale_cause": "quality" if complete else "library",
        "updated": runtime._format_age(updated_at) if updated_at else None,
    }


def _effective_upgrade_quality_signature():
    values = settings_store.current()
    return upgrade_state.quality_signature(
        values.get("STREAMRIP_QUALITY"),
        values.get("PREFER_HIRES"),
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
    updated_at = state.get("updated_at")
    return {
        "complete": complete,
        "candidates": candidates,
        "count": len(candidates),
        "generation": generation,
        # Two statuses where Upgrade has three: needing no baseline, Downsample
        # has no baseline_missing case to report. It has no quality signature to
        # age out either, so a saved run is stale only when it did not finish.
        "status": "current" if complete else "stale",
        "stale": bool(state.get("updated_at") and not complete),
        "updated": runtime._format_age(updated_at) if updated_at else None,
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
    return _canonical_json(_saved_review_row(surface, spec))


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


def _existing_saved_review_job(surface, signature):
    review_jobs = [
        job for job in job_mgr.registry.all()
        if job.execute_kind == surface and job.status in job_mgr.ACTIVE
    ]
    for job in review_jobs:
        if (job.execute_kind == surface
                and getattr(job, "_saved_review_signature", None) == signature):
            current_signature = _saved_review_signature(
                surface, {
                    "candidates": _saved_review_specs_from_job(surface, job),
                    "quality_signature": (job.execute_args or {}).get(
                        "quality_signature", ""),
                })
            if current_signature == signature:
                return job
    for job in review_jobs:
        current_signature = _saved_review_signature(
            surface, {
                "candidates": _saved_review_specs_from_job(surface, job),
                "quality_signature": (job.execute_args or {}).get(
                    "quality_signature", ""),
            })
        if current_signature == signature:
            job._saved_review_signature = signature
            return job
    return None


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


def _is_saved_review_title(surface, title):
    return title in (_SAVED_REVIEW_TITLES.get(surface),
                     _SAVED_REVIEW_TITLES_PRIOR.get(surface))


def _stale_saved_review_job(surface):
    review_jobs = [
        job for job in job_mgr.registry.awaiting_review()
        if job.execute_kind == surface
    ]
    for job in reversed(review_jobs):
        if (getattr(job, "_saved_review_signature", None) is not None
                or _is_saved_review_title(surface, job.title)):
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


def _sync_saved_review_job(job, surface, state, signature):
    """Bring an existing saved-state review job back in line after restore/hide.

    The job is the user's live review session, so preserve ticks for candidates
    that still exist and add restored saved candidates unticked.
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
            rebuilt.append(
                _candidate_from_saved_spec(
                    surface, spec, cid=cid, seq=seq, selected=selected)
            )
        job.candidates = rebuilt
        job._saved_review_signature = signature
        if surface == "upgrade":
            job.execute_args = {
                **(job.execute_args or {}),
                "quality_signature": str(state.get("quality_signature") or ""),
            }
        n = len(rebuilt)
        job.title = _SAVED_REVIEW_TITLES[surface]
        if surface == "downsample":
            job.summary = f"{n} album{'s' if n != 1 else ''} can be downsampled."
        else:
            job.summary = f"{n} album{'s' if n != 1 else ''} can be upgraded."
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
    if runtime._web_writes_paused():
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
    if (getattr(job, "_saved_review_signature", None) is None
            and not _is_saved_review_title(surface, job.title)):
        return job
    with _SAVED_REVIEW_LOCK:
        state = (
            _upgrade_state_summary()
            if surface == "upgrade"
            else _downsample_state_summary()
        )
        current = {
            "candidates": state["candidates"] if state.get("complete") else [],
        }
        if surface == "upgrade":
            current["quality_signature"] = state.get("quality_signature", "")
        saved_signature = _saved_review_signature(surface, current)
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return job
        review_signature = _saved_review_signature(
            surface,
            {
                "candidates": _saved_review_specs_from_job(surface, job),
                "quality_signature": (job.execute_args or {}).get(
                    "quality_signature", ""
                ),
            },
        )
        return job if review_signature == saved_signature else False


def _review_job_from_upgrade_state(state):
    with _SAVED_REVIEW_LOCK:
        claimed = _active_saved_review_claim("upgrade", state)
        if claimed is not None:
            return claimed
        signature = _saved_review_signature("upgrade", state)
        existing = _existing_saved_review_job("upgrade", signature)
        if existing is not None:
            return existing
        stale = _stale_saved_review_job("upgrade")
        if stale is not None:
            return _sync_saved_review_job(stale, "upgrade", state, signature)
        job = job_mgr.Job(title="Albums to upgrade")
        job.kind = "scan"
        job.execute_kind = "upgrade"
        job.execute_args = {
            "quality_signature": str(state.get("quality_signature") or ""),
        }
        job.review_verb = "Upgrade"
        job._saved_review_signature = signature
        job._execute_fn = runtime._resume_upgrade(job, job.execute_args)
        for spec in state.get("candidates") or []:
            job.add_candidate(
                kind="upgrade",
                title=spec.get("title") or "?",
                artist=spec.get("artist") or "",
                detail=spec.get("detail") or "",
                payload=spec.get("payload") or {},
                selected=False,
            )
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        n = len(job.candidates)
        job.summary = f"{n} album{'s' if n != 1 else ''} can be upgraded."
        if not _publish_saved_review(job):
            return None
        return job


def _review_job_from_downsample_state(state):
    with _SAVED_REVIEW_LOCK:
        claimed = _active_saved_review_claim("downsample", state)
        if claimed is not None:
            return claimed
        signature = _saved_review_signature("downsample", state)
        existing = _existing_saved_review_job("downsample", signature)
        if existing is not None:
            return existing
        stale = _stale_saved_review_job("downsample")
        if stale is not None:
            return _sync_saved_review_job(stale, "downsample", state, signature)
        job = job_mgr.Job(title="Albums to downsample")
        job.kind = "scan"
        job.execute_kind = "downsample"
        job.review_verb = "Downsample"
        job._saved_review_signature = signature
        job._execute_fn = runtime._resume_downsample(job, job.execute_args)
        for spec in state.get("candidates") or []:
            payload = {
                "album_dir": spec.get("album_dir") or "",
                "est_saving": spec.get("est_saving") or 0,
            }
            if spec.get("_premise") is not None:
                payload["_premise"] = spec["_premise"]
            job.add_candidate(
                kind="downsample",
                title=spec.get("title") or "?",
                artist=spec.get("artist") or "",
                detail=spec.get("detail") or "",
                payload=payload,
                selected=False,
            )
        job.status = job_mgr.JobStatus.AWAITING_REVIEW
        n = len(job.candidates)
        job.summary = f"{n} album{'s' if n != 1 else ''} can be downsampled."
        if not _publish_saved_review(job):
            return None
        return job


def _review_job_from_current_saved_state(surface):
    """Build or reuse one review from a state snapshot taken atomically.

    Hide and approval use the same lock. A delayed request therefore cannot
    reintroduce a candidate that was just hidden or create a second review for
    work that an existing job has already claimed.
    """
    with _SAVED_REVIEW_LOCK:
        if surface == "upgrade":
            state = _upgrade_state_summary()
            factory = _review_job_from_upgrade_state
        elif surface == "downsample":
            state = _downsample_state_summary()
            factory = _review_job_from_downsample_state
        else:
            raise ValueError("unsupported saved review surface")
        if not state["complete"] or not state["candidates"]:
            return None
        if surface == "upgrade" and state.get("stale"):
            return None
        return factory(state)
