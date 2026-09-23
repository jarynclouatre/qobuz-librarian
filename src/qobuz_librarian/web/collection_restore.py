"""Build a download review from a collection snapshot."""
from __future__ import annotations

from collections import Counter

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable
from qobuz_librarian.api.search import (
    find_qobuz_track_by_isrc,
    get_album,
    search_albums,
)
from qobuz_librarian.library import candidate_premise, catalog, discovery, scanner
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
NO_QOBUZ_TRACKS = "its missing tracks are not on Qobuz"
UNSAFE_NAME = "the backup names a folder the library can't hold"


def _isrc(value) -> str:
    return str(value or "").replace("-", "").upper().strip()


def _album_isrcs(album) -> list:
    return [i for i in (_isrc(t.get("isrc"))
                        for t in album.get("tracks") or []) if i]


def _snapshot_album_title(album) -> str:
    """What to call a backed-up album: its tag title, else its folder name."""
    return (album.get("title") or album.get("name") or "").strip()


def _title_key(value) -> str:
    text = str(value or "").strip()
    return discovery.owned_title_key(text) or text.casefold()


def _name_key(value) -> str:
    text = str(value or "").strip()
    return normalize(text) or text.casefold()


def _matches_snapshot(found, album, artist_name) -> bool:
    found_artist = found.get("artist") or {}
    if isinstance(found_artist, dict):
        found_artist = found_artist.get("name") or ""
    return (
        _title_key(found.get("title")) == _title_key(_snapshot_album_title(album))
        and _name_key(found_artist) == _name_key(artist_name)
    )


class _OnDisk:
    """What the library holds right now, read once and answered per artist."""

    def __init__(self):
        self.by_name = {}
        self.by_normalized = {}
        self.unreadable = set()
        for artist_dir in scanner.list_library_artists(
                on_artist_error=lambda path, error: self.unreadable.add(path.name)):
            self.by_name[artist_dir.name] = artist_dir
            self.by_normalized.setdefault(normalize(artist_dir.name),
                                          artist_dir)
        self._albums = {}
        self._tracks = {}
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

    def tracks(self, album_dir):
        if album_dir not in self._tracks:
            self._tracks[album_dir] = scanner.read_album_dir(album_dir)
        return self._tracks[album_dir]

    def isrcs(self, album_dir):
        if album_dir not in self._isrcs:
            self._isrcs[album_dir] = {
                code for code in (_isrc(t.get("isrc"))
                                  for t in self.tracks(album_dir)) if code}
        return self._isrcs[album_dir]


def _owned_folder(disk, artist_dir, album, backup_ids):
    """The folder that holds this backed-up album now, or None.

    ``backup_ids`` maps the backup's folder names to their Qobuz ids. A folder
    the backup also names carries that id, and two known ids settle whether a
    same-titled folder is this album; without one the year must agree, so a
    2001 "Weezer" is not taken for the 1994 one.
    """
    album_dirs = disk.album_dirs(artist_dir)
    name = album.get("name") or ""
    for d in album_dirs:
        if d.name == name:
            return d
    wanted_id = str(album.get("qobuz_album_id") or "")
    wanted_year = catalog._dir_year(name)
    keys = {discovery.owned_title_key(album.get("name")),
            discovery.owned_title_key(album.get("title"))} - {""}
    for d in album_dirs:
        if discovery.owned_title_key(d.name) not in keys:
            continue
        held_id = backup_ids.get(d.name)
        if wanted_id and held_id:
            if held_id == wanted_id:
                return d
            continue
        held_year = catalog._dir_year(d.name)
        if wanted_year is None or held_year is None or held_year == wanted_year:
            return d
    wanted = _album_isrcs(album)
    if not wanted:
        return None
    best, best_matched = None, 0
    for d in album_dirs:
        have = disk.isrcs(d)
        matched = sum(1 for code in wanted if code in have)
        if matched > best_matched:
            best, best_matched = d, matched
    if best is not None and best_matched / len(wanted) >= ISRC_OWNED_RATIO:
        return best
    return None


def _lost_tracks(disk, album_dir, album) -> int:
    """How many of the backup's tracks for this album its folder lacks."""
    isrcs = disk.isrcs(album_dir)
    titles = Counter(normalize(t.get("title") or "")
                     for t in disk.tracks(album_dir))
    lost = 0
    for track in album.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        title = normalize(track.get("title") or "")
        if titles[title] > 0:
            titles[title] -= 1
        elif _isrc(track.get("isrc")) not in isrcs:
            lost += 1
    return lost


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


