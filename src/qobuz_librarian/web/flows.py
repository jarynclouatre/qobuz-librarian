"""Scan / execute logic behind the web Artist and Library flows.

These wrap the same engine the CLI uses (catalog matching, gap detection,
process_album) but without any terminal prompts, a scan attaches review
candidates to the job, and execution runs over the candidates the user kept.
"""
import argparse
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import redaction, repair_log, state_file
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api.auth import AuthLost, QobuzAccess, QobuzUnavailable, load_qobuz_token
from qobuz_librarian.api.search import get_album
from qobuz_librarian.download_result import (
    download_attention_kind,
    incomplete_track_counts,
)
from qobuz_librarian.integrations import beets as beets_mod
from qobuz_librarian.integrations import downsample_engine
from qobuz_librarian.integrations import lyrics as lyrics_mode
from qobuz_librarian.integrations.lyrics import lyric_fetch
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import (
    candidate_premise,
    catalog,
    downsample_state,
    generation_state,
    library_scan_state,
    lyrics,
    scan_checkpoint,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import migrate as migrate_engine
from qobuz_librarian.library import new_releases as new_releases_mod
from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
from qobuz_librarian.library.candidate_premise import CandidateStale
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
from qobuz_librarian.modes import process as process_mode
from qobuz_librarian.modes import repair
from qobuz_librarian.modes.repair import RepairRecovery, RepairRecoveryRequired
from qobuz_librarian.modes.upgrade import BENIGN_UPGRADE_RESULTS
from qobuz_librarian.quality import decision as quality_decision
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.web import job_persistence, review_badges, settings_store
from qobuz_librarian.web import jobs as job_mgr


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
        # Whole-album replacement belongs to the explicit Upgrade review,
        # which enables it for that approved run.
        auto_upgrade=False,
        verbose=False,
    )


def _set_empty_library_summary(job):
    """Describe the expected layout and point an empty scan back to Settings."""
    job.error = (
        f"Nothing to scan: no artist folders were found in {cfg.MUSIC_ROOT}. "
        "Qobuz Librarian expects one folder per artist, each holding album "
        "folders. Check the music folder path in Settings."
    )
    # A run that never scanned anything is not a clean pass. submit_scan leaves
    # an explicitly-set terminal status alone, so this keeps the green Done
    # chip off a scan the user still has to fix something for.
    job.status = job_mgr.JobStatus.FAILED
    log.info("No artist folders found in the configured music library.")
    log.info("  Expected layout: <music library>/<Artist>/<Album (Year)>/<track>.flac")
    log.info("  Check the music library path in Settings.")


def _mark_job_failed(job):
    """Set an explicit terminal outcome for a handled execution failure."""
    job.status = job_mgr.JobStatus.FAILED


def _close_completed_job(job):
    """Publish exact completion under the same lock as cancellation."""
    with job._lock:
        if job.status is job_mgr.JobStatus.RUNNING:
            job.cancel_requested = False
            job.status = job_mgr.JobStatus.DONE


def _record_unchecked_artists(job, count):
    """Keep an incomplete scan's artist count across a restart."""
    count = max(0, int(count))
    job._unchecked_artists = count
    if not isinstance(job.execute_args, dict):
        job.execute_args = {}
    if count:
        job.execute_args["_unchecked_artists"] = count
    else:
        job.execute_args.pop("_unchecked_artists", None)


def _sync_surface_badge(surface):
    """Point the nav dot at whatever that surface's own page would say.

    A saved view the generation authority holds stale shows the user nothing,
    so it has nothing to advertise however full its own store looks.
    """
    if surface == "upgrade":
        state_module = upgrade_state
    elif surface == "downsample":
        state_module = downsample_state
    else:
        return
    review_badges.set_ready(
        surface,
        generation_state.output_is_current(surface)
        and state_module.has_visible_candidates(),
    )


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
    result = downsample_state.update_artist(artist_dir)
    if not result.complete:
        err = next(iter(result.errors.values()), "unknown error")
        generation_state.mark_output_status(
            "downsample",
            "stale",
            reason="Downsample could not refresh after a local album change.",
        )
        log.info(f"  downsample view refresh skipped for {artist_dir.name}: {err}")
    _sync_surface_badge("downsample")


def _refresh_upgrade_artist_state(artist_dir, token, args=None):
    if not cfg.UPGRADE_SCAN_ENABLED:
        review_badges.set_ready("upgrade", False)
        return
    if artist_dir is None or not token:
        return
    try:
        result = upgrade_state.update_artist(
            artist_dir,
            token=token,
            args=args or build_args(),
            capped=quality_decision.load_capped(),
        )
    except (AuthLost, QobuzUnavailable):
        raise
    if not result.complete:
        err = next(iter(result.errors.values()), "unknown error")
        generation_state.mark_output_status(
            "upgrade",
            "stale",
            reason="Upgrade could not refresh after a local album change.",
        )
        log.info(f"  upgrade view refresh skipped for {artist_dir.name}: {err}")
    _sync_surface_badge("upgrade")


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
    album_id = (album or {}).get("id")
    authority = generation_state.load()
    if (
        album_id
        and generation_state.output_is_current("library", state=authority)
        and not library_scan_state.remove_album(album_id)
    ):
        generation_state.mark_output_status(
            "library",
            "stale",
            reason="An in-app album change could not update the saved Library view.",
        )
    if album_id and not new_releases_mod.note_owned_album(album):
        if generation_state.output_is_current(
            "new_releases", state=authority
        ):
            generation_state.mark_output_status(
                "new_releases",
                "stale",
                reason="An in-app album change could not update New Releases.",
            )
    if not album_id:
        for surface, reason in (
            (
                "library",
                "An album changed in the app, and it could not be matched "
                "to anything in these saved results.",
            ),
            (
                "new_releases",
                "An album changed in the app, and it could not be matched "
                "to anything in these saved New Releases results.",
            ),
        ):
            if generation_state.output_is_current(surface, state=authority):
                generation_state.mark_output_status(
                    surface,
                    "stale",
                    reason=reason,
                )
    artist_dir = _artist_dir_from_result(album, result, fallback_artist)
    if artist_dir is None:
        surfaces = []
        if upgrade and generation_state.output_is_current(
            "upgrade", state=authority
        ):
            surfaces.append("upgrade")
        if downsample and generation_state.output_is_current(
            "downsample", state=authority
        ):
            surfaces.append("downsample")
        if surfaces:
            generation_state.invalidate(
                surfaces,
                "An album changed in the app, and its artist folder could "
                "not be identified.",
            )
            for surface in surfaces:
                _sync_surface_badge(surface)
        return
    if upgrade:
        try:
            _refresh_upgrade_artist_state(artist_dir, token, args=args)
        except (AuthLost, QobuzUnavailable) as exc:
            generation_state.mark_output_status(
                "upgrade",
                "stale",
                reason="Upgrade could not refresh after a local album change.",
            )
            _sync_surface_badge("upgrade")
            log.info(f"  upgrade view needs refresh: {exc}")
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


def _spec_dismissed(spec, hidden):
    """Is this saved candidate on the dismissed list right now? The snapshot
    keeps dismissed albums so Restore has something to bring back, so every
    path that shows saved candidates filters here instead."""
    return hidden_mod.is_hidden(
        hidden_mod.SCOPE_MISSING,
        spec.get("artist"),
        spec.get("title"),
        hidden,
        year=(spec.get("payload") or {}).get("year"),
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
    extra_payload = {"_artist_dir": artist_key} if artist_key else {}
    if gap.on_disk_dir is not None:
        extra_payload["album_dir"] = str(gap.on_disk_dir)
        receipts = {}
        for track in gap.present:
            source = track.get("path") if isinstance(track, dict) else None
            sealed = (
                backup_mod.capture_gap_fill_source_receipt(source, gap.on_disk_dir)
                if source else None
            )
            if sealed is None:
                receipts = {}
                break
            receipts[sealed["relative"]] = sealed["file"]
        extra_payload["_gap_fill_receipts"] = receipts
        premise = candidate_premise.capture("gap-fill", gap.on_disk_dir)
    else:
        artist_dir = cfg.MUSIC_ROOT / artist_key if artist_key else None
        extra_payload["_artist_dir_path"] = str(artist_dir or "")
        premise = candidate_premise.capture("missing", artist_dir) if artist_dir else None
    if premise is not None:
        extra_payload["_premise"] = premise
    return _album_candidate_spec(
        album, artist_name, selected=selected, is_new=is_new,
        extra_payload=extra_payload)


def _add_gap_candidate(job, gap, artist_name, selected=False, is_new=False,
                       artist_key=None):
    return _add_candidate_spec(
        job, _gap_candidate_spec(
            gap, artist_name, selected, is_new, artist_key=artist_key))


def candidate_matches_query(c, q):
    """The review filter's predicate, artist or album title contains ``q``.

    Shared by the fragment render and bulk endpoints so the visible filter and
    affected rows remain identical. Both the query and candidate are folded.
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
    period. Every writer of that summary (the scan when it parks, the
    restart rebuild) goes through here, so the History card cannot call Gap
    Fill candidates "missing albums" or disagree with the tabs it describes.
    Splits with the same predicate the tabs use."""
    gaps = sum(1 for c in candidates if is_gap_candidate(c))
    missing = len(candidates) - gaps
    return (f"{len(candidates):,} to review across Missing Albums "
            f"({missing:,}) and Gap Fill ({gaps:,})")


def split_review_summary(execute_kind, candidates):
    """The one-line count for a review split off a partial download, recounted
    for the candidates left behind. The split copies the parked review but not
    its summary, so its History card had nothing where the count belongs.
    Shapes match what each scan writes when it parks, so a review screen
    treats it exactly as it treats the original's.

    Repair is left out on purpose: its summary is caveat-driven, and a bare
    count would drop what the scan could not check."""
    n = len(candidates)
    if not n:
        return ""
    if execute_kind == "library":
        return library_review_summary(candidates) + "."
    if execute_kind == "new_releases":
        return f"{plural(n, 'new release')} found across the library."
    if execute_kind == "upgrade":
        return (f"{plural(n, 'upgradeable album')} Qobuz can serve at "
                "higher quality.")
    if execute_kind == "downsample":
        return f"{plural(n, 'album')} stored above CD rate."
    return ""


def fold_key(c):
    """A candidate's merge identity: Qobuz album id, falling back to
    artist+title for keyless carry-overs. Shared by the fold and its caller's
    before/after arithmetic so the summary counts what actually changed."""
    album_id = str((c.get("payload") or {}).get("album_id") or "")
    if album_id:
        return album_id
    return ((c.get("artist") or "").lower(), (c.get("title") or "").lower())


def _fold_row(c):
    """Candidate content a refresh owns, excluding review identity and tick."""
    return {
        "kind": c.get("kind", "album"),
        "title": c.get("title") or "?",
        "artist": c.get("artist") or "",
        "detail": c.get("detail") or "",
        "payload": c.get("payload") or {},
    }


def fold_new_candidates(parked, cands, *, review_generation=None):
    """Merge a refresh's finds into a parked review, keyed by Qobuz album id
    (falling back to artist+title for keyless carry-overs).

    Entries the refresh didn't touch, and the user's ticks on them, are
    never changed. A same-key row found by the refresh takes its fresh title,
    detail, class, and payload while keeping the review row's cid, sequence,
    and user tick. Absence from the refresh is NOT evidence of change (the
    cheap refresh skips unchanged artists), so nothing is removed on that
    basis. Candidates the user dismissed while the refresh ran are checked
    against a fresh hidden snapshot so the fold can't resurrect them. Returns
    (added, updated), False when the changed review could not be saved, or None
    when the review stopped being parked mid-refresh (approved/discarded)."""
    _key = fold_key
    def _fold():
        if parked.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return None
        # Load while the review-action lock is held. A dismissal that finishes
        # while this fold waits must be visible before any candidate is added.
        hidden = hidden_mod.load()
        fresh_by_key = {}
        for c in cands:
            fresh_by_key.setdefault(_key(c), c)
        updated = 0
        keep = []
        for c in parked.candidates:
            key = _key(c)
            fresh = fresh_by_key.get(key)
            if fresh is None or _fold_row(fresh) == _fold_row(c):
                keep.append(c)
                continue
            replacement = _fold_row(fresh)
            replacement.update({
                "cid": c.get("cid"),
                "seq": c.get("seq"),
                "selected": bool(c.get("selected")),
            })
            keep.append(replacement)
            updated += 1
        parked.candidates = keep
        seen = {_key(c) for c in keep}
        added = 0
        for c in cands:
            key = _key(c)
            if key in seen:
                continue
            if hidden_mod.is_hidden(
                    hidden_mod.SCOPE_MISSING, c.get("artist") or "",
                    c.get("title") or "", hidden,
                    year=(c.get("payload") or {}).get("year")):
                continue
            seen.add(key)
            if len(parked.candidates) >= parked.CANDIDATE_CAP:
                parked._candidate_cap_noted = True
                if isinstance(parked.execute_args, dict):
                    parked.execute_args["_candidate_cap_hit"] = True
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
                "selected": bool(c.get("selected")),
            })
            added += 1
        if review_generation is not None:
            parked.execute_args = {
                **(parked.execute_args or {}),
                "_library_review_generation": int(review_generation),
            }
        return added, updated

    saved, result = job_persistence.persist_review_mutation(parked, _fold)
    return result if saved else False


def _refresh_restored_missing_spec(spec, token):
    """Reclassify a restored missing album when its folder now exists."""
    if not token:
        return spec
    payload = spec.get("payload") or {}
    album_id = payload.get("album_id")
    if not album_id:
        return spec
    candidate_album = {
        "id": album_id,
        "title": spec.get("title") or "",
        "artist": {"name": spec.get("artist") or ""},
    }
    try:
        if catalog.find_album_dir_filesystem(candidate_album) is None:
            return spec
        album = get_album(album_id, token)
        tracks = (album.get("tracks") or {}).get("items") or []
        if not tracks:
            return spec
        album_dir = catalog.find_album_dir_filesystem(album)
        if album_dir is None:
            return spec
        existing, _ = catalog.find_existing_tracks(album, album_dir=album_dir)
        missing, _ = catalog.compute_missing(tracks, existing)
    except Exception:
        return spec
    if not missing:
        return None
    if len(missing) == len(tracks):
        return spec

    album = dict(album)
    album["_partial_missing_count"] = len(missing)
    refreshed = _album_candidate_spec(
        album, spec.get("artist") or "", selected=False)
    restored = dict(spec)
    restored["detail"] = refreshed["detail"]
    restored["payload"] = {**payload, **refreshed["payload"]}
    return restored


def refold_restored_missing(artists, fingerprints):
    """Return just-restored dismissals to the open Library review.

    Dismissing drops rows from the parked job for good, so clearing the
    hidden entries alone leaves Restore looking like a no-op until some
    future scan re-carries them, and most users never scan again. Rebuild
    the restored artists'/albums' candidate specs from the saved scan state
    and fold them back in, unselected. Returns how many rejoined the review,
    or None when no library review is parked (they return on the next scan)."""
    parked = None
    for job in job_mgr.registry.awaiting_review():
        if getattr(job, "execute_kind", "") != "library":
            continue
        if parked is None or (job.created_at or 0) > (parked.created_at or 0):
            parked = job
    if parked is None:
        return None
    active_album_ids = set()
    for job in job_mgr.registry.pending_and_running():
        if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        if job.album_id:
            active_album_ids.add(str(job.album_id))
        for candidate in list(job.candidates or []):
            payload = candidate.get("payload") or {}
            album_id = payload.get("album_id")
            if album_id:
                active_album_ids.add(str(album_id))
            qobuz_album = ((payload.get("candidate") or {})
                           .get("qobuz_album") or {})
            if qobuz_album.get("id"):
                active_album_ids.add(str(qobuz_album["id"]))
    wanted_artists = {(a or "").lower() for a in artists}
    wanted_fps = set(fingerprints)
    specs = []
    try:
        token = load_qobuz_token()[1]
    except Exception:
        token = None
    state = library_scan_state.kind_state("missing")
    for name, entry in (state.get("artists") or {}).items():
        for spec in (entry or {}).get("candidates") or []:
            artist = spec.get("artist") or name
            title = spec.get("title") or ""
            if ((artist or "").lower() in wanted_artists
                    or hidden_mod.album_fingerprint(artist, title)
                    in wanted_fps):
                spec = dict(spec)
                album_id = (spec.get("payload") or {}).get("album_id")
                if album_id and str(album_id) in active_album_ids:
                    continue
                spec = _refresh_restored_missing_spec(spec, token)
                if spec is None:
                    continue
                spec["selected"] = False
                specs.append(spec)
    if not specs:
        return 0
    folded = fold_new_candidates(parked, specs)
    if folded is False:
        return False
    if folded is None:
        return None
    added = folded[0]
    if added:
        parked.notify_review_changed()
    return added


