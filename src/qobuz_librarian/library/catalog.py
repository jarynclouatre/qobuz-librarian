"""Album/artist catalog matching and track-presence detection.

Behaviour you should not change without understanding the consequence:

- find_album_dir_filesystem: exact-path fast path first, then fuzzy scan.
  Artist-variant expansion covers "The X" / "X" / comma-prefix forms.
  Multi-artist folder detection (_MULTI_ARTIST_SEPS) expands search dirs.
- compute_missing: 3-layer matching chain in order —
    1. ISRC — authoritative recording identity
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
import ctypes
import errno
import math
import os
import re
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.api.auth import QobuzError
from qobuz_librarian.api.search import get_album, search_albums
from qobuz_librarian.library.scanner import (
    _list_artist_subdirs_cached,
    iter_tree_no_symlinks,
    read_album_dir,
)
from qobuz_librarian.library.sqlite_atomic import inspect_sqlite_source
from qobuz_librarian.library.tags import (
    beets_sanitize,
    canonical_recording_id,
    canonical_track_slot,
    canonical_track_title,
    differs_by_album_variant,
    normalize,
    similarity,
    strip_album_decorations,
    strip_edition_suffix,
    strip_leading_article,
    strip_year_decoration,
)
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog

# ── Multi-artist folder detection ─────────────────────────────────────────────
# Beets writes ALBUMARTIST = "X, Y" for multi-artist releases.
_MULTI_ARTIST_SEPS = (
    ", ", " & ", " and ", " feat. ", " feat ",
    " ft. ", " ft ", " with ", " x ", " vs. ", " vs ",
)
# Migration uses comma-space ONLY — other separators appear in real band
# names ('Bob Marley & The Wailers') and auto-migrating those would
# silently fold entire artists' catalogs into a lead-member folder.
_MIGRATION_SEPS = (", ",)
_PRIMARY_ARTIST_SEPS = (", ", " & ", " and ", " feat. ", " feat ", " ft. ", " ft ")


def _primary_artist_of(qartist):
    """Return the primary (first) name from a multi-artist string.

    Qobuz returns the same album with different artist-string formats
    depending on which edition is queried: "Jay Z and Kanye West"
    (album-level) vs. "Jay Z, Kanye West" (track-level). The migration
    check needs a canonical primary so it matches the on-disk folder
    regardless of which form Qobuz happened to return.
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


def _find_multi_artist_dirs(qartist):
    """Return MUSIC_ROOT children whose name is 'qartist<sep><other>'.
    Uses the scan cache so artist-mode runs don't re-iterdir."""
    if not qartist or not config.MUSIC_ROOT.exists():
        return []
    matches = []
    for d in _list_artist_subdirs_cached(config.MUSIC_ROOT):
        if _is_multi_artist_subset(d.name, qartist):
            matches.append(d)
    return matches


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
    r"|^(\d{4})\s*[-–—]\s+"      # leading '2010 - Title'
)


def _decoration_year(name):
    """The 4-digit year a folder name uses as a decoration ('' if none).

    A bare year that is itself the title ('1984', '2112') is not a decoration;
    a year in a clear slot — trailing '(2010)'/'[2010]', or a leading '[2010] '
    or '2010 - ' prefix — is. Mirrors tags.py's year-stripping rules."""
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
def predicted_album_paths(qobuz_album):
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

    artist_dirs = [config.MUSIC_ROOT / artist]
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
    yr = album_year(qobuz_album)
    if yr:
        years.append(yr)

    # Also look under the de-editioned title, in case the user's folder drops
    # the tag ('Album (Deluxe)' → also 'Album').
    bare_titles = [title]
    stripped = strip_album_decorations(title)
    if stripped and stripped != title:
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
        for bt in bare_titles:
            for year in years:
                candidates.append(
                    config.MUSIC_ROOT / "Various Artists" / f"{bt} ({year})")
                candidates.append(
                    config.MUSIC_ROOT / "Various Artists" / f"[{year}] {bt}")
                candidates.append(
                    config.MUSIC_ROOT / "Various Artists" / f"{year} - {bt}")
            candidates.append(config.MUSIC_ROOT / "Various Artists" / bt)

    return candidates


def find_album_dir_filesystem(qobuz_album):
    """Resolve a Qobuz album dict to its on-disk directory.

    Fast path: check predicted_album_paths() against cached subdir listings.
    Fuzzy path: expand artist variants ('The X' / 'X' / comma-prefix) and
    scan each parent dir for the best-scoring album folder.
    """
    candidates = predicted_album_paths(qobuz_album)
    vlog(f"predicted {len(candidates)} candidate path(s)")

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
        ad = config.MUSIC_ROOT / beets_sanitize(av)
        # is_dir, not exists: a stray regular file with an artist's name would
        # blow up the subdir listing below with NotADirectoryError.
        if ad.is_dir():
            _push(ad)
        for ma in _find_multi_artist_dirs(av):
            _push(ma)

    # Diacritics often differ between Qobuz and disk ('Beyoncé' vs 'Beyonce',
    # 'Motörhead' vs 'Motorhead'); the exact .exists() checks above miss
    # those, so also take any artist folder whose ASCII-folded name matches.
    artist_norms = {normalize(av) for av in artist_variants if normalize(av)}
    if artist_norms and config.MUSIC_ROOT.exists():
        for d in _list_artist_subdirs_cached(config.MUSIC_ROOT):
            if normalize(d.name) in artist_norms:
                _push(d)

    global_best, global_best_score = None, 0.0
    norm_title = normalize(strip_album_decorations(title))
    for artist_dir in search_dirs:
        vlog(f"scanning artist dir: {artist_dir}")
        subdirs = _list_artist_subdirs_cached(artist_dir)
        if not subdirs:
            continue

        stripped_title = strip_album_decorations(title)
        # Evaluate every subdir that clears both gates, not just the single
        # top scorer: a prefix-only high scorer that fails the coverage gate
        # (studio 'The North Borders' scoring 0.79 against the live 'The North
        # Borders Tour — Live') must not mask a lower-scored folder in the same
        # artist dir that is the real match.
        for d in subdirs:
            score = similarity(strip_album_decorations(d.name), stripped_title)
            if score < config.FUZZY_DIR_THRESH or score <= global_best_score:
                continue
            # Real edition variants normalize to the same string, so require the
            # titles to be close in length — that keeps an extra word ('Live',
            # 'Tour') from resolving to the base album's folder on prefix alone.
            norm_dir = normalize(strip_album_decorations(d.name))
            coverage = (min(len(norm_dir), len(norm_title))
                        / max(len(norm_dir), len(norm_title))
                        if norm_dir and norm_title else 0.0)
            if coverage < config.FUZZY_DIR_MIN_COVERAGE:
                vlog(f"rejected {d.name}: score {score:.2f} but length "
                     f"coverage {coverage:.2f} < {config.FUZZY_DIR_MIN_COVERAGE}")
                continue
            global_best, global_best_score = d, score
            vlog(f"fuzzy match: {d}  (score {score:.2f})")
    return global_best


