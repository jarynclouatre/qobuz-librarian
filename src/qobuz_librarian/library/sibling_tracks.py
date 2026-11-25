"""Recording matches between sibling album folders and their backups."""
import math
import os
import re
from pathlib import Path

from qobuz_librarian.library import catalog, scanner
from qobuz_librarian.library.tags import (
    canonical_recording_id,
    canonical_track_slot,
    canonical_track_title,
    disc_folder_number,
    normalize,
)


def read_held_track(descriptor, path):
    parsed = None
    if scanner.HAVE_MUTAGEN:
        try:
            with os.fdopen(os.dup(descriptor), "rb") as source:
                parsed = scanner.mutagen.File(source, easy=True)
        except OSError:
            raise
        except Exception:
            parsed = None

    if parsed is not None and parsed.tags:
        tags = parsed.tags

        def first(key):
            value = tags.get(key)
            return value[0] if value and isinstance(value, list) else ""

        title = first("title")
        if title:
            info = parsed.info
            disc = scanner.parse_track_num(first("discnumber"))
            result = {
                "title": title,
                "isrc": first("isrc").strip().replace("-", "").upper(),
                "mb_trackid": first("musicbrainz_trackid").strip().lower(),
                "album": first("album"),
                "albumartist": first("albumartist") or first("artist"),
                "tracknumber": scanner.parse_track_num(first("tracknumber")),
                "discnumber": disc or 1,
                "disc_tagged": bool(disc),
                "bits": getattr(info, "bits_per_sample", 0) if info else 0,
                "sample_rate": getattr(info, "sample_rate", 0) if info else 0,
                "channels": getattr(info, "channels", 0) if info else 0,
                "length": getattr(info, "length", 0.0) if info else 0.0,
                "path": str(path),
            }
            result["size"] = int(os.fstat(descriptor).st_size)
            result["normalized"] = normalize(title)
            return result

    match = re.match(r"^(\d+)[\s.\-]+(.+)$", path.stem)
    disc = re.match(r"(?:disc|cd)\s*0*(\d+)", path.parent.name, re.IGNORECASE)
    title = match.group(2) if match else path.stem
    return {
        "title": title,
        "tracknumber": int(match.group(1)) if match else 0,
        "isrc": "",
        "mb_trackid": "",
        "album": "",
        "albumartist": "",
        "discnumber": int(disc.group(1)) if disc else 1,
        "bits": 0,
        "sample_rate": 0,
        "channels": 0,
        "length": 0.0,
        "path": str(path),
        "size": int(os.fstat(descriptor).st_size),
        "normalized": normalize(title),
    }


def _same_recording_signal(a, b):
    """Whether two tracks have known durations within two seconds."""
    la, lb = a.get("length"), b.get("length")
    if isinstance(la, bool) or isinstance(lb, bool):
        return False
    try:
        la, lb = float(la or 0), float(lb or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        la > 0 and lb > 0
        and math.isfinite(la) and math.isfinite(lb)
        and abs(la - lb) <= 2.0
    )


def _edition_labels(track, folder):
    labels = (catalog._edition_title_parts(track.get("title"))[1]
              | catalog._edition_title_parts(track.get("album"))[1])
    if folder:
        labels |= catalog._edition_title_parts(folder.name, folder=True)[1]
    return catalog._recording_labels(labels)


def match_sibling_track(sibling_track, primary_tracks, *,
                        sibling_folder=None, primary_folder=None):
    match = _match_recording(sibling_track, primary_tracks,
                             sibling_folder=sibling_folder, primary_folder=primary_folder)
    if match is None or _edition_labels(sibling_track, sibling_folder) != _edition_labels(
            match, primary_folder):
        return None
    return match


def track_slot(track, album_dir):
    slot = canonical_track_slot(track, "discnumber", "tracknumber")
    if slot is None or not album_dir or not track.get("path"):
        return slot
    try:
        folders = Path(track["path"]).relative_to(album_dir).parts[:-1]
    except ValueError:
        return None
    discs = {disc for name in folders if (disc := disc_folder_number(name))}
    if not discs:
        return slot
    if len(discs) != 1:
        return None
    disc = discs.pop()
    if track.get("disc_tagged") is not False and slot[0] != disc:
        return None
    return disc, slot[1]


def _match_recording(sibling_track, primary_tracks, *, sibling_folder, primary_folder):
    """Match sibling → primary by ISRC > MBID > (disc, title + same recording)."""
    s_isrc = canonical_recording_id(sibling_track.get("isrc"), "isrc")
    s_mbid = canonical_recording_id(
        sibling_track.get("mb_trackid"), "mb_trackid")
    if s_isrc is None or s_mbid is None:
        return None
    s_title_norm = canonical_track_title(sibling_track.get("title"))

    if s_isrc:
        matches = []
        for pt in primary_tracks:
            primary_isrc = canonical_recording_id(pt.get("isrc"), "isrc")
            primary_mbid = canonical_recording_id(
                pt.get("mb_trackid"), "mb_trackid")
            if (
                primary_isrc is None
                or primary_mbid is None
                or primary_isrc != s_isrc
            ):
                continue
            if s_mbid and primary_mbid and s_mbid != primary_mbid:
                continue
            matches.append(pt)
        # A one-sided recording ID is uncertainty, not permission to fall back
        # to a title before deleting the sibling's file.
        return matches[0] if len(matches) == 1 else None
    if s_mbid:
        matches = []
        for pt in primary_tracks:
            primary_isrc = canonical_recording_id(pt.get("isrc"), "isrc")
            primary_mbid = canonical_recording_id(
                pt.get("mb_trackid"), "mb_trackid")
            if (
                primary_isrc is None
                or primary_mbid is None
                or primary_isrc
                or primary_mbid != s_mbid
            ):
                continue
            matches.append(pt)
        return matches[0] if len(matches) == 1 else None
    s_slot = track_slot(sibling_track, sibling_folder)
    if not s_title_norm or s_slot is None:
        return None
    in_slot = [pt for pt in primary_tracks if track_slot(pt, primary_folder) == s_slot]
    if len(in_slot) != 1:
        return None
    pt = in_slot[0]
    if (canonical_recording_id(pt.get("isrc"), "isrc") == ""
            and canonical_recording_id(pt.get("mb_trackid"), "mb_trackid") == ""
            and canonical_track_title(pt.get("title")) == s_title_norm
            and _same_recording_signal(sibling_track, pt)):
        return pt
    return None
