"""The shared discovery engine - one answer to "what's missing for this artist"
that both the CLI artist mode and the web scans drive.

These exercise the engine against a real temp library on disk (so folder
resolution, edition matching and track comparison all run for real) with only
the Qobuz API stubbed. They pin the reconciled behaviour the two interfaces
must now agree on; the per-interface tests elsewhere prove each face presents
this same result.
"""
import json
from datetime import date

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.library import catalog as cat
from qobuz_librarian.library import discovery, hidden
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_missing_for_artist,
    find_new_releases_for_artist,
)
from qobuz_librarian.library.scanner import clear_scan_caches

# ── Fixture library + fake Qobuz ────────────────────────────────────────────────

def _qt(title, isrc="", disc=1):
    return {"title": title, "isrc": isrc, "media_number": disc, "track_number": 0}


def _et(title, isrc="", disc=1):
    return {"title": title, "isrc": isrc, "discnumber": disc}


def _album(album_id, title, artist, year, tracks, bd=16, sr=44.1):
    """A full Qobuz album dict (get_album shape), with a track list."""
    return {
        "id": album_id, "title": title,
        "artist": {"name": artist, "id": f"art-{artist}"},
        "release_date_original": f"{year}-01-01",
        "maximum_bit_depth": bd, "maximum_sampling_rate": sr,
        "tracks_count": len(tracks),
        "tracks": {"items": tracks},
    }


def _catalog_entry(album):
    """The lighter shape get_artist_albums returns - no track list."""
    return {k: v for k, v in album.items() if k != "tracks"}


class FakeQobuz:
    """Stands in for the Qobuz search/catalog API. Holds artist search hits,
    one catalog per artist id, and full album dicts keyed by id."""

    def __init__(self, *, artists, catalog, total=None, release_types=None):
        self._artists = artists                       # search_artists results
        self._full = {a["id"]: a for a in catalog}    # get_album by id
        self._catalog = [_catalog_entry(a) for a in catalog]
        self._total = total if total is not None else len(catalog)
        # None stands for lists Qobuz didn't answer.
        self._release_types = release_types

    def search_artists(self, query, token, limit=5):
        return self._artists

    def get_artist_albums(self, artist_id, token, limit=500, fresh=False):
        return list(self._catalog), self._total

    def get_album(self, album_id, token):
        return self._full[album_id]

    def get_artist_release_types(self, artist_id, token, album_ids, fresh=False):
        return self._release_types

    def search_albums(self, query, token, limit=12):
        # Per-folder search fallback: the fixtures keep everything in the
        # catalog, so the fallback returns nothing.
        return []

    def install(self, monkeypatch):
        monkeypatch.setattr(discovery, "search_artists", self.search_artists)
        monkeypatch.setattr(discovery, "get_artist_albums", self.get_artist_albums)
        monkeypatch.setattr(discovery, "get_artist_release_types",
                            self.get_artist_release_types)
        monkeypatch.setattr(discovery, "get_album", self.get_album)
        monkeypatch.setattr(cat, "get_album", self.get_album)
        monkeypatch.setattr(cat, "search_albums", self.search_albums)


def _library(monkeypatch, tmp_path, layout):
    """layout: {artist_folder: {album_folder: [existing_track_dict, ...]}}.

    Creates the dirs (one empty .flac per track so the audio-file count is
    honest) and stubs read_album_dir to return the assigned track dicts.
    """
    track_map = {}
    for artist, albums in layout.items():
        for album, tracks in albums.items():
            d = tmp_path / artist / album
            d.mkdir(parents=True)
            for i in range(len(tracks)):
                (d / f"{i + 1:02d}.flac").write_bytes(b"")
            track_map[str(d)] = tracks
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cat, "read_album_dir", lambda d: track_map.get(str(d), []))
    clear_scan_caches()


@pytest.fixture
def beatles_search():
    return [{"name": "The Beatles", "id": "beatles", "albums_count": 50}]


