"""Download phase for one album: pick a strategy, rip, drop lossy/broken
files, retry the strays once, and reconcile the counts.

Shared by the single-album path (`modes/process.py`) and the queue executor
(`queue/executor.py`). Both hand it a staging snapshot and the missing/present
split and read back the `n_ok`/`n_fail`/`n_lossy` bookkeeping. Results are
written into a caller-owned `result` dict as the work progresses - not just
returned - so the gap-fill backup taken mid-download can still be resolved by
the caller's finally/except when a rip raises AuthLost or hits a full disk.
"""

import os
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
from qobuz_librarian.integrations import staging
from qobuz_librarian.integrations.rip import (
    cleanup_lossy,
    files_added_since,
    flac_audio_offset,
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
    keep_staging_run,
    retain_staging_run,
    staging_run_from_record,
)
from qobuz_librarian.library.backup import (
    backup_gap_fill_files,
    library_backup_record,
)
from qobuz_librarian.library.catalog import (
    edition_files,
    find_extras_in_existing,
    unstreamable,
)
from qobuz_librarian.library.scanner import read_album_dir, read_audio_meta
from qobuz_librarian.library.tags import normalize, strip_edition_suffix
from qobuz_librarian.quality.decision import mark_album_capped
from qobuz_librarian.recovery import normalise_recovery_owner
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, report_progress, vlog

# Set once this process starts a download; the terminal mentions held
# staging only after a run that downloaded.
download_ran = False


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
    if meta_disc == 1 and len(context_discs) > 1 and not meta.get("disc_tagged"):
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


def _output_tail(out, length):
    """The end of rip's output without Rich's column padding."""
    return " ".join(out.split())[-length:]


def _cool_down(last_out):
    """Pause before the next rip, longer when Qobuz throttled the last one."""
    seconds = cfg.DELAY_BETWEEN
    if detect_rate_limited(last_out):
        seconds = cfg.RATE_LIMIT_COOLDOWN
        log.info(
            fmt(
                C.YELLOW,
                f"    ⏳ Qobuz rate-limit detected - cooling down "
                f"{int(seconds)}s before the next track.",
            )
        )
    # Stop must not wait out a 30s cooldown.
    while seconds > 0 and not is_cancel_requested():
        step = min(0.5, seconds)
        time.sleep(step)
        seconds -= step


def _single_track_rip_identities(paths, track, context_tracks):
    """A track URL that yields one audio file yielded that track.

    A truncated FLAC can lose the tags that would otherwise place it, and on a
    deluxe edition its filename number and bare title repeat on another disc.
    """
    identities = _capture_file_identities(paths, context_tracks)
    if len(paths) == 1:
        identities[str(Path(paths[0]))] = {**_track_identity(track), "conflicted": False}
    return identities


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


def _reject_key(path):
    return str(Path(path)), getattr(path, "identity", None)


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
    if retained.owner is None:
        try:
            clear_superseded_attempts(
                {relative: tree.path / relative
                 for tree in retained.trees
                 for relative, _identity in tree.files},
                keep=retained.path)
        except OSError as exc:
            vlog(f"couldn't clear older kept copies: {exc}")
    return True


def keep_unimported_download(result):
    """Leave a complete --no-import download where the manual import finds
    it, out of reach of the next run's clean-up of abandoned downloads."""
    if result.get("_staging_run_retained"):
        return None
    run = staging_run_from_record(result.get("_staging_run"))
    if run is None:
        return None
    try:
        return keep_staging_run(run)
    except OSError as exc:
        vlog(f"couldn't keep {run.path} for a manual import: {exc}")
        return None


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


def retire_failed_download_staging(result):
    """Clear the run of a download that imported nothing.

    Usually nothing arrived and the run holds at most cover art. Audio that
    is still there is kept for review as a failed download.
    """
    return retire_download_staging_after_import(result) or retain_download_staging(
        result, label="failed"
    )


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


