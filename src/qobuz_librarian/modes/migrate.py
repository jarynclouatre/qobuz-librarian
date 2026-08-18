"""CLI flow for the one-time library-migration tool.

Wires ``library.migrate`` (the planning/copy engine) to the terminal: resolve
and validate the source and destination, build the plan, show a real preview,
confirm, then copy. Local-only: it reorganises files on disk and never needs a
Qobuz login.
"""
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.library import migrate as engine
from qobuz_librarian.library.scanner import HAVE_MUTAGEN
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, fmt, format_size, section, truncate
from qobuz_librarian.ui_cli.errors import EXIT_CONFIG, EXIT_GENERAL
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import confirm


def _prompt_path(msg: str) -> str:
    return ask(msg, lower=False) or ""


def _resolve_paths(args):
    """Source and destination from flags, then env, then an interactive prompt.

    Returns (src, dest) as validated Paths, or (None, None) after explaining
    what's wrong."""
    src = (getattr(args, "migrate_src", "") or config.MIGRATE_SRC).strip()
    dest = (getattr(args, "migrate_dest", "") or config.MIGRATE_DEST).strip()
    if not src:
        src = _prompt_path("  Source library to organise: ")
    if not dest:
        dest = _prompt_path("  Destination for the organised copy: ")
    if not src or not dest:
        log.warning(fmt(C.RED,
            "  ✗  Need both a source and a destination.\n"
            "     Set MIGRATE_SRC and MIGRATE_DEST, or pass "
            "--migrate-src / --migrate-dest."))
        return None, None

    src, dest = Path(src), Path(dest)
    in_place = bool(getattr(args, "in_place", False))
    err = engine.validate_paths(src, dest, in_place=in_place)
    if err:
        log.warning(fmt(C.RED, f"  ✗  {err}"))
        return None, None
    return src, dest


def _progress_printer():
    """Heartbeat progress: a per-file line on a 47k-file library would bury
    everything else, so log on each phase change and every 500 files."""
    state = {"phase": ""}

    def report(phase, current, total, item):
        if phase != state["phase"]:
            state["phase"] = phase
            log.info(fmt(C.GRAY, f"  {phase}…"))
        if total and (current == total or current % 500 == 0):
            log.info(fmt(C.GRAY, f"    {current}/{total}"))

    return report


def _artist_count(entries) -> int:
    return len({e.dest_rel.parts[0] for e in entries if e.dest_rel})


def _print_preview(plan, verbose: bool, in_place: bool,
                   resume_entries) -> None:
    resume_entries = list(resume_entries)
    s = plan.summary()
    log.info("")
    log.info(fmt(C.BOLD + C.CYAN, "  Preview"))
    if s["place"]:
        log.info(fmt(C.WHITE,
            f"    {s['place']} file(s) to place across "
            f"{_artist_count(plan.placed)} artist(s)"))
    if resume_entries:
        log.info(fmt(C.WHITE,
            f"    {len(resume_entries)} existing file(s) verified; "
            "cover art and sidecars can be resumed"))
    if s["unplaceable"]:
        log.info(fmt(C.YELLOW,
            f"    {s['unplaceable']} couldn't be identified; left where they are"))
    unsafe_collisions = s["collision"] - len(resume_entries)
    if unsafe_collisions:
        log.info(fmt(C.YELLOW,
            f"    {unsafe_collisions} skipped to avoid a name collision"))
    need, free = engine.space_estimate(
        plan, in_place=in_place, resume_entries=resume_entries)
    if need:
        verb = "move" if in_place else "copy"
        if free is None:
            log.info(fmt(C.GRAY, f"    ≈{format_size(need)} to {verb}"))
        else:
            log.info(fmt(C.GRAY,
                f"    ≈{format_size(need)} to {verb} · "
                f"{format_size(free)} free at the destination"))
            if need > free:
                log.info(fmt(C.BOLD + C.RED,
                    f"    ⚠  Not enough free space: needs about {format_size(need)} "
                    f"but only {format_size(free)} is free. Free up space or pick "
                    "another destination first."))
    if verbose:
        for e in plan.unplaceable[:50]:
            log.info(fmt(C.GRAY, f"      ? {truncate(str(e.source), 70)}"))
        for e in plan.collisions[:50]:
            log.info(fmt(C.GRAY,
                f"      ! {truncate(str(e.source), 55)}: {e.reason}"))


