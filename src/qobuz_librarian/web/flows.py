"""Scan / execute logic behind the web Artist and Library flows.

These wrap the same engine the CLI uses (catalog matching, gap detection,
process_album) but without any terminal prompts — a scan attaches review
candidates to the job, and execution runs over the candidates the user kept.
"""
import argparse
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable, load_qobuz_token
from qobuz_librarian.api.search import get_album
from qobuz_librarian.library import (
    downsample_state,
    library_scan_state,
    scan_checkpoint,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import new_releases as new_releases_mod
from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
from qobuz_librarian.library.catalog import (
    album_quality_label,
    album_year,
    find_album_dir_filesystem,
    find_qobuz_album_for_dir,
    is_lossless_album,
)
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_missing_for_artist,
    find_new_releases_for_artist,
    flush_resolve_cache,
    resolve_artist_dir,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.web import review_badges


def build_args():
    """Namespace of CLI flags used by process_album and the artist/walk runners.

    `consolidate` is forced False: the web has no confirm() UI, so letting
    the engine scan for siblings it can't act on would waste time.
    """
    return argparse.Namespace(
        force=False, yes=True, dry_run=False, no_import=False,
        no_upgrade=False, no_downsample=False,
        prefer_hires=cfg.PREFER_HIRES,
        consolidate=False,
        migrate_multi_artist=cfg.MIGRATE_MULTI_ARTIST,
        include_comps=False,
        include_singles=False,
        no_catalog=False,
        auto_safe=False,
        # auto_upgrade is request-scoped (the explicit Upgrade flow flips it
        # to True for one run) so passive gap scans do not need to mutate
        # cfg.AUTO_UPGRADE_ENABLED mid-job.
        auto_upgrade=cfg.AUTO_UPGRADE_ENABLED,
        verbose=False,
    )


def _set_empty_library_summary(job):
    """Nothing to scan. Say what the app expected to find and where to fix it.

    The layout and the pointer to Settings used to go to the log, which the user
    never sees, leaving a message that only told them to check that their folder
    contained folders.
    """
    from qobuz_librarian import config as _cfg
    from qobuz_librarian.web import jobs as _job_mgr
    job.error = (
        f"Nothing to scan — no artist folders were found in {_cfg.MUSIC_ROOT}. "
        "Qobuz Librarian expects one folder per artist, each holding album "
        "folders. Check the music folder path in Settings."
    )
    # A run that never scanned anything is not a clean pass. submit_scan leaves
    # an explicitly-set terminal status alone, so this keeps the green Done
    # chip off a scan the user still has to fix something for.
    job.status = _job_mgr.JobStatus.FAILED
    log.info("No artist folders found in the configured music library.")
    log.info("  Expected layout: <music library>/<Artist>/<Album (Year)>/<track>.flac")
    log.info("  Check the music library path in Settings.")


def _surface_has_candidates(surface):
    if surface == "upgrade":
        return upgrade_state.has_visible_candidates()
    elif surface == "downsample":
        return downsample_state.has_visible_candidates()
    return False


def _artist_dir_from_result(album, result=None, fallback_artist=None):
    result_dir = (result or {}).get("dir")
    if result_dir:
        album_dir = Path(result_dir)
        if album_dir.exists():
            return album_dir.parent if album_dir.is_dir() else album_dir.parent.parent
    if album:
        try:
            clear_scan_caches()
            album_dir = find_album_dir_filesystem(album)
            if album_dir and album_dir.exists():
                return album_dir.parent
        except Exception as exc:
            log.info(f"  state refresh path lookup failed: {exc}")
    if fallback_artist:
        try:
            return resolve_artist_dir(fallback_artist)
        except Exception as exc:
            log.info(f"  state refresh artist lookup failed: {exc}")
    return None


def _refresh_downsample_artist_state(artist_dir):
    if artist_dir is None:
        return
    result = downsample_state.update_artist(artist_dir, hidden=hidden_mod.load())
    if result.complete:
        review_badges.set_ready("downsample", _surface_has_candidates("downsample"))
    else:
        err = next(iter(result.errors.values()), "unknown error")
        log.info(f"  downsample view refresh skipped for {artist_dir.name}: {err}")


def _refresh_upgrade_artist_state(artist_dir, token, args=None):
    if not cfg.UPGRADE_SCAN_ENABLED:
        review_badges.set_ready("upgrade", False)
        return
    if artist_dir is None or not token:
        return
    try:
        from qobuz_librarian.quality.decision import load_capped
        result = upgrade_state.update_artist(
            artist_dir,
            token=token,
            args=args or build_args(),
            capped=load_capped(),
            hidden=hidden_mod.load(),
        )
    except (AuthLost, QobuzUnavailable):
        raise
    if result.complete:
        review_badges.set_ready("upgrade", _surface_has_candidates("upgrade"))
    else:
        err = next(iter(result.errors.values()), "unknown error")
        log.info(f"  upgrade view refresh skipped for {artist_dir.name}: {err}")


def _refresh_after_local_album_change(
    album,
    result=None,
    *,
    fallback_artist=None,
    token=None,
    args=None,
    upgrade=False,
    downsample=False,
):
    artist_dir = _artist_dir_from_result(album, result, fallback_artist)
    if artist_dir is None:
        return
    if upgrade:
        _refresh_upgrade_artist_state(artist_dir, token, args=args)
    if downsample:
        _refresh_downsample_artist_state(artist_dir)


def _album_cover(album):
    """The album's small cover URL, only if it's a trusted Qobuz CDN link."""
    img = album.get("image") or {}
    url = img.get("small") or img.get("thumbnail") or ""
    return url if url.startswith("https://static.qobuz.com/") else ""


def _album_candidate_spec(
    album,
    artist_name,
    selected=True,
    is_new=False,
    extra_payload=None,
):
    year = album_year(album)
    partial_n = album.get("_partial_missing_count")
    if partial_n:
        detail = (f"{year or '?'} · {album_quality_label(album)} · "
                  f"gap-fill: {partial_n} missing of "
                  f"{album.get('tracks_count') or '?'}")
    else:
        tc = album.get('tracks_count')
        n = int(tc) if str(tc or '').isdigit() else None
        detail = (f"{year or '?'} · {album_quality_label(album)} · "
                  f"{n if n is not None else '?'} track{'' if n == 1 else 's'}")
    payload = {"album_id": album.get("id"), "year": year, "cover": _album_cover(album)}
    if partial_n:
        payload["gap_fill"] = partial_n
    if extra_payload:
        payload.update(extra_payload)
    if is_new:
        payload["is_new"] = True
    return {
        "kind": "album",
        "title": album.get("title") or "?",
        "artist": artist_name,
        "detail": detail,
        "payload": payload,
        "selected": selected,
    }


def _add_candidate_spec(job, spec):
    return job.add_candidate(
        kind=spec.get("kind", "album"),
        title=spec.get("title") or "?",
        artist=spec.get("artist") or "",
        detail=spec.get("detail") or "",
        payload=spec.get("payload") or {},
        selected=bool(spec.get("selected")),
    )


def _readd_candidate(job, c):
    """Re-add a candidate restored from a scan checkpoint, with a fresh cid."""
    _add_candidate_spec(job, c)


def _cap_note(job) -> str:
    """A truncation notice appended to a scan summary when the candidate list hit
    the in-memory cap, so a summary never implies more results are reviewable
    than were actually kept. Empty when nothing was dropped."""
    if not job.candidate_cap_hit:
        return ""
    return (f" Showing the first {len(job.candidates):,}; the scan hit the "
            f"{job.CANDIDATE_CAP:,} result cap. Work through or dismiss some "
            "results, then refresh to see the rest.")


def _gap_candidate_spec(
    gap,
    artist_name,
    selected=False,
    is_new=False,
    artist_key=None,
):
    """Turn an engine AlbumGap into a review candidate. A partial gap carries
    its missing-track count so the detail reads 'gap-fill: N missing'."""
    album = gap.qobuz_album
    if gap.on_disk_dir is not None:
        album = {**album, "_partial_missing_count": gap.missing_count}
    extra_payload = {"_artist_dir": artist_key} if artist_key else None
    return _album_candidate_spec(
        album, artist_name, selected=selected, is_new=is_new,
        extra_payload=extra_payload)


def _add_gap_candidate(job, gap, artist_name, selected=False, is_new=False):
    return _add_candidate_spec(
        job, _gap_candidate_spec(gap, artist_name, selected, is_new))


def candidate_matches_query(c, q):
    """The review filter's predicate — artist or album title contains ``q``.

    Shared by the fragment render and the bulk selection/dismiss endpoints so
    what the user sees is exactly what a bulk action touches. It folds case
    itself: it used to require a pre-lowered query, and a caller that passed
    the raw box contents matched nothing and dismissed nothing, silently.
    """
    hay = (c.get("artist") or "") + " " + (c.get("title") or "")
    return (q or "").lower() in hay.lower()


def is_gap_candidate(c):
    """Whether a saved review candidate is a Gap Fill entry (missing tracks in
    an owned album) rather than a fully missing album. New scans stamp the
    payload; candidates carried forward from older checkpoints only say so in
    their detail line, so fall back to that."""
    if (c.get("payload") or {}).get("gap_fill"):
        return True
    return "gap-fill:" in (c.get("detail") or "")


def library_review_summary(candidates):
    """The parked library review's one-line summary, without the trailing
    period. Every writer of that summary — the scan when it parks, the
    restart rebuild — goes through here, so the History card cannot call Gap
    Fill candidates "missing albums" or disagree with the tabs it describes.
    Splits with the same predicate the tabs use."""
    gaps = sum(1 for c in candidates if is_gap_candidate(c))
    missing = len(candidates) - gaps
    return (f"{len(candidates):,} to review across Missing Albums "
            f"({missing:,}) and Gap Fill ({gaps:,})")


def fold_key(c):
    """A candidate's merge identity: Qobuz album id, falling back to
    artist+title for keyless carry-overs. Shared by the fold and its caller's
    before/after arithmetic so the summary counts what actually changed."""
    album_id = str((c.get("payload") or {}).get("album_id") or "")
    if album_id:
        return album_id
    return ((c.get("artist") or "").lower(), (c.get("title") or "").lower())


def fold_new_candidates(parked, cands):
    """Merge a refresh's finds into a parked review, keyed by Qobuz album id
    (falling back to artist+title for keyless carry-overs).

    Entries the refresh didn't touch — and the user's ticks on them — are
    never changed. An album the refresh found in the OTHER class (a missing
    album now partially on disk, or a gapped album deleted by hand) swaps to
    the fresh candidate with its tick preserved; absence from the refresh is
    NOT evidence of change (the cheap refresh skips unchanged artists), so
    nothing is removed on that basis. Candidates the user dismissed while the
    refresh ran are checked against a fresh hidden snapshot so the fold can't
    resurrect them. Returns (added, updated), or None when the review stopped
    being parked mid-refresh (approved/discarded) — the caller should leave
    the scan's results alone so they park as their own review."""
    from qobuz_librarian.web import jobs as job_mgr

    _key = fold_key
    # Loaded fresh, NOT the scan's own snapshot from its start — the window a
    # refresh runs is exactly when a dismissal would be missed.
    hidden = hidden_mod.load()
    with parked._lock:
        if parked.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return None
        fresh_by_key = {}
        for c in cands:
            fresh_by_key.setdefault(_key(c), c)
        updated = 0
        swapped_ticks = {}
        keep = []
        for c in parked.candidates:
            key = _key(c)
            fresh = fresh_by_key.get(key)
            if (fresh is not None
                    and is_gap_candidate(fresh) != is_gap_candidate(c)):
                # Disk reality changed class — the fresh row replaces this one
                # below, wearing the user's tick.
                swapped_ticks[key] = bool(c.get("selected"))
                updated += 1
                continue
            keep.append(c)
        parked.candidates = keep
        seen = {_key(c) for c in keep}
        added = 0
        for c in cands:
            key = _key(c)
            if key in seen:
                continue
            if key not in swapped_ticks and hidden_mod.is_hidden(
                    hidden_mod.SCOPE_MISSING, c.get("artist") or "",
                    c.get("title") or "", hidden):
                continue
            seen.add(key)
            # Class swaps ride past the cap: their old row was just removed,
            # so appending the replacement is net-zero.
            if (key not in swapped_ticks
                    and len(parked.candidates) >= parked.CANDIDATE_CAP):
                parked._candidate_cap_noted = True
                continue
            seq = parked._cand_seq
            parked._cand_seq += 1
            parked.candidates.append({
                "cid": f"c{seq}", "seq": seq,
                "kind": c.get("kind", "album"),
                "title": c.get("title") or "?",
                "artist": c.get("artist") or "",
                "detail": c.get("detail") or "",
                "payload": c.get("payload") or {},
                "selected": swapped_ticks.get(key, bool(c.get("selected"))),
            })
            if key not in swapped_ticks:
                added += 1
    return added, updated


def refold_restored_missing(artists, fingerprints):
    """Return just-restored dismissals to the open Library review.

    Dismissing drops rows from the parked job for good, so clearing the
    hidden entries alone leaves Restore looking like a no-op until some
    future scan re-carries them — and most users never scan again. Rebuild
    the restored artists'/albums' candidate specs from the saved scan state
    and fold them back in, unselected. Returns how many rejoined the review,
    or None when no library review is parked (they return on the next scan)."""
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import job_persistence
    from qobuz_librarian.web import jobs as job_mgr

    parked = None
    for job in job_mgr.registry.awaiting_review():
        if getattr(job, "execute_kind", "") != "library":
            continue
        if parked is None or (job.created_at or 0) > (parked.created_at or 0):
            parked = job
    if parked is None:
        return None
    wanted_artists = {(a or "").lower() for a in artists}
    wanted_fps = set(fingerprints)
    specs = []
    state = library_scan_state.kind_state("missing")
    for name, entry in (state.get("artists") or {}).items():
        for spec in (entry or {}).get("candidates") or []:
            artist = spec.get("artist") or name
            title = spec.get("title") or ""
            if ((artist or "").lower() in wanted_artists
                    or hidden_mod.album_fingerprint(artist, title)
                    in wanted_fps):
                spec = dict(spec)
                spec["selected"] = False
                specs.append(spec)
    if not specs:
        return 0
    with parked._lock:
        before = {fold_key(c) for c in parked.candidates}
    if fold_new_candidates(parked, specs) is None:
        return None
    # Same reconciliation the refresh's fold does: saved specs whose folder
    # turns out to exist on disk must not rejoin as "missing".
    drop_owned_missing_candidates(parked)
    with parked._lock:
        added = len({fold_key(c) for c in parked.candidates} - before)
    if added:
        job_persistence.persist(parked)
        parked.notify_review_changed()
    return added


def refold_into_living_review(picks, execute_kind="library", ticked=True):
    """Fold a list of picks back into the newest parked review of
    ``execute_kind``, ticked by default — so they come back ready to retry
    instead of stranding in a dead job. Callers include picks a cancelled
    download never reached, albums that failed during a partial approval, and
    the unticked Gap Fill candidate left by a partial import. The living review
    is the split-off review left behind at approval; when the whole review was
    selected, those paths rebuild or re-park their own review instead. Dedups by
    fold key, so re-adding a pick already in the review is safe. Returns how
    many rejoined, or None when there's no review to fold into."""
    from qobuz_librarian.web import job_persistence
    from qobuz_librarian.web import jobs as job_mgr

    parked = None
    for j in job_mgr.registry.awaiting_review():
        if getattr(j, "execute_kind", "") != execute_kind:
            continue
        if parked is None or (j.created_at or 0) > (parked.created_at or 0):
            parked = j
    if parked is None:
        return None
    specs = [dict(c, selected=True) if ticked else dict(c) for c in picks]
    folded = fold_new_candidates(parked, specs)
    if folded is None:
        return None
    job_persistence.persist(parked)
    parked.notify_review_changed()
    return folded[0]


def _park_library_failures(failed_cands, execute_kind="library",
                           ticked=True, summary=None):
    """After a whole-review download (every candidate ticked) finishes, retire
    empties the living review — so the albums that FAILED to download would
    vanish until the next scan. Re-park them as a fresh living review of
    ``execute_kind``, ticked, so they come back ready to retry (mirrors the
    split-off review left by a partial approval). Persisted so a restart
    keeps them even though the retired baseline no longer rebuilds. Also the
    park half of the unticked instant Gap Fill fold, via ``ticked``/
    ``summary``. Returns the parked job, or None when nothing failed."""
    from qobuz_librarian.web import job_persistence
    from qobuz_librarian.web import jobs as job_mgr
    if not failed_cands:
        return None
    if execute_kind == "new_releases":
        job = job_mgr.Job(title="New-release check",
                          execute_kind="new_releases",
                          status=job_mgr.JobStatus.AWAITING_REVIEW)
    else:
        job = job_mgr.Job(title="Library scan", kind="scan",
                          execute_kind="library",
                          status=job_mgr.JobStatus.AWAITING_REVIEW)
    job._execute_fn = lambda j, chosen: execute_albums(
        j, chosen, load_qobuz_token()[1])
    for c in failed_cands:
        job.add_candidate(
            kind=c.get("kind", "album"),
            title=c.get("title") or "?",
            artist=c.get("artist") or "",
            detail=c.get("detail") or "",
            payload=c.get("payload") or {},
            selected=True if ticked else bool(c.get("selected")),
        )
    n = len(job.candidates)
    if summary is not None:
        job.summary = summary
    elif execute_kind == "new_releases":
        job.summary = (f"{n:,} new release{'s' if n != 1 else ''} didn't "
                       "download last time. Ticked and ready to retry.")
    else:
        job.summary = (f"{n:,} album{'s' if n != 1 else ''} didn't download "
                       "last time. Ticked and ready to retry.")
    job_mgr.registry.add(job)
    job_persistence.persist(job)
    return job


def _return_new_release_picks(picks):
    """A new release stays in the New Releases review until it's downloaded or
    dismissed — so picks that failed, were cancelled, or never ran fold back
    into the parked new-release review, ticked; with none parked (the whole
    review was ticked at approve) they re-park as a fresh one. The persistent
    baseline consumed these ids at check time, so without this they'd never
    be offered as new again."""
    if not picks:
        return
    if refold_into_living_review(picks, execute_kind="new_releases") is None:
        _park_library_failures(picks, execute_kind="new_releases")


def _fold_partial_gap_fill(full_album, artist_name, n_missing):
    """A partial download (some tracks failed) leaves the album on disk with
    gaps. Fold it into the living Library review as an unticked Gap Fill
    candidate right away — the app tracks its own downloads, so the gap must
    not wait for the next manual refresh to become visible. With no library
    review parked, a fresh one is parked so /library shows it. Runs after
    prune_library_review_candidates, which just dropped the album's stale
    Missing candidates — this replaces them with the honest remainder."""
    spec = _album_candidate_spec(
        {**full_album, "_partial_missing_count": n_missing},
        artist_name, selected=False)
    if refold_into_living_review([spec], ticked=False) is None:
        _park_library_failures(
            [spec], ticked=False,
            summary="1 album downloaded only partly — the missing tracks "
                    "are ready as Gap Fill.")


def prune_library_review_candidates(album):
    """A full album just landed on disk (Search download, batch download,
    upgrade replace): drop its candidates from every parked or still-scanning
    library review, so a stale review can't offer to download an album the
    user already has. The executing job itself is RUNNING and untouched.
    Matched by Qobuz album id. Returns the number of candidates dropped."""
    from qobuz_librarian.web import job_persistence
    from qobuz_librarian.web import jobs as job_mgr
    album_id = str((album or {}).get("id") or "")
    if not album_id:
        return 0
    dropped = 0
    states = (job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)
    for job in job_mgr.registry.all():
        if (getattr(job, "execute_kind", "") not in ("library", "new_releases")
                or job.status not in states):
            continue
        try:
            with job._lock:
                keep = [c for c in job.candidates
                        if str((c.get("payload") or {}).get("album_id") or "")
                        != album_id]
                n = len(job.candidates) - len(keep)
                if not n:
                    continue
                job.candidates = keep
            dropped += n
            job_persistence.persist(job)
            job.notify_review_changed()
            job_mgr.finalize_review_if_empty(job)
        except Exception as e:
            # Pruning is housekeeping on the side of a successful download —
            # never let it turn that success into a failure.
            log.info(f"  couldn't prune review {job.id}: {e}")
    return dropped


def drop_owned_missing_candidates(job):
    """Reconcile a parked library review against the disk right before it
    executes: a missing-album candidate whose folder now holds music (grabbed
    from Search while the review sat parked, or added by hand) is dropped
    instead of downloaded again. A folder that merely shares the album's name
    but holds no audio — a failed download, deleted tracks, a leftover shell —
    is NOT ownership: the scanner reads tracks and rightly still lists that
    album missing, so dropping it here would silently hide a real gap. Gap Fill
    candidates are left alone — their album folder exists by definition;
    full-album imports already prune them via prune_library_review_candidates.
    Returns how many of the dropped candidates were selected (the number the
    user believes they're about to download)."""
    from qobuz_librarian.library.catalog import _count_audio_files_in, find_album_dir_filesystem
    from qobuz_librarian.web import job_persistence
    with job._lock:
        snapshot = [(c["cid"], bool(c.get("selected")),
                     {"id": (c.get("payload") or {}).get("album_id"),
                      "title": c.get("title") or "",
                      "artist": {"name": c.get("artist") or ""}})
                    for c in job.candidates if not is_gap_candidate(c)]
    # Disk probes happen outside the lock; a live scan can keep appending.
    owned = {}
    for cid, selected, alb in snapshot:
        try:
            d = find_album_dir_filesystem(alb)
            if d is not None and _count_audio_files_in(d) > 0:
                owned[cid] = selected
        except Exception:
            continue
    if not owned:
        return 0
    with job._lock:
        job.candidates = [c for c in job.candidates if c["cid"] not in owned]
    job_persistence.persist(job)
    job.notify_review_changed()
    return sum(1 for sel in owned.values() if sel)


def _record_last_scan():
    try:
        cfg.LAST_SCAN_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _load_scan_seen(mode):
    """Fingerprints the last completed walk of this mode surfaced, or None if
    there's no prior run to compare against (first scan badges nothing)."""
    try:
        data = json.loads(cfg.SCAN_SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bucket = data.get(mode) if isinstance(data, dict) else None
    return set(bucket) if isinstance(bucket, list) else None


def _save_scan_seen(mode, fingerprints):
    try:
        data = json.loads(cfg.SCAN_SEEN_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[mode] = sorted(fingerprints)
    try:
        cfg.SCAN_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.SCAN_SEEN_FILE.with_suffix(cfg.SCAN_SEEN_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(cfg.SCAN_SEEN_FILE)
    except OSError:
        pass


def _flag_new_since_last_scan(job, mode):
    """Badge candidates whose album wasn't surfaced by the previous walk, then
    record this walk's set for next time. First-ever run badges nothing (no
    baseline to diff). Skipped on a cancelled scan so a partial run can't
    poison the baseline."""
    # Snapshot under the lock — the scan worker appends candidates to this
    # list and we walk it twice here, which without a snapshot is not safe
    # against a same-instant append. `dismiss_albums` uses the same pattern.
    with job._lock:
        candidates = list(job.candidates)
    seen_now = set()
    fps = {}
    for c in candidates:
        fp = hidden_mod.album_fingerprint(c.get("artist"), c.get("title"))
        if fp:
            seen_now.add(fp)
            fps[c["cid"]] = fp
    prev = _load_scan_seen(mode)
    if prev is not None:
        for c in candidates:
            fp = fps.get(c["cid"])
            if fp and fp not in prev:
                c["payload"]["is_new"] = True
    _save_scan_seen(mode, seen_now)


def dismiss_albums(job, artist, scope=hidden_mod.SCOPE_MISSING, gap_only=None,
                   query=""):
    """Apply one hide action only while its review remains live."""
    with job._review_action_lock:
        return _dismiss_albums_locked(
            job,
            artist,
            scope=scope,
            gap_only=gap_only,
            query=query,
        )


def _dismiss_albums_locked(job, artist, scope=hidden_mod.SCOPE_MISSING,
                           gap_only=None, query=""):
    """Hide ``artist``'s albums that aren't currently selected, in ``scope``.

    Selection is server-backed (saved as the user ticks), so "hide the rest"
    means: of this artist's candidates, hide the ones whose saved `selected`
    flag is off and keep the ticked ones. Other artists' candidates and their
    selections are never touched — critical now that pagination means most of
    them aren't even on the page that triggered the hide.

    ``gap_only`` narrows the hide to one side of a library review's tab split:
    True drops only Gap Fill candidates, False only fully missing albums, None
    (the default) both. The button only ever shows one tab's rows, so it must
    not silently dismiss the other tab's. ``query`` narrows the same way for
    an active review filter — only the rows the user can see leave.

    The hidden albums are recorded in the durable store so future bulk walks of
    that scope skip them, then dropped from this job's review list. Returns the
    number hidden.
    """
    from qobuz_librarian.web import job_persistence
    from qobuz_librarian.web import jobs as job_mgr

    # Snapshot + mutate under the lock in one go: a live scan appends candidates
    # from the worker thread, so reading job.candidates and replacing it in
    # separate steps could drop a concurrently-added album.
    with job._lock:
        if job.status not in (
            job_mgr.JobStatus.AWAITING_REVIEW,
            job_mgr.JobStatus.SCANNING,
        ):
            return None
        # The store's key deliberately ignores parentheticals so editions of one
        # album stay dismissed together, but that also collapses genuinely
        # different records — "The Asylum Albums (1972-1975)" and "(1976-1980)"
        # share a key. Writing an unticked row's key would hide the ticked one
        # it collides with, for good and under the other album's title. A ticked
        # album wins: its twin is left in the review rather than dismissed into
        # a key that cannot tell them apart, so nothing the user kept disappears
        # and the count that leaves matches the count recorded.
        kept = set()
        for c in job.candidates:
            if c.get("artist") == artist and c.get("selected"):
                fp = hidden_mod.album_fingerprint(c.get("artist"), c.get("title"))
                if fp:
                    kept.add(fp)
        to_hide = [c for c in job.candidates
                   if c.get("artist") == artist and not c.get("selected")
                   and (gap_only is None or is_gap_candidate(c) == gap_only)
                   and (not query or candidate_matches_query(c, query))
                   and hidden_mod.album_fingerprint(
                       c.get("artist"), c.get("title")) not in kept]
        if not to_hide:
            return 0
        specs = [(c.get("artist"), c.get("title"),
                  (c.get("payload") or {}).get("year")) for c in to_hide]
    # Record the dismissals durably FIRST, outside the lock (disk I/O mustn't
    # stall the scan thread's next add_candidate).
    hidden_mod.hide(scope, specs)
    drop = {c["cid"] for c in to_hide}
    with job._lock:
        # Re-read the ticks under the lock: a selection saved while the store
        # write above ran was promised "keep the ticked ones", so it wins —
        # dropping the stale snapshot's ids wholesale would dismiss an album
        # the user just ticked.
        ticked_meanwhile = [c for c in job.candidates
                            if c["cid"] in drop and c.get("selected")]
        keep = {c["cid"] for c in ticked_meanwhile}
        # Only this artist's unselected candidates leave; every other
        # candidate (and its saved selection) is preserved untouched.
        job.candidates = [c for c in job.candidates
                          if c["cid"] not in drop or c["cid"] in keep]
    if ticked_meanwhile:
        # Their dismissals were already written durably — take them back out,
        # or the next bulk walk skips albums that are visibly ticked here.
        fps = [hidden_mod.album_fingerprint(c.get("artist"), c.get("title"))
               for c in ticked_meanwhile]
        hidden_mod.restore_albums(scope, [fp for fp in fps if fp])
    job_persistence.persist(job)
    return len(to_hide) - len(ticked_meanwhile)


# ── Scans ─────────────────────────────────────────────────────────────────────


def _scan_library_artist(artist_dir, token, partial_only, hidden):
    """Worker: find one artist's gaps. Runs in a pool thread (its own HTTP
    session); returns plain data so the caller adds candidates serially —
    keeping job.candidates single-writer. Also returns the artist's id and its
    lossless catalog ids so the caller can seed the new-release baseline (the
    discography is already fetched here)."""
    result = find_missing_for_artist(
        artist_dir.name, token=token,
        opts=DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES),
        artist_dir=artist_dir, hidden=hidden,
        single_store=hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None,
        want_missing=not partial_only)
    artist_id = str(result.artist_id) if result.artist_id else None
    # None signals "don't seed a baseline" — a transient short-page fetch
    # isn't the whole discography, so seeding it would later dump the dropped
    # albums as "new".
    catalog_ids = None if result.catalog_incomplete else [
        str(a["id"]) for a in result.catalog
        if is_lossless_album(a) and a.get("id") is not None]
    return artist_dir.name, result.artist_name, result.gaps, artist_id, catalog_ids


_CHECKPOINT_EVERY = 15  # artists between progress saves (resume granularity)
# Seconds between live-status refreshes during the whole-library repair sweep
# (see scan_repairs).
_REPAIR_HEARTBEAT_SECS = 2


def scan_library(job, token, partial_only=False, force_full=False):
    clear_scan_caches()
    # Drop the Various-Artists folder: it has no single Qobuz artist catalog
    # to diff against, so a gap scan can only mis-resolve it.
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        return
    kind = "partial" if partial_only else "missing"
    # Resume an interrupted scan of this kind: skip the artists already done and
    # restore the albums they turned up, so we continue rather than restart.
    cp = scan_checkpoint.load(kind)
    resuming = cp is not None
    checkpoint_artists = dict(cp.get("artists") or {}) if resuming else {}
    current_artist_names = {ad.name for ad in artists}
    if resuming:
        scanned = set()
        baseline_seen = {}
        for name in set(cp["scanned"]):
            if name not in current_artist_names:
                continue
            saved = checkpoint_artists.get(name)
            if not isinstance(saved, dict) or saved.get("catalog_ids") is None:
                continue
            scanned.add(name)
            artist_id = saved.get("artist_id") or ""
            if artist_id:
                baseline_seen[str(artist_id)] = list(saved.get("catalog_ids") or [])
    else:
        scanned = set()
        baseline_seen = {}
    total = 0
    # Snapshot the dismissed-album memory before restoring the checkpoint so
    # albums the user dismissed since the interruption are not re-added, and
    # so the parallel workers below see the same consistent view.
    hidden = hidden_mod.load()
    hidden_sig = library_scan_state.hidden_signature(
        hidden, hidden_mod.SCOPE_MISSING)
    quality_sig = library_scan_state.quality_signature()
    previous_scan = library_scan_state.kind_state(kind)
    cheap_refresh = (
        not force_full
        and not resuming
        and previous_scan.get("complete")
        and previous_scan.get("hidden_signature", "") == hidden_sig
        # Saved candidates computed under a different quality policy must not
        # carry forward — a settings change re-derives even unchanged folders.
        and previous_scan.get("quality_signature", "") == quality_sig
    )
    # The two refreshes and the fingerprint pass below run before the main
    # artist loop, and on a first scan of a large library each takes real
    # minutes — without progress ticks the job sits on "Waiting for output"
    # looking hung the whole time.
    downsample_refresh_started_at = time.time()
    log.info(f"Reading albums from {plural(len(artists), 'artist folder')} on disk…")
    downsample_refresh = downsample_state.refresh_for_artists(
        artists,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        persist=False,
        skip_unchanged=cheap_refresh,
        on_artist=lambda ad, _specs, _err, done_i, total_i: job.push_progress(
            "Reading albums on disk", done_i, total_i, ad.name, unit="artist"),
    )
    upgrade_refresh = None
    upgrade_refresh_started_at = None
    if not job.cancel_requested and cfg.UPGRADE_SCAN_ENABLED:
        from qobuz_librarian.quality.decision import load_capped
        from qobuz_librarian.web.jobs import pool_initializer_kwargs
        upgrade_refresh_started_at = time.time()
        log.info("Comparing owned albums against the editions Qobuz can serve…")
        upgrade_refresh = upgrade_state.refresh_for_artists(
            artists,
            token=token,
            args=build_args(),
            capped=load_capped(),
            hidden=hidden,
            cancel_check=lambda: bool(job.cancel_requested),
            workers=max(1, int(cfg.ARTIST_SCAN_WORKERS)),
            pool_kwargs=pool_initializer_kwargs(),
            skip_unchanged=cheap_refresh,
            persist=False,
            on_artist=lambda ad, _specs, _err, done_i, total_i: job.push_progress(
                "Checking upgrade quality", done_i, total_i, ad.name, unit="artist"),
        )
    elif not cfg.UPGRADE_SCAN_ENABLED:
        review_badges.set_ready("upgrade", False)
    target = "Gap Fill candidates in owned albums" if partial_only else "missing albums"
    log.info(f"Scanning {plural(len(artists), 'library artist')} for {target}")
    fingerprints = {}
    for _i, _ad in enumerate(artists, 1):
        fingerprints[_ad.name] = artist_fingerprint(_ad)
        if _i % 25 == 0 or _i == len(artists):
            job.push_progress("Fingerprinting artist folders", _i, len(artists),
                              _ad.name, unit="folder")
    previous_artists = (previous_scan.get("artists") or {}) if cheap_refresh else {}
    state_artists: dict[str, dict] = {}
    if resuming:
        restored_by_artist: dict[str, list[dict]] = {}
        for c in cp["candidates"]:
            artist_key = (c.get("payload") or {}).get("_artist_dir") or c.get("artist")
            if artist_key not in scanned:
                continue
            if hidden_mod.is_hidden(hidden_mod.SCOPE_MISSING,
                                    c.get("artist"), c.get("title"), hidden):
                continue
            _readd_candidate(job, c)
            total += 1
            if artist_key:
                restored_by_artist.setdefault(artist_key, []).append(c)
        for name in scanned:
            saved = checkpoint_artists.get(name)
            if not isinstance(saved, dict):
                continue
            catalog_ids = saved.get("catalog_ids")
            if catalog_ids is None:
                continue
            candidates = [
                c for c in saved.get("candidates", [])
                if not hidden_mod.is_hidden(
                    hidden_mod.SCOPE_MISSING,
                    c.get("artist"),
                    c.get("title"),
                    hidden,
                )
            ]
            state_artists[name] = {
                "fingerprint": saved.get("fingerprint") or fingerprints.get(name, ""),
                "candidates": candidates or restored_by_artist.get(name, []),
                "artist_id": saved.get("artist_id") or "",
                "catalog_ids": list(catalog_ids or []),
            }
        log.info(f"Resuming. {plural(len(scanned), 'artist')} already scanned, "
                 f"{plural(total, 'album')} found so far.")
    todo = []
    n = len(artists)
    done = len(scanned)
    reused = 0
    scan_errors = 0
    for artist_dir in artists:
        if artist_dir.name in scanned:
            continue
        saved = previous_artists.get(artist_dir.name)
        saved_catalog_ids = saved.get("catalog_ids") if saved else None
        if (
            saved
            and saved_catalog_ids is not None
            and saved.get("fingerprint") == fingerprints.get(artist_dir.name)
            and (saved.get("artist_id") or not saved.get("candidates"))
        ):
            candidates = [
                c for c in saved.get("candidates", [])
                if not hidden_mod.is_hidden(
                    hidden_mod.SCOPE_MISSING,
                    c.get("artist"),
                    c.get("title"),
                    hidden,
                )
            ]
            for c in candidates:
                _readd_candidate(job, c)
                total += 1
            scanned.add(artist_dir.name)
            done += 1
            reused += 1
            if saved.get("artist_id"):
                baseline_seen[str(saved["artist_id"])] = list(saved_catalog_ids or [])
            state_artists[artist_dir.name] = {
                "fingerprint": fingerprints.get(artist_dir.name, ""),
                "candidates": candidates,
                "artist_id": saved.get("artist_id") or "",
                "catalog_ids": list(saved_catalog_ids or []),
            }
            hit = ({"artist": artist_dir.name, "albums": len(candidates)}
                   if candidates else None)
            job.push_progress("Scanning library", done, n, artist_dir.name,
                              found=total, hit=hit, unit="artist")
        else:
            todo.append(artist_dir)
    if reused:
        log.info(f"  Reused {plural(reused, 'unchanged artist')} from the saved scan.")
    since_save = 0
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Resolve/scan artists in parallel (each worker has its own HTTP session),
    # but collect results and write candidates on this one thread so the
    # candidate list and progress stay single-writer.
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="libscan",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(_scan_library_artist, ad, token, partial_only,
                             hidden): ad
                   for ad in todo}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                log.info("Cancelled. Stopping scan.")
                break
            done += 1
            try:
                name, artist_name, gaps, artist_id, catalog_ids = fut.result()
            except (AuthLost, QobuzUnavailable):
                # A lost token or an unreachable API isn't a per-artist hiccup
                # — cancel the rest and fail the scan rather than silently
                # report a partial library as the full picture.
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                scan_errors += 1
                log.info(f"    skipped {futures[fut].name}: {e}")
                job.push_progress("Scanning library", done, n, futures[fut].name,
                                  found=total, unit="artist")
                continue
            scanned.add(name)
            if artist_id and catalog_ids is not None:
                baseline_seen[artist_id] = catalog_ids
            artist_candidates = []
            for gap in gaps:
                # Library is a discovery list — leave candidates unticked so a
                # single click can't queue hundreds nobody reviewed.
                spec = _gap_candidate_spec(
                    gap, artist_name or name, selected=False, artist_key=name)
                if _add_candidate_spec(job, spec) is not None:
                    artist_candidates.append(spec)
                    total += 1
            if catalog_ids is not None:
                state_artists[name] = {
                    "fingerprint": fingerprints.get(name, ""),
                    "candidates": artist_candidates,
                    "artist_id": artist_id or "",
                    "catalog_ids": list(catalog_ids or []),
                }
            # Add the albums before the progress tick so a hit lands the live
            # preview the same moment the running total moves.
            hit = ({"artist": artist_name or name, "albums": len(gaps)}
                   if gaps else None)
            job.push_progress("Scanning library", done, n, artist_name or name,
                              found=total, hit=hit, unit="artist")
            if gaps:
                tail = "with Gap Fill candidates" if partial_only else "to fill"
                log.info(f"  {artist_name} — {plural(len(gaps), 'album')} {tail}")
            since_save += 1
            if since_save >= _CHECKPOINT_EVERY:
                since_save = 0
                scan_checkpoint.save(
                    kind, scanned, job.candidates, baseline_seen, state_artists)
    # Reached here only without an AuthLost/outage abort (that re-raises out
    # above, leaving the checkpoint for resume and not seeding the baseline).
    flush_resolve_cache()
    baseline_save_failed = False
    if job.cancel_requested:
        # Deliberate stop — discard this kind's progress so it isn't auto-resumed.
        scan_checkpoint.clear(kind)
    else:
        # Only a reached-all-artists crawl stamps "last scanned" or seeds the
        # new-release baseline.
        catalog_complete = (
            len(state_artists) == len(artists)
            and len(scanned) == len(artists)
            and scan_errors == 0
        )
        library_complete = (
            catalog_complete
            and not job.candidate_cap_hit
        )
        library_scan_state.save_kind(
            kind,
            artists=state_artists,
            complete=library_complete,
            hidden_signature=hidden_sig,
            quality_sig=quality_sig,
        )
        # Publish Upgrade/Downsample from their OWN completeness, gated only
        # on a complete catalog crawl — NOT on library_complete.
        if catalog_complete:
            if downsample_refresh.complete:
                downsample_state.save(
                    downsample_refresh,
                    preserve_concurrent=True,
                    refresh_started_at=downsample_refresh_started_at,
                )
                review_badges.set_ready(
                    "downsample", _surface_has_candidates("downsample"))
            if upgrade_refresh is not None and upgrade_refresh.complete:
                upgrade_state.save(
                    upgrade_refresh,
                    preserve_concurrent=True,
                    refresh_started_at=upgrade_refresh_started_at,
                )
                review_badges.set_ready(
                    "upgrade", _surface_has_candidates("upgrade"))
        if catalog_complete:
            _record_last_scan()
            _flag_new_since_last_scan(job, kind)
            # The crawl reached every artist cleanly — establish the new-release
            # baseline from the catalog snapshot (only the first time; the daily
            # check keeps it fresh after), and clear this kind's checkpoint.
            if not new_releases_mod.is_baseline_complete():
                baseline_save_failed = (
                    new_releases_mod.seed_baseline(baseline_seen) is False)
            scan_checkpoint.clear(kind)
        elif scanned or job.candidates or baseline_seen:
            scan_checkpoint.save(
                kind, scanned, job.candidates, baseline_seen, state_artists)
    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    elif partial_only:
        job.summary = (library_review_summary(job.candidates) + "." + _cap_note(job)
                       if total else "No Gap Fill candidates found in your owned albums.")
    else:
        job.summary = (library_review_summary(job.candidates) + "." + _cap_note(job)
                       if total else
                       "No missing albums found for artists in your library.")
    # Artists that errored or came back with a short catalog page aren't in
    # state_artists; the checkpoint stays for them and the last-scan stamp is
    # withheld.
    unchecked = len(artists) - len(state_artists)
    if not job.cancel_requested and unchecked > 0:
        job._unchecked_artists = unchecked
        job.summary += (f" {plural(unchecked, 'artist')} couldn't be checked; "
                        "scan again to resume from where it left off.")
    if baseline_save_failed:
        job.summary += (" The New Releases baseline couldn't be saved; "
                        "the next complete scan will try again.")
    log.info(job.summary)


def scan_new_releases(job, token):
    """Surface albums that appeared in library artists' Qobuz catalogs since the
    last check and that the user doesn't own or hasn't hidden — flagged as new
    for review, but left un-ticked so one click can't queue the whole list.
    Cheap (one catalog call per artist, no track fetches), so it's the quick
    "what's new" pass rather than the full gap scan."""
    clear_scan_caches()
    # Same VA exclusion as scan_library: the Various-Artists folder has no single
    # Qobuz catalog, so it can't yield meaningful "new releases".
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        return
    state = new_releases_mod.load()
    seen = state.get("seen") or {}
    # If the catalog fetch limit has grown since the baseline was captured,
    # the old baseline is missing everything past the previous cap — a plain
    # diff would dump that whole back-slice as "new".
    cur_limit = int(cfg.ARTIST_CATALOG_LIMIT)
    prev_limit = state.get("baseline_limit")
    rebaseline = prev_limit is None or cur_limit > int(prev_limit)
    hidden = hidden_mod.load()
    single_store = hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None
    opts = DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES)
    log.info(f"Checking {plural(len(artists), 'artist')} for new releases…")
    total = 0
    done = 0
    failed_count = 0
    n = len(artists)
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # This run's reached artists; merged over the prior baseline at the end (so a
    # run where some/all artists errored can't wipe their baselines and re-surface
    # everything — only artists actually reached get their snapshot refreshed).
    current_seen = {}
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="newrel",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(find_new_releases_for_artist, ad.name, token=token,
                             opts=opts, seen_by_id=seen, hidden=hidden,
                             single_store=single_store, artist_dir=ad,
                             baseline_only=rebaseline): ad
                   for ad in artists}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                log.info("Cancelled. Stopping check.")
                break
            done += 1
            try:
                result = fut.result()
            except (AuthLost, QobuzUnavailable):
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                failed_count += 1
                log.info(f"    skipped {futures[fut].name}: {e}")
                job.push_progress("Checking for new releases", done, n,
                                  futures[fut].name, found=total, unit="artist")
                continue
            if result.artist_id and not getattr(result, "fetch_failed", False):
                current_seen[result.artist_id] = result.current_ids
            else:
                # An unresolved artist and a short/failed catalogue fetch are
                # both unchecked, not clean zero-release results.
                failed_count += 1
            for gap in result.new_gaps:
                # Leave new releases UN-ticked, like the library gap list: a
                # review is for picking, and one tap must never queue the whole
                # list of rips. The "new" badge still flags them for the eye.
                _add_gap_candidate(job, gap, result.artist_name,
                                   selected=False, is_new=True)
                total += 1
            hit = ({"artist": result.artist_name, "albums": len(result.new_gaps)}
                   if result.new_gaps else None)
            job.push_progress("Checking for new releases", done, n,
                              result.artist_name or futures[fut].name,
                              found=total, hit=hit, unit="artist")
            if result.new_gaps:
                log.info(f"  {result.artist_name} — "
                         f"{plural(len(result.new_gaps), 'new release')}")
    flush_resolve_cache()
    saved = None
    if not job.cancel_requested:
        # UNION each reached artist's snapshot into the prior baseline rather
        # than replacing it.
        merged = dict(seen)
        for aid, ids in current_seen.items():
            merged[aid] = sorted(set(merged.get(aid, [])) | set(ids))
        complete = failed_count == 0 and bool(current_seen)
        saved = new_releases_mod.mark_run(
            merged,
            complete=complete,
            baseline_limit=cur_limit if complete else None,
        )
    if job.cancel_requested:
        # A cancelled crawl only reached a fraction of the artists, so it can't
        # claim "No new releases" or "First check recorded" definitively.
        job.summary = ("Stopped early. Partial check, "
                       f"{plural(total, 'new release')} found so far.")
        log.info(job.summary)
        return
    if failed_count:
        job._unchecked_artists = failed_count

    if rebaseline and seen:
        if saved is False:
            job.summary = ("The fresh catalogue baseline couldn't be saved. "
                           "Check again to retry it.")
            if failed_count:
                job.summary += (f" {plural(failed_count, 'artist')} also "
                                "couldn't be checked.")
        elif failed_count:
            job.summary = ("Catalogue rebaseline incomplete. "
                           f"{plural(failed_count, 'artist')} couldn't be "
                           "checked; check again to retry them.")
        else:
            job.summary = ("Catalogue limit changed. Recorded a fresh baseline. "
                           "Future checks will flag new releases.")
    elif total:
        job.summary = f"{plural(total, 'new release')} found across the library."
        if failed_count:
            job.summary += (f" {plural(failed_count, 'artist')} couldn't be "
                            "checked; check again for a complete result.")
        if saved is False:
            job.summary += (" The updated baseline couldn't be saved, so these "
                            "releases may appear again.")
    elif failed_count:
        job.summary = ("No new releases from the artists that could be checked. "
                       f"{plural(failed_count, 'artist')} couldn't be checked; "
                       "check again for a complete result.")
        if saved is False:
            job.summary += " The partial baseline update couldn't be saved."
    elif not seen:
        if saved is False:
            job.summary = ("The current Qobuz catalogue was checked, but the "
                           "starting baseline couldn't be saved. Check again.")
        else:
            job.summary = ("First check complete. Recorded the current Qobuz "
                           "catalogue baseline. Future checks will flag new releases.")
    elif saved is False:
        job.summary = ("No new releases found, but the updated baseline couldn't "
                       "be saved. Check again.")
    else:
        job.summary = "No new releases added to your saved baseline."
    log.info(job.summary)


