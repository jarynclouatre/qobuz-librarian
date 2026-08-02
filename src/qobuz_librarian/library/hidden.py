"""Albums the user dismissed from the bulk library/upgrade walks.

The walks resurface every gap on every run. With a large library triaged over
weeks, that means re-reviewing the same albums you've already decided you don't
want, forever. This module is the memory that stops it: a dismissed album is
recorded by a loose artist+title fingerprint and filtered out of the bulk walks
until the user restores it.

Keyed on a fingerprint, not the Qobuz album id: dedup can resolve to a
different edition (a new id) of the same album on a later scan, and an id key
would let that edition slip back onto the list. The fingerprint keeps every
edition of one album dismissed together. The year is kept for the Hidden view
but is deliberately not part of the match key — a remaster carries a different
year, and a missing year (common from Qobuz) would otherwise mis-key.

Hides are scoped so the two walks don't cross-contaminate: a "missing" hide
(don't offer to download this album I don't own) is independent of an "upgrade"
hide (don't offer to re-rip this album I own at higher quality). Restoring one
scope never touches the other.

Explicit single-artist scans do NOT consult this store — typing a name is a
conscious request to see everything by that artist. Only the bulk walks filter
on it.
"""
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library.tags import normalize, strip_album_decorations_strict

SCOPE_MISSING = "missing"
SCOPE_UPGRADE = "upgrade"
SCOPE_DOWNSAMPLE = "downsample"
# A deliberately downloaded single track: the album folder is left partial on
# purpose.
SCOPE_SINGLE = "single"
_SCOPES = (SCOPE_MISSING, SCOPE_UPGRADE, SCOPE_DOWNSAMPLE, SCOPE_SINGLE)

# Serialises the load-modify-save in every mutator.
_LOCK = threading.RLock()


@contextmanager
def _store_lock():
    """Serialise the hidden-store load-modify-save across BOTH threads and OS
    processes. The web hide/restore routes deliberately run without the global
    run-lock (so a review row can still be dismissed while a scan holds it, or
    after the web app hands the run-lock to the terminal).
    """
    with _LOCK, state_file.store_lock(cfg.HIDDEN_FILE):
        yield


def album_fingerprint(artist, title):
    """Loose key tying every edition of one album together.

    Returns None when artist or title normalize to nothing (pure-CJK / emoji
    titles) — the caller treats that as 'can't fingerprint', so such an album
    is never matched against the store and stays visible rather than being
    wrongly hidden.
    """
    a = normalize(artist or "")
    # The strict strip: the loose one collapsed distinct albums into one key
    # ('Alone' with 'Alone (Again)'), so dismissing one buried the other and
    # a kept album could vanish under a dismissed sibling's fingerprint.
    t = normalize(strip_album_decorations_strict(title or ""))
    if not a or not t:
        return None
    return f"{a}|{t}"


def _entry_rows(entry):
    """The review rows recorded under one fingerprint.

    One key can cover several rows: the fingerprint deliberately ties every
    edition of an album together, and Qobuz lists editions as separate
    releases. Entries written before rows were kept carry only the first one at
    the top level, so derive a single row from those — the counts on screen
    have to mean the same thing for an old store as a new one.
    """
    rows = entry.get("rows")
    if isinstance(rows, list) and rows:
        kept = [r for r in rows if isinstance(r, dict)]
        # A rows list holding no dicts (hand edit, partial old write) must
        # degrade to the top-level fallback like a missing list, not return
        # [] and crash every page that indexes rows[0].
        if kept:
            return kept
    return [{"title": entry.get("title") or "", "year": entry.get("year") or "",
             "ts": entry.get("ts") or ""}]


def load():
    """Return the whole store as {scope: {fingerprint: entry}}, tolerating a
    missing file by returning empty scopes. A corrupt file is moved aside
    (….corrupt) with a warning rather than silently overwritten by the next
    save() — which would destroy the dismissed-album list with no trace."""
    base = {s: {} for s in _SCOPES}
    data = state_file.load_json_object(
        cfg.HIDDEN_FILE, "the hidden-albums store",
        "your list of dismissed albums")
    if data is None:
        return base
    for scope in _SCOPES:
        bucket = data.get(scope)
        if isinstance(bucket, dict):
            base[scope] = _rekeyed(
                {k: v for k, v in bucket.items() if isinstance(v, dict)})
    return base


