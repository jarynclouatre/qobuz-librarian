"""Plain-JSON snapshots of what the collection holds.

The music folder is the collection's only record. If that disk dies there is
nothing left to rebuild from, so this writes the shape of the library out as
readable JSON: artist and album folder names, track titles and ISRCs, and the
Qobuz ids the app already knows. The file is rewritten in full every time,
never appended to, because a collection shrinks as well as grows and a restore
has to know exactly what was there rather than a union of everything ever seen.

The app never uploads anything. Snapshots land in a folder the owner points
their own sync tool at.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import config as cfg
from .. import state_file
from . import discovery, scanner

FORMAT = "qobuz-librarian-collection-snapshot"
VERSION = 1

LATEST_NAME = "collection.json"
DATED_NAME_FMT = "collection-%Y%m%d-%H%M%S.json"
SUSPECT_NAME = "collection-suspect.json"
KEEP_DATED = 5

# An unmounted, dying or half-walked drive reads as a much smaller library.
# Losing more than this much of the album count freezes the good snapshot until
# the owner confirms the albums really are gone.
SHRINK_TOLERANCE = 0.10
# Under this size the percentage is noise: four albums down to three is an
# ordinary tidy-up, not a failing disk.
SHRINK_GUARD_MIN_ALBUMS = 10

_LOST = ("the record of what the library held (without it a lost disk can "
         "only be rebuilt by hand)")


def snapshot_dir() -> Path:
    """Where backups are written. Settings carries this as a plain string."""
    raw = str(getattr(cfg, "COLLECTION_BACKUP_DIR", "") or "").strip()
    return Path(raw) if raw else cfg.DATA_DIR / "collection-backups"


def latest_path() -> Path:
    return snapshot_dir() / LATEST_NAME


def suspect_path() -> Path:
    return snapshot_dir() / SUSPECT_NAME


def _previous_ids(previous):
    """Map artist name to (artist id, {album folder: album id}) from a snapshot.

    A scan skips artists whose folders have not changed, so it reports no Qobuz
    ids for them. Carrying the previous snapshot's ids forward is what keeps a
    restore able to fetch the exact album instead of guessing from a search.
    """
    index = {}
    for artist in (previous or {}).get("artists") or []:
        if not isinstance(artist, dict):
            continue
        name = artist.get("name")
        if not name:
            continue
        albums = {}
        for album in artist.get("albums") or []:
            if isinstance(album, dict) and album.get("name"):
                albums[album["name"]] = album.get("qobuz_album_id")
        index[name] = (artist.get("qobuz_artist_id"), albums)
    return index


def _track_entry(meta):
    entry = {
        "title": meta.get("title") or "",
        "disc": meta.get("discnumber") or 1,
        "number": meta.get("tracknumber") or 0,
        "isrc": meta.get("isrc") or "",
    }
    if meta.get("mb_trackid"):
        entry["mb_trackid"] = meta["mb_trackid"]
    return entry


def build_snapshot(*, owned_qobuz=None, artist_ids=None, previous=None,
                   source="scan", walk_errors=None):
    """Walk the library and return the snapshot document.

    The walk goes through the scanner rather than the track cache: the cache is
    an accelerator the scanner already uses, and it is known to disagree with
    disk by a few tracks, which is not a difference a rebuild record can carry.
    """
    owned_qobuz = owned_qobuz or {}
    artist_ids = artist_ids or {}
    carried = _previous_ids(previous)
    # Artist ids every past scan has ever resolved, keyed by folder name. An
    # ordinary scan skips unchanged folders and reports nothing for them, so
    # without this a library that never changes would back up id-less forever.
    resolved = discovery.cached_artist_resolutions()

    artists = []
    album_count = 0
    track_count = 0
    for artist_dir in scanner.list_library_artists(walk_errors=walk_errors):
        name = artist_dir.name
        prev_artist_id, prev_albums = carried.get(name, (None, {}))
        albums = []
        for album_dir in scanner.list_artist_album_dirs(
                artist_dir, walk_errors=walk_errors):
            tracks = scanner.read_album_dir(album_dir, walk_errors=walk_errors)
            if not tracks:
                continue
            album = {"name": album_dir.name}
            tagged = next((t.get("album") for t in tracks if t.get("album")), "")
            if tagged and tagged != album_dir.name:
                album["title"] = tagged
            album["qobuz_album_id"] = (
                owned_qobuz.get(name, {}).get(album_dir.name)
                or prev_albums.get(album_dir.name)
                or None)
            album["tracks"] = [_track_entry(t) for t in tracks]
            albums.append(album)
            album_count += 1
            track_count += len(tracks)
        if not albums:
            continue
        hit = resolved.get(name)
        cached_id = (str(hit[0])
                     if isinstance(hit, (list, tuple)) and len(hit) > 1 and hit[0]
                     else None)
        artists.append({
            "name": name,
            "qobuz_artist_id": (artist_ids.get(name) or prev_artist_id
                                or cached_id or None),
            "albums": albums,
        })

    now = datetime.now(timezone.utc)
    return {
        "format": FORMAT,
        "version": VERSION,
        "updated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "updated_at_epoch": now.timestamp(),
        "source": source,
        "music_root": str(cfg.MUSIC_ROOT),
        "counts": {"artists": len(artists), "albums": album_count,
                   "tracks": track_count},
        "artists": artists,
    }


def load_latest():
    """The last written snapshot, or None when there isn't a readable one."""
    return state_file.load_json_object(latest_path(), "the collection snapshot",
                                       _LOST)


