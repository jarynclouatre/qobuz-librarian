"""Downsample walk: shrink hi-res library files to CD rate.

Pure local housekeeping: no Qobuz lookup, so it runs without credentials. The
resample is irreversible (the hi-res original is overwritten in place, with no
re-download fallback), so the walk confirms per artist with default NO and each
file is decode-verified before it replaces anything.
"""
import signal
import sys
import threading
import time

from qobuz_librarian import config as cfg
from qobuz_librarian import post_job_hook, saved_views
from qobuz_librarian.integrations.downsample_engine import (
    HAVE_DOWNSAMPLE,
    DownsampleInterrupted,
    downsample_dir,
    read_only_note,
)
from qobuz_librarian.library import downsample_state, generation_state
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.scanner import clear_scan_caches, list_library_artists
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.quality.decision import mark_local_album_capped
from qobuz_librarian.ui_cli.colors import (
    C,
    banner,
    block,
    fit_line,
    fmt,
    format_size,
    term_width,
    truncate,
)
from qobuz_librarian.ui_cli.errors import (
    EXIT_CONFIG,
    EXIT_GENERAL,
    EXIT_INTERRUPT,
    no_artist_folders,
    plural,
    unreadable_artist,
)
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm
from qobuz_librarian.web import settings_store


def _downsample_album(album_dir, keep_originals):
    """Rewrite one album; a Ctrl-C lets it finish the tracks in flight and
    report them before the walk stops. A second one also cuts short a safety
    copy still being made; the tracks in flight always finish and are
    recorded. The encoders run in their own session, so the terminal's
    Ctrl-C reaches only the app."""
    stop = threading.Event()
    presses = []

    def request_stop(signum, frame):
        presses.append(signum)
        if len(presses) == 2:
            signal.default_int_handler(signum, frame)
        stop.set()

    previous = signal.signal(signal.SIGINT, request_stop)
    try:
        res = downsample_dir(album_dir, verbose=True, base_dir=album_dir,
                             log=log.info, keep_originals=keep_originals,
                             cancel_check=stop.is_set)
    except DownsampleInterrupted as exc:
        return exc.result, True
    finally:
        signal.signal(signal.SIGINT, previous)
    return res, stop.is_set()


