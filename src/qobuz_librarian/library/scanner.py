"""Filesystem scanning.

Things worth knowing if you edit this:

- `list_library_artists` and `list_artist_album_dirs` skip dot-folders and
  folders with no audio in their tree. Without this, hidden dirs like
  `.Trash` and leftover empty folders get treated as content and break
  later matching.
- `list_library_artists` also excludes `STAGING_DIR` so in-progress
  downloads never get scanned as if they were library content.
- `read_album_dir` walks per-disc subdirs (`CD1/`, `CD2/`) but never
  follows symlinks, so a loop in the library can't recurse forever.
- Every audio format is read with mutagen; a file that won't parse or
  has no tags falls back to a title/track guessed from its filename, so
  untagged bonus tracks (mp3, m4a from older rips) stay visible to
  `find_extras_in_existing`.
"""
import logging
import os
import re
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.library import flac_cache
from qobuz_librarian.library.tags import normalize
from qobuz_librarian.ui_cli.logging import vlog


def iter_tree_no_symlinks(root: Path, errors=None):
    """Yield every entry under root, never descending into symlinked dirs.

    A symlink loop inside MUSIC_ROOT must not send a walk into unbounded
    recursion, and content linked in from outside an album shouldn't be
    scanned as if it lived there. Symlinked subdirs are yielded as leaves so
    the caller still sees them; they're just never followed.

    Pass ``errors`` (a list) to learn whether any subtree couldn't be read -
    the walk continues past it, so the listing is incomplete and conclusions
    like "contains no audio" must not be drawn (or cached) from it.
    """
    def _onerror(err):
        # os.walk swallows scandir failures by default - a permission-denied
        # or I/O-failed subdir would then silently drop its tracks from the
        # scan with no signal at all.
        vlog(f"scan: couldn't read {getattr(err, 'filename', root)}: {err}")
        if errors is None:
            raise err
        errors.append(err)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                onerror=_onerror):
        dp = Path(dirpath)
        for name in dirnames:
            yield dp / name
        for name in filenames:
            yield dp / name

log = logging.getLogger("qobuz_librarian")

try:
    import mutagen
    HAVE_MUTAGEN = True
except ImportError:
    mutagen = None
    HAVE_MUTAGEN = False


# ── Track-number parsing ──────────────────────────────────────────────────────
def parse_track_num(s):
    """Parse a FLAC TRACKNUMBER or DISCNUMBER tag value to int.

    Handles '1', '01', '1/12', '01/12', '5 of 12'. Returns 0 on empty or
    unparseable input.
    """
    if not s:
        return 0
    m = re.match(r"^\s*(\d+)", str(s))
    return int(m.group(1)) if m else 0


# ── Audio metadata ────────────────────────────────────────────────────────────
# Cached sentinel for a file mutagen can't parse / that has no title tag, so the
# filename-fallback files (untagged legacy mp3/m4a) aren't fully re-parsed on
# every scan. Self-invalidates with mtime/size like any cache row.
_NEG_META = {"__neg__": True}


def read_audio_meta(path: Path):
    """Read tags and audio info via mutagen. Returns dict or None.

    Works for any format mutagen understands (FLAC, MP3, M4A, …) through its
    uniform "easy" tag interface. Returns None when mutagen is unavailable,
    the file can't be parsed, or it has no title tag - the caller then derives
    title and track number from the filename, so untagged bonus tracks still
    show up.
    """
    if not HAVE_MUTAGEN:
        return None
    cached = flac_cache.get(path)
    if cached is not None:
        # A cached negative result (unparseable / title-less file): return None
        # without re-parsing - these otherwise pay a full mutagen parse on every
        # scan even though they always fall back to the filename.
        return None if cached.get("__neg__") else cached
    # Capture the file signature before parsing so a file edited mid-scan isn't
    # cached with its new mtime but these now-stale tags.
    sig = flac_cache.signature(path)
    try:
        f = mutagen.File(str(path), easy=True)
    except OSError:
        # A read failure (EACCES/EIO/ESTALE) is not proof the file is
        # untagged.
        raise
    except Exception:
        flac_cache.put(path, _NEG_META, sig=sig)
        return None
    if f is None:
        flac_cache.put(path, _NEG_META, sig=sig)
        return None

    tags = f.tags

    def first(key):
        v = tags.get(key) if tags else None
        if v and isinstance(v, list):
            return v[0]
        return ""

    title = first("title")
    if not title:
        flac_cache.put(path, _NEG_META, sig=sig)
        return None

    info = f.info
    meta = {
        "title":       title,
        "isrc":        first("isrc").strip().replace("-", "").upper(),
        "mb_trackid":  first("musicbrainz_trackid").strip().lower(),
        "album":       first("album"),
        "albumartist": first("albumartist") or first("artist"),
        "tracknumber": parse_track_num(first("tracknumber")),
        "discnumber":  parse_track_num(first("discnumber")) or 1,
        "bits":        getattr(info, "bits_per_sample", 0) if info else 0,
        "sample_rate": getattr(info, "sample_rate", 0) if info else 0,
        "channels":    getattr(info, "channels", 0) if info else 0,
        "length":      getattr(info, "length", 0.0) if info else 0.0,
        "path":        str(path),
        # Carry the size from the signature so read_album_dir doesn't have to
        # re-stat every audio file just for its size (a second stat per file
        # adds up on a NAS-backed library).
        "size":        sig[1] if sig else 0,
    }
    flac_cache.put(path, meta, sig=sig)
    return meta


