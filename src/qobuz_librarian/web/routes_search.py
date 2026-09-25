"""Routes for the Search page and downloads."""
import asyncio
import html
import logging
import re
import threading
import time
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from qobuz_librarian import cli, download, state_file
from qobuz_librarian import config as cfg
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api import search as qobuz_search
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    NoCredsError,
    QobuzAccess,
    QobuzError,
    QobuzUnavailable,
)
from qobuz_librarian.integrations import lyrics as lyrics_mode
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
from qobuz_librarian.web import flows, runtime, scans
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


def _download_fragment(kind: str, body: str, outcome: str) -> HTMLResponse:
    return HTMLResponse(
        runtime._ql_notice_html(kind, body),
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
        and not runtime._web_writes_paused()
        and runtime._qobuz_ready()
        and generation_state.baseline_complete()
        and time.monotonic() - _unreadable_checked_at
            >= _UNREADABLE_RECHECK_SECONDS
        and unreadable_artists_mod.load()
    )


def _library_scan_resume_due():
    """Whether an interrupted library scan is waiting to resume, from local
    state alone."""
    return bool(
        cfg.AUTO_LIBRARY_SCAN
        and not runtime._web_writes_paused()
        and runtime._qobuz_ready()
        and not generation_state.baseline_complete()
        and scan_checkpoint.pending() is not None
    )