def refold_into_living_review(picks, execute_kind="library", ticked=True):
    """Fold a list of picks back into the newest parked review of
    ``execute_kind``, ticked by default, so they come back ready to retry
    instead of stranding in a dead job. Callers include picks a cancelled
    download never reached, albums that failed during a partial approval, and
    the unticked Gap Fill candidate left by a partial import. The living review
    is the split-off review left behind at approval; when the whole review was
    selected, those paths rebuild or re-park their own review instead. Dedups by
    fold key, so re-adding a pick already in the review is safe. Returns how
    many rejoined, or None when there's no review to fold into."""
    parked = None
    for j in job_mgr.registry.awaiting_review():
        if getattr(j, "execute_kind", "") != execute_kind:
            continue
        if parked is None or (j.created_at or 0) > (parked.created_at or 0):
            parked = j
    if parked is None:
        return None
    specs = [dict(c, selected=True) if ticked else dict(c) for c in picks]
    # A count we wrote ourselves has to be rewritten once the review grows, or
    # the History card keeps the number it had at the split and disagrees with
    # the review it points at. A scan's own summary is left alone: it carries
    # caveats a bare count would throw away.
    ours = (parked.summary or "") == split_review_summary(
        getattr(parked, "execute_kind", ""), parked.candidates)
    folded = fold_new_candidates(parked, specs)
    if folded is False:
        return False
    if folded is None:
        return None
    if ours:
        parked.summary = split_review_summary(
            getattr(parked, "execute_kind", ""), parked.candidates)
    parked.notify_review_changed()
    return folded[0]


def _park_library_failures(failed_cands, execute_kind="library",
                           ticked=True, summary=None, execute_args=None):
    """After a whole-review download (every candidate ticked) finishes, retire
    empties the living review, so the albums that FAILED to download would
    vanish until the next scan. Re-park them as a fresh living review of
    ``execute_kind``, ticked, so they come back ready to retry (mirrors the
    split-off review left by a partial approval). Persisted so a restart
    keeps them even though the retired baseline no longer rebuilds. Also the
    park half of the unticked instant Gap Fill fold, via ``ticked``/
    ``summary``. Returns the parked job, or None when nothing failed."""
    if not failed_cands:
        return None
    if execute_kind == "new_releases":
        job = job_mgr.Job(title="New-release check",
                          execute_kind="new_releases",
                          status=job_mgr.JobStatus.AWAITING_REVIEW)
    elif execute_kind == "upgrade":
        job = job_mgr.Job(title="Upgrade candidates",
                          execute_kind="upgrade",
                          status=job_mgr.JobStatus.AWAITING_REVIEW)
    elif execute_kind == "repair":
        job = job_mgr.Job(title="Repair scan", kind="scan",
                          execute_kind="repair",
                          status=job_mgr.JobStatus.AWAITING_REVIEW,
                          review_verb="Repair")
    else:
        job = job_mgr.Job(title="Library scan", kind="scan",
                          execute_kind="library",
                          status=job_mgr.JobStatus.AWAITING_REVIEW)
    job.execute_args = dict(execute_args or {})
    job.execute_args.pop("_credential_generation", None)

    def fresh_download_token():
        return api_client.authorize_qobuz_action(QobuzAccess.DOWNLOAD_ACTION).token

    if execute_kind == "upgrade":
        job._execute_fn = lambda j, chosen: execute_upgrades(
            j, chosen, fresh_download_token())
    elif execute_kind == "repair":
        job._execute_fn = lambda j, chosen: execute_repairs(
            j, chosen, fresh_download_token())
    else:
        job._execute_fn = lambda j, chosen: execute_albums(
            j, chosen, fresh_download_token())
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
    elif execute_kind == "upgrade":
        job.summary = (f"{n:,} album{'s' if n != 1 else ''} didn't upgrade "
                       "last time. Ticked and ready to retry.")
    elif execute_kind == "repair":
        job.summary = (f"{n:,} album{'s' if n != 1 else ''} couldn't be "
                       "repaired last time. Ticked and ready to retry.")
    else:
        job.summary = (f"{n:,} album{'s' if n != 1 else ''} didn't download "
                       "last time. Ticked and ready to retry.")
    if not job_persistence.admit(job):
        return False
    job_mgr.registry.add(job)
    return job


def _return_library_picks(picks):
    """Return unfinished picks to the Library review, creating one if needed."""
    if not picks:
        return True
    folded = refold_into_living_review(picks)
    if folded is False:
        return False
    if folded is None:
        return _park_library_failures(picks) is not False
    return True


def _return_new_release_picks(picks):
    """A new release stays in the New Releases review until it's downloaded or
    dismissed, so picks that failed, were cancelled, or never ran fold back
    into the parked new-release review, ticked; with none parked (the whole
    review was ticked at approve) they re-park as a fresh one. The persistent
    baseline consumed these ids at check time, so without this they'd never
    be offered as new again."""
    if not picks:
        return True
    folded = refold_into_living_review(picks, execute_kind="new_releases")
    if folded is False:
        return False
    if folded is None:
        return _park_library_failures(
            picks, execute_kind="new_releases") is not False
    return True


def _return_qobuz_review_picks(picks, execute_kind, execute_args=None):
    """Return unfinished Upgrade or Repair picks to the living review."""
    if not picks:
        return True
    folded = refold_into_living_review(picks, execute_kind=execute_kind)
    if folded is False:
        return False
    if folded is None:
        return _park_library_failures(
            picks,
            execute_kind=execute_kind,
            execute_args=execute_args,
        ) is not False
    return True


def _fold_partial_gap_fill(full_album, artist_name, n_missing):
    """A partial download (some tracks failed) leaves the album on disk with
    gaps. Fold it into the living Library review as an unticked Gap Fill
    candidate right away, the app tracks its own downloads, so the gap must
    not wait for the next manual refresh to become visible. With no library
    review parked, a fresh one is parked so /library shows it. Runs after
    prune_library_review_candidates, which just dropped the album's stale
    Missing candidates, this replaces them with the honest remainder."""
    spec = _album_candidate_spec(
        {**full_album, "_partial_missing_count": n_missing},
        artist_name, selected=False)
    folded = refold_into_living_review([spec], ticked=False)
    if folded is False:
        return False
    if folded is None:
        return _park_library_failures(
            [spec], ticked=False,
            summary="1 album downloaded only partly: the missing tracks "
                    "are ready as Gap Fill.") is not False
    return True


def prune_library_review_candidates(album):
    """A full album just landed on disk (Search download, batch download,
    upgrade replace): drop its candidates from every parked or still-scanning
    library review, so a stale review can't offer to download an album the
    user already has. The executing job itself is RUNNING and untouched.
    Matched by Qobuz album id. Returns the number of candidates dropped."""
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
            def _drop():
                if job.status not in states:
                    return 0
                keep = [c for c in job.candidates
                        if str((c.get("payload") or {}).get("album_id") or "")
                        != album_id]
                n = len(job.candidates) - len(keep)
                if not n:
                    return 0
                job.candidates = keep
                return n

            saved, n = job_persistence.persist_review_mutation(job, _drop)
            if not saved:
                job.notify_review_changed("save_failed")
                continue
            if not n:
                continue
            dropped += n
            job.notify_review_changed()
            job_mgr.finalize_review_if_empty(job)
        except Exception as e:
            # Pruning is housekeeping on the side of a successful download,
            # never let it turn that success into a failure.
            log.info(f"  couldn't prune review {job.id}: {e}")
    return dropped


def _library_review_lists(album_id):
    states = (job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)
    for job in job_mgr.registry.all():
        if (getattr(job, "execute_kind", "") not in ("library", "new_releases")
                or job.status not in states):
            continue
        if any(str((c.get("payload") or {}).get("album_id") or "") == album_id
               for c in list(job.candidates)):
            return True
    return False


def apply_pending_review_removals(state=None):
    """Take albums downloaded in the terminal off the living Library review.

    A terminal run shares the library, the settings and the run lock, but the
    review is held in memory by whichever process serves the web UI, so it
    cannot reach it. It records what it took off the saved missing list; this
    applies those records, so the page cannot report an album as both on disk
    and missing. Records are cleared once no review carries the album, which
    also covers the web's own downloads: they pruned their review as they ran,
    so the first pass here just clears them.
    """
    pending = generation_state.pending_review_removals(state)
    if not pending:
        return 0
    dropped = 0
    settled = []
    for album_id in pending:
        dropped += prune_library_review_candidates({"id": album_id})
        if not _library_review_lists(album_id):
            settled.append(album_id)
    if settled:
        generation_state.clear_review_removals(settled)
    return dropped


def owned_missing_candidate_ids(job, token, candidate_ids=None):
    """Return selected missing albums whose exact edition is already complete.

    A non-empty folder is not proof of ownership: it may hold one track from a
    partial import or unrelated audio. Fetch the selected edition and use the
    normal one-to-one track matcher. Any lookup, read, or identity uncertainty
    leaves the candidate alone. Gap Fill candidates are already partial by
    definition and are never considered.
    """
    eligible = set(candidate_ids) if candidate_ids is not None else None
    with job._lock:
        snapshot = [
            (
                c["cid"],
                {
                    "id": (c.get("payload") or {}).get("album_id"),
                    "title": c.get("title") or "",
                    "artist": {"name": c.get("artist") or ""},
                },
            )
            for c in job.candidates
            if (c.get("selected") and not is_gap_candidate(c)
                and (eligible is None or c.get("cid") in eligible))
        ]
    # Provider and disk reads happen outside the lock. A live scan may keep
    # appending, but results remain bound to the exact candidate IDs checked.
    owned = set()
    for cid, candidate_album in snapshot:
        album_id = candidate_album.get("id")
        if not album_id:
            continue
        try:
            # Most selected Missing Albums have no matching folder at all and
            # need no extra provider request. Only materialize track metadata
            # when the cheap resolver finds something that might be complete.
            if catalog.find_album_dir_filesystem(candidate_album) is None:
                continue
            album = get_album(album_id, token)
            tracks = (album.get("tracks") or {}).get("items") or []
            if not tracks:
                continue
            album_dir = catalog.find_album_dir_filesystem(album)
            if album_dir is None:
                continue
            existing, _ = catalog.find_existing_tracks(album, album_dir=album_dir)
            missing, _ = catalog.compute_missing(tracks, existing)
            if existing and not missing:
                owned.add(cid)
        except Exception:
            continue
    return owned


def drop_owned_missing_candidates(job, token):
    """Remove selected missing albums already proven complete on disk."""
    owned = owned_missing_candidate_ids(job, token)
    if not owned:
        return 0
    def _drop():
        before = len(job.candidates)
        job.candidates = [c for c in job.candidates if c["cid"] not in owned]
        return before - len(job.candidates)

    saved, dropped = job_persistence.persist_review_mutation(job, _drop)
    if not saved:
        job.notify_review_changed("save_failed")
        return 0
    job.notify_review_changed()
    return dropped


def _record_last_scan():
    try:
        cfg.LAST_SCAN_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _load_scan_seen(mode):
    """Fingerprints the last completed walk of this mode surfaced, or None if
    there's no prior run to compare against (first scan badges nothing)."""
    try:
        data = state_file.load_json_object(
            cfg.SCAN_SEEN_FILE,
            "the saved new-since-scan baseline",
            "your previous scan comparison",
        )
    except OSError:
        return None
    bucket = data.get(mode) if isinstance(data, dict) else None
    return set(bucket) if isinstance(bucket, list) else None


def _save_scan_seen(mode, fingerprints):
    try:
        with state_file.store_lock(cfg.SCAN_SEEN_FILE):
            data = state_file.load_json_object(
                cfg.SCAN_SEEN_FILE,
                "the saved new-since-scan baseline",
                "your previous scan comparison",
            ) or {}
            data[mode] = sorted(fingerprints)
            state_file.write_json(cfg.SCAN_SEEN_FILE, data)
    except OSError:
        pass


