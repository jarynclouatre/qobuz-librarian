"""The release types the Missing Albums list shows beside albums, and
whether Gap Fill lists bonus versions.

Qobuz files each of an artist's releases as an album, a live release, a
compilation or an EP or single (or as something else, which is never offered).
Albums always show; the Library review's switches add the other three, and
that choice is saved here. CLI runs start from it too.

An album whose only missing tracks are other versions of songs its folder
holds (a remix, a live take, a demo) counts as complete. Gap Fill lists it
only with the Bonus versions switch on, saved here beside the others.
"""
import threading

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file

ALBUM = "album"
LIVE = "live"
COMPILATION = "compilation"
EP = "epSingle"
BONUS = "bonusVersions"
OPTIONAL = (LIVE, COMPILATION, EP)
SWITCHES = OPTIONAL + (BONUS,)
# What the Library review's switches call them.
LABELS = {LIVE: "Live", COMPILATION: "Compilations", EP: "EPs & singles",
          BONUS: "Bonus versions"}
_lock = threading.Lock()


def load() -> frozenset:
    """The switches turned on, empty unless the user chose some."""
    try:
        data = state_file.load_json_object(
            cfg.MISSING_TYPES_FILE, "the saved Missing Albums switches",
            "which release types Missing Albums shows")
    except OSError:
        return frozenset()
    types = (data or {}).get("types")
    if not isinstance(types, list):
        return frozenset()
    return frozenset(t for t in types if t in SWITCHES)


def switch(release_type, on):
    """Turn one switch on or off. The switches now on, or None when the
    choice could not be saved."""
    with _lock:
        included = set(load())
        if on:
            included.add(release_type)
        else:
            included.discard(release_type)
        try:
            state_file.write_json(cfg.MISSING_TYPES_FILE, {
                "version": 1,
                "types": [t for t in SWITCHES if t in included],
            })
        except OSError:
            return None
    return frozenset(included)


def shown(release_type, included) -> bool:
    """Whether a missing release Qobuz files as ``release_type`` is listed
    with the types in ``included`` switched on. A release whose type could
    not be read always is."""
    return release_type in (None, ALBUM) or release_type in included


def gap_shown(bonus_only, included) -> bool:
    """Whether Gap Fill lists an album, given whether its only missing
    tracks are bonus versions and the switches in ``included``."""
    return not bonus_only or BONUS in included


def named(types) -> str:
    """The switches for ``types`` in one phrase, such as "Live, Compilations
    or EPs & singles"."""
    labels = [LABELS[t] for t in SWITCHES if t in types]
    return ", ".join(labels[:-1]) + (" or " if len(labels) > 1 else "") + labels[-1]
