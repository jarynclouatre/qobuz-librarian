"""Rebuild a library from a collection backup file.

The backup written under Settings lists every artist, album and track the
library held. This reads one back, works out which of those albums are not on
disk any more, finds each one on Qobuz, and parks a single review so the whole
rebuild is confirmed once rather than album by album.

Nothing here writes to the library, and none of the uploaded file's text is
ever used as a path: an album is matched against folders the scanner listed,
and a row for an artist with no folder left carries only the artist's name,
checked to be a single folder name before it is stored.
"""
from __future__ import annotations

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable
from qobuz_librarian.api.search import (
    find_qobuz_track_by_isrc,
    get_album,
    search_albums,
)
from qobuz_librarian.library import candidate_premise, discovery, scanner
from qobuz_librarian.library.catalog import (
    dedup_album_versions,
    is_lossless_album,
)
from qobuz_librarian.library.tags import normalize
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.web import flows

# An album whose folder was renamed still carries its ISRCs. Half of them
# matching the artist's on-disk set is the album, not a coincidence: ISRCs are
# per-recording, so two different albums share them only when one reissues the
# other, which is the same music either way.
ISRC_OWNED_RATIO = 0.5

# How many of an album's ISRCs are worth spending a Qobuz lookup on before
# giving up and searching by name.
ISRC_LOOKUP_TRIES = 3

NO_QOBUZ_ALBUM = "no longer on Qobuz"
NO_ISRC_MATCH = "no track matched by ISRC"
NO_SEARCH_MATCH = "no album matched by name"
UNSAFE_NAME = "the backup names a folder the library can't hold"


def _isrc(value) -> str:
    return str(value or "").replace("-", "").upper().strip()


def _album_isrcs(album) -> list:
    return [i for i in (_isrc(t.get("isrc"))
                        for t in album.get("tracks") or []) if i]


def _snapshot_album_title(album) -> str:
    """What to call a backed-up album: its tag title, else its folder name."""
    return (album.get("title") or album.get("name") or "").strip()


class _OnDisk:
    """What the library holds right now, read once and answered per artist."""

    def __init__(self):
        self.by_name = {}
        self.by_normalized = {}
        for artist_dir in scanner.list_library_artists():
            self.by_name[artist_dir.name] = artist_dir
            self.by_normalized.setdefault(normalize(artist_dir.name),
                                          artist_dir)
        self._albums = {}
        self._isrcs = {}

    def artist_dir(self, name):
        found = self.by_name.get(name)
        if found is not None:
            return found
        return self.by_normalized.get(normalize(name))

    def album_dirs(self, artist_dir):
        if artist_dir not in self._albums:
            self._albums[artist_dir] = scanner.list_artist_album_dirs(
                artist_dir)
        return self._albums[artist_dir]

    def isrcs(self, artist_dir):
        """Every ISRC under one artist. Read only when a name match fails, so
        an untouched library never pays for the tag walk."""
        if artist_dir not in self._isrcs:
            found = set()
            for album_dir in self.album_dirs(artist_dir):
                for track in scanner.read_album_dir(album_dir):
                    code = _isrc(track.get("isrc"))
                    if code:
                        found.add(code)
            self._isrcs[artist_dir] = found
        return self._isrcs[artist_dir]


def _still_owned(disk, artist_dir, album) -> bool:
    """Is this backed-up album still in the library under some name?"""
    album_dirs = disk.album_dirs(artist_dir)
    if any(d.name == album.get("name") for d in album_dirs):
        return True
    owned_titles = discovery.owned_album_titles(album_dirs)
    for candidate in (album.get("name"), album.get("title")):
        key = discovery.owned_title_key(candidate)
        if key and key in owned_titles:
            return True
    wanted = _album_isrcs(album)
    if not wanted:
        return False
    have = disk.isrcs(artist_dir)
    matched = sum(1 for code in wanted if code in have)
    return matched / len(wanted) >= ISRC_OWNED_RATIO


def _usable(album) -> bool:
    """A Qobuz album worth downloading: lossless and carrying real tracks."""
    if not isinstance(album, dict) or not album.get("id"):
        return False
    tracks = (album.get("tracks") or {}).get("items") or []
    count = album.get("tracks_count")
    return is_lossless_album(album) and bool(tracks or count)


