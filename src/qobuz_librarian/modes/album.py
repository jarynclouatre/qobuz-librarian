"""Album mode for a single-album query, selection, and download."""
import sys

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    Aborted,
    AuthLost,
    CatalogMiss,
    QobuzError,
    QobuzUnavailable,
    friendly_qobuz_error,
)
from qobuz_librarian.api.search import get_album, search_albums
from qobuz_librarian.cli import parse_qobuz_url
from qobuz_librarian.integrations.beets import staging_preflight
from qobuz_librarian.library.candidate_premise import CandidateStale
from qobuz_librarian.library.catalog import (
    compute_missing,
    find_existing_tracks,
    is_lossless_album,
)
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.modes.process import process_album
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.durable_album import plan_durable_new_album
from qobuz_librarian.queue.executor import (
    _admit_new_queue_items,
    _execute_download_queue,
    _refresh_review_state_after_downloads,
)
from qobuz_librarian.queue.persistence import (
    clear_pending_queue,
    save_pending_queue,
)
from qobuz_librarian.ui_cli.colors import C, banner, fmt
from qobuz_librarian.ui_cli.errors import EXIT_AUTH, EXIT_GENERAL, auth_lost_msg, die
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import (
    confirm,
    interactive_query,
    print_album_summary,
    prompt_album_selection,
)
from qobuz_librarian.ui_cli.sentinels import MORE, URL_QUERY

_DIRECT_QUEUE_MODE = "album-now"
_SETTLED_ONE_SHOT_RESULTS = {
    "already_complete",
    "downloaded",
    "dry_run",
    "skipped_already_higher_quality",
    "user_skipped",
}


def _one_shot_exit_code(result):
    if not isinstance(result, dict):
        return EXIT_GENERAL
    verdict = result.get("quality_verdict") or {}
    if (
        result.get("attention")
        or result.get("upgrade_unverified")
        or result.get("catalogue_unverified")
        or result.get("recovery_unverified")
        or result.get("consolidation_interrupted")
        or (verdict.get("under") and not verdict.get("recovered"))
    ):
        return EXIT_GENERAL
    return (
        0
        if result.get("result") in _SETTLED_ONE_SHOT_RESULTS
        else EXIT_GENERAL
    )


def _download_album_now(
    album,
    args,
    token,
    *,
    existing_state=None,
    already_confirmed=False,
):
    """Use the durable lane for an eligible fresh album, else the legacy path."""
    def finish(result):
        if isinstance(result, dict):
            _refresh_review_state_after_downloads(
                [{**result, "album": album}],
                token,
                args,
            )
        return result

    if is_lossless_album(album):
        if existing_state is None:
            existing, album_dir = find_existing_tracks(album)
            tracks = (album.get("tracks") or {}).get("items") or []
            missing, present = compute_missing(tracks, existing)
        else:
            existing, album_dir, missing, present = existing_state
            tracks = (album.get("tracks") or {}).get("items") or []
        candidate = _build_queue_item(
            album=album,
            album_dir=album_dir,
            label=(
                f"{(album.get('artist') or {}).get('name') or '?'}"
                f" - {album.get('title') or '?'}"
            ),
            missing=(tracks if getattr(args, "force", False) else missing),
            present=present,
            upgrade_only=False,
            auto_upgrade=False,
        )
        if plan_durable_new_album(candidate, args) is not None:
            if not already_confirmed:
                print_album_summary(
                    album,
                    missing,
                    present,
                    album_dir,
                    bool(getattr(args, "force", False)),
                )
                if getattr(args, "dry_run", False):
                    log.info(fmt(
                        C.YELLOW,
                        "\n  --dry-run: stopping here, nothing downloaded.\n",
                    ))
                    return {"result": "dry_run", "n_missing": len(missing)}
                if not confirm(
                    f"\n  Proceed with downloading {len(missing)} track(s)?",
                    default_yes=False,
                    auto_yes=bool(getattr(args, "yes", False)),
                ):
                    log.info(fmt(C.GRAY, "  Skipped."))
                    return {"result": "user_skipped", "n_missing": len(missing)}
            try:
                token = _admit_new_queue_items([candidate], token)
            except CandidateStale as exc:
                log.info(fmt(C.YELLOW, f"  ⚠  {exc}"))
                return {"result": "stale_candidate"}
            staging_preflight(args)
            queue = [candidate]

            def _save_progress():
                save_pending_queue(queue, mode=_DIRECT_QUEUE_MODE)

            if not getattr(args, "dry_run", False):
                _save_progress()
            results, drained = _execute_download_queue(
                queue,
                args,
                token,
                on_progress=(
                    None if getattr(args, "dry_run", False)
                    else _save_progress
                ),
                consolidate_duplicates=False,
            )
            if not getattr(args, "dry_run", False):
                if drained:
                    clear_pending_queue()
                else:
                    log.info(fmt(
                        C.YELLOW,
                        "  ⚠  Safe recovery was saved. Restart without a "
                        "search argument and choose Resume before other work.",
                    ))
            return finish(results[0] if results else None)
    return finish(process_album(album, args, allow_force=True, token=token))


