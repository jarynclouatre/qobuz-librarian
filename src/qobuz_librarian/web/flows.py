"""Scan / execute logic behind the web Artist and Library flows.

These wrap the same engine the CLI uses (catalog matching, gap detection,
process_album) but without any terminal prompts, a scan attaches review
candidates to the job, and execution runs over the candidates the user kept.
"""
import argparse
import contextlib
import dataclasses
import errno
import os
import re
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
    undecodable_cause,
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
    job_labels,
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
        include_other=False,
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


def _mark_job_failed(job, *, partly=False):
    """Set an explicit terminal outcome for a handled execution failure.
    ``partly`` records that some of the work landed all the same."""
    job.status = job_mgr.JobStatus.FAILED
    if partly:
        job.execute_args = {**job.execute_args, "partly_done": True}


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
    names = _first_names(left_out)
    finds = sum(left_out.values())
    return (f"The review lists at most {cap:,} albums, so {finds:,} "
            f"{'find' if finds == 1 else 'finds'} from "
            f"{plural(len(left_out), 'artist folder')} "
            f"{'is' if finds == 1 else 'are'} not listed: {names}. "
            "Work through or dismiss some, then refresh to list them.")


_LISTED_MISSING = 12


def _missing_track_lines(album, missing):
    """The first missing tracks in album order, each with its number and
    title. An album on more than one disc numbers them disc-track."""
    def disc(track):
        return track.get("media_number") or 1

    tracks = (album.get("tracks") or {}).get("items") or []
    several = (album.get("media_count") or 1) > 1 or len(
        {disc(t) for t in (*tracks, *missing)}) > 1
    lines = []
    for track in sorted(missing, key=lambda t: (disc(t), t.get("track_number") or 0)
                        )[:_LISTED_MISSING]:
        number = track.get("track_number")
        if type(number) is not int:
            number = ""
        elif several:
            number = f"{disc(track)}-{number:02d}"
        else:
            number = f"{number:02d}"
        title = (track.get("title") or "?").strip()
        version = (track.get("version") or "").strip()
        if version and version.lower() not in title.lower():
            title = f"{title} ({version})"
        lines.append({"n": number, "title": title})
    return lines


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
        extra_payload["missing_tracks"] = _missing_track_lines(
            gap.qobuz_album, gap.missing)
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
    its summary. Shapes match what each scan writes when it parks, so a review
    screen treats it exactly as it treats the original's.

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
    """Up to ``limit`` album names, with a count standing in for the rest."""
    names = [_album_label(c) for c in candidates]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) <= limit:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def _first_names(names, limit=5):
    """The first ``limit`` names in order, with a count of the rest."""
    shown = ", ".join(sorted(names, key=str.casefold)[:limit])
    if len(names) > limit:
        shown += f" (+{len(names) - limit} more)"
    return shown


def _left_out_note(unreadable):
    return (f"{plural(len(unreadable), 'artist folder')} couldn't be read and "
            f"{'was' if len(unreadable) == 1 else 'were'} left out: "
            f"{_first_names(unreadable)}.")


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


class _CandidateFold:
    """One refresh's finds merged into a parked review."""

    _key = staticmethod(fold_key)

    def __init__(self, parked, cands, review_generation, coverage, retire):
        self.parked = parked
        self.cands = cands
        self.review_generation = review_generation
        self.coverage = coverage
        self.retire = retire

    def run(self):
        if self.parked.status != job_mgr.JobStatus.AWAITING_REVIEW:
            return None
        self._read_review()
        self._carry_rows()
        self._drop_duplicates()
        self._add_finds()
        if self.review_generation is not None:
            self.parked.execute_args = {
                **(self.parked.execute_args or {}),
                "_library_review_generation": int(self.review_generation),
            }
        counted = self._count_changes()
        notes = self._write_notes()
        # Saved with this change; the review's lock is already held.
        self.parked.log_lines.extend(counted + notes)
        self.parked._trim_log_lines_locked()
        return (self.added, self.updated, self.removed, self.held_back,
                self.switched_off, self.left_out, notes)

    def _switched_on(self, c):
        if is_gap_candidate(c):
            return missing_types.gap_shown(
                (c.get("payload") or {}).get("bonus_only"), self.included)
        return missing_types.shown(
            (c.get("payload") or {}).get("release_type"), self.included)

    def _read_review(self):
        # Load while the review-action lock is held. A dismissal that finishes
        # while this fold waits must be visible before any candidate is added.
        self.hidden = hidden_mod.load()
        self.library = self.parked.execute_kind == "library"
        self.included = missing_types.load()
        self.listed = (review_listed(self.included) if self.library
                       else review_tab_filter(""))
        self.fresh_by_key = {}
        for c in self.cands:
            self.fresh_by_key.setdefault(self._key(c), c)
        self.covered = None
        self.on_disk = frozenset()
        if self.coverage is not None:
            self.covered = ({name.casefold() for name in self.coverage[0]}, self.coverage[1])
            self.on_disk = self.coverage[2]
        # A scan that read no artist folder says nothing about any of them,
        # and neither does a music folder that cannot be listed now; the
        # library may not be mounted.
        self.drop_gone = bool(self.covered and self.covered[0]) and _music_root_listed()
        self.updated = 0
        self.removed = 0
        # A ticked row stays whatever the refresh makes of it, unless the
        # scan found the album on disk or its folder is gone. The log names
        # the ticked ones that left, says once which ones it no longer offers
        # (then marks them) and how many moved under a switch that is off. A
        # kept missing album takes the seal of its artist folder as the
        # refresh read it, so its pick is not refused for a change the
        # refresh has already looked at.
        self.in_library = []
        self.gone = []
        self.retired = []
        self.unoffered = []
        self.moved = {}
        self.switched_off = {}
        self.seals = {}
        for c in self.cands:
            payload = c.get("payload") or {}
            if (candidate_premise.expected_kind(c) == "missing"
                    and payload.get("_premise")):
                self.seals.setdefault(payload.get("_artist_dir_path"),
                                      payload["_premise"])

    def _carry_rows(self):
        self.keep = []
        for c in self.parked.candidates:
            self._carry_row(c)

    def _carry_row(self, c):
        key = self._key(c)
        fresh = self.fresh_by_key.get(key)
        if fresh is None and key in self.retire:
            if self.listed(c):
                self.removed += 1
                self.retired.append(c)
            return
        if fresh is None and self.drop_gone and _folder_gone(c):
            if self.listed(c):
                self.removed += 1
            if c.get("selected"):
                self.gone.append(c)
            return
        if fresh is None and self.covered and _scan_would_offer(c, self.covered):
            album_id = str((c.get("payload") or {}).get("album_id") or "")
            if not c.get("selected") or album_id in self.on_disk:
                if self.listed(c):
                    self.removed += 1
                if c.get("selected"):
                    self.in_library.append(c)
                return
            self._keep_unoffered(c)
            return
        if fresh is None or _fold_row(fresh) == _fold_row(c):
            self.keep.append(c)
            return
        self._refresh_row(c, fresh)

    def _keep_unoffered(self, c):
        payload = dict(c.get("payload") or {})
        path = payload.get("_artist_dir_path")
        if path and candidate_premise.expected_kind(c) == "missing":
            if path not in self.seals:
                self.seals[path] = candidate_premise.capture("missing", path)
            if self.seals[path] is not None:
                payload["_premise"] = self.seals[path]
        if not payload.get("_unoffered"):
            payload["_unoffered"] = True
            self.unoffered.append(c)
        self.keep.append({**c, "payload": payload})

    def _refresh_row(self, c, fresh):
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
        self.keep.append(replacement)
        if (self.library and replacement["selected"] and self._switched_on(c)
                and not self._switched_on(fresh)):
            switch = switch_holding(fresh)
            self.moved[switch] = self.moved.get(switch, 0) + 1
        if self.listed(c) and not self.listed(replacement):
            switch = switch_holding(replacement)
            self.switched_off[switch] = self.switched_off.get(switch, 0) + 1
        elif (_row_shows_change(_fold_row(fresh), _fold_row(c))
                and (self.listed(fresh) or self.listed(c))):
            self.updated += 1

    def _drop_duplicates(self):
        # Older builds matched rows by album id alone and could leave an
        # album listed under two artists as two missing-album rows under one
        # of them. Keep the first, with either row's tick.
        first_at = {}
        unique = []
        for c in self.keep:
            key = self._key(c) if self.library and not is_gap_candidate(c) else None
            at = first_at.get(key) if key is not None else None
            if at is None:
                if key is not None:
                    first_at[key] = len(unique)
                unique.append(c)
                continue
            if c.get("selected") and not unique[at].get("selected"):
                unique[at] = {**unique[at], "selected": True}
            if self.listed(c):
                self.removed += 1
        self.keep = unique

    def _add_finds(self):
        self.parked.candidates = self.keep
        seen = {self._key(c) for c in self.keep}
        listed_rows = sum(1 for c in self.keep if self.listed(c))
        self.added = 0
        self.held_back = {}
        self.left_out = {}
        for c in self.cands:
            key = self._key(c)
            if key in seen:
                continue
            if hidden_mod.is_hidden(
                    hidden_mod.SCOPE_MISSING, c.get("artist") or "",
                    c.get("title") or "", self.hidden,
                    year=(c.get("payload") or {}).get("year")):
                continue
            seen.add(key)
            if self.listed(c) and listed_rows >= self.parked.CANDIDATE_CAP:
                self.parked._candidate_cap_noted = True
                if isinstance(self.parked.execute_args, dict):
                    self.parked.execute_args["_candidate_cap_hit"] = True
                folder = str((c.get("payload") or {}).get("_artist_dir")
                             or c.get("artist") or "")
                self.left_out[folder] = self.left_out.get(folder, 0) + 1
                continue
            seq = self.parked._cand_seq
            self.parked._cand_seq += 1
            self.parked.candidates.append({
                "cid": f"c{seq}", "seq": seq,
                "kind": c.get("kind", "album"),
                "title": c.get("title") or "?",
                "artist": c.get("artist") or "",
                "detail": c.get("detail") or "",
                "payload": c.get("payload") or {},
                "selected": bool(c.get("selected")),
            })
            self.added += 1
            if self.listed(c):
                listed_rows += 1
            else:
                switch = switch_holding(c)
                self.held_back[switch] = self.held_back.get(switch, 0) + 1

    def _count_changes(self):
        held = sum(self.held_back.values())
        away = sum(self.switched_off.values())
        counted = []
        changes = [f"{verb} {plural(n, 'album')}" for verb, n in (
            ("added", self.added - held), ("updated", self.updated), ("removed", self.removed))
            if n]
        if self.coverage is not None and (changes or held or away):
            # Say what the refresh changed in the list the review shows.
            line = "A refresh "
            if changes:
                line += (", ".join(changes[:-1])
                         + (" and " if len(changes) > 1 else "")
                         + changes[-1] + ".")
                if held:
                    line += (f" {held} more {'is' if held == 1 else 'are'} "
                             f"under {missing_types.named(self.held_back)}.")
            elif held:
                line += (f"added {plural(held, 'album')} under "
                         f"{missing_types.named(self.held_back)}.")
            if away:
                moved_line = (f"moved {plural(away, 'album')} under "
                              f"{missing_types.named(self.switched_off)}.")
                line += (" It " if changes or held else "") + moved_line
            counted.append(line)
        return counted

    def _write_notes(self):
        notes = []
        if self.in_library:
            n = len(self.in_library)
            notes.append(
                f"{plural(n, 'ticked album')} {'is' if n == 1 else 'are'} "
                f"now in the library and left the review: "
                f"{named_albums(self.in_library)}.")
        if self.gone:
            n = len(self.gone)
            notes.append(
                f"{plural(n, 'ticked album')} left the review because "
                f"{'its folder is' if n == 1 else 'their folders are'} no "
                f"longer in the library: {named_albums(self.gone)}.")
        if self.retired:
            n = len(self.retired)
            notes.append(
                f"{plural(n, 'album')} left the review because "
                f"{'its artist folder' if n == 1 else 'their artist folders'} "
                f"changed and the check no longer offers "
                f"{'it' if n == 1 else 'them'}: "
                f"{named_albums(self.retired)}.")
        if self.unoffered:
            n = len(self.unoffered)
            notes.append(
                f"The refresh no longer offers {plural(n, 'ticked album')}, "
                f"which {'stays' if n == 1 else 'stay'} ticked: "
                f"{named_albums(self.unoffered)}.")
        if self.moved:
            n = sum(self.moved.values())
            notes.append(
                f"{plural(n, 'ticked album')} {'is' if n == 1 else 'are'} "
                f"now under {missing_types.named(self.moved)} and "
                f"{'stays' if n == 1 else 'stay'} listed while ticked.")
        return notes


def fold_new_candidates(parked, cands, *, review_generation=None,
                        coverage=None, retire=()):
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
    match again, so Download would refuse it after every refresh. So does a
    row ``retire`` names by fold key that the refresh did not find again.
    Candidates the user dismissed while the refresh ran are checked against a
    fresh hidden snapshot so the fold can't resurrect them. Returns
    (added, updated, removed, held_back, switched_off, left_out, notes),
    False when the changed review could not be saved, or None when the review
    stopped being parked mid-refresh (approved/discarded). ``updated`` and
    ``removed`` count the rows the review lists that changed or left;
    ``held_back`` maps the switches (switch_holding) to the added rows they
    keep out of the list, and ``switched_off`` to the listed rows the refresh
    moved under them; ``left_out`` maps artist folders to the listed rows the
    review had no room for; ``notes`` are the lines the fold added to the
    review's log besides the counts, which the refresh's own summary gives."""
    fold = _CandidateFold(parked, cands, review_generation, coverage, retire)
    with job_mgr._library_review_state_guard(parked), parked._review_action_lock:
        saved, result = job_persistence.persist_review_mutation(parked, fold.run)
        if saved and result is not None and not parked.candidates:
            if job_mgr.finalize_review_if_empty(
                    parked, summary="Nothing left to review.") is None:
                return False
        return result if saved else False


def refold_restored_missing(artists, fingerprints):
    """Return just-restored dismissals to the open Library review.

    Dismissing drops rows from the parked job for good, so clearing the
    hidden entries alone leaves Restore looking like a no-op until some
    future scan re-carries them. Rebuild
    the restored artists'/albums' candidate specs from the saved scan state
    and fold them back in, unselected. Returns how many rejoined the review,
    or None when no library review is parked (they return on the next scan)."""
    parked = _newest_parked("library")
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


def refold_into_living_review(picks, execute_kind="library", ticked=True,
                              retire=()):
    """Fold a list of picks back into the newest parked review of
    ``execute_kind``, ticked by default, so they come back ready to retry
    instead of stranding in a dead job. Callers include picks a cancelled
    download never reached, albums that failed during a partial approval, and
    the unticked Gap Fill candidate left by a partial import. The living review
    is the split-off review left behind at approval; when the whole review was
    selected, those paths rebuild or re-park their own review instead. Dedups by
    fold key, so re-adding a pick already in the review is safe. Returns how
    many rejoined, or None when there's no review to fold into. ``retire``
    is passed on to fold_new_candidates."""
    parked = _newest_parked(execute_kind)
    if parked is None:
        return None
    specs = [dict(c, selected=True) if ticked else dict(c) for c in picks]
    # A count we wrote ourselves has to be rewritten once the review grows, or
    # the History card keeps the number it had at the split and disagrees with
    # the review it points at. A scan's own summary is left alone: it carries
    # caveats a bare count would throw away.
    ours = (parked.summary or "") == split_review_summary(
        parked.execute_kind, parked.candidates)
    folded = fold_new_candidates(parked, specs, retire=retire)
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
    """Park the albums a whole-review download (every candidate ticked)
    failed to download as a fresh saved review of ``execute_kind``, ticked
    and ready to retry, as a partial approval leaves its unpicked ones.
    Also the park half of the unticked instant Gap Fill fold, via
    ``ticked``/``summary``. Returns the parked job, None when nothing
    failed, or False when the review could not be saved."""
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
        _add_candidate_spec(job, dict(c, selected=True) if ticked else c)
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
                "to try again.")
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


_KEPT_ORIGINALS = re.compile(
    r" The originals are kept for [^.]*\."
    r"| Your hi-res originals are kept for [^.]*\. Until then you can put "
    r"them back from Settings[^.]*\.")


def note_downsample_put_back(origin):
    """Say on the page of the last Downsample run that rewrote ``origin``
    that its originals were put back, and drop the offer to put them back
    once none of that run's albums has originals left."""
    folder = os.fspath(origin)
    runs = [job for job in job_mgr.registry.all()
            if job.execute_kind == "downsample" and job.phase == "execute"
            and job.status in job_mgr.TERMINAL
            and any(c.get("selected")
                    and (c.get("payload") or {}).get("album_dir") == folder
                    for c in job.candidates)]
    if not runs:
        return
    job = max(runs, key=lambda j: j.finished_at or j.created_at or 0)
    kept = {os.fspath(other) for _path, other, _ok
            in backup_mod.list_undo_copies() if other}
    with job._lock:
        picked = [c for c in job.candidates if c.get("selected")]
        albums = [c for c in picked
                  if (c.get("payload") or {}).get("album_dir") == folder]
        note = f"{named_albums(albums)} was put back as it was."
        summary = job.summary or ""
        if note in summary:
            return
        if not any((c.get("payload") or {}).get("album_dir") in kept
                   for c in picked):
            summary = _KEPT_ORIGINALS.sub("", summary)
        job.summary = f"{summary} {note}" if summary else note
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
    extra_payload = {
        "album_dir": str(album_dir),
        "exact_edition": exact_edition,
        "_premise": premise,
        "_gap_fill_receipts": candidate_premise.gap_fill_receipts(premise),
    }
    # The folder names the tracks still missing; the list is shown only when
    # it agrees with the count the download left, which leaves out tracks
    # Qobuz does not stream.
    existing, _ = catalog.find_existing_tracks(full_album, album_dir=album_dir)
    missing, _present = catalog.edition_missing(full_album, existing, album_dir)
    missing = [t for t in missing if t.get("streamable") is not False]
    if len(missing) == n_missing:
        extra_payload["missing_tracks"] = _missing_track_lines(full_album, missing)
    return _album_candidate_spec(
        {**full_album, "_partial_missing_count": n_missing},
        artist_name, selected=False, extra_payload=extra_payload)


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
    if folded is None and not park_when_absent:
        return True
    # Fold before taking the album's other rows off, so that can't empty and
    # close the review, and settle the record the download left for the next
    # page load, which would take this row off too.
    key = fold_key(spec)
    prune_library_review_candidates(
        full_album, keep=lambda c: is_gap_candidate(c) and fold_key(c) == key)
    generation_state.clear_review_removals([full_album.get("id")])
    if folded is None:
        return _park_library_failures(
            [spec], ticked=False,
            summary="1 album downloaded only partly: the missing tracks "
                    "are ready as Gap Fill.") is not False
    return True


def prune_library_review_candidates(album, keep=None):
    """A full album just landed on disk (Search download, batch download,
    upgrade replace): drop its candidates from every parked or still-scanning
    Qobuz album review, so a stale review can't offer to download an album the
    user already has. The executing job itself is RUNNING and untouched.
    Matched by Qobuz album id; a row ``keep`` accepts stays. Returns the
    number of candidates dropped."""
    album_id = str((album or {}).get("id") or "")
    if not album_id:
        return 0
    return _prune_reviews(
        ("library", "new_releases", "collection_restore"),
        lambda c: (str((c.get("payload") or {}).get("album_id") or "")
                   == album_id and not (keep is not None and keep(c))))


def drop_owned_new_releases(job):
    """Take the albums a Library scan found on disk out of the New releases
    review, as the scan takes them out of the Library review: a new release
    copied in from outside is owned as much as one downloaded here."""
    coverage = job.scan_coverage
    if job.cancel_requested or not coverage or not coverage[2]:
        return 0
    on_disk = coverage[2]
    return _prune_reviews(
        ("new_releases",),
        lambda c: str((c.get("payload") or {}).get("album_id") or "")
        in on_disk)


