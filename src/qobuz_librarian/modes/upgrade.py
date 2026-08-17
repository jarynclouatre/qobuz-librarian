"""Upgrade walk mode: review saved baseline quality upgrade candidates."""
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.api.search import get_album
from qobuz_librarian.download_result import download_attention_kind
from qobuz_librarian.library import (
    candidate_premise,
    downsample_state,
    generation_state,
    library_scan_state,
    new_releases,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.catalog import (
    find_album_dir_filesystem,
)
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.modes.process import process_album
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.quality.decision import (
    load_capped,
    mark_album_capped,
)
from qobuz_librarian.ui_cli.colors import C, banner, block, fmt, truncate
from qobuz_librarian.ui_cli.errors import EXIT_GENERAL, plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm
from qobuz_librarian.web import review_badges

# process_album outcomes that mean "nothing to upgrade here", not a failure.
BENIGN_UPGRADE_RESULTS = frozenset({
    "upgrade_only_no_op",
    "skipped_already_higher_quality",
    "skipped_has_extras",
    "lossy_only",
    "no_tracks",
    "user_skipped",
    "dry_run",
    "cancelled",
})


def _artist_dir_from_upgrade_result(album, result):
    result_dir = (result or {}).get("dir")
    if result_dir:
        album_dir = result_dir
        if not hasattr(album_dir, "exists"):
            from pathlib import Path
            album_dir = Path(album_dir)
        if album_dir.exists():
            return album_dir.parent if album_dir.is_dir() else album_dir.parent.parent
    try:
        clear_scan_caches()
        album_dir = find_album_dir_filesystem(album)
        if album_dir and album_dir.exists():
            return album_dir.parent
    except Exception as exc:
        vlog(f"post-upgrade state refresh path lookup failed: {exc}")
    return None


def _refresh_saved_state_after_upgrade(album, result, token, args):
    authority = generation_state.load()
    if (
        generation_state.output_is_current("library", state=authority)
        and not library_scan_state.remove_album((album or {}).get("id"))
    ):
        generation_state.mark_output_status(
            "library",
            "stale",
            reason="A CLI replacement could not update the saved Library view.",
        )
    if not new_releases.note_owned_album(album):
        if generation_state.output_is_current(
            "new_releases", state=authority
        ):
            generation_state.mark_output_status(
                "new_releases",
                "stale",
                reason="A CLI replacement could not update New Releases.",
            )
    artist_dir = _artist_dir_from_upgrade_result(album, result)
    if artist_dir is None:
        surfaces = [
            surface
            for surface in ("upgrade", "downsample")
            if generation_state.output_is_current(surface, state=authority)
        ]
        if surfaces:
            generation_state.invalidate(
                surfaces,
                "An album was replaced from the terminal, and its artist "
                "folder could not be identified.",
            )
        return
    hidden = hidden_mod.load()
    upgrade_result = upgrade_state.update_artist(
        artist_dir,
        token=token,
        args=args,
        capped=load_capped(),
    )
    if not upgrade_result.complete:
        generation_state.mark_output_status(
            "upgrade",
            "stale",
            reason="Upgrade could not refresh after a CLI replacement.",
        )
    upgrade_saved = upgrade_state.load()
    review_badges.set_ready(
        "upgrade",
        upgrade_state.has_visible_candidates(upgrade_saved, hidden),
    )
    downsample_result = downsample_state.update_artist(artist_dir)
    if not downsample_result.complete:
        generation_state.mark_output_status(
            "downsample",
            "stale",
            reason="Downsample could not refresh after a CLI replacement.",
        )
    downsample_saved = downsample_state.load()
    review_badges.set_ready(
        "downsample",
        downsample_state.has_visible_candidates(downsample_saved, hidden),
    )


def run_upgrade_walk_mode(args, token):
    """Upgrade walk over saved Library/baseline upgrade candidates.

    The Library refresh is the source of truth for upgrade discovery. The CLI
    keeps its walk value by grouping saved candidates by artist, but it no
    longer performs a separate live upgrade scan.

    Returns the exit code: 0 when the walk finished cleanly, non-zero when it
    was cut short or approved replacement work failed. The interactive menu
    ignores it; --upgrade-walk exits with it.
    """
    if not cfg.UPGRADE_SCAN_ENABLED:
        log.info(fmt(C.YELLOW, "  Upgrade scanning is turned off."))
        return 0
    clear_scan_caches()
    banner("Upgrade walk: saved Library candidates")

    saved_state = upgrade_state.load()
    authority = generation_state.load()
    generation = int(authority.get("generation") or 0)
    output = generation_state.output_state("upgrade", authority)
    saved_current = bool(
        generation > 0
        and saved_state.get("complete")
        and int(saved_state.get("generation") or 0) == generation
        and int(saved_state.get("revision") or 0)
        == int(output.get("revision") or 0)
        and output.get("status") == "current"
        and output.get("complete")
        and str(saved_state.get("quality_signature") or "")
        == upgrade_state.quality_signature()
    )
    if not saved_current:
        log.warning(fmt(C.YELLOW,
            "  Saved Upgrade results are missing, incomplete, or stale. "
            "Run a Library refresh first."))
        return EXIT_GENERAL
    saved = [
        c for c in upgrade_state.visible_candidates(
            saved_state, hidden_mod.load())
        if (c.get("payload") or {}).get("album_id")
    ]
    if not saved:
        log.info(fmt(C.YELLOW,
            "  No saved upgrade candidates. Run a Library refresh first."))
        return 0

    # A saved candidate is a record of a past scan, not a fact about the
    # library now. Set aside anything whose album folder has gone since,
    # instead of presenting it (and calling it high-confidence) as if it were
    # still there. Candidates from older scans don't carry the folder, and a
    # missing music root proves nothing about any of them; both present as
    # before.
    n_stale_candidates = 0
    if Path(cfg.MUSIC_ROOT).is_dir():
        still = []
        for c in saved:
            d = (c.get("payload") or {}).get("album_dir")
            if d and not Path(d).is_dir():
                n_stale_candidates += 1
                vlog(f"  gone from disk: {c.get('artist') or '?'}: "
                     f"{c.get('title') or '?'}")
            else:
                still.append(c)
        saved = still
    if n_stale_candidates:
        log.info(fmt(C.YELLOW,
            f"  {plural(n_stale_candidates, 'saved candidate')} no longer in "
            "the library. "
            "Run a Library refresh to clear them."))
    if not saved:
        log.info(fmt(C.YELLOW,
            "  Nothing left to review. Run a Library refresh first."))
        return 0

    by_artist: dict[str, list[dict]] = {}
    for cand in saved:
        artist = cand.get("artist") or "Unknown artist"
        by_artist.setdefault(artist, []).append(cand)

    # Consolidation prompts per-album would be unbearably noisy at scale.
    saved_consolidate = args.consolidate
    args.consolidate = False

    n = len(by_artist)
    n_reviewed = 0
    n_upgraded_albums = 0
    n_unverified_albums = 0
    n_catalogue_failed = 0
    n_failed_albums = 0
    n_attention_albums = 0
    n_gone_albums = 0
    n_stale_albums = 0
    no_answer = False
    interrupted = False
    unsafe_artists = []  # --auto-safe skipped artists, for end-of-run review

    log.info(fmt(C.GRAY,
        f"  {plural(len(saved), 'upgrade candidate')} across "
        f"{plural(n, 'artist')} from the saved Library refresh."))
    log.info(fmt(C.GRAY, "  Ctrl-C to stop at any point."))

    # Auto-accept-all gate. Skipped under --dry-run (which changes nothing), so
    # a preview run doesn't prompt to "run unattended", matching downsample.py.
    auto_accept_all = False
    try:
        if (not args.yes and not getattr(args, "auto_safe", False)
                and not getattr(args, "dry_run", False)):
            try:
                _r = input(fmt(C.CYAN,
                    "\n  Auto-accept all upgrades and run unattended? [y/N]: "
                )).strip().lower()
            except EOFError:
                _r = ""
            if _r in ("y", "yes"):
                auto_accept_all = True
                log.info(fmt(C.GREEN,
                    "  ✓ Auto-accepting every artist. Walk away."))
        log.info("")

        for artist_name, candidates in by_artist.items():
            n_reviewed += 1
            log.info("")
            log.info("")
            n_albums = len(candidates)

            log.info(fmt(C.BOLD + C.WHITE, f"  {artist_name}"))
            log.info(fmt(C.MAGENTA,
                f"  {plural(n_albums, 'album')} to upgrade:"))
            log.info("")
            for c in candidates:
                payload = c.get("payload") or {}
                title = truncate(c.get("title") or "?", 48)
                year = payload.get("year") or "?"
                detail = c.get("detail") or "higher Qobuz quality"
                log.info(f"    • {fmt(C.WHITE, title)} "
                         f"{fmt(C.GRAY, f'({year})')}   "
                         f"{fmt(C.MAGENTA, detail)}")
            log.info("")

            # --auto-safe is the fully unattended path.
            if getattr(args, "auto_safe", False):
                low_conf = []
                for _c in candidates:
                    _reasons = []
                    payload = _c.get("payload") or {}
                    if payload.get("needed_edition_swap"):
                        _reasons.append("edition was auto-swapped")
                    _sim = payload.get("title_similarity")
                    if _sim is None:
                        _reasons.append("confidence data missing")
                    elif float(_sim) < cfg.AUTO_SAFE_TITLE_SIM_THRESH:
                        _reasons.append(
                            f"title similarity {float(_sim):.2f} < "
                            f"{cfg.AUTO_SAFE_TITLE_SIM_THRESH:.2f}")
                    if _reasons:
                        low_conf.append(
                            (_c.get("title") or "?", _reasons))
                if low_conf:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  --auto-safe: skipping {artist_name} "
                        f"({len(low_conf)}/{len(candidates)} low-confidence)."))
                    for _t, _rs in low_conf:
                        log.info(fmt(C.GRAY,
                            f"     · {truncate(_t, 50)}: {'; '.join(_rs)}"))
                    unsafe_artists.append((artist_name, low_conf))
                    log.info("")
                    continue
                log.info(fmt(C.GREEN,
                    f"  ✓  --auto-safe: all {n_albums} candidate(s) high-confidence."))
            else:
                _flush_stdin()
                # on_eof=None so a closed stdin is distinguishable from a No:
                # the walk would otherwise skip every artist and call that a
                # success, the same lie the downsample walk told.
                answer = confirm(f"  Upgrade {plural(n_albums, 'album')}?",
                                 default_yes=False,
                                 auto_yes=args.yes or auto_accept_all,
                                 on_eof=None)
                if answer is None:
                    no_answer = True
                    break
                if not answer:
                    log.info(fmt(C.GRAY, "  Skipped."))
                    log.info("")
                    continue

            for j, c in enumerate(candidates, 1):
                payload = c.get("payload") or {}
                album_id = payload.get("album_id")
                label = f"[{j}/{n_albums}]"
                log.info("")
                log.info(fmt(C.BOLD + C.WHITE,
                    f"  {label} {truncate(c.get('title') or '?', 55)}"))
                try:
                    premise = candidate_premise.validate(c)
                except candidate_premise.CandidateStale:
                    log.info(fmt(
                        C.YELLOW,
                        "  The local album changed after the Library refresh. "
                        "It was left unchanged; refresh Library before "
                        "trying this Upgrade again.",
                    ))
                    n_stale_albums += 1
                    continue
                try:
                    album = get_album(album_id, token)
                    if not album:
                        log.info(fmt(C.YELLOW,
                            f"  Album {album_id} is no longer on Qobuz; skipping."))
                        n_failed_albums += 1
                        continue
                    # Upgrade_only=True so partial-album cases
                    # only re-rip the present tracks, not download missing ones.
                    _proc_result = process_album(album, args, allow_force=False,
                                  label=label, already_confirmed=True,
                                  upgrade_only=True,
                                  token=token,
                                  expected_album_receipt=premise["receipt"])
                except AuthLost:
                    raise

                _pr_result = (_proc_result or {}).get("result", "")
                attention_kind = download_attention_kind(_proc_result or {})
                if _pr_result == "upgrade_no_local_tracks":
                    # The album left the library after the scan (an older
                    # saved state without folder records, or the folder is
                    # there but its tracks are not). Not benign: it must show
                    # in the closing summary, and the saved state is left for
                    # a Library refresh to clear.
                    n_gone_albums += 1
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                if (_proc_result or {}).get("catalogue_unverified", False):
                    n_catalogue_failed += 1
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                if attention_kind == "backup":
                    # Keep the backup visible until the replacement or
                    # automatic restore can be verified.
                    n_unverified_albums += 1
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                if _pr_result in BENIGN_UPGRADE_RESULTS:
                    if _pr_result not in {"cancelled", "dry_run"}:
                        _refresh_saved_state_after_upgrade(
                            album, _proc_result, token, args)
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                if not (_proc_result or {}).get("imported", False):
                    # Attempted (Qobuz had a higher-quality copy) but didn't
                    # land: backup failed, download failed, or import failed.
                    n_failed_albums += 1
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                verdict = (_proc_result or {}).get("quality_verdict")
                if verdict and verdict["under"] and not verdict["recovered"]:
                    mark_album_capped(album.get("id"), album, {
                        "n_below": verdict["n_below"],
                        "n_at": 0,
                        "n_above": 0,
                    })
                    log.info(fmt(C.YELLOW,
                        f"     ⚠  Upgrade incomplete: "
                        f"{verdict['n_below']} track(s) still below target "
                        f"after retry. Marked capped."))
                elif attention_kind == "processing":
                    n_attention_albums += 1
                    n_upgraded_albums += 1
                    _refresh_saved_state_after_upgrade(
                        album, _proc_result, token, args)
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                n_upgraded_albums += 1
                _refresh_saved_state_after_upgrade(album, _proc_result, token, args)
                time.sleep(cfg.ARTIST_API_DELAY)

            log.info("")
    except KeyboardInterrupt:
        interrupted = True
        log.info("")
        log.info(fmt(C.GRAY, "  Interrupted. Stopping upgrade walk."))
    finally:
        args.consolidate = saved_consolidate

    n_failed_attempts = (n_failed_albums + n_unverified_albums
                         + n_attention_albums
                         + n_catalogue_failed + n_gone_albums
                         + n_stale_albums)
    n_gone = n_stale_candidates + n_gone_albums

    log.info("")
    if interrupted:
        log.warning(fmt(C.YELLOW, "  ⚠  Upgrade walk stopped early."))
    elif no_answer:
        log.warning(block(fmt(C.YELLOW,
            "  ✗  The walk stopped: each artist needs a confirmation and "
            "there is no terminal to give one. Re-run with --yes (or "
            "--auto-safe) to accept unattended.")))
    elif n_failed_attempts:
        log.warning(fmt(C.RED, "  ✗  Upgrade walk finished with errors."))
    elif n_upgraded_albums or not n_gone:
        log.info(fmt(C.GREEN, "  ✓  Upgrade walk complete."))
    else:
        log.info(fmt(C.YELLOW, "  ⚠  Upgrade walk finished; nothing was "
                               "upgraded."))
    log.info(fmt(C.GRAY,
        f"     Reviewed {plural(n_reviewed, 'artist')}; upgraded tracks in "
        f"{plural(n_upgraded_albums, 'album')}."))
    if n_gone:
        log.info(fmt(C.YELLOW,
            f"     ⚠  {plural(n_gone, 'candidate')} no longer in the library. "
            "Run a Library refresh to clear them."))
    if n_stale_albums:
        log.info(fmt(
            C.YELLOW,
            f"     ⚠  {plural(n_stale_albums, 'candidate')} changed after "
            "the saved review. Refresh Library before retrying.",
        ))
    if n_catalogue_failed:
        log.info(fmt(C.YELLOW,
            f"     ⚠  {plural(n_catalogue_failed, 'replacement')} needs "
            "Beets catalogue attention (backup retained)."))
    if n_unverified_albums:
        log.info(fmt(C.YELLOW,
            f"     ⚠  {plural(n_unverified_albums, 'album')} kept a safety "
            "backup because the replacement or recovery couldn't be verified."))
    if n_attention_albums:
        log.info(fmt(C.YELLOW,
            f"     ⚠  {plural(n_attention_albums, 'album')} did not finish "
            "cleanly (see the log above)."))
    if n_failed_albums:
        log.info(fmt(C.YELLOW,
            f"     ⚠  {plural(n_failed_albums, 'album')} couldn't be upgraded "
            "(see the log above)."))
    if unsafe_artists:
        log.info("")
        log.info(fmt(C.YELLOW + C.BOLD,
            f"  ⚠  {plural(len(unsafe_artists), 'artist')} skipped for manual review:"))
        for _a, _lc in unsafe_artists:
            log.info(fmt(C.WHITE, f"     {_a}"))
            for _t, _rs in _lc:
                log.info(fmt(C.GRAY,
                    f"       · {truncate(_t, 50)}: {'; '.join(_rs)}"))
        log.info(fmt(C.GRAY,
            "     Re-run without --auto-safe to review these interactively."))
    return EXIT_GENERAL if interrupted or no_answer or n_failed_attempts else 0
