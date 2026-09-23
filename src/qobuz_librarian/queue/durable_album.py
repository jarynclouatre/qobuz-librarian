"""Frozen completion inputs for the crash-safe new-album queue lane."""

from __future__ import annotations

from dataclasses import dataclass

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    CompletionExpectation,
    CompletionInput,
    CompletionOrigin,
    CompletionScope,
    DownloadCoverage,
    QualityTarget,
    RecoveryOwner,
    SourceLineage,
    SourceTransition,
    SourceTransitionKind,
    StagedBinding,
    StagedReceipt,
    completion_input_ready,
    normalise_album_id,
)
from qobuz_librarian.download import album_track_slots, downloads_whole_album
from qobuz_librarian.library.catalog import is_lossless_album
from qobuz_librarian.quality.decision import album_max_quality


@dataclass(frozen=True, slots=True)
class DurableNewAlbumPlan:
    """An exact full-album request supported by the status-first runner."""

    expectation: CompletionExpectation
    effective_tier: int
    library_backup_kind: str | None = None


def _full_album_gap_fill(item, tracks) -> bool:
    missing = item.get("missing")
    present = item.get("present")
    return bool(
        item.get("album_dir") is not None
        and not bool(item.get("auto_upgrade"))
        and not bool(item.get("upgrade_only"))
        and not bool(item.get("force_track_by_track"))
        and isinstance(missing, list)
        and isinstance(present, (list, tuple))
        and missing
        and present
        and downloads_whole_album(
            len(present), len(missing), len(tracks))
    )


def _full_album_download_may_backup_present(item, tracks) -> bool:
    """True when the download strategy may move already-present tracks."""
    missing = item.get("missing")
    present = item.get("present")
    if (
        item.get("album_dir") is None
        or bool(item.get("force_track_by_track"))
        or not isinstance(missing, (list, tuple))
        or not isinstance(present, (list, tuple))
        or not present
    ):
        return False
    if bool(item.get("upgrade_only")):
        return len(missing) == len(tracks)
    return downloads_whole_album(
        len(present), len(missing), len(tracks))


def queue_item_may_create_library_backup(item) -> bool:
    """True when legacy execution could move library sources before a rip."""
    if type(item) is not dict:
        return False
    if bool(item.get("siblings_to_delete")):
        return True
    if item.get("album_dir") is None:
        return False
    album = item.get("album")
    track_group = album.get("tracks") if type(album) is dict else None
    tracks = track_group.get("items") if type(track_group) is dict else None
    if not isinstance(tracks, list):
        return bool(item.get("auto_upgrade")) or bool(
            item.get("present")
            and not bool(item.get("force_track_by_track"))
        )
    return bool(item.get("auto_upgrade")) or (
        _full_album_download_may_backup_present(item, tracks)
    )


def plan_durable_new_album(item, args) -> DurableNewAlbumPlan | None:
    """Freeze one full-album lane the live completion proof can authorise."""
    if (
        type(item) is not dict
        or getattr(args, "no_import", False)
        or getattr(args, "consolidate", False)
        or (
            cfg.DOWNSAMPLE_HIRES_ENABLED
            and not getattr(args, "no_downsample", False)
        )
    ):
        return None
    album = item.get("album")
    if type(album) is not dict or not is_lossless_album(album):
        return None
    tracks = (album.get("tracks") or {}).get("items")
    slots = album_track_slots(album)
    if (
        not isinstance(tracks, list)
        or not slots
        or bool(item.get("force_track_by_track"))
        or bool(item.get("siblings_to_delete"))
        or callable(item.get("pre_import_retag"))
    ):
        return None
    new_album = (
        item.get("album_dir") is None
        and item.get("missing") == tracks
        and item.get("present") in ([], ())
        and not bool(item.get("upgrade_only"))
        and not bool(item.get("auto_upgrade"))
    )
    whole_upgrade = (
        item.get("album_dir") is not None
        and item.get("missing") == tracks
        and item.get("present") in ([], ())
        and bool(item.get("auto_upgrade"))
    )
    full_gap_fill = _full_album_gap_fill(item, tracks)
    if sum((new_album, whole_upgrade, full_gap_fill)) != 1:
        return None
    album_id = normalise_album_id(album.get("id"))
    effective_tier = item.get("quality") or cfg.STREAMRIP_QUALITY
    if album_id is None or type(effective_tier) is not int:
        return None
    if effective_tier not in (2, 3, 4):
        return None
    bits, rate = album_max_quality(album, effective_tier)
    if type(bits) is not int or type(rate) is not int or bits <= 0 or rate <= 0:
        return None
    # Late: loading the Beets integration reads the Beets config, which only
    # an album that qualifies so far needs.
    from qobuz_librarian.integrations import beets

    if not beets.path_templates_give_albums_own_folders():
        return None
    expectation = CompletionExpectation(
        album_id=album_id,
        scope=CompletionScope.ALBUM,
        catalogue_slots=slots,
        requested_slots=slots,
        baseline_slots=(),
        quality_targets=tuple(
            QualityTarget(slot, bits, rate) for slot in slots
        ),
    )
    backup_kind = (
        "upgrade" if whole_upgrade
        else "gap-fill" if full_gap_fill
        else None
    )
    return DurableNewAlbumPlan(expectation, effective_tier, backup_kind)


