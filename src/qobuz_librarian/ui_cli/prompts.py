"""Interactive prompts and display helpers."""
import re
import sys
import time
from urllib.parse import urlsplit

from qobuz_librarian.library import catalog, scanner
from qobuz_librarian.library.catalog import (
    album_quality_label,
    album_year,
    album_year_int,
    is_lossless_album,
)
from qobuz_librarian.library.tags import normalize, strip_album_decorations
from qobuz_librarian.post_job_hook import release_title
from qobuz_librarian.quality import decision as quality_decision
from qobuz_librarian.quality.decision import album_max_quality
from qobuz_librarian.quality.tiers import format_quality
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, fmt, section, term_width, truncate
from qobuz_librarian.ui_cli.errors import plural, shown_path, unreadable_artist
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.sentinels import MORE, NO_ANSWER, URL_QUERY

# ── Recent downloads ──────────────────────────────────────────────────────────

_STATUS_TAGS = {
    "done": (C.GREEN, "done"),
    "failed": (C.RED, "failed"),
    "canceled": (C.YELLOW, "cancelled"),
}


def show_recent_downloads(limit=10):
    """Print the last album downloads from History, newest next to the prompt."""
    from qobuz_librarian.web import job_persistence

    job_persistence.init()
    rows = job_persistence.history_page(limit, 0, bulk=False)
    if not rows:
        log.info(fmt(C.GRAY, "  No downloads yet."))
        return
    log.info(fmt(C.GRAY, f"  Last {plural(len(rows), 'download')}:"))
    for row in reversed(rows):
        when = row["finished_at"] or row["created_at"]
        day = time.strftime("%Y-%m-%d", time.localtime(when)) if when else "?"
        color, word = _STATUS_TAGS.get(row["status"], (C.GRAY, row["status"]))
        tag = fmt(color, f" {word}")
        if row["tracks_missing"] > 0:
            tag += fmt(C.YELLOW, f" ({row['tracks_missing']} missing)")
        log.info(f"    {fmt(C.GRAY, day)}  {truncate(row['artist'] or '?', 22)}"
                 f" - {truncate(row['title'] or '?', 32)}{tag}")


# ── Interactive UI ────────────────────────────────────────────────────────────

def _flush_stdin():
    """Discard buffered input (escape sequences from arrow/page keys
    pressed during long-running output, typeahead, etc.) before showing
    a prompt. Prevents typeahead from prior output stages racing into
    the next answer. POSIX-only; no-op when stdin isn't a tty or when
    termios is unavailable (e.g. Windows)."""
    try:
        import termios
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (ImportError, OSError):
        pass


def confirm(msg, default_yes=True, auto_yes=False, on_eof=False, quiet=False):
    if auto_yes:
        return True
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    while True:
        r = ask(msg + suffix, quiet=quiet)
        if r is None:
            # A closed input is not consent, and not an answer either.
            return on_eof
        if not r:
            return default_yes
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False
        print(fmt(C.YELLOW, "  Answer y or n."))


def download_question(missing):
    """The confirm before downloading an album's missing tracks, counting only
    those Qobuz streams."""
    unstreamed = sum(1 for t in missing if catalog.unstreamable(t))
    note = f" ({unstreamed} not on Qobuz)" if unstreamed else ""
    return (f"\n  Proceed with downloading "
            f"{plural(len(missing) - unstreamed, 'track')}{note}?")


def _ownership_note(res):
    """Search's library mark for one result, or ""."""
    unavailable = res.get("unavailable") or 0
    gap = fmt(C.GRAY, f" · {unavailable} not on Qobuz") if unavailable else ""
    if res.get("owned"):
        bonus = res.get("bonus_versions") or 0
        extra = (fmt(C.GRAY, f" +{bonus} bonus version{'s' if bonus != 1 else ''}")
                 if bonus else "")
        return fmt(C.GREEN, " • In library") + extra + gap
    if res.get("partial"):
        return fmt(C.YELLOW,
                   f" • {res['have_tracks']} of {res['want_tracks']} tracks") + gap
    if res.get("as_new_edition") and res.get("folder"):
        return fmt(C.CYAN, " • Other version in library")
    return fmt(C.GRAY, f" • {unavailable} not on Qobuz") if unavailable else ""