def resolve_album_from_args(args, token):
    """Album mode: resolve a single Qobuz album from CLI args or interactive prompt.
    Returns the album dict, or raises CatalogMiss/Aborted/AuthLost/QobuzError."""
    if args.query and "qobuz.com" in args.query[0]:
        parsed = parse_qobuz_url(args.query[0])
        if not parsed:
            die(fmt(C.RED,
                f"✗  Couldn't parse Qobuz URL: {args.query[0]}\n"
                f"   Supported formats:\n"
                f"     https://play.qobuz.com/album/<id>\n"
                f"     https://open.qobuz.com/album/<id>\n"
                f"     https://www.qobuz.com/<lang>/album/<slug>/<id>\n"),
                EXIT_GENERAL)
        kind, qid = parsed
        if kind == "track":
            from qobuz_librarian.cli import _compose_service_name
            _svc = _compose_service_name()
            die(fmt(C.RED,
                "✗  Track URL passed; Qobuz Librarian handles albums only.\n"
                "   For a single track use the bundled streamrip:\n"
                "     rip url <url>\n"
                f"   (in Docker: docker compose run --rm {_svc} "
                "rip url <url>)"),
                EXIT_GENERAL)
        log.info(fmt(C.GRAY, f"  ⟳  Fetching album {qid} …"))
        return get_album(qid, token)

    if len(args.query) >= 2:
        artist, album = args.query[0], " ".join(args.query[1:])
        query = f"{artist} {album}".strip()
    elif len(args.query) == 1:
        query = args.query[0].strip()
    else:
        sel = interactive_query()
        if sel is None:
            raise Aborted("user cancelled at album query")
        if sel[0] == URL_QUERY:
            parsed = parse_qobuz_url(sel[1])
            if parsed and parsed[0] == "track":
                # Recoverable, not fatal: in the menu loop this re-prompts (and
                # keeps any albums already queued) instead of die()ing the whole
                # session over one mistyped paste.
                raise CatalogMiss(
                    "That's a track URL. Paste the album URL instead, or use "
                    "`rip url <track-url>` for a single track.")
            if not parsed:
                raise CatalogMiss("Bad Qobuz URL. Paste an album URL.")
            return get_album(parsed[1], token)
        artist, album = sel
        query = f"{artist} {album}".strip()

    if not query:
        die(fmt(C.RED, "✗  Empty query."), EXIT_GENERAL)

    search_limit = cfg.SEARCH_LIMIT
    first_search = True
    while True:
        if not first_search:
            log.info(fmt(C.GRAY, f"  ⟳  Loading more results (up to {search_limit}) …"))
        results = search_albums(query, token, limit=search_limit)
        first_search = False
        if not results:
            raise CatalogMiss(f"No Qobuz match for: {query}")
        can_load_more = (len(results) >= search_limit and search_limit < 50)
        chosen = prompt_album_selection(results, prefer_hires=args.prefer_hires,
                                        can_load_more=can_load_more)
        if chosen is None:
            raise Aborted("user cancelled at album selection")
        if chosen == MORE:
            search_limit = min(search_limit * 2, 50)
            continue
        break
    return get_album(chosen["id"], token)


def _interactive_album_action(album, args, token, album_queue, flush_queue):
    """Show the summary and run the [d]/[q]/[f]/[s] prompt for one album."""
    try:
        existing, album_dir = find_existing_tracks(album)
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        missing, present = compute_missing(qobuz_tracks, existing)
        print_album_summary(album, missing, present, album_dir, args.force)

        if not missing and not args.force:
            log.info(fmt(C.GREEN, "  ✓  Already complete; nothing to download."))
            return
        if album_queue:
            log.info(fmt(C.GRAY,
                f"  ({len(album_queue)} album(s) already in queue; "
                "enter 'f' to download all)"))
        flush_opt = "  [f]lush queue" if album_queue else ""
        try:
            r = input(fmt(C.CYAN,
                f"  [d]ownload now (default)  [q]ueue for later"
                f"{flush_opt}  [s]kip: ")).strip().lower()
        except EOFError:
            r = "s"

        if r in ("q", "queue") or (r in ("f", "flush") and album_queue):
            # 'f'/'flush' is only shown when album_queue is non-empty; guard
            # here so an accidental 'f' on an empty queue doesn't silently
            # queue+flush without the user seeing the option advertised.
            #
            # --force means "re-download every track".
            _missing = (list((album.get("tracks") or {}).get("items") or [])
                        if args.force else missing)
            album_id = album.get("id")
            if any(qi["album"].get("id") == album_id for qi in album_queue):
                log.info(fmt(C.GRAY, "  (already in queue; skipping duplicate)"))
            else:
                candidate = _build_queue_item(
                    album=album,
                    album_dir=album_dir,
                    label=(f"{(album.get('artist') or {}).get('name') or '?'}"
                           f" - {album.get('title') or '?'}"),
                    missing=_missing,
                    present=present,
                    upgrade_only=False,
                    auto_upgrade=False,
                )
                try:
                    _admit_new_queue_items([candidate], token)
                except CandidateStale as exc:
                    log.info(fmt(C.YELLOW, f"  ⚠  {exc}"))
                    return
                album_queue.append(candidate)
            if r in ("f", "flush"):
                flush_queue()
            else:
                log.info(fmt(C.CYAN,
                    f"  ✓  Queued. ({len(album_queue)} album(s) in queue)"))
        elif r in ("s", "skip"):
            log.info(fmt(C.GRAY, "  Skipped."))
        else:
            try:
                _download_album_now(
                    album,
                    args,
                    token,
                    existing_state=(existing, album_dir, missing, present),
                    already_confirmed=True,
                )
            except AuthLost:
                die(fmt(C.RED, auth_lost_msg("mid-album")), EXIT_AUTH)
    except AuthLost:
        die(fmt(C.RED, auth_lost_msg("mid-album")), EXIT_AUTH)
    except QobuzUnavailable as e:
        log.info(fmt(C.YELLOW, f"\n⚠  Qobuz is temporarily unavailable: {e}\n"))
    except QobuzError as e:
        log.info(fmt(C.RED, f"\n✗  Qobuz API error: {friendly_qobuz_error(e)}.\n"))


