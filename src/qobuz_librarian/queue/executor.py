"""Queue executor: download, pre-import hooks, beets, backup resolution."""
import errno
import os
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian import (
    disk_support,
    download_result,
    post_job_hook,
    run_lock,
    saved_views,
)
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api.auth import AuthLost, DownloadsRefused
from qobuz_librarian.completion import (
    CompletionOrigin,
    CompletionOriginKind,
    RecoveryOwner,
    SourceTransitionKind,
    normalise_album_id,
    parse_completion_input_record,
)
from qobuz_librarian.download import (
    clear_attempts_after_import,
    discard_retry_staging,
    keep_unimported_download,
    note_superseded_attempts,
    retain_download_staging,
    retire_download_staging_after_import,
    retire_empty_download_staging,
    retire_failed_download_staging,
    run_album_download,
    staged_track_bindings,
    validated_staged_album_dirs,
)
from qobuz_librarian.download_result import (
    download_job_outcome,
    incomplete_track_counts,
    shortfall_fields,
)
from qobuz_librarian.integrations.beets import (
    ImportedThenInterrupted,
    MusicDiskFull,
    _consolidate_duplicate_albums,
    _prepare_staging_tags,
    backup_listed_in_beets,
    beets_import_albums,
    capture_beets_album_entries,
    durable_import_possible,
    fill_album_tags,
    forget_imported_beets_entries,
    prepare_managed_staging_tags,
    relocate_disc_album_artwork,
    retire_backup_beets_entries,
    retire_replaced_beets_entries,
    staging_preflight,
)
from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE, downsample_dir
from qobuz_librarian.integrations.lyrics import (
    _run_lyric_hook,
    write_post_import_sidecars,
)
from qobuz_librarian.integrations.rip import (
    is_cancel_requested,
    snapshot_staging,
)
from qobuz_librarian.integrations.staging import (
    capture_tree,
    list_groups,
    park_trees,
    reconcile_imported_group,
    tree_matches,
)
from qobuz_librarian.library.backup import (
    backup_album_dir,
    backup_files_preserved,
    capture_album_source_receipt,
    capture_album_tree_identity,
    carry_backup_companions,
    companions_in_the_way,
    dispose_backup,
    pin_unverified_upgrade_backup,
    restore_gap_fill_backup,
    restore_upgrade_backup,
    some_names,
    unwritable_folder,
    warn_pin_failed,
)
from qobuz_librarian.library.candidate_premise import (
    CandidateStale,
    canonical_premise,
    validate_premise,
)
from qobuz_librarian.library.catalog import (
    _count_audio_files_in,
    edition_album_title,
    edition_files,
    find_album_dir_by_track_signatures,
    find_album_dir_filesystem,
    folder_holds_all_tracks,
    names_other_version,
    other_version_dir,
    prompt_and_migrate_multi_artist_folder,
    track_signatures_for_album_dirs,
)
from qobuz_librarian.library.scanner import cache_album_tags, clear_scan_caches, read_album_dir
from qobuz_librarian.modes.consolidate import match_sibling_track
from qobuz_librarian.quality.decision import mark_album_capped, mark_local_album_capped
from qobuz_librarian.quality.verify import (
    quality_below_line,
    quality_met_line,
    redownload_with_staged_fallback,
    retry_preserves_track_coverage,
    verify_and_recover,
)
from qobuz_librarian.queue import journal as queue_state
from qobuz_librarian.queue.durable_album import (
    plan_durable_new_album,
    queue_item_may_create_library_backup,
)
from qobuz_librarian.queue.durable_runner import (
    DurableAlbumStatus,
    DurableAlbumUnavailable,
    execute_durable_new_album,
)
from qobuz_librarian.queue.startup_recovery import (
    BlockedItemSettlementAction,
    BlockedItemSettlementStatus,
    StartupRecoveryAction,
    StartupRecoveryStatus,
    recover_startup_state,
    settle_blocked_item,
)
from qobuz_librarian.repair_log import warn_if_download_truncated
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate, wrap
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog

# Installed by the web layer (web/jobs): called when a staged download stays
# under the quality target after the recovery attempt, so the owning job can
# carry an attention marker. The CLI leaves it unset.
on_quality_shortfall = None


@dataclass(frozen=True, slots=True)
class PreImportHooksResult:
    resampled: int
    errors: int = 0
    saved_bytes: int = 0
    flush_warnings: int = 0
    cancelled: bool = False


def _sibling_tracks_all_kept(sibling, kept):
    """True when every audio file in ``sibling`` is a recording ``kept`` also
    holds, so retiring the sibling loses no track. False when a file in the
    sibling matches no track there, as in another edition or an untagged rip,
    and None when either tree can't be read."""
    errors = []
    sibling_tracks = read_album_dir(Path(sibling), walk_errors=errors)
    kept_tracks = read_album_dir(Path(kept), walk_errors=errors)
    if errors or not kept_tracks:
        return None
    claimed = set()
    for track in sibling_tracks:
        match = match_sibling_track(track, kept_tracks)
        if match is None or id(match) in claimed:
            return False
        claimed.add(id(match))
    return True


def _seal_queue_item_siblings(item):
    receipts = []
    for sibling in item.get("siblings_to_delete", []):
        receipt = capture_album_tree_identity(sibling)
        receipts.append(receipt)
        if receipt is None:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Keeping sibling {Path(sibling).name}; it couldn't "
                "be checked before the download.",
            ))
    item["_sibling_cleanup_receipts"] = tuple(receipts)


_IMPORT_OWNERSHIP_IDENTITY_FIELDS = (
    "device",
    "inode",
    "size",
    "modified_ns",
    "changed_ns",
)


def _valid_import_ownership_identity(value):
    return (
        isinstance(value, dict)
        and all(type(value.get(field)) is int
                for field in _IMPORT_OWNERSHIP_IDENTITY_FIELDS)
    )


def _valid_import_ownership_relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    relative = PurePosixPath(value)
    return (
        not relative.is_absolute()
        and relative.as_posix() == value
        and all(part not in ("", ".", "..") for part in relative.parts)
    )


def _valid_import_ownership_location(value):
    return (
        _valid_import_ownership_identity(value)
        and _valid_import_ownership_relative(value.get("relative"))
    )


def _ownership_identity_from_location(value):
    return {
        field: value[field] for field in _IMPORT_OWNERSHIP_IDENTITY_FIELDS
    }


def _ownership_identity_from_stat(value):
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
    }


def _exact_import_ownership_identity(value):
    return (
        _valid_import_ownership_identity(value)
        and set(value) == set(_IMPORT_OWNERSHIP_IDENTITY_FIELDS)
    )


def _validated_import_album_scope(record, file_relative):
    scope = record.get("album_scope") if isinstance(record, dict) else None
    if (
        not isinstance(scope, dict)
        or set(scope) != {"relative", "directory", "ancestors"}
        or not _valid_import_ownership_relative(scope.get("relative"))
        or not _exact_import_ownership_identity(scope.get("directory"))
        or not isinstance(scope.get("ancestors"), list)
    ):
        return None
    scope_parts = PurePosixPath(scope["relative"]).parts
    file_parts = file_relative.parts
    if (
        not scope_parts
        or len(scope_parts) >= len(file_parts)
        or file_parts[:len(scope_parts)] != scope_parts
        or len(scope["ancestors"]) != len(scope_parts) - 1
    ):
        return None
    identities = []
    for length, ancestor in enumerate(scope["ancestors"], start=1):
        expected_relative = PurePosixPath(*scope_parts[:length]).as_posix()
        if (
            not isinstance(ancestor, dict)
            or set(ancestor) != {"relative", "directory"}
            or ancestor.get("relative") != expected_relative
            or not _exact_import_ownership_identity(
                ancestor.get("directory")
            )
        ):
            return None
        identities.append(ancestor["directory"])
    identities.append(scope["directory"])
    return scope_parts, identities


