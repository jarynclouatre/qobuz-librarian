import os
from types import SimpleNamespace

import pytest

from qobuz_librarian import config
from qobuz_librarian.library import catalog
from qobuz_librarian.library.catalog import (
    _is_split_album_merge,
    compute_missing,
    dedup_album_versions,
    filter_owned_albums,
    find_album_dir_filesystem,
    find_extras_in_existing,
)


def test_automatic_multi_artist_migration_is_fail_closed(
        tmp_path, monkeypatch):
    music_root = tmp_path / "music"
    source = music_root / "Artist, Other" / "Album"
    destination = music_root / "Artist" / "Album"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    source_track = source / "01 - Source.flac"
    destination_track = destination / "02 - Existing.flac"
    source_track.write_bytes(b"source")
    destination_track.write_bytes(b"destination")

    monkeypatch.setattr(config, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(
        catalog, "find_album_dir_filesystem", lambda _album: source)
    capture = {"stale": True}

    result = catalog.prompt_and_migrate_multi_artist_folder(
        {"artist": {"name": "Artist"}},
        SimpleNamespace(yes=True),
        ownership_move_out=capture,
    )

    assert result == source
    assert source_track.read_bytes() == b"source"
    assert destination_track.read_bytes() == b"destination"
    assert capture == {}


def test_multi_artist_migration_keeps_a_band_name_whole(tmp_path, monkeypatch):
    music_root = tmp_path / "music"
    source = music_root / "Earth, Wind & Fire, The Emotions" / "Boogie Wonderland"
    source.mkdir(parents=True)
    monkeypatch.setattr(config, "MUSIC_ROOT", music_root)

    assert catalog.multi_artist_migration_destination(
        {"artist": {"name": "Earth, Wind & Fire"}}, source,
    ) == music_root / "Earth, Wind & Fire" / "Boogie Wonderland"


def _qt(title, isrc="", disc=1, **kw):
    return {"title": title, "isrc": isrc, "media_number": disc, **kw}


def _et(title, isrc="", disc=1, **kw):
    from qobuz_librarian.library.tags import normalize
    return {"title": title, "isrc": isrc, "discnumber": disc,
            "normalized": normalize(title), **kw}


def _qalbum(title, year, bd=16, sr=44.1, tc=10):
    return {"title": title, "release_date_original": str(year),
            "maximum_bit_depth": bd, "maximum_sampling_rate": sr,
            "tracks_count": tc}


def test_compute_missing_disc_and_edition_handling():
    qobuz = [_qt("Intro", disc=1), _qt("Theme", disc=1),
             _qt("Intro", disc=2), _qt("Theme", disc=2)]
    owned = [_et("Intro", disc=1), _et("Theme", disc=1),
             _et("Intro", disc=2), _et("Theme", disc=2)]
    assert not compute_missing(qobuz, owned)[0]
    m, _ = compute_missing(qobuz, owned[:2])
    assert sorted(t["media_number"] for t in m) == [2, 2]
    m, p = compute_missing([_qt("Song (2014 Remaster)")], [_et("Song")])
    assert len(p) == 1 and not m
    m, p = compute_missing([_qt("Song")], [_et("Song (Acoustic)")])
    assert len(m) == 1 and not p
    # A rip that tags both discs as disc 1 is still complete.
    qobuz = [_qt("One"), _qt("Two"), _qt("Three", disc=2), _qt("Four", disc=2)]
    m, p = compute_missing(qobuz, [_et(t["title"]) for t in qobuz])
    assert not m and len(p) == 4


def test_non_latin_titles_match_on_text_not_empty_normalization():
    _, p = compute_missing([_qt("東京")], [_et("東京")])
    assert len(p) == 1
    m, p = compute_missing([_qt("東京")], [_et("大阪")])
    assert len(m) == 1 and not p
    extras = find_extras_in_existing([_qt("東京")], [_et("大阪")])
    assert [t["title"] for t in extras] == ["大阪"]


def test_find_extras_flags_bonus_tracks_for_upgrade_safety():
    extras = find_extras_in_existing(
        [_qt("Time")], [_et("Time"), _et("Time (Bonus Track)")])
    assert len(extras) == 1 and "Bonus" in extras[0]["title"]

    extras = find_extras_in_existing(
        [_qt("T1", isrc=" "), _qt("T2", isrc=" ")],
        [_et("Bonus", isrc=" "), _et("T1", isrc=" "), _et("T2", isrc=" ")])
    assert [t["title"] for t in extras] == ["Bonus"]


def test_dedup_album_versions_collapses_editions_but_keeps_distinct_years():
    pairs = [_qalbum("Abbey Road", 1969), _qalbum("Abbey Road (Remaster)", 1969)]
    assert len(dedup_album_versions(pairs)) == 1
    pairs = [_qalbum("American Football", 1999), _qalbum("American Football", 2016)]
    assert len(dedup_album_versions(pairs)) == 2
    cjk = [_qalbum("東京", 2020), _qalbum("大阪", 2021)]
    assert len(dedup_album_versions(cjk)) == 2
    pair = [_qalbum("Album", 2020, bd=16, sr=44.1),
            _qalbum("Album", 2020, bd=24, sr=96)]
    result = dedup_album_versions(pair, prefer_hires=True)
    assert len(result) == 1 and result[0][0]["maximum_bit_depth"] == 24
    # A smaller hi-res "Bonus Content" companion never wins over the album.
    album = _qalbum("The Reminder", 2007, tc=13)
    bonus = {**_qalbum("The Reminder", 2007, bd=24, sr=96, tc=9), "version": "Bonus Content"}
    [(picked, _)] = dedup_album_versions([bonus, album], prefer_hires=True)
    assert picked is album


def test_filter_owned_albums_doesnt_swallow_sequels_or_distinct_years():
    pairs = [({"title": "Reload", "release_date_original": "1997"}, 1),
             ({"title": "Album (Deluxe Edition)", "release_date_original": "2010"}, 1)]
    result = filter_owned_albums(pairs, {"load": [1996], "album": [2010]})
    assert [a["title"] for a, _ in result] == ["Reload"]

    pairs = [({"title": "Revolver", "release_date_original": "2022"}, 1)]
    assert filter_owned_albums(pairs, {"revolver": [1966]}) == []

    pairs = [({"title": "Revolver", "release_date_original": "1966"}, 1)]
    assert filter_owned_albums(pairs, {"revolver": [None]}) == []

    pairs = [({"title": "Wasting Light", "release_date_original": "2011"}, 1)]
    assert [a["title"] for a, _ in
            filter_owned_albums(pairs, {"wastinglightlive": [2019]})] == ["Wasting Light"]



def test_split_album_merge_rules(tmp_path):
    art = tmp_path / "Bonobo"
    (art / "Black Sands").mkdir(parents=True)
    (art / "Black Sands (2010)").mkdir()
    assert _is_split_album_merge(art / "Black Sands", art / "Black Sands (2010)", "Bonobo") is False

    collaborators = tmp_path / "Bonobo, Andreya Triana"
    (collaborators / "Black Sands (2010)").mkdir(parents=True)
    assert _is_split_album_merge(
        collaborators / "Black Sands (2010)",
        art / "Black Sands (2010)",
        "Bonobo",
    ) is True

    (art / "Black Sands (Live)").mkdir()
    assert _is_split_album_merge(art / "Black Sands (Live)", art / "Black Sands (2010)", "Bonobo") is False

    (art / "Live (2010)").mkdir()
    (art / "Live (2011)").mkdir()
    assert _is_split_album_merge(art / "Live (2010)", art / "Live (2011)", "Bonobo") is False



def test_find_album_dir_does_not_match_a_live_release_to_the_studio_folder(tmp_path, monkeypatch):
    from qobuz_librarian.library.scanner import clear_scan_caches
    monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
    missing = {
        "id": "M",
        "artist": {"name": "Absent Artist"},
        "title": "Absent Album",
    }
    assert find_album_dir_filesystem(missing) is None
    (tmp_path / "Bonobo" / "The North Borders (2013)").mkdir(parents=True)
    clear_scan_caches()
    live = {"id": "L", "artist": {"name": "Bonobo"},
            "title": "The North Borders Tour. - Live.",
            "release_date_original": "2014-01-01"}
    assert find_album_dir_filesystem(live) is None
    clear_scan_caches()
    studio = {"id": "S", "artist": {"name": "Bonobo"},
              "title": "The North Borders", "release_date_original": "2013-01-01"}
    assert find_album_dir_filesystem(studio).name == "The North Borders (2013)"
    clear_scan_caches()


def test_folder_holds_all_tracks_matches_identity_not_count(tmp_path):
    from qobuz_librarian.library.catalog import folder_holds_all_tracks

    expected = [{"id": 1, "title": "Alpha"}, {"id": 2, "title": "Beta"}]
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "01 - Alpha.flac").write_bytes(b"x")
    (folder / "09 - Bonus.flac").write_bytes(b"x")
    assert folder_holds_all_tracks(folder, expected) is False

    (folder / "02 - Beta.flac").write_bytes(b"x")
    assert folder_holds_all_tracks(folder, expected) is True

    assert folder_holds_all_tracks(folder, []) is False
    assert folder_holds_all_tracks(tmp_path / "gone", expected) is False


