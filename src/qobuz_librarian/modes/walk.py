"""Walk modes: library walk, walk+queue, and album fill walk."""
import re
import sys

from qobuz_librarian import config as cfg
from qobuz_librarian import safefile
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable
from qobuz_librarian.library import collection_snapshot
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.catalog import _count_audio_files_in
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.modes.artist import (
    _consolidation_disabled_notice,
    run_artist_gap_fill,
    run_artist_missing_albums,
)
from qobuz_librarian.queue.executor import _execute_download_queue
from qobuz_librarian.queue.journal import clear_queue_journal
from qobuz_librarian.queue.persistence import (
    keep_queue,
    own_files_clause,
    parked_queue_note,
    queue_parked,
    queue_saver,
    save_or_trim,
)
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, banner, fmt, truncate, wrap
from qobuz_librarian.ui_cli.errors import (
    EXIT_GENERAL,
    EXIT_INTERRUPT,
    cli_command,
    no_artist_folders,
    plural,
    unreadable_artist,
)
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm


def _seen_lines(path):
    """The entries of a walk's seen file, without its header comments."""
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                # Only "# " (hash + SPACE) is a header comment; an artist folder
                # like "#1 Dads" or "#####" is a real entry and must load back,
                # or it gets re-prompted every walk and the file grows each time.
                if s and not s.startswith("# "):
                    yield s
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Couldn't read {path.name}: {e}."))


def _library_artists():
    """The library's artist folders, and the error for each that could not
    be read."""
    errors = {}

    def read_failed(path, error):
        errors[path.name] = str(error)
        log.warning(unreadable_artist(path.name, error))

    return list_library_artists(on_artist_error=read_failed), errors


def load_walk_seen():
    """Load set of normalized artist names already decided in walk mode."""
    return {n for s in _seen_lines(cfg.WALK_SEEN_FILE) if (n := normalize(s))}


def _atomic_append(path, header_lines, entry):
    """Read-modify-write through an atomic replace.

    Plain "a" mode leaves a truncated line on the disk if the process dies
    mid-write; the next reader silently drops the partial line and the
    entry is lost.
    """
    existing = path.read_bytes() if path.exists() else b""
    content = bytearray()
    if not existing and header_lines:
        for line in header_lines:
            content.extend(line.encode("utf-8"))
    content.extend(existing)
    content.extend(entry.encode("utf-8"))
    safefile.write_atomic(path, content)


def record_walk_seen(artist_name, seen=None):
    """Append a decided artist to the walk seen file (creates with header).

    Skip if already recorded (normalized) so the file doesn't grow unbounded
    across re-walks. ``seen``, when given, is the caller's in-memory set: it's
    used for the dedup check and updated on success, sparing a full re-read of
    the file on every record during a walk.
    """
    try:
        cfg.WALK_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = normalize(artist_name)
        known = seen if seen is not None else load_walk_seen()
        if key in known:
            return
        _atomic_append(
            cfg.WALK_SEEN_FILE,
            header_lines=[
                "# Library walk decisions: artists you've answered y/N for during the library walk.\n",
                "# Skipped on future walks so you don't loop the same names.\n",
                "# To revisit an artist, delete its line below (or delete this whole file).\n",
                "# Comments start with '# ' (hash + space). One artist per line.\n",
                "\n",
            ],
            entry=artist_name + "\n",
        )
        if seen is not None:
            seen.add(key)
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Couldn't write {cfg.WALK_SEEN_FILE.name}: {e}."))


def _album_seen_key(artist_name, album_name):
    """Normalized 'artist::album' key for the album-walk seen file."""
    return f"{normalize(artist_name)}::{normalize(album_name)}"


def _album_seen_before(seen, artist_name, album_dir):
    """Whether an earlier walk settled this album with as many tracks on disk."""
    key = _album_seen_key(artist_name, album_dir.name)
    if key not in seen:
        return False
    return seen[key] is None or seen[key] == _count_audio_files_in(album_dir)


