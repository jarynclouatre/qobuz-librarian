from pathlib import Path

from qobuz_librarian.library.downsample import DownsampleCandidate


def _candidate(album_dir: Path, artist="Artist", title="Album"):
    return DownsampleCandidate(
        album_dir=album_dir,
        artist=artist,
        title=title,
        n_hires=2,
        n_flac=3,
        source_rates=[96000],
        target_rates=[48000],
        est_saving=1234,
    )


def test_refresh_for_artists_writes_shared_downsample_state(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    candidate = _candidate(album_dir)

    result = downsample_state.refresh_for_artists(
        [artist_dir],
        hidden=None,
        scan_artist=lambda _ad: [candidate],
    )

    assert result.complete is True
    assert result.candidates == [candidate]
    state = downsample_state.load()
    assert state["complete"] is True
    assert state["artists_scanned"] == ["Artist"]
    assert state["candidates"][0]["artist"] == "Artist"
    assert state["candidates"][0]["album_dir"] == str(album_dir)


def test_refresh_for_artists_uses_downsample_hidden_scope(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, hidden

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    candidate = _candidate(album_dir)
    hidden_store = hidden.load()
    hidden_store[hidden.SCOPE_DOWNSAMPLE][hidden.album_fingerprint("Artist", "Album")] = {
        "artist": "Artist",
        "title": "Album",
    }

    result = downsample_state.refresh_for_artists(
        [artist_dir],
        hidden=hidden_store,
        scan_artist=lambda _ad: [candidate],
    )

    assert result.candidates == []
    assert downsample_state.load()["candidates"] == []


def test_update_artist_replaces_only_that_artists_downsample_candidates(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    old_artist = tmp_path / "Artist"
    other_artist = tmp_path / "Other Artist"
    old_album = old_artist / "Old Album"
    other_album = other_artist / "Other Album"
    new_album = old_artist / "New Album"
    for path in (old_album, other_album, new_album):
        path.mkdir(parents=True)

    downsample_state.save(downsample_state.RefreshResult(
        candidates=[
            _candidate(old_album, artist="Artist", title="Old Album"),
            _candidate(other_album, artist="Other Artist", title="Other Album"),
        ],
        artists_scanned=["Artist", "Other Artist"],
        errors={},
        complete=True,
    ))

    result = downsample_state.update_artist(
        old_artist,
        hidden=None,
        scan_artist=lambda _ad: [
            _candidate(new_album, artist="Artist", title="New Album")
        ],
    )

    assert [c.title for c in result.candidates] == ["New Album"]
    state = downsample_state.load()
    assert [c["title"] for c in state["candidates"]] == [
        "Other Album",
        "New Album",
    ]
    assert state["artists_scanned"] == ["Artist", "Other Artist"]


def test_cancelled_refresh_keeps_last_complete_downsample_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    downsample_state.save(downsample_state.RefreshResult(
        [_candidate(album_dir)],
        ["Artist"],
        {},
        True,
    ))

    result = downsample_state.refresh_for_artists(
        [artist_dir],
        scan_artist=lambda _ad: (_ for _ in ()).throw(
            AssertionError("cancelled refresh should not scan")),
        cancel_check=lambda: True,
    )

    state = downsample_state.load()
    assert result.complete is False
    assert state["complete"] is True
    assert [c["title"] for c in state["candidates"]] == ["Album"]


def test_fingerprint_failure_is_recorded_without_aborting_other_artists(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    denied = tmp_path / "Denied"
    readable = tmp_path / "Readable"
    album = readable / "Album"
    denied.mkdir()
    album.mkdir(parents=True)
    candidate = _candidate(album, artist="Readable")

    def fingerprint(path):
        if path == denied:
            raise PermissionError("cannot read album folder")
        return "readable-fingerprint"

    monkeypatch.setattr(downsample_state, "artist_fingerprint", fingerprint)
    result = downsample_state.refresh_for_artists(
        [denied, readable],
        scan_artist=lambda path: [candidate] if path == readable else [],
        persist=False,
    )

    assert result.complete is False
    assert result.artists_scanned == ["Denied", "Readable"]
    assert result.errors == {"Denied": "cannot read album folder"}
    assert result.candidates == [candidate]
    assert result.fingerprints == {"Readable": "readable-fingerprint"}

    targeted = downsample_state.update_artist(
        denied,
        scan_artist=lambda _path: (_ for _ in ()).throw(
            AssertionError("fingerprint failure must stop this artist scan")),
    )
    assert targeted.complete is False
    assert targeted.errors == {"Denied": "cannot read album folder"}