# ── Album directory scan ──────────────────────────────────────────────────────
def read_album_dir(album_dir: Path, walk_errors=None):
    """Scan album_dir for audio files; return list of track-metadata dicts.

    Tags are read with mutagen for every format (flac, mp3, m4a, …); a file
    that won't parse or carries no tags falls back to title/track from its
    filename, so even untagged bonus tracks appear in find_extras_in_existing
    and aren't silently destroyed by upgrade-replace.
    Multi-disc subdirectories (CD1/, CD2/) are walked; symlinks never followed.

    Pass ``walk_errors`` (a list) to learn whether any entry or subtree
    couldn't be read - the returned list is then possibly INCOMPLETE, and a
    caller about to delete something based on these counts must treat that as
    unverifiable rather than as a smaller album.
    """
    try:
        album_dir.stat()
    except FileNotFoundError:
        return []
    except OSError as e:
        if walk_errors is None:
            raise
        walk_errors.append(f"{album_dir}: {e}")
        return []

    audio_files = []
    _exts = set(config.AUDIO_EXTS)
    try:
        for f in iter_tree_no_symlinks(album_dir, errors=walk_errors):
            # is_file() re-raises EACCES/EIO/ESTALE (only ENOENT-class errors
            # are swallowed by pathlib).
            try:
                if f.suffix.lower() in _exts and f.is_file():
                    audio_files.append(f)
            except OSError as e:
                if walk_errors is not None:
                    walk_errors.append(f"{f}: {e}")
                vlog(f"skipping unreadable entry {f} in {album_dir}: {e}")
    except OSError as e:
        if walk_errors is None:
            raise
        walk_errors.append(f"{album_dir}: {e}")
        vlog(f"walk failed in {album_dir}: {e}")
    audio_files.sort()
    vlog(f"found {len(audio_files)} audio file(s) in {album_dir}")

    tracks = []
    for f in audio_files:
        try:
            tags = read_audio_meta(f)
        except OSError as e:
            # The file is listed but can't be read: drop it AND mark the walk
            # degraded.
            if walk_errors is None:
                raise
            walk_errors.append(f"{f}: {e}")
            vlog(f"couldn't read tags for {f} in {album_dir}: {e}")
            continue
        if tags is None:
            stem = f.stem
            # Strip a leading track-number token in any common form - "NN - ",
            # "NN.
            m = re.match(r"^(\d+)[\s.\-]+(.+)$", stem)
            # Derive the disc from a "Disc N" / "CD N" parent so two same-titled
            # tracks on different discs don't collapse to one (disc, title) key.
            disc_m = re.match(r"(?:disc|cd)\s*0*(\d+)", f.parent.name, re.IGNORECASE)
            tags = {
                "title":       m.group(2) if m else stem,
                "tracknumber": int(m.group(1)) if m else 0,
                "isrc":        "",
                "mb_trackid":  "",
                "album":       "",
                "albumartist": "",
                "discnumber":  int(disc_m.group(1)) if disc_m else 1,
                "bits":        0,
                "sample_rate": 0,
                "channels":    0,
                "length":      0.0,
                "path":        str(f),
            }
        tags["normalized"] = normalize(tags["title"])
        # read_audio_meta now carries size from the file signature; only the
        # filename-fallback path (above) and pre-existing cache entries that
        # predate the "size" key fall through to a stat here.
        if "size" not in tags:
            try:
                tags["size"] = f.stat().st_size
            except OSError:
                tags["size"] = 0
        tracks.append(tags)
    return tracks


