import json
from pathlib import Path
from types import SimpleNamespace

from qobuz_librarian.library.downsample import DownsampleCandidate
from qobuz_librarian.web import jobs as jm


def _candidate(album_dir: Path):
    return DownsampleCandidate(
        album_dir=album_dir,
        artist="Artist",
        title="Album (2024)",
        n_hires=1,
        n_flac=1,
        source_rates=[96000],
        target_rates=[48000],
        est_saving=2048,
    )


def test_downsample_scan_reports_incomplete_shared_refresh(tmp_path, monkeypatch):
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    denied_dir = tmp_path / "Denied"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    denied_dir.mkdir()
    calls = []

    def fake_refresh(artists, **kwargs):
        artist_list = list(artists)
        calls.append([a.name for a in artist_list])
        kwargs["on_artist"](artist_list[0], [_candidate(album_dir)], None, 1, 2)
        kwargs["on_artist"](
            artist_list[1], [], PermissionError("no access"), 2, 2)
        return downsample_state.RefreshResult(
            candidates=[_candidate(album_dir)],
            artists_scanned=["Artist", "Denied"],
            errors={"Denied": "no access"},
            complete=False,
        )

    monkeypatch.setattr(
        flows, "list_library_artists", lambda: [artist_dir, denied_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_refresh)
    job = jm.Job(title="downsample")

    flows.scan_downsamples(job)

    assert calls == [["Artist", "Denied"]]
    assert len(job.candidates) == 1
    assert job.candidates[0]["kind"] == "downsample"
    assert job.summary.startswith("1 album stored above CD rate")
    assert "1 artist couldn't be checked" in job.summary
    assert job.execute_args["_unchecked_artists"] == 1

    def all_error_refresh(artists, **kwargs):
        artist_list = list(artists)
        for index, artist in enumerate(artist_list, 1):
            kwargs["on_artist"](
                artist, [], PermissionError("no access"), index, 2)
        return downsample_state.RefreshResult(
            candidates=[],
            artists_scanned=["Artist", "Denied"],
            errors={"Artist": "no access", "Denied": "no access"},
            complete=False,
        )

    monkeypatch.setattr(
        flows.downsample_state, "refresh_for_artists", all_error_refresh)
    failed = jm.Job(title="downsample")

    flows.scan_downsamples(failed)

    assert failed.status == jm.JobStatus.FAILED
    assert failed.error == "The Downsample scan did not complete."
    assert "2 artists couldn't be checked" in failed.summary
    assert "every album is already at CD rate" not in failed.summary


def test_downsample_scan_rechecks_hidden_before_adding_active_candidates(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, hidden
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album (2024)"
    album_dir.mkdir(parents=True)
    candidate = _candidate(album_dir)
    badge_calls = []

    def fake_refresh(artists, **kwargs):
        artist_list = list(artists)
        hidden.hide(hidden.SCOPE_DOWNSAMPLE, [("Artist", "Album (2024)", "")])
        kwargs["on_artist"](artist_list[0], [candidate], None, 1, 1)
        return downsample_state.RefreshResult([candidate], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_refresh)
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *args: badge_calls.append(args))
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    job = jm.Job(title="downsample")

    flows.scan_downsamples(job)

    assert job.candidates == []
    assert badge_calls == [("downsample", False)]


def test_baseline_scan_refreshes_shared_downsample_state(tmp_path, monkeypatch):
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    calls = []

    def fake_refresh(artists, **kwargs):
        calls.append([a.name for a in artists])
        return downsample_state.RefreshResult([], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_refresh)
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
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", []),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert calls == [["Artist"]]


def test_upgrade_scan_uses_shared_refresh_state(tmp_path, monkeypatch):
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    calls = []
    spec = {
        "title": "Album",
        "artist": "Artist",
        "detail": "16-bit/44.1kHz -> 24-bit/96kHz",
        "payload": {"album_id": "up1", "year": "2024", "cover": ""},
    }

    def fake_refresh(artists, **kwargs):
        artist_list = list(artists)
        calls.append([a.name for a in artist_list])
        kwargs["on_artist"](artist_list[0], [spec], None, 1, 1)
        return upgrade_state.RefreshResult([spec], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists", fake_refresh)
    job = jm.Job(title="upgrade")

    flows.scan_upgrades(job, "tok")

    assert calls == [["Artist"]]
    assert len(job.candidates) == 1
    assert job.candidates[0]["kind"] == "upgrade"


def test_upgrade_scan_rechecks_hidden_before_adding_active_candidates(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    spec = {
        "title": "Album",
        "artist": "Artist",
        "detail": "16-bit/44.1kHz -> 24-bit/96kHz",
        "payload": {"album_id": "up1", "year": "2024", "cover": ""},
    }
    badge_calls = []

    def fake_refresh(artists, **kwargs):
        artist_list = list(artists)
        hidden.hide(hidden.SCOPE_UPGRADE, [("Artist", "Album", "2024")])
        kwargs["on_artist"](artist_list[0], [spec], None, 1, 1)
        return upgrade_state.RefreshResult([spec], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists", fake_refresh)
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *args: badge_calls.append(args))
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    job = jm.Job(title="upgrade")

    flows.scan_upgrades(job, "tok")

    assert job.candidates == []
    assert badge_calls == [("upgrade", False)]


def test_execute_downsamples_refreshes_affected_artist_state(tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    refreshed = []

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: {"resampled": 1, "saved_bytes": 100, "errors": 0})
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda artist_dir, **kwargs: refreshed.append(artist_dir.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_dir": str(album_dir)},
    }])

    assert refreshed == ["Artist"]


def test_cancelled_downsample_summary_counts_the_interrupted_album(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    albums = [tmp_path / "Artist" / name for name in ("One", "Two")]
    for album in albums:
        album.mkdir(parents=True)
    job = jm.Job(title="downsample")
    calls = 0

    def fake_downsample(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"resampled": 1, "saved_bytes": 100, "errors": 0}
        job.cancel_requested = True
        return {"resampled": 1, "saved_bytes": 50, "errors": 0,
                "cancelled": True}

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        fake_downsample)
    monkeypatch.setattr(
        "qobuz_librarian.quality.decision.mark_local_album_capped",
        lambda *_args: None)
    monkeypatch.setattr(flows, "_refresh_downsample_artist_state",
                        lambda *_args: None)

    flows.execute_downsamples(job, [
        {"artist": "Artist", "title": album.name,
         "payload": {"album_dir": str(album)}}
        for album in albums
    ])

    assert "Downsampled 1 album (100B smaller)" in job.summary
    assert (
        "1 album interrupted (50B smaller so far; remaining files left unchanged)"
        in job.summary
    )
    assert "0 albums not started" in job.summary


def test_downsample_summary_distinguishes_each_skipped_album(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    unchanged_dir = artist_dir / "Unchanged"
    unchanged_dir.mkdir(parents=True)
    missing_dir = artist_dir / "Missing"

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *_a, **_k: {"resampled": 0, "saved_bytes": 0, "errors": 0},
    )
    monkeypatch.setattr(flows, "_refresh_downsample_artist_state",
                        lambda *_args: None)
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [
        {"artist": "Artist", "title": "No details", "payload": {}},
        {"artist": "Artist", "title": "Missing",
         "payload": {"album_dir": str(missing_dir)}},
        {"artist": "Artist", "title": "Unchanged",
         "payload": {"album_dir": str(unchanged_dir)}},
    ])

    assert "1 album skipped (saved folder details missing)" in job.summary
    assert "1 album skipped (no longer on disk)" in job.summary
    assert "1 album skipped (nothing needed changing)" in job.summary


