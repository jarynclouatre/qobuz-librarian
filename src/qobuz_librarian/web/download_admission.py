"""Whether a download is already queued or complete on disk."""
import logging
import math
import threading

from qobuz_librarian.library import catalog
from qobuz_librarian.web import jobs as job_mgr

_log = logging.getLogger("qobuz_librarian")


# Serialises the dedupe-check-then-submit in queue_download: the network
# get_album() await between the early check and the submit leaves a window where
# two requests for one album both pass the check and queue it twice.
_DOWNLOAD_SUBMIT_LOCK = threading.Lock()


def _find_job_touching_album(album_id: str, skip_single_track: bool = False):
    """Return a pending/running job that already covers album_id, either as
    its direct subject or as one of its candidates.

    Reviews don't count, parked or still being built: an album merely
    listed among a review's candidates isn't queued for anything, so
    refusing an explicit download with "already queued" over it would be
    false, and with a whole-library review its candidates are exactly the
    albums the user is most likely to search for. Approve re-checks the
    disk and drops candidates that landed in the meantime, so downloading
    now can't double up later.

    ``skip_single_track`` ignores one-track downloads, so a full-album
    download doesn't fold onto a job that only downloaded one track."""
    for j in job_mgr.registry.pending_and_running():
        if j.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        if skip_single_track and (j.single or {}).get("track_id"):
            continue
        if j.album_id == album_id:
            return j
        if j.status == job_mgr.JobStatus.SCANNING:
            # Still collecting proposals. They are no more queued than a
            # parked review's, and saying "already queued" over one told the
            # user their download had happened when nothing was queued.
            continue
        # Snapshot: an approved job appends to candidates from the worker
        # thread, and iterating it live can raise "list changed size".
        for cand in list(j.candidates or []):
            payload = cand.get("payload") or {}
            if payload.get("album_id") == album_id:
                return j
            qa = (payload.get("candidate") or {}).get("qobuz_album") or {}
            if qa.get("id") == album_id:
                return j
    return None


def _duplicate_download_job(album_id: str, track_id: str = "",
                            as_new_edition: bool = False):
    """The already-active job a new /download should fold onto, or None to let it
    queue. Matched by intent, not album id alone: "get this edition too" is a
    deliberate extra copy and never folds; a single-track download folds only onto an
    identical one; a normal full-album download folds onto another full-album job,
    but not onto a one-track download from the same album, and not onto a
    review's candidate, which is a proposal rather than queued work."""
    if as_new_edition:
        # "Get this edition too" is a deliberate extra copy of an owned album,
        # so it skips folding onto scans and normal downloads, but two
        # identical new-edition submits are the same tap twice, not two
        # deliberate editions. Fold onto an in-flight one.
        for j in job_mgr.registry.pending_and_running():
            if (j.album_id == album_id
                    and (getattr(j, "execute_args", None) or {}).get("new_edition")):
                return j
        return None
    if track_id:
        for j in job_mgr.registry.pending_and_running():
            s = j.single or {}
            if s.get("album_id") == album_id and s.get("track_id") == str(track_id):
                return j
        return None
    return _find_job_touching_album(album_id, skip_single_track=True)


def _active_search_downloads() -> tuple[
        set[str], set[tuple[str, str]], set[str]]:
    albums = set()
    tracks = set()
    scanning_albums = set()
    for job in job_mgr.registry.pending_and_running():
        if job.status == job_mgr.JobStatus.AWAITING_REVIEW:
            continue
        single = job.single or {}
        track_id = str(single.get("track_id") or "")
        album_id = str(single.get("album_id") or job.album_id or "")
        if album_id and track_id:
            tracks.add((album_id, track_id))
        elif album_id:
            albums.add(album_id)
        for candidate in list(job.candidates or []):
            payload = candidate.get("payload") or {}
            candidate_album = payload.get("album_id")
            if not candidate_album:
                candidate_album = (
                    (payload.get("candidate") or {}).get("qobuz_album") or {}
                ).get("id")
            if candidate_album:
                candidate_album = str(candidate_album)
                if job.status == job_mgr.JobStatus.SCANNING:
                    scanning_albums.add(candidate_album)
                elif candidate.get("selected"):
                    albums.add(candidate_album)
    scanning_albums.difference_update(albums)
    return albums, tracks, scanning_albums


def _album_tracks_complete(album: dict) -> bool:
    """Whether this payload carries the album's whole track list.

    A truncated list makes everything on disk look present. tracks.total
    counts the list itself; tracks_count is album metadata, used only when
    the payload carries no count of its own.
    """
    tracks = album.get("tracks") or {}
    items = tracks.get("items") or []
    total = tracks.get("total")
    if total is None:
        total = album.get("tracks_count")
    if not items or total is None:
        return False
    try:
        return int(total) == len(items) and int(tracks.get("offset") or 0) == 0
    except (TypeError, ValueError):
        return False


def _same_edition_is_complete(album: dict) -> bool:
    """Prove that this exact release year is already complete on disk.

    The ordinary album resolver may fall back to a similarly named folder.
    That is useful for gap detection, but it is not enough to refuse a
    deliberate second edition. Require the submitted release year to match
    the resolved folder before comparing its complete track list.
    """

    try:
        folder = catalog.find_album_dir_filesystem(album)
        release_year = catalog.album_year(album)
        if (
            folder is None
            or not release_year
            or str(catalog._dir_year(folder.name) or "") != str(release_year)
        ):
            return False
        existing, _ = catalog.find_existing_tracks(album, album_dir=folder)
        wanted = (album.get("tracks") or {}).get("items") or []
        return bool(existing and _album_tracks_complete(album)) and not catalog.compute_missing(
            wanted, existing)[0]
    except Exception:
        _log.exception(
            "edition ownership check failed for album %s", album.get("id"))
        return False


def _qobuz_quality_bits_rate(primary: dict | None,
                             fallback: dict | None = None) -> tuple[int, int]:
    """Return Qobuz source quality as (bits, sample_rate_hz)."""
    primary = primary if isinstance(primary, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    bits = primary.get("maximum_bit_depth") or fallback.get("maximum_bit_depth") or 0
    rate = (primary.get("maximum_sampling_rate")
            or fallback.get("maximum_sampling_rate") or 0)
    try:
        bits_i = int(bits)
    except (TypeError, ValueError, OverflowError):
        bits_i = 0
    try:
        rate_f = float(rate)
    except (TypeError, ValueError, OverflowError):
        rate_f = 0.0
    if not math.isfinite(rate_f) or rate_f <= 0:
        rate_f = 0.0
    if bits_i <= 0:
        bits_i = 0
    if 0 < rate_f < 1000:
        rate_f *= 1000
    return bits_i, int(round(rate_f))
