"""Startup and shutdown of the Web app: the run lock, saved jobs and background work."""
import asyncio
import logging
import os
import shutil
import signal
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from qobuz_librarian import config as cfg
from qobuz_librarian import raise_open_file_limit, run_lock
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.integrations import lyrics as lyrics_mode
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import flac_cache, generation_state, repair_cache
from qobuz_librarian.ui_cli import logging as cli_logging
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import (
    diagnostics,
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


def _has_startup_write_authority() -> bool:
    """Whether this Web process owns the real single-writer boundary."""
    return runtime._run_lock_intact() and not write_gate._web_writes_paused()


def _shutdown_web_mutations() -> None:
    """Quiesce every Web writer before releasing the process run lock."""
    job_mgr.stop_worker()
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
    """Publish a lease only while recovery and saved jobs are reconciled."""
    runtime._RUN_LOCK_HANDLE = lease
    try:
        settings_store.reload_from_disk()
        result = queue_recovery._record_startup_recovery(lease)

        publication_recovery = (
            generation_state.reconcile_interrupted_library_publication(lease)
        )
        if publication_recovery is None:
            raise RuntimeError(
                "interrupted Library publication state could not be saved"
            )
        if publication_recovery:
            _log.warning(
                "Recovered a Library crawl interrupted before its saved view "
                "was published."
            )
        if restore_jobs:
            _restore_jobs_once()
        return result
    except BaseException:
        runtime._RUN_LOCK_HANDLE = None
        _close_web_run_lock(lease)
        raise


async def _retry_web_run_lock(log, *, delay: float = 30) -> None:
    """Retry a busy lock and an acquired lease whose recovery read failed."""
    while runtime._LOCK_BUSY_PID is not None or queue_recovery._STARTUP_RECOVERY_UNKNOWN:
        await asyncio.sleep(delay)
        with runtime._auto_check_lock:
            if runtime._CLI_MODE:
                return
        try:
            lease = run_lock.acquire("web")
        except run_lock.LockBusy as busy:
            with runtime._auto_check_lock:
                if runtime._CLI_MODE:
                    return
                runtime._LOCK_BUSY_PID = busy.pid
            continue

        with runtime._auto_check_lock:
            if runtime._CLI_MODE:
                if lease is not None:
                    _close_web_run_lock(lease)
                return
            if lease is None:
                runtime._RUN_LOCK_HANDLE = None
                runtime._LOCK_BUSY_PID = None
                runtime._LOCK_UNENFORCEABLE = True
                log.error(
                    "Run-lock became unenforceable; download/scan endpoints "
                    "paused until a lock-capable data folder is available "
                    "and the app is restarted."
                )
                return
            try:
                result = _recover_under_web_run_lock(lease)
            except Exception:
                runtime._LOCK_BUSY_PID = None
                runtime._LOCK_UNENFORCEABLE = False
                log.exception(
                    "Lock acquired, but durable recovery could not be read; "
                    "the lease was released and Web will retry."
                )
                continue
            runtime._LOCK_UNENFORCEABLE = False
            runtime._LOCK_BUSY_PID = None
        log.info(
            "Lock acquired; durable queue startup state: %s.",
            result.status.value,
        )
        return


def _restore_jobs_once() -> None:
    with runtime._JOBS_RESTORE_LOCK:
        if runtime._JOBS_RESTORED:
            return
        # Ahead of the restore, which would otherwise reopen a gone backup as a
        # failure pointing at the notice this settles away, and ahead of the
        # first page render, which carries the attention count.
        rendering._retire_gone_recoveries(job_persistence.recovery_history())
        try:
            job_mgr.restore_jobs(
                job_runs._RESUME_EXECUTE,
                durable_recovery_clear=(
                    queue_recovery._startup_recovery_status_value() == "clear"
                ),
                durable_recovery_job_id=queue_recovery._startup_recovery_web_job_id(),
                requeue=job_runs._requeued_download_run,
            )
        except Exception as exc:
            _log.warning(
                "couldn't restore prior jobs: %s. Starting fresh.",
                exc,
            )
        finally:
            # restore_jobs publishes into the registry only after it has built
            # the full batch.
            runtime._JOBS_RESTORED = True


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
    raise_open_file_limit()
    runtime._SHUTTING_DOWN = False
    runtime._STOP_SIGNALLED.clear()
    _watch_stop_signal(asyncio.get_running_loop())
    with runtime._JOBS_RESTORE_LOCK:
        runtime._JOBS_RESTORED = False
    queue_recovery._STARTUP_RECOVERY_RESULT = None
    queue_recovery._STARTUP_RECOVERY_UNKNOWN = False
    job_mgr.set_durable_recovery_job_id(None)
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
    settings_store.load()
    try:
        cfg.validate_storage_roots()
    except ValueError as exc:
        raise RuntimeError(f"invalid storage paths: {exc}") from None
    # If creds are provided via env vars, mirror them into the streamrip
    # config now so web-triggered downloads don't fail on streamrip's
    # interactive auth prompt (the env-var path doesn't otherwise reach
    # streamrip's own config file).
    if api_auth.sync_streamrip_creds_from_env() is False:
        _log.warning("Couldn't write env credentials into the streamrip "
                     "config; web downloads may fail until creds are set "
                     "via the Settings page.")
    api_auth.enforce_streamrip_disc_folders()
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
                result = _recover_under_web_run_lock(
                    runtime._RUN_LOCK_HANDLE,
                    restore_jobs=False,
                )
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
        lock_retry_task = (
            asyncio.create_task(_retry_web_run_lock(_log))
            if runtime._LOCK_BUSY_PID is not None
            else None
        )

        problems = storage._unwritable_volumes()
        if problems:
            _log.error("STARTUP: critical volumes not usable: %s. Write "
                       "endpoints will return 503 until the mounts are "
                       "fixed.", problems)
        # Heavy, throttled maintenance: prune_missing() stats every cached file
        # (100k+ on a NAS library), so run it in the background instead of blocking
        # the app from serving its first request.
        async def _bg_prune_flac_cache():
            try:
                n_pruned = await asyncio.get_running_loop().run_in_executor(
                    None, flac_cache.prune_missing)
                if n_pruned:
                    _log.info("Pruned %d stale tag-cache entries.", n_pruned)
            except Exception as e:
                _log.debug("flac-cache prune error: %s", e)
            try:
                repair_cache.prune_expired()
            except Exception as e:
                _log.debug("repair-cache prune error: %s", e)
        maintenance_task = None
        run_startup_maintenance = _has_startup_write_authority()
        if run_startup_maintenance:
            # The CLI runs these too. A browsing-only Web process must leave them
            # alone because the CLI or another Web process can own the library.
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
        job_mgr.configure_staging_entry_guard(queue_recovery._staging_entry_allowed)
        job_mgr.configure_held_release(lambda: not write_gate._web_writes_paused())
        job_mgr.start_worker()
        if run_startup_maintenance:
            maintenance_token = job_mgr.begin_library_operation(
                "Startup maintenance")
            if maintenance_token is None:
                raise RuntimeError("Web workers stopped during startup")

            async def _registered_cache_prune():
                try:
                    await _bg_prune_flac_cache()
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
        # A second Web process must not rebadge the first process's live jobs
        # as failed merely because it cannot take the run lock.
        if runtime._run_lock_intact():
            _restore_jobs_once()
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
                if not run_startup_maintenance:
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
