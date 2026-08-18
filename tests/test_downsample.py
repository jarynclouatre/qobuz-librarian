import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian.integrations import downsample_engine as de
from qobuz_librarian.integrations.downsample_engine import (
    _encode_opts_for_bps,
    detect_resampler_filter,
    read_local_bit_depth,
    read_sample_rate,
    read_total_samples,
    resample_one,
    target_rate,
)


@pytest.fixture
def _need_ffmpeg():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")


@pytest.fixture
def _need_flac():
    if shutil.which("flac") is None:
        pytest.skip("flac not available")


def _hires_flac(path: Path, seconds=2.0):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anoisesrc=sample_rate=96000:duration={seconds}:color=pink:amplitude=0.8",
         "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
         "-c:a", "flac", str(path)],
        check=True)


def _jpeg(path: Path, color="red"):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"color=c={color}:s=64x64", "-frames:v", "1", str(path)],
        check=True)
    return path.read_bytes()


def test_resample_preserves_source_bit_depth():
    base = "aresample=resampler=soxr"

    af, fmt, depth = _encode_opts_for_bps(24, base)
    assert fmt == "s32"
    assert depth == ["-bits_per_raw_sample", "24"]
    assert af.endswith("aformat=sample_fmts=s32")

    assert _encode_opts_for_bps(16, base) == (base, "s16", [])

    assert _encode_opts_for_bps(0, base) == (base, "s32", [])


def test_flac_header_parse_anchors_at_offset_zero():
    streaminfo = bytes(4) + bytes([0x12] * 20)         # block header + body bytes
    at_zero = b"fLaC" + streaminfo
    not_at_zero = b"ID3\x04\x00" + at_zero             # real marker pushed past 0

    assert de.parse_flac_info(at_zero) != (0, 0)       # anchored marker parses
    assert de.parse_flac_info(not_at_zero) == (0, 0)   # hidden marker ignored
    assert de.parse_flac_total_samples(at_zero) != 0
    assert de.parse_flac_total_samples(not_at_zero) == 0


def test_resample_clean_hires_shrinks_and_verifies(tmp_path, _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    in_size = src.stat().st_size
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert err is None
    assert saved is not None and saved > 0            # genuinely smaller
    assert src.stat().st_size == in_size - saved
    assert read_sample_rate(src) == 48000             # actually downsampled
    assert read_local_bit_depth(src) == 24            # depth preserved
    assert abs(read_total_samples(src) - 48000 * 2) < 48000
    assert subprocess.run(["flac", "-t", "-s", str(src)]).returncode == 0


def _peak_dbfs(path: Path) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                        "-af", "astats", "-f", "null", "-"],
                       capture_output=True, text=True)
    return max(float(line.split("Peak level dB:")[1])
               for line in r.stderr.splitlines() if "Peak level dB:" in line)


def test_resample_pulls_a_clipping_hot_source_below_full_scale(
        tmp_path, _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anoisesrc=sample_rate=96000:duration=2:color=pink:amplitude=0.9",
         "-af", "volume=6dB", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
         "-c:a", "flac", str(src)],
        check=True)
    assert _peak_dbfs(src) > -0.1
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert err is None
    assert saved is not None and saved > 0
    assert read_sample_rate(src) == 48000
    assert read_local_bit_depth(src) == 24
    assert _peak_dbfs(src) < -0.5


def test_resample_preserves_embedded_jpeg_and_all_pictures(tmp_path, _need_ffmpeg, _need_flac):
    from mutagen.flac import FLAC, Picture
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    front = _jpeg(tmp_path / "front.jpg", "red")
    back = _jpeg(tmp_path / "back.jpg", "blue")
    f = FLAC(str(src))
    for typ, data in ((3, front), (4, back)):
        pic = Picture()
        pic.type, pic.mime, pic.data = typ, "image/jpeg", data
        f.add_picture(pic)
    f.save()

    af, _ = detect_resampler_filter()
    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert err is None and saved is not None

    pics = FLAC(str(src)).pictures
    assert len(pics) == 2                              # -map 0 kept both
    for pic in pics:
        assert pic.mime == "image/jpeg"               # -c:v copy, not PNG
        assert pic.data[:2] == b"\xff\xd8"            # real JPEG SOI marker
    assert {bytes(p.data) for p in pics} == {front, back}