def _drop_dismissed_elsewhere(job, dismissed):
    """The Library review and the New releases review share one Dismissed
    list, so an album dismissed in one leaves the other too, unless it is
    ticked there."""
    fingerprints = {hidden_mod.album_fingerprint(artist, title)
                    for artist, title, _year in dismissed}
    fingerprints.discard(None)
    if not fingerprints:
        return 0
    store = hidden_mod.load()

    def gone(c):
        artist, title = c.get("artist"), c.get("title")
        return (not c.get("selected")
                and hidden_mod.album_fingerprint(artist, title) in fingerprints
                and hidden_mod.is_hidden(
                    hidden_mod.SCOPE_MISSING, artist, title, store,
                    year=(c.get("payload") or {}).get("year")))

    other = "new_releases" if job.execute_kind == "library" else "library"
    return _prune_reviews((other,), gone, exclude=job)


def _prune_reviews(kinds, gone, exclude=None):
    """Drop the rows ``gone`` accepts from every parked or still-scanning
    review of ``kinds`` but ``exclude``. Returns how many rows left."""
    dropped = 0
    states = (job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)
    for job in job_mgr.registry.all():
        if (job is exclude
                or getattr(job, "execute_kind", "") not in kinds
                or job.status not in states):
            continue
        try:
            def _drop():
                if job.status not in states:
                    return 0
                kept = [c for c in job.candidates if not gone(c)]
                n = len(job.candidates) - len(kept)
                if not n:
                    return 0
                job.candidates = kept
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
            # Pruning is housekeeping on the side of a download, dismissal or
            # scan that succeeded, never let it turn that into a failure.
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
        failure = ("The collection backup couldn't be written: "
                   f"{collection_snapshot.write_error(e)}.")
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
    selections are never touched; most of them aren't on the page that
    triggered the hide.

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
    upgrade = scope == hidden_mod.SCOPE_UPGRADE

    def dismissed_as(c):
        if not upgrade:
            return c.get("title")
        return hidden_mod.versioned_title(
            c.get("title"), (c.get("payload") or {}).get("version"))

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
        kept = {hidden_mod.album_fingerprint(c.get("artist"), dismissed_as(c))
                for c in mine if c.get("selected")}
        kept.discard(None)
        to_hide = [c for c in mine
                   if not c.get("selected")
                   and in_tab(c)
                   and (not query or candidate_matches_query(c, query))
                   and hidden_mod.album_fingerprint(
                       c.get("artist"), dismissed_as(c)) not in kept]
        if not to_hide:
            return 0
        specs = [(c.get("artist"), dismissed_as(c),
                  (c.get("payload") or {}).get("year")) for c in to_hide]
        versions = ([(c.get("payload") or {}).get("version") or ""
                     for c in to_hide] if upgrade else None)
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
    hidden_mod.hide(scope, specs, gap_fill=gap_flags, album_ids=album_ids,
                    versions=versions)
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
            (c.get("artist"), dismissed_as(c),
             (c.get("payload") or {}).get("year"))
            for c in ticked_meanwhile
            if (c.get("artist") or "", dismissed_as(c) or "",
                str((c.get("payload") or {}).get("year") or "")) in new_keys
        ]
        hidden_mod.restore_rows(scope, raced_specs)
    if (scope == hidden_mod.SCOPE_MISSING
            and job.execute_kind in ("library", "new_releases")):
        _drop_dismissed_elsewhere(job, specs)
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
    _LibraryScan(
        job,
        token,
        checkpoint=checkpoint,
        partial_only=partial_only,
        force_full=force_full,
        attempt_id=attempt_id,
        scan_started_revision=scan_started_revision,
        allow_checkpoint_resume=allow_checkpoint_resume,
    ).run()


class _LibraryScan:
    """One Library scan. Its steps run in order and share what they read,
    reuse and find through the instance."""

    def __init__(self, job, token, *, checkpoint, partial_only, force_full,
                 attempt_id, scan_started_revision, allow_checkpoint_resume):
        self.job = job
        self.token = token
        self.checkpoint = checkpoint
        self.partial_only = partial_only
        self.force_full = force_full
        self.attempt_id = attempt_id
        self.scan_started_revision = scan_started_revision
        self.allow_checkpoint_resume = allow_checkpoint_resume

    def run(self):
        if not self._list_artists():
            return
        self._load_checkpoint()
        self._choose_refresh()
        self._refresh_disk_views()
        self._fingerprint_artists()
        self._restore_checkpoint_artists()
        self._reuse_unchanged_artists()
        self._crawl_artists()
        self._save_results()
        self._write_summary()

    def _list_artists(self):
        clear_scan_caches()
        self.unreadable_artists = set()
        self.local_discovery_errors = {}

        self.all_artists = list_library_artists(
            on_artist_error=self._artist_read_failed)
        # Compilations have no single artist catalogue to compare.
        self.artists = [d for d in self.all_artists
                        if normalize(d.name) not in VA_NORMALIZED]
        self.discovery_errors = len(self.unreadable_artists)
        if not self.artists and not self.discovery_errors:
            _set_empty_library_summary(self.job)
            if not self.partial_only:
                generation_state.finish_attempt(
                    self.attempt_id, "incomplete", "No library artist folders were found."
                )
            return False
        self.kind = "partial" if self.partial_only else "missing"
        return True

    def _artist_read_failed(self, artist_dir, error):
        self.local_discovery_errors[artist_dir.name] = str(error)
        if normalize(artist_dir.name) not in VA_NORMALIZED:
            self.unreadable_artists.add(artist_dir.name)

    def _load_checkpoint(self):
        # Resume an interrupted scan of this kind: skip the artists already done and
        # restore the albums they turned up, so we continue rather than restart.
        self.cp = scan_checkpoint.load(self.kind) if self.allow_checkpoint_resume else None
        self.quality_sig = library_scan_state.quality_signature()
        if self.cp is not None and self.cp["meta"].get(
                "quality_signature", self.quality_sig) != self.quality_sig:
            # Its artists were checked under other quality or release settings.
            log.info("The interrupted scan ran under other settings, so it "
                     "starts again.")
            self.cp = None
        self.resuming = self.cp is not None
        self.checkpoint_artists = dict(self.cp.get("artists") or {}) if self.resuming else {}
        current_artist_names = {ad.name for ad in self.artists}
        if self.resuming:
            self.scanned = set()
            self.baseline_seen = {}
            for name in set(self.cp["scanned"]):
                if name not in current_artist_names:
                    continue
                saved = self.checkpoint_artists.get(name)
                if (
                    not isinstance(saved, dict)
                    or not isinstance(saved.get("catalog_ids"), list)
                ):
                    continue
                self.scanned.add(name)
                artist_id = saved.get("artist_id") or ""
                if artist_id:
                    self.baseline_seen[str(artist_id)] = [
                        str(album_id) for album_id in saved["catalog_ids"]
                        if album_id is not None
                    ]
        else:
            self.scanned = set()
            self.baseline_seen = {}

    def _choose_refresh(self):
        self.total = 0
        # Artist folders whose finds the review had no room for, with how many
        # each lost. They are not saved as scanned, so a later refresh checks
        # them again.
        self.left_out = {}
        # Snapshot the dismissed-album memory before restoring the checkpoint so
        # albums the user dismissed since the interruption are not re-added, and
        # so the parallel workers below see the same consistent view.
        self.hidden = hidden_mod.load()
        # The review keeps every release type and bonus version and lists what
        # the switches pick; the running count follows the list.
        self._listed = review_listed()
        # Dismissing and restoring deliberately do NOT bar the cheap path. The
        # snapshot holds every candidate the last crawl found, dismissed or not, so
        # both directions are a filter over saved data, not a reason to ask Qobuz
        # about the whole library again.
        def _reusable(snapshot):
            return bool(
                not self.force_full
                and not self.resuming
                and snapshot.get("complete")
                # Saved candidates computed under a different quality policy must
                # not carry forward, a settings change re-derives even unchanged
                # folders.
                and snapshot.get("quality_signature", "") == self.quality_sig
            )

        self.previous_scan = library_scan_state.kind_summary(self.kind)
        self.cheap_refresh = _reusable(self.previous_scan)
        if self.cheap_refresh:
            # Only the cheap path reuses the saved artists; a full rescan would
            # hold them in memory for its whole run for nothing.
            self.previous_scan = library_scan_state.kind_state(self.kind)
            self.cheap_refresh = _reusable(self.previous_scan)
        if not self.resuming and not self.cheap_refresh:
            # A refresh needs no starting checkpoint: interrupted before it
            # saves progress, the next one reuses the saved scan again.
            self.checkpoint.save(self.scanned, self.baseline_seen)

    def _step(self, name):
        return f"Step {self._passes.index(name) + 1} of {len(self._passes)}: {name}"

    def _refresh_disk_views(self):
        # The two refreshes and the fingerprint pass below run before the main
        # artist loop, and on a first scan of a large library each takes real
        # minutes, without progress ticks the job sits on "Waiting for output"
        # looking hung the whole time.
        # The upgrade pass is optional, so number the steps from the ones that
        # will actually run rather than hard-coding four.
        self._passes = ["Reading albums on disk"]
        if cfg.UPGRADE_SCAN_ENABLED:
            self._passes.append("Checking upgrade quality")
        self._finding = ("Finding Gap Fill candidates" if self.partial_only
                         else "Finding missing albums")
        self._passes += ["Checking artist folders", self._finding]

        self.downsample_refresh_started_at = time.time()
        log.info(f"Reading albums from {plural(len(self.all_artists), 'artist folder')} on disk…")
        self.downsample_refresh = downsample_state.refresh_for_artists(
            self.all_artists,
            hidden=self.hidden,
            discovery_errors=self.local_discovery_errors,
            cancel_check=lambda: bool(self.job.cancel_requested),
            persist=False,
            skip_unchanged=self.cheap_refresh,
            on_artist=lambda ad, _specs, _err, done_i, total_i: self.job.push_progress(
                self._step("Reading albums on disk"),
                done_i, total_i, ad.name, unit="artist"),
        )
        self.upgrade_refresh = None
        self.upgrade_refresh_started_at = None
        self.offered_before = _offered_upgrades()
        if not self.job.cancel_requested and cfg.UPGRADE_SCAN_ENABLED:
            self.upgrade_refresh_started_at = time.time()
            # A resume carries over the upgrade pass the interrupted scan finished.
            # Folders whose fingerprint changed since are checked again.
            finished_upgrade = _finished_pass(self.cp, "upgrade") if self.resuming else None
            log.info("Comparing owned albums against the editions Qobuz can serve…")
            self.upgrade_refresh = upgrade_state.refresh_for_artists(
                self.artists,
                token=self.token,
                args=build_args(),
                capped=quality_decision.load_capped(),
                hidden=self.hidden,
                cancel_check=lambda: bool(self.job.cancel_requested),
                workers=max(1, int(cfg.ARTIST_SCAN_WORKERS)),
                pool_kwargs=job_mgr.pool_initializer_kwargs(),
                skip_unchanged=self.cheap_refresh or finished_upgrade is not None,
                previous=finished_upgrade,
                persist=False,
                known_fingerprints=self.downsample_refresh.fingerprints,
                on_artist=lambda ad, _specs, _err, done_i, total_i: self.job.push_progress(
                    self._step("Checking upgrade quality"),
                    done_i, total_i, ad.name, unit="artist"),
            )
            if self.upgrade_refresh.complete and not self.job.cancel_requested:
                self.checkpoint.keep_pass(
                    "upgrade", upgrade_state.reusable_result(self.upgrade_refresh))
        elif not cfg.UPGRADE_SCAN_ENABLED:
            review_badges.set_ready("upgrade", False)

    def _fingerprint_artists(self):
        target = ("Gap Fill candidates in owned albums" if self.partial_only
                  else "missing albums and Gap Fill candidates")
        log.info(f"Scanning {plural(len(self.artists), 'library artist')} for {target}")
        self.fingerprints = {}
        for _i, _ad in enumerate(self.artists, 1):
            if self.job.cancel_requested:
                break
            try:
                self.fingerprints[_ad.name] = (
                    self.downsample_refresh.fingerprints.get(_ad.name)
                    or artist_fingerprint(_ad))
            except OSError as exc:
                self._artist_read_failed(_ad, exc)
                log.warning(f"Unreadable artist {_ad.name}: {exc}. Check permissions and retry.")
            if _i % 25 == 0 or _i == len(self.artists):
                self.job.push_progress(self._step("Checking artist folders"),
                                       _i, len(self.artists), _ad.name, unit="folder")
        self.artists = ([] if self.job.cancel_requested
                        else [ad for ad in self.artists if ad.name in self.fingerprints])
        self.discovery_errors = len(self.unreadable_artists)
        if self.resuming:
            # A checkpoint says what was compared, not that the local folder is
            # still the same. Reuse only a current matching per-artist fingerprint;
            # removed and legacy entries fall away independently.
            self.scanned = {
                name
                for name in self.scanned
                if name in self.fingerprints
                and isinstance(self.checkpoint_artists.get(name), dict)
                and self.checkpoint_artists[name].get("fingerprint")
                == self.fingerprints.get(name)
            }
            self.baseline_seen = {}
            for name in self.scanned:
                saved = self.checkpoint_artists[name]
                artist_id = saved.get("artist_id") or ""
                if artist_id:
                    self.baseline_seen[str(artist_id)] = list(
                        saved.get("catalog_ids") or [])

    def _restore_checkpoint_artists(self):
        self.previous_artists = (
            (self.previous_scan.get("artists") or {}) if self.cheap_refresh else {})
        self.state_artists: dict[str, dict] = {}
        if self.resuming:
            # Older checkpoints also list every album found outside the artists.
            restored_by_artist: dict[str, list[dict]] = {}
            for c in self.cp["candidates"]:
                artist_key = (c.get("payload") or {}).get("_artist_dir") or c.get("artist")
                restored_by_artist.setdefault(artist_key, []).append(c)
            for name, saved in self.checkpoint_artists.items():
                if name not in self.scanned:
                    continue
                candidates = (list(saved.get("candidates") or [])
                              or restored_by_artist.get(name, []))
                dropped = 0
                for c in candidates:
                    if _spec_dismissed(c, self.hidden):
                        continue
                    listed = self._listed(c)
                    if _add_candidate_spec(self.job, c, listed=listed) is None:
                        dropped += 1
                    elif listed:
                        self.total += 1
                if dropped:
                    self.left_out[name] = dropped
                    continue
                self.state_artists[name] = {
                    "fingerprint": saved.get("fingerprint") or self.fingerprints.get(name, ""),
                    "candidates": candidates,
                    "artist_id": saved.get("artist_id") or "",
                    "catalog_ids": list(saved["catalog_ids"]),
                    "owned_ids": dict(saved.get("owned_ids") or {}),
                }
            recheck = len(self.artists) - len(self.scanned)
            log.info(
                f"Resuming. Reusing {plural(len(self.scanned), 'unchanged artist')}; "
                f"checking {plural(recheck, 'changed or unproven artist')} again. "
                f"{plural(self.total, 'album')} found so far."
            )

    def _reuse_unchanged_artists(self):
        self.todo = []
        self.n = len(self.artists)
        self.done = len(self.scanned)
        reused = 0
        self.scan_errors = self.discovery_errors
        self.read_errors = 0
        # Ids for the collection snapshot written when the scan finishes. An
        # artist reused from saved state brings the ids saved with it; one saved
        # before they were kept is carried forward from the last snapshot.
        self.owned_qobuz = {}
        self.snapshot_artist_ids = {}
        self.on_disk_ids = set()

        def _reuse_owned(name, entry):
            if entry.get("owned_ids"):
                self.owned_qobuz[name] = dict(entry["owned_ids"])
            if entry.get("artist_id"):
                self.snapshot_artist_ids[name] = str(entry["artist_id"])

        for name, entry in self.state_artists.items():
            _reuse_owned(name, entry)
        for artist_dir in self.artists:
            if artist_dir.name in self.scanned:
                continue
            saved = self.previous_artists.get(artist_dir.name)
            saved_catalog_ids = saved.get("catalog_ids") if saved else None
            if (
                saved
                and saved_catalog_ids is not None
                and saved.get("fingerprint") == self.fingerprints.get(artist_dir.name)
                and (saved.get("artist_id") or not saved.get("candidates"))
                and not _read_without_release_types(saved.get("candidates") or [])
            ):
                candidates = list(saved.get("candidates") or [])
                shown = dropped = 0
                for c in candidates:
                    if _spec_dismissed(c, self.hidden):
                        continue
                    listed = self._listed(c)
                    if _add_candidate_spec(self.job, c, listed=listed) is None:
                        dropped += 1
                    elif listed:
                        shown += 1
                self.total += shown
                self.scanned.add(artist_dir.name)
                self.done += 1
                reused += 1
                if saved.get("artist_id"):
                    self.baseline_seen[str(saved["artist_id"])] = list(saved_catalog_ids or [])
                if dropped:
                    self.left_out[artist_dir.name] = dropped
                else:
                    self.state_artists[artist_dir.name] = {
                        "fingerprint": self.fingerprints.get(artist_dir.name, ""),
                        "candidates": candidates,
                        "artist_id": saved.get("artist_id") or "",
                        "catalog_ids": list(saved_catalog_ids or []),
                        "owned_ids": dict(saved.get("owned_ids") or {}),
                    }
                    _reuse_owned(artist_dir.name, self.state_artists[artist_dir.name])
                hit = ({"artist": artist_dir.name, "albums": shown}
                       if shown else None)
                self.job.push_progress(
                    self._step(self._finding), self.done, self.n, artist_dir.name,
                    found=self.total, hit=hit, unit="artist")
            else:
                self.todo.append(artist_dir)
        if reused:
            log.info(f"  Reused {plural(reused, 'unchanged artist')} from the saved scan.")
        if reused and self.todo:
            # Before any artist is checked, so an interrupted refresh resumes
            # without asking Qobuz about the artists it reused. With nothing to
            # check there is nothing to resume.
            self.checkpoint.save(self.scanned, self.baseline_seen, self.state_artists)

    def _crawl_artists(self):
        workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
        # Resolve/scan artists in parallel (each worker has its own HTTP session),
        # but collect results and write candidates on this one thread so the
        # candidate list and progress stay single-writer.
        missing_premises = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="libscan",
                                **job_mgr.pool_initializer_kwargs()) as ex:
            futures = {ex.submit(_scan_library_artist, ad, self.token, self.partial_only,
                                 self.hidden): ad
                       for ad in self.todo}
            for fut in as_completed(list(futures)):
                # A finished artist's result, with every album Qobuz sent for it,
                # is let go once read rather than kept to the end of the scan.
                ad = futures.pop(fut)
                if self.job.cancel_requested:
                    for f in futures:
                        f.cancel()
                    self.checkpoint.flush()
                    log.info("Cancelled. Stopping scan.")
                    break
                self.done += 1
                try:
                    (name, artist_name, gaps, artist_id, catalog_ids,
                     owned_ids, on_disk) = fut.result()
                except (AuthLost, QobuzUnavailable) as e:
                    # A lost token or an unreachable API isn't a per-artist
                    # hiccup, so cancel the rest and fail the scan rather than
                    # silently report a partial library as the full picture.
                    for f in futures:
                        f.cancel()
                    self.checkpoint.flush()
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
                    self.scan_errors += 1
                    if isinstance(e, OSError):
                        self.read_errors += 1
                        self.unreadable_artists.add(ad.name)
                    log.info(f"    skipped {ad.name}: {e}")
                    self.job.push_progress(
                        self._step(self._finding), self.done, self.n, ad.name,
                        found=self.total, unit="artist")
                    continue
                self.scanned.add(name)
                self.on_disk_ids.update(on_disk)
                if owned_ids:
                    self.owned_qobuz[name] = owned_ids
                if artist_id:
                    self.snapshot_artist_ids[name] = artist_id
                if artist_id and catalog_ids is not None:
                    self.baseline_seen[artist_id] = catalog_ids
                artist_candidates = []
                shown = dropped = 0
                for gap in gaps:
                    # Library is a discovery list, leave candidates unticked so a
                    # single click can't queue hundreds nobody reviewed.
                    spec = _gap_candidate_spec(
                        gap, artist_name or name, selected=False, artist_key=name,
                        missing_premises=missing_premises)
                    if _spec_dismissed(spec, self.hidden):
                        # Saved but not reviewed: the snapshot is the record
                        # Restore brings back from.
                        artist_candidates.append(spec)
                        continue
                    listed = self._listed(spec)
                    if _add_candidate_spec(self.job, spec, listed=listed) is None:
                        dropped += 1
                        continue
                    artist_candidates.append(spec)
                    if listed:
                        self.total += 1
                        shown += 1
                if catalog_ids is not None and dropped:
                    self.left_out[name] = dropped
                elif catalog_ids is not None:
                    self.state_artists[name] = {
                        "fingerprint": self.fingerprints.get(name, ""),
                        "candidates": artist_candidates,
                        "artist_id": artist_id or "",
                        "catalog_ids": list(catalog_ids or []),
                        "owned_ids": dict(owned_ids or {}),
                    }
                # Add the albums before the progress tick so a hit lands the live
                # preview the same moment the running total moves.
                hit = ({"artist": artist_name or name, "albums": shown}
                       if shown else None)
                self.job.push_progress(
                    self._step(self._finding), self.done, self.n,
                    artist_name or name,
                    found=self.total, hit=hit, unit="artist")
                if shown:
                    # A full scan's count holds missing albums and gaps together.
                    tail = "with Gap Fill candidates" if self.partial_only else "to review"
                    log.info(f"  {artist_name} - {plural(shown, 'album')} {tail}")
                self.checkpoint.save(self.scanned, self.baseline_seen, self.state_artists)

    def _save_results(self):
        # Reached here only without an AuthLost/outage abort (that re-raises out
        # above, leaving the checkpoint for resume and not seeding the baseline).
        # job.candidates now holds everything this scan offers for these artists.
        # A left-out artist's rows are missing from this scan for want of room,
        # which says nothing about what it offers.
        self.job.scan_coverage = (
            frozenset(self.scanned - set(self.left_out)), self.partial_only,
            frozenset(self.on_disk_ids))
        self.job.scan_left_out = dict(self.left_out)
        flush_resolve_cache()
        self.baseline_save_failed = False
        self.scan_state_save_failed = False
        self.scan_state_changed = False
        self.snapshot_refused = None
        self.downsample_save_failed = False
        self.upgrade_save_failed = False
        self.catalog_complete = False
        self.publication = None
        if self.job.stopping_for_restart:
            # Not a deliberate stop: the progress stays for the next start, as it
            # would after a crash.
            pass
        elif self.job.cancel_requested:
            # Deliberate stop, discard this kind's progress so it isn't auto-resumed.
            if not self.checkpoint.clear():
                self.job.push_line(
                    "The stopped scan checkpoint could not be cleared. It will be "
                    "ignored unless its artists still match."
                )
            if not self.partial_only:
                generation_state.finish_attempt(self.attempt_id, "cancelled")
        else:
            self._publish_catalog()
            if self.publication is not None:
                self._save_scan_state()
            # Publish Upgrade/Downsample from their OWN completeness, gated only
            # on a complete catalog crawl, NOT on library_complete.
            if self.publication is not None and not self.scan_state_save_failed:
                self._save_disk_views()
            if self.publication is not None and not self.scan_state_save_failed:
                self._finish_published_scan()
            elif self.catalog_complete and self.scan_state_save_failed:
                # Keep a complete resumable copy when the main review could not be
                # saved.
                self.checkpoint.save(self.scanned, self.baseline_seen, self.state_artists)
            elif self.catalog_complete and self.partial_only:
                self.checkpoint.clear()
            elif (self.scanned or self.job.candidates or self.baseline_seen
                    or self.unreadable_artists):
                self.checkpoint.save(self.scanned, self.baseline_seen, self.state_artists)

    def _publish_catalog(self):
        # A crawl that reached every artist it could read stamps "last
        # scanned" and seeds the new-release baseline. A folder that cannot be
        # read is left out and recorded; it has no saved state, so the next
        # refresh after it becomes readable checks it like a new artist.
        readable_names = {ad.name for ad in self.artists} - self.unreadable_artists
        self.catalog_complete = (
            bool(readable_names)
            and readable_names <= set(self.state_artists) | set(self.left_out)
            and readable_names <= self.scanned
            and self.scan_errors - self.discovery_errors - self.read_errors == 0
        )
        if self.catalog_complete and self.unreadable_artists and not self.partial_only:
            self.downsample_refresh = _without_unreadable(
                self.downsample_refresh, self.unreadable_artists)
            self.upgrade_refresh = _without_unreadable(
                self.upgrade_refresh, self.unreadable_artists)
        if self.catalog_complete and self.kind == "missing":
            self.publication = generation_state.commit_catalog_generation(
                self.attempt_id,
                limited=self.job.candidate_cap_hit,
                expected_revision=self.scan_started_revision,
            )
            self.scan_state_save_failed = self.publication is None
            if self.publication is None:
                self.scan_state_changed = (
                    generation_state.revision() != self.scan_started_revision)
                generation_state.finish_attempt(
                    self.attempt_id,
                    "incomplete",
                    "Saved Library state changed while the crawl was running.",
                )
        elif self.catalog_complete:
            partial_revision = generation_state.reserve_revision()
            if partial_revision is not None:
                saved_partial = library_scan_state.save_kind(
                    self.kind,
                    artists=self.state_artists,
                    complete=True,
                    quality_sig=self.quality_sig,
                    revision=partial_revision,
                )
                self.scan_state_save_failed = saved_partial is None
            else:
                self.scan_state_save_failed = True
        else:
            if not self.partial_only:
                generation_state.finish_attempt(
                    self.attempt_id,
                    "incomplete",
                    "One or more artists could not be checked.",
                )

    def _save_scan_state(self):
        scan_generation = int(self.publication["generation"])
        changed_albums = self.publication.get("changed_albums") or []
        if changed_albums:
            self.state_artists = library_scan_state.without_albums(
                self.state_artists, changed_albums)
            for album_id in changed_albums:
                prune_library_review_candidates({"id": album_id})
        scan_revision = generation_state.reserve_revision()
        scan_saved = (
            scan_revision is not None
            and library_scan_state.save_kind(
                self.kind,
                artists=self.state_artists,
                complete=True,
                quality_sig=self.quality_sig,
                generation=scan_generation,
                revision=scan_revision,
                limited=self.job.candidate_cap_hit,
            )
            is not None
        )
        self.scan_state_save_failed = not scan_saved
        if scan_saved:
            for album_id in generation_state.album_changes_since(
                    self.publication["revision"]) or ():
                library_scan_state.remove_album(album_id)
            self.job.execute_args = {
                **(self.job.execute_args or {}),
                "_library_review_generation": scan_generation,
            }
        else:
            generation_state.abort_catalog_generation(
                self.publication,
                "The completed Library crawl could not publish its saved view.",
            )

    def _save_disk_views(self):
        scan_generation = int(self.publication["generation"])
        if self.downsample_refresh.complete:
            self.downsample_save_failed = not downsample_state.save(
                self.downsample_refresh,
                preserve_concurrent=True,
                refresh_started_at=self.downsample_refresh_started_at,
                refresh_started_revision=self.scan_started_revision,
                generation=scan_generation,
            )
            if not self.downsample_save_failed:
                saved_views.sync_badge("downsample")
        if self.upgrade_refresh is not None and self.upgrade_refresh.complete:
            self.upgrade_save_failed = not upgrade_state.save(
                self.upgrade_refresh,
                preserve_concurrent=True,
                refresh_started_at=self.upgrade_refresh_started_at,
                refresh_started_revision=self.scan_started_revision,
                generation=scan_generation,
            )
            if not self.upgrade_save_failed:
                saved_views.sync_badge("upgrade")
                self.job.upgrade_finds = len(_offered_upgrades() - self.offered_before)
                self.job.upgrade_finds_first = not self.offered_before
        elif self.upgrade_refresh is None:
            generation_state.mark_output_status(
                "upgrade",
                "disabled",
                generation=scan_generation,
                reason="Upgrade scanning is disabled.",
            )

    def _finish_published_scan(self):
        scan_generation = int(self.publication["generation"])
        if not self.scan_state_save_failed and not self.cheap_refresh:
            _record_last_scan()
        # What's on disk counts the tag cache's rows, so drop the rows of
        # files moved or deleted outside the app since they were read.
        flac_cache.prune_missing(force=True)
        _flag_new_since_last_scan(self.job, self.kind, found=[
            c for entry in self.state_artists.values()
            for c in entry.get("candidates") or []])
        # The crawl reached every artist cleanly, establish the new-release
        # baseline from the catalog snapshot and clear this kind's
        # checkpoint. A crawl that read every catalogue counts as the last
        # new-release check; a refresh reuses most of them.
        self.baseline_save_failed = not new_releases_mod.seed_baseline(
            self.baseline_seen,
            generation=scan_generation,
            checked_at=None if self.cheap_refresh else time.time(),
        )
        if not self.checkpoint.clear():
            self.job.push_line(
                "The completed scan checkpoint could not be cleared. Its "
                "saved generation is complete, so it will not be treated "
                "as newer results."
            )
        self.snapshot_refused = _write_collection_snapshot(
            self.job, self.owned_qobuz, self.snapshot_artist_ids)

    def _write_summary(self):
        if self.job.cancel_requested and any(
                other.execute_kind == "library" and other is not self.job
                for other in job_mgr.registry.awaiting_review()):
            # Its count holds the rows carried over from the saved scan, and a
            # stopped refresh folds none of it in.
            self.job.summary = "Stopped early. The open Library review is unchanged."
        elif self.job.cancel_requested:
            self.job.summary = (f"Stopped early. {plural(self.total, 'album')} found so far."
                                if self.total else "Stopped before anything turned up.")
        elif self.total:
            self.job.summary = library_review_summary(self.job.candidates) + "."
            if self.left_out:
                self.job.summary += " " + left_out_note(self.left_out, self.job.CANDIDATE_CAP)
        elif self.partial_only:
            self.job.summary = "No owned albums are missing tracks."
        else:
            self.job.summary = "No missing albums found for artists in your library."
        if getattr(self.job, "upgrade_finds", 0) and not self.job.cancel_requested:
            self.job.summary += " " + upgrade_finds_note(
                self.job.upgrade_finds, first=getattr(self.job, "upgrade_finds_first", False))
        # Artists that errored or came back with a short catalog page aren't in
        # state_artists; the checkpoint stays for them and the last-scan stamp is
        # withheld.
        unchecked = (len(self.artists) - len(self.state_artists)
                     - len(self.left_out) + self.discovery_errors)
        if not self.job.cancel_requested and not self.partial_only and self.publication is not None:
            unreadable_artists_mod.record(self.unreadable_artists)
        if (not self.job.cancel_requested and self.publication is not None
                and unchecked == len(self.unreadable_artists) and self.unreadable_artists):
            # Finished for everything readable: the rest is not a resume.
            self.job.summary += (
                f" {_left_out_note(self.unreadable_artists)} "
                + ("Each is checked by itself once it can be read."
                   if cfg.AUTO_LIBRARY_SCAN
                   else "Scan again once it can be read."
                   if len(self.unreadable_artists) == 1
                   else "Scan again once they can be read."))
        if (not self.job.cancel_requested and unchecked > 0
                and self.publication is not None
                and unchecked == len(self.unreadable_artists)):
            # Left out rather than unchecked: nothing here is waiting to resume.
            _record_unchecked_artists(self.job, 0)
        elif not self.job.cancel_requested and unchecked > 0:
            _record_unchecked_artists(self.job, unchecked)
            self.job.summary += f" {plural(unchecked, 'artist')} couldn't be checked"
            if self.unreadable_artists:
                self.job.summary += (f" ({len(self.unreadable_artists)} unreadable: "
                                     f"{_first_names(self.unreadable_artists)})")
            self.job.summary += "; scan again to resume from where it left off."
        stale_tabs = [
            label for label, refresh, save_failed in (
                ("Upgrade", self.upgrade_refresh, self.upgrade_save_failed),
                ("Downsample", self.downsample_refresh, self.downsample_save_failed),
            )
            if refresh is not None and not (
                self.catalog_complete
                and refresh.complete
                and not self.scan_state_save_failed
                and not save_failed
            )
        ]
        caveats = []
        if not self.job.cancel_requested and stale_tabs:
            caveats.append(
                f"{' and '.join(stale_tabs)} results were not updated because "
                "this scan did not finish cleanly."
            )
        if not self.job.cancel_requested and (skipped := undecodable_named()):
            caveats.append(undecodable_note(skipped))
        if not self.job.cancel_requested and not self.partial_only and (
                no_artist := new_releases_mod.unresolved_note(
                    [name for name, entry in self.state_artists.items()
                     if not entry.get("artist_id")])):
            log.info(no_artist)
        if self.baseline_save_failed:
            caveats.append("New releases could not be updated; the next "
                           "complete scan tries again.")
        if self.snapshot_refused:
            caveats.append(self.snapshot_refused)
        if self.scan_state_changed:
            caveats.append("Albums changed while the refresh ran, so its "
                           "results were not saved; refresh again to save them.")
        elif self.scan_state_save_failed:
            caveats.append("The saved scan state couldn't be written; scan again "
                           "before restarting the app.")
        # A refresh folded into an open review keeps these after its counts.
        self.job.scan_caveats = caveats
        if caveats:
            self.job.summary += " " + " ".join(caveats)
        # The crawl is stamped complete before its saved views are written, so a
        # view that never lands would leave the attempt reading "complete" and the
        # page showing a clean review. Correct the record with what is missing,
        # which is what puts the warning on Library.
        unsaved = list(stale_tabs)
        if self.baseline_save_failed:
            unsaved.append("New releases")
        if (unsaved and self.attempt_id is not None and self.publication is not None
                and not self.scan_state_save_failed and not self.job.cancel_requested):
            named = (" and ".join(unsaved) if len(unsaved) < 3
                     else ", ".join(unsaved[:-1]) + " and " + unsaved[-1])
            generation_state.finish_attempt(
                self.attempt_id, "incomplete", f"{named} results were not updated.")
        log.info(self.job.summary)


