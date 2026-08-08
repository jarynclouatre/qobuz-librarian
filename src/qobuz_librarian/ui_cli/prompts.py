"""Interactive prompts, display helpers, and fetch-log functions."""
import fcntl
import json
import re
import sys
from urllib.parse import urlsplit

from qobuz_librarian import config as cfg
from qobuz_librarian.library.catalog import (
    album_quality_label,
    album_year,
    album_year_int,
    is_lossless_album,
)
from qobuz_librarian.quality.decision import album_max_quality
from qobuz_librarian.quality.tiers import format_quality
from qobuz_librarian.ui_cli.colors import C, fmt, section, term_width, truncate
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.sentinels import MORE, URL_QUERY

# ── Fetch log ─────────────────────────────────────────────────────────────────

# Once the JSONL grows beyond this, rotate the current file to .1 (one backup
# kept) and start fresh.
_FETCH_LOG_MAX_BYTES = 5 * 1024 * 1024


def _fetch_log_lock():
    """Exclusive cross-process lock for the fetch-log read-modify-write. Both web
    worker lanes and concurrent CLI runs call log_fetch on the same file; without
    serialising the migrate/rotate/append sequence, a tmp.replace can discard an
    entry another writer appended between its read and its replace, and two
    rotations can race. Returns an open fd to keep referenced (closing releases
    the lock), or None if the lock can't be taken (degrade to best-effort)."""
    try:
        lock_path = cfg.FETCH_LOG_FILE.parent / (cfg.FETCH_LOG_FILE.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fp = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        return fp
    except OSError:
        return None


def log_fetch(entry):
    lock = _fetch_log_lock()
    try:
        cfg.FETCH_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if cfg.FETCH_LOG_FILE.exists():
            try:
                with open(cfg.FETCH_LOG_FILE, "rb") as f:
                    first = f.read(1)
            except OSError:
                first = b""
            if first == b"[":
                # If the legacy-array migration didn't complete, the file is
                # still a JSON array - appending a JSONL line would leave a
                # hybrid that can't be parsed and hides all history.
                if not _migrate_fetch_log_to_jsonl():
                    return
            _rotate_fetch_log_if_needed()
        with open(cfg.FETCH_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # Surface in --verbose instead of silently lying about
        # a successful log write. Still doesn't crash the run.
        vlog(f"log_fetch failed: {e}")
    finally:
        if lock is not None:
            try:
                lock.close()
            except OSError:
                pass


def _rotate_fetch_log_if_needed():
    try:
        if cfg.FETCH_LOG_FILE.stat().st_size <= _FETCH_LOG_MAX_BYTES:
            return
        rotated = cfg.FETCH_LOG_FILE.with_suffix(cfg.FETCH_LOG_FILE.suffix + ".1")
        cfg.FETCH_LOG_FILE.replace(rotated)
    except OSError as e:
        vlog(f"fetch-log rotate failed: {e}")


def _migrate_fetch_log_to_jsonl():
    """Rewrite a legacy JSON-array log as JSONL. Returns True when the file is
    safe to append to afterwards (migrated, or it wasn't an array), False when
    migration failed and the file is still a JSON array."""
    try:
        with open(cfg.FETCH_LOG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return True
        tmp = cfg.FETCH_LOG_FILE.with_suffix(cfg.FETCH_LOG_FILE.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for e in data:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(cfg.FETCH_LOG_FILE)
        return True
    except Exception as e:
        vlog(f"fetch-log migrate failed: {e}")
        return False


def _read_fetch_log(limit_tail=None):
    """Return list of fetch entries; handles both JSONL (new) and
    legacy JSON-array formats so reads keep working before migration.

    If ``limit_tail`` is set, returns only the last N entries (oldest →
    newest). The dashboard reads with limit_tail=8 so the whole multi-MB
    JSONL doesn't get slurped on every page load."""
    if not cfg.FETCH_LOG_FILE.exists():
        return []
    # Fast path for JSONL + tail-limit: stream from the end via
    # deque(maxlen=N). Saves both memory and parse time on large logs.
    if limit_tail is not None:
        try:
            with open(cfg.FETCH_LOG_FILE, "rb") as f:
                first = f.read(1)
        except OSError:
            return []
        if first != b"[":
            from collections import deque
            try:
                with open(cfg.FETCH_LOG_FILE, encoding="utf-8") as f:
                    last_lines = deque(f, maxlen=limit_tail)
            except OSError:
                return []
            entries = []
            for line in last_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):     # a non-dict line would break entry.get(...)
                    entries.append(obj)
            return entries
        # legacy array format - fall through to full read (we never tail-
        # optimised this path; once log_fetch migrates the file, it's a
        # one-time cost).
    try:
        with open(cfg.FETCH_LOG_FILE, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []
    s = content.lstrip()
    if not s:
        return []
    if s.startswith("["):
        try:
            data = json.loads(content)
            if isinstance(data, list):
                rows = [d for d in data if isinstance(d, dict)]
                return rows[-limit_tail:] if limit_tail else rows
            return []
        except json.JSONDecodeError:
            return []
    entries = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # Skip the partial/malformed line, keep going. JSONL's whole
            # appeal is that one bad line doesn't kill the rest.
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def show_recent_fetches(limit=10):
    """Print the last N entries from FETCH_LOG_FILE. Used by '?' in album-mode
    interactive query - useful on mobile when the user can't recall what they
    last grabbed.

    Reads via _read_fetch_log so both JSONL (new) and legacy JSON-array
    logs render the same. Pre-migration users see their history unchanged."""
    if not cfg.FETCH_LOG_FILE.exists():
        log.info(fmt(C.GRAY, "  No downloads yet."))
        return
    entries = _read_fetch_log(limit_tail=limit)
    if not entries:
        log.info(fmt(C.GRAY, "  No downloads yet."))
        return
    n = min(limit, len(entries))
    log.info(fmt(C.GRAY, f"  Last {n} download(s):"))
    for e in entries[-n:]:
        ts = (e.get("ts") or "?")[:10]
        artist = e.get("artist") or "?"
        title  = e.get("title") or "?"
        n_ok   = e.get("tracks_downloaded", 0)
        n_fail = e.get("tracks_failed", 0)
        tag    = ""
        if n_fail:
            tag = fmt(C.RED, f" ✗{n_fail}")
        elif e.get("result") == "already_complete":
            tag = fmt(C.GRAY, " (already complete)")
        elif n_ok:
            tag = fmt(C.GREEN, f" +{n_ok}")
        log.info(f"    {fmt(C.GRAY, ts)}  {truncate(artist, 22)} - {truncate(title, 32)}{tag}")


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


def confirm(msg, default_yes=True, auto_yes=False, on_eof=False, strict=False):
    if auto_yes:
        return True
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    while True:
        try:
            r = input(fmt(C.CYAN, msg + suffix)).strip().lower()
        except EOFError:
            # Closed stdin is not consent - and not an answer either.
            return on_eof
        if not r:
            return default_yes
        if r in ("y", "yes"):
            return True
        if not strict or r in ("n", "no"):
            return False
        # strict: the caller SAVES this answer (or destroys something on it),
        # so a typo must not read as consent - "maybe" mapping to No is
        # harmless when No is safe, but here No means "delete the originals,
        # permanently, from now on". Re-ask until it's a real yes or no.
        print(fmt(C.YELLOW, "  Please answer y or n."))


def prompt_album_selection(albums, prefer_hires=False, can_load_more=False):
    if not albums:
        return None
    if prefer_hires:
        # Sort by bit depth desc, sample rate desc, then year asc so the
        # original pressing leads.
        albums = sorted(
            albums,
            key=lambda a: (-(a.get("maximum_bit_depth") or 0),
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
        title  = a.get("title") or "?"
        year   = album_year(a) or "?"
        tracks = a.get("tracks_count") or "?"
        track_word = "track" if tracks == 1 else "tracks"
        qual   = album_quality_label(a)
        marker = fmt(C.YELLOW, " ⚠ lossy") if not is_lossless_album(a) else ""

        line1 = (f"  {fmt(C.BOLD, str(i).rjust(2))}.  "
                 f"{fmt(C.WHITE, truncate(artist, title_max // 2))} "
                 f"{fmt(C.GRAY, '-')} "
                 f"{fmt(C.WHITE, truncate(title, title_max))}")
        line2 = f"      {fmt(C.GRAY, f'{year} • {tracks} {track_word} • {qual}')}{marker}"
        print(line1)
        print(line2)
    if can_load_more:
        print(fmt(C.GRAY, "  m) Load more results"))
    print()
    hint = (f"1-{len(albums)}, m=more, q/Enter=cancel" if can_load_more
            else f"1-{len(albums)}, q/Enter=cancel")
    while True:
        try:
            r = input(fmt(C.CYAN, f"  Pick a number ({hint}): ")).strip().lower()
        except EOFError:
            print(fmt(C.GRAY, "  stdin closed - cancelling."))
            return None
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
    """Parse '1,3,5-7' → [1,3,5,6,7]; 'a' or 'all' → all numbers; '' → []."""
    s = s.strip().lower()
    if not s:
        return []
    if s in ("a", "all"):
        return list(range(1, max_n + 1))
    selected = set()
    # Split on commas AND whitespace so the natural "1 3" reads as two picks,
    # not the single token "13" (which would select the wrong album, or nothing).
    for tok in re.split(r"[\s,]+", s):
        if not tok:
            continue
        if "-" in tok:
            a, _, b = tok.partition("-")
            if a.isdecimal() and b.isdecimal():
                lo, hi = int(a), int(b)
                if lo > hi:
                    lo, hi = hi, lo
                lo = max(1, lo)
                hi = min(max_n, hi)
                if lo <= hi:
                    for n in range(lo, hi + 1):
                        selected.add(n)
        elif tok.isdecimal():
            n = int(tok)
            if 1 <= n <= max_n:
                selected.add(n)
    return sorted(selected)


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
    section("Album mode - interactive query", color=C.CYAN)
    print()
    while True:
        try:
            line = input(fmt(C.CYAN, "  Artist, free-text query, or Qobuz URL (q=cancel, ?=recent): ")).strip()
        except EOFError:
            return None
        if not line or line.lower() in ("q", "quit", "exit"):
            return None
        if line == "?":
            # Show recent fetches and re-prompt; don't return.
            show_recent_fetches()
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
            try:
                album = input(fmt(C.CYAN, "  Album (blank=search above text, q=cancel, ?=recent): ")).strip()
            except EOFError:
                return None
            if album.lower() in ("q", "quit", "exit"):
                return None
            if album == "?":
                show_recent_fetches()
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
        try:
            r = input(fmt(C.CYAN, f"{label_prefix}  Pick edition ({hint}): ")).strip().lower()
        except EOFError:
            # Closed stdin without --yes is not consent. Default to keep current.
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
        f"  Found {len(summaries)} similar album folder(s) for the same artist."))
    log.info(fmt(C.GRAY,
        "  Tracks that match the primary album can be removed from each sibling."))
    log.info(fmt(C.GRAY,
        "  Bonus tracks (no match) will be left in place."))

    from qobuz_librarian.quality.decision import quality_change_summary
    for i, s in enumerate(summaries, 1):
        sib_dir = s["dir"]
        n_total = len(s["all_tracks"])
        n_over  = len(s["overlap"])
        n_uniq  = len(s["unique"])

        print()
        log.info(fmt(C.BOLD + C.WHITE, f"  [{i}] {truncate(sib_dir.name, 55)}"))
        log.info(fmt(C.GRAY, f"      similarity: {s['score']:.2f}"))
        log.info(fmt(C.GRAY, f"      overlap:    {n_over}/{n_total} tracks"))
        if n_uniq:
            log.info(fmt(C.GRAY, f"      bonus:      {n_uniq} track(s) unique to sibling"))

        if not n_over:
            log.info(fmt(C.GREEN, "      → nothing to consolidate"))
            continue

        qc = quality_change_summary(s["overlap"])
        if qc["losing_hires"]:
            log.info(fmt(C.RED + C.BOLD,
                f"      ⚠  {qc['losing_hires']} track(s) here are HIGHER quality than primary."))
        if qc["unknown"]:
            log.info(fmt(C.RED + C.BOLD,
                f"      ⚠  {qc['unknown']} track(s) here have uncertain quality "
                "or channel layout - can't confirm they're safe to delete."))


def print_per_track_consolidation(summary):
    """Mobile-friendly per-track display: stacked, not tabular."""
    from qobuz_librarian.quality.decision import _track_quality_cmp

    sib_dir = summary["dir"]
    print()
    log.info(fmt(C.BOLD + C.WHITE, f"  Per-track detail: {truncate(sib_dir.name, 50)}"))
    print()

    for st, pt in summary["overlap"]:
        trk = str(st.get("tracknumber") or "?").rjust(2)
        title = truncate(st.get("title") or "?", 50)
        sib_q = format_quality(st.get("bits", 0), st.get("sample_rate", 0))
        pri_q = format_quality(pt.get("bits", 0), pt.get("sample_rate", 0))

        comparison = _track_quality_cmp(st, pt)
        if comparison is None:
            badge = fmt(C.RED + C.BOLD, "delete (quality uncertain)")
        elif comparison > 0:
            badge = fmt(C.RED + C.BOLD, "delete (HIGHER quality)")
        elif comparison < 0:
            badge = fmt(C.GREEN, "delete (lower quality)")
        else:
            badge = fmt(C.GRAY, "delete (same quality)")

        log.info(f"  {trk}.  {fmt(C.WHITE, title)}")
        log.info(fmt(C.GRAY,
            f"       sib: {sib_q}  /  primary: {pri_q}  →  {badge}"))


# ── Artist prompt ─────────────────────────────────────────────────────────────

def prompt_artist_name():
    """Interactive prompt for artist name; offers to list available artists."""
    from qobuz_librarian.library.scanner import list_library_artists

    print()
    while True:
        try:
            name = input(fmt(C.CYAN, "  Artist (q=cancel, ?=library): ")).strip()
        except EOFError:
            return None
        if not name or name.lower() in ("q", "quit", "exit"):
            return None
        if name == "?":
            artists = list_library_artists()
            if not artists:
                log.info(fmt(C.YELLOW, "  No artist directories found."))
                continue
            log.info(fmt(C.GRAY, f"  {len(artists)} artist(s) in library:"))
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
    title  = album.get("title") or "?"
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
    log.info(f"  {fmt(C.GRAY, 'tracks:')}   {n_total}")

    if auto_upgrade:
        # If process_album passed us the existing-quality label, show the
        # before→after contrast on the summary line too - mirrors the louder
        # banner above and reinforces what's about to happen.
        if existing_quality_label:
            log.info(f"  {fmt(C.MAGENTA + C.BOLD, f'↑ AUTO-UPGRADE: {existing_quality_label} → {qual}')} "
                     f"{fmt(C.GRAY, f'(all {n_total} track(s) will be replaced)')}")
        else:
            log.info(f"  {fmt(C.MAGENTA + C.BOLD, f'↑ AUTO-UPGRADE: replace all {n_total} track(s) at {qual}')}")
        return
    if force:
        log.info(f"  {fmt(C.YELLOW, '--force: will re-download all tracks regardless')}")
        return

    if album_dir:
        log.info(f"  {fmt(C.GRAY, 'found:')}    {album_dir}")
    else:
        log.info(f"  {fmt(C.GRAY, 'found:')}    not found on disk")

    if n_present == 0:
        log.info(f"  {fmt(C.GREEN, 'in your library:')}  none - full album will be fetched")
    elif n_missing == 0:
        log.info(f"  {fmt(C.GREEN, 'in your library:')}  "
                 f"ALL {n_total} track{'s' if n_total != 1 else ''}")
    else:
        log.info(f"  {fmt(C.GREEN, 'in your library:')}  {n_present}/{n_total}")
        log.info(f"  {fmt(C.YELLOW, 'missing:')}          {n_missing}")
        log.info(fmt(C.GRAY, "  Missing tracks:"))
        for t in missing[:25]:
            n = t.get("track_number") or "?"
            log.info(f"     {n:>2}.  {truncate(t.get('title') or '?', 60)}")
        if len(missing) > 25:
            log.info(fmt(C.GRAY, f"     … and {len(missing) - 25} more"))
