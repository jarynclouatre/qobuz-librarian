"""Routes for collection snapshots, backups and staging leftovers."""
import asyncio
import contextlib
import html
import json
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import QobuzAccess
from qobuz_librarian.integrations import staging as staging_mod
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import (
    candidate_premise,
    collection_snapshot,
    generation_state,
    scanner,
)
from qobuz_librarian.library.candidate_premise import CandidateStale
from qobuz_librarian.quality import decision as quality_decision
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import collection_restore, flows, runtime, scans
from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web.csrf import body_limit

router = APIRouter()
_log = logging.getLogger("qobuz_librarian")


@router.post("/collection/snapshot")
async def collection_snapshot_now(request: Request, force: str = Form("")):
    """Write a collection snapshot now instead of waiting for the next scan."""
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    existing = scans._active_scan("collection_snapshot",
                            statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.RUNNING))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    job = job_mgr.Job(title="Collection backup")
    job.execute_kind = "collection_snapshot"
    forced = bool((force or "").strip())
    if job_mgr.submit(
            job, lambda j: flows.run_collection_snapshot(j, force=forced)) is None:
        return runtime._job_admission_response(request)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@router.get("/collection/snapshot/download")
async def collection_snapshot_download(request: Request):
    """Hand the current snapshot to the browser as a file."""
    loop = asyncio.get_running_loop()
    state, _snapshot = await loop.run_in_executor(
        None, collection_snapshot.latest_status)
    if state == "unreadable":
        return RedirectResponse(
            url="/settings?error=" + runtime._notice_key(
                "The current collection backup couldn't be read safely, so it "
                "was not downloaded. Check the backup folder in Settings."),
            status_code=303)
    path = collection_snapshot.latest_path()
    if state != "ready" or not path.is_file():
        return RedirectResponse(
            url="/settings?error=" + runtime._notice_key(
                "There is no snapshot yet. Run a library scan, or use Back up "
                "now, and it will be here."),
            status_code=303)
    return FileResponse(path, media_type="application/json",
                        filename=collection_snapshot.LATEST_NAME)


def _restore_response(request, message, *, redirect=None, kind="error"):
    """Answer the Settings upload form, which posts through htmx."""
    if runtime._is_htmx(request):
        if redirect:
            return HTMLResponse("", headers={"HX-Redirect": redirect})
        return HTMLResponse(runtime._ql_notice_html(kind, html.escape(message)))
    if redirect:
        return RedirectResponse(url=redirect, status_code=303)
    return RedirectResponse(
        url="/settings?error=" + runtime._notice_key(message), status_code=303)


async def _read_backup_upload(upload):
    """The uploaded backup and "", or None and why it was refused.

    Counted as it arrives, so a file larger than any backup, or shaped like
    none, is turned away before the JSON parser builds an object for every
    brace in it.
    """
    limit = body_limit("/collection/restore")
    raw = bytearray()
    containers = commas = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            return raw, ""
        raw += chunk
        if len(raw) > limit:
            return None, "That file is far too large to be a collection backup."
        containers += chunk.count(b"{") + chunk.count(b"[")
        commas += chunk.count(b",")
        if (containers > collection_snapshot.UPLOAD_MAX_CONTAINERS
                or commas > collection_snapshot.UPLOAD_MAX_COMMAS):
            return None, "That file isn't a collection backup from this app."


