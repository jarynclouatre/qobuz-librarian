"""Grouping and paging for review lists."""
import hashlib
import json
import re

from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import missing_types
from qobuz_librarian.ui_cli.colors import format_size
from qobuz_librarian.web import flows


def _selection_token(job, tab=""):
    in_tab = flows.review_tab_filter(tab)
    with job._lock:
        selected = sorted(
            c["cid"] for c in job.candidates if c.get("selected") and in_tab(c))
    return hashlib.sha256(json.dumps([job.id, selected]).encode()).hexdigest()


def _review_context(job, page=1, query="", tab="", *, through=False, anchor=""):
    """Saved choices, counts and artist groups for a review page."""
    hidden_count = hidden_mod.count(_hide_scope(job.execute_kind))
    with job._lock:
        tab_counts = None
        if job.execute_kind == "library" and job.candidates:
            tab_counts = _review_tab_totals(job)
        if tab_counts:
            if tab not in ("missing", "gaps"):
                tab = ("gaps" if tab_counts["gaps"] and not tab_counts["missing"]
                       else "missing")
        else:
            tab = ""
        groups = _review_artist_groups(job, query, tab)
        page_groups, page, n_pages = _paginate_groups(
            groups, page, through=through, anchor=anchor)
        counts = job.selection_counts()
        filtered_total = sum(len(rows) for _artist, rows in groups)
        filtered_rest = _filtered_rest_of(groups)
        return {
            "review_groups": page_groups,
            "review_page": page,
            "review_pages": n_pages,
            "review_query": query,
            # Dismiss counts follow the current filter.
            "review_filtered_total": filtered_total,
            "review_filtered_selected": filtered_total - filtered_rest,
            "review_filtered_rest": filtered_rest,
            "review_tab": tab,
            "review_selection_token": _selection_token(job, tab),
            "review_tab_counts": tab_counts,
            "review_type_switches": RELEASE_TYPE_SWITCHES,
            "review_hidden_count": hidden_count,
            "review_counts": counts,
            "review_summary_line": _review_summary_line(job),
            "review_log_lines": _review_log_lines(job),
            "review_reclaimable_label": (format_size(counts["reclaimable"])
                                         if counts["reclaimable"] else ""),
            "review_page_size": REVIEW_PAGE_ARTISTS,
        }


# The generated summaries that say nothing but the size of the review. A
# summary that only restates the count is wrong the moment a row is dismissed
# and keeps the old number until the job is rebuilt from disk.
_COUNT_ONLY_SUMMARY = re.compile(
    r"(?:[0-9][0-9,]* to review across Missing Albums \([0-9][0-9,]*\)"
    r" and Gap Fill \([0-9][0-9,]*\)(?:, from your last library scan)?"
    r"|[0-9][0-9,]* upgrade candidates? ready to review"
    r"|[0-9][0-9,]* upgradeable albums? Qobuz can serve at higher quality"
    r"|[0-9][0-9,]* albums? can be upgraded"
    r"|[0-9][0-9,]* albums? can be downsampled"
    r"|[0-9][0-9,]* albums? stored above (?:CD rate|48 kHz)"
    r"|[0-9][0-9,]* new releases? found across the library)\."
)


def _review_summary_line(job) -> str:
    """The scan's own summary as a review screen should show it: its caveats
    kept, its bare count dropped."""
    return review_summary_without_count(job.execute_kind, job.summary)


def _review_log_lines(job):
    """The review's activity log without the scan's bare count, which a
    dismissal or a release-type switch makes untrue."""
    with job._lock:
        lines = list(job.log_lines)
    if job.execute_kind not in _TRIAGE_KINDS:
        return lines
    kept = []
    for line in lines:
        count = _COUNT_ONLY_SUMMARY.match(line)
        if count is None:
            kept.append(line)
        elif rest := line[count.end():].strip():
            kept.append(rest)
    return kept


def review_summary_without_count(execute_kind, summary) -> str:
    summary = str(summary or "").strip()
    if execute_kind not in _TRIAGE_KINDS or not summary:
        return summary
    count = _COUNT_ONLY_SUMMARY.match(summary)
    return summary[count.end():].strip() if count else summary


# Review kinds that get the paced-triage surface (unticked and hideable). They
# share one review screen; hidden-store scope decides where dismissals land.
_TRIAGE_KINDS = ("library", "upgrade", "new_releases", "downsample")


def _hide_scope(execute_kind):
    if execute_kind == "upgrade":
        return hidden_mod.SCOPE_UPGRADE
    if execute_kind == "downsample":
        return hidden_mod.SCOPE_DOWNSAMPLE
    return hidden_mod.SCOPE_MISSING