def prompt_album_selection(albums, prefer_hires=False, can_load_more=False,
                           ownership=None):
    """Pick one search result. ``ownership`` maps an album id to its
    catalog.search_result_ownership() marks."""
    if not albums:
        return None
    if prefer_hires:
        # Qobuz lists results by relevance. Keep that order between albums and
        # put the best resolution first among one album's editions, so a
        # hi-res single by someone else never outranks the album searched for.
        def edition_key(a):
            artist = ((a.get("artist") or {}).get("name") or "").casefold()
            bare = strip_album_decorations(a.get("title") or "")
            return artist, normalize(bare) or bare.strip().casefold()
        first_seen = {}
        for position, a in enumerate(albums):
            first_seen.setdefault(edition_key(a), position)
        albums = sorted(
            albums,
            key=lambda a: (first_seen[edition_key(a)],
                           -(a.get("maximum_bit_depth") or 0),
                           -(a.get("maximum_sampling_rate") or 0),
                           album_year_int(a)),
        )

    print()
    print(fmt(C.BOLD + C.WHITE, "  Qobuz search results:"))
    print()
    # 120 is a comfortable max for a desktop terminal; on a narrow or mobile
    # terminal, term_width() already returns ~60 and title_max scales down.
    width = min(term_width(), 120)
    title_max = max(20, width - 32)

    for i, a in enumerate(albums, 1):
        artist = (a.get("artist") or {}).get("name") or "?"
        title  = release_title(a.get("title"), a.get("version"))
        year   = album_year(a) or "?"
        tracks = a.get("tracks_count") or "?"
        track_word = "track" if tracks == 1 else "tracks"
        qual   = album_quality_label(a)
        marker = fmt(C.YELLOW, " ⚠ lossy") if not is_lossless_album(a) else ""
        explicit = " • Explicit" if a.get("parental_warning") else ""

        line1 = (f"  {fmt(C.BOLD, str(i).rjust(2))}.  "
                 f"{fmt(C.WHITE, truncate(artist, title_max // 2))} "
                 f"{fmt(C.GRAY, '-')} "
                 f"{fmt(C.WHITE, truncate(title, title_max))}")
        owned = _ownership_note((ownership or {}).get(a.get("id")) or {})
        line2 = (f"      {fmt(C.GRAY, f'{year} • {tracks} {track_word} • {qual}{explicit}')}"
                 f"{marker}{owned}")
        print(line1)
        print(line2)
    if can_load_more:
        print(fmt(C.GRAY, "  m) Load more results"))
    print()
    hint = (f"1-{len(albums)}, m=more, q/Enter=cancel" if can_load_more
            else f"1-{len(albums)}, q/Enter=cancel")
    while True:
        r = ask(f"  Pick a number ({hint}): ")
        if r is None:
            return NO_ANSWER
        if r in ("q", "quit", "exit", ""):
            return None
        if can_load_more and r == "m":
            return MORE
        if r.isdecimal():
            idx = int(r)
            if 1 <= idx <= len(albums):
                return albums[idx - 1]
        print(fmt(C.GRAY, f"  Enter a number{', m,' if can_load_more else ''} or q."))


def parse_number_list(s, max_n):
    """Parse '1,3,5-7' → ([1,3,5,6,7], []); 'a' or 'all' → every number;
    '' → ([], []). The second list holds the parts that name nothing in
    1..max_n."""
    s = s.strip().lower()
    if not s:
        return [], []
    if s in ("a", "all"):
        return list(range(1, max_n + 1)), []
    selected = set()
    rejected = []
    # Split on commas AND whitespace so the natural "1 3" reads as two picks,
    # not the single token "13" (which would select the wrong album, or nothing).
    for tok in re.split(r"[\s,]+", s):
        if not tok:
            continue
        a, dash, b = tok.partition("-")
        if dash and a.isdecimal() and b.isdecimal():
            lo, hi = sorted((int(a), int(b)))
        elif tok.isdecimal():
            lo = hi = int(tok)
        else:
            rejected.append(tok)
            continue
        if lo < 1 or hi > max_n:
            rejected.append(tok)
            continue
        selected.update(range(lo, hi + 1))
    return sorted(selected), rejected


def _is_qobuz_url(text: str) -> bool:
    """True only for an actual Qobuz URL (http(s) scheme + a qobuz.com host), so a
    free-text query that merely contains "qobuz.com" is searched, not misrouted to
    the URL handler."""
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    netloc = parts.netloc.lower()
    return netloc == "qobuz.com" or netloc.endswith(".qobuz.com")


