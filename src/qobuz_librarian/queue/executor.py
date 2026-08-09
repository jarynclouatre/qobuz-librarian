"""Queue executor: download, pre-import hooks, beets, backup resolution."""
import errno
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.completion import (
    CompletionOrigin,
    CompletionOriginKind,
    RecoveryOwner,
    SourceTransitionKind,
    normalise_album_id,
    parse_completion_input_record,
)
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
    _consolidate_duplicate_albums,
    beets_import_albums,
    relocate_disc_album_artwork,
    retire_backup_beets_entries,
    staging_preflight,
)
from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE, downsample_dir
from qobuz_librarian.integrations.lyrics import (
    _record_post_import_lyric_retry,
    _resolve_signatures_to_paths,
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
    capture_album_source_receipt,
    carry_backup_companions,
    dispose_backup,
    pin_unverified_upgrade_backup,
    restore_gap_fill_backup,
    restore_upgrade_backup,
    warn_pin_failed,
)
from qobuz_librarian.library.catalog import (
    _count_audio_files_in,
    _is_split_album_merge,
    find_album_dir_by_track_signatures,
    find_album_dir_filesystem,
    folder_holds_all_tracks,
    prompt_and_migrate_multi_artist_folder,
    track_signatures_for_album_dirs,
)
from qobuz_librarian.library.post_import_relocation import (
    PostImportRelocationAttention,
    PostImportRelocationUnavailable,
    RelocationKind,
    relocate_post_import_album,
)
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.quality.decision import mark_local_album_capped
from qobuz_librarian.quality.verify import (
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
    StartupRecoveryAction,
    StartupRecoveryStatus,
    recover_startup_state,
)
from qobuz_librarian.repair_log import warn_if_download_truncated
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import log_fetch

# Installed by the web layer (web/jobs): called when a staged download stays
# under the quality target after the recovery attempt, so the owning job can
# carry an attention marker. The CLI leaves it unset.
on_quality_shortfall = None


@dataclass(frozen=True, slots=True)
class PreImportHooksResult:
    lyric_sigs: list
    resampled: int
    errors: int = 0
    saved_bytes: int = 0
    flush_warnings: int = 0
    cancelled: bool = False

    def __iter__(self):
        """Keep the established two-value unpacking contract."""
        yield self.lyric_sigs
        yield self.resampled


def _seal_queue_item_siblings(item):
    receipts = []
    for sibling in item.get("siblings_to_delete", []):
        receipt = capture_album_source_receipt(sibling)
        receipts.append(receipt)
        if receipt is None:
            log.info(fmt(
                C.YELLOW,
                f"  ⚠  Keeping sibling {Path(sibling).name}; it couldn't "
                "be sealed safely before the download.",
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


def _ownership_location(relative, identity):
    return {"relative": relative, **{
        field: identity[field] for field in _IMPORT_OWNERSHIP_IDENTITY_FIELDS
    }}


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

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        return None
    dir_flags = os.O_RDONLY | nofollow | directory | getattr(
        os, "O_CLOEXEC", 0)
    leaf_flags = (
        os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
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
    item.pop("_import_ownership_created_directories", None)
    item.pop("_import_ownership_created_files", None)
    item.pop("_import_ownership_directory_changes", None)


def _strict_relative_ancestor(directory, file_relative):
    directory_parts = PurePosixPath(directory).parts
    file_parts = PurePosixPath(file_relative).parts
    return (
        len(directory_parts) < len(file_parts)
        and file_parts[:len(directory_parts)] == directory_parts
    )


def _relocation_location_key(value):
    return (
        value["relative"],
        *(value[field] for field in _IMPORT_OWNERSHIP_IDENTITY_FIELDS),
    )


def _validated_relocation_changes(receipt, name):
    changes = receipt.get(name) if isinstance(receipt, dict) else None
    if not isinstance(changes, list):
        return None
    by_before = {}
    after_keys = set()
    for change in changes:
        before = change.get("before") if isinstance(change, dict) else None
        after = change.get("after") if isinstance(change, dict) else None
        if (
            not isinstance(change, dict)
            or set(change) != {"before", "after"}
            or not _valid_import_ownership_location(before)
            or not _valid_import_ownership_location(after)
            or set(before) != {
                "relative", *_IMPORT_OWNERSHIP_IDENTITY_FIELDS}
            or set(after) != {
                "relative", *_IMPORT_OWNERSHIP_IDENTITY_FIELDS}
        ):
            return None
        before_key = _relocation_location_key(before)
        after_key = _relocation_location_key(after)
        if before_key in by_before or after_key in after_keys:
            return None
        by_before[before_key] = dict(after)
        after_keys.add(after_key)
    return by_before


def _advance_import_ownership_after_relocation(item, receipt):
    """Carry a committed relocation into an import's exact Undo proof."""
    payload = item.get("_import_ownership")
    records = payload.get("items") if isinstance(payload, dict) else None
    root = payload.get("root") if isinstance(payload, dict) else None
    file_changes = _validated_relocation_changes(receipt, "file_changes")
    directory_changes = _validated_relocation_changes(
        receipt, "directory_changes")
    released = (
        receipt.get("released_files", [])
        if isinstance(receipt, dict) else None
    )
    created = (
        receipt.get("created_directories")
        if isinstance(receipt, dict) else None
    )
    if (
        not isinstance(records, list)
        or not records
        or not isinstance(root, str)
        or root != os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
        or not isinstance(receipt, dict)
        or type(receipt.get("version")) is not int
        or receipt.get("version") != 2
        or file_changes is None
        or directory_changes is None
        or not isinstance(released, list)
        or not isinstance(created, list)
    ):
        _clear_import_ownership(item)
        return

    changed_file_locations = set(file_changes)
    changed_file_locations.update(
        _relocation_location_key(after) for after in file_changes.values())
    released_locations = {}
    for entry in released:
        if (
            not _valid_import_ownership_location(entry)
            or set(entry) != {
                "relative", *_IMPORT_OWNERSHIP_IDENTITY_FIELDS}
        ):
            _clear_import_ownership(item)
            return
        key = _relocation_location_key(entry)
        if key in released_locations or key in changed_file_locations:
            _clear_import_ownership(item)
            return
        released_locations[key] = dict(entry)

    created_by_relative = {}
    for entry in created:
        if (
            not _valid_import_ownership_location(entry)
            or set(entry) != {
                "relative", *_IMPORT_OWNERSHIP_IDENTITY_FIELDS}
            or entry["relative"] in created_by_relative
        ):
            _clear_import_ownership(item)
            return
        created_by_relative[entry["relative"]] = dict(entry)

    updates = []
    final_leaves = set()
    relocation_created = {}
    released_uses = {key: 0 for key in released_locations}
    for record in records:
        current_identity = (
            record.get("file") if isinstance(record, dict) else None)
        current_relative = (
            record.get("relative") if isinstance(record, dict) else None)
        companions = (
            record.get("companions") if isinstance(record, dict) else None)
        record_created = (
            record.get("created_directories")
            if isinstance(record, dict) else None
        )
        if (
            not _valid_import_ownership_identity(current_identity)
            or not _valid_import_ownership_relative(current_relative)
            or not isinstance(companions, list)
            or len(companions) > 1
            or not isinstance(record_created, list)
        ):
            _clear_import_ownership(item)
            return

        current = _ownership_location(current_relative, current_identity)
        current_key = _relocation_location_key(current)
        if current_key in released_locations:
            _clear_import_ownership(item)
            return
        final = file_changes.get(current_key, current)
        final_key = _relocation_location_key(final)
        if final_key in final_leaves:
            _clear_import_ownership(item)
            return
        final_leaves.add(final_key)
        if final is not current:
            try:
                final_path = Path(root).joinpath(
                    *PurePosixPath(final["relative"]).parts)
                final_path.relative_to(Path(os.path.abspath(os.fspath(
                    item["_resolved_post_dir"]))))
            except (KeyError, OSError, TypeError, ValueError):
                _clear_import_ownership(item)
                return

        advanced_created = []
        advanced_relative = set()
        for existing in record_created:
            if (
                not _valid_import_ownership_location(existing)
                or set(existing) != {
                    "relative", *_IMPORT_OWNERSHIP_IDENTITY_FIELDS}
            ):
                _clear_import_ownership(item)
                return
            advanced = directory_changes.get(
                _relocation_location_key(existing), existing)
            changed_inode = (
                advanced["device"], advanced["inode"]
            ) != (existing["device"], existing["inode"])
            if (
                changed_inode
                and created_by_relative.get(advanced["relative"])
                != advanced
            ):
                continue
            if not _strict_relative_ancestor(
                advanced["relative"], final["relative"]
            ):
                continue
            previous = next(
                (
                    entry for entry in advanced_created
                    if entry["relative"] == advanced["relative"]
                ),
                None,
            )
            if previous is not None and previous != advanced:
                _clear_import_ownership(item)
                return
            if previous is None:
                advanced_created.append(dict(advanced))
                advanced_relative.add(advanced["relative"])

        final_companions = []
        if companions:
            companion = companions[0]
            companion_identity = (
                companion.get("file")
                if isinstance(companion, dict) else None)
            companion_relative = (
                companion.get("relative")
                if isinstance(companion, dict) else None)
            if (
                not isinstance(companion, dict)
                or set(companion) != {"kind", "relative", "file"}
                or companion.get("kind") != "artwork"
                or not _exact_import_ownership_identity(companion_identity)
                or not _valid_import_ownership_relative(companion_relative)
                or companion_relative == current_relative
            ):
                _clear_import_ownership(item)
                return
            companion_location = _ownership_location(
                companion_relative, companion_identity)
            companion_key = _relocation_location_key(companion_location)
            if companion_key in released_locations:
                released_uses[companion_key] += 1
            else:
                final_companion = file_changes.get(
                    companion_key,
                    companion_location,
                )
                final_companion_key = _relocation_location_key(
                    final_companion)
                companion_parent = PurePosixPath(
                    final_companion["relative"]).parent.as_posix()
                if (
                    final_companion["relative"] == final["relative"]
                    or not _strict_relative_ancestor(
                        companion_parent, final["relative"])
                ):
                    if companion_key in file_changes:
                        _clear_import_ownership(item)
                        return
                else:
                    if final_companion_key in final_leaves:
                        _clear_import_ownership(item)
                        return
                    final_leaves.add(final_companion_key)
                    final_companions.append({
                        "kind": "artwork",
                        "relative": final_companion["relative"],
                        "file": _ownership_identity_from_location(
                            final_companion),
                    })

        for relative, entry in created_by_relative.items():
            if (
                relative not in advanced_relative
                and _strict_relative_ancestor(relative, final["relative"])
            ):
                previous = relocation_created.get(relative)
                if previous is not None and previous != entry:
                    _clear_import_ownership(item)
                    return
                relocation_created[relative] = dict(entry)

        updates.append({
            "record": record,
            "relative": final["relative"],
            "file": _ownership_identity_from_location(final),
            "created_directories": advanced_created,
            "companions": final_companions,
        })

    created_files = item.get("_import_ownership_created_files", [])
    if not isinstance(created_files, list):
        _clear_import_ownership(item)
        return
    advanced_created_files = []
    for record in created_files:
        path = record.get("path") if isinstance(record, dict) else None
        identity = record.get("file") if isinstance(record, dict) else None
        try:
            relative = Path(os.path.abspath(os.fspath(path))).relative_to(
                Path(root)
            ).as_posix()
        except (OSError, TypeError, ValueError):
            _clear_import_ownership(item)
            return
        if (
            set(record) != {"path", "file"}
            or not _valid_import_ownership_relative(relative)
            or not _exact_import_ownership_identity(identity)
        ):
            _clear_import_ownership(item)
            return
        current = _ownership_location(relative, identity)
        current_key = _relocation_location_key(current)
        if current_key in released_locations:
            released_uses[current_key] += 1
            continue
        final_created = file_changes.get(current_key, current)
        final_key = _relocation_location_key(final_created)
        try:
            final_path = Path(root).joinpath(
                *PurePosixPath(final_created["relative"]).parts
            )
            final_path.relative_to(Path(os.path.abspath(os.fspath(
                item["_resolved_post_dir"]
            ))))
        except (KeyError, OSError, TypeError, ValueError):
            if current_key in file_changes:
                _clear_import_ownership(item)
                return
            continue
        if final_key in final_leaves:
            _clear_import_ownership(item)
            return
        final_leaves.add(final_key)
        advanced_created_files.append({
            "path": os.path.abspath(os.fspath(final_path)),
            "file": _ownership_identity_from_location(final_created),
        })

    if any(uses > 1 for uses in released_uses.values()):
        _clear_import_ownership(item)
        return
    for update in updates:
        record = update.pop("record")
        record.update(update)
    if relocation_created:
        item["_import_ownership_created_directories"] = list(
            relocation_created.values())
    else:
        item.pop("_import_ownership_created_directories", None)
    if advanced_created_files:
        item["_import_ownership_created_files"] = advanced_created_files
    else:
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
    extra = item.get("_import_ownership_created_directories")
    if extra is not None:
        groups.append(extra)
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
        result=item,
    )


def _staged_album_dirs(item):
    return validated_staged_album_dirs(item)


def _run_pre_import_hooks_for_dirs(
    album_dirs,
    args,
    *,
    on_sources_changed=None,
):
    """Run downsample + lyric hooks scoped to ``album_dirs``.

    The result retains two-value unpacking for older callers while exposing the
    complete downsample outcome for truthful album and Job summaries.
    """
    sigs = []
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
            lh_result = _run_lyric_hook(d)
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW, "  ⚠  lyric hook interrupted"))
            raise
        except Exception as _le:
            log.info(fmt(C.YELLOW, f"  ⚠  lyric hook failed: {_le}"))
            lh_result = None
            # Hook crashed; capture signatures of every staged FLAC under this
            # album dir so the post-import resolution still has a shot. Recording
            # staging paths here would go stale once beets moves the files.
            try:
                from qobuz_librarian.integrations.rip import _flac_signature
                for p in sorted(d.rglob("*.flac")):
                    sig = _flac_signature(p)
                    if sig is not None:
                        sigs.append((sig, str(p)))
            except Exception:
                pass
        if isinstance(lh_result, tuple) and len(lh_result) == 2:
            sigs.extend(lh_result[1])
    if on_sources_changed is not None:
        on_sources_changed(SourceTransitionKind.LYRICS_TAG)
    return PreImportHooksResult(
        lyric_sigs=sigs,
        resampled=resampled_total,
        errors=downsample_errors,
        saved_bytes=downsample_saved_bytes,
        flush_warnings=downsample_flush_warnings,
        cancelled=downsample_cancelled,
    )


