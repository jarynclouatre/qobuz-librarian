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
    no_artist_folders,
    plural,
    unreadable_artist,
)
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm


def load_walk_seen():
    """Load set of normalized artist names already decided in walk mode."""
    if not cfg.WALK_SEEN_FILE.exists():
        return set()
    seen = set()
    try:
        with open(cfg.WALK_SEEN_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                # Only "# " (hash + SPACE) is a header comment; an artist folder
                # like "#1 Dads" or "#####" is a real entry and must load back,
                # or it gets re-prompted every walk and the file grows each time.
                if not s or s.startswith("# "):
                    continue
                n = normalize(s)
                if n:
                    seen.add(n)
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Couldn't read {cfg.WALK_SEEN_FILE.name}: {e}."))
    return seen


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
    if not cfg.ALBUM_WALK_SEEN_FILE.exists():
        return {}
    seen = {}
    try:
        with open(cfg.ALBUM_WALK_SEEN_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                # Only "# " (hash + SPACE) is a header comment; an artist folder
                # like "#1 Dads" or "#####" is a real entry and must load back,
                # or it gets re-prompted every walk and the file grows each time.
                if not s or s.startswith("# "):
                    continue
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
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't read {cfg.ALBUM_WALK_SEEN_FILE.name}: {e}."))
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


def run_album_walk_mode(args, token):
    """Album fill walk: scan every album under every artist and
    prompt only on incomplete ones."""
    banner("Gap Fill: fill missing tracks in albums you already own")

    discovery_errors = {}

    def artist_read_failed(path, error):
        discovery_errors[path.name] = str(error)
        log.warning(unreadable_artist(path.name, error))

    all_artists = list_library_artists(on_artist_error=artist_read_failed)
    if not all_artists:
        log.info(fmt(C.YELLOW, wrap(no_artist_folders(), indent="  ⚠  ",
                                    hanging="     ")))
        return EXIT_GENERAL if discovery_errors else 0

    vlog(f"  {plural(len(all_artists), 'artist')} in library.")
    flt = ask("  Only artists whose name contains (Enter for all): ")
    if flt is None:
        return EXIT_GENERAL
    artists = all_artists
    if flt:
        artists = [a for a in all_artists if flt in a.name.lower()]
        log.info(fmt(C.GRAY, f"  {plural(len(artists), 'artist')} match {flt!r}."))
        if not artists:
            return EXIT_GENERAL if discovery_errors else 0

    hidden = hidden_mod.load()
    seen = load_album_walk_seen()
    if seen:
        vlog(f"  {plural(len(seen), 'album decision')} loaded from "
             f"{cfg.ALBUM_WALK_SEEN_FILE.name}.")
    log.info(fmt(C.GRAY,
        "  Per album: y=queue, enter/N=skip, d=download queue now, s=stop."))
    log.info(fmt(C.GRAY, wrap(
        "Albums already complete or checked on earlier walks are skipped; "
        "--reset-walk-seen brings the checked ones back.",
        indent="  ", hanging="  ")))
    print()

    saved_consolidate = args.consolidate
    _consolidation_disabled_notice(args, "Gap Fill")
    args.consolidate = False

    shared_queue = []
    n_artists_scanned = 0
    n_albums_complete = 0
    n_albums_skipped = 0
    n_albums_dismissed = 0
    n_albums_unmatched = 0
    n_albums_unplaced = 0
    n_albums_unavailable = 0
    n_albums_unchecked = 0
    unmatched_names = []
    unplaced_names = []
    unavailable_names = []
    unchecked_names = []
    n_albums_filled = 0
    n_albums_left_out = 0
    interrupted = False
    walk_stopped = False
    no_answer = False
    partial_completion = False
    retry_needed = False
    own_files_left = False
    # An album stopped for recovery holds the saved queue until the next
    # launch, and a full one takes nothing more, so the walk ends there.
    parked = False
    queue_full = False

    _save_queue = queue_saver("album_walk", dry_run=args.dry_run)

    def _save_now():
        nonlocal parked, queue_full
        n_queued = len(shared_queue)
        if not save_or_trim(shared_queue, _save_queue):
            parked = True
        elif len(shared_queue) < n_queued:
            queue_full = True

    def _flush_queue():
        nonlocal n_albums_filled, partial_completion, retry_needed, own_files_left
        nonlocal parked
        if not parked:
            _save_now()
        if parked:
            return
        if not shared_queue:
            log.info(fmt(C.GRAY, "    The queue is empty."))
            return
        results = []
        try:
            _, drained, parked = _download_queue(
                shared_queue, args, token, _save_queue, results_out=results)
        finally:
            n_albums_filled += sum(
                1 for r in results
                if r.get("result") in ("downloaded", "partial"))
        if args.dry_run:
            return
        partial_completion = partial_completion or any(
            r.get("result") == "partial" for r in results
        )
        retry_needed = not drained and bool(shared_queue)
        own_files_left = own_files_left or any(
            r.get("companions_unsettled") for r in results)

    try:
        for i, artist_dir in enumerate(artists, 1):
            artist_query = re.sub(r"\s+", " ",
                                  artist_dir.name.replace("_", " ")).strip()
            if normalize(artist_query) in VA_NORMALIZED:
                vlog(f"  [{i}/{len(artists)}] Skipping '{artist_query}' "
                     "(not a real artist).")
                continue

            _albs = list_artist_album_dirs(artist_dir)
            seen_dirs = {ad for ad in _albs
                         if _album_seen_before(seen, artist_query, ad)}
            n_albums_left_out += len(seen_dirs)
            if _albs and len(seen_dirs) == len(_albs):
                vlog(f"  [{i}/{len(artists)}] {artist_query}: "
                     "all albums already decided; skipping.")
                continue

            print()
            qhint = (f" [queue: {len(shared_queue)}]"
                     if shared_queue else "")
            log.info(fmt(C.BOLD + C.CYAN,
                f"  [{i}/{len(artists)}] {artist_query}{qhint}"))


            try:
                clear_scan_caches()
                gap_fill_result = run_artist_gap_fill(
                    artist_query, artist_dir, args, token,
                    shared_queue=shared_queue,
                    flush_callback=_flush_queue,
                    skip_predicate=lambda ad: (
                        parked or queue_full or ad in seen_dirs),
                    save_callback=_save_now,
                    hidden=hidden,
                    consolidate=saved_consolidate,
                    numbered=False,
                )
                stopped = False
                for r in (gap_fill_result[0] if gap_fill_result else []):
                    _ad = r.get("dir")
                    _result = r.get("result")
                    if _result == "no_answer":
                        no_answer = True
                        continue
                    if _ad is None or _result == "dry_run":
                        continue
                    if _result == "user_stopped":
                        stopped = True
                    if _result in _ALBUM_WALK_DECIDED and not args.dry_run:
                        record_album_walk_seen(artist_query, _ad, seen)
                    if _result == "already_complete":
                        n_albums_complete += 1
                    elif _result in ("user_skipped", "user_stopped"):
                        n_albums_skipped += 1
                    elif _result == "no_qobuz_match":
                        n_albums_unmatched += 1
                        unmatched_names.append(_ad.name)
                    elif _result == "sibling_skipped":
                        pass  # a folded duplicate folder; recorded seen, not summarized
                    elif _result == "dismissed":
                        n_albums_dismissed += 1
                    elif _result == "other_edition":
                        n_albums_unavailable += 1
                        unavailable_names.append(_ad.name)
                    elif _result == "unchecked":
                        n_albums_unchecked += 1
                        unchecked_names.append(_ad.name)
                    else:
                        # Matched a candidate then rejected it (false_match,
                        # low_overlap, predicted_path_mismatch) or it had no
                        # tracks; not the same as "no Qobuz match".
                        n_albums_unplaced += 1
                        unplaced_names.append(_ad.name)
                n_artists_scanned += 1
                if queue_full:
                    log.info(fmt(C.GRAY, wrap(
                        "Stopping the album walk; the albums the saved queue "
                        "had no room for are offered again on the next walk.",
                        indent="  ", hanging="  ")))
                    walk_stopped = True
                    break
                if stopped or no_answer or parked:
                    log.info(fmt(C.GRAY, "  Stopping the album walk."))
                    walk_stopped = True
                    break
            except KeyboardInterrupt:
                # Persist NOW: the current artist's just-approved albums are in
                # shared_queue (appended as decided) but save_callback only fired
                # for completed artists, so without this the in-progress artist's
                # approvals wouldn't actually reach disk despite the message.
                keep_queue(shared_queue, _save_queue)
                interrupted = True
                break
            except (AuthLost, QobuzUnavailable):
                # The artist's approvals so far are saved before the run exits
                # with the right status and message.
                keep_queue(shared_queue, _save_queue)
                raise
    finally:
        args.consolidate = saved_consolidate

    if parked:
        log.info(parked_queue_note())
    elif shared_queue and not interrupted and not args.dry_run:
        try:
            download_now = confirm(
                f"\n  Walk {'stopped' if walk_stopped else 'done'}. "
                f"{plural(len(shared_queue), 'album')} still queued. "
                "Download now?", default_yes=True, auto_yes=args.yes)
        except KeyboardInterrupt:
            print()
            interrupted = True
            download_now = False
        if download_now:
            try:
                _flush_queue()
            except KeyboardInterrupt:
                interrupted = True
                log.info(fmt(C.YELLOW, "\n  ⚠  Downloading the queue was interrupted."))
                keep_queue(shared_queue, _save_queue)
            if parked:
                log.info(parked_queue_note())
        else:
            keep_queue(shared_queue, _save_queue)

    print()
    needs_attention = (interrupted or no_answer or partial_completion
                       or retry_needed or own_files_left or parked
                       or bool(discovery_errors))
    log.info(fmt(C.GRAY,
        f"     Already complete: {n_albums_complete} · "
        f"Filled: {n_albums_filled}"))
    leftovers = []
    if n_albums_skipped:
        leftovers.append(f"skipped by you: {n_albums_skipped}")
    if n_albums_dismissed:
        leftovers.append(f"dismissed in the Library review: {n_albums_dismissed}")
    if n_albums_unmatched:
        leftovers.append(f"no Qobuz match: {n_albums_unmatched}")
    if n_albums_unplaced:
        leftovers.append(f"couldn't place: {n_albums_unplaced}")
    if n_albums_unavailable:
        leftovers.append(f"nothing available on Qobuz: {n_albums_unavailable}")
    if n_albums_unchecked:
        leftovers.append(f"couldn't check: {n_albums_unchecked}")
    if leftovers:
        log.info(fmt(C.GRAY, "    " + " · ".join(leftovers)))
    for label, names in (
        ("No Qobuz match", unmatched_names),
        ("Couldn't place", unplaced_names),
        ("Nothing available on Qobuz", unavailable_names),
        ("Couldn't check", unchecked_names),
    ):
        note = _leftover_folder_note(label, names)
        if note:
            log.info(fmt(C.GRAY, f"    {note}"))
    if n_albums_left_out:
        one = n_albums_left_out == 1
        log.info(fmt(C.GRAY, wrap(
            f"{plural(n_albums_left_out, 'album')} checked on earlier walks "
            f"{'was' if one else 'were'} left out; --reset-walk-seen brings "
            f"{'it' if one else 'them'} back.", indent="    ", hanging="    ")))
    if (n_artists_scanned and not needs_attention and not walk_stopped
            and not args.dry_run):
        _write_collection_snapshot()
    clause = None
    if discovery_errors:
        clause = f"{plural(len(discovery_errors), 'artist')} could not be read"
    elif partial_completion or retry_needed:
        clause = "some albums need a retry"
    elif own_files_left:
        clause = own_files_clause()
    if interrupted:
        log.info(fmt(C.GRAY, "  Interrupted."))
    elif clause:
        log.warning(fmt(C.YELLOW, f"  ⚠  Needs attention: {clause}."))
    elif walk_stopped:
        log.info(fmt(C.GRAY, "  Stopped."))
    else:
        log.info(fmt(C.GREEN, "  ✓  Done."))
    if interrupted:
        return EXIT_INTERRUPT
    return EXIT_GENERAL if needs_attention else 0


def run_walk_queued_mode(args, token):
    """Walk artists, accumulate decisions across artists, flush on demand."""
    banner("Library walk: scan artists, queue, download when you choose")

    discovery_errors = {}

    def artist_read_failed(path, error):
        discovery_errors[path.name] = str(error)
        log.warning(unreadable_artist(path.name, error))

    all_artists = list_library_artists(on_artist_error=artist_read_failed)
    if not all_artists:
        log.info(fmt(C.YELLOW, wrap(no_artist_folders(), indent="  ⚠  ",
                                    hanging="     ")))
        return EXIT_GENERAL if discovery_errors else 0

    # The web review's dismissals and this walk's own per-artist record are two
    # different decisions: one album you said no to, versus one artist you have
    # already been through. Both apply here.
    hidden = hidden_mod.load()

    seen = load_walk_seen()
    n_before = len(all_artists)
    all_artists = [a for a in all_artists if normalize(a.name) not in seen]
    n_hidden = n_before - len(all_artists)
    if n_hidden:
        one = n_hidden == 1
        log.info(fmt(C.GRAY, wrap(
            f"{plural(n_hidden, 'artist')} answered on earlier walks "
            f"{'is' if one else 'are'} left out; --reset-walk-seen brings "
            f"{'it' if one else 'them'} back.", indent="  ", hanging="  ")))
    if not all_artists:
        if not discovery_errors:
            log.info(fmt(C.GREEN, "  ✓  All artists already decided."))
        return EXIT_GENERAL if discovery_errors else 0

    vlog(f"  {plural(len(all_artists), 'artist')} to walk.")
    flt = ask("  Only artists whose name contains (Enter for all): ")
    if flt is None:
        return EXIT_GENERAL
    artists = all_artists
    if flt:
        artists = [a for a in all_artists if flt in a.name.lower()]
        log.info(fmt(C.GRAY, f"  {plural(len(artists), 'artist')} match {flt!r}."))
        if not artists:
            return EXIT_GENERAL if discovery_errors else 0

    log.info(fmt(C.GRAY,
        "  Per artist: y=scan and queue, Enter/n=skip, p=download the queue,"))
    log.info(fmt(C.GRAY,
        "             s=stop, f <text>=only the rest whose names contain it."))
    print()

    shared_queue = []
    saved_consolidate = args.consolidate
    _consolidation_disabled_notice(args, "Library walk")
    args.consolidate = False

    _save_queue = queue_saver("walk_queue", dry_run=args.dry_run)
    partial_completion = False
    retry_needed = False
    own_files_left = False
    # An album stopped for recovery holds the saved queue until the next
    # launch, and a full one takes nothing more, so the walk ends there.
    parked = False
    queue_full = False

    def _save_now():
        nonlocal parked, queue_full
        n_queued = len(shared_queue)
        if not save_or_trim(shared_queue, _save_queue):
            parked = True
        elif len(shared_queue) < n_queued:
            queue_full = True

    def _flush_queue():
        nonlocal partial_completion, retry_needed, own_files_left, parked
        _save_now()
        if parked:
            return
        if not shared_queue:
            log.info(fmt(C.GRAY, "  The queue is empty."))
            return
        results, drained, parked = _download_queue(
            shared_queue, args, token, _save_queue)
        if not args.dry_run:
            partial_completion = partial_completion or any(
                r.get("result") == "partial" for r in results
            )
            retry_needed = not drained and bool(shared_queue)
            own_files_left = own_files_left or any(
                r.get("companions_unsettled") for r in results)

    n_scanned = 0
    n_skipped = 0
    interrupted = False
    walk_stopped = False
    ctrl_c = False
    i = 0

    try:
        while i < len(artists):
            d = artists[i]
            _flush_stdin()
            qsize = len(shared_queue)
            qhint = f" [queue: {qsize}]" if qsize else ""
            r = ask(
                f"  [{i + 1}/{len(artists)}] {truncate(d.name, 50)}"
                f"{qhint}: scan? [y/N/p/s/f]: ", lower=False)
            if r is None:
                interrupted = True
                break
            rl = r.lower()

            if rl in ("s", "q", "quit"):
                log.info(fmt(C.GRAY, "  Stopping walk."))
                walk_stopped = True
                break
            if rl == "p":
                _flush_queue()
                if parked:
                    break
                continue
            if rl == "f":
                log.info(fmt(C.GRAY,
                    "  Type f and part of a name, for example 'f mar'."))
                continue
            if rl.startswith("f "):
                sub = r[2:].strip().lower()
                if sub:
                    remaining = [a for a in artists[i:]
                                 if sub in a.name.lower()]
                    if remaining:
                        artists = artists[:i] + remaining
                        log.info(fmt(C.GRAY,
                            f"  Filtered to {plural(len(remaining), 'artist')}."))
                    else:
                        log.info(fmt(C.YELLOW,
                            f"  No remaining artists match {sub!r}."))
                continue

            decided = True
            if rl in ("y", "yes"):
                # d came straight from list_library_artists(), so it already is
                # the artist folder to scan; no need to fuzzy-resolve it again.
                artist_query = re.sub(r"\s+", " ",
                                      d.name.replace("_", " ")).strip()
                if normalize(artist_query) in VA_NORMALIZED:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  'Various Artists' isn't a real artist; skipping."))
                else:
                    print()
                    banner(f"Scanning: {artist_query}")
                    try:
                        clear_scan_caches()
                        gap_fill_result = run_artist_gap_fill(
                            artist_query, d, args, token,
                            shared_queue=shared_queue,
                            save_callback=_save_now,
                            hidden=hidden,
                            consolidate=saved_consolidate,
                        )
                        (step_one, owned_titles, handled_ids, resolved_dirs,
                         artist_id, prefetched_catalog) = gap_fill_result
                        if any(r.get("result") == "no_answer"
                               for r in step_one):
                            # Nothing was answered, so the artist is not
                            # recorded as decided.
                            interrupted = True
                            decided = False
                            break
                        if any(r.get("result") == "user_stopped"
                               for r in step_one):
                            # Stopped before its albums were all offered, so
                            # the artist is offered again on the next walk.
                            decided = False
                        elif not args.no_catalog:
                            _, step_two_attention = run_artist_missing_albums(
                                artist_query, owned_titles,
                                args, token,
                                artist_id=artist_id,
                                handled_ids=handled_ids,
                                resolved_dirs=resolved_dirs,
                                prefetched_catalog=prefetched_catalog,
                                shared_queue=shared_queue,
                                hidden=hidden,
                                artist_dir=d,
                            )
                            partial_completion = (
                                partial_completion or step_two_attention
                            )
                            # An unanswered or unfinished step 2 leaves the
                            # artist to be offered again.
                            decided = not step_two_attention
                            _save_now()
                        if queue_full:
                            decided = False
                    except KeyboardInterrupt:
                        log.info(fmt(C.GRAY,
                            "\n  Artist scan interrupted; stopping the walk."))
                        ctrl_c = True
                        interrupted = True
                        decided = False
                        # Stop, like the album walk's Ctrl-C does; the message
                        # above promises the walk ends here.
                        break

                    n_scanned += 1

                    if parked or queue_full:
                        if queue_full:
                            log.info(fmt(C.GRAY, wrap(
                                "Stopping the walk; the albums the saved "
                                "queue had no room for are offered again on "
                                "the next walk.", indent="  ", hanging="  ")))
                        walk_stopped = True
                        if decided and not args.dry_run:
                            record_walk_seen(d.name, seen)
                        break
                    if shared_queue:
                        print()
                        if confirm(
                                f"  Download the queue now ({plural(len(shared_queue), 'album')})?",
                                default_yes=False, auto_yes=args.yes):
                            _flush_queue()
                    print()
                    if parked:
                        if decided and not args.dry_run:
                            record_walk_seen(d.name, seen)
                        break
            elif rl in ("", "n", "no"):
                n_skipped += 1
            else:
                log.info(fmt(C.YELLOW,
                    f"  Unrecognized: {r!r}. Use y/N/p/s/f."))
                continue

            # --dry-run is preview-only: don't record the artist as decided, or
            # the next real walk would silently skip it (mirrors the album walk's
            # dry-run skip and --reset-walk-seen's dry-run guard).
            if decided and not args.dry_run:
                record_walk_seen(d.name, seen)
            i += 1
    except KeyboardInterrupt:
        print()
        ctrl_c = True
        interrupted = True
    finally:
        args.consolidate = saved_consolidate
        if parked:
            log.info(parked_queue_note())
        elif shared_queue and (
            ctrl_c
            or isinstance(sys.exc_info()[1], (AuthLost, QobuzUnavailable))
        ):
            # Ctrl-C means stop, and after a lost token or an unreachable API
            # a flush would only fail again, so keep the queue without asking.
            keep_queue(shared_queue, _save_queue)
        elif shared_queue:
            print()
            log.info(fmt(C.YELLOW,
                f"  {plural(len(shared_queue), 'album')} still queued."))
            try:
                if confirm("  Download them before exiting?",
                           default_yes=True, auto_yes=args.yes):
                    try:
                        _flush_queue()
                    except KeyboardInterrupt:
                        ctrl_c = True
                        interrupted = True
                        log.info(fmt(C.YELLOW,
                            "\n  ⚠  Downloading the queue was interrupted."))
                        keep_queue(shared_queue, _save_queue)
                    if parked:
                        log.info(parked_queue_note())
                else:
                    keep_queue(shared_queue, _save_queue)
            except KeyboardInterrupt:
                ctrl_c = True
                interrupted = True
                keep_queue(shared_queue, _save_queue)

    print()
    needs_attention = (interrupted or partial_completion or retry_needed
                       or own_files_left or parked or bool(discovery_errors))
    log.info(fmt(C.GRAY, f"     Scanned {n_scanned}, skipped {n_skipped}."))
    if (n_scanned and not needs_attention and not walk_stopped
            and not args.dry_run):
        _write_collection_snapshot()
    clause = None
    if discovery_errors:
        clause = f"{plural(len(discovery_errors), 'artist')} could not be read"
    elif partial_completion or retry_needed:
        clause = "some albums need a retry"
    elif own_files_left:
        clause = own_files_clause()
    if interrupted:
        log.info(fmt(C.GRAY, "  Interrupted." if ctrl_c else "  Stopped."))
    elif clause:
        log.warning(fmt(C.YELLOW, f"  ⚠  Needs attention: {clause}."))
    elif walk_stopped:
        log.info(fmt(C.GRAY, "  Stopped."))
    else:
        log.info(fmt(C.GREEN, "  ✓  Done."))
    if ctrl_c:
        return EXIT_INTERRUPT
    return EXIT_GENERAL if needs_attention else 0