def _run(monkeypatch, tmp_path, *, layout, catalog, artists,
         query="The Beatles", artist_folder="The Beatles", total=None,
         hidden=None, want_missing=True, prefer_hires=True):
    _library(monkeypatch, tmp_path, layout)
    FakeQobuz(artists=artists, catalog=catalog, total=total).install(monkeypatch)
    artist_dir = (tmp_path / artist_folder) if artist_folder else None
    if artist_dir is not None and not artist_dir.exists():
        artist_dir = None
    result = find_missing_for_artist(
        query, token="tok",
        opts=DiscoveryOpts(prefer_hires=prefer_hires),
        artist_dir=artist_dir, hidden=hidden, want_missing=want_missing)
    clear_scan_caches()
    return result


def _titles(gaps):
    return sorted(g.qobuz_album.get("title") for g in gaps)


# ── Core classification ─────────────────────────────────────────────────────────


def test_fully_missing_album_is_a_gap_with_no_dir(monkeypatch, tmp_path, beatles_search):
    owned = _album("a1", "Revolver", "The Beatles", 1966,
                   [_qt(f"r{i}", f"ISRCR{i}") for i in range(11)])
    missing = _album("a2", "Abbey Road", "The Beatles", 1969,
                     [_qt(f"a{i}", f"ISRCA{i}") for i in range(10)])
    # Its name is close enough to find the Revolver folder, but that folder
    # holds none of its songs.
    namesake = _album("a3", "Evolver", "The Beatles", 1970,
                      [_qt(f"e{i}", f"ISRCE{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Revolver (1966)":
                                       [_et(f"r{i}", f"ISRCR{i}") for i in range(11)]}},
               catalog=[owned, missing, namesake], artists=beatles_search)

    fully = [g for g in res.gaps if g.on_disk_dir is None]
    assert _titles(fully) == ["Abbey Road", "Evolver"]
    # The owned, complete album is not a gap and not unmatched.
    assert "Revolver" not in _titles(res.gaps)
    assert [c["dir"].name for c in res.complete] == ["Revolver (1966)"]
    assert not res.unmatched_dirs
    # A refresh drops a ticked pick the scan found on disk.
    assert res.on_disk == {"a1"}


def test_deluxe_edition_gap_measured_against_the_owned_edition(monkeypatch, tmp_path, beatles_search):
    # The folder is an anniversary edition; the gap must be computed against the
    # edition that folder actually is (14 tracks), not the standard release.
    deluxe = _album("dlx", "Severed Survival", "Autopsy", 1989,
                    [_qt(f"s{i}", f"ISRCS{i}") for i in range(14)])
    res = _run(monkeypatch, tmp_path,
               query="Autopsy", artist_folder="Autopsy",
               layout={"Autopsy": {"Severed Survival (20th Anniversary Edition) (2016)":
                                   [_et(f"s{i}", f"ISRCS{i}") for i in range(8)]}},
               catalog=[deluxe],
               artists=[{"name": "Autopsy", "id": "autopsy", "albums_count": 9}])

    partials = [g for g in res.gaps if g.on_disk_dir is not None]
    assert len(partials) == 1
    assert len(partials[0].missing) == 6
    assert len(partials[0].present) == 8


def test_one_file_folder_matches_its_album_not_a_one_track_live_release(
        monkeypatch, tmp_path):
    # One file fits a one-track release by count alone. The 2024 live single
    # is not the 1973 album the folder names.
    studio = _album("bdb", "Billion Dollar Babies", "Alice Cooper", 1973,
                    [_qt(f"b{i}", f"ISRCB{i}") for i in range(10)])
    live = {**_album("live", "Billion Dollar Babies", "Alice Cooper", 2024,
                     [_qt("Billion Dollar Babies", "ISRCLIVE")]),
            "version": "Live 2024"}
    res = _run(monkeypatch, tmp_path, query="Alice Cooper", artist_folder="Alice Cooper",
               layout={"Alice Cooper": {"Billion Dollar Babies (1973)":
                                        [_et("b5", "ISRCB5")]}},
               catalog=[live, studio],
               artists=[{"name": "Alice Cooper", "id": "alice", "albums_count": 30}])

    assert not res.skipped
    assert [(g.qobuz_album["id"], len(g.missing)) for g in res.gaps] == [("bdb", 9)]


