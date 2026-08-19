import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian.library.downsample import DownsampleCandidate
from qobuz_librarian.web import jobs as jm


def _allow_legacy_candidate_execution(monkeypatch):
    from qobuz_librarian.library import candidate_premise

    monkeypatch.setattr(
        candidate_premise,
        "validate",
        lambda candidate: {
            "kind": candidate_premise.expected_kind(candidate),
            "receipt": None,
        },
    )


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
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", [], {}),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert calls == [["Artist"]]


def test_a_completed_scan_leaves_a_collection_snapshot(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import collection_snapshot, scanner
    from qobuz_librarian.web import flows

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "COLLECTION_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(
        scanner, "read_album_dir",
        lambda d, walk_errors=None: [{"title": "One", "tracknumber": 1,
                                      "discnumber": 1, "isrc": "GB0000000001",
                                      "mb_trackid": "", "album": "Album"}])
    scanner.clear_scan_caches()

    artist_dir = music / "Artist"
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda _kind: True)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows, "_scan_library_artist",
        lambda ad, token, partial_only, hidden: (
            ad.name, ad.name, [], "artist-7", [], {"Album": "album-9"}),
    )

    flows.scan_library(jm.Job(title="baseline"), "tok")

    saved = json.loads(collection_snapshot.latest_path().read_text())
    assert saved["counts"] == {"artists": 1, "albums": 1, "tracks": 1}
    artist = saved["artists"][0]
    # The ids the scan resolved are what lets a restore ask for the exact album.
    assert artist["qobuz_artist_id"] == "artist-7"
    assert artist["albums"][0]["qobuz_album_id"] == "album-9"


def test_scan_library_reuses_checkpoint_from_interrupted_publication(monkeypatch):
    from qobuz_librarian.web import flows

    previous = {
        "generation": 4,
        "catalog_complete": True,
        "latest_attempt": {"id": 10, "status": "complete"},
        "outputs": {},
    }
    captured = {}
    monkeypatch.setattr(flows.generation_state, "load", lambda: previous)
    monkeypatch.setattr(
        flows.generation_state,
        "library_publication_incomplete",
        lambda state: state is previous,
    )
    monkeypatch.setattr(flows.generation_state, "begin_attempt", lambda: 11)
    monkeypatch.setattr(flows.generation_state, "revision", lambda: 12)

    def _capture(_job, _token, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(flows, "_scan_library_impl", _capture)

    flows.scan_library(jm.Job(title="baseline"), "tok")

    assert captured["allow_checkpoint_resume"] is True
    assert captured["attempt_id"] == 11


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




def test_execute_downsamples_refreshes_affected_artist_state(tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    _allow_legacy_candidate_execution(monkeypatch)

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    refreshed = []
    suppressed = []

    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.downsample_dir",
        lambda *a, **k: {"resampled": 1, "saved_bytes": 100, "errors": 0})
    monkeypatch.setattr(flows.downsample_state, "update_artist",
                        lambda artist_dir, **kwargs: refreshed.append(artist_dir.name)
                        or SimpleNamespace(complete=True, candidates=[]))
    monkeypatch.setattr(
        flows.upgrade_state,
        "remove_album_dir",
        lambda path: suppressed.append(path) or True,
    )
    monkeypatch.setattr(
        flows,
        "_refresh_upgrade_artist_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Downsample must not contact Qobuz")
        ),
    )
    monkeypatch.setattr(flows.review_badges, "set_ready", lambda *a, **k: None)
    job = jm.Job(title="downsample")

    flows.execute_downsamples(job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_dir": str(album_dir)},
    }])

    assert refreshed == ["Artist"]
    assert suppressed == [album_dir]




def test_execute_upgrades_refreshes_upgrade_and_downsample_state(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    _allow_legacy_candidate_execution(monkeypatch)

    refreshed_upgrade = []
    refreshed_downsample = []
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    monkeypatch.setattr(flows, "get_album",
                        lambda album_id, token: {"id": album_id, "title": "Album"})
    outcome = {"imported": True, "result": "downloaded", "dir": album_dir}
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: dict(outcome))
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

    outcome.update({
        "result": "partial",
        "upgrade_unverified": True,
    })
    attention_job = jm.Job(title="upgrade", status=jm.JobStatus.RUNNING)
    attention_job.execute_args = {
        "quality_signature": flows.upgrade_state.quality_signature(),
    }

    flows.execute_upgrades(attention_job, [{
        "artist": "Artist",
        "title": "Album",
        "payload": {"album_id": "alb-1"},
    }], "tok")

    assert attention_job.status is jm.JobStatus.FAILED
    assert attention_job.attention == "backup"


