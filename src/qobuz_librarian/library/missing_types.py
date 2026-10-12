"""The release types the Missing Albums list shows beside albums.

Qobuz files each of an artist's releases as an album, a live release, a
compilation or an EP or single (or as something else, which is never offered).
Albums always show; the Library review's switches add the other three, and
that choice is saved here. CLI runs start from it too.
"""
import threading

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file

ALBUM = "album"
LIVE = "live"
COMPILATION = "compilation"
EP = "epSingle"
OPTIONAL = (LIVE, COMPILATION, EP)
_lock = threading.Lock()


def load() -> frozenset:
    """The optional types switched on, empty unless the user chose some."""
    try:
        data = state_file.load_json_object(
            cfg.MISSING_TYPES_FILE, "the saved Missing Albums switches",
            "which release types Missing Albums shows")
    except OSError:
        return frozenset()
    types = (data or {}).get("types")
    if not isinstance(types, list):
        return frozenset()
    return frozenset(t for t in types if t in OPTIONAL)


def switch(release_type, on):
    """Turn one optional type on or off. The types now on, or None when the
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
                "types": [t for t in OPTIONAL if t in included],
            })
        except OSError:
            return None
    return frozenset(included)


def shown(release_type, included) -> bool:
    """Whether a missing release Qobuz files as ``release_type`` is listed
    with the types in ``included`` switched on. A release whose type could
    not be read always is."""
    return release_type in (None, ALBUM) or release_type in included
