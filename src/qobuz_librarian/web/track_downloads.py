"""Single-track downloads and the Undo record each one leaves."""
import copy
import logging
import os
from pathlib import Path

from qobuz_librarian import completion, redaction, run_lock
from qobuz_librarian import config as cfg
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.api.auth import CredentialChanged, QobuzAccess
from qobuz_librarian.library import catalog, post_import_relocation
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.post_import_relocation import PostImportRelocationAttention
from qobuz_librarian.queue import builder as queue_builder
from qobuz_librarian.queue import executor as queue_executor
from qobuz_librarian.web import (
    flows,
    job_persistence,
    owned_paths,
    qobuz_access,
    queue_recovery,
    storage,
)
from qobuz_librarian.web import jobs as job_mgr

_log = logging.getLogger("qobuz_librarian")


def _single_ownership_item(payload):
    """Validate the sealed one-item evidence before touching the library."""
    root = Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
        or payload.get("sealed") is not True
        or payload.get("root") != str(root)
        or not owned_paths._valid_ownership_identity(payload.get("root_identity"))
    ):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1:
        return None
    item = items[0]
    if not isinstance(item, dict) or not owned_paths._valid_ownership_identity(item.get("file")):
        return None
    relative = item.get("relative")
    if not isinstance(relative, str):
        return None
    file_relative = owned_paths._strict_ownership_relative(root, relative)
    if file_relative is None:
        return None
    created = item.get("created_directories")
    if not isinstance(created, list):
        return None
    seen = set()
    for record in created:
        if not owned_paths._valid_ownership_identity(record):
            return None
        directory_relative = record.get("relative")
        if not isinstance(directory_relative, str):
            return None
        directory_relative = owned_paths._strict_ownership_relative(
            root, directory_relative)
        if (
            directory_relative is None
            or len(directory_relative.parts) >= len(file_relative.parts)
            or file_relative.parts[:len(directory_relative.parts)]
            != directory_relative.parts
        ):
            return None
        identity = (record["device"], record["inode"])
        if identity in seen:
            return None
        seen.add(identity)
    companions = item.get("companions")
    if not isinstance(companions, list) or len(companions) > 1:
        return None
    artwork = None
    if companions:
        receipt = companions[0]
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"kind", "relative", "file"}
            or receipt.get("kind") != "artwork"
            or not owned_paths._valid_ownership_identity(receipt.get("file"))
            or not isinstance(receipt.get("relative"), str)
        ):
            return None
        artwork_relative = owned_paths._strict_ownership_relative(
            root, receipt["relative"])
        if (
            artwork_relative is None
            or artwork_relative == file_relative
            or len(artwork_relative.parent.parts) >= len(file_relative.parts)
            or file_relative.parts[:len(artwork_relative.parent.parts)]
            != artwork_relative.parent.parts
        ):
            return None
        artwork = {
            "path": root / artwork_relative,
            "file_identity": receipt["file"],
            "relative": artwork_relative,
        }
    return {
        "root": root,
        "path": root / file_relative,
        "file_identity": item["file"],
        "root_identity": payload["root_identity"],
        "created_directories": created,
        "artwork": artwork,
    }


