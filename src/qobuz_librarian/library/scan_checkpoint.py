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
import json
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
# A slow write waits this many times its own length before the next, and a
# large checkpoint a second for every megabyte it holds, so on a large library
# writing the checkpoint stays a small part of the scan and of the disk's time.
CHECKPOINT_WRITE_SPACING = 20
CHECKPOINT_BYTES_PER_SECOND = 1_000_000

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


def _chunks(data, artists):
    """``data`` as JSON, the artists of each kind in ``artists`` encoded one
    at a time: the C encoder is quick, but holds up every other thread for as
    long as one call takes."""
    yield "{"
    for i, (kind, entry) in enumerate(data.items()):
        yield f"{', ' if i else ''}{json.dumps(kind)}: "
        if kind not in artists:
            yield from json.JSONEncoder().iterencode(entry)
            continue
        yield "{"
        for key, value in entry.items():
            yield f"{json.dumps(key)}: {json.dumps(value)}, "
        yield '"artists": {'
        for j, (name, artist) in enumerate(artists[kind].items()):
            yield (f"{', ' if j else ''}{json.dumps(name)}: "
                   f"{json.dumps(candidate_premise.compact_artist(artist))}")
        yield "}}"
    yield "}"


def _write(data, artists=None) -> bool:
    global _sole_entry
    _sole_entry = None
    try:
        identity = state_file.write_chunks(
            cfg.SCAN_CHECKPOINT_FILE, _chunks(data, artists or {}))
        if len(data) == 1:
            (kind,) = data
            _sole_entry = (str(cfg.SCAN_CHECKPOINT_FILE), identity, kind)
    except OSError as e:
        # Surface (verbose) rather than fail completely silent. On a full or
        # read-only data volume an hours-long scan would otherwise save no
        # resumable checkpoint with zero signal.
        cli_logging.vlog(f"scan checkpoint write failed ({e}); resume won't be available")
        return False
    _write_progress(identity, data)
    return True


def _progress_path():
    return cfg.SCAN_CHECKPOINT_FILE.with_suffix(".progress.json")


def _done_by_kind(data):
    done = {}
    for kind, entry in data.items():
        if isinstance(entry, dict):
            scanned = entry.get("scanned")
            done[kind] = len(scanned) if isinstance(scanned, list) else 0
    return done


def _write_progress(identity, data):
    """Note each kind's scanned count beside the checkpoint, for the pages
    that show no more than that. One left behind by a failed write names an
    older checkpoint and is not used."""
    try:
        state_file.write_json(_progress_path(), {
            "checkpoint": list(identity),
            "done": _done_by_kind(data),
        }, indent=None)
    except OSError:
        pass


def _progress() -> dict:
    """Each waiting kind's scanned count, without reading the checkpoint when
    the note beside it still describes it."""
    identity = state_file.file_identity(cfg.SCAN_CHECKPOINT_FILE)
    if identity is None:
        return {}
    try:
        note = json.loads(_progress_path().read_text(encoding="utf-8"))
        if (note.get("checkpoint") == list(identity)
                and isinstance(note.get("done"), dict)):
            return note["done"]
    except (OSError, ValueError, AttributeError):
        pass
    with _lock:
        identity = state_file.file_identity(cfg.SCAN_CHECKPOINT_FILE)
        if identity is None:
            return {}
        data = _read()
        _write_progress(identity, data)
    return _done_by_kind(data)


def load(kind) -> dict | None:
    """This kind's checkpoint, or None.

    Shape: ``{"scanned": [folder_name, ...], "seen": {artist_id: [album_id,
    ...]}, "artists": {folder_name: snapshot}, "meta": {...}, "passes": {name:
    result}}``, the albums found kept in each artist's snapshot.
    ``artists``, ``meta`` and ``passes`` are optional for older checkpoints,
    and older ones also list the albums found under ``candidates``.
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


def save(kind, scanned, seen, artists=None, meta=None, passes=None) -> bool:
    with _lock:
        path = str(cfg.SCAN_CHECKPOINT_FILE)
        if _sole_entry is not None and _sole_entry == (
                path, state_file.file_identity(path), kind):
            data = {}
        else:
            data = _read()
        data[kind] = {
            "scanned": sorted(scanned),
            "seen": seen,
            "meta": meta or {},
            "passes": passes or {},
            "ts": time.time(),
        }
        return _write(data, {kind: artists or {}})


class Writer(AbstractContextManager):
    """Coalesce one scan's saves on its result-collection thread."""

    def __init__(self, kind, meta=None):
        self.kind = kind
        self.meta = meta or {}
        self.passes = {}
        self._latest = None
        self._pending = False
        self._last_write = None
        self._interval = CHECKPOINT_INTERVAL_SECONDS

    def save(self, scanned, seen, artists=None, meta=None) -> bool:
        # Keep the single writer's live containers, without copying receipts.
        self._latest = (scanned, seen, artists, meta)
        self._pending = True
        if (self._last_write is None
                or time.monotonic() - self._last_write >= self._interval):
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
        started = time.monotonic()
        scanned, seen, artists, meta = self._latest
        saved = save(self.kind, scanned, seen, artists,
                     meta=meta or self.meta, passes=self.passes)
        # Timed from the end of the write: from its start, a write as long as
        # the interval was followed by the next one straight away.
        self._last_write = time.monotonic()
        identity = state_file.file_identity(cfg.SCAN_CHECKPOINT_FILE)
        self._interval = max(
            CHECKPOINT_INTERVAL_SECONDS,
            CHECKPOINT_WRITE_SPACING * (self._last_write - started),
            (identity[2] if identity else 0) / CHECKPOINT_BYTES_PER_SECOND)
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
        except FileNotFoundError:
            pass
        except OSError as e:
            cli_logging.vlog(f"scan checkpoint clear failed ({e}); stale resume data remains")
            return False
        try:
            _progress_path().unlink(missing_ok=True)
        except OSError:
            pass
        return True


def pending() -> dict | None:
    """A summary of any unfinished scan for the dashboard, or None. Missing
    takes precedence (it's the kind the first-run auto-scan runs). Returns
    ``{"kind", "done"}`` where done is how many artists are already scanned."""
    done = _progress()
    for kind in _KINDS:
        if kind in done:
            return {"kind": kind, "done": done[kind]}
    return None
