"""Multi-provider lyrics fetcher.

The engine behind the import-time lyric hook and the library lyrics pass:
writes synced (or plain) lyrics into FLAC tags or .lrc sidecars.

  • Asks LRCLIB first for the exact track at its length, when Lrclib is a
    provider, and takes its search hits only at that length or under the
    track's own title and artist.
  • Tries synced lyrics from each provider; rejects results whose timing
    doesn't fit the track length, held closer for live and remixed cuts.
  • Falls back to plain lyrics across the providers; only writes plain
    lyrics when the track has no existing lyrics at all.
  • Drops the headings, credits, section labels and page trailers lyric
    sites add around the words.
  • Replaces only lyrics it wrote itself, as recorded in the state file. A
    re-check fetches those again, and removes them when nothing matches.
  • Per-run circuit breaker disables a provider after several consecutive
    failures.
  • State file tracks per-file status so subsequent passes only re-check
    tracks worth re-checking.
"""

from __future__ import annotations

import ctypes
import errno
import functools
import hashlib
import logging
import math
import os
import queue
import random
import re
import secrets
import shutil
import stat
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.api.client import ua_string
from qobuz_librarian.file_exclusion import acquire_inode_write_exclusion
from qobuz_librarian.library import backup as backup_mod

# mutagen and syncedlyrics are decoupled on purpose: tag-only operations
# (classify/read/write embedded lyrics, .lrc sidecars) need only mutagen,
# while provider fetching needs syncedlyrics. Importing them together would
# null FLAC whenever the network lib is absent, silently disabling the
# mutagen-only paths too.
try:
    from mutagen.flac import FLAC
except Exception:  # mutagen missing - tag I/O unavailable
    FLAC = None  # type: ignore

try:
    import syncedlyrics
    AVAILABLE = FLAC is not None
    IMPORT_ERROR: Optional[Exception] = (
        None if AVAILABLE else ImportError("mutagen unavailable"))
except Exception as _e:  # missing deps shouldn't crash the ingest pipeline
    syncedlyrics = None  # type: ignore
    AVAILABLE = False
    IMPORT_ERROR = _e

DEFAULT_PROVIDERS  = ["Lrclib", "NetEase", "Musixmatch"]
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / ".lyric_fetch_state.json"

# A crash can lose results since the last checkpoint, plus in-flight tracks.
# Saves are checked after each result; normal exits flush pending changes.
CHECKPOINT_INTERVAL_SECONDS = 5.0

# Per-file outcome codes are internal vocabulary; the lyrics pass shows its log
# to the user, so map them to plain phrases (a raw "write-error" or a
# "{'cached-synced': 4}" dict in the activity log reads like debug output).
_OUTCOME_LABELS = {
    "synced": "added synced lyrics", "plain": "added lyrics",
    "fetched": "added lyrics",
    "wrote-synced": "added synced lyrics", "wrote-plain": "added lyrics",
    "dry:wrote-synced": "would add synced lyrics",
    "dry:wrote-plain": "would add lyrics",
    "copied-synced": "copied its synced lyrics to the other format",
    "copied-plain": "copied its lyrics to the other format",
    "dry:copied-synced": "would copy its synced lyrics to the other format",
    "dry:copied-plain": "would copy its lyrics to the other format",
    "already-synced": "already had synced lyrics",
    "already-plain": "already had lyrics",
    "cached-synced": "already had synced lyrics",
    "cached-plain": "already had lyrics",
    "kept-existing-plain": "kept existing lyrics",
    "not_found": "no lyrics found", "not-found": "no lyrics found",
    "none": "no lyrics found",
    "providers-unavailable": "lyric providers unavailable",
    "write-error": "couldn't write lyrics",
    "unsafe-path": "refused an unsafe track path",
    "error": "error", "exception": "error",
    "skipped": "skipped", "skipped-long": "skipped (too long)",
    "skipped-tags": "skipped (missing tags)",
    "skipped-instrumental": "skipped (instrumental)",
    "removed-instrumental": "removed lyrics (instrumental)",
    "dry:removed-instrumental": "would remove lyrics (instrumental)",
    "removed-unfit": "removed lyrics (no match found)",
    "dry:removed-unfit": "would remove lyrics (no match found)",
    "kept-unrecorded": "kept lyrics this app has no record of writing",
}


def _outcome_label(outcome):
    return _OUTCOME_LABELS.get(
        outcome, (outcome or "").replace("-", " ").replace("_", " "))


def _track_label(path: Path, root: Path) -> str:
    """The track's folders under the library or staging root and its file
    name, so a library-wide log names the artist and album."""
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        return Path(path).name
    return " / ".join(part for part in parts if not part.startswith("."))

SYNCED_RE = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
LRC_TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

RECHECK_AFTER_DAYS = 30
# How long the record of lyrics this app wrote outlives its file's path, for
# the track to come back from a backup or turn up under a new path.
MOVED_RECORD_DAYS = 90
MAX_TRACK_SECONDS  = 60 * 20

# Duration-fit tolerances for synced lyrics. The lower-bound check is
# deliberately loose: songs with long instrumental intros/outros legitimately
# leave a large gap between the last lyric timestamp and the track end, and a
# partial LRC is still useful.
LRC_OVERRUN_GRACE  = 15     # last timestamp may exceed track length by this much
LRC_MIN_COVERAGE   = 0.2    # last timestamp should reach at least this fraction
LRC_GAP_GRACE      = 240    # …unless the gap to track end is within this many seconds
# A live, remixed or extended cut is held closer: studio timing on it is wrong
# even where the words are right, and plain lyrics serve it better.
LRC_CUT_OVERRUN    = 5
LRC_CUT_GAP        = 60     # last timestamp within this many seconds of the end…
LRC_CUT_GAP_SHARE  = 0.3    # …or within this share of the track

LRCLIB_GET_URL = "https://lrclib.net/api/get"
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
# A search hit this close to the track's length is taken as this recording.
# One of another length counts only under the track's own title and artist.
LRCLIB_SEARCH_GRACE = 3
# LRCLIB answers a busy moment with 503, or 429 when asked too often, and a
# Retry-After of a second or so: one request in seven asked one at a time,
# a third of them or more from a full pass, and its searches more often.
# The request waits that long, LRCLIB_RETRY_WAIT when it is not told, and
# up to as long again, picked at random so the workers do not all ask
# again at once, and asks again. After LRCLIB_ATTEMPTS tries each wait
# doubles, and a request that would wait past LRCLIB_BUSY_LIMIT seconds,
# or has been asked LRCLIB_MAX_ATTEMPTS times, goes unanswered, which is
# no failure for the circuit breaker. A 429 holds back every worker's next
# request too.
LRCLIB_ATTEMPTS = 8
LRCLIB_MAX_ATTEMPTS = 30
LRCLIB_BUSY_LIMIT = 120.0
LRCLIB_RETRY_WAIT = 1.0
_lrclib_busy_until = 0.0
_lrclib_busy_lock = threading.Lock()

# Provider circuit breaker. With ≥8 workers transient errors compound quickly,
# so the threshold is bumped from 3 to give providers more rope before being
# disabled.
PROVIDER_FAIL_THRESHOLD = 5
PROVIDER_COOLDOWN_SECONDS = 600  # 10 min before a disabled provider gets retried
# NetEase counts the searches it is sent, one a lookup, and refuses one
# that comes within a minute of twenty others and every lookup for about
# twenty minutes after, answering HTTP 200 with code 405. Its lookups start
# at least this far apart across all workers, a quarter under that limit.
# A provider that answers busy anyway, or with HTTP 429 or 503, is asked
# nothing for PROVIDER_PAUSE seconds, then by one lookup at a time. A pass
# waits for it up to its patience, BUSY_PATIENCE unless its caller says
# otherwise, and then leaves the tracks that need it for the next pass.
PROVIDER_MIN_INTERVAL = {"NetEase": 4.0}
PROVIDER_PAUSE = 120.0
BUSY_PATIENCE = 1800.0
_BUSY_CODES = frozenset({405})
_provider_turns: dict[str, float] = {}  # provider -> next lookup's monotonic start
_provider_held: dict[str, float] = {}   # provider -> monotonic end of its pause
_pacing_lock = threading.Lock()
_dead_providers: dict[str, float] = {}   # provider -> epoch when disabled
_provider_fails: dict[str, int] = {}
_answered_providers: set[str] = set()    # answered a lookup this pass
_refusing_providers: set[str] = set()    # disabled this pass before any answer


def _is_provider_dead(prov: str, log: Optional[logging.Logger] = None) -> bool:
    """
    Return True if `prov` is currently in the cooldown window. If the cooldown
    has elapsed, clear the entry (and reset the strike count) so the next call
    actually queries the provider again. Caller must NOT hold _breaker_lock.
    """
    with _breaker_lock:
        disabled_at = _dead_providers.get(prov)
        if disabled_at is None:
            return False
        if time.time() - disabled_at >= PROVIDER_COOLDOWN_SECONDS:
            del _dead_providers[prov]
            _provider_fails[prov] = 0
            if log is not None:
                log.info("provider %s cooldown elapsed - re-enabling", prov)
            return False
        return True

# Locks for concurrent execution. _state_lock guards the shared state dict
# during save_state (iter races with mutation). _breaker_lock guards the
# circuit-breaker counters/sets that all worker threads share.
_state_lock = threading.Lock()
_breaker_lock = threading.Lock()


# syncedlyrics logs provider errors via Python's logging module, and only
# there: a provider that raises is caught and logged, and Musixmatch logs a
# refused search. We capture warnings/errors per-thread, so any of them marks
# the lookup as unanswered, then silence the StreamHandlers syncedlyrics
# installs on each provider logger so they don't double-print to stderr.
class _ChatterCapture(logging.Handler):
    """Buffers warning+ records into a thread-local list during begin/end."""
    _local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        buf = getattr(self._local, "buf", None)
        if buf is None:
            return
        try:
            buf.append(self.format(record))
        except Exception:
            self.handleError(record)

    @classmethod
    def begin(cls) -> None:
        cls._local.buf = []

    @classmethod
    def end(cls) -> str:
        buf = getattr(cls._local, "buf", None)
        cls._local.buf = None
        return "\n".join(buf or [])

    @classmethod
    def note(cls, text: str) -> None:
        buf = getattr(cls._local, "buf", None)
        if buf is not None:
            buf.append(text)

    @classmethod
    def count(cls) -> int:
        return len(getattr(cls._local, "buf", None) or ())


_chatter_handler = _ChatterCapture(level=logging.WARNING)
_chatter_handler.setFormatter(logging.Formatter("%(message)s"))


class _NoAnswer(Exception):
    """A provider refused a request or asked to be asked later."""


def _answer_code(answer) -> object:
    """The status a provider puts inside its JSON: NetEase's "code", or
    Musixmatch's message header."""
    if not isinstance(answer, dict):
        return 200
    if "code" in answer:
        return answer["code"]
    message = answer.get("message")
    header = message.get("header") if isinstance(message, dict) else None
    if isinstance(header, dict):
        return header.get("status_code", 200)
    return 200


def _note_refused_response(response, *_args, **_kwargs):
    """Note a provider response that is no answer, and end the provider's
    lookup there. syncedlyrics returns None for a refused or failed request
    just as for a miss, and NetEase and Musixmatch refuse with HTTP 200 and
    an error status in their JSON. One asking to be asked later is noted as
    busy."""
    status = response.status_code
    code = 200
    if status < 400 and response.content[:1] == b"{":
        try:
            code = _answer_code(response.json())
        except ValueError:
            pass
    if status in (429, 503) or code in _BUSY_CODES:
        note = f"busy: HTTP {status}, code {code} from {response.url}"
    elif (status >= 400 and status != 404
          or isinstance(code, int) and code not in (200, 404)):
        note = f"refused: HTTP {status}, code {code} from {response.url}"
    else:
        return
    _ChatterCapture.note(note)
    raise _NoAnswer(note)

# The lyrics a provider answered for each search of the track being looked
# up, so the search for plain lyrics after the one for synced lyrics, which
# makes the same requests, is answered without them.
_asked = threading.local()


def _remembered_lrc(provider, get_lrc, search_term):
    answers = getattr(_asked, "answers", None)
    key = (str(provider), search_term)
    if answers is not None and key in answers:
        return answers[key]
    noted = _ChatterCapture.count()
    lrc = get_lrc(search_term)
    if answers is not None and _ChatterCapture.count() == noted:
        answers[key] = lrc
    return lrc