def _verified_import_album_dir(item):
    """Locate the landed album from the sealed one-file import receipt."""
    payload = item.get("_import_ownership")
    root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
        or payload.get("sealed") is not True
        or payload.get("root") != str(root)
        or not _exact_import_ownership_identity(payload.get("root_identity"))
    ):
        return None
    records = payload.get("items")
    if not isinstance(records, list) or len(records) != 1:
        return None
    record = records[0]
    if (
        not isinstance(record, dict)
        or not _exact_import_ownership_identity(record.get("file"))
        or not _valid_import_ownership_relative(record.get("relative"))
    ):
        return None
    relative = PurePosixPath(record["relative"])
    if len(relative.parts) < 2:
        return None
    validated_scope = _validated_import_album_scope(record, relative)
    if validated_scope is None:
        return None
    album_parts, directory_identities = validated_scope

    dir_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
    leaf_flags = (
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        | os.O_NONBLOCK
    )
    chain = []
    leaf_fd = None
    try:
        chain.append(os.open(root, dir_flags))
        for part in relative.parts[:-1]:
            chain.append(os.open(part, dir_flags, dir_fd=chain[-1]))

        root_stat = os.fstat(chain[0])
        named_root = os.stat(root, follow_symlinks=False)
        expected_root = payload["root_identity"]
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or not stat.S_ISDIR(named_root.st_mode)
            or _ownership_identity_from_stat(root_stat) != expected_root
            or _ownership_identity_from_stat(named_root) != expected_root
        ):
            return None

        for index, part in enumerate(relative.parts[:-1]):
            held = os.fstat(chain[index + 1])
            named = os.stat(
                part, dir_fd=chain[index], follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(held.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or _ownership_identity_from_stat(named)
                != _ownership_identity_from_stat(held)
                or (
                    index < len(directory_identities)
                    and _ownership_identity_from_stat(held)
                    != directory_identities[index]
                )
            ):
                return None

        leaf_name = relative.parts[-1]
        named_before = os.stat(
            leaf_name, dir_fd=chain[-1], follow_symlinks=False
        )
        leaf_fd = os.open(leaf_name, leaf_flags, dir_fd=chain[-1])
        held_leaf = os.fstat(leaf_fd)
        named_after = os.stat(
            leaf_name, dir_fd=chain[-1], follow_symlinks=False
        )
        if (
            not stat.S_ISREG(named_before.st_mode)
            or not stat.S_ISREG(held_leaf.st_mode)
            or not stat.S_ISREG(named_after.st_mode)
            or _ownership_identity_from_stat(named_before) != record["file"]
            or _ownership_identity_from_stat(held_leaf) != record["file"]
            or _ownership_identity_from_stat(named_after) != record["file"]
        ):
            return None

        # Confirm the entire held path is still publicly named after the leaf
        # was opened. The receipt's explicit scope is accepted only while this
        # descriptor chain and the exact sealed identities still agree.
        named_root = os.stat(root, follow_symlinks=False)
        if _ownership_identity_from_stat(named_root) != expected_root:
            return None
        for index, part in enumerate(relative.parts[:-1]):
            held = os.fstat(chain[index + 1])
            named = os.stat(
                part, dir_fd=chain[index], follow_symlinks=False
            )
            if (
                _ownership_identity_from_stat(named)
                != _ownership_identity_from_stat(held)
                or (
                    index < len(directory_identities)
                    and _ownership_identity_from_stat(held)
                    != directory_identities[index]
                )
            ):
                return None
        if (
            _ownership_identity_from_stat(os.fstat(leaf_fd))
            != record["file"]
            or _ownership_identity_from_stat(os.stat(
                leaf_name, dir_fd=chain[-1], follow_symlinks=False
            )) != record["file"]
        ):
            return None
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if leaf_fd is not None:
            try:
                os.close(leaf_fd)
            except OSError:
                pass
        for descriptor in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass
    return root.joinpath(*album_parts)


def _clear_import_ownership(item):
    item.pop("_import_ownership", None)
    item.pop("_import_ownership_created_files", None)


def _post_import_change_within_item(item, change):
    scope = item.get("_resolved_post_dir")
    path = change.get("path") if isinstance(change, dict) else None
    if scope is None or not isinstance(path, str) or not path or "\x00" in path:
        return False
    try:
        scope = Path(os.path.abspath(os.fspath(scope)))
        path = Path(os.path.abspath(path))
        relative = path.relative_to(scope)
    except (OSError, TypeError, ValueError):
        return False
    return bool(relative.parts) and all(
        part not in ("", ".", "..") for part in relative.parts
    )


def _post_import_directory_change_within_item(item, change):
    scope = item.get("_resolved_post_dir")
    path = change.get("path") if isinstance(change, dict) else None
    if scope is None or not isinstance(path, str) or not path or "\x00" in path:
        return False
    try:
        scope = Path(os.path.abspath(os.fspath(scope)))
        path = Path(os.path.abspath(path))
        relative = path.relative_to(scope)
    except (OSError, TypeError, ValueError):
        return False
    return all(part not in ("", ".", "..") for part in relative.parts)


def _advance_created_directory_identities(item, payload, directory_changes):
    records = payload.get("items") if isinstance(payload, dict) else None
    root = payload.get("root") if isinstance(payload, dict) else None
    if (
        not isinstance(records, list)
        or not records
        or not isinstance(root, str)
        or not os.path.isabs(root)
    ):
        return False
    groups = [record.get("created_directories") for record in records]
    if not all(isinstance(group, list) for group in groups):
        return False
    for group in groups:
        for record in group:
            if not _valid_import_ownership_location(record):
                return False
            expected_path = os.path.abspath(os.fspath(
                Path(root).joinpath(*PurePosixPath(record["relative"]).parts)
            ))
            current = _ownership_identity_from_location(record)
            used = set()
            while True:
                matching = [
                    (index, change)
                    for index, change in enumerate(directory_changes)
                    if index not in used
                    and change["path"] == expected_path
                    and change["before"] == current
                ]
                if not matching:
                    break
                if len(matching) != 1:
                    return False
                index, change = matching[0]
                if not _post_import_directory_change_within_item(item, change):
                    return False
                used.add(index)
                current = dict(change["after"])
            for field in _IMPORT_OWNERSHIP_IDENTITY_FIELDS:
                record[field] = current[field]
    return True


def _advance_import_ownership_identities(
        items, changes, created_files=None, directory_changes=None):
    """Carry exact post-import file proofs into each sealed import item."""
    if created_files is None:
        created_files = []
    if directory_changes is None:
        directory_changes = []
    if (
        not isinstance(changes, list)
        or not isinstance(created_files, list)
        or not isinstance(directory_changes, list)
    ):
        return
    validated_directory_changes = []
    for change in directory_changes:
        path = change.get("path") if isinstance(change, dict) else None
        before = change.get("before") if isinstance(change, dict) else None
        after = change.get("after") if isinstance(change, dict) else None
        if (
            not isinstance(path, str)
            or not os.path.isabs(path)
            or not _valid_import_ownership_identity(before)
            or not _valid_import_ownership_identity(after)
        ):
            for queue_item in items:
                if isinstance(queue_item.get("_import_ownership"), dict):
                    _clear_import_ownership(queue_item)
            return
        validated_directory_changes.append({
            "path": os.path.abspath(path),
            "before": dict(before),
            "after": dict(after),
        })
    for queue_item in items:
        payload = queue_item.get("_import_ownership")
        records = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(records, list) or not records:
            continue
        root = payload.get("root") if isinstance(payload, dict) else None
        if not isinstance(root, str) or root != os.path.abspath(
                os.fspath(cfg.MUSIC_ROOT)):
            _clear_import_ownership(queue_item)
            continue
        item_created_files = []
        refused_item = False
        for record in records:
            current = record.get("file") if isinstance(record, dict) else None
            relative = (
                record.get("relative") if isinstance(record, dict) else None
            )
            if (
                not _valid_import_ownership_identity(current)
                or not _valid_import_ownership_relative(relative)
            ):
                refused_item = True
                break
            current = dict(current)
            expected_path = os.path.abspath(os.fspath(
                Path(root).joinpath(*PurePosixPath(relative).parts)
            ))
            expected_sidecar = os.path.abspath(os.fspath(
                Path(expected_path).with_suffix(".lrc")
            ))
            matching_created = [
                receipt
                for receipt in created_files
                if (
                    isinstance(receipt, dict)
                    and receipt.get("track") == current
                    and receipt.get("track_path") == expected_path
                )
            ]
            if len(matching_created) > 1:
                refused_item = True
                break
            if matching_created:
                receipt = matching_created[0]
                created_identity = receipt.get("file")
                created_path = receipt.get("path")
                if (
                    not _valid_import_ownership_identity(created_identity)
                    or not _post_import_change_within_item(queue_item, receipt)
                    or not isinstance(created_path, str)
                    or not os.path.isabs(created_path)
                    or os.path.abspath(created_path) != expected_sidecar
                ):
                    refused_item = True
                    break
                item_created_files.append({
                    "path": os.path.abspath(created_path),
                    "file": dict(created_identity),
                })
            used = set()
            advanced = False
            refused = False
            while True:
                matching = [
                    (index, change)
                    for index, change in enumerate(changes)
                    if index not in used
                    and isinstance(change, dict)
                    and change.get("before") == current
                    and change.get("path") == expected_path
                ]
                if not matching:
                    break
                if len(matching) != 1:
                    refused = True
                    break
                index, change = matching[0]
                after = change.get("after")
                if (
                    not _valid_import_ownership_identity(after)
                    or not _post_import_change_within_item(queue_item, change)
                ):
                    refused = True
                    break
                used.add(index)
                current = dict(after)
                advanced = True
            if refused:
                refused_item = True
                break
            if advanced:
                record["file"] = current
        if refused_item:
            _clear_import_ownership(queue_item)
            continue
        if item_created_files:
            queue_item["_import_ownership_created_files"] = item_created_files
        else:
            queue_item.pop("_import_ownership_created_files", None)
        if (
            isinstance(queue_item.get("_import_ownership"), dict)
            and not _advance_created_directory_identities(
                queue_item, payload, validated_directory_changes)
        ):
            _clear_import_ownership(queue_item)


def _download_for_queue_item(item):
    """Download one queue item, writing n_ok/n_fail/etc back into the item.
    AuthLost and KeyboardInterrupt propagate to the caller; existing=None lets
    run_album_download read the on-disk tracks only if it reaches the
    backup-present-tracks branch."""
    run_album_download(
        album=item["album"],
        missing=item["missing"],
        present=item["present"],
        album_dir=item.get("album_dir"),
        snapshot=item["snapshot_before"],
        quality=item.get("quality"),
        upgrade_only=item["upgrade_only"],
        force_track_by_track=item.get("force_track_by_track", False),
        exact_edition=item.get("exact_edition", False),
        result=item,
        expected_gap_fill_receipts=item.get(
            "_validated_gap_fill_receipts"
        ),
    )


def _bind_queue_item_premise(item, premise):
    album_dir = item.get("album_dir")
    if album_dir is None:
        item["_validated_source_receipt"] = None
        item["_validated_gap_fill_receipts"] = None
        return None
    if (
        premise is None
        or premise["path"] != os.path.abspath(os.fspath(album_dir))
    ):
        raise CandidateStale(
            "The queued album path no longer matches its local receipt. "
            "Refresh or rescan."
        )
    item["_validated_source_receipt"] = premise["receipt"]
    item["_validated_gap_fill_receipts"] = item.get("_gap_fill_receipts")
    return premise


def _validate_queue_item_premise(item):
    """Recheck the exact approved local state when queued work starts."""
    album_dir = item.get("album_dir")
    if album_dir is None:
        clear_scan_caches()
        other = other_version_dir(item["album"])
        if other is not None and not names_other_version(item["album"], other):
            raise CandidateStale(
                "This album appeared locally after the download was queued. "
                "Refresh or rescan."
            )
        item["_validated_source_receipt"] = None
        item["_validated_gap_fill_receipts"] = None
        return None
    premise = validate_premise(item.get("_source_premise"))
    return _bind_queue_item_premise(item, premise)


def _admit_new_queue_items(items, token):
    """Validate, admit, and revalidate new selections before queue persistence."""
    items = list(items)
    for item in items:
        _validate_queue_item_premise(item)
    if items:
        token = api_client.authorize_bound_download(token)
    for item in items:
        _validate_queue_item_premise(item)
    return token


def _bind_resumed_queue_item_premise(item):
    """Keep the original seal for exact recovery after mutation began."""
    return _bind_queue_item_premise(
        item,
        canonical_premise(item.get("_source_premise")),
    )


def _staged_album_dirs(item):
    return validated_staged_album_dirs(item)


def _run_pre_import_hooks_for_dirs(
    album_dirs,
    args,
    *,
    on_sources_changed=None,
):
    """Run downsample + lyric hooks scoped to ``album_dirs``."""
    resampled_total = 0
    downsample_errors = 0
    downsample_saved_bytes = 0
    downsample_flush_warnings = 0
    downsample_cancelled = False
    # Before anything else looks at these directories: a multi-disc album's
    # cover sits in the album root, where beets' import task never searches.
    # Left there it strands in staging and reads as an unfinished download.
    for d in album_dirs:
        try:
            relocate_disc_album_artwork(d)
        except Exception as _ae:
            vlog(f"artwork relocation raised: {_ae}")
    if (cfg.DOWNSAMPLE_HIRES_ENABLED and HAVE_DOWNSAMPLE
            and not getattr(args, "no_downsample", False)):
        for d in album_dirs:
            try:
                res = downsample_dir(d, verbose=True, base_dir=d, log=log.info)
                resampled_total += (res or {}).get("resampled", 0)
                downsample_errors += (res or {}).get("errors", 0)
                downsample_saved_bytes += (res or {}).get("saved_bytes", 0)
                downsample_flush_warnings += (
                    (res or {}).get("flush_warnings", 0)
                )
                downsample_cancelled = (
                    downsample_cancelled
                    or bool((res or {}).get("cancelled", False))
                )
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW, "  ⚠  downsample hook interrupted"))
                raise
            except Exception as _ce:
                downsample_errors += 1
                log.info(fmt(C.YELLOW, f"  ⚠  downsample hook failed: {_ce}"))
        if on_sources_changed is not None:
            on_sources_changed(SourceTransitionKind.DOWNSAMPLE)
    for d in album_dirs:
        try:
            _run_lyric_hook(d)
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW, "  ⚠  lyric hook interrupted"))
            raise
        except Exception as _le:
            log.info(fmt(C.YELLOW, f"  ⚠  lyric hook failed: {_le}"))
    if on_sources_changed is not None:
        on_sources_changed(SourceTransitionKind.LYRICS_TAG)
    return PreImportHooksResult(
        resampled=resampled_total,
        errors=downsample_errors,
        saved_bytes=downsample_saved_bytes,
        flush_warnings=downsample_flush_warnings,
        cancelled=downsample_cancelled,
    )


def _pre_import_staging_hooks(args, album_dirs=None):
    """Compatibility wrapper for the single-album pre-import hooks."""
    return _run_pre_import_hooks_for_dirs(
        album_dirs or [cfg.STAGING_DIR], args)


def _pre_import_outcome_fields(prepared):
    return {
        "downsample_errors": getattr(prepared, "errors", 0),
        "downsample_saved_bytes": getattr(prepared, "saved_bytes", 0),
        "downsample_flush_warnings": getattr(prepared, "flush_warnings", 0),
        "downsample_cancelled": bool(getattr(prepared, "cancelled", False)),
    }


def _downsample_outcome_needs_attention(item):
    return bool(
        item.get("downsample_errors", 0)
        or item.get("downsample_flush_warnings", 0)
        or item.get("downsample_cancelled", False)
    )


def _queue_outcome_needs_attention(item):
    verdict = item.get("quality_verdict") or {}
    return bool(
        _downsample_outcome_needs_attention(item)
        or item.get("upgrade_unverified", False)
        or item.get("catalogue_unverified", False)
        or item.get("recovery_unverified", False)
        or item.get("_siblings_preserved")
        or (verdict.get("under") and not verdict.get("recovered"))
    )


def _import_album_with_retry(
        album_dirs, *, ownership_out=None, source_receipts=None, album_dir=None,
        places=None, folder=None, failure_out=None):
    """Run beets import on ``album_dirs`` with up to ``BEETS_MAX_ATTEMPTS``
    attempts, retrying only on idle-timeout (other failures aren't transient).
    Returns True on import success, False on permanent failure, and raises
    MusicDiskFull when beets stopped on a full disk. ``failure_out`` gets
    what beets_import_albums says about a failure."""
    if not album_dirs:
        return True
    if ownership_out is not None:
        ownership_out.clear()
    max_attempts = max(1, int(cfg.BEETS_MAX_ATTEMPTS))
    for attempt in range(1, max_attempts + 1):
        if source_receipts is not None and not all(
                tree_matches(receipt) for receipt in source_receipts):
            return False
        pin = {} if album_dir is None else {"album_dir": album_dir}
        if places:
            pin["places"] = places
        if folder is not None:
            pin["folder"] = folder
        if failure_out is not None:
            pin["failure_out"] = failure_out
        if ownership_out is None:
            kind = beets_import_albums(album_dirs, **pin)
            attempt_capture = None
        else:
            attempt_capture = {}
            try:
                kind = beets_import_albums(
                    album_dirs,
                    ownership_out=attempt_capture,
                    **pin,
                )
            except ImportedThenInterrupted:
                result = attempt_capture.get("result")
                if isinstance(result, dict):
                    ownership_out["result"] = result
                raise
        if kind == "ok":
            if ownership_out is not None:
                result = attempt_capture.get("result")
                if isinstance(result, dict):
                    ownership_out["result"] = result
            return True
        if kind == "timeout" and attempt < max_attempts:
            log.info(fmt(C.YELLOW,
                f"  ⏳ beets import timed out (attempt {attempt}/{max_attempts}); "
                f"pausing {int(cfg.BEETS_RETRY_PAUSE)}s before retry."))
            time.sleep(cfg.BEETS_RETRY_PAUSE)
            continue
        if kind == "disk_full":
            raise MusicDiskFull
        return False
    return False


def _move_to_beets_retry(album_dirs, label, *, expected=None):
    """Park exact failed-import trees with durable replay manifests."""
    album_dirs = [Path(directory) for directory in album_dirs]
    if not album_dirs:
        return []
    expected_provided = expected is not None
    expected_by_path = {
        receipt.path: receipt for receipt in (expected or ())
    }
    parked = []
    moved = 0
    for index, directory in enumerate(album_dirs):
        receipt = expected_by_path.get(directory)
        if expected_provided and receipt is None:
            log.info(fmt(C.YELLOW,
                f"  ⚠  couldn't set aside {directory.name}; no exact "
                "pre-import receipt was available, so it was left in staging."))
            continue
        group = park_trees(
            [directory],
            f"{label}-{index + 1}",
            kind="beets",
            expected=[receipt] if receipt is not None else None,
        )
        if group is not None:
            parked.append(group)
            moved += 1
        else:
            log.info(fmt(C.YELLOW,
                f"  ⚠  couldn't set aside {directory.name}; it changed "
                "during the import attempt and was left in staging."))
    if moved:
        log.info(fmt(C.YELLOW,
            f"  ⏭  set aside {plural(moved, 'album folder')} under "
            f"{cfg.BEETS_RETRY_DIR}/; "
            f"{'it' if moved == 1 else 'they'} will re-import on the next "
            "download run."))
    return parked


def _reimport_parked_albums():
    """Re-attempt beets import on albums an earlier batch parked under
    ``STAGING_DIR/<BEETS_RETRY_DIR>/``. A park almost always means a transient
    cause, such as a DB lock, idle timeout, or momentarily busy disk, so retrying
    the import (never the download; the files are already on disk) at the start
    of the next flush clears the backlog without re-fetching anything. Groups
    that still fail stay parked for the run after. Returns (anything_imported,
    landed_library_dirs): the flag drives the duplicate-album fold, and the
    resolved dirs feed the post-import lyric finaliser.

    A full disk stops the re-imports there: the OSError(ENOSPC) is raised
    with that pair for the albums before it as ``parked_results``."""
    groups = list_groups(kind="beets")
    any_ok = False
    landed_dirs = []
    for group in groups:
        album_dirs = [tree.path for tree in group.trees]
        log.info(fmt(C.GRAY,
            f"  ↻  Re-importing the album set aside: "
            f"{truncate(group.path.name, 50)}"))
        # Tag signatures survive beets moving/renaming the files, so the
        # landed folder is findable after the import for the lyric finaliser.
        sigs_by_dir = {d: track_signatures_for_album_dirs([d])
                       for d in album_dirs}
        disk_full = None
        try:
            _import_album_with_retry(
                album_dirs, source_receipts=group.trees)
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            disk_full = exc
        outcome = reconcile_imported_group(group)
        if outcome["moved_audio"]:
            any_ok = True
            for directory in album_dirs:
                try:
                    landed = find_album_dir_by_track_signatures(
                        sigs_by_dir.get(directory))
                except Exception as _e_sig:
                    landed = None
                    vlog(f"parked reimport: couldn't locate landed dir: {_e_sig}")
                if landed is not None:
                    landed_dirs.append(landed)
        if outcome["reason"] == "tracks":
            log.info(fmt(C.YELLOW,
                f"  ⏭  Set aside album in {truncate(group.path.name, 50)} still "
                "holds exact tracks; left for a later run."))
        elif outcome["reason"] in {"changed", "companions"}:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Kept recovery folder {group.path}: it contains "
                "unverified changes or companion files and won't be "
                "automatically replayed again."))
        if disk_full is not None:
            disk_full.parked_results = (any_ok, landed_dirs)
            raise disk_full
    return any_ok, landed_dirs


_RETRYABLE_STOP_RESULTS = {
    "cancelled", "interrupted", "auth_lost", "disk_full", "io_error",
    "upgrade_aborted_backup_failed", "import_failed",
}


