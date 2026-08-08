"""Download phase for one album: pick a strategy, rip, drop lossy/broken
files, retry the strays once, and reconcile the counts.

Shared by the single-album path (`modes/process.py`) and the queue executor
(`queue/executor.py`). Both hand it a staging snapshot and the missing/present
split and read back the `n_ok`/`n_fail`/`n_lossy` bookkeeping. Results are
written into a caller-owned `result` dict as the work progresses - not just
returned - so the gap-fill backup taken mid-download can still be resolved by
the caller's finally/except when a rip raises AuthLost or hits a full disk.
"""

import re
import shutil
import time
from collections import Counter
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    detect_auth_lost,
    detect_disk_full,
    detect_rate_limited,
)
from qobuz_librarian.completion import (
    DownloadCounts,
    DownloadCoverage,
    StagedBinding,
    authoritative_slots,
    normalise_album_id,
)
from qobuz_librarian.integrations.rip import (
    cleanup_lossy,
    files_added_since,
    is_cancel_requested,
    rip_url,
    snapshot_staging,
)
from qobuz_librarian.integrations.staging import (
    capture_file,
    capture_staging_run,
    create_staging_run,
    discard_group,
    discard_quarantined_file,
    retain_staging_run,
    staging_run_from_record,
)
from qobuz_librarian.library.backup import (
    backup_gap_fill_files,
    library_backup_record,
)
from qobuz_librarian.library.catalog import find_extras_in_existing
from qobuz_librarian.library.scanner import read_album_dir, read_audio_meta
from qobuz_librarian.library.tags import normalize, strip_edition_suffix
from qobuz_librarian.recovery import normalise_recovery_owner
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.logging import log, report_progress, vlog


def match_key_from_stem(p):
    """Normalized title key from a filename stem (or bare stem string) used to
    line a downloaded/deleted file up against its Qobuz track.

    Accepts a Path or a bare stem string. A Path's ``.stem`` would mis-split a
    title like "01. ★" (pathlib reads ". ★" as a suffix), so an already-extracted
    string is taken verbatim. Strips a leading
    "<disc>-<track>"/"<track>" number and any "Artist - " prefix streamrip
    writes, then runs the result through the same normalize/strip_edition_suffix
    a Qobuz title goes through, so the two sides compare on equal terms."""
    s = p.stem if hasattr(p, "stem") else str(p)
    m = re.match(r"^(?:\d+[-.])?\d+[\s\-–.]+(.+)$", s)
    t = m.group(1) if m else s
    m = re.match(r"^.+?\s+-\s+(.+)$", t)
    return normalize(strip_edition_suffix(m.group(1) if m else t))


def _bare_title(title):
    return normalize(strip_edition_suffix(title or ""))


_DISC_DIR_RE = re.compile(r"^(?:disc|cd)\s*0*(\d+)\b", re.IGNORECASE)
_NUMBERED_STEM_RE = re.compile(r"^(?:(\d+)[-.])?(\d+)(?:\s*[-–.]\s*|\s+)(.+)$")


