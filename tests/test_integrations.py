"""Tests for rip, beets, lyrics, and the seams between them."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.integrations.lyrics import (
    load_lyric_retry,
    save_lyric_retry,
)
from qobuz_librarian.integrations.rip import (
    _FLAC_TRUNCATION_FLOOR,
    cleanup_lossy,
    cleanup_staging_residue,
    is_flac,
)

# ── shared SQLite transaction boundary ────────────────────────────────


# ── rip: FLAC validation + lossy cleanup ──────────────────────────────────


def test_is_flac_rejects_truncated_keeps_complete(tmp_path, _need_ffmpeg, _need_flac):
    def _sine(path, seconds):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=44100:duration={seconds}",
                "-c:a",
                "flac",
                str(path),
            ],
            check=True,
        )

    # A short but complete track is real audio. Keep it even though it sits
    # well under the size heuristic the no-flac fallback uses.
    short = tmp_path / "interlude.flac"
    _sine(short, 1.2)
    assert short.stat().st_size < _FLAC_TRUNCATION_FLOOR
    assert is_flac(short) is True

    # An interrupted download leaves a file whose header still advertises the
    # full duration, so only decoding the (missing) frames exposes the gap.
    full = tmp_path / "full.flac"
    _sine(full, 3)
    data = full.read_bytes()
    partial = tmp_path / "partial.flac"
    partial.write_bytes(data[: len(data) * 2 // 5])
    assert is_flac(partial) is False

    assert is_flac(tmp_path / "never-written.flac") is False


def test_flac_audio_ok_treats_a_verify_timeout_as_broken(monkeypatch):
    # A `flac -t` that hangs past the timeout (a pathological/corrupt large
    # FLAC) must read as broken (False), not as "tool absent" (None): None
    # routes a large file through the size heuristic, which trusts it.
    import qobuz_librarian.integrations.rip as rip

    monkeypatch.setattr(rip.shutil, "which", lambda name: "/usr/bin/flac")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="flac", timeout=300)

    monkeypatch.setattr(rip.subprocess, "run", hang)

    assert rip.flac_audio_ok("/any/large.flac") is False


def test_cleanup_lossy_sorts_flac_lossy_and_broken(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path)
    good = tmp_path / "good.flac"
    good.write_bytes(b"\x00" * 200_000)
    bad = tmp_path / "truncated.flac"
    bad.write_bytes(b"\x00" * 200_000)
    mp3 = tmp_path / "track.mp3"
    mp3.write_bytes(b"\x00" * 1000)
    # is_flac stubbed: only `good` verifies; the other FLAC is treated as broken.
    with patch("qobuz_librarian.integrations.rip.is_flac", side_effect=lambda p: p == good):
        kept, lossy, broken = cleanup_lossy([good, bad, mp3])
    assert kept == [good]
    assert lossy == [mp3] and broken == [bad]
    assert not bad.exists() and not mp3.exists()


# ── rip: staging residue cleanup ─────────────────────────────────────────


def test_cleanup_staging_residue_keeps_art_beside_leftover_audio(tmp_path, monkeypatch):
    # An interrupted run can leave a fully-downloaded album in staging; its
    # cover.jpg is the filesystem fetchart source on import (ARTWORK=sidecar).
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").write_bytes(b"audio data" * 1000)
    (album / "cover.jpg").write_bytes(b"img")
    (album / "meta.json").write_text("{}")
    # Multi-disc: art at album root, audio one level down.
    boxset = tmp_path / "Artist" / "BoxSet"
    (boxset / "Disc 1").mkdir(parents=True)
    (boxset / "Disc 1" / "01.flac").write_bytes(b"audio" * 1000)
    (boxset / "cover.jpg").write_bytes(b"img")
    # A legacy orphan has no run receipt, so its current occupant is preserved.
    orphan = tmp_path / "Old"
    orphan.mkdir()
    (orphan / "cover.jpg").write_bytes(b"img")

    cleanup_staging_residue()
    assert (album / "cover.jpg").exists() and (album / "meta.json").exists()
    assert (boxset / "cover.jpg").exists()
    assert (orphan / "cover.jpg").exists()


# ── exact empty-directory cleanup ────────────────────────────────────────


# ── lyrics: retry manifest + atomic writes ────────────────────────────────


class _FakeLyricFLAC:
    def __init__(self, path):
        from mutagen.flac import VCFLACDict

        self.filename = str(path)
        self.tags = VCFLACDict()
        self.save_targets = []

    def save(self, target):
        self.save_targets.append(target)
        Path(target).write_bytes(b"new-audio+tags")


def test_lyric_retry_round_trips_and_clears(tmp_path, monkeypatch):
    rfile = tmp_path / "retry.json"
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
    save_lyric_retry(["/music/a.flac", "/music/b.flac"])
    assert load_lyric_retry() == ["/music/a.flac", "/music/b.flac"]
    # Saving an empty list removes the file rather than leaving an empty manifest.
    save_lyric_retry([])
    assert not rfile.exists()


def test_cli_lyric_retry_does_not_report_write_errors_as_resolved(
        tmp_path, monkeypatch, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyrics

    tracks = [tmp_path / f"{index}.flac" for index in range(2)]
    for track in tracks:
        track.write_bytes(b"synthetic")
    monkeypatch.setattr(cfg, "LYRIC_RETRY_FILE", tmp_path / "retry.json")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(lyrics.lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(
        lyrics.lyric_fetch,
        "fetch_for_paths",
        lambda *_args, **_kwargs: {"write-error": 2},
    )
    monkeypatch.setattr(
        lyrics,
        "_refresh_lyric_retry",
        lambda _paths: lyrics.save_lyric_retry([]),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    lyrics.save_lyric_retry([str(track) for track in tracks])

    lyrics.offer_resume_lyric_retry(object())

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "2 failed" in messages
    assert "All retried files resolved" not in messages


def test_lyric_retry_summary_does_not_invent_missing_successes():
    from qobuz_librarian.integrations.lyrics import summarize_lyric_retry

    assert summarize_lyric_retry(
        {"wrote-synced": 1}, attempted=3, remaining=0
    ) == {"resolved": 1, "failed": 2, "remaining": 0}
    assert summarize_lyric_retry(
        {"wrote-synced": 1, "write-error": 1, "providers-unavailable": 1},
        attempted=3,
        remaining=1,
    ) == {"resolved": 1, "failed": 1, "remaining": 1}


def test_import_lyric_hook_reports_an_all_error_run(
        tmp_path, monkeypatch, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyrics

    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "track.flac").write_bytes(b"synthetic")
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path)
    monkeypatch.setattr(cfg, "LYRIC_FETCH_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(lyrics.lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(
        lyrics.lyric_fetch,
        "fetch_for_paths",
        lambda *_args, **_kwargs: {"write-error": 1},
    )

    counts, _signatures = lyrics._run_lyric_hook(album)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert counts == {"write-error": 1}
    assert "1 failed" in messages


def test_post_import_sidecar_write_failure_gets_a_terminal_warning(
        tmp_path, monkeypatch, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyrics

    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "track.flac"
    track.write_bytes(b"synthetic")

    class FakeFLAC:
        tags = {"lyrics": ["[00:01.00]line"]}

    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "LYRICS_FORMAT", "sidecar")
    monkeypatch.setattr(lyrics, "HAVE_LYRIC_FETCH", True)
    monkeypatch.setattr(lyrics.lyric_fetch, "FLAC", lambda _path: FakeFLAC())
    monkeypatch.setattr(
        lyrics.lyric_fetch,
        "write_sidecar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic failure")),
    )

    lyrics.write_post_import_sidecars([album])

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "1 sidecar failed" in messages


def test_write_lyrics_saves_atomically_and_clears_legacy_tag(tmp_path):
    from qobuz_librarian.integrations import lyric_fetch

    real = tmp_path / "track.flac"
    real.write_bytes(b"original-audio")

    f = _FakeLyricFLAC(real)
    f.tags["UNSYNCEDLYRICS"] = ["stale plain text"]
    lyric_fetch.write_lyrics(f, "[00:01.00]hello")

    assert f.tags["lyrics"] == ["[00:01.00]hello"]
    assert "unsyncedlyrics" not in f.tags
    # The live file must never be written in place. Mutagen saves into a temp
    # copy that is then atomically swapped in, so a crash can't truncate it.
    assert f.save_targets and all(t != f.filename for t in f.save_targets)
    assert real.read_bytes() == b"new-audio+tags"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_lyric_fetch_refuses_paths_outside_or_linked_out_of_its_owned_root(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyric_fetch

    music_root = tmp_path / "music"
    outside = tmp_path / "outside"
    (outside / "Artist" / "Album").mkdir(parents=True)
    music_root.mkdir()
    (music_root / "Artist").symlink_to(outside / "Artist", target_is_directory=True)
    outside_track = outside / "outside.flac"
    linked_track = music_root / "Artist" / "Album" / "linked.flac"
    outside_track.write_bytes(b"outside audio")
    (outside / "Artist" / "Album" / "linked.flac").write_bytes(b"linked outside audio")
    state_path = tmp_path / "lyrics-state.json"
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)

    counts = lyric_fetch.fetch_for_paths(
        [outside_track, linked_track],
        owned_root=music_root,
        state_path=state_path,
        rescan=True,
        workers=1,
        lyrics_format="both",
    )
    indexed = lyric_fetch.index_existing(
        [outside_track, linked_track],
        owned_root=music_root,
        state_path=tmp_path / "lyrics-index-state.json",
        workers=1,
    )

    assert counts == {"unsafe-path": 2}
    assert indexed == {"unsafe-path": 2}
    assert outside_track.read_bytes() == b"outside audio"
    assert (outside / "Artist" / "Album" / "linked.flac").read_bytes() == (b"linked outside audio")
    assert not outside_track.with_suffix(".lrc").exists()
    assert not linked_track.with_suffix(".lrc").exists()


# ── beets: _beets_direct behaviour ─────────────────────────────────────────


def test_beets_runtime_check_rejects_an_unrelated_executable(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    unrelated = shutil.which("true")
    assert unrelated is not None
    monkeypatch.setattr(cfg, "BEETS_PYTHON", unrelated)

    assert beets.beets_runtime_path() is None


def test_beets_direct_preflights_a_new_database_before_import(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    config_dir = tmp_path / "beets"
    config_dir.mkdir()
    database = config_dir / "library.db"
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)

    inspected = []
    imported = []

    def reject_filesystem(anchor, *_args, **_kwargs):
        inspected.append(anchor["descriptor"])
        raise OSError("unsupported database filesystem")

    monkeypatch.setattr(beets, "inspect_sqlite_source", reject_filesystem)
    monkeypatch.setattr(
        beets,
        "_beets_direct_guarded",
        lambda *_args, **_kwargs: imported.append(True) or (True, "ok"),
    )

    real_connect = beets.sqlite3.connect

    def fail_bootstrap(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("file:/proc/self/fd/"):
            raise beets.sqlite3.DatabaseError("incomplete bootstrap")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(beets.sqlite3, "connect", fail_bootstrap)
    assert beets._beets_direct(None, lambda: None, [str(tmp_path)]) == (False, "error")
    assert not database.exists()
    assert not list(config_dir.glob(".qobuz-beets-bootstrap-*"))
    assert imported == []

    monkeypatch.setattr(beets.sqlite3, "connect", real_connect)
    assert beets._beets_direct(None, lambda: None, [str(tmp_path)]) == (False, "error")
    assert inspected and inspected[0] is not None
    assert imported == []
    assert database.read_bytes().startswith(b"SQLite format 3\0")


def test_beets_direct_detects_silent_skip_by_unmoved_audio(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    captured_env = {}
    captured_args = []
    messages = []

    class _Proc:
        def __init__(self, lines=(), on_wait=None):
            self.stdout = iter(lines)
            self.returncode = 0
            self._on_wait = on_wait

        def wait(self, timeout=None):
            if self._on_wait:
                self._on_wait()
            return 0

        def kill(self):
            pass

    def _popen_returning(proc):
        def _popen(*args, **kwargs):
            captured_args.append(args[0])
            captured_env.update(kwargs.get("env") or {})
            return proc

        return _popen

    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(beets.log, "info", messages.append)
    album = tmp_path / "Artist - Album"
    album.mkdir()
    track = album / "01.flac"
    track.write_bytes(b"flac-bytes")

    # beets moves the staged track into the library (here, deletes it) and
    # prints a per-item "Skipping." for a duplicate.
    monkeypatch.setattr(subprocess, "Popen", _popen_returning(_Proc(["Skipping.\n"], track.unlink)))
    runtime = beets._checked_beets_runtime(sys.executable)
    assert runtime is not None
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=runtime,
    )
    assert ok is True and kind == "ok"
    assert captured_args[-1][:4] == [
        sys.executable,
        "-I",
        str(beets._managed_beets_entrypoint()),
        "--run-beets",
    ]
    assert captured_env.get("BEETSDIR") == str(cfg.BEETS_CONFIG_DIR)

    # A partial exit-0 import reports only what is known about the remnant.
    track.write_bytes(b"flac-bytes")
    leftover = album / "02.flac"
    leftover.write_bytes(b"leftover")
    messages.clear()
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _popen_returning(_Proc(on_wait=track.unlink)),
    )
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=runtime,
    )
    assert ok is True and kind == "ok"
    assert any(
        "1 staged track(s) were not imported and remain in staging" in message
        for message in messages
    )
    assert not any("likely duplicates or unreadable" in message for message in messages)
    leftover.unlink()

    # beets exits 0 but moves nothing out of staging: the real silent skip.
    track.write_bytes(b"flac-bytes")
    monkeypatch.setattr(subprocess, "Popen", _popen_returning(_Proc()))
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=runtime,
    )
    assert ok is False and kind == "error"

    # A runtime replaced after preflight must not fall back to an unchecked
    # PATH launcher.
    invalid_runtime = beets._BeetsRuntime(
        runtime.python,
        (*runtime.link_identity[:-1], runtime.link_identity[-1] + 1),
        runtime.target_identity,
    )
    spawned_before = len(captured_args)
    ok, kind = beets._beets_direct(
        None,
        lambda: None,
        [str(album)],
        beets_runtime=invalid_runtime,
    )
    assert ok is False and kind == "error"
    assert len(captured_args) == spawned_before


def test_beets_pruning_stays_bound_to_the_captured_staging_roots(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    staging = tmp_path / "staging"
    stable_run = staging / ".qobuz-run-111111111111111111111111"
    swapped_run = staging / ".qobuz-run-222222222222222222222222"
    stable_album = stable_run / "Artist" / "Album"
    swapped_album = swapped_run / "Artist" / "Album"
    for album in (stable_album, swapped_album):
        disc = album / "Disc 2"
        disc.mkdir(parents=True)
        (disc / "01.flac").write_bytes(b"track")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    capture = beets._capture_beets_prune_roots(
        [str(stable_album), str(swapped_album)]
    )
    assert capture is not None
    (stable_album / "Disc 2" / "01.flac").unlink()
    (swapped_album / "Disc 2" / "01.flac").unlink()
    parked = staging / "parked-original"
    swapped_run.rename(parked)
    outside = tmp_path / "outside"
    (outside / "Artist" / "Album" / "Disc 2").mkdir(parents=True)
    swapped_run.symlink_to(outside, target_is_directory=True)

    try:
        beets._prune_captured_beets_directories(capture)
    finally:
        beets._close_beets_prune_capture(capture)

    assert stable_run.is_dir()
    assert list(stable_run.iterdir()) == []
    assert swapped_run.is_symlink()
    assert (outside / "Artist" / "Album" / "Disc 2").is_dir()
    assert (parked / "Artist" / "Album" / "Disc 2").is_dir()


# ── beets: staging tag prep (quarantine, never delete) ────────────────────


def test_prepare_staging_tags_sets_aside_untagged_keeps_tagged(tmp_path, monkeypatch, _need_ffmpeg):
    # A cancelled/crashed rip leaves untagged FLACs beets would file under
    # '/_/'. They're moved out of the import set, but set aside and never deleted.
    from mutagen.flac import FLAC

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.integrations.staging import capture_file

    staging = tmp_path / "staging"
    data = tmp_path / "data"
    staging.mkdir()
    data.mkdir()
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", data)
    messages = []
    monkeypatch.setattr(beets.log, "info", messages.append)

    tagged = staging / "Real Artist" / "Real Album" / "01 - Good.flac"
    untagged = staging / "Partial" / "00 -.flac"
    _make_silent_flac(tagged)
    _make_silent_flac(untagged)
    f = FLAC(str(tagged))
    f["albumartist"], f["album"], f["title"] = ["Real Artist"], ["Real Album"], ["Good"]
    f.save()
    broken = staging / "Broken" / "x.flac"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a flac at all")

    moved = beets._prepare_staging_tags()
    assert tagged.exists()
    assert not untagged.exists() and untagged in moved
    assert not broken.exists() and broken in moved
    assert len(list((staging / cfg.BEETS_RETRY_DIR).rglob("*.flac"))) == 2
    summary = next(message for message in messages if "Set aside 2 untagged" in message)
    assert "private staging recovery" in summary
    assert "each file has its own recovery record" in summary
    assert str(staging / cfg.BEETS_RETRY_DIR) not in summary

    clean = capture_file(tagged)
    assert clean is not None
    binding = [
        {
            "slot": "qobuz:1",
            "path": str(clean.path),
            "identity": list(clean.identity),
        }
    ]
    intent = beets._prepare_staging_tags(roots=[tagged.parent], managed_bindings=binding)
    assert intent[0]["identity"] == list(clean.identity)

    f = FLAC(str(tagged))
    f["album"] = ["  Real Album  "]
    f.save()
    dirty = capture_file(tagged)
    assert dirty is not None
    binding[0]["identity"] = list(dirty.identity)
    with pytest.raises(OSError, match="requires a tag-clean rewrite"):
        beets._prepare_staging_tags(roots=[tagged.parent], managed_bindings=binding)
    assert capture_file(tagged, expected=dirty.identity) is not None
    assert FLAC(str(tagged))["album"] == ["  Real Album  "]

    rewritten = beets.prepare_managed_staging_tags(
        [tagged.parent],
        binding,
        authority_check=lambda: None,
    )
    assert rewritten[0]["identity"] != list(dirty.identity)
    assert FLAC(str(tagged))["album"] == ["Real Album"]

    f = FLAC(str(tagged))
    f["album"] = ["  Real Album  "]
    f.save()
    dirty = capture_file(tagged)
    assert dirty is not None
    binding[0]["identity"] = list(dirty.identity)
    authority_live = [True]
    commit_checks = []

    def authority_check():
        if not authority_live[0]:
            raise RuntimeError("lease lost")

    def stop_at_commit(_tags, _path, *, commit_guard, **_kwargs):
        authority_live[0] = False
        try:
            allowed = commit_guard()
        except RuntimeError:
            allowed = False
        commit_checks.append(allowed)
        raise OSError("commit refused")

    from qobuz_librarian.integrations import lyric_fetch

    monkeypatch.setattr(lyric_fetch, "save_flac_tags", stop_at_commit)
    with pytest.raises(OSError, match="tag-clean rewrite failed"):
        beets.prepare_managed_staging_tags(
            [tagged.parent],
            binding,
            authority_check=authority_check,
        )
    assert commit_checks == [False]
    assert capture_file(tagged, expected=dirty.identity) is not None


def test_prepare_managed_staging_tags_returns_bindings_in_input_order(
    tmp_path, monkeypatch, _need_ffmpeg
):
    # The scan walks the tree in directory order, but the durable runner
    # compares the result against the catalogue-ordered bindings. The records
    # must come back in the order they were passed, not readdir order.
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.integrations.staging import capture_file

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)

    album = staging / "Artist" / "Album"
    binding = []
    for n in (1, 2, 3):
        path = album / f"0{n} - Track {n}.flac"
        _make_silent_flac(path)
        from mutagen.flac import FLAC
        f = FLAC(str(path))
        f["albumartist"], f["album"], f["title"] = ["Artist"], ["Album"], [f"Track {n}"]
        f.save()
        clean = capture_file(path)
        binding.append({
            "slot": f"qobuz:{n}",
            "path": str(clean.path),
            "identity": list(clean.identity),
        })

    # Hand them in reverse of on-disk name order; the walk won't match this.
    binding.reverse()
    rewritten = beets.prepare_managed_staging_tags(
        [album], binding, authority_check=lambda: None
    )
    assert [r["slot"] for r in rewritten] == [b["slot"] for b in binding]


# ── beets: import override pins non-destructive duplicate handling ─────────


def test_import_override_pins_duplicate_action_merge(monkeypatch):
    # OUR importer must pin duplicate_action: merge regardless of the user's
    # config.
    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("/config/beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", ["lastgenre"])
    monkeypatch.setattr(cfg, "ARTWORK", "sidecar")
    conf = yaml.safe_load(beets._build_import_override_yaml())
    assert conf["import"]["duplicate_action"] == "merge"
    assert conf["plugins"].count("inline") == 1
    assert conf["plugins"][-1] == "qobuz_art_guard"
    assert conf["pluginpath"][0] == str(Path(beets.__file__).parent / "beets_plugins")
    # Streamrip already wrote authoritative Qobuz tags, so autotag must be pinned
    # off. Otherwise a user's autotag:yes pushes downloads through MusicBrainz
    # matching and strands unmatched albums in staging under quiet mode.
    assert conf["import"]["autotag"] is False


def _duplicate_album_fixture(tmp_path, *, conflicting_attribute=False):
    import sqlite3

    music = tmp_path / "music" / "Artist" / "Album"
    music.mkdir(parents=True)
    first = music / "01.flac"
    second = music / "02.flac"
    cover = music / "cover.jpg"
    first.write_bytes(b"first audio")
    second.write_bytes(b"second audio")
    cover.write_bytes(b"artwork")
    database = tmp_path / "library.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE albums (
                added REAL, album TEXT, albumartist TEXT, artpath BLOB,
                custom_field TEXT, id INTEGER PRIMARY KEY
            );
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, album_id INTEGER, path BLOB,
                title TEXT, mtime REAL
            );
            CREATE TABLE album_attributes (
                id INTEGER PRIMARY KEY, entity_id INTEGER,
                key TEXT, value TEXT
            );
            CREATE TABLE item_attributes (
                id INTEGER PRIMARY KEY, entity_id INTEGER,
                key TEXT, value TEXT
            );
        """)
        artpath = os.fsencode(cover)
        connection.executemany(
            "INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?)",
            [
                (10.0, "Album", "Artist", artpath, "opaque", 1),
                (20.0, "Album", "Artist", None, "opaque", 2),
            ],
        )
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?)",
            [
                (11, 1, os.fsencode(first), "First", 101.25),
                (12, 2, os.fsencode(second), "Second", 202.5),
            ],
        )
        connection.executemany(
            "INSERT INTO album_attributes VALUES (?, ?, ?, ?)",
            [
                (21, 1, "qobuz_id", "123"),
                (22, 1, "source", "qobuz"),
                (23, 2, "qobuz_id", "123"),
                (
                    24,
                    2,
                    "loser_only" if conflicting_attribute else "source",
                    "must survive" if conflicting_attribute else "qobuz",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO item_attributes VALUES (?, ?, ?, ?)",
            [(31, 11, "token", "one"), (32, 12, "token", "two")],
        )
        connection.commit()
    finally:
        connection.close()
    return database, (first, second, cover)


def _duplicate_album_db_snapshot(database):
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        return tuple(
            (table, connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall())
            for table in ("albums", "items", "album_attributes", "item_attributes")
        )
    finally:
        connection.close()


def _configure_consolidation(monkeypatch, tmp_path, database):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "BEETS_TIMEOUT", 30)
    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)