def load_album_walk_seen():
    """Albums already decided during the album fill walk, as normalized
    'artist::album' keys mapped to the audio files the folder held then (None
    for an entry written before the count was kept)."""
    seen = {}
    for s in _seen_lines(cfg.ALBUM_WALK_SEEN_FILE):
        if " | " not in s:
            continue
        artist, album = s.split(" | ", 1)
        n_tracks = None
        head, _sep, tail = album.rpartition(" | ")
        if head and tail.isdigit():
            album, n_tracks = head, int(tail)
        key = _album_seen_key(artist, album)
        if key:
            seen[key] = n_tracks
    return seen


def record_album_walk_seen(artist_name, album_dir, seen=None):
    """Append a decided 'Artist | Album | audio files' entry to the album-walk
    seen file.

    ``seen``, when given, is the caller's in-memory map from
    load_album_walk_seen(): used for the dedup check and updated on success,
    so the per-album record on a long walk doesn't re-read the whole file each
    time.
    """
    try:
        cfg.ALBUM_WALK_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = _album_seen_key(artist_name, album_dir.name)
        n_tracks = _count_audio_files_in(album_dir)
        known = seen if seen is not None else load_album_walk_seen()
        if key in known and known[key] == n_tracks:
            return
        _atomic_append(
            cfg.ALBUM_WALK_SEEN_FILE,
            header_lines=[
                "# Gap Fill decisions: albums you've answered "
                "y/N for during the Gap Fill walk.\n",
                "# Format: Artist | Album | audio files in the folder, one per "
                "line.\n",
                "# An album whose folder gains or loses tracks is offered "
                "again.\n",
                "# To revisit an album, delete its line below "
                "(or delete this whole file).\n",
                "# Comments start with '# ' (hash + space).\n",
                "\n",
            ],
            entry=f"{artist_name} | {album_dir.name} | {n_tracks}\n",
        )
        if seen is not None:
            seen[key] = n_tracks
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't write {cfg.ALBUM_WALK_SEEN_FILE.name}: {e}."))


# Outcomes that settle an album for the album-fill walk: it's done, or the
# user said no, or Qobuz couldn't place the folder or has nothing to fill it
# from (recorded so it stops nagging on every walk).
_ALBUM_WALK_DECIDED = {
    "already_complete", "user_skipped",
    "no_qobuz_match", "no_tracks", "false_match", "predicted_path_mismatch",
    "other_edition", "sibling_skipped",
}

# Folder names shown in a leftover category before it falls back to a count.
_NAMED_LEFTOVERS = 3


def _reset_command():
    return cli_command("--reset-walk-seen", interactive=False)


def _write_collection_snapshot():
    """Record the library after a completed CLI walk."""
    clear_scan_caches()
    try:
        previous = collection_snapshot.load_latest()
    except OSError:
        previous = None
    try:
        document = collection_snapshot.build_snapshot(
            previous=previous, source="scan")
        written, refusal = collection_snapshot.write_snapshot(document)
    except OSError as exc:
        log.warning(fmt(
            C.YELLOW,
            "  ⚠  The collection backup could not be written: "
            f"{collection_snapshot.write_error(exc)}.",
        ))
        return
    if not written:
        log.warning(fmt(C.YELLOW, f"  ⚠  {refusal}"))
        return
    counts = document["counts"]
    log.info(fmt(
        C.GRAY,
        f"  Collection backup updated: {plural(counts['albums'], 'album')} "
        f"by {plural(counts['artists'], 'artist')}.",
    ))


def _leftover_folder_note(label, names) -> str:
    """"{Label}: "Name", "Name" and N more" for a leftover album category."""
    names = [n for n in names if n]
    if not names:
        return ""
    shown = ", ".join(f"“{n}”" for n in names[:_NAMED_LEFTOVERS])
    extra = len(names) - _NAMED_LEFTOVERS
    if extra > 0:
        shown += f" and {extra:,} more"
    return f"{label}: {shown}"


