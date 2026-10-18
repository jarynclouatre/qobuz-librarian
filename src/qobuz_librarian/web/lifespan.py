"""Startup and shutdown of the Web app: the run lock, saved jobs and background work."""
import asyncio
import logging
import os
import shutil
import signal
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from qobuz_librarian import beets_db, disk_support, raise_open_file_limit, run_lock
from qobuz_librarian import config as cfg
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.integrations import beets as beets_mod
from qobuz_librarian.integrations import lyrics as lyrics_mode
from qobuz_librarian.integrations import staging as staging_mod
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import flac_cache, repair_cache
from qobuz_librarian.modes import process as process_mode
from qobuz_librarian.queue import startup_recovery
from qobuz_librarian.ui_cli import logging as cli_logging
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import (
    diagnostics,
    flows,
    job_persistence,
    job_runs,
    new_release_checks,
    qobuz_access,
    queue_recovery,
    rendering,
    runtime,
    settings_store,
    storage,
    write_gate,
)
from qobuz_librarian.web import jobs as job_mgr

_log = logging.getLogger("qobuz_librarian")


class StartupRefused(RuntimeError):
    """A start-up refusal whose message already says what to fix.

    Starlette hands uvicorn the formatted traceback to log, which put dozens
    of framework lines above the message. Formatting reads the traceback
    through this attribute, so the log shows the message alone; the
    interpreter keeps the real one.
    """

    @property
    def __traceback__(self):
        return None

    @__traceback__.setter
    def __traceback__(self, value):
        BaseException.__traceback__.__set__(self, value)


def _has_startup_write_authority() -> bool:
    """Whether this Web process owns the real single-writer boundary."""
    return runtime._run_lock_intact() and not write_gate._web_writes_paused()


# Set once the start-up clean-up has run, which waits for the library when
# another run holds it as the app starts.
_startup_maintained = False
_put_back_pending = False
# Held, never waited for, for a whole Resume web app and for each later
# put-back of albums left in their backups, so Switch and Resume can say
# which is running.
_TAKING_BACK = threading.Lock()
_putting_back = False


def _put_back_unless_taking_back(put_back) -> None:
    """Run ``put_back`` holding ``_TAKING_BACK``. While a Resume holds it,
    the put-back waits for the next try."""
    global _put_back_pending, _putting_back
    if not _TAKING_BACK.acquire(blocking=False):
        _put_back_pending = True
        return
    _putting_back = True
    try:
        put_back()
    finally:
        _putting_back = False
        _TAKING_BACK.release()


def _startup_cleanup() -> None:
    """Expire old upgrade backups and drop lyric state for files that are
    gone. The CLI runs these too; only the process holding the library may."""
    try:
        n = backup_mod.cleanup_old_upgrade_backups()
        if n:
            _log.info(
                "Cleaned up %s at startup.",
                plural(n, "stale upgrade backup"),
            )
    except Exception as e:
        _log.debug("upgrade-backup cleanup error at startup: %s", e)
    try:
        lyrics_mode._prune_lyric_state_orphans()
    except Exception as e:
        _log.debug("lyric-state prune error at startup: %s", e)
    try:
        if startup_recovery.saved_queue_operation_ids() == set():
            staging_mod.remove_empty_retry_groups()
        staging_mod.remove_emptied_kept_runs()
    except Exception as e:
        _log.debug("staging clean-up error at startup: %s", e)


def _prune_caches() -> None:
    try:
        n_pruned = flac_cache.prune_missing()
        if n_pruned:
            _log.info("Pruned %d stale tag-cache entries.", n_pruned)
    except Exception as e:
        _log.debug("flac-cache prune error: %s", e)
    try:
        repair_cache.prune_expired()
    except Exception as e:
        _log.debug("repair-cache prune error: %s", e)


def _late_startup_maintenance() -> None:
    """Run the start-up clean-up once the library is this process's, when
    another run held it as the app started. Work already running here keeps
    it waiting for the next try."""
    global _startup_maintained
    if _startup_maintained:
        return
    state, token, lock = write_gate._begin_direct_library_operation(
        "Startup maintenance")
    if state != "ok":
        return
    _startup_maintained = True
    try:
        _put_back_unless_taking_back(_put_back_albums)
        _startup_cleanup()
    finally:
        lock.release()
        job_mgr.end_library_operation(token)
    token = job_mgr.begin_library_operation("Startup maintenance")
    if token is None:
        return
    try:
        _prune_caches()
    finally:
        job_mgr.end_library_operation(token)


