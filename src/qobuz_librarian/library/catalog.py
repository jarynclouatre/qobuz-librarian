"""Album/artist catalog matching and track-presence detection.

Behaviour you should not change without understanding the consequence:

- find_album_dir_filesystem: exact-path fast path first, then fuzzy scan.
  Artist-variant expansion covers "The X" / "X" / comma-prefix forms.
  Multi-artist folder detection (_MULTI_ARTIST_SEPS) expands search dirs.
- compute_missing: 3-layer matching chain in order -
    1. ISRC - authoritative recording identity
    2. (disc, normalized title)
    3. edition-stripped variants on BOTH sides
  A track is "present" if ANY layer matches. Order matters: ISRC is
  authoritative; title-match is a fallback. Reordering causes false
  re-downloads of already-owned tracks. compute_missing and
  find_extras_in_existing share one matcher so the two never drift.
- album_year(): prefers release_date_original; falls back to released_at
  parsed in UTC. Local-timezone parsing flipped years for late-night-UTC
  releases.
- predicted_album_paths uses a list, not a set, to preserve deterministic
  candidate order across runs (set hash iteration is non-deterministic).
"""
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.api import album_cache
from qobuz_librarian.api import search as qobuz_search
from qobuz_librarian.api.auth import QobuzError, QobuzUnavailable
from qobuz_librarian.api.search import get_album, search_albums
from qobuz_librarian.integrations import rip
from qobuz_librarian.library.scanner import (
    SYSTEM_FOLDER_NAMES,
    _list_artist_subdirs_cached,
    is_library_system_folder,
    iter_tree_no_symlinks,
    read_album_dir,
)
from qobuz_librarian.library.tags import (
    beets_sanitize,
    canonical_recording_id,
    canonical_track_slot,
    canonical_track_title,
    differs_by_album_variant,
    folder_title_forms,
    known_year,
    normalize,
    similarity,
    strip_album_decorations,
    strip_edition_suffix,
    strip_leading_article,
    strip_year_decoration,
)
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog

# ── Multi-artist folder detection ─────────────────────────────────────────────
# Beets writes ALBUMARTIST = "X, Y" for multi-artist releases.
_MULTI_ARTIST_SEPS = (
    ", ", " & ", " and ", " feat. ", " feat ",
    " ft. ", " ft ", " with ", " x ", " vs. ", " vs ",
)
# Migration uses comma-space ONLY - other separators appear in real band
# names ('Bob Marley & The Wailers') and auto-migrating those would
# silently fold entire artists' catalogs into a lead-member folder.
_MIGRATION_SEPS = (", ",)
_PRIMARY_ARTIST_SEPS = (", ", " & ", " and ", " feat. ", " feat ", " ft. ", " ft ")


def _primary_artist_of(qartist):
    """Return the primary (first) name from a multi-artist string.

    Qobuz returns the same album with different artist-string formats
    depending on which edition is queried: "Jay Z and Kanye West"
    (album-level) vs. "Jay Z, Kanye West" (track-level). The folder lookup
    needs a canonical primary so it finds the on-disk folder regardless of
    which form Qobuz happened to return. Only a lookup: a split name is never
    a folder to create, since band names carry the same separators.
    """
    if not qartist:
        return qartist
    s = qartist.strip()
    for sep in _PRIMARY_ARTIST_SEPS:
        if sep in s:
            return s.split(sep, 1)[0].strip()
    return s


def _has_separator_match(folder_name, qartist, seps):
    if not folder_name or not qartist:
        return False
    # The folder was written by beets, so a special-char artist ("AC/DC")
    # lives in "AC_DC, …"; sanitise the Qobuz name to the same form first.
    qa = beets_sanitize(qartist).strip()
    fl = folder_name.strip()
    if len(fl) <= len(qa):
        return False
    if fl[:len(qa)].lower() != qa.lower():
        return False
    rest = fl[len(qa):]
    return any(rest.startswith(sep) for sep in seps)


def _is_multi_artist_subset(folder_name, qartist):
    """True iff folder_name is 'qartist<sep><other>' for a detection sep."""
    return _has_separator_match(folder_name, qartist, _MULTI_ARTIST_SEPS)


def _is_migration_candidate(folder_name, qartist):
    """True iff the folder is safe to migrate (comma-space form only)."""
    return _has_separator_match(folder_name, qartist, _MIGRATION_SEPS)


# Lookups over the cached MUSIC_ROOT listing, kept while that listing is the
# current one: one artist's thousand albums ask the same questions thousands
# of times.
_root_lookups: tuple = (None, {})


def _root_dirs_matching(key, match):
    """MUSIC_ROOT children other than NAS and Windows system folders for
    which ``match(name)`` holds, remembered under ``key`` for as long as the
    cached listing stands."""
    global _root_lookups
    root_dirs = _list_artist_subdirs_cached(config.MUSIC_ROOT)
    listing, found = _root_lookups
    if listing is not root_dirs:
        found = {}
        _root_lookups = (root_dirs, found)
    dirs = found.get(key)
    if dirs is None:
        dirs = found[key] = [d for d in root_dirs
                             if d.name not in SYSTEM_FOLDER_NAMES
                             and match(d.name)]
    return list(dirs)


def _listed_root_dir(name):
    """The MUSIC_ROOT folder ``name`` opens, under the name the folder lists
    it by, or None. A case-insensitive disk opens a folder under any case,
    and a path built from another case would be a second name for it."""
    folded = name.casefold()
    try:
        listed = _root_dirs_matching(
            ("case", folded), lambda entry: entry.casefold() == folded)
        exact = next((d for d in listed if d.name == name), None)
        if exact is not None or not listed:
            return exact
        wanted = (config.MUSIC_ROOT / name).stat()
        return next((d for d in listed
                     if os.path.samestat(d.stat(), wanted)), None)
    except OSError:
        return None


def _find_multi_artist_dirs(qartist):
    """Return MUSIC_ROOT children whose name is 'qartist<sep><other>'.
    Uses the scan cache so artist-mode runs don't re-iterdir."""
    if not qartist or not config.MUSIC_ROOT.exists():
        return []
    return _root_dirs_matching(
        ("multi", qartist), lambda name: _is_multi_artist_subset(name, qartist))


# ── Path utilities ────────────────────────────────────────────────────────────
def _paths_equal(a: Path, b: Path) -> bool:
    """Best-effort path equality. samefile() is authoritative but raises when a
    path doesn't exist; fall back to comparing resolved (symlink-collapsed)
    forms, then to a plain string normalization."""
    try:
        return a.samefile(b)
    except (OSError, TypeError, ValueError):
        pass
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return os.path.normpath(str(a)) == os.path.normpath(str(b))


_DECORATION_YEAR_RE = re.compile(
    r"[(\[](\d{4})[)\]]\s*$"      # trailing '(2010)' / '[2010]'
    r"|^\[(\d{4})\]\s+"          # leading '[2010] Title'
    r"|^(\d{4})\s*[-–]\s+"      # leading '2010 - Title'
)


def _decoration_year(name):
    """The 4-digit year a folder name uses as a decoration ('' if none).

    A bare year that is itself the title ('1984', '2112') is not a decoration;
    a year in a clear slot - trailing '(2010)'/'[2010]', or a leading '[2010] '
    or '2010 - ' prefix - is. Mirrors tags.py's year-stripping rules."""
    m = _DECORATION_YEAR_RE.search(name or "")
    return next((g for g in m.groups() if g), "") if m else ""


def _is_split_album_merge(album_dir, post_dir, qartist):
    """True when album_dir and post_dir are the SAME album split across two
    folders, safe to consolidate into post_dir (where beets just filed the new
    tracks). The safe automatic shape is multi-artist: existing tracks under
    'Primary, Other/Album', with new ones under 'Primary/Album'.
    """
    if album_dir is None or post_dir is None:
        return False
    try:
        if not album_dir.exists() or not post_dir.exists():
            return False
    except OSError:
        return False
    if _paths_equal(post_dir, album_dir) or not qartist:
        return False
    a = normalize(strip_year_decoration(album_dir.name))
    if not a or a != normalize(strip_year_decoration(post_dir.name)):
        return False
    # strip_year_decoration also strips a "(Live 2008)"/"(Demos 1992)" marker,
    # so a distinct live/demo release can normalize equal to the studio album
    # above.
    _an, _pn = normalize(album_dir.name), normalize(post_dir.name)
    if _an != _pn and (differs_by_album_variant(_an, _pn)
                       or differs_by_album_variant(_pn, _an)):
        return False
    ay, py = _decoration_year(album_dir.name), _decoration_year(post_dir.name)
    if ay and py and ay != py:
        return False
    if (_is_multi_artist_subset(album_dir.parent.name, qartist)
            and not _is_multi_artist_subset(post_dir.parent.name, qartist)):
        return True
    return False


# ── Album dir resolution ──────────────────────────────────────────────────────
def predicted_album_paths(qobuz_album, *, exact_edition=False):
    """Build an ordered list of candidate paths for a Qobuz album.

    List (not set): set iteration order is hash-based, causing non-deterministic
    candidate resolution across runs.
    """
    artist_raw = (qobuz_album.get("artist") or {}).get("name") or ""
    artist = beets_sanitize(artist_raw)
    title  = beets_sanitize(qobuz_album.get("title") or "")

    candidates = []
    if not artist or not title:
        return candidates

    artist_dirs = [_listed_root_dir(artist) or config.MUSIC_ROOT / artist]
    artist_dirs.extend(_find_multi_artist_dirs(artist_raw))
    # Qobuz returns "Jay Z and Kanye West" on some editions while the folder
    # is "Jay Z, Kanye West"; searching multi-artist dirs under the primary
    # name finds it, so the migration that follows isn't starved of a source dir.
    primary_raw = _primary_artist_of(artist_raw)
    if primary_raw and primary_raw != artist_raw:
        for d in _find_multi_artist_dirs(primary_raw):
            if d not in artist_dirs:
                artist_dirs.append(d)

    years = []
    # The year as listed: a folder Beets named from a placeholder date
    # still carries it.
    yr = _listed_year(qobuz_album)
    if yr:
        years.append(yr)

    # Also look under the de-editioned title, in case the user's folder drops
    # the tag ('Album (Deluxe)' → also 'Album').
    bare_titles = [title]
    stripped = strip_album_decorations(title)
    if not exact_edition and stripped and stripped != title:
        bare_titles.append(stripped)

    for ad in artist_dirs:
        for bt in bare_titles:
            for year in years:
                # Common beets path-template forms: trailing-paren, leading
                # [year], leading bare year.
                candidates.append(ad / f"{bt} ({year})")
                candidates.append(ad / f"[{year}] {bt}")
                candidates.append(ad / f"{year} - {bt}")
            candidates.append(ad / bt)

    if artist_raw.lower().strip() in (
            "various artists", "various", "va", "compilations", "soundtrack"):
        various = (_listed_root_dir("Various Artists")
                   or config.MUSIC_ROOT / "Various Artists")
        for bt in bare_titles:
            for year in years:
                candidates.append(various / f"{bt} ({year})")
                candidates.append(various / f"[{year}] {bt}")
                candidates.append(various / f"{year} - {bt}")
            candidates.append(various / bt)

    return candidates


def _predicted_album_dir(qobuz_album, *, exact_edition=False):
    candidates = predicted_album_paths(qobuz_album, exact_edition=exact_edition)
    vlog(f"predicted {plural(len(candidates), 'candidate path')}")

    _parent_kid_names: dict = {}
    for c in candidates:
        p_str = str(c.parent)
        if p_str not in _parent_kid_names:
            try:
                _parent_kid_names[p_str] = {
                    k.name for k in _list_artist_subdirs_cached(c.parent)
                }
            except (FileNotFoundError, NotADirectoryError):
                # A predicted artist dir can be a stray regular FILE with that
                # name; that's "no albums here", not a scan-stopping error.
                _parent_kid_names[p_str] = set()
        if c.name in _parent_kid_names[p_str]:
            vlog(f"exact match: {c}")
            return c
    return None


