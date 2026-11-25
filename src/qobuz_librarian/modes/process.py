"""Core album processing: detect gaps, prompt, download, import, consolidate."""
import json
import math
import os
import stat
import subprocess
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock, safefile, saved_views
from qobuz_librarian.api import client as api_client
from qobuz_librarian.download import (
    clear_attempts_after_import,
    discard_download_staging,
    discard_retry_staging,
    download_holds_audio,
    keep_unimported_download,
    note_superseded_attempts,
    retain_download_staging,
    retire_download_staging_after_import,
    retire_empty_download_staging,
    retire_failed_download_staging,
    run_album_download,
    validated_staged_album_dirs,
)
from qobuz_librarian.download_result import (
    processing_attention,
    shortfall_fields,
    track_shortfall,
    tracks_missing,
)
from qobuz_librarian.integrations import beets, lyric_fetch, track_annotations
from qobuz_librarian.integrations.beets import (
    _ART_EXTS,
    MusicDiskFull,
    _beets_snapshot_from_record,
    _beets_snapshot_record,
    _prepare_staging_tags,
    beets_import_paths,
    capture_beets_album_entries,
    fill_album_tags,
    forget_imported_beets_entries,
    relocate_disc_album_artwork,
    retire_backup_beets_entries,
    retire_replaced_beets_entries,
    staging_preflight,
)
from qobuz_librarian.integrations.lyrics import write_post_import_sidecars
from qobuz_librarian.integrations.rip import (
    audio_decodes,
    is_cancel_requested,
    snapshot_staging,
)
from qobuz_librarian.library import candidate_premise, catalog, tags
from qobuz_librarian.library.backup import (
    backup_album_dir,
    backup_files_preserved,
    capture_album_source_receipt,
    carry_backup_companions,
    companion_conflicts,
    dispose_backup,
    interrupted_upgrades,
    mark_upgrade_pending,
    pin_unverified_upgrade_backup,
    put_back_healthy_tracks,
    restore_gap_fill_backup,
    restore_incomplete_upgrade_backup,
    restore_upgrade_backup,
    unread_audio,
    unwritable_folder,
    unwritable_new_album_folder,
    upgrade_backup_restorable,
    warn_pin_failed,
    without_unread_audio,
)
from qobuz_librarian.library.catalog import (
    _disc_scoped_match,
    album_quality_label,
    compute_missing,
    find_album_dir_by_track_signatures,
    find_album_dir_filesystem,
    find_expanded_edition,
    find_extras_in_existing,
    folder_holds_all_tracks,
    is_lossless_album,
    prompt_and_migrate_multi_artist_folder,
    separate_edition_folder,
    track_signatures_for_album_dirs,
)
from qobuz_librarian.library.scanner import (
    cache_album_tags,
    clear_scan_caches,
    drop_artist_subdirs_cache,
    read_album_dir,
)
from qobuz_librarian.library.tags import (
    canonical_recording_id,
    canonical_track_slot,
    canonical_track_title,
    normalize,
    strip_edition_suffix,
)
from qobuz_librarian.modes.consolidate import (
    ConsolidationCancelled,
    consolidate_albums,
)
from qobuz_librarian.quality.decision import (
    compare_album_quality,
    existing_track_quality,
    lossy_label,
    mark_local_album_capped,
    quality_relation,
    track_relation,
)
from qobuz_librarian.quality.verify import (
    quality_below_line,
    quality_met_line,
    redownload_with_staged_fallback,
    retry_preserves_track_coverage,
    verify_and_recover,
)
from qobuz_librarian.queue.executor import (
    _pre_import_outcome_fields,
    _pre_import_staging_hooks,
)
from qobuz_librarian.repair_log import warn_if_download_truncated
from qobuz_librarian.ui_cli.colors import C, fmt, format_size, section, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import (
    confirm,
    download_question,
    print_album_summary,
    prompt_edition_pick,
)


def _recover_incomplete_upgrade_backup(backup, album_dir, *, operation):
    """Best-effort recovery for a backup transaction that did not complete.

    The caller must still abort the requested operation.  A complete recovery
    only means the exact original was put back and the recovery copy was
    retired; it does not turn the interrupted backup into permission to carry
    on with a destructive replacement.
    """
    outcome = restore_incomplete_upgrade_backup(backup, album_dir)
    recovered = bool(
        outcome is not None
        and outcome.unresolved == 0
        and outcome.backup_disposed
    )
    if recovered:
        log.info(fmt(
            C.GREEN,
            f"  ✓  {album_dir} is as it was; its copy in the backup folder "
            "was removed." if outcome.restored == 0 else
            f"  ✓  Restored the original folder after the interrupted "
            f"{operation}."))
        return outcome

    _pin_or_warn(
        backup,
        f"{operation} recovery kept; automatic reconciliation did not "
        "complete")
    unresolved = (
        f" ({plural(outcome.unresolved, 'original file')} still unresolved)"
        if outcome is not None else ""
    )
    log.info(fmt(
        C.RED,
        f"  ✗  The interrupted {operation} could not be fully restored"
        f"{unresolved}."))
    log.info(fmt(
        C.WHITE,
        f"     Recovery data remains at {backup}; leave it there until the "
        "album is reconciled."))
    return outcome


def force_cleanup_preflight(album_dir, args, *, expected_album_receipt=None):
    """With --force, move an existing album dir aside to a backup before
    re-import, so beets doesn't make '<n>.1.flac' collisions against the old
    files, and a re-download that fails can be restored.

    Returns the backup Path when the folder was moved aside; True when there's
    nothing to move; None when the operation must stop without further prompts;
    False when the user declined or no backup data was retained. --yes does NOT
    silence this prompt."""
    if not args.force:
        return True

    if not album_dir or not album_dir.exists():
        return True

    # Refuse to --force a symlinked album dir: the redownload would land files
    # through the surviving link into the target, wiping the original without
    # an undo.
    if album_dir.is_symlink():
        log.info("")
        log.info(fmt(C.RED + C.BOLD,
            "  ✗  --force on a symlinked album folder is unsafe:"))
        log.info(fmt(C.WHITE, f"     {album_dir}"))
        log.info(fmt(C.GRAY,
            "     Resolve the symlink (or pick the real path) and re-run."))
        return None

    total_size = 0
    audio_files = []
    for f in album_dir.rglob("*"):
        if f.is_file():
            try:
                total_size += f.stat().st_size
            except OSError:
                pass
            if f.suffix.lower() in cfg.AUDIO_EXTS:
                audio_files.append(f)

    log.info("")
    log.info(fmt(C.YELLOW + C.BOLD, "  ⚠  --force AND existing album folder detected:"))
    log.info(fmt(C.WHITE, f"     {album_dir}"))
    log.info(fmt(C.GRAY,
        f"     {plural(len(audio_files), 'audio file')}, "
        f"{format_size(total_size)} total"))
    log.info(fmt(C.GRAY,
        "     Left in place, beets would create '<n>.1.flac' alongside the old files."))

    move_it = confirm(
        "\n  Move this folder to a backup before re-downloading? "
        "(restored automatically if the re-download fails)",
        default_yes=True, auto_yes=False, q_is_no=False)
    if not move_it:
        log.info(fmt(C.YELLOW, "  Continuing without moving it. Expect file collisions."))
        return False

    backup_path = (
        backup_album_dir(
            album_dir,
            expected_receipt=expected_album_receipt,
        )
        if expected_album_receipt is not None
        else backup_album_dir(album_dir)
    )
    if backup_path is not None and not backup_path.complete:
        _recover_incomplete_upgrade_backup(
            backup_path, album_dir, operation="forced re-download backup")
        log.info(fmt(C.RED,
            "  ✗  The backup was interrupted; refusing to continue with "
            "--force."))
        return None
    if backup_path is None:
        log.info(fmt(C.RED,
            "  ✗  Couldn't back up the folder; refusing to remove it. "
            "Expect collisions."))
        return False
    log.info(fmt(C.GREEN,
        "  ⤷  Moved existing folder to a backup (auto-restore on failure)."))
    return backup_path


_UPGRADE_VERIFY_DURATION_RATIO = 0.97


def _folder_tracks_checked(folder):
    """(tracks, degraded) for an album folder, read through read_album_dir so
    lengths/quality come from the same reader the rest of the app uses."""
    errs = []
    tracks = read_album_dir(folder, walk_errors=errs)
    return tracks, bool(errs)