if AVAILABLE:
    # Stop syncedlyrics' LRCProvider.__init__ from re-adding a StreamHandler
    # to each provider's named logger every time it's instantiated (which
    # happens once per syncedlyrics.search() call → handler list grows
    # unbounded and stderr fills with duplicate provider chatter).
    try:
        from syncedlyrics.providers.base import LRCProvider as _LRCProvider
        _orig_lrc_init = _LRCProvider.__init__

        def _quiet_lrc_init(self):  # type: ignore[no-redef]
            _orig_lrc_init(self)
            self.session.hooks["response"].append(_note_refused_response)
            self.get_lrc = functools.partial(_remembered_lrc, self, self.get_lrc)
            for h in list(self.logger.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    self.logger.removeHandler(h)

        _LRCProvider.__init__ = _quiet_lrc_init  # type: ignore[assignment]
    except Exception:
        pass

    # Route the syncedlyrics + per-provider loggers through our thread-local
    # capture handler. Disable propagation so they don't escape to root.
    for _name in ("syncedlyrics", "Lrclib", "NetEase", "Megalobiz",
                  "Musixmatch", "Genius"):
        _lg = logging.getLogger(_name)
        for _h in list(_lg.handlers):
            if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
                _lg.removeHandler(_h)
        _lg.propagate = False
        _lg.addHandler(_chatter_handler)
        _lg.setLevel(logging.WARNING)


@dataclass
class TrackState:
    mtime: float = 0.0
    size: int = 0
    # synced | plain | not_found | transient | error | skipped | unsafe_path
    status: str = ""
    source: str = ""        # provider that succeeded
    last_seen: float = 0.0  # epoch of last attempt
    representations: str = ""  # embed | sidecar | both
    written: str = ""       # sha256 of the lyrics this app last wrote
    missing_since: float = 0.0  # epoch the file was first found gone
    recheck: bool = False   # a re-check has yet to judge the track
    refused: str = ""       # providers that last failed its lookups, comma-separated


_SAVED_STATUSES = frozenset({
    "", "synced", "plain", "not_found", "transient", "error", "skipped",
    "unsafe_path",
})
_SAVED_REPRESENTATIONS = frozenset({"", "embed", "sidecar", "both"})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

# Where a saved status/source pair shows the app did not fetch the lyrics.
_NOT_FETCHED_SOURCES = frozenset({
    "", "indexed", "kept-existing", "existing-representation"})


def _lyrics_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The comment this app keeps in a track beside the lyrics it wrote, holding
# their digest. It stays with the file through moves, copies and other tools'
# tag edits, so the app knows its own lyrics without the state file, and
# lyrics edited since no longer match it.
WRITTEN_MARK = "QOBUZ_LIBRARIAN_LYRICS"


def written_mark(f) -> Optional[str]:
    """What a track's mark holds, or None when it has none. Anything but
    one digest matches no lyrics."""
    values = f.tags.get(WRITTEN_MARK) if f.tags is not None else None
    return ",".join(values) if values else None


# streamrip, which downloads for this app, writes these in capitals and
# pads them to two digits; beets, for one, rewrites them unpadded.
_DOWNLOAD_NUMBERS = ("TRACKNUMBER", "DISCNUMBER", "TRACKTOTAL", "DISCTOTAL")
_PADDED_NUMBER_RE = re.compile(r"\d{2,}")


def lyriced_by_earlier_version(f) -> bool:
    """Whether lyrics with neither a record nor a mark were written by an
    earlier version of this app: they are the track's only lyrics, under
    the lower-case "lyrics" name its writer has always used, and the track
    still holds streamrip's numbering as downloaded."""
    pairs = list(f.tags or ())
    if [key for key, _value in pairs
            if key.lower() in ("lyrics", "unsyncedlyrics")] != ["lyrics"]:
        return False
    numbers = {}
    for key, value in pairs:
        if key in _DOWNLOAD_NUMBERS:
            numbers.setdefault(key, []).append(value)
    return all(
        len(numbers.get(key, ())) == 1
        and _PADDED_NUMBER_RE.fullmatch(numbers[key][0]) is not None
        for key in _DOWNLOAD_NUMBERS)


def _track_state_from_dict(value) -> TrackState | None:
    if not isinstance(value, dict):
        return None
    mtime = value.get("mtime", 0.0)
    size = value.get("size", 0)
    last_seen = value.get("last_seen", 0.0)
    missing_since = value.get("missing_since", 0.0)
    if (
        isinstance(mtime, bool)
        or type(mtime) not in (int, float)
        or not math.isfinite(mtime)
        or isinstance(size, bool)
        or type(size) is not int
        or size < 0
        or isinstance(last_seen, bool)
        or type(last_seen) not in (int, float)
        or not math.isfinite(last_seen)
        or last_seen < 0
    ):
        return None
    status = value.get("status", "")
    source = value.get("source", "")
    representations = value.get("representations", "")
    written = value.get("written", "")
    if (isinstance(missing_since, bool)
            or type(missing_since) not in (int, float)
            or not math.isfinite(missing_since) or missing_since < 0):
        missing_since = 0.0
    recheck = value.get("recheck", False)
    refused = value.get("refused", "")
    return TrackState(
        mtime=float(mtime),
        size=size,
        status=status if isinstance(status, str) and status in _SAVED_STATUSES else "",
        source=source if isinstance(source, str) else "",
        last_seen=float(last_seen),
        representations=(
            representations
            if isinstance(representations, str)
            and representations in _SAVED_REPRESENTATIONS
            else ""
        ),
        written=(
            written
            if isinstance(written, str) and _DIGEST_RE.fullmatch(written)
            else ""
        ),
        missing_since=float(missing_since),
        recheck=recheck is True,
        refused=refused if isinstance(refused, str) else "",
    )


def load_state(path: Path = DEFAULT_STATE_FILE) -> dict[str, TrackState]:
    raw = state_file.load_json_object(
        path, "the lyrics state file",
        "the record of which tracks have already been checked for lyrics")
    if raw is None:
        return {}
    loaded = {}
    for key, value in raw.items():
        state = _track_state_from_dict(value)
        if state is not None:
            loaded[str(key)] = state
    return loaded


@contextmanager
def _state_file_lock(path: Path):
    """Hold an exclusive cross-process lock for the state file while reading +
    writing it."""
    with state_file.store_lock(path):
        yield


def _write_state_unlocked(state: dict[str, TrackState], path: Path) -> None:
    # Unique temp + atomic replace: a shared ".tmp" name let two concurrent
    # checkpoints (a web lyrics pass alongside a CLI import hook) clobber each
    # other's write, and a failed write left the temp orphaned beside the state.
    state_file.write_json(
        path, {k: v.__dict__ for k, v in state.items()},
        indent=0, separators=(",", ":"),
    )


def save_state(state: dict[str, TrackState], path: Path = DEFAULT_STATE_FILE) -> None:
    with _state_file_lock(path):
        _write_state_unlocked(state, path)


def update_state(mutator, path: Path = DEFAULT_STATE_FILE) -> None:
    """Atomically read-modify-write the state file under the cross-process
    lock."""
    with _state_file_lock(path):
        state = load_state(path)
        mutator(state)
        _write_state_unlocked(state, path)


class _StateWriter(dict[str, TrackState]):
    """Coalesce one pass's changed rows without replacing other writers' rows."""

    def __init__(self, path: Path, log: logging.Logger, *, dry_run=False):
        super().__init__(load_state(path))
        self.path = path
        self.log = log
        self.dry_run = dry_run
        self._pending: dict[str, TrackState] = {}
        self._last_write = None

    def __setitem__(self, key: str, value: TrackState) -> None:
        super().__setitem__(key, value)
        if not self.dry_run:
            self._pending[key] = replace(value)

    def save(self) -> None:
        if (self._last_write is None
                or time.monotonic() - self._last_write >= CHECKPOINT_INTERVAL_SECONDS):
            self.flush()

    def flush(self) -> None:
        with _state_lock:
            if not self._pending:
                return
            self._last_write = time.monotonic()
            try:
                update_state(lambda disk: disk.update(self._pending), self.path)
            except Exception as e:
                self.log.warning("checkpoint failed (continuing): %s", e)
            else:
                self._pending.clear()


def prune_missing(state: dict[str, TrackState]) -> int:
    """Drop entries whose file no longer exists, mutating `state` in place.
    A record of lyrics this app wrote stays MOVED_RECORD_DAYS from when its
    file was first missed."""
    now = time.time()
    gone = []
    for key, st in state.items():
        if os.path.exists(key):
            st.missing_since = 0.0
        elif st.written and not st.missing_since:
            st.missing_since = now
        elif not st.written or now - st.missing_since >= MOVED_RECORD_DAYS * 86400:
            gone.append(key)
    for k in gone:
        del state[k]
    return len(gone)


def _held_lyric_digests(path: Path) -> set[str]:
    """Digests of the lyrics in a track's tags and in the .lrc beside it,
    neither read through a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    texts = []
    for name in (path, path.with_suffix(".lrc")):
        try:
            descriptor = os.open(name, flags)
        except OSError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                continue
            if name == path:
                texts += embedded_lyric_values(FLAC(f"/proc/self/fd/{descriptor}"))
            else:
                with open(os.dup(descriptor), "rb") as handle:
                    texts.append(handle.read().decode("utf-8", errors="replace"))
        except Exception:
            continue
        finally:
            os.close(descriptor)
    return {_lyrics_digest(text) for text in texts}


def carry_moved_records(state: dict[str, TrackState], paths) -> int:
    """Give each of ``paths`` without one the record of lyrics this app wrote
    kept under another path for the same file: one of the same size and
    modification time, for lyrics the track holds. A track moved, renamed or
    copied with its album keeps its lyrics recognised as the app's; the old
    record goes when its path is gone."""
    recorded: dict[tuple[int, int], list[str]] = {}
    for key, st in state.items():
        if st.written:
            recorded.setdefault((st.size, int(st.mtime)), []).append(key)
    if not recorded:
        return 0
    carried = 0
    for path in paths:
        key = str(path)
        if key in state and state[key].written:
            continue
        try:
            info = os.stat(key, follow_symlinks=False)
        except OSError:
            continue
        keys = recorded.get((info.st_size, int(info.st_mtime)))
        if not keys:
            continue
        held = _held_lyric_digests(Path(key))
        old = next((k for k in keys if state[k].written in held), None)
        if old is None:
            continue
        state[key] = replace(state[old], missing_since=0.0)
        if not os.path.exists(old):
            keys.remove(old)
            del state[old]
        carried += 1
    return carried


def _carry_moved(paths, path: Path, log: logging.Logger) -> None:
    if not path.exists():
        return
    try:
        with _state_file_lock(path):
            state = load_state(path)
            if carry_moved_records(state, paths):
                _write_state_unlocked(state, path)
    except Exception as e:
        log.warning("couldn't carry lyric records to moved tracks: %s", e)


def carry_copied_lyrics(copies, path: Path = DEFAULT_STATE_FILE) -> None:
    """Record each (source, target, lyrics) copy of a track's lyrics as the
    app's own when the source's record says the app wrote them. The source
    is matched by its size and modification time, as it may have left the
    path it was recorded under."""
    if not copies or not path.exists():
        return
    with _state_file_lock(path):
        state = load_state(path)
        changed = False
        for source, target, lyrics in copies:
            try:
                before = os.stat(source, follow_symlinks=False)
                after = os.stat(target, follow_symlinks=False)
            except OSError:
                continue
            digest = _lyrics_digest(lyrics)
            record = next((
                st for st in state.values()
                if st.written == digest and st.size == before.st_size
                and int(st.mtime) == int(before.st_mtime)), None)
            if record is None:
                continue
            state[str(target)] = replace(
                record, mtime=after.st_mtime, size=after.st_size,
                missing_since=0.0)
            changed = True
        if changed:
            _write_state_unlocked(state, path)


def classify(text: Optional[str]) -> str:
    if not text or not text.strip():
        return "none"
    return "synced" if SYNCED_RE.search(text) else "plain"


def embedded_lyric_values(f):
    return [value for key in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS")
            for value in f.tags.get(key, ()) if value.strip()]


def get_existing_lyrics(f, path=None, include_sidecar=False, *,
                        parent_fd=None, parent_guard=None,
                        track_guard=None) -> Optional[str]:
    values = embedded_lyric_values(f)
    if values:
        return values[0]
    # Sidecars are only trusted through the exact no-follow parent and track
    # binding used by the caller. An unbound path lookup could follow an .lrc
    # symlink outside the owned library and falsely complete this track.
    if include_sidecar and path is not None:
        return _read_bound_sidecar(
            Path(path),
            parent_fd,
            parent_guard,
            track_guard,
        )
    return None


def _regular_identity(value):
    if not stat.S_ISREG(value.st_mode):
        return None
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "modified_ns": int(value.st_mtime_ns),
        "changed_ns": int(value.st_ctime_ns),
    }


def _guard_passes(guard) -> bool:
    if guard is None:
        return False
    try:
        return guard() is True
    except (OSError, TypeError, ValueError):
        return False


def _read_bound_sidecar(
        path: Path, parent_fd, parent_guard, track_guard) -> Optional[str]:
    """Read one exact regular .lrc beside one exact held track, or refuse it."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None or parent_fd is None:
        return None
    descriptor = None
    target = Path(path).with_suffix(".lrc")
    try:
        if not _guard_passes(parent_guard) or not _guard_passes(track_guard):
            return None
        flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(target.name, flags, dir_fd=parent_fd)
        frozen = _regular_identity(os.fstat(descriptor))
        if frozen is None or _named_identity(parent_fd, target.name) != frozen:
            return None
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if (
            not _guard_passes(parent_guard)
            or not _guard_passes(track_guard)
            or _regular_identity(os.fstat(descriptor)) != frozen
            or _named_identity(parent_fd, target.name) != frozen
        ):
            return None
        content = b"".join(chunks).decode("utf-8", errors="replace")
        return content if content.strip() else None
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _valid_regular_identity(value) -> bool:
    return (
        isinstance(value, dict)
        and all(
            isinstance(value.get(field), int)
            for field in (
                "device", "inode", "size", "modified_ns", "changed_ns")
        )
    )


class _BoundTrack:
    """An exact regular file held beneath an exact no-follow directory chain."""

    def __init__(self, path: Path, owned_root: Path):
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if nofollow is None or directory is None:
            raise OSError("safe no-follow file operations are unavailable")

        self.path = Path(os.path.abspath(os.fspath(path)))
        self.root = Path(os.path.abspath(os.fspath(owned_root)))
        try:
            relative = self.path.relative_to(self.root)
        except ValueError:
            raise OSError(
                f"{self.path}: track is outside the owned lyrics root") from None
        if not relative.parts or any(part in ("", ".", "..")
                                     for part in relative.parts):
            raise OSError(f"{self.path}: invalid track path")

        self._directory_fds: list[int] = []
        self._directory_names: list[str] = []
        self.track_fd: Optional[int] = None
        try:
            directory_flags = os.O_RDONLY | directory | nofollow
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            root_fd = os.open(str(self.root), directory_flags)
            root_stat = os.fstat(root_fd)
            named_root = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or not stat.S_ISDIR(named_root.st_mode)
                or root_stat.st_dev != named_root.st_dev
                or root_stat.st_ino != named_root.st_ino
            ):
                os.close(root_fd)
                raise OSError(f"{self.root}: owned lyrics root changed")
            self._directory_fds.append(root_fd)

            for part in relative.parts[:-1]:
                child_fd = os.open(
                    part, directory_flags, dir_fd=self._directory_fds[-1])
                held = os.fstat(child_fd)
                named = os.stat(
                    part,
                    dir_fd=self._directory_fds[-1],
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(held.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or held.st_dev != named.st_dev
                    or held.st_ino != named.st_ino
                ):
                    os.close(child_fd)
                    raise OSError(f"{self.path}: directory chain changed")
                self._directory_names.append(part)
                self._directory_fds.append(child_fd)

            track_flags = (
                os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
            track_flags |= getattr(os, "O_CLOEXEC", 0)
            self.track_fd = os.open(
                relative.parts[-1],
                track_flags,
                dir_fd=self._directory_fds[-1],
            )
            self.identity = _regular_identity(os.fstat(self.track_fd))
            if (
                self.identity is None
                or not self.chain_is_named()
                or self.named_identity() != self.identity
            ):
                raise OSError(f"{self.path}: track identity changed")
        except BaseException:
            self.close()
            raise

    @property
    def parent_fd(self) -> int:
        return self._directory_fds[-1]

    @property
    def name(self) -> str:
        return self.path.name

    def chain_is_named(self) -> bool:
        try:
            held_root = os.fstat(self._directory_fds[0])
            named_root = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(held_root.st_mode)
                or not stat.S_ISDIR(named_root.st_mode)
                or held_root.st_dev != named_root.st_dev
                or held_root.st_ino != named_root.st_ino
            ):
                return False
            for index, name in enumerate(self._directory_names, start=1):
                held = os.fstat(self._directory_fds[index])
                named = os.stat(
                    name,
                    dir_fd=self._directory_fds[index - 1],
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(held.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or held.st_dev != named.st_dev
                    or held.st_ino != named.st_ino
                ):
                    return False
        except (OSError, IndexError):
            return False
        return True

    def named_identity(self):
        try:
            return _named_identity(self.parent_fd, self.name)
        except OSError:
            return None

    def exact_track_is_named(self, track_fd=None, expected=None) -> bool:
        descriptor = self.track_fd if track_fd is None else track_fd
        frozen = self.identity if expected is None else expected
        if descriptor is None or frozen is None or not self.chain_is_named():
            return False
        try:
            return (
                _regular_identity(os.fstat(descriptor)) == frozen
                and self.named_identity() == frozen
            )
        except OSError:
            return False

    def open_named_track(self, expected):
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None or not self.chain_is_named():
            raise OSError(f"{self.path}: track directory changed")
        flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(self.name, flags, dir_fd=self.parent_fd)
        if not self.exact_track_is_named(descriptor, expected):
            os.close(descriptor)
            raise OSError(f"{self.path}: replacement identity changed")
        return descriptor

    def close(self) -> None:
        if self.track_fd is not None:
            try:
                os.close(self.track_fd)
            except OSError:
                pass
            self.track_fd = None
        for descriptor in reversed(self._directory_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._directory_fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _open_parent(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("safe no-follow file operations are unavailable")
    flags = os.O_RDONLY | directory | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(str(path.parent), flags)


def _parent_is_still_named(
        parent_fd: int, path: Path, parent_guard=None) -> bool:
    if parent_guard is not None:
        try:
            return parent_guard() is True
        except (OSError, TypeError, ValueError):
            return False
    try:
        held = os.fstat(parent_fd)
        named = os.stat(path.parent, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(named.st_mode)
        and held.st_dev == named.st_dev
        and held.st_ino == named.st_ino
    )


def _named_identity(parent_fd: int, name: str):
    value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return _regular_identity(value)


def _node_identity(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _named_node_identity(parent_fd: int, name: str):
    return _node_identity(os.stat(
        name, dir_fd=parent_fd, follow_symlinks=False))


def _renameat2(
        first_parent_fd: int,
        first: str,
        second_parent_fd: int,
        second: str,
        flags: int,
) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError(
            errno.ENOTSUP, "atomic exchange is unavailable") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
            first_parent_fd,
            os.fsencode(first),
            second_parent_fd,
            os.fsencode(second),
            flags,
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _rename_exchange(parent_fd: int, first: str, second: str) -> None:
    _renameat2(parent_fd, first, parent_fd, second, 2)


def _rename_noreplace(
        first_parent_fd: int,
        first: str,
        second_parent_fd: int,
        second: str,
) -> None:
    _renameat2(first_parent_fd, first, second_parent_fd, second, 1)


def _make_temp(parent_fd: int, target_name: str):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow file operations are unavailable")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)
    for _ in range(16):
        name = f"{target_name}.{secrets.token_hex(8)}.tmp"
        try:
            return name, os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
    raise FileExistsError(f"couldn't reserve a temporary file for {target_name}")


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _copy_file_fd(source_fd: int, target_fd: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    os.lseek(target_fd, 0, os.SEEK_SET)
    os.ftruncate(target_fd, 0)
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        _write_all(target_fd, chunk)


def _fd_has_content(fd: int, expected: bytes) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    offset = 0
    while offset < len(expected):
        chunk = os.read(fd, min(1024 * 1024, len(expected) - offset))
        if not chunk or chunk != expected[offset:offset + len(chunk)]:
            return False
        offset += len(chunk)
    return os.read(fd, 1) == b""


_journal_lock = threading.Lock()
_journals: list[dict] = []


@contextmanager
def recording_writes():
    """Collect every file this module rewrites, creates or removes while the
    block runs, as ``{path: (before, after)}`` with ``None`` for absent."""
    journal: dict = {}
    with _journal_lock:
        _journals.append(journal)
    try:
        yield journal
    finally:
        with _journal_lock:
            _journals[:] = [j for j in _journals if j is not journal]


def _note_write(path, before, after) -> None:
    key = os.path.abspath(os.fspath(path))
    with _journal_lock:
        for journal in _journals:
            journal[key] = (journal.get(key, (before,))[0], after)


def _emit_receipt(output, receipt) -> None:
    if isinstance(output, dict):
        output.clear()
        output.update(receipt)
    elif isinstance(output, list):
        output.append(receipt)


def _unlink_held_name(
        parent_fd: int,
        name: str,
        held_fd: int,
        expected_identity=None,
        write_exclusion=None,
) -> bool:
    """Remove one held regular file without unlinking through its public name.

    Linux has no inode-conditional unlink. Move the named candidate into a
    freshly-created private directory first, then validate it there. A public
    replacement that wins before the move is restored with no-overwrite rename;
    one that appears afterward is never touched.
    """
    if held_fd is None:
        return False
    try:
        frozen = expected_identity or _regular_identity(os.fstat(held_fd))
        if (
            frozen is None
            or _regular_identity(os.fstat(held_fd)) != frozen
            or _named_identity(parent_fd, name) != frozen
        ):
            return False
    except OSError:
        return False

    directory = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or nofollow is None:
        return False
    quarantine_name = None
    quarantine_fd = None
    moved = False
    moved_node = None

    def _stable_named_node(parent, candidate):
        try:
            value = os.stat(candidate, dir_fd=parent, follow_symlinks=False)
            return (
                int(value.st_dev),
                int(value.st_ino),
                int(value.st_mode),
                int(value.st_size),
                int(value.st_mtime_ns),
            )
        except FileNotFoundError:
            return None
        except OSError:
            return False

    def _capture_quarantined_node():
        nonlocal moved_node
        if quarantine_fd is None:
            return False
        current = _stable_named_node(quarantine_fd, "held")
        if current not in (None, False):
            moved_node = current
            return True
        return False

    def _moved_node_is_public():
        return (
            moved_node is not None
            and _stable_named_node(quarantine_fd, "held") is None
            and _stable_named_node(parent_fd, name) == moved_node
        )

    try:
        for _ in range(16):
            candidate = f".ql-delete-{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            quarantine_name = candidate
            break
        if quarantine_name is None:
            return False
        quarantine_fd = os.open(
            quarantine_name,
            os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        try:
            _rename_noreplace(parent_fd, name, quarantine_fd, "held")
        finally:
            # Python may deliver a signal after renameat2 completed but before
            # its wrapper returned. Derive the namespace outcome from the held
            # inode instead of trusting control flow.
            moved = _capture_quarantined_node()
        moved_identity = _regular_identity(os.fstat(held_fd))
        if (
            moved_identity is None
            or _named_identity(quarantine_fd, "held") != moved_identity
            or any(
                moved_identity[field] != frozen[field]
                for field in (
                    "device", "inode", "size", "modified_ns")
            )
        ):
            return False
        if write_exclusion is not None and not write_exclusion.intact():
            return False
        # The only remaining unlink is inside an unpredictable mode-0700
        # directory held open by this process, not through the mutable public
        # path that was validated above.
        try:
            os.unlink("held", dir_fd=quarantine_fd)
        except BaseException:
            # An interrupt after unlink is a completed forward operation. The
            # caller can no longer roll the old inode back, but this positively
            # prevents an empty private quarantine from being stranded.
            moved = _capture_quarantined_node()
            raise
        moved = False
        return True
    except OSError:
        return False
    finally:
        if moved and quarantine_fd is not None:
            try:
                _rename_noreplace(
                    quarantine_fd, "held", parent_fd, name)
                moved = False
            except OSError:
                # A new public file is never overwritten. The quarantined
                # candidate remains recoverable instead of being guessed at.
                moved = not _moved_node_is_public()
            except BaseException:
                # As above, a late signal can mean the restore completed. Keep
                # the derived state, then propagate the interruption.
                moved = not _moved_node_is_public()
                raise
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass
        if quarantine_name is not None and not moved:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except OSError:
                pass


def _exchange_existing(
        parent_fd: int,
        temp_name: str,
        target_name: str,
        temp_fd: int,
        expected_fd: int,
        expected_identity=None,
        new_identity=None,
        post_exchange_guard=None,
):
    path_flag = getattr(os, "O_PATH", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if path_flag is None or nofollow is None:
        raise OSError(errno.ENOTSUP, "safe atomic exchange is unavailable")

    frozen_expected = (
        expected_identity or _regular_identity(os.fstat(expected_fd)))
    frozen_new = new_identity or _regular_identity(os.fstat(temp_fd))
    exclusion = acquire_inode_write_exclusion(expected_fd)
    if exclusion is None:
        raise OSError(
            errno.EBUSY,
            f"{target_name}: target is open for writing or cannot be protected",
        )

    def restore_original_layout() -> bool:
        """Reconcile an exchange whose syscall or follow-up was interrupted."""
        try:
            old_node = _node_identity(os.fstat(expected_fd))
            new_node = _node_identity(os.fstat(temp_fd))
            named_target = _named_node_identity(parent_fd, target_name)
            named_temp = _named_node_identity(parent_fd, temp_name)
        except OSError:
            return False
        if named_target == old_node and named_temp == new_node:
            return True
        if named_target != new_node or named_temp != old_node:
            return False
        try:
            _rename_exchange(parent_fd, temp_name, target_name)
        except BaseException:
            # The exchange may have completed before Python delivered a signal.
            # Derive the outcome below instead of trusting the raised control
            # flow or replacing the original exception.
            pass
        try:
            restored_old_node = _node_identity(os.fstat(expected_fd))
            restored_new_node = _node_identity(os.fstat(temp_fd))
            restored = (
                _named_node_identity(parent_fd, target_name)
                == restored_old_node
                and _named_node_identity(parent_fd, temp_name)
                == restored_new_node
            )
            if restored and isinstance(new_identity, dict):
                # rename changes ctime. Refresh the caller-owned temporary
                # identity so its ordinary finally block can remove only this
                # exact rolled-back candidate instead of stranding it.
                refreshed_new = _regular_identity(os.fstat(temp_fd))
                if (
                    refreshed_new is None
                    or _named_identity(parent_fd, temp_name) != refreshed_new
                ):
                    return False
                new_identity.clear()
                new_identity.update(refreshed_new)
            return restored
        except OSError:
            # Cleanup must not replace the original exception. Both held file
            # descriptors remain open, and no unmatched public name is touched.
            return False

    def forward_layout_complete() -> bool:
        """Prove the new file committed after the exact old name was removed."""
        try:
            new_after = _regular_identity(os.fstat(temp_fd))
            old_after = _regular_identity(os.fstat(expected_fd))
            if new_after is None or old_after is None:
                return False
            try:
                _named_identity(parent_fd, temp_name)
            except FileNotFoundError:
                pass
            else:
                return False
            return (
                all(
                    new_after[field] == frozen_new[field]
                    for field in ("device", "inode", "size", "modified_ns")
                )
                and all(
                    old_after[field] == frozen_expected[field]
                    for field in ("device", "inode", "size", "modified_ns")
                )
                and _named_identity(parent_fd, target_name) == new_after
            )
        except OSError:
            return False

    swapped_fd = None
    try:
        if (
            frozen_expected is None
            or frozen_new is None
            or not exclusion.intact()
            or _regular_identity(os.fstat(expected_fd)) != frozen_expected
            or _regular_identity(os.fstat(temp_fd)) != frozen_new
            or _named_identity(parent_fd, target_name) != frozen_expected
            or _named_identity(parent_fd, temp_name) != frozen_new
        ):
            raise OSError(
                f"{target_name}: target changed before atomic replacement")

        if not exclusion.intact():
            raise OSError(
                f"{target_name}: target changed before atomic replacement")
        try:
            # A signal can be delivered after renameat2 completed but before its
            # wrapper returns, so every exceptional exit from this point first
            # derives the real layout from the two held identities.
            _rename_exchange(parent_fd, temp_name, target_name)
            swapped_fd = os.open(
                temp_name,
                path_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            new_after = _regular_identity(os.fstat(temp_fd))
            old_after = _regular_identity(os.fstat(expected_fd))
            swapped_identity = _regular_identity(os.fstat(swapped_fd))
            names_match = (
                exclusion.intact()
                and new_after is not None
                and old_after is not None
                and all(
                    new_after[field] == frozen_new[field]
                    for field in ("device", "inode", "size", "modified_ns")
                )
                and all(
                    old_after[field] == frozen_expected[field]
                    for field in ("device", "inode", "size", "modified_ns")
                )
                and _named_identity(parent_fd, target_name) == new_after
                and swapped_identity == old_after
                and _named_identity(parent_fd, temp_name) == old_after
            )
            guard_passed = names_match
            if guard_passed and post_exchange_guard is not None:
                try:
                    guard_passed = post_exchange_guard() is True
                except Exception:
                    guard_passed = False
            guard_passed = guard_passed and exclusion.intact()
            if guard_passed:
                if _unlink_held_name(
                        parent_fd,
                        temp_name,
                        expected_fd,
                        old_after,
                        write_exclusion=exclusion,
                ):
                    return new_after
            raise OSError(
                f"{target_name}: target changed during atomic replacement")
        except BaseException as exc:
            # Roll back whenever both exact names remain. If the old temporary
            # name was already unlinked, classify the exact forward layout so
            # the exceptional exit is never mistaken for an unknown swap.
            if not restore_original_layout() and forward_layout_complete():
                exc.add_note(
                    f"{target_name}: atomic replacement completed before "
                    "the interruption was delivered"
                )
            raise
        finally:
            if swapped_fd is not None:
                os.close(swapped_fd)
    finally:
        exclusion.close()


def save_flac_tags(
        f, path: Path, *, identity_change_out=None,
        directory_mutation_out=None, parent_fd=None,
        parent_guard=None, expected_identity=None, commit_guard=None) -> None:
    """Save changed FLAC tags through a durable same-directory replacement."""
    path = Path(path)
    if isinstance(identity_change_out, dict):
        identity_change_out.clear()
    if isinstance(directory_mutation_out, dict):
        directory_mutation_out.clear()
    owned_parent_fd = (
        _open_parent(path) if parent_fd is None else os.dup(parent_fd)
    )
    source_fd = None
    temp_fd = None
    temp_name = None
    temp_identity = None

    def commit_is_allowed() -> bool:
        if commit_guard is None:
            return True
        try:
            return commit_guard() is True
        except Exception:
            return False

    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file operations are unavailable")
        if not commit_is_allowed():
            raise OSError(f"{path.name}: commit guard changed before tag rewrite")
        source_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        source_flags |= getattr(os, "O_CLOEXEC", 0)
        source_fd = os.open(path.name, source_flags, dir_fd=owned_parent_fd)
        before = _regular_identity(os.fstat(source_fd))
        if (
            before is None
            or (expected_identity is not None and before != expected_identity)
            or not _parent_is_still_named(
                owned_parent_fd, path, parent_guard)
            or _named_identity(owned_parent_fd, path.name) != before
            or not commit_is_allowed()
        ):
            raise OSError(f"{path.name}: source changed before tag rewrite")

        temp_name, temp_fd = _make_temp(owned_parent_fd, path.name)
        if isinstance(directory_mutation_out, dict):
            directory_mutation_out["mutated"] = True
        _copy_file_fd(source_fd, temp_fd)
        shutil.copystat(
            f"/proc/self/fd/{source_fd}", f"/proc/self/fd/{temp_fd}")
        if not commit_is_allowed():
            raise OSError(f"{path.name}: commit guard changed during tag rewrite")
        temp_path = Path(f"/proc/self/fd/{owned_parent_fd}/{temp_name}")
        f.save(str(temp_path))

        temp_identity = _regular_identity(os.fstat(temp_fd))
        if temp_identity is None or _named_identity(
                owned_parent_fd, temp_name) != temp_identity:
            raise OSError(f"{path.name}: temporary replacement changed")
        if not backup_mod._fsync(temp_path):
            raise OSError(
                f"{path.name}: replacement couldn't be flushed to disk; "
                "original left untouched")
        if (
            not _parent_is_still_named(
                owned_parent_fd, path, parent_guard)
            or _regular_identity(os.fstat(source_fd)) != before
            or _named_identity(owned_parent_fd, path.name) != before
            or _regular_identity(os.fstat(temp_fd)) != temp_identity
            or _named_identity(owned_parent_fd, temp_name) != temp_identity
            or not commit_is_allowed()
        ):
            raise OSError(f"{path.name}: source changed during tag rewrite")

        after = _exchange_existing(
            owned_parent_fd,
            temp_name,
            path.name,
            temp_fd,
            source_fd,
            before,
            temp_identity,
            post_exchange_guard=commit_guard,
        )
        temp_name = None
        if (
            after is None
            or not _parent_is_still_named(
                owned_parent_fd, path, parent_guard)
            or _named_identity(owned_parent_fd, path.name) != after
            or not commit_is_allowed()
        ):
            raise OSError(f"{path.name}: replacement identity couldn't be proved")
        _note_write(path, before, after)
        _emit_receipt(identity_change_out, {
            "path": os.path.abspath(os.fspath(path)),
            "before": before,
            "after": after,
        })
        if not backup_mod._fsync(Path(f"/proc/self/fd/{owned_parent_fd}")):
            raise OSError(
                f"{path.name}: replacement completed, but the folder couldn't "
                "be flushed to disk; the change may not survive a power loss")
    finally:
        if temp_name is not None:
            _unlink_held_name(
                owned_parent_fd,
                temp_name,
                temp_fd,
                temp_identity,
            )
        if temp_fd is not None:
            os.close(temp_fd)
        if source_fd is not None:
            os.close(source_fd)
        os.close(owned_parent_fd)


def write_lyrics(f, content: str, *, path=None, mark=False, **save_kwargs) -> None:
    # Vorbis comments are case-insensitive in mutagen, so assigning
    # "lyrics" replaces any lyrics/LYRICS value. The caller decides whether
    # that value may go; a separate UNSYNCEDLYRICS field is never touched.
    f.tags["lyrics"] = [content]
    if mark:
        f.tags[WRITTEN_MARK] = [_lyrics_digest(content)]
    save_flac_tags(
        f, Path(f.filename) if path is None else Path(path), **save_kwargs)


def _open_up_private_sidecar(parent_fd, path) -> None:
    """Give an owner-only sidecar the track's read access.

    Earlier versions wrote every .lrc as 0600, which a media server running
    as another user cannot read.
    """
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None or parent_fd is None:
        return
    try:
        track = os.stat(Path(path).name, dir_fd=parent_fd,
                        follow_symlinks=False)
        descriptor = os.open(Path(path).with_suffix(".lrc").name,
                             os.O_RDONLY | nofollow | os.O_NONBLOCK,
                             dir_fd=parent_fd)
    except OSError:
        return
    try:
        sidecar = os.fstat(descriptor)
        if (stat.S_ISREG(sidecar.st_mode)
                and stat.S_IMODE(sidecar.st_mode) == 0o600
                and stat.S_IMODE(track.st_mode) & 0o044
                and sidecar.st_uid == os.geteuid()):
            os.fchmod(descriptor, stat.S_IMODE(track.st_mode) & 0o666)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _new_file_mode(parent_fd: int, track_name: str) -> int:
    """Permissions for a new sidecar: the track's, else what the umask allows."""
    try:
        track = os.stat(track_name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISREG(track.st_mode):
            return stat.S_IMODE(track.st_mode) & 0o666
    except OSError:
        pass
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return 0o666 & ~int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return 0o644


def write_sidecar(
        path: Path, content: str, *, creation_out=None,
        directory_mutation_out=None, parent_fd=None,
        parent_guard=None, track_guard=None, sidecar_identity_out=None,
        replaces=None) -> bool:
    """Write lyrics to a .lrc file next to the track (UTF-8).

    Never downgrades a user's sidecar: if a non-empty .lrc already exists and is
    SYNCED while the new content is not, keep the existing one. Bound discovery
    recognises the sidecar before any provider fetch, while this final check
    stops a plain result from overwriting a hand-synced .lrc. ``replaces``,
    when given, is the only non-blank text an existing sidecar may hold for it
    to be replaced, synced or not. Return True when a sidecar was written and
    False when the existing file was kept."""
    path = Path(path)
    target = path.with_suffix(".lrc")
    if isinstance(creation_out, dict):
        creation_out.clear()
    if isinstance(directory_mutation_out, dict):
        directory_mutation_out.clear()
    if isinstance(sidecar_identity_out, (dict, list)):
        sidecar_identity_out.clear()
    owned_parent_fd = (
        _open_parent(target) if parent_fd is None else os.dup(parent_fd)
    )
    existing_fd = None
    existing_identity = None
    temp_fd = None
    temp_name = None
    temp_identity = None
    published_identity = None
    created_target = False
    receipt_emitted = False
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("safe no-follow file operations are unavailable")
        if track_guard is not None and track_guard() is not True:
            raise OSError(f"{path.name}: track changed before sidecar write")
        existing_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        existing_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            existing_fd = os.open(
                target.name, existing_flags, dir_fd=owned_parent_fd)
        except FileNotFoundError:
            pass
        if existing_fd is not None:
            existing_identity = _regular_identity(os.fstat(existing_fd))
            if existing_identity is None:
                raise OSError(f"{target.name}: existing sidecar is not a file")
            os.lseek(existing_fd, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(existing_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            old = b"".join(chunks).decode("utf-8", errors="replace")
            if old.strip() and (
                old != replaces if replaces is not None
                else classify(old) == "synced" and classify(content) != "synced"
            ):
                if (
                    not _guard_passes(track_guard)
                    or not _parent_is_still_named(
                        owned_parent_fd, target, parent_guard)
                    or _regular_identity(
                        os.fstat(existing_fd)) != existing_identity
                    or _named_identity(
                        owned_parent_fd, target.name) != existing_identity
                ):
                    raise OSError(
                        f"{target.name}: existing sidecar changed before keep")
                _emit_receipt(sidecar_identity_out, {
                    "path": os.path.abspath(os.fspath(target)),
                    "file": existing_identity,
                })
                return False

        if track_guard is not None and track_guard() is not True:
            raise OSError(f"{path.name}: track changed before sidecar write")
        temp_name, temp_fd = _make_temp(owned_parent_fd, target.name)
        if isinstance(directory_mutation_out, dict):
            directory_mutation_out["mutated"] = True
        os.fchmod(temp_fd, (
            stat.S_IMODE(os.fstat(existing_fd).st_mode)
            if existing_fd is not None
            else _new_file_mode(owned_parent_fd, path.name)
        ))
        intended_content = content.encode("utf-8")
        _write_all(temp_fd, intended_content)
        temp_path = Path(f"/proc/self/fd/{owned_parent_fd}/{temp_name}")
        if not backup_mod._fsync(temp_path):
            raise OSError(
                f"{target.name}: replacement couldn't be flushed to disk; "
                "original left untouched")
        temp_identity = _regular_identity(os.fstat(temp_fd))
        if (
            temp_identity is None
            or (track_guard is not None and track_guard() is not True)
            or not _parent_is_still_named(
                owned_parent_fd, target, parent_guard)
            or _named_identity(owned_parent_fd, temp_name) != temp_identity
        ):
            raise OSError(f"{target.name}: temporary sidecar changed")

        if existing_fd is None:
            if _regular_identity(os.fstat(temp_fd)) != temp_identity:
                raise OSError(f"{target.name}: temporary sidecar changed")
            temp_mode = stat.S_IMODE(os.fstat(temp_fd).st_mode)
            _rename_noreplace(
                owned_parent_fd,
                temp_name,
                owned_parent_fd,
                target.name,
            )
            created_target = True
            temp_name = None
            created_stat = os.fstat(temp_fd)
            created = _regular_identity(created_stat)
            named_stat = os.stat(
                target.name,
                dir_fd=owned_parent_fd,
                follow_symlinks=False,
            )
            publication_is_exact = (
                created is not None
                and _regular_identity(named_stat) == created
                and stat.S_IMODE(created_stat.st_mode) == temp_mode
                and all(
                    created[field] == temp_identity[field]
                    for field in (
                        "device", "inode", "size", "modified_ns")
                )
                and _fd_has_content(temp_fd, intended_content)
            )
            if publication_is_exact:
                published_identity = created
            if (
                not publication_is_exact
                or (track_guard is not None and track_guard() is not True)
                or not _parent_is_still_named(
                    owned_parent_fd, target, parent_guard)
            ):
                raise OSError(
                    f"{target.name}: created sidecar changed after publication")
            _emit_receipt(creation_out, {
                "path": os.path.abspath(os.fspath(target)),
                "file": published_identity,
            })
            receipt_emitted = True
            _note_write(target, None, published_identity)
        else:
            if (
                _regular_identity(os.fstat(existing_fd)) != existing_identity
                or (track_guard is not None and track_guard() is not True)
                or not _parent_is_still_named(
                    owned_parent_fd, target, parent_guard)
                or _regular_identity(os.fstat(temp_fd)) != temp_identity
                or _named_identity(
                    owned_parent_fd, target.name) != existing_identity
            ):
                raise OSError(f"{target.name}: existing sidecar changed")
            replaced = _exchange_existing(
                owned_parent_fd,
                temp_name,
                target.name,
                temp_fd,
                existing_fd,
                existing_identity,
                temp_identity,
                post_exchange_guard=track_guard,
            )
            temp_name = None
            if replaced is not None:
                _note_write(target, existing_identity, replaced)
            if (
                replaced is None
                or (track_guard is not None and track_guard() is not True)
                or not _parent_is_still_named(
                    owned_parent_fd, target, parent_guard)
                or _named_identity(
                    owned_parent_fd, target.name) != replaced
            ):
                raise OSError(
                    f"{target.name}: replacement identity couldn't be proved")
            published_identity = replaced

        if not backup_mod._fsync(Path(f"/proc/self/fd/{owned_parent_fd}")):
            raise OSError(
                f"{target.name}: replacement completed, but the folder couldn't "
                "be flushed to disk; the change may not survive a power loss")
        if (
            (track_guard is not None and not _guard_passes(track_guard))
            or not _parent_is_still_named(
                owned_parent_fd, target, parent_guard)
            or published_identity is None
            or _regular_identity(os.fstat(temp_fd)) != published_identity
            or _named_identity(
                owned_parent_fd, target.name) != published_identity
        ):
            raise OSError(f"{target.name}: final sidecar identity changed")
        _emit_receipt(sidecar_identity_out, {
            "path": os.path.abspath(os.fspath(target)),
            "file": published_identity,
        })
        return True
    finally:
        if (
            created_target
            and not receipt_emitted
            and published_identity is not None
        ):
            _unlink_held_name(
                owned_parent_fd, target.name, temp_fd, published_identity)
        if temp_name is not None:
            _unlink_held_name(
                owned_parent_fd, temp_name, temp_fd, temp_identity)
        if temp_fd is not None:
            os.close(temp_fd)
        if existing_fd is not None:
            os.close(existing_fd)
        os.close(owned_parent_fd)


def _put_back_sidecar(path: Path, binding, written: str,
                      before: Optional[str]) -> bool:
    try:
        if before is None:
            return _remove_written_sidecar(path, binding, written)
        return write_sidecar(
            path,
            before,
            parent_fd=binding.parent_fd,
            parent_guard=binding.chain_is_named,
            track_guard=binding.exact_track_is_named,
            replaces=written)
    except OSError:
        return False


def write_output(
        path: Path, f, content: str, fmt: str, *, binding,
        sidecar_replaces=None, sidecar_before=None, mark=False):
    """Persist lyrics per fmt: 'embed' (FLAC tag), 'sidecar' (.lrc), 'both'.

    ``sidecar_replaces`` is passed to write_sidecar as ``replaces``. The
    sidecar is written first, and one it keeps raises before the tag
    changes. When the tag write then fails, the sidecar goes back to
    ``sidecar_before``, or away when there was none. With ``mark`` the
    track's mark names the lyrics as the app's, in sidecar format too."""
    fmt = _normalise_lyrics_format(fmt)
    if not binding.exact_track_is_named():
        raise OSError(f"{path.name}: track changed before lyric write")

    if fmt in ("sidecar", "both") and not write_sidecar(
            path,
            content,
            parent_fd=binding.parent_fd,
            parent_guard=binding.chain_is_named,
            track_guard=binding.exact_track_is_named,
            replaces=sidecar_replaces):
        raise OSError(f"{path.with_suffix('.lrc').name}: existing lyrics kept")

    marked = _lyrics_digest(content) if mark else None
    if fmt == "sidecar" and (marked is None or written_mark(f) == marked):
        return binding.identity
    change = {}
    save = dict(
        identity_change_out=change,
        parent_fd=binding.parent_fd,
        parent_guard=binding.chain_is_named,
        expected_identity=binding.identity,
    )
    try:
        if fmt == "sidecar":
            f.tags[WRITTEN_MARK] = [marked]
            save_flac_tags(f, path, **save)
        else:
            write_lyrics(f, content, path=path, mark=mark, **save)
    except Exception as e:
        if fmt != "embed" and not _put_back_sidecar(
                path, binding, content, sidecar_before):
            raise OSError(
                f"{e}; {path.with_suffix('.lrc').name} keeps the new lyrics"
            ) from e
        raise
    if (
        change.get("path") != os.path.abspath(os.fspath(path))
        or change.get("before") != binding.identity
        or not _valid_regular_identity(change.get("after"))
    ):
        raise OSError(f"{path.name}: tag replacement wasn't proven")
    return change["after"]


# Title suffixes that confuse provider matching. Strip Spotify-style
# "(Remastered 2009)", "(Album Version)", "[Mono]", trailing
# " - 2009 Remaster", etc., before querying - providers index the canonical
# title. A word naming a different performance ("Live", "Acoustic", "Demo")
# must not be listed here: it has to survive into the query, or the studio
# take's words get written into that recording.
_TITLE_NOISE_KEYWORDS = (
    "remaster", "remastered", "remix", "remixed",
    "album version", "single version", "radio edit", "radio version",
    "extended version", "extended mix", "edit", "mono", "stereo",
    "bonus track", "bonus", "deluxe", "explicit", "clean version",
    "anniversary", "expanded edition", "anniversary edition",
)
# A suffix can carry both at once: "(Live Remastered 2009)" names a different
# performance and a different master, and stripping it for the master lost the
# performance with it.
_PERFORMANCE_KEYWORDS = (
    "live", "acoustic", "unplugged", "demo", "demos", "session", "sessions",
    "instrumental", "alternate take", "alternate version", "re-recorded",
    "rerecorded",
)
# Whole words only: "Oliver" is not a live recording, nor "Obsession" a session.
_PERFORMANCE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _PERFORMANCE_KEYWORDS) + r")\b",
    re.IGNORECASE)
_kw_alt = "|".join(re.escape(k) for k in _TITLE_NOISE_KEYWORDS)
_TITLE_NOISE_RE = re.compile(
    rf"\s*\([^()]*(?:{_kw_alt})[^()]*\)|"
    rf"\s*\[[^\[\]]*(?:{_kw_alt})[^\[\]]*\]|"
    rf"\s+-\s+[^-]*(?:{_kw_alt})[^-]*$",
    re.IGNORECASE,
)
del _kw_alt


def _drop_noise(match) -> str:
    text = match.group(0)
    return text if _PERFORMANCE_RE.search(text) else ""


def _clean_title(title: str) -> str:
    cleaned = title
    for _ in range(4):
        prev = cleaned
        cleaned = _TITLE_NOISE_RE.sub(_drop_noise, cleaned).strip()
        if cleaned == prev:
            break
    return cleaned or title.strip()


# An instrumental has no words to find, so any provider hit for it is the
# vocal take. The duration check cannot catch that: the two run the same length.
_NO_VOCAL_WORDS = r"instrumentals?|backing track|no vocals?|karaoke|minus one"
_INSTRUMENTAL_RE = re.compile(
    rf"\([^()]*\b(?:{_NO_VOCAL_WORDS})\b[^()]*\)|"
    rf"\[[^\[\]]*\b(?:{_NO_VOCAL_WORDS})\b[^\[\]]*\]|"
    rf"\s+-\s+[^-]*\b(?:{_NO_VOCAL_WORDS})\b[^-]*|"
    r"\binstrumentals\s*$",
    re.IGNORECASE,
)


def is_instrumental(title: str) -> bool:
    title = (title or "").strip()
    return (bool(_INSTRUMENTAL_RE.search(title))
            or _clean_title(title).lower() in ("instrumental", "instrumentals"))


# A cut whose timing differs from the studio take, though its words rarely do.
_OTHER_CUT_WORDS = (
    r"live|remix|remixed|mix|extended|edit|dub|demos?|acoustic|unplugged|"
    r"sessions?")
_OTHER_CUT_RE = re.compile(
    rf"[\(\[][^()\[\]]*\b(?:{_OTHER_CUT_WORDS})\b|"
    rf"\s+-\s+.*\b(?:{_OTHER_CUT_WORDS})\b|"
    r"\b(?:remix|mix|edit)\s*$",
    re.IGNORECASE,
)


def is_other_cut(title: str) -> bool:
    return bool(_OTHER_CUT_RE.search(title or ""))


def build_query(f) -> Optional[str]:
    title  = (f.tags.get("title")  or [""])[0].strip()
    artist = (f.tags.get("artist") or f.tags.get("albumartist") or [""])[0].strip()
    if not title or not artist:
        return None
    return f"{_clean_title(title)} {artist}"


# Second tries for a track every provider missed. Providers rarely carry a
# "(feat. ...)" or "(LP Version)" in their titles, or a credit listing every
# artist. A version naming another performance stays, as in _clean_title.
_FEATURING_RE = re.compile(
    r"\s*[\(\[](?:feat\.?|ft\.|featuring)\s[^()\[\]]*[\)\]]", re.IGNORECASE)
_VERSION_LABEL_RE = re.compile(
    r"\s*[\(\[][^()\[\]]*\bversion\b[^()\[\]]*[\)\]]", re.IGNORECASE)
_CREDIT_SPLIT_RE = re.compile(
    r",|;|\s(?:feat\.?|ft\.|featuring)\s", re.IGNORECASE)
MAX_FALLBACK_QUERIES = 2


def fallback_queries(f) -> list[str]:
    """Plainer searches for a missed track: the title without featured
    artists or a version label, with the first credited artist, the album
    artist, then the other credited artists."""
    if not build_query(f):
        return []
    cleaned = _clean_title((f.tags.get("title") or [""])[0].strip())
    title = _VERSION_LABEL_RE.sub(
        _drop_noise, _FEATURING_RE.sub("", cleaned)).strip() or cleaned
    artists = _CREDIT_SPLIT_RE.split((f.tags.get("artist") or [""])[0])
    album_artist = _CREDIT_SPLIT_RE.split((f.tags.get("albumartist") or [""])[0])
    queries = [
        f"{title} {name.strip()}"
        for name in (artists[0], album_artist[0], *artists[1:])
        if name.strip() and name.strip().lower() != "various artists"
    ]
    return [query for query in dict.fromkeys(queries)
            if query != build_query(f)][:MAX_FALLBACK_QUERIES]


# Lines lyric sites add that nobody sings: a "<title> Lyrics" heading or
# credits above the words, section headings, and the page's trailers. A
# label in brackets or parentheses, numbered, or ending in its colon is a
# heading. Other labels, bare or with only a name or a parenthesis after the
# colon, are headings only in lyrics that use labels outside brackets for two
# sections or more, so a sung "Breakdown" or "Chorus: Oh Oh Oh" stays a
# lyric.
_LRC_LINE_RE = re.compile(r"((?:\s*\[\d{1,2}:\d{2}(?:\.\d{1,3})?\])*)\s*(.*?)\s*")
_SECTION_WORD = (r"(?i:intro|outro|verse|chorus|pre-chorus|post-chorus|hook|bridge|"
                 r"refrain|interlude|breakdown)")
_SECTION_NUMBER = r"\s+(?i:\d+|one|two|three|four|five|six)"
_SECTION = rf"{_SECTION_WORD}(?:{_SECTION_NUMBER})?"
_NUMBERED = rf"{_SECTION_WORD}{_SECTION_NUMBER}"
_CREDIT = r"[A-Z0-9][\w.'&$-]*(?:\s*[,&]?\s+[A-Z0-9][\w.'&$-]*){0,3}"
_HEADING_RE = re.compile(
    rf"\[\s*{_SECTION}\b[^\[\]]*\]|\(\s*{_SECTION}\s*\)|"
    rf"{_NUMBERED}\s*:?|{_NUMBERED}\s*:\s*(?:\(.*\)|{_CREDIT})")
_SECTION_LABEL_RE = re.compile(
    rf"{_SECTION}\s*:?|{_SECTION}\s*:\s*(?:\(.*\)|{_CREDIT})")
_SECTION_NAME_RE = re.compile(rf"({_SECTION})")
_HEADER_RE = re.compile(
    r"\d+\s+Contributors?|(?:作词|作曲|编曲|制作人|词|曲|(?i:lyricist|lyrics by|"
    r"written by|composer|composed by|music by|arranger|arranged by|producer|"
    r"produced by))\s*[:：]")
_PAGE_RE = re.compile(
    r"(?:See .*? LiveGet tickets as low as \$\d+)?(?:You might also like)?(?=\S|$)")
_EMBED_RE = re.compile(r"(?:^|(?<=\S))\d*Embed$")


def clean_lyrics(text: str, title: str) -> str:
    """``text`` without the lines lyric sites add around the words."""
    heading = {_words(title) + " lyrics", _song_key(title) + " lyrics"}
    lines = text.splitlines()
    bodies = [_LRC_LINE_RE.fullmatch(line).group(2) for line in lines]
    last = max((i for i, body in enumerate(bodies) if body), default=-1)
    sections = {" ".join(_SECTION_NAME_RE.match(body).group(1).casefold().split())
                for body in bodies if _SECTION_LABEL_RE.fullmatch(body)}
    headed = len(sections) > 1
    kept = []
    head = True
    for i, line in enumerate(lines):
        stamps, body = _LRC_LINE_RE.fullmatch(line).groups()
        words = _PAGE_RE.sub("", body, count=1)
        if i == last:
            words = _EMBED_RE.sub("", words)
        if (words != body and not words
                or head and (_words(words) in heading or _HEADER_RE.match(words))
                or _HEADING_RE.fullmatch(words)
                or _SECTION_LABEL_RE.fullmatch(words)
                and (headed or words.endswith(":"))):
            continue
        head = head and not words
        kept.append(line if words == body else f"{stamps}{words}")
    if len(kept) == len(lines) and all(a is b for a, b in zip(kept, lines)):
        return text
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def lrc_max_seconds(text: str) -> Optional[float]:
    matches = LRC_TS_RE.findall(text)
    if not matches:
        return None
    best = 0.0
    for mm, ss, frac in matches:
        v = int(mm) * 60 + int(ss)
        if frac:
            v += int(frac.ljust(3, "0")) / 1000.0
        if v > best:
            best = v
    return best


def lrc_duration_sane(lyrics: str, track_seconds: float, *,
                      other_cut: bool = False) -> tuple[bool, str]:
    """Reject LRCs whose timing clearly doesn't fit the track length."""
    if not track_seconds or track_seconds <= 0:
        return True, ""
    last = lrc_max_seconds(lyrics)
    if last is None:
        return True, ""
    overrun = LRC_CUT_OVERRUN if other_cut else LRC_OVERRUN_GRACE
    if last > track_seconds + overrun:
        return False, f"LRC ends {last:.0f}s past track end ({track_seconds:.0f}s)"
    gap = track_seconds - last
    if other_cut and gap > max(LRC_CUT_GAP, track_seconds * LRC_CUT_GAP_SHARE):
        return False, f"LRC ends at {last:.0f}s, this cut runs {track_seconds:.0f}s"
    if last < track_seconds * LRC_MIN_COVERAGE and gap > LRC_GAP_GRACE:
        return False, f"LRC ends at {last:.0f}s, track is {track_seconds:.0f}s"
    return True, ""


def _written_from(tagged: str, lyrics: str) -> bool:
    """Whether ``tagged`` is what the app writes for ``lyrics``: the same
    lines with the same capitals, punctuation and timing, or untimed where
    ``tagged`` has no timing. Only blank lines and the spaces around a line
    may differ."""
    if not SYNCED_RE.search(tagged):
        lyrics = LRC_TS_RE.sub("", lyrics)
    tagged_lines, lines = ([line.strip() for line in text.splitlines() if line.strip()]
                           for text in (tagged, lyrics))
    return tagged_lines == lines


def _same_words(first: str, second: str) -> bool:
    """Whether two lyrics share most of their words, timed or not. Versions
    of one song share nearly all of them; two songs rarely half."""
    first, second = ({*re.findall(r"\w+", LRC_TS_RE.sub("", text).casefold())}
                     for text in (first, second))
    return len(first & second) >= 0.8 * max(len(first), len(second), 1)


def usable_providers(names) -> list[str]:
    """The configured providers this app can drive, in their own spelling.
    Nothing configured means the built-in list; only unknown names leave
    none."""
    if not names:
        return list(DEFAULT_PROVIDERS)
    known = {name.lower(): name for name in cfg.LYRICS_PROVIDER_NAMES}
    return list(dict.fromkeys(
        known[name.strip().lower()] for name in names
        if name.strip().lower() in known))


class _Stopped(Exception):
    """The pass was asked to stop while a track's lookups were under way."""


class _Pass:
    """What one lyrics pass's lookups share: its stop request, how long it
    waits for a provider that asked to be asked later, and how many tracks
    it judged without each provider it set aside."""

    def __init__(self, should_stop: Optional[Callable[[], bool]] = None,
                 patience: float = BUSY_PATIENCE):
        self.should_stop = should_stop
        self.patience = patience
        self._busy_since: dict[str, float] = {}
        self._given_up: set[str] = set()
        self._lock = threading.Lock()
        self.set_aside: Counter = Counter()

    def waits_for(self, prov: str, log: logging.Logger) -> bool:
        """Whether lookups still wait for ``prov``, which asked to be asked
        later: until the pass has waited its patience since it first did."""
        now = time.monotonic()
        with self._lock:
            since = self._busy_since.setdefault(prov, now)
            waits = now - since < self.patience
            told = prov in self._given_up
            if not waits:
                self._given_up.add(prov)
        if since == now and waits:
            log.info("%s asked to be asked later; its lookups wait for it", prov)
        elif not waits and not told:
            log.info("%s is still busy; tracks that need it are left for "
                     "the next pass", prov)
        return waits

    def note_set_aside(self, provs) -> None:
        """Count a track judged without ``provs``, which did not answer."""
        with self._lock:
            self.set_aside.update(provs)

    def answered(self, prov: str) -> None:
        with self._lock:
            self._busy_since.pop(prov, None)
            self._given_up.discard(prov)

    def check(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise _Stopped

    def sleep_until(self, moment: float) -> None:
        while (left := moment - time.monotonic()) > 0:
            self.check()
            time.sleep(min(left, 0.25))


def _bounded_provider_search(query: str, prov: str, run: _Pass, *,
                             budget: float, answers: Optional[dict] = None,
                             **kwargs):
    """Run one third-party provider lookup behind an application deadline.

    syncedlyrics providers do not share one reliable timeout contract. Use a
    daemon thread so a provider that ignores its own socket timeout cannot park
    a lyrics worker or prevent process shutdown. The caller records the timeout
    as a hard provider failure, which trips the existing circuit breaker and
    prevents an unbounded stream of abandoned calls.
    """
    outcome = queue.Queue(maxsize=1)

    def invoke():
        _ChatterCapture.begin()
        _asked.answers = answers
        result = None
        error = None
        try:
            result = syncedlyrics.search(query, providers=[prov], **kwargs)
        except BaseException as exc:  # provider code must always signal caller
            error = exc
        chatter = _ChatterCapture.end()
        outcome.put_nowait((result, error, chatter))

    worker = threading.Thread(
        target=invoke,
        name=f"lyrics-provider-{prov}",
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + budget
    while True:
        try:
            return outcome.get(
                timeout=min(0.25, max(0.001, deadline - time.monotonic())))
        except queue.Empty:
            if time.monotonic() >= deadline:
                return None, TimeoutError(
                    f"provider lookup exceeded {cfg.LYRICS_PROVIDER_TIMEOUT:g}s"
                ), ""
            run.check()


def _take_provider_turn(prov: str, run: _Pass, log: logging.Logger) -> bool:
    """Wait for a turn at ``prov``: PROVIDER_MIN_INTERVAL after the turn
    before it, taken by any worker, and past any pause. False when the pass
    no longer waits out the pause."""
    while True:
        with _pacing_lock:
            held = _provider_held.get(prov, 0.0)
        if held > time.monotonic() and not run.waits_for(prov, log):
            return False
        with _pacing_lock:
            start = max(time.monotonic(), _provider_turns.get(prov, 0.0),
                        _provider_held.get(prov, 0.0))
            _provider_turns[prov] = start + PROVIDER_MIN_INTERVAL.get(prov, 0.0)
        run.sleep_until(start)
        with _pacing_lock:
            if _provider_held.get(prov, 0.0) <= time.monotonic():
                return True


def _hold_busy_provider(prov: str) -> None:
    with _pacing_lock:
        _provider_held[prov] = max(_provider_held.get(prov, 0.0),
                                   time.monotonic() + PROVIDER_PAUSE)


def _query_provider(query: str, prov: str, log: logging.Logger,
                    run: _Pass, answers: Optional[dict] = None,
                    missed: Optional[dict] = None,
                    **kwargs) -> tuple[Optional[str], bool]:
    """
    Wrap syncedlyrics.search() for a single provider. Provider errors come
    through Python's logging module; _ChatterCapture buffers them per-thread
    for the circuit breaker. After PROVIDER_FAIL_THRESHOLD
    strikes the provider is skipped for the rest of this run; any successful
    result clears strikes.

    Returns (result, failed_hard). failed_hard means the provider raised or
    logged a warning or error - the query never got an answer, which is not
    the same fact as a clean "no lyrics here" and must not be recorded as
    one. A provider the pass stopped waiting for as busy is noted "busy" in
    ``missed``.
    """
    if _is_provider_dead(prov, log):
        return None, True
    while True:
        run.check()
        # A busy answer is no failure: the lookup waits for the provider,
        # or leaves the track for the next pass.
        if ((answers is None or (prov, query) not in answers)
                and not _take_provider_turn(prov, run, log)):
            if missed is not None:
                missed[prov] = "busy"
            return None, True
        result, error, chatter = _bounded_provider_search(
            query, prov, run, budget=cfg.LYRICS_PROVIDER_TIMEOUT,
            answers=answers, **kwargs
        )
        if not any(line.startswith("busy:") for line in chatter.splitlines()):
            break
        _hold_busy_provider(prov)
    run.answered(prov)
    raised = error is not None
    if error is not None:
        log.debug("provider %s raised: %s", prov, error)
    # A provider that raises (rather than logging) is still a hard failure - it
    # must strike the breaker, or a broken provider is re-queried for every
    # track of a walk instead of being disabled after a few strikes.
    if raised or chatter:
        _provider_failed(prov, log, chatter.splitlines()[0] if chatter else "")
        return None, True
    # A result or a clean "not found" both clear any stale fail count.
    _provider_answered(prov)
    return result, False


def _provider_failed(prov: str, log: logging.Logger, detail: str) -> None:
    with _breaker_lock:
        n = _provider_fails.get(prov, 0) + 1
        _provider_fails[prov] = n
        log.debug("provider %s soft-fail #%d: %s", prov, n, detail)
        # Announce the disable once, on the strike that trips it. Workers
        # already past the _is_provider_dead check keep arriving with a
        # higher count, which would repeat the line under a number that no
        # longer matches the threshold being applied.
        if n >= PROVIDER_FAIL_THRESHOLD and prov not in _dead_providers:
            _dead_providers[prov] = time.time()
            if prov not in _answered_providers:
                _refusing_providers.add(prov)
            log.info("disabling provider %s for %ds after %d consecutive "
                     "failures (will retry after cooldown)",
                     prov, PROVIDER_COOLDOWN_SECONDS,
                     PROVIDER_FAIL_THRESHOLD)


def _provider_answered(prov: str) -> None:
    with _breaker_lock:
        _provider_fails[prov] = 0
        _answered_providers.add(prov)
        _refusing_providers.discard(prov)


_http = threading.local()


def _lrclib_session() -> requests.Session:
    session = getattr(_http, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": ua_string()})
        _http.session = session
    return session


def lrclib_signature(f, duration: float) -> Optional[dict]:
    """What LRCLIB's exact lookup matches on, or None when a tag is missing."""
    fields = {
        "track_name": (f.tags.get("title") or [""])[0].strip(),
        "artist_name": (f.tags.get("artist") or f.tags.get("albumartist")
                        or [""])[0].strip(),
        "album_name": (f.tags.get("album") or [""])[0].strip(),
        "duration": round(duration or 0),
    }
    return fields if all(fields.values()) else None


def _retry_after(response) -> Optional[float]:
    """How long a busy LRCLIB asks to be left, or None when it is not
    busy."""
    if response.status_code not in (429, 503):
        return None
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return LRCLIB_RETRY_WAIT
    return wait if 0 <= wait < math.inf else LRCLIB_RETRY_WAIT


def _lrclib_not_found(response) -> bool:
    """Whether a 404 is LRCLIB's own answer that it has no such track."""
    try:
        answer = response.json()
    except ValueError:
        return False
    return isinstance(answer, dict) and answer.get("name") == "TrackNotFound"


def _lrclib_get(url: str, params: dict, log: logging.Logger, what: str,
                answer_type: type, run: _Pass):
    """LRCLIB's JSON answer, or None when it has no such track. Returns
    (answer, failed_hard) like _query_provider: any other status, or a body
    that is not the JSON LRCLIB sends, a not-found page too, is a page from
    something else in the way, not a miss. A busy answer is asked again
    after the wait it gives."""
    global _lrclib_busy_until
    response = None
    retry_at = 0.0
    waited = 0.0
    for attempt in range(1, LRCLIB_MAX_ATTEMPTS + 1):
        run.check()
        with _lrclib_busy_lock:
            held = _lrclib_busy_until
        if held - time.monotonic() > LRCLIB_BUSY_LIMIT - waited:
            break
        if held > time.monotonic():
            retry_at = max(retry_at, held + random.random())
        run.sleep_until(retry_at)
        try:
            response = _lrclib_session().get(
                url, params=params, timeout=cfg.LYRICS_PROVIDER_TIMEOUT)
        except requests.RequestException as e:
            _provider_failed("Lrclib", log, f"{what}: {e}")
            return None, True
        wait = _retry_after(response)
        if wait is None:
            break
        if response.status_code == 429:
            with _lrclib_busy_lock:
                _lrclib_busy_until = max(
                    _lrclib_busy_until, time.monotonic() + wait)
        pause = (wait * (1 + random.random())
                 * 2 ** max(0, attempt - LRCLIB_ATTEMPTS))
        if waited + pause > LRCLIB_BUSY_LIMIT:
            break
        waited += pause
        retry_at = time.monotonic() + pause
    if response is None or _retry_after(response) is not None:
        log.debug("LRCLIB %s: still busy after %d tries", what, attempt)
        return None, True
    if response.status_code == 404 and _lrclib_not_found(response):
        _provider_answered("Lrclib")
        return None, False
    try:
        answer = response.json() if response.status_code == 200 else None
    except ValueError:
        answer = None
    if not isinstance(answer, answer_type):
        _provider_failed("Lrclib", log, f"{what}: HTTP {response.status_code}")
        return None, True
    _provider_answered("Lrclib")
    return answer, False


def _lrclib_exact(signature: dict, log: logging.Logger,
                  run: _Pass) -> tuple[Optional[dict], bool]:
    """LRCLIB's record for this title, artist and album at this length (it
    allows two seconds either way). A 404 is a clean miss."""
    return _lrclib_get(LRCLIB_GET_URL, signature, log, "exact lookup", dict,
                       run)


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", (text or "").casefold()))


def _song_key(title: str) -> str:
    return _words(re.sub(r"\s*[\(\[][^()\[\]]*[\)\]]|\s+-\s+.*$", "", title or ""))


def credited_names(*credits: str) -> frozenset[str]:
    names = {_words(part) for credit in credits
             for part in (credit, *_CREDIT_SPLIT_RE.split(credit or ""))}
    return frozenset(names - {""})


def _lrclib_search(query: str, duration: float, song: tuple[str, frozenset],
                   log: logging.Logger, run: _Pass) -> tuple[list[dict], bool]:
    """LRCLIB's search hits that can be this recording, those at its length
    first, then those under its title and artist. syncedlyrics would take the
    closest name at any length."""
    records, failed = _lrclib_get(
        LRCLIB_SEARCH_URL, {"q": query}, log, "search", list, run)
    if records is None:
        return [], failed
    title, artists = song
    timed, named = [], []
    for record in records:
        if not isinstance(record, dict) or record.get("instrumental"):
            continue
        length = record.get("duration")
        if (duration and isinstance(length, (int, float))
                and abs(length - duration) <= LRCLIB_SEARCH_GRACE):
            timed.append(record)
        elif (_song_key(record.get("trackName")) == _song_key(title)
              and credited_names(record.get("artistName") or "") & artists):
            named.append(record)
    return timed + named, failed


def search_lyrics(
    queries: list[str], providers: list[str], duration: float,
    log: logging.Logger, skip_plain: bool = False, *,
    exact: Optional[dict] = None, other_cut: bool = False,
    song: tuple[str, frozenset] = ("", frozenset()),
    run: Optional[_Pass] = None, missed: Optional[dict] = None,
) -> tuple[Optional[str], Optional[str], str, int, int]:
    """
    With Lrclib among the providers, its exact lookup by ``exact`` (see
    lrclib_signature) comes first: it matches the track's length, so its
    timing is this cut's. Each query then runs only when every provider
    missed the ones before it; ``other_cut`` holds synced results to the
    track's length. Lrclib's search hits are held to the track's length, or
    to its title and artist in ``song``.
    Returns (lyrics, provider_name, kind, providers_tried, failed_hard) where
    kind is 'synced', 'plain', or 'instrumental' when LRCLIB's exact record
    says the track has no words, and providers_tried counts the lookups due,
    made or skipped. failed_hard counts those that never got an answer:
    the provider raised, was refused or failed, stayed busy, or was
    skipped as disabled by the circuit breaker. Only a miss with failed_hard at 0 says
    no provider has the lyrics, so an outage doesn't poison state. Each
    provider with such a lookup is noted in ``missed``, "busy" when it
    stayed busy and "failed" otherwise.
    """
    run = run or _Pass()
    missed = {} if missed is None else missed
    answers: dict = {}
    tried = 0
    hard = 0
    exact_plain = None
    if exact and "Lrclib" in providers:
        tried += 1
        if _is_provider_dead("Lrclib", log):
            record, failed = None, True
        else:
            record, failed = _lrclib_exact(exact, log, run)
        hard += failed
        if failed:
            missed.setdefault("Lrclib", "failed")
        if record is not None:
            if record.get("instrumental"):
                return None, "Lrclib", "instrumental", tried, hard
            synced = record.get("syncedLyrics") or ""
            if SYNCED_RE.search(synced) and lrc_duration_sane(synced, duration)[0]:
                return synced, "Lrclib", "synced", tried, hard
            exact_plain = (record.get("plainLyrics") or "").strip() or None
    for query in queries:
        lrclib_hits = []
        for prov in providers:
            tried += 1
            if _is_provider_dead(prov, log):
                hard += 1
                missed.setdefault(prov, "failed")
                continue
            if prov == "Lrclib":
                lrclib_hits, failed = _lrclib_search(
                    query, duration, song, log, run)
                results = [hit.get("syncedLyrics") for hit in lrclib_hits]
            else:
                result, failed = _query_provider(
                    query, prov, log, run, answers, missed, synced_only=True)
                results = [result]
            hard += failed
            if failed:
                missed.setdefault(prov, "failed")
            for result in results:
                if not result or not SYNCED_RE.search(result):
                    continue
                ok, reason = lrc_duration_sane(
                    result, duration, other_cut=other_cut)
                if not ok:
                    log.debug("rejected %s synced result: %s", prov, reason)
                    continue
                return result, prov, "synced", tried, hard
        if skip_plain:
            continue
        if exact_plain:
            return exact_plain, "Lrclib", "plain", tried, hard
        for prov in providers:
            if prov == "Lrclib":
                result = next((hit.get("plainLyrics") for hit in lrclib_hits
                               if (hit.get("plainLyrics") or "").strip()), None)
            else:
                tried += 1
                result, failed = _query_provider(
                    query, prov, log, run, answers, missed, plain_only=True)
                hard += failed
                if failed:
                    missed.setdefault(prov, "failed")
            if result and result.strip():
                return result, prov, "plain", tried, hard
    return None, None, "", tried, hard


def _normalise_lyrics_format(value: str) -> str:
    value = (value or "embed").strip().lower()
    return value if value in ("embed", "sidecar", "both") else "embed"


def _required_representations(value: str) -> tuple[str, ...]:
    value = _normalise_lyrics_format(value)
    if value == "both":
        return ("embed", "sidecar")
    return (value,)


def _representation_state(values: Iterable[str]) -> str:
    present = set(values)
    if {"embed", "sidecar"}.issubset(present):
        return "both"
    if "embed" in present:
        return "embed"
    if "sidecar" in present:
        return "sidecar"
    return ""


def _representations_cover(recorded: str, requested: str) -> bool:
    if recorded == "both":
        present = {"embed", "sidecar"}
    elif recorded in ("embed", "sidecar"):
        present = {recorded}
    else:
        present = set()
    return set(_required_representations(requested)).issubset(present)


def _best_existing_lyrics(*values: Optional[str]) -> Optional[str]:
    for wanted in ("synced", "plain"):
        for value in values:
            if classify(value) == wanted:
                return value
    return None


def should_process(
    path: Path,
    st: Optional[TrackState],
    rescan: bool,
    *,
    mtime: Optional[float] = None,
    size: Optional[int] = None,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
) -> bool:
    """
    Decide whether `path` is worth (re-)processing. mtime/size may be passed
    in by the caller (e.g. from a bulk listing) to avoid an extra path.stat().
    """
    if rescan or st is None or st.recheck:
        return True
    if mtime is None or size is None:
        try:
            stat = path.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            return False
    if int(mtime) != int(st.mtime) or size != st.size:
        return True
    if st.status == "synced":
        # A cached track stat cannot prove a sibling sidecar still exists. Reopen
        # final sidecar/both states through the held no-follow binding each run.
        if "sidecar" in _required_representations(lyrics_format):
            return True
        return not _representations_cover(
            st.representations, lyrics_format)
    if st.status == "plain":
        # Plain lyrics this app wrote are re-tried every run in case a
        # provider has gained synced lyrics for the track. Plain lyrics from
        # anywhere else are never replaced, and --skip-plain makes all of
        # them final. Those an index found are opened once to tell which.
        replaceable = (bool(st.written) or st.source == "indexed"
                       or st.source not in _NOT_FETCHED_SOURCES)
        if replaceable and not skip_existing_plain:
            return True
        if "sidecar" in _required_representations(lyrics_format):
            return True
        return not _representations_cover(
            st.representations, lyrics_format)
    if st.status in ("not_found", "error", "skipped"):
        # "skipped" (long-track, missing-tags) expires like not_found
        # instead of being re-opened on every run.
        age_days = (time.time() - st.last_seen) / 86400
        return age_days >= RECHECK_AFTER_DAYS
    return True


def _commit(state: dict[str, TrackState], key: str, st: TrackState) -> None:
    with _state_lock:
        state[key] = st


def _remove_written_sidecar(path: Path, binding: _BoundTrack, expected: str) -> bool:
    """Delete the .lrc beside a held track while it still holds ``expected``."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return False
    name = path.with_suffix(".lrc").name
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=binding.parent_fd)
    try:
        identity = _regular_identity(os.fstat(descriptor))
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        content = b"".join(chunks).decode("utf-8", errors="replace")
        removed = (
            identity is not None
            and content == expected
            and binding.exact_track_is_named()
            and _unlink_held_name(
                binding.parent_fd, name, descriptor, identity)
        )
        if removed:
            _note_write(path.with_suffix(".lrc"), identity, None)
        return removed
    finally:
        os.close(descriptor)


def _remove_written(
        path: Path, f, st: TrackState, state: dict[str, TrackState], key: str,
        binding: _BoundTrack, representations: dict, ours: dict,
        dry_run: bool, log: logging.Logger, commit, *,
        reason: str = "instrumental") -> str:
    """Remove the lyrics this app wrote from an instrumental, or from a track
    a re-check found no lyrics for (``reason`` "unfit"). Lyrics from anywhere
    else stay, and the result says so."""
    removed = [name for name in ("embed", "sidecar") if ours[name]]
    kept = {name: value for name, value in representations.items()
            if classify(value) != "none" and name not in removed}
    st.last_seen = time.time()
    if removed and not dry_run:
        try:
            if "sidecar" in removed and not _remove_written_sidecar(
                    path, binding, representations["sidecar"]):
                raise OSError(f"{path.with_suffix('.lrc').name} changed")
            marked = written_mark(f) is not None
            if "embed" in removed or marked:
                if "embed" in removed:
                    del f.tags["lyrics"]
                if marked:
                    del f.tags[WRITTEN_MARK]
                change = {}
                save_flac_tags(
                    f, path, identity_change_out=change,
                    parent_fd=binding.parent_fd,
                    parent_guard=binding.chain_is_named,
                    expected_identity=binding.identity)
                if (change.get("before") != binding.identity
                        or not _valid_regular_identity(change.get("after"))):
                    raise OSError(f"{path.name}: tag replacement wasn't proven")
                st.mtime = change["after"]["modified_ns"] / 1_000_000_000
                st.size = change["after"]["size"]
                left = embedded_lyric_values(f)
                if left:
                    kept["embed"] = left[0]
        except Exception as e:
            log.error("couldn't remove lyrics: %s - %s", path, e)
            st.status = "error"
            commit(state, key, st)
            return "write-error"
        st.written = ""
    if kept:
        st.status = classify(_best_existing_lyrics(*kept.values()))
        st.source = "kept-existing"
        st.representations = _representation_state(kept)
        commit(state, key, st)
        return f"already-{st.status}"
    st.status, st.source = (
        ("skipped", "instrumental") if reason == "instrumental"
        else ("not_found", ""))
    st.representations = ""
    commit(state, key, st)
    if removed:
        return f"{'dry:' if dry_run else ''}removed-{reason}"
    return "skipped-instrumental"


def process_file(
    path: Path, state: dict[str, TrackState],
    providers: list[str], dry_run: bool, log: logging.Logger,
    owned_root: Path,
    synced_only: bool = False,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
    rescan: bool = False,
    run: Optional[_Pass] = None,
) -> str:
    try:
        binding = _BoundTrack(path, owned_root)
    except (OSError, TypeError, ValueError) as e:
        log.warning("unsafe lyric path refused: %s - %s", path, e)
        if not dry_run:
            key = str(path)
            st = replace(state[key]) if key in state else TrackState()
            st.status = "unsafe_path"
            st.source = "outside-owned-root"
            st.last_seen = time.time()
            _commit(state, key, st)
        return "unsafe-path"

    with binding:
        return _process_bound_file(
            path,
            state,
            providers,
            dry_run,
            log,
            binding,
            synced_only=synced_only,
            skip_existing_plain=skip_existing_plain,
            lyrics_format=lyrics_format,
            rescan=rescan,
            run=run,
        )


def _process_bound_file(
    path: Path, state: dict[str, TrackState],
    providers: list[str], dry_run: bool, log: logging.Logger,
    binding: _BoundTrack,
    synced_only: bool = False,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
    rescan: bool = False,
    run: Optional[_Pass] = None,
) -> str:
    key = str(path)
    st = replace(state[key]) if key in state else TrackState()
    rescan = rescan or st.recheck
    # Keep uncommitted changes out of checkpoints; previews never commit.
    if dry_run:
        def commit(*_args, **_kwargs):
            return None
    else:
        def commit(state, key, st):
            # A re-check that could not judge the track leaves it to the
            # next pass.
            st.recheck = rescan and st.status in ("transient", "error")
            if st.status != "transient":
                st.refused = ""
            _commit(state, key, st)

    try:
        f = FLAC(f"/proc/self/fd/{binding.track_fd}")
    except Exception as e:
        log.error("FLAC open failed: %s - %s", path, e)
        st.status = "error"
        st.last_seen = time.time()
        commit(state, key, st)
        return "error"

    track_stat = os.fstat(binding.track_fd)
    # Older state has no digest; a file untouched since this app wrote plain
    # lyrics into it still holds those lyrics.
    unchanged_since_fetch = (
        not st.written
        and st.status == "plain"
        and st.source not in _NOT_FETCHED_SOURCES
        and int(track_stat.st_mtime) == int(st.mtime)
        and track_stat.st_size == st.size
    )
    st.mtime = track_stat.st_mtime
    st.size = track_stat.st_size

    duration = getattr(f.info, "length", 0) or 0
    if duration > MAX_TRACK_SECONDS:
        st.status = "skipped"
        st.source = "long-track"
        st.last_seen = time.time()
        commit(state, key, st)
        return "skipped-long"

    instrumental = is_instrumental((f.tags.get("title") or [""])[0])
    requested_format = _normalise_lyrics_format(lyrics_format)
    required = _required_representations(requested_format)

    def track_guard():
        return binding.exact_track_is_named()

    embedded = get_existing_lyrics(f)
    sidecar = _read_bound_sidecar(
        path,
        binding.parent_fd,
        binding.chain_is_named,
        track_guard,
    )
    if sidecar is not None and not dry_run:
        _open_up_private_sidecar(binding.parent_fd, path)
    if not track_guard():
        log.warning("track changed during lyric discovery: %s", path)
        st.status = "transient"
        st.source = "track-changed"
        st.representations = ""
        st.last_seen = time.time()
        commit(state, key, st)
        return "write-error"
    representations = {"embed": embedded, "sidecar": sidecar}
    present = {
        name for name, value in representations.items()
        if classify(value) != "none"
    }
    missing = [name for name in required if name not in present]
    considered = (
        tuple(representations.values())
        if missing
        else tuple(representations[name] for name in required)
    )
    existing = _best_existing_lyrics(*considered)
    existing_kind = classify(existing)
    lyrics = None
    source = None
    kind = ""
    write_format = requested_format

    # Only lyrics this app wrote may be replaced; anything else stays as it is.
    tagged = f.tags.get("lyrics") if f.tags is not None else None
    # The record and the track's mark each name lyrics the app wrote.
    written = {digest for digest in (st.written or None, written_mark(f))
               if digest is not None}
    only_tag = bool(tagged) and len(tagged) == 1 and all(
        value == tagged[0] for value in embedded_lyric_values(f))
    # Lyrics an earlier version wrote are known only by how the track looks,
    # which lyrics added since by hand can share. They are replaced only
    # when they are what the app writes for a provider's lyrics, and never
    # removed.
    guessed = (only_tag and not written and not unchanged_since_fetch
               and "sidecar" not in present and lyriced_by_earlier_version(f))
    ours = {
        "embed": only_tag and (
            _lyrics_digest(tagged[0]) in written if written
            else unchanged_since_fetch or guessed),
        # Before the written digest was kept, the format "both" wrote the
        # same text to the sidecar as to the tag.
        "sidecar": bool(sidecar) and (
            _lyrics_digest(sidecar) in written if written
            else unchanged_since_fetch and bool(tagged) and len(tagged) == 1
            and sidecar.strip() == tagged[0].strip()),
    }
    # A re-check judges the lyrics this app wrote again, as if the track had
    # none. Lyrics it has no record of writing are kept, and counted apart.
    rejudge = rescan and bool(present) and all(ours[name] for name in present)
    unrecorded = rescan and bool(present) and not rejudge

    def kept(outcome):
        if ((unrecorded or rescan and guessed)
                and outcome.startswith(("already-", "kept-existing"))):
            return "kept-unrecorded"
        return outcome

    def remove_written(reason):
        return kept(_remove_written(
            path, f, st, state, key, binding, representations,
            {**ours, "embed": ours["embed"] and not guessed},
            dry_run, log, commit, reason=reason))

    if instrumental:
        return remove_written("instrumental")
    if rejudge:
        existing = None
        existing_kind = "none"
        write_format = _representation_state(present.union(required))

    representation_kinds = {
        name: classify(value) for name, value in representations.items()
    }
    stale_plain = next(
        (name for name, kind in representation_kinds.items() if kind == "plain"),
        None)
    mismatched_both = (
        not rejudge
        and requested_format == "both"
        and not missing
        and set(representation_kinds.values()) == {"plain", "synced"}
        and ours[stale_plain]
    )

    if mismatched_both:
        # Both requested copies exist, but one is a weaker stale plain lyric
        # this app wrote. Repair that representation from the locally held
        # synced copy. Treating the pair as already-synced leaves the
        # disagreement cached forever.
        write_format = stale_plain
        lyrics = next(
            value for name, value in representations.items()
            if representation_kinds[name] == "synced"
        )
        source = "existing-representation"
        kind = "synced"
        action = "copied-synced"
        st.last_seen = time.time()
    elif (missing and existing_kind != "none"
          and not (synced_only and existing_kind == "plain")):
        # A requested representation is absent, but the other one already has
        # usable lyrics. Complete the configured format locally instead of
        # making a provider call or rewriting the representation we trust.
        lyrics = existing
        source = "existing-representation"
        kind = existing_kind
        write_format = missing[0]
        action = f"copied-{kind}"
        st.last_seen = time.time()
    else:
        if not missing and existing_kind == "synced":
            st.status = "synced"
            st.representations = _representation_state(present)
            st.last_seen = time.time()
            commit(state, key, st)
            return kept("already-synced")

        if (
            not missing
            and existing_kind == "plain"
            and skip_existing_plain
        ):
            # --skip-plain: every requested representation exists and the user
            # opted out of an upgrade pass.
            st.status = "plain"
            st.representations = _representation_state(present)
            st.last_seen = time.time()
            commit(state, key, st)
            return kept("already-plain")

        if existing_kind == "plain":
            # Fill missing copies, but replace only lyrics this app wrote.
            upgradable = [name for name in required if name in missing or ours[name]]
            if not upgradable:
                st.status = "plain"
                st.source = "kept-existing"
                st.representations = _representation_state(present)
                st.last_seen = time.time()
                commit(state, key, st)
                return kept("already-plain")
            write_format = (
                "both" if len(upgradable) == 2 else upgradable[0])
            if ours["embed"] and not st.written and not guessed:
                st.written = _lyrics_digest(tagged[0])

        query = build_query(f)
        if not query:
            st.status = "skipped"
            st.source = "missing-tags"
            st.representations = _representation_state(present)
            st.last_seen = time.time()
            commit(state, key, st)
            return "skipped-tags"

        # If the file already has plain lyrics, the plain-fallback pass is pure
        # waste: any plain result would be discarded below. The same shortcut
        # applies when the caller asked for synced-only.
        skip_plain = synced_only or existing_kind == "plain"
        title = (f.tags.get("title") or [""])[0]
        artists = credited_names(*f.tags.get("artist", ()),
                                 *f.tags.get("albumartist", ()))
        missed = {}
        lyrics, source, kind, _tried, failed_hard = search_lyrics(
            [query, *fallback_queries(f)], providers, duration, log,
            skip_plain=skip_plain, exact=lrclib_signature(f, duration),
            other_cut=is_other_cut(title), song=(title, artists),
            run=run, missed=missed,
        )
        st.last_seen = time.time()
        refused = {prov for prov, why in missed.items()
                   if why == "failed" and prov != "Lrclib"}
        # A provider that failed the track's lookups on its last pass too,
        # or every lookup of this pass until it was disabled, is set aside,
        # LRCLIB never, and the track judged on the answers of the others.
        # Lyrics it may have given are not removed.
        with _breaker_lock:
            refusing = set(_refusing_providers)
        set_aside = refused & (set(st.refused.split(",")) | refusing)
        if set_aside and set_aside == set(missed) and len(missed) < len(providers):
            failed_hard = 0
            run.note_set_aside(set_aside)
        unconfirmed = bool(set_aside) and st.source not in set(providers) - set_aside
        if lyrics:
            lyrics = clean_lyrics(lyrics, title) or None
        if guessed and lyrics and _written_from(clean_lyrics(tagged[0], title), lyrics):
            guessed = False
        elif guessed:
            lyrics = None
        if rejudge and failed_hard and (source != "Lrclib" or "Lrclib" in missed):
            # A provider that did not answer may have had better lyrics.
            # LRCLIB's own, when it answered, are held to the track's length.
            lyrics = None
        timed = _best_existing_lyrics(*representations.values())
        if (rejudge and lyrics and kind == "plain" and classify(timed) == "synced"
                and lrc_duration_sane(timed, duration, other_cut=is_other_cut(title))[0]
                and _same_words(timed, lyrics)):
            # Providers do not offer timing every time they are asked; the
            # same words with timing that fits the track stay.
            lyrics, source, kind = clean_lyrics(timed, title), st.source, "synced"
        if rejudge and lyrics and missed and (
                kind == "plain" and classify(timed) == "synced"
                or st.source in missed and source != st.source):
            # A provider that did not answer may have had timed lyrics, or
            # may still have the lyrics it gave; the track keeps its own.
            lyrics = None
            unconfirmed = True

        if kind == "instrumental":
            return remove_written("instrumental")
        if not lyrics:
            st.representations = _representation_state(present)
            if failed_hard:
                # A provider did not answer for this file, whether disabled
                # by the circuit breaker or failing on the connection, and
                # may have had its lyrics. That is no verdict about the track
                # - "not found" here would suppress it for RECHECK_AFTER_DAYS.
                # Keep it immediately retryable, and remove nothing.
                st.status = "transient"
                st.refused = ",".join(sorted(refused))
                commit(state, key, st)
                return "providers-unavailable"
            if rejudge and not synced_only and not unconfirmed:
                return remove_written("unfit")
            if rejudge:
                # Plain lyrics were not searched for, or a provider set aside
                # may have given these.
                st.status = classify(_best_existing_lyrics(*representations.values()))
                commit(state, key, st)
                return kept(f"already-{st.status}")
            if existing_kind == "plain":
                st.status = "plain"
                st.source = "kept-existing"
                commit(state, key, st)
                return kept("kept-existing-plain")
            st.status = "not_found"
            st.source = ""
            commit(state, key, st)
            return "not-found"

        if kind == "synced":
            action = "wrote-synced"
        elif existing_kind == "none":
            action = "wrote-plain"
        else:
            st.status = "plain"
            st.source = "kept-existing"
            st.representations = _representation_state(present)
            commit(state, key, st)
            return kept("kept-existing-plain")
        if rejudge and all(representations[name] == lyrics
                           for name in present.union(required)):
            st.status = kind
            st.source = source
            st.written = _lyrics_digest(lyrics)
            st.representations = _representation_state(present)
            commit(state, key, st)
            return f"already-{kind}"

    if dry_run:
        st.status = kind
        st.source = f"{source} (dry-run)"
        commit(state, key, st)
        return f"dry:{action}"

    claimed = rejudge or source != "existing-representation"
    try:
        final_identity = write_output(
            path, f, lyrics, write_format, binding=binding,
            sidecar_replaces=sidecar if ours["sidecar"] else "",
            sidecar_before=sidecar, mark=claimed)
    except Exception as e:
        log.error("write failed: %s - %s", path, e)
        if binding.exact_track_is_named():
            st.status = "error"
        else:
            # The provider may have taken seconds to answer. If the public
            # path changed in that interval, refuse this write but retry the
            # newly named track on the next pass instead of suppressing it for
            # the normal error backoff window.
            st.status = "transient"
            st.source = "track-changed"
            st.representations = ""
        commit(state, key, st)
        return "write-error"

    st.status = kind
    st.source = source
    if claimed:
        st.written = _lyrics_digest(lyrics)
    if not _valid_regular_identity(final_identity):
        log.error("write completed without an exact file identity: %s", path)
        st.status = "error"
        st.representations = ""
        commit(state, key, st)
        return "write-error"
    st.mtime = final_identity["modified_ns"] / 1_000_000_000
    st.size = final_identity["size"]
    st.representations = _representation_state(
        present.union(_required_representations(write_format)))
    commit(state, key, st)
    return action


def fetch_for_paths(
    paths: Iterable[Path],
    providers: Optional[list[str]] = None,
    delay: float = 0.0,
    state_path: Path = DEFAULT_STATE_FILE,
    dry_run: bool = False,
    rescan: bool = False,
    log: Optional[logging.Logger] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    workers: int = 8,
    synced_only: bool = False,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    *,
    owned_root: Path,
    busy_patience: float = BUSY_PATIENCE,
    set_aside: Optional[Counter] = None,
) -> Counter:
    """
    Run the lyrics pipeline over `paths`. Returns Counter of outcome → n.
    State is loaded from / saved to `state_path` so re-runs only re-check
    tracks whose previous outcome warrants it. The per-run provider circuit
    breaker is reset on each call.

    `should_stop`, if supplied, is polled before each file and during its
    lookups and lets the caller stop cleanly (e.g. on SIGINT). A track whose
    lookups a stop cut short is left as it was and not counted.

    `busy_patience` is how many seconds the pass waits for a provider that
    asked to be asked later before leaving the tracks that need it.

    `set_aside`, if supplied, gets the number of tracks judged without each
    provider set aside for them.

    `workers` controls per-file concurrency. Each file's work is dominated
    by network I/O (multi-provider HTTP), which releases the GIL - so
    threading is the right tool here. Default 8 is a reasonable balance
    against provider rate limits; raise it if your library is huge and
    your providers tolerate more parallelism, lower it (or set 1) to debug.
    """
    if log is None:
        log = logging.getLogger("lyric_fetch")
    if not AVAILABLE:
        log.warning("lyric_fetch unavailable: %s", IMPORT_ERROR)
        return Counter({"unavailable": 0})

    providers = usable_providers(providers)
    with _breaker_lock:
        _dead_providers.clear()
        _provider_fails.clear()
        _answered_providers.clear()
        _refusing_providers.clear()

    paths = [Path(p) for p in paths]
    if not dry_run:
        _carry_moved(paths, state_path, log)
    state = _StateWriter(state_path, log, dry_run=dry_run)
    if dry_run:
        carry_moved_records(state, paths)
    candidates = [p for p in paths
                  if should_process(p, state.get(str(p)), rescan,
                                    skip_existing_plain=skip_existing_plain,
                                    lyrics_format=lyrics_format)]
    if rescan and not dry_run:
        # Tracks a stop leaves unjudged are re-checked by the next pass.
        for p in candidates:
            st = state.get(str(p))
            state[str(p)] = (replace(st, recheck=True) if st is not None
                             else TrackState(recheck=True))
        state.flush()

    counts: Counter = Counter()
    total = len(candidates)
    workers = max(1, int(workers))
    if candidates and not providers:
        # Every track is then left retryable, never marked not found.
        log.warning("No known lyric provider is set in LYRICS_PROVIDERS; "
                    "no lyrics were fetched.")

    run = _Pass(should_stop, busy_patience)

    def run_one(fp: Path) -> Optional[str]:
        try:
            outcome = process_file(
                fp, state, providers, dry_run, log,
                owned_root,
                synced_only=synced_only,
                skip_existing_plain=skip_existing_plain,
                lyrics_format=lyrics_format,
                rescan=rescan,
                run=run,
            )
        except _Stopped:
            return None
        except Exception as e:
            log.exception("unexpected error on %s: %s", fp, e)
            outcome = "exception"
        if delay > 0 and outcome.startswith(("wrote-", "dry:", "not-found", "kept-existing")):
            time.sleep(delay)
        return outcome

    completed = 0
    stopped = False
    try:
        if workers == 1:
            for fp in candidates:
                if should_stop and should_stop():
                    stopped = True
                    break
                outcome = run_one(fp)
                if outcome is None:
                    stopped = True
                    break
                completed += 1
                counts[outcome] += 1
                label = _track_label(fp, owned_root)
                log.info("[%d/%d] %s - %s", completed, total, _outcome_label(outcome), label)
                if progress_cb:
                    progress_cb(completed, total, label)
                state.save()
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lyrics") as ex:
                remaining = iter(candidates)
                futures = {}

                def submit_available():
                    nonlocal stopped
                    while not stopped and len(futures) < workers:
                        if should_stop and should_stop():
                            stopped = True
                            return
                        try:
                            fp = next(remaining)
                        except StopIteration:
                            return
                        futures[ex.submit(run_one, fp)] = fp

                submit_available()
                try:
                    while futures:
                        done, _ = wait(
                            tuple(futures), return_when=FIRST_COMPLETED)
                        for fut in done:
                            fp = futures.pop(fut)
                            try:
                                outcome = fut.result()
                            except Exception as e:
                                log.exception("worker raised on %s: %s", fp, e)
                                outcome = "exception"
                            if outcome is None:
                                continue
                            # Every submitted worker is drained, and counted unless a
                            # stop cut its lookups short. Once a stop is observed no
                            # replacement work is queued, so a returned summary
                            # cannot omit a worker that may write.
                            try:
                                completed += 1
                                counts[outcome] += 1
                                label = _track_label(fp, owned_root)
                                log.info("[%d/%d] %s - %s", completed, total, _outcome_label(outcome), label)
                                if progress_cb:
                                    progress_cb(completed, total, label)
                                state.save()
                            except Exception as e:
                                log.exception("post-process error on %s: %s", fp, e)
                        if not stopped and should_stop and should_stop():
                            stopped = True
                        submit_available()
                except KeyboardInterrupt:
                    for f in futures:
                        f.cancel()
                    raise

    finally:
        state.flush()
        if set_aside is not None:
            set_aside.update(run.set_aside)

    if stopped:
        counts["stopped"] = 1
        counts["stop-total"] = total
    return counts


def index_existing(
    items: Iterable,
    *,
    owned_root: Path,
    state_path: Path = DEFAULT_STATE_FILE,
    log: Optional[logging.Logger] = None,
    workers: int = 64,
    should_stop: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Counter:
    """
    Fast scan-only pass - open each FLAC, classify the existing lyrics tag,
    and write state entries for files that already have synced or plain
    lyrics. No provider calls, so workers can be cranked far higher than the
    network path tolerates. Use this to seed the state file after a
    fresh import so a subsequent normal run skips already-synced files
    instead of re-checking them.

    `items` is an iterable of either `Path` or `(Path, mtime, size)` tuples.
    Tuple hints are accepted for caller compatibility, but the held descriptor's
    verified stat remains authoritative at this mutation boundary.
    Files for which `state` already records a matching mtime+size are
    skipped without opening the FLAC, so re-running --index after adding
    new files is cheap.
    """
    if log is None:
        log = logging.getLogger("lyric_fetch")
    if not AVAILABLE:
        log.warning("lyric_fetch unavailable: %s", IMPORT_ERROR)
        return Counter({"unavailable": 0})

    normalized: list[tuple[Path, float, int]] = []
    for it in items:
        if isinstance(it, tuple):
            p, mt, sz = it
            normalized.append((Path(p), float(mt or 0), int(sz or 0)))
        else:
            normalized.append((Path(it), 0.0, 0))

    _carry_moved([fp for fp, _mt, _sz in normalized], state_path, log)
    state = _StateWriter(state_path, log)
    counts: Counter = Counter()
    total = len(normalized)
    workers = max(1, int(workers))

    def index_one(fp: Path, mt_hint: float, sz_hint: int) -> str:
        key = str(fp)
        try:
            binding = _BoundTrack(fp, owned_root)
        except (OSError, TypeError, ValueError) as e:
            log.warning("unsafe lyric index path refused: %s - %s", fp, e)
            return "unsafe-path"
        with binding:
            held = os.fstat(binding.track_fd)
            mtime, size = held.st_mtime, held.st_size
            cached = state.get(key)
            if cached is not None and cached.recheck:
                return "awaiting-recheck"
            if (cached is not None
                    and int(mtime) == int(cached.mtime)
                    and size == cached.size
                    and cached.status in (
                        "synced", "plain", "not_found", "skipped")
                    and (
                        cached.status not in ("synced", "plain")
                        or _representations_cover(
                            cached.representations, "embed")
                    )):
                return f"cached-{cached.status}"
            try:
                f = FLAC(f"/proc/self/fd/{binding.track_fd}")
            except Exception as e:
                log.debug("FLAC open failed: %s - %s", fp, e)
                return "open-error"
            kind = classify(get_existing_lyrics(f))
            if kind == "none":
                # Don't write state for files with no lyrics - let the normal
                # run pick them up via should_process(st=None).
                return "no-lyrics"
            st = TrackState(
                mtime=mtime, size=size,
                status=kind, source="indexed",
                last_seen=time.time(),
                representations="embed",
                written=cached.written if cached is not None else "",
            )
            _commit(state, key, st)
            return f"indexed-{kind}"

    log.info("indexing %d files with %d workers (no provider calls)",
             total, workers)
    progress_every = max(25, min(250, total // 50))
    completed = 0
    stopped = False
    last_log = time.monotonic()
    try:
        if workers == 1:
            for fp, mt, sz in normalized:
                if should_stop and should_stop():
                    stopped = True
                    break
                outcome = index_one(fp, mt, sz)
                completed += 1
                counts[outcome] += 1
                if progress_cb:
                    progress_cb(completed, total, fp.name)
                if completed % progress_every == 0:
                    now = time.monotonic()
                    rate = progress_every / max(0.001, now - last_log)
                    last_log = now
                    log.info("[%d/%d] · %.0f tracks/s", completed, total, rate)
                state.save()
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lyrics-idx") as ex:
                remaining = iter(normalized)
                futures = {}

                def submit_available():
                    nonlocal stopped
                    while not stopped and len(futures) < workers:
                        if should_stop and should_stop():
                            stopped = True
                            return
                        try:
                            fp, mt, sz = next(remaining)
                        except StopIteration:
                            return
                        futures[ex.submit(index_one, fp, mt, sz)] = fp

                submit_available()
                try:
                    while futures:
                        done, _ = wait(
                            tuple(futures), return_when=FIRST_COMPLETED)
                        for fut in done:
                            fp = futures.pop(fut)
                            try:
                                outcome = fut.result()
                            except Exception as e:
                                log.debug("worker raised on %s: %s", fp, e)
                                outcome = "exception"
                            completed += 1
                            counts[outcome] += 1
                            if progress_cb:
                                progress_cb(completed, total, fp.name)
                            if completed % progress_every == 0:
                                now = time.monotonic()
                                rate = progress_every / max(0.001, now - last_log)
                                last_log = now
                                log.info("[%d/%d] · %.0f tracks/s", completed, total, rate)
                            state.save()
                        if not stopped and should_stop and should_stop():
                            stopped = True
                        submit_available()
                except KeyboardInterrupt:
                    for f in futures:
                        f.cancel()
                    raise

    finally:
        state.flush()

    if stopped:
        counts["stopped"] = 1
        counts["stop-total"] = total
    return counts