def find_album_dir_filesystem(qobuz_album, *, exact_edition=False):
    """Resolve a Qobuz album dict to its on-disk directory.

    Fast path: check predicted_album_paths() against cached subdir listings.
    Fuzzy path: expand artist variants ('The X' / 'X' / comma-prefix) and
    scan each parent dir for the best-scoring album folder. exact_edition
    keeps the version label and requires a title match, ignoring folder years.
    """
    title = qobuz_album.get("title") or ""
    version = (qobuz_album.get("version") or "").strip()
    labelled = bool(version) and version.casefold() not in title.casefold()
    if exact_edition:
        if labelled:
            qobuz_album = {**qobuz_album, "title": f"{title} ({version})"}
    elif labelled:
        # A folder named for this version is its own, even beside the plain
        # one: "Lost Boys (Acoustic)" is never filed in "Lost Boys". One of
        # another year gives way to the folder the release's download got.
        own = find_album_dir_filesystem(qobuz_album, exact_edition=True)
        if own is not None and (
                _decoration_year(own.name) in ("", _listed_year(qobuz_album))
                or _predicted_album_dir(qobuz_album) is None):
            return own
    predicted = _predicted_album_dir(qobuz_album, exact_edition=exact_edition)
    if predicted is not None:
        return predicted

    artist = (qobuz_album.get("artist") or {}).get("name") or ""
    title  = qobuz_album.get("title") or ""

    if not artist:
        return None

    artist_variants = [artist]
    if artist.lower().startswith("the "):
        artist_variants.append(artist[4:])
    else:
        artist_variants.append("The " + artist)
    if "," in artist:
        artist_variants.append(artist.split(",")[0].strip())

    search_dirs = []
    seen_dirs: set = set()

    def _push(p):
        key = str(p)
        if key in seen_dirs:
            return
        seen_dirs.add(key)
        search_dirs.append(p)

    for av in artist_variants:
        # Folders only: a stray regular file with an artist's name would
        # blow up the subdir listing below with NotADirectoryError.
        ad = _listed_root_dir(beets_sanitize(av))
        if ad is not None:
            _push(ad)
        for ma in _find_multi_artist_dirs(av):
            _push(ma)

    # Diacritics often differ between Qobuz and disk ('Beyoncé' vs 'Beyonce',
    # 'Motörhead' vs 'Motorhead'); the exact .exists() checks above miss
    # those, so also take any artist folder whose ASCII-folded name matches.
    artist_norms = {normalize(av) for av in artist_variants if normalize(av)}
    if artist_norms and config.MUSIC_ROOT.exists():
        for d in _root_dirs_matching(
                ("folded", frozenset(artist_norms)),
                lambda name: normalize(name) in artist_norms):
            _push(d)

    global_best, global_best_score = None, 0.0
    norm_title = normalize(strip_album_decorations(title))
    edition_title = canonical_track_title(beets_sanitize(title))
    # Folder names often leave out a bracketed "(Live)" or the version, so
    # only the rest of the title has to agree with the folder name.
    title_words = _other_release_words(_TRAILING_BRACKETS_RE.sub("", title))
    release_words = _other_release_words(f"{title} {qobuz_album.get('version') or ''}")
    for artist_dir in search_dirs:
        vlog(f"scanning artist dir: {artist_dir}")
        subdirs = _list_artist_subdirs_cached(artist_dir)
        if not subdirs:
            continue

        stripped_title = strip_album_decorations(title)
        # Evaluate every subdir that clears both gates, not just the single
        # top scorer: a prefix-only high scorer that fails the coverage gate
        # (studio 'The North Borders' scoring 0.79 against the live 'The North
        # Borders Tour - Live') must not mask a lower-scored folder in the same
        # artist dir that is the real match.
        for d in subdirs:
            if exact_edition:
                folder_title = canonical_track_title(_DECORATION_YEAR_RE.sub("", d.name))
                if edition_title and folder_title == edition_title:
                    return d
                continue
            folder_words = _other_release_words(d.name)
            for dir_bare in folder_title_forms(d.name, artist_dir.name, artist):
                score = similarity(dir_bare, stripped_title)
                if score < config.FUZZY_DIR_THRESH or score <= global_best_score:
                    continue
                # Real edition variants normalize to the same string, so require
                # the titles to be close in length - that keeps an extra word
                # ('Live', 'Tour') from resolving to the base album's folder on
                # prefix alone.
                norm_dir = normalize(dir_bare)
                coverage = (min(len(norm_dir), len(norm_title))
                            / max(len(norm_dir), len(norm_title))
                            if norm_dir and norm_title else 0.0)
                if coverage < config.FUZZY_DIR_MIN_COVERAGE:
                    vlog(f"rejected {d.name}: score {score:.2f} but length "
                         f"coverage {coverage:.2f} < {config.FUZZY_DIR_MIN_COVERAGE}")
                    continue
                if (_numbers_differ(dir_bare, stripped_title)
                        or title_words - folder_words
                        or folder_words - release_words):
                    vlog(f"rejected {d.name}: another numbered, live or remix release")
                    continue
                global_best, global_best_score = d, score
                vlog(f"fuzzy match: {d}  (score {score:.2f})")
    return global_best


_TRAILING_BRACKETS_RE = re.compile(r"(?:\s*(?:\([^()]*\)|\[[^\[\]]*\]))+\s*$")
_DISC_FOLDER_RE = re.compile(r"^(?:disc|cd)\s*\d+", re.IGNORECASE)


def track_signatures_for_album_dirs(album_dirs):
    """Return tag signatures for staged/import-candidate FLACs.

    These signatures survive beets moving/renaming files, so callers can find
    the imported folder when Qobuz's artist/title prediction misses.
    """
    signatures = []
    seen = set()
    for album_dir in album_dirs or []:
        if album_dir is None:
            continue
        root = Path(album_dir)
        try:
            flacs = sorted(p for p in root.rglob("*.flac") if p.is_file())
        except OSError:
            continue
        for fp in flacs:
            sig = rip._flac_signature(fp)
            if sig is None or sig in seen:
                continue
            signatures.append(sig)
            seen.add(sig)
    return signatures


def _artist_dirs_for_track_signatures(signatures):
    artist_names = sorted({str(sig[0]).strip() for sig in signatures
                           if sig and sig[0]})
    if not artist_names:
        return []

    dirs = []
    seen = set()

    def _push(path):
        try:
            key = str(path.resolve(strict=False))
        except OSError:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        dirs.append(path)

    for artist in artist_names:
        direct = _listed_root_dir(beets_sanitize(artist))
        if direct is not None:
            _push(direct)

    wanted_norms = {normalize(a) for a in artist_names if normalize(a)}
    if wanted_norms and config.MUSIC_ROOT.exists():
        for d in _root_dirs_matching(
                ("folded", frozenset(wanted_norms)),
                lambda name: normalize(name) in wanted_norms):
            _push(d)
    return dirs


def find_album_dir_by_track_signatures(signatures, *, expected_dir=None):
    """Locate imported tracks, within their pinned folder when supplied."""
    wanted = {tuple(sig) for sig in signatures or [] if sig}
    if not wanted:
        return None

    matches = {}
    roots = [expected_dir] if expected_dir is not None else _artist_dirs_for_track_signatures(wanted)
    for artist_dir in roots:
        try:
            flacs = sorted(p for p in artist_dir.rglob("*.flac") if p.is_file())
        except OSError:
            continue
        for fp in flacs:
            sig = rip._flac_signature(fp)
            if sig not in wanted:
                continue
            album_dir = fp.parent
            if _DISC_FOLDER_RE.match(album_dir.name):
                album_dir = album_dir.parent
            matches.setdefault(album_dir, set()).add(sig)

    complete = [
        album_dir
        for album_dir, matched in matches.items()
        if matched == wanted
    ]
    if expected_dir is not None:
        return expected_dir if expected_dir in complete else None
    return complete[0] if len(complete) == 1 else None


def find_existing_tracks(qobuz_album, *, album_dir=None):
    """Return (track_list, album_dir_or_None) by reading the filesystem.

    track_list is empty when no album dir is found - surfaces as
    '0 of N present' so the user can spot a folder-naming mismatch.

    Pass ``album_dir`` when the caller already resolved it - the artist
    walk and the single-album download path both resolve once via
    ``find_album_dir_filesystem`` for a guardrail and would otherwise
    re-resolve here, doubling the cached-subdir-listing work per album.
    """
    if album_dir is None:
        album_dir = find_album_dir_filesystem(qobuz_album)
    if album_dir is None:
        return [], None
    return read_album_dir(album_dir), album_dir


# ── Track-presence detection ──────────────────────────────────────────────────
def _norm_isrc(value):
    """Canonical ISRC for identity comparison. A blank or whitespace-only tag
    must collapse to '' so two un-identified tracks aren't treated as the same
    recording (which would hide a real gap, or a bonus track before a wipe)."""
    return (value or "").strip().replace("-", "").upper()


def _track_key(title_raw, disc, isrc, *, disc_scoped=True):
    """Identity keys for one track: ISRC plus the (optionally disc-scoped) title
    in both its full and edition-stripped forms. Titles are compared
    normalized (ASCII-folded, punctuation dropped).
    """
    scope = disc if disc_scoped else None

    def _scoped(text):
        norm = canonical_track_title(text)
        return (scope, norm) if norm else None

    title_raw = title_raw or ""
    return {
        "isrc": _norm_isrc(isrc),
        "title": _scoped(title_raw),
        "stripped": _scoped(strip_edition_suffix(title_raw)),
    }


# Strongest identity first: an ISRC-identifiable track claims its exact slot
# before a weaker title match can consume it.
_MATCH_LAYERS = ("isrc", "title", "stripped")


def _keys_match(a, b, layer):
    """Whether two track keys agree at one matching layer. Symmetric in a/b;
    the 'stripped' layer pairs an edition-stripped title on either side, so
    'Foo (LP Version)' matches a bare 'Foo' and the reverse."""
    if layer == "isrc":
        return bool(a["isrc"]) and a["isrc"] == b["isrc"]
    if layer == "title":
        return a["title"] is not None and a["title"] == b["title"]
    return any(x is not None and x == y for x, y in (
        (a["title"], b["stripped"]),
        (a["stripped"], b["title"]),
        (a["stripped"], b["stripped"]),
    ))


def _pair_indices(claim_keys, slot_keys):
    """One-to-one match claim_keys against slot_keys, strongest layer first.
    Returns a list over claim_keys of the slot index each claimed (None =
    unclaimed).
    """
    claim_isrcs = {k["isrc"] for k in claim_keys if k["isrc"]}
    slot_isrcs = {k["isrc"] for k in slot_keys if k["isrc"]}
    used = [False] * len(slot_keys)
    claimed = [None] * len(claim_keys)
    for layer in _MATCH_LAYERS:
        for ci, ck in enumerate(claim_keys):
            if claimed[ci] is not None:
                continue
            if layer != "isrc" and ck["isrc"] and ck["isrc"] in slot_isrcs:
                continue
            for si, sk in enumerate(slot_keys):
                if used[si] or not _keys_match(ck, sk, layer):
                    continue
                if layer != "isrc" and sk["isrc"] and sk["isrc"] in claim_isrcs:
                    continue
                used[si] = True
                claimed[ci] = si
                break
    return claimed


def _pair_tracks(claim_keys, slot_keys):
    """Bool list over claim_keys marking which claimed a slot (see
    _pair_indices for the matching rules)."""
    return [si is not None for si in _pair_indices(claim_keys, slot_keys)]


def _disc_count(tracks, field):
    return len({(t.get(field, 1) or 1) for t in tracks})


