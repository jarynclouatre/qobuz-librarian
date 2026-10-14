"""Routes for the Search page and downloads."""
import asyncio
import base64
import hashlib
import html
import logging
import re
import threading
import time
import urllib.parse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from qobuz_librarian import cli, state_file
from qobuz_librarian import config as cfg
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api import search as qobuz_search
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    DownloaderNotReady,
    NoCredsError,
    QobuzAccess,
    QobuzError,
    QobuzUnavailable,
)
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.integrations import lyrics as lyrics_mode
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import (
    catalog,
    collection_snapshot,
    generation_state,
    new_releases,
    scan_checkpoint,
    tags,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import unreadable_artists as unreadable_artists_mod
from qobuz_librarian.quality.tiers import format_quality
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import (
    diagnostics,
    download_admission,
    download_outcomes,
    flows,
    job_labels,
    job_runs,
    new_release_checks,
    qobuz_access,
    refusals,
    rendering,
    runtime,
    scans,
    storage,
    track_downloads,
    write_gate,
)
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


def _download_fragment(kind: str, body: str, outcome: str) -> HTMLResponse:
    return HTMLResponse(
        rendering._ql_notice_html(kind, body),
        headers={"X-QL-Download-Outcome": outcome},
    )


def _staging_album_count() -> int:
    """Album folders left in staging by an interrupted import. The CLI warns
    about these at startup (`_check_staging_occupied`); the web has no such
    signal, so a crash mid-import leaves web-only users with no idea files are
    stranded. Only meaningful when nothing is actively writing; the caller
    suppresses the banner while a job is running."""
    try:
        return sum(
            1 for d in cfg.STAGING_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError:
        return 0


def _albums_in_backups() -> list:
    """Albums missing from the library whose files are in the backup folder,
    by name. Only meaningful while nothing is working in the library."""
    try:
        return [
            diagnostics._album_name_from_path(origin)
            for _path, origin in backup_mod.albums_left_in_backups()
        ]
    except OSError:
        return []


@router.head("/")
async def dashboard_head():
    """A body-less 200 for uptime monitors and curl -I."""
    return Response(status_code=200)


def _new_release_review():
    """The awaiting-review new-release check for the dashboard badge, if any."""
    for j in job_mgr.registry.awaiting_review():
        if j.execute_kind == "new_releases":
            return {"id": j.id, "count": len(j.candidates)}
    return None


_auto_start_busy = threading.Lock()


_UNREADABLE_RECHECK_SECONDS = 300
_unreadable_checked_at = float("-inf")


def _unreadable_recheck_pending():
    """Whether folders the last scan left out as unreadable are due another
    look, from local state alone."""
    return bool(
        cfg.AUTO_LIBRARY_SCAN
        and not write_gate._web_writes_paused()
        and qobuz_access._qobuz_ready()
        and generation_state.baseline_complete()
        and time.monotonic() - _unreadable_checked_at
            >= _UNREADABLE_RECHECK_SECONDS
        and unreadable_artists_mod.load()
        and not _auto_scan_backing_off()
    )


# A scan started automatically that then fails holds off the next automatic
# start on the same schedule as a failed live check, instead of starting
# again on every Search visit.
_auto_scan = {"job": None, "until": 0.0, "delay": 0.0}


def _auto_scan_backing_off():
    """Whether the last automatic scan failed recently enough to wait."""
    job = _auto_scan["job"]
    if job is not None and job.status in job_mgr.TERMINAL:
        _auto_scan["job"] = None
        if job.status is job_mgr.JobStatus.FAILED:
            delay = min(max(_auto_scan["delay"] * 2,
                            new_release_checks._AUTO_START_RETRY_FIRST),
                        new_release_checks._AUTO_START_RETRY_MAX)
            _auto_scan.update(
                until=(job.finished_at or time.time()) + delay, delay=delay)
        else:
            _auto_scan.update(until=0.0, delay=0.0)
    return time.time() < _auto_scan["until"]


def _library_scan_resume_due():
    """Whether an interrupted library scan is waiting to resume, from local
    state alone."""
    return bool(
        cfg.AUTO_LIBRARY_SCAN
        and not write_gate._web_writes_paused()
        and qobuz_access._qobuz_ready()
        and not generation_state.baseline_complete()
        and scan_checkpoint.pending() is not None
        and not _auto_scan_backing_off()
    )


def _start_due_jobs_in_background():
    """Run the dashboard's automatic starts off the request. Each begins with a
    live Qobuz check, which must never hold up the page. A token Qobuz
    rejected is asked about again every few minutes, so Search comes back by
    itself once it is accepted.

    Returns whether an interrupted library scan is resuming, so the page that
    started it does not also offer to resume it.
    """
    if (qobuz_access._token_valid_for() is False
            and qobuz_access._token_recheck_due()):
        try:
            threading.Thread(target=qobuz_access._recheck_token_if_due,
                             name="token-recheck", daemon=True).start()
        except RuntimeError:
            pass
    try:
        resuming = _library_scan_resume_due()
        due = (resuming or _unreadable_recheck_pending()
               or new_release_checks._new_release_check_due())
    except OSError as e:
        _log.warning(
            "automatic start from the dashboard skipped: %s", e)
        return False
    if not due:
        return False
    resuming = resuming and not any(
        j.status != job_mgr.JobStatus.AWAITING_REVIEW
        for j in job_mgr.registry.pending_and_running())
    if not _auto_start_busy.acquire(blocking=False):
        return resuming

    def run():
        try:
            _maybe_resume_library_scan()
            new_release_checks._maybe_auto_check_new_releases()
        except Exception as e:
            _log.warning(
                "automatic start from the dashboard failed: %s", e)
        finally:
            _auto_start_busy.release()

    try:
        threading.Thread(target=run, name="dashboard-auto-start",
                         daemon=True).start()
    except RuntimeError:
        _auto_start_busy.release()
        return False
    return resuming


def _maybe_resume_library_scan():
    """Resume an interrupted library scan when the app is idle, driving it to
    completion across restarts.

    A FRESH first scan is NOT auto-started; the dashboard offers it as a choice
    (see ``offer_baseline``) so a brand-new user isn't hit with a long,
    network-heavy job unprompted. Once they start one and it gets interrupted, it
    leaves a checkpoint and resumes from here. Off entirely via AUTO_LIBRARY_SCAN.
    """
    resume_due = _library_scan_resume_due()
    if not resume_due and not _unreadable_recheck_pending():
        return
    readable_again = False
    if not resume_due:
        # A folder the last scan left out as unreadable is looked at again
        # every few minutes, and only a refresh of what changed follows.
        global _unreadable_checked_at
        _unreadable_checked_at = time.monotonic()
        listed = unreadable_artists_mod.load()
        readable_again = bool(
            listed and unreadable_artists_mod.readable_again(listed))
        if not readable_again:
            return
    credentials = new_release_checks._auto_start_credentials()
    if credentials is None:
        return
    with runtime._auto_check_lock:
        if any(j.status != job_mgr.JobStatus.AWAITING_REVIEW
               for j in job_mgr.registry.pending_and_running()):
            return
        if readable_again:
            _auto_scan["job"] = scans._start_library_scan(credentials)
            return
        cp = scan_checkpoint.pending()
        if cp is not None:
            _auto_scan["job"] = scans._start_library_scan(
                credentials,
                partial_only=(cp["kind"] == "partial"),
            )


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = "", kind: str = "artist",
                    artist_id: str = "", artist_name: str = "", album_id: str = ""):
    active_jobs = [j for j in job_mgr.registry.pending_and_running()
                   if j.status in (job_mgr.JobStatus.RUNNING, job_mgr.JobStatus.SCANNING)]

    # These all read the (often NAS / network-mounted) data + music volumes,
    # the fetch log, the creds file, the lyric-retry file, and a staging
    # iterdir().
    def _gather_disk_state():
        # These read state files, so they run here (off the event loop)
        # alongside the other disk work; any job they start is submitted from
        # a background thread.
        resuming = _start_due_jobs_in_background()
        scan_state = scans._library_scan_state()
        library_generation = scans._truthful_library_generation()
        return {
            "new_release_review": _new_release_review(),
            # First run offers the baseline scan as a Run/Skip choice rather than
            # auto-starting it; suppress the offer once the user skips it (the
            # dismiss marker) or turns it off via AUTO_LIBRARY_SCAN.
            "offer_baseline": (cfg.AUTO_LIBRARY_SCAN
                               and not new_releases.auto_scan_attempted()),
            # First-run setup banner: shown until a full library scan has
            # seeded the new-release baseline.
            "baseline_complete": generation_state.baseline_complete(
                library_generation),
            "setup_scanning": (resuming
                               or scans._active_library_scan() is not None),
            "library_scan_state": scan_state,
            # An interrupted gap-scan, surfaced on the dashboard the way
            # /library already does, gated on no scan running.
            "library_resume": (None if resuming else
                               scans._library_resume_offer(library_generation)),
            # A fresh install has no credentials; the page says so up front.
            # Filesystem-only.
            "creds_ok": qobuz_access._creds_ok(),
            "qobuz_ready": qobuz_access._qobuz_ready(),
            "lyric_retry_count":
                len(lyrics_mode.load_lyric_retry()) if cfg.LYRIC_RETRY_FILE.exists() else 0,
            "staging_album_count": 0 if active_jobs else _staging_album_count(),
            "albums_in_backups": (
                [] if active_jobs or not runtime._run_lock_intact()
                else _albums_in_backups()),
            # Stores that couldn't be read, kept aside while the run fell back
            # to defaults.
            "corrupt_stores": state_file.corrupt_store_details(),
            "restored_albums": [
                diagnostics._album_name_from_path(path)
                for path in runtime.take_restored_albums()
            ],
        }

    loop = asyncio.get_running_loop()
    disk = await loop.run_in_executor(None, _gather_disk_state)
    search_kind = str(kind or "").strip().lower()
    if search_kind not in ("artist", "album", "track"):
        search_kind = "artist"
    search_q = str(q or "").strip()[:200]
    search_artist_id = str(artist_id or "").strip()[:64]
    search_artist_name = str(artist_name or "").strip()[:200]
    search_album_id = str(album_id or "").strip()[:64] if search_kind == "album" else ""
    pending = job_mgr.registry.pending_and_running()
    return rendering._tr(request, "index.html", {
        "active_jobs": active_jobs,
        "pending": pending,
        "queue_waits": {j.id: job_labels._queue_wait(j) for j in pending},
        "review": job_mgr.registry.awaiting_review(),
        "creds_token_valid": qobuz_access._token_valid_for(),
        "search_q": search_q,
        "search_kind": search_kind,
        "search_artist_id": search_artist_id,
        "search_artist_name": search_artist_name,
        "search_album_id": search_album_id,
        "auto_search": bool(search_q or search_album_id),
        "have_lyrics": lyric_fetch.AVAILABLE,
        "page": "dashboard",
        **disk,
    })


@router.post("/lyric-retry")
async def lyric_retry(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = refusals._lock_busy_response(request)
    if busy is not None:
        return busy
    # A retry and a full backfill share the one lyric-state file and must
    # never run at once, so fold onto whichever lyrics pass is already in flight.
    existing = scans._active_scan(
        "lyrics", statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    job = job_mgr.Job(title="Lyric retry")
    job.execute_kind = "lyrics"
    if job_mgr.submit(job, lambda j: flows.run_lyric_retry(j)) is None:
        return refusals._job_admission_response(request)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


_SEARCH_SNAPSHOT_RESULT_LIMIT = 150

# An artist with more albums than this lists them a batch at a time.
_ARTIST_ALBUMS_AT_ONCE = 100
_ARTIST_ALBUM_BATCH = 50

# An artist's albums are listed in this order, by the Qobuz list each is
# filed in. A release in none of them is one of the artist's albums when the
# artist is its main artist, such as a label reissue, and Appears on holds
# the rest, such as another artist's cover.
_ARTIST_SECTIONS = {
    "album": "Albums",
    "live": "Live",
    "compilation": "Compilations",
    "epSingle": "EPs & singles",
    "appears": "Appears on",
}

# Seconds Search waits for its Owned marks, which can need an album's exact
# track list after the folder check, before it renders the results with the
# marks it has.
_OWNERSHIP_TIMEOUT = 20


def _qobuz_quality_short_label(primary: dict | None,
                               fallback: dict | None = None) -> str:
    bits, rate = download_admission._qobuz_quality_bits_rate(primary, fallback)
    if not bits or not rate:
        return ""
    return format_quality(bits, rate)


def _filter_artist_only_albums(albums, query):
    def words(text):
        # normalize() drops what has no ASCII form, such as a CJK title.
        return {tags.normalize(word) or word
                for word in re.findall(r"[^\W_]+", text.casefold())}

    query_words = words(query) - {"", "the", "a", "an", "and"}
    if not query_words:
        return albums

    def matches(word, candidates):
        return any(word == candidate or tags.similarity(word, candidate)
                   >= cfg.ARTIST_NAME_THRESH for candidate in candidates)

    kept = []
    for album in albums:
        artist_words = words((album.get("artist") or {}).get("name") or "")
        title_words = words(album.get("title") or "") | words(album.get("version") or "")
        if (all(matches(word, artist_words) for word in query_words)
                and not any(matches(word, title_words) for word in query_words)):
            continue
        kept.append(album)
    return kept


def _qobuz_url(query):
    """Whether ``query`` is a qobuz.com URL, and the (kind, id) it names."""
    try:
        parts = urllib.parse.urlsplit(query)
        netloc = parts.netloc.lower()
        is_qobuz_url = (parts.scheme in ("http", "https")
                        and (netloc == "qobuz.com"
                             or netloc.endswith(".qobuz.com")))
    except ValueError:
        is_qobuz_url = False
    parsed = cli.parse_qobuz_url(query) if is_qobuz_url else None
    return is_qobuz_url, parsed


async def _fetch_album(album_id, query, token):
    """One album by id, as a one-row result."""
    raw = []
    error = None
    try:
        raw = [await qobuz_access._qobuz_call(
            qobuz_search.get_album, album_id, token)]
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."
    except (AuthLost, QobuzUnavailable):
        raise
    except QobuzError:
        error = "Couldn't fetch that album."
    except Exception:
        _log.exception(
            "album fetch failed for %r", query)
        error = "Couldn't fetch that album."
    return raw, error


async def _fetch_track(track_id, token):
    """The track a pasted track URL names, as a one-track result."""
    raw = []
    error = None
    try:
        track = await qobuz_access._qobuz_call(
            qobuz_search.get_track, track_id, token)
        raw = [track] if track else []
        if not raw:
            error = "Couldn't fetch that track. Check the URL."
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."
    except (AuthLost, QobuzUnavailable):
        raise
    except QobuzError:
        error = "Couldn't fetch that track. Check the URL."
    return raw, error


async def _fetch_artist_albums(artist_id, artist_name, query, token):
    """An artist's albums, and the header that names the artist."""
    raw = []
    selected_artist = None
    error = None
    try:
        raw, artist_total = await qobuz_access._qobuz_call(
            qobuz_search.get_artist_albums, artist_id, token,
            limit=cfg.ARTIST_CATALOG_LIMIT)
        selected_artist = {
            "id": artist_id,
            "name": artist_name or query,
            "total": artist_total,
            "shown": len(raw),
        }
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."
    return raw, selected_artist, error


async def _release_types(artist_id, raw, token):
    """The Qobuz list each of an artist's releases is filed in, or None when
    the lists can't be read and the albums are listed in one run."""
    try:
        return await qobuz_access._qobuz_call(
            qobuz_search.get_artist_release_types, artist_id, token,
            [a["id"] for a in raw if a.get("id") is not None])
    except (asyncio.TimeoutError, AuthLost, QobuzError, QobuzUnavailable) as exc:
        _log.info("release lists for artist %s couldn't be read: %s",
                  artist_id, exc)
        return None


async def _artist_album_results(artist_id, raw, query, token, queued_albums,
                                scanning_albums, sectioned=True):
    """An artist's records for the page, and whether they are in sections.
    The release lists are read while the Owned marks are worked out."""
    types = None
    if sectioned:
        (results, groups), types = await asyncio.gather(
            _album_results(raw, query, token, queued_albums, scanning_albums),
            _release_types(artist_id, raw, token))
    else:
        results, groups = await _album_results(
            raw, query, token, queued_albums, scanning_albums)
    _order_artist_albums(artist_id, groups, types)
    return results, groups, types is not None


def _order_artist_albums(artist_id, groups, types):
    """Sort an artist's records oldest first by the year on their row, then
    by title, under the section of the Qobuz list each is filed in when
    ``types`` is known."""
    rank = {name: i for i, name in enumerate(_ARTIST_SECTIONS)}
    for g in groups:
        g["section"] = None
        if types is not None:
            main = any(str(e["artist_id"]) == str(artist_id) for e in g["editions"])
            g["section"] = next((types[str(e["id"])] for e in g["editions"]
                                 if str(e["id"]) in types),
                                "album" if main else "appears")
    groups.sort(key=lambda g: (rank.get(g["section"], 0), g["sort_year"],
                               tags.normalize(g["title"])))
    for index, g in enumerate(groups):
        g["index"] = index


def _section_headings(groups, batch, shown=()):
    """Give the first record of each section in ``batch`` its section's
    heading, unless a record of that section is on the page already, at a
    position in ``shown``."""
    sections = {}
    for g in groups:
        if g["section"] is None:
            return
        s = sections.setdefault(g["section"], {
            "label": _ARTIST_SECTIONS[g["section"]], "order": g["index"],
            "count": 0, "missing": 0, "listed": False})
        s["count"] += 1
        s["missing"] += not g["owned"]
        if g["index"] in shown:
            s["listed"] = True
    for g in batch:
        s = sections[g["section"]]
        g["heading"] = None if s["listed"] else s
        s["listed"] = True


async def _search_artists(query, token):
    """Artists whose name matches ``query``."""
    artist_results = []
    error = None
    try:
        artist_raw = await qobuz_access._qobuz_call(
            qobuz_search.search_artists, query, token,
            limit=cfg.ARTIST_LOOKUP_LIMIT)
        for a in artist_raw:
            if not a.get("id"):
                continue
            img = a.get("image") or {}
            cover = ""
            if isinstance(img, dict):
                cover = img.get("small") or img.get("thumbnail") or ""
            albums_count = a.get("albums_count")
            artist_results.append({
                "id": a.get("id"),
                "name": a.get("name") or "?",
                "cover": cover if str(cover).startswith(
                    "https://static.qobuz.com/") else "",
                "albums_count": (
                    albums_count
                    if isinstance(albums_count, int)
                    and albums_count > 0 else None),
            })
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."
    return artist_results, error


async def _search_catalog(kind, query, token):
    """Albums or tracks matching ``query``, without the albums that match it
    only by artist name."""
    raw = []
    artist_only_count = 0
    error = None
    _search_fn = qobuz_search.search_tracks if kind == "track" else qobuz_search.search_albums
    try:
        raw = await qobuz_access._qobuz_call(
            _search_fn, query, token, limit=cfg.SEARCH_LIMIT)
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."

    if kind == "album":
        kept = _filter_artist_only_albums(raw, query)
        artist_only_count = len(raw) - len(kept)
        raw = kept
    return raw, artist_only_count, error


async def _track_results(raw, query, token, queued_tracks):
    """Rows for a track search, each marked owned when that track is on
    disk."""
    results = []
    loop = asyncio.get_running_loop()
    _track_raws = []
    for t in raw:
        alb = t.get("album") or {}
        if not t.get("id") or not alb.get("id"):
            continue
        _tbd, _tsr = download_admission._qobuz_quality_bits_rate(t, alb)
        _timg = alb.get("image") or {}
        _tcover = _timg.get("small") or _timg.get("thumbnail") or ""
        _perf = (t.get("performer") or {}).get("name")
        results.append({
            "track_id":    t.get("id"),
            "album_id":    alb.get("id"),
            "title":       t.get("title") or "?",
            "version":     t.get("version") or alb.get("version") or "",
            "artist":      (alb.get("artist") or {}).get("name") or _perf or "?",
            "artist_id":   (alb.get("artist") or {}).get("id"),
            "album_title": alb.get("title") or "?",
            "year":        catalog.album_year(alb) or "?",
            "track_n":     t.get("track_number") or "?",
            "total":       alb.get("tracks_count") or "?",
            "quality":     _qobuz_quality_short_label(t, alb),
            "hires":       _tbd >= 24,
            "lossy":       _tbd == 0,
            "bit_depth":   _tbd,
            "sample_rate": _tsr,
            "cover":       _tcover if _tcover.startswith(
                "https://static.qobuz.com/") else "",
            "owned":       False,
            "queued":      (str(alb.get("id")), str(t.get("id")))
                           in queued_tracks,
            "scanning":    False,
        })
        _track_raws.append(t)

    if _track_raws:
        def _annotate_owned_tracks():
            albums = {}
            for res, track in zip(results, _track_raws):
                album = track.get("album") or {}
                album_id = str(album.get("id") or "")
                group = albums.setdefault(
                    album_id, {"album": album, "results": []})
                group["results"].append(res)

            for album_id, group in albums.items():
                try:
                    folder = (catalog.find_album_dir_filesystem(group["album"])
                              or catalog.find_album_dir_filesystem(group["album"], exact_edition=True))
                    if folder is None:
                        continue
                    exact_album = api_client.call_within(
                        cfg.WEB_FETCH_TIMEOUT,
                        qobuz_search.get_album,
                        album_id,
                        token,
                    )
                    qobuz_tracks = (
                        (exact_album.get("tracks") or {}).get("items")
                        or []
                    )
                    if not qobuz_tracks:
                        continue
                    present_ids = catalog.owned_edition_track_ids(exact_album)
                    for res in group["results"]:
                        res["owned"] = str(res["track_id"]) in present_ids
                except Exception:
                    _log.exception(
                        "track ownership annotation failed for album %s", album_id)

        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _annotate_owned_tracks),
                timeout=_OWNERSHIP_TIMEOUT)
        except asyncio.TimeoutError:
            _log.warning(
                "track ownership annotation timed out (%ss) for %r; "
                "results shown without Owned marks",
                _OWNERSHIP_TIMEOUT,
                query,
            )
        except Exception:
            _log.exception(
                "track ownership annotation failed for %r", query)
    return results


async def _album_results(raw, query, token, queued_albums, scanning_albums):
    """Rows for an album list, marked owned, part-owned or missing, and
    grouped one row per record."""
    results = []
    album_groups = []
    loop = asyncio.get_running_loop()
    _album_raws = []
    for a in raw:
        if not a.get("id"):
            continue
        _bd, _sr = download_admission._qobuz_quality_bits_rate(a)
        _img = a.get("image") or {}
        _cover = _img.get("small") or _img.get("thumbnail") or ""
        _qual = _qobuz_quality_short_label(a)
        results.append({
            "id":      a.get("id"),
            "title":   a.get("title") or "?",
            "artist":  (a.get("artist") or {}).get("name") or "?",
            "artist_id": (a.get("artist") or {}).get("id"),
            "year":    catalog.album_year(a) or "?",
            "tracks":  a.get("tracks_count") or "?",
            "quality": _qual,
            "hires":   _bd >= 24,
            "lossy":   _bd == 0,
            "bit_depth": _bd,
            "sample_rate": _sr,
            "cover":   _cover if _cover.startswith(
                "https://static.qobuz.com/") else "",
            "owned":   False,
            "as_new_edition": False,
            "ownership_unknown": True,
            "queued":  str(a.get("id")) in queued_albums,
            "scanning": str(a.get("id")) in scanning_albums,
        })
        _album_raws.append(a)

    if _album_raws:
        def _annotate_owned():
            def fetch_album(album_id):
                return api_client.call_within(
                    cfg.WEB_FETCH_TIMEOUT, qobuz_search.get_album, album_id, token)

            annotations = []
            for alb in _album_raws:
                try:
                    annotations.append(
                        catalog.search_result_ownership(alb, fetch_album))
                except Exception:
                    annotations.append({"ownership_unknown": True})
                    _log.exception(
                        "ownership annotation failed for album %s", alb.get("id"))
            return annotations
        try:
            annotations = await asyncio.wait_for(
                loop.run_in_executor(None, _annotate_owned),
                timeout=_OWNERSHIP_TIMEOUT)
            for res, annotation in zip(results, annotations):
                res.update(annotation)
        except asyncio.TimeoutError:
            _log.warning(
                "ownership annotation timed out (%ss) for %r; results "
                "shown without Owned marks", _OWNERSHIP_TIMEOUT, query)
        except Exception:
            _log.exception(
                "ownership annotation failed for %r", query)

    # Collapse the flat result list into one row per album: a
    # remaster, deluxe, and box set of the same record group together
    # with the alternates tucked under the main row, instead of the
    # same album scattering down the page.
    if _album_raws:
        by_key = {}
        group_raws = []
        for res, alb in zip(results, _album_raws):
            ver = alb.get("version") or ""
            identity_title = res["title"]
            if ver:
                identity_title += f" ({ver})"
            fingerprint = hidden_mod.album_fingerprint(
                res["artist"], tags.strip_leading_article(identity_title)
            )
            # Unknown identity must fail open into its own result.
            # Sharing an empty fuzzy key hides unrelated releases.
            key = (("album", fingerprint) if fingerprint else
                   ("release", str(res["id"])))
            if key not in by_key:
                by_key[key] = (dict(res, editions=[]), [])
                album_groups.append(by_key[key][0])
                group_raws.append(by_key[key][1])
            g, raws = by_key[key]
            raws.append(alb)
            if res.get("disk_year"):
                g["disk_year"] = res["disk_year"]
            # Each edition keeps its own title and its own ownership
            # verdict, so its row and download confirmation name that
            # pressing and count its own tracks.
            g["editions"].append({
                "id": res["id"],
                "title": res["title"],
                "artist": res["artist"],
                "artist_id": res["artist_id"],
                "version": (alb.get("version") or "").strip(),
                "year": res["year"], "tracks": res["tracks"],
                "quality": res["quality"], "hires": res["hires"],
                "lossy": res["lossy"], "bit_depth": res["bit_depth"],
                "sample_rate": res["sample_rate"],
                "cover": res["cover"],
                "owned": bool(res["owned"]),
                "bonus_versions": res.get("bonus_versions", 0),
                "as_new_edition": res["as_new_edition"],
                "queued": bool(res["queued"]),
                "scanning": bool(res["scanning"]),
                "partial": bool(res.get("partial")),
                "have_tracks": res.get("have_tracks"),
                "want_tracks": res.get("want_tracks"),
                "folder": res.get("folder", ""),
                "ownership_unknown": res["ownership_unknown"],
            })
        for index, (g, raws) in enumerate(zip(album_groups, group_raws)):
            g["index"] = index
            eds = g["editions"]
            # A complete edition outranks a part-finished one: if any
            # edition of this record is whole on disk, search must not
            # offer a plain Download for the record at all. An
            # instrumental or karaoke copy is not the record.
            derivative = ([catalog.is_derivative_release(alb) for alb in raws]
                          if len(raws) > 1 else [False])
            owned = [i for i, e in enumerate(eds)
                     if e["owned"] and (not derivative[i] or all(derivative))]
            g["owned"] = bool(owned)
            # The row shows a complete copy you own, so the rest read as
            # "other versions". Otherwise it shows the original release,
            # not whichever deluxe or remaster Qobuz listed first; a
            # part-finished edition keeps its own "N of M" among the
            # versions.
            if owned:
                rep_i = owned[0]
                if g.get("disk_year"):
                    for i in owned:
                        if str(eds[i]["year"]) == str(g["disk_year"]):
                            rep_i = i
                            break
            else:
                original = catalog._best_edition(raws, prefer_hires=False)
                rep_i = next(i for i, alb in enumerate(raws) if alb is original)
            g["sort_year"] = catalog.album_year_int(raws[rep_i])
            if rep_i:
                eds.insert(0, eds.pop(rep_i))
            rep = eds[0]
            for f in ("id", "title", "artist", "artist_id", "year", "tracks",
                      "quality", "hires", "lossy", "bit_depth",
                      "sample_rate", "cover", "version", "queued", "scanning",
                      "as_new_edition", "folder", "ownership_unknown",
                      "bonus_versions"):
                g[f] = rep[f]
            g["partial"] = rep["partial"] and not g["owned"]
            g["have_tracks"] = rep["have_tracks"]
            g["want_tracks"] = rep["want_tracks"]
            g["others"] = eds[1:]
            # Tracks of a part-owned version under the original still
            # show on the row, as a count; the version's name is in its
            # own row under Versions.
            part = [e for e in eds[1:] if e["partial"]]
            if part and not g["owned"] and not g["partial"]:
                best = max(part, key=lambda e: e["have_tracks"] or 0)
                g["other_have"] = best["have_tracks"]
                g["other_want"] = best["want_tracks"]
                g["other_folder"] = best["folder"]
    return results, album_groups


def _in_library(group):
    """Whether any version of a record has files in the library."""
    return any(e["owned"] or e["folder"] for e in group["editions"])


def _album_batch(groups, through=(), listed=None):
    """The records an artist's next batch lists, and the next batch's start,
    count, how many records are left after this one and the token naming
    every record on the page by then.

    The first batch, with ``listed`` None, also lists every record with a
    copy in the library. A later batch takes the next records that are not
    in ``listed``, a token from _listed_token, so a record whose place or
    Owned mark changed since is neither repeated nor left out. ``through``
    names album ids the batch has to reach, such as picks kept from before a
    reload."""
    on_page = set()
    if listed is None:
        remaining = groups
    else:
        listed_ids = _listed_ids(groups, listed)
        on_page = {g["index"] for g in groups
                   if any(str(e["id"]) in listed_ids for e in g["editions"])}
        remaining = [g for g in groups if g["index"] not in on_page]
    end = _ARTIST_ALBUM_BATCH
    wanted = {str(album_id) for album_id in through}
    if wanted:
        last = max((i for i, g in enumerate(remaining)
                    if any(str(e["id"]) in wanted for e in g["editions"])),
                   default=-1)
        while end <= last:
            end += _ARTIST_ALBUM_BATCH
    batch = remaining[:end]
    if listed is None:
        batch = [g for g in groups if g["index"] < end or _in_library(g)]
    _section_headings(groups, batch, shown=on_page)
    on_page |= {g["index"] for g in batch}
    left = len(groups) - len(on_page)
    if not left:
        return batch, None
    return batch, {"start": remaining[end - 1]["index"] + 1, "count": min(left, _ARTIST_ALBUM_BATCH),
                   "left": left, "total": len(groups),
                   "listed": _listed_token(groups, on_page)}


def _album_id_hash(album_id):
    return hashlib.blake2s(str(album_id).encode(), digest_size=4).digest()


def _listed_token(groups, indexes):
    """The album ids of the records at ``indexes``, four bytes of a hash
    each, so the token names the same albums when the artist's list gains,
    loses or reorders albums between requests."""
    hashes = sorted({_album_id_hash(e["id"]) for g in groups
                     if g["index"] in indexes for e in g["editions"]})
    return base64.urlsafe_b64encode(b"".join(hashes)).decode().rstrip("=")


def _listed_ids(groups, token):
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        return set()
    hashes = {raw[i:i + 4] for i in range(0, len(raw) - 3, 4)}
    return {str(e["id"]) for g in groups for e in g["editions"]
            if _album_id_hash(e["id"]) in hashes}


def _search_error(exc, query):
    """What Search says about a request that raised ``exc``."""
    if isinstance(exc, NoCredsError):
        return "No Qobuz credentials set. Visit Settings."
    if isinstance(exc, AuthLost):
        return "Token is expired or invalid. Update it in Settings."
    if isinstance(exc, QobuzUnavailable):
        return str(exc)
    if not isinstance(exc, QobuzError):
        _log.exception("search failed for %r", query)
    return "Search failed. Try again."


@router.post("/search", response_class=HTMLResponse)
async def do_search(request: Request, q: str = Form("", max_length=500),
                    kind: str = Form("album"),
                    artist_id: str = Form(""),
                    artist_name: str = Form(""),
                    album_id: str = Form("")):
    results = []
    album_groups = []
    artist_results = []
    artist_only_count = 0
    selected_artist = None
    sectioned = False
    error = None
    query = q.strip()
    kind_raw = str(kind).strip().lower()
    kind = kind_raw if kind_raw in ("artist", "track") else "album"
    artist_id = str(artist_id or "").strip()
    artist_name = str(artist_name or "").strip()
    album_id = str(album_id or "").strip()
    if not rendering._is_htmx(request):
        return RedirectResponse(url="/", status_code=303)
    if query or (kind == "album" and album_id):
        try:
            token = qobuz_access._get_token()
            is_qobuz_url, parsed = _qobuz_url(query)
            raw = []
            if kind == "album" and (album_id or (parsed and parsed[0] == "album")):
                raw, error = await _fetch_album(album_id or parsed[1], query, token)
            elif parsed and parsed[0] == "album" and kind == "track":
                error = ("That's an album URL. Switch to Album to download it, "
                         "or paste a single track to download one track.")
            elif parsed and parsed[0] == "album" and kind == "artist":
                error = "That's an album URL. Switch to Album to download it."
            elif parsed and parsed[0] == "track" and kind == "track":
                raw, error = await _fetch_track(parsed[1], token)
            elif parsed and parsed[0] == "track":
                # Album mode: a track URL points the user at the Track toggle.
                error = ("That's a track URL. Switch to Track to download one "
                         "track, or paste the album URL in Album mode.")
            elif parsed or is_qobuz_url:
                # Another Qobuz URL kind (artist, playlist), or a qobuz.com URL
                # in no recognised format.
                if kind == "artist":
                    error = "Search artists by name. Paste album or track URLs only."
                else:
                    error = ("Only Qobuz album and track URLs are supported. "
                             "Search for an artist by name instead.")
            elif kind == "artist" and artist_id:
                raw, selected_artist, error = await _fetch_artist_albums(
                    artist_id, artist_name, query, token)
            elif kind == "artist":
                artist_results, error = await _search_artists(query, token)
            else:
                raw, artist_only_count, error = await _search_catalog(
                    kind, query, token)

            queued_albums, queued_tracks, scanning_albums = (
                download_admission._active_search_downloads())
            if kind == "track":
                results = await _track_results(raw, query, token, queued_tracks)
            elif selected_artist:
                results, album_groups, sectioned = await _artist_album_results(
                    artist_id, raw, query, token, queued_albums, scanning_albums)
            elif kind == "album":
                results, album_groups = await _album_results(
                    raw, query, token, queued_albums, scanning_albums)
        except Exception as exc:
            error = _search_error(exc, query)
    album_total = len(album_groups)
    album_more = None
    if selected_artist and album_total > _ARTIST_ALBUMS_AT_ONCE:
        album_groups, album_more = _album_batch(album_groups)
        album_more["url"] = _album_batch_url(
            artist_id, artist_name, query, album_more["listed"], sectioned)
    elif selected_artist:
        _section_headings(album_groups, album_groups)
    creds_ok = qobuz_access._creds_ok()
    search_state = f"{kind}|{query}"
    if artist_id:
        search_state += f"|artist:{artist_id}"
    if kind == "album" and album_id:
        search_state += f"|album:{album_id}"
    search_result_count = len(results) if results else len(artist_results)
    if album_more:
        search_result_count = sum(len(g["editions"]) for g in album_groups)
    search_count = (len(album_groups) if kind == "album" else
                    len(artist_results) if kind == "artist" else len(results))
    search_count_label = plural(search_count, kind)
    if kind == "album":
        search_count_label += " on Qobuz"
    search_cacheable = search_result_count <= _SEARCH_SNAPSHOT_RESULT_LIMIT
    ctx = {"q": query, "results": results, "album_groups": album_groups,
           "album_total": album_total, "album_more": album_more,
           "artist_results": artist_results, "selected_artist": selected_artist,
           "error": error, "kind": kind,
           "search_count_label": search_count_label,
           "artist_only_count": artist_only_count,
           "artist_only_label": plural(artist_only_count, "album"),
           "search_state": search_state,
           "search_cacheable": search_cacheable,
           "defer_search_views": not search_cacheable or album_more is not None,
           "creds_ok": creds_ok, "qobuz_ready": qobuz_access._qobuz_ready(), "page": "search"}
    if rendering._is_htmx(request):
        resp = rendering._tr(request, "_search_results.html", ctx)
        # Put the search in the address bar, so a reload or Back lands on the
        # same results. GET / rehydrates from these.
        if ((query or (kind == "album" and album_id))
                and request.headers.get("HX-History-Restore-Request") != "true"):
            params = {"kind": kind, "q": query}
            if artist_id:
                params["artist_id"] = artist_id
                if artist_name:
                    params["artist_name"] = artist_name
            if kind == "album" and album_id:
                params["album_id"] = album_id
            resp.headers["HX-Push-Url"] = "/?" + urllib.parse.urlencode(params)
        return resp
    return RedirectResponse(url="/", status_code=303)


def _album_batch_url(artist_id, artist_name, query, listed, sectioned):
    return "/search/artist-albums?" + urllib.parse.urlencode(
        {"artist_id": artist_id, "artist_name": artist_name, "q": query,
         "listed": listed, "sections": 1 if sectioned else 0})


@router.get("/search/artist-albums", response_class=HTMLResponse)
async def search_artist_albums(request: Request, artist_id: str = "",
                               artist_name: str = "", q: str = "",
                               start: int = 0, listed: str = "",
                               sections: int = 0,
                               through: list[str] = Query(default=[])):
    """The next batch of a long artist list, rows for both views."""
    artist_id = artist_id.strip()[:64]
    artist_name = artist_name.strip()[:200]
    query = q.strip()[:200]
    if not artist_id or start <= 0:
        return HTMLResponse("")
    album_groups = []
    album_more = None
    try:
        token = qobuz_access._get_token()
        raw, selected_artist, error = await _fetch_artist_albums(
            artist_id, artist_name, query, token)
        if not error:
            queued_albums, _queued_tracks, scanning_albums = (
                download_admission._active_search_downloads())
            _results, album_groups, sectioned = await _artist_album_results(
                artist_id, raw, query, token, queued_albums, scanning_albums,
                sectioned=bool(sections))
            if sections and not sectioned:
                error = "Couldn't read Qobuz's release lists. Try again."
    except Exception as exc:
        error = _search_error(exc, query)
    if error:
        return HTMLResponse(rendering._ql_notice_html("error", html.escape(error)))
    album_groups, album_more = _album_batch(
        album_groups, through, listed[:6 * cfg.ARTIST_CATALOG_LIMIT])
    if album_more:
        album_more["url"] = _album_batch_url(
            artist_id, artist_name, query, album_more["listed"], sectioned)
    return rendering._tr(request, "_search_results.html", {
        "album_batch": True, "album_groups": album_groups,
        "album_more": album_more, "selected_artist": selected_artist,
        "q": query, "page": "search"})


def _track_length(seconds):
    s = max(0, int(round(float(seconds or 0))))
    return f"{s // 60}:{s % 60:02d}"


def _album_tracklist(album_id, token):
    """One release's tracks, with the ones in its library folder marked."""
    album = api_client.call_within(
        cfg.WEB_FETCH_TIMEOUT, qobuz_search.get_album, album_id, token)
    items = (album.get("tracks") or {}).get("items") or []
    present_ids = set()
    try:
        present_ids = {str(t.get("id")) for t in catalog.find_edition_tracks(album)[3]}
    except Exception:
        # A library that can't be read still leaves a usable tracklist; the
        # marks are the only thing lost.
        _log.exception(
            "tracklist ownership check failed for album %r", album_id)
    tracks = []
    for i, t in enumerate(items, 1):
        title = t.get("title") or "?"
        version = (t.get("version") or "").strip()
        tracks.append({
            "n": t.get("track_number") or i,
            "disc": t.get("media_number") or 1,
            "title": title,
            "version": "" if version.casefold() in title.casefold() else version,
            "length": _track_length(t.get("duration")),
            "owned": str(t.get("id")) in present_ids})
    return tracks


@router.get("/search/album-tracks", response_class=HTMLResponse)
async def search_album_tracks(request: Request, album_id: str = ""):
    """The tracks under one search row, fetched when the row is opened rather
    than shipped with every result."""
    album_id = str(album_id or "").strip()
    if not album_id or not qobuz_access._qobuz_ready():
        return HTMLResponse("")
    loop = asyncio.get_running_loop()
    error = ""
    tracks = []
    try:
        token = qobuz_access._get_token()
        tracks = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: _album_tracklist(album_id, token)),
            timeout=cfg.WEB_FETCH_TIMEOUT)
    except NoCredsError:
        error = "No Qobuz credentials set. Visit Settings."
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."
    except AuthLost:
        error = "Token is expired or invalid. Update it in Settings."
    except QobuzUnavailable as exc:
        error = str(exc)
    except QobuzError:
        error = "Qobuz could not list these tracks. Try again."
    except Exception:
        _log.exception(
            "tracklist failed for album %r", album_id)
        error = "Qobuz could not list these tracks. Try again."
    return rendering._tr(request, "_search_tracklist.html", {
        "tracks": tracks,
        "partly_owned": any(t["owned"] for t in tracks)
        and not all(t["owned"] for t in tracks),
        "multi_disc": len({t["disc"] for t in tracks}) > 1,
        "error": error,
        "page": "search"})