def initial_completion_input(
    plan: DurableNewAlbumPlan,
    owner: RecoveryOwner,
    origin: CompletionOrigin,
) -> CompletionInput:
    """Build the immutable pre-mutation input saved with ACTIVE state."""
    if type(plan) is not DurableNewAlbumPlan:
        raise ValueError("a durable full-album plan is required")
    value = CompletionInput(
        owner=owner,
        origin=origin,
        expectation=plan.expectation,
        effective_tier=plan.effective_tier,
    )
    value.to_record()
    return value


def completion_input_from_download(
    initial: CompletionInput,
    coverage: DownloadCoverage,
) -> CompletionInput | None:
    """Append exact source receipts only when the full frozen request landed."""
    if type(initial) is not CompletionInput or type(coverage) is not DownloadCoverage:
        return None
    expectation = initial.expectation
    if (
        initial.lineages
        or initial.counts is not None
        or coverage.album_id != expectation.album_id
        or coverage.catalogue_slots != expectation.catalogue_slots
        or coverage.requested_slots != expectation.requested_slots
    ):
        return None
    by_slot = {}
    for binding in coverage.bindings:
        if binding.slot in by_slot:
            return None
        by_slot[binding.slot] = binding
    if set(by_slot) != set(expectation.requested_slots):
        return None
    value = CompletionInput(
        owner=initial.owner,
        origin=initial.origin,
        expectation=expectation,
        effective_tier=initial.effective_tier,
        lineages=tuple(
            SourceLineage(
                slot,
                StagedReceipt(
                    by_slot[slot].path,
                    by_slot[slot].identity,
                ),
            )
            for slot in expectation.requested_slots
        ),
        counts=coverage.counts,
    )
    return value if completion_input_ready(value) else None


def advance_completion_sources(
    previous: CompletionInput,
    bindings,
    kind: SourceTransitionKind,
) -> CompletionInput | None:
    """Append one exact, same-path staged rewrite to frozen source lineage."""
    if (
        type(previous) is not CompletionInput
        or not completion_input_ready(previous)
        or type(kind) is not SourceTransitionKind
    ):
        return None
    try:
        values = tuple(bindings)
    except TypeError:
        return None
    by_slot = {}
    for binding in values:
        try:
            slot = binding.slot
            path = binding.path
            identity = binding.identity
        except AttributeError:
            return None
        if slot in by_slot:
            return None
        by_slot[slot] = StagedReceipt(path, identity)
    if set(by_slot) != {lineage.slot for lineage in previous.lineages}:
        return None

    lineages = []
    for lineage in previous.lineages:
        after = by_slot[lineage.slot]
        before = lineage.current
        if after.path != before.path:
            return None
        transitions = lineage.transitions
        if after != before:
            transitions = (*transitions, SourceTransition(kind, before, after))
        lineages.append(SourceLineage(
            lineage.slot,
            lineage.origin,
            transitions,
        ))
    value = CompletionInput(
        owner=previous.owner,
        origin=previous.origin,
        expectation=previous.expectation,
        effective_tier=previous.effective_tier,
        lineages=tuple(lineages),
        counts=previous.counts,
    )
    return value if completion_input_ready(value) else None


def managed_completion_input(previous: CompletionInput, managed) -> CompletionInput | None:
    """Bind Beets' sealed tag-clean source receipts to the persisted input."""
    try:
        if managed.owner != previous.owner:
            return None
        mappings = tuple(managed.mappings)
    except AttributeError:
        return None
    bindings = tuple(StagedBinding(
        mapping.slot,
        mapping.source_path,
        mapping.source_identity,
    ) for mapping in mappings)
    return advance_completion_sources(
        previous,
        bindings,
        SourceTransitionKind.BEETS_TAG_CLEAN,
    )


def managed_binding_records(value: CompletionInput) -> tuple[dict, ...] | None:
    """Copy the current exact staged sources into the managed-Beets schema."""
    if not completion_input_ready(value):
        return None
    return tuple({
        "slot": lineage.slot,
        "path": lineage.current.path,
        "identity": list(lineage.current.identity),
    } for lineage in value.lineages)
