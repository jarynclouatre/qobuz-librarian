from types import SimpleNamespace

import pytest


def _args(**over):
    base = dict(force=False, yes=True, no_import=False, dry_run=False,
                verbose=False, consolidate=False, no_upgrade=False,
                no_downsample=True,
                auto_upgrade=False, prefer_hires=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_direct_download_refuses_files_changed_after_confirmation(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    source = album_dir / "01.flac"
    source.write_bytes(b"reviewed bytes")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    album = {
        "id": "album",
        "title": "Album",
        "artist": {"name": "Artist"},
        "tracks": {"items": [
            {"id": "owned", "title": "Owned"},
            {"id": "missing", "title": "Missing"},
        ]},
    }
    existing = [{"title": "Owned", "path": str(source)}]

    monkeypatch.setattr(proc, "is_lossless_album", lambda _album: True)
    monkeypatch.setattr(
        proc,
        "find_existing_tracks",
        lambda _album: (existing, album_dir),
    )
    monkeypatch.setattr(
        proc,
        "compute_missing",
        lambda tracks, _existing: ([tracks[1]], [tracks[0]]),
    )
    monkeypatch.setattr(proc, "print_album_summary", lambda *_a, **_kw: None)

    def confirm(*_args, **_kwargs):
        source.write_bytes(b"replacement bytes")
        return True

    monkeypatch.setattr(proc, "confirm", confirm)
    monkeypatch.setattr(proc, "staging_preflight", lambda _args: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "log_fetch", lambda _row: None)
    monkeypatch.setattr(
        proc,
        "run_album_download",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("download started against changed files")
        ),
    )

    result = proc.process_album(album, _args(yes=False), token="tok")

    assert result["result"] == "stale_candidate"
    assert source.read_bytes() == b"replacement bytes"


def test_partial_retention_moves_only_the_recorded_download_run(
        monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.download import retain_download_staging
    from qobuz_librarian.integrations import staging as staging_tx

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    owned = staging_tx.create_staging_run()
    concurrent = staging_tx.create_staging_run()
    owned_file = owned.path / "Artist" / "Album" / "01.flac"
    concurrent_file = concurrent.path / "Other" / "Album" / "01.flac"
    owned_file.parent.mkdir(parents=True)
    concurrent_file.parent.mkdir(parents=True)
    owned_file.write_bytes(b"owned partial")
    concurrent_file.write_bytes(b"concurrent run")
    result = {"_staging_run": owned.to_record()}

    assert retain_download_staging(result, label="incomplete-replacement")
    assert not owned.path.exists()
    assert concurrent_file.read_bytes() == b"concurrent run"
    groups = staging_tx.list_groups(kind="interrupted")
    assert len(groups) == 1
    assert list(groups[0].trees[0].path.rglob("*.flac"))[0].read_bytes() == (
        b"owned partial"
    )


def test_upgrade_carries_hand_added_tags_to_the_replacement(monkeypatch, tmp_path):
    import shutil
    import subprocess

    from mutagen.flac import FLAC, Picture

    from qobuz_librarian.library.backup import backup_album_dir
    from qobuz_librarian.modes import process as proc

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")

    def track(path, **tags):
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=d=1", "-c:a", "flac", "-y", str(path)], check=True)
        f = FLAC(path)
        for key, value in {"TITLE": "Song", "TRACKNUMBER": "1",
                           "DISCNUMBER": "1", "ISRC": "USAAA2100001",
                           **tags}.items():
            f[key] = value
        f.save()
        return f

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    original = track(album_dir / "01 Song.flac", COMMENT="my note",
                     MY_TAG="mine", REPLAYGAIN_TRACK_GAIN="-3 dB")
    back = Picture()
    back.type, back.mime, back.data = 4, "image/png", b"back-cover"
    original.add_picture(back)
    original.save()
    monkeypatch.setattr(proc.cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(proc.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    backup = backup_album_dir(album_dir)
    assert backup is not None and backup.complete
    track(album_dir / "01 Song.flac")

    proc._carry_non_audio_from_backup({"id": "x"}, album_dir, backup,
                                      replacement_dir=album_dir)

    replacement = FLAC(album_dir / "01 Song.flac")
    assert replacement["COMMENT"] == ["my note"]
    assert replacement["MY_TAG"] == ["mine"]
    assert "REPLAYGAIN_TRACK_GAIN" not in replacement
    assert [p.data for p in replacement.pictures] == [b"back-cover"]


def test_replacement_catalogue_retires_only_captured_rows(monkeypatch, tmp_path):
    import os
    import sqlite3

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    old_files = [album / "01.flac", album / "02.flac"]
    for path in old_files:
        path.write_bytes(b"old")

    database = tmp_path / "beets.db"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    monkeypatch.setattr(cfg, "BEETS_TIMEOUT", 5)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute(
            "CREATE TABLE items ("
            "id INTEGER PRIMARY KEY, path BLOB NOT NULL, album_id INTEGER, title TEXT)"
        )
        connection.execute(
            "CREATE TABLE album_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, key TEXT, value TEXT)"
        )
        connection.execute(
            "CREATE TABLE item_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, key TEXT, value TEXT)"
        )
        connection.execute("INSERT INTO albums VALUES (10, 'Original')")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, 10, ?)",
            [
                (1, os.fsencode(old_files[0]), "Old one"),
                (2, os.fsencode(old_files[1]), "Old two"),
            ],
        )
        connection.execute(
            "INSERT INTO album_attributes VALUES (1, 10, 'source', 'old')"
        )
        connection.execute(
            "INSERT INTO item_attributes VALUES (1, 1, 'source', 'old')"
        )
    connection.close()

    snapshot = beets.capture_beets_album_entries(album)
    assert snapshot is not None

    backup = tmp_path / "backup"
    album.rename(backup)
    album.mkdir()
    replacement_files = [album / f"0{number}.flac" for number in range(1, 4)]
    for path in replacement_files:
        path.write_bytes(b"new")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO albums VALUES (20, 'Replacement')")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, 20, ?)",
            [
                (11, os.fsencode(replacement_files[0]), "New one"),
                (12, os.fsencode(replacement_files[1]), "New two"),
                (13, os.fsencode(replacement_files[2]), "New three"),
            ],
        )
        connection.execute(
            "INSERT INTO album_attributes VALUES (2, 20, 'source', 'new')"
        )
        connection.execute(
            "INSERT INTO item_attributes VALUES (2, 11, 'source', 'new')"
        )
    connection.close()

    assert beets.retire_replaced_beets_entries(
        snapshot, album, replacement_files
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM items ORDER BY id").fetchall() == [
            (11,), (12,), (13,)
        ]
        assert connection.execute("SELECT id FROM albums ORDER BY id").fetchall() == [
            (20,)
        ]
        assert connection.execute(
            "SELECT entity_id FROM item_attributes ORDER BY id"
        ).fetchall() == [(11,)]
        assert connection.execute(
            "SELECT entity_id FROM album_attributes ORDER BY id"
        ).fetchall() == [(20,)]
    connection.close()


@pytest.mark.parametrize("scenario", ["clean", "ambiguous"])
def test_backup_catalogue_retirement_selects_one_complete_replacement_album(
        monkeypatch, tmp_path, scenario):
    """Retire legacy rows only when one album covers every replacement."""
    import os
    import sqlite3

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    shared = album / "01.flac"
    added = album / "02.flac"
    retired = album / "Old name.flac"
    shared.write_bytes(b"replacement-one")
    added.write_bytes(b"replacement-two")

    database = tmp_path / "beets.db"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", database)
    monkeypatch.setattr(cfg, "BEETS_TIMEOUT", 5)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT)"
        )
        connection.execute(
            "CREATE TABLE items ("
            "id INTEGER PRIMARY KEY, path BLOB NOT NULL, "
            "album_id INTEGER, title TEXT)"
        )
        connection.execute(
            "CREATE TABLE album_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, "
            "key TEXT, value TEXT)"
        )
        connection.execute(
            "CREATE TABLE item_attributes ("
            "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, "
            "key TEXT, value TEXT)"
        )
        connection.executemany(
            "INSERT INTO albums VALUES (?, ?)",
            [(10, "Retired partial"), (20, "Full replacement")],
        )
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, ?, ?)",
            [
                (1, os.fsencode(shared), 10, "Original shared row"),
                (2, os.fsencode(retired), 10, "Old name"),
                (11, os.fsencode(shared), 20, "Replacement one"),
                (12, os.fsencode(added), 20, "Replacement two"),
            ],
        )
        connection.execute(
            "INSERT INTO item_attributes VALUES (1, 1, 'source', 'old')"
        )
        connection.execute(
            "INSERT INTO album_attributes VALUES (1, 10, 'source', 'old')"
        )
        if scenario == "ambiguous":
            connection.execute(
                "INSERT INTO items VALUES (?, ?, ?, ?)",
                (3, os.fsencode(added), 10, "Ambiguous complete row"),
            )
    connection.close()

    backup = {
        "receipt": {
            "origin": str(album),
            "tree": {
                "files": {
                    shared.name: {},
                    retired.name: {},
                }
            },
        }
    }
    replacement_receipt = {
        "tree": {"files": {shared.name: {}, added.name: {}}}
    }

    result = beets.retire_backup_beets_entries(
        backup, album, replacement_receipt
    )

    assert result is (scenario == "clean")
    with sqlite3.connect(database) as connection:
        item_ids = connection.execute(
            "SELECT id FROM items ORDER BY id"
        ).fetchall()
        album_ids = connection.execute(
            "SELECT id FROM albums ORDER BY id"
        ).fetchall()
    if scenario == "ambiguous":
        assert item_ids == [(1,), (2,), (3,), (11,), (12,)]
        assert album_ids == [(10,), (20,)]
    else:
        assert item_ids == [(11,), (12,)]
        assert album_ids == [(20,)]