def test_targeted_upgrade_refresh_respects_upgrade_scan_disabled(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(
        flows.upgrade_state,
        "update_artist",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("disabled upgrade scan should not refresh")),
    )
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)

    flows._refresh_after_local_album_change(
        {"artist": {"name": "Artist"}, "title": "Album"},
        {"dir": album_dir},
        token="tok",
        upgrade=True,
    )


def test_execute_upgrades_refreshes_upgrade_and_downsample_state(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    refreshed_upgrade = []
    refreshed_downsample = []
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    monkeypatch.setattr(flows, "get_album",
                        lambda album_id, token: {"id": album_id, "title": "Album"})
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": True, "result": "downloaded",
                                         "dir": album_dir})
    monkeypatch.setattr("qobuz_librarian.library.catalog.find_existing_tracks",
                        lambda album: ([], None))
    monkeypatch.setattr(flows.upgrade_state, "update_artist",
                        lambda artist_dir, **kwargs: refreshed_upgrade.append(artist_dir.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda artist_dir, **kwargs: refreshed_downsample.append(artist_dir.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda *_: None)
    stale_job = jm.Job(title="upgrade")
    stale_job.execute_args = {"quality_signature": "old-policy"}

    flows.execute_upgrades(stale_job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_id": "alb-1"},
    }], "tok")

    assert stale_job.status == jm.JobStatus.FAILED
    assert "before any albums were changed" in stale_job.summary
    assert refreshed_upgrade == []
    assert refreshed_downsample == []

    job = jm.Job(title="upgrade")
    job.execute_args = {
        "quality_signature": flows.upgrade_state.quality_signature(),
    }

    flows.execute_upgrades(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_id": "alb-1"},
    }], "tok")

    assert refreshed_upgrade == ["Artist"]
    assert refreshed_downsample == ["Artist"]


