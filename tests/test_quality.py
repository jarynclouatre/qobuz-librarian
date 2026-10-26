"""Tests for quality/tiers.py and quality/decision.py."""

import pytest

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


def test_upgrade_scan_leaves_downsampled_albums_alone_while_downsample_is_on(
        tmp_path, monkeypatch):
    """A downsampled album was offered its hi-res master again whenever the
    album's Qobuz rating hid nothing, and with Downsample on every download
    of it would be downsampled again. While Downsample is on, a file is held
    to what a download keeps after it; once it is off, to the full quality,
    and an album downsampled as it downloaded is offered again. One the user
    shrank with Downsample stays out either way."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import discovery
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

    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", True)
    monkeypatch.setattr(decision.downsample_engine, "HAVE_DOWNSAMPLE", True)
    master = {"maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}
    kept = {"bits": 24, "sample_rate": 48000}
    cd = {"bits": 16, "sample_rate": 44100}
    assert decision.compare_album_quality([kept], master)["classification"] == "all_equal"
    assert decision.compare_album_quality([cd], master)["classification"] == "all_lower"

    decision.mark_local_album_capped(album_dir, origin="auto")
    monkeypatch.setattr(decision, "list_artist_album_dirs",
                        lambda _artist_dir: [album_dir])
    monkeypatch.setattr(discovery, "_resolve_cache", {})
    monkeypatch.setattr(discovery, "search_artists", lambda *a, **k: [])
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
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    assert decision.compare_album_quality([kept], master)["classification"] == "all_lower"
    with pytest.raises(AssertionError, match="skip quality compare"):
        decision.scan_artist_for_upgrades(
            "Artist", artist_dir, "tok",
            type("Args", (), {"prefer_hires": True, "no_upgrade": False})(),
            capped=decision.load_capped())
    decision.mark_local_album_capped(album_dir)
    assert decision.scan_artist_for_upgrades(
        "Artist", artist_dir, "tok",
        type("Args", (), {"prefer_hires": True, "no_upgrade": False})(),
        capped=decision.load_capped()) == []


def test_upgrade_scan_offers_only_the_edition_on_disk(tmp_path, monkeypatch):
    # Upgrade offered a 2026 mix in place of a 2009 remaster because Prefer
    # hi-res matched the folder to the highest-resolution edition, and a
    # labelled remaster in place of an earlier master of its recordings.
    from qobuz_librarian.library import catalog, discovery
    from qobuz_librarian.quality import decision

    album_dir = tmp_path / "The Beatles" / "Rubber Soul (1965)"
    album_dir.mkdir(parents=True)
    songs = ["Drive My Car", "Norwegian Wood", "Nowhere Man", "Girl"]
    files = [{"title": f"{song} (Remastered)", "album": "Rubber Soul",
              "isrc": f"GBAYE060147{i}", "bits": 16, "sample_rate": 44100,
              "channels": 2, "tracknumber": i + 1}
             for i, song in enumerate(songs)]

    def release(album_id, version, track_version, isrc_prefix):
        return {
            "id": album_id, "title": "Rubber Soul", "version": version,
            "artist": {"name": "The Beatles"}, "release_date_original": "1965-12-03",
            "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
            "tracks_count": len(songs), "streamable": True,
            "tracks": {"items": [
                {"id": f"{album_id}-{i}", "title": song, "version": track_version,
                 "isrc": f"{isrc_prefix}{i}", "track_number": i + 1}
                for i, song in enumerate(songs)]},
        }

    new_mix = release("mix", "2026 Mix", "2026 Mix", "GBUM7260036")
    remaster = release("remaster", "Remastered 2009", "Remastered", "GBAYE060147")
    releases = {"mix": new_mix, "remaster": remaster}
    listed = [new_mix, remaster]

    monkeypatch.setattr(decision, "list_artist_album_dirs", lambda _d: [album_dir])
    monkeypatch.setattr(discovery, "resolve_artist", lambda *a: ("26390", "The Beatles"))
    monkeypatch.setattr(decision, "get_artist_albums", lambda *a, **k: (listed, len(listed)))
    monkeypatch.setattr(decision, "get_album", lambda album_id, _token: releases[album_id])
    monkeypatch.setattr(catalog, "find_qobuz_album_for_dir", lambda *a, **k: new_mix)
    monkeypatch.setattr(catalog, "find_album_dir_filesystem", lambda *a, **k: album_dir)
    monkeypatch.setattr(decision, "find_existing_tracks", lambda _album: (files, album_dir))
    args = type("Args", (), {"prefer_hires": True, "no_upgrade": False})()

    found = decision.scan_artist_for_upgrades(
        "The Beatles", album_dir.parent, "tok", args, capped={})
    assert [c["qobuz_album"]["id"] for c in found] == ["remaster"]
    # Nor one Qobuz won't stream, whole or a track of it.
    remaster["streamable"] = False
    assert decision.scan_artist_for_upgrades(
        "The Beatles", album_dir.parent, "tok", args, capped={}) == []
    remaster["streamable"] = True
    remaster["tracks"]["items"][2]["streamable"] = False
    assert decision.scan_artist_for_upgrades(
        "The Beatles", album_dir.parent, "tok", args, capped={}) == []
    del remaster["tracks"]["items"][2]["streamable"]

    for local, song in zip(files, songs):
        local["title"] = song
    assert decision.scan_artist_for_upgrades(
        "The Beatles", album_dir.parent, "tok", args, capped={}) == []