def _offered_upgrades():
    # By Qobuz album, so a folder renamed outside the app is not a new find.
    return {(c.get("payload") or {}).get("album_id")
            or (c.get("payload") or {}).get("album_dir")
            for c in upgrade_state.visible_candidates()}


def undecodable_note(paths):
    one = len(paths) == 1
    names = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more"
                                    if len(paths) > 3 else "")
    cause, fix = undecodable_cause(cfg.MUSIC_ROOT, "it" if one else "them")
    return (f"Skipped {names}: {'its name is' if one else 'their names are'} "
            f"not valid UTF-8{cause}, so {'it reads' if one else 'they read'} "
            f"as missing. {fix}")


def upgrade_finds_note(n, first=False):
    return f"{plural(n, 'album' if first else 'new album')} can be upgraded."


def _newest_parked(kind, exclude=None):
    """The newest review of ``kind`` still waiting to be worked through, if
    any. ``exclude`` skips a job that is running and not parked yet."""
    parked = None
    for j in job_mgr.registry.awaiting_review():
        if j.execute_kind != kind:
            continue
        if exclude is not None and j.id == exclude.id:
            continue
        if parked is None or (j.created_at or 0) > (parked.created_at or 0):
            parked = j
    return parked


def _new_release_rows_to_recheck(job):
    """The waiting New releases rows that Download refuses because their
    artist folder changed after they were listed, or because they were
    sealed by an older version: album id to row. A check asks about these
    as if it had never seen them, so each comes back sealed afresh or
    leaves the review."""
    parked = _newest_parked("new_releases", exclude=job)
    if parked is None:
        return {}
    with parked._lock:
        rows = list(parked.candidates)
    captures = {}
    stale = {}
    for c in rows:
        album_id = str((c.get("payload") or {}).get("album_id") or "")
        if not album_id or candidate_premise.expected_kind(c) != "missing":
            continue
        try:
            candidate_premise.validate(c, missing_premises=captures)
        except CandidateStale as exc:
            if exc.cause in ("changed", "older"):
                stale[album_id] = c
    return stale


def _append_to_parked_new_release_review(job, found=0, retire=()):
    """Fold this check's finds into the new-release list already waiting.

    A second list beside the first would split one person's picks across two
    pages and leave the ticks and dismissals they had already made stranded on
    the older one. Folding keeps every earlier choice: a row the user dismissed
    while this ran is checked against a fresh hidden snapshot and not brought
    back, and a row already in the list keeps its tick. ``found`` counts this
    check's new releases; the rest of its rows were already listed and were
    checked again, as were the rows ``retire`` names by fold key, which leave.
    This job then finishes as a plain result pointing at the list it fed,
    never as a review of its own. Returns True when the finds were folded."""
    parked = _newest_parked("new_releases", exclude=job)
    if parked is None:
        return False
    added = refold_into_living_review(
        list(job.candidates), execute_kind="new_releases", ticked=False,
        retire=retire)
    if added is None or added is False:
        return False
    with job._lock:
        job.candidates = []
    waiting = len(parked.candidates)
    if added or retire:
        # Through persist_review_mutation, not a bare assignment: the fold was
        # already written with the old summary, so a summary left in memory
        # only would come back after a restart claiming the count the review
        # had BEFORE the fold. A save that fails restores the old text rather
        # than leaving disk and screen disagreeing.
        restated = f"{plural(waiting, 'new release')} waiting to review" + (
            f", {added} of them from the latest check." if added else ".")
        job_persistence.persist_review_mutation(
            parked, lambda: setattr(parked, "summary", restated))
    if added:
        job.summary += f" {waiting:,} now waiting to review."
        job_mgr._start_post_job_hook_for_job(parked)
    elif found:
        job.summary += " All were already in the New releases review."
    parked.notify_review_changed()
    job.status = job_mgr.JobStatus.DONE
    return True


_NEW_RELEASE_LATE_LISTING = 14 * 86400