def _download_queue(queue, args, token, save, results_out=None):
    """Download a walk's saved queue and return its results, whether it
    drained, and whether an album stopped for recovery now holds the saved
    queue. A queue that drains is cleared; one that does not stays saved."""
    log.info(fmt(C.CYAN,
        f"\n  ⟳  Downloading the queue ({plural(len(queue), 'album')})…"))
    results, drained = _execute_download_queue(
        queue, args, token, on_progress=lambda: save(queue),
        refresh_review=True, results_out=results_out)
    parked = False
    if not args.dry_run:
        if drained:
            clear_queue_journal()
        elif queue and queue_parked():
            parked = True
        elif queue:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {plural(len(queue), 'album')} couldn't be "
                "downloaded; kept in the queue to retry on the next run."))
    return results, drained, parked


def _ask_name_filter(all_artists):
    flt = ask("  Only artists whose name contains (Enter for all): ")
    if flt is None:
        return None
    if not flt:
        return all_artists
    artists = [a for a in all_artists if flt in a.name.lower()]
    log.info(fmt(C.GRAY, f"  {plural(len(artists), 'artist')} match {flt!r}."))
    return artists


def _attention_clause(discovery_errors, retry, own_files_left):
    if discovery_errors:
        return f"{plural(len(discovery_errors), 'artist')} could not be read"
    if retry:
        return "some albums need a retry"
    if own_files_left:
        return own_files_clause()
    return None


def run_album_walk_mode(args, token):
    """Album fill walk: scan every album under every artist and
    prompt only on incomplete ones."""
    return _AlbumWalk(args, token).run()


