"""Artist mode: two-step run for one artist.

  Step 1, Gap fill: walk every album you own by this artist and offer to
           download the missing tracks of each.
  Step 2, Missing albums: list everything on Qobuz by this artist you
           don't have, and offer to download.

The matching itself (which folder is which Qobuz edition, what's missing,
and what's a false match) lives in library.discovery, the one engine the web
scans share. This module is the terminal face: prompts, sibling cleanup, the
two-step presentation.
"""
import time

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
from qobuz_librarian.api.search import get_album, get_artist_albums
from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.candidate_premise import CandidateStale
from qobuz_librarian.library.catalog import (
    _count_audio_files_in,
    album_quality_label,
    album_year,
    find_expanded_edition,
    find_extras_in_existing,
    match_dir_to_catalog,
)
from qobuz_librarian.library.discovery import (
    ArtistSearchUnavailable,
    DiscoveryOpts,
    discover_fully_missing,
    match_album_dir,
    owned_album_titles,
    resolve_artist,
    resolve_artist_dir,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
)
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.modes.process import (
    detect_sibling_album_groups,
    pick_canonical_sibling,
    process_album,
)
from qobuz_librarian.quality.decision import compare_album_quality
from qobuz_librarian.quality.tiers import downsample_target_rate, streamrip_quality_cap
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.executor import (
    _admit_new_queue_items,
    _execute_download_queue,
    _refresh_review_state_after_downloads,
)
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, banner, fmt, section, truncate, wrap
from qobuz_librarian.ui_cli.errors import EXIT_GENERAL, plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import (
    _flush_stdin,
    confirm,
    parse_number_list,
    prompt_edition_pick,
)


def _downsample_note(album):
    """' → will downsample to Xkhz' when the hi-res master will be reduced, else
    ''. Qobuz sampling rate is kHz (44.1, 96.0); >=1000 means it's already Hz."""
    if not (cfg.DOWNSAMPLE_HIRES_ENABLED and HAVE_DOWNSAMPLE):
        return ""
    sr = album.get("maximum_sampling_rate") or 0
    sr_hz = int(round(sr)) if sr >= 1000 else int(round(sr * 1000))
    # Cap the advertised rate to the active streamrip quality tier first, the
    # same way album_max_quality() does.
    if sr_hz:
        sr_hz = min(sr_hz, streamrip_quality_cap()[1])
    target_hz = downsample_target_rate(sr_hz)
    if target_hz < sr_hz:
        return f"  → will downsample to {target_hz / 1000:g}kHz"
    return ""


def _consolidation_disabled_notice(args, mode_name):
    if args.consolidate:
        log.info(fmt(C.GRAY, wrap(
            f"{mode_name} skips sibling-folder consolidation. Use Album mode "
            "with --consolidate to check one album.",
            indent="  · ", hanging="    ")))