def interactive_query():
    """Return one of: None (cancel), (URL_QUERY, url), or (artist, album)."""
    section("Search", color=C.CYAN)
    print()
    while True:
        line = ask(
            "  Artist, free-text query, or Qobuz URL (q=cancel, ?=recent): ",
            lower=False)
        if line is None:
            return None
        if not line or line.lower() in ("q", "quit", "exit"):
            return None
        if line == "?":
            show_recent_downloads()
            print()
            continue
        if _is_qobuz_url(line):
            return (URL_QUERY, line)
        if line.lower().startswith(("http://", "https://")):
            log.info(fmt(C.YELLOW,
                "  ⚠  Only Qobuz URLs are supported here. Paste a Qobuz album URL "
                "or search by artist/title."))
            continue
        artist = line
        # Inner loop so '?' here re-asks for the album, not the artist -
        # otherwise the user loses the artist name they just typed.
        while True:
            album = ask(
                "  Album (blank=search above text, q=cancel, ?=recent): ",
                lower=False)
            if album is None:
                return None
            if album.lower() in ("q", "quit", "exit"):
                return None
            if album == "?":
                show_recent_downloads()
                print()
                continue
            # Blank album: use the artist/free-text input as a single combined
            # query, mirroring the one-positional-arg non-interactive path.
            return (artist, album)


def prompt_edition_pick(current_album, current_extras_count, candidates,
                       existing, args, *, label_prefix=""):
    """Return (chosen_album, chosen_new_extras) or (None, None) to keep current."""
    if not candidates:
        return None, None

    n_local = len(existing)
    cur_q = album_max_quality(current_album)
    cur_tracks = (current_album.get("tracks") or {}).get("items") or []
    cur_track_count = len(cur_tracks) or current_album.get("tracks_count") or 0

    def _is_strict_improvement(full, new_extras):
        # Better on extras axis, OR same extras with strictly higher quality.
        cand_q = album_max_quality(full)
        if len(new_extras) < current_extras_count:
            return True
        if len(new_extras) == current_extras_count and cand_q > cur_q:
            return True
        return False

    if getattr(args, "yes", False):
        # Non-interactive: pick top candidate if it's a clear improvement.
        for full, new_extras in candidates:
            if _is_strict_improvement(full, new_extras):
                vlog(f"    auto-picking edition (--yes): {full.get('title')!r}")
                return full, new_extras
        return None, None

    # Interactive: show menu.
    log.info(fmt(C.MAGENTA,
        f"\n{label_prefix}↑  Quality / coverage upgrade may be available "
        f"({len(candidates)} alternate edition{'s' if len(candidates) != 1 else ''} found):"))

    def _row(idx, title_str, year_str, tc, qual_str, extras_note, marker=""):
        track_word = "track" if tc == 1 else "tracks"
        line1 = (f"{label_prefix}  {fmt(C.BOLD, str(idx).rjust(2))}.  "
                 f"{fmt(C.WHITE, truncate(title_str, 60))}{marker}")
        line2 = (f"{label_prefix}      "
                 f"{fmt(C.GRAY, f'{year_str} • {tc} {track_word} • {qual_str}')}"
                 f"{extras_note}")
        log.info(line1)
        log.info(line2)

    for i, (full, new_extras) in enumerate(candidates, 1):
        title_str = full.get("title") or "?"
        year_str = album_year(full) or "?"
        f_tracks = (full.get("tracks") or {}).get("items") or []
        tc = len(f_tracks) or full.get("tracks_count") or 0
        qual_str = album_quality_label(full)
        if new_extras:
            extras_note = fmt(C.YELLOW,
                f"  ⚠ would lose {len(new_extras)} on-disk track"
                f"{'s' if len(new_extras) != 1 else ''}")
        else:
            extras_note = fmt(C.GREEN, f"  ✓ covers all {n_local} on-disk")
        _row(i, title_str, year_str, tc, qual_str, extras_note)

    # The "keep current" option.
    keep_idx = len(candidates) + 1
    cur_title = current_album.get("title") or "?"
    cur_year = album_year(current_album) or "?"
    cur_qual = album_quality_label(current_album)
    if current_extras_count:
        cur_extras_note = fmt(C.YELLOW,
            f"  ⚠ {current_extras_count} on-disk track"
            f"{'s' if current_extras_count != 1 else ''} not on this edition")
    else:
        cur_extras_note = fmt(C.GRAY, "  (current match)")
    _row(keep_idx, cur_title, cur_year, cur_track_count, cur_qual, cur_extras_note,
         marker=fmt(C.GRAY, "  [keep]"))

    log.info("")
    hint = (f"1-{len(candidates)} to switch, {keep_idx}/Enter to keep current, q to skip")
    while True:
        r = ask(f"{label_prefix}  Pick edition ({hint}): ")
        if r is None:
            # A closed input without --yes is not consent. Keep the current one.
            return None, None
        if r in ("q", "quit", "exit", "s", "skip"):
            return None, None
        if r == "" or r == str(keep_idx):
            return None, None
        if r.isdecimal():
            idx = int(r)
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]
        log.info(fmt(C.GRAY,
            f"{label_prefix}  Enter 1-{len(candidates)}, {keep_idx} to keep, or q."))


