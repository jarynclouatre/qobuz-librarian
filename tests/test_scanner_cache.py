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