def run_artist_gap_fill(artist_name, artist_dir, args, token, *,
                      shared_queue=None, flush_callback=None,
                      skip_predicate=None, save_callback=None, fresh=False,
                      hidden=None):
    """Walk every album dir under artist_dir, match each to Qobuz, prompt to fill
    gaps. The matching is library.discovery.match_album_dir; this function is
    the prompting + sibling cleanup around it. Returns (results, owned_titles,
    handled_ids, resolved_dirs, artist_id, catalog).

    ``hidden`` is a loaded hidden-store from a whole-library walk, so an album
    dismissed on the web review's Gap Fill tab stays dismissed here. Asking for
    one artist by name leaves it None and is offered every gap.
    """
    album_dirs = list_artist_album_dirs(artist_dir)
    if not album_dirs:
        log.info(fmt(C.YELLOW, f"  ⚠  No album folders found under {artist_dir}."))
        # catalog=None, not []: the missing-albums step treats a non-None
        # catalog as authoritative.
        return [], {}, set(), set(), None, None

    sibling_choices = {}  # picked_dir -> [siblings to delete after fill]

    # Resolve the artist + pre-fetch the catalog.
    vlog("  Resolving artist + pre-fetching catalog …")
    artist_search_failed = False
    try:
        artist_id, resolved_name = resolve_artist(artist_name, token)
    except ArtistSearchUnavailable:
        artist_id, resolved_name = None, None
        artist_search_failed = True
    if resolved_name:
        artist_name = resolved_name
    catalog = []
    catalog_fetched = False
    if artist_id is not None:
        try:
            catalog, qobuz_total = get_artist_albums(
                artist_id, token, limit=cfg.ARTIST_CATALOG_LIMIT, fresh=fresh)
            catalog_fetched = True
            vlog(f"  Pre-fetched {len(catalog)} catalog entries (artist_id={artist_id}).")
            if qobuz_total is not None and qobuz_total > len(catalog):
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Qobuz reports {qobuz_total} albums; only fetched "
                    f"{len(catalog)}. Folders past the limit will fall back to search."))
        except QobuzError as e:
            log.info(fmt(C.YELLOW,
                f"  ⚠  catalog pre-fetch failed ({e}); per-folder fallback."))
            catalog = []
    elif artist_search_failed:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Qobuz couldn't complete the artist lookup for "
            f"{artist_name!r}; trying each album instead."))
    else:
        log.info(fmt(C.YELLOW,
            f"  ⚠  No confident Qobuz artist match for {artist_name!r}; "
            "per-folder fallback."))

    skip_set = set()  # folders set aside while picking among duplicate groups
    sibling_groups = detect_sibling_album_groups(album_dirs)
    if sibling_groups:
        log.info(fmt(C.YELLOW,
            f"\n  ⚠  Detected {len(sibling_groups)} sibling folder group(s):"))
        for bare, dirs in sibling_groups:
            print()
            log.info(fmt(C.WHITE, f"  Group '{bare}':"))
            for j, d in enumerate(dirs, 1):
                n_audio = _count_audio_files_in(d)
                _sib_qual = ""
                if catalog:
                    _sc = match_dir_to_catalog(d, catalog, artist_name)
                    if _sc:
                        _sib_qual = f"  {fmt(C.CYAN, album_quality_label(_sc))}"
                log.info(f"    {fmt(C.BOLD, str(j))}. "
                         f"{fmt(C.WHITE, d.name)}  "
                         f"{fmt(C.GRAY, f'[{n_audio} audio file(s)]')}"
                         f"{_sib_qual}")
            log.info(fmt(C.GRAY,
                "    Pick which to keep; others are deleted after a successful fill."))
            log.info(fmt(C.GRAY, "    Enter to skip the entire group."))
            picked = None
            yes_skip_delete = False  # --yes picks but never deletes
            if args.yes:
                picked = pick_canonical_sibling(dirs)
                yes_skip_delete = True
                log.info(fmt(C.GRAY,
                    f"    --yes: picking {picked.name} "
                    f"(siblings preserved; deletion needs interactive confirm)"))
            else:
                while True:
                    rr = ask(f"    Pick [1-{len(dirs)}, enter=skip]: ",
                             lower=False)
                    if not rr:
                        break
                    if rr.isdigit() and 1 <= int(rr) <= len(dirs):
                        picked = dirs[int(rr) - 1]
                        break
                    log.info(fmt(C.GRAY,
                        f"    Enter 1-{len(dirs)} or blank to skip."))
            if picked is None:
                log.info(fmt(C.GRAY, "    Skipped entire group."))
                for d in dirs:
                    skip_set.add(d)
                continue
            others = [d for d in dirs if d is not picked]
            # Under --yes, pick but DO NOT enqueue siblings for delete-after-fill;
            # matches the docstring's --yes contract.
            sibling_choices[picked] = [] if yes_skip_delete else others
            for d in others:
                skip_set.add(d)
        if skip_set:
            album_dirs = [d for d in album_dirs if d not in skip_set]
            log.info(fmt(C.GRAY,
                f"  → {len(album_dirs)} folder(s) to scan."))

    # Folder bare-titles seed the owned set the missing-albums step reads, so a
    # catalog album already on disk isn't re-offered.
    owned_titles = owned_album_titles(album_dirs)
    handled_ids = set()     # Qobuz ids matched here; the missing step skips them
    resolved_dirs = set()   # folders matched here, so a different edition of the
                            # same album doesn't re-surface them as missing

    section(f"Step 1: Gap fill ({len(album_dirs)} album(s) for {artist_name})")
    vlog("  For each, querying Qobuz and offering to fill missing tracks.")
    vlog("  Press 's' at any prompt to stop the scan.")

    results = []
    # Folders set aside while picking among duplicate sibling groups are
    # removed from album_dirs above, so the main loop never records a decision
    # for them.
    for d in sorted(skip_set, key=lambda p: p.name):
        results.append({"dir": d, "result": "sibling_skipped"})
    stopped_early = False
    # 'a' at any gap-fill prompt auto-confirms the rest of THIS artist's albums.
    # Scoped local; it doesn't bleed into step 2 or the next artist in a walk.
    auto_yes_rest = False
    # In shared_queue mode, append decisions STRAIGHT into shared_queue (not a
    # local list): a Ctrl-C mid-loop then leaves the current artist's
    # approvals in the caller's queue (which the walk persists on interrupt)
    # instead of dropping them, and the 'd' flush can act on them mid-artist.
    queue = shared_queue if shared_queue is not None else []
    # AuthLost / QobuzUnavailable mid-loop: stash it, finish the hand-off so
    # the artist's queue reaches shared_queue, then re-raise.
    pending_stop = None

    for i, ad in enumerate(album_dirs, 1):
        if stopped_early:
            break

        if skip_predicate is not None:
            try:
                if skip_predicate(ad):
                    continue
            except Exception as _spe:
                vlog(f"  skip_predicate raised on {ad.name}: {_spe}")

        label = f"[{i}/{len(album_dirs)}]"
        print()
        log.info(fmt(C.BOLD + C.WHITE, f"  {label} {truncate(ad.name, 55)}"))

        try:
            m = match_album_dir(ad, artist_name, token,
                                catalog=catalog, prefer_hires=args.prefer_hires)
        except (AuthLost, QobuzUnavailable) as _e_stop:
            pending_stop = _e_stop
            break
        time.sleep(cfg.ARTIST_API_DELAY)

        # Sibling-pick fallback. The picked folder failed to match, so offer each
        # alternate from its group in turn.
        if m.status == "no_match" and ad in sibling_choices:
            log.info(fmt(C.YELLOW, f"    ⚠  No Qobuz match for {ad.name}."))
            fallback_queue = list(sibling_choices[ad])
            original_ad = ad
            while fallback_queue:
                next_try = fallback_queue.pop(0)
                if not confirm(f"    Try sibling {next_try.name} instead?",
                               default_yes=True, auto_yes=args.yes):
                    log.info(fmt(C.GRAY, "    Skipping the rest of this group."))
                    break
                log.info(fmt(C.GRAY, f"    Trying {next_try.name} …"))
                try:
                    m = match_album_dir(next_try, artist_name, token,
                                        catalog=catalog, prefer_hires=args.prefer_hires)
                except (AuthLost, QobuzUnavailable) as _e_stop_sib:
                    pending_stop = _e_stop_sib
                    m = None
                    break
                time.sleep(cfg.ARTIST_API_DELAY)
                if m.status != "no_match":
                    all_in_group = sibling_choices[original_ad] + [original_ad]
                    sibling_choices[next_try] = [d for d in all_in_group
                                                 if d is not next_try]
                    sibling_choices.pop(original_ad, None)
                    ad = next_try
                    log.info(fmt(C.GREEN, f"    ✓  Switched to {ad.name}."))
                    break
                log.info(fmt(C.YELLOW,
                    f"    ⚠  No Qobuz match for {next_try.name} either."))

        if pending_stop is not None:
            break

        if m is None or m.status == "no_match":
            log.info(fmt(C.YELLOW, "    ⚠  No confident Qobuz match for this folder. Skipping."))
            results.append({"dir": ad, "result": "no_qobuz_match"})
            continue

        if m.status == "predicted_path_mismatch":
            pred = (m.qobuz_album or {}).get("title") or "?"
            log.info(fmt(C.YELLOW,
                f"    ⚠  Qobuz match resolves to a different folder ({pred}). "
                f"Likely false match; skipping to avoid duplication."))
            results.append({"dir": ad, "result": "predicted_path_mismatch",
                            "qobuz_title": pred})
            continue

        if m.status == "low_overlap":
            # A different, similarly-named album fuzz-matched this folder.
            pred = (m.qobuz_album or {}).get("title") or "?"
            log.info(fmt(C.YELLOW,
                f"    ⚠  Qobuz fuzzy-match has low track overlap with this "
                f"folder ({pred}); possible wrong album. Skipping."))
            results.append({"dir": ad, "result": "low_overlap",
                            "qobuz_title": pred})
            continue

        album = m.qobuz_album
        if album.get("id") is not None:
            handled_ids.add(album["id"])
        resolved_dirs.add(str(ad))
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        n_total = len(qobuz_tracks)

        if m.status == "no_tracks":
            log.info(fmt(C.YELLOW, "    ⚠  Qobuz returned no tracks. Skipping."))
            results.append({"dir": ad, "result": "no_tracks"})
            continue

        if m.status == "false_match":
            log.info(fmt(C.YELLOW,
                f"    ⚠  Qobuz match has 0 track overlap with this folder "
                f"({len(m.existing)} on disk, none matched). "
                f"Likely false match; skipping to avoid duplication."))
            results.append({"dir": ad, "result": "false_match",
                            "qobuz_title": album.get("title") or "?",
                            "n_existing": len(m.existing), "n_qobuz": n_total})
            continue

        existing, missing, present = m.existing, m.missing, m.present

        # Artist mode: complete is complete.
        if m.status == "complete":
            has_extras = False
            # --dry-run is preview-only: skip the interactive expanded-edition
            # pick entirely; it prompts and would queue and persist an item, both
            # of which a dry run must not do.
            if (existing and not getattr(args, "no_upgrade", False)
                    and not getattr(args, "dry_run", False)):
                qual = compare_album_quality(existing, album)
                if qual["classification"] in ("all_lower", "mixed_below"):
                    extra_albums = find_extras_in_existing(qobuz_tracks, existing)
                    has_extras = bool(extra_albums)
            if has_extras:
                try:
                    _cands = find_expanded_edition(album, ad, existing, token, args)
                except (AuthLost, QobuzUnavailable) as _e_stop_exp:
                    pending_stop = _e_stop_exp
                    break
                _exp, _exp_extras = prompt_edition_pick(
                    album, len(extra_albums), _cands, existing, args, label_prefix="    ")
                if _exp is not None:
                    candidate = _build_queue_item(
                        album=_exp, album_dir=ad, label=label,
                        missing=list((_exp.get("tracks") or {}).get("items") or []),
                        present=[], upgrade_only=False, auto_upgrade=True,
                        siblings_to_delete=sibling_choices.get(ad, []),
                    )
                    try:
                        _admit_new_queue_items([candidate], token)
                    except CandidateStale as exc:
                        log.info(fmt(C.YELLOW, f"    ⚠  {exc}"))
                        results.append({"dir": ad, "result": "stale_candidate"})
                        continue
                    log.info(fmt(C.MAGENTA,
                        f"    ↑  Switching to {_exp.get('title') or '?'!r} at "
                        f"{album_quality_label(_exp)}; queued for batch upgrade"))
                    queue.append(candidate)
                    continue
                log.info(fmt(C.GRAY,
                    f"    ✓  All {n_total} track(s) present "
                    f"(Qobuz higher-quality but you have bonus tracks; preserving)"))
            else:
                log.info(fmt(C.GREEN,
                    f"    ✓  All {n_total} track(s) present; checking next"))
            results.append({"dir": ad, "result": "already_complete", "n_total": n_total})
            # Don't delete the picked album's siblings here: the sibling-group
            # prompt promised deletion "on successful fill", but this branch
            # did NO download (the folder was already complete).
            continue

        # Partial: offer to fill the gap, unless the web review already got a
        # no for this album. Both of its tabs write the same dismissal scope.
        if hidden is not None and hidden_mod.is_hidden(
                hidden_mod.SCOPE_MISSING, artist_name,
                album.get("title") or "", hidden, year=album_year(album)):
            log.info(fmt(C.GRAY, "    Dismissed in the Library review; skipping."))
            results.append({"dir": ad, "result": "dismissed"})
            continue

        log.info(fmt(C.YELLOW,
            f"    {len(present)}/{n_total} present; {len(missing)} missing"))
        for _mt in missing[:8]:
            _tn = _mt.get("track_number") or "?"
            log.info(fmt(C.GRAY,
                f"      {str(_tn).rjust(2)}. {truncate(_mt.get('title') or '?', 56)}"))
        if len(missing) > 8:
            log.info(fmt(C.GRAY, f"      … and {len(missing) - 8} more"))
        log.info(fmt(C.GRAY,
            f"    Qobuz: {album_year(album) or '?'} • {album_quality_label(album)}"
            f"{_downsample_note(album)}"))

        if args.dry_run:
            log.info(fmt(C.GRAY, "    --dry-run: would prompt to download here"))
            results.append({"dir": ad, "result": "dry_run", "n_missing": len(missing)})
            continue

        if args.yes or auto_yes_rest:
            answer = "y"
        else:
            while True:
                _flush_hint = (
                    f"/d=download queue ({len(shared_queue) if shared_queue else 0})"
                ) if (flush_callback is not None and shared_queue is not None) else ""
                _q = (f"    Download {len(missing)} missing track(s)? "
                      f"[y/N/a=yes-rest{_flush_hint}/s=stop]: ")
                _flush_stdin()
                answer = ask(_q)
                if answer is None:
                    answer = "n"
                    break
                if answer == "d" and flush_callback is not None:
                    try:
                        flush_callback()
                    except KeyboardInterrupt:
                        log.info(fmt(C.GRAY,
                            "\n    Flush interrupted; back to the prompt."))
                    continue
                if answer == "a":
                    auto_yes_rest = True
                    answer = "y"
                    log.info(fmt(C.GRAY,
                        "    Auto-yes for the rest of this artist's albums."))
                break

        if answer == "s":
            log.info(fmt(C.GRAY, "    Stopping artist scan."))
            stopped_early = True
            results.append({"dir": ad, "result": "user_stopped"})
            break
        if answer not in ("y", "yes"):
            results.append({"dir": ad, "result": "user_skipped", "n_missing": len(missing)})
            continue

        candidate = _build_queue_item(
            album=album, album_dir=ad, label=label,
            missing=list(missing), present=list(present),
            upgrade_only=False, auto_upgrade=False,
            siblings_to_delete=sibling_choices.get(ad, []),
        )
        try:
            _admit_new_queue_items([candidate], token)
        except CandidateStale as exc:
            log.info(fmt(C.YELLOW, f"    ⚠  {exc}"))
            results.append({"dir": ad, "result": "stale_candidate"})
            continue
        queue.append(candidate)

    # Shared_queue mode: decisions already went straight into shared_queue
    # (see above), so just persist; no extend (that would double every item).
    if shared_queue is not None:
        if save_callback is not None and queue:
            try:
                save_callback()
            except Exception as _sce:
                vlog(f"  save_callback raised: {_sce}")
    elif queue and pending_stop is None:
        # The executor deletes each item's chosen siblings itself once the fill
        # lands cleanly, so there's nothing to clean up here.
        queue_results, drained = _execute_download_queue(queue, args, token,
                                                         refresh_review=True)
        results.extend(queue_results)
        if not drained:
            log.info(fmt(C.YELLOW,
                "  ⚠  Some albums couldn't be downloaded. They were kept for "
                "retry; rerun to try them again."))

    if pending_stop is not None:
        raise pending_stop

    # catalog is None when the pre-fetch didn't land (no artist match, or a
    # failed fetch) so the missing-albums step re-fetches instead of trusting
    # an empty list as "nothing on Qobuz".
    return (results, owned_titles, handled_ids, resolved_dirs, artist_id,
            catalog if catalog_fetched else None)


