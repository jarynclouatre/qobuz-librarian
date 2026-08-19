"""Collection snapshot: what gets recorded, and what refuses to overwrite it."""
import json
import shutil

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.library import collection_snapshot as snap
from qobuz_librarian.library import scanner
from qobuz_librarian.library.scanner import clear_scan_caches


def _track(title, number, isrc="", album="", disc=1):
    return {"title": title, "tracknumber": number, "discnumber": disc,
            "isrc": isrc, "mb_trackid": "", "album": album}


class Library:
    """A real folder tree with stub tag reads, the way the scan tests build one."""

    def __init__(self, root):
        self.root = root
        self.tracks = {}

    def add(self, artist, album, entries):
        d = self.root / artist / album
        d.mkdir(parents=True)
        for i in range(len(entries)):
            (d / f"{i + 1:02d}.flac").write_bytes(b"")
        self.tracks[str(d)] = entries
        clear_scan_caches()

    def remove(self, artist, album):
        d = self.root / artist / album
        shutil.rmtree(d)
        self.tracks.pop(str(d), None)
        clear_scan_caches()


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = Library(tmp_path / "music")
    lib.root.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", lib.root)
    monkeypatch.setattr(cfg, "COLLECTION_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(scanner, "read_album_dir",
                        lambda d, walk_errors=None: lib.tracks.get(str(d), []))
    return lib


def test_snapshot_records_folders_titles_and_isrcs(library):
    library.add("Bonobo", "Migration", [
        _track("Migration", 1, isrc="GBCEL1600123", album="Migration"),
        _track("Break Apart", 2, isrc="GBCEL1600124", album="Migration"),
    ])
    library.add("Bonobo", "Black Sands (2010)", [
        _track("Kiara", 1, album="Black Sands"),
    ])

    doc = snap.build_snapshot(source="manual")

    assert doc["counts"] == {"artists": 1, "albums": 2, "tracks": 3}
    artist = doc["artists"][0]
    assert artist["name"] == "Bonobo"
    names = [a["name"] for a in artist["albums"]]
    assert names == ["Black Sands (2010)", "Migration"]
    black_sands = artist["albums"][0]
    # The tagged title is only carried when the folder name isn't already it.
    assert black_sands["title"] == "Black Sands"
    assert "title" not in artist["albums"][1]
    assert [t["isrc"] for t in artist["albums"][1]["tracks"]] == [
        "GBCEL1600123", "GBCEL1600124"]


def test_ids_from_the_scan_win_and_unscanned_artists_keep_theirs(library):
    library.add("Bonobo", "Migration", [_track("Migration", 1)])
    library.add("Four Tet", "Sixteen Oceans", [_track("School", 1)])
    previous = {
        "artists": [
            {"name": "Bonobo", "qobuz_artist_id": "old-bonobo",
             "albums": [{"name": "Migration", "qobuz_album_id": "old-mig"}]},
            {"name": "Four Tet", "qobuz_artist_id": "ft",
             "albums": [{"name": "Sixteen Oceans", "qobuz_album_id": "so"}]},
        ]
    }

    # A scan only reports the artists whose folders changed. Four Tet isn't in
    # this run's results, so its ids have to survive from the last snapshot or
    # a restore loses the exact album and has to guess from a search.
    doc = snap.build_snapshot(
        owned_qobuz={"Bonobo": {"Migration": "new-mig"}},
        artist_ids={"Bonobo": "new-bonobo"},
        previous=previous)

    by_name = {a["name"]: a for a in doc["artists"]}
    assert by_name["Bonobo"]["qobuz_artist_id"] == "new-bonobo"
    assert by_name["Bonobo"]["albums"][0]["qobuz_album_id"] == "new-mig"
    assert by_name["Four Tet"]["qobuz_artist_id"] == "ft"
    assert by_name["Four Tet"]["albums"][0]["qobuz_album_id"] == "so"


def test_an_empty_music_folder_leaves_the_saved_snapshot_alone(library):
    library.add("Bonobo", "Migration", [_track("Migration", 1)])
    good = snap.build_snapshot()
    assert snap.write_snapshot(good)[0] is True
    kept = snap.latest_path().read_bytes()

    # An unmounted drive looks exactly like a collection somebody deleted.
    library.remove("Bonobo", "Migration")
    ok, reason = snap.write_snapshot(snap.build_snapshot(previous=good))
    assert ok is False and reason
    assert snap.latest_path().read_bytes() == kept


def test_a_big_shrink_is_held_back_until_it_is_forced(library):
    for i in range(20):
        library.add("Bonobo", f"Album {i:02d}", [_track("t", 1)])
    big = snap.build_snapshot()
    assert snap.write_snapshot(big)[0] is True
    kept = snap.latest_path().read_bytes()

    for i in range(15):
        library.remove("Bonobo", f"Album {i:02d}")
    shrunk = snap.build_snapshot()

    ok, reason = snap.write_snapshot(shrunk)
    assert ok is False
    assert "20" in reason and "5" in reason
    assert snap.latest_path().read_bytes() == kept
    assert snap.suspect_path().exists()

    ok, reason = snap.write_snapshot(shrunk, force=True)
    assert ok is True and reason is None
    assert json.loads(snap.latest_path().read_text())["counts"]["albums"] == 5
    # Forcing settles the question, so the suspect copy stops nagging.
    assert not snap.suspect_path().exists()


def test_an_ordinary_tidy_up_still_writes(library):
    for i in range(12):
        library.add("Bonobo", f"Album {i:02d}", [_track("t", 1)])
    assert snap.write_snapshot(snap.build_snapshot())[0] is True

    library.remove("Bonobo", "Album 00")
    ok, reason = snap.write_snapshot(snap.build_snapshot())
    assert ok is True and reason is None


def test_only_the_newest_dated_copies_are_kept(library):
    library.add("Bonobo", "Migration", [_track("Migration", 1)])
    doc = snap.build_snapshot()
    for _ in range(snap.KEEP_DATED + 3):
        assert snap.write_snapshot(doc)[0] is True

    dated = list(snap.snapshot_dir().glob("collection-2*.json"))
    assert len(dated) == snap.KEEP_DATED


def test_validate_upload_rejects_files_that_are_not_ours():
    good = {"format": snap.FORMAT, "version": snap.VERSION,
            "artists": [{"name": "Bonobo", "albums": []}]}
    assert snap.validate_upload(good) == (True, None)
    assert snap.validate_upload({"hello": "world"})[0] is False
    assert snap.validate_upload({**good, "version": 99})[0] is False
    assert snap.validate_upload({**good, "artists": []})[0] is False


def test_an_artist_id_every_past_scan_resolved_still_reaches_the_backup(
        library, monkeypatch):
    # An ordinary scan skips unchanged folders and reports no ids for them,
    # so the builder falls back to the on-disk resolution cache.
    library.add("Bonobo", "Black Sands (2010)",
                [{"title": "Prelude", "number": 1}])
    monkeypatch.setattr(snap.discovery, "cached_artist_resolutions",
                        lambda: {"Bonobo": ["7619", "Bonobo"]})
    document = snap.build_snapshot()
    assert document["artists"][0]["qobuz_artist_id"] == "7619"

    # Ids handed over by the scan itself still win.
    fresh = snap.build_snapshot(artist_ids={"Bonobo": "1"})
    assert fresh["artists"][0]["qobuz_artist_id"] == "1"