def run_album_mode(args, token, *, query_args=None, loop=False):
    """One pass of album mode: resolve, then process_album.

    With loop=True (interactive menu), repeats until the user hits q/blank
    at the search prompt. Offers a [d]ownload / [q]ueue / [s]kip prompt so
    multiple albums can be accumulated and batch-downloaded together.
    CatalogMiss/QobuzError are non-fatal in loop mode so the user can
    immediately try again with a different query.
    """
    album_queue = []
    interrupted = False

    def _flush_queue():
        if not album_queue:
            return
        banner(f"Executing queue: {len(album_queue)} album(s)", C.GREEN)
        # _execute_download_queue drops finished items from album_queue in
        # place and leaves the unfinished ones for a retry, so DON'T clear it
        # What remains is exactly the work to re-offer.
        _, drained = _execute_download_queue(album_queue, args, token,
                                             refresh_review=True)
        if not args.dry_run and not drained:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {len(album_queue)} album(s) couldn't be downloaded; "
                f"re-run the command to try them again."))

    try:
        while True:
            clear_scan_caches()
            saved_query = args.query
            if query_args is not None:
                args.query = query_args
            try:
                album = resolve_album_from_args(args, token)
            except AuthLost:
                die(fmt(C.RED, auth_lost_msg("mid-album")), EXIT_AUTH)
            except CatalogMiss as e:
                (log.info if loop else log.warning)(
                    fmt(C.YELLOW, f"\n⚠  {e}\n")
                )
                args.query = saved_query
                if not loop:
                    return EXIT_GENERAL
                continue
            except QobuzUnavailable as e:
                args.query = saved_query
                if not loop:
                    raise
                log.info(fmt(C.YELLOW, f"\n⚠  Qobuz is temporarily unavailable: {e}\n"))
                continue
            except QobuzError as e:
                cleaned = friendly_qobuz_error(e)
                report = log.info if loop else log.warning
                if cleaned.startswith("HTTP 404"):
                    report(fmt(C.RED,
                        "\n✗  No album with that id. Check the URL or search by name.\n"))
                else:
                    report(fmt(C.RED, f"\n✗  Qobuz API error: {cleaned}.\n"))
                args.query = saved_query
                if not loop:
                    # One-shot invocation: a Qobuz API failure is fatal.
                    raise SystemExit(1)
                continue
            except Aborted as e:
                # Cancelling at the result picker (not the top-level query
                # prompt) should re-prompt in loop mode, NOT return. A return
                # falls through to the finally block and flushes the queue.
                if loop and "selection" in str(e):
                    log.info(fmt(C.GRAY, "  Cancelled; back to album prompt."))
                    args.query = saved_query
                    continue
                log.info(fmt(C.GRAY, "  Cancelled."))
                args.query = saved_query
                return 0
            finally:
                args.query = saved_query

            if loop and not args.yes:
                _interactive_album_action(album, args, token, album_queue, _flush_queue)
            else:
                try:
                    result = _download_album_now(album, args, token)
                except AuthLost:
                    die(fmt(C.RED, auth_lost_msg("mid-album")), EXIT_AUTH)

            if not loop:
                exit_code = _one_shot_exit_code(result)
                if exit_code:
                    log.warning(fmt(
                        C.YELLOW,
                        "  ⚠  Album run needs attention; review the result "
                        "above and retry if needed.",
                    ))
                return exit_code
    except KeyboardInterrupt:
        interrupted = True
        if album_queue:
            log.info(fmt(C.YELLOW,
                f"\n  ⚠  Interrupted with {len(album_queue)} album(s) queued; "
                "discarding queue (Ctrl+C means abort)."))
        raise
    finally:
        # Flush only on a clean exit.
        if not interrupted and sys.exc_info()[0] is None:
            _flush_queue()
