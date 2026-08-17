"""Core album processing: detect gaps, prompt, download, import, consolidate."""
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.api import client as api_client
from qobuz_librarian.download import (
    download_staged_files,
    retain_download_staging,
    retire_download_staging_after_import,
    retire_empty_download_staging,
    run_album_download,
    validated_staged_album_dirs,
)
from qobuz_librarian.download_result import incomplete_track_counts
from qobuz_librarian.integrations.beets import (
    beets_import_paths,
    capture_beets_album_entries,
    relocate_disc_album_artwork,
    retire_backup_beets_entries,
    retire_replaced_beets_entries,
    staging_preflight,
)
from qobuz_librarian.integrations.lyrics import (
    _record_post_import_lyric_retry,
    _resolve_signatures_to_paths,
    write_post_import_sidecars,
)
from qobuz_librarian.integrations.rip import (
    is_cancel_requested,
    snapshot_staging,
)
from qobuz_librarian.library import candidate_premise, post_import_relocation, tags
from qobuz_librarian.library.backup import (
    backup_album_dir,
    capture_album_source_receipt,
    carry_backup_companions,
    dispose_backup,
    pin_unverified_upgrade_backup,
    restore_gap_fill_backup,
    restore_incomplete_upgrade_backup,
    restore_upgrade_backup,
    warn_pin_failed,
)
from qobuz_librarian.library.catalog import (
    _disc_scoped_match,
    _is_split_album_merge,
    album_quality_label,
    compute_missing,
    find_album_dir_by_track_signatures,
    find_album_dir_filesystem,
    find_existing_tracks,
    find_expanded_edition,
    find_extras_in_existing,
    folder_holds_all_tracks,
    is_lossless_album,
    prompt_and_migrate_multi_artist_folder,
    track_signatures_for_album_dirs,
)
from qobuz_librarian.library.post_import_relocation import (
    PostImportRelocationAttention,
    PostImportRelocationUnavailable,
    RelocationKind,
)
from qobuz_librarian.library.scanner import (
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
from qobuz_librarian.modes.consolidate import consolidate_albums
from qobuz_librarian.quality.decision import (
    compare_album_quality,
    existing_track_quality,
    mark_local_album_capped,
    quality_relation,
)
from qobuz_librarian.quality.verify import (
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
    log_fetch,
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
            f"  ✓  Restored the original folder after the interrupted "
            f"{operation}."))
        return outcome

    if not pin_unverified_upgrade_backup(
            backup,
            f"{operation} recovery kept; automatic reconciliation did not "
            "complete"):
        warn_pin_failed(backup)
    unresolved = (
        f" ({outcome.unresolved} original file(s) still unresolved)"
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


def force_cleanup_preflight(album, args, *, expected_album_receipt=None):
    """With --force, move an existing album dir aside to a backup before
    re-import, so beets doesn't make '<n>.1.flac' collisions against the old
    files, and a re-download that fails can be restored.

    Returns the backup Path when the folder was moved aside; True when there's
    nothing to move; None when the operation must stop without further prompts;
    False when the user declined or no backup data was retained. --yes does NOT
    silence this prompt."""
    if not args.force:
        return True

    album_dir = find_album_dir_filesystem(album)
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
        f"     {len(audio_files)} audio file(s), {format_size(total_size)} total"))
    log.info(fmt(C.GRAY,
        "     Left in place, beets would create '<n>.1.flac' alongside the old files."))

    move_it = confirm(
        "\n  Move this folder to a backup before re-downloading? "
        "(restored automatically if the re-download fails)",
        default_yes=True, auto_yes=False)
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
    lengths/quality come from the same reader the rest of the app uses.
    degraded=True means part of the tree couldn't be read, so the list may be
    INCOMPLETE. Counts and quality drawn from it must not authorize deleting
    anything (a transient read error would read as a smaller album)."""
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
    if old_isrc or new_isrc:
        return bool(
            old_isrc
            and old_isrc == new_isrc
            and not (old_mbid and new_mbid and old_mbid != new_mbid)
        )
    if old_mbid or new_mbid:
        return bool(old_mbid and old_mbid == new_mbid)
    return _unidentified_track_match(original, replacement)


def _pair_upgrade_tracks_for_disposal(originals, replacements):
    """Return unique, destruction-safe original/replacement pairs.

    The normal library matcher deliberately tolerates missing identifiers and
    fuzzy edition titles so scans can find music. Deleting the upgrade backup
    needs stronger evidence: one-sided recording IDs cannot fall back to a
    title, and unidentified tracks must agree on slot, title, and duration.
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
        candidates = [
            index for index, replacement in enumerate(replacements)
            if _upgrade_track_identity_matches(original, replacement)
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


def _upgrade_replacement_verified(album, album_dir, backup_path):
    """True only when the freshly imported album is at least as complete as the
    backed-up original: same-or-more tracks AND same-or-more playtime. The
    success gate clears `flac -t` per file, which proves each file decodes but
    not that the matcher kept every track or that a re-rip didn't land short.
    """
    # beets renames the imported folder to its canonical $albumartist/$album,
    # so the post-import dir is often not the one resolved before the
    # download.
    drop_artist_subdirs_cache(album_dir.parent)
    post_dir = find_album_dir_filesystem(album)
    if not post_dir or not post_dir.exists():
        return False
    return _upgrade_trees_verified(post_dir, backup_path)


def _upgrade_trees_verified(post_dir, backup_path):
    """Compare exact replacement/original tree views at the disposal gate."""
    new_tracks, new_degraded = _folder_tracks_checked(post_dir)
    old_tracks, old_degraded = _folder_tracks_checked(backup_path)
    # A degraded walk returns only the READABLE tracks: a transient error on
    # the backup side lowers the baseline enough for an incomplete replacement
    # to pass every gate below and delete the only full copy.
    if old_degraded or new_degraded:
        log.info(fmt(C.YELLOW,
            "  ⚠  Couldn't fully read the "
            f"{'backup' if old_degraded else 'imported album'} while verifying "
            "the upgrade; keeping the backup."))
        return False
    new_n, new_secs = len(new_tracks), _tracks_seconds(new_tracks)
    old_n, old_secs = len(old_tracks), _tracks_seconds(old_tracks)
    # An empty or unreadable backup reads as (0, 0.0), which makes both gates
    # below pass vacuously (new_n < 0 is never true), deleting the only full
    # copy. Treat an unreadable/empty backup as unverifiable and keep it.
    if old_n == 0:
        log.info(fmt(C.YELLOW,
            "  ⚠  Couldn't read the upgrade backup (empty or unreadable) "
            "; keeping it."))
        return False
    if new_n < old_n:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Upgrade landed {new_n} track(s) but the original held "
            f"{old_n}; keeping the backup."))
        return False
    if old_secs > 0 and new_secs < old_secs * _UPGRADE_VERIFY_DURATION_RATIO:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Upgrade playtime {int(new_secs)}s falls short of the "
            f"original {int(old_secs)}s (a track may be truncated); "
            f"keeping the backup."))
        return False
    # Every original must have one identity-paired replacement.
    pairs = _pair_upgrade_tracks_for_disposal(old_tracks, new_tracks)
    if pairs is None:
        log.info(fmt(C.YELLOW,
            "  ⚠  Couldn't prove one unique replacement for every original "
            "track; keeping the backup."))
        return False
    for ot, nt in pairs:
        oq, nq = _track_quality(ot), _track_quality(nt)
        title = truncate(str(ot.get("title") or ""), 40)
        if _track_identity_conflicts(ot, nt):
            log.info(fmt(C.YELLOW,
                f"  ⚠  Upgrade matched {title} to a different recording "
                "identity; keeping the backup."))
            return False
        relation = quality_relation(nq, oq)
        if relation not in ("equal", "higher"):
            detail = (
                "trades bit depth for sample rate"
                if relation == "incomparable"
                else f"returned at {_fmt_quality(nq)} instead of {_fmt_quality(oq)}"
            )
            log.info(fmt(C.YELLOW,
                f"  ⚠  Upgrade for {title} {detail}; keeping the backup."))
            return False
        old_channels = _known_track_channels(ot)
        new_channels = _known_track_channels(nt)
        if old_channels is None or new_channels is None:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Couldn't verify the channel layout for {title} "
                "; keeping the backup."))
            return False
        if new_channels != old_channels:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Upgrade for {title} changed from {old_channels} to "
                f"{new_channels} channels; keeping the backup."))
            return False
        old_duration = _known_track_duration(ot)
        new_duration = _known_track_duration(nt)
        if old_duration is None or new_duration is None:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Couldn't verify the playtime for {title} "
                "; keeping the backup."))
            return False
        if new_duration < old_duration * _UPGRADE_VERIFY_DURATION_RATIO:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Upgrade for {title} is shorter than the original "
                "; keeping the backup."))
            return False
    return True