def _flag_new_since_last_scan(job, mode):
    """Badge candidates whose album wasn't surfaced by the previous walk, then
    record this walk's set for next time. First-ever run badges nothing (no
    baseline to diff). Skipped on a cancelled scan so a partial run can't
    poison the baseline."""
    # Snapshot under the lock, the scan worker appends candidates to this
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
    selections are never touched, critical now that pagination means most of
    them aren't even on the page that triggered the hide.

    ``gap_only`` narrows the hide to one side of a library review's tab split:
    True drops only Gap Fill candidates, False only fully missing albums, None
    (the default) both. The button only ever shows one tab's rows, so it must
    not silently dismiss the other tab's. ``query`` narrows the same way for
    an active review filter, only the rows the user can see leave.

    The hidden albums are recorded in the durable store so future bulk walks of
    that scope skip them, then dropped from this job's review list. Returns the
    number hidden, or False when the matching review snapshot could not be
    saved.
    """
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
        # different records, "The Asylum Albums (1972-1975)" and "(1976-1980)"
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
        gap_flags = [is_gap_candidate(c) for c in to_hide]
    # Record the dismissals durably FIRST, outside the lock (disk I/O mustn't
    # stall the scan thread's next add_candidate).
    hidden_before = hidden_mod.load()
    new_specs = [
        spec for spec in specs
        if not hidden_mod.is_hidden_row(scope, *spec, hidden_before)
    ]
    hidden_mod.hide(scope, specs, gap_fill=gap_flags)
    drop = {c["cid"] for c in to_hide}

    def _drop():
        # Re-read the ticks under the lock: a selection saved while the store
        # write above ran was promised "keep the ticked ones", so it wins,
        # dropping the stale snapshot's ids wholesale would dismiss an album
        # the user just ticked.
        ticked_meanwhile = [c for c in job.candidates
                            if c["cid"] in drop and c.get("selected")]
        keep = {c["cid"] for c in ticked_meanwhile}
        # Only this artist's unselected candidates leave; every other
        # candidate (and its saved selection) is preserved untouched.
        job.candidates = [c for c in job.candidates
                          if c["cid"] not in drop or c["cid"] in keep]
        return ticked_meanwhile

    saved, ticked_meanwhile = job_persistence.persist_review_mutation(job, _drop)
    if not saved:
        hidden_mod.restore_rows(scope, new_specs)
        return False
    if ticked_meanwhile:
        # Their dismissals were already written durably, take them back out,
        # or the next bulk walk skips albums that are visibly ticked here.
        new_keys = {
            (artist_name or "", title or "", str(year or ""))
            for artist_name, title, year in new_specs
        }
        raced_specs = [
            (c.get("artist"), c.get("title"),
             (c.get("payload") or {}).get("year"))
            for c in ticked_meanwhile
            if (c.get("artist") or "", c.get("title") or "",
                str((c.get("payload") or {}).get("year") or "")) in new_keys
        ]
        hidden_mod.restore_rows(scope, raced_specs)
    return len(to_hide) - len(ticked_meanwhile)


# ── Scans ─────────────────────────────────────────────────────────────────────


def _scan_library_artist(artist_dir, token, partial_only, hidden):
    """Worker: find one artist's gaps. Runs in a pool thread (its own HTTP
    session); returns plain data so the caller adds candidates serially,
    keeping job.candidates single-writer. Also returns the artist's id and its
    lossless catalog ids so the caller can seed the new-release baseline (the
    discography is already fetched here).

    Dismissed albums come back too. The caller keeps them out of the review but
    saves them in the snapshot, so dismissing costs no rescan and Restore has a
    record to bring back."""
    result = find_missing_for_artist(
        artist_dir.name, token=token,
        opts=DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES),
        artist_dir=artist_dir, hidden=hidden,
        single_store=hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None,
        want_missing=not partial_only, include_hidden=True)
    artist_id = str(result.artist_id) if result.artist_id else None
    # None signals "don't seed a baseline", a transient short-page fetch
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
_REPAIR_CHECKPOINT_VERSION = 2
_REPAIR_RESULT_VERSION = 1


def _repair_checkpoint_signature():
    """Version the settings that affect saved Repair results."""
    return {
        "result_version": _REPAIR_RESULT_VERSION,
        "deep_decode": True,
        "min_short_seconds": 30,
        "max_ratio": 0.85,
        "max_short_seconds": 60,
        "flac_tool": bool(shutil.which("flac")),
    }


def _capture_repair_artist_proof(artist_dir):
    """Hash the exact artist tree used by a resumable Repair result."""
    return backup_mod.capture_album_source_receipt(Path(artist_dir))


def _repair_checkpoint_bundle(name, agg, proof):
    return {
        "version": _REPAIR_CHECKPOINT_VERSION,
        "scan_signature": _repair_checkpoint_signature(),
        "proof": proof,
        "candidates": list(agg.get("specs") or []),
        "counts": {
            "verified_ok": int(agg.get("verified_ok") or 0),
            "unverified": int(agg.get("unverified") or 0),
            "failed_albums": int(agg.get("failed") or 0),
        },
        "artist": name,
    }


def _validated_repair_checkpoint_bundle(artist_dir, value):
    """Return a reusable per-artist bundle, or None so Repair scans it again."""
    name = Path(artist_dir).name
    if (
        type(value) is not dict
        or set(value) != {
            "version", "scan_signature", "proof", "candidates", "counts",
            "artist",
        }
        or value.get("version") != _REPAIR_CHECKPOINT_VERSION
        or value.get("scan_signature") != _repair_checkpoint_signature()
        or value.get("artist") != name
        or type(value.get("candidates")) is not list
        or type(value.get("counts")) is not dict
        or set(value["counts"]) != {
            "verified_ok", "unverified", "failed_albums",
        }
        or any(
            type(value["counts"].get(key)) is not int
            or value["counts"][key] < 0
            for key in ("verified_ok", "unverified", "failed_albums")
        )
        # Failed albums were not fully checked. Retry their artist instead of
        # reusing an incomplete result in a later clean scan.
        or value["counts"]["failed_albums"]
    ):
        return None
    proof = backup_mod.canonical_album_source_receipt(
        value.get("proof"),
        expected_origin=str(Path(artist_dir).absolute()),
    )
    if proof is None:
        return None
    candidates = []
    for spec in value["candidates"]:
        if (
            type(spec) is not dict
            or spec.get("artist") != name
            or spec.get("kind") not in ("repair", "redownload")
            or candidate_premise.canonical(spec) is None
        ):
            return None
        if spec.get("kind") == "repair":
            payload = spec.get("payload") or {}
            try:
                repair._verified_repair_source_receipts(
                    payload.get("verified_truncated") or [],
                    payload.get("album_dir"),
                )
            except (OSError, TypeError, ValueError):
                return None
        candidates.append(spec)
    current = backup_mod.canonical_album_source_receipt(
        _capture_repair_artist_proof(artist_dir),
        expected_origin=str(Path(artist_dir).absolute()),
    )
    if current is None or not candidate_premise.durable_receipts_match(proof, current):
        return None
    return {
        **value,
        "proof": proof,
        "candidates": candidates,
        "counts": dict(value["counts"]),
    }


def scan_library(job, token, partial_only=False, force_full=False):
    previous_generation = generation_state.load()
    previous_attempt_status = str(
        (previous_generation.get("latest_attempt") or {}).get("status")
        or "never"
    )
    allow_checkpoint_resume = bool(
        partial_only
        or previous_attempt_status in {"running", "failed", "incomplete"}
        or generation_state.library_publication_incomplete(previous_generation)
        or (
            not int(previous_generation.get("generation") or 0)
            and previous_attempt_status == "never"
        )
    )
    attempt_id = None if partial_only else generation_state.begin_attempt()
    if not partial_only and attempt_id is None:
        job.error = (
            "The Library scan could not save its starting state. Check the "
            "data folder; no catalogue crawl was started."
        )
        job.summary = job.error
        _mark_job_failed(job)
        return
    scan_started_revision = generation_state.revision()
    try:
        return _scan_library_impl(
            job,
            token,
            partial_only=partial_only,
            force_full=force_full,
            attempt_id=attempt_id,
            scan_started_revision=scan_started_revision,
            allow_checkpoint_resume=allow_checkpoint_resume,
        )
    except Exception as exc:
        if not partial_only:
            generation_state.finish_attempt(attempt_id, "failed", str(exc))
        raise


def _scan_library_impl(
    job,
    token,
    *,
    partial_only=False,
    force_full=False,
    attempt_id=None,
    scan_started_revision=0,
    allow_checkpoint_resume=True,
):
    clear_scan_caches()
    # Drop the Various-Artists folder: it has no single Qobuz artist catalog
    # to diff against, so a gap scan can only mis-resolve it.
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        _set_empty_library_summary(job)
        if not partial_only:
            generation_state.finish_attempt(
                attempt_id, "incomplete", "No library artist folders were found."
            )
        return
    kind = "partial" if partial_only else "missing"
    # Resume an interrupted scan of this kind: skip the artists already done and
    # restore the albums they turned up, so we continue rather than restart.
    cp = scan_checkpoint.load(kind) if allow_checkpoint_resume else None
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
    quality_sig = library_scan_state.quality_signature()
    previous_scan = library_scan_state.kind_state(kind)
    # Dismissing and restoring deliberately do NOT bar the cheap path. The
    # snapshot holds every candidate the last crawl found, dismissed or not, so
    # both directions are a filter over saved data, not a reason to ask Qobuz
    # about the whole library again.
    cheap_refresh = (
        not force_full
        and not resuming
        and previous_scan.get("complete")
        # Saved candidates computed under a different quality policy must not
        # carry forward, a settings change re-derives even unchanged folders.
        and previous_scan.get("quality_signature", "") == quality_sig
    )
    # The two refreshes and the fingerprint pass below run before the main
    # artist loop, and on a first scan of a large library each takes real
    # minutes, without progress ticks the job sits on "Waiting for output"
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
        upgrade_refresh_started_at = time.time()
        log.info("Comparing owned albums against the editions Qobuz can serve…")
        upgrade_refresh = upgrade_state.refresh_for_artists(
            artists,
            token=token,
            args=build_args(),
            capped=quality_decision.load_capped(),
            hidden=hidden,
            cancel_check=lambda: bool(job.cancel_requested),
            workers=max(1, int(cfg.ARTIST_SCAN_WORKERS)),
            pool_kwargs=job_mgr.pool_initializer_kwargs(),
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
    if resuming:
        # A checkpoint says what was compared, not that the local folder is
        # still the same. Reuse only a current matching per-artist fingerprint;
        # removed and legacy entries fall away independently.
        scanned = {
            name
            for name in scanned
            if isinstance(checkpoint_artists.get(name), dict)
            and checkpoint_artists[name].get("fingerprint")
            == fingerprints.get(name)
        }
        baseline_seen = {}
        for name in scanned:
            saved = checkpoint_artists[name]
            artist_id = saved.get("artist_id") or ""
            if artist_id:
                baseline_seen[str(artist_id)] = list(
                    saved.get("catalog_ids") or [])
    previous_artists = (previous_scan.get("artists") or {}) if cheap_refresh else {}
    state_artists: dict[str, dict] = {}
    if resuming:
        restored_by_artist: dict[str, list[dict]] = {}
        for c in cp["candidates"]:
            artist_key = (c.get("payload") or {}).get("_artist_dir") or c.get("artist")
            if artist_key not in scanned:
                continue
            if hidden_mod.is_hidden(hidden_mod.SCOPE_MISSING,
                                    c.get("artist"), c.get("title"), hidden,
                                    year=(c.get("payload") or {}).get("year")):
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
            candidates = list(saved.get("candidates") or [])
            state_artists[name] = {
                "fingerprint": saved.get("fingerprint") or fingerprints.get(name, ""),
                "candidates": candidates or restored_by_artist.get(name, []),
                "artist_id": saved.get("artist_id") or "",
                "catalog_ids": list(catalog_ids or []),
            }
        recheck = len(artists) - len(scanned)
        log.info(
            f"Resuming. Reusing {plural(len(scanned), 'unchanged artist')}; "
            f"checking {plural(recheck, 'changed or unproven artist')} again. "
            f"{plural(total, 'album')} found so far."
        )
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
            candidates = list(saved.get("candidates") or [])
            visible = [c for c in candidates if not _spec_dismissed(c, hidden)]
            for c in visible:
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
            hit = ({"artist": artist_dir.name, "albums": len(visible)}
                   if visible else None)
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
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="libscan",
                            **job_mgr.pool_initializer_kwargs()) as ex:
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
                # A lost token or an unreachable API isn't a per-artist
                # hiccup, so cancel the rest and fail the scan rather than
                # silently report a partial library as the full picture.
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
            shown = 0
            for gap in gaps:
                # Library is a discovery list, leave candidates unticked so a
                # single click can't queue hundreds nobody reviewed.
                spec = _gap_candidate_spec(
                    gap, artist_name or name, selected=False, artist_key=name)
                if _spec_dismissed(spec, hidden):
                    # Saved but not reviewed: the snapshot is the record
                    # Restore brings back from.
                    artist_candidates.append(spec)
                    continue
                if _add_candidate_spec(job, spec) is not None:
                    artist_candidates.append(spec)
                    total += 1
                    shown += 1
            if catalog_ids is not None:
                state_artists[name] = {
                    "fingerprint": fingerprints.get(name, ""),
                    "candidates": artist_candidates,
                    "artist_id": artist_id or "",
                    "catalog_ids": list(catalog_ids or []),
                }
            # Add the albums before the progress tick so a hit lands the live
            # preview the same moment the running total moves.
            hit = ({"artist": artist_name or name, "albums": shown}
                   if shown else None)
            job.push_progress("Scanning library", done, n, artist_name or name,
                              found=total, hit=hit, unit="artist")
            if shown:
                tail = "with Gap Fill candidates" if partial_only else "to fill"
                log.info(f"  {artist_name} - {plural(shown, 'album')} {tail}")
            since_save += 1
            if since_save >= _CHECKPOINT_EVERY:
                since_save = 0
                scan_checkpoint.save(
                    kind, scanned, job.candidates, baseline_seen, state_artists)
    # Reached here only without an AuthLost/outage abort (that re-raises out
    # above, leaving the checkpoint for resume and not seeding the baseline).
    flush_resolve_cache()
    baseline_save_failed = False
    scan_state_save_failed = False
    downsample_save_failed = False
    upgrade_save_failed = False
    catalog_complete = False
    if job.cancel_requested:
        # Deliberate stop, discard this kind's progress so it isn't auto-resumed.
        if not scan_checkpoint.clear(kind):
            job.push_line(
                "The stopped scan checkpoint could not be cleared. It will be "
                "ignored unless its artist proofs still match."
            )
        if not partial_only:
            generation_state.finish_attempt(attempt_id, "cancelled")
    else:
        # Only a reached-all-artists crawl stamps "last scanned" or seeds the
        # new-release baseline.
        catalog_complete = (
            len(state_artists) == len(artists)
            and len(scanned) == len(artists)
            and scan_errors == 0
        )
        publication = None
        if catalog_complete and kind == "missing":
            publication = generation_state.commit_catalog_generation(
                attempt_id,
                limited=job.candidate_cap_hit,
                expected_revision=scan_started_revision,
            )
            scan_state_save_failed = publication is None
            if publication is None:
                generation_state.finish_attempt(
                    attempt_id,
                    "incomplete",
                    "Saved Library state changed while the crawl was running.",
                )
        elif catalog_complete:
            partial_revision = generation_state.reserve_revision()
            if partial_revision is not None:
                saved_partial = library_scan_state.save_kind(
                    kind,
                    artists=state_artists,
                    complete=True,
                    quality_sig=quality_sig,
                    revision=partial_revision,
                )
                scan_state_save_failed = saved_partial is None
            else:
                scan_state_save_failed = True
        else:
            if not partial_only:
                generation_state.finish_attempt(
                    attempt_id,
                    "incomplete",
                    "One or more artists could not be checked.",
                )
        if publication is not None:
            scan_generation = int(publication["generation"])
            scan_revision = generation_state.reserve_revision()
            scan_saved = (
                scan_revision is not None
                and library_scan_state.save_kind(
                    kind,
                    artists=state_artists,
                    complete=True,
                    quality_sig=quality_sig,
                    generation=scan_generation,
                    revision=scan_revision,
                    limited=job.candidate_cap_hit,
                )
                is not None
            )
            scan_state_save_failed = not scan_saved
            if scan_saved:
                job.execute_args = {
                    **(job.execute_args or {}),
                    "_library_review_generation": scan_generation,
                }
            else:
                generation_state.abort_catalog_generation(
                    publication,
                    "The completed Library crawl could not publish its saved view.",
                )
        # Publish Upgrade/Downsample from their OWN completeness, gated only
        # on a complete catalog crawl, NOT on library_complete.
        if publication is not None and not scan_state_save_failed:
            scan_generation = int(publication["generation"])
            if downsample_refresh.complete:
                downsample_save_failed = not downsample_state.save(
                    downsample_refresh,
                    preserve_concurrent=True,
                    refresh_started_at=downsample_refresh_started_at,
                    refresh_started_revision=scan_started_revision,
                    generation=scan_generation,
                )
                if not downsample_save_failed:
                    _sync_surface_badge("downsample")
            if upgrade_refresh is not None and upgrade_refresh.complete:
                upgrade_save_failed = not upgrade_state.save(
                    upgrade_refresh,
                    preserve_concurrent=True,
                    refresh_started_at=upgrade_refresh_started_at,
                    refresh_started_revision=scan_started_revision,
                    generation=scan_generation,
                )
                if not upgrade_save_failed:
                    _sync_surface_badge("upgrade")
            elif upgrade_refresh is None:
                generation_state.mark_output_status(
                    "upgrade",
                    "disabled",
                    generation=scan_generation,
                    reason="Upgrade scanning is disabled.",
                )
        if publication is not None and not scan_state_save_failed:
            scan_generation = int(publication["generation"])
            if not scan_state_save_failed:
                _record_last_scan()
            _flag_new_since_last_scan(job, kind)
            # The crawl reached every artist cleanly, establish the new-release
            # baseline from the catalog snapshot (only the first time; the daily
            # check keeps it fresh after), and clear this kind's checkpoint.
            baseline_save_failed = not new_releases_mod.seed_baseline(
                baseline_seen,
                generation=scan_generation,
            )
            if not scan_checkpoint.clear(kind):
                job.push_line(
                    "The completed scan checkpoint could not be cleared. Its "
                    "saved generation is complete, so it will not be treated "
                    "as newer results."
                )
        elif catalog_complete and scan_state_save_failed:
            # Keep a complete resumable copy when the main review could not be
            # saved.
            scan_checkpoint.save(
                kind, scanned, job.candidates, baseline_seen, state_artists)
        elif catalog_complete and partial_only:
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
        _record_unchecked_artists(job, unchecked)
        job.summary += (f" {plural(unchecked, 'artist')} couldn't be checked; "
                        "scan again to resume from where it left off.")
    stale_tabs = [
        label for label, refresh, save_failed in (
            ("Upgrade", upgrade_refresh, upgrade_save_failed),
            ("Downsample", downsample_refresh, downsample_save_failed),
        )
        if refresh is not None and not (
            catalog_complete
            and refresh.complete
            and not scan_state_save_failed
            and not save_failed
        )
    ]
    if not job.cancel_requested and stale_tabs:
        job.summary += (
            f" {' and '.join(stale_tabs)} results were not updated because "
            "this scan did not finish cleanly."
        )
    if baseline_save_failed:
        job.summary += (" The New Releases baseline couldn't be saved; "
                        "the next complete scan will try again.")
    if scan_state_save_failed:
        job.summary += (" The saved scan state couldn't be written; scan again "
                        "before restarting the app.")
    # The crawl is stamped complete before its saved views are written, so a
    # view that never landed left the attempt reading "complete" and the page
    # showing a clean review. Correct the record with what is missing, which is
    # what puts the warning on Library at all.
    unsaved = list(stale_tabs)
    if baseline_save_failed:
        unsaved.append("New Releases")
    if (unsaved and attempt_id is not None and publication is not None
            and not scan_state_save_failed and not job.cancel_requested):
        named = (" and ".join(unsaved) if len(unsaved) < 3
                 else ", ".join(unsaved[:-1]) + " and " + unsaved[-1])
        generation_state.finish_attempt(
            attempt_id, "incomplete", f"{named} results were not updated.")
    log.info(job.summary)


def _parked_new_release_review(exclude=None):
    """The newest new-release review still waiting to be worked through, if any.
    ``exclude`` skips the check currently running, which is not parked yet."""
    parked = None
    for j in job_mgr.registry.awaiting_review():
        if getattr(j, "execute_kind", "") != "new_releases":
            continue
        if exclude is not None and j.id == exclude.id:
            continue
        if parked is None or (j.created_at or 0) > (parked.created_at or 0):
            parked = j
    return parked


def _append_to_parked_new_release_review(job):
    """Fold this check's finds into the new-release list already waiting.

    A second list beside the first would split one person's picks across two
    pages and leave the ticks and dismissals they had already made stranded on
    the older one. Folding keeps every earlier choice: a row the user dismissed
    while this ran is checked against a fresh hidden snapshot and not brought
    back, and a row already in the list keeps its tick. This job then finishes
    as a plain result pointing at the list it fed, never as a review of its own.
    Returns True when the finds were folded."""
    parked = _parked_new_release_review(exclude=job)
    if parked is None:
        return False
    added = refold_into_living_review(
        list(job.candidates), execute_kind="new_releases", ticked=False)
    if added is None or added is False:
        return False
    with job._lock:
        job.candidates = []
    waiting = len(parked.candidates)
    if added:
        # Through persist_review_mutation, not a bare assignment: the fold was
        # already written with the old summary, so a summary left in memory
        # only would come back after a restart claiming the count the review
        # had BEFORE the fold. A save that fails restores the old text rather
        # than leaving disk and screen disagreeing.
        restated = (f"{plural(waiting, 'new release')} waiting to review, "
                    f"{added} of them from the latest check.")
        job_persistence.persist_review_mutation(
            parked, lambda: setattr(parked, "summary", restated))
        job.summary += (
            " Added to the new-release review already waiting, "
            f"now {plural(waiting, 'new release')}.")
    else:
        job.summary += " They were already in the review waiting for you."
    parked.notify_review_changed()
    job.status = job_mgr.JobStatus.DONE
    return True


def scan_new_releases(job, token):
    """Surface albums that appeared in library artists' Qobuz catalogs since the
    last check and that the user doesn't own or hasn't hidden, flagged as new
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
    # the old baseline is missing everything past the previous cap, a plain
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
    # Folders Qobuz has no artist for at all. Kept apart from failed_count:
    # they are a settled answer, so they must not hold the baseline back or
    # push the user towards a retry that returns the same result.
    unresolved_names = []
    n = len(artists)
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # This run's reached artists; merged over the prior baseline at the end (so a
    # run where some/all artists errored can't wipe their baselines and re-surface
    # everything, only artists actually reached get their snapshot refreshed).
    current_seen = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="newrel",
                            **job_mgr.pool_initializer_kwargs()) as ex:
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
            elif getattr(result, "unresolved", False):
                unresolved_names.append(futures[fut].name)
            else:
                # A short or failed catalogue fetch is unchecked, not a clean
                # zero-release result.
                failed_count += 1
            for gap in result.new_gaps:
                # Leave new releases UN-ticked, like the library gap list: a
                # review is for picking, and one tap must never queue the whole
                # list of rips. The "new" badge still flags them for the eye.
                _add_gap_candidate(
                    job,
                    gap,
                    result.artist_name,
                    selected=False,
                    is_new=True,
                    artist_key=futures[fut].name,
                )
                total += 1
            hit = ({"artist": result.artist_name, "albums": len(result.new_gaps)}
                   if result.new_gaps else None)
            job.push_progress("Checking for new releases", done, n,
                              result.artist_name or futures[fut].name,
                              found=total, hit=hit, unit="artist")
            if result.new_gaps:
                log.info(f"  {result.artist_name} - "
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
    if failed_count:
        _record_unchecked_artists(job, failed_count)
    # Not logged on its own: every summary below carries it, and the summary
    # is the last line of the log.
    skipped_note = new_releases_mod.unresolved_note(unresolved_names)
    if job.cancel_requested:
        # A cancelled crawl only reached a fraction of the artists, so it can't
        # claim "No new releases" or "First check recorded" definitively.
        job.summary = ("Stopped early. Partial check, "
                       f"{plural(total, 'new release')} found so far.")
        if failed_count:
            job.summary += (
                f" {plural(failed_count, 'artist')} couldn't be checked "
                "before the stop."
            )
        if skipped_note:
            job.summary += f" {skipped_note}"
        log.info(job.summary)
        return

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
        job.summary = "No new releases since your last check."
    if skipped_note:
        job.summary += f" {skipped_note}"
    if not total and (failed_count or saved is False):
        # With no reviewable finds, submit_scan would otherwise replace this
        # explicitly incomplete result with a green Done state. Keep partial
        # success reviewable when it found albums, but never present an
        # unchecked or unsaved zero as a completed zero. job.summary above
        # already names the reason and the remedy; a separate job.error would
        # only restate it, and the FAILED status carries its own marker.
        _mark_job_failed(job)
    elif total:
        # An earlier check's list may still be parked and half-worked. Join it
        # rather than parking a rival list beside it.
        _append_to_parked_new_release_review(job)
    # Not logged here: job.summary already renders on the job page, and the
    # activity log below it would otherwise repeat the exact same sentence.


# ── Execute ───────────────────────────────────────────────────────────────────

def _note_staging_wait(job, phase, current, total):
    """If a long staging-lock holder (a library-wide Lyrics scan) owns the mutex
    right now, show that this job is waiting behind it. Without this the album
    sits on RUNNING with no visible reason until the holder releases the lock;
    the note is replaced by real progress the moment the lock is acquired."""
    holder = job_mgr.staging_holder()
    if holder:
        job.push_progress(phase, current, total,
                          f"waiting for {holder} to finish…", unit="album")


def execute_albums(job, chosen, token):
    """Download each selected album via the normal process_album path."""
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
    retryable_tracks = 0
    lossy_only_tracks = 0
    attention_counts = {
        "backup": 0,
        "quality": 0,
        "processing": 0,
        "partial": 0,
    }
    failed = 0
    # Failed at the fetch step specifically, before any of process_album's own
    # attention reasons could apply. Tracked apart so the summary can give a
    # plain reason instead of leaving it to the raw exception in the log.
    fetch_failed = 0
    skipped = 0
    processed = 0
    # Picks that didn't land (never fetched, errored, or came back empty).
    failed_cands = []
    cancelled_cand = None
    # Fold-back recovery is kind-scoped: this function executes both library
    # and new-release batches, and their picks must never cross into each
    # other's review, a library pick returns to the Library review, a
    # new-release pick to the New Releases review (it stays there until
    # downloaded or dismissed).
    is_library_run = getattr(job, "execute_kind", "") == "library"
    is_nr_run = getattr(job, "execute_kind", "") == "new_releases"
    review_save_failed = False
    retry_save_failed = False

    def _remember_review_save(saved, *, retry=True):
        nonlocal review_save_failed, retry_save_failed
        if saved is not False or review_save_failed:
            return
        review_save_failed = True
        retry_save_failed = retry
        if retry:
            job.push_line(
                "Retry choices could not be saved back to their review. "
                "Check the data folder, then refresh before retrying."
            )
        else:
            job.push_line(
                "The finished Library review could not be saved. Check the "
                "data folder, then refresh before continuing."
            )

    def _apply_non_track_attention():
        messages = []
        if attention_counts["backup"]:
            messages.append(
                f"{plural(attention_counts['backup'], 'album')} "
                "retained a safety backup because its replacement or recovery "
                "could not be verified. Review it under Settings > Diagnostics "
                "before downloading that album again."
            )
            job.attention = "backup"
        if attention_counts["quality"]:
            messages.append(
                f"{plural(attention_counts['quality'], 'album')} remained "
                "below the target quality after its automatic retry."
            )
            if not job.attention:
                job.attention = "quality"
        if attention_counts["processing"]:
            messages.append(
                f"{plural(attention_counts['processing'], 'imported album')} "
                "has post-download work that did not finish cleanly; see the "
                "job log."
            )
            if not job.attention:
                job.attention = "processing"
        if attention_counts["partial"]:
            messages.append(
                f"{plural(attention_counts['partial'], 'imported album')} "
                "reported unfinished work; see the job log."
            )
            if not job.attention:
                job.attention = "partial"
        if messages:
            job.error = " ".join(
                part for part in (job.error, *messages) if part
            )

    def _fold_back_unfinished():
        # Token death / outage mid-batch: once something has imported, this
        # job is headed for FAILED (approve's no-harm re-park only covers the
        # nothing-landed case), fold the picks this run didn't finish back
        # into the living review, ticked, so they come back to retry like any
        # other early exit.
        if not job._imported_any:
            return
        leftovers = failed_cands + [cand] + chosen[processed:]
        if is_library_run:
            _remember_review_save(_return_library_picks(leftovers))
        elif is_nr_run:
            _remember_review_save(_return_new_release_picks(leftovers))
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_id = cand["payload"].get("album_id")
        label = f"[{i}/{len(chosen)}] {cand.get('artist','')} - {cand['title']}"
        log.info(label)
        job._progress_scope = (i - 1, len(chosen), "album")
        job.push_progress("Downloading albums", i - 1, len(chosen),
                          f"{cand.get('artist','')} - {cand['title']}", unit="album")
        try:
            full = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            _fold_back_unfinished()
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            fetch_failed += 1
            failed_cands.append(cand)
            continue
        _note_staging_wait(job, "Downloading albums", i - 1, len(chosen))
        try:
            with job_mgr.staging_lock():
                if candidate_premise.expected_kind(cand) == "missing":
                    premise = candidate_premise.validate_container(cand)
                    # Earlier albums in this same batch may have changed the
                    # artist tree. That is safe only while this exact edition
                    # still has no local folder of its own.
                    clear_scan_caches()
                    if find_album_dir_filesystem(full) is not None:
                        raise CandidateStale(
                            "This missing album appeared locally after the "
                            "review was built. Refresh the review; nothing "
                            "was changed."
                        )
                else:
                    premise = candidate_premise.validate(cand)
                result = process_mode.process_album(full, args, allow_force=False,
                                       already_confirmed=True, token=token,
                                       expected_album_receipt=(
                                           premise["receipt"]
                                           if premise["kind"] == "gap-fill"
                                           else None
                                       ),
                                       expected_gap_fill_receipts=(
                                           cand["payload"].get(
                                               "_gap_fill_receipts")
                                           if premise["kind"] == "gap-fill"
                                           else None
                                       ))
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
            # so the cancel fold-back below would drop its pick, remember it.
            cancelled_cand = cand
        attention_kind = download_attention_kind(result or {})
        if attention_kind == "backup" and not (result or {}).get("imported"):
            # Keep unresolved backups out of the retry review. Diagnostics
            # owns the next decision.
            attention_counts["backup"] += 1
            job._imported_any = True
        elif result and result.get("imported") and result.get("n_ok", 0) > 0:
            job._imported_any = True
            _refresh_after_local_album_change(
                full,
                result,
                fallback_artist=cand.get("artist"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
            # The album is on disk now, any OTHER parked library review still
            # offering it is stale.
            prune_library_review_candidates(full)
            # A partial (some tracks landed, some failed) isn't a full
            # download, count it apart so the summary doesn't claim it
            # finished, and surface the remainder NOW (after the prune, which
            # would otherwise drop the fresh candidate as a same-id stale one)
            # instead of leaving it invisible until the next manual refresh.
            retryable, lossy_only = incomplete_track_counts(result)
            if attention_kind == "backup":
                attention_counts["backup"] += 1
            elif retryable or lossy_only:
                partial += 1
                retryable_tracks += retryable
                lossy_only_tracks += lossy_only
                if is_nr_run and retryable:
                    _remember_review_save(_return_new_release_picks([cand]))
                elif is_library_run and retryable:
                    _remember_review_save(_fold_partial_gap_fill(
                        full, cand.get("artist") or "", retryable))
            elif attention_kind:
                attention_counts[attention_kind] += 1
                if attention_kind == "quality":
                    job_mgr.record_quality_shortfall(
                        job, result.get("quality_verdict")
                    )
            else:
                ok += 1
        elif result and result.get("result") in _benign:
            if result.get("result") != "cancelled":
                skipped += 1
        else:
            failed += 1
            failed_cands.append(cand)
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if job.cancel_requested:
        # Nothing this run leaves behind is lost: the picks it never started
        # AND the ones that failed along the way fold back into the living
        # review, ticked, so a cancel mid-batch doesn't strand them in this
        # dead job. When the whole review was ticked at approval there is no
        # living review to fold into, so park a fresh one, the finish path
        # and the new-release side both already do that.
        unrun = chosen[processed:]
        if cancelled_cand is not None:
            unrun = [cancelled_cand] + unrun
        leftovers = failed_cands + unrun
        if leftovers and is_library_run:
            _remember_review_save(_return_library_picks(leftovers))
        elif leftovers and is_nr_run:
            _remember_review_save(_return_new_release_picks(leftovers))
        interrupted = 1 if cancelled_cand is not None else 0
        not_started = len(chosen) - processed
        parts = [f"{plural(ok, 'album')} downloaded"]
        if partial:
            parts.append(f"{plural(partial, 'album')} partly downloaded")
        attention_total = sum(attention_counts.values())
        if attention_total:
            parts.append(f"{plural(attention_total, 'album')} needs attention")
        if failed:
            parts.append(f"{plural(failed, 'album')} failed")
        if interrupted:
            parts.append(f"{plural(interrupted, 'album')} interrupted")
        if skipped:
            parts.append(f"{plural(skipped, 'album')} skipped")
        parts.append(f"{plural(not_started, 'album')} not started")
        job.summary = "Stopped early. " + ", ".join(parts) + "."
        retry_count = failed + interrupted + not_started
        destination = "Library" if is_library_run else "New Releases"
        if retry_count and (is_library_run or is_nr_run):
            if retry_save_failed:
                job.summary += (
                    f" {plural(retry_count, 'retry choice')} could not be "
                    f"saved back to {destination}."
                )
            else:
                job.summary += (
                    f" {plural(retry_count, 'retry choice')} returned to "
                    f"{destination}, selected for retry."
                )
        if retry_save_failed:
            job.error = (
                "Retry choices could not be saved. Check the data folder, "
                "then refresh before retrying."
            )
            job.attention = job.attention or "review"
        _apply_non_track_attention()
        log.info(job.summary)
        return
    if ok:
        parts = [f"{ok}/{plural(len(chosen), 'album')} downloaded and imported"]
    elif partial:
        parts = []
    elif sum(attention_counts.values()):
        parts = []
    else:
        parts = ["No albums downloaded or imported"]
    if partial:
        parts.append(
            f"{plural(partial, 'album')} only partly downloaded "
            "(some tracks are missing)"
        )
    if attention_counts["backup"]:
        parts.append(
            f"{plural(attention_counts['backup'], 'album')} retained "
            "a safety backup for review"
        )
    if attention_counts["quality"]:
        parts.append(
            f"{plural(attention_counts['quality'], 'album')} finished below "
            "target quality"
        )
    if attention_counts["processing"]:
        parts.append(
            f"{plural(attention_counts['processing'], 'imported album')} needs "
            "post-download attention"
        )
    if attention_counts["partial"]:
        parts.append(
            f"{plural(attention_counts['partial'], 'imported album')} reported "
            "unfinished work"
        )
    has_issues = bool(partial or failed or sum(attention_counts.values()))
    job.summary = ("" if has_issues else "Finished. ") + ", ".join(parts) + "."
    log.info(job.summary)
    if partial:
        log.info(f"  {plural(partial, 'album')} downloaded only partly "
                 f"(some tracks are missing); see the log.")
        job.attention = "partial" if retryable_tracks else "lossy"
        messages = []
        if retryable_tracks:
            destination = "New Releases" if is_nr_run else "Library Gap Fill"
            if retry_save_failed:
                messages.append(
                    f"{plural(retryable_tracks, 'track')} could not be saved "
                    f"to {destination}."
                )
            else:
                messages.append(
                    f"{plural(retryable_tracks, 'track')} can be retried from "
                    f"{destination}."
                )
        if lossy_only_tracks:
            messages.append(
                f"{plural(lossy_only_tracks, 'track')} can only be found "
                "lossy on Qobuz and needs another source."
            )
        job.error = " ".join(messages)
    _apply_non_track_attention()
    if has_issues:
        job.status = job_mgr.JobStatus.FAILED
    # A whole-review download (every candidate ticked, nothing re-parked at
    # approval) consumed the entire living review.
    if getattr(job, "_consumed_whole_review", False):
        retry_parked = True
        if failed_cands:
            retry_parked = _park_library_failures(failed_cands) is not False
            _remember_review_save(retry_parked)
        if retry_parked:
            _remember_review_save(library_scan_state.mark_review_retired(
                reason="worked_through",
                generation=(job.execute_args or {}).get(
                    "_library_review_generation"
                ),
            ), retry=False)
    elif failed_cands and is_library_run:
        # Partial approve: the unticked picks stayed behind as a living split-
        # off review.
        _remember_review_save(_return_library_picks(failed_cands))
    elif failed_cands and is_nr_run:
        # A new release that failed to download wasn't downloaded, it goes
        # back to the New Releases review rather than being consumed (the
        # baseline already recorded it, so nothing else would re-offer it).
        _remember_review_save(_return_new_release_picks(failed_cands))
    if failed:
        destination = "New Releases" if is_nr_run else "Library"
        if retry_save_failed:
            outcome = f"They could not be saved back to {destination}."
        else:
            pronoun = "It is" if failed == 1 else "They are"
            outcome = f"{pronoun} selected in {destination} for retry."
        # A fetch failure has no useful detail beyond "Qobuz didn't answer for
        # it"; the raw exception (endpoint name, ids) stays in the activity
        # log for anyone who needs it, not in the sentence the user reads.
        if fetch_failed == failed:
            reason = (" Qobuz couldn't be reached for it."
                      if failed == 1 else
                      " Qobuz couldn't be reached for them.")
        elif fetch_failed:
            reason = f" Qobuz couldn't be reached for {fetch_failed} of them."
        else:
            reason = ""
        failure_message = (
            f"{failed} of {plural(len(chosen), 'album')} didn't finish."
            f"{reason} {outcome}"
        )
        job.error = " ".join(part for part in (job.error, failure_message) if part)
    if review_save_failed:
        warning = (
            "Retry choices could not be saved. Check the data folder, then "
            "refresh before retrying."
            if retry_save_failed else
            "The finished Library review could not be saved. Check the data "
            "folder, then refresh before continuing."
        )
        job.error = " ".join(part for part in (job.error, warning) if part)
        job.attention = job.attention or "review"


# ── Upgrade flow ──────────────────────────────────────────────────────────────

def scan_upgrades(job, token):
    """Scan the library for albums Qobuz can serve at higher quality."""
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
    capped = quality_decision.load_capped()
    # Upgrades the user dismissed ("I'm happy with my copy"), independent of
    # the auto-`capped` memory and of the missing-album hides.
    hidden = hidden_mod.load()
    log.info(f"Scanning {plural(len(artists), 'artist')} for quality upgrades")
    total = 0
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
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
                    current_hidden,
                    year=(spec.get("payload") or {}).get("year")):
                continue
            # Unticked by default, like the gap scan, one click shouldn't
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
            log.info(f"  {name} - {plural(added, 'album')} to upgrade")

    refresh = upgrade_state.refresh_for_artists(
        artists,
        token=token,
        args=args,
        capped=capped,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        on_artist=_on_artist,
        workers=workers,
        pool_kwargs=job_mgr.pool_initializer_kwargs(),
    )
    if not job.cancel_requested and refresh.complete:
        _sync_surface_badge("upgrade")
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
                       "Every album is already at the best quality Qobuz offers.")
    log.info(job.summary)


def execute_upgrades(job, chosen, token):
    """Re-rip the present tracks of each chosen album at higher quality."""
    effective = settings_store.current()
    current_quality_signature = upgrade_state.quality_signature(
        effective.get("STREAMRIP_QUALITY"),
        effective.get("PREFER_HIRES"),
    )
    expected_quality_signature = (job.execute_args or {}).get(
        "quality_signature"
    )
    if expected_quality_signature != current_quality_signature:
        job.status = job_mgr.JobStatus.FAILED
        job.summary = "Upgrade stopped before any albums were changed."
        job.error = (
            "Download quality changed after this review was built. Run a "
            "Library refresh before trying Upgrade again."
        )
        log.info(job.summary)
        log.info(job.error)
        return

    clear_scan_caches()
    args = build_args()
    # Explicit upgrade: enable the replace path for this run only, and turn
    # off per-album consolidation prompts (the CLI upgrade walk does the same).
    args.auto_upgrade = True
    args.consolidate = False
    # Outcomes that aren't a failure: the album just didn't need (or couldn't
    # safely take) an upgrade.
    _skip = BENIGN_UPGRADE_RESULTS
    ok = 0
    kept = 0
    catalogue_failed = 0
    quality_attention = 0
    processing_attention = 0
    failed = 0
    failed_cands = []
    processed = 0
    current_interrupted = False

    def return_unfinished(current_index):
        if not job._imported_any:
            return True
        leftovers = failed_cands + chosen[current_index:]
        saved = _return_qobuz_review_picks(
            leftovers,
            "upgrade",
            execute_args=job.execute_args,
        )
        job.summary = (
            f"Qobuz stopped after {plural(ok, 'album')} upgraded. "
            f"{plural(len(leftovers), 'album')} selected for retry."
        )
        if saved is False:
            job.error = (
                "Retry choices could not be saved. Check the data folder, "
                "then refresh Upgrade before retrying."
            )
        return saved

    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_id = cand["payload"].get("album_id")
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist','')} - "
                 f"{cand.get('title') or '?'}")
        job._progress_scope = (i - 1, len(chosen), "album")
        job.push_progress("Upgrading albums", i - 1, len(chosen),
                          f"{cand.get('artist','')} - {cand.get('title') or '?'}", unit="album")
        try:
            album = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            return_unfinished(i - 1)
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            failed_cands.append(cand)
            continue
        if not album:
            log.info(f"  album {album_id} is no longer on Qobuz; skipping.")
            failed += 1
            failed_cands.append(cand)
            continue
        _note_staging_wait(job, "Upgrading albums", i - 1, len(chosen))
        try:
            with job_mgr.staging_lock():
                premise = candidate_premise.validate(cand)
                result = process_mode.process_album(album, args, allow_force=False,
                                       already_confirmed=True,
                                       upgrade_only=True, token=token,
                                       expected_album_receipt=premise["receipt"])
        except (AuthLost, QobuzUnavailable):
            return_unfinished(i - 1)
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            failed_cands.append(cand)
            continue
        _res = (result or {}).get("result")
        attention_kind = download_attention_kind(result or {})
        if result and (
            _res == "cancelled" or result.get("cancelled")
        ):
            current_interrupted = True
        elif result and result.get("catalogue_unverified"):
            catalogue_failed += 1
            job._imported_any = True
        elif result and attention_kind == "backup":
            # Imported, but the rebuilt folder couldn't be verified as
            # complete as the original, so the backup was kept.
            kept += 1
            job._imported_any = True
        elif result and result.get("imported") and _res not in (
                _skip | {"upgrade_aborted_backup_failed"}):
            ok += 1
            job._imported_any = True
            verdict = result.get("quality_verdict")
            if attention_kind == "quality":
                quality_attention += 1
                job_mgr.record_quality_shortfall(job, verdict)
                quality_decision.mark_album_capped(album.get("id"), album, {
                    "n_below": verdict["n_below"],
                    "n_at": 0,
                    "n_above": 0,
                })
                log.info(
                    f"  upgrade incomplete: {plural(verdict['n_below'], 'track')} "
                    "still below target after retry. Marked capped."
                )
            elif attention_kind == "processing":
                processing_attention += 1
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
            failed_cands.append(cand)
        if current_interrupted:
            break
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if job.cancel_requested or current_interrupted:
        first_unfinished = processed - 1 if current_interrupted else processed
        retry_count = len(failed_cands) + len(chosen[first_unfinished:])
        retry_saved = return_unfinished(first_unfinished)
        parts = [f"{ok} upgraded"]
        if current_interrupted:
            parts.append("1 interrupted")
        parts.append(f"{len(chosen) - processed} not started")
        job.summary = "Stopped early. " + ", ".join(parts) + "."
        if retry_count and retry_saved is not False:
            job.summary += (
                f" {plural(retry_count, 'retry choice')} returned to Upgrade, "
                "selected for retry."
            )
        if kept:
            job.summary += (
                f" {plural(kept, 'replacement')} retained a safety backup."
            )
        if catalogue_failed:
            job.summary += (
                f" {plural(catalogue_failed, 'replacement')} needs catalogue "
                "attention; backup retained."
            )
        if quality_attention:
            job.summary += (
                f" {plural(quality_attention, 'album')} finished below target "
                "quality."
            )
        if processing_attention:
            job.summary += (
                f" {plural(processing_attention, 'album')} needs "
                "post-download attention."
            )
        if kept or catalogue_failed:
            job.attention = "backup"
            job.error = (
                "A retained safety backup needs review under Settings > "
                "Diagnostics before that album is upgraded again."
            )
        elif quality_attention:
            job.attention = "quality"
            job.error = (
                "An album remained below the target quality after its "
                "automatic retry."
            )
        elif processing_attention:
            job.attention = "processing"
            job.error = "Post-download work needs attention; see the job log."
        if kept or catalogue_failed or quality_attention or processing_attention:
            _mark_job_failed(job)
        log.info(job.summary)
        return
    msg = f"Upgraded {ok}/{plural(len(chosen), 'album')}."
    if not any((failed, kept, catalogue_failed, quality_attention,
                processing_attention)):
        msg = "Finished. " + msg
    if kept:
        msg += (f" {kept} kept the original (upgrade couldn't be verified "
                f"complete; backup retained).")
    if catalogue_failed:
        msg += (f" {plural(catalogue_failed, 'replacement')} needs catalogue "
                "attention; backup retained.")
    if quality_attention:
        msg += (
            f" {plural(quality_attention, 'album')} finished below target "
            "quality."
        )
    if processing_attention:
        msg += (
            f" {plural(processing_attention, 'album')} needs post-download "
            "attention."
        )
    job.summary = msg
    log.info(msg)
    retry_saved = _return_qobuz_review_picks(
        failed_cands,
        "upgrade",
        execute_args=job.execute_args,
    )
    if any((failed, kept, catalogue_failed, quality_attention,
            processing_attention)):
        problems = []
        if kept:
            problems.append(
                f"{plural(kept, 'replacement')} retained a safety backup"
            )
        if catalogue_failed:
            problems.append(
                f"{plural(catalogue_failed, 'replacement')} couldn't be "
                "reconciled with the Beets catalogue"
            )
        if failed:
            problems.append(
                f"{plural(failed, 'album')} couldn't be upgraded"
            )
        if quality_attention:
            problems.append(
                f"{plural(quality_attention, 'album')} remained below target "
                "quality"
            )
        if processing_attention:
            problems.append(
                f"{plural(processing_attention, 'album')} has post-download "
                "work needing attention"
            )
        if kept or catalogue_failed:
            problems.append("review retained backups under Settings > Diagnostics")
            job.attention = "backup"
        elif quality_attention:
            job.attention = "quality"
        elif processing_attention:
            job.attention = "processing"
        job.error = "; ".join(problems) + "."
        if failed_cands:
            retry_note = (
                f" {plural(len(failed_cands), 'album')} selected for retry."
                if retry_saved is not False
                else " Retry choices could not be saved; refresh Upgrade."
            )
            job.error += retry_note
        _mark_job_failed(job)


