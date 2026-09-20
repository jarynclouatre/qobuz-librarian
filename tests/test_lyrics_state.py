"""Lyrics checkpoints, retry handoffs, and library-only maintenance."""

import logging
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import lyric_fetch, lyrics, rip
from qobuz_librarian.library import lyrics as library_lyrics


def test_checkpoints_batch_and_flush_without_replacing_another_pass(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    lyric_fetch.save_state({"shared": lyric_fetch.TrackState(status="plain")}, path)
    now = [100.0]
    monkeypatch.setattr(lyric_fetch.time, "monotonic", lambda: now[0])
    writer = lyric_fetch._StateWriter(path, logging.getLogger("test"))
    other = lyric_fetch._StateWriter(path, logging.getLogger("test"))

    with patch.object(lyric_fetch, "_write_state_unlocked",
                      wraps=lyric_fetch._write_state_unlocked) as write:
        lyric_fetch._commit(writer, "first", lyric_fetch.TrackState(status="synced"))
        writer.save()
        lyric_fetch._commit(writer, "second", lyric_fetch.TrackState(status="transient"))
        writer.save()
        assert write.call_count == 1
        assert "second" not in lyric_fetch.load_state(path)

        lyric_fetch._commit(other, "shared", lyric_fetch.TrackState(status="synced"))
        other.flush()
        writer.flush()
        saved = lyric_fetch.load_state(path)
        assert saved["second"].status == "transient"
        assert saved["shared"].status == "synced"
        assert saved["first"].status == "synced"
        assert write.call_count == 3

        lyric_fetch._commit(writer, "third", lyric_fetch.TrackState(status="plain"))
        writer.save()
        assert write.call_count == 3
        now[0] += lyric_fetch.CHECKPOINT_INTERVAL_SECONDS
        writer.save()
        assert write.call_count == 4
        assert lyric_fetch.load_state(path)["third"].status == "plain"
        writer.flush()
        assert write.call_count == 4


def test_failed_checkpoint_keeps_retry_state_for_final_flush(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(lyric_fetch.time, "monotonic", lambda: 100.0)
    writer = lyric_fetch._StateWriter(path, logging.getLogger("test"))
    lyric_fetch._commit(writer, "track", lyric_fetch.TrackState(status="transient"))
    with patch.object(lyric_fetch, "_write_state_unlocked", side_effect=OSError) as write:
        writer.save()
        writer.save()
        assert write.call_count == 1
    writer.flush()
    assert lyric_fetch.load_state(path)["track"].status == "transient"


@pytest.mark.parametrize("engine", ["fetch_for_paths", "index_existing"])
@pytest.mark.parametrize("workers", [1, 2])
def test_interruption_flushes_finished_and_draining_workers(
        tmp_path, monkeypatch, engine, workers):
    paths = [tmp_path / name for name in ("first.flac", "second.flac")]
    for path in paths:
        path.write_bytes(b"audio")
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(lyric_fetch, "FLAC", lambda _p: SimpleNamespace(
        tags={"lyrics": ["[00:01.00]lyrics"]}))

    def process(path, state, *_args, **_kwargs):
        lyric_fetch._commit(state, str(path), lyric_fetch.TrackState(status="transient"))
        return "providers-unavailable"

    monkeypatch.setattr(lyric_fetch, "process_file", process)
    second_started = threading.Event()
    release = threading.Event()
    commit = lyric_fetch._commit

    def slow_commit(state, key, value):
        if key == str(paths[1]):
            second_started.set()
            assert release.wait(5)
        commit(state, key, value)

    monkeypatch.setattr(lyric_fetch, "_commit", slow_commit)

    def interrupt(*_args):
        if workers > 1:
            assert second_started.wait(5)
        release.set()
        raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            getattr(lyric_fetch, engine)(
                paths, owned_root=tmp_path, state_path=state_path,
                workers=workers, progress_cb=interrupt)
    finally:
        release.set()
    saved = lyric_fetch.load_state(state_path)
    assert set(saved) == {str(path) for path in paths[:workers]}
    if engine == "fetch_for_paths":
        monkeypatch.setattr(cfg, "LYRIC_FETCH_STATE_FILE", state_path)
        monkeypatch.setattr(cfg, "LYRIC_RETRY_FILE", tmp_path / "retry.json")
        monkeypatch.setattr(lyrics, "HAVE_LYRIC_FETCH", True)
        assert lyrics._refresh_lyric_retry(paths)
        assert set(lyrics.load_lyric_retry()) == set(saved)


def test_cancel_flushes_results_held_inside_interval(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(lyric_fetch.time, "monotonic", lambda: 100.0)
    processed = []

    def process(path, state, *_args, **_kwargs):
        lyric_fetch._commit(state, str(path), lyric_fetch.TrackState(status="transient"))
        processed.append(str(path))
        return "providers-unavailable"

    monkeypatch.setattr(lyric_fetch, "process_file", process)
    counts = lyric_fetch.fetch_for_paths(
        [tmp_path / f"{n}.flac" for n in range(3)], owned_root=tmp_path,
        state_path=state_path, workers=1, should_stop=lambda: len(processed) == 2)
    assert counts["stopped"] == 1
    assert counts["providers-unavailable"] == 2
    assert set(lyric_fetch.load_state(state_path)) == set(processed)


def test_album_import_does_not_check_saved_paths_and_flushes_retry_handoff(
        tmp_path, monkeypatch):
    album = tmp_path / "staging" / "Album"
    album.mkdir(parents=True)
    tracks = [album / f"{n}.flac" for n in range(3)]
    for track in tracks:
        track.write_bytes(b"audio")
    state_path = tmp_path / "state.json"
    old_key = str(tmp_path / "deleted.flac")
    lyric_fetch.save_state({old_key: lyric_fetch.TrackState(status="plain")}, state_path)
    monkeypatch.setattr(cfg, "LYRIC_FETCH_STATE_FILE", state_path)
    monkeypatch.setattr(cfg, "STAGING_DIR", album.parent)
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(lyric_fetch.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(rip, "_flac_signature", lambda path: path.name)
    exists = os.path.exists

    def check_path(path):
        assert str(path) != old_key, "album import walked an unrelated saved path"
        return exists(path)

    monkeypatch.setattr(lyric_fetch.os.path, "exists", check_path)

    def process(path, state, *_args, **_kwargs):
        lyric_fetch._commit(state, str(path), lyric_fetch.TrackState(status="transient"))
        return "providers-unavailable"

    monkeypatch.setattr(lyric_fetch, "process_file", process)
    counts, signatures = lyrics._run_lyric_hook(album)
    assert counts["providers-unavailable"] == 3
    assert {path for _, path in signatures} == {str(path) for path in tracks}
    assert old_key in lyric_fetch.load_state(state_path)


def test_deleted_paths_pruned_only_by_whole_library_pass(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    live = tmp_path / "live.flac"
    live.touch()
    deleted = tmp_path / "deleted.flac"
    lyric_fetch.save_state({
        str(path): lyric_fetch.TrackState(status="synced") for path in (live, deleted)
    }, state_path)
    monkeypatch.setattr(cfg, "LYRIC_FETCH_STATE_FILE", state_path)
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(library_lyrics, "iter_library_flacs", lambda **_kwargs: iter(()))
    library_lyrics.run_library_lyrics(artist_dirs=[tmp_path])
    library_lyrics.run_library_lyrics(dry_run=True)
    assert str(deleted) in lyric_fetch.load_state(state_path)
    library_lyrics.run_library_lyrics(rescan=True)
    assert set(lyric_fetch.load_state(state_path)) == {str(live)}