# ── Execute ───────────────────────────────────────────────────────────────────

def _note_staging_wait(job, phase, current, total):
    """If a long staging-lock holder (a library-wide Lyrics scan) owns the mutex
    right now, show that this job is waiting behind it. Without this the album
    sits on RUNNING with no visible reason until the holder releases the lock;
    the note is replaced by real progress the moment the lock is acquired."""
    from qobuz_librarian.web.jobs import staging_holder
    holder = staging_holder()
    if holder:
        job.push_progress(phase, current, total,
                          f"waiting for {holder} to finish…", unit="album")


def execute_albums(job, chosen, token):
    """Download each selected album via the normal process_album path."""
    from qobuz_librarian.modes.process import process_album
    from qobuz_librarian.web.jobs import staging_lock

    # The web worker runs jobs back-to-back; a directory listing cached by
    # a previous job would otherwise be reused even though folders may
    # have moved since.
    clear_scan_caches()
    args = build_args()
    _benign = {"already_complete", "skipped_already_higher_quality", "dry_run",
               "user_skipped", "lossy_only", "no_tracks", "skipped_has_extras",
               "cancelled"}
    ok = 0
    partial = 0
    failed = 0
    processed = 0
    # Picks that didn't land (never fetched, errored, or came back empty).
    failed_cands = []
    cancelled_cand = None
    # Fold-back recovery is kind-scoped: this function executes both library
    # and new-release batches, and their picks must never cross into each
    # other's review — a library pick returns to the Library review, a
    # new-release pick to the New Releases review (it stays there until
    # downloaded or dismissed).
    is_library_run = getattr(job, "execute_kind", "") == "library"
    is_nr_run = getattr(job, "execute_kind", "") == "new_releases"

    def _fold_back_unfinished():
        # Token death / outage mid-batch: once something has imported, this
        # job is headed for FAILED (approve's no-harm re-park only covers the
        # nothing-landed case) — fold the picks this run didn't finish back
        # into the living review, ticked, so they come back to retry like any
        # other early exit.
        if not job._imported_any:
            return
        leftovers = failed_cands + [cand] + chosen[processed:]
        if is_library_run:
            refold_into_living_review(leftovers)
        elif is_nr_run:
            _return_new_release_picks(leftovers)
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_id = cand["payload"].get("album_id")
        label = f"[{i}/{len(chosen)}] {cand.get('artist','')} — {cand['title']}"
        log.info(label)
        job._progress_scope = (i, len(chosen), "album")
        job.push_progress("Downloading albums", i, len(chosen),
                          f"{cand.get('artist','')} — {cand['title']}", unit="album")
        try:
            full = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            _fold_back_unfinished()
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            failed_cands.append(cand)
            continue
        _note_staging_wait(job, "Downloading albums", i, len(chosen))
        try:
            with staging_lock():
                result = process_album(full, args, allow_force=False,
                                       already_confirmed=True, token=token)
        except (AuthLost, QobuzUnavailable):
            _fold_back_unfinished()
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            failed_cands.append(cand)
            continue
        if result and result.get("result") == "cancelled":
            # Cancelled mid-download: processed already points past this album,
            # so the cancel fold-back below would drop its pick — remember it.
            cancelled_cand = cand
        if result and result.get("imported") and result.get("n_ok", 0) > 0:
            job._imported_any = True
            _refresh_after_local_album_change(
                full,
                result,
                fallback_artist=cand.get("artist"),
                downsample=True,
            )
            # The album is on disk now — any OTHER parked library review still
            # offering it is stale.
            prune_library_review_candidates(full)
            # A partial (some tracks landed, some failed) isn't a full
            # download — count it apart so the summary doesn't claim it
            # finished, and surface the remainder NOW (after the prune, which
            # would otherwise drop the fresh candidate as a same-id stale one)
            # instead of leaving it invisible until the next manual refresh.
            if result.get("n_fail", 0) > 0:
                partial += 1
                if is_nr_run:
                    _return_new_release_picks([cand])
                elif is_library_run:
                    _fold_partial_gap_fill(full, cand.get("artist") or "",
                                           result.get("n_fail", 0))
            else:
                ok += 1
        elif not (result and result.get("result") in _benign):
            failed += 1
            failed_cands.append(cand)
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if job.cancel_requested:
        # The picks this run never started aren't lost: fold them back into
        # the living review, ticked, so a cancel mid-batch doesn't strand them
        # in this dead job.
        unrun = chosen[processed:]
        if cancelled_cand is not None:
            unrun = [cancelled_cand] + unrun
        if unrun and is_library_run:
            refold_into_living_review(unrun)
        elif unrun and is_nr_run:
            _return_new_release_picks(unrun)
        job.summary = (f"Stopped early. {ok} downloaded, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    parts = [f"{ok}/{plural(len(chosen), 'album')} downloaded and imported"]
    if partial:
        parts.append(f"{plural(partial, 'album')} only partly (some tracks failed)")
    job.summary = "Finished. " + ", ".join(parts) + "."
    log.info(f"Finished. {ok}/{plural(len(chosen), 'album')} downloaded and imported.")
    if partial:
        log.info(f"  {plural(partial, 'album')} downloaded only partly "
                 f"(some tracks failed); see the log.")
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} didn't finish; see the log."
    # A whole-review download (every candidate ticked, nothing re-parked at
    # approval) consumed the entire living review.
    if getattr(job, "_consumed_whole_review", False):
        if failed_cands:
            _park_library_failures(failed_cands)
        from qobuz_librarian.library import library_scan_state
        library_scan_state.mark_review_retired(reason="worked_through")
    elif failed_cands and is_library_run:
        # Partial approve: the unticked picks stayed behind as a living split-
        # off review.
        refold_into_living_review(failed_cands)
    elif failed_cands and is_nr_run:
        # A new release that failed to download wasn't downloaded — it goes
        # back to the New Releases review rather than being consumed (the
        # baseline already recorded it, so nothing else would re-offer it).
        _return_new_release_picks(failed_cands)


