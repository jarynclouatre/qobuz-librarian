from pathlib import Path

import pytest


def test_census_buckets_tiers_and_reclaims_only_true_hires(tmp_path, monkeypatch):
    from qobuz_librarian.library import flac_cache
    monkeypatch.setattr("qobuz_librarian.config.FLAC_CACHE_ENABLED", True)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "music")
    flac_cache._reset_for_tests()

    def track(artist, name, bits, sr, size):
        p = tmp_path / "music" / artist / "Album" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 16)
        flac_cache.put(p, {"bits": bits, "sample_rate": sr, "size": size,
                           "path": str(p)})

    track("A", "cd.flac", 16, 44100, 100)
    track("A", "h96.flac", 24, 96000, 1000)      # reclaims half (96 → 48)
    track("B", "h192.flac", 24, 192000, 2000)    # reclaims three quarters
    track("B", "cd24.flac", 24, 44100, 500)      # 24-bit at CD rate: no cut
    track("C", "untagged.mp3", 0, 0, 50)
    c = flac_cache.census()
    assert c["tiers"]["cd"] == [1, 100]
    assert c["tiers"]["hires96"] == [2, 1500]    # the 24/44.1 file sits here
    assert c["tiers"]["hires192"] == [1, 2000]
    assert c["tiers"]["unknown"] == [1, 50]
    assert c["reclaim_bytes"] == 500 + 1500      # only rates above their target
    assert c["top_hires_artists"][0] == ("B", 2500)
    flac_cache._reset_for_tests()


def test_census_ignores_staging_and_backup_copies(tmp_path, monkeypatch):
    # Every download and upgrade reads tags outside the library, so those paths
    # get cache rows too. Counting them made "What's on disk" climb with every
    # download and stay high for as long as an upgrade backup was kept.
    from qobuz_librarian.library import flac_cache
    monkeypatch.setattr("qobuz_librarian.config.FLAC_CACHE_ENABLED", True)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "music")
    flac_cache._reset_for_tests()

    for relative in ("music/A/Album/01.flac",
                     "staging/.qobuz-run-1/A/Album/01.flac",
                     "upgrade_backups/20260814_A_Album/01.flac"):
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 16)
        flac_cache.put(p, {"bits": 24, "sample_rate": 96000, "size": 1000,
                           "path": str(p)})

    c = flac_cache.census()
    assert c["total_tracks"] == 1
    assert c["total_bytes"] == 1000
    assert c["top_hires_artists"] == [("A", 1000)]
    flac_cache._reset_for_tests()


def test_walk_error_does_not_cache_dir_as_audioless(tmp_path, monkeypatch):
    # A transient scandir failure is consumed by os.walk's error callback, so
    # the shortened walk finds nothing - that must not be cached as "contains
    # no audio" or the artist vanishes from the whole scan.
    from qobuz_librarian.library import scanner

    scanner._HAS_AUDIO_CACHE.clear()
    (tmp_path / "Album").mkdir()
    (tmp_path / "Album" / "01.flac").write_bytes(b"x")

    calls = {"n": 0}
    real_walk = scanner.os.walk

    def flaky_walk(root, followlinks=False, onerror=None):
        calls["n"] += 1
        if calls["n"] == 1:
            if onerror is not None:
                onerror(OSError("transient EIO"))
            return iter(())
        return real_walk(root, followlinks=followlinks, onerror=onerror)

    monkeypatch.setattr(scanner.os, "walk", flaky_walk)
    with pytest.raises(OSError, match="transient EIO"):
        scanner.list_artist_album_dirs(tmp_path)
    # The failure must not poison the cache; the next complete listing sees it.
    assert scanner.list_artist_album_dirs(tmp_path) == [tmp_path / "Album"]


def test_album_listing_root_error_is_not_an_empty_artist(tmp_path, monkeypatch):
    from qobuz_librarian.library import scanner

    real_iterdir = Path.iterdir

    def unreadable(path):
        if path == tmp_path:
            raise OSError("artist root EIO")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", unreadable)
    with pytest.raises(OSError, match="artist root EIO"):
        scanner.list_artist_album_dirs(tmp_path)


def test_landed_album_is_cached_and_its_staging_copy_is_not(tmp_path, monkeypatch):
    # A finished download used to leave no trace in the tag cache, so the census
    # behind "What's on disk" stayed at whatever the last library scan found and
    # fell further behind with every album. Caching the staging copy instead
    # would bring back the opposite fault, a count that climbs on its own.
    from qobuz_librarian.library import flac_cache, scanner
    monkeypatch.setattr("qobuz_librarian.config.FLAC_CACHE_ENABLED", True)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "music")
    flac_cache._reset_for_tests()

    landed = tmp_path / "music" / "Artist" / "Album (2020)"
    staged = tmp_path / "staging" / ".qobuz-run-1" / "Artist" / "Album (2020)"
    for directory in (landed, staged):
        directory.mkdir(parents=True)
        (directory / "01 - Song.flac").write_bytes(b"x" * 16)

    scanner.cache_album_tags([landed, staged])

    assert flac_cache.get(landed / "01 - Song.flac") is not None
    assert flac_cache.get(staged / "01 - Song.flac") is None
    flac_cache._reset_for_tests()


def test_store_stamp_moves_when_another_process_writes(tmp_path, monkeypatch):
    # A terminal download writes this cache from its own process, so the web
    # app's memoized census held pre-download totals until its timer expired.
    import sqlite3

    from qobuz_librarian.library import flac_cache
    monkeypatch.setattr("qobuz_librarian.config.FLAC_CACHE_ENABLED", True)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "music")
    flac_cache._reset_for_tests()

    track = tmp_path / "music" / "A" / "Album" / "cd.flac"
    track.parent.mkdir(parents=True, exist_ok=True)
    track.write_bytes(b"x" * 16)
    flac_cache.put(track, {"bits": 16, "sample_rate": 44100, "size": 100,
                           "path": str(track)})
    flac_cache.census()
    before = flac_cache.store_stamp()

    other = sqlite3.connect(str(tmp_path / "data" / "flac_cache.db"))
    other.execute(
        "INSERT OR REPLACE INTO files (path, mtime_ns, size, payload) "
        "VALUES (?,?,?,?)",
        (str(track.parent / "second.flac"), 0, 0, "{}"),
    )
    other.commit()
    other.close()

    assert flac_cache.store_stamp() != before
    flac_cache._reset_for_tests()