REVIEW_PAGE_ARTISTS = 40
# Whole-group candidate budget per page; see _paginate_groups.
REVIEW_PAGE_CANDIDATES = 1500


def _artist_sort_key(name: str) -> str:
    """Order artists ignoring a leading article, so 'The Beatles' files under B
    (not T) and 'A Tribe Called Quest' under T, the way music libraries sort.
    Case-insensitive."""
    low = (name or "").strip().casefold()
    for art in ("the ", "a ", "an "):
        if low.startswith(art):
            return low[len(art):]
    return low


def _review_artist_groups(job, query="", tab=""):
    """Candidates grouped by artist for the review screen, in a deterministic
    order so pagination is stable across reloads. ``query`` filters across the
    WHOLE set (artist name or any album title), so the filter spans pages, not
    just the one on screen. ``tab`` narrows a library review to one side of its
    Missing Albums / Gap Fill split. Returns a list of (artist, items) pairs."""
    with job._lock:
        cands = [c.copy() for c in job.candidates]
    q = (query or "").strip().lower()
    in_tab = flows.review_tab_filter(tab)
    groups: dict = {}
    for c in cands:
        if not in_tab(c):
            continue
        artist = c.get("artist") or ""
        if q and not flows.candidate_matches_query(c, q):
            continue
        groups.setdefault(artist, []).append(c)
    ordered = []
    for artist in sorted(groups, key=_artist_sort_key):
        items = sorted(groups[artist], key=lambda c: c.get("seq", 0))
        ordered.append((artist, items))
    return ordered


def _paginate_groups(groups, page, rows=lambda group: len(group[1]), *,
                     through=False, anchor=""):
    """Slice artist groups into one page. Returns (page_groups, page, n_pages).
    ``page`` is clamped into range so a stale/empty page lands somewhere valid.

    Pages pack whole artist groups (so select-artist stays sane) up to
    REVIEW_PAGE_ARTISTS groups AND ~REVIEW_PAGE_CANDIDATES rows, as ``rows``
    counts them for one group. A single group larger than the budget still
    gets its own page, whole."""
    pages = []
    cur, cur_rows = [], 0
    for g in groups:
        n = rows(g)
        if cur and (len(cur) >= REVIEW_PAGE_ARTISTS
                    or cur_rows + n > REVIEW_PAGE_CANDIDATES):
            pages.append(cur)
            cur, cur_rows = [], 0
        cur.append(g)
        cur_rows += n
    if cur:
        pages.append(cur)
    n_pages = max(1, len(pages))
    page = max(1, min(int(page or 1), n_pages))
    if through:
        if anchor:
            for i, batch in enumerate(pages):
                if any(artist == anchor for artist, _items in batch):
                    page = max(page, i + 1)
                    break
        return [group for batch in pages[:page] for group in batch], page, n_pages
    return (pages[page - 1] if pages else []), page, n_pages


def _filtered_rest_of(groups):
    return sum(1 for _artist, rows in groups
               for row in rows if not row.get("selected"))


# The Missing Albums switches, in the order the review shows them.
RELEASE_TYPE_SWITCHES = (
    (missing_types.LIVE, "Live"),
    (missing_types.COMPILATION, "Compilations"),
    (missing_types.EP, "EPs & singles"),
)


def _review_tab_totals(job):
    """Whole-set totals and selected counts behind a library review's Missing
    Albums / Gap Fill tabs, ignoring the page filter so the tab labels stay
    truthful. Selected counts feed the tab-scoped bulk bar: what the user sees
    on the active tab is exactly what Download/Dismiss will act on. Missing
    Albums counts the release types switched on, named in ``included``;
    ``switched_off`` counts the rows the other switches hold back and
    ``switched_off_under`` names those switches. ``typed`` says whether any
    of its rows carries a type, which a review saved before types were read
    does not."""
    included = missing_types.load()
    in_missing = flows.review_tab_filter("missing", included)
    gaps = gaps_sel = missing = missing_sel = 0
    held_back = {}
    typed = False
    with job._lock:
        for c in job.candidates:
            if flows.is_gap_candidate(c):
                gaps += 1
                gaps_sel += 1 if c.get("selected") else 0
                continue
            release_type = (c.get("payload") or {}).get("release_type")
            typed = typed or bool(release_type)
            if in_missing(c):
                missing += 1
                missing_sel += 1 if c.get("selected") else 0
            else:
                held_back[release_type] = held_back.get(release_type, 0) + 1
    return {"missing": missing, "gaps": gaps,
            "missing_selected": missing_sel, "gaps_selected": gaps_sel,
            "included": sorted(included), "typed": typed,
            "switched_off": sum(held_back.values()),
            "switched_off_under": [label for release_type, label
                                   in RELEASE_TYPE_SWITCHES
                                   if held_back.get(release_type)]}