# ── Consolidation display ─────────────────────────────────────────────────────

def print_consolidation_overview(summaries):
    section("Consolidation candidates")
    print()
    log.info(fmt(C.GRAY,
        f"  Found {plural(len(summaries), 'similar album folder')} for the same artist."))
    log.info(fmt(C.GRAY,
        "  A sibling whose tracks are all in the primary album can move to the backup folder."))
    log.info(fmt(C.GRAY,
        "  One with tracks the primary album lacks is left as it is."))

    for i, s in enumerate(summaries, 1):
        sib_dir = s["dir"]
        n_total = len(s["all_tracks"])
        n_over  = len(s["overlap"])
        n_uniq  = len(s["unique"])

        print()
        log.info(fmt(C.BOLD + C.WHITE, f"  [{i}] {truncate(sib_dir.name, 55)}"))
        log.info(fmt(C.GRAY, f"      similarity: {s['score']:.2f}"))
        log.info(fmt(C.GRAY, f"      overlap:    {n_over} of {n_total} tracks"))
        if n_uniq:
            log.info(fmt(C.GRAY, f"      not in the primary: {plural(n_uniq, 'track')}"))

        if not n_over:
            log.info(fmt(C.GREEN, "      → nothing to consolidate"))
            continue
        if n_uniq:
            log.info(fmt(C.GRAY, "      → left as it is"))
            continue

        qc = quality_decision.quality_change_summary(s["overlap"])
        if qc["losing_hires"]:
            log.info(fmt(C.RED + C.BOLD,
                f"      ⚠  {plural(qc['losing_hires'], 'track')} here "
                f"{'is' if qc['losing_hires'] == 1 else 'are'} HIGHER quality than primary."))
        if qc["unknown"]:
            log.info(fmt(C.RED + C.BOLD,
                f"      ⚠  {plural(qc['unknown'], 'track')} here "
                f"{'has' if qc['unknown'] == 1 else 'have'} uncertain quality or channel "
                "layout - can't confirm "
                f"{'it is' if qc['unknown'] == 1 else 'they are'} safe to move out."))


def print_per_track_consolidation(summary):
    """Mobile-friendly per-track display: stacked, not tabular."""
    sib_dir = summary["dir"]
    print()
    log.info(fmt(C.BOLD + C.WHITE, f"  Per-track detail: {truncate(sib_dir.name, 50)}"))
    print()

    for st, pt in summary["overlap"]:
        trk = str(st.get("tracknumber") or "?").rjust(2)
        title = truncate(st.get("title") or "?", 50)
        sib_q = format_quality(st.get("bits", 0), st.get("sample_rate", 0))
        pri_q = format_quality(pt.get("bits", 0), pt.get("sample_rate", 0))

        comparison = quality_decision._track_quality_cmp(st, pt)
        if comparison is None:
            badge = fmt(C.RED + C.BOLD, "move out (quality uncertain)")
        elif comparison > 0:
            badge = fmt(C.RED + C.BOLD, "move out (HIGHER quality)")
        elif comparison < 0:
            badge = fmt(C.GREEN, "move out (lower quality)")
        else:
            badge = fmt(C.GRAY, "move out (same quality)")

        log.info(f"  {trk}.  {fmt(C.WHITE, title)}")
        log.info(fmt(C.GRAY,
            f"       sib: {sib_q}  /  primary: {pri_q}  →  {badge}"))


# ── Artist prompt ─────────────────────────────────────────────────────────────

