"""Tests for qobuz_librarian.library.tags - the gnarly bits."""
from qobuz_librarian.library.tags import (
    beets_sanitize,
    clean_qobuz_string,
    differs_by_album_variant,
    normalize,
    similarity,
    strip_album_decorations,
    strip_edition_suffix,
)


def test_clean_qobuz_string_trims_but_keeps_intentional_quotes():
    # Quotes are part of the real title for some releases (Bowie's "Heroes"),
    # and Qobuz returns them that way - so they are preserved, not stripped.
    assert clean_qobuz_string('"Heroes" ') == '"Heroes"'
    assert clean_qobuz_string("'Heroes'") == "'Heroes'"
    assert clean_qobuz_string('the "wall" album') == 'the "wall" album'
    # Trailing/internal whitespace is still normalised.
    assert clean_qobuz_string("Hunky Dory ") == "Hunky Dory"
    assert clean_qobuz_string("Hunky  Dory") == "Hunky Dory"
    # None / non-string fed in from a Qobuz API field that was null.
    assert clean_qobuz_string(None) == ""
    assert clean_qobuz_string(42) == ""


def test_normalize_folds_accents_and_drops_cjk():
    assert normalize("Café") == "cafe"
    assert normalize("Björk") == "bjork"
    # Pure CJK normalizes to empty after ASCII fold - similarity must not
    # treat two such strings as a match (see test_similarity_empty).
    assert normalize("最好") == ""


def test_beets_sanitize_matches_beets_on_disk_names():
    assert beets_sanitize("AC/DC") == "AC_DC"
    assert beets_sanitize("hello:world") == "hello_world"
    # Leading/trailing dots turn into _ (not dropped) exactly as beets writes
    # them, so a folder like "...And Justice for All" resolves on a scan
    # instead of being reported missing and re-downloaded.
    assert beets_sanitize("...And Justice for All") == "_..And Justice for All"
    assert beets_sanitize("Artist.") == "Artist_"


def test_similarity_empty_both_does_not_score_1():
    # Two strings that normalize to "" must NOT match - otherwise CJK-only
    # titles would all collide with each other and with empty fields.
    assert similarity("", "") == 0.0
    assert similarity("最好", "とても") == 0.0
    assert similarity("Cafe", "Café") == 1.0


def test_strip_edition_suffix_preserves_distinct_versions():
    assert strip_edition_suffix("Song (2014 Remaster)") == "Song"
    assert strip_edition_suffix("Song (Remaster) (Mono)") == "Song"
    # Acoustic / Live are distinct recordings, not editions - leave them in.
    assert strip_edition_suffix("Song (Acoustic)") == "Song (Acoustic)"
    assert strip_edition_suffix("Song (Live)") == "Song (Live)"
    # an edition tag wrapping a performance one is still stripped, the
    # performance marker kept.
    assert strip_edition_suffix("Song (LP Version) (Remix)") == "Song (Remix)"
    # "with" mid-phrase is an edition descriptor, not a collaboration - the
    # whole edition tag still strips...
    assert strip_edition_suffix("Song (Single Version with Intro)") == "Song"
    # ...but a leading "(with X)" credit marks a distinct recording, kept.
    assert strip_edition_suffix("Song (with Beyoncé)") == "Song (with Beyoncé)"


def test_strip_album_decorations_drops_year_prefixes_and_edition_tags():
    # Beets path templates `[$year] $album` and `$year - $album` produce
    # folder names like "[1971] Hunky Dory" or "1971 - Hunky Dory".
    assert strip_album_decorations("[1971] Hunky Dory") == "Hunky Dory"
    assert strip_album_decorations("1971 - Hunky Dory") == "Hunky Dory"
    assert strip_album_decorations("Revolver (2009 Remaster)") == "Revolver"
    assert strip_album_decorations("Cassadaga: Deluxe Edition") == "Cassadaga"
    # `Cassadaga: A Companion` is a distinct EP - not a deluxe edition tag.
    assert strip_album_decorations("Cassadaga: A Companion") == "Cassadaga: A Companion"


def test_differs_by_album_variant_rejects_coincidental_letter_overlap():
    # The bug this closed: a normalized title that coincidentally starts with
    # the SAME letters as a variant token isn't a variant - the token has to
    # land at a real word boundary in the original.
    assert not differs_by_album_variant("song", "songliverpool")
    assert not differs_by_album_variant("song", "songsessional")
    # The plain-prefix cases (no variant token at all) also stay False, so a
    # 'Cliff' folder doesn't get told 'Cliffside' differs.
    assert not differs_by_album_variant("cliff", "cliffside")
    assert not differs_by_album_variant("death", "deathmetal")
    # And the empty-suffix case (identical bare titles) is False - they're
    # the same album, not a variant pair.
    assert not differs_by_album_variant("album", "album")