def test_web_unverified_repair_with_local_change_marks_remote_views_stale(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    _allow_legacy_candidate_execution(monkeypatch)

    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    marked = []

    monkeypatch.setattr(
        "qobuz_librarian.modes.repair.repair_album_dir",
        lambda *args, **kwargs: {
            "imported": True,
            "n_ok": 1,
            "n_fail": 0,
            "dir": album_dir,
            "repair_unverified": True,
        },
    )
    monkeypatch.setattr(flows.generation_state, "load", lambda: {})
    monkeypatch.setattr(
        flows.generation_state,
        "output_is_current",
        lambda surface, **kwargs: surface in {"library", "new_releases"},
    )
    monkeypatch.setattr(
        flows.generation_state,
        "mark_output_status",
        lambda surface, status, **kwargs: marked.append((surface, status)) or True,
    )
    monkeypatch.setattr(
        flows,
        "_refresh_upgrade_artist_state",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        flows,
        "_refresh_downsample_artist_state",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(flows, "_return_qobuz_review_picks", lambda *a, **k: True)
    monkeypatch.setattr(flows.time, "sleep", lambda *_: None)
    job = jm.Job(title="repair", status=jm.JobStatus.RUNNING)

    flows.execute_repairs(job, [{
        "kind": "repair",
        "artist": "Artist",
        "title": "Album",
        "payload": {
            "album_dir": str(album_dir),
            "artist_name": "Artist",
            "verified_truncated": [],
        },
    }], "tok")

    assert marked == [("library", "stale"), ("new_releases", "stale")]


def test_whole_album_repair_rechecks_download_access_before_backup(
        tmp_path, monkeypatch):
    from qobuz_librarian.api import client
    from qobuz_librarian.api.auth import DownloaderNotReady, credentials_from_values
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.library import backup
    from qobuz_librarian.web import flows

    credentials = credentials_from_values("", "token", source="streamrip")
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    backup_calls = []

    monkeypatch.setattr(
        flows,
        "get_album",
        lambda album_id, token: {
            "id": album_id,
            "title": "Album",
            "artist": {"name": "Artist"},
            "tracks": {"items": []},
        },
    )
    monkeypatch.setattr(beets, "capture_beets_album_entries", lambda path: [])
    monkeypatch.setattr(
        backup,
        "backup_album_dir",
        lambda *args, **kwargs: backup_calls.append(True),
    )
    monkeypatch.setattr(
        client,
        "authorize_qobuz_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(DownloaderNotReady()),
    )

    with pytest.raises(DownloaderNotReady):
        flows._redownload_damaged_album(
            {
                "album_id": "album",
                "album_dir": str(album_dir),
                "artist_name": "Artist",
            },
            credentials.token,
        )

    assert backup_calls == []

def test_execute_upgrades_marks_partial_cap_before_refresh(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import decision
    from qobuz_librarian.web import flows

    _allow_legacy_candidate_execution(monkeypatch)

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
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", [], {}),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert calls == [["Artist"]]




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


def test_resumed_baseline_scan_can_complete_saved_library_state(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, library_scan_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library_scan.json")
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE", tmp_path / "checkpoint.json")
    good_dir = tmp_path / "Good"
    next_dir = tmp_path / "Next"
    good_dir.mkdir()
    next_dir.mkdir()
    cfg.SCAN_CHECKPOINT_FILE.write_text(
        json.dumps(
            {
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
            }
        ),
        encoding="utf-8",
    )

    def fake_downsample_refresh(_artists, **_kwargs):
        return downsample_state.RefreshResult(
            [],
            ["Good", "Next"],
            {},
            True,
            {"Good": "good-fp", "Next": "next-fp"},
        )

    def fake_upgrade_refresh(_artists, **_kwargs):
        return upgrade_state.RefreshResult(
            [],
            ["Good", "Next"],
            {},
            True,
            {"Good": "good-fp", "Next": "next-fp"},
        )

    monkeypatch.setattr(flows, "list_library_artists", lambda: [good_dir, next_dir])
    monkeypatch.setattr(
        flows, "artist_fingerprint", lambda path: f"{path.name.lower()}-fp", raising=False
    )
    monkeypatch.setattr(flows.downsample_state, "refresh_for_artists", fake_downsample_refresh)
    monkeypatch.setattr(flows.upgrade_state, "refresh_for_artists", fake_upgrade_refresh)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "next-id", ["next-album"], {}),
    )

    job = jm.Job(title="baseline")
    flows.scan_library(job, "tok")

    state = library_scan_state.kind_state("missing")
    assert state["complete"] is True
    assert sorted(state["artists"]) == ["Good", "Next"]
    assert job.execute_args["_library_review_generation"] == state["generation"]
    assert flows.scan_checkpoint.load("missing") is None


def test_complete_scan_keeps_resume_checkpoint_when_saved_state_fails(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import downsample_state, library_scan_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows, "artist_fingerprint", lambda _path: "fp", raising=False)
    monkeypatch.setattr(
        flows.downsample_state,
        "refresh_for_artists",
        lambda *_a, **_k: downsample_state.RefreshResult(
            [], ["Artist"], {}, True, {"Artist": "fp"}
        ),
    )
    monkeypatch.setattr(
        flows.upgrade_state,
        "refresh_for_artists",
        lambda *_a, **_k: upgrade_state.RefreshResult([], ["Artist"], {}, True, {"Artist": "fp"}),
    )
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda _kind: None)
    saved_checkpoints = []
    cleared = []
    monkeypatch.setattr(
        flows.scan_checkpoint,
        "save",
        lambda *args, **kwargs: saved_checkpoints.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda kind: cleared.append(kind))
    monkeypatch.setattr(library_scan_state, "save_kind", lambda *_a, **_k: None)
    monkeypatch.setattr(flows, "_record_last_scan", lambda: None)
    monkeypatch.setattr(flows, "_flag_new_since_last_scan", lambda *a, **k: None)
    monkeypatch.setattr(flows, "flush_resolve_cache", lambda: None)
    monkeypatch.setattr(flows.new_releases_mod, "is_baseline_complete", lambda: True)
    gap = SimpleNamespace(
        qobuz_album={"id": "album-1", "title": "Album", "tracks_count": 1},
        on_disk_dir=None,
        missing_count=0,
    )
    monkeypatch.setattr(
        flows,
        "_scan_library_artist",
        lambda ad, *_a, **_k: (ad.name, ad.name, [gap], "artist-id",
                               ["album-1"], {}),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert [c["payload"]["album_id"] for c in job.candidates] == ["album-1"]
    assert saved_checkpoints
    assert cleared == []
    assert "saved scan state couldn't be written" in job.summary


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
        return ad.name, ad.name, [], "good-id", ["fresh"], {}

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
        )

    def fake_upgrade_refresh(artists, **kwargs):
        upgrade_skip.append(kwargs.get("skip_unchanged"))
        return SimpleNamespace(
            complete=True,
            candidates=[],
            artists_scanned=[],
            errors={},
            fingerprints={},
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

    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
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
            scanned.append(ad.name)
            or (ad.name, ad.name, [], "artist-id", [], {})),
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
        lambda ad, token, partial_only, hidden: (ad.name, ad.name, [], "artist-id", [], {}),
    )
    job = jm.Job(title="baseline")

    flows.scan_library(job, "tok")

    assert not job.candidates


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
            complete=[],
            singles=[],
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

    artists = [tmp_path / name for name in ("Good", "No Match", "Short", "Error")]
    for artist in artists:
        artist.mkdir()

    def fake_find(name, **_kwargs):
        if name == "Error":
            raise RuntimeError("temporary failure")
        if name == "No Match":
            # Qobuz has no artist by that name: settled, so it belongs in the
            # skipped note rather than the retryable count.
            return SimpleNamespace(artist_id=None, fetch_failed=False,
                                   unresolved=True, current_ids=[],
                                   new_gaps=[], artist_name=None)
        return SimpleNamespace(
            artist_id=name,
            fetch_failed=name == "Short",
            unresolved=False,
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
    assert "2 artists couldn't be checked" in job.summary
    assert "No Match" in job.summary
    assert job.status is jm.JobStatus.FAILED
    # job.summary above already names the reason and the remedy; job.error
    # would only restate it.
    assert not job.error


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
    monkeypatch.setattr(flows, "list_library_artists", lambda: artists)
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
    assert "couldn't be checked" not in job.summary


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
    assert job.status is jm.JobStatus.FAILED
    # job.summary above already names the reason and the remedy; job.error
    # would only restate it.
    assert not job.error


def test_partial_new_release_find_stays_reviewable(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    good = tmp_path / "Good"
    failed = tmp_path / "Failed"
    good.mkdir()
    failed.mkdir()

    def _find(name, **_kwargs):
        if name == "Failed":
            raise RuntimeError("temporary failure")
        return SimpleNamespace(
            artist_id="artist-id",
            fetch_failed=False,
            current_ids=["old", "new"],
            new_gaps=[object()],
            artist_name="Good",
        )

    monkeypatch.setattr(cfg, "ARTIST_SCAN_WORKERS", 1)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [good, failed])
    monkeypatch.setattr(flows, "find_new_releases_for_artist", _find)
    monkeypatch.setattr(flows.new_releases_mod, "load", lambda: {
        "seen": {"artist-id": ["old"]},
        "baseline_limit": int(cfg.ARTIST_CATALOG_LIMIT),
    })
    monkeypatch.setattr(
        flows.new_releases_mod,
        "mark_run",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        flows,
        "_add_gap_candidate",
        lambda job, *_args, **_kwargs: job.add_candidate(
            kind="album",
            title="New album",
            artist="Good",
            detail="2026",
            payload={"album_id": "new"},
            selected=False,
        ),
    )
    job = jm.Job(title="new releases")

    flows.scan_new_releases(job, "tok")

    assert len(job.candidates) == 1
    assert job.status is jm.JobStatus.PENDING
    assert job.error is None
    assert "1 artist couldn't be checked" in job.summary




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
    monkeypatch.setattr(flows, "list_library_artists", lambda: [good])
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


def test_a_new_release_check_with_nothing_parked_still_parks_its_own_review(
        tmp_path, monkeypatch):
    from qobuz_librarian.web import flows

    _stub_new_release_scan(monkeypatch, tmp_path, "fresh")
    job = jm.Job(title="New-release check")
    job.execute_kind = "new_releases"

    flows.scan_new_releases(job, "tok")

    assert [c["title"] for c in job.candidates] == ["fresh"]
    assert job.status is jm.JobStatus.PENDING