def _rekeyed(bucket):
    """Entries under the fingerprint the current key function produces.

    A store written before the strict title key computed its keys with the
    loose one, which merged distinct albums — one entry can therefore cover
    rows that now belong under several keys. Rebuild each entry's key(s) from
    the full titles its rows kept; entries whose rows don't recompute (no
    titles recorded, artist lost) stay under their stored key. In-memory
    only — the next save persists the re-keyed form."""
    out = {}
    for old_key, entry in bucket.items():
        rows = _entry_rows(entry)
        artist = entry.get("artist") or ""
        split = {}
        for row in rows:
            fp = album_fingerprint(artist, row.get("title") or "")
            if fp is None:
                split = None
                break
            split.setdefault(fp, []).append(row)
        if not split:
            out[old_key] = entry
            continue
        for fp, fp_rows in split.items():
            if fp in out:
                have = {(r.get("title"), r.get("year"))
                        for r in out[fp].get("rows") or []}
                out[fp]["rows"] = (out[fp].get("rows") or []) + [
                    r for r in fp_rows if (r.get("title"), r.get("year"))
                    not in have]
                continue
            head = dict(entry)
            head["title"] = fp_rows[0].get("title") or entry.get("title") or ""
            head["year"] = fp_rows[0].get("year") or ""
            head["rows"] = fp_rows
            out[fp] = head
    return out


_SAVE_FAILED_MSG = ("Couldn't save that — the data folder looks full or "
                    "read-only, so it would have come back after a restart.")


def save(store):
    # Callers hold _store_lock() across load()+save(), so this writer never
    # races a second writer of the same store.
    try:
        state_file.write_json(cfg.HIDDEN_FILE, store, ensure_ascii=False)
        return True
    except OSError as e:
        from qobuz_librarian.ui_cli.logging import vlog
        vlog(f"hidden-store write failed ({e}); the change may not persist")
        return False


def is_hidden(scope, artist, title, store):
    """True when (artist, title) is dismissed in this scope. `store` is a
    preloaded dict from load() so a scan doesn't re-read the file per album."""
    fp = album_fingerprint(artist, title)
    if fp is None:
        return False
    return fp in (store.get(scope) or {})


def hide(scope, items):
    """Record dismissals. `items` is an iterable of (artist, title, year).

    Returns the number of ROWS newly recorded, which is what left the user's
    review — not the number of fingerprints, which is smaller whenever two
    editions of one album were both on the list. The two used to be reported
    side by side as if they were the same number.
    """
    with _store_lock():
        store = load()
        bucket = store.setdefault(scope, {})
        now = datetime.now(timezone.utc).isoformat()
        added = 0
        for artist, title, year in items:
            fp = album_fingerprint(artist, title)
            if fp is None:
                continue
            row = {"title": title or "", "year": str(year or ""), "ts": now}
            entry = bucket.get(fp)
            if entry is None:
                bucket[fp] = {"artist": artist or "", "title": title or "",
                              "year": str(year or ""), "ts": now, "rows": [row]}
                added += 1
                continue
            rows = _entry_rows(entry)
            if any(r.get("title") == row["title"] and r.get("year") == row["year"]
                   for r in rows):
                continue
            entry["rows"] = rows + [row]
            added += 1
        if added and not save(store):
            raise OSError(_SAVE_FAILED_MSG)
    return added


def restore(scope, artists):
    """Un-hide every album whose stored artist matches one in `artists`. Returns
    the count removed.

    Matched on the normalized artist, like the rest of the store keys, so a
    casing or spacing difference between the posted value and the stored string
    still restores it (and every casing variant of one artist clears together)
    rather than stranding an album as un-restorable."""
    with _store_lock():
        store = load()
        bucket = store.get(scope) or {}
        targets = {normalize(a) for a in artists if normalize(a)}
        drop = [fp for fp, e in bucket.items()
                if normalize(e.get("artist") or "") in targets]
        rows = sum(len(_entry_rows(bucket[fp])) for fp in drop)
        for fp in drop:
            bucket.pop(fp, None)
        if drop:
            store[scope] = bucket
            if not save(store):
                raise OSError(_SAVE_FAILED_MSG)
    return rows


def restore_all(scope):
    """Un-hide every album dismissed in a scope at once (the Library page's
    'Bring all back'). Returns the count removed."""
    return _restore_all(scope)[0]


def take_all(scope):
    """Clear a scope and return the artist names it held, in one lock hold.
    The Dismissed page's Bring-all-back needs the names for the refold; read
    outside the lock, a dismissal landing in between would be cleared but
    never refolded — gone from both lists."""
    return _restore_all(scope)[1]


