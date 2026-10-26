"""Saved whole-library scan snapshot for cheap post-baseline refreshes."""
import copy
import hashlib
import json
import re
import secrets
import threading
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import safefile, state_file
from qobuz_librarian.library import candidate_premise
from qobuz_librarian.ui_cli import logging as cli_logging

STATE_VERSION = 1
# Raised when the rules that decide which albums a scan offers change, so a
# refresh derives results saved under older rules again instead of reusing
# them for unchanged folders.
CANDIDATE_RULES = 6

# save_kind and mark_review_retired both read-modify-write the shared file;
# serialise them so a scan's periodic save and a review retire (discard /
# worked-through) can't clobber each other's field. Review reconstruction also
# holds this lock through publication, so retirement and resurrection have one
# ordering. It is reentrant because a guarded lifecycle transition calls the
# state helpers below.
_lock = threading.RLock()


def review_state_lock():
    """Return the shared review publication and retirement lock."""
    return _lock


def _empty_state():
    return {
        "version": STATE_VERSION,
        "updated_at": None,
        # When the parked Library review derived from this snapshot was
        # retired (discarded, or worked through to empty).
        "review_retired_at": 0.0,
        "review_retired_generation": 0,
        # Why the review retired: "discarded" (thrown away in one action) or
        # "worked_through" (dismissed/downloaded down to empty).
        "review_retired_reason": "",
        "kinds": {},
    }


def _empty_kind():
    return {
        "updated_at": None,
        "generation": 0,
        "revision": 0,
        "complete": False,
        "limited": False,
        "quality_signature": "",
        "artists": {},
    }


def quality_signature() -> str:
    """Every setting the saved candidates were computed under."""
    return (f"{getattr(cfg, 'STREAMRIP_QUALITY', '')}"
            f"|{bool(getattr(cfg, 'PREFER_HIRES', False))}"
            f"|{bool(getattr(cfg, 'SUPPRESS_SINGLE_TRACK_GAPS', False))}"
            f"|{getattr(cfg, 'ARTIST_CATALOG_LIMIT', '')}"
            f"|{getattr(cfg, 'MISSING_ALBUMS_MIN_TRACKS', '')}"
            f"|{bool(getattr(cfg, 'EXCLUDE_LIVE_ALBUMS', False))}"
            f"|rules{CANDIDATE_RULES}")


def _normalise(data):
    # A version the build doesn't know is a deliberate schema signal, not
    # corruption: leave the file alone and rebuild from a fresh scan.
    if data is None or data.get("version") != STATE_VERSION:
        return _empty_state()
    base = _empty_state()
    kinds = data.get("kinds") if isinstance(data.get("kinds"), dict) else {}
    updated_at, _ = state_file.optional_time(data.get("updated_at"))
    retired_at, _ = state_file.optional_time(data.get("review_retired_at"))
    retired_generation, _ = state_file.nonnegative_int(
        data.get("review_retired_generation")
    )
    base.update({
        "updated_at": updated_at,
        "review_retired_at": retired_at or 0.0,
        "review_retired_generation": retired_generation,
        "review_retired_reason": str(data.get("review_retired_reason") or ""),
        "kinds": kinds,
    })
    return base


# The header of the saved scan, beside it. Loading the whole file once per
# page costs more than a large library can spare, and a refresh that finds
# what is already saved changes only this header. The file keeps a full
# header of its own, so a build that knows nothing of this one still reads
# it; this one wins only while it names the stamp the file opens with.
_HEAD_VERSION = 1
_STAMP = re.compile(rb'\{"stamp": "([0-9a-f]{16})"')


def _head_path():
    path = Path(cfg.LIBRARY_SCAN_STATE_FILE)
    return path.with_name(path.stem + ".head" + path.suffix)


def _file_stamp():
    try:
        with open(cfg.LIBRARY_SCAN_STATE_FILE, "rb") as f:
            match = _STAMP.match(f.read(64))
    except FileNotFoundError:
        return None
    return match.group(1).decode() if match else None


def _read_head():
    """The header file, or None when it does not read as one, and its bytes,
    None when there is no file. Without it the file is read whole, and its own
    header may be older than refreshes that changed no rows."""
    try:
        raw = _head_path().read_bytes()
        head = json.loads(raw)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        return None, b""
    if (
        not isinstance(head, dict)
        or head.get("version") != _HEAD_VERSION
        or not isinstance(head.get("stamp"), str)
        or not isinstance(head.get("summary"), dict)
        or not isinstance(head.get("digests"), dict)
    ):
        return None, raw
    return head, raw


