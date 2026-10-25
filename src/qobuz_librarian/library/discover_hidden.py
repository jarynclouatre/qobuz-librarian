"""Artists the user told Discover to stop suggesting.

Each is kept by its Qobuz artist id and by name: a build ranks Last.fm's names
before it knows any ids, and a namesake Qobuz resolves to the same name is the
same suggestion as far as the user is concerned.
"""
import threading
import time

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library.tags import normalize

_LOCK = threading.RLock()


def _read() -> dict:
    return state_file.load_json_object(
        cfg.DISCOVER_HIDDEN_FILE, "the artists hidden from Discover",
        "your list of artists hidden from Discover") or {}


def load(data: dict | None = None) -> list[dict]:
    """The hidden artists, most recently hidden first."""
    rows = (_read() if data is None else data).get("artists")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        artist_id = str(row.get("id") or "").strip()
        if name or artist_id:
            try:
                ts = float(row.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            out.append({"id": artist_id, "name": name, "ts": ts})
    return sorted(out, key=lambda row: -row["ts"])


def _brought_back_at(data: dict) -> float:
    try:
        return float(data.get("brought_back_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _name_key(name) -> str:
    return normalize(name) or str(name or "").strip().casefold()


class Hidden:
    """One read of the list, asked about many names during a build.
    `brought_back_at` is when an artist was last taken off it; a list built
    before then may have left that artist out."""

    def __init__(self, rows, brought_back_at=0.0):
        self.ids = {row["id"] for row in rows if row["id"]}
        self.names = {_name_key(row["name"]) for row in rows if row["name"]}
        self.names.discard("")
        self.brought_back_at = brought_back_at

    def covers_name(self, name) -> bool:
        return _name_key(name) in self.names

    def covers(self, card: dict) -> bool:
        return (str(card.get("artist_id") or "") in self.ids
                or self.covers_name(card.get("name")))


def current() -> Hidden:
    try:
        data = _read()
    except OSError:
        return Hidden([])
    return Hidden(load(data), _brought_back_at(data))


def _save(rows, brought_back_at) -> None:
    state_file.write_json(cfg.DISCOVER_HIDDEN_FILE, {
        "artists": [{"id": r["id"], "name": r["name"], "ts": r["ts"]}
                    for r in rows],
        "brought_back_at": brought_back_at}, ensure_ascii=False)


def _matches(row, artist_id, name) -> bool:
    if artist_id and row["id"]:
        return row["id"] == artist_id
    return _name_key(row["name"]) == _name_key(name)


def hide(artist_id, name) -> None:
    artist_id = str(artist_id or "").strip()
    name = str(name or "").strip()
    with _LOCK, state_file.store_lock(cfg.DISCOVER_HIDDEN_FILE):
        data = _read()
        rows = [row for row in load(data) if not _matches(row, artist_id, name)]
        rows.insert(0, {"id": artist_id, "name": name, "ts": time.time()})
        _save(rows, _brought_back_at(data))


def unhide(artist_id, name) -> bool:
    """Whether the artist was on the list."""
    artist_id = str(artist_id or "").strip()
    name = str(name or "").strip()
    with _LOCK, state_file.store_lock(cfg.DISCOVER_HIDDEN_FILE):
        rows = load()
        kept = [row for row in rows if not _matches(row, artist_id, name)]
        if len(kept) == len(rows):
            return False
        _save(kept, time.time())
        return True