_ATTENTION_BUCKETS = {"partial", "not_imported", "stopped", "failed", "other"}


def _gap_fill_result_bucket(result):
    """Map one album result to the same bucket used by the artist summary."""
    hard_failures = {
        "failed", "nothing_landed", "not_imported", "import_failed",
        "upgrade_aborted_backup_failed",
        "replacement_aborted_catalogue_failed",
    }
    stopped_results = {
        "interrupted", "cancelled", "disk_full", "io_error", "auth_lost",
    }
    partial_fields = {
        "n_fail", "n_lossy", "n_broken", "downsample_errors",
        "downsample_flush_warnings",
    }
    status = result.get("result", "")
    imported = bool(result.get("imported", False))
    downloaded = result.get("n_ok", 0) > 0
    verdict = result.get("quality_verdict") or {}
    has_partial_attention = (
        status == "partial"
        or any(result.get(field, 0) for field in partial_fields)
        or result.get("downsample_cancelled", False)
        or result.get("consolidation_interrupted", False)
        or result.get("upgrade_unverified", False)
        or result.get("catalogue_unverified", False)
        or result.get("recovery_unverified", False)
        or (verdict.get("under") and not verdict.get("recovered"))
    )

    if status in {"already_complete", "skipped_already_higher_quality"}:
        return "already_complete"
    if status in {"user_skipped", "user_stopped", "dismissed"}:
        return "skipped"
    if status == "no_qobuz_match":
        return "no_match"
    if status == "predicted_path_mismatch":
        return "path_mismatch"
    if status == "false_match":
        return "false_match"
    if status == "dry_run":
        return "dry_run"
    if status in {"no_tracks", "lossy_only"}:
        return "nothing_available"
    if status in {"low_overlap", "sibling_skipped"}:
        return "held"
    if status in stopped_results:
        return "stopped"
    if status in hard_failures:
        return "failed"
    if downloaded and not imported:
        return "not_imported"
    if imported and has_partial_attention:
        return "partial"
    if imported and downloaded and result.get("auto_upgrade", False):
        return "upgraded"
    if imported and downloaded:
        return "filled"
    return "other"


