"""The health checks and staging leftovers that Settings reports."""
import html
import json
import logging
import os
import re
import shutil
import stat
from pathlib import Path

from fastapi import Request

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import beets as beets_mod
from qobuz_librarian.integrations import staging as staging_mod
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library import collection_snapshot
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.web import job_labels, storage, write_gate

_log = logging.getLogger("qobuz_librarian")


def _beets_runtime_diagnostic() -> tuple[str | None, str]:
    """Distinguish an absent launcher from a failed runtime verification."""
    configured = getattr(cfg, "BEETS_PYTHON", "")
    discovered = None if configured else shutil.which("beet")
    try:
        candidate = beets_mod._beets_python_from_launcher()
    except (OSError, TypeError, ValueError):
        candidate = None
    if candidate is None:
        if configured or discovered:
            return (
                None,
                "The Beets launcher could not be resolved to a verifiable "
                "Python executable",
            )
        return (
            None,
            "No Beets launcher was found on PATH and BEETS_PYTHON is unset",
        )
    runtime = beets_mod._checked_beets_runtime(candidate)
    if runtime is None:
        return (
            None,
            "The configured Beets launcher could not be verified as an "
            "executable Python runtime",
        )
    if beets_mod._configured_beets_plugins(runtime) is None:
        return (
            None,
            "Could not verify a Beets 2.14.1 runtime and readable "
            f"configuration using {runtime.python}",
        )
    return runtime.python, runtime.python


# What each kind of kept staging group is, in the user's terms. The keys are
# the manifest kinds written by the download, import and recovery paths.
_STAGING_LEFTOVER_KINDS = {
    "rejected": (
        "Lossy track set aside",
        "Qobuz served this track in a lossy format, so it was kept out of "
        "your library. It stays here in case a later attempt finds the "
        "lossless version.",
    ),
    "legacy-rejected": (
        "Lossy track set aside",
        "Qobuz served this track in a lossy format, so it was kept out of "
        "your library. It stays here in case a later attempt finds the "
        "lossless version.",
    ),
    "untagged": (
        "Untagged file set aside",
        "This file arrived without an album or artist tag, so it could not "
        "be filed.",
    ),
    "unimported": (
        "File that never imported",
        "The download finished but this file was not filed into the "
        "library.",
    ),
    "unresolved": (
        "File set aside",
        "Kept out of the library because it could not be filed.",
    ),
    "beets": (
        "Album waiting to be filed",
        "The tracks are downloaded; filing them failed. The next download "
        "run tries again on its own, with no re-download.",
    ),
    "interrupted": (
        "Files from a stopped download",
        "A download stopped part way and its files were kept so nothing "
        "already fetched is thrown away.",
    ),
}


def _album_name_from_path(path):
    """An album folder read back as a name: artist, then album."""
    parts = [part for part in Path(str(path)).parts if part not in ("/", "")]
    if len(parts) >= 2:
        return f"{parts[-2]} · {parts[-1]}"
    return parts[-1] if parts else ""


def _staging_display_name(path):
    """A staging path as something worth reading: artist, album and file, with
    the app's own private run folders left out of it."""
    try:
        relative = Path(str(path)).relative_to(Path(str(cfg.STAGING_DIR)))
    except ValueError:
        relative = Path(str(path))
    return " / ".join(
        part for part in relative.parts if not part.startswith("."))


def _staging_tree_contents(tree):
    """The deepest album folders one retained tree holds."""
    relatives = [rel for rel, _identity in tree.directories if rel]
    leaves = sorted(
        rel for rel in relatives
        if not any(other != rel and other.startswith(rel + "/")
                   for other in relatives)
    )
    if leaves:
        return ", ".join(leaf.replace("/", " / ") for leaf in leaves)
    return plural(len(tree.files), "file")


