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
    f["GENRE"] = ["Rock", "Pop"]
    f.save()

    af, _ = detect_resampler_filter()
    rel, sr, rate, saved, err = resample_one("track.flac", 96000, 48000, af,
                                             base_dir=tmp_path)
    assert err is None and saved is not None

    pics = FLAC(str(src)).pictures
    assert len(pics) == 2                              # both carried over
    for pic in pics:
        assert pic.mime == "image/jpeg"               # verbatim, not re-encoded
        assert pic.data[:2] == b"\xff\xd8"            # real JPEG SOI marker
    assert {(p.type, bytes(p.data)) for p in pics} == {(3, front), (4, back)}
    assert FLAC(str(src))["GENRE"] == ["Rock", "Pop"]   # not one "Rock;Pop"


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


def test_resample_keeps_original_when_decode_fails(tmp_path, monkeypatch,
                                                   _need_ffmpeg, _need_flac):
    src = tmp_path / "track.flac"
    _hires_flac(src, 2.0)
    before = src.read_bytes()
    verdicts = iter([True, False])                     # source, then encode
    monkeypatch.setattr(de, "_decode_ok", lambda p, **_kwargs: next(verdicts))
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
    monkeypatch.setattr(mode, "list_library_artists",
                        lambda **_kw: [artist_dir])
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