def prompt_artist_name():
    """Interactive prompt for artist name; offers to list available artists."""
    print()
    while True:
        name = ask("  Artist (q=cancel, ?=library): ", lower=False)
        if name is None:
            return None
        if not name or name.lower() in ("q", "quit", "exit"):
            return None
        if name == "?":
            artists = scanner.list_library_artists(
                on_artist_error=lambda path, error: log.warning(
                    unreadable_artist(path.name, error)))
            if not artists:
                log.info(fmt(C.YELLOW, "  No artist directories found."))
                continue
            log.info(fmt(C.GRAY, f"  {plural(len(artists), 'artist')} in library:"))
            for d in artists[:50]:
                log.info(fmt(C.GRAY, f"    • {d.name}"))
            if len(artists) > 50:
                log.info(fmt(C.GRAY, f"    … and {len(artists) - 50} more"))
            continue
        return name


# ── Album summary ─────────────────────────────────────────────────────────────

def print_album_summary(album, missing, present, album_dir, force, auto_upgrade=False,
                        existing_quality_label=None):
    artist = (album.get("artist") or {}).get("name") or "?"
    title  = release_title(album.get("title"), album.get("version"))
    year   = album_year(album) or "?"
    qual   = album_quality_label(album)
    n_total   = len(missing) + len(present)
    n_missing = len(missing)
    n_present = len(present)

    section("Selected album")
    print()
    log.info(f"  {fmt(C.BOLD + C.WHITE, artist)} {fmt(C.GRAY, '-')} "
             f"{fmt(C.BOLD + C.WHITE, title)}  {fmt(C.GRAY, f'({year})')}")
    log.info(f"  {fmt(C.GRAY, 'quality:')}  {qual}")
    unstreamed = sum(1 for t in missing if catalog.unstreamable(t))
    note = fmt(C.GRAY, f"  ({unstreamed} not on Qobuz)") if unstreamed else ""
    log.info(f"  {fmt(C.GRAY, 'tracks:')}   {n_total}{note}")

    if auto_upgrade:
        # If process_album passed us the existing-quality label, show the
        # before→after contrast on the summary line too - mirrors the louder
        # banner above and reinforces what's about to happen.
        if existing_quality_label:
            log.info(f"  {fmt(C.MAGENTA + C.BOLD, f'↑ AUTO-UPGRADE: {existing_quality_label} → {qual}')} "
                     f"{fmt(C.GRAY, f'(all {plural(n_total, "track")} will be replaced)')}")
        else:
            log.info(f"  {fmt(C.MAGENTA + C.BOLD, f'↑ AUTO-UPGRADE: replace all {plural(n_total, "track")} at {qual}')}")
        return
    if force:
        log.info(f"  {fmt(C.YELLOW, '--force: will re-download all tracks regardless')}")
        return

    other_dir = None if album_dir else catalog.other_version_dir(album)
    if album_dir:
        log.info(f"  {fmt(C.GRAY, 'found:')}    {shown_path(album_dir)}")
    elif other_dir:
        log.info(f"  {fmt(C.GRAY, 'found:')}    another version in "
                 f"{shown_path(other_dir)}")
    else:
        log.info(f"  {fmt(C.GRAY, 'found:')}    not found on disk")

    if n_present == 0 and other_dir:
        log.info(f"  {fmt(C.GREEN, 'in your library:')}  "
                 "none of this version - full album will be fetched as a separate copy")
    elif n_present == 0:
        log.info(f"  {fmt(C.GREEN, 'in your library:')}  none - full album will be fetched")
    elif n_missing == 0:
        log.info(f"  {fmt(C.GREEN, 'in your library:')}  "
                 f"all {n_total} track{'s' if n_total != 1 else ''}")
    else:
        log.info(f"  {fmt(C.GREEN, 'in your library:')}  "
                 f"{n_present} of {n_total} tracks")
        log.info(f"  {fmt(C.YELLOW, 'missing:')}          {n_missing}")
        log.info(fmt(C.GRAY, "  Missing tracks:"))
        for t in missing[:25]:
            n = t.get("track_number") or "?"
            note = fmt(C.GRAY, "  (not on Qobuz)") if catalog.unstreamable(t) else ""
            log.info(f"     {n:>2}.  {truncate(t.get('title') or '?', 60)}{note}")
        if len(missing) > 25:
            log.info(fmt(C.GRAY, f"     … and {len(missing) - 25} more"))