def test_one_file_folder_matches_the_edition_its_name_spells_out(
        monkeypatch, tmp_path):
    # Both editions strip to "Criminal Minded", and one file sits closer to
    # the ten-track one by count. The folder names the deluxe edition.
    deluxe = _album("deluxe", "Criminal Minded (Deluxe)", "BDP", 1987,
                    [_qt(f"d{i}", f"ISRCD{i}") for i in range(25)]
                    + [_qt("South Bronx", "ISRCSB")])
    other = _album("hotclub", "Criminal Minded (Hot Club Version)", "BDP", 1987,
                   [_qt(f"h{i}", f"ISRCH{i}") for i in range(9)]
                   + [_qt("South Bronx", "ISRCSBHC")])
    res = _run(monkeypatch, tmp_path, query="BDP", artist_folder="BDP",
               layout={"BDP": {"Criminal Minded (Deluxe) (1987)":
                               [_et("South Bronx", "ISRCSB")]}},
               catalog=[other, deluxe],
               artists=[{"name": "BDP", "id": "bdp", "albums_count": 2}])

    assert not res.skipped
    assert [(g.qobuz_album["id"], len(g.missing))
            for g in res.partials] == [("deluxe", 25)]


def test_no_gap_fill_for_tracks_another_copy_of_the_album_holds(
        monkeypatch, tmp_path):
    # A one-file copy of a 2011 reissue sits beside the complete 1977 album.
    # Its eleven missing tracks are all on disk in the other folder.
    original = _album("nw77", "News Of The World", "Queen", 1977,
                      [_qt(f"n{i}", f"ISRC77{i}") for i in range(12)])
    reissue = _album("nw11", "News Of The World", "Queen", 2011,
                     [_qt(f"n{i}", f"ISRC11{i}") for i in range(12)])
    res = _run(monkeypatch, tmp_path, query="Queen", artist_folder="Queen",
               layout={"Queen": {
                   "News Of The World (1977)": [_et(f"n{i}", f"ISRC77{i}")
                                                for i in range(12)],
                   "News Of The World (2011)": [_et("n0", "ISRC110")]}},
               catalog=[original, reissue],
               artists=[{"name": "Queen", "id": "queen", "albums_count": 80}])

    assert res.gaps == []
    assert sorted(c["dir"].name for c in res.complete) == [
        "News Of The World (1977)", "News Of The World (2011)"]


