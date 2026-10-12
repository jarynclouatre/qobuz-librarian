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
from qobuz_librarian.library import discovery
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

    def __init__(self, *, artists, catalog, total=None):
        self._artists = artists                       # search_artists results
        self._full = {a["id"]: a for a in catalog}    # get_album by id
        self._catalog = [_catalog_entry(a) for a in catalog]
        self._total = total if total is not None else len(catalog)

    def search_artists(self, query, token, limit=5):
        return self._artists

    def get_artist_albums(self, artist_id, token, limit=500, fresh=False):
        return list(self._catalog), self._total

    def get_album(self, album_id, token):
        return self._full[album_id]

    def search_albums(self, query, token, limit=12):
        # Per-folder search fallback: the fixtures keep everything in the
        # catalog, so the fallback returns nothing.
        return []

    def install(self, monkeypatch):
        monkeypatch.setattr(discovery, "search_artists", self.search_artists)
        monkeypatch.setattr(discovery, "get_artist_albums", self.get_artist_albums)
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
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Revolver (1966)":
                                       [_et(f"r{i}", f"ISRCR{i}") for i in range(11)]}},
               catalog=[owned, missing], artists=beatles_search)

    fully = [g for g in res.gaps if g.on_disk_dir is None]
    assert _titles(fully) == ["Abbey Road"]
    # The owned, complete album is not a gap and not unmatched.
    assert "Revolver" not in _titles(res.gaps)
    assert [c["dir"].name for c in res.complete] == ["Revolver (1966)"]
    assert not res.unmatched_dirs


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