_DISC_FOLDER_RE = re.compile(r"^(?:disc|cd)\s*\d+", re.IGNORECASE)


def track_signatures_for_album_dirs(album_dirs):
    """Return tag signatures for staged/import-candidate FLACs.

    These signatures survive beets moving/renaming files, so callers can find
    the imported folder when Qobuz's artist/title prediction misses.
    """
    from qobuz_librarian.integrations.rip import _flac_signature

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
            sig = _flac_signature(fp)
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
        direct = config.MUSIC_ROOT / beets_sanitize(artist)
        if direct.exists():
            _push(direct)

    wanted_norms = {normalize(a) for a in artist_names if normalize(a)}
    if wanted_norms and config.MUSIC_ROOT.exists():
        for d in _list_artist_subdirs_cached(config.MUSIC_ROOT):
            if normalize(d.name) in wanted_norms:
                _push(d)
    return dirs


def find_album_dir_by_track_signatures(signatures):
    """Find the album folder that contains imported tracks with these tags.

    This is a narrow fallback for post-import code. It is only needed when
    `find_album_dir_filesystem()` cannot predict where beets filed the album,
    usually because Qobuz's album artist string differs from the FLAC tags.
    """
    wanted = {tuple(sig) for sig in signatures or [] if sig}
    if not wanted:
        return None

    from qobuz_librarian.integrations.rip import _flac_signature

    matches = {}
    for artist_dir in _artist_dirs_for_track_signatures(wanted):
        try:
            flacs = sorted(p for p in artist_dir.rglob("*.flac") if p.is_file())
        except OSError:
            continue
        for fp in flacs:
            sig = _flac_signature(fp)
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
    return complete[0] if len(complete) == 1 else None


def find_existing_tracks(qobuz_album, *, album_dir=None):
    """Return (track_list, album_dir_or_None) by reading the filesystem.

    track_list is empty when no album dir is found — surfaces as
    '0 of N present' so the user can spot a folder-naming mismatch.

    Pass ``album_dir`` when the caller already resolved it — the artist
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

    If either side is entirely on disc 1 — a single-disc album, or a flat folder
    whose tracks carry no DISCNUMBER tag and so all default to 1 — disc scoping
    can only invent false gaps (a later-disc Qobuz track never finding its
    disc-1 file). Matching disc-agnostically then is safe: one-to-one pairing
    still keeps two same-titled tracks from collapsing onto a single file."""
    return (_disc_count(qobuz_tracks, "media_number") > 1
            and _disc_count(existing_tracks, "discnumber") > 1)


def _qobuz_keys(qobuz_tracks, disc_scoped=True):
    return [_track_key(qt.get("title"), qt.get("media_number", 1) or 1,
                       qt.get("isrc"), disc_scoped=disc_scoped)
            for qt in qobuz_tracks]


def _existing_keys(existing_tracks, disc_scoped=True):
    return [_track_key(et.get("title"), et.get("discnumber", 1) or 1,
                       et.get("isrc"), disc_scoped=disc_scoped)
            for et in existing_tracks]


def compute_missing(qobuz_tracks, existing_tracks):
    """Split qobuz_tracks into (missing, present) against existing_tracks.

    A Qobuz track is present when an on-disk track matches it on ISRC,
    normalized title, or edition-stripped title — in that priority. Matching is
    one-to-one (see _pair_tracks), so a duplicate title surfaces as a real gap
    instead of being masked by a single file.
    """
    disc_scoped = _disc_scoped_match(qobuz_tracks, existing_tracks)
    claimed = _pair_tracks(_qobuz_keys(qobuz_tracks, disc_scoped),
                           _existing_keys(existing_tracks, disc_scoped))
    missing = [qt for i, qt in enumerate(qobuz_tracks) if not claimed[i]]
    present = [qt for i, qt in enumerate(qobuz_tracks) if claimed[i]]
    return missing, present


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


