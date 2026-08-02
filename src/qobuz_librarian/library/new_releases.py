"""State for the new-release quickscan.

The quickscan compares each library artist's current Qobuz catalog against the
album ids recorded here from the last check; anything new the user doesn't own
and hasn't hidden is a new release. The first check of an artist records only a
baseline (so the back catalogue isn't dumped as "new") — later checks surface
the difference. ``last_run`` lets the dashboard throttle the automatic check.
"""
import json
import threading
import time

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file

# The mutators below are load-modify-save sequences; a library scan seeds the
# baseline from a worker thread while the dashboard's auto-check touches the run
# time from another, so serialise them or one's stale snapshot clobbers the
# other (worst case: baseline_complete gets wiped and the check never re-fires).
_lock = threading.Lock()


def load() -> dict:
    """Return ``{"last_run": float|None, "seen": {artist_id: [album_id, …]},
    "baseline_complete": bool, "auto_scan_attempted": bool}``, tolerating a
    missing or corrupt file with an empty baseline."""
    base = {"last_run": None, "seen": {}, "baseline_complete": False,
            "auto_scan_attempted": False, "baseline_limit": None}
    data = state_file.load_json_object(
        cfg.NEW_RELEASE_STATE_FILE, "the new-release baseline",
        "the per-artist snapshot of what Qobuz already had (without it every "
        "artist's back catalogue reads as new)")
    if data is None:
        return base
    seen = data.get("seen")
    if isinstance(seen, dict):
        # Normalise both keys and album ids to str here, at the one read point,
        # so the diff in find_new_releases_for_artist (which compares str ids)
        # can't be fooled into treating everything as "new" by an int id.
        base["seen"] = {str(k): [str(x) for x in v]
                        for k, v in seen.items() if isinstance(v, list)}
    lr = data.get("last_run")
    if isinstance(lr, (int, float)):
        base["last_run"] = float(lr)
    # The ARTIST_CATALOG_LIMIT the baseline was captured under.
    bl = data.get("baseline_limit")
    if isinstance(bl, int) and not isinstance(bl, bool):
        base["baseline_limit"] = bl
    base["baseline_complete"] = bool(data.get("baseline_complete"))
    base["auto_scan_attempted"] = bool(data.get("auto_scan_attempted"))
    return base


def save(state) -> bool:
    tmp = None
    try:
        cfg.NEW_RELEASE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.NEW_RELEASE_STATE_FILE.with_suffix(
            cfg.NEW_RELEASE_STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(cfg.NEW_RELEASE_STATE_FILE)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def last_run() -> float | None:
    return load().get("last_run")


def baseline_limit() -> int | None:
    """The ARTIST_CATALOG_LIMIT the baseline was captured under, or None if a
    pre-tracking baseline. The check re-baselines when the live limit exceeds it."""
    return load().get("baseline_limit")


def mark_run(seen, when=None, complete=False, baseline_limit=None) -> bool:
    """Persist the updated per-artist catalog snapshot and the run time, keeping
    the other fields (load-update-save, not a fresh dict). complete=True also
    marks the baseline ready (a full check crawls every artist, like a library
    scan); baseline_limit records the catalog cap this snapshot was taken at."""
    with _lock:
        state = load()
        state["seen"] = seen
        state["last_run"] = when if when is not None else time.time()
        if complete:
            state["baseline_complete"] = True
        if baseline_limit is not None:
            state["baseline_limit"] = int(baseline_limit)
        return save(state)


def is_baseline_complete() -> bool:
    """True once a full library scan has established the baseline. The automatic
    new-release check stays dormant until then — so it never crawls to an empty
    baseline (surfacing nothing) or activates off a partial scan."""
    return bool(load().get("baseline_complete"))


def seed_baseline(seen) -> bool:
    """Record the per-artist catalog snapshot from a cleanly-completed library
    scan and mark the baseline ready. The scan already fetched every discography,
    so this captures "what exists now" for free; the daily check diffs against it."""
    with _lock:
        state = load()
        state["seen"] = {str(k): list(v) for k, v in (seen or {}).items()}
        state["baseline_complete"] = True
        # Stamp the cap this snapshot was taken at, so a later limit bump triggers
        # a re-baseline instead of surfacing the newly-visible back-slice as "new".
        state["baseline_limit"] = int(cfg.ARTIST_CATALOG_LIMIT)
        return save(state)


def auto_scan_attempted() -> bool:
    return bool(load().get("auto_scan_attempted"))


def note_auto_scan_attempted() -> bool:
    """Remember that the first-run library scan was auto-started, so a fresh one
    isn't relaunched on every load if the user cancels it. (An interrupted scan
    leaves a checkpoint and is auto-resumed regardless of this flag.)"""
    with _lock:
        state = load()
        state["auto_scan_attempted"] = True
        return save(state)


def touch_run(when=None) -> bool:
    """Record that a check was attempted (updates last_run only, keeps the
    baseline). Called when the auto-check submits, so a run that fails or is
    cancelled doesn't re-fire on every dashboard load until one happens to
    succeed."""
    with _lock:
        state = load()
        state["last_run"] = float(when) if when is not None else time.time()
        return save(state)
