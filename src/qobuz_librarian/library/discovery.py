"""The one engine behind "given an artist, what am I missing?".

Both faces call this: the CLI artist mode wraps it in terminal prompts, the web
scans turn its result into review candidates. Neither computes matching logic of
its own any more, so a fix lands once and the two can't drift.

The answer is built in two passes, because the right question differs for albums
you own versus albums you don't:

  Pass A - disk walk. For every folder you own under the artist, match it to the
    Qobuz edition that folder actually is and compare track-for-track. This is
    what catches gaps accurately, including in deluxe/anniversary editions and in
    singles the catalog-level filters drop.
  Pass B - catalog walk. Everything in the artist's catalog that Pass A didn't
    already account for, isn't owned under another folder, and isn't dismissed,
    surfaces as a fully-missing album.

`resolve_artist` lives here too: it's the shared artist matcher (article-aware,
prefers the deepest catalog over a bare-name twin, cached to disk).
"""
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
from qobuz_librarian.api.search import get_album, get_artist_albums, search_artists
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import tags
from qobuz_librarian.library.catalog import (
    _dir_year,
    _paths_equal,
    album_released_within,
    album_year,
    compute_missing,
    dedup_album_versions,
    filter_compilation_albums,
    filter_owned_albums,
    filter_short_releases,
    find_album_dir_filesystem,
    find_existing_tracks,
    find_qobuz_album_for_dir,
    is_lossless_album,
)
from qobuz_librarian.library.scanner import (
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import (
    normalize,
    similarity,
    strip_leading_article,
)
from qobuz_librarian.ui_cli.logging import log, vlog

# ── Artist resolution (the shared matcher) ──────────────────────────────────────

# Folder name → matched Qobuz artist, cached to disk.
_RESOLVE_CACHE_VERSION = 1
_resolve_cache = None
_resolve_cache_dirty = False
_resolve_cache_lock = threading.Lock()


class ArtistSearchUnavailable(Exception):
    """Artist search did not produce a trustworthy answer.

    This is deliberately distinct from a clean no-match: bulk scans must leave
    the artist incomplete and retry it instead of saving an empty result.
    """


def _load_resolve_cache() -> dict:
    global _resolve_cache
    # Double-checked lock: the artist scan calls this from a ThreadPoolExecutor,
    # so an unlocked lazy-init would let two workers both set {} and one's loaded
    # entries get dropped.
    if _resolve_cache is None:
        with _resolve_cache_lock:
            if _resolve_cache is None:
                loaded = {}
                try:
                    raw = json.loads(cfg.ARTIST_RESOLVE_CACHE_FILE
                                     .read_text(encoding="utf-8"))
                    if raw.get("version") == _RESOLVE_CACHE_VERSION:
                        loaded = raw.get("entries") or {}
                except (OSError, ValueError):
                    pass
                _resolve_cache = loaded
    return _resolve_cache


def cached_artist_resolutions() -> dict:
    """The folder-name to [id, name] map past scans resolved, as a plain copy.

    Discover reads it to learn what Qobuz calls each artist on the shelf, which
    is usually how Last.fm spells them too.
    """
    return dict(_load_resolve_cache())


def flush_resolve_cache():
    """Persist the resolution cache to disk, only if it gained entries."""
    global _resolve_cache_dirty
    if not _resolve_cache_dirty or _resolve_cache is None:
        return
    path = cfg.ARTIST_RESOLVE_CACHE_FILE
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": _RESOLVE_CACHE_VERSION,
                              "entries": _resolve_cache})
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".arcache.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
        tmp = None
        _resolve_cache_dirty = False
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def pick_best_artist(results, query):
    """The artist search result that is really `query`, or None.

    Qobuz lists the canonical artist ('The Beatles') next to bare-name twins
    ('Beatles') that aggregate covers, interviews and bootlegs. A raw string
    match favours the twin - it has no 'The ' to cost it similarity - so compare
    with the leading article stripped and, among equally close names, take the
    one with the deepest catalog: the real artist's, not the twin's.
    """
    q = strip_leading_article(query)

    def match_score(a):
        return similarity(strip_leading_article(a.get("name", "")), q)

    qualifying = [a for a in results if match_score(a) >= cfg.ARTIST_NAME_THRESH]
    if not qualifying:
        return None
    return max(qualifying,
               key=lambda a: (match_score(a), a.get("albums_count") or 0))