# ── Library directory listing ─────────────────────────────────────────────────
_HAS_AUDIO_CACHE: dict = {}


def _has_audio_anywhere(d: Path, walk_errors=None):
    """True if audio exists, False if none does, or None after a recorded error.

    Without an explicit ``walk_errors`` list, traversal errors propagate so a
    production scan cannot consume an incomplete tree as empty. A caller that
    supplies a list may continue cautiously, but receives None rather than a
    false claim that the directory contains no audio.

    Result cached per path: a single scan calls this once per artist plus
    once per album dir, but artist-walk/upgrade-walk/lyric-walk all hit
    list_artist_album_dirs for the same artists in turn, and the catalog
    fuzzy-resolution fall-through re-asks the same dirs again - a fresh
    iter_tree per call is wasted iterdir+stat on every album subtree.
    """
    key = str(d)
    # Atomic get: a concurrent download's clear_scan_caches() can empty the
    # dict between an `in` check and the lookup, and that KeyError would
    # escape the OSError guard below and drop the artist from the scan.
    cached = _HAS_AUDIO_CACHE.get(key)
    if cached is not None:
        return cached
    exts = set(config.AUDIO_EXTS)
    error_count = len(walk_errors) if walk_errors is not None else 0
    try:
        for f in iter_tree_no_symlinks(d, errors=walk_errors):
            try:
                if f.is_file() and f.suffix.lower() in exts:
                    _HAS_AUDIO_CACHE[key] = True
                    return True
            except OSError as e:
                if walk_errors is None:
                    raise
                walk_errors.append(f"{f}: {e}")
    except OSError as e:
        if walk_errors is None:
            raise
        walk_errors.append(f"{d}: {e}")
    if walk_errors is not None and len(walk_errors) > error_count:
        # os.walk consumed a scandir failure via the error callback and walked
        # on without that subtree - the audio may live exactly there.
        return None
    _HAS_AUDIO_CACHE[key] = False
    return False


def list_library_artists(walk_errors=None):
    """List artist directories under MUSIC_ROOT.

    Skips dot-folders (startswith(".")) and the staging
    directory. Sorted by name (case-insensitive). Empty artist directories
    (no audio files anywhere in the tree) are also skipped - they cost an
    API round-trip during scans for zero gain and clutter the walk output.
    A single info line names anything skipped so the user can hand-clean.

    Used for fuzzy resolution and the library / walk+queue / album-fill
    walks.
    """
    artists = []
    empties = []
    try:
        entries = list(config.MUSIC_ROOT.iterdir())
    except FileNotFoundError:
        return []
    except OSError as e:
        if walk_errors is None:
            raise
        walk_errors.append(f"{config.MUSIC_ROOT}: {e}")
        log.info(f"  ⚠  Couldn’t list MUSIC_ROOT: {e}.")
        return []
    try:
        for d in entries:
            if not d.is_dir():
                continue
            if d.name.startswith("."):          # skip hidden dirs (.Trash, .DS_Store/, etc.)
                continue
            if d.resolve() == config.STAGING_DIR.resolve():
                continue
            has_audio = _has_audio_anywhere(d, walk_errors=walk_errors)
            if has_audio is None:
                continue
            if not has_audio:
                empties.append(d.name)
                continue
            artists.append(d)
    except OSError as e:
        if walk_errors is None:
            raise
        walk_errors.append(f"{config.MUSIC_ROOT}: {e}")
        log.info(f"  ⚠  Couldn’t list MUSIC_ROOT: {e}.")
    if empties:
        names = ", ".join(sorted(empties)[:5])
        more = f" (+{len(empties) - 5} more)" if len(empties) > 5 else ""
        log.info(f"  · Skipping {len(empties)} empty artist dir(s): {names}{more}.")
    return sorted(artists, key=lambda p: p.name.lower())