def folder_holds_all_tracks(folder, qobuz_tracks, destructive=False):
    """True only when every expected Qobuz track one-to-one matches an audio
    file under ``folder``.

    This gates gap-fill backup deletion: a raw file count can be satisfied by
    extras, duplicate files, or pre-existing tracks while an expected track is
    absent, and the backup being deleted holds the only copy of the tracks
    moved aside. No expected list, an unreadable folder, or a walk that
    couldn't cover the whole tree reads as False — unverifiable means keep
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
    if destructive:
        return _destructive_track_coverage(qobuz_tracks, tracks)
    still_missing, _ = compute_missing(qobuz_tracks, tracks)
    return not still_missing


def pair_existing_tracks(a_tracks, b_tracks):
    """One-to-one pair two ON-DISK track lists with the scan's matcher.

    Returns (pairs, unpaired_a): ``pairs`` as (a_track, b_track) tuples,
    ``unpaired_a`` the a-side tracks that claimed nothing. This keeps discovery
    semantics and must not authorize destructive cleanup; backup-disposal
    callers use the strict matching path above."""
    disc_scoped = (_disc_count(a_tracks, "discnumber") > 1
                   and _disc_count(b_tracks, "discnumber") > 1)
    indices = _pair_indices(_existing_keys(a_tracks, disc_scoped),
                            _existing_keys(b_tracks, disc_scoped))
    pairs = [(a_tracks[ai], b_tracks[si])
             for ai, si in enumerate(indices) if si is not None]
    unpaired = [a_tracks[ai]
                for ai, si in enumerate(indices) if si is None]
    return pairs, unpaired


def find_extras_in_existing(qobuz_tracks, existing_tracks):
    """Return on-disk tracks that match no Qobuz track.

    The reverse of compute_missing: each Qobuz track accounts for at most one
    file, so a same-titled bonus track (a custom rip, a B-side, a deluxe extra)
    is flagged rather than hidden. This gates the upgrade wipe-and-replace, so a
    missed extra means silently deleting a track the user can't get back.
    """
    disc_scoped = _disc_scoped_match(qobuz_tracks, existing_tracks)
    matched = _pair_tracks(_existing_keys(existing_tracks, disc_scoped),
                           _qobuz_keys(qobuz_tracks, disc_scoped))
    return [et for i, et in enumerate(existing_tracks) if not matched[i]]


# ── Album metadata helpers ────────────────────────────────────────────────────
def album_year(album):
    """Album release year as a string, or '' if unknown.

    Prefers release_date_original. Falls back to released_at parsed in UTC
    (not local timezone — local-TZ parsing was a real bug that flipped the
    year for albums released late at night UTC).
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
    enough on its own — a 2020 album it only just listed is a back-catalogue gap,
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
                       # album — ignored when sizing the standard edition.


def _standard_track_count(group):
    """Track count of the real album among a group of editions.

    Deluxe/anniversary/expanded editions only ADD tracks and remasters keep
    the original count, so the smallest real edition is the album. None when
    no edition reports a usable count.

    Editions below ``_MIN_ALBUM_TRACKS`` (3 tracks or fewer) are treated as
    stray singles/EPs that happen to dedup into the same group as the album
    by title and ignored when sizing — so a same-titled 3-track EP filed
    next to a 15-track deluxe doesn't win the canonical pick. (An EP with
    4+ tracks that shares the album's normalized name is a rarer edge that
    needs Qobuz release-type metadata to distinguish reliably; until then
    it can still mislead the canonical pick.)
    """
    counts = [tc for a in group if (tc := a.get("tracks_count") or 0) > 0]
    if not counts:
        return None
    full = [c for c in counts if c >= _MIN_ALBUM_TRACKS]
    return min(full) if full else min(counts)


def _is_decorated_edition(album):
    """True if the title carries an edition tag (remaster, deluxe, anniversary,
    a year, ...) rather than being the plain original release."""
    title = album.get("title") or ""
    return (strip_album_decorations(title).casefold().strip()
            != title.casefold().strip())


def _best_edition(group, prefer_hires):
    """Representative edition for a group of same-album editions.

    Both modes target the standard track count so a padded deluxe/anniversary
    edition never wins on track count alone. prefer_hires then takes the best
    resolution at that count; otherwise it takes the original (untagged)
    edition without chasing a remaster's higher resolution.
    """
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
        _is_decorated_edition(a),
        album_year_int(a),
        str(a.get("id") or ""),
    ))