def _tracks_seconds(tracks):
    total = 0.0
    for t in tracks:
        try:
            total += float(t.get("length") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _track_quality(t):
    """(bit_depth, sample_rate) for one track, (0, 0) when unreadable."""
    try:
        return (int(t.get("bits") or 0), int(t.get("sample_rate") or 0))
    except (TypeError, ValueError):
        return (0, 0)


def _fmt_quality(q):
    return f"{q[0]}-bit/{(q[1] or 0) / 1000:g}kHz"


def _known_track_channels(track):
    try:
        channels = int(track.get("channels") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return channels if channels > 0 else None


def _known_track_duration(track):
    if isinstance(track.get("length"), bool):
        return None
    try:
        duration = float(track.get("length") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return duration if duration > 0 and math.isfinite(duration) else None


def _recording_identity(track, field):
    return canonical_recording_id(track.get(field), field)


def _known_track_slot(track):
    return canonical_track_slot(track, "discnumber", "tracknumber")


def _unidentified_track_match(original, replacement):
    old_title = canonical_track_title(original.get("title"))
    new_title = canonical_track_title(replacement.get("title"))
    old_slot = _known_track_slot(original)
    old_duration = _known_track_duration(original)
    new_duration = _known_track_duration(replacement)
    return bool(
        old_title
        and old_title == new_title
        and old_slot is not None
        and old_slot == _known_track_slot(replacement)
        and old_duration is not None
        and new_duration is not None
        and abs(old_duration - new_duration) <= 2.0
    )


def _upgrade_track_identity_matches(original, replacement):
    old_isrc = _recording_identity(original, "isrc")
    new_isrc = _recording_identity(replacement, "isrc")
    old_mbid = _recording_identity(original, "mb_trackid")
    new_mbid = _recording_identity(replacement, "mb_trackid")
    if None in (old_isrc, new_isrc, old_mbid, new_mbid):
        return False
    if tags.isrc_on_one_side(original, replacement):
        return False
    if old_isrc:
        return bool(
            old_isrc == new_isrc
            and not (old_mbid and new_mbid and old_mbid != new_mbid)
        )
    if old_mbid or new_mbid:
        return bool(old_mbid and old_mbid == new_mbid)
    return _unidentified_track_match(original, replacement)


def _damaged_track_match(original, replacement):
    old_slot = _known_track_slot(original)
    old_title = canonical_track_title(original.get("title"))
    return bool(
        old_title
        and old_title == canonical_track_title(replacement.get("title"))
        and old_slot is not None
        and old_slot == _known_track_slot(replacement)
    )


def _held_paths(backup_path, names):
    return {os.fspath(Path(backup_path) / name) for name in names}


def _pair_upgrade_tracks_for_disposal(originals, replacements, damaged=()):
    """Return unique, destruction-safe original/replacement pairs.

    The normal library matcher deliberately tolerates missing identifiers and
    fuzzy edition titles so scans can find music. Deleting the upgrade backup
    needs stronger evidence: one-sided recording IDs cannot fall back to a
    title, and unidentified tracks must agree on slot, title, and duration.
    A file Repair found damaged pairs on slot and title alone: its recording
    ID is why it was re-downloaded, and the damage can throw its length.
    """
    replacement_slots = [
        slot for track in replacements
        if (slot := _known_track_slot(track)) is not None
    ]
    if len(replacement_slots) != len(set(replacement_slots)):
        return None

    pairs = []
    claimed = set()
    for original in originals:
        matches = (_damaged_track_match if original.get("path") in damaged
                   else _upgrade_track_identity_matches)
        candidates = [
            index for index, replacement in enumerate(replacements)
            if matches(original, replacement)
        ]
        if len(candidates) != 1 or candidates[0] in claimed:
            return None
        index = candidates[0]
        claimed.add(index)
        pairs.append((original, replacements[index]))
    return pairs


def _track_identity_conflicts(original, replacement):
    for field in ("isrc", "mb_trackid"):
        old_value = _recording_identity(original, field)
        new_value = _recording_identity(replacement, field)
        if old_value is None or new_value is None:
            return True
        if old_value and new_value and old_value != new_value:
            return True
    return False


def _upgrade_replacement_verified(album, album_dir, backup_path, damaged=()):
    """True only when the freshly imported album is at least as complete as the
    backed-up original: same-or-more tracks AND same-or-more playtime. The
    success gate clears `flac -t` per file, which proves each file decodes but
    not that the matcher kept every track or that a re-rip didn't land short.
    """
    # An upgrade is filed into the album's own folder. Otherwise beets names
    # the folder from the tags, so it can differ from the one resolved
    # before the download.
    drop_artist_subdirs_cache(album_dir.parent)
    post_dir = album_dir if album_dir.is_dir() else find_album_dir_filesystem(album)
    if not post_dir or not post_dir.exists():
        return False
    return _upgrade_trees_verified(post_dir, backup_path, damaged)


def _upgrade_trees_verified(post_dir, backup_path, damaged=()):
    """Repair and Gap Fill can keep a replacement at the original quality."""
    return bool(_upgrade_comparison(backup_path, [post_dir], damaged))


def _upgrade_improves_original(post_dir, backup_path):
    return _upgrade_comparison(backup_path, [post_dir]) == "higher"


def _upgrade_same_quality(backup_path, staged_dirs):
    return _upgrade_comparison(backup_path, staged_dirs, quiet=True) == "equal"


def _upgrade_comparison(backup_path, replacement_dirs, damaged=(), *, quiet=False,
                        matched_pairs=None):
    """Compare complete sets of paired recordings, including their channels and playtime."""
    def refused(reason):
        if not quiet:
            log.info(fmt(C.YELLOW, reason))
        return None

    damaged = _held_paths(backup_path, damaged)
    old_tracks, degraded = _folder_tracks_checked(backup_path)
    if degraded:
        return refused("  ⚠  Couldn't fully read the backup while verifying the upgrade.")
    new_tracks = []
    for folder in replacement_dirs:
        tracks, degraded = _folder_tracks_checked(folder)
        if degraded:
            return refused("  ⚠  Couldn't fully read the imported album while verifying the upgrade.")
        new_tracks.extend(tracks)
    new_n, new_secs = len(new_tracks), _tracks_seconds(new_tracks)
    old_n, old_secs = len(old_tracks), _tracks_seconds(old_tracks)
    # An empty or unreadable backup reads as (0, 0.0), which makes both gates
    # below pass vacuously (new_n < 0 is never true), deleting the only full
    # copy. Treat an unreadable/empty backup as unverifiable and keep it.
    if old_n == 0:
        return refused(
            "  ⚠  Couldn't read the upgrade backup (empty or unreadable).")
    if new_n < old_n:
        return refused(
            f"  ⚠  Upgrade landed {plural(new_n, 'track')} but the original held "
            f"{old_n}.")
    if old_secs > 0 and new_secs < old_secs * _UPGRADE_VERIFY_DURATION_RATIO:
        return refused(
            f"  ⚠  Upgrade playtime {int(new_secs)}s falls short of the "
            f"original {int(old_secs)}s (a track may be truncated).")
    # Every original must have one identity-paired replacement.
    pairs = _pair_upgrade_tracks_for_disposal(old_tracks, new_tracks, damaged)
    if pairs is None:
        return refused(
            "  ⚠  Couldn't prove one unique replacement for every original "
            "track.")
    improved = new_n > old_n
    for ot, nt in pairs:
        oq, nq = _track_quality(ot), _track_quality(nt)
        title = truncate(str(ot.get("title") or ""), 40)
        if ot.get("path") not in damaged and _track_identity_conflicts(ot, nt):
            return refused(
                f"  ⚠  Upgrade matched {title} to a different recording "
                "identity.")
        if ot.get("lossy") and not nt.get("lossy"):
            relation = "higher" if track_relation(ot, nq) == "lower" else "unknown"
        else:
            relation = quality_relation(nq, oq)
        if relation not in ("equal", "higher"):
            detail = (
                "trades bit depth for sample rate"
                if relation == "incomparable"
                else f"returned at {_fmt_quality(nq)} instead of {_fmt_quality(oq)}"
            )
            return refused(
                f"  ⚠  Upgrade for {title} {detail}.")
        if relation == "higher":
            improved = True
        old_channels = _known_track_channels(ot)
        new_channels = _known_track_channels(nt)
        if old_channels is None or new_channels is None:
            return refused(
                f"  ⚠  Couldn't verify the channel layout for {title}.")
        if new_channels != old_channels:
            return refused(
                f"  ⚠  Upgrade for {title} changed from {old_channels} to "
                f"{new_channels} channels.")
        old_duration = _known_track_duration(ot)
        new_duration = _known_track_duration(nt)
        if old_duration is None or new_duration is None:
            return refused(
                f"  ⚠  Couldn't verify the playtime for {title}.")
        if new_duration < old_duration * _UPGRADE_VERIFY_DURATION_RATIO:
            return refused(
                f"  ⚠  Upgrade for {title} is shorter than the original.")
    if matched_pairs is not None:
        matched_pairs.extend(pairs)
    return "higher" if improved else "equal"


def _repair_keep_originals(backup, staged_dirs, damaged):
    """Leave only repairs, higher-quality recordings and new tracks for import."""
    pairs = []
    if not damaged or not _upgrade_comparison(
            backup, staged_dirs, damaged, matched_pairs=pairs):
        return None
    held = _held_paths(backup, damaged)
    if not held.issubset(old["path"] for old, _ in pairs):
        return None
    keep = {}
    for old, new in pairs:
        if old["path"] in held or old.get("lossy") or _track_quality(old) != _track_quality(new):
            continue
        if abs(_known_track_duration(old) - _known_track_duration(new)) > 2:
            return None
        path = Path(old["path"])
        if not audio_decodes(path):
            return None
        staged = Path(new["path"])
        keep[path.relative_to(backup).as_posix()] = (staged, safefile.stat_identity(staged.lstat()))
    for staged, identity in keep.values():
        if not safefile.delete_verified(staged, identity):
            return None
    return list(keep)


def _intentional_replacement_verified(replacement_path, _backup_path):
    """Final exact-tree gate for a user-requested forced replacement."""
    tracks, degraded = _folder_tracks_checked(replacement_path)
    return bool(tracks) and not degraded


# Tags the new download and its tagging own, or that describe the old master
# rather than the recording (gain, encoder, catalogue ids). Whatever else an
# original track carries is the user's own.
_REPLACEMENT_OWNED_TAGS = frozenset({
    "title", "titlesort", "version", "subtitle", "artist", "artists",
    "artistsort", "artists_sort", "artist_credit", "artists_credit", "album",
    "albumsort", "albumartist", "album artist", "albumartists",
    "albumartistsort", "albumartists_sort", "albumartist_credit",
    "albumartists_credit", "tracknumber", "tracktotal", "totaltracks",
    "discnumber", "disctotal", "totaldiscs", "discsubtitle", "date", "year",
    "originaldate", "originalyear", "genre", "composer", "composersort",
    "lyricist", "arranger", "conductor", "performer", "producer", "remixer",
    "label", "publisher", "organization", "copyright", "isrc", "barcode",
    "upc", "ean", "catalognumber", "catalogid", "media", "releasecountry",
    "releasestatus", "releasetype", "script", "language", "asin", "bpm",
    "initialkey", "compilation", "encoder", "encodedby", "encoded_by",
    "encodersettings", "encoder_settings", "length", "itunesadvisory",
    "explicit", "url", "website",
})
_REPLACEMENT_OWNED_PREFIXES = (
    "replaygain_", "r128_", "musicbrainz_", "acoustid_", "discogs_",
)


_LYRIC_TAGS = frozenset({"lyrics", "unsyncedlyrics"})


def _carried_track_tags(entries, target):
    """Keep the original track's own tags, preferring synced lyrics."""
    tags = [
        (key, value) for key, value in entries
        if key.lower() not in _REPLACEMENT_OWNED_TAGS
        and not key.lower().startswith(_REPLACEMENT_OWNED_PREFIXES)
        and (key.lower() not in _LYRIC_TAGS or value.strip())
    ]
    rank = ("none", "plain", "synced").index
    lyric_rank = max((rank(lyric_fetch.classify(value)) for key, value in tags
                      if key.lower() in _LYRIC_TAGS), default=0)
    if rank(lyric_fetch.classify(lyric_fetch.get_existing_lyrics(target))) > lyric_rank:
        tags = [(key, value) for key, value in tags if key.lower() not in _LYRIC_TAGS]
    return tags


def _missing_track_pictures(original, target):
    embedded = {picture.data for picture in target.pictures}
    missing = []
    for picture in original:
        if picture.data not in embedded:
            missing.append(picture)
            embedded.add(picture.data)
    return missing


def _annotations_to_carry(old, target):
    """The original track's own tags and pictures to keep on the replacement."""
    from mutagen.flac import FLAC

    if Path(old["path"]).suffix.lower() == ".flac":
        source = FLAC(old["path"])
        entries, pictures = source.tags or [], source.pictures
    else:
        entries, pictures = track_annotations.read(old["path"])
    return _carried_track_tags(entries, target), _missing_track_pictures(pictures, target)


def _track_annotations_preserved(backup_path, replacement_path):
    from mutagen.flac import FLAC

    new_tracks, new_degraded = _folder_tracks_checked(replacement_path)
    old_tracks, old_degraded = _folder_tracks_checked(backup_path)
    pairs = (None if new_degraded or old_degraded
             else _pair_upgrade_tracks_for_disposal(old_tracks, new_tracks))
    if pairs is None:
        return False
    for old, new in pairs:
        try:
            target = FLAC(new["path"])
            tags, pictures = _annotations_to_carry(old, target)
            if not _tags_present(tags, target) or pictures:
                return False
        except Exception:
            return False
    return True


def _tags_present(tags, target):
    """Whether ``target`` already holds each of ``tags`` as it is."""
    original = {}
    for key, value in tags:
        original.setdefault(key.lower(), []).append(value)
    if original.keys() & _LYRIC_TAGS:
        for key in _LYRIC_TAGS:
            original.setdefault(key, [])
    return all(
        values == [value for value in target.get(key, [])
                   if key not in _LYRIC_TAGS or value.strip()]
        for key, values in original.items())


def _carry_track_annotations(backup_path, replacement_path, *, edition=None,
                             damaged=(), kept=()):
    """Give each replacement track the user's own tags and pictures.

    Original tags take precedence unless the new download owns that kind
    of tag. Distinct original pictures follow the download's embedded art.
    Returns False when a track could not be paired or written.
    """
    new_tracks, new_degraded = _folder_tracks_checked(replacement_path)
    if new_degraded:
        return False
    if edition is not None:
        new_tracks = catalog.edition_files(edition, new_tracks, replacement_path)
    return _copy_track_annotations(backup_path, new_tracks, damaged=damaged, kept=kept)


def _carry_staged_upgrade_tags(backup_path, album_dirs, *, damaged=()):
    """Give beets the tags it will index when it imports these tracks."""
    tracks = []
    for folder in album_dirs:
        found, degraded = _folder_tracks_checked(folder)
        if degraded:
            return
        tracks.extend(found)
    _copy_track_annotations(backup_path, tracks, tags_only=True, damaged=damaged)


def _copy_track_annotations(backup_path, new_tracks, *, damaged=(), tags_only=False, kept=()):
    from mutagen.flac import FLAC

    old_tracks, old_degraded = _folder_tracks_checked(backup_path)
    pairs = (None if old_degraded
             else _pair_upgrade_tracks_for_disposal(
                 old_tracks, new_tracks, _held_paths(backup_path, damaged)))
    if pairs is None:
        return False
    carried_to = 0
    preserved = _held_paths(backup_path, kept)
    for old, new in pairs:
        if old["path"] in preserved:
            continue
        target_path = Path(new["path"])
        try:
            target = FLAC(str(target_path))
            if target.tags is None:
                target.add_tags()
            lyric_keys = _LYRIC_TAGS
            tags, pictures = _annotations_to_carry(old, target)
            if tags_only:
                pictures = []
            if not pictures and _tags_present(tags, target):
                continue
            if any(key.lower() in lyric_keys for key, _value in tags):
                for key in lyric_keys:
                    if key in target.tags:
                        del target.tags[key]
            for key in {key.lower() for key, _value in tags}:
                if key in target.tags:
                    del target.tags[key]
            target.tags.extend(tags)
            for picture in pictures:
                target.add_picture(picture)
            lyric_fetch.save_flac_tags(target, target_path)
        except Exception as e:
            if not tags_only:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Couldn't carry your own tags onto "
                    f"{truncate(target_path.name, 40)} ({e}); keeping the "
                    "backup."))
            return False
        carried_to += 1
    if carried_to and not tags_only:
        log.info(fmt(C.GRAY,
            f"  ⤷  Copied metadata from the originals to {plural(carried_to, 'track')}."))
    return True


def _carry_non_audio_from_backup(album, album_dir, backup_path,
                                 replacement_dir=None, damaged=(), kept=()):
    """Carry companions and the user's own track tags between exact trees.

    Returns the exact replacement path, its updated receipt, and the names
    left in the backup: companions the replacement has a different file for,
    and audio the album reader skips. Callers must use the path and receipt
    for the final held-view disposal decision, and keep the backup while
    anything was left in it. Any changed name, symlink, changed file, or
    uncertain durability keeps the backup and returns ``None``.
    """
    dest = replacement_dir
    if dest is None:
        dest = find_album_dir_filesystem(album)
        if not dest or not dest.exists():
            dest = album_dir
    if not dest or not dest.exists():
        return None
    if not _carry_track_annotations(backup_path, dest, damaged=damaged, kept=kept):
        return None
    receipt = capture_album_source_receipt(dest)
    if receipt is None:
        return None
    renamed = _lyrics_follow_tracks(backup_path, dest, damaged)
    updated = carry_backup_companions(
        backup_path,
        dest,
        expected_replacement_receipt=receipt,
        skip_conflicts=True,
        renamed=renamed,
    )
    if updated is None:
        return None
    left = companion_conflicts(backup_path, updated, renamed)
    unread = unread_audio(backup_path.receipt, backup_path, updated)
    if left is None or unread is None:
        return None
    return dest, updated, sorted(left + unread)


def _lyrics_follow_tracks(backup_path, replacement_path, damaged=()):
    """The name each of the backup's .lrc files takes in the replacement:
    that of the track its own track became, where that has another name.
    None are renamed when the tracks can't be paired, or when two would end
    up with one name."""
    backup_path = Path(os.fspath(backup_path))
    replacement_path = Path(os.fspath(replacement_path))
    new_tracks, new_degraded = _folder_tracks_checked(replacement_path)
    old_tracks, old_degraded = _folder_tracks_checked(backup_path)
    pairs = (None if new_degraded or old_degraded
             else _pair_upgrade_tracks_for_disposal(
                 old_tracks, new_tracks, _held_paths(backup_path, damaged)))
    renamed = {}
    for old, new in pairs or ():
        source = Path(old["path"]).with_suffix(".lrc")
        if source.is_file():
            renamed[source.relative_to(backup_path).as_posix()] = (
                Path(new["path"]).with_suffix(".lrc")
                .relative_to(replacement_path).as_posix())
    names = [renamed.get(name, name) for name in (
        path.relative_to(backup_path).as_posix()
        for path in backup_path.rglob("*.lrc"))]
    return renamed if len(set(names)) == len(names) else {}


def _keep_own_cover(backup, staged_dirs):
    """Give the download the album's own cover art where both have one.

    A staged image with a different image at the same place in the backup,
    under that name or one that differs from it only in case, takes the
    backup's bytes, so the import files the user's cover instead of Qobuz's
    and the backup's proof finds it unchanged.
    """
    kept = 0
    for staged in staged_dirs:
        staged = Path(staged)
        try:
            images = sorted(
                path for path in staged.rglob("*")
                if path.suffix.lower() in _ART_EXTS
                and not path.is_symlink() and path.is_file())
        except OSError:
            continue
        for image in images:
            own = Path(backup) / image.relative_to(staged)
            try:
                if not os.path.lexists(own) and own.parent.is_dir():
                    named = [path for path in own.parent.iterdir()
                             if path.name.casefold() == own.name.casefold()]
                    own = named[0] if len(named) == 1 else own
                if own.is_symlink() or not own.is_file():
                    continue
                data = own.read_bytes()
                if data == image.read_bytes():
                    continue
                safefile.write_atomic(
                    image, data, mode=stat.S_IMODE(image.stat().st_mode),
                    prefix=".ql-own-cover-")
                kept += 1
            except OSError as e:
                vlog(f"couldn't stage your own {image.name}: {e}")
    if kept:
        log.info(fmt(C.GRAY, "  ⤷  Kept your own cover art for the album."))


def _keep_backup_for_companions(backup_path, left):
    """Pin a backup that holds the only copy of files it could not carry.

    Returns the log line naming them and where they are.
    """
    names = ", ".join(left[:3]) + (
        f" and {len(left) - 3} more" if len(left) > 3 else "")
    one = len(left) == 1
    _pin_or_warn(
        backup_path,
        f"kept for the original {names}, which the new album does not hold")
    return (
        f"  ⚠  The original {names} {'is' if one else 'are'} not in the new "
        f"album, so {'it is' if one else 'they are'} kept in the backup at "
        f"{backup_path}.")


def _put_original_back(backup, album_dir):
    """Undo an upgrade whose replacement failed verification.

    The replacement was imported into ``album_dir`` after the original left
    it, so that folder holds only the new download. It is moved into the
    backup area as a backup of its own, marked as set aside before it is
    complete, and the original album is restored to ``album_dir``. Returns
    the set-aside backup, or True when nothing had landed there, once the
    original is back. Otherwise returns None with
    the original's backup pinned; the library is then left holding the
    replacement.
    """
    aside = None
    if not upgrade_backup_restorable(backup, album_dir):
        _pin_or_warn(backup)
        return None
    if album_dir.is_dir():
        aside = backup_album_dir(album_dir, set_aside=True)
        if aside is not None and not aside.complete:
            _recover_incomplete_upgrade_backup(
                aside, album_dir, operation="move of the new download")
            aside = None
        if aside is None:
            _pin_or_warn(backup)
            return None
    if restore_upgrade_backup(backup, album_dir):
        return aside or True
    if aside is not None and not restore_upgrade_backup(aside, album_dir):
        log.info(fmt(C.RED,
            f"  ✗  The new download could not be put back either; it is at "
            f"{aside}."))
    _pin_or_warn(backup)
    return None


def mark_pending_upgrade(backup, entries, staged_dirs):
    """Record in an upgrade's backup the album's beets entries and the
    folders its download is imported from, for a start-up after a hard stop.
    """
    return mark_upgrade_pending(backup, json.dumps({
        "beets": _beets_snapshot_record(entries),
        "staged": [os.fspath(directory) for directory in staged_dirs],
    }, sort_keys=True))


def _finish_upgrade(backup, album_dir, entries):
    """Finish an upgrade whose download is in ``album_dir`` as the run
    would have. False, with nothing removed, when the download doesn't
    check out against the original or its beets entries can't take it."""
    if not _upgrade_improves_original(album_dir, backup.path):
        return False
    carried = _carry_non_audio_from_backup(
        None, album_dir, backup, replacement_dir=album_dir)
    if carried is None:
        return False
    replacement_path, replacement_receipt, left = carried
    if not retire_replaced_beets_entries(
            entries, replacement_path,
            _replacement_audio_paths(replacement_path, replacement_receipt)):
        return False
    if left:
        log.info(fmt(C.YELLOW, _keep_backup_for_companions(backup, left)))
    elif not dispose_backup(
            backup,
            replacement_path=replacement_path,
            expected_replacement_receipt=replacement_receipt,
            replacement_validator=without_unread_audio(
                _upgrade_improves_original, backup, replacement_receipt)):
        if not pin_unverified_upgrade_backup(
                backup,
                "upgrade kept; final exact replacement proof did not hold"):
            warn_pin_failed(backup.path)
    return True


def finish_interrupted_upgrades():
    """Finish verified upgrades and put back originals after a failed import.

    An older import at equal quality keeps its original in Backups for
    review. An album still in its backup is left to the start-up put-back.
    Returns the album folders upgraded and those put back.
    """
    finished, put_back = [], []
    for backup, note in interrupted_upgrades():
        try:
            record = json.loads(note)
            entries = _beets_snapshot_from_record(record["beets"])
            staged = [Path(directory) for directory in record["staged"]]
        except (KeyError, TypeError, ValueError):
            continue
        if entries is None:
            continue
        album_dir = Path(backup.receipt["origin"])
        if not album_dir.is_dir():
            if album_dir.parent.is_dir():
                forget_imported_beets_entries(entries, album_dir, staged)
            continue
        if _finish_upgrade(backup, album_dir, entries):
            log.info(fmt(C.GRAY,
                f"  ✓  Finished the upgrade of {album_dir} that an earlier "
                "run left unfinished."))
            saved_views.forget_upgraded_album(None, album_dir)
            finished.append(album_dir)
            continue
        if _upgrade_same_quality(backup.path, [album_dir]):
            # An older run may already have retired the original beets rows.
            _pin_or_warn(backup, "interrupted upgrade imported the same quality")
            log.info(fmt(C.YELLOW,
                f"  An interrupted upgrade of {album_dir} imported the same "
                f"quality. The original is at {backup} for review in Settings."))
            continue
        try:
            album_dir.rmdir()
        except OSError:
            pass
        aside = _put_original_back(backup, album_dir)
        if aside is None:
            continue
        log.info(fmt(C.YELLOW,
            f"  ⟲  Put back the original album in {album_dir}; an earlier "
            "run's upgrade of it could not be finished."))
        if aside is not True:
            log.info(fmt(C.GRAY, f"     The new download was set aside at {aside}."))
        forget_imported_beets_entries(entries, album_dir, staged)
        put_back.append(album_dir)
    if finished or put_back:
        clear_scan_caches()
    return finished, put_back


def _replacement_audio_paths(replacement_path, receipt):
    try:
        files = receipt["tree"]["files"]
        if not isinstance(files, dict):
            return ()
        return tuple(
            replacement_path / relative
            for relative in files
            if Path(relative).suffix.lower() in cfg.AUDIO_EXTS
        )
    except (KeyError, TypeError, ValueError):
        return ()


def detect_sibling_album_groups(album_dirs):
    """Group album dirs whose names strip to the same bare title.
    Returns [(bare_title, [dirs])] for groups with 2+ members. A folder named
    as a separate edition is never one."""
    groups = {}
    for d in album_dirs:
        if separate_edition_folder(d.name):
            continue
        bare = normalize(tags.strip_album_decorations(d.name))
        if not bare:
            continue
        groups.setdefault(bare, []).append(d)
    return [(k, v) for k, v in groups.items() if len(v) >= 2]


def pick_canonical_sibling(dirs):
    """Most audio files wins; tiebreak on longest name (more decoration =
    usually the fuller edition, such as Deluxe or Expanded)."""
    def score(d):
        try:
            n = sum(1 for f in d.rglob("*")
                    if f.is_file() and f.suffix.lower() in cfg.AUDIO_EXTS)
        except OSError:
            n = 0
        return (n, len(d.name))
    return max(dirs, key=score)


def _offer_expanded_edition(album, album_dir, existing, extras, token, args):
    """Look for an expanded Qobuz edition that also covers the on-disk extras
    and let the user pick one. Returns (edition, edition_extras, edition_qual)
    for the chosen album, or (None, [], None) when nothing was offered or the
    user declined."""
    cands = find_expanded_edition(album, album_dir, existing, token, args)
    exp, exp_extras = prompt_edition_pick(
        album, len(extras), cands, existing, args, label_prefix="  ")
    if exp is None:
        return None, [], None
    return exp, exp_extras, compare_album_quality(existing, exp)


class UpgradedThenInterrupted(KeyboardInterrupt):
    """Ctrl-C landed while the old copy of an upgraded album was removed."""


def _dispose_replaced_original(backup, **kwargs):
    try:
        return dispose_backup(backup, **kwargs)
    except KeyboardInterrupt:
        raise UpgradedThenInterrupted from None


def _pin_or_warn(backup, note=None):
    if not pin_unverified_upgrade_backup(backup, note):
        warn_pin_failed(backup)


class _AlbumRun:
    """One album from detection to the result process_album returns."""

    def __init__(self, album, args, *, allow_force, label, already_confirmed,
                 upgrade_only, token, quality, treat_as_new, exact_edition,
                 expected_album_receipt, expected_gap_fill_receipts,
                 cover_backup, rebuild_dir, repair_damaged):
        self.album = album
        self.args = args
        self.allow_force = allow_force
        self.label = label
        self.already_confirmed = already_confirmed
        self.upgrade_only = upgrade_only
        self.token = token
        self.quality = quality
        self.treat_as_new = treat_as_new
        self.exact_edition = exact_edition
        self.expected_album_receipt = expected_album_receipt
        self.expected_gap_fill_receipts = expected_gap_fill_receipts
        self.cover_backup = cover_backup
        self.rebuild_dir = rebuild_dir
        self.repair_damaged = repair_damaged
        self.repair_kept = []
        self.repair_token = None

    def run(self):
        for step in (self._locate, self._plan_upgrade, self._check_premise,
                     self._stop_if_complete, self._confirm,
                     self._back_up_original):
            stopped = step()
            if stopped is not None:
                return stopped
        self._reset_counts()
        try:
            for step in (self._download, self._stop_incomplete_replacement,
                         self._check_quality, self._prepare_staged,
                         self._import):
                stopped = step()
                if stopped is not None:
                    return stopped
        finally:
            self._settle_upgrade_backup()
            self._settle_gap_fill_backup()
            # The return value was built before this recovery block. Update the
            # same result if an automatic restore failed.
            if self.early_result is not None:
                self.early_result.update(
                    upgrade_unverified=self.upgrade_unverified,
                    catalogue_unverified=self.catalogue_unverified,
                    recovery_unverified=self.recovery_unverified,
                )
            # A cancelled Upgrade says it put the album back only once it has.
            if self.cancelled is not None and self.upgrade_unverified:
                self.cancelled["upgrade_unverified"] = True

        if self.disk_full is not None:
            raise self.disk_full
        if self.imported:
            clear_attempts_after_import(self.download_result)

        self._consolidate()
        self._locate_imported()
        self._count_shortfall()
        self._print_result()
        return self._outcome()

    def _locate(self):
        self.use_force = bool(self.args.force) and self.allow_force
        self.force_tracks = False
        label_prefix = (self.label + " ") if self.label else ""

        if not is_lossless_album(self.album):
            log.info(fmt(C.RED,
                f"\n  ✗  {label_prefix}Lossy-only on Qobuz "
                f"({self.album.get('title') or '?'}, {album_quality_label(self.album)})."))
            log.info(fmt(C.GRAY, "     Skipping: no lossless version on Qobuz."))
            return {"result": "lossy_only"}

        self.qobuz_tracks = (self.album.get("tracks") or {}).get("items") or []
        if not self.qobuz_tracks:
            log.info(fmt(C.RED, f"  ✗  {label_prefix}Album has no tracks in API response. Skipping."))
            return {"result": "no_tracks"}

        # --force cleanup is deferred until AFTER the download confirm
        # below: deleting here would mean a 'no' at the download prompt
        # leaves the user with their folder already wiped.
        if self.rebuild_dir is not None:
            self.existing, self.album_dir = [], None
            self.missing, self.present = self.qobuz_tracks, []
        elif self.exact_edition:
            self.existing, self.album_dir, self.missing, self.present = catalog.find_edition_tracks(self.album)
            other = catalog.other_version_dir(self.album)
            self.mixed_folder = self.album_dir is not None and len(self.existing) > len(self.present)
            if self.use_force and self.mixed_folder:
                self.force_tracks = True
                self.use_force = self.treat_as_new = False
                self.missing = self.qobuz_tracks
            else:
                # A song the folder holds as another master is not fetched into
                # it again; when that covers every gap, this edition is a
                # separate copy, as when none of its tracks are on disk.
                held = ([] if self.album_dir is None or self.treat_as_new or self.use_force else
                        catalog.held_by_other_versions(self.album, self.existing, self.album_dir, self.missing))
                if held and len(held) == len(self.missing):
                    self.treat_as_new = True
                elif held:
                    self.missing = [t for t in self.missing if not any(t is h for h in held)]
                self.treat_as_new = bool((self.missing or self.use_force) and (
                    self.treat_as_new or (other and self.album_dir is None)))
            if self.treat_as_new:
                self.existing, self.album_dir = [], None
                self.missing, self.present = self.qobuz_tracks, []
                self.use_force = False
            elif self.use_force:
                self.existing = []
                self.missing, self.present = self.qobuz_tracks, []
        elif self.use_force:
            _, self.album_dir = catalog.find_album_folder(self.album)
            self.existing = []
            self.missing, self.present = self.qobuz_tracks, []
        elif self.treat_as_new:
            # Deliberately fetching a different edition of an album the user
            # already owns, such as a remaster or new mix kept alongside the original.
            self.album_dir = None
            self.existing = []
            self.missing, self.present = self.qobuz_tracks, []
        else:
            self.existing, self.album_dir = catalog.find_album_folder(self.album)
            vlog(f"hybrid detection: {plural(len(self.existing), 'existing track')} total")
            self.missing, self.present = compute_missing(self.qobuz_tracks, self.existing)

    def _plan_upgrade(self):
        # Runs BEFORE the "already complete" early-exit, because an album that is
        # "complete" track-wise might still be lower quality than Qobuz and
        # warrant an upgrade-replace.
        self.auto_upgrade_active = False    # if True, do backup-then-wipe-then-redownload
        self.upgrade_backup_path = None     # populated after backup_album_dir succeeds
        self.replaced_beets_entries = None
        self.import_folder = self.rebuild_dir
        self.upgrade_existing_label = None  # before-quality label, set in the auto-upgrade branch

        # Quality-upgrade replace path.
        auto_upgrade = getattr(self.args, "auto_upgrade", cfg.AUTO_UPGRADE_ENABLED)
        if (auto_upgrade
                and self.existing
                and not self.use_force
                and not (self.exact_edition and self.mixed_folder)
                and not getattr(self.args, "no_upgrade", False)
                and self.album_dir is not None):
            qual = compare_album_quality(self.existing, self.album)
            cls = qual["classification"]
            if self.exact_edition:
                matched = catalog.edition_files(self.album, self.existing, self.album_dir)
                extras = [track for track in self.existing if track not in matched]
            else:
                extras = find_extras_in_existing(self.qobuz_tracks, self.existing)
            qbits, qrate = qual["qobuz_quality"]
            target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                            if qbits and qrate else "Qobuz quality")

            if cls == "all_higher":
                # Existing strictly better than Qobuz everywhere. Never replace.
                log.info(fmt(C.GREEN,
                    f"\n  ✓  Already higher quality than Qobuz "
                    f"({plural(qual['n_above'], 'track')} above target)."))
                if not self.missing:
                    return {"result": "skipped_already_higher_quality",
                            "n_total": len(self.qobuz_tracks)}
                # Has missing tracks. Will fall through to gap-fill prompt with
                # explicit warning that this creates mixed quality.
                log.info(fmt(C.YELLOW,
                    f"     ⚠  {plural(len(self.missing), 'track')} missing; filling "
                    f"{'it' if len(self.missing) == 1 else 'them'} at {target_label} "
                    "would mix quality."))

            elif cls in ("all_lower", "mixed_below"):
                # Qobuz is higher quality, but only replace when it's verifiably
                # safe.
                if qual.get("n_unknown"):
                    log.info(fmt(C.YELLOW,
                        f"\n  ⚠  Can't auto-upgrade: {plural(qual['n_unknown'], 'track')} "
                        f"{'has' if qual['n_unknown'] == 1 else 'have'} unreadable "
                        "quality and would be replaced unverified; "
                        f"filling gaps only."))
                    # Fall through to plain gap-fill; do NOT set auto_upgrade_active.
                elif tags.upgrade_unprovable(self.existing, self.qobuz_tracks):
                    log.info(fmt(C.YELLOW,
                        "\n  ⚠  Not upgrading: ISRC tags are missing from these "
                        "files or from Qobuz's copy, so the new copy can't be "
                        "matched to them track by track."))
                elif extras and self.missing and self.already_confirmed:
                    # User already okayed a gap-fill.
                    _exp, _exp_extras, _exp_qual = _offer_expanded_edition(
                        self.album, self.album_dir, self.existing, extras, self.token, self.args)
                    if _exp is not None and _exp_qual["classification"] in ("all_lower", "mixed_below"):
                        log.info(fmt(C.MAGENTA,
                            f"\n  ↑  Switching to {_exp.get('title') or '?'!r}; "
                            f"covers your {len(self.existing)} tracks "
                            f"with {len(_exp_extras)} local-only at {album_quality_label(_exp)}"))
                        self.album = _exp
                        self.qobuz_tracks = (_exp.get("tracks") or {}).get("items") or []
                        extras = _exp_extras
                        qual = _exp_qual
                        qbits, qrate = _exp_qual["qobuz_quality"]
                        target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                                        if qbits and qrate else "Qobuz quality")
                        self.missing, self.present = compute_missing(self.qobuz_tracks, self.existing)
                        if extras:
                            log.info(fmt(C.YELLOW,
                                f"\n  ⚠  Can't auto-upgrade ({plural(len(extras), 'bonus track')} here); "
                                f"filling {len(self.missing)} at {target_label} (will mix quality)."))
                        else:
                            log.info(fmt(C.MAGENTA + C.BOLD,
                                f"\n  ↑ Auto-upgrade via expanded edition → {target_label}"))
                            log.info(fmt(C.YELLOW,
                                "  ⚠  This was queued as a gap-fill but will now back up and"
                                " replace the entire folder."))
                            self.auto_upgrade_active = True
                            self.existing = []
                            self.missing, self.present = self.qobuz_tracks, []
                    else:
                        log.info(fmt(C.YELLOW,
                            f"\n  ⚠  Can't auto-upgrade ({plural(len(extras), 'bonus track')} here); "
                            f"filling {len(self.missing)} at {target_label} (will mix quality)."))
                elif extras:
                    # Before giving up, try to find an expanded edition that
                    # covers the local tracks.
                    _exp, _exp_extras, _exp_qual = _offer_expanded_edition(
                        self.album, self.album_dir, self.existing, extras, self.token, self.args)
                    if (_exp is not None
                            and _exp_qual["classification"] in ("all_lower", "mixed_below")
                            and not _exp_extras):
                        log.info(fmt(C.MAGENTA,
                            f"\n  ↑  Switching to {_exp.get('title') or '?'!r}; "
                            f"covers your {len(self.existing)} tracks at {album_quality_label(_exp)}"))
                        self.album = _exp
                        self.qobuz_tracks = (_exp.get("tracks") or {}).get("items") or []
                        extras = _exp_extras
                        qual = _exp_qual
                        qbits, qrate = _exp_qual["qobuz_quality"]
                        target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                                        if qbits and qrate else "Qobuz quality")
                        self.missing, self.present = compute_missing(self.qobuz_tracks, self.existing)
                        log.info(fmt(C.MAGENTA + C.BOLD,
                            f"\n  ↑ Auto-upgrade via expanded edition → {target_label}"))
                        log.info(fmt(C.YELLOW,
                            "  ⚠  This was queued as a gap-fill but will now back up and"
                            " replace the entire folder. Confirm or cancel at the prompt below."))
                        self.auto_upgrade_active = True
                        self.existing = []
                        self.missing, self.present = self.qobuz_tracks, []
                    else:
                        log.info(fmt(C.YELLOW,
                            f"\n  ⚠  Upgrade to {target_label} blocked: "
                            f"{plural(len(extras), 'on-disk track')} Qobuz doesn't carry:"))
                        for _e in extras[:5]:
                            log.info(fmt(C.GRAY,
                                f"       • {truncate(_e.get('title') or '?', 60)}"))
                        if len(extras) > 5:
                            log.info(fmt(C.GRAY,
                                f"       ... and {len(extras) - 5} more"))
                        log.info(fmt(C.GRAY,
                            "     If these look like normal album tracks, your tags"
                            " differ from Qobuz's. Skipping."))
                        return {"result": "skipped_has_extras",
                                "n_total": len(self.qobuz_tracks),
                                "n_extras": len(extras)}
                elif not catalog.same_edition(self.album, self.existing, self.album_dir, self.token):
                    log.info(fmt(C.YELLOW,
                        f"\n  ⚠  Not upgrading: the {target_label} copy on Qobuz is "
                        f"another version ({catalog._release_title(self.album)})."))
                else:
                    self._begin_auto_upgrade(target_label)

            elif cls == "mixed_above":
                # Some tracks above, rest equal. Treat like all_higher.
                log.info(fmt(C.GREEN,
                    "\n  ✓  No upgrade available (some tracks already above target)."))
                if not self.missing:
                    return {"result": "skipped_already_higher_quality",
                            "n_total": len(self.qobuz_tracks)}
                # Has missing tracks.
                log.info(fmt(C.YELLOW,
                    f"     ⚠  {plural(len(self.missing), 'track')} missing; filling "
                    f"{'it' if len(self.missing) == 1 else 'them'} at {target_label} "
                    "would mix quality."))

            elif cls == "mixed_both":
                # Some above, some below: ambiguous. Don't auto-replace.
                log.info(fmt(C.YELLOW,
                    f"\n  ⚠  Mixed quality: {qual['n_above']} above and "
                    f"{qual['n_below']} below Qobuz target. Not auto-upgrading."))
                log.info(fmt(C.GRAY,
                    "     Falling back to gap-fill if applicable. "
                    "Use --force on this album manually if you want to replace."))

            elif cls == "incomparable":
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Not auto-upgrading: at least one local track would "
                    "gain one resolution dimension but lose the other."))
                log.info(fmt(C.GRAY,
                    "     Keeping the existing files and filling gaps only."))

            elif cls == "unknown":
                log.info(fmt(C.GRAY,
                    "  · Couldn't read quality from existing tracks; using gap-fill."))

            # cls == "all_equal": no message.

    def _begin_auto_upgrade(self, target_label):
        # No extras, so auto-upgrade is safe.
        _qcounts = {}
        for _t in self.existing:
            _bb, _rr = existing_track_quality(_t)
            if _bb and _rr:
                _qcounts[(_bb, _rr)] = _qcounts.get((_bb, _rr), 0) + 1
        if _qcounts:
            _eb, _er = max(_qcounts, key=_qcounts.get)
            existing_label = f"{_eb}-bit/{_er/1000:.1f}kHz"
            if len(_qcounts) > 1:
                existing_label = "mostly " + existing_label
        else:
            existing_label = "lower-quality"
        existing_label = lossy_label(self.existing) or existing_label
        self.upgrade_existing_label = existing_label
        _fill = f" +{len(self.missing)} gap-fill" if self.missing else ""
        if not self.already_confirmed:
            log.info(fmt(C.MAGENTA + C.BOLD,
                f"\n  ↑ Auto-upgrade: {existing_label} → {target_label}{_fill}"))
        self.auto_upgrade_active = True
        # Override detection results so downstream code does a full
        # download.
        if self.upgrade_only:
            # map each local track to its single best Qobuz match so
            # duplicate matches (one local file matching both "Foo"
            # and "Foo (Edit)" via title-stripping) don't cause re-
            # ripping the same track twice, which would collide at
            # the destination filename and produce a 0-byte fallback
            # that cleanup_lossy then deletes, losing the original.
            _claimed_qids = set()
            _upgrade_targets = []
            # Mirror compute_missing's disc scoping: only require the
            # disc to match when BOTH sides are genuinely multi-disc.
            _disc_scoped = _disc_scoped_match(self.present, self.existing)
            for _et in self.existing:
                _eisrc = (_et.get("isrc") or "").replace("-", "").upper().strip()
                _enorm = _et.get("normalized") or ""
                _edisc = _et.get("discnumber", 1) or 1
                _estripped = normalize(strip_edition_suffix(
                    _et.get("title") or ""))
                _best_qt, _best_rank = None, 99
                for _qt in self.present:
                    if _qt.get("id") in _claimed_qids:
                        continue
                    # Qobuz tracks carry isrc/version but never an mbid,
                    # so there's no MusicBrainz tier here (the on-disk
                    # mb_trackid has nothing to match against): ISRC, then
                    # normalized title, then edition-stripped title.
                    _qisrc = (_qt.get("isrc") or "").replace("-", "").upper().strip()
                    _qnorm = normalize(_qt.get("title") or "")
                    _qstripped = normalize(strip_edition_suffix(
                        _qt.get("title") or ""))
                    _qdisc = _qt.get("media_number", 1) or 1
                    if _eisrc and _qisrc and _eisrc == _qisrc:
                        _r = 0
                    elif ((not _disc_scoped or _qdisc == _edisc)
                            and _enorm and _enorm == _qnorm):
                        _r = 1
                    elif ((not _disc_scoped or _qdisc == _edisc)
                            and _estripped and _estripped == _qstripped):
                        _r = 2
                    else:
                        continue
                    if _r < _best_rank:
                        _best_qt, _best_rank = _qt, _r
                        if _r == 0:
                            break
                if _best_qt is not None:
                    _claimed_qids.add(_best_qt.get("id"))
                    _upgrade_targets.append(_best_qt)
            self.existing = []
            self.missing, self.present = _upgrade_targets, []
        else:
            self.existing = []
            self.missing, self.present = self.qobuz_tracks, []

    def _check_premise(self):
        # If caller asked for upgrade-only but the auto-upgrade branch above
        # didn't fire (quality classification didn't match upgrade criteria,
        # extras blocked it, etc.), bail rather than silently fall through to
        # gap-fill because the user explicitly asked not to fill missing tracks.
        if self.upgrade_only and not self.auto_upgrade_active:
            if not self.present:
                # A different fact than "no upgrade applies": the album isn't in
                # the library at all any more (moved, renamed, deleted since the
                # scan that produced this candidate).
                log.info(fmt(C.YELLOW,
                    "  ⚠  None of this album's tracks are in your library any "
                    "more; skipping."))
                return {"result": "upgrade_no_local_tracks",
                        "n_total": len(self.qobuz_tracks)}
            log.info(fmt(C.YELLOW,
                "  ⚠  Upgrade-only requested but no upgrade applies here; skipping."))
            return {"result": "upgrade_only_no_op", "n_total": len(self.qobuz_tracks)}

        self.reviewed_album_absent = (
            self.album_dir is None and not self.treat_as_new and self.rebuild_dir is None)
        self.source_premise = None
        if self.missing and not self.args.dry_run and self.album_dir is not None:
            premise_kind = (
                "upgrade"
                if self.auto_upgrade_active or self.upgrade_only or self.use_force
                else "gap-fill"
            )
            if self.expected_album_receipt is None:
                self.source_premise = candidate_premise.capture(
                    premise_kind,
                    self.album_dir,
                )
            else:
                self.source_premise = candidate_premise.canonical_premise({
                    "version": candidate_premise.PREMISE_VERSION,
                    "kind": premise_kind,
                    "path": os.path.abspath(os.fspath(self.album_dir)),
                    "receipt": self.expected_album_receipt,
                })
            if self.source_premise is None:
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  The local album could not be checked. Refresh or "
                    "rerun the command.",
                ))
                return {"result": "stale_candidate"}
            self.expected_album_receipt = self.source_premise["receipt"]
            if self.expected_gap_fill_receipts is None:
                self.expected_gap_fill_receipts = (
                    candidate_premise.gap_fill_receipts(self.source_premise)
                )

    def _stop_if_complete(self):
        if not self.already_confirmed:
            print_album_summary(self.album, self.missing, self.present, self.album_dir, self.use_force,
                                auto_upgrade=self.auto_upgrade_active,
                                existing_quality_label=self.upgrade_existing_label)

        # A gap of only tracks Qobuz does not stream is nothing to download.
        unstreamed = [] if not self.present or self.use_force or self.force_tracks or self.auto_upgrade_active else [
            track for track in self.missing if catalog.unstreamable(track)]
        if not self.missing or len(unstreamed) == len(self.missing):
            if self.missing:
                log.info(fmt(C.GREEN,
                    f"\n  ✓  Every track Qobuz streams is here; "
                    f"{plural(len(self.missing), 'track')} not available on Qobuz.\n"))
            else:
                log.info(fmt(C.GREEN, "\n  ✓  Already complete. Nothing to download.\n"))
            self.consolidation_interrupted = self.consolidation_cancelled = False
            self.n_consolidated = self.consolidation_failed = 0
            if self.args.consolidate:
                try:
                    self.n_consolidated, self.consolidation_failed = consolidate_albums(self.album, self.args)
                except ConsolidationCancelled as cancel:
                    self.n_consolidated = cancel.moved
                    self.consolidation_cancelled = True
                except KeyboardInterrupt:
                    self.consolidation_interrupted = True
                    log.info(fmt(C.GRAY, "\n  Consolidation interrupted."))
            return {
                "result": "already_complete",
                "n_total": len(self.qobuz_tracks),
                "unavailable_tracks": [track.get("title") or "?" for track in self.missing],
                "consolidation_interrupted": self.consolidation_interrupted,
                "consolidation_cancelled": self.consolidation_cancelled,
                "consolidated_tracks_removed": self.n_consolidated,
                "consolidation_failed": self.consolidation_failed,
            }

    def _confirm(self):
        # Tracks for an album already on disk land in its folder, and an upgrade
        # or --force moves that folder aside first; a new album is made in its
        # artist's folder. A folder the app can't write to is refused here,
        # before anything is downloaded or asked.
        if self.album_dir is not None and self.album_dir.is_dir():
            folder = unwritable_folder(
                self.album_dir, parent=self.auto_upgrade_active or self.use_force)
        else:
            folder = unwritable_new_album_folder(self.album)
        if folder:
            log.info(fmt(C.RED,
                f"  ✗  The app can't write to {folder}. Nothing was downloaded."))
            return {"result": "folder_read_only", "folder_denied": str(folder)}

        # A dry run has already printed the plan above; stop before the download
        # confirm so we don't ask "proceed with downloading?" for a run that never
        # downloads.
        if self.args.dry_run:
            log.info(fmt(C.YELLOW, "\n  --dry-run: stopping here, nothing downloaded.\n"))
            return {"result": "dry_run", "n_missing": len(self.missing)}

        # Default NO: the missing-tracks list above is the user's chance to
        # decide; defaulting yes would mean every enter-press starts a download
        # they may not want. Press y to proceed.
        if not self.already_confirmed:
            if all(catalog.unstreamable(track) for track in self.missing):
                log.info(fmt(C.YELLOW,
                    f"\n  ⚠  Nothing can be downloaded: "
                    f"{plural(len(self.missing), 'track')} not available on Qobuz.\n"))
                return {"result": "no_tracks", "n_missing": len(self.missing)}
            proceed = confirm(download_question(self.missing),
                              default_yes=False, auto_yes=self.args.yes, on_eof=None)
            if proceed is None:
                return {"result": "no_answer", "n_missing": len(self.missing)}
            if not proceed:
                log.info(fmt(C.GRAY, "  Skipped."))
                return {"result": "user_skipped", "n_missing": len(self.missing)}

        expected_generation = getattr(self.token, "credential_generation", "")
        if expected_generation:
            self.token = api_client.authorize_bound_download(self.token)

        if self.source_premise is not None:
            current_premise = candidate_premise.capture(
                self.source_premise["kind"],
                self.source_premise["path"],
            )
            if current_premise != self.source_premise:
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  The local files changed after this download was "
                    "approved. Refresh or rerun the command.",
                ))
                return {"result": "stale_candidate"}
        elif self.reviewed_album_absent:
            clear_scan_caches()
            if catalog.other_version_dir(self.album) is not None:
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  This album appeared locally after the download was "
                    "approved. Refresh or rerun the command.",
                ))
                return {"result": "stale_candidate"}

        staging_preflight(self.args)

        if self.force_tracks and not confirm(
                f"\n  Back up and replace this edition's "
                f"{plural(len(self.present), 'existing track')}, keeping the other "
                "files in this folder?",
                default_yes=True, auto_yes=False):
            return {"result": "cancelled"}

    def _back_up_original(self):
        if (
            (self.auto_upgrade_active or self.use_force)
            and self.album_dir is not None
            and self.album_dir.exists()
        ):
            self.replaced_beets_entries = capture_beets_album_entries(self.album_dir)
            if self.replaced_beets_entries is None:
                log.info(fmt(
                    C.RED,
                    "  ✗  Couldn't identify this album's Beets entries; "
                    "refusing to replace its folder.",
                ))
                return {"result": "replacement_aborted_catalogue_failed"}

        # Same-filesystem move, so this is fast (rename, not copy). The backup
        # is restored if anything fails before beets import succeeds.
        if self.auto_upgrade_active and self.album_dir and self.album_dir.exists():
            self.upgrade_backup_path = (
                backup_album_dir(
                    self.album_dir,
                    expected_receipt=self.expected_album_receipt,
                )
                if self.expected_album_receipt is not None
                else backup_album_dir(self.album_dir)
            )
            if (
                self.upgrade_backup_path is not None
                and not self.upgrade_backup_path.complete
            ):
                _recover_incomplete_upgrade_backup(
                    self.upgrade_backup_path,
                    self.album_dir,
                    operation="upgrade backup",
                )
                return {"result": "upgrade_aborted_backup_failed"}
            if (
                self.upgrade_backup_path is None
            ):
                log.info(fmt(C.RED,
                    "  ✗  Could not back up the existing folder; refusing to "
                    "wipe without a backup. Skipping this album."))
                return {"result": "upgrade_aborted_backup_failed"}
            log.info(fmt(C.GRAY,
                "  ⤷  Backed up existing folder (auto-restore on failure)"))
            # The download goes into the folder of the album it upgrades, never
            # wherever its tags would file it beside another album.
            self.import_folder = self.album_dir

        if self.use_force:
            force_outcome = force_cleanup_preflight(
                self.album_dir,
                self.args,
                expected_album_receipt=self.expected_album_receipt,
            )
            if force_outcome is None:
                # A symlink has no safe --force path; skip without asking again.
                return {"result": "cancelled"}
            if force_outcome is False:
                if not confirm("\n  Proceed with --force despite expected '<n>.1.flac' collisions?",
                               default_yes=False, auto_yes=False):
                    log.info(fmt(C.GRAY, "  Skipping this album."))
                    return {"result": "cancelled"}
            elif (
                getattr(force_outcome, "complete", False) is True
                and self.upgrade_backup_path is None
            ):
                # The moved-aside folder is restored on failure / cleared on success
                # by the same finally block that handles the auto-upgrade backup.
                self.upgrade_backup_path = force_outcome

    def _reset_counts(self):
        # Pre-init so the backup-resolution finally block has sane defaults if a
        # rip raises AuthLost / OSError before the result is read back.
        self.n_ok = self.n_fail = self.n_lossy = 0
        self.failed_tracks, self.lossy_tracks, self.broken_tracks = [], [], []
        self.imported = False
        self.disk_full = None
        self.upgrade_unverified = False
        self.catalogue_unverified = False
        self.companions_kept = []
        self.upgrade_put_back = False
        self.upgrade_set_aside = None
        self.elapsed = 0.0
        self.download_phase_completed = False
        self.resampled_n = 0
        self.downsample_outcome = _pre_import_outcome_fields(None)
        self.post_import_signatures = []
        self.staged_dirs_for_import = []
        self.download_result = {}
        self.quality_verdict = None
        self._gap_fill_not_located = False  # gap-fill succeeded on paper but album not found on disk
        self.recovery_unverified = False
        self.early_result = None
        self.cancelled = None
        self.post_dir = None

    def _imported_dir(self):
        expected = self.import_folder or self.album_dir
        if (self.exact_edition or self.import_folder is not None) and expected is not None:
            found = find_album_dir_by_track_signatures(
                self.post_import_signatures, expected_dir=expected)
            if (found is not None or self.upgrade_backup_path is None
                    or self.import_folder is not None):
                return found
        return find_album_dir_by_track_signatures(self.post_import_signatures)

    def _download(self):
        self.snapshot = snapshot_staging()
        vlog(f"staging snapshot: {len(self.snapshot)} files before download")

        try:
            run_album_download(
                album=self.album, missing=self.missing, present=self.present,
                existing=self.existing, album_dir=self.album_dir, snapshot=self.snapshot,
                quality=self.quality, upgrade_only=self.upgrade_only,
                exact_edition=self.exact_edition,
                result=self.download_result,
                expected_gap_fill_receipts=self.expected_gap_fill_receipts)
        except BaseException:
            retain_download_staging(self.download_result)
            raise
        self.n_ok = self.download_result["n_ok"]
        self.n_fail = self.download_result["n_fail"]
        self.n_lossy = self.download_result["n_lossy"]
        self.failed_tracks = self.download_result["failed_tracks"]
        self.lossy_tracks = self.download_result["lossy_tracks"]
        self.broken_tracks = self.download_result.get("broken_tracks", [])
        self.elapsed = self.download_result["elapsed"]

        if is_cancel_requested():
            return self._cancelled_result()

    # Cancelled mid-rip: the user hit stop, so the partial set must not be
    # imported. It is kept for the next try only when it holds audio.
    def _cancelled_result(self):
        if download_holds_audio(self.download_result):
            kept = retain_download_staging(self.download_result)
        else:
            kept = False
            discard_download_staging(self.download_result)
        log.info(fmt(C.YELLOW,
            "\n  Stopped; nothing was imported."
            + (" The part already downloaded is kept in staging."
               if kept else "")))
        self.cancelled = {
            "result": "cancelled",
            "imported": False,
            "n_ok": self.n_ok,
            "n_lossy": self.n_lossy,
            "n_fail": self.n_fail,
        }
        return self.cancelled

    def _stop_incomplete_replacement(self):
        # A wipe-replace (auto-upgrade or --force moved the original aside)
        # that came back partial gets rolled back to the original by the
        # finally below, so importing the partial first would only strand its
        # rows in beets once the backup is restored over it.
        replacement = (self.auto_upgrade_active or self.use_force or self.force_tracks
                       or self.rebuild_dir is not None)
        if (replacement and self.n_ok > 0 and (self.n_fail > 0 or self.n_lossy > 0)
                and not self.args.no_import):
            return self._keep_original_over_incomplete("Re-download")

    def _keep_original_over_incomplete(self, attempt):
        retained = retain_download_staging(
            self.download_result, label="incomplete-replacement")
        disposition = (
            "retaining it in staging recovery"
            if retained else
            "leaving its isolated staging run untouched for review"
        )
        log.info(fmt(C.YELLOW,
            f"\n  {attempt} came back incomplete; {disposition} and "
            "keeping your original."))
        self.early_result = {
            "result": "partial",
            "imported": False,
            "n_ok": self.n_ok,
            "n_fail": self.n_fail,
            "n_lossy": self.n_lossy,
            "n_broken": self.download_result.get("n_broken", 0),
            "n_unavailable": self.download_result.get("n_unavailable", 0),
            "upgrade_put_back": True,
        }
        return self.early_result

    def _check_quality(self):
        if self.n_ok > 0 and not self.args.no_import and not self.use_force:
            self.staged_dirs_for_import = validated_staged_album_dirs(
                self.download_result)
            staged_dirs = self.staged_dirs_for_import
            if staged_dirs:
                effective_tier = (
                    self.quality if self.quality is not None else cfg.STREAMRIP_QUALITY
                )

                def _redownload_at_max():
                    original_result = dict(self.download_result)
                    retry_result = {}

                    def _run_retry():
                        run_album_download(
                            album=self.album, missing=self.missing, present=self.present,
                            existing=self.existing, album_dir=self.album_dir,
                            snapshot=self.snapshot, quality=4,
                            upgrade_only=self.upgrade_only, result=retry_result,
                            exact_edition=self.exact_edition,
                            expected_gap_fill_receipts=(
                                self.expected_gap_fill_receipts
                            ))

                    try:
                        fresh_dirs, retry_kept = redownload_with_staged_fallback(
                            staged_dirs,
                            run_retry=_run_retry,
                            collect_staged_dirs=lambda: (
                                validated_staged_album_dirs(retry_result)
                                if retry_result.get("n_ok", 0) > 0 else []),
                            discard_retry=lambda: discard_retry_staging(
                                retry_result),
                            retry_preserves_original=lambda: (
                                retry_preserves_track_coverage(
                                    original_result, retry_result)))
                    except Exception:
                        self.download_result.clear()
                        self.download_result.update(original_result)
                        raise
                    if retry_kept:
                        retire_empty_download_staging(original_result)
                        self.download_result.clear()
                        self.download_result.update(retry_result)
                    else:
                        retire_empty_download_staging(retry_result)
                        self.download_result.clear()
                        self.download_result.update(original_result)
                    return fresh_dirs, self.download_result.get(
                        "_cd_quality_paths", ())

                self.quality_verdict = verify_and_recover(
                    self.album, staged_dirs,
                    redownload_at_max=_redownload_at_max,
                    effective_tier=effective_tier,
                    allow_retry=self.download_result.get("gap_fill_backup_path") is None,
                    exempt=self.download_result.get("_cd_quality_paths", ()))
                if self.quality_verdict["retried"]:
                    self.staged_dirs_for_import = (
                        self.quality_verdict.get("staged_dirs") or self.staged_dirs_for_import
                    )
                    self.n_ok = self.download_result.get("n_ok", 0)
                    self.n_fail = self.download_result.get("n_fail", 0)
                    self.n_lossy = self.download_result.get("n_lossy", 0)
                    self.failed_tracks = self.download_result.get(
                        "failed_tracks", self.failed_tracks)
                    self.lossy_tracks = self.download_result.get(
                        "lossy_tracks", self.lossy_tracks)
                    self.broken_tracks = self.download_result.get(
                        "broken_tracks", self.broken_tracks)
                    self.elapsed = self.download_result.get("elapsed", self.elapsed)
                    if (self.auto_upgrade_active and self.n_ok > 0
                            and (self.n_fail > 0 or self.n_lossy > 0)):
                        return self._keep_original_over_incomplete(
                            "Recovery re-download")
                if self.quality_verdict["recovered"]:
                    log.info(fmt(C.CYAN,
                        "  ↻ recovered: re-fetched at the highest source after "
                        "a short first rip."))
                elif not self.quality_verdict["under"]:
                    met = quality_met_line(
                        self.n_ok, self.download_result.get("cd_quality_tracks"))
                    if met:
                        log.info(fmt(C.GRAY, f"  {met}"))
                else:
                    log.info(fmt(C.YELLOW,
                        f"  {quality_below_line(self.quality_verdict)}"))
                if is_cancel_requested():
                    return self._cancelled_result()

    def _prepare_staged(self):
        # Downsampling and lyric_fetch run on staging BEFORE beets imports.
        if self.n_ok > 0 and not self.args.no_import:
            if not self.staged_dirs_for_import:
                self.staged_dirs_for_import = validated_staged_album_dirs(
                    self.download_result)
            if self.import_folder is not None:
                # An upgrade or a Repair keeps the album's own name, version
                # and all.
                fill_tags = fill_album_tags(
                    self.cover_backup if self.upgrade_backup_path is None
                    else self.upgrade_backup_path)
            else:
                fill_tags = (
                    fill_album_tags(self.album_dir)
                    if self.album_dir is not None and not self.treat_as_new
                    and not self.download_result.get("download_full_album") else None)
            if fill_tags:
                _prepare_staging_tags(self.staged_dirs_for_import, album_tags=fill_tags)
            elif self.exact_edition or self.treat_as_new or (
                    self.import_folder is not None
                    and catalog.edition_album_title(self.album)):
                _prepare_staging_tags(self.staged_dirs_for_import,
                                      album_title=catalog.edition_album_title(
                                          self.album, separate=self.treat_as_new))
            prepared = _pre_import_staging_hooks(self.args, self.staged_dirs_for_import)
            self.resampled_n = prepared.resampled
            self.downsample_outcome = _pre_import_outcome_fields(prepared)
            if self.auto_upgrade_active and self.upgrade_backup_path is not None:
                if (not processing_attention(self.downsample_outcome)
                        and _upgrade_same_quality(self.upgrade_backup_path, self.staged_dirs_for_import)):
                    self.early_result = {"result": "upgrade_same_quality", "imported": False, "n_ok": 0}
                    return self.early_result
                _carry_staged_upgrade_tags(self.upgrade_backup_path, self.staged_dirs_for_import)
            elif self.rebuild_dir is not None and self.cover_backup is not None:
                _carry_staged_upgrade_tags(self.cover_backup, self.staged_dirs_for_import,
                                           damaged=self.repair_damaged)
                refusal = self._prepare_repair_import()
                if refusal:
                    log.warning(f"  {refusal}")
                    retain_download_staging(self.download_result, label="unverified-repair")
                    return {"result": "unverified", "imported": False, "n_ok": 0,
                            "refusal": refusal}
            self.post_import_signatures = track_signatures_for_album_dirs(
                self.staged_dirs_for_import)

    def _prepare_repair_import(self):
        try:
            kept = _repair_keep_originals(
                self.cover_backup, self.staged_dirs_for_import, self.repair_damaged)
        except (OSError, subprocess.TimeoutExpired):
            kept = None
        if kept is None:
            return "The download could not be matched to the original album."
        if kept:
            self.repair_token = beets.prepare_repair_import(
                self.cover_backup, self.staged_dirs_for_import)
            if not self.repair_token:
                return "The beets entries for this repair could not be recorded."
            if not put_back_healthy_tracks(self.cover_backup, self.rebuild_dir, kept):
                return "The healthy originals could not be returned to their original paths."
        self.repair_kept = kept

    def _import(self):
        self.imported = False
        if self.args.no_import:
            # Without the import a multi-disc cover would stay in the album
            # root, where the manual import leaves it behind.
            try:
                for _staged in (validated_staged_album_dirs(self.download_result)
                                if self.n_ok > 0 else []):
                    relocate_disc_album_artwork(_staged)
            except Exception as _ae:
                vlog(f"artwork relocation raised: {_ae}")
            if self.n_ok > 0 and not self.n_fail and not self.n_lossy:
                keep_unimported_download(self.download_result)
                kept_in = cfg.STAGING_DIR
            else:
                retain_download_staging(self.download_result, label="incomplete")
                kept_in = cfg.STAGING_DIR / cfg.BEETS_RETRY_DIR
            log.info(fmt(C.YELLOW, f"\n  --no-import: skipping beets. Files remain in {kept_in}/"))
        elif self.n_ok == 0:
            log.info(fmt(C.YELLOW, "\n  Skipping beets import: nothing succeeded."))
            retire_failed_download_staging(self.download_result)
        else:
            log.info("")
            # A multi-disc cover sits in the album root, which beets' import
            # task never searches; left there it strands in staging. The queue
            # lanes do this in their pre-import hooks, which this path skips.
            for _staged in (self.staged_dirs_for_import or []):
                try:
                    relocate_disc_album_artwork(_staged)
                except Exception as _ae:
                    vlog(f"artwork relocation raised: {_ae}")
            own_cover = (
                self.upgrade_backup_path if self.auto_upgrade_active else self.cover_backup)
            if own_cover is not None:
                _keep_own_cover(own_cover, self.staged_dirs_for_import or [])
            note_superseded_attempts(self.download_result)
            pending = (
                not self.auto_upgrade_active
                or self.replaced_beets_entries is None
                or mark_pending_upgrade(
                    self.upgrade_backup_path, self.replaced_beets_entries,
                    self.staged_dirs_for_import or []))
            if not pending:
                log.info(fmt(C.RED,
                    "  ✗  Couldn't record the upgrade in its backup; "
                    "skipping the import."))
            # A brand-new album (no folder found on disk) lands in a fresh
            # directory, so it can't split an existing beets album into
            # duplicate rows, so skip the full-library de-dup scan for it.
            # Folding the new entry into the replaced one would change the
            # rows the retire step below proves it is removing.
            try:
                self.imported = pending and beets_import_paths(
                    consolidate=self.album_dir is not None and not self.exact_edition,
                    album_dirs=self.staged_dirs_for_import,
                    album_dir=self.album_dir,
                    folder=self.import_folder,
                    **({"repair_token": self.repair_token} if self.repair_token else {}),
                    protected_paths=(
                        [self.album_dir] if self.replaced_beets_entries is not None
                        else None),
                )
            except MusicDiskFull as exc:
                self.imported, self.disk_full = False, exc
            if (
                self.imported
                and "_staging_run" in self.download_result
                and not retire_download_staging_after_import(self.download_result)
            ):
                log.info(fmt(
                    C.RED,
                    "  ✗  The exact download run was not empty after beets; "
                    "the import was not accepted.",
                ))
                self.imported = False
            if not self.imported:
                retained = retain_download_staging(
                    self.download_result, label="beets-incomplete")
                if retained:
                    log.info(fmt(
                        C.YELLOW,
                        "  ⚠  Kept the exact residual download run in "
                        "private staging recovery.",
                    ))

        self.download_phase_completed = True

    def _settle_upgrade_backup(self):
        # Always resolve upgrade backup, including on exception.
        if self.upgrade_backup_path is not None:
            # Require zero failures AND zero lossy-deletes.
            upgrade_succeeded = (self.download_phase_completed
                                  and self.imported
                                  and self.n_ok > 0
                                  and self.n_fail == 0
                                  and self.n_lossy == 0)
            replacement_dir = None
            if (self.exact_edition or self.import_folder is not None) and upgrade_succeeded:
                replacement_dir = self._imported_dir()
            # An auto-upgrade wipes the only full copy, so it has to clear a
            # higher bar before the backup is deleted: the rebuilt folder must
            # be verifiably at least as complete as the original (same-or-more
            # tracks and playtime).
            if self.exact_edition:
                upgrade_verified = upgrade_succeeded and folder_holds_all_tracks(
                    replacement_dir, self.qobuz_tracks, destructive=True, edition=self.album)
            else:
                upgrade_verified = upgrade_succeeded and (
                    self.import_folder is None or replacement_dir is not None)
            if upgrade_verified and self.auto_upgrade_active:
                upgrade_verified = _upgrade_improves_original(replacement_dir, self.upgrade_backup_path)
            if upgrade_verified:
                # Carry non-audio companions (booklets, art, .cue/.log) from
                # the backup into the rebuilt album before deleting it. The
                # audio-only verification ignores them, so they'd be lost.
                carried = _carry_non_audio_from_backup(
                    self.album, self.album_dir, self.upgrade_backup_path,
                    replacement_dir=replacement_dir)
                if carried is not None:
                    replacement_path, replacement_receipt, left = carried
                    tree_validator = (
                        _upgrade_improves_original
                        if self.auto_upgrade_active
                        else _intentional_replacement_verified
                    )

                    def validator(replacement, backup):
                        return tree_validator(replacement, backup) and (
                            not self.exact_edition or folder_holds_all_tracks(
                                replacement, self.qobuz_tracks, destructive=True,
                                edition=self.album, folder_label=replacement_path.name))

                    catalogue_retired = (
                        self.replaced_beets_entries is not None
                        and retire_replaced_beets_entries(
                            self.replaced_beets_entries,
                            replacement_path,
                            _replacement_audio_paths(
                                replacement_path, replacement_receipt
                            ),
                        )
                    )
                    if not catalogue_retired:
                        self.upgrade_unverified = True
                        self.catalogue_unverified = True
                        _pin_or_warn(
                            self.upgrade_backup_path,
                            "upgrade kept; the old Beets entries "
                            "could not be removed")
                        log.info(fmt(C.YELLOW,
                            "  ⚠  Upgrade landed, but its Beets catalogue "
                            "couldn't be reconciled."))
                        log.info(fmt(C.GRAY,
                            f"     Backup remains at {self.upgrade_backup_path} "
                            "until you reconcile it."))
                    elif left:
                        self.companions_kept = left
                        log.info(fmt(C.YELLOW, _keep_backup_for_companions(
                            self.upgrade_backup_path, left)))
                    elif _dispose_replaced_original(
                        self.upgrade_backup_path,
                        replacement_path=replacement_path,
                        expected_replacement_receipt=replacement_receipt,
                        replacement_validator=without_unread_audio(
                            validator, self.upgrade_backup_path,
                            replacement_receipt),
                    ):
                        if self.auto_upgrade_active and not self.upgrade_only:
                            log.info(fmt(C.GRAY,
                                "  ✓  Upgrade complete; backup cleared."))
                    else:
                        self.upgrade_unverified = True
                        _pin_or_warn(
                            self.upgrade_backup_path,
                            "upgrade kept; final exact replacement proof "
                            "did not hold")
                        log.info(fmt(C.YELLOW,
                            "  ⚠  Upgrade succeeded but the exact backup "
                            "couldn't be removed."))
                        log.info(fmt(C.GRAY,
                            f"     Backup remains at {self.upgrade_backup_path} "
                            "until you reconcile it."))
                else:
                    self.upgrade_unverified = True
                    _pin_or_warn(self.upgrade_backup_path)
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Upgrade landed, but the rebuilt album or its "
                        "companions couldn't be confirmed written to disk; keeping the "
                        "backup."))
                    log.info(fmt(C.GRAY,
                        f"     Backup remains at {self.upgrade_backup_path}; keep it "
                        "until you've confirmed the rebuilt album is safe."))
            elif upgrade_succeeded and self.auto_upgrade_active:
                # Passed the decode/lossy gate but the rebuilt folder isn't
                # verifiably as complete as the original. Put the original
                # back and move the replacement out of the library.
                put_back = _put_original_back(self.upgrade_backup_path, self.album_dir)
                if put_back is not None:
                    self.upgrade_put_back = True
                    self.imported = False
                    self.upgrade_set_aside = put_back if put_back is not True else None
                    log.info(fmt(C.YELLOW,
                        "\n  ⚠  Upgrade couldn't be verified as complete, so "
                        f"your original album was put back in {self.album_dir}."))
                    if self.upgrade_set_aside is not None:
                        log.info(fmt(C.GRAY,
                            "     The new download was set aside at "
                            f"{self.upgrade_set_aside}."))
                    if not forget_imported_beets_entries(
                            self.replaced_beets_entries, self.album_dir):
                        log.info(fmt(C.GRAY,
                            "     If beets still lists the new download, run "
                            "`beet update`."))
                else:
                    self.upgrade_unverified = True
                    log.info(fmt(C.YELLOW,
                        "\n  ⚠  Upgrade couldn't be verified as complete, and "
                        "your original couldn't be put back automatically."))
                    log.info(fmt(C.GRAY,
                        f"     Original kept at {self.upgrade_backup_path}; it is "
                        "not removed automatically."))
                    log.info(fmt(C.WHITE,
                        f"     To go back to it, move {self.album_dir!s} out of the "
                        "library, then move the backup's files, not its "
                        f"dot-files, into {self.album_dir!s}."))
            elif self.download_phase_completed and self.args.no_import:
                self.upgrade_unverified = True
                _pin_or_warn(
                    self.upgrade_backup_path,
                    "upgrade backup kept; --no-import leaves the "
                    "replacement outside the verified library flow")
                log.info(fmt(C.YELLOW,
                    f"\n  ⚠  --no-import set; cannot auto-verify upgrade. "
                    f"Backup kept at {self.upgrade_backup_path}."))
            else:
                # --force routes through the same backup-restore branch (the
                # forced re-download backed the original up the same way an
                # auto-upgrade would), so name what ran.
                if upgrade_succeeded:
                    self.imported = False
                    self.upgrade_unverified = True
                same_quality = (self.early_result or {}).get("result") == "upgrade_same_quality"
                if same_quality:
                    if not discard_download_staging(self.download_result):
                        log.warning("  The same-quality download could not be cleared from staging.")
                else:
                    op_label = "Forced re-download" if self.use_force else "Upgrade"
                    log.info(fmt(C.YELLOW,
                        f"\n  ⚠  {op_label} did not succeed (no successful import); "
                        "restoring backup …"))
                if restore_upgrade_backup(self.upgrade_backup_path, self.album_dir,
                                          replace_partial=not same_quality):
                    if same_quality:
                        log.info("  The download matched the original quality. Kept the original album.")
                        saved_views.note_same_quality_upgrade(
                            self.album_dir, self.album, self.quality_verdict, resampled=self.resampled_n)
                    else:
                        log.info(fmt(C.GREEN,
                            f"  ✓  Restored original folder to {self.album_dir}."))
                else:
                    if same_quality:
                        self.early_result["result"] = "not_imported"
                    self.upgrade_unverified = True
                    _pin_or_warn(
                        self.upgrade_backup_path,
                        "upgrade backup kept; automatic restore did not "
                        "complete")
                    log.info(fmt(C.RED,
                        f"  ✗  Auto-restore failed. Original folder is at: "
                        f"{self.upgrade_backup_path}"))
                    log.info(fmt(C.WHITE,
                        f"     To put it back, move anything left in "
                        f"{self.album_dir!s} out of the library, then move the "
                        "backup's files, not its dot-files, into it."))

    def _settle_gap_fill_backup(self):
        # run_album_download records this the moment it stashes present tracks,
        # so it's reachable here even when the rip raised before returning.
        gap_fill_backup_path = self.download_result.get("gap_fill_backup_path")
        if gap_fill_backup_path is not None and gap_fill_backup_path.exists():
            gap_fill_succeeded = (self.download_phase_completed
                                  and self.imported
                                  and self.n_ok > 0
                                  and self.n_fail == 0
                                  and self.n_lossy == 0)
            if gap_fill_succeeded:
                # Mirror the executor's "imported but folder not located"
                # guard: beets files by tags, so a re-rip whose canonical
                # folder differs from album_dir (renamed edition, unexpected
                # albumartist) lands elsewhere.
                _filled = (
                    self._imported_dir()
                    if self.post_import_signatures else None
                )
                if _filled is None and not self.exact_edition:
                    _filled = find_album_dir_filesystem(self.album)
                if _filled is None and not self.exact_edition:
                    clear_scan_caches()
                    _filled = find_album_dir_filesystem(self.album)
                # NOT just "the folder has any audio": album_dir still
                # physically holds the EXTRAS (which are audio) after the
                # present tracks were moved to the backup, so a >0 check
                # passes even when the re-rip landed elsewhere / imported
                # nothing and the present tracks were never restored. The
                # backup (their only copy) would be deleted.
                _filled_ok = folder_holds_all_tracks(
                    _filled, self.qobuz_tracks, destructive=True,
                    edition=self.album if self.exact_edition else None)
                _originals_ok = _filled_ok and (
                    _carry_track_annotations(gap_fill_backup_path, _filled, edition=self.album)
                    if self.force_tracks else backup_files_preserved(_filled, gap_fill_backup_path)
                )
                _filled_receipt = (
                    capture_album_source_receipt(_filled)
                    if _originals_ok else None
                )
                if _filled_ok and not _originals_ok:
                    self.recovery_unverified = True
                    _pin_or_warn(
                        gap_fill_backup_path,
                        "replacement backup kept; original files or tags could not be verified")
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Download landed, but the originals couldn't be verified; "
                        "keeping the backup."))
                elif _filled_ok and _filled_receipt is not None:
                    if not retire_backup_beets_entries(
                        gap_fill_backup_path,
                        _filled,
                        _filled_receipt,
                        **({"replacement_audio_paths": [t["path"] for t in catalog.edition_files(
                            self.album, read_album_dir(_filled), _filled)]} if self.exact_edition else {}),
                    ):
                        self._gap_fill_not_located = True
                        self.recovery_unverified = True
                        self.catalogue_unverified = True
                        _pin_or_warn(
                            gap_fill_backup_path,
                            "gap-fill backup kept; the old Beets "
                            "entries could not be removed")
                        log.info(fmt(C.YELLOW,
                            "  ⚠  Gap-fill landed, but its Beets catalogue "
                            "couldn't be reconciled; keeping the exact "
                            "backup."))
                    elif not dispose_backup(
                        gap_fill_backup_path,
                        replacement_path=_filled,
                        expected_replacement_receipt=_filled_receipt,
                        replacement_validator=lambda replacement, backup: (
                            folder_holds_all_tracks(
                                replacement, self.qobuz_tracks, destructive=True,
                                edition=self.album if self.exact_edition else None,
                                folder_label=_filled.name)
                            and (_intentional_replacement_verified(replacement, backup)
                                 if self.force_tracks else backup_files_preserved(replacement, backup))
                        ),
                    ):
                        self.recovery_unverified = True
                        _pin_or_warn(
                            gap_fill_backup_path,
                            "gap-fill backup kept; final exact "
                            "replacement proof did not hold")
                        log.info(fmt(C.YELLOW,
                            "  ⚠  Gap-fill complete but the exact backup "
                            "couldn't be removed."))
                elif _filled_ok:
                    self._gap_fill_not_located = True
                    self.recovery_unverified = True
                    _pin_or_warn(
                        gap_fill_backup_path,
                        "gap-fill backup kept; replacement not confirmed written to disk")
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Gap-fill landed, but the filled album couldn't "
                        "be confirmed written to disk; keeping the backed-up tracks."))
                    log.info(fmt(C.GRAY,
                        f"     Backup remains at {gap_fill_backup_path}."))
                else:
                    self._gap_fill_not_located = True
                    self.recovery_unverified = True
                    _pin_or_warn(
                        gap_fill_backup_path,
                        "gap-fill backup kept; the complete replacement "
                        "could not be located")
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Gap-fill imported but the filled album couldn't be "
                        f"located on disk; keeping backed-up tracks at "
                        f"{gap_fill_backup_path}."))
            elif self.download_phase_completed and self.args.no_import:
                # Gate on download_phase_completed like the upgrade branch above:
                # a crash MID-download (phase not completed) must fall through to
                # the restore branch, not keep a backup while the album is short.
                self.recovery_unverified = True
                _pin_or_warn(
                    gap_fill_backup_path,
                    "gap-fill backup kept; --no-import leaves the "
                    "replacement outside the verified library flow")
                log.info(fmt(C.YELLOW,
                    f"  ⚠  --no-import; gap-fill backup kept at "
                    f"{gap_fill_backup_path}."))
            else:
                log.info(fmt(C.YELLOW,
                    "  ⚠  Gap-fill did not succeed; restoring backed-up tracks…"))
                _n_back = restore_gap_fill_backup(gap_fill_backup_path, self.album_dir,
                                                  keep_larger_dst=False)
                # restore_gap_fill_backup preserves anything it couldn't
                # restore at the backup path (and only removes the backup on a
                # complete restore).
                if _n_back and not gap_fill_backup_path.exists():
                    log.info(fmt(C.GREEN,
                        f"  ✓  Restored {plural(_n_back, 'track')} to {self.album_dir}."))
                else:
                    self.recovery_unverified = True
                    _pin_or_warn(
                        gap_fill_backup_path,
                        "gap-fill backup kept; automatic restore was "
                        "partial or failed")
                    log.info(fmt(C.RED,
                        f"  ✗  Restored {plural(_n_back, 'track')}; remaining originals "
                        f"preserved at {gap_fill_backup_path}; reconcile by hand."))

    def _consolidate(self):
        self.n_consolidated = self.consolidation_failed = 0
        self.consolidation_interrupted = self.consolidation_cancelled = False
        # treat_as_new keeps this download as its own edition; consolidation folds
        # editions together by deleting overlapping sibling tracks, so the two are
        # mutually exclusive. Never consolidate a deliberately separate edition.
        if self.args.consolidate and not self.treat_as_new and not self.force_tracks:
            if self.imported:
                try:
                    self.n_consolidated, self.consolidation_failed = consolidate_albums(self.album, self.args)
                except ConsolidationCancelled as cancel:
                    self.n_consolidated = cancel.moved
                    self.consolidation_cancelled = True
                except KeyboardInterrupt:
                    self.consolidation_interrupted = True
                    log.info(fmt(C.GRAY, "\n  Consolidation interrupted."))
            else:
                log.info(fmt(C.YELLOW,
                    "\n  --consolidate requested but beets import didn't succeed; skipping."))

    def _locate_imported(self):
        if self.imported:
            strict_success = self.n_fail == 0 and self.n_lossy == 0
            if getattr(self.args, "migrate_multi_artist", False) and strict_success:
                migration_source = (
                    self._imported_dir()
                    if self.post_import_signatures else None
                )
                if migration_source is None:
                    log.info(fmt(
                        C.YELLOW,
                        "  ⚠  Multi-artist folder filing was skipped because "
                        "the exact imported album could not be located.",
                    ))
                    self.post_dir = None
                else:
                    self.post_dir = prompt_and_migrate_multi_artist_folder(
                        self.album,
                        self.args,
                        authority=run_lock.current_lease(),
                        source_dir=migration_source,
                    )
            else:
                self.post_dir = None
            if self.post_dir is None:
                self.post_dir = (
                    self._imported_dir()
                    if self.post_import_signatures else None
                )
                if self.post_dir is None and not self.exact_edition:
                    self.post_dir = find_album_dir_filesystem(self.album)
            # A brand-new album can land in a folder the cached listing predates;
            # clear the cache and look once more before giving up, or the
            # sidecars and the truncation check silently no-op.
            if self.post_dir is None and not self.exact_edition:
                clear_scan_caches()
                self.post_dir = find_album_dir_filesystem(self.album)
            if self.post_dir:
                if self.resampled_n > 0:
                    mark_local_album_capped(self.post_dir, qobuz_album=self.album,
                                            origin="auto")
                # Materialise .lrc sidecars next to the final renamed files
                # (no-op unless LYRICS_FORMAT is sidecar/both).
                try:
                    write_post_import_sidecars(
                        [self.post_dir, self.album_dir],
                        track_signatures=self.post_import_signatures)
                except Exception as _e_sc:
                    vlog(f"post-import sidecar write raised: {_e_sc}")
                # After the sidecars, because writing one can rewrite the audio
                # file and leave the rows just cached already stale.
                cache_album_tags([self.post_dir, self.album_dir])

    def _count_shortfall(self):
        self.n_failed, self.n_damaged, self.n_truly_lossy, self.n_unavailable = track_shortfall(self.download_result)
        self.n_retryable = self.n_failed + self.n_damaged
        self.n_unretryable = self.n_truly_lossy + self.n_unavailable
        self.unavailable_tracks = self.download_result.get("unavailable_tracks", [])
        self.downsample_attention = bool(
            self.downsample_outcome["downsample_errors"]
            or self.downsample_outcome["downsample_flush_warnings"]
            or self.downsample_outcome["downsample_cancelled"]
        )
        _broken = set(self.broken_tracks)
        self.truly_lossy = [t for t in self.lossy_tracks if t not in _broken]

    def _print_result(self):
        if (
            self.already_confirmed
            and not self.n_fail
            and not self.n_lossy
            and self.imported
            and not self._gap_fill_not_located
            and not self.recovery_unverified
            and not self.downsample_attention
            and not self.consolidation_interrupted
        ):
            if self.auto_upgrade_active and not self.upgrade_unverified:
                verdict = self.quality_verdict or {}
                label = ("replaced" if verdict.get("under") and not verdict.get("recovered")
                         else "↑ upgraded")
                log.info(fmt(C.MAGENTA + C.BOLD,
                    f"  {label} · {plural(self.n_ok, 'track')} · {int(self.elapsed)}s · imported"))
            elif not self.auto_upgrade_active and not self.upgrade_unverified:
                log.info(fmt(C.GREEN,
                    f"  ✓ {self.n_ok} downloaded · {int(self.elapsed)}s · imported"))
            # An unverified replacement already printed the backup warning above;
            # don't also claim a clean download or upgrade here.
        else:
            section("Result")
            log.info("")
            log.info(f"  {fmt(C.GREEN if self.n_ok else C.GRAY,    '✓ downloaded:')}    {self.n_ok}")
            if self.n_truly_lossy:
                log.info(f"  {fmt(C.YELLOW, '⚠ lossy on Qobuz:')} {self.n_truly_lossy}")
            if self.n_damaged:
                log.info(f"  {fmt(C.YELLOW, '⚠ broken file:')}   {self.n_damaged}")
            if self.n_unavailable:
                log.info(f"  {fmt(C.YELLOW, '⚠ not on Qobuz:')}  {self.n_unavailable}")
            if self.n_failed:
                log.info(f"  {fmt(C.RED, '✗ failed:')}        {self.n_failed}")
            if self.downsample_outcome["downsample_errors"]:
                log.info(
                    f"  {fmt(C.RED, '✗ downsample failed:')} "
                    f"{self.downsample_outcome['downsample_errors']}"
                )
            if self.downsample_outcome["downsample_flush_warnings"]:
                log.info(
                    f"  {fmt(C.YELLOW, '⚠ disk write warning:')}  "
                    f"{self.downsample_outcome['downsample_flush_warnings']}"
                )
            if self.downsample_outcome["downsample_cancelled"]:
                log.info(f"  {fmt(C.YELLOW, '⚠ downsample:')}     stopped early")
            log.info(f"  {fmt(C.GRAY, '  runtime:')}        {int(self.elapsed)}s")
            beets_outcome = (
                "imported" if self.imported
                else "imported, then the original was put back" if self.upgrade_put_back
                else "not imported (--no-import)" if self.args.no_import
                else "not imported"
            )
            log.info(f"  {fmt(C.GRAY, '  beets:')}          {beets_outcome}")
            if self.args.consolidate:
                if self.consolidation_interrupted:
                    log.info(f"  {fmt(C.YELLOW, '⚠ consolidated:')} stopped early")
                else:
                    log.info(f"  {fmt(C.GRAY, '  consolidated:')}   {plural(self.n_consolidated, 'sibling track')} removed")
                    if self.consolidation_failed:
                        log.info(f"  {fmt(C.YELLOW, '⚠ consolidated:')} {plural(self.consolidation_failed, 'track')} left in place")
            if self.failed_tracks:
                log.info(fmt(C.RED, "\n  failed tracks:"))
                for t in self.failed_tracks[:10]:
                    log.info(f"     {truncate(t, 60)}")
            if self.truly_lossy:
                log.info(fmt(C.YELLOW,
                    "\n  only available lossy on Qobuz (would need another source):"))
                for t in self.truly_lossy[:10]:
                    log.info(f"     {truncate(t, 60)}")
            if self.unavailable_tracks:
                log.info(fmt(C.YELLOW,
                    "\n  not available on Qobuz (would need another source):"))
                for t in self.unavailable_tracks:
                    log.info(f"     {truncate(t, 60)}")
            if self.n_damaged:
                log.info(fmt(C.YELLOW,
                    "\n  Qobuz sent a broken file at this quality, discarded:"))
                for t in self.broken_tracks[:10]:
                    log.info(f"     {truncate(t, 60)}")
            log.info("")

    def _outcome(self):
        if self.n_ok and (
            self.n_retryable or self.n_unretryable or self.downsample_attention
            or self.upgrade_unverified
            or self.catalogue_unverified
            or self.recovery_unverified
            or self.consolidation_interrupted
            or (
                self.quality_verdict
                and self.quality_verdict.get("under")
                and not self.quality_verdict.get("recovered")
            )
        ):
            result_status = "partial"
        elif self.n_ok and not self.imported and not self.args.no_import:
            # Tracks ripped but didn't make it into the library (a beets failure, or
            # an upgrade/--force the finally block rolled back to the original), the
            # library is unchanged, so don't report this as a completed "downloaded".
            result_status = "not_imported"
        elif self.n_ok:
            result_status = "downloaded"
        elif self.n_retryable or self.n_unretryable:
            result_status = "failed"
        else:
            result_status = "nothing_landed"

        # A clean import can still hide a truncated track: the download drops
        # files that won't decode, but one cut at a frame boundary with its FLAC
        # header rewritten short decodes fine and only shows against the real
        # Qobuz length.
        if (result_status in ("downloaded", "partial") and self.token
                and not getattr(self.args, "no_import", False)):
            if self.post_dir:
                warn_if_download_truncated(
                    self.post_dir, self.token, self.album.get("title"),
                    self.missing if self.present else None)

        outcome = {
            "result": result_status,
            "rate_limited": self.download_result.get("rate_limited", False),
            "n_ok": self.n_ok, "n_fail": self.n_fail, "n_lossy": self.n_lossy,
            **shortfall_fields(self.download_result),
            "failed_tracks": self.failed_tracks,
            "lossy_tracks": self.truly_lossy,
            "broken_tracks": self.broken_tracks,
            "imported": self.imported,
            **({"repair_kept": self.repair_kept} if self.repair_kept else {}),
            "dir": self.post_dir,
            "upgrade_unverified": self.upgrade_unverified,
            "catalogue_unverified": self.catalogue_unverified,
            "recovery_unverified": self.recovery_unverified,
            "companions_kept": self.companions_kept,
            "upgrade_put_back": self.upgrade_put_back,
            # Put back because the new copy failed its check, not because the
            # download came up short, so another try fetches the same copy.
            "upgrade_refused": self.upgrade_put_back,
            "upgrade_set_aside": str(self.upgrade_set_aside) if self.upgrade_set_aside else None,
            "auto_upgrade": bool(self.auto_upgrade_active),
            "quality_verdict": self.quality_verdict,
            "consolidation_interrupted": self.consolidation_interrupted,
            "consolidation_cancelled": self.consolidation_cancelled,
            "consolidated_tracks_removed": self.n_consolidated,
            "consolidation_failed": self.consolidation_failed,
            "elapsed_s": int(self.elapsed),
            **self.downsample_outcome,
        }
        n_missing = tracks_missing(outcome)
        if n_missing:
            reasons = []
            if self.n_retryable:
                reasons.append(f"downloading the album again fetches {self.n_retryable}")
            for count, why in ((self.n_truly_lossy, "only lossy"),
                               (self.n_unavailable, "not available")):
                if count:
                    reasons.append(
                        f"{count} {'is' if count == 1 else 'are'} {why} on Qobuz")
            log.info(fmt(C.YELLOW,
                f"  ⚠  Imported, {plural(n_missing, 'track')} missing: "
                f"{'; '.join(reasons)}."))
        return outcome


