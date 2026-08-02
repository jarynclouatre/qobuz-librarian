"""Lyrics backfill walk — fetch lyrics for tracks already in the library.

Local apart from the provider HTTP, so it runs without a Qobuz login. Re-runs
are cheap (already-lyriced tracks are skipped from the state file) and safe to
interrupt — progress is checkpointed as it goes.
"""
from qobuz_librarian import config as cfg
from qobuz_librarian.library.lyrics import HAVE_LYRICS, run_library_lyrics
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.ui_cli.colors import C, banner, fmt
from qobuz_librarian.ui_cli.errors import EXIT_CONFIG, EXIT_GENERAL, die, plural
from qobuz_librarian.ui_cli.logging import log


def run_library_lyrics_mode(args):
    """Returns the exit code: 0 when the pass finished, non-zero when it was
    cut short. The interactive menu ignores it; --lyrics-walk exits with it."""
    clear_scan_caches()
    banner("Lyrics — fetch lyrics for tracks already in your library")

    if not HAVE_LYRICS:
        # log.warning (not log.info) so an unattended `--quiet --lyrics-walk`
        # cron run still surfaces the missing dep instead of looking like a
        # silent success; die() with EXIT_CONFIG so the cron's exit-code
        # check notices too.
        log.warning(fmt(C.YELLOW,
            "  ⚠  Lyric fetching isn't available — the syncedlyrics provider "
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
    log.info(fmt(C.GRAY, "  Ctrl-C to stop — progress is saved.\n"))

    try:
        res = run_library_lyrics(
            dry_run=args.dry_run,
            rescan=args.lyrics_rescan,
            synced_only=args.lyrics_synced_only,
            log=log,
        )
    except KeyboardInterrupt:
        print()
        log.info(fmt(C.GRAY, "  Interrupted — what was done is saved; re-run to continue."))
        return EXIT_GENERAL

    _report_summary(res, dry_run=args.dry_run)
    return 0


def _report_summary(res, *, dry_run):
    total = res.get("total", 0)
    if not total:
        log.info(fmt(C.YELLOW, "  No FLAC files found in the library."))
        return

    wrote_synced = res.get("wrote-synced", 0) + res.get("dry:wrote-synced", 0)
    wrote_plain  = res.get("wrote-plain", 0) + res.get("dry:wrote-plain", 0)
    already      = (res.get("already-synced", 0) + res.get("already-plain", 0)
                    + res.get("kept-existing-plain", 0))
    not_found    = res.get("not-found", 0)
    unavailable  = res.get("providers-unavailable", 0)
    errors       = (res.get("write-error", 0) + res.get("exception", 0)
                    + res.get("error", 0))

    print()
    log.info(fmt(C.GREEN, "  ✓  Lyrics pass complete."))
    verb = "Would write" if dry_run else "Wrote"
    log.info(fmt(C.GRAY,
        f"     {plural(total, 'track')} scanned · {verb.lower()} "
        f"{wrote_synced} synced + {wrote_plain} plain · "
        f"{already} already had lyrics · {not_found} not found."))
    if unavailable:
        log.info(fmt(C.YELLOW,
            f"     {plural(unavailable, 'track')} couldn't reach a provider "
            "(rate-limited or down) — re-run later to pick them up."))
    if errors:
        log.info(fmt(C.YELLOW,
            f"     {plural(errors, 'track')} hit an error while writing or "
            "fetching lyrics — see the log above."))