def run_artist_missing_albums(artist_name, owned_titles, args, token,
                      artist_id=None, handled_ids=None, resolved_dirs=None,
                      prefetched_catalog=None, *, shared_queue=None, fresh=False,
                      hidden=None):
    """List the artist's full Qobuz catalog with already-owned albums filtered
    out, prompt to download some. The matching is discovery.discover_fully_missing;
    this is the numbered-list presentation around it. Returns
    ``(count_downloaded_or_queued, needs_attention)``.

    ``hidden`` is a loaded hidden-store from a whole-library walk, so albums
    dismissed in the web review stay dismissed here. Asking for one artist by
    name leaves it None and sees the whole catalogue.
    """
    section(f"Step 2: Missing albums by {artist_name}, not yet in your library")

    if artist_id is None:
        log.info(fmt(C.GRAY, "  Looking up artist on Qobuz …"))
        try:
            artist_id, resolved_name = resolve_artist(artist_name, token)
        except ArtistSearchUnavailable:
            log.info(fmt(C.YELLOW,
                "  ⚠  Qobuz couldn't complete the artist lookup. "
                "Try this step again."))
            return 0, True
        if artist_id is None:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Couldn't find {artist_name!r} on Qobuz. Skipping step 2."))
            return 0, False
        if resolved_name:
            artist_name = resolved_name
        log.info(fmt(C.GRAY, f"  Matched Qobuz artist: {artist_name!r} (id {artist_id})"))

    if prefetched_catalog is not None:
        catalog = prefetched_catalog
        vlog(f"  Reusing catalog from gap-fill ({len(catalog)} entries); no refetch.")
    else:
        vlog(f"  Fetching catalog (limit {cfg.ARTIST_CATALOG_LIMIT}) …")
        try:
            catalog, qobuz_total = get_artist_albums(artist_id, token,
                                                     limit=cfg.ARTIST_CATALOG_LIMIT,
                                                     fresh=fresh)
        except QobuzError as e:
            log.info(fmt(C.YELLOW, f"  ⚠  Couldn't fetch artist catalog: {e}."))
            return 0, True
        if qobuz_total is not None and qobuz_total > len(catalog):
            log.info(fmt(C.YELLOW,
                f"  ⚠  Qobuz reports {qobuz_total} total albums; only fetched "
                f"{len(catalog)} (limit={cfg.ARTIST_CATALOG_LIMIT}). Some entries omitted."))

    opts = DiscoveryOpts(prefer_hires=args.prefer_hires,
                         include_comps=args.include_comps,
                         include_singles=getattr(args, "include_singles", False))
    gaps = discover_fully_missing(
        artist_name, catalog, opts,
        hidden=hidden,
        handled_ids=handled_ids or set(),
        resolved_dirs=resolved_dirs or set(),
        owned_titles=owned_titles or {}, token=token)

    # Partially-present albums (a collaboration filed elsewhere) list first.
    partials = [g for g in gaps if g.on_disk_dir is not None]
    fully = [g for g in gaps if g.on_disk_dir is None]
    ordered = partials + fully
    n_partial = len(partials)

    if not ordered:
        log.info(fmt(C.GREEN, "\n  ✓  No new albums to suggest. You're caught up!\n"))
        return 0, False

    print()
    header = f"  {len(ordered)} album(s) you don't have"
    if n_partial:
        header += f"  ({n_partial} partially present; listed first)"
    log.info(fmt(C.BOLD + C.WHITE, header + ":"))
    print()
    for i, gap in enumerate(ordered, 1):
        a = gap.qobuz_album
        title  = a.get("title") or "?"
        year   = album_year(a) or "?"
        tracks = a.get("tracks_count") or "?"
        track_word = "track" if tracks == 1 else "tracks"
        qual   = album_quality_label(a)

        line1 = (f"  {fmt(C.BOLD, str(i).rjust(3))}.  "
                 f"{fmt(C.WHITE, truncate(title, 55))}")
        line2 = f"        {fmt(C.GRAY, f'{year} • {tracks} {track_word} • {qual}')}"
        if gap.on_disk_dir is not None:
            n_have = len(gap.present)
            n_tot = n_have + len(gap.missing)
            line2 += fmt(C.YELLOW, f"  ← {n_have}/{n_tot} tracks already present")
        print(line1)
        print(line2)
    print()

    if args.dry_run:
        log.info(fmt(C.YELLOW, "  --dry-run: stopping here, nothing prompted."))
        return 0, False

    _flush_stdin()
    raw = ask(
        "  Pick numbers to download (e.g. 1,3,5-7 / a=all / blank=skip): ",
        lower=False) or ""
    picks = parse_number_list(raw, len(ordered))
    if not picks:
        return 0, False

    if shared_queue is not None:
        log.info(fmt(C.GRAY,
            f"  Queuing {plural(len(picks), 'album')}; fetching track lists …"))
        pending = []
        needs_attention = False
        for i, idx in enumerate(picks, 1):
            chosen = ordered[idx - 1].qobuz_album
            try:
                full = get_album(chosen["id"], token)
            except QobuzError as e:
                log.info(fmt(C.YELLOW, f"    ⚠  Couldn't fetch: {e}. Skipping."))
                needs_attention = True
                continue
            full_tracks = (full.get("tracks") or {}).get("items") or []
            pending.append(_build_queue_item(
                album=full, album_dir=None,
                label=f"new {i}/{len(picks)}",
                missing=list(full_tracks), present=[],
                upgrade_only=False, auto_upgrade=False,
                siblings_to_delete=[],
            ))
            time.sleep(cfg.ARTIST_API_DELAY)
        if pending:
            try:
                _admit_new_queue_items(pending, token)
            except CandidateStale as exc:
                log.info(fmt(C.YELLOW, f"  ⚠  {exc}"))
                return 0, True
            shared_queue.extend(pending)
        n_queued = len(pending)
        log.info(fmt(C.GREEN,
            f"  ✓ Queued {plural(n_queued, 'album')} "
            f"(queue total: {len(shared_queue)})."))
        return n_queued, needs_attention

    log.info(fmt(C.GRAY, f"  Selected {plural(len(picks), 'album')}; fetching track lists …"))

    saved_yes = args.yes
    args.yes = True
    n_done = 0
    needs_attention = False
    try:
        for i, idx in enumerate(picks, 1):
            chosen = ordered[idx - 1].qobuz_album
            label = f"[new {i}/{len(picks)}]"
            print()
            log.info(fmt(C.BOLD + C.WHITE, f"  {label} {truncate(chosen.get('title') or '?', 55)}"))
            try:
                full = get_album(chosen["id"], token)
            except QobuzError as e:
                log.info(fmt(C.YELLOW, f"    ⚠  Couldn't fetch: {e}. Skipping."))
                needs_attention = True
                continue
            try:
                r = process_album(full, args, allow_force=False, label=label,
                                  already_confirmed=True, token=token)
            except KeyboardInterrupt:
                log.info(fmt(C.GRAY, "\n    Interrupted. Stopping step 2."))
                needs_attention = True
                break
            if r.get("n_ok", 0) > 0 and r.get("imported", False):
                n_done += 1
                _refresh_review_state_after_downloads(
                    [{**r, "album": full}],
                    token,
                    args,
                )
            if _gap_fill_result_bucket(r) in _ATTENTION_BUCKETS:
                needs_attention = True
            time.sleep(cfg.ARTIST_API_DELAY)
    finally:
        args.yes = saved_yes

    return n_done, needs_attention


