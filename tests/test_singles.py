"""Single-track grabs: the hidden 'single' scope + the discovery gates that keep
a grabbed single from reading as a gap and from flooding scans with the artist's
catalogue."""
from pathlib import Path

import pytest

from qobuz_librarian.library import hidden
from qobuz_librarian.library.discovery import (
    DirMatch,
    DiscoveryOpts,
    DiscoveryResult,
    _collecting,
    classify_owned_match,
    discover_fully_missing,
)


@pytest.fixture
def store_file(tmp_path, monkeypatch):
    """Point the hidden store at a fresh per-test file."""
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    return tmp_path / "hidden.json"


def _album(title, aid="1", artist="Allie X"):
    return {"id": aid, "title": title, "artist": {"name": artist}}


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
    # The missing-album pass skips it even with no folder on disk.
    catalog = [{**m.qobuz_album, "maximum_bit_depth": 16, "tracks_count": 12,
                "release_date_original": "2024-01-01"}]
    assert discover_fully_missing("Allie X", catalog, DiscoveryOpts(), single_store=store) == []
    # The single alone does not make the artist one being collected.
    assert _collecting(store, "Allie X", [m.album_dir]) is False
    assert _collecting(store, "Allie X", [m.album_dir, Path("/m/Allie X/Cape God (2020)")])
