"""Track which Library scan and saved views are current."""

from __future__ import annotations

import copy
import threading
import time

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.ui_cli import logging as cli_logging

STATE_VERSION = 1
SURFACES = ("library", "upgrade", "downsample", "new_releases")
_lock = threading.RLock()
_UNPUBLISHED_LIBRARY_REASON = (
    "The latest Library scan stopped before this review was saved."
)
_INTERRUPTED_PUBLICATION_MESSAGE = (
    "The Library scan finished checking the catalogue, but stopped before "
    "its review was saved."
)


def _empty_output():
    return {
        "generation": 0,
        "revision": 0,
        "status": "missing",
        "complete": False,
        "limited": False,
        "policy_signature": "",
        "reason": "",
    }


def _empty_state():
    return {
        "version": STATE_VERSION,
        "revision": 0,
        "generation": 0,
        "catalog_complete": False,
        "updated_at": None,
        "latest_attempt": {
            "id": 0,
            "status": "never",
            "at": None,
            "message": "",
        },
        "outputs": {surface: _empty_output() for surface in SURFACES},
        "pending_review_removals": [],
    }


def _normalise_output(value):
    base = _empty_output()
    if not isinstance(value, dict):
        return base
    status = str(value.get("status") or "missing")
    if status not in {
        "missing", "needs_refresh", "current", "stale", "disabled"
    }:
        status = "stale"
    try:
        generation = max(0, int(value.get("generation") or 0))
        revision = max(0, int(value.get("revision") or 0))
    except (TypeError, ValueError, OverflowError):
        generation = revision = 0
        status = "stale"
    base.update({
        "generation": generation,
        "revision": revision,
        "status": status,
        "complete": bool(value.get("complete")),
        "limited": bool(value.get("limited")),
        "policy_signature": str(value.get("policy_signature") or ""),
        "reason": str(value.get("reason") or ""),
    })
    return base


def _normalise_removals(value):
    if not isinstance(value, list):
        return []
    cleaned = []
    for entry in value:
        album_id = str(entry or "").strip()
        if album_id and album_id not in cleaned:
            cleaned.append(album_id)
    return cleaned


def load():
    data = state_file.load_json_object(
        cfg.LIBRARY_GENERATION_STATE_FILE,
        "the Library generation record",
        "which saved Library-derived views are current",
    )
    if data is None or data.get("version") != STATE_VERSION:
        return _empty_state()
    base = _empty_state()
    try:
        revision = max(0, int(data.get("revision") or 0))
        generation = max(0, int(data.get("generation") or 0))
    except (TypeError, ValueError, OverflowError):
        return base
    attempt = data.get("latest_attempt")
    if not isinstance(attempt, dict):
        attempt = {}
    try:
        attempt_id = max(0, int(attempt.get("id") or 0))
    except (TypeError, ValueError, OverflowError):
        attempt_id = 0
    attempt_status = str(attempt.get("status") or "never")
    if attempt_status not in {
        "never", "running", "complete", "failed", "cancelled", "incomplete"
    }:
        attempt_status = "failed"
    outputs = data.get("outputs")
    if not isinstance(outputs, dict):
        outputs = {}
    base.update({
        "revision": revision,
        "generation": generation,
        "catalog_complete": bool(data.get("catalog_complete")),
        "updated_at": data.get("updated_at"),
        "latest_attempt": {
            "id": attempt_id,
            "status": attempt_status,
            "at": attempt.get("at"),
            "message": str(attempt.get("message") or ""),
        },
        "outputs": {
            surface: _normalise_output(outputs.get(surface))
            for surface in SURFACES
        },
        "pending_review_removals": _normalise_removals(
            data.get("pending_review_removals")
        ),
    })
    return base


def _write(data) -> bool:
    try:
        state_file.write_json(cfg.LIBRARY_GENERATION_STATE_FILE, data)
        return True
    except OSError as exc:
        cli_logging.vlog(f"Library generation state write failed ({exc})")
        return False


def pending_review_removals(state=None) -> list[str]:
    """Albums the app downloaded that an open review may still be listing."""
    data = load() if state is None else state
    return list(data.get("pending_review_removals") or [])


def note_review_removal(album_id) -> bool:
    """Record an album taken off the saved missing list for a review that this
    process cannot reach.

    A terminal run holds no web job, so it cannot edit the living review the
    web process keeps in memory. The web applies these the next time it builds
    the Library page and clears them again; nothing else reads them, and the
    list stays as short as the number of downloads since that page was open.
    """
    album_id = str(album_id or "").strip()
    if not album_id:
        return False
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        pending = [
            entry for entry in data["pending_review_removals"]
            if entry != album_id
        ]
        pending.append(album_id)
        data["pending_review_removals"] = pending
        return _write(data)