def _pre_import_staging_hooks(args, album_dirs=None):
    """Compatibility wrapper for the single-album pre-import hooks.

    New downloads pass their validated per-run album roots. The whole-staging
    default remains only for older direct callers.
    """
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
        album_dirs, *, ownership_out=None, source_receipts=None):
    """Run beets import on ``album_dirs`` with up to ``BEETS_MAX_ATTEMPTS``
    attempts, retrying only on idle-timeout (other failures aren't transient).
    Returns True on import success, False on permanent failure."""
    if not album_dirs:
        return True
    if ownership_out is not None:
        ownership_out.clear()
    max_attempts = max(1, int(cfg.BEETS_MAX_ATTEMPTS))
    for attempt in range(1, max_attempts + 1):
        if source_receipts is not None and not all(
                tree_matches(receipt) for receipt in source_receipts):
            return False
        if ownership_out is None:
            kind = beets_import_albums(album_dirs)
            attempt_capture = None
        else:
            attempt_capture = {}
            kind = beets_import_albums(
                album_dirs,
                ownership_out=attempt_capture,
            )
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
                f"  ⚠  couldn't safely park {directory.name}; no exact "
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
                f"  ⚠  couldn't safely park {directory.name}; it changed "
                "during the import attempt and was left in staging."))
    if moved:
        log.info(fmt(C.YELLOW,
            f"  ⏭  parked {moved} album dir(s) under "
            f"{cfg.BEETS_RETRY_DIR}/ "
            f"; it will re-import on the next download run."))
    return parked


def _reimport_parked_albums():
    """Re-attempt beets import on albums an earlier batch parked under
    ``STAGING_DIR/<BEETS_RETRY_DIR>/``. A park almost always means a transient
    cause, such as a DB lock, idle timeout, or momentarily busy disk, so retrying
    the import (never the download; the files are already on disk) at the start
    of the next flush clears the backlog without re-fetching anything. Groups
    that still fail stay parked for the run after. Returns (anything_imported,
    landed_library_dirs): the flag drives the duplicate-album fold, and the
    resolved dirs feed the post-import lyric finaliser. A parked reimport
    that skipped it kept its embedded lyrics and never got its .lrc sidecar."""
    groups = list_groups(kind="beets")
    any_ok = False
    landed_dirs = []
    for group in groups:
        album_dirs = [tree.path for tree in group.trees]
        log.info(fmt(C.GRAY,
            f"  ↻  Re-importing parked album(s): "
            f"{truncate(group.path.name, 50)}"))
        # Tag signatures survive beets moving/renaming the files, so the
        # landed folder is findable after the import for the lyric finaliser.
        sigs_by_dir = {d: track_signatures_for_album_dirs([d])
                       for d in album_dirs}
        _import_album_with_retry(
            album_dirs, source_receipts=group.trees)
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
                f"  ⏭  Parked album in {truncate(group.path.name, 50)} still "
                "holds exact tracks; left for a later run."))
        elif outcome["reason"] in {"changed", "companions"}:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Kept recovery folder {group.path}: it contains "
                "unverified changes or companion files and won't be "
                "automatically replayed again."))
    return any_ok, landed_dirs


