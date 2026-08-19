"""Collection restore: what a backup file puts back, and what it refuses to."""
import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.api.search import QobuzError
from qobuz_librarian.library import candidate_premise, scanner
from qobuz_librarian.web import collection_restore as restore
from qobuz_librarian.web import jobs as jm


def _track(title, number, isrc=""):
    return {"title": title, "disc": 1, "number": number, "isrc": isrc}


def _snapshot(artists):
    return {"format": "qobuz-librarian-collection-snapshot", "version": 1,
            "artists": artists}


def _qobuz_album(album_id, title, artist="Bonobo", tracks=8):
    return {"id": album_id, "title": title, "tracks_count": tracks,
            "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1,
            "artist": {"name": artist},
            "tracks": {"items": [{"id": f"t{i}"} for i in range(tracks)]}}


class Library:
    """A real folder tree with stub tag reads, the way the scan tests build one."""

    def __init__(self, root):
        self.root = root
        self.tracks = {}

    def add(self, artist, album, entries=()):
        d = self.root / artist / album
        d.mkdir(parents=True)
        for i in range(max(1, len(entries))):
            (d / f"{i + 1:02d}.flac").write_bytes(b"")
        self.tracks[str(d)] = [
            {"title": e["title"], "tracknumber": e["number"],
             "discnumber": 1, "isrc": e.get("isrc", ""), "album": album}
            for e in entries
        ]
        scanner.clear_scan_caches()


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = Library(tmp_path / "music")
    lib.root.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", lib.root)
    monkeypatch.setattr(scanner, "read_album_dir",
                        lambda d, walk_errors=None: lib.tracks.get(str(d), []))
    return lib


@pytest.fixture
def qobuz(monkeypatch):
    """A stand-in catalogue: what each lookup returns, and what it was asked."""
    state = {"albums": {}, "isrc_tracks": {}, "search": {},
             "album_calls": [], "search_calls": []}

    def get_album(album_id, _token):
        state["album_calls"].append(str(album_id))
        found = state["albums"].get(str(album_id))
        if found is None:
            raise QobuzError("no such album")
        return found

    def by_isrc(isrc, _token):
        return state["isrc_tracks"].get(isrc)

    def search(query, _token, limit=None):
        state["search_calls"].append(query)
        return state["search"].get(query, [])

    monkeypatch.setattr(restore, "get_album", get_album)
    monkeypatch.setattr(restore, "find_qobuz_track_by_isrc", by_isrc)
    monkeypatch.setattr(restore, "search_albums", search)
    return state


def _run(snapshot):
    job = jm.Job(title="Restore from backup")
    job.execute_kind = "collection_restore"
    restore.scan_restore(job, snapshot, "token")
    return job


def test_an_album_still_on_disk_is_not_offered_again(library, qobuz):
    library.add("Bonobo", "Migration", [_track("Migration", 1)])
    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Migration", "qobuz_album_id": "a1",
         "tracks": [_track("Migration", 1)]}]}]))

    assert job.candidates == []
    assert qobuz["album_calls"] == []


def test_a_re_edition_of_the_same_album_counts_as_owned(library, qobuz):
    library.add("Bonobo", "Migration (2017)", [_track("Migration", 1)])
    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Migration", "qobuz_album_id": "a1",
         "tracks": [_track("Migration", 1)]}]}]))

    assert job.candidates == []
    assert qobuz["album_calls"] == []


def test_a_folder_renamed_past_recognition_is_matched_by_its_isrcs(library,
                                                                   qobuz):
    library.add("Bonobo", "unsorted rip 03", [
        _track("Migration", 1, isrc="GBCEL1600123"),
        _track("Break Apart", 2, isrc="GBCEL1600124"),
    ])
    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Migration", "qobuz_album_id": "a1", "tracks": [
            _track("Migration", 1, isrc="GBCEL1600123"),
            _track("Break Apart", 2, isrc="GBCEL1600124"),
            _track("Outlier", 3, isrc="GBCEL1600125"),
        ]}]}]))

    assert job.candidates == []
    assert qobuz["album_calls"] == []


def test_too_few_shared_isrcs_is_not_the_same_album(library, qobuz):
    library.add("Bonobo", "unsorted rip 03", [
        _track("Migration", 1, isrc="GBCEL1600123"),
    ])
    qobuz["albums"]["a1"] = _qobuz_album("a1", "Migration")
    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Migration", "qobuz_album_id": "a1", "tracks": [
            _track("Migration", 1, isrc="GBCEL1600123"),
            _track("Break Apart", 2, isrc="GBCEL1600124"),
            _track("Outlier", 3, isrc="GBCEL1600125"),
            _track("Kerala", 4, isrc="GBCEL1600126"),
        ]}]}]))

    assert [c["title"] for c in job.candidates] == ["Migration"]