# ── Downsample flow ─────────────────────────────────────────────────────────────

def _note_scan_wrap_up(job, done, total):
    """Say what a scan is doing once its walk is over.

    Saving the results and building the review take a moment more, and leaving
    the card on a full bar and "32 / 32 artists" made that read as a stall on
    finished work.
    """
    if done >= total and not job.cancel_requested:
        job.push_progress("Saving results")


def scan_downsamples(job):
    """Scan the library for FLACs stored above CD rate.

    Local only, the answer comes off disk, so unlike the upgrade scan there's
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
            _note_scan_wrap_up(job, done, n)
            return
        added = 0
        current_hidden = hidden_mod.load()
        for c in cands:
            if hidden_mod.is_hidden(
                    hidden_mod.SCOPE_DOWNSAMPLE, c.artist, c.title, current_hidden):
                continue
            # Unticked by default, a downsample is irreversible, so nothing is
            # shrunk without an explicit per-album tick.
            premise = candidate_premise.capture("downsample", c.album_dir)
            payload = {
                "album_dir": str(c.album_dir),
                "est_saving": c.est_saving,
            }
            if premise is not None:
                payload["_premise"] = premise
            job.add_candidate(
                kind="downsample",
                title=c.title,
                artist=name,
                detail=c.detail,
                payload=payload,
                selected=False,
            )
            total += 1
            added += 1
        hit = {"artist": name, "albums": added} if added else None
        job.push_progress("Scanning for hi-res files", done, n, name,
                          found=total, hit=hit, unit="artist")
        if added:
            log.info(f"  {name} - {plural(added, 'album')} above CD rate")
        _note_scan_wrap_up(job, done, n)

    refresh = downsample_state.refresh_for_artists(
        artists,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        on_artist=_on_artist,
    )
    unchecked = len(refresh.errors)
    if not job.cancel_requested and unchecked:
        _record_unchecked_artists(job, unchecked)
    if not job.cancel_requested and refresh.complete:
        _sync_surface_badge("downsample")
    if job.cancel_requested:
        log.info("Cancelled. Stopping scan.")
    elif unchecked:
        log.info(f"Scan incomplete. {plural(unchecked, 'artist')} couldn't be checked.")
    if not job.cancel_requested and refresh.complete:
        _flag_new_since_last_scan(job, "downsample")
    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    elif unchecked:
        job.summary = (
            f"{plural(total, 'album')} stored above CD rate."
            if total else
            "No downsample candidates found among the artists that could be checked."
        )
        job.summary += (
            f" {plural(unchecked, 'artist')} couldn't be checked; refresh "
            "candidates to retry."
        )
        if not total:
            job.error = "The Downsample scan did not complete."
            _mark_job_failed(job)
    else:
        job.summary = (f"{plural(total, 'album')} stored above CD rate."
                       + _cap_note(job)
                       if total else
                       "Every album is already at CD rate or lower.")
    log.info(job.summary)


def execute_downsamples(
        job, chosen, token=None, args=None, keep_originals=None):
    """Shrink the chosen albums' hi-res FLACs to CD rate, in place.

    Each file is decode-verified before it overwrites the original (in
    resample_one), so a bad encode can't destroy a master that has no
    re-download fallback.
    """
    if type(keep_originals) is not bool:
        keep_originals = cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"

    # Read through the module: ffmpeg is a property of the server, not of the
    # moment this file was imported.
    if not downsample_engine.HAVE_DOWNSAMPLE:
        job.error = "Downsampling isn't available on this server."
        job.summary = job.error
        _mark_job_failed(job)
        return
    shrunk = 0
    total_saved = 0
    interrupted_saved = 0
    total_errors = 0
    total_flush_warns = 0
    state_refresh_warnings = 0
    skipped_missing_details = 0
    skipped_missing_folders = 0
    skipped_unchanged = 0
    failed_albums = 0
    interrupted = 0
    processed = 0
    resampled_files = 0
    failed_files = []
    flushed_files = []
    stopped_early = False

    def skip_parts():
        parts = []
        if skipped_missing_details:
            parts.append(
                f"{plural(skipped_missing_details, 'album')} skipped "
                "(saved folder details missing)"
            )
        if skipped_missing_folders:
            parts.append(
                f"{plural(skipped_missing_folders, 'album')} skipped "
                "(no longer on disk)"
            )
        if skipped_unchanged:
            parts.append(
                f"{plural(skipped_unchanged, 'album')} skipped "
                "(nothing needed changing)"
            )
        return parts

    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            stopped_early = True
            break
        processed = i
        raw_album_dir = (cand.get("payload") or {}).get("album_dir")
        if not raw_album_dir:
            log.info("  skipped: saved candidate is missing its folder path")
            skipped_missing_details += 1
            continue
        album_dir = Path(raw_album_dir)
        title = cand.get("title") or album_dir.name
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist', '')} - {title}")
        job._progress_scope = (i - 1, len(chosen), "album")
        job.push_progress("Downsampling albums", i - 1, len(chosen),
                          f"{cand.get('artist', '')} - {title}", unit="album")
        if not album_dir.is_dir():
            log.info("  skipped: folder no longer exists")
            skipped_missing_folders += 1
            if album_dir.parent.is_dir():
                _refresh_downsample_artist_state(album_dir.parent)
            else:
                downsample_state.remove_artist(album_dir.parent.name)
                _sync_surface_badge("downsample")
            continue
        _note_staging_wait(job, "Downsampling albums", i - 1, len(chosen))
        try:
            with job_mgr.staging_lock():
                candidate_premise.validate(cand)
                if quality_decision.mark_local_album_capped(album_dir) is not True:
                    raise OSError(
                        "The local downsample decision could not be saved; "
                        "the album was left unchanged."
                    )
                if not upgrade_state.remove_album_dir(album_dir):
                    generation_state.mark_output_status(
                        "upgrade",
                        "stale",
                        reason=(
                            "An intentional Downsample could not update the "
                            "saved Upgrade view."
                        ),
                    )
                    state_refresh_warnings += 1
                res = downsample_engine.downsample_dir(album_dir, verbose=True,
                                     base_dir=album_dir, log=log.info,
                                     keep_originals=keep_originals,
                                     cancel_check=lambda: job.cancel_requested)
        except Exception as e:
            log.info(f"  failed: {e}")
            total_errors += 1
            failed_albums += 1
            continue
        if res.get("cancelled"):
            # A Stop mid-album leaves the rest of its tracks hi-res. The state
            # refresh below re-lists that album so the run can be finished later.
            interrupted += 1
            interrupted_saved += res.get("saved_bytes", 0)
            stopped_early = True
        elif res.get("resampled"):
            shrunk += 1
            total_saved += res.get("saved_bytes", 0)
        elif res.get("errors"):
            failed_albums += 1
        else:
            skipped_unchanged += 1
        _refresh_downsample_artist_state(album_dir.parent)
        total_errors += res.get("errors", 0)
        total_flush_warns += res.get("flush_warnings", 0)
        resampled_files += res.get("resampled", 0)
        album_label = f"{cand.get('artist', '')} - {title}".strip(" -")
        for entry in res.get("failed_files") or []:
            failed_files.append(dict(entry, album=album_label))
        for entry in res.get("flushed_files") or []:
            flushed_files.append(dict(entry, album=album_label))
    job._progress_scope = None
    if stopped_early:
        parts = [
            f"Downsampled {plural(shrunk, 'album')} "
            f"({format_size(total_saved)} smaller)"
        ]
        if failed_albums:
            parts.append(f"{plural(failed_albums, 'album')} failed")
        if interrupted:
            progress = "remaining files left unchanged"
            if interrupted_saved:
                progress = (
                    f"{format_size(interrupted_saved)} smaller so far; "
                    f"{progress}"
                )
            parts.append(
                f"{plural(interrupted, 'album')} interrupted "
                f"({progress})"
            )
        parts.extend(skip_parts())
        parts.append(f"{plural(len(chosen) - processed, 'album')} not started")
        job.summary = "Stopped early. " + ", ".join(parts) + "."
        job.summary += _kept_originals_note(resampled_files)
        log.info(job.summary)
        with job._lock:
            if job.status is job_mgr.JobStatus.RUNNING:
                job.status = job_mgr.JobStatus.CANCELED
        return
    # "Reclaimed" is a claim about free space. When the originals are kept, a
    # full copy of every one of them is written to the backup folder, so the
    # run costs MORE disk than it saves until that retention expires.
    #
    # A run that hit an error ends on the red Failed chip, so opening the
    # summary with "Finished" put a contradiction on one line. Lead with what
    # the run actually managed instead.
    went_wrong = bool(total_errors or total_flush_warns)
    if shrunk:
        summary = (f"{'Partly done' if went_wrong else 'Finished'}. "
                   f"Downsampled {plural(shrunk, 'album')}, "
                   f"{format_size(total_saved)} smaller."
                   + _kept_originals_note(resampled_files))
    else:
        summary = "Nothing was downsampled."
    for part in skip_parts():
        summary += f" {part}."
    if state_refresh_warnings:
        summary += (
            " The files were changed, but the saved Upgrade view needs a "
            "Library refresh."
        )
    job.summary = summary
    log.info(summary)
    if total_errors:
        detail = _named_files(failed_files, total_errors)
        reason = _lone_reason(failed_files, total_errors)
        job.error = (
            f"Couldn't downsample {detail}: {reason}" if reason else
            f"Couldn't downsample {detail}. "
            f"{'It was' if total_errors == 1 else 'They were'} left unchanged."
        )
    if total_flush_warns:
        # These files WERE rewritten, only the folder flush failed
        # afterwards, so the swap may not survive a power loss.
        detail = _named_files(flushed_files, total_flush_warns)
        reason = _lone_reason(flushed_files, total_flush_warns)
        note = (
            f"Resampled {detail} but couldn't flush "
            f"{'it' if total_flush_warns == 1 else 'them'} to disk, so the "
            "change may not survive a power cut. Check the drive."
            + (f" ({reason})" if reason else "")
        )
        job.error = f"{job.error} {note}" if job.error else note
    if went_wrong:
        _mark_job_failed(job)
    else:
        _close_completed_job(job)


def _named_files(entries, total, limit=3):
    """Name the files a downsample could not handle.

    A bare count plus "see the log" leaves the person no album to open and
    nothing to act on, so name each file with the album it sits in.
    """
    named = [e for e in entries if e.get("name")][:limit]
    if not named:
        return f"{plural(total, 'file')} (see the log)"
    parts = []
    for entry in named:
        album = entry.get("album")
        parts.append(f"{album}, {entry['name']}" if album else entry["name"])
    detail = "; ".join(parts)
    if total == 1:
        return detail
    if total > len(named):
        return f"{plural(total, 'file')}: {detail} and {total - len(named)} more"
    return f"{plural(total, 'file')}: {detail}"


def _lone_reason(entries, total):
    """The per-file reason, only when a single file carries the whole story."""
    if total != 1 or not entries:
        return ""
    return entries[0].get("reason") or ""


def _kept_originals_note(resampled_files):
    """The sentence that stops a downsample summary overstating what it freed.

    Originals are only stashed for files that were actually rewritten; a run
    that converted nothing keeps nothing, so it must not promise a restore
    that has nothing behind it.
    """
    if not resampled_files or cfg.DOWNSAMPLE_KEEP_ORIGINALS != "keep":
        return ""
    days = cfg.UPGRADE_BACKUP_RETENTION_DAYS
    return (f" Your hi-res originals are kept for {plural(days, 'day')}, so "
            "nothing is freed until they expire. Until then you can put them "
            "back from Settings.")

# ── Repair flow ───────────────────────────────────────────────────────────────

def _repair_damage_detail(truncated):
    """Say what the scan measured instead of asserting truncation.

    A file that will not decode is damaged and can be called that outright. A
    file that merely runs shorter than the recording its ISRC names has only
    been measured against a catalogue, so the card reports the gap and leaves
    the verdict to the person reading it.
    """
    broken = [t for t in truncated if t.get("reason")]
    short = [t for t in truncated if not t.get("reason")]
    parts = []
    if broken:
        parts.append(f"{plural(len(broken), 'track')} won't decode")
    if short:
        worst = max(float(t.get("qobuz_duration") or 0)
                    - float(t.get("file_length") or 0) for t in short)
        lead = "up to " if len(short) > 1 else ""
        parts.append(f"{plural(len(short), 'track')} {lead}"
                     f"{int(round(worst))}s shorter than the catalogue version")
    return ", ".join(parts) or plural(len(truncated), "damaged track")


def _repair_album_outcome(album_dir, name, token):
    """Scan one album into an outcome dict: counts, review-candidate specs, and
    any log lines to emit. AuthLost / QobuzUnavailable propagate (they stop the
    sweep); any other scan error is recorded as a failed album."""
    out = {"verified_ok": 0, "unverified": 0, "failed": 0, "specs": [],
           "warns": []}
    try:
        scan = repair_log.scan_dir_for_isrc_repairs(album_dir, token, deep=True)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception as e:
        out["warns"].append(f"    skipped {album_dir.name}: {e}")
        out["failed"] = 1
        return out
    out["verified_ok"] = scan["verified_ok"]
    out["unverified"] = scan.get("unverified", 0)
    truncated = scan["verified_truncated"]
    suspicious = [
        entry
        for bucket in ("isrc_no_match", "no_isrc_tag", "isrc_mismatch")
        for entry in scan.get(bucket, [])
        if entry.get("diagnostic")
    ]
    premise = None
    if truncated or suspicious:
        premise = candidate_premise.capture("repair", album_dir)
    if truncated:
        detail = _repair_damage_detail(truncated)
        payload = {"album_dir": str(album_dir), "artist_name": name,
                   "verified_truncated": truncated}
        if premise is not None:
            payload["_premise"] = premise
        out["specs"].append({
            "kind": "repair", "title": album_dir.name, "artist": name,
            "detail": detail,
            "payload": payload})
        # The sweep opens by promising that problems appear in the log, so each
        # flagged album has to say so as it is found. Without this the log runs
        # empty for hours and then a review appears from nowhere.
        out["warns"].append(f"  {name} - {album_dir.name}: {detail}")
    for entry in scan.get("isrc_mismatch", []):
        if entry.get("diagnostic"):
            # Damaged as well as mis-tagged, so it is picked up below as a
            # re-download candidate and reported there. Saying it here too
            # printed the same file twice in slightly different words.
            continue
        out["warns"].append(
            f"    ~ {album_dir.name} - {entry.get('local_title') or '?'}: "
            f"shorter than its ISRC match, but that match is "
            f"\"{entry.get('title') or '?'}\", a different recording. "
            "Left alone; check the file's ISRC tag if you think it is wrong.")
    # Damaged files that can't be matched to a Qobuz recording can't be
    # surgically refilled, so offer a whole-album re-download instead (the user
    # confirms it in review). Every bucket that can hold one carries them: no
    # ISRC at all, an ISRC Qobuz doesn't know, or an ISRC that names some other
    # song (where a single-track refill would fetch that other song).
    if suspicious:
        matched = find_qobuz_album_for_dir(album_dir, name, token)
        if matched and matched.get("id"):
            m_title = matched.get("title") or album_dir.name
            m_year = album_year(matched) or "?"
            detail = (f"{plural(len(suspicious), 'damaged file')} can't be "
                      f"verified by ID. Re-download the whole album fresh "
                      f"as “{m_title}” ({m_year})")
            payload = {"album_dir": str(album_dir), "artist_name": name,
                       "album_id": matched.get("id"),
                       "matched_title": m_title}
            if premise is not None:
                payload["_premise"] = {
                    **premise,
                    "kind": "repair-redownload",
                }
            out["specs"].append({
                "kind": "redownload", "title": album_dir.name, "artist": name,
                "detail": detail,
                "payload": payload})
            # The sweep promises every problem reaches the log. A re-download
            # candidate is a problem, and without this it produced a review
            # card and no line at all, so hours of scanning read as idle and
            # then a review appeared from nowhere.
            out["warns"].append(f"  {name} - {album_dir.name}: {detail}")
        else:
            for e in suspicious:
                # Name the file, not the recording its ISRC resolved to: when
                # the tag is wrong those are different songs and the user would
                # be sent looking for a track this album does not contain.
                out["warns"].append(
                    f"    ⚠ {album_dir.name} - "
                    f"{e.get('local_title') or e.get('title') or '?'}: "
                    f"{e['diagnostic']}; couldn't match this folder to a Qobuz "
                    "album to re-download. Check by hand.")
    return out


def _scan_repair_artist(artist_dir, token, job, beat=None):
    """Scan one artist's albums for damaged FLACs, runs on a pool worker.

    Returns ``(name, agg)``; ``agg`` carries per-artist counts and a list of
    review-candidate specs the caller adds on the single writer thread, so the
    candidate list and checkpoint stay single-writer (mirroring the library
    scan). Every album is decode-tested fresh each run so on-disk corruption is
    always caught; the per-track Qobuz lookups are what's cached, so a re-scan
    only re-reads files rather than re-crawling Qobuz. Bails between albums on
    cancel; AuthLost / QobuzUnavailable propagate so the caller can stop the
    sweep."""
    name = artist_dir.name
    proof_before = _capture_repair_artist_proof(artist_dir)
    if proof_before is None:
        raise OSError("artist files could not be sealed before Repair checked them")
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
    proof_after = _capture_repair_artist_proof(artist_dir)
    if proof_after is None or proof_after != proof_before:
        raise OSError("artist files changed while Repair was checking them")
    agg["proof"] = proof_after
    return name, agg


def _repair_item(artist, albums, flagged):
    """One consistent live-status line for the whole-library repair sweep.

    Every progress push during the sweep, the per-album heartbeat, the
    per-artist completion tick, and the failure tick, renders through this so
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
    and no future has completed, otherwise a long stretch with no completed
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