def _queue_item_needs_retry(item):
    """Whether an item should stay queued for a later run."""
    if item.get("result") in _RETRYABLE_STOP_RESULTS:
        return True
    return item.get("n_ok", 0) == 0


def _resolve_queue_item(
    item,
    args,
    imported_globally,
    *,
    authority=None,
):
    """Resolve backup and compute result for one completed queue item.

    Handles art cleanup, multi-artist filing, sibling retention, and
    backup restoration. Mutates item["imported"] and
    item["_resolved_post_dir"].
    Returns a result dict in process_album's shape.
    """
    item["imported"] = imported_globally
    bp = item.get("backup_path")
    album_dir = item["album_dir"]

    def imported_dir():
        signatures = item.get("post_import_signatures")
        if album_dir is not None:
            # Gap Fill and Upgrade import into the album's own folder, and
            # another copy of the album can hold tracks with the same tags.
            found = find_album_dir_by_track_signatures(
                signatures, expected_dir=album_dir)
            if found is not None or item.get("exact_edition") or bp is not None:
                return found
        return find_album_dir_by_track_signatures(signatures)

    _sibs = item.get("siblings_to_delete", [])
    item["_siblings_preserved"] = [os.fspath(path) for path in _sibs]
    item["_siblings_kept"] = []

    # One strict per-album success flag covers art cleanup, sibling deletion,
    # backup resolution, and opt-in migration must all agree. A partial result
    # (e.g. 5/12 tracks ok) must not delete backups or siblings.
    _item_strict_success = (
        imported_globally
        and item.get("n_ok", 0) > 0
        and item.get("n_fail", 0) == 0
        and item.get("n_lossy", 0) == 0
    )
    ownership_scope_used = False
    ownership_source_dir = None
    ownership_source_required = (
        imported_globally
        and item.get("_capture_import_ownership") is True
    )
    if ownership_source_required:
        ownership_source_dir = _verified_import_album_dir(item)
        ownership_scope_used = ownership_source_dir is not None

    # A Track download or a repair keeps its album where it is.
    migration_requested = (
        getattr(args, "migrate_multi_artist", False)
        and _item_strict_success
        and not ownership_source_required
    )
    migration_source_dir = imported_dir() if migration_requested else None
    if migration_source_dir is not None:
        if authority is not None:
            _require_executor_authority(authority)
        _migrated = prompt_and_migrate_multi_artist_folder(
            item["album"],
            args,
            authority=authority,
            source_dir=migration_source_dir,
        )
        if authority is not None:
            _require_executor_authority(authority)
    else:
        _migrated = None
        if migration_requested:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  Multi-artist folder filing was skipped because the "
                "exact imported album could not be located.",
            ))
    post_dir = _migrated
    if post_dir is None and ownership_scope_used:
        post_dir = ownership_source_dir
    if (
        post_dir is None
        and imported_globally
        and not ownership_source_required
    ):
        post_dir = imported_dir()
    if post_dir is None and not ownership_source_required and not item.get("exact_edition"):
        post_dir = find_album_dir_filesystem(item["album"])
    # For brand-new albums (album_dir=None), find_album_dir_filesystem may
    # return None if the cache hasn't refreshed. Clear and retry once.
    if (
        post_dir is None
        and imported_globally
        and not ownership_source_required
        and not item.get("exact_edition")
    ):
        clear_scan_caches()
        post_dir = find_album_dir_filesystem(item["album"])
    if post_dir is None and not item.get("exact_edition"):
        post_dir = album_dir
    album_has_content = (
        post_dir is not None and post_dir.exists()
        and _count_audio_files_in(post_dir) > 0
    )
    # The post-import sidecar pass writes into this folder.
    item["_resolved_post_dir"] = post_dir
    item["_resolved_post_dir_from_import_ownership"] = (
        ownership_source_required
    )
    had_any_success = (
        imported_globally and item.get("n_ok", 0) > 0 and album_has_content
    )

    if had_any_success:
        if post_dir:
            if item.get("resampled_n", 0) > 0:
                mark_local_album_capped(post_dir, qobuz_album=item["album"],
                                        origin="auto")
        # A sibling is only eligible for retirement after a clean, complete
        # replacement. Bind each move to its pre-download receipt, carry its
        # unique companion files without overwrite, then retire only the exact
        # recovery backup while the replacement is held and revalidated.
        _album_tracks = (
            (item["album"].get("tracks") or {}).get("items") or []
        )
        _sibs_whole = (
            bool(_sibs)
            and not ownership_scope_used
            and folder_holds_all_tracks(
                post_dir, _album_tracks, destructive=True)
        )
        _sibs_clean = (item.get("n_fail", 0) == 0
                       and item.get("n_lossy", 0) == 0)
        if _sibs_clean and _sibs_whole:
            replacement_receipt = capture_album_source_receipt(post_dir)
            receipts = item.get("_sibling_cleanup_receipts")
            if not isinstance(receipts, tuple) or len(receipts) != len(_sibs):
                receipts = (None,) * len(_sibs)
            if replacement_receipt is None:
                log.info(fmt(
                    C.YELLOW,
                    f"  ⚠  Keeping {plural(len(_sibs), 'sibling folder')}; the "
                    "replacement couldn't be held as an exact safe tree.",
                ))
            else:
                for sib_dir, receipt in zip(_sibs, receipts):
                    sibling_path = Path(sib_dir)
                    name = sibling_path.name
                    if receipt is None:
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling {name}; it wasn't checked "
                            "before the download.",
                        ))
                        continue
                    covered = _sibling_tracks_all_kept(sibling_path, post_dir)
                    if covered is False:
                        item["_siblings_preserved"].remove(
                            os.fspath(sibling_path))
                        item["_siblings_kept"].append(name)
                        log.info(fmt(
                            C.GRAY,
                            f"  ·  Kept {name}; not every track in it "
                            f"matches one in {post_dir.name}.",
                        ))
                        continue
                    if covered is None:
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling {name}; its tracks couldn't "
                            f"be compared with {post_dir.name}.",
                        ))
                        continue
                    # Its own cover, log or lyrics would have to stay in
                    # the backup, away from the library, for good.
                    held = capture_album_source_receipt(sibling_path)
                    clashes = None if held is None else companions_in_the_way(
                        held, replacement_receipt)
                    if clashes is None:
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling {name}; its other files "
                            f"couldn't be compared with {post_dir.name}.",
                        ))
                        continue
                    if clashes:
                        item["_siblings_preserved"].remove(
                            os.fspath(sibling_path))
                        one = len(clashes) == 1
                        log.info(fmt(
                            C.GRAY,
                            f"  ·  Kept {name}; its {some_names(clashes)} "
                            f"{'differs' if one else 'differ'} from the "
                            f"{'one' if one else 'ones'} in {post_dir.name}.",
                        ))
                        continue
                    sibling_backup = backup_album_dir(
                        sibling_path, expected_receipt=receipt)
                    if sibling_backup is None:
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling {name}; its exact tree "
                            "changed or couldn't be moved.",
                        ))
                        continue
                    if sibling_backup.complete is not True:
                        from qobuz_librarian.modes.process import (
                            _recover_incomplete_upgrade_backup,
                        )

                        _recover_incomplete_upgrade_backup(
                            sibling_backup,
                            sibling_path,
                            operation="sibling retirement backup",
                        )
                        log.info(fmt(
                            C.RED,
                            "  ✗  Sibling retirement stopped part-way; "
                            "the original reconciliation is reported above.",
                        ))
                        break
                    carried_receipt = carry_backup_companions(
                        sibling_backup,
                        post_dir,
                        expected_replacement_receipt=replacement_receipt,
                    )
                    if carried_receipt is None:
                        if not pin_unverified_upgrade_backup(
                                sibling_backup,
                                "sibling backup kept; companion carry was "
                                "not exact"):
                            warn_pin_failed(sibling_backup)
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling recovery for {name} at "
                            f"{sibling_backup.path}; its companion files "
                            "couldn't be carried without uncertainty.",
                        ))
                        break
                    replacement_receipt = carried_receipt
                    # With no entries to retire, the kept album needn't be
                    # one Beets lists whole: it may know only the new tracks.
                    listed = backup_listed_in_beets(sibling_backup)
                    if listed is not False and not retire_backup_beets_entries(
                        sibling_backup,
                        post_dir,
                        replacement_receipt,
                    ):
                        if not pin_unverified_upgrade_backup(
                                sibling_backup,
                                "sibling backup kept; the old Beets "
                                "entries could not be removed"):
                            warn_pin_failed(sibling_backup)
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling recovery for {name} at "
                            f"{sibling_backup.path}; its Beets catalogue "
                            "couldn't be reconciled.",
                        ))
                        break
                    retired = dispose_backup(
                        sibling_backup,
                        replacement_path=post_dir,
                        expected_replacement_receipt=replacement_receipt,
                        replacement_validator=lambda replacement, held: (
                            folder_holds_all_tracks(
                                replacement, _album_tracks,
                                destructive=True)
                            and _sibling_tracks_all_kept(
                                held, replacement) is True
                        ),
                    )
                    if not retired:
                        if not pin_unverified_upgrade_backup(
                                sibling_backup,
                                "sibling backup kept; final exact "
                                "replacement proof did not hold"):
                            warn_pin_failed(sibling_backup)
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling recovery for {name} at "
                            f"{sibling_backup.path}; the final exact "
                            "replacement proof did not hold.",
                        ))
                        break
                    try:
                        item["_siblings_preserved"].remove(
                            os.fspath(sibling_path))
                    except ValueError:
                        pass
                    log.info(fmt(
                        C.GREEN,
                        f"  ✓  Consolidated sibling {name}.",
                    ))
        elif _sibs and _sibs_clean:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Keeping {plural(len(_sibs), 'sibling folder')}; the filled "
                f"folder couldn't be verified as holding every track, and a "
                f"sibling may hold the only copy of what's missing."))
        elif _sibs:
            log.info(fmt(C.GRAY,
                f"  · Keeping {plural(len(_sibs), 'sibling folder')}"
                f"; partial result (n_fail={item.get('n_fail', 0)}, "
                f"n_lossy={item.get('n_lossy', 0)})"))
    item.pop("_sibling_cleanup_receipts", None)

    if bp is not None:
        # Backup resolution uses stricter success than art/sibling cleanup.
        # the backup is the only intact copy of pre-upgrade content, so only
        # drop it when the new folder is whole (n_fail == 0 AND n_lossy == 0).
        # _item_strict_success is the download-and-import-was-clean signal,
        # independent of whether we then relocated the imported folder.
        if (
            _item_strict_success
            and album_has_content
            and not ownership_scope_used
        ):
            # bp is set only for an auto-upgrade, which wiped the only full copy,
            # so clear the same bar process.py does before deleting the backup:
            # the rebuilt folder must be verifiably at least as complete as the
            # original (track count + playtime), not merely decode-clean. The
            # artist/upgrade walks run their bulk upgrades through this executor,
            # so without this they'd skip the completeness gate.
            from qobuz_librarian.modes.process import (
                _carry_non_audio_from_backup,
                _keep_backup_for_companions,
                _put_original_back,
                _replacement_audio_paths,
                _upgrade_replacement_verified,
                _upgrade_trees_verified,
            )
            replaced_entries = item.get("_replaced_beets_entries")
            if _upgrade_replacement_verified(item["album"], album_dir, bp):
                # Carry non-audio companions (booklets, scans, .cue/.log,
                # hand-placed cover art) from the backup into the rebuilt album
                # before deleting it. The audio-only completeness gate ignores
                # them, so they'd be lost with the backup. Mirrors the single-
                # album CLI path in modes/process.py; the bulk artist/upgrade
                # walks and the web Upgrade action run through this executor.
                # A failed carry keeps the backup: it holds the only copies.
                carried = _carry_non_audio_from_backup(
                        item["album"], album_dir, bp,
                        replacement_dir=post_dir)
                if carried is not None:
                    replacement_path, replacement_receipt, left = carried
                    if not (
                        retire_replaced_beets_entries(
                            replaced_entries,
                            replacement_path,
                            _replacement_audio_paths(
                                replacement_path, replacement_receipt),
                        )
                        if replaced_entries is not None
                        else retire_backup_beets_entries(
                            bp,
                            replacement_path,
                            replacement_receipt,
                        )
                    ):
                        item["upgrade_unverified"] = True
                        item["catalogue_unverified"] = True
                        if not pin_unverified_upgrade_backup(
                                bp,
                                "upgrade kept; the old Beets entries "
                                "could not be removed"):
                            warn_pin_failed(bp)
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  Couldn't reconcile the Beets "
                            f"catalogue for {truncate(album_dir.name, 40)}; "
                            "keeping the exact backup."))
                    elif left:
                        item["companions_kept"] = left
                        log.info(fmt(C.YELLOW,
                            _keep_backup_for_companions(bp, left)))
                    elif not dispose_backup(
                        bp,
                        replacement_path=replacement_path,
                        expected_replacement_receipt=replacement_receipt,
                        replacement_validator=_upgrade_trees_verified,
                    ):
                        item["upgrade_unverified"] = True
                        if not pin_unverified_upgrade_backup(
                                bp,
                                "upgrade kept; final exact replacement "
                                "proof did not hold"):
                            warn_pin_failed(bp)
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  Couldn't remove the exact backup "
                            f"for {truncate(album_dir.name, 40)}."))
                else:
                    item["upgrade_unverified"] = True
                    if not pin_unverified_upgrade_backup(bp):
                        warn_pin_failed(bp)
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  {truncate(album_dir.name, 40)}: upgraded, but "
                        "the rebuilt album or its companions couldn't be "
                        "confirmed written to disk; keeping the backup."))
                    log.info(fmt(C.GRAY,
                        f"     Backup at {bp}; keep it until you've confirmed "
                        "the rebuilt album is safe."))
            else:
                put_back = _put_original_back(bp, album_dir)
                if put_back is not None:
                    # The original is back and the new copy is out of the
                    # library, so nothing was upgraded and nothing waits on a
                    # backup.
                    item["result"] = "incomplete"
                    item["upgrade_put_back"] = True
                    imported_globally = item["imported"] = False
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  {truncate(album_dir.name, 40)}: upgrade "
                        "couldn't be verified as complete, so your original "
                        "album was put back."))
                    if put_back is not True:
                        log.info(fmt(C.GRAY,
                            f"     The new download was set aside at "
                            f"{put_back}."))
                    if replaced_entries is not None:
                        forget_imported_beets_entries(replaced_entries, album_dir)
                else:
                    # The walk still flags the album: its upgrade did not happen.
                    item["upgrade_unverified"] = True
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  {truncate(album_dir.name, 40)}: upgrade "
                        "couldn't be verified as complete, and your original "
                        "couldn't be put back automatically."))
                    log.info(fmt(C.GRAY,
                        f"     Original preserved at {bp}; it is not removed "
                        "automatically."))
        elif _item_strict_success:
            # The download and import succeeded cleanly, but the imported album
            # couldn't be relocated (beets renamed the folder past what the
            # matcher found), so post_dir fell back to the original we'd moved
            # aside. Restoring it now would duplicate the content beside the
            # fresh import. Keep the backup and let the user reconcile.
            item["upgrade_unverified"] = True
            if not pin_unverified_upgrade_backup(
                    bp,
                    "upgrade backup kept; the clean imported replacement "
                    "could not be located"):
                warn_pin_failed(bp)
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: imported, but the new "
                f"folder couldn't be located; keeping the backup rather than "
                f"restoring it as a duplicate."))
            log.info(fmt(C.GRAY,
                f"     Backup at {bp}; remove it once you've confirmed the "
                f"upgrade landed."))
        elif args.no_import:
            item["upgrade_unverified"] = True
            if not pin_unverified_upgrade_backup(
                    bp,
                    "upgrade backup kept; --no-import leaves the replacement "
                    "outside the verified library flow"):
                warn_pin_failed(bp)
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: backup kept at {bp}"))
        else:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: upgrade did not succeed; restoring..."))
            if restore_upgrade_backup(bp, album_dir):
                log.info(fmt(C.GREEN, f"  ✓  Restored {truncate(album_dir.name, 50)}"))
            else:
                item["upgrade_unverified"] = True
                if not pin_unverified_upgrade_backup(
                        bp,
                        "upgrade backup kept; automatic restore did not "
                        "complete"):
                    warn_pin_failed(bp)
                log.info(fmt(C.RED, f"  ✗  Auto-restore failed. Backup: {bp}"))
                log.info(fmt(C.WHITE,
                    f"     To put it back, move anything left in {album_dir} "
                    "out of the library, then move the backup's files, not "
                    "its dot-files, into it."))

    # Gap-fill backup: present tracks moved to backup before rip.
    # Drop on success; restore in place if the queue item failed.
    gfb = item.get("gap_fill_backup_path")
    if gfb is not None and gfb.exists():
        # Drop the moved-aside present tracks only when the re-rip imported
        # cleanly. _item_strict_success requires n_fail == 0 and n_lossy ==
        # 0, so a lossy/short re-rip of a present track can't clear the
        # original (mirrors the full-album gap-fill gate in process.py). That
        # signal is independent of whether we then *located* the filled
        # folder, which matters: a clean import beets filed where the matcher
        # can't find it must NOT be treated as a failure and restored over
        # itself.
        _filled_whole = (
            not ownership_scope_used
            and album_has_content
            and folder_holds_all_tracks(
                post_dir,
                (item["album"].get("tracks") or {}).get("items") or [],
                destructive=True,
                edition=item["album"] if item.get("exact_edition") else None,
            )
        )
        _filled_receipt = (
            capture_album_source_receipt(post_dir)
            if _item_strict_success and _filled_whole else None
        )
        if (_item_strict_success and _filled_whole and _filled_receipt is not None
                and not backup_files_preserved(post_dir, gfb)):
            item["recovery_unverified"] = True
            if not pin_unverified_upgrade_backup(
                    gfb, "gap-fill backup kept; original files differ from the import"):
                warn_pin_failed(gfb)
            log.info(fmt(C.YELLOW,
                "  ⚠  Gap-fill landed, but the original files differ; "
                "keeping the backup."))
        elif _item_strict_success and _filled_whole and _filled_receipt is not None:
            if not retire_backup_beets_entries(
                gfb,
                post_dir,
                _filled_receipt,
                **({"replacement_audio_paths": [t["path"] for t in edition_files(
                    item["album"], read_album_dir(post_dir), post_dir)]}
                   if item.get("exact_edition") else {}),
            ):
                item["recovery_unverified"] = True
                item["catalogue_unverified"] = True
                if not pin_unverified_upgrade_backup(
                        gfb,
                        "gap-fill backup kept; the old Beets entries "
                        "could not be removed"):
                    warn_pin_failed(gfb)
                log.info(fmt(C.YELLOW,
                    "  ⚠  Gap-fill complete but its Beets catalogue "
                    "couldn't be reconciled; keeping the exact backup."))
            elif not dispose_backup(
                gfb,
                replacement_path=post_dir,
                expected_replacement_receipt=_filled_receipt,
                replacement_validator=lambda replacement, backup: (
                    folder_holds_all_tracks(
                        replacement,
                        (item["album"].get("tracks") or {}).get("items") or [],
                        destructive=True,
                        edition=item["album"] if item.get("exact_edition") else None,
                        folder_label=post_dir.name,
                    )
                    and backup_files_preserved(replacement, backup)
                ),
            ):
                item["recovery_unverified"] = True
                if not pin_unverified_upgrade_backup(
                        gfb,
                        "gap-fill backup kept; final exact replacement "
                        "proof did not hold"):
                    warn_pin_failed(gfb)
                log.info(fmt(C.YELLOW,
                    "  ⚠  Gap-fill complete but the exact backup couldn't "
                    "be removed."))
        elif _item_strict_success and _filled_whole:
            item["recovery_unverified"] = True
            if not pin_unverified_upgrade_backup(
                    gfb, "gap-fill backup kept; replacement not confirmed written to disk"):
                warn_pin_failed(gfb)
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: filled, but the new "
                "folder couldn't be confirmed written to disk; keeping the backed-up "
                "tracks."))
            log.info(fmt(C.GRAY, f"     Backup at {gfb}."))
        elif _item_strict_success:
            # Imported cleanly, but either beets filed the album where the
            # matcher couldn't find it (renamed past the fuzzy gate, or under
            # an unexpected albumartist) or the located folder is short of the
            # full track count. Restoring the backed-up tracks here would
            # strand them beside the fresh import and destroy the only backup
            # Keep it and let the user reconcile, exactly as the upgrade
            # branch above does.
            item["recovery_unverified"] = True
            if not pin_unverified_upgrade_backup(
                    gfb,
                    "gap-fill backup kept; the clean imported replacement "
                    "could not be confirmed whole"):
                warn_pin_failed(gfb)
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: filled, but the new "
                f"folder couldn't be confirmed whole; keeping the backed-up "
                f"tracks rather than restoring them as a duplicate."))
            log.info(fmt(C.GRAY,
                f"     Backup at {gfb}; remove it once you've confirmed the "
                f"fill landed."))
        else:
            _restore_target = album_dir or post_dir
            if _restore_target is None:
                item["recovery_unverified"] = True
                if not pin_unverified_upgrade_backup(
                        gfb,
                        "gap-fill backup kept; no bound album path was "
                        "available for restore"):
                    warn_pin_failed(gfb)
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Gap-fill failed and no album dir to restore to. "
                    f"Backed-up tracks at: {gfb}"))
            else:
                if args.no_import:
                    # --no-import skips beets, so the success gate is always
                    # False here even when the download landed fine. Restoring
                    # the moved-aside present tracks is right, but it's not a
                    # failure.
                    log.info(fmt(C.GRAY,
                        f"  · {truncate(_restore_target.name, 40)}: --no-import "
                        f"set; restoring the backed-up tracks to the library."))
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  {truncate(_restore_target.name, 40)}: gap-fill "
                        f"did not succeed; restoring backed-up tracks…"))
                _n_back = restore_gap_fill_backup(gfb, _restore_target,
                                                  keep_larger_dst=False)
                if _n_back and not gfb.exists():
                    log.info(fmt(C.GREEN,
                        f"  ✓  Restored {plural(_n_back, 'track')} to "
                        f"{truncate(_restore_target.name, 50)}"))
                else:
                    item["recovery_unverified"] = True
                    if gfb.exists() and not pin_unverified_upgrade_backup(
                            gfb,
                            "gap-fill backup kept; automatic restore was "
                            "partial or failed"):
                        warn_pin_failed(gfb)
                    log.info(fmt(C.RED,
                        f"  ✗  Restored {plural(_n_back, 'track')}; remaining originals "
                        f"are preserved at:\n     {gfb}"))

    n_ok = item.get("n_ok", 0)
    n_fail = item.get("n_fail", 0)
    n_retryable, n_unretryable = incomplete_track_counts(item)
    outcome_attention = _queue_outcome_needs_attention(item)
    # A stop marker (cancel or an undeliverable album discarded the staged
    # files; disk-full / I/O / auth stopped the batch) must keep its label.
    # n_ok still counts the tracks that briefly landed, so the n_ok branch
    # below would mislabel a discarded cancel as "downloaded" in its result.
    _stop = item.get("result")
    if _stop in (
            "cancelled", "incomplete", "interrupted", "disk_full", "io_error",
            "auth_lost", "import_failed", "upgrade_aborted_backup_failed",
            "stale_candidate", "disk_refused", "folder_read_only"):
        status = _stop
    elif n_ok and (n_retryable or n_unretryable or outcome_attention):
        status = "partial"
    elif n_ok:
        status = "downloaded"
    elif n_retryable or n_unretryable:
        status = "failed"
    else:
        status = "nothing_landed"

    return {
        "dir": album_dir,
        "result": status,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_lossy": item.get("n_lossy", 0),
        **shortfall_fields(item),
        "failed_tracks": item.get("failed_tracks", []),
        "lossy_tracks": item.get("lossy_tracks", []),
        "broken_tracks": item.get("broken_tracks", []),
        "downsample_errors": item.get("downsample_errors", 0),
        "downsample_saved_bytes": item.get("downsample_saved_bytes", 0),
        "downsample_flush_warnings": item.get(
            "downsample_flush_warnings", 0
        ),
        "downsample_cancelled": bool(item.get("downsample_cancelled", False)),
        "upgrade_unverified": bool(item.get("upgrade_unverified", False)),
        "upgrade_put_back": bool(item.get("upgrade_put_back", False)),
        "catalogue_unverified": bool(item.get("catalogue_unverified", False)),
        "recovery_unverified": bool(item.get("recovery_unverified", False)),
        "import_denied": item.get("import_denied"),
        "folder_denied": item.get("folder_denied"),
        "quality_verdict": item.get("quality_verdict"),
        "siblings_preserved": item.get("_siblings_preserved", []),
        "siblings_kept": item.get("_siblings_kept", []),
        "imported": imported_globally,
        "auto_upgrade": item["auto_upgrade"],
        "elapsed_s": int(item.get("elapsed", 0)),
        "album": item["album"],
    }


