"""Small persistent state for navigation review markers."""

import threading
import time
from contextlib import ExitStack, contextmanager

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file

SURFACES = {"library", "upgrade", "downsample", "repair"}
_VERSION = 1
_STATE_LOCK = threading.RLock()


def _empty() -> dict:
    return {
        "version": _VERSION,
        "surfaces": {name: {"ready_at": 0.0, "seen_at": 0.0}
                     for name in sorted(SURFACES)},
    }


def load(*, fail_closed=False) -> dict:
    path = cfg.REVIEW_BADGE_STATE_FILE
    try:
        raw = state_file.load_json_object(
            path,
            "the saved review badges",
            "your navigation review markers",
        )
    except OSError:
        if fail_closed:
            raise
        return _empty()
    if raw is None:
        return _empty()
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, dict):
        return _empty()
    state = _empty()
    for name in SURFACES:
        entry = surfaces.get(name)
        if not isinstance(entry, dict):
            continue
        try:
            state["surfaces"][name] = {
                "ready_at": float(entry.get("ready_at") or 0.0),
                "seen_at": float(entry.get("seen_at") or 0.0),
            }
        except (TypeError, ValueError):
            continue
    return state


@contextmanager
def _mutation_lock():
    """Hold the badge-store lock across one load/change/save cycle."""
    path = cfg.REVIEW_BADGE_STATE_FILE
    with _STATE_LOCK:
        stack = ExitStack()
        try:
            stack.enter_context(state_file.store_lock(path))
        except OSError:
            stack.close()
            yield False
            return
        with stack:
            yield True


def _save(state: dict) -> bool:
    path = cfg.REVIEW_BADGE_STATE_FILE
    try:
        state_file.write_json(path, state)
        return True
    except OSError:
        return False


def _load_for_mutation():
    try:
        return load(fail_closed=True)
    except OSError:
        return None


def _ts(now=None) -> float:
    return float(time.time() if now is None else now)


def mark_ready(surface: str, *, now=None) -> None:
    if surface not in SURFACES:
        return
    with _mutation_lock() as locked:
        if not locked:
            return
        state = _load_for_mutation()
        if state is None:
            return
        entry = state["surfaces"][surface]
        seen_at = float(entry.get("seen_at") or 0.0)
        ready_at = float(entry.get("ready_at") or 0.0)
        entry["ready_at"] = max(
            _ts(now), seen_at + 0.000001, ready_at + 0.000001
        )
        _save(state)


def clear_ready(surface: str) -> None:
    if surface not in SURFACES:
        return
    with _mutation_lock() as locked:
        if not locked:
            return
        state = _load_for_mutation()
        if state is None:
            return
        state["surfaces"][surface]["ready_at"] = 0.0
        _save(state)


def clear_ready_if_generation(surface: str, generation: float) -> bool:
    """Clear only the badge generation the caller actually observed."""
    if surface not in SURFACES or generation <= 0:
        return False
    with _mutation_lock() as locked:
        if not locked:
            return False
        state = _load_for_mutation()
        if state is None:
            return False
        entry = state["surfaces"][surface]
        if float(entry.get("ready_at") or 0.0) != generation:
            return False
        entry["ready_at"] = 0.0
        return _save(state)


def set_ready(surface: str, ready: bool, *, now=None) -> None:
    if ready:
        mark_ready(surface, now=now)
    else:
        clear_ready(surface)


def ready_generation(surface: str) -> float:
    if surface not in SURFACES:
        return 0.0
    entry = load()["surfaces"][surface]
    ready_at = float(entry.get("ready_at") or 0.0)
    seen_at = float(entry.get("seen_at") or 0.0)
    return ready_at if ready_at > seen_at else 0.0


def mark_seen(surface: str, generation: float) -> None:
    if surface not in SURFACES or generation <= 0:
        return
    with _mutation_lock() as locked:
        if not locked:
            return
        state = _load_for_mutation()
        if state is None:
            return
        entry = state["surfaces"][surface]
        ready_at = float(entry.get("ready_at") or 0.0)
        if ready_at != generation:
            return
        entry["seen_at"] = max(
            generation, float(entry.get("seen_at") or 0.0)
        )
        _save(state)


def snapshot() -> dict[str, bool]:
    state = load()
    out: dict[str, bool] = {}
    for name, entry in state["surfaces"].items():
        out[name] = (
            float(entry.get("ready_at") or 0.0)
            > float(entry.get("seen_at") or 0.0)
        )
    return out