@router.post("/download", response_class=HTMLResponse)
async def queue_download(request: Request, album_id: str = Form(""),
                         as_new_edition: str = Form(""),
                         track_id: str = Form("")):
    busy = refusals._lock_busy_response(request, downloads=True)
    if busy is not None:
        return busy
    album_id = album_id.strip()
    track_id = track_id.strip()
    if not album_id:
        msg = "Missing album id."
        if rendering._is_htmx(request):
            # 200, not 400: htmx only swaps 2xx/3xx responses, so a 400
            # fragment is silently dropped and the user sees no feedback.
            return _download_fragment("error", html.escape(msg), "failed")
        return RedirectResponse(url="/queue?error=" + rendering._notice_key(msg),
                                status_code=303)
    # "Get this edition too": download a different edition of an album the
    # user already owns, as a separate album.
    download_as_new_edition = str(as_new_edition).strip().lower() in (
        "1", "true", "yes", "on")
    # Refuse true duplicates (same album already active or pending), but only
    # of the SAME intent (see _duplicate_download_job).
    existing = download_admission._duplicate_download_job(album_id, track_id, download_as_new_edition)
    if existing:
        if rendering._is_htmx(request):
            return _download_fragment(
                "warning",
                f'Already queued. <a href="/jobs/{existing.id}" '
                f'class="ql-inline-link">View job</a>.',
                "duplicate",
            )
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    loop = asyncio.get_running_loop()
    root_state, recorded_albums = await loop.run_in_executor(
        None, collection_snapshot.music_root_write_state)
    if root_state != "ready":
        msg = storage._music_write_target_message(root_state, recorded_albums)
        if rendering._is_htmx(request):
            return _download_fragment("error", html.escape(msg), "failed")
        return RedirectResponse(
            url="/queue?error=" + rendering._notice_key(msg), status_code=303)
    try:
        credentials = await qobuz_access._authorize_qobuz_for_web(
            QobuzAccess.DOWNLOAD_ACTION
        )
        token = credentials.token
        album = await qobuz_access._qobuz_call(qobuz_search.get_album, album_id, token)
        if download_as_new_edition and await loop.run_in_executor(
            None, lambda: download_admission._same_edition_is_complete(album)
        ):
            msg = "This edition is already in your library."
            if rendering._is_htmx(request):
                return _download_fragment(
                    "warning", html.escape(msg), "owned")
            return RedirectResponse(
                url="/queue?error=" + rendering._notice_key(msg), status_code=303)
        if not download_as_new_edition and track_id:
            # A single track was excluded from the guard entirely, so nothing
            # checked whether that track was already on disk before fetching it
            # again. Ask about the one track, not the whole album.
            def _track_already_there():
                try:
                    _existing, _folder, _missing, present = catalog.find_edition_tracks(
                        album, track_id=track_id)
                except Exception:
                    _log.exception(
                        "track ownership read failed for album %s", album_id)
                    return False
                return any(str(t.get("id") or "") == track_id for t in present)

            if await loop.run_in_executor(None, _track_already_there):
                msg = "That track is already in your library."
                if rendering._is_htmx(request):
                    return _download_fragment(
                        "warning", html.escape(msg), "owned")
                return RedirectResponse(
                    url="/queue?error=" + rendering._notice_key(msg), status_code=303)

        if not download_as_new_edition and not track_id:
            existing, folder, missing, present = await loop.run_in_executor(
                None, catalog.find_edition_tracks, album)
            if existing and not missing and catalog.album_tracks_complete(album):
                msg = "This edition is already in your library."
                if rendering._is_htmx(request):
                    return _download_fragment("warning", html.escape(msg), "owned")
                return RedirectResponse(
                    url="/queue?error=" + rendering._notice_key(msg),
                    status_code=303)
            other = await loop.run_in_executor(None, catalog.other_version_dir, album)
            download_as_new_edition = bool(other and folder is None)
        title  = album.get("title") or "?"
        artist = (album.get("artist") or {}).get("name") or "?"
        single_track = None
        if track_id:
            _tracks = (album.get("tracks") or {}).get("items") or []
            single_track = next(
                (t for t in _tracks if str(t.get("id")) == track_id), None)
            if single_track is None:
                msg = "That track isn't on this album."
                if rendering._is_htmx(request):
                    # 200, not 400: htmx drops non-2xx/3xx fragments, so a 400
                    # here renders nothing. The notice conveys the failure.
                    return _download_fragment(
                        "error", html.escape(msg), "failed")
                return RedirectResponse(
                    url="/queue?error=" + rendering._notice_key(msg), status_code=303)
        job = job_mgr.Job(
            title=(single_track.get("title") or title) if single_track else title,
            artist=artist,
            album_id=album_id,
            edition=str(
                (
                    (single_track.get("version") if single_track else None)
                    or album.get("version")
                    or ""
                )
            ).strip(),
        )
        if single_track:
            # Flagging it now (before the run fills in the undo details) is what
            # tells the UI to hide Cancel on this job: a one-track download is done
            # before you could catch it.
            job.single = {"album_id": album_id, "track_id": str(track_id)}
        if download_as_new_edition:
            # Retry rebuilds the run from the persisted job, so the edition
            # override has to live on the job. Closure-only, a retried "get
            # this edition too" would fall back to the owned-album skip.
            job.execute_args = {"new_edition": True}

        # Re-check under the lock right before submitting: closes the race with
        # a concurrent /download for the same album across the get_album await.
        with download_admission._DOWNLOAD_SUBMIT_LOCK, qobuz_access._CREDENTIAL_LOCK:
            dup = download_admission._duplicate_download_job(album_id, track_id, download_as_new_edition)
            if dup:
                if rendering._is_htmx(request):
                    return _download_fragment(
                        "warning",
                        f'Already queued. <a href="/jobs/{dup.id}" '
                        f'class="ql-inline-link">View job</a>.',
                        "duplicate",
                    )
                return RedirectResponse(url=f"/jobs/{dup.id}", status_code=303)
            busy = refusals._lock_busy_response(request, downloads=True)
            if busy is not None:
                return busy
            if not qobuz_access._credential_generation_is_active(credentials.generation):
                raise CredentialChanged(
                    "Qobuz credentials changed before the download was queued."
                )
            run_fn = (track_downloads._make_single_track_run(album, single_track, token)
                      if single_track
                      else job_runs._make_download_run(
                          album, token, treat_as_new=download_as_new_edition))
            if job_mgr.submit(job, run_fn) is None:
                return refusals._job_admission_response(request)
        if rendering._is_htmx(request):
            response = rendering._tr(request, "_job_queued.html", {"job": job})
            response.headers["X-QL-Download-Outcome"] = "queued"
            return response
        # Land on the new job's page so the user sees their download starting.
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    except (NoCredsError, DownloaderNotReady) as exc:
        msg = job_mgr.qobuz_action_error_message(exc, unchanged=True)
        if rendering._is_htmx(request):
            return _download_fragment("error", html.escape(msg), "failed")
        return RedirectResponse(url="/settings#qobuz", status_code=303)
    except Exception as e:
        _log.warning("couldn't queue download for album %s", album_id,
                     exc_info=True)
        user_msg = download_outcomes._download_error_message(
            e,
            "Couldn't queue download. Try again.",
        )
        if rendering._is_htmx(request):
            return _download_fragment(
                "error", html.escape(user_msg), "failed")
        msg = rendering._notice_key(user_msg)
        return RedirectResponse(url=f"/queue?error={msg}", status_code=303)
