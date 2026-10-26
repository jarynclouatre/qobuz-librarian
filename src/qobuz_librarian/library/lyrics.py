"""Library-wide lyric backfill: fetch lyrics for tracks already on disk.

The manual counterpart to the import-time hook in ``integrations/lyrics.py``:
that hook lyrics each album as it's downloaded, this walks the whole library
and fills in whatever's missing. Both lean on the same provider engine
(``integrations/lyric_fetch``) and the same per-track state file, so a backfill
skips tracks the import hook already lyriced and vice-versa.

Local apart from the provider HTTP; no Qobuz login needed. Honours the
``LYRICS_FORMAT`` / ``LYRICS_PROVIDERS`` settings; ``LYRICS_ENABLED`` only
gates the automatic import-time fetch, so an explicit backfill runs regardless.
"""
from collections import Counter

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.library.scanner import (
    is_library_system_folder,
    iter_tree_no_symlinks,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.ui_cli.errors import plural, unreadable_artist
from qobuz_librarian.ui_cli.logging import log as _default_log
from qobuz_librarian.ui_cli.logging import report_progress

HAVE_LYRICS = lyric_fetch.AVAILABLE


def iter_library_flacs(*, artist_dirs=None, on_artist_error=None):
    """Yield ``(path, mtime, size)`` for every FLAC under the given artist dirs.

    Walks artists then albums via the same listing the rest of the app uses,
    so the dot-folder, staging-dir and empty-folder skips all apply. The
    stat is taken once here and handed to both the index and fetch passes so
    neither has to re-stat the file.

    ``artist_dirs=None`` walks every artist under MUSIC_ROOT (the whole-library
    backfill). Pass a list to walk only those artists.
    """
    if on_artist_error is None:
        def on_artist_error(path, error):
            _default_log.warning(unreadable_artist(path.name, error))
    artists = (artist_dirs if artist_dirs is not None else
               list_library_artists(on_artist_error=on_artist_error))
    for artist_dir in artists:
        walk_errors = []
        albums = list_artist_album_dirs(artist_dir, walk_errors=walk_errors)
        for album_dir in albums:
            try:
                flacs = sorted(
                    p for p in iter_tree_no_symlinks(album_dir, errors=walk_errors)
                    if p.is_file() and p.suffix.lower() == ".flac"
                    and not any(is_library_system_folder(part)
                                for part in p.relative_to(album_dir).parts))
            except OSError as e:
                walk_errors.append(f"{album_dir}: {e}")
                continue
            for fp in flacs:
                try:
                    st = fp.stat()
                except OSError as e:
                    walk_errors.append(f"{fp}: {e}")
                    continue
                yield fp, st.st_mtime, st.st_size
        if walk_errors:
            on_artist_error(artist_dir, "; ".join(map(str, walk_errors)))


def run_library_lyrics(*, dry_run=False, rescan=False, synced_only=False,
                       should_stop=None, log=None, artist_dirs=None):
    """Fetch lyrics across the whole library (or a subset of artists).

    Returns the fetch engine's outcome counts (``wrote-synced``,
    ``already-synced``, ``not-found``, …) with a ``total`` of files considered
    and ``had_lyrics``, the tracks it skipped for already carrying lyrics.
    Re-running is cheap: the state file lets each pass skip tracks already
    resolved, and provider-unavailable tracks are re-tried on the next run.

    ``artist_dirs`` (default None = whole library) scopes the walk to those
    artists only.
    """
    log = log or _default_log
    unreadable = []

    def artist_read_failed(path, error):
        # An unreadable folder is met again when its albums are listed.
        if path.name in unreadable:
            return
        unreadable.append(path.name)
        log.warning(unreadable_artist(path.name, error))

    items = list(iter_library_flacs(
        artist_dirs=artist_dirs, on_artist_error=artist_read_failed))
    # Full-library maintenance only, including when no tracks remain.
    if artist_dirs is None and not unreadable and not dry_run and lyric_fetch.AVAILABLE:
        lyric_fetch.update_state(lyric_fetch.prune_missing, cfg.LYRIC_FETCH_STATE_FILE)
    total = len(items)
    if not total:
        return {"total": 0, "unreadable_artists": unreadable}
    paths = [p for p, _, _ in items]
    if not dry_run:
        cleared = lyric_fetch.clear_stale_temps(dict.fromkeys(p.parent for p in paths))
        if cleared:
            log.info(f"Removed {plural(cleared, 'leftover temporary lyrics file')}.")

    # Seed the state file with a fast, no-network classification of files that
    # already carry lyrics, so the fetch pass only queries providers for the
    # tracks that actually need them, and its progress count reflects real
    # work instead of ticking through thousands of already-lyriced files.
    if not rescan and not dry_run:
        report_progress("Scanning library lyrics", 0, total, "", "track")
        indexed = lyric_fetch.index_existing(
            paths, owned_root=cfg.MUSIC_ROOT,
            state_path=cfg.LYRIC_FETCH_STATE_FILE, log=log,
            should_stop=should_stop,
            progress_cb=lambda c, t, name: report_progress(
                "Scanning library lyrics", c, t, name, "track"),
        )
        if indexed.get("stopped"):
            processed = sum(
                count for outcome, count in indexed.items()
                if outcome not in {"stopped", "stop-total"})
            return {
                "total": total,
                "processed": processed,
                "stop_total": indexed.get("stop-total", total),
                "stop_stage": "index",
                "stopped": 1,
                "unreadable_artists": unreadable,
            }

    skips = Counter()
    counts = lyric_fetch.fetch_for_paths(
        paths,
        owned_root=cfg.MUSIC_ROOT,
        providers=cfg.LYRICS_PROVIDERS or None,
        lyrics_format=cfg.LYRICS_FORMAT,
        dry_run=dry_run,
        rescan=rescan,
        synced_only=synced_only,
        state_path=cfg.LYRIC_FETCH_STATE_FILE,
        log=log,
        should_stop=should_stop,
        progress_cb=lambda c, t, name, note: report_progress(
            " · ".join(filter(None, ("Looking up lyrics", note))),
            c, t, name, "track"),
        skips=skips,
    )
    result = dict(counts)
    result["had_lyrics"] = skips["lyrics"]
    result["unreadable_artists"] = unreadable
    result["total"] = total
    result["processed"] = sum(
        count for outcome, count in counts.items()
        if outcome not in {"stopped", "stop-total", "stop-deferred"})
    if counts.get("stopped"):
        result["stop_total"] = counts.get("stop-total", result["processed"])
        result["stop_deferred"] = counts.get("stop-deferred", 0)
        result["stop_stage"] = "fetch"
    return result


STOPPED = "Stopped after "


def copied_place(lyrics_format):
    """Where a pass in ``lyrics_format`` copies the lyrics a track has."""
    return {"embed": "tags", "sidecar": ".lrc files"}.get(
        (lyrics_format or "embed").lower(), "tags or .lrc files")


def copied_note(count, lyrics_format):
    return (f"lyrics copied to {copied_place(lyrics_format)} for "
            f"{plural(count, 'track')}")


def stop_summary(summary, lyrics_format):
    """What a stopped pass checked and wrote."""
    done = []
    added = summary["wrote"] - summary["upgraded"]
    if added:
        done.append(f"added lyrics to {plural(added, 'track')}")
    if summary["upgraded"]:
        done.append(f"upgraded {plural(summary['upgraded'], 'track')} "
                    "from plain to synced lyrics")
    if summary["copied"]:
        done.append(copied_note(summary["copied"], lyrics_format))
    wrote = "; ".join(done) + "." if done else "nothing was written."
    left = summary["deferred"]
    waiting = (f" {plural(left, 'track')} left for NetEase "
               f"{'is' if left == 1 else 'are'} looked up again by the next run."
               if left else "")
    return (f"{STOPPED}checking {summary['processed'] + left} of "
            f"{plural(summary['candidate_total'], 'track')}. "
            f"{wrote[0].upper()}{wrote[1:]}{waiting}")


def read_only_note(count):
    one = count == 1
    return (f"{plural(count, 'track')} {'is' if one else 'are'} read-only, "
            f"so {'its' if one else 'their'} lyrics were left alone")


def summarize_lyrics_result(result):
    """Group the fetch engine's outcomes for CLI and Web summaries."""
    total = max(0, int(result.get("total", 0)))
    processed = max(0, int(result.get("processed", 0)))
    stopped = bool(result.get("stopped"))
    candidate_total = (
        max(processed, int(result.get("stop_total", processed)))
        if stopped else processed
    )

    def count(*outcomes):
        return sum(int(result.get(outcome, 0)) for outcome in outcomes)

    # Tracks the pass skipped because they already carry lyrics, which the
    # summary counts as having them rather than as checked before.
    had_lyrics = max(0, int(result.get("had_lyrics", 0)))
    summary = {
        "total": total,
        "processed": processed,
        "stopped": stopped,
        "candidate_total": candidate_total,
        "already_checked": max(0, total - candidate_total - had_lyrics),
        "unfinished": max(0, candidate_total - processed),
        # Tracks a stop left for NetEase after the other providers found
        # nothing synced for them; the next run looks them up again.
        "deferred": max(0, int(result.get("stop_deferred", 0))),
        "wrote": count(
            "wrote-synced", "wrote-plain", "upgraded-synced",
            "dry:wrote-synced", "dry:wrote-plain", "dry:upgraded-synced",
        ),
        "upgraded": count("upgraded-synced", "dry:upgraded-synced"),
        "copied": count(
            "copied-synced", "copied-plain",
            "dry:copied-synced", "dry:copied-plain",
        ),
        "already": count(
            "already-synced", "already-plain", "kept-existing-plain",
            "kept-plain-lrc",
        ),
        "not_found": count("not-found"),
        "missing_tags": count("skipped-tags"),
        "too_long": count("skipped-long"),
        "instrumental": count("skipped-instrumental"),
        "read_only": count("skipped-read-only"),
        "unsafe": count("unsafe-path"),
        "unavailable": count("providers-unavailable"),
        "errors": count("write-error", "exception", "error"),
    }
    accounted = sum(
        summary[key]
        for key in (
            "wrote", "copied", "already", "not_found", "missing_tags",
            "too_long",
            "instrumental", "read_only",
            "unsafe", "unavailable", "errors",
        )
    )
    summary["other_errors"] = max(0, processed - accounted)
    summary["already"] += had_lyrics
    summary["failures"] = (
        summary["unsafe"] + summary["unavailable"]
        + summary["errors"] + summary["other_errors"]
        + len(result.get("unreadable_artists", []))
    )
    # A track no provider answered for is tried again by the next pass, so
    # a pass that leaves some finished. One that no provider answered at
    # all, or that failed in any other way, did not.
    answered = (summary["wrote"] + summary["not_found"]
                + summary["instrumental"] + count("kept-existing-plain"))
    summary["failed"] = (
        summary["failures"] > summary["unavailable"]
        or summary["unavailable"] > 0 and not answered)
    return summary