def _sleep_unless_cancelled(seconds, cancel_check, step=0.5):
    """Sleep up to ``seconds``, but wake the moment a cancel lands. The inter-album
    cooldown after a rate-limited rip is RATE_LIMIT_COOLDOWN (30s by default), and
    a plain sleep would swallow a Stop for that whole window. Returns True if cut
    short by a cancel."""
    deadline = time.monotonic() + seconds
    while True:
        if cancel_check():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(step, remaining))


def _record_terminal_downloads(changes, *, no_import=False):
    """Give the terminal's finished, failed and stopped downloads their row in
    the activity record, and report each to POST_JOB_HOOK. With ``no_import``
    a download that landed whole is done, kept in staging.

    Queue and History read the job archive, which otherwise only the web
    writes. Bookkeeping never changes an already-completed download, so a
    failure to write is logged for --verbose and nothing else.
    """
    from qobuz_librarian.web import download_outcomes, job_persistence

    finished = time.time()
    recorded = False
    for change in changes:
        album = change.get("album") or {}
        album_id = str(album.get("id") or "").strip()
        if not album_id:
            continue
        stopped = change.get("result") == "interrupted"
        kept = no_import and change.get("result") == "downloaded"
        status = ("canceled" if stopped
                  else "done" if kept
                  else download_job_outcome(change)[0])
        error = None
        if stopped:
            summary = "Stopped before it finished."
        elif kept:
            summary = (f"{plural(change.get('n_ok', 0), 'track')} downloaded, "
                       "not imported.")
        else:
            summary = download_outcomes._summarize_download_result(change)
            error = download_outcomes.download_error(
                change, job_controls=False)
        artist = (album.get("artist") or {}).get("name") or ""
        try:
            saved = job_persistence.record_terminal_download(
                album_id=album_id,
                artist=artist,
                title=album.get("title") or "",
                summary=summary,
                started_at=finished - float(change.get("elapsed_s", 0) or 0),
                finished_at=finished,
                edition=album.get("version") or "",
                result=change,
                status=status,
                error=error,
            )
        except Exception as exc:
            saved = None
            vlog(f"activity record write failed: {exc}")
        if not saved:
            vlog(f"activity record write failed for album {album_id}")
        else:
            recorded = True
        post_job_hook.run(post_job_hook.payload(
            event_id=saved or album_id, status=status,
            title=album.get("title") or "", edition=album.get("version") or "",
            artist=artist, error=error, finished_at=finished,
            tracks_missing=download_result.tracks_missing(change),
            kind="download", summary=summary))
    if recorded:
        job_persistence.prune_finished(
            job_persistence.FINISHED_HISTORY_KEEP
        )


def _landed(change):
    return bool(change.get("imported")) and change.get("n_ok", 0) > 0


def _record_terminal_results(changes, args):
    """History rows and POST_JOB_HOOK calls for the albums that landed, failed,
    were stopped or had duplicate folders consolidated."""
    _record_terminal_downloads([
        change for change in changes
        if _landed(change) or change.get("result") == "interrupted"
        or download_job_outcome(change)[0] == "failed"
        or change.get("consolidated_tracks_removed")
        or change.get("consolidation_failed")
    ], no_import=getattr(args, "no_import", False))


def _refresh_review_state_after_downloads(
    changes,
    token,
    args,
    *,
    allow_upgrade_refresh=True,
    record=True,
):
    """Record finished terminal downloads in History and bring the saved
    Library views up to date with the albums that landed. A bookkeeping
    failure never changes the already-completed download result."""
    successful = [change for change in changes if _landed(change)]
    if record:
        _record_terminal_results(changes, args)
    saved_views.refresh_after_changes(
        successful, token, args, upgrade=allow_upgrade_refresh,
        claims_album=True)


def _require_executor_authority(authority):
    if (
        authority is None
        or type(authority) is not run_lock.RunLockLease
        or authority.intact() is not True
    ):
        raise DurableAlbumUnavailable(
            "the run lock is unavailable; no queue work was started"
        )


def _durable_execution_origin():
    """Bind durable work and its acknowledgement to one exact owner."""
    jobs_module = sys.modules.get("qobuz_librarian.web.jobs")
    if jobs_module is not None:
        current_job_id = getattr(jobs_module, "current_job_id", None)
        if callable(current_job_id):
            job_id = current_job_id()
            if job_id is not None:
                current_album_id = getattr(
                    jobs_module,
                    "current_job_album_id",
                    None,
                )
                acknowledge_completion = getattr(
                    jobs_module,
                    "acknowledge_current_job_durable_completion",
                    None,
                )
                return (
                    CompletionOrigin(CompletionOriginKind.WEB_JOB, job_id),
                    f"web-job:{job_id}",
                    (
                        current_album_id()
                        if callable(current_album_id)
                        else None
                    ),
                    (
                        acknowledge_completion
                        if callable(acknowledge_completion)
                        else None
                    ),
                )
    return (
        CompletionOrigin(CompletionOriginKind.CLI, "download-queue"),
        "cli:download-queue",
        None,
        None,
    )