class _NewReleasesRun:
    """One check of the library artists' Qobuz catalogs for new releases."""

    def __init__(self, job, token):
        self.job = job
        self.token = token

    def run(self):
        if not self._list_artists():
            return
        self._load_baseline()
        self._start_check()
        self._crawl()
        self._save_baseline()
        self._note_unchecked()
        if self.job.cancel_requested:
            self._summarize_stop()
            return
        self._summarize()
        self._note_rechecked()
        self._finish()

    def _artist_read_failed(self, artist_dir, error):
        if normalize(artist_dir.name) not in VA_NORMALIZED:
            self.unreadable_artists[artist_dir.name] = str(error)

    def _list_artists(self):
        clear_scan_caches()
        self.unreadable_artists = {}
        # Same VA exclusion as scan_library: the Various-Artists folder has no single
        # Qobuz catalog, so it can't yield meaningful "new releases".
        self.artists = [d for d in list_library_artists(on_artist_error=self._artist_read_failed)
                        if normalize(d.name) not in VA_NORMALIZED]
        if not self.artists and not self.unreadable_artists:
            _set_empty_library_summary(self.job)
            return False
        if not self.artists:
            self.job.summary = (
                f"No artist folder could be read "
                f"({_first_names(self.unreadable_artists)}), so nothing was checked.")
            _mark_job_failed(self.job)
            return False
        return True

    def _load_baseline(self):
        state = new_releases_mod.load()
        self.seen = state.get("seen") or {}
        self.window = int(cfg.NEW_RELEASE_MAX_AGE_DAYS)
        self.capped_ids = frozenset(
            artist_id for artist_id, days in (state.get("capped_recent") or {}).items()
            if days >= self.window)
        # A big artist's unseen release counts as new from the last check on,
        # with slack for a release Qobuz lists some days after its date.
        checked_at = state.get("checked_at") or (
            time.time() - max(0, int(cfg.NEW_RELEASE_CHECK_INTERVAL)))
        self.new_since = checked_at - _NEW_RELEASE_LATE_LISTING
        # If the catalog fetch limit has grown since the baseline was captured,
        # the old baseline is missing everything past the previous cap, a plain
        # diff would dump that whole back-slice as "new".
        self.cur_limit = int(cfg.ARTIST_CATALOG_LIMIT)
        prev_limit = state.get("baseline_limit")
        self.rebaseline = prev_limit is None or self.cur_limit > int(prev_limit)
        # A waiting row that Download refuses because its artist folder changed
        # is asked about again as if never seen: found again, it is sealed
        # afresh and keeps its tick; not offered any more, it leaves the review.
        self.recheck = {} if self.rebaseline else _new_release_rows_to_recheck(self.job)
        self.asked = self.seen
        if self.recheck:
            self.asked = {artist_id: [a for a in ids if a not in self.recheck]
                          for artist_id, ids in self.seen.items()}
        self.hidden = hidden_mod.load()
        self.single_store = self.hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None
        # A new release shows only where the Missing Albums switches would list it.
        switched_on = missing_types.load()
        self.opts = DiscoveryOpts(
            prefer_hires=cfg.PREFER_HIRES,
            include_live=missing_types.LIVE in switched_on,
            include_comps=missing_types.COMPILATION in switched_on,
            include_singles=missing_types.EP in switched_on,
            include_other=missing_types.OTHER in switched_on,
        )

    def _start_check(self):
        log.info(f"Checking {plural(len(self.artists), 'artist')} for new releases…")
        self.total = 0
        self.done = 0
        # Unreadable folders are left out, as a Library scan leaves them: checking
        # again can't read them, so they don't make the check fail.
        self.failed_count = 0
        self.failed_names = []
        # Folders Qobuz has no artist for at all. Kept apart from failed_count:
        # they are a settled answer, so they must not hold the baseline back or
        # push the user towards a retry that returns the same result.
        self.unresolved_names = []
        self.n = len(self.artists)
        self.workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
        # This run's reached artists; merged over the prior baseline at the end (so a
        # run where some/all artists errored can't wipe their baselines and re-surface
        # everything, only artists actually reached get their snapshot refreshed).
        self.current_seen = {}
        self.capped_recent = {}
        self.missing_premises = {}
        self.listed_ids = set()
        self.reached = set()
        self.refound = set()

    def _crawl(self):
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="newrel",
                                **job_mgr.pool_initializer_kwargs()) as ex:
            futures = {ex.submit(find_new_releases_for_artist, ad.name, token=self.token,
                                 opts=self.opts, seen_by_id=self.asked, hidden=self.hidden,
                                 single_store=self.single_store, artist_dir=ad,
                                 baseline_only=self.rebaseline,
                                 capped_ids=self.capped_ids,
                                 new_since=self.new_since): ad
                       for ad in self.artists}
            for fut in as_completed(list(futures)):
                ad = futures.pop(fut)
                if self.job.cancel_requested:
                    for f in futures:
                        f.cancel()
                    log.info("Cancelled. Stopping check.")
                    break
                self.done += 1
                try:
                    result = fut.result()
                except (AuthLost, QobuzUnavailable):
                    for f in futures:
                        f.cancel()
                    raise
                except Exception as e:
                    self.failed_count += 1
                    self.failed_names.append(ad.name)
                    log.info(f"    skipped {ad.name}: {e}")
                    self.job.push_progress("Checking for new releases", self.done, self.n,
                                           ad.name, found=self.total, unit="artist")
                    continue
                self._take_result(ad, result)

    def _take_result(self, ad, result):
        if result.artist_id and not getattr(result, "fetch_failed", False):
            self.current_seen[result.artist_id] = result.current_ids
            self.reached.add(ad.name)
            if getattr(result, "capped_recent", False):
                self.capped_recent[result.artist_id] = self.window
        elif getattr(result, "unresolved", False):
            self.unresolved_names.append(ad.name)
        elif not getattr(result, "by_lead", False):
            # A short or failed catalogue fetch is unchecked, not a clean
            # zero-release result.
            self.failed_count += 1
            self.failed_names.append(ad.name)
        new_gaps = []
        for gap in result.new_gaps:
            # Two folders of one Qobuz artist both see its new release.
            album_id = str(gap.qobuz_album.get("id") or "")
            if album_id and album_id in self.listed_ids:
                continue
            self.listed_ids.add(album_id)
            new_gaps.append(gap)
        found = 0
        for gap in new_gaps:
            # Leave new releases UN-ticked, like the library gap list: a
            # review is for picking, and one tap must never queue the whole
            # list of rips. The "new" badge still flags them for the eye.
            _add_gap_candidate(
                self.job,
                gap,
                result.artist_name,
                selected=False,
                is_new=True,
                artist_key=ad.name,
                missing_premises=self.missing_premises,
            )
            album_id = str(gap.qobuz_album.get("id") or "")
            if album_id in self.recheck:
                self.refound.add(album_id)
            else:
                found += 1
        self.total += found
        hit = ({"artist": result.artist_name, "albums": found}
               if found else None)
        self.job.push_progress("Checking for new releases", self.done, self.n,
                               result.artist_name or ad.name,
                               found=self.total, hit=hit, unit="artist")
        if found:
            log.info(f"  {result.artist_name} - "
                     f"{plural(found, 'new release')}")

    def _save_baseline(self):
        flush_resolve_cache()
        self.saved = None
        if not self.job.cancel_requested:
            # UNION each reached artist's snapshot into the prior baseline rather
            # than replacing it.
            complete = self.failed_count == 0 and bool(self.current_seen)
            self.saved = new_releases_mod.mark_run(
                self.current_seen,
                started_from=self.seen,
                complete=complete,
                baseline_limit=self.cur_limit if complete else None,
                capped_recent=self.capped_recent,
            )

    def _note_unchecked(self):
        if self.failed_count:
            _record_unchecked_artists(self.job, self.failed_count)
        shown = ", ".join(sorted(self.failed_names, key=str.casefold)[:3])
        if len(self.failed_names) > 3:
            shown += f" and {len(self.failed_names) - 3:,} more"
        self.unchecked = plural(self.failed_count, "artist") + (f" ({shown})" if shown else "")
        if no_artist := new_releases_mod.unresolved_note(self.unresolved_names):
            log.info(no_artist)
        self.skipped_note = ""
        if self.unreadable_artists:
            self.skipped_note = _left_out_note(self.unreadable_artists)

    def _summarize_stop(self):
        # A cancelled crawl only reached a fraction of the artists, so it can't
        # claim "No new releases" or "First check recorded" definitively.
        self.job.summary = ("Stopped early. Partial check, "
                            f"{plural(self.total, 'new release')} found so far.")
        if self.failed_count:
            self.job.summary += (
                f" {self.unchecked} couldn't be checked "
                "before the stop."
            )
        if self.skipped_note:
            self.job.summary += f" {self.skipped_note}"
        log.info(self.job.summary)

    def _summarize(self):
        if self.rebaseline and self.seen:
            if self.saved is False:
                self.job.summary = ("What Qobuz has now couldn't be saved. Check "
                                    "again to retry it.")
                if self.failed_count:
                    self.job.summary += (f" {self.unchecked} also "
                                         "couldn't be checked.")
            elif self.failed_count:
                self.job.summary = (f"{self.unchecked} couldn't be "
                                    "checked; check again to retry.")
            else:
                self.job.summary = ("The catalogue limit changed, so this check "
                                    "recorded what Qobuz has now. Later checks list "
                                    "what is new.")
        elif self.total:
            self.job.summary = f"{plural(self.total, 'new release')} found."
            if self.failed_count:
                self.job.summary += (f" {self.unchecked} couldn't be "
                                     "checked; check again for a complete result.")
            if self.saved is False:
                self.job.summary += (" This check couldn't be saved, so these "
                                     "releases may appear again.")
        elif self.failed_count:
            self.job.summary = ("No new releases from the artists that could be checked. "
                                f"{self.unchecked} couldn't be checked; "
                                "check again for a complete result.")
            if self.saved is False:
                self.job.summary += " This check couldn't be saved."
        elif not self.seen:
            if self.saved is False:
                self.job.summary = ("The current Qobuz catalogue was checked but "
                                    "couldn't be saved. Check again.")
            else:
                self.job.summary = ("First check complete. Later checks list albums "
                                    "added to Qobuz after it.")
        elif self.saved is False:
            self.job.summary = ("No new releases found, but this check couldn't be "
                                "saved. Check again.")
        else:
            self.job.summary = "No new releases."

    def _note_rechecked(self):
        self.retire = {fold_key(c) for album_id, c in self.recheck.items()
                       if album_id not in self.refound
                       and ((c.get("payload") or {}).get("_artist_dir") in self.reached
                            or _folder_gone(c))}
        changed = not self._failed_empty()
        if changed and self.refound:
            one = len(self.refound) == 1
            self.job.summary += (
                f" {plural(len(self.refound), 'album')} already in the New releases "
                f"review {'was' if one else 'were'} checked again after "
                f"{'its artist folder' if one else 'their artist folders'} "
                "changed.")
        if changed and self.retire:
            one = len(self.retire) == 1
            self.job.summary += (
                f" {plural(len(self.retire), 'album')} left the New releases review "
                f"because {'its artist folder' if one else 'their artist folders'}"
                f" changed and this check no longer offers "
                f"{'it' if one else 'them'}.")
        if self.skipped_note:
            self.job.summary += f" {self.skipped_note}"

    def _failed_empty(self):
        return not self.total and (self.failed_count or self.saved is False)

    def _finish(self):
        if self._failed_empty():
            # With no reviewable finds, submit_scan would otherwise replace this
            # explicitly incomplete result with a green Done state. Keep partial
            # success reviewable when it found albums, but never present an
            # unchecked or unsaved zero as a completed zero. job.summary above
            # already names the reason and the remedy; a separate job.error would
            # only restate it, and the FAILED status carries its own marker.
            _mark_job_failed(self.job)
        elif self.total or self.refound or self.retire:
            # An earlier check's list may still be parked and half-worked. Join it
            # rather than parking a rival list beside it.
            _append_to_parked_new_release_review(self.job, found=self.total, retire=self.retire)
        # Not logged here: job.summary already renders on the job page, and the
        # activity log below it would otherwise repeat the exact same sentence.


def scan_new_releases(job, token):
    """Surface albums that appeared in library artists' Qobuz catalogs since the
    last check and that the user doesn't own or hasn't hidden, flagged as new
    for review, but left un-ticked so one click can't queue the whole list.
    Cheap (about one call per artist, no track fetches), so it's the quick
    "what's new" pass rather than the full gap scan."""
    _NewReleasesRun(job, token).run()


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


def _denied_folder_cause(exc):
    if (isinstance(exc, OSError)
            and exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
            and isinstance(exc.filename, str) and os.path.isabs(exc.filename)):
        return _permission_cause(os.path.dirname(exc.filename))
    return ""


def _permission_cause(folder):
    folder, _is_host = storage._resolve_host_path(folder)
    return f"permission denied in {folder}"


def _download_failure_cause(result):
    denied = result.get("folder_denied") or result.get("import_denied")
    return _permission_cause(denied) if denied else failure_cause(result)


def execute_albums(job, chosen, token):
    """Download each selected album via the normal process_album path."""
    _AlbumsRun(job, chosen, token).run()


_NEXT_ALBUM = object()


