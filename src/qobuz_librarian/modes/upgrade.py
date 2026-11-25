"""Upgrade walk mode: review library quality upgrade candidates."""
import sys
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import post_job_hook, saved_views
from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
from qobuz_librarian.api.search import get_album
from qobuz_librarian.download_result import download_attention_kind
from qobuz_librarian.library import (
    candidate_premise,
    generation_state,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.scanner import clear_scan_caches, list_library_artists
from qobuz_librarian.library.tags import VA_NORMALIZED, known_year, normalize
from qobuz_librarian.modes.process import UpgradedThenInterrupted, process_album
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.quality.decision import (
    load_capped,
    mark_album_capped,
)
from qobuz_librarian.ui_cli.colors import C, banner, block, fmt, truncate, wrap
from qobuz_librarian.ui_cli.errors import (
    EXIT_GENERAL,
    EXIT_INTERRUPT,
    no_artist_folders,
    plural,
    unreadable_artist,
)
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm

# process_album outcomes that mean "nothing to upgrade here", not a failure.
BENIGN_UPGRADE_RESULTS = frozenset({
    "upgrade_only_no_op",
    "upgrade_same_quality",
    "skipped_already_higher_quality",
    "skipped_has_extras",
    "lossy_only",
    "no_tracks",
    "user_skipped",
    "dry_run",
    "cancelled",
})


def run_upgrade_walk_mode(args, token):
    """Review current saved upgrades or discover candidates for this walk.

    Returns the exit code: 0 when the walk finished cleanly, non-zero when it
    was cut short or approved replacement work failed. The interactive menu
    ignores it; --upgrade-walk exits with it.
    """
    return _UpgradeWalk(args, token).run()


_STOP_WALK = object()


class _UpgradeWalk:
    """One upgrade walk, reviewing the candidates artist by artist."""

    def __init__(self, args, token):
        self.args = args
        self.token = token

    def run(self):
        if not cfg.UPGRADE_SCAN_ENABLED:
            log.info(fmt(C.YELLOW, "  Upgrade scanning is turned off."))
            return 0
        clear_scan_caches()
        banner("Upgrade walk")
        self.started = time.time()

        for step in (self._load_state, self._visible, self._drop_gone):
            exit_code = step()
            if exit_code is not None:
                return exit_code

        self.by_artist: dict[str, list[dict]] = {}
        for cand in self.saved:
            artist = cand.get("artist") or "Unknown artist"
            self.by_artist.setdefault(artist, []).append(cand)

        # Consolidation prompts per-album would be unbearably noisy at scale.
        saved_consolidate = self.args.consolidate
        self.args.consolidate = False

        self._reset_counts()
        self._announce()

        # Auto-accept-all gate. Skipped under --dry-run (which changes nothing), so
        # a preview run doesn't prompt to "run unattended", matching downsample.py.
        self.auto_accept_all = False
        try:
            exit_code = self._ask_unattended()
            if exit_code is not None:
                return exit_code

            for artist_name, candidates in sorted(
                    self.by_artist.items(), key=lambda group: group[0].casefold()):
                if self._review_artist(artist_name, candidates) is _STOP_WALK:
                    break
        except KeyboardInterrupt:
            self.interrupted = True
        except QobuzUnavailable as exc:
            self.interrupted = True
            self.unavailable = exc
        finally:
            self.args.consolidate = saved_consolidate

        exit_code = self._print_summary()
        if exit_code is not None:
            return exit_code
        self._report()
        if self.unavailable:
            raise self.unavailable
        if self.interrupted:
            return EXIT_INTERRUPT
        return EXIT_GENERAL if self.no_answer or self.n_failed_attempts else 0

    def _load_state(self):
        """Load the saved upgrades, or discover them when they are not current."""
        self.saved_state = upgrade_state.load()
        authority = generation_state.load()
        generation = int(authority.get("generation") or 0)
        output = generation_state.output_state("upgrade", authority)
        saved_current = bool(
            generation > 0
            and self.saved_state.get("complete")
            and int(self.saved_state.get("generation") or 0) == generation
            and int(self.saved_state.get("revision") or 0)
            == int(output.get("revision") or 0)
            and output.get("status") == "current"
            and output.get("complete")
            and str(self.saved_state.get("quality_signature") or "")
            == upgrade_state.quality_signature()
        )
        if not saved_current:
            return self._discover()

    def _discover(self):
        discovery_errors = {}

        def artist_read_failed(artist_dir, error):
            if normalize(artist_dir.name) not in VA_NORMALIZED:
                discovery_errors[artist_dir.name] = str(error)
                log.warning(unreadable_artist(artist_dir.name, error))

        artists = [d for d in list_library_artists(on_artist_error=artist_read_failed)
                   if normalize(d.name) not in VA_NORMALIZED]
        if not artists and not discovery_errors:
            log.info(block(fmt(C.YELLOW, f"  ⚠  {no_artist_folders()}")))
            return 0
        log.info(f"  Checking {plural(len(artists), 'artist')} for upgrades…")

        def on_artist(artist_dir, _candidates, error, done, total):
            if sys.stdout.isatty():
                line = f"  [{done}/{total}] Scanning {truncate(artist_dir.name, 46).removesuffix('…')}…"
                print(f"\r{line:<80}", end="", flush=True)
            if error is not None and artist_dir.name not in discovery_errors:
                log.info("")
                log.info(fmt(C.YELLOW, f"  Skipped {artist_dir.name}: {error}"))

        refresh = upgrade_state.refresh_for_artists(
            artists,
            token=self.token,
            args=self.args,
            capped=load_capped(),
            hidden=hidden_mod.load(),
            workers=max(1, int(cfg.ARTIST_SCAN_WORKERS)),
            on_artist=on_artist,
            discovery_errors=discovery_errors,
            persist=False,
        )
        if sys.stdout.isatty():
            print()
        if not refresh.complete:
            log.warning(fmt(C.YELLOW,
                "  Not every artist could be checked, so nothing was upgraded."))
            return EXIT_GENERAL
        self.saved_state = {"complete": True, "candidates": refresh.candidates}

    def _visible(self):
        # The saved rows carry no kind; the web review adds it when it lists them,
        # and the premise check reads it to know what was sealed.
        self.saved = [
            {**c, "kind": "upgrade"} for c in upgrade_state.visible_candidates(
                self.saved_state, hidden_mod.load())
            if (c.get("payload") or {}).get("album_id")
        ]
        if not self.saved:
            log.info(fmt(C.YELLOW, "  No upgrades found."))
            return 0

    def _drop_gone(self):
        # A saved candidate is a record of a past scan, not a fact about the
        # library now. Set aside anything whose album folder has gone since,
        # instead of presenting it (and calling it high-confidence) as if it were
        # still there. Candidates from older scans don't carry the folder, and a
        # missing music root proves nothing about any of them; both present as
        # before.
        self.n_stale_candidates = 0
        if Path(cfg.MUSIC_ROOT).is_dir():
            still = []
            for c in self.saved:
                d = (c.get("payload") or {}).get("album_dir")
                if d and not Path(d).is_dir():
                    self.n_stale_candidates += 1
                    vlog(f"  gone from disk: {c.get('artist') or '?'}: "
                         f"{c.get('title') or '?'}")
                else:
                    still.append(c)
            self.saved = still
        if self.n_stale_candidates:
            log.info(fmt(C.YELLOW,
                f"  {plural(self.n_stale_candidates, 'saved candidate')} no longer in "
                "the library. Run a Library refresh to clear "
                f"{'it' if self.n_stale_candidates == 1 else 'them'}."))
        if not self.saved:
            log.info(fmt(C.YELLOW,
                "  Nothing left to review. Run a Library refresh first."))
            return 0

    def _reset_counts(self):
        self.n = len(self.by_artist)
        self.n_reviewed = 0
        self.n_upgraded_albums = 0
        self.n_same_quality = 0
        self.n_unverified_albums = 0
        self.n_catalogue_failed = 0
        self.n_failed_albums = 0
        self.n_refused_albums = 0
        self.n_attention_albums = 0
        self.n_below_target = 0
        self.n_gone_albums = 0
        self.n_stale_albums = 0
        self.n_would_upgrade = 0
        self.dry_run = getattr(self.args, "dry_run", False)
        self.no_answer = False
        self.n_attempted = 0
        self.interrupted = False
        self.unavailable = None
        self.stopped_during_removal = None
        self.unsafe_artists = []  # --auto-safe skipped artists, for end-of-run review

    def _announce(self):
        log.info(fmt(C.GRAY,
            f"  {plural(len(self.saved), 'upgrade candidate')} across "
            f"{plural(self.n, 'artist')}."))
        log.info(fmt(C.GRAY, "  Ctrl-C to stop at any point."))
        if not self.dry_run:
            log.info(fmt(C.YELLOW, wrap(
                "If the same tracks come back at the same quality, your original "
                "files are kept. A verified upgrade replaces them permanently.")))

    def _ask_unattended(self):
        if (not self.args.yes and not getattr(self.args, "auto_safe", False)
                and not self.dry_run):
            answer = confirm(
                "\n  Auto-accept all upgrades and run unattended?",
                default_yes=False, auto_yes=False, on_eof=None, quiet=True)
            if answer is None:
                log.warning(fmt(C.YELLOW,
                    "\n  No answer was given."))
                return EXIT_GENERAL
            if answer:
                self.auto_accept_all = True
                log.info(fmt(C.GREEN,
                    "  ✓ Auto-accepting every artist."))
        log.info("")

    def _review_artist(self, artist_name, candidates):
        self.n_reviewed += 1
        log.info("")
        log.info("")
        n_albums = len(candidates)

        log.info(fmt(C.BOLD + C.WHITE, f"  {artist_name}"))
        log.info(fmt(C.MAGENTA,
            f"  {plural(n_albums, 'album')} to upgrade:"))
        log.info("")
        for c in candidates:
            payload = c.get("payload") or {}
            title = truncate(post_job_hook.release_title(
                c.get("title"), payload.get("version")), 48)
            year = known_year(payload.get("year")) or "?"
            detail = c.get("detail") or "higher Qobuz quality"
            log.info(f"    • {fmt(C.WHITE, title)} "
                     f"{fmt(C.GRAY, f'({year})')}   "
                     f"{fmt(C.MAGENTA, detail)}")
        log.info("")

        # --auto-safe is the fully unattended path.
        if getattr(self.args, "auto_safe", False):
            low_conf = self._low_confidence(candidates)
            if low_conf:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  --auto-safe: skipping {artist_name} "
                    f"({len(low_conf)}/{len(candidates)} low-confidence)."))
                for _t, _rs in low_conf:
                    log.info(fmt(C.GRAY,
                        f"     · {truncate(_t, 50)}: {'; '.join(_rs)}"))
                self.unsafe_artists.append((artist_name, low_conf))
                log.info("")
                return
            log.info(fmt(C.GREEN,
                f"  ✓  --auto-safe: all {plural(n_albums, 'candidate')} high-confidence."))
        elif not self.dry_run:
            _flush_stdin()
            # on_eof=None so a closed stdin is distinguishable from a No:
            # the walk would otherwise skip every artist and call that a
            # success.
            answer = confirm(f"  Upgrade {plural(n_albums, 'album')}?",
                             default_yes=False,
                             auto_yes=self.args.yes or self.auto_accept_all,
                             on_eof=None, quiet=True)
            if answer is None:
                self.no_answer = True
                return _STOP_WALK
            if not answer:
                log.info(fmt(C.GRAY, "  Skipped."))
                log.info("")
                return
        if self.dry_run:
            self.n_would_upgrade += n_albums
            return

        for j, c in enumerate(candidates, 1):
            self._upgrade_album(j, c, n_albums)

        log.info("")

    def _low_confidence(self, candidates):
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
        return low_conf

    def _upgrade_album(self, j, c, n_albums):
        payload = c.get("payload") or {}
        album_id = payload.get("album_id")
        label = f"[{j}/{n_albums}]"
        log.info("")
        title = post_job_hook.release_title(
            c.get("title"), payload.get("version"))
        log.info(fmt(C.BOLD + C.WHITE,
            f"  {label} {truncate(title, 55)}"))
        try:
            premise = candidate_premise.validate(c)
        except candidate_premise.CandidateStale as exc:
            if exc.cause == "unreadable":
                log.info(fmt(C.YELLOW,
                    "  The local album could not be read. It was left unchanged."))
                self.n_failed_albums += 1
                return
            message = (
                str(exc) if exc.cause in ("link", "unsupported") else
                "The local album changed after it was checked. "
                "It was left unchanged."
            )
            log.info(fmt(C.YELLOW, f"  {message}"))
            self.n_stale_albums += 1
            return
        try:
            album = get_album(album_id, self.token)
        except QobuzError as exc:
            log.info(fmt(C.YELLOW,
                "  It is no longer on Qobuz. Skipped."
                if exc.status_code == 404 else
                f"  Qobuz couldn't supply it ({exc}). Skipped."))
            self.n_failed_albums += 1
            return
        try:
            # Upgrade_only=True so partial-album cases
            # only re-rip the present tracks, not download missing ones.
            self.n_attempted += 1
            _proc_result = process_album(album, self.args, allow_force=False,
                          label=label, already_confirmed=True,
                          upgrade_only=True,
                          token=self.token,
                          expected_album_receipt=premise["receipt"])
        except AuthLost:
            raise
        except UpgradedThenInterrupted:
            self.n_upgraded_albums += 1
            self.stopped_during_removal = c.get("title") or "?"
            saved_views.forget_upgraded_album(
                album, payload.get("album_dir"))
            raise

        self._tally_result(album, payload, _proc_result)

    def _tally_result(self, album, payload, _proc_result):
        _pr_result = (_proc_result or {}).get("result", "")
        attention_kind = download_attention_kind(_proc_result or {})
        if _pr_result == "upgrade_no_local_tracks":
            # The album left the library after the scan (an older
            # saved state without folder records, or the folder is
            # there but its tracks are not). Not benign: it must show
            # in the closing summary, and the saved state is left for
            # a Library refresh to clear.
            self.n_gone_albums += 1
            time.sleep(cfg.ARTIST_API_DELAY)
            return
        if (_proc_result or {}).get("catalogue_unverified", False):
            self.n_catalogue_failed += 1
            time.sleep(cfg.ARTIST_API_DELAY)
            return
        if attention_kind == "backup":
            # Keep the backup visible until the replacement or
            # automatic restore can be verified.
            self.n_unverified_albums += 1
            time.sleep(cfg.ARTIST_API_DELAY)
            return
        if _pr_result in BENIGN_UPGRADE_RESULTS:
            if _pr_result == "upgrade_same_quality":
                self.n_same_quality += 1
            elif _pr_result not in {"cancelled", "dry_run"}:
                saved_views.forget_upgraded_album(
                    None, payload.get("album_dir"))
            time.sleep(cfg.ARTIST_API_DELAY)
            return
        if (_proc_result or {}).get("upgrade_refused"):
            # Another try would fetch the same copy.
            self.n_refused_albums += 1
            saved_views.forget_upgraded_album(
                None, payload.get("album_dir"))
            time.sleep(cfg.ARTIST_API_DELAY)
            return
        if not (_proc_result or {}).get("imported", False):
            # Attempted (Qobuz had a higher-quality copy) but didn't
            # land: backup failed, download failed, or import failed.
            self.n_failed_albums += 1
            time.sleep(cfg.ARTIST_API_DELAY)
            return
        verdict = (_proc_result or {}).get("quality_verdict")
        if verdict and verdict["under"] and not verdict["recovered"]:
            mark_album_capped(album.get("id"), album, {
                "n_below": verdict["n_below"],
                "n_at": 0,
                "n_above": 0,
            })
            retry = " after retry" if verdict["retried"] else ""
            self.n_below_target += 1
            log.info(fmt(C.YELLOW,
                f"     ⚠  Upgrade incomplete: "
                f"{plural(verdict['n_below'], 'track')} still below target"
                f"{retry}. Marked capped."))
        elif attention_kind == "processing":
            self.n_attention_albums += 1
            self.n_upgraded_albums += 1
            saved_views.refresh_after_changes(
                [{**_proc_result, "album": album}], self.token, self.args)
            time.sleep(cfg.ARTIST_API_DELAY)
            return
        self.n_upgraded_albums += 1
        saved_views.refresh_after_changes(
            [{**_proc_result, "album": album}], self.token, self.args)
        time.sleep(cfg.ARTIST_API_DELAY)

    def _print_summary(self):
        self.n_failed_attempts = (self.n_failed_albums + self.n_refused_albums
                                  + self.n_unverified_albums
                                  + self.n_attention_albums + self.n_below_target
                                  + self.n_catalogue_failed + self.n_gone_albums
                                  + self.n_stale_albums)
        n_gone = self.n_stale_candidates + self.n_gone_albums

        log.info("")
        if self.no_answer and not self.n_attempted and not self.interrupted:
            log.warning(fmt(C.YELLOW, "  No answer was given."))
            return EXIT_GENERAL
        if self.dry_run:
            log.info(fmt(C.GRAY,
                f"     Reviewed {plural(self.n_reviewed, 'artist')}; would upgrade "
                f"{plural(self.n_would_upgrade, 'album')}."))
        else:
            log.info(fmt(C.GRAY,
                f"     Reviewed {plural(self.n_reviewed, 'artist')}; upgraded tracks in "
                f"{plural(self.n_upgraded_albums, 'album')}."))
        if self.n_same_quality:
            log.info(fmt(C.GRAY,
                f"     Skipped {plural(self.n_same_quality, 'album')} at the same quality."))
        if self.stopped_during_removal:
            log.info(fmt(C.YELLOW,
                f"     ⚠  {truncate(self.stopped_during_removal, 48)} was upgraded; the "
                "stop came while its old copy was being removed."))
        if n_gone:
            log.info(fmt(C.YELLOW,
                f"     ⚠  {plural(n_gone, 'candidate')} no longer in the library. "
                f"Run a Library refresh to clear {'it' if n_gone == 1 else 'them'}."))
        if self.n_stale_albums:
            log.info(fmt(
                C.YELLOW,
                f"     ⚠  {plural(self.n_stale_albums, 'candidate')} changed after "
                "being checked.",
            ))
        if self.n_catalogue_failed:
            log.info(fmt(C.YELLOW,
                f"     ⚠  {plural(self.n_catalogue_failed, 'replacement')} "
                "couldn't be updated in Beets (backup kept)."))
        if self.n_unverified_albums:
            log.info(fmt(C.YELLOW,
                f"     ⚠  {plural(self.n_unverified_albums, 'album')} kept a backup "
                "because the replacement or recovery couldn't be verified."))
        if self.n_attention_albums:
            log.info(fmt(C.YELLOW,
                f"     ⚠  {plural(self.n_attention_albums, 'album')} did not finish "
                "cleanly (see the log above)."))
        if self.n_below_target:
            log.info(fmt(C.YELLOW,
                f"     ⚠  {plural(self.n_below_target, 'album')} remained below the "
                "target quality."))
        if self.n_refused_albums:
            log.info(fmt(C.YELLOW,
                f"     ⚠  {plural(self.n_refused_albums, 'album')} couldn't be verified "
                "after downloading, so the original was put back. Off the "
                "Upgrade list until its next refresh."))
        if self.n_failed_albums:
            log.info(fmt(C.YELLOW,
                f"     ⚠  {plural(self.n_failed_albums, 'album')} couldn't be upgraded "
                "(see the log above)."))
        if self.unsafe_artists:
            log.info("")
            log.info(fmt(C.YELLOW + C.BOLD,
                f"  ⚠  {plural(len(self.unsafe_artists), 'artist')} skipped for manual review:"))
            for _a, _lc in self.unsafe_artists:
                log.info(fmt(C.WHITE, f"     {_a}"))
                for _t, _rs in _lc:
                    log.info(fmt(C.GRAY,
                        f"       · {truncate(_t, 50)}: {'; '.join(_rs)}"))
            log.info(fmt(C.GRAY,
                "     Re-run without --auto-safe to review these interactively."))
        clause = None
        if self.no_answer:
            clause = "no answer was given, so the walk stopped early"
        elif self.unsafe_artists:
            clause = f"{plural(len(self.unsafe_artists), 'artist')} skipped for manual review"
        elif n_gone and not (self.n_upgraded_albums or self.n_would_upgrade):
            clause = f"{plural(n_gone, 'candidate')} no longer in the library"
        if self.interrupted:
            if not self.unavailable:
                log.info(fmt(C.GRAY, "  Interrupted."))
        elif self.n_failed_attempts:
            log.warning(fmt(C.RED, f"  ✗  Finished with {plural(self.n_failed_attempts, 'error')}."))
        elif clause:
            log.warning(fmt(C.YELLOW, f"  ⚠  Needs attention: {clause}."))
        elif not (self.n_upgraded_albums or self.n_would_upgrade or self.n_same_quality):
            log.info(fmt(C.GRAY, "  Nothing to do."))
        else:
            log.info(fmt(C.GREEN, "  ✓  Done."))

    def _report(self):
        if self.n_attempted or self.n_failed_attempts:
            stopped = self.interrupted or self.no_answer
            parts = [f"{plural(self.n_upgraded_albums, 'album')} upgraded"]
            for count, what in (
                    (self.n_same_quality, "skipped at the same quality"),
                    (self.n_failed_albums, "couldn't be upgraded"),
                    (self.n_refused_albums, "put back after a download that couldn't "
                                            "be verified"),
                    (self.n_unverified_albums, "kept a backup"),
                    (self.n_catalogue_failed, "couldn't be updated in Beets"),
                    (self.n_attention_albums, "did not finish cleanly"),
                    (self.n_below_target, "remained below the target quality"),
                    (self.n_gone_albums, "no longer in the library"),
                    (self.n_stale_albums, "changed after being checked")):
                if count:
                    parts.append(f"{count} {what}")
            post_job_hook.report_terminal_run(
                kind="upgrade", title="Upgrade run",
                status=("canceled" if stopped
                        else "failed" if self.n_failed_attempts else "done"),
                summary=(("Stopped early. " if stopped else "")
                         + ", ".join(parts) + "."),
                error=(f"Finished with {plural(self.n_failed_attempts, 'error')}."
                       if self.n_failed_attempts and not stopped else None),
                started_at=self.started)