def _intentional_replacement_verified(replacement_path, _backup_path):
    """Final exact-tree gate for a user-requested forced replacement."""
    tracks, degraded = _folder_tracks_checked(replacement_path)
    return bool(tracks) and not degraded


def _carry_non_audio_from_backup(album, album_dir, backup_path,
                                 replacement_dir=None):
    """Carry companions between exact held trees.

    The returned pair is the exact replacement path and its updated receipt;
    callers must use both for the final held-view disposal decision. Any
    changed name, conflicting companion, symlink, active writer, or uncertain
    durability keeps the backup and returns ``None``.
    """
    dest = replacement_dir
    if not dest or not dest.exists():
        dest = find_album_dir_filesystem(album)
    if not dest or not dest.exists():
        dest = album_dir
    if not dest or not dest.exists():
        return None
    receipt = capture_album_source_receipt(dest)
    if receipt is None:
        return None
    updated = carry_backup_companions(
        backup_path,
        dest,
        expected_replacement_receipt=receipt,
    )
    return (dest, updated) if updated is not None else None


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
    Returns [(bare_title, [dirs])] for groups with 2+ members."""
    groups = {}
    for d in album_dirs:
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


def process_album(album, args, *, allow_force=True, label=None,
                  already_confirmed=False, upgrade_only=False,
                  token=None, quality=None, treat_as_new=False,
                  expected_album_receipt=None,
                  expected_gap_fill_receipts=None):
    """End-to-end processing for one Qobuz album: detect → prompt → download →
    cleanup → import → consolidate.

    Parameters:
      album         Qobuz album dict (must include tracks.items)
      args          parsed argparse Namespace
      allow_force   if False, --force is ignored for this album. Used by
                    artist mode so a single forgetful run doesn't wipe every
                    album by an artist.
      label         optional prefix for status output (e.g. "[3/12]")

    Track-by-track downloading is a queue-only contract: this path always
    downloads the whole album in one rip invocation and delegates per-track
    decisions to run_album_download. Callers that need one-track-at-a-time
    isolation (repair) must go through the queue builder/executor and set
    `force_track_by_track`.

    Returns dict with run results (used by artist mode summary).
    Never raises for "this album can't be done"; only KeyboardInterrupt,
    AuthLost, and SystemExit propagate.
    """
    use_force = bool(args.force) and allow_force
    label_prefix = (label + " ") if label else ""

    if not is_lossless_album(album):
        log.info(fmt(C.RED,
            f"\n  ✗  {label_prefix}Lossy-only on Qobuz "
            f"({album.get('title') or '?'}, {album_quality_label(album)})."))
        log.info(fmt(C.GRAY, "     Skipping: no lossless version on Qobuz."))
        return {"result": "lossy_only"}

    qobuz_tracks = (album.get("tracks") or {}).get("items") or []
    if not qobuz_tracks:
        log.info(fmt(C.RED, f"  ✗  {label_prefix}Album has no tracks in API response. Skipping."))
        return {"result": "no_tracks"}

    # ── Detect what's already there ──────────────────────────────────────────
    # --force cleanup is deferred until AFTER the download confirm
    # below: deleting here would mean a 'no' at the download prompt
    # leaves the user with their folder already wiped.
    if use_force:
        _, album_dir = find_existing_tracks(album)
        existing = []
        missing, present = qobuz_tracks, []
    elif treat_as_new:
        # Deliberately fetching a different edition of an album the user
        # already owns, such as a remaster or new mix kept alongside the original.
        album_dir = None
        existing = []
        missing, present = qobuz_tracks, []
    else:
        existing, album_dir = find_existing_tracks(album)
        vlog(f"hybrid detection: {len(existing)} existing track(s) total")
        missing, present = compute_missing(qobuz_tracks, existing)

    # ── Quality-aware auto-upgrade decision ──────────────────────────────────
    # Runs BEFORE the "already complete" early-exit, because an album that is
    # "complete" track-wise might still be lower quality than Qobuz and
    # warrant an upgrade-replace.
    auto_upgrade_active = False    # if True, do backup-then-wipe-then-redownload
    upgrade_backup_path = None     # populated after backup_album_dir succeeds
    replaced_beets_entries = None
    upgrade_existing_label = None  # before-quality label, set in the auto-upgrade branch

    # Quality-upgrade replace path.
    auto_upgrade = getattr(args, "auto_upgrade", cfg.AUTO_UPGRADE_ENABLED)
    if (auto_upgrade
            and existing
            and not use_force
            and not getattr(args, "no_upgrade", False)
            and album_dir is not None):
        qual = compare_album_quality(existing, album)
        cls = qual["classification"]
        extras = find_extras_in_existing(qobuz_tracks, existing)
        qbits, qrate = qual["qobuz_quality"]
        target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                        if qbits and qrate else "Qobuz quality")

        if cls == "all_higher":
            # Existing strictly better than Qobuz everywhere. Never replace.
            log.info(fmt(C.GREEN,
                f"\n  ✓  Already higher quality than Qobuz "
                f"({qual['n_above']} track(s) above target)."))
            if not missing:
                log_fetch({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "album_id": album.get("id"),
                    "artist": (album.get("artist") or {}).get("name"),
                    "title": album.get("title"),
                    "result": "skipped_already_higher_quality",
                    "tracks_total": len(qobuz_tracks),
                    "qobuz_quality": f"{qbits}-bit/{qrate}Hz",
                })
                return {"result": "skipped_already_higher_quality",
                        "n_total": len(qobuz_tracks)}
            # Has missing tracks. Will fall through to gap-fill prompt with
            # explicit warning that this creates mixed quality.
            log.info(fmt(C.YELLOW,
                f"     ⚠  {len(missing)} track(s) missing; filling them at "
                f"{target_label} would mix quality."))

        elif cls in ("all_lower", "mixed_below"):
            # Qobuz is higher quality, but only replace when it's verifiably
            # safe.
            if qual.get("n_unknown"):
                log.info(fmt(C.YELLOW,
                    f"\n  ⚠  Can't auto-upgrade: {qual['n_unknown']} track(s) "
                    f"have unreadable quality and would be replaced unverified; "
                    f"filling gaps only."))
                # Fall through to plain gap-fill; do NOT set auto_upgrade_active.
            elif extras and missing and already_confirmed:
                # User already okayed a gap-fill.
                _exp, _exp_extras, _exp_qual = _offer_expanded_edition(
                    album, album_dir, existing, extras, token, args)
                if _exp is not None and _exp_qual["classification"] in ("all_lower", "mixed_below"):
                    log.info(fmt(C.MAGENTA,
                        f"\n  ↑  Switching to {_exp.get('title') or '?'!r}; "
                        f"covers your {len(existing)} tracks "
                        f"with {len(_exp_extras)} local-only at {album_quality_label(_exp)}"))
                    album = _exp
                    qobuz_tracks = (_exp.get("tracks") or {}).get("items") or []
                    extras = _exp_extras
                    qual = _exp_qual
                    qbits, qrate = _exp_qual["qobuz_quality"]
                    target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                                    if qbits and qrate else "Qobuz quality")
                    missing, present = compute_missing(qobuz_tracks, existing)
                    if extras:
                        log.info(fmt(C.YELLOW,
                            f"\n  ⚠  Can't auto-upgrade ({len(extras)} bonus track(s) here); "
                            f"filling {len(missing)} at {target_label} (will mix quality)."))
                    else:
                        log.info(fmt(C.MAGENTA + C.BOLD,
                            f"\n  ↑ Auto-upgrade via expanded edition → {target_label}"))
                        log.info(fmt(C.YELLOW,
                            "  ⚠  This was queued as a gap-fill but will now back up and"
                            " replace the entire folder."))
                        auto_upgrade_active = True
                        existing = []
                        missing, present = qobuz_tracks, []
                else:
                    log.info(fmt(C.YELLOW,
                        f"\n  ⚠  Can't auto-upgrade ({len(extras)} bonus track(s) here); "
                        f"filling {len(missing)} at {target_label} (will mix quality)."))
            elif extras:
                # Before giving up, try to find an expanded edition that
                # covers the local tracks.
                _exp, _exp_extras, _exp_qual = _offer_expanded_edition(
                    album, album_dir, existing, extras, token, args)
                if (_exp is not None
                        and _exp_qual["classification"] in ("all_lower", "mixed_below")
                        and not _exp_extras):
                    log.info(fmt(C.MAGENTA,
                        f"\n  ↑  Switching to {_exp.get('title') or '?'!r}; "
                        f"covers your {len(existing)} tracks at {album_quality_label(_exp)}"))
                    album = _exp
                    qobuz_tracks = (_exp.get("tracks") or {}).get("items") or []
                    extras = _exp_extras
                    qual = _exp_qual
                    qbits, qrate = _exp_qual["qobuz_quality"]
                    target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                                    if qbits and qrate else "Qobuz quality")
                    missing, present = compute_missing(qobuz_tracks, existing)
                    log.info(fmt(C.MAGENTA + C.BOLD,
                        f"\n  ↑ Auto-upgrade via expanded edition → {target_label}"))
                    log.info(fmt(C.YELLOW,
                        "  ⚠  This was queued as a gap-fill but will now back up and"
                        " replace the entire folder. Confirm or cancel at the prompt below."))
                    auto_upgrade_active = True
                    existing = []
                    missing, present = qobuz_tracks, []
                else:
                    log.info(fmt(C.YELLOW,
                        f"\n  ⚠  Upgrade to {target_label} blocked: "
                        f"{len(extras)} on-disk track(s) Qobuz doesn't carry:"))
                    for _e in extras[:5]:
                        log.info(fmt(C.GRAY,
                            f"       • {truncate(_e.get('title') or '?', 60)}"))
                    if len(extras) > 5:
                        log.info(fmt(C.GRAY,
                            f"       ... and {len(extras) - 5} more"))
                    log.info(fmt(C.GRAY,
                        "     If these look like normal album tracks, your tags"
                        " differ from Qobuz's. Skipping (logged)."))
                    log_fetch({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "album_id": album.get("id"),
                        "artist": (album.get("artist") or {}).get("name"),
                        "title": album.get("title"),
                        "result": "skipped_has_extras",
                        "tracks_total": len(qobuz_tracks),
                        "qobuz_quality": f"{qbits}-bit/{qrate}Hz",
                        "n_extras": len(extras),
                        "extra_titles": [t.get("title") or "?" for t in extras[:20]],
                    })
                    return {"result": "skipped_has_extras",
                            "n_total": len(qobuz_tracks),
                            "n_extras": len(extras)}
            else:
                # No extras, so auto-upgrade is safe.
                _qcounts = {}
                for _t in existing:
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
                upgrade_existing_label = existing_label
                _fill = f" +{len(missing)} gap-fill" if missing else ""
                if not already_confirmed:
                    log.info(fmt(C.MAGENTA + C.BOLD,
                        f"\n  ↑ Auto-upgrade: {existing_label} → {target_label}{_fill}"))
                auto_upgrade_active = True
                # Override detection results so downstream code does a full
                # download.
                if upgrade_only:
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
                    _disc_scoped = _disc_scoped_match(present, existing)
                    for _et in existing:
                        _eisrc = (_et.get("isrc") or "").replace("-", "").upper().strip()
                        _enorm = _et.get("normalized") or ""
                        _edisc = _et.get("discnumber", 1) or 1
                        _estripped = normalize(strip_edition_suffix(
                            _et.get("title") or ""))
                        _best_qt, _best_rank = None, 99
                        for _qt in present:
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
                    existing = []
                    missing, present = _upgrade_targets, []
                else:
                    existing = []
                    missing, present = qobuz_tracks, []

        elif cls == "mixed_above":
            # Some tracks above, rest equal. Treat like all_higher.
            log.info(fmt(C.GREEN,
                "\n  ✓  No upgrade available (some track(s) already above target)."))
            if not missing:
                log_fetch({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "album_id": album.get("id"),
                    "artist": (album.get("artist") or {}).get("name"),
                    "title": album.get("title"),
                    "result": "skipped_already_higher_quality",
                    "tracks_total": len(qobuz_tracks),
                })
                return {"result": "skipped_already_higher_quality",
                        "n_total": len(qobuz_tracks)}
            # Has missing tracks.
            log.info(fmt(C.YELLOW,
                f"     ⚠  {len(missing)} track(s) missing; filling them at "
                f"{target_label} would mix quality."))

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

        # cls == "all_equal": no message, today's behavior.

    # If caller asked for upgrade-only but the auto-upgrade branch above
    # didn't fire (quality classification didn't match upgrade criteria,
    # extras blocked it, etc.), bail rather than silently fall through to
    # gap-fill because the user explicitly asked not to fill missing tracks.
    if upgrade_only and not auto_upgrade_active:
        if not present:
            # A different fact than "no upgrade applies": the album isn't in
            # the library at all any more (moved, renamed, deleted since the
            # scan that produced this candidate).
            log.info(fmt(C.YELLOW,
                "  ⚠  None of this album's tracks are in your library any "
                "more; skipping."))
            return {"result": "upgrade_no_local_tracks",
                    "n_total": len(qobuz_tracks)}
        log.info(fmt(C.YELLOW,
            "  ⚠  Upgrade-only requested but no upgrade applies here; skipping."))
        return {"result": "upgrade_only_no_op", "n_total": len(qobuz_tracks)}

    reviewed_album_absent = album_dir is None and not treat_as_new
    source_premise = None
    if missing and not args.dry_run and album_dir is not None:
        premise_kind = (
            "upgrade"
            if auto_upgrade_active or upgrade_only or use_force
            else "gap-fill"
        )
        if expected_album_receipt is None:
            source_premise = candidate_premise.capture(
                premise_kind,
                album_dir,
            )
        else:
            source_premise = candidate_premise.canonical_premise({
                "version": candidate_premise.PREMISE_VERSION,
                "kind": premise_kind,
                "path": os.path.abspath(os.fspath(album_dir)),
                "receipt": expected_album_receipt,
            })
        if source_premise is None:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  The local album could not be sealed exactly. Refresh "
                "or rerun the command; nothing was changed.",
            ))
            return {"result": "stale_candidate"}
        expected_album_receipt = source_premise["receipt"]
        if expected_gap_fill_receipts is None:
            expected_gap_fill_receipts = (
                candidate_premise.gap_fill_receipts(source_premise)
            )

    if not already_confirmed:
        print_album_summary(album, missing, present, album_dir, use_force,
                            auto_upgrade=auto_upgrade_active,
                            existing_quality_label=upgrade_existing_label)

    if not missing:
        log.info(fmt(C.GREEN, "\n  ✓  Already complete. Nothing to download.\n"))
        log_fetch({
            "ts": datetime.now(timezone.utc).isoformat(),
            "album_id": album.get("id"),
            "artist": (album.get("artist") or {}).get("name"),
            "title": album.get("title"),
            "result": "already_complete",
            "tracks_total": len(qobuz_tracks),
            "tracks_downloaded": 0,
        })
        consolidation_interrupted = False
        if args.consolidate:
            try:
                consolidate_albums(album, args)
            except KeyboardInterrupt:
                consolidation_interrupted = True
                log.info(fmt(C.GRAY, "\n  Consolidation interrupted."))
        return {
            "result": "already_complete",
            "n_total": len(qobuz_tracks),
            "consolidation_interrupted": consolidation_interrupted,
        }

    # A dry run has already printed the plan above; stop before the download
    # confirm so we don't ask "proceed with downloading?" for a run that never
    # downloads.
    if args.dry_run:
        log.info(fmt(C.YELLOW, "\n  --dry-run: stopping here, nothing downloaded.\n"))
        return {"result": "dry_run", "n_missing": len(missing)}

    # Default NO: the missing-tracks list above is the user's chance to
    # decide; defaulting yes would mean every enter-press starts a download
    # they may not want. Press y to proceed.
    if not already_confirmed and not confirm(
            f"\n  Proceed with downloading {len(missing)} track(s)?",
            default_yes=False, auto_yes=args.yes):
        log.info(fmt(C.GRAY, "  Skipped."))
        return {"result": "user_skipped", "n_missing": len(missing)}

    expected_generation = getattr(token, "credential_generation", "")
    if expected_generation:
        token = api_client.authorize_bound_download(token)

    if source_premise is not None:
        current_premise = candidate_premise.capture(
            source_premise["kind"],
            source_premise["path"],
        )
        if current_premise != source_premise:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  The local files changed after this download was "
                "approved. Refresh or rerun the command; nothing was changed.",
            ))
            return {"result": "stale_candidate"}
    elif reviewed_album_absent:
        clear_scan_caches()
        if find_album_dir_filesystem(album) is not None:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  This album appeared locally after the download was "
                "approved. Refresh or rerun the command; nothing was changed.",
            ))
            return {"result": "stale_candidate"}

    # ── Pre-flight: staging dir state (BEFORE backup so sys.exit can't strand it)
    staging_preflight(args)

    if (
        (auto_upgrade_active or use_force)
        and album_dir is not None
        and album_dir.exists()
    ):
        replaced_beets_entries = capture_beets_album_entries(album_dir)
        if replaced_beets_entries is None:
            log.info(fmt(
                C.RED,
                "  ✗  Couldn't safely identify this album's Beets entries; "
                "refusing to replace its folder.",
            ))
            log_fetch({
                "ts": datetime.now(timezone.utc).isoformat(),
                "album_id": album.get("id"),
                "artist": (album.get("artist") or {}).get("name"),
                "title": album.get("title"),
                "result": "replacement_aborted_catalogue_failed",
            })
            return {"result": "replacement_aborted_catalogue_failed"}

    # ── Auto-upgrade: back up the existing folder before redownload ──────────
    # Same-filesystem move, so this is fast (rename, not copy). The backup
    # is restored if anything fails before beets import succeeds.
    if auto_upgrade_active and album_dir and album_dir.exists():
        upgrade_backup_path = (
            backup_album_dir(
                album_dir,
                expected_receipt=expected_album_receipt,
            )
            if expected_album_receipt is not None
            else backup_album_dir(album_dir)
        )
        if (
            upgrade_backup_path is not None
            and not upgrade_backup_path.complete
        ):
            _recover_incomplete_upgrade_backup(
                upgrade_backup_path,
                album_dir,
                operation="upgrade backup",
            )
            log_fetch({
                "ts": datetime.now(timezone.utc).isoformat(),
                "album_id": album.get("id"),
                "artist": (album.get("artist") or {}).get("name"),
                "title": album.get("title"),
                "result": "upgrade_aborted_backup_failed",
            })
            return {"result": "upgrade_aborted_backup_failed"}
        if (
            upgrade_backup_path is None
        ):
            log.info(fmt(C.RED,
                "  ✗  Could not back up the existing folder; refusing to "
                "wipe without a backup. Skipping this album."))
            log_fetch({
                "ts": datetime.now(timezone.utc).isoformat(),
                "album_id": album.get("id"),
                "artist": (album.get("artist") or {}).get("name"),
                "title": album.get("title"),
                "result": "upgrade_aborted_backup_failed",
            })
            return {"result": "upgrade_aborted_backup_failed"}
        log.info(fmt(C.GRAY,
            "  ⤷  Backed up existing folder (auto-restore on failure)"))

    # ── --force: NOW move the existing album dir aside (deferred from above) ──
    if use_force:
        force_outcome = force_cleanup_preflight(
            album,
            args,
            expected_album_receipt=expected_album_receipt,
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
            and upgrade_backup_path is None
        ):
            # The moved-aside folder is restored on failure / cleared on success
            # by the same finally block that handles the auto-upgrade backup.
            upgrade_backup_path = force_outcome

    # ── Download phase ───────────────────────────────────────────────────────
    # Pre-init so the backup-resolution finally block has sane defaults if a
    # rip raises AuthLost / OSError before the result is read back.
    n_ok = n_fail = n_lossy = 0
    failed_tracks, lossy_tracks, broken_tracks = [], [], []
    imported = False
    upgrade_unverified = False
    catalogue_unverified = False
    elapsed = 0.0
    download_phase_completed = False
    transient_lyric_sigs = []
    resampled_n = 0
    downsample_outcome = _pre_import_outcome_fields(([], 0))
    post_import_signatures = []
    staged_dirs_for_import = []
    download_result = {}
    quality_verdict = None
    _gap_fill_not_located = False  # gap-fill succeeded on paper but album not found on disk
    recovery_unverified = False
    early_result = None

    try:
        snapshot = snapshot_staging()
        vlog(f"staging snapshot: {len(snapshot)} files before download")

        try:
            run_album_download(
                album=album, missing=missing, present=present,
                existing=existing, album_dir=album_dir, snapshot=snapshot,
                quality=quality, upgrade_only=upgrade_only,
                result=download_result,
                expected_gap_fill_receipts=expected_gap_fill_receipts)
        except BaseException:
            retain_download_staging(download_result)
            raise
        n_ok = download_result["n_ok"]
        n_fail = download_result["n_fail"]
        n_lossy = download_result["n_lossy"]
        failed_tracks = download_result["failed_tracks"]
        lossy_tracks = download_result["lossy_tracks"]
        broken_tracks = download_result.get("broken_tracks", [])
        elapsed = download_result["elapsed"]

        # Cancelled mid-rip: the user hit stop, so the partial set must not be
        # imported.
        if is_cancel_requested():
            log.info(fmt(C.YELLOW,
                "\n  Cancelled; retaining the partial download for review. "
                "nothing imported."))
            retain_download_staging(download_result)
            early_result = {
                "result": "cancelled",
                "imported": False,
                "n_ok": n_ok,
                "n_lossy": n_lossy,
                "n_fail": n_fail,
            }
            return early_result

        # A wipe-replace (auto-upgrade or --force moved the original aside)
        # that came back partial gets rolled back to the original by the
        # finally below, so importing the partial first would only strand its
        # rows in beets once the backup is restored over it.
        if ((auto_upgrade_active or use_force) and n_ok > 0
                and (n_fail > 0 or n_lossy > 0) and not args.no_import):
            retained = retain_download_staging(
                download_result, label="incomplete-replacement")
            disposition = (
                "retaining it in staging recovery"
                if retained else
                "leaving its isolated staging run untouched for review"
            )
            log.info(fmt(C.YELLOW,
                f"\n  Re-download came back incomplete; {disposition} and "
                "keeping your original."))
            early_result = {
                "result": "partial",
                "imported": False,
                "n_ok": n_ok,
                "n_fail": n_fail,
                "n_lossy": n_lossy,
            }
            return early_result

        if n_ok > 0 and not args.no_import and not use_force:
            staged_dirs_for_import = validated_staged_album_dirs(
                download_result)
            staged_dirs = staged_dirs_for_import
            if staged_dirs:
                effective_tier = (
                    quality if quality is not None else cfg.STREAMRIP_QUALITY
                )

                def _redownload_at_max():
                    original_result = dict(download_result)
                    retry_result = {}

                    def _run_retry():
                        run_album_download(
                            album=album, missing=missing, present=present,
                            existing=existing, album_dir=album_dir,
                            snapshot=snapshot, quality=4,
                            upgrade_only=upgrade_only, result=retry_result,
                            expected_gap_fill_receipts=(
                                expected_gap_fill_receipts
                            ))

                    try:
                        fresh_dirs, retry_kept = redownload_with_staged_fallback(
                            staged_dirs,
                            run_retry=_run_retry,
                            collect_staged_dirs=lambda: (
                                validated_staged_album_dirs(retry_result)
                                if retry_result.get("n_ok", 0) > 0 else []),
                            collect_retry_files=lambda: download_staged_files(
                                retry_result),
                            retry_preserves_original=lambda: (
                                retry_preserves_track_coverage(
                                    original_result, retry_result)))
                    except Exception:
                        download_result.clear()
                        download_result.update(original_result)
                        raise
                    if retry_kept:
                        retire_empty_download_staging(original_result)
                        download_result.clear()
                        download_result.update(retry_result)
                    else:
                        retire_empty_download_staging(retry_result)
                        download_result.clear()
                        download_result.update(original_result)
                    return fresh_dirs

                quality_verdict = verify_and_recover(
                    album, staged_dirs,
                    redownload_at_max=_redownload_at_max,
                    effective_tier=effective_tier,
                    allow_retry=download_result.get("gap_fill_backup_path") is None)
                if quality_verdict["retried"]:
                    staged_dirs_for_import = (
                        quality_verdict.get("staged_dirs") or staged_dirs_for_import
                    )
                    n_ok = download_result.get("n_ok", 0)
                    n_fail = download_result.get("n_fail", 0)
                    n_lossy = download_result.get("n_lossy", 0)
                    failed_tracks = download_result.get(
                        "failed_tracks", failed_tracks)
                    lossy_tracks = download_result.get(
                        "lossy_tracks", lossy_tracks)
                    broken_tracks = download_result.get(
                        "broken_tracks", broken_tracks)
                    elapsed = download_result.get("elapsed", elapsed)
                    if (auto_upgrade_active and n_ok > 0
                            and (n_fail > 0 or n_lossy > 0)):
                        retained = retain_download_staging(
                            download_result, label="incomplete-replacement")
                        disposition = (
                            "retaining it in staging recovery"
                            if retained else
                            "leaving its isolated staging run untouched for review"
                        )
                        log.info(fmt(C.YELLOW,
                            "\n  Recovery re-download came back incomplete; "
                            f"{disposition} and keeping your original."))
                        early_result = {
                            "result": "partial",
                            "imported": False,
                            "n_ok": n_ok,
                            "n_fail": n_fail,
                            "n_lossy": n_lossy,
                        }
                        return early_result
                if quality_verdict["recovered"]:
                    log.info(fmt(C.CYAN,
                        "  ↻ recovered: re-fetched at the highest source after "
                        "a short first rip."))
                elif not quality_verdict["under"]:
                    log.info(fmt(C.GRAY,
                        "  Quality check: staged rip meets the selected source cap."))

        # ── Pre-import: downsample + lyrics on STAGING ──────────────────────────
        # Downsampling and lyric_fetch run on staging BEFORE beets imports.
        if n_ok > 0 and not args.no_import:
            if not staged_dirs_for_import:
                staged_dirs_for_import = validated_staged_album_dirs(
                    download_result)
            prepared = _pre_import_staging_hooks(args, staged_dirs_for_import)
            transient_lyric_sigs, resampled_n = prepared
            downsample_outcome = _pre_import_outcome_fields(prepared)
            post_import_signatures = track_signatures_for_album_dirs(
                staged_dirs_for_import)

        # ── Beets import ─────────────────────────────────────────────────────────
        imported = False
        if args.no_import:
            log.info(fmt(C.YELLOW, f"\n  --no-import: skipping beets. Files remain in {cfg.STAGING_DIR}/"))
        elif n_ok == 0:
            log.info(fmt(C.YELLOW, "\n  Skipping beets import: nothing succeeded."))
        else:
            log.info("")
            # A multi-disc cover sits in the album root, which beets' import
            # task never searches; left there it strands in staging. The queue
            # lanes do this in their pre-import hooks, which this path skips.
            for _staged in (staged_dirs_for_import or []):
                try:
                    relocate_disc_album_artwork(_staged)
                except Exception as _ae:
                    vlog(f"artwork relocation raised: {_ae}")
            # A brand-new album (no folder found on disk) lands in a fresh
            # directory, so it can't split an existing beets album into
            # duplicate rows, so skip the full-library de-dup scan for it.
            imported = beets_import_paths(
                consolidate=album_dir is not None,
                album_dirs=staged_dirs_for_import,
            )
            if (
                imported
                and "_staging_run" in download_result
                and not retire_download_staging_after_import(download_result)
            ):
                log.info(fmt(
                    C.RED,
                    "  ✗  The exact download run was not empty after beets; "
                    "the import was not accepted.",
                ))
                imported = False
            if not imported:
                retained = retain_download_staging(
                    download_result, label="beets-incomplete")
                if retained:
                    log.info(fmt(
                        C.YELLOW,
                        "  ⚠  Kept the exact residual download run in "
                        "private staging recovery.",
                    ))

        download_phase_completed = True

    finally:
        # Always resolve upgrade backup, including on exception.
        # ── Auto-upgrade backup resolution ───────────────────────────────────────
        upgrade_restored = False
        if upgrade_backup_path is not None:
            # Require zero failures AND zero lossy-deletes.
            upgrade_succeeded = (download_phase_completed
                                  and imported
                                  and n_ok > 0
                                  and n_fail == 0
                                  and n_lossy == 0)
            # An auto-upgrade wipes the only full copy, so it has to clear a
            # higher bar before the backup is deleted: the rebuilt folder must
            # be verifiably at least as complete as the original (same-or-more
            # tracks and playtime).
            upgrade_verified = upgrade_succeeded and (
                not auto_upgrade_active
                or _upgrade_replacement_verified(
                    album, album_dir, upgrade_backup_path))
            if upgrade_verified:
                # Carry non-audio companions (booklets, art, .cue/.log) from
                # the backup into the rebuilt album before deleting it. The
                # audio-only verification ignores them, so they'd be lost.
                carried = _carry_non_audio_from_backup(
                    album, album_dir, upgrade_backup_path)
                if carried is not None:
                    replacement_path, replacement_receipt = carried
                    validator = (
                        _upgrade_trees_verified
                        if auto_upgrade_active
                        else _intentional_replacement_verified
                    )
                    catalogue_retired = (
                        replaced_beets_entries is not None
                        and retire_replaced_beets_entries(
                            replaced_beets_entries,
                            replacement_path,
                            _replacement_audio_paths(
                                replacement_path, replacement_receipt
                            ),
                        )
                    )
                    if not catalogue_retired:
                        upgrade_unverified = True
                        catalogue_unverified = True
                        if not pin_unverified_upgrade_backup(
                                upgrade_backup_path,
                                "upgrade kept; the replaced Beets entries "
                                "could not be retired safely"):
                            warn_pin_failed(upgrade_backup_path)
                        log.info(fmt(C.YELLOW,
                            "  ⚠  Upgrade landed, but its Beets catalogue "
                            "couldn't be reconciled safely."))
                        log.info(fmt(C.GRAY,
                            f"     Backup remains at {upgrade_backup_path} "
                            "until you reconcile it."))
                    elif dispose_backup(
                        upgrade_backup_path,
                        replacement_path=replacement_path,
                        expected_replacement_receipt=replacement_receipt,
                        replacement_validator=validator,
                    ):
                        if auto_upgrade_active and not upgrade_only:
                            log.info(fmt(C.GRAY,
                                "  ✓  Upgrade complete; backup cleared."))
                    else:
                        upgrade_unverified = True
                        if not pin_unverified_upgrade_backup(
                                upgrade_backup_path,
                                "upgrade kept; final exact replacement proof "
                                "did not hold"):
                            warn_pin_failed(upgrade_backup_path)
                        log.info(fmt(C.YELLOW,
                            "  ⚠  Upgrade succeeded but the exact backup "
                            "couldn't be safely removed."))
                        log.info(fmt(C.GRAY,
                            f"     Backup remains at {upgrade_backup_path} "
                            "until you reconcile it."))
                else:
                    upgrade_unverified = True
                    if not pin_unverified_upgrade_backup(upgrade_backup_path):
                        warn_pin_failed(upgrade_backup_path)
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Upgrade landed, but the rebuilt album or its "
                        "companions couldn't be flushed safely; keeping the "
                        "backup."))
                    log.info(fmt(C.GRAY,
                        f"     Backup remains at {upgrade_backup_path}; keep it "
                        "until you've confirmed the rebuilt album is safe."))
            elif upgrade_succeeded and auto_upgrade_active:
                # Passed the decode/lossy gate but the rebuilt folder isn't
                # verifiably as complete as the original. Keep the only full
                # copy instead of deleting it.
                upgrade_unverified = True
                if not pin_unverified_upgrade_backup(upgrade_backup_path):
                    warn_pin_failed(upgrade_backup_path)
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Upgrade couldn't be verified as complete; "
                    "keeping your original."))
                log.info(fmt(C.GRAY,
                    f"     Original preserved at {upgrade_backup_path} "
                    f"(kept until you confirm the upgrade landed)."))
                log.info(fmt(C.WHITE,
                    f"     Restore: mv {upgrade_backup_path!s} {album_dir!s}"))
            elif download_phase_completed and args.no_import:
                upgrade_unverified = True
                if not pin_unverified_upgrade_backup(
                        upgrade_backup_path,
                        "upgrade backup kept; --no-import leaves the "
                        "replacement outside the verified library flow"):
                    warn_pin_failed(upgrade_backup_path)
                log.info(fmt(C.YELLOW,
                    f"\n  ⚠  --no-import set; cannot auto-verify upgrade. "
                    f"Backup kept at {upgrade_backup_path}."))
            else:
                # --force routes through the same backup-restore branch (the
                # forced re-download backed the original up the same way an
                # auto-upgrade would), so name what actually ran instead of
                # always saying "Upgrade".
                op_label = "Forced re-download" if args.force else "Upgrade"
                log.info(fmt(C.YELLOW,
                    f"\n  ⚠  {op_label} did not succeed (no successful import); "
                    "restoring backup …"))
                upgrade_restored = restore_upgrade_backup(upgrade_backup_path, album_dir)
                if upgrade_restored:
                    log.info(fmt(C.GREEN,
                        f"  ✓  Restored original folder to {album_dir}."))
                else:
                    upgrade_unverified = True
                    if not pin_unverified_upgrade_backup(
                            upgrade_backup_path,
                            "upgrade backup kept; automatic restore did not "
                            "complete"):
                        warn_pin_failed(upgrade_backup_path)
                    log.info(fmt(C.RED,
                        f"  ✗  Auto-restore failed. Original folder is at: "
                        f"{upgrade_backup_path}"))
                    log.info(fmt(C.WHITE,
                        f"     Manual restore: mv {upgrade_backup_path!s} {album_dir!s}"))

        # ── Gap-fill backup resolution ───────────────────────────────────────
        # run_album_download records this the moment it stashes present tracks,
        # so it's reachable here even when the rip raised before returning.
        gap_fill_backup_path = download_result.get("gap_fill_backup_path")
        if gap_fill_backup_path is not None and gap_fill_backup_path.exists():
            gap_fill_succeeded = (download_phase_completed
                                  and imported
                                  and n_ok > 0
                                  and n_fail == 0
                                  and n_lossy == 0)
            if gap_fill_succeeded:
                # Mirror the executor's "imported but folder not located"
                # guard: beets files by tags, so a re-rip whose canonical
                # folder differs from album_dir (renamed edition, unexpected
                # albumartist) lands elsewhere.
                _filled = (
                    find_album_dir_by_track_signatures(post_import_signatures)
                    if post_import_signatures else None
                )
                if _filled is None:
                    _filled = find_album_dir_filesystem(album)
                if _filled is None:
                    clear_scan_caches()
                    _filled = find_album_dir_filesystem(album)
                # NOT just "the folder has any audio": album_dir still
                # physically holds the EXTRAS (which are audio) after the
                # present tracks were moved to the backup, so a >0 check
                # passes even when the re-rip landed elsewhere / imported
                # nothing and the present tracks were never restored. The
                # backup (their only copy) would be deleted.
                _filled_ok = folder_holds_all_tracks(
                    _filled, qobuz_tracks, destructive=True)
                _filled_receipt = (
                    capture_album_source_receipt(_filled)
                    if _filled_ok else None
                )
                if _filled_ok and _filled_receipt is not None:
                    if not retire_backup_beets_entries(
                        gap_fill_backup_path,
                        _filled,
                        _filled_receipt,
                    ):
                        _gap_fill_not_located = True
                        recovery_unverified = True
                        catalogue_unverified = True
                        if not pin_unverified_upgrade_backup(
                                gap_fill_backup_path,
                                "gap-fill backup kept; the replaced Beets "
                                "entries could not be retired safely"):
                            warn_pin_failed(gap_fill_backup_path)
                        log.info(fmt(C.YELLOW,
                            "  ⚠  Gap-fill landed, but its Beets catalogue "
                            "couldn't be reconciled safely; keeping the exact "
                            "backup."))
                    elif not dispose_backup(
                        gap_fill_backup_path,
                        replacement_path=_filled,
                        expected_replacement_receipt=_filled_receipt,
                        replacement_validator=lambda replacement, _backup: (
                            folder_holds_all_tracks(
                                replacement, qobuz_tracks, destructive=True)
                        ),
                    ):
                        recovery_unverified = True
                        if not pin_unverified_upgrade_backup(
                                gap_fill_backup_path,
                                "gap-fill backup kept; final exact "
                                "replacement proof did not hold"):
                            warn_pin_failed(gap_fill_backup_path)
                        log.info(fmt(C.YELLOW,
                            "  ⚠  Gap-fill complete but the exact backup "
                            "couldn't be safely removed."))
                elif _filled_ok:
                    _gap_fill_not_located = True
                    recovery_unverified = True
                    if not pin_unverified_upgrade_backup(
                            gap_fill_backup_path,
                            "gap-fill backup kept; replacement not durable"):
                        warn_pin_failed(gap_fill_backup_path)
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Gap-fill landed, but the filled album couldn't "
                        "be flushed safely; keeping the backed-up tracks."))
                    log.info(fmt(C.GRAY,
                        f"     Backup remains at {gap_fill_backup_path}."))
                else:
                    _gap_fill_not_located = True
                    recovery_unverified = True
                    if not pin_unverified_upgrade_backup(
                            gap_fill_backup_path,
                            "gap-fill backup kept; the complete replacement "
                            "could not be located"):
                        warn_pin_failed(gap_fill_backup_path)
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Gap-fill imported but the filled album couldn't be "
                        f"located on disk; keeping backed-up tracks at "
                        f"{gap_fill_backup_path}."))
            elif download_phase_completed and args.no_import:
                # Gate on download_phase_completed like the upgrade branch above:
                # a crash MID-download (phase not completed) must fall through to
                # the restore branch, not keep a backup while the album is short.
                recovery_unverified = True
                if not pin_unverified_upgrade_backup(
                        gap_fill_backup_path,
                        "gap-fill backup kept; --no-import leaves the "
                        "replacement outside the verified library flow"):
                    warn_pin_failed(gap_fill_backup_path)
                log.info(fmt(C.YELLOW,
                    f"  ⚠  --no-import; gap-fill backup kept at "
                    f"{gap_fill_backup_path}."))
            else:
                log.info(fmt(C.YELLOW,
                    "  ⚠  Gap-fill did not succeed; restoring backed-up tracks…"))
                _n_back = restore_gap_fill_backup(gap_fill_backup_path, album_dir,
                                                  keep_larger_dst=False)
                # restore_gap_fill_backup preserves anything it couldn't
                # restore at the backup path (and only removes the backup on a
                # complete restore).
                if _n_back and not gap_fill_backup_path.exists():
                    log.info(fmt(C.GREEN,
                        f"  ✓  Restored {_n_back} track(s) to {album_dir}."))
                else:
                    recovery_unverified = True
                    if not pin_unverified_upgrade_backup(
                            gap_fill_backup_path,
                            "gap-fill backup kept; automatic restore was "
                            "partial or failed"):
                        warn_pin_failed(gap_fill_backup_path)
                    log.info(fmt(C.RED,
                        f"  ✗  Restored {_n_back} track(s); remaining originals "
                        f"preserved at {gap_fill_backup_path}; reconcile by hand."))

        # The return value was built before this recovery block. Update the
        # same result if an automatic restore failed.
        if early_result is not None:
            early_result.update(
                upgrade_unverified=upgrade_unverified,
                catalogue_unverified=catalogue_unverified,
                recovery_unverified=recovery_unverified,
            )

    # ── Consolidation ────────────────────────────────────────────────────────
    n_consolidated = 0
    consolidation_interrupted = False
    # treat_as_new keeps this download as its own edition; consolidation folds
    # editions together by deleting overlapping sibling tracks, so the two are
    # mutually exclusive. Never consolidate a deliberately separate edition.
    if args.consolidate and not treat_as_new:
        if imported:
            try:
                n_consolidated = consolidate_albums(album, args)
            except KeyboardInterrupt:
                consolidation_interrupted = True
                log.info(fmt(C.GRAY, "\n  Consolidation interrupted."))
        else:
            log.info(fmt(C.YELLOW,
                "\n  --consolidate requested but beets import didn't succeed; skipping."))

    # ── Post-import cleanup: folder layout and duplicate cover art ──────────
    if imported:
        strict_success = n_fail == 0 and n_lossy == 0
        post_dir_exact = False
        if getattr(args, "migrate_multi_artist", False) and strict_success:
            migration_source = (
                find_album_dir_by_track_signatures(post_import_signatures)
                if post_import_signatures else None
            )
            if migration_source is None:
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Multi-artist folder filing was skipped because "
                    "the exact imported album could not be located.",
                ))
                post_dir = None
            else:
                post_dir = prompt_and_migrate_multi_artist_folder(
                    album,
                    args,
                    authority=run_lock.current_lease(),
                    source_dir=migration_source,
                )
                post_dir_exact = post_dir is not None
        else:
            post_dir = None
        if post_dir is None:
            post_dir = (
                find_album_dir_by_track_signatures(post_import_signatures)
                if post_import_signatures else None
            )
            post_dir_exact = post_dir is not None
            if post_dir is None:
                post_dir = find_album_dir_filesystem(album)
        # A brand-new album can land in a folder the cached listing predates;
        # clear the cache and look once more before giving up, or art cleanup
        # and the lyric-retry queue silently no-op.
        if post_dir is None:
            clear_scan_caches()
            post_dir = find_album_dir_filesystem(album)
        if post_dir:
            if resampled_n > 0:
                mark_local_album_capped(post_dir, qobuz_album=album)
            split_artist = (album.get("artist") or {}).get("name") or ""
            if (
                post_dir_exact
                and _is_split_album_merge(album_dir, post_dir, split_artist)
            ):
                try:
                    relocation = post_import_relocation.relocate_post_import_album(
                        album_dir,
                        post_dir,
                        kind=RelocationKind.SPLIT_GAP_FILL,
                        authority=run_lock.current_lease(),
                    )
                except PostImportRelocationAttention:
                    raise
                except (PostImportRelocationUnavailable, OSError, ValueError) as exc:
                    log.info(fmt(
                        C.YELLOW,
                        "  ⚠  Beets split this album across two artist folders; "
                        "the safe reunion was refused and both copies were kept.",
                    ))
                    vlog(f"split-folder reunion refused: {exc}")
                else:
                    if relocation.changed:
                        message = (
                            f"  ✓  Reunited {relocation.published_files} existing "
                            f"file(s) in the primary-artist folder."
                        )
                        if relocation.reason:
                            message += f" {relocation.reason.capitalize()}."
                        log.info(fmt(C.GREEN, message))
                    else:
                        log.info(fmt(
                            C.YELLOW,
                            "  ⚠  Split-folder detected, but every source name "
                            "already exists at the destination; both were kept.",
                        ))
            # Resolve transient-lyric signatures captured pre-beets
            if transient_lyric_sigs:
                resolved = _resolve_signatures_to_paths(
                    transient_lyric_sigs, [post_dir])
                if resolved:
                    if _record_post_import_lyric_retry(resolved):
                        vlog(f"lyric retry: queued {len(resolved)} "
                             f"post-import path(s) for next-launch retry")
            # Materialise .lrc sidecars next to the final renamed files
            # (no-op unless LYRICS_FORMAT is sidecar/both).
            try:
                write_post_import_sidecars([post_dir, album_dir])
            except Exception as _e_sc:
                vlog(f"post-import sidecar write raised: {_e_sc}")
    elif transient_lyric_sigs:
        # Import didn't succeed (beets failed, silent skip, or n_ok==0): the
        # files are still in STAGING_DIR.
        resolved = _resolve_signatures_to_paths(
            transient_lyric_sigs, [cfg.STAGING_DIR])
        if resolved:
            if _record_post_import_lyric_retry(resolved):
                vlog(f"lyric retry: import unsuccessful; queued {len(resolved)} "
                     f"staging path(s) for next-launch retry")

    # ── Summary ──────────────────────────────────────────────────────────────
    n_retryable, n_truly_lossy = incomplete_track_counts(download_result)
    downsample_attention = bool(
        downsample_outcome["downsample_errors"]
        or downsample_outcome["downsample_flush_warnings"]
        or downsample_outcome["downsample_cancelled"]
    )
    _broken = set(broken_tracks)
    truly_lossy = [t for t in lossy_tracks if t not in _broken]
    if (
        already_confirmed
        and not n_fail
        and not n_lossy
        and imported
        and not _gap_fill_not_located
        and not recovery_unverified
        and not downsample_attention
        and not consolidation_interrupted
    ):
        if auto_upgrade_active and not upgrade_unverified:
            log.info(fmt(C.MAGENTA + C.BOLD,
                f"  ↑ upgraded · {n_ok} track(s) · {int(elapsed)}s · imported"))
        elif not auto_upgrade_active and not upgrade_unverified:
            log.info(fmt(C.GREEN,
                f"  ✓ {n_ok} downloaded · {int(elapsed)}s · imported"))
        # An unverified replacement already printed the backup warning above;
        # don't also claim a clean download or upgrade here.
    else:
        section("Result")
        log.info("")
        log.info(f"  {fmt(C.GREEN if n_ok else C.GRAY,    '✓ downloaded:')}    {n_ok}")
        if n_truly_lossy:
            log.info(f"  {fmt(C.YELLOW, '⚠ lossy on Qobuz:')} {n_truly_lossy}")
        if broken_tracks:
            log.info(f"  {fmt(C.YELLOW, '⚠ incomplete:')}    {len(broken_tracks)}")
        if n_fail:
            log.info(f"  {fmt(C.RED, '✗ failed:')}        {n_fail}")
        if downsample_outcome["downsample_errors"]:
            log.info(
                f"  {fmt(C.RED, '✗ downsample failed:')} "
                f"{downsample_outcome['downsample_errors']}"
            )
        if downsample_outcome["downsample_flush_warnings"]:
            log.info(
                f"  {fmt(C.YELLOW, '⚠ flush warning:')}  "
                f"{downsample_outcome['downsample_flush_warnings']}"
            )
        if downsample_outcome["downsample_cancelled"]:
            log.info(f"  {fmt(C.YELLOW, '⚠ downsample:')}     stopped early")
        log.info(f"  {fmt(C.GRAY, '  runtime:')}        {int(elapsed)}s")
        log.info(f"  {fmt(C.GRAY, '  beets:')}          {'imported' if imported else 'skipped/failed'}")
        if args.consolidate:
            if consolidation_interrupted:
                log.info(f"  {fmt(C.YELLOW, '⚠ consolidated:')} stopped early")
            else:
                log.info(f"  {fmt(C.GRAY, '  consolidated:')}   {plural(n_consolidated, 'sibling track')} removed")
        if failed_tracks:
            log.info(fmt(C.RED, "\n  failed tracks:"))
            for t in failed_tracks[:10]:
                log.info(f"     {truncate(t, 60)}")
        if truly_lossy:
            log.info(fmt(C.YELLOW,
                "\n  only available lossy on Qobuz (would need another source):"))
            for t in truly_lossy[:10]:
                log.info(f"     {truncate(t, 60)}")
        if broken_tracks:
            log.info(fmt(C.YELLOW,
                "\n  downloaded incomplete and discarded (a re-run usually fixes these):"))
            for t in broken_tracks[:10]:
                log.info(f"     {truncate(t, 60)}")
        log.info("")

    if n_ok and (
        n_retryable or n_truly_lossy or downsample_attention
        or upgrade_unverified
        or catalogue_unverified
        or recovery_unverified
        or consolidation_interrupted
        or (
            quality_verdict
            and quality_verdict.get("under")
            and not quality_verdict.get("recovered")
        )
    ):
        result_status = "partial"
    elif n_ok and not imported and not args.no_import:
        # Tracks ripped but didn't make it into the library (a beets failure, or
        # an upgrade/--force the finally block rolled back to the original), the
        # library is unchanged, so don't log this as a completed "downloaded".
        result_status = "not_imported"
    elif n_ok:
        result_status = "downloaded"
    elif n_retryable or n_truly_lossy:
        result_status = "failed"
    else:
        result_status = "nothing_landed"

    log_fetch({
        "ts": datetime.now(timezone.utc).isoformat(),
        "album_id": album.get("id"),
        "artist": (album.get("artist") or {}).get("name"),
        "title": album.get("title"),
        "result": result_status,
        "tracks_total": len(qobuz_tracks),
        "tracks_already_present": len(present),
        "tracks_attempted": len(missing),
        "tracks_downloaded": n_ok,
        "tracks_lossy_deleted": n_lossy,
        "tracks_failed": n_fail,
        "failed_titles": failed_tracks,
        "lossy_titles": lossy_tracks,
        "broken_titles": broken_tracks,
        **downsample_outcome,
        "imported": imported,
        "force": bool(use_force),
        "auto_upgrade": bool(auto_upgrade_active),
        "upgrade_backup_path": str(upgrade_backup_path) if upgrade_backup_path else None,
        "upgrade_restored": upgrade_restored,
        "catalogue_unverified": catalogue_unverified,
        "recovery_unverified": recovery_unverified,
        "consolidated": bool(
            args.consolidate and imported and not consolidation_interrupted),
        "consolidation_interrupted": consolidation_interrupted,
        "consolidated_tracks_removed": n_consolidated,
        "elapsed_s": int(elapsed),
    })

    # A clean import can still hide a truncated track: the download drops
    # files that won't decode, but one cut at a frame boundary with its FLAC
    # header rewritten short decodes fine and only shows against the real
    # Qobuz length.
    if (result_status in ("downloaded", "partial") and token
            and not getattr(args, "no_import", False)):
        final_dir = find_album_dir_filesystem(album)
        if final_dir:
            warn_if_download_truncated(final_dir, token, album.get("title"))

    return {
        "result": result_status,
        "n_ok": n_ok, "n_fail": n_fail, "n_lossy": n_lossy,
        "n_broken": max(n_retryable - n_fail, 0),
        "n_lossy_only": n_truly_lossy,
        "failed_tracks": failed_tracks,
        "lossy_tracks": truly_lossy,
        "broken_tracks": broken_tracks,
        "imported": imported,
        "upgrade_unverified": upgrade_unverified,
        "catalogue_unverified": catalogue_unverified,
        "recovery_unverified": recovery_unverified,
        "auto_upgrade": bool(auto_upgrade_active),
        "quality_verdict": quality_verdict,
        "consolidation_interrupted": consolidation_interrupted,
        "consolidated_tracks_removed": n_consolidated,
        **downsample_outcome,
    }
