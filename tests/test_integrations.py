"""Tests for rip, beets, lyrics, and the seams between them."""

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
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


def test_lyrics_provider_lookup_has_an_application_deadline(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyric_fetch

    release = threading.Event()

    class ProviderLibrary:
        @staticmethod
        def search(*_args, **_kwargs):
            release.wait()
            return "late result"

    monkeypatch.setattr(lyric_fetch, "syncedlyrics", ProviderLibrary())
    monkeypatch.setattr(cfg, "LYRICS_PROVIDER_TIMEOUT", 0.02)
    lyric_fetch._provider_fails.clear()
    lyric_fetch._dead_providers.clear()

    started = time.monotonic()
    result, failed = lyric_fetch._query_provider(
        "song artist", "SlowProvider", logging.getLogger("test")
    )
    elapsed = time.monotonic() - started

    assert result is None
    assert failed is True
    assert elapsed < 0.5
    assert lyric_fetch._provider_fails["SlowProvider"] == 1
    release.set()
    for thread in threading.enumerate():
        if thread.name == "lyrics-provider-SlowProvider":
            thread.join(timeout=1)

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




def test_rip_url_kills_and_reaps_a_timed_out_process(monkeypatch):
    from qobuz_librarian.integrations import rip

    real_popen = subprocess.Popen

    def sleeping_process(_args, **kwargs):
        return real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **kwargs,
        )

    monkeypatch.setattr(rip.subprocess, "Popen", sleeping_process)
    code, output = rip.rip_url(
        "https://example.invalid/album", timeout=0.05
    )

    assert code == 124
    assert "rip timed out" in output








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




def test_lyric_retry_read_error_does_not_replace_manifest(
        tmp_path, monkeypatch):
    import errno

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyrics

    retry_file = tmp_path / "retry.json"
    original = b'{"version":1,"files":["/music/kept.flac"]}'
    retry_file.write_bytes(original)
    monkeypatch.setattr(cfg, "LYRIC_RETRY_FILE", retry_file)
    monkeypatch.setattr(cfg, "LYRIC_RETRY_VERSION", 1)
    path_open = Path.open

    def fail_manifest_read(self, *args, **kwargs):
        if self == retry_file:
            raise OSError(errno.EIO, "injected read error")
        return path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_manifest_read)
    lyrics._record_post_import_lyric_retry(["/music/new.flac"])
    monkeypatch.undo()

    assert retry_file.read_bytes() == original












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


@pytest.mark.parametrize("plain_representation", ["embed", "sidecar"])
def test_both_lyrics_repairs_a_plain_sibling_without_provider(
        tmp_path, monkeypatch, plain_representation, _need_ffmpeg):
    from mutagen.flac import FLAC

    from qobuz_librarian.integrations import lyric_fetch

    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    track = tmp_path / "Artist" / "Album" / "track.flac"
    _make_silent_flac(track)
    synced = "[00:01.00]same lyric"
    plain = "older plain lyric"
    tagged = FLAC(track)
    tagged["lyrics"] = plain if plain_representation == "embed" else synced
    tagged.save()
    track.with_suffix(".lrc").write_text(
        synced if plain_representation == "embed" else plain,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lyric_fetch,
        "search_lyrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local representation repair must not call a provider")
        ),
    )

    counts = lyric_fetch.fetch_for_paths(
        [track],
        owned_root=tmp_path,
        state_path=tmp_path / "state.json",
        rescan=True,
        workers=1,
        lyrics_format="both",
    )

    assert counts == {"wrote-synced": 1}
    assert FLAC(track)["lyrics"] == [synced]
    assert track.with_suffix(".lrc").read_text(encoding="utf-8") == synced




def test_post_import_retry_resolution_preserves_duplicate_signatures(
        tmp_path, monkeypatch):
    from qobuz_librarian.integrations import lyrics, rip

    first = tmp_path / "First" / "track.flac"
    second = tmp_path / "Second" / "track.flac"
    for track in (first, second):
        track.parent.mkdir()
        track.write_bytes(b"synthetic")
    signature = ("artist", "album", 1, 1, "title")
    monkeypatch.setattr(rip, "_flac_signature", lambda _path: signature)

    resolved = lyrics._resolve_signatures_to_paths(
        [(signature, "/staging/first.flac"),
         (signature, "/staging/second.flac")],
        [first.parent, second.parent],
    )

    assert resolved == [str(first), str(second)]




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


