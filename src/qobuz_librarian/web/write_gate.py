"""Whether Web downloads and scans may run, and the notice that says why not."""
import logging

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.queue import startup_recovery
from qobuz_librarian.queue.startup_recovery import (
    POST_IMPORT_RELOCATION_LOG_ENTRY,
)
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web import queue_recovery, runtime, storage

_log = logging.getLogger("qobuz_librarian")


def _writes_paused_notice(*, durable_resume_job_id: str | None = None,
                          queue_behind_job=None,
                          log_details: bool = False):
    """Why downloads and scans are paused, in the user's words, or None.

    One source for both the 503 a blocked request gets and the notice the
    dashboard shows, so the two cannot drift into naming different causes.
    ``log_details`` is for the request path only: the dashboard reads this on
    every load and must not write a log line each time. ``queue_behind_job``
    is the one job allowed past another download's recovery, to wait its turn
    rather than be refused.
    """
    queue_recovery._await_recovery_refresh()
    reason = ""
    action = None
    settle = None
    give_up = None
    if runtime._CLI_MODE:
        reason = "Terminal mode is holding the library."
        msg = ("Terminal (CLI) mode is on, so downloads and scans are paused "
               "here. Resume on Settings → Mode (Resume web app).")
        action = {"href": "/settings#mode", "label": "Open Settings"}
    elif runtime._LOCK_BUSY_PID is not None:
        reason = "Another Qobuz Librarian run is using the library."
        msg = ("Another Qobuz Librarian run is active. Downloads and scans are "
               "paused. Stop the other run first, then restart Qobuz "
               "Librarian.")
    elif (unwritable := storage._unwritable_volumes()):
        reason = "A folder Qobuz Librarian must write to is read-only."
        action = {"href": "/settings#diagnostics", "label": "Open Diagnostics"}
        msg = ("Qobuz Librarian can't write where it needs to: "
               f"{'; '.join(unwritable)}. On a NAS, set "
               "PUID/PGID to the share owner and confirm the host "
               "directories exist. Downloads can't run until fixed.")
    elif runtime._LOCK_UNENFORCEABLE and isinstance(run_lock.unavailable_reason,
                                            PermissionError):
        reason = "The run lock file can't be opened."
        msg = (f"Qobuz Librarian can't open {cfg.LOCK_FILE}: permission "
               "denied. Downloads and scans are paused. Set its owner to the "
               "user Qobuz Librarian runs as (PUID and PGID in Docker), then "
               "restart.")
    elif runtime._LOCK_UNENFORCEABLE:
        reason = "The data folder can't hold the run lock."
        msg = ("The data folder can't hold the run lock (read-only, or a "
               "mount without file locking). Downloads and "
               "scans are paused. Move the data folder to a writable "
               "filesystem that supports file locking, then restart.")
    elif not storage._data_dir_available():
        # The folder was fine at startup, so the lock checks above all pass and
        # nothing else notices. Without this branch the readiness check knows
        # the app cannot save anything while every page still looks normal.
        reason = "Qobuz Librarian can't write to its data folder."
        action = {"href": "/settings#diagnostics", "label": "Open Diagnostics"}
        msg = ("Qobuz Librarian can't write to its data folder, so downloads "
               "and scans are paused and nothing new can be saved. Check that "
               "the folder "
               "still exists and that Qobuz Librarian can write to it; the "
               "app picks it up again on its own.")
    elif queue_recovery._STARTUP_RECOVERY_REFRESHING:
        reason = "Interrupted work is being checked."
        msg = ("Qobuz Librarian is checking for interrupted downloads, so "
               "downloads and scans wait until it finishes.")
    elif queue_recovery._STARTUP_RECOVERY_UNKNOWN:
        reason = "Interrupted work could not be checked safely."
        msg = (
            "Qobuz Librarian took the run lock but could not read its saved "
            "recovery state. The lock was released, downloads and scans stay "
            "paused, and the app tries again on its own. Check the "
            "data-folder permissions if this notice remains."
        )
    elif not runtime._run_lock_intact():
        reason = "The run lock was lost."
        msg = ("The run lock was lost, so downloads and scans are paused. "
               "Restart Qobuz Librarian.")
    elif not job_mgr.job_persistence.ready_for_admission():
        jobs_db = job_mgr.job_persistence.database_path()
        if job_mgr.job_persistence.database_damaged():
            reason = "The Queue and History file is damaged."
            msg = (
                f"Qobuz Librarian can't read {jobs_db}, so downloads and scans "
                "are paused before any work starts. Nothing new was queued. "
                f"Move it, and any {jobs_db.name}-wal and {jobs_db.name}-shm "
                "beside it, out of the data folder and restart Qobuz "
                "Librarian; History and any review waiting in it will be lost."
            )
        else:
            reason = "Queue and History cannot be saved to the data folder."
            action = {"href": "/settings#diagnostics",
                      "label": "Open Diagnostics"}
            msg = (
                f"Qobuz Librarian can't open or write {jobs_db}, so downloads "
                "and scans are paused before any work starts. Nothing new was "
                "queued. Check the permissions of that file and the data "
                "folder, and free space, then restart Qobuz Librarian."
            )
    elif (queue_recovery._startup_recovery_status_value() == "attention_required"):
        relocation = queue_recovery._post_import_relocation_recovery()
        if relocation is not None:
            paths = "; ".join(str(path) for path in relocation.paths)
            # relocation.reason is str(exc) from the relocation code; an
            # internal diagnostic, not an explanation. It belongs in the log,
            # which this message points at; the user gets what happened to
            # their music and what to do.
            if log_details:
                _log.warning(
                    "post-import relocation recovery: %s (paths: %s)",
                    relocation.reason or "reason not reported",
                    paths or "none reported")
            reason = "A move of album folders inside your library was interrupted."
            msg = (
                "Qobuz Librarian can't confirm that move finished, so downloads "
                "and scans are paused and your files are left exactly as they "
                "are. "
                + (f"The folders involved: {paths}. " if paths else "")
                + "Restart Qobuz Librarian; if this screen comes back, the "
                f"“{POST_IMPORT_RELOCATION_LOG_ENTRY}” entry in the container "
                "log has the technical detail."
            )
        elif queue_recovery._unreadable_queue_paths() is not None:
            files = "; ".join(str(path) for path in queue_recovery._unreadable_queue_paths())
            reason = "The saved download queue can't be read."
            msg = (
                "Downloads and scans are paused and nothing was changed. "
                + (f"The file involved: {files}. " if files else "")
                + "Fix its permissions, or if it is damaged, move it out of "
                "the data folder (the downloads it listed will need queuing "
                "again), then restart Qobuz Librarian."
            )
        elif queue_recovery._recovery_pause_is_another_download(queue_behind_job):
            return None
        else:
            reason = "An interrupted download couldn't be verified."
            # A terminal download never became a web job, so it has no History
            # row and no Retry button. Point each origin at the surface that
            # can settle it, the way the resume_required branch below does.
            origin = queue_recovery._startup_recovery_origin_value()
            named = queue_recovery._startup_recovery_album_label()
            of_album = f" of “{named}”" if named else ""
            paused = ("Downloads and scans are paused, and its saved queue and "
                      "staged files were left unchanged. ")
            held_job_id = queue_recovery._startup_recovery_web_job_id()
            partial = startup_recovery.partial_import_note(
                queue_recovery._STARTUP_RECOVERY_RESULT)
            if partial is not None:
                msg = (f"{partial} Downloads and scans are paused until it "
                       "is given up, which keeps the tracks Beets moved and "
                       "sets the rest aside in Settings > Diagnostics.")
                if origin == "cli":
                    settle = queue_recovery._terminal_recovery_offer()
                elif held_job_id is not None:
                    action = {"href": f"/jobs/{held_job_id}",
                              "label": "Open that download"}
            elif origin != "cli" and held_job_id is not None:
                action = {"href": f"/jobs/{held_job_id}",
                          "label": "Open that download"}
                give_up = queue_recovery._web_give_up_offer(held_job_id)
                msg = ("Downloads and scans are paused until the interrupted "
                       f"download{of_album} is retried or given up. Its saved "
                       "queue and staged files were left unchanged.")
            elif origin == "cli":
                settle = queue_recovery._terminal_recovery_offer()
                if settle is not None:
                    # Not everyone who starts a download in a terminal wants to
                    # go back to one to get out of it, and giving up on the
                    # album is the one decision that lifts the pause on its own.
                    action = {"href": "/queue", "label": "Settle it"}
                    if settle["imported"]:
                        msg = (f"An interrupted terminal download{of_album} "
                               "stopped after Beets had filed it, and it "
                               "could not be verified. Downloads and scans "
                               "are paused. Giving up on it clears the pause "
                               "and keeps what Beets filed in your library. "
                               "Checking it again needs terminal mode in "
                               "Settings.")
                    else:
                        msg = (f"An interrupted terminal download{of_album} "
                               "could not be verified safely. " + paused +
                               "Giving up on it clears the pause, and because "
                               "nothing reached your library the album is "
                               "still there to download again from the "
                               "Library review or a search. Trying that same "
                               "download again needs terminal mode in "
                               "Settings.")
                else:
                    action = {"href": "/settings#mode", "label": "Open Settings"}
                    msg = (f"An interrupted terminal download{of_album} could "
                           "not be verified safely. " + paused + "Switch to "
                           "terminal mode in Settings and run Qobuz Librarian "
                           "there; it offers to settle this.")
            else:
                msg = (f"An interrupted download{of_album} could not be "
                       "verified safely. " + paused + "Settle it from the "
                       "interface it was started in; if it stays blocked, "
                       "check the application log.")
    elif (
        queue_recovery._startup_recovery_status_value() == "resume_required"
        and not queue_recovery._durable_resume_allowed(durable_resume_job_id or "")
        and not queue_recovery._recovery_pause_is_another_download(queue_behind_job)
    ):
        reason = "An interrupted download is waiting to be settled."
        origin = queue_recovery._startup_recovery_origin_value()
        named = queue_recovery._startup_recovery_album_label()
        of_album = f" of “{named}”" if named else ""
        held_job_id = queue_recovery._startup_recovery_web_job_id()
        if origin == "cli":
            action = {"href": "/settings#mode", "label": "Open Settings"}
            msg = (f"An interrupted terminal download{of_album} has saved "
                   "recovery state. Other library changes are paused. Switch "
                   "to terminal mode in Settings, then resume that download "
                   "there.")
        elif held_job_id is not None:
            # The saved origin is missing on records written before it
            # existed, and it decided the wording; the job named by the saved
            # mode is the one to open, so that is what the notice offers.
            action = {"href": f"/jobs/{held_job_id}",
                      "label": "Open that download"}
            give_up = queue_recovery._web_give_up_offer(held_job_id)
            msg = ("Downloads and scans are paused until the interrupted "
                   f"download{of_album} is retried or given up.")
        else:
            msg = (f"An interrupted download{of_album} has saved recovery "
                   "state. Other library changes are paused until that exact "
                   "download is resumed from the interface where it started.")
    else:
        return None
    return {"reason": reason, "msg": msg, "action": action, "settle": settle,
            "give_up": give_up}