def test_execute_upgrades_marks_partial_cap_before_refresh(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import decision
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 192,
    }
    refreshed_upgrade = []

    monkeypatch.setattr(flows, "get_album", lambda album_id, token: album)
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": True, "result": "downloaded",
                                         "dir": album_dir,
                                         "quality_verdict": {
                                             "under": True,
                                             "recovered": False,
                                             "n_below": 1,
                                         }})
    monkeypatch.setattr("qobuz_librarian.library.catalog.find_existing_tracks",
                        lambda _album: ([object()], None))
    monkeypatch.setattr(
        "qobuz_librarian.quality.decision.compare_album_quality",
        lambda *_a, **_k: {
            "classification": "mixed_below",
            "n_at": 0,
            "n_below": 1,
            "n_above": 0,
        },
    )

    def refresh_upgrade(_artist_dir, **_kwargs):
        assert decision.is_album_capped("alb-1", decision.load_capped())
        refreshed_upgrade.append(_artist_dir.name)
        return SimpleNamespace(complete=True, candidates=[])

    monkeypatch.setattr(flows.upgrade_state, "update_artist", refresh_upgrade)
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda _artist_dir, **_kwargs:
                        SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda *_: None)
    job = jm.Job(title="upgrade")
    job.execute_args = {
        "quality_signature": flows.upgrade_state.quality_signature(),
    }

    flows.execute_upgrades(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_id": "alb-1"},
    }], "tok")

    assert refreshed_upgrade == ["Artist"]


def test_execute_upgrades_does_not_mark_cap_when_staging_verdict_passed(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import decision
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "CAPPED_FILE", tmp_path / "capped.json")
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    album = {
        "id": "alb-1",
        "title": "Album",
        "artist": {"name": "Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 192,
    }

    monkeypatch.setattr(flows, "get_album", lambda album_id, token: album)
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": True, "result": "downloaded",
                                         "dir": album_dir,
                                         "quality_verdict": {
                                             "under": False,
                                             "recovered": False,
                                             "n_below": 0,
                                         }})
    monkeypatch.setattr("qobuz_librarian.library.catalog.find_existing_tracks",
                        lambda _album: ([object()], None))
    monkeypatch.setattr(
        "qobuz_librarian.quality.decision.compare_album_quality",
        lambda *_a, **_k: {
            "classification": "mixed_below",
            "n_at": 0,
            "n_below": 1,
            "n_above": 0,
        },
    )
    monkeypatch.setattr(flows.upgrade_state, "update_artist",
                        lambda _artist_dir, **_kwargs:
                        SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda _artist_dir, **_kwargs:
                        SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    monkeypatch.setattr(flows.time, "sleep", lambda *_: None)
    job = jm.Job(title="upgrade")
    job.execute_args = {
        "quality_signature": flows.upgrade_state.quality_signature(),
    }

    flows.execute_upgrades(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_id": "alb-1"},
    }], "tok")

    assert not decision.is_album_capped("alb-1", decision.load_capped())


