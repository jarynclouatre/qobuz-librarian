from pathlib import Path
from types import SimpleNamespace

from qobuz_librarian.library.discovery import AlbumGap
from qobuz_librarian.modes import artist as artist_mode
from qobuz_librarian.modes.artist import run_artist_missing_albums


def _qalbum(title, tracks_count, year):
    return {
        "id": title, "title": title, "tracks_count": tracks_count,
        "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1,
        "artist": {"name": "Bonobo", "id": 99},
        "release_date_original": f"{year}-01-01",
    }


def test_missing_albums_lists_partials_first_with_present_count(monkeypatch, capsys):
    """The missing-albums step lists a partially-present album (a collaboration
    filed under another folder) ahead of the fully-missing ones and shows how
    many tracks are already on disk. What counts as owned vs missing is the
    engine's call - see test_discovery; this checks the terminal presentation."""
    partial = AlbumGap(_qalbum("Black Sands", 11, 2010),
                       Path("/library/Black Sands"),
                       missing=[{}] * 8, present=[{}] * 3)
    absent = AlbumGap(_qalbum("Migration", 12, 2017), None)
    monkeypatch.setattr(artist_mode, "discover_fully_missing",
                        lambda *a, **k: [partial, absent])

    args = SimpleNamespace(prefer_hires=False, include_comps=True,
                           include_singles=True, dry_run=True, yes=False)
    run_artist_missing_albums("Bonobo", {}, args, "tok",
                              artist_id=99, prefetched_catalog=[])

    out = capsys.readouterr().out
    assert "3/11" in out
    # The partially-present album is listed first (a lower pick number).
    assert out.index("Black Sands") < out.index("Migration")
