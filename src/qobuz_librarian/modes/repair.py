"""Album repair mode: ISRC-anchored refill of truncated FLACs."""
import contextlib
import json
import os
import stat
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian import post_job_hook
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api.auth import (
    AuthLost,
    DownloadsRefused,
    QobuzError,
    QobuzUnavailable,
)
from qobuz_librarian.api.search import get_album
from qobuz_librarian.download_result import cd_quality_note
from qobuz_librarian.integrations.beets import (
    _highest_album_id,
    beets_entry_ids,
    forget_replaced_beets_entries,
    settle_tracks_added_to_album,
)
from qobuz_librarian.library import candidate_premise
from qobuz_librarian.library.backup import (
    BackupResult,
    _normalise_gap_fill_expected_receipts,
    backup_gap_fill_files,
    backup_payload,
    files_replaced_in_album,
    first_symlink,
    pin_unverified_upgrade_backup,
    restore_gap_fill_backup,
    retire_verified_repair_backup,
    warn_pin_failed,
)
from qobuz_librarian.library.catalog import _norm_isrc, find_qobuz_album_for_dir
from qobuz_librarian.library.discovery import resolve_artist_dir
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
    read_album_dir,
)
from qobuz_librarian.quality.tiers import tier_for
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.executor import (
    _execute_download_queue,
    _refresh_review_state_after_downloads,
)
from qobuz_librarian.repair_log import append_repair_log, scan_dir_for_isrc_repairs
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.errors import (
    EXIT_AUTH,
    EXIT_GENERAL,
    EXIT_INTERRUPT,
    auth_lost_msg,
    die,
    plural,
    unreadable_artist,
)
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.sentinels import NO_ANSWER


@dataclass(frozen=True)
class RepairRecovery:
    """The exact backup that must remain reachable until Repair settles it."""

    backup: BackupResult
    album_dir: Path
    stage: str
    reason: str
    retained: bool = True

    def as_record(self):
        receipt = self.backup.receipt
        if receipt is not None:
            receipt = json.loads(json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ))
        return {
            "version": 1,
            "kind": "repair-backup",
            "status": "retained" if self.retained else "resolved",
            "location": str(self.backup.path),
            "album_dir": str(Path(self.album_dir)),
            "stage": str(self.stage),
            "reason": str(self.reason),
            "complete": self.backup.complete is True,
            "requested": int(self.backup.requested),
            "backed_up": int(self.backup.backed_up),
            "receipt": receipt,
        }


class RepairRecoveryRequired(Exception):
    """Carry an exact retained backup across an exceptional Repair exit."""

    def __init__(self, recovery, cause):
        self.recovery = recovery
        self.cause = cause
        super().__init__(str(cause) or cause.__class__.__name__)


def _format_mmss(secs):
    """Format a duration in seconds as m:ss (e.g. 163.4 → '2:43')."""
    s = max(0, int(round(float(secs or 0))))
    return f"{s // 60}:{s % 60:02d}"


try:
    from mutagen.flac import FLAC as _FLAC
except Exception:
    _FLAC = None


def _snapshot_flac_metadata(path):
    """Full Vorbis-comment + embedded-picture snapshot of a FLAC, or None when
    it can't be read (mutagen missing, not a FLAC, unreadable)."""
    if _FLAC is None or not path:
        return None
    try:
        f = _FLAC(path)
    except Exception:
        return None
    return {
        "tags": list(f.tags or ()),
        "pictures": list(f.pictures),
    }


def _restore_flac_metadata(path, snap):
    """Replace a refilled FLAC's tags + embedded art with the snapshot from the
    truncated original it replaces, so only the audio changed, album, track
    number, title and art stay exactly as the user had them. Returns True on a
    successful write."""
    if _FLAC is None or not snap:
        return False
    try:
        f = _FLAC(path)
    except Exception:
        return False
    if f.tags is None:
        f.add_tags()
    else:
        f.tags.clear()
    f.tags.extend(snap["tags"])
    # Only swap embedded art when the original actually had some. A truncated
    # original downloaded without art (or with less) must not strip the
    # freshly-downloaded refill's Qobuz cover, that would downgrade the result.
    pics_ok = True
    if snap["pictures"]:
        f.clear_pictures()
        for pic in snap["pictures"]:
            try:
                f.add_picture(pic)
            except Exception:
                # Keep going, losing one malformed picture mustn't abort the
                # tag carry, but report the restore incomplete so the backup
                # (which still holds the original art) isn't deleted on it.
                pics_ok = False
    try:
        f.save()
    except Exception:
        return False
    return pics_ok


def _backup_source_by_isrc(verified_truncated, album_dir, backup_path):
    """Map each truncated track's ISRC to where its originals now live in the
    backup dir, so the refill can inherit the original's tags + embedded art
    by reading from disk at retag time. backup_gap_fill_files preserves each
    file's path relative to album_dir, so the backup copy is at the same
    relative spot.
    """
    out = {}
    for b in verified_truncated:
        isrc = _norm_isrc(b.get("isrc"))
        src = b.get("path") or ""
        if not isrc or not src:
            continue
        try:
            rel = Path(src).relative_to(album_dir)
        except ValueError:
            rel = Path(Path(src).name)
        cand = backup_path / rel
        if cand.exists():
            out.setdefault(isrc, []).append(cand)
    for paths in out.values():
        paths.sort()
    return out


def _refill_places(verified_truncated, album_dir):
    """Each damaged track's path in ``album_dir`` without its extension, by
    ISRC, so its refill takes its place. An ISRC two of them share, or one
    that is not letters and digits, is left out."""
    places = {}
    for b in verified_truncated:
        isrc = _norm_isrc(b.get("isrc"))
        try:
            rel = Path(b.get("path") or "").relative_to(album_dir)
        except ValueError:
            rel = None
        places[isrc] = (
            None if isrc in places or rel is None
            or not (isrc.isascii() and isrc.isalnum())
            else rel.with_suffix("").as_posix())
    return {isrc: place for isrc, place in places.items() if place}


def _retag_refills_in_staging(staged_dirs, source_by_isrc):
    """Carry the truncated originals' tags + art onto the matching refills (by
    ISRC) while they're still in staging, before beets files them. A
    recording that also appears on a compilation is downloaded by its track ID
    (so the audio is the right recording) but tagged by Qobuz for whatever
    album it files that ISRC under; without this the refill would import as
    the compilation, contradicting the folder it lives in.
    """
    if _FLAC is None:
        return set(source_by_isrc)
    if not source_by_isrc:
        return set()
    failed = set()
    remaining = {isrc: list(paths) for isrc, paths in source_by_isrc.items()}
    for d in staged_dirs:
        for fp in sorted(Path(d).rglob("*.flac")):
            try:
                isrc = _norm_isrc((_FLAC(fp).get("isrc") or [""])[0])
            except Exception:
                continue
            srcs = remaining.get(isrc)
            if not srcs:
                continue
            src = srcs.pop(0)
            snap = _snapshot_flac_metadata(src)
            if snap and _restore_flac_metadata(fp, snap):
                log.info(fmt(C.GRAY,
                    f"  ⤷  Kept original tags on refilled "
                    f"{truncate(fp.name, 40)}"))
            else:
                failed.add(isrc)
    failed.update(isrc for isrc, srcs in remaining.items() if srcs)
    return failed