def _head_summary(head):
    return _summarise(_normalise({**head["summary"], "version": STATE_VERSION}))


def _current_head():
    """The header file when it belongs to the saved file now on disk, or
    None, and the header file's bytes either way."""
    head, raw = _read_head()
    if head is not None and head["stamp"] != _file_stamp():
        head = None
    return head, raw


def _with_head(data, head):
    summary = head["summary"]
    headers = summary.get("kinds") if isinstance(summary.get("kinds"), dict) else {}
    kinds = data.get("kinds") if isinstance(data.get("kinds"), dict) else {}
    return {
        **data,
        **{key: summary.get(key) for key in (
            "updated_at", "review_retired_at", "review_retired_generation",
            "review_retired_reason")},
        "kinds": {
            name: (
                {**bucket, **headers[name]}
                if isinstance(bucket, dict) and isinstance(headers.get(name), dict)
                else bucket
            )
            for name, bucket in kinds.items()
        },
    }


def load():
    # The header first: one written after this file names its stamp.
    head, _raw = _read_head()
    data = state_file.load_json_object(
        cfg.LIBRARY_SCAN_STATE_FILE, "the saved library scan",
        "your parked Library review (it would need a full rescan)")
    if data is not None and head is not None and data.get("stamp") == head["stamp"]:
        data = _with_head(data, head)
    return _normalise(data)


def _kind_from(data, kind, *, keep_artists=True):
    base = _empty_kind()
    bucket = (data.get("kinds") or {}).get(kind)
    if isinstance(bucket, dict):
        updated_at, updated_ok = state_file.optional_time(bucket.get("updated_at"))
        generation, generation_ok = state_file.nonnegative_int(bucket.get("generation"))
        revision, revision_ok = state_file.nonnegative_int(bucket.get("revision"))
        raw_artists = bucket.get("artists")
        artists_ok = raw_artists is None or isinstance(raw_artists, dict)
        artists = {}
        for name, entry in (raw_artists or {}).items() if artists_ok else ():
            if not isinstance(entry, dict):
                artists_ok = False
                continue
            cleaned = _clean_artist_state(entry)
            raw_candidates = entry.get("candidates")
            if (
                raw_candidates is not None
                and (
                    not isinstance(raw_candidates, list)
                    or len(cleaned["candidates"]) != len(raw_candidates)
                )
            ):
                artists_ok = False
            if keep_artists:
                artists[str(name)] = cleaned
        base.update({
            "updated_at": updated_at,
            "generation": generation,
            "revision": revision,
            "complete": bool(bucket.get("complete")) and all((
                updated_ok, generation_ok, revision_ok, artists_ok,
            )),
            "limited": bool(bucket.get("limited")),
            "quality_signature": str(bucket.get("quality_signature") or ""),
            "artists": artists,
        })
    if not keep_artists:
        del base["artists"]
    return base


def kind_state(kind: str, data=None):
    """One kind of the saved snapshot, from ``data`` when the caller already
    holds load()."""
    return _kind_from(load() if data is None else data, kind)


# A page and its poll need only each kind's header, which a large library
# buries under every saved candidate.
_summary_lock = threading.Lock()
_summary_read_lock = threading.Lock()
_summary_cache = None


def _summarise(data):
    return {
        "updated_at": data["updated_at"],
        "review_retired_at": data["review_retired_at"],
        "review_retired_generation": data["review_retired_generation"],
        "review_retired_reason": data["review_retired_reason"],
        "kinds": {
            str(kind): _kind_from(data, kind, keep_artists=False)
            for kind in data["kinds"]
        },
    }


def _identities():
    return (state_file.file_identity(cfg.LIBRARY_SCAN_STATE_FILE),
            state_file.file_identity(_head_path()))


def _remember_summary(identity, value):
    global _summary_cache
    with _summary_lock:
        _summary_cache = (
            None if value is None
            else (str(cfg.LIBRARY_SCAN_STATE_FILE), identity, value))


def _cached_summary(identity):
    with _summary_lock:
        cached = _summary_cache
    if cached is not None and cached[:2] == (
            str(cfg.LIBRARY_SCAN_STATE_FILE), identity):
        return copy.deepcopy(cached[2])
    return None