def _start_due_jobs_in_background():
    """Run the dashboard's automatic starts off the request. Each begins with a
    live Qobuz check, which must never hold up the page."""
    try:
        due = (_library_scan_resume_due() or _unreadable_recheck_pending()
               or runtime._new_release_check_due())
    except OSError as e:
        _log.warning(
            "automatic start from the dashboard skipped: %s", e)
        return
    if not due:
        return
    if not _auto_start_busy.acquire(blocking=False):
        return

    def run():
        try:
            _maybe_resume_library_scan()
            runtime._maybe_auto_check_new_releases()
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
    credentials = runtime._auto_start_credentials()
    if credentials is None:
        return
    with runtime._auto_check_lock:
        if any(j.status != job_mgr.JobStatus.AWAITING_REVIEW
               for j in job_mgr.registry.pending_and_running()):
            return
        if readable_again:
            scans._start_library_scan(credentials)
            return
        cp = scan_checkpoint.pending()
        if cp is not None:
            scans._start_library_scan(
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
        _start_due_jobs_in_background()
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
            "baseline_complete": generation_state.baseline_complete(),
            "setup_scanning": scans._active_library_scan() is not None,
            "library_scan_state": scan_state,
            # An interrupted gap-scan, surfaced on the dashboard the way
            # /library already does, gated on no scan running.
            "library_resume": (
                lambda cp, generation: (
                    cp
                    if (
                        cp is not None
                        and scans._active_library_scan() is None
                        and (
                            not int(generation.get("generation") or 0)
                            or str(
                                (generation.get("latest_attempt") or {}).get(
                                    "status"
                                )
                            ) in {"running", "failed", "incomplete"}
                        )
                    )
                    else None
                )
            )(scan_checkpoint.pending(), library_generation),
            # First-run nudge: a fresh install has no creds, so every search/scan
            # would fail cryptically, so surface it up front. Filesystem-only.
            "creds_ok": runtime._creds_ok(),
            "qobuz_ready": runtime._qobuz_ready(),
            "lyric_retry_count":
                len(lyrics_mode.load_lyric_retry()) if cfg.LYRIC_RETRY_FILE.exists() else 0,
            "staging_album_count": 0 if active_jobs else _staging_album_count(),
            # A store that couldn't be read was kept aside and the run fell back
            # to defaults, and only the container log said so, which nobody reads.
            "corrupt_stores": state_file.corrupt_store_details(),
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
    return runtime._tr(request, "index.html", {
        "active_jobs": active_jobs,
        "pending": pending,
        "queue_waits": {j.id: runtime._queue_wait(j) for j in pending},
        "review": job_mgr.registry.awaiting_review(),
        "creds_token_valid": runtime._token_valid_for(),
        "search_q": search_q,
        "search_kind": search_kind,
        "search_artist_id": search_artist_id,
        "search_artist_name": search_artist_name,
        "search_album_id": search_album_id,
        "auto_search": bool(search_q or search_album_id),
        "page": "dashboard",
        **disk,
    })


@router.post("/lyric-retry")
async def lyric_retry(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    # A retry and a full backfill share the one lyric-state file, so they must
    # never run at once, so fold onto whichever lyrics pass is already in flight.
    existing = scans._active_scan(
        "lyrics", statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    job = job_mgr.Job(title="Lyric retry")
    job.execute_kind = "lyrics"
    if job_mgr.submit(job, lambda j: flows.run_lyric_retry(j)) is None:
        return runtime._job_admission_response(request)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


_SEARCH_SNAPSHOT_RESULT_LIMIT = 150


def _qobuz_quality_short_label(primary: dict | None,
                               fallback: dict | None = None) -> str:
    bits, rate = runtime._qobuz_quality_bits_rate(primary, fallback)
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
    error = None
    query = q.strip()
    kind_raw = str(kind).strip().lower()
    kind = kind_raw if kind_raw in ("artist", "track") else "album"
    artist_id = str(artist_id or "").strip()
    artist_name = str(artist_name or "").strip()
    album_id = str(album_id or "").strip()
    if not runtime._is_htmx(request):
        return RedirectResponse(url="/", status_code=303)
    if query or (kind == "album" and album_id):
        try:
            token = runtime._get_token()

            try:
                _split = urllib.parse.urlsplit(query)
                netloc = _split.netloc.lower()
                is_qobuz_url = (_split.scheme in ("http", "https")
                                and (netloc == "qobuz.com"
                                     or netloc.endswith(".qobuz.com")))
            except ValueError:
                is_qobuz_url = False
            parsed = cli.parse_qobuz_url(query) if is_qobuz_url else None
            raw = []
            loop = asyncio.get_running_loop()
            if kind == "album" and (album_id or (parsed and parsed[0] == "album")):
                try:
                    raw = [await asyncio.wait_for(
                        loop.run_in_executor(
                            None, lambda: api_client.call_within(
                                cfg.WEB_FETCH_TIMEOUT, qobuz_search.get_album,
                                album_id or parsed[1], token)
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )]
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
            elif parsed and parsed[0] == "album" and kind == "track":
                error = ("That's an album URL. Switch to Album to download it, "
                         "or paste a single track to download one track.")
            elif parsed and parsed[0] == "album" and kind == "artist":
                error = "That's an album URL. Switch to Album to download it."
            elif parsed and parsed[0] == "track" and kind == "track":
                # Tracks mode: resolve the pasted track URL to that one track;
                # the track-results loop below renders it for a one-track download.
                try:
                    _t = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: api_client.call_within(
                            cfg.WEB_FETCH_TIMEOUT, qobuz_search.get_track, parsed[1], token)),
                        timeout=cfg.WEB_FETCH_TIMEOUT)
                    raw = [_t] if _t else []
                    if not raw:
                        error = "Couldn't fetch that track. Check the URL."
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
                except (AuthLost, QobuzUnavailable):
                    raise
                except QobuzError:
                    error = "Couldn't fetch that track. Check the URL."
            elif parsed and parsed[0] == "track":
                # Album mode: a track URL -- point the user at the Track toggle
                # instead of the old (now false) "works on albums" message.
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
                try:
                    raw, artist_total = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: api_client.call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                qobuz_search.get_artist_albums,
                                artist_id,
                                token,
                                limit=cfg.ARTIST_CATALOG_LIMIT,
                            ),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                    selected_artist = {
                        "id": artist_id,
                        "name": artist_name or query,
                        "total": artist_total,
                        "shown": len(raw),
                    }
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
            elif kind == "artist":
                try:
                    artist_raw = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: api_client.call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                qobuz_search.search_artists,
                                query,
                                token,
                                limit=cfg.ARTIST_LOOKUP_LIMIT,
                            ),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
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
            else:
                _search_fn = qobuz_search.search_tracks if kind == "track" else qobuz_search.search_albums
                try:
                    raw = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: api_client.call_within(cfg.WEB_FETCH_TIMEOUT, _search_fn,
                                                query, token, limit=cfg.SEARCH_LIMIT),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."

                if kind == "album":
                    kept = _filter_artist_only_albums(raw, query)
                    artist_only_count = len(raw) - len(kept)
                    raw = kept

            queued_albums, queued_tracks, scanning_albums = (
                runtime._active_search_downloads())
            _track_raws = []
            for t in (raw if kind == "track" else []):
                alb = t.get("album") or {}
                if not t.get("id") or not alb.get("id"):
                    continue
                _tbd, _tsr = runtime._qobuz_quality_bits_rate(t, alb)
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
                            folder = catalog.find_album_dir_filesystem(group["album"])
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
                            existing, _ = catalog.find_existing_tracks(
                                exact_album, album_dir=folder)
                            if not existing:
                                continue
                            _missing, present = catalog.compute_missing(
                                qobuz_tracks, existing)
                            present_ids = {
                                str(track.get("id"))
                                for track in present if track.get("id")
                            }
                            for res in group["results"]:
                                res["owned"] = str(res["track_id"]) in present_ids
                        except Exception:
                            _log.exception(
                                "track ownership annotation failed for album %s", album_id)

                try:
                    _own_timeout = 20
                    await asyncio.wait_for(
                        loop.run_in_executor(None, _annotate_owned_tracks),
                        timeout=_own_timeout)
                except asyncio.TimeoutError:
                    _log.warning(
                        "track ownership annotation timed out (%ss) for %r; "
                        "results shown without Owned marks",
                        _own_timeout,
                        query,
                    )
                except Exception:
                    _log.exception(
                        "track ownership annotation failed for %r", query)
            _album_raws = []
            for a in (raw if kind == "album" or selected_artist else []):
                if not a.get("id"):
                    continue
                _bd, _sr = runtime._qobuz_quality_bits_rate(a)
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
                    "ownership_unknown": True,
                    "queued":  str(a.get("id")) in queued_albums,
                    "scanning": str(a.get("id")) in scanning_albums,
                })
                _album_raws.append(a)

            # Flag results already in the library so search never offers a
            # plain Download on an album you own; the app is gap-fill, so
            # that would contradict its own purpose.
            if _album_raws:
                def _annotate_owned():
                    # Same filesystem resolver the download and scan paths use.
                    # "Owned" means COMPLETE, not "a folder with a file in it":
                    # a part-finished album reading "Owned" loses both its
                    # checkbox and its download button, which is the gap-fill
                    # case this app exists for.
                    annotations = []
                    for alb in _album_raws:
                        res = {"ownership_unknown": True}
                        annotations.append(res)
                        try:
                            folder = catalog.find_album_dir_filesystem(alb)
                            if folder is None:
                                res["ownership_unknown"] = False
                                continue
                            exact_album = api_client.call_within(
                                cfg.WEB_FETCH_TIMEOUT,
                                qobuz_search.get_album,
                                alb["id"],
                                token,
                            )
                            qobuz_tracks = (
                                (exact_album.get("tracks") or {}).get("items")
                                or []
                            )
                            if not runtime._album_tracks_complete(exact_album):
                                continue
                            existing, _ = catalog.find_existing_tracks(
                                exact_album, album_dir=folder)
                            if not existing:
                                res["ownership_unknown"] = False
                                continue
                            missing, present = catalog.compute_missing(
                                qobuz_tracks, existing)
                            res["disk_year"] = catalog._dir_year(folder.name)
                            if missing:
                                res["partial"] = True
                                res["have_tracks"] = len(present)
                                res["want_tracks"] = len(qobuz_tracks)
                                res["replaces_existing"] = download.downloads_whole_album(
                                    len(present), len(missing), len(qobuz_tracks))
                            else:
                                res["owned"] = True
                            res["ownership_unknown"] = False
                        except Exception:
                            _log.exception(
                                "ownership annotation failed for album %s", alb.get("id"))
                    return annotations
                try:
                    # Exact ownership may need the selected edition's track
                    # list after the cheap folder check. Keep the annotation
                    # bounded; search results are still useful without it.
                    _own_timeout = 20
                    annotations = await asyncio.wait_for(
                        loop.run_in_executor(None, _annotate_owned),
                        timeout=_own_timeout)
                    for res, annotation in zip(results, annotations):
                        res.update(annotation)
                except asyncio.TimeoutError:
                    _log.warning(
                        "ownership annotation timed out (%ss) for %r; results "
                        "shown without Owned marks", _own_timeout, query)
                except Exception:
                    _log.exception(
                        "ownership annotation failed for %r", query)

            # Collapse the flat result list into one row per album: a
            # remaster, deluxe, and box set of the same record group together
            # with the alternates tucked under the main row, instead of the
            # same album scattering down the page.
            if _album_raws:
                by_key = {}
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
                    g = by_key.get(key)
                    if g is None:
                        g = dict(res, editions=[])
                        by_key[key] = g
                        album_groups.append(g)
                    # A complete edition outranks a part-finished one: if any
                    # edition of this record is whole on disk, search must not
                    # offer a plain Download for the record at all.
                    g["owned"] = g["owned"] or res["owned"]
                    if res.get("disk_year"):
                        g["disk_year"] = res["disk_year"]
                    # Each edition keeps its OWN title and its own ownership
                    # verdict. Sharing the group's title made a plain pressing
                    # render as the deluxe it was grouped under, right down to
                    # the download confirmation naming a record you had not
                    # picked; sharing one verdict put a count from one pressing
                    # beside the track total of another.
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
                        "queued": bool(res["queued"]),
                        "scanning": bool(res["scanning"]),
                        "partial": bool(res.get("partial")),
                        "have_tracks": res.get("have_tracks"),
                        "want_tracks": res.get("want_tracks"),
                        "replaces_existing": bool(res.get("replaces_existing")),
                        "ownership_unknown": res["ownership_unknown"],
                    })
                for g in album_groups:
                    eds = g["editions"]
                    # The row shows exactly one edition, so it has to be one the
                    # ownership check actually ran against: a complete copy
                    # first (that is the one you own, and the rest read as
                    # "other versions"), then a part-finished one, so the
                    # "N of M" beside it counts the same pressing the Download
                    # button would fetch.
                    rep_i = 0
                    owned = [i for i, e in enumerate(eds) if e["owned"]]
                    part = [i for i, e in enumerate(eds) if e["partial"]]
                    if owned:
                        rep_i = owned[0]
                        if g.get("disk_year"):
                            for i in owned:
                                if str(eds[i]["year"]) == str(g["disk_year"]):
                                    rep_i = i
                                    break
                    elif part:
                        rep_i = part[0]
                    if rep_i:
                        eds.insert(0, eds.pop(rep_i))
                    rep = eds[0]
                    for f in ("id", "title", "artist", "artist_id", "year", "tracks",
                              "quality", "hires", "lossy", "bit_depth",
                              "sample_rate", "cover", "version", "queued",
                              "scanning", "replaces_existing", "ownership_unknown"):
                        g[f] = rep[f]
                    g["partial"] = rep["partial"] and not g["owned"]
                    g["have_tracks"] = rep["have_tracks"]
                    g["want_tracks"] = rep["want_tracks"]
                    g["others"] = eds[1:]
        except NoCredsError:
            error = "No Qobuz credentials set. Visit Settings."
        except AuthLost:
            error = "Token is expired or invalid. Update it in Settings."
        except QobuzUnavailable as exc:
            error = str(exc)
        except QobuzError:
            error = "Search failed. Try again."
        except Exception:
            _log.exception(
                "search failed for %r", query)
            error = "Search failed. Try again."
    creds_ok = runtime._creds_ok()
    search_state = f"{kind}|{query}"
    if artist_id:
        search_state += f"|artist:{artist_id}"
    if kind == "album" and album_id:
        search_state += f"|album:{album_id}"
    search_result_count = len(results) if results else len(artist_results)
    search_count = (len(album_groups) if kind == "album" else
                    len(artist_results) if kind == "artist" else len(results))
    search_count_label = plural(search_count, kind)
    if kind == "album":
        search_count_label += " on Qobuz"
    defer_search_views = search_result_count > _SEARCH_SNAPSHOT_RESULT_LIMIT
    ctx = {"q": query, "results": results, "album_groups": album_groups,
           "artist_results": artist_results, "selected_artist": selected_artist,
           "error": error, "kind": kind,
           "search_count_label": search_count_label,
           "artist_only_count": artist_only_count,
           "artist_only_label": plural(artist_only_count, "result"),
           "search_state": search_state,
           "search_cacheable": not defer_search_views,
           "defer_search_views": defer_search_views,
           "creds_ok": creds_ok, "qobuz_ready": runtime._qobuz_ready(), "page": "search"}
    if runtime._is_htmx(request):
        resp = runtime._tr(request, "_search_results.html", ctx)
        # Put the search in the address bar. Without it a reload, or Back after
        # a look at the Queue, landed on the empty state with the query, the
        # album list and every tick gone. GET / rehydrates from these.
        if query or (kind == "album" and album_id):
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