def _positive_int(value):
    if isinstance(value, (bool, float)):
        return None
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _clean_isrc(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _file_track_identity(path, context_tracks):
    """Best stable identity available before a rejected file is deleted."""
    path = Path(path)
    stem_match = _NUMBERED_STEM_RE.match(path.stem)
    stem_disc = _positive_int(stem_match.group(1)) if stem_match else None
    stem_track = _positive_int(stem_match.group(2)) if stem_match else None
    parent_match = _DISC_DIR_RE.match(path.parent.name)
    parent_disc = _positive_int(parent_match.group(1)) if parent_match else None

    try:
        meta = read_audio_meta(path) if path.exists() else None
    except OSError as exc:
        vlog(f"retry identity: couldn't read {path}: {exc}")
        meta = None
    meta = meta or {}

    meta_track = _positive_int(meta.get("tracknumber"))
    track_conflict = bool(meta_track and stem_track and meta_track != stem_track)
    if track_conflict:
        track = None
    else:
        track = meta_track or stem_track

    context_discs = {_positive_int(track.get("media_number")) or 1 for track in context_tracks}
    meta_disc = _positive_int(meta.get("discnumber"))
    # read_audio_meta defaults a missing DISCNUMBER to 1.
    if meta_disc == 1 and len(context_discs) > 1:
        meta_disc = None
    explicit_disc = parent_disc or stem_disc
    if parent_disc and stem_disc and parent_disc != stem_disc:
        explicit_disc = None
        disc_conflict = True
    else:
        disc_conflict = False
    if explicit_disc and meta_disc and explicit_disc != meta_disc:
        disc = None
        disc_conflict = True
    else:
        disc = explicit_disc or meta_disc
    if disc is None and not disc_conflict and len(context_discs) == 1:
        disc = next(iter(context_discs))

    title = _bare_title(meta.get("title"))
    if not title:
        title = match_key_from_stem(path)
    return {
        "isrc": _clean_isrc(meta.get("isrc")),
        "position": (disc, track) if disc and track else None,
        "track": track,
        "title": title,
        "conflicted": track_conflict or disc_conflict,
    }


def _track_identity(track):
    disc = _positive_int(track.get("media_number")) or 1
    number = _positive_int(track.get("track_number"))
    return {
        "isrc": _clean_isrc(track.get("isrc")),
        "position": (disc, number) if number else None,
        "track": number,
        "title": _bare_title(track.get("title")),
    }


def _album_track_keys(tracks):
    """Stable unique keys for the exact Qobuz slots in one album response."""
    tracks = list(tracks)
    qobuz_ids = Counter(normalise_album_id(track.get("id")) for track in tracks)
    positions = Counter(
        (_positive_int(track.get("media_number")) or 1, _positive_int(track.get("track_number")))
        for track in tracks
        if _positive_int(track.get("track_number")) is not None
    )
    keys = []
    for index, track in enumerate(tracks):
        qobuz_id = normalise_album_id(track.get("id"))
        if qobuz_id and qobuz_ids[qobuz_id] == 1:
            keys.append(f"qobuz:{qobuz_id}")
            continue
        position = (
            _positive_int(track.get("media_number")) or 1,
            _positive_int(track.get("track_number")),
        )
        if position[1] is not None and positions[position] == 1:
            keys.append(f"position:{position[0]}:{position[1]}")
            continue
        # The album response order is the last exact distinction available for
        # malformed catalogue rows.
        keys.append(f"album-slot:{index + 1}")
    return keys


def album_track_slots(album):
    """Return the canonical slots for one complete Qobuz album response.

    An empty tuple means the response is not usable as authoritative album
    inventory.  Queue recovery uses this before any filesystem mutation.
    """
    if type(album) is not dict:
        return ()
    tracks = (album.get("tracks") or {}).get("items")
    if not isinstance(tracks, list) or not tracks:
        return ()
    try:
        slots = tuple(_album_track_keys(tracks))
    except (AttributeError, TypeError, ValueError):
        return ()
    return slots if authoritative_slots(slots) else ()


def _capture_file_identities(paths, context_tracks):
    return {str(Path(path)): _file_track_identity(path, context_tracks) for path in paths}


def _pair_files_to_tracks(paths, tracks, identities, context_tracks):
    """Pair downloaded files to Qobuz tracks without guessing at twins.

    Strong identity locks a file: an explicit disc/track that names another
    album slot cannot fall through to a convenient title match. Weaker track
    number and title matching is allowed only when that key is unique across
    the complete Qobuz context.
    """
    paths = list(paths)
    tracks = list(tracks)
    if not paths or not tracks:
        return []
    context_ids = [_track_identity(track) for track in context_tracks]
    context_counts = {
        layer: Counter(
            identity.get(layer) for identity in context_ids if identity.get(layer) is not None
        )
        for layer in ("isrc", "position", "track", "title")
    }
    file_ids = [
        identities.get(str(Path(path))) or _file_track_identity(path, context_tracks)
        for path in paths
    ]
    track_ids = [_track_identity(track) for track in tracks]

    def allowed_layer(identity):
        if identity.get("conflicted"):
            return None
        isrc = identity.get("isrc")
        if isrc and context_counts["isrc"].get(isrc, 0) == 1:
            return "isrc"
        if identity.get("position") is not None:
            return "position"
        number = identity.get("track")
        if number is not None and context_counts["track"].get(number, 0) == 1:
            return "track"
        title = identity.get("title")
        if title and context_counts["title"].get(title, 0) == 1:
            return "title"
        return None

    remaining_files = set(range(len(paths)))
    remaining_tracks = set(range(len(tracks)))
    pairs = []
    for layer in ("isrc", "position", "track", "title"):
        file_groups = {}
        track_groups = {}
        for index in remaining_files:
            identity = file_ids[index]
            key = identity.get(layer)
            if key is not None and allowed_layer(identity) == layer:
                file_groups.setdefault(key, []).append(index)
        for index in remaining_tracks:
            key = track_ids[index].get(layer)
            if key is not None:
                track_groups.setdefault(key, []).append(index)
        for key, file_group in file_groups.items():
            track_group = track_groups.get(key, [])
            if (
                context_counts[layer].get(key, 0) != 1
                or len(file_group) != 1
                or len(track_group) != 1
            ):
                continue
            file_index, track_index = file_group[0], track_group[0]
            if file_index not in remaining_files or track_index not in remaining_tracks:
                continue
            remaining_files.remove(file_index)
            remaining_tracks.remove(track_index)
            pairs.append((paths[file_index], tracks[track_index]))
    return pairs


def _remove_reject(bucket, rejected):
    for index, item in enumerate(bucket):
        if item == rejected:
            del bucket[index]
            return True
    return False


def _reject_label(path):
    try:
        return Path(path).stem
    except (TypeError, ValueError):
        return str(path)


def retain_download_staging(result, *, label="interrupted", recovery_checkpoint=None):
    """Park one exact rip root after cancellation or an abrupt stop."""
    if result.get("_staging_run_retained"):
        return True
    run = staging_run_from_record(result.get("_staging_run"))
    if run is None:
        return False
    if (run.owner is None) != (recovery_checkpoint is None):
        raise ValueError("owned download staging requires a recovery checkpoint")
    if recovery_checkpoint is not None and not callable(recovery_checkpoint):
        raise ValueError("recovery checkpoint must be callable")
    if run.owner is None:
        retained = retain_staging_run(run, label=label)
    else:
        retained = retain_staging_run(run, label=label, on_intent=recovery_checkpoint)
    if retained is None:
        return False
    result["_staging_run_retained"] = True
    return True


def retire_empty_download_staging(result, *, recovery_checkpoint=None):
    """Durably reclaim an exact run root only after its contents moved out."""
    if result.get("_staging_run_retained"):
        return False
    run = staging_run_from_record(result.get("_staging_run"))
    if run is None:
        return False
    if (run.owner is None) != (recovery_checkpoint is None):
        raise ValueError("owned download staging requires a recovery checkpoint")
    if recovery_checkpoint is not None and not callable(recovery_checkpoint):
        raise ValueError("recovery checkpoint must be callable")
    current = capture_staging_run(run)
    if current is None or current.files:
        return False
    if run.owner is None:
        retained = retain_staging_run(run, label="completed-empty")
        return retained is not None and discard_group(retained)
    retained = retain_staging_run(run, label="completed-empty", on_intent=recovery_checkpoint)
    return retained is not None and discard_group(retained, expected_owner=run.owner)


def retire_download_staging_after_import(result, *, recovery_checkpoint=None):
    """Reclaim the run once beets has moved every audio track out.

    A gap-fill or per-track rip stages the album cover next to the tracks;
    merging into an album that already has its art leaves that cover in the
    run, which is not an incomplete import. Accept when no audio remains and
    dispose the run, art and all. Leftover audio means beets did not import
    everything, so leave it for recovery.
    """
    if result.get("_staging_run_retained"):
        return False
    run = staging_run_from_record(result.get("_staging_run"))
    if run is None:
        return False
    if (run.owner is None) != (recovery_checkpoint is None):
        raise ValueError("owned download staging requires a recovery checkpoint")
    if recovery_checkpoint is not None and not callable(recovery_checkpoint):
        raise ValueError("recovery checkpoint must be callable")
    current = capture_staging_run(run)
    if current is None:
        return False
    if any(
        (current.path / relative).suffix.lower() in cfg.AUDIO_EXTS
        for relative, _identity in current.files
    ):
        return False
    if run.owner is None:
        retained = retain_staging_run(run, label="completed-import")
        return retained is not None and discard_group(retained)
    retained = retain_staging_run(run, label="completed-import", on_intent=recovery_checkpoint)
    return retained is not None and discard_group(retained, expected_owner=run.owner)


def discard_download_staging(result, *, recovery_checkpoint=None):
    """Throw away one exact run root after a deliberate cancel."""
    if result.get("_staging_run_retained"):
        return False
    run = staging_run_from_record(result.get("_staging_run"))
    if run is None:
        return False
    if (run.owner is None) != (recovery_checkpoint is None):
        raise ValueError("owned download staging requires a recovery checkpoint")
    if recovery_checkpoint is not None and not callable(recovery_checkpoint):
        raise ValueError("recovery checkpoint must be callable")
    if run.owner is None:
        retained = retain_staging_run(run, label="cancelled")
        return retained is not None and discard_group(retained)
    retained = retain_staging_run(run, label="cancelled", on_intent=recovery_checkpoint)
    return retained is not None and discard_group(retained, expected_owner=run.owner)


def _receipt_records(receipts):
    return [
        {"path": str(receipt.path), "identity": list(receipt.identity)}
        for receipt in receipts
        if hasattr(receipt, "identity")
    ]


def _staged_binding_records(pairs, keys_by_object, slot_order):
    records = []
    seen_slots = set()
    seen_paths = set()
    seen_nodes = set()
    for path, track in pairs:
        receipt = path if hasattr(path, "identity") else capture_file(path)
        slot = keys_by_object.get(id(track))
        if (
            receipt is None
            or not isinstance(slot, str)
            or not slot
            or len(receipt.identity) != 6
            or not all(type(part) is int for part in receipt.identity)
        ):
            return []
        absolute = str(Path(receipt.path))
        node = tuple(receipt.identity[:2])
        if slot in seen_slots or absolute in seen_paths or node in seen_nodes:
            return []
        seen_slots.add(slot)
        seen_paths.add(absolute)
        seen_nodes.add(node)
        records.append(
            {
                "slot": slot,
                "path": absolute,
                "identity": list(receipt.identity),
            }
        )
    # Pairing follows the staging walk, which follows directory order - with
    # parallel track downloads that rarely matches the catalogue. Everything
    # downstream (journal lineages, the durable runner's binding compare)
    # holds bindings in catalogue-slot order, so emit that order here.
    position = {slot: index for index, slot in enumerate(slot_order)}
    records.sort(key=lambda record: position[record["slot"]])
    return records


def staged_track_bindings(result):
    """Return exact Qobuz-slot bindings for the complete staged audio set."""
    records = result.get("_staged_track_bindings")
    if not isinstance(records, list) or not records:
        raise OSError("staged track bindings are unavailable")
    parsed = []
    seen_slots = set()
    seen_paths = set()
    seen_nodes = set()
    for record in records:
        if (
            type(record) is not dict
            or set(record) != {"slot", "path", "identity"}
            or not isinstance(record.get("slot"), str)
            or not record["slot"]
            or len(record["slot"]) > 256
            or "\x00" in record["slot"]
            or not isinstance(record.get("path"), str)
            or not record["path"]
            or "\x00" in record["path"]
            or not isinstance(record.get("identity"), list)
            or len(record["identity"]) != 6
            or not all(type(part) is int and part >= 0 for part in record["identity"])
        ):
            raise OSError("staged track bindings are malformed")
        frozen = tuple(record["identity"])
        receipt = capture_file(record["path"], expected=frozen)
        if receipt is None:
            raise OSError("staged track binding changed")
        slot = record["slot"]
        path = str(receipt.path)
        node = receipt.identity[:2]
        if slot in seen_slots or path in seen_paths or node in seen_nodes:
            raise OSError("staged track bindings are not unique")
        seen_slots.add(slot)
        seen_paths.add(path)
        seen_nodes.add(node)
        parsed.append(
            {
                "slot": slot,
                "path": path,
                "identity": list(receipt.identity),
            }
        )

    run = staging_run_from_record(result.get("_staging_run"))
    current = capture_staging_run(run)
    if current is None:
        raise OSError("staged download changed before import")
    current_audio = {
        str(current.path / relative): identity
        for relative, identity in current.files
        if (current.path / relative).suffix.lower() in cfg.AUDIO_EXTS
    }
    bound_audio = {record["path"]: tuple(record["identity"]) for record in parsed}
    if current_audio != bound_audio:
        raise OSError("staged audio does not match its Qobuz bindings")
    return tuple(parsed)


def refresh_staged_track_bindings(result):
    """Re-seal the same staged slot paths after an authorised in-place rewrite."""
    records = result.get("_staged_track_bindings")
    if not isinstance(records, list) or not records:
        raise OSError("staged track bindings are unavailable")

    parsed = []
    seen_slots = set()
    seen_paths = set()
    seen_nodes = set()
    for record in records:
        if (
            type(record) is not dict
            or set(record) != {"slot", "path", "identity"}
            or not isinstance(record.get("slot"), str)
            or not record["slot"]
            or len(record["slot"]) > 256
            or "\x00" in record["slot"]
            or not isinstance(record.get("path"), str)
            or not record["path"]
            or "\x00" in record["path"]
            or not isinstance(record.get("identity"), list)
            or len(record["identity"]) != 6
            or not all(type(part) is int and part >= 0 for part in record["identity"])
        ):
            raise OSError("staged track bindings are malformed")
        receipt = capture_file(record["path"])
        if receipt is None:
            raise OSError("rewritten staged track is unavailable")
        slot = record["slot"]
        path = str(receipt.path)
        node = receipt.identity[:2]
        if slot in seen_slots or path in seen_paths or node in seen_nodes:
            raise OSError("rewritten staged track bindings are not unique")
        seen_slots.add(slot)
        seen_paths.add(path)
        seen_nodes.add(node)
        parsed.append(
            {
                "slot": slot,
                "path": path,
                "identity": list(receipt.identity),
            }
        )

    run = staging_run_from_record(result.get("_staging_run"))
    current = capture_staging_run(run)
    if current is None:
        raise OSError("staged download changed during source rewrite")
    current_audio = {
        str(current.path / relative): identity
        for relative, identity in current.files
        if (current.path / relative).suffix.lower() in cfg.AUDIO_EXTS
    }
    rebound_audio = {record["path"]: tuple(record["identity"]) for record in parsed}
    if current_audio != rebound_audio:
        raise OSError("rewritten staged audio does not match its Qobuz bindings")

    result["_staged_track_bindings"] = parsed
    refreshed_receipts = [
        {
            "path": record["path"],
            "identity": list(record["identity"]),
        }
        for record in parsed
    ]
    result["_clean_staged_files"] = [dict(record) for record in refreshed_receipts]
    result["_all_staged_audio"] = [dict(record) for record in refreshed_receipts]
    return tuple(parsed)


def _bindings_match_catalogue(records, tracks, catalogue_slots):
    """Confirm persisted slot labels from each file's exact disc/track."""
    if len(tracks) != len(catalogue_slots):
        return False
    slots_by_position = {}
    for track, slot in zip(tracks, catalogue_slots, strict=True):
        position = _track_identity(track)["position"]
        if position is None or position in slots_by_position:
            return False
        slots_by_position[position] = slot
    for record in records:
        identity = _file_track_identity(record["path"], tracks)
        if (
            identity["conflicted"]
            or identity["position"] not in slots_by_position
            or record["slot"] != slots_by_position[identity["position"]]
        ):
            return False
    return True


def exact_download_coverage(result, album):
    """Copy one current exact staged result into immutable proof input."""
    try:
        if type(result) is not dict or type(album) is not dict:
            return None
        album_id = normalise_album_id(album.get("id"))
        tracks = (album.get("tracks") or {}).get("items")
        if album_id is None or not isinstance(tracks, list) or not tracks:
            return None
        catalogue_slots = album_track_slots(album)
        if not catalogue_slots:
            return None
        stored_catalogue = result.get("_catalogue_track_keys")
        requested = result.get("_expected_track_keys")
        if (
            result.get("_album_id") != album_id
            or not isinstance(stored_catalogue, list)
            or tuple(stored_catalogue) != catalogue_slots
            or not isinstance(requested, list)
        ):
            return None
        requested_slots = tuple(requested)
        if not authoritative_slots(requested_slots) or not set(requested_slots) <= set(
            catalogue_slots
        ):
            return None

        records = staged_track_bindings(result)
        if not _bindings_match_catalogue(records, tracks, catalogue_slots):
            return None
        bindings = tuple(
            StagedBinding(
                slot=record["slot"],
                path=record["path"],
                identity=tuple(record["identity"]),
            )
            for record in records
        )
        raw_counts = (
            result.get("n_fail"),
            result.get("n_lossy"),
            result.get("_unmatched_audio"),
            result.get("_retained_rejects", 0),
        )
        if not all(type(value) is int and value >= 0 for value in raw_counts) or any(
            type(result.get(name)) is not list or bool(result[name])
            for name in ("failed_tracks", "lossy_tracks", "broken_tracks")
        ):
            return None
        return DownloadCoverage(
            album_id=album_id,
            catalogue_slots=catalogue_slots,
            requested_slots=requested_slots,
            bindings=bindings,
            counts=DownloadCounts(
                failed=raw_counts[0],
                lossy=raw_counts[1],
                # n_lossy is the final unique set of unresolved lossy *or*
                # broken target slots.
                broken=0,
                unmatched=raw_counts[2],
                extra=raw_counts[3],
            ),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def download_staged_files(result, *, clean_only=False):
    key = "_clean_staged_files" if clean_only else "_all_staged_audio"
    records = result.get(key)
    if not isinstance(records, list):
        return []
    receipts = []
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "identity"}
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("identity"), list)
            or len(record["identity"]) != 6
            or not all(type(part) is int for part in record["identity"])
        ):
            return []
        receipt = capture_file(record["path"], expected=tuple(record["identity"]))
        if receipt is None:
            return []
        receipts.append(receipt)
    return receipts


