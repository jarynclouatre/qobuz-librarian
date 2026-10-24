import errno
from types import SimpleNamespace

import pytest

from qobuz_librarian.web import jobs as jm


def test_compilations_stay_in_downsample_results_after_a_library_refresh(
        tmp_path, monkeypatch, tagged_flac):
    from mutagen import flac

    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import (
        downsample,
        downsample_state,
        flac_cache,
        library_scan_state,
    )
    from qobuz_librarian.web import flows

    monkeypatch.setattr(downsample, "HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "FLAC_CACHE_ENABLED", True)
    flac_cache._reset_for_tests()
    music = tmp_path / "music"
    track = tagged_flac(music / "Various Artists" / "Compilation" / "01.flac")
    audio = flac.FLAC(track)
    audio.info.sample_rate = 96000
    audio.save()
    original = track.read_bytes()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False)
    for setting in (
        "LIBRARY_SCAN_STATE_FILE", "LIBRARY_GENERATION_STATE_FILE",
        "SCAN_CHECKPOINT_FILE", "NEW_RELEASE_STATE_FILE", "DOWNSAMPLE_STATE_FILE",
        "UNREADABLE_ARTISTS_FILE", "HIDDEN_FILE",
    ):
        monkeypatch.setattr(cfg, setting, tmp_path / f"{setting}.json")

    job = jm.Job(title="Downsample")
    flows.scan_downsamples(job)

    assert not job.error
    assert len(job.candidates) == 1
    assert job.candidates[0]["artist"] == "Various Artists"
    assert job.candidates[0]["selected"] is False
    assert len(downsample_state.visible_candidates()) == 1

    tagged_flac(music / "Artist" / "Album" / "01.flac")
    checked = []

    def scan_artist(artist, *_args, **_kwargs):
        checked.append(artist.name)
        return artist.name, artist.name, [], artist.name, [], {}, set()

    monkeypatch.setattr(flows, "_scan_library_artist", scan_artist)
    # The only cached tags are from an album later moved out by hand, which
    # What's on disk must stop counting once a scan finishes.
    flac_cache.put(music / "Moved" / "Album" / "01.flac",
                   {"bits": 24, "sample_rate": 96000, "size": 1000},
                   sig=(1, 1000))
    assert flac_cache.census()["total_tracks"] == 1
    baseline = jm.Job(title="Library scan")
    try:
        flows.scan_library(baseline, "")
        assert flac_cache.census() is None
    finally:
        flac_cache._reset_for_tests()

    assert not baseline.error
    assert checked == ["Artist"]
    assert library_scan_state.kind_state("missing")["complete"] is True
    assert [c["artist"] for c in downsample_state.visible_candidates()] == ["Various Artists"]
    assert track.read_bytes() == original


def test_a_scan_of_a_new_empty_library_does_not_fail(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)

    job = jm.Job(title="Downsample")
    flows.scan_downsamples(job)

    assert not job.error
    assert job.status != jm.JobStatus.FAILED