def _make_retag_callback(retag_sources, retag_failed):
    """The pre-import retag hook the executor calls on the staged refills."""
    def _retag_callback(staged_dirs):
        retag_failed.update(retag_sources)
        done = _retag_refills_in_staging(staged_dirs, retag_sources)
        retag_failed.intersection_update(done)
    return _retag_callback


def _resolve_parent_album(album_dir, artist_name, verified_truncated,
                          wanted_isrcs, token):
    """Pick the Qobuz album that should drive where the refill is filed. Qobuz
    files a recording under whichever album it returns first for the ISRC,
    often a compilation the track also appears on, so the most-common ISRC
    album can be the wrong edition.
    """
    try:
        match = find_qobuz_album_for_dir(
            album_dir, artist_name, token,
            prefer_hires=cfg.PREFER_HIRES, target_dir=album_dir)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        match = None
    if match and wanted_isrcs:
        have = {_norm_isrc(t.get("isrc"))
                for t in (match.get("tracks") or {}).get("items") or []}
        if wanted_isrcs.issubset(have):
            return match

    parent_ids = []
    for b in verified_truncated:
        aid = (b["qobuz_track"].get("album") or {}).get("id")
        if aid:
            parent_ids.append(aid)
    if parent_ids:
        most_common_aid = Counter(parent_ids).most_common(1)[0][0]
        try:
            album = get_album(most_common_aid, token)
            if album:
                return album
        except QobuzError as e:
            log.info(fmt(C.GRAY,
                f"  (parent-album lookup failed: {e}; using the folder name)"))

    return {
        "id": "repair",
        "title": album_dir.name,
        "artist": {"name": artist_name},
        "tracks": {"items": []},
    }