class _AlbumsRun:
    """One run of the chosen albums, each through process_album in turn."""

    def __init__(self, job, chosen, token):
        self.job = job
        self.chosen = chosen
        self.token = token

    def run(self):
        self._reset_run_counts()
        self._start()
        for i, cand in enumerate(self.chosen, 1):
            self.i, self.cand = i, cand
            if self.job.cancel_requested:
                break
            self._run_album()
        self.job._progress_scope = None
        if self.job.cancel_requested and (
                self.cancelled_cand is not None or self.processed < len(self.chosen)):
            self._finish_stopped_early()
            return
        self._write_summary()
        self._partial_track_messages()
        self._return_failed_picks()
        self._failure_message()
        self._finish()

    def _run_album(self):
        self._start_album()
        if self._fetch_album() is _NEXT_ALBUM:
            return
        if self._check_and_download() is _NEXT_ALBUM:
            return
        self._classify_result()
        time.sleep(cfg.ARTIST_API_DELAY)

    def _reset_run_counts(self):
        # The web worker runs jobs back-to-back; a directory listing cached by
        # a previous job would otherwise be reused even though folders may
        # have moved since.
        clear_scan_caches()
        with self.job._lock:
            self.job.execute_args = {**{key: value
                                        for key, value in (self.job.execute_args or {}).items()
                                        if key not in (_WRITTEN_INTO, _IMPORTED_INTO,
                                                       job_persistence.ALBUMS_DONE)},
                                     job_persistence.ALBUMS_PICKED: len(self.chosen)}

    def _start(self):
        self.args = build_args()
        self._benign = {"already_complete", "skipped_already_higher_quality", "dry_run",
                        "user_skipped", "lossy_only", "no_tracks", "skipped_has_extras",
                        "cancelled"}
        self.ok = 0
        self.partial = 0
        self.retryable_tracks = 0
        self.lossy_only_tracks = 0
        self.unavailable_tracks = 0
        self.attention_counts = {
            "backup": 0,
            "quality": 0,
            "processing": 0,
            "partial": 0,
        }
        self.failed = 0
        # Failed at the fetch step specifically, before any of process_album's own
        # attention reasons could apply. Tracked apart so the summary can give a
        # plain reason instead of leaving it to the raw exception in the log.
        self.fetch_failed = 0
        self.skipped = 0
        self.processed = 0
        # The summary names the albums, not just how many.
        self.downloaded = []
        self.partial_cands = []
        self.skipped_cands = []
        # Picks that didn't land (never fetched, errored, or came back empty).
        self.failed_cands = []
        self.failure_causes = []
        self.cancelled_cand = None
        # Fold-back recovery is kind-scoped: this function executes both library
        # and new-release batches, and their picks must never cross into each
        # other's review, a library pick returns to the Library review, a
        # new-release pick to the New Releases review (it stays there until
        # downloaded or dismissed).
        self.is_library_run = getattr(self.job, "execute_kind", "") == "library"
        self.is_nr_run = getattr(self.job, "execute_kind", "") == "new_releases"
        self.is_restore_run = getattr(self.job, "execute_kind", "") == "collection_restore"
        self.review_kind = (self.job.execute_kind
                            if self.is_library_run or self.is_nr_run or self.is_restore_run else None)
        self.review_args = self.job.execute_args if self.is_restore_run else None
        self.review_save_failed = False
        self.retry_save_failed = False

    def _remember_review_save(self, saved, *, retry=True):
        if saved is not False or self.review_save_failed:
            return
        self.review_save_failed = True
        self.retry_save_failed = retry
        if retry:
            self.job.push_line(
                "Retry choices could not be saved back to their review. "
                "Check the data folder, then refresh before retrying."
            )
        else:
            self.job.push_line(
                "The finished Library review could not be saved. Check the "
                "data folder, then refresh before continuing."
            )

    def _apply_non_track_attention(self):
        messages = []
        if self.attention_counts["backup"]:
            one = self.attention_counts["backup"] == 1
            messages.append(
                f"{plural(self.attention_counts['backup'], 'album')} "
                "kept a backup because "
                f"{'its' if one else 'their'} replacement or recovery "
                f"could not be verified. Review {'it' if one else 'them'} "
                "under Settings → Diagnostics before downloading "
                f"{'that album' if one else 'those albums'} again."
            )
            self.job.attention = "backup"
        if self.attention_counts["quality"]:
            messages.append(
                f"{plural(self.attention_counts['quality'], 'album')} remained "
                "below the target quality."
            )
            if not self.job.attention:
                self.job.attention = "quality"
        if self.attention_counts["processing"]:
            messages.append(
                f"{plural(self.attention_counts['processing'], 'imported album')} "
                f"{'has' if self.attention_counts['processing'] == 1 else 'have'} "
                "a step after the download that did not finish; see the "
                "job log."
            )
            if not self.job.attention:
                self.job.attention = "processing"
        if self.attention_counts["partial"]:
            messages.append(
                f"{plural(self.attention_counts['partial'], 'imported album')} "
                "reported unfinished work; see the job log."
            )
            if not self.job.attention:
                self.job.attention = "partial"
        if messages:
            self.job.error = " ".join(
                part for part in (self.job.error, *messages) if part
            )

    def _fold_back_unfinished(self, *, always=False):
        # Token death / outage mid-batch: once something has imported, this
        # job is headed for FAILED (approve's no-harm re-park only covers the
        # nothing-landed case), fold the picks this run didn't finish back
        # into the living review, ticked, so they come back to retry like any
        # other early exit. A full disk has no re-park, so it always folds.
        if not self.job._imported_any and not always:
            return
        leftovers = _resealed_past_own_imports(
            self.job, self.failed_cands + [self.cand] + self.chosen[self.processed:])
        if self.review_kind:
            self._remember_review_save(
                _return_picks(leftovers, self.review_kind, self.review_args))

    def _disk_full_summary(self):
        parts = []
        if self.ok:
            parts.append(f"{plural(self.ok, 'album')} downloaded")
        if self.partial:
            parts.append(f"{plural(self.partial, 'album')} partly downloaded")
        attention_total = sum(self.attention_counts.values())
        if attention_total:
            parts.append(f"{plural(attention_total, 'album')} "
                         f"{'needs' if attention_total == 1 else 'need'} attention")
        self.job.summary = "Stopped: the music disk is full. " + (
            ", ".join(parts) + "." if parts else "Nothing was downloaded.")
        if self.review_kind:
            destination = ("Library" if self.is_library_run else
                           "New releases" if self.is_nr_run else "Restore review")
            self.job.summary += (
                f" The rest could not be saved back to {destination}."
                if self.retry_save_failed else
                f" The rest are selected for retry in {destination}."
            )

    def _start_album(self):
        self.processed = self.i
        self.album_id = self.cand["payload"].get("album_id")
        label = f"[{self.i}/{len(self.chosen)}] {self.cand.get('artist','')} - {self.cand['title']}"
        log.info(label)
        album = " · ".join(filter(None, (self.cand.get("artist"), self.cand["title"])))
        self.job._progress_scope = (self.i - 1, len(self.chosen), "album", album)
        self.job.push_progress("Downloading albums", self.i - 1, len(self.chosen), album,
                               unit="album")

    def _fetch_album(self):
        try:
            self.full = get_album(self.album_id, self.token)
        except (AuthLost, QobuzUnavailable):
            self._fold_back_unfinished()
            raise
        except Exception as e:
            log.info(f"  could not fetch album {self.album_id}: {e}")
            self.failed += 1
            self.fetch_failed += 1
            self.failed_cands.append(self.cand)
            return _NEXT_ALBUM

    def _check_and_download(self):
        _note_staging_wait(self.job, "Downloading albums", self.i - 1, len(self.chosen))
        self.exact_edition = bool(
            self.is_restore_run or self.is_nr_run or not is_gap_candidate(self.cand)
            or self.cand["payload"].get("exact_edition"))
        try:
            with job_mgr.staging_lock():
                if candidate_premise.expected_kind(self.cand) == "missing":
                    premise = candidate_premise.validate_container(self.cand)
                    # Earlier albums in this same batch may have changed the
                    # artist tree. That is safe only while this exact edition
                    # still has no local folder of its own.
                    clear_scan_caches()
                    _existing, found, _missing, present = catalog.find_edition_tracks(self.full)
                    # A folder holding none of this edition's tracks is
                    # another album that shares its title, such as an
                    # artist's second self-titled record.
                    if found is not None and present:
                        raise CandidateStale(
                            "This missing album appeared locally after the "
                            "review was built. Refresh the review."
                        )
                else:
                    premise = candidate_premise.validate(self.cand)
                _note_written_into(self.job, self.cand)
                self.result = process_mode.process_album(self.full, self.args, allow_force=False,
                                       already_confirmed=True, token=self.token,
                                       exact_edition=self.exact_edition,
                                       expected_album_receipt=(
                                           premise["receipt"]
                                           if premise["kind"] == "gap-fill"
                                           else None
                                       ),
                                       expected_gap_fill_receipts=(
                                           self.cand["payload"].get(
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
            self._fold_back_unfinished(always=disk_full)
            if disk_full:
                self._disk_full_summary()
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            self.failed += 1
            self.failed_cands.append(self.cand)
            self.failure_causes.append(_denied_folder_cause(e))
            return _NEXT_ALBUM

    def _classify_result(self):
        if self.result and self.result.get("result") == "cancelled":
            # Cancelled mid-download: processed already points past this album,
            # so the cancel fold-back below would drop its pick, remember it.
            self.cancelled_cand = self.cand
        attention_kind = download_attention_kind(self.result or {})
        if attention_kind == "backup" and not (self.result or {}).get("imported"):
            # Keep unresolved backups out of the retry review. Diagnostics
            # owns the next decision.
            self.attention_counts["backup"] += 1
            self.job._imported_any = True
            _settle_pick(self.job, self.cand)
        elif self.result and self.result.get("imported") and self.result.get("n_ok", 0) > 0:
            self.job._imported_any = True
            _settle_pick(self.job, self.cand, self.result.get("dir"))
            _note_album_done(self.job)
            _refresh_after_local_album_change(
                self.full,
                self.result,
                fallback_artist=self.cand.get("artist"),
                token=self.token,
                args=self.args,
                claims_album=True,
            )
            # The album is on disk now, any OTHER parked library review still
            # offering it is stale.
            prune_library_review_candidates(self.full)
            # A partial (some tracks landed, some failed) isn't a full
            # download, count it apart so the summary doesn't claim it
            # finished, and surface the remainder NOW (after the prune, which
            # would otherwise drop the fresh candidate as a same-id stale one)
            # instead of leaving it invisible until the next manual refresh.
            retryable, unretryable = incomplete_track_counts(self.result)
            if attention_kind == "backup":
                self.attention_counts["backup"] += 1
            elif retryable or unretryable:
                _, _, lossy_only, unavailable = track_shortfall(self.result)
                self.partial += 1
                self.partial_cands.append(self.cand)
                self.retryable_tracks += retryable
                self.lossy_only_tracks += lossy_only
                self.unavailable_tracks += unavailable
                if (self.is_library_run or self.is_restore_run or self.is_nr_run) and retryable:
                    self._remember_review_save(_fold_partial_gap_fill(
                        self.full, self.cand.get("artist") or "", retryable,
                        album_dir=self.result.get("dir"), exact_edition=self.exact_edition,
                        park_when_absent=True))
            elif attention_kind:
                self.attention_counts[attention_kind] += 1
                if attention_kind == "quality":
                    job_mgr.record_quality_shortfall(
                        self.job, self.result.get("quality_verdict")
                    )
            else:
                self.ok += 1
                self.downloaded.append(self.cand)
        elif self.result and self.result.get("result") in self._benign:
            if self.result.get("result") != "cancelled":
                self.skipped += 1
                self.skipped_cands.append(self.cand)
        else:
            self.failed += 1
            self.failed_cands.append(self.cand)
            self.failure_causes.append(_download_failure_cause(self.result or {}))

    def _finish_stopped_early(self):
        # Nothing this run leaves behind is lost: the picks it never started
        # AND the ones that failed along the way fold back into the living
        # review, ticked, so a cancel mid-batch doesn't strand them in this
        # dead job. When the whole review was ticked at approval there is no
        # living review to fold into, so park a fresh one, the finish path
        # and the new-release side both already do that.
        unrun = self.chosen[self.processed:]
        if self.cancelled_cand is not None:
            unrun = [self.cancelled_cand] + unrun
        leftovers = _resealed_past_own_imports(self.job, self.failed_cands + unrun)
        if leftovers and self.review_kind:
            self._remember_review_save(
                _return_picks(leftovers, self.review_kind, self.review_args))
        parts = []
        if self.downloaded:
            parts.append(f"{named_albums(self.downloaded)} downloaded")
        if self.partial:
            parts.append(f"{named_albums(self.partial_cands)} partly downloaded")
        attention_total = sum(self.attention_counts.values())
        if attention_total:
            parts.append(f"{plural(attention_total, 'album')} "
                         f"{'needs' if attention_total == 1 else 'need'} attention")
        if self.failed:
            parts.append(f"{plural(self.failed, 'album')} failed")
        if self.skipped:
            parts.append(f"{named_albums(self.skipped_cands)} skipped")
        self.job.summary = "Stopped early." + (
            " " + ", ".join(parts) + "." if parts else "")
        destination = ("Library" if self.is_library_run else
                       "New releases" if self.is_nr_run else "Restore review")
        if leftovers and self.review_kind:
            names = named_albums(leftovers)
            if self.retry_save_failed:
                self.job.summary += (f" {names} could not be saved back to "
                                     f"{destination}.")
            else:
                self.job.summary += (
                    f" {names} {'is' if len(leftovers) == 1 else 'are'} back "
                    f"in {destination}, ticked, to download again."
                )
        if self.retry_save_failed:
            self.job.error = (
                "Retry choices could not be saved. Check the data folder, "
                "then refresh before retrying."
            )
            self.job.attention = self.job.attention or "review"
        self._apply_non_track_attention()
        log.info(self.job.summary)

    def _write_summary(self):
        if self.ok:
            parts = [f"{named_albums(self.downloaded)} downloaded"]
        elif self.partial:
            parts = []
        elif sum(self.attention_counts.values()):
            parts = []
        else:
            parts = ["No albums downloaded or imported"]
        if self.partial:
            parts.append(f"{named_albums(self.partial_cands)} partly downloaded")
        if self.skipped and (self.ok or self.partial):
            parts.append(f"{named_albums(self.skipped_cands)} skipped")
        if self.attention_counts["backup"]:
            parts.append(
                f"{plural(self.attention_counts['backup'], 'album')} kept "
                "a backup for review"
            )
        if self.attention_counts["quality"]:
            parts.append(
                f"{plural(self.attention_counts['quality'], 'album')} finished below "
                "target quality"
            )
        if self.attention_counts["processing"]:
            parts.append(
                f"{plural(self.attention_counts['processing'], 'imported album')} "
                f"{'has' if self.attention_counts['processing'] == 1 else 'have'} "
                "a step after the download that did not finish"
            )
        if self.attention_counts["partial"]:
            parts.append(
                f"{plural(self.attention_counts['partial'], 'imported album')} reported "
                "unfinished work"
            )
        self.has_issues = bool(self.partial or self.failed or sum(self.attention_counts.values()))
        self.job.summary = ", ".join(parts) + "."
        log.info(self.job.summary)

    def _partial_track_messages(self):
        if self.partial:
            log.info(f"  {plural(self.partial, 'album')} downloaded only partly "
                     f"(some tracks are missing); see the log.")
            self.job.attention = "partial" if self.retryable_tracks else "lossy"
            messages = []
            if self.retryable_tracks:
                destination = "Library Gap Fill"
                if self.retry_save_failed:
                    messages.append(
                        f"{plural(self.retryable_tracks, 'track')} could not be saved "
                        f"to {destination}."
                    )
                else:
                    messages.append(
                        f"{plural(self.retryable_tracks, 'track')} can be retried from "
                        f"{destination}."
                    )
            if self.lossy_only_tracks or self.unavailable_tracks:
                messages.append(
                    f"{download_outcomes.beyond_retry(self.lossy_only_tracks, self.unavailable_tracks)}."
                )
            self.job.error = " ".join(messages)
        self._apply_non_track_attention()

    def _return_failed_picks(self):
        if self.has_issues:
            _mark_job_failed(self.job, partly=bool(self.ok or self.partial))
        self.failed_cands = _resealed_past_own_imports(self.job, self.failed_cands)
        # A whole-review download (every candidate ticked, nothing re-parked at
        # approval) consumed the entire living review.
        if getattr(self.job, "_consumed_whole_review", False):
            retry_parked = True
            if self.failed_cands:
                retry_parked = _park_library_failures(self.failed_cands) is not False
                self._remember_review_save(retry_parked)
            if retry_parked:
                self._remember_review_save(library_scan_state.mark_review_retired(
                    reason="worked_through",
                    generation=(self.job.execute_args or {}).get(
                        "_library_review_generation"
                    ),
                ), retry=False)
        elif self.failed_cands and self.review_kind:
            # Partial approve: the unticked picks stayed behind as a living split-
            # off review. A new release that failed to download goes back to the
            # New Releases review rather than being consumed.
            self._remember_review_save(
                _return_picks(self.failed_cands, self.review_kind, self.review_args))

    def _failure_message(self):
        if self.failed:
            destination = ("New releases" if self.is_nr_run else
                           "Restore review" if self.is_restore_run else "Library")
            one = self.failed == 1
            if self.retry_save_failed:
                outcome = (f"{'It' if one else 'They'} could not be saved "
                           f"back to {destination}.")
            else:
                pronoun = "It is" if one else "They are"
                outcome = f"{pronoun} selected in {destination} for retry."
            # A fetch failure has no useful detail beyond "Qobuz didn't answer for
            # it"; the raw exception (endpoint name, ids) stays in the activity
            # log for anyone who needs it, not in the sentence the user reads.
            if self.fetch_failed == self.failed:
                reason = (" Qobuz couldn't be reached for it."
                          if self.failed == 1 else
                          " Qobuz couldn't be reached for them.")
            elif self.fetch_failed:
                reason = f" Qobuz couldn't be reached for {self.fetch_failed} of them."
            else:
                reason = ""
            causes = "; ".join(dict.fromkeys(cause for cause in self.failure_causes if cause))
            failure_message = (
                f"{named_albums(self.failed_cands)} didn't finish"
                f"{': ' + causes if causes else ''}.{reason} {outcome}"
            )
            self.job.error = " ".join(part for part in (self.job.error, failure_message) if part)

    def _finish(self):
        if self.review_save_failed:
            warning = (
                "Retry choices could not be saved. Check the data folder, then "
                "refresh before retrying."
                if self.retry_save_failed else
                "The finished Library review could not be saved. Check the data "
                "folder, then refresh before continuing."
            )
            self.job.error = " ".join(part for part in (self.job.error, warning) if part)
            self.job.attention = self.job.attention or "review"
        _close_completed_job(self.job)


def execute_upgrades(job, chosen, token):
    """Re-rip the present tracks of each chosen album at higher quality."""
    _UpgradesRun(job, chosen, token).run()


class _UpgradesRun:
    """One Upgrade run of the chosen albums, each through process_album in turn."""

    def __init__(self, job, chosen, token):
        self.job = job
        self.chosen = chosen
        self.token = token

    def run(self):
        if not self._quality_unchanged():
            return
        self._start()
        for i, cand in enumerate(self.chosen, 1):
            self.i, self.cand = i, cand
            if self.job.cancel_requested:
                break
            self.processed = self.i
            if self._fetch_album() is _NEXT_ALBUM:
                continue
            if self._check_and_upgrade() is _NEXT_ALBUM:
                continue
            self._tally_result()
            if self.current_interrupted:
                break
            time.sleep(cfg.ARTIST_API_DELAY)
        self.job._progress_scope = None
        if self.current_interrupted or (
                self.job.cancel_requested and self.processed < len(self.chosen)):
            self._finish_stopped_early()
            return
        self._write_summary()
        self._finish()

    def _quality_unchanged(self):
        effective = settings_store.current()
        current_quality_signature = upgrade_state.quality_signature(
            effective.get("STREAMRIP_QUALITY"),
            effective.get("PREFER_HIRES"),
            effective.get("DOWNSAMPLE_HIRES_ENABLED"),
        )
        expected_quality_signature = (self.job.execute_args or {}).get(
            "quality_signature"
        )
        if expected_quality_signature != current_quality_signature:
            self.job.status = job_mgr.JobStatus.FAILED
            self.job.summary = "Upgrade stopped before any albums were changed."
            self.job.error = (
                "Download quality changed after this review was built. Run a "
                "Library refresh before trying Upgrade again."
            )
            log.info(self.job.summary)
            log.info(self.job.error)
            return False
        return True

    def _start(self):
        clear_scan_caches()
        _start_album_count(self.job)
        self.args = build_args()
        # Explicit upgrade: enable the replace path for this run only, and turn
        # off per-album consolidation prompts (the CLI upgrade walk does the same).
        self.args.auto_upgrade = True
        self.args.consolidate = False
        # Outcomes that aren't a failure: the album just didn't need (or couldn't
        # safely take) an upgrade.
        self._skip = BENIGN_UPGRADE_RESULTS
        self.ok = 0
        self.upgraded = []
        self.kept = 0
        self.companion_notes = []
        self.cd_quality_tracks = []
        self.put_back_notes = []
        self.catalogue_failed = 0
        self.quality_attention = 0
        self.processing_attention = 0
        self.failed = 0
        self.denied = []
        self.changed = 0
        self.failed_cands = []
        self.refused_cands = []
        self.processed = 0
        self.current_interrupted = False
        self.put_back = None

    def _return_unfinished(self, current_index, *, disk_full=False):
        if not self.job._imported_any:
            return True
        leftovers = self.failed_cands + self.chosen[current_index:]
        saved = _return_upgrade_picks(
            leftovers, self.refused_cands, self.job.execute_args)
        done = f"{plural(self.ok, 'album')} upgraded"
        if disk_full:
            lead = "Stopped: the music disk is full." + (
                f" {done}." if self.ok else "")
        elif self.ok:
            lead = f"Qobuz stopped after {done}."
        else:
            lead = "Qobuz stopped before an album was upgraded."
        self.job.summary = (
            f"{lead} {plural(len(leftovers), 'album')} selected for retry."
        )
        if saved is False:
            self.job.error = (
                "Retry choices could not be saved. Check the data folder, "
                "then refresh Upgrade before retrying."
            )
        return saved

    def _fetch_album(self):
        album_id = self.cand["payload"].get("album_id")
        log.info(f"[{self.i}/{len(self.chosen)}] {self.cand.get('artist','')} - "
                 f"{self.cand.get('title') or '?'}")
        self.job._progress_scope = (self.i - 1, len(self.chosen), "album")
        self.job.push_progress("Upgrading albums", self.i - 1, len(self.chosen),
                               f"{self.cand.get('artist','')} - {self.cand.get('title') or '?'}", unit="album")
        try:
            self.album = get_album(album_id, self.token)
        except (AuthLost, QobuzUnavailable):
            self._return_unfinished(self.i - 1)
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            self.failed += 1
            self.failed_cands.append(self.cand)
            return _NEXT_ALBUM
        if not self.album:
            log.info(f"  album {album_id} is no longer on Qobuz; skipping.")
            self.failed += 1
            self.failed_cands.append(self.cand)
            return _NEXT_ALBUM

    def _check_and_upgrade(self):
        _note_staging_wait(self.job, "Upgrading albums", self.i - 1, len(self.chosen))
        try:
            with job_mgr.staging_lock():
                premise = candidate_premise.validate(self.cand)
                self.result = process_mode.process_album(self.album, self.args, allow_force=False,
                                       already_confirmed=True,
                                       upgrade_only=True, token=self.token,
                                       expected_album_receipt=premise["receipt"])
        except (AuthLost, QobuzUnavailable, beets_mod.MusicDiskFull) as e:
            self._return_unfinished(
                self.i - 1, disk_full=isinstance(e, beets_mod.MusicDiskFull))
            raise
        except CandidateStale as e:
            log.info(f"  skipped: {e}")
            self.changed += 1
            return _NEXT_ALBUM
        except Exception as e:
            log.info(f"  failed: {e}")
            self.failed += 1
            self.failed_cands.append(self.cand)
            return _NEXT_ALBUM

    def _tally_result(self):
        _res = (self.result or {}).get("result")
        self.attention_kind = download_attention_kind(self.result or {})
        if self.result and (
            _res == "cancelled" or self.result.get("cancelled")
        ):
            self.current_interrupted = True
            if _res == "cancelled" and not self.result.get("upgrade_unverified"):
                self.put_back = self.cand
        elif self.result and self.result.get("catalogue_unverified"):
            self.catalogue_failed += 1
            self.job._imported_any = True
            _note_album_done(self.job)
        elif self.result and self.attention_kind == "backup":
            # Imported, but the rebuilt folder couldn't be verified as
            # complete as the original, so the backup was kept.
            self.kept += 1
            self.job._imported_any = True
            _note_album_done(self.job)
        elif self.result and self.result.get("imported") and _res not in (
                self._skip | {"upgrade_aborted_backup_failed"}):
            self._tally_upgraded()
        elif self.result and _res in (self._skip - {"cancelled", "dry_run"}):
            saved_views.forget_upgraded_album(
                None, self.cand["payload"].get("album_dir"))
        elif _res not in self._skip:
            self.failed += 1
            if self.result and self.result.get("folder_denied"):
                self.denied.append(self.result["folder_denied"])
            if self.result and self.result.get("upgrade_refused"):
                self.refused_cands.append(self.cand)
            else:
                self.failed_cands.append(self.cand)
            if self.result and self.result.get("upgrade_put_back"):
                self.put_back_notes.append((
                    f"{self.cand.get('artist', '')} · {self.cand.get('title') or '?'}",
                    self.result.get("upgrade_set_aside")))

    def _tally_upgraded(self):
        self.ok += 1
        self.upgraded.append(self.cand)
        self.job._imported_any = True
        _note_album_done(self.job)
        self.cd_quality_tracks.extend(self.result.get("cd_quality_tracks") or [])
        if self.result.get("companions_kept"):
            self.companion_notes.append((
                f"{self.cand.get('artist', '')} · {self.cand.get('title') or '?'}",
                self.result["companions_kept"]))
        verdict = self.result.get("quality_verdict")
        if self.attention_kind == "quality":
            self.quality_attention += 1
            job_mgr.record_quality_shortfall(self.job, verdict)
            quality_decision.mark_album_capped(self.album.get("id"), self.album, {
                "n_below": verdict["n_below"],
                "n_at": 0,
                "n_above": 0,
            })
            retry = " after retry" if verdict["retried"] else ""
            log.info(
                f"  upgrade incomplete: {plural(verdict['n_below'], 'track')} "
                f"still below target{retry}. Marked capped."
            )
        elif self.attention_kind == "processing":
            self.processing_attention += 1
        _refresh_after_local_album_change(
            self.album,
            self.result,
            fallback_artist=self.cand.get("artist"),
            token=self.token,
            args=self.args,
        )
        # The verified replace refreshed the whole album, so a parked
        # library review's candidates for it (incl. Gap Fill) are stale.
        prune_library_review_candidates(self.album)

    def _finish_stopped_early(self):
        first_unfinished = self.processed - 1 if self.current_interrupted else self.processed
        leftovers = self.failed_cands + self.chosen[first_unfinished:]
        retry_saved = _return_upgrade_picks(
            leftovers, self.refused_cands, self.job.execute_args)
        parts = []
        if self.upgraded:
            parts.append(f"{named_albums(self.upgraded)} upgraded")
        if self.failed:
            parts.append(f"{plural(self.failed, 'album')} failed")
        if self.changed:
            parts.append(f"{plural(self.changed, 'album')} changed since the review")
        self.job.summary = "Stopped early." + (
            " " + ", ".join(parts) + "." if parts else "")
        if self.put_back:
            self.job.summary += f" {named_albums([self.put_back])} was put back as it was"
            self.job.summary += ("; it is ticked in Upgrade to try again."
                                 if retry_saved is not False and leftovers == [self.put_back]
                                 else ".")
        if retry_saved is False:
            unsaved = len(leftovers) + len(self.refused_cands)
            self.job.summary += (
                f" {plural(unsaved, 'album')} could not be saved back to "
                "Upgrade."
            )
        elif leftovers and leftovers != [self.put_back]:
            self.job.summary += (
                f" {named_albums(leftovers)} "
                f"{'is' if len(leftovers) == 1 else 'are'} ticked in Upgrade "
                "to try again."
            )
        if self.cd_quality_tracks:
            self.job.summary += f" {cd_quality_note(self.cd_quality_tracks)}"
        if self.kept:
            self.job.summary += (
                f" {plural(self.kept, 'replacement')} kept a backup."
            )
        self.job.summary += _kept_companions_note(self.companion_notes)
        self.job.summary += _put_back_note(self.put_back_notes)
        if self.catalogue_failed:
            self.job.summary += (
                f" {plural(self.catalogue_failed, 'replacement')} "
                "couldn't be updated in Beets; backup kept."
            )
        if self.quality_attention:
            self.job.summary += (
                f" {plural(self.quality_attention, 'album')} finished below target "
                "quality."
            )
        if self.processing_attention:
            self.job.summary += (
                f" {plural(self.processing_attention, 'album')} "
                f"{'has' if self.processing_attention == 1 else 'have'} "
                "a step after the download that did not finish."
            )
        if self.kept or self.catalogue_failed:
            self.job.attention = "backup"
            self.job.error = (
                "A kept backup needs review under Settings → "
                "Diagnostics before that album is upgraded again."
            )
        elif self.quality_attention:
            self.job.attention = "quality"
            self.job.error = "An album remained below the target quality."
        elif self.processing_attention:
            self.job.attention = "processing"
            self.job.error = "A step after the download did not finish; see the job log."
        elif self.failed:
            self.job.error = (f"{plural(self.failed, 'album')} couldn't be upgraded"
                              f"{_folders_denied_note(self.failed, self.denied)}.")
        elif self.changed:
            self.job.error = (_changed_since_review(self.changed)
                              + "; refresh Upgrade to check again.")
        if retry_saved is False:
            self.job.error = " ".join(part for part in (
                self.job.error,
                "Retry choices could not be saved. Check the data folder, "
                "then refresh Upgrade before retrying.") if part)
        if self.kept or self.catalogue_failed or self.quality_attention or self.processing_attention:
            _mark_job_failed(self.job)
        log.info(self.job.summary)

    def _write_summary(self):
        msg = (f"Upgraded {named_albums(self.upgraded)}." if self.upgraded
               else "No albums were upgraded.")
        if self.kept:
            msg += (f" {plural(self.kept, 'replacement')} kept a backup because "
                    "the replacement or recovery couldn't be verified; review it "
                    "under Settings → Diagnostics.")
        if self.changed:
            msg += (f" {_changed_since_review(self.changed)}; refresh "
                    "Upgrade to check again.")
        msg += _kept_companions_note(self.companion_notes)
        msg += _put_back_note(self.put_back_notes)
        if self.cd_quality_tracks:
            msg += f" {cd_quality_note(self.cd_quality_tracks)}"
        if self.catalogue_failed:
            msg += (f" {plural(self.catalogue_failed, 'replacement')} "
                    "couldn't be updated in Beets; backup kept.")
        if self.quality_attention:
            msg += (
                f" {plural(self.quality_attention, 'album')} finished below target "
                "quality."
            )
        if self.processing_attention:
            msg += (
                f" {plural(self.processing_attention, 'album')} "
                f"{'has' if self.processing_attention == 1 else 'have'} a step after "
                "the download that did not finish."
            )
        self.job.summary = msg
        log.info(msg)

    def _finish(self):
        retry_saved = _return_upgrade_picks(
            self.failed_cands, self.refused_cands, self.job.execute_args)
        if any((self.failed, self.kept, self.catalogue_failed, self.quality_attention,
                self.processing_attention, self.changed)):
            problems = []
            if self.kept:
                problems.append(
                    f"{plural(self.kept, 'replacement')} kept a backup"
                )
            if self.catalogue_failed:
                problems.append(
                    f"{plural(self.catalogue_failed, 'replacement')} couldn't be "
                    "updated in Beets"
                )
            if self.failed:
                problems.append(
                    f"{plural(self.failed, 'album')} couldn't be upgraded"
                    f"{_folders_denied_note(self.failed, self.denied)}"
                )
            if self.changed:
                problems.append(_changed_since_review(self.changed))
            if self.quality_attention:
                problems.append(
                    f"{plural(self.quality_attention, 'album')} remained below target "
                    "quality"
                )
            if self.processing_attention:
                problems.append(
                    f"{plural(self.processing_attention, 'album')} "
                    f"{'has' if self.processing_attention == 1 else 'have'} a step "
                    "after the download that did not finish"
                )
            if self.kept or self.catalogue_failed:
                problems.append("review kept backups under Settings → Diagnostics")
                self.job.attention = "backup"
            elif self.quality_attention:
                self.job.attention = "quality"
            elif self.processing_attention:
                self.job.attention = "processing"
            self.job.error = "; ".join(problems) + "."
            if self.changed:
                self.job.error += " Refresh Upgrade to check again."
            if retry_saved is False:
                unsaved = len(self.failed_cands) + len(self.refused_cands)
                self.job.error += (
                    f" {plural(unsaved, 'album')} could not be saved back to "
                    "Upgrade; refresh Upgrade.")
            elif self.failed_cands:
                self.job.error += (
                    f" {plural(len(self.failed_cands), 'album')} selected for retry.")
            _mark_job_failed(self.job, partly=bool(self.upgraded))
        else:
            _close_completed_job(self.job)


def _changed_since_review(n):
    return (f"{plural(n, 'album')} changed after the review and "
            f"{'was' if n == 1 else 'were'} left unchanged")


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
            if c.album:
                payload["album"] = c.album
            if c.year:
                payload["year"] = c.year
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
    _DownsamplesRun(job, chosen, keep_originals).run()


class _DownsamplesRun:
    """One Downsample run of the chosen albums, each shrunk to CD rate in turn."""

    def __init__(self, job, chosen, keep_originals):
        self.job = job
        self.chosen = chosen
        self.keep_originals = keep_originals

    def run(self):
        if not self._downsampling_available():
            return
        self._start()
        for i, cand in enumerate(self.chosen, 1):
            self.i, self.cand = i, cand
            if self.job.cancel_requested:
                self.stopped_early = True
                break
            self.processed = self.i
            if self._saved_folder() is _NEXT_ALBUM:
                continue
            self._announce_album()
            if self._skip_missing_folder() is _NEXT_ALBUM:
                continue
            if self._downsample_album() is _NEXT_ALBUM:
                continue
            self._tally_result()
        self.job._progress_scope = None
        if self.stopped_early:
            self._finish_stopped_early()
            return
        self._write_summary()
        self._failed_files_message()
        self._flush_warning_message()
        self._finish()

    def _downsampling_available(self):
        if type(self.keep_originals) is not bool:
            self.keep_originals = cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"

        # Read through the module: ffmpeg is a property of the server, not of the
        # moment this file was imported.
        if not downsample_engine.HAVE_DOWNSAMPLE:
            self.job.error = "Downsampling isn't available on this server."
            self.job.summary = self.job.error
            _mark_job_failed(self.job)
            return False
        return True

    def _start(self):
        _start_album_count(self.job)
        self.shrunk = 0
        self.total_saved = 0
        self.interrupted_saved = 0
        self.total_errors = 0
        self.total_flush_warns = 0
        self.state_refresh_warnings = 0
        self.skipped_missing_details = 0
        self.skipped_missing_folders = 0
        self.skipped_unchanged = 0
        self.skipped_changed = 0
        self.failed_albums = 0
        self.interrupted = 0
        self.processed = 0
        self.resampled_files = 0
        self.read_only_tracks = 0
        self.failed_files = []
        self.flushed_files = []
        self.errored_albums = []
        self.stopped_early = False

    def _skip_parts(self):
        parts = []
        if self.skipped_missing_details:
            parts.append(
                f"{plural(self.skipped_missing_details, 'album')} skipped "
                "(saved folder details missing)"
            )
        if self.skipped_missing_folders:
            parts.append(
                f"{plural(self.skipped_missing_folders, 'album')} skipped "
                "(no longer on disk)"
            )
        if self.skipped_unchanged:
            parts.append(
                f"{plural(self.skipped_unchanged, 'album')} skipped "
                "(nothing needed changing)"
            )
        if self.skipped_changed:
            parts.append(
                f"{plural(self.skipped_changed, 'album')} skipped "
                "(the files no longer match the review; refresh it)"
            )
        if self.read_only_tracks:
            parts.append(downsample_engine.read_only_note(self.read_only_tracks))
        return parts

    def _saved_folder(self):
        self.raw_album_dir = (self.cand.get("payload") or {}).get("album_dir")
        if not self.raw_album_dir:
            log.info("  skipped: saved candidate is missing its folder path")
            self.skipped_missing_details += 1
            return _NEXT_ALBUM

    def _announce_album(self):
        self.album_dir = Path(self.raw_album_dir)
        self.title = self.cand.get("title") or self.album_dir.name
        self.label = f"{self.cand.get('artist', '')} · {self.title}".strip(" ·")
        log.info(f"[{self.i}/{len(self.chosen)}] {self.cand.get('artist', '')} - {self.title}")
        self.job._progress_scope = (self.i - 1, len(self.chosen), "album")
        self.job.push_progress("Downsampling albums", self.i - 1, len(self.chosen),
                               f"{self.cand.get('artist', '')} - {self.title}", unit="album")

    def _skip_missing_folder(self):
        if not self.album_dir.is_dir():
            log.info("  skipped: folder no longer exists")
            self.skipped_missing_folders += 1
            if self.album_dir.parent.is_dir():
                saved_views.refresh_downsample_artist(self.album_dir.parent)
            else:
                downsample_state.remove_artist(self.album_dir.parent.name)
                saved_views.sync_badge("downsample")
            return _NEXT_ALBUM

    def _downsample_album(self):
        _note_staging_wait(self.job, "Downsampling albums", self.i - 1, len(self.chosen))
        try:
            with job_mgr.staging_lock():
                candidate_premise.validate(self.cand)
                if quality_decision.mark_local_album_capped(self.album_dir) is not True:
                    raise OSError(
                        "The local downsample decision could not be saved; "
                        "the album was left unchanged."
                    )
                if not upgrade_state.remove_album_dir(self.album_dir):
                    generation_state.mark_output_status(
                        "upgrade",
                        "stale",
                        reason=(
                            "An intentional Downsample could not update the "
                            "saved Upgrade view."
                        ),
                    )
                    self.state_refresh_warnings += 1
                self.res = downsample_engine.downsample_dir(self.album_dir, verbose=True,
                                     base_dir=self.album_dir, log=log.info,
                                     keep_originals=self.keep_originals,
                                     cancel_check=lambda: self.job.cancel_requested)
                try:
                    quality_decision.seal_local_album_cap(self.album_dir)
                except Exception as e:
                    log.info(f"  couldn't record the downsampled audio: {e}")
        except CandidateStale as e:
            log.info(f"  skipped: {e}")
            self.skipped_changed += 1
            return _NEXT_ALBUM
        except Exception as e:
            log.info(f"  failed: {e}")
            self.failed_albums += 1
            self.errored_albums.append(self.label)
            return _NEXT_ALBUM

    def _tally_result(self):
        if self.res.get("cancelled"):
            # A Stop mid-album leaves the rest of its tracks hi-res. The state
            # refresh below re-lists that album so the run can be finished later.
            self.interrupted += 1
            self.interrupted_saved += self.res.get("saved_bytes", 0)
            self.stopped_early = True
        elif self.res.get("resampled"):
            self.shrunk += 1
            self.total_saved += self.res.get("saved_bytes", 0)
            _note_album_done(self.job)
        elif self.res.get("errors"):
            self.failed_albums += 1
        elif not self.res.get("read_only"):
            self.skipped_unchanged += 1
        self.read_only_tracks += self.res.get("read_only", 0)
        saved_views.refresh_downsample_artist(self.album_dir.parent)
        self.total_errors += self.res.get("errors", 0)
        self.total_flush_warns += self.res.get("flush_warnings", 0)
        self.resampled_files += self.res.get("resampled", 0)
        for entry in self.res.get("failed_files") or []:
            self.failed_files.append(dict(entry, album=self.label))
        for entry in self.res.get("flushed_files") or []:
            self.flushed_files.append(dict(entry, album=self.label))

    def _finish_stopped_early(self):
        parts = []
        if self.shrunk:
            parts.append(
                f"Downsampled {plural(self.shrunk, 'album')} "
                f"({format_size(self.total_saved)} smaller)")
        if self.failed_albums:
            parts.append(f"{plural(self.failed_albums, 'album')} failed")
        if self.interrupted:
            progress = "remaining files left unchanged"
            if self.interrupted_saved:
                progress = (
                    f"{format_size(self.interrupted_saved)} smaller so far; "
                    f"{progress}"
                )
            parts.append(
                f"{plural(self.interrupted, 'album')} interrupted "
                f"({progress})"
            )
        parts.extend(self._skip_parts())
        not_started = len(self.chosen) - self.processed
        if not_started:
            parts.append(f"{plural(not_started, 'album')} not started")
        self.job.summary = "Stopped early." + (
            " " + ", ".join(parts) + "." if parts else "")
        self.job.summary += _kept_originals_note(self.resampled_files, self.keep_originals)
        log.info(self.job.summary)
        self._failed_files_message()
        self._flush_warning_message()
        with self.job._lock:
            if self.job.status is job_mgr.JobStatus.RUNNING:
                self.job.status = job_mgr.JobStatus.CANCELED

    def _write_summary(self):
        # "Reclaimed" is a claim about free space. When the originals are kept, a
        # full copy of every one of them is written to the backup folder, so the
        # run costs MORE disk than it saves until that retention expires.
        self.went_wrong = bool(
            self.total_errors or self.total_flush_warns or self.errored_albums)
        if self.shrunk:
            summary = (f"Downsampled {plural(self.shrunk, 'album')}, "
                       f"{format_size(self.total_saved)} smaller."
                       + _kept_originals_note(self.resampled_files, self.keep_originals))
        else:
            summary = "Nothing was downsampled."
        for part in self._skip_parts():
            summary += f" {part}."
        if self.state_refresh_warnings:
            summary += " The saved Upgrade view needs a Library refresh."
        self.job.summary = summary
        log.info(summary)

    def _failed_files_message(self):
        if self.total_errors:
            detail = _named_files(self.failed_files, self.total_errors)
            reason = _lone_reason(self.failed_files, self.total_errors)
            damaged = [e for e in self.failed_files if e.get("damaged")]
            if reason:
                self.job.error = (
                    f"Couldn't downsample {detail}: {reason.rstrip('.')}.")
            elif len(damaged) == self.total_errors:
                self.job.error = (
                    f"Couldn't downsample {detail}. They don't decode cleanly and "
                    "may be damaged, so they were left unchanged; Repair can "
                    "re-download them.")
            else:
                self.job.error = (
                    f"Couldn't downsample {detail}. "
                    f"{'It was' if self.total_errors == 1 else 'They were'} left "
                    "unchanged.")
                if damaged:
                    names = "; ".join(
                        f"{e['album']}, {e['name']}" if e.get("album")
                        else e["name"] for e in damaged[:3])
                    if len(damaged) > 3:
                        names += f" and {len(damaged) - 3} more"
                    self.job.error += (
                        f" {names} {'doesn' if len(damaged) == 1 else 'don'}'t "
                        "decode cleanly and may be damaged; Repair can "
                        f"re-download {'it' if len(damaged) == 1 else 'them'}.")
        if self.errored_albums:
            names = "; ".join(self.errored_albums[:3])
            if len(self.errored_albums) > 3:
                names += f" and {len(self.errored_albums) - 3} more"
            note = f"Couldn't downsample {names}; see the log."
            self.job.error = f"{self.job.error} {note}" if self.job.error else note

    def _flush_warning_message(self):
        if self.total_flush_warns:
            # These files WERE rewritten, only the folder flush failed
            # afterwards, so the swap may not survive a power loss.
            detail = _named_files(self.flushed_files, self.total_flush_warns)
            reason = _lone_reason(self.flushed_files, self.total_flush_warns)
            note = (
                f"Resampled {detail} but couldn't confirm "
                f"{'it was' if self.total_flush_warns == 1 else 'they were'} written "
                "to disk, so the "
                "change may not survive a power cut. Check the drive."
                + (f" ({reason})" if reason else "")
            )
            self.job.error = f"{self.job.error} {note}" if self.job.error else note

    def _finish(self):
        if self.went_wrong:
            _mark_job_failed(self.job, partly=bool(self.shrunk))
        else:
            _close_completed_job(self.job)


def _folders_denied_note(failed, denied):
    """Why the albums whose folders are in ``denied`` couldn't be upgraded,
    as a tail for the count of ``failed``, or ""."""
    if not denied:
        return ""
    named = set(denied)
    if len(named) == 1:
        folders, _is_host = storage._resolve_host_path(denied[0])
    else:
        folders = "their folders"
    if len(denied) == failed:
        return f": the app can't write to {folders}"
    return f", {len(denied)} because the app can't write to {folders}"


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
                f"{'is' if one else 'are'} kept in a backup under Settings → "
                "Diagnostics.")
    return (f" {plural(len(notes), 'album')} had original files that differ "
            "from the new album's; they are kept in backups under Settings → "
            "Diagnostics.")


def _put_back_note(notes):
    """Name the albums whose unverified upgrade was undone.

    ``notes`` pairs each album's label with where its download was set aside.
    """
    if not notes:
        return ""
    aside = any(where for _label, where in notes)
    tail = (" and the new download set aside under Settings → Diagnostics"
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
    return (f" The originals are kept for {plural(days, 'day')} and can be "
            "put back from Settings → Diagnostics.")


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
                     f"{int(round(worst))}s shorter than the Qobuz recording")
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
            # re-download candidate and reported there.
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
                       "matched_title": m_title,
                       "damaged": [
                           {"file": Path(e["path"]).relative_to(album_dir).as_posix(),
                            "title": e.get("local_title") or e.get("title")}
                           for e in (*truncated, *suspicious)]}
            if premise is not None:
                payload["_premise"] = {
                    **premise,
                    "kind": "repair-redownload",
                }
            out["specs"].append({
                "kind": "redownload", "title": album_dir.name, "artist": name,
                "detail": detail,
                "payload": payload})
            # The sweep promises every problem reaches the log, and a
            # re-download candidate is one.
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
                "up; replace it with a copy of its file")
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
    """One consistent live-status line for the whole-library repair sweep.
    The counts are the whole sweep's so far, the artist the one it is on."""
    if not artist and not albums:
        return "Starting…"
    albums_word = "album" if albums == 1 else "albums"
    item = f"{albums:,} {albums_word} checked · {flagged:,} flagged"
    return f"{item} · now {artist}" if artist else item


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
                _add_candidate_spec(job, spec)
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
                "artists_total": n,
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
                else:
                    job_mgr.settle_repair_resume_notes()
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
    # Honest summary: report what was actually checked, and never
    # claim completeness the scan didn't earn.
    caveats = _repair_scan_caveats(
        n_unverified, n_failed_albums,
        n_failed_artists - len(unreadable_artists) - n_linked_artists,
        n_linked_artists)
    unmatched = (
        f" {plural(n_unmatched, 'track')} couldn't be matched to a Qobuz "
        f"recording, so {'its' if n_unmatched == 1 else 'their'} length "
        "wasn't checked."
        if n_unmatched else "")
    if total:
        job.summary = (f"{plural(total, 'album')} flagged with damaged files. "
                       f"{plural(n_verified, 'track')} checked and complete."
                       + unmatched + caveats)
    else:
        job.summary = (f"No damaged files found. "
                       f"{plural(n_verified, 'track')} checked and complete."
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
    else:
        # Kept through approval and discard, for "Last repair scan" and the
        # time the sweep took.
        job.execute_args["_scan_finished_at"] = time.time()
        if n_failed_artists or n_failed_albums or n_unverified:
            job.attention = "unchecked"
    job_mgr.settle_repair_resume_notes()
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
    return _RedownloadRun(payload, token, recovery_checkpoint,
                          expected_album_receipt).run()


class _RedownloadRun:
    """One whole-album Repair re-download, its original held in a backup until checked."""

    def __init__(self, payload, token, recovery_checkpoint, expected_album_receipt):
        self.payload = payload
        self.token = token
        self.recovery_checkpoint = recovery_checkpoint
        self.expected_album_receipt = expected_album_receipt

    def run(self):
        refused = self._set_aside()
        if refused is not None:
            return refused
        refused = self._save_backup_record()
        if refused is not None:
            return refused
        self._download()
        if self.backup:
            try:
                self._settle_backup()
            except BaseException as exc:
                self._stopped_after_landing(exc)
                raise
        return self.result

    def _set_aside(self):
        log.info("  The damaged file can't be verified by its ID, so the whole "
                 "album is being re-downloaded fresh from Qobuz.")
        self.full = get_album(self.payload["album_id"], self.token)
        self.album_dir = Path(self.payload["album_dir"])
        self.catalogue_snapshot = beets_mod.capture_beets_album_entries(self.album_dir) \
            if self.album_dir.exists() else None
        if self.album_dir.exists() and self.catalogue_snapshot is None:
            log.info("  Couldn't identify this album's Beets entries; "
                     "leaving Repair alone.")
            return {"imported": False, "n_ok": 0,
                    "result": "catalogue_snapshot_failed"}
        expected_generation = getattr(self.token, "credential_generation", "")
        if expected_generation:
            self.token = api_client.authorize_qobuz_action(
                QobuzAccess.DOWNLOAD_ACTION,
                expected_generation=expected_generation,
            ).token
        self.backup = None
        if self.album_dir.exists():
            self.backup = (
                backup_mod.backup_album_dir(
                    self.album_dir,
                    expected_receipt=self.expected_album_receipt,
                )
                if self.expected_album_receipt is not None
                else backup_mod.backup_album_dir(self.album_dir)
            )
        if self.backup is not None and not self.backup.complete:
            process_mode._recover_incomplete_upgrade_backup(
                self.backup, self.album_dir, operation="repair backup")
            log.info("  The backup was interrupted, so the repair was stopped. "
                     "See the recovery message above.")
            return {"imported": False, "n_ok": 0, "result": "backup_failed"}
        if (
            self.album_dir.exists()
            and self.backup is None
        ):
            log.info("  Couldn't move the existing folder aside; left this album "
                     "alone. See the log above.")
            return {"imported": False, "n_ok": 0, "result": "backup_failed"}

    def _pin_repair_recovery(self, note):
        if (self.backup is not None and self.backup.exists()
                and not backup_mod.pin_unverified_upgrade_backup(self.backup, note)):
            backup_mod.warn_pin_failed(self.backup)

    def _checkpoint_recovery(self, stage, reason, *, retained=True, required=False):
        recovery = RepairRecovery(
            backup=self.backup,
            album_dir=self.album_dir,
            stage=stage,
            reason=reason,
            retained=retained,
        )
        if self.recovery_checkpoint is None:
            return recovery, True
        try:
            persisted = self.recovery_checkpoint(recovery) is True
        except Exception as exc:
            persisted = False
            log.info(f"  Couldn't save the Repair recovery record: {exc}")
        if required and not persisted:
            log.info("  Repair stopped before downloading the replacement "
                     "because its recovery record could not be saved.")
        return recovery, persisted

    def _stopped_after_landing(self, exc):
        # A stop once the replacement reached the library: the original
        # stays in its backup until the new album is checked, and Beets
        # drops the entries the new files replaced at their paths.
        if self.backup is None or not self.backup.exists() or not self.album_dir.exists():
            return
        self._pin_repair_recovery("repair backup kept, re-download was interrupted")
        with contextlib.suppress(KeyboardInterrupt):
            beets_mod.forget_replaced_beets_entries(folder=self.album_dir)
        recovery, _ = self._checkpoint_recovery(
            "refill",
            "Repair stopped after the replacement reached the library, "
            "before it was checked.",
        )
        raise RepairRecoveryRequired(recovery, exc) from exc

    def _save_backup_record(self):
        if self.backup is not None:
            _recovery, recovery_saved = self._checkpoint_recovery(
                "backup",
                "The original album is held while its replacement is downloaded.",
                required=True,
            )
            if not recovery_saved:
                if backup_mod.restore_upgrade_backup(self.backup, self.album_dir):
                    self._checkpoint_recovery(
                        "resolved",
                        "The original album was restored before Repair stopped.",
                        retained=False,
                    )
                    return {
                        "imported": False,
                        "n_ok": 0,
                        "result": "recovery_record_failed",
                    }
                self._pin_repair_recovery(
                    "repair backup kept, recovery record and automatic restore "
                    "did not complete")
                recovery, _ = self._checkpoint_recovery(
                    "restore",
                    "The recovery record could not be saved and automatic "
                    "restoration did not complete.",
                )
                cause = OSError("Repair recovery record could not be saved")
                raise RepairRecoveryRequired(recovery, cause) from cause

    def _download(self):
        try:
            with job_mgr.staging_lock():
                self.result = process_mode.process_album(
                    self.full, build_args(), allow_force=False, already_confirmed=True,
                    token=self.token, cover_backup=self.backup,
                    rebuild_dir=self.album_dir if self.backup is not None else None) or {}
        except Exception as exc:
            if self.backup:
                if backup_mod.restore_upgrade_backup(self.backup, self.album_dir):
                    self._checkpoint_recovery(
                        "resolved",
                        "The original album was restored after the replacement "
                        "download stopped.",
                        retained=False,
                    )
                else:
                    self._pin_repair_recovery(
                        "repair backup kept, automatic restore after an error did "
                        "not complete")
                    recovery, _ = self._checkpoint_recovery(
                        "restore",
                        "The replacement download stopped and automatic "
                        "restoration did not complete.",
                    )
                    raise RepairRecoveryRequired(recovery, exc) from exc
            raise
        except BaseException as exc:
            self._stopped_after_landing(exc)
            raise
        self.imported_ok = bool(self.result.get("imported")) and self.result.get("n_ok", 0) > 0

    def _settle_backup(self):
        damaged = self.payload.get("damaged") or []
        damaged_files = [d["file"] for d in damaged]
        if self.imported_ok and process_mode._upgrade_replacement_verified(
                self.full, self.album_dir, self.backup, damaged_files):
            # Carry useful companions, then retire the original's backup when
            # every file it holds is verifiably superseded in the new album.
            carried = process_mode._carry_non_audio_from_backup(
                self.full, self.album_dir, self.backup, replacement_dir=self.album_dir,
                damaged=damaged_files)
            if carried is not None:
                self._retire_original(carried, damaged)
            else:
                self.result["repair_unverified"] = True
                self._pin_repair_recovery(
                    "repair backup kept, replacement not confirmed written to disk")
                log.info("  Re-download landed, but the rebuilt album or its "
                         "companions couldn't be confirmed written to disk; keeping your "
                         f"backup at {self.backup}.")
                self._checkpoint_recovery(
                    "verification",
                    "The replacement landed, but its album or companion files "
                    "could not be confirmed written to disk.",
                )
        elif self.imported_ok:
            # Imported, but a decode pass alone doesn't prove the re-rip kept
            # every track, a truncated or short result could be WORSE than
            # the damaged original it replaced.
            self.result["repair_unverified"] = True
            self._pin_repair_recovery(
                "repair backup kept, replacement could not be verified "
                "complete")
            # A track the replacement lacks keeps its entry, and Restore
            # needs it.
            beets_mod.forget_replaced_beets_entries(folder=self.album_dir)
            log.info("  Re-download landed but couldn't be verified as complete "
                     f"as the original; keeping your backup at {self.backup}.")
            self._checkpoint_recovery(
                "verification",
                "The replacement landed but could not be verified as complete "
                "as the original album.",
            )
        else:
            log.info("  Re-download didn't complete. Restoring the original "
                     "album folder.")
            if backup_mod.restore_upgrade_backup(self.backup, self.album_dir):
                self._checkpoint_recovery(
                    "resolved",
                    "The replacement did not complete, so the original album "
                    "was restored.",
                    retained=False,
                )
            else:
                self._pin_repair_recovery(
                    "repair backup kept, automatic restore did not complete")
                self._checkpoint_recovery(
                    "restore",
                    "The replacement did not complete and automatic "
                    "restoration was incomplete.",
                )

    def _retire_original(self, carried, damaged):
        replacement_path, replacement_receipt, left = carried
        if not beets_mod.retire_replaced_beets_entries(
            self.catalogue_snapshot,
            replacement_path,
            process_mode._replacement_audio_paths(
                replacement_path,
                replacement_receipt,
            ),
        ):
            self.result["repair_unverified"] = True
            self._pin_repair_recovery(
                "repair backup kept; the old Beets entries "
                "could not be removed")
            log.info("  Re-download verified, but the old "
                     "Beets entries couldn't be removed; "
                     f"keeping the backup at {self.backup}.")
            self._checkpoint_recovery(
                "verification",
                "The re-download verified, but the old Beets "
                "entries could not be removed.",
            )
        elif left:
            self.result["companions_kept"] = left
            log.info(process_mode._keep_backup_for_companions(
                self.backup, left))
            one = len(left) == 1
            self._checkpoint_recovery(
                "verification",
                "The re-download verified. The original album's "
                f"{', '.join(left)} {'differs' if one else 'differ'} "
                "from the new album's and "
                f"{'is' if one else 'are'} kept here.",
            )
        elif backup_mod.retire_verified_repair_backup(self.backup):
            self._checkpoint_recovery(
                "resolved",
                "The re-download verified, so the original album's "
                "backup was removed.",
                retained=False,
            )
            log.info("  Re-download verified; removed the original "
                     "album's backup.")
        else:
            self._pin_repair_recovery(
                "repair backup kept, could not be proven redundant "
                "after a verified re-download")
            log.info("  Re-download verified, but the original "
                     "album's backup couldn't be proven redundant; "
                     f"keeping it at {self.backup}.")
            self._checkpoint_recovery(
                "verification",
                "The re-download verified, but the original album's "
                "backup could not be proven redundant.",
            )
        if not self.result.get("repair_unverified"):
            repair_log.append_repair_log([
                {"artist": self.payload["artist_name"],
                 "album": self.album_dir.name, "title": d["title"]}
                for d in damaged])


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
    _RepairsRun(job, chosen, token).run()


class _RepairsRun:
    """One Repair run of the chosen albums, each refilled or re-downloaded in turn."""

    def __init__(self, job, chosen, token):
        self.job = job
        self.chosen = chosen
        self.token = token

    def run(self):
        self._start()
        for i, cand in enumerate(self.chosen, 1):
            self.i, self.cand = i, cand
            if self.job.cancel_requested:
                self.stopped_early = True
                break
            self.processed = self.i
            self._announce_album()
            try:
                self._repair_album()
            except RepairRecoveryRequired as exc:
                # The callback normally saved this before the exceptional
                # boundary.
                _checkpoint_repair_recovery(self.job, exc.recovery)
                if self.job.cancel_requested:
                    self.stopped_early = True
                    self.unfinished_start = self.processed
                    break
                if isinstance(exc.cause, (AuthLost, QobuzUnavailable, SystemExit)):
                    self._return_unfinished(self.i - 1, include_current=False)
                    raise exc.cause
                raise
            except (AuthLost, QobuzUnavailable, beets_mod.MusicDiskFull) as e:
                self._return_unfinished(
                    self.i - 1, disk_full=isinstance(e, beets_mod.MusicDiskFull))
                raise
            except CandidateStale as e:
                log.info(f"  skipped: {e}")
                self.changed += 1
                continue
            except Exception as e:
                log.info(f"  failed: {e}")
                self.failed += 1
                self.failed_cands.append(self.cand)
                continue
            self._note_local_change()
            self._reseal_put_back()
            self._count_result()
            if self.local_files_changed:
                _refresh_after_local_album_change(
                    None,
                    {"dir": self.p["album_dir"], **self.result, "in_place": True},
                    fallback_artist=self.p.get("artist_name"),
                    token=self.token,
                    args=self.args,
                )
            if self.stopped_early:
                break
            time.sleep(cfg.ARTIST_API_DELAY)
        self.job._progress_scope = None
        if self.stopped_early:
            self._finish_stopped_early()
            return
        self._write_summary()
        self._finish()

    def _start(self):
        clear_scan_caches()
        _start_album_count(self.job)
        self.args = build_args()
        self.fixed = 0
        self.unverified = 0
        self.failed = 0
        self.failed_stale = 0
        self.changed = 0
        self.partly = []
        self.cd_quality_tracks = []
        self.failed_cands = []
        self.denied = []
        self.processed = 0
        self.stopped_early = False
        self.unfinished_start = None

    def _outcome_counts(self):
        counts = [(n, label) for n, label in (
            (self.fixed, "repaired"),
            (self.unverified, "replaced but unproven"),
            (self.failed, "failed"),
            (len(self.partly), "partly repaired"),
        ) if n]
        if not counts:
            return ""
        (first, label), rest = counts[0], counts[1:]
        return " " + ", ".join(
            [f"{plural(first, 'album')} {label}"]
            + [f"{n} {label}" for n, label in rest]) + "."

    def _rescan_note(self):
        if not self.changed:
            return ""
        return (f" {plural(self.changed, 'album')} "
                f"{'needs' if self.changed == 1 else 'need'} a new Repair scan "
                "before another try.")

    def _kept_note(self):
        count = len(getattr(self.job, "recoveries", []) or [])
        return f" {plural(count, 'backup')} kept for recovery." if count else ""

    def _failed_note(self):
        if not self.failed:
            return ""
        note = (f"{self.failed} of {plural(len(self.chosen), 'album')} couldn't "
                "be repaired"
                f"{_folders_denied_note(self.failed, self.denied)}"
                f"{'.' if len(self.denied) == self.failed else '; see the log.'}")
        if self.failed_stale:
            if self.failed_stale < self.failed:
                which = f"{self.failed_stale} of them"
            else:
                which = "it" if self.failed == 1 else "them"
            note += f" A new Repair scan can try {which} again."
        return note

    def _return_unfinished(self, current_index, *, include_current=True,
                           disk_full=False):
        if (not self.job._imported_any
                and not getattr(self.job, "recoveries", None)):
            return True
        start = current_index if include_current else current_index + 1
        leftovers = self.failed_cands + self.chosen[start:]
        saved = _return_picks(
            leftovers,
            "repair",
            execute_args=getattr(self.job, "execute_args", None),
        )
        self.job.summary = (
            "Stopped: the music disk is full." if disk_full else "Qobuz stopped."
        ) + self._outcome_counts()
        if leftovers and saved is not False:
            self.job.summary += (
                f" {plural(len(leftovers), 'album')} selected for retry.")
        self.job.summary += self._kept_note()
        if saved is False:
            self.job.error = (
                "Retry choices could not be saved. Check the data folder, "
                "then run a fresh Repair scan."
            )
        return saved

    def _announce_album(self):
        self.p = self.cand["payload"]
        # Pin the progress card to album-level scope so the inner per-album
        # phases (download / import / downsample) read "album i / N" instead of
        # resetting it to 1 / 1, the card now reflects the whole batch.
        self.job._progress_scope = (self.i - 1, len(self.chosen), "album")
        self.job.push_progress("Repairing damaged albums", self.i - 1, len(self.chosen),
                               f"{self.p['artist_name']} - {self.cand['title']}", unit="album")
        log.info(f"[{self.i}/{len(self.chosen)}] {self.p['artist_name']} - {self.cand['title']}")
        _note_staging_wait(self.job, "Repairing damaged albums", self.i - 1, len(self.chosen))

    def _repair_album(self):
        if self.cand.get("kind") == "redownload":
            premise = candidate_premise.validate(self.cand)
            # _redownload_damaged_album takes the staging lock itself.
            self.result = _redownload_damaged_album(
                self.p,
                self.token,
                expected_album_receipt=premise["receipt"],
                recovery_checkpoint=lambda recovery: (
                    _checkpoint_repair_recovery(self.job, recovery)
                ),
            )
        else:
            with job_mgr.staging_lock():
                candidate_premise.validate(self.cand)
                self.result = repair.repair_album_dir(Path(self.p["album_dir"]),
                                          self.p["verified_truncated"],
                                          self.p["artist_name"], self.args, self.token,
                                          recovery_checkpoint=lambda recovery: (
                                              _checkpoint_repair_recovery(
                                                  self.job, recovery)
                                          ))

    def _note_local_change(self):
        # Each chosen album was flagged as damaged, so anything that didn't
        # end up downloaded-and-imported is a real failure. A kept backup is
        # not one, a verified repair counts, and the backup is reported in
        # the summary's recovery tail.
        self.local_files_changed = bool(
            self.result
            and self.result.get("imported")
            and self.result.get("n_ok", 0) > 0
        )
        if self.local_files_changed:
            self.job._imported_any = True

    def _reseal_put_back(self):
        if self.result and self.result.get("put_back") and "_premise" in self.p:
            # Every original went back as a copy of itself, which the pick's
            # seals would refuse as changed files.
            resealed = candidate_premise.resealed_after_tracks_put_back(
                self.p["_premise"], self.result["put_back"])
            receipts = [
                repair_log.resealed_source_receipt(
                    self.p["album_dir"], b["source_receipt"])
                for b in self.p["verified_truncated"]
            ] if resealed is not None else []
            if resealed is not None and all(receipts):
                self.p["_premise"] = resealed
                for b, receipt in zip(self.p["verified_truncated"], receipts):
                    b["source_receipt"] = receipt

    def _count_result(self):
        if self.result and (
            self.result.get("result") == "cancelled" or self.result.get("cancelled")
        ):
            self.stopped_early = True
            self.unfinished_start = self.processed - 1
            try:
                candidate_premise.validate(self.cand)
            except CandidateStale:
                self.changed += 1
                self.unfinished_start = self.processed
        elif (self.result and self.result.get("n_ok", 0) > 0 and self.result.get("imported")
                and self.result.get("n_fail", 0) == 0
                and self.result.get("repair_unverified")):
            # Its own outcome, neither repaired nor failed: the replacement
            # landed and the originals are held as a recovery backup, so
            # "repaired" would vouch for files nothing has proven complete,
            # and "couldn't be repaired" would say the album was untouched when
            # it has just been replaced.
            self.unverified += 1
            self.job._imported_any = True
            self.cd_quality_tracks.extend(self.result.get("cd_quality_tracks") or [])
        elif (self.result and self.result.get("n_ok", 0) > 0 and self.result.get("imported")
                and self.result.get("n_fail", 0) == 0):
            self.fixed += 1
            self.job._imported_any = True
            _note_album_done(self.job)
            self.cd_quality_tracks.extend(self.result.get("cd_quality_tracks") or [])
        elif self.result and self.result.get("n_ok", 0) > 0 and self.result.get("imported"):
            # Some tracks verified and stay; the others' originals went
            # back, and only a fresh scan can offer them again.
            self.partly.append((self.result["n_ok"], self.result.get("n_fail", 0)))
            self.job._imported_any = True
            self.cd_quality_tracks.extend(self.result.get("cd_quality_tracks") or [])
        else:
            self.failed += 1
            if self.result and self.result.get("folder_denied"):
                self.denied.append(self.result["folder_denied"])
            # Start refuses a pick whose album this run changed, so unless
            # the put-back resealed it above, only a fresh scan can offer it.
            try:
                candidate_premise.validate(self.cand)
            except CandidateStale:
                self.failed_stale += 1
            else:
                self.failed_cands.append(self.cand)

    def _finish_stopped_early(self):
        if self.unfinished_start is None:
            self.unfinished_start = self.processed
        leftovers = self.failed_cands + self.chosen[self.unfinished_start:]
        retry_saved = _return_picks(
            leftovers,
            "repair",
            execute_args=getattr(self.job, "execute_args", None),
        )
        self.job.summary = ("Stopped early." + self._outcome_counts()
                            + self._rescan_note() + self._kept_note())
        if retry_saved is False:
            self.job.summary += (f" {named_albums(leftovers)} could not be saved "
                                 "back to Repair.")
            self.job.error = (
                "Retry choices could not be saved. Check the data folder, "
                "then run a fresh Repair scan."
            )
        elif leftovers:
            self.job.summary += (
                f" {named_albums(leftovers)} "
                f"{'is' if len(leftovers) == 1 else 'are'} back in Repair, "
                "ticked, to repair again."
            )
        if self.failed and not self.job.error:
            self.job.error = self._failed_note()
        log.info(self.job.summary)
        with self.job._lock:
            if self.job.status is job_mgr.JobStatus.RUNNING:
                self.job.status = job_mgr.JobStatus.CANCELED

    def _write_summary(self):
        unproven = (
            f" {plural(self.unverified, 'album')} "
            f"{'was' if self.unverified == 1 else 'were'} replaced but could not be "
            "proven complete, so your original files were kept."
            if self.unverified else ""
        )
        repaired_tracks = sum(ok for ok, _ in self.partly)
        self.failed_tracks = sum(bad for _, bad in self.partly)
        part = (
            f" {plural(len(self.partly), 'album')} only in part: "
            f"{plural(repaired_tracks, 'track')} repaired, {self.failed_tracks} "
            f"failed and {'its original was' if self.failed_tracks == 1 else 'their originals were'} "
            "put back; a new Repair scan can try "
            f"{'it' if self.failed_tracks == 1 else 'them'} again."
            if self.partly else ""
        )
        repaired = self.fixed + len(self.partly)
        of_chosen = "" if repaired == len(self.chosen) else f"{repaired} of "
        lead = f"Repaired {of_chosen}{plural(len(self.chosen), 'album')}."
        if self.chosen and self.unverified == len(self.chosen):
            lead = ""
        self.job.summary = (
            f"{lead}{part}{unproven}{self._kept_note()}"
            f"{self._rescan_note()}").strip()
        if self.cd_quality_tracks:
            self.job.summary += f" {cd_quality_note(self.cd_quality_tracks)}"
        log.info(self.job.summary)

    def _finish(self):
        retry_saved = _return_picks(
            self.failed_cands,
            "repair",
            execute_args=getattr(self.job, "execute_args", None),
        )
        if self.failed or self.changed or self.partly:
            self.job.error = (
                self._failed_note()
                + (f" {plural(self.failed_tracks, 'track')} couldn't be repaired; "
                   "see the log." if self.partly else "")
                + self._rescan_note()).strip()
            if self.failed_cands:
                self.job.error += (
                    f" {plural(len(self.failed_cands), 'album')} selected for retry."
                    if retry_saved is not False
                    else " Retry choices could not be saved; run a fresh scan."
                )
            _mark_job_failed(self.job, partly=bool(self.fixed or self.partly))
        else:
            _close_completed_job(self.job)


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
    unread = []
    try:
        document = collection_snapshot.build_snapshot(
            previous=previous, source="manual", unread=unread)
        written, refusal = collection_snapshot.write_snapshot(
            document, force=force)
    except OSError as e:
        job.summary = (f"The backup couldn't be written: "
                       f"{collection_snapshot.write_error(e)}. Check the "
                       "Backup folder in Settings.")
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
    unread_note = ""
    if unread:
        names = _first_names(unread)
        unread_note = (
            f" {plural(len(unread), 'artist folder')} couldn't be read in "
            "full, so the backup keeps what the last one recorded there: "
            f"{names}.")
    if (not force and previous is not None
            and collection_snapshot.same_collection(document, previous)):
        age = job_labels._format_age(previous.get("updated_at_epoch"))
        job.summary = (f"Nothing changed since the last backup"
                       f"{f' ({age})' if age else ''}, so it was kept as it "
                       "is." + unread_note)
        log.info(job.summary)
        _close_completed_job(job)
        return
    job.summary = (
        f"Backed up {plural(counts['albums'], 'album')} by "
        f"{plural(counts['artists'], 'artist')} to "
        f"{collection_snapshot.latest_path()}." + unread_note
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
            f"Couldn't read the source library ({exc}). "
            "Fix this and scan again.")
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
        # did not mount, so it is an error, not a finished scan with an
        # empty manifest.
        job.error = (
            "The only music files left in the source folder could not be "
            "read; they are listed in the activity log. Nothing was copied "
            "and no plan was written." if unreadable else
            "No music files were found in the source folder. Check that it is "
            "the folder you meant and that its drive or share is attached, "
            "then scan again.")
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


_MIGRATE_DISK_FULL = ("The destination disk is full. Free space there and run "
                      "Migrate again; it carries on from where it stopped.")


def execute_migration(job, chosen, dest, *, in_place, src=None,
                      allow_low_space=False):
    """Copy (or move) the files behind the approved albums into the layout."""
    _MigrationRun(job, chosen, dest, in_place=in_place,
                  allow_low_space=allow_low_space).run()


class _MigrationRun:
    """One migration of the chosen albums' files into the destination layout."""

    def __init__(self, job, chosen, dest, *, in_place, allow_low_space):
        self.job = job
        self.chosen = chosen
        self.dest = dest
        self.in_place = in_place
        self.allow_low_space = allow_low_space

    def run(self):
        if not self._read_chosen():
            return
        self._build_plan()
        if not self._migrate_under_lock():
            return
        self._tally()
        self._summary_parts()
        self._error_fallbacks()
        self._finish()

    def _read_chosen(self):
        self.dest = Path(self.dest)
        self.dest_display, _ = storage._resolve_host_path(str(self.dest))
        self.entries = []
        self.source_root = None
        self.source_root_receipt = None
        self.dest_root_receipt = None
        self.destination_name_semantics = None
        self.companion_receipts = []
        companion_seen = set()
        for c in self.chosen:
            payload = c.get("payload", {})
            payload_source_root = payload.get("source_root")
            payload_source_receipt = payload.get("source_root_receipt")
            payload_dest_receipt = payload.get("dest_root_receipt")
            payload_name_semantics = payload.get("destination_name_semantics")
            if self.source_root is None:
                self.source_root = payload_source_root
                self.source_root_receipt = payload_source_receipt
                self.dest_root_receipt = payload_dest_receipt
                self.destination_name_semantics = payload_name_semantics
            elif (
                payload_source_root != self.source_root
                or payload_source_receipt != self.source_root_receipt
                or payload_dest_receipt != self.dest_root_receipt
                or payload_name_semantics != self.destination_name_semantics
            ):
                self.job.error = (
                    "The selected albums came from different migration previews. "
                    "Scan again and select albums from one "
                    "preview.")
                self.job.summary = self.job.error
                _mark_job_failed(self.job)
                return False
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
                    self.job.error = (
                        "The saved migration preview is malformed. Scan again.")
                    self.job.summary = self.job.error
                    _mark_job_failed(self.job)
                    return False
                key = tuple(relative)
                if key not in companion_seen:
                    companion_seen.add(key)
                    self.companion_receipts.append(receipt)
            for raw in payload.get("entries", []):
                if not isinstance(raw, (list, tuple)) or len(raw) != 4:
                    self.job.error = (
                        "The saved migration preview is incomplete. Scan again.")
                    self.job.summary = self.job.error
                    _mark_job_failed(self.job)
                    return False
                src_s, dest_s, sealed_source, sealed_destination_path = raw
                self.entries.append(migrate_engine.PlanEntry(
                    source=Path(src_s), status=migrate_engine.PLACE, dest_rel=Path(dest_s),
                    source_receipt=sealed_source,
                    destination_path_receipt=sealed_destination_path))
            for raw in payload.get("resume_entries", []):
                if not isinstance(raw, (list, tuple)) or len(raw) != 4:
                    self.job.error = (
                        "The saved migration preview is incomplete. Scan again.")
                    self.job.summary = self.job.error
                    _mark_job_failed(self.job)
                    return False
                src_s, dest_s, sealed_source, sealed_destination = raw
                self.entries.append(migrate_engine.PlanEntry(
                    source=Path(src_s), status=migrate_engine.COLLISION,
                    dest_rel=Path(dest_s), reason="destination already exists",
                    source_receipt=sealed_source,
                    destination_receipt=sealed_destination))
        self.fingerprinted = set((self.job.execute_args or {}).get("fingerprinted", ()))
        for entry in self.entries:
            entry.source_of_truth = (
                "acoustid" if str(entry.source) in self.fingerprinted else "tags")
        if not self.entries:
            self.job.error = (
                "The selected migration review contains no saved files. Scan again."
            )
            self.job.summary = self.job.error
            _mark_job_failed(self.job)
            return False
        return True

    def _build_plan(self):
        self.plan = migrate_engine.MigrationPlan(
            dest_root=self.dest,
            entries=self.entries,
            source_root=Path(self.source_root) if self.source_root else None,
            source_root_receipt=self.source_root_receipt,
            dest_root_receipt=self.dest_root_receipt,
            companion_receipts=self.companion_receipts,
            destination_name_semantics=self.destination_name_semantics,
        )
        self.resume_entries = self.plan.collisions

    def _migrate_under_lock(self):
        # Serialize the file moves under the staging lock like every other execute
        # flow.
        # A cross-filesystem move can run for hours; name the holder so a download
        # that blocks on the staging mutex shows what it's waiting behind instead of
        # sitting on RUNNING with no reason (the same treatment the lyrics scan gets).
        self.result = None
        self.results_manifest = None
        self.results_error = None
        with job_mgr.staging_lock():
            job_mgr.set_staging_holder("Library migration")
            try:
                if not self._space_allowed():
                    return False
                self._gather_left_behind()
                if not self._write_plan():
                    return False
                self._run_plan()
            finally:
                job_mgr.set_staging_holder(None)
        return True

    def _space_allowed(self):
        # This decisive estimate belongs inside the same mutation interval
        # as execution.
        need, free = migrate_engine.space_estimate(
            self.plan, in_place=self.in_place, resume_entries=self.resume_entries)
        if free is not None and need > free and not self.allow_low_space:
            risk = ("An in-place move that runs out mid-run would leave "
                    "your library half-relocated." if self.in_place else
                    "A copy that runs out stops partway, with the new "
                    "library incomplete.")
            self.job.error = (
                f"Not enough free space at {self.dest_display}: the "
                f"{'move' if self.in_place else 'copy'} needs about "
                f"{format_size(need)} but only {format_size(free)} is free. "
                f"{risk} Free up space, choose another destination, or "
                "scan again and confirm the low-space risk on the new "
                "review.")
            self.job.summary = self.job.error
            _mark_job_failed(self.job)
            log.info(self.job.summary)
            return False
        return True

    def _gather_left_behind(self):
        self.left_behind = [
            migrate_engine.PlanEntry(
                source=Path(source), status=status, source_of_truth=sot,
                dest_rel=Path(destination) if destination else None,
                reason=reason)
            for status, sot, source, destination, reason
            in (self.job.execute_args or {}).get("left_behind", [])]
        chosen_ids = {c.get("cid") for c in self.chosen}
        for candidate in self.job.candidates:
            if candidate.get("cid") in chosen_ids:
                continue
            payload = candidate.get("payload") or {}
            for raw in [*payload.get("entries", []),
                        *payload.get("resume_entries", [])]:
                if isinstance(raw, (list, tuple)) and len(raw) == 4:
                    self.left_behind.append(migrate_engine.PlanEntry(
                        source=Path(raw[0]), status=migrate_engine.SKIPPED,
                        source_of_truth=("acoustid" if raw[0] in self.fingerprinted
                                         else "tags"),
                        dest_rel=Path(raw[1]),
                        reason="not selected in the review"))

    def _write_plan(self):
        try:
            manifest = migrate_engine.write_manifest(
                self.plan, left_behind=self.left_behind)
        except (KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
            self.job.error = (
                f"Nothing was {'moved' if self.in_place else 'copied'}: "
                f"the plan could not be saved at "
                f"{self.dest_display} ({exc}). If that folder was moved, "
                "removed or remounted since the preview, scan again.")
            self.job.summary = self.job.error
            _mark_job_failed(self.job)
            log.info(self.job.summary)
            return False
        manifest_display, _ = storage._resolve_host_path(str(manifest))
        self.job.push_line(f"Full plan written to {manifest_display}")
        if isinstance(self.job.execute_args, dict):
            # Saved now so a restart part way through can still name
            # the plan.
            self.job.execute_args["manifest"] = manifest_display
            job_persistence.persist(self.job)
        return True

    def _run_plan(self):
        self.result = migrate_engine.execute_plan(
            self.plan, in_place=self.in_place,
            cancel_check=lambda: self.job.cancel_requested,
            progress=self.job.push_progress,
            resume_entries=self.resume_entries)
        self.pruned = getattr(self.result, "pruned", 0)
        try:
            self.results_manifest = migrate_engine.write_results_manifest(
                self.result, plan=self.plan, left_behind=self.left_behind)
        except BaseException as exc:
            self.results_error = exc
            if not isinstance(exc, Exception):
                self.job.push_line(f"migration report could not be saved: {exc}")
                raise

    def _tally(self):
        interrupted = [
            (source, reason)
            for source, _destination, status, reason in self.result.outcomes
            if status == migrate_engine.INTERRUPTED
        ]
        self.failed = self.result.failed + len(interrupted)
        for failed_src, reason in (self.result.failures + interrupted)[:50]:
            self.job.push_line(f"failed: {failed_src} - {reason}")
        companion_outcomes = getattr(self.result, "companion_outcomes", ())
        self.companion_skipped = sum(
            status == migrate_engine.SKIPPED
            and reason != migrate_engine.SAME_COMPANION
            for _source, _destination, status, reason in companion_outcomes
        )
        self.skipped = sum(
            status == migrate_engine.SKIPPED
            and not reason.startswith(migrate_engine.VERIFIED_COPY)
            for _source, _destination, status, reason in self.result.outcomes
        )
        self.companion_failed = sum(
            status in {migrate_engine.FAILED, migrate_engine.INTERRUPTED}
            for _source, _destination, status, _reason in companion_outcomes
        )
        for source, _destination, status, reason in companion_outcomes:
            if status in {migrate_engine.FAILED, migrate_engine.INTERRUPTED}:
                self.job.push_line(f"sidecar failed: {source} - {reason}")
        left_behind_rows = (self.job.execute_args or {}).get("left_behind", [])
        self.unreadable = sum(
            status == migrate_engine.UNPLACEABLE
            and reason.startswith(migrate_engine.UNREADABLE)
            for status, _sot, _source, _destination, reason in left_behind_rows)
        self.not_carried = migrate_engine.not_carried_line(
            sum(status == migrate_engine.COLLISION
                for status, _sot, _source, _destination, _reason
                in left_behind_rows),
            sum(status == migrate_engine.UNPLACEABLE
                and not reason.startswith(migrate_engine.UNREADABLE)
                for status, _sot, _source, _destination, reason
                in left_behind_rows))

    def _summary_parts(self):
        verb = "moved" if self.in_place else "copied"
        self.has_problem = bool(
            self.failed or self.companion_failed or self.results_error or self.result.lingered
            or self.unreadable)
        self.unreadable_only = bool(self.unreadable) and not (
            self.failed or self.companion_failed or self.results_error or self.result.lingered
            or self.result.cancelled)
        if self.unreadable_only:
            self.lead = ("Finished, but "
                         f"{plural(self.unreadable, 'file or folder', 'files or folders')} "
                         "could not be read")
        elif self.result.disk_full:
            self.lead = "Stopped: the destination disk is full"
        elif self.has_problem:
            self.lead = (
                "Migration stopped with problems"
                if self.result.cancelled else "Migration needs attention"
            )
        elif self.result.cancelled:
            self.lead = "Migration stopped early"
        else:
            self.lead = f"{plural(self.result.copied, 'file')} {verb} into {self.dest_display}"
        self.parts = [self.lead]
        if self.has_problem or self.result.cancelled:
            self.parts.append(f"{plural(self.result.copied, 'file')} {verb} into {self.dest_display}")
        verified = sum(
            status == migrate_engine.SKIPPED
            and reason.startswith(migrate_engine.VERIFIED_COPY)
            for _source, _destination, status, reason in self.result.outcomes
        )
        if verified:
            self.parts.append(f"{plural(verified, 'existing file')} verified")
        if self.skipped:
            self.parts.append(f"{self.skipped} skipped (already present)")
        if self.result.companions:
            self.parts.append(f"carried {plural(self.result.companions, 'cover/sidecar file')}")
        if self.companion_skipped:
            self.parts.append(
                f"{plural(self.companion_skipped, 'cover/sidecar file')} already existed"
            )
        if self.companion_failed:
            self.parts.append(f"{plural(self.companion_failed, 'cover/sidecar file')} failed")
        if self.result.lingered:
            self.parts.append(
                f"{plural(self.result.lingered, 'original')} still at the source; "
                "the report says why")
        if self.unreadable_only:
            self.parts.append("what could not be read stayed at the source")
        elif self.unreadable:
            self.parts.append(_unreadable_left(self.unreadable))
        if self.failed:
            self.parts.append(f"{self.failed} failed; see the log")
            # Set job.error too (not just the prose summary) so a migration with
            # failed copies ends red, like every other execute path, instead of a
            # green DONE that buries "N failed" mid-sentence.
            self.job.error = f"{plural(self.failed, 'file')} couldn't be migrated; see the log."
        if self.result.disk_full:
            self.job.error = _MIGRATE_DISK_FULL
        if self.pruned:
            self.parts.append(f"cleared {plural(self.pruned, 'empty source folder')}")
        left_lines = migrate_engine.left_in_source_lines(
            getattr(self.result, "left_in_source", ()))
        if left_lines:
            self.parts.append(left_lines[0].rstrip(":") + ", listed in the activity log")
            for line in left_lines:
                self.job.push_line(line)
        if self.not_carried:
            self.parts.append(self.not_carried)
            self.job.push_line(self.not_carried)
        if self.result.cancelled:
            self.parts.append("the destination may contain a partial migration")
        if self.results_error is not None:
            self.parts.append("the migration report could not be saved")
            self.job.error = " ".join(filter(None, (
                self.job.error,
                "Migration finished, but its results report could not be saved: "
                f"{self.results_error}. Check the migration details before running it "
                "again.")))
        else:
            report_display, _ = storage._resolve_host_path(str(self.results_manifest))
            self.parts.append(f"report saved at {report_display}")

    def _error_fallbacks(self):
        if self.result.lingered and not self.job.error:
            self.job.error = (
                "Migration needs attention: "
                f"{plural(self.result.lingered, 'original')} still at the source. "
                "See the saved report.")
        if self.unreadable and not self.job.error:
            self.job.error = (
                f"{self.lead}; what could not be read stayed at the source. "
                if self.unreadable_only else
                f"Migration needs attention: {_unreadable_left(self.unreadable)}. "
            ) + "See the activity log."
        if self.companion_failed:
            attention = plural(self.companion_failed, "sidecar failure")
            if self.job.error:
                self.job.error = f"{self.job.error} Also: {attention}; see the migration details."
            else:
                self.job.error = (
                    f"Migration needs attention: {attention}. See the migration "
                    "details and saved report before running it again."
                )

    def _finish(self):
        if self.has_problem:
            _mark_job_failed(self.job)
        elif self.result.cancelled:
            self.job.status = job_mgr.JobStatus.CANCELED
        if self.has_problem or self.result.cancelled:
            # Finished with a problem or part way, this run belongs in the
            # unresolved History until someone has looked.
            self.job.attention = self.job.attention or "failed"
        self.job.summary = "; ".join(self.parts) + "."
        log.info(self.job.summary)
        if not self.has_problem and not self.result.cancelled:
            # The exact copies, optional source retirement, and results report are
            # all durable. Close under the cancellation lock so a Stop arriving at
            # this final boundary cannot relabel the completed migration.
            _close_completed_job(self.job)


backup_mod.on_restored = reseal_put_back_album