def _retry_can_queue(job) -> bool:
    """Whether Retry would queue this job behind the paused recovery."""
    if isinstance(job, dict):
        job = (job_mgr.registry.get(job["id"])
               or job_mgr.load_historical_job(job["id"]))
    return (
        queue_recovery._recovery_pause_is_another_download(job)
        and _writes_paused_notice(queue_behind_job=job) is None
    )


def _web_writes_paused() -> bool:
    """True when destructive web work must not run: the same conditions
    _lock_busy_response answers 503 for, as one predicate for the AUTOMATIC
    triggers (dashboard new-release check, library-scan resume) that have no
    request to bounce. Any trigger checking only part of this list quietly
    re-opens the hole the pause exists to close."""
    queue_recovery._await_recovery_refresh()
    return (
        runtime._SHUTTING_DOWN
        or runtime._CLI_MODE
        or runtime._LOCK_BUSY_PID is not None
        or bool(storage._unwritable_volumes())
        or not storage._data_dir_available()
        or not job_mgr.job_persistence.ready_for_admission()
        or runtime._LOCK_UNENFORCEABLE
        or not runtime._run_lock_intact()
        or queue_recovery._startup_recovery_status_value() in {
            "attention_required",
            "resume_required",
        }
    )


def _readiness_report() -> tuple[int, dict]:
    failed = []
    if (
        not web_auth.auth_disabled()
        and web_auth.creds_file_present_but_unreadable()
    ):
        failed.append("credentials")
    if not storage._data_dir_available():
        failed.append("data")
    if (
        not runtime._CLI_MODE
        and runtime._run_lock_intact()
        and not job_mgr.job_persistence.ready_for_admission()
    ):
        failed.append("job_persistence")
    if runtime._LOCK_UNENFORCEABLE or (
        not runtime._CLI_MODE
        and runtime._LOCK_BUSY_PID is None
        and not runtime._run_lock_intact()
    ):
        failed.append("run_lock")
    if runtime._SHUTTING_DOWN:
        failed.append("shutting_down")
    if failed:
        return 503, {"ok": False, "status": "not_ready", "checks": failed}

    degraded = []
    if runtime._CLI_MODE:
        degraded.append("terminal_mode")
    if runtime._LOCK_BUSY_PID is not None:
        degraded.append("other_writer")
    if storage._unwritable_volumes():
        degraded.append("write_volumes")
    if queue_recovery._startup_recovery_status_value() in {
        "attention_required",
        "resume_required",
    }:
        degraded.append("recovery")
    if degraded:
        return 200, {"ok": True, "status": "degraded", "checks": degraded}
    return 200, {"ok": True, "status": "ready"}


def _begin_direct_library_operation(label):
    """Atomically gate, register, and lock a request-owned library mutation."""
    with runtime._auto_check_lock:
        if _web_writes_paused():
            return "paused", None, None
        token = job_mgr.begin_library_operation(label)
        if token is None:
            return "paused", None, None
        lock = job_mgr.staging_lock()
        if not lock.acquire(blocking=False):
            job_mgr.end_library_operation(token)
            return "busy", None, None
        return "ok", token, lock
