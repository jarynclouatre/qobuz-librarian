import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.integrations.rip import flac_audio_offset
from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs


@pytest.fixture
def _need_tools():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    if shutil.which("flac") is None:
        pytest.skip("flac not available")


def _make_flac(path: Path, *, seconds=4, amp=0.5, isrc="USABC1234500"):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anoisesrc=duration={seconds}:color=white:amplitude={amp}",
         "-ac", "2", "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
         str(path)], check=True)
    from mutagen.flac import FLAC
    f = FLAC(str(path))
    if isrc:
        f["isrc"] = isrc
    f["title"] = path.stem
    f["tracknumber"] = "1"
    f.save()


def _frame_corrupt(path: Path):
    off = flac_audio_offset(str(path)) or 8192
    size = path.stat().st_size
    start = off + (size - off) // 2
    n = min(4096, size - start - 16)
    with open(path, "r+b") as fh:
        fh.seek(start)
        cur = fh.read(n)
        fh.seek(start)
        fh.write(bytes((b ^ 0xFF) for b in cur))


def _decodes(path: Path) -> bool:
    return subprocess.run(["flac", "-t", "-s", str(path)],
                          capture_output=True).returncode == 0


def _names(entries):
    return {Path(e["path"]).name for e in entries}


# The title has to read like the file's own ("02.flac" is tagged "02"), because
# a catalogue title that names a different song withdraws the single-track
# refill on purpose. These fixtures are about lease and decode behaviour, so
# they must not trip the identity gate as a side effect.
_QT = {"duration": 4.0, "title": "02", "track_number": 1, "isrc": "USABC1234500"}


def test_shallow_scan_catches_frame_crc_corruption(tmp_path, _need_tools):
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "01.flac"
    _make_flac(p)
    _frame_corrupt(p)
    assert not _decodes(p), "fixture should be genuinely corrupt"

    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_QT):
        r = scan_dir_for_isrc_repairs(album, "token", deep=False)

    assert "01.flac" in _names(r["verified_truncated"]), (
        "shallow sweep must flag a frame-CRC-corrupt FLAC, not pass it as ok "
        f"(got {r})")
    assert r["verified_truncated"][0]["reason"] == "decode_failed"


def test_shallow_scan_surfaces_no_isrc_corruption(tmp_path, _need_tools):
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "01.flac"
    _make_flac(p, isrc=None)
    _frame_corrupt(p)
    assert not _decodes(p)

    r = scan_dir_for_isrc_repairs(album, "token", deep=False)
    diagnosed = {Path(e["path"]).name for e in r["no_isrc_tag"]
                 if e.get("diagnostic")}
    assert "01.flac" in diagnosed, (
        f"shallow sweep must surface a corrupt no-ISRC FLAC, not skip it (got {r})")


def test_shallow_scan_does_not_false_flag_healthy(tmp_path, _need_tools):
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    _make_flac(album / "01.flac")              # normal noise
    _make_flac(album / "02.flac", amp=0.01)    # quiet but valid
    assert _decodes(album / "01.flac") and _decodes(album / "02.flac")

    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_QT):
        r = scan_dir_for_isrc_repairs(album, "token", deep=False)

    assert r["verified_truncated"] == [], f"healthy files must not be flagged (got {r})"
    assert r["verified_ok"] == 2


def test_byte_size_gate_does_not_flag_a_small_valid_flac_with_flac_absent(tmp_path, monkeypatch):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")

    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "01 - Quiet.flac"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", "4", "-sample_fmt", "s16",
         "-c:a", "flac", str(p)], check=True)
    from mutagen.flac import FLAC
    f = FLAC(str(p))
    f["isrc"] = "USABC1234500"
    f["title"] = "Quiet"
    f["tracknumber"] = "1"
    f.save()

    import qobuz_librarian.repair_log as rl
    monkeypatch.setattr(rl, "find_qobuz_track_by_isrc", lambda i, t: _QT)
    real_which = shutil.which
    monkeypatch.setattr(rl.shutil, "which",
                        lambda name, *a, **k: None if name == "flac"
                        else real_which(name, *a, **k))

    r = scan_dir_for_isrc_repairs(album, "token", deep=False)
    assert "01 - Quiet.flac" not in _names(r["verified_truncated"]), (
        f"a small but valid FLAC must not be flagged when flac is absent (got {r})")