def clear_review_removals(album_ids) -> bool:
    """Forget removals no open review carries any more."""
    done = {str(entry or "").strip() for entry in album_ids}
    done.discard("")
    if not done:
        return True
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        pending = [
            entry for entry in data["pending_review_removals"]
            if entry not in done
        ]
        if pending == data["pending_review_removals"]:
            return True
        data["pending_review_removals"] = pending
        return _write(data)


def revision() -> int:
    return int(load().get("revision") or 0)


def current_generation() -> int:
    return int(load().get("generation") or 0)


def output_state(surface: str, state=None):
    if surface not in SURFACES:
        raise ValueError("unknown Library-derived surface")
    state = load() if state is None else state
    return _normalise_output((state.get("outputs") or {}).get(surface))


def output_is_current(surface: str, *, generation=None, state=None) -> bool:
    state = load() if state is None else state
    expected = int(
        state.get("generation") or 0
        if generation is None
        else generation
    )
    output = output_state(surface, state)
    return bool(
        output["status"] == "current"
        and output["complete"]
        and output["generation"] == expected
    )


def library_snapshot_available(state=None) -> bool:
    """Whether the current scan has a saved Library snapshot.

    Freshness is deliberately separate. A local change can make the snapshot
    stale without erasing the completed baseline or its saved review.
    """
    state = load() if state is None else state
    from qobuz_librarian.library import library_scan_state

    snapshot = library_scan_state.kind_state("missing")
    output = output_state("library", state)
    return bool(
        int(state.get("generation") or 0) > 0
        and state.get("catalog_complete")
        and snapshot.get("complete")
        and int(snapshot.get("generation") or 0)
        == int(state.get("generation") or 0)
        and int(snapshot.get("revision") or 0)
        == int(output.get("revision") or 0)
    )


def baseline_complete(state=None) -> bool:
    state = load() if state is None else state
    return bool(
        library_snapshot_available(state)
        and output_is_current("library", state=state)
    )


def library_publication_incomplete(state=None) -> bool:
    """Detect a restart after the catalogue commit but before the review save.

    A catalogue generation is committed before its Library snapshot so a
    snapshot can never claim authority from an older crawl. If the process is
    interrupted between those writes, the attempt used to look complete even
    though its main view was never saved. The zero revision and original reason
    distinguish that window from a later local change.
    """
    state = load() if state is None else state
    latest = state.get("latest_attempt") or {}
    output = output_state("library", state)
    generation = int(state.get("generation") or 0)
    return bool(
        generation > 0
        and state.get("catalog_complete")
        and str(latest.get("status") or "") == "complete"
        and int(output.get("generation") or 0) == generation
        and int(output.get("revision") or 0) == 0
        and output.get("status") == "needs_refresh"
        and not output.get("complete")
        and output.get("reason") == _UNPUBLISHED_LIBRARY_REASON
    )


def reconcile_interrupted_library_publication(authority) -> bool | None:
    """Mark a catalogue commit without its saved review as incomplete.

    Callers must hold the process run lock. ``True`` means the interrupted
    state was repaired, ``False`` means there was nothing to repair, and
    ``None`` means the required state write failed.
    """
    intact = getattr(authority, "intact", None)
    if not callable(intact) or intact() is not True:
        raise ValueError("exact run-lock authority is required")
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        if not library_publication_incomplete(data):
            return False
        latest = data.get("latest_attempt") or {}
        _next_revision(data)
        data["latest_attempt"] = {
            "id": int(latest.get("id") or 0),
            "status": "incomplete",
            "at": time.time(),
            "message": _INTERRUPTED_PUBLICATION_MESSAGE,
        }
        return True if _write(data) else None


def _next_revision(data) -> int:
    value = int(data.get("revision") or 0) + 1
    data["revision"] = value
    data["updated_at"] = time.time()
    data["version"] = STATE_VERSION
    return value


def reserve_revision() -> int | None:
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        value = _next_revision(data)
        return value if _write(data) else None


def begin_attempt() -> int | None:
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        attempt_id = _next_revision(data)
        data["latest_attempt"] = {
            "id": attempt_id,
            "status": "running",
            "at": time.time(),
            "message": "",
        }
        return attempt_id if _write(data) else None


def finish_attempt(attempt_id, status: str, message: str = "") -> bool:
    if status not in {"failed", "cancelled", "incomplete"}:
        raise ValueError("invalid Library attempt status")
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        latest = data.get("latest_attempt") or {}
        if attempt_id is not None and int(latest.get("id") or 0) != int(attempt_id):
            return False
        _next_revision(data)
        data["latest_attempt"] = {
            "id": int(attempt_id or latest.get("id") or 0),
            "status": status,
            "at": time.time(),
            "message": str(message or ""),
        }
        return _write(data)