def summary():
    """load() without the artists of any kind."""
    value = _cached_summary(_identities())
    if value is not None:
        return value
    # One read at a time: a page and its poll arriving together would
    # otherwise each hold a whole copy of a file with no header beside it.
    with _summary_read_lock:
        identity = _identities()
        value = _cached_summary(identity)
        if value is not None:
            return value
        head, _raw = _current_head()
        value = _head_summary(head) if head is not None else _summarise(load())
        # Only files that stayed put while they were read can stand for them.
        if _identities() == identity:
            _remember_summary(identity, value)
    return copy.deepcopy(value)


def kind_summary(kind: str):
    """kind_state() without the artists."""
    header = summary()["kinds"].get(kind)
    if header is None:
        header = _empty_kind()
        del header["artists"]
    return header


def _encoded(data, stamp, digests):
    """The saved file's text, opening with ``stamp``, filling ``digests``
    with each kind's artists as written."""
    encoder = json.JSONEncoder()
    top = {"stamp": stamp}
    top.update((key, value) for key, value in data.items()
               if key not in ("stamp", "kinds"))
    yield encoder.encode(top)[:-1] + ', "kinds": {'
    for index, (kind, bucket) in enumerate(data["kinds"].items()):
        yield (", " if index else "") + encoder.encode(str(kind)) + ": "
        if not isinstance(bucket, dict) or "artists" not in bucket:
            yield encoder.encode(bucket)
            continue
        fields = encoder.encode(
            {key: value for key, value in bucket.items() if key != "artists"})
        yield fields[:-1] + (', "artists": ' if len(fields) > 2 else '"artists": ')
        digest = hashlib.sha256()
        for piece in safefile.json_chunks(encoder, bucket["artists"], depth=1):
            digest.update(piece.encode())
            yield piece
        digests[str(kind)] = digest.hexdigest()
        yield "}"
    yield "}}"


def _artists_digest(artists):
    digest = hashlib.sha256()
    for piece in safefile.json_chunks(json.JSONEncoder(), artists, depth=1):
        digest.update(piece.encode())
    return digest.hexdigest()


def _write_head(stamp, value, digests):
    try:
        return state_file.write_json(_head_path(), {
            "version": _HEAD_VERSION, "stamp": stamp, "digests": digests,
            "summary": value,
        }, indent=None)
    except OSError as e:
        cli_logging.vlog(f"library scan state header write failed ({e})")
        return None


def _write_state(data):
    stamp = secrets.token_hex(8)
    digests = {}
    try:
        identity = state_file.write_chunks(
            cfg.LIBRARY_SCAN_STATE_FILE, _encoded(data, stamp, digests))
    except OSError as e:
        # Losing this file only costs a slower next scan, but say so (verbose)
        # rather than going stale with zero signal on a full/read-only volume.
        cli_logging.vlog(f"library scan state write failed ({e}); next scan re-crawls")
        return False
    value = _summarise(_normalise(data))
    head_identity = _write_head(stamp, value, digests)
    # Without its header the file stands alone, which it can.
    _remember_summary((identity, head_identity),
                      value if head_identity is not None else None)
    return True


def _write_header(data, head):
    """Save ``data``, a summary or a whole load(), whose artists are the
    saved file's own, rewriting only the header when one belongs to it."""
    if head is None:
        return _write_state(data)
    value = _summarise(_normalise({**data, "version": STATE_VERSION}))
    head_identity = _write_head(head["stamp"], value, head["digests"])
    if head_identity is None:
        return False
    _remember_summary(
        (state_file.file_identity(cfg.LIBRARY_SCAN_STATE_FILE), head_identity),
        value)
    return True


def _clean_artist_state(entry):
    entry = candidate_premise.restore_artist(entry)
    if not isinstance(entry, dict):
        entry = {}
    candidates = entry.get("candidates")
    catalog_ids = entry.get("catalog_ids")
    return {
        "fingerprint": str(entry.get("fingerprint") or ""),
        "candidates": (
            [candidate for candidate in candidates
             if isinstance(candidate, dict)
             and (
                 candidate.get("payload") is None
                 or isinstance(candidate.get("payload"), dict)
             )]
            if isinstance(candidates, list) else []
        ),
        "artist_id": entry.get("artist_id") or "",
        "catalog_ids": (list(catalog_ids) if isinstance(catalog_ids, list)
                        else None),
        "owned_ids": (
            {str(k): str(v) for k, v in entry["owned_ids"].items()}
            if isinstance(entry.get("owned_ids"), dict) else {}),
    }