def _repair_receipt_parts(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise OSError("repair import receipt has an invalid path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise OSError("repair import receipt has an invalid path")
    return tuple(relative.parts)


def _audio_files(album_dir):
    """The album's audio files by path relative to it, or None if it can't
    be read."""
    try:
        return {
            path.relative_to(album_dir).as_posix()
            for path in Path(album_dir).rglob("*")
            if path.suffix.lower() in cfg.AUDIO_EXTS and path.is_file()}
    except OSError:
        return None


def _album_identity(album_dir):
    """The album folder's device and inode, or None once it is gone."""
    try:
        st = os.lstat(album_dir)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISDIR(st.st_mode) else None


def _refills_present_in(album_dir, wanted_counts, baseline_counts):
    """True once each wanted ISRC has its refills back ON TOP of the files
    that already carried it."""
    if not wanted_counts:
        return True
    if baseline_counts is None:
        return False
    present = Counter(_norm_isrc(et.get("isrc")) for et in read_album_dir(album_dir))
    return all(present.get(isrc, 0) >= baseline_counts.get(isrc, 0) + n
               for isrc, n in wanted_counts.items())


def _refills_intact(album_dir, wanted_counts, token, baseline_counts):
    """True only when none of the refilled ISRCs is still flagged truncated by
    a fresh duration scan. A presence check can't tell a complete refill from a
    short-but-decodable one, the exact failure repair exists to fix, so the
    rebuilt folder is re-verified against the same gate before the originals'
    backup is trusted as redundant. Caller guarantees wanted_counts is
    non-empty; AuthLost / QobuzUnavailable propagate so a transient outage
    can't read as "still truncated"."""
    if baseline_counts is None:
        return False
    try:
        scan = scan_dir_for_isrc_repairs(
            album_dir, token, deep=True, only_isrcs=set(wanted_counts))
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        return False
    # Require POSITIVE re-verification: every wanted ISRC must have re-matched
    # a Qobuz recording AND passed the truncation gate. Checking only "not
    # flagged truncated" was unsafe, a wanted ISRC whose post-repair lookup
    # transiently returned nothing (4xx, index lag, strict-equality miss)
    # lands in isrc_no_match, NOT verified_truncated, so it would read as
    # intact and the only good-enough original's backup would be deleted while
    # the refill is still short.
    verified_ok = Counter()
    for isrc, n in dict(scan.get("verified_ok_isrcs") or {}).items():
        verified_ok[_norm_isrc(isrc)] += n
    return all(verified_ok.get(isrc, 0) >= baseline_counts.get(isrc, 0) + n
               for isrc, n in wanted_counts.items())


def _prompt_library_album_for_repair(args, token):
    """Library-scoped picker for repair mode. Returns (artist_dir, album_dir)."""
    print()
    log.info(fmt(C.GRAY, "  Tip: '?' lists artists; '*' scans your whole library."))
    artist_dir = None
    while artist_dir is None:
        r = ask("  Artist (blank/q to return, '*' for whole library): ",
                lower=False)
        if r is None:
            return NO_ANSWER, None
        if not r or r.lower() in ("q", "quit", "exit"):
            return None, None
        if r in ("*", "all") or r.lower() == "library":
            # Sentinel: caller sweeps every album under MUSIC_ROOT.
            return "__ALL__", None
        if r == "?":
            artists = list_library_artists(
                on_artist_error=lambda path, error: log.warning(
                    unreadable_artist(path.name, error)))
            if not artists:
                log.info(fmt(C.YELLOW, "  No artist directories found."))
                continue
            log.info(fmt(C.GRAY, f"  {plural(len(artists), 'artist')} in library:"))
            for d in artists[:50]:
                log.info(fmt(C.GRAY, f"    • {d.name}"))
            if len(artists) > 50:
                log.info(fmt(C.GRAY, f"    ... and {len(artists) - 50} more"))
            continue
        artist_dir = resolve_artist_dir(r)
        if artist_dir is None:
            log.info(fmt(C.YELLOW,
                f"  ⚠  No artist matches {r!r} (try '?' for the list)."))

    albums = list_artist_album_dirs(artist_dir)
    if not albums:
        log.info(fmt(C.YELLOW,
            f"  ⚠  No album folders under {artist_dir.name}."))
        return None, None

    log.info(fmt(C.BOLD + C.CYAN,
        f"\n  Albums by {artist_dir.name} ({len(albums)}):"))
    for i, d in enumerate(albums, 1):
        log.info(fmt(C.WHITE, f"   {i:>3}.  {truncate(d.name, 72)}"))

    while True:
        r = ask(f"  Pick album (1-{len(albums)}, q to cancel): ", lower=False)
        if r is None:
            return NO_ANSWER, None
        if not r or r.lower() in ("q", "quit", "exit"):
            return None, None
        try:
            idx = int(r)
        except ValueError:
            log.info(fmt(C.GRAY, "  Enter a number, or q."))
            continue
        if not 1 <= idx <= len(albums):
            log.info(fmt(C.GRAY,
                f"  Out of range, pick 1-{len(albums)}."))
            continue
        album_dir = albums[idx - 1]
        break

    return artist_dir, album_dir


def _verified_repair_source_receipts(verified_truncated, album_dir):
    """Require the exact album-relative files sealed by the repair scan."""
    album = Path(os.path.abspath(os.fsdecode(os.fspath(album_dir))))
    receipts = {}
    for entry in verified_truncated:
        try:
            source = Path(os.path.abspath(
                os.fsdecode(os.fspath(entry["path"]))))
            sealed = entry["source_receipt"]
            relative = sealed["relative"]
            parts = _repair_receipt_parts(relative)
            file_receipt = sealed["file"]
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise OSError(
                "repair scan has no exact source receipt; rescan the album"
            ) from exc
        if source != album.joinpath(*parts) or relative in receipts:
            raise OSError(
                "repair scan source receipt does not match the selected album"
            )
        receipts[relative] = file_receipt
    if len(receipts) != len(verified_truncated) or not receipts:
        raise OSError("repair scan source receipts are incomplete")
    return _normalise_gap_fill_expected_receipts(receipts)


def repair_album_dir(album_dir, verified_truncated, artist_name, args, token,
                     *, recovery_checkpoint=None):
    """Back up truncated originals, ISRC-refill them, resolve the backup.

    Non-interactive and self-contained, the CLI repair mode and the web
    Repair flow both call this so there is one implementation of the
    risky part. Forces no-upgrade for the run so a quality delta can never
    escalate a surgical repair into a full wipe-and-replace. Auth lost
    before the backup is taken aborts with the originals untouched;
    exceptional exits after backup carry the exact retained recovery. Returns
    {"n_ok", "n_fail", "backup"}. ``backup`` is the exact carried
    ``BackupResult`` while recovery data remains, never just its pathname.
    """
    saved_no_upgrade = getattr(args, "no_upgrade", False)
    retained_recovery = None

    def checkpoint_recovery(backup, stage, reason, *, required=False):
        nonlocal retained_recovery
        recovery = RepairRecovery(
            backup=backup,
            album_dir=Path(album_dir),
            stage=stage,
            reason=reason,
        )
        retained_recovery = recovery
        if recovery_checkpoint is None:
            return recovery, True
        try:
            persisted = recovery_checkpoint(recovery) is True
        except Exception as exc:
            persisted = False
            log.info(fmt(C.RED,
                f"  ✗  Couldn't save the Repair recovery record ({exc})."))
        if required and not persisted:
            log.info(fmt(C.RED,
                "  ✗  Repair stopped before downloading replacements "
                "because its recovery record could not be saved."))
        return recovery, persisted

    def resolve_recovery(backup, reason):
        nonlocal retained_recovery
        recovery = RepairRecovery(
            backup=backup,
            album_dir=Path(album_dir),
            stage="resolved",
            reason=reason,
            retained=False,
        )
        retained_recovery = None
        if recovery_checkpoint is not None:
            try:
                recovery_checkpoint(recovery)
            except Exception as exc:
                log.info(fmt(C.RED,
                    f"  ✗  Couldn't save the Repair recovery record ({exc})."))

    def recovery_result():
        return retained_recovery.backup if retained_recovery is not None else None

    def interrupt_with_recovery(cause, stage, reason):
        recovery, _ = checkpoint_recovery(
            backup_path, stage, reason)
        raise RepairRecoveryRequired(recovery, cause) from cause

    args.no_upgrade = True
    try:
        try:
            source_receipts = _verified_repair_source_receipts(
                verified_truncated, album_dir)
        except OSError as exc:
            log.info(fmt(C.RED,
                f"  ✗  Repair needs a fresh exact scan ({exc}); "
                "nothing was changed."))
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": None,
            }

        # Everything Qobuz-side happens up front, while the truncated
        # originals are still in place. If the parent-album lookup hits a
        # transient outage the repair aborts cleanly with nothing moved,
        # rather than stranding the only copies in the backup dir.
        wanted_isrcs = {_norm_isrc(b.get("isrc")) for b in verified_truncated}
        wanted_isrcs.discard("")
        # Per-ISRC counts of the originals going to backup, the presence gate
        # needs the multiset, not just the set, so two same-ISRC truncated files
        # (a .1.flac collision pair, or the same recording on two discs) aren't
        # treated as repaired when only one refill lands.
        wanted_counts = Counter(_norm_isrc(b.get("isrc")) for b in verified_truncated)
        wanted_counts.pop("", None)

        album = _resolve_parent_album(album_dir, artist_name,
                                      verified_truncated, wanted_isrcs, token)

        qobuz_tracks_full = (album.get("tracks") or {}).get("items") or []
        missing_tracks = [b["qobuz_track"] for b in verified_truncated]

        present_synth = [{} for _ in range(max(
            0, len(qobuz_tracks_full) - len(missing_tracks)))]

        # The refills are asked for at the album's own quality rather than
        # the download setting, so the album does not end up mixed. Qobuz
        # sends the best it has up to that. An album that can't be read
        # uses the setting.
        held = read_album_dir(album_dir, walk_errors=[])
        quality = tier_for(max((t.get("bits") or 0 for t in held), default=0),
                           max((t.get("sample_rate") or 0 for t in held),
                               default=0))

        # force_track_by_track: repair must replace exactly the truncated
        # tracks, never re-rip the whole album URL. The flag makes this
        # un-bypassable regardless of the executor's missing/total ratio.
        qi = _build_queue_item(
            album=album,
            album_dir=album_dir,
            label=f"{artist_name} - {album_dir.name}  [repair]",
            missing=missing_tracks,
            present=present_synth,
            upgrade_only=False,
            auto_upgrade=False,
            quality=quality,
            force_track_by_track=True,
        )
        qi["_capture_import_ownership"] = True
        qi["refill_places"] = _refill_places(verified_truncated, album_dir)

        # The album folder's identity before the first backup/download
        # mutation, so a same-name replacement can't pass for it later.
        album_identity = _album_identity(album_dir)
        if album_identity is None:
            log.info(fmt(C.RED,
                "  ✗  The selected album could not be held safely; "
                "nothing was changed."))
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": None,
            }

        expected_generation = getattr(token, "credential_generation", "")
        if expected_generation:
            token = api_client.authorize_bound_download(token)

        link = first_symlink(album_dir)
        if link:
            log.info(fmt(C.RED,
                f"  ✗  {link} is a symbolic link, which Repair can't check or "
                "back up safely. Replace it with a copy of its file, then "
                "try again; nothing was changed."))
            return {"n_ok": 0, "n_fail": len(verified_truncated), "backup": None}
        broken_paths = [b["path"] for b in verified_truncated]
        replaced_entries = beets_entry_ids(broken_paths)
        backup_path = backup_gap_fill_files(
            broken_paths,
            album_dir,
            expected_receipts=source_receipts,
            kind="repair",
        )
        if backup_path is None:
            if all(os.path.lexists(path) for path in broken_paths):
                log.info(fmt(C.RED,
                    "  ✗  Backup could not start, aborting repair without "
                    "changing the selected files."))
            else:
                log.info(fmt(C.RED,
                    "  ✗  The backup stopped part way and not every damaged "
                    "file could be put back. No replacement was downloaded; "
                    "the missing files are in the backup listed under "
                    "Settings, Diagnostics."))
            return {"n_ok": 0, "n_fail": len(verified_truncated), "backup": None}
        _initial_recovery, recovery_saved = checkpoint_recovery(
            backup_path,
            "backup",
            "Original files are held while replacement tracks are downloaded.",
            required=True,
        )
        if not recovery_saved:
            restored = 0
            if _album_identity(album_dir) == album_identity:
                try:
                    restored = restore_gap_fill_backup(backup_path, album_dir)
                except BaseException as exc:
                    recovery, _ = checkpoint_recovery(
                        backup_path,
                        "restore",
                        "Repair could not save its recovery record and "
                        "automatic restoration was interrupted.",
                    )
                    raise RepairRecoveryRequired(recovery, exc) from exc
            if (restored == backup_path.backed_up
                    and restored > 0 and not backup_path.exists()):
                resolve_recovery(
                    backup_path,
                    "Every moved original was restored after recovery "
                    "persistence failed.",
                )
                return {
                    "n_ok": 0,
                    "n_fail": len(verified_truncated),
                    "imported": False,
                    "backup": None,
                }
            checkpoint_recovery(
                backup_path,
                "backup",
                "Repair could not save its recovery record and automatic "
                "restore did not complete.",
            )
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": recovery_result(),
            }

        def pin_repair_recovery(note):
            if (backup_path.exists()
                    and not pin_unverified_upgrade_backup(backup_path, note)):
                warn_pin_failed(backup_path)

        def settle_beets_after_stop():
            # The import step settles beets right after the refills land; a
            # stop there would leave each refilled track listed twice.
            with contextlib.suppress(KeyboardInterrupt):
                settle_tracks_added_to_album(album_dir, last_old_id)

        if _album_identity(album_dir) != album_identity:
            pin_repair_recovery(
                "repair backup kept, the selected album changed after backup")
            checkpoint_recovery(
                backup_path,
                "backup",
                "The selected album changed after its originals were backed up.",
            )
            log.info(fmt(C.RED,
                "  ✗  The selected album changed during backup; "
                f"the originals are preserved at:\n     {backup_path}"))
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": recovery_result(),
            }
        post_backup_premise = candidate_premise.capture(
            "repair",
            album_dir,
        )
        if post_backup_premise is None:
            pin_repair_recovery(
                "repair backup kept, the remaining album could not be checked"
            )
            checkpoint_recovery(
                backup_path,
                "backup",
                "The remaining album could not be checked before its refill.",
            )
            log.info(fmt(
                C.RED,
                "  ✗  The remaining album could not be verified after its "
                "damaged files were backed up. No replacement download was "
                f"started; the originals are preserved at:\n     {backup_path}",
            ))
            return {
                "n_ok": 0,
                "n_fail": len(verified_truncated),
                "imported": False,
                "backup": recovery_result(),
            }
        qi["_source_premise"] = post_backup_premise
        qi["_gap_fill_receipts"] = candidate_premise.gap_fill_receipts(
            post_backup_premise
        )
        log.info(fmt(C.GRAY,
            f"  ⟳  Moved {plural(len(verified_truncated), 'broken file')} "
            f"to backup: {backup_path.name}"))

        # Baseline: ISRC counts of what remains on disk now that the truncated
        # originals are in the backup. The presence/intact gates below compare
        # against baseline + wanted, so a healthy PRE-EXISTING file sharing a
        # refill's ISRC can't vouch for a refill that never came back.
        held_tracks = _audio_files(album_dir)
        _bl_errs = []
        baseline_counts = Counter(
            _norm_isrc(et.get("isrc"))
            for et in read_album_dir(album_dir, walk_errors=_bl_errs))
        baseline_counts.pop("", None)
        if _bl_errs:
            baseline_counts = None

        # Carry the truncated originals' own tags + art onto the refills
        # before beets files them. The audio is fetched by track ID (the right
        # recording), but Qobuz returns its compilation/single album for that
        # ISRC, so without this the refill would import tagged + named for a
        # different album than the folder it lives in.
        retag_sources = _backup_source_by_isrc(
            verified_truncated, album_dir, backup_path)
        retag_failed = set()
        if retag_sources:
            qi["pre_import_retag"] = _make_retag_callback(
                retag_sources, retag_failed)

        last_old_id = _highest_album_id()
        try:
            _execute_download_queue([qi], args, token)
        except AuthLost as exc:
            # Don't auto-restore: a multi-track refill may have already written
            # good replacements, and moving the truncated originals back would
            # clobber them (the same reasoning as the interrupt branch below).
            # Preserve the backup and point the user at it.
            if isinstance(exc, DownloadsRefused):
                lost, until = "refused the download", "downloads work again"
            else:
                lost, until = "rejected the token", "your token is refreshed"
            if backup_path and backup_path.exists():
                pin_repair_recovery(
                    "repair backup kept, authentication was lost mid-refill")
                settle_beets_after_stop()
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Qobuz {lost} mid-refill; damaged originals preserved at:\n"
                    f"     {backup_path}\n"
                    f"     Re-run Repair once {until}, or restore "
                    f"them by hand."))
            interrupt_with_recovery(
                exc,
                "refill",
                f"Qobuz {lost} while replacement tracks were being "
                "downloaded.",
            )
        except BaseException as exc:
            # Interrupted or an unexpected failure mid-refill. The originals
            # go back only where nothing landed: a refill that did may be
            # good, so it stays and the backup keeps the original it replaced.
            # A refill filed under another name would sit beside its original,
            # so nothing goes back once a new track is in the album.
            now_held = _audio_files(album_dir)
            if (backup_path.exists()
                    and _album_identity(album_dir) == album_identity
                    and held_tracks is not None and now_held is not None
                    and now_held - held_tracks
                    <= set(backup_payload(backup_path.receipt))):
                try:
                    restore_gap_fill_backup(
                        backup_path, album_dir, replace_dst=False,
                        keep=files_replaced_in_album(backup_path))
                except BaseException as restore_exc:
                    log.info(fmt(C.RED,
                        "  ✗  Putting the originals back stopped "
                        f"({restore_exc!r})."))
            if not backup_path.exists():
                resolve_recovery(
                    backup_path,
                    "Repair stopped before a replacement landed, so every "
                    "original was put back.",
                )
                log.info(fmt(C.YELLOW,
                    "  ⚠  Repair stopped; the damaged originals are back in "
                    "the album. Re-run Repair to retry."))
                raise
            pin_repair_recovery("repair backup kept, refill was interrupted")
            settle_beets_after_stop()
            landed = len(files_replaced_in_album(backup_path))
            stopped = (
                f"Repair stopped after {plural(landed, 'replacement track')} "
                "reached the album, before they were checked."
                if landed else
                "Repair stopped while replacement tracks were being "
                "downloaded.")
            log.info(fmt(C.YELLOW,
                f"  ⚠  {stopped} Damaged originals are kept at:\n"
                f"     {backup_path}\n"
                f"     Settings, Diagnostics lists them and can put them "
                f"back."))
            interrupt_with_recovery(exc, "refill", stopped)

        if _album_identity(album_dir) != album_identity:
            pin_repair_recovery(
                "repair backup kept, the album changed during refill")
            log.info(fmt(C.RED,
                "  ✗  The selected album changed during the refill. "
                f"The originals are preserved at:\n     {backup_path}"))
            checkpoint_recovery(
                backup_path,
                "refill",
                "The selected album changed while replacement tracks were "
                "downloaded.",
            )
            return {
                "n_ok": 0,
                "n_fail": max(qi.get("n_fail", 0), len(verified_truncated)),
                "imported": False,
                "backup": recovery_result(),
            }

        n_fail_final = qi.get("n_fail", 0)
        n_ok_final = qi.get("n_ok", 0)
        download_clean = (n_fail_final == 0 and n_ok_final > 0
                          and qi.get("imported", False))
        # "Back in place" = the refilled files are in album_dir. Success
        # additionally requires they're verifiably NOT still truncated, a
        # presence check alone would let a short re-rip pass, and "no ISRC to
        # verify by" is unproven, not proven.
        back_in_place = download_clean and _refills_present_in(
            album_dir, wanted_counts, baseline_counts)
        if (download_clean and baseline_counts is not None
                and not back_in_place):
            pin_repair_recovery(
                "repair backup kept, refill location could not be proven")
            log.info(fmt(C.RED,
                "  ✗  The refilled tracks were imported but could not be "
                "found in the album folder. The originals are preserved at:\n"
                f"     {backup_path}"))
            checkpoint_recovery(
                backup_path,
                "placement",
                "The replacement tracks could not be found in the album "
                "folder after import.",
            )
            return {
                "n_ok": 0,
                "n_fail": max(n_fail_final, len(verified_truncated)),
                "imported": False,
                "backup": recovery_result(),
            }
        if back_in_place and bool(wanted_isrcs):
            try:
                repaired = _refills_intact(album_dir, wanted_counts, token,
                                           baseline_counts)
            except (AuthLost, QobuzUnavailable) as exc:
                # _refills_intact verifies via Qobuz, so a token loss / outage
                # here aborts before the backup-resolution block below, leaving
                # the truncated originals orphaned in the backup dir with no
                # pointer for the user. Surface the location (same as the
                # download-phase AuthLost branch) before re-raising.
                if backup_path and backup_path.exists():
                    pin_repair_recovery(
                        "repair backup kept, final refill verification was "
                        "unavailable")
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Couldn't verify the re-downloaded tracks "
                        f"(auth lost / Qobuz unavailable), damaged originals "
                        f"preserved at:\n     {backup_path}\n"
                        f"     Re-run Repair once Qobuz is reachable, or restore "
                        f"them by hand."))
                interrupt_with_recovery(
                    exc,
                    "verification",
                    "Qobuz became unavailable before the replacement tracks "
                    "could be verified.",
                )
        else:
            repaired = False

        partial = []
        if backup_path and backup_path.exists():
            if repaired and retag_failed:
                # The audio is verifiably fixed, but the originals' tags/art
                # couldn't be carried onto some refills, the backup holds the
                # only copy of that metadata, so keep it. Pin it too: the age
                # sweep proves redundancy by same-path same-or-larger bytes,
                # which the (usually larger) refill satisfies, without the pin
                # the only copy of those tags would be reaped on schedule.
                if not pin_unverified_upgrade_backup(
                        backup_path,
                        "repair kept, original tags/art not carried onto the "
                        "refill; this backup holds their only copy"):
                    warn_pin_failed(backup_path)
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Repaired, but the original tags/art couldn't be "
                    f"carried onto {plural(len(retag_failed), 'refilled track')}, "
                    f"keeping the backup; it still holds them.\n"
                    f"     Backup: {backup_path}"))
                checkpoint_recovery(
                    backup_path,
                    "verification",
                    "The replacement audio was verified, but the original "
                    "tags or artwork could not be carried onto it.",
                )
            elif repaired:
                if retire_verified_repair_backup(backup_path):
                    resolve_recovery(
                        backup_path,
                        "The replacement tracks verified, so the damaged "
                        "originals' backup was removed.",
                    )
                    log.info(fmt(C.GREEN,
                        "  ✓  Replacement tracks verified, removed the "
                        "damaged originals' backup."))
                else:
                    if not pin_unverified_upgrade_backup(
                            backup_path,
                            "repair backup kept, could not be proven "
                            "redundant after a verified refill"):
                        warn_pin_failed(backup_path)
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Replacement tracks verified, but the originals' "
                        "backup couldn't be proven redundant, keeping it:\n"
                        f"     {backup_path}"))
                    checkpoint_recovery(
                        backup_path,
                        "verification",
                        "The replacement tracks verified, but the originals' "
                        "backup could not be proven redundant.",
                    )
            elif back_in_place:
                # The re-downloaded tracks are physically back but couldn't be
                # verified as intact (still short of the listed length, or no
                # ISRC to check against). Keep the originals, deleting the
                # only other copy on a presence check alone is how a bad re-
                # rip silently replaces a good-enough track with a worse one.
                pin_repair_recovery(
                    "repair backup kept, replacement could not be verified "
                    "intact")
                log.info(fmt(C.YELLOW,
                    f"  ⚠  The re-download couldn't be shown to be complete: "
                    f"it may be short of its listed length too, or Qobuz "
                    f"couldn't confirm its recording. The album now holds "
                    f"that download and your original is kept in the backup "
                    f"below; restore it from Settings, Diagnostics to put it "
                    f"back. Backup:\n     {backup_path}"))
                checkpoint_recovery(
                    backup_path,
                    "verification",
                    "The replacement is in the album but could not be shown "
                    "to be any better than the original, which is kept here. "
                    "Restore puts the original back.",
                )
            else:
                # A refill failed to download, or beets didn't import the
                # replacement. Restore so the album is at least back to its
                # pre-repair state instead of silently short a track. A refill
                # that did land keeps its place.
                if n_fail_final > 0:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  {plural(n_fail_final, 'track')} failed to re-download, "
                        "restoring "
                        f"{'their damaged originals' if n_fail_final > 1 else 'its damaged original'} "
                        "so the album isn't left short. A track that did "
                        "download keeps its place."))
                else:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Refill didn't return to the album folder, "
                        "restoring the damaged originals so it isn't left "
                        "short."))
                try:
                    n_restored = restore_gap_fill_backup(
                        backup_path, album_dir, replace_dst=not n_fail_final,
                        keep=(files_replaced_in_album(backup_path)
                              if n_fail_final else frozenset()))
                except BaseException as exc:
                    pin_repair_recovery(
                        "repair backup kept, automatic restore was "
                        "interrupted")
                    interrupt_with_recovery(
                        exc,
                        "restore",
                        "Automatic restoration stopped before every original "
                        "was returned.",
                    )
                if backup_path.exists() and n_ok_final and wanted_isrcs:
                    replaced = files_replaced_in_album(backup_path)
                    partial = [b for b in verified_truncated
                               if b["source_receipt"]["relative"] in replaced]
                landed = Counter(_norm_isrc(b.get("isrc")) for b in partial)
                partial_ok = False
                if partial and "" not in landed:
                    # The tracks that did download stay in the album, so the
                    # backup may go once they verify as a full refill does.
                    try:
                        partial_ok = (
                            _refills_intact(album_dir, landed, token,
                                            baseline_counts)
                            and retire_verified_repair_backup(backup_path))
                    except (AuthLost, QobuzUnavailable) as exc:
                        pin_repair_recovery(
                            "repair backup kept, final refill verification "
                            "was unavailable")
                        interrupt_with_recovery(
                            exc,
                            "verification",
                            "Qobuz became unavailable before the replacement "
                            "tracks could be verified.",
                        )
                if partial_ok:
                    resolve_recovery(
                        backup_path,
                        "The tracks that downloaded verified, and the others' "
                        "originals were put back.",
                    )
                    log.info(fmt(C.GREEN,
                        f"  ✓  Repaired {plural(len(partial), 'track')}, "
                        "verified; the originals of the tracks that failed "
                        "are back in the album."))
                elif n_restored and not backup_path.exists():
                    resolve_recovery(
                        backup_path,
                        "The refill did not complete, so every original was "
                        "restored.",
                    )
                    log.info(fmt(C.GREEN,
                        "  ✓  Restored the originals; re-run Repair to retry."))
                else:
                    partial = []
                    pin_repair_recovery(
                        "repair backup kept, automatic restore was partial or "
                        "failed")
                    log.info(fmt(C.RED,
                        f"  ✗  Restored {plural(n_restored, 'track')}; remaining "
                        f"originals are at:\n     {backup_path}"))
                    checkpoint_recovery(
                        backup_path,
                        "restore",
                        "The refill did not complete and automatic "
                        "restoration was incomplete.",
                    )

        # Beets took each refill in as a new entry at its original's path.
        if (back_in_place or partial) and replaced_entries:
            forget_replaced_beets_entries(replaced_entries)

        # A verified refill is a repair whether or not the originals' backup
        # could also be retired; a kept backup rides along in "backup".
        if repaired or partial:
            log_entries = [{
                "artist": artist_name,
                "album":  album_dir.name,
                "title":  b["title"],
            } for b in (verified_truncated if repaired else partial)]
            if append_repair_log(log_entries):
                log.info(fmt(C.CYAN,
                    f"  📋  Logged {plural(len(log_entries), 'replaced track')}, "
                    f"refresh these albums on any offline client:\n"
                    f"     {cfg.REPAIR_LOG_PATH}"))

        return {
            "n_ok": n_ok_final if repaired else len(partial),
            "n_fail": (n_fail_final if repaired or partial
                       else max(n_fail_final, len(verified_truncated))),
            "imported": repaired or bool(partial),
            "cancelled": qi.get("result") == "cancelled",
            "backup": recovery_result(),
            "cd_quality_tracks": (qi.get("cd_quality_tracks", [])
                                  if repaired or partial else []),
        }
    finally:
        args.no_upgrade = saved_no_upgrade


