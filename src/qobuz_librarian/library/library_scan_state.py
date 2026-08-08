"""Saved whole-library scan snapshot for cheap post-baseline refreshes."""
import hashlib
import json
import threading
import time

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file

STATE_VERSION = 1

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
        # Why the review retired: "discarded" (thrown away in one action) or
        # "worked_through" (dismissed/downloaded down to empty).
        "review_retired_reason": "",
        "kinds": {},
    }


def _empty_kind():
    return {
        "updated_at": None,
        "complete": False,
        "hidden_signature": "",
        "quality_signature": "",
        "artists": {},
    }


def quality_signature() -> str:
    """Every setting the saved candidates were computed under. When it differs
    from the current settings, a refresh must re-derive candidates even for
    unchanged folders, the cheap skip would otherwise carry forward promises
    made under a dead policy. Not just the quality pair: single-track-gap
    suppression, the catalogue limit, the missing-album track minimum, and
    live-album exclusion all change WHICH candidates a scan yields, so leaving
    any of them out keeps stale gap/missing lists after Settings says the
    policy changed."""
    return (f"{getattr(cfg, 'STREAMRIP_QUALITY', '')}"
            f"|{bool(getattr(cfg, 'PREFER_HIRES', False))}"
            f"|{bool(getattr(cfg, 'SUPPRESS_SINGLE_TRACK_GAPS', False))}"
            f"|{getattr(cfg, 'ARTIST_CATALOG_LIMIT', '')}"
            f"|{getattr(cfg, 'MISSING_ALBUMS_MIN_TRACKS', '')}"
            f"|{bool(getattr(cfg, 'EXCLUDE_LIVE_ALBUMS', False))}")


def hidden_signature(store, scope: str) -> str:
    bucket = (store or {}).get(scope) if isinstance(store, dict) else {}
    if not isinstance(bucket, dict):
        bucket = {}
    raw = json.dumps(bucket, sort_keys=True, ensure_ascii=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load():
    data = state_file.load_json_object(
        cfg.LIBRARY_SCAN_STATE_FILE, "the saved library scan",
        "your parked Library review (it would need a full rescan)")
    # A version the build doesn't know is a deliberate schema signal, not
    # corruption: leave the file alone and rebuild from a fresh scan.
    if data is None or data.get("version") != STATE_VERSION:
        return _empty_state()
    base = _empty_state()
    kinds = data.get("kinds") if isinstance(data.get("kinds"), dict) else {}
    base.update({
        "updated_at": data.get("updated_at"),
        "review_retired_at": float(data.get("review_retired_at") or 0.0),
        "review_retired_reason": str(data.get("review_retired_reason") or ""),
        "kinds": kinds,
    })
    return base


def kind_state(kind: str):
    data = load()
    bucket = (data.get("kinds") or {}).get(kind)
    if not isinstance(bucket, dict):
        return _empty_kind()
    base = _empty_kind()
    artists = bucket.get("artists") if isinstance(bucket.get("artists"), dict) else {}
    base.update({
        "updated_at": bucket.get("updated_at"),
        "complete": bool(bucket.get("complete")),
        "hidden_signature": str(bucket.get("hidden_signature") or ""),
        "quality_signature": str(bucket.get("quality_signature") or ""),
        "artists": artists,
    })
    return base


def _write_state(data):
    try:
        state_file.write_json(cfg.LIBRARY_SCAN_STATE_FILE, data)
        return True
    except OSError as e:
        # Losing this file only costs a slower next scan, but say so (verbose)
        # rather than going stale with zero signal on a full/read-only volume.
        from qobuz_librarian.ui_cli.logging import vlog
        vlog(f"library scan state write failed ({e}); next scan re-crawls")
        return False


def _clean_artist_state(entry):
    if not isinstance(entry, dict):
        entry = {}
    candidates = entry.get("candidates")
    catalog_ids = entry.get("catalog_ids")
    return {
        "fingerprint": str(entry.get("fingerprint") or ""),
        "candidates": candidates if isinstance(candidates, list) else [],
        "artist_id": entry.get("artist_id") or "",
        "catalog_ids": (list(catalog_ids) if isinstance(catalog_ids, list)
                        else None),
    }


def save_kind(kind: str, *, artists: dict, complete: bool,
              hidden_signature: str = "", quality_sig: str = ""):
    with _lock:
        data = load()
        kinds = data.setdefault("kinds", {})
        now = time.time()
        kinds[kind] = {
            "updated_at": now,
            "complete": bool(complete),
            "hidden_signature": str(hidden_signature or ""),
            "quality_signature": str(quality_sig or ""),
            "artists": {
                str(name): _clean_artist_state(entry)
                for name, entry in (artists or {}).items()
            },
        }
        data["updated_at"] = now
        data["version"] = STATE_VERSION
        return now if _write_state(data) else None


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
    with _lock:
        data = load()
        if generation is not None:
            try:
                expected = float(generation)
                current = float(
                    ((data.get("kinds") or {}).get("missing") or {}).get(
                        "updated_at"
                    )
                    or 0.0
                )
            except (TypeError, ValueError):
                expected = current = 0.0
            if not expected or current != expected:
                # This review belongs to an older snapshot. Retiring it must
                # not suppress results published by a newer full scan.
                return True
        data["review_retired_at"] = float(time.time() if now is None else now)
        if reason:
            data["review_retired_reason"] = reason
        data["version"] = STATE_VERSION
        return _write_state(data)


def clear_review_retired() -> bool:
    """Lift a review retirement so the saved-state review rebuilds again, used
    when the user brings dismissed results back from the finished Library page.
    Returns True if a retirement was actually cleared (there was one to lift)."""
    with _lock:
        data = load()
        if not float(data.get("review_retired_at") or 0.0):
            return False
        data["review_retired_at"] = 0.0
        data["review_retired_reason"] = ""
        data["version"] = STATE_VERSION
        return _write_state(data)