def _album_count(snapshot):
    counts = (snapshot or {}).get("counts")
    if isinstance(counts, dict) and isinstance(counts.get("albums"), int):
        return counts["albums"]
    return 0


def shrink_verdict(snapshot, previous):
    """Return None when the snapshot is safe to keep, or a plain refusal.

    Zero artists is never written: an unmounted music folder looks exactly like
    a collection somebody deleted, and only one of those should survive into
    the backup.
    """
    if not (snapshot.get("artists") or []):
        return "No music found in your library folder. The last backup was kept."
    before = _album_count(previous)
    after = _album_count(snapshot)
    if before < SHRINK_GUARD_MIN_ALBUMS or after >= before:
        return None
    if (before - after) <= before * SHRINK_TOLERANCE:
        return None
    return (f"{before:,} albums last time, {after:,} now. The last backup "
            f"was kept.")


def write_snapshot(snapshot, *, force=False):
    """Write the snapshot, honouring the shrink guard. Returns (ok, reason).

    Holds the same lock as the read so two scans finishing together cannot
    interleave a compare with a write. Raises OSError; callers decide how a
    failure surfaces, and never fail a scan over it.
    """
    latest = latest_path()
    # The lock lives with the app's data, not in the backup folder: that folder
    # is synced offsite, and a stray .lock file riding along in it is noise.
    with state_file.store_lock(cfg.DATA_DIR / ".qobuz_collection_backup"):
        previous = None if force else state_file.load_json_object(
            latest, "the collection snapshot", _LOST)
        refusal = None if force else shrink_verdict(snapshot, previous)
        if refusal:
            # Kept beside the good copy rather than dropped: if the drive really
            # did lose albums, this is the file the owner confirms against.
            state_file.write_json(suspect_path(), snapshot)
            return False, refusal
        state_file.write_json(latest, snapshot)
        state_file.write_json(_dated_path(), snapshot)
        _prune_dated()
        # A confirmed write settles whatever the guard was holding out for.
        suspect_path().unlink(missing_ok=True)
    return True, None


def _dated_path() -> Path:
    """A dated copy's path, kept unique so two backups a second apart both
    survive instead of the second one replacing the first."""
    base = datetime.now(timezone.utc).strftime(DATED_NAME_FMT)
    path = snapshot_dir() / base
    n = 2
    while path.exists():
        path = snapshot_dir() / f"{base[:-5]}-{n}.json"
        n += 1
    return path


def _prune_dated():
    """Keep the newest KEEP_DATED dated copies. The name sorts by time."""
    try:
        dated = sorted(snapshot_dir().glob("collection-2*.json"))
    except OSError:
        return
    for old in dated[:-KEEP_DATED] if len(dated) > KEEP_DATED else []:
        try:
            old.unlink()
        except OSError:
            pass


def validate_upload(data):
    """Check an uploaded file is one of ours. Returns (ok, reason)."""
    if not isinstance(data, dict):
        return False, "That file isn't a collection snapshot from this app."
    if data.get("format") != FORMAT:
        return False, "That file isn't a collection snapshot from this app."
    if data.get("version") != VERSION:
        return False, (f"That snapshot was written by a different version of "
                       f"the app (version {data.get('version')!r}, this one "
                       f"reads {VERSION}).")
    artists = data.get("artists")
    if not isinstance(artists, list) or not artists:
        return False, "That snapshot doesn't list any artists."
    return True, None
