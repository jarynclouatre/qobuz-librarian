"""Single-track grabs: the hidden 'single' scope + the discovery gates that keep
a grabbed single from reading as a gap and from flooding scans with the artist's
catalogue."""
from pathlib import Path

import pytest

from qobuz_librarian.library import hidden
from qobuz_librarian.library.discovery import (
    DirMatch,
    DiscoveryResult,
    _collecting,
    classify_owned_match,
)


@pytest.fixture
def store_file(tmp_path, monkeypatch):
    """Point the hidden store at a fresh per-test file."""
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    return tmp_path / "hidden.json"


def _album(title, aid="1", artist="Allie X"):
    return {"id": aid, "title": title, "artist": {"name": artist}}


# ── the hidden 'single' scope ──────────────────────────────────────────────────

def test_mark_is_unmark_single_roundtrips(store_file):
    assert hidden.mark_single("Allie X", "Girl With No Face", "2024", "555") is True
    store = hidden.load()
    assert hidden.is_single("Allie X", "Girl With No Face", store) is True
    assert hidden.is_single("Allie X", "Some Other Album", store) is False
    # case/spacing-insensitive on the artist, like the rest of the store keys
    assert hidden.is_single("allie x", "Girl With No Face", store) is True

    assert hidden.unmark_single("Allie X", "Girl With No Face") is True
    assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
    # unmarking something not marked is a no-op, not an error
    assert hidden.unmark_single("Allie X", "Girl With No Face") is False




def test_single_marks_keep_multiple_exact_editions_independent(store_file):
    hidden.mark_single("Weezer", "Weezer", "1994", "blue-album")
    hidden.mark_single("Weezer", "Weezer", "2001", "green-album")
    store = hidden.load()

    assert hidden.is_single(
        "Weezer", "Weezer", store, year="1994", album_id="blue-album"
    ) is True
    assert hidden.is_single(
        "Weezer", "Weezer", store, year="2001", album_id="green-album"
    ) is True

    assert hidden.unmark_single(
        "Weezer", "Weezer", year="1994", album_id="blue-album"
    ) is True
    remaining = hidden.load()
    assert hidden.is_single(
        "Weezer", "Weezer", remaining, year="1994", album_id="blue-album"
    ) is False
    assert hidden.is_single(
        "Weezer", "Weezer", remaining, year="2001", album_id="green-album"
    ) is True


# ── the discovery gates ────────────────────────────────────────────────────────

def test_marked_partial_goes_to_singles_not_gaps(store_file):
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "555")
    store = hidden.load()
    result = DiscoveryResult("aid", "Allie X")
    m = DirMatch(status="partial", album_dir=Path("/m/Allie X/Girl With No Face (2024)"),
                 qobuz_album=_album("Girl With No Face", aid="555"),
                 missing=[{"id": "t2"}], present=[{"id": "t1"}])
    handled, resolved = set(), set()
    classify_owned_match(result, m, None, store, "Allie X", handled, resolved)
    assert len(result.singles) == 1
    assert result.gaps == []
    # its album id is still accounted for, so the missing pass can't re-offer it
    assert "555" in handled


def test_collecting_requires_an_album_thats_not_a_single(store_file):
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "555")
    store = hidden.load()
    one_single = [Path("/m/Allie X/Girl With No Face (2024)")]
    two_dirs = one_single + [Path("/m/Allie X/Cape God (2020)")]
    # only the single → not collecting (catalogue stays quiet)
    assert _collecting(store, "Allie X", one_single) is False
    # a real album alongside the single → collecting
    assert _collecting(store, "Allie X", two_dirs) is True
    # no store (an explicit single-artist request) → always show everything
    assert _collecting(None, "Allie X", one_single) is True


# ── failure seams ──────────────────────────────────────────────────────────────

def test_discover_fully_missing_skips_single_catalog_album(store_file):
    """discover_fully_missing must not re-offer a catalog album the user
    deliberately grabbed as a single - even when no owned folder exists."""
    from qobuz_librarian.library.discovery import DiscoveryOpts, discover_fully_missing

    hidden.mark_single("Allie X", "Girl With No Face", "2024", "555")
    store = hidden.load()

    # Minimal catalog entry that passes is_lossless_album and filter_short_releases
    catalog = [{
        "id": "555",
        "title": "Girl With No Face",
        "artist": {"name": "Allie X"},
        "maximum_bit_depth": 16,
        "tracks_count": 12,
        "release_date_original": "2024-01-01",
    }]
    gaps = discover_fully_missing(
        "Allie X", catalog, DiscoveryOpts(),
        single_store=store,
    )
    assert gaps == [], "single-marked album must be suppressed from the catalog walk"






def test_mark_single_matches_remaster_title_via_fingerprint(store_file):
    """album_fingerprint calls strip_album_decorations, so marking the remaster
    edition ties to the same fingerprint as the bare title - an is_single check
    on either spelling returns True."""
    hidden.mark_single("Allie X", "Girl With No Face (2024 Remaster)", "2024", "555")
    store = hidden.load()
    # bare title resolves to same fingerprint
    assert hidden.is_single("Allie X", "Girl With No Face", store) is True
    # original decorated form also matches
    assert hidden.is_single("Allie X", "Girl With No Face (2024 Remaster)", store) is True