def test_resample_keeps_truncated_source_untouched(tmp_path, _need_ffmpeg, _need_flac):
    full = tmp_path / "full.flac"
    _hires_flac(full, 3.0)
    data = full.read_bytes()
    src = tmp_path / "track.flac"
    src.write_bytes(data[: len(data) * 2 // 5])        # 40%, header lies full
    before = src.read_bytes()
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert saved is None and err is not None
    assert src.read_bytes() == before                  # original untouched
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_resample_keeps_original_when_source_bit_depth_unreadable(
        tmp_path, monkeypatch, _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)                              # real 24-bit / 96 kHz
    before = src.read_bytes()
    monkeypatch.setattr(de, "read_local_bit_depth", lambda p, **_kwargs: 0)
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert saved is None and err is not None
    assert src.read_bytes() == before                  # master left untouched
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_resample_keeps_original_when_peak_probe_fails(
    tmp_path, monkeypatch, _need_ffmpeg, _need_flac
):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    before = src.read_bytes()
    monkeypatch.setattr(de, "_resampled_peak_dbfs", lambda *_a, **_k: None)
    af, _ = detect_resampler_filter()

    _rel, _sr, _rate, saved, err = resample_one("track.flac", 96000, 48000, af, base_dir=tmp_path)
    assert saved is None
    assert "couldn't verify the resampled peak" in err
    assert src.read_bytes() == before
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_resample_keeps_original_when_decode_fails(tmp_path, monkeypatch,
                                                   _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    before = src.read_bytes()
    monkeypatch.setattr(de, "_decode_ok", lambda p, **_kwargs: False)
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert saved is None and err == "resampled file failed verification"
    assert src.read_bytes() == before
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_target_rate_family_table():
    assert target_rate(96000) == 48000
    assert target_rate(192000) == 48000
    assert target_rate(88200) == 44100
    assert target_rate(176400) == 44100
    assert target_rate(44100) is None                  # already CD rate
    assert target_rate(48000) is None


def test_resample_keeps_original_when_flush_fails(tmp_path, monkeypatch,
                                                  _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    before = src.read_bytes()
    monkeypatch.setattr("qobuz_librarian.library.backup._fsync", lambda _p: False)
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert saved is None and err is not None
    assert src.read_bytes() == before
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_resample_refuses_to_overwrite_a_same_name_replacement(
        tmp_path, monkeypatch, _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    replacement = tmp_path / "replacement.flac"
    displaced = tmp_path / "displaced.flac"
    _hires_flac(src, 1.0)
    _hires_flac(replacement, 1.5)
    original_bytes = src.read_bytes()
    replacement_bytes = replacement.read_bytes()
    real_exchange = de._exchange_existing

    def replace_before_exchange(*args, **kwargs):
        src.rename(displaced)
        replacement.rename(src)
        return real_exchange(*args, **kwargs)

    monkeypatch.setattr(de, "_exchange_existing", replace_before_exchange)
    af, _ = detect_resampler_filter()

    rel, sr, rate, saved, err = resample_one(
        "track.flac", 96000, 48000, af, base_dir=tmp_path)

    assert saved is None and err is not None
    assert src.read_bytes() == replacement_bytes
    assert displaced.read_bytes() == original_bytes
    assert not list(tmp_path.glob(".compress-*.flac"))


def test_downsample_never_sweeps_a_glob_matching_user_file(
        tmp_path, monkeypatch):
    user_file = tmp_path / ".compress-user-master.flac"
    user_file.write_bytes(b"user-owned audio")
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))

    result = de.downsample_dir(tmp_path, verbose=False, base_dir=tmp_path)

    assert result["resampled"] == 0
    assert user_file.read_bytes() == b"user-owned audio"


def test_walk_binds_first_keep_choice_and_stops_if_it_cannot_be_saved(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import downsample as mode
    from qobuz_librarian.web import settings_store

    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    candidate = SimpleNamespace(
        album_dir=album_dir,
        artist="Artist",
        title="Album",
        detail="96 kHz to 48 kHz",
        est_saving=100,
    )
    refresh = SimpleNamespace(
        candidates=[candidate],
        artists_scanned=["Artist"],
        errors={},
    )
    answers = iter((True,))
    rewrites = []

    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", None)
    monkeypatch.setattr(mode, "HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(mode, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(mode, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(mode.hidden_mod, "load", lambda: {})
    monkeypatch.setattr(
        mode.downsample_state,
        "refresh_for_artists",
        lambda *_args, **_kwargs: refresh,
    )
    monkeypatch.setattr(mode, "_flush_stdin", lambda: None)
    monkeypatch.setattr(mode, "confirm", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(settings_store, "save", lambda _values: (False, []))
    monkeypatch.setattr(
        mode,
        "downsample_dir",
        lambda *_args, **kwargs: rewrites.append(kwargs) or {
            "resampled": 1,
            "errors": 0,
            "saved_bytes": 100,
            "flush_warnings": 0,
        },
    )
    monkeypatch.setattr(mode, "mark_local_album_capped", lambda _path: True)
    monkeypatch.setattr(mode.upgrade_state, "remove_album_dir", lambda _path: True)
    monkeypatch.setattr(mode.downsample_state, "update_artist", lambda *_a, **_k: None)
    monkeypatch.setattr(mode.downsample_state, "load", lambda: {})
    monkeypatch.setattr(
        mode.downsample_state,
        "has_visible_candidates",
        lambda *_args: False,
    )
    monkeypatch.setattr(mode.review_badges, "set_ready", lambda *_args: None)

    result = mode.run_downsample_walk_mode(
        SimpleNamespace(dry_run=False, yes=True),
    )

    assert result == mode.EXIT_CONFIG
    assert rewrites == []
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS is None

    # A running Web job can make settings_store defer applying the saved
    # value. The CLI run must use its answer directly rather than treating the
    # still-unset cfg value as "delete".
    answers = iter((True, True))
    monkeypatch.setattr(settings_store, "save", lambda _values: (True, []))

    result = mode.run_downsample_walk_mode(
        SimpleNamespace(dry_run=False, yes=True),
    )

    assert result == 0
    assert len(rewrites) == 1
    assert rewrites[0]["keep_originals"] is True
    assert cfg.DOWNSAMPLE_KEEP_ORIGINALS is None


def test_walk_reports_a_flush_warning_as_unfinished_work(
        tmp_path, monkeypatch, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import downsample as mode
    from qobuz_librarian.ui_cli import logging as cli_logging

    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    candidate = SimpleNamespace(
        album_dir=album_dir,
        artist="Artist",
        title="Album",
        detail="96 kHz to 48 kHz",
        est_saving=100,
    )
    refresh = SimpleNamespace(
        candidates=[candidate],
        artists_scanned=["Artist"],
        errors={},
    )

    monkeypatch.setattr(cfg, "DOWNSAMPLE_KEEP_ORIGINALS", "keep")
    monkeypatch.setattr(mode, "HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(mode, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(mode, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(mode.hidden_mod, "load", lambda: {})
    monkeypatch.setattr(
        mode.downsample_state,
        "refresh_for_artists",
        lambda *_args, **_kwargs: refresh,
    )
    monkeypatch.setattr(mode, "_flush_stdin", lambda: None)
    monkeypatch.setattr(mode, "confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        mode,
        "downsample_dir",
        lambda *_args, **_kwargs: {
            "resampled": 1,
            "errors": 0,
            "saved_bytes": 100,
            "flush_warnings": 1,
        },
    )
    monkeypatch.setattr(mode, "mark_local_album_capped", lambda _path: True)
    monkeypatch.setattr(mode.upgrade_state, "remove_album_dir", lambda _path: True)
    monkeypatch.setattr(mode.downsample_state, "update_artist", lambda *_a, **_k: None)
    monkeypatch.setattr(mode.downsample_state, "load", lambda: {})
    monkeypatch.setattr(
        mode.downsample_state,
        "has_visible_candidates",
        lambda *_args: False,
    )
    monkeypatch.setattr(mode.review_badges, "set_ready", lambda *_args: None)

    cli_logging.set_quiet(True)
    try:
        with caplog.at_level("INFO", logger="qobuz_librarian"):
            result = mode.run_downsample_walk_mode(
                SimpleNamespace(dry_run=False, yes=True),
            )
    finally:
        cli_logging.set_quiet(False)

    assert result == mode.EXIT_GENERAL
    assert any(
        record.levelname == "WARNING" and "couldn't be flushed" in record.getMessage()
        for record in caplog.records
    )


def test_downsample_dir_stops_between_tracks_on_cancel(tmp_path, monkeypatch):
    import time as _time

    calls = []
    logs = []

    def fake_resample(rel, sr, rate, af, base_dir=None):
        calls.append(rel)
        _time.sleep(0.2)
        return (rel, sr, rate, 10, None)

    monkeypatch.setattr(de, "RESAMPLE_WORKERS", 1)
    monkeypatch.setattr(de, "resample_one", fake_resample)
    monkeypatch.setattr(de, "read_sample_rate", lambda p: 96000)
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))
    for n in range(4):
        (tmp_path / f"{n}.flac").write_bytes(b"x")

    res = de.downsample_dir(
        tmp_path,
        verbose=True,
        base_dir=tmp_path,
        log=logs.append,
        cancel_check=lambda: len(calls) > 0,
    )

    assert res["cancelled"] is True
    assert res["resampled"] <= 2 and len(calls) <= 2   # rest were discarded
    assert any("⚠ downsample needs attention" in line for line in logs)
    assert not any("✓ downsample:" in line for line in logs)


def test_downsample_cancelled_during_safety_copy_starts_no_rewrite(tmp_path, monkeypatch):
    source = tmp_path / "track.flac"
    source.write_bytes(b"hi-res audio")
    kept = tmp_path / "kept-originals"
    kept.mkdir()
    cancelled = False
    rewrites = []
    discarded = []

    def stash(files, _album_dir, *, include_identity_receipts=False):
        nonlocal cancelled
        assert include_identity_receipts is True
        copied = set(files)
        receipts = {path: {"source": str(path)} for path in copied}
        cancelled = True
        return kept, copied, receipts

    def rewrite(*args, **kwargs):
        rewrites.append((args, kwargs))
        return (args[0], args[1], args[2], 10, None)

    monkeypatch.setattr(de, "read_sample_rate", lambda _path: 96000)
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))
    monkeypatch.setattr(de, "resample_one", rewrite)
    monkeypatch.setattr("qobuz_librarian.library.backup.stash_downsample_originals", stash)
    monkeypatch.setattr(de, "_discard_unused_stash", lambda path, _log: discarded.append(path))

    result = de.downsample_dir(
        tmp_path,
        verbose=False,
        base_dir=tmp_path,
        keep_originals=True,
        cancel_check=lambda: cancelled,
    )

    assert result == {
        "resampled": 0,
        "errors": 0,
        "saved_bytes": 0,
        "failed_files": [],
        "cancelled": True,
    }
    assert rewrites == []
    assert discarded == [kept]


def test_flush_failed_rewrite_counts_as_resampled_with_a_warning(tmp_path, monkeypatch):
    logs = []

    def fake_resample(rel, sr, rate, af, base_dir=None):
        return (rel, sr, rate, 10,
                "resampled, but the folder couldn't be flushed to disk")

    monkeypatch.setattr(de, "RESAMPLE_WORKERS", 1)
    monkeypatch.setattr(de, "resample_one", fake_resample)
    monkeypatch.setattr(de, "read_sample_rate", lambda p: 96000)
    monkeypatch.setattr(de, "detect_resampler_filter", lambda: ("soxr", "x"))
    (tmp_path / "0.flac").write_bytes(b"x")

    res = de.downsample_dir(
        tmp_path, verbose=True, base_dir=tmp_path, log=logs.append
    )

    assert res["resampled"] == 1
    assert res["errors"] == 0
    assert res["flush_warnings"] == 1
    assert res["saved_bytes"] == 10
    assert any("1 flush warning" in line for line in logs)
    assert not any("✓ downsample:" in line for line in logs)