def _track_length(seconds):
    s = max(0, int(round(float(seconds or 0))))
    return f"{s // 60}:{s % 60:02d}"


def _album_tracklist(album_id, token):
    """One release's tracks, with the ones already on disk marked.

    Presence is the same folder resolve and pairing the search rows use, so a
    marked line and an Owned row can never disagree."""
    album = api_client.call_within(
        cfg.WEB_FETCH_TIMEOUT, qobuz_search.get_album, album_id, token)
    items = (album.get("tracks") or {}).get("items") or []
    present_ids = set()
    try:
        folder = catalog.find_album_dir_filesystem(album)
        if folder is not None and items:
            existing, _ = catalog.find_existing_tracks(album, album_dir=folder)
            if existing:
                _missing, present = catalog.compute_missing(items, existing)
                present_ids = {str(t.get("id")) for t in present if t.get("id")}
    except Exception:
        # A library that can't be read still leaves a usable tracklist; the
        # marks are the only thing lost.
        _log.exception(
            "tracklist ownership check failed for album %r", album_id)
    return [{"n": t.get("track_number") or i,
             "disc": t.get("media_number") or 1,
             "title": t.get("title") or "?",
             "length": _track_length(t.get("duration")),
             "owned": str(t.get("id")) in present_ids}
            for i, t in enumerate(items, 1)]