def _report_repair_recovery(backup, *, reason=""):
    """Tell an interactive caller exactly where retained originals remain."""
    if not isinstance(backup, BackupResult):
        return False
    count = backup.backed_up
    requested = backup.requested
    detail = (f"{count} of {plural(requested, 'original file')} "
              f"{'remains' if count == 1 else 'remain'}")
    if reason:
        log.info(fmt(C.YELLOW, f"  ⚠  {reason}"))
    log.info(fmt(C.YELLOW,
        f"  ⚠  Recovery needed: {detail} at:\n"
        f"     {backup.path}\n"
        "     Keep this folder until the album has been checked."))
    return True


def _scan_report_repair(album_dir, artist_name, args, token, deep=True,
                        quiet=False, cd_quality=None):
    """Scan one album dir by ISRC, report, confirm, and repair. Returns
    "repaired" | "would_repair" | "clean" | "attention" | "skipped" | "failed" |
    "recovery", and adds a repaired album's tracks that came at CD quality to
    ``cd_quality``.
    """
    if not quiet:
        section(f"Repair scan, {truncate(album_dir.name, 60)}")
        log.info(fmt(C.GRAY,
            "  Resolving every FLAC by ISRC against Qobuz "
            "(no album-edition guessing) …"))

    scan = scan_dir_for_isrc_repairs(album_dir, token, deep=deep)
    verified_truncated = scan["verified_truncated"]
    verified_ok        = scan["verified_ok"]
    isrc_no_match      = scan["isrc_no_match"]
    no_isrc_tag        = scan["no_isrc_tag"]
    isrc_mismatch      = scan.get("isrc_mismatch") or []
    unverified         = scan.get("unverified", 0)

    if (quiet and not unverified and not verified_truncated and not isrc_mismatch and not any(
            x.get("diagnostic") for x in (*isrc_no_match, *no_isrc_tag))):
        return "clean"
    if quiet:
        # Commit the caller's \r progress line and head the report.
        print()
        log.info(fmt(C.BOLD + C.WHITE,
            f"  {artist_name} - {truncate(album_dir.name, 50)}"))

    log.info(fmt(C.GRAY,
        f"  {verified_ok} complete  ·  "
        f"{len(verified_truncated)} damaged  ·  "
        f"{len(isrc_no_match)} recording ID not on Qobuz  ·  "
        f"{len(no_isrc_tag)} no recording ID  ·  "
        f"{len(isrc_mismatch)} recording or edition not confirmed"))
    if unverified:
        log.warning(fmt(C.YELLOW,
            f"  ⚠  Scan incomplete: {unverified} unverified."))

    if isrc_no_match:
        log.info(fmt(C.GRAY,
            "\n  Skipped (Qobuz has no track with this recording ID; it may "
            "come from another store or have left Qobuz):"))
        for x in isrc_no_match[:10]:
            diag = x.get("diagnostic")
            if diag:
                log.info(fmt(C.YELLOW,
                    f"    • {truncate(x['title'], 50)}  "
                    f"[isrc={x['isrc']}], {diag}"))
            else:
                log.info(fmt(C.GRAY,
                    f"    • {truncate(x['title'], 50)}  "
                    f"[isrc={x['isrc']}]"))
        if len(isrc_no_match) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(isrc_no_match) - 10} more"))
    if no_isrc_tag:
        log.info(fmt(C.GRAY,
            "\n  Skipped (no recording ID (ISRC) tag, so the recording "
            "can't be confirmed):"))
        for x in no_isrc_tag[:10]:
            diag = x.get("diagnostic")
            if diag:
                log.info(fmt(C.YELLOW,
                    f"    • {truncate(x['title'], 60)}: {diag}"))
            else:
                log.info(fmt(C.GRAY,
                    f"    • {truncate(x['title'], 60)}"))
        if len(no_isrc_tag) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(no_isrc_tag) - 10} more"))

    # Two different outcomes share this bucket, so they can't share one
    # heading: a short file is genuinely left alone, a damaged one still needs
    # dealing with and just can't be refilled a track at a time.
    mismatch_damaged = [x for x in isrc_mismatch if x.get("diagnostic")]
    mismatch_short = [x for x in isrc_mismatch if not x.get("diagnostic")]
    unresolved_damage = bool(
        mismatch_damaged
        or any(x.get("diagnostic") for x in (*isrc_no_match, *no_isrc_tag))
    )

    def _mismatch_row(x, colour):
        log.info(
            fmt(colour, f"    • {truncate(x.get('local_title') or '?', 40)}")
            + fmt(C.GRAY,
                f"   {_format_mmss(x['file_length']):>5}"
                f" / {_format_mmss(x['qobuz_duration']):>5}"
                f"  matched \"{truncate(x.get('title') or '?', 30)}\""
                f"  [isrc={x.get('isrc')}]"))

    if mismatch_damaged:
        log.info(fmt(C.YELLOW,
            "\n  Damaged, but the recording or edition could not be confirmed. "
            "Review a whole-album re-download, or check the recording and "
            "edition tags before retrying:"))
        for x in mismatch_damaged[:10]:
            _mismatch_row(x, C.YELLOW)
        if len(mismatch_damaged) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(mismatch_damaged) - 10} more"))

    if mismatch_short:
        log.info(fmt(C.GRAY,
            "\n  Left alone (shorter than the match, but its recording or "
            "edition could not be confirmed):"))
        for x in mismatch_short[:10]:
            _mismatch_row(x, C.WHITE)
        if len(mismatch_short) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(mismatch_short) - 10} more"))

    if not verified_truncated:
        if unresolved_damage:
            log.info(fmt(
                C.YELLOW,
                "\n  ⚠  Damage was found, but no affected track was safe "
                "to refill automatically. Follow the guidance above.",
            ))
            matched = find_qobuz_album_for_dir(album_dir, artist_name, token)
            if matched and matched.get("id"):
                log.info(fmt(C.GRAY,
                    "     To download the whole album again as "
                    f"\"{matched.get('title') or album_dir.name}\", run:\n"
                    "     qobuz-librarian --force "
                    f"https://open.qobuz.com/album/{matched['id']}"))
        elif not quiet and not unverified:
            log.info(fmt(C.GREEN,
                "\n  ✓  No damaged tracks. Nothing to repair."))
        return "attention" if unresolved_damage or unverified else "clean"

    log.info(fmt(C.YELLOW + C.BOLD,
        f"\n  ⚠  {plural(len(verified_truncated), 'damaged track')}, "
        "matched on Qobuz by recording ID (ISRC):"))
    log.info(fmt(C.GRAY,
        "    track  title                                              "
        "  on-disk / Qobuz   ISRC"))
    for b in verified_truncated:
        qt = b["qobuz_track"]
        # Name the row after the file on disk. The matched recording's own
        # title is the right thing to re-download and the wrong thing to label
        # a file with, because a mis-tagged ISRC makes them different songs.
        title = b.get("local_title") or b["title"]
        ver = qt.get("version") or ""
        if ver and ver.lower() not in title.lower():
            title = f"{title} ({ver})"
        tnum = b.get("local_track_number") or b["track_number"]
        tnum_str = f"#{tnum:>2}" if tnum else "   "
        log.info(
            fmt(C.WHITE, f"    {tnum_str}  {truncate(title, 50):<50}")
            + fmt(C.GRAY,
                f"   {_format_mmss(b['file_length']):>5}"
                f" / {_format_mmss(b['qobuz_duration']):>5}"
                f"  {b['isrc']}"
                + ("  won't decode" if b.get("reason") else "")))

    # Repair moves the truncated originals aside before re-ripping, so it has to
    # stop here under --dry-run, reaching repair_album_dir would mutate the
    # filesystem (and an interrupt mid-repair could strand the originals).
    if getattr(args, "dry_run", False):
        log.info(fmt(C.YELLOW,
            f"\n  --dry-run: would re-download "
            f"{plural(len(verified_truncated), 'track')}. Nothing changed."))
        return "attention" if unverified else "would_repair"

    if not args.yes:
        r = ask(f"\n  Re-download {plural(len(verified_truncated), 'track')}? "
                "[y/N]: ")
        if r is None:
            return "attention"
        if r not in ("y", "yes"):
            log.info(fmt(C.GRAY, "  Skipped."))
            return "attention" if unverified else "skipped"

    try:
        result = repair_album_dir(
            album_dir, verified_truncated, artist_name, args, token)
    except RepairRecoveryRequired as exc:
        _report_repair_recovery(
            exc.recovery.backup,
            reason=exc.recovery.reason,
        )
        raise exc.cause
    if (result
            and result.get("n_ok", 0) > 0
            and result.get("n_fail", 0) == 0
            and result.get("imported")
            and not result.get("repair_unverified")):
        if isinstance(result.get("backup"), BackupResult):
            log.info(fmt(C.YELLOW,
                "  ⚠  The repair verified, and its backup is kept for review "
                f"in Settings, Diagnostics:\n     {result['backup'].path}"))
        _refresh_review_state_after_downloads(
            [{**result, "album": None, "dir": album_dir, "in_place": True}],
            token,
            args,
        )
        if cd_quality is not None:
            cd_quality.extend(result.get("cd_quality_tracks") or [])
        return "attention" if unverified else "repaired"
    if result and _report_repair_recovery(result.get("backup")):
        return "recovery"
    return "failed"


