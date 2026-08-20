"""Build the artist and album feeds shown on Discover."""
import hashlib
import logging
import threading
import time
from difflib import SequenceMatcher

from qobuz_librarian import config as cfg
from qobuz_librarian.api import discover_cache as cache
from qobuz_librarian.api.auth import (
    AuthLost,
    QobuzError,
    QobuzUnavailable,
    token_credential_generation,
)
from qobuz_librarian.api.lastfm import (
    LastfmKeyRejected,
    LastfmRateLimited,
    LastfmUnavailable,
    get_artist_top_tags,
    get_similar_artists,
    get_tag_top_albums,
)
from qobuz_librarian.api.search import (
    get_artist_albums,
    get_user_favorites,
    search_albums,
    search_artists,
)
from qobuz_librarian.library import catalog
from qobuz_librarian.library.discovery import (
    cached_artist_resolutions,
    pick_best_artist,
)
from qobuz_librarian.library.scanner import (
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import normalize
from qobuz_librarian.ui_cli.logging import vlog

# How much of each thing is fetched, resolved and shown. The card counts are
# what the page displays; the attempt caps stop an artist Qobuz has nothing for
# from turning one build into hundreds of fruitless searches.
_RANK_KEEP      = 100
_FEED_CARDS     = 40
_PARTIAL_CARDS  = 12
_PUBLISH_EVERY  = 20
SEARCH_CARDS    = 12
_TAG_SAMPLE     = 120
_TAG_CHIPS      = 8
GENRE_CARDS     = 24
_GENRE_FETCH    = 100
_SEED_CHIPS     = 3
# Last.fm having one bad minute shouldn't lose a build that is most of the way
# through; three refusals in a row is an outage, not a blip. A build that has
# never once reached Last.fm stops at the first refusal instead: there is no
# progress to protect, and three timeouts at ten seconds each would leave the
# page counting to nothing for a minute and a half before saying why.
_MAX_CONSECUTIVE_FAILURES = 3
# How long a failed build is left alone before anything starts it again. Without
# it, every reopened page relaunches a build against the same dead key or the
# same unreachable service.
_RETRY_AFTER_ERROR = 60.0

# Tags that describe the listener rather than the music, and bare years or
# decades, which the decade chips already cover from real Qobuz release dates.
_TAG_DENYLIST = frozenset({
    "seen live", "favorites", "favourites", "favorite songs", "my favorites",
    "under 2000 listeners", "albums i own", "spotify", "beautiful", "awesome",
    "love at first listen", "check out",
})


class Library:
    """The owned artists, in the three forms the rest of the module needs:
    normalized keys to exclude against, raw names for the exclusion cases that
    can't be normalized, seeds to ask Last.fm about, and a signature that
    changes when the library does."""

    def __init__(self, keys, raws, seeds, signature):
        self.keys = keys
        self.raws = raws
        self.seeds = seeds
        self.signature = signature

    def __len__(self):
        return len(self.seeds)

    def owns(self, name: str) -> bool:
        """Whether `name` is an artist already in the library.

        The comparison is generous so spelling variants are not suggested as
        new artists.
        """
        key = normalize(name)
        if not key:
            # A pure CJK or emoji name normalizes to nothing, and comparing
            # nothing to nothing matches everything. Exact text is all that is
            # safe here.
            target = str(name or "").strip()
            return bool(target) and any(target == r.strip() for r in self.raws)
        if key in self.keys:
            return True
        # 'The Beatles' and 'Beatles' are the same shelf.
        flipped = key[3:] if key.startswith("the") else "the" + key
        if flipped and flipped in self.keys:
            return True
        thresh = cfg.FUZZY_DIR_THRESH
        for owned in self.keys:
            matcher = SequenceMatcher(None, key, owned)
            if matcher.real_quick_ratio() < thresh:
                continue
            if matcher.quick_ratio() < thresh:
                continue
            if matcher.ratio() >= thresh:
                return True
        return False


def read_library() -> Library:
    """Walk the library and build the artist picture Discover works from.

    Both names an artist can be known by are kept: the folder on disk, and the
    canonical Qobuz name a past scan resolved it to. Last.fm is asked about the
    canonical one, which is spelled the way Last.fm spells it, while exclusion
    checks both, so a folder called 'Beatles, The' still recognises its own
    artist coming back as a suggestion.
    """
    resolutions = cached_artist_resolutions()
    keys, raws, seeds, albums = set(), [], [], []
    for directory in list_library_artists():
        folder = directory.name
        raws.append(folder)
        folder_key = normalize(folder)
        if folder_key:
            keys.add(folder_key)
        hit = resolutions.get(folder)
        canonical = ""
        if isinstance(hit, (list, tuple)) and len(hit) > 1:
            canonical = str(hit[1] or "").strip()
        if canonical:
            raws.append(canonical)
            canonical_key = normalize(canonical)
            if canonical_key:
                keys.add(canonical_key)
        seeds.append(canonical or folder)
        albums.extend(
            f"{folder.casefold()}/{album.name.casefold()}"
            for album in list_artist_album_dirs(directory)
        )
    signature_parts = {f"key:{key}" for key in keys}
    signature_parts.update(
        f"name:{name.strip().casefold()}" for name in raws if name.strip()
    )
    signature_parts.update(f"album:{album}" for album in albums)
    signature = hashlib.sha1(
        "\n".join(sorted(signature_parts)).encode("utf-8")).hexdigest()
    return Library(keys, raws, seeds, signature)


_library_cache = None
_library_cache_at = 0.0
_library_lock = threading.Lock()
# Long enough that opening the page and its first few polls don't each re-walk
# the library, short enough that a download landing mid-session is noticed.
_LIBRARY_CACHE_SECONDS = 60.0


def library(*, max_age: float = _LIBRARY_CACHE_SECONDS) -> Library:
    global _library_cache, _library_cache_at
    with _library_lock:
        fresh = (_library_cache is not None
                 and (time.time() - _library_cache_at) <= max_age)
        if fresh:
            return _library_cache
    built = read_library()
    with _library_lock:
        _library_cache = built
        _library_cache_at = time.time()
    return built


def rank_candidates(accumulated: dict, owned: Library,
                    limit: int = _RANK_KEEP) -> list[dict]:
    """Order the suggestions and drop the ones already in the library.

    `accumulated` maps a suggested name to the owned artists that named it and
    how strongly. Ties break on how many owned artists agreed, then on name, so
    the same library always produces the same order.
    """
    ranked = []
    for name, acc in accumulated.items():
        if owned.owns(name):
            continue
        ranked.append({
            "name": name,
            "score": round(acc["score"], 4),
            "seeds": acc["seeds"],
        })
    ranked.sort(key=lambda c: (-c["score"], -len(c["seeds"]), c["name"].lower()))
    return ranked[:limit]


def _seed_names(candidate: dict) -> list[str]:
    """The owned artists to name on the card, strongest agreement first."""
    ordered = sorted(candidate.get("seeds") or [],
                     key=lambda pair: (-pair[1], pair[0].lower()))
    out = []
    for name, _match in ordered:
        if name not in out:
            out.append(name)
        if len(out) >= _SEED_CHIPS:
            break
    return out


def _album_row(album: dict) -> dict:
    """One album, trimmed to what a card needs. The two quality fields keep
    their Qobuz names so the page labels them with the same helper the search
    results use, rather than a second opinion about what hi-res means."""
    image = album.get("image") or {}
    cover = image.get("small") or image.get("thumbnail") or ""
    return {
        "id": str(album.get("id")),
        "title": album.get("title") or "?",
        "version": album.get("version") or "",
        "artist": (album.get("artist") or {}).get("name") or "",
        "year": catalog.album_year(album) or "",
        "tracks": album.get("tracks_count") or 0,
        "maximum_bit_depth": album.get("maximum_bit_depth") or 0,
        "maximum_sampling_rate": album.get("maximum_sampling_rate") or 0,
        "cover": cover if cover.startswith("https://static.qobuz.com/") else "",
    }


def resolve_artist(name: str, token) -> dict | None:
    """The Qobuz artist `name` turns out to be, or None if Qobuz has no such
    artist. A no-such-artist answer is remembered, so a library full of names
    Qobuz doesn't carry isn't searched for again on every build."""
    key = cache.artist_resolution_key(normalize(name) or name)
    hit = cache.get_resolution(key)
    if cache.is_miss(hit):
        return None
    if isinstance(hit, dict) and hit.get("id"):
        return hit
    try:
        results = search_artists(name, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
    except (AuthLost, QobuzUnavailable):
        raise
    except QobuzError as e:
        # Qobuz answered badly, which is not the same as answering "no". Don't
        # write a miss the next build would trust.
        vlog(f"discover: artist search failed for {name!r}: {e}")
        return None
    best = pick_best_artist(results, name)
    if not best or not best.get("id"):
        cache.put_resolution_miss(key)
        return None
    payload = {"id": str(best.get("id")), "name": best.get("name") or name}
    cache.put_resolution(key, payload)
    return payload


def artist_albums(
    artist_id,
    artist_name: str,
    token,
    *,
    prefer_hires: bool | None = None,
) -> list[dict]:
    """The artist's albums, one row per record rather than one per edition.

    Short releases are dropped under the same threshold the missing-albums
    step uses: a suggestion's discography is for browsing, and a run of
    one-track singles buries the records worth seeing.
    """
    items, _total = get_artist_albums(artist_id, token)
    if prefer_hires is None:
        prefer_hires = bool(cfg.PREFER_HIRES)
    pairs = catalog.dedup_album_versions(items, prefer_hires=prefer_hires)
    pairs = catalog.filter_compilation_albums(pairs, artist_name)
    pairs = catalog.filter_short_releases(pairs, cfg.MISSING_ALBUMS_MIN_TRACKS)
    # dedup_album_versions already returns oldest first, which is how the rest
    # of the app lists a discography.
    return [_album_row(album) for album, _versions in pairs if album.get("id")]


def resolve_album(
    artist: str,
    title: str,
    token,
    *,
    prefer_hires: bool | None = None,
) -> dict | None:
    """The Qobuz album `artist` / `title` turns out to be, or None.

    Both halves have to match: an album search for a title alone happily
    returns a covers compilation by somebody else.
    """
    if prefer_hires is None:
        prefer_hires = bool(cfg.PREFER_HIRES)
    key = cache.album_resolution_key(
        normalize(artist) or artist,
        f"{normalize(title) or title}|prefer_hires:{int(prefer_hires)}",
    )
    hit = cache.get_resolution(key)
    if cache.is_miss(hit):
        return None
    if isinstance(hit, dict) and hit.get("id"):
        return hit
    try:
        results = search_albums(f"{artist} {title}", token,
                                limit=cfg.ARTIST_LOOKUP_LIMIT)
    except (AuthLost, QobuzUnavailable):
        raise
    except QobuzError as e:
        vlog(f"discover: album search failed for {artist!r} / {title!r}: {e}")
        return None
    best, best_score = None, 0.0
    matches = []
    for album in results:
        if not album.get("id"):
            continue
        found_artist = (album.get("artist") or {}).get("name") or ""
        artist_score = _text_similarity(found_artist, artist)
        found_title = album.get("title") or ""
        title_score = max(
            _text_similarity(found_title, title),
            _text_similarity(
                catalog.strip_album_decorations(found_title),
                catalog.strip_album_decorations(title),
            ),
        )
        if artist_score < cfg.ARTIST_NAME_THRESH:
            continue
        if title_score < cfg.AUTO_SAFE_TITLE_SIM_THRESH:
            continue
        combined = artist_score + title_score
        matches.append((album, combined))
        if combined > best_score:
            best, best_score = album, combined
    if best is None:
        cache.put_resolution_miss(key)
        return None
    best_group_key = (
        normalize(catalog.strip_album_decorations(best.get("title") or ""))
        or catalog.strip_album_decorations(best.get("title") or "")
        .strip()
        .casefold(),
        catalog.album_year_int(best),
    )
    same_release = [
        album
        for album, _score in matches
        if (
            normalize(catalog.strip_album_decorations(album.get("title") or ""))
            or catalog.strip_album_decorations(album.get("title") or "")
            .strip()
            .casefold(),
            catalog.album_year_int(album),
        ) == best_group_key
    ]
    picked = catalog.dedup_album_versions(
        same_release, prefer_hires=prefer_hires)
    if picked:
        best = picked[0][0]
    row = _album_row(best)
    cache.put_resolution(key, row)
    return row


def _text_similarity(a: str, b: str) -> float:
    """Similarity of two names after normalizing both. Kept local rather than
    reusing tags.similarity so a build comparing thousands of pairs doesn't
    evict the scanner's cached comparisons."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _artist_card(candidate: dict, token, *, prefer_hires: bool) -> dict | None:
    """One suggestion, ready to render: who, why, and what can be downloaded.
    None when Qobuz has nothing to offer for the name, so the card never
    appears with a download button that leads nowhere."""
    artist = resolve_artist(candidate["name"], token)
    if not artist:
        return None
    albums = artist_albums(
        artist["id"], artist["name"], token, prefer_hires=prefer_hires)
    if not albums:
        return None
    cover = next((a["cover"] for a in albums if a["cover"]), "")
    return {
        "name": artist["name"],
        "artist_id": artist["id"],
        "seeds": _seed_names(candidate),
        "score": candidate.get("score", 0.0),
        "cover": cover,
        "albums": albums,
    }


def _artist_cards(
    ranked: list[dict],
    token,
    want: int,
    *,
    prefer_hires: bool,
    progress=None,
) -> list[dict]:
    """Work down the ranking until `want` cards are filled. Names Qobuz can't
    serve are skipped and the next candidate promoted, bounded so a run of
    unresolvable names can't turn one build into hundreds of searches.

    `progress` is called with (checked, cards) after each candidate, so a build
    that has this as its only slow phase can still show it moving.
    """
    cards, attempts = [], 0
    for candidate in ranked:
        if len(cards) >= want or attempts >= want * 3:
            break
        attempts += 1
        card = _artist_card(candidate, token, prefer_hires=prefer_hires)
        if card:
            cards.append(card)
        if progress is not None:
            progress(attempts, list(cards))
    return cards


# One background build per feed. Not a queue job: Discover is a browse surface,
# and a job would give it a history, a retry and a place to get stuck.
SIMILAR = "similar"
TAGS = "tags"


def genre_feed_kind(tag: str) -> str:
    return f"genre:{normalize(tag) or tag}"


_builds: dict[str, dict] = {}
_builds_lock = threading.Lock()
_MAX_BUILD_STATES = 100
_MAX_ACTIVE_BUILDS = 8


def _new_build(library_sig: str = "") -> dict:
    return {"phase": "building", "stage": "library", "checked": 0, "total": 0,
            "error": "", "items": [], "started_at": time.time(),
            "finished_at": 0.0, "library_sig": library_sig}


def _catalogue_feed_signature(owned: Library, prefer_hires: bool) -> str:
    """Identity of a feed whose album editions follow the quality policy."""
    return f"{owned.signature}|prefer_hires:{int(prefer_hires)}"


def _publish(kind: str, **fields) -> None:
    with _builds_lock:
        build = _builds.get(kind)
        if build is not None:
            build.update(fields)
            if build.get("phase") != "building":
                _trim_builds_locked()


def _trim_builds_locked() -> None:
    """Bound finished in-memory feeds without dropping work still running."""
    excess = len(_builds) - _MAX_BUILD_STATES
    if excess <= 0:
        return
    finished = sorted(
        ((kind, build) for kind, build in _builds.items()
         if build.get("phase") != "building"),
        key=lambda pair: (
            pair[1].get("finished_at") or pair[1].get("started_at") or 0,
            pair[0],
        ),
    )
    for old_kind, _build in finished[:excess]:
        _builds.pop(old_kind, None)


def _snapshot(build: dict) -> dict:
    return dict(build, items=list(build["items"]))


def build_status(kind: str) -> dict | None:
    with _builds_lock:
        build = _builds.get(kind)
        return _snapshot(build) if build else None


def _run(kind: str, worker, args) -> None:
    try:
        worker(kind, *args)
    except LastfmKeyRejected:
        _publish(kind, phase="error", error="key", finished_at=time.time())
    except LastfmRateLimited:
        _publish(kind, phase="error", error="rate_limited",
                 finished_at=time.time())
    except LastfmUnavailable:
        _publish(kind, phase="error", error="unavailable",
                 finished_at=time.time())
    except (AuthLost, QobuzUnavailable, QobuzError) as e:
        vlog(f"discover {kind}: Qobuz unavailable ({e})")
        _publish(kind, phase="error", error="qobuz", finished_at=time.time())
    except Exception:
        logging.getLogger("qobuz_librarian").exception(
            "discover %s build failed", kind)
        _publish(kind, phase="error", error="other", finished_at=time.time())


def start_build(kind: str, worker, *args, library_sig: str = "") -> dict:
    """Start `kind` building unless it already is. Returns the state to show
    now, which for a build already under way is its progress so far."""
    with _builds_lock:
        build = _builds.get(kind)
        if build is not None and build["phase"] == "building":
            return _snapshot(build)
        if (build is not None and build["phase"] == "error"
                and build.get("library_sig", "") == library_sig
                and (time.time() - build["finished_at"]) < _RETRY_AFTER_ERROR):
            return _snapshot(build)
        if sum(
            candidate.get("phase") == "building"
            for candidate in _builds.values()
        ) >= _MAX_ACTIVE_BUILDS:
            _builds[kind] = {
                **_new_build(library_sig),
                "phase": "waiting",
                "error": "busy",
            }
            _trim_builds_locked()
            return _snapshot(_builds[kind])
        _builds[kind] = _new_build(library_sig)
        _trim_builds_locked()
        snapshot = _snapshot(_builds[kind])
    thread = threading.Thread(target=_run, args=(kind, worker, args),
                              name=f"discover-{kind}", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        _publish(kind, phase="error", error="other", finished_at=time.time())
        return build_status(kind) or snapshot
    return snapshot


def _similar_worker(
    kind: str,
    token,
    owned: Library,
    prefer_hires: bool,
    feed_signature: str,
) -> None:
    accumulated: dict[str, dict] = {}
    total = len(owned)
    _publish(kind, total=total)
    consecutive_failures = 0
    reached_lastfm = False
    for index, seed in enumerate(owned.seeds, start=1):
        key = cache.similar_key(normalize(seed) or seed)
        rows = cache.get_lastfm(key, cache.SIMILAR_TTL)
        if rows is None:
            try:
                rows = get_similar_artists(seed)
                cache.put_lastfm(key, rows)
                consecutive_failures = 0
                reached_lastfm = True
            except LastfmUnavailable:
                consecutive_failures += 1
                if (not reached_lastfm
                        or consecutive_failures >= _MAX_CONSECUTIVE_FAILURES):
                    raise
                rows = cache.get_lastfm(key, cache.SIMILAR_TTL,
                                        allow_stale=True) or []
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            match = float(row.get("match") or 0.0)
            acc = accumulated.setdefault(name, {"score": 0.0, "seeds": []})
            acc["score"] += match
            acc["seeds"].append((seed, match))
        _publish(kind, checked=index)
        if index % _PUBLISH_EVERY == 0 and index != total:
            partial = rank_candidates(accumulated, owned)
            _publish(
                kind,
                items=_artist_cards(
                    partial,
                    token,
                    _PARTIAL_CARDS,
                    prefer_hires=prefer_hires,
                ),
            )
    ranked = rank_candidates(accumulated, owned)
    # Looking every candidate up in Qobuz takes about as long again as the
    # Last.fm pass, so it gets its own counter rather than leaving the library
    # one sitting full.
    # The partial list is already on screen and the final one is the same
    # ranking, so it is held until the new one is at least as long: a card
    # count that counts down reads as suggestions being taken away.
    shown = (build_status(kind) or {}).get("items") or []
    _publish(kind, stage="catalogue", checked=0, total=_FEED_CARDS)

    def catalogue_progress(_attempts, found):
        # The partial cards were resolved through Qobuz too, so counting them
        # keeps the number and the list saying the same thing.
        listed = found if len(found) >= len(shown) else shown
        _publish(kind, checked=len(listed), items=listed)

    cards = _artist_cards(
        ranked,
        token,
        _FEED_CARDS,
        prefer_hires=prefer_hires,
        progress=catalogue_progress,
    )
    cache.put_feed(kind, cards, feed_signature)
    _publish(kind, phase="ready", items=cards, checked=len(cards),
             finished_at=time.time())


def ensure_similar_feed(token) -> dict:
    """What the Similar tab should show, building it if there is nothing usable
    to show yet."""
    owned = library()
    prefer_hires = bool(cfg.PREFER_HIRES)
    signature = _catalogue_feed_signature(owned, prefer_hires)
    view = feed_view(SIMILAR, signature)
    if view["phase"] in ("building", "ready"):
        return view
    start_build(
        SIMILAR,
        _similar_worker,
        token,
        owned,
        prefer_hires,
        signature,
        library_sig=signature,
    )
    return feed_view(SIMILAR, signature)


def _tags_worker(kind: str, token, owned: Library) -> None:
    """The tags that describe the library, from a sample of its artists. A
    sample rather than all of them: the chips only need the shape of the
    collection, and every artist asked is another request."""
    del token
    sample = sorted(owned.seeds, key=str.lower)[:_TAG_SAMPLE]
    weights: dict[str, float] = {}
    labels: dict[str, str] = {}
    _publish(kind, total=len(sample))
    consecutive_failures = 0
    reached_lastfm = False
    for index, seed in enumerate(sample, start=1):
        key = cache.tags_key(normalize(seed) or seed)
        rows = cache.get_lastfm(key, cache.TAGS_TTL)
        if rows is None:
            try:
                rows = get_artist_top_tags(seed)
                cache.put_lastfm(key, rows)
                consecutive_failures = 0
                reached_lastfm = True
            except LastfmUnavailable:
                consecutive_failures += 1
                if (not reached_lastfm
                        or consecutive_failures >= _MAX_CONSECUTIVE_FAILURES):
                    raise
                rows = cache.get_lastfm(key, cache.TAGS_TTL,
                                        allow_stale=True) or []
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not _tag_is_musical(name):
                continue
            folded = name.lower()
            labels.setdefault(folded, name)
            weights[folded] = weights.get(folded, 0.0) + float(row.get("count") or 0)
        _publish(kind, checked=index)
    ordered = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    chips = [labels[folded] for folded, _weight in ordered[:_TAG_CHIPS]]
    cache.put_feed(kind, chips, owned.signature)
    _publish(kind, phase="ready", items=chips, finished_at=time.time())


def _tag_is_musical(name: str) -> bool:
    """Whether a Last.fm tag describes music rather than the listener. Bare
    years and decades go too: the decade chips already do that from real Qobuz
    release dates."""
    text = str(name or "").strip()
    if not text or text.lower() in _TAG_DENYLIST:
        return False
    stripped = text.rstrip("sS")
    return not (stripped.isdigit() and 2 <= len(stripped) <= 4)


def ensure_library_tags(token) -> dict:
    owned = library()
    view = feed_view(TAGS, owned.signature)
    if view["phase"] in ("building", "ready"):
        return view
    start_build(TAGS, _tags_worker, token, owned,
                library_sig=owned.signature)
    return feed_view(TAGS, owned.signature)


def _genre_worker(
    kind: str,
    token,
    owned: Library,
    tag: str,
    prefer_hires: bool,
    feed_signature: str,
) -> None:
    key = cache.tag_albums_key(normalize(tag) or tag, 1)
    rows = cache.get_lastfm(key, cache.TAGS_TTL)
    if rows is None:
        rows = get_tag_top_albums(tag, limit=_GENRE_FETCH)
        cache.put_lastfm(key, rows)
    _publish(kind, total=len(rows))
    cards = []
    for index, row in enumerate(rows, start=1):
        _publish(kind, checked=index)
        if len(cards) >= GENRE_CARDS:
            break
        album = resolve_album(
            row.get("artist") or "",
            row.get("title") or "",
            token,
            prefer_hires=prefer_hires,
        )
        if not album:
            continue
        # Same short-release rule as the artist rows; an unknown track count
        # passes rather than punishing bad metadata.
        if 0 < (album.get("tracks") or 0) < cfg.MISSING_ALBUMS_MIN_TRACKS:
            continue
        if _owned_on_disk(album):
            continue
        cards.append(album)
        if index % _PUBLISH_EVERY == 0:
            _publish(kind, items=list(cards))
    cache.put_feed(kind, cards, feed_signature)
    _publish(kind, phase="ready", items=cards, finished_at=time.time())


def ensure_genre_feed(token, tag: str) -> dict:
    owned = library()
    prefer_hires = bool(cfg.PREFER_HIRES)
    signature = _catalogue_feed_signature(owned, prefer_hires)
    kind = genre_feed_kind(tag)
    view = _without_owned_albums(feed_view(kind, signature))
    if view["phase"] in ("building", "ready"):
        return view
    start_build(
        kind,
        _genre_worker,
        token,
        owned,
        tag,
        prefer_hires,
        signature,
        library_sig=signature,
    )
    return _without_owned_albums(feed_view(kind, signature))


def _owned_on_disk(album: dict) -> bool:
    """Whether this record already has a folder in the library, using the same
    resolver the download and scan paths use."""
    try:
        return catalog.find_album_dir_filesystem({
            "title": album.get("title") or "",
            "version": album.get("version") or "",
            "artist": {"name": album.get("artist") or ""},
            "release_date_original": f"{album.get('year') or ''}-01-01",
        }) is not None
    except Exception:
        return False


def _without_owned_albums(view: dict) -> dict:
    if view["phase"] != "building":
        view["items"] = [row for row in view["items"] if not _owned_on_disk(row)]
    return view


FAVOURITES = "favourites"


def favourites_feed_kind(token) -> str:
    """Keep account-private saved albums out of another account's feed."""
    generation = token_credential_generation(token)
    return f"{FAVOURITES}:{generation}" if generation else FAVOURITES


def _favourites_worker(kind: str, token, owned: Library) -> None:
    """The albums starred on Qobuz that are not in the library yet. No Last.fm
    here: these are already Qobuz records, so there is nothing to resolve."""
    rows = get_user_favorites(token)
    _publish(kind, total=len(rows))
    cards = []
    for index, album in enumerate(rows, start=1):
        card = _album_row(album)
        if not _owned_on_disk(card):
            cards.append(card)
        _publish(kind, checked=index)
        if index % _PUBLISH_EVERY == 0:
            _publish(kind, items=list(cards))
    cache.put_feed(kind, cards, owned.signature)
    _publish(kind, phase="ready", items=cards, finished_at=time.time())


def ensure_favourites_feed(token) -> dict:
    owned = library()
    kind = favourites_feed_kind(token)
    view = _without_owned_albums(
        feed_view(kind, owned.signature, ttl=cache.FAVOURITES_TTL))
    if view["phase"] in ("building", "ready"):
        return view
    start_build(kind, _favourites_worker, token, owned,
                library_sig=owned.signature)
    return _without_owned_albums(
        feed_view(kind, owned.signature, ttl=cache.FAVOURITES_TTL))


def search_feed_kind(query: str) -> str:
    return f"search:{normalize(query) or query}"


def _search_worker(
    kind: str,
    token,
    owned: Library,
    query: str,
    prefer_hires: bool,
    feed_signature: str,
) -> None:
    """Artists like the one typed in, minus the ones already owned. Built on a
    thread like the others: one Last.fm call is quick, but resolving a dozen
    names on Qobuz is not, and a request that sat there for half a minute would
    read as a hung page."""
    key = cache.similar_key(normalize(query) or query)
    rows = cache.get_lastfm(key, cache.SIMILAR_TTL)
    if rows is None:
        rows = get_similar_artists(query)
        cache.put_lastfm(key, rows)
    accumulated = {}
    for row in rows:
        candidate = str(row.get("name") or "").strip()
        if not candidate:
            continue
        match = float(row.get("match") or 0.0)
        accumulated[candidate] = {"score": match, "seeds": [(query, match)]}
    ranked = rank_candidates(accumulated, owned)
    _publish(kind, total=len(ranked))
    cards = _artist_cards(
        ranked, token, SEARCH_CARDS, prefer_hires=prefer_hires,
        progress=lambda checked, found: _publish(kind, checked=checked,
                                                 items=found))
    cache.put_feed(kind, cards, feed_signature)
    _publish(kind, phase="ready", items=cards, finished_at=time.time())


def ensure_search_feed(token, query: str) -> dict:
    text = str(query or "").strip()
    if not text:
        return {"phase": "idle", "checked": 0, "total": 0, "error": "",
                "items": [], "built_at": 0.0, "stale": False}
    owned = library()
    prefer_hires = bool(cfg.PREFER_HIRES)
    signature = _catalogue_feed_signature(owned, prefer_hires)
    kind = search_feed_kind(text)
    view = feed_view(kind, signature)
    if view["phase"] in ("building", "ready"):
        return view
    start_build(
        kind,
        _search_worker,
        token,
        owned,
        text,
        prefer_hires,
        signature,
        library_sig=signature,
    )
    return feed_view(kind, signature)


def feed_view(kind: str, library_sig: str, *,
              ttl: float = cache.FEED_TTL) -> dict:
    """One answer for the page: the items, where they came from, and whether
    anything is still being worked on.

    A build under way wins over the saved copy, because its partial results are
    newer. A build that failed falls back to the saved copy however old it is,
    marked stale, so an outage shows the suggestions from last time rather than
    an empty page.
    """
    view = {"phase": "idle", "stage": "library", "checked": 0, "total": 0,
            "error": "", "items": [], "built_at": 0.0, "stale": False}
    build = build_status(kind)
    if build and build["phase"] == "building":
        if build.get("library_sig") == library_sig:
            view.update(phase="building", stage=build["stage"],
                        checked=build["checked"], total=build["total"],
                        items=build["items"])
        else:
            view.update(phase="waiting", error="busy")
        return view
    if (build and build["phase"] == "waiting"
            and build.get("library_sig") == library_sig):
        view.update(phase="waiting", error="busy")
        return view
    if (build and build["phase"] == "ready"
            and build.get("library_sig") == library_sig
            and (time.time() - build["finished_at"]) <= ttl):
        view.update(phase="ready", items=build["items"],
                    total=build["total"], checked=build["checked"],
                    built_at=build["finished_at"])
        return view
    saved = cache.get_feed(kind)
    if build and build["phase"] == "error":
        view.update(phase="error", error=build["error"], items=build["items"])
        if not view["items"] and saved:
            view.update(items=saved["payload"], built_at=saved["built_at"],
                        stale=True)
        return view
    if saved and saved["library_sig"] == library_sig \
            and (time.time() - saved["built_at"]) <= ttl:
        view.update(phase="ready", items=saved["payload"],
                    built_at=saved["built_at"])
    return view


def _reset_for_tests() -> None:
    global _library_cache, _library_cache_at
    with _builds_lock:
        _builds.clear()
    with _library_lock:
        _library_cache = None
        _library_cache_at = 0.0
