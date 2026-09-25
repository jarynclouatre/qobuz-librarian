"""The Dismissed lists and their Bring back actions."""
import asyncio
import urllib.parse
from datetime import datetime

from fastapi.responses import RedirectResponse

from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import library_scan_state
from qobuz_librarian.web import flows, review_pages, runtime


def _hidden_filter(request):
    return (request.query_params.get("q") or "").strip()[:200]


def _hidden_matching(groups, q):
    """The Dismissed page's filter applied to its artist groups.

    The page and every Bring back button on it go through here, so what the
    filter shows is exactly what those buttons take.
    """
    needle = (q or "").strip().lower()
    if not needle:
        return groups
    matched = []
    for g in groups:
        if needle in g["artist"].lower():
            matched.append(g)
            continue
        albums = [a for a in g["albums"]
                  if needle in a["title"].lower()
                  or any(needle in o["title"].lower() for o in a["others"])]
        if albums:
            matched.append({
                "artist": g["artist"], "albums": albums,
                "rows": sum(1 + len(a["others"]) for a in albums)})
    return matched


def _hidden_view(request, scope, *, page, restore_action, back_url,
                 restore_all_action=None):
    q = _hidden_filter(request)
    groups = _hidden_matching(hidden_mod.hidden_by_artist(scope), q)
    # Review rows, not fingerprints. One fingerprint can hold several editions
    # of an album, and every page that counts dismissals elsewhere (the review
    # link, the Library, Upgrade and Downsample cards) counts rows.
    # Counted after the filter, because the button below acts on what is shown.
    total_rows = sum(g["rows"] for g in groups)

    try:
        pg = int(request.query_params.get("p") or 1)
    except ValueError:
        pg = 1
    # Whole artists per page, same budgets as the review pages.
    page_groups, pg, n_pages = review_pages._paginate_groups(
        groups, pg, rows=lambda g: g["rows"])
    for g in page_groups:
        for a in g["albums"]:
            a["when"], a["when_exact"] = runtime._when_label(_hidden_ts_epoch(a.get("ts")))

    return runtime._tr(request, "hidden.html", {
        "page": page, "scope": scope, "back_url": back_url,
        "restore_action": restore_action,
        "restore_all_action": restore_all_action,
        "restore_all_count": total_rows,
        "notice": runtime._notice_text(request.query_params.get("notice")),
        "hidden_q": q,
        "hidden_total_artists": len(groups),
        "hidden_page": pg, "hidden_pages": n_pages,
        "groups": page_groups})


async def _restore_hidden_filter(request, scope):
    """The filter a Bring back button was pressed under, and the fingerprints
    it covers. Empty filter means the whole scope, and None fingerprints say
    so, which lets the caller take the cheaper clear-the-bucket path."""
    form = await request.form()
    q = (form.get("q") or "").strip()[:200]
    if not q:
        return "", None
    groups = _hidden_matching(hidden_mod.hidden_by_artist(scope), q)
    return q, [a["fp"] for g in groups for a in g["albums"]]


async def _restore_hidden_all(request, scope, dest, what, what_plural):
    """Bring back for the Dismissed pages, scoped to the page's filter. The
    library scope has its own richer endpoint (it also lifts a retired
    review); this covers the Upgrade and Downsample scopes, whose reviews
    re-derive from saved state at read time."""
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    q, fingerprints = await _restore_hidden_filter(request, scope)
    loop = asyncio.get_running_loop()
    try:
        if fingerprints is None:
            changed = await loop.run_in_executor(
                None, lambda: hidden_mod.restore_all(scope))
        else:
            changed = await loop.run_in_executor(
                None, lambda: hidden_mod.restore_albums(scope, fingerprints))
    except OSError as e:
        return RedirectResponse(
            url=dest + _hidden_query(q, str(e)), status_code=303)
    if not changed:
        msg = "Nothing to bring back."
    elif q:
        msg = f"Brought back {changed} {what if changed == 1 else what_plural}."
    else:
        msg = f"Brought every {what} back."
    return RedirectResponse(url=dest + _hidden_query(q, msg), status_code=303)


def _hidden_query(q, notice, p=None):
    """Back to the Dismissed page with its filter and page still on, so a
    restore does not silently widen the list the next click acts on or drop
    the user back at page 1."""
    parts = "?notice=" + runtime._notice_key(notice)
    if q:
        parts += "&q=" + urllib.parse.quote(q)
    if p:
        parts += "&p=" + urllib.parse.quote(p)
    return parts


async def _restore_hidden(request, scope, redirect):
    # Mutates the hidden store, so it honours the run-lock like every other
    # state-changing POST: a restore mustn't race a CLI run or another job.
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    form = await request.form()
    artists = form.getlist("artist")[:10000]
    fingerprints = form.getlist("fingerprint")[:10000]
    q = (form.get("q") or "").strip()[:200]
    p = (form.get("p") or "").strip()[:6]
    p = p if p.isdigit() else None
    restored = 0
    try:
        if artists:
            restored += hidden_mod.restore(scope, artists)
        if fingerprints:
            restored += hidden_mod.restore_albums(scope, fingerprints)
    except OSError as e:
        # Store write failed; nothing was restored; say so instead of
        # rendering the rows gone until the next reload.
        return RedirectResponse(
            url=redirect + _hidden_query(q, str(e), p), status_code=303)
    if scope == hidden_mod.SCOPE_MISSING and (artists or fingerprints):
        # Upgrade/Downsample re-derive their reviews from saved state at read
        # time, so restore takes effect there on its own.
        loop = asyncio.get_running_loop()
        rejoined = await loop.run_in_executor(
            None, lambda: flows.refold_restored_missing(artists, fingerprints))
        if rejoined is False:
            msg = ("Brought back, but the open Library review couldn't be "
                   "saved. Check the data folder, then refresh the review.")
        elif rejoined is None:
            # No live parked review to fold into.
            lifted = await loop.run_in_executor(
                None, library_scan_state.clear_review_retired)
            msg = ("Brought back to the Library review." if lifted
                   else "Brought back. They return the next time the library scans.")
        elif rejoined:
            msg = (f"Brought back {rejoined} to the Library review."
                   if rejoined != 1 else "Brought back to the Library review.")
        else:
            msg = "Brought back. Nothing needs adding to the Library review."
        return RedirectResponse(
            url=redirect + _hidden_query(q, msg, p), status_code=303)
    # Upgrade and Downsample scopes: nothing to fold.
    msg = (f"Brought back {restored}." if restored != 1 else "Brought back."
           ) if restored else "Nothing to bring back."
    return RedirectResponse(
        url=redirect + _hidden_query(q, msg, p), status_code=303)


def _hidden_ts_epoch(ts: str):
    """Parse the hidden store's ISO timestamp to a float epoch, or None for
    an old entry written before the store kept one."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None