def validated_staged_album_dirs(result):
    """Return import roots only when every staged audio file is exact and owned."""
    if result.get("_unmatched_audio") != 0:
        raise OSError("downloaded audio included an unclassified Qobuz track")
    run = staging_run_from_record(result.get("_staging_run"))
    current_run = capture_staging_run(run)
    clean = download_staged_files(result, clean_only=True)
    all_audio = download_staged_files(result)
    if current_run is None or not clean or len(clean) != len(all_audio):
        raise OSError("staged download identity changed before import")
    owned = {receipt.path: receipt.identity for receipt in clean}
    album_dirs = set()
    for receipt in clean:
        try:
            receipt.path.relative_to(current_run.path)
        except ValueError:
            raise OSError("staged audio escaped its run directory") from None
        parent = receipt.path.parent
        if _DISC_DIR_RE.match(parent.name):
            parent = parent.parent
        album_dirs.add(parent)
    for directory in album_dirs:
        from qobuz_librarian.integrations.staging import capture_tree

        tree = capture_tree(directory)
        if tree is None:
            raise OSError("staged album changed before import")
        current_audio = {
            tree.path / relative: identity
            for relative, identity in tree.files
            if (tree.path / relative).suffix.lower() in cfg.AUDIO_EXTS
        }
        expected_audio = {
            path: identity
            for path, identity in owned.items()
            if path == directory or directory in path.parents
        }
        if current_audio != expected_audio:
            raise OSError("staged album contains changed or unclassified audio")
    return sorted(album_dirs)


