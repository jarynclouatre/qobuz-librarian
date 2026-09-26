"""Where a job page leads back to, what a queued job waits behind, and ages."""
import hashlib
import math
import time
from datetime import datetime

from qobuz_librarian.web import job_persistence, queue_recovery, runtime
from qobuz_librarian.web import jobs as job_mgr


def _queue_wait(job):
    """Describe what a PENDING job is waiting behind on its worker lane, so the
    UI can explain the wait instead of showing a bare "Queued". Scans share one
    worker and downloads another (see web/jobs.py), so a job only waits behind
    others in its OWN lane (job.kind: "scan" | "download"). ``position`` counts
    how many run before it (the one holding the worker + any earlier-queued).
    A job waiting behind an interrupted download instead carries
    ``paused_for``, because "starts automatically" is not true of it: nothing
    moves until that album is retried or given up.

    Returns {"ahead_title", "lane", "position", "paused_for"} or None when
    nothing's ahead, i.e. it's about to start, so there's nothing to explain."""
    if job.status != job_mgr.JobStatus.PENDING:
        return None
    holder = None
    ahead = 0
    for j in job_mgr.registry.all():
        if j.id == job.id or j.kind != job.kind:
            continue
        if j.status in (job_mgr.JobStatus.SCANNING, job_mgr.JobStatus.RUNNING):
            holder = j
        elif (j.status == job_mgr.JobStatus.PENDING
              and (j.created_at or 0) < (job.created_at or 0)):
            ahead += 1
    paused_for = None
    if queue_recovery._recovery_pause_is_another_download(job):
        paused_for = {
            "album": queue_recovery._startup_recovery_album_label(),
            "job_id": queue_recovery._startup_recovery_web_job_id(),
        }
    if holder is None and ahead == 0 and paused_for is None:
        return None
    return {
        "ahead_title": holder.title if holder else "",
        "lane": job.kind,
        "position": ahead + (1 if holder else 0),
        "paused_for": paused_for,
    }


_JOB_NAV_SURFACES = {
    "library": ("library", "/library", "Back to Library"),
    "new_releases": ("library", "/library", "Back to Library"),
    "upgrade": ("upgrade", "/upgrade", "Back to Upgrade"),
    "downsample": ("downsample", "/downsample", "Back to Downsample"),
    "repair": ("repair", "/repair", "Back to Repair"),
    "lyrics": ("lyrics", "/lyrics", "Back to Lyrics"),
    "migration": ("settings", "/migrate", "Back to Migration"),
    "collection_snapshot": ("settings", "/settings", "Back to Settings"),
    "collection_restore": ("settings", "/settings", "Back to Settings"),
}


def _job_nav_destination(job) -> tuple[str, str, str]:
    destination = _JOB_NAV_SURFACES.get(job.execute_kind)
    if destination is not None:
        if destination[0] == "upgrade" and not runtime._upgrade_available():
            return "queue", "/queue/history", "Back to History"
        return destination
    if job.status in job_mgr.TERMINAL:
        return "queue", "/queue/history", "Back to History"
    return "queue", "/queue", "Back to Queue"


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
    exact = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
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
