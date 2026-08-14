from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_direct_artist_download_refreshes_saved_state(monkeypatch):
    album = _qalbum("Migration", 1, 2017)
    full = {**album, "tracks": {"items": [{"id": "track-id"}]}}
    gap = AlbumGap(album, None)
    result = {
        "dir": Path("/library/Bonobo/Migration"),
        "result": "downloaded",
        "imported": True,
        "n_ok": 1,
    }
    refreshed = []

    monkeypatch.setattr(
        artist_mode,
        "discover_fully_missing",
        lambda *_args, **_kwargs: [gap],
    )
    monkeypatch.setattr(artist_mode, "_flush_stdin", lambda: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    monkeypatch.setattr(artist_mode, "get_album", lambda *_args: full)
    monkeypatch.setattr(
        artist_mode,
        "process_album",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        artist_mode,
        "_refresh_review_state_after_downloads",
        lambda changes, token, args: refreshed.append((changes, token, args)),
    )
    monkeypatch.setattr(artist_mode.time, "sleep", lambda _seconds: None)
    args = SimpleNamespace(
        prefer_hires=False,
        include_comps=True,
        include_singles=True,
        dry_run=False,
        yes=False,
    )

    downloaded, needs_attention = run_artist_missing_albums(
        "Bonobo",
        {},
        args,
        "token",
        artist_id=99,
        prefetched_catalog=[],
    )

    assert (downloaded, needs_attention) == (1, False)
    assert refreshed == [([{**result, "album": full}], "token", args)]


def test_shared_missing_album_admits_before_queueing(monkeypatch):
    from qobuz_librarian.api import client
    from qobuz_librarian.api.auth import DownloaderNotReady, credentials_from_values

    album = _qalbum("Migration", 1, 2017)
    full = {**album, "tracks": {"items": [{"id": "track-id"}]}}
    gap = AlbumGap(album, None)
    queued = []

    monkeypatch.setattr(
        artist_mode,
        "discover_fully_missing",
        lambda *_args, **_kwargs: [gap],
    )
    monkeypatch.setattr(artist_mode, "_flush_stdin", lambda: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    monkeypatch.setattr(artist_mode, "get_album", lambda *_args: full)
    monkeypatch.setattr(artist_mode.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        client,
        "authorize_qobuz_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DownloaderNotReady()
        ),
    )
    args = SimpleNamespace(
        prefer_hires=False,
        include_comps=True,
        include_singles=True,
        dry_run=False,
        yes=False,
    )
    token = credentials_from_values(
        "",
        "token",
        source="streamrip",
    ).token

    with pytest.raises(DownloaderNotReady):
        run_artist_missing_albums(
            "Bonobo",
            {},
            args,
            token,
            artist_id=99,
            prefetched_catalog=[],
            shared_queue=queued,
        )

    assert queued == []
