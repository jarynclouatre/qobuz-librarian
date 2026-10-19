"""Background job system for the web UI.

Two job shapes share one worker and one log-streaming mechanism:

* **Simple job**: `submit(job, fn)`. Runs `fn(job)` to completion. Used for a
  single-album download.
* **Scan / review / execute job**: `submit_scan(job, scan_fn, execute_fn)`.
  `scan_fn(job)` inspects the library/catalog and attaches *candidates*. The
  job then parks in `AWAITING_REVIEW` so the user can pick which candidates to
  act on in the web UI. `approve(job, ids)` resumes it and runs
  `execute_fn(job, selected)` on the worker. This is the backbone of the
  artist / library / repair / upgrade flows, which are interactive by nature
  and can't just stream a terminal prompt to a browser.

Progress is captured from the shared ``qobuz_librarian`` logger and streamed
to connected SSE clients.
"""
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from qobuz_librarian import config as cfg
from qobuz_librarian import download_result, post_job_hook, redaction
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api.auth import (
    AuthLost,
    CredentialChanged,
    DownloaderNotReady,
    DownloadsRefused,
    NoCredsError,
    QobuzEntitlementError,
    QobuzError,
    QobuzUnavailable,
)
from qobuz_librarian.completion import (
    CompletionOrigin,
    CompletionOriginKind,
    RecoveryOwner,
    normalise_album_id,
)
from qobuz_librarian.integrations import beets
from qobuz_librarian.integrations import rip as rip_module
from qobuz_librarian.library import library_scan_state, scan_checkpoint, scanner
from qobuz_librarian.library.candidate_premise import CandidateStale
from qobuz_librarian.post_job_hook import release_title
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import set_progress_reporter, set_thread_wrapper
from qobuz_librarian.web import job_persistence, review_badges, storage

log = logging.getLogger("qobuz_librarian")

# Thread-local pointer to the job currently being run on this worker.
_TLS = threading.local()
_settings_admission_lock = threading.RLock()


@contextmanager
def settings_admission_guard():
    """Serialize live config apply with both worker-lane RUNNING claims."""
    with _settings_admission_lock:
        yield


def _current_job_cancel_requested() -> bool:
    j = getattr(_TLS, "current_job", None)
    return bool(j and j.cancel_requested)


def _current_scan_cancel_requested() -> bool:
    """A crawl only reads, so its Qobuz calls can stop as soon as it is
    cancelled; a download's calls run to their end."""
    j = getattr(_TLS, "current_job", None)
    return bool(j and j.cancel_requested and j.status is JobStatus.SCANNING)


def current_job_id() -> str | None:
    """Return the exact Web job currently owned by this worker thread."""
    job = getattr(_TLS, "current_job", None)
    return (
        job.id
        if type(job) is Job and isinstance(job.id, str) and job.id
        else None
    )


def current_job_album_id() -> str | None:
    """Return the album bound to the current exact Web job, when present."""
    job = getattr(_TLS, "current_job", None)
    return (
        job.album_id
        if type(job) is Job
        and isinstance(job.album_id, str)
        and job.album_id
        else None
    )


def acknowledge_current_job_durable_completion(
    origin,
    owner,
    *,
    album_id,
    completion_hash,
    planned,
    post_dir,
) -> bool:
    """Persist completion only for this worker's exact single-album job."""
    job = getattr(_TLS, "current_job", None)
    planned_album = (
        planned.get("album") if isinstance(planned, dict) else None
    )
    if (
        type(job) is not Job
        or type(origin) is not CompletionOrigin
        or origin.kind is not CompletionOriginKind.WEB_JOB
        or origin.reference != job.id
        or type(owner) is not RecoveryOwner
        or normalise_album_id(album_id) != album_id
        or normalise_album_id(job.album_id) != album_id
        or normalise_album_id(
            planned_album.get("id")
            if isinstance(planned_album, dict)
            else None
        ) != album_id
        or type(post_dir) is not str
        or not os.path.isabs(post_dir)
        or "\x00" in post_dir
    ):
        return False
    return job_persistence.acknowledge_durable_completion(
        job.id,
        owner,
        album_id=album_id,
        completion_hash=completion_hash,
    )


def _adopt_current_job(job):
    """Pool initializer that attributes a worker's log lines to its job."""
    _TLS.current_job = job


def pool_initializer_kwargs() -> dict:
    """kwargs for a ThreadPoolExecutor created INSIDE a job's worker thread, so
    its workers inherit this job via _TLS.current_job."""
    return {"initializer": _adopt_current_job,
            "initargs": (getattr(_TLS, "current_job", None),)}


def _propagate_job_to_thread(target):
    """Wrap a helper-thread target so it inherits the SPAWNING thread's
    current_job. Used for rip.py's output-reader thread: it logs streamrip's
    live progress + per-track errors via the shared logger, and JobLogHandler
    routes records by thread, so without carrying the job onto that thread its
    lines would be dropped. current_job is captured here, on the spawning
    (worker) thread; the wrapped target re-applies it on the helper thread."""
    job = getattr(_TLS, "current_job", None)

    def _wrapped(*args, **kwargs):
        _TLS.current_job = job
        return target(*args, **kwargs)

    return _wrapped


rip_module.set_cancel_check(_current_job_cancel_requested)
api_client.set_stop_check(_current_scan_cancel_requested)
# Carry the running job onto subprocess-reader helper threads (rip + beets) so
# their live-output log lines reach the right job instead of being dropped by
# JobLogHandler's per-thread routing.
set_thread_wrapper(_propagate_job_to_thread)


def _report_progress_to_current_job(phase, current, total, item):
    j = getattr(_TLS, "current_job", None)
    if j is not None:
        j.push_progress(phase, current, total, item)


set_progress_reporter(_report_progress_to_current_job)


def _report_import_window_to_current_job(active):
    j = getattr(_TLS, "current_job", None)
    if j is not None:
        j.set_importing(active)


beets.set_import_window_reporter(_report_import_window_to_current_job)


class JobStatus(str, Enum):
    PENDING         = "pending"
    SCANNING        = "scanning"
    AWAITING_REVIEW = "awaiting_review"
    RUNNING         = "running"
    DONE            = "done"
    FAILED          = "failed"
    CANCELED        = "canceled"


TERMINAL = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELED)
ACTIVE = (JobStatus.PENDING, JobStatus.SCANNING,
          JobStatus.AWAITING_REVIEW, JobStatus.RUNNING)

JOB_ADMISSION_ERROR = (
    "This job couldn't be saved to the data folder, so no work was started. "
    "Check the data volume, then restart Qobuz Librarian."
)
JOB_FINAL_SAVE_ERROR = (
    "The work finished, but the app couldn't save the final job result. "
    "Check the data volume before retrying or clearing this job."
)

# Distinguishes an intact review whose last eligible tick disappeared from a
# stale/non-review job and from a durable admission failure.
APPROVAL_NO_SELECTION = object()

# Sentinel sent to live SSE subscribers to close the current phase's stream.
STREAM_END = "__DONE__"

# Sentinel prefix marking a fanned-out line as a structured progress update
# (phase + counts) rather than log output.
PROGRESS_PREFIX = "\x00PROGRESS\x00"

# Sentinel fanned out when a review's selection or candidate set changes, so
# other open tabs of the same review refresh live.
REVIEW_CHANGED = "\x00REVIEW\x00"
_MISSING = object()

