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
prefers the artist whose catalog holds the owned albums, then the deepest
catalog over a bare-name twin, cached to disk).
"""
import json
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
from qobuz_librarian.api.search import (
    get_album,
    get_artist_albums,
    get_artist_release_types,
    search_artists,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import missing_types, tags
from qobuz_librarian.library.catalog import (
    _catalog_candidates_for_dir,
    _count_audio_files_in,
    _dir_year,
    _paths_equal,
    _pick_best_target_dir_match,
    _primary_artist_of,
    album_released_within,
    album_year,
    album_year_int,
    bears_album_title,
    bonus_versions,
    compute_missing,
    dedup_album_versions,
    edition_missing,
    filter_compilation_albums,
    filter_owned_albums,
    filter_short_releases,
    find_album_dir_filesystem,
    find_existing_tracks,
    find_qobuz_album_for_dir,
    held_by_other_versions,
    holds_recordings,
    is_lossless_album,
    same_edition,
    unstreamable,
)
from qobuz_librarian.library.scanner import (
    list_artist_album_dirs,
    list_library_artists,
    read_album_dir,
)
from qobuz_librarian.library.tags import (
    normalize,
    similarity,
    strip_leading_article,
)
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog

# ── Artist resolution (the shared matcher) ──────────────────────────────────────

# Folder name → matched Qobuz artist, cached to disk. Picks made without
# looking at the folders are listed as unchecked and confirmed against the
# folders the next time the artist is scanned; a file without that list
# predates it, so all its picks load as unchecked. The file stays at version
# 1, which older versions still read, ignoring the list. Version 2 files hold
# the same list.
_RESOLVE_CACHE_VERSION = 1
_resolve_cache = None
_resolve_unchecked = set()
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
                    if (
                        isinstance(raw, dict)
                        and raw.get("version") == _RESOLVE_CACHE_VERSION
                        and isinstance(raw.get("entries"), dict)
                    ):
                        for query, hit in raw["entries"].items():
                            if (
                                not isinstance(hit, (list, tuple))
                                or len(hit) != 2
                            ):
                                continue
                            artist_id, artist_name = hit
                            if (
                                isinstance(artist_id, bool)
                                or not isinstance(artist_id, (str, int))
                                or not str(artist_id).strip()
                                or not isinstance(artist_name, str)
                                or not artist_name.strip()
                            ):
                                continue
                            loaded[str(query)] = [artist_id, artist_name]
                        if isinstance(raw.get("unchecked"), list):
                            _resolve_unchecked.update(
                                q for q in raw["unchecked"] if q in loaded)
                        else:
                            _resolve_unchecked.update(loaded)
                except (OSError, ValueError):
                    pass
                _resolve_cache = loaded
    return _resolve_cache


def cached_artist_resolutions() -> dict:
    """The folder-name to [id, name] map past scans resolved, as a plain copy.

    Discover reads it to learn what Qobuz calls each artist on the shelf, which
    is usually how Last.fm spells them too.
    """
    cache = _load_resolve_cache()
    with _resolve_cache_lock:
        return dict(cache)


def flush_resolve_cache():
    """Persist the resolution cache to disk, only if it changed."""
    global _resolve_cache_dirty
    # Scan workers keep matching artists while this runs, so the snapshot is
    # taken under the lock their changes hold.
    with _resolve_cache_lock:
        if not _resolve_cache_dirty or _resolve_cache is None:
            return
        payload = json.dumps({
            "version": _RESOLVE_CACHE_VERSION,
            "entries": _resolve_cache,
            "unchecked": sorted(q for q in _resolve_unchecked
                                if q in _resolve_cache),
        })
        _resolve_cache_dirty = False
    try:
        state_file.write_chunks(cfg.ARTIST_RESOLVE_CACHE_FILE, (payload,))
    except OSError:
        with _resolve_cache_lock:
            _resolve_cache_dirty = True


# The tag Qobuz adds to tell namesakes apart: 'Tesla (Band)'.
_NAMESAKE_TAG_RE = re.compile(r"\s+\([^()]+\)$")


def _close_artists(results, query):
    """The search results whose name is close enough to be `query`, best first:
    closest name, then deepest catalog. A namesake tag is left out of the
    comparison, so 'Tesla (Band)' matches 'Tesla' better than 'Teslah' does,
    but ranks after a name that matches as written."""
    q = strip_leading_article(query)
    scored = []
    for a in results:
        name = strip_leading_article(a.get("name", ""))
        as_written = similarity(name, q)
        score = max(as_written, similarity(_NAMESAKE_TAG_RE.sub("", name), q))
        scored.append((score, as_written, a.get("albums_count") or 0, a))
    close = [s for s in scored if s[0] >= cfg.ARTIST_NAME_THRESH]
    close.sort(key=lambda s: s[:3], reverse=True)
    return [a for *_rank, a in close]


def pick_best_artist(results, query, token=None, fetch_titles=None):
    """The artist search result that is really `query`, or None.

    Qobuz lists the canonical artist ('The Beatles') next to bare-name twins
    ('Beatles') that aggregate covers, interviews and bootlegs. A raw string
    match favours the twin - it has no 'The ' to cost it similarity - so compare
    with the leading article stripped and, among equally close names, take the
    one with the deepest catalog: the real artist's, not the twin's.

    When several artists are that close, `fetch_titles` is called for album
    titles known to be by `query`, keyed with owned_title_key, and the artist
    whose catalog lists the most of them wins.
    """
    close = _close_artists(results, query)
    if not close:
        return None
    if len(close) > 1 and fetch_titles is not None:
        titles = fetch_titles()
        if titles:
            return _artist_holding_titles(close, titles, token)
    return close[0]


# How a folder and Qobuz join two acts: "A, B", "A and B", "A & B", "A with B".
_ACT_JOINER_RE = re.compile(r"\s*(?:,|&|\+|\band\b|\bwith\b)\s*", re.IGNORECASE)


def _same_name(name, query):
    """Whether an artist name is the query itself, not just close to it."""
    def key(text):
        text = strip_leading_article(_NAMESAKE_TAG_RE.sub("", text or ""))
        return normalize(_ACT_JOINER_RE.sub(" ", text))
    return bool(key(query)) and key(name) == key(query)


def _titles_listed(artist_id, token, owned_titles, *, strict=False):
    """How many of the owned album titles the artist's Qobuz catalog lists.
    Reads through the shared catalog cache the scan itself uses. ``strict``
    raises ArtistSearchUnavailable when the catalog can't be read, where a 0
    would be taken for an answer."""
    if not artist_id:
        return 0
    try:
        catalog, _total = get_artist_albums(artist_id, token,
                                            limit=cfg.ARTIST_CATALOG_LIMIT)
    except QobuzError as e:
        if strict:
            raise ArtistSearchUnavailable(str(e)) from e
        return 0
    listed = {owned_title_key(a.get("title")) for a in catalog}
    return sum(1 for key in owned_titles if key in listed)


def _artist_holding_titles(close, owned_titles, token):
    """Among same-named artists, the one whose catalog lists the most owned
    album titles. Two acts can share a name ('Nico', 'The Clash' next to a
    rapper called Clash), and the deepest catalog is not always the one on
    the shelf. The usual pick is checked first; the others' catalogs are only
    fetched when it misses an owned title."""
    best = close[0]
    held = _titles_listed(best.get("id"), token, owned_titles)
    if held >= len(owned_titles):
        return best
    for other in close[1:]:
        n = _titles_listed(other.get("id"), token, owned_titles)
        if n > held:
            best, held = other, n
    return best


def resolve_artist(query, token, owned_titles=None):
    """Return (artist_id, artist_name) for the best Qobuz match, or (None, None).

    The matching itself is pick_best_artist; this adds the on-disk cache, so a
    re-scan skips the search call. flush_resolve_cache() persists new matches.
    ``owned_titles`` (owned_album_titles of the artist's folders) settles a
    name shared by several Qobuz artists in favour of the one whose catalog
    holds those albums, and confirms a cached pick made before that check. A
    cached pick that misses some of them keeps its place unless another
    artist's catalog holds more, whether or not the search still returns it.
    """
    global _resolve_cache_dirty
    cache = _load_resolve_cache()
    hit = cache.get(query)
    if hit is not None:
        unchecked = query in _resolve_unchecked
        if not owned_titles or (not unchecked and _same_name(hit[1], query)):
            return hit[0], hit[1]
        held = _titles_listed(hit[0], token, owned_titles)
        if held >= len(owned_titles) or (held and not unchecked):
            if unchecked:
                with _resolve_cache_lock:
                    _resolve_unchecked.discard(query)
                    _resolve_cache_dirty = True
            return hit[0], hit[1]
    # Qobuz finds nothing for ".38 Special", only for "38 Special". Without
    # the dot a name can match another artist, so the bare name is trusted
    # only when that artist's catalog lists one of the folder's albums.
    lookup = query
    try:
        results = search_artists(query, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
        bare = query.strip().lstrip("._-").strip()
        if (owned_titles and bare and bare != query.strip()
                and not _close_artists(results, query)):
            lookup = bare
            results = [a for a in search_artists(
                bare, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
                if _titles_listed(a.get("id"), token, owned_titles)]
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception as e:
        log.info(f"  artist search failed for '{query}': {e}")
        if hit is not None:
            return hit[0], hit[1]
        raise ArtistSearchUnavailable(str(e)) from e
    close = _close_artists(results, lookup)
    if not close:
        if hit is not None:
            return hit[0], hit[1]
        return None, None
    if hit is not None:
        if all(str(a.get("id")) != str(hit[0]) for a in close):
            close.append({"id": hit[0], "name": hit[1]})
        close.sort(key=lambda a: str(a.get("id")) != str(hit[0]))
    best = close[0]
    if owned_titles and len(close) > 1:
        best = _artist_holding_titles(close, owned_titles, token)
    # A near name ("Burning Witches" for "Burning Witch") is another act unless
    # its catalog lists one of the folder's albums.
    if (owned_titles and not _same_name(best.get("name"), lookup)
            and not _titles_listed(best.get("id"), token, owned_titles,
                                   strict=True)):
        best = next((a for a in close if _same_name(a.get("name"), lookup)),
                    None)
    if best is None:
        if hit is not None:
            with _resolve_cache_lock:
                cache.pop(query, None)
                _resolve_unchecked.discard(query)
                _resolve_cache_dirty = True
        return None, None
    aid, aname = best.get("id"), best.get("name")
    # A name match with no id is a malformed/partial result, not a usable hit -
    # the callers need the id, so caching [None, name] would skip the artist on
    # every later scan. Treat it as a miss (don't cache); a later scan retries.
    if aid:
        with _resolve_cache_lock:
            cache[query] = [aid, aname]
            if owned_titles:
                _resolve_unchecked.discard(query)
            else:
                _resolve_unchecked.add(query)
            _resolve_cache_dirty = True
    return aid, aname


def resolve_folder_artist(name, token, album_dirs):
    """resolve_artist for an artist folder holding ``album_dirs``, plus
    whether the folder's first-named artist stands in for it.

    A folder named for several artists ("A, B") finds no Qobuz artist of
    that name. The first artist it names stands in when that artist's
    catalog lists one of the folder's albums.
    """
    artist_id, artist_name = resolve_artist(
        name, token, owned_album_titles(album_dirs))
    lead = _primary_artist_of(name)
    if artist_id or not album_dirs or lead == name.strip():
        return artist_id, artist_name, False
    lead_id, lead_name = resolve_artist(lead, token)
    if not lead_id:
        return artist_id, artist_name, False
    try:
        albums, _total = get_artist_albums(lead_id, token,
                                           limit=cfg.ARTIST_CATALOG_LIMIT)
    except QobuzError as e:
        raise ArtistSearchUnavailable(str(e)) from e
    if any(_pick_best_target_dir_match(
            _catalog_candidates_for_dir(ad, albums, lead_name), ad)
           for ad in album_dirs):
        return lead_id, lead_name, True
    return artist_id, artist_name, False


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
        candidates = list_library_artists(
            on_artist_error=lambda path, error: log.warning(
                f"Unreadable artist {path.name}: {error}. Check permissions and retry."))
    if not candidates:
        return None

    target = normalize(artist_query)
    if not target:
        return None

    for d in candidates:
        if normalize(d.name) == target:
            return d

    target_bare = tags.bare_artist_key(artist_query)
    for d in candidates:
        if tags.bare_artist_key(d.name) == target_bare:
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
    include_live: bool = False

    def included_types(self) -> frozenset:
        """The release types listed beside albums."""
        return frozenset(name for name, on in (
            (missing_types.LIVE, self.include_live),
            (missing_types.COMPILATION, self.include_comps),
            (missing_types.EP, self.include_singles),
        ) if on)


@dataclass
class AlbumGap:
    """One album that needs attention. on_disk_dir is None for a fully-missing
    album; set (with missing/present track lists) for an owned album with gaps."""
    qobuz_album: dict
    on_disk_dir: Path | None
    missing: list = field(default_factory=list)
    present: list = field(default_factory=list)
    # The Qobuz list a fully-missing album is filed in (see missing_types);
    # None when the artist's lists could not be read.
    release_type: str | None = None

    @property
    def fully_missing(self) -> bool:
        return self.on_disk_dir is None

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def bonus_only(self) -> bool:
        """Whether every missing track is another version of a song the
        folder holds, which leaves the album complete."""
        return (self.on_disk_dir is not None and bool(self.missing)
                and len(bonus_versions(self.missing, self.present))
                == len(self.missing))


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
    on_disk: set = field(default_factory=set)         # catalog ids found in a folder
    # The catalog fetch was a transient SHORT page (a partial 200), NOT a
    # legitimate cap - so it's not the whole discography - or Qobuz didn't
    # return a release a folder's edition check needed. Either way this
    # isn't the artist's whole picture.
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
    # True for a folder named for several artists that the first one named
    # stands in for. It holds a few of that artist's albums, so nothing new
    # is listed for it, and it is neither unchecked nor unknown to Qobuz.
    by_lead: bool = False


def owned_title_key(name):
    """The key an album folder name is filed under in the owned-title map.

    Shared so anything comparing a name that never came from a folder listing -
    a title out of a collection backup, say - keys it exactly the way the
    folders were keyed.
    """
    return normalize(strip_leading_article(tags.strip_album_decorations(name or "")))


def _record_owned_title(owned_titles, album_dir):
    year = _dir_year(album_dir.name)
    for bare in tags.folder_title_forms(album_dir.name, album_dir.parent.name):
        key = normalize(strip_leading_article(bare))
        if key:
            owned_titles.setdefault(key, set()).add(year)


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


def _collecting(single_store, artist_name, album_dirs, complete_dirs=()):
    """An artist is 'collected' - surfaced by the bulk catalog walk and the
    new-release check - only when they own at least one album folder that isn't
    just a downloaded single. A folder the owned pass found complete is an
    album whatever its mark says. No single store (an explicit single-artist
    request) means show everything."""
    if single_store is None:
        return True
    # Derive per-folder, not by count: an artist is collecting if ANY owned
    # folder isn't a downloaded single (is_single normalises the folder name
    # to the mark's fingerprint).
    return any(str(ad) in complete_dirs
               or not hidden_mod.is_single(
                   artist_name, ad.name, single_store, year=_dir_year(ad.name))
               for ad in album_dirs)


def _filled_single_dirs(single_store, artist_name, album_dirs, catalog):
    """Folders marked as a downloaded single that now hold as many tracks as
    the marked album has. The new-release check has no owned-folder pass to
    call them complete, and scans leave the mark in place, so an artist whose
    single was filled in later would otherwise never be checked again."""
    if single_store is None:
        return set()
    marked = {}
    for album in catalog:
        n = album.get("tracks_count")
        if (isinstance(n, int) and n > 0
                and _is_single(single_store, artist_name, album)):
            key = hidden_mod.album_fingerprint(artist_name, album.get("title"))
            marked[key] = min(n, marked.get(key, n))
    filled = set()
    for ad in album_dirs:
        n = marked.get(hidden_mod.album_fingerprint(artist_name, ad.name))
        if n and _count_audio_files_in(ad) >= n:
            filled.add(str(ad))
    return filled


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


def _held_edition(album, existing, album_dir, artist_name, token, catalog,
                  prefer_hires):
    """The release filed in ``album_dir`` that the files there are the same
    edition of (same_edition), or None; whether Qobuz left a release that
    could be it unchecked; and the closest release, the one that pairs with
    the most files. ``album`` stands when none of its songs is on the files
    as another version; otherwise, of it and the other releases in
    ``catalog`` filed there, the one that pairs with the most files, the
    first on a tie. A gap is filled from that edition only, so a folder
    never mixes two masters."""
    def fit(release):
        missing, present = edition_missing(release, existing, album_dir)
        held = same_edition(release, existing, album_dir, token, fill=True)
        return len(present), held, held and not held_by_other_versions(
            release, existing, album_dir, missing)

    fits = [(album, fit(album))]
    if fits[0][1][2]:
        return album, False, album
    unchecked = False
    for other in _catalog_candidates_for_dir(
            album_dir, catalog, artist_name, prefer_hires):
        if str(other.get("id")) == str(album.get("id")):
            continue
        predicted = find_album_dir_filesystem(other, audio_only=True)
        if predicted is None or not _paths_equal(predicted, album_dir):
            continue
        try:
            full = get_album(other["id"], token)
        except QobuzError:
            unchecked = True
            continue
        if (full.get("tracks") or {}).get("items"):
            fits.append((full, fit(full)))

    def most_files(entries):
        return max(entries, key=lambda entry: entry[1][0])

    closest = most_files(fits)[0]
    held = [entry for entry in fits if entry[1][1]]
    best = most_files(held) if held else None
    if best is None or (unchecked and not best[1][2]):
        return None, unchecked, closest
    return best[0], False, best[0]


def _edition_gap(album, existing, album_dir):
    """compute_missing for a gap fill from ``album``, counting a song the
    folder holds as another version as present, so the fill leaves it out."""
    tracks = album["tracks"]["items"]
    missing, _present = compute_missing(tracks, existing)
    others = {id(t) for t in held_by_other_versions(album, existing, album_dir, missing)}
    missing = [t for t in missing if id(t) not in others]
    left = {id(t) for t in missing}
    return missing, [t for t in tracks if id(t) not in left]


@dataclass
class DirMatch:
    """How one owned folder matched the catalog. status is one of: partial,
    complete, false_match, low_overlap, predicted_path_mismatch, no_tracks,
    other_edition (no listed release is the edition the folder holds;
    qobuz_album is the closest one), unchecked (Qobuz did not return a
    release that could be it), no_match."""
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
    predicted = find_album_dir_filesystem(album, audio_only=True)
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
    if missing:
        held, unchecked, closest = _held_edition(
            album, existing, album_dir, artist_name, token, catalog, prefer_hires)
        if held is None:
            if unchecked:
                return DirMatch("unchecked", album_dir, album, existing=existing)
            return DirMatch("other_edition", album_dir, closest, existing=existing)
        album = held
        missing, present = _edition_gap(album, existing, album_dir)
    status = "partial" if missing else "complete"
    return DirMatch(status, album_dir, album,
                    list(missing), list(present), existing)


def _gap_held_by_siblings(m, matches):
    """True when every track missing from a partial folder is in another folder
    of the same album: one matched to the same release, or one with the same
    title, like a second copy or another edition. A Gap Fill there would only
    download tracks already on disk."""
    key = owned_title_key(m.album_dir.name)
    held = []
    for other in matches:
        if other is m:
            continue
        same_release = (other.qobuz_album is not None and other.qobuz_album.get("id")
                        == m.qobuz_album.get("id"))
        if same_release or owned_title_key(other.album_dir.name) == key:
            held.extend(other.existing or read_album_dir(other.album_dir))
    return bool(held) and not compute_missing(m.missing, held)[0]


def _short(album):
    return not filter_short_releases([(album, 1)], cfg.MISSING_ALBUMS_MIN_TRACKS)


def _looks_live(album):
    return cfg.EXCLUDE_LIVE_ALBUMS and _is_live_release(album.get("title") or "")


def _filtered_albums(albums, artist_name, opts):
    """One edition per album, past the compilation, length and live-title
    filters: what an artist without readable release lists is offered."""
    pairs = dedup_album_versions(albums, prefer_hires=opts.prefer_hires)
    if not opts.include_comps:
        pairs = filter_compilation_albums(pairs, artist_name)
    if not opts.include_singles:
        pairs = filter_short_releases(pairs, cfg.MISSING_ALBUMS_MIN_TRACKS)
    return [album for album, _n in pairs]


def _typed_albums(albums, release_types, artist_name, opts):
    """(album, type) for each album in one of the offered Qobuz lists, one
    edition per album and type. The older filters still sort what Qobuz
    files: an album or compilation credited first to another artist counts
    as a compilation (that artist's live releases and singles are left out),
    anything else shorter than MISSING_ALBUMS_MIN_TRACKS as an EP or single,
    and with EXCLUDE_LIVE_ALBUMS an album or compilation titled as a live
    recording as live."""
    by_type = {}
    for album in albums:
        rtype = release_types.get(str(album.get("id")))
        if rtype is None:
            continue
        if not filter_compilation_albums([(album, 1)], artist_name):
            if rtype in (missing_types.LIVE, missing_types.EP):
                continue
            rtype = missing_types.COMPILATION
        elif _short(album):
            rtype = missing_types.EP
        elif rtype != missing_types.EP and _looks_live(album):
            rtype = missing_types.LIVE
        by_type.setdefault(rtype, []).append(album)
    typed = []
    for rtype, group in by_type.items():
        pairs = dedup_album_versions(group, prefer_hires=opts.prefer_hires)
        typed.extend((album, rtype) for album, _n in pairs)
    return typed


def artist_release_types(artist_id, artist_name, token, albums):
    """The Qobuz list each of ``albums`` is filed in, for
    discover_fully_missing, or None when the lists can't be read and the
    artist's albums are offered untyped."""
    album_ids = [a["id"] for a in albums
                 if is_lossless_album(a) and a.get("id") is not None]
    if not album_ids:
        return {}
    try:
        types = get_artist_release_types(artist_id, token, album_ids)
    except (AuthLost, QobuzUnavailable):
        raise
    except QobuzError as e:
        log.info(f"  {artist_name}: Qobuz's release lists couldn't be read "
                 f"({e}); listing every release.")
        return None
    if types is None:
        log.info(f"  {artist_name}: Qobuz's release lists came back "
                 "incomplete; listing every release.")
    return types


def discover_fully_missing(artist_name, catalog, opts, *, hidden=None,
                           handled_ids=frozenset(), resolved_dirs=frozenset(),
                           owned_titles=None, token=None, quick=False,
                           single_store=None, include_hidden=False,
                           release_types=None, on_disk=None, unchecked=None):
    """Catalog albums the owned-album pass didn't account for: fully-missing
    releases, plus a collaboration filed under another artist's folder that
    still has track gaps in the edition that folder holds. Shared by the CLI
    missing-albums step and the web scan. handled_ids / resolved_dirs /
    owned_titles come from the owned pass so an album already matched to a
    folder isn't re-offered as missing.

    release_types (artist_release_types) decides what counts as missing: only
    albums Qobuz files as the artist's albums, live releases, compilations or
    EPs and singles, each gap carrying its type for the caller to show or not
    (missing_types.shown). Without it, missing albums pass the older
    compilation, length and live-title filters and carry no type. The track
    gaps always do.

    quick=True is the new-release path: only fully-missing albums are returned
    and no track lists are fetched, so the cost stays at the catalog list alone.

    include_hidden=True still consults ``hidden`` for the collecting/singles
    rules but returns dismissed albums as gaps, so a caller that saves a
    snapshot can record the whole truth and filter when it displays.

    ``on_disk`` collects the ids of the albums left out because a folder
    holds them, and ``unchecked`` the folders whose edition Qobuz left
    unchecked by not returning a release.
    """
    def held(album):
        if on_disk is not None and album.get("id") is not None:
            on_disk.add(str(album["id"]))

    owned_titles = owned_titles or {}
    handled_id_keys = {
        str(album_id) for album_id in handled_ids if album_id is not None
    }
    gaps = []
    lossless = [a for a in catalog if is_lossless_album(a)]
    fillable = _filtered_albums(lossless, artist_name, opts)
    if release_types is None:
        offered = [(album, missing_types.LIVE if _looks_live(album) else None)
                   for album in fillable]
    else:
        offered = _typed_albums(lossless, release_types, artist_name, opts)
    if cfg.EXCLUDE_LIVE_ALBUMS:
        fillable = [album for album in fillable if not _looks_live(album)]
    fill_keys = {id(album) for album in fillable}
    types = {id(album): rtype for album, rtype in offered}
    albums = {id(album): album for album in fillable}
    albums.update((id(album), album) for album, _rtype in offered)
    for album in sorted(albums.values(), key=lambda a: (
            album_year_int(a), normalize(a.get("title") or ""))):
        key = id(album)
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
        resolved = album_dir is not None and str(album_dir) in resolved_dirs
        if resolved and bears_album_title(album, album_dir):
            held(album)
            continue
        if existing and quick:
            # New-release check: a catalog entry that resolves to a folder the
            # user already has (even partially) isn't new - skip it without the
            # track-list fetch the gap path below would need.
            continue
        if existing:
            # Resolves to a folder the owned pass didn't walk (e.g. a
            # collaboration filed under a second artist), or to an owned
            # folder named for another title, which holds this album only if
            # it holds its songs.
            album, tracks = _materialize_tracks(album, token)
            if not tracks:
                if resolved:
                    held(album)
                continue
            missing, present = compute_missing(tracks, existing)
            missing = [t for t in missing if not unstreamable(t)]
            # Resolution matched on folder-name similarity alone, so a
            # different but similarly-named album that merely shares a title
            # (an "Intro") can land here.
            if (len(present) * 2 >= len(existing)
                    or holds_recordings(tracks, existing)):
                if key in fill_keys and missing and present and not resolved:
                    edition, missed, _closest = _held_edition(
                        album, existing, album_dir, artist_name, token, catalog,
                        opts.prefer_hires)
                    if missed and unchecked is not None:
                        unchecked.add(str(album_dir))
                    if edition is not None:
                        missing, present = _edition_gap(edition, existing, album_dir)
                        missing = [t for t in missing if not unstreamable(t)]
                    if (edition is not None and missing
                            and str(edition.get("id")) not in handled_id_keys
                            and not _is_single(single_store, artist_name, edition)
                            and (include_hidden
                                 or not _is_hidden(hidden, artist_name, edition))):
                        handled_id_keys.add(str(edition.get("id")))
                        gaps.append(AlbumGap(edition, album_dir,
                                             list(missing), list(present)))
                held(album)
                continue
        # Not resolved, or resolved to a wrong folder.
        if _owned_by_name(owned_titles, album, artist_name):
            held(album)
            continue
        if key not in types:
            continue
        gaps.append(AlbumGap(album, None, release_type=types[key]))
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
    opts         - DiscoveryOpts (prefer_hires; include_comps / include_singles
                   loosen the filters an artist without readable release
                   lists falls back to). Fully-missing albums of every
                   offered type come back, each with its release_type.
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
    if artist_dir is None:
        artist_dir = resolve_artist_dir(query)
    album_dirs = list_artist_album_dirs(artist_dir) if artist_dir else []
    owned_titles = owned_album_titles(album_dirs)
    artist_id, artist_name, by_lead = resolve_folder_artist(
        query, token, album_dirs)
    if not artist_id:
        return DiscoveryResult(None, None)

    catalog, total = get_artist_albums(artist_id, token,
                                       limit=cfg.ARTIST_CATALOG_LIMIT, fresh=fresh)
    truncated = bool(total and total > len(catalog))
    if truncated:
        log.info(f"  Qobuz lists {plural(total, 'album')}; scanning the first "
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

    matches = [match_album_dir(ad, artist_name, token,
                               catalog=catalog, prefer_hires=opts.prefer_hires)
               for ad in album_dirs if skip_dir is None or not skip_dir(ad)]
    for m in matches:
        if m.status == "partial":
            # A track Qobuz does not stream is no gap a fill could close.
            fetchable = [t for t in m.missing if not unstreamable(t)]
            if not fetchable or _gap_held_by_siblings(m, matches):
                m = DirMatch("complete", m.album_dir, m.qobuz_album,
                             existing=m.existing)
            elif len(fetchable) < len(m.missing):
                m = replace(m, missing=fetchable)
        classify_owned_match(result, m, hidden, single_store, artist_name,
                             handled_ids, resolved_dirs,
                             include_hidden=include_hidden)
    result.on_disk.update(str(album_id) for album_id in handled_ids)
    if any(m.status == "unchecked" for m in matches):
        result.catalog_incomplete = True

    # Skip the catalog walk for an artist you own only downloaded singles by - they
    # aren't one you're collecting, so their back catalogue shouldn't surface.
    # hidden is None on an explicit single-artist request, which always sees all.
    complete_dirs = {str(entry["dir"]) for entry in result.complete}
    if want_missing and (hidden is None
                         or _collecting(single_store, artist_name, album_dirs,
                                        complete_dirs)):
        handled_keys = {str(album_id) for album_id in handled_ids}
        # A folder its first-named artist stands in for holds a few of that
        # artist's albums: it offers their gaps and lists nothing missing.
        release_types = None if by_lead else artist_release_types(
            artist_id, artist_name, token,
            [a for a in catalog if str(a.get("id")) not in handled_keys])
        unchecked = set()
        gaps = discover_fully_missing(
            artist_name, catalog, opts, hidden=hidden, handled_ids=handled_ids,
            resolved_dirs=resolved_dirs, owned_titles=owned_titles, token=token,
            single_store=single_store, include_hidden=include_hidden,
            release_types=release_types, on_disk=result.on_disk,
            unchecked=unchecked)
        if unchecked:
            result.catalog_incomplete = True
        if by_lead:
            result.gaps.extend(
                gap for gap in gaps if gap.on_disk_dir is not None
                and _paths_equal(gap.on_disk_dir.parent, artist_dir))
        else:
            result.gaps.extend(
                gaps_this_folder_offers(gaps, artist_id, artist_dir, token))

    vlog(f"  discovery({artist_name!r}): {len(result.partials)} partial, "
         f"{len(result.fully_missing)} fully-missing, "
         f"{len(result.complete)} complete, {len(result.unmatched_dirs)} unmatched, "
         f"{len(result.skipped)} skipped")
    return result


def gaps_this_folder_offers(gaps, artist_id, artist_dir, token):
    """``gaps`` found from ``artist_dir`` less the albums another artist
    folder offers: one whose own scan resolves to ``artist_id`` too. An album
    in a folder that can't be read stays here, as that folder can't offer
    it. A failed artist search raises ArtistSearchUnavailable, as it does
    for the folder's own artist.
    """
    owners = {}
    kept = []
    for gap in gaps:
        folder = gap.on_disk_dir.parent if gap.on_disk_dir else None
        if (folder is None or normalize(folder.name) in tags.VA_NORMALIZED
                or (artist_dir is not None and _paths_equal(folder, artist_dir))):
            kept.append(gap)
            continue
        if folder not in owners:
            try:
                owners[folder] = resolve_folder_artist(
                    folder.name, token, list_artist_album_dirs(folder))[0]
            except OSError as e:
                vlog(f"  couldn't read {folder.name!r}: {e}")
                owners[folder] = None
        if owners[folder] is None or str(owners[folder]) != str(artist_id):
            kept.append(gap)
    return kept


def find_new_releases_for_artist(query, *, token, opts=None, seen_by_id=None,
                                 hidden=None, single_store=None, artist_dir=None,
                                 baseline_only=False):
    """Albums new to this artist's Qobuz catalog since the last check that the
    user doesn't own and hasn't hidden - the cheap "what's new" path.

    Unlike find_missing_for_artist this skips the owned-folder matching pass and
    never fetches track lists (a fully-missing album is read straight off the
    catalog list), so the cost is about one Qobuz call per artist, plus the
    release lists for an artist with something new. A new release is listed
    only when its type is among opts.included_types(), as in Missing Albums.

    seen_by_id maps artist_id → the catalog album ids known at the last check.
    The first time an artist is seen its baseline is recorded and nothing is
    surfaced, so the back catalogue isn't dumped as "new". current_ids is always
    this run's full lossless id set, for the caller to store as the next baseline.
    """
    opts = opts or DiscoveryOpts()
    if artist_dir is None:
        artist_dir = resolve_artist_dir(query)
    album_dirs = list_artist_album_dirs(artist_dir) if artist_dir else []
    artist_id, artist_name, by_lead = resolve_folder_artist(
        query, token, album_dirs)
    if by_lead:
        return NewReleaseResult(None, artist_name, by_lead=True)
    if not artist_id:
        # No id and no name is Qobuz answering "no such artist"; an id-less
        # name is a partial response the next check may well resolve, so only
        # the first is reported as permanently unresolvable.
        return NewReleaseResult(None, artist_name, unresolved=artist_name is None)
    # The baseline is persisted as JSON (string keys), so key everything by a
    # string id - otherwise an int id from resolve never matches the reloaded
    # snapshot and every run silently re-baselines instead of diffing.
    artist_id = str(artist_id)

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

    # Still record the baseline so a later real album doesn't dump the back
    # catalogue as "new" - but an artist you own only singles by isn't one
    # you're collecting, so don't surface their releases.
    if not _collecting(single_store, artist_name, album_dirs,
                       _filled_single_dirs(single_store, artist_name,
                                           album_dirs, catalog)):
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
        single_store=single_store,
        release_types=artist_release_types(
            artist_id, artist_name, token, lossless))
    included = opts.included_types()
    new_gaps = [gap for gap in new_gaps
                if missing_types.shown(gap.release_type, included)]
    return NewReleaseResult(artist_id, artist_name, new_gaps, current_ids)


def classify_owned_match(result, m, hidden, single_store, artist_name,
                         handled_ids, resolved_dirs, include_hidden=False):
    """Fold one DirMatch into the running DiscoveryResult, recording which
    catalog ids and folders the owned pass has now accounted for. A track-less
    match, or one where no listed release is the folder's edition or Qobuz
    left one that could be unchecked, still counts as accounted-for (its
    folder fuzz-resolved there), so the missing pass won't re-offer that
    album; a false match, a predicted-path mismatch, a low-overlap
    wrong-album match, or no-match does not."""
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
    if m.status == "false_match":
        # The folder shares no track with the release it matched, so neither
        # is accounted for: the album the folder really holds can still be
        # offered against it, and the matched release as missing.
        n_qobuz = len((m.qobuz_album.get("tracks") or {}).get("items") or [])
        result.skipped.append({"dir": ad, "reason": "false_match",
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?",
                               "n_existing": len(m.existing), "n_qobuz": n_qobuz})
        return
    if m.qobuz_album and m.qobuz_album.get("id") is not None:
        handled_ids.add(m.qobuz_album["id"])
    resolved_dirs.add(str(ad))
    if m.status in ("no_tracks", "other_edition", "unchecked"):
        result.skipped.append({"dir": ad, "reason": m.status,
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?"})
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
        result.complete.append({"dir": ad, "qobuz_album": m.qobuz_album,
                                 "existing": m.existing})