class _AlbumWalk:
    """One Gap Fill walk, its queue shared by every artist it scans."""

    def __init__(self, args, token):
        self.args = args
        self.token = token

    def run(self):
        banner("Gap Fill: fill missing tracks in albums you already own")
        early_exit = self._choose_artists()
        if early_exit is not None:
            return early_exit
        self._start()
        self._walk()
        self._settle_queue()
        return self._finish()

    def _choose_artists(self):
        all_artists, self.discovery_errors = _library_artists()
        if not all_artists:
            log.info(fmt(C.YELLOW, wrap(no_artist_folders(), indent="  ⚠  ",
                                        hanging="     ")))
            return EXIT_GENERAL if self.discovery_errors else 0

        vlog(f"  {plural(len(all_artists), 'artist')} in library.")
        self.artists = _ask_name_filter(all_artists)
        if self.artists is None:
            return EXIT_GENERAL
        if not self.artists:
            return EXIT_GENERAL if self.discovery_errors else 0

    def _start(self):
        self.hidden = hidden_mod.load()
        self.seen = load_album_walk_seen()
        if self.seen:
            vlog(f"  {plural(len(self.seen), 'album decision')} loaded from "
                 f"{cfg.ALBUM_WALK_SEEN_FILE.name}.")
        log.info(fmt(C.GRAY,
            "  Per album: y=queue, Enter=skip, d=download the queue now, s=stop."))
        log.info(fmt(C.GRAY, wrap(
            "Albums already complete or checked on earlier walks are skipped.",
            indent="  ", hanging="  ")))
        print()

        self.saved_consolidate = self.args.consolidate
        _consolidation_disabled_notice(self.args, "Gap Fill")
        self.args.consolidate = False

        self.shared_queue = []
        self.n_artists_scanned = 0
        self.n_albums_complete = 0
        self.n_albums_skipped = 0
        self.n_albums_dismissed = 0
        self.n_albums_unmatched = 0
        self.n_albums_unplaced = 0
        self.n_albums_unavailable = 0
        self.n_albums_unchecked = 0
        self.unmatched_names = []
        self.unplaced_names = []
        self.unavailable_names = []
        self.unchecked_names = []
        self.n_albums_filled = 0
        self.n_albums_left_out = 0
        self.interrupted = False
        self.walk_stopped = False
        self.no_answer = False
        self.partial_completion = False
        self.retry_needed = False
        self.own_files_left = False
        # An album stopped for recovery holds the saved queue until the next
        # launch, and a full one takes nothing more, so the walk ends there.
        self.parked = False
        self.queue_full = False

        self._save_queue = queue_saver("album_walk", dry_run=self.args.dry_run)

    def _save_now(self):
        n_queued = len(self.shared_queue)
        if not save_or_trim(self.shared_queue, self._save_queue):
            self.parked = True
        elif len(self.shared_queue) < n_queued:
            self.queue_full = True

    def _flush_queue(self):
        if not self.parked:
            self._save_now()
        if self.parked:
            return
        if not self.shared_queue:
            log.info(fmt(C.GRAY, "    The queue is empty."))
            return
        results = []
        try:
            _, drained, self.parked = _download_queue(
                self.shared_queue, self.args, self.token, self._save_queue, results_out=results)
        finally:
            self.n_albums_filled += sum(
                1 for r in results
                if r.get("result") in ("downloaded", "partial"))
        if self.args.dry_run:
            return
        self.partial_completion = self.partial_completion or any(
            r.get("result") == "partial" for r in results
        )
        self.retry_needed = not drained and bool(self.shared_queue)
        self.own_files_left = self.own_files_left or any(
            r.get("companions_unsettled") for r in results)

    def _walk(self):
        try:
            for i, artist_dir in enumerate(self.artists, 1):
                if self._walk_artist(i, artist_dir):
                    break
        finally:
            self.args.consolidate = self.saved_consolidate

    def _walk_artist(self, i, artist_dir):
        """Whether the walk stops after this artist."""
        artist_query = re.sub(r"\s+", " ",
                              artist_dir.name.replace("_", " ")).strip()
        if normalize(artist_query) in VA_NORMALIZED:
            vlog(f"  [{i}/{len(self.artists)}] Skipping '{artist_query}' "
                 "(not a real artist).")
            return False

        _albs = list_artist_album_dirs(artist_dir)
        seen_dirs = {ad for ad in _albs
                     if _album_seen_before(self.seen, artist_query, ad)}
        self.n_albums_left_out += len(seen_dirs)
        if _albs and len(seen_dirs) == len(_albs):
            vlog(f"  [{i}/{len(self.artists)}] {artist_query}: "
                 "all albums already decided; skipping.")
            return False

        print()
        qhint = (f" [queue: {len(self.shared_queue)}]"
                 if self.shared_queue else "")
        log.info(fmt(C.BOLD + C.CYAN,
            f"  [{i}/{len(self.artists)}] {artist_query}{qhint}"))

        try:
            clear_scan_caches()
            gap_fill_result = run_artist_gap_fill(
                artist_query, artist_dir, self.args, self.token,
                shared_queue=self.shared_queue,
                flush_callback=self._flush_queue,
                skip_predicate=lambda ad: (
                    self.parked or self.queue_full or ad in seen_dirs),
                save_callback=self._save_now,
                hidden=self.hidden,
                consolidate=self.saved_consolidate,
                numbered=False,
            )
            stopped = self._tally(artist_query, gap_fill_result)
            self.n_artists_scanned += 1
            if self.queue_full:
                log.info(fmt(C.GRAY, wrap(
                    "Stopping the album walk; the albums the saved queue "
                    "had no room for are offered again on the next walk.",
                    indent="  ", hanging="  ")))
                self.walk_stopped = True
                return True
            if stopped or self.no_answer or self.parked:
                log.info(fmt(C.GRAY, "  Stopping the album walk."))
                self.walk_stopped = True
                return True
        except KeyboardInterrupt:
            # Persist NOW: the current artist's just-approved albums are in
            # shared_queue (appended as decided) but save_callback only fired
            # for completed artists, so without this the in-progress artist's
            # approvals wouldn't actually reach disk despite the message.
            keep_queue(self.shared_queue, self._save_queue)
            self.interrupted = True
            return True
        except (AuthLost, QobuzUnavailable):
            # The artist's approvals so far are saved before the run exits
            # with the right status and message.
            keep_queue(self.shared_queue, self._save_queue)
            raise
        return False

    def _tally(self, artist_query, gap_fill_result):
        """Count and record each album's outcome; whether the user stopped."""
        stopped = False
        for r in (gap_fill_result[0] if gap_fill_result else []):
            _ad = r.get("dir")
            _result = r.get("result")
            if _result == "no_answer":
                self.no_answer = True
                continue
            if _ad is None or _result == "dry_run":
                continue
            if _result == "user_stopped":
                stopped = True
            if _result in _ALBUM_WALK_DECIDED and not self.args.dry_run:
                record_album_walk_seen(artist_query, _ad, self.seen)
            if _result == "already_complete":
                self.n_albums_complete += 1
            elif _result in ("user_skipped", "user_stopped"):
                self.n_albums_skipped += 1
            elif _result == "no_qobuz_match":
                self.n_albums_unmatched += 1
                self.unmatched_names.append(_ad.name)
            elif _result == "sibling_skipped":
                pass  # a folded duplicate folder; recorded seen, not summarized
            elif _result == "dismissed":
                self.n_albums_dismissed += 1
            elif _result == "other_edition":
                self.n_albums_unavailable += 1
                self.unavailable_names.append(_ad.name)
            elif _result in ("unchecked", "stale_candidate"):
                self.n_albums_unchecked += 1
                self.unchecked_names.append(_ad.name)
            else:
                # Matched a candidate then rejected it (false_match,
                # low_overlap, predicted_path_mismatch) or it had no
                # tracks; not the same as "no Qobuz match".
                self.n_albums_unplaced += 1
                self.unplaced_names.append(_ad.name)
        return stopped

    def _settle_queue(self):
        if self.parked:
            log.info(parked_queue_note())
        elif self.shared_queue and not self.interrupted and not self.args.dry_run:
            try:
                download_now = confirm(
                    f"\n  Walk {'stopped' if self.walk_stopped else 'done'}. "
                    f"{plural(len(self.shared_queue), 'album')} still queued. "
                    "Download now?", default_yes=True, auto_yes=self.args.yes)
            except KeyboardInterrupt:
                print()
                self.interrupted = True
                download_now = False
            if download_now:
                try:
                    self._flush_queue()
                except KeyboardInterrupt:
                    self.interrupted = True
                    log.info(fmt(C.YELLOW, "\n  ⚠  Downloading the queue was interrupted."))
                    keep_queue(self.shared_queue, self._save_queue)
                if self.parked:
                    log.info(parked_queue_note())
            else:
                keep_queue(self.shared_queue, self._save_queue)

    def _finish(self):
        print()
        needs_attention = (self.interrupted or self.no_answer or self.partial_completion
                           or self.retry_needed or self.own_files_left or self.parked
                           or bool(self.discovery_errors))
        log.info(fmt(C.GRAY,
            f"     Already complete: {self.n_albums_complete} · "
            f"Filled: {self.n_albums_filled}"))
        self._report_leftovers()
        if (self.n_artists_scanned and not needs_attention and not self.walk_stopped
                and not self.args.dry_run):
            _write_collection_snapshot()
        clause = _attention_clause(
            self.discovery_errors, self.partial_completion or self.retry_needed, self.own_files_left)
        if self.interrupted:
            log.info(fmt(C.GRAY, "  Interrupted."))
        elif clause:
            log.warning(fmt(C.YELLOW, f"  ⚠  Needs attention: {clause}."))
        elif self.walk_stopped:
            log.info(fmt(C.GRAY, "  Stopped."))
        else:
            log.info(fmt(C.GREEN, "  ✓  Done."))
        if self.interrupted:
            return EXIT_INTERRUPT
        return EXIT_GENERAL if needs_attention else 0

    def _report_leftovers(self):
        leftovers = []
        if self.n_albums_skipped:
            leftovers.append(f"skipped by you: {self.n_albums_skipped}")
        if self.n_albums_dismissed:
            leftovers.append(f"dismissed in the Library review: {self.n_albums_dismissed}")
        if self.n_albums_unmatched:
            leftovers.append(f"no Qobuz match: {self.n_albums_unmatched}")
        if self.n_albums_unplaced:
            leftovers.append(f"couldn't place: {self.n_albums_unplaced}")
        if self.n_albums_unavailable:
            leftovers.append(f"nothing available on Qobuz: {self.n_albums_unavailable}")
        if self.n_albums_unchecked:
            leftovers.append(f"couldn't check: {self.n_albums_unchecked}")
        if leftovers:
            log.info(fmt(C.GRAY, "    " + " · ".join(leftovers)))
        for label, names in (
            ("No Qobuz match", self.unmatched_names),
            ("Couldn't place", self.unplaced_names),
            ("Nothing available on Qobuz", self.unavailable_names),
            ("Couldn't check", self.unchecked_names),
        ):
            note = _leftover_folder_note(label, names)
            if note:
                log.info(fmt(C.GRAY, f"    {note}"))
        if self.n_albums_left_out:
            one = self.n_albums_left_out == 1
            log.info(fmt(C.GRAY, wrap(
                f"{plural(self.n_albums_left_out, 'album')} checked on earlier walks "
                f"{'was' if one else 'were'} left out. To bring "
                f"{'it' if one else 'them'} back:", indent="    ", hanging="    ")))
            log.info(fmt(C.GRAY, f"      {_reset_command()}"))