def save_kind(kind: str, *, artists: dict, complete: bool,
              quality_sig: str = "",
              generation: int = 0, revision: int = 0,
              limited: bool = False):
    with _lock, state_file.store_lock(cfg.LIBRARY_SCAN_STATE_FILE):
        # The replaced file stays readable through this handle, so a failed
        # publication can put it back without a copy held in memory.
        try:
            previous = open(cfg.LIBRARY_SCAN_STATE_FILE, "rb")
        except FileNotFoundError:
            previous = None
        try:
            return _save_kind_locked(
                kind, previous, artists=artists, complete=complete,
                quality_sig=quality_sig, generation=generation,
                revision=revision, limited=limited)
        finally:
            if previous is not None:
                previous.close()


def _restore(previous, head_raw, *, whole):
    """Put back the saved file read through ``previous`` and the header
    file's bytes ``head_raw``."""
    try:
        if whole and previous is None:
            _write_state(_normalise(None))
            return
        if whole:
            previous.seek(0)
            state_file.write_chunks(
                cfg.LIBRARY_SCAN_STATE_FILE,
                iter(lambda: previous.read(1 << 20), b""))
        if head_raw is None:
            _head_path().unlink(missing_ok=True)
        else:
            state_file.write_chunks(_head_path(), (head_raw,))
    except OSError as e:
        cli_logging.vlog(f"library scan state restore failed ({e})")
    finally:
        _remember_summary(None, None)


def _save_kind_locked(kind, previous, *, artists, complete, quality_sig,
                      generation, revision, limited):
    head, head_raw = _current_head()
    # Read before the rows below are built, so the two are not held at once.
    header = _head_summary(head) if head is not None else summary()
    now = time.time()
    bucket = {
        "updated_at": now,
        "generation": int(generation or 0),
        "revision": int(revision or 0),
        "complete": bool(complete),
        "limited": bool(limited),
        "quality_signature": str(quality_sig or ""),
        # In name order, so the same rows always read the same.
        "artists": {
            str(name): candidate_premise.compact_artist(_clean_artist_state(entry))
            for name, entry in sorted(
                (artists or {}).items(), key=lambda item: str(item[0]))
        },
    }
    if (
        head is not None
        and kind in (head["summary"].get("kinds") or {})
        and head["digests"].get(kind) == _artists_digest(bucket["artists"])
    ):
        # The saved file already holds these rows: only the header moves.
        data = header
        data["kinds"][kind] = _kind_from(
            {"kinds": {kind: bucket}}, kind, keep_artists=False)
        whole = False
    else:
        # Only another kind's saved rows need the whole file read.
        data = dict(load() if set(header["kinds"]) - {kind} else header)
        data["kinds"] = {
            name: value for name, value in data["kinds"].items()
            if name != kind
        }
        data["kinds"][kind] = bucket
        head = None
        whole = True
    data["updated_at"] = now
    data["version"] = STATE_VERSION
    if not _write_header(data, head):
        return None
    if generation and revision:
        from qobuz_librarian.library import generation_state

        if not generation_state.mark_output_current(
            "library",
            generation=generation,
            revision=revision,
            complete=complete,
            limited=limited,
            policy_signature=quality_sig,
        ):
            # The snapshot write landed but its authority record did not.
            # No other snapshot writer can enter until the prior file is
            # restored.
            _restore(previous, head_raw, whole=whole)
            return None
    return int(generation) if generation else now


def mark_review_retired(
    now=None,
    reason: str = "",
    generation: float | None = None,
) -> bool:
    """Record that the parked Library review from the current snapshot was
    retired. ``reason`` is "discarded" (thrown away) or "worked_through"
    (dismissed/downloaded to empty), driving the Library page's copy. The
    saved-state review reconstruction won't rebuild a review from a snapshot
    whose missing kind was last saved at or before this, so a finished review
    doesn't come back; a fresh missing scan clears the block by construction."""
    # The whole file, so a build that reads only the file agrees.
    with _lock, state_file.store_lock(cfg.LIBRARY_SCAN_STATE_FILE):
        data = load()
        if generation is not None:
            try:
                expected = int(generation)
                current = int(
                    ((data.get("kinds") or {}).get("missing") or {}).get(
                        "generation"
                    )
                    or 0
                )
            except (TypeError, ValueError):
                expected = current = 0
            if not expected or current != expected:
                # This review belongs to an older snapshot. Retiring it must
                # not suppress results published by a newer full scan.
                return True
        data["review_retired_at"] = float(time.time() if now is None else now)
        current_generation = int(
            ((data.get("kinds") or {}).get("missing") or {}).get(
                "generation"
            )
            or 0
        )
        data["review_retired_generation"] = current_generation
        if reason:
            data["review_retired_reason"] = reason
        data["version"] = STATE_VERSION
        return _write_state(data)