def test_duplicate_album_fold_preserves_files_and_all_nonstructural_data(tmp_path, monkeypatch):
    import hashlib
    import sqlite3

    from qobuz_librarian.integrations import beets

    database, files = _duplicate_album_fixture(tmp_path)
    _configure_consolidation(monkeypatch, tmp_path, database)
    relative_item = os.path.join("Artist", "Album", "01.flac")
    assert beets._consolidation_item_dir(relative_item) == str(files[0].parent)
    assert (
        beets._consolidation_path_is_protected(
            beets._consolidation_item_dir(relative_item),
            {files[0].parent},
        )
        is True
    )
    before_files = [
        (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).digest(),
        )
        for path in files
    ]
    before = _duplicate_album_db_snapshot(database)

    beets._consolidate_duplicate_albums()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT id, album_id, path, title, mtime FROM items ORDER BY id"
        ).fetchall() == [
            (11, 1, os.fsencode(files[0]), "First", 101.25),
            (12, 1, os.fsencode(files[1]), "Second", 202.5),
        ]
        assert connection.execute("SELECT * FROM albums ORDER BY id").fetchall() == [
            before[0][1][0]
        ]
        assert (
            connection.execute("SELECT * FROM album_attributes ORDER BY id").fetchall()
            == before[2][1][:2]
        )
        assert (
            connection.execute("SELECT * FROM item_attributes ORDER BY id").fetchall()
            == before[3][1]
        )
    finally:
        connection.close()
    assert before_files == [
        (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).digest(),
        )
        for path in files
    ]