def _shutdown_web_mutations() -> None:
    """Quiesce every Web writer before releasing the process run lock."""
    job_mgr.stop_worker()
    job_mgr.flush_pending_saves()
    job_mgr.configure_staging_entry_guard(None)
    job_mgr.configure_held_release(None)
    if runtime._RUN_LOCK_HANDLE is not None:
        try:
            runtime._RUN_LOCK_HANDLE.close()
        except OSError:
            pass
        runtime._RUN_LOCK_HANDLE = None


async def _finish_web_lifespan(
    ticker,
    lock_retry_task,
    maintenance_task,
    token_probe_task,
) -> None:
    """Stop background work, then release the run lock after all writers."""
    with runtime._auto_check_lock:
        runtime._SHUTTING_DOWN = True
    # First, so running work unwinds while the rest shuts down.
    job_mgr.stop_for_restart()
    for task in (ticker, lock_retry_task, token_probe_task):
        if task is not None:
            task.cancel()
    try:
        for task in (ticker, lock_retry_task, token_probe_task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        if maintenance_task is not None:
            await maintenance_task
    finally:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _shutdown_web_mutations)


def _close_web_run_lock(lease) -> None:
    """Release a candidate Web lease without hiding a close failure."""
    try:
        lease.close()
    except OSError:
        _log.exception(
            "couldn't release a rejected Web run-lock lease"
        )


def _recover_under_web_run_lock(lease, *, restore_jobs: bool = True):
    """Publish a lease only while recovery, the upgrades a hard stop cut
    short and saved jobs are reconciled."""
    runtime._RUN_LOCK_HANDLE = lease
    try:
        settings_store.reload_from_disk()
        result = queue_recovery._record_startup_recovery(lease, settle=True)
        if restore_jobs and runtime._JOBS_RESTORED:
            _settle_interrupted_upgrades()
            _settle_restarted_web_jobs()
        elif restore_jobs:
            _restore_jobs_once()
        return result
    except BaseException:
        runtime._RUN_LOCK_HANDLE = None
        _close_web_run_lock(lease)
        raise


def _settle_restarted_web_jobs() -> None:
    """Apply what settling did to the web jobs already restored."""
    unsettled = queue_recovery._unsettled_web_job_ids()
    job_mgr.settle_restarted_downloads({
        job_id: settled
        for job_id, settled in queue_recovery.take_settled_downloads().items()
        if job_id not in unsettled
    })


async def _retry_web_run_lock(log, *, delay: float = 30) -> None:
    """Retry lock acquisition and saved-job restoration while Web is active,
    read the saved recovery state again after a read of it failed, and run
    the start-up clean-up once they succeed."""
    while True:
        await asyncio.sleep(delay)
        if not _startup_maintained and not runtime._SHUTTING_DOWN:
            await asyncio.get_running_loop().run_in_executor(
                None, _late_startup_maintenance)
        elif _put_back_pending and not runtime._SHUTTING_DOWN:
            await asyncio.get_running_loop().run_in_executor(
                None, _put_back_unless_taking_back,
                _put_back_albums_left_in_backups)
        if runtime._SHUTTING_DOWN:
            return
        if await asyncio.get_running_loop().run_in_executor(
                None, _retry_web_run_lock_once, log):
            await asyncio.get_running_loop().run_in_executor(
                None, _put_back_unless_taking_back,
                _put_back_albums_left_in_backups)