def test_baseline_scan_refreshes_shared_upgrade_state(tmp_path, monkeypatch):
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    calls = []

    def fake_downsample_refresh(artists, **kwargs):
        return SimpleNamespace(
            complete=True,
            candidates=[],
            artists_scanned=[],
            errors={},
            fingerprints={},
            hidden_signature="",
        )

    def fake_upgrade_refresh(artists, **kwargs):
        calls.append([a.name for a in artists])
        return upgrade_state.RefreshResult([], ["Artist"], {}, True)

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists", fake_upgrade_refresh)
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
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", []),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert calls == [["Artist"]]


def test_cancelled_baseline_scan_does_not_publish_quality_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    downsample_candidate = _candidate(album_dir)
    upgrade_candidate = {
        "title": "Album",
        "artist": "Artist",
        "detail": "16-bit/44.1kHz -> 24-bit/96kHz",
        "payload": {"album_id": "up1", "year": "2024", "cover": ""},
    }
    badge_calls = []

    def fake_downsample_refresh(_artists, **kwargs):
        result = downsample_state.RefreshResult(
            [downsample_candidate], ["Artist"], {}, True)
        if kwargs.get("persist", True):
            downsample_state.save(result)
        return result

    def fake_upgrade_refresh(_artists, **kwargs):
        result = upgrade_state.RefreshResult(
            [upgrade_candidate], ["Artist"], {}, True)
        if kwargs.get("persist", True):
            upgrade_state.save(result)
        return result

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists",
                        fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists",
                        fake_upgrade_refresh)
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *args: badge_calls.append(args))
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)

    def cancel_during_library_scan(ad, token, partial_only, hidden):
        job.cancel_requested = True
        return ad.name, ad.name, [], "artist-id", []

    monkeypatch.setattr(flows, "_scan_library_artist", cancel_during_library_scan)
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert downsample_state.load()["complete"] is False
    assert upgrade_state.load()["complete"] is False
    assert badge_calls == []


def test_incomplete_baseline_scan_does_not_publish_quality_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "DOWNSAMPLE_STATE_FILE", tmp_path / "downsample.json")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    downsample_candidate = _candidate(album_dir)
    upgrade_candidate = {
        "title": "Album",
        "artist": "Artist",
        "detail": "16-bit/44.1kHz -> 24-bit/96kHz",
        "payload": {"album_id": "up1", "year": "2024", "cover": ""},
    }
    badge_calls = []

    def fake_downsample_refresh(_artists, **kwargs):
        result = downsample_state.RefreshResult(
            [downsample_candidate], ["Artist"], {}, True)
        if kwargs.get("persist", True):
            downsample_state.save(result)
        return result

    def fake_upgrade_refresh(_artists, **kwargs):
        result = upgrade_state.RefreshResult(
            [upgrade_candidate], ["Artist"], {}, True)
        if kwargs.get("persist", True):
            upgrade_state.save(result)
        return result

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists",
                        fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists",
                        fake_upgrade_refresh)
    monkeypatch.setattr(flows.review_badges, "set_ready",
                        lambda *args: badge_calls.append(args))
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
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("artist failed")),
    )

    flows.scan_library(jm.Job(title="baseline"), "tok")

    assert downsample_state.load()["complete"] is False
    assert upgrade_state.load()["complete"] is False
    assert badge_calls == []


def test_resumed_baseline_scan_can_complete_saved_library_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, library_scan_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE",
                        tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE",
                        tmp_path / "checkpoint.json")
    good_dir = tmp_path / "Good"
    next_dir = tmp_path / "Next"
    good_dir.mkdir()
    next_dir.mkdir()
    cfg.SCAN_CHECKPOINT_FILE.write_text(json.dumps({
        "missing": {
            "scanned": ["Good"],
            "candidates": [],
            "seen": {"good-id": ["good-album"]},
            "artists": {
                "Good": {
                    "fingerprint": "good-fp",
                    "candidates": [],
                    "artist_id": "good-id",
                    "catalog_ids": ["good-album"],
                },
            },
        },
    }), encoding="utf-8")

    def fake_downsample_refresh(_artists, **_kwargs):
        return downsample_state.RefreshResult(
            [], ["Good", "Next"], {}, True,
            {"Good": "good-fp", "Next": "next-fp"},
        )

    def fake_upgrade_refresh(_artists, **_kwargs):
        return upgrade_state.RefreshResult(
            [], ["Good", "Next"], {}, True,
            {"Good": "good-fp", "Next": "next-fp"},
        )

    monkeypatch.setattr(flows, "list_library_artists", lambda: [good_dir, next_dir])
    monkeypatch.setattr(flows, "artist_fingerprint",
                        lambda path: f"{path.name.lower()}-fp", raising=False)
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists",
                        fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists",
                        fake_upgrade_refresh)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, token, partial_only, hidden:
            (ad.name, ad.name, [], "next-id", ["next-album"]),
    )

    flows.scan_library(jm.Job(title="baseline"), "tok")

    state = library_scan_state.kind_state("missing")
    assert state["complete"] is True
    assert sorted(state["artists"]) == ["Good", "Next"]
    assert flows.scan_checkpoint.load("missing") is None


