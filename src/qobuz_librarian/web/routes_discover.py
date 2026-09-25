"""Routes for the Discover page."""
import asyncio
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    NoCredsError,
    QobuzError,
    QobuzUnavailable,
)
from qobuz_librarian.library import recommendations
from qobuz_librarian.quality.tiers import format_quality
from qobuz_librarian.web import runtime

router = APIRouter()


_DISCOVER_TABS = (
    ("similar", "/discover", "Similar"),
    ("genres", "/discover/genres", "Genres"),
    ("search", "/discover/search", "Search"),
    ("favourites", "/discover/favourites", "Favourites"),
)


def _discover_unavailable_response():
    return RedirectResponse(url="/", status_code=303)


def _discover_album_views(rows, queued=None, scanning=None):
    """Add the quality labels and the queued/in-scan states a card shows, using
    the same helpers the search results use so the two never disagree about
    what counts as hi-res."""
    if queued is None or scanning is None:
        queued, _tracks, scanning = runtime._active_search_downloads()
    out = []
    for row in rows or []:
        bits, rate = runtime._qobuz_quality_bits_rate(row)
        album_id = str(row.get("id") or "")
        out.append(dict(
            row,
            quality=format_quality(bits, rate) if bits and rate else "",
            hires=bits >= 24,
            lossy=bits == 0,
            queued=album_id in queued,
            scanning=album_id in scanning,
        ))
    return out


def _discover_decades(albums):
    """Chips for the decades actually present, newest first. Fewer than two
    real decades and the row is not offered: a filter with one setting is a
    control that does nothing."""
    years = set()
    for album in albums:
        try:
            years.add(int(str(album.get("year") or "")[:4]))
        except ValueError:
            continue
    decades = sorted({(year // 10) * 10 for year in years}, reverse=True)
    return [("", "All")] + [(str(d), f"{d}s") for d in decades]


def _discover_empty_feed():
    return {"phase": "idle", "checked": 0, "total": 0, "error": "",
            "items": [], "built_at": 0.0, "stale": False}


async def _discover_render(request: Request, tab: str, *, tag: str = "",
                           query: str = ""):
    if not runtime._discover_available():
        return _discover_unavailable_response()
    context = {
        "page": "discover",
        "discover_tab": tab,
        "discover_tabs": _DISCOVER_TABS,
        "creds_ok": bool(runtime._read_creds().get("auth_token")),
        "qobuz_ready": runtime._qobuz_ready(),
        "feed": _discover_empty_feed(),
        "artists": [],
        "albums": [],
        "tags": [],
        "tag": "",
        "query": query,
        "poll_url": "/discover",
        "feed_age": "",
        "library_count": 0,
    }
    if not context["qobuz_ready"]:
        return runtime._tr(request, "discover.html", context)

    try:
        token = runtime._get_token()
    except (SystemExit, NoCredsError):
        context["creds_ok"] = False
        context["qobuz_ready"] = False
        return runtime._tr(request, "discover.html", context)
    loop = asyncio.get_running_loop()
    if tab == "genres":
        tags_view = await loop.run_in_executor(
            None, lambda: recommendations.ensure_library_tags(token))
        chips = [c for c in tags_view["items"] if isinstance(c, str)]
        chosen = str(tag or "").strip() or (chips[0] if chips else "")
        context["tags"] = chips
        context["tag"] = chosen
        if chosen:
            feed = await loop.run_in_executor(
                None, lambda: recommendations.ensure_genre_feed(token, chosen))
            context["albums"] = _discover_album_views(feed["items"])
            context["poll_url"] = (
                "/discover/genres?tag=" + urllib.parse.quote(chosen))
        else:
            feed = tags_view
            context["poll_url"] = "/discover/genres"
        context["feed"] = feed
    elif tab == "search":
        feed = await loop.run_in_executor(
            None, lambda: recommendations.ensure_search_feed(token, query))
        context["feed"] = feed
        context["artists"] = feed["items"]
        context["poll_url"] = (
            "/discover/search?q=" + urllib.parse.quote(query))
    elif tab == "favourites":
        feed = await loop.run_in_executor(
            None, lambda: recommendations.ensure_favourites_feed(token))
        context["feed"] = feed
        context["albums"] = _discover_album_views(feed["items"])
        context["poll_url"] = "/discover/favourites"
    else:
        feed = await loop.run_in_executor(
            None, lambda: recommendations.ensure_similar_feed(token))
        context["feed"] = feed
        context["artists"] = feed["items"]
        context["library_count"] = len(recommendations.library())
        context["poll_url"] = "/discover"
    if context["feed"]["built_at"]:
        context["feed_age"] = runtime._format_age(context["feed"]["built_at"])
    return runtime._tr(request, "discover.html", context)


@router.get("/discover", response_class=HTMLResponse)
async def discover_page(request: Request):
    return await _discover_render(request, "similar")


@router.get("/discover/genres", response_class=HTMLResponse)
async def discover_genres(request: Request, tag: str = ""):
    return await _discover_render(request, "genres", tag=tag[:120])


@router.get("/discover/search", response_class=HTMLResponse)
async def discover_search(request: Request, q: str = ""):
    return await _discover_render(request, "search", query=q.strip()[:200])


@router.get("/discover/favourites", response_class=HTMLResponse)
async def discover_favourites(request: Request):
    return await _discover_render(request, "favourites")


@router.get("/discover/artist-albums", response_class=HTMLResponse)
async def discover_artist_albums(request: Request, artist_id: str = "",
                                 name: str = ""):
    """The albums under one suggestion, fetched when the row is opened rather
    than shipped with every card on the page."""
    if not runtime._discover_available():
        return _discover_unavailable_response()
    if not artist_id or not runtime._qobuz_ready():
        return HTMLResponse("")
    loop = asyncio.get_running_loop()
    try:
        token = runtime._get_token()
    except (SystemExit, NoCredsError):
        return runtime._tr(request, "_discover_albums.html", {
            "albums": [],
            "decades": [],
            "error": (
                "The Qobuz connection changed. Reload Discover and reconnect "
                "in Settings."
            ),
            "page": "discover",
        })
    # A call that failed and an artist with nothing to offer both end up with
    # no rows, so the reason is carried through: an empty catalogue must never
    # stand in for a request that never got an answer.
    error = ""
    rows = []
    try:
        rows = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: recommendations.artist_albums(
                    artist_id, name, token)),
            timeout=cfg.WEB_FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        error = "Timed out reaching the Qobuz API."
    except AuthLost:
        error = "Token is expired or invalid. Update it in Settings."
    except QobuzUnavailable as exc:
        error = str(exc)
    except QobuzError:
        error = "Qobuz could not list this artist. Try again."
    albums = _discover_album_views(rows)
    return runtime._tr(request, "_discover_albums.html", {
        "albums": albums,
        "decades": _discover_decades(albums),
        "error": error,
        "page": "discover",
    })
