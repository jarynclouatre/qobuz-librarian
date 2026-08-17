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

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.ui_cli import logging as cli_logging

# The library gap-scan kinds pending() surfaces for the dashboard resume
# prompt.
_KINDS = ("missing", "partial")

# save/clear are read-modify-write of the shared file; serialise them so two
# scan kinds progressing in parallel can't clobber each other's entry.
_lock = threading.Lock()


def _read() -> dict:
    data = state_file.load_json_object(
        cfg.SCAN_CHECKPOINT_FILE, "the scan checkpoint",
        "an interrupted scan's saved progress (it would restart from the "
        "beginning)")
    return data if data is not None else {}


def _write(data) -> bool:
    try:
        state_file.write_json(cfg.SCAN_CHECKPOINT_FILE, data, indent=None)
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
    "meta": {...}}``. ``artists`` and ``meta`` are optional for older
    checkpoints.
    """
    cp = _read().get(kind)
    if not isinstance(cp, dict):
        return None
    # setdefault only fills ABSENT keys; coerce present-but-wrong types too so a
    # corrupt or hand-edited checkpoint can't crash the consumer's set()/dict().
    if not isinstance(cp.get("scanned"), list):
        cp["scanned"] = []
    if not isinstance(cp.get("candidates"), list):
        cp["candidates"] = []
    if not isinstance(cp.get("seen"), dict):
        cp["seen"] = {}
    if not isinstance(cp.get("artists"), dict):
        cp["artists"] = {}
    if not isinstance(cp.get("meta"), dict):
        cp["meta"] = {}
    return cp


def save(kind, scanned, candidates, seen, artists=None, meta=None) -> bool:
    with _lock:
        data = _read()
        data[kind] = {
            "scanned": sorted(scanned),
            "candidates": candidates,
            "seen": seen,
            "artists": artists or {},
            "meta": meta or {},
            "ts": time.time(),
        }
        return _write(data)


def clear(kind) -> bool:
    with _lock:
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
    data = _read()
    for kind in _KINDS:
        cp = data.get(kind)
        if isinstance(cp, dict):
            return {"kind": kind, "done": len(cp.get("scanned", []))}
    return None