def test_resumed_baseline_rescans_checkpoint_entries_without_artist_snapshot(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, library_scan_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE",
                        tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE",
                        tmp_path / "checkpoint.json")
    good_dir = tmp_path / "Good"
    good_dir.mkdir()
    cfg.SCAN_CHECKPOINT_FILE.write_text(json.dumps({
        "missing": {
            "scanned": ["Good"],
            "candidates": [{
                "kind": "album",
                "title": "Stale",
                "artist": "Good",
                "detail": "old checkpoint candidate",
                "payload": {"album_id": "old", "_artist_dir": "Good"},
                "selected": False,
            }],
            "seen": {"good-id": ["old"]},
        },
    }), encoding="utf-8")
    scanned = []

    monkeypatch.setattr(flows, "list_library_artists", lambda: [good_dir])
    monkeypatch.setattr(flows, "artist_fingerprint",
                        lambda path: f"{path.name.lower()}-fp", raising=False)
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult(
            [], ["Good"], {}, True, {"Good": "good-fp"}),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *_a, **_k: upgrade_state.RefreshResult(
            [], ["Good"], {}, True, {"Good": "good-fp"}),
    )
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)

    def fake_scan_artist(ad, token, partial_only, hidden):
        scanned.append(ad.name)
        return ad.name, ad.name, [], "good-id", ["fresh"]

    monkeypatch.setattr(flows, "_scan_library_artist", fake_scan_artist)
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    state = library_scan_state.kind_state("missing")
    assert scanned == ["Good"]
    assert job.candidates == []
    assert state["complete"] is True
    assert sorted(state["artists"]) == ["Good"]
    assert flows.scan_checkpoint.load("missing") is None


def test_scan_library_reuses_unchanged_artist_snapshot(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    saved_candidate = {
        "kind": "album",
        "title": "Saved Album",
        "artist": "Artist",
        "detail": "2024 · CD quality · 10 tracks",
        "payload": {"album_id": "saved", "_artist_dir": "Artist"},
        "selected": False,
    }
    hidden = hidden_mod.load()
    library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": "same",
                "candidates": [saved_candidate],
                "artist_id": "artist-id",
                "catalog_ids": ["saved"],
            },
        },
        complete=True,
        hidden_signature=library_scan_state.hidden_signature(
            hidden, hidden_mod.SCOPE_MISSING),
        quality_sig=library_scan_state.quality_signature(),
    )
    downsample_skip = []
    upgrade_skip = []

    def fake_downsample_refresh(artists, **kwargs):
        downsample_skip.append(kwargs.get("skip_unchanged"))
        return SimpleNamespace(
            complete=True,
            candidates=[],
            artists_scanned=[],
            errors={},
            fingerprints={},
            hidden_signature="",
        )

    def fake_upgrade_refresh(artists, **kwargs):
        upgrade_skip.append(kwargs.get("skip_unchanged"))
        return SimpleNamespace(
            complete=True,
            candidates=[],
            artists_scanned=[],
            errors={},
            fingerprints={},
            hidden_signature="",
        )

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows, "artist_fingerprint", lambda _path: "same",
                        raising=False)
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists",
                        fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists",
                        fake_upgrade_refresh)
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
            AssertionError("unchanged artist should be reused")),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert downsample_skip == [True]
    assert upgrade_skip == [True]
    assert [c["title"] for c in job.candidates] == ["Saved Album"]