def test_forget_beets_entries_resolves_relative_catalogue_paths(
    monkeypatch, tmp_path
):
    """Undo must find a relative Beets row through the configured music root.

    Managed imports can store a root-relative path even when an administrator's
    normal Beets config names another mount alias for the same library.  The
    cleanup path is already gone by the time this helper runs, so an absolute
    ``path:`` query cannot use filesystem identity to bridge those aliases.
    """
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    music_root = tmp_path / "app-view" / "music"
    deleted = music_root / "Artist" / "Album" / "01 - Track.flac"
    deleted.parent.mkdir(parents=True)
    config_dir = tmp_path / "beets"
    config_dir.mkdir()
    database = config_dir / "library.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO items (id, path) VALUES (?, ?)",
            (7, os.fsencode("Artist/Album/01 - Track.flac")),
        )
        connection.execute(
            "INSERT INTO items (id, path) VALUES (?, ?)",
            (8, os.fsencode("Other/Album/01 - Track.flac")),
        )

    monkeypatch.setattr(cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)

    class Runtime:
        python = sys.executable

    monkeypatch.setattr(beets, "_resolve_beets_runtime", Runtime)
    monkeypatch.setattr(beets, "_require_beets_runtime", lambda _runtime: None)
    calls = []

    def run_beets(args, **_kwargs):
        calls.append(args)
        if "remove" in args:
            item_ids = [
                int(argument.removeprefix("id:"))
                for argument in args
                if argument.startswith("id:")
            ]
            with sqlite3.connect(database) as connection:
                connection.executemany(
                    "DELETE FROM items WHERE id = ?",
                    [(item_id,) for item_id in item_ids],
                )
        # Model Beets' old absolute-path lookup missing this relative row.
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(beets, "_run_owned_beets_capture", run_beets)

    result = beets.forget_beets_entries([deleted])

    assert result == beets.ForgetBeetsEntriesResult(True, 1)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id, path FROM items").fetchall() == [
            (8, os.fsencode("Other/Album/01 - Track.flac"))
        ]
    remove = next(args for args in calls if "remove" in args)
    assert "id:7" in remove
    assert not any(argument.startswith("path:") for argument in remove)


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