def _restore_all(scope):
    with _store_lock():
        store = load()
        bucket = store.get(scope) or {}
        n = sum(len(_entry_rows(e)) for e in bucket.values())
        artists = sorted({(e.get("artist") or "Unknown artist")
                          for e in bucket.values()})
        if bucket:
            store[scope] = {}
            if not save(store):
                raise OSError(_SAVE_FAILED_MSG)
    return n, artists


def restore_albums(scope, fingerprints):
    """Un-hide specific albums by fingerprint — the same key is_hidden looks up.

    Unknown fingerprints are silently skipped: the Hidden page can render
    against a slightly stale store (another tab just restored the same row, or
    the bucket changed mid-request) and a hard 404 there would be hostile."""
    with _store_lock():
        store = load()
        bucket = store.get(scope) or {}
        drop = [fp for fp in fingerprints if fp in bucket]
        rows = sum(len(_entry_rows(bucket[fp])) for fp in drop)
        for fp in drop:
            bucket.pop(fp, None)
        if drop:
            store[scope] = bucket
            if not save(store):
                raise OSError(_SAVE_FAILED_MSG)
    return rows


def hidden_by_artist(scope, store=None):
    """[{artist, rows, albums: [{title, year, ts, fp, others}]}] for the Hidden view.

    One entry per fingerprint, because that is the unit Restore can act on —
    every edition under a key was dismissed together and comes back together.
    ``others`` lists the further titles the key covers so the page can say so
    instead of appearing to have lost them, and ``rows`` is how many review rows
    the artist's entries account for.
    """
    bucket = (store if store is not None else load()).get(scope) or {}
    groups = {}
    for fp, e in bucket.items():
        artist = e.get("artist") or "Unknown artist"
        rows = _entry_rows(e)
        first = rows[0]
        groups.setdefault(artist, []).append({
            "title": first.get("title") or e.get("title") or "?",
            "year": first.get("year") or "",
            "ts": first.get("ts") or e.get("ts") or "",
            "fp": fp,
            "others": [r.get("title") or "?" for r in rows[1:]],
        })
    out = []
    for artist in sorted(groups, key=str.lower):
        albums = sorted(groups[artist], key=lambda a: (a["title"].lower(), a["year"]))
        out.append({
            "artist": artist,
            "albums": albums,
            "rows": sum(1 + len(a["others"]) for a in albums),
        })
    return out


def count(scope, store=None):
    """Review rows dismissed in this scope — the same unit the review counts."""
    bucket = (store if store is not None else load()).get(scope) or {}
    return sum(len(_entry_rows(e)) for e in bucket.values())


# ── Singles — a deliberately downloaded track, not a gap ───────────────────────

def is_single(artist, title, store):
    """True when (artist, title) is marked as a deliberately downloaded single, so
    its partial folder isn't read as a gap. `store` is preloaded by load()."""
    fp = album_fingerprint(artist, title)
    if fp is None:
        return False
    return fp in (store.get(SCOPE_SINGLE) or {})


def mark_single(artist, title, year, album_id):
    """Record that the user downloaded a single from this album. Idempotent; keeps
    the original timestamp on a repeat. Returns True if a mark now exists."""
    fp = album_fingerprint(artist, title)
    if fp is None:
        return False
    with _store_lock():
        store = load()
        bucket = store.setdefault(SCOPE_SINGLE, {})
        prev = bucket.get(fp) or {}
        bucket[fp] = {
            "artist": artist or "", "title": title or "", "year": str(year or ""),
            "album_id": str(album_id or "") or prev.get("album_id", ""),
            "ts": prev.get("ts") or datetime.now(timezone.utc).isoformat(),
        }
        if not save(store):
            raise OSError(_SAVE_FAILED_MSG)
    return True


def unmark_single(artist, title):
    """Drop the single mark — graduation (the folder is now complete) or the user
    chose to complete the album. Returns True if a mark was removed."""
    fp = album_fingerprint(artist, title)
    if fp is None:
        return False
    with _store_lock():
        store = load()
        bucket = store.get(SCOPE_SINGLE) or {}
        if fp not in bucket:
            return False
        bucket.pop(fp, None)
        store[SCOPE_SINGLE] = bucket
        # Best-effort on purpose: unmark runs as internal bookkeeping inside
        # undo/repair/graduation flows — a failed cleanup write shouldn't
        # abort those; save() already leaves a verbose trail.
        save(store)
    return True