# What each kind of job is called once its review is approved and it starts
# doing the work. One job carries the scan and the run it was approved for, so
# a page open on the run must not still be headed with the scan's name.
RUN_TITLES = {
    "library": "Library download",
    "new_releases": "New releases download",
    "upgrade": "Upgrade run",
    "downsample": "Downsample run",
    "repair": "Repair",
}
# The name a run takes back on returning to review when its own review's
# title was not saved with it.
REVIEW_TITLES = {
    "library": "Library scan",
    "new_releases": "New releases",
    "upgrade": "Albums to upgrade",
    "downsample": "Albums to downsample",
    "repair": "Repair scan",
}


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Job:
    id: str           = field(default_factory=_new_id)
    _retired: bool    = field(default=False, init=False, repr=False)
    title: str        = ""
    artist: str       = ""
    album_id: str     = ""
    edition: str      = ""
    # Single-track download undo info, set by the Get-track flow: the resolved
    # album dir, the downloaded track's isrc/number, and its exact
    # file/directory ownership record, enough for /undo to cleanly reverse
    # it.
    single: dict      = field(default_factory=dict)
    # Set once this download has put the whole album (or the requested track)
    # on disk, so a search row that launched it can say "Owned" the moment
    # the job ends instead of offering the same download again.
    landed_complete: bool = False
    kind: str         = "download"          # download | scan
    status: JobStatus = JobStatus.PENDING
    phase: str        = ""                  # "", scan, execute
    # Live progress header (separate from the log): "Scanning 430/1882 · Beyoncé"
    progress_phase: str   = ""
    progress_current: int = 0
    progress_total: int   = 0
    progress_item: str    = ""
    progress_found: int   = 0   # running tally of results found so far
    # Artists with at least one find so far, the server-side count behind the
    # scan page's "across N artists".
    progress_found_artists: int = 0
    # What current/total are counting, so the UI can label a bare "12 / 350"
    # ("artist", "album", "track").
    progress_unit: str    = ""
    log_lines: list   = field(default_factory=list)
    # Review candidates: each is a dict
    #   {cid, kind, title, artist, detail, payload, selected}
    candidates: list  = field(default_factory=list)
    error: Optional[str] = None
    # Short, user-facing outcome shown as a callout on the finished job page,
    # e.g. why a scan found nothing, so it isn't buried in the log.
    summary: str = ""
    # Set when a finished job needs the user's eyes, e.g. a download that
    # stayed under the quality target after the automatic retry.
    attention: str = ""
    quality_shortfall: dict = field(default_factory=dict)
    # Tracks a download that imported still lacks. It outlives the attention
    # marker, which clears once the user has seen the job.
    tracks_missing: int = 0
    # Exact retained recovery files.
    recoveries: list = field(default_factory=list)
    # The verb the review screen's submit button uses. Most jobs download what
    # you approve; the migration job copies (or moves), so it overrides this.
    review_verb: str = "Download"
    # How to rebind execute_fn after a container restart.
    execute_kind: str = ""
    execute_args: dict = field(default_factory=dict)
    execute_args_unreadable: bool = False
    cancel_requested: bool = False
    # Set when the app is stopping. The job winds down as it would for a
    # cancel, but its saved row still says it was running, so the next start
    # reports it as interrupted by the restart.
    stopping_for_restart: bool = False
    # A finished Library scan's artist folders and whether it looked at Gap
    # Fill alone, so its fold can drop what it no longer offers for them.
    scan_coverage: tuple | None = field(default=None, repr=False)
    # A finished Library scan's artist folders whose finds the review had no
    # room for, with how many each lost.
    scan_left_out: dict = field(default_factory=dict, repr=False)
    # True only while beets is putting an album into the library. That runs to
    # its own end, so a cancel arriving now cannot stop it.
    importing: bool = False
    created_at: float = field(default_factory=time.time)
    # When an approved review joined the back of the scan lane.
    queued_at: Optional[float] = field(default=None, repr=False)
    # When the job actually started working (left the queue).
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    _execute_fn: Optional[Callable] = field(default=None, repr=False)
    _subscribers: list = field(default_factory=list, repr=False)
    # Reentrant because a save that fails under it logs, and this job's own
    # log handler takes it again to append the line.
    _lock: object = field(default_factory=threading.RLock, repr=False)
    _review_action_lock: object = field(
        default_factory=threading.RLock,
        repr=False,
    )
    # Monotonic candidate counter.
    _cand_seq: int = field(default=0, repr=False)
    # Counts the snapshots taken for saving, so an older one is not written
    # over a newer one.
    _persist_serial: int = field(default=0, repr=False)
    # Set once a scan hits JOB_CANDIDATE_CAP and stops listing further finds.
    _candidate_cap_noted: bool = field(default=False, repr=False)
    # Rows added under release types the review does not list, which the cap
    # leaves out of its count.
    _unlisted_rows: int = field(default=0, repr=False)
    # Set by the execute flows the moment the first album/track lands on disk.
    _imported_any: bool = field(default=False, repr=False)
    # How many artists a library scan couldn't check (per-artist errors).
    _unchecked_artists: int = field(default=0, repr=False)
    _preserve_persisted_single: bool = field(default=False, repr=False)
    _single_undo_unavailable: bool = field(default=False, repr=False)
    # Set on a run this start found cut short, so only it hears which of its
    # albums the start-up puts back.
    _cut_short_this_start: bool = field(default=False, repr=False)
    # Set only by a successful jobs.db admission or restart restore. Tests and
    # diagnostics also create intentionally in-memory Jobs directly.
    _durability_required: bool = field(default=False, repr=False)
    # When set to (current, total, unit), push_progress reports THESE counts
    # in place of the caller's, so a multi-item execute (repairing 16 albums)
    # keeps the progress card on "3 / 16 albums" even while each album's inner
    # phases (download, import, downsample) report their own 1 / 1.
    _progress_scope: Optional[tuple] = field(default=None, repr=False)

    def __post_init__(self):
        self.sync_cand_seq()

    @property
    def interrupted_by_restart(self) -> bool:
        return job_persistence.interrupted_by_restart(
            self.status.value, self.summary, self.error)

    @property
    def display_title(self) -> str:
        return release_title(self.title, self.edition)

    def sync_cand_seq(self):
        """Advance the cid counter past any candidates this job already carries.

        A job rebuilt with pre-existing candidates (restart rehydration, the
        tab-split) would otherwise mint fresh cids from c0 and collide with
        the rows it inherited. A cid-keyed dismiss then deletes unrelated
        candidates. Must be re-run by any code that assigns ``candidates``
        after construction."""
        highest = -1
        for c in self.candidates:
            seq = c.get("seq")
            if seq is None:
                # Legacy rows persisted before seq existed: recover it from
                # the cid ("c57" → 57).
                cid = str(c.get("cid") or "")
                try:
                    seq = int(cid.lstrip("c"))
                except ValueError:
                    continue
            if seq > highest:
                highest = seq
        if highest >= self._cand_seq - 1:
            self._cand_seq = highest + 1

    def _fan_out(self, line: str):
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    # A consumer that fell behind (throttled tab, slow link):
                    # drop its oldest buffered line to keep the live tail
                    # and the closing STREAM_END flowing rather than
                    # freezing the stream.
                    try:
                        q.get_nowait()
                        q.put_nowait(line)
                    except (queue.Empty, queue.Full):
                        pass

    # Bound long job logs before they dominate process memory.
    LOG_CAP = cfg.JOB_LOG_CAP

    _LOG_SLACK = 1000
    _TRUNCATION_MARKER = "[… earlier output truncated to bound memory …]"
    # LOG_CAP bounds memory; this one bounds the archive. PERSIST_KEEP holds
    # 1000 finished rows, so keeping LOG_CAP lines each would put hundreds of
    # MB in jobs.db to answer what the tail almost always answers.
    LOG_PERSIST_CAP = 500
    _PERSIST_TRUNCATION_MARKER = "[… earlier output not kept on disk …]"
    # Strip C0 control bytes except \t (\x09) and \n (\x0a). A stray NUL or
    # ESC byte from streamrip/beets truncates some browsers' SSE display and
    # garbles the JSON status endpoint.
    _CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

    def _trim_log_lines_locked(self) -> None:
        """Bound the live tail while preserving the newest line.

        The caller holds ``self._lock``.  A one-line cap cannot also carry a
        truncation marker, so the real latest line wins in that configuration.
        """
        if len(self.log_lines) <= self.LOG_CAP + self._LOG_SLACK:
            return
        del self.log_lines[:len(self.log_lines) - self.LOG_CAP]
        if self.LOG_CAP > 1:
            self.log_lines[0] = self._TRUNCATION_MARKER

    def push_line(self, line: str):
        """Append a real log line and stream it to live subscribers."""
        # Masked here as well as in the logger filter: a line pushed straight
        # in never passes through logging, and this is the funnel every one of
        # them reaches before it is streamed, stored or replayed.
        line = redaction.redact(self._CTRL_RE.sub("", line))
        # A file name that is not valid UTF-8 arrives with surrogates, which
        # the page and the saved log cannot encode.
        line = line.encode("utf-8", "backslashreplace").decode("utf-8")
        # Mutate log_lines under the lock: subscribe()/the status API read it
        # under the same lock from request threads, and the truncation `del`
        # below would otherwise tear a concurrent reader's slice.
        with self._lock:
            self.log_lines.append(line)
            self._trim_log_lines_locked()
        self._fan_out(line)

    def persisted_log_lines_locked(self) -> list:
        """The tail of the log to keep on disk, marked if anything was cut."""
        if len(self.log_lines) <= self.LOG_PERSIST_CAP:
            return list(self.log_lines)
        kept = list(self.log_lines[-self.LOG_PERSIST_CAP:])
        kept[0] = self._PERSIST_TRUNCATION_MARKER
        return kept

    def end_stream(self):
        """Close the current phase's live stream without storing a marker."""
        self._fan_out(STREAM_END)

    def push_progress(self, phase, current=0, total=0, item="", found=0,
                      hit=None, unit=""):
        """Update the live progress header (phase + counts) and stream it as a
        distinct event. Kept out of log_lines so it never clutters the log or
        gets replayed line-by-line on reconnect; the latest snapshot is re-sent
        once when a new subscriber attaches.

        ``found`` is a running tally (e.g. albums with gaps so far); ``hit`` is
        a one-off {artist, albums} the scanning page appends to its live preview
        It is deliberately left out of the stored snapshot so a reconnect
        replay doesn't duplicate a preview row. ``unit`` names what current/total
        count ("artist"/"album"/"track") so the UI can label the bare numbers."""
        # An execute loop can pin the counts to an overall scope (e.g. 3 of 16
        # albums finished) so inner per-album phases don't reset the card to
        # 1/1. The count is work completed, never work begun: counting the
        # album being written left the card reading "2 / 2" for the whole of
        # the second album.
        scope = self._progress_scope
        if scope is not None:
            current, total, unit = scope
        # Write the snapshot fields under the lock so a reconnecting subscriber
        # reading them via _progress_snapshot() (also under the lock) can't catch
        # a torn mix, e.g. the new phase label with the previous phase's counts.
        with self._lock:
            self.progress_phase = phase
            self.progress_current = current
            self.progress_total = total
            self.progress_item = item
            self.progress_found = found
            self.progress_unit = unit
            if hit and hit.get("albums"):
                self.progress_found_artists += 1
            found_artists = self.progress_found_artists
            importing = self.importing
        payload = {"phase": phase, "current": current, "total": total,
                   "item": item, "found": found}
        if importing:
            payload["importing"] = True
        # Optional keys mirror `hit`: kept out of the payload when unset so the
        # common case stays lean and existing consumers see no new field.
        if unit:
            payload["unit"] = unit
        if hit:
            payload["hit"] = hit
        if found_artists:
            payload["found_artists"] = found_artists
        self._fan_out(PROGRESS_PREFIX + json.dumps(payload))

    def notify_review_changed(self, origin: str = ""):
        """Tell every open review tab that the selection changed."""
        self._fan_out(REVIEW_CHANGED + origin)

    def set_importing(self, active: bool) -> None:
        """Open or close the window where a cancel can no longer be honoured.

        Re-sends the current progress line so an open page withdraws (or
        restores) its Cancel button as the import starts and ends, rather than
        offering a stop the import will run straight past.
        """
        with self._lock:
            if self.importing == bool(active):
                return
            self.importing = bool(active)
            scope = self._progress_scope
            # The stop holds back the albums a run has yet to start.
            if (not active and self.stopping_for_restart and scope
                    and scope[0] + 1 < scope[1]):
                self.cancel_requested = True
            snapshot = self._progress_snapshot()
        self._fan_out(snapshot)

    def _progress_snapshot(self) -> str:
        snap = {
            "phase": self.progress_phase, "current": self.progress_current,
            "total": self.progress_total, "item": self.progress_item,
            "found": self.progress_found}
        if self.progress_unit:
            snap["unit"] = self.progress_unit
        if self.progress_found_artists:
            snap["found_artists"] = self.progress_found_artists
        if self.importing:
            snap["importing"] = True
        return PROGRESS_PREFIX + json.dumps(snap)

    # Cap replay so a late subscriber doesn't get thousands of historical
    # lines blasted at them (and so the bounded queue isn't filled by history
    # alone, which would silently drop live lines).
    REPLAY_TAIL = cfg.JOB_LOG_REPLAY_TAIL

    def subscribe(self) -> "queue.Queue[str]":
        """Return a queue that replays the recent history then receives
        future lines. Only the last REPLAY_TAIL lines are replayed; for
        long-running jobs, full history is available on the job page from
        ``job.log_lines``.

        Snapshot + register is done under the lock so a push_line racing
        with subscribe doesn't drop a live line on the floor between the
        history snapshot and the subscriber appearing in the fan-out set.
        """
        q: queue.Queue[str] = queue.Queue(maxsize=2000)
        with self._lock:
            # Leave at least one slot for the progress snapshot below, so a
            # large REPLAY_TAIL can't fill the queue with history alone and
            # drop the reconnect header.
            tail = min(self.REPLAY_TAIL, q.maxsize - 1)
            for line in (self.log_lines[-tail:] if tail > 0 else []):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    break
            # Re-send the current progress once so a reconnect/new tab shows the
            # live header immediately instead of a blank bar until the next tick.
            if self.progress_phase:
                try:
                    q.put_nowait(self._progress_snapshot())
                except queue.Full:
                    pass
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]"):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    CANDIDATE_CAP = cfg.JOB_CANDIDATE_CAP

    def add_candidate(self, kind, title, artist="", detail="", payload=None,
                      selected=True, listed=True):
        # Locked: a live scan appends from the worker thread while the request
        # thread may be reading/dropping candidates via the review screen.
        capped = False
        cid = None
        with self._lock:
            # Bound the in-memory (and persisted) candidate list so a runaway
            # whole-library scan can't exhaust memory. Only the rows the review
            # lists count: switched-off release types are kept for their
            # switches without taking a listed album's place.
            if (listed and len(self.candidates) - self._unlisted_rows
                    >= self.CANDIDATE_CAP):
                capped = not self._candidate_cap_noted
                self._candidate_cap_noted = True
                if isinstance(self.execute_args, dict):
                    self.execute_args["_candidate_cap_hit"] = True
            else:
                # Append regardless of whether the cap was hit EARLIER: a
                # mid-scan hide can shrink the list back below the cap, and a
                # candidate we actually appended must return its real cid, not
                # be swallowed as None by the sticky _candidate_cap_noted flag.
                seq = self._cand_seq
                self._cand_seq += 1
                self.candidates.append({
                    "cid": f"c{seq}", "seq": seq, "kind": kind, "title": title,
                    "artist": artist, "detail": detail, "payload": payload or {},
                    "selected": selected,
                })
                cid = f"c{seq}"
                if not listed:
                    self._unlisted_rows += 1
        if capped:  # log once, outside the lock (push_line takes it itself)
            self.push_line(
                f"Reached the {self.CANDIDATE_CAP:,}-result cap. Further "
                "finds aren't listed. Work through or dismiss some results, "
                "then refresh to see the rest.")
        return cid

    @property
    def candidate_cap_hit(self) -> bool:
        """True once a scan hit CANDIDATE_CAP and stopped listing further finds,
        so summaries can say results were truncated instead of implying the full
        found-count is reviewable."""
        saved = (
            self.execute_args.get("_candidate_cap_hit", False)
            if isinstance(self.execute_args, dict)
            else False
        )
        return self._candidate_cap_noted or saved is True

    @property
    def unchecked_artists(self) -> int:
        """Number of artists an incomplete scan could not check."""
        if self._unchecked_artists > 0:
            return self._unchecked_artists
        saved = (
            self.execute_args.get("_unchecked_artists", 0)
            if isinstance(self.execute_args, dict)
            else 0
        )
        return saved if type(saved) is int and saved > 0 else 0

    def selected_candidates(self) -> list:
        return [c for c in self.candidates if c.get("selected")]

    def set_selected(self, cid: str, on: bool) -> Optional[bool]:
        """Flip one candidate's selected flag while the review is still live.

        Returns None once the job has left review, False for no change, and
        True when one row changed.
        """
        with self._review_action_lock, self._lock:
            if self.status != JobStatus.AWAITING_REVIEW:
                return None
            for c in self.candidates:
                if c.get("cid") == cid:
                    changed = bool(c.get("selected")) != bool(on)
                    c["selected"] = bool(on)
                    return changed
        return False

    def selection_counts(self) -> dict:
        """Authoritative review tallies for the UI, computed server-side so a
        paginated page (which holds only some checkboxes) still shows the true
        whole-set numbers. ``reclaimable`` sums est_saving over selected
        candidates for the downsample review's "space reclaimed" figure."""
        with self._lock:
            cands = list(self.candidates)
        selected = [c for c in cands if c.get("selected")]
        artists = {c.get("artist") for c in cands}
        reclaimable = sum(est_saving(candidate) for candidate in selected)
        return {
            "total": len(cands),
            "artists": len(artists),
            "selected": len(selected),
            "reclaimable": reclaimable,
        }