def _report_gap_fill_summary(results):
    """Report each gap-fill outcome in one primary summary bucket."""
    counts = {
        "already_complete": 0,
        "filled": 0,
        "upgraded": 0,
        "partial": 0,
        "skipped": 0,
        "no_match": 0,
        "path_mismatch": 0,
        "false_match": 0,
        "nothing_available": 0,
        "held": 0,
        "dry_run": 0,
        "not_imported": 0,
        "stopped": 0,
        "failed": 0,
        "other": 0,
    }
    for result in results:
        counts[_gap_fill_result_bucket(result)] += 1

    labels = (
        ("already_complete", C.GREEN, "✓ already complete:"),
        ("filled", C.GREEN, "✓ tracks filled:"),
        ("upgraded", C.MAGENTA, "↑ auto-upgraded:"),
        ("partial", C.YELLOW, "⚠ partly filled:"),
        ("skipped", C.YELLOW, "skipped by user:"),
        ("no_match", C.YELLOW, "no Qobuz match:"),
        ("path_mismatch", C.YELLOW, "path mismatch:"),
        ("false_match", C.YELLOW, "false match (0 overlap):"),
        ("nothing_available", C.YELLOW, "nothing available on Qobuz:"),
        ("held", C.YELLOW, "held for review:"),
        ("dry_run", C.GRAY, "dry run, not fetched:"),
        ("not_imported", C.YELLOW, "downloaded, not imported:"),
        ("stopped", C.YELLOW, "stopped, still queued:"),
        ("failed", C.RED, "✗ failed:"),
        ("other", C.YELLOW, "other outcomes:"),
    )
    for bucket, colour, label in labels:
        if counts[bucket]:
            padded_label = f"{label:<30}"
            log.info(f"  {fmt(colour, padded_label)}{counts[bucket]}")
    return counts