def _same_bytes(older, newer, older_start, newer_start, length):
    with open(older, "rb") as left, open(newer, "rb") as right:
        left.seek(older_start)
        right.seek(newer_start)
        while length > 0:
            chunk = min(length, 1 << 20)
            if left.read(chunk) != right.read(chunk):
                return False
            length -= chunk
    return True


def _holds_file(kept, newer):
    """Whether the file ``newer`` holds everything the kept file ``kept``
    does. A FLAC track must carry the same stream header and start with all
    of the kept copy's audio, so a whole track covers one cut short while
    another master, a downsampled copy or one damaged past its header never
    does; tags and art may differ. Any other file must be the same bytes."""
    try:
        kept_size = os.stat(kept, follow_symlinks=False).st_size
        newer_size = os.stat(newer, follow_symlinks=False).st_size
        if Path(kept).suffix.lower() != ".flac":
            return (kept_size == newer_size
                    and _same_bytes(kept, newer, 0, 0, kept_size))
        with open(kept, "rb") as fh:
            kept_head = fh.read(42)
        with open(newer, "rb") as fh:
            newer_head = fh.read(42)
        kept_start = flac_audio_offset(kept)
        newer_start = flac_audio_offset(newer)
    except OSError:
        return False
    if (
        len(kept_head) != 42
        or kept_head != newer_head
        or kept_head[:4] != b"fLaC"
        or kept_head[4] & 0x7F
        or not kept_start
        or not newer_start
    ):
        return False
    audio = kept_size - kept_start
    try:
        return (newer_size - newer_start >= audio
                and _same_bytes(kept, newer, kept_start, newer_start, audio))
    except OSError:
        return False


def _attempt_covered(tree, later):
    """Whether a kept attempt holds nothing the later copy ``later`` (path
    inside the run -> file) lacks."""
    if not tree.files or any(relative not in later for relative, _ in tree.files):
        return False
    return all(_holds_file(tree.path / relative, later[relative])
               for relative, _identity in tree.files)


def _superseded_attempts(later, *, keep=None):
    if not later:
        return []
    return [
        inspection.path
        for inspection in staging.inspect_retry_groups()
        if inspection.kind == "interrupted"
        and inspection.status == "ready"
        and inspection.owner is None
        and inspection.path != keep
        and inspection.current_trees
        and all(tree is not None and _attempt_covered(tree, later)
                for tree in inspection.current_trees)
    ]


def _discard_superseded(paths):
    removed = sum(1 for path in paths if discard_group(Path(path)))
    if removed:
        log.info(fmt(C.GRAY,
            f"  Removed {plural(removed, 'older kept copy', 'older kept copies')} "
            "of this album from staging."))
    return removed


def clear_superseded_attempts(later, *, keep=None):
    """Remove the copies earlier stopped downloads kept of an album that a
    later copy, ``later`` (path inside the run -> file), holds all of.

    Only kept staging attempts no download is tied to are considered, and one
    is removed only while every file is exactly as it was set aside.
    """
    return _discard_superseded(_superseded_attempts(later, keep=keep))


def _run_relative(path):
    """``path`` inside a download run, as the run's own files are named."""
    try:
        parts = Path(path).relative_to(cfg.STAGING_DIR).parts
    except ValueError:
        return None
    if len(parts) < 2 or not staging._RUN_DIR_RE.fullmatch(parts[0]):
        return None
    return Path(*parts[1:]).as_posix()


def _superseded_rejects(later):
    """The tracks earlier downloads set aside as damaged that a later copy,
    ``later`` (path inside the run -> file), holds all of, as (group, owner)
    pairs. One tied to a download still saved is left to that download."""
    from qobuz_librarian.queue import startup_recovery

    saved = None
    found = []
    for inspection in staging.inspect_retry_groups():
        group = inspection.file_group
        if (inspection.kind != "rejected" or inspection.status != "ready"
                or group is None or group.original is None):
            continue
        if inspection.owner is not None:
            if saved is None:
                saved = startup_recovery.saved_queue_operation_ids()
            if saved is None or inspection.owner["operation_id"] in saved:
                continue
        relative = _run_relative(group.original.path)
        if relative in later and _holds_file(group.retained.path, later[relative]):
            found.append([str(inspection.path), inspection.owner])
    return found