def est_saving(candidate) -> int:
    """The space a downsample candidate frees, in bytes."""
    try:
        value = int((candidate.get("payload") or {}).get("est_saving") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, value)


class JobRegistry:
    """In-memory store for all jobs, bounded to the last N finished jobs."""

    MAX_FINISHED = 50
    # A finished job a client is still streaming is kept past MAX_FINISHED
    # until the stream closes, so a slow viewer isn't 404'd.
    _PINNED_MAX_AGE_SEC = 3600

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def add(self, job: Job):
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
        job_persistence.persist(job)
        job_persistence.prune_finished(self.PERSIST_KEEP)

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def discard_merged_review(self, job: Job) -> None:
        """Forget a split remnant already removed by one database commit."""
        with self._lock:
            if self._jobs.get(job.id) is not job:
                return
            self._jobs.pop(job.id, None)
            self._order = [job_id for job_id in self._order
                           if job_id != job.id]

    def all(self) -> list[Job]:
        with self._lock:
            return [self._jobs[jid] for jid in self._order if jid in self._jobs]

    def pending_and_running(self) -> list[Job]:
        return [j for j in self.all() if j.status in ACTIVE]

    def awaiting_review(self) -> list[Job]:
        return [j for j in self.all()
                if j.status == JobStatus.AWAITING_REVIEW]

    def executing(self) -> list[Job]:
        """Jobs actively reading cfg/touching files RIGHT NOW (not merely queued
        or parked)."""
        return [j for j in self.all()
                if j.status in (JobStatus.RUNNING, JobStatus.SCANNING)]

    def finished(self) -> list[Job]:
        return [j for j in self.all() if j.status in TERMINAL]

    PERSIST_KEEP = job_persistence.FINISHED_HISTORY_KEEP

    def _prune_locked(self):
        """Evict the oldest finished jobs past MAX_FINISHED. Caller holds _lock.

        Keeps the most recently finished. A restart rehydrates the on-disk
        archive in creation order, so ordering by finish time (not insertion)
        is what makes "the last N finished" hold there too. The SQLite row
        stays after eviction so /jobs/{id} can still render the archive view;
        only "Clear finished" deletes from disk (see clear_finished)."""
        finished = [self._jobs[jid] for jid in self._order
                    if (jid in self._jobs
                        and self._jobs[jid].status in TERMINAL
                        # Its worker has yet to save the final result.
                        and self._jobs[jid].finished_at is not None
                        and not self._jobs[jid].recoveries)]
        excess = len(finished) - self.MAX_FINISHED
        if excess <= 0:
            return
        finished.sort(key=lambda j: j.finished_at or j.created_at)
        now = time.time()
        for job in finished:
            if excess <= 0:
                break
            # Don't evict a job a client is still streaming/viewing. That
            # would 404 it out from under them; it's pruned once the stream
            # closes.
            age = now - (job.finished_at or job.created_at)
            if job._subscribers and age < self._PINNED_MAX_AGE_SEC:
                continue
            self._order.remove(job.id)
            self._jobs.pop(job.id, None)
            excess -= 1

    def clear_finished(self):
        """Drop ordinary terminal jobs from memory after durable clearing."""
        with self._lock:
            keep = [jid for jid in self._order
                    if (self._jobs.get(jid)
                        and (self._jobs[jid].status not in TERMINAL
                             or self._jobs[jid].finished_at is None
                             or self._jobs[jid].recoveries
                             or job_persistence.has_active_single_undo(
                                 self._jobs[jid].single)))]
            dropped = [jid for jid in self._jobs.keys() if jid not in keep]
            for jid in dropped:
                self._jobs.pop(jid, None)
            self._order = keep


class JobLogHandler(logging.Handler):
    """Routes records from the shared qobuz_librarian logger to a Job."""

    _ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, job: Job):
        self.job = job
        super().__init__()

    def createLock(self):
        # The job's own lock, so a thread that logs while holding it never
        # waits on a second lock held by another thread writing to this job.
        self.lock = self.job._lock

    def filter(self, record: logging.LogRecord):
        # Both worker lanes (download + scan) attach a handler to the SAME
        # process-global "qobuz_librarian" logger, and Python dispatches every
        # record to EVERY handler regardless of the emitting thread. The
        # filter runs before the lock is taken, so other threads' records
        # never touch this job's lock.
        if getattr(_TLS, "current_job", None) is not self.job:
            return False
        if getattr(record, "file_only", False):
            return False
        return super().filter(record)

    def emit(self, record: logging.LogRecord):
        try:
            self.job.push_line(self._ANSI.sub("", self.format(record)))
        except Exception:
            self.handleError(record)


registry = JobRegistry()


def prepare_recovery_resolution(location, receipt):
    """Seal archived Repair records before a manual backup Restore."""
    return job_persistence.prepare_recovery_resolution(location, receipt)


def resolve_recovery_resolution(plan) -> bool:
    """Durably retire a restored carrier, then refresh any live history row."""
    job_ids = (
        {job_id for job_id, _raw in plan.rows}
        if type(plan) is job_persistence.RecoveryResolutionPlan
        else set()
    )
    live_jobs = sorted(
        (job for job in registry.all() if job.id in job_ids),
        key=lambda job: job.id,
    )
    for job in live_jobs:
        job._lock.acquire()
    try:
        # A full persist takes the same job lock before the database lock.
        updates = job_persistence.resolve_recovery_resolution(plan)
        if updates is None:
            return False
        live_by_id = {job.id: job for job in live_jobs}
        for job_id, state in updates.items():
            job = live_by_id.get(job_id)
            if job is None:
                continue
            job.recoveries = state["recoveries"]
            job.attention = state["attention"]
        return True
    finally:
        for job in reversed(live_jobs):
            job._lock.release()

# Undo and backup Restore mutate the library directly from request workers
# rather than through JobRegistry.
_library_operations: dict[str, str] = {}
_library_operations_condition = threading.Condition()
_library_operations_accepting = True


def begin_library_operation(label: str) -> Optional[str]:
    token = uuid.uuid4().hex
    with _library_operations_condition:
        if not _library_operations_accepting:
            return None
        _library_operations[token] = str(label)
    return token


def end_library_operation(token: str) -> None:
    with _library_operations_condition:
        _library_operations.pop(token, None)
        _library_operations_condition.notify_all()


def active_library_operations() -> list[str]:
    with _library_operations_condition:
        return list(_library_operations.values())


def work_in_flight() -> bool:
    """Whether a job is running or queued to run, other than a review or a
    held download, or a library operation is open."""
    return any(
        job.status != JobStatus.AWAITING_REVIEW and not is_held(job)
        for job in registry.pending_and_running()
    ) or bool(active_library_operations())

# Review ticks save the whole candidate list, and on a big library that list
# runs to tens of megabytes. Written per checkbox, each tap waited on a
# multi-hundred-ms serialize+write. A save that took a while waits this many
# times as long before the next, so a run of ticks is not answered behind one
# save after another.
_PERSIST_DELAY = 1.5
_PERSIST_SPACING = 10
_persist_timers: dict[str, threading.Timer] = {}
_persist_delays: dict[str, float] = {}
_persist_timers_lock = threading.Lock()


def _flush_soon(job) -> None:
    with _persist_timers_lock:
        if _persist_timers.pop(job.id, None) is None:
            return
    # A job pruned or dismissed while the timer ran must not be written
    # back; restore would resurrect a review the user already discarded.
    if registry.get(job.id) is not job:
        return
    started = time.monotonic()
    saved = job_persistence.persist(job)
    with _persist_timers_lock:
        _persist_delays[job.id] = max(
            _PERSIST_DELAY, _PERSIST_SPACING * (time.monotonic() - started))
    if not saved:
        # The request already returned before this background write ran.
        # Tell every open copy of the review that its latest tick is only
        # in memory and may disappear on a restart.
        job.notify_review_changed("save_failed")


def persist_soon(job) -> None:
    with _persist_timers_lock:
        if job.id in _persist_timers:
            return
        t = threading.Timer(
            _persist_delays.get(job.id, _PERSIST_DELAY), _flush_soon, (job,))
        t.daemon = True
        _persist_timers[job.id] = t
        t.start()


def flush_pending_saves() -> None:
    """Save every review whose ticks are still waiting for their save."""
    with _persist_timers_lock:
        waiting = list(_persist_timers.values())
    for timer in waiting:
        timer.cancel()
        _flush_soon(*timer.args)


# Two worker lanes so a quick single-album download isn't stuck behind a scan
# whose execute phase is downloading 30 albums.
_download_worker_thread: Optional[threading.Thread] = None
_scan_worker_thread: Optional[threading.Thread] = None
_download_queue: "queue.Queue" = queue.Queue()
_scan_queue: "queue.Queue" = queue.Queue()
_stop_event = threading.Event()
_restart_stop = threading.Event()
_worker_lifecycle_lock = threading.Lock()
_POST_JOB_HOOK_WORKERS = 4
_POST_JOB_HOOK_QUEUE_CAP = job_persistence.FINISHED_HISTORY_KEEP
_post_job_hook_queue: "queue.Queue[dict]" = queue.Queue(
    maxsize=_POST_JOB_HOOK_QUEUE_CAP)
_post_job_hook_threads: set[threading.Thread] = set()
_post_job_hook_threads_lock = threading.Lock()
_staging_entry_guard_lock = threading.Lock()
_staging_entry_guard: Callable[[Optional[Job]], bool] | None = None
_held_downloads_lock = threading.Lock()
_held_downloads: list[tuple[Job, Callable]] = []
_held_release_check: Callable[[], bool] | None = None
# Set while downloads wait because Qobuz rejected the token ("rejected") or
# could not be reached ("unreachable").
_qobuz_hold: dict | None = None


class StagingRecoveryBlocked(RuntimeError):
    """The staging mutex was reached after durable recovery became active."""


class WaitForQobuz(Exception):
    """A download found Qobuz unavailable before it touched anything."""


def configure_staging_entry_guard(
        guard: Callable[[Optional[Job]], bool] | None) -> None:
    """Set the check run only after exclusive staging ownership is obtained."""
    if guard is not None and not callable(guard):
        raise TypeError("staging entry guard must be callable or None")
    global _staging_entry_guard
    with _staging_entry_guard_lock:
        _staging_entry_guard = guard


