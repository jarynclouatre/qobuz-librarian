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
from qobuz_librarian.ui_cli.colors import C, block, fmt, format_size, section, truncate
from qobuz_librarian.ui_cli.errors import EXIT_CONFIG, EXIT_GENERAL, EXIT_INTERRUPT, plural
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import confirm


def _prompt_path(msg: str) -> str:
    return ask(msg, lower=False, quiet=True) or ""


def _resolve_paths(args):
    """Source and destination from flags, then env, then an interactive prompt.

    Returns (src, dest) as validated Paths, or (None, None) after explaining
    what's wrong."""
    src = (getattr(args, "migrate_src", "") or config.MIGRATE_SRC).strip()
    dest = (getattr(args, "migrate_dest", "") or config.MIGRATE_DEST).strip()
    if not src:
        src = _prompt_path("  Source library to organise: ")
    if not src:
        log.warning(fmt(C.RED, "\n  ✗  No source was given. Nothing changed."))
        return None, None
    if not dest:
        dest = _prompt_path("  Destination for the organised copy: ")
    if not dest:
        log.warning(fmt(C.RED,
            "\n  ✗  No destination was given. Nothing changed."))
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
    everything else, so log each phase once and then every 500 files."""
    seen = set()

    def report(phase, current, total, item):
        if phase not in seen:
            seen.add(phase)
            log.info(fmt(C.GRAY, f"  {phase}…"))
        if total and (current == total or current % 500 == 0):
            log.info(fmt(C.GRAY, f"    {current}/{total}"))
        elif not total and current and current % 500 == 0:
            log.info(fmt(C.GRAY, f"    {current}"))

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
            f"    {plural(s['place'], 'file')} to place across "
            f"{plural(_artist_count(plan.placed), 'artist')}"))
    if resume_entries:
        log.info(fmt(C.WHITE,
            f"    {plural(len(resume_entries), 'existing file')} verified; "
            + (f"{'its original' if len(resume_entries) == 1 else 'their originals'} "
               "will be deleted and cover art and sidecars carried"
               if in_place else
               "cover art and sidecars can be resumed")))
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


def _print_leftovers(leftovers) -> None:
    if not leftovers:
        return
    log.info(fmt(C.YELLOW,
        f"    ⚠  An earlier migration was cut off and left "
        f"{plural(len(leftovers), 'hidden item')}. Ones named "
        ".qobuz-migrate-copy-, -dir- or -name-probe- are unfinished copies "
        "you can delete. The others may hold a track that was being moved: "
        "check it is in the new library before you delete one. This run "
        "leaves them alone."))
    for path in leftovers[:20]:
        log.info(fmt(C.YELLOW, f"      {path}"))
    if len(leftovers) > 20:
        log.info(fmt(C.YELLOW, f"      … and {len(leftovers) - 20} more"))


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
        log.info(block(fmt(C.YELLOW + C.BOLD,
            "  In-place mode: files are MOVED into place; originals are "
            "relocated, not copied, and folders left empty are removed.")))
    if not use_acoustid:
        log.info(block(fmt(C.GRAY,
            "  Tags only (fast). Add --acoustid to fingerprint files whose tags "
            "can't place them.")))

    progress = _progress_printer()
    try:
        items = engine.collect_items(
            src, use_acoustid=use_acoustid, progress=progress)
    except OSError as e:
        log.warning(fmt(C.RED,
            f"  ✗  Couldn't read the source library ({e}). Nothing was "
            "changed; fix this and run it again."))
        return EXIT_GENERAL
    unreadable = getattr(items, "unreadable", ())
    if unreadable:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {plural(len(unreadable), 'file or folder', 'files or folders')} "
            "couldn't be read and will stay where "
            f"{'it is' if len(unreadable) == 1 else 'they are'}:"))
        for path, reason in unreadable[:20]:
            log.info(fmt(C.YELLOW, f"       {truncate(str(path), 60)}: {reason}"))
        if len(unreadable) > 20:
            log.info(fmt(C.YELLOW, f"       … and {len(unreadable) - 20} more"))
    if not items:
        # Same as the Web scan: an empty source means the wrong folder or an
        # unmounted share, not a library with nothing worth moving.
        log.warning(fmt(C.RED,
            f"  ✗  The only music files left in {src} could not be read; "
            "they are listed above." if unreadable else
            f"  ✗  No music files were found in {src}. Check that it is the "
            "folder you meant and that its drive or share is attached, then "
            "run this again."))
        return EXIT_GENERAL
    plan = engine.build_plan(items, dest)
    resume_entries = engine.verified_resume_entries(plan, progress=progress)

    _print_preview(plan, bool(getattr(args, "verbose", False)), in_place,
                   resume_entries)
    _print_leftovers(engine.interrupted_migration_leftovers(src, dest))

    if getattr(args, "dry_run", False):
        log.info(fmt(C.CYAN,
            f"  Dry run: nothing was {'moved' if in_place else 'copied'}."))
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
        risk = ("a move that runs out leaves the library half-relocated"
                if in_place else
                "a copy that runs out stops partway, with the new library "
                "incomplete")
        log.info(fmt(C.YELLOW,
            f"  ⚠  The destination is short on space (needs ~{format_size(need)}, "
            f"only {format_size(free)} free); {risk}."))
        ack = ask("  Type 'yes' to migrate anyway: ", colour=C.RED) or ""
        if ack != "yes":
            log.info(fmt(C.GRAY, "  Cancelled. Nothing changed."))
            return 0
    elif resume_entries:
        action = (f"{verb} {plural(len(plan.placed), 'new file')} and finish "
                  f"{plural(len(resume_entries), 'verified existing file')}"
                  if plan.placed else
                  f"Finish {plural(len(resume_entries), 'verified existing file')}")
        if not confirm(f"  {action} in {dest}?", default_yes=False,
                       auto_yes=bool(getattr(args, "yes", False))):
            log.info(fmt(C.GRAY, "  Cancelled. Nothing changed."))
            return 0
    elif not confirm(f"  {verb} {plural(len(plan.placed), 'file')} into {dest}?",
                     default_yes=False,
                     auto_yes=bool(getattr(args, "yes", False))):
        log.info(fmt(C.GRAY, "  Cancelled. Nothing changed."))
        return 0

    # A preview, refusal, or decline stays read-only; an approved migration
    # still cannot copy without its plan recorded beside the destination.
    try:
        manifest = engine.write_manifest(plan)
        log.info(fmt(C.GRAY, f"  Full plan written to {manifest}"))
    except (KeyError, OSError, TypeError, UnicodeError, ValueError) as e:
        log.warning(fmt(C.RED,
            "  ✗  Couldn't record the approved migration safely, so nothing "
            f"was copied ({e})."))
        return EXIT_GENERAL

    result = engine.execute_plan(
        plan, in_place=in_place, progress=progress,
        resume_entries=resume_entries)

    try:
        results_manifest = engine.write_results_manifest(result, plan=plan)
        log.info(fmt(C.GRAY, f"  · Results manifest: {results_manifest}"))
    except BaseException as e:
        log.warning(fmt(C.RED,
            "  ✗  The migration ran, but its durable results record could "
            f"not be written ({e}). Do not start another migration until "
            "the destination and its audit files have been checked."))
        if not isinstance(e, Exception):
            raise
        return EXIT_GENERAL

    pruned = getattr(result, "pruned", 0)
    companion_outcomes = getattr(result, "companion_outcomes", ())
    companion_skipped = sum(
        status == engine.SKIPPED and reason != engine.SAME_COMPANION
        for _source, _destination, status, reason in companion_outcomes)
    companion_present = sum(
        status == engine.SKIPPED and reason == engine.SAME_COMPANION
        for _source, _destination, status, reason in companion_outcomes)
    skipped = sum(
        status == engine.SKIPPED
        and not reason.startswith(engine.VERIFIED_COPY)
        for _source, _destination, status, reason in result.outcomes)
    companion_failed = sum(
        status == engine.FAILED
        for _source, _destination, status, _reason in companion_outcomes)
    has_problem = bool(result.failed or companion_failed)

    log.info("")
    if has_problem or result.cancelled:
        log.info(fmt(C.GRAY,
            f"     {plural(result.copied, 'file')} "
            f"{'moved' if in_place else 'copied'} before the run ended."))
    else:
        if result.copied or not resume_entries:
            log.info(fmt(C.GRAY,
                f"     {plural(result.copied, 'file')} "
                f"{'moved' if in_place else 'copied'}."))
        if resume_entries:
            log.info(fmt(C.GRAY,
                f"     {plural(len(resume_entries), 'file')} already in place "
                f"{'was' if len(resume_entries) == 1 else 'were'} verified."))
    if result.interrupted:
        log.info(fmt(C.YELLOW,
            f"     ⚠  Stopped early; the library is partly moved into {dest}."
            if in_place else
            f"     ⚠  Stopped early; {dest} holds a partial copy."))
    if skipped:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {skipped} skipped (destination already existed)."))
    if getattr(result, "companions", 0):
        log.info(fmt(C.GREEN,
            f"  ✓  {plural(result.companions, 'cover/sidecar file')} carried."))
    if companion_present:
        log.info(fmt(C.GRAY,
            f"     {plural(companion_present, 'cover/sidecar file')} already in place."))
    if companion_skipped:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {plural(companion_skipped, 'cover/sidecar file')} already existed "
            "with different content."))
    if companion_failed:
        log.info(fmt(C.RED,
            f"  ✗  {plural(companion_failed, 'cover/sidecar file')} could not be "
            "carried; see the results manifest."))
    if result.lingered:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {plural(result.lingered, 'original')} still at the source; "
            "see the results manifest for why."))
    if unreadable:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {plural(len(unreadable), 'file or folder', 'files or folders')} "
            "could not be read and stayed at the source."))
    if pruned:
        log.info(fmt(C.GRAY,
            f"  Cleared {plural(pruned, 'now-empty folder')} from the source."))
    left = getattr(result, "left_in_source", ())
    left_out = [(folder, names) for folder, names in left if names]
    copies_only = len(left) - len(left_out)
    if left_out:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {plural(len(left_out), 'source folder')} still "
            f"{'holds' if len(left_out) == 1 else 'hold'} files that were "
            "not moved:"))
        for folder, names in left_out[:20]:
            shown = ", ".join(names[:8]) + (
                f" and {len(names) - 8} more" if len(names) > 8 else "")
            log.info(fmt(C.YELLOW, f"       {truncate(folder, 50)}: {shown}"))
        if len(left_out) > 20:
            log.info(fmt(C.YELLOW, f"       … and {len(left_out) - 20} more"))
    if copies_only:
        log.info(fmt(C.GRAY,
            f"  {plural(copies_only, 'source folder')} still "
            f"{'holds' if copies_only == 1 else 'hold'} only cover art and "
            "sidecars, which were copied."))
    if result.failed:
        log.info(fmt(C.RED, f"  ✗  {result.failed} failed:"))
        for src, reason in result.failures[:50]:
            log.info(fmt(C.RED, f"       {truncate(str(src), 60)}: {reason}"))
        if result.failed > 50:
            log.info(fmt(C.RED,
                f"       … and {result.failed - 50} more; see {results_manifest}"))
    if result.cancelled and not result.interrupted:
        log.info(fmt(C.YELLOW,
            "  ⚠  Stopped early; the library is partly moved."
            if in_place else
            "  ⚠  Stopped early; the destination holds a partial copy."))
    log.info(fmt(C.GRAY,
        f"  New library: {dest}\n"
        f"  Plan:        {manifest}\n"
        f"  Results:     {results_manifest}\n"
        "  Spot-check it before pointing the tool at it as your main library."))
    n_errors = result.failed + companion_failed
    if result.interrupted or result.cancelled:
        log.info(fmt(C.GRAY, "  Interrupted."))
    elif n_errors:
        log.warning(fmt(C.RED, f"  ✗  Finished with {plural(n_errors, 'error')}."))
    elif result.lingered:
        log.warning(fmt(C.YELLOW,
            "  ⚠  Finished, but some originals are still at the source."))
    elif unreadable:
        log.warning(fmt(C.YELLOW,
            "  ⚠  Finished, but some files could not be read."))
    else:
        log.info(fmt(C.GREEN, "  ✓  Done."))
    # A run with failures or one that stopped early is not a success, however
    # much of it landed. A script chaining off this must see the difference.
    if result.interrupted:
        return EXIT_INTERRUPT
    if (result.failed or companion_failed or result.cancelled
            or result.lingered or unreadable):
        return EXIT_GENERAL
    return 0