def resolve_artist(query, token):
    """Return (artist_id, artist_name) for the best Qobuz match, or (None, None).

    The matching itself is pick_best_artist; this adds the on-disk cache, so a
    re-scan skips the search call. flush_resolve_cache() persists new matches.
    """
    global _resolve_cache_dirty
    cache = _load_resolve_cache()
    hit = cache.get(query)
    if hit is not None:
        return hit[0], hit[1]
    try:
        results = search_artists(query, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception as e:
        log.info(f"  artist search failed for '{query}': {e}")
        raise ArtistSearchUnavailable(str(e)) from e
    best = pick_best_artist(results, query)
    if best is None:
        return None, None
    aid, aname = best.get("id"), best.get("name")
    # A name match with no id is a malformed/partial result, not a usable hit -
    # the callers need the id, so caching [None, name] would skip the artist on
    # every later scan. Treat it as a miss (don't cache); a later scan retries.
    if aid:
        cache[query] = [aid, aname]
        _resolve_cache_dirty = True
    return aid, aname


def resolve_artist_dir(artist_query, candidates=None):
    """Fuzzy-find an artist's directory in MUSIC_ROOT. Returns Path or None.
    Handles 'The X' / 'X' equivalence.

    Pass ``candidates`` (a list_library_artists() result) to match many artists
    against one directory listing. The search-ownership annotation resolves the
    artist of every result; left to itself this re-walks MUSIC_ROOT once per
    result, turning an O(library) listing into O(results × library) - slow
    enough on a large library to trip the caller's timeout and drop every mark."""
    if not artist_query:
        return None
    if candidates is None:
        candidates = list_library_artists()
    if not candidates:
        return None

    target = normalize(artist_query)
    if not target:
        return None

    for d in candidates:
        if normalize(d.name) == target:
            return d

    target_alt = target[3:] if target.startswith("the") else "the" + target
    for d in candidates:
        if normalize(d.name) == target_alt:
            return d

    def base(s):
        return s[4:] if s.lower().startswith("the ") else s
    best, best_score = None, 0.0
    for d in candidates:
        s = max(similarity(d.name, artist_query),
                similarity(base(d.name), base(artist_query)))
        if s > best_score:
            best, best_score = d, s
    if best and best_score >= cfg.FUZZY_DIR_THRESH:
        return best
    return None


# ── The discovery result ────────────────────────────────────────────────────────

# Markers of a live/tour/session/acoustic release, matched ONLY as a delimited
# release tag so an album whose real title merely contains one of these words
# is never dropped (e.g. "Live and Let Die", "Live Through This", "Tour de
# France", "Acoustic" the studio LP).
_LIVE_RELEASE_PATTERNS = (
    # "(Live)", "(Live at Wembley)", "[Live in Tokyo]", "(Recorded Live ...)"
    re.compile(r"[\(\[][^)\]]*\blive\b", re.IGNORECASE),
    # " - Live", " - Live at the Apollo", "– Live in ..." (en/em dash too)
    re.compile(r"[\-–]\s*live\b", re.IGNORECASE),
    # "Live at/in/from/on ..." - a place/date phrase, not "Live and ..."/"Live Through ..."
    re.compile(r"\blive\s+(?:at|in|from|on)\b", re.IGNORECASE),
    # Acoustic/unplugged/session formats, as a tagged or delimited marker.
    re.compile(r"\bunplugged\b", re.IGNORECASE),
    re.compile(r"[\(\[][^)\]]*\b(?:acoustic|session|sessions)\b", re.IGNORECASE),
    re.compile(r"[\-–]\s*(?:acoustic\s+session|live\s+session)", re.IGNORECASE),
    re.compile(r"\b(?:bbc|peel|abbey\s+road)\s+sessions?\b", re.IGNORECASE),
    re.compile(r"\blive\s+sessions?\b", re.IGNORECASE),
    # A bracketed/parenthetical "... Tour" tag, e.g. "(The Wall Tour Live)".
    re.compile(r"[\(\[][^)\]]*\btour\b", re.IGNORECASE),
)


def _is_live_release(title: str) -> bool:
    """True if the album title looks like a live/tour/session/acoustic release.
    Conservative by design: only a clearly tagged or delimited marker counts, so
    a studio album that merely has one of these words in its real name is kept.
    Used only when cfg.EXCLUDE_LIVE_ALBUMS is on; default behaviour is unchanged."""
    if not title:
        return False
    return any(p.search(title) for p in _LIVE_RELEASE_PATTERNS)


@dataclass
class DiscoveryOpts:
    prefer_hires: bool = False
    include_comps: bool = False
    include_singles: bool = False


@dataclass
class AlbumGap:
    """One album that needs attention. on_disk_dir is None for a fully-missing
    album; set (with missing/present track lists) for an owned album with gaps."""
    qobuz_album: dict
    on_disk_dir: Path | None
    missing: list = field(default_factory=list)
    present: list = field(default_factory=list)

    @property
    def fully_missing(self) -> bool:
        return self.on_disk_dir is None

    @property
    def missing_count(self) -> int:
        return len(self.missing)


@dataclass
class DiscoveryResult:
    artist_id: str | None
    artist_name: str | None
    gaps: list = field(default_factory=list)          # AlbumGap (partials + fully-missing)
    complete: list = field(default_factory=list)      # {dir, qobuz_album, existing}
    singles: list = field(default_factory=list)       # {dir, qobuz_album, present, missing} - downloaded singles, not gaps
    skipped: list = field(default_factory=list)       # {dir, reason, qobuz_title, ...}
    unmatched_dirs: list = field(default_factory=list)  # folders no Qobuz album matched
    catalog: list = field(default_factory=list)       # the fetched catalog (callers may reuse)
    # The catalog fetch was a transient SHORT page (a partial 200), NOT a
    # legitimate cap - so it's not the whole discography.
    catalog_incomplete: bool = False

    @property
    def partials(self):
        return [g for g in self.gaps if g.on_disk_dir is not None]

    @property
    def fully_missing(self):
        return [g for g in self.gaps if g.on_disk_dir is None]


@dataclass
class NewReleaseResult:
    """find_new_releases_for_artist's answer: the fully-missing albums new since
    the last check, plus this run's full lossless catalog id set (current_ids),
    which the caller stores as the next baseline."""
    artist_id: str | None
    artist_name: str | None
    new_gaps: list = field(default_factory=list)
    current_ids: list = field(default_factory=list)
    # True when the catalog fetch came back empty AND with no total - a
    # failed or empty 200, indistinguishable from a transient API hiccup.
    fetch_failed: bool = False
    # True when Qobuz has no artist under this name at all. Permanent, unlike
    # fetch_failed: a collaboration folder ("Bonobo, Joy Crookes"), a
    # compilation, an artist Qobuz doesn't carry. Checking again returns the
    # same answer, so callers must not count these as unchecked.
    unresolved: bool = False


def owned_title_key(name):
    """The key an album folder name is filed under in the owned-title map.

    Shared so anything comparing a name that never came from a folder listing -
    a title out of a collection backup, say - keys it exactly the way the
    folders were keyed.
    """
    return normalize(strip_leading_article(tags.strip_album_decorations(name or "")))


def _record_owned_title(owned_titles, album_dir):
    key = owned_title_key(album_dir.name)
    if key:
        owned_titles.setdefault(key, set()).add(_dir_year(album_dir.name))


def owned_album_titles(album_dirs):
    """{normalized bare title: {years}} for a set of album folders - the
    year-aware owned set the missing pass uses as a resolution-miss backstop.
    Shared so the CLI gap-fill and the engine seed it identically."""
    titles: dict = {}
    for ad in album_dirs:
        _record_owned_title(titles, ad)
    return titles


def _owned_by_name(owned_titles, album, artist_name=None):
    """True when the album's bare title (year-aware) already names an owned
    folder - the backstop for a resolution miss, so a duplicate isn't offered.
    ``artist_name`` lets filter_owned_albums offer distinct self-titled albums
    (a 2001 'Weezer' when only the 1994 one is owned) instead of hiding them."""
    return not filter_owned_albums([(album, 1)], owned_titles, artist_name)


def _is_hidden(hidden, artist_name, album):
    return hidden is not None and hidden_mod.is_hidden(
        hidden_mod.SCOPE_MISSING,
        artist_name,
        album.get("title"),
        hidden,
        year=album_year(album),
    )


def _is_single(single_store, artist_name, album):
    return (single_store is not None and album is not None
            and hidden_mod.is_single(
                artist_name,
                album.get("title"),
                single_store,
                year=album_year(album),
                album_id=album.get("id"),
            ))


def _collecting(single_store, artist_name, album_dirs):
    """An artist is 'collected' - surfaced by the bulk catalog walk and the
    new-release check - only when they own at least one album folder that isn't
    just a downloaded single. No single store (an explicit single-artist request)
    means show everything."""
    if single_store is None:
        return True
    # Derive per-folder, not by count: an artist is collecting if ANY owned
    # folder isn't a downloaded single (is_single normalises the folder name
    # to the mark's fingerprint).
    return any(not hidden_mod.is_single(
                   artist_name, ad.name, single_store, year=_dir_year(ad.name))
               for ad in album_dirs)


def _materialize_tracks(album, token):
    """Ensure the album dict carries its track list (get_artist_albums omits it).
    Returns (album, tracks); album may be the freshly fetched full dict."""
    tracks = (album.get("tracks") or {}).get("items") or []
    if tracks:
        return album, tracks
    try:
        full = get_album(album.get("id"), token)
    except AuthLost:
        raise
    except QobuzError:
        return album, []
    tracks = ((full or {}).get("tracks") or {}).get("items") or []
    return (full or album), tracks


@dataclass
class DirMatch:
    """How one owned folder matched the catalog. status is one of: partial,
    complete, false_match, low_overlap, predicted_path_mismatch, no_tracks,
    no_match."""
    status: str
    album_dir: Path
    qobuz_album: dict | None = None
    missing: list = field(default_factory=list)
    present: list = field(default_factory=list)
    existing: list = field(default_factory=list)


def match_album_dir(album_dir, artist_name, token, *, catalog, prefer_hires):
    """Match one owned folder to the Qobuz edition it actually is and compare
    track-for-track. The single per-folder decision both the CLI gap-fill loop
    and the engine's owned-album pass share, so the two can't drift.

    Resolution is target_dir-aligned (the matched edition resolves back to this
    folder); the redundant predicted-path recheck is kept because a folder that
    fuzz-matches a differently-located album is the classic false match.
    """
    album = find_qobuz_album_for_dir(
        album_dir, artist_name, token, prefer_hires=prefer_hires,
        catalog=catalog, target_dir=album_dir)
    if album is None:
        return DirMatch("no_match", album_dir)
    predicted = find_album_dir_filesystem(album)
    if predicted is None or not _paths_equal(predicted, album_dir):
        return DirMatch("predicted_path_mismatch", album_dir, album)
    tracks = (album.get("tracks") or {}).get("items") or []
    if not tracks:
        return DirMatch("no_tracks", album_dir, album)
    existing, _ = find_existing_tracks(album, album_dir=album_dir)
    missing, present = compute_missing(tracks, existing)
    if existing and not present:
        return DirMatch("false_match", album_dir, album, existing=existing)
    # Resolution can land a different but similarly-named album here when the
    # fuzzy match is symmetric (this album resolves back to this folder), so
    # the predicted-path recheck above agrees even though it's the wrong
    # album.
    if missing and len(present) * 2 < len(existing):
        return DirMatch("low_overlap", album_dir, album, existing=existing)
    status = "partial" if missing else "complete"
    return DirMatch(status, album_dir, album,
                    list(missing), list(present), existing)


def discover_fully_missing(artist_name, catalog, opts, *, hidden=None,
                           handled_ids=frozenset(), resolved_dirs=frozenset(),
                           owned_titles=None, token=None, quick=False,
                           single_store=None, include_hidden=False):
    """Catalog albums the owned-album pass didn't account for: fully-missing
    releases, plus a collaboration filed under another artist's folder that
    still has track gaps. Shared by the CLI missing-albums step and the web
    scan. handled_ids / resolved_dirs / owned_titles come from the owned pass so
    an album already matched to a folder isn't re-offered as missing.

    quick=True is the new-release path: only fully-missing albums are returned
    and no track lists are fetched, so the cost stays at the catalog list alone.

    include_hidden=True still consults ``hidden`` for the collecting/singles
    rules but returns dismissed albums as gaps, so a caller that saves a
    snapshot can record the whole truth and filter when it displays.
    """
    owned_titles = owned_titles or {}
    handled_id_keys = {
        str(album_id) for album_id in handled_ids if album_id is not None
    }
    gaps = []
    pairs = dedup_album_versions(
        [a for a in catalog if is_lossless_album(a)],
        prefer_hires=opts.prefer_hires)
    if not opts.include_comps:
        pairs = filter_compilation_albums(pairs, artist_name)
    if not opts.include_singles:
        pairs = filter_short_releases(pairs, cfg.MISSING_ALBUMS_MIN_TRACKS)
    if cfg.EXCLUDE_LIVE_ALBUMS:
        pairs = [(a, n) for (a, n) in pairs
                 if not _is_live_release(a.get("title") or "")]
    for album, _n_versions in pairs:
        album_id = album.get("id")
        if album_id is not None and str(album_id) in handled_id_keys:
            continue
        # A deliberately downloaded single from this album: the owned pass
        # suppresses it, but this fully-missing pass would otherwise re-offer
        # it as a gap.
        if _is_single(single_store, artist_name, album):
            continue
        if not include_hidden and _is_hidden(hidden, artist_name, album):
            continue
        existing, album_dir = find_existing_tracks(album)
        if album_dir is not None and str(album_dir) in resolved_dirs:
            continue
        if existing and quick:
            # New-release check: a catalog entry that resolves to a folder the
            # user already has (even partially) isn't new - skip it without the
            # track-list fetch the gap path below would need.
            continue
        if existing:
            # Resolves to a folder the owned pass didn't walk (e.g. a
            # collaboration filed under a second artist).
            album, tracks = _materialize_tracks(album, token)
            if not tracks:
                continue
            missing, present = compute_missing(tracks, existing)
            # Resolution matched on folder-name similarity alone, so a
            # different but similarly-named album that merely shares a title
            # (an "Intro") can land here.
            if len(present) * 2 >= len(existing):
                if missing and present:
                    gaps.append(AlbumGap(album, album_dir,
                                         list(missing), list(present)))
                continue
        # Not resolved, or resolved to a wrong folder.
        if _owned_by_name(owned_titles, album, artist_name):
            continue
        gaps.append(AlbumGap(album, None))
    return gaps


def _catalog_fetch_incomplete(catalog, total, limit) -> bool:
    """True when get_artist_albums returned a transient SHORT page rather than a
    legitimate cap. A capped result fills the page (len == limit); a short page
    (a partial 200 mid-pagination) has fewer than BOTH the limit and Qobuz's own
    reported total. A fully-empty 200 (total None, no albums key) is incomplete
    too. Recording a baseline from an incomplete fetch is the bug this guards:
    the dropped albums later re-surface as 'new releases' (flagged for
    review) or as missing gaps. Mirrors get_artist_albums' own `complete` test.
    """
    if not catalog and total is None:
        return True
    return total is not None and len(catalog) < total and len(catalog) < limit


def find_missing_for_artist(query, *, token, opts=None, artist_dir=None,
                            hidden=None, single_store=None, want_missing=True,
                            skip_dir=None, fresh=False, include_hidden=False):
    """Find what's missing for one artist.

    query        - artist name (or folder name) used to resolve the Qobuz artist.
    opts         - DiscoveryOpts (prefer_hires / include_comps / include_singles).
    artist_dir   - the artist's on-disk folder; resolved from query when omitted.
    hidden       - a loaded hidden-store (library.hidden.load()) to filter bulk
                   walks; pass None for a single-artist request, which sees all.
    want_missing - when False, only owned-album gaps are returned (no fully-
                   missing albums); the album-fill walk uses this.
    skip_dir     - optional predicate(Path) -> bool; folders it accepts are left
                   unmatched (the album-fill walk skips already-decided albums).
    fresh        - bypass the catalog cache so an explicit single-artist scan
                   sees just-released albums; the bulk walk leaves it off.
    include_hidden - return dismissed albums among the gaps anyway. The bulk
                   scan uses this so its saved snapshot keeps every candidate
                   and a later restore has something to bring back.
    """
    opts = opts or DiscoveryOpts()
    artist_id, artist_name = resolve_artist(query, token)
    if not artist_id:
        return DiscoveryResult(None, None)
    if artist_dir is None:
        artist_dir = resolve_artist_dir(query)

    catalog, total = get_artist_albums(artist_id, token,
                                       limit=cfg.ARTIST_CATALOG_LIMIT, fresh=fresh)
    truncated = bool(total and total > len(catalog))
    if truncated:
        log.info(f"  Qobuz lists {total} albums; scanning the first "
                 f"{len(catalog)}.")

    # catalog_incomplete is the PRECISE short-page signal (distinct from the
    # informational `truncated` above, which also fires on a legitimate cap):
    # only a fetch shorter than BOTH the limit and the total is untrustworthy.
    result = DiscoveryResult(
        artist_id, artist_name, catalog=catalog,
        catalog_incomplete=_catalog_fetch_incomplete(
            catalog, total, cfg.ARTIST_CATALOG_LIMIT))
    handled_ids = set()
    resolved_dirs = set()

    album_dirs = list_artist_album_dirs(artist_dir) if artist_dir else []
    owned_titles = owned_album_titles(album_dirs)
    for ad in album_dirs:
        if skip_dir is not None and skip_dir(ad):
            continue
        m = match_album_dir(ad, artist_name, token,
                            catalog=catalog, prefer_hires=opts.prefer_hires)
        classify_owned_match(result, m, hidden, single_store, artist_name,
                             handled_ids, resolved_dirs,
                             include_hidden=include_hidden)

    # Skip the catalog walk for an artist you own only downloaded singles by - they
    # aren't one you're collecting, so their back catalogue shouldn't surface.
    # hidden is None on an explicit single-artist request, which always sees all.
    if want_missing and (hidden is None
                         or _collecting(single_store, artist_name, album_dirs)):
        result.gaps.extend(discover_fully_missing(
            artist_name, catalog, opts, hidden=hidden, handled_ids=handled_ids,
            resolved_dirs=resolved_dirs, owned_titles=owned_titles, token=token,
            single_store=single_store, include_hidden=include_hidden))

    vlog(f"  discovery({artist_name!r}): {len(result.partials)} partial, "
         f"{len(result.fully_missing)} fully-missing, "
         f"{len(result.complete)} complete, {len(result.unmatched_dirs)} unmatched, "
         f"{len(result.skipped)} skipped")
    return result


def find_new_releases_for_artist(query, *, token, opts=None, seen_by_id=None,
                                 hidden=None, single_store=None, artist_dir=None,
                                 baseline_only=False):
    """Albums new to this artist's Qobuz catalog since the last check that the
    user doesn't own and hasn't hidden - the cheap "what's new" path.

    Unlike find_missing_for_artist this skips the owned-folder matching pass and
    never fetches track lists (a fully-missing album is read straight off the
    catalog list), so the cost is about one Qobuz call per artist.

    seen_by_id maps artist_id → the catalog album ids known at the last check.
    The first time an artist is seen its baseline is recorded and nothing is
    surfaced, so the back catalogue isn't dumped as "new". current_ids is always
    this run's full lossless id set, for the caller to store as the next baseline.
    """
    opts = opts or DiscoveryOpts()
    artist_id, artist_name = resolve_artist(query, token)
    if not artist_id:
        # No id and no name is Qobuz answering "no such artist"; an id-less
        # name is a partial response the next check may well resolve, so only
        # the first is reported as permanently unresolvable.
        return NewReleaseResult(None, artist_name, unresolved=artist_name is None)
    # The baseline is persisted as JSON (string keys), so key everything by a
    # string id - otherwise an int id from resolve never matches the reloaded
    # snapshot and every run silently re-baselines instead of diffing.
    artist_id = str(artist_id)
    if artist_dir is None:
        artist_dir = resolve_artist_dir(query)

    # Always fresh: stale catalog data would mean missing the very release this
    # check exists to find. The fetch refreshes the shared cache as a side effect.
    catalog, _total = get_artist_albums(artist_id, token,
                                        limit=cfg.ARTIST_CATALOG_LIMIT, fresh=True)
    # Don't record a baseline from an INCOMPLETE fetch.
    if _catalog_fetch_incomplete(catalog, _total, cfg.ARTIST_CATALOG_LIMIT):
        prev = (seen_by_id or {}).get(artist_id)
        return NewReleaseResult(artist_id, artist_name, [],
                                prev if prev is not None else [],
                                fetch_failed=True)
    lossless = [a for a in catalog if is_lossless_album(a)]
    current_ids = [str(a["id"]) for a in lossless if a.get("id") is not None]

    # Two cases where we record the snapshot as baseline but surface NOTHING:
    # - capped: a catalogue bigger than the fetch cap comes back as a
    # different unstable slice each run (Qobuz has no stable sort), so the
    # diff can't be trusted - it would oscillate and dump old albums as "new".
    capped = (len(catalog) >= cfg.ARTIST_CATALOG_LIMIT
              or (_total is not None and _total > cfg.ARTIST_CATALOG_LIMIT))
    if capped or baseline_only:
        return NewReleaseResult(artist_id, artist_name, [], current_ids)

    album_dirs = list_artist_album_dirs(artist_dir) if artist_dir else []
    # Still record the baseline so a later real album doesn't dump the back
    # catalogue as "new" - but an artist you own only singles by isn't one
    # you're collecting, so don't surface their releases.
    if not _collecting(single_store, artist_name, album_dirs):
        return NewReleaseResult(artist_id, artist_name, [], current_ids)

    seen = (seen_by_id or {}).get(artist_id)
    if seen is None:
        return NewReleaseResult(artist_id, artist_name, [], current_ids)

    # "New" = appeared in the catalogue since the baseline - including an old
    # album Qobuz only just added, which is genuinely new TO YOU.
    seen_set = set(seen)
    # An id-less album never makes it into the baseline (current_ids filters
    # id=None), so skip it here too - str(None) would otherwise miss the set and
    # flag it new on every check.
    fresh = [a for a in lossless if a.get("id") is not None
             and str(a.get("id")) not in seen_set]
    # A "new release" should be recent.
    fresh = [a for a in fresh
             if album_released_within(a, cfg.NEW_RELEASE_MAX_AGE_DAYS)]
    if not fresh:
        return NewReleaseResult(artist_id, artist_name, [], current_ids)

    new_gaps = discover_fully_missing(
        artist_name, fresh, opts, hidden=hidden,
        owned_titles=owned_album_titles(album_dirs), token=token, quick=True,
        single_store=single_store)
    return NewReleaseResult(artist_id, artist_name, new_gaps, current_ids)


def classify_owned_match(result, m, hidden, single_store, artist_name,
                         handled_ids, resolved_dirs, include_hidden=False):
    """Fold one DirMatch into the running DiscoveryResult, recording which
    catalog ids and folders the owned pass has now accounted for. A false or
    track-less match still counts as accounted-for (its folder fuzz-resolved
    there), so the missing pass won't re-offer that album; a predicted-path
    mismatch, a low-overlap wrong-album match, or no-match does not."""
    ad = m.album_dir
    if m.status == "no_match":
        result.unmatched_dirs.append(ad)
        return
    if m.status == "predicted_path_mismatch":
        result.skipped.append({"dir": ad, "reason": "predicted_path_mismatch",
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?"})
        return
    if m.status == "low_overlap":
        # A different, similarly-named album fuzz-matched this folder; like a
        # path mismatch it's NOT accounted for, so the real album it matched
        # stays eligible to be offered fully-missing rather than re-downloaded
        # over this unrelated folder.
        result.skipped.append({"dir": ad, "reason": "low_overlap",
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?"})
        return
    if m.qobuz_album and m.qobuz_album.get("id") is not None:
        handled_ids.add(m.qobuz_album["id"])
    resolved_dirs.add(str(ad))
    if m.status == "no_tracks":
        result.skipped.append({"dir": ad, "reason": "no_tracks",
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?"})
        return
    if m.status == "false_match":
        n_qobuz = len((m.qobuz_album.get("tracks") or {}).get("items") or [])
        result.skipped.append({"dir": ad, "reason": "false_match",
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?",
                               "n_existing": len(m.existing), "n_qobuz": n_qobuz})
        return
    if m.status == "partial":
        if _is_single(single_store, artist_name, m.qobuz_album):
            # A track the user downloaded on purpose. Its album id is already in
            # handled_ids (added above), so the missing pass won't re-offer it.
            result.singles.append({"dir": ad, "qobuz_album": m.qobuz_album,
                                   "present": m.present, "missing": m.missing})
            return
        if not include_hidden and _is_hidden(hidden, artist_name, m.qobuz_album):
            return
        result.gaps.append(AlbumGap(m.qobuz_album, ad, m.missing, m.present))
    else:
        # Graduation is completeness-driven, not button-driven: an album that's
        # now fully present on disk drops any stale single marker no matter which
        # path completed it (CLI fill, repair, bulk import) - otherwise the mark
        # suppresses the artist from walks and new-release checks forever.
        if single_store is not None and _is_single(single_store, artist_name,
                                                   m.qobuz_album):
            hidden_mod.unmark_single(
                artist_name,
                (m.qobuz_album or {}).get("title") or "",
                year=album_year(m.qobuz_album or {}),
                album_id=(m.qobuz_album or {}).get("id"),
            )
        result.complete.append({"dir": ad, "qobuz_album": m.qobuz_album,
                                 "existing": m.existing})