class _StagingMutex:
    """Lock-compatible staging gate with a post-acquire recovery check."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if not acquired:
            return False
        with _staging_entry_guard_lock:
            guard = _staging_entry_guard
        if guard is None:
            return True
        try:
            allowed = guard(getattr(_TLS, "current_job", None)) is True
        except BaseException:
            self._lock.release()
            raise
        if allowed:
            return True
        self._lock.release()
        if not blocking:
            return False
        raise StagingRecoveryBlocked(
            "Library work paused for an interrupted download's exact recovery."
        )

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self):
        if not self.acquire():
            raise StagingRecoveryBlocked(
                "Library work paused for an interrupted download's exact recovery."
            )
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


# Mutual exclusion around the actual rip+import work.
_staging_lock = _StagingMutex()


def staging_lock():
    """Return the staging-mutex object so callers can ``with staging_lock():``."""
    return _staging_lock


class StagingWaitCancelled(Exception):
    """A cancel arrived while the job waited for the staging lock."""


@contextmanager
def staging_lock_unless_cancelled(job):
    """The staging lock for a job that has changed nothing yet; a cancel
    while it waits behind another run ends the wait."""
    mutex = staging_lock()
    while not mutex.acquire(timeout=0.5):
        if job.cancel_requested:
            raise StagingWaitCancelled()
    try:
        yield mutex
    finally:
        mutex.release()


# Human label for a long staging-lock hold. A library-wide Lyrics scan holds
# the mutex for its whole (possibly hours-long) run, not per album.
_staging_holder: Optional[str] = None
_staging_holder_lock = threading.Lock()


def set_staging_holder(label: Optional[str]) -> None:
    global _staging_holder
    with _staging_holder_lock:
        _staging_holder = label or None


def staging_holder() -> Optional[str]:
    with _staging_holder_lock:
        return _staging_holder


def qobuz_action_error_message(exc, *, unchanged=False) -> str:
    """Short action-gate copy shared by scans and review approvals."""
    if isinstance(exc, NoCredsError):
        message = "Connect Qobuz in Settings."
    elif isinstance(exc, DownloadsRefused):
        message = str(exc)
    elif isinstance(exc, AuthLost):
        message = "Qobuz rejected the saved token. Reconnect in Settings."
    elif isinstance(exc, DownloaderNotReady):
        message = (
            "Your Qobuz token works, but downloads also need your Qobuz "
            "user ID. Add it in Settings."
        )
    elif isinstance(exc, CredentialChanged):
        message = "Qobuz credentials changed while this was starting. Try again."
    elif isinstance(exc, QobuzEntitlementError):
        message = "Your Qobuz account cannot perform this action."
    elif isinstance(exc, QobuzUnavailable):
        message = str(exc)
    else:
        message = "Qobuz could not be reached. Try again."
    if unchanged:
        message += " Nothing changed."
    return message


def _friendly_job_error(exc, fallback: str) -> str:
    """Map common worker failures to a short user-facing summary.

    The raw error text remains in job.log_lines for the expandable log;
    job.error is what the red banner shows."""
    if isinstance(exc, NoCredsError):
        return "No Qobuz credentials set. Visit Settings."
    if isinstance(exc, DownloadsRefused):
        return str(exc)
    if isinstance(exc, AuthLost):
        return "Token is expired or invalid. Update it in Settings."
    if isinstance(exc, QobuzUnavailable):
        return str(exc)
    if isinstance(exc, QobuzError):
        return "Qobuz answered with an error. The job log has the response."
    if isinstance(exc, CandidateStale):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        # job.error is rendered through Jinja autoescape, so don't escape here
        # too (that double-encodes characters like & in a path).
        fname = str(exc.filename) if exc.filename else "see log"
        return f"Required tool or path missing; see log ({fname})."
    if isinstance(exc, beets.MusicDiskFull):
        return f"{exc.strerror}."
    if isinstance(exc, OSError):
        import errno
        if exc.errno == errno.ENOSPC:
            return "Out of disk space. Free space and retry."
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return ("Permission denied writing to the staging or music dir. "
                    "Check PUID/PGID match the volume owner.")
    return fallback


def _mark_final_save_failure(job: Job) -> None:
    """Make an unsaved terminal result visibly conservative in memory."""
    prior_error = job.error
    job.status = JobStatus.FAILED
    job.error = (
        f"{prior_error} {JOB_FINAL_SAVE_ERROR}".strip()
        if prior_error
        else JOB_FINAL_SAVE_ERROR
    )


def _finish_task_phase(job: Job) -> None:
    """Durably close one worker phase after its final status is known."""
    if job.stopping_for_restart and job.status is JobStatus.CANCELED:
        job_persistence.record_stop(job)
        job.end_stream()
        return
    if job.status in TERMINAL and job.finished_at is None:
        job.finished_at = time.time()
    if job.status is JobStatus.FAILED and not job.attention:
        # Without this a failure is silent everywhere except the job's own
        # page: the queue badge simply drops by one, and someone watching
        # another page is left with whatever it said before.
        job.attention = "failed"
    saved = True
    if registry.get(job.id) is not None:
        saved = job_persistence.persist(job)
    if not saved and job.status == JobStatus.AWAITING_REVIEW:
        # The review remains usable in memory, but its exact candidate set may
        # not survive a restart. Tell every open review before closing the
        # worker stream so the operator gets the same durable-write warning as
        # a failed tick or fold.
        job.notify_review_changed("save_failed")
    elif (
        not saved
        and job.status in TERMINAL
        and job._durability_required
    ):
        # The work may already have had its intended effect, so retain its
        # truthful summary, but do not show a green/canceled terminal state
        # that exists only in memory.  A restart will recover the older
        # durable row; the live page must be at least as cautious.
        _mark_final_save_failure(job)
    if job.status in TERMINAL or (
            job.status is JobStatus.AWAITING_REVIEW and job.phase == "scan"):
        _start_post_job_hook_for_job(job)
    job.end_stream()


def _run_task(job: Job, fn):
    """Run one phase of a job with log capture and status bookkeeping."""
    handler = JobLogHandler(job)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    _TLS.current_job = job
    scanner.forget_named_paths()
    try:
        fn(job)
        if (job.cancel_requested and job.status not in TERMINAL
                and job.status != JobStatus.AWAITING_REVIEW):
            # A cooperative fn returned early on the cancel flag.
            job.status = JobStatus.CANCELED
        # fn may have parked the job in AWAITING_REVIEW (scan with results);
        # only auto-complete a job that's still RUNNING.
        elif job.status == JobStatus.RUNNING:
            job.status = JobStatus.DONE
        if (job.cancel_requested and job.status == JobStatus.DONE
                and not job.stopping_for_restart):
            # The work finished before the stop could take effect. Saying only
            # "Done" would hide that a cancel was asked for and ignored.
            job.attention = job.attention or "cancel_late"
    except BaseException as e:  # noqa: BLE001 - the worker must stay durable
        if isinstance(e, WaitForQobuz):
            if job.cancel_requested and not job.stopping_for_restart:
                job.status = JobStatus.CANCELED
                return
            if _wait_for_qobuz(job, fn, e.__cause__):
                return
            e = e.__cause__
        if isinstance(e, StagingWaitCancelled):
            job.status = JobStatus.CANCELED
            return
        if job.cancel_requested and isinstance(e, QobuzUnavailable):
            # Stopped while waiting on Qobuz, which a cancel cuts short.
            job.status = JobStatus.CANCELED
            return
        if isinstance(e, StagingRecoveryBlocked) and job.album_id:
            if job.cancel_requested and not job.stopping_for_restart:
                job.status = JobStatus.CANCELED
                return
            if _wait_for_recovery(job, fn):
                return
        # SystemExit and other fatal boundaries must surface as failed jobs,
        # not escape after the still-RUNNING state was persisted.
        raw = str(e) or e.__class__.__name__
        # The failure text becomes job.error, which is stored and shown; an
        # aiohttp error names the signed-in URL it could not reach.
        cleaned = redaction.redact(JobLogHandler._ANSI.sub("", raw).strip())
        job.status = JobStatus.FAILED
        job.error = _friendly_job_error(e, cleaned)
        job.push_line(f"[ERROR] {cleaned}")
        if isinstance(e, beets.MusicDiskFull):
            storage.note_music_disk_full()
        if (job.album_id and isinstance(e, (AuthLost, QobuzUnavailable))
                and not isinstance(e, DownloadsRefused)):
            _start_qobuz_hold(e)
    finally:
        _TLS.current_job = None
        log.removeHandler(handler)
        # Persist before the hook starts so durability never waits on a webhook.
        _finish_task_phase(job)


def _review_items(job) -> int:
    """How many items a parked review lists, leaving out Library picks a
    switch that is off holds back."""
    if job.execute_kind != "library":
        return len(job.candidates)
    from qobuz_librarian.web import flows
    shown = flows.review_listed()
    return sum(1 for c in job.candidates if shown(c))


def _post_job_hook_payload(job):
    """The hook's report of a job: finished, parked for review, or cut short
    by a restart, which reads ``interrupted`` so it is not taken for a
    cancel and carries the restart line History shows as its summary."""
    with job._lock:
        review = job.status is JobStatus.AWAITING_REVIEW
        interrupted = job.interrupted_by_restart
        restart_line = (job.summary if job.status is JobStatus.CANCELED
                        else job.error)
        return post_job_hook.payload(
            event_id=job.id,
            status="interrupted" if interrupted else job.status.value,
            title=job.title,
            edition=job.edition,
            artist=job.artist,
            error=job.error,
            finished_at=time.time() if review else job.finished_at,
            tracks_missing=job.tracks_missing,
            kind=job.execute_kind or "download",
            summary=restart_line if interrupted else job.summary,
            review_items=_review_items(job) if review else 0,
        )


def _start_post_job_hook_for_job(job):
    _start_post_job_hook(_post_job_hook_payload(job))


def _fire_post_job_hook(payload):
    post_job_hook.run(payload)


def _post_job_hook_worker():
    while True:
        payload = _post_job_hook_queue.get()
        try:
            _fire_post_job_hook(payload)
        except Exception:
            log.exception("post-job hook worker failed")
        finally:
            _post_job_hook_queue.task_done()


def _ensure_post_job_hook_workers() -> bool:
    with _post_job_hook_threads_lock:
        while len(_post_job_hook_threads) < _POST_JOB_HOOK_WORKERS:
            thread = threading.Thread(
                target=_post_job_hook_worker,
                args=(),
                daemon=True,
                name="post-job-hook",
            )
            try:
                thread.start()
            except RuntimeError as exc:
                log.warning(
                    "post-job hook thread could not start: %s", exc)
                break
            _post_job_hook_threads.add(thread)
        return bool(_post_job_hook_threads)


def _start_post_job_hook(payload):
    if not os.environ.get("POST_JOB_HOOK", "").strip():
        return
    if not _ensure_post_job_hook_workers():
        return
    try:
        _post_job_hook_queue.put_nowait(payload)
    except queue.Full:
        log.warning(
            "post-job hook queue is full; notification for %s was skipped",
            payload.get("id") or "a finished job",
        )


def _wait_for_post_job_hooks():
    """Wait at most one hook timeout for notifications already in flight."""
    deadline = time.monotonic() + float(cfg.POST_JOB_HOOK_TIMEOUT) + 1.0
    while _post_job_hook_queue.unfinished_tasks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "%s still pending at shutdown",
                plural(_post_job_hook_queue.unfinished_tasks,
                       "post-job hook notification"),
            )
            return
        time.sleep(min(0.05, remaining))


def fire_auth_lost_hook():
    """Report one healthy-to-rejected Qobuz token transition asynchronously.

    The payload keeps the normal hook's core fields so existing scripts can
    render it without special-casing.
    """
    payload = post_job_hook.payload(
        event_id="qobuz-auth",
        status="auth_lost",
        title="Qobuz token rejected: replace it on the Settings page",
        error="Qobuz rejected the saved token",
        finished_at=time.time(),
    )
    _start_post_job_hook(payload)


def _worker_loop(work_queue: "queue.Queue"):
    while not _stop_event.is_set():
        if work_queue is _download_queue:
            _release_held_downloads()
        try:
            job, fn = work_queue.get(timeout=1)
        except queue.Empty:
            # Idle tick: apply any deferred settings change so `current()`
            # on the Settings page reflects reality even when no new job
            # has been submitted.
            try:
                from qobuz_librarian.web import settings_store
                # Only drain when nothing is active anywhere: the OTHER worker
                # lane may be mid-job, and applying a deferred quality/config
                # change on this idle tick is exactly the mid-job mutation
                # save() deferred to prevent.
                with settings_admission_guard():
                    if not settings_store._any_active_job():
                        settings_store.drain_pending()
            except Exception:
                log.exception("couldn't apply deferred settings while idle")
            continue
        if _restart_stop.is_set():
            # Stopping: leave it queued. Its saved row still says pending, so
            # the next start queues it again.
            work_queue.put((job, fn))
            work_queue.task_done()
            break
        # Settings changes deferred while we were busy are applied BEFORE the
        # next job so "apply now" takes effect for it, but NOT while the
        # OTHER lane is mid-execution.
        # Sole worker thread. Catching BaseException ensures one
        # crashed job can't take down the whole queue.
        try:
            if (work_queue is _download_queue
                    and job.status is JobStatus.PENDING
                    and _hold_for_qobuz(job, fn)):
                continue
            canceled_pending = False
            with settings_admission_guard():
                try:
                    from qobuz_librarian.web import settings_store
                    if not registry.executing():
                        settings_store.drain_pending()
                except Exception:
                    log.exception("couldn't apply deferred settings before job %s", job.id)
                with job._lock:
                    if job.cancel_requested or job.status != JobStatus.PENDING:
                        # A queued cancel and this worker claim use the same
                        # lock. Exactly one side can move PENDING onward.
                        if job.status == JobStatus.PENDING:
                            job.status = JobStatus.CANCELED
                            job.finished_at = time.time()
                            canceled_pending = True
                        claimed = False
                    else:
                        claimed = True
                        job.status = JobStatus.RUNNING
                        if job.started_at is None:
                            job.started_at = time.time()
            if canceled_pending:
                _finish_canceled(job)
            elif claimed:
                # Recheck after the queue wait.
                if not job_persistence.admit(job):
                    with job._lock:
                        job.status = JobStatus.FAILED
                        job.error = JOB_ADMISSION_ERROR
                        job.finished_at = time.time()
                    _finish_task_phase(job)
                else:
                    _run_task(job, fn)
        except BaseException as e:  # noqa: BLE001 - must not die
            try:
                if job.status not in TERMINAL:
                    job.status = JobStatus.FAILED
                    import traceback as _tb
                    summary = _tb.format_exception_only(type(e), e)[-1].strip()
                    job.error = f"Worker crash: {summary}. Restart the job."
                log.exception(
                    "worker: job %s crashed hard", job.id)
                _finish_task_phase(job)
            except Exception:
                log.exception("couldn't finalize crashed job %s", job.id)
        finally:
            try:
                work_queue.task_done()
            except ValueError:
                pass


def start_worker():
    global _download_worker_thread, _scan_worker_thread
    global _library_operations_accepting
    with _worker_lifecycle_lock:
        with _library_operations_condition:
            _library_operations_accepting = True
        _stop_event.clear()
        _restart_stop.clear()
        if not (_download_worker_thread and _download_worker_thread.is_alive()):
            _download_worker_thread = threading.Thread(
                target=_worker_loop, args=(_download_queue,), daemon=True,
                name="job-worker-download")
            _download_worker_thread.start()
        if not (_scan_worker_thread and _scan_worker_thread.is_alive()):
            _scan_worker_thread = threading.Thread(
                target=_worker_loop, args=(_scan_queue,), daemon=True,
                name="job-worker-scan")
            _scan_worker_thread.start()


def stop_for_restart() -> None:
    """Start winding down because the app is stopping.

    Nothing more leaves the queues, and each running job stops as it would
    for a cancel. A run importing an album stops once that import ends, or
    finishes if it was the last album; a single download already importing
    finishes and is recorded as it ends.
    """
    _restart_stop.set()
    _stop_event.set()
    for job in registry.executing():
        with job._lock:
            # A cancel already asked for stays a cancel, and a run already
            # set to stop after its import keeps to that.
            if (job.status in TERMINAL or job.cancel_requested
                    or job.stopping_for_restart):
                continue
            if job.importing:
                if not job.album_id:
                    job.stopping_for_restart = True
                continue
            job.stopping_for_restart = True
            job.cancel_requested = True


def stop_worker():
    """Stop both lanes and wait for every request-owned library write."""
    global _download_worker_thread, _scan_worker_thread
    global _library_operations_accepting
    with _worker_lifecycle_lock:
        # Serialise the admission gate with start_worker().
        with _library_operations_condition:
            _library_operations_accepting = False
        _stop_event.set()
        workers = tuple(
            worker for worker in (
                _download_worker_thread,
                _scan_worker_thread,
            )
            if worker is not None
        )
        for worker in workers:
            worker.join()
        _download_worker_thread = None
        _scan_worker_thread = None
        # Keep start_worker() outside the lifecycle boundary until every
        # already-admitted request write has drained as well.
        with _library_operations_condition:
            _library_operations_condition.wait_for(
                lambda: not _library_operations)
        _wait_for_post_job_hooks()


def submit(job: Job, fn) -> Optional[Job]:
    """Queue a simple job. fn(job) runs to completion on the download worker."""
    # A fresh durable Web album binds its completion acknowledgement to this
    # exact jobs.db row.
    if not job_persistence.admit(job):
        return None
    registry.add(job)
    _download_queue.put((job, fn))
    return job


def configure_held_release(check: Callable[[], bool] | None) -> None:
    """Set the check that says held downloads may take their turn."""
    if check is not None and not callable(check):
        raise TypeError("held release check must be callable or None")
    global _held_release_check
    with _held_downloads_lock:
        _held_release_check = check


def qobuz_hold() -> dict | None:
    """Why downloads wait for Qobuz and how many do, or None."""
    with _held_downloads_lock:
        if _qobuz_hold is None:
            return None
        waiting = sum(1 for job, _run in _held_downloads
                      if job.status is JobStatus.PENDING)
        return {**_qobuz_hold, "waiting": waiting}


def is_held(job: Job) -> bool:
    """Whether a download sits with the held ones, waiting for Qobuz or for
    a paused library, rather than in the work queue."""
    with _held_downloads_lock:
        return any(held is job for held, _run in _held_downloads)


def waiting_for_qobuz(job: Job) -> bool:
    with _held_downloads_lock:
        return (_qobuz_hold is not None
                and job.status is JobStatus.PENDING
                and any(held is job for held, _run in _held_downloads))


def release_qobuz_hold() -> None:
    """Let the downloads held for Qobuz take their turn again."""
    global _qobuz_hold
    with _held_downloads_lock:
        _qobuz_hold = None


def _start_qobuz_hold(exc) -> None:
    global _qobuz_hold
    with _held_downloads_lock:
        _qobuz_hold = {
            "reason": "rejected" if isinstance(exc, AuthLost) else "unreachable",
        }


def _hold_in_order(job: Job, run) -> None:
    """Add a download to the held ones in the order downloads were queued.
    The caller holds ``_held_downloads_lock``."""
    at = next((n for n, (held, _run) in enumerate(_held_downloads)
               if held.created_at > job.created_at), len(_held_downloads))
    _held_downloads.insert(at, (job, run))


def _hold_for_qobuz(job: Job, fn) -> bool:
    """Set a download aside while Qobuz is unavailable.

    It is rebuilt from its saved album so it runs with whatever credentials
    are saved when its turn comes.
    """
    if _qobuz_hold is None or not job.album_id:
        return False
    from qobuz_librarian.web import job_runs
    run = job_runs._requeued_download_run(job) or fn
    with _held_downloads_lock:
        if _qobuz_hold is None:
            return False
        _hold_in_order(job, run)
    return True


def _back_to_waiting(job: Job) -> None:
    with job._lock:
        job.status = JobStatus.PENDING
        job.started_at = None


def _wait_for_qobuz(job: Job, fn, exc) -> bool:
    """Return a download that never began to the queue, behind the hold every
    later download waits on too. False when it must fail instead."""
    _start_qobuz_hold(exc)
    if not _hold_for_qobuz(job, fn):
        return False
    _back_to_waiting(job)
    job.push_line(f"Waiting for Qobuz: {qobuz_action_error_message(exc)}")
    return True


def _wait_for_recovery(job: Job, fn) -> bool:
    """Return a download that reached the staging door while downloads are
    paused to the held downloads, which start once the pause lifts."""
    _back_to_waiting(job)
    with _held_downloads_lock:
        _hold_in_order(job, fn)
    return True


def _release_held_downloads() -> None:
    with _held_downloads_lock:
        if not _held_downloads:
            return
        check = _held_release_check
        qobuz_held = _qobuz_hold is not None
    if qobuz_held:
        from qobuz_librarian.web import qobuz_access
        try:
            qobuz_access._recheck_token_if_due()
        except Exception as exc:
            log.warning("couldn't ask Qobuz again for held downloads: %s", exc)
        if _qobuz_hold is not None:
            return
    try:
        if check is None or not check():
            return
    except Exception as exc:
        log.warning(
            "couldn't check whether held downloads may start: %s", exc)
        return
    with _held_downloads_lock:
        released = list(_held_downloads)
        _held_downloads.clear()
    for job, fn in released:
        _download_queue.put((job, fn))


def submit_scan(job: Job, scan_fn, execute_fn):
    """Queue a scan/review/execute job.

    scan_fn(job) attaches candidates via job.add_candidate(). If it finds
    any, the job parks in AWAITING_REVIEW for the user to pick from; if it
    finds none, the job completes immediately. execute_fn(job, selected)
    runs later, once approve() is called.
    """
    job.kind = "scan"
    job._execute_fn = execute_fn
    if not job_persistence.admit(job):
        return None
    registry.add(job)

    def _scan(j: Job):
        j.status = JobStatus.SCANNING
        if j.started_at is None:
            j.started_at = time.time()
        j.phase = "scan"
        # Record SCANNING on disk so a restart mid-crawl restores through the
        # neutral "interrupted, resumes where it left off" path rather than the
        # harsher running-job "submit again" one (the worker only ever persisted
        # the preceding RUNNING).
        job_persistence.persist(j)
        scan_fn(j)
        if j.status != JobStatus.RUNNING and j.status != JobStatus.SCANNING:
            return  # scan_fn already set a terminal/explicit status
        if j.cancel_requested:
            return  # _run_task will detect the flag and set CANCELED
        if j.selected_candidates() or j.candidates:
            j.status = JobStatus.AWAITING_REVIEW
            review_badges.mark_ready(j.execute_kind)
        else:
            j.push_line("Nothing to do. No candidates found.")
            j.status = JobStatus.DONE

    _scan_queue.put((job, _scan))
    return job


def _library_review_state_guard(job: Job):
    """Serialize Library retirement with saved-state reconstruction."""
    if job.execute_kind != "library":
        return nullcontext()
    return library_scan_state.review_state_lock()


def finalize_review_if_empty(
    job: Job, *, summary: str | None = None,
) -> Optional[bool]:
    """Complete a triage review once dismissal has emptied its candidate list.

    Dismissing the last album leaves nothing to act on, so the job must not stay
    in AWAITING_REVIEW. Otherwise the dashboard keeps showing the "N new
    releases" banner (now zero) and the job page stays badged "awaiting review"
    over an empty list. Mirror submit_scan's found-nothing path and mark it DONE.
    Returns True if it finalized the job, False when it was not empty, or None
    when the terminal state could not be saved.
    """
    with _library_review_state_guard(job), job._review_action_lock:
        review_generation = None
        with job._lock:
            if job.status != JobStatus.AWAITING_REVIEW or job.candidates:
                return False
            if job.execute_kind == "library":
                review_generation = (job.execute_args or {}).get(
                    "_library_review_generation"
                )
            previous = (job.status, job.finished_at, job.summary)
            job.status = JobStatus.DONE
            job.finished_at = time.time()
            unchecked = job.unchecked_artists
            if summary is not None:
                job.summary = summary
            elif job.candidate_cap_hit or unchecked:
                parts = ["All listed albums reviewed."]
                if job.candidate_cap_hit:
                    parts.append("The scan hit the result cap, so some finds may "
                                 "not have been listed.")
                if unchecked:
                    parts.append(
                        f"{unchecked} artist{'s' if unchecked != 1 else ''} "
                        "couldn't be checked; scan again to finish.")
                if job.execute_kind == "downsample":
                    parts.append("Albums kept hi-res can be restored later.")
                else:
                    parts.append("Dismissed items can be restored later.")
                job.summary = " ".join(parts)
            elif job.execute_kind == "downsample":
                job.summary = ("All albums reviewed. Albums kept hi-res can be "
                               "restored later.")
            else:
                job.summary = (
                    "All albums reviewed. Dismissed items can be restored later."
                )
        if not job_persistence.persist(job):
            with job._lock:
                job.status, job.finished_at, job.summary = previous
            job.notify_review_changed("save_failed")
            return None
        if job.execute_kind == "library":
            # Worked through to empty. Retire the baseline's review so the
            # saved-state reconstruction doesn't rebuild it from stale scan state.
            if not library_scan_state.mark_review_retired(
                    reason="worked_through", generation=review_generation):
                with job._lock:
                    job.status, job.finished_at, job.summary = previous
                job_persistence.persist(job)
                job.notify_review_changed("save_failed")
                return None
            badge_generation = review_badges.ready_generation("library")
            other_candidates = False
            for other in registry.awaiting_review():
                if other.execute_kind != "library":
                    continue
                with other._lock:
                    if (other.status == JobStatus.AWAITING_REVIEW
                            and other.candidates):
                        other_candidates = True
                        break
            if not other_candidates:
                # A newer scan may light the badge between the snapshot and
                # this clear. Only retire the generation that belonged to the
                # review we just emptied.
                review_badges.clear_ready_if_generation(
                    "library", badge_generation)
        _start_post_job_hook_for_job(job)
    return True


def _merge_key(candidate):
    album_id = str((candidate.get("payload") or {}).get("album_id") or "")
    if album_id:
        return album_id
    return ((candidate.get("artist") or "").lower(),
            (candidate.get("title") or "").lower())


def _restore_untouched_review(j: Job, *, status=JobStatus.RUNNING) -> bool:
    parked = None
    if j.execute_kind in job_persistence.REVIEW_PICK_KINDS:
        parked = next((other for other in registry.awaiting_review()
                       if other is not j and other.execute_kind == j.execute_kind),
                      None)
    action_jobs = sorted(
        [item for item in (j, parked) if item is not None],
        key=lambda item: item.id,
    )
    with _library_review_state_guard(j), ExitStack() as action_locks:
        for item in action_jobs:
            action_locks.enter_context(item._review_action_lock)
        with ExitStack() as job_locks:
            for item in action_jobs:
                job_locks.enter_context(item._lock)
            if j.status != status or j.cancel_requested:
                return False
            previous = (j.status, j.finished_at, j.error, j.candidates, j._cand_seq,
                        j.title)
            should_merge = bool(
                parked is not None
                and parked.status == JobStatus.AWAITING_REVIEW
            )
            if should_merge:
                # A refresh can fold the approved album back into the parked
                # review, and the parked review mints cids from its own counter.
                own = {_merge_key(c) for c in j.candidates}
                cids = {c.get("cid") for c in j.candidates}
                j.sync_cand_seq()
                candidates = list(j.candidates)
                for candidate in parked.candidates:
                    if _merge_key(candidate) in own:
                        continue
                    if candidate.get("cid") in cids:
                        seq = j._cand_seq
                        j._cand_seq += 1
                        candidate = {**candidate, "cid": f"c{seq}", "seq": seq}
                    candidates.append(candidate)
                candidates.sort(key=lambda candidate: (
                    int(candidate.get("seq") or 0),
                    str(candidate.get("cid") or ""),
                ))
                j.candidates = candidates
                j.sync_cand_seq()
            j.status = JobStatus.AWAITING_REVIEW
            j.finished_at = None
            j.error = None
            review_title = (j.execute_args or {}).get("_review_title")
            if not review_title and j.title == RUN_TITLES.get(j.execute_kind):
                review_title = REVIEW_TITLES.get(j.execute_kind)
            j.title = review_title or j.title
            saved = (
                job_persistence.restore_split_review(j, parked)
                if should_merge
                else job_persistence.admit_review_transition(j)
            )
            if not saved:
                (j.status, j.finished_at, j.error,
                 j.candidates, j._cand_seq, j.title) = previous
                return False
            if should_merge:
                parked._retired = True
                parked.status = JobStatus.CANCELED
                registry.discard_merged_review(parked)
    if parked is not None and should_merge:
        parked.end_stream()
    return True


def approve(
    job: Job,
    selected_ids=None,
    *,
    split_review=None,
    selection_filter: Optional[Callable[[dict], bool]] = None,
):
    """Resume a reviewed job: run execute_fn over the selected candidates.

    Selection is server-backed: each tick is saved as it happens, so the web
    approve passes selected_ids=None and the already-saved `selected` flags
    drive the run. selected_ids is still honoured when given (the CLI/tests set
    the selection at approve time); None means "use whatever is already saved."
    ``split_review``, when supplied, runs under the same lock and may move
    unselected candidates into one new parked Job. The active and parked rows
    are then committed atomically before either is published to a worker.

    ``selection_filter`` limits the eligible selection (for example, to the
    active Library tab). The final non-empty check shares the same lock as the
    status transition, so a late untick cannot turn an approval into an empty
    execution. Returns APPROVAL_NO_SELECTION for that intact empty review,
    False for a stale/non-review job, None when durable admission failed, and
    True once the execute phase was safely queued.
    """
    # Flip the status under the job lock so a second concurrent approve
    # (double-click, two tabs) loses the check and can't enqueue the execute
    # phase a second time, which would re-download and re-import every album.
    parked = None
    with job._review_action_lock, job._lock:
        if job.status != JobStatus.AWAITING_REVIEW or job._execute_fn is None:
            return False
        selected = set(selected_ids) if selected_ids is not None else None

        def _will_select(candidate):
            chosen = (
                candidate.get("cid") in selected
                if selected is not None
                else bool(candidate.get("selected"))
            )
            return chosen and (
                selection_filter is None or selection_filter(candidate)
            )

        if not any(_will_select(candidate) for candidate in job.candidates):
            return APPROVAL_NO_SELECTION
        previous = {
            "status": job.status,
            "finished_at": job.finished_at,
            "started_at": job.started_at,
            "title": job.title,
            "execute_args": job.execute_args,
        }
        previous_candidates = job.candidates
        previous_selected = [
            (candidate, candidate.get("selected", _MISSING))
            for candidate in job.candidates
        ]
        prior_consumed = getattr(job, "_consumed_whole_review", _MISSING)
        if selected is not None:
            for c in job.candidates:
                c["selected"] = c.get("cid") in selected
        if split_review is not None:
            parked = split_review(job)
            if parked is not None and type(parked) is not Job:
                raise TypeError("split_review must return a Job or None")
        job.status = JobStatus.PENDING
        job.finished_at = None
        # Renamed here rather than by the worker: the worker gets there
        # milliseconds after the redirect has already rendered, so the tab
        # that pressed the button kept the scan's heading for the whole run
        # while every other tab, and a reload, showed the right one.
        run_title = RUN_TITLES.get(getattr(job, "execute_kind", ""))
        if run_title:
            # Saved with the run so a cancel or a restart that puts the picks
            # back can give the review its own name again.
            job.execute_args = {**(job.execute_args or {}),
                                "_review_title": job.title}
            job.title = run_title
        # Restart the elapsed clock at approve so the execute phase (downloading /
        # repairing) times itself; otherwise it keeps counting from the scan
        # start and folds in however long the results sat awaiting review, making
        # "Downloading · 1:17" read as download time when it was mostly browsing.
        job.started_at = time.time()
        # Drop the scan's last progress snapshot for the same reason. Left in
        # place it stood in for the execute phase until its first push, so a
        # page opened on a running downsample described the scan that ended
        # ("Scanning for hi-res files, 32 / 32 artists") instead of the run.
        job.progress_phase = ""
        job.progress_current = 0
        job.progress_total = 0
        job.progress_item = ""
        job.progress_found = 0
        job.progress_unit = ""
        job.progress_found_artists = 0
        related = (parked,) if parked is not None else ()
        if not job_persistence.admit_review_transition(job, related):
            for name, value in previous.items():
                setattr(job, name, value)
            job.candidates = previous_candidates
            for candidate, selected in previous_selected:
                if selected is _MISSING:
                    candidate.pop("selected", None)
                else:
                    candidate["selected"] = selected
            if prior_consumed is _MISSING:
                try:
                    delattr(job, "_consumed_whole_review")
                except AttributeError:
                    pass
            else:
                job._consumed_whole_review = prior_consumed
            return None

    if parked is not None:
        registry.add(parked)
        job.notify_review_changed()

    def _execute(j: Job):
        # Cancelled between approve and the worker picking this up: don't
        # start the work.
        if j.cancel_requested:
            return
        j.phase = "execute"
        # Read the selection at run time, not at approve time.
        with j._lock:
            chosen = j.selected_candidates()
        if not chosen:
            j.push_line("No candidates selected. Nothing to do.")
            j.status = JobStatus.DONE
            return
        try:
            j._execute_fn(j, chosen)
        except (Exception, SystemExit) as e:
            # Qobuz went away before ANYTHING landed: put the review back
            # exactly as it was instead of burying the picks in a failed job.
            # a months-old review's ticks would otherwise be gone for good.
            if (j._imported_any or j.cancel_requested or j.recoveries
                    or not isinstance(e, (AuthLost, QobuzUnavailable,
                                          CredentialChanged,
                                          DownloaderNotReady,
                                          QobuzEntitlementError,
                                          CandidateStale,
                                          SystemExit))):
                raise
            if not _restore_untouched_review(j):
                raise RuntimeError(
                    "The review could not be saved back to the data folder."
                ) from e
            message = (
                str(e)
                if isinstance(e, (CandidateStale, beets.StagingNotEmpty))
                else qobuz_action_error_message(e)
                     + " Your review and picks are untouched."
            )
            j.push_line(message)
            j.notify_review_changed()

    job.queued_at = time.time()
    _scan_queue.put((job, _execute))
    return True


def cancel_review(job: Job) -> Optional[bool]:
    """Discard a waiting review, or return None when it could not be saved."""
    # Flip under the job lock so this can't race approve() (which also flips
    # AWAITING_REVIEW under the lock); otherwise a cancel could land after an
    # approve already queued the execute phase, showing CANCELED while work
    # runs.
    with _library_review_state_guard(job), job._review_action_lock:
        review_generation = None
        with job._lock:
            if job.status != JobStatus.AWAITING_REVIEW:
                return False
            if job.execute_kind == "library":
                review_generation = (job.execute_args or {}).get(
                    "_library_review_generation"
                )
            previous_finished_at = job.finished_at
            previous_summary = job.summary
            canceled_at = time.time()
            job.status = JobStatus.CANCELED
            job.finished_at = canceled_at
            job.summary = "Review discarded. No files were changed."
        if not job_persistence.persist(job):
            with job._lock:
                if (
                    job.status == JobStatus.CANCELED
                    and job.finished_at == canceled_at
                ):
                    job.status = JobStatus.AWAITING_REVIEW
                    job.finished_at = previous_finished_at
                    job.summary = previous_summary
            return None
        if job.execute_kind == "library":
            # Discarded. Retire the baseline's review so the saved-state
            # reconstruction doesn't immediately rebuild what was discarded.
            if not library_scan_state.mark_review_retired(
                reason="discarded",
                generation=review_generation,
            ):
                with job._lock:
                    job.status = JobStatus.AWAITING_REVIEW
                    job.finished_at = previous_finished_at
                    job.summary = previous_summary
                job_persistence.persist(job)
                return None
        elif job.execute_kind in ("upgrade", "downsample"):
            review_badges.mark_discarded(job.execute_kind)
        _start_post_job_hook_for_job(job)
        job.end_stream()
        return True


# How many times in a row a download cut off before import is queued again
# on its own before it waits for Retry.
RESTART_REQUEUE_LIMIT = 2

_LEGACY_PAUSE_TEXT = (
    "downloads and scans are paused",
    "downloads are paused",
    "downloads and scans stay paused",
    "library work paused for an interrupted download",
)


def _settle_restarted_download(job: Job, settled, *, was_running: bool,
                               can_requeue: bool) -> bool:
    """Apply what start-up did with a download a restart cut off.

    Returns True when the job is to wait in the queue again. The count is
    saved with the job before it can run, so a download that keeps getting
    cut off stops after ``RESTART_REQUEUE_LIMIT`` tries.
    """
    restart = job_persistence.RESTART_INTERRUPTED + " " if was_running else ""
    job.phase = ""
    job.cancel_requested = False
    if settled.outcome == "requeue" and was_running:
        count = (job.execute_args or {}).get("restart_requeues")
        count = count if type(count) is int else 0
        if not can_requeue:
            job.error = restart + "Use Retry to download it again."
            job.attention = "failed"
        elif count < RESTART_REQUEUE_LIMIT:
            job.execute_args = {**(job.execute_args or {}),
                                "restart_requeues": count + 1}
            job.status = JobStatus.PENDING
            job.error = None
            job.attention = ""
            job.started_at = None
            job.finished_at = None
            job.push_line(
                "Interrupted by a restart, so it was queued again. Only what "
                "is still missing is downloaded.")
            return True
        else:
            job.error = (restart + "It was interrupted "
                         f"{plural(count + 1, 'time')} in a row, so it was not "
                         "queued again. Use Retry to download it again.")
            job.attention = "failed"
    elif settled.outcome == "requeue":
        job.error = "This download did not finish. Use Retry to download it again."
        job.attention = "failed"
    else:
        job.error = restart + settled.reason
        job.attention = "processing"
    job.status = JobStatus.FAILED
    job.finished_at = job.finished_at or time.time()
    return False


@dataclass(frozen=True)
class _RunningDownload:
    """A download job running at the restart that no saved queue names."""

    album_id: str
    outcome: str = "requeue"
    reason: str = ""


def settle_restarted_downloads(cut_off: dict) -> None:
    """Apply what settling did to stopped jobs restored before it ran."""
    for job_id, settled in cut_off.items():
        job = registry.get(job_id)
        if (
            job is None
            or job.status is not JobStatus.FAILED
            or job.recoveries
            or normalise_album_id(job.album_id)
            != normalise_album_id(settled.album_id)
        ):
            continue
        with job._lock:
            _settle_restarted_download(
                job, settled, was_running=False, can_requeue=False)
        job_persistence.persist(job)


def _clear_legacy_pause(job: Job) -> bool:
    """Rewrite a download's saved pause notice once nothing holds it.

    Earlier versions saved "downloads are paused" text, and a recovery
    attention, with the job; once its saved queue entry is gone neither is
    true, and the job can simply be retried.
    """
    error = (job.error or "").lower()
    if (
        job.status is not JobStatus.FAILED
        or not job.album_id
        or job.recoveries
        or (job.attention != "recovery"
            and not any(text in error for text in _LEGACY_PAUSE_TEXT))
    ):
        return False
    restart = job_persistence.RESTART_INTERRUPTED
    job.error = ((restart + " Use Retry to download it again.")
                 if (job.error or "").startswith(restart)
                 else "This download did not finish. Use Retry to download it again.")
    job.attention = "failed"
    return True


def restore_jobs(
    execute_registry: dict,
    *,
    unsettled: frozenset = frozenset(),
    cut_off: dict | None = None,
    requeue: Callable[[Job], Optional[Callable]] | None = None,
    return_picks: Callable[[Job], str] | None = None,
    finished_upgrades=(),
    journaled: frozenset | None = None,
) -> None:
    """Rehydrate the registry from the on-disk job table at app startup.

    ``execute_registry`` maps an ``execute_kind`` string to a factory
    ``factory(job, execute_args)`` that returns the bound execute_fn to use
    when the user later approves a reloaded AWAITING_REVIEW job. The
    factory is invoked lazily on approve, so it can re-read a fresh
    Qobuz token (the one captured at original-submit time is gone).

    Three behaviours by saved status:
      * DONE / FAILED / CANCELED: loaded as-is so /queue still shows them.
      * AWAITING_REVIEW: loaded with candidates intact, execute_fn rebound
        from ``execute_registry`` if the kind is known. If the kind is
        unknown (registry change between releases), the job is rebadged
        FAILED with a hint to re-run the original scan.
      * PENDING / RUNNING / SCANNING: the closures that drove them are
        gone, so they're rebadged FAILED("interrupted on restart; submit
        again") and saved back. The user sees them rather than them
        silently vanishing. The exception is a PENDING job ``requeue``
        returns a run function for: it never started, so it waits in the
        queue again, in the order it was first queued. ``cut_off`` maps
        a download job's id to what start-up did with its saved queue entry
        (see ``_settle_restarted_download``); ``unsettled`` names the jobs
        whose entry could not be settled. A download that was running with
        no saved queue entry, when ``journaled`` names every web job that
        has one, is settled the same way. A review's run that
        stopped without saving its stop hands its picks to ``return_picks``.
        An Upgrade run counts the album folders in ``finished_upgrades``,
        whose upgrade the start-up finished, as upgraded.
    """
    job_persistence.init()
    rows = job_persistence.load_all()
    if not rows:
        return
    interrupted = 0
    review = 0
    historical = 0
    restored = []
    requeued = []
    pending_reviews = []
    cut_short = []
    finished_jobs = []
    pending_logs = {
        row["id"]: row["log_lines_json"]
        for row in rows if "log_lines_json" in row
    }

    def _restore_log_lines(job):
        if job.id in pending_logs:
            job.log_lines, _ = job_persistence.decode_log_lines(
                pending_logs.pop(job.id))

    unreadable_review_error = (
        "This saved review couldn't be restored because its choices could "
        "not be read. Start the scan again from its original page."
    )

    def _persist_restored_transition(job, previous_status):
        _restore_log_lines(job)
        if job.status == JobStatus.FAILED and not job.attention:
            job.attention = "failed"
        saved = job_persistence.persist(job)
        if saved is not True:
            _mark_final_save_failure(job)
        if (
            saved is True
            and previous_status is not None
            and previous_status not in TERMINAL
            and job.status in TERMINAL
        ):
            finished_jobs.append(job)
        return saved

    for row in rows:
        unknown_recovery_status = False
        try:
            status = JobStatus(row["status"])
            previous_status = status
        except ValueError:
            previous_status = None
            if not row.get("recoveries"):
                # An ordinary row from an incompatible version cannot be
                # resumed safely and carries nothing the user must recover.
                job_persistence.delete(row["id"])
                continue
            # A recovery carrier is different: its exact backup record is a
            # safety obligation.
            status = JobStatus.FAILED
            unknown_recovery_status = True
        if row.get("execute_kind") == "upgrade":
            # Earlier versions saved Upgrade retry reviews with the download verb.
            row["review_verb"] = "Upgrade"
        job = _job_from_row(row, status)
        job._durability_required = True
        if status not in TERMINAL:
            _restore_log_lines(job)
        pending_review = (
            status == JobStatus.PENDING
            and job.execute_kind in job_persistence.REVIEW_PICK_KINDS
            and bool(job.candidates or row.get("candidates_unreadable"))
        )
        returns_picks = (status == JobStatus.RUNNING
                         and job.execute_kind in job_persistence.REVIEW_PICK_KINDS
                         and job.finished_at is None
                         and not job.recoveries
                         and not job.execute_args_unreadable)
        if returns_picks:
            # A stop the app saw has already dealt with the picks and saved
            # its stop time; a hard stop did neither.
            cut_short.append(job)
        completion_acknowledged = False
        if job.album_id and not job.recoveries:
            completion_acknowledged = (
                job_persistence.durable_completion_acknowledged(
                    job.id,
                    job_created_at=job.created_at,
                    album_id=job.album_id,
                    strict=True,
                )
                is True
            )
        settled = (cut_off or {}).get(job.id)
        if (
            settled is None
            and status == JobStatus.RUNNING
            and job.kind == "download"
            and job.album_id
            and journaled is not None
            and job.id not in journaled
            and set(job.single or {}) <= {"album_id", "track_id"}
            and not (job.execute_args or {}).get("new_edition")
            and (job.finished_at is not None
                 or not (job.execute_args or {}).get(job_persistence.SAVED_QUEUE))
        ):
            # Its partial download was a run folder no saved queue claimed,
            # which start-up has already removed, or a saved one the stop
            # discarded itself.
            settled = _RunningDownload(album_id=job.album_id)
        if settled is not None and (
            job.id in unsettled
            or job.recoveries
            or job.execute_args_unreadable
            or normalise_album_id(job.album_id)
            != normalise_album_id(settled.album_id)
        ):
            settled = None
        if completion_acknowledged:
            # Startup recovery proved and acknowledged this exact job
            # incarnation before retiring the queue journal.
            job.status = JobStatus.DONE
            job.phase = ""
            job.error = None
            job.summary = job.summary or "Download completed before the restart."
            job.attention = ""
            job.cancel_requested = False
            job.finished_at = job.finished_at or time.time()
            historical += 1
            _persist_restored_transition(job, previous_status)
        elif settled is not None and (
            status in (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.FAILED)
        ):
            run = (requeue(job) if requeue is not None
                   and not row.get("single_unreadable") else None)
            if _settle_restarted_download(
                job, settled, was_running=status is not JobStatus.FAILED,
                can_requeue=run is not None,
            ) and _persist_restored_transition(job, previous_status) is True:
                requeued.append((job, run))
            else:
                if job.status is not JobStatus.FAILED:
                    job.status = JobStatus.FAILED
                    job.error = (job_persistence.RESTART_INTERRUPTED
                                 + " Use Retry to download it again.")
                    job.finished_at = time.time()
                interrupted += 1
                _persist_restored_transition(job, previous_status)
        elif (
            status == JobStatus.FAILED
            and job.id not in unsettled
            and _clear_legacy_pause(job)
        ):
            historical += 1
            _persist_restored_transition(job, previous_status)
        elif unknown_recovery_status:
            job.error = ("This saved Repair job had an unrecognised state. "
                         "The original files were kept; check them before "
                         "retrying.")
            job.attention = "recovery"
            job.finished_at = job.finished_at or time.time()
            interrupted += 1
            _persist_restored_transition(job, previous_status)
        elif (
            status == JobStatus.PENDING
            and not pending_review
            and requeue is not None
            and not row.get("single_unreadable")
            and job.id not in unsettled
            and (run := requeue(job)) is not None
        ):
            requeued.append((job, run))
        elif status == JobStatus.SCANNING or (
            status == JobStatus.PENDING
            and job.kind == "scan"
            and not pending_review
            and not job.recoveries
        ):
            job.status = JobStatus.CANCELED
            job.summary = _interrupted_scan_summary(job)
            job.finished_at = (job.finished_at
                               or job_persistence.previous_write_at()
                               or job.created_at)
            interrupted += 1
            _persist_restored_transition(job, previous_status)
        elif (
            status == JobStatus.RUNNING
            and job.execute_kind in ("library", "new_releases")
            and job.title == RUN_TITLES[job.execute_kind]
        ):
            # Stopping folded the albums it had not finished back into the
            # review and logged what it did last.
            job.status = JobStatus.CANCELED
            restart = job_persistence.RESTART_INTERRUPTED + " "
            stopped = next((line for line in reversed(job.log_lines)
                            if line.startswith("Stopped early. ")), "")
            done = (job.execute_args or {}).get(job_persistence.ALBUMS_DONE)
            if stopped:
                job.summary = restart + stopped.removeprefix("Stopped early. ")
                if job.execute_kind == "library":
                    job.attention = "interrupted"
            elif done:
                picked = (job.execute_args or {}).get(
                    job_persistence.ALBUMS_PICKED) or done + sum(
                        1 for c in job.candidates if c.get("selected"))
                job.summary = (restart + f"{done} of "
                               f"{plural(max(picked, done), 'album')} downloaded "
                               "before it stopped. The rest did not finish.")
            else:
                job.summary = restart.rstrip()
            job.finished_at = job.finished_at or time.time()
            interrupted += 1
            _persist_restored_transition(job, previous_status)
        elif status in (JobStatus.PENDING, JobStatus.RUNNING) and not pending_review:
            job.status = JobStatus.FAILED
            if job.execute_kind in ("upgrade", "downsample", "repair"):
                # A run keeps its review's job, and with it the review's
                # summary, which no longer describes this run.
                job.summary = ""
            # Only an album download / single-track download carries an
            # album_id, and only those get a Retry button on the job + history
            # pages.
            restart = job_persistence.RESTART_INTERRUPTED + " "
            done = (job.execute_args or {}).get(job_persistence.ALBUMS_DONE)
            verb = _RUN_DONE_VERBS.get(job.execute_kind)
            picked = sum(1 for c in job.candidates if c.get("selected"))
            finished = 0
            if job.execute_kind == "upgrade":
                finished = sum(
                    1 for c in job.candidates if c.get("selected")
                    and (c.get("payload") or {}).get("album_dir")
                    in finished_upgrades)
            if finished:
                done = (done or 0) + finished
                job.execute_args = {**(job.execute_args or {}),
                                    job_persistence.ALBUMS_DONE: done}
            if done and verb:
                restart += (f"{done} of {plural(max(picked, done), 'album')} "
                            f"{verb}")
                restart += ("; the one it was upgrading when it stopped was "
                            "finished when the app started again. "
                            if finished else " before it stopped. ")
            if job.execute_args_unreadable:
                job.error = (
                    restart
                    + "The saved details needed to retry this job couldn't "
                    "be read. Start it again from its original page."
                )
            elif job.id in unsettled:
                job.error = (restart + "Its download couldn't be cleared "
                             "safely, so downloads and scans are paused. "
                             "Qobuz Librarian tries again when it restarts.")
                job.attention = "recovery"
            elif job.album_id:
                job.error = restart + "Use Retry to download it again."
            elif job.execute_kind == "lyrics":
                job.error = restart + "Start it again from the Lyrics page."
            elif job.execute_kind == "migration":
                job.error = restart + "Start it again from the Library migration page."
            elif job.execute_kind == "library":
                job.error = restart + "Start the scan again from the Library page."
            elif job.execute_kind == "downsample":
                job.error = restart + "Start it again from Downsample."
            elif job.execute_kind == "upgrade":
                job.error = (restart.rstrip() if done and done >= picked
                             else restart + "Start it again from Upgrade.")
            elif returns_picks and job.execute_kind == "collection_restore":
                job.error = restart.rstrip()
            else:
                job.error = restart + "Run it again to retry."
            if job.recoveries:
                job.error = (restart + "The original files were kept; check "
                             "them before retrying.")
            job.finished_at = job.finished_at or time.time()
            job._cut_short_this_start = True
            interrupted += 1
            _persist_restored_transition(job, previous_status)
        elif status == JobStatus.AWAITING_REVIEW or pending_review:
            if job.recoveries:
                # Older builds could re-park a Repair after its originals had
                # already been retained.
                job.status = JobStatus.FAILED
                job.error = ("The original files were kept. Check or "
                             "restore them before retrying this repair.")
                job.finished_at = time.time()
                interrupted += 1
                _persist_restored_transition(job, previous_status)
            elif row.get("candidates_unreadable"):
                job.status = JobStatus.FAILED
                job.error = unreadable_review_error
                job.finished_at = time.time()
                interrupted += 1
                _persist_restored_transition(job, previous_status)
            elif job.execute_args_unreadable:
                job.status = JobStatus.FAILED
                job.error = (
                    "The saved details needed to resume this review couldn't "
                    "be read. Start it again from its original page."
                )
                job.finished_at = time.time()
                interrupted += 1
                _persist_restored_transition(job, previous_status)
            elif (factory := execute_registry.get(job.execute_kind)) is None:
                job.status = JobStatus.FAILED
                job.error = ("Couldn't restore this job's executor across "
                             "the restart. Re-run the original scan.")
                job.finished_at = time.time()
                interrupted += 1
                _persist_restored_transition(job, previous_status)
            else:
                try:
                    execute_fn = factory(job, job.execute_args)
                    if not callable(execute_fn):
                        raise TypeError("restored executor is not callable")
                except Exception:
                    log.exception("couldn't restore executor for job %s", job.id)
                    job.status = JobStatus.FAILED
                    job.error = (
                        "The saved details needed to resume this review "
                        "couldn't be restored. Start it again from its "
                        "original page."
                    )
                    job.finished_at = time.time()
                    interrupted += 1
                    _persist_restored_transition(job, previous_status)
                else:
                    job._execute_fn = execute_fn
                    if pending_review:
                        pending_reviews.append(job)
                    review += 1
        else:
            historical += 1
        restored.append(job)
    # Insert once, then bound the in-memory finished set to MAX_FINISHED just
    # like a live add() would. The archive on disk keeps up to PERSIST_KEEP
    # so an evicted job still renders via /jobs/{id}.
    with registry._lock:
        for job in restored:
            registry._jobs[job.id] = job
            registry._order.append(job.id)
        registry._prune_locked()
        for job in restored:
            if job.id in registry._jobs:
                _restore_log_lines(job)
    for job in pending_reviews:
        if not _restore_untouched_review(job, status=JobStatus.PENDING):
            with job._lock:
                job.status = JobStatus.FAILED
                job.attention = "failed"
                job.finished_at = job.finished_at or time.time()
                job.error = (
                    job_persistence.RESTART_INTERRUPTED + " Its picks could "
                    "not be put back in the review. Run the scan again."
                )
            job_persistence.persist(job)
    for job in cut_short if return_picks is not None else ():
        try:
            outcome = return_picks(job)
        except Exception:
            log.exception("couldn't return the picks of job %s", job.id)
            continue
        if not outcome or job.execute_kind not in (
                "library", "new_releases", "collection_restore"):
            continue
        with job._lock:
            if job.status is JobStatus.CANCELED:
                job.summary = f"{job.summary} {outcome}"
            else:
                job.error = f"{job.error} {outcome}"
        job_persistence.persist(job)
    # Behind the same pause as any download that arrives while the library is
    # paused, and in the order they were queued.
    requeued.sort(key=lambda pair: pair[0].created_at)
    with _held_downloads_lock:
        _held_downloads.extend(requeued)
    for job in finished_jobs:
        _start_post_job_hook(_post_job_hook_payload(job))
    if interrupted or review or requeued:
        log.info(
            "Restored %s / %s / %s / %s from the previous run.",
            plural(historical, "historical job"),
            plural(review, "review job"),
            plural(len(requeued), "queued download"),
            plural(interrupted, "interrupted job"),
        )


_RUN_DONE_VERBS = {
    "upgrade": "upgraded",
    "downsample": "downsampled",
    "repair": "repaired",
}


def _interrupted_scan_summary(job: Job) -> str:
    """What a scan cut short by a restart says it left behind. A library or
    whole-library repair scan keeps its progress in a checkpoint; every other
    kind starts again."""
    restart = job_persistence.RESTART_INTERRUPTED + " "
    if job.execute_kind == "library":
        if scan_checkpoint.pending() is None:
            return restart + "Start the scan again from the Library page."
        return (restart + "The next Library scan continues from where it "
                "stopped.")
    if job.execute_kind == "repair":
        if scan_checkpoint.load("repair") is None:
            return restart + "Start the scan again from the Repair page."
        return (restart + "Start the repair scan again and it continues "
                "from where it left off.")
    if job.execute_kind == "downsample":
        return restart + "Start it again from Downsample."
    return restart + "Run the scan again to retry."


def load_historical_job(job_id: str) -> Optional[Job]:
    """Rebuild a Job from the on-disk archive without inserting it back into
    the registry. Used by /jobs/{id} when the live registry doesn't know
    about it, so an evicted (or restart-old) terminal job still gets a
    read-only render instead of silently redirecting to /queue."""
    row = job_persistence.load_one(job_id)
    if row is None:
        return None
    try:
        status = JobStatus(row["status"])
    except ValueError:
        return None
    if status == JobStatus.AWAITING_REVIEW and row.get("candidates_unreadable"):
        status = JobStatus.FAILED
        row["error"] = (
            "This saved review couldn't be restored because its choices "
            "could not be read. Start the scan again from its original page."
        )
        row["finished_at"] = row.get("finished_at") or time.time()
    return _job_from_row(row, status)


def _job_from_row(row: dict, status: JobStatus) -> Job:
    """A Job rebuilt from one saved row."""
    job = Job(
        id=row["id"],
        title=row.get("title") or "",
        artist=row.get("artist") or "",
        album_id=row.get("album_id") or "",
        edition=row.get("edition") or "",
        kind=row.get("kind") or "download",
        status=status,
        phase=row.get("phase") or "",
        candidates=row.get("candidates") or [],
        error=row.get("error"),
        summary=row.get("summary") or "",
        review_verb=row.get("review_verb") or "Download",
        execute_kind=row.get("execute_kind") or "",
        execute_args=row.get("execute_args") or {},
        execute_args_unreadable=bool(row.get("execute_args_unreadable")),
        single=row.get("single") or {},
        attention=row.get("attention") or "",
        quality_shortfall=row.get("quality_shortfall") or {},
        tracks_missing=row.get("tracks_missing") or 0,
        recoveries=row.get("recoveries") or [],
        log_lines=list(row.get("log_lines") or []),
        created_at=(
            row.get("created_at")
            if type(row.get("created_at")) in (int, float)
            else time.time()
        ),
        finished_at=row.get("finished_at"),
    )
    if row.get("single_unreadable"):
        job._preserve_persisted_single = True
        job._single_undo_unavailable = True
    return job


def _finish_canceled(job: Job) -> bool:
    """Persist and publish a CANCELED transition made under the job lock."""
    saved = True
    if registry.get(job.id) is not None:
        saved = job_persistence.persist(job)
    if not saved and not job._durability_required:
        saved = True
    if not saved:
        _mark_final_save_failure(job)
        job.end_stream()
        return False
    _start_post_job_hook_for_job(job)
    job.end_stream()
    return True


def request_cancel(job: Job) -> bool:
    """Stop a job from the UI, whatever phase it's in.

    - awaiting review  → discarded immediately
    - scanning/running → cooperative: the flag is set and the scan/execute
      loops bail at their next iteration (so a long library scan can be
      stopped without restarting the container)
    - pending          → canceled or returned to review

    Returns False if the job is finished, is already putting an album into the
    library.
    """
    if job.status == JobStatus.AWAITING_REVIEW:
        review_canceled = cancel_review(job)
        if review_canceled is True:
            return True
        if review_canceled is None:
            return False
    if (job.status == JobStatus.PENDING
            and job.execute_kind in job_persistence.REVIEW_PICK_KINDS
            and job.candidates):
        if _restore_untouched_review(job, status=JobStatus.PENDING):
            job.notify_review_changed()
            return True
        if job.status == JobStatus.PENDING:
            return False
    # Either it wasn't in review, or an approve() flipped it to PENDING
    # between the check and cancel_review's locked re-check.
    with job._lock:
        if job.importing:
            return False
        if job.status in TERMINAL:
            return False
        canceled_pending = job.status == JobStatus.PENDING
        if canceled_pending:
            # Publish the terminal row while the same lock excludes the worker
            # claim.  Mutating first and saving after releasing this lock let
            # a failed write race the worker into consuming the queue entry,
            # leaving neither a durable cancel nor runnable work.
            previous_finished_at = job.finished_at
            previous_cancel_requested = job.cancel_requested
            previous_summary = job.summary
            canceled_at = time.time()
            job.cancel_requested = True
            job.status = JobStatus.CANCELED
            job.finished_at = canceled_at
            job.summary = job.summary or "Cancelled before it started."
            if (
                not job_persistence._persist_locked(job)
                and job._durability_required
            ):
                job.status = JobStatus.PENDING
                job.cancel_requested = previous_cancel_requested
                job.finished_at = previous_finished_at
                job.summary = previous_summary
                return False
        else:
            job.cancel_requested = True
    if canceled_pending:
        _start_post_job_hook_for_job(job)
        job.end_stream()
    return True


def record_quality_shortfall(job, verdict):
    with job._lock:
        job.attention = "quality"
        job.quality_shortfall = download_result.quality_shortfall_record(verdict)


def note_quality_shortfall(verdict):
    job = getattr(_TLS, "current_job", None)
    if job is not None:
        record_quality_shortfall(job, verdict)


from qobuz_librarian.queue import executor as _queue_executor  # noqa: E402

_queue_executor.on_quality_shortfall = note_quality_shortfall
