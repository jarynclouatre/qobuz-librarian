"""Whether Web downloads and scans may run, and the notice that says why not."""
import logging

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.queue import startup_recovery
from qobuz_librarian.queue.startup_recovery import (
    POST_IMPORT_RELOCATION_LOG_ENTRY,
)
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web import queue_recovery, runtime, storage

_log = logging.getLogger("qobuz_librarian")


def _terminal_command():
    return ("docker compose run --rm -it qobuz-librarian cli"
            if cfg.in_container() else "qobuz-librarian")


def _terminal_queue_notice():
    """Why downloads wait for a queue the terminal saved, or None."""
    waiting = queue_recovery._terminal_queue_waiting()
    if not waiting:
        return None
    return {
        "reason": "A download queue saved in the terminal is waiting.",
        "msg": (f"Downloads are paused: a queue of {plural(waiting, 'album')} "
                "saved in the terminal is waiting. Switch to terminal mode "
                f"in Settings, then run “{_terminal_command()}”; it offers to "
                "resume or "
                "discard the queue."),
        "action": {"href": "/settings#mode", "label": "Open Settings"},
    }


def _read_only_library_notice():
    """Why downloads are off for a music folder mounted read-only, or None."""
    if not storage.music_mounted_read_only():
        return None
    return {
        "reason": "Your music folder is mounted read-only.",
        "msg": ("Downloads are off: your music folder is mounted read-only, "
                "so nothing can be added to it. Library scans still run, and "
                "the tools skip files they can't change."),
        "action": None,
    }


def _review_downloads(job) -> bool:
    """Whether approving ``job`` downloads. An Upgrade downloads too, but only
    a read-only library stops it; a queue held in the terminal does not."""
    return queue_recovery._downloads_albums(job) or (
        job is not None and job.execute_kind == "upgrade"
        and storage.music_mounted_read_only())


def _downloads_held_notice():
    """Why downloads alone are paused, or None."""
    return _read_only_library_notice() or _terminal_queue_notice()


def _writes_paused_notice(*, log_details: bool = False,
                          downloads: bool = False):
    """Why downloads and scans are paused, in the user's words, or None.

    One source for both the 503 a blocked request gets and the notice the
    dashboard shows, so the two cannot drift into naming different causes.
    ``log_details`` is for the request path only: the dashboard reads this on
    every load and must not write a log line each time. ``downloads`` is for
    a request that downloads albums, the only work a queue saved in the
    terminal holds.
    """
    if not (runtime._CLI_MODE or runtime._LOCK_BUSY_PID is not None):
        queue_recovery._await_recovery_refresh()
    reason = ""
    action = None
    if runtime._CLI_MODE:
        reason = "Terminal mode is holding the library."
        msg = ("Terminal (CLI) mode is on, so downloads and scans are paused "
               "here. Resume on Settings → Mode (Resume web app).")
        action = {"href": "/settings#mode", "label": "Open Settings"}
    elif runtime._LOCK_BUSY_PID is not None:
        reason = "Another Qobuz Librarian run is using the library."
        msg = ("Another Qobuz Librarian run is using the library, so downloads "
               "and scans here are paused until it ends.")
    elif (unwritable := storage._unwritable_volumes()):
        action = {"href": "/settings#diagnostics", "label": "Open Diagnostics"}
        if all(storage.DOCKER_MADE in p for p in unwritable):
            reason = "A folder path in .env looks mistyped."
            advice = ("Check the folder paths in your .env, then run "
                      "docker compose up -d.")
        else:
            reason = "A folder Qobuz Librarian must write to is read-only."
            advice = ("On a NAS, set PUID/PGID to the share owner and "
                      "confirm the host directories exist.")
        msg = ("Qobuz Librarian can't write where it needs to: "
               f"{'; '.join(unwritable)}. {advice} Downloads can't run "
               "until fixed.")
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
            "Qobuz Librarian could not read its saved recovery state, so "
            "downloads and scans are paused. It tries again on its own; if "
            "this notice remains, the application log says why."
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
    elif not runtime._JOBS_RESTORED:
        reason = "Saved downloads and reviews could not be loaded."
        msg = (reason + " Downloads and scans are paused; "
               "Qobuz Librarian retries automatically.")
        action = {"href": "/settings#diagnostics", "label": "Open Diagnostics"}
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
        else:
            reason = "An interrupted download couldn't be cleared safely."
            named = queue_recovery._startup_recovery_album_label()
            of_album = f" of “{named}”" if named else ""
            if getattr(queue_recovery._STARTUP_RECOVERY_RESULT, "reason",
                       None) == "startup-recovery-unsettled":
                msg = ("Interrupted work could not be checked safely, so "
                       "downloads and scans are paused. Qobuz Librarian "
                       "checks again on its own; the “"
                       + startup_recovery.BLOCKED_DOWNLOAD_LOG_ENTRY
                       + "” entry in the application log says why.")
            else:
                msg = (f"An interrupted download{of_album} could not be "
                       "cleared safely. Downloads and scans are paused, and "
                       "its files were left as they were. "
                       "Qobuz Librarian tries again when it restarts; the "
                       "application log says why it stopped.")
    elif queue_recovery._terminal_queue_waiting():
        return _terminal_queue_notice() if downloads else None
    elif queue_recovery._startup_recovery_status_value() == "resume_required":
        reason = "An interrupted terminal download is waiting."
        action = {"href": "/settings#mode", "label": "Open Settings"}
        msg = ("An interrupted download saved in the terminal is waiting to "
               "finish. Other library changes are paused. Switch to terminal "
               f"mode in Settings, then run “{_terminal_command()}”; it offers "
               "to resume it.")
    elif storage.music_mounted_read_only():
        return _read_only_library_notice() if downloads else None
    else:
        return None
    return {"reason": reason, "msg": msg, "action": action}


def _web_writes_paused() -> bool:
    """True when destructive web work must not run: the same conditions
    _lock_busy_response answers 503 for, as one predicate for the AUTOMATIC
    triggers (dashboard new-release check, library-scan resume) that have no
    request to bounce. Any trigger checking only part of this list quietly
    re-opens the hole the pause exists to close."""
    if (runtime._SHUTTING_DOWN or runtime._CLI_MODE
            or runtime._LOCK_BUSY_PID is not None):
        return True
    queue_recovery._await_recovery_refresh()
    return (
        bool(storage._unwritable_volumes())
        or not storage._data_dir_available()
        or not job_mgr.job_persistence.ready_for_admission()
        or not runtime._JOBS_RESTORED
        or runtime._LOCK_UNENFORCEABLE
        or not runtime._run_lock_intact()
        or (
            queue_recovery._startup_recovery_status_value() in {
                "attention_required",
                "resume_required",
            }
            and not queue_recovery._terminal_queue_waiting()
        )
    )


def _web_downloads_paused() -> bool:
    """_web_writes_paused, or a queue the terminal saved holding downloads."""
    return (_web_writes_paused()
            or bool(queue_recovery._terminal_queue_waiting())
            or storage.music_mounted_read_only())


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
    if (not runtime._CLI_MODE and runtime._run_lock_intact()
            and not runtime._JOBS_RESTORED):
        failed.append("saved_jobs")
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
