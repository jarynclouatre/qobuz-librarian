"""State for the new-release quickscan.

The quickscan compares each library artist's current Qobuz catalog against the
album ids recorded here from the last check; anything new the user doesn't own
and hasn't hidden is a new release. The first check of an artist records only a
baseline (so the back catalogue isn't dumped as "new") - later checks surface
the difference. ``last_run`` lets the dashboard throttle the automatic check.
"""
import threading
import time

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library import generation_state

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
            "generation": 0, "revision": 0,
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
    try:
        base["generation"] = max(0, int(data.get("generation") or 0))
        base["revision"] = max(0, int(data.get("revision") or 0))
    except (TypeError, ValueError):
        base["baseline_complete"] = False
        base["generation"] = base["revision"] = 0
    base["auto_scan_attempted"] = bool(data.get("auto_scan_attempted"))
    return base


def save(state) -> bool:
    try:
        state_file.write_json(cfg.NEW_RELEASE_STATE_FILE, state, indent=None)
        return True
    except OSError:
        return False


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
    with _lock, state_file.store_lock(cfg.NEW_RELEASE_STATE_FILE):
        state = load()
        generation = generation_state.current_generation()
        baseline_was_current = bool(
            state.get("baseline_complete")
            and int(state.get("generation") or 0) == generation
            and generation_state.output_is_current(
                "new_releases",
                generation=generation,
            )
        )
        revision = generation_state.reserve_revision()
        if revision is None:
            return False
        state["seen"] = seen
        state["last_run"] = when if when is not None else time.time()
        if complete:
            state["baseline_complete"] = True
            state["generation"] = generation
            state["revision"] = revision
        elif baseline_was_current:
            # A partial check only unions catalogue ids from artists it did
            # reach. The prior complete baseline remains authoritative, but
            # its exact saved contents still need a matching new revision.
            state["revision"] = revision
        if baseline_limit is not None:
            state["baseline_limit"] = int(baseline_limit)
        if not save(state):
            return False
        if not complete and not baseline_was_current:
            return True
        return generation_state.mark_output_current(
            "new_releases",
            generation=generation,
            revision=revision,
            complete=True,
        )


def is_baseline_complete() -> bool:
    """True once a full library scan has established the baseline. The automatic
    new-release check stays dormant until then - so it never crawls to an empty
    baseline (surfacing nothing) or activates off a partial scan."""
    state = load()
    generation = generation_state.current_generation()
    output = generation_state.output_state("new_releases")
    return bool(
        generation > 0
        and state.get("baseline_complete")
        and int(state.get("generation") or 0) == generation
        and int(state.get("revision") or 0)
        == int(output.get("revision") or 0)
        and generation_state.output_is_current(
            "new_releases", generation=generation
        )
    )


def seed_baseline(seen, *, generation=None, revision=None) -> bool:
    """Record the per-artist catalog snapshot from a cleanly-completed library
    scan and mark the baseline ready. The scan already fetched every discography,
    so this captures "what exists now" for free; the daily check diffs against it."""
    with _lock, state_file.store_lock(cfg.NEW_RELEASE_STATE_FILE):
        state = load()
        target_generation = (
            generation_state.current_generation()
            if generation is None
            else int(generation)
        )
        target_revision = (
            generation_state.reserve_revision()
            if revision is None
            else int(revision)
        )
        if target_revision is None:
            return False
        state["seen"] = {str(k): list(v) for k, v in (seen or {}).items()}
        state["baseline_complete"] = True
        state["generation"] = target_generation
        state["revision"] = target_revision
        # Stamp the cap this snapshot was taken at, so a later limit bump triggers
        # a re-baseline instead of surfacing the newly-visible back-slice as "new".
        state["baseline_limit"] = int(cfg.ARTIST_CATALOG_LIMIT)
        if not save(state):
            return False
        return generation_state.mark_output_current(
            "new_releases",
            generation=target_generation,
            revision=target_revision,
            complete=True,
        )


def auto_scan_attempted() -> bool:
    return bool(load().get("auto_scan_attempted"))


def note_auto_scan_attempted() -> bool:
    """Remember that the first-run library scan was auto-started, so a fresh one
    isn't relaunched on every load if the user cancels it. (An interrupted scan
    leaves a checkpoint and is auto-resumed regardless of this flag.)"""
    with _lock, state_file.store_lock(cfg.NEW_RELEASE_STATE_FILE):
        state = load()
        state["auto_scan_attempted"] = True
        return save(state)


def touch_run(when=None) -> bool:
    """Record that a check was attempted (updates last_run only, keeps the
    baseline). Called when the auto-check submits, so a run that fails or is
    cancelled doesn't re-fire on every dashboard load until one happens to
    succeed."""
    with _lock, state_file.store_lock(cfg.NEW_RELEASE_STATE_FILE):
        state = load()
        state["last_run"] = float(when) if when is not None else time.time()
        return save(state)


def note_owned_album(album) -> bool:
    """Fold one exact in-app download into the current catalogue baseline."""
    artist_id = str(((album or {}).get("artist") or {}).get("id") or "").strip()
    album_id = str((album or {}).get("id") or "").strip()
    if not artist_id or not album_id:
        return False
    with _lock, state_file.store_lock(cfg.NEW_RELEASE_STATE_FILE):
        state = load()
        generation = generation_state.current_generation()
        if (
            not state.get("baseline_complete")
            or int(state.get("generation") or 0) != generation
        ):
            return False
        revision = generation_state.reserve_revision()
        if revision is None:
            return False
        seen = dict(state.get("seen") or {})
        albums = list(seen.get(artist_id) or [])
        if album_id not in albums:
            albums.append(album_id)
        seen[artist_id] = albums
        state["seen"] = seen
        state["revision"] = revision
        if not save(state):
            return False
        return generation_state.mark_output_current(
            "new_releases",
            generation=generation,
            revision=revision,
            complete=True,
            preserve_noncurrent=True,
        )
