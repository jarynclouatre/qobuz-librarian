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


class _Cancelled(Exception):
    pass


def _prompt_path(msg: str) -> str:
    answer = ask(msg, lower=False, quiet=True) or ""
    if answer.lower() in ("q", "quit", "exit"):
        raise _Cancelled
    return answer


def _resolve_paths(args):
    """Source and destination from flags, then env, then an interactive prompt.

    Returns (src, dest) as validated Paths, or (None, None) after explaining
    what's wrong."""
    src = (getattr(args, "migrate_src", "") or config.MIGRATE_SRC).strip()
    dest = (getattr(args, "migrate_dest", "") or config.MIGRATE_DEST).strip()
    mounted = config.in_container()
    if not src:
        src = _prompt_path(
            "  Source library to organise"
            + (" (a folder mounted into the container, e.g. /migrate-source)"
               if mounted else "") + ": ")
    if not src:
        log.warning(fmt(C.RED, "\n  ✗  No source was given."))
        return None, None
    if not Path(src).is_dir():
        log.warning(fmt(C.RED, f"  ✗  Source isn't a readable directory: {src}"))
        return None, None
    if not dest:
        dest = _prompt_path(
            "  Destination for the organised copy"
            + (" (a folder mounted into the container, e.g. /migrate-dest)"
               if mounted else "") + ": ")
    if not dest:
        log.warning(fmt(C.RED,
            "\n  ✗  No destination was given."))
        return None, None

    src, dest = Path(src), Path(dest)
    in_place = bool(getattr(args, "in_place", False))
    err = engine.validate_paths(src, dest, in_place=in_place)
    if err:
        log.warning(fmt(C.RED, f"  ✗  {err}"))
        return None, None
    return src, dest


def _progress_printer():
    """Heartbeat progress: log each phase once and then every 500 files."""
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
            f"    {plural(s['unplaceable'], 'file')} couldn't be identified; left in place"))
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
    return _MigrateRun(args).run()