def filter_owned_albums(catalog_pairs, owned_bare_titles, artist_name=None):
    """Drop catalog entries whose bare title matches something already owned.

    An exact bare-title match normally counts as owned regardless of year — a
    far-off year is a remaster/reissue of the same work (a 2022 'Revolver' is the
    1966 one re-released, not a new album). The one exception is a *self-titled*
    release: when the bare title equals the artist name and the catalog entry is
    undecorated and its year is far from every owned copy, it's usually a
    genuinely distinct same-titled album (Weezer's colour records, Peter
    Gabriel's four 'Peter Gabriel' LPs) rather than a reissue — so it's offered
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
            if differs_by_album_variant(shorter, longer):
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


def dedup_album_versions(albums, prefer_hires=False):
    """Collapse multiple editions of the same album into one canonical entry.

    Two albums dedup if their stripped-decoration titles normalize identically
    AND share the same release year. Different years are kept as separate groups
    so self-titled albums from different years (Weezer's colour records, Peter
    Gabriel's four eponymous LPs) are not collapsed into one canonical entry.
    Within a group, picks (see _best_edition):
      - prefer_hires=True:  best resolution at the standard track count
      - prefer_hires=False: the original (untagged) edition
    The standard track count is the smallest real edition, so a padded
    deluxe/anniversary release never wins on track count alone.

    Returns list of (canonical_album, n_versions_in_group) tuples, sorted by
    canonical's release year ascending.
    """
    groups = {}
    for a in albums:
        bare = strip_album_decorations(a.get("title") or "")
        # Pure-CJK / emoji titles normalize to '' (ASCII-fold drops them);
        # fall back to the raw title so they aren't silently dropped from the
        # missing list. Only a genuinely empty title is skipped.
        key_title = normalize(bare) or bare.strip().casefold()
        if not key_title:
            continue
        # Include year in the group key.
        key = (key_title, album_year_int(a))
        groups.setdefault(key, []).append(a)

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

def maybe_remove_empty_dir(d: Path):
    """Remove dir only if it has no remaining files (cover art etc preserved)."""
    try:
        children = list(iter_tree_no_symlinks(d))
        if any(p.is_file() for p in children):
            return False
        # Remove empty subdirs deepest-first
        subdirs = sorted((p for p in children if p.is_dir()),
                         key=lambda p: -len(p.parts))
        for sd in subdirs:
            try: sd.rmdir()
            except OSError: pass
        d.rmdir()
        return True
    except OSError:
        return False


def _count_audio_files_in(d):
    """Count audio files recursively in a directory. 0 if missing."""
    if d is None or not d.exists():
        return 0
    n = 0
    try:
        for f in iter_tree_no_symlinks(d):
            if f.is_file() and f.suffix.lower() in config.AUDIO_EXTS:
                n += 1
    except OSError:
        pass
    return n


_RENAME_NOREPLACE = 1


def _open_migration_directory(path, *, dir_fd=None):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe directory access is unavailable")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, dir_fd=dir_fd)


def _renameat2_at(source_fd, source_name, destination_fd, destination_name,
                  flags):
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        int(source_fd),
        os.fsencode(source_name),
        int(destination_fd),
        os.fsencode(destination_name),
        int(flags),
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination_name))


def _rename_noreplace_at(source_fd, source_name, destination_fd,
                         destination_name):
    _renameat2_at(
        source_fd,
        source_name,
        destination_fd,
        destination_name,
        _RENAME_NOREPLACE,
    )



def _migration_name_missing(parent_fd, name):
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return False


class _MigrationEntryPreserved(OSError):
    """Carry a concrete private recovery location without printing it."""

    def __init__(self, location, message):
        self.location = os.fspath(location)
        super().__init__(errno.EBUSY, message)


def _migration_descriptor_path(descriptor):
    try:
        value = os.readlink(f"/proc/self/fd/{int(descriptor)}")
    except (OSError, TypeError, ValueError) as exc:
        raise OSError(
            "preserved migration entry has no stable recovery path"
        ) from exc
    if value.endswith(" (deleted)") or not Path(value).is_absolute():
        raise OSError("preserved migration entry has no stable recovery path")
    return Path(value)


def _migration_entry_preserved(parent_fd, name, message):
    return _MigrationEntryPreserved(
        _migration_descriptor_path(parent_fd) / name,
        message,
    )


def _migration_absolute_parts(value):
    raw = os.fspath(value)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise OSError("invalid absolute path")
    absolute = Path(os.path.abspath(raw))
    try:
        relative = absolute.relative_to(Path(os.path.sep))
    except ValueError:
        raise OSError("path has no stable filesystem anchor") from None
    return absolute, tuple(relative.parts)


def _open_absolute_migration_chain(value):
    """Hold every no-follow component from `/` through one directory."""
    _absolute, names = _migration_absolute_parts(value)
    chain = [_open_migration_directory(os.path.sep)]
    try:
        for name in names:
            child = _open_migration_directory(name, dir_fd=chain[-1])
            if not _migration_named_directory_matches(
                    chain[-1], name, child):
                os.close(child)
                raise OSError("migration directory changed while it was opened")
            chain.append(child)
        return chain, names
    except BaseException:
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _migration_chain_is_named(chain, names):
    if len(chain) != len(names) + 1:
        return False
    return all(
        _migration_named_directory_matches(
            chain[index], name, chain[index + 1])
        for index, name in enumerate(names)
    )


def _open_migration_database_anchor():
    configured = getattr(config, "BEETS_DB_PATH", None)
    if not configured:
        return None
    database, parts = _migration_absolute_parts(configured)
    if not parts:
        raise OSError("invalid beets database path")
    parent_chain, parent_names = _open_absolute_migration_chain(
        database.parent)
    descriptor = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe beets database access is unavailable")
        try:
            descriptor = os.open(
                database.name,
                os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_chain[-1],
            )
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None and not stat.S_ISREG(
                os.fstat(descriptor).st_mode):
            raise OSError("beets database is not a regular file")
        anchor = {
            "path": database,
            "parent_chain": parent_chain,
            "parent_names": parent_names,
            "name": database.name,
            "descriptor": descriptor,
        }
        if not _migration_database_anchor_matches(anchor):
            raise OSError("configured beets database changed while it was opened")
        return anchor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        for directory_fd in reversed(parent_chain):
            os.close(directory_fd)
        raise


def _migration_database_anchor_matches(anchor):
    if anchor is None:
        return True
    try:
        configured = getattr(config, "BEETS_DB_PATH", None)
        if not configured:
            return False
        current, _parts = _migration_absolute_parts(configured)
        if current != anchor["path"] or not _migration_chain_is_named(
                anchor["parent_chain"], anchor["parent_names"]):
            return False
        descriptor = anchor["descriptor"]
        if descriptor is None:
            return _migration_name_missing(
                anchor["parent_chain"][-1], anchor["name"])
        return (
            stat.S_ISREG(os.fstat(descriptor).st_mode)
            and _migration_named_entry_matches(
                anchor["parent_chain"][-1],
                anchor["name"],
                descriptor,
            )
        )
    except (OSError, TypeError, ValueError):
        return False


def _close_migration_database_anchor(anchor):
    if anchor is None:
        return
    descriptor = anchor.get("descriptor")
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            pass
    for directory_fd in reversed(anchor.get("parent_chain", ())):
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _require_no_migration_items_triggers(connection):
    trigger = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name = 'items' COLLATE NOCASE LIMIT 1"
    ).fetchone()
    if trigger is not None:
        raise sqlite3.DatabaseError(
            "custom items triggers make path updates unverifiable")


def _require_migration_delete_journal_mode(connection):
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
        raise sqlite3.DatabaseError(
            "safe path updates require SQLite delete-journal mode")


def _prepare_migration_database_transaction_name(anchor):
    """Compatibility gate; SQLite must never use a private database name."""
    if (
        anchor is not None
        and anchor["descriptor"] is not None
        and not _migration_database_anchor_matches(anchor)
    ):
        raise OSError("configured beets database changed before SQLite binding")



def _preflight_migration_database_anchor(anchor, anchors_match=None):
    """Reject an unsafe DB before its associated filesystem mutation."""
    if anchor is None:
        return
    if not _migration_database_anchor_matches(anchor):
        raise OSError("configured beets database changed before the move")
    if anchor["descriptor"] is None:
        return
    try:
        timeout = getattr(config, "BEETS_TIMEOUT", 5)
        timeout = float(timeout) if timeout and timeout > 0 else 5.0
        inspect_sqlite_source(
            anchor,
            _migration_database_anchor_matches,
            lambda connection: (
                _require_no_migration_items_triggers(connection),
                _require_migration_delete_journal_mode(connection),
            ),
            connect=sqlite3.connect,
            timeout=timeout,
            anchors_match=anchors_match,
        )
    except sqlite3.Error as exc:
        raise OSError(f"beets database preflight failed: {exc}") from exc


def _migration_named_entry_matches(parent_fd, name, entry_fd):
    try:
        held = os.fstat(entry_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        return False
    return (
        stat.S_IFMT(held.st_mode) == stat.S_IFMT(named.st_mode)
        and (int(held.st_dev), int(held.st_ino))
        == (int(named.st_dev), int(named.st_ino))
    )


def _migration_named_directory_matches(parent_fd, name, directory_fd):
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            return False
    except (OSError, TypeError, ValueError):
        return False
    return _migration_named_entry_matches(parent_fd, name, directory_fd)


def _migration_rename_layout(source_fd, source_name, destination_fd,
                             destination_name, entry_fd):
    source_matches = _migration_named_entry_matches(
        source_fd, source_name, entry_fd)
    destination_matches = _migration_named_entry_matches(
        destination_fd, destination_name, entry_fd)
    if source_matches and _migration_name_missing(
            destination_fd, destination_name):
        return "source"
    if destination_matches and _migration_name_missing(source_fd, source_name):
        return "destination"
    return "uncertain"


def _migration_entry_is_unlinked(parent_fd, name, entry_fd):
    try:
        value = os.fstat(entry_fd)
    except (OSError, TypeError, ValueError):
        return False
    return int(value.st_nlink) == 0 and _migration_name_missing(parent_fd, name)


def _migration_entry_record(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(stat.S_IFMT(value.st_mode)),
    )



def _fsync_migration_directories(*descriptors):
    synced = set()
    for descriptor in descriptors:
        value = os.fstat(descriptor)
        if not stat.S_ISDIR(value.st_mode):
            raise OSError(errno.ENOTDIR, "migration anchor is not a directory")
        identity = (int(value.st_dev), int(value.st_ino))
        if identity in synced:
            continue
        os.fsync(descriptor)
        synced.add(identity)


def _open_migration_handle(parent_fd, name):
    path_only = getattr(os, "O_PATH", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if path_only is None or nofollow is None:
        raise OSError("safe migration handles are unavailable")
    descriptor = os.open(
        name,
        path_only | nofollow | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    if not _migration_named_entry_matches(parent_fd, name, descriptor):
        os.close(descriptor)
        raise OSError("migration entry changed while it was opened")
    return descriptor


def _reserve_migration_directory_at(parent_fd, *, prefix, mode=0o700):
    for _ in range(16):
        name = f".{prefix}-{secrets.token_hex(12)}"
        try:
            os.mkdir(name, mode=mode, dir_fd=parent_fd)
        except FileExistsError:
            continue
        descriptor = None
        try:
            descriptor = _open_migration_directory(name, dir_fd=parent_fd)
            if not _migration_named_directory_matches(
                    parent_fd, name, descriptor):
                raise OSError("reserved migration directory changed")
            _fsync_migration_directories(parent_fd)
            return name, descriptor
        except BaseException:
            if descriptor is None:
                try:
                    descriptor = _open_migration_directory(
                        name, dir_fd=parent_fd)
                except OSError:
                    descriptor = None
            if (
                descriptor is not None
                and _migration_named_directory_matches(
                    parent_fd, name, descriptor)
            ):
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                    _fsync_migration_directories(parent_fd)
                except OSError:
                    pass
            if descriptor is not None:
                os.close(descriptor)
            raise
    raise OSError("could not reserve a private migration directory")


def _remove_reserved_migration_directory_at(parent_fd, name, directory_fd):
    if not _migration_named_directory_matches(parent_fd, name, directory_fd):
        return False
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except BaseException:
        if not _migration_entry_is_unlinked(parent_fd, name, directory_fd):
            return False
    if not _migration_entry_is_unlinked(parent_fd, name, directory_fd):
        return False
    try:
        _fsync_migration_directories(parent_fd)
    except BaseException:
        return False
    return True


def _empty_migration_directory_state(
        parent_fd, name, temporary_name, directory_fd):
    if _migration_named_directory_matches(parent_fd, name, directory_fd):
        return "public"
    if _migration_named_directory_matches(
            parent_fd, temporary_name, directory_fd):
        return "quarantined"
    try:
        held = os.fstat(directory_fd)
    except (OSError, TypeError, ValueError):
        return "uncertain"
    if (
        stat.S_ISDIR(held.st_mode)
        and int(held.st_nlink) == 0
        and _migration_name_missing(parent_fd, name)
        and _migration_name_missing(parent_fd, temporary_name)
    ):
        return "removed"
    return "uncertain"


def _restore_empty_migration_directory_at(
        parent_fd, name, temporary_name, directory_fd):
    state = _empty_migration_directory_state(
        parent_fd, name, temporary_name, directory_fd)
    if state == "public":
        return True
    if state != "quarantined":
        return False
    try:
        restored = _rollback_migration_rename(
            parent_fd,
            name,
            parent_fd,
            temporary_name,
            directory_fd,
        )
    except _MigrationEntryPreserved:
        raise
    except BaseException:
        restored = False
    if restored:
        return True
    if _empty_migration_directory_state(
            parent_fd, name, temporary_name, directory_fd) != "public":
        return False
    try:
        _fsync_migration_directories(parent_fd)
    except BaseException:
        return False
    return True


def _restore_any_migration_entry_at(parent_fd, name, temporary_name):
    """Put an unexpectedly quarantined entry back without overwriting."""
    if (
        not _migration_name_missing(parent_fd, name)
        or _migration_name_missing(parent_fd, temporary_name)
    ):
        return False
    moved_fd = None
    try:
        moved_fd = _open_migration_handle(parent_fd, temporary_name)
        return _rollback_migration_rename(
            parent_fd,
            name,
            parent_fd,
            temporary_name,
            moved_fd,
        )
    except _MigrationEntryPreserved:
        raise
    except BaseException:
        return False
    finally:
        if moved_fd is not None:
            os.close(moved_fd)


def _rename_exact_migration_entry_at(
        source_parent_fd, source_name,
        destination_parent_fd, destination_name, expected_fd):
    """Move one exact entry, restoring a replacement moved at the boundary."""
    if _migration_rename_layout(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            expected_fd) != "source":
        raise OSError("exact migration rename precondition changed")
    deferred = None
    try:
        _rename_noreplace_at(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )
    except BaseException as exc:
        deferred = exc
    if _migration_rename_layout(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            expected_fd) == "destination":
        sync_error = None
        try:
            _fsync_migration_directories(
                source_parent_fd, destination_parent_fd)
        except BaseException as exc:
            sync_error = exc
        return deferred or sync_error

    if _migration_name_missing(source_parent_fd, source_name):
        unexpected_fd = None
        try:
            unexpected_fd = _open_migration_handle(
                destination_parent_fd, destination_name)
            if _rollback_migration_rename(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                    unexpected_fd):
                raise OSError(
                    "migration entry changed during cleanup; the "
                    "replacement was restored"
                ) from deferred
        except FileNotFoundError:
            pass
        finally:
            if unexpected_fd is not None:
                os.close(unexpected_fd)
    if (
        deferred is not None
        and _migration_rename_layout(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            expected_fd,
        ) == "source"
    ):
        raise deferred
    raise OSError("exact migration rename outcome is uncertain") from deferred


def _remove_private_empty_migration_directory_at(
        parent_fd, temporary_name, directory_fd):
    """Remove an exact held directory behind a fresh private handoff."""
    private_name = None
    private_fd = None
    moved = False

    def restore_exact() -> bool:
        nonlocal moved
        if private_fd is None:
            return False
        restored = _rollback_migration_rename(
            parent_fd,
            temporary_name,
            private_fd,
            "candidate",
            directory_fd,
        )
        if restored:
            moved = False
        return restored

    try:
        private_name, private_fd = _reserve_migration_directory_at(
            parent_fd, prefix="qobuz-migrate-discard")
        deferred = _rename_exact_migration_entry_at(
            parent_fd,
            temporary_name,
            private_fd,
            "candidate",
            directory_fd,
        )
        moved = _migration_named_directory_matches(
            private_fd, "candidate", directory_fd)
        if not moved:
            return False
        if deferred is not None:
            if not restore_exact():
                raise _migration_entry_preserved(
                    private_fd,
                    "candidate",
                    "Migration cleanup could not restore its held directory. "
                    "The directory was kept for review.",
                ) from deferred
            if isinstance(deferred, OSError):
                return False
            raise deferred
        try:
            changed = bool(os.listdir(directory_fd))
        except BaseException as exc:
            if not restore_exact():
                raise _migration_entry_preserved(
                    private_fd,
                    "candidate",
                    "Migration cleanup could not restore its held directory. "
                    "The directory was kept for review.",
                ) from exc
            if isinstance(exc, OSError):
                return False
            raise
        if changed:
            if not restore_exact():
                raise _migration_entry_preserved(
                    private_fd,
                    "candidate",
                    "A changed migration directory could not be restored. "
                    "The directory was kept for review.",
                )
            return False
        try:
            _fsync_migration_directories(parent_fd, private_fd)
            os.rmdir("candidate", dir_fd=private_fd)
        except BaseException as exc:
            if _migration_entry_is_unlinked(
                    private_fd, "candidate", directory_fd):
                _fsync_migration_directories(parent_fd, private_fd)
                moved = False
                if isinstance(exc, OSError):
                    return True
                raise
            if _migration_named_directory_matches(
                    private_fd, "candidate", directory_fd):
                if not restore_exact():
                    raise _migration_entry_preserved(
                        private_fd,
                        "candidate",
                        "Migration cleanup could not restore its held "
                        "directory. The directory was kept for review.",
                    ) from exc
                if isinstance(exc, OSError):
                    return False
                raise
            raise OSError(
                "private empty-directory cleanup outcome is uncertain"
            ) from exc
        if not _migration_entry_is_unlinked(
                private_fd, "candidate", directory_fd):
            raise OSError(
                "private empty-directory removal outcome is uncertain")
        moved = False
        _fsync_migration_directories(parent_fd, private_fd)
        return True
    finally:
        preserved = None
        if moved and private_fd is not None:
            try:
                if not restore_exact():
                    if _migration_named_directory_matches(
                            private_fd, "candidate", directory_fd):
                        preserved = _migration_entry_preserved(
                            private_fd,
                            "candidate",
                            "Migration cleanup could not restore its held "
                            "directory. The directory was kept for review.",
                        )
                    else:
                        preserved = _MigrationEntryPreserved(
                            _migration_descriptor_path(directory_fd),
                            "Migration cleanup changed unexpectedly. The "
                            "directory was kept for review.",
                        )
            except _MigrationEntryPreserved as exc:
                preserved = exc
        if private_fd is not None:
            try:
                if (
                    private_name is not None
                    and not os.listdir(private_fd)
                ):
                    _remove_reserved_migration_directory_at(
                        parent_fd, private_name, private_fd)
            except BaseException:
                pass
            os.close(private_fd)
        if preserved is not None:
            raise preserved


def _remove_empty_migration_directory_at(parent_fd, name, directory_fd):
    if not _migration_named_directory_matches(parent_fd, name, directory_fd):
        return False
    # Avoid a rename-and-rollback cycle for a directory that is already known
    # to contain a conflict-kept entry.
    if os.listdir(directory_fd):
        return False
    for _ in range(16):
        temporary_name = f".qobuz-migrate-empty-{secrets.token_hex(12)}"
        try:
            _rename_noreplace_at(
                parent_fd, name, parent_fd, temporary_name)
        except BaseException as exc:
            state = _empty_migration_directory_state(
                parent_fd, name, temporary_name, directory_fd)
            if isinstance(exc, FileExistsError) and state == "public":
                continue
            if state == "quarantined":
                if not _restore_empty_migration_directory_at(
                        parent_fd, name, temporary_name, directory_fd):
                    raise OSError(
                        "empty-directory quarantine could not be rolled back"
                    ) from exc
            elif state != "public":
                restored_unexpected = _restore_any_migration_entry_at(
                    parent_fd, name, temporary_name)
                if restored_unexpected:
                    if isinstance(exc, OSError):
                        return False
                    raise
                raise OSError(
                    "empty-directory quarantine outcome is uncertain"
                ) from exc
            raise
        moved_fd = None
        try:
            try:
                moved_fd = _open_migration_handle(parent_fd, temporary_name)
            except BaseException as exc:
                if not _restore_empty_migration_directory_at(
                        parent_fd, name, temporary_name, directory_fd):
                    raise OSError(
                        "empty-directory cleanup could not be rolled back"
                    ) from exc
                raise
            if (
                not _migration_name_missing(parent_fd, name)
                or not _migration_named_entry_matches(
                    parent_fd, temporary_name, directory_fd)
                or not _migration_named_entry_matches(
                    parent_fd, temporary_name, moved_fd)
            ):
                if (
                    _migration_name_missing(parent_fd, name)
                    and _migration_named_entry_matches(
                        parent_fd, temporary_name, moved_fd)
                    and not _migration_named_entry_matches(
                        parent_fd, temporary_name, directory_fd)
                ):
                    if not _rollback_migration_rename(
                            parent_fd,
                            name,
                            parent_fd,
                            temporary_name,
                            moved_fd):
                        raise OSError(
                            "late replacement directory could not be restored")
                    return False
                if not _restore_empty_migration_directory_at(
                        parent_fd, name, temporary_name, directory_fd):
                    raise OSError(
                        "empty-directory cleanup could not be rolled back")
                return False
            try:
                removed = _remove_private_empty_migration_directory_at(
                    parent_fd, temporary_name, directory_fd)
            except _MigrationEntryPreserved:
                raise
            except BaseException as exc:
                state = _empty_migration_directory_state(
                    parent_fd, name, temporary_name, directory_fd)
                if state == "removed":
                    _fsync_migration_directories(parent_fd)
                    if isinstance(exc, OSError):
                        return True
                    raise
                if not _restore_empty_migration_directory_at(
                        parent_fd, name, temporary_name, directory_fd):
                    raise OSError(
                        "empty-directory cleanup outcome is uncertain"
                    ) from exc
                if isinstance(exc, OSError):
                    return False
                raise
            if not removed:
                if not _restore_empty_migration_directory_at(
                        parent_fd, name, temporary_name, directory_fd):
                    raise OSError(
                        "empty-directory cleanup could not be rolled back")
                return False
            try:
                _fsync_migration_directories(parent_fd)
            except BaseException as exc:
                raise OSError(
                    "empty-directory removal could not be made durable"
                ) from exc
            return True
        finally:
            if moved_fd is not None:
                os.close(moved_fd)
    raise OSError("could not reserve an empty-directory cleanup name")


def _open_or_publish_migration_directory_at(
        parent_fd, name, *, created_out=None):
    try:
        return _open_migration_directory(name, dir_fd=parent_fd), False
    except FileNotFoundError:
        pass

    reserved_name, directory_fd = _reserve_migration_directory_at(
        parent_fd, prefix="qobuz-migrate-directory", mode=0o777)
    try:
        try:
            _rename_noreplace_at(
                parent_fd, reserved_name, parent_fd, name)
        except FileExistsError:
            if not _remove_reserved_migration_directory_at(
                    parent_fd, reserved_name, directory_fd):
                raise OSError(
                    "temporary migration directory could not be reclaimed")
            # Drop the reference before the close: if the re-open below raises,
            # the handler must not close this number again. The kernel hands a
            # freed number straight back, so the retry would land on whatever
            # was opened in between.
            closing = directory_fd
            directory_fd = None
            os.close(closing)
            return _open_migration_directory(name, dir_fd=parent_fd), False
        if not _migration_named_directory_matches(
                parent_fd, name, directory_fd):
            raise OSError("published migration directory changed")
        _fsync_migration_directories(parent_fd)
        if created_out is not None:
            record = {
                "parent_fd": None,
                "directory_fd": None,
                "name": name,
            }
            try:
                record["parent_fd"] = os.dup(parent_fd)
                record["directory_fd"] = os.dup(directory_fd)
                created_out.append(record)
            except BaseException:
                if record not in created_out:
                    for key in ("directory_fd", "parent_fd"):
                        descriptor = record[key]
                        if descriptor is not None:
                            try:
                                os.close(descriptor)
                            except OSError:
                                pass
                raise
        return directory_fd, True
    except BaseException:
        if directory_fd is not None:
            if _migration_named_directory_matches(
                    parent_fd, name, directory_fd):
                _remove_reserved_migration_directory_at(
                    parent_fd, name, directory_fd)
            elif _migration_named_directory_matches(
                    parent_fd, reserved_name, directory_fd):
                _remove_reserved_migration_directory_at(
                    parent_fd, reserved_name, directory_fd)
            os.close(directory_fd)
        raise

def _open_migration_entry(parent_fd, name):
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISDIR(named.st_mode):
        descriptor = _open_migration_directory(name, dir_fd=parent_fd)
    elif stat.S_ISREG(named.st_mode):
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe file access is unavailable")
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    else:
        raise OSError(errno.EINVAL, "unsupported migration entry", name)
    if not _migration_named_entry_matches(parent_fd, name, descriptor):
        os.close(descriptor)
        raise OSError("migration entry changed while it was opened")
    return descriptor


def _rollback_migration_rename(source_parent_fd, source_name,
                               destination_parent_fd, destination_name,
                               moved_fd):
    if _migration_rename_layout(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            moved_fd) != "destination":
        return False
    try:
        _rename_noreplace_at(
            destination_parent_fd,
            destination_name,
            source_parent_fd,
            source_name,
        )
    except BaseException as exc:
        if _migration_rename_layout(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
                moved_fd) != "source":
            if _migration_named_entry_matches(
                    destination_parent_fd, destination_name, moved_fd):
                raise _migration_entry_preserved(
                    destination_parent_fd,
                    destination_name,
                    "A second entry blocked safe migration restoration. "
                    "Both entries were kept for review.",
                ) from exc
            return False
    if _migration_rename_layout(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            moved_fd) != "source":
        return False
    try:
        _fsync_migration_directories(
            source_parent_fd, destination_parent_fd)
    except BaseException:
        return False
    return True


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

    primary = beets_sanitize(_primary_artist_of(qartist_raw))
    if not primary or normalize(current.parent.name) == normalize(primary):
        return None

    # Keep the historical comma-only guard.
    if not _is_migration_candidate(current.parent.name, qartist_raw):
        return None
    return Path(config.MUSIC_ROOT) / primary / current.name


def prompt_and_migrate_multi_artist_folder(
    album,
    args,
    *,
    ownership_move_out=None,
    operation_id_out=None,
    authority=None,
    source_dir=None,
    await_handoff=False,
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

    capture = ownership_move_out if isinstance(ownership_move_out, dict) else None
    if capture is not None:
        capture.clear()
    operation_capture = (
        operation_id_out if isinstance(operation_id_out, dict) else None
    )
    if await_handoff is True and operation_capture is None:
        raise ValueError("a deferred relocation requires an operation receiver")
    if type(await_handoff) is not bool:
        raise ValueError("the relocation handoff option is invalid")
    if operation_capture is not None:
        operation_capture.clear()

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
        relocate_kwargs = {
            "kind": RelocationKind.WHOLE_ALBUM,
            "authority": lease,
        }
        if await_handoff is True:
            relocate_kwargs["await_handoff"] = True
        result = relocate_post_import_album(
            current,
            destination,
            **relocate_kwargs,
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

    if capture is not None and result.ownership_receipt is not None:
        capture.update(result.ownership_receipt)
    if operation_capture is not None:
        operation_capture["operation_id"] = result.operation_id
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
    # name, not a year decoration — don't read it as a release year.
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
    only, artist-name guard). Returning the ranked list — not just the top
    pick — lets find_qobuz_album_for_dir resolve the target_dir case from the
    catalog instead of falling back to the search API."""
    if not catalog:
        return []
    bare = strip_album_decorations(album_dir.name)
    bare_softened = bare.replace("_", " ").replace("  ", " ").strip()
    dir_year = _dir_year(album_dir.name)

    candidates = []
    for r in catalog:
        if not is_lossless_album(r):
            continue
        r_artist = (r.get("artist") or {}).get("name", "")
        if r_artist and similarity(r_artist, artist_name) < config.ARTIST_NAME_THRESH:
            continue
        r_bare = strip_album_decorations(r.get("title", ""))
        s1 = similarity(r_bare, bare)
        s2 = similarity(r_bare, bare_softened) if bare_softened and bare_softened != bare else 0.0
        score = max(s1, s2)
        # Coverage + variant guards, mirroring find_album_dir_filesystem: a
        # 0.65-similar prefix match ('The North Borders' vs '… Tour — Live')
        # would otherwise pull a live/tour edition onto the studio-album dir.
        norm_r = normalize(r_bare)
        norm_b = normalize(bare)
        if norm_r and norm_b:
            coverage = min(len(norm_r), len(norm_b)) / max(len(norm_r), len(norm_b))
            if coverage < config.FUZZY_DIR_MIN_COVERAGE:
                continue
            shorter_n, longer_n = sorted((norm_r, norm_b), key=len)
            if differs_by_album_variant(shorter_n, longer_n):
                continue
        candidates.append((score, _year_proximity_bonus(dir_year, r), r))

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


def _pick_best_target_dir_match(scored_cands, target_dir):
    """Among candidates whose predicted on-disk path resolves to target_dir, pick
    the one whose tracks_count best fits the on-disk audio count.
    scored_cands: iterable of (combined_score: float, album_dict).
    """
    n_disk = _count_audio_files_in(target_dir)
    resolving = []
    for score, cand in scored_cands:
        predicted = find_album_dir_filesystem(cand)
        if predicted is not None and _paths_equal(predicted, target_dir):
            resolving.append((score, cand))
    if not resolving:
        return None, 0
    if n_disk == 0:
        # Empty folder — nothing to fit against; trust score order.
        return resolving[0][1], resolving[0][0]

    def rank(item):
        score, cand = item
        tc = cand.get("tracks_count") or 0
        if tc == 0:
            return (10 ** 6, -score)  # unknown count → last resort
        if tc >= n_disk:
            dist = tc - n_disk
        else:
            # Slight bias toward tc >= n_disk so a partial library (5/11)
            # still picks the 11-track edition over a 5-track release.
            dist = (n_disk - tc) * 1.5
        return (dist, -score)

    resolving.sort(key=rank)
    return resolving[0][1], resolving[0][0]


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
            # Pick by tracks_count fit (not first-resolving).
            scored = [(1.0 - 0.001 * i, c) for i, c in enumerate(cands)]
            best, _bs = _pick_best_target_dir_match(scored, target_dir)
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
             f"{top[0]:.2f} — under threshold")
        return None
    best_score, _best_year_bonus, best = passing[0]

    # When a specific on-disk target is required, walk all viable candidates
    # and return the first whose predicted path resolves back to target_dir.
    if target_dir is not None:
        # Pick among the viable candidates by track-count fit, not score, so a
        # box set doesn't outrank the standard album the folder actually holds.
        viable = [(s + yb, r) for (s, yb, r) in candidates
                  if s >= config.ARTIST_DIR_MATCH_THRESH]
        best, best_score = _pick_best_target_dir_match(viable, target_dir)
        if best is not None:
            vlog(f"    Qobuz match (target-dir aligned): {best.get('title')!r} "
                 f"(score {best_score:.2f}, tc={best.get('tracks_count')})")
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
    for alternate editions whose track count covers the local library.

    Returns a list of (full_album_dict, new_extras_list) tuples for editions
    that don't make the extras-blocking-upgrade situation worse. Sorted: fewest
    new_extras first (zero is best — covers all local tracks), then by quality
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
        if not q_tracks:
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