def test_title_fallback_cannot_hand_an_isrc_twin_to_another_track():
    qobuz = [
        {"title": "Song", "media_number": 1, "isrc": "USAAA0000001"},
        {"title": "Song", "media_number": 1, "isrc": "USBBB0000002"},
    ]
    on_disk = [
        {"title": "Song", "discnumber": 1, "isrc": "USAAA0000001"},
        {"title": "Song", "discnumber": 1, "isrc": "USAAA0000001"},
    ]
    missing, present = compute_missing(qobuz, on_disk)
    assert [t["isrc"] for t in missing] == ["USBBB0000002"]
    assert len(present) == 1

    other_edition = [{"title": "Song", "discnumber": 1, "isrc": "GBZZZ9999999"}]
    missing, _ = compute_missing([qobuz[0]], other_edition)
    assert not missing


def test_publishing_a_migration_directory_never_closes_a_reused_number(
        tmp_path, monkeypatch):
    """The FileExistsError reclaim closes the reserved descriptor and then
    re-opens the published name. When that re-open fails, the outer handler
    must not close the same raw number a second time - the kernel hands it
    straight back, so the retry lands on whatever was opened in between."""
    canary_path = tmp_path / "canary"
    canary_path.write_bytes(b"canary")
    parent_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    real_open = catalog._open_migration_directory
    real_reserve = catalog._reserve_migration_directory_at
    seen = {"reserved_fd": None, "canary_fd": None}

    def reserve(parent, *, prefix, mode=0o700):
        name, descriptor = real_reserve(parent, prefix=prefix, mode=mode)
        seen["reserved_fd"] = descriptor
        return name, descriptor

    def open_directory(path, *, dir_fd=None):
        if path != "Album":
            return real_open(path, dir_fd=dir_fd)
        if seen["reserved_fd"] is None:
            raise FileNotFoundError(path)
        seen["canary_fd"] = os.open(str(canary_path), os.O_RDONLY)
        raise NotADirectoryError(path)

    monkeypatch.setattr(catalog, "_reserve_migration_directory_at", reserve)
    monkeypatch.setattr(catalog, "_open_migration_directory", open_directory)
    monkeypatch.setattr(
        catalog, "_rename_noreplace_at",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileExistsError("Album")))

    try:
        with pytest.raises(NotADirectoryError):
            catalog._open_or_publish_migration_directory_at(parent_fd, "Album")
        # Vacuous unless the kernel really recycled the number.
        assert seen["canary_fd"] == seen["reserved_fd"]
        assert os.fstat(seen["canary_fd"]).st_size == len(b"canary")
    finally:
        for descriptor in (seen["canary_fd"], parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