def _staging_leftovers():
    """Every group the app is holding in staging, as user-facing rows.

    Downloads warn that files are being kept, and until this there was
    nowhere to see what they are or get rid of them, so they accumulated
    unseen. Read-only: removing one is always a deliberate click.
    """
    leftovers = []
    for inspection in staging_mod.inspect_retry_groups():
        kind = inspection.kind or "unresolved"
        label, reason = _STAGING_LEFTOVER_KINDS.get(
            kind, _STAGING_LEFTOVER_KINDS["unresolved"])
        what = ""
        held = inspection.file_group or inspection.planned_file
        if held is not None:
            source = held.original or held.retained
            what = _staging_display_name(source)
        elif inspection.planned_trees and not any(
                tree.files or any(rel for rel, _identity in tree.directories)
                for tree in inspection.planned_trees):
            label = "Empty folder from a stopped download"
            reason = "The download stopped before any file arrived."
        elif inspection.planned_trees:
            what = ", ".join(
                _staging_tree_contents(tree)
                for tree in inspection.planned_trees
            )
        if inspection.status == "ready" and inspection.owner is None:
            removable, note = True, ""
        elif inspection.owner is not None:
            removable = False
            note = ("It is tied to a download that never finished, so the app "
                    "will not clear it on its own.")
        elif inspection.status == "malformed":
            removable = False
            note = ("The app's record of what it holds can't be read, so it "
                    "can't tell what is in there.")
        elif inspection.status == "incomplete":
            removable = False
            note = "Some of the files it recorded are already gone."
        else:
            removable = False
            note = ("Its contents changed since it was set aside, so the app "
                    "can't tell whether these are still the files it kept.")
        leftovers.append({
            "name": inspection.path.name,
            "label": f"{label}: {what}" if what else label,
            "reason": reason,
            "removable": removable,
            "note": note,
            "path": str(inspection.path),
        })
    return leftovers


