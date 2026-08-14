"""Downsample walk: shrink hi-res library files to CD rate.

Pure local housekeeping: no Qobuz lookup, so it runs without credentials. The
resample is irreversible (the hi-res original is overwritten in place, with no
re-download fallback), so the walk confirms per artist with default NO and each
file is decode-verified before it replaces anything.
"""
import sys

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE, downsample_dir
from qobuz_librarian.library import downsample_state, generation_state
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.scanner import clear_scan_caches, list_library_artists
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.quality.decision import mark_local_album_capped
from qobuz_librarian.ui_cli.colors import C, banner, block, fmt, format_size, truncate
from qobuz_librarian.ui_cli.errors import EXIT_CONFIG, EXIT_GENERAL, plural
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm
from qobuz_librarian.web import review_badges


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
    banner("Downsample: bring hi-res library files down to CD rate")

    if not HAVE_DOWNSAMPLE:
        log.info(fmt(C.YELLOW,
            "  ⚠  Downsampling isn't available (needs ffmpeg and flac)."))
        return EXIT_CONFIG

    all_artists = list_library_artists()
    if not all_artists:
        log.info(fmt(C.YELLOW, "  ⚠  No artist directories found."))
        return 0

    keep_originals = cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep"
    if cfg.DOWNSAMPLE_KEEP_ORIGINALS == "keep":
        log.info(fmt(C.YELLOW,
            "  ⚠  This rewrites hi-res files in place to 44.1/48kHz. A "
            "restorable copy of each original is kept for a while."))
    elif cfg.DOWNSAMPLE_KEEP_ORIGINALS == "delete":
        log.info(fmt(C.YELLOW,
            "  ⚠  This rewrites hi-res files in place to 44.1/48kHz. The "
            "originals are not kept and there is no undo."))
    else:
        log.info(fmt(C.YELLOW,
            "  ⚠  This rewrites hi-res files in place to 44.1/48kHz. You'll "
            "choose first whether to keep the hi-res originals."))
    if args.dry_run:
        log.info(fmt(C.GRAY, "  --dry-run: listing candidates only, nothing is changed."))
    log.info(fmt(C.GRAY, "  Ctrl-C to stop at any point."))

    hidden = hidden_mod.load()

    def _on_artist(artist_dir, _candidates, error, done, total):
        _scan_line = f"  [{done}/{total}] Scanning {truncate(artist_dir.name, 46)}…"
        if sys.stdout.isatty():
            print(f"\r{_scan_line:<80}", end="", flush=True)
        if error is not None:
            log.info("")
            log.info(fmt(C.YELLOW,
                f"  Skipped {artist_dir.name}: {error}"))

    refresh = downsample_state.refresh_for_artists(
        all_artists,
        hidden=hidden,
        on_artist=_on_artist,
        persist=not args.dry_run,
    )
    unchecked = len(refresh.errors)
    candidates_by_artist = {}
    for candidate in refresh.candidates:
        candidates_by_artist.setdefault(candidate.artist, []).append(candidate)

    # First downsample with the keep-vs-delete choice unmade: ask once. The
    # web UI asks the same thing and saves it as the standing default
    # (changeable later in Settings).
    if (refresh.candidates and not args.dry_run
            and cfg.DOWNSAMPLE_KEEP_ORIGINALS not in ("keep", "delete")):
        from qobuz_librarian.web import settings_store
        _flush_stdin()
        keep = confirm(
            "  Keep a restorable backup of the hi-res originals? "
            "(answering No deletes them to save space)",
            default_yes=True, auto_yes=False, on_eof=None, strict=True)
        if keep is None:
            # Closed stdin never answered.
            keep_originals = True
            log.info(fmt(C.GRAY,
                "  No input available; keeping originals for this run. "
                "set the preference in Settings."))
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
                f"  Saved. Originals will be {'kept' if keep else 'deleted'}; "
                "change this any time in Settings."))
        log.info("")

    # Auto-accept gate. Skipped under --yes, which has already answered it;
    # otherwise an unattended run stalls here and then declines every artist.
    auto_accept_all = False
    if refresh.candidates and not args.dry_run and not args.yes:
        try:
            _r = input(fmt(C.CYAN,
                "\n  Auto-accept every artist and run unattended? [y/N]: "
            )).strip().lower()
        except EOFError:
            _r = ""
        if _r in ("y", "yes"):
            auto_accept_all = True
            log.info(fmt(C.GREEN, "  ✓ Auto-accepting every artist. Walk away."))
    log.info("")

    n_scanned = len(refresh.artists_scanned) - unchecked
    n_albums_done = 0
    total_saved = 0
    total_errors = 0
    total_flush_warns = 0
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
            log.info(fmt(C.BOLD + C.WHITE, f"  {artist_name}"))
            log.info(fmt(C.MAGENTA,
                f"  {plural(n_albums, 'album')} above CD rate, "
                f"~{format_size(est_total)} reclaimable:"))
            log.info("")
            for c in candidates:
                log.info(f"    • {fmt(C.WHITE, truncate(c.title, 48))}   "
                         f"{fmt(C.GRAY, c.detail)}")
            log.info("")

            if args.dry_run:
                continue

            _flush_stdin()
            # on_eof=None so a closed stdin is distinguishable from a No: the
            # walk would otherwise skip every artist and call that a success.
            answer = confirm(f"  Downsample {plural(n_albums, 'album')}?",
                             default_yes=False,
                             auto_yes=args.yes or auto_accept_all, on_eof=None)
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
                    f"  [{j}/{n_albums}] {truncate(c.title, 55)}"))
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
                res = downsample_dir(c.album_dir, verbose=True,
                                     base_dir=c.album_dir, log=log.info,
                                     keep_originals=keep_originals)
                if res.get("resampled"):
                    n_albums_done += 1
                total_saved += res.get("saved_bytes", 0)
                total_errors += res.get("errors", 0)
                total_flush_warns += res.get("flush_warnings", 0)
            if artist_attempted:
                downsample_state.update_artist(artist_dir, hidden=hidden)
                saved_state = downsample_state.load()
                review_badges.set_ready(
                    "downsample",
                    downsample_state.has_visible_candidates(saved_state, hidden),
                )
            log.info("")
    except KeyboardInterrupt:
        log.info("")
        log.info(fmt(C.GRAY, "  Interrupted."))
        interrupted = True

    log.info("")
    if no_answer:
        # Input can also run dry mid-walk (a piped "y\n"), so only claim
        # nothing happened when nothing did.
        done_part = ("Nothing was downsampled" if not n_albums_done else
                     "The walk stopped early")
        log.warning(block(fmt(C.YELLOW,
            f"  ✗  {done_part}: each artist needs a confirmation and "
            "there is no terminal left to give one. Re-run with --yes to "
            f"accept all {plural(len(candidates_by_artist), 'artist')} "
            "unattended.")))
    elif interrupted:
        log.warning(fmt(C.YELLOW, "  ⚠  Downsample walk stopped early."))
    elif unchecked:
        log.warning(block(fmt(C.YELLOW,
            f"  ✗  Downsample walk incomplete: {plural(unchecked, 'artist')} "
            "couldn't be checked. Re-run to retry.")))
    elif total_errors:
        log.warning(fmt(C.RED,
            "  ✗  Downsample walk finished with errors. Re-run to retry the "
            "files left unchanged."))
    elif not total_flush_warns:
        log.info(fmt(C.GREEN, "  ✓  Downsample walk complete."))
    log.info(fmt(C.GRAY,
        f"     Checked {plural(n_scanned, 'artist')}; downsampled "
        f"{plural(n_albums_done, 'album')}, reclaimed {format_size(total_saved)}."))
    if total_errors:
        log.info(fmt(C.YELLOW,
            f"     {plural(total_errors, 'file')} could not be downsampled "
            "(left unchanged)."))
    if total_flush_warns:
        log.warning(block(fmt(C.YELLOW,
            f"  ⚠  Downsample walk needs attention: "
            f"{plural(total_flush_warns, 'file')} resampled but couldn't be "
            "flushed to disk. The swap may not survive a power loss; check "
            "the drive.")))
    if state_refresh_warnings:
        log.warning(fmt(
            C.YELLOW,
            "  Files were changed, but the saved Upgrade view needs a "
            "Library refresh.",
        ))
    return EXIT_GENERAL if (no_answer or interrupted or unchecked
                            or total_errors or total_flush_warns
                            or state_refresh_warnings) else 0