def test_saved_artist_pick_moves_to_the_namesake_whose_catalog_holds_the_folders(
        monkeypatch, tmp_path):
    # A pick saved by an older version went to the deepest catalog named The
    # Clash, an unrelated act. The next scan checks it against the folders and
    # moves to the namesake that lists them.
    london = _album("lc", "London Calling", "The Clash", 1979,
                    [_qt(f"l{i}", f"ISRCL{i}") for i in range(19)])
    other = _album("x1", "Bangkok Nights", "The Clash", 2019,
                   [_qt(f"x{i}", f"ISRCX{i}") for i in range(12)])
    catalogs = {"rapper": [_catalog_entry(other)], "band": [_catalog_entry(london)]}

    class Namesakes(FakeQobuz):
        def get_artist_albums(self, artist_id, token, limit=500, fresh=False):
            return list(catalogs[artist_id]), len(catalogs[artist_id])

    _library(monkeypatch, tmp_path,
             {"The Clash": {"London Calling (1979)":
                            [_et(f"l{i}", f"ISRCL{i}") for i in range(17)]}})
    Namesakes(artists=[{"name": "The Clash", "id": "rapper", "albums_count": 111},
                       {"name": "The Clash", "id": "band", "albums_count": 39}],
              catalog=[london, other]).install(monkeypatch)
    cache_file = tmp_path / "resolve.json"
    cache_file.write_text(json.dumps(
        {"version": 1, "entries": {"The Clash": ["rapper", "The Clash"]}}))
    monkeypatch.setattr(cfg, "ARTIST_RESOLVE_CACHE_FILE", cache_file)
    monkeypatch.setattr(discovery, "_resolve_cache", None)
    monkeypatch.setattr(discovery, "_resolve_unchecked", set())

    res = find_missing_for_artist(
        "The Clash", token="tok", opts=DiscoveryOpts(prefer_hires=True),
        artist_dir=tmp_path / "The Clash", hidden={})

    assert res.artist_id == "band"
    assert [(g.qobuz_album["id"], len(g.missing)) for g in res.gaps] == [("lc", 2)]
    discovery.flush_resolve_cache()
    saved = json.loads(cache_file.read_text())
    assert saved["entries"]["The Clash"][0] == "band"
    assert saved["unchecked"] == []
    # Older versions read only version 1 and would drop every pick.
    assert saved["version"] == 1


def test_saved_artist_pick_stays_when_the_search_offers_only_a_lesser_match(
        monkeypatch, tmp_path):
    # An older version saved the band, whose catalog lists one of the two
    # folders. The search no longer returns the band, only a near-name that
    # lists neither.
    catalogs = {"band": [{"title": "White Noise and Black Metal"}],
                "other": [{"title": "Hood Anthems"}]}
    monkeypatch.setattr(discovery, "search_artists", lambda query, token, limit: [
        {"id": "other", "name": "M. Craft", "albums_count": 59}])
    monkeypatch.setattr(discovery, "get_artist_albums",
                        lambda artist_id, token, limit=None: (catalogs[artist_id], None))
    cache_file = tmp_path / "resolve.json"
    cache_file.write_text(json.dumps(
        {"version": 1, "entries": {"Craft": ["band", "Craft"]}}))
    monkeypatch.setattr(cfg, "ARTIST_RESOLVE_CACHE_FILE", cache_file)
    monkeypatch.setattr(discovery, "_resolve_cache", None)
    monkeypatch.setattr(discovery, "_resolve_unchecked", set())
    owned = discovery.owned_album_titles(
        [tmp_path / "White Noise and Black Metal (2018)", tmp_path / "Fuck the Universe (2004)"])

    assert discovery.resolve_artist("Craft", "tok", owned) == ("band", "Craft")


