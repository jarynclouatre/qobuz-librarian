"""Multi-provider lyrics fetcher.

The engine behind the import-time lyric hook and the library lyrics pass:
writes synced (or plain) lyrics into FLAC tags or .lrc sidecars.

  • Asks LRCLIB first for the exact track at its length, when Lrclib is a
    provider, and takes any provider's search hits only under the track's
    title, at that length or by its artist.
  • Tries synced lyrics from each provider; rejects results whose timing
    doesn't fit the track length, held closer for live and remixed cuts.
  • Falls back to plain lyrics across the providers; only writes plain
    lyrics when the track has no existing lyrics at all.
  • Replaces plain lyrics with synced ones when a provider has them, and
    never replaces synced lyrics or removes any.
  • Drops the headings, credits, section labels and page trailers lyric
    sites add around the words, and takes nothing when that is all there is.
  • Per-run circuit breaker disables a provider after several consecutive
    failures.
  • State file tracks per-file status so subsequent passes only re-check
    tracks worth re-checking.
"""

from __future__ import annotations

import codecs
import functools
import logging
import math
import os
import queue
import random
import re
import secrets
import stat
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

from qobuz_librarian import config as cfg
from qobuz_librarian import disk_support, state_file
from qobuz_librarian.api.client import ua_string
from qobuz_librarian.safefile import (
    copy_metadata,
    copy_owner,
    delete_verified,
    fsync_dir,
    identity_record,
    named_entry_matches,
    rename_exchange,
    rename_noreplace,
    same_directory,
    stat_identity,
    write_all,
)
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import wrap_thread_target

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
# A slow write waits this many times its own length before the next, and a
# large state file a second for every megabyte it holds, so on a large library
# rewriting it stays a small part of the pass and of the disk's time.
CHECKPOINT_WRITE_SPACING = 20
CHECKPOINT_BYTES_PER_SECOND = 1_000_000

# Per-file outcome codes are internal vocabulary; the lyrics pass shows its log
# to the user, so map them to plain phrases (a raw "write-error" or a
# "{'cached-synced': 4}" dict in the activity log reads like debug output).
_OUTCOME_LABELS = {
    "wrote-synced": "added synced lyrics", "wrote-plain": "added lyrics",
    "dry:wrote-synced": "would add synced lyrics",
    "dry:wrote-plain": "would add lyrics",
    "upgraded-synced": "replaced its plain lyrics with synced ones",
    "dry:upgraded-synced": "would replace its plain lyrics with synced ones",
    "copied-synced": "copied its synced lyrics to the other format",
    "copied-plain": "copied its lyrics to the other format",
    "dry:copied-synced": "would copy its synced lyrics to the other format",
    "dry:copied-plain": "would copy its lyrics to the other format",
    "already-synced": "already had synced lyrics",
    "already-plain": "already had lyrics",
    "kept-existing-plain": "kept existing lyrics",
    "kept-plain-lrc": "kept its plain .lrc; this disk can't swap it for synced lyrics",
    "not-found": "no lyrics found",
    "providers-unavailable": "lyric providers unavailable",
    "write-error": "couldn't write lyrics",
    "unsafe-path": "refused an unsafe track path",
    "error": "error", "exception": "error",
    "skipped-long": "skipped (too long)",
    "skipped-tags": "skipped (missing tags)",
    "skipped-instrumental": "skipped (instrumental)",
    "skipped-read-only": "left alone (read-only)",
}


def _outcome_label(outcome, synced_only=False):
    if synced_only and outcome == "not-found":
        return "no synced lyrics found"
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

# Some tools stamp [mm:ss:xx] or [mm:ss,xx] instead of [mm:ss.xx].
SYNCED_RE = re.compile(r"\[\d{1,2}:\d{2}(?:[.:,]\d{1,3})?\]")
LRC_TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:,](\d{1,3}))?\]")

RECHECK_AFTER_DAYS = 30
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
# A search hit this close to the track's length is taken as this recording
# under the track's title, or under one that holds it or is held in it by
# the track's artist. One of another length counts only under the track's
# own title and artist.
LRCLIB_SEARCH_GRACE = 3
# LRCLIB answers a busy moment with 503, or 429 when asked too often, and a
# Retry-After of a second or so: one request in seven asked one at a time,
# a third of them or more from a full pass, and its searches more often.
# The request waits that long, LRCLIB_RETRY_WAIT when it is not told, and
# up to as long again, picked at random so the workers do not all ask
# again at once, and asks again. After LRCLIB_ATTEMPTS tries each wait
# doubles, and a request that would wait past LRCLIB_BUSY_LIMIT seconds,
# or the pass's patience when that is shorter but no shorter than
# LRCLIB_BUSY_FLOOR, or has been asked LRCLIB_MAX_ATTEMPTS times, goes
# unanswered, which is no failure for the circuit breaker. A 429 holds
# back every worker's next request too.
LRCLIB_ATTEMPTS = 8
LRCLIB_MAX_ATTEMPTS = 30
LRCLIB_BUSY_LIMIT = 120.0
LRCLIB_BUSY_FLOOR = 5.0
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
# during a state save (iter races with mutation). _breaker_lock guards the
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
    _asked.fit = None
    lrc = _fitting(get_lrc(search_term))
    if answers is not None and _ChatterCapture.count() == noted:
        answers[key] = lrc
    return lrc


_checks_log = logging.getLogger("qobuz_librarian")


# A provider's pick is held to the checks LRCLIB's search hits get: the
# track's title and artist, and its length. The track being looked up rides
# on _asked.song as (title, credited names, length), and how the record the
# provider took fits it on _asked.fit. Lyrics from a record that can't be
# told to fit are not taken.
def _fitting(lrc):
    song = getattr(_asked, "song", None)
    if lrc is None or song is None:
        return lrc
    fit = getattr(_asked, "fit", None)
    if fit not in _FITS:
        return None
    if fit != "named" or not lrc.synced:
        return lrc
    ok, reason = lrc_duration_sane(lrc.synced, song[2], other_cut=True)
    if not ok:
        _checks_log.debug("synced lyrics for %r of another length kept as "
                          "plain: %s", song[0], reason)
        return type(lrc)(synced=None,
                         unsynced=lrc.unsynced or _without_stamps(lrc.synced))
    return lrc


def _netease_fit(track, song):
    length = track.get("duration")
    return _hit_fit(track.get("name"),
                    [artist.get("name") or "" for artist in track.get("artists") or ()],
                    length / 1000 if isinstance(length, (int, float)) else None,
                    song)


def _musixmatch_fit(item, song):
    track = item.get("track") or {}
    return _hit_fit(track.get("track_name"), [track.get("artist_name") or ""],
                    track.get("track_length"), song)


_LISTED_LENGTH_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:]\d+)?\]")


def _megalobiz_fit(link, song):
    """Megalobiz lists a lyric as one line of text naming the song, its
    artist and its length."""
    text = link.get_text()
    length = _LISTED_LENGTH_RE.search(text)
    words = f" {_words(_LISTED_LENGTH_RE.sub(' ', text))} "
    key = _song_key(song[0])
    if not key or f" {key} " not in words or not any(
            f" {name} " in words for name in song[1]):
        _checks_log.debug("not taken for %r: Megalobiz %r", song[0],
                          " ".join(text.split()))
        return None
    if length and _at_length(int(length[1]) * 60 + int(length[2]), song[2]):
        return "timed"
    return "named"


def _checked_best_match(fit, best_match, results, search_term,
                        compare_key="name", min_score=65):
    """syncedlyrics' pick among a provider's search results, made only among
    those that fit the track, the best fitting first (see _hit_fit)."""
    song = getattr(_asked, "song", None)
    if song is None:
        return best_match(results, search_term, compare_key, min_score)
    fitting = {kind: [] for kind in _FITS}
    for result in results or ():
        try:
            kind = fit(result, song)
        except (AttributeError, KeyError, TypeError, ValueError):
            kind = None
        if kind:
            fitting[kind].append(result)
    for kind, found in fitting.items():
        if found:
            _asked.fit = kind
            return best_match(found, search_term, compare_key, 0)
    return None


