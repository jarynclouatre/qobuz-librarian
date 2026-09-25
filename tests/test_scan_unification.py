import errno
from types import SimpleNamespace

import pytest

from qobuz_librarian.web import jobs as jm


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
    from qobuz_librarian.web import flows

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
        )
        return artist.name, artist.name, [gap], artist.name, [artist.name], {}

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
    assert generation_state.load()["latest_attempt"]["status"] == (
        "complete" if finished else "incomplete")
    assert unreadable_artists.load() == ([blocked.name] if finished else [])
    checkpoint = scan_checkpoint.load("missing")
    if finished:
        assert checkpoint is None
    else:
        assert set(checkpoint["scanned"]) == set(readable_names)

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


def test_a_scan_stopped_for_a_restart_keeps_its_progress(tmp_path, monkeypatch):
    # Stopping the app winds a scan down the way a cancel does, but a cancel
    # throws its progress away. After a restart the scan has to resume.
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

    def scan_artist(artist, *_a, **_k):
        job.stopping_for_restart = True
        job.cancel_requested = True
        gap = discovery.AlbumGap(
            qobuz_album={"id": artist.name, "title": "Missing album"},
            on_disk_dir=None,
        )
        return artist.name, artist.name, [gap], artist.name, [artist.name], {}

    monkeypatch.setattr(flows, "_scan_library_artist", scan_artist)
    monkeypatch.setattr(
        downsample_state, "refresh_for_artists",
        lambda artists, **_k: downsample_state.RefreshResult(
            [], [artist.name for artist in artists], {}, True),
    )
    flows.scan_library(job, "")

    assert scan_checkpoint.load("missing") is not None
    assert generation_state.load()["latest_attempt"]["status"] == "running"


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
        return artist.name, artist.name, [], "artist-id", ["album"], {}

    monkeypatch.setattr(flows, "_scan_library_artist", scan_artist)
    monkeypatch.setattr(flows, "_scan_repair_artist", scan_artist)
    with pytest.raises(flows.QobuzUnavailable):
        getattr(flows, scan_name)(jm.Job(title="scan"), "tok")

    saved = flows.scan_checkpoint.load(kind)
    assert saved["scanned"] == ["A"]
    assert set(saved["artists"]) == {"A"}


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
    artist_dir = tmp_path / "Artist"
    (artist_dir / "Album").mkdir(parents=True)
    candidates = [
        {
            "kind": "album",
            "title": title,
            "artist": "Artist",
            "detail": "2024 · CD quality · 10 tracks",
            "payload": {"album_id": title, "_artist_dir": "Artist"},
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
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
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
    job = jm.Job(title="new releases")

    flows.scan_new_releases(job, "tok")

    assert job.status is not jm.JobStatus.FAILED
    assert job.error is None
    assert marked["complete"] is True
    # Naming the folder is the only way the user learns why that artist never
    # surfaces, so a silent skip would be its own fault.
    assert "Bonobo, Joy Crookes" in job.summary
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
                            new_gaps=[object()], artist_name="Good"))
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