def _durable_plan_allowed(items, item, *, execution_mode, web_album_id):
    """Keep the first Web lane to one job-bound, canonical album only."""
    if not execution_mode.startswith("web-job:"):
        return True
    item_album_id = normalise_album_id(item["album"].get("id"))
    return (
        len(items) == 1
        and item_album_id is not None
        and normalise_album_id(web_album_id) == item_album_id
    )


def _recovered_completion_details(
    item,
    *,
    expected_origin,
    execution_mode,
    origin,
    owner,
    album_id,
    planned,
    post_dir,
):
    """Validate one recovery callback against its journal and caller item."""
    try:
        caller_planned = queue_state._serialize_queue_item(item)
    except (KeyError, TypeError, ValueError):
        return None
    expected_album_id = normalise_album_id(item["album"].get("id"))
    if (
        type(origin) is not CompletionOrigin
        or origin != expected_origin
        or type(owner) is not RecoveryOwner
        or expected_album_id is None
        or normalise_album_id(album_id) != expected_album_id
        or planned != caller_planned
        or type(post_dir) is not str
        or not os.path.isabs(post_dir)
        or "\x00" in post_dir
    ):
        return None

    loaded = queue_state.load_queue_journal(owner.operation_id)
    journal = loaded.journal
    if (
        loaded.status is not queue_state.QueueLoadStatus.READY
        or journal is None
        or (
            execution_mode.startswith("web-job:")
            and journal.mode != execution_mode
        )
        or (
            not execution_mode.startswith("web-job:")
            and journal.mode.startswith("web-job:")
        )
    ):
        return None
    saved = next(
        (entry for entry in journal.items if entry.item_id == owner.item_id),
        None,
    )
    retirement = next(
        (
            entry
            for entry in journal.retirements
            if entry.item_id == owner.item_id
        ),
        None,
    )
    if saved is not None:
        if (
            saved.phase not in {
                queue_state.QueuePhase.RESOLVING,
                queue_state.QueuePhase.COMPLETE,
            }
            or saved.planned != planned
        ):
            return None
        completion_record = saved.completion_input
    elif retirement is not None:
        if (
            retirement.planned != planned
            or retirement.action is not None
            or retirement.final_path != post_dir
        ):
            return None
        completion_record = retirement.completion_input
    else:
        return None
    completion_input = parse_completion_input_record(
        completion_record,
        expected_owner=owner,
    )
    coverage = (
        completion_input.download_coverage()
        if completion_input is not None
        else None
    )
    if (
        coverage is None
        or completion_input.origin != origin
        or coverage.album_id != expected_album_id
    ):
        return None
    return (
        Path(post_dir),
        len(coverage.bindings),
        coverage.counts.failed,
        coverage.counts.lossy,
    )


def _recovered_owner_settled(owner):
    """Confirm recovery removed both the item and its carrier retirement."""
    if type(owner) is not RecoveryOwner:
        return False
    loaded = queue_state.load_queue_journal(owner.operation_id)
    if loaded.status is queue_state.QueueLoadStatus.ABSENT:
        return True
    journal = loaded.journal
    return (
        loaded.status is queue_state.QueueLoadStatus.READY
        and journal is not None
        and all(entry.item_id != owner.item_id for entry in journal.items)
        and all(
            retirement.item_id != owner.item_id
            for retirement in journal.retirements
        )
    )


def _exact_resume_owner(recovery, item, *, execution_mode):
    """Match one returned startup identity to this exact planned item.

    Returns the recovered action alongside the owner: a merely PENDING entry is
    a persisted decision the caller can hand back, an in-flight one is not.
    """
    if recovery.status is StartupRecoveryStatus.CLEAR:
        return None, execution_mode, None
    if recovery.status is StartupRecoveryStatus.ATTENTION_REQUIRED:
        raise DurableAlbumUnavailable(
            recovery.reason or "saved queue recovery needs attention"
        )
    if recovery.status is not StartupRecoveryStatus.RESUME_REQUIRED:
        raise DurableAlbumUnavailable("saved queue recovery is unavailable")

    try:
        planned = queue_state._serialize_queue_item(item)
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableAlbumUnavailable(
            "the queued album no longer matches its saved plan"
        ) from exc

    web_execution = execution_mode.startswith("web-job:")
    candidates = []
    for recovered in recovery.items:
        if recovered.action not in {
            StartupRecoveryAction.PENDING,
            StartupRecoveryAction.RESUME_DOWNLOAD,
        }:
            continue
        if web_execution:
            if recovered.mode != execution_mode:
                continue
        elif recovered.mode.startswith("web-job:"):
            continue
        loaded = queue_state.load_queue_journal(recovered.operation_id)
        journal = loaded.journal
        if (
            loaded.status is not queue_state.QueueLoadStatus.READY
            or journal is None
            or journal.mode != recovered.mode
        ):
            continue
        saved = next(
            (
                entry
                for entry in journal.items
                if entry.item_id == recovered.item_id
            ),
            None,
        )
        if (
            saved is not None
            and saved.phase is recovered.phase
            and saved.planned == planned
        ):
            candidates.append(recovered)

    if len(candidates) != 1:
        raise DurableAlbumUnavailable(
            "the exact saved queue item cannot resume"
        )
    recovered = candidates[0]
    return (
        RecoveryOwner(recovered.operation_id, recovered.item_id),
        recovered.mode,
        recovered.action,
    )


def _release_unplannable_claim(owner, action, *, authority):
    """Hand a saved entry the durable lane can't plan back to the legacy lane.

    A PENDING entry holds no recovery references and no frozen completion input
    because startup recovery classifies it PENDING, so there is
    nothing to resume and the entry can simply stay pending until the shrinking
    queue drops it. An ACTIVE claim that never reached a mutation gate goes back
    through the journal's own reset, which refuses the moment anything moved.
    """
    if action is StartupRecoveryAction.PENDING:
        return True
    if action is not StartupRecoveryAction.RESUME_DOWNLOAD:
        return False
    loaded = queue_state.load_queue_journal(owner.operation_id)
    if (
        loaded.status is not queue_state.QueueLoadStatus.READY
        or loaded.journal is None
    ):
        return False
    _require_executor_authority(authority)
    try:
        queue_state.reset_unstarted_item_to_pending(
            loaded.journal,
            owner.item_id,
        )
    except (KeyError, OSError, queue_state.QueueJournalError):
        return False
    _require_executor_authority(authority)
    return True


def _durable_completed_result(item, post_dir):
    """Publish the ordinary queue result shape without heuristic resolution."""
    item["imported"] = True
    item["_resolved_post_dir"] = post_dir
    item["_resolved_post_dir_from_import_ownership"] = False
    item["_siblings_preserved"] = []
    n_ok = item.get("n_ok", 0)
    n_fail = item.get("n_fail", 0)
    n_retryable, n_unretryable = incomplete_track_counts(item)
    outcome_attention = _queue_outcome_needs_attention(item)
    if n_ok and (n_retryable or n_unretryable or outcome_attention):
        status = "partial"
    elif n_retryable or n_unretryable:
        status = "failed"
    else:
        status = "downloaded"
    return {
        "dir": post_dir,
        "result": status,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_lossy": item.get("n_lossy", 0),
        **shortfall_fields(item),
        "failed_tracks": item.get("failed_tracks", []),
        "lossy_tracks": item.get("lossy_tracks", []),
        "broken_tracks": item.get("broken_tracks", []),
        "downsample_errors": item.get("downsample_errors", 0),
        "downsample_saved_bytes": item.get("downsample_saved_bytes", 0),
        "downsample_flush_warnings": item.get(
            "downsample_flush_warnings", 0
        ),
        "downsample_cancelled": bool(item.get("downsample_cancelled", False)),
        "upgrade_unverified": bool(item.get("upgrade_unverified", False)),
        "catalogue_unverified": bool(item.get("catalogue_unverified", False)),
        "recovery_unverified": bool(item.get("recovery_unverified", False)),
        "quality_verdict": item.get("quality_verdict"),
        "siblings_preserved": [],
        "imported": True,
        "auto_upgrade": bool(item.get("auto_upgrade")),
        "elapsed_s": int(item.get("elapsed", 0)),
    }


def _queue_done_line(results, n_items, *, stopped=False):
    """Colour and text for the closing queue summary.

    A parked, blocked, cancelled, or interrupted album downloads its tracks
    without one of them failing, so the verdict reads the album tally as well
    as the track tally. A cancelled album is tallied as cancelled rather than
    by its tracks. An album Qobuz could not deliver whole was discarded, so
    every track it asked for is still unresolved. An Upgrade that kept the
    original, and an album whose files changed after it was queued, were
    left as they are, with nothing to retry. An Upgrade whose new copy is in
    the library without the old album's own files has nothing to retry
    either.
    """
    n_success = sum(1 for r in results if r.get("result") == "downloaded")
    n_cancelled = sum(1 for r in results if r.get("result") == "cancelled")
    n_left = sum(1 for r in results if _left_as_it_is(r))
    n_own_left = sum(1 for r in results if r.get("companions_unsettled"))
    n_total_ok = n_total_missing = 0
    for r in results:
        if r.get("result") == "cancelled" or _left_as_it_is(r):
            continue
        missing = sum(incomplete_track_counts(r))
        if r.get("result") == "incomplete":
            missing += r.get("n_ok", 0)
        else:
            n_total_ok += r.get("n_ok", 0)
        n_total_missing += missing
    n_unfinished = max(
        0, n_items - n_success - n_cancelled - n_left - n_own_left)
    clear = (n_total_missing == 0 and n_unfinished == 0 and n_cancelled == 0
             and n_left == 0 and n_own_left == 0)
    text = (f"  {'✓' if clear else '⚠'} Queue {'stopped' if stopped else 'done'}: "
            f"{n_success}/{n_items} album{'s' if n_items != 1 else ''} OK")
    # A stop tallies finished albums only, so its zeros say nothing about
    # the album it cut short.
    if n_total_ok or not stopped:
        text += f" · {plural(n_total_ok, 'track')} downloaded"
    if n_total_missing or not stopped:
        text += f" · {plural(n_total_missing, 'track')} unresolved"
    if n_cancelled:
        text += (f" · {n_cancelled} album"
                 f"{'s' if n_cancelled != 1 else ''} cancelled")
    if n_left:
        text += (f" · {plural(n_left, 'album')} left as "
                 f"{'it is' if n_left == 1 else 'they are'}")
    if n_own_left:
        text += (f" · {plural(n_own_left, 'album')} upgraded, your own "
                 "files from the old copy kept in a backup")
    if n_unfinished:
        text += (f" · {n_unfinished} album"
                 f"{'s' if n_unfinished != 1 else ''} unfinished")
    return (C.GREEN if clear else C.YELLOW), text


_FINISHING_UP = (
    "\n    Interrupted. Finishing up for the albums that landed.")


def _left_as_it_is(result):
    return result.get("result") == "stale_candidate" or (
        result.get("result") == "incomplete" and result.get("upgrade_put_back"))


def _durable_completion_line(result, elapsed):
    n_ok = result.get("n_ok", 0)
    if result.get("result") == "downloaded":
        return C.GREEN, f"    ✓ {plural(n_ok, 'track')} · {int(elapsed)}s"

    details = []
    unresolved = sum(incomplete_track_counts(result))
    if unresolved:
        details.append(f"{unresolved} unresolved")
    errors = result.get("downsample_errors", 0)
    if errors:
        details.append(f"{errors} failed to downsample")
    flush_warnings = result.get("downsample_flush_warnings", 0)
    if flush_warnings:
        details.append(plural(flush_warnings, "disk write warning"))
    if result.get("downsample_cancelled", False):
        details.append("downsample stopped")
    detail = " · ".join(details) or "needs attention"
    colour = C.YELLOW if n_ok else C.RED
    symbol = "⚠" if n_ok else "✗"
    return colour, f"    {symbol} {plural(n_ok, 'track')} · {detail} · {int(elapsed)}s"


_NEXT_ALBUM = object()
_STOP_QUEUE = object()