def list_artist_album_dirs(artist_dir: Path, walk_errors=None):
    """Album subdirectories under an artist dir, sorted by name.

    Skips hidden dot-folders (.Trash, .DS_Store-style, etc.) and folders with
    no audio anywhere in their tree. An empty album folder owns nothing to
    match, upgrade or repair, and resolving one by its name alone only yields a
    confusing "0 present" result; this mirrors list_library_artists, which
    drops empty artist dirs for the same reason. A short notice names anything
    skipped so the user can hand-clean leftover folders.
    """
    albums = []
    empties = []
    try:
        entries = sorted(artist_dir.iterdir(), key=lambda p: p.name.lower())
    except FileNotFoundError:
        return []
    except OSError as e:
        if walk_errors is None:
            raise
        walk_errors.append(f"{artist_dir}: {e}")
        vlog(f"list_artist_album_dirs: {e}")
        return []
    try:
        for d in entries:
            if not d.is_dir():
                continue
            if d.name.startswith("."):          # skip hidden dirs (.Trash, .DS_Store/, etc.)
                continue
            if d.name.endswith(".restore_trash"):  # leftover from an interrupted restore
                continue
            has_audio = _has_audio_anywhere(d, walk_errors=walk_errors)
            if has_audio is None:
                continue
            if not has_audio:
                empties.append(d.name)
                continue
            albums.append(d)
    except OSError as e:
        if walk_errors is None:
            raise
        walk_errors.append(f"{artist_dir}: {e}")
        vlog(f"list_artist_album_dirs: {e}")
    if empties:
        names = ", ".join(empties[:5])
        more = f" (+{len(empties) - 5} more)" if len(empties) > 5 else ""
        # Verbose-only: in a whole-library sweep this fires per artist and floods
        # the activity log. It's a hand-clean hint, not something every scan needs.
        vlog(f"  · {artist_dir.name}: skipping {len(empties)} empty album "
             f"folder(s): {names}{more}.")
    return albums


# ── Per-scan directory cache ──────────────────────────────────────────────────
# Cleared via clear_scan_caches() at every top-level mode entry so memory
# stays bounded.
_ARTIST_SUBDIRS_CACHE: dict = {}


def _list_artist_subdirs_cached(artist_dir: Path):
    key = str(artist_dir)
    # Atomic get (see _has_audio_anywhere): an `in`/`[]` pair could KeyError if a
    # concurrent clear_scan_caches() empties the dict between them.
    cached = _ARTIST_SUBDIRS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        subdirs = sorted((d for d in artist_dir.iterdir() if d.is_dir()),
                         key=lambda p: p.name.lower())
    except OSError as e:
        vlog(f"  iterdir failed for {artist_dir}: {e}")
        raise
    _ARTIST_SUBDIRS_CACHE[key] = subdirs
    return subdirs


def cache_album_tags(album_dirs):
    """Read the tags of albums that have just landed in the library.

    The quality census aggregates this cache, and only a library scan fills
    it. The app is meant to keep track of what it downloads without asking for
    another scan, so an album that imports and is never read here leaves
    "What's on disk" short by its own tracks for good. This is the same parse a
    scan would do, minus the walk.

    Only directories inside the library count: the census ignores everything
    else, and a staging copy must never be cached as a library track.
    """
    music_root = os.path.abspath(os.fspath(config.MUSIC_ROOT)) + os.sep
    seen = set()
    for album_dir in album_dirs:
        if album_dir is None:
            continue
        try:
            resolved = os.path.abspath(os.fspath(album_dir))
        except (TypeError, ValueError):
            continue
        if not resolved.startswith(music_root) or resolved in seen:
            continue
        seen.add(resolved)
        try:
            read_album_dir(Path(resolved))
        except OSError as e:
            vlog(f"post-import tag cache failed for {resolved}: {e}")
    if seen:
        flac_cache.flush_pending()


def clear_scan_caches():
    """Drop per-scan caches. Pure-function lru_caches (normalize / etc.)
    are left alone - deterministic and worth keeping warm.

    Also drains the flac_cache write buffer so anything parsed mid-scan is
    on disk before the next pass starts (the scan-end commit point - put()
    buffers rather than committing per-file to keep a cold 200k-track scan
    out of per-file disk-sync territory)."""
    _ARTIST_SUBDIRS_CACHE.clear()
    _HAS_AUDIO_CACHE.clear()
    flac_cache.flush_pending()


def drop_artist_subdirs_cache(artist_dir):
    """Invalidate the cached subdir listing for one artist, not the whole map.

    Use this when only one artist's library folder has changed on disk (a
    beets rename of just-imported album, an in-place upgrade landing) - a
    bulk-upgrade pass touches one artist at a time, so the full-cache wipe
    `clear_scan_caches()` does would cold-rebuild every OTHER artist's
    listing on the next item too. Quiet on a missing key."""
    if artist_dir is None:
        return
    _ARTIST_SUBDIRS_CACHE.pop(str(artist_dir), None)