def test_duration_gate_abs_cap_flags_long_track_short_by_over_a_minute(
        monkeypatch, tmp_path):
    import qobuz_librarian.repair_log as rl

    album = tmp_path / "album"
    album.mkdir()
    source = album / "01.flac"
    source.write_bytes(b"held source")

    def entry(flen):
        return {"path": str(source), "title": "t",
                "isrc": "USABC1234500", "length": flen,
                "sample_rate": 44100, "bits": 16, "channels": 2}

    qt = {"duration": 600.0, "title": "t", "track_number": 1, "isrc": "USABC1234500"}
    monkeypatch.setattr(rl, "_qobuz_track_by_isrc", lambda i, t: qt)
    monkeypatch.setattr(rl, "_flac_decode_ok", lambda *a, **k: True)

    monkeypatch.setattr(rl, "_read_held_audio_meta", lambda _s: entry(531.0))
    flagged = rl.scan_dir_for_isrc_repairs(
        album, "tok", deep=True)["verified_truncated"]
    assert len(flagged) == 1 and flagged[0].get("reason") is None

    monkeypatch.setattr(rl, "_read_held_audio_meta", lambda _s: entry(560.0))
    assert rl.scan_dir_for_isrc_repairs(
        album, "tok", deep=True)["verified_truncated"] == []


def test_duration_gate_ignores_unreadable_length_tag(monkeypatch, tmp_path):
    import qobuz_librarian.repair_log as rl

    album = tmp_path / "album"
    album.mkdir()
    source = album / "01.flac"
    source.write_bytes(b"held source")
    entry = {"path": str(source), "title": "t",
             "isrc": "USABC1234500", "length": 0.0,
             "sample_rate": 44100, "bits": 16, "channels": 2}
    qt = {"duration": 200.0, "title": "t", "track_number": 1, "isrc": "USABC1234500"}
    monkeypatch.setattr(rl, "_qobuz_track_by_isrc", lambda i, t: qt)
    monkeypatch.setattr(rl, "_read_held_audio_meta", lambda _s: entry)
    monkeypatch.setattr(rl, "_flac_decode_ok", lambda *a, **k: True)

    r = rl.scan_dir_for_isrc_repairs(album, "tok", deep=True)
    assert r["verified_truncated"] == []
    assert r["verified_ok"] == 1


def test_scan_diagnoses_a_file_it_cannot_lease(tmp_path, _need_tools):
    # A write lease needs file ownership (or CAP_LEASE), so on a
    # mixed-ownership library every non-owned file would be given up as
    # "unverified" and a genuinely broken one never flagged. The sealed receipt
    # already proves the file held still, so diagnosis carries on without the
    # lease.
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    good = album / "01.flac"
    bad = album / "02.flac"
    _make_flac(good)
    _make_flac(bad)
    _frame_corrupt(bad)
    assert not _decodes(bad)

    with patch("qobuz_librarian.repair_log.acquire_inode_write_exclusion",
               return_value=None), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_QT):
        r = scan_dir_for_isrc_repairs(album, "token", deep=False)

    assert r["unverified"] == 0, f"lease refusal must not skip diagnosis (got {r})"
    assert r["verified_ok"] == 1
    assert "02.flac" in _names(r["verified_truncated"])
    assert r["verified_truncated"][0]["reason"] == "decode_failed"
    assert r["verified_truncated"][0].get("source_receipt"), (
        "a lease-less verdict still needs the exact receipt the repair act "
        "verifies against")


