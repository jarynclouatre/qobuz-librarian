"""Where a job page leads back to, what a queued job waits behind, and ages."""
import hashlib
import math
import time
from datetime import datetime

from qobuz_librarian.ui_cli import colors
from qobuz_librarian.web import job_persistence, runtime
from qobuz_librarian.web import jobs as job_mgr


def _queue_wait(job):
    """Describe what a PENDING job is waiting behind on its worker lane, so the
    UI can explain the wait instead of showing a bare "Queued". Scans share one
    worker and downloads another (see web/jobs.py), so a job only waits behind
    others in its OWN lane (job.kind: "scan" | "download"). ``position`` counts
    how many run before it (the one holding the worker + any earlier-queued).
    One held because Qobuz rejected the token or could not be reached carries
    ``for_qobuz``, and one held while downloads are paused ``paused``.
    ``starts_after`` names the job right before it when that is a different
    kind of job, and is empty when it waits behind more of its own kind.

    Returns {"ahead_title", "starts_after", "lane", "position", "paused",
    "for_qobuz"} or None when nothing's ahead, i.e. it's about to start, so
    there's nothing to explain."""
    if job.status != job_mgr.JobStatus.PENDING:
        return None
    holder = None
    ahead = 0
    before = None
    mine = job.queued_at or job.created_at or 0
    for j in job_mgr.registry.all():
        if j.id == job.id or j.kind != job.kind:
            continue
        if j.status in (job_mgr.JobStatus.SCANNING, job_mgr.JobStatus.RUNNING):
            holder = j
        elif (j.status == job_mgr.JobStatus.PENDING
              and (j.queued_at or j.created_at or 0) < mine):
            ahead += 1
            if before is None or (j.queued_at or j.created_at or 0) > (
                    before.queued_at or before.created_at or 0):
                before = j
    for_qobuz = job_mgr.waiting_for_qobuz(job)
    paused = not for_qobuz and job_mgr.is_held(job)
    if holder is None and ahead == 0 and not paused and not for_qobuz:
        return None
    before = before or holder
    return {
        "ahead_title": holder.title if holder else "",
        "starts_after": before.title if before and (
            (before.execute_kind, before.phase) != (job.execute_kind, job.phase)) else "",
        "lane": job.kind,
        "position": ahead + (1 if holder else 0),
        "paused": paused,
        "for_qobuz": for_qobuz,
    }


def _field(job, name):
    return job.get(name) if isinstance(job, dict) else getattr(job, name)


def _run_album(job):
    """The artist, title and version of the one album a Library download
    fetches, which the job is listed under, as the same download from Search
    is."""
    album = (_field(job, "execute_args") or {}).get("run_album")
    if (album and _field(job, "execute_kind") == "library"
            and _field(job, "title") == job_mgr.RUN_TITLES["library"]):
        return album
    return None


def _job_name(job) -> tuple[str, str]:
    """The artist and title a Job or a History row is listed under."""
    artist, title, edition = _run_album(job) or (
        _field(job, "artist"), _field(job, "title"), _field(job, "edition"))
    return artist or "", job_mgr.release_title(title, edition)


def _job_own_title(job) -> str:
    """The title a job's progress items repeat, left out where it is shown."""
    album = _run_album(job)
    return album[1] if album else job.title


_JOB_NAV_SURFACES = {
    "library": ("library", "/library", "Library"),
    "new_releases": ("library", "/library", "Library"),
    "upgrade": ("upgrade", "/upgrade", "Upgrade"),
    "downsample": ("downsample", "/downsample", "Downsample"),
    "repair": ("repair", "/repair", "Repair"),
    "lyrics": ("lyrics", "/lyrics", "Lyrics"),
    "migration": ("settings", "/migrate", "Migration"),
    "collection_snapshot": ("settings", "/settings#collection-backup", "Settings"),
    "collection_restore": ("settings", "/settings#collection-restore", "Settings"),
}


def _job_nav_destination(job) -> tuple[str, str, str]:
    destination = _JOB_NAV_SURFACES.get(job.execute_kind)
    if destination is not None:
        if destination[0] == "upgrade" and not runtime._upgrade_available():
            return "queue", "/queue/history", "History"
        return destination
    if job.status in job_mgr.TERMINAL:
        return "queue", "/queue/history", "History"
    return "queue", "/queue", "Queue"


def _queue_rows_signature(jobs):
    rows = "\n".join(sorted(
        f"{j.id}:{j.status.value}" for j in jobs
        if j.status != job_mgr.JobStatus.AWAITING_REVIEW
    ))
    return hashlib.sha256(rows.encode("utf-8")).hexdigest()[:16]


def _format_age(ts: float) -> str:
    """Human-readable age of a past timestamp."""
    try:
        ts = float(ts)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(ts):
        return ""
    age = time.time() - ts
    if age < 120:
        return "just now"
    if age < 3600:
        return f"{int(age / 60)} min ago"
    if age < 86400:
        return f"{int(age / 3600)} hr ago"
    days = int(age / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _when_label(ts) -> tuple[str, str]:
    """(label, exact) pair for a history timestamp: relative while it's fresh
    (matching the "1 hr ago" the tool pages already speak), a short date once
    it isn't. The exact stamp goes in a tooltip for anyone who needs the
    minute."""
    if not ts:
        return "", ""
    exact = (datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
             + colors.clock_zone())
    if time.time() - ts < 7 * 86400:
        return _format_age(ts), exact
    dt = datetime.fromtimestamp(ts)
    label = f"{dt.strftime('%b')} {dt.day}"
    if dt.year != datetime.now().year:
        label += f", {dt.year}"
    return label, exact


def _tool_last_run_age(execute_kind: str) -> str | None:
    """Age of a tool scan's last clean run, or None if it never finished."""
    ts = job_persistence.last_finished_at(execute_kind)
    return _format_age(ts) if ts is not None else None
