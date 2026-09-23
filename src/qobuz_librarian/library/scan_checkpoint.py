"""Progress checkpoints for resumable library scans.

A full-library scan can take a while; if it's interrupted (the container stops,
the box loses power) the work shouldn't be thrown away. As the scan finishes each
artist it records progress here: which artists are done, the albums found so far,
and the per-artist catalog snapshot for the new-release baseline. The next start
reads this and continues from where it left off rather than re-crawling.

Progress is kept per scan **kind** in one file, so interrupted scans of
different kinds don't wipe each other. "missing" / "partial" are the library
gap scans, surfaced for resume on the dashboard via ``pending()``; "repair" is
the damaged-file sweep, which shares this store but resumes on a manual re-run
of the repair scan rather than the dashboard, so ``pending()`` leaves it out. A
clean finish or a deliberate cancel clears that kind's entry; a kind's presence
means "an unfinished scan of that kind is waiting to resume."
"""
import threading
import time
from contextlib import AbstractContextManager

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library import candidate_premise
from qobuz_librarian.ui_cli import logging as cli_logging

# The library gap-scan kinds pending() surfaces for the dashboard resume
# prompt.
_KINDS = ("missing", "partial")

# A crash can lose this interval's worth of completed artists, plus any
# artists still being scanned. Normal exits flush the remaining progress.
CHECKPOINT_INTERVAL_SECONDS = 5.0

# save/clear are read-modify-write of the shared file; serialise them so two
# scan kinds progressing in parallel can't clobber each other's entry.
_lock = threading.Lock()

# Set when this process last wrote the file holding nothing but one kind's
# entry: (path, file identity, kind). The next save of that kind then has
# nothing to preserve and skips re-reading its own progress.
_sole_entry = None


def _read() -> dict:
    data = state_file.load_json_object(
        cfg.SCAN_CHECKPOINT_FILE, "the scan checkpoint",
        "an interrupted scan's saved progress (it would restart from the "
        "beginning)")
    return data if data is not None else {}


def _write(data) -> bool:
    global _sole_entry
    _sole_entry = None
    try:
        identity = state_file.write_json(
            cfg.SCAN_CHECKPOINT_FILE, data, indent=None)
        if len(data) == 1:
            (kind,) = data
            _sole_entry = (str(cfg.SCAN_CHECKPOINT_FILE), identity, kind)
        return True
    except OSError as e:
        # Surface (verbose) rather than fail completely silent. On a full or
        # read-only data volume an hours-long scan would otherwise save no
        # resumable checkpoint with zero signal.
        cli_logging.vlog(f"scan checkpoint write failed ({e}); resume won't be available")
        return False


def load(kind) -> dict | None:
    """This kind's checkpoint, or None.

    Shape: ``{"scanned": [folder_name, ...], "candidates": [candidate_dict, ...],
    "seen": {artist_id: [album_id, ...]}, "artists": {folder_name: snapshot},
    "meta": {...}, "passes": {name: result}}``. ``artists``, ``meta`` and
    ``passes`` are optional for older checkpoints.
    """
    cp = _read().get(kind)
    if not isinstance(cp, dict):
        return None
    # setdefault only fills ABSENT keys; coerce present-but-wrong types too so a
    # corrupt or hand-edited checkpoint can't crash the consumer's set()/dict().
    if isinstance(cp.get("scanned"), list):
        cp["scanned"] = [
            name for name in cp["scanned"]
            if isinstance(name, str) and name
        ]
    else:
        cp["scanned"] = []
    if isinstance(cp.get("candidates"), list):
        cp["candidates"] = [
            candidate for candidate in cp["candidates"]
            if isinstance(candidate, dict)
        ]
    else:
        cp["candidates"] = []
    if not isinstance(cp.get("seen"), dict):
        cp["seen"] = {}
    if not isinstance(cp.get("artists"), dict):
        cp["artists"] = {}
    cp["candidates"] = [
        candidate_premise.restore_candidate(candidate, _artist_premise(candidate, cp["artists"]))
        for candidate in cp["candidates"]
    ]
    cp["artists"] = {
        name: candidate_premise.restore_artist(entry)
        for name, entry in cp["artists"].items()
    }
    if not isinstance(cp.get("meta"), dict):
        cp["meta"] = {}
    if not isinstance(cp.get("passes"), dict):
        cp["passes"] = {}
    return cp


def _artist_premise(candidate, artists):
    payload = candidate.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    name = payload.get("_artist_dir") or candidate.get("artist")
    entry = artists.get(name)
    return entry.get("_premise") if isinstance(entry, dict) else None


def save(kind, scanned, candidates, seen, artists=None, meta=None,
         passes=None) -> bool:
    with _lock:
        path = str(cfg.SCAN_CHECKPOINT_FILE)
        if _sole_entry is not None and _sole_entry == (
                path, state_file.file_identity(path), kind):
            data = {}
        else:
            data = _read()
        artists = {
            name: candidate_premise.compact_artist(entry)
            for name, entry in (artists or {}).items()
        }
        data[kind] = {
            "scanned": sorted(scanned),
            "candidates": [
                candidate_premise.compact_candidate(candidate, _artist_premise(candidate, artists))
                for candidate in candidates
            ],
            "seen": seen,
            "artists": artists,
            "meta": meta or {},
            "passes": passes or {},
            "ts": time.time(),
        }
        return _write(data)


class Writer(AbstractContextManager):
    """Coalesce one scan's saves on its result-collection thread."""

    def __init__(self, kind):
        self.kind = kind
        self.passes = {}
        self._latest = None
        self._pending = False
        self._last_write = None

    def save(self, scanned, candidates, seen, artists=None, meta=None) -> bool:
        # Keep the single writer's live containers, without copying receipts.
        self._latest = (scanned, candidates, seen, artists, meta)
        self._pending = True
        if (self._last_write is None
                or time.monotonic() - self._last_write >= CHECKPOINT_INTERVAL_SECONDS):
            return self.flush()
        return True

    def keep_pass(self, name, result) -> bool:
        """Save a finished pass with the progress, for a resume to reuse."""
        self.passes[name] = result
        if self._latest is None:
            return True
        self._pending = True
        return self.flush()

    def flush(self) -> bool:
        if not self._pending:
            return True
        self._last_write = time.monotonic()
        saved = save(self.kind, *self._latest, passes=self.passes)
        if saved is not False:
            self._pending = False
        return saved

    def clear(self) -> bool:
        # Drop the buffer rather than flushing it: writing the file out only
        # to delete the entry a moment later.
        self._latest = None
        self._pending = False
        self._last_write = None
        return clear(self.kind)

    def __exit__(self, *exc):
        self.flush()


def clear(kind) -> bool:
    with _lock:
        path = str(cfg.SCAN_CHECKPOINT_FILE)
        if _sole_entry is not None and _sole_entry == (
                path, state_file.file_identity(path), kind):
            data = {}
        else:
            data = _read()
            if kind not in data:
                return True
            del data[kind]
        if data:
            return _write(data)
        try:
            cfg.SCAN_CHECKPOINT_FILE.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            cli_logging.vlog(f"scan checkpoint clear failed ({e}); stale resume data remains")
            return False


def pending() -> dict | None:
    """A summary of any unfinished scan for the dashboard, or None. Missing
    takes precedence (it's the kind the first-run auto-scan runs). Returns
    ``{"kind", "done"}`` where done is how many artists are already scanned."""
    for kind in _KINDS:
        cp = load(kind)
        if cp is not None:
            return {"kind": kind, "done": len(cp.get("scanned", []))}
    return None