def test_managed_override_seals_pinned_database_root_and_plugin_order(
        monkeypatch, tmp_path):
    import fcntl

    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    config_dir = tmp_path / "beets"
    music = tmp_path / "music"
    config_dir.mkdir()
    music.mkdir()
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", config_dir / "library.db")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "sidecar")
    capture = {"_override_fd": None}

    override = beets._prepare_managed_override(
        capture,
        {
            "plugins": ["fetchart", "inline", "permissions"],
            "plugin_paths": ["/user/beets-plugins"],
            "disabled": [],
            "musicbrainz_enabled": None,
        },
    )
    descriptor = capture["_override_fd"]
    try:
        assert override == Path(f"/proc/self/fd/{descriptor}")
        payload = os.pread(descriptor, os.fstat(descriptor).st_size, 0)
        configured = yaml.safe_load(payload)
        assert configured["library"] == str(config_dir / "library.db")
        assert configured["directory"] == str(music)
        assert configured["import"] == {
            "quiet": True,
            "incremental": False,
            "autotag": False,
            "write": False,
            "move": True,
            "duplicate_action": "merge",
        }
        assert configured["plugins"] == [
            "fetchart",
            "inline",
            "permissions",
            "qobuz_art_guard",
            "qobuz_ownership",
        ]
        assert configured["pluginpath"] == [
            str(Path(beets.__file__).parent / "beets_plugins"),
            "/user/beets-plugins",
        ]
        required_seals = (
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE
        )
        assert fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & required_seals \
            == required_seals
        with pytest.raises(OSError):
            os.pwrite(descriptor, b"x", 0)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    "catalogue_shape",
    [
        "proved",
        "legacy-album-complete",
        "candidate-album-duplicate",
        "destination-without-album",
        "missing-candidate-album",
        "outside-library-row",
        "malformed-path",
    ],
)
def test_managed_import_identifies_one_complete_album_beside_legacy_rows(
        monkeypatch, tmp_path, catalogue_shape):
    """A gap fill can leave legacy rows beside the one complete album."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    first = album / "01.flac"
    second = album / "02.flac"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    database = tmp_path / "beets.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE items ("
            "id INTEGER PRIMARY KEY, path BLOB NOT NULL, album_id INTEGER)"
        )
        connection.executemany("INSERT INTO albums VALUES (?)", [(10,), (20,)])
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, ?)",
            [
                (1, os.fsencode(first), 10),
                (11, os.fsencode(first), 20),
                (12, os.fsencode(second), 20),
            ],
        )
        if catalogue_shape == "legacy-album-complete":
            connection.execute(
                "INSERT INTO items VALUES (?, ?, ?)",
                (2, os.fsencode(second), 10),
            )
        elif catalogue_shape == "candidate-album-duplicate":
            connection.execute(
                "INSERT INTO items VALUES (?, ?, ?)",
                (13, os.fsencode(first), 20),
            )
        elif catalogue_shape == "destination-without-album":
            connection.execute(
                "INSERT INTO items VALUES (?, ?, ?)",
                (13, os.fsencode(second), None),
            )
        elif catalogue_shape == "missing-candidate-album":
            connection.execute("DELETE FROM albums WHERE id = 20")
        elif catalogue_shape == "outside-library-row":
            connection.execute(
                "INSERT INTO items VALUES (?, ?, ?)",
                (2, os.fsencode(tmp_path / "outside.flac"), 10),
            )
        elif catalogue_shape == "malformed-path":
            connection.execute(
                "INSERT INTO items VALUES (?, ?, ?)",
                (2, b"bad\x00path", 10),
            )

    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    monkeypatch.setattr(cfg, "BEETS_TIMEOUT", 5)
    destinations = {
        first.relative_to(music).as_posix(),
        second.relative_to(music).as_posix(),
    }
    anchor = beets._open_beets_database_anchor()
    try:
        matches = beets._managed_database_matches(
            anchor, destinations, str(music)
        )
        boundary = beets._managed_album_boundary(
            destinations,
            str(music),
            beets._ownership_identity(os.stat(music)),
            database_anchor=anchor,
        )
    finally:
        beets._close_beets_database_anchor(anchor)

    assert matches is (catalogue_shape == "proved")
    if catalogue_shape != "proved":
        assert boundary is None
    else:
        assert boundary is not None
        assert boundary[0] == "Artist/Album"


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


def test_staging_receipts_refuse_hardlinked_files(tmp_path, monkeypatch):
    from qobuz_librarian.integrations.staging import capture_file, capture_tree

    staging = tmp_path / "staging"
    album = staging / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "01.flac"
    outside = tmp_path / "outside.flac"
    track.write_bytes(b"audio")
    os.link(track, outside)
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)

    assert capture_file(track) is None
    assert capture_tree(album) is None
    assert outside.read_bytes() == b"audio"


def test_ownership_source_guard_refuses_a_hardlinked_track(
        tmp_path, monkeypatch):
    module = _load_ownership_for_test(monkeypatch)
    root = tmp_path / "staging"
    track = root / "Artist" / "Album" / "01.flac"
    outside = tmp_path / "outside.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    os.link(track, outside)
    plugin = object.__new__(module.QobuzOwnershipPlugin)
    held = None

    try:
        with pytest.raises(OSError, match="single-link"):
            held = plugin._hold_source_leaf_locked(root, track)
    finally:
        if held is not None:
            os.close(held[2])
            os.close(held[0])

    assert outside.read_bytes() == b"audio"


@pytest.mark.parametrize(
    ("move_shape", "accepted"),
    [
        ("rename", True),
        ("copy-unlink", True),
        ("source-name-reused", False),
        ("source-still-linked", False),
        ("content-changed", False),
        ("destination-hardlinked", False),
    ],
)
def test_ownership_accepts_only_an_exact_single_link_move(
        tmp_path, monkeypatch, move_shape, accepted):
    """Accept rename or copy-unlink, but reject unsafe lookalikes."""
    module = _load_ownership_for_test(monkeypatch)
    staging = tmp_path / "staging"
    source = staging / "01.flac"
    library = tmp_path / "music"
    destination = library / "Artist" / "Album" / "01.flac"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b"single-track-audio")

    plugin = object.__new__(module.QobuzOwnershipPlugin)
    plugin._lock = threading.RLock()
    plugin._enabled = True
    plugin._managed = True
    plugin._root = os.fsencode(library)
    plugin._root_fd = os.open(library, os.O_RDONLY | os.O_DIRECTORY)
    source_parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    source_fd = os.open(source, os.O_RDONLY)
    item = object()
    selected = {
        "item": item,
        "source_path": os.path.abspath(os.fsencode(source)),
        "source_parent_fd": source_parent_fd,
        "source_name": os.fsencode(source.name),
        "source_fd": source_fd,
        "destination_parent": [b"Artist", b"Album"],
        "pending_move": None,
        "destination": None,
        "destination_identity": None,
        "move_proven": False,
    }
    plugin._source_items = [selected]
    plugin._source_item = selected

    try:
        plugin._before_item_moved(item, source, destination)
        if move_shape == "rename":
            source.rename(destination)
        else:
            shutil.copyfile(source, destination)
        if move_shape not in {"rename", "source-still-linked"}:
            source.unlink()
        if move_shape == "source-name-reused":
            source.write_bytes(b"unrelated-replacement")
        elif move_shape == "content-changed":
            destination.write_bytes(b"tampered-audio")
        elif move_shape == "destination-hardlinked":
            os.link(destination, tmp_path / "outside-hardlink.flac")
        if accepted:
            plugin._item_moved(item, source, destination)
        else:
            with pytest.raises((OSError, ValueError)):
                plugin._item_moved(item, source, destination)
        assert selected["move_proven"] is accepted
    finally:
        os.close(source_fd)
        os.close(source_parent_fd)
        os.close(plugin._root_fd)


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
    assert conf["import"]["write"] is False
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




def test_artwork_relocation_never_overwrites_a_disc_that_has_its_own(tmp_path):
    from qobuz_librarian.integrations.beets import relocate_disc_album_artwork

    album = _staged_album(tmp_path, 2)
    (album / "Disc 1" / "cover.jpg").write_bytes(b"the disc's own")

    assert relocate_disc_album_artwork(album) is False
    assert (album / "Disc 1" / "cover.jpg").read_bytes() == b"the disc's own"