def _repair_scan_caveats(n_unverified, n_failed_albums, n_failed_artists):
    """Format incomplete Repair scan work without merging unlike failures."""
    parts = []
    if n_unverified:
        reason = (
            "the app couldn't read them or they changed mid-check, check "
            "file ownership and PUID"
            if shutil.which("flac") else "no flac tool"
        )
        parts.append(
            f"{plural(n_unverified, 'track')} couldn't be decode-checked "
            f"({reason})."
        )
    if n_failed_albums:
        parts.append(
            f"{plural(n_failed_albums, 'album')} couldn't be scanned; "
            "re-run to retry."
        )
    if n_failed_artists:
        parts.append(
            f"{plural(n_failed_artists, 'artist')} couldn't be scanned; "
            "re-run to retry."
        )
    return "" if not parts else " " + " ".join(parts)


def scan_repairs(job, token):
    """Scan every album for ISRC-verified truncated FLACs (fanned out across
    ARTIST_SCAN_WORKERS; see _scan_repair_artist for the per-artist work)."""
    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        _set_empty_library_summary(job)
        return
    # Resume only artists whose saved file receipt still matches. Older global
    # checkpoints are treated as hints to rescan.
    cp = scan_checkpoint.load("repair")
    checkpoint_artists = (
        cp.get("artists", {})
        if cp and cp.get("meta", {}).get("repair_checkpoint_version")
        == _REPAIR_CHECKPOINT_VERSION
        else {}
    )
    scanned = set()
    repair_bundles = {}
    total = 0
    n_verified = 0
    n_unverified = 0
    n_failed_albums = 0
    n_failed_artists = 0
    if cp:
        for artist_dir in artists:
            bundle = _validated_repair_checkpoint_bundle(
                artist_dir,
                checkpoint_artists.get(artist_dir.name),
            )
            if bundle is None:
                continue
            repair_bundles[artist_dir.name] = bundle
            scanned.add(artist_dir.name)
            counts = bundle["counts"]
            n_verified += counts["verified_ok"]
            n_unverified += counts["unverified"]
            n_failed_albums += counts["failed_albums"]
            for spec in bundle["candidates"]:
                _readd_candidate(job, spec)
                total += 1
        recheck = len(artists) - len(scanned)
        log.info(
            f"Resuming. Reusing {plural(len(scanned), 'unchanged artist')}; "
            f"checking {plural(recheck, 'changed or unproven artist')} again. "
            f"{plural(total, 'album')} flagged so far."
        )
    log.info(f"Scanning {plural(len(artists), 'artist')} for damaged files. "
             "Only problems are listed below; expect long quiet stretches. "
             "Slow on a big library.")
    todo = [ad for ad in artists if ad.name not in scanned]
    n = len(artists)
    done = len(scanned)
    since_save = 0
    checkpoint_warning = False

    def warn_checkpoint(message):
        nonlocal checkpoint_warning
        if checkpoint_warning:
            return
        checkpoint_warning = True
        log.info(message)
        if hasattr(job, "push_line"):
            job.push_line(message)

    def save_checkpoint():
        saved = scan_checkpoint.save(
            "repair",
            scanned,
            [],
            {},
            artists=repair_bundles,
            meta={
                "repair_checkpoint_version": _REPAIR_CHECKPOINT_VERSION,
            },
        )
        if saved is False:
            warn_checkpoint(
                "Repair progress could not be saved. Check the data folder; "
                "an interruption will recheck these artists."
            )
        return saved
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
    # thread so they stay single-writer, the same shape the library scan
    # uses.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repairscan",
                            **job_mgr.pool_initializer_kwargs()) as ex:
        futures = {ex.submit(_scan_repair_artist, ad, token, job, beat): ad
                   for ad in todo}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                stopped = (
                    f"Stopped early. {plural(total, 'album')} flagged so far."
                    if total else "Stopped before anything was flagged."
                )
                job.summary = stopped + _repair_scan_caveats(
                    n_unverified, n_failed_albums, n_failed_artists)
                log.info("Cancelled. Stopping scan.")
                if scan_checkpoint.clear("repair") is False:
                    warning = (
                        " Old resume data could not be cleared; check the data "
                        "folder before running Repair again."
                    )
                    job.summary += warning
                    warn_checkpoint(warning.strip())
                return
            done += 1
            try:
                name, agg = fut.result()
            except (AuthLost, QobuzUnavailable):
                # A lost token or an unreachable API isn't a per-artist hiccup,
                # stop the sweep rather than report a partial library as whole.
                # The checkpoint stays, so it resumes once auth/network is back.
                for f in futures:
                    f.cancel()
                if scanned:
                    save_checkpoint()
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                log.info(f"    skipped {futures[fut].name}: {e}")
                n_failed_artists += 1
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
            n_failed_albums += agg["failed"]
            for spec in agg["specs"]:
                # Unticked by default, like Upgrade and Downsample: Repair
                # deletes and refills files, so one click on a whole-library
                # sweep must not start replacing albums nobody looked at.
                job.add_candidate(selected=False, **spec)
                total += 1
            if not agg["failed"]:
                scanned.add(name)
                repair_bundles[name] = _repair_checkpoint_bundle(
                    name,
                    agg,
                    agg["proof"],
                )
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
                save_checkpoint()
    clear_failed = scan_checkpoint.clear("repair") is False
    # Honest summary: report what was actually decode-verified, and never
    # claim completeness the scan didn't earn.
    caveats = _repair_scan_caveats(
        n_unverified, n_failed_albums, n_failed_artists)
    if total:
        job.summary = (f"{plural(total, 'album')} flagged with damaged files. "
                       f"{plural(n_verified, 'track')} decode-verified clean."
                       + caveats)
    else:
        job.summary = (f"No damaged files found. "
                       f"{plural(n_verified, 'track')} decode-verified intact."
                       + caveats)
    if clear_failed:
        job.summary += (
            " Old resume data could not be cleared. Check the data folder "
            "before running Repair again."
        )
        warn_checkpoint(
            "Repair finished, but its old resume data could not be cleared. "
            "Check the data folder before running Repair again."
        )
    log.info(job.summary)