def test_duplicate_album_fold_rolls_back_an_interrupted_transaction(tmp_path, monkeypatch):
    from qobuz_librarian.integrations import beets

    database, _ = _duplicate_album_fixture(tmp_path)
    _configure_consolidation(monkeypatch, tmp_path, database)
    before = _duplicate_album_db_snapshot(database)
    fold = beets._fold_duplicate_album_group

    def interrupt_after_mutation(*args, **kwargs):
        assert fold(*args, **kwargs) is True
        raise KeyboardInterrupt

    monkeypatch.setattr(beets, "_fold_duplicate_album_group", interrupt_after_mutation)

    with pytest.raises(KeyboardInterrupt):
        beets._consolidate_duplicate_albums()

    assert _duplicate_album_db_snapshot(database) == before


def _load_art_guard_for_test(monkeypatch, loaded_plugins):
    import importlib.util
    import sys
    import types

    class FakeLog:
        def warning(self, *_args, **_kwargs):
            pass

    class FakeBeetsPlugin:
        def __init__(self):
            self.name = "qobuz_art_guard"
            self._log = FakeLog()

        def register_listener(self, *_args, **_kwargs):
            pass

    plugins_module = types.ModuleType("beets.plugins")
    plugins_module.BeetsPlugin = FakeBeetsPlugin
    plugins_module.find_plugins = lambda: loaded_plugins
    plugins_module.send = lambda *_args, **_kwargs: []
    beets_module = types.ModuleType("beets")
    beets_module.plugins = plugins_module
    monkeypatch.setitem(sys.modules, "beets", beets_module)
    monkeypatch.setitem(sys.modules, "beets.plugins", plugins_module)

    from qobuz_librarian.integrations import beets

    plugin_path = Path(beets.__file__).parent / "beets_plugins" / "qobuz_art_guard.py"
    spec = importlib.util.spec_from_file_location("_qobuz_art_guard_test", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ownership_for_test(monkeypatch):
    import importlib.util

    _load_art_guard_for_test(monkeypatch, [])
    from qobuz_librarian.integrations import beets

    plugin_path = Path(beets.__file__).parent / "beets_plugins" / "qobuz_ownership.py"
    spec = importlib.util.spec_from_file_location("_qobuz_ownership_test", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_close_failure_preserves_reused_descriptor(tmp_path, operation, *, escapes=True):
    root = tmp_path / "walk-root"
    (root / "A").mkdir(parents=True)
    canary_path = root / "canary"
    canary_path.write_bytes(b"canary")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    real_close = os.close
    real_open = os.open
    state = {"fired": False, "canary_fd": None}

    def faulting_close(descriptor):
        if not state["fired"]:
            state["fired"] = True
            real_close(descriptor)
            state["canary_fd"] = real_open(canary_path, os.O_RDONLY)
            assert state["canary_fd"] == descriptor
            raise OSError("synthetic close failure after release")
        real_close(descriptor)

    os.close = faulting_close
    try:
        if escapes:
            with pytest.raises(OSError, match="synthetic close failure"):
                operation(root, root_fd)
        else:
            assert operation(root, root_fd) is False
    finally:
        os.close = real_close

    canary_fd = state["canary_fd"]
    assert canary_fd is not None
    try:
        assert os.fstat(canary_fd).st_size == len(b"canary")
    finally:
        for descriptor in (canary_fd, root_fd):
            try:
                real_close(descriptor)
            except OSError:
                pass


def test_beets_parent_walk_never_retries_a_released_descriptor(tmp_path):
    from qobuz_librarian.integrations import beets

    _assert_close_failure_preserves_reused_descriptor(
        tmp_path,
        lambda _root, root_fd: beets._open_ownership_parent(
            root_fd, (b"A", b"leaf")
        ),
    )


def test_art_guard_parent_walk_never_retries_a_released_descriptor(
        tmp_path, monkeypatch):
    module = _load_art_guard_for_test(monkeypatch, [])
    _assert_close_failure_preserves_reused_descriptor(
        tmp_path,
        lambda _root, root_fd: module._open_relative_directory(root_fd, (b"A",)),
    )


def test_art_guard_candidate_walk_never_retries_a_released_descriptor(
        tmp_path, monkeypatch):
    module = _load_art_guard_for_test(monkeypatch, [])

    def operation(root, _root_fd):
        monkeypatch.setattr(
            module,
            "_candidate_within_task",
            lambda _task, _path: (os.fsencode(root), (b"A", b"leaf")),
        )
        candidate = type("Candidate", (), {"path": b"candidate"})()
        return module._remove_candidate_source(object(), candidate, -1)

    _assert_close_failure_preserves_reused_descriptor(
        tmp_path, operation, escapes=False
    )


def test_ownership_created_walk_never_retries_a_released_descriptor(
        tmp_path, monkeypatch):
    module = _load_ownership_for_test(monkeypatch)

    def operation(_root, root_fd):
        plugin = object.__new__(module.QobuzOwnershipPlugin)
        plugin._root_fd = root_fd
        return plugin._open_created_locked((b"A",))

    _assert_close_failure_preserves_reused_descriptor(tmp_path, operation)


def _art_guard_task(root, staging, album_name):
    import types

    destination_dir = root / "Artist" / album_name
    candidate = staging / f"{album_name}.jpg"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"new artwork")

    class Album:
        albumartist = "Artist"
        album = album_name
        artpath = None
        stored = 0

        def art_destination(self, _candidate, *, item_dir):
            return os.path.join(item_dir, b"cover.jpg")

        def store(self):
            self.stored += 1

    class Item:
        def __init__(self):
            self.id = 1
            self.path = os.fsencode(candidate)

        @staticmethod
        def destination():
            return os.fsencode(destination_dir / "01.flac")

    class Task:
        toppath = os.fsencode(staging)

        def __init__(self):
            self.album = Album()
            self.pruned = []
            self.item = Item()

        def imported_items(self):
            return [self.item]

        def prune(self, path):
            self.pruned.append(path)

    task = Task()
    selected = types.SimpleNamespace(path=os.fsencode(candidate), source_name="filesystem")

    class FetchArt:
        name = "fetchart"
        store_source = False

        def __init__(self):
            self.art_candidates = {task: selected}

        @staticmethod
        def _is_source_file_removal_enabled():
            return False

        @staticmethod
        def _is_candidate_fallback(_candidate):
            return False

    return task, selected, FetchArt(), destination_dir


def test_art_guard_publishes_only_in_a_new_held_album_directory(tmp_path, monkeypatch):
    import gc
    import types

    root = tmp_path / "music"
    staging = tmp_path / "staging"
    root.mkdir()
    loaded = []
    module = _load_art_guard_for_test(monkeypatch, loaded)
    session = types.SimpleNamespace(lib=types.SimpleNamespace(directory=os.fsencode(root)))
    gc.collect()
    descriptor_count = len(os.listdir("/proc/self/fd"))

    existing_task, _, existing_fetchart, existing_dir = _art_guard_task(root, staging, "Existing")
    existing_dir.mkdir(parents=True)
    existing_cover = existing_dir / "cover.jpg"
    existing_cover.write_bytes(b"user artwork")
    loaded[:] = [existing_fetchart]
    plugin = module.QobuzArtGuardPlugin()
    plugin._guard_art(session, existing_task)
    plugin._publish_art(session, existing_task)

    assert existing_cover.read_bytes() == b"user artwork"
    assert existing_task.album.artpath is None
    assert existing_fetchart.art_candidates == {}

    new_task, _, new_fetchart, new_dir = _art_guard_task(root, staging, "Brand New")
    loaded[:] = [new_fetchart]
    plugin._guard_art(session, new_task)
    assert not new_dir.exists()
    new_dir.mkdir(parents=True)
    (new_dir / "01.flac").write_bytes(b"audio")
    new_task.item.path = os.fsencode(new_dir / "01.flac")
    plugin._publish_art(session, new_task)

    assert (new_dir / "cover.jpg").read_bytes() == b"new artwork"
    assert new_task.album.artpath == os.fsencode(new_dir / "cover.jpg")
    assert new_task.album.stored == 1
    assert new_fetchart.art_candidates == {}

    real_copy = module._copy_candidate_to_private

    race_root = tmp_path / "race-music"
    race_root.mkdir()
    race_session = types.SimpleNamespace(
        lib=types.SimpleNamespace(directory=os.fsencode(race_root))
    )
    race_task, _, race_fetchart, race_dir = _art_guard_task(race_root, staging, "Moving Parent")
    loaded[:] = [race_fetchart]
    plugin._guard_art(race_session, race_task)
    race_dir.mkdir(parents=True)
    (race_dir / "01.flac").write_bytes(b"audio")
    race_task.item.path = os.fsencode(race_dir / "01.flac")
    displaced_artist = tmp_path / "displaced-artist-with-art"

    def move_parent_after_copy(parent_fd, candidate_fd):
        copied = real_copy(parent_fd, candidate_fd)
        (race_root / "Artist").rename(displaced_artist)
        (race_root / "Artist").mkdir()
        return copied

    monkeypatch.setattr(module, "_copy_candidate_to_private", move_parent_after_copy)
    plugin._publish_art(race_session, race_task)

    assert race_task.album.artpath is None
    assert not (displaced_artist / "Moving Parent" / "cover.jpg").exists()
    assert not (race_root / "Artist" / "Moving Parent" / "cover.jpg").exists()
    assert len(os.listdir("/proc/self/fd")) == descriptor_count


@pytest.fixture
def _need_ffmpeg():
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")


@pytest.fixture
def _need_flac():
    import shutil

    if shutil.which("flac") is None:
        pytest.skip("flac not available")


def _make_silent_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "1",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )


def test_import_override_yaml_round_trips_through_parser(monkeypatch):
    # The beets override is written by a hand-rolled single-quoted YAML
    # emitter.
    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    monkeypatch.setattr(cfg, "BEETS_DB_PATH", "/config/beets/musiclibrary.db")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", "/music")
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "$albumartist's picks/$album %aunique{}")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "Singles/$artist - $title")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "embed")

    parsed = yaml.safe_load(beets._build_import_override_yaml())

    assert parsed["library"] == "/config/beets/musiclibrary.db"
    assert parsed["directory"] == "/music"
    assert parsed["import"]["move"] is True
    assert parsed["import"]["autotag"] is False
    assert parsed["import"]["duplicate_action"] == "merge"
    # the apostrophe survived the single-quote doubling
    assert parsed["paths"]["default"] == "$albumartist's picks/$album %aunique{}"
    assert parsed["paths"]["singleton"] == "Singles/$artist - $title"
    assert "fetchart" in parsed["plugins"] and "embedart" in parsed["plugins"]

    # A relative deployment path must still name the same database and music
    # root after the override is written inside the Beets config directory.
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("music"))
    relative = yaml.safe_load(beets._build_import_override_yaml())
    assert relative["library"] == str(Path("beets/musiclibrary.db").absolute())
    assert relative["directory"] == str(Path("music").absolute())