class _QueueFlush:
    """One flush of the download queue, an album at a time."""

    def __init__(self, queue, args, token, *, on_progress, refresh_review,
                 consolidate_duplicates, results_out):
        self.queue = queue
        self.args = args
        self.token = token
        self.on_progress = on_progress
        self.refresh_review = refresh_review
        self.consolidate_duplicates = consolidate_duplicates
        self.results_out = results_out

    def run(self):
        self._start()
        for idx, item in enumerate(self.items, 1):
            self.idx, self.item = idx, item
            if self._run_album() is _STOP_QUEUE:
                break
        return self._finish()

    def _run_album(self):
        for step in (self._recover_finished_album, self._claim,
                     self._check_before_download, self._run_durable_lane,
                     self._download, self._import):
            verdict = step()
            if verdict is not None:
                return verdict
        return self._resolve()

    def _start(self):
        self.expected_generation = getattr(self.token, "credential_generation", "")
        if self.expected_generation:
            from qobuz_librarian.api.client import authorize_bound_download

            self.token = authorize_bound_download(self.token, prepare=False)

        self.authority = run_lock.current_lease()
        _require_executor_authority(self.authority)
        (
            self.origin,
            self.execution_mode,
            self.web_album_id,
            self.external_acknowledge_completion,
        ) = _durable_execution_origin()
        self.items = list(self.queue)
        self.n_items = len(self.items)
        self.interrupted = False
        self.cancelled = False
        self.disk_full = False
        self.io_error = False
        self.auth_lost_exc = None
        self.any_imported = False
        self._parked_post_dirs = []
        self.preflight_done = False
        self.durable_stopped = False
        self.recovery_persist_blocked = False
        self.results = [] if self.results_out is None else self.results_out
        self.never_started = []
        self.saved_state_changes = []
        self.recorded = len(self.results)

    def _record_finished(self):
        """Give each album that finished its History row and hook call when
        it finishes, so a stop later in the queue keeps them."""
        if not self.refresh_review:
            return
        finished = [
            result for result in self.results[self.recorded:]
            if result.get("album")
            and all(result is not skipped for skipped in self.never_started)
        ]
        self.recorded = len(self.results)
        _record_terminal_results(finished, self.args)

    def _persist(self):
        if self.on_progress is not None:
            # This is the durable commit gate for the caller's shrinking
            # queue.  Continuing after an I/O/CAS failure lets memory claim an
            # item is finished while the saved journal still says it is
            # pending, so stop before another album can mutate the library.
            #
            # Callers rewrite the journal as pending, which it refuses while it
            # holds blocked or retired work. Every mid-batch call is safe only
            # because a durable stop breaks the loop before the next one: an
            # album parked for attention stays blocked, and the batch-end call
            # below stands down for exactly that reason.
            self.on_progress()

    def _stop_for_full_disk(self, location):
        log.info(fmt(C.RED,
            f"\n    Out of disk space{location}. Stopping "
            "the queue; restoring backups and keeping the rest for a "
            "retry once space is freed."))
        self.disk_full = True

    def _ensure_preflight(self):
        if self.preflight_done:
            return
        _require_executor_authority(self.authority)
        staging_preflight(self.args)
        _require_executor_authority(self.authority)
        try:
            parked_imported, parked_dirs = _reimport_parked_albums()
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            parked_imported, parked_dirs = getattr(
                exc, "parked_results", (False, []))
            self._stop_for_full_disk(" during library import")
        _require_executor_authority(self.authority)
        self.any_imported = self.any_imported or parked_imported
        self._parked_post_dirs.extend(parked_dirs)
        if parked_imported:
            self.saved_state_changes.extend({
                "album": None,
                "dir": post_dir,
                "imported": True,
                "n_ok": 1,
            } for post_dir in (parked_dirs or [None]))
        self.preflight_done = True

    def _drop(self, item):
        """An item that's fully handled leaves the queue so a retry won't redo
        it. Persist immediately, so even a hard crash mid-flush can't resurrect
        already-imported albums."""
        try:
            self.queue.remove(item)
        except ValueError:
            pass
        self._persist()

    def _short_circuit(self, item):
        """Drain an item we can no longer process (cancel / interrupt / auth
        loss / disk full): mark it, run backup resolution so any upgrade backup
        gets restored, and keep it queued for a later retry. Skipping
        resolution would orphan the original tracks under their backup path."""
        # _build_queue_item seeds these keys with None, so setdefault() would
        # leave them None and _resolve_queue_item would label the stop
        # "nothing_landed". Assign over None, keeping a real result a prior
        # run's flush recorded.
        if item.get("snapshot_before") is None:
            item["snapshot_before"] = set()
        if not item.get("result"):
            item["result"] = ("cancelled" if self.cancelled
                              else "interrupted" if self.interrupted
                              else "disk_full" if self.disk_full
                              else "io_error" if self.io_error
                              else "auth_lost")
        result = _resolve_queue_item(
            item,
            self.args,
            False,
            authority=self.authority,
        )
        self.results.append(result)
        self.never_started.append(result)

    def _retain_failed_staging(self, item, *, label=None):
        """Capture a retention failure without skipping backup resolution."""
        try:
            if label is None:
                return retain_download_staging(item), None
            return retain_download_staging(item, label=label), None
        except BaseException as preserve_exc:
            log.info(fmt(
                C.RED,
                "    Could not preserve the failed staging run; resolving "
                "the album backup before returning the storage error.",
            ))
            return False, preserve_exc

    def _resolve_failed_item(self, item, retention_exc=None, *, cause=None):
        self.results.append(_resolve_queue_item(
            item, self.args, False, authority=self.authority))
        if retention_exc is not None:
            if cause is None:
                raise retention_exc
            raise retention_exc from cause

    def _cancel_item(self, item):
        item["result"] = "cancelled"
        self.cancelled = True
        _retained, retention_exc = self._retain_failed_staging(item)
        if retention_exc is None:
            log.info(fmt(C.YELLOW,
                "    Cancelled; retained this album's partial download "
                "for review."))
        self._resolve_failed_item(item, retention_exc)

    def _handle_download_exception(self, item, exc, *, phase=None):
        if isinstance(exc, KeyboardInterrupt):
            log.info(fmt(C.YELLOW,
                "\n    Interrupted. Stopping further downloads."))
            item["result"] = "interrupted"
            self.interrupted = True
        elif isinstance(exc, AuthLost):
            reason = (str(exc) if isinstance(exc, DownloadsRefused)
                      else "Qobuz rejected the token.")
            log.info(fmt(C.RED,
                f"\n    {reason} Stopping further downloads; "
                "will restore upgrade backups and exit."))
            item["result"] = "auth_lost"
            self.auth_lost_exc = exc
        elif isinstance(exc, OSError):
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                self._stop_for_full_disk(
                    f" during {phase}"
                    if phase
                    else f" at {cfg.STAGING_DIR}"
                )
                item["result"] = "disk_full"
            else:
                from qobuz_librarian.ui_cli.errors import oserr_hint
                # oserr_hint already ends the sentence for a permissions or
                # read-only mount; only an unclassified errno falls back to
                # the raw exception, which at least says something.
                hint = oserr_hint(exc)
                detail = hint if hint else f" ({exc})."
                log.info(fmt(C.RED,
                    f"\n    Storage error{detail} Stopping the queue; "
                    "restoring backups and keeping the rest for a retry."))
                item["result"] = "io_error"
                self.io_error = True
        else:
            raise exc
        _retained, retention_exc = self._retain_failed_staging(item)
        self._resolve_failed_item(item, retention_exc, cause=exc)

    def _record_durable_completion(self, item, post_dir, idx):
        self.any_imported = True
        result = {**_durable_completed_result(item, post_dir),
                  "album": item.get("album")}
        self.results.append(result)
        self.saved_state_changes.append({**result, "album": item.get("album")})
        log.info(fmt(*_durable_completion_line(
            result, item.get("elapsed", 0)
        )))
        verdict = result.get("quality_verdict") or {}
        if verdict.get("under"):
            log.info(fmt(C.YELLOW, f"    {quality_below_line(verdict)}"))
        self._after_album(item, post_dir, idx)

    def _after_album(self, item, post_dir, idx):
        """Recheck a finished album's track lengths, then pause before the
        next one. A Ctrl-C here stops the queue and keeps the album."""
        self._record_finished()
        if self.interrupted:
            return
        try:
            # A frame-boundary truncation decodes fine and only shows against
            # the real Qobuz length. Advisory: it never alters the album.
            if post_dir is not None and self.token and item.get("n_ok", 0) > 0:
                try:
                    warn_if_download_truncated(
                        post_dir, self.token, item["album"].get("title"),
                        item["missing"] if item.get("present") else None)
                except Exception as _e_rc:
                    vlog(f"post-download recheck raised: {_e_rc}")
            # Qobuz throttles sustained bulk queues. When the last rip showed
            # throttle signals, pause longer than the normal inter-album gap.
            if idx < self.n_items:
                cooldown = (
                    cfg.RATE_LIMIT_COOLDOWN if item.get("rate_limited") else 0
                )
                if cooldown:
                    log.info(fmt(
                        C.YELLOW,
                        f"    ⏳ Qobuz rate-limit detected; cooling down "
                        f"{int(cooldown)}s before the next album.",
                    ))
                    if _sleep_unless_cancelled(cooldown, is_cancel_requested):
                        self.cancelled = True
                else:
                    _sleep_unless_cancelled(
                        cfg.DELAY_BETWEEN,
                        is_cancel_requested,
                    )
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW,
                "\n    Interrupted. Stopping further downloads."))
            self.interrupted = True

    def _recover_finished_album(self):
        self._record_finished()
        _require_executor_authority(self.authority)
        self.requires_library_backup = queue_item_may_create_library_backup(self.item)
        self.plan = plan_durable_new_album(self.item, self.args, check_beets=False)
        self.recoverable = _durable_plan_allowed(
            self.items,
            self.item,
            execution_mode=self.execution_mode,
            web_album_id=self.web_album_id,
        )
        if not self.recoverable:
            self.plan = None
        # An album that finished before a restart is acknowledged even if a
        # beets path template changed since; it is already filed.
        if self.plan is not None and not durable_import_possible(self.item.get("album_dir")):
            self.plan = None

        recovered_completion = {}

        def _acknowledge_recovered_completion(
            recovered_origin,
            recovered_owner,
            *,
            album_id,
            completion_hash,
            planned,
            post_dir,
        ):
            if not self.recoverable:
                return False
            details = _recovered_completion_details(
                self.item,
                expected_origin=self.origin,
                execution_mode=self.execution_mode,
                origin=recovered_origin,
                owner=recovered_owner,
                album_id=album_id,
                planned=planned,
                post_dir=post_dir,
            )
            if details is None:
                return False
            if self.execution_mode.startswith("web-job:") and (
                len(self.queue) != 1
                or self.queue[0] is not self.item
                or not callable(self.external_acknowledge_completion)
            ):
                return False
            if recovered_origin.kind is CompletionOriginKind.WEB_JOB:
                if self.external_acknowledge_completion(
                    recovered_origin,
                    recovered_owner,
                    album_id=album_id,
                    completion_hash=completion_hash,
                    planned=planned,
                    post_dir=post_dir,
                ) is not True:
                    return False
            _require_executor_authority(self.authority)
            caller_index = next(
                (
                    index
                    for index, queued in enumerate(self.queue)
                    if queued is self.item
                ),
                None,
            )
            if caller_index is None:
                return False
            del self.queue[caller_index]
            recovered_completion.update(
                post_dir=details[0],
                n_ok=details[1],
                n_fail=details[2],
                n_lossy=details[3],
                owner=recovered_owner,
            )
            return True

        self.startup = recover_startup_state(
            authority=self.authority,
            acknowledge_completion=_acknowledge_recovered_completion,
        )
        _require_executor_authority(self.authority)
        if recovered_completion:
            if (
                self.startup.status is StartupRecoveryStatus.ATTENTION_REQUIRED
                or not _recovered_owner_settled(
                    recovered_completion["owner"]
                )
            ):
                self.recovery_persist_blocked = True
                self.durable_stopped = True
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Saved completion recovery remains unsettled; "
                    "stopping this batch.",
                ))
                return _STOP_QUEUE
            self.item.update(
                n_ok=recovered_completion["n_ok"],
                n_fail=recovered_completion["n_fail"],
                n_lossy=recovered_completion["n_lossy"],
                elapsed=0.0,
            )
            self._persist()
            self._record_durable_completion(
                self.item,
                recovered_completion["post_dir"],
                self.idx,
            )
            return _NEXT_ALBUM

    def _claim(self):
        try:
            self.resume_owner, self.durable_mode, self.resume_action = _exact_resume_owner(
                self.startup,
                self.item,
                execution_mode=self.execution_mode,
            )
        except DurableAlbumUnavailable:
            if self.any_imported:
                self.durable_stopped = True
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Remaining saved queue work needs exact recovery; "
                    "stopping this batch.",
                ))
                return _STOP_QUEUE
            raise
        if self.resume_owner is not None and self.plan is None:
            # Every walk saves its queue before flushing it, so during an
            # ordinary flush each item resolves an owner for its own pending
            # entry. A pending entry is a persisted decision, not recovery
            # state, so give it back and let the legacy lane run the album.
            if _release_unplannable_claim(
                self.resume_owner,
                self.resume_action,
                authority=self.authority,
            ):
                self.resume_owner = None
            elif self.any_imported:
                self.durable_stopped = True
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Remaining saved queue work is not yet supported "
                    "by safe recovery; stopping this batch.",
                ))
                return _STOP_QUEUE
            else:
                raise DurableAlbumUnavailable(
                    "the saved queue item is not yet supported by safe recovery"
                )
        if (
            self.resume_owner is None
            or self.resume_action is StartupRecoveryAction.PENDING
        ):
            try:
                self.token = _admit_new_queue_items([self.item], self.token)
            except CandidateStale as exc:
                log.info(fmt(C.YELLOW,
                    f"    ⚠  {truncate(self.item['album'].get('title') or '?', 55)}: {exc}"))
                self.item["result"] = "stale_candidate"
                self._drop(self.item)
                self.results.append(_resolve_queue_item(
                    self.item,
                    self.args,
                    False,
                    authority=self.authority,
                ))
                return _NEXT_ALBUM
        else:
            _bind_resumed_queue_item_premise(self.item)
        if (
            self.expected_generation
            and self.resume_owner is not None
            and self.resume_action is not StartupRecoveryAction.PENDING
        ):
            from qobuz_librarian.api.client import authorize_bound_download

            self.token = authorize_bound_download(self.token)
        if self.resume_owner is None:
            self._ensure_preflight()

    def _check_before_download(self):
        # A cancel raised while the PREVIOUS album was importing (i.e. after its
        # own post-download checkpoint at the bottom of the loop) hasn't set
        # `cancelled` yet. Re-check here so the next album short-circuits at the
        # boundary instead of downloading in full before the stale check lands.
        # set the flag first so _short_circuit marks the item 'cancelled', not
        # the auth_lost fallback.
        if not self.cancelled and is_cancel_requested():
            self.cancelled = True
        if self.resume_owner is not None and self.cancelled:
            raise KeyboardInterrupt
        if (self.interrupted or self.cancelled or self.disk_full or self.io_error
                or self.auth_lost_exc is not None):
            self._short_circuit(self.item)
            return _NEXT_ALBUM
        # A retried item may still carry a stop marker ('disk_full',
        # 'interrupted', ...) from an earlier flush; clear it so a
        # now-successful re-download isn't read as still-stopped by
        # _queue_item_needs_retry and left in the queue forever.
        self.item["result"] = None
        self.item["folder_denied"] = None
        self.item["quality_verdict"] = None
        self.item["upgrade_unverified"] = False
        self.item["catalogue_unverified"] = False
        self.item["recovery_unverified"] = False
        self.item["resampled_n"] = 0
        self.item.update(
            downsample_errors=0,
            downsample_saved_bytes=0,
            downsample_flush_warnings=0,
            downsample_cancelled=False,
        )
        self.item["post_import_signatures"] = []
        self.retention_exc = None

        self.album = self.item["album"]
        self.album_dir = self.item["album_dir"]
        self.title = self.album.get("title") or "?"

        print()
        log.info(fmt(C.BOLD + C.WHITE,
            f"  [Q {self.idx}/{self.n_items}] {truncate(self.title, 55)}"))
        if self.requires_library_backup and self.plan is None:
            # The exact-recovery lane only covers a handful of shapes (no
            # sibling replacement, no downsampling, whole-album requests). The
            # rest still run, on the path that backs up before it replaces.
            log.info(fmt(C.GRAY,
                "    Crash-safe recovery doesn't cover this album; using the "
                "standard path, which backs up anything it replaces."))

        if self.plan is not None and self.resume_owner is None and (
                refusal := disk_support.refusal("whole_albums")):
            log.info(fmt(C.YELLOW, f"    ⚠  {refusal} Nothing was downloaded."))
            self.item["result"] = "disk_refused"
            self._drop(self.item)
            self.results.append(_resolve_queue_item(
                self.item, self.args, False, authority=self.authority))
            return _NEXT_ALBUM

        if self.resume_owner is None and self.album_dir and self.album_dir.is_dir() and (
                folder := unwritable_folder(
                    self.album_dir, parent=bool(self.item["auto_upgrade"]))):
            log.info(fmt(C.RED,
                f"    ✗  The app can't write to {folder}. Nothing was "
                "downloaded."))
            self.item["result"] = "folder_read_only"
            self.item["folder_denied"] = str(folder)
            self.results.append(_resolve_queue_item(
                self.item, self.args, False, authority=self.authority))
            return _NEXT_ALBUM

    def _run_durable_lane(self):
        if self.plan is not None:
            def _prepare_staged(album_dirs, checkpoint_sources):
                title = edition_album_title(self.album) if self.item.get("exact_edition") else None
                if title:
                    prepare_managed_staging_tags(
                        album_dirs, staged_track_bindings(self.item), album_title=title,
                        authority_check=lambda: _require_executor_authority(self.authority))
                    checkpoint_sources(SourceTransitionKind.BEETS_TAG_CLEAN)
                prepared = _run_pre_import_hooks_for_dirs(
                    album_dirs,
                    self.args,
                    on_sources_changed=checkpoint_sources,
                )
                note_superseded_attempts(self.item)
                return prepared

            try:
                outcome = execute_durable_new_album(
                    self.queue,
                    self.item,
                    self.args,
                    plan=self.plan,
                    origin=self.origin,
                    mode=self.durable_mode,
                    authority=self.authority,
                    resume_owner=self.resume_owner,
                    prepare_staged=_prepare_staged,
                    acknowledge_completion=self.external_acknowledge_completion,
                )
            except (KeyboardInterrupt, AuthLost) as exc:
                # The album is back in the queue or parked for recovery as it
                # stands; the albums that finished before it are still
                # wrapped up below.
                if isinstance(exc, KeyboardInterrupt):
                    log.info(fmt(C.YELLOW,
                        "\n    Interrupted. Stopping further downloads."))
                    self.interrupted = True
                    self.item["result"] = "interrupted"
                else:
                    reason = (str(exc) if isinstance(exc, DownloadsRefused)
                              else "Qobuz rejected the token.")
                    log.info(fmt(C.RED,
                        f"\n    {reason} Stopping further downloads."))
                    self.auth_lost_exc = exc
                    self.item["result"] = "auth_lost"
                self.results.append({
                    "album": self.item["album"],
                    "dir": self.item.get("album_dir"),
                    "result": self.item["result"],
                    "n_ok": 0,
                    "n_fail": 0,
                    "imported": False,
                    "auto_upgrade": bool(self.item.get("auto_upgrade")),
                })
                self.durable_stopped = True
                return _STOP_QUEUE
            return self._settle_durable_outcome(outcome)

    def _settle_durable_outcome(self, outcome):
        removed = all(queued is not self.item for queued in self.queue)
        if removed:
            self._persist()
        if outcome.status is DurableAlbumStatus.CANCELLED:
            self.item["result"] = "cancelled"
            self.cancelled = True
            log.info(fmt(C.YELLOW,
                "    Cancelled; discarded this album's partial "
                "download."))
            self.results.append(_resolve_queue_item(
                self.item, self.args, False, authority=self.authority))
            return _NEXT_ALBUM
        # A new album has nothing in the library to put back, so when
        # some of its tracks arrived it is fetched again on the standard
        # path, which imports the tracks it gets like any partial album.
        refetch = (
            outcome.status is DurableAlbumStatus.INCOMPLETE
            and self.plan.library_backup_kind is None
            and self.item.get("n_ok", 0) > 0
            and not self.item.get("rate_limited")
        )
        if outcome.status is DurableAlbumStatus.INCOMPLETE and not refetch:
            # Qobuz could not serve the whole album, or the download did
            # not hold up against the tracks it would replace. The
            # download is gone and nothing is left waiting on recovery, so
            # the rest of the queue carries on.
            self.item["result"] = "incomplete"
            if outcome.reason == "upgrade-unverified":
                # The original stays, and another try would fetch the
                # same copy, so nothing is left to retry.
                self.item["upgrade_put_back"] = True
                verdict = self.item.get("quality_verdict") or {}
                if verdict.get("under"):
                    mark_album_capped(self.item["album"].get("id"), self.item["album"], {
                        "n_below": verdict["n_below"], "n_at": 0, "n_above": 0})
                self._drop(self.item)
                log.info(fmt(C.YELLOW,
                    "    ⚠  The new copy couldn't be verified against "
                    "yours, so your original was kept and the download "
                    "was discarded."))
            else:
                log.info(fmt(C.YELLOW,
                    "    ⚠  Qobuz didn't deliver every track; nothing was "
                    "imported and the partial download was discarded."))
            self.results.append(_resolve_queue_item(
                self.item, self.args, False, authority=self.authority))
            return _NEXT_ALBUM
        if outcome.status is DurableAlbumStatus.RETRY:
            # Nothing was imported and nothing waits on recovery, so the
            # album stays queued for another try and the rest carry on.
            log.info(fmt(C.YELLOW, wrap(
                "Qobuz couldn't deliver the whole album. Nothing was "
                "imported; it stays queued for another try.",
                indent="    ⚠  ", hanging="       ")))
            self.results.append(_resolve_queue_item(
                self.item, self.args, False, authority=self.authority))
            return _NEXT_ALBUM
        if outcome.status not in (
            DurableAlbumStatus.COMPLETE, DurableAlbumStatus.INCOMPLETE,
        ):
            self.results.append({
                "album": self.item["album"],
                "dir": outcome.post_dir,
                "result": outcome.status.value,
                "n_ok": self.item.get("n_ok", 0),
                "n_fail": self.item.get("n_fail", 0),
                "n_lossy": self.item.get("n_lossy", 0),
                "siblings_preserved": [],
                "imported": False,
                "auto_upgrade": bool(self.item.get("auto_upgrade")),
                "companions_unsettled": outcome.reason
                    == "library-backup-companions-unsettled",
            })
            self.durable_stopped = True
            if outcome.reason == "managed-import-disk-full":
                self.results[-1]["disk_full"] = True
                self._stop_for_full_disk(" during library import")
            elif self.results[-1]["companions_unsettled"]:
                # Kept as it is, as the next start would keep it, so
                # nothing of it is left to resume.
                self.queue.remove(self.item)
                kept = None
                web_job = self.execution_mode.startswith("web-job:")
                if not web_job:
                    try:
                        kept = settle_blocked_item(
                            authority=self.authority,
                            operation_id=outcome.operation_id,
                            item_id=outcome.item_id,
                            action=BlockedItemSettlementAction.DISCARD,
                        )
                    except (OSError, sqlite3.Error,
                            queue_state.QueueJournalError) as exc:
                        vlog(f"keeping the uncarried upgrade failed: {exc}")
                if (kept is not None and kept.status
                        is BlockedItemSettlementStatus.DISCARDED):
                    self.durable_stopped = False
                    self.results[-1]["companions_settled"] = True
                    self._persist()
                    log.info(fmt(C.YELLOW, wrap(
                        kept.reason, indent="    ⚠  ", hanging="       ")))
                    return _NEXT_ALBUM
                log.info(fmt(
                    C.YELLOW,
                    "    ⚠  The new copy is in your library, but files "
                    "from the album it replaced could not be carried "
                    "into it. "
                    + ("When this download ends, the app keeps that "
                       "album as a backup." if web_job else
                       "Stopping the queue; the next time the app "
                       "starts, it keeps that album as a backup."),
                ))
            else:
                log.info(fmt(
                    C.YELLOW,
                    "    ⚠  This album needs a safe retry or recovery; "
                    "stopping the queue.",
                ))
            return _STOP_QUEUE
        if outcome.status is DurableAlbumStatus.COMPLETE:
            if not removed or outcome.post_dir is None:
                raise DurableAlbumUnavailable(
                    "the completed durable album was not finalised exactly"
                )
            self._record_durable_completion(self.item, outcome.post_dir, self.idx)
            return _NEXT_ALBUM
        log.info(fmt(C.YELLOW,
            "    ⚠  Qobuz didn't deliver every track; downloading the "
            "album again to keep the tracks that arrive."))

    def _download(self):
        _seal_queue_item_siblings(self.item)
        if self.item["auto_upgrade"] and self.album_dir and self.album_dir.exists():
            # The album's beets entry takes in the replacement once it checks
            # out, so the album keeps its id and fields.
            self.item["_replaced_beets_entries"] = capture_beets_album_entries(self.album_dir)
            bp = backup_album_dir(
                self.album_dir,
                expected_receipt=self.item.get("_validated_source_receipt"),
            )
            if bp is not None and not bp.complete:
                from qobuz_librarian.modes.process import (
                    _recover_incomplete_upgrade_backup,
                )

                _recover_incomplete_upgrade_backup(
                    bp, self.album_dir, operation="queued upgrade backup")
                log.info(fmt(C.RED,
                    "    ✗  The backup was interrupted; skipping this "
                    "album."))
                self.item["result"] = "upgrade_aborted_backup_failed"
                self.results.append(_resolve_queue_item(
                    self.item, self.args, False, authority=self.authority))
                return _NEXT_ALBUM
            if bp is None:
                log.info(fmt(C.RED,
                    "    ✗  Could not back up; skipping this album."))
                self.item["result"] = "upgrade_aborted_backup_failed"
                self.results.append(_resolve_queue_item(
                    self.item, self.args, False, authority=self.authority))
                return _NEXT_ALBUM   # Nothing downloaded, so it stays queued for retry.
            self.item["backup_path"] = bp
        # Snapshot staging AFTER the backup: a custom config can point
        # UPGRADE_BACKUP_DIR inside STAGING_DIR, and a backup copied in BEFORE
        # the snapshot is already present at snapshot time, so it's correctly
        # excluded from the freshly-downloaded diff (not re-imported/downsampled).
        try:
            self.item["snapshot_before"] = snapshot_staging()
            _download_for_queue_item(self.item)
        except (KeyboardInterrupt, AuthLost, OSError) as exc:
            self._handle_download_exception(self.item, exc)
            return _NEXT_ALBUM

        if is_cancel_requested():
            self._cancel_item(self.item)
            return _NEXT_ALBUM

        self.summary_n_ok = self.item.get("n_ok", 0)
        self.summary_n_missing = sum(incomplete_track_counts(self.item))
        summary_elapsed = int(self.item.get("elapsed", 0))
        if self.summary_n_ok and not self.summary_n_missing:
            log.info(fmt(C.GREEN, f"    ✓ {plural(self.summary_n_ok, 'track')} · {summary_elapsed}s"))
        elif self.summary_n_ok:
            log.info(fmt(C.YELLOW,
                f"    ⚠ {self.summary_n_ok} ok · {self.summary_n_missing} missing · {summary_elapsed}s"))
        else:
            log.info(fmt(C.RED, f"    ✗ download failed · {summary_elapsed}s"))

    def _import(self):
        self.item_imported = False
        if self.summary_n_ok > 0 and not self.args.no_import:
            try:
                self.album_dirs = _staged_album_dirs(self.item)
            except OSError as exc:
                self._handle_download_exception(self.item, exc)
                return _NEXT_ALBUM
            if not self.album_dirs:
                log.info(fmt(C.YELLOW,
                    "    ⚠  No staged audio dir found for this album; skipping beets."))
            else:
                return self._import_staged()
        elif self.args.no_import:
            # Without the pre-import hooks a multi-disc cover would stay in
            # the album root, where the manual import leaves it behind.
            try:
                for staged in _staged_album_dirs(self.item):
                    relocate_disc_album_artwork(staged)
            except OSError as exc:
                vlog(f"artwork relocation raised: {exc}")
            if self.summary_n_ok and not self.summary_n_missing:
                keep_unimported_download(self.item)
                kept_in = cfg.STAGING_DIR
            elif self.retention_exc is None:
                _retained, self.retention_exc = self._retain_failed_staging(
                    self.item, label="incomplete")
                kept_in = cfg.STAGING_DIR / cfg.BEETS_RETRY_DIR
            log.info(fmt(C.YELLOW,
                f"  --no-import: skipping beets. Files in {kept_in}/"))
        elif self.summary_n_ok == 0:
            log.info(fmt(C.GRAY, "  Skipping beets: nothing landed."))
            retire_failed_download_staging(self.item)

    def _import_staged(self):
        for step in (self._check_quality, self._prepare_staged_album,
                     self._import_with_beets):
            verdict = step()
            if verdict is not None:
                return verdict
        self._accept_import()

    def _redownload_at_max(self):
        saved_q = self.item.get("quality")
        retry_item = dict(self.item)
        retry_item["quality"] = 4
        retry_item.pop("_staging_run", None)
        retry_item.pop("_staging_run_retained", None)

        def _run_retry():
            _download_for_queue_item(retry_item)

        try:
            fresh_dirs, retry_kept = redownload_with_staged_fallback(
                self.album_dirs,
                run_retry=_run_retry,
                collect_staged_dirs=lambda: (
                    _staged_album_dirs(retry_item)
                    if retry_item.get("n_ok", 0) > 0 else []),
                discard_retry=lambda: discard_retry_staging(
                    retry_item),
                retry_preserves_original=lambda: (
                    retry_preserves_track_coverage(
                        self.item, retry_item)))
            # Kept or not, a throttled re-download slows the next album.
            if retry_item.get("rate_limited"):
                self.item["rate_limited"] = True
            if retry_kept:
                retire_empty_download_staging(self.item)
                for key in ("n_ok", "n_fail", "n_lossy", "n_broken",
                            "n_unavailable", "failed_tracks",
                            "lossy_tracks", "broken_tracks",
                            "unavailable_tracks", "elapsed",
                            "cd_quality_tracks",
                            "_cd_quality_paths",
                            "gap_fill_backup_path", "_staging_run",
                            "_staging_run_retained",
                            "_expected_track_keys",
                            "_clean_track_keys",
                            "_exact_track_coverage",
                            "_unmatched_audio",
                            "_clean_staged_files",
                            "_all_staged_audio",
                            "_staged_track_bindings"):
                    if key in retry_item:
                        self.item[key] = retry_item[key]
                    else:
                        self.item.pop(key, None)
            else:
                retire_empty_download_staging(retry_item)
        finally:
            self.item["quality"] = saved_q
        return fresh_dirs, self.item.get("_cd_quality_paths", ())

    def _check_quality(self):
        effective_tier = self.item.get("quality") or cfg.STREAMRIP_QUALITY
        try:
            self.item["quality_verdict"] = verify_and_recover(
                self.album, self.album_dirs,
                redownload_at_max=self._redownload_at_max,
                effective_tier=effective_tier,
                allow_retry=self.item.get("gap_fill_backup_path") is None,
                exempt=self.item.get("_cd_quality_paths", ()))
        except (KeyboardInterrupt, AuthLost, OSError) as exc:
            self._handle_download_exception(self.item, exc)
            return _NEXT_ALBUM
        if is_cancel_requested():
            self._cancel_item(self.item)
            return _NEXT_ALBUM
        if self.item["quality_verdict"]["retried"]:
            self.album_dirs = self.item["quality_verdict"]["staged_dirs"]
        if self.item["quality_verdict"]["recovered"]:
            log.info(fmt(C.CYAN,
                "    ↻ recovered: re-fetched at the highest source."))
        elif not self.item["quality_verdict"]["under"]:
            met = quality_met_line(
                self.item.get("n_ok", 0), self.item.get("cd_quality_tracks"))
            if met:
                log.info(fmt(C.GRAY, f"    {met}"))
        else:
            log.info(fmt(C.YELLOW,
                f"    {quality_below_line(self.item['quality_verdict'])}"))
            # Still under target: the import proceeds, but the owning
            # job must not look like a clean finish.
            if on_quality_shortfall is not None:
                on_quality_shortfall(self.item["quality_verdict"])

    def _prepare_staged_album(self):
        # Repair carries a callback that re-tags the staged refills
        # with the originals' own metadata before beets files them, so
        # a recording that also lives on a compilation comes back tagged
        # for the album the user owns. No-op for every other queue item.
        retag = self.item.get("pre_import_retag")
        if callable(retag):
            try:
                retag(self.album_dirs)
            except Exception as _e_rt:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  repair retag step failed: {_e_rt}"))
        try:
            if self.item.get("backup_path") is not None:
                fill_tags = fill_album_tags(self.item["backup_path"])
            else:
                fill_tags = (
                    fill_album_tags(self.album_dir)
                    if self.album_dir is not None
                    and not self.item.get("download_full_album") else None)
            if fill_tags:
                _prepare_staging_tags(self.album_dirs, album_tags=fill_tags)
            elif self.item.get("exact_edition") or (
                    self.item.get("backup_path") is not None
                    and edition_album_title(self.album)):
                _prepare_staging_tags(self.album_dirs, album_title=edition_album_title(self.album))
            prepared = _run_pre_import_hooks_for_dirs(self.album_dirs, self.args)
            if self.item.get("backup_path") is not None:
                from qobuz_librarian.modes.process import (
                    _keep_own_cover,
                )

                _keep_own_cover(self.item["backup_path"], self.album_dirs)
            note_superseded_attempts(self.item)
            self.item["resampled_n"] = prepared.resampled
            self.item.update(_pre_import_outcome_fields(prepared))
            self.item["post_import_signatures"] = (
                track_signatures_for_album_dirs(self.album_dirs))
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW,
                "  ⚠  Pre-import hook interrupted; skipping beets "
                "for this album; stopping the queue."))
            self.interrupted = True
            # Mark interrupted so _resolve_queue_item labels it as such
            # instead of falling through to "downloaded" (the album did
            # not import).
            self.item["result"] = "interrupted"
            self.results.append(_resolve_queue_item(
                self.item, self.args, False, authority=self.authority))
            return _NEXT_ALBUM
        except OSError as exc:
            self._handle_download_exception(self.item, exc, phase="staged preparation")
            return _NEXT_ALBUM

    def _import_with_beets(self):
        self.parking_receipts = tuple(
            capture_tree(directory) for directory in self.album_dirs)
        if any(receipt is None for receipt in self.parking_receipts):
            self.parking_receipts = ()
        try:
            self.ownership_capture = (
                {} if self.item.get("_capture_import_ownership") is True
                else None
            )
            pin = (
                {} if self.item.get("album_dir") is None
                else {"album_dir": self.item["album_dir"],
                      "places": self.item.get("refill_places")}
            )
            if self.item.get("backup_path") is not None:
                pin["folder"] = self.item["album_dir"]
                if self.item.get("_replaced_beets_entries") is not None:
                    del pin["album_dir"]
                    from qobuz_librarian.modes.process import mark_pending_upgrade
                    if not mark_pending_upgrade(
                            self.item["backup_path"],
                            self.item["_replaced_beets_entries"], self.album_dirs):
                        raise OSError(
                            "couldn't record the upgrade in its backup")
            failure = {}
            if self.ownership_capture is None:
                self.item_imported = _import_album_with_retry(
                    self.album_dirs, failure_out=failure, **pin)
            else:
                self.item_imported = _import_album_with_retry(
                    self.album_dirs,
                    ownership_out=self.ownership_capture,
                    failure_out=failure,
                    **pin,
                )
            if not self.item_imported and failure.get("permission_denied"):
                self.item["import_denied"] = failure["permission_denied"]
        except ImportedThenInterrupted:
            log.info(fmt(C.YELLOW,
                "\n    Interrupted. Stopping further downloads."))
            self.interrupted = True
            self.item_imported = True
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW,
                "\n  beets interrupted. Resolving backups based on disk."))
            self.interrupted = True
            self.item_imported = False
            # Label as interrupted (not "downloaded"). Beets did not
            # finish importing this album.
            self.item["result"] = "interrupted"
            self.results.append(_resolve_queue_item(
                self.item, self.args, False, authority=self.authority))
            return _NEXT_ALBUM
        except OSError as exc:
            # The original may already be in the upgrade backup.
            # Resolve it before stopping the queue.
            self._handle_download_exception(
                self.item, exc, phase="library import"
            )
            return _NEXT_ALBUM

    def _accept_import(self):
        if self.item_imported and self.ownership_capture is not None:
            ownership_result = self.ownership_capture.get("result")
            if isinstance(ownership_result, dict):
                self.item["_import_ownership"] = ownership_result

        retired_import_staging = True
        if self.item_imported and "_staging_run" in self.item:
            try:
                retired_import_staging = (
                    retire_download_staging_after_import(self.item)
                )
            except BaseException as cleanup_exc:
                retired_import_staging = False
                self.retention_exc = cleanup_exc
                log.info(fmt(
                    C.RED,
                    "  ✗  Could not retire the exact imported staging "
                    "run; resolving the album backup before returning "
                    "the storage error.",
                ))
        if self.item_imported and retired_import_staging:
            self.any_imported = True
            clear_attempts_after_import(self.item)
        else:
            if self.item_imported:
                log.info(fmt(
                    C.RED,
                    "  ✗  The exact download run was not empty "
                    "after beets; the import was not accepted.",
                ))
            self.item_imported = False
            self.item["result"] = "import_failed"
            if self.retention_exc is None:
                retained, self.retention_exc = self._retain_failed_staging(
                    self.item, label="beets-incomplete")
                if retained:
                    log.info(fmt(
                        C.YELLOW,
                        "  ⚠  Kept the exact residual download run in "
                        "private staging recovery; the queue item "
                        "remains pending.",
                    ))
                elif self.retention_exc is None:
                    try:
                        _move_to_beets_retry(
                            self.album_dirs,
                            self.title,
                            expected=self.parking_receipts,
                        )
                    except BaseException as parking_exc:
                        self.retention_exc = parking_exc
                        log.info(fmt(
                            C.RED,
                            "  ✗  Could not park the failed beets "
                            "import; resolving the album backup before "
                            "returning the storage error.",
                        ))

    def _resolve(self):
        # Drop a finished item from the persisted queue BEFORE resolving its
        # backup. If the process dies between the two, the worst case is an
        # orphaned backup the retention sweep clears, not a completed album
        # left queued and needlessly re-downloaded on the next resume.
        if not _queue_item_needs_retry(self.item):
            self._drop(self.item)
        result = _resolve_queue_item(
            self.item, self.args, self.item_imported, authority=self.authority)
        self.results.append(result)
        if result.get("imported") and result.get("n_ok", 0) > 0:
            change = {**result, "album": self.item.get("album")}
            if self.item.get("_resolved_post_dir") is not None:
                change["_resolved_post_dir"] = self.item["_resolved_post_dir"]
            self.saved_state_changes.append(change)
        if self.retention_exc is not None:
            raise self.retention_exc

        self._after_album(
            self.item,
            self.item.get("_resolved_post_dir")
            if self.item_imported and not self.args.no_import else None,
            self.idx,
        )

    def _preflight_if_skipped(self):
        self._record_finished()
        if not self.preflight_done and not self.durable_stopped:
            _require_executor_authority(self.authority)
            final_startup = recover_startup_state(
                authority=self.authority,
                acknowledge_completion=None,
            )
            _require_executor_authority(self.authority)
            # Albums of this queue still waiting for another try are no recovery.
            if final_startup.status is StartupRecoveryStatus.CLEAR or (
                final_startup.status is StartupRecoveryStatus.RESUME_REQUIRED
                and all(entry.phase is queue_state.QueuePhase.PENDING
                        for entry in final_startup.items)
            ):
                self._ensure_preflight()
            else:
                self.durable_stopped = True
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Saved queue recovery remains unfinished; "
                    "skipping unrelated staging work.",
                ))

    def _consolidate(self):
        # Consolidate duplicate albums once if anything landed. The fold is a
        # library-wide pass, so running it per-album would waste work.
        ownership_scope_resolved = any(
            item.get("_resolved_post_dir_from_import_ownership") is True
            for item in self.items
        )
        if (self.consolidate_duplicates
                and self.any_imported
                and not any(item.get("exact_edition") for item in self.items)
                and not ownership_scope_resolved):
            try:
                protected_ownership_dirs = [
                    item.get("_resolved_post_dir")
                    for item in self.items
                    if isinstance(item.get("_import_ownership"), dict)
                    and item.get("_resolved_post_dir") is not None
                ]
                if protected_ownership_dirs:
                    _consolidate_duplicate_albums(
                        protected_paths=protected_ownership_dirs)
                else:
                    _consolidate_duplicate_albums()
            except KeyboardInterrupt:
                self.interrupted = True
                log.info(fmt(C.YELLOW, _FINISHING_UP))
            except Exception as _e_c:
                vlog(f"consolidate duplicate albums raised: {_e_c}")
        elif self.consolidate_duplicates and ownership_scope_resolved:
            vlog(
                "skipping duplicate consolidation for descriptor-verified "
                "one-track ownership scope"
            )

    def _write_sidecars(self):
        print()
        _post_dirs = [it.get("_resolved_post_dir") for it in self.items
                      if it.get("_resolved_post_dir") is not None]
        # Materialise .lrc sidecars (and strip the embedded tag in sidecar mode) for
        # every imported album: the lyric hook always embeds, and this pass emits
        # the sidecars. No-op when LYRICS_FORMAT is embed.
        ownership_tracks = [
            it["_import_ownership"]
            for it in self.items
            if isinstance(it.get("_import_ownership"), dict)
        ]
        if self.any_imported and (
                _post_dirs or self._parked_post_dirs or ownership_tracks):
            capture_ownership = bool(ownership_tracks)
            ownership_changes = [] if capture_ownership else None
            ownership_created_files = [] if capture_ownership else None
            ownership_directory_changes = [] if capture_ownership else None
            try:
                if ownership_changes is None:
                    write_post_import_sidecars(_post_dirs + self._parked_post_dirs)
                else:
                    write_post_import_sidecars(
                        _post_dirs + self._parked_post_dirs,
                        identity_changes_out=ownership_changes,
                        created_files_out=ownership_created_files,
                        directory_changes_out=ownership_directory_changes,
                        owned_tracks=ownership_tracks,
                    )
            except KeyboardInterrupt:
                self.interrupted = True
                log.info(fmt(C.YELLOW, _FINISHING_UP))
            except Exception as _e_sc:
                vlog(f"post-import sidecar write raised: {_e_sc}")
            finally:
                if capture_ownership:
                    _advance_import_ownership_identities(
                        self.items,
                        ownership_changes,
                        ownership_created_files,
                        ownership_directory_changes,
                    )

        # After the sidecar pass, because writing a sidecar can rewrite the audio
        # file (embedded lyrics are stripped in sidecar mode) and that would leave
        # every row just written already stale.
        if self.any_imported:
            cache_album_tags(_post_dirs + self._parked_post_dirs)

    def _finish(self):
        self._preflight_if_skipped()
        self._consolidate()
        self._write_sidecars()
        log.info(fmt(*_queue_done_line(
            self.results, self.n_items,
            stopped=(self.interrupted or self.disk_full or self.io_error or self.durable_stopped
                     or self.auth_lost_exc is not None))))

        # A durable stop leaves the journal holding blocked or retired work on
        # purpose, and rewriting it as pending is what the journal refuses. Every
        # album that finished already persisted through _drop, so the saved state is
        # current without this.
        if not self.recovery_persist_blocked and not self.durable_stopped:
            self._persist()
        # Only the CLI batch callers (walk / artist / album / queue resume) refresh
        # saved state here. The web single-track and CLI Repair callers refresh
        # after their own higher-level operation has verified the file change.
        if self.refresh_review:
            _refresh_review_state_after_downloads(
                self.saved_state_changes,
                self.token,
                self.args,
                allow_upgrade_refresh=(self.auth_lost_exc is None and not self.interrupted),
                record=False,
            )
        if self.auth_lost_exc is not None:
            raise self.auth_lost_exc
        # Re-raising KeyboardInterrupt is what stops the caller's _flush_queue
        # from reaching its clear-on-drain branch and discarding the items that
        # were short-circuited (and so still need a retry).
        if self.interrupted:
            raise KeyboardInterrupt
        return self.results, not self.queue and not self.durable_stopped