def _all_library_album_dirs(*, on_artist_error=None):
    """Every (artist_dir, album_dir) under MUSIC_ROOT, artist-sorted."""
    pairs = []
    for adir in list_library_artists(on_artist_error=on_artist_error):
        walk_errors = []
        for aldir in list_artist_album_dirs(adir, walk_errors=walk_errors):
            pairs.append((adir, aldir))
        if walk_errors:
            error = "; ".join(map(str, walk_errors))
            if on_artist_error is not None:
                on_artist_error(adir, error)
            else:
                log.warning(unreadable_artist(adir.name, error))
    return pairs


def _report_repair_run(tally, started, *, interrupted=False, clause=None):
    """Give a finished terminal Repair its History card and hook report."""
    parts = [f"{count} {what}" for count, what in (
        (tally.get("repaired", 0), "repaired"),
        (tally.get("attention", 0), "did not finish cleanly"),
        (tally.get("failed", 0), "failed"),
        (tally.get("recovery", 0), "kept a recovery backup"),
    ) if count]
    summary = ("Stopped early. " if interrupted else "") + (
        f"{', '.join(parts)}." if parts else "No damaged tracks found.")
    if tally.get("cd_quality"):
        summary += f" {cd_quality_note(tally['cd_quality'])}"
    failed = tally.get("failed", 0)
    error = (f"Finished with {plural(failed, 'error')}." if failed
             else f"Needs attention: {clause}." if clause else None)
    post_job_hook.report_terminal_run(
        kind="repair", title="Repair",
        status=("canceled" if interrupted
                else "failed" if error else "done"),
        summary=summary, error=None if interrupted else error,
        started_at=started)