@router.get("/search/album-tracks", response_class=HTMLResponse)
async def search_album_tracks(request: Request, album_id: str = ""):
    """The tracks under one search row, fetched when the row is opened rather
    than shipped with every result."""
    album_id = str(album_id or "").strip()
    if not album_id or not runtime._qobuz_ready():
        return HTMLResponse("")
    loop = asyncio.get_running_loop()
    error = ""
    tracks = []
    try:
        token = runtime._get_token()
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
    return runtime._tr(request, "_search_tracklist.html", {
        "tracks": tracks,
        "multi_disc": len({t["disc"] for t in tracks}) > 1,
        "error": error,
        "page": "search"})


@router.post("/download", response_class=HTMLResponse)
async def queue_download(request: Request, album_id: str = Form(""),
                         as_new_edition: str = Form(""),
                         track_id: str = Form("")):
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    album_id = album_id.strip()
    track_id = track_id.strip()
    if not album_id:
        msg = "Missing album id."
        if runtime._is_htmx(request):
            # 200, not 400: htmx only swaps 2xx/3xx responses, so a 400
            # fragment is silently dropped and the user sees no feedback.
            return _download_fragment("error", html.escape(msg), "failed")
        return RedirectResponse(url="/queue?error=" + runtime._notice_key(msg),
                                status_code=303)
    # "Get this edition too": download a different edition of an album the
    # user already owns, as a separate album.
    download_as_new_edition = str(as_new_edition).strip().lower() in (
        "1", "true", "yes", "on")
    # Refuse true duplicates (same album already active or pending), but only
    # of the SAME intent (see _duplicate_download_job).
    existing = runtime._duplicate_download_job(album_id, track_id, download_as_new_edition)
    if existing:
        if runtime._is_htmx(request):
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
        msg = runtime._music_write_target_message(root_state, recorded_albums)
        if runtime._is_htmx(request):
            return _download_fragment("error", html.escape(msg), "failed")
        return RedirectResponse(
            url="/queue?error=" + runtime._notice_key(msg), status_code=303)
    try:
        credentials = await runtime._authorize_qobuz_for_web(
            QobuzAccess.DOWNLOAD_ACTION
        )
        token = credentials.token
        album = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: api_client.call_within(cfg.WEB_FETCH_TIMEOUT, qobuz_search.get_album, album_id, token)),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        if download_as_new_edition and await loop.run_in_executor(
            None, lambda: runtime._same_edition_is_complete(album)
        ):
            msg = "This edition is already in your library."
            if runtime._is_htmx(request):
                return _download_fragment(
                    "warning", html.escape(msg), "owned")
            return RedirectResponse(
                url="/queue?error=" + runtime._notice_key(msg), status_code=303)
        if not download_as_new_edition and track_id:
            # A single track was excluded from the guard entirely, so nothing
            # checked whether that track was already on disk before fetching it
            # again. Ask about the one track, not the whole album.
            def _track_already_there():
                try:
                    album_dir = catalog.find_album_dir_filesystem(album)
                except Exception:
                    _log.exception(
                        "track ownership folder lookup failed for album %s", album_id)
                    return False
                if album_dir is None:
                    return False
                try:
                    existing_tracks, _ = catalog.find_existing_tracks(album, album_dir=album_dir)
                except Exception:
                    _log.exception(
                        "track ownership read failed for album %s", album_id)
                    return False
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                if not (existing_tracks and qobuz_tracks):
                    return False
                _missing, present = catalog.compute_missing(qobuz_tracks, existing_tracks)
                return any(str(t.get("id") or "") == track_id for t in present)

            if await loop.run_in_executor(None, _track_already_there):
                msg = "That track is already in your library."
                if runtime._is_htmx(request):
                    return _download_fragment(
                        "warning", html.escape(msg), "owned")
                return RedirectResponse(
                    url="/queue?error=" + runtime._notice_key(msg), status_code=303)

        if not download_as_new_edition and not track_id:
            def _already_complete():
                try:
                    album_dir = catalog.find_album_dir_filesystem(album)
                except Exception:
                    _log.exception(
                        "album ownership folder lookup failed for album %s", album_id)
                    return False
                if album_dir is None:
                    return False
                try:
                    # Already resolved above; pass it through so we don't repeat
                    # the cached-subdir scan + fuzzy fallback for the same album.
                    existing_tracks, _ = catalog.find_existing_tracks(album, album_dir=album_dir)
                except Exception:
                    _log.exception(
                        "album ownership read failed for album %s", album_id)
                    existing_tracks = []
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                return bool(existing_tracks and runtime._album_tracks_complete(album)) and not (
                    catalog.compute_missing(qobuz_tracks, existing_tracks)[0])

            # Resolving the album folder walks the (often NAS-mounted) library,
            # so keep it off the event loop; otherwise a large library stalls
            # every other request while this one request blocks.
            if await loop.run_in_executor(None, _already_complete):
                msg = "This album is already in your library."
                if runtime._is_htmx(request):
                    # Offer the deliberate second-edition path instead of a
                    # dead end: a remaster or a different mix can be kept
                    # alongside the owned copy, under its edition's name.
                    aid = html.escape(album_id)
                    return HTMLResponse(
                        f'<div class="ql-download-choice">'
                        f'<div class="ql-download-choice-copy">'
                        f'<p>{html.escape(msg)}</p>'
                        f'<span>A remaster or different mix downloads into a '
                        f'folder of its own, named after its edition, beside '
                        f'the existing library copy.</span></div>'
                        f'<form hx-post="/download" hx-target="#download-toast" '
                        f'hx-swap="innerHTML">'
                        f'<input type="hidden" name="album_id" value="{aid}">'
                        f'<input type="hidden" name="as_new_edition" value="1">'
                        f'<button type="submit" class="ql-btn ql-btn-primary ql-btn-sm '
                        f'w-full sm:w-auto whitespace-nowrap">'
                        f'Download this edition anyway</button></form></div>',
                        headers={"X-QL-Download-Outcome": "owned"},
                    )
                return RedirectResponse(
                    url="/queue?error=" + runtime._notice_key(msg),
                    status_code=303)
        title  = album.get("title") or "?"
        artist = (album.get("artist") or {}).get("name") or "?"
        single_track = None
        if track_id:
            _tracks = (album.get("tracks") or {}).get("items") or []
            single_track = next(
                (t for t in _tracks if str(t.get("id")) == track_id), None)
            if single_track is None:
                msg = "That track isn't on this album."
                if runtime._is_htmx(request):
                    # 200, not 400: htmx drops non-2xx/3xx fragments, so a 400
                    # here renders nothing. The notice conveys the failure.
                    return _download_fragment(
                        "error", html.escape(msg), "failed")
                return RedirectResponse(
                    url="/queue?error=" + runtime._notice_key(msg), status_code=303)
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
        with runtime._DOWNLOAD_SUBMIT_LOCK, runtime._CREDENTIAL_LOCK:
            dup = runtime._duplicate_download_job(album_id, track_id, download_as_new_edition)
            if dup:
                if runtime._is_htmx(request):
                    return _download_fragment(
                        "warning",
                        f'Already queued. <a href="/jobs/{dup.id}" '
                        f'class="ql-inline-link">View job</a>.',
                        "duplicate",
                    )
                return RedirectResponse(url=f"/jobs/{dup.id}", status_code=303)
            busy = runtime._lock_busy_response(request)
            if busy is not None:
                return busy
            if not runtime._credential_generation_is_active(credentials.generation):
                raise CredentialChanged(
                    "Qobuz credentials changed before the download was queued."
                )
            run_fn = (runtime._make_single_track_run(album, single_track, token)
                      if single_track
                      else runtime._make_download_run(
                          album, token, treat_as_new=download_as_new_edition))
            if job_mgr.submit(job, run_fn) is None:
                return runtime._job_admission_response(request)
        if runtime._is_htmx(request):
            response = runtime._tr(request, "_job_queued.html", {"job": job})
            response.headers["X-QL-Download-Outcome"] = "queued"
            return response
        # Land on the new job's page so the user sees their download starting.
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    except NoCredsError as exc:
        msg = job_mgr.qobuz_action_error_message(exc, unchanged=True)
        if runtime._is_htmx(request):
            return _download_fragment("error", html.escape(msg), "failed")
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    except Exception as e:
        _log.warning("couldn't queue download for album %s", album_id,
                     exc_info=True)
        user_msg = runtime._download_error_message(
            e,
            "Couldn't queue download. Try again.",
        )
        if runtime._is_htmx(request):
            return _download_fragment(
                "error", html.escape(user_msg), "failed")
        msg = runtime._notice_key(user_msg)
        return RedirectResponse(url=f"/queue?error={msg}", status_code=303)