@router.post("/collection/restore")
async def collection_restore_upload(request: Request,
                                    backup: UploadFile = File(...),
                                    empty_replacement: str = Form("")):
    """Read a collection backup and park one review of what it can put back."""
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    existing = scans._active_scan(
        "collection_restore",
        statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
                  job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.RUNNING))
    if existing is not None:
        # In the page, say so where the form sits and link the open review;
        # a bare redirect there would swap the whole page under the reader
        # without a word about why.
        if runtime._is_htmx(request):
            return HTMLResponse(runtime._ql_notice_html(
                "error",
                "A restore is already open. Finish or discard it before "
                "uploading another backup. "
                f'<a href="/jobs/{existing.id}" class="ql-inline-link">'
                "Open the restore review</a>."))
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    raw, refusal = await _read_backup_upload(backup)
    if raw is None:
        return _restore_response(request, refusal)
    # Each copy goes as soon as the next exists; a large backup otherwise sits
    # in memory three times over while it parses.
    head = raw[:200]
    try:
        text = raw.decode("utf-8-sig")
        del raw
        data = json.loads(text)
        del text
    except (UnicodeDecodeError, ValueError, RecursionError):
        # A backup cut short (a partial download or copy) still opens with
        # this app's format name.
        if collection_snapshot.FORMAT.encode() in head:
            return _restore_response(
                request, "That backup file is incomplete or damaged.")
        return _restore_response(
            request, "That file isn't a collection backup from this app.")
    ok, reason = collection_snapshot.validate_upload(data)
    if not ok:
        return _restore_response(request, reason)
    # An empty music folder that the app's own last backup says held albums is
    # most likely an unmounted share, so the restore is refused unless the
    # upload marks it as an empty replacement folder.
    def _restore_target_state():
        root = Path(cfg.MUSIC_ROOT)
        hint = runtime._music_root_hint()
        problem = scans._music_root_problem()
        if problem:
            return problem, None, None
        scanner.clear_scan_caches()
        try:
            unreadable = []
            artists = scanner.list_library_artists(
                on_artist_error=lambda path, error: unreadable.append(path.name))
            emptied = not artists and not unreadable
        except OSError:
            return f"{root} could not be read. {hint}", None, None
        # The listing reads a folder that has gone as an empty one.
        problem = scans._music_root_problem()
        if problem:
            return problem, None, None
        root_identity = candidate_premise.capture_music_root_identity()
        if root_identity is None:
            return (f"{root} could not be verified as the restore target. "
                    f"{hint}", None, None)
        if not emptied:
            return None, None, root_identity
        backup_state, latest = collection_snapshot.latest_status()
        if backup_state == "unreadable" and empty_replacement != "1":
            return (
                "No music was found in the library folder, and the last "
                "collection backup could not be read safely. Check both "
                "folders before continuing. If this is an intentionally empty "
                "replacement folder, select that option and upload again.",
                None,
                None,
            )
        recorded = ((latest or {}).get("counts") or {}).get("albums")
        if isinstance(recorded, int) and recorded > 0:
            return None, recorded, root_identity
        return None, None, root_identity
    loop = asyncio.get_running_loop()
    root_problem, recorded_albums, root_identity = await loop.run_in_executor(
        None, _restore_target_state)
    if root_problem:
        return _restore_response(
            request, f"{root_problem} No restore was started.")
    if recorded_albums and empty_replacement != "1":
        return _restore_response(
            request,
            f"No music was found in your library folder, but its last backup "
            f"recorded {recorded_albums:,} albums. Check that the folder is "
            f"mounted, then upload again. If this is an empty replacement "
            f"folder, select that option and upload again. The current backup "
            f"will be kept.")
    try:
        credentials = await runtime._authorize_qobuz_for_web(
            QobuzAccess.CATALOGUE_ACTION)
    except runtime._QOBUZ_ACTION_ERRORS as exc:
        return _restore_response(
            request, job_mgr.qobuz_action_error_message(exc, unchanged=True))
    job = job_mgr.Job(title="Restore from backup")
    job.execute_kind = "collection_restore"

    def _scan(j):
        if not candidate_premise.music_root_matches(root_identity):
            raise CandidateStale(
                "The music folder changed before the restore check began. "
                "Check that it is mounted, then upload the backup again."
            )
        active = runtime._authorize_qobuz_live(
            QobuzAccess.CATALOGUE_ACTION,
            expected_generation=credentials.generation,
        )
        collection_restore.scan_restore(j, data, active.token)

    submitted = await scans._submit_scan_deduped_async(
        job,
        _scan,
        runtime._resume_album_download(job, job.execute_args),
        "collection_restore",
        statuses=(job_mgr.JobStatus.PENDING, job_mgr.JobStatus.SCANNING,
                  job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.RUNNING),
    )
    if submitted is None:
        busy = runtime._lock_busy_response(request)
        if busy is not None:
            return busy
        return runtime._job_admission_response(request)
    return _restore_response(request, "", redirect=f"/jobs/{submitted.id}")


