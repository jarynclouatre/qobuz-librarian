"""Lyrics backfill walk: fetch lyrics for tracks already in the library.

Local apart from the provider HTTP, so it runs without a Qobuz login. Re-runs
are cheap (already-lyriced tracks are skipped from the state file) and safe to
interrupt; progress is checkpointed as it goes.
"""
import signal
import threading

from qobuz_librarian import config as cfg
from qobuz_librarian import saved_views
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.library import candidate_premise
from qobuz_librarian.library.lyrics import (
    HAVE_LYRICS,
    read_only_note,
    run_library_lyrics,
    summarize_lyrics_result,
)
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.ui_cli.colors import C, banner, block, fmt
from qobuz_librarian.ui_cli.errors import (
    EXIT_CONFIG,
    EXIT_GENERAL,
    EXIT_INTERRUPT,
    die,
    plural,
)
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.web import settings_store


def run_library_lyrics_mode(args):
    """Returns the exit code: 0 when the pass finished, non-zero when it was
    cut short or work failed. The interactive menu ignores it; --lyrics-walk
    exits with it."""
    clear_scan_caches()
    banner("Lyrics: fetch lyrics for tracks already in your library")

    if not HAVE_LYRICS:
        # die() writes to stderr, so a `--quiet --lyrics-walk` cron run still
        # shows it, and EXIT_CONFIG fails the cron's exit-code check.
        fix = (
            "   The image includes it, so rebuild it with "
            "`docker compose build --no-cache`.\n"
            if cfg.in_container() else
            "   Add it to the environment Qobuz Librarian runs in:\n"
            "     pipx inject qobuz-librarian 'syncedlyrics>=1.0'\n"
            "   or, for a pip install:\n"
            "     pip install 'syncedlyrics>=1.0'\n"
        )
        die(fmt(C.RED,
            "\n✗  Lyric fetching needs syncedlyrics (the [lyrics] extra), "
            "which isn't installed.\n" + fix), EXIT_CONFIG)

    providers = (", ".join(lyric_fetch.usable_providers(cfg.LYRICS_PROVIDERS))
                 or "no known provider (check LYRICS_PROVIDERS)")
    lyrics_format = settings_store.ENUM_OPTION_LABELS["LYRICS_FORMAT"].get(
        (cfg.LYRICS_FORMAT or "embed").lower(), cfg.LYRICS_FORMAT)
    log.info(fmt(C.GRAY,
        f"  Lyrics providers: {providers}. Lyrics format: {lyrics_format}."))
    if args.lyrics_rescan:
        log.info(fmt(C.GRAY,
            "  Also looking up tracks checked in the last 30 days."))
    if args.lyrics_synced_only:
        log.info(fmt(C.GRAY,
            "  --lyrics-synced-only: only timed (synced) lyrics will be written."))
    if args.dry_run:
        log.info(fmt(C.GRAY,
            "  --dry-run: reporting what would be fetched; nothing is written."))
    log.info(fmt(C.GRAY, "  Ctrl-C to stop.\n" if args.dry_run
                 else "  Ctrl-C to stop; progress is saved.\n"))

    stop_requested = threading.Event()
    announced = False

    def request_stop(signum, frame):
        if stop_requested.is_set():
            signal.default_int_handler(signum, frame)
        stop_requested.set()

    # The notice is logged here, not in the handler: Ctrl-C can land while
    # the log is writing to its file, and logging again from inside that
    # write fails.
    def should_stop():
        nonlocal announced
        if stop_requested.is_set() and not announced:
            announced = True
            log.warning(fmt(C.YELLOW,
                "  Stopping after the current tracks finish. Press Ctrl-C "
                "again if it won't stop."))
        return stop_requested.is_set()

    previous_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        with lyric_fetch.recording_writes() as written:
            try:
                res = run_library_lyrics(
                    dry_run=args.dry_run,
                    rescan=args.lyrics_rescan,
                    synced_only=args.lyrics_synced_only,
                    should_stop=should_stop,
                    log=log,
                )
            finally:
                if written:
                    # Saved Upgrade and Downsample picks stay valid over
                    # lyrics this pass wrote.
                    saved_views.reseal_saved_reviews(
                        candidate_premise.sealer_after_writes(written))
    except KeyboardInterrupt:
        print()
        log.info(fmt(C.GRAY, "  Interrupted."))
        return EXIT_INTERRUPT
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    failed = _report_summary(
        res, dry_run=args.dry_run, synced_only=args.lyrics_synced_only)
    if stop_requested.is_set():
        return EXIT_INTERRUPT
    return EXIT_GENERAL if failed else 0