def _redownload_damaged_album(payload, token, *, recovery_checkpoint=None,
                              expected_album_receipt=None):
    """Re-fetch a whole album whose damaged file couldn't be ID-verified.

    The folder is moved aside first so beets imports a clean copy instead of
    colliding with the broken files (the --force path can't be used here: it
    needs an interactive deletion confirm the web has no way to answer). If
    the re-download doesn't complete, the original folder is moved back so the
    user is never left worse off.
    """
    log.info("  The damaged file can't be verified by its ID, so the whole "
             "album is being re-downloaded fresh from Qobuz.")
    full = get_album(payload["album_id"], token)
    album_dir = Path(payload["album_dir"])
    catalogue_snapshot = beets_mod.capture_beets_album_entries(album_dir) \
        if album_dir.exists() else None
    if album_dir.exists() and catalogue_snapshot is None:
        log.info("  Couldn't safely identify this album's Beets entries; "
                 "leaving Repair alone.")
        return {"imported": False, "n_ok": 0,
                "result": "catalogue_snapshot_failed"}
    expected_generation = getattr(token, "credential_generation", "")
    if expected_generation:
        token = api_client.authorize_qobuz_action(
            QobuzAccess.DOWNLOAD_ACTION,
            expected_generation=expected_generation,
        ).token
    backup = None
    if album_dir.exists():
        backup = (
            backup_mod.backup_album_dir(
                album_dir,
                expected_receipt=expected_album_receipt,
            )
            if expected_album_receipt is not None
            else backup_mod.backup_album_dir(album_dir)
        )
    if backup is not None and not backup.complete:
        process_mode._recover_incomplete_upgrade_backup(
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
                and not backup_mod.pin_unverified_upgrade_backup(backup, note)):
            backup_mod.warn_pin_failed(backup)

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
            if backup_mod.restore_upgrade_backup(backup, album_dir):
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
                "repair backup kept, recovery record and automatic restore "
                "did not complete")
            recovery, _ = checkpoint_recovery(
                "restore",
                "The recovery record could not be saved and automatic "
                "restoration did not complete.",
            )
            cause = OSError("Repair recovery record could not be saved")
            raise RepairRecoveryRequired(recovery, cause) from cause

    try:
        with job_mgr.staging_lock():
            result = process_mode.process_album(full, build_args(), allow_force=False,
                                   already_confirmed=True, token=token) or {}
    except Exception as exc:
        if backup:
            if backup_mod.restore_upgrade_backup(backup, album_dir):
                checkpoint_recovery(
                    "resolved",
                    "The original album was restored after the replacement "
                    "download stopped.",
                    retained=False,
                )
            else:
                pin_repair_recovery(
                    "repair backup kept, automatic restore after an error did "
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
        if imported_ok and process_mode._upgrade_replacement_verified(full, album_dir, backup):
            # Carry useful companions, then retire the original's backup when
            # every file it holds is verifiably superseded in the new album.
            carried = process_mode._carry_non_audio_from_backup(
                full, album_dir, backup)
            if carried is not None:
                replacement_path, replacement_receipt = carried
                if not beets_mod.retire_replaced_beets_entries(
                    catalogue_snapshot,
                    replacement_path,
                    process_mode._replacement_audio_paths(
                        replacement_path,
                        replacement_receipt,
                    ),
                ):
                    result["repair_unverified"] = True
                    pin_repair_recovery(
                        "repair backup kept; the replaced Beets entries "
                        "could not be retired safely")
                    log.info("  Re-download verified, but its replaced "
                             "Beets entries couldn't be reconciled safely; "
                             f"keeping the backup at {backup}.")
                    checkpoint_recovery(
                        "verification",
                        "The re-download verified, but the replaced Beets "
                        "entries could not be reconciled safely.",
                    )
                elif backup_mod.retire_verified_repair_backup(backup):
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
                        "repair backup kept, could not be proven redundant "
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
                    "repair backup kept, replacement not durable")
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
            # every track, a truncated or short result could be WORSE than
            # the damaged original it replaced.
            result["repair_unverified"] = True
            pin_repair_recovery(
                "repair backup kept, replacement could not be verified "
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
            if backup_mod.restore_upgrade_backup(backup, album_dir):
                checkpoint_recovery(
                    "resolved",
                    "The replacement did not complete, so the original album "
                    "was restored.",
                    retained=False,
                )
            else:
                pin_repair_recovery(
                    "repair backup kept, automatic restore did not complete")
                checkpoint_recovery(
                    "restore",
                    "The replacement did not complete and automatic "
                    "restoration was incomplete.",
                )
    return result


def _checkpoint_repair_recovery(job, recovery):
    """Attach one exact Repair carrier to the durable job record."""
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
    whose damage couldn't be ID-verified, depending on each candidate."""
    clear_scan_caches()
    args = build_args()
    fixed = 0
    unverified = 0
    failed = 0
    failed_cands = []
    interrupted = 0
    processed = 0
    stopped_early = False
    unfinished_start = None

    def return_unfinished(current_index, *, include_current=True):
        if (not job._imported_any
                and not getattr(job, "recoveries", None)):
            return True
        start = current_index if include_current else current_index + 1
        leftovers = failed_cands + chosen[start:]
        saved = _return_qobuz_review_picks(
            leftovers,
            "repair",
            execute_args=getattr(job, "execute_args", None),
        )
        recovery_count = len(getattr(job, "recoveries", []) or [])
        job.summary = (
            f"Qobuz stopped after {plural(fixed, 'album')} repaired. "
            f"{plural(len(leftovers), 'album')} selected for retry."
        )
        if recovery_count:
            job.summary += (
                f" {plural(recovery_count, 'backup')} kept for recovery."
            )
        if saved is False:
            job.error = (
                "Retry choices could not be saved. Check the data folder, "
                "then run a fresh Repair scan."
            )
        return saved

    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            stopped_early = True
            break
        processed = i
        p = cand["payload"]
        # Pin the progress card to album-level scope so the inner per-album
        # phases (download / import / downsample) read "album i / N" instead of
        # resetting it to 1 / 1, the card now reflects the whole batch.
        job._progress_scope = (i - 1, len(chosen), "album")
        job.push_progress("Repairing damaged albums", i - 1, len(chosen),
                          f"{p['artist_name']} - {cand['title']}", unit="album")
        log.info(f"[{i}/{len(chosen)}] {p['artist_name']} - {cand['title']}")
        _note_staging_wait(job, "Repairing damaged albums", i - 1, len(chosen))
        try:
            if cand.get("kind") == "redownload":
                premise = candidate_premise.validate(cand)
                # _redownload_damaged_album takes the staging lock itself.
                result = _redownload_damaged_album(
                    p,
                    token,
                    expected_album_receipt=premise["receipt"],
                    recovery_checkpoint=lambda recovery: (
                        _checkpoint_repair_recovery(job, recovery)
                    ),
                )
            else:
                with job_mgr.staging_lock():
                    candidate_premise.validate(cand)
                    result = repair.repair_album_dir(Path(p["album_dir"]),
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
                stopped_early = True
                unfinished_start = processed
                break
            if isinstance(exc.cause, (AuthLost, QobuzUnavailable, SystemExit)):
                return_unfinished(i - 1, include_current=False)
                raise exc.cause
            raise
        except (AuthLost, QobuzUnavailable):
            return_unfinished(i - 1)
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            failed_cands.append(cand)
            continue
        # Each chosen album was flagged as damaged, so anything that didn't
        # end up downloaded-and-imported is a real failure. A kept backup is
        # not one, a verified repair counts, and the backup is reported in
        # the summary's recovery tail.
        local_files_changed = bool(
            result
            and result.get("imported")
            and result.get("n_ok", 0) > 0
        )
        if local_files_changed:
            job._imported_any = True
        if result and (
            result.get("result") == "cancelled" or result.get("cancelled")
        ):
            interrupted += 1
            stopped_early = True
            unfinished_start = processed - 1
        elif (result and result.get("n_ok", 0) > 0 and result.get("imported")
                and result.get("n_fail", 0) == 0
                and result.get("repair_unverified")):
            # Its own outcome, neither repaired nor failed: the replacement
            # landed and the originals are held as a recovery backup, so
            # "repaired" would vouch for files nothing has proven complete,
            # and "couldn't be repaired" claimed the album was untouched when
            # it had just been replaced.
            unverified += 1
            job._imported_any = True
        elif (result and result.get("n_ok", 0) > 0 and result.get("imported")
                and result.get("n_fail", 0) == 0):
            fixed += 1
            job._imported_any = True
        else:
            failed += 1
            failed_cands.append(cand)
        if local_files_changed:
            _refresh_after_local_album_change(
                None,
                result,
                fallback_artist=p.get("artist_name"),
                token=token,
                args=args,
                upgrade=True,
                downsample=True,
            )
        if stopped_early:
            break
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    recovery_count = len(getattr(job, "recoveries", []) or [])
    if stopped_early:
        if unfinished_start is None:
            unfinished_start = processed
        retry_count = len(failed_cands) + len(chosen[unfinished_start:])
        retry_saved = return_unfinished(unfinished_start)
        parts = [f"{fixed} repaired"]
        if unverified:
            parts.append(f"{unverified} replaced but unproven")
        if interrupted:
            parts.append(f"{interrupted} interrupted")
        if recovery_count:
            parts.append(f"{recovery_count} kept for recovery")
        parts.append(f"{len(chosen) - processed} not started")
        job.summary = "Stopped early. " + ", ".join(parts) + "."
        if retry_count and retry_saved is not False:
            job.summary += (
                f" {plural(retry_count, 'retry choice')} returned to Repair, "
                "selected for retry."
            )
        log.info(job.summary)
        with job._lock:
            if job.status is job_mgr.JobStatus.RUNNING:
                job.status = job_mgr.JobStatus.CANCELED
        return
    kept = (f" {plural(recovery_count, 'backup')} kept for recovery."
            if recovery_count else "")
    unproven = (
        f" {plural(unverified, 'album')} "
        f"{'was' if unverified == 1 else 'were'} replaced but could not be "
        "proven complete, so your original files were kept."
        if unverified else ""
    )
    job.summary = (f"{'Finished. ' if not (failed or unverified) else ''}"
                   f"Repaired {fixed}/{plural(len(chosen), 'album')}."
                   f"{kept}{unproven}")
    log.info(job.summary)
    retry_saved = _return_qobuz_review_picks(
        failed_cands,
        "repair",
        execute_args=getattr(job, "execute_args", None),
    )
    if failed:
        job.error = (
            f"{failed} of {plural(len(chosen), 'album')} couldn't be repaired; "
            "see the log."
        )
        if failed_cands:
            job.error += (
                f" {plural(len(failed_cands), 'album')} selected for retry."
                if retry_saved is not False
                else " Retry choices could not be saved; run a fresh scan."
            )
        _mark_job_failed(job)
    else:
        _close_completed_job(job)


def run_lyric_retry(job):
    """Retry lyric fetching for tracks queued from a previous failed run."""
    paths = lyrics_mode.load_lyric_retry()
    if not paths:
        job.summary = "No tracks were queued for lyric retry."
        log.info(job.summary)
        _close_completed_job(job)
        return

    if not lyric_fetch.AVAILABLE:
        job.summary = ("The syncedlyrics library isn't installed; manifest "
                       "preserved for a later retry.")
        job.error = job.summary
        _mark_job_failed(job)
        log.info(job.summary)
        return

    existing = [Path(p) for p in paths if Path(p).exists()]
    dropped = len(paths) - len(existing)
    if dropped:
        log.info(f"{plural(dropped, 'queued path')} no longer on disk; skipping.")
    if not existing:
        if not lyrics_mode.save_lyric_retry([]):
            job.summary = (
                "All queued files are gone from disk, but the retry manifest "
                "couldn't be cleared and was left unchanged."
            )
            job.error = job.summary
            _mark_job_failed(job)
            log.info(job.summary)
            return
        job.summary = "All queued files are gone from disk; manifest cleared."
        log.info(job.summary)
        _close_completed_job(job)
        return

    log.info(f"Retrying lyrics on {plural(len(existing), 'track')} ...")
    # Hold the staging lock: fetch_for_paths rewrites library FLACs in place, so
    # it must not run concurrently with the scan-lane downsample/repair/upgrade
    # work that mutates the same files (the documented file-mutation mutex).
    try:
        with job_mgr.staging_lock():
            job_mgr.set_staging_holder("Lyrics retry")
            try:
                counts = lyric_fetch.fetch_for_paths(
                    existing, owned_root=cfg.MUSIC_ROOT, log=log,
                    providers=cfg.LYRICS_PROVIDERS or None,
                    lyrics_format=cfg.LYRICS_FORMAT,
                    state_path=cfg.LYRIC_FETCH_STATE_FILE,
                    should_stop=lambda: job.cancel_requested,
                )
            finally:
                job_mgr.set_staging_holder(None)
    except Exception as e:
        job.error = redaction.redact(
            f"Lyric retry failed: {e}; manifest preserved.")
        job.summary = "Lyric retry failed. Manifest preserved, will retry next time."
        _mark_job_failed(job)
        log.info(job.error)
        return

    refreshed = lyrics_mode._refresh_lyric_retry(existing)
    remaining = lyrics_mode.load_lyric_retry()
    attempted_paths = {str(path) for path in existing}
    attempted_remaining = sum(
        1 for path in remaining if path in attempted_paths
    )
    outcome = lyrics_mode.summarize_lyric_retry(
        counts,
        attempted=len(existing),
        remaining=attempted_remaining,
    )
    resolved = outcome["resolved"]
    failed = outcome["failed"]
    if not refreshed:
        job.summary = (
            f"Processed {plural(len(existing), 'track')}, but the saved retry "
            "queue couldn't be updated and was left unchanged."
        )
        job.error = job.summary
        _mark_job_failed(job)
        log.info(job.summary)
        return
    stopped = bool(counts.get("stopped"))
    if stopped:
        job.summary = (
            f"Stopped. Resolved {resolved}; {failed} failed; "
            f"{plural(len(remaining), 'track')} still queued for retry."
        )
    elif failed:
        job.summary = f"Resolved {resolved}; {failed} failed and need review."
        if remaining:
            job.summary += (
                f" {plural(len(remaining), 'track')} still queued for retry."
            )
        job.error = f"{failed} lyric retries failed and need review."
        _mark_job_failed(job)
    elif remaining:
        job.summary = (f"Resolved {resolved}. {plural(len(remaining), 'track')} "
                       "still unresolved, will retry next time.")
        job.error = (
            f"{plural(len(remaining), 'lyric retry')} remain unresolved."
        )
        _mark_job_failed(job)
    else:
        job.summary = f"All {plural(len(existing), 'retried track')} resolved."
    log.info(job.summary)
    if not stopped and not failed and not remaining:
        _close_completed_job(job)


def run_library_lyrics(job, *, rescan=False, synced_only=False):
    """Fetch lyrics for every library track that's missing them."""
    if not lyrics.HAVE_LYRICS:
        job.summary = "Lyric fetching isn't available; the syncedlyrics library isn't installed."
        job.error = job.summary
        _mark_job_failed(job)
        log.info(job.summary)
        return

    log.info(f"Fetching lyrics across the library (writing {(cfg.LYRICS_FORMAT or 'embed').lower()}).")
    if rescan:
        log.info("Re-checking every track (ignoring saved state).")
    # Hold the staging lock: the engine rewrites library FLACs in place, which
    # must not race the scan-lane downsample/repair/upgrade work on the same tree.
    with job_mgr.staging_lock():
        job_mgr.set_staging_holder("Lyrics scan")
        try:
            res = lyrics.run_library_lyrics(rescan=rescan, synced_only=synced_only,
                         should_stop=lambda: job.cancel_requested, log=log)
        finally:
            job_mgr.set_staging_holder(None)

    total = res.get("total", 0)
    if not total:
        job.summary = "No FLAC files found in the library."
        log.info(job.summary)
        _close_completed_job(job)
        return
    if res.get("stopped"):
        stop_total = max(0, int(res.get("stop_total", total)))
        processed = min(stop_total, max(0, int(res.get("processed", 0))))
        job.summary = (
            f"Stopped after processing {processed} of "
            f"{plural(stop_total, 'track')}.")
        log.info(job.summary)
        return

    counts = lyrics.summarize_lyrics_result(res)
    processed = counts["processed"]
    skipped = counts["already_checked"]
    if not processed:
        job.summary = (
            f"Nothing needed checking, all {plural(total, 'track')} have "
            "lyrics or were checked before. Tick “Re-check everything” to "
            "redo them.")
        log.info(job.summary)
        _close_completed_job(job)
        return
    parts = [
        f"{plural(processed, 'track')} checked",
        f"{plural(counts['wrote'], 'track')} got lyrics",
    ]
    for count, phrase in (
        (counts["already"],
         f"{plural(counts['already'], 'track')} already had lyrics"),
        (counts["not_found"],
         f"no lyrics found for {plural(counts['not_found'], 'track')}"),
        (counts["missing_tags"],
         f"{plural(counts['missing_tags'], 'track')} missing tags"),
        (counts["too_long"],
         f"{plural(counts['too_long'], 'track')} too long"),
        (counts["policy_skipped"],
         f"{plural(counts['policy_skipped'], 'track')} skipped by policy"),
        (counts["unsafe"],
         f"{plural(counts['unsafe'], 'unsafe path')} refused"),
        (counts["unavailable"],
         f"{plural(counts['unavailable'], 'track')} couldn't reach a provider "
         "(re-run later)"),
        (counts["errors"],
         f"{plural(counts['errors'], 'track')} hit an error"),
        (counts["other_errors"],
         f"{plural(counts['other_errors'], 'track')} returned an unexpected "
         "result"),
    ):
        if count:
            parts.append(phrase)
    if counts["failures"]:
        failures = []
        if counts["unsafe"]:
            failures.append(
                f"{plural(counts['unsafe'], 'unsafe track path')} refused")
        if counts["unavailable"]:
            failures.append(
                f"{plural(counts['unavailable'], 'track')} couldn't reach a provider")
        if counts["errors"]:
            failures.append(f"{plural(counts['errors'], 'track')} hit an error")
        if counts["other_errors"]:
            failures.append(
                f"{plural(counts['other_errors'], 'track')} returned an "
                "unexpected result")
        job.error = (
            "Lyrics pass finished with errors: " + "; ".join(failures) + ".")
        _mark_job_failed(job)
    if skipped:
        parts.append(f"{skipped} skipped (already checked)")
    job.summary = " · ".join(parts) + "."
    if not counts["failures"]:
        _close_completed_job(job)
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
    if not items:
        # An empty source is nearly always the wrong folder or a share that
        # did not mount. Reporting it as a finished scan with a manifest of
        # nothing sent people away believing their library had been read.
        job.error = (
            "No music files were found in the source folder. Check that it is "
            "the folder you meant and that its drive or share is attached, "
            "then scan again. Nothing was copied and no plan was written.")
        job.summary = job.error
        _mark_job_failed(job)
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
        _mark_job_failed(job)
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
    job.execute_args["requires_low_space_override"] = bool(
        in_place and free is not None and need > free
    )
    if need and free is not None:
        space = f"≈{format_size(need)} to {verb}, {format_size(free)} free at the destination"
        if need > free:
            space = ("⚠ not enough free space: needs "
                     f"≈{format_size(need)} but only {format_size(free)} is free")
            if in_place:
                space += (
                    ". The in-place move stays blocked until you confirm the "
                    "low-space risk below"
                )
        parts.append(space)
    # Show the manifest where the user can actually find it, the container
    # path means nothing from a phone. Settings does the same for every path
    # it displays. Runtime import: app imports flows at startup.
    from qobuz_librarian.web.app import _resolve_host_path
    manifest_display, _ = _resolve_host_path(str(manifest))
    job.summary = ("; ".join(parts) + ". Unidentified and skipped files stay "
                   f"where they are. Full plan written to {manifest_display}.")
    # Not logged here: job.summary already renders on the job page, and the
    # activity log below it would otherwise repeat the exact same sentence.


def execute_migration(job, chosen, dest, *, in_place, src=None,
                      allow_low_space=False):
    """Copy (or move) the files behind the approved albums into the layout."""
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
            _mark_job_failed(job)
            return
        if manifest_artifact is None:
            manifest_artifact = payload_manifest
        elif payload_manifest != manifest_artifact:
            job.error = (
                "The selected albums came from different migration previews. "
                "Nothing was changed; scan again and select albums from one "
                "preview.")
            job.summary = job.error
            _mark_job_failed(job)
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
            _mark_job_failed(job)
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
                _mark_job_failed(job)
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
                _mark_job_failed(job)
                return
            src_s, dest_s, sealed_source, sealed_destination_path = raw
            entries.append(migrate_engine.PlanEntry(
                source=Path(src_s), status=migrate_engine.PLACE, dest_rel=Path(dest_s),
                source_receipt=sealed_source,
                destination_path_receipt=sealed_destination_path))
        for raw in payload.get("resume_entries", []):
            if not isinstance(raw, (list, tuple)) or len(raw) != 4:
                job.error = (
                    "The saved migration preview is incomplete. Nothing was "
                    "changed; scan again.")
                job.summary = job.error
                _mark_job_failed(job)
                return
            src_s, dest_s, sealed_source, sealed_destination = raw
            entries.append(migrate_engine.PlanEntry(
                source=Path(src_s), status=migrate_engine.COLLISION,
                dest_rel=Path(dest_s), reason="destination already exists",
                source_receipt=sealed_source,
                destination_receipt=sealed_destination))
    if not entries:
        job.error = (
            "The selected migration review contains no saved files. Nothing "
            "was changed; scan again."
        )
        job.summary = job.error
        _mark_job_failed(job)
        return

    plan = migrate_engine.MigrationPlan(
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
    # A cross-filesystem move can run for hours; name the holder so a download
    # that blocks on the staging mutex shows what it's waiting behind instead of
    # sitting on RUNNING with no reason (the same treatment the lyrics scan gets).
    result = None
    results_artifact = None
    results_manifest = None
    results_error = None
    execution_abort = None
    with job_mgr.staging_lock():
        job_mgr.set_staging_holder("Library migration")
        try:
            if not migrate_engine.verify_audit_artifact(plan, manifest_artifact):
                job.error = (
                    "The saved migration preview no longer matches the files. "
                    "Nothing was moved; scan again before approving it.")
                job.summary = job.error
                _mark_job_failed(job)
                log.info(job.summary)
                return
            # This decisive estimate belongs inside the same mutation interval
            # as execution.
            need, free = migrate_engine.space_estimate(
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
                    "destination, or scan again and confirm the low-space "
                    "risk on the new review.")
                job.summary = job.error
                _mark_job_failed(job)
                log.info(job.summary)
                return
            try:
                result = migrate_engine.execute_plan(
                    plan, in_place=in_place,
                    cancel_check=lambda: job.cancel_requested,
                    progress=job.push_progress,
                    resume_entries=resume_entries)
            except migrate_engine.MigrationExecutionAbort as exc:
                result = exc.result
                execution_abort = exc
            pruned = getattr(result, "pruned", 0)
            try:
                results_artifact = migrate_engine.write_results_manifest(
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
            job_mgr.set_staging_holder(None)
    for failed_src, reason in result.failures[:50]:
        job.push_line(f"failed: {failed_src} - {reason}")
    companion_outcomes = getattr(result, "companion_outcomes", ())
    companion_skipped = sum(
        status == migrate_engine.SKIPPED
        for _source, _destination, status, _reason in companion_outcomes
    )
    companion_failed = sum(
        status == migrate_engine.FAILED
        for _source, _destination, status, _reason in companion_outcomes
    )
    for source, _destination, status, reason in companion_outcomes:
        if status == migrate_engine.FAILED:
            job.push_line(f"sidecar failed: {source} - {reason}")
    recoveries = tuple(getattr(result, "recoveries", ()))
    for recovery in recoveries:
        location = recovery.get("location", "unknown location")
        restart = recovery.get("restart", "Inspect it before another migration.")
        job.push_line(f"kept for recovery: {location} - {restart}")

    verb = "moved" if in_place else "copied"
    has_problem = bool(
        result.failed or companion_failed or recoveries or results_error
    )
    if has_problem:
        lead = (
            "Migration stopped with problems"
            if result.cancelled else "Migration needs attention"
        )
    elif result.cancelled:
        lead = "Migration stopped early"
    else:
        lead = f"{plural(result.copied, 'file')} {verb} into {dest}"
    parts = [lead]
    if has_problem or result.cancelled:
        parts.append(f"{plural(result.copied, 'file')} {verb} into {dest}")
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
        parts.append("the destination may contain a partial migration")
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
    if has_problem:
        _mark_job_failed(job)
    elif result.cancelled:
        job.status = job_mgr.JobStatus.CANCELED
    job.summary = "; ".join(parts) + "."
    log.info(job.summary)
    if not has_problem and not result.cancelled:
        # The exact copies, optional source retirement, and results report are
        # all durable. Close under the cancellation lock so a Stop arriving at
        # this final boundary cannot relabel the completed migration.
        _close_completed_job(job)
