"""Lyrics backfill walk: fetch lyrics for tracks already in the library.

Local apart from the provider HTTP, so it runs without a Qobuz login. Re-runs
are cheap (already-lyriced tracks are skipped from the state file) and safe to
interrupt; progress is checkpointed as it goes.
"""
import signal
import threading

from qobuz_librarian import config as cfg
from qobuz_librarian.library.lyrics import (
    HAVE_LYRICS,
    run_library_lyrics,
    summarize_lyrics_result,
)
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.ui_cli.colors import C, banner, fmt
from qobuz_librarian.ui_cli.errors import EXIT_CONFIG, EXIT_GENERAL, die, plural
from qobuz_librarian.ui_cli.logging import log


def run_library_lyrics_mode(args):
    """Returns the exit code: 0 when the pass finished, non-zero when it was
    cut short or work failed. The interactive menu ignores it; --lyrics-walk
    exits with it."""
    clear_scan_caches()
    banner("Lyrics: fetch lyrics for tracks already in your library")

    if not HAVE_LYRICS:
        # log.warning (not log.info) so an unattended `--quiet --lyrics-walk`
        # cron run still surfaces the missing dep instead of looking like a
        # silent success; die() with EXIT_CONFIG so the cron's exit-code
        # check notices too.
        log.warning(fmt(C.YELLOW,
            "  ⚠  Lyric fetching isn't available; the syncedlyrics provider "
            "library isn't installed."))
        log.warning(fmt(C.GRAY,
            "     The bundled Docker image includes it; bare installs need "
            "the [lyrics] extra (`pip install 'syncedlyrics>=1.0'` also works)."))
        die("syncedlyrics not installed", EXIT_CONFIG)

    providers = ", ".join(cfg.LYRICS_PROVIDERS) or "Lrclib, NetEase, Musixmatch"
    log.info(fmt(C.GRAY,
        f"  Writing {(cfg.LYRICS_FORMAT or 'embed').lower()} lyrics via {providers}."))
    if args.lyrics_rescan:
        log.info(fmt(C.GRAY,
            "  --lyrics-rescan: re-checking every track, ignoring saved state."))
    if args.lyrics_synced_only:
        log.info(fmt(C.GRAY,
            "  --lyrics-synced-only: only timed (synced) lyrics will be written."))
    if args.dry_run:
        log.info(fmt(C.GRAY,
            "  --dry-run: reporting what would be fetched; nothing is written."))
    log.info(fmt(C.GRAY, "  Ctrl-C to stop; progress is saved.\n"))

    stop_requested = threading.Event()

    def request_stop(signum, frame):
        if stop_requested.is_set():
            signal.default_int_handler(signum, frame)
        stop_requested.set()
        log.warning(fmt(C.YELLOW,
            "  Stopping after the current tracks finish. Press Ctrl-C again "
            "if it won't stop."))

    previous_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        res = run_library_lyrics(
            dry_run=args.dry_run,
            rescan=args.lyrics_rescan,
            synced_only=args.lyrics_synced_only,
            should_stop=stop_requested.is_set,
            log=log,
        )
    except KeyboardInterrupt:
        print()
        log.warning(fmt(C.YELLOW,
            "  Interrupted. What was done is saved; re-run to continue."))
        return EXIT_GENERAL
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    failed = _report_summary(res, dry_run=args.dry_run)
    return EXIT_GENERAL if failed else 0


def _report_summary(res, *, dry_run):
    total = res.get("total", 0)
    if not total:
        log.info(fmt(C.YELLOW, "  No FLAC files found in the library."))
        return False

    summary = summarize_lyrics_result(res)
    processed = summary["processed"]
    skipped = summary["already_checked"]
    print()
    if summary["stopped"] and res.get("stop_stage") == "index":
        log.warning(fmt(C.YELLOW,
            "  ⚠  Lyrics pass stopped while checking existing tags."))
        log.info(fmt(C.GRAY,
            f"     Scanned {processed} of {plural(summary['candidate_total'], 'track')}. "
            "No provider work started."))
        return True
    if not processed and not summary["stopped"]:
        log.info(fmt(C.GREEN, "  ✓  Lyrics pass complete."))
        log.info(fmt(C.GRAY,
            f"     Nothing needed checking; all {plural(total, 'track')} "
            "have lyrics or were checked before (--lyrics-rescan redoes "
            "them)."))
        return False

    wrote_synced = res.get("wrote-synced", 0) + res.get("dry:wrote-synced", 0)
    wrote_plain  = res.get("wrote-plain", 0) + res.get("dry:wrote-plain", 0)

    if summary["stopped"]:
        log.warning(fmt(C.YELLOW, "  ⚠  Lyrics pass stopped early."))
    elif summary["failures"]:
        log.warning(fmt(C.RED, "  ✗  Lyrics pass finished with errors."))
    else:
        log.info(fmt(C.GREEN, "  ✓  Lyrics pass complete."))
    verb = "Would write" if dry_run else "Wrote"
    skipped_part = (f" · {skipped} skipped (already checked)" if skipped else "")
    unfinished_part = (
        f" · {summary['unfinished']} left for the next run"
        if summary["unfinished"] else ""
    )
    checked_part = (
        f"{processed} of {plural(summary['candidate_total'], 'track')} checked"
        if summary["stopped"] else f"{plural(processed, 'track')} checked"
    )
    log.info(fmt(C.GRAY,
        f"     {checked_part} · {verb.lower()} "
        f"{wrote_synced} synced + {wrote_plain} plain · "
        f"{summary['already']} already had lyrics · "
        f"{summary['not_found']} no lyrics found"
        f"{skipped_part}{unfinished_part}."))
    if summary["missing_tags"]:
        log.info(fmt(C.YELLOW,
            f"     {plural(summary['missing_tags'], 'track')} skipped because "
            "artist or title tags are missing."))
    if summary["too_long"]:
        log.info(fmt(C.YELLOW,
            f"     {plural(summary['too_long'], 'track')} skipped because "
            "they are longer than 20 minutes."))
    if summary["policy_skipped"]:
        log.info(fmt(C.YELLOW,
            f"     {plural(summary['policy_skipped'], 'track')} skipped by "
            "the current Lyrics policy."))
    if summary["unsafe"]:
        log.warning(fmt(C.RED,
            f"     {plural(summary['unsafe'], 'track path')} refused as unsafe; "
            "check the library path and symlinks."))
    if summary["unavailable"]:
        log.warning(fmt(C.YELLOW,
            f"     {plural(summary['unavailable'], 'track')} couldn't reach a "
            "provider (rate-limited or down); re-run later to pick them up."))
    if summary["errors"]:
        log.warning(fmt(C.YELLOW,
            f"     {plural(summary['errors'], 'track')} hit an error while "
            "writing or fetching lyrics; see the log above."))
    if summary["other_errors"]:
        log.warning(fmt(C.YELLOW,
            f"     {plural(summary['other_errors'], 'track')} returned an "
            "unexpected result; see the log above."))
    return bool(summary["stopped"] or summary["failures"])