def test_upgrade_verification_rejects_a_masked_per_track_downgrade(monkeypatch, tmp_path):
    from qobuz_librarian.modes import process as proc

    backup = tmp_path / "backup"
    backup.mkdir()
    post = tmp_path / "Album"
    post.mkdir()
    original = [{"title": "T1", "length": 200.0,
                 "bits": 24, "sample_rate": 48000, "channels": 2,
                 "discnumber": 1, "tracknumber": 1},
                {"title": "T2", "length": 180.0,
                 "bits": 24, "sample_rate": 48000, "channels": 2,
                 "discnumber": 1, "tracknumber": 2}]
    replacement = [{"title": "T1", "length": 200.0,
                    "bits": 24, "sample_rate": 96000, "channels": 2,
                    "discnumber": 1, "tracknumber": 1},
                   {"title": "T2", "length": 180.0,
                    "bits": 16, "sample_rate": 44100, "channels": 2,
                    "discnumber": 1, "tracknumber": 2}]
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: post)
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else replacement)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is False

    fixed = [{"title": "T1", "length": 200.0,
              "bits": 24, "sample_rate": 96000, "channels": 2,
              "discnumber": 1, "tracknumber": 1},
             {"title": "T2", "length": 180.0,
              "bits": 24, "sample_rate": 48000, "channels": 2,
              "discnumber": 1, "tracknumber": 2}]
    monkeypatch.setattr(proc, "read_album_dir",
                        lambda f, walk_errors=None: original if f == backup else fixed)
    assert proc._upgrade_replacement_verified({"id": "x"}, post, backup) is True

    def verifies(old_tracks, new_tracks):
        old_tracks = [
            {"discnumber": 1, "tracknumber": index, **track}
            for index, track in enumerate(old_tracks, 1)
        ]
        new_tracks = [
            {"discnumber": 1, "tracknumber": index, **track}
            for index, track in enumerate(new_tracks, 1)
        ]
        monkeypatch.setattr(
            proc,
            "read_album_dir",
            lambda f, walk_errors=None: (
                old_tracks if f == backup else new_tracks
            ),
        )
        return proc._upgrade_replacement_verified(
            {"id": "x"}, post, backup)

    stereo = {"channels": 2, "length": 100.0}
    cd = {"bits": 16, "sample_rate": 44100, **stereo}
    hires = {"bits": 24, "sample_rate": 96000, **stereo}
    assert verifies(
        [{"title": "T1", "isrc": "USAAA1234567", **cd}],
        [{"title": "T1", "isrc": "USBBB1234568", **hires}],
    ) is False
    assert verifies(
        [{"title": "T1", "isrc": "US-AAA-12-34567", **cd}],
        [{"title": "T1", "isrc": "USAAA1234567", **hires}],
    ) is True
    # A missing or cut-short track, or hi-res moved to another track.
    assert verifies([{"title": "T1", **cd}, {"title": "T2", **cd}],
                    [{"title": "T1", **cd}]) is False
    assert verifies([{"title": "T1", **cd}], [{"title": "T1", **cd, "length": 20.0}]) is False
    assert verifies([{"title": "A", **cd}, {"title": "B", **hires}],
                    [{"title": "A", **hires}, {"title": "B", **cd}]) is False