@pytest.mark.parametrize("readable_names", [("Readable",), ()])
def test_unreadable_artist_is_reported_and_retried(tmp_path, monkeypatch, readable_names):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import (
        discovery,
        downsample_state,
        generation_state,
        library_scan_state,
        new_releases,
        scan_checkpoint,
        scanner,
        unreadable_artists,
    )
    from qobuz_librarian.web import flows, new_release_checks

    music = tmp_path / "music"
    blocked = music / "Unreadable"
    for name in (blocked.name, *readable_names):
        album = music / name / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False)
    for setting in (
        "LIBRARY_SCAN_STATE_FILE", "LIBRARY_GENERATION_STATE_FILE",
        "SCAN_CHECKPOINT_FILE", "NEW_RELEASE_STATE_FILE", "DOWNSAMPLE_STATE_FILE",
        "UNREADABLE_ARTISTS_FILE",
    ):
        monkeypatch.setattr(cfg, setting, tmp_path / f"{setting}.json")
    scandir = scanner.os.scandir

    def unreadable_scandir(path):
        if path == blocked or path == str(blocked):
            raise PermissionError(errno.EACCES, "Permission denied", str(blocked))
        return scandir(path)

    monkeypatch.setattr(scanner.os, "scandir", unreadable_scandir)
    checked = []

    def scan_artist(artist, *_a, **_k):
        checked.append(artist.name)
        gap = discovery.AlbumGap(
            qobuz_album={"id": artist.name, "title": "Missing album"},
            on_disk_dir=None,
            release_type="album",
        )
        return artist.name, artist.name, [gap], artist.name, [artist.name], {}, set()

    monkeypatch.setattr(flows, "_scan_library_artist", scan_artist)
    monkeypatch.setattr(
        downsample_state, "refresh_for_artists",
        lambda artists, **_k: downsample_state.RefreshResult(
            [], [artist.name for artist in artists], {}, True),
    )
    job = jm.Job(title="baseline")
    flows.scan_library(job, "")

    assert not job.error
    assert checked == list(readable_names)
    assert len(job.candidates) == len(readable_names)
    assert blocked.name in job.summary
    # The readable part of the library finishes; a library with nothing
    # readable does not.
    finished = bool(readable_names)
    assert job.unchecked_artists == (0 if finished else 1)
    assert library_scan_state.kind_state("missing")["complete"] is finished
    assert new_releases.is_baseline_complete() is finished
    assert blocked.name not in new_releases.load()["seen"]
    if finished:
        # The scan read every catalogue, so no new-release check follows it
        # until the interval has passed.
        monkeypatch.setattr(cfg, "NEW_RELEASE_CHECK_INTERVAL", 86400)
        monkeypatch.setattr(new_release_checks, "_new_release_submitted_at", 0.0)
        assert not new_release_checks._new_release_interval_elapsed()
    assert generation_state.load()["latest_attempt"]["status"] == (
        "complete" if finished else "incomplete")
    assert unreadable_artists.load() == ([blocked.name] if finished else [])
    checkpoint = scan_checkpoint.load("missing")
    if finished:
        assert checkpoint is None
    else:
        assert set(checkpoint["scanned"]) == set(readable_names)

    if finished:
        # A new-release check after the scan listed a release the refresh
        # below does not fetch again.
        new_releases.mark_run({"Readable": ["Readable", "Later"]})
    monkeypatch.setattr(scanner.os, "scandir", scandir)
    assert unreadable_artists.readable_again([blocked.name]) == [blocked.name]
    checked.clear()
    flows.scan_library(jm.Job(title="retry"), "")

    assert checked == [blocked.name]
    assert scan_checkpoint.load("missing") is None
    assert unreadable_artists.load() == []
    assert set(library_scan_state.kind_state("missing")["artists"]) == {
        blocked.name, *readable_names,
    }
    assert new_releases.is_baseline_complete()
    assert blocked.name in new_releases.load()["seen"]
    if finished:
        assert "Later" in new_releases.load()["seen"]["Readable"]


