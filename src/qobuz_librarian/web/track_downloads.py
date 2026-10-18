"""Single-track downloads and the Undo record each one leaves."""
import os
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import download_result, redaction
from qobuz_librarian.api import auth as api_auth
from qobuz_librarian.api.auth import CredentialChanged
from qobuz_librarian.library import catalog, discovery
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.scanner import list_artist_album_dirs
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.queue import builder as queue_builder
from qobuz_librarian.queue import executor as queue_executor
from qobuz_librarian.ui_cli.logging import vlog
from qobuz_librarian.web import (
    flows,
    job_persistence,
    owned_paths,
    qobuz_access,
    storage,
)
from qobuz_librarian.web import jobs as job_mgr


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


def _single_owned_path(payload, created_files_after_import=None):
    """Bind only the exact destination reported by the one-run beets hook."""
    item = _single_ownership_item(payload)
    if item is None:
        return None
    created_directories = item["created_directories"]
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
    owned_binding = _single_owned_path(
        queue_item.get("_import_ownership"),
        queue_item.get("_import_ownership_created_files"),
    )
    if owned_binding is not None:
        owned_path, owned_file_path = owned_binding
        actual_album_dir = _album_dir_for_owned_file(
            owned_file_path, landed_dir
        )
        if actual_album_dir is None:
            owned_path = None
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
    return single, landed_dir, owned_path


def _persist_single_download_undo(job, *, ownership_valid) -> None:
    """Save the job, failing it when its exact Undo record was not saved."""
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


def _folder_label(landed_dir):
    try:
        relative = Path(landed_dir).relative_to(
            os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    except (TypeError, ValueError):
        return None
    return " / ".join(relative.parts) or None


def _rest_of_album_note(landed_dir, token):
    """What Library scans make of the rest of an album a track download
    left part-owned, or "" when that can't be told."""
    if cfg.SUPPRESS_SINGLE_TRACK_GAPS:
        return "The rest of the album stays out of scans."
    try:
        relative = Path(landed_dir).relative_to(
            os.path.abspath(os.fspath(cfg.MUSIC_ROOT)))
    except (TypeError, ValueError):
        return ""
    if len(relative.parts) < 2:
        return ""
    folder = Path(cfg.MUSIC_ROOT) / relative.parts[0]
    if normalize(folder.name) in VA_NORMALIZED:
        return "Scans skip Various Artists folders."
    try:
        artist_id = discovery.resolve_folder_artist(
            folder.name, token, list_artist_album_dirs(folder))[0]
    except Exception as exc:
        vlog(f"couldn't tell whether scans read {folder.name!r}: {exc}")
        return ""
    if not artist_id:
        return (f"Qobuz has no artist for “{folder.name}”, so scans skip "
                "that folder.")
    return "Future scans can still offer the rest of the album."


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
            active = qobuz_access._authorize_download_start(
                expected_generation=expected_generation,
            )
            active_token = active.token
        args = flows.build_args()
        artist = (album.get("artist") or {}).get("name") or "?"
        title = album.get("title") or "?"
        t_title = track.get("title") or "?"
        existing, album_dir, missing, present = catalog.find_edition_tracks(
            album, track_id=track.get("id"))
        missing_ids = {str(t.get("id")) for t in missing}
        # Already own this exact track? Don't re-rip it; that just lands a beets
        # ".1.flac" duplicate beside the copy you have, and don't mark anything.
        if str(track.get("id")) not in missing_ids:
            j.summary = f"You already have “{t_title}”. Nothing downloaded."
            return
        if album_dir is None:
            existing, album_dir = catalog.find_album_folder(album)
            if album_dir is not None and catalog.names_other_version(album, album_dir):
                existing, album_dir = [], None
        qi = queue_builder._build_queue_item(
            album=album, album_dir=album_dir,
            label=f"{artist}, {t_title}  [single]",
            missing=[track], present=existing,
            upgrade_only=False, auto_upgrade=False,
            force_track_by_track=True,
            exact_edition=True,
        )
        qi["_capture_import_ownership"] = True
        flows._note_staging_wait(j, "Downloading", 0, 1)
        owned_path = None
        with job_mgr.staging_lock_unless_cancelled(j):
            with qobuz_access._CREDENTIAL_LOCK:
                if (active is not None
                        and not qobuz_access._credential_generation_is_active(
                            active.generation)):
                    raise CredentialChanged(
                        "Qobuz credentials changed before the download began."
                    )
            queue_executor._execute_download_queue([qi], args, active_token)
            landed_dir = qi.get("_resolved_post_dir") or album_dir
            download_succeeded = (
                qi.get("n_ok", 0) > 0
                and qi.get("imported", False)
                and qi.get("n_fail", 0) == 0
            )
            if download_succeeded:
                single, landed_dir, owned_path = _single_download_undo_snapshot(
                    qi,
                    landed_dir,
                    album=album,
                    track=track,
                    artist=artist,
                    title=title,
                    track_title=t_title,
                )
                j.single = single
                _persist_single_download_undo(
                    j, ownership_valid=owned_path is not None)
        if not download_succeeded:
            j.status = job_mgr.JobStatus.FAILED
            _, _, lossy_only, unavailable = download_result.track_shortfall(qi)
            if (lossy_only or unavailable) and qi.get("result") not in (
                    "disk_full", "io_error"):
                j.error = (
                    "Qobuz only has this track in a lossy format, so it was "
                    "not added." if lossy_only else
                    "Qobuz doesn't stream this track, so it can't be "
                    "downloaded.")
                j.execute_args = {**(j.execute_args or {}),
                                  "retry_disabled": "lossy"}
                return
            broken = qi.get("n_broken") and not qi.get("n_fail")
            j.error = _SINGLE_TRACK_FAILURES.get(qi.get("result"))
            if not j.error or broken and qi.get("result") == "incomplete":
                j.error = (
                    "Downloaded, but the import failed. See job log."
                    if qi.get("n_ok")
                    else "Qobuz sent a broken file at this quality, so the "
                    "track was not added."
                    if broken
                    else "The track was not retrieved. The job log says why."
                )
            return
        j.landed_complete = True
        folder = _folder_label(landed_dir)
        got = f"Got “{t_title}”" + (f", filed under {folder}." if folder else ".")
        # The mark keeps Upgrade off a deliberate single whatever the toggle
        # says; the toggle only decides whether scans read it.
        rest = [t for t in missing
                if str(t.get("id")) != str(track.get("id"))
                and not catalog.unstreamable(t)]
        marked = len(rest) > len(
            catalog.bonus_versions(rest, [*present, track]))
        if marked:
            try:
                hidden_mod.mark_single(artist, title, catalog.album_year(album),
                                       album.get("id"))
                upgrade = (
                    "Upgrade treats the album as a single."
                    if not cfg.SUPPRESS_SINGLE_TRACK_GAPS
                    and not cfg.UPGRADE_SINGLES_ENABLED else "")
                j.summary = " ".join(filter(None, (
                    got, upgrade,
                    _rest_of_album_note(landed_dir, active_token))))
            except OSError as e:
                # The track landed fine, so don't fail the job, but don't claim
                # the exclusion stuck either.
                marked = False
                j.summary = got
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
        )
        flows._close_completed_job(j)
    return run