# ── Upgrade flow ──────────────────────────────────────────────────────────────

def scan_upgrades(job, token):
    """Scan the library for albums Qobuz can serve at higher quality."""
    from qobuz_librarian.quality.decision import load_capped

    if not cfg.UPGRADE_SCAN_ENABLED:
        review_badges.set_ready("upgrade", False)
        job.summary = "Upgrade scanning is turned off."
        log.info(job.summary)
        return
    clear_scan_caches()
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        return
    args = build_args()
    capped = load_capped()
    # Upgrades the user dismissed ("I'm happy with my copy") — independent of
    # the auto-`capped` memory and of the missing-album hides.
    hidden = hidden_mod.load()
    log.info(f"Scanning {plural(len(artists), 'artist')} for quality upgrades")
    total = 0
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    from qobuz_librarian.web.jobs import pool_initializer_kwargs

    def _on_artist(ad, specs, error, done, n):
        nonlocal total
        name = ad.name
        if isinstance(error, (AuthLost, QobuzUnavailable)):
            raise error
        if error is not None:
            log.info(f"    skipped {name}: {error}")
            job.push_progress("Scanning for upgrades", done, n, name,
                              found=total, unit="artist")
            return
        added = 0
        current_hidden = hidden_mod.load()
        for spec in specs:
            if hidden_mod.is_hidden(
                    hidden_mod.SCOPE_UPGRADE,
                    spec.get("artist") or name,
                    spec.get("title"),
                    current_hidden):
                continue
            # Unticked by default — like the gap scan, one click shouldn't
            # re-rip hundreds of albums nobody reviewed.
            job.add_candidate(
                kind="upgrade",
                title=spec.get("title") or "?",
                artist=spec.get("artist") or name,
                detail=spec.get("detail") or "",
                payload=spec.get("payload") or {},
                selected=False,
            )
            total += 1
            added += 1
        hit = {"artist": name, "albums": added} if added else None
        job.push_progress("Scanning for upgrades", done, n, name,
                          found=total, hit=hit, unit="artist")
        if added:
            log.info(f"  {name} — {plural(added, 'album')} to upgrade")

    refresh = upgrade_state.refresh_for_artists(
        artists,
        token=token,
        args=args,
        capped=capped,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        on_artist=_on_artist,
        workers=workers,
        pool_kwargs=pool_initializer_kwargs(),
    )
    if not job.cancel_requested and refresh.complete:
        review_badges.set_ready("upgrade", _surface_has_candidates("upgrade"))
    if job.cancel_requested or not refresh.complete:
        log.info("Cancelled. Stopping scan.")
    if not job.cancel_requested:
        _flag_new_since_last_scan(job, "upgrade")
    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    else:
        job.summary = (f"{plural(total, 'upgradeable album')} Qobuz can serve "
                       "at higher quality." + _cap_note(job) if total else
                       "No upgrades; every album is already at the best quality "
                       "Qobuz offers.")
    log.info(job.summary)