def _by_isrc(album, artist_name, token):
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
        if _usable(found) and _matches_snapshot(found, album, artist_name):
            return found
    return None


def _by_search(album, artist_name, token):
    title = _snapshot_album_title(album)
    if not title:
        return None
    wanted = _title_key(title)
    wanted_artist = _name_key(artist_name)
    try:
        results = search_albums(f"{artist_name} {title}", token)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        return None
    lossless = [a for a in results if is_lossless_album(a)]
    for found, _versions in dedup_album_versions(
            lossless, prefer_hires=cfg.PREFER_HIRES):
        if _title_key(found.get("title")) != wanted:
            continue
        found_artist = (found.get("artist") or {}).get("name") or ""
        if wanted_artist and _name_key(found_artist) != wanted_artist:
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
    found = _by_isrc(album, artist_name, token)
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
    for name in sorted(disk.unreadable):
        job.push_line(f"Unreadable artist {name}. Check folder permissions and retry.")
    unreadable_keys = {normalize(name) for name in disk.unreadable}
    if disk.unreadable:
        flows._record_unchecked_artists(job, len(disk.unreadable))
    artists = [a for a in snapshot.get("artists") or [] if isinstance(a, dict)]
    total = len(artists)
    owned = 0
    queued = 0
    gap_fills = 0
    unlisted = 0
    unresolved = []
    seen_album_ids = set()

    for done, entry in enumerate(artists, 1):
        if job.cancel_requested:
            log.info("Cancelled. Stopping the check.")
            break
        artist_name = (entry.get("name") or "").strip()
        if not artist_name:
            continue
        if normalize(artist_name) in unreadable_keys:
            continue
        artist_dir = disk.artist_dir(artist_name)
        artist_key = artist_dir.name if artist_dir is not None else None
        albums = [a for a in entry.get("albums") or [] if isinstance(a, dict)]
        backup_ids = {a["name"]: str(a["qobuz_album_id"]) for a in albums
                      if a.get("name") and a.get("qobuz_album_id")}
        for album in albums:
            title = _snapshot_album_title(album) or "?"
            folder = (_owned_folder(disk, artist_dir, album, backup_ids)
                      if artist_dir is not None else None)
            if folder is not None and not _lost_tracks(disk, folder, album):
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
            missing = []
            if folder is not None:
                missing, _present = catalog.compute_missing(
                    (found.get("tracks") or {}).get("items") or [],
                    disk.tracks(folder))
                if not missing:
                    unlisted += 1
                    job.push_line(f"{artist_name} - {title}: {NO_QOBUZ_TRACKS}.")
                    continue
            added = flows.add_restore_candidate(
                job, found, artist_name, artist_key=artist_key,
                album_dir=folder, missing=missing)
            if added is None:
                unresolved.append((artist_name, title, UNSAFE_NAME))
                job.push_line(f"{artist_name} - {title}: {UNSAFE_NAME}.")
                continue
            seen_album_ids.add(album_id)
            queued += 1
            if folder is not None:
                gap_fills += 1
        job.push_progress("Checking the backup", done, total, artist_name,
                          found=queued, unit="artist")

    if job.cancel_requested:
        job.summary = (f"Stopped early. {plural(queued, 'album')} to download "
                       "from the part of the backup that was checked.")
        log.info(job.summary)
        return

    ready = []
    if queued > gap_fills:
        ready.append(f"{plural(queued - gap_fills, 'album')} to download")
    if gap_fills:
        ready.append(f"{plural(gap_fills, 'album')} with missing tracks")
    parts = [f"Restore review ready: {' and '.join(ready)}."
             if queued else "Nothing to restore."]
    if disk.unreadable:
        if not queued:
            parts = ["Restore check incomplete."]
            flows._mark_job_failed(job)
        parts.append("Unreadable: " + ", ".join(sorted(disk.unreadable))
                     + ". Check folder permissions and retry.")
    if owned:
        parts.append(f"{plural(owned, 'album')} already in your library.")
    if unresolved:
        parts.append(f"{plural(len(unresolved), 'album')} couldn't be matched "
                     "on Qobuz; see the job log.")
    if unlisted:
        parts.append("Qobuz no longer lists the missing tracks of "
                     f"{plural(unlisted, 'album')}; see the job log.")
    job.summary = " ".join(parts)
    log.info(job.summary)
