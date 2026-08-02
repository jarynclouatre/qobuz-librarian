"""String normalization, path sanitization, and title-stripping helpers.

The regex constants, cache sizes, and iteration limits here are load-bearing:
changing them shifts fuzzy-matching results across catalog matching, folder
resolution, and consolidation.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# ── Path sanitization ─────────────────────────────────────────────────────────
_NORMALIZE_RE       = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RUN_RE  = re.compile(r"\s+")
_CANONICAL_ISRC_RE  = re.compile(
    r"[a-z]{2}[a-z0-9]{3}\d{7}\Z", re.IGNORECASE | re.ASCII)
_CANONICAL_MBID_RE  = re.compile(r"[a-f0-9]{32}\Z", re.IGNORECASE | re.ASCII)


def canonical_recording_id(value, field):
    """Canonical destructive recording ID, or ``None`` when malformed.

    The empty string means the tag is genuinely absent. Callers can therefore
    distinguish missing authority (which may use a strict metadata fallback)
    from a nonblank placeholder or damaged tag (which must fail closed).
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return ""
    canonical = value.replace("-", "")
    if field == "isrc":
        return canonical.upper() if _CANONICAL_ISRC_RE.fullmatch(canonical) else None
    if field == "mb_trackid":
        if canonical == "0" * 32:
            return None
        return canonical.lower() if _CANONICAL_MBID_RE.fullmatch(canonical) else None
    return None


