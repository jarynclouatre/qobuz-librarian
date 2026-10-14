"""Tests for rip, beets, lyrics, and the seams between them."""

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.integrations.rip import (
    _FLAC_TRUNCATION_FLOOR,
    cleanup_lossy,
    is_flac,
)

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


def test_rip_reaps_a_timed_out_process_and_sorts_what_it_left(monkeypatch, tmp_path):
    """A rip past its timeout is killed and reaped with exit 124. Of what it
    left, verified FLACs are kept while lossy and broken files go."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import rip

    real_popen = subprocess.Popen

    def sleeping_process(_args, **kwargs):
        return real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **kwargs,
        )

    with monkeypatch.context() as mp:
        mp.setattr(rip.subprocess, "Popen", sleeping_process)
        code, _output = rip.rip_url(
            "https://example.invalid/album", timeout=0.05
        )

    assert code == 124

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

    # A track a stop cut off stays with the rest of the stopped download.
    bad.write_bytes(b"\x00" * 200_000)
    with patch("qobuz_librarian.integrations.rip.is_flac", return_value=False):
        assert cleanup_lossy([bad], keep_broken=True) == ([], [], [bad])
    assert bad.exists()


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


def test_lyric_writes_are_atomic_and_stay_inside_the_owned_root(tmp_path, monkeypatch):
    """The live file is never written in place: mutagen saves into a temp copy
    that is then swapped in, so a crash can't truncate it, and hand-typed
    unsynced lyrics are kept. A path outside the owned root, or linked out of
    it, is refused and nothing is written there."""
    from qobuz_librarian.integrations import lyric_fetch

    written = tmp_path / "atomic"
    written.mkdir()
    real = written / "track.flac"
    real.write_bytes(b"original-audio")

    f = _FakeLyricFLAC(real)
    f.tags["UNSYNCEDLYRICS"] = ["hand-typed words"]
    lyric_fetch.write_lyrics(f, "[00:01.00]hello")

    assert f.tags["lyrics"] == ["[00:01.00]hello"]
    assert f.tags["unsyncedlyrics"] == ["hand-typed words"]
    assert f.save_targets and all(t != f.filename for t in f.save_targets)
    assert real.read_bytes() == b"new-audio+tags"
    assert not any(p.name.endswith(".tmp") for p in written.iterdir())

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


def test_lyrics_pass_replaces_only_lyrics_it_wrote(
        tmp_path, monkeypatch, tagged_flac):
    """Lyrics already in a file are left alone, with a synced-only pass
    writing its own sidecar beside them. A recheck fetches the lyrics it
    wrote again, replacing them, removing them when nothing matches, or
    keeping their timing when only their words come back, and removes them
    from instrumentals, backing tracks included. Its own synced sidecar is
    replaced with plain lyrics like the tag."""
    from mutagen import flac

    from qobuz_librarian.integrations import lyric_fetch

    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    track = tmp_path / "Artist" / "Album" / "track.flac"
    tagged_flac(track, title="Song", artist="Artist")
    original = ["", "words typed in by hand"]
    audio = flac.FLAC(track)
    audio["lyrics"] = original
    audio.save()
    calls = []

    def provider(*_args, **_kwargs):
        calls.append(True)
        return "[00:00.50]provider line", "Lrclib", "synced", 1, 0

    with monkeypatch.context() as mp:
        mp.setattr(lyric_fetch, "search_lyrics", provider)

        lyric_fetch.fetch_for_paths(
            [track],
            owned_root=tmp_path,
            state_path=tmp_path / "state.json",
            rescan=True,
            workers=1,
            lyrics_format="embed",
        )

        assert flac.FLAC(track)["lyrics"] == original
        assert not calls

        lyric_fetch.fetch_for_paths(
            [track], owned_root=tmp_path, state_path=tmp_path / "state.json",
            rescan=True, workers=1, lyrics_format="both", synced_only=True,
        )

        assert flac.FLAC(track)["lyrics"] == original
        assert len(calls) == 1
        assert lyric_fetch.classify(track.with_suffix(".lrc").read_text()) == "synced"

    album = tmp_path / "Instrumentals" / "Album"
    fetched = tagged_flac(album / "01.flac", title="La La (Backing Track)",
                          artist="Artist")
    typed = tagged_flac(album / "02.flac", title="Theme (Instrumental)",
                        artist="Artist", lyrics="words typed in by hand")
    monkeypatch.setattr(
        lyric_fetch, "search_lyrics",
        lambda *_args, **_kwargs: ("[00:00.50]vocal take", "Lrclib", "synced", 1, 0))
    options = dict(owned_root=tmp_path, state_path=tmp_path / "recheck-state.json",
                   rescan=True, workers=1, lyrics_format="embed")
    # Written before backing tracks counted as instrumentals.
    detect = lyric_fetch.is_instrumental
    monkeypatch.setattr(lyric_fetch, "is_instrumental", lambda _title: False)
    wrong = tagged_flac(album / "03.flac", title="Song", artist="Artist")
    poem = tagged_flac(album / "04.flac", title="Prayer Meeting", artist="Artist")
    timed = tagged_flac(album / "05.flac", seconds=60, title="Timed", artist="Artist")
    lyric_fetch.fetch_for_paths([fetched, wrong, poem, timed], **options)
    assert flac.FLAC(fetched)["lyrics"] == ["[00:00.50]vocal take"]
    monkeypatch.setattr(lyric_fetch, "is_instrumental", detect)
    monkeypatch.setattr(
        lyric_fetch, "search_lyrics",
        lambda queries, *_args, **_kwargs: {
            "Song Artist": ("[00:00.50]this song", "Lrclib", "synced", 1, 0),
            "Timed Artist": ("vocal take", "NetEase", "plain", 1, 0),
        }.get(queries[0], (None, None, "", 1, 0)))

    counts = lyric_fetch.fetch_for_paths(
        [fetched, typed, wrong, poem, timed], **options)

    assert counts == {"removed-instrumental": 1, "kept-unrecorded": 1,
                      "wrote-synced": 1, "removed-unfit": 1, "already-synced": 1}
    assert "lyrics" not in flac.FLAC(fetched)
    assert flac.FLAC(typed)["lyrics"] == ["words typed in by hand"]
    assert flac.FLAC(wrong)["lyrics"] == ["[00:00.50]this song"]
    assert "lyrics" not in flac.FLAC(poem)
    assert lyric_fetch.WRITTEN_MARK not in flac.FLAC(poem)
    assert flac.FLAC(timed)["lyrics"] == ["[00:00.50]vocal take"]
    assert lyric_fetch.load_state(tmp_path / "recheck-state.json")[str(typed)].status == "plain"

    lyric_fetch.fetch_for_paths([wrong], **{**options, "lyrics_format": "sidecar"})
    monkeypatch.setattr(
        lyric_fetch, "search_lyrics",
        lambda *_args, **_kwargs: ("other words", "NetEase", "plain", 1, 0))
    for _ in range(2):
        counts = lyric_fetch.fetch_for_paths([wrong], **{**options, "lyrics_format": "both"})
        assert wrong.with_suffix(".lrc").read_text() == "other words"
        assert flac.FLAC(wrong)["lyrics"] == ["other words"]
    assert counts == {"already-plain": 1}

    # A tag write that fails puts the sidecar written before it back.
    monkeypatch.setattr(
        lyric_fetch, "search_lyrics",
        lambda *_args, **_kwargs: ("[00:00.50]new words", "Lrclib", "synced", 1, 0))
    with monkeypatch.context() as mp:
        mp.setattr(lyric_fetch, "write_lyrics",
                   lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        counts = lyric_fetch.fetch_for_paths([wrong], **{**options, "lyrics_format": "both"})
    assert counts == {"write-error": 1}
    assert wrong.with_suffix(".lrc").read_text() == "other words"
    counts = lyric_fetch.fetch_for_paths([wrong], **{**options, "lyrics_format": "both"})
    assert counts == {"wrote-synced": 1}

    # The track's mark keeps them the app's without the record, until the
    # words in it are edited.
    monkeypatch.setattr(
        lyric_fetch, "search_lyrics",
        lambda *_args, **_kwargs: ("[00:00.50]found again", "Lrclib", "synced", 1, 0))
    lost = {**options, "state_path": tmp_path / "lost-state.json", "lyrics_format": "both"}
    assert lyric_fetch.fetch_for_paths([wrong], **lost) == {"wrote-synced": 1}
    assert wrong.with_suffix(".lrc").read_text() == "[00:00.50]found again"
    audio = flac.FLAC(wrong)
    audio["lyrics"] = "[00:00.50]found again, and corrected"
    audio.save()
    lost["state_path"] = tmp_path / "lost-again.json"
    assert lyric_fetch.fetch_for_paths([wrong], **lost) == {"kept-unrecorded": 1}
    assert flac.FLAC(wrong)["lyrics"] == ["[00:00.50]found again, and corrected"]


def test_lyrics_an_earlier_version_wrote_without_a_record_are_the_apps(
        tmp_path, monkeypatch, tagged_flac):
    """Lyrics an earlier version wrote into its downloads, under the name it
    always used and with streamrip's numbering intact, are upgraded with no
    record of them when a provider returns their words. Other words there
    may have been added by hand, and stay, as do lyrics another tool wrote,
    in a track retagged since, or with an .lrc beside them."""
    from mutagen import flac

    from qobuz_librarian.integrations import lyric_fetch

    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(
        lyric_fetch, "search_lyrics",
        lambda *_args, **_kwargs: ("[00:00.50]this song", "Lrclib", "synced", 1, 0))
    numbers = {"TRACKNUMBER": "01", "DISCNUMBER": "01", "TRACKTOTAL": "09",
               "DISCTOTAL": "01", "TITLE": "Song", "ARTIST": "Artist"}
    album = tmp_path / "Artist" / "Album"
    earlier = tagged_flac(album / "01.flac", lyrics="This song", **numbers)
    other = tagged_flac(album / "02.flac", LYRICS="[00:00.50]by hand", **numbers)
    retagged = tagged_flac(album / "03.flac", lyrics="[00:00.50]by hand",
                           **{**numbers, "TRACKNUMBER": "3"})
    beside = tagged_flac(album / "04.flac", lyrics="[00:00.50]by hand", **numbers)
    beside.with_suffix(".lrc").write_text("[00:00.50]by hand")
    typed = tagged_flac(album / "05.flac", lyrics="[00:00.50]by hand", **numbers)
    tracks = [earlier, other, retagged, beside, typed]
    options = dict(owned_root=tmp_path, state_path=tmp_path / "state.json", workers=1)

    counts = lyric_fetch.fetch_for_paths(tracks, rescan=True, **options)

    assert counts == {"wrote-synced": 1, "kept-unrecorded": 4}
    assert flac.FLAC(earlier)["lyrics"] == ["[00:00.50]this song"]
    assert all(flac.FLAC(track)["lyrics"] == ["[00:00.50]by hand"]
               for track in tracks[1:])
    with monkeypatch.context() as mp:
        mp.setattr(lyric_fetch, "search_lyrics",
                   lambda *_args, **_kwargs: (None, None, "", 1, 0))
        assert lyric_fetch.fetch_for_paths([typed], rescan=True, **options) == {
            "kept-unrecorded": 1}
    assert flac.FLAC(typed)["lyrics"] == ["[00:00.50]by hand"]

    plain = tagged_flac(tmp_path / "Artist" / "Other" / "01.flac",
                        lyrics="this song", **numbers)
    lyric_fetch.index_existing([plain], owned_root=tmp_path,
                               state_path=options["state_path"], workers=1)
    assert lyric_fetch.fetch_for_paths([plain], **options) == {"wrote-synced": 1}


def test_synced_lyrics_must_fit_the_length_of_this_cut(
        tmp_path, monkeypatch, tagged_flac):
    from types import SimpleNamespace

    from mutagen import flac

    from qobuz_librarian.integrations import lyric_fetch

    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    album = tmp_path / "Humble Pie" / "Performance"
    studio = tagged_flac(album / "01.flac", seconds=227, title="Four Day Creep",
                         artist="Humble Pie", album="Performance")
    jam = tagged_flac(album / "02.flac", seconds=1007, artist="Humble Pie",
                      title="Rollin' Stone (Live At Fillmore East)", album="Performance")
    prayer = tagged_flac(tmp_path / "Charles Mingus" / "Blues & Roots" / "01.flac",
                         seconds=345, title="Wednesday Night Prayer Meeting",
                         artist="Charles Mingus", album="Blues & Roots")
    asked = []
    hits = {
        "Rollin' Stone (Live At Fillmore East) Humble Pie": [{
            "trackName": "Rollin' Stone", "artistName": "Humble Pie",
            "duration": 250,
            "plainLyrics": "Rollin' Stone Lyrics\n[Verse 1: Steve Marriott]\n"
                           "studio words\n(Chorus)\nChorus: Oh Oh Oh\n"
                           "You might also like\n3Embed",
            "syncedLyrics": "[00:20.00]studio take\n[04:00.00]end"}],
        "Wednesday Night Prayer Meeting Charles Mingus": [{
            "trackName": "Intro to Wednesday Night Prayer Meeting",
            "artistName": "Mingus Big Band", "duration": 22,
            "plainLyrics": "a spoken poem",
            "syncedLyrics": "[00:01.00]a spoken poem"}],
    }

    def get(_url, params, timeout):
        asked.append(params)
        if params.get("track_name") == "Four Day Creep":
            return SimpleNamespace(status_code=200, json=lambda: {
                "syncedLyrics": "[00:12.00]exact take\n[03:30.00]end",
                "plainLyrics": "exact take"})
        if "q" in params:
            return SimpleNamespace(status_code=200,
                                   json=lambda: hits.get(params["q"], []))
        return SimpleNamespace(status_code=404, json=lambda: {})

    monkeypatch.setattr(lyric_fetch, "_lrclib_session",
                        lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(lyric_fetch, "syncedlyrics",
                        SimpleNamespace(search=lambda *_args, **_kwargs: None))

    lyric_fetch.fetch_for_paths([studio, jam, prayer], owned_root=tmp_path,
                                state_path=tmp_path / "state.json", workers=1)

    assert asked[0] == {"track_name": "Four Day Creep", "artist_name": "Humble Pie",
                        "album_name": "Performance", "duration": 227}
    assert flac.FLAC(studio)["lyrics"] == ["[00:12.00]exact take\n[03:30.00]end"]
    # Studio timing on a 17-minute jam: the words without it, or the site's
    # heading, label and trailers.
    assert flac.FLAC(jam)["lyrics"] == ["studio words\nChorus: Oh Oh Oh"]
    # A same-named intro by another band is not this recording.
    assert "lyrics" not in flac.FLAC(prayer)


def test_a_recheck_keeps_the_apps_lyrics_while_a_provider_is_disabled(
        tmp_path, monkeypatch, tagged_flac):
    from types import SimpleNamespace

    from mutagen import flac

    from qobuz_librarian.integrations import lyric_fetch

    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(lyric_fetch, "syncedlyrics",
                        SimpleNamespace(search=lambda *_args, **_kwargs: None))
    words = "[00:10.00]timed words\n[03:10.00]end"
    status = [200]
    asked = []

    def answer(params):
        if status[0] == "page":
            raise ValueError("an HTML page")
        return [] if "q" in params else {"syncedLyrics": words}

    def get(_url, params, timeout):
        asked.append(params)
        # Busy on four requests in five, as LRCLIB is under load.
        busy = status[0] == 200 and len(asked) % 5
        return SimpleNamespace(
            status_code=503 if busy else 200 if status[0] == "page" else status[0],
            headers={"Retry-After": "0"} if busy else {},
            json=lambda: answer(params))

    monkeypatch.setattr(lyric_fetch, "_lrclib_session", lambda: SimpleNamespace(get=get))
    tracks = [tagged_flac(tmp_path / "Artist" / "Album" / f"{n}.flac", seconds=200,
                          title=f"Song {n}", artist="Artist", album="Album")
              for n in range(6)]
    options = dict(owned_root=tmp_path, state_path=tmp_path / "state.json",
                   providers=["Lrclib", "NetEase"], workers=1)
    assert lyric_fetch.fetch_for_paths(tracks, **options) == {"wrote-synced": 6}

    # LRCLIB is rate-limited until the circuit breaker disables it, so
    # only NetEase is left to answer for the later tracks. A refusal or a
    # page from something in the way is no answer either.
    for status[0] in (429, 403, "page"):
        counts = lyric_fetch.fetch_for_paths(tracks, rescan=True, **options)

        assert counts == {"providers-unavailable": 6}
        assert all(flac.FLAC(track)["lyrics"] == [words] for track in tracks)

    # NetEase alone finding nothing is no reason to wait a month.
    new = tagged_flac(tmp_path / "Artist" / "Album" / "new.flac", seconds=200,
                      title="New", artist="Artist", album="Album")
    assert lyric_fetch.fetch_for_paths([new], **options) == {"providers-unavailable": 1}

    # Musixmatch refuses inside an HTTP 200 answer.
    refusal = SimpleNamespace(
        status_code=200, content=b"{}", url="musixmatch",
        json=lambda: {"message": {"header": {"status_code": 401}, "body": ""}})

    def refusing(_query, providers, **_kwargs):
        if providers == ["Musixmatch"]:
            lyric_fetch._note_refused_response(refusal)

    monkeypatch.setattr(lyric_fetch, "syncedlyrics", SimpleNamespace(search=refusing))
    status[0] = 404
    counts = lyric_fetch.fetch_for_paths(
        tracks, rescan=True, **{**options, "providers": ["Lrclib", "Musixmatch"]})
    assert counts == {"providers-unavailable": 6}
    assert all(flac.FLAC(track)["lyrics"] == [words] for track in tracks)

    # NetEase asked too often answers busy for a while, and is waited for.
    busy = SimpleNamespace(status_code=200, content=b'{"code":405}',
                           json=lambda: {"code": 405}, url="netease")
    netease = []

    def search(*_args, **_kwargs):
        netease.append(True)
        if len(netease) <= 6:
            lyric_fetch._note_refused_response(busy)
        return words

    monkeypatch.setattr(lyric_fetch, "syncedlyrics", SimpleNamespace(search=search))
    status[0] = 404
    later = [tagged_flac(tmp_path / "Artist" / "Later" / f"{n}.flac", seconds=200,
                         title=f"Later {n}", artist="Artist", album="Later")
             for n in range(6)]
    assert lyric_fetch.fetch_for_paths(later, **options) == {"wrote-synced": 6}

    # While it stays busy it is asked nothing until its pause ends. A stop
    # ends the wait, and a pass that does not wait leaves the tracks for
    # the next one.
    monkeypatch.setattr(lyric_fetch, "PROVIDER_PAUSE", 60.0)
    netease.clear()

    def busy_again(*_args, **_kwargs):
        netease.append(True)
        lyric_fetch._note_refused_response(busy)

    monkeypatch.setattr(lyric_fetch, "syncedlyrics", SimpleNamespace(search=busy_again))
    started = time.monotonic()
    counts = lyric_fetch.fetch_for_paths(
        later, rescan=True, should_stop=lambda: bool(netease), **options)
    assert counts == {"stopped": 1, "stop-total": 6}
    assert time.monotonic() - started < 5
    netease.clear()
    counts = lyric_fetch.fetch_for_paths(later, rescan=True, busy_patience=0, **options)
    assert counts == {"providers-unavailable": 6}
    assert not netease
    assert all(flac.FLAC(track)["lyrics"] == [words] for track in later)


def _import_with_lyric_hook(tagged_flac):
    import shutil

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyrics

    staged = tagged_flac(cfg.STAGING_DIR / ".qobuz-run-1" / "Album" / "01.flac",
                         title="Song", artist="Artist")
    lyrics._run_lyric_hook(staged.parent)
    imported = cfg.MUSIC_ROOT / "Artist" / "Album" / "01 - Song.flac"
    imported.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(staged, imported)
    return imported


def test_lyrics_written_during_import_upgrade_but_others_in_the_album_stay(
        tmp_path, monkeypatch, tagged_flac):
    """Plain lyrics the import wrote still upgrade to synced on the next
    pass. Another file in the album already holding the same words from
    another tool, whose tags the import rewrites, cannot be told apart from
    them, so it never becomes the app's to replace."""
    from mutagen import flac

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyric_fetch, lyrics
    from qobuz_librarian.library import lyrics as library_lyrics

    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)

    def use(root):
        monkeypatch.setattr(cfg, "STAGING_DIR", root / "staging")
        monkeypatch.setattr(cfg, "MUSIC_ROOT", root / "music")
        monkeypatch.setattr(cfg, "LYRIC_FETCH_STATE_FILE", root / "state.json")
        answers = [("plain words", "Lrclib", "plain", 1, 0),
                   ("[00:01.00]synced words", "Lrclib", "synced", 1, 0)]
        monkeypatch.setattr(lyric_fetch, "search_lyrics",
                            lambda *_args, **_kwargs: answers[0])
        return answers

    answers = use(tmp_path / "upgraded")
    imported = _import_with_lyric_hook(tagged_flac)
    audio = flac.FLAC(imported)
    audio["tracknumber"] = "1"
    audio.save()
    lyrics.write_post_import_sidecars([imported.parent])
    lyrics._prune_lyric_state_orphans()
    answers.pop(0)
    library_lyrics.run_library_lyrics()

    assert flac.FLAC(imported)["lyrics"] == ["[00:01.00]synced words"]

    # Renamed with their album folder, they are still the app's to re-check.
    moved = imported.parent.rename(imported.parent.with_name("Album (2001)")) / imported.name
    library_lyrics.run_library_lyrics()
    answers[0] = ("[00:01.00]checked again", "Lrclib", "synced", 1, 0)
    library_lyrics.run_library_lyrics(rescan=True)

    assert flac.FLAC(moved)["lyrics"] == ["[00:01.00]checked again"]

    answers = use(tmp_path / "sibling")
    own = tagged_flac(cfg.MUSIC_ROOT / "Artist" / "Album" / "02 - Song (Live).flac",
                      title="Song (Live)", artist="Artist", tracknumber="2",
                      lyrics="plain words")
    imported = _import_with_lyric_hook(tagged_flac)
    sibling = flac.FLAC(own)
    sibling["replaygain_album_gain"] = "-6.00 dB"
    sibling.save()
    lyrics.write_post_import_sidecars([imported.parent])
    lyrics._prune_lyric_state_orphans()
    answers.pop(0)
    library_lyrics.run_library_lyrics()

    assert flac.FLAC(own)["lyrics"] == ["plain words"]


