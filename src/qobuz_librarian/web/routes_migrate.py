"""Routes for the Migrate page."""
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from qobuz_librarian import config as cfg
from qobuz_librarian.library import migrate as migrate_engine
from qobuz_librarian.web import flows, runtime, scans
from qobuz_librarian.web import jobs as job_mgr

router = APIRouter()


def _migrate_checks(src, dest):
    checks = []
    for label, path in (("Source folder", src), ("Destination folder", dest)):
        if not path:
            checks.append({"label": label, "ok": False, "detail": "not set"})
            continue
        p = Path(path)
        is_dest = label.startswith("Destination")
        if not p.exists():
            # The migration creates the destination tree, so a not-yet-created
            # dest is fine as long as a writable ancestor exists to land it in.
            anc = migrate_engine.existing_ancestor(p) if is_dest else None
            if is_dest and anc and os.access(str(anc), os.W_OK):
                checks.append({"label": label, "ok": True,
                               "detail": f"{p} (will be created under {anc})"})
            elif is_dest:
                checks.append({"label": label, "ok": False,
                               "detail": f"{p} can't be created. Nearest existing "
                                         f"folder {anc or p.anchor} is not writable"})
            else:
                checks.append({"label": label, "ok": False, "detail": f"{p} does not exist"})
        elif not p.is_dir():
            checks.append({"label": label, "ok": False, "detail": f"{p} is not a directory"})
        elif not os.access(str(p), os.R_OK):
            checks.append({"label": label, "ok": False, "detail": f"{p} is not readable"})
        elif is_dest and not os.access(str(p), os.W_OK):
            checks.append({"label": label, "ok": False, "detail": f"{p} is not writable"})
        else:
            checks.append({"label": label, "ok": True, "detail": str(p)})
    return checks


@router.get("/migrate", response_class=HTMLResponse)
async def migrate_page(request: Request):
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    return runtime._tr(request, "migrate.html", {
        # No nav item of its own; it's reached from Settings, so Settings
        # stays lit. The paths surface through migrate_checks, not directly.
        "page": "settings",
        "configured": bool(src and dest),
        "migrate_checks": _migrate_checks(src, dest),
    })


@router.post("/migrate")
async def migrate_scan(request: Request):
    # No credential check: migration only reads and reorganises local files.
    busy = runtime._lock_busy_response(request)
    if busy is not None:
        return busy
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    form = await request.form()
    use_acoustid = form.get("acoustid") == "on"
    in_place = form.get("in_place") == "on"
    if not src or not dest:
        err = ("Set MIGRATE_SRC and MIGRATE_DEST: the source library and "
               "the destination for the organised copy, then try again.")
    else:
        err = migrate_engine.validate_paths(Path(src), Path(dest), in_place=in_place)
    if err:
        return runtime._tr(request, "migrate.html", {
            "page": "settings",
            "configured": bool(src and dest), "error": err,
            "migrate_checks": _migrate_checks(src, dest)})
    job = job_mgr.Job(title="Library migration")
    job.review_verb = "Move" if in_place else "Copy"
    job.execute_kind = "migration"
    # src is persisted so a resume after restart can still prune the emptied
    # source folders on an in-place move (the live execute below gets it too).
    job.execute_args = {"dest": str(dest), "in_place": bool(in_place),
                        "src": str(src), "allow_low_space": False}
    job = await scans._submit_scan_deduped_async(
        job,
        lambda j: flows.scan_migration(j, src, dest, use_acoustid=use_acoustid,
                                       in_place=in_place),
        lambda j, chosen: flows.execute_migration(j, chosen, dest,
                                                  in_place=in_place, src=src,
                                                  allow_low_space=False),
        "migration")
    if job is None:
        return scans._scan_submission_failure_response(request, "/migrate")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
