"""Discover recommendation tests."""

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.api import discover_cache as dc
from qobuz_librarian.library import recommendations as rec


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    dc._reset_for_tests()
    rec._reset_for_tests()
    yield
    dc._reset_for_tests()
    rec._reset_for_tests()


def _library(names):
    """A Library built from folder names, without touching a disk."""
    return rec.Library(
        keys={k for k in (rec.normalize(n) for n in names) if k},
        raws=list(names),
        seeds=list(names),
        signature="sig",
    )


def test_owned_artists_are_recognised_through_spelling_variants():
    owned = _library(["The Beatles", "Sigur Rós"])
    for name in ("Beatles", "Sigur Ros"):
        assert owned.owns(name), name
    # Ranking drops an owned artist under either spelling.
    ranked = rec.rank_candidates({"Sigur Ros": {"score": 5.0, "seeds": [("Mogwai", 1.0)]},
                                  "Om": {"score": 0.3, "seeds": [("Mogwai", 0.3)]}}, owned)
    assert [c["name"] for c in ranked] == ["Om"]