def _note_genius_pick(response, *_args, **_kwargs):
    """Genius takes its first search hit as it is."""
    song = getattr(_asked, "song", None)
    if song is None or "/api/search" not in str(response.url):
        return
    try:
        sections = response.json()["response"]["sections"]
        hit = sections[1]["hits"][0]["result"]
        _asked.fit = _hit_fit(
            hit.get("title"),
            [(hit.get("primary_artist") or {}).get("name") or "",
             hit.get("artist_names") or ""],
            None, song)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        _asked.fit = None


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
            if str(self) == "Genius":
                self.session.hooks["response"].append(_note_genius_pick)
            self.get_lrc = functools.partial(_remembered_lrc, self, self.get_lrc)
            for h in list(self.logger.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    self.logger.removeHandler(h)

        _LRCProvider.__init__ = _quiet_lrc_init  # type: ignore[assignment]
    except Exception:
        pass

    try:
        from syncedlyrics.providers import megalobiz as _megalobiz
        from syncedlyrics.providers import musixmatch as _musixmatch
        from syncedlyrics.providers import netease as _netease

        for _module, _fit in ((_netease, _netease_fit),
                              (_musixmatch, _musixmatch_fit),
                              (_megalobiz, _megalobiz_fit)):
            _module.get_best_match = functools.partial(
                _checked_best_match, _fit, _module.get_best_match)
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
    sidecar: str = ""  # the .lrc read last time, by its file identity


_SAVED_STATUSES = frozenset({
    "", "synced", "plain", "not_found", "transient", "error", "skipped",
    "unsafe_path",
})
_SAVED_REPRESENTATIONS = frozenset({"", "embed", "sidecar", "both"})
def _track_state_from_dict(value) -> TrackState | None:
    if not isinstance(value, dict):
        return None
    mtime = value.get("mtime", 0.0)
    size = value.get("size", 0)
    last_seen = value.get("last_seen", 0.0)
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
    sidecar = value.get("sidecar", "")
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
        sidecar=sidecar if isinstance(sidecar, str) else "",
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
        self._interval = CHECKPOINT_INTERVAL_SECONDS
        self._flush_lock = threading.Lock()

    def __setitem__(self, key: str, value: TrackState) -> None:
        super().__setitem__(key, value)
        if not self.dry_run:
            self._pending[key] = replace(value)

    def save(self) -> None:
        if (self._last_write is None
                or time.monotonic() - self._last_write >= self._interval):
            self.flush()

    def flush(self) -> None:
        """Write the pending rows. Workers keep recording results meanwhile:
        they wait only for the rows to be handed over, not for the disk."""
        with self._flush_lock:
            with _state_lock:
                pending, self._pending = self._pending, {}
            if not pending:
                return
            started = time.monotonic()
            try:
                update_state(lambda disk: disk.update(pending), self.path)
            except Exception as e:
                self.log.warning("checkpoint failed (continuing): %s", e)
                with _state_lock:
                    self._pending = {**pending, **self._pending}
            self._last_write = time.monotonic()
            try:
                identity = state_file.file_identity(self.path)
            except OSError:
                identity = None
            self._interval = max(
                CHECKPOINT_INTERVAL_SECONDS,
                CHECKPOINT_WRITE_SPACING * (self._last_write - started),
                (identity[2] if identity else 0) / CHECKPOINT_BYTES_PER_SECOND)


def prune_missing(state: dict[str, TrackState]) -> int:
    """Drop entries whose file no longer exists, mutating `state` in place."""
    gone = [k for k in state if not os.path.exists(k)]
    for k in gone:
        del state[k]
    return len(gone)


def classify(text: Optional[str]) -> str:
    if not text or not text.strip():
        return "none"
    return "synced" if SYNCED_RE.search(text) else "plain"


class _Undecoded(str):
    """An .lrc file in an encoding the app can't tell, read only to see
    whether it is synced: it is never copied, replaced or removed."""


def _decode_lrc(raw: bytes) -> str:
    try:
        if raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = None
    # UTF-16 without a byte-order mark reads as UTF-8 with a NUL in every
    # other place.
    if text is None or "\x00" in text:
        return _Undecoded(
            raw.decode("utf-8", errors="replace").replace("\x00", ""))
    return text


def tag_lyric_values(parsed) -> list:
    """The lyrics in a track's tags of any format: every tag named like
    lyrics, ID3 USLT and MP4 \xa9lyr as written, and ID3 SYLT as LRC lines,
    or as plain lines when it is not timed in milliseconds."""
    tags = getattr(parsed, "tags", None)
    values = []
    for key in (tags.keys() if tags is not None else ()):
        name = str(key).lower()
        value = tags[key]
        if name.startswith("sylt"):
            timed = getattr(value, "format", None) == 2
            values.append("\n".join(
                f"[{ms // 60000:02d}:{ms % 60000 / 1000:05.2f}]{text}"
                if timed else text for text, ms in value.text))
        elif "lyrics" in name or name.startswith("uslt") or name == "\xa9lyr":
            text = getattr(value, "text", value)
            values += [str(item) for item in (
                text if isinstance(text, list) else [text])]
    return [value for value in values if value.strip()]


def embedded_lyric_values(f):
    tags = f.tags or {}
    return [value for key in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS")
            for value in tags.get(key, ()) if value.strip()]


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
    return identity_record(value) if stat.S_ISREG(value.st_mode) else None


def _guard_passes(guard) -> bool:
    if guard is None:
        return False
    try:
        return guard() is True
    except (OSError, TypeError, ValueError):
        return False


def _sidecar_key(identity) -> str:
    if not identity:
        return ""
    return ":".join(str(identity[field]) for field in (
        "device", "inode", "size", "modified_ns", "changed_ns"))


# Marks a recorded .lrc that holds synced lyrics, or is never replaced.
_SYNCED_LRC = ":synced"


def _sidecar_unchanged(path: Path, recorded: str) -> bool:
    """Whether the .lrc beside ``path`` is still the very file last read. Its
    change time moves with any write and cannot be set back."""
    if not recorded:
        return False
    try:
        current = _regular_identity(os.lstat(Path(path).with_suffix(".lrc")))
    except OSError:
        return False
    return _sidecar_key(current) == recorded.removesuffix(_SYNCED_LRC)


def _read_bound_sidecar(
        path: Path, parent_fd, parent_guard, track_guard) -> Optional[str]:
    """Read one exact regular .lrc beside one exact held track, or refuse it."""
    return _read_bound_sidecar_with_identity(
        path, parent_fd, parent_guard, track_guard)[0]


def _read_bound_sidecar_with_identity(
        path: Path, parent_fd, parent_guard, track_guard):
    """The sidecar's text, as _read_bound_sidecar reads it, and the identity
    of the file it came from."""
    if parent_fd is None:
        return None, None
    descriptor = None
    target = Path(path).with_suffix(".lrc")
    try:
        if not _guard_passes(parent_guard) or not _guard_passes(track_guard):
            return None, None
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        descriptor = os.open(target.name, flags, dir_fd=parent_fd)
        frozen = _regular_identity(os.fstat(descriptor))
        if frozen is None or _named_identity(parent_fd, target.name) != frozen:
            return None, None
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
            return None, None
        content = _decode_lrc(b"".join(chunks))
        return (content if content.strip() else None), frozen
    except (OSError, TypeError, ValueError):
        return None, None
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

        self.path = Path(os.path.abspath(os.fspath(path)))
        self.root = Path(os.path.abspath(os.fspath(owned_root)))
        try:
            relative = self.path.relative_to(self.root)
        except ValueError:
            raise OSError(
                f"{self.path}: track is outside {self.root}") from None
        if not relative.parts or any(part in ("", ".", "..")
                                     for part in relative.parts):
            raise OSError(f"{self.path}: invalid track path")

        self._directory_fds: list[int] = []
        self._directory_names: list[str] = []
        self.track_fd: Optional[int] = None
        try:
            directory_flags = (
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            root_fd = os.open(str(self.root), directory_flags)
            self._directory_fds.append(root_fd)
            if not same_directory(
                    os.fstat(root_fd),
                    os.stat(self.root, follow_symlinks=False)):
                raise OSError(f"{self.root}: folder changed while it was opened")

            for part in relative.parts[:-1]:
                child_fd = os.open(
                    part, directory_flags, dir_fd=self._directory_fds[-1])
                if not named_entry_matches(
                        self._directory_fds[-1], part, child_fd,
                        directory=True):
                    os.close(child_fd)
                    raise OSError(f"{self.path}: directory chain changed")
                self._directory_names.append(part)
                self._directory_fds.append(child_fd)

            track_flags = (
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
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
            if not same_directory(
                    os.fstat(self._directory_fds[0]),
                    os.stat(self.root, follow_symlinks=False)):
                return False
        except (OSError, IndexError):
            return False
        return all(
            named_entry_matches(
                self._directory_fds[index - 1], name,
                self._directory_fds[index], directory=True)
            for index, name in enumerate(self._directory_names, start=1))

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
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.open(str(path.parent), flags)


def _parent_is_still_named(
        parent_fd: int, path: Path, parent_guard=None) -> bool:
    if parent_guard is not None:
        try:
            return parent_guard() is True
        except (OSError, TypeError, ValueError):
            return False
    try:
        return same_directory(
            os.fstat(parent_fd), os.stat(path.parent, follow_symlinks=False))
    except OSError:
        return False


def _named_identity(parent_fd: int, name: str):
    value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return _regular_identity(value)


def _make_temp(parent_fd: int, target_name: str):
    # Fixed length: the track's own name may already be at the disk's limit.
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    for _ in range(16):
        name = f".ql-lyrics-{secrets.token_hex(8)}.tmp"
        try:
            return name, os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
    raise FileExistsError(f"couldn't reserve a temporary file for {target_name}")


_TEMP_NAME_RE = re.compile(r"\.ql-lyrics-[0-9a-f]{16}\.tmp")
# A temp changed this recently may belong to a rewrite still running.
STALE_TEMP_SECONDS = 3600


def clear_stale_temps(folders: Iterable[Path]) -> int:
    """Remove the temp copies a killed tag or sidecar rewrite left in
    ``folders``. The file each was made for keeps its own name throughout,
    so the copy is never the only one. Returns how many went."""
    removed = 0
    for folder in folders:
        try:
            with os.scandir(folder) as entries:
                names = [entry.name for entry in entries
                         if _TEMP_NAME_RE.fullmatch(entry.name)]
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            try:
                value = os.lstat(path)
            except OSError:
                continue
            if (stat.S_ISREG(value.st_mode)
                    and time.time() - value.st_ctime >= STALE_TEMP_SECONDS
                    and delete_verified(path, stat_identity(value), sync=False)):
                removed += 1
    return removed


def _fsync_temp(temp_fd: int, name: str) -> None:
    try:
        os.fsync(temp_fd)
    except OSError as exc:
        raise OSError(f"{name}: replacement couldn't be flushed to disk; "
                      "original left untouched") from exc


def _copy_file_fd(source_fd: int, target_fd: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    os.lseek(target_fd, 0, os.SEEK_SET)
    os.ftruncate(target_fd, 0)
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        write_all(target_fd, chunk)


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


def _unlink_held_name(parent_fd: int, name: str, held_fd: int) -> bool:
    """Remove `name` while it is still the file `held_fd` holds."""
    if held_fd is None:
        return False
    try:
        expected = stat_identity(os.fstat(held_fd))
    except OSError:
        return False
    return delete_verified(
        f"/proc/self/fd/{parent_fd}/{name}", expected, sync=False)


def _stable_identity(identity):
    """The fields a rename leaves alone: everything but ctime."""
    return tuple(identity[field]
                 for field in ("device", "inode", "size", "modified_ns"))


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
    """Swap the new file `temp_name` in for `target_name`, which must still be
    the file `expected_fd` holds, then remove the old one. Returns the new
    file's identity after the swap.

    A swap that does not check out afterwards is undone while both names
    still hold the two files, and raises. Once it checks out it is kept: an
    old file that cannot be removed stays under the temp name."""
    expected = expected_identity or _regular_identity(os.fstat(expected_fd))
    new = new_identity or _regular_identity(os.fstat(temp_fd))

    def guard_passes() -> bool:
        if post_exchange_guard is None:
            return True
        try:
            return post_exchange_guard() is True
        except Exception:
            return False

    if (
        expected is None
        or new is None
        or _regular_identity(os.fstat(expected_fd)) != expected
        or _named_identity(parent_fd, target_name) != expected
        or _named_identity(parent_fd, temp_name) != new
        or not guard_passes()
    ):
        raise OSError(
            f"{target_name}: target changed before atomic replacement")
    rename_exchange(parent_fd, temp_name, parent_fd, target_name)
    try:
        after = _regular_identity(os.fstat(temp_fd))
        old = _regular_identity(os.fstat(expected_fd))
        swapped = (
            after is not None
            and old is not None
            and _named_identity(parent_fd, target_name) == after
            and _stable_identity(after) == _stable_identity(new)
            and _named_identity(parent_fd, temp_name) == old
            and _stable_identity(old) == _stable_identity(expected)
        )
    except OSError:
        swapped = False
    if not swapped or not guard_passes():
        if (
            named_entry_matches(parent_fd, target_name, temp_fd)
            and named_entry_matches(parent_fd, temp_name, expected_fd)
        ):
            try:
                rename_exchange(parent_fd, temp_name, parent_fd, target_name)
            except OSError:
                pass
        raise OSError(
            f"{target_name}: target changed during atomic replacement")
    _unlink_held_name(parent_fd, temp_name, expected_fd)
    return after


class ReadOnlyTrack(OSError):
    """The track is read-only to the app, so its tags are left alone."""


def is_read_only(track_fd: int) -> bool:
    """Whether the held track has no write permission at all, or none the
    app holds."""
    mode = os.fstat(track_fd).st_mode
    return (not mode & 0o222
            or not os.access(f"/proc/self/fd/{track_fd}", os.W_OK))


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

    def still_allowed() -> bool:
        if not _parent_is_still_named(owned_parent_fd, path, parent_guard):
            return False
        if commit_guard is None:
            return True
        try:
            return commit_guard() is True
        except Exception:
            return False

    try:
        source_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        source_fd = os.open(path.name, source_flags, dir_fd=owned_parent_fd)
        before = _regular_identity(os.fstat(source_fd))
        if (
            before is None
            or (expected_identity is not None and before != expected_identity)
            or _named_identity(owned_parent_fd, path.name) != before
            or not still_allowed()
        ):
            raise OSError(f"{path.name}: source changed before tag rewrite")
        if is_read_only(source_fd):
            raise ReadOnlyTrack(f"{path.name} is read-only")

        temp_name, temp_fd = _make_temp(owned_parent_fd, path.name)
        if isinstance(directory_mutation_out, dict):
            directory_mutation_out["mutated"] = True
        _copy_file_fd(source_fd, temp_fd)
        copy_metadata(source_fd, temp_fd)
        f.save(f"/proc/self/fd/{owned_parent_fd}/{temp_name}")
        _fsync_temp(temp_fd, path.name)

        after = _exchange_existing(
            owned_parent_fd,
            temp_name,
            path.name,
            temp_fd,
            source_fd,
            before,
            post_exchange_guard=still_allowed,
        )
        temp_name = None
        _note_write(path, before, after)
        _emit_receipt(identity_change_out, {
            "path": os.path.abspath(os.fspath(path)),
            "before": before,
            "after": after,
        })
        if not fsync_dir(f"/proc/self/fd/{owned_parent_fd}"):
            raise OSError(
                f"{path.name}: replacement completed, but the folder couldn't "
                "be flushed to disk; the change may not survive a power loss")
    finally:
        if temp_name is not None:
            _unlink_held_name(owned_parent_fd, temp_name, temp_fd)
        if temp_fd is not None:
            os.close(temp_fd)
        if source_fd is not None:
            os.close(source_fd)
        os.close(owned_parent_fd)


def write_lyrics(f, content: str, *, path=None, **save_kwargs) -> None:
    # Vorbis comments are case-insensitive in mutagen, so assigning
    # "lyrics" replaces any lyrics/LYRICS value. The caller decides whether
    # that value may go; a separate UNSYNCEDLYRICS field is never touched.
    f.tags["lyrics"] = [content]
    save_flac_tags(
        f, Path(f.filename) if path is None else Path(path), **save_kwargs)


def _new_file_mode(parent_fd: int, track_name: str, temp_fd: int) -> int:
    """Permissions for a new sidecar: the track's, else what the umask allows.
    A sidecar of a track also takes its owner and group."""
    try:
        track = os.stat(track_name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISREG(track.st_mode):
            copy_owner(track, temp_fd)
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
        parent_guard=None, track_guard=None, sidecar_identity_out=None) -> bool:
    """Write lyrics to a .lrc file next to the track (UTF-8).

    A non-empty .lrc already there is replaced only when it is plain and the
    new content is synced, and never when its encoding can't be told.
    Return True when a sidecar was written and False when the existing file
    was kept."""
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
    temp_fd = None
    temp_name = None
    created_target = False
    receipt_emitted = False

    def still_allowed() -> bool:
        return (
            (track_guard is None or _guard_passes(track_guard))
            and _parent_is_still_named(owned_parent_fd, target, parent_guard)
        )

    try:
        if not still_allowed():
            raise OSError(f"{path.name}: track changed before sidecar write")
        existing_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            existing_fd = os.open(
                target.name, existing_flags, dir_fd=owned_parent_fd)
        except FileNotFoundError:
            pass
        if existing_fd is not None:
            existing = _regular_identity(os.fstat(existing_fd))
            if existing is None:
                raise OSError(f"{target.name}: existing sidecar is not a file")
            chunks = []
            while chunk := os.read(existing_fd, 1024 * 1024):
                chunks.append(chunk)
            old = _decode_lrc(b"".join(chunks))
            if old.strip() and (
                isinstance(old, _Undecoded)
                or classify(old) != "plain" or classify(content) != "synced"
            ):
                if (
                    _regular_identity(os.fstat(existing_fd)) != existing
                    or _named_identity(owned_parent_fd, target.name) != existing
                ):
                    raise OSError(
                        f"{target.name}: existing sidecar changed before keep")
                _emit_receipt(sidecar_identity_out, {
                    "path": os.path.abspath(os.fspath(target)),
                    "file": existing,
                })
                return False

        temp_name, temp_fd = _make_temp(owned_parent_fd, target.name)
        if isinstance(directory_mutation_out, dict):
            directory_mutation_out["mutated"] = True
        if existing_fd is not None:
            copy_metadata(existing_fd, temp_fd)
        else:
            os.fchmod(temp_fd, _new_file_mode(owned_parent_fd, path.name, temp_fd))
        write_all(temp_fd, content.encode("utf-8"))
        _fsync_temp(temp_fd, target.name)

        if existing_fd is None:
            rename_noreplace(
                owned_parent_fd, temp_name, owned_parent_fd, target.name)
            created_target = True
            temp_name = None
            published = _regular_identity(os.fstat(temp_fd))
            if (
                published is None
                or _named_identity(owned_parent_fd, target.name) != published
                or not still_allowed()
            ):
                raise OSError(
                    f"{target.name}: created sidecar changed after publication")
            _emit_receipt(creation_out, {
                "path": os.path.abspath(os.fspath(target)),
                "file": published,
            })
            receipt_emitted = True
            _note_write(target, None, published)
        else:
            published = _exchange_existing(
                owned_parent_fd,
                temp_name,
                target.name,
                temp_fd,
                existing_fd,
                existing,
                post_exchange_guard=still_allowed,
            )
            temp_name = None
            _note_write(target, existing, published)

        if not fsync_dir(f"/proc/self/fd/{owned_parent_fd}"):
            raise OSError(
                f"{target.name}: replacement completed, but the folder couldn't "
                "be flushed to disk; the change may not survive a power loss")
        if (
            not still_allowed()
            or _named_identity(owned_parent_fd, target.name) != published
        ):
            raise OSError(f"{target.name}: final sidecar identity changed")
        _emit_receipt(sidecar_identity_out, {
            "path": os.path.abspath(os.fspath(target)),
            "file": published,
        })
        return True
    finally:
        if created_target and not receipt_emitted:
            _unlink_held_name(owned_parent_fd, target.name, temp_fd)
        if temp_name is not None:
            _unlink_held_name(owned_parent_fd, temp_name, temp_fd)
        if temp_fd is not None:
            os.close(temp_fd)
        if existing_fd is not None:
            os.close(existing_fd)
        os.close(owned_parent_fd)


def write_output(path: Path, f, content: str, fmt: str, *, binding):
    """Persist lyrics per fmt: 'embed' (FLAC tag), 'sidecar' (.lrc), 'both'.

    The sidecar is written first, and one it keeps raises before the tag
    changes. A tag write that then fails leaves the new sidecar in place."""
    fmt = _normalise_lyrics_format(fmt)
    if not binding.exact_track_is_named():
        raise OSError(f"{path.name}: track changed before lyric write")

    if fmt in ("sidecar", "both") and not write_sidecar(
            path,
            content,
            parent_fd=binding.parent_fd,
            parent_guard=binding.chain_is_named,
            track_guard=binding.exact_track_is_named):
        raise OSError(f"{path.with_suffix('.lrc').name}: existing lyrics kept")

    if fmt == "sidecar":
        return binding.identity
    change = {}
    write_lyrics(
        f, content, path=path, identity_change_out=change,
        parent_fd=binding.parent_fd, parent_guard=binding.chain_is_named,
        expected_identity=binding.identity)
    if (
        change.get("path") != os.path.abspath(os.fspath(path))
        or change.get("before") != binding.identity
        or not _valid_regular_identity(change.get("after"))
    ):
        raise OSError(f"{path.name}: tag replacement wasn't proven")
    return change["after"]


def carry_lyrics(source: Path, source_root: Path, track: Path,
                 owned_root: Path, lyrics_format: str) -> None:
    """Give ``track`` the lyrics ``source`` holds, by the Lyrics pass's rule.

    Missing lyrics are added and plain ones give way to synced; synced ones
    stay and nothing is removed. They go where ``lyrics_format`` asks; a
    .lrc already beside the track counts as lyrics it has. Raises when they
    could not be written."""
    import mutagen

    with _BoundTrack(source, source_root) as held:
        with open(f"/proc/self/fd/{held.track_fd}", "rb") as handle:
            old_tag = _best_existing_lyrics(
                *tag_lyric_values(mutagen.File(handle)))
        old_sidecar = _read_bound_sidecar(
            source, held.parent_fd, held.chain_is_named,
            held.exact_track_is_named)
    if isinstance(old_sidecar, _Undecoded):
        old_sidecar = None
    give_lyrics(track, owned_root, _best_existing_lyrics(old_tag, old_sidecar),
                lyrics_format)


def give_lyrics(track: Path, owned_root: Path, lyrics, lyrics_format: str) -> None:
    """Give ``track`` these lyrics by the Lyrics pass's rule, as
    ``carry_lyrics`` does. Raises when they could not be written."""
    rank = ("none", "plain", "synced").index
    kind = classify(lyrics)
    if kind == "none":
        return
    places = _required_representations(lyrics_format)
    with _BoundTrack(track, owned_root) as binding:
        f = FLAC(f"/proc/self/fd/{binding.track_fd}")
        tag = get_existing_lyrics(f)
        sidecar = _read_bound_sidecar(
            track, binding.parent_fd, binding.chain_is_named,
            binding.exact_track_is_named)
        if rank(classify(_best_existing_lyrics(tag, sidecar))) >= rank(kind):
            return
        kinds = {"embed": classify(tag), "sidecar": classify(sidecar)}
        targets = [name for name in ("embed", "sidecar")
                   if name in places and rank(kinds[name]) < rank(kind)]
        write_output(track, f, lyrics,
                     "both" if len(targets) == 2 else targets[0],
                     binding=binding)


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
# Japanese releases also wrap the label in dashes or tildes: "Song -Live-",
# "Song〜Instrumental〜".
_NO_VOCAL_WORDS = (r"instrumentals?|backing track|no vocals?|off[ -]?vocal|"
                   r"karaoke|minus one|inst\.?")
_INSTRUMENTAL_RE = re.compile(
    rf"\([^()]*\b(?:{_NO_VOCAL_WORDS})(?!\w)[^()]*\)|"
    rf"\[[^\[\]]*\b(?:{_NO_VOCAL_WORDS})(?!\w)[^\[\]]*\]|"
    rf"\s+-\s+[^-]*\b(?:{_NO_VOCAL_WORDS})(?!\w)[^-]*|"
    rf"(?:^|\s)-[^-]*\b(?:{_NO_VOCAL_WORDS})(?!\w)[^-]*-\s*$|"
    rf"[~～〜][^~～〜]*\b(?:{_NO_VOCAL_WORDS})(?!\w)[^~～〜]*[~～〜]|"
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
    rf"(?:^|\s)-[^-]*\b(?:{_OTHER_CUT_WORDS})\b[^-]*-\s*$|"
    rf"[~～〜][^~～〜]*\b(?:{_OTHER_CUT_WORDS})\b[^~～〜]*[~～〜]|"
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
_LRC_LINE_RE = re.compile(r"((?:\s*\[\d{1,2}:\d{2}(?:[.:,]\d{1,3})?\])*)\s*(.*?)\s*")
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
    r"produced by))\s*[:：]|纯音乐")
# A "Credits" line opens a block that runs to the first empty line, and a
# name with a part played, "Jane Doe: lead vocals", is a credit too.
_CREDITS_RE = re.compile(r"(?i:credits?)\s*[:：]?")
_ROLE = (r"(?:lead |backing )?(?:vocals?|voice|guitars?|bass|drums|percussion|"
         r"keys|keyboards?|piano|synths?|programming|strings|horns|"
         r"mix(?:ing|ed by)?|master(?:ing|ed by)?|engineer(?:ing|ed by)?|"
         r"record(?:ing|ed by))")
_ROLE_RE = re.compile(
    rf"[^:：]{{1,40}}[:：]\s*(?i:{_ROLE}(?:\s*(?:,|&|/|and)\s*{_ROLE})*)")
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
    credits = False
    for i, line in enumerate(lines):
        stamps, body = _LRC_LINE_RE.fullmatch(line).groups()
        words = _PAGE_RE.sub("", body, count=1)
        if i == last:
            words = _EMBED_RE.sub("", words)
        if head and _CREDITS_RE.fullmatch(words):
            credits = True
            continue
        if credits:
            credits = bool(words)
            if credits:
                continue
        if (words != body and not words
                or head and (_words(words) in heading or _HEADER_RE.match(words)
                             or _ROLE_RE.fullmatch(words))
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
    waits for a provider that asked to be asked later, whether its disk can
    swap a plain .lrc for a synced one, and, while only the providers ahead
    of a paced one are asked, those providers."""

    def __init__(self, should_stop: Optional[Callable[[], bool]] = None,
                 patience: float = BUSY_PATIENCE, replaces_lrc: bool = True,
                 ahead: Optional[list[str]] = None):
        self.should_stop = should_stop
        self.patience = patience
        self.replaces_lrc = replaces_lrc
        self.ahead = ahead
        self._busy_since: dict[str, float] = {}
        self._given_up: set[str] = set()
        self._lock = threading.Lock()

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
                             song: Optional[tuple] = None, **kwargs):
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
        _asked.song = song
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
                    song: Optional[tuple] = None,
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
    one, nor must a provider the pass stopped waiting for as busy.
    """
    if _is_provider_dead(prov, log):
        return None, True
    while True:
        run.check()
        # A busy answer is no failure: the lookup waits for the provider,
        # or leaves the track for the next pass.
        if ((answers is None or (prov, query) not in answers)
                and not _take_provider_turn(prov, run, log)):
            return None, True
        result, error, chatter = _bounded_provider_search(
            query, prov, run, budget=cfg.LYRICS_PROVIDER_TIMEOUT,
            answers=answers, song=song, **kwargs
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
            log.info("disabling provider %s for %ds after %d consecutive "
                     "failures (will retry after cooldown)",
                     prov, PROVIDER_COOLDOWN_SECONDS,
                     PROVIDER_FAIL_THRESHOLD)


def _provider_answered(prov: str) -> None:
    with _breaker_lock:
        _provider_fails[prov] = 0


_http = threading.local()


def _lrclib_session() -> requests.Session:
    session = getattr(_http, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": ua_string()})
        _http.session = session
    return session


def _lrclib_request(url: str, params: dict):
    """One GET to LRCLIB. A kept-alive connection LRCLIB has since closed,
    as one left idle while NetEase's pace holds a worker can be, fails with
    a reset; the request is made once more on a new connection."""
    try:
        return _lrclib_session().get(
            url, params=params, timeout=cfg.LYRICS_PROVIDER_TIMEOUT)
    except requests.ConnectionError as e:
        if isinstance(e, requests.Timeout):
            raise
        session = getattr(_http, "session", None)
        if session is not None:
            _http.session = None
            session.close()
    return _lrclib_session().get(
        url, params=params, timeout=cfg.LYRICS_PROVIDER_TIMEOUT)


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
    busy_limit = min(LRCLIB_BUSY_LIMIT, max(LRCLIB_BUSY_FLOOR, run.patience))
    for attempt in range(1, LRCLIB_MAX_ATTEMPTS + 1):
        run.check()
        with _lrclib_busy_lock:
            held = _lrclib_busy_until
        if held - time.monotonic() > busy_limit - waited:
            break
        if held > time.monotonic():
            retry_at = max(retry_at, held + random.random())
        run.sleep_until(retry_at)
        try:
            response = _lrclib_request(url, params)
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
        if waited + pause > busy_limit:
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
    """The title's words without its version labels or apostrophes, so
    "Don't Stop (Live)" and "Dont Stop" are one song."""
    return _words(re.sub(r"\s*[\(\[][^()\[\]]*[\)\]]|\s+-\s+.*$|['’]", "",
                         title or ""))


def credited_names(*credits: str) -> frozenset[str]:
    names = {_words(part) for credit in credits
             for part in (credit, *_CREDIT_SPLIT_RE.split(credit or ""))}
    return frozenset(names - {""})


def _lrclib_search(query: str, duration: float, song: tuple[str, frozenset],
                   log: logging.Logger, run: _Pass) -> tuple[list[dict], bool]:
    """LRCLIB's search hits that can be this recording, those at its length
    under its title, or by its artist under a title that holds its title or
    is held in it, first, then those under its title and artist at any
    length. syncedlyrics would take the closest name at any length."""
    records, failed = _lrclib_get(
        LRCLIB_SEARCH_URL, {"q": query}, log, "search", list, run)
    if records is None:
        return [], failed
    fitting = {kind: [] for kind in _FITS}
    for record in records:
        if not isinstance(record, dict) or record.get("instrumental"):
            continue
        fit = _hit_fit(record.get("trackName"), [record.get("artistName") or ""],
                       record.get("duration"), (*song, duration))
        if fit:
            fitting[fit].append(record)
    return [record for found in fitting.values() for record in found], failed


def _lrclib_album_search(signature: dict, duration: float,
                         log: logging.Logger, run: _Pass) -> tuple[list[dict], bool]:
    """LRCLIB's records of the track's exact title on its album, two seconds
    either side of its length, by an artist written in another script than
    the track's, as one written in Latin letters and the other in Cyrillic."""
    records, failed = _lrclib_get(
        LRCLIB_SEARCH_URL, {"track_name": signature["track_name"],
                            "album_name": signature["album_name"]},
        log, "album search", list, run)
    title = _words(signature["track_name"])
    album = _words(signature["album_name"])
    latin = _latin(signature["artist_name"])
    return [record for record in records or ()
            if isinstance(record, dict) and not record.get("instrumental")
            and latin is not None
            and _latin(record.get("artistName")) not in (None, latin)
            and _words(record.get("trackName")) == title
            and _words(record.get("albumName")) == album
            and isinstance(record.get("duration"), (int, float))
            and abs(record["duration"] - duration) <= 2], failed


def _latin(text) -> Optional[bool]:
    """Whether every letter of ``text`` is a Latin one, or None without
    letters."""
    letters = [char for char in str(text or "") if char.isalpha()]
    if not letters:
        return None
    return all(unicodedata.name(char, "").startswith("LATIN") for char in letters)


def _at_length(length, duration) -> bool:
    return (bool(duration) and isinstance(length, (int, float))
            and abs(length - duration) <= LRCLIB_SEARCH_GRACE)


# How a record fits a track, best first.
_FITS = ("timed", "named", "loose")


def _hit_fit(name, artists, length, song) -> Optional[str]:
    """How a provider's record fits the track ``song``, its (title, credited
    names, length): "timed" by its artist at its length under its title, or
    under one that holds its title or is held in it, "named" under its
    title and by its artist at another length, "loose" under its title at
    its length by another artist, or None."""
    title, credits, duration = song
    record = name
    key = _song_key(title)
    name = _song_key(name)
    credited = credited_names(*artists) & credits
    at_length = _at_length(length, duration) and key and name
    if at_length and credited and (name == key or f" {key} " in f" {name} "
                                   or f" {name} " in f" {key} "):
        return "timed"
    if name == key and credited:
        return "named"
    if at_length and name == key:
        return "loose"
    _checks_log.debug("not taken for %r: %r by %s, %s s", title, record,
                      ", ".join(artists), length)
    return None


def _without_stamps(text: str) -> str:
    """``text`` without its LRC stamps: plain lyrics a provider made from
    synced ones keep every stamp not followed by a space."""
    return "\n".join(LRC_TS_RE.sub("", line).strip()
                     for line in text.splitlines())


def search_lyrics(
    queries: list[str], providers: list[str], duration: float,
    log: logging.Logger, skip_plain: bool = False, *,
    exact: Optional[dict] = None, other_cut: bool = False,
    song: tuple[str, frozenset] = ("", frozenset()),
    run: Optional[_Pass] = None,
) -> tuple[Optional[str], Optional[str], str, int, int]:
    """
    With Lrclib among the providers, its exact lookup by ``exact`` (see
    lrclib_signature) comes first: it matches the track's length, so its
    timing is this cut's. Each query then runs only when every provider
    missed the ones before it; ``other_cut`` holds synced results to the
    track's length. Every provider's search hits are held to the track's
    length, or to its title and artist in ``song``, and synced lyrics from a
    hit at another length to the track's length as for ``other_cut``.
    Returns (lyrics, provider_name, kind, providers_tried, failed_hard) where
    kind is 'synced', 'plain', or 'instrumental' when LRCLIB's exact record
    says the track has no words, and providers_tried counts the lookups due,
    made or skipped. failed_hard counts those that never got an answer:
    the provider raised, was refused or failed, stayed busy, or was
    skipped as disabled by the circuit breaker. Only a miss with failed_hard at 0 says
    no provider has the lyrics, so an outage doesn't poison state.
    """
    run = run or _Pass()
    answers: dict = {}
    track = (*song, duration)
    tried = 0
    hard = 0
    exact_plain = None
    exact_missed = False
    if exact and "Lrclib" in providers:
        tried += 1
        if _is_provider_dead("Lrclib", log):
            record, failed = None, True
        else:
            record, failed = _lrclib_exact(exact, log, run)
        hard += failed
        exact_missed = record is None and not failed
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
                continue
            if prov == "Lrclib":
                lrclib_hits, failed = _lrclib_search(
                    query, duration, song, log, run)
                if (exact_missed and query == queries[0]
                        and not lrclib_hits and not failed):
                    lrclib_hits, failed = _lrclib_album_search(
                        exact, duration, log, run)
                results = [(hit.get("syncedLyrics"),
                            not _at_length(hit.get("duration"), duration))
                           for hit in lrclib_hits]
            else:
                result, failed = _query_provider(
                    query, prov, log, run, answers, song=track,
                    synced_only=True)
                results = [(result, False)]
            hard += failed
            for result, held in results:
                if not result or not SYNCED_RE.search(result):
                    continue
                ok, reason = lrc_duration_sane(
                    result, duration, other_cut=other_cut or held)
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
                    query, prov, log, run, answers, song=track,
                    plain_only=True)
                hard += failed
                if result and SYNCED_RE.search(result):
                    result = _without_stamps(result)
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
    lyrics_format: str = "embed",
    synced_only: bool = False,
) -> bool:
    """
    Decide whether `path` is worth (re-)processing. mtime/size may be passed
    in by the caller (e.g. from a bulk listing) to avoid an extra path.stat().
    """
    if rescan or st is None:
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
    if st.status == "plain" and (st.source == "indexed" or _due(st)):
        # Plain lyrics are looked up again for synced ones, as soon as the
        # library index finds them and then like a track with none.
        return True
    if st.status in ("synced", "plain"):
        # A cached track stat cannot prove a sibling sidecar still exists.
        # Reopen final sidecar/both states through the held no-follow binding
        # unless the .lrc is still the very file read last time.
        # A synced track recorded without the mark may have a plain .lrc
        # beside it to upgrade.
        if ("sidecar" in _required_representations(lyrics_format)
                and (not _sidecar_unchanged(path, st.sidecar)
                     or st.status == "synced"
                     and not st.sidecar.endswith(_SYNCED_LRC))):
            return True
        return not _representations_cover(
            st.representations, lyrics_format)
    if (st.status == "not_found" and st.source == "synced-only"
            and not synced_only):
        return True
    if st.status in ("not_found", "error", "skipped"):
        # "skipped" (long-track, missing-tags) expires like not_found
        # instead of being re-opened on every run.
        return _due(st)
    return True


def _due(st: TrackState) -> bool:
    return (time.time() - st.last_seen) / 86400 >= RECHECK_AFTER_DAYS


def _commit(state: dict[str, TrackState], key: str, st: TrackState) -> None:
    with _state_lock:
        state[key] = st


def process_file(
    path: Path, state: dict[str, TrackState],
    providers: list[str], dry_run: bool, log: logging.Logger,
    owned_root: Path,
    synced_only: bool = False,
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
            lyrics_format=lyrics_format,
            rescan=rescan,
            run=run,
        )


def _process_bound_file(
    path: Path, state: dict[str, TrackState],
    providers: list[str], dry_run: bool, log: logging.Logger,
    binding: _BoundTrack,
    synced_only: bool = False,
    lyrics_format: str = "embed",
    rescan: bool = False,
    run: Optional[_Pass] = None,
) -> str:
    key = str(path)
    st = replace(state[key]) if key in state else TrackState()
    # Keep uncommitted changes out of checkpoints; previews never commit.
    if dry_run:
        def commit(*_args, **_kwargs):
            return None
    else:
        commit = _commit

    try:
        f = FLAC(f"/proc/self/fd/{binding.track_fd}")
    except Exception as e:
        log.error("FLAC open failed: %s - %s", path, e)
        st.status = "error"
        st.last_seen = time.time()
        commit(state, key, st)
        return "error"

    track_stat = os.fstat(binding.track_fd)
    # Plain lyrics looked up for synced ones lately wait like a track with
    # none, while the file stays as it was.
    plain_waits = (
        not rescan and st.status == "plain" and st.source != "indexed"
        and not _due(st)
        and int(track_stat.st_mtime) == int(st.mtime)
        and track_stat.st_size == st.size
    )
    seen = st.last_seen
    st.mtime = track_stat.st_mtime
    st.size = track_stat.st_size
    st.last_seen = time.time()

    duration = getattr(f.info, "length", 0) or 0
    if duration > MAX_TRACK_SECONDS:
        st.status = "skipped"
        st.source = "long-track"
        commit(state, key, st)
        return "skipped-long"

    if f.tags is None:
        st.status = "skipped"
        st.source = "missing-tags"
        commit(state, key, st)
        return "skipped-tags"

    title = (f.tags.get("title") or [""])[0]
    if is_instrumental(title):
        st.status = "skipped"
        st.source = "instrumental"
        commit(state, key, st)
        return "skipped-instrumental"

    required = _required_representations(lyrics_format)

    def track_guard():
        return binding.exact_track_is_named()

    embedded = get_existing_lyrics(f)
    sidecar, sidecar_identity = _read_bound_sidecar_with_identity(
        path,
        binding.parent_fd,
        binding.chain_is_named,
        track_guard,
    )
    st.sidecar = _sidecar_key(sidecar_identity)
    if not track_guard():
        log.warning("track changed during lyric discovery: %s", path)
        st.status = "transient"
        st.source = "track-changed"
        st.representations = ""
        commit(state, key, st)
        return "write-error"
    kinds = {"embed": classify(embedded), "sidecar": classify(sidecar)}
    present = {name for name, kind in kinds.items() if kind != "none"}
    if kinds["sidecar"] == "synced" or isinstance(sidecar, _Undecoded):
        st.sidecar += _SYNCED_LRC
    st.representations = _representation_state(present)

    rank = ("none", "plain", "synced").index

    def fill(kind):
        """The requested places holding weaker lyrics than ``kind``."""
        return [name for name in required if rank(kinds[name]) < rank(kind)]

    def holding(kind):
        """The places holding lyrics as good as ``kind``, as recorded for a
        track of that kind: a plain .lrc beside synced tag lyrics is still
        to be upgraded."""
        return {name for name, held in kinds.items()
                if held != "none" and rank(held) >= rank(kind)}

    if isinstance(sidecar, _Undecoded):
        kind = kinds["sidecar"]
        kinds["sidecar"] = "synced"
        sidecar = None
        if not fill("synced"):
            st.status = kind
            commit(state, key, st)
            return f"already-{kind}"
    existing = _best_existing_lyrics(embedded, sidecar)
    existing_kind = classify(existing)

    def read_only(kind):
        # Not recorded, so the track is tried again once it can be written.
        return "embed" in fill(kind) and is_read_only(binding.track_fd)

    keeps_lrc = (kinds["sidecar"] == "plain" and "sidecar" in required
                 and run is not None and not run.replaces_lrc)
    lyrics, source, kind = existing, "existing-representation", existing_kind
    if existing_kind == "synced" or (
            existing_kind == "plain" and plain_waits):
        if plain_waits:
            st.last_seen = seen
        if not fill(kind):
            st.status = kind
            st.representations = _representation_state(holding(kind))
            commit(state, key, st)
            return f"already-{kind}"
        if kind == "synced" and keeps_lrc:
            st.status = kind
            st.representations = _representation_state(holding(kind))
            commit(state, key, st)
            return "kept-plain-lrc"
        if read_only(kind):
            return "skipped-read-only"
        action = f"copied-{kind}"
    elif keeps_lrc:
        st.status = "plain"
        st.source = "kept-existing"
        commit(state, key, st)
        return "kept-plain-lrc"
    else:
        if read_only("synced"):
            return "skipped-read-only"
        query = build_query(f)
        if not query:
            st.status = "skipped"
            st.source = "missing-tags"
            commit(state, key, st)
            return "skipped-tags"

        # Plain lyrics already there are only replaced by synced ones. The
        # providers ahead of a paced one settle a track alone only with
        # synced lyrics they find first, as the full lookup would.
        ahead = run.ahead if run is not None else None
        found, prov, found_kind, tried, failed_hard = search_lyrics(
            [query] if ahead else [query, *fallback_queries(f)],
            ahead or providers, duration, log,
            skip_plain=bool(ahead) or synced_only or existing_kind == "plain",
            exact=lrclib_signature(f, duration),
            other_cut=is_other_cut(title),
            song=(title, credited_names(*f.tags.get("artist", ()),
                                        *f.tags.get("albumartist", ()))),
            run=run,
        )
        if found_kind == "instrumental":
            st.status = "skipped"
            st.source = "instrumental"
            commit(state, key, st)
            return "skipped-instrumental"
        found = clean_lyrics(found, title) if found else None
        if ahead and not found:
            return "deferred"
        if found:
            lyrics, source, kind = found, prov, found_kind
            action = f"wrote-{kind}"
        elif failed_hard or not tried:
            # A provider did not answer for this file, whether disabled by
            # the circuit breaker or failing on the connection, and may have
            # had its lyrics, or none is set. That is no verdict about the track: "not found"
            # here would suppress it for RECHECK_AFTER_DAYS.
            st.status = "transient"
            commit(state, key, st)
            return "providers-unavailable"
        elif existing_kind == "none":
            st.status = "not_found"
            st.source = "synced-only" if synced_only else ""
            commit(state, key, st)
            return "not-found"
        elif synced_only or not fill(kind):
            st.status = "plain"
            st.source = "kept-existing"
            commit(state, key, st)
            return "kept-existing-plain"
        else:
            action = "copied-plain"
    targets = fill(kind)
    write_format = "both" if len(targets) == 2 else targets[0]
    if action == "wrote-synced" and any(kinds[name] == "plain" for name in targets):
        action = "upgraded-synced"

    if dry_run:
        return f"dry:{action}"

    try:
        final_identity = write_output(
            path, f, lyrics, write_format, binding=binding)
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
    if not _valid_regular_identity(final_identity):
        log.error("write completed without an exact file identity: %s", path)
        st.status = "error"
        st.representations = ""
        commit(state, key, st)
        return "write-error"
    st.mtime = final_identity["modified_ns"] / 1_000_000_000
    st.size = final_identity["size"]
    st.representations = _representation_state(holding(kind).union(targets))
    commit(state, key, st)
    return action


# A pass's time left is told once it has run this long, and logged this
# often.
ETA_AFTER_SECONDS = 30
ETA_LOG_SECONDS = 300


def _time_left(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute left"
    if minutes < 60:
        return f"about {plural(minutes, 'minute')} left"
    hours, minutes = divmod(5 * round(minutes / 5), 60)
    return f"about {hours} h {minutes} min left" if minutes else f"about {hours} h left"


class _Progress:
    """How far a pass has got over ``total`` tracks and roughly how long it
    has left. With ``paced`` set, the tracks the providers ahead of it leave
    take at least its interval each."""

    def __init__(self, total: int, paced: Optional[str], *, split: bool):
        self.total = total
        self.paced = paced
        self.split = split
        self.started = self.logged = time.monotonic()
        self.done = 0
        self.deferred = 0
        self.second: Optional[tuple[float, int]] = None

    def seconds_left(self) -> Optional[float]:
        now = time.monotonic()
        interval = PROVIDER_MIN_INTERVAL.get(self.paced, 0.0)
        if self.second is not None:
            began, done_then = self.second
            finished = self.done - done_then
            left = self.total - self.done
            return (left * (now - began) / finished if finished >= 5
                    else left * interval)
        visited = self.done + self.deferred
        elapsed = now - self.started
        if elapsed < ETA_AFTER_SECONDS or visited < 10:
            return None
        left = (self.total - visited) * elapsed / visited
        if self.split:
            left += self.deferred * self.total / visited * interval
        return left

    def note(self) -> str:
        left = self.seconds_left()
        parts = []
        if self.paced and (self.second is not None or not self.split):
            parts.append(f"waiting on {self.paced}, "
                         f"{60 / PROVIDER_MIN_INTERVAL[self.paced]:.0f} "
                         "lookups a minute")
        if left is not None:
            parts.append(_time_left(left))
        return " · ".join(parts)

    def due_line(self) -> Optional[str]:
        """A line for the log every ETA_LOG_SECONDS, once the time left can
        be told."""
        now = time.monotonic()
        if self.seconds_left() is None or now - self.logged < ETA_LOG_SECONDS:
            return None
        self.logged = now
        return (f"{self.done} of {plural(self.total, 'track')} checked; "
                f"{self.note().replace(' · ', '; ')}.")


def _drain(items, worker, on_result, *, workers, should_stop,
           thread_name_prefix, log):
    """Run ``worker`` over ``items`` on a thread pool and hand each result to
    ``on_result`` as it lands. Returns True when ``should_stop`` ended the run
    before every item was submitted."""
    stopped = False
    worker = wrap_thread_target(worker)
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix=thread_name_prefix) as ex:
        remaining = iter(items)
        futures = {}

        def submit_available():
            nonlocal stopped
            while not stopped and len(futures) < workers:
                if should_stop and should_stop():
                    stopped = True
                    return
                try:
                    item = next(remaining)
                except StopIteration:
                    return
                futures[ex.submit(worker, item)] = item

        submit_available()
        try:
            while futures:
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for fut in done:
                    item = futures.pop(fut)
                    try:
                        outcome = fut.result()
                    except Exception as e:
                        log.exception("worker raised on %s: %s", item, e)
                        outcome = "exception"
                    on_result(item, outcome)
                if not stopped and should_stop and should_stop():
                    stopped = True
                submit_available()
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            raise
    return stopped


def fetch_for_paths(
    paths: Iterable[Path],
    providers: Optional[list[str]] = None,
    state_path: Path = DEFAULT_STATE_FILE,
    dry_run: bool = False,
    rescan: bool = False,
    log: Optional[logging.Logger] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    workers: int = 8,
    synced_only: bool = False,
    lyrics_format: str = "embed",
    progress_cb: Optional[Callable[[int, int, str, str], None]] = None,
    *,
    owned_root: Path,
    busy_patience: float = BUSY_PATIENCE,
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

    `progress_cb` gets the tracks done, their total, the track last seen
    and a note of the paced provider being waited on and the time left.

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

    paths = [Path(p) for p in paths]
    state = _StateWriter(state_path, log, dry_run=dry_run)
    candidates = [p for p in paths
                  if should_process(p, state.get(str(p)), rescan,
                                    lyrics_format=lyrics_format,
                                    synced_only=synced_only)]

    counts: Counter = Counter()
    total = len(candidates)
    workers = max(1, int(workers))
    if candidates and not providers:
        # Every track is then left retryable, never marked not found.
        log.warning("No known lyric provider is set in LYRICS_PROVIDERS; "
                    "no lyrics were fetched.")

    found = disk_support.support(owned_root)
    # A paced provider is asked only about the tracks the providers ahead
    # of it could not settle, once they have been through every track.
    paced = next((i for i, prov in enumerate(providers)
                  if prov in PROVIDER_MIN_INTERVAL), 0)
    run = _Pass(should_stop, busy_patience,
                replaces_lrc=found is None or found.exchange,
                ahead=providers[:paced] or None)
    deferred: list[Path] = []

    def run_one(fp: Path) -> Optional[str]:
        try:
            outcome = process_file(
                fp, state, providers, dry_run, log,
                owned_root,
                synced_only=synced_only,
                lyrics_format=lyrics_format,
                rescan=rescan,
                run=run,
            )
        except _Stopped:
            return None
        except Exception as e:
            log.exception("unexpected error on %s: %s", fp, e)
            outcome = "exception"
        return outcome

    progress = _Progress(
        total, next((prov for prov in providers
                     if prov in PROVIDER_MIN_INTERVAL), None),
        split=run.ahead is not None)

    def take(fp, outcome):
        if outcome is None:
            return
        # Every submitted worker is drained, and counted unless a stop cut
        # its lookups short. Once a stop is observed no replacement work is
        # queued, so a returned summary cannot omit a worker that may write.
        # The first part counts every track it has been through, and the
        # part for a paced provider the tracks left to it.
        try:
            label = _track_label(fp, owned_root)
            if progress.second is None:
                at, of = progress.done + progress.deferred + 1, total
            else:
                at, of = progress.done - progress.second[1] + 1, len(deferred)
            if outcome == "deferred":
                deferred.append(fp)
                progress.deferred += 1
                said = f"left for {progress.paced}"
            else:
                progress.done += 1
                counts[outcome] += 1
                said = _outcome_label(outcome, synced_only)
                state.save()
            log.info("[%d/%d] %s - %s", at, of, said, label)
            if line := progress.due_line():
                log.info(line)
            if progress_cb:
                progress_cb(at, of, label, progress.note())
        except Exception as e:
            log.exception("post-process error on %s: %s", fp, e)

    try:
        stopped = _drain(candidates, run_one, take, workers=workers,
                         should_stop=should_stop, thread_name_prefix="lyrics",
                         log=log)
        if deferred and not stopped:
            order = {fp: i for i, fp in enumerate(candidates)}
            deferred.sort(key=order.__getitem__)
            run.ahead = None
            progress.second = (time.monotonic(), progress.done)
            log.info("%s left for %s; %s is asked at most %.0f times a minute.",
                     plural(len(deferred), "track"),
                     " and ".join(providers[paced:]), providers[paced],
                     60 / PROVIDER_MIN_INTERVAL[providers[paced]])
            stopped = _drain(deferred, run_one, take, workers=workers,
                             should_stop=should_stop,
                             thread_name_prefix="lyrics", log=log)
    finally:
        state.flush()

    if stopped:
        counts["stopped"] = 1
        counts["stop-total"] = total
        if run.ahead is not None and deferred:
            counts["stop-deferred"] = len(deferred)
    return counts


def index_existing(
    paths: Iterable[Path],
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

    Files for which `state` already records a matching mtime+size are
    skipped without opening the FLAC, so re-running --index after adding
    new files is cheap.
    """
    if log is None:
        log = logging.getLogger("lyric_fetch")
    if not AVAILABLE:
        log.warning("lyric_fetch unavailable: %s", IMPORT_ERROR)
        return Counter({"unavailable": 0})

    normalized = [Path(p) for p in paths]

    state = _StateWriter(state_path, log)
    counts: Counter = Counter()
    total = len(normalized)
    workers = max(1, int(workers))

    def index_one(fp: Path) -> str:
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
            if (cached is not None
                    and int(mtime) == int(cached.mtime)
                    and size == cached.size
                    and cached.status in (
                        "synced", "plain", "not_found", "skipped")
                    and (
                        cached.status not in ("synced", "plain")
                        or cached.representations
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
            )
            _commit(state, key, st)
            return f"indexed-{kind}"

    log.info("indexing %d files with %d workers (no provider calls)",
             total, workers)
    completed = 0

    def take(fp, outcome):
        nonlocal completed
        completed += 1
        counts[outcome] += 1
        if progress_cb:
            progress_cb(completed, total, fp.name)
        state.save()

    try:
        stopped = _drain(normalized, index_one, take, workers=workers,
                         should_stop=should_stop,
                         thread_name_prefix="lyrics-idx", log=log)
    finally:
        state.flush()

    if stopped:
        counts["stopped"] = 1
        counts["stop-total"] = total
    return counts