def process_album(album, args, *, allow_force=True, label=None,
                  already_confirmed=False, upgrade_only=False,
                  token=None, quality=None, treat_as_new=False,
                  exact_edition=False,
                  expected_album_receipt=None,
                  expected_gap_fill_receipts=None,
                  cover_backup=None, rebuild_dir=None, repair_damaged=()):
    """End-to-end processing for one Qobuz album: detect → prompt → download →
    cleanup → import → consolidate.

    Parameters:
      album         Qobuz album dict (must include tracks.items)
      args          parsed argparse Namespace
      allow_force   if False, --force is ignored for this album. Used by
                    artist mode so a single forgetful run doesn't wipe every
                    album by an artist.
      label         optional prefix for status output (e.g. "[3/12]")
      cover_backup  a whole-album backup the caller took first, whose cover
                    art the download keeps
      rebuild_dir   the folder ``cover_backup`` was taken from; the whole
                    album is downloaded into it under the backup's album name
      repair_damaged  damaged filenames relative to the whole-album backup

    Track-by-track downloading is a queue-only contract: this path always
    downloads the whole album in one rip invocation and delegates per-track
    decisions to run_album_download. Callers that need one-track-at-a-time
    isolation (repair) must go through the queue builder/executor and set
    `force_track_by_track`.

    Returns dict with run results (used by artist mode summary).
    Never raises for "this album can't be done"; only KeyboardInterrupt,
    AuthLost, and SystemExit propagate.
    """
    return _AlbumRun(
        album, args, allow_force=allow_force, label=label,
        already_confirmed=already_confirmed, upgrade_only=upgrade_only,
        token=token, quality=quality, treat_as_new=treat_as_new,
        exact_edition=exact_edition,
        expected_album_receipt=expected_album_receipt,
        expected_gap_fill_receipts=expected_gap_fill_receipts,
        cover_backup=cover_backup, rebuild_dir=rebuild_dir, repair_damaged=repair_damaged,
    ).run()