def _diagnostics_result_notice(kind: str, body: str) -> str:
    """A Restore/Remove result on the diagnostics list."""
    if kind in ("success", "info"):
        return (f'<div id="download-toast" hx-swap-oob="beforeend">'
                f'{runtime._ql_notice_html(kind, body)}</div>')
    return runtime._ql_notice_html(kind, body)


def _stuck_backup_notice(request: Request, target: Path, headline: str) -> str:
    """A refusal that names the backup's folder and offers to delete it
    unchecked."""
    location, _is_host = runtime._resolve_host_path(str(target))
    return _diagnostics_result_notice(
        "error",
        runtime.templates.get_template("_stuck_backup_notice.html").render(
            request=request, headline=headline, location=location,
            name=target.name),
    )


def _unreadable_record_notice(request: Request, target: Path) -> str:
    return _stuck_backup_notice(
        request, target,
        "This backup's recovery record can't be read, so the app can't tell "
        "what it holds or whether your library already has it. It was left "
        "untouched.")


def _restore_refused_notice(request: Request, target: Path, origin) -> str:
    """Say why an upgrade restore refused, in the figures it refused on."""
    backup_size = backup_mod.album_tree_size(target)
    origin_size = backup_mod.album_tree_size(origin) if origin else None
    origin_display, _is_host = runtime._resolve_host_path(str(origin or ""))
    if (backup_size and origin_size
            and origin_size[1] >= backup_size[1] > 0):
        headline = (
            f"{origin_display} now holds "
            f"{plural(origin_size[0], 'file')} "
            f"({origin_size[1] / 1024 / 1024:.1f} MB) while this backup holds "
            f"{plural(backup_size[0], 'file')} "
            f"({backup_size[1] / 1024 / 1024:.1f} MB). Putting the backup "
            "back would replace the larger album with the smaller one, so it "
            "was left alone.")
    else:
        headline = ("The files couldn't be moved back automatically, so the "
                    "backup was left untouched.")
    return _stuck_backup_notice(request, target, headline)


@contextlib.contextmanager
def _library_held(request: Request, operation: str, verb: str):
    """Hold the library for one diagnostics action. Yields None while it is
    held, or the refusal to answer with when library writes are paused or a
    job is working in the library."""
    state, operation_token, lock = runtime._begin_direct_library_operation(
        operation)
    if state == "paused":
        yield (_diagnostics_result_notice(
                   "warning", f"Library writes were paused before {verb} "
                   "could start. Resume the web app, then try again.")
               + runtime._diagnostics_fragment(request))
        return
    if state == "busy":
        yield (_diagnostics_result_notice(
                   "warning", "A job is working in the library right now. "
                   "Try again once it finishes.")
               + runtime._diagnostics_fragment(request))
        return
    try:
        yield None
    finally:
        lock.release()
        job_mgr.end_library_operation(operation_token)


def _backup_target(backup):
    """The retained backup a diagnostics button names, or None when it is gone
    or the name is not a bare folder name the page could have listed."""
    name = (backup or "").strip()
    target = Path(str(cfg.UPGRADE_BACKUP_DIR)) / name
    if (not name or name != Path(name).name or name.startswith(".")
            or not target.is_dir()):
        return None
    return target


async def _run_diagnostics_action(request: Request, action, arg):
    """Run a diagnostics-list button's work off the event loop and answer with
    its notice and the list redrawn."""
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    loop = asyncio.get_running_loop()
    return HTMLResponse(await loop.run_in_executor(None, action, request, arg))