def canonical_track_slot(track, disc_field, number_field):
    """Return a strict positive ``(disc, track)`` pair, or ``None``.

    Missing disc tags mean disc one. Explicit booleans, floats, zeroes, and
    malformed values are uncertainty; coercing them can make two unrelated
    tracks appear to occupy the same destructive-matching slot.
    """
    disc_value = track.get(disc_field)
    if disc_value is None or disc_value == "":
        disc_value = 1
    number_value = track.get(number_field)
    if isinstance(disc_value, (bool, float)) or isinstance(number_value, (bool, float)):
        return None
    try:
        disc = int(disc_value)
        number = int(number_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if disc <= 0 or number <= 0:
        return None
    return disc, number

# beets' own default `replace` rules (its config_default.yaml), in the order
# it applies them.
_BEETS_REPLACEMENTS = (
    (re.compile(r'[<>:?*|]'), "_"),
    (re.compile(r'"'),        "_"),
    (re.compile(r"[\\/]"),    "_"),
    (re.compile(r"^\."),      "_"),
    (re.compile(r"\.$"),      "_"),
    (re.compile(r"[\x00-\x1f]"), "_"),
    (re.compile(r"^-"),       "_"),
    (re.compile(r"\s+$"),     ""),
    (re.compile(r"^\s+"),     ""),
)


def clean_qobuz_string(s):
    """Trim surrounding whitespace and collapse internal whitespace runs in a
    Qobuz response field.

    Qobuz titles and artist names occasionally arrive with trailing spaces
    (e.g. ``"Hunky Dory "``), which otherwise produce folder names like
    ``Hunky Dory  (1971)/`` after beets sanitizes them. Normalising at the API
    response boundary means downstream consumers (process, queue, web, beets)
    all get the clean form for free.

    Outer quotes are deliberately NOT stripped: Qobuz returns genuinely
    quoted titles as-is — David Bowie's ``"Heroes"`` comes back as ``'"Heroes"'``
    with the quotes part of the title — so removing them would corrupt the name
    on exactly the releases where the quotes are intentional. Qobuz's rare stray
    wrapping quote is the lesser evil to leave in place.

    Returns the empty string for None or non-string input so callers can rely
    on a usable str.
    """
    if not isinstance(s, str):
        return ""
    return _WHITESPACE_RUN_RE.sub(" ", s).strip()

# Normalized forms of the "Various Artists" placeholder used by Qobuz and many
# libraries.
VA_NORMALIZED = frozenset({"variousartists", "various", "va"})


@lru_cache(maxsize=2048)
def beets_sanitize(s):
    """Sanitize a string into the path component beets would write for it.

    A leading or trailing dot becomes ``_`` (not dropped) and a leading dash
    becomes ``_``, matching beets exactly — anything less mis-predicts the
    on-disk folder for names like "...And Justice for All".
    """
    if not s:
        return ""
    for rx, repl in _BEETS_REPLACEMENTS:
        s = rx.sub(repl, s)
    return s


@lru_cache(maxsize=8192)
def normalize(s):
    """ASCII-fold and strip punctuation for fuzzy comparison.

    Pure CJK / emoji titles that strip to "" return "" — callers treat
    that as "can't compare" (similarity() returns 0.0 for such pairs).
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    return _NORMALIZE_RE.sub("", s.lower())


@lru_cache(maxsize=8192)
def similarity(a, b):
    """Normalized similarity score [0.0, 1.0] between two strings.

    Two empty-normalized strings (e.g. pure-CJK titles) would score 1.0
    against each other — a false-positive waiting to bite
    find_qobuz_album_for_dir. Force 0.0 in that case.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def strip_leading_article(name):
    """Drop a leading 'the/a/an ' so 'The Wall' compares equal to a bare
    'Wall'. Left unchanged if stripping would empty it."""
    return _LEADING_ARTICLE_RE.sub("", name or "", count=1) or name


# ── Track-title edition stripping ─────────────────────────────────────────────
# Performance variants mark genuinely different recordings and must NOT be
# stripped.
_PERFORMANCE_VARIANT_RE = re.compile(
    r"\b(?:"
    r"acoustic|live|demo|remix|instrumental|"
    r"a\s*cappella|acapella|"
    r"cover|reprise|interlude|skit|"
    r"radio\s*edit|extended\s*mix|club\s*mix|"
    r"dub(?:\s*mix)?|piano\s*version|"
    r"unplugged|orchestral|symphonic|"
    r"feat\.?|featuring|"
    r"karaoke|backing\s*track|"
    r"alternate\s*(?:take|version|mix)|"
    r"early\s*(?:take|version|mix)|"
    r"rough\s*(?:take|mix|cut)|"
    r"session"
    r")\b",
    re.IGNORECASE,
)

# Trailing parenthesized chunk capturer — non-greedy, anchored to end.
_TRAILING_PAREN_CAPTURE_RE = re.compile(r"\s*\(([^()]*)\)\s*$")

# A parenthesized collaboration credit — "(with Beyoncé)" — names a distinct
# recording and stays attached.
_LEADING_COLLAB_RE = re.compile(r"^\s*with\s+\w", re.IGNORECASE)


@lru_cache(maxsize=4096)
def strip_edition_suffix(title):
    """Strip trailing parenthesized edition tags from a track title.

    Strips: anything in trailing parens that does NOT contain a performance-
    variant keyword. So (LP Version), (2014 Remaster), (Deluxe Edition),
    (Bonus Track), (Mono), (Explicit), (Japanese Version), (Edit), and
    arbitrary other release-version markers all get removed.

    Preserves: (Acoustic), (Live), (Demo), (Remix), (Instrumental),
    (Radio Edit), (Extended Mix), (feat. X), and other markers indicating a
    genuinely different recording — including when an edition tag sits OUTSIDE
    a performance one, so "Song (LP Version) (Remix)" → "Song (Remix)".

    Handles up to 4 trailing-paren groups ("Foo (Remaster) (Mono)").
    """
    if not title:
        return title
    s = title.strip()
    kept = []
    for _ in range(4):
        m = _TRAILING_PAREN_CAPTURE_RE.search(s)
        if not m:
            break
        head = s[: m.start()].strip()
        if not head:
            break
        if (_PERFORMANCE_VARIANT_RE.search(m.group(1))
                or _LEADING_COLLAB_RE.match(m.group(1))):
            kept.append(m.group(1).strip())
        s = head
    for group in reversed(kept):
        s = f"{s} ({group})"
    return s or title


def strip_trailing_parens(title):
    """A title with every trailing parenthesized group removed — both edition
    AND performance-variant tags. Distinct from strip_edition_suffix (which
    keeps performance variants): this only answers "is there a non-empty core
    once the parenthesised decorations are gone", which catalog matching uses to
    tell a non-Latin title apart from its ASCII-folded edition tag."""
    s = (title or "").strip()
    while True:
        m = _TRAILING_PAREN_CAPTURE_RE.search(s)
        if not m:
            return s
        head = s[: m.start()].strip()
        if not head:
            return s
        s = head


def canonical_track_title(value):
    """Exact title key that keeps non-Latin title text distinguishable."""
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


# ── Album-name decoration stripping ──────────────────────────────────────────
_YEAR_PAREN_RE = re.compile(r"\s*\([^)]*\d{4}[^)]*\)\s*$")
# Leading-year forms from alternate beets path templates, e.g. `[$year]
# $album/` produces "[1971] Hunky Dory"; `$year - $album/` produces "1971 -
# Hunky Dory".
_LEADING_YEAR_RE   = re.compile(
    r"^\s*(?:\[\s*\d{4}\s*\]\s*[-–—]?|\d{4}\s*[-–—])\s+")

# Edition keywords to strip from album title suffixes.
_EDITION_TAIL_KEYWORDS = (
    r"(?:\d{4}\s+)?(?:re)?master(?:ed)?(?:\s+edition)?|"
    r"(?:\d{1,4}\w{0,2}\s+)?anniversary(?:\s+edition)?|"
    r"deluxe(?:\s+edition)?|"
    r"super\s+deluxe(?:\s+edition)?|"
    r"expanded(?:\s+edition)?|"
    r"special\s+edition|"
    r"collector'?s\s+edition|"
    r"limited\s+edition|"
    r"bonus\s+edition|"
    r"reissue"
)
_EDITION_TAIL_RE = re.compile(
    r"\s*[-–—:]\s*(?:" + _EDITION_TAIL_KEYWORDS + r")\s*$",
    re.IGNORECASE,
)

# The same distinct-release markers, applied to the parenthesized form: a
# trailing '(Live)' / '(Acoustic)' / '(Demos)' is a different record, not an
# edition, so it's never stripped — mirroring the colon/dash exclusions above
# and strip_edition_suffix on track titles.
_ALBUM_VARIANT_RE = re.compile(
    r"\b(?:live|unplugged|acoustic|demos?|instrumental|"
    r"remix(?:ed|es)?|b[\s-]?sides?|rarities|companion|sessions?)\b",
    re.IGNORECASE,
)


def _strip_trailing_paren_tag(s):
    """Drop a trailing parenthesized edition tag, unless its contents mark a
    distinct release (live/acoustic/remix/...), which stays attached."""
    m = _TRAILING_PAREN_CAPTURE_RE.search(s)
    if not m or _ALBUM_VARIANT_RE.search(m.group(1)):
        return s
    return s[: m.start()].strip()


# Normalized forms of the markers above, for callers comparing already-
# normalized bare titles where normalize() has dropped the spaces that
# _ALBUM_VARIANT_RE's word boundaries rely on. Keep in step with it.
_ALBUM_VARIANT_TOKENS = (
    "live", "unplugged", "acoustic", "demos", "demo", "instrumental",
    "remixes", "remixed", "remix", "bsides", "rarities", "companion",
    "sessions", "session",
)

# Common words that legitimately follow a variant marker in real album titles
# once spaces have been normalized out: "live AT Wembley", "live IN Tokyo",
# "Live FROM the BBC", "demo TAPES", "Demo VERSION", "Live RECORDINGS", "Live
# EP".
_ALBUM_VARIANT_CONTINUATIONS = (
    "at", "in", "from", "of", "and", "with", "feat",
    "tapes", "tape", "version", "versions", "tracks", "track",
    "recordings", "recording", "performance", "performances",
    "mix", "mixes", "edit", "edits", "album", "albums", "ep", "eps",
    # Tour / concert / broadcast cluster — "Live Tour 2024", "Live in Concert",
    # "Live Broadcast 1972", "Anniversary Live", "Live Show", "Re-recorded".
    "tour", "tours", "broadcast", "broadcasts", "concert", "concerts",
    "show", "shows", "rerecorded", "rerecording", "rerecordings",
    "anniversary",
)


def differs_by_album_variant(shorter, longer):
    """True when normalized bare title ``longer`` is ``shorter`` plus a trailing
    distinct-release marker (live/acoustic/remix/...).

    Lets a prefix-based owned check tell 'Album' from 'Album (Live)' once both
    have been normalized to bare alphanumerics, so a live or acoustic record
    isn't mistaken for an un-stripped edition of the studio album.

    The marker must land at what looks like a word boundary in the normalized
    form — exact suffix, digits (a year), another variant token, or one of
    the common connector words real album titles use after a variant. Without
    that guard, a normalized title that coincidentally starts with the same
    letters as a token (``songliverpool``, ``songsessional``) would falsely
    register as a Live or Sessions variant and over-surface its owner as a
    different album.
    """
    if not longer.startswith(shorter):
        return False
    suffix = longer[len(shorter):]
    for tok in _ALBUM_VARIANT_TOKENS:
        if not suffix.startswith(tok):
            continue
        rest = suffix[len(tok):]
        if not rest:
            return True              # exact suffix — "Album (Live)" → albumlive
        if rest[0].isdigit():
            return True              # "(Live 2020)" → liver…digit
        if any(rest.startswith(other) for other in _ALBUM_VARIANT_TOKENS):
            return True              # "(Live Sessions)" → livesessions
        if any(rest.startswith(c) for c in _ALBUM_VARIANT_CONTINUATIONS):
            return True              # "(Live at Wembley)" → liveatwembley
        # else: this token matched as a prefix but the continuation looks
        # like the same word carrying on (live → liver…, session → sessional)
        # — keep looking for a longer token that might match cleanly.
    return False


@lru_cache(maxsize=2048)
def strip_album_decorations(name):
    """Strip trailing edition decorations iteratively from an album name.
    Parenthesized decorations: 'Revolver (2009 Remaster)' → 'Revolver' 'Album
    (Deluxe) (2018)' → 'Album' Colon / dash suffixes: 'Cassadaga: Deluxe
    Edition' → 'Cassadaga' 'Revolver - 2022 Remaster' → 'Revolver' 'Album:
    50th Anniversary Edition' → 'Album' Deliberately NOT stripped (these are
    distinct releases), in either the parenthesized or colon/dash form:
    'Cassadaga: A Companion' (companion EP — different recordings) 'Album:
    Live in Tokyo' / 'Album (Live)' (live album) 'Album: B-Sides' / 'Greatest
    Hits (Acoustic)' (rarities / acoustic set) Iterates up to 8 times so
    combined decorations like 'Foo: Deluxe Edition (2023)' fully strip in a
    single call.
    """
    s = name
    for _ in range(8):
        new = _LEADING_YEAR_RE.sub("", s).strip()
        if new == s:
            new = _strip_trailing_paren_tag(s)
        if new == s:
            new = _EDITION_TAIL_RE.sub("", s).strip()
        if new == s or not new:
            break
        s = new
    return s or name


# The affirmative-decoration gate for the strict variant below: a bare year,
# or edition vocabulary. Two things that LOOK like decorations are identity
# and must not match — a year span ("1972-1975" names which albums a box
# holds) and a possessive version ("Taylor's Version" is a re-recording, not
# a format; "Collector's Edition" is covered by its own keyword).
_DECOR_PAREN_KEYWORD_RE = re.compile(
    r"\b(?:(?:re)?master(?:ed)?|deluxe|edition|expanded|anniversary|"
    r"reissue|hi-?res|hd|version|explicit|clean|mono|stereo|legacy|"
    r"special|collector'?s|limited|bonus|digital)\b",
    re.IGNORECASE,
)
_YEAR_SPAN_RE = re.compile(r"\d{4}\s*[-–—/]\s*\d{4}")
_POSSESSIVE_VERSION_RE = re.compile(r"\b(?!collector)\w+'s\s+version\b",
                                    re.IGNORECASE)


def _paren_tag_is_decoration(tag):
    tag = tag.strip()
    if re.fullmatch(r"\d{4}", tag):
        return True
    if _YEAR_SPAN_RE.search(tag) or _POSSESSIVE_VERSION_RE.search(tag):
        return False
    return bool(_DECOR_PAREN_KEYWORD_RE.search(tag))


@lru_cache(maxsize=2048)
def strip_album_decorations_strict(name):
    """strip_album_decorations for identity keys: a trailing parenthesized
    tag is removed only when it affirmatively reads as a decoration (a bare
    year, or edition vocabulary), never merely because it is parenthesized.

    The loose variant's catch-all is right for fuzzy owned-matching, which
    backs it with similarity scores and confirmations — but as a store key it
    collapsed distinct albums: 'Alone' with 'Alone (Again)', 'Rancid' with
    'Rancid (5)', 'The Asylum Albums (1972-1975)' with '(1976-1980)'. Those
    keep their parentheses here; 'Revolver (2009 Remaster)' still folds."""
    s = name
    for _ in range(8):
        new = _LEADING_YEAR_RE.sub("", s).strip()
        if new == s:
            m = _TRAILING_PAREN_CAPTURE_RE.search(s)
            if (m and not _ALBUM_VARIANT_RE.search(m.group(1))
                    and _paren_tag_is_decoration(m.group(1))):
                new = s[: m.start()].strip()
        if new == s:
            new = _EDITION_TAIL_RE.sub("", s).strip()
        if new == s or not new:
            break
        s = new
    return s or name


def strip_year_decoration(name):
    """Remove only a leading or trailing year tag from an album folder name.

    'Black Sands (2010)' and '[2010] Black Sands' both reduce to 'Black Sands',
    but edition/live/remaster tags are left attached — so 'Album (Live)' stays
    distinct from 'Album (2018)' and the two are never treated as one album.
    """
    s = _LEADING_YEAR_RE.sub("", name).strip()
    s = _YEAR_PAREN_RE.sub("", s).strip()
    return s or name