def run_album_repair_mode(args, token, *, loop=False):
    """ISRC-anchored refill of truncated FLACs, replaces are matched on
    ISRC so the new file is the same recording as the old one.

    `'*'` at the artist prompt sweeps the whole library. --no-upgrade is
    forced on so the upgrade path can't escalate to wipe-and-replace.
    """
    saved_no_upgrade = getattr(args, "no_upgrade", False)
    args.no_upgrade = True  # repair = surgical; never silently upgrade the rest

    try:
        if not getattr(args, "dry_run", False):
            log.info(fmt(C.YELLOW,
                "  The damaged tracks are removed once their replacements "
                "verify. This cannot be undone."))
        while True:
            clear_scan_caches()
            try:
                artist_dir, album_dir = _prompt_library_album_for_repair(
                    args, token)
                started = time.time()
            except AuthLost as exc:
                die(fmt(C.RED, auth_lost_msg(exc)), EXIT_AUTH)

            if artist_dir == NO_ANSWER:
                return EXIT_GENERAL
            if artist_dir == "__ALL__":
                unreadable = []

                def artist_read_failed(path, error):
                    unreadable.append(path.name)
                    log.warning(f"Unreadable artist {path.name}: {error}. Check permissions and retry.")

                targets = _all_library_album_dirs(on_artist_error=artist_read_failed)
                if not targets:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  No album folders found under the music root."))
                    if not loop:
                        return EXIT_GENERAL if unreadable else 0
                    continue
                section(f"Repair scan, whole library "
                        f"({plural(len(targets), 'album')})")
                log.info(fmt(C.GRAY,
                    "  Albums with no damaged tracks are skipped silently. "
                    "Ctrl-C to stop."))
                tally = {
                    "repaired": 0,
                    "would_repair": 0,
                    "clean": 0,
                    "attention": 0,
                    "skipped": 0,
                    "failed": 0,
                    "recovery": 0,
                    "cd_quality": [],
                }
                interrupted = False
                try:
                    for i, (adir, aldir) in enumerate(targets, 1):
                        _line = (f"  [{i}/{len(targets)}] Scanning "
                                 f"{truncate(adir.name, 28)} - "
                                 f"{truncate(aldir.name, 30).removesuffix('…')}…")
                        if sys.stdout.isatty():
                            print(f"\r{_line:<90}", end="", flush=True)
                        else:
                            vlog(_line.strip())
                        try:
                            status = _scan_report_repair(
                                aldir, adir.name, args, token,
                                deep=True, quiet=True,
                                cd_quality=tally["cd_quality"])
                        except AuthLost as exc:
                            print()
                            die(fmt(C.RED, auth_lost_msg(exc)), EXIT_AUTH)
                        except QobuzUnavailable:
                            # Finish the progress line before the transient
                            # abort propagates to the clean EXIT_TRANSIENT stop.
                            print()
                            raise
                        tally[status] = tally.get(status, 0) + 1
                except KeyboardInterrupt:
                    interrupted = True
                    print()
                else:
                    if sys.stdout.isatty():
                        print(f"\r{' ' * 90}\r", end="", flush=True)
                section("Library repair summary")
                done_label, done_count = (
                    ("would repair", tally["would_repair"])
                    if getattr(args, "dry_run", False)
                    else ("repaired", tally["repaired"]))
                _summary = (f"  {done_label}: {done_count}  ·  "
                            f"clean: {tally['clean']}  ·  "
                            f"skipped: {tally['skipped']}")
                if tally["attention"]:
                    _summary += f"  ·  needs attention: {tally['attention']}"
                if tally["failed"]:
                    _summary += f"  ·  failed: {tally['failed']}"
                if tally["recovery"]:
                    _summary += f"  ·  recovery needed: {tally['recovery']}"
                needs_attention = (
                    interrupted
                    or bool(unreadable)
                    or any(tally[key] for key in (
                        "attention", "failed", "recovery"
                    ))
                )
                if unreadable:
                    _summary += "  ·  unreadable artists: " + ", ".join(unreadable)
                (log.warning if needs_attention else log.info)(
                    fmt(C.YELLOW if needs_attention else C.GRAY, _summary)
                )
                clause = None
                if tally["recovery"]:
                    clause = f"{plural(tally['recovery'], 'album')} kept a recovery backup"
                elif tally["attention"]:
                    clause = f"{plural(tally['attention'], 'album')} did not finish cleanly"
                elif unreadable:
                    clause = f"{plural(len(unreadable), 'artist')} could not be read"
                if interrupted:
                    log.info(fmt(C.GRAY, "  Interrupted."))
                elif tally["failed"]:
                    log.warning(fmt(C.RED, f"  ✗  Finished with {plural(tally['failed'], 'error')}."))
                elif clause:
                    log.warning(fmt(C.YELLOW, f"  ⚠  Needs attention: {clause}."))
                else:
                    log.info(fmt(C.GREEN, "  ✓  Done."))
                if not getattr(args, "dry_run", False):
                    _report_repair_run(tally, started, interrupted=interrupted,
                                       clause=clause)
                if not loop:
                    if interrupted:
                        return EXIT_INTERRUPT
                    return EXIT_GENERAL if needs_attention else 0
                continue

            if album_dir is None:
                return 0

            cd_quality = []
            try:
                status = _scan_report_repair(
                    album_dir, artist_dir.name, args, token,
                    cd_quality=cd_quality,
                )
            except AuthLost as exc:
                die(fmt(C.RED, auth_lost_msg(exc)), EXIT_AUTH)
            if status in {"repaired", "attention", "failed", "recovery"}:
                _report_repair_run({status: 1, "cd_quality": cd_quality}, started, clause={
                    "attention": "the album did not finish cleanly",
                    "recovery": "the album kept a recovery backup",
                }.get(status))

            if not loop:
                needs_attention = status in {"attention", "failed", "recovery"}
                if needs_attention:
                    clause = "review the result above before retrying"
                    log.warning(fmt(C.YELLOW, f"  ⚠  Needs attention: {clause}."))
                else:
                    log.info(fmt(C.GREEN, "  ✓  Done."))
                return EXIT_GENERAL if needs_attention else 0
    finally:
        args.no_upgrade = saved_no_upgrade