def note_superseded_attempts(result):
    """Just before an album imports, note the copies earlier attempts kept of
    it, and the tracks they set aside as damaged, that the files about to go
    in, as downsampled and tagged, hold all of."""
    try:
        current = staging.capture_staging_run(
            staging_run_from_record(result.get("_staging_run")))
        if current is not None:
            later = {relative: current.path / relative
                     for relative, _identity in current.files}
            result["_superseded_attempts"] = [
                str(path) for path in _superseded_attempts(later)]
            result["_superseded_rejects"] = _superseded_rejects(later)
    except OSError as exc:
        vlog(f"couldn't compare older kept copies: {exc}")


def clear_attempts_after_import(result):
    """Once an album imported, drop the kept copies noted before it went in."""
    try:
        removed = _discard_superseded(
            result.pop("_superseded_attempts", None) or ())
        rejects = sum(
            1 for path, owner in result.pop("_superseded_rejects", None) or ()
            if staging.discard_file_group(Path(path), expected_owner=owner))
    except OSError as exc:
        vlog(f"couldn't clear older kept copies: {exc}")
        return 0
    if rejects:
        log.info(fmt(C.GRAY,
            f"  Removed {plural(rejects, 'damaged copy', 'damaged copies')} "
            "of this album's tracks from staging."))
    return removed + rejects


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
        ) or result.get("unavailable_tracks"):
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
        tree = staging.capture_tree(directory)
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


def staged_album_dirs(root):
    """Album folders holding audio under ``root``, disc folders merged."""
    album_dirs = set()
    for directory, _dirs, files in os.walk(root):
        if any(Path(name).suffix.lower() in cfg.AUDIO_EXTS for name in files):
            parent = Path(directory)
            if _DISC_DIR_RE.match(parent.name):
                parent = parent.parent
            album_dirs.add(parent)
    return sorted(album_dirs)


def _disc_twins(tracks):
    """True when two discs each hold a track with the same number and title,
    which streamrip saves under one name unless Disc N folders keep them
    apart."""
    seen = {}
    for track in tracks:
        key = (
            track.get("track_number"),
            (track.get("title") or "").strip().casefold(),
            (track.get("version") or "").strip().casefold(),
        )
        disc = track.get("media_number") or 1
        if seen.setdefault(key, disc) != disc:
            return True
    return False


def _into_disc_folder(receipt, track):
    """Move a track-URL rip into the Disc N folder an album rip would use."""
    path = Path(receipt)
    target = path.parent / f"Disc {_positive_int(track.get('media_number')) or 1}" / path.name
    if target.exists():
        return receipt
    try:
        target.parent.mkdir(exist_ok=True)
        os.rename(path, target)
    except OSError:
        return receipt
    return capture_file(target)