def _disc_scoped_match(qobuz_tracks, existing_tracks):
    """Scope title matching by disc only when BOTH sides span multiple discs.

    If either side is entirely on disc 1 - a single-disc album, or a flat folder
    whose tracks carry no DISCNUMBER tag and so all default to 1 - disc scoping
    can only invent false gaps (a later-disc Qobuz track never finding its
    disc-1 file). Matching disc-agnostically then is safe: one-to-one pairing
    still keeps two same-titled tracks from collapsing onto a single file."""
    return (_disc_count(qobuz_tracks, "media_number") > 1
            and _disc_count(existing_tracks, "discnumber") > 1)


def _qobuz_keys(qobuz_tracks, disc_scoped=True):
    # Qobuz keeps "Live" or "Mix Minus Piano" in the version field; the file's
    # title tag carries it, so compare the same full title.
    return [_track_key(_release_title(qt), qt.get("media_number", 1) or 1,
                       qt.get("isrc"), disc_scoped=disc_scoped)
            for qt in qobuz_tracks]


def _existing_keys(existing_tracks, disc_scoped=True):
    return [_track_key(et.get("title"), et.get("discnumber", 1) or 1,
                       et.get("isrc"), disc_scoped=disc_scoped)
            for et in existing_tracks]


def compute_missing(qobuz_tracks, existing_tracks):
    """Split qobuz_tracks into (missing, present) against existing_tracks.

    A Qobuz track is present when an on-disk track matches it on ISRC,
    normalized title, or edition-stripped title - in that priority. Matching is
    one-to-one (see _pair_tracks), so a duplicate title surfaces as a real gap
    instead of being masked by a single file.
    """
    disc_scoped = _disc_scoped_match(qobuz_tracks, existing_tracks)
    claimed = _pair_tracks(_qobuz_keys(qobuz_tracks, disc_scoped),
                           _existing_keys(existing_tracks, disc_scoped))
    missing = [qt for i, qt in enumerate(qobuz_tracks) if not claimed[i]]
    present = [qt for i, qt in enumerate(qobuz_tracks) if claimed[i]]
    return missing, present


def unstreamable(track) -> bool:
    """Whether Qobuz lists the track but will not serve it, so no download
    can fetch it."""
    return track.get("streamable") is False


def _release_title(record):
    title = (record.get("title") or "").strip()
    version = (record.get("version") or "").strip()
    work = (record.get("work") or "").strip()
    if version and version not in title:
        title = f"{title} ({version})"
    if work and work not in title:
        title = f"{work}: {title}"
    return title


_VERSION_LABEL_RE = re.compile(
    r"\b(?:mix|remix|remaster(?:ed)?|mono|stereo|live|acoustic|instrumental|"
    r"demo|take|edit|version|session|edition|deluxe|expanded|anniversary|reissue)\b",
    re.IGNORECASE,
)
_RECORDING_WORD_RE = re.compile(
    r"mix|remaster|mono|stereo|live|acoustic|instrumental|demo|take|session|edit(?!ion)")
_PACKAGING_LABEL_RE = re.compile(
    r"(?:album|anniversary|bonus|collectors|deluxe|digital|edition|expanded|legacy|limited|lp|"
    r"pa|special|split|standard|super|the|tracks?|uk|us|version|"
    r"[0-9]+(?:st|nd|rd|th)?)+")
_TITLE_BRACKET_RE = re.compile(
    r"\s*(?:\(((?:[^()]|\([^()]*\))*)\)|\[((?:[^\[\]]|\[[^\[\]]*\])*)\])\s*$")
_RELEASE_TYPE_RE = re.compile(r"[\s:(\[\-\u2013\u2014]+(?:EP|E\.P\.|Single)[)\]]?$", re.IGNORECASE)
_TITLE_SUFFIX_RE = re.compile(r"(?:\s[-\u2013\u2014]\s+|:\s*)(.+)\s*$")


def _label_parts(label):
    return {canonical_track_title(part) for part in re.split(r"\s*[/;]\s*", label)} - {""}


def _edition_title_parts(title, version="", *, folder=False, known_versions=()):
    title = (title or "").strip()
    labels, seen = set(), set()
    if version:
        seen.add(canonical_track_title(version))
        labels |= _label_parts(version)
    if folder:
        title = _DECORATION_YEAR_RE.sub("", title).strip()
    while match := _TITLE_BRACKET_RE.search(title) or _TITLE_SUFFIX_RE.search(title):
        label = next(part for part in match.groups() if part is not None)
        key = canonical_track_title(label)
        if re.fullmatch(r"Qobuz [a-zA-Z0-9]+", label):
            title = title[:match.start()].strip()
            continue
        if key not in seen and key not in known_versions and not _VERSION_LABEL_RE.search(label):
            break
        seen.add(key)
        labels |= _label_parts(label)
        title = title[:match.start()].strip()
    return canonical_track_title(beets_sanitize(title)), frozenset(labels)


def edition_album_title(album, *, separate=False):
    title = _release_title(album)
    if album.get("version") or _edition_title_parts(title)[1]:
        return title
    if separate:
        label = "Explicit" if album.get("parental_warning") else f"Qobuz {album.get('id')}"
        return f"{title} ({label})"
    return None


def _recording_labels(labels):
    return {label for label in labels if not _PACKAGING_LABEL_RE.fullmatch(label)}


def _recording_kinds(labels):
    return {word for label in labels for word in _RECORDING_WORD_RE.findall(label)}


def _version_in_title(track):
    version = (track.get("version") or "").strip()
    title = track.get("title") or ""
    return bool(version
                and re.search(rf"(?<!\w){re.escape(version)}(?!\w)", title, re.IGNORECASE)
                and canonical_track_title(version) in _edition_title_parts(title)[0])


def _edition_pairs(album, existing, folder, *, album_folder=None):
    """Pair Qobuz tracks with files of this edition. A shared ISRC is the same
    recording; without one, the title and its mix or master labels decide."""
    tracks = (album.get("tracks") or {}).get("items") or []
    album_key, album_version = _edition_title_parts(
        _release_title(album), album.get("version"), folder=album_folder is not None)
    if not album_version and album_folder:
        _, album_version = _edition_title_parts(str(album_folder), folder=True)
    known_versions = album_version | frozenset(
        canonical_track_title(t["version"]) for t in tracks if t.get("version"))
    folder_key, folder_version = _edition_title_parts(
        folder.name if folder is not None else "", folder=True, known_versions=known_versions)
    local_albums = [_edition_title_parts(t.get("album"), known_versions=known_versions)
                    for t in existing]
    local_isrcs = [_norm_isrc(t.get("isrc")) for t in existing]
    # A restore brings back the mix or master the backup's folder names; the
    # original's recording IDs do not stand in for it.
    restored_kinds = _recording_kinds(album_version) if album_folder is not None else None
    used = set()
    pairs = {}
    for index, track in enumerate(tracks):
        isrc = canonical_recording_id(track.get("isrc"), "isrc")
        matches = [i for i, local_isrc in enumerate(local_isrcs)
                   if isrc and local_isrc == isrc and i not in used
                   and (restored_kinds is None or restored_kinds
                        == _recording_kinds(local_albums[i][1] or folder_version))]
        if matches:
            title = canonical_track_title(_release_title(track))
            picked = max(matches, key=lambda i: (
                (track.get("media_number") or 1) == (existing[i].get("discnumber") or 1),
                title == canonical_track_title(existing[i].get("title") or ""),
                track.get("track_number") == existing[i].get("tracknumber")))
            used.add(picked)
            pairs[index] = picked
    if len(pairs) == len(tracks) or len(used) == len(existing):
        return pairs

    track_labels = [_label_parts(t["version"]) if t.get("version") else frozenset()
                    for t in tracks]
    disc_scoped = _disc_scoped_match(tracks, existing)
    local_keys = [_edition_title_parts(t.get("title"), known_versions=known_versions)
                  for t in existing]
    for index, track in enumerate(tracks):
        if index in pairs:
            continue
        # "To Live Is to Fly" with version "Live": the word is the title's own.
        title_word = _version_in_title(track)
        wanted, track_version = _edition_title_parts(
            _release_title(track), "" if title_word else track.get("version"))
        if not wanted:
            continue
        track_album = album_version - _label_parts(track["version"]) if title_word else album_version
        if track_version:
            # Blue Lines is "2012 Mix/Master" because eight tracks are; the
            # ninth says "Remastered 2012" and is not bound by their label.
            track_album = track_album - frozenset().union(
                *(labels for i, labels in enumerate(track_labels) if i != index))
        version = track_version | track_album
        isrc = _norm_isrc(track.get("isrc"))
        matches = []
        for local_index, local in enumerate(existing):
            if local_index in used:
                continue
            if isrc and local_isrcs[local_index] and isrc != local_isrcs[local_index]:
                continue
            if disc_scoped and (track.get("media_number") or 1) != (local.get("discnumber") or 1):
                continue
            local_title, local_version = local_keys[local_index]
            local_album, tag_version = local_albums[local_index]
            if wanted != local_title or album_key not in (local_album, folder_key):
                continue
            if not version or not version.issubset(local_version):
                local_version = local_version | (tag_version or folder_version)
            if _recording_labels(version) == _recording_labels(local_version):
                matches.append(local_index)
        if matches:
            picked = max(matches, key=lambda i: (
                (track.get("media_number") or 1) == (existing[i].get("discnumber") or 1)))
            used.add(picked)
            pairs[index] = picked
    return pairs


def edition_missing(album, existing, folder, *, album_folder=None):
    """Compare one release without substituting another mix or remaster."""
    tracks = (album.get("tracks") or {}).get("items") or []
    pairs = _edition_pairs(album, existing, folder, album_folder=album_folder)
    return ([t for i, t in enumerate(tracks) if i not in pairs],
            [t for i, t in enumerate(tracks) if i in pairs])


def edition_files(album, existing, folder):
    paired = set(_edition_pairs(album, existing, folder).values())
    return [track for i, track in enumerate(existing) if i in paired]


def held_by_other_versions(album, existing, folder, missing):
    """The tracks in ``missing`` whose song the folder already holds as
    another version, such as a different master: matched by title against
    the files none of the edition's tracks paired with."""
    if not missing:
        return []
    paired = set(_edition_pairs(album, existing, folder).values())
    spare = [track for i, track in enumerate(existing) if i not in paired]
    return compute_missing(missing, spare)[1] if spare else []


_LABEL_WORD_RE = re.compile(
    r"(?:19|20)\d\d|remaster|remix|mix|mono|stereo|live|acoustic|instrumental|demo|"
    r"take|session|edit(?!ion)|deluxe|expanded|anniversary|special|super|legacy|"
    r"collector|bonus|reissue")
_LABEL_FILLER_RE = re.compile(
    r"version|edition|original|album|the|and|in|\d+(?:st|nd|rd|th)|ed|es|s")
_EXPANDED_LABEL_RE = re.compile(r"deluxe|expanded|anniversary|bonus|special|super|collector|legacy")


def _label_words(label):
    """The edition words and years in a label, and the rest of it."""
    words = set(_LABEL_WORD_RE.findall(label))
    return words, _LABEL_FILLER_RE.sub("", _LABEL_WORD_RE.sub("", label))


def _shows_edition_tracks(album, existing, pairs, token=None):
    """Whether the files hold this release's track list rather than a
    smaller edition's: all of its tracks by recording ID, or all but at
    most one in ten and more of them than any smaller edition of the album
    in its artist's catalogue has. The catalogue is fetched with ``token``
    when none is cached. Without one that lists the album, nothing rules a
    smaller edition out."""
    tracks = (album.get("tracks") or {}).get("items") or []
    held = sum(1 for i, j in pairs.items()
               if _norm_isrc(tracks[i].get("isrc"))
               and _norm_isrc(tracks[i].get("isrc")) == _norm_isrc(existing[j].get("isrc")))
    if held == len(tracks):
        return True
    if len(tracks) - held > len(tracks) // 10:
        return False
    artist_id = (album.get("artist") or {}).get("id")
    cached = album_cache.get_catalog(f"{artist_id}:{config.ARTIST_CATALOG_LIMIT}", math.inf)
    listed = (cached or {}).get("items")
    if listed is None and token and artist_id:
        try:
            listed, _total = qobuz_search.get_artist_albums(
                artist_id, token, limit=config.ARTIST_CATALOG_LIMIT)
        except QobuzError:
            listed = None
    title = (edition_group_key(album) or (None,))[0]
    editions = [other.get("tracks_count") or 0 for other in listed or []
                if isinstance(other, dict) and (edition_group_key(other) or (None,))[0] == title]
    return bool(editions) and not any(held <= count < len(tracks) for count in editions)