def test_scan_library_force_full_ignores_saved_artist_snapshot(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import library_scan_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    hidden = hidden_mod.load()
    library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": "same",
                "candidates": [],
                "artist_id": "artist-id",
                "catalog_ids": [],
            },
        },
        complete=True,
        hidden_signature=library_scan_state.hidden_signature(
            hidden, hidden_mod.SCOPE_MISSING),
        quality_sig=library_scan_state.quality_signature(),
    )
    downsample_skip = []
    upgrade_skip = []
    scanned = []

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows, "artist_fingerprint", lambda _path: "same",
                        raising=False)
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda _artists, **kwargs: downsample_skip.append(
            kwargs.get("skip_unchanged")) or SimpleNamespace(
                complete=True,
                candidates=[],
                artists_scanned=[],
                errors={},
                fingerprints={},
                hidden_signature="",
            ),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda _artists, **kwargs: upgrade_skip.append(
            kwargs.get("skip_unchanged")) or SimpleNamespace(
                complete=True,
                candidates=[],
                artists_scanned=[],
                errors={},
                fingerprints={},
                hidden_signature="",
            ),
    )
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
        lambda ad, token, partial_only, hidden: (
            scanned.append(ad.name) or (ad.name, ad.name, [], "artist-id", [])),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok", force_full=True)

    assert downsample_skip == [False]
    assert upgrade_skip == [False]
    assert scanned == ["Artist"]


def test_scan_library_skips_upgrade_refresh_when_upgrade_disabled(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()

    monkeypatch.setattr(cfg, "UPGRADE_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda _artists, **_kwargs: downsample_state.RefreshResult([], ["Artist"], {}, True),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("upgrade refresh should be skipped")),
    )
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
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", []),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert job.summary == "No missing albums found for artists in your library."


def test_library_artist_scan_ignores_single_store_when_suppression_off(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    seen = []

    def fake_find_missing(*args, **kwargs):
        seen.append(kwargs.get("single_store"))
        return SimpleNamespace(
            artist_id="id1",
            artist_name="Artist",
            gaps=[],
            catalog=[],
            catalog_incomplete=False,
        )

    monkeypatch.setattr(cfg, "SUPPRESS_SINGLE_TRACK_GAPS", False)
    monkeypatch.setattr(flows, "find_missing_for_artist", fake_find_missing)

    flows._scan_library_artist(artist_dir, "tok", False, {"single": {"old": {}}})

    assert seen == [None]


def test_new_release_scan_keeps_incomplete_rebaseline_truthful(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artists = [tmp_path / name for name in ("Good", "Unresolved", "Short", "Error")]
    for artist in artists:
        artist.mkdir()

    def fake_find(name, **_kwargs):
        if name == "Error":
            raise RuntimeError("temporary failure")
        if name == "Unresolved":
            return SimpleNamespace(artist_id=None, fetch_failed=False,
                                   current_ids=[], new_gaps=[], artist_name=None)
        return SimpleNamespace(
            artist_id=name,
            fetch_failed=name == "Short",
            current_ids=["album"],
            new_gaps=[],
            artist_name=name,
        )

    marked = {}
    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda: artists)
    monkeypatch.setattr(flows, "find_new_releases_for_artist", fake_find)
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"Good": []},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT) - 1,
    })
    monkeypatch.setattr(
        flows.new_releases_mod,
        "mark_run",
        lambda _seen, **kwargs: marked.update(kwargs) or True,
    )
    job = jm.Job(title="new releases")

    flows.scan_new_releases(job, "tok")

    assert marked["complete"] is False
    assert marked["baseline_limit"] is None
    assert "3 artists couldn't be checked" in job.summary
    assert "Recorded a fresh baseline" not in job.summary


def test_new_release_scan_reports_state_save_failure(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artist = tmp_path / "Artist"
    artist.mkdir()
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist])
    monkeypatch.setattr(
        flows,
        "find_new_releases_for_artist",
        lambda *_args, **_kwargs: SimpleNamespace(
            artist_id="artist-id",
            fetch_failed=False,
            current_ids=["album"],
            new_gaps=[],
            artist_name="Artist",
        ),
    )
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"artist-id": []},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT),
    })
    monkeypatch.setattr(flows.new_releases_mod, "mark_run", lambda *_a, **_k: False)
    job = jm.Job(title="new releases")

    flows.scan_new_releases(job, "tok")

    assert "couldn't be saved" in job.summary