def test_folder_named_for_two_artists_matches_under_the_first_one_listing_it(
        monkeypatch, tmp_path):
    # No Qobuz artist is called "Daisy the Great, Audiotree" and the library
    # has no Daisy the Great folder. That artist's catalog lists the session,
    # so it is matched there and its gap offered; the studio album is not
    # this folder's to list. Galantis lists nothing in its shared folder.
    session = _album("dtg", "Audiotree Live", "Daisy the Great", 2019,
                     [_qt(f"d{i}", f"ISRCD{i}") for i in range(5)])
    studio = _album("dts", "Liar", "Daisy the Great", 2022,
                    [_qt(f"s{i}", f"ISRCS{i}") for i in range(9)])
    church = _album("gal", "Church", "Galantis", 2020,
                    [_qt(f"g{i}", f"ISRCG{i}") for i in range(10)])

    class ByArtist(FakeQobuz):
        def search_artists(self, query, token, limit=5):
            return [a for a in self._artists if a["name"] in query]

        def get_artist_albums(self, artist_id, token, limit=500, fresh=False):
            listed = [a for a in self._catalog if a["artist"]["id"] == artist_id]
            return listed, len(listed)

    _library(monkeypatch, tmp_path, {
        "Daisy the Great, Audiotree": {"Audiotree Live (2019)":
                                       [_et(f"d{i}", f"ISRCD{i}") for i in range(2)]},
        "Galantis, OneRepublic": {"Bones (2019)": [_et("Bones", "ISRCB")]},
    })
    ByArtist(artists=[{"name": "Daisy the Great", "id": "art-Daisy the Great"},
                      {"name": "Galantis", "id": "art-Galantis"}],
             catalog=[session, studio, church]).install(monkeypatch)
    monkeypatch.setattr(discovery, "_resolve_cache", {})
    opts = DiscoveryOpts(prefer_hires=True)

    daisy = find_missing_for_artist(
        "Daisy the Great, Audiotree", token="tok", opts=opts,
        artist_dir=tmp_path / "Daisy the Great, Audiotree", hidden={})
    assert daisy.artist_id == "art-Daisy the Great"
    assert [(g.qobuz_album["id"], len(g.missing), g.on_disk_dir)
            for g in daisy.gaps] == [
        ("dtg", 3, tmp_path / "Daisy the Great, Audiotree" / "Audiotree Live (2019)")]

    shared = find_missing_for_artist(
        "Galantis, OneRepublic", token="tok", opts=opts,
        artist_dir=tmp_path / "Galantis, OneRepublic", hidden={})
    assert shared.artist_id is None
    assert shared.gaps == []


def test_missing_albums_are_what_qobuz_files_as_the_artists_albums(
        monkeypatch, tmp_path):
    # What Qobuz files as the artist's albums lists as albums, except that a
    # two-track one counts as a single and one credited first to another
    # artist as a compilation; a label reissue filed elsewhere is not offered
    # at all. When the lists can't be read the artist keeps every album,
    # untyped, rather than losing them all.
    owned = _album("o1", "Vænir", "Monolord", 2015,
                   [_qt(f"v{i}", f"ISRCV{i}") for i in range(4)])
    short = _album("s1", "It's All The Same", "Monolord", 2023,
                   [_qt("a"), _qt("b")])
    duet = _album("d1", "Split", "Elder", 2019, [_qt(f"d{i}") for i in range(5)])
    live = _album("l1", "Live at Roadburn", "Monolord", 2020,
                  [_qt(f"l{i}") for i in range(7)])
    reissue = _album("r1", "Empress Rising", "Monolord", 2012,
                     [_qt(f"e{i}") for i in range(5)])
    studio = _album("a1", "Rust", "Monolord", 2017, [_qt(f"r{i}") for i in range(5)])
    catalog = [owned, short, duet, live, reissue, studio]
    layout = {"Monolord": {"Vænir (2015)":
                           [_et(f"v{i}", f"ISRCV{i}") for i in range(4)]}}
    artists = [{"name": "Monolord", "id": "monolord", "albums_count": 5}]

    _library(monkeypatch, tmp_path, layout)
    FakeQobuz(artists=artists, catalog=catalog, release_types={
        "o1": "album", "s1": "album", "d1": "album", "l1": "live", "a1": "album",
    }).install(monkeypatch)
    res = find_missing_for_artist("Monolord", token="tok",
                                  artist_dir=tmp_path / "Monolord")
    typed = {g.qobuz_album["title"]: g.release_type for g in res.fully_missing}
    assert typed == {"Rust": "album", "It's All The Same": "epSingle",
                     "Split": "compilation", "Live at Roadburn": "live"}

    FakeQobuz(artists=artists, catalog=catalog).install(monkeypatch)
    res = find_missing_for_artist("Monolord", token="tok",
                                  artist_dir=tmp_path / "Monolord")
    assert {g.qobuz_album["title"]: g.release_type for g in res.fully_missing} == {
        "Live at Roadburn": None, "Empress Rising": None, "Rust": None}


