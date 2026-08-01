"""Small persistent state for navigation review markers."""

import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager

from qobuz_librarian import config as cfg

SURFACES = {"library", "upgrade", "downsample", "repair"}
_VERSION = 1
_STATE_LOCK = threading.RLock()


def _empty() -> dict:
    return {
        "version": _VERSION,
        "surfaces": {name: {"ready_at": 0.0, "seen_at": 0.0}
                     for name in sorted(SURFACES)},
    }


def load() -> dict:
    path = cfg.REVIEW_BADGE_STATE_FILE
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return _empty()
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, dict):
        return _empty()
    state = _empty()
    for name in SURFACES:
        entry = surfaces.get(name)
        if not isinstance(entry, dict):
            continue
        state["surfaces"][name] = {
            "ready_at": float(entry.get("ready_at") or 0.0),
            "seen_at": float(entry.get("seen_at") or 0.0),
        }
    return state


@contextmanager
def _mutation_lock():
    """Hold the badge-store lock across one load/change/save cycle."""
    path = cfg.REVIEW_BADGE_STATE_FILE
    with _STATE_LOCK:
        lock_file = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = path.with_name(path.name + ".lock")
            lock_file = lock_path.open("w", encoding="utf-8")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError:
            if lock_file is not None:
                lock_file.close()
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()


def _save(state: dict) -> None:
    path = cfg.REVIEW_BADGE_STATE_FILE
    fd = None
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _ts(now=None) -> float:
    return float(time.time() if now is None else now)


def mark_ready(surface: str, *, now=None) -> None:
    if surface not in SURFACES:
        return
    with _mutation_lock() as locked:
        if not locked:
            return
        state = load()
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
        state = load()
        state["surfaces"][surface]["ready_at"] = 0.0
        _save(state)


def clear_ready_if_generation(surface: str, generation: float) -> bool:
    """Clear only the badge generation the caller actually observed."""
    if surface not in SURFACES or generation <= 0:
        return False
    with _mutation_lock() as locked:
        if not locked:
            return False
        state = load()
        entry = state["surfaces"][surface]
        if float(entry.get("ready_at") or 0.0) != generation:
            return False
        entry["ready_at"] = 0.0
        _save(state)
        return True


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
        state = load()
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