def execute_upgrades(job, chosen, token):
    """Re-rip the present tracks of each chosen album at higher quality."""
    from qobuz_librarian.modes.process import process_album
    from qobuz_librarian.modes.upgrade import BENIGN_UPGRADE_RESULTS
    from qobuz_librarian.web.jobs import staging_lock

    clear_scan_caches()
    args = build_args()
    # Explicit upgrade: enable the replace path for this run only, and turn
    # off per-album consolidation prompts (the CLI upgrade walk does the same).
    args.auto_upgrade = True
    args.consolidate = False
    # Outcomes that aren't a failure — the album just didn't need (or couldn't
    # safely take) an upgrade.
    _skip = BENIGN_UPGRADE_RESULTS
    ok = 0
    kept = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_id = cand["payload"].get("album_id")
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist','')} — "
                 f"{cand.get('title') or '?'}")
        job._progress_scope = (i, len(chosen), "album")
        job.push_progress("Upgrading albums", i, len(chosen),
                          f"{cand.get('artist','')} — {cand.get('title') or '?'}", unit="album")
        try:
            album = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            continue
        if not album:
            log.info(f"  album {album_id} is no longer on Qobuz; skipping.")
            failed += 1
            continue
        _note_staging_wait(job, "Upgrading albums", i, len(chosen))
        try:
            with staging_lock():
                result = process_album(album, args, allow_force=False,
                                       already_confirmed=True,
                                       upgrade_only=True, token=token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        _res = (result or {}).get("result")
        if result and result.get("upgrade_unverified"):
            # Imported, but the rebuilt folder couldn't be verified as
            # complete as the original, so the backup was kept.
            kept += 1
            job._imported_any = True
        elif result and result.get("imported") and _res not in (
                _skip | {"upgrade_aborted_backup_failed"}):
            ok += 1
            job._imported_any = True
            verdict = result.get("quality_verdict")
            if verdict and verdict["under"] and not verdict["recovered"]:
                from qobuz_librarian.quality.decision import mark_album_capped
                mark_album_capped(album.get("id"), album, {
                    "n_below": verdict["n_below"],
                    "n_at": 0,
                    "n_above": 0,
                })
                log.info(
                    f"  upgrade incomplete: {plural(verdict['n_below'], 'track')} "
                    "still below target after retry. Marked capped."
                )
            _refresh_after_local_album_change(
                album,
                result,
                fallback_artist=cand.get("artist"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
            # The verified replace refreshed the whole album, so a parked
            # library review's candidates for it (incl. Gap Fill) are stale.
            prune_library_review_candidates(album)
        elif result and _res in (_skip - {"cancelled", "dry_run"}):
            _refresh_after_local_album_change(
                album,
                result,
                fallback_artist=cand.get("artist"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
        elif _res not in _skip:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if job.cancel_requested:
        job.summary = (f"Stopped early. {ok} upgraded, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    msg = f"Finished. Upgraded {ok}/{plural(len(chosen), 'album')}."
    if kept:
        msg += (f" {kept} kept the original (upgrade couldn't be verified "
                f"complete; backup retained).")
    job.summary = msg
    log.info(msg)
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be upgraded; see the log."


# ── Downsample flow ─────────────────────────────────────────────────────────────

def scan_downsamples(job):
    """Scan the library for FLACs stored above CD rate.

    Local only — the answer comes off disk, so unlike the upgrade scan there's
    no Qobuz lookup and no token. Serial (the per-file read is fast and disk-
    bound; fanning out would just thrash the spindle) with a cancel check and
    per-artist progress.
    """
    clear_scan_caches()
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        return
    hidden = hidden_mod.load()
    log.info(f"Scanning {plural(len(artists), 'artist')} for hi-res files to downsample")
    total = 0

    def _on_artist(ad, cands, error, done, n):
        nonlocal total
        name = ad.name
        if error is not None:
            log.info(f"    skipped {name}: {error}")
            job.push_progress("Scanning for hi-res files", done, n, name,
                              found=total, unit="artist")
            return
        added = 0
        current_hidden = hidden_mod.load()
        for c in cands:
            if hidden_mod.is_hidden(
                    hidden_mod.SCOPE_DOWNSAMPLE, c.artist, c.title, current_hidden):
                continue
            # Unticked by default — a downsample is irreversible, so nothing is
            # shrunk without an explicit per-album tick.
            job.add_candidate(
                kind="downsample",
                title=c.title,
                artist=name,
                detail=c.detail,
                payload={"album_dir": str(c.album_dir), "est_saving": c.est_saving},
                selected=False,
            )
            total += 1
            added += 1
        hit = {"artist": name, "albums": added} if added else None
        job.push_progress("Scanning for hi-res files", done, n, name,
                          found=total, hit=hit, unit="artist")
        if added:
            log.info(f"  {name} — {plural(added, 'album')} above CD rate")

    refresh = downsample_state.refresh_for_artists(
        artists,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        on_artist=_on_artist,
    )
    if not job.cancel_requested and refresh.complete:
        review_badges.set_ready("downsample", _surface_has_candidates("downsample"))
    if job.cancel_requested or not refresh.complete:
        log.info("Cancelled. Stopping scan.")
    if not job.cancel_requested:
        _flag_new_since_last_scan(job, "downsample")
    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    else:
        job.summary = (f"{plural(total, 'album')} stored above CD rate."
                       + _cap_note(job)
                       if total else
                       "No hi-res files; every album is already at CD rate or lower.")
    log.info(job.summary)


def execute_downsamples(job, chosen, token=None, args=None):
    """Shrink the chosen albums' hi-res FLACs to CD rate, in place.

    Each file is decode-verified before it overwrites the original (in
    resample_one), so a bad encode can't destroy a master that has no
    re-download fallback.
    """
    from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE, downsample_dir
    from qobuz_librarian.quality.decision import mark_local_album_capped
    from qobuz_librarian.web.jobs import staging_lock

    if not HAVE_DOWNSAMPLE:
        job.error = "Downsampling isn't available on this server."
        return
    shrunk = 0
    total_saved = 0
    total_errors = 0
    total_flush_warns = 0
    skipped = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        raw_album_dir = (cand.get("payload") or {}).get("album_dir")
        if not raw_album_dir:
            log.info("  skipped: saved candidate is missing its folder path")
            skipped += 1
            continue
        album_dir = Path(raw_album_dir)
        title = cand.get("title") or album_dir.name
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist', '')} — {title}")
        job._progress_scope = (i, len(chosen), "album")
        job.push_progress("Downsampling albums", i, len(chosen),
                          f"{cand.get('artist', '')} — {title}", unit="album")
        if not album_dir.is_dir():
            log.info("  skipped: folder no longer exists")
            skipped += 1
            if album_dir.parent.is_dir():
                _refresh_downsample_artist_state(album_dir.parent)
            else:
                downsample_state.remove_artist(album_dir.parent.name)
                review_badges.set_ready(
                    "downsample", _surface_has_candidates("downsample"))
            continue
        _note_staging_wait(job, "Downsampling albums", i, len(chosen))
        try:
            with staging_lock():
                res = downsample_dir(album_dir, verbose=True,
                                     base_dir=album_dir, log=log.info,
                                     keep_originals=cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep",
                                     cancel_check=lambda: job.cancel_requested)
        except Exception as e:
            log.info(f"  failed: {e}")
            total_errors += 1
            continue
        if res.get("resampled") and not res.get("cancelled"):
            # A Stop mid-album leaves the rest of its tracks hi-res: don't
            # count it done or mark it capped — the state refresh below
            # re-lists the remainder so the run can be finished later.
            shrunk += 1
            mark_local_album_capped(album_dir)
            if token:
                try:
                    _refresh_upgrade_artist_state(
                        album_dir.parent, token, args=args or build_args())
                except (AuthLost, QobuzUnavailable) as exc:
                    log.info(
                        f"  upgrade view refresh skipped after downsample: {exc}")
        _refresh_downsample_artist_state(album_dir.parent)
        total_saved += res.get("saved_bytes", 0)
        total_errors += res.get("errors", 0)
        total_flush_warns += res.get("flush_warnings", 0)
    job._progress_scope = None
    if job.cancel_requested:
        job.summary = (f"Stopped early. Downsampled {plural(shrunk, 'album')} "
                       f"({format_size(total_saved)} smaller), "
                       f"{len(chosen) - processed} not started."
                       + _kept_originals_note())
        log.info(job.summary)
        return
    # "Reclaimed" is a claim about free space. When the originals are kept, a
    # full copy of every one of them is written to the backup folder, so the
    # run costs MORE disk than it saves until that retention expires.
    summary = (f"Finished. Downsampled {plural(shrunk, 'album')}, "
               f"{format_size(total_saved)} smaller." + _kept_originals_note())
    if skipped:
        summary += f" {plural(skipped, 'album')} skipped (no longer on disk)."
    job.summary = summary
    log.info(summary)
    if total_errors:
        job.error = (f"{plural(total_errors, 'file')} couldn't be downsampled "
                     "(left unchanged); see the log.")
    if total_flush_warns:
        # These files WERE rewritten — only the folder flush failed
        # afterwards, so the swap may not survive a power loss.
        note = (f"{plural(total_flush_warns, 'file')} resampled but couldn't "
                f"be flushed to disk — check the drive; see the log.")
        job.error = f"{job.error} {note}" if job.error else note


def _kept_originals_note():
    """The sentence that stops a downsample summary overstating what it freed."""
    if cfg.DOWNSAMPLE_KEEP_ORIGINALS != "keep":
        return ""
    days = cfg.UPGRADE_BACKUP_RETENTION_DAYS
    return (f" Your hi-res originals are kept for {plural(days, 'day')}, so "
            "nothing is freed until they expire or you remove them in Settings.")

# ── Repair flow ───────────────────────────────────────────────────────────────

def _repair_album_outcome(album_dir, name, token):
    """Scan one album into an outcome dict: counts, review-candidate specs, and
    any log lines to emit. AuthLost / QobuzUnavailable propagate (they stop the
    sweep); any other scan error is recorded as a failed album."""
    from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs
    out = {"verified_ok": 0, "unverified": 0, "failed": 0, "specs": [],
           "warns": []}
    try:
        scan = scan_dir_for_isrc_repairs(album_dir, token, deep=True)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception as e:
        out["warns"].append(f"    skipped {album_dir.name}: {e}")
        out["failed"] = 1
        return out
    out["verified_ok"] = scan["verified_ok"]
    out["unverified"] = scan.get("unverified", 0)
    truncated = scan["verified_truncated"]
    if truncated:
        out["specs"].append({
            "kind": "repair", "title": album_dir.name, "artist": name,
            "detail": f"{plural(len(truncated), 'truncated track')}",
            "payload": {"album_dir": str(album_dir), "artist_name": name,
                        "verified_truncated": truncated}})
    # Damaged files that can't be matched to a Qobuz recording can't be
    # surgically refilled — offer a whole-album re-download instead (the user
    # confirms it in review). Both no-match buckets carry them: no ISRC at all,
    # or an ISRC Qobuz doesn't know.
    suspicious = [
        entry
        for bucket in ("isrc_no_match", "no_isrc_tag")
        for entry in scan.get(bucket, [])
        if entry.get("diagnostic")
    ]
    if suspicious:
        matched = find_qobuz_album_for_dir(album_dir, name, token)
        if matched and matched.get("id"):
            m_title = matched.get("title") or album_dir.name
            m_year = album_year(matched) or "?"
            out["specs"].append({
                "kind": "redownload", "title": album_dir.name, "artist": name,
                "detail": (f"{plural(len(suspicious), 'damaged file')} can't be "
                           f"verified by ID. Re-download the whole album fresh "
                           f"as “{m_title}” ({m_year})"),
                "payload": {"album_dir": str(album_dir), "artist_name": name,
                            "album_id": matched.get("id"),
                            "matched_title": m_title}})
        else:
            for e in suspicious:
                out["warns"].append(
                    f"    ⚠ {album_dir.name} — {e.get('title') or '?'}: "
                    f"{e['diagnostic']}; couldn't match this folder to a Qobuz "
                    "album to re-download. Check by hand.")
    return out


def _scan_repair_artist(artist_dir, token, job, beat=None):
    """Scan one artist's albums for damaged FLACs — runs on a pool worker.

    Returns ``(name, agg)``; ``agg`` carries per-artist counts and a list of
    review-candidate specs the caller adds on the single writer thread, so the
    candidate list and checkpoint stay single-writer (mirroring the library
    scan). Every album is decode-tested fresh each run so on-disk corruption is
    always caught; the per-track Qobuz lookups are what's cached, so a re-scan
    only re-reads files rather than re-crawling Qobuz. Bails between albums on
    cancel; AuthLost / QobuzUnavailable propagate so the caller can stop the
    sweep."""
    name = artist_dir.name
    agg = {"verified_ok": 0, "unverified": 0, "failed": 0, "checked": 0,
           "specs": []}
    for album_dir in list_artist_album_dirs(artist_dir):
        if job.cancel_requested:
            break
        outcome = _repair_album_outcome(album_dir, name, token)
        agg["verified_ok"] += outcome.get("verified_ok", 0)
        agg["unverified"] += outcome.get("unverified", 0)
        agg["failed"] += outcome.get("failed", 0)
        agg["checked"] += 1
        agg["specs"].extend(outcome.get("specs", []))
        for w in outcome.get("warns", []):
            log.info(w)
        if beat is not None:
            _emit_repair_heartbeat(beat, job, name)
    return name, agg


def _repair_item(artist, albums, flagged):
    """One consistent live-status line for the whole-library repair sweep.

    Every progress push during the sweep — the per-album heartbeat, the
    per-artist completion tick, and the failure tick — renders through this so
    the job page's detail line updates *in place* (the artist and the counts
    climbing) instead of structurally flip-flopping between an artist-name form
    and a bare-tally form, which reads as flicker. ``artist`` is whichever one a
    worker is currently grinding on (carried in ``beat['current']``); it is
    empty only in the opening instant before the first heartbeat fires, where a
    neutral label stands in."""
    if not artist and not albums:
        return "Starting…"
    who = artist or "your library"
    return f"{who} · {albums:,} albums checked · {flagged:,} flagged"


def _emit_repair_heartbeat(beat, job, artist_name):
    """Refresh the live progress line from whichever worker crosses the interval,
    so it keeps ticking even while every worker is deep inside one large artist
    and no future has completed — otherwise a long stretch with no completed
    artist looks like a hang. Pushed through the progress channel, not the log,
    so the activity log stays a list of flagged albums rather than a scroll of
    heartbeats. Counts are shared under ``beat['lock']``."""
    with beat["lock"]:
        beat["albums"] += 1
        if time.time() - beat["last"] < _REPAIR_HEARTBEAT_SECS:
            return
        beat["last"] = time.time()
        # Advance the displayed artist only when the throttled beat actually
        # fires, so the detail line names a stable artist for a calm interval
        # (rather than hopping every time a worker finishes a small one), while
        # the completion ticks in between keep the bar and counts climbing.
        beat["current"] = artist_name
        albums, artists, flagged, n = (beat["albums"], beat["artists"],
                                       beat["flagged"], beat["n"])
        item = _repair_item(artist_name, albums, flagged)
    job.push_progress("Checking for damaged files", artists, n, item,
                      found=flagged, unit="artist")


def scan_repairs(job, token):
    """Scan every album for ISRC-verified truncated FLACs (fanned out across
    ARTIST_SCAN_WORKERS; see _scan_repair_artist for the per-artist work)."""
    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        _set_empty_library_summary(job)
        return
    # Resume an interrupted sweep: skip the artists already checked and
    # restore the damaged albums they turned up.
    cp = scan_checkpoint.load("repair")
    scanned = set(cp["scanned"]) if cp else set()
    total = 0
    n_verified = 0      # ISRC'd FLACs that actually decoded clean this run
    n_unverified = 0    # couldn't decode-check (flac tool absent)
    n_failed = 0        # albums that errored mid-scan (surfaced, not hidden)
    if cp:
        for c in cp["candidates"]:
            _readd_candidate(job, c)
            total += 1
        log.info(f"Resuming. {plural(len(scanned), 'artist')} already checked, "
                 f"{plural(total, 'album')} flagged so far.")
    log.info(f"Scanning {plural(len(artists), 'artist')} for damaged files. "
             "Only problems are listed below; expect long quiet stretches. "
             "Slow on a big library.")
    todo = [ad for ad in artists if ad.name not in scanned]
    n = len(artists)
    done = len(scanned)
    since_save = 0
    # Shared heartbeat state: workers bump it per album and one logs the periodic
    # line when due, so progress keeps showing even while every worker is deep in
    # one large artist and no future has completed (see _emit_repair_heartbeat).
    beat = {"lock": threading.Lock(), "albums": 0, "artists": done,
            "flagged": total, "n": n, "last": time.time(), "current": ""}
    # Show the progress bar immediately rather than a blank header until the
    # first artist comes back.
    job.push_progress("Checking for damaged files", done, n,
                      _repair_item("", 0, total), found=total, unit="artist")
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Scan artists in parallel (each worker gets its own HTTP session), but
    # add candidates, advance progress, and write the checkpoint on THIS one
    # thread so they stay single-writer — the same shape the library scan
    # uses.
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repairscan",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(_scan_repair_artist, ad, token, job, beat): ad
                   for ad in todo}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                job.summary = (f"Stopped early. {plural(total, 'album')} flagged so far."
                               if total else "Stopped before anything was flagged.")
                log.info("Cancelled. Stopping scan.")
                scan_checkpoint.clear("repair")
                return
            done += 1
            try:
                name, agg = fut.result()
            except (AuthLost, QobuzUnavailable):
                # A lost token or an unreachable API isn't a per-artist hiccup —
                # stop the sweep rather than report a partial library as whole.
                # The checkpoint stays, so it resumes once auth/network is back.
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                log.info(f"    skipped {futures[fut].name}: {e}")
                n_failed += 1
                with beat["lock"]:
                    beat["artists"] = done
                    albums_seen = beat["albums"]
                    current = beat["current"]
                job.push_progress("Checking for damaged files", done, n,
                                  _repair_item(current, albums_seen, total),
                                  found=total, unit="artist")
                continue
            n_verified += agg["verified_ok"]
            n_unverified += agg["unverified"]
            n_failed += agg["failed"]
            for spec in agg["specs"]:
                job.add_candidate(**spec)
                total += 1
            scanned.add(name)
            with beat["lock"]:
                beat["artists"] = done
                beat["flagged"] = total
                albums_seen = beat["albums"]
                current = beat["current"]
            job.push_progress("Checking for damaged files", done, n,
                              _repair_item(current, albums_seen, total),
                              found=total, unit="artist")
            since_save += 1
            if since_save >= _CHECKPOINT_EVERY:
                since_save = 0
                scan_checkpoint.save("repair", scanned, job.candidates, {})
    scan_checkpoint.clear("repair")
    # Honest summary: report what was actually decode-verified, and never
    # claim completeness the scan didn't earn. With the flac tool present,
    # an unverified track means the file couldn't be read or changed while
    # it was being checked — unreadable is an ownership problem, not a
    # missing tool.
    unver_reason = (
        "the app couldn't read them or they changed mid-check — check "
        "file ownership and PUID"
        if shutil.which("flac") else "no flac tool"
    )
    unver = (f" {plural(n_unverified, 'track')} couldn't be decode-checked "
             f"({unver_reason})." if n_unverified else "")
    fail = (f" {plural(n_failed, 'album')} couldn't be scanned; re-run to retry."
            if n_failed else "")
    if total:
        job.summary = (f"{plural(total, 'album')} flagged with damaged files. "
                       f"{plural(n_verified, 'track')} decode-verified clean."
                       + unver + fail)
    else:
        job.summary = (f"No damaged files found. "
                       f"{plural(n_verified, 'track')} decode-verified intact."
                       + unver + fail)
    log.info(job.summary)


def _redownload_damaged_album(payload, token, *, recovery_checkpoint=None):
    """Re-fetch a whole album whose damaged file couldn't be ID-verified.

    The folder is moved aside first so beets imports a clean copy instead of
    colliding with the broken files (the --force path can't be used here: it
    needs an interactive deletion confirm the web has no way to answer). If
    the re-download doesn't complete, the original folder is moved back so the
    user is never left worse off.
    """
    from qobuz_librarian.library.backup import (
        backup_album_dir,
        pin_unverified_upgrade_backup,
        restore_upgrade_backup,
        retire_verified_repair_backup,
        warn_pin_failed,
    )
    from qobuz_librarian.modes.process import (
        _carry_non_audio_from_backup,
        _recover_incomplete_upgrade_backup,
        _upgrade_replacement_verified,
        process_album,
    )
    from qobuz_librarian.modes.repair import (
        RepairRecovery,
        RepairRecoveryRequired,
    )
    from qobuz_librarian.web.jobs import staging_lock

    log.info("  The damaged file can't be verified by its ID, so the whole "
             "album is being re-downloaded fresh from Qobuz.")
    full = get_album(payload["album_id"], token)
    album_dir = Path(payload["album_dir"])
    backup = backup_album_dir(album_dir) if album_dir.exists() else None
    if backup is not None and not backup.complete:
        _recover_incomplete_upgrade_backup(
            backup, album_dir, operation="repair backup")
        log.info("  The backup was interrupted, so the repair was stopped. "
                 "See the recovery message above.")
        return {"imported": False, "n_ok": 0, "result": "backup_failed"}
    if (
        album_dir.exists()
        and backup is None
    ):
        log.info("  Couldn't move the existing folder aside; left this album "
                 "alone. See the log above.")
        return {"imported": False, "n_ok": 0, "result": "backup_failed"}

    def pin_repair_recovery(note):
        if (backup is not None and backup.exists()
                and not pin_unverified_upgrade_backup(backup, note)):
            warn_pin_failed(backup)

    def checkpoint_recovery(stage, reason, *, retained=True, required=False):
        recovery = RepairRecovery(
            backup=backup,
            album_dir=album_dir,
            stage=stage,
            reason=reason,
            retained=retained,
        )
        if recovery_checkpoint is None:
            return recovery, True
        try:
            persisted = recovery_checkpoint(recovery) is True
        except Exception as exc:
            persisted = False
            log.info(f"  Couldn't save the Repair recovery record: {exc}")
        if required and not persisted:
            log.info("  Repair stopped before downloading the replacement "
                     "because its recovery record could not be saved.")
        return recovery, persisted

    if backup is not None:
        _recovery, recovery_saved = checkpoint_recovery(
            "backup",
            "The original album is held while its replacement is downloaded.",
            required=True,
        )
        if not recovery_saved:
            if restore_upgrade_backup(backup, album_dir):
                checkpoint_recovery(
                    "resolved",
                    "The original album was restored before Repair stopped.",
                    retained=False,
                )
                return {
                    "imported": False,
                    "n_ok": 0,
                    "result": "recovery_record_failed",
                }
            pin_repair_recovery(
                "repair backup kept — recovery record and automatic restore "
                "did not complete")
            recovery, _ = checkpoint_recovery(
                "restore",
                "The recovery record could not be saved and automatic "
                "restoration did not complete.",
            )
            cause = OSError("Repair recovery record could not be saved")
            raise RepairRecoveryRequired(recovery, cause) from cause

    try:
        with staging_lock():
            result = process_album(full, build_args(), allow_force=False,
                                   already_confirmed=True, token=token) or {}
    except Exception as exc:
        if backup:
            if restore_upgrade_backup(backup, album_dir):
                checkpoint_recovery(
                    "resolved",
                    "The original album was restored after the replacement "
                    "download stopped.",
                    retained=False,
                )
            else:
                pin_repair_recovery(
                    "repair backup kept — automatic restore after an error did "
                    "not complete")
                recovery, _ = checkpoint_recovery(
                    "restore",
                    "The replacement download stopped and automatic "
                    "restoration did not complete.",
                )
                raise RepairRecoveryRequired(recovery, exc) from exc
        raise
    imported_ok = bool(result.get("imported")) and result.get("n_ok", 0) > 0
    if backup:
        if imported_ok and _upgrade_replacement_verified(full, album_dir, backup):
            # Carry useful companions, then retire the original's backup when
            # every file it holds is verifiably superseded in the new album.
            carried = _carry_non_audio_from_backup(
                full, album_dir, backup)
            if carried is not None:
                if retire_verified_repair_backup(backup):
                    checkpoint_recovery(
                        "resolved",
                        "The re-download verified, so the original album's "
                        "backup was removed.",
                        retained=False,
                    )
                    log.info("  Re-download verified; removed the original "
                             "album's backup.")
                else:
                    pin_repair_recovery(
                        "repair backup kept — could not be proven redundant "
                        "after a verified re-download")
                    log.info("  Re-download verified, but the original "
                             "album's backup couldn't be proven redundant; "
                             f"keeping it at {backup}.")
                    checkpoint_recovery(
                        "verification",
                        "The re-download verified, but the original album's "
                        "backup could not be proven redundant.",
                    )
            else:
                result["repair_unverified"] = True
                pin_repair_recovery(
                    "repair backup kept — replacement not durable")
                log.info("  Re-download landed, but the rebuilt album or its "
                         "companions couldn't be flushed safely; keeping your "
                         f"backup at {backup}.")
                checkpoint_recovery(
                    "verification",
                    "The replacement landed, but its album or companion files "
                    "could not be flushed safely.",
                )
        elif imported_ok:
            # Imported, but a decode pass alone doesn't prove the re-rip kept
            # every track — a truncated or short result could be WORSE than
            # the damaged original it replaced.
            result["repair_unverified"] = True
            pin_repair_recovery(
                "repair backup kept — replacement could not be verified "
                "complete")
            log.info("  Re-download landed but couldn't be verified as complete "
                     f"as the original; keeping your backup at {backup}.")
            checkpoint_recovery(
                "verification",
                "The replacement landed but could not be verified as complete "
                "as the original album.",
            )
        else:
            log.info("  Re-download didn't complete. Restoring the original "
                     "album folder.")
            if restore_upgrade_backup(backup, album_dir):
                checkpoint_recovery(
                    "resolved",
                    "The replacement did not complete, so the original album "
                    "was restored.",
                    retained=False,
                )
            else:
                pin_repair_recovery(
                    "repair backup kept — automatic restore did not complete")
                checkpoint_recovery(
                    "restore",
                    "The replacement did not complete and automatic "
                    "restoration was incomplete.",
                )
    return result


def _checkpoint_repair_recovery(job, recovery):
    """Attach one exact Repair carrier to the durable job record."""
    from qobuz_librarian.web import job_persistence

    try:
        record = recovery.as_record()
        required = {
            "version", "kind", "status", "location", "album_dir", "stage",
            "reason", "complete", "requested", "backed_up", "receipt",
        }
        if (
            not isinstance(record, dict)
            or set(record) != required
            or record.get("version") != 1
            or record.get("kind") != "repair-backup"
            or record.get("status") not in ("retained", "resolved")
            or not isinstance(record.get("location"), str)
            or not record["location"]
            or not isinstance(record.get("album_dir"), str)
            or not record["album_dir"]
            or type(record.get("complete")) is not bool
            or type(record.get("requested")) is not int
            or type(record.get("backed_up")) is not int
            or record["requested"] < 0
            or record["backed_up"] < 0
            or record["backed_up"] > record["requested"]
            or (
                record.get("receipt") is not None
                and not isinstance(record.get("receipt"), dict)
            )
        ):
            raise ValueError("Repair recovery record is malformed")
    except (AttributeError, TypeError, ValueError) as exc:
        log.info(f"  Couldn't prepare the Repair recovery record: {exc}")
        return False

    key = (record["kind"], record["location"])
    with job._lock:
        current = list(getattr(job, "recoveries", []) or [])
        matching = lambda item: (
            isinstance(item, dict)
            and (item.get("kind"), item.get("location")) == key
        )
        if record["status"] == "resolved":
            current = [item for item in current if not matching(item)]
        else:
            replaced = False
            updated = []
            for item in current:
                if matching(item):
                    if not replaced:
                        updated.append(record)
                        replaced = True
                else:
                    updated.append(item)
            if not replaced:
                updated.append(record)
            current = updated
        job.recoveries = current
        if current:
            job.attention = "recovery"
        elif job.attention == "recovery":
            job.attention = ""
    return job_persistence.persist_recoveries(job)


def execute_repairs(job, chosen, token):
    """Refill ISRC-verified truncated tracks, or re-download whole albums
    whose damage couldn't be ID-verified — depending on each candidate."""
    from qobuz_librarian.modes.repair import (
        RepairRecoveryRequired,
        repair_album_dir,
    )
    from qobuz_librarian.web.jobs import staging_lock

    clear_scan_caches()
    args = build_args()
    fixed = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        p = cand["payload"]
        # Pin the progress card to album-level scope so the inner per-album
        # phases (download / import / downsample) read "album i / N" instead of
        # resetting it to 1 / 1 — the card now reflects the whole batch.
        job._progress_scope = (i, len(chosen), "album")
        job.push_progress("Repairing damaged albums", i, len(chosen),
                          f"{p['artist_name']} — {cand['title']}", unit="album")
        log.info(f"[{i}/{len(chosen)}] {p['artist_name']} — {cand['title']}")
        _note_staging_wait(job, "Repairing damaged albums", i, len(chosen))
        try:
            if cand.get("kind") == "redownload":
                # _redownload_damaged_album takes the staging lock itself.
                result = _redownload_damaged_album(
                    p,
                    token,
                    recovery_checkpoint=lambda recovery: (
                        _checkpoint_repair_recovery(job, recovery)
                    ),
                )
            else:
                with staging_lock():
                    result = repair_album_dir(Path(p["album_dir"]),
                                              p["verified_truncated"],
                                              p["artist_name"], args, token,
                                              recovery_checkpoint=lambda recovery: (
                                                  _checkpoint_repair_recovery(
                                                      job, recovery)
                                              ))
        except RepairRecoveryRequired as exc:
            # The callback normally saved this before the exceptional
            # boundary.
            _checkpoint_repair_recovery(job, exc.recovery)
            if job.cancel_requested:
                break
            if isinstance(exc.cause, (AuthLost, QobuzUnavailable, SystemExit)):
                raise exc.cause
            raise
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        # Each chosen album was flagged as damaged, so anything that didn't
        # end up downloaded-and-imported is a real failure. A kept backup is
        # not one — a verified repair counts, and the backup is reported in
        # the summary's recovery tail.
        if (result and result.get("n_ok", 0) > 0 and result.get("imported")
                and result.get("n_fail", 0) == 0
                and not result.get("repair_unverified")):
            fixed += 1
            job._imported_any = True
            _refresh_after_local_album_change(
                None,
                result,
                fallback_artist=p.get("artist_name"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
        else:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    recovery_count = len(getattr(job, "recoveries", []) or [])
    if job.cancel_requested:
        kept = (f", {recovery_count} kept for recovery"
                if recovery_count else "")
        job.summary = (f"Stopped early. {fixed} repaired{kept}, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    kept = (f" {plural(recovery_count, 'backup')} kept for recovery."
            if recovery_count else "")
    job.summary = (f"Finished. Repaired {fixed}/{plural(len(chosen), 'album')}."
                   f"{kept}")
    log.info(job.summary)
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be repaired; see the log."


def run_lyric_retry(job):
    """Retry lyric fetching for tracks queued from a previous failed run."""
    from qobuz_librarian.integrations.lyrics import (
        _refresh_lyric_retry,
        load_lyric_retry,
        lyric_fetch,
        save_lyric_retry,
    )

    paths = load_lyric_retry()
    if not paths:
        job.summary = "No tracks were queued for lyric retry."
        log.info(job.summary)
        return

    if not lyric_fetch.AVAILABLE:
        job.summary = ("The syncedlyrics library isn't installed; manifest "
                       "preserved for a later retry.")
        log.info(job.summary)
        return

    existing = [Path(p) for p in paths if Path(p).exists()]
    dropped = len(paths) - len(existing)
    if dropped:
        log.info(f"{plural(dropped, 'queued path')} no longer on disk; skipping.")
    if not existing:
        save_lyric_retry([])
        job.summary = "All queued files are gone from disk; manifest cleared."
        log.info(job.summary)
        return

    log.info(f"Retrying lyrics on {plural(len(existing), 'track')} ...")
    # Hold the staging lock: fetch_for_paths rewrites library FLACs in place, so
    # it must not run concurrently with the scan-lane downsample/repair/upgrade
    # work that mutates the same files (the documented file-mutation mutex).
    from qobuz_librarian.web.jobs import set_staging_holder, staging_lock
    try:
        with staging_lock():
            set_staging_holder("Lyrics retry")
            try:
                lyric_fetch.fetch_for_paths(
                    existing, owned_root=cfg.MUSIC_ROOT, log=log,
                    providers=cfg.LYRICS_PROVIDERS or None,
                    lyrics_format=cfg.LYRICS_FORMAT,
                    state_path=cfg.LYRIC_FETCH_STATE_FILE,
                    should_stop=lambda: job.cancel_requested,
                )
            finally:
                set_staging_holder(None)
    except Exception as e:
        job.error = f"Lyric retry failed: {e}; manifest preserved."
        job.summary = "Lyric retry failed. Manifest preserved, will retry next time."
        log.info(job.error)
        return

    _refresh_lyric_retry(existing)
    remaining = load_lyric_retry()
    resolved = len(existing) - len(remaining)
    if job.cancel_requested:
        job.summary = (f"Stopped. Resolved {resolved}, "
                       f"{plural(len(remaining), 'track')} still queued for retry.")
    elif remaining:
        job.summary = (f"Resolved {resolved}. {plural(len(remaining), 'track')} "
                       "still unresolved, will retry next time.")
    else:
        job.summary = f"All {plural(len(existing), 'retried track')} resolved."
    log.info(job.summary)


def run_library_lyrics(job, *, rescan=False, synced_only=False):
    """Fetch lyrics for every library track that's missing them."""
    from qobuz_librarian.library.lyrics import HAVE_LYRICS
    from qobuz_librarian.library.lyrics import run_library_lyrics as engine

    if not HAVE_LYRICS:
        job.summary = "Lyric fetching isn't available; the syncedlyrics library isn't installed."
        log.info(job.summary)
        return

    log.info(f"Fetching lyrics across the library (writing {(cfg.LYRICS_FORMAT or 'embed').lower()}).")
    if rescan:
        log.info("Re-checking every track (ignoring saved state).")
    # Hold the staging lock: the engine rewrites library FLACs in place, which
    # must not race the scan-lane downsample/repair/upgrade work on the same tree.
    from qobuz_librarian.web.jobs import set_staging_holder, staging_lock
    with staging_lock():
        set_staging_holder("Lyrics scan")
        try:
            res = engine(rescan=rescan, synced_only=synced_only,
                         should_stop=lambda: job.cancel_requested, log=log)
        finally:
            set_staging_holder(None)

    total = res.get("total", 0)
    if not total:
        job.summary = "No FLAC files found in the library."
        log.info(job.summary)
        return
    if res.get("stopped"):
        stop_total = max(0, int(res.get("stop_total", total)))
        processed = min(stop_total, max(0, int(res.get("processed", 0))))
        job.summary = (
            f"Stopped after processing {processed} of "
            f"{plural(stop_total, 'track')}.")
        log.info(job.summary)
        return

    # `total` is every FLAC in the library; the run only opens the tracks the
    # saved state says still need checking. Summarise what the run did, not
    # the size of the library it did it in.
    processed = max(0, int(res.get("processed", 0)))
    skipped = max(0, total - processed)
    if not processed:
        job.summary = (
            f"Nothing needed checking — all {plural(total, 'track')} have "
            "lyrics or were checked before. Tick “Re-check everything” to "
            "redo them.")
        log.info(job.summary)
        return
    wrote = (res.get("wrote-synced", 0) + res.get("wrote-plain", 0)
             + res.get("dry:wrote-synced", 0) + res.get("dry:wrote-plain", 0))
    not_found = res.get("not-found", 0)
    unavailable = res.get("providers-unavailable", 0)
    parts = [f"{plural(processed, 'track')} checked", f"{wrote} got lyrics"]
    if not_found:
        parts.append(f"{not_found} no lyrics found")
    if unavailable:
        parts.append(f"{unavailable} couldn't reach a provider (re-run later)")
    if skipped:
        parts.append(f"{skipped} skipped (already checked)")
    job.summary = " · ".join(parts) + "."
    log.info(job.summary)


# ── Library migration ──────────────────────────────────────────────────────────


def scan_migration(job, src, dest, *, use_acoustid, in_place=False):
    """Analyze the source library and attach one candidate per placeable album.

    New placements and verified existing copies become the review list (grouped
    by artist). Files that can't be identified or have unresolved collisions
    are reported and left untouched. A preview manifest is written to the
    destination so the plan is reviewable before anything is copied.
    """
    from qobuz_librarian.library import migrate as engine

    src, dest = Path(src), Path(dest)
    items = engine.collect_items(
        src, use_acoustid=use_acoustid,
        cancel_check=lambda: job.cancel_requested,
        progress=job.push_progress)
    if job.cancel_requested:
        n = len(items) if items else 0
        job.summary = (f"Stopped early. {plural(n, 'file')} scanned so far."
                       if n else "Stopped before anything was scanned.")
        return
    plan = engine.build_plan(items, dest)
    resume_entries = engine.verified_resume_entries(
        plan, progress=job.push_progress,
        cancel_check=lambda: job.cancel_requested)
    if job.cancel_requested:
        job.summary = "Stopped while checking existing copies. Nothing was copied."
        return

    try:
        manifest_artifact = engine.write_manifest(plan)
        manifest = Path(manifest_artifact["path"])
    except (KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
        job.error = (
            "The migration preview could not be recorded safely, so nothing "
            "can be approved. Fix the destination or filename issue and "
            "scan again.")
        job.summary = job.error
        log.info(f"{job.error} Details: {exc}")
        return

    groups: dict = {}
    for kind, plan_entries in (
            ("entries", plan.placed), ("resume_entries", resume_entries)):
        for entry in plan_entries:
            # dest_rel is <artist>/<album (year)>/[Disc N/]<track>;
            # group by album directory.
            key = (entry.dest_rel.parts[0], entry.dest_rel.parts[1])
            group = groups.setdefault(
                key, {"entries": [], "resume_entries": []})
            group[kind].append(entry)
    for (artist, album), group in sorted(groups.items()):
        entries = group["entries"]
        resumes = group["resume_entries"]
        source_folders = {
            tuple(entry.source_receipt.get("relative", ())[:-1])
            for entry in entries + resumes
            if entry.source_receipt is not None
        }
        if entries and resumes:
            detail = (f"{plural(len(entries), 'track')} to "
                      f"{'move' if in_place else 'copy'} · "
                      f"{plural(len(resumes), 'track')} already verified")
        elif entries:
            detail = (f"{plural(len(entries), 'track')} → "
                      f"{artist}/{album}")
        else:
            detail = (f"{plural(len(resumes), 'track')} already copied · "
                      "finish cover art and sidecars")
        payload = {
            "entries": [
                (str(e.source), str(e.dest_rel), e.source_receipt,
                 e.destination_path_receipt)
                for e in entries],
            "resume_entries": [
                (str(e.source), str(e.dest_rel), e.source_receipt,
                 e.destination_receipt)
                for e in resumes],
            "source_root": (str(plan.source_root)
                            if plan.source_root is not None else None),
            "source_root_receipt": plan.source_root_receipt,
            "dest_root_receipt": plan.dest_root_receipt,
            "destination_name_semantics": plan.destination_name_semantics,
            "manifest_artifact": manifest_artifact,
            "companion_receipts": [
                receipt for receipt in plan.companion_receipts
                if tuple(receipt.get("relative", ())[:-1]) in source_folders
            ],
        }
        job.add_candidate(
            kind="migrate",
            title=album,
            artist=artist,
            detail=detail,
            payload=payload,
        )

    s = plan.summary()
    verb = "move" if in_place else "copy"
    parts = []
    if s["place"]:
        parts.append(f"{plural(s['place'], 'file')} ready to {verb}")
    if resume_entries:
        parts.append(
            f"{plural(len(resume_entries), 'existing file')} verified to resume")
    if not parts:
        parts.append("No files ready")
    if s["unplaceable"]:
        parts.append(f"{s['unplaceable']} couldn't be identified")
    unsafe_collisions = s["collision"] - len(resume_entries)
    if unsafe_collisions:
        parts.append(f"{unsafe_collisions} skipped to avoid name collisions")
    need, free = engine.space_estimate(
        plan, in_place=in_place, resume_entries=resume_entries)
    if need and free is not None:
        space = f"≈{format_size(need)} to {verb}, {format_size(free)} free at the destination"
        if need > free:
            space = ("⚠ not enough free space: needs "
                     f"≈{format_size(need)} but only {format_size(free)} is free")
            if in_place:
                space += (". The in-place move is blocked unless you re-run with "
                          "“proceed even if low on space” checked")
        parts.append(space)
    # Show the manifest where the user can actually find it — the container
    # path means nothing from a phone. Settings does the same for every path
    # it displays. Runtime import: app imports flows at startup.
    from qobuz_librarian.web.app import _resolve_host_path
    manifest_display, _ = _resolve_host_path(str(manifest))
    job.summary = ("; ".join(parts) + ". Unidentified and skipped files stay "
                   f"where they are. Full plan written to {manifest_display}.")
    log.info(job.summary)


def execute_migration(job, chosen, dest, *, in_place, src=None,
                      allow_low_space=False):
    """Copy (or move) the files behind the approved albums into the layout."""
    from qobuz_librarian.library import migrate as engine

    dest = Path(dest)
    entries = []
    source_root = None
    source_root_receipt = None
    dest_root_receipt = None
    destination_name_semantics = None
    companion_receipts = []
    companion_seen = set()
    manifest_artifact = None
    for c in chosen:
        payload = c.get("payload", {})
        payload_manifest = payload.get("manifest_artifact")
        if not isinstance(payload_manifest, dict):
            job.error = (
                "This migration review is missing the saved preview details "
                "needed to verify its files. Nothing was changed; scan again "
                "before moving or copying files.")
            job.summary = job.error
            return
        if manifest_artifact is None:
            manifest_artifact = payload_manifest
        elif payload_manifest != manifest_artifact:
            job.error = (
                "The selected albums came from different migration previews. "
                "Nothing was changed; scan again and select albums from one "
                "preview.")
            job.summary = job.error
            return
        payload_source_root = payload.get("source_root")
        payload_source_receipt = payload.get("source_root_receipt")
        payload_dest_receipt = payload.get("dest_root_receipt")
        payload_name_semantics = payload.get("destination_name_semantics")
        if source_root is None:
            source_root = payload_source_root
            source_root_receipt = payload_source_receipt
            dest_root_receipt = payload_dest_receipt
            destination_name_semantics = payload_name_semantics
        elif (
            payload_source_root != source_root
            or payload_source_receipt != source_root_receipt
            or payload_dest_receipt != dest_root_receipt
            or payload_name_semantics != destination_name_semantics
        ):
            job.error = (
                "The selected albums came from different migration previews. "
                "Nothing was changed; scan again and select albums from one "
                "preview.")
            job.summary = job.error
            return
        for receipt in payload.get("companion_receipts", []):
            relative = receipt.get("relative") if isinstance(receipt, dict) else None
            if (
                not isinstance(relative, (list, tuple))
                or not relative
                or any(
                    not isinstance(part, str) or part in ("", ".", "..")
                    for part in relative
                )
            ):
                job.error = (
                    "The saved migration preview is malformed. Nothing was "
                    "changed; scan again.")
                job.summary = job.error
                return
            key = tuple(relative)
            if key not in companion_seen:
                companion_seen.add(key)
                companion_receipts.append(receipt)
        for raw in payload.get("entries", []):
            if not isinstance(raw, (list, tuple)) or len(raw) != 4:
                job.error = (
                    "The saved migration preview is incomplete. Nothing was "
                    "changed; scan again.")
                job.summary = job.error
                return
            src_s, dest_s, sealed_source, sealed_destination_path = raw
            entries.append(engine.PlanEntry(
                source=Path(src_s), status=engine.PLACE, dest_rel=Path(dest_s),
                source_receipt=sealed_source,
                destination_path_receipt=sealed_destination_path))
        for raw in payload.get("resume_entries", []):
            if not isinstance(raw, (list, tuple)) or len(raw) != 4:
                job.error = (
                    "The saved migration preview is incomplete. Nothing was "
                    "changed; scan again.")
                job.summary = job.error
                return
            src_s, dest_s, sealed_source, sealed_destination = raw
            entries.append(engine.PlanEntry(
                source=Path(src_s), status=engine.COLLISION,
                dest_rel=Path(dest_s), reason="destination already exists",
                source_receipt=sealed_source,
                destination_receipt=sealed_destination))
    if not entries:
        job.push_line("Nothing selected. Nothing to copy.")
        return

    plan = engine.MigrationPlan(
        dest_root=dest,
        entries=entries,
        source_root=Path(source_root) if source_root else None,
        source_root_receipt=source_root_receipt,
        dest_root_receipt=dest_root_receipt,
        companion_receipts=companion_receipts,
        destination_name_semantics=destination_name_semantics,
    )
    resume_entries = plan.collisions
    # Serialize the file moves under the staging lock like every other execute
    # flow.
    from qobuz_librarian.web.jobs import set_staging_holder, staging_lock
    # A cross-filesystem move can run for hours; name the holder so a download
    # that blocks on the staging mutex shows what it's waiting behind instead of
    # sitting on RUNNING with no reason (the same treatment the lyrics scan gets).
    result = None
    results_artifact = None
    results_manifest = None
    results_error = None
    execution_abort = None
    with staging_lock():
        set_staging_holder("Library migration")
        try:
            if not engine.verify_audit_artifact(plan, manifest_artifact):
                job.error = (
                    "The saved migration preview no longer matches the files. "
                    "Nothing was moved; scan again before approving it.")
                job.summary = job.error
                log.info(job.summary)
                return
            # This decisive estimate belongs inside the same mutation interval
            # as execution.
            need, free = engine.space_estimate(
                plan, in_place=in_place, resume_entries=resume_entries)
            if (
                in_place
                and free is not None
                and need > free
                and not allow_low_space
            ):
                job.error = (
                    f"Not enough free space at {dest}: the move needs about "
                    f"{format_size(need)} but only {format_size(free)} is free. "
                    "An in-place move that runs out mid-run would leave your "
                    "library half-relocated. Free up space, choose another "
                    "destination, or re-run the migration with “proceed even "
                    "if low on space” checked.")
                job.summary = job.error
                log.info(job.summary)
                return
            try:
                result = engine.execute_plan(
                    plan, in_place=in_place,
                    cancel_check=lambda: job.cancel_requested,
                    progress=job.push_progress,
                    resume_entries=resume_entries)
            except engine.MigrationExecutionAbort as exc:
                result = exc.result
                execution_abort = exc
            pruned = getattr(result, "pruned", 0)
            try:
                results_artifact = engine.write_results_manifest(
                    result, plan=plan)
                results_manifest = Path(results_artifact["path"])
            except BaseException as exc:
                results_error = exc
                if execution_abort is not None:
                    try:
                        job.push_line(
                            "partial migration report could not be saved: "
                            f"{exc}"
                        )
                    finally:
                        execution_abort.reraise(publication_error=exc)
                if not isinstance(exc, Exception):
                    try:
                        job.push_line(
                            "migration report could not be saved: "
                            f"{exc}"
                        )
                    finally:
                        raise exc.with_traceback(exc.__traceback__)
            if execution_abort is not None:
                try:
                    job.push_line(
                        f"partial migration report saved at {results_manifest}"
                    )
                finally:
                    execution_abort.reraise()
        finally:
            set_staging_holder(None)
    for failed_src, reason in result.failures[:50]:
        job.push_line(f"failed: {failed_src} — {reason}")
    companion_outcomes = getattr(result, "companion_outcomes", ())
    companion_skipped = sum(
        status == engine.SKIPPED
        for _source, _destination, status, _reason in companion_outcomes
    )
    companion_failed = sum(
        status == engine.FAILED
        for _source, _destination, status, _reason in companion_outcomes
    )
    for source, _destination, status, reason in companion_outcomes:
        if status == engine.FAILED:
            job.push_line(f"sidecar failed: {source} — {reason}")
    recoveries = tuple(getattr(result, "recoveries", ()))
    for recovery in recoveries:
        location = recovery.get("location", "unknown location")
        restart = recovery.get("restart", "Inspect it before another migration.")
        job.push_line(f"kept for recovery: {location} — {restart}")

    verb = "moved" if in_place else "copied"
    parts = [f"{plural(result.copied, 'file')} {verb} into {dest}"]
    if result.skipped:
        parts.append(f"{result.skipped} skipped (already present)")
    if result.companions:
        parts.append(f"carried {plural(result.companions, 'cover/sidecar file')}")
    if companion_skipped:
        parts.append(
            f"{plural(companion_skipped, 'cover/sidecar file')} already existed"
        )
    if companion_failed:
        parts.append(f"{plural(companion_failed, 'cover/sidecar file')} failed")
    if result.lingered:
        parts.append(f"{result.lingered} moved but the original couldn't be removed")
    if result.failed:
        parts.append(f"{result.failed} failed; see the log")
        # Set job.error too (not just the prose summary) so a migration with
        # failed copies ends red, like every other execute path, instead of a
        # green DONE that buries "N failed" mid-sentence.
        job.error = f"{plural(result.failed, 'file')} couldn't be migrated; see the log."
    if pruned:
        parts.append(f"cleared {plural(pruned, 'empty source folder')}")
    if result.cancelled:
        parts.append("stopped early")
    if recoveries:
        parts.append(
            f"{plural(len(recoveries), 'file')} kept for recovery; see the details"
        )
    if results_error is not None:
        parts.append("the migration report could not be saved")
        job.error = (
            "Migration finished, but its results report could not be saved: "
            f"{results_error}. Check the migration details before running it "
            "again.")
    else:
        parts.append(f"report saved at {results_manifest}")
    if companion_failed or recoveries:
        recovery_attention = (
            f"{plural(len(recoveries), 'file')} kept for recovery"
            if recoveries else ""
        )
        attention = (
            f"{plural(companion_failed, 'sidecar failure') if companion_failed else ''}"
            f"{' and ' if companion_failed and recoveries else ''}"
            f"{recovery_attention}"
        )
        if job.error:
            job.error = f"{job.error} Also: {attention}; see the migration details."
        else:
            job.error = (
                f"Migration needs attention: {attention}. See the migration "
                "details and saved report before running it again."
            )
    job.summary = "; ".join(parts) + "."
    log.info(job.summary)