def test_zero_overlap_match_is_skipped_as_false_match(monkeypatch, tmp_path, beatles_search):
    # One file with no track number fits a same-titled one-track single
    # better than the album. The single shares no track with the folder, so
    # it is a false match, not a gap to download over it, and that verdict
    # must not hide the album the file came from.
    single = _album("s1", "Help", "The Beatles", 1965, [_qt("Help!", "ISRCSINGLE")])
    album = _album("a1", "Help", "The Beatles", 1965,
                   [_qt(f"h{i}", f"ISRCH{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Help (1965)": [_et("h3", "ISRCH3")]}},
               catalog=[single, album], artists=beatles_search)

    assert any(s["reason"] == "false_match" for s in res.skipped)
    assert [(g.qobuz_album["id"], len(g.missing)) for g in res.gaps] == [("a1", 9)]
    assert res.gaps[0].on_disk_dir == tmp_path / "The Beatles" / "Help (1965)"
    assert res.on_disk == {"a1"}


def test_gap_fill_offers_the_edition_the_folder_holds_not_a_remaster(
        monkeypatch, tmp_path):
    # One track grabbed from the CD edition. The hi-res remaster, with its own
    # recording IDs, sorts first, but filling from it would put two masters in
    # the folder. With only the remaster listed, nothing is offered.
    def remastered(title, isrc):
        return {**_qt(title, isrc), "version": "2021 Remaster"}

    remaster = {**_album("rm", "Deja Vu", "CSNY", 1970,
                         [remastered(f"d{i}", f"ISRCRM{i}") for i in range(10)],
                         bd=24, sr=192),
                "version": "2021 Remaster"}
    cd = _album("cd", "Deja Vu", "CSNY", 1970,
                [_qt(f"d{i}", f"ISRCCD{i}") for i in range(10)])
    layout = {"CSNY": {"Deja Vu (1970)": [_et("d1", "ISRCCD1")]}}
    artists = [{"name": "CSNY", "id": "csny", "albums_count": 20}]

    res = _run(monkeypatch, tmp_path, query="CSNY", artist_folder="CSNY",
               layout=layout, catalog=[remaster, cd], artists=artists,
               want_missing=False)
    assert [(g.qobuz_album["id"], len(g.missing)) for g in res.gaps] == [("cd", 9)]

    res = _run(monkeypatch, tmp_path / "only", query="CSNY", artist_folder="CSNY",
               layout=layout, catalog=[remaster], artists=artists,
               want_missing=False)
    assert res.gaps == []


def test_remix_album_holding_none_of_the_folders_tracks_is_no_gap(
        monkeypatch, tmp_path):
    # The folder holds the original single, which Qobuz files under the
    # featured artist. The remix collection resolves to the folder and its
    # first remix shares the song's title, but none of its six tracks is the
    # folder's, so it is not a gap, bonus versions or not.
    def remix(name, isrc):
        return {**_qt("Bones", isrc), "version": name}

    one = {**_album("one", "Bones", "Galantis", 2019, [remix("B-Sights Remix", "ISRCB1")]),
           "version": "B-Sights Remix"}
    remixes = {**_album("rmx", "Bones", "Galantis", 2019,
                        [remix("VIP Mix", "ISRCV")]
                        + [remix(f"Remix {i}", f"ISRCR{i}") for i in range(5)]),
               "version": "Remixes"}
    res = _run(monkeypatch, tmp_path, query="Galantis", artist_folder="Galantis",
               layout={"Galantis": {"Bones (2019)": [_et("Bones", "ISRCORIG")]}},
               catalog=[one, remixes],
               artists=[{"name": "Galantis", "id": "galantis", "albums_count": 200}])

    assert res.partials == []