def run_walk_queued_mode(args, token):
    """Walk artists, accumulate decisions across artists, flush on demand."""
    return _QueuedWalk(args, token).run()


class _QueuedWalk:
    """One library walk, its queue shared by every artist it scans."""

    def __init__(self, args, token):
        self.args = args
        self.token = token

    def run(self):
        banner("Library walk: scan artists, queue, download when you choose")
        early_exit = self._choose_artists()
        if early_exit is not None:
            return early_exit
        self._start()
        try:
            self._walk()
        except KeyboardInterrupt:
            print()
            self.ctrl_c = True
            self.interrupted = True
        finally:
            self._settle_queue()
        return self._finish()

    def _choose_artists(self):
        all_artists, self.discovery_errors = _library_artists()
        if not all_artists:
            log.info(fmt(C.YELLOW, wrap(no_artist_folders(), indent="  ⚠  ",
                                        hanging="     ")))
            return EXIT_GENERAL if self.discovery_errors else 0

        # The web review's dismissals and this walk's own per-artist record are two
        # different decisions: one album you said no to, versus one artist you have
        # already been through. Both apply here.
        self.hidden = hidden_mod.load()

        self.seen = load_walk_seen()
        n_before = len(all_artists)
        all_artists = [a for a in all_artists if normalize(a.name) not in self.seen]
        n_hidden = n_before - len(all_artists)
        if n_hidden:
            one = n_hidden == 1
            log.info(fmt(C.GRAY, wrap(
                f"{plural(n_hidden, 'artist')} answered on earlier walks "
                f"{'is' if one else 'are'} left out. To bring "
                f"{'it' if one else 'them'} back:", indent="  ", hanging="  ")))
            log.info(fmt(C.GRAY, f"    {_reset_command()}"))
        if not all_artists:
            if not self.discovery_errors:
                log.info(fmt(C.GREEN, "  ✓  All artists already decided."))
            return EXIT_GENERAL if self.discovery_errors else 0

        vlog(f"  {plural(len(all_artists), 'artist')} to walk.")
        self.artists = _ask_name_filter(all_artists)
        if self.artists is None:
            return EXIT_GENERAL
        if not self.artists:
            return EXIT_GENERAL if self.discovery_errors else 0

    def _start(self):
        log.info(fmt(C.GRAY,
            "  Per artist: y=scan and queue, Enter=skip, d=download the queue now,"))
        log.info(fmt(C.GRAY,
            "              s=stop, f <text>=only the rest whose names contain it."))
        print()

        self.shared_queue = []
        self.saved_consolidate = self.args.consolidate
        _consolidation_disabled_notice(self.args, "Library walk")
        self.args.consolidate = False

        self._save_queue = queue_saver("walk_queue", dry_run=self.args.dry_run)
        self.partial_completion = False
        self.retry_needed = False
        self.own_files_left = False
        # An album stopped for recovery holds the saved queue until the next
        # launch, and a full one takes nothing more, so the walk ends there.
        self.parked = False
        self.queue_full = False

        self.n_scanned = 0
        self.n_skipped = 0
        self.interrupted = False
        self.walk_stopped = False
        self.ctrl_c = False

    def _save_now(self):
        n_queued = len(self.shared_queue)
        if not save_or_trim(self.shared_queue, self._save_queue):
            self.parked = True
        elif len(self.shared_queue) < n_queued:
            self.queue_full = True

    def _flush_queue(self):
        self._save_now()
        if self.parked:
            return
        if not self.shared_queue:
            log.info(fmt(C.GRAY, "  The queue is empty."))
            return
        results, drained, self.parked = _download_queue(
            self.shared_queue, self.args, self.token, self._save_queue)
        if not self.args.dry_run:
            self.partial_completion = self.partial_completion or any(
                r.get("result") == "partial" for r in results
            )
            self.retry_needed = not drained and bool(self.shared_queue)
            self.own_files_left = self.own_files_left or any(
                r.get("companions_unsettled") for r in results)

    def _walk(self):
        i = 0
        while i < len(self.artists):
            d = self.artists[i]
            _flush_stdin()
            qsize = len(self.shared_queue)
            qhint = f" [queue: {qsize}]" if qsize else ""
            r = ask(
                f"  [{i + 1}/{len(self.artists)}] {truncate(d.name, 50)}"
                f"{qhint}: scan? [y/N/d/s/f]: ", lower=False)
            if r is None:
                self.interrupted = True
                break
            rl = r.lower()

            if rl in ("s", "q", "quit"):
                log.info(fmt(C.GRAY, "  Stopping walk."))
                self.walk_stopped = True
                break
            if rl in ("d", "p"):
                self._flush_queue()
                if self.parked:
                    break
                continue
            if rl == "f":
                log.info(fmt(C.GRAY,
                    "  Type f and part of a name, for example 'f mar'."))
                continue
            if rl.startswith("f "):
                sub = r[2:].strip().lower()
                if sub:
                    remaining = [a for a in self.artists[i:]
                                 if sub in a.name.lower()]
                    if remaining:
                        self.artists = self.artists[:i] + remaining
                        log.info(fmt(C.GRAY,
                            f"  Filtered to {plural(len(remaining), 'artist')}."))
                    else:
                        log.info(fmt(C.YELLOW,
                            f"  No remaining artists match {sub!r}."))
                continue

            decided = True
            if rl in ("y", "yes"):
                decided, stop = self._scan_artist(d)
                if stop:
                    break
            elif rl in ("", "n", "no"):
                self.n_skipped += 1
            else:
                log.info(fmt(C.YELLOW,
                    f"  Unrecognized: {r!r}. Use y/N/d/s/f."))
                continue

            # --dry-run is preview-only: don't record the artist as decided, or
            # the next real walk would silently skip it (mirrors the album walk's
            # dry-run skip and --reset-walk-seen's dry-run guard).
            if decided and not self.args.dry_run:
                record_walk_seen(d.name, self.seen)
            i += 1

    def _scan_artist(self, d):
        """Whether the artist is decided, and whether the walk stops here."""
        # d came straight from list_library_artists(), so it already is
        # the artist folder to scan; no need to fuzzy-resolve it again.
        artist_query = re.sub(r"\s+", " ",
                              d.name.replace("_", " ")).strip()
        if normalize(artist_query) in VA_NORMALIZED:
            log.info(fmt(C.YELLOW,
                "  ⚠  'Various Artists' isn't a real artist; skipping."))
            return True, False
        print()
        banner(f"Scanning: {artist_query}")
        try:
            decided, stop = self._scan(d, artist_query)
        except KeyboardInterrupt:
            log.info(fmt(C.GRAY,
                "\n  Artist scan interrupted; stopping the walk."))
            self.ctrl_c = True
            self.interrupted = True
            decided = False
            # Stop, like the album walk's Ctrl-C does; the message
            # above promises the walk ends here.
            return decided, True
        if stop:
            return decided, stop

        self.n_scanned += 1
        return self._offer_download(d, decided)

    def _scan(self, d, artist_query):
        decided = True
        clear_scan_caches()
        gap_fill_result = run_artist_gap_fill(
            artist_query, d, self.args, self.token,
            shared_queue=self.shared_queue,
            save_callback=self._save_now,
            hidden=self.hidden,
            consolidate=self.saved_consolidate,
        )
        (step_one, owned_titles, handled_ids, resolved_dirs,
         artist_id, prefetched_catalog) = gap_fill_result
        if any(r.get("result") == "no_answer"
               for r in step_one):
            # Nothing was answered, so the artist is not
            # recorded as decided.
            self.interrupted = True
            decided = False
            return decided, True
        if any(r.get("result") == "user_stopped"
               for r in step_one):
            # Stopped before its albums were all offered, so
            # the artist is offered again on the next walk.
            decided = False
        elif not self.args.no_catalog:
            _, step_two_attention = run_artist_missing_albums(
                artist_query, owned_titles,
                self.args, self.token,
                artist_id=artist_id,
                handled_ids=handled_ids,
                resolved_dirs=resolved_dirs,
                prefetched_catalog=prefetched_catalog,
                shared_queue=self.shared_queue,
                hidden=self.hidden,
                artist_dir=d,
            )
            self.partial_completion = (
                self.partial_completion or step_two_attention
            )
            # An unanswered or unfinished step 2 leaves the
            # artist to be offered again.
            decided = not step_two_attention
            self._save_now()
        if self.queue_full:
            decided = False
        return decided, False

    def _offer_download(self, d, decided):
        if self.parked or self.queue_full:
            if self.queue_full:
                log.info(fmt(C.GRAY, wrap(
                    "Stopping the walk; the albums the saved "
                    "queue had no room for are offered again on "
                    "the next walk.", indent="  ", hanging="  ")))
            self.walk_stopped = True
            if decided and not self.args.dry_run:
                record_walk_seen(d.name, self.seen)
            return decided, True
        if self.shared_queue:
            print()
            if confirm(
                    f"  Download the queue now ({plural(len(self.shared_queue), 'album')})?",
                    default_yes=False, auto_yes=self.args.yes):
                self._flush_queue()
        print()
        if self.parked:
            if decided and not self.args.dry_run:
                record_walk_seen(d.name, self.seen)
            return decided, True
        return decided, False

    def _settle_queue(self):
        self.args.consolidate = self.saved_consolidate
        if self.parked:
            log.info(parked_queue_note())
        elif self.shared_queue and (
            self.ctrl_c
            or isinstance(sys.exc_info()[1], (AuthLost, QobuzUnavailable))
        ):
            # Ctrl-C means stop, and after a lost token or an unreachable API
            # a flush would only fail again, so keep the queue without asking.
            keep_queue(self.shared_queue, self._save_queue)
        elif self.shared_queue:
            print()
            log.info(fmt(C.YELLOW,
                f"  {plural(len(self.shared_queue), 'album')} still queued."))
            try:
                if confirm("  Download them before exiting?",
                           default_yes=True, auto_yes=self.args.yes):
                    try:
                        self._flush_queue()
                    except KeyboardInterrupt:
                        self.ctrl_c = True
                        self.interrupted = True
                        log.info(fmt(C.YELLOW,
                            "\n  ⚠  Downloading the queue was interrupted."))
                        keep_queue(self.shared_queue, self._save_queue)
                    if self.parked:
                        log.info(parked_queue_note())
                else:
                    keep_queue(self.shared_queue, self._save_queue)
            except KeyboardInterrupt:
                self.ctrl_c = True
                self.interrupted = True
                keep_queue(self.shared_queue, self._save_queue)

    def _finish(self):
        print()
        needs_attention = (self.interrupted or self.partial_completion or self.retry_needed
                           or self.own_files_left or self.parked or bool(self.discovery_errors))
        log.info(fmt(C.GRAY, f"     Scanned {self.n_scanned}, skipped {self.n_skipped}."))
        if (self.n_scanned and not needs_attention and not self.walk_stopped
                and not self.args.dry_run):
            _write_collection_snapshot()
        clause = _attention_clause(
            self.discovery_errors, self.partial_completion or self.retry_needed, self.own_files_left)
        if self.interrupted:
            log.info(fmt(C.GRAY, "  Interrupted." if self.ctrl_c else "  Stopped."))
        elif clause:
            log.warning(fmt(C.YELLOW, f"  ⚠  Needs attention: {clause}."))
        elif self.walk_stopped:
            log.info(fmt(C.GRAY, "  Stopped."))
        else:
            log.info(fmt(C.GREEN, "  ✓  Done."))
        if self.ctrl_c:
            return EXIT_INTERRUPT
        return EXIT_GENERAL if needs_attention else 0