def run_album_download(
    *,
    album,
    missing,
    present,
    album_dir,
    snapshot,
    existing=None,
    quality=None,
    upgrade_only=False,
    force_track_by_track=False,
    result=None,
    recovery_owner=None,
    recovery_checkpoint=None,
    required_backup_kind=None,
):
    """Download ``missing`` for one album and reconcile what actually landed.

    Picks a single full-album rip when most of the album is missing, else
    fetches track by track. ``existing`` is the on-disk track list (dicts with
    "path") used to stash already-owned tracks before a full-album re-rip;
    pass None to have it read from ``album_dir`` only if that branch is reached.

    Writes into ``result`` (created if None) as it goes - ``gap_fill_backup_path``
    the moment the backup is taken, then n_ok / n_fail / n_lossy /
    failed_tracks / lossy_tracks / rate_limited / elapsed / download_full_album /
    full_album_rc at the end - and returns it. Honours is_cancel_requested() to
    stop early; raises AuthLost on auth loss and OSError(ENOSPC) on a full disk
    for the caller to handle."""
    recovery_owner = normalise_recovery_owner(recovery_owner)
    if required_backup_kind not in {None, "upgrade", "gap-fill"}:
        raise ValueError("required backup kind is invalid")
    if required_backup_kind is not None and recovery_owner is None:
        raise ValueError("required backups need a durable recovery owner")
    if (recovery_owner is None) != (recovery_checkpoint is None):
        raise ValueError("owned downloads require a recovery checkpoint")
    if recovery_checkpoint is not None and not callable(recovery_checkpoint):
        raise ValueError("recovery checkpoint must be callable")

    if result is None:
        result = {}
    result.setdefault("gap_fill_backup_path", None)
    staging_run = None

    def _rip(url, **kwargs):
        nonlocal staging_run
        if staging_run is None:
            if recovery_owner is None:
                staging_run = create_staging_run()
                result.pop("_staging_run_retained", None)
                result["_staging_run"] = staging_run.to_record()
            else:

                def created(record):
                    result.pop("_staging_run_retained", None)
                    result["_staging_run"] = record
                    recovery_checkpoint(
                        {
                            "version": 1,
                            "kind": "staging-run",
                            "owner": dict(recovery_owner),
                            "record": record,
                        }
                    )

                staging_run = create_staging_run(owner=recovery_owner, on_created=created)
        try:
            return rip_url(url, staging_dir=staging_run.path, **kwargs)
        except BaseException:
            if recovery_owner is None:
                retain_download_staging(result)
            else:
                retain_download_staging(result, recovery_checkpoint=recovery_checkpoint)
            raise

    def _run_files_since(prior):
        if staging_run is None or capture_staging_run(staging_run) is None:
            raise OSError("staging run directory changed during download")
        added = files_added_since(prior)
        owned = []
        for receipt in added:
            try:
                receipt.path.relative_to(staging_run.path)
            except ValueError:
                continue
            owned.append(receipt)
        return owned

    qobuz_tracks = (album.get("tracks") or {}).get("items") or []
    n_tracks_total = len(qobuz_tracks)

    # Streamrip's track-URL path crashes with KeyError: 'body' on some tracks
    # (older catalog, edge metadata), so prefer the album URL when most of the
    # album is missing - beets merges any redundant duplicate of a present
    # track on import.
    if force_track_by_track:
        download_full_album = False
    elif upgrade_only:
        download_full_album = len(missing) == n_tracks_total
    else:
        download_full_album = len(present) == 0 or len(missing) >= max(4, int(n_tracks_total * 0.7))

    album_id = album.get("id")
    t_start = time.time()
    n_fail = 0
    failed_tracks = []
    # Keep failed tracks as their Qobuz objects. Titles are display text, not
    # identity: two requested tracks can share one across discs or versions.
    failed_track_objs = []
    full_album_rc = None
    rate_limited = False

    if download_full_album:
        log.info(
            fmt(C.GRAY, f"  Strategy: full-album URL ({len(missing)} of {n_tracks_total} missing)")
        )
    else:
        why = (
            "forced per-track (repair)"
            if force_track_by_track
            else f"{len(missing)} of {n_tracks_total} missing"
        )
        log.info(fmt(C.GRAY, f"  Strategy: per-track ({why})"))

    # Free-space preflight.
    if cfg.MIN_FREE_STAGING_MB > 0:
        try:
            free_mb = shutil.disk_usage(cfg.STAGING_DIR).free // (1024 * 1024)
        except OSError:
            free_mb = None
        if free_mb is not None and free_mb < cfg.MIN_FREE_STAGING_MB:
            raise OSError(
                28,
                f"Only {free_mb} MB free at {cfg.STAGING_DIR} "
                f"(below the {cfg.MIN_FREE_STAGING_MB} MB MIN_FREE_STAGING_MB "
                f"floor) - refusing to start the download.",
            )

    if download_full_album:
        url = f"https://play.qobuz.com/album/{album_id}"
        section("Downloading full album")
        report_progress(
            "Downloading album", 0, 0, f"{album.get('title') or '?'} · {n_tracks_total} tracks"
        )
        vlog(f"  ⟳  {url}")
        # Move the already-present tracks to a backup before the rip so beets
        # doesn't create 'Foo.1.flac' duplicates on import, and so a rip
        # failure (network drop, Ctrl+C, auth loss) can't leave the user with
        # permanently lost tracks.
        if present and album_dir:
            ex = existing if existing is not None else read_album_dir(album_dir)
            extra_paths = {e["path"] for e in find_extras_in_existing(qobuz_tracks, ex)}
            to_clear = [e for e in ex if e["path"] not in extra_paths]
            if to_clear:
                vlog(
                    f"pre-download: backing up + removing {len(to_clear)} present "
                    f"track(s) to prevent .1.flac collisions"
                )
                if recovery_owner is None:
                    backup_result = backup_gap_fill_files([e["path"] for e in to_clear], album_dir)
                else:
                    backup_result = backup_gap_fill_files(
                        [e["path"] for e in to_clear],
                        album_dir,
                        owner=recovery_owner,
                        on_intent=recovery_checkpoint,
                    )
                result["gap_fill_backup_path"] = backup_result
                if recovery_owner is not None and backup_result is not None:
                    carrier = library_backup_record(
                        backup_result,
                        expected_owner=recovery_owner,
                    )
                    if carrier is None:
                        raise OSError(
                            "Present-track backup could not be reopened "
                            "exactly; the download was not started."
                        )
                    recovery_checkpoint({
                        "version": 1,
                        "kind": "library-backup-carrier",
                        "owner": dict(recovery_owner),
                        "carrier": carrier,
                    })
                if backup_result is None:
                    raise OSError(
                        "Present tracks could not be backed up safely; "
                        "the download was not started."
                    )
                if not backup_result.complete:
                    log.info(
                        fmt(
                            C.RED,
                            "  ✗  Only part of the pre-download backup completed. "
                            "The download was not started; retained originals are "
                            f"at {backup_result.path}.",
                        )
                    )
                    raise OSError("Partial pre-download backup; refusing to download.")
            elif required_backup_kind == "gap-fill":
                raise OSError(
                    "Present tracks could not be bound to an exact backup; "
                    "the download was not started."
                )
        elif required_backup_kind == "gap-fill":
            raise OSError(
                "The durable gap-fill backup precondition changed; "
                "the download was not started."
            )
        rc, out = _rip(url, timeout=cfg.RIP_TIMEOUT, live_output=True, quality=quality)
        full_album_rc = rc
        if detect_auth_lost(out):
            raise AuthLost("rip output contained auth-lost markers")
        if detect_disk_full(out):
            raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
        rate_limited = rate_limited or detect_rate_limited(out)
        # rip exits 0 even when it skipped tracks after persistent retries;
        # count the ERROR markers so a "succeeded" line can't hide a gap.
        n_errors = len(re.findall(r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s*)?ERROR\b", out, re.MULTILINE))
        if rc != 0:
            log.info(fmt(C.RED, f"  ✗  rip exit {rc}; last 300 chars:"))
            log.info(fmt(C.GRAY, "  " + out[-300:].replace("\n", "\n  ")))
        elif n_errors:
            log.info(
                fmt(
                    C.YELLOW,
                    f"  ⚠  rip exit 0 but {n_errors} error(s) in output - "
                    f"some tracks likely skipped (see summary below).",
                )
            )
        else:
            log.info(fmt(C.GREEN, "  ✓  Download succeeded."))
    else:
        section("Downloading missing tracks")
        for i, t in enumerate(missing, 1):
            if is_cancel_requested():
                break
            tid = t.get("id")
            # Show the version + track number so an EP of same-titled remixes
            # doesn't render as N identical lines that look like a dup-download.
            ttl = t.get("title") or "?"
            ver = t.get("version") or ""
            if ver and ver.lower() not in ttl.lower():
                ttl = f"{ttl} ({ver})"
            tnum = t.get("track_number")
            tnum_prefix = f"#{tnum:>2} · " if tnum else ""
            log.info(
                fmt(C.BLUE, f"\n  [{i}/{len(missing)}]")
                + f"  {fmt(C.WHITE, truncate(tnum_prefix + ttl, 60))}"
            )
            report_progress("Downloading", i, len(missing), ttl)
            rc, out = _rip(
                f"https://play.qobuz.com/track/{tid}", timeout=cfg.RIP_TIMEOUT, quality=quality
            )
            if detect_auth_lost(out):
                raise AuthLost("rip output contained auth-lost markers")
            if detect_disk_full(out):
                raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
            rate_limited = rate_limited or detect_rate_limited(out)
            if rc == 0:
                log.info(fmt(C.GREEN, "    ✓ ok"))
            elif is_cancel_requested():
                # rip exited because we asked it to stop, not a real failure.
                break
            else:
                failed_track_objs.append(t)
                if "KeyError: 'body'" in out:
                    log.info(
                        fmt(
                            C.RED,
                            "    ✗ streamrip KeyError on track endpoint "
                            "(known bug; usually works via album URL).",
                        )
                    )
                else:
                    log.info(fmt(C.RED, f"    ✗ rip exit {rc}"))
                    log.info(fmt(C.GRAY, "      " + out[-200:].replace("\n", " ")))
            # Qobuz throttles sustained per-track pulls; when the last rip shows
            # throttle signals, pause longer before the next so we stop pounding
            # the limit (set RATE_LIMIT_COOLDOWN=0 to disable).
            cooldown = cfg.RATE_LIMIT_COOLDOWN if detect_rate_limited(out) else 0
            if cooldown and i < len(missing):
                log.info(
                    fmt(
                        C.YELLOW,
                        f"    ⏳ Qobuz rate-limit detected - cooling down "
                        f"{int(cooldown)}s before the next track.",
                    )
                )
                time.sleep(cooldown)
            else:
                time.sleep(cfg.DELAY_BETWEEN)

    new_files = _run_files_since(snapshot)
    audio_new = [f for f in new_files if f.suffix.lower() in cfg.AUDIO_EXTS]
    vlog(f"  {len(new_files)} new file(s) in staging ({len(audio_new)} audio)")
    # cleanup_lossy removes rejects, so capture their tags and path-derived
    # disc/track identity first. A title alone is not safe for same-title twins.
    file_identities = _capture_file_identities(audio_new, qobuz_tracks)
    if recovery_owner is None:
        kept, lossy, broken = cleanup_lossy(audio_new)
    else:
        kept, lossy, broken = cleanup_lossy(
            audio_new,
            owner=recovery_owner,
            on_intent=recovery_checkpoint,
        )
    n_ok = len(kept)
    attempted_tracks = qobuz_tracks if download_full_album else missing
    retried_clean_targets = set()

    # Both reject kinds get one per-track retry: a broken FLAC is usually a
    # transient glitch, and the album URL occasionally serves lossy for a
    # track the track URL has lossless.
    discarded = lossy + broken
    resolved_rejects = []
    if discarded and attempted_tracks and not is_cancel_requested():
        retry_pairs = _pair_files_to_tracks(
            discarded, attempted_tracks, file_identities, qobuz_tracks
        )
        if retry_pairs:
            log.info(
                fmt(
                    C.GRAY,
                    f"  ↻  Retrying {len(retry_pairs)} lossy/incomplete "
                    "track(s) once via per-track URL",
                )
            )
            recovered = 0
            for rejected, t in retry_pairs:
                if is_cancel_requested():
                    break
                tid = t.get("id")
                if not tid:
                    continue
                # Collect this target independently. A clean same-title file
                # produced by another retry must never vouch for this one.
                retry_snapshot = snapshot_staging()
                rc, out = _rip(
                    f"https://play.qobuz.com/track/{tid}", timeout=cfg.RIP_TIMEOUT, quality=quality
                )
                if detect_auth_lost(out):
                    raise AuthLost("rip output contained auth-lost markers")
                if detect_disk_full(out):
                    raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
                rate_limited = rate_limited or detect_rate_limited(out)
                retry_audio = [
                    f
                    for f in _run_files_since(retry_snapshot)
                    if f.suffix.lower() in cfg.AUDIO_EXTS
                ]
                retry_identities = _capture_file_identities(retry_audio, qobuz_tracks)
                if recovery_owner is None:
                    retry_kept, _, _ = cleanup_lossy(retry_audio)
                else:
                    retry_kept, _, _ = cleanup_lossy(
                        retry_audio,
                        owner=recovery_owner,
                        on_intent=recovery_checkpoint,
                    )
                matches = _pair_files_to_tracks(retry_kept, [t], retry_identities, qobuz_tracks)
                if not matches:
                    continue
                recovered_path, _ = matches[0]
                removed = _remove_reject(lossy, rejected) or _remove_reject(broken, rejected)
                if not removed:
                    continue
                resolved_rejects.append(rejected)
                kept.append(recovered_path)
                file_identities.update(retry_identities)
                retried_clean_targets.add(id(t))
                recovered += 1
            if recovered:
                n_ok = len(kept)
                log.info(fmt(C.GREEN, f"  ✓  Retry recovered {recovered} track(s)"))

    # A HARD failure (rip errored with no file landing at all - distinct from
    # a file that landed lossy/broken, retried above) gets one more per-track
    # pull before it's given up on: a transient 5xx / momentary network blip
    # usually clears on a second attempt, and otherwise the user has to re-run
    # the whole repair or download just for that one track.
    if failed_track_objs and missing and not is_cancel_requested():
        clean_failed_ids = {
            id(track)
            for _, track in _pair_files_to_tracks(
                kept, failed_track_objs, file_identities, qobuz_tracks
            )
        }
        rejected_failed_ids = {
            id(track)
            for _, track in _pair_files_to_tracks(
                lossy + broken, failed_track_objs, file_identities, qobuz_tracks
            )
        }
        hard_targets = [
            track
            for track in failed_track_objs
            if id(track) not in clean_failed_ids
            and id(track) not in rejected_failed_ids
            and track.get("id")
        ]
        if hard_targets:
            log.info(
                fmt(
                    C.GRAY,
                    f"  ↻  Retrying {len(hard_targets)} failed download(s) once via per-track URL",
                )
            )
            recovered = 0
            for t in hard_targets:
                if is_cancel_requested():
                    break
                hard_snapshot = snapshot_staging()
                rc, out = _rip(
                    f"https://play.qobuz.com/track/{t['id']}",
                    timeout=cfg.RIP_TIMEOUT,
                    quality=quality,
                )
                if detect_auth_lost(out):
                    raise AuthLost("rip output contained auth-lost markers")
                if detect_disk_full(out):
                    raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
                rate_limited = rate_limited or detect_rate_limited(out)
                time.sleep(cfg.DELAY_BETWEEN)
                hard_audio = [
                    f for f in _run_files_since(hard_snapshot) if f.suffix.lower() in cfg.AUDIO_EXTS
                ]
                hard_identities = _capture_file_identities(hard_audio, qobuz_tracks)
                if recovery_owner is None:
                    hard_kept, hard_lossy, hard_broken = cleanup_lossy(hard_audio)
                else:
                    hard_kept, hard_lossy, hard_broken = cleanup_lossy(
                        hard_audio,
                        owner=recovery_owner,
                        on_intent=recovery_checkpoint,
                    )
                matches = _pair_files_to_tracks(hard_kept, [t], hard_identities, qobuz_tracks)
                if matches:
                    kept.append(matches[0][0])
                    file_identities.update(hard_identities)
                    retried_clean_targets.add(id(t))
                    recovered += 1
                    continue
                # Preserve an exact reject from the hard retry in the right
                # summary bucket. Unexpected or ambiguous files prove nothing.
                reject_matches = _pair_files_to_tracks(
                    hard_lossy + hard_broken, [t], hard_identities, qobuz_tracks
                )
                if reject_matches:
                    rejected_path = reject_matches[0][0]
                    (lossy if rejected_path in hard_lossy else broken).append(rejected_path)
                    file_identities.update(hard_identities)
            if recovered:
                n_ok = len(kept)
                log.info(fmt(C.GREEN, f"  ✓  Retry recovered {recovered} failed download(s)"))

    # Reconcile completeness by unique Qobuz track identity, never by the raw
    # number of audio names that appeared.
    lossy_tracks = lossy + broken
    clean_pairs = _pair_files_to_tracks(kept, attempted_tracks, file_identities, qobuz_tracks)
    clean_target_ids = {id(track) for _, track in clean_pairs}
    clean_paths = {Path(path) for path, _ in clean_pairs}
    unmatched_clean = [path for path in kept if Path(path) not in clean_paths]
    rejected_target_ids = {
        id(track)
        for _, track in _pair_files_to_tracks(
            lossy_tracks, attempted_tracks, file_identities, qobuz_tracks
        )
    } - clean_target_ids
    expected_target_ids = {id(track) for track in attempted_tracks}
    missing_target_ids = expected_target_ids - clean_target_ids - rejected_target_ids
    n_ok = len(clean_target_ids)
    n_lossy = len(rejected_target_ids)
    n_fail = max(len(missing_target_ids), 1 if unmatched_clean else 0)
    failed_tracks = [
        track.get("title") or "?" for track in attempted_tracks if id(track) in missing_target_ids
    ]

    if not download_full_album and failed_track_objs:
        failed_ids = {id(track) for track in failed_track_objs}
        landed_despite_error = (clean_target_ids & failed_ids) - retried_clean_targets
        if landed_despite_error:
            log.info(
                fmt(
                    C.GRAY,
                    f"  · {len(landed_despite_error)} track(s) landed despite a streamrip "
                    f"post-processing error - counting as success.",
                )
            )

    if download_full_album and full_album_rc is not None:
        # A full-album rip re-downloads the WHOLE album URL (all
        # n_tracks_total tracks), including the already-present ones we moved
        # to the gap-fill backup - so n_ok (every clean FLAC that landed) is
        # counted against the total, NOT len(missing).
        if n_fail == 0 and full_album_rc != 0 and n_ok > 0:
            log.info(
                fmt(
                    C.GRAY,
                    f"  · {n_ok} track(s) landed despite rip exit "
                    f"{full_album_rc} (streamrip post-processing error).",
                )
            )

    if lossy:
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  {len(lossy)} track(s) only available lossy on Qobuz "
                f"(no lossless for your tier - another source needed):",
            )
        )
        for d in lossy[:5]:
            log.info(fmt(C.GRAY, f"     {_reject_label(d)}"))
    if broken:
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  {len(broken)} track(s) downloaded incomplete and were "
                f"discarded (a re-run usually fixes these):",
            )
        )
        for d in broken[:5]:
            log.info(fmt(C.GRAY, f"     {_reject_label(d)}"))

    # Paths are retained only inside this download phase. The queue, activity
    # log, and CLI have always exposed short display strings.
    lossy_track_labels = [_reject_label(path) for path in lossy_tracks]
    broken_track_labels = [_reject_label(path) for path in broken]

    # Rejects remain durably manifested while their retry is unresolved.
    rejects_to_retire = list(resolved_rejects)
    if not is_cancel_requested():
        rejects_to_retire.extend(lossy_tracks)
    retained_rejects = 0
    seen_rejects = set()
    for rejected in rejects_to_retire:
        key = (str(Path(rejected)), getattr(rejected, "identity", None))
        if key in seen_rejects:
            continue
        seen_rejects.add(key)
        if recovery_owner is None:
            discarded_reject = discard_quarantined_file(rejected)
        else:
            discarded_reject = discard_quarantined_file(rejected, expected_owner=recovery_owner)
        if not discarded_reject:
            retained_rejects += 1
    if retained_rejects:
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  {retained_rejects} rejected staging file(s) changed or "
                "could not be retired; their recovery records were kept.",
            )
        )

    album_keys = _album_track_keys(qobuz_tracks)
    keys_by_object = {id(track): key for track, key in zip(qobuz_tracks, album_keys)}

    result.update(
        {
            "n_ok": n_ok,
            "n_fail": n_fail,
            "n_lossy": n_lossy,
            "failed_tracks": failed_tracks,
            "lossy_tracks": lossy_track_labels,
            "broken_tracks": broken_track_labels,
            "rate_limited": rate_limited,
            "elapsed": time.time() - t_start,
            "download_full_album": download_full_album,
            "full_album_rc": full_album_rc,
            "_album_id": normalise_album_id(album_id),
            "_catalogue_track_keys": list(album_keys),
            "_expected_track_keys": [
                keys_by_object[id(track)]
                for track in attempted_tracks
                if id(track) in keys_by_object
            ],
            "_clean_track_keys": [
                keys_by_object[id(track)]
                for track in attempted_tracks
                if id(track) in clean_target_ids and id(track) in keys_by_object
            ],
            "_exact_track_coverage": (
                not unmatched_clean and clean_target_ids == expected_target_ids
            ),
            "_unmatched_audio": len(unmatched_clean),
            "_retained_rejects": retained_rejects,
            "_clean_staged_files": _receipt_records([path for path, _ in clean_pairs]),
            "_all_staged_audio": _receipt_records(kept),
            "_staged_track_bindings": _staged_binding_records(
                clean_pairs, keys_by_object, album_keys
            ),
        }
    )
    return result