def test_lyric_sidecars_keep_the_tags_and_permissions_around_them(
        tmp_path, monkeypatch, tagged_flac):
    """Writing a sidecar after import removes only the embedded copy it
    preserved, never other tags, and leaves tracks with lyrics from
    anywhere else and their .lrc alone. A preview leaves an existing
    sidecar's permissions alone, and a real write gives it the track's."""
    from mutagen import flac

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyric_fetch, lyrics

    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "LYRICS_ENABLED", True)
    monkeypatch.setattr(cfg, "LYRICS_FORMAT", "sidecar")
    monkeypatch.setattr(cfg, "LYRIC_FETCH_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(lyrics, "HAVE_LYRIC_FETCH", True)
    words = "[00:01.00]Words kept with the song"
    track = tagged_flac(tmp_path / "Album" / "track.flac", title="Song",
                        lyrics=words, unsyncedlyrics=words, comment="Personal note")
    lyric_fetch.save_state({str(track): lyric_fetch.TrackState(
        written=lyric_fetch._lyrics_digest(words))}, cfg.LYRIC_FETCH_STATE_FILE)
    other = tagged_flac(tmp_path / "Album" / "other.flac", title="Other",
                        lyrics="[00:01.00]from another tool")
    other.with_suffix(".lrc").write_text("[00:01.20]timed by hand\n")

    lyrics.write_post_import_sidecars([track.parent])

    audio = flac.FLAC(track)
    assert track.with_suffix(".lrc").read_text() == words
    assert "lyrics" not in audio and "unsyncedlyrics" not in audio
    assert audio["title"] == ["Song"] and audio["comment"] == ["Personal note"]
    assert other.with_suffix(".lrc").read_text() == "[00:01.20]timed by hand\n"
    assert flac.FLAC(other)["lyrics"] == ["[00:01.00]from another tool"]

    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    preview = tmp_path / "preview"
    track = preview / "track.flac"
    tagged_flac(track, title="Song", artist="Artist")
    track.chmod(0o644)
    sidecar = track.with_suffix(".lrc")
    sidecar.write_text("[00:00.50]line\n")
    sidecar.chmod(0o600)
    options = dict(owned_root=preview, state_path=preview / "state.json",
                   rescan=True, workers=1, lyrics_format="sidecar")

    lyric_fetch.fetch_for_paths([track], dry_run=True, **options)
    assert sidecar.stat().st_mode & 0o777 == 0o600

    lyric_fetch.fetch_for_paths([track], **options)
    assert sidecar.stat().st_mode & 0o777 == 0o644


# ── beets: _beets_direct behaviour ─────────────────────────────────────────


def test_beets_output_is_read_past_the_select_descriptor_limit():
    import resource

    from qobuz_librarian.integrations import beets

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 2048:
        pytest.skip("the hard open-file limit is too low")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 2048), hard))
    held = []
    try:
        while len(held) < 1100:
            held.append(os.open(os.devnull, os.O_RDONLY))
        read_fd, write_fd = os.pipe()
        assert read_fd >= 1024
        os.write(write_fd, b"Tagging:\n    Artist - Album\n")
        os.close(write_fd)
        received = bytearray()
        with os.fdopen(read_fd, "rb", buffering=0) as stream:
            beets._read_cancellable_beets_pipe(
                stream, threading.Event(), received.extend)
        assert received == b"Tagging:\n    Artist - Album\n"
    finally:
        for descriptor in held:
            os.close(descriptor)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


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

    # A partial exit-0 import is accepted and counts the remnant left in staging.
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
    assert leftover.exists()
    assert messages[-1].split()[0] == "1"
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
            "singletons": False,
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
        # Seal, shrink, grow and write, by their Linux values.
        required_seals = 0x1 | 0x2 | 0x4 | 0x8
        get_seals = getattr(fcntl, "F_GET_SEALS", 1034)
        assert fcntl.fcntl(descriptor, get_seals) & required_seals \
            == required_seals
        with pytest.raises(OSError):
            os.pwrite(descriptor, b"x", 0)
    finally:
        os.close(descriptor)


# ── beets: staging tag prep (quarantine, never delete) ────────────────────


@pytest.mark.parametrize(
    ("move_shape", "accepted"),
    [
        ("rename", True),
        ("source-still-linked", False),
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
    assert any(message.lstrip().startswith("⚠") for message in messages)
    assert not any(str(staging / cfg.BEETS_RETRY_DIR) in message for message in messages)

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
    # A disc that already has its own cover is never overwritten.
    (album / "cover.jpg").write_bytes(b"another cover")
    assert relocate_disc_album_artwork(album) is False
    assert (album / "Disc 1" / "cover.jpg").read_bytes() == b"art"
