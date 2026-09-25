"""Tests for qobuz_librarian.library.scanner - library walking, album reads,
and the FLAC tag cache."""


def test_flac_cache_hits_when_unchanged_and_invalidates_on_change(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import flac_cache
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "FLAC_CACHE_ENABLED", True)
    flac_cache._reset_for_tests()
    try:
        f = tmp_path / "song.flac"
        f.write_bytes(b"abc")
        assert flac_cache.get(f) is None                        # cold miss
        flac_cache.put(f, {"title": "T", "isrc": "X"})
        assert flac_cache.get(f) == {"title": "T", "isrc": "X"}  # hit, unchanged
        f.write_bytes(b"abcd")                                   # size change invalidates
        assert flac_cache.get(f) is None                        # self-invalidated
    finally:
        flac_cache._reset_for_tests()


def test_transient_read_error_is_a_walk_error_not_untagged(monkeypatch, tmp_path):
    """A file mutagen can't READ (EIO/EACCES) is not a file with no tags: the
    filename fallback would blank its ISRC and quality - undercounting the
    censuses that gate backup deletion - and a cached negative would keep the
    identity blanked on every later scan until the file changes."""
    from qobuz_librarian.library import flac_cache, scanner

    album = tmp_path / "Album"
    album.mkdir()
    f = album / "01 - Song.flac"
    f.write_bytes(b"x")

    calls = {"n": 0}

    class Boom:
        def __call__(self, path, easy=True):
            calls["n"] += 1
            raise OSError(5, "EIO")

    monkeypatch.setattr(scanner, "HAVE_MUTAGEN", True)
    monkeypatch.setattr(scanner, "mutagen",
                        type("M", (), {"File": staticmethod(Boom())}))
    errs = []
    tracks = scanner.read_album_dir(album, walk_errors=errs)
    assert tracks == []          # dropped, not filename-fallback'd
    assert errs                  # and the walk reports it

    # Nothing was negative-cached: once the file reads again, its tags return.
    assert flac_cache.get(f) is None

    class Tags(dict):
        pass

    def good(path, easy=True):
        info = type("I", (), {"bits_per_sample": 16, "sample_rate": 44100,
                              "channels": 2, "length": 60.0})()
        obj = type("F", (), {})()
        obj.tags = {"title": ["Song"], "isrc": ["USAAA0000001"]}
        obj.info = info
        return obj

    monkeypatch.setattr(scanner, "mutagen", type("M", (), {"File": staticmethod(good)}))
    errs2 = []
    tracks2 = scanner.read_album_dir(album, walk_errors=errs2)
    assert not errs2
    assert [t["isrc"] for t in tracks2] == ["USAAA0000001"]


def test_an_artist_with_audio_survives_an_unreadable_subfolder(tmp_path, monkeypatch):
    # The audio check stops at the first track it finds, so whether it meets an
    # unreadable subfolder before or after that track is directory order. It
    # decided whether the artist was scanned at all, which made the same
    # library behave differently on CI than on a developer's disk.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import scanner

    music = tmp_path / "music"
    artist = music / "Partly readable"
    (artist / "Album").mkdir(parents=True)
    (artist / "Album" / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)

    def raises(_d, walk_errors=None):
        raise PermissionError(13, "Permission denied", str(artist / "Blocked"))

    monkeypatch.setattr(scanner, "_has_audio_anywhere", raises)
    reported = []
    found = scanner.list_library_artists(
        on_artist_error=lambda path, error: reported.append(path))

    assert found == [artist]
    assert reported == [artist]