def test_transient_api_error_aborts_the_scan_instead_of_burying_a_folder(
        monkeypatch, tmp_path, beatles_search):
    # A transient Qobuz failure while matching an owned folder must propagate,
    # not collapse into a "no match / nothing missing" verdict - otherwise an
    # outage silently buries albums the next scan would never re-check.
    from qobuz_librarian.api.auth import QobuzUnavailable

    full = _album("a1", "Abbey Road", "The Beatles", 1969,
                  [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])

    class Flaky(FakeQobuz):
        def get_album(self, album_id, token):
            raise QobuzUnavailable("the Qobuz API timed out - try again later")

    _library(monkeypatch, tmp_path,
             {"The Beatles": {"Abbey Road (1969)":
                              [_et(f"t{i}", f"ISRC{i}") for i in range(6)]}})
    Flaky(artists=beatles_search, catalog=[full]).install(monkeypatch)

    with pytest.raises(QobuzUnavailable):
        find_missing_for_artist(
            "The Beatles", token="tok", opts=DiscoveryOpts(prefer_hires=True),
            artist_dir=tmp_path / "The Beatles")


# ── New-release quickscan ─────────────────────────────────────────────────────


def test_new_releases_surface_only_what_appeared_since_the_baseline(
        monkeypatch, tmp_path):
    # resolve_artist hands back an int id (as Qobuz does) but the baseline is
    # persisted as JSON, so it comes back string-keyed - the engine must match
    # the two or it re-baselines forever and never surfaces anything.
    owned = _album(101, "Ocean Eyes", "Billie Eilish", 2016,
                   [_qt(f"o{i}", f"ISRCO{i}") for i in range(4)])
    old = _album(202, "Happier Than Ever", "Billie Eilish", 2021,
                 [_qt(f"h{i}", f"ISRCH{i}") for i in range(16)])
    # A genuinely-new release: dated in the current year so it always stays
    # inside the default recency window, whenever the suite runs.
    fresh = _album(303, "Hit Me Hard And Soft", "Billie Eilish", date.today().year,
                   [_qt(f"s{i}", f"ISRCS{i}") for i in range(10)])
    _library(monkeypatch, tmp_path,
             {"Billie Eilish": {"Ocean Eyes (2016)":
                                [_et(f"o{i}", f"ISRCO{i}") for i in range(4)]}})
    FakeQobuz(artists=[{"name": "Billie Eilish", "id": 2867335}],
              catalog=[owned, old, fresh]).install(monkeypatch)
    monkeypatch.setattr(discovery, "_resolve_cache", {})
    ad = tmp_path / "Billie Eilish"
    opts = DiscoveryOpts(prefer_hires=True)

    first = find_new_releases_for_artist("Billie Eilish", token="tok", opts=opts,
                                         seen_by_id=None, artist_dir=ad)
    assert first.new_gaps == []                       # first check only baselines
    assert set(first.current_ids) == {"101", "202", "303"}

    # A later check that already knew the old catalog (string-keyed, as stored)
    # surfaces only the unowned album that's new since - not the owned one, not
    # the one it had already seen.
    later = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts,
        seen_by_id={str(first.artist_id): ["101", "202"]}, artist_dir=ad)
    assert _titles(later.new_gaps) == ["Hit Me Hard And Soft"]

    caught_up = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts,
        seen_by_id={str(first.artist_id): first.current_ids}, artist_dir=ad)
    assert caught_up.new_gaps == []

    # Recency window: an OLD album that's new to the baseline (Qobuz back-
    # filled it) is a back-catalogue gap, not a new release.
    recency = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts,
        seen_by_id={str(first.artist_id): ["101"]}, artist_dir=ad)
    assert _titles(recency.new_gaps) == ["Hit Me Hard And Soft"]

    # The only folder began as a Track download and has since been filled in.
    # Scans leave its single mark alone, so the check has to see the folder
    # is whole or it skips the artist for good.
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    hidden.mark_single("Billie Eilish", "Ocean Eyes", "2016", 101)
    filled = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts, hidden=hidden.load(),
        single_store=hidden.load(),
        seen_by_id={str(first.artist_id): ["101", "202"]}, artist_dir=ad)
    assert _titles(filled.new_gaps) == ["Hit Me Hard And Soft"]