def _execute_download_queue(queue, args, token, *, on_progress=None,
                            refresh_review=False,
                            consolidate_duplicates=True,
                            results_out=None):
    """Flush a batch of pre-confirmed download decisions, one album at a time.

    Each item runs through its own pipeline: download, downsample, lyrics,
    beets import (with retry on idle timeout), and backup resolution. A
    single broken or hung album in a large batch loses only itself instead of
    the whole import. Albums that exhaust ``BEETS_MAX_ATTEMPTS`` are parked
    under ``STAGING_DIR/<BEETS_RETRY_DIR>/`` and re-imported (no re-download)
    at the start of the next flush.

    Items are dropped from ``queue`` in place the moment they're done, so what
    remains is exactly the unfinished work: a resume re-downloads only what
    never landed and never what already imported. ``on_progress`` (when given)
    fires after each change so the caller can re-persist the shrinking queue.

    Returns ``(results, drained)``. A durable safety stop may return before the
    rest of the batch has results; ``drained`` is True only when nothing is
    left to retry or recover. ``results_out`` collects the same results as
    they come, for a caller that a Ctrl-C leaves without a return value.
    """
    if not queue:
        return [], True

    section(f"Download queue: {plural(len(queue), 'album')}")

    if args.dry_run:
        log.info(fmt(C.YELLOW, "\n  --dry-run: would queue:"))
        dry_run_results = []
        for item in queue:
            tag = "↑ upgrade" if item["auto_upgrade"] else "fill"
            # Brand-new albums (missing-album / album-mode queue) have album_dir=None
            _dry_label = (item["album_dir"].name if item["album_dir"]
                          else (item["album"].get("title") or "?"))
            log.info(fmt(C.GRAY,
                f"    [{item['label']}] {tag}: {_dry_label}"))
            dry_run_results.append({"dir": item["album_dir"], "result": "dry_run",
                                    "n_missing": len(item["missing"])})
        return dry_run_results, True

    return _QueueFlush(
        queue, args, token, on_progress=on_progress,
        refresh_review=refresh_review,
        consolidate_duplicates=consolidate_duplicates,
        results_out=results_out,
    ).run()