def _discard_unchecked_backup_sync(request: Request, backup: str) -> str:
    target = _backup_target(backup)
    if target is None:
        return (_diagnostics_result_notice("error", "That backup isn't there anymore. "
                                "It may already be restored or cleaned up.")
                + runtime._diagnostics_fragment(request))
    with _library_held(request, "Backup removal", "Delete") as refusal:
        if refusal:
            return refusal
        location, _is_host = runtime._resolve_host_path(str(target))
        if backup_mod.discard_backup_unchecked(target):
            note = _diagnostics_result_notice(
                "success", "Deleted the backup without checking it.")
        else:
            note = _diagnostics_result_notice(
                "error", "The app couldn't delete the folder. Remove "
                f"{html.escape(location)} outside the app.")
    return note + runtime._diagnostics_fragment(request)


def _restore_backup_sync(request: Request, backup: str) -> str:
    target = _backup_target(backup)
    if target is None:
        return (_diagnostics_result_notice("error", "That backup isn't there anymore. "
                                "It may already be restored or cleaned up.")
                + runtime._diagnostics_fragment(request))
    with _library_held(request, "Backup restore", "Restore") as refusal:
        if refusal:
            return refusal
        carried = backup_mod.load_backup_result(target)
        receipt = carried.receipt if carried is not None else None
        try:
            origin = Path(receipt["origin"])
            kind = receipt["kind"]
        except (KeyError, TypeError, ValueError):
            carried = None
        if carried is None:
            note = _unreadable_record_notice(request, target)
        elif (
            resolution_plan := job_mgr.prepare_recovery_resolution(
                str(carried.path), carried.receipt)
        ) is None:
            note = _diagnostics_result_notice(
                "error", "The saved recovery records could not be checked, "
                "so this backup was left untouched. Check that the data "
                "volume is writable, then try again.")
        elif kind in {"gap-fill", "downsample"}:
            # These backups hold the good originals; the destination may hold a
            # partial or a rewritten copy, so the backup always wins the swap.
            n = backup_mod.restore_gap_fill_backup(
                carried, origin, keep_larger_dst=False)
            if n and kind == "downsample":
                # Undoing a downsample has to undo everything it recorded, not
                # just the files: the album is no longer shrunk, so the cap that
                # hides it from Upgrade must go, and the saved candidate counts
                # for that artist are now wrong on both tool pages.
                try:
                    quality_decision.clear_local_album_cap(origin)
                except OSError:
                    _log.exception(
                        "couldn't clear the downsample cap for %s", origin)
                try:
                    flows._refresh_downsample_artist_state(Path(origin).parent)
                except Exception:
                    _log.exception("downsample state refresh failed after undo")
                try:
                    if generation_state.output_is_current("upgrade"):
                        generation_state.mark_output_status(
                            "upgrade",
                            "stale",
                            reason=(
                                "Upgrade needs refresh after Downsample was "
                                "undone."
                            ),
                        )
                except Exception:
                    _log.exception("upgrade state invalidation failed after undo")
            if n and not carried.exists():
                if job_mgr.resolve_recovery_resolution(resolution_plan):
                    note = _diagnostics_result_notice(
                        "success", f"Restored {plural(n, 'file')} to "
                        f"{html.escape(runtime._resolve_host_path(str(origin))[0])}.")
                else:
                    note = _diagnostics_result_notice(
                        "error", "The files were restored, but their saved "
                        "recovery status could not be updated. History will "
                        "continue to flag the recovery; do not run Restore "
                        "again until the data volume has been checked.")
            elif n:
                note = _diagnostics_result_notice(
                    "warning", f"Restored {plural(n, 'file')}; the rest "
                    "couldn't be "
                    "moved and stay in the backup.")
            else:
                note = _diagnostics_result_notice(
                    "error", "Nothing could be restored. The backup is "
                    "untouched; check the log.")
        elif kind == "upgrade":
            ok = backup_mod.restore_upgrade_backup(carried, origin)
            if ok and job_mgr.resolve_recovery_resolution(resolution_plan):
                note = _diagnostics_result_notice(
                    "success", f"Restored the album to "
                    f"{html.escape(runtime._resolve_host_path(str(origin))[0])}.")
            elif ok:
                note = _diagnostics_result_notice(
                    "error", "The album was restored, but its saved recovery "
                    "status could not be updated. History will continue to "
                    "flag the recovery; do not run Restore again until the "
                    "data volume has been checked.")
            else:
                note = _restore_refused_notice(request, target, origin)
        else:
            note = _diagnostics_result_notice(
                "error", "This backup has an unsupported recovery record, so "
                "it was left untouched.")
    return note + runtime._diagnostics_fragment(request)