def _gap_fill_exit_code(counts):
    needs_attention = any(
        counts[bucket]
        for bucket in _ATTENTION_BUCKETS
    )
    if needs_attention:
        log.warning(fmt(
            C.YELLOW,
            "  ⚠  Artist run needs attention; review the summary above "
            "and retry any unfinished albums.",
        ))
    return EXIT_GENERAL if needs_attention else 0


def run_artist_mode(artist_name, args, token):
    """Top-level artist mode entry point. Runs both phases."""
    clear_scan_caches()
    banner(f"Artist mode: {artist_name}")

    if normalize(artist_name) in VA_NORMALIZED:
        log.warning(fmt(C.YELLOW,
            "\n  ⚠  'Various Artists' isn't a real artist; artist mode doesn't support it."))
        log.info(fmt(C.GRAY,
            "     Use album mode for individual compilation albums."))
        return EXIT_GENERAL

    # --consolidate inside artist mode would prompt per album (a LOT on a
    # 30-album scan).
    saved_consolidate = args.consolidate
    if args.consolidate:
        _consolidation_disabled_notice(args, "Artist mode")
        args.consolidate = False

    try:
        artist_dir = resolve_artist_dir(artist_name)
        if artist_dir is None:
            log.info(fmt(C.YELLOW, f"\n  ⚠  No matching artist directory in {cfg.MUSIC_ROOT}."))
            if confirm("\n  Continue anyway and just look for albums by this artist on Qobuz?",
                       default_yes=False, auto_yes=args.yes):
                _, needs_attention = run_artist_missing_albums(
                    artist_name, {}, args, token, fresh=True
                )
                if needs_attention:
                    log.warning(fmt(
                        C.YELLOW,
                        "  ⚠  Artist run needs attention; review the "
                        "details above and retry.",
                    ))
                    return EXIT_GENERAL
            return 0
        vlog(f"  Library: {artist_dir}")

        # fresh=True: an explicit single-artist run should see just-released
        # albums.
        (gap_fill_results, owned_titles, handled_ids, resolved_dirs,
         artist_id, catalog) = run_artist_gap_fill(artist_name, artist_dir, args, token,
                                                   fresh=True)

        no_match     = [r for r in gap_fill_results if r.get("result") == "no_qobuz_match"]
        path_mis     = [r for r in gap_fill_results if r.get("result") == "predicted_path_mismatch"]
        false_match  = [r for r in gap_fill_results if r.get("result") == "false_match"]

        section("Step 1: Gap-fill summary")
        print()
        counts = _report_gap_fill_summary(gap_fill_results)
        exit_code = _gap_fill_exit_code(counts)

        if no_match:
            print()
            log.info(fmt(C.YELLOW, "  no Qobuz match; folders to investigate:"))
            for r in no_match[:25]:
                log.info(fmt(C.GRAY, f"    • {r['dir'].name}"))
            if len(no_match) > 25:
                log.info(fmt(C.GRAY, f"    ... and {len(no_match) - 25} more"))
        if path_mis:
            print()
            log.info(fmt(C.YELLOW, "  path mismatch (Qobuz match resolved to a different folder):"))
            for r in path_mis[:25]:
                qt = r.get("qobuz_title", "?")
                log.info(fmt(C.GRAY, f"    • {r['dir'].name}   →   matched {truncate(qt, 40)!r}"))
            if len(path_mis) > 25:
                log.info(fmt(C.GRAY, f"    ... and {len(path_mis) - 25} more"))
        if false_match:
            print()
            log.info(fmt(C.YELLOW, "  false match (Qobuz match shared 0 tracks with folder):"))
            for r in false_match[:25]:
                qt = r.get("qobuz_title", "?")
                log.info(fmt(C.GRAY,
                    f"    • {r['dir'].name}   →   matched {truncate(qt, 40)!r}"))
            if len(false_match) > 25:
                log.info(fmt(C.GRAY, f"    ... and {len(false_match) - 25} more"))

        if args.no_catalog:
            log.info(fmt(C.GRAY, "\n  --no-catalog: skipping step 2."))
            return exit_code
        print()

        n_added, step_two_attention = run_artist_missing_albums(
            artist_name, owned_titles, args, token,
            artist_id=artist_id, handled_ids=handled_ids,
            resolved_dirs=resolved_dirs,
            prefetched_catalog=catalog,
        )
        if n_added:
            log.info(fmt(C.GREEN, f"\n  ✓  Added {n_added} new album(s) for {artist_name}.\n"))
        if step_two_attention:
            log.warning(fmt(
                C.YELLOW,
                "  ⚠  Artist run needs attention; review Step 2 above "
                "and retry any unfinished albums.",
            ))
            return EXIT_GENERAL
        return exit_code
    finally:
        # Restore --consolidate so the menu loop sees the original CLI value.
        args.consolidate = saved_consolidate
