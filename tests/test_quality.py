"""Tests for quality/tiers.py and quality/decision.py."""

from datetime import datetime, timedelta, timezone

from qobuz_librarian.quality.decision import (
    album_max_quality,
    compare_album_quality,
    load_capped,
    quality_change_summary,
    save_capped,
)
from qobuz_librarian.quality.tiers import format_quality


def test_format_quality_renders_known_tiers_and_unknown():
    assert format_quality(16, 44100) == "16/44.1"
    assert format_quality(24, 96000) == "24/96"
    # Unreadable tracks stay visible as unknown.
    assert format_quality(0, 44100) == "?"


def test_streamrip_quality_tier_1_coerces_to_lossless(monkeypatch, capsys):
    # Tier 1 (320kbps MP3) is unsupported: the pipeline is FLAC-only and the
    # post-download cleanup discards every non-FLAC file, so a tier-1 setting
    # would rip each track and then delete it, so the setting downloads
    # nothing.
    import importlib

    from qobuz_librarian import config as cfg
    monkeypatch.setenv("STREAMRIP_QUALITY", "1")
    importlib.reload(cfg)
    try:
        err = capsys.readouterr().err
        assert cfg.STREAMRIP_QUALITY == 2          # coerced to CD lossless, not left at 1
        assert "STREAMRIP_QUALITY" in err and "320" in err
    finally:
        # streamrip_quality_cap() reads cfg.STREAMRIP_QUALITY live, so reset it
        # here (not only via teardown) so tier 2 can't leak into later tests.
        monkeypatch.delenv("STREAMRIP_QUALITY", raising=False)
        importlib.reload(cfg)


def test_album_max_quality_keeps_qobuz_master_when_downsample_enabled(monkeypatch):
    # Upgrade discovery should still report the best Qobuz can provide.
    monkeypatch.setattr("qobuz_librarian.config.STREAMRIP_QUALITY", 4)
    monkeypatch.setattr("qobuz_librarian.config.DOWNSAMPLE_HIRES_ENABLED", True)
    assert album_max_quality(
        {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}) == (24, 192000)
    assert album_max_quality(
        {"maximum_bit_depth": 24, "maximum_sampling_rate": 88.2}) == (24, 88200)
    # 44.1/48 kHz aren't resampled, so they pass through.
    assert album_max_quality(
        {"maximum_bit_depth": 16, "maximum_sampling_rate": 44.1}) == (16, 44100)


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


def test_quality_change_summary_counts_upgrades_and_losses():
    t = lambda b, r, channels=2: {
        "bits": b, "sample_rate": r, "channels": channels,
    }
    assert quality_change_summary([(t(16, 44100), t(24, 96000))])["upgrading"] == 1
    # A would-be downgrade from hi-res must be flagged so we can refuse it.
    assert quality_change_summary([(t(24, 96000), t(16, 44100))])["losing_hires"] == 1
    uncertain = quality_change_summary([
        (t(16, 192000), t(24, 96000)),
        (t(24, 96000, 6), t(24, 96000, 2)),
    ])
    assert uncertain["unknown"] == 2


def test_capped_persistence_round_trips_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.CAPPED_FILE", tmp_path / "capped.json")
    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    save_capped({"old": {"ts": stale, "title": "Stale"},
                 "new": {"ts": fresh, "title": "Fresh"}})
    loaded = load_capped()
    assert loaded == {"new": {"ts": fresh, "title": "Fresh"}}

    # If someone hand-edits the file into a JSON list, load_capped must return
    # a dict so is_album_capped's .get() doesn't blow up the upgrade scan.
    (tmp_path / "capped.json").write_text('["x", "y"]', encoding="utf-8")
    assert load_capped() == {}


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