def test_a_missing_album_is_fetched_by_its_saved_qobuz_id(library, qobuz):
    library.add("Bonobo", "Black Sands", [_track("Kiara", 1)])
    qobuz["albums"]["a1"] = _qobuz_album("a1", "Migration")
    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Black Sands", "tracks": [_track("Kiara", 1)]},
        {"name": "Migration", "qobuz_album_id": "a1",
         "tracks": [_track("Migration", 1)]}]}]))

    assert [c["title"] for c in job.candidates] == ["Migration"]
    candidate = job.candidates[0]
    assert candidate["selected"] is True
    assert candidate["payload"]["album_id"] == "a1"


def test_a_dead_album_id_falls_back_to_the_isrc_then_the_name(library, qobuz):
    library.add("Bonobo", "Black Sands", [_track("Kiara", 1)])
    qobuz["albums"]["a9"] = _qobuz_album("a9", "Migration")
    qobuz["isrc_tracks"]["GBCEL1600123"] = {"album": {"id": "a9"}}
    qobuz["albums"]["a8"] = _qobuz_album("a8", "Fragments")
    qobuz["search"]["Bonobo Fragments"] = [_qobuz_album("a8", "Fragments")]

    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Migration", "qobuz_album_id": "gone", "tracks": [
            _track("Migration", 1, isrc="GBCEL1600123")]},
        {"name": "Fragments", "tracks": [_track("Rosewood", 1)]}]}]))

    assert sorted(c["title"] for c in job.candidates) == ["Fragments",
                                                          "Migration"]
    assert "Bonobo Fragments" in qobuz["search_calls"]


def test_an_album_nothing_can_match_is_named_in_the_log(library, qobuz):
    library.add("Bonobo", "Black Sands", [_track("Kiara", 1)])
    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Migration", "qobuz_album_id": "gone",
         "tracks": [_track("Migration", 1, isrc="GBCEL1600123")]}]}]))

    assert job.candidates == []
    assert any("Migration" in line and restore.NO_QOBUZ_ALBUM in line
               for line in job.log_lines)


def test_an_artist_with_no_folder_seals_the_music_folder_instead(library,
                                                                 qobuz):
    library.add("Bonobo", "Black Sands", [_track("Kiara", 1)])
    qobuz["albums"]["a1"] = _qobuz_album("a1", "Room 25", artist="Noname")
    job = _run(_snapshot([{"name": "Noname", "albums": [
        {"name": "Room 25", "qobuz_album_id": "a1",
         "tracks": [_track("Self", 1)]}]}]))

    payload = job.candidates[0]["payload"]
    assert "_premise" not in payload
    sealed = payload[candidate_premise.ABSENT_CONTAINER_KEY]
    assert sealed["artist"] == "Noname"
    # The seal is the music folder's own incarnation, so approving it later
    # still refuses an unmounted library.
    assert candidate_premise.validate(job.candidates[0])["kind"] == "missing"


def test_an_unmounted_library_refuses_an_absent_artist_row(library, qobuz,
                                                          monkeypatch):
    qobuz["albums"]["a1"] = _qobuz_album("a1", "Room 25", artist="Noname")
    job = _run(_snapshot([{"name": "Noname", "albums": [
        {"name": "Room 25", "qobuz_album_id": "a1",
         "tracks": [_track("Self", 1)]}]}]))

    monkeypatch.setattr(candidate_premise, "capture_music_root_identity",
                        lambda: None)
    with pytest.raises(candidate_premise.CandidateStale):
        candidate_premise.validate(job.candidates[0])


def test_one_album_is_queued_once_however_often_it_appears(library, qobuz):
    qobuz["albums"]["a1"] = _qobuz_album("a1", "Migration")
    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Migration", "qobuz_album_id": "a1", "tracks": []},
        {"name": "Migration (2017)", "qobuz_album_id": "a1", "tracks": []}]}]))

    assert len(job.candidates) == 1


def test_a_lossy_match_is_not_offered_as_a_restore(library, qobuz):
    lossy = _qobuz_album("a1", "Migration")
    lossy["maximum_bit_depth"] = 0
    qobuz["albums"]["a1"] = lossy
    job = _run(_snapshot([{"name": "Bonobo", "albums": [
        {"name": "Migration", "qobuz_album_id": "a1", "tracks": []}]}]))

    assert job.candidates == []


def test_the_review_records_which_music_folder_it_was_read_against(library,
                                                                   qobuz):
    qobuz["albums"]["a1"] = _qobuz_album("a1", "Room 25", artist="Noname")
    job = _run(_snapshot([{"name": "Noname", "albums": [
        {"name": "Room 25", "qobuz_album_id": "a1",
         "tracks": [_track("Self", 1)]}]}]))

    recorded = job.execute_args.get("music_root")
    assert candidate_premise.music_root_matches(recorded)


def test_music_root_matches_only_the_same_directory_incarnation(library):
    recorded = candidate_premise.capture_music_root_identity()
    assert candidate_premise.music_root_matches(recorded)
    stolen = list(recorded)
    stolen[0] += 1
    assert not candidate_premise.music_root_matches(stolen)