_RETRYABLE_STOP_RESULTS = {
    "cancelled", "interrupted", "auth_lost", "disk_full", "io_error",
    "upgrade_aborted_backup_failed", "import_failed",
}


def _queue_item_needs_retry(item):
    """Whether an item should stay queued for a later run.

    Kept: items stopped before they could finish (cancel / interrupt / auth
    loss / disk full / an upgrade whose backup couldn't be taken), imports that
    were not transactionally accepted, and downloads that landed nothing.
    Dropped: only completed download-phase items without one of those explicit
    retry markers."""
    if item.get("result") in _RETRYABLE_STOP_RESULTS:
        return True
    return item.get("n_ok", 0) == 0


def _reunite_split_album(
    item,
    album_dir,
    post_dir,
    *,
    authority=None,
    await_handoff=False,
    operation_id_out=None,
):
    """Safely reunite one album that Beets filed across artist folders."""
    if type(await_handoff) is not bool:
        raise ValueError("the relocation handoff option is invalid")
    operation_capture = (
        operation_id_out if isinstance(operation_id_out, dict) else None
    )
    if await_handoff and operation_capture is None:
        raise ValueError("a deferred relocation requires an operation receiver")
    if operation_capture is not None:
        operation_capture.clear()
    split_artist = (item["album"].get("artist") or {}).get("name") or ""
    if not _is_split_album_merge(album_dir, post_dir, split_artist):
        return post_dir
    try:
        if authority is not None:
            _require_executor_authority(authority)
        relocation_kwargs = {
            "kind": RelocationKind.SPLIT_GAP_FILL,
            "authority": authority,
        }
        if await_handoff:
            relocation_kwargs["await_handoff"] = True
        relocation = relocate_post_import_album(
            album_dir,
            post_dir,
            **relocation_kwargs,
        )
        if authority is not None:
            _require_executor_authority(authority)
    except PostImportRelocationAttention:
        raise
    except (PostImportRelocationUnavailable, OSError, ValueError) as exc:
        if authority is not None:
            _require_executor_authority(authority)
        log.info(fmt(
            C.YELLOW,
            "  ⚠  Beets placed this album across two artist folders; "
            "the automatic consolidation was skipped safely and "
            "neither folder was overwritten.",
        ))
        vlog(f"split-folder consolidation refused: {exc}")
        return post_dir

    if not relocation.changed:
        reason = relocation.reason or "nothing was safe to move"
        log.info(fmt(
            C.YELLOW,
            "  ⚠  Beets placed this album across two artist "
            f"folders; both were kept because {reason}.",
        ))
        return post_dir

    post_dir = relocation.destination
    if operation_capture is not None:
        if relocation.operation_id is None:
            raise PostImportRelocationAttention(
                "the split-folder relocation lost its recovery identity"
            )
        operation_capture["operation_id"] = relocation.operation_id
    item["_resolved_post_dir"] = post_dir
    receipt = relocation.ownership_receipt
    if (
        isinstance(item.get("_import_ownership"), dict)
        and isinstance(receipt, dict)
    ):
        _advance_import_ownership_after_relocation(item, receipt)
    elif isinstance(item.get("_import_ownership"), dict):
        _clear_import_ownership(item)
    clear_scan_caches()
    message = (
        "  ✓  Consolidated the existing album files into "
        f"{truncate(post_dir.name, 40)} without overwriting."
    )
    if relocation.reason:
        message += f" {relocation.reason.capitalize()}."
    log.info(fmt(C.GREEN, message))
    return post_dir


