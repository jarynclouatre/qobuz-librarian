import errno
import os
import sqlite3

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.library import post_import_relocation as relocation


def _configure(tmp_path, monkeypatch):
    music = tmp_path / "music"
    data = tmp_path / "data"
    beets = tmp_path / "beets"
    for directory in (music, data, beets):
        directory.mkdir()
    database = beets / "library.db"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "LOCK_FILE", data / "run.lock")
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", beets)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    return music, data, database


def _database(database, *, tracks, artwork=None):
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, artpath BLOB)"
        )
        connection.executemany(
            "INSERT INTO items (id, path) VALUES (?, ?)",
            ((index, os.fsencode(path)) for index, path in enumerate(tracks, 1)),
        )
        connection.execute(
            "INSERT INTO albums (id, artpath) VALUES (1, ?)",
            (os.fsencode(artwork) if artwork is not None else None,),
        )
        connection.commit()
    finally:
        connection.close()


def _paths(database):
    connection = sqlite3.connect(database)
    try:
        tracks = [
            os.fsdecode(row[0])
            for row in connection.execute("SELECT path FROM items ORDER BY id")
        ]
        artwork = connection.execute(
            "SELECT artpath FROM albums WHERE id = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    return tracks, os.fsdecode(artwork) if artwork is not None else None


def test_relocation_is_additive_and_keeps_destination_conflicts(
    tmp_path, monkeypatch
):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)

    moving = source / "01 - New.flac"
    conflict = source / "02 - Conflict.flac"
    source_art = source / "cover.jpg"
    destination_conflict = destination / conflict.name
    destination_art = destination / source_art.name
    moving.write_bytes(b"new track")
    conflict.write_bytes(b"source version")
    source_art.write_bytes(b"same artwork")
    destination_conflict.write_bytes(b"destination version")
    destination_art.write_bytes(b"same artwork")
    _database(database, tracks=(moving, conflict), artwork=source_art)

    authority = run_lock.acquire()
    try:
        result = relocation.relocate_post_import_album(
            source,
            destination,
            kind=relocation.RelocationKind.SPLIT_GAP_FILL,
            authority=authority,
        )
    finally:
        authority.close()

    assert result.changed is True
    assert (destination / moving.name).read_bytes() == b"new track"
    assert destination_conflict.read_bytes() == b"destination version"
    assert destination_art.read_bytes() == b"same artwork"
    assert conflict.read_bytes() == b"source version"
    assert not moving.exists()
    assert not source_art.exists()
    tracks, artwork = _paths(database)
    assert tracks == [str(destination / moving.name), str(conflict)]
    assert artwork == str(destination_art)


def test_whole_album_conflict_keeps_the_source_album_intact(
    tmp_path, monkeypatch
):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    source_track = source / "01 - Track.flac"
    source_art = source / "cover.jpg"
    destination_track = destination / source_track.name
    source_track.write_bytes(b"source audio")
    source_art.write_bytes(b"source artwork")
    destination_track.write_bytes(b"different destination audio")
    _database(database, tracks=(source_track,), artwork=source_art)

    authority = run_lock.acquire()
    try:
        result = relocation.relocate_post_import_album(
            source,
            destination,
            kind=relocation.RelocationKind.WHOLE_ALBUM,
            authority=authority,
        )
    finally:
        authority.close()

    assert result.changed is False
    assert result.destination == source
    assert source_track.read_bytes() == b"source audio"
    assert source_art.read_bytes() == b"source artwork"
    assert destination_track.read_bytes() == b"different destination audio"
    assert not (destination / source_art.name).exists()
    assert _paths(database) == ([str(source_track)], str(source_art))


@pytest.mark.parametrize(
    ("checkpoint", "committed"),
    (
        ("after-copy-data", False),
        ("after-filesystem-publication", False),
        ("after-database-publication", True),
    ),
)
def test_hard_kill_recovery_uses_database_as_commit_decision(
    tmp_path, monkeypatch, checkpoint, committed
):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    child = os.fork()
    if child == 0:
        try:
            authority = run_lock.acquire()

            def hard_kill(name):
                if name == checkpoint:
                    os._exit(77)

            relocation._checkpoint = hard_kill
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
        except BaseException:
            os._exit(91)
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.CLEAR
    new_track = destination / track.name
    assert track.exists() is not committed
    assert new_track.exists() is committed
    tracks, _artwork = _paths(database)
    assert tracks == [str(new_track if committed else track)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))
    assert not list(music.glob(".qobuz-librarian-relocation-*"))


def test_copy_failure_rolls_back_without_touching_source(tmp_path, monkeypatch):
    music, data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr(relocation, "_copy_file", disk_full)
    authority = run_lock.acquire()
    try:
        with pytest.raises(OSError, match="no space left"):
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert track.read_bytes() == b"audio"
    assert not destination.exists()
    assert _paths(database)[0] == [str(track)]
    assert not list(data.glob(".qobuz_post_import_relocations/*.json"))
    assert not list(music.glob(".qobuz-librarian-relocation-*"))


def test_album_link_is_refused_without_following_it(tmp_path, monkeypatch):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    outside.mkdir()
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    secret = outside / "secret"
    secret.write_bytes(b"keep")
    (source / "link").symlink_to(outside, target_is_directory=True)
    _database(database, tracks=(track,))

    authority = run_lock.acquire()
    try:
        with pytest.raises(OSError, match="link or special entry"):
            relocation.relocate_post_import_album(
                source,
                music / "Artist" / "Album",
                kind=relocation.RelocationKind.WHOLE_ALBUM,
                authority=authority,
            )
    finally:
        authority.close()

    assert secret.read_bytes() == b"keep"
    assert track.read_bytes() == b"audio"
    assert _paths(database)[0] == [str(track)]


def test_recovery_refuses_a_replaced_destination_album(tmp_path, monkeypatch):
    music, _data, database = _configure(tmp_path, monkeypatch)
    source = music / "Artist, Other" / "Album"
    destination = music / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    track = source / "01 - Track.flac"
    track.write_bytes(b"audio")
    _database(database, tracks=(track,))

    child = os.fork()
    if child == 0:
        try:
            authority = run_lock.acquire()

            def hard_kill(name):
                if name == "after-filesystem-publication":
                    os._exit(77)

            relocation._checkpoint = hard_kill
            relocation.relocate_post_import_album(
                source,
                destination,
                kind=relocation.RelocationKind.SPLIT_GAP_FILL,
                authority=authority,
            )
        except BaseException:
            os._exit(91)
        os._exit(92)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    published = destination / track.name
    assert published.exists()
    old_destination = destination.with_name("Album.old")
    destination.rename(old_destination)
    destination.mkdir()
    (old_destination / track.name).rename(published)

    authority = run_lock.acquire()
    try:
        recovered = relocation.reconcile_post_import_relocations(
            authority=authority
        )
    finally:
        authority.close()

    assert recovered.status is relocation.RelocationRecoveryStatus.ATTENTION_REQUIRED
    assert track.read_bytes() == b"audio"
    assert published.read_bytes() == b"audio"
    assert _paths(database)[0] == [str(track)]