def same_edition(album, existing, folder, token=None, *, fill=False):
    """Whether the files are this release rather than another mix or master
    of it: some of its tracks pair with them as the same edition, none of
    the rest is a song they hold as another version, and the labels a
    download of it leaves are on the files or their folder too.

    A download writes each track's version into its title and the release
    title into the album tag, but Qobuz's album version only when the album
    tag or folder names an edition, so that version is checked only then.
    A deluxe, expanded or other edition label the files lack still counts
    when they hold its track list (_shows_edition_tracks, which may ask
    Qobuz for the artist's catalogue with ``token``). With ``fill``, for a
    gap fill that brings the rest of that list, such a label counts without
    it, and up to one in ten of the release's songs on the files may be
    another version, which the fill leaves out. Wording and word order may
    differ: "Remastered" is the files' "2015 Remaster", but "2009 Remaster"
    is not their "Remastered"."""
    tracks = (album.get("tracks") or {}).get("items") or []
    pairs = _edition_pairs(album, existing, folder)
    missing = [t for i, t in enumerate(tracks) if i not in pairs]
    if not pairs:
        return False
    others = held_by_other_versions(album, existing, folder, missing)
    if others and (not fill or len(others) * 9 > len(pairs)):
        return False
    title_labels = _edition_title_parts(album.get("title"))[1]
    album_labels = _edition_title_parts(_release_title(album), album.get("version") or "")[1]
    wanted, known = set(title_labels), set(album_labels)
    for index in pairs:
        track = tracks[index]
        labels = _edition_title_parts(_release_title(track), track.get("version") or "")[1]
        wanted |= labels
        known |= labels
        if track.get("version"):
            known.add(canonical_track_title(track["version"]))
    files = [existing[i] for i in set(pairs.values())]
    tagged = set(_edition_title_parts(folder.name, folder=True, known_versions=known)[1])
    for local in files:
        tagged |= _edition_title_parts(local.get("album"), known_versions=known)[1]
    if tagged - title_labels:
        wanted |= album_labels
    held = set(tagged)
    for local in files:
        held |= _edition_title_parts(local.get("title"), known_versions=known)[1]
    held_words, held_rests = set(), {""}
    for label in held:
        words, rest = _label_words(label)
        held_words |= words
        held_rests.add(rest)

    def unheld(labels):
        return [label for label in labels - held
                if _label_words(label)[1] not in held_rests
                or not _label_words(label)[0] <= held_words]

    lacking = unheld(wanted)
    if not all(_PACKAGING_LABEL_RE.fullmatch(label) for label in lacking):
        return False
    expanded = [label for label in unheld(album_labels) if _EXPANDED_LABEL_RE.search(label)]
    return not (lacking or expanded) or fill or _shows_edition_tracks(
        album, existing, pairs, token)


_ALTERNATE_WORD_RE = re.compile(
    r"mix|mono|stereo|live|acoustic|instrumental|demo|take|session|"
    r"edit(?!ion)|version|alternat|outtake")
# "Remastered Version", "2017 Mix", "Stereo Mix 2015": the same recording
# mastered or mixed again, not another version of the song.
_REMASTER_WORD_RE = re.compile(
    r"(?:19|20)\d\d|digital|remaster(?:ed)?|master(?:ed)?|mono|stereo|new|"
    r"mix(?:ed)?|version|edition")
_REMASTER_LABEL_RE = re.compile(f"(?:{_REMASTER_WORD_RE.pattern})+")


def _remaster_words(labels):
    return {word for label in labels if _REMASTER_LABEL_RE.fullmatch(label)
            for word in _REMASTER_WORD_RE.findall(label)}


def _remaster_label(label, held_labels=()):
    """Whether a label adds only a remaster or a dated or new mix to the
    copy held with ``held_labels``. A remastered mono or stereo version,
    such as "Mono Remastered", is still another version unless the held
    copy is mono or stereo too."""
    if not _REMASTER_LABEL_RE.fullmatch(label):
        return False
    dated = re.search(r"(?:19|20)\d\d|new", label)
    if dated and "mix" in label:
        return True
    words = _remaster_words([label]) - _remaster_words(held_labels)
    if words & {"mono", "stereo"}:
        return False
    return bool(dated or "master" in label) or words <= {"version", "edition", "digital"}


def bonus_versions(missing, present):
    """The tracks in ``missing`` that are another version of a song in
    ``present``: the same title once its version labels and the version
    field are set aside, adding a mix, live, demo, edit, instrumental or
    other version label the present copy lacks, such as "Footprints
    (Remix)" beside "Footprints". A reprise, a remaster or a dated mix
    such as "2017 Mix" alone is not."""
    held = {}
    for track in present:
        base, labels = _edition_title_parts(_release_title(track))
        held.setdefault(base, []).append(_recording_labels(labels))
    bonus = []
    for track in missing:
        base, labels = _edition_title_parts(_release_title(track))
        recording = _recording_labels(labels)
        if recording in held.get(base, ()):
            continue
        for other in held.get(base, ()):
            added = recording - other
            if (added and any(_ALTERNATE_WORD_RE.search(label)
                              and not _remaster_label(label, other)
                              for label in added)
                    and not any("reprise" in label for label in added)):
                bonus.append(track)
                break
    return bonus


def _edition_matches(album):
    folders = []
    for exact in (True, False):
        folder = find_album_dir_filesystem(album, exact_edition=exact)
        if folder is not None and folder not in folders:
            folders.append(folder)
    bare = canonical_track_title(strip_album_decorations(album.get("title") or ""))
    short = len((album.get("tracks") or {}).get("items") or []) < _MIN_ALBUM_TRACKS
    for parent in dict.fromkeys(folder.parent for folder in folders):
        for folder in _list_artist_subdirs_cached(parent):
            name = strip_album_decorations(folder.name)
            if folder in folders or not bare:
                continue
            if canonical_track_title(name) == bare or (
                    short and canonical_track_title(_RELEASE_TYPE_RE.sub("", name)) == bare):
                folders.append(folder)
    for folder in folders:
        existing, _ = find_existing_tracks(album, album_dir=folder)
        missing, present = edition_missing(album, existing, folder)
        # A single beside its remixes shares a recording with them, but a
        # folder holding none of the versioned tracks is not that release.
        if (any(map(_carries_version, missing)) and not any(map(_carries_version, present))
                and names_other_version(album, folder)):
            continue
        yield existing, folder, missing, present


def _carries_version(track):
    return bool(_edition_title_parts(_release_title(track), track.get("version") or "")[1])


def names_other_version(album, folder):
    """Whether the release carries a version, such as "Remixes", "Acoustic"
    or "Deluxe Edition", that the folder's name lacks. A folder named for
    the release, or for a title that carries a label of its own, has it."""
    title, version = album.get("title") or "", (album.get("version") or "").strip()
    labels = _edition_title_parts(title, "" if version in title else version)[1]
    named = {canonical_track_title(beets_sanitize(_release_title(album)))}
    if _edition_title_parts(title)[1]:
        named.add(canonical_track_title(beets_sanitize(title)))
    return (bool(labels - _edition_title_parts(folder.name, folder=True, known_versions=labels)[1])
            and canonical_track_title(_DECORATION_YEAR_RE.sub("", folder.name)) not in named)


def find_edition_tracks(album, *, track_id=None):
    """Find one edition using full track versions, album labels and ISRCs."""
    tracks = (album.get("tracks") or {}).get("items") or []
    best = ([], None, tracks, [])
    for existing, folder, missing, present in _edition_matches(album):
        if track_id is not None and any(str(t.get("id")) == str(track_id) for t in present):
            return existing, folder, missing, present
        if len(present) > len(best[3]):
            best = existing, folder, missing, present
            if not missing:
                break
    return best


def owned_edition_track_ids(album):
    return {
        str(track["id"])
        for _existing, _folder, _missing, present in _edition_matches(album)
        for track in present if track.get("id")
    }


def bears_album_title(album, folder):
    """Whether a folder is named for the album once edition labels, the year
    and a leading article are stripped, reading "&" as "and"."""
    def bare(name):
        name = strip_leading_article(strip_album_decorations(name or ""))
        return canonical_track_title(re.sub(r"\s*&\s*", " and ", name))
    return bare(folder.name) == bare(album.get("title"))


def holds_recordings(tracks, existing):
    """Whether the files hold every recording on a track list by ISRC, as a
    box set holds the albums it collects."""
    wanted = {_norm_isrc(track.get("isrc")) for track in tracks}
    held = {_norm_isrc(track.get("isrc")) for track in existing}
    return bool(wanted) and "" not in wanted and wanted <= held


def album_tracks_complete(album: dict) -> bool:
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


def titled_as_searched(album, query):
    """Whether ``album`` is titled exactly what was searched, so it leads
    the results wherever Qobuz ranked it."""
    def key(text):
        return normalize(strip_leading_article(text.strip()))

    wanted = key(query)
    title = album.get("title") or ""
    return bool(wanted) and wanted in (
        key(title), key(strip_album_decorations(title)))


def search_result_ownership(album, fetch_album):
    """How the library holds an album search result, as Search marks it.

    ``fetch_album`` returns the result's full payload by id; it is called only
    when a folder on disk may hold the album, or when the album cache says
    Qobuz did not stream one of its tracks, since it may stream it now. An
    album the cache does not hold gets no "not on Qobuz" count. Owned needs
    the edition and all its tracks, not just the songs on another mix.
    ``ownership_unknown`` stays set when the check could not finish.
    """
    res = {"ownership_unknown": True}
    folder = (find_album_dir_filesystem(album)
              or find_album_dir_filesystem(album, exact_edition=True))
    if folder is None:
        res["ownership_unknown"] = False
        cached = (album_cache.get(album["id"]) or {}).get("tracks") or {}
        if any(unstreamable(t) for t in cached.get("items") or []):
            try:
                fresh = fetch_album(album["id"]).get("tracks") or {}
            except (QobuzError, QobuzUnavailable):
                fresh = {}
            res["unavailable"] = sum(
                1 for t in fresh.get("items") or [] if unstreamable(t))
        return res
    exact_album = fetch_album(album["id"])
    qobuz_tracks = (exact_album.get("tracks") or {}).get("items") or []
    if not album_tracks_complete(exact_album):
        return res
    existing, matched_folder, missing, present = find_edition_tracks(exact_album)
    if not existing:
        files, other = find_album_folder(exact_album)
        if other is not None and holds_recordings(qobuz_tracks, files):
            res["owned"] = True
        elif other is not None:
            res["as_new_edition"] = True
            res["folder"] = str(other.relative_to(config.MUSIC_ROOT))
        res["ownership_unknown"] = False
        return res
    res["disk_year"] = _dir_year(matched_folder.name)
    held = held_by_other_versions(exact_album, existing, matched_folder, missing)
    gaps = [t for t in missing if not any(t is h for h in held)]
    # No download fetches a track Qobuz does not stream, so it is no gap to
    # offer; the mark says how many there are.
    res["unavailable"] = sum(1 for t in gaps if unstreamable(t))
    gaps = [t for t in gaps if not unstreamable(t)]
    bonus = bonus_versions(gaps, present)
    if gaps and len(bonus) == len(gaps):
        # Only other versions of songs on disk are missing: the album is
        # complete.
        res["owned"] = True
        res["bonus_versions"] = len(bonus)
    elif gaps:
        res["partial"] = True
        res["folder"] = str(matched_folder.relative_to(config.MUSIC_ROOT))
        res["have_tracks"] = len(qobuz_tracks) - len(gaps) - res["unavailable"]
        res["want_tracks"] = len(qobuz_tracks)
    elif held:
        # Every song is on disk, some as another master: a Download here is a
        # separate copy, not a fill.
        res["as_new_edition"] = True
        res["folder"] = str(matched_folder.relative_to(config.MUSIC_ROOT))
    else:
        res["owned"] = True
    res["ownership_unknown"] = False
    return res