def test_artists_cut_at_the_result_cap_are_checked_again(tmp_path, monkeypatch):
    """Switched-off release types filled the result cap, and the artists whose
    albums then found no room were saved as having nothing missing, so no
    refresh ever listed them."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import (
        discovery,
        downsample_state,
        generation_state,
        library_scan_state,
    )
    from qobuz_librarian.web import flows

    music = tmp_path / "music"
    for name in ("Alpha", "Beta"):
        album = music / name / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False)
    for setting in (
        "LIBRARY_SCAN_STATE_FILE", "LIBRARY_GENERATION_STATE_FILE",
        "SCAN_CHECKPOINT_FILE", "NEW_RELEASE_STATE_FILE", "DOWNSAMPLE_STATE_FILE",
        "UNREADABLE_ARTISTS_FILE", "MISSING_TYPES_FILE", "HIDDEN_FILE",
        "SCAN_SEEN_FILE",
    ):
        monkeypatch.setattr(cfg, setting, tmp_path / f"{setting}.json")
    monkeypatch.setattr(jm.Job, "CANDIDATE_CAP", 2)
    monkeypatch.setattr(flows, "as_completed", iter)
    releases = {
        "Alpha": [("a1", "compilation"), ("a2", "compilation"),
                  ("a3", "album"), ("a4", "album")],
        "Beta": [("b1", "album")],
    }
    checked = []

    def scan_artist(artist, *_a, **_k):
        checked.append(artist.name)
        gaps = [
            discovery.AlbumGap(
                qobuz_album={"id": album_id, "title": album_id},
                on_disk_dir=None,
                release_type=release_type,
            )
            for album_id, release_type in releases[artist.name]
        ]
        return artist.name, artist.name, gaps, artist.name, ["x"], {}, set()

    monkeypatch.setattr(flows, "_scan_library_artist", scan_artist)
    monkeypatch.setattr(
        downsample_state, "refresh_for_artists",
        lambda artists, **_k: downsample_state.RefreshResult(
            [], [artist.name for artist in artists], {}, True),
    )
    job = jm.Job(title="baseline")
    flows.scan_library(job, "")

    assert sorted(c["title"] for c in job.candidates) == ["a1", "a2", "a3", "a4"]
    assert job.scan_left_out == {"Beta": 1}
    assert job.unchecked_artists == 0
    assert generation_state.load()["latest_attempt"]["status"] == "complete"
    assert set(library_scan_state.kind_state("missing")["artists"]) == {"Alpha"}

    monkeypatch.setattr(jm.Job, "CANDIDATE_CAP", 10)
    checked.clear()
    job = jm.Job(title="refresh")
    flows.scan_library(job, "")

    assert checked == ["Beta"]
    assert "b1" in {c["title"] for c in job.candidates}


def test_a_scan_stopped_for_a_restart_keeps_its_progress(tmp_path, monkeypatch):
    # Stopping the app winds a scan down the way a cancel does, but a cancel
    # throws its progress away. After a restart the scan has to resume, with
    # the albums it had found listed once, also from an older checkpoint that
    # lists them a second time outside the artists.
    import json
    import shutil
    import threading

    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import (
        discovery,
        downsample_state,
        generation_state,
        scan_checkpoint,
    )
    from qobuz_librarian.web import flows

    music = tmp_path / "music"
    for name in ("One", "Two"):
        album = music / name / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False)
    for setting in (
        "LIBRARY_SCAN_STATE_FILE", "LIBRARY_GENERATION_STATE_FILE",
        "SCAN_CHECKPOINT_FILE", "NEW_RELEASE_STATE_FILE", "DOWNSAMPLE_STATE_FILE",
    ):
        monkeypatch.setattr(cfg, setting, tmp_path / f"{setting}.json")
    job = jm.Job(title="baseline")
    one_found = threading.Event()
    asked = []

    def scan_artist(artist, *_a, **_k):
        asked.append(artist.name)
        if job.title == "baseline" and artist.name == "Two":
            one_found.wait(5)
            job.stopping_for_restart = True
            job.cancel_requested = True
        gap = discovery.AlbumGap(
            qobuz_album={"id": artist.name, "title": "Missing album"},
            on_disk_dir=None,
        )
        return artist.name, artist.name, [gap], artist.name, [artist.name], {}, set()

    def progress(*a, **k):
        jm.Job.push_progress(job, *a, **k)
        if "hit" in k:
            one_found.set()

    monkeypatch.setattr(job, "push_progress", progress)
    monkeypatch.setattr(flows, "_scan_library_artist", scan_artist)
    monkeypatch.setattr(
        downsample_state, "refresh_for_artists",
        lambda artists, **_k: downsample_state.RefreshResult(
            [], [artist.name for artist in artists], {}, True),
    )
    flows.scan_library(job, "")

    assert scan_checkpoint.load("missing") is not None
    assert generation_state.load()["latest_attempt"]["status"] == "running"
    stopped = tmp_path / "stopped"
    shutil.copytree(tmp_path, stopped, ignore=shutil.ignore_patterns("music"))
    checkpoint = json.loads(cfg.SCAN_CHECKPOINT_FILE.read_text())["missing"]
    assert checkpoint["scanned"] == ["One"]
    assert "candidates" not in checkpoint
    old_format = {**checkpoint, "candidates": [
        c for entry in checkpoint["artists"].values() for c in entry["candidates"]]}
    for checkpoint in (None, old_format):
        if checkpoint is not None:
            for kept in stopped.iterdir():
                shutil.copy(kept, tmp_path / kept.name)
            cfg.SCAN_CHECKPOINT_FILE.write_text(json.dumps({"missing": checkpoint}))
        job = jm.Job(title="refresh")
        asked.clear()
        flows.scan_library(job, "")

        assert asked == ["Two"]
        assert sorted(c["artist"] for c in job.candidates) == ["One", "Two"]


@pytest.mark.parametrize("scan_name,kind", [("scan_library", "missing"),
                                          ("scan_repairs", "repair")])
def test_scan_keeps_last_artist_on_api_abort(tmp_path, monkeypatch, scan_name, kind):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artists = [tmp_path / "A", tmp_path / "B"]
    for artist in artists:
        artist.mkdir()
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE", tmp_path / "checkpoint.json")
    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False)
    monkeypatch.setattr(flows.scan_checkpoint.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(flows, "list_library_artists", lambda **_k: artists)
    monkeypatch.setattr(flows, "as_completed", iter)
    monkeypatch.setattr(
        flows.downsample_state, "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult([], ["A", "B"], {}, True),
    )

    def scan_artist(artist, *_a, **_k):
        if artist.name == "B":
            raise flows.QobuzUnavailable("offline")
        if kind == "repair":
            return artist.name, {
                "specs": [], "verified_ok": 1, "unverified": 0, "failed": 0,
                "proof": flows._capture_repair_artist_proof(artist),
            }
        return artist.name, artist.name, [], "artist-id", ["album"], {}, set()

    monkeypatch.setattr(flows, "_scan_library_artist", scan_artist)
    monkeypatch.setattr(flows, "_scan_repair_artist", scan_artist)
    with pytest.raises(flows.QobuzUnavailable):
        getattr(flows, scan_name)(jm.Job(title="scan"), "tok")

    saved = flows.scan_checkpoint.load(kind)
    assert saved["scanned"] == ["A"]
    assert set(saved["artists"]) == {"A"}


def test_a_slow_checkpoint_write_is_not_repeated_after_every_artist(monkeypatch):
    """The interval was timed from the start of a write, so once one took
    longer than the interval a large checkpoint was written after every
    artist."""
    from qobuz_librarian.library import scan_checkpoint

    clock = [0.0]
    writes = []

    def slow_save(*_a, **_k):
        writes.append(clock[0])
        clock[0] += 6.0
        return True

    monkeypatch.setattr(scan_checkpoint.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(scan_checkpoint, "save", slow_save)
    writer = scan_checkpoint.Writer("missing")
    for _ in range(20):
        writer.save(set(), {})
        clock[0] += 1.0

    assert len(writes) == 1


def test_a_waiting_scan_is_shown_without_reading_its_checkpoint(
        tmp_path, monkeypatch):
    """Search and Library read the whole checkpoint of an unfinished scan, a
    hundred megabytes on a large library, on every load."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import scan_checkpoint

    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE", tmp_path / "checkpoint.json")
    scan_checkpoint.save("missing", {"A", "B"}, {})
    monkeypatch.setattr(scan_checkpoint, "_read",
                        lambda: pytest.fail("the checkpoint was read"))

    assert scan_checkpoint.pending() == {"kind": "missing", "done": 2}