def clear_review_retired() -> bool:
    """Lift a review retirement so the saved-state review rebuilds again, used
    when the user brings dismissed results back from the finished Library page.
    Returns True if a retirement was actually cleared (there was one to lift)."""
    with _lock, state_file.store_lock(cfg.LIBRARY_SCAN_STATE_FILE):
        data = load()
        if not float(data.get("review_retired_at") or 0.0):
            return False
        data["review_retired_at"] = 0.0
        data["review_retired_generation"] = 0
        data["review_retired_reason"] = ""
        data["version"] = STATE_VERSION
        return _write_state(data)


def _drop_albums(entry, album_ids) -> bool:
    candidates = [
        candidate
        for candidate in entry["candidates"]
        if str((candidate.get("payload") or {}).get("album_id") or "")
        not in album_ids
    ]
    if len(candidates) == len(entry["candidates"]):
        return False
    entry["candidates"] = candidates
    # These rows no longer describe the folder they were found in, so a
    # refresh derives the artist again even when the folder reads as it did
    # before, as after an Undo.
    entry["fingerprint"] = ""
    return True


def without_albums(artists: dict, album_ids) -> dict:
    """A kind's saved ``artists`` without the exact Qobuz albums in
    ``album_ids``, ready for ``save_kind``."""
    album_ids = {str(album_id) for album_id in album_ids or ()}
    if not album_ids:
        return artists
    kept = {}
    for name, entry in artists.items():
        entry = {**entry, "candidates": list(entry.get("candidates") or [])}
        _drop_albums(entry, album_ids)
        kept[name] = entry
    return kept


def forget_fingerprints(artist_names) -> bool:
    """Make the next refresh derive these artist folders again.

    A saved row's seal covers more of its folder than the fingerprint a
    refresh compares, so a row can be refused as changed while the folder
    still reads as unchanged and the refresh would put the same row back.
    """
    names = {str(name) for name in artist_names or () if name}
    if not names:
        return True
    with _lock, state_file.store_lock(cfg.LIBRARY_SCAN_STATE_FILE):
        data = load()
        changed = False
        for bucket in data["kinds"].values():
            artists = bucket.get("artists") if isinstance(bucket, dict) else None
            for name in names & set(artists or ()):
                entry = artists[name]
                if isinstance(entry, dict) and entry.get("fingerprint"):
                    entry["fingerprint"] = ""
                    changed = True
        return _write_state(data) if changed else True


def remove_album(album_id) -> bool:
    """Remove one exact Qobuz album from the saved living Library snapshot.

    The open review is a separate copy, held in memory by whichever process is
    serving the web UI, so a terminal download cannot edit it directly. The
    removal is recorded for that process to apply, which keeps the snapshot and
    the review from disagreeing about an album already on disk.
    """
    album_id = str(album_id or "").strip()
    if not album_id:
        return False
    from qobuz_librarian.library import generation_state

    with _lock, state_file.store_lock(cfg.LIBRARY_SCAN_STATE_FILE):
        generation_state.note_album_change(album_id)
        data = load()
        bucket = ((data.get("kinds") or {}).get("missing") or {})
        generation = int(bucket.get("generation") or 0)
        if not generation or generation != generation_state.current_generation():
            return False
        artists = bucket.get("artists") or {}
        changed = False
        rebuilt = {}
        for name, entry in artists.items():
            cleaned = _clean_artist_state(entry)
            changed = _drop_albums(cleaned, {album_id}) or changed
            rebuilt[name] = candidate_premise.compact_artist(cleaned)
        if not changed:
            return generation_state.note_review_removal(album_id)
        revision = generation_state.reserve_revision()
        if revision is None:
            return False
        now = time.time()
        bucket = {
            **bucket,
            "updated_at": now,
            "revision": revision,
            "artists": rebuilt,
        }
        data.setdefault("kinds", {})["missing"] = bucket
        data["updated_at"] = now
        if not _write_state(data):
            return False
        return generation_state.mark_output_current(
            "library",
            generation=generation,
            revision=revision,
            complete=bool(bucket.get("complete")),
            limited=bool(bucket.get("limited")),
            policy_signature=str(bucket.get("quality_signature") or ""),
            preserve_noncurrent=True,
        ) and generation_state.note_review_removal(album_id)