def _resolve_queue_item(
    item,
    args,
    imported_globally,
    *,
    authority=None,
    record_fetch=True,
):
    """Resolve backup and compute result for one completed queue item.

    Handles art cleanup, safe folder consolidation, sibling retention, and
    backup restoration. Mutates item["imported"] and
    item["_resolved_post_dir"].
    Returns a result dict in process_album's shape.
    """
    item["imported"] = imported_globally
    bp = item.get("backup_path")
    album_dir = item["album_dir"]
    _sibs = item.get("siblings_to_delete", [])
    item["_siblings_preserved"] = [os.fspath(path) for path in _sibs]

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

    ownership_move = None
    item.pop("_post_import_relocation_pending", None)
    deferred_relocation_handoff = (
        item.get("_defer_post_import_relocation_handoff") is True
    )
    migration_requested = (
        getattr(args, "migrate_multi_artist", False)
        and _item_strict_success
        and (
            not ownership_source_required
            or deferred_relocation_handoff
        )
    )
    migration_source_dir = ownership_source_dir
    if migration_requested and not ownership_source_required:
        migration_source_dir = find_album_dir_by_track_signatures(
            item.get("post_import_signatures")
        )
    deferred_web_single_migration = (
        migration_requested
        and deferred_relocation_handoff
        and ownership_source_required
        and ownership_scope_used
    )
    deferred_web_split_reunion = (
        deferred_relocation_handoff
        and ownership_source_required
        and ownership_scope_used
        and _item_strict_success
        and _is_split_album_merge(
            album_dir,
            ownership_source_dir,
            (item["album"].get("artist") or {}).get("name") or "",
        )
    )
    if deferred_web_single_migration or deferred_web_split_reunion:
        item["_post_import_relocation_pending"] = True
    if (
        migration_requested
        and migration_source_dir is not None
        and not deferred_web_single_migration
    ):
        ownership_move = (
            {} if isinstance(item.get("_import_ownership"), dict) else None
        )
        if authority is not None:
            _require_executor_authority(authority)
        migration_kwargs = {
            "ownership_move_out": ownership_move,
            "authority": authority,
            "source_dir": migration_source_dir,
        }
        _migrated = prompt_and_migrate_multi_artist_folder(
            item["album"],
            args,
            **migration_kwargs,
        )
        if authority is not None:
            _require_executor_authority(authority)
    else:
        _migrated = None
        if (
            migration_requested
            and ownership_source_required
            and not ownership_scope_used
        ):
            log.info(fmt(
                C.YELLOW,
                "  ⚠  Multi-artist folder filing was skipped because the "
                "exact imported album folder could not be verified.",
            ))
        elif migration_requested and migration_source_dir is None:
            log.info(fmt(
                C.YELLOW,
                "  ⚠  Multi-artist folder filing was skipped because the "
                "exact imported album could not be located.",
            ))
    post_dir = _migrated
    post_dir_exact = post_dir is not None
    if post_dir is None and ownership_scope_used:
        post_dir = ownership_source_dir
        post_dir_exact = True
    if (
        post_dir is None
        and imported_globally
        and not ownership_source_required
    ):
        post_dir = find_album_dir_by_track_signatures(
            item.get("post_import_signatures"))
        post_dir_exact = post_dir is not None
    if post_dir is None and not ownership_source_required:
        post_dir = find_album_dir_filesystem(item["album"])
    # For brand-new albums (album_dir=None), find_album_dir_filesystem may
    # return None if the cache hasn't refreshed. Clear and retry once.
    if (
        post_dir is None
        and imported_globally
        and not ownership_source_required
    ):
        clear_scan_caches()
        post_dir = find_album_dir_filesystem(item["album"])
    if post_dir is None:
        post_dir = album_dir
    album_has_content = (
        post_dir is not None and post_dir.exists()
        and _count_audio_files_in(post_dir) > 0
    )
    # Stash resolved post-import dir so the lyric-retry resolver can
    # use it without redoing the find_album_dir_filesystem dance.
    item["_resolved_post_dir"] = post_dir
    item["_resolved_post_dir_from_import_ownership"] = (
        ownership_source_required
    )
    if (
        isinstance(item.get("_import_ownership"), dict)
        and isinstance(ownership_move, dict)
        and ownership_move.get("version") == 2
    ):
        _advance_import_ownership_after_relocation(item, ownership_move)
    elif (
        isinstance(item.get("_import_ownership"), dict)
        and ownership_scope_used
        and post_dir is not None
        and os.path.abspath(os.fspath(post_dir))
        != os.path.abspath(os.fspath(ownership_source_dir))
    ):
        _clear_import_ownership(item)
    had_any_success = (
        imported_globally and item.get("n_ok", 0) > 0 and album_has_content
    )

    if had_any_success:
        if post_dir:
            if item.get("resampled_n", 0) > 0:
                mark_local_album_capped(post_dir, qobuz_album=item["album"])
        if not ownership_source_required and post_dir_exact:
            post_dir = _reunite_split_album(
                item,
                album_dir,
                post_dir,
                authority=authority,
            )
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
                    f"  ⚠  Keeping {len(_sibs)} sibling folder(s); the "
                    "replacement couldn't be held as an exact safe tree.",
                ))
            else:
                for sib_dir, receipt in zip(_sibs, receipts):
                    sibling_path = Path(sib_dir)
                    name = sibling_path.name
                    if receipt is None:
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling {name}; it wasn't sealed "
                            "before the download.",
                        ))
                        continue
                    sibling_backup = backup_album_dir(
                        sibling_path, expected_receipt=receipt)
                    if sibling_backup is None:
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling {name}; its exact tree "
                            "changed or couldn't be moved safely.",
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
                    if not retire_backup_beets_entries(
                        sibling_backup,
                        post_dir,
                        replacement_receipt,
                    ):
                        if not pin_unverified_upgrade_backup(
                                sibling_backup,
                                "sibling backup kept; the replaced Beets "
                                "entries could not be retired safely"):
                            warn_pin_failed(sibling_backup)
                        log.info(fmt(
                            C.YELLOW,
                            f"  ⚠  Kept sibling recovery for {name} at "
                            f"{sibling_backup.path}; its Beets catalogue "
                            "couldn't be reconciled safely.",
                        ))
                        break
                    retired = dispose_backup(
                        sibling_backup,
                        replacement_path=post_dir,
                        expected_replacement_receipt=replacement_receipt,
                        replacement_validator=lambda replacement, _backup: (
                            folder_holds_all_tracks(
                                replacement, _album_tracks,
                                destructive=True)
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
                        f"  ✓  Safely consolidated sibling {name}.",
                    ))
        elif _sibs and _sibs_clean:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Keeping {len(_sibs)} sibling folder(s); the filled "
                f"folder couldn't be verified as holding every track, and a "
                f"sibling may hold the only copy of what's missing."))
        elif _sibs:
            log.info(fmt(C.GRAY,
                f"  · Keeping {len(_sibs)} sibling(s) "
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
                _upgrade_replacement_verified,
                _upgrade_trees_verified,
            )
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
                    replacement_path, replacement_receipt = carried
                    if not retire_backup_beets_entries(
                        bp,
                        replacement_path,
                        replacement_receipt,
                    ):
                        item["upgrade_unverified"] = True
                        item["catalogue_unverified"] = True
                        if not pin_unverified_upgrade_backup(
                                bp,
                                "upgrade kept; the replaced Beets entries "
                                "could not be retired safely"):
                            warn_pin_failed(bp)
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  Couldn't safely reconcile the Beets "
                            f"catalogue for {truncate(album_dir.name, 40)}; "
                            "keeping the exact backup."))
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
                            f"  ⚠  Couldn't safely remove the exact backup "
                            f"for {truncate(album_dir.name, 40)}."))
                else:
                    item["upgrade_unverified"] = True
                    if not pin_unverified_upgrade_backup(bp):
                        warn_pin_failed(bp)
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  {truncate(album_dir.name, 40)}: upgraded, but "
                        "the rebuilt album or its companions couldn't be "
                        "flushed safely; keeping the backup."))
                    log.info(fmt(C.GRAY,
                        f"     Backup at {bp}; keep it until you've confirmed "
                        "the rebuilt album is safe."))
            else:
                item["upgrade_unverified"] = True
                if not pin_unverified_upgrade_backup(bp):
                    warn_pin_failed(bp)
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {truncate(album_dir.name, 40)}: upgrade couldn't be "
                    f"verified as complete; keeping your original."))
                log.info(fmt(C.GRAY,
                    f"     Original preserved at {bp} "
                    f"(kept until you confirm the upgrade landed)."))
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
                log.info(fmt(C.WHITE, f"     Manual: mv {bp} {album_dir}"))

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
            )
        )
        _filled_receipt = (
            capture_album_source_receipt(post_dir)
            if _item_strict_success and _filled_whole else None
        )
        if _item_strict_success and _filled_whole and _filled_receipt is not None:
            if not retire_backup_beets_entries(
                gfb,
                post_dir,
                _filled_receipt,
            ):
                item["recovery_unverified"] = True
                item["catalogue_unverified"] = True
                if not pin_unverified_upgrade_backup(
                        gfb,
                        "gap-fill backup kept; the replaced Beets entries "
                        "could not be retired safely"):
                    warn_pin_failed(gfb)
                log.info(fmt(C.YELLOW,
                    "  ⚠  Gap-fill complete but its Beets catalogue "
                    "couldn't be reconciled safely; keeping the exact backup."))
            elif not dispose_backup(
                gfb,
                replacement_path=post_dir,
                expected_replacement_receipt=_filled_receipt,
                replacement_validator=lambda replacement, _backup: (
                    folder_holds_all_tracks(
                        replacement,
                        (item["album"].get("tracks") or {}).get("items") or [],
                        destructive=True,
                    )
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
                    "be safely removed."))
        elif _item_strict_success and _filled_whole:
            item["recovery_unverified"] = True
            if not pin_unverified_upgrade_backup(
                    gfb, "gap-fill backup kept; replacement not durable"):
                warn_pin_failed(gfb)
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: filled, but the new "
                "folder couldn't be flushed safely; keeping the backed-up "
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
                    # failure. Say so instead of "gap-fill did not succeed".
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
                        f"  ✓  Restored {_n_back} track(s) to "
                        f"{truncate(_restore_target.name, 50)}"))
                else:
                    item["recovery_unverified"] = True
                    if gfb.exists() and not pin_unverified_upgrade_backup(
                            gfb,
                            "gap-fill backup kept; automatic restore was "
                            "partial or failed"):
                        warn_pin_failed(gfb)
                    log.info(fmt(C.RED,
                        f"  ✗  Restored {_n_back} track(s); remaining originals "
                        f"are preserved at:\n     {gfb}"))

    n_ok = item.get("n_ok", 0)
    n_fail = item.get("n_fail", 0)
    n_retryable, n_lossy_only = incomplete_track_counts(item)
    outcome_attention = _queue_outcome_needs_attention(item)
    # A stop marker (cancel discarded the staged files; disk-full / I/O / auth
    # stopped the batch) must keep its label. n_ok still counts the tracks that
    # briefly landed, so the n_ok branch below would mislabel a discarded cancel
    # as "downloaded" in the fetch log.
    _stop = item.get("result")
    if _stop in (
            "cancelled", "interrupted", "disk_full", "io_error", "auth_lost",
            "import_failed", "upgrade_aborted_backup_failed"):
        status = _stop
    elif n_ok and (n_retryable or n_lossy_only or outcome_attention):
        status = "partial"
    elif n_ok:
        status = "downloaded"
    elif n_retryable or n_lossy_only:
        status = "failed"
    else:
        status = "nothing_landed"

    if record_fetch:
        log_fetch({
            "ts": datetime.now(timezone.utc).isoformat(),
            "album_id": item["album"].get("id"),
            "artist": (item["album"].get("artist") or {}).get("name"),
            "title": item["album"].get("title"),
            "result": status,
            "tracks_total": len(
                (item["album"].get("tracks") or {}).get("items") or []
            ),
            "tracks_downloaded": n_ok,
            "tracks_failed": n_fail,
            "tracks_lossy_deleted": item.get("n_lossy", 0),
            "failed_titles": item.get("failed_tracks", []),
            "lossy_titles": item.get("lossy_tracks", []),
            "broken_titles": item.get("broken_tracks", []),
            "downsample_errors": item.get("downsample_errors", 0),
            "downsample_saved_bytes": item.get("downsample_saved_bytes", 0),
            "downsample_flush_warnings": item.get(
                "downsample_flush_warnings", 0
            ),
            "downsample_cancelled": bool(
                item.get("downsample_cancelled", False)
            ),
            "upgrade_unverified": bool(item.get("upgrade_unverified", False)),
            "catalogue_unverified": bool(
                item.get("catalogue_unverified", False)
            ),
            "recovery_unverified": bool(
                item.get("recovery_unverified", False)
            ),
            "siblings_preserved": item.get("_siblings_preserved", []),
            "imported": imported_globally,
            "auto_upgrade": item["auto_upgrade"],
            "elapsed_s": int(item.get("elapsed", 0)),
            "queued": True,
        })
    return {
        "dir": album_dir,
        "result": status,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_lossy": item.get("n_lossy", 0),
        "n_broken": max(n_retryable - n_fail, 0),
        "n_lossy_only": n_lossy_only,
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
        "siblings_preserved": item.get("_siblings_preserved", []),
        "imported": imported_globally,
        "auto_upgrade": item["auto_upgrade"],
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


def _refresh_review_state_after_downloads(results, token, args):
    """Keep the saved Upgrade/Downsample review state fresh after a CLI download
    batch. The WebUI reads that saved state, and WEBUI_SCAN_CONTRACT requires CLI
    file changes to update it too. The web download path does the same via
    flows._refresh_after_local_album_change. One re-scan per changed artist;
    best-effort, so a refresh hiccup never fails a download that already landed."""
    from pathlib import Path

    changed = []
    seen = set()
    for r in results:
        if not (r.get("imported") and r.get("n_ok", 0) > 0 and r.get("dir")):
            continue
        artist_dir = Path(r["dir"]).parent
        if str(artist_dir) not in seen:
            seen.add(str(artist_dir))
            changed.append(artist_dir)
    if not changed:
        return
    try:
        from qobuz_librarian.library import downsample_state
        from qobuz_librarian.library import hidden as hidden_mod
        from qobuz_librarian.quality import upgrade_state
        from qobuz_librarian.quality.decision import load_capped
        from qobuz_librarian.web import review_badges
    except Exception as e:
        vlog(f"post-download review-state refresh unavailable: {e}")
        return
    hidden = hidden_mod.load()
    capped = load_capped()
    for artist_dir in changed:
        try:
            upgrade_state.update_artist(artist_dir, token=token, args=args,
                                        capped=capped, hidden=hidden)
            downsample_state.update_artist(artist_dir, hidden=hidden)
        except Exception as e:
            vlog(f"post-download review-state refresh failed for {artist_dir}: {e}")
    try:
        review_badges.set_ready(
            "upgrade",
            upgrade_state.has_visible_candidates(upgrade_state.load(), hidden))
        review_badges.set_ready(
            "downsample",
            downsample_state.has_visible_candidates(downsample_state.load(), hidden))
    except Exception as e:
        vlog(f"post-download review-badge refresh failed: {e}")


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
            "the exact saved queue item cannot safely resume"
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
    """Publish the ordinary queue/log shape without heuristic resolution."""
    item["imported"] = True
    item["_resolved_post_dir"] = post_dir
    item["_resolved_post_dir_from_import_ownership"] = False
    item["_siblings_preserved"] = []
    n_ok = item.get("n_ok", 0)
    n_fail = item.get("n_fail", 0)
    n_retryable, n_lossy_only = incomplete_track_counts(item)
    outcome_attention = _queue_outcome_needs_attention(item)
    if n_ok and (n_retryable or n_lossy_only or outcome_attention):
        status = "partial"
    elif n_retryable or n_lossy_only:
        status = "failed"
    else:
        status = "downloaded"
    log_fetch({
        "ts": datetime.now(timezone.utc).isoformat(),
        "album_id": item["album"].get("id"),
        "artist": (item["album"].get("artist") or {}).get("name"),
        "title": item["album"].get("title"),
        "result": status,
        "tracks_total": len(
            (item["album"].get("tracks") or {}).get("items") or []
        ),
        "tracks_downloaded": n_ok,
        "tracks_failed": n_fail,
        "tracks_lossy_deleted": item.get("n_lossy", 0),
        "failed_titles": item.get("failed_tracks", []),
        "lossy_titles": item.get("lossy_tracks", []),
        "broken_titles": item.get("broken_tracks", []),
        "downsample_errors": item.get("downsample_errors", 0),
        "downsample_saved_bytes": item.get("downsample_saved_bytes", 0),
        "downsample_flush_warnings": item.get(
            "downsample_flush_warnings", 0
        ),
        "downsample_cancelled": bool(item.get("downsample_cancelled", False)),
        "upgrade_unverified": bool(item.get("upgrade_unverified", False)),
        "catalogue_unverified": bool(item.get("catalogue_unverified", False)),
        "recovery_unverified": bool(item.get("recovery_unverified", False)),
        "siblings_preserved": [],
        "imported": True,
        "auto_upgrade": bool(item.get("auto_upgrade")),
        "elapsed_s": int(item.get("elapsed", 0)),
        "queued": True,
    })
    return {
        "dir": post_dir,
        "result": status,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_lossy": item.get("n_lossy", 0),
        "n_broken": max(n_retryable - n_fail, 0),
        "n_lossy_only": n_lossy_only,
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
    }


def _queue_done_line(results, n_items):
    """Colour and text for the closing queue summary.

    A parked, blocked, cancelled, or interrupted album downloads its tracks
    without one of them failing, so the verdict reads the album tally as well
    as the track tally.
    """
    n_success = sum(1 for r in results if r.get("result") == "downloaded")
    n_total_ok = sum(r.get("n_ok", 0) for r in results)
    n_total_missing = sum(sum(incomplete_track_counts(r)) for r in results)
    n_unfinished = max(0, n_items - n_success)
    clear = n_total_missing == 0 and n_unfinished == 0
    text = (f"  {'✓' if clear else '⚠'} Queue done: "
            f"{n_success}/{n_items} albums OK · "
            f"{n_total_ok} track{'s' if n_total_ok != 1 else ''} downloaded · "
            f"{n_total_missing} track{'s' if n_total_missing != 1 else ''} unresolved")
    if n_unfinished:
        text += (f" · {n_unfinished} album"
                 f"{'s' if n_unfinished != 1 else ''} unfinished")
    return (C.GREEN if clear else C.YELLOW), text


def _durable_completion_line(result, elapsed):
    n_ok = result.get("n_ok", 0)
    if result.get("result") == "downloaded":
        return C.GREEN, f"    ✓ {n_ok} track(s) · {int(elapsed)}s"

    details = []
    unresolved = sum(incomplete_track_counts(result))
    if unresolved:
        details.append(f"{unresolved} unresolved")
    errors = result.get("downsample_errors", 0)
    if errors:
        details.append(f"{errors} downsample failed")
    flush_warnings = result.get("downsample_flush_warnings", 0)
    if flush_warnings:
        details.append(f"{flush_warnings} flush warning")
    if result.get("downsample_cancelled", False):
        details.append("downsample stopped")
    detail = " · ".join(details) or "needs attention"
    colour = C.YELLOW if n_ok else C.RED
    symbol = "⚠" if n_ok else "✗"
    return colour, f"    {symbol} {n_ok} track(s) · {detail} · {int(elapsed)}s"


def _execute_download_queue(queue, args, token, *, on_progress=None,
                            refresh_review=False,
                            consolidate_duplicates=True):
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
    left to retry or recover.
    """
    if not queue:
        return [], True

    section(f"Download queue: {len(queue)} album(s)")

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

    authority = run_lock.current_lease()
    _require_executor_authority(authority)
    (
        origin,
        execution_mode,
        web_album_id,
        external_acknowledge_completion,
    ) = _durable_execution_origin()
    items = list(queue)
    n_items = len(items)
    interrupted = False
    cancelled = False
    disk_full = False
    io_error = False
    auth_lost_exc = None
    queue_transient_lyric_sigs = []
    any_imported = False
    _parked_post_dirs = []
    preflight_done = False
    durable_stopped = False
    recovery_persist_blocked = False
    results = []

    def _persist():
        if on_progress is not None:
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
            on_progress()

    def _ensure_preflight():
        nonlocal any_imported, _parked_post_dirs, preflight_done
        if preflight_done:
            return
        _require_executor_authority(authority)
        staging_preflight(args)
        _require_executor_authority(authority)
        parked_imported, parked_dirs = _reimport_parked_albums()
        _require_executor_authority(authority)
        any_imported = any_imported or parked_imported
        _parked_post_dirs.extend(parked_dirs)
        preflight_done = True

    def _drop(item):
        """An item that's fully handled leaves the queue so a retry won't redo
        it. Persist immediately, so even a hard crash mid-flush can't resurrect
        already-imported albums."""
        try:
            queue.remove(item)
        except ValueError:
            pass
        _persist()

    def _short_circuit(item):
        """Drain an item we can no longer process (cancel / interrupt / auth
        loss / disk full): mark it, run backup resolution so any upgrade backup
        gets restored, and keep it queued for a later retry. Skipping
        resolution would orphan the original tracks under their backup path."""
        # NB: _build_queue_item always seeds these keys with None, so setdefault()
        # is a no-op here (the key is present). It would leave result=None and
        # _resolve_queue_item would then mislabel the stop as "nothing_landed"
        # (and write that to the fetch log) instead of the true reason. Assign
        # explicitly, overriding None while preserving a real result a prior
        # run's flush already recorded.
        if item.get("snapshot_before") is None:
            item["snapshot_before"] = set()
        if not item.get("result"):
            item["result"] = ("cancelled" if cancelled
                              else "interrupted" if interrupted
                              else "disk_full" if disk_full
                              else "io_error" if io_error
                              else "auth_lost")
        results.append(_resolve_queue_item(
            item,
            args,
            False,
            authority=authority,
            record_fetch=False,
        ))

    def _retain_failed_staging(item, *, label=None):
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

    def _resolve_failed_item(item, retention_exc=None, *, cause=None):
        results.append(_resolve_queue_item(
            item, args, False, authority=authority))
        if retention_exc is not None:
            if cause is None:
                raise retention_exc
            raise retention_exc from cause

    def _handle_download_exception(item, exc, *, phase=None):
        nonlocal interrupted, disk_full, io_error, auth_lost_exc
        if isinstance(exc, KeyboardInterrupt):
            log.info(fmt(C.YELLOW,
                "\n    Interrupted. Stopping further downloads; "
                "resolving backups for albums already processed."))
            item["result"] = "interrupted"
            interrupted = True
        elif isinstance(exc, AuthLost):
            log.info(fmt(C.RED,
                "\n    Auth lost. Stopping further downloads; "
                "will restore upgrade backups and exit."))
            item["result"] = "auth_lost"
            auth_lost_exc = exc
        elif isinstance(exc, OSError):
            if exc.errno == errno.ENOSPC:
                location = (
                    f" during {phase}"
                    if phase
                    else f" at {cfg.STAGING_DIR}"
                )
                log.info(fmt(C.RED,
                    f"\n    Out of disk space{location}. Stopping "
                    "the queue; restoring backups and keeping the rest for a "
                    "retry once space is freed."))
                item["result"] = "disk_full"
                disk_full = True
            else:
                log.info(fmt(C.RED,
                    f"\n    Storage error ({exc}). Stopping the queue; "
                    "restoring backups and keeping the rest for a retry."))
                item["result"] = "io_error"
                io_error = True
        else:
            raise exc
        _retained, retention_exc = _retain_failed_staging(item)
        _resolve_failed_item(item, retention_exc, cause=exc)

    def _record_durable_completion(item, post_dir, idx):
        nonlocal any_imported, cancelled
        any_imported = True
        lyric_sigs = item.get("_durable_lyric_sigs")
        if isinstance(lyric_sigs, (list, tuple)):
            queue_transient_lyric_sigs.extend(lyric_sigs)
        result = _durable_completed_result(item, post_dir)
        results.append(result)
        log.info(fmt(*_durable_completion_line(
            result, item.get("elapsed", 0)
        )))
        if token and item.get("n_ok", 0) > 0:
            try:
                warn_if_download_truncated(
                    post_dir,
                    token,
                    item["album"].get("title"),
                )
            except Exception as _e_rc:
                vlog(f"post-download recheck raised: {_e_rc}")
        if idx < n_items:
            cooldown = (
                cfg.RATE_LIMIT_COOLDOWN if item.get("rate_limited") else 0
            )
            if cooldown:
                log.info(fmt(
                    C.YELLOW,
                    f"    ⏳ Qobuz rate-limit detected; cooling down "
                    f"{int(cooldown)}s before the next album "
                    f"(set RATE_LIMIT_COOLDOWN=0 to disable).",
                ))
                if _sleep_unless_cancelled(cooldown, is_cancel_requested):
                    cancelled = True
            else:
                _sleep_unless_cancelled(
                    cfg.DELAY_BETWEEN,
                    is_cancel_requested,
                )

    for idx, item in enumerate(items, 1):
        _require_executor_authority(authority)
        requires_library_backup = queue_item_may_create_library_backup(item)
        plan = plan_durable_new_album(item, args)
        if plan is not None and not _durable_plan_allowed(
            items,
            item,
            execution_mode=execution_mode,
            web_album_id=web_album_id,
        ):
            plan = None

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
            if plan is None:
                return False
            details = _recovered_completion_details(
                item,
                expected_origin=origin,
                execution_mode=execution_mode,
                origin=recovered_origin,
                owner=recovered_owner,
                album_id=album_id,
                planned=planned,
                post_dir=post_dir,
            )
            if details is None:
                return False
            if execution_mode.startswith("web-job:") and (
                len(queue) != 1
                or queue[0] is not item
                or not callable(external_acknowledge_completion)
            ):
                return False
            if recovered_origin.kind is CompletionOriginKind.WEB_JOB:
                if external_acknowledge_completion(
                    recovered_origin,
                    recovered_owner,
                    album_id=album_id,
                    completion_hash=completion_hash,
                    planned=planned,
                    post_dir=post_dir,
                ) is not True:
                    return False
            _require_executor_authority(authority)
            caller_index = next(
                (
                    index
                    for index, queued in enumerate(queue)
                    if queued is item
                ),
                None,
            )
            if caller_index is None:
                return False
            del queue[caller_index]
            recovered_completion.update(
                post_dir=details[0],
                n_ok=details[1],
                n_fail=details[2],
                n_lossy=details[3],
                owner=recovered_owner,
            )
            return True

        startup = recover_startup_state(
            authority=authority,
            acknowledge_completion=_acknowledge_recovered_completion,
        )
        _require_executor_authority(authority)
        if recovered_completion:
            if (
                startup.status is StartupRecoveryStatus.ATTENTION_REQUIRED
                or not _recovered_owner_settled(
                    recovered_completion["owner"]
                )
            ):
                recovery_persist_blocked = True
                durable_stopped = True
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Saved completion recovery remains unsettled; "
                    "stopping this batch.",
                ))
                break
            item.update(
                n_ok=recovered_completion["n_ok"],
                n_fail=recovered_completion["n_fail"],
                n_lossy=recovered_completion["n_lossy"],
                elapsed=0.0,
            )
            _persist()
            _record_durable_completion(
                item,
                recovered_completion["post_dir"],
                idx,
            )
            continue
        try:
            resume_owner, durable_mode, resume_action = _exact_resume_owner(
                startup,
                item,
                execution_mode=execution_mode,
            )
        except DurableAlbumUnavailable:
            if any_imported:
                durable_stopped = True
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Remaining saved queue work needs exact recovery; "
                    "stopping this batch.",
                ))
                break
            raise
        if resume_owner is not None and plan is None:
            # Every walk saves its queue before flushing it, so during an
            # ordinary flush each item resolves an owner for its own pending
            # entry. Treating that as unrecoverable refused the whole flush and
            # left the saved queue failing the same way on every launch. A
            # pending entry is a persisted decision, not recovery state, so give
            # it back and let the legacy lane run the album.
            if _release_unplannable_claim(
                resume_owner,
                resume_action,
                authority=authority,
            ):
                resume_owner = None
            elif any_imported:
                durable_stopped = True
                log.info(fmt(
                    C.YELLOW,
                    "  ⚠  Remaining saved queue work is not yet supported "
                    "by safe recovery; stopping this batch.",
                ))
                break
            else:
                raise DurableAlbumUnavailable(
                    "the saved queue item is not yet supported by safe recovery"
                )
        if resume_owner is None:
            _ensure_preflight()

        # A cancel raised while the PREVIOUS album was importing (i.e. after its
        # own post-download checkpoint at the bottom of the loop) hasn't set
        # `cancelled` yet. Re-check here so the next album short-circuits at the
        # boundary instead of downloading in full before the stale check lands.
        # set the flag first so _short_circuit marks the item 'cancelled', not
        # the auth_lost fallback.
        if not cancelled and is_cancel_requested():
            cancelled = True
        if resume_owner is not None and cancelled:
            raise KeyboardInterrupt
        if (interrupted or cancelled or disk_full or io_error
                or auth_lost_exc is not None):
            _short_circuit(item)
            continue
        # A retried item may still carry a stop marker ('disk_full',
        # 'interrupted', ...) from an earlier flush; clear it so a
        # now-successful re-download isn't read as still-stopped by
        # _queue_item_needs_retry and left in the queue forever.
        item["result"] = None
        item["quality_verdict"] = None
        item["upgrade_unverified"] = False
        item["catalogue_unverified"] = False
        item["recovery_unverified"] = False
        item["resampled_n"] = 0
        item.update(
            downsample_errors=0,
            downsample_saved_bytes=0,
            downsample_flush_warnings=0,
            downsample_cancelled=False,
        )
        item["post_import_signatures"] = []
        retention_exc = None

        album = item["album"]
        album_dir = item["album_dir"]
        title = album.get("title") or "?"

        print()
        log.info(fmt(C.BOLD + C.WHITE,
            f"  [Q {idx}/{n_items}] {truncate(title, 55)}"))
        if requires_library_backup and plan is None:
            # The exact-recovery lane only covers a handful of shapes (no
            # sibling replacement, no downsampling, whole-album requests). The
            # rest still run, on the path that backs up before it replaces.
            log.info(fmt(C.GRAY,
                "    Crash-safe recovery doesn't cover this album; using the "
                "standard path, which backs up anything it replaces."))

        if plan is not None:
            def _prepare_staged(album_dirs, checkpoint_sources):
                return _run_pre_import_hooks_for_dirs(
                    album_dirs,
                    args,
                    on_sources_changed=checkpoint_sources,
                )

            outcome = execute_durable_new_album(
                queue,
                item,
                args,
                plan=plan,
                origin=origin,
                mode=durable_mode,
                authority=authority,
                resume_owner=resume_owner,
                prepare_staged=_prepare_staged,
                acknowledge_completion=external_acknowledge_completion,
            )
            removed = all(queued is not item for queued in queue)
            if removed:
                _persist()
            if outcome.status is DurableAlbumStatus.CANCELLED:
                item["result"] = "cancelled"
                cancelled = True
                log.info(fmt(C.YELLOW,
                    "    Cancelled; discarded this album's partial "
                    "download."))
                results.append(_resolve_queue_item(
                    item, args, False, authority=authority))
                continue
            if outcome.status is not DurableAlbumStatus.COMPLETE:
                results.append({
                    "dir": outcome.post_dir,
                    "result": outcome.status.value,
                    "n_ok": item.get("n_ok", 0),
                    "n_fail": item.get("n_fail", 0),
                    "n_lossy": item.get("n_lossy", 0),
                    "siblings_preserved": [],
                    "imported": False,
                    "auto_upgrade": bool(item.get("auto_upgrade")),
                })
                durable_stopped = True
                log.info(fmt(
                    C.YELLOW,
                    "    ⚠  This album needs a safe retry or recovery; "
                    "stopping the queue.",
                ))
                break
            if not removed or outcome.post_dir is None:
                raise DurableAlbumUnavailable(
                    "the completed durable album was not finalised exactly"
                )
            _record_durable_completion(item, outcome.post_dir, idx)
            continue

        _seal_queue_item_siblings(item)
        if item["auto_upgrade"] and album_dir and album_dir.exists():
            bp = backup_album_dir(album_dir)
            if bp is not None and not bp.complete:
                from qobuz_librarian.modes.process import (
                    _recover_incomplete_upgrade_backup,
                )

                _recover_incomplete_upgrade_backup(
                    bp, album_dir, operation="queued upgrade backup")
                log.info(fmt(C.RED,
                    "    ✗  The backup was interrupted; skipping this "
                    "album."))
                item["result"] = "upgrade_aborted_backup_failed"
                results.append(_resolve_queue_item(
                    item, args, False, authority=authority))
                continue
            if bp is None:
                log.info(fmt(C.RED,
                    "    ✗  Could not back up; skipping this album."))
                item["result"] = "upgrade_aborted_backup_failed"
                results.append(_resolve_queue_item(
                    item, args, False, authority=authority))
                continue   # Nothing downloaded, so it stays queued for retry.
            item["backup_path"] = bp
        # Snapshot staging AFTER the backup: a custom config can point
        # UPGRADE_BACKUP_DIR inside STAGING_DIR, and a backup copied in BEFORE
        # the snapshot is already present at snapshot time, so it's correctly
        # excluded from the freshly-downloaded diff (not re-imported/downsampled).
        try:
            item["snapshot_before"] = snapshot_staging()
            _download_for_queue_item(item)
        except (KeyboardInterrupt, AuthLost, OSError) as exc:
            _handle_download_exception(item, exc)
            continue

        if is_cancel_requested():
            item["result"] = "cancelled"
            cancelled = True
            _retained, retention_exc = _retain_failed_staging(item)
            if retention_exc is None:
                log.info(fmt(C.YELLOW,
                    "    Cancelled; retained this album's partial download "
                    "for review."))
            _resolve_failed_item(item, retention_exc)
            continue

        summary_n_ok = item.get("n_ok", 0)
        summary_n_fail = item.get("n_fail", 0)
        summary_elapsed = int(item.get("elapsed", 0))
        if summary_n_ok and not summary_n_fail:
            log.info(fmt(C.GREEN, f"    ✓ {summary_n_ok} track(s) · {summary_elapsed}s"))
        elif summary_n_ok:
            log.info(fmt(C.YELLOW,
                f"    ⚠ {summary_n_ok} ok · {summary_n_fail} failed · {summary_elapsed}s"))
        else:
            log.info(fmt(C.RED, f"    ✗ download failed · {summary_elapsed}s"))

        # ── Per-album pre-import + beets import ──────────────────────────
        item_imported = False
        if summary_n_ok > 0 and not args.no_import:
            try:
                album_dirs = _staged_album_dirs(item)
            except OSError as exc:
                _handle_download_exception(item, exc)
                continue
            if not album_dirs:
                log.info(fmt(C.YELLOW,
                    "    ⚠  No staged audio dir found for this album; skipping beets."))
            else:
                effective_tier = item.get("quality") or cfg.STREAMRIP_QUALITY

                def _redownload_at_max():
                    saved_q = item.get("quality")
                    retry_item = dict(item)
                    retry_item["quality"] = 4

                    def _run_retry():
                        _download_for_queue_item(retry_item)

                    try:
                        fresh_dirs, retry_kept = redownload_with_staged_fallback(
                            album_dirs,
                            run_retry=_run_retry,
                            collect_staged_dirs=lambda: (
                                _staged_album_dirs(retry_item)
                                if retry_item.get("n_ok", 0) > 0 else []),
                            collect_retry_files=lambda: download_staged_files(
                                retry_item),
                            retry_preserves_original=lambda: (
                                retry_preserves_track_coverage(
                                    item, retry_item)))
                    except Exception:
                        item["quality"] = saved_q
                        raise
                    if retry_kept:
                        retire_empty_download_staging(item)
                        for key in ("n_ok", "n_fail", "n_lossy",
                                    "failed_tracks", "lossy_tracks",
                                    "broken_tracks", "elapsed",
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
                                item[key] = retry_item[key]
                            else:
                                item.pop(key, None)
                    else:
                        retire_empty_download_staging(retry_item)
                    item["quality"] = saved_q
                    return fresh_dirs

                try:
                    item["quality_verdict"] = verify_and_recover(
                        album, album_dirs,
                        redownload_at_max=_redownload_at_max,
                        effective_tier=effective_tier,
                        allow_retry=item.get("gap_fill_backup_path") is None)
                except (KeyboardInterrupt, AuthLost, OSError) as exc:
                    _handle_download_exception(item, exc)
                    continue
                if item["quality_verdict"]["retried"]:
                    album_dirs = item["quality_verdict"]["staged_dirs"]
                if item["quality_verdict"]["recovered"]:
                    log.info(fmt(C.CYAN,
                        "    ↻ recovered: re-fetched at the highest source."))
                elif not item["quality_verdict"]["under"]:
                    log.info(fmt(C.GRAY,
                        "    Quality check: staged rip meets the selected source cap."))
                elif on_quality_shortfall is not None:
                    # Still under target after the retry: the import proceeds,
                    # but the owning job must not look like a clean finish.
                    on_quality_shortfall(item["quality_verdict"])

                # Repair carries a callback that re-tags the staged refills
                # with the originals' own metadata before beets files them, so
                # a recording that also lives on a compilation comes back tagged
                # for the album the user owns. No-op for every other queue item.
                retag = item.get("pre_import_retag")
                if callable(retag):
                    try:
                        retag(album_dirs)
                    except Exception as _e_rt:
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  repair retag step failed: {_e_rt}"))
                try:
                    prepared = _run_pre_import_hooks_for_dirs(album_dirs, args)
                    sigs, resampled_n = prepared
                    item["resampled_n"] = resampled_n
                    item.update(_pre_import_outcome_fields(prepared))
                    item["post_import_signatures"] = (
                        track_signatures_for_album_dirs(album_dirs))
                    queue_transient_lyric_sigs.extend(sigs)
                except KeyboardInterrupt:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Pre-import hook interrupted; skipping beets "
                        "for this album; stopping the queue."))
                    interrupted = True
                    # Mark interrupted so _resolve_queue_item labels it as such
                    # in the fetch log instead of falling through to "downloaded"
                    # (the album did not import).
                    item["result"] = "interrupted"
                    results.append(_resolve_queue_item(
                        item, args, False, authority=authority))
                    continue

                parking_receipts = tuple(
                    capture_tree(directory) for directory in album_dirs)
                if any(receipt is None for receipt in parking_receipts):
                    parking_receipts = ()
                try:
                    ownership_capture = (
                        {} if item.get("_capture_import_ownership") is True
                        else None
                    )
                    if ownership_capture is None:
                        item_imported = _import_album_with_retry(album_dirs)
                    else:
                        item_imported = _import_album_with_retry(
                            album_dirs,
                            ownership_out=ownership_capture,
                        )
                    if item_imported and ownership_capture is not None:
                        ownership_result = ownership_capture.get("result")
                        if isinstance(ownership_result, dict):
                            item["_import_ownership"] = ownership_result
                except KeyboardInterrupt:
                    log.info(fmt(C.YELLOW,
                        "\n  beets interrupted. Resolving backups based on disk."))
                    interrupted = True
                    item_imported = False
                    # Label as interrupted (not "downloaded"). Beets did not
                    # finish importing this album.
                    item["result"] = "interrupted"
                    results.append(_resolve_queue_item(
                        item, args, False, authority=authority))
                    continue
                except OSError as exc:
                    # The original may already be in the upgrade backup.
                    # Resolve it before stopping the queue.
                    _handle_download_exception(
                        item, exc, phase="library import"
                    )
                    continue

                retired_import_staging = True
                if item_imported and "_staging_run" in item:
                    try:
                        retired_import_staging = (
                            retire_download_staging_after_import(item)
                        )
                    except BaseException as cleanup_exc:
                        retired_import_staging = False
                        retention_exc = cleanup_exc
                        log.info(fmt(
                            C.RED,
                            "  ✗  Could not retire the exact imported staging "
                            "run; resolving the album backup before returning "
                            "the storage error.",
                        ))
                if item_imported and retired_import_staging:
                    any_imported = True
                else:
                    if item_imported:
                        log.info(fmt(
                            C.RED,
                            "  ✗  The exact download run was not empty "
                            "after beets; the import was not accepted.",
                        ))
                    item_imported = False
                    item["result"] = "import_failed"
                    if retention_exc is None:
                        retained, retention_exc = _retain_failed_staging(
                            item, label="beets-incomplete")
                        if retained:
                            log.info(fmt(
                                C.YELLOW,
                                "  ⚠  Kept the exact residual download run in "
                                "private staging recovery; the queue item "
                                "remains pending.",
                            ))
                        elif retention_exc is None:
                            try:
                                _move_to_beets_retry(
                                    album_dirs,
                                    title,
                                    expected=parking_receipts,
                                )
                            except BaseException as parking_exc:
                                retention_exc = parking_exc
                                log.info(fmt(
                                    C.RED,
                                    "  ✗  Could not park the failed beets "
                                    "import; resolving the album backup before "
                                    "returning the storage error.",
                                ))
        elif args.no_import:
            log.info(fmt(C.YELLOW,
                f"  --no-import: skipping beets. Files in {cfg.STAGING_DIR}/"))
        elif summary_n_ok == 0:
            log.info(fmt(C.GRAY, "  Skipping beets: nothing landed."))

        # Drop a finished item from the persisted queue BEFORE resolving its
        # backup. If the process dies between the two, the worst case is an
        # orphaned backup the retention sweep clears, not a completed album
        # left queued and needlessly re-downloaded on the next resume.
        if not _queue_item_needs_retry(item):
            _drop(item)
        results.append(_resolve_queue_item(
            item, args, item_imported, authority=authority))
        if retention_exc is not None:
            raise retention_exc

        # Same post-import length recheck process_album runs, here covering every
        # queue path (walk fill, artist/album queue, repair refill, resume,
        # web single-track): a frame-boundary truncation decodes fine and only
        # shows against the real Qobuz length. This is advisory and never alters the
        # album, logs its own auth/outage case, and is wrapped so it can't take
        # down a batch whose album already imported cleanly.
        if (item_imported and token and not args.no_import
                and item.get("n_ok", 0) > 0):
            post_dir = item.get("_resolved_post_dir")
            if post_dir is not None:
                try:
                    warn_if_download_truncated(
                        post_dir, token, item["album"].get("title"))
                except Exception as _e_rc:
                    vlog(f"post-download recheck raised: {_e_rc}")

        # Qobuz throttles sustained bulk queues. When the last rip showed
        # throttle signals, pause longer than the normal inter-album gap
        # so we stop pounding the limit. No pause after the final album.
        if idx < n_items:
            cooldown = cfg.RATE_LIMIT_COOLDOWN if item.get("rate_limited") else 0
            if cooldown:
                log.info(fmt(C.YELLOW,
                    f"    ⏳ Qobuz rate-limit detected; cooling down "
                    f"{int(cooldown)}s before the next album "
                    f"(set RATE_LIMIT_COOLDOWN=0 to disable)."))
                if _sleep_unless_cancelled(cooldown, is_cancel_requested):
                    cancelled = True  # next album short-circuits at the loop top
            else:
                _sleep_unless_cancelled(cfg.DELAY_BETWEEN, is_cancel_requested)

    if not preflight_done and not durable_stopped:
        _require_executor_authority(authority)
        final_startup = recover_startup_state(
            authority=authority,
            acknowledge_completion=None,
        )
        _require_executor_authority(authority)
        if final_startup.status is StartupRecoveryStatus.CLEAR:
            _ensure_preflight()
        else:
            durable_stopped = True
            log.info(fmt(
                C.YELLOW,
                "  ⚠  Saved queue recovery remains unfinished; "
                "skipping unrelated staging work.",
            ))

    # ── Post-batch: consolidate duplicate albums once if anything landed.
    # The fold is a library-wide pass, so running it per-album would waste
    # work; once at end is enough.
    ownership_scope_resolved = any(
        item.get("_resolved_post_dir_from_import_ownership") is True
        for item in items
    )
    if (consolidate_duplicates
            and any_imported
            and not ownership_scope_resolved):
        try:
            protected_ownership_dirs = [
                item.get("_resolved_post_dir")
                for item in items
                if isinstance(item.get("_import_ownership"), dict)
                and item.get("_resolved_post_dir") is not None
            ]
            if protected_ownership_dirs:
                _consolidate_duplicate_albums(
                    protected_paths=protected_ownership_dirs)
            else:
                _consolidate_duplicate_albums()
        except Exception as _e_c:
            vlog(f"consolidate duplicate albums raised: {_e_c}")
    elif consolidate_duplicates and ownership_scope_resolved:
        vlog(
            "skipping duplicate consolidation for descriptor-verified "
            "one-track ownership scope"
        )

    # ── Post-batch lyric-retry resolution.
    print()
    _post_dirs = [it.get("_resolved_post_dir") for it in items
                  if it.get("_resolved_post_dir") is not None]
    if (queue_transient_lyric_sigs
            and any_imported
            and auth_lost_exc is None
            and not interrupted):
        try:
            # Search the imported folders first, then the staging tree: in a
            # mixed batch, an album that imported cleanly is matched in its
            # post_dir, while one whose import failed is parked under
            # STAGING_DIR/.beets_retry/. Without the staging fallback here its
            # transient-lyric sigs would be silently dropped and never retried.
            resolved = _resolve_signatures_to_paths(
                queue_transient_lyric_sigs, _post_dirs + [cfg.STAGING_DIR])
            if resolved:
                if _record_post_import_lyric_retry(resolved):
                    vlog(f"lyric retry: queued {len(resolved)} "
                         f"post-import path(s) for next-launch retry")
        except Exception as _e_lr:
            vlog(f"lyric retry resolution failed: {_e_lr}")
    elif (queue_transient_lyric_sigs
            and auth_lost_exc is None
            and not interrupted):
        # Some albums downloaded but none imported, with files still in staging
        # (or parked under .beets_retry/). Record those paths so the
        # next-launch retry has a shot. Stale entries self-prune there.
        try:
            resolved = _resolve_signatures_to_paths(
                queue_transient_lyric_sigs, [cfg.STAGING_DIR])
            if resolved:
                if _record_post_import_lyric_retry(resolved):
                    vlog(f"lyric retry: no album imported; queued "
                         f"{len(resolved)} staging path(s) for next-launch retry")
        except Exception as _e_lr:
            vlog(f"lyric retry (staging fallback) failed: {_e_lr}")

    # Materialise .lrc sidecars (and strip the embedded tag in sidecar mode) for
    # every imported album, not only when a transient lyric outage happened.
    # The lyric hook always embeds and relies on this post-import pass to emit
    # the sidecars; gating it on transient sigs broke LYRICS_FORMAT=sidecar/both
    # on the normal happy path. No-op when LYRICS_FORMAT is embed.
    ownership_tracks = [
        it["_import_ownership"]
        for it in items
        if isinstance(it.get("_import_ownership"), dict)
    ]
    if any_imported and (
            _post_dirs or _parked_post_dirs or ownership_tracks):
        capture_ownership = bool(ownership_tracks)
        ownership_changes = [] if capture_ownership else None
        ownership_created_files = [] if capture_ownership else None
        ownership_directory_changes = (
            [
                change
                for item in items
                for change in item.get(
                    "_import_ownership_directory_changes", [])
            ]
            if capture_ownership else None
        )
        try:
            if ownership_changes is None:
                write_post_import_sidecars(_post_dirs + _parked_post_dirs)
            else:
                write_post_import_sidecars(
                    _post_dirs + _parked_post_dirs,
                    identity_changes_out=ownership_changes,
                    created_files_out=ownership_created_files,
                    directory_changes_out=ownership_directory_changes,
                    owned_tracks=ownership_tracks,
                )
        except Exception as _e_sc:
            vlog(f"post-import sidecar write raised: {_e_sc}")
        finally:
            if capture_ownership:
                _advance_import_ownership_identities(
                    items,
                    ownership_changes,
                    ownership_created_files,
                    ownership_directory_changes,
                )
                for item in items:
                    item.pop("_import_ownership_directory_changes", None)

    log.info(fmt(*_queue_done_line(results, n_items)))

    # A durable stop leaves the journal holding blocked or retired work on
    # purpose, and rewriting it as pending is what the journal refuses. Every
    # album that finished already persisted through _drop, so the saved state is
    # current without this, and the refusal surfaces as a traceback in the walk
    # at the exact moment an album has been correctly parked.
    if not recovery_persist_blocked and not durable_stopped:
        _persist()
    if auth_lost_exc is not None:
        raise auth_lost_exc
    # Re-raising KeyboardInterrupt is what stops _flush_queue in modes 4/5
    # from reaching its clear-on-drain branch and discarding the items that
    # were short-circuited (and so still need a retry).
    if interrupted:
        raise KeyboardInterrupt
    # Only the CLI batch callers (walk / artist / album / queue resume) refresh
    # the saved review state. They change library files and nothing else keeps
    # that state fresh. The web single-track and CLI repair callers must NOT:
    # the web path already refreshes right after, under the same staging lock,
    # and repair calls this per album in a loop (one artist re-scan per album).
    if refresh_review:
        _refresh_review_state_after_downloads(results, token, args)
    return results, not queue and not durable_stopped
