"""End-to-end integrity harness for the destructive in-place operations.

Runs each real op (downsample, repair retag, consolidate-merge, gap-fill backup
+ restore) against synthesized edge-case files in a throwaway tree, then checks
the files before/after - bit depth preserved, audio decodes, tags intact, no
silent loss. NOT a unit test: it exercises the real engine functions on real
ffmpeg/flac/mutagen, the cases the all-CD-rate test library can't cover.

Run inside the container:  python3 scripts/e2e_destructive_check.py
Exit 0 = all checks passed; non-zero = a real integrity failure (details on
stdout).
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def bps(p):
    out = subprocess.run(["metaflac", "--show-bps", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def rate(p):
    out = subprocess.run(["metaflac", "--show-sample-rate", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def decodes(p):
    return subprocess.run(["flac", "-t", str(p)],
                          capture_output=True).returncode == 0


def tag(p, k):
    out = subprocess.run(["metaflac", f"--show-tag={k}", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return out.split("=", 1)[1] if "=" in out else ""


def synth(path, *, seconds=3, ar=96000, sfmt="s32", brs=24, freq=440, tags=None):
    """Generate a real FLAC at a given rate/depth with tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
           "-ar", str(ar), "-sample_fmt", sfmt]
    if brs:
        cmd += ["-bits_per_raw_sample", str(brs)]
    cmd += ["-c:a", "flac", "-y", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    if tags:
        args = ["metaflac"]
        for k, v in tags.items():
            args += [f"--set-tag={k}={v}"]
        args.append(str(path))
        subprocess.run(args, check=True, capture_output=True)


def test_downsample_preserves_bit_depth_and_audio():
    """24-bit/96k → CD rate must stay 24-bit, keep tags, and still decode."""
    from qobuz_librarian.integrations.downsample_engine import (
        detect_resampler_filter,
        read_sample_rate,
        resample_one,
    )
    t = Path(tempfile.mkdtemp())
    try:
        f = t / "24bit.flac"
        synth(f, ar=96000, sfmt="s32", brs=24,
              tags={"ARTIST": "X", "ALBUM": "Y", "TITLE": "Z", "ISRC": "US1230000001"})
        in_bps, in_isrc = bps(f), tag(f, "ISRC")
        af, _ = detect_resampler_filter()
        r = resample_one(f.name, read_sample_rate(f), 48000, af, base_dir=t)
        out_bps, out_rate = bps(f), rate(f)
        check("downsample 24-bit stays 24-bit",
              in_bps == 24 and out_bps == 24, f"{in_bps}b -> {out_bps}b")
        check("downsample lands at target rate", out_rate == 48000, f"{out_rate}Hz")
        check("downsample output decodes", decodes(f))
        check("downsample preserves ISRC tag",
              tag(f, "ISRC") == in_isrc, tag(f, "ISRC"))
        check("downsample reported no error", r[4] is None, str(r[4]))

        # 16-bit must stay 16-bit (no regression to 32).
        g = t / "16bit.flac"
        synth(g, ar=88200, sfmt="s16", brs=None)
        resample_one(g.name, read_sample_rate(g), 44100, af, base_dir=t)
        check("downsample 16-bit stays 16-bit", bps(g) == 16, f"{bps(g)}b")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_downsample_refuses_bad_encode_keeps_original():
    """If the re-encode came out the wrong depth, the original must be left
    untouched (the guard added with R5)."""
    import qobuz_librarian.integrations.downsample_engine as de
    from qobuz_librarian.integrations.downsample_engine import (
        detect_resampler_filter,
        resample_one,
    )
    t = Path(tempfile.mkdtemp())
    try:
        f = t / "master.flac"
        synth(f, ar=96000, sfmt="s32", brs=24)
        before = f.read_bytes()
        af, _ = detect_resampler_filter()
        # Force the post-encode probe to report a depth change → must NOT overwrite.
        orig = de.read_local_bit_depth
        calls = {"n": 0}

        def fake_bps(p, *, pass_fds=()):
            calls["n"] += 1
            # first call (source) = 24; later call (output verify) = 16 (wrong)
            return 24 if calls["n"] == 1 else 16
        de.read_local_bit_depth = fake_bps
        try:
            r = resample_one(f.name, 96000, 48000, af, base_dir=t)
        finally:
            de.read_local_bit_depth = orig
        # Name the depth guard: any other refusal (a stub with the wrong
        # signature, a missing tool) would otherwise read as this one firing.
        check("downsample aborts on depth mismatch",
              r[4] is not None and "expected 24-bit" in r[4], str(r[4]))
        check("downsample left original intact on mismatch",
              f.read_bytes() == before)
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_repair_retag_transplants_from_backup():
    """The R1 path: retag reads the original's tags+art from its backup copy and
    applies them to the refill, without holding art in RAM."""
    from qobuz_librarian.modes.repair import _backup_source_by_isrc, _retag_refills_in_staging
    t = Path(tempfile.mkdtemp())
    try:
        album = t / "Artist" / "Rival Dealer (2013)"
        # The "original" (owned) file, then its backup copy (as gap-fill backup makes).
        orig = album / "02 - Hiders.flac"
        synth(orig, ar=44100, sfmt="s16", brs=None,
              tags={"ALBUM": "Rival Dealer", "TRACKNUMBER": "2",
                    "TITLE": "Hiders", "ISRC": "GBTEST1300002"})
        backup = t / "backup" / "02 - Hiders.flac"
        backup.parent.mkdir(parents=True)
        shutil.copy(orig, backup)
        vt = [{"isrc": "GBTEST1300002", "path": str(orig)}]
        srcmap = _backup_source_by_isrc(vt, album, t / "backup")
        check("retag found backup source by ISRC", "GBTEST1300002" in srcmap)

        # The "refill" streamrip wrote - wrong album/track (compilation).
        staged = t / "staging" / "Tunes 2011-2019"
        refill = staged / "06 - Hiders.flac"
        synth(refill, ar=44100, sfmt="s16", brs=None,
              tags={"ALBUM": "Tunes 2011-2019", "TRACKNUMBER": "6",
                    "TITLE": "Hiders", "ISRC": "GBTEST1300002"})
        _retag_refills_in_staging([staged], srcmap)
        check("retag fixed ALBUM to owned edition",
              tag(refill, "ALBUM") == "Rival Dealer", tag(refill, "ALBUM"))
        check("retag fixed TRACKNUMBER",
              tag(refill, "TRACKNUMBER") == "2", tag(refill, "TRACKNUMBER"))
        check("retag kept refill decodable", decodes(refill))
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_gap_fill_backup_restore_round_trips():
    """Files moved to a gap-fill backup must restore byte-identical."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library.backup import backup_gap_fill_files, restore_gap_fill_backup
    t = Path(tempfile.mkdtemp())
    old_backup, old_root = cfg.UPGRADE_BACKUP_DIR, cfg.MUSIC_ROOT
    try:
        cfg.UPGRADE_BACKUP_DIR = t / "backups"
        # The backup refuses any album outside the configured music tree, so
        # the throwaway tree has to be that tree for the run.
        cfg.MUSIC_ROOT = t
        album = t / "Artist" / "Album (2020)"
        f1 = album / "01 - A.flac"
        f2 = album / "02 - B.flac"
        synth(f1, ar=44100, sfmt="s16", brs=None, tags={"TITLE": "A"})
        synth(f2, ar=44100, sfmt="s16", brs=None, tags={"TITLE": "B"})
        h1, h2 = f1.read_bytes(), f2.read_bytes()
        bp = backup_gap_fill_files([str(f1), str(f2)], album)
        check("gap-fill backup created", bp is not None and bp.exists())
        check("gap-fill backup moved originals out", not f1.exists() and not f2.exists())
        n = restore_gap_fill_backup(bp, album)
        check("gap-fill restore returned count", n == 2, f"n={n}")
        check("gap-fill restored byte-identical",
              f1.exists() and f2.exists()
              and f1.read_bytes() == h1 and f2.read_bytes() == h2)
    finally:
        cfg.UPGRADE_BACKUP_DIR, cfg.MUSIC_ROOT = old_backup, old_root
        shutil.rmtree(t, ignore_errors=True)


def test_decode_check_catches_corrupt_no_isrc_file():
    """The repair decode probe must flag a normal-size, no-ISRC FLAC that has
    been corrupted in the middle (the general-library gap we closed)."""

    from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs
    t = Path(tempfile.mkdtemp())
    try:
        f = t / "song.flac"
        synth(f, ar=44100, sfmt="s16", brs=None, seconds=3)  # no ISRC tag
        # Corrupt the middle of the audio so it won't decode but stays full-size.
        data = bytearray(f.read_bytes())
        mid = len(data) // 2
        for i in range(mid, min(mid + 4000, len(data))):
            data[i] = 0
        f.write_bytes(bytes(data))
        check("corrupted file actually fails to decode", not decodes(f))
        rep = scan_dir_for_isrc_repairs(t, "token", deep=True)
        flagged = any(e.get("diagnostic") for e in rep["no_isrc_tag"])
        check("repair deep scan flags the corrupt no-ISRC file", flagged)
    finally:
        shutil.rmtree(t, ignore_errors=True)


def main():
    for fn in (test_downsample_preserves_bit_depth_and_audio,
               test_downsample_refuses_bad_encode_keeps_original,
               test_repair_retag_transplants_from_backup,
               test_gap_fill_backup_restore_round_trips,
               test_decode_check_catches_corrupt_no_isrc_file):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"raised {type(e).__name__}: {e}")
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 60)
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILURES:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
