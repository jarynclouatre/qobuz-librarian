"""Scan / execute logic behind the web Artist and Library flows.

These wrap the same engine the CLI uses (catalog matching, gap detection,
process_album) but without any terminal prompts, a scan attaches review
candidates to the job, and execution runs over the candidates the user kept.
"""
import argparse
import contextlib
import dataclasses
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import disk_support, repair_log, saved_views, state_file
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api.auth import AuthLost, QobuzAccess, QobuzUnavailable
from qobuz_librarian.api.search import get_album
from qobuz_librarian.download_result import (
    cd_quality_note,
    download_attention_kind,
    failure_cause,
    incomplete_track_counts,
    track_shortfall,
)
from qobuz_librarian.integrations import beets as beets_mod
from qobuz_librarian.integrations import downsample_engine
from qobuz_librarian.integrations.lyrics import lyric_fetch
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import (
    candidate_premise,
    catalog,
    collection_snapshot,
    downsample_state,
    flac_cache,
    generation_state,
    library_scan_state,
    lyrics,
    missing_types,
    scan_checkpoint,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import migrate as migrate_engine
from qobuz_librarian.library import new_releases as new_releases_mod
from qobuz_librarian.library import unreadable_artists as unreadable_artists_mod
from qobuz_librarian.library.artist_fingerprint import artist_fingerprint
from qobuz_librarian.library.candidate_premise import CandidateStale
from qobuz_librarian.library.catalog import (
    album_is_upcoming,
    album_quality_label,
    album_year,
    find_qobuz_album_for_dir,
    is_lossless_album,
)
from qobuz_librarian.library.discovery import (
    AlbumGap,
    DiscoveryOpts,
    find_missing_for_artist,
    find_new_releases_for_artist,
    flush_resolve_cache,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    is_library_system_folder,
    list_artist_album_dirs,
    list_library_artists,
    start_skip_record,
    undecodable_named,
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
from qobuz_librarian.web import (
    download_outcomes,
    job_persistence,
    review_badges,
    settings_store,
    storage,
)
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
        include_live=False,
        no_catalog=False,
        auto_safe=False,
        # Whole-album replacement belongs to the explicit Upgrade review,
        # which enables it for that approved run.
        auto_upgrade=False,
        verbose=False,
    )


def _fresh_music_root():
    """True when the music folder holds nothing yet and no collection backup
    says it once held albums: a new install, not a dropped mount."""
    try:
        empty = not any(not is_library_system_folder(entry.name)
                        for entry in Path(cfg.MUSIC_ROOT).iterdir())
    except OSError:
        return False
    return empty and collection_snapshot.music_root_write_state()[0] == "ready"


def _set_empty_library_summary(job):
    """Describe the expected layout and name where the music path is set.

    Inside the image the container path is meaningless on the host, and there
    is no Settings control for it, so the message has to name the mount.
    """
    if _fresh_music_root():
        job.summary = "Nothing to scan yet. The music folder is empty."
        log.info("The music folder is empty, so there is nothing to scan yet.")
        return
    if cfg.in_container():
        where = ("Nothing to scan: no artist folders were found under the "
                 "/music mount.")
        fix = "Check what your Compose file mounts at /music."
    else:
        where = ("Nothing to scan: no artist folders were found in "
                 f"{cfg.MUSIC_ROOT}.")
        fix = "Check MUSIC_ROOT."
    job.error = (
        f"{where} Qobuz Librarian expects one folder per artist, each "
        f"holding album folders. {fix}"
    )
    # A run that never scanned anything is not a clean pass. submit_scan leaves
    # an explicitly-set terminal status alone, so this keeps the green Done
    # chip off a scan the user still has to fix something for.
    job.status = job_mgr.JobStatus.FAILED
    log.info("No artist folders found in the configured music library.")
    log.info("  Expected layout: <music library>/<Artist>/<Album (Year)>/<track>.flac")
    log.info(f"  {fix}")


def _mark_job_failed(job):
    """Set an explicit terminal outcome for a handled execution failure."""
    job.status = job_mgr.JobStatus.FAILED


def _close_completed_job(job):
    """Publish exact completion under the same lock as cancellation."""
    with job._lock:
        if job.status is job_mgr.JobStatus.RUNNING:
            job.cancel_requested = False
            job.status = job_mgr.JobStatus.DONE


def _without_unreadable(refresh, unreadable):
    """A quality pass whose only failures are folders left out as unreadable,
    counted complete for the rest."""
    if (refresh is None or refresh.complete or not refresh.errors
            or set(refresh.errors) - set(unreadable)):
        return refresh
    return dataclasses.replace(refresh, errors={}, complete=True)


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


def _refresh_after_local_album_change(
    album,
    result=None,
    *,
    fallback_artist=None,
    token=None,
    args=None,
    claims_album=False,
):
    if (result or {}).get("imported") and result.get("dir"):
        reseal_past_album_import(result["dir"])
    saved_views.refresh_after_changes(
        [{**(result or {}), "album": album, "fallback_artist": fallback_artist}],
        token,
        args or build_args(),
        claims_album=claims_album,
    )


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
    version = (album.get("version") or "").strip()
    if version:
        payload["version"] = version
    if album.get("parental_warning"):
        payload["explicit"] = True
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


def _add_candidate_spec(job, spec, listed=True):
    return job.add_candidate(
        kind=spec.get("kind", "album"),
        title=spec.get("title") or "?",
        artist=spec.get("artist") or "",
        detail=spec.get("detail") or "",
        payload=spec.get("payload") or {},
        selected=bool(spec.get("selected")),
        listed=listed,
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


def _read_without_release_types(candidates):
    """Whether an artist's saved missing albums were found without Qobuz's
    release lists, so a refresh reads the artist again rather than keep them
    all listed."""
    return any(not is_gap_candidate(c)
               and not (c.get("payload") or {}).get("release_type")
               for c in candidates)


def _readd_candidate(job, c, listed=True):
    """Re-add a candidate restored from a scan checkpoint, with a fresh cid."""
    return _add_candidate_spec(job, c, listed=listed)


def _cap_note(job) -> str:
    """A truncation notice appended to a scan summary when the candidate list hit
    the in-memory cap, so a summary never implies more results are reviewable
    than were actually kept. Empty when nothing was dropped."""
    if not job.candidate_cap_hit:
        return ""
    return (f" Showing the first {len(job.candidates):,}; the scan hit the "
            f"{job.CANDIDATE_CAP:,} result cap. Work through or dismiss some "
            "results, then refresh to see the rest.")


def left_out_note(left_out, cap) -> str:
    """Name the artist folders whose finds the review had no room for, so a
    summary never implies more is listed than was kept. Empty when nothing
    was left out."""
    if not left_out:
        return ""
    names = ", ".join(sorted(left_out, key=str.casefold)[:5])
    more = f" (+{len(left_out) - 5} more)" if len(left_out) > 5 else ""
    finds = sum(left_out.values())
    return (f"The review lists at most {cap:,} albums, so {finds:,} "
            f"{'find' if finds == 1 else 'finds'} from "
            f"{plural(len(left_out), 'artist folder')} "
            f"{'is' if finds == 1 else 'are'} not listed: {names}{more}. "
            "Work through or dismiss some, then refresh to list them.")


def _gap_candidate_spec(
    gap,
    artist_name,
    selected=False,
    is_new=False,
    artist_key=None,
    missing_premises=None,
):
    """Turn an engine AlbumGap into a review candidate. A partial gap carries
    its missing-track count so the detail reads 'gap-fill: N missing'."""
    album = gap.qobuz_album
    if gap.on_disk_dir is not None:
        album = {**album, "_partial_missing_count": gap.missing_count}
    extra_payload = {"_artist_dir": artist_key} if artist_key else {}
    if gap.on_disk_dir is not None:
        extra_payload["album_dir"] = str(gap.on_disk_dir)
        if gap.bonus_only:
            extra_payload["bonus_only"] = True
        premise = candidate_premise.capture("gap-fill", gap.on_disk_dir)
        # The present tracks are Qobuz's, with no local path; the album seal
        # already holds every file the whole-album path would move aside.
        extra_payload["_gap_fill_receipts"] = (
            candidate_premise.gap_fill_receipts(premise) or {})
    else:
        if gap.release_type:
            extra_payload["release_type"] = gap.release_type
        artist_dir = cfg.MUSIC_ROOT / artist_key if artist_key else None
        extra_payload["_artist_dir_path"] = str(artist_dir or "")
        premise = (missing_premises or {}).get(artist_dir) if artist_dir else None
        if premise is None and artist_dir:
            # One seal of the artist folder serves every missing album under
            # it. A failed capture is not cached: it would cost the rest of
            # that artist's rows their premise over one transient read.
            premise = candidate_premise.capture("missing", artist_dir)
            if premise is not None and missing_premises is not None:
                missing_premises[artist_dir] = premise
    if premise is not None:
        extra_payload["_premise"] = premise
    return _album_candidate_spec(
        album, artist_name, selected=selected, is_new=is_new,
        extra_payload=extra_payload)


def _add_gap_candidate(job, gap, artist_name, selected=False, is_new=False,
                       artist_key=None, missing_premises=None):
    return _add_candidate_spec(
        job, _gap_candidate_spec(
            gap, artist_name, selected, is_new, artist_key=artist_key,
            missing_premises=missing_premises))


def add_restore_candidate(job, album, artist_name, *, artist_key=None,
                          album_dir=None, missing=()):
    """Add one album a collection restore has to fetch back.

    Ticked on arrival: uploading a backup file is the intent, and the review is
    where it gets confirmed. ``artist_key`` is the artist's folder when the
    library still has one; without it the row seals the music folder itself,
    because there is no artist tree left to receipt. ``album_dir`` makes the
    row a Gap Fill for a folder that lost the ``missing`` tracks.
    """
    spec = _gap_candidate_spec(
        AlbumGap(qobuz_album=album, on_disk_dir=album_dir,
                 missing=list(missing)),
        artist_name,
        selected=True,
        artist_key=artist_key,
    )
    if artist_key is None:
        absent = candidate_premise.capture_absent(artist_name)
        if absent is None:
            return None
        spec["payload"][candidate_premise.ABSENT_CONTAINER_KEY] = absent
    return _add_candidate_spec(job, spec)


def candidate_matches_query(c, q):
    """The review filter's predicate, artist or album title contains ``q``."""
    hay = (c.get("artist") or "") + " " + (c.get("title") or "")
    return (q or "").lower() in hay.lower()


def is_gap_candidate(c):
    """Whether a saved review candidate is a Gap Fill entry (missing tracks in
    an owned album) rather than a fully missing album."""
    if (c.get("payload") or {}).get("gap_fill"):
        return True
    return "gap-fill:" in (c.get("detail") or "")


def review_tab_filter(tab, included=None):
    """Which candidates a library review lists on ``tab``: the Gap Fill
    rows, or the missing albums, that are ticked or that the switches in
    ``included`` (else the saved switches) list. Any other tab lists
    everything."""
    if tab not in ("missing", "gaps"):
        return lambda c: True
    if included is None:
        included = missing_types.load()
    if tab == "gaps":
        return lambda c: is_gap_candidate(c) and (
            bool(c.get("selected")) or missing_types.gap_shown(
                (c.get("payload") or {}).get("bonus_only"), included))
    return lambda c: not is_gap_candidate(c) and (
        bool(c.get("selected")) or missing_types.shown(
            (c.get("payload") or {}).get("release_type"), included))


def review_listed(included=None):
    """Which candidates a library review lists on either tab."""
    if included is None:
        included = missing_types.load()
    gaps = review_tab_filter("gaps", included)
    missing = review_tab_filter("missing", included)
    return lambda c: gaps(c) or missing(c)


def switch_holding(c):
    """The switch that decides whether a library review lists ``c``: Bonus
    versions for a Gap Fill row, else its release type."""
    payload = c.get("payload") or {}
    if is_gap_candidate(c):
        return missing_types.BONUS if payload.get("bonus_only") else None
    return payload.get("release_type")


def library_review_summary(candidates):
    """The parked library review's one-line summary, without the trailing
    period."""
    included = missing_types.load()
    in_gaps = review_tab_filter("gaps", included)
    shown = review_tab_filter("missing", included)
    gaps = sum(1 for c in candidates if in_gaps(c))
    missing = sum(1 for c in candidates if shown(c))
    return (f"{missing + gaps:,} to review across Missing Albums "
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
        return f"{plural(n, 'album')} stored above 48 kHz."
    if execute_kind == "collection_restore":
        return f"{plural(n, 'album')} from the backup to restore."
    return ""


def _album_label(candidate):
    """One album named the way the review names it."""
    artist = (candidate.get("artist") or "").strip()
    title = (candidate.get("title") or "?").strip()
    return f"{artist} · {title}" if artist else title


def named_albums(candidates, limit=3):
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


def fold_key(c):
    """A candidate's merge identity: its artist and Qobuz album id, falling
    back to artist+title for keyless carry-overs. The artist is part of it
    because one album can be listed under two artists, as one's album and
    another's compilation."""
    artist = (c.get("artist") or "").lower()
    album_id = str((c.get("payload") or {}).get("album_id") or "")
    if album_id:
        return artist, album_id
    return artist, (c.get("title") or "").lower()


def _fold_row(c):
    """Candidate content a refresh owns, excluding review identity and tick."""
    return {
        "kind": c.get("kind", "album"),
        "title": c.get("title") or "?",
        "artist": c.get("artist") or "",
        "detail": c.get("detail") or "",
        "payload": c.get("payload") or {},
    }


def _row_shows_change(fresh, old):
    """Whether a refreshed row reads differently in the review. The file
    evidence every scan seals again, the "new" badge, which only says when
    the row was found, and the payload's other bookkeeping are not changes
    a user can see."""
    def shown(row):
        payload = row["payload"]
        return (row["kind"], row["title"], row["artist"], row["detail"],
                payload.get("version"), payload.get("release_type"),
                bool(payload.get("gap_fill")), payload.get("cover"),
                payload.get("verified_truncated"))
    return shown(fresh) != shown(old)


def _scan_would_offer(c, coverage):
    """Whether a scan with this coverage decides this row: it re-derived the
    row's artist, and it looks for the row's kind of album."""
    artists, partial_only = coverage
    payload = c.get("payload") or {}
    artist = str(payload.get("_artist_dir") or c.get("artist") or "")
    return (
        c.get("kind", "album") == "album"
        and artist.casefold() in artists
        and (not partial_only or bool(payload.get("gap_fill")))
    )


def _folder_gone(c):
    """Whether the folder a Library row adds to has left the library: the
    album folder a Gap Fill row fills, the artist folder of a missing album.
    A folder that cannot be read is not gone."""
    path = candidate_premise.expected_path(c)
    if c.get("kind", "album") != "album" or not path:
        return False
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        pass
    return False


def _music_root_listed():
    """Whether the music folder can be read and holds anything."""
    try:
        with os.scandir(cfg.MUSIC_ROOT) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def fold_new_candidates(parked, cands, *, review_generation=None,
                        coverage=None):
    """Merge a refresh's finds into a parked review, keyed by artist and
    Qobuz album id (see fold_key).

    Entries the refresh didn't touch, and the user's ticks on them, are
    never changed. A same-key row found by the refresh takes its fresh title,
    detail, class, and payload while keeping the review row's cid, sequence,
    and user tick. Absence is evidence only inside ``coverage``, the artists a
    finished scan derived again, whether it looked at Gap Fill alone and the
    Qobuz ids it found on disk: a row there that the scan no longer offers is
    removed when it is unticked or on disk, and a ticked one otherwise stays.
    A row whose folder is gone leaves too, ticked or not: its seal can never
    match again, so Download would refuse it after every refresh.
    Candidates the user dismissed while the refresh ran are checked against a
    fresh hidden snapshot so the fold can't resurrect them. Returns
    (added, updated, removed, held_back, left_out, notes), False when the
    changed review could not be saved, or None when the review stopped being
    parked mid-refresh (approved/discarded). ``updated`` and ``removed`` count
    the rows the review lists that changed or left; ``held_back`` maps the
    switches (switch_holding) to the added rows they keep out of the list;
    ``left_out`` maps artist folders to the listed rows the review had
    no room for; ``notes`` are the lines the fold added to the review's log."""
    _key = fold_key
    def _fold():
        if parked.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return None
        # Load while the review-action lock is held. A dismissal that finishes
        # while this fold waits must be visible before any candidate is added.
        hidden = hidden_mod.load()
        library = parked.execute_kind == "library"
        included = missing_types.load()
        listed = (review_listed(included) if library
                  else review_tab_filter(""))

        def switched_on(c):
            if is_gap_candidate(c):
                return missing_types.gap_shown(
                    (c.get("payload") or {}).get("bonus_only"), included)
            return missing_types.shown(
                (c.get("payload") or {}).get("release_type"), included)

        fresh_by_key = {}
        for c in cands:
            fresh_by_key.setdefault(_key(c), c)
        covered = None
        on_disk = frozenset()
        if coverage is not None:
            covered = ({name.casefold() for name in coverage[0]}, coverage[1])
            on_disk = coverage[2]
        # A scan that read no artist folder says nothing about any of them,
        # and neither does a music folder that cannot be listed now; the
        # library may not be mounted.
        drop_gone = bool(covered and covered[0]) and _music_root_listed()
        updated = 0
        removed = 0
        # A ticked row stays whatever the refresh makes of it, unless the
        # scan found the album on disk. The log names the ticked ones that
        # left, says once which ones it no longer offers (then marks them)
        # and how many moved under a switch that is off. A kept missing album
        # takes the seal of its artist folder as the refresh read it, so its
        # pick is not refused for a change the refresh has already looked at.
        in_library = []
        unoffered = []
        moved = {}
        seals = {}
        for c in cands:
            payload = c.get("payload") or {}
            if (candidate_premise.expected_kind(c) == "missing"
                    and payload.get("_premise")):
                seals.setdefault(payload.get("_artist_dir_path"),
                                 payload["_premise"])
        keep = []
        for c in parked.candidates:
            key = _key(c)
            fresh = fresh_by_key.get(key)
            if fresh is None and drop_gone and _folder_gone(c):
                if listed(c):
                    removed += 1
                continue
            if fresh is None and covered and _scan_would_offer(c, covered):
                album_id = str((c.get("payload") or {}).get("album_id") or "")
                if not c.get("selected") or album_id in on_disk:
                    if listed(c):
                        removed += 1
                    if c.get("selected"):
                        in_library.append(c)
                    continue
                payload = dict(c.get("payload") or {})
                path = payload.get("_artist_dir_path")
                if path and candidate_premise.expected_kind(c) == "missing":
                    if path not in seals:
                        seals[path] = candidate_premise.capture("missing", path)
                    if seals[path] is not None:
                        payload["_premise"] = seals[path]
                if not payload.get("_unoffered"):
                    payload["_unoffered"] = True
                    unoffered.append(c)
                keep.append({**c, "payload": payload})
                continue
            if fresh is None or _fold_row(fresh) == _fold_row(c):
                keep.append(c)
                continue
            replacement = _fold_row(fresh)
            if not (c.get("payload") or {}).get("is_new"):
                # Already in the review, so not a new find.
                replacement["payload"] = {
                    key: value for key, value in replacement["payload"].items()
                    if key != "is_new"}
            replacement.update({
                "cid": c.get("cid"),
                "seq": c.get("seq"),
                "selected": bool(c.get("selected")),
            })
            keep.append(replacement)
            if (library and replacement["selected"] and switched_on(c)
                    and not switched_on(fresh)):
                switch = switch_holding(fresh)
                moved[switch] = moved.get(switch, 0) + 1
            if (_row_shows_change(_fold_row(fresh), _fold_row(c))
                    and (listed(fresh) or listed(c))):
                updated += 1
        # Older builds matched rows by album id alone and could leave an
        # album listed under two artists as two missing-album rows under one
        # of them. Keep the first, with either row's tick.
        first_at = {}
        unique = []
        for c in keep:
            key = _key(c) if library and not is_gap_candidate(c) else None
            at = first_at.get(key) if key is not None else None
            if at is None:
                if key is not None:
                    first_at[key] = len(unique)
                unique.append(c)
                continue
            if c.get("selected") and not unique[at].get("selected"):
                unique[at] = {**unique[at], "selected": True}
            if listed(c):
                removed += 1
        keep = unique
        parked.candidates = keep
        seen = {_key(c) for c in keep}
        listed_rows = sum(1 for c in keep if listed(c))
        added = 0
        held_back = {}
        left_out = {}
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
            if listed(c) and listed_rows >= parked.CANDIDATE_CAP:
                parked._candidate_cap_noted = True
                if isinstance(parked.execute_args, dict):
                    parked.execute_args["_candidate_cap_hit"] = True
                folder = str((c.get("payload") or {}).get("_artist_dir")
                             or c.get("artist") or "")
                left_out[folder] = left_out.get(folder, 0) + 1
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
            if listed(c):
                listed_rows += 1
            else:
                switch = switch_holding(c)
                held_back[switch] = held_back.get(switch, 0) + 1
        if review_generation is not None:
            parked.execute_args = {
                **(parked.execute_args or {}),
                "_library_review_generation": int(review_generation),
            }
        held = sum(held_back.values())
        notes = []
        changes = [f"{verb} {plural(n, 'album')}" for verb, n in (
            ("added", added - held), ("updated", updated), ("removed", removed))
            if n]
        if coverage is not None and (changes or held):
            # Say what the refresh changed in the list the review shows.
            line = "A later refresh "
            if changes:
                line += (", ".join(changes[:-1])
                         + (" and " if len(changes) > 1 else "")
                         + changes[-1] + ".")
                if held:
                    line += (f" {held} more {'is' if held == 1 else 'are'} "
                             f"under {missing_types.named(held_back)}.")
            else:
                line += (f"added {plural(held, 'album')} under "
                         f"{missing_types.named(held_back)}.")
            notes.append(line)
        if in_library:
            n = len(in_library)
            notes.append(
                f"{plural(n, 'ticked album')} {'is' if n == 1 else 'are'} "
                f"now in the library and left the review: "
                f"{named_albums(in_library)}.")
        if unoffered:
            n = len(unoffered)
            notes.append(
                f"The refresh no longer offers {plural(n, 'ticked album')}, "
                f"which {'stays' if n == 1 else 'stay'} ticked: "
                f"{named_albums(unoffered)}.")
        if moved:
            n = sum(moved.values())
            notes.append(
                f"{plural(n, 'ticked album')} {'is' if n == 1 else 'are'} "
                f"now under {missing_types.named(moved)} and "
                f"{'stays' if n == 1 else 'stay'} listed while ticked.")
        # Saved with this change; the review's lock is already held.
        parked.log_lines.extend(notes)
        parked._trim_log_lines_locked()
        return added, updated, removed, held_back, left_out, notes

    with job_mgr._library_review_state_guard(parked), parked._review_action_lock:
        saved, result = job_persistence.persist_review_mutation(parked, _fold)
        if saved and result is not None and not parked.candidates:
            if job_mgr.finalize_review_if_empty(
                    parked, summary="Nothing left to review.") is None:
                return False
        return result if saved else False


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
        if job.execute_kind != "library":
            continue
        if parked is None or (job.created_at or 0) > (parked.created_at or 0):
            parked = job
    if parked is None:
        return None
    wanted_artists = {hidden_mod.artist_key(a) for a in artists}
    wanted_artists.discard("")
    wanted_fps = set(fingerprints)
    specs = []
    state = library_scan_state.kind_state("missing")
    for name, entry in (state.get("artists") or {}).items():
        for spec in (entry or {}).get("candidates") or []:
            artist = spec.get("artist") or name
            title = spec.get("title") or ""
            if (hidden_mod.artist_key(artist) in wanted_artists
                    or hidden_mod.album_fingerprint(artist, title)
                    in wanted_fps):
                spec = dict(spec)
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
        if j.execute_kind != execute_kind:
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
        parked.execute_kind, parked.candidates)
    folded = fold_new_candidates(parked, specs)
    if folded is False:
        return False
    if folded is None:
        return None
    if ours:
        parked.summary = split_review_summary(
            parked.execute_kind, parked.candidates)
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
        job = job_mgr.Job(title="New releases",
                          execute_kind="new_releases",
                          status=job_mgr.JobStatus.AWAITING_REVIEW)
    elif execute_kind == "upgrade":
        job = job_mgr.Job(title="Albums to upgrade",
                          execute_kind="upgrade",
                          status=job_mgr.JobStatus.AWAITING_REVIEW,
                          review_verb="Upgrade")
    elif execute_kind == "repair":
        job = job_mgr.Job(title="Repair scan", kind="scan",
                          execute_kind="repair",
                          status=job_mgr.JobStatus.AWAITING_REVIEW,
                          review_verb="Repair")
    elif execute_kind == "collection_restore":
        job = job_mgr.Job(title="Restore from backup",
                          execute_kind="collection_restore",
                          status=job_mgr.JobStatus.AWAITING_REVIEW)
    elif execute_kind == "downsample":
        job = job_mgr.Job(title="Albums to downsample", kind="scan",
                          execute_kind="downsample",
                          status=job_mgr.JobStatus.AWAITING_REVIEW,
                          review_verb="Downsample")
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
    elif execute_kind == "downsample":
        job._execute_fn = lambda j, chosen: execute_downsamples(
            j, chosen, keep_originals=(j.execute_args or {}).get(
                "keep_originals"))
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
    elif execute_kind == "collection_restore":
        job.summary = (f"{n:,} album{'s' if n != 1 else ''} from the backup "
                       "didn't download last time. Ticked and ready to retry.")
    elif execute_kind == "downsample":
        job.summary = (f"{n:,} album{'s' if n != 1 else ''} didn't downsample "
                       "last time. Ticked and ready to retry.")
    else:
        job.summary = (f"{n:,} album{'s' if n != 1 else ''} didn't download "
                       "last time. Ticked and ready to retry.")
    if not job_persistence.admit(job):
        return False
    job_mgr.registry.add(job)
    return job


def _return_picks(picks, execute_kind, execute_args=None):
    """Return unfinished picks to their review, ticked, creating one if needed.

    A new release stays in the New Releases review until it's downloaded or
    dismissed: the persistent baseline consumed its id at check time, so
    without this it would never be offered as new again."""
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


def _return_upgrade_picks(retry, refused, execute_args):
    """Return unfinished Upgrade picks to the Upgrade list ticked, and those
    whose new copy was refused unticked, as another try fetches the same
    copy."""
    if not refused:
        return _return_picks(retry, "upgrade", execute_args)
    picks = ([dict(c, selected=True) for c in retry]
             + [dict(c, selected=False) for c in refused])
    folded = refold_into_living_review(picks, "upgrade", ticked=False)
    if folded is not None:
        return folded is not False
    n = len(picks)
    summary = f"{n:,} album{'s' if n != 1 else ''} didn't upgrade last time."
    if retry:
        summary += f" {len(retry):,} ticked and ready to retry."
    return _park_library_failures(
        picks, "upgrade", ticked=False, summary=summary,
        execute_args=execute_args) is not False


def return_interrupted_picks(job):
    """Put the ticked albums of a run the app stopped mid-way back in its
    review, ticked. Any of them may already be done, so one whose files no
    longer match the review is left out, and so is its saved results row,
    as the review would only refuse it. A missing album is not left out
    for a change the run made itself, a download into its artist folder.
    Returns a sentence naming where they went, or "" for none."""
    with job._lock:
        picks = [c for c in job.candidates if c.get("selected")]
    written = [Path(p) for p in
               (job.execute_args or {}).get(_WRITTEN_INTO) or ()]

    def changed_by_this_run(c):
        if candidate_premise.expected_kind(c) != "missing":
            return False
        folder = candidate_premise.expected_path(c)
        if not folder or not any(p.is_relative_to(folder) for p in written):
            return False
        try:
            candidate_premise.validate_container(c)
        except CandidateStale:
            return False
        return True

    seals = {}
    left_out = []
    unfinished = []
    for c in picks:
        if (candidate_premise.changed_on_disk(c, seals)
                and not changed_by_this_run(c)):
            left_out.append(c)
        else:
            unfinished.append(c)
    unfinished = _resealed_past_own_imports(job, unfinished)
    surface = job.execute_kind
    remove = {
        "upgrade": lambda p: upgrade_state.remove_album_dir(p.get("album_dir")),
        "downsample": lambda p: downsample_state.remove_album_dir(
            p.get("album_dir")),
        "library": lambda p: library_scan_state.remove_album(p.get("album_id")),
    }.get(surface)
    if (left_out and remove is not None
            and generation_state.output_is_current(surface)
            and not all([remove(c.get("payload") or {}) for c in left_out])):
        generation_state.mark_output_status(
            surface, "stale",
            reason="An album changed before a restart, and these saved "
                   "results could not be updated.")
    if left_out:
        n = len(left_out)
        job.push_line(f"{plural(n, 'ticked album')} changed on disk and left "
                      f"the review: {named_albums(left_out)}.")
    saved = True
    if unfinished:
        saved = _return_picks(
            unfinished, surface,
            execute_args=(None if surface in ("library", "new_releases")
                          else job.execute_args)) is not False
        if not saved:
            job.push_line(
                "Retry choices could not be saved back to their review. "
                "Check the data folder, then refresh before retrying.")
        else:
            job.push_line(f"{plural(len(unfinished), 'album')} returned to "
                          "the review, selected for retry.")
    job_persistence.persist(job)
    if unfinished and saved:
        return (f"{named_albums(unfinished)} went back to the review, ticked, "
                "to download again.")
    if unfinished:
        return (f"{named_albums(unfinished)} could not be put back in the "
                "review.")
    if left_out:
        return f"{named_albums(left_out)} changed on disk and left the review."
    return ""


def note_upgrades_put_back(origins):
    """Say on the page of each Upgrade run this start found cut short which
    of its albums the start-up put back as they were."""
    folders = {os.fspath(origin) for origin in origins}
    for job in job_mgr.registry.all():
        if (job.execute_kind != "upgrade" or job.recoveries
                or not job._cut_short_this_start):
            continue
        with job._lock:
            albums = [c for c in job.candidates if c.get("selected")
                      and (c.get("payload") or {}).get("album_dir") in folders]
            if not albums:
                continue
            note = (f"{named_albums(albums)} "
                    + ("was put back as it was." if len(albums) == 1
                       else "were put back as they were."))
            job.summary = f"{job.summary} {note}" if job.summary else note
        job_persistence.persist(job)


def reseal_put_back_album(backup_receipt):
    """Seal again the rows and saved results over an album a restore put back
    byte for byte, so the new inodes of a copy do not make Start refuse it,
    including the pick of a run still going that will return it for retry.
    Reads file details only."""
    _reseal_reviews(candidate_premise.sealer_after_put_back(backup_receipt))


def reseal_after_own_writes(writes):
    """Seal again the rows and saved results over folders whose only change
    is the app's own file ``writes``, such as a lyrics pass's tag writes, so
    Start does not refuse them. Reads file details only."""
    if writes:
        _reseal_reviews(candidate_premise.sealer_after_writes(writes))


def reseal_past_album_import(album_dir):
    """Seal again the missing albums whose artist folder changed only by
    ``album_dir``, a new folder a download filed an album into, so Start does
    not refuse another album of that artist for it. One that the new folder
    may hold keeps its old seal. Reads file details only."""
    album_dir = Path(album_dir)
    captures = {}

    def fresh_seal(value):
        if (type(value) is not dict or value.get("kind") != "missing"
                or type(value.get("path")) is not str):
            return None
        path = Path(value["path"])
        if album_dir == path or not album_dir.is_relative_to(path):
            return None
        if candidate_premise.canonical_premise(value) is None:
            return None
        if path not in captures:
            captures[path] = candidate_premise.capture("missing", path)
        return candidate_premise.resealed_after_own_imports(
            value, captures[path], {album_dir.relative_to(path).parts[0]})

    def admits(c):
        path = Path(c["payload"]["_premise"]["path"])
        return not _may_hold(
            c, [path / album_dir.relative_to(path).parts[0]])

    _reseal_reviews(fresh_seal, admits)


def _reseal_reviews(fresh_seal, admits=None):
    def resealed(c):
        payload = c.get("payload") or {}
        fresh = fresh_seal(payload.get("_premise"))
        if fresh is None or (admits is not None and not admits(c)):
            return None
        payload = {**payload, "_premise": fresh}
        if "_gap_fill_receipts" in payload:
            payload["_gap_fill_receipts"] = (
                candidate_premise.gap_fill_receipts(fresh))
        return {**c, "payload": payload}

    saved_views.reseal_saved_reviews(fresh_seal)
    for job in job_mgr.registry.pending_and_running():
        with job._lock:
            rows = list(job.candidates)
        if not any(fresh_seal((c.get("payload") or {}).get("_premise"))
                   for c in rows):
            continue
        if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            with job._lock:
                for c in job.candidates:
                    fresh = resealed(c)
                    if fresh is not None:
                        c["payload"] = fresh["payload"]
            # A hard stop returns the run's picks from what is saved.
            job_persistence.persist(job)
            continue

        def _reseal(job=job):
            job.candidates = [resealed(c) or c for c in job.candidates]

        with job_mgr._library_review_state_guard(job):
            job_persistence.persist_review_mutation(job, _reseal)


def _partial_gap_fill_spec(full_album, artist_name, n_missing, *,
                           album_dir=None, exact_edition=True):
    if album_dir is None:
        if exact_edition:
            _existing, album_dir, _missing, _present = catalog.find_edition_tracks(full_album)
        else:
            album_dir = catalog.other_version_dir(full_album)
    if album_dir is None:
        return None
    album_dir = Path(album_dir)
    premise = candidate_premise.capture("gap-fill", album_dir)
    if premise is None:
        return None
    return _album_candidate_spec(
        {**full_album, "_partial_missing_count": n_missing},
        artist_name, selected=False, extra_payload={
            "album_dir": str(album_dir),
            "exact_edition": exact_edition,
            "_premise": premise,
            "_gap_fill_receipts": candidate_premise.gap_fill_receipts(premise),
        })


def _fold_partial_gap_fill(full_album, artist_name, n_missing, *,
                           album_dir=None, exact_edition=True,
                           park_when_absent=False):
    """Return a partial download to Gap Fill, creating a review only if requested."""
    spec = _partial_gap_fill_spec(
        full_album, artist_name, n_missing,
        album_dir=album_dir, exact_edition=exact_edition)
    if spec is None:
        return False
    folded = refold_into_living_review([spec], ticked=False)
    if folded is False:
        return False
    if folded is None:
        if not park_when_absent:
            return True
        return _park_library_failures(
            [spec], ticked=False,
            summary="1 album downloaded only partly: the missing tracks "
                    "are ready as Gap Fill.") is not False
    return True


def prune_library_review_candidates(album):
    """A full album just landed on disk (Search download, batch download,
    upgrade replace): drop its candidates from every parked or still-scanning
    Qobuz album review, so a stale review can't offer to download an album the
    user already has. The executing job itself is RUNNING and untouched.
    Matched by Qobuz album id. Returns the number of candidates dropped."""
    album_id = str((album or {}).get("id") or "")
    if not album_id:
        return 0
    dropped = 0
    states = (job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)
    for job in job_mgr.registry.all():
        if (getattr(job, "execute_kind", "") not in (
                "library", "new_releases", "collection_restore")
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
        if (getattr(job, "execute_kind", "") not in (
                "library", "new_releases", "collection_restore")
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
            existing, _album_dir, missing, _present = catalog.find_edition_tracks(album)
            if existing and not missing:
                owned.add(cid)
        except Exception as exc:
            log.info(f"  couldn't check owned album {album_id}: {exc}")
            continue
    return owned


def _record_last_scan():
    try:
        cfg.LAST_SCAN_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _write_collection_snapshot(job, owned_qobuz, artist_ids, source="scan"):
    """Leave a rebuild record after a completed scan.

    Returns the guard's refusal or the write error so the caller can put it in
    the job summary, or None when the snapshot was written. A snapshot that
    cannot be written never fails the scan: the review the user is waiting for
    is already good.
    """
    try:
        previous = collection_snapshot.load_latest()
    except OSError:
        previous = None
    try:
        document = collection_snapshot.build_snapshot(
            owned_qobuz=owned_qobuz, artist_ids=artist_ids,
            previous=previous, source=source)
        written, refusal = collection_snapshot.write_snapshot(document)
    except OSError as e:
        failure = f"The collection backup couldn't be written: {e}"
        job.push_line(failure)
        return failure
    if not written:
        # With no earlier snapshot there is nothing being protected and nothing
        # to tell the user about; the guard just declines to save an empty one.
        if previous is None:
            return None
        job.push_line(refusal)
        return refusal
    return None


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
            seen = sorted(fingerprints)
            if data.get(mode) == seen:
                return
            data[mode] = seen
            state_file.write_json(cfg.SCAN_SEEN_FILE, data)
    except OSError:
        pass


def _flag_new_since_last_scan(job, mode, found=()):
    """Badge candidates whose album wasn't surfaced by the previous walk, then
    record this walk's set for next time. ``found`` adds what the walk found
    beyond its candidates, such as dismissed albums, so one brought back
    later is not new. First-ever run badges nothing (no baseline to diff).
    Skipped on a cancelled scan so a partial run can't poison the
    baseline."""
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
    for c in found:
        fp = hidden_mod.album_fingerprint(c.get("artist"), c.get("title"))
        if fp:
            seen_now.add(fp)
    prev = _load_scan_seen(mode)
    if prev is not None:
        for c in candidates:
            fp = fps.get(c["cid"])
            if fp and fp not in prev:
                c["payload"]["is_new"] = True
    _save_scan_seen(mode, seen_now)


def dismiss_albums(job, artists, scope=hidden_mod.SCOPE_MISSING, tab="",
                   query=""):
    """Hide the albums of ``artists`` that aren't currently selected, in
    ``scope``, as one action with one save.

    Selection is server-backed (saved as the user ticks), so "hide the rest"
    means: of these artists' candidates, hide the ones whose saved `selected`
    flag is off and keep the ticked ones. Other artists' candidates and their
    selections are never touched, critical now that pagination means most of
    them aren't even on the page that triggered the hide.

    ``tab`` narrows the hide to what one tab of a library review lists
    (review_tab_filter): its Gap Fill rows, or the missing albums of the
    switched-on types. The button only ever shows one tab's rows, so it must
    not silently dismiss the other tab's. ``query`` narrows the same way for
    an active review filter, only the rows the user can see leave.

    The hidden albums are recorded in the durable store so future bulk walks of
    that scope skip them, then dropped from this job's review list. Returns the
    number hidden, False when the matching review snapshot could not be saved,
    or None once the review has left review.
    """
    artists = set(artists)
    in_tab = review_tab_filter(tab)
    live = (job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)
    # Snapshot + mutate under the lock in one go: a live scan appends candidates
    # from the worker thread, so reading job.candidates and replacing it in
    # separate steps could drop a concurrently-added album.
    with job._review_action_lock, job._lock:
        if job.status not in live:
            return None
        # The store's key deliberately ignores parentheticals so editions of one
        # album stay dismissed together, but that also collapses genuinely
        # different records, "The Asylum Albums (1972-1975)" and "(1976-1980)"
        # share a key. Writing an unticked row's key would hide the ticked one
        # it collides with, for good and under the other album's title. A ticked
        # album wins: its twin is left in the review rather than dismissed into
        # a key that cannot tell them apart, so nothing the user kept disappears
        # and the count that leaves matches the count recorded.
        mine = [c for c in job.candidates if c.get("artist") in artists]
        kept = {hidden_mod.album_fingerprint(c.get("artist"), c.get("title"))
                for c in mine if c.get("selected")}
        kept.discard(None)
        to_hide = [c for c in mine
                   if not c.get("selected")
                   and in_tab(c)
                   and (not query or candidate_matches_query(c, query))
                   and hidden_mod.album_fingerprint(
                       c.get("artist"), c.get("title")) not in kept]
        if not to_hide:
            return 0
        specs = [(c.get("artist"), c.get("title"),
                  (c.get("payload") or {}).get("year")) for c in to_hide]
        gap_flags = [is_gap_candidate(c) for c in to_hide]
        album_ids = ([(c.get("payload") or {}).get("album_id") for c in to_hide]
                     if job.execute_kind == "new_releases" else None)
    # Record the dismissals durably FIRST, outside the locks: neither the scan
    # thread's next add_candidate nor a tick waits on this write.
    hidden_before = hidden_mod.load()
    new_specs = [
        spec for spec in specs
        if not hidden_mod.is_hidden_row(scope, *spec, hidden_before)
    ]
    hidden_mod.hide(scope, specs, gap_fill=gap_flags, album_ids=album_ids)
    drop = {c["cid"] for c in to_hide}

    def _drop():
        if job.status not in live:
            return None
        # Re-read the ticks under the lock: a selection saved while the store
        # write above ran was promised "keep the ticked ones", so it wins,
        # dropping the stale snapshot's ids wholesale would dismiss an album
        # the user just ticked.
        ticked_meanwhile = [c for c in job.candidates
                            if c["cid"] in drop and c.get("selected")]
        keep = {c["cid"] for c in ticked_meanwhile}
        # Only these artists' unselected candidates leave; every other
        # candidate (and its saved selection) is preserved untouched.
        job.candidates = [c for c in job.candidates
                          if c["cid"] not in drop or c["cid"] in keep]
        return ticked_meanwhile

    saved, ticked_meanwhile = job_persistence.persist_review_mutation(job, _drop)
    if not saved or ticked_meanwhile is None:
        hidden_mod.restore_rows(scope, new_specs)
        return False if not saved else None
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


def _scan_library_artist(artist_dir, token, partial_only, hidden):
    """Worker: find one artist's gaps. Runs in a pool thread (its own HTTP
    session); returns plain data so the caller adds candidates serially,
    keeping job.candidates single-writer. Also returns the artist's id and its
    lossless catalog ids so the caller can seed the new-release baseline (the
    discography is already fetched here).

    Dismissed albums come back too. The caller keeps them out of the review but
    saves them in the snapshot, so dismissing costs no rescan and Restore has a
    record to bring back.

    The last item is every catalogue id the scan counts as on disk: each
    album it found in a folder and the editions it groups with them."""
    result = find_missing_for_artist(
        artist_dir.name, token=token,
        opts=DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES),
        artist_dir=artist_dir, hidden=hidden,
        single_store=hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None,
        want_missing=not partial_only, include_hidden=True)
    artist_id = str(result.artist_id) if result.artist_id else None
    # None signals "don't seed a baseline" or save the artist: a transient
    # short-page fetch isn't the whole discography, so seeding it would later
    # dump the dropped albums as "new", and a folder Qobuz left unchecked is
    # checked again on the next refresh.
    catalog_ids = None if result.catalog_incomplete else [
        str(a["id"]) for a in result.catalog
        if is_lossless_album(a) and a.get("id") is not None
        and not album_is_upcoming(a)]
    # Folder name to Qobuz id for the albums already on disk. The scan resolves
    # these anyway and then drops them; the collection snapshot keeps them so a
    # restore can ask for the exact album instead of guessing from a search.
    owned_ids = {}
    for entry in list(result.complete) + list(result.singles):
        album = entry.get("qobuz_album") or {}
        folder = entry.get("dir")
        if folder is not None and album.get("id") is not None:
            owned_ids[Path(folder).name] = str(album["id"])
    for gap in result.gaps:
        album = gap.qobuz_album or {}
        if gap.on_disk_dir is not None and album.get("id") is not None:
            owned_ids[Path(gap.on_disk_dir).name] = str(album["id"])
    held = {catalog.edition_group_key(a) for a in result.catalog
            if str(a.get("id")) in result.on_disk}
    held.discard(None)
    on_disk = set(result.on_disk) | {
        str(a["id"]) for a in result.catalog
        if a.get("id") is not None and catalog.edition_group_key(a) in held}
    return (artist_dir.name, result.artist_name, result.gaps, artist_id,
            catalog_ids, owned_ids, on_disk)


# Seconds between live-status refreshes during the whole-library repair sweep
# (see scan_repairs).
_REPAIR_HEARTBEAT_SECS = 2
_REPAIR_CHECKPOINT_VERSION = 3
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
    """Seal the artist tree used by a resumable Repair result."""
    return backup_mod.capture_album_tree_identity(Path(artist_dir))


def _repair_checkpoint_bundle(name, agg, proof):
    return {
        "version": _REPAIR_CHECKPOINT_VERSION,
        "scan_signature": _repair_checkpoint_signature(),
        "proof": proof,
        "candidates": list(agg.get("specs") or []),
        "counts": {
            "verified_ok": int(agg.get("verified_ok") or 0),
            "unverified": int(agg.get("unverified") or 0),
            "unmatched": int(agg.get("unmatched") or 0),
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
            "verified_ok", "unverified", "unmatched", "failed_albums",
        }
        or any(
            type(value["counts"].get(key)) is not int
            or value["counts"][key] < 0
            for key in ("verified_ok", "unverified", "unmatched",
                        "failed_albums")
        )
        # Failed albums were not fully checked. Retry their artist instead of
        # reusing an incomplete result in a later clean scan.
        or value["counts"]["failed_albums"]
    ):
        return None
    proof = backup_mod.canonical_album_tree_identity(
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
    current = backup_mod.canonical_album_tree_identity(
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


def _finished_pass(cp, name):
    """A pass an interrupted scan finished, shaped as the saved state its
    refresh would otherwise reuse, or None."""
    result = ((cp or {}).get("passes") or {}).get(name)
    if (
        not isinstance(result, dict)
        or result.get("complete") is not True
        or not isinstance(result.get("fingerprints"), dict)
        or not isinstance(result.get("candidates"), list)
        or not all(isinstance(c, dict) for c in result["candidates"])
    ):
        return None
    return result


# The settings a Library refresh's results are computed under.
_REFRESH_SETTINGS = ("STREAMRIP_QUALITY", "PREFER_HIRES", "SUPPRESS_SINGLE_TRACK_GAPS",
                     "DOWNSAMPLE_HIRES_ENABLED")
SETTINGS_WAIT = "Waiting to apply the saved settings"


def _apply_saved_refresh_settings(job):
    """Wait for any job running beside this refresh to finish, when it holds
    back a change to the settings the refresh's results follow, and apply
    the change. False when the refresh was stopped meanwhile."""
    while settings_store.waiting(_REFRESH_SETTINGS):
        with job_mgr.settings_admission_guard():
            others = [j for j in job_mgr.registry.executing() if j is not job]
            if not others:
                settings_store.drain_pending()
                return True
        job.push_progress(SETTINGS_WAIT,
                          item=f"waiting for {others[0].title} to finish…")
        if job.cancel_requested:
            return False
        time.sleep(1)
    return True


def scan_library(job, token, partial_only=False, force_full=False):
    start_skip_record()
    if not _apply_saved_refresh_settings(job):
        job.summary = "Stopped before anything turned up."
        return
    # Albums downloaded before this scan reads the library leave the parked
    # review now. Left for the next page load, they would also take away
    # what the scan finds for them again, such as the Gap Fill a one-track
    # download leaves.
    apply_pending_review_removals()
    previous_generation = generation_state.load()
    previous_attempt_status = str(
        (previous_generation.get("latest_attempt") or {}).get("status")
        or "never"
    )
    allow_checkpoint_resume = not force_full and bool(
        partial_only
        or previous_attempt_status in {"running", "failed", "incomplete"}
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
        with scan_checkpoint.Writer(
                "partial" if partial_only else "missing",
                meta={"quality_signature":
                      library_scan_state.quality_signature()},
        ) as checkpoint:
            return _scan_library_impl(
                job,
                token,
                checkpoint=checkpoint,
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
    checkpoint,
    partial_only=False,
    force_full=False,
    attempt_id=None,
    scan_started_revision=0,
    allow_checkpoint_resume=True,
):
    clear_scan_caches()
    unreadable_artists = set()
    local_discovery_errors = {}

    def artist_read_failed(artist_dir, error):
        local_discovery_errors[artist_dir.name] = str(error)
        if normalize(artist_dir.name) not in VA_NORMALIZED:
            unreadable_artists.add(artist_dir.name)

    all_artists = list_library_artists(on_artist_error=artist_read_failed)
    # Compilations have no single artist catalogue to compare.
    artists = [d for d in all_artists
               if normalize(d.name) not in VA_NORMALIZED]
    discovery_errors = len(unreadable_artists)
    if not artists and not discovery_errors:
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
    quality_sig = library_scan_state.quality_signature()
    if cp is not None and cp["meta"].get(
            "quality_signature", quality_sig) != quality_sig:
        # Its artists were checked under other quality or release settings.
        log.info("The interrupted scan ran under other settings, so it "
                 "starts again.")
        cp = None
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
            if (
                not isinstance(saved, dict)
                or not isinstance(saved.get("catalog_ids"), list)
            ):
                continue
            scanned.add(name)
            artist_id = saved.get("artist_id") or ""
            if artist_id:
                baseline_seen[str(artist_id)] = [
                    str(album_id) for album_id in saved["catalog_ids"]
                    if album_id is not None
                ]
    else:
        scanned = set()
        baseline_seen = {}
    total = 0
    # Artist folders whose finds the review had no room for, with how many
    # each lost. They are not saved as scanned, so a later refresh checks
    # them again.
    left_out = {}
    # Snapshot the dismissed-album memory before restoring the checkpoint so
    # albums the user dismissed since the interruption are not re-added, and
    # so the parallel workers below see the same consistent view.
    hidden = hidden_mod.load()
    # The review keeps every release type and bonus version and lists what
    # the switches pick; the running count follows the list.
    _listed = review_listed()
    # Dismissing and restoring deliberately do NOT bar the cheap path. The
    # snapshot holds every candidate the last crawl found, dismissed or not, so
    # both directions are a filter over saved data, not a reason to ask Qobuz
    # about the whole library again.
    def _reusable(snapshot):
        return bool(
            not force_full
            and not resuming
            and snapshot.get("complete")
            # Saved candidates computed under a different quality policy must
            # not carry forward, a settings change re-derives even unchanged
            # folders.
            and snapshot.get("quality_signature", "") == quality_sig
        )

    previous_scan = library_scan_state.kind_summary(kind)
    cheap_refresh = _reusable(previous_scan)
    if cheap_refresh:
        # Only the cheap path reuses the saved artists; a full rescan would
        # hold them in memory for its whole run for nothing.
        previous_scan = library_scan_state.kind_state(kind)
        cheap_refresh = _reusable(previous_scan)
    if not resuming and not cheap_refresh:
        # A refresh needs no starting checkpoint: interrupted before it
        # saves progress, the next one reuses the saved scan again.
        checkpoint.save(scanned, baseline_seen)
    # The two refreshes and the fingerprint pass below run before the main
    # artist loop, and on a first scan of a large library each takes real
    # minutes, without progress ticks the job sits on "Waiting for output"
    # looking hung the whole time.
    # The upgrade pass is optional, so number the steps from the ones that
    # will actually run rather than hard-coding four.
    _passes = ["Reading albums on disk"]
    if cfg.UPGRADE_SCAN_ENABLED:
        _passes.append("Checking upgrade quality")
    _finding = ("Finding Gap Fill candidates" if partial_only
                else "Finding missing albums")
    _passes += ["Checking artist folders", _finding]

    def _step(name):
        return f"Step {_passes.index(name) + 1} of {len(_passes)}: {name}"

    downsample_refresh_started_at = time.time()
    log.info(f"Reading albums from {plural(len(all_artists), 'artist folder')} on disk…")
    downsample_refresh = downsample_state.refresh_for_artists(
        all_artists,
        hidden=hidden,
        discovery_errors=local_discovery_errors,
        cancel_check=lambda: bool(job.cancel_requested),
        persist=False,
        skip_unchanged=cheap_refresh,
        on_artist=lambda ad, _specs, _err, done_i, total_i: job.push_progress(
            _step("Reading albums on disk"),
            done_i, total_i, ad.name, unit="artist"),
    )
    upgrade_refresh = None
    upgrade_refresh_started_at = None
    offered_before = _offered_upgrades()
    if not job.cancel_requested and cfg.UPGRADE_SCAN_ENABLED:
        upgrade_refresh_started_at = time.time()
        # A resume carries over the upgrade pass the interrupted scan finished.
        # Folders whose fingerprint changed since are checked again.
        finished_upgrade = _finished_pass(cp, "upgrade") if resuming else None
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
            skip_unchanged=cheap_refresh or finished_upgrade is not None,
            previous=finished_upgrade,
            persist=False,
            known_fingerprints=downsample_refresh.fingerprints,
            on_artist=lambda ad, _specs, _err, done_i, total_i: job.push_progress(
                _step("Checking upgrade quality"),
                done_i, total_i, ad.name, unit="artist"),
        )
        if upgrade_refresh.complete and not job.cancel_requested:
            checkpoint.keep_pass(
                "upgrade", upgrade_state.reusable_result(upgrade_refresh))
    elif not cfg.UPGRADE_SCAN_ENABLED:
        review_badges.set_ready("upgrade", False)
    target = ("Gap Fill candidates in owned albums" if partial_only
              else "missing albums and Gap Fill candidates")
    log.info(f"Scanning {plural(len(artists), 'library artist')} for {target}")
    fingerprints = {}
    for _i, _ad in enumerate(artists, 1):
        if job.cancel_requested:
            break
        try:
            fingerprints[_ad.name] = (
                downsample_refresh.fingerprints.get(_ad.name)
                or artist_fingerprint(_ad))
        except OSError as exc:
            artist_read_failed(_ad, exc)
            log.warning(f"Unreadable artist {_ad.name}: {exc}. Check permissions and retry.")
        if _i % 25 == 0 or _i == len(artists):
            job.push_progress(_step("Checking artist folders"),
                              _i, len(artists), _ad.name, unit="folder")
    artists = ([] if job.cancel_requested
               else [ad for ad in artists if ad.name in fingerprints])
    discovery_errors = len(unreadable_artists)
    if resuming:
        # A checkpoint says what was compared, not that the local folder is
        # still the same. Reuse only a current matching per-artist fingerprint;
        # removed and legacy entries fall away independently.
        scanned = {
            name
            for name in scanned
            if name in fingerprints
            and isinstance(checkpoint_artists.get(name), dict)
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
        # Older checkpoints also list every album found outside the artists.
        restored_by_artist: dict[str, list[dict]] = {}
        for c in cp["candidates"]:
            artist_key = (c.get("payload") or {}).get("_artist_dir") or c.get("artist")
            restored_by_artist.setdefault(artist_key, []).append(c)
        for name, saved in checkpoint_artists.items():
            if name not in scanned:
                continue
            candidates = (list(saved.get("candidates") or [])
                          or restored_by_artist.get(name, []))
            dropped = 0
            for c in candidates:
                if _spec_dismissed(c, hidden):
                    continue
                listed = _listed(c)
                if _readd_candidate(job, c, listed=listed) is None:
                    dropped += 1
                elif listed:
                    total += 1
            if dropped:
                left_out[name] = dropped
                continue
            state_artists[name] = {
                "fingerprint": saved.get("fingerprint") or fingerprints.get(name, ""),
                "candidates": candidates,
                "artist_id": saved.get("artist_id") or "",
                "catalog_ids": list(saved["catalog_ids"]),
                "owned_ids": dict(saved.get("owned_ids") or {}),
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
    scan_errors = discovery_errors
    read_errors = 0
    # Ids for the collection snapshot written when the scan finishes. An
    # artist reused from saved state brings the ids saved with it; one saved
    # before they were kept is carried forward from the last snapshot.
    owned_qobuz = {}
    snapshot_artist_ids = {}
    on_disk_ids = set()

    def _reuse_owned(name, entry):
        if entry.get("owned_ids"):
            owned_qobuz[name] = dict(entry["owned_ids"])
        if entry.get("artist_id"):
            snapshot_artist_ids[name] = str(entry["artist_id"])

    for name, entry in state_artists.items():
        _reuse_owned(name, entry)
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
            and not _read_without_release_types(saved.get("candidates") or [])
        ):
            candidates = list(saved.get("candidates") or [])
            shown = dropped = 0
            for c in candidates:
                if _spec_dismissed(c, hidden):
                    continue
                listed = _listed(c)
                if _readd_candidate(job, c, listed=listed) is None:
                    dropped += 1
                elif listed:
                    shown += 1
            total += shown
            scanned.add(artist_dir.name)
            done += 1
            reused += 1
            if saved.get("artist_id"):
                baseline_seen[str(saved["artist_id"])] = list(saved_catalog_ids or [])
            if dropped:
                left_out[artist_dir.name] = dropped
            else:
                state_artists[artist_dir.name] = {
                    "fingerprint": fingerprints.get(artist_dir.name, ""),
                    "candidates": candidates,
                    "artist_id": saved.get("artist_id") or "",
                    "catalog_ids": list(saved_catalog_ids or []),
                    "owned_ids": dict(saved.get("owned_ids") or {}),
                }
                _reuse_owned(artist_dir.name, state_artists[artist_dir.name])
            hit = ({"artist": artist_dir.name, "albums": shown}
                   if shown else None)
            job.push_progress(_step(_finding), done, n, artist_dir.name,
                              found=total, hit=hit, unit="artist")
        else:
            todo.append(artist_dir)
    if reused:
        log.info(f"  Reused {plural(reused, 'unchanged artist')} from the saved scan.")
    if reused and todo:
        # Before any artist is checked, so an interrupted refresh resumes
        # without asking Qobuz about the artists it reused. With nothing to
        # check there is nothing to resume.
        checkpoint.save(scanned, baseline_seen, state_artists)
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Resolve/scan artists in parallel (each worker has its own HTTP session),
    # but collect results and write candidates on this one thread so the
    # candidate list and progress stay single-writer.
    missing_premises = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="libscan",
                            **job_mgr.pool_initializer_kwargs()) as ex:
        futures = {ex.submit(_scan_library_artist, ad, token, partial_only,
                             hidden): ad
                   for ad in todo}
        for fut in as_completed(list(futures)):
            # A finished artist's result, with every album Qobuz sent for it,
            # is let go once read rather than kept to the end of the scan.
            ad = futures.pop(fut)
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                checkpoint.flush()
                log.info("Cancelled. Stopping scan.")
                break
            done += 1
            try:
                (name, artist_name, gaps, artist_id, catalog_ids,
                 owned_ids, on_disk) = fut.result()
            except (AuthLost, QobuzUnavailable) as e:
                # A lost token or an unreachable API isn't a per-artist
                # hiccup, so cancel the rest and fail the scan rather than
                # silently report a partial library as the full picture.
                for f in futures:
                    f.cancel()
                checkpoint.flush()
                if isinstance(e, QobuzUnavailable):
                    name = ad.name
                    log.info(f"    {name}: {e}")
                    raise QobuzUnavailable(
                        f"Qobuz didn't answer for {name}, so the scan "
                        "stopped there. The next scan picks up where it "
                        "left off.") from e
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                scan_errors += 1
                if isinstance(e, OSError):
                    read_errors += 1
                    unreadable_artists.add(ad.name)
                log.info(f"    skipped {ad.name}: {e}")
                job.push_progress(_step(_finding), done, n, ad.name,
                                  found=total, unit="artist")
                continue
            scanned.add(name)
            on_disk_ids.update(on_disk)
            if owned_ids:
                owned_qobuz[name] = owned_ids
            if artist_id:
                snapshot_artist_ids[name] = artist_id
            if artist_id and catalog_ids is not None:
                baseline_seen[artist_id] = catalog_ids
            artist_candidates = []
            shown = dropped = 0
            for gap in gaps:
                # Library is a discovery list, leave candidates unticked so a
                # single click can't queue hundreds nobody reviewed.
                spec = _gap_candidate_spec(
                    gap, artist_name or name, selected=False, artist_key=name,
                    missing_premises=missing_premises)
                if _spec_dismissed(spec, hidden):
                    # Saved but not reviewed: the snapshot is the record
                    # Restore brings back from.
                    artist_candidates.append(spec)
                    continue
                listed = _listed(spec)
                if _add_candidate_spec(job, spec, listed=listed) is None:
                    dropped += 1
                    continue
                artist_candidates.append(spec)
                if listed:
                    total += 1
                    shown += 1
            if catalog_ids is not None and dropped:
                left_out[name] = dropped
            elif catalog_ids is not None:
                state_artists[name] = {
                    "fingerprint": fingerprints.get(name, ""),
                    "candidates": artist_candidates,
                    "artist_id": artist_id or "",
                    "catalog_ids": list(catalog_ids or []),
                    "owned_ids": dict(owned_ids or {}),
                }
            # Add the albums before the progress tick so a hit lands the live
            # preview the same moment the running total moves.
            hit = ({"artist": artist_name or name, "albums": shown}
                   if shown else None)
            job.push_progress(_step(_finding), done, n, artist_name or name,
                              found=total, hit=hit, unit="artist")
            if shown:
                # A full scan's count holds missing albums and gaps together.
                tail = "with Gap Fill candidates" if partial_only else "to review"
                log.info(f"  {artist_name} - {plural(shown, 'album')} {tail}")
            checkpoint.save(scanned, baseline_seen, state_artists)
    # Reached here only without an AuthLost/outage abort (that re-raises out
    # above, leaving the checkpoint for resume and not seeding the baseline).
    # job.candidates now holds everything this scan offers for these artists.
    # A left-out artist's rows are missing from this scan for want of room,
    # which says nothing about what it offers.
    job.scan_coverage = (frozenset(scanned - set(left_out)), partial_only,
                         frozenset(on_disk_ids))
    job.scan_left_out = dict(left_out)
    flush_resolve_cache()
    baseline_save_failed = False
    scan_state_save_failed = False
    scan_state_changed = False
    snapshot_refused = None
    downsample_save_failed = False
    upgrade_save_failed = False
    catalog_complete = False
    publication = None
    if job.stopping_for_restart:
        # Not a deliberate stop: the progress stays for the next start, as it
        # would after a crash.
        pass
    elif job.cancel_requested:
        # Deliberate stop, discard this kind's progress so it isn't auto-resumed.
        if not checkpoint.clear():
            job.push_line(
                "The stopped scan checkpoint could not be cleared. It will be "
                "ignored unless its artist proofs still match."
            )
        if not partial_only:
            generation_state.finish_attempt(attempt_id, "cancelled")
    else:
        # A crawl that reached every artist it could read stamps "last
        # scanned" and seeds the new-release baseline. A folder that cannot be
        # read is left out and recorded; it has no saved state, so the next
        # refresh after it becomes readable checks it like a new artist.
        readable_names = {ad.name for ad in artists} - unreadable_artists
        catalog_complete = (
            bool(readable_names)
            and readable_names <= set(state_artists) | set(left_out)
            and readable_names <= scanned
            and scan_errors - discovery_errors - read_errors == 0
        )
        if catalog_complete and unreadable_artists and not partial_only:
            downsample_refresh = _without_unreadable(
                downsample_refresh, unreadable_artists)
            upgrade_refresh = _without_unreadable(
                upgrade_refresh, unreadable_artists)
        if catalog_complete and kind == "missing":
            publication = generation_state.commit_catalog_generation(
                attempt_id,
                limited=job.candidate_cap_hit,
                expected_revision=scan_started_revision,
            )
            scan_state_save_failed = publication is None
            if publication is None:
                scan_state_changed = (
                    generation_state.revision() != scan_started_revision)
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
            changed_albums = publication.get("changed_albums") or []
            if changed_albums:
                state_artists = library_scan_state.without_albums(
                    state_artists, changed_albums)
                for album_id in changed_albums:
                    prune_library_review_candidates({"id": album_id})
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
                for album_id in generation_state.album_changes_since(
                        publication["revision"]) or ():
                    library_scan_state.remove_album(album_id)
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
                    saved_views.sync_badge("downsample")
            if upgrade_refresh is not None and upgrade_refresh.complete:
                upgrade_save_failed = not upgrade_state.save(
                    upgrade_refresh,
                    preserve_concurrent=True,
                    refresh_started_at=upgrade_refresh_started_at,
                    refresh_started_revision=scan_started_revision,
                    generation=scan_generation,
                )
                if not upgrade_save_failed:
                    saved_views.sync_badge("upgrade")
                    job.upgrade_finds = len(_offered_upgrades() - offered_before)
                    job.upgrade_finds_first = not offered_before
            elif upgrade_refresh is None:
                generation_state.mark_output_status(
                    "upgrade",
                    "disabled",
                    generation=scan_generation,
                    reason="Upgrade scanning is disabled.",
                )
        if publication is not None and not scan_state_save_failed:
            scan_generation = int(publication["generation"])
            if not scan_state_save_failed and not cheap_refresh:
                _record_last_scan()
            # What's on disk counts the tag cache's rows, so drop the rows of
            # files moved or deleted outside the app since they were read.
            flac_cache.prune_missing(force=True)
            _flag_new_since_last_scan(job, kind, found=[
                c for entry in state_artists.values()
                for c in entry.get("candidates") or []])
            # The crawl reached every artist cleanly, establish the new-release
            # baseline from the catalog snapshot and clear this kind's
            # checkpoint. A crawl that read every catalogue counts as the last
            # new-release check; a refresh reuses most of them.
            baseline_save_failed = not new_releases_mod.seed_baseline(
                baseline_seen,
                generation=scan_generation,
                checked_at=None if cheap_refresh else time.time(),
            )
            if not checkpoint.clear():
                job.push_line(
                    "The completed scan checkpoint could not be cleared. Its "
                    "saved generation is complete, so it will not be treated "
                    "as newer results."
                )
            snapshot_refused = _write_collection_snapshot(
                job, owned_qobuz, snapshot_artist_ids)
        elif catalog_complete and scan_state_save_failed:
            # Keep a complete resumable copy when the main review could not be
            # saved.
            checkpoint.save(scanned, baseline_seen, state_artists)
        elif catalog_complete and partial_only:
            checkpoint.clear()
        elif (scanned or job.candidates or baseline_seen
                or unreadable_artists):
            checkpoint.save(scanned, baseline_seen, state_artists)
    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    elif total:
        job.summary = library_review_summary(job.candidates) + "."
        if left_out:
            job.summary += " " + left_out_note(left_out, job.CANDIDATE_CAP)
    elif partial_only:
        job.summary = "No owned albums are missing tracks."
    else:
        job.summary = "No missing albums found for artists in your library."
    if getattr(job, "upgrade_finds", 0) and not job.cancel_requested:
        job.summary += " " + upgrade_finds_note(
            job.upgrade_finds, first=getattr(job, "upgrade_finds_first", False))
    # Artists that errored or came back with a short catalog page aren't in
    # state_artists; the checkpoint stays for them and the last-scan stamp is
    # withheld.
    unchecked = len(artists) - len(state_artists) - len(left_out) + discovery_errors
    if not job.cancel_requested and not partial_only and publication is not None:
        unreadable_artists_mod.record(unreadable_artists)
    if (not job.cancel_requested and publication is not None
            and unchecked == len(unreadable_artists) and unreadable_artists):
        # Finished for everything readable: the rest is not a resume.
        names = ", ".join(sorted(unreadable_artists, key=str.casefold)[:5])
        more = (f" (+{len(unreadable_artists) - 5} more)"
                if len(unreadable_artists) > 5 else "")
        job.summary += (
            f" {plural(len(unreadable_artists), 'artist folder')} couldn't be "
            f"read and {'was' if len(unreadable_artists) == 1 else 'were'} "
            f"left out: {names}{more}. "
            + ("Each is checked by itself once it can be read."
               if cfg.AUTO_LIBRARY_SCAN
               else "Scan again once it can be read."
               if len(unreadable_artists) == 1
               else "Scan again once they can be read."))
    if (not job.cancel_requested and unchecked > 0
            and publication is not None
            and unchecked == len(unreadable_artists)):
        # Left out rather than unchecked: nothing here is waiting to resume.
        _record_unchecked_artists(job, 0)
    elif not job.cancel_requested and unchecked > 0:
        _record_unchecked_artists(job, unchecked)
        job.summary += f" {plural(unchecked, 'artist')} couldn't be checked"
        if unreadable_artists:
            names = ", ".join(sorted(unreadable_artists, key=str.casefold)[:5])
            more = (f" (+{len(unreadable_artists) - 5} more)"
                    if len(unreadable_artists) > 5 else "")
            job.summary += f" ({len(unreadable_artists)} unreadable: {names}{more})"
        job.summary += "; scan again to resume from where it left off."
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
    caveats = []
    if not job.cancel_requested and stale_tabs:
        caveats.append(
            f"{' and '.join(stale_tabs)} results were not updated because "
            "this scan did not finish cleanly."
        )
    if not job.cancel_requested and (skipped := undecodable_named()):
        caveats.append(undecodable_note(skipped))
    if not job.cancel_requested and not partial_only and (
            no_artist := new_releases_mod.unresolved_note(
                [name for name, entry in state_artists.items()
                 if not entry.get("artist_id")])):
        caveats.append(no_artist)
    if baseline_save_failed:
        caveats.append("New releases could not be updated; the next "
                       "complete scan tries again.")
    if snapshot_refused:
        caveats.append(snapshot_refused)
    if scan_state_changed:
        caveats.append("Albums changed while the refresh ran, so its "
                       "results were not saved; refresh again to save them.")
    elif scan_state_save_failed:
        caveats.append("The saved scan state couldn't be written; scan again "
                       "before restarting the app.")
    # A refresh folded into an open review keeps these after its counts.
    job.scan_caveats = caveats
    if caveats:
        job.summary += " " + " ".join(caveats)
    # The crawl is stamped complete before its saved views are written, so a
    # view that never landed left the attempt reading "complete" and the page
    # showing a clean review. Correct the record with what is missing, which is
    # what puts the warning on Library at all.
    unsaved = list(stale_tabs)
    if baseline_save_failed:
        unsaved.append("New releases")
    if (unsaved and attempt_id is not None and publication is not None
            and not scan_state_save_failed and not job.cancel_requested):
        named = (" and ".join(unsaved) if len(unsaved) < 3
                 else ", ".join(unsaved[:-1]) + " and " + unsaved[-1])
        generation_state.finish_attempt(
            attempt_id, "incomplete", f"{named} results were not updated.")
    log.info(job.summary)


def _offered_upgrades():
    return {((c.get("payload") or {}).get("album_id"),
             (c.get("payload") or {}).get("album_dir"))
            for c in upgrade_state.visible_candidates()}


def undecodable_note(paths):
    one = len(paths) == 1
    names = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more"
                                    if len(paths) > 3 else "")
    return (f"Skipped {names}: {'its name is' if one else 'their names are'} "
            f"not valid UTF-8, so {'it reads' if one else 'they read'} as "
            f"missing. Rename {'it' if one else 'them'} so scans include "
            f"{'it' if one else 'them'}.")


def upgrade_finds_note(n, first=False):
    return f"{plural(n, 'album' if first else 'new album')} can be upgraded."


def _parked_new_release_review(exclude=None):
    """The newest new-release review still waiting to be worked through, if any.
    ``exclude`` skips the check currently running, which is not parked yet."""
    parked = None
    for j in job_mgr.registry.awaiting_review():
        if j.execute_kind != "new_releases":
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
            f" {added:,} added to the New releases review already waiting, "
            f"now {plural(waiting, 'new release')}.")
        job_mgr._start_post_job_hook_for_job(parked)
    else:
        job.summary += " All were already in the New releases review."
    parked.notify_review_changed()
    job.status = job_mgr.JobStatus.DONE
    return True


_NEW_RELEASE_LATE_LISTING = 14 * 86400


def scan_new_releases(job, token):
    """Surface albums that appeared in library artists' Qobuz catalogs since the
    last check and that the user doesn't own or hasn't hidden, flagged as new
    for review, but left un-ticked so one click can't queue the whole list.
    Cheap (about one call per artist, no track fetches), so it's the quick
    "what's new" pass rather than the full gap scan."""
    clear_scan_caches()
    unreadable_artists = {}

    def artist_read_failed(artist_dir, error):
        if normalize(artist_dir.name) not in VA_NORMALIZED:
            unreadable_artists[artist_dir.name] = str(error)

    # Same VA exclusion as scan_library: the Various-Artists folder has no single
    # Qobuz catalog, so it can't yield meaningful "new releases".
    artists = [d for d in list_library_artists(on_artist_error=artist_read_failed)
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists and not unreadable_artists:
        _set_empty_library_summary(job)
        return
    if not artists:
        names = sorted(unreadable_artists, key=str.casefold)
        job.summary = (
            f"No artist folder could be read ({', '.join(names[:5])}"
            + (f" (+{len(names) - 5} more)" if len(names) > 5 else "")
            + "), so nothing was checked.")
        _mark_job_failed(job)
        return
    state = new_releases_mod.load()
    seen = state.get("seen") or {}
    window = int(cfg.NEW_RELEASE_MAX_AGE_DAYS)
    capped_ids = frozenset(
        artist_id for artist_id, days in (state.get("capped_recent") or {}).items()
        if days >= window)
    # A big artist's unseen release counts as new from the last check on,
    # with slack for a release Qobuz lists some days after its date.
    checked_at = state.get("checked_at") or (
        time.time() - max(0, int(cfg.NEW_RELEASE_CHECK_INTERVAL)))
    new_since = checked_at - _NEW_RELEASE_LATE_LISTING
    # If the catalog fetch limit has grown since the baseline was captured,
    # the old baseline is missing everything past the previous cap, a plain
    # diff would dump that whole back-slice as "new".
    cur_limit = int(cfg.ARTIST_CATALOG_LIMIT)
    prev_limit = state.get("baseline_limit")
    rebaseline = prev_limit is None or cur_limit > int(prev_limit)
    hidden = hidden_mod.load()
    single_store = hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None
    # A new release shows only where the Missing Albums switches would list it.
    switched_on = missing_types.load()
    opts = DiscoveryOpts(
        prefer_hires=cfg.PREFER_HIRES,
        include_live=missing_types.LIVE in switched_on,
        include_comps=missing_types.COMPILATION in switched_on,
        include_singles=missing_types.EP in switched_on,
    )
    log.info(f"Checking {plural(len(artists), 'artist')} for new releases…")
    total = 0
    done = 0
    # Unreadable folders are left out, as a Library scan leaves them: checking
    # again can't read them, so they don't make the check fail.
    failed_count = 0
    failed_names = []
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
    capped_recent = {}
    missing_premises = {}
    listed_ids = set()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="newrel",
                            **job_mgr.pool_initializer_kwargs()) as ex:
        futures = {ex.submit(find_new_releases_for_artist, ad.name, token=token,
                             opts=opts, seen_by_id=seen, hidden=hidden,
                             single_store=single_store, artist_dir=ad,
                             baseline_only=rebaseline,
                             capped_ids=capped_ids,
                             new_since=new_since): ad
                   for ad in artists}
        for fut in as_completed(list(futures)):
            ad = futures.pop(fut)
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
                failed_names.append(ad.name)
                log.info(f"    skipped {ad.name}: {e}")
                job.push_progress("Checking for new releases", done, n,
                                  ad.name, found=total, unit="artist")
                continue
            if result.artist_id and not getattr(result, "fetch_failed", False):
                current_seen[result.artist_id] = result.current_ids
                if getattr(result, "capped_recent", False):
                    capped_recent[result.artist_id] = window
            elif getattr(result, "unresolved", False):
                unresolved_names.append(ad.name)
            elif not getattr(result, "by_lead", False):
                # A short or failed catalogue fetch is unchecked, not a clean
                # zero-release result.
                failed_count += 1
                failed_names.append(ad.name)
            new_gaps = []
            for gap in result.new_gaps:
                # Two folders of one Qobuz artist both see its new release.
                album_id = str(gap.qobuz_album.get("id") or "")
                if album_id and album_id in listed_ids:
                    continue
                listed_ids.add(album_id)
                new_gaps.append(gap)
            for gap in new_gaps:
                # Leave new releases UN-ticked, like the library gap list: a
                # review is for picking, and one tap must never queue the whole
                # list of rips. The "new" badge still flags them for the eye.
                _add_gap_candidate(
                    job,
                    gap,
                    result.artist_name,
                    selected=False,
                    is_new=True,
                    artist_key=ad.name,
                    missing_premises=missing_premises,
                )
                total += 1
            hit = ({"artist": result.artist_name, "albums": len(new_gaps)}
                   if new_gaps else None)
            job.push_progress("Checking for new releases", done, n,
                              result.artist_name or ad.name,
                              found=total, hit=hit, unit="artist")
            if new_gaps:
                log.info(f"  {result.artist_name} - "
                         f"{plural(len(new_gaps), 'new release')}")
    flush_resolve_cache()
    saved = None
    if not job.cancel_requested:
        # UNION each reached artist's snapshot into the prior baseline rather
        # than replacing it.
        complete = failed_count == 0 and bool(current_seen)
        saved = new_releases_mod.mark_run(
            current_seen,
            started_from=seen,
            complete=complete,
            baseline_limit=cur_limit if complete else None,
            capped_recent=capped_recent,
        )
    if failed_count:
        _record_unchecked_artists(job, failed_count)
    shown = ", ".join(sorted(failed_names, key=str.casefold)[:3])
    if len(failed_names) > 3:
        shown += f" and {len(failed_names) - 3:,} more"
    unchecked = plural(failed_count, "artist") + (f" ({shown})" if shown else "")
    # Not logged on its own: every summary below carries it, and the summary
    # is the last line of the log.
    skipped_note = new_releases_mod.unresolved_note(unresolved_names)
    if unreadable_artists:
        names = sorted(unreadable_artists, key=str.casefold)
        shown = ", ".join(names[:5]) + (
            f" (+{len(names) - 5} more)" if len(names) > 5 else "")
        skipped_note = " ".join(filter(None, [
            skipped_note,
            f"{plural(len(names), 'artist folder')} couldn't be read and "
            f"{'was' if len(names) == 1 else 'were'} left out: {shown}."]))
    if job.cancel_requested:
        # A cancelled crawl only reached a fraction of the artists, so it can't
        # claim "No new releases" or "First check recorded" definitively.
        job.summary = ("Stopped early. Partial check, "
                       f"{plural(total, 'new release')} found so far.")
        if failed_count:
            job.summary += (
                f" {unchecked} couldn't be checked "
                "before the stop."
            )
        if skipped_note:
            job.summary += f" {skipped_note}"
        log.info(job.summary)
        return

    if rebaseline and seen:
        if saved is False:
            job.summary = ("What Qobuz has now couldn't be saved. Check "
                           "again to retry it.")
            if failed_count:
                job.summary += (f" {unchecked} also "
                                "couldn't be checked.")
        elif failed_count:
            job.summary = (f"{unchecked} couldn't be "
                           "checked; check again to retry them.")
        else:
            job.summary = ("The catalogue limit changed, so this check "
                           "recorded what Qobuz has now. Later checks list "
                           "what is new.")
    elif total:
        job.summary = f"{plural(total, 'new release')} found."
        if failed_count:
            job.summary += (f" {unchecked} couldn't be "
                            "checked; check again for a complete result.")
        if saved is False:
            job.summary += (" This check couldn't be saved, so these "
                            "releases may appear again.")
    elif failed_count:
        job.summary = ("No new releases from the artists that could be checked. "
                       f"{unchecked} couldn't be checked; "
                       "check again for a complete result.")
        if saved is False:
            job.summary += " This check couldn't be saved."
    elif not seen:
        if saved is False:
            job.summary = ("The current Qobuz catalogue was checked but "
                           "couldn't be saved. Check again.")
        else:
            job.summary = ("First check complete. Later checks list albums "
                           "added to Qobuz after it.")
    elif saved is False:
        job.summary = ("No new releases found, but this check couldn't be "
                       "saved. Check again.")
    else:
        job.summary = "No new releases since the last Library scan or check."
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


def _note_staging_wait(job, phase, current, total):
    """If a long staging-lock holder (a library-wide Lyrics scan) owns the mutex
    right now, show that this job is waiting behind it. Without this the album
    sits on RUNNING with no visible reason until the holder releases the lock;
    the note is replaced by real progress the moment the lock is acquired."""
    holder = job_mgr.staging_holder()
    if holder:
        job.push_progress(phase, current, total,
                          f"waiting for {holder} to finish…", unit="album")


_WRITTEN_INTO = "_written_into"
_IMPORTED_INTO = "_imported_into"


def _note_written_into(job, cand):
    """Save the folder a pick is about to write into before it does, so a
    hard stop can tell the run's own changes from someone else's."""
    folder = candidate_premise.expected_path(cand)
    with job._lock:
        written = (job.execute_args or {}).get(_WRITTEN_INTO) or []
        if not folder or folder in written:
            return
        job.execute_args = {**(job.execute_args or {}),
                            _WRITTEN_INTO: [*written, folder]}
    job_persistence.persist(job)


def _start_album_count(job):
    with job._lock:
        job.execute_args = {
            key: value for key, value in (job.execute_args or {}).items()
            if key != job_persistence.ALBUMS_DONE}


def _note_album_done(job):
    """Count an album the run has finished and save it, so a hard stop can
    say what was already done."""
    with job._lock:
        args = job.execute_args or {}
        job.execute_args = {
            **args,
            job_persistence.ALBUMS_DONE:
                (args.get(job_persistence.ALBUMS_DONE) or 0) + 1}
    job_persistence.persist(job)


def _settle_pick(job, cand, folder=None):
    """Untick a pick the run has imported, so a hard stop does not put it
    back in the review, and save the folder it was imported into."""
    with job._lock:
        cand["selected"] = False
        if folder:
            imported = (job.execute_args or {}).get(_IMPORTED_INTO) or []
            job.execute_args = {**(job.execute_args or {}),
                                _IMPORTED_INTO: [*imported, str(folder)]}
    job_persistence.persist(job)


def _resealed_past_own_imports(job, picks):
    """``picks`` with each missing album whose artist folder changed only by
    the new folders this run imported albums into sealed again, so Start
    does not refuse it for the run's own downloads. One that a folder named
    for it, or the files a check before its download would find, may now
    hold keeps its old seal."""
    imported = [Path(p) for p in (job.execute_args or {}).get(_IMPORTED_INTO) or ()]
    if not imported:
        return picks
    clear_scan_caches()
    captures = {}
    resealed = []
    for c in picks:
        path = (candidate_premise.expected_path(c)
                if candidate_premise.expected_kind(c) == "missing" else "")
        own = {p.relative_to(path).parts[0] for p in imported
               if path and p != Path(path) and p.is_relative_to(path)}
        fresh = None
        if own:
            if path not in captures:
                captures[path] = candidate_premise.capture("missing", path)
            fresh = candidate_premise.resealed_after_own_imports(
                (c.get("payload") or {}).get("_premise"), captures[path], own)
        if fresh is not None and not _may_hold(c, [Path(path) / name for name in own]):
            c = {**c, "payload": {**c["payload"], "_premise": fresh}}
        resealed.append(c)
    return resealed


def _may_hold(cand, folders):
    """Whether ``folders`` are named for the pick's album, or the check run
    before its download would find its tracks on disk."""
    if any(catalog.bears_album_title({"title": cand.get("title")}, folder)
           for folder in folders):
        return True
    full = catalog.album_cache.get((cand.get("payload") or {}).get("album_id")) or {}
    if not (full.get("tracks") or {}).get("items"):
        return False
    _existing, found, _missing, present = catalog.find_edition_tracks(full)
    return found is not None and bool(present)


def execute_albums(job, chosen, token):
    """Download each selected album via the normal process_album path."""
    # The web worker runs jobs back-to-back; a directory listing cached by
    # a previous job would otherwise be reused even though folders may
    # have moved since.
    clear_scan_caches()
    with job._lock:
        job.execute_args = {**{key: value
                               for key, value in (job.execute_args or {}).items()
                               if key not in (_WRITTEN_INTO, _IMPORTED_INTO,
                                              job_persistence.ALBUMS_DONE)},
                            job_persistence.ALBUMS_PICKED: len(chosen)}
    args = build_args()
    _benign = {"already_complete", "skipped_already_higher_quality", "dry_run",
               "user_skipped", "lossy_only", "no_tracks", "skipped_has_extras",
               "cancelled"}
    ok = 0
    partial = 0
    retryable_tracks = 0
    lossy_only_tracks = 0
    unavailable_tracks = 0
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
    failure_causes = []
    cancelled_cand = None
    # Fold-back recovery is kind-scoped: this function executes both library
    # and new-release batches, and their picks must never cross into each
    # other's review, a library pick returns to the Library review, a
    # new-release pick to the New Releases review (it stays there until
    # downloaded or dismissed).
    is_library_run = getattr(job, "execute_kind", "") == "library"
    is_nr_run = getattr(job, "execute_kind", "") == "new_releases"
    is_restore_run = getattr(job, "execute_kind", "") == "collection_restore"
    review_kind = (job.execute_kind
                   if is_library_run or is_nr_run or is_restore_run else None)
    review_args = job.execute_args if is_restore_run else None
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
                "below the target quality."
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

    def _fold_back_unfinished(*, always=False):
        # Token death / outage mid-batch: once something has imported, this
        # job is headed for FAILED (approve's no-harm re-park only covers the
        # nothing-landed case), fold the picks this run didn't finish back
        # into the living review, ticked, so they come back to retry like any
        # other early exit. A full disk has no re-park, so it always folds.
        if not job._imported_any and not always:
            return
        leftovers = _resealed_past_own_imports(
            job, failed_cands + [cand] + chosen[processed:])
        if review_kind:
            _remember_review_save(
                _return_picks(leftovers, review_kind, review_args))

    def _disk_full_summary():
        parts = []
        if ok:
            parts.append(f"{plural(ok, 'album')} downloaded")
        if partial:
            parts.append(f"{plural(partial, 'album')} partly downloaded")
        attention_total = sum(attention_counts.values())
        if attention_total:
            parts.append(f"{plural(attention_total, 'album')} "
                         f"{'needs' if attention_total == 1 else 'need'} attention")
        job.summary = "Stopped: the music disk is full. " + (
            ", ".join(parts) + "." if parts else "Nothing was downloaded.")
        if review_kind:
            destination = ("Library" if is_library_run else
                           "New releases" if is_nr_run else "Restore review")
            job.summary += (
                f" The rest could not be saved back to {destination}."
                if retry_save_failed else
                f" The rest are selected for retry in {destination}."
            )

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
        exact_edition = bool(
            is_restore_run or is_nr_run or not is_gap_candidate(cand)
            or cand["payload"].get("exact_edition"))
        try:
            with job_mgr.staging_lock():
                if candidate_premise.expected_kind(cand) == "missing":
                    premise = candidate_premise.validate_container(cand)
                    # Earlier albums in this same batch may have changed the
                    # artist tree. That is safe only while this exact edition
                    # still has no local folder of its own.
                    clear_scan_caches()
                    _existing, found, _missing, present = catalog.find_edition_tracks(full)
                    # A folder holding none of this edition's tracks is
                    # another album that shares its title, such as an
                    # artist's second self-titled record.
                    if found is not None and present:
                        raise CandidateStale(
                            "This missing album appeared locally after the "
                            "review was built. Refresh the review; nothing "
                            "was changed."
                        )
                else:
                    premise = candidate_premise.validate(cand)
                _note_written_into(job, cand)
                result = process_mode.process_album(full, args, allow_force=False,
                                       already_confirmed=True, token=token,
                                       exact_edition=exact_edition,
                                       expected_album_receipt=(
                                           premise["receipt"]
                                           if premise["kind"] == "gap-fill"
                                           else None
                                       ),
                                       expected_gap_fill_receipts=(
                                           cand["payload"].get(
                                               "_gap_fill_receipts")
                                           # A review saved without them:
                                           # the files were just proved
                                           # unchanged, so seal them now.
                                           or candidate_premise.gap_fill_receipts(
                                               premise)
                                           if premise["kind"] == "gap-fill"
                                           else None
                                       ))
        except (AuthLost, QobuzUnavailable, beets_mod.MusicDiskFull) as e:
            disk_full = isinstance(e, beets_mod.MusicDiskFull)
            _fold_back_unfinished(always=disk_full)
            if disk_full:
                _disk_full_summary()
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
            _settle_pick(job, cand)
        elif result and result.get("imported") and result.get("n_ok", 0) > 0:
            job._imported_any = True
            _settle_pick(job, cand, result.get("dir"))
            _note_album_done(job)
            _refresh_after_local_album_change(
                full,
                result,
                fallback_artist=cand.get("artist"),
                token=token,
                args=args,
                claims_album=True,
            )
            # The album is on disk now, any OTHER parked library review still
            # offering it is stale.
            prune_library_review_candidates(full)
            # A partial (some tracks landed, some failed) isn't a full
            # download, count it apart so the summary doesn't claim it
            # finished, and surface the remainder NOW (after the prune, which
            # would otherwise drop the fresh candidate as a same-id stale one)
            # instead of leaving it invisible until the next manual refresh.
            retryable, unretryable = incomplete_track_counts(result)
            if attention_kind == "backup":
                attention_counts["backup"] += 1
            elif retryable or unretryable:
                _, _, lossy_only, unavailable = track_shortfall(result)
                partial += 1
                retryable_tracks += retryable
                lossy_only_tracks += lossy_only
                unavailable_tracks += unavailable
                if (is_library_run or is_restore_run or is_nr_run) and retryable:
                    _remember_review_save(_fold_partial_gap_fill(
                        full, cand.get("artist") or "", retryable,
                        album_dir=result.get("dir"), exact_edition=exact_edition,
                        park_when_absent=True))
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
            failure_causes.append(failure_cause(result or {}))
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if job.cancel_requested and (
            cancelled_cand is not None or processed < len(chosen)):
        # Nothing this run leaves behind is lost: the picks it never started
        # AND the ones that failed along the way fold back into the living
        # review, ticked, so a cancel mid-batch doesn't strand them in this
        # dead job. When the whole review was ticked at approval there is no
        # living review to fold into, so park a fresh one, the finish path
        # and the new-release side both already do that.
        unrun = chosen[processed:]
        if cancelled_cand is not None:
            unrun = [cancelled_cand] + unrun
        leftovers = _resealed_past_own_imports(job, failed_cands + unrun)
        if leftovers and review_kind:
            _remember_review_save(
                _return_picks(leftovers, review_kind, review_args))
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
        destination = ("Library" if is_library_run else
                       "New releases" if is_nr_run else "Restore review")
        if retry_count and (is_library_run or is_nr_run or is_restore_run):
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
            destination = "Library Gap Fill"
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
        if lossy_only_tracks or unavailable_tracks:
            messages.append(
                f"{download_outcomes.beyond_retry(lossy_only_tracks, unavailable_tracks)}."
            )
        job.error = " ".join(messages)
    _apply_non_track_attention()
    if has_issues:
        job.status = job_mgr.JobStatus.FAILED
    failed_cands = _resealed_past_own_imports(job, failed_cands)
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
    elif failed_cands and review_kind:
        # Partial approve: the unticked picks stayed behind as a living split-
        # off review. A new release that failed to download goes back to the
        # New Releases review rather than being consumed.
        _remember_review_save(
            _return_picks(failed_cands, review_kind, review_args))
    if failed:
        destination = ("New releases" if is_nr_run else
                       "Restore review" if is_restore_run else "Library")
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
        causes = "; ".join(dict.fromkeys(cause for cause in failure_causes if cause))
        failure_message = (
            f"{failed} of {plural(len(chosen), 'album')} didn't finish"
            f"{': ' + causes if causes else ''}.{reason} {outcome}"
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
    _close_completed_job(job)


def execute_upgrades(job, chosen, token):
    """Re-rip the present tracks of each chosen album at higher quality."""
    effective = settings_store.current()
    current_quality_signature = upgrade_state.quality_signature(
        effective.get("STREAMRIP_QUALITY"),
        effective.get("PREFER_HIRES"),
        effective.get("DOWNSAMPLE_HIRES_ENABLED"),
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
    _start_album_count(job)
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
    companion_notes = []
    cd_quality_tracks = []
    put_back_notes = []
    catalogue_failed = 0
    quality_attention = 0
    processing_attention = 0
    failed = 0
    changed = 0
    failed_cands = []
    refused_cands = []
    processed = 0
    current_interrupted = False

    def return_unfinished(current_index):
        if not job._imported_any:
            return True
        leftovers = failed_cands + chosen[current_index:]
        saved = _return_upgrade_picks(
            leftovers, refused_cands, job.execute_args)
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
        except (AuthLost, QobuzUnavailable, beets_mod.MusicDiskFull):
            return_unfinished(i - 1)
            raise
        except CandidateStale as e:
            log.info(f"  skipped: {e}")
            changed += 1
            continue
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
            _note_album_done(job)
        elif result and attention_kind == "backup":
            # Imported, but the rebuilt folder couldn't be verified as
            # complete as the original, so the backup was kept.
            kept += 1
            job._imported_any = True
            _note_album_done(job)
        elif result and result.get("imported") and _res not in (
                _skip | {"upgrade_aborted_backup_failed"}):
            ok += 1
            job._imported_any = True
            _note_album_done(job)
            cd_quality_tracks.extend(result.get("cd_quality_tracks") or [])
            if result.get("companions_kept"):
                companion_notes.append((
                    f"{cand.get('artist', '')} - {cand.get('title') or '?'}",
                    result["companions_kept"]))
            verdict = result.get("quality_verdict")
            if attention_kind == "quality":
                quality_attention += 1
                job_mgr.record_quality_shortfall(job, verdict)
                quality_decision.mark_album_capped(album.get("id"), album, {
                    "n_below": verdict["n_below"],
                    "n_at": 0,
                    "n_above": 0,
                })
                retry = " after retry" if verdict["retried"] else ""
                log.info(
                    f"  upgrade incomplete: {plural(verdict['n_below'], 'track')} "
                    f"still below target{retry}. Marked capped."
                )
            elif attention_kind == "processing":
                processing_attention += 1
            _refresh_after_local_album_change(
                album,
                result,
                fallback_artist=cand.get("artist"),
                token=token,
                args=args,
            )
            # The verified replace refreshed the whole album, so a parked
            # library review's candidates for it (incl. Gap Fill) are stale.
            prune_library_review_candidates(album)
        elif result and _res in (_skip - {"cancelled", "dry_run"}):
            saved_views.forget_upgraded_album(
                None, cand["payload"].get("album_dir"))
        elif _res not in _skip:
            failed += 1
            if result and result.get("upgrade_refused"):
                refused_cands.append(cand)
            else:
                failed_cands.append(cand)
            if result and result.get("upgrade_put_back"):
                put_back_notes.append((
                    f"{cand.get('artist', '')} - {cand.get('title') or '?'}",
                    result.get("upgrade_set_aside")))
        if current_interrupted:
            break
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    if current_interrupted or (
            job.cancel_requested and processed < len(chosen)):
        first_unfinished = processed - 1 if current_interrupted else processed
        leftovers = failed_cands + chosen[first_unfinished:]
        retry_saved = _return_upgrade_picks(
            leftovers, refused_cands, job.execute_args)
        parts = [f"{ok} upgraded"]
        if failed:
            parts.append(f"{failed} failed")
        if changed:
            parts.append(f"{changed} changed since the review")
        if current_interrupted:
            parts.append("1 interrupted")
        parts.append(f"{len(chosen) - processed} not started")
        job.summary = "Stopped early. " + ", ".join(parts) + "."
        if retry_saved is False:
            unsaved = len(leftovers) + len(refused_cands)
            job.summary += (
                f" {plural(unsaved, 'album')} could not be saved back to "
                "Upgrade."
            )
        elif leftovers:
            job.summary += (
                f" {plural(len(leftovers), 'retry choice')} returned to "
                "Upgrade, selected for retry."
            )
        if cd_quality_tracks:
            job.summary += f" {cd_quality_note(cd_quality_tracks)}"
        if kept:
            job.summary += (
                f" {plural(kept, 'replacement')} retained a safety backup."
            )
        job.summary += _kept_companions_note(companion_notes)
        job.summary += _put_back_note(put_back_notes)
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
            job.error = "An album remained below the target quality."
        elif processing_attention:
            job.attention = "processing"
            job.error = "Post-download work needs attention; see the job log."
        elif failed:
            job.error = f"{plural(failed, 'album')} couldn't be upgraded."
        elif changed:
            job.error = _changed_since_review(changed) + "."
        if retry_saved is False:
            job.error = " ".join(part for part in (
                job.error,
                "Retry choices could not be saved. Check the data folder, "
                "then refresh Upgrade before retrying.") if part)
        if kept or catalogue_failed or quality_attention or processing_attention:
            _mark_job_failed(job)
        log.info(job.summary)
        return
    msg = f"Upgraded {ok}/{plural(len(chosen), 'album')}."
    if not any((failed, kept, catalogue_failed, quality_attention,
                processing_attention, changed)):
        msg = "Finished. " + msg
    if kept:
        msg += (f" {plural(kept, 'replacement')} kept a safety backup because "
                "the replacement or recovery couldn't be verified; review it "
                "under Settings > Diagnostics.")
    if changed:
        msg += f" {_changed_since_review(changed)}."
    msg += _kept_companions_note(companion_notes)
    msg += _put_back_note(put_back_notes)
    if cd_quality_tracks:
        msg += f" {cd_quality_note(cd_quality_tracks)}"
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
    retry_saved = _return_upgrade_picks(
        failed_cands, refused_cands, job.execute_args)
    if any((failed, kept, catalogue_failed, quality_attention,
            processing_attention, changed)):
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
        if changed:
            problems.append(_changed_since_review(changed))
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
        if retry_saved is False:
            unsaved = len(failed_cands) + len(refused_cands)
            job.error += (
                f" {plural(unsaved, 'album')} could not be saved back to "
                "Upgrade; refresh Upgrade.")
        elif failed_cands:
            job.error += (
                f" {plural(len(failed_cands), 'album')} selected for retry.")
        _mark_job_failed(job)
    else:
        _close_completed_job(job)


def _changed_since_review(n):
    return (f"{plural(n, 'album')} changed after the review and "
            f"{'was' if n == 1 else 'were'} left unchanged; refresh Upgrade "
            "to check again")


def _note_scan_wrap_up(job, done, total):
    """Say what a scan is doing once its walk is over."""
    if done >= total and not job.cancel_requested:
        job.push_progress("Saving results")


def scan_downsamples(job):
    """Scan local files for albums stored above CD rate."""
    refusal = disk_support.refusal("downsample")
    if refusal:
        raise RuntimeError(refusal)
    clear_scan_caches()
    unreadable_artists = {}

    def artist_read_failed(artist_dir, error):
        unreadable_artists[artist_dir.name] = str(error)

    artists = list_library_artists(on_artist_error=artist_read_failed)
    if not artists and not unreadable_artists:
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
            log.info(f"  {name} - {plural(added, 'album')} above 48 kHz")
        _note_scan_wrap_up(job, done, n)

    refresh = downsample_state.refresh_for_artists(
        artists,
        hidden=hidden,
        cancel_check=lambda: bool(job.cancel_requested),
        on_artist=_on_artist,
        discovery_errors=unreadable_artists,
    )
    unchecked = len(refresh.errors)
    if not job.cancel_requested and unchecked:
        _record_unchecked_artists(job, unchecked)
    if not job.cancel_requested and refresh.complete:
        saved_views.sync_badge("downsample")
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
            f"{plural(total, 'album')} stored above 48 kHz."
            if total else
            "No albums to downsample among the artists that could be checked."
        )
        job.summary += (
            f" {plural(unchecked, 'artist')} couldn't be checked; refresh "
            "results to retry."
        )
        if not total:
            job.error = "The Downsample scan did not complete."
            _mark_job_failed(job)
    else:
        job.summary = (f"{plural(total, 'album')} stored above 48 kHz."
                       + _cap_note(job)
                       if total else
                       "No album is stored above 48 kHz.")
    if unreadable_artists:
        job.summary += " Unreadable: " + ", ".join(sorted(unreadable_artists)) + "."
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
    _start_album_count(job)
    shrunk = 0
    total_saved = 0
    interrupted_saved = 0
    total_errors = 0
    total_flush_warns = 0
    state_refresh_warnings = 0
    skipped_missing_details = 0
    skipped_missing_folders = 0
    skipped_unchanged = 0
    skipped_changed = 0
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
        if skipped_changed:
            parts.append(
                f"{plural(skipped_changed, 'album')} skipped "
                "(the files no longer match the review; refresh it)"
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
                saved_views.refresh_downsample_artist(album_dir.parent)
            else:
                downsample_state.remove_artist(album_dir.parent.name)
                saved_views.sync_badge("downsample")
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
        except CandidateStale as e:
            log.info(f"  skipped: {e}")
            skipped_changed += 1
            continue
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
            _note_album_done(job)
        elif res.get("errors"):
            failed_albums += 1
        else:
            skipped_unchanged += 1
        saved_views.refresh_downsample_artist(album_dir.parent)
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
        job.summary += _kept_originals_note(resampled_files, keep_originals)
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
                   + _kept_originals_note(resampled_files, keep_originals))
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
        damaged = [e for e in failed_files if e.get("damaged")]
        if reason:
            job.error = f"Couldn't downsample {detail}: {reason}"
        elif len(damaged) == total_errors:
            job.error = (
                f"Couldn't downsample {detail}. They don't decode cleanly and "
                "may be damaged, so they were left unchanged; Repair can "
                "re-download them.")
        else:
            job.error = (
                f"Couldn't downsample {detail}. "
                f"{'It was' if total_errors == 1 else 'They were'} left "
                "unchanged.")
            if damaged:
                names = "; ".join(
                    f"{e['album']}, {e['name']}" if e.get("album")
                    else e["name"] for e in damaged[:3])
                if len(damaged) > 3:
                    names += f" and {len(damaged) - 3} more"
                job.error += (
                    f" {names} {'doesn' if len(damaged) == 1 else 'don'}'t "
                    "decode cleanly and may be damaged; Repair can "
                    f"re-download {'it' if len(damaged) == 1 else 'them'}.")
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


def _kept_companions_note(notes):
    """Where an upgrade left original files the new album has its own copy of.

    ``notes`` pairs each album's label with the files kept in its backup.
    """
    if not notes:
        return ""
    if len(notes) == 1:
        label, names = notes[0]
        one = len(names) == 1
        return (f" {label}: the original {', '.join(names)} "
                f"{'differs' if one else 'differ'} from the new album's and "
                f"{'is' if one else 'are'} kept in a backup under Settings > "
                "Diagnostics.")
    return (f" {plural(len(notes), 'album')} had original files that differ "
            "from the new album's; they are kept in backups under Settings > "
            "Diagnostics.")


def _put_back_note(notes):
    """Name the albums whose unverified upgrade was undone.

    ``notes`` pairs each album's label with where its download was set aside.
    """
    if not notes:
        return ""
    aside = any(where for _label, where in notes)
    tail = (" and the new download set aside under Settings > Diagnostics"
            if aside else "")
    if len(notes) == 1:
        return (f" {notes[0][0]}: the new download couldn't be verified as "
                f"complete, so the original album was put back{tail}.")
    return (f" {plural(len(notes), 'album')} couldn't be verified as complete "
            f"after downloading, so the originals were put back"
            f"{tail.replace('download', 'downloads')}.")


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


def _kept_originals_note(resampled_files, keep_originals):
    """The sentence that stops a downsample summary overstating what it freed.

    Originals are only stashed for files that were actually rewritten; a run
    that converted nothing keeps nothing, so it must not promise a restore
    that has nothing behind it.
    """
    if not resampled_files or not keep_originals:
        return ""
    days = cfg.UPGRADE_BACKUP_RETENTION_DAYS
    return (f" Your hi-res originals are kept for {plural(days, 'day')}, so "
            "nothing is freed until they expire. Until then you can put them "
            "back from Settings.")


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
    out = {"verified_ok": 0, "unverified": 0, "unmatched": 0, "failed": 0,
           "specs": [], "warns": []}
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
    out["unmatched"] = sum(
        not entry.get("diagnostic")
        for bucket in ("isrc_no_match", "no_isrc_tag", "isrc_mismatch")
        for entry in scan.get(bucket, []))
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
            # Damaged as well as unmatched, so it is picked up below as a
            # re-download candidate and reported there. Saying it here too
            # printed the same file twice in slightly different words.
            continue
        out["warns"].append(
            f"    ~ {album_dir.name} - {entry.get('local_title') or '?'}: "
            f"shorter than \"{entry.get('title') or '?'}\", but the recording "
            "or edition could not be confirmed. Left alone; check the "
            "recording and edition tags before retrying.")
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
                      f"matched to a recording and edition. Re-download the whole album fresh "
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
    if any(spec["kind"] == "redownload" for spec in out["specs"]):
        # The whole-album re-download replaces the short tracks too, and a
        # refill run first would change the album under it.
        out["specs"] = [spec for spec in out["specs"]
                        if spec["kind"] == "redownload"]
    return out


class _RepairLink(OSError):
    """An artist folder Repair can't seal because of a symbolic link."""


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
        link = backup_mod.first_symlink(artist_dir)
        if link:
            raise _RepairLink(
                f"{link} is a symbolic link, which Repair can't check or back "
                "up safely; replace it with a copy of its file")
        raise OSError("artist files could not be recorded before Repair checked them")
    agg = {"verified_ok": 0, "unverified": 0, "unmatched": 0, "failed": 0,
           "checked": 0, "specs": []}
    for album_dir in list_artist_album_dirs(artist_dir):
        if job.cancel_requested:
            break
        outcome = _repair_album_outcome(album_dir, name, token)
        agg["verified_ok"] += outcome.get("verified_ok", 0)
        agg["unverified"] += outcome.get("unverified", 0)
        agg["unmatched"] += outcome.get("unmatched", 0)
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
    """One consistent live-status line for the whole-library repair sweep."""
    if not artist and not albums:
        return "Starting…"
    who = artist or "your library"
    albums_word = "album" if albums == 1 else "albums"
    return f"{who} · {albums:,} {albums_word} checked · {flagged:,} flagged"


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


def _repair_scan_caveats(n_unverified, n_failed_albums, n_failed_artists,
                         n_linked_artists=0):
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
    if n_linked_artists:
        parts.append(
            f"{plural(n_linked_artists, 'artist')} couldn't be scanned "
            "because of a symbolic link, named in the log; replace it with a "
            "copy of its file."
        )
    return "" if not parts else " " + " ".join(parts)


def scan_repairs(job, token):
    """Scan every album for ISRC-verified truncated FLACs (fanned out across
    ARTIST_SCAN_WORKERS; see _scan_repair_artist for the per-artist work)."""
    refusal = disk_support.refusal("repair")
    if refusal:
        raise RuntimeError(refusal)
    with scan_checkpoint.Writer("repair") as checkpoint:
        return _scan_repairs_impl(job, token, checkpoint)


def _scan_repairs_impl(job, token, checkpoint):
    clear_scan_caches()
    unreadable_artists = {}

    def artist_read_failed(artist_dir, error):
        unreadable_artists[artist_dir.name] = str(error)

    artists = list_library_artists(on_artist_error=artist_read_failed)
    if not artists and not unreadable_artists:
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
    n_unmatched = 0
    n_failed_albums = 0
    n_failed_artists = len(unreadable_artists)
    n_linked_artists = 0
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
            n_unmatched += counts["unmatched"]
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
             "Only problems are listed below.")
    todo = [ad for ad in artists if ad.name not in scanned]
    n = len(artists)
    done = len(scanned)
    checkpoint_warning = False

    def warn_checkpoint(message):
        nonlocal checkpoint_warning
        if checkpoint_warning:
            return
        checkpoint_warning = True
        log.info(message)
        if hasattr(job, "push_line"):
            job.push_line(message)

    def save_checkpoint(*, force=False):
        saved = checkpoint.save(
            scanned,
            {},
            artists=repair_bundles,
            meta={
                "repair_checkpoint_version": _REPAIR_CHECKPOINT_VERSION,
            },
        )
        if force and saved is not False:
            saved = checkpoint.flush()
        if saved is False:
            warn_checkpoint(
                "Repair progress could not be saved. Check the data folder; "
                "an interruption will recheck these artists."
            )
        return saved
    save_checkpoint()
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
        for fut in as_completed(list(futures)):
            ad = futures.pop(fut)
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                stopped = (
                    f"Stopped early. {plural(total, 'album')} flagged so far."
                    if total else "Stopped before anything was flagged."
                )
                job.summary = stopped + _repair_scan_caveats(
                    n_unverified, n_failed_albums,
                    n_failed_artists - n_linked_artists, n_linked_artists)
                log.info("Cancelled. Stopping scan.")
                if job.stopping_for_restart:
                    save_checkpoint(force=True)
                elif checkpoint.clear() is False:
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
                    save_checkpoint(force=True)
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                log.info(f"    skipped {ad.name}: {e}")
                n_failed_artists += 1
                n_linked_artists += isinstance(e, _RepairLink)
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
            n_unmatched += agg.get("unmatched", 0)
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
            save_checkpoint()
    if n_failed_artists:
        _record_unchecked_artists(job, n_failed_artists)
    clear_failed = False
    if n_failed_artists or n_failed_albums:
        save_checkpoint(force=True)
    else:
        clear_failed = checkpoint.clear() is False
    # Honest summary: report what was actually decode-verified, and never
    # claim completeness the scan didn't earn.
    caveats = _repair_scan_caveats(
        n_unverified, n_failed_albums,
        n_failed_artists - len(unreadable_artists) - n_linked_artists,
        n_linked_artists)
    unmatched = (
        f" {plural(n_unmatched, 'track')} couldn't be matched to a Qobuz "
        "recording, so their length wasn't checked."
        if n_unmatched else "")
    if total:
        job.summary = (f"{plural(total, 'album')} flagged with damaged files. "
                       f"{plural(n_verified, 'track')} decode-verified clean."
                       + unmatched + caveats)
    else:
        job.summary = (f"No damaged files found. "
                       f"{plural(n_verified, 'track')} decode-verified intact."
                       + unmatched + caveats)
    if clear_failed:
        job.summary += (
            " Old resume data could not be cleared. Check the data folder "
            "before running Repair again."
        )
        warn_checkpoint(
            "Repair finished, but its old resume data could not be cleared. "
            "Check the data folder before running Repair again."
        )
    if unreadable_artists:
        job.summary = (
            f"{plural(len(unreadable_artists), 'artist')} could not be read: "
            + ", ".join(sorted(unreadable_artists)) + ". " + job.summary
        )
    if n_failed_artists and not total:
        _mark_job_failed(job)
    elif n_failed_artists or n_failed_albums or n_unverified:
        job.attention = "unchecked"
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

    def stopped_after_landing(exc):
        # A stop once the replacement reached the library: the original
        # stays in its backup until the new album is checked, and Beets
        # drops the entries the new files replaced at their paths.
        if backup is None or not backup.exists() or not album_dir.exists():
            return
        pin_repair_recovery("repair backup kept, re-download was interrupted")
        with contextlib.suppress(KeyboardInterrupt):
            beets_mod.forget_replaced_beets_entries(folder=album_dir)
        recovery, _ = checkpoint_recovery(
            "refill",
            "Repair stopped after the replacement reached the library, "
            "before it was checked.",
        )
        raise RepairRecoveryRequired(recovery, exc) from exc

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
            result = process_mode.process_album(
                full, build_args(), allow_force=False, already_confirmed=True,
                token=token, cover_backup=backup,
                rebuild_dir=album_dir if backup is not None else None) or {}
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
    except BaseException as exc:
        stopped_after_landing(exc)
        raise
    imported_ok = bool(result.get("imported")) and result.get("n_ok", 0) > 0
    if backup:
        try:
            if imported_ok and process_mode._upgrade_replacement_verified(full, album_dir, backup):
                # Carry useful companions, then retire the original's backup when
                # every file it holds is verifiably superseded in the new album.
                carried = process_mode._carry_non_audio_from_backup(
                    full, album_dir, backup, replacement_dir=album_dir)
                if carried is not None:
                    replacement_path, replacement_receipt, left = carried
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
                    elif left:
                        result["companions_kept"] = left
                        log.info(process_mode._keep_backup_for_companions(
                            backup, left))
                        one = len(left) == 1
                        checkpoint_recovery(
                            "verification",
                            "The re-download verified. The original album's "
                            f"{', '.join(left)} {'differs' if one else 'differ'} "
                            "from the new album's and "
                            f"{'is' if one else 'are'} kept here.",
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
                # A track the replacement lacks keeps its entry, and Restore
                # needs it.
                beets_mod.forget_replaced_beets_entries(folder=album_dir)
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
        except BaseException as exc:
            stopped_after_landing(exc)
            raise
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
    _start_album_count(job)
    args = build_args()
    fixed = 0
    unverified = 0
    failed = 0
    changed = 0
    partly = []
    cd_quality_tracks = []
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
        saved = _return_picks(
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
        except (AuthLost, QobuzUnavailable, beets_mod.MusicDiskFull):
            return_unfinished(i - 1)
            raise
        except CandidateStale as e:
            log.info(f"  skipped: {e}")
            changed += 1
            continue
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
            try:
                candidate_premise.validate(cand)
            except CandidateStale:
                changed += 1
                unfinished_start = processed
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
            cd_quality_tracks.extend(result.get("cd_quality_tracks") or [])
        elif (result and result.get("n_ok", 0) > 0 and result.get("imported")
                and result.get("n_fail", 0) == 0):
            fixed += 1
            job._imported_any = True
            _note_album_done(job)
            cd_quality_tracks.extend(result.get("cd_quality_tracks") or [])
        elif result and result.get("n_ok", 0) > 0 and result.get("imported"):
            # Some tracks verified and stay; the others' originals went
            # back, and only a fresh scan can offer them again.
            partly.append((result["n_ok"], result.get("n_fail", 0)))
            job._imported_any = True
            cd_quality_tracks.extend(result.get("cd_quality_tracks") or [])
        else:
            failed += 1
            # Start refuses a pick whose album this run changed, even when it
            # only put the originals back, so only a fresh scan can offer it.
            try:
                candidate_premise.validate(cand)
            except CandidateStale:
                changed += 1
            else:
                failed_cands.append(cand)
        if local_files_changed:
            _refresh_after_local_album_change(
                None,
                {"dir": p["album_dir"], **result, "in_place": True},
                fallback_artist=p.get("artist_name"),
                token=token,
                args=args,
            )
        if stopped_early:
            break
        time.sleep(cfg.ARTIST_API_DELAY)
    job._progress_scope = None
    recovery_count = len(getattr(job, "recoveries", []) or [])
    if stopped_early:
        if unfinished_start is None:
            unfinished_start = processed
        leftovers = failed_cands + chosen[unfinished_start:]
        retry_saved = _return_picks(
            leftovers,
            "repair",
            execute_args=getattr(job, "execute_args", None),
        )
        parts = [f"{plural(fixed, 'album')} repaired"]
        if unverified:
            parts.append(f"{unverified} replaced but unproven")
        if failed:
            parts.append(f"{failed} failed")
        if interrupted:
            parts.append(f"{interrupted} interrupted")
        if partly:
            parts.append(f"{len(partly)} partly repaired")
        if changed:
            parts.append(f"{changed} to check again with a new Repair scan")
        if recovery_count:
            parts.append(f"{recovery_count} kept for recovery")
        parts.append(f"{len(chosen) - processed} not started")
        job.summary = "Stopped early. " + ", ".join(parts) + "."
        if retry_saved is False:
            job.summary += (
                f" {plural(len(leftovers), 'retry choice')} could not be "
                "saved back to Repair."
            )
            job.error = (
                "Retry choices could not be saved. Check the data folder, "
                "then run a fresh Repair scan."
            )
        elif leftovers:
            job.summary += (
                f" {plural(len(leftovers), 'retry choice')} returned to "
                "Repair, selected for retry."
            )
        if failed and not job.error:
            job.error = (
                f"{failed} of {plural(len(chosen), 'album')} couldn't be "
                "repaired; see the log."
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
    rescan = (
        f" {plural(changed, 'album')} "
        f"{'needs' if changed == 1 else 'need'} a new Repair scan before "
        "another try."
        if changed else ""
    )
    repaired_tracks = sum(ok for ok, _ in partly)
    failed_tracks = sum(bad for _, bad in partly)
    part = (
        f" {plural(len(partly), 'album')} only in part: "
        f"{plural(repaired_tracks, 'track')} repaired, {failed_tracks} "
        f"failed and {'its original was' if failed_tracks == 1 else 'their originals were'} "
        "put back; a new Repair scan can try "
        f"{'it' if failed_tracks == 1 else 'them'} again."
        if partly else ""
    )
    job.summary = (
        f"{'Finished. ' if not (failed or unverified or changed or partly) else ''}"
        f"Repaired {fixed + len(partly)}/{plural(len(chosen), 'album')}."
        f"{part}{kept}{unproven}{rescan}")
    if cd_quality_tracks:
        job.summary += f" {cd_quality_note(cd_quality_tracks)}"
    log.info(job.summary)
    retry_saved = _return_picks(
        failed_cands,
        "repair",
        execute_args=getattr(job, "execute_args", None),
    )
    if failed or changed or partly:
        job.error = ((
            f"{failed} of {plural(len(chosen), 'album')} couldn't be repaired; "
            "see the log." if failed else "")
            + (f" {plural(failed_tracks, 'track')} couldn't be repaired; "
               "see the log." if partly else "")
            + rescan).strip()
        if failed_cands:
            job.error += (
                f" {plural(len(failed_cands), 'album')} selected for retry."
                if retry_saved is not False
                else " Retry choices could not be saved; run a fresh scan."
            )
        _mark_job_failed(job)
    else:
        _close_completed_job(job)


def run_collection_snapshot(job, *, force=False):
    """Write a collection snapshot on demand, outside a scan.

    Nothing here talks to Qobuz. Album ids come from the last snapshot, so a
    manual backup between scans keeps everything the scans have resolved.
    """
    job.push_line("Reading the library.")
    try:
        previous = collection_snapshot.load_latest()
    except OSError:
        previous = None
    try:
        document = collection_snapshot.build_snapshot(
            previous=previous, source="manual")
        written, refusal = collection_snapshot.write_snapshot(
            document, force=force)
    except OSError as e:
        job.summary = f"The snapshot couldn't be written: {e}"
        job.error = job.summary
        _mark_job_failed(job)
        log.info(job.summary)
        return
    counts = document["counts"]
    if not written:
        job.summary = refusal
        job.error = refusal
        _mark_job_failed(job)
        log.info(job.summary)
        return
    job.summary = (
        f"Backed up {plural(counts['albums'], 'album')} by "
        f"{plural(counts['artists'], 'artist')} to "
        f"{collection_snapshot.latest_path()}."
    )
    log.info(job.summary)
    _close_completed_job(job)


def run_library_lyrics(job, *, rescan=False, synced_only=False):
    """Fetch lyrics for every library track that's missing them."""
    if not lyrics.HAVE_LYRICS:
        job.summary = "Lyric fetching isn't available; the syncedlyrics library isn't installed."
        job.error = job.summary
        _mark_job_failed(job)
        log.info(job.summary)
        return
    refusal = disk_support.refusal("lyrics")
    if refusal:
        job.summary = job.error = refusal
        _mark_job_failed(job)
        log.info(refusal)
        return

    log.info(f"Fetching lyrics across the library (writing {(cfg.LYRICS_FORMAT or 'embed').lower()}).")
    if rescan:
        log.info("Re-checking every track, including ones already checked.")
    # Hold the staging lock: the engine rewrites library FLACs in place, which
    # must not race the scan-lane downsample/repair/upgrade work on the same tree.
    with job_mgr.staging_lock():
        job_mgr.set_staging_holder("Lyrics scan")
        try:
            with lyric_fetch.recording_writes() as written:
                try:
                    res = lyrics.run_library_lyrics(
                        rescan=rescan, synced_only=synced_only,
                        should_stop=lambda: job.cancel_requested, log=log)
                finally:
                    reseal_after_own_writes(written)
        finally:
            job_mgr.set_staging_holder(None)

    total = res.get("total", 0)
    unreadable = res.get("unreadable_artists", [])
    if unreadable:
        _record_unchecked_artists(job, len(unreadable))
    if not total and unreadable:
        job.summary = ("Lyrics scan incomplete. Unreadable: "
                       + ", ".join(unreadable)
                       + ". Retry after checking permissions.")
        _mark_job_failed(job)
        return
    if not total:
        job.summary = "No FLAC files found in the library."
        log.info(job.summary)
        _close_completed_job(job)
        return
    if res.get("stopped") and res.get("stop_stage") == "index":
        job.summary = (
            f"{lyrics.STOPPED}reading the lyrics of "
            f"{min(total, int(res.get('processed', 0)))} of "
            f"{plural(total, 'track')}, before any lookup. Nothing was written.")
        log.info(job.summary)
        return
    if res.get("stopped"):
        job.summary = lyrics.stop_summary(
            lyrics.summarize_lyrics_result(res), cfg.LYRICS_FORMAT)
        log.info(job.summary)
        return

    counts = lyrics.summarize_lyrics_result(res)
    processed = counts["processed"]
    skipped = counts["already_checked"]
    if not processed and not unreadable:
        job.summary = (
            f"Nothing needed checking, "
            f"{'the one track has' if total == 1 else f'all {total} tracks have'} "
            f"lyrics or {'was' if total == 1 else 'were'} checked before. "
            "Tick “Re-check everything” to check "
            f"{'it' if total == 1 else 'them'} again.")
        log.info(job.summary)
        _close_completed_job(job)
        return
    parts = [
        f"{plural(processed, 'track')} checked",
        f"{plural(counts['wrote'], 'track')} got lyrics"
        + (f" ({counts['upgraded']} upgraded from plain to synced)"
           if counts["upgraded"] else ""),
    ]
    if unreadable:
        parts.append("unreadable artists: " + ", ".join(unreadable)
                     + "; retry after checking permissions")
    for count, phrase in (
        (counts["copied"],
         lyrics.copied_note(counts["copied"], cfg.LYRICS_FORMAT)),
        (counts["already"],
         f"{plural(counts['already'], 'track')} already had lyrics"),
        (counts["not_found"],
         f"no synced lyrics found for {plural(counts['not_found'], 'track')} "
         "(a run without “Synced lyrics only” tries them again)"
         if synced_only else
         f"no lyrics found for {plural(counts['not_found'], 'track')} "
         f"(next try after {lyric_fetch.RECHECK_AFTER_DAYS} days)"),
        (counts["missing_tags"],
         f"{plural(counts['missing_tags'], 'track')} missing tags"),
        (counts["too_long"],
         f"{plural(counts['too_long'], 'track')} too long"),
        (counts["instrumental"],
         f"{plural(counts['instrumental'], 'track')} instrumental"),
        (counts["read_only"], lyrics.read_only_note(counts["read_only"])),
        (counts["unsafe"],
         f"{plural(counts['unsafe'], 'unsafe path')} refused"),
        (counts["unavailable"],
         f"{plural(counts['unavailable'], 'track')} could not be checked "
         "(the next run tries again)"),
        (counts["errors"],
         f"{plural(counts['errors'], 'track')} hit an error"),
        (counts["other_errors"],
         f"{plural(counts['other_errors'], 'track')} returned an unexpected "
         "result"),
    ):
        if count:
            parts.append(phrase)
    if counts["failed"]:
        failures = []
        if unreadable:
            failures.append("unreadable artists: " + ", ".join(unreadable))
        if counts["unsafe"]:
            failures.append(
                f"{plural(counts['unsafe'], 'unsafe track path')} refused")
        if counts["unavailable"] and counts["failures"] == counts["unavailable"]:
            failures.append(
                "no lyric provider answered, so "
                f"{plural(counts['unavailable'], 'track')} could not be checked")
        elif counts["unavailable"]:
            failures.append(
                f"{plural(counts['unavailable'], 'track')} could not be checked")
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
    if not counts["failed"]:
        _close_completed_job(job)
    log.info(job.summary)


def scan_migration(job, src, dest, *, use_acoustid, in_place=False):
    """Analyze the source library and attach one candidate per placeable album.

    New placements and verified existing copies become the review list (grouped
    by artist). Files that can't be identified or have unresolved collisions
    are reported and left untouched. Nothing is written until the review is
    approved.
    """
    from qobuz_librarian.library import migrate as engine

    src, dest = Path(src), Path(dest)
    try:
        items = engine.collect_items(
            src, use_acoustid=use_acoustid,
            cancel_check=lambda: job.cancel_requested,
            progress=job.push_progress)
    except OSError as exc:
        job.error = (
            f"Couldn't read the source library ({exc}). Nothing was changed; "
            "fix this and scan again.")
        job.summary = job.error
        _mark_job_failed(job)
        return
    skipped = engine.skipped_folders_note(items)
    if skipped:
        job.push_line(skipped)
    unreadable = getattr(items, "unreadable", ())
    for path, reason in unreadable[:50]:
        job.push_line(f"could not be read, left where it is: {path} - {reason}")
    if len(unreadable) > 50:
        job.push_line(f"… and {len(unreadable) - 50} more")
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
            "The only music files left in the source folder could not be "
            "read; they are listed in the activity log. Nothing was copied "
            "and no plan was written." if unreadable else
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
    reviewed = {id(entry) for entry in plan.placed + resume_entries}
    job.execute_args["fingerprinted"] = [
        str(entry.source) for entry in plan.placed + resume_entries
        if entry.source_of_truth == "acoustid"]
    job.execute_args["left_behind"] = [
        [entry.status, entry.source_of_truth, str(entry.source),
         str(entry.dest_rel) if entry.dest_rel is not None else "",
         entry.reason]
        for entry in plan.entries if id(entry) not in reviewed]

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
    owners = engine._companion_owners(plan.placed + resume_entries)
    for (artist, album), group in sorted(groups.items()):
        entries = group["entries"]
        resumes = group["resume_entries"]
        resumed = (f"{plural(len(resumes), 'track')} already copied, "
                   f"{'original' if len(resumes) == 1 else 'originals'} to "
                   "delete" if in_place else
                   f"{plural(len(resumes), 'track')} already verified")
        if entries and resumes:
            detail = (f"{plural(len(entries), 'track')} to "
                      f"{'move' if in_place else 'copy'} · {resumed}")
        elif entries:
            detail = (f"{plural(len(entries), 'track')} → "
                      f"{artist}/{album}")
        elif in_place:
            detail = resumed
        else:
            detail = f"{plural(len(resumes), 'track')} already copied"
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
            "companion_receipts": engine.album_companion_receipts(
                plan, entries + resumes, owners),
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
        deleted = ("; its original will be deleted" if len(resume_entries) == 1
                   else "; their originals will be deleted")
        parts.append(
            f"{plural(len(resume_entries), 'existing file')} verified to "
            f"resume{deleted if in_place else ''}")
    if not parts:
        parts.append("No files ready")
    if s["unplaceable"]:
        parts.append(f"{s['unplaceable']} couldn't be identified")
    if unreadable:
        parts.append(
            f"{plural(len(unreadable), 'file or folder', 'files or folders')} "
            "couldn't be read, listed in the activity log")
    unsafe_collisions = s["collision"] - len(resume_entries)
    if unsafe_collisions:
        parts.append(f"{unsafe_collisions} skipped to avoid name collisions")
    need, free = engine.space_estimate(
        plan, in_place=in_place, resume_entries=resume_entries)
    job.execute_args["requires_low_space_override"] = bool(
        free is not None and need > free
    )
    if need and free is not None:
        space = f"≈{format_size(need)} to {verb}, {format_size(free)} free at the destination"
        if need > free:
            space = ("⚠ not enough free space: needs "
                     f"≈{format_size(need)} but only {format_size(free)} is free. "
                     f"The {'in-place move' if in_place else 'copy'} stays "
                     "blocked until you confirm the low-space risk below")
        parts.append(space)
    leftovers = engine.interrupted_migration_leftovers(src, dest)
    if leftovers:
        parts.append(
            "an earlier migration was cut off and left "
            f"{plural(len(leftovers), 'hidden item')}, listed in the activity "
            "log; this run leaves them alone")
        job.push_line(
            "Hidden items named .qobuz-migrate-copy-, -dir- or -name-probe- "
            "are unfinished copies you can delete. The others may hold a "
            "track that was being moved: check it is in the new library "
            "before you delete one.")
        for path in leftovers[:50]:
            shown, _ = storage._resolve_host_path(str(path))
            job.push_line(f"left by an interrupted migration: {shown}")
        if len(leftovers) > 50:
            job.push_line(f"… and {len(leftovers) - 50} more")
    job.summary = ("; ".join(parts) + ". Unidentified and skipped files stay "
                   "where they are. The full plan is saved at the destination "
                   f"when you start the {verb}.")
    # Not logged here: job.summary already renders on the job page, and the
    # activity log below it would otherwise repeat the exact same sentence.


def _unreadable_left(count):
    return (f"{plural(count, 'file or folder', 'files or folders')} could "
            "not be read and stayed at the source")


def execute_migration(job, chosen, dest, *, in_place, src=None,
                      allow_low_space=False):
    """Copy (or move) the files behind the approved albums into the layout."""
    dest = Path(dest)
    dest_display, _ = storage._resolve_host_path(str(dest))
    entries = []
    source_root = None
    source_root_receipt = None
    dest_root_receipt = None
    destination_name_semantics = None
    companion_receipts = []
    companion_seen = set()
    for c in chosen:
        payload = c.get("payload", {})
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
    fingerprinted = set((job.execute_args or {}).get("fingerprinted", ()))
    for entry in entries:
        entry.source_of_truth = (
            "acoustid" if str(entry.source) in fingerprinted else "tags")
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
    results_manifest = None
    results_error = None
    with job_mgr.staging_lock():
        job_mgr.set_staging_holder("Library migration")
        try:
            # This decisive estimate belongs inside the same mutation interval
            # as execution.
            need, free = migrate_engine.space_estimate(
                plan, in_place=in_place, resume_entries=resume_entries)
            if free is not None and need > free and not allow_low_space:
                risk = ("An in-place move that runs out mid-run would leave "
                        "your library half-relocated." if in_place else
                        "A copy that runs out stops partway, with the new "
                        "library incomplete.")
                job.error = (
                    f"Not enough free space at {dest_display}: the "
                    f"{'move' if in_place else 'copy'} needs about "
                    f"{format_size(need)} but only {format_size(free)} is free. "
                    f"{risk} Free up space, choose another destination, or "
                    "scan again and confirm the low-space risk on the new "
                    "review.")
                job.summary = job.error
                _mark_job_failed(job)
                log.info(job.summary)
                return
            left_behind = [
                migrate_engine.PlanEntry(
                    source=Path(source), status=status, source_of_truth=sot,
                    dest_rel=Path(destination) if destination else None,
                    reason=reason)
                for status, sot, source, destination, reason
                in (job.execute_args or {}).get("left_behind", [])]
            chosen_ids = {c.get("cid") for c in chosen}
            for candidate in job.candidates:
                if candidate.get("cid") in chosen_ids:
                    continue
                payload = candidate.get("payload") or {}
                for raw in [*payload.get("entries", []),
                            *payload.get("resume_entries", [])]:
                    if isinstance(raw, (list, tuple)) and len(raw) == 4:
                        left_behind.append(migrate_engine.PlanEntry(
                            source=Path(raw[0]), status=migrate_engine.SKIPPED,
                            source_of_truth=("acoustid" if raw[0] in fingerprinted
                                             else "tags"),
                            dest_rel=Path(raw[1]),
                            reason="not selected in the review"))
            try:
                manifest = migrate_engine.write_manifest(
                    plan, left_behind=left_behind)
            except (KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
                job.error = (
                    f"Nothing was {'moved' if in_place else 'copied'}: "
                    f"the plan could not be saved at "
                    f"{dest_display} ({exc}). If that folder was moved, "
                    "removed or remounted since the preview, scan again.")
                job.summary = job.error
                _mark_job_failed(job)
                log.info(job.summary)
                return
            manifest_display, _ = storage._resolve_host_path(str(manifest))
            job.push_line(f"Full plan written to {manifest_display}")
            if isinstance(job.execute_args, dict):
                # Saved now so a restart part way through can still name
                # the plan.
                job.execute_args["manifest"] = manifest_display
                job_persistence.persist(job)
            result = migrate_engine.execute_plan(
                plan, in_place=in_place,
                cancel_check=lambda: job.cancel_requested,
                progress=job.push_progress,
                resume_entries=resume_entries)
            pruned = getattr(result, "pruned", 0)
            try:
                results_manifest = migrate_engine.write_results_manifest(
                    result, plan=plan, left_behind=left_behind)
            except BaseException as exc:
                results_error = exc
                if not isinstance(exc, Exception):
                    job.push_line(f"migration report could not be saved: {exc}")
                    raise
        finally:
            job_mgr.set_staging_holder(None)
    interrupted = [
        (source, reason)
        for source, _destination, status, reason in result.outcomes
        if status == migrate_engine.INTERRUPTED
    ]
    failed = result.failed + len(interrupted)
    for failed_src, reason in (result.failures + interrupted)[:50]:
        job.push_line(f"failed: {failed_src} - {reason}")
    companion_outcomes = getattr(result, "companion_outcomes", ())
    companion_skipped = sum(
        status == migrate_engine.SKIPPED
        and reason != migrate_engine.SAME_COMPANION
        for _source, _destination, status, reason in companion_outcomes
    )
    skipped = sum(
        status == migrate_engine.SKIPPED
        and not reason.startswith(migrate_engine.VERIFIED_COPY)
        for _source, _destination, status, reason in result.outcomes
    )
    companion_failed = sum(
        status in {migrate_engine.FAILED, migrate_engine.INTERRUPTED}
        for _source, _destination, status, _reason in companion_outcomes
    )
    for source, _destination, status, reason in companion_outcomes:
        if status in {migrate_engine.FAILED, migrate_engine.INTERRUPTED}:
            job.push_line(f"sidecar failed: {source} - {reason}")
    left_behind_rows = (job.execute_args or {}).get("left_behind", [])
    unreadable = sum(
        status == migrate_engine.UNPLACEABLE
        and reason.startswith(migrate_engine.UNREADABLE)
        for status, _sot, _source, _destination, reason in left_behind_rows)
    not_carried = migrate_engine.not_carried_line(
        sum(status == migrate_engine.COLLISION
            for status, _sot, _source, _destination, _reason
            in left_behind_rows),
        sum(status == migrate_engine.UNPLACEABLE
            and not reason.startswith(migrate_engine.UNREADABLE)
            for status, _sot, _source, _destination, reason
            in left_behind_rows))
    verb = "moved" if in_place else "copied"
    has_problem = bool(
        failed or companion_failed or results_error or result.lingered
        or unreadable)
    unreadable_only = bool(unreadable) and not (
        failed or companion_failed or results_error or result.lingered
        or result.cancelled)
    if unreadable_only:
        lead = ("Finished, but "
                f"{plural(unreadable, 'file or folder', 'files or folders')} "
                "could not be read")
    elif has_problem:
        lead = (
            "Migration stopped with problems"
            if result.cancelled else "Migration needs attention"
        )
    elif result.cancelled:
        lead = "Migration stopped early"
    else:
        lead = f"{plural(result.copied, 'file')} {verb} into {dest_display}"
    parts = [lead]
    if has_problem or result.cancelled:
        parts.append(f"{plural(result.copied, 'file')} {verb} into {dest_display}")
    verified = sum(
        status == migrate_engine.SKIPPED
        and reason.startswith(migrate_engine.VERIFIED_COPY)
        for _source, _destination, status, reason in result.outcomes
    )
    if verified:
        parts.append(f"{plural(verified, 'existing file')} verified")
    if skipped:
        parts.append(f"{skipped} skipped (already present)")
    if result.companions:
        parts.append(f"carried {plural(result.companions, 'cover/sidecar file')}")
    if companion_skipped:
        parts.append(
            f"{plural(companion_skipped, 'cover/sidecar file')} already existed"
        )
    if companion_failed:
        parts.append(f"{plural(companion_failed, 'cover/sidecar file')} failed")
    if result.lingered:
        parts.append(
            f"{plural(result.lingered, 'original')} still at the source; "
            "the report says why")
    if unreadable_only:
        parts.append("what could not be read stayed at the source")
    elif unreadable:
        parts.append(_unreadable_left(unreadable))
    if failed:
        parts.append(f"{failed} failed; see the log")
        # Set job.error too (not just the prose summary) so a migration with
        # failed copies ends red, like every other execute path, instead of a
        # green DONE that buries "N failed" mid-sentence.
        job.error = f"{plural(failed, 'file')} couldn't be migrated; see the log."
    if pruned:
        parts.append(f"cleared {plural(pruned, 'empty source folder')}")
    left_lines = migrate_engine.left_in_source_lines(
        getattr(result, "left_in_source", ()))
    if left_lines:
        parts.append(left_lines[0].rstrip(":") + ", listed in the activity log")
        for line in left_lines:
            job.push_line(line)
    if not_carried:
        parts.append(not_carried)
        job.push_line(not_carried)
    if result.cancelled:
        parts.append("the destination may contain a partial migration")
    if results_error is not None:
        parts.append("the migration report could not be saved")
        job.error = (
            "Migration finished, but its results report could not be saved: "
            f"{results_error}. Check the migration details before running it "
            "again.")
    else:
        report_display, _ = storage._resolve_host_path(str(results_manifest))
        parts.append(f"report saved at {report_display}")
    if result.lingered and not job.error:
        job.error = (
            "Migration needs attention: "
            f"{plural(result.lingered, 'original')} still at the source. "
            "See the saved report.")
    if unreadable and not job.error:
        job.error = (
            f"{lead}; what could not be read stayed at the source. "
            if unreadable_only else
            f"Migration needs attention: {_unreadable_left(unreadable)}. "
        ) + "See the activity log."
    if companion_failed:
        attention = plural(companion_failed, "sidecar failure")
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
    if has_problem or result.cancelled:
        # Finished with a problem or part way, this run belongs in the
        # unresolved History until someone has looked.
        job.attention = job.attention or "failed"
    job.summary = "; ".join(parts) + "."
    log.info(job.summary)
    if not has_problem and not result.cancelled:
        # The exact copies, optional source retirement, and results report are
        # all durable. Close under the cancellation lock so a Stop arriving at
        # this final boundary cannot relabel the completed migration.
        _close_completed_job(job)


backup_mod.on_restored = reseal_put_back_album