def test_dismissed_album_survives_a_refresh_without_a_rescan(
        tmp_path, monkeypatch):
    """Dismissing used to invalidate the whole snapshot, so the next refresh
    re-crawled every artist against Qobuz, and it dropped the dismissed album
    from the saved scan so Restore had nothing to bring back."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(cfg, "SCAN_SEEN_FILE", tmp_path / "scan_seen.json")
    artist_dir = tmp_path / "Artist"
    (artist_dir / "Album").mkdir(parents=True)
    candidates = [
        {
            "kind": "album",
            "title": title,
            "artist": "Artist",
            "detail": "2024 · CD quality · 10 tracks",
            "payload": {"album_id": title, "_artist_dir": "Artist",
                        "release_type": "album"},
            "selected": False,
        }
        for title in ("Kept Album", "Dismissed Album")
    ]
    library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": "same",
                "candidates": candidates,
                "artist_id": "artist-id",
                "catalog_ids": ["Kept Album", "Dismissed Album"],
            },
        },
        complete=True,
        quality_sig=library_scan_state.quality_signature(),
    )
    hidden_mod.hide(hidden_mod.SCOPE_MISSING, [("Artist", "Dismissed Album", "")])

    def fake_refresh(artists, **kwargs):
        return SimpleNamespace(
            complete=True, candidates=[], artists_scanned=[], errors={},
            fingerprints={},
        )

    monkeypatch.setattr(flows, "list_library_artists", lambda **_k: [artist_dir])
    monkeypatch.setattr(flows, "artist_fingerprint", lambda _path: "same",
                        raising=False)
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists", fake_refresh)
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("a dismissal must not force a rescan")),
    )
    job = jm.Job(title="refresh")

    flows.scan_library(job, "tok")

    assert [c["title"] for c in job.candidates] == ["Kept Album"]
    saved = library_scan_state.kind_state("missing")["artists"]["Artist"]
    assert sorted(c["title"] for c in saved["candidates"]) == [
        "Dismissed Album", "Kept Album",
    ]

    # Brought back, it is an album the user has seen, not a new find.
    hidden_mod.restore_all(hidden_mod.SCOPE_MISSING)
    job = jm.Job(title="refresh")
    flows.scan_library(job, "tok")

    assert [(c["title"], bool(c["payload"].get("is_new")))
            for c in job.candidates] == [
        ("Kept Album", False), ("Dismissed Album", False)]


def test_new_release_check_completes_past_a_folder_qobuz_has_no_artist_for(
        tmp_path, monkeypatch):
    # A collaboration folder Beets filed as "Bonobo, Joy Crookes" matches no
    # Qobuz artist, and no later check ever will. Counting it as unchecked left
    # every check Failed and the baseline permanently incomplete.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artists = [tmp_path / name for name in ("Bonobo", "Bonobo, Joy Crookes")]
    for artist in artists:
        artist.mkdir()

    def fake_find(name, **_kwargs):
        if name == "Bonobo, Joy Crookes":
            return SimpleNamespace(artist_id=None, fetch_failed=False,
                                   unresolved=True, current_ids=[],
                                   new_gaps=[], artist_name=None)
        return SimpleNamespace(artist_id="386473", fetch_failed=False,
                               unresolved=False, current_ids=["album"],
                               new_gaps=[], artist_name=name)

    marked = {}
    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda **_k: artists)
    monkeypatch.setattr(flows, "find_new_releases_for_artist", fake_find)
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"386473": ["album"]},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT),
    })
    monkeypatch.setattr(
        flows.new_releases_mod,
        "mark_run",
        lambda _seen, **kwargs: marked.update(kwargs) or True,
    )
    logged = []
    monkeypatch.setattr(flows.log, "info", logged.append)
    job = jm.Job(title="new releases")

    flows.scan_new_releases(job, "tok")

    assert job.status is not jm.JobStatus.FAILED
    assert job.error is None
    assert marked["complete"] is True
    # The log names the folder; the summary stays clear of it, as it would
    # otherwise repeat after every check with nothing to act on.
    assert any("Bonobo, Joy Crookes" in line for line in logged)
    assert "Bonobo, Joy Crookes" not in job.summary
    assert job.unchecked_artists == 0


def test_scan_signature_covers_candidate_shaping_settings(monkeypatch):
    """The cheap refresh reuses saved candidates while the signature matches;
    so every setting that changes WHICH candidates a scan yields has to be in
    it, or Settings changes leave stale gap/missing lists."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import library_scan_state as lss

    base = lss.quality_signature()
    with monkeypatch.context() as mctx:
        mctx.setattr(cfg, "EXCLUDE_LIVE_ALBUMS", not cfg.EXCLUDE_LIVE_ALBUMS)
        assert lss.quality_signature() != base
    with monkeypatch.context() as mctx:
        mctx.setattr(cfg, "MISSING_ALBUMS_MIN_TRACKS", int(cfg.MISSING_ALBUMS_MIN_TRACKS) + 1)
        assert lss.quality_signature() != base