def _by_stored_id(album, token):
    album_id = album.get("qobuz_album_id")
    if not album_id:
        return None
    try:
        found = get_album(album_id, token)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        return None
    return found if _usable(found) else None


def _by_isrc(album, token):
    for code in _album_isrcs(album)[:ISRC_LOOKUP_TRIES]:
        track = find_qobuz_track_by_isrc(code, token)
        album_id = ((track or {}).get("album") or {}).get("id")
        if not album_id:
            continue
        try:
            found = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception:
            continue
        if _usable(found):
            return found
    return None


def _by_search(album, artist_name, token):
    title = _snapshot_album_title(album)
    if not title:
        return None
    wanted = discovery.owned_title_key(title)
    wanted_artist = normalize(artist_name)
    try:
        results = search_albums(f"{artist_name} {title}", token)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        return None
    lossless = [a for a in results if is_lossless_album(a)]
    for found, _versions in dedup_album_versions(
            lossless, prefer_hires=cfg.PREFER_HIRES):
        if discovery.owned_title_key(found.get("title")) != wanted:
            continue
        found_artist = (found.get("artist") or {}).get("name") or ""
        if wanted_artist and normalize(found_artist) != wanted_artist:
            continue
        try:
            full = get_album(found.get("id"), token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception:
            continue
        if _usable(full):
            return full
    return None


def _resolve(album, artist_name, token):
    """Find one backed-up album on Qobuz. Returns (album dict, reason)."""
    found = _by_stored_id(album, token)
    if found is not None:
        return found, None
    stored_failed = bool(album.get("qobuz_album_id"))
    found = _by_isrc(album, token)
    if found is not None:
        return found, None
    found = _by_search(album, artist_name, token)
    if found is not None:
        return found, None
    if stored_failed:
        return None, NO_QOBUZ_ALBUM
    return None, NO_ISRC_MATCH if _album_isrcs(album) else NO_SEARCH_MATCH


def scan_restore(job, snapshot, token):
    """Diff an uploaded backup against the library and park one review."""
    scanner.clear_scan_caches()
    # The music folder this diff is being read against. Kept on the job so a
    # later approval that finds every row stale can tell "the folder is not
    # the one this was built against" apart from ordinary churn on disk.
    root = candidate_premise.capture_music_root_identity()
    if root is not None and isinstance(job.execute_args, dict):
        job.execute_args["music_root"] = root
    disk = _OnDisk()
    artists = [a for a in snapshot.get("artists") or [] if isinstance(a, dict)]
    total = len(artists)
    owned = 0
    queued = 0
    unresolved = []
    seen_album_ids = set()

    for done, entry in enumerate(artists, 1):
        if job.cancel_requested:
            log.info("Cancelled. Stopping the check.")
            break
        artist_name = (entry.get("name") or "").strip()
        if not artist_name:
            continue
        artist_dir = disk.artist_dir(artist_name)
        artist_key = artist_dir.name if artist_dir is not None else None
        for album in entry.get("albums") or []:
            if not isinstance(album, dict):
                continue
            title = _snapshot_album_title(album) or "?"
            if artist_dir is not None and _still_owned(disk, artist_dir, album):
                owned += 1
                continue
            found, reason = _resolve(album, artist_name, token)
            if found is None:
                unresolved.append((artist_name, title, reason))
                job.push_line(f"{artist_name} - {title}: {reason}.")
                continue
            album_id = str(found.get("id"))
            if album_id in seen_album_ids:
                continue
            added = flows.add_restore_candidate(
                job, found, artist_name, artist_key=artist_key)
            if added is None:
                unresolved.append((artist_name, title, UNSAFE_NAME))
                job.push_line(f"{artist_name} - {title}: {UNSAFE_NAME}.")
                continue
            seen_album_ids.add(album_id)
            queued += 1
        job.push_progress("Checking the backup", done, total, artist_name,
                          found=queued, unit="artist")

    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(queued, 'album')} to download "
                       "from the part of the backup that was checked.")
        log.info(job.summary)
        return

    parts = [f"Restore review ready: {plural(queued, 'album')} to download."
             if queued else "Nothing to restore."]
    if owned:
        parts.append(f"{plural(owned, 'album')} already in your library.")
    if unresolved:
        parts.append(f"{plural(len(unresolved), 'album')} couldn't be matched "
                     "on Qobuz; see the job log.")
    job.summary = " ".join(parts)
    log.info(job.summary)