def test_cancelled_new_release_scan_keeps_failed_artist_count(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    artists = [tmp_path / name for name in ("Broken", "Stopped")]
    for artist in artists:
        artist.mkdir()

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda: artists)
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"existing": []},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT),
    })

    def fake_find(name, **_kwargs):
        if name == "Broken":
            raise RuntimeError("temporary failure")
        return SimpleNamespace(
            artist_id=name,
            fetch_failed=False,
            current_ids=[],
            new_gaps=[],
            artist_name=name,
        )

    monkeypatch.setattr(flows, "find_new_releases_for_artist", fake_find)
    job = jm.Job(title="new releases")
    job.push_progress = lambda *_a, **_k: setattr(
        job, "cancel_requested", True)

    flows.scan_new_releases(job, "tok")

    assert "1 artist couldn't be checked before the stop" in job.summary
    assert job.execute_args["_unchecked_artists"] == 1

def test_incomplete_baseline_scan_summary_reports_unchecked_artists(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE",
                        tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE",
                        tmp_path / "checkpoint.json")
    good_dir = tmp_path / "Good"
    bad_dir = tmp_path / "Bad"
    good_dir.mkdir()
    bad_dir.mkdir()

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [good_dir, bad_dir])
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult([], [], {}, True),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *_a, **_k: upgrade_state.RefreshResult([], [], {}, True),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)

    def fake_scan_artist(ad, token, partial_only, hidden):
        if ad.name == "Bad":
            raise RuntimeError("artist failed")
        return ad.name, ad.name, [], f"{ad.name}-id", [f"{ad.name}-album"]

    monkeypatch.setattr(flows, "_scan_library_artist", fake_scan_artist)

    job = jm.Job(title="baseline")
    flows.scan_library(job, "tok")

    # One artist errored, so the checkpoint stays and the crawl was partial.
    # the summary must say so instead of a clean-sounding definitive total.
    assert "1 artist" in job.summary
    assert "resume" in job.summary.lower()
    assert "Upgrade and Downsample results were not updated" in job.summary


def test_scan_signature_covers_candidate_shaping_settings(monkeypatch):
    """The cheap refresh reuses saved candidates while the signature matches;
    so every setting that changes WHICH candidates a scan yields has to be in
    it, or Settings changes leave stale gap/missing lists."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import library_scan_state as lss

    base = lss.quality_signature()
    for name in ("SUPPRESS_SINGLE_TRACK_GAPS", "EXCLUDE_LIVE_ALBUMS"):
        with monkeypatch.context() as mctx:
            mctx.setattr(cfg, name, not getattr(cfg, name))
            assert lss.quality_signature() != base, name
    for name in ("ARTIST_CATALOG_LIMIT", "MISSING_ALBUMS_MIN_TRACKS"):
        with monkeypatch.context() as mctx:
            mctx.setattr(cfg, name, int(getattr(cfg, name)) + 1)
            assert lss.quality_signature() != base, name


def test_web_lyric_retry_marks_write_errors_failed(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import lyrics
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    tracks = [tmp_path / f"{index}.flac" for index in range(3)]
    for track in tracks:
        track.write_bytes(b"synthetic")
    monkeypatch.setattr(cfg, "LYRIC_RETRY_FILE", tmp_path / "retry.json")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(lyrics.lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(
        lyrics.lyric_fetch,
        "fetch_for_paths",
        lambda *_args, **_kwargs: {"write-error": 3},
    )
    monkeypatch.setattr(
        lyrics,
        "_refresh_lyric_retry",
        lambda _paths: lyrics.save_lyric_retry([]),
    )
    lyrics.save_lyric_retry([str(track) for track in tracks])
    job = jm.Job(title="lyrics retry")

    flows.run_lyric_retry(job)

    assert job.status == jm.JobStatus.FAILED
    assert "3 failed" in job.summary
    assert "All 3 retried tracks resolved" not in job.summary
