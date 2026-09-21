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