def run_migrate_mode(args):
    """Returns the exit code: 0 when the migration ran (or the user declined
    it), non-zero when it could not start or did not finish cleanly. The
    interactive menu ignores it; the --migrate flag exits with it."""
    section("Library migration: organise an existing collection")

    if not HAVE_MUTAGEN:
        log.warning(fmt(C.RED,
            "  ✗  mutagen isn't available, so tags can't be read and every file "
            "would be unidentifiable.\n     Use the bundled image (it includes "
            "mutagen) or `pip install mutagen`."))
        return EXIT_CONFIG

    src, dest = _resolve_paths(args)
    if src is None:
        return EXIT_CONFIG

    in_place = bool(getattr(args, "in_place", False))
    use_acoustid = bool(getattr(args, "acoustid", False))

    log.info(fmt(C.GRAY, f"  Source:      {src}"))
    log.info(fmt(C.GRAY, f"  Destination: {dest}"))
    if in_place:
        log.info(fmt(C.YELLOW + C.BOLD,
            "  In-place mode: files are MOVED into place; originals are "
            "relocated, not copied, and folders left empty are removed."))
    else:
        log.info(fmt(C.GREEN,
            "  Copy mode: your originals stay exactly where they are."))
    if not use_acoustid:
        log.info(fmt(C.GRAY,
            "  Tags only (fast). Add --acoustid to fingerprint files whose tags "
            "can't place them."))

    progress = _progress_printer()
    items = engine.collect_items(src, use_acoustid=use_acoustid, progress=progress)
    plan = engine.build_plan(items, dest)
    resume_entries = engine.verified_resume_entries(plan, progress=progress)

    _print_preview(plan, bool(getattr(args, "verbose", False)), in_place,
                   resume_entries)

    if getattr(args, "dry_run", False):
        log.info(fmt(C.CYAN, "  Dry run: nothing was copied."))
        return 0
    if not plan.placed and not resume_entries:
        log.info(fmt(C.GRAY, "  Nothing to place. Stopping."))
        return 0

    verb = "Move" if in_place else "Copy"
    need, free = engine.space_estimate(
        plan, in_place=in_place, resume_entries=resume_entries)
    short = free is not None and need > free
    if short and bool(getattr(args, "yes", False)):
        log.warning(fmt(C.RED,
            "  ✗  Not enough free space at the destination; refusing to start an "
            "unattended (--yes) migration that would run out mid-move and scatter "
            "the library. Free up space or pick another destination."))
        return EXIT_GENERAL
    if short:
        log.info(fmt(C.YELLOW,
            f"  ⚠  The destination is short on space (needs ~{format_size(need)}, "
            f"only {format_size(free)} free); a move that runs out leaves the "
            "library half-relocated."))
        ack = ask("  Type 'yes' to migrate anyway: ", colour=C.RED) or ""
        if ack != "yes":
            log.info(fmt(C.GRAY, "  Cancelled. Nothing changed."))
            return 0
    elif resume_entries:
        action = (f"{verb} {len(plan.placed)} new file(s) and finish "
                  f"{len(resume_entries)} verified existing file(s)"
                  if plan.placed else
                  f"Finish {len(resume_entries)} verified existing file(s)")
        if not confirm(f"  {action} in {dest}?", default_yes=False,
                       auto_yes=bool(getattr(args, "yes", False))):
            log.info(fmt(C.GRAY, "  Cancelled. Nothing changed."))
            return 0
    elif not confirm(f"  {verb} {len(plan.placed)} file(s) into {dest}?",
                     default_yes=False,
                     auto_yes=bool(getattr(args, "yes", False))):
        log.info(fmt(C.GRAY, "  Cancelled. Nothing changed."))
        return 0

    # Seal the exact approved plan before execution. A preview, refusal, or
    # decline stays read-only; an approved migration still cannot copy without
    # a durable, non-overwriting audit record beside the destination.
    try:
        manifest_artifact = engine.write_manifest(plan)
        manifest = Path(manifest_artifact["path"])
        log.info(fmt(C.GRAY, f"  Full plan written to {manifest}"))
    except (KeyError, OSError, TypeError, UnicodeError, ValueError) as e:
        log.warning(fmt(C.RED,
            "  ✗  Couldn't record the approved migration safely, so nothing "
            f"was copied ({e})."))
        return EXIT_GENERAL

    if not engine.verify_audit_artifact(plan, manifest_artifact):
        log.warning(fmt(C.RED,
            "  ✗  The reviewed migration record changed or can no longer be "
            "proved. Nothing was copied; scan again before approving it."))
        return EXIT_GENERAL

    execution_abort = None
    try:
        result = engine.execute_plan(
            plan, in_place=in_place, progress=progress,
            resume_entries=resume_entries)
    except engine.MigrationExecutionAbort as exc:
        result = exc.result
        execution_abort = exc

    try:
        results_artifact = engine.write_results_manifest(result, plan=plan)
        results_manifest = Path(results_artifact["path"])
        log.info(fmt(C.GRAY, f"  · Results manifest: {results_manifest}"))
    except BaseException as e:
        try:
            log.warning(fmt(C.RED,
                "  ✗  The migration ran, but its durable results record could "
                f"not be written ({e}). Do not start another migration until "
                "the destination and its audit files have been checked."))
            for recovery in getattr(result, "recoveries", ()):
                log.info(fmt(C.RED,
                    "     Recovery retained at: "
                    f"{recovery.get('location', 'unknown location')}"))
        finally:
            if execution_abort is not None:
                execution_abort.reraise(publication_error=e)
        if not isinstance(e, Exception):
            raise
        return EXIT_GENERAL

    if execution_abort is not None:
        execution_abort.reraise()

    pruned = getattr(result, "pruned", 0)
    companion_outcomes = getattr(result, "companion_outcomes", ())
    companion_skipped = sum(
        status == engine.SKIPPED
        for _source, _destination, status, _reason in companion_outcomes)
    companion_failed = sum(
        status == engine.FAILED
        for _source, _destination, status, _reason in companion_outcomes)
    recoveries = tuple(getattr(result, "recoveries", ()))
    has_problem = bool(result.failed or companion_failed or recoveries)

    log.info("")
    if has_problem:
        outcome = (
            "Migration stopped with problems."
            if result.cancelled else "Migration needs attention."
        )
        log.warning(fmt(C.RED, f"  ✗  {outcome}"))
    elif result.cancelled:
        log.warning(fmt(C.YELLOW,
            "  ⚠  Migration stopped early; the destination is incomplete."))
    else:
        log.info(fmt(C.GREEN,
            f"  ✓  {result.copied} file(s) "
            f"{'moved' if in_place else 'copied'}."))
    if has_problem or result.cancelled:
        log.info(fmt(C.GRAY,
            f"     {result.copied} file(s) "
            f"{'moved' if in_place else 'copied'} before the run ended."))
    if result.skipped:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {result.skipped} skipped (destination already existed)."))
    if getattr(result, "companions", 0):
        log.info(fmt(C.GREEN,
            f"  ✓  {result.companions} cover/sidecar file(s) carried."))
    if companion_skipped:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {companion_skipped} cover/sidecar file(s) already existed."))
    if companion_failed:
        log.info(fmt(C.RED,
            f"  ✗  {companion_failed} cover/sidecar file(s) could not be "
            "carried; see the results manifest."))
    if result.lingered:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {result.lingered} moved but the original couldn't be removed "
            f"(still at the source)."))
    if pruned:
        log.info(fmt(C.GRAY,
            f"  Cleared {pruned} now-empty folder(s) from the source."))
    if result.failed:
        log.info(fmt(C.RED, f"  ✗  {result.failed} failed:"))
        for src, reason in result.failures[:50]:
            log.info(fmt(C.RED, f"       {truncate(str(src), 60)}: {reason}"))
        if result.failed > 50:
            log.info(fmt(C.RED,
                f"       … and {result.failed - 50} more; see {results_manifest}"))
    if result.cancelled:
        log.info(fmt(C.YELLOW,
            "  ⚠  Stopped early; the destination holds a partial copy."))
    for recovery in recoveries:
        log.info(fmt(C.RED,
            "  ⚠  Recovery retained at "
            f"{recovery.get('location', 'an unknown location')}. "
            f"{recovery.get('restart', '')}"))
    log.info(fmt(C.GRAY,
        f"  New library: {dest}\n"
        f"  Plan:        {manifest}\n"
        f"  Results:     {results_manifest}\n"
        "  Spot-check it before pointing the tool at it as your main library."))
    # A run that lost files, stopped early, or left a recovery behind is not a
    # success, however much of it landed. A script chaining off this must see
    # the difference.
    if result.failed or companion_failed or result.cancelled or recoveries:
        return EXIT_GENERAL
    return 0