def _retry_web_run_lock_once(log) -> bool:
    """One try of the lock retry, off the event loop. True once it took the
    lock back."""
    with runtime._auto_check_lock:
        if runtime._SHUTTING_DOWN:
            return False
        if runtime._CLI_MODE:
            return False
        if runtime._run_lock_intact():
            _recheck_recovery()
            if not runtime._JOBS_RESTORED:
                _restore_jobs_once()
            return False
        if (runtime._LOCK_BUSY_PID is None
                and not queue_recovery._STARTUP_RECOVERY_UNKNOWN):
            return False
    try:
        lease = run_lock.acquire("web")
    except run_lock.LockBusy as busy:
        with runtime._auto_check_lock:
            if runtime._CLI_MODE or runtime._run_lock_intact():
                return False
            runtime._LOCK_BUSY_PID = busy.pid
        return False

    with runtime._auto_check_lock:
        if runtime._CLI_MODE or runtime._SHUTTING_DOWN:
            if lease is not None:
                _close_web_run_lock(lease)
            return False
        if lease is None:
            runtime._RUN_LOCK_HANDLE = None
            runtime._LOCK_BUSY_PID = None
            runtime._LOCK_UNENFORCEABLE = True
            log.error(
                "Run-lock became unenforceable; download/scan endpoints "
                "paused until a lock-capable data folder is available "
                "and the app is restarted."
            )
            return False
        try:
            result = _recover_under_web_run_lock(lease)
        except Exception:
            runtime._LOCK_BUSY_PID = None
            runtime._LOCK_UNENFORCEABLE = False
            log.exception(
                "Lock acquired, but durable recovery could not be read; "
                "the lease was released and Web will retry."
            )
            return False
        runtime._LOCK_UNENFORCEABLE = False
        runtime._LOCK_BUSY_PID = None
    log.info(
        "Lock acquired; durable queue startup state: %s.",
        result.status.value,
    )
    return True


def _recheck_recovery() -> None:
    """Read the saved recovery state again, settling as a start does, after
    a read of it failed with the lock kept. Waits while work here is running
    or queued to run."""
    if (not queue_recovery._recovery_read_failed()
            or job_mgr.work_in_flight()):
        return
    try:
        queue_recovery._record_startup_recovery(
            runtime._RUN_LOCK_HANDLE, settle=True)
    except Exception as exc:
        _log.warning(
            "couldn't read the saved recovery state again; downloads and "
            "scans stay paused: %s",
            exc,
        )
        return
    if runtime._JOBS_RESTORED:
        _settle_restarted_web_jobs()


def _restore_jobs_once() -> bool:
    with runtime._JOBS_RESTORE_LOCK:
        if runtime._JOBS_RESTORED:
            return True
        try:
            job_persistence.init()
            if not job_persistence.ready_for_admission():
                return False
            rendering._retire_gone_recoveries(
                job_persistence.recovery_history(strict=True))
            finished, put_back = _settle_interrupted_upgrades()
            job_mgr.restore_jobs(
                job_runs._RESUME_EXECUTE,
                unsettled=queue_recovery._unsettled_web_job_ids(),
                cut_off=queue_recovery.settled_downloads(),
                requeue=job_runs._requeued_download_run,
                return_picks=flows.return_interrupted_picks,
                finished_upgrades=finished,
                journaled=queue_recovery._journaled_web_job_ids(),
            )
        except Exception as exc:
            _log.warning(
                "couldn't restore saved jobs; downloads stay paused: %s",
                exc,
            )
            return False
        queue_recovery.take_settled_downloads()
        flows.note_upgrades_put_back(put_back)
        runtime._JOBS_RESTORED = True
        return True


def _settle_interrupted_upgrades() -> tuple[list, list]:
    """Finish or undo the upgrades a hard stop cut short, before the saved
    jobs say what their runs did. Returns the album folders upgraded, and
    those put back."""
    if storage._unwritable_volumes():
        return [], []
    try:
        if beets_db.remove_old_copies():
            _log.info("Removed the beets database copies an earlier version left.")
        beets_mod.remove_stale_import_overrides()
        finished, put_back = process_mode.finish_interrupted_upgrades()
    except Exception:
        _log.exception("couldn't settle the upgrades a hard stop cut short")
        return [], []
    runtime.note_restored_albums(put_back)
    return [str(path) for path in finished], put_back


def _note_put_back(origins) -> None:
    """Tell the dashboard and the Upgrade runs a restart cut short about
    the albums the start-up put back."""
    runtime.note_restored_albums(origins)
    flows.note_upgrades_put_back(origins)