def downloads_whole_album(n_present, n_missing, n_total):
    """Whether a gap fill fetches the whole album instead of the gaps."""
    return n_present == 0 or n_missing == n_total


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
    exact_edition=False,
    result=None,
    recovery_owner=None,
    recovery_checkpoint=None,
    required_backup_kind=None,
    expected_gap_fill_receipts=None,
):
    """Download ``missing`` for one album and reconcile what actually landed.

    Fetches partial albums track by track. ``existing`` is the on-disk track list
    (dicts with "path") used to stash already-owned tracks before a full-album re-rip;
    pass None to have it read from ``album_dir`` only if that branch is reached.

    Writes into ``result`` (created if None) as it goes - ``gap_fill_backup_path``
    the moment the backup is taken, then n_ok / n_fail / n_lossy /
    n_unavailable / failed_tracks / lossy_tracks / unavailable_tracks /
    rate_limited / elapsed / download_full_album / full_album_rc at the end -
    and returns it. A track Qobuz does not stream is never fetched on its own;
    it counts in n_fail and again in n_unavailable. Honours
    is_cancel_requested() to stop early; raises AuthLost on auth loss and
    OSError(ENOSPC) on a full disk for the caller to handle."""
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
        global download_ran
        nonlocal staging_run
        download_ran = True
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

    if force_track_by_track:
        download_full_album = False
    elif upgrade_only:
        download_full_album = len(missing) == n_tracks_total
    else:
        download_full_album = downloads_whole_album(
            len(present), len(missing), n_tracks_total)
    # A track URL never gets a Disc N folder, so the second of two twins
    # would overwrite the first.
    disc_folders = not download_full_album and _disc_twins(missing)
    to_fetch = [track for track in missing if not unstreamable(track)]
    if not to_fetch:
        download_full_album = False

    def _rip_track(track, tier=None):
        before = snapshot_staging()
        rc, out = _rip(
            f"https://play.qobuz.com/track/{track.get('id')}",
            timeout=cfg.RIP_TIMEOUT,
            quality=quality if tier is None else tier,
        )
        audio = [f for f in _run_files_since(before) if f.suffix.lower() in cfg.AUDIO_EXTS]
        if disc_folders:
            audio = [moved for f in audio if (moved := _into_disc_folder(f, track))]
        return rc, out, audio

    cut_short = set()

    def _sort_audio(paths):
        # A track the stop cut off is not damaged: it stays in the run, which
        # the stop keeps or throws away whole.
        stopping = is_cancel_requested()
        if recovery_owner is None:
            sorted_audio = cleanup_lossy(paths, keep_broken=stopping)
        else:
            sorted_audio = cleanup_lossy(
                paths, owner=recovery_owner, on_intent=recovery_checkpoint,
                keep_broken=stopping)
        if stopping:
            cut_short.update(_reject_key(path) for path in sorted_audio[2])
        return sorted_audio

    album_id = album.get("id")
    t_start = time.time()
    n_fail = 0
    failed_tracks = []
    # Keep failed tracks as their Qobuz objects. Titles are display text, not
    # identity: two requested tracks can share one across discs or versions.
    failed_track_objs = []
    rip_identities = {}
    verified = []
    lossy = []
    broken = []
    last_out = ""
    full_album_rc = None
    rate_limited = False

    if download_full_album:
        log.info(
            fmt(C.GRAY, f"  Whole album ({len(missing)} of {n_tracks_total} tracks missing)")
        )
    else:
        why = (
            "repair"
            if force_track_by_track
            else f"{len(missing)} of {n_tracks_total} tracks missing"
        )
        log.info(fmt(C.GRAY, f"  Track by track ({why})"))
    if len(to_fetch) < len(missing):
        n_unavailable = len(missing) - len(to_fetch)
        log.info(fmt(C.YELLOW,
            f"  ⚠  {plural(n_unavailable, 'track')} "
            f"{'is' if n_unavailable == 1 else 'are'} not available on Qobuz "
            "and cannot be downloaded."))

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
            "Downloading album", 0, 0,
            f"{album.get('title') or '?'} · {plural(n_tracks_total, 'track')}"
        )
        vlog(f"  ⟳  {url}")
        # Move the already-present tracks to a backup before the rip so beets
        # doesn't create 'Foo.1.flac' duplicates on import, and so a rip
        # failure (network drop, Ctrl+C, auth loss) can't leave the user with
        # permanently lost tracks.
        if present and album_dir:
            ex = existing if existing is not None else read_album_dir(album_dir)
            if exact_edition:
                to_clear = edition_files(album, ex, album_dir)
            else:
                extra_paths = {e["path"] for e in find_extras_in_existing(qobuz_tracks, ex)}
                to_clear = [e for e in ex if e["path"] not in extra_paths]
            if to_clear:
                exact_receipts = expected_gap_fill_receipts
                if expected_gap_fill_receipts is not None:
                    try:
                        album_root = Path(os.path.abspath(os.fspath(album_dir)))
                        exact_receipts = {}
                        for entry in to_clear:
                            source = Path(os.path.abspath(entry["path"]))
                            relative = source.relative_to(album_root).as_posix()
                            exact_receipts[relative] = (
                                expected_gap_fill_receipts[relative]
                            )
                    except (KeyError, OSError, TypeError, ValueError):
                        raise OSError(
                            "Reviewed gap-fill receipts do not cover the "
                            "files that would be moved; the download was not "
                            "started."
                        ) from None
                vlog(
                    f"pre-download: backing up + removing "
                    f"{plural(len(to_clear), 'present track')} to prevent "
                    ".1.flac collisions"
                )
                if recovery_owner is None:
                    backup_result = (
                        backup_gap_fill_files(
                            [e["path"] for e in to_clear],
                            album_dir,
                            expected_receipts=exact_receipts,
                        )
                        if expected_gap_fill_receipts is not None
                        else backup_gap_fill_files(
                            [e["path"] for e in to_clear], album_dir)
                    )
                else:
                    backup_kwargs = {
                        "owner": recovery_owner,
                        "on_intent": recovery_checkpoint,
                    }
                    if expected_gap_fill_receipts is not None:
                        backup_kwargs["expected_receipts"] = exact_receipts
                    backup_result = backup_gap_fill_files(
                        [e["path"] for e in to_clear],
                        album_dir,
                        **backup_kwargs,
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
        last_out = out
        full_album_rc = rc
        if detect_auth_lost(out):
            raise AuthLost("rip output contained auth-lost markers")
        if detect_disk_full(out):
            raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
        rate_limited = rate_limited or detect_rate_limited(out)
        # rip exits 0 even when it skipped tracks after persistent retries;
        # count the ERROR markers so a "succeeded" line can't hide a gap.
        n_errors = len(re.findall(r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s*)?ERROR\b", out, re.MULTILINE))
        n_errors = max(n_errors - (len(missing) - len(to_fetch)), 0)
        if rc != 0:
            # Cancelling also ends rip with a non-zero exit, which is not a failure.
            if not is_cancel_requested():
                log.info(fmt(C.RED, f"  ✗  rip exit {rc}; last 300 chars:"))
                log.info(fmt(C.GRAY, "  " + _output_tail(out, 300)))
        elif n_errors:
            log.info(
                fmt(
                    C.YELLOW,
                    f"  ⚠  rip exit 0 but {plural(n_errors, 'error')} in output - "
                    f"some tracks likely skipped (see summary below).",
                )
            )
        else:
            log.info(fmt(C.GREEN, "  ✓  Download succeeded."))
    else:
        section("Downloading missing tracks")
        for i, t in enumerate(to_fetch, 1):
            if is_cancel_requested():
                break
            # Show the version + track number so an EP of same-titled remixes
            # doesn't render as N identical lines that look like a dup-download.
            ttl = t.get("title") or "?"
            ver = t.get("version") or ""
            if ver and ver.lower() not in ttl.lower():
                ttl = f"{ttl} ({ver})"
            tnum = t.get("track_number")
            tnum_prefix = f"#{tnum:>2} · " if tnum else ""
            log.info(
                fmt(C.BLUE, f"\n  [{i}/{len(to_fetch)}]")
                + f"  {fmt(C.WHITE, truncate(tnum_prefix + ttl, 60))}"
            )
            report_progress("Downloading", i, len(to_fetch), ttl)
            rc, out, track_audio = _rip_track(t)
            rip_identities.update(_single_track_rip_identities(track_audio, t, qobuz_tracks))
            if detect_auth_lost(out):
                raise AuthLost("rip output contained auth-lost markers")
            if detect_disk_full(out):
                raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
            rate_limited = rate_limited or detect_rate_limited(out)
            if rc == 0 and track_audio:
                track_kept, track_lossy, track_broken = _sort_audio(track_audio)
                verified.extend(track_kept)
                lossy.extend(track_lossy)
                broken.extend(track_broken)
                if track_lossy:
                    log.info(fmt(C.YELLOW, "    ⚠ lossy, set aside"))
                elif not track_broken:
                    log.info(fmt(C.GREEN, "    ✓ ok"))
            elif is_cancel_requested():
                # rip exited because we asked it to stop, not a real failure.
                break
            elif rc == 0:
                # streamrip logs a refused track and still exits 0.
                failed_track_objs.append(t)
                log.info(fmt(C.RED, "    ✗ no file arrived"))
                log.info(fmt(C.GRAY, "      " + _output_tail(out, 200)))
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
                    log.info(fmt(C.GRAY, "      " + _output_tail(out, 200)))
            # Qobuz throttles sustained per-track pulls; when the last rip shows
            # throttle signals, pause longer before the next so we stop pounding
            # the limit.
            last_out = out
            if i < len(to_fetch):
                _cool_down(out)

    new_files = _run_files_since(snapshot) if staging_run is not None else []
    audio_new = [f for f in new_files if f.suffix.lower() in cfg.AUDIO_EXTS]
    vlog(f"  {plural(len(new_files), 'new file')} in staging "
         f"({len(audio_new)} audio)")
    # cleanup_lossy removes rejects, so capture their tags and path-derived
    # disc/track identity first. A title alone is not safe for same-title twins.
    file_identities = _capture_file_identities(audio_new, qobuz_tracks)
    file_identities.update(rip_identities)
    kept, late_lossy, late_broken = _sort_audio([f for f in audio_new if f not in verified])
    kept = [f for f in audio_new if f in verified] + kept
    lossy += late_lossy
    broken += late_broken
    n_ok = len(kept)
    attempted_tracks = qobuz_tracks if download_full_album else missing
    retried_clean_targets = set()

    # Both reject kinds get one per-track retry: a broken FLAC is usually a
    # transient glitch, and the album URL occasionally serves lossy for a
    # track the track URL has lossless.
    discarded = lossy + broken
    resolved_rejects = []
    broken_twice = set()
    if discarded and attempted_tracks and not is_cancel_requested():
        retry_pairs = _pair_files_to_tracks(
            discarded, attempted_tracks, file_identities, qobuz_tracks
        )
        if retry_pairs:
            n_broken = sum(1 for path, _ in retry_pairs if path in broken)
            kind = ("broken" if n_broken == len(retry_pairs)
                    else "lossy" if not n_broken else "lossy or broken")
            log.info(
                fmt(
                    C.GRAY,
                    f"  ↻  Retrying {plural(len(retry_pairs), f'{kind} track')} "
                    "once via per-track URL",
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
                _cool_down(last_out)
                if is_cancel_requested():
                    break
                rc, out, retry_audio = _rip_track(t)
                if detect_auth_lost(out):
                    raise AuthLost("rip output contained auth-lost markers")
                if detect_disk_full(out):
                    raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
                rate_limited = rate_limited or detect_rate_limited(out)
                last_out = out
                retry_identities = _single_track_rip_identities(retry_audio, t, qobuz_tracks)
                retry_kept, retry_lossy, retry_broken = _sort_audio(retry_audio)
                # A second reject of the same track is surplus to the first.
                resolved_rejects.extend(retry_lossy + retry_broken)
                if retry_broken:
                    broken_twice.add(id(t))
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
                log.info(fmt(C.GREEN, f"  ✓  Retry recovered {plural(recovered, 'track')}"))

    # The album URL does not say which tracks it failed on, so a track that
    # never landed from it counts as a failed pull, and one that timed out
    # with the whole album gets its own full timeout on the retry below.
    if download_full_album and not is_cancel_requested():
        landed_ids = {
            id(track)
            for _, track in _pair_files_to_tracks(
                kept + lossy + broken, attempted_tracks, file_identities, qobuz_tracks
            )
        }
        failed_track_objs = [
            track for track in attempted_tracks
            if id(track) not in landed_ids and not unstreamable(track)
        ]

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
                    f"  ↻  Retrying {plural(len(hard_targets), 'failed download')} "
                    "once via per-track URL",
                )
            )
            recovered = 0
            for t in hard_targets:
                _cool_down(last_out)
                if is_cancel_requested():
                    break
                rc, out, hard_audio = _rip_track(t)
                if detect_auth_lost(out):
                    raise AuthLost("rip output contained auth-lost markers")
                if detect_disk_full(out):
                    raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
                rate_limited = rate_limited or detect_rate_limited(out)
                last_out = out
                hard_identities = _single_track_rip_identities(hard_audio, t, qobuz_tracks)
                hard_kept, hard_lossy, hard_broken = _sort_audio(hard_audio)
                matches = _pair_files_to_tracks(hard_kept, [t], hard_identities, qobuz_tracks)
                if matches:
                    kept.append(matches[0][0])
                    file_identities.update(hard_identities)
                    retried_clean_targets.add(id(t))
                    recovered += 1
                    continue
                # Preserve an exact reject from the hard retry in the right
                # summary bucket. Unexpected or ambiguous files prove nothing
                # and are retired with the other surplus rejects.
                reject_matches = _pair_files_to_tracks(
                    hard_lossy + hard_broken, [t], hard_identities, qobuz_tracks
                )
                rejected_path = reject_matches[0][0] if reject_matches else None
                if rejected_path is not None:
                    (lossy if rejected_path in hard_lossy else broken).append(rejected_path)
                    file_identities.update(hard_identities)
                resolved_rejects.extend(
                    path for path in hard_lossy + hard_broken if path is not rejected_path
                )
            if recovered:
                n_ok = len(kept)
                log.info(fmt(C.GREEN, f"  ✓  Retry recovered {plural(recovered, 'failed download')}"))

    # Qobuz serves some tracks broken at a hi-res tier while their CD-quality
    # stream is whole; a track its retry also got broken comes at CD quality
    # rather than missing.
    # The crash-safe lane's completion proof holds every track to the chosen
    # quality, so only the standard path does this.
    cd_quality = []
    tier = quality if quality is not None else cfg.STREAMRIP_QUALITY
    if (broken and tier > 2 and recovery_owner is None and not rate_limited
            and not is_cancel_requested()):
        for rejected, t in _pair_files_to_tracks(
            list(broken), attempted_tracks, file_identities, qobuz_tracks
        ):
            if rate_limited:
                break
            if id(t) not in broken_twice:
                continue
            _cool_down(last_out)
            if is_cancel_requested():
                break
            rc, out, cd_audio = _rip_track(t, tier=2)
            if detect_auth_lost(out):
                raise AuthLost("rip output contained auth-lost markers")
            if detect_disk_full(out):
                raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
            rate_limited = rate_limited or detect_rate_limited(out)
            last_out = out
            cd_identities = _single_track_rip_identities(cd_audio, t, qobuz_tracks)
            cd_kept, cd_lossy, cd_broken = _sort_audio(cd_audio)
            resolved_rejects.extend(cd_lossy + cd_broken)
            matches = _pair_files_to_tracks(cd_kept, [t], cd_identities, qobuz_tracks)
            if not matches or not _remove_reject(broken, rejected):
                continue
            resolved_rejects.append(rejected)
            kept.append(matches[0][0])
            file_identities.update(cd_identities)
            retried_clean_targets.add(id(t))
            cd_quality.append(matches[0][0])
        if cd_quality:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Qobuz sent a broken file at this quality for "
                f"{plural(len(cd_quality), 'track')}, so "
                f"{'it' if len(cd_quality) == 1 else 'they'} came at CD quality:"))
            for path in cd_quality:
                log.info(fmt(C.GRAY, f"     {_reject_label(path)}"))
            # Upgrade would only fetch the same broken file again.
            mark_album_capped(album_id, album, {
                "n_below": len(cd_quality), "n_at": 0, "n_above": 0})

    # Reconcile completeness by unique Qobuz track identity, never by the raw
    # number of audio names that appeared.
    lossy_tracks = lossy + broken
    clean_pairs = _pair_files_to_tracks(kept, attempted_tracks, file_identities, qobuz_tracks)
    clean_target_ids = {id(track) for _, track in clean_pairs}
    clean_paths = {Path(path) for path, _ in clean_pairs}
    unmatched_clean = [path for path in kept if Path(path) not in clean_paths]
    rejected_pairs = _pair_files_to_tracks(
        lossy_tracks, attempted_tracks, file_identities, qobuz_tracks
    )
    rejected_target_ids = {id(track) for _, track in rejected_pairs} - clean_target_ids
    broken_target_ids = {
        id(track) for path, track in rejected_pairs if path in broken
    } - clean_target_ids
    expected_target_ids = {id(track) for track in attempted_tracks}
    missing_target_ids = expected_target_ids - clean_target_ids - rejected_target_ids
    unavailable = [
        track for track in attempted_tracks
        if id(track) in missing_target_ids and unstreamable(track)
    ]
    failed_target_ids = missing_target_ids - {id(track) for track in unavailable}
    n_ok = len(clean_target_ids)
    n_lossy = len(rejected_target_ids)
    n_fail = max(len(failed_target_ids), 1 if unmatched_clean else 0) + len(unavailable)
    failed_tracks = [
        track.get("title") or "?" for track in attempted_tracks if id(track) in failed_target_ids
    ]

    if not download_full_album and failed_track_objs:
        failed_ids = {id(track) for track in failed_track_objs}
        landed_despite_error = (clean_target_ids & failed_ids) - retried_clean_targets
        if landed_despite_error:
            log.info(
                fmt(
                    C.GRAY,
                    f"  · {plural(len(landed_despite_error), 'track')} landed despite a streamrip "
                    f"post-processing error - counting as success.",
                )
            )

    if download_full_album and full_album_rc is not None:
        # A full-album rip re-downloads the WHOLE album URL (all
        # n_tracks_total tracks), including the already-present ones we moved
        # to the gap-fill backup - so n_ok (every clean FLAC that landed) is
        # counted against the total, NOT len(missing).
        if (n_fail == 0 and full_album_rc != 0 and n_ok > 0
                and not is_cancel_requested()):
            log.info(
                fmt(
                    C.GRAY,
                    f"  · {plural(n_ok, 'track')} landed despite rip exit "
                    f"{full_album_rc} (streamrip post-processing error).",
                )
            )

    if lossy:
        log.info(
            fmt(
                C.YELLOW,
                f"  ⚠  {plural(len(lossy), 'track')} only available lossy on Qobuz "
                f"(no lossless for your tier - another source needed):",
            )
        )
        for d in lossy[:5]:
            log.info(fmt(C.GRAY, f"     {_reject_label(d)}"))
        if len(lossy) > 5:
            log.info(fmt(C.GRAY, f"     and {len(lossy) - 5} more"))
    if broken:
        if is_cancel_requested():
            note = f"The stop cut {plural(len(broken), 'track')} short:"
        else:
            note = ("Qobuz sent a broken file at this quality for "
                    f"{plural(len(broken), 'track')}, discarded:")
        log.info(fmt(C.YELLOW, f"  ⚠  {note}"))
        for d in broken[:5]:
            log.info(fmt(C.GRAY, f"     {_reject_label(d)}"))
        if len(broken) > 5:
            log.info(fmt(C.GRAY, f"     and {len(broken) - 5} more"))

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
        key = _reject_key(rejected)
        if key in seen_rejects or key in cut_short:
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
                f"  ⚠  {plural(retained_rejects, 'rejected staging file')} changed or "
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
            "n_broken": len(broken_target_ids),
            "n_unavailable": len(unavailable),
            "failed_tracks": failed_tracks,
            "unavailable_tracks": [track.get("title") or "?" for track in unavailable],
            "lossy_tracks": lossy_track_labels,
            "broken_tracks": broken_track_labels,
            "cd_quality_tracks": [_reject_label(path) for path in cd_quality],
            "_cd_quality_paths": [str(path) for path in cd_quality],
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