def commit_catalog_generation(
    attempt_id,
    *,
    limited: bool = False,
    expected_revision: int | None = None,
):
    """Commit a completed crawl before replacing its saved views."""
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        latest = data.get("latest_attempt") or {}
        if attempt_id is not None and int(latest.get("id") or 0) != int(attempt_id):
            return None
        if (
            expected_revision is not None
            and int(data.get("revision") or 0) != int(expected_revision)
        ):
            # A local operation or targeted saved-view update landed after the
            # crawl began.  Publishing this older picture would resurrect the
            # candidate that operation just retired.
            return None
        previous = copy.deepcopy(data)
        generation = int(data.get("generation") or 0) + 1
        commit_revision = _next_revision(data)
        data["generation"] = generation
        data["catalog_complete"] = True
        data["latest_attempt"] = {
            "id": int(attempt_id or latest.get("id") or 0),
            "status": "complete",
            "at": time.time(),
            "message": "",
        }
        data["outputs"] = {
            surface: {
                **_empty_output(),
                "generation": generation,
                "status": "needs_refresh",
                "limited": bool(limited) if surface == "library" else False,
                "reason": _UNPUBLISHED_LIBRARY_REASON,
            }
            for surface in SURFACES
        }
        if not _write(data):
            return None
        return {
            "generation": generation,
            "revision": commit_revision,
            "attempt_id": int(attempt_id or latest.get("id") or 0),
            "previous": previous,
        }


def abort_catalog_generation(publication, message: str) -> bool:
    """Restore the prior complete generation after its main snapshot fails.

    Individual state files use atomic replacement, so a failed Library-state
    write leaves the prior snapshot on disk.  Restoring this small authority
    record makes that prior snapshot current again while keeping a durable
    failed-attempt warning.
    """
    if not isinstance(publication, dict):
        return False
    previous = publication.get("previous")
    if not isinstance(previous, dict):
        return False
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        current = load()
        if int(current.get("generation") or 0) != int(
            publication.get("generation") or 0
        ):
            return False
        restored = copy.deepcopy(previous)
        restored["revision"] = max(
            int(current.get("revision") or 0),
            int(restored.get("revision") or 0),
        ) + 1
        restored["updated_at"] = time.time()
        restored["version"] = STATE_VERSION
        restored["latest_attempt"] = {
            "id": int(publication.get("attempt_id") or 0),
            "status": "failed",
            "at": time.time(),
            "message": str(message or ""),
        }
        return _write(restored)


def mark_output_current(
    surface: str,
    *,
    generation: int,
    revision: int,
    complete: bool = True,
    limited: bool = False,
    policy_signature: str = "",
    preserve_noncurrent: bool = False,
) -> bool:
    """Save one output revision.

    A full scan may make the output current. A targeted artist or album merge
    may advance its exact saved revision, but it cannot prove that an unrelated
    stale or incomplete part of the output is current again.
    """
    if surface not in SURFACES:
        raise ValueError("unknown Library-derived surface")
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        if int(data.get("generation") or 0) != int(generation):
            return False
        previous = output_state(surface, data)
        if int(previous.get("revision") or 0) > int(revision):
            return False
        _next_revision(data)
        if (
            preserve_noncurrent
            and (
                previous.get("status") != "current"
                or previous.get("complete") is not True
            )
        ):
            output = {
                **previous,
                "generation": int(generation),
                "revision": int(revision),
            }
        else:
            output = {
                "generation": int(generation),
                "revision": int(revision),
                "status": "current" if complete else "needs_refresh",
                "complete": bool(complete),
                "limited": bool(limited),
                "policy_signature": str(policy_signature or ""),
                "reason": (
                    "" if complete
                    else "This view did not finish refreshing."
                ),
            }
        data.setdefault("outputs", {})[surface] = output
        return _write(data)


def mark_output_status(
    surface: str,
    status: str,
    *,
    generation: int | None = None,
    reason: str = "",
) -> bool:
    if surface not in SURFACES:
        raise ValueError("unknown Library-derived surface")
    if status not in {"needs_refresh", "stale", "disabled"}:
        raise ValueError("invalid Library-derived output status")
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        current = int(data.get("generation") or 0)
        if generation is not None and current != int(generation):
            return False
        previous = output_state(surface, data)
        _next_revision(data)
        data.setdefault("outputs", {})[surface] = {
            **previous,
            "generation": current,
            "status": status,
            "complete": False,
            "reason": str(reason or ""),
        }
        return _write(data)


def invalidate(surfaces, reason: str) -> bool:
    wanted = tuple(dict.fromkeys(surfaces))
    if any(surface not in SURFACES for surface in wanted):
        raise ValueError("unknown Library-derived surface")
    with _lock, state_file.store_lock(cfg.LIBRARY_GENERATION_STATE_FILE):
        data = load()
        current = int(data.get("generation") or 0)
        _next_revision(data)
        outputs = data.setdefault("outputs", {})
        for surface in wanted:
            outputs[surface] = {
                **output_state(surface, data),
                "generation": current,
                "status": "stale",
                "complete": False,
                "reason": str(reason or ""),
            }
        return _write(data)