def run_downsample_walk_mode(args):
    """Walk every artist, find albums stored above CD rate, and shrink the
    chosen ones in place.

    Artists with nothing to shrink are skipped silently (a \\r progress line
    overwrites itself while scanning). Each artist confirms separately, default
    NO; --yes accepts every one and --dry-run lists what would shrink and
    changes nothing.

    Returns the exit code: 0 when the walk finished without file errors or
    durability warnings, non-zero when it failed, was cut short, or couldn't
    ask. The interactive menu ignores it; --downsample-walk exits with it.
    """
    clear_scan_caches()
    banner("Downsample: bring library files above 48 kHz down to 44.1 / 48 kHz")
    started = time.time()

    if not HAVE_DOWNSAMPLE:
        log.info(fmt(C.YELLOW,
            "  ⚠  Downsampling isn't available (needs ffmpeg and flac)."))
        return EXIT_CONFIG

    discovery_errors = {}

    def artist_read_failed(path, error):
        discovery_errors[path.name] = str(error)
        log.warning(unreadable_artist(path.name, error))

    all_artists = list_library_artists(on_artist_error=artist_read_failed)
    if not all_artists:
        log.info(block(fmt(C.YELLOW, f"  ⚠  {no_artist_folders()}")))
        return EXIT_GENERAL if discovery_errors else 0

    keep_originals = cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"
    if cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep":
        log.info(block(fmt(C.YELLOW,
            "  ⚠  This rewrites hi-res files in place to 44.1/48kHz. A "
            "restorable copy of each original is kept for "
            f"{plural(cfg.UPGRADE_BACKUP_RETENTION_DAYS, 'day')}; a rewrite "
            "can be undone from Settings → Diagnostics in the web app until "
            "then.")))
    elif cfg.DOWNSAMPLE_KEEP_ORIGINALS == "delete":
        log.info(block(fmt(C.YELLOW,
            "  ⚠  This rewrites hi-res files in place to 44.1/48kHz. The "
            "originals are not kept and there is no undo.")))
    else:
        log.info(block(fmt(C.YELLOW,
            "  ⚠  This rewrites hi-res files in place to 44.1/48kHz. You'll "
            "choose first whether to keep the hi-res originals.")))
    if args.dry_run:
        log.info(fmt(C.GRAY, "  --dry-run: listing candidates only, nothing is changed."))
    log.info(fmt(C.GRAY, "  Ctrl-C to stop at any point."))

    hidden = hidden_mod.load()
    # One column short of the edge, where a terminal would wrap.
    status_width = term_width() - 1

    def _on_artist(artist_dir, _candidates, error, done, total):
        _scan_line = f"  [{done}/{total}] Scanning {truncate(artist_dir.name, 46).removesuffix('…')}…"
        if sys.stdout.isatty():
            print(f"\r{fit_line(_scan_line, status_width)}", end="", flush=True)
        if error is not None and artist_dir.name not in discovery_errors:
            log.info("")
            log.info(fmt(C.YELLOW,
                f"  Skipped {artist_dir.name}: {error}"))

    refresh = downsample_state.refresh_for_artists(
        all_artists,
        hidden=hidden,
        on_artist=_on_artist,
        discovery_errors=discovery_errors,
        persist=not args.dry_run,
    )
    if sys.stdout.isatty():
        print(f"\r{' ' * status_width}\r", end="", flush=True)
    unchecked = len(refresh.errors)
    # The snapshot keeps albums the user chose to keep at hi-res; this run only
    # offers the ones still on the list.
    offered = downsample_state.visible_of(refresh.candidates, hidden)
    candidates_by_artist = {}
    for candidate in offered:
        candidates_by_artist.setdefault(candidate.artist, []).append(candidate)

    # First downsample with the keep-vs-delete choice unmade: ask once. The
    # web UI asks the same thing and saves it as the standing default
    # (changeable later in Settings).
    if (offered and not args.dry_run
            and cfg.DOWNSAMPLE_KEEP_ORIGINALS not in ("keep", "delete")):
        _flush_stdin()
        keep = confirm(
            "  Keep a restorable backup of the hi-res originals? "
            "(answering No deletes them to save space)",
            default_yes=True, auto_yes=False, on_eof=None, quiet=True)
        if keep is None:
            log.warning(fmt(C.YELLOW,
                "\n  No answer was given. No music files were changed."))
            return EXIT_GENERAL
        else:
            _choice = "keep" if keep else "delete"
            saved, _warnings = settings_store.save(
                {"DOWNSAMPLE_KEEP_ORIGINALS": _choice}
            )
            if saved is not True:
                log.warning(block(fmt(
                    C.YELLOW,
                    "  ✗  Couldn't save the keep-or-delete choice. No music "
                    "files were changed; check the data folder and try again."
                )))
                return EXIT_CONFIG
            # A Web job may defer applying the saved setting, so use this
            # answer for the current run.
            keep_originals = keep
            log.info(fmt(C.GRAY,
                f"  Saved. Originals will be {'kept' if keep else 'deleted'}."))
        log.info("")

    auto_accept_all = False
    if offered and not args.dry_run and not args.yes:
        answer = confirm(
            "\n  Auto-accept every artist and run unattended?",
            default_yes=False, auto_yes=False, on_eof=None, quiet=True)
        if answer is None:
            log.warning(fmt(C.YELLOW,
                "\n  No answer was given. No music files were changed."))
            return EXIT_GENERAL
        if answer:
            auto_accept_all = True
            log.info(fmt(C.GREEN, "  ✓ Auto-accepting every artist."))
    log.info("")

    n_scanned = len(refresh.artists_scanned) - unchecked
    n_albums_done = 0
    n_attempted = 0
    total_saved = 0
    # A preview never reaches the counters below, so it keeps its own running
    # total of what it listed and how much smaller that would make it.
    n_albums_found = 0
    est_total_saving = 0
    total_errors = 0
    total_damaged = 0
    total_flush_warns = 0
    total_read_only = 0
    state_refresh_warnings = 0
    interrupted = False
    no_answer = False

    try:
        for artist_dir in all_artists:
            artist_name = artist_dir.name
            candidates = candidates_by_artist.get(artist_name, [])
            if not candidates:
                continue

            log.info("")
            log.info("")
            n_albums = len(candidates)
            est_total = sum(c.est_saving for c in candidates)
            n_albums_found += n_albums
            est_total_saving += est_total
            log.info(fmt(C.BOLD + C.WHITE, f"  {artist_name}"))
            log.info(fmt(C.MAGENTA,
                f"  {plural(n_albums, 'album')} above 48 kHz would be "
                f"~{format_size(est_total)} smaller:"))
            log.info("")
            for c in candidates:
                log.info(f"    • {fmt(C.WHITE, truncate(c.label, 48))}   "
                         f"{fmt(C.GRAY, c.detail)}")
            log.info("")

            if args.dry_run:
                continue

            _flush_stdin()
            # on_eof=None so a closed stdin is distinguishable from a No: the
            # walk would otherwise skip every artist and call that a success.
            answer = confirm(f"  Downsample {plural(n_albums, 'album')}?",
                             default_yes=False,
                             auto_yes=args.yes or auto_accept_all, on_eof=None,
                             quiet=True)
            if answer is None:
                no_answer = True
                break
            if not answer:
                log.info(fmt(C.GRAY, "  Skipped."))
                log.info("")
                continue

            artist_attempted = False
            for j, c in enumerate(candidates, 1):
                log.info("")
                log.info(fmt(C.BOLD + C.WHITE,
                    f"  [{j}/{n_albums}] {truncate(c.label, 55)}"))
                artist_attempted = True
                if mark_local_album_capped(c.album_dir) is not True:
                    total_errors += 1
                    log.info(fmt(
                        C.YELLOW,
                        "  The local downsample decision could not be saved; "
                        "the album was left unchanged.",
                    ))
                    continue
                if not upgrade_state.remove_album_dir(c.album_dir):
                    generation_state.mark_output_status(
                        "upgrade",
                        "stale",
                        reason=(
                            "An intentional Downsample could not update the "
                            "saved Upgrade view."
                        ),
                    )
                    state_refresh_warnings += 1
                n_attempted += 1
                res, stopped = _downsample_album(c.album_dir, keep_originals)
                if res.get("resampled"):
                    n_albums_done += 1
                total_saved += res.get("saved_bytes", 0)
                total_errors += res.get("errors", 0)
                total_damaged += sum(
                    1 for entry in res.get("failed_files") or []
                    if entry.get("damaged"))
                total_flush_warns += res.get("flush_warnings", 0)
                total_read_only += res.get("read_only", 0)
                if stopped:
                    interrupted = True
                    break
            if artist_attempted:
                saved_views.refresh_downsample_artist(artist_dir)
            log.info("")
            if interrupted:
                break
    except KeyboardInterrupt:
        log.info("")
        interrupted = True

    log.info("")
    if no_answer and not n_attempted:
        log.warning(fmt(C.YELLOW,
            "  No answer was given. No music files were changed."))
        return EXIT_GENERAL
    if args.dry_run:
        log.info(fmt(C.GRAY,
            f"     Checked {plural(n_scanned, 'artist')}; found "
            f"{plural(n_albums_found, 'album')} to downsample, "
            f"~{format_size(est_total_saving)} smaller."))
    else:
        log.info(fmt(C.GRAY,
            f"     Checked {plural(n_scanned, 'artist')}; downsampled "
            f"{plural(n_albums_done, 'album')}, "
            f"{format_size(total_saved)} smaller."))
        if keep_originals and n_albums_done:
            log.info(block(fmt(C.GRAY,
                "     The hi-res originals are kept for "
                f"{plural(cfg.UPGRADE_BACKUP_RETENTION_DAYS, 'day')}, so "
                "nothing is freed until they expire. Until then they can be "
                "put back from Settings → Diagnostics in the web app.")))
    if total_read_only:
        log.info(fmt(C.GRAY, f"     {read_only_note(total_read_only)}."))
    if total_errors:
        log.info(fmt(C.YELLOW,
            f"     {plural(total_errors, 'file')} could not be downsampled "
            "(left unchanged)."))
    if total_damaged:
        log.info(fmt(C.YELLOW,
            f"     {plural(total_damaged, 'file')} "
            f"{'doesn' if total_damaged == 1 else 'don'}'t decode cleanly "
            "and may be damaged; Repair can re-download "
            f"{'it' if total_damaged == 1 else 'them'}."))
    if total_flush_warns:
        log.warning(block(fmt(C.YELLOW,
            f"     ⚠  {plural(total_flush_warns, 'file')} resampled but couldn't be "
            "flushed to disk. The swap may not survive a power loss; check "
            "the drive.")))
    clause = None
    if unchecked:
        clause = f"{plural(unchecked, 'artist')} could not be checked"
    elif no_answer:
        clause = "no answer was given, so the walk stopped early"
    elif total_flush_warns:
        clause = f"{plural(total_flush_warns, 'file')} could not be flushed to disk"
    elif state_refresh_warnings:
        clause = "the saved Upgrade results need a Library refresh"
    if interrupted:
        log.info(fmt(C.GRAY, "  Interrupted."))
    elif total_errors:
        log.warning(fmt(C.RED, f"  ✗  Finished with {plural(total_errors, 'error')}."))
    elif clause:
        log.warning(fmt(C.YELLOW, f"  ⚠  Needs attention: {clause}."))
    elif not offered:
        log.info(fmt(C.GRAY, "  Nothing to do."))
    else:
        log.info(fmt(C.GREEN, "  ✓  Done."))
    if n_attempted:
        parts = [f"{plural(n_albums_done, 'album')} downsampled, "
                 f"{format_size(total_saved)} smaller"]
        if total_errors:
            parts.append(f"{plural(total_errors, 'file')} left unchanged")
        if total_read_only:
            parts.append(read_only_note(total_read_only))
        if total_flush_warns:
            parts.append(f"{plural(total_flush_warns, 'file')} not flushed "
                         "to disk")
        failed = total_errors or total_flush_warns
        post_job_hook.report_terminal_run(
            kind="downsample", title="Downsample run",
            status="canceled" if interrupted else "failed" if failed else "done",
            summary=(("Stopped early. " if interrupted else "")
                     + ", ".join(parts) + "."),
            error=(f"Finished with {plural(failed, 'error')}."
                   if failed and not interrupted else None),
            started_at=started)
    if interrupted:
        return EXIT_INTERRUPT
    return EXIT_GENERAL if (no_answer or unchecked
                            or total_errors or total_flush_warns
                            or state_refresh_warnings) else 0