def _single_owned_path(
    payload,
    landed_dir=None,
    created_after_import=None,
    created_files_after_import=None,
):
    """Bind only the exact destination reported by the one-run beets hook."""
    item = _single_ownership_item(payload)
    if item is None:
        return None
    created_directories = list(item["created_directories"])
    for record in created_after_import or ():
        if not owned_paths._valid_ownership_identity(record):
            return None
        relative = record.get("relative")
        if (not isinstance(relative, str)
                or owned_paths._strict_ownership_relative(item["root"], relative) is None):
            return None
        created_directories.append(record)
    path = item["path"]
    owned = owned_paths._bind_owned_path(
        item["root"],
        path,
        expected_file=item["file_identity"],
        expected_root=item["root_identity"],
        created_directories=created_directories,
    )
    if owned is None:
        return None
    companions = []
    artwork = item.get("artwork")
    if artwork is not None:
        artwork_relative = artwork["relative"]
        artwork_created = []
        for record in created_directories:
            directory_relative = owned_paths._strict_ownership_relative(
                item["root"], record["relative"])
            if (
                directory_relative is not None
                and len(directory_relative.parts)
                < len(artwork_relative.parts)
                and artwork_relative.parts[:len(directory_relative.parts)]
                == directory_relative.parts
            ):
                artwork_created.append(record)
        companion = owned_paths._bind_owned_path(
            item["root"],
            artwork["path"],
            expected_file=artwork["file_identity"],
            expected_root=item["root_identity"],
            created_directories=artwork_created,
        )
        if companion is None:
            return None
        companion["kind"] = "artwork"
        companions.append(companion)
    for record in created_files_after_import or ():
        if (
            not isinstance(record, dict)
            or not owned_paths._valid_ownership_identity(record.get("file"))
            or not isinstance(record.get("path"), str)
        ):
            return None
        companion_path = Path(os.path.abspath(record["path"]))
        if companion_path != Path(path).with_suffix(".lrc"):
            return None
        companion = owned_paths._bind_owned_path(
            item["root"],
            companion_path,
            expected_file=record["file"],
            expected_root=item["root_identity"],
            created_directories=created_directories,
        )
        if companion is None:
            return None
        companion["kind"] = "lyrics"
        companions.append(companion)
    if len(companions) > 2:
        return None
    if companions:
        owned["companions"] = companions
    return (owned, path) if owned is not None else None


def _album_dir_for_owned_file(path, landed_dir):
    """Accept only the album scope already verified by the import pipeline."""
    if landed_dir is None:
        return None
    try:
        path = Path(os.path.abspath(os.fspath(path)))
        album_dir = Path(os.path.abspath(os.fspath(landed_dir)))
        relative = path.relative_to(album_dir)
    except (OSError, TypeError, ValueError):
        return None
    return album_dir if relative.parts else None