def test_scan_downgrades_a_file_that_changes_mid_check(tmp_path, _need_tools):
    # Without a lease nothing blocks a concurrent writer, so the stat-identity
    # recheck is the whole safety story: a file that changes during its check
    # must land in "unverified", never in a repair verdict.
    from qobuz_librarian import repair_log as rl

    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "01.flac"
    _make_flac(p)

    real_flac_audio_ok = rl.flac_audio_ok

    def mutate_then_check(path, *, descriptor=None):
        with open(p, "ab") as fh:
            fh.write(b"x")
        return real_flac_audio_ok(path, descriptor=descriptor)

    with patch("qobuz_librarian.repair_log.acquire_inode_write_exclusion",
               return_value=None), \
         patch("qobuz_librarian.repair_log.flac_audio_ok",
               side_effect=mutate_then_check), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_QT):
        r = scan_dir_for_isrc_repairs(album, "token", deep=False)

    assert r["unverified"] == 1, f"a mid-check change must downgrade (got {r})"
    assert r["verified_ok"] == 0
    assert r["verified_truncated"] == []


def test_shallow_scan_files_corrupt_unmatched_isrc_under_no_match(
        tmp_path, _need_tools):
    """A corrupt unmatched ISRC keeps its diagnostic under no-match."""
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "01.flac"
    _make_flac(p)
    _frame_corrupt(p)
    assert not _decodes(p)

    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=None):
        r = scan_dir_for_isrc_repairs(album, "token", deep=False)

    assert not r["no_isrc_tag"], f"tagged file must not land there (got {r})"
    diagnosed = [e for e in r["isrc_no_match"] if e.get("diagnostic")]
    assert [Path(e["path"]).name for e in diagnosed] == ["01.flac"], (
        f"corrupt unmatched-ISRC file must carry its diagnostic (got {r})")


def _qt(title, duration, isrc):
    return {"duration": duration, "title": title, "track_number": 1,
            "isrc": isrc}


def test_short_file_is_left_alone_when_its_isrc_names_another_song(
        tmp_path, _need_tools):
    """A wrong ISRC tag resolves to a stranger, and a stranger's duration is
    not evidence about this file. Measured on a real library: a 128 s Crass
    track carried an ISRC belonging to a 407 s recording of a different song,
    so the scan called it truncated and offered to replace it with that other
    song wearing the original's tags."""
    album = tmp_path / "Crass" / "Yes Sir, I Will (1983)"
    album.mkdir(parents=True)
    p = album / "The Five Knuckle Shuffle.flac"
    _make_flac(p, isrc="GBBTF1800330")
    assert _decodes(p)

    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_qt("A Rock 'n' Roll Swindler", 407.0,
                                "GBBTF1800330")):
        r = scan_dir_for_isrc_repairs(album, "token", deep=True)

    assert r["verified_truncated"] == [], (
        f"a healthy file must not be offered for repair (got {r})")
    assert _names(r["isrc_mismatch"]) == {"The Five Knuckle Shuffle.flac"}, (
        f"it belongs under the mismatch heading instead (got {r})")
    assert r["isrc_mismatch"][0]["local_title"] == "The Five Knuckle Shuffle"


def test_a_mismatched_isrc_never_hides_a_file_that_will_not_decode(
        tmp_path, _need_tools):
    """Damage counts however badly the tag is written, but it decides only
    that something is wrong, never what to fetch.

    Detection must not consult the tag, so this file is still reported. The
    refill target must, because refilling by an ISRC that names another song
    downloads that other song, puts this file's title on it and then deletes
    this file. So the damage lands in isrc_mismatch carrying a diagnostic,
    which offers a whole-album re-download, and never in verified_truncated,
    which is the single-track swap.
    """
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "Broken.flac"
    _make_flac(p, isrc="USABC9999900")
    _frame_corrupt(p)
    assert not _decodes(p)

    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_qt("Something Else Entirely", 407.0,
                                "USABC9999900")):
        r = scan_dir_for_isrc_repairs(album, "token", deep=True)

    assert r["verified_truncated"] == [], (
        f"a wrong ISRC must never become a refill target (got {r})")
    assert _names(r["isrc_mismatch"]) == {"Broken.flac"}, (
        f"a broken file must still be caught and reported (got {r})")
    entry = r["isrc_mismatch"][0]
    assert entry["reason"] in ("decode_failed", "byte_size_short")
    assert entry["diagnostic"], (
        "without a diagnostic nothing offers this file a re-download")