def _report_summary(res, *, dry_run, synced_only=False):
    total = res.get("total", 0)
    unreadable = res.get("unreadable_artists", [])
    if unreadable:
        log.warning("Unchecked artists: " + ", ".join(unreadable) + ". Retry after checking permissions.")
    if not total:
        log.info(fmt(C.YELLOW, "  No FLAC files found in the library."))
        return bool(unreadable)

    summary = summarize_lyrics_result(res)
    processed = summary["processed"]
    skipped = summary["already_checked"]
    print()
    if summary["stopped"] and res.get("stop_stage") == "index":
        log.info(fmt(C.GRAY,
            f"     Scanned {processed} of {plural(summary['candidate_total'], 'track')}. "
            "No provider work started."))
        log.info(fmt(C.GRAY, "  Interrupted."))
        return True
    if not processed and not summary["stopped"] and not unreadable:
        log.info(fmt(C.GRAY,
            f"     Nothing needed checking; "
            f"{'the one track has' if total == 1 else f'all {total} tracks have'} "
            f"lyrics or {'was' if total == 1 else 'were'} checked before "
            f"(--lyrics-rescan checks {'it' if total == 1 else 'them'} again)."))
        log.info(fmt(C.GRAY, "  Nothing to do."))
        return False

    wrote_synced = res.get("wrote-synced", 0) + res.get("dry:wrote-synced", 0)
    wrote_plain  = res.get("wrote-plain", 0) + res.get("dry:wrote-plain", 0)

    verb = "Would write" if dry_run else "Wrote"
    skipped_part = (f" · {skipped} skipped (already checked)" if skipped else "")
    unfinished_part = (
        f" · {summary['unfinished']} "
        f"{'not checked' if dry_run else 'left for the next run'}"
        if summary["unfinished"] else ""
    )
    retry_part = (
        " (a run without --lyrics-synced-only tries them again)" if synced_only
        else f" (next try after {lyric_fetch.RECHECK_AFTER_DAYS} days)"
    ) if summary["not_found"] and not dry_run else ""
    checked_part = (
        f"{processed} of {plural(summary['candidate_total'], 'track')} checked"
        if summary["stopped"] else f"{plural(processed, 'track')} checked"
    )
    log.info(block(fmt(C.GRAY,
        f"     {checked_part} · {verb.lower()} "
        f"{wrote_synced} synced + {wrote_plain} plain · "
        f"{summary['already']} already had lyrics · "
        f"{summary['not_found']} no {'synced ' if synced_only else ''}"
        f"lyrics found{retry_part}"
        f"{skipped_part}{unfinished_part}.")))
    if summary["missing_tags"]:
        log.info(fmt(C.YELLOW,
            f"     {plural(summary['missing_tags'], 'track')} skipped because "
            "artist or title tags are missing."))
    if summary["too_long"]:
        log.info(fmt(C.YELLOW,
            f"     {plural(summary['too_long'], 'track')} skipped for running "
            "longer than 20 minutes."))
    if summary["instrumental"]:
        log.info(fmt(C.YELLOW,
            f"     {plural(summary['instrumental'], 'track')} skipped as "
            "instrumental."))
    if summary["read_only"]:
        log.info(fmt(C.YELLOW,
            f"     {read_only_note(summary['read_only'])}."))
    if summary["unsafe"]:
        log.warning(fmt(C.RED,
            f"     {plural(summary['unsafe'], 'track path')} refused as unsafe; "
            "check the library path and symlinks."))
    if summary["unavailable"]:
        log.warning(fmt(C.YELLOW,
            f"     {plural(summary['unavailable'], 'track')} couldn't reach a "
            "provider (rate-limited or down); re-run later to pick "
            f"{'it' if summary['unavailable'] == 1 else 'them'} up."))
    if summary["errors"]:
        log.warning(fmt(C.YELLOW,
            f"     {plural(summary['errors'], 'track')} hit an error while "
            "writing or fetching lyrics; see the log above."))
    if summary["other_errors"]:
        log.warning(fmt(C.YELLOW,
            f"     {plural(summary['other_errors'], 'track')} returned an "
            "unexpected result; see the log above."))
    if summary["stopped"]:
        log.info(fmt(C.GRAY, "  Interrupted."))
    elif summary["failed"]:
        log.warning(fmt(C.RED, f"  ✗  Finished with {plural(summary['failures'], 'error')}."))
    else:
        log.info(fmt(C.GREEN, "  ✓  Done."))
    return bool(summary["stopped"] or summary["failed"])