def _single_download_undo_snapshot(
    queue_item,
    landed_dir,
    *,
    album,
    track,
    artist,
    title,
    track_title,
):
    """Build one exact single-track Undo record from current ownership."""
    owned_path = None
    owned_file_path = None
    owned_binding = _single_owned_path(
        queue_item.get("_import_ownership"),
        landed_dir,
        queue_item.get("_import_ownership_created_directories"),
        queue_item.get("_import_ownership_created_files"),
    )
    if owned_binding is not None:
        owned_path, owned_file_path = owned_binding
        actual_album_dir = _album_dir_for_owned_file(
            owned_file_path, landed_dir
        )
        if actual_album_dir is None:
            owned_path = None
            owned_file_path = None
        else:
            landed_dir = actual_album_dir
    single = {
        "album_id": str(album.get("id") or ""),
        "track_id": str(track.get("id") or ""),
        "dir": str(landed_dir) if landed_dir else "",
        "isrc": track.get("isrc") or "",
        "track_no": track.get("track_number"),
        "disc_no": track.get("media_number") or 1,
        "title": track_title,
        "artist": artist,
        "album": title,
        "marked": False,
    }
    if owned_path is not None:
        single["owned_path"] = owned_path
        single["owned_root"] = str(
            Path(os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
        )
    return single, landed_dir, owned_path, owned_file_path


def _persist_single_download_undo(
    job,
    queue_item,
    *,
    ownership_valid,
    source_single=None,
    destination_single=None,
) -> None:
    """Commit a single-track Undo proof before its relocation can be retired."""
    operation_key = "_post_import_relocation_operation_id"
    if operation_key not in queue_item:
        with job._lock:
            preserve_existing = (
                getattr(job, "_preserve_persisted_single", False) is True
            )
            if ownership_valid and preserve_existing:
                job.__dict__.pop("_preserve_persisted_single", None)
        persisted = job_persistence.persist(job)
        if ownership_valid and persisted is not True:
            with job._lock:
                if preserve_existing:
                    job._preserve_persisted_single = True
            raise RuntimeError(
                "The downloaded track's Undo could not be saved to the data "
                "folder."
            )
        if ownership_valid and persisted is True:
            with job._lock:
                job.__dict__.pop("_preserve_persisted_single", None)
        return

    def restore_source(*, clear_preservation=False) -> None:
        with job._lock:
            if type(source_single) is dict:
                job.single = source_single
            if clear_preservation:
                job.__dict__.pop("_preserve_persisted_single", None)

    def accept_destination(single_snapshot) -> None:
        with job._lock:
            job.single = copy.deepcopy(single_snapshot)
            job.__dict__.pop("_single_undo_unavailable", None)
            job.__dict__.pop("_preserve_persisted_single", None)

    # Engage this before sealing or persisting the destination. Any ordinary
    # job save that was already waiting must preserve the durable source Undo.
    with job._lock:
        job._preserve_persisted_single = True
        single_snapshot = copy.deepcopy(destination_single)

    authority = run_lock.current_lease()
    operation_id = queue_item.get(operation_key)
    album_id = completion.normalise_album_id(job.album_id)
    planned_album = queue_item.get("album")
    planned_album_id = completion.normalise_album_id(
        planned_album.get("id") if isinstance(planned_album, dict) else None
    )
    single_album_id = completion.normalise_album_id(
        single_snapshot.get("album_id")
        if isinstance(single_snapshot, dict)
        else None
    )
    binding_valid = (
        ownership_valid is True
        and album_id == job.album_id
        and planned_album_id == album_id
        and single_album_id == album_id
    )
    if authority is None or not binding_valid:
        queue_recovery._refresh_post_import_relocation_recovery(authority)
        restore_source(clear_preservation=True)
        raise PostImportRelocationAttention(
            "The relocated track could not be bound to its exact Undo record."
        )

    consumer = {
        "kind": "web-single",
        "job_id": job.id,
        "job_created_at": job.created_at,
        "album_id": album_id,
    }
    try:
        handoff_hash = post_import_relocation.seal_post_import_relocation_handoff(
            operation_id,
            consumer=consumer,
            payload=single_snapshot,
            authority=authority,
        )
    except Exception as exc:
        queue_recovery._refresh_post_import_relocation_recovery(authority)
        restore_source(clear_preservation=True)
        raise PostImportRelocationAttention(
            "The relocated track's Undo could not be saved."
        ) from exc

    handoff = {"consumer": consumer, "hash": handoff_hash}
    persistence_error = None
    try:
        persisted = job_persistence.persist_post_import_relocation_handoff(
            job,
            operation_id=operation_id,
            handoff_hash=handoff_hash,
            single=single_snapshot,
        )
    except BaseException as exc:
        persisted = False
        persistence_error = exc
    if persisted is not True:
        # A failed return can still mean SQLite committed before reporting an
        # I/O error.
        queue_item["_post_import_relocation_handoff_unknown"] = True
        try:
            proof_before = (
                job_persistence.post_import_relocation_handoff_persisted(
                    operation_id,
                    handoff,
                )
            )
        except BaseException:
            _log.exception("couldn't verify track Undo before recovery")
            proof_before = None
        recovered_clear = queue_recovery._refresh_post_import_relocation_recovery(authority)
        try:
            proof_after = (
                job_persistence.post_import_relocation_handoff_persisted(
                    operation_id,
                    handoff,
                )
            )
        except BaseException:
            _log.exception("couldn't verify track Undo after recovery")
            proof_after = None
        if proof_before is True or proof_after is True:
            accept_destination(single_snapshot)
            queue_item.pop("_post_import_relocation_handoff_unknown", None)
            queue_item["_post_import_relocation_final_proven"] = True
            if recovered_clear:
                return
        elif proof_before is False or proof_after is False:
            queue_item.pop("_post_import_relocation_handoff_unknown", None)
            restore_source(clear_preservation=True)
        raise PostImportRelocationAttention(
            "The relocated track's Undo could not be saved to the data "
            "folder."
        ) from persistence_error

    accept_destination(single_snapshot)
    queue_item.pop("_post_import_relocation_handoff_unknown", None)
    queue_item["_post_import_relocation_final_proven"] = True

    try:
        post_import_relocation.acknowledge_post_import_relocation(
            operation_id,
            handoff_hash,
            authority=authority,
        )
    except Exception as exc:
        if queue_recovery._refresh_post_import_relocation_recovery(authority):
            return
        raise PostImportRelocationAttention(
            "The relocated track's durable Undo handoff needs recovery."
        ) from exc


_SINGLE_TRACK_FAILURES = {
    "disk_full": "Out of disk space before the track landed. The job log says where.",
    "io_error": (
        "A storage error stopped the track before it landed. The job log has "
        "the error."
    ),
    "incomplete": (
        "The track arrived incomplete and was discarded. Retry fetches it again."
    ),
}


def _make_single_track_run(album, track, token):
    """Run a single-track download: download just ``track`` via the per-track
    queue path (the same isolation repair uses, never a whole-album rip)."""
    expected_generation = api_auth.token_credential_generation(token)

    def run(j):
        storage._require_music_write_target_for_job()
        active = None
        active_token = token
        if getattr(token, "credential_generation", ""):
            active = qobuz_access._authorize_qobuz_live(
                QobuzAccess.DOWNLOAD_ACTION,
                expected_generation=expected_generation,
            )
            active_token = active.token
        args = flows.build_args()
        artist = (album.get("artist") or {}).get("name") or "?"
        title = album.get("title") or "?"
        t_title = track.get("title") or "?"
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        existing, album_dir = catalog.find_existing_tracks(album)
        missing, _present = catalog.compute_missing(qobuz_tracks, existing)
        missing_ids = {str(t.get("id")) for t in missing}
        # Already own this exact track? Don't re-rip it; that just lands a beets
        # ".1.flac" duplicate beside the copy you have, and don't mark anything.
        if str(track.get("id")) not in missing_ids:
            j.summary = f"You already have “{t_title}”. Nothing downloaded."
            return
        qi = queue_builder._build_queue_item(
            album=album, album_dir=album_dir,
            label=f"{artist}, {t_title}  [single]",
            missing=[track], present=existing,
            upgrade_only=False, auto_upgrade=False,
            force_track_by_track=True,
        )
        qi["_capture_import_ownership"] = True
        qi["_defer_post_import_relocation_handoff"] = True
        flows._note_staging_wait(j, "Downloading", 0, 1)
        owned_path = None
        source_single = None
        with job_mgr.staging_lock():
            with qobuz_access._CREDENTIAL_LOCK:
                if (active is not None
                        and not qobuz_access._credential_generation_is_active(
                            active.generation)):
                    raise CredentialChanged(
                        "Qobuz credentials changed before the download began."
                    )
            try:
                queue_executor._execute_download_queue([qi], args, active_token)
                landed_dir = qi.get("_resolved_post_dir") or album_dir
                download_succeeded = (
                    qi.get("n_ok", 0) > 0
                    and qi.get("imported", False)
                    and qi.get("n_fail", 0) == 0
                )
                if download_succeeded:
                    single, landed_dir, owned_path, _owned_file_path = (
                        _single_download_undo_snapshot(
                            qi,
                            landed_dir,
                            album=album,
                            track=track,
                            artist=artist,
                            title=title,
                            track_title=t_title,
                        )
                    )
                    j.single = single
                    _persist_single_download_undo(
                        j,
                        qi,
                        ownership_valid=owned_path is not None,
                    )
                    source_single = copy.deepcopy(single)

                    if (
                        qi.get("_post_import_relocation_pending") is True
                        and owned_path is not None
                    ):

                        authority = run_lock.current_lease()
                        if authority is None:
                            raise RuntimeError(
                                "Automatic filing paused because write "
                                "authority could not be verified."
                            )
                        original_landed_dir = landed_dir
                        ownership_move = {}
                        operation_capture = {}
                        split_ownership_advanced = False
                        try:
                            migrated = queue_executor._reunite_split_album(
                                qi,
                                album_dir,
                                original_landed_dir,
                                authority=authority,
                                await_handoff=True,
                                operation_id_out=operation_capture,
                            )
                            split_ownership_advanced = (
                                "operation_id" in operation_capture
                            )
                            if (
                                not split_ownership_advanced
                                and getattr(args, "migrate_multi_artist", False)
                            ):
                                migrated = catalog.prompt_and_migrate_multi_artist_folder(
                                    album,
                                    args,
                                    ownership_move_out=ownership_move,
                                    operation_id_out=operation_capture,
                                    authority=authority,
                                    source_dir=original_landed_dir,
                                    await_handoff=True,
                                )
                        finally:
                            if "operation_id" in operation_capture:
                                qi["_post_import_relocation_operation_id"] = (
                                    operation_capture["operation_id"]
                                )

                        if "_post_import_relocation_operation_id" in qi:
                            if migrated is None:
                                raise RuntimeError(
                                    "The relocated track's destination was lost."
                                )
                            landed_dir = migrated
                            qi["_resolved_post_dir"] = landed_dir
                            if not split_ownership_advanced:
                                queue_executor._advance_import_ownership_after_relocation(
                                    qi,
                                    ownership_move,
                                )
                            single, landed_dir, owned_path, _owned_file_path = (
                                _single_download_undo_snapshot(
                                    qi,
                                    landed_dir,
                                    album=album,
                                    track=track,
                                    artist=artist,
                                    title=title,
                                    track_title=t_title,
                                )
                            )
                            with j._lock:
                                j._preserve_persisted_single = True
                                j.single = single
                            _persist_single_download_undo(
                                j,
                                qi,
                                ownership_valid=owned_path is not None,
                                source_single=source_single,
                                destination_single=single,
                            )
                        elif (
                            migrated is not None
                            and os.path.abspath(os.fspath(migrated))
                            != os.path.abspath(os.fspath(original_landed_dir))
                        ):
                            queue_recovery._refresh_post_import_relocation_recovery(authority)
                            raise RuntimeError(
                                "The relocated track's recovery record was "
                                "lost."
                            )
                        else:
                            landed_dir = original_landed_dir
                    qi.pop("_post_import_relocation_pending", None)
            except BaseException:
                if (
                    qi.get("_post_import_relocation_pending") is True
                    or "_post_import_relocation_operation_id" in qi
                ):

                    queue_recovery._refresh_post_import_relocation_recovery(
                        run_lock.current_lease()
                    )
                    with j._lock:
                        if (
                            type(source_single) is dict
                            and qi.get(
                                "_post_import_relocation_final_proven"
                            ) is not True
                            and qi.get(
                                "_post_import_relocation_handoff_unknown"
                            ) is not True
                        ):
                            j.single = source_single
                            j.__dict__.pop(
                                "_preserve_persisted_single", None
                            )
                raise
        if not download_succeeded:
            j.status = job_mgr.JobStatus.FAILED
            j.error = _SINGLE_TRACK_FAILURES.get(qi.get("result"))
            if not j.error:
                j.error = (
                    "Downloaded, but the import failed. See job log."
                    if qi.get("n_ok")
                    else "The track was not retrieved. The job log says why."
                )
            return
        j.landed_complete = True
        # The mark keeps Upgrade off a deliberate single whatever the toggle
        # says; the toggle only decides whether scans read it.
        marked = len(missing) > 1
        if marked:
            try:
                hidden_mod.mark_single(artist, title, catalog.album_year(album),
                                       album.get("id"))
                j.summary = (
                    f"Got “{t_title}”, filed under {artist} / {title}. "
                    + ("The rest of the album stays out of scans."
                       if cfg.SUPPRESS_SINGLE_TRACK_GAPS
                       else "Future scans can still offer the rest of the album.")
                )
            except OSError as e:
                # The track landed fine, so don't fail the job, but don't claim
                # the exclusion stuck either.
                marked = False
                j.summary = (f"Got “{t_title}”, filed under {artist} / {title}.")
                if cfg.SUPPRESS_SINGLE_TRACK_GAPS:
                    j.error = redaction.redact(
                        f"{e} The rest of the album may still show in scans.")
                elif not cfg.UPGRADE_SINGLES_ENABLED:
                    j.error = redaction.redact(
                        f"{e} Upgrade may still offer the whole album.")
                else:
                    j.error = redaction.redact(str(e))
        else:
            # This download completed the album, so it's a normal full album
            # now, so clear any single mark an earlier partial download left
            # behind.
            hidden_mod.unmark_single(
                artist,
                title,
                year=catalog.album_year(album),
                album_id=album.get("id"),
            )
            j.summary = (f"Got “{t_title}”; that completed {title}, so it's "
                         "filed as a full album.")
            # Complete means any parked Gap Fill candidate for it is stale.
            flows.prune_library_review_candidates(album)
        with j._lock:
            j.single["marked"] = marked
        if owned_path is None:
            j.summary += (
                " Undo isn't available because the downloaded file "
                "couldn't be verified."
            )
        job_persistence.persist(j)
        flows._refresh_after_local_album_change(
            album,
            {"dir": landed_dir},
            fallback_artist=artist,
            token=active_token,
            args=args,
            upgrade=True,
            downsample=True,
        )
    return run