def _watch_stop_signal(loop) -> None:
    """Close live streams and start winding down work on SIGTERM, then let
    the server's own handler begin its shutdown."""
    if threading.current_thread() is not threading.main_thread():
        return
    previous = signal.getsignal(signal.SIGTERM)
    if not callable(previous):
        return

    def _on_stop(signum, frame):
        runtime._STOP_SIGNALLED.set()
        loop.call_soon_threadsafe(
            lambda: loop.run_in_executor(None, job_mgr.stop_for_restart))
        previous(signum, frame)

    signal.signal(signal.SIGTERM, _on_stop)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _startup_maintained
    _startup_maintained = False
    raise_open_file_limit()
    # A scan keeps several threads busy. Passing the interpreter between
    # threads every millisecond rather than every five keeps pages and
    # their files answering while one runs.
    sys.setswitchinterval(0.001)
    runtime._SHUTTING_DOWN = False
    runtime._STOP_SIGNALLED.clear()
    _watch_stop_signal(asyncio.get_running_loop())
    with runtime._JOBS_RESTORE_LOCK:
        runtime._JOBS_RESTORED = False
    queue_recovery._STARTUP_RECOVERY_RESULT = None
    queue_recovery._STARTUP_RECOVERY_UNKNOWN = False
    settings_store.load()
    try:
        cfg.validate_storage_roots()
    except ValueError as exc:
        raise StartupRefused(f"invalid storage paths: {exc}") from None
    qobuz_access._scrub_stored_credentials(_log)
    cli_logging.attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL)
    if web_auth.auth_disabled():
        _log.warning("[warn] WEB_AUTH=none: the web UI is unauthenticated, do not "
                     "expose to an untrusted network")
    else:
        try:
            cred_status = web_auth.apply_env_credentials()
        except web_auth.CredentialSeedError as exc:
            raise RuntimeError(str(exc)) from None
        except web_auth.PasswordRejected as exc:
            raise RuntimeError(
                f"WEB_AUTH_PASSWORD was rejected: {exc}") from None
        if (
            cred_status in {"partial", "failed"}
            and not web_auth.credentials_configured()
        ):
            if cred_status == "partial":
                raise RuntimeError(
                    "Incomplete web login seed: set both WEB_AUTH_USER and "
                    "WEB_AUTH_PASSWORD (or WEB_AUTH_PASSWORD_FILE)."
                )
            raise RuntimeError(
                "The seeded web login could not be saved to "
                f"{cfg.WEB_AUTH_FILE}; refusing to start with an open "
                "first-run setup screen."
            )
        if cred_status == "applied":
            _log.info("Configured the web login from WEB_AUTH_USER / "
                      "WEB_AUTH_PASSWORD.")
        elif cred_status == "kept":
            _log.info("Kept the password set in Settings. Change "
                      "WEB_AUTH_PASSWORD and restart to reset it.")
        elif cred_status == "partial":
            _log.warning("Set both WEB_AUTH_USER and WEB_AUTH_PASSWORD to seed "
                         "the web login from the environment: only one was set.")
        elif cred_status == "failed":
            _log.warning("Couldn't write the web login from the environment; "
                         "the data volume may not be writable.")
        if web_auth.creds_file_present_but_unreadable():
            _log.warning(
                "The web login in %s can't be read, so every page will answer "
                "503. Set WEB_AUTH_USER / WEB_AUTH_PASSWORD and restart to "
                "replace it, or stop the app and delete the file to set a new "
                "login.", cfg.WEB_AUTH_FILE)
        elif not web_auth.credentials_configured():
            _log.warning(
                "No web login configured. The open /setup screen is reachable "
                "to whoever hits the port first, who would then own the admin "
                "account. Seed WEB_AUTH_USER / WEB_AUTH_PASSWORD (compose) to "
                "close this window, and complete setup promptly on a trusted "
                "network.")
    api_auth.check_env_credentials()
    api_auth.sweep_streamrip_run_copies()
    # Acquire the shared run lock so separate CLI runs cannot overlap Web work.
    if os.environ.get("QL_CLI_ONLY", "").strip().lower() in ("1", "true", "yes", "on"):
        # Terminal-first deployment leaves the lock free for a CLI process.
        runtime._CLI_MODE = True
        runtime._LOCK_BUSY_PID = None
        queue_recovery._STARTUP_RECOVERY_RESULT = None
        queue_recovery._STARTUP_RECOVERY_UNKNOWN = False
        _log.info("QL_CLI_ONLY set: starting in terminal (CLI) mode; the web "
                  "app holds no lock and download/scan endpoints are paused.")
    else:
        runtime._CLI_MODE = False
        try:
            runtime._RUN_LOCK_HANDLE = run_lock.acquire("web")
            runtime._LOCK_BUSY_PID = None
            if runtime._RUN_LOCK_HANDLE is None:
                # None isn't success: the lock can't be ENFORCED here, and
                # storing it as acquired would leave the corruption guard
                # silently off.
                runtime._LOCK_UNENFORCEABLE = True
                _log.error(
                    "STARTUP: the data dir can't hold the single-writer lock; "
                    "download/scan endpoints paused. Move the data folder to "
                    "a writable filesystem with file locking, then restart "
                    "the app.")
            else:
                runtime._LOCK_UNENFORCEABLE = False
                try:
                    result = _recover_under_web_run_lock(
                        runtime._RUN_LOCK_HANDLE,
                        restore_jobs=False,
                    )
                except Exception:
                    _log.exception(
                        "Lock acquired, but durable recovery could not be read; "
                        "the lease was released and Web will retry."
                    )
                else:
                    _log.info(
                        "Durable queue startup state: %s.",
                        result.status.value,
                    )
        except run_lock.LockBusy as busy:
            runtime._LOCK_BUSY_PID = busy.pid
            _log.error(
                "STARTUP: another Qobuz Librarian run holds the lock (pid %s). "
                "Background task will retry acquisition every 30s; in the "
                "meantime download/scan endpoints will return 503.",
                busy.pid,
            )

    lock_retry_task = None
    maintenance_task = None
    ticker = None
    token_probe_task = None
    try:
        lock_retry_task = asyncio.create_task(_retry_web_run_lock(_log))

        problems = storage._unwritable_volumes()
        if problems:
            _log.error("STARTUP: critical volumes not usable: %s. Write "
                       "endpoints will return 503 until the mounts are "
                       "fixed.", problems)
        for label, ok, detail in await asyncio.get_running_loop().run_in_executor(
                None, disk_support.report):
            if not ok:
                _log.warning("STARTUP: %s: %s", label, detail)
        maintenance_task = None
        if runtime._run_lock_intact():
            _restore_jobs_once()
        run_startup_maintenance = _has_startup_write_authority()
        if run_startup_maintenance:
            # A browsing-only Web process must leave these alone because the
            # CLI or another Web process can own the library; it runs them
            # once it holds the library instead.
            _put_back_albums_left_in_backups()
            _startup_cleanup()
        job_mgr.configure_staging_entry_guard(queue_recovery._staging_entry_allowed)
        job_mgr.configure_held_release(
            lambda: not write_gate._web_downloads_paused())
        job_mgr.start_worker()
        if run_startup_maintenance:
            maintenance_token = job_mgr.begin_library_operation(
                "Startup maintenance")
            if maintenance_token is None:
                raise RuntimeError("Web workers stopped during startup")
            _startup_maintained = True

            # Heavy, throttled maintenance: prune_missing() stats every cached
            # file (100k+ on a NAS library), so run it in the background
            # instead of blocking the app from serving its first request.
            async def _registered_cache_prune():
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, _prune_caches)
                finally:
                    job_mgr.end_library_operation(maintenance_token)

            maintenance_task = asyncio.create_task(_registered_cache_prune())
        if not shutil.which("rip"):
            _log.warning("`rip` (streamrip) not found in PATH; downloads will fail")
        _beets_python, beets_failure = diagnostics._beets_runtime_diagnostic()
        if _beets_python is None:
            _log.warning("%s; imports will fail", beets_failure)
        if not shutil.which("flac"):
            _log.warning("`flac` not found; FLAC integrity checks fall back to a size heuristic")
        if not shutil.which("ffmpeg"):
            _log.warning("`ffmpeg` not found; hi-res downsampling disabled")
        # Probe the saved token against Qobuz so a stale slot (non-empty but
        # not actually authenticated) surfaces in the dashboard banner rather
        # than failing the user's first search.
        token_probe_task = asyncio.create_task(qobuz_access._probe_token())
        # Keep the dashboard banner honest after startup: any in-session 401 from
        # the API client flips _TOKEN_VALID to False here, so a token that expires
        # mid-session shows "saved token isn't authenticating" immediately instead
        # of leaving stale green until the user happens to retry the failed action.
        api_auth.register_auth_state_listener(qobuz_access._on_auth_state)

        # The dashboard kicks off _maybe_auto_check_new_releases on load, but
        # a headless box nobody opens would never check at all, making
        # NEW_RELEASE_CHECK_INTERVAL a dead letter exactly where it matters
        # most.
        async def _auto_check_ticker():
            loop = asyncio.get_running_loop()
            while True:
                await asyncio.sleep(900)
                try:
                    await loop.run_in_executor(None, new_release_checks._maybe_auto_check_new_releases)
                except Exception as e:
                    _log.warning("background new-release tick failed: %s", e)
                if not _startup_maintained:
                    continue
                # Retention is a promise made in the diagnostics list ("cleared
                # automatically after N days"), and a container that is never
                # restarted would never keep it. The sweep stamps itself and
                # returns early inside a day, so ticking it is nearly free.
                try:
                    swept = await loop.run_in_executor(
                        None, _sweep_upgrade_backups)
                    if swept:
                        _log.info("Cleaned up %s.",
                                  plural(swept, "stale upgrade backup"))
                except Exception as e:
                    _log.debug("upgrade-backup cleanup error: %s", e)

        # Always armed: the helper reads NEW_RELEASE_CHECK_INTERVAL live, so a
        # Settings change (off to on, or a new interval) takes effect at the next
        # tick without a restart.
        ticker = asyncio.create_task(_auto_check_ticker())
        if cfg.NEW_RELEASE_CHECK_INTERVAL > 0:
            hours = max(1, round(cfg.NEW_RELEASE_CHECK_INTERVAL / 3600))
            _log.info("New-release checks run in the background every "
                      "%s; set NEW_RELEASE_CHECK_INTERVAL=0 to turn them off.",
                      plural(hours, "hour"))
        yield
    finally:
        await _finish_web_lifespan(
            ticker, lock_retry_task, maintenance_task, token_probe_task)