def _diagnostics():
    """Read-only health checks surfaced on the Settings page."""
    checks = []

    def _tree_size(path) -> int:
        """Best-effort total bytes of regular files under a directory tree.

        Skips whatever it can't stat rather than giving up on the whole
        total: this feeds a rough disk-usage line, not a byte-exact figure.
        """
        total = 0
        try:
            for parent, _dirs, names in os.walk(path):
                for name in names:
                    try:
                        value = os.stat(os.path.join(parent, name),
                                        follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(value.st_mode):
                        total += int(value.st_size)
        except OSError:
            pass
        return total

    def _dir_check(label, path, *, want_writable, skip_names=(),
                   unit=("entry", "entries"), show_size=False):
        # The Library paths section above already resolves the same folders to
        # the host path Docker exposes them at; naming this one by its
        # container path made the two sections disagree about what to call
        # the same folder.
        p = Path(path)
        display, _is_host = storage._resolve_host_path(str(p))
        if not p.exists():
            hint = " (volume not mounted?)" if cfg.in_container() else ""
            checks.append({"label": label, "ok": False,
                           "detail": f"{display} does not exist{hint}"})
            return
        if not p.is_dir():
            checks.append({"label": label, "ok": False,
                           "detail": f"{display} exists but is not a directory"})
            return
        if want_writable and not os.access(p, os.W_OK):
            checks.append({"label": label, "ok": False,
                           "detail": f"{display} is not writable by the container user. "
                           "On a NAS, set PUID/PGID in .env to your media-share owner"})
            return
        try:
            n = sum(1 for entry in p.iterdir() if entry.name not in skip_names)
        except OSError as e:
            checks.append({"label": label, "ok": False,
                           "detail": f"{display} unreadable: {e}"})
            return
        size = f" · {format_size(_tree_size(p))}" if show_size else ""
        checks.append({"label": label, "ok": True, "mono": True,
                       "detail": f"{display}: {plural(n, *unit)}{size}"})

    # The panel exists to say what stops a scan or a download before one is
    # started, and a pause is the most direct reason there is. It was the one
    # thing missing: every row could read OK while nothing could run at all.
    paused = write_gate._writes_paused_notice()
    if paused is not None:
        # The banner at the top of this same page already carries the whole
        # sentence; the row names the cause so the panel reads as a checklist.
        checks.append({"label": "Writes paused", "ok": False,
                       "detail": paused["reason"]})
    else:
        checks.append({"label": "Writes on", "ok": True,
                       "detail": "Not paused"})

    music_state, recorded_albums = collection_snapshot.music_root_write_state()
    if music_state == "ready":
        _dir_check("Music library", cfg.MUSIC_ROOT, want_writable=True)
    else:
        checks.append({
            "label": "Music library",
            "ok": False,
            "detail": storage._music_write_target_message(
                music_state,
                recorded_albums,
                diagnostic=True,
            ),
        })
    # The app's own recovery folder is not a staging entry; counting it made a
    # pile of kept files read as one healthy item. It gets its own check below,
    # so this row says what it counted rather than "0 entries", which read as
    # an empty staging folder next to a warning about the files kept in it.
    _dir_check("Staging area", cfg.STAGING_DIR, want_writable=True,
               skip_names={cfg.BEETS_RETRY_DIR}, show_size=True,
               unit=("album waiting to import", "albums waiting to import"))
    _dir_check("Data folder", cfg.DATA_DIR, want_writable=True)
    # A single file left owned by another user, as a command run as root or a
    # PUID change leaves behind, passes the folder check above.
    try:
        locked = sorted(
            entry.name for entry in Path(cfg.DATA_DIR).iterdir()
            if not os.access(entry, os.R_OK | os.W_OK
                             | (os.X_OK if entry.is_dir() else 0)))
    except OSError:
        locked = []
    if locked:
        shown = ", ".join(locked[:5]) + (
            f" and {len(locked) - 5} more" if len(locked) > 5 else "")
        checks.append({"label": "Data folder files", "ok": False,
                       "detail": f"Not readable and writable by the container "
                       f"user: {shown}. Set their owner to PUID/PGID"})
    # A whole-directory count and size, the same shape as the Staging area
    # row above. The rows further down (Unfinished upgrade backups, Backups
    # needing review) each cover one problem subset; this is the total the
    # folder is actually holding.
    _dir_check("Upgrade backups", cfg.UPGRADE_BACKUP_DIR, want_writable=True,
               show_size=True, unit=("kept backup", "kept backups"))

    beets_db = Path(cfg.BEETS_DB_PATH)
    beets_db_display, _is_host = storage._resolve_host_path(str(beets_db))
    if beets_db.exists():
        ok = os.access(beets_db, os.R_OK)
        checks.append({"label": "Beets database", "ok": ok,
                       "detail": f"{beets_db_display}" if ok
                       else f"{beets_db_display} exists but is not readable"})
    elif beets_db.parent.exists():
        checks.append({"label": "Beets database", "ok": True,
                       "detail": f"{beets_db_display} (created on first import)"})
    else:
        parent_display, _is_host = storage._resolve_host_path(str(beets_db.parent))
        checks.append({"label": "Beets database", "ok": False,
                       "detail": f"{parent_display} does not exist"})

    missing_tool_fix = ("Pull the image again (docker compose pull)."
                        if cfg.in_container()
                        else "See Quick start in the README.")
    for binary in ("rip", "ffmpeg", "flac"):
        found = shutil.which(binary)
        checks.append({"label": f"{binary} binary",
                       "ok": bool(found),
                       "detail": found or f"{binary} was not found. "
                       f"{missing_tool_fix}"})
    beets_python, beets_detail = _beets_runtime_diagnostic()
    checks.append({
        "label": "Beets",
        "ok": beets_python is not None,
        "detail": beets_detail,
    })

    stranded = []
    stranded_error = False
    if cfg.UPGRADE_BACKUP_DIR.exists():
        try:
            for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
                if entry.is_dir() and (entry.suffix == ".partial"
                                       or entry.name == ".restore_trash"):
                    stranded.append(entry)
        except OSError as exc:
            stranded_error = True
            _log.warning(
                "couldn't inspect stranded upgrade backups: %s", exc)
    if stranded_error:
        checks.append({
            "label": "Unfinished upgrade backups",
            "ok": False,
            "detail": "Could not inspect this folder; its status is unknown.",
        })
    elif stranded:
        checks.append({"label": "Unfinished upgrade backups", "ok": False,
                       "detail": f"{len(stranded)} found in "
                                 f"{cfg.UPGRADE_BACKUP_DIR}; manual cleanup needed"})
    else:
        checks.append({"label": "Unfinished upgrade backups", "ok": True,
                       "detail": "none"})
    backup_dir = collection_snapshot.snapshot_dir()
    if cfg.collection_backup_dir_in_music(backup_dir):
        checks.append({
            "label": "Backup folder",
            "ok": False,
            "detail": f"{storage._resolve_host_path(str(backup_dir))[0]} is inside "
                      "the music folder, so a music disk that fails takes "
                      "the backups with it.",
        })

    inventory = {"orphans": [], "undo": [], "leftovers": []}
    try:
        # An upgrade's backup inside its retention window is expected, not a
        # fault; the age sweep settles it.
        inventory["orphans"] = [
            item for item in backup_mod.list_retained_backups()
            if not backup_mod.awaiting_retention(item[0])
        ]
    except Exception as exc:
        _log.warning(
            "couldn't inspect kept recovery backups: %s", exc)
        checks.append({
            "label": "Backups needing review",
            "ok": False,
            "detail": "Could not inspect kept backups; their status is unknown.",
        })
    orphans = inventory["orphans"]
    interrupted_disposals = [
        item for item in orphans
        if item[0].name.startswith(".ql-dispose-backup-")
    ]
    orphans = [
        item for item in orphans
        if not item[0].name.startswith(".ql-dispose-backup-")
        and not item[2].removable
    ]
    if interrupted_disposals:
        checks.append({
            "label": "Interrupted backup cleanup",
            "ok": False,
            "detail": f"{plural(len(interrupted_disposals), 'backup')} kept "
                      "recovery data; review the location shown below before "
                      "removing anything.",
        })
    if orphans:
        checks.append({"label": "Backups needing review", "ok": False,
                       "detail": f"{plural(len(orphans), 'backup')} "
                                 f"{'was' if len(orphans) == 1 else 'were'} "
                                 "kept. Restore or remove "
                                 f"{'it' if len(orphans) == 1 else 'them'} "
                                 "below."})
    elif not any(d["label"] == "Backups needing review" for d in checks):
        checks.append({"label": "Backups needing review", "ok": True,
                       "detail": "none"})

    # Counted separately from the backups above, which are a fault. These are
    # the undo copies a downsample was asked to keep, and leaving them out of
    # every count made the restore rows below look like unexplained extras.
    try:
        inventory["undo"] = backup_mod.list_undo_copies()
    except Exception as exc:
        _log.warning(
            "couldn't inspect retained hi-res originals: %s", exc)
        checks.append({
            "label": "Hi-res originals kept",
            "ok": False,
            "detail": "Could not inspect retained originals; their status is unknown.",
        })
    undo_copies = inventory["undo"]
    if undo_copies:
        checks.append({
            "label": "Hi-res originals kept",
            "ok": True,
            "detail": f"{plural(len(undo_copies), 'downsampled album')} can be "
                      f"put back, listed below. Cleared automatically after "
                      f"{plural(cfg.UPGRADE_BACKUP_RETENTION_DAYS, 'day')}.",
        })

    try:
        inventory["leftovers"] = _staging_leftovers()
    except Exception as exc:
        _log.warning(
            "couldn't inspect files kept in staging: %s", exc)
        checks.append({
            "label": "Files kept in staging",
            "ok": False,
            "detail": "Could not inspect kept staging files; their status is unknown.",
        })
    leftovers = inventory["leftovers"]
    if leftovers:
        checks.append({
            "label": "Files kept in staging",
            "ok": False,
            "detail": f"{plural(len(leftovers), 'item')} "
                      f"{'is' if len(leftovers) == 1 else 'are'} being held "
                      "outside your library. Review them below.",
        })
    elif not any(d["label"] == "Files kept in staging" for d in checks):
        checks.append({"label": "Files kept in staging", "ok": True,
                       "detail": "none"})
    return {"checks": checks, **inventory}


def _diagnostics_fragment(request: Request, diagnostics=None) -> str:
    """The diagnostics list items, plus a row per retained backup.

    Shared by the Settings page render, the Recheck partial, and the restore
    POST, which re-renders the list in place so a restored backup disappears
    from it without a page reload."""
    if not isinstance(diagnostics, dict):
        report = _diagnostics()
        if diagnostics is not None:
            report["checks"] = diagnostics
    else:
        report = diagnostics
    checks = report["checks"]
    rows = []
    for d in checks:
        icon = "OK" if d["ok"] else "!"
        cls = "ql-diagnostic-status-ok" if d["ok"] else "ql-diagnostic-status-error"
        aria = "OK" if d["ok"] else "Needs attention"
        # A path-and-count line reads fine in the small monospace the rest of
        # the checks share; the sentences explaining an actual problem don't.
        detail_cls = "ql-diagnostic-detail ql-diagnostic-detail-mono" if d.get("mono") else "ql-diagnostic-detail"
        detail = f'<div class="{detail_cls}">{html.escape(d.get("detail") or "")}</div>' if d.get("detail") else ""
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status {cls}" aria-label="{aria}">{icon}</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">{html.escape(d["label"])}</div>{detail}</div>'
            f'</div>'
        )
    orphans = report["orphans"]
    tok = html.escape(request.state.csrf_token)
    for path, origin, classification in orphans:
        name = html.escape(path.name)
        if origin:
            dest_display, _is_host = storage._resolve_host_path(str(origin))
            dest = html.escape(dest_display)
            album = html.escape(_album_name_from_path(origin))
        else:
            dest = "its album folder"
            album = ""
        if path.name.startswith(".ql-dispose-backup-"):
            held = path / "held"
            try:
                location = (
                    held
                    if stat.S_ISDIR(
                        held.stat(follow_symlinks=False).st_mode)
                    else path
                )
            except OSError:
                location = path
            display_location, _is_host_path = storage._resolve_host_path(location)
            detail = (
                f"Recovery files for {dest} were kept at "
                f"{html.escape(display_location)}. Review them before removing "
                "anything."
            )
            rows.append(
                f'<div class="ql-diagnostic-row">'
                f'<span class="ql-diagnostic-status '
                f'ql-diagnostic-status-error" '
                f'aria-label="Needs attention">!</span>'
                f'<div class="min-w-0"><div '
                f'class="ql-diagnostic-label">Interrupted backup cleanup'
                f'</div><div class="ql-diagnostic-detail">{detail}</div>'
                f'</div></div>'
            )
            continue
        if backup_mod.is_set_aside_replacement(path):
            # Restore would put this download over the album's original.
            rows.append(
                f'<div class="ql-diagnostic-row" data-backup-status="set-aside">'
                f'<span class="ql-diagnostic-status ql-diagnostic-status-error" '
                f'aria-label="Needs attention">!</span>'
                f'<div class="min-w-0"><div class="ql-diagnostic-label">'
                f'Download set aside{f": {album}" if album else ""}</div>'
                f'<div class="ql-diagnostic-detail">An upgrade couldn\'t '
                f'verify this download, so the original album was put back in '
                f'{dest}.</div>'
                f'<form hx-post="/backups/discard-unchecked" '
                f'hx-target="#diagnostics-list" class="mt-2" data-busy-submit>'
                f'<input type="hidden" name="_csrf_token" value="{tok}">'
                f'<input type="hidden" name="backup" value="{name}">'
                f'<button type="submit" class="ql-btn ql-btn-sm" '
                f'data-confirm="Delete this download? The album keeps its '
                f'original files." data-confirm-action="Delete" '
                f'data-irreversible>Delete</button>'
                f'</form></div></div>'
            )
            continue
        reason = html.escape(classification.detail)
        status = "ok" if classification.removable else "error"
        icon = "OK" if classification.removable else "!"
        aria = "OK" if classification.removable else "Needs attention"
        where = dest if origin else "the album folder it came from"
        if re.match(r"^\d{8}_\d{6}(?:_\d{6})?_(?:gapfill|downsample)_", path.name):
            # These restore file by file and the backup wins each swap.
            restore_confirm = (
                f"Put these files back in {where}? Any file of the same name "
                "there is replaced and cannot be brought back.")
        else:
            restore_confirm = (
                f"Put this album back in {where}? Restore goes ahead only "
                "while that folder holds less than the backup, and replaces "
                "what it holds.")
        # Remove proves every file back first, so it can only succeed where
        # the listing could not finish its own check.
        remove_form = (
            f'<form hx-post="/backups/discard" hx-target="#diagnostics-list" data-busy-submit>'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Remove this backup? It is deleted only after '
            f'every file it holds is verified byte-for-byte back in {where}." '
            f'data-confirm-action="Remove" data-irreversible>Remove</button>'
            f'</form>'
        ) if classification.status != "retained" else ""
        rows.append(
            f'<div class="ql-diagnostic-row" data-backup-status="{classification.status}">'
            f'<span class="ql-diagnostic-status ql-diagnostic-status-{status}" aria-label="{aria}">{icon}</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">'
            f'Backup{f": {album}" if album else ""}</div>'
            f'<div class="ql-diagnostic-detail">{reason}</div>'
            f'<div class="mt-2 flex gap-2">'
            f'<form hx-post="/backups/restore" hx-target="#diagnostics-list" data-busy-submit>'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="{restore_confirm}" '
            f'data-confirm-action="Restore" data-irreversible>Restore</button>'
            f'</form>'
            f'{remove_form}'
            f'</div></div></div>'
        )
    undo = report["undo"]
    for path, origin in undo:
        name = html.escape(path.name)
        if origin:
            dest_display, _is_host = storage._resolve_host_path(str(origin))
            dest = html.escape(dest_display)
            album = html.escape(_album_name_from_path(origin))
        else:
            dest = "its album folder"
            album = ""
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status ql-diagnostic-status-ok" aria-label="OK">OK</span>'
            f'<div class="min-w-0"><div class="ql-diagnostic-label">'
            f'Hi-res originals kept{f": {album}" if album else ""}</div>'
            f'<div class="ql-diagnostic-detail">Copies of the files this album '
            f'had before it was downsampled, so the rewrite can be undone; '
            f'cleared automatically after '
            f'{plural(cfg.UPGRADE_BACKUP_RETENTION_DAYS, "day")}.</div>'
            f'<div class="mt-2 flex gap-2">'
            f'<form hx-post="/backups/restore" hx-target="#diagnostics-list" data-busy-submit>'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Put the hi-res originals of '
            f'{album or dest} back? This undoes the downsample." '
            f'data-confirm-action="Restore">Restore</button>'
            f'</form>'
            f'<form hx-post="/backups/release-originals" '
            f'hx-target="#diagnostics-list" data-busy-submit>'
            f'<input type="hidden" name="_csrf_token" value="{tok}">'
            f'<input type="hidden" name="backup" value="{name}">'
            f'<button type="submit" class="ql-btn ql-btn-sm" '
            f'data-confirm="Delete the hi-res originals of '
            f'{album or dest}? They are the only copies left at the original '
            f'quality, the album keeps its downsampled files, and the '
            f'downsample can no longer be undone." '
            f'data-confirm-action="Delete originals" data-irreversible>'
            f'Delete originals</button>'
            f'</form>'
            f'</div></div></div>'
        )
    leftovers = report["leftovers"]
    for leftover in leftovers:
        detail = html.escape(leftover["reason"])
        if leftover["note"]:
            detail += " " + html.escape(leftover["note"])
        name = html.escape(leftover["name"])
        if leftover["removable"]:
            action = (
                f'<form hx-post="/staging/discard" '
                f'hx-target="#diagnostics-list" class="mt-2" data-busy-submit>'
                f'<input type="hidden" name="_csrf_token" value="{tok}">'
                f'<input type="hidden" name="group" value="{name}">'
                f'<button type="submit" class="ql-btn ql-btn-sm" '
                f'data-confirm="Delete these files? They are not in your '
                f'library and this cannot be undone." '
                f'data-confirm-action="Remove" data-irreversible>Remove</button>'
                f'</form>'
            )
        else:
            # Nothing automatic will ever clear this row, so it needs the one
            # place to look and a way to end it from here.
            where, _is_host = storage._resolve_host_path(leftover["path"])
            where = html.escape(where)
            detail += f" Its folder is {where}."
            action = (
                f'<form hx-post="/staging/discard-unchecked" '
                f'hx-target="#diagnostics-list" class="mt-2" data-busy-submit>'
                f'<input type="hidden" name="_csrf_token" value="{tok}">'
                f'<input type="hidden" name="group" value="{name}">'
                f'<button type="submit" class="ql-btn ql-btn-sm" '
                f'data-confirm="Delete the files at {where} without checking '
                f'them? The app cannot confirm what they are, they are not in '
                f'your library, and this cannot be undone." '
                f'data-confirm-action="Delete anyway" '
                f'data-irreversible>Delete anyway</button>'
                f'</form>'
            )
        rows.append(
            f'<div class="ql-diagnostic-row">'
            f'<span class="ql-diagnostic-status ql-diagnostic-status-error" '
            f'aria-label="Needs attention">!</span>'
            f'<div class="min-w-0">'
            f'<div class="ql-diagnostic-label">'
            f'{html.escape(leftover["label"])}</div>'
            f'<div class="ql-diagnostic-detail">{detail}</div>'
            f'{action}</div></div>'
        )
    return "\n".join(rows)


def _collection_backup_status():
    """What the Settings page shows about the collection snapshot."""
    state, latest = collection_snapshot.latest_status()
    info = {
        "folder": str(collection_snapshot.snapshot_dir()),
        "age": None,
        "counts": None,
        "held_back": None,
        "held_back_unreadable": False,
        "unreadable": state == "unreadable",
        "failed": None,
    }
    failure = collection_snapshot.last_failure()
    if failure is not None:
        info["failed"] = {"age": job_labels._format_age(failure[0]), "error": failure[1]}
    if isinstance(latest, dict):
        info["counts"] = latest.get("counts")
        stamp = latest.get("updated_at_epoch")
        if isinstance(stamp, (int, float)):
            info["age"] = job_labels._format_age(float(stamp))
    suspect = collection_snapshot.suspect_path()
    try:
        held = json.loads(suspect.read_text(encoding="utf-8"))
    except FileNotFoundError:
        held = None
    except (OSError, RecursionError, ValueError):
        held = None
        info["held_back_unreadable"] = True
    valid, _reason = collection_snapshot.validate_upload(
        held, allow_empty=True
    )
    if valid:
        info["held_back"] = held.get("counts")
    elif held is not None:
        info["held_back_unreadable"] = True
    return info
