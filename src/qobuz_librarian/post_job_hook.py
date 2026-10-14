"""POST_JOB_HOOK: the command run with each finished job's state as JSON on
stdin, for web jobs and terminal runs alike."""
import json
import os
import signal
import subprocess
import time
import uuid

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.logging import log, vlog


def release_title(title, edition="") -> str:
    title = str(title or "").strip() or "?"
    edition = str(edition or "").strip()
    if not edition or edition.casefold() in title.casefold():
        return title
    return f"{title} ({edition})"


def payload(
    *, event_id, status, title, edition="", artist="", error=None,
    finished_at=None, tracks_missing=0, kind="", summary="", review_items=0,
):
    return {
        "id": event_id,
        "status": status,
        "title": title,
        "edition": edition,
        "display_title": release_title(title, edition),
        "artist": artist,
        "error": error,
        "tracks_missing": tracks_missing,
        "finished_at": finished_at,
        "kind": kind,
        "summary": summary,
        "review_items": review_items,
    }


def run(payload: dict) -> None:
    """Run the POST_JOB_HOOK command (if set) with ``payload`` as JSON on
    stdin. Errors are logged and never raised; a broken hook can't kill the
    worker."""
    cmd = os.environ.get("POST_JOB_HOOK", "").strip()
    if not cmd:
        return
    try:
        proc = subprocess.Popen(
            ["sh", "-c", cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=cfg.child_env(),
            # Own process group, so the timeout can kill a hook's whole
            # pipeline. Killing just the shell leaves its children running.
            start_new_session=True,
        )
    except OSError as e:
        log.warning("post-job hook could not start: %s", e)
        return
    try:
        proc.communicate(json.dumps(payload).encode("utf-8"),
                         timeout=cfg.POST_JOB_HOOK_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Kill and reap the whole pipeline, including the shell process.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        proc.communicate()
        log.warning("post-job hook timed out")
    else:
        if proc.returncode:
            log.warning(
                "post-job hook exited with status %s", proc.returncode)


def report_terminal_run(*, kind, title, status, summary, started_at,
                        error=None):
    """Give a finished terminal Upgrade, Repair or Downsample run the History
    card and hook report its web run gets. Bookkeeping never changes the
    run's outcome, so a failure to write is logged for --verbose only."""
    from qobuz_librarian.web import job_persistence

    finished = time.time()
    job_id = None
    try:
        job_id = job_persistence.record_terminal_run(
            kind=kind, title=title, status=status, summary=summary,
            error=error, started_at=started_at, finished_at=finished)
        if job_id:
            job_persistence.prune_finished(job_persistence.FINISHED_HISTORY_KEEP)
    except Exception as exc:
        vlog(f"activity record write failed: {exc}")
    run(payload(event_id=job_id or uuid.uuid4().hex[:8], status=status,
                title=title, error=error, finished_at=finished, kind=kind,
                summary=summary))