def _put_back_albums_left_in_backups() -> None:
    """Restore the albums a hard stop left in their whole-album backups.

    Takes the same gate and steps as Restore in Diagnostics, so it runs only
    with exact write authority and while nothing works in the library, and
    it restores only into a place that is still empty. While the library is
    busy it stays pending, and the lock retry loop tries again.
    """
    global _put_back_pending
    try:
        state, operation_token, lock = (
            write_gate._begin_direct_library_operation("Backup restore"))
        if state != "ok":
            _put_back_pending = True
            return
        try:
            _put_back_albums()
        finally:
            lock.release()
            job_mgr.end_library_operation(operation_token)
    except Exception:
        _log.exception("couldn't put back albums left in their backups")


def _put_back_albums() -> None:
    """The put-back itself, for a caller already holding the library."""
    global _put_back_pending
    _put_back_pending = False
    try:
        backup_mod.settle_interrupted_restores()
        for path, origin in backup_mod.albums_left_in_backups():
            backup = backup_mod.album_backup_to_put_back(path, origin)
            if backup is None:
                continue
            plan = job_mgr.prepare_recovery_resolution(
                str(backup.path), backup.receipt)
            if plan is None or not backup_mod.restore_upgrade_backup(
                    backup, origin, replace_partial=False):
                continue
            _note_put_back([origin])
            _log.warning(
                "Put back %s, which an interrupted upgrade or repair had "
                "moved to %s.", origin, path)
            if not job_mgr.resolve_recovery_resolution(plan):
                _log.warning(
                    "The saved recovery status of %s could not be "
                    "updated.", origin)
        for origin in backup_mod.put_back_interrupted_backups():
            _note_put_back([origin])
            _log.warning(
                "Put back %s, which a hard stop had left part way into its "
                "backup.", origin)
    except Exception:
        _log.exception("couldn't put back albums left in their backups")


def _sweep_upgrade_backups():
    """Expire old upgrade backups when nothing else is writing to the library.

    Gives up rather than waiting if the library is busy: the sweep only does
    real work once a day, and the next tick is fifteen minutes away.
    """
    state, operation_token, lock = write_gate._begin_direct_library_operation(
        "Backup retention")
    if state != "ok":
        return 0
    try:
        return backup_mod.cleanup_old_upgrade_backups()
    finally:
        lock.release()
        job_mgr.end_library_operation(operation_token)