# ── beets: staged artwork a multi-disc import would leave behind ──────────


def _staged_album(root, discs):
    album = root / "Artist" / "Album (2001)"
    album.mkdir(parents=True)
    (album / "cover.jpg").write_bytes(b"art")
    for disc in range(1, discs + 1):
        parent = album / f"Disc {disc}" if discs > 1 else album
        parent.mkdir(exist_ok=True)
        (parent / f"{disc:02d}. Track.flac").write_bytes(b"audio")
    return album


def test_multidisc_artwork_moves_where_beets_can_see_it(tmp_path):
    from qobuz_librarian.integrations.beets import relocate_disc_album_artwork

    album = _staged_album(tmp_path, 2)

    assert relocate_disc_album_artwork(album) is True
    # Beets gives the import task the disc directories, and fetchart searches
    # only those. A cover left in the album root is never filed, and the
    # leftover reads to the durable completion proof as an unfinished download.
    assert not (album / "cover.jpg").exists()
    assert (album / "Disc 1" / "cover.jpg").read_bytes() == b"art"
    assert not (album / "Disc 2" / "cover.jpg").exists()


def test_single_disc_artwork_is_left_where_fetchart_already_looks(tmp_path):
    from qobuz_librarian.integrations.beets import relocate_disc_album_artwork

    album = _staged_album(tmp_path, 1)

    assert relocate_disc_album_artwork(album) is False
    assert (album / "cover.jpg").exists()


def test_artwork_relocation_never_overwrites_a_disc_that_has_its_own(tmp_path):
    from qobuz_librarian.integrations.beets import relocate_disc_album_artwork

    album = _staged_album(tmp_path, 2)
    (album / "Disc 1" / "cover.jpg").write_bytes(b"the disc's own")

    assert relocate_disc_album_artwork(album) is False
    assert (album / "Disc 1" / "cover.jpg").read_bytes() == b"the disc's own"