@router.post("/backups/restore", response_class=HTMLResponse)
async def restore_backup(request: Request, backup: str = Form("")):
    """Move an orphaned backup's files home. The button on the diagnostics list."""
    return await _run_diagnostics_action(request, _restore_backup_sync, backup)


def _discard_backup_sync(request: Request, backup: str) -> str:
    target = _backup_target(backup)
    if target is None:
        return (_diagnostics_result_notice("error", "That backup isn't there anymore. "
                                "It may already be restored or cleaned up.")
                + runtime._diagnostics_fragment(request))
    with _library_held(request, "Backup removal", "Remove") as refusal:
        if refusal:
            return refusal
        carried = backup_mod.load_backup_result(target)
        if carried is None or carried.receipt is None:
            note = _unreadable_record_notice(request, target)
        elif (
            resolution_plan := job_mgr.prepare_recovery_resolution(
                str(carried.path), carried.receipt)
        ) is None:
            note = _diagnostics_result_notice(
                "error", "The saved recovery records could not be checked, "
                "so this backup was left untouched. Check that the data "
                "volume is writable, then try again.")
        elif backup_mod.discard_redundant_backup(target):
            dest = html.escape(runtime._resolve_host_path(
                str(carried.receipt.get("origin", "")))[0])
            if job_mgr.resolve_recovery_resolution(resolution_plan):
                note = _diagnostics_result_notice(
                    "success", "Removed the backup. Every file it held is "
                    f"verified present at {dest}.")
            else:
                note = _diagnostics_result_notice(
                    "error", "The backup was removed, but its saved recovery "
                    "status could not be updated. History may keep flagging "
                    "the recovery until the data volume has been checked.")
        else:
            note = _diagnostics_result_notice(
                "error", "Couldn't verify every file is back byte-for-byte, "
                "so the backup was left untouched. Restore is the safe way "
                "to bring its files home.")
    return note + runtime._diagnostics_fragment(request)


def _release_undo_copy_sync(request: Request, backup: str) -> str:
    target = _backup_target(backup)
    if target is None:
        return (_diagnostics_result_notice("error", "Those originals aren't there "
                                "anymore. They may already be restored or "
                                "cleared.")
                + runtime._diagnostics_fragment(request))
    with _library_held(request, "Backup removal", "Delete") as refusal:
        if refusal:
            return refusal
        carried = backup_mod.load_backup_result(target)
        if carried is None or carried.receipt is None:
            note = _unreadable_record_notice(request, target)
        elif (
            resolution_plan := job_mgr.prepare_recovery_resolution(
                str(carried.path), carried.receipt)
        ) is None:
            note = _diagnostics_result_notice(
                "error", "The saved recovery records could not be checked, "
                "so these originals were left untouched. Check that the data "
                "volume is writable, then try again.")
        elif backup_mod.release_undo_copy(target):
            album = html.escape(
                runtime._album_name_from_path(carried.receipt.get("origin", "")))
            if job_mgr.resolve_recovery_resolution(resolution_plan):
                note = _diagnostics_result_notice(
                    "success", f"Deleted the hi-res originals of {album}. "
                    "That downsample can no longer be undone.")
            else:
                note = _diagnostics_result_notice(
                    "error", "The hi-res originals were deleted, but their "
                    "saved recovery status could not be updated. History may "
                    "keep flagging the recovery until the data volume has "
                    "been checked.")
        else:
            note = _diagnostics_result_notice(
                "error", "Couldn't confirm the album still holds every one of "
                "these files, so the originals were left where they are. "
                "Restore them instead if the album is incomplete.")
    return note + runtime._diagnostics_fragment(request)