def _parked_new_release_review(*candidates):
    """A new-release list already waiting, with the user's ticks on it."""
    parked = jm.Job(title="New-release check")
    parked.kind = "scan"
    parked.execute_kind = "new_releases"
    parked.status = jm.JobStatus.AWAITING_REVIEW
    parked._execute_fn = lambda _job, _chosen: None
    for title, selected in candidates:
        parked.add_candidate("album", title, "Good", detail="2025",
                             payload={"album_id": title}, selected=selected)
    jm.registry.add(parked)
    return parked


def _stub_new_release_scan(monkeypatch, tmp_path, found_title):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows, job_persistence

    # The suite runs without the job archive, so review saves would otherwise
    # report failure and roll every mutation back. Records what each save would
    # have written, so a test can tell an in-memory change from a saved one.
    saves = []

    def _save(_job, mutate):
        result = mutate()
        saves.append((_job.id, _job.summary, len(_job.candidates)))
        return True, result

    monkeypatch.setattr(job_persistence, "persist_review_mutation", _save)

    good = tmp_path / "Good"
    good.mkdir()

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda **_k: [good])
    monkeypatch.setattr(flows, "find_new_releases_for_artist",
                        lambda _name, **_kwargs: SimpleNamespace(
                            artist_id="artist-id", fetch_failed=False,
                            unresolved=False, current_ids=["old", found_title],
                            new_gaps=[SimpleNamespace(qobuz_album={"id": found_title})],
                            artist_name="Good"))
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"artist-id": ["old"]},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT),
    })
    monkeypatch.setattr(flows.new_releases_mod, "mark_run",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(flows, "_add_gap_candidate",
                        lambda job, *_args, **_kwargs: job.add_candidate(
                            kind="album", title=found_title, artist="Good",
                            detail="2026", payload={"album_id": found_title},
                            selected=False))
    return saves


def test_a_fresh_new_release_check_joins_the_list_already_waiting(
        tmp_path, monkeypatch):
    """Pressing Check new releases with a half-worked list still parked must
    add to that list, not park a rival one beside it: two lists would split the
    user's own picks across two pages and strand the ticks already made."""
    from qobuz_librarian.web import flows

    parked = _parked_new_release_review(("kept", True), ("untouched", False))
    saves = _stub_new_release_scan(monkeypatch, tmp_path, "fresh")
    job = jm.Job(title="New-release check")
    job.execute_kind = "new_releases"

    flows.scan_new_releases(job, "tok")

    # The earlier ticks survive, the new find is there, and it arrives un-ticked
    # like every other new release.
    by_title = {c["title"]: c for c in parked.candidates}
    assert set(by_title) == {"kept", "untouched", "fresh"}
    assert by_title["kept"]["selected"] is True
    assert by_title["untouched"]["selected"] is False
    assert by_title["fresh"]["selected"] is False
    # This run leaves no second review behind, and says where its finds went.
    assert job.candidates == []
    assert job.status is jm.JobStatus.DONE
    assert [j for j in jm.registry.awaiting_review()
            if j.execute_kind == "new_releases"] == [parked]
    # The parked list's own summary can't keep the count it had before the fold,
    # and it has to be SAVED with it: a summary rewritten in memory only came
    # back after a restart still claiming the count from before the fold.
    assert (parked.id, parked.summary, 3) in saves
