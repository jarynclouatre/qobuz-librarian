"""CLI new-release check for additions to Qobuz since the last
check across all library artists.

Uses the same engine as the web dashboard's auto-check (the cheap one-call-per-
artist diff against the last-seen catalog ids), just printed to the terminal
instead of routed through a job's candidate list. Cancellable with Ctrl-C.
``--dry-run`` skips advancing the baseline so a user can preview without losing
the chance to see the same releases again on the next run.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    NoCredsError,
    QobuzAccess,
    QobuzUnavailable,
)
from qobuz_librarian.api.client import authorize_qobuz_action
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import new_releases as new_releases_mod
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_new_releases_for_artist,
    flush_resolve_cache,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_library_artists,
)
from qobuz_librarian.ui_cli.colors import C, banner, fmt, section
from qobuz_librarian.ui_cli.errors import EXIT_AUTH, EXIT_GENERAL, die, plural
from qobuz_librarian.ui_cli.logging import log


def run_check_new_releases_mode(args):
    """Walk library artists and report what's new on Qobuz since the last check.

    Mirrors the engine the web auto-check uses, so a run advances the same
    'seen' baseline and the dashboard's age line picks it up. First run on a
    library records the baseline and surfaces nothing, matching the web's
    first-touch contract.
    """
    banner("New releases: what Qobuz has added since your last check")

    if not new_releases_mod.is_baseline_complete():
        # Same shape as the upgrade walk's refusal: the banner and the reason
        # belong on one stream, so they stay in order when the run is piped.
        log.warning(fmt(C.YELLOW,
            "  No new-release baseline exists yet. Run a Library refresh "
            "first."))
        return EXIT_GENERAL

    try:
        token = authorize_qobuz_action(
            QobuzAccess.CATALOGUE_ACTION
        ).token
    except NoCredsError:
        die(fmt(C.RED,
            "  ✗  No Qobuz credentials configured.\n"
            f"     Paste your user_auth_token on the Settings page "
            f"(http://<host>:{cfg.WEB_PUBLIC_PORT}/settings)\n"
            "     or set QOBUZ_USER_AUTH_TOKEN in your environment.\n"),
            EXIT_AUTH)

    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        log.info(fmt(C.YELLOW, "  ⚠  No artist folders found under MUSIC_ROOT."))
        return 0

    state = new_releases_mod.load()
    seen = state.get("seen") or {}
    hidden = hidden_mod.load()
    # Single-track marks only suppress artists when the setting is on. The
    # same gate the web check applies, so the two report the same artists.
    single_store = hidden if cfg.SUPPRESS_SINGLE_TRACK_GAPS else None
    # A grown catalog cap means the old baseline is missing everything past
    # the previous limit; re-baseline instead of dumping that slice as "new"
    # (mirrors the web check's guard).
    cur_limit = int(cfg.ARTIST_CATALOG_LIMIT)
    prev_limit = state.get("baseline_limit")
    rebaseline = prev_limit is None or cur_limit > int(prev_limit)
    opts = DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES)

    section(f"New-release check: {plural(len(artists), 'artist')}")
    if not seen:
        log.info(fmt(C.GRAY,
            "  First check on this library: the current catalogue is the "
            "starting baseline, so nothing surfaces until a later check."))

    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    current_seen = {}
    total_new = 0
    artists_with_news = 0
    failed_count = 0
    # Folders Qobuz has no artist for. A settled answer, so they neither hold
    # the baseline back nor earn the "run it again" advice below.
    unresolved_names = []

    # A context manager would wait for running calls after cancellation.
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="newrel")
    try:
        futures = {ex.submit(find_new_releases_for_artist, ad.name,
                             token=token, opts=opts, seen_by_id=seen,
                             hidden=hidden, single_store=single_store,
                             artist_dir=ad, baseline_only=rebaseline): ad
                   for ad in artists}
        for fut in as_completed(futures):
            ad = futures[fut]
            try:
                result = fut.result()
            except (AuthLost, QobuzUnavailable):
                raise
            except Exception as e:
                log.info(fmt(C.GRAY, f"    skipped {ad.name}: {e}"))
                failed_count += 1
                continue
            if result.artist_id and not getattr(result, "fetch_failed", False):
                current_seen[result.artist_id] = result.current_ids
            elif getattr(result, "unresolved", False):
                unresolved_names.append(ad.name)
            else:
                failed_count += 1
            if result.new_gaps:
                artists_with_news += 1
                log.info(fmt(C.GREEN,
                    f"  {result.artist_name}: "
                    f"{plural(len(result.new_gaps), 'new release')}"))
                for gap in result.new_gaps:
                    a = gap.qobuz_album
                    title = a.get("title") or "?"
                    year = (str(a.get("release_date_original") or "")[:4]
                            or "?")
                    log.info(fmt(C.WHITE, f"    • {title} ({year})"))
                total_new += len(result.new_gaps)
    except KeyboardInterrupt:
        log.info(fmt(C.YELLOW, "\n  ⚠  Cancelled; not recording this run."))
        raise
    finally:
        # wait=False: never block on in-flight calls.
        ex.shutdown(wait=False, cancel_futures=True)

    saved = None
    if not args.dry_run:
        # UNION each reached artist's snapshot into the prior baseline rather
        # than replacing it. An over-cap catalogue comes back as a different
        # slice each run, so a replace would let rotated-out ids re-surface as
        # "new" (the same merge the web flow does).
        merged = dict(seen)
        for aid, ids in current_seen.items():
            merged[aid] = sorted(set(merged.get(aid, [])) | set(ids))
        complete = failed_count == 0 and bool(current_seen)
        saved = new_releases_mod.mark_run(
            merged,
            complete=complete,
            baseline_limit=cur_limit if complete else None,
        )

    flush_resolve_cache()
    log.info("")
    skipped_note = new_releases_mod.unresolved_note(unresolved_names)
    if skipped_note:
        log.info(fmt(C.GRAY, f"  · {skipped_note}"))
    if rebaseline and seen:
        if args.dry_run:
            text = "  · Catalogue rebaseline previewed; no changes saved."
            if failed_count:
                text = ("  · Catalogue rebaseline preview incomplete; "
                        f"{plural(failed_count, 'artist')} couldn't be checked; "
                        "no changes saved.")
            log.info(fmt(C.YELLOW if failed_count else C.GRAY, text))
        elif saved is False:
            text = "  ⚠  The fresh catalogue baseline couldn't be saved."
            if failed_count:
                text += (f" {plural(failed_count, 'artist')} also couldn't be "
                         "checked.")
            log.info(fmt(C.YELLOW, text + " Run the check again."))
        elif failed_count:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Catalogue rebaseline incomplete; "
                f"{plural(failed_count, 'artist')} couldn't be checked; "
                "run the check again."))
        else:
            log.info(fmt(C.GRAY,
                "  · Catalogue limit changed; recorded a fresh baseline; "
                "future checks diff against it."))
    elif total_new:
        log.info(fmt(C.BOLD + C.WHITE,
            f"  ✓  {plural(total_new, 'new release')} across "
            f"{plural(artists_with_news, 'artist')}."))
        log.info(fmt(C.GRAY,
            "  Run the web library scan or the per-artist scan to queue them."))
        if failed_count:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {plural(failed_count, 'artist')} couldn't be checked; "
                "run the check again for a complete result."))
        if args.dry_run:
            log.info(fmt(C.GRAY, "  Dry run; no changes saved."))
        elif saved is False:
            log.info(fmt(C.YELLOW,
                "  ⚠  The updated baseline couldn't be saved; these releases "
                "may appear again."))
    elif failed_count:
        log.info(fmt(C.YELLOW,
            "  · No new releases from the artists that could be checked. "
            f"{plural(failed_count, 'artist')} couldn't be checked; run again."))
        if args.dry_run:
            log.info(fmt(C.GRAY, "  Dry run; no changes saved."))
        elif saved is False:
            log.info(fmt(C.YELLOW,
                "  ⚠  The partial baseline update couldn't be saved."))
    elif seen:
        if args.dry_run:
            log.info(fmt(C.GRAY,
                "  · Nothing new found in this preview; no changes saved."))
        elif saved is False:
            log.info(fmt(C.YELLOW,
                "  ⚠  Nothing new found, but the updated baseline couldn't be "
                "saved. Run the check again."))
        else:
            log.info(fmt(C.GRAY, "  · Nothing new found."))
    elif args.dry_run:
        log.info(fmt(C.GRAY, "  · Baseline preview complete; no changes saved."))
    elif saved is False:
        log.info(fmt(C.YELLOW,
            "  ⚠  The baseline couldn't be saved. Run the check again."))
    else:
        log.info(fmt(C.GRAY, "  · Baseline recorded."))

    exit_code = EXIT_GENERAL if failed_count or saved is False else 0
    if exit_code:
        log.warning(fmt(
            C.YELLOW,
            "  ⚠  New-release check incomplete; review the details above "
            "and run it again.",
        ))
    return exit_code