def _staging_group_target(group):
    """The kept group a Remove button names, or None when the name is not a
    bare folder name the page could have listed."""
    name = (group or "").strip()
    if not name or name in (".", "..") or name != Path(name).name:
        return None
    return Path(str(cfg.STAGING_DIR)) / cfg.BEETS_RETRY_DIR / name


def _discard_staging_group_sync(request: Request, group: str) -> str:
    target = _staging_group_target(group)
    if target is None:
        return (_diagnostics_result_notice(
                    "error", "That isn't a group of kept files.")
                + runtime._diagnostics_fragment(request))
    if not target.is_dir():
        return (_diagnostics_result_notice("error", "Those files aren't there anymore.")
                + runtime._diagnostics_fragment(request))
    with _library_held(request, "Staging cleanup", "Remove") as refusal:
        if refusal:
            return refusal
        location, _is_host = runtime._resolve_host_path(str(target))
        stuck = _diagnostics_result_notice(
            "error", "The app couldn't remove them, so they were left where "
            f"they are. The row below offers to delete {html.escape(location)} "
            "without checking it.")
        inspection = staging_mod.inspect_retry_group(target)
        if inspection.status != "ready" or inspection.owner is not None:
            note = _diagnostics_result_notice(
                "error", "These files changed since the app set them aside, "
                "so they were left untouched.")
        elif inspection.file_group is not None:
            removed = staging_mod.discard_file_group(inspection.file_group)
            note = (_diagnostics_result_notice("success", "Removed the kept file.")
                    if removed else stuck)
        else:
            removed = staging_mod.discard_group(target)
            note = (_diagnostics_result_notice("success", "Removed the kept files.")
                    if removed else stuck)
    return note + runtime._diagnostics_fragment(request)


def _discard_staging_group_unchecked_sync(request: Request, group: str) -> str:
    target = _staging_group_target(group)
    if target is None:
        return (_diagnostics_result_notice(
                    "error", "That isn't a group of kept files.")
                + runtime._diagnostics_fragment(request))
    if not target.is_dir():
        return (_diagnostics_result_notice("error", "Those files aren't there anymore.")
                + runtime._diagnostics_fragment(request))
    with _library_held(request, "Staging cleanup", "this") as refusal:
        if refusal:
            return refusal
        location, _is_host = runtime._resolve_host_path(str(target))
        if staging_mod.discard_group_unchecked(target):
            note = _diagnostics_result_notice(
                "success", "Deleted the files without checking them.")
        else:
            note = _diagnostics_result_notice(
                "error", "The app couldn't delete the folder. Remove "
                f"{html.escape(location)} outside the app.")
    return note + runtime._diagnostics_fragment(request)


@router.post("/staging/discard-unchecked", response_class=HTMLResponse)
async def discard_staging_group_unchecked(request: Request,
                                          group: str = Form("")):
    """Delete a held group no automatic route will touch, offered by the row."""
    return await _run_diagnostics_action(request, _discard_staging_group_unchecked_sync, group)


@router.post("/staging/discard", response_class=HTMLResponse)
async def discard_staging_group(request: Request, group: str = Form("")):
    """Delete one group of files the app is holding in staging. The Remove
    button on the diagnostics list."""
    return await _run_diagnostics_action(request, _discard_staging_group_sync, group)


@router.post("/backups/discard", response_class=HTMLResponse)
async def discard_backup(request: Request, backup: str = Form("")):
    """Delete a kept backup once its files are verified home. The Remove button on the diagnostics list."""
    return await _run_diagnostics_action(request, _discard_backup_sync, backup)


@router.post("/backups/release-originals", response_class=HTMLResponse)
async def release_backup_originals(request: Request, backup: str = Form("")):
    """Delete a downsample's kept originals before their days run out."""
    return await _run_diagnostics_action(request, _release_undo_copy_sync, backup)


@router.post("/backups/discard-unchecked", response_class=HTMLResponse)
async def discard_backup_unchecked(request: Request, backup: str = Form("")):
    """Delete a backup no automatic route will touch, offered by the refusal."""
    return await _run_diagnostics_action(request, _discard_unchecked_backup_sync, backup)