class _MigrateRun:
    """One terminal migration, from the chosen folders to the summary.

    A step that ends the run returns its exit code; the others return None."""

    def __init__(self, args):
        self.args = args

    def run(self):
        section("Library migration: organise an existing collection")
        for step in (self._resolve, self._collect, self._preview,
                     self._approve, self._record_plan, self._execute):
            code = step()
            if code is not None:
                return code
        self._count_outcomes()
        self._report_counts()
        self._report_end()
        return self._exit_code()

    def _resolve(self):
        if not HAVE_MUTAGEN:
            log.warning(fmt(C.RED,
                "  ✗  mutagen isn't available, so tags can't be read and every file "
                "would be unidentifiable.\n     Use the bundled image (it includes "
                "mutagen) or `pip install mutagen`."))
            return EXIT_CONFIG

        try:
            self.src, self.dest = _resolve_paths(self.args)
        except _Cancelled:
            log.info(fmt(C.GRAY, "  Cancelled."))
            return 0
        if self.src is None:
            return EXIT_CONFIG

        self.in_place = bool(getattr(self.args, "in_place", False))
        self.use_acoustid = bool(getattr(self.args, "acoustid", False))

        log.info(fmt(C.GRAY, f"  Source:      {self.src}"))
        log.info(fmt(C.GRAY, f"  Destination: {self.dest}"))
        if self.in_place:
            log.info(block(fmt(C.YELLOW + C.BOLD,
                "  In-place mode: files are MOVED into place; originals are "
                "relocated, not copied, and folders left empty are removed.")))
        if not self.use_acoustid:
            log.info(block(fmt(C.GRAY,
                "  Tags only (fast). Add --acoustid to fingerprint files whose tags "
                "can't place them.")))
        return None

    def _collect(self):
        self.progress = _progress_printer()
        try:
            self.items = engine.collect_items(
                self.src, use_acoustid=self.use_acoustid, progress=self.progress)
        except OSError as e:
            log.warning(fmt(C.RED,
                f"  ✗  Couldn't read the source library ({e}). Fix this and "
                "run it again."))
            return EXIT_GENERAL
        skipped = engine.skipped_folders_note(self.items)
        if skipped:
            log.info(fmt(C.GRAY, f"  {skipped}"))
        self.unreadable = getattr(self.items, "unreadable", ())
        if self.unreadable:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {plural(len(self.unreadable), 'file or folder', 'files or folders')} "
                "couldn't be read and will stay where "
                f"{'it is' if len(self.unreadable) == 1 else 'they are'}:"))
            for path, reason in self.unreadable[:20]:
                log.info(fmt(C.YELLOW, f"       {truncate(str(path), 60)}: {reason}"))
            if len(self.unreadable) > 20:
                log.info(fmt(C.YELLOW, f"       … and {len(self.unreadable) - 20} more"))
        if not self.items:
            # Same as the Web scan: an empty source means the wrong folder or an
            # unmounted share, not a library with nothing worth moving.
            log.warning(fmt(C.RED,
                f"  ✗  The only music files left in {self.src} could not be read; "
                "they are listed above." if self.unreadable else
                f"  ✗  No music files were found in {self.src}. Check that it is the "
                "folder you meant and that its drive or share is attached, then "
                "run this again."))
            return EXIT_GENERAL
        return None

    def _preview(self):
        self.plan = engine.build_plan(self.items, self.dest)
        self.resume_entries = engine.verified_resume_entries(self.plan, progress=self.progress)

        _print_preview(self.plan, bool(getattr(self.args, "verbose", False)), self.in_place,
                       self.resume_entries)
        _print_leftovers(engine.interrupted_migration_leftovers(self.src, self.dest))

        if getattr(self.args, "dry_run", False):
            log.info(fmt(C.CYAN,
                f"  Dry run: nothing was {'moved' if self.in_place else 'copied'}."))
            return 0
        if not self.plan.placed and not self.resume_entries:
            log.info(fmt(C.GRAY, "  Nothing to place. Stopping."))
            return 0
        return None

    def _approve(self):
        verb = "Move" if self.in_place else "Copy"
        need, free = engine.space_estimate(
            self.plan, in_place=self.in_place, resume_entries=self.resume_entries)
        short = free is not None and need > free
        if short and bool(getattr(self.args, "yes", False)):
            log.warning(fmt(C.RED,
                "  ✗  Not enough free space at the destination; refusing to start an "
                "unattended (--yes) migration that would run out mid-move and scatter "
                "the library. Free up space or pick another destination."))
            return EXIT_GENERAL
        if short:
            risk = ("a move that runs out leaves the library half-relocated"
                    if self.in_place else
                    "a copy that runs out stops partway, with the new library "
                    "incomplete")
            log.info(fmt(C.YELLOW,
                f"  ⚠  The destination is short on space (needs ~{format_size(need)}, "
                f"only {format_size(free)} free); {risk}."))
            ack = ask("  Type 'yes' to migrate anyway: ", colour=C.RED) or ""
            if ack != "yes":
                log.info(fmt(C.GRAY, "  Cancelled."))
                return 0
        elif self.resume_entries:
            action = (f"{verb} {plural(len(self.plan.placed), 'new file')} and finish "
                      f"{plural(len(self.resume_entries), 'verified existing file')}"
                      if self.plan.placed else
                      f"Finish {plural(len(self.resume_entries), 'verified existing file')}")
            if not confirm(f"  {action} in {self.dest}?", default_yes=False,
                           auto_yes=bool(getattr(self.args, "yes", False))):
                log.info(fmt(C.GRAY, "  Cancelled."))
                return 0
        elif not confirm(f"  {verb} {plural(len(self.plan.placed), 'file')} into {self.dest}?",
                         default_yes=False,
                         auto_yes=bool(getattr(self.args, "yes", False))):
            log.info(fmt(C.GRAY, "  Cancelled."))
            return 0
        return None

    def _record_plan(self):
        # A preview, refusal, or decline stays read-only; an approved migration
        # still cannot copy without its plan recorded beside the destination.
        try:
            self.manifest = engine.write_manifest(self.plan)
            log.info(fmt(C.GRAY, f"  Full plan written to {self.manifest}"))
        except (KeyError, OSError, TypeError, UnicodeError, ValueError) as e:
            log.warning(fmt(C.RED,
                "  ✗  Couldn't record the approved migration, so nothing "
                f"was copied ({e})."))
            return EXIT_GENERAL
        return None

    def _execute(self):
        self.result = engine.execute_plan(
            self.plan, in_place=self.in_place, progress=self.progress,
            resume_entries=self.resume_entries)

        try:
            self.results_manifest = engine.write_results_manifest(self.result, plan=self.plan)
            log.info(fmt(C.GRAY, f"  · Results manifest: {self.results_manifest}"))
        except BaseException as e:
            log.warning(fmt(C.RED,
                "  ✗  The migration ran, but its durable results record could "
                f"not be written ({e}). Do not start another migration until "
                "the destination and its audit files have been checked."))
            if not isinstance(e, Exception):
                raise
            return EXIT_GENERAL
        return None

    def _count_outcomes(self):
        self.pruned = getattr(self.result, "pruned", 0)
        companion_outcomes = getattr(self.result, "companion_outcomes", ())
        self.companion_skipped = sum(
            status == engine.SKIPPED and reason != engine.SAME_COMPANION
            for _source, _destination, status, reason in companion_outcomes)
        self.companion_present = sum(
            status == engine.SKIPPED and reason == engine.SAME_COMPANION
            for _source, _destination, status, reason in companion_outcomes)
        self.skipped = sum(
            status == engine.SKIPPED
            and not reason.startswith(engine.VERIFIED_COPY)
            for _source, _destination, status, reason in self.result.outcomes)
        self.verified = sum(
            status == engine.SKIPPED
            and reason.startswith(engine.VERIFIED_COPY)
            for _source, _destination, status, reason in self.result.outcomes)
        self.companion_failed = sum(
            status == engine.FAILED
            for _source, _destination, status, _reason in companion_outcomes)

    def _report_counts(self):
        log.info("")
        if self.result.cancelled:
            log.info(fmt(C.GRAY,
                f"     {plural(self.result.copied, 'file')} "
                f"{'moved' if self.in_place else 'copied'} before the run ended."))
        else:
            if self.result.copied or not self.verified:
                log.info(fmt(C.GRAY,
                    f"     {plural(self.result.copied, 'file')} "
                    f"{'moved' if self.in_place else 'copied'}."))
            if self.verified:
                log.info(fmt(C.GRAY,
                    f"     {plural(self.verified, 'file')} already in place "
                    f"{'was' if self.verified == 1 else 'were'} verified."))
        if self.result.interrupted:
            log.info(fmt(C.YELLOW,
                f"     ⚠  Stopped early; the library is partly moved into {self.dest}."
                if self.in_place else
                f"     ⚠  Stopped early; {self.dest} holds a partial copy."))
        if self.skipped:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {self.skipped} skipped (destination already existed)."))
        if getattr(self.result, "companions", 0):
            log.info(fmt(C.GREEN,
                f"  ✓  {plural(self.result.companions, 'cover/sidecar file')} carried."))
        if self.companion_present:
            log.info(fmt(C.GRAY,
                f"     {plural(self.companion_present, 'cover/sidecar file')} already in place."))
        if self.companion_skipped:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {plural(self.companion_skipped, 'cover/sidecar file')} already existed "
                "with different content."))
        if self.companion_failed:
            log.info(fmt(C.RED,
                f"  ✗  {plural(self.companion_failed, 'cover/sidecar file')} could not be "
                "carried; see the results manifest."))
        if self.result.lingered:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {plural(self.result.lingered, 'original')} still at the source; "
                "see the results manifest for why."))
        if self.unreadable:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {plural(len(self.unreadable), 'file or folder', 'files or folders')} "
                "could not be read and stayed at the source."))
        if self.pruned:
            log.info(fmt(C.GRAY,
                f"  Cleared {plural(self.pruned, 'now-empty folder')} from the source."))
        left_lines = engine.left_in_source_lines(
            getattr(self.result, "left_in_source", ()))
        if left_lines:
            log.info(fmt(C.YELLOW, f"  ⚠  {left_lines[0]}"))
            for line in left_lines[1:]:
                log.info(fmt(C.YELLOW, f"       {line}"))
        resumed = {id(entry) for entry in self.resume_entries}
        not_carried = engine.not_carried_line(
            sum(id(entry) not in resumed for entry in self.plan.collisions),
            self.plan.summary()["unplaceable"])
        if not_carried:
            log.info(fmt(C.YELLOW, f"  ⚠  {not_carried}."))

    def _report_end(self):
        if self.result.failed:
            log.info(fmt(C.RED, f"  ✗  {self.result.failed} failed:"))
            for src, reason in self.result.failures[:50]:
                log.info(fmt(C.RED, f"       {truncate(str(src), 60)}: {reason}"))
            if self.result.failed > 50:
                log.info(fmt(C.RED,
                    f"       … and {self.result.failed - 50} more; see {self.results_manifest}"))
        if self.result.cancelled and not self.result.interrupted:
            log.info(fmt(C.YELLOW,
                "  ⚠  Stopped early; the library is partly moved."
                if self.in_place else
                "  ⚠  Stopped early; the destination holds a partial copy."))
        log.info(fmt(C.GRAY,
            f"  New library: {self.dest}\n"
            f"  Plan:        {self.manifest}\n"
            f"  Results:     {self.results_manifest}\n"
            "  Spot-check it before pointing the tool at it as your main library."))
        n_errors = self.result.failed + self.companion_failed
        if self.result.interrupted:
            log.info(fmt(C.GRAY, "  Interrupted."))
        elif self.result.cancelled:
            log.warning(fmt(C.RED,
                "  ✗  Stopped early"
                + (f" with {plural(n_errors, 'error')}" if n_errors else "") + "."))
        elif n_errors:
            log.warning(fmt(C.RED, f"  ✗  Finished with {plural(n_errors, 'error')}."))
        elif self.result.lingered:
            log.warning(fmt(C.YELLOW,
                "  ⚠  Finished, but some originals are still at the source."))
        elif self.unreadable:
            log.warning(fmt(C.YELLOW,
                "  ⚠  Finished, but "
                f"{plural(len(self.unreadable), 'file or folder', 'files or folders')} "
                "could not be read."))
        else:
            log.info(fmt(C.GREEN, "  ✓  Done."))

    def _exit_code(self):
        # A run with failures or one that stopped early is not a success, however
        # much of it landed. A script chaining off this must see the difference.
        if self.result.interrupted:
            return EXIT_INTERRUPT
        if (self.result.failed or self.companion_failed or self.result.cancelled
                or self.result.lingered or self.unreadable):
            return EXIT_GENERAL
        return 0