def find_album_folder(album):
    """(files, folder) for the folder that holds the album or another
    version of it, or ([], None). The folder the album's name finds counts
    when it bears the album's title, when the album's songs make up most of
    its files, or when it holds every recording on the album; a folder
    holding another album, such as "Illmatic" for "Stillmatic", does not."""
    existing, folder = find_existing_tracks(album)
    if folder is None or bears_album_title(album, folder):
        return existing, folder
    tracks = (album.get("tracks") or {}).get("items") or []
    if tracks and existing:
        present = compute_missing(tracks, existing)[1]
        if len(present) * 2 >= len(existing) or holds_recordings(tracks, existing):
            return existing, folder
    return [], None


def other_version_dir(album):
    """The folder of the album or another version of it (find_album_folder)."""
    return find_album_folder(album)[1]


def _strict_recording_id(track, field):
    return canonical_recording_id(track.get(field), field)


def _strict_track_slot(track, disc_field, number_field):
    return canonical_track_slot(track, disc_field, number_field)


def _strict_track_duration(track, field):
    if isinstance(track.get(field), bool):
        return None
    try:
        duration = float(track.get(field) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return duration if duration > 0 and math.isfinite(duration) else None


def _strict_titles_match(qobuz_track, existing_track):
    if (
        not isinstance(qobuz_track.get("title"), str)
        or not isinstance(existing_track.get("title"), str)
    ):
        return False
    qobuz_key = _track_key(
        qobuz_track.get("title"), 1, "", disc_scoped=False)
    existing_key = _track_key(
        existing_track.get("title"), 1, "", disc_scoped=False)
    return _keys_match(qobuz_key, existing_key, "title")


def _destructive_track_match(qobuz_track, existing_track):
    qobuz_isrc = _strict_recording_id(qobuz_track, "isrc")
    existing_isrc = _strict_recording_id(existing_track, "isrc")
    qobuz_mbid = _strict_recording_id(qobuz_track, "mb_trackid")
    existing_mbid = _strict_recording_id(existing_track, "mb_trackid")
    if None in (qobuz_isrc, existing_isrc, qobuz_mbid, existing_mbid):
        return False
    if qobuz_isrc or existing_isrc:
        return bool(
            qobuz_isrc
            and qobuz_isrc == existing_isrc
            and not (
                qobuz_mbid and existing_mbid
                and qobuz_mbid != existing_mbid
            )
        )
    if qobuz_mbid or existing_mbid:
        return bool(qobuz_mbid and qobuz_mbid == existing_mbid)

    qobuz_slot = _strict_track_slot(
        qobuz_track, "media_number", "track_number")
    qobuz_duration = _strict_track_duration(qobuz_track, "duration")
    existing_duration = _strict_track_duration(existing_track, "length")
    return bool(
        qobuz_slot is not None
        and qobuz_slot == _strict_track_slot(
            existing_track, "discnumber", "tracknumber")
        and qobuz_duration is not None
        and existing_duration is not None
        and abs(qobuz_duration - existing_duration) <= 2.0
        and _strict_titles_match(qobuz_track, existing_track)
    )


def _destructive_track_coverage(qobuz_tracks, existing_tracks):
    existing_slots = [
        slot for track in existing_tracks
        if (slot := _strict_track_slot(
            track, "discnumber", "tracknumber")) is not None
    ]
    if len(existing_slots) != len(set(existing_slots)):
        return False

    claimed = set()
    for qobuz_track in qobuz_tracks:
        candidates = [
            index for index, existing_track in enumerate(existing_tracks)
            if _destructive_track_match(qobuz_track, existing_track)
        ]
        if len(candidates) != 1 or candidates[0] in claimed:
            return False
        claimed.add(candidates[0])
    return True


def folder_holds_all_tracks(folder, qobuz_tracks, destructive=False, *,
                           edition=None, folder_label=None):
    """True only when every expected Qobuz track one-to-one matches an audio
    file under ``folder``.

    This gates gap-fill backup deletion: a raw file count can be satisfied by
    extras, duplicate files, or pre-existing tracks while an expected track is
    absent, and the backup being deleted holds the only copy of the tracks
    moved aside. No expected list, an unreadable folder, or a walk that
    couldn't cover the whole tree reads as False - unverifiable means keep
    the backup. Destructive callers opt into recording-authority, slot, title,
    and duration checks; ordinary discovery keeps its intentionally permissive
    edition matcher."""
    if not qobuz_tracks or folder is None or not folder.exists():
        return False
    walk_errors = []
    tracks = read_album_dir(folder, walk_errors=walk_errors)
    if not tracks or walk_errors:
        # A degraded walk lists only the readable part of the folder, but the
        # decision below is about ALL of it: pairing against a partial listing
        # can prove "complete" with the wrong file while the right one sits in
        # the unreadable subtree.
        return False
    if edition is not None:
        pairs = _edition_pairs(edition, tracks, Path(folder_label) if folder_label else folder)
        if len(pairs) != len(qobuz_tracks):
            return False
        known_versions = frozenset(canonical_track_title(t["version"])
                                   for t in [edition, *qobuz_tracks] if t.get("version"))
        tracks = [{**tracks[pairs[i]], "title": _edition_title_parts(
            tracks[pairs[i]].get("title"), known_versions=known_versions)[0]}
            for i in range(len(qobuz_tracks))]
        qobuz_tracks = [{**t, "title": _edition_title_parts(
            _release_title(t), t.get("version"))[0]} for t in qobuz_tracks]
    if destructive:
        return _destructive_track_coverage(qobuz_tracks, tracks)
    still_missing, _ = compute_missing(qobuz_tracks, tracks)
    return not still_missing


def find_extras_in_existing(qobuz_tracks, existing_tracks):
    """Return on-disk tracks that match no Qobuz track.

    The reverse of compute_missing: each Qobuz track accounts for at most one
    file, so a same-titled bonus track (a custom rip, a B-side, a deluxe extra)
    is flagged rather than hidden. This gates the upgrade wipe-and-replace, so a
    missed extra means silently deleting a track the user can't get back.
    """
    return [et for et, qt in zip(existing_tracks,
                                 qobuz_tracks_for(existing_tracks, qobuz_tracks))
            if qt is None]


def qobuz_tracks_for(existing_tracks, qobuz_tracks):
    """The Qobuz track each on-disk track matches, or None, paired one to
    one on ISRC, then title (see _pair_indices)."""
    disc_scoped = _disc_scoped_match(qobuz_tracks, existing_tracks)
    slots = _pair_indices(_existing_keys(existing_tracks, disc_scoped),
                          _qobuz_keys(qobuz_tracks, disc_scoped))
    return [None if si is None else qobuz_tracks[si] for si in slots]


# ── Album metadata helpers ────────────────────────────────────────────────────
def _listed_year(album):
    """The year Qobuz lists, placeholder or not.

    Prefers release_date_original. Falls back to released_at parsed in UTC so
    late UTC releases keep their catalogue year in every local timezone.
    """
    rdo = album.get("release_date_original") or ""
    if isinstance(rdo, str) and rdo[:4].isdigit():
        return rdo[:4]
    ra = album.get("released_at")
    if isinstance(ra, (int, float)):
        try:
            return str(datetime.fromtimestamp(int(ra), tz=timezone.utc).year)
        except (ValueError, OSError, OverflowError):
            return ""
    return ""


def album_year(album):
    """Album release year as a string, or '' if unknown."""
    return known_year(_listed_year(album))


def album_year_int(album, fallback=99999):
    """Numeric year for sorting; missing → fallback (sorts last by default)."""
    y = album_year(album)
    try:
        return int(y) if y else fallback
    except ValueError:
        return fallback


def album_release_ts(album):
    """Original-release date as a UTC unix timestamp, or None if unknown.

    Prefers release_date_original (YYYY-MM-DD); falls back to released_at.
    """
    rdo = album.get("release_date_original") or ""
    if isinstance(rdo, str) and len(rdo) >= 10 and rdo[:4].isdigit():
        try:
            return datetime.strptime(
                rdo[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    ra = album.get("released_at")
    if isinstance(ra, (int, float)):
        return float(ra)
    return None


def album_released_within(album, max_age_days):
    """True if the album's ORIGINAL release date is within max_age_days of now.

    For surfacing genuinely-new releases: Qobuz routinely back-fills old
    catalogue titles, so "appeared in the catalog since the baseline" isn't
    enough on its own - a 2020 album it only just listed is a back-catalogue gap,
    not a new release. An album with no parseable date is treated as recent (don't
    suppress a possibly-new release over a missing date). max_age_days <= 0
    disables the window (any catalog newcomer counts, the original behaviour).
    """
    if max_age_days <= 0:
        return True
    ts = album_release_ts(album)
    if ts is None:
        return True
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    return ts >= cutoff


def is_lossless_album(album):
    return (album.get("maximum_bit_depth") or 0) >= 16


def album_quality_label(album):
    """Human-readable quality string for a Qobuz album dict."""
    bd = album.get("maximum_bit_depth") or 0
    sr = album.get("maximum_sampling_rate") or 0
    sr_khz = (sr / 1000) if sr >= 1000 else sr
    if bd >= 24:
        return f"{bd}-bit/{sr_khz:g}kHz (hi-res)"
    if bd >= 16:
        return f"{bd}-bit/{sr_khz:g}kHz"
    return "lossy"


# ── Catalog filtering ─────────────────────────────────────────────────────────
_MIN_ALBUM_TRACKS = 4  # below this an edition is a stray single/EP, not the
                       # album - ignored when sizing the standard edition.
# A normalized title ending in a number: "jubileumii", "greatesthitsvol3".
_NUMBERED_KEY_RE = re.compile(r"(.*?)(?:part|pt|vol|volume|chapter|act|book)?(\d+|[ivx]+)")


def _numbered_apart(key_a, key_b):
    """Whether two normalized titles are the same name with different
    numbers at the end, as 'Jubileum I' and 'Jubileum II' or 'Van Halen'
    and 'Van Halen II' are (see _numbers_differ)."""
    stems, numbers = [], []
    for key in (key_a, key_b):
        match = _NUMBERED_KEY_RE.fullmatch(key)
        stems.append(match.group(1) if match else key)
        numbers.append(match.group(2) if match else "")
    return stems[0] == stems[1] and _numbers_differ(*numbers)


def _standard_track_count(group):
    """Track count of the real album among a group of editions.

    The plain release is the album. Qobuz can file a smaller companion under
    the same title with a version such as 'Bonus Content', so the smallest
    edition only stands in when there is no plain one: deluxe, anniversary
    and expanded editions add tracks and remasters keep the original count.
    None when no edition reports a usable count.

    Editions below ``_MIN_ALBUM_TRACKS`` (3 tracks or fewer) are treated as
    stray singles/EPs that happen to dedup into the same group as the album
    by title and ignored when sizing - so a same-titled 3-track EP filed
    next to a 15-track deluxe doesn't win the canonical pick. (An EP with
    4+ tracks, no version and the album's title can still mislead it.)
    """
    sized = [a for a in group if (a.get("tracks_count") or 0) > 0]
    if not sized:
        return None
    full = [a for a in sized if a["tracks_count"] >= _MIN_ALBUM_TRACKS]
    plain = [a for a in full if _is_plain_release(a)]
    return min(a["tracks_count"] for a in (plain or full or sized))


def _is_decorated_edition(album):
    """True if the title carries an edition tag (remaster, deluxe, anniversary,
    a year, ...) rather than being the plain original release."""
    title = album.get("title") or ""
    return (strip_album_decorations(title).casefold().strip()
            != title.casefold().strip())


def _is_plain_release(album):
    """True when neither the title nor Qobuz's version field names an
    edition."""
    return (not (album.get("version") or "").strip()
            and not _is_decorated_edition(album))


_DERIVATIVE_WORDS = r"(?:instrumentals?|karaoke|a\s?c{1,2}ap{1,2}ellas?)"
_DERIVATIVE_RE = re.compile(
    rf"\b(?:{_DERIVATIVE_WORDS}|tribute\s+(?:to|2)|(?:(?<=\w\s)|(?<=[(\[]))(?<!\band\s)tribute|"
    r"cover versions?|backing tracks?|"
    r"(?:made famous|originally performed|as performed) by|in the style of)\b",
    re.IGNORECASE)
# "Wolf + Instrumentals", "Papercuts: Instrumentals", "2001 Instrumental",
# "Carnival (Instrumental)", "From Zero: A Cappellas + Instrumentals".
_DERIVATIVE_TAIL_RE = re.compile(
    rf"\s*(?:[(\[][^()\[\]]*\b{_DERIVATIVE_WORDS}\b[^()\[\]]*[)\]]"
    rf"|(?:[-\u2013\u2014:+]\s*|\s)(?:the\s+)?{_DERIVATIVE_WORDS}"
    rf"(?:\s*(?:\+|&|and)\s*{_DERIVATIVE_WORDS})?(?:\s+(?:version|edition))?)\s*$",
    re.IGNORECASE)


def is_derivative_release(album):
    """Whether a release is made from an album rather than being it: an
    instrumental, karaoke, a cappella, tribute or cover version, named so in
    its title or on every one of its tracks. A summary without a track list
    is read from the album cache, so no request is made."""
    if _DERIVATIVE_RE.search(f"{album.get('title') or ''} {album.get('version') or ''}"):
        return True
    full = album if album.get("tracks") else (album_cache.get(album.get("id")) or {})
    items = (full.get("tracks") or {}).get("items") or []
    return bool(items) and all(_DERIVATIVE_RE.search(_release_title(t)) for t in items)


def _artist_key(album):
    return normalize((album.get("artist") or {}).get("name") or "")


def _derivative_of(album):
    """The edition_group_key title of the album an instrumental, karaoke or
    a cappella release is named after, or None for a release not named as
    one. The word after the artist's own name, as in "Canned Heat
    Instrumentals" or "Canned Heat: Instrumentals", names a collection
    rather than a version of the self-titled album, unless it is in
    brackets or added with "+"."""
    raw = album.get("title") or ""
    title = strip_album_decorations(raw)
    tail = _DERIVATIVE_TAIL_RE.search(title)
    base = strip_album_decorations(title[:tail.start()]) if tail else title
    if base == title:
        # strip_album_decorations drops "(Acapella)" or "(Karaoke Version)"
        # along with edition labels, so look at what it took off.
        dropped = raw.replace(title, " ", 1)
        if not re.search(rf"\b{_DERIVATIVE_WORDS}\b",
                         f"{dropped} {album.get('version') or ''}", re.IGNORECASE):
            return None
    elif tail.group().strip()[0] not in "([+" and normalize(base) == _artist_key(album):
        return None
    key = edition_group_key({"title": base})
    return key[0] if key else None


def _fold_derivatives(groups):
    """Move an instrumental, karaoke or a cappella release whose group holds
    no original into the group of the same artist's album it is named
    after, the nearest year first. Such releases often come out later or
    carry the label in the title, which keeps them apart by title and year."""
    def has_original(group, artist):
        return any(_artist_key(a) == artist and _derivative_of(a) is None for a in group)

    for key, group in list(groups.items()):
        for album in list(group):
            base = _derivative_of(album)
            artist = _artist_key(album)
            if base is None or has_original(group, artist):
                continue
            targets = [k for k, other in groups.items()
                       if k[0] == base and k != key and has_original(other, artist)]
            if targets:
                group.remove(album)
                groups[min(targets, key=lambda k: abs(k[1] - key[1]))].append(album)
        if not group:
            del groups[key]


def _best_edition(group, prefer_hires):
    """Representative edition for a group of same-album editions.

    An instrumental, karaoke or other derivative release never stands for
    the album when the same artist's original is in the group. Both modes
    target the standard track count so a padded deluxe/anniversary edition
    never wins on track count alone. prefer_hires then takes the best
    resolution at that count; otherwise it takes the original (untagged)
    edition without chasing a remaster's higher resolution.
    """
    if len(group) > 1:
        derivative = {id(a): is_derivative_release(a) for a in group}
        with_original = {_artist_key(a) for a in group if not derivative[id(a)]}
        group = [a for a in group
                 if not derivative[id(a)] or _artist_key(a) not in with_original]
    standard = _standard_track_count(group)

    def off_standard(a):
        if standard is None:
            return 0
        return abs((a.get("tracks_count") or 0) - standard)

    if prefer_hires:
        return min(group, key=lambda a: (
            off_standard(a),
            -(a.get("maximum_bit_depth") or 0),
            -(a.get("maximum_sampling_rate") or 0),
            album_year_int(a),
            str(a.get("id") or ""),
        ))
    return min(group, key=lambda a: (
        off_standard(a),
        not _is_plain_release(a),
        album_year_int(a),
        str(a.get("id") or ""),
    ))


def filter_owned_albums(catalog_pairs, owned_bare_titles, artist_name=None):
    """Drop catalog entries whose bare title matches something already owned.

    An exact bare-title match normally counts as owned regardless of year - a
    far-off year is a remaster/reissue of the same work (a 2022 'Revolver' is the
    1966 one re-released, not a new album). The one exception is a *self-titled*
    release: when the bare title equals the artist name and the catalog entry is
    undecorated and its year is far from every owned copy, it's usually a
    genuinely distinct same-titled album (Weezer's colour records, Peter
    Gabriel's four 'Peter Gabriel' LPs) rather than a reissue - so it's offered
    instead of silently hidden. ``artist_name`` enables that exception; without
    it the year-blind exact-own behaviour is unchanged. Falls back to a fuzzy
    match at CONSOLIDATE_THRESH for edition variants the decoration stripper
    didn't fully normalize, with a nearby-year guard separating distinct names.
    """
    self_titled_key = (normalize(strip_leading_article(artist_name))
                       if artist_name else None)
    if not owned_bare_titles:
        return list(catalog_pairs)
    missing = []
    for album, n_versions in catalog_pairs:
        bare = strip_leading_article(strip_album_decorations(album.get("title") or ""))
        key = normalize(bare)
        if not key:
            missing.append((album, n_versions))
            continue
        catalog_yr_str = album_year(album)
        try:
            cat_yr = int(catalog_yr_str) if catalog_yr_str else None
        except ValueError:
            cat_yr = None
        if key in owned_bare_titles:
            owned_years = owned_bare_titles[key]
            # Exact bare-title match → owned regardless of year (reissue of
            # the same work), EXCEPT a self-titled, undecorated, far-from-
            # owned-year release: those are usually distinct same-titled
            # albums, so let them fall through and be offered rather than
            # hidden behind a year-blind own.
            is_self_titled = self_titled_key is not None and key == self_titled_key
            distinct_self_titled = (
                is_self_titled
                and not _is_decorated_edition(album)
                and cat_yr is not None and None not in owned_years
                and all(oy is None or abs(oy - cat_yr) > 3 for oy in owned_years)
            )
            if not distinct_self_titled:
                continue
        # Fuzzy fallback for edition variants the stripper didn't fully
        # normalize.
        owned_fuzzy = False
        for owned, owned_years in owned_bare_titles.items():
            if not owned or not (key.startswith(owned) or owned.startswith(key)):
                continue
            # The extra text on the longer title is what one release has and
            # the other doesn't.
            shorter, longer = sorted((key, owned), key=len)
            if differs_by_album_variant(shorter, longer) or _numbered_apart(key, owned):
                continue
            if similarity(key, owned) < config.CONSOLIDATE_THRESH:
                continue
            if (cat_yr is None or None in owned_years
                    or any(abs(oy - cat_yr) <= 3
                           for oy in owned_years if oy is not None)):
                owned_fuzzy = True
                break
        if owned_fuzzy:
            continue
        missing.append((album, n_versions))
    return missing


def filter_short_releases(catalog_pairs, min_tracks=None):
    """Drop releases with fewer than min_tracks tracks.
    Hides standalone singles and very short EPs from the missing-albums step
    by default. None/missing tracks_count is allowed through (don't penalize
    bad metadata).
    """
    if min_tracks is None:
        min_tracks = config.MISSING_ALBUMS_MIN_TRACKS
    kept = []
    for album, n_versions in catalog_pairs:
        tc = album.get("tracks_count")
        if tc is None:
            kept.append((album, n_versions))
            continue
        try:
            if int(tc) >= min_tracks:
                kept.append((album, n_versions))
        except (TypeError, ValueError):
            kept.append((album, n_versions))
    return kept


def edition_group_key(album):
    """The key dedup_album_versions groups an album's editions by: its title
    without edition labels or a leading article, and its year. None for an
    album without a title."""
    bare = strip_leading_article(strip_album_decorations(album.get("title") or ""))
    # An emoji title normalizes to ''; fall back to the raw title so it isn't
    # silently dropped from the missing list. Only a genuinely empty title is
    # skipped.
    key_title = normalize(bare) or bare.strip().casefold()
    if not key_title:
        return None
    return key_title, album_year_int(album)


def dedup_album_versions(albums, prefer_hires=False):
    """Collapse multiple editions of the same album into one canonical entry.

    Two albums dedup if their stripped-decoration titles normalize identically,
    a leading article aside, AND share the same release year. Different years are kept as separate groups
    so self-titled albums from different years (Weezer's colour records, Peter
    Gabriel's four eponymous LPs) are not collapsed into one canonical entry.
    Within a group, picks (see _best_edition):
      - prefer_hires=True:  best resolution at the standard track count
      - prefer_hires=False: the original (untagged) edition
    The standard track count is the plain release's, or else the smallest
    real edition's, so a padded deluxe/anniversary release never wins on
    track count alone and a smaller companion never stands in for the album.
    An instrumental or karaoke release (is_derivative_release) is not picked
    over the same artist's original, and one named after an album by its
    title joins that album's group whatever its year (_fold_derivatives).

    Returns list of (canonical_album, n_versions_in_group) tuples, sorted by
    canonical's release year ascending.
    """
    groups = {}
    for a in albums:
        key = edition_group_key(a)
        if key is None:
            continue
        groups.setdefault(key, []).append(a)
    _fold_derivatives(groups)

    representatives = []
    for group in groups.values():
        best = _best_edition(group, prefer_hires)
        representatives.append((best, len(group)))

    representatives.sort(key=lambda pair: (
        album_year_int(pair[0]),
        normalize(pair[0].get("title", "")),
    ))
    return representatives


def _artist_name_sim(a, b):
    """Artist-name similarity that treats a leading 'The' as optional, so
    'The Beatles' and 'Beatles' match."""
    def _bare(s):
        s = (s or "").strip()
        return s[4:].strip() if s[:4].lower() == "the " else s
    return max(similarity(a, b), similarity(_bare(a), _bare(b)))


def filter_compilation_albums(catalog_pairs, artist_name):
    """Drop entries that look like compilations or have a different primary artist."""
    kept = []
    for album, n_versions in catalog_pairs:
        a_artist = (album.get("artist") or {}).get("name", "")
        if not a_artist:
            continue
        # If the album's primary artist isn't a strong match for the queried
        # artist, it's almost certainly a compilation/various-artists release.
        if _artist_name_sim(a_artist, artist_name) < config.ARTIST_NAME_THRESH:
            continue
        # Some Qobuz records flag compilations explicitly; honor it when present.
        if album.get("is_compilation") is True:
            continue
        kept.append((album, n_versions))
    return kept


# ── Post-import filesystem helpers ────────────────────────────────────────────

def _count_audio_files_in(d):
    """Count audio files recursively in a directory. 0 if missing."""
    if d is None or not d.exists():
        return 0
    n = 0
    try:
        for f in iter_tree_no_symlinks(d):
            if (f.suffix.lower() in config.AUDIO_EXTS
                    and not is_library_system_folder(f.name) and f.is_file()):
                n += 1
    except OSError:
        pass
    return n


def multi_artist_migration_destination(album, source_dir):
    """Return the opt-in primary-artist destination for an exact album path."""
    current = Path(source_dir)
    qartist_raw = (album.get("artist") or {}).get("name") or ""
    if not qartist_raw:
        return None
    if qartist_raw.lower().strip() in {
        "various artists", "various", "va", "compilations", "soundtrack",
    }:
        return None

    # Qobuz's main artist, whole: "Simon & Garfunkel" and "Tyler, The Creator"
    # are one artist each, so splitting the name would file under "Simon/".
    primary = beets_sanitize(qartist_raw.strip())
    if not primary or normalize(current.parent.name) == normalize(primary):
        return None

    # Keep the historical comma-only guard.
    if not _is_migration_candidate(current.parent.name, qartist_raw):
        return None
    return _artist_folder(primary, exclude=current.parent) / current.name


def _artist_folder(name, *, exclude=None):
    """The library's folder for an artist: an existing one whose name matches,
    exactly, then by case, then normalized, else a new one named ``name``."""
    root = Path(config.MUSIC_ROOT)
    try:
        folders = [d for d in _list_artist_subdirs_cached(root)
                   if exclude is None or d.name != Path(exclude).name]
    except OSError:
        folders = []
    for same in (lambda d: d.name == name,
                 lambda d: d.name.casefold() == name.casefold(),
                 lambda d: normalize(d.name) == normalize(name)):
        found = next((d for d in folders if same(d)), None)
        if found is not None:
            return found
    return root / name


def prompt_and_migrate_multi_artist_folder(
    album,
    args,
    *,
    authority=None,
    source_dir=None,
):
    """Safely move an imported ``Primary, Other/Album`` below ``Primary``.

    The public option remains opt-in.  Publication is additive and
    crash-recoverable: an existing destination name is kept, never replaced.
    """
    from qobuz_librarian import run_lock
    from qobuz_librarian.library.post_import_relocation import (
        PostImportRelocationAttention,
        PostImportRelocationUnavailable,
        RelocationKind,
        relocate_post_import_album,
    )

    current = (
        Path(source_dir)
        if source_dir is not None
        else find_album_dir_filesystem(album)
    )
    if current is None:
        return None

    destination = multi_artist_migration_destination(album, current)
    if destination is None:
        return current
    lease = authority if authority is not None else run_lock.current_lease()
    try:
        result = relocate_post_import_album(
            current,
            destination,
            kind=RelocationKind.WHOLE_ALBUM,
            authority=lease,
        )
    except PostImportRelocationAttention:
        raise
    except (PostImportRelocationUnavailable, OSError, ValueError) as exc:
        log.info(fmt(
            C.YELLOW,
            "  ⚠  Multi-artist folder move was skipped safely; "
            f"the imported files remain at {current}."
        ))
        vlog(f"multi-artist folder move refused: {exc}")
        return current

    if not result.changed:
        if result.reason:
            log.info(fmt(C.YELLOW, f"  ⚠  Multi-artist folder unchanged: {result.reason}."))
        return current

    message = f"  ✓  Filed imported album under primary artist: {destination}."
    if result.reason:
        message += f" {result.reason.capitalize()}."
    log.info(fmt(C.GREEN, message))
    return destination


# ── Qobuz catalog matching ────────────────────────────────────────────────────

_DIR_YEAR_PAREN_RE = re.compile(r"\((\d{4})\)")
_DIR_YEAR_BARE_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _dir_year(name):
    """The release year embedded in an album folder name, or None. Prefers a
    parenthesised '(2013)' over a bare in-title year."""
    m = _DIR_YEAR_PAREN_RE.search(name)
    if m:
        return int(m.group(1))
    m = _DIR_YEAR_BARE_RE.search(name)
    if not m:
        return None
    # A title that simply IS a 4-digit number ('1989', '2112') is the album
    # name, not a year decoration - don't read it as a release year.
    if strip_album_decorations(name).strip() == m.group(1):
        return None
    return int(m.group(1))


def _year_proximity_bonus(dir_year, album):
    """Score nudge when a catalog album's year sits near the folder's: +0.10
    exact, +0.05 within a year, else 0. Breaks ties between two same-titled
    releases (a reissue, an annual live set) that score equally on title."""
    if dir_year is None:
        return 0.0
    ry_str = album_year(album)
    try:
        ry = int(ry_str) if ry_str else None
    except ValueError:
        ry = None
    if ry is None:
        return 0.0
    if ry == dir_year:
        return 0.10
    if abs(ry - dir_year) <= 1:
        return 0.05
    return 0.0


def _sort_by_match(candidates, prefer_hires):
    """Sort (score, year_bonus, album) tuples best-first in place: combined
    score descending, then resolution descending when prefer_hires."""
    if prefer_hires:
        candidates.sort(key=lambda x: (
            -(x[0] + x[1]),
            -(x[2].get("maximum_bit_depth") or 0),
            -(x[2].get("maximum_sampling_rate") or 0),
        ))
    else:
        candidates.sort(key=lambda x: -(x[0] + x[1]))


def _catalog_candidates_for_dir(album_dir, catalog, artist_name, prefer_hires=False):
    """All catalog entries scoring above ARTIST_DIR_MATCH_THRESH for
    album_dir, sorted best-first (similarity + year-proximity bonus, lossless
    only, artist-name guard). Returning the ranked list - not just the top
    pick - lets find_qobuz_album_for_dir resolve the target_dir case from the
    catalog instead of falling back to the search API."""
    if not catalog:
        return []
    forms = [
        (bare, bare.replace("_", " ").replace("  ", " ").strip(), normalize(bare))
        for bare in folder_title_forms(
            album_dir.name, album_dir.parent.name, artist_name)
    ]
    dir_year = _dir_year(album_dir.name)

    candidates = []
    for r in catalog:
        if not is_lossless_album(r):
            continue
        r_artist = (r.get("artist") or {}).get("name", "")
        if r_artist and similarity(r_artist, artist_name) < config.ARTIST_NAME_THRESH:
            continue
        title = r.get("title", "")
        version = (r.get("version") or "").strip()
        names = [title]
        if version and version.casefold() not in title.casefold():
            # Its own folder may name the version: "Lost Boys (Acoustic)".
            names.append(f"{title} ({version})")
        scores = []
        for name in names:
            r_bare = strip_album_decorations(name)
            norm_r = normalize(r_bare)
            for bare, bare_softened, norm_b in forms:
                s1 = similarity(r_bare, bare)
                s2 = similarity(r_bare, bare_softened) if bare_softened and bare_softened != bare else 0.0
                # Coverage + variant guards, mirroring find_album_dir_filesystem:
                # a 0.65-similar prefix match ('The North Borders' vs '… Tour -
                # Live') would otherwise pull a live/tour edition onto the
                # studio-album dir.
                if norm_r and norm_b:
                    coverage = min(len(norm_r), len(norm_b)) / max(len(norm_r), len(norm_b))
                    if coverage < config.FUZZY_DIR_MIN_COVERAGE:
                        continue
                    shorter_n, longer_n = sorted((norm_r, norm_b), key=len)
                    if differs_by_album_variant(shorter_n, longer_n):
                        continue
                scores.append(max(s1, s2))
        if scores:
            candidates.append((max(scores), _year_proximity_bonus(dir_year, r), r))

    if not candidates:
        return []
    _sort_by_match(candidates, prefer_hires)
    return [r for (s, _, r) in candidates if s >= config.ARTIST_DIR_MATCH_THRESH]


def match_dir_to_catalog(album_dir, catalog, artist_name, prefer_hires=False):
    """Best single candidate from the artist catalog (or None)."""
    cands = _catalog_candidates_for_dir(album_dir, catalog, artist_name, prefer_hires)
    if not cands:
        return None
    best = cands[0]
    vlog(f"    local catalog match: {best.get('title')!r}")
    return best


# Words that make a release something other than the album a plainly named
# folder holds. A dated remix ("2019 Remix", "Remixed 2015") is an edition of
# the whole album, not a remix release, so it doesn't count.
_OTHER_RELEASE_RE = re.compile(
    r"\b(live|remix(?:es|ed)?|single|acoustic|unplugged|instrumental|karaoke|"
    r"a\s?cappella|acapella)\b",
    re.IGNORECASE)
_DATED_REMIX_RE = re.compile(
    r"\b(?:(?:19|20)\d\d(?:\W+\w+){0,2}?\W+remix(?:ed)?"
    r"|remix(?:ed)?(?:\W+\w+){0,8}?\W+(?:19|20)\d\d)\b",
    re.IGNORECASE)
_ROMAN_RE = re.compile(r"(x{0,3})(ix|iv|v?i{0,3})")
_ROMAN_DIGITS = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
                 "vii": 7, "viii": 8, "ix": 9}
_NUMBER_WORDS = {word: n for n, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
# The format a folder name records rather than the album's own numbers:
# "[16-44.1]", "24bit 96kHz", "320kbps", "[2 CD]", "2xLP", "(2 Discs)".
_FORMAT_TAG_RE = re.compile(
    r"\b(?:16|20|24|32)(?:\s*-?\s*bits?\s*[-/_ ]?\s*|b?\s*[-/_ ]\s*)"
    r"(?:44[.,]1|48|88[.,]2|96|176[.,]4|192|352[.,]8|384)(?:\s*k(?:hz)?)?\b"
    r"|\b\d+(?:[.,]\d+)?\s*-?\s*(?:k?hz|kbps|bits?)\b"
    r"|\b\d+\s*[-x\u00d7]?\s*(?:cds?|discs?|disks?|lps?)\b",
    re.IGNORECASE)
_THOUSANDS_RE = re.compile(r"(?<=\d)[,.](?=\d{3}(?!\d))")


def _roman_value(word):
    match = _ROMAN_RE.fullmatch(word)
    if not match:
        return None
    return 10 * len(match.group(1)) + _ROMAN_DIGITS.get(match.group(2), 0)


def _title_numbers(title):
    """The numbers in an album title, roman numerals and number words
    included, so 'Jubileum I' and 'Jubileum II' or 'Vol. 1' and 'Vol. Two'
    read as different albums. A format tag such as '[24-96]' or '[2 CD]'
    is not one of the title's numbers, and '10,000' is one number."""
    title = _THOUSANDS_RE.sub("", _FORMAT_TAG_RE.sub(" ", title))
    numbers = set()
    for word in re.findall(r"[^\W_]+", title.lower()):
        if word.isdigit():
            numbers.add(int(word))
        elif word in _NUMBER_WORDS:
            numbers.add(_NUMBER_WORDS[word])
        elif (value := _roman_value(word)) is not None:
            numbers.add(value)
    return numbers


def _numbers_differ(a, b):
    """Whether two album titles name different albums by their numbers:
    'Bloodrock 2' and 'Bloodrock 3', or 'Van Halen' and 'Van Halen II'. A
    number only one title has is the same album when it is a 1, as in
    'Queen I', or a large one, as in 'Exodus 40' or 'Substance 1987'."""
    numbers_a, numbers_b = _title_numbers(a), _title_numbers(b)
    if numbers_a == numbers_b:
        return False
    return bool(numbers_a and numbers_b) or any(1 < n < 20 for n in numbers_a | numbers_b)


def _other_release_words(text):
    words = set()
    for word in _OTHER_RELEASE_RE.findall(_DATED_REMIX_RE.sub(" ", text)):
        word = re.sub(r"\W", "", word.lower())
        words.add("remix" if word.startswith("remix")
                  else "acappella" if word.startswith("acap") else word)
    return words


def _title_key(title):
    return normalize(re.sub(r"\s*&\s*", " and ", strip_album_decorations(title)))


def _full_title_key(title):
    return normalize(re.sub(r"\s*&\s*", " and ", _DIR_YEAR_PAREN_RE.sub("", title)))


def _pick_best_target_dir_match(cands, target_dir):
    """The candidate whose predicted on-disk path is target_dir and which is
    most likely the release the folder holds. cands are album dicts, best
    title score first.

    Ranked by title (the same bare title, then the same numbers in it), then
    whether it is a live, remix, single or acoustic release the folder name
    doesn't call itself, then the folder's year, then whether the edition
    words in the folder name match too, and only then by how well
    tracks_count fits the folder: its audio file count, or its highest track
    number when that is larger, so one file tagged track 8 isn't taken for a
    one-track single.
    """
    n_disk = max([_count_audio_files_in(target_dir)]
                 + [t.get("tracknumber") or 0 for t in read_album_dir(target_dir)])
    folder = target_dir.name
    folder_bare = strip_album_decorations(folder)
    folder_full = _full_title_key(folder)
    folder_key = _title_key(folder)
    folder_numbers = _title_numbers(folder_bare)
    folder_words = _other_release_words(folder)
    dir_year = _dir_year(folder)

    def rank(item):
        index, cand = item
        title = cand.get("title") or ""
        version = (cand.get("version") or "").strip()
        bare = strip_album_decorations(title)
        full_names = {_full_title_key(title)}
        names = {_title_key(title)}
        if version:
            full_names.add(_full_title_key(f"{title} ({version})"))
            names.add(_title_key(f"{title} ({version})"))
        if folder_key in names:
            title_rank = 0
        elif _title_numbers(bare) == folder_numbers:
            title_rank = 1
        else:
            title_rank = 2
        year = album_year_int(cand, None)
        if dir_year is None:
            year_rank = 0
        elif year is None:
            year_rank = 1
        else:
            gap = abs(year - dir_year)
            year_rank = 0 if gap <= 1 else gap
        other = bool(_other_release_words(f"{title} {version}") - folder_words)
        tc = cand.get("tracks_count") or 0
        if n_disk == 0:
            dist = 0
        elif tc == 0:
            dist = 10 ** 6  # unknown count → last resort
        elif tc >= n_disk:
            dist = tc - n_disk
        else:
            # Slight bias toward tc >= n_disk so a partial library (5/11)
            # still picks the 11-track edition over a 5-track release.
            dist = (n_disk - tc) * 1.5
        return (title_rank, other, year_rank, folder_full not in full_names,
                dist, index)

    resolving = []
    for index, cand in enumerate(cands):
        predicted = find_album_dir_filesystem(cand)
        if predicted is not None and _paths_equal(predicted, target_dir):
            resolving.append((index, cand))
    if not resolving:
        return None
    return min(resolving, key=rank)[1]


def find_qobuz_album_for_dir(album_dir: Path, artist_name: str, token,
                             prefer_hires=False, catalog=None, target_dir=None):
    """Search Qobuz for the album matching an existing dir. Returns a fully-
    populated album dict (with track list) or None. Picks the highest-
    similarity match between the dir's bare name (stripped of year/edition
    decorations) and the candidate's bare title.
    """
    # Fast path: try pre-fetched catalog first.
    if catalog:
        if target_dir is None:
            local = match_dir_to_catalog(album_dir, catalog, artist_name, prefer_hires)
            if local is not None:
                try:
                    return get_album(local["id"], token)
                except QobuzError as e:
                    log.info(fmt(C.YELLOW,
                        f"    ⚠  Catalog match get_album failed for "
                        f"album_id={local.get('id')} ({e}); trying per-folder search."))
        else:
            cands = _catalog_candidates_for_dir(
                album_dir, catalog, artist_name, prefer_hires)
            best = _pick_best_target_dir_match(cands, target_dir)
            if best is not None:
                vlog(f"    catalog target-dir match: {best.get('title')!r} "
                     f"(tc={best.get('tracks_count')})")
                try:
                    return get_album(best["id"], token)
                except QobuzError as e:
                    log.info(fmt(C.YELLOW,
                        f"    ⚠  Catalog target-dir match failed for "
                        f"album_id={best.get('id')} ({e}); trying per-folder search."))

    bare = strip_album_decorations(album_dir.name)

    # Build query variants. Underscore is how beets sanitizes :, /, etc;
    # replacing it with space matches Qobuz's actual titles much better.
    queries = [f"{artist_name} {bare}".strip()]
    softened = bare.replace("_", " ").replace("  ", " ").strip()
    if softened and softened != bare:
        queries.append(f"{artist_name} {softened}".strip())
    # Last-resort fallback: prefix only (everything before first sanitized char).
    # Lets us find "Juturna: Deluxe..." by searching "Circa Survive Juturna".
    prefix = re.split(r"[_:]", bare, maxsplit=1)[0].strip()
    if prefix and prefix != bare and len(prefix) >= 3:
        queries.append(f"{artist_name} {prefix}".strip())

    results = []
    seen_ids = set()
    for q in queries:
        try:
            batch = search_albums(q, token, limit=config.CATALOG_SEARCH_LIMIT)
        except QobuzError as e:
            vlog(f"    search variant {q!r} failed: {e}")
            continue
        for r in batch:
            rid = r.get("id")
            if rid and rid not in seen_ids:
                seen_ids.add(rid)
                results.append(r)
    if not results:
        return None

    # Year from dir name for the same self-titled-tie reason as
    # match_dir_to_catalog. See that function for the rationale.
    dir_year = _dir_year(album_dir.name)

    # Filter to lossless + matching artist
    candidates = []
    for r in results:
        if not is_lossless_album(r):
            continue
        r_artist = (r.get("artist") or {}).get("name", "")
        if similarity(r_artist, artist_name) < config.ARTIST_NAME_THRESH:
            continue
        r_bare = strip_album_decorations(r.get("title", ""))
        score = similarity(r_bare, bare)
        candidates.append((score, _year_proximity_bonus(dir_year, r), r))

    if not candidates:
        vlog(f"    no candidates after artist+lossless filter for {album_dir.name!r}")
        return None

    _sort_by_match(candidates, prefer_hires)
    # Threshold on the RAW score, not the year-boosted sort rank: a nearby-
    # year but title-weak candidate can sort first yet fail the threshold, so
    # don't let it shadow a higher-raw candidate that clears the bar.
    passing = [c for c in candidates if c[0] >= config.ARTIST_DIR_MATCH_THRESH]
    if not passing:
        top = candidates[0]
        vlog(f"    best Qobuz match {top[2].get('title')!r} scored "
             f"{top[0]:.2f} - under threshold")
        return None
    best_score, _best_year_bonus, best = passing[0]

    # When a specific on-disk target is required, pick among the viable
    # candidates whose predicted path resolves back to target_dir.
    if target_dir is not None:
        best = _pick_best_target_dir_match([r for (_s, _yb, r) in passing],
                                           target_dir)
        if best is not None:
            vlog(f"    Qobuz match (target-dir aligned): {best.get('title')!r} "
                 f"(tc={best.get('tracks_count')})")
            try:
                return get_album(best["id"], token)
            except QobuzError as e:
                log.info(fmt(C.YELLOW, f"    ⚠  Couldn't fetch album details: {e}."))
                return None
        vlog(f"    no candidate resolves to {target_dir.name!r}; returning None")
        return None

    vlog(f"    Qobuz match: {best.get('title')!r} (score {best_score:.2f})")

    # Pull the full track list
    try:
        return get_album(best["id"], token)
    except QobuzError as e:
        log.info(fmt(C.YELLOW, f"    ⚠  Couldn't fetch album details: {e}."))
        return None


def find_expanded_edition(album, album_dir, existing, token, _args=None):
    """When a Qobuz edition produces extras that would block an upgrade, search
    for alternate editions whose track count covers the local library. Only
    releases the files hold as the same edition (same_edition) count, not
    another mix or master.

    Returns a list of (full_album_dict, new_extras_list) tuples for editions
    that don't make the extras-blocking-upgrade situation worse. Sorted: fewest
    new_extras first (zero is best - covers all local tracks), then by quality
    descending ('quality is king'), then by track count descending.

    Limits to 3 full get_album() calls to keep API usage sane. Returns []
    (empty list) if nothing viable is found.
    """
    if not token or album_dir is None:
        return []

    artist_name = (album.get("artist") or {}).get("name", "") or ""
    bare = strip_album_decorations(album.get("title") or "")
    if not bare or not artist_name:
        return []

    n_local = len(existing)

    # Build the same query variants as find_qobuz_album_for_dir.
    queries = [f"{artist_name} {bare}".strip()]
    softened = bare.replace("_", " ").replace("  ", " ").strip()
    if softened != bare:
        queries.append(f"{artist_name} {softened}".strip())
    prefix = re.split(r"[_:]", bare, maxsplit=1)[0].strip()
    if prefix and prefix != bare and len(prefix) >= 3:
        queries.append(f"{artist_name} {prefix}".strip())

    seen_ids = {album.get("id")}  # skip the edition we already have
    candidates = []
    for q in queries:
        try:
            batch = search_albums(q, token, limit=config.CATALOG_SEARCH_LIMIT)
        except QobuzError:
            continue
        for r in batch:
            rid = r.get("id")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            if not is_lossless_album(r):
                continue
            r_artist = (r.get("artist") or {}).get("name", "") or ""
            if similarity(r_artist, artist_name) < config.ARTIST_NAME_THRESH:
                continue
            r_bare = strip_album_decorations(r.get("title") or "")
            score = similarity(r_bare, bare)
            if score < config.ARTIST_DIR_MATCH_THRESH:
                continue
            tc = r.get("tracks_count") or 0
            # Must have enough tracks to plausibly cover local collection.
            if tc < n_local - 1:
                continue
            candidates.append(r)

    if not candidates:
        return []

    # Sort by quality descending (quality is king), then track count
    # descending.
    candidates.sort(key=lambda r: (
        -(r.get("maximum_bit_depth") or 0),
        -(r.get("maximum_sampling_rate") or 0),
        -(r.get("tracks_count") or 0),
    ))

    current_extras_count = len(find_extras_in_existing(
        (album.get("tracks") or {}).get("items") or [], existing))

    found = []
    api_calls = 0
    for cand in candidates:
        if api_calls >= config.EDITION_SEARCH_API_BUDGET:
            break
        try:
            full = get_album(cand["id"], token)
            api_calls += 1
        except QobuzError:
            continue
        q_tracks = (full.get("tracks") or {}).get("items") or []
        if not q_tracks or not same_edition(full, existing, album_dir, token):
            continue
        new_extras = find_extras_in_existing(q_tracks, existing)
        # Keep candidates that are no worse than current on the extras axis.
        if len(new_extras) <= current_extras_count:
            found.append((full, new_extras))
            vlog(f"    candidate edition: {full.get('title')!r} "
                 f"({len(new_extras)} extras vs {current_extras_count}, "
                 f"{album_quality_label(full)})")

    # Sort returned candidates: fewest new_extras first (zero is best),
    # then quality descending, then track count descending.
    found.sort(key=lambda fe: (
        len(fe[1]),
        -(fe[0].get("maximum_bit_depth") or 0),
        -(fe[0].get("maximum_sampling_rate") or 0),
        -len((fe[0].get("tracks") or {}).get("items") or []),
    ))
    return found
