"""Tests for quality/tiers.py and quality/decision.py."""


from qobuz_librarian.quality.decision import (
    compare_album_quality,
    quality_change_summary,
)


def test_streamrip_quality_tier_1_coerces_to_lossless(monkeypatch):
    # Tier 1 (320kbps MP3) is unsupported: the pipeline is FLAC-only and the
    # post-download cleanup discards every non-FLAC file, so a tier-1 setting
    # would rip each track and then delete it, so the setting downloads
    # nothing.
    import importlib

    from qobuz_librarian import config as cfg
    monkeypatch.setenv("STREAMRIP_QUALITY", "1")
    importlib.reload(cfg)
    try:
        assert cfg.STREAMRIP_QUALITY == 2          # coerced to CD lossless, not left at 1
    finally:
        # streamrip_quality_cap() reads cfg.STREAMRIP_QUALITY live, so reset it
        # here (not only via teardown) so tier 2 can't leak into later tests.
        monkeypatch.delenv("STREAMRIP_QUALITY", raising=False)
        importlib.reload(cfg)


def test_compare_album_quality_classifies_and_counts_unknown():
    qalbum = {"maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}
    # No existing tracks stays distinct from an equal-quality album.
    assert compare_album_quality([], qalbum)["classification"] == "no_existing"
    # All-lower → all-upgrading territory.
    assert compare_album_quality(
        [{"bits": 16, "sample_rate": 44100}], qalbum)["classification"] == "all_lower"
    # All-equal must stay distinct so the upgrade
    # flow doesn't kick off a wipe-replace for parity.
    assert compare_album_quality(
        [{"bits": 24, "sample_rate": 96000}], qalbum)["classification"] == "all_equal"
    # An unreadable track (bits=0) gets surfaced as n_unknown so the upgrade
    # path won't wipe-replace it unverified.
    r = compare_album_quality(
        [{"bits": 16, "sample_rate": 44100}, {"bits": 0, "sample_rate": 0}], qalbum)
    assert r["n_unknown"] == 1
    assert r["classification"] == "unknown"
    crossed = compare_album_quality(
        [{"bits": 16, "sample_rate": 192000}], qalbum)
    assert crossed["classification"] == "incomparable"
    assert crossed["n_incomparable"] == 1
    assert compare_album_quality(
        [{"bits": 16, "sample_rate": 44100}],
        {"maximum_bit_depth": 0, "maximum_sampling_rate": 96.0},
    )["classification"] == "unknown"
    # A per-track downgrade from hi-res is flagged so it can be refused.
    hires = {"bits": 24, "sample_rate": 96000, "channels": 2}
    cd = {"bits": 16, "sample_rate": 44100, "channels": 2}
    assert quality_change_summary([(hires, cd)])["losing_hires"] == 1


def test_upgrade_scan_skips_locally_capped_downsample_album(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import decision

    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    qobuz_album = {
        "id": "qobuz-album",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 192.0,
        "tracks": {"items": [{"title": "Song"}]},
    }

    decision.mark_local_album_capped(album_dir)
    monkeypatch.setattr(decision, "list_artist_album_dirs",
                        lambda _artist_dir: [album_dir])
    monkeypatch.setattr(decision, "search_artists", lambda *a, **k: [])
    monkeypatch.setattr(
        "qobuz_librarian.library.catalog.find_qobuz_album_for_dir",
        lambda *a, **k: qobuz_album,
    )

    def fail_if_quality_compared(*_args, **_kwargs):
        raise AssertionError("locally capped albums should skip quality compare")

    monkeypatch.setattr(decision, "find_existing_tracks", fail_if_quality_compared)

    result = decision.scan_artist_for_upgrades(
        "Artist",
        artist_dir,
        "tok",
        type("Args", (), {"prefer_hires": True, "no_upgrade": False})(),
        capped=decision.load_capped(),
    )

    assert result == []
