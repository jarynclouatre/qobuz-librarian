"""Queue item construction."""
from qobuz_librarian.library import candidate_premise

_UNSET = object()


def _capture_source_premise(album_dir, *, upgrade_only, auto_upgrade,
                            force_track_by_track):
    if album_dir is None:
        return None
    if upgrade_only or auto_upgrade:
        kind = "upgrade"
    elif force_track_by_track:
        kind = "repair"
    else:
        kind = "gap-fill"
    return candidate_premise.capture(kind, album_dir)


def _build_queue_item(*, album, album_dir, label, missing, present,
                      upgrade_only, auto_upgrade,
                      siblings_to_delete=None, quality=None,
                      force_track_by_track=False, source_premise=_UNSET):
    """Bundle a confirmed download decision for batch processing.
    siblings_to_delete: list of sibling album dirs to remove after this item
    lands successfully.
    """
    if source_premise is _UNSET:
        source_premise = _capture_source_premise(
            album_dir,
            upgrade_only=upgrade_only,
            auto_upgrade=auto_upgrade,
            force_track_by_track=force_track_by_track,
        )
    return {
        "album": album,
        "album_dir": album_dir,
        "label": label,
        "missing": missing,
        "present": present,
        "upgrade_only": upgrade_only,
        "auto_upgrade": auto_upgrade,
        "backup_path": None,
        "snapshot_before": None,
        "n_ok": 0,
        "n_fail": 0,
        "n_lossy": 0,
        "failed_tracks": [],
        "lossy_tracks": [],
        "broken_tracks": [],
        "elapsed": 0.0,
        "imported": False,
        "result": None,
        "siblings_to_delete": list(siblings_to_delete or []),
        "quality": quality,
        "force_track_by_track": bool(force_track_by_track),
        "_source_premise": source_premise,
        "_gap_fill_receipts": candidate_premise.gap_fill_receipts(source_premise),
    }
