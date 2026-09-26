from copy import deepcopy
from pathlib import Path

import pytest

from qobuz_librarian.library.candidate_premise import (
    CandidateStale,
    capture,
    validate,
    validate_container,
    validate_premise,
)


def _music_album(tmp_path: Path, monkeypatch):
    from qobuz_librarian import config

    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "01.flac"
    track.write_bytes(b"reviewed audio bytes")
    monkeypatch.setattr(config, "MUSIC_ROOT", root)
    return root, album, track


def test_album_candidate_receipt_accepts_unchanged_and_rejects_changed_bytes(
        tmp_path, monkeypatch):
    _root, album, track = _music_album(tmp_path, monkeypatch)
    premise = capture("upgrade", album)
    assert premise is not None
    candidate = {
        "kind": "upgrade",
        "payload": {"album_dir": str(album), "_premise": premise},
    }

    assert validate(candidate) == premise

    track.write_bytes(b"different audio bytes")
    with pytest.raises(CandidateStale) as stale:
        validate(candidate)
    assert stale.value.cause == "changed"
    # A pick saved before receipts existed is refused as well.
    del candidate["payload"]["_premise"]
    with pytest.raises(CandidateStale):
        validate(candidate)


def test_library_gap_fill_row_seals_the_files_a_whole_album_fill_moves(
        tmp_path, monkeypatch):
    from qobuz_librarian.library.discovery import AlbumGap
    from qobuz_librarian.web import flows

    _root, album, track = _music_album(tmp_path, monkeypatch)
    qobuz_album = {"id": "A1", "title": "Album", "artist": {"name": "Artist"},
                   "tracks_count": 10}
    # Discovery hands over Qobuz's track dicts, which carry no local path.
    gap = AlbumGap(qobuz_album, album, [{"id": n} for n in range(2, 11)],
                   [{"id": 1, "title": "One", "track_number": 1}])

    spec = flows._gap_candidate_spec(gap, "Artist")

    assert set(spec["payload"]["_gap_fill_receipts"]) == {track.name}


def _renumber_mount_ids(premise):
    current = deepcopy(premise)
    receipt = current["receipt"]
    mount_ids = {
        identity[6]
        for identity in receipt["path_generations"]
    } | {
        identity[6]
        for identity in receipt["directory_generations"].values()
    }
    replacements = {
        mount_id: mount_id + 100_000 + index
        for index, mount_id in enumerate(sorted(mount_ids))
    }
    for identity in receipt["path_generations"]:
        identity[6] = replacements[identity[6]]
    for identity in receipt["directory_generations"].values():
        identity[6] = replacements[identity[6]]
    return current


def test_candidate_receipt_survives_mount_namespace_renumbering(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.candidate_premise as candidate_premise

    _root, album, _track = _music_album(tmp_path, monkeypatch)
    premise = capture("downsample", album)
    candidate = {
        "kind": "downsample",
        "payload": {"album_dir": str(album), "_premise": premise},
    }
    current = _renumber_mount_ids(premise)
    monkeypatch.setattr(
        candidate_premise, "capture", lambda _kind, _path: current)

    assert validate(candidate) == current
    assert validate_premise(premise) == current
    # A new mount boundary at the album is not the reviewed topology.
    for identity in (current["receipt"]["path_generations"][-1],
                     *current["receipt"]["directory_generations"].values()):
        identity[6] = -1
    with pytest.raises(CandidateStale):
        validate(candidate)


def test_missing_candidate_binds_the_reviewed_artist_tree(tmp_path, monkeypatch):
    import qobuz_librarian.library.candidate_premise as candidate_premise

    root, album, _track = _music_album(tmp_path, monkeypatch)
    artist = album.parent
    premise = capture("missing", artist)
    candidate = {
        "kind": "album",
        "payload": {
            "album_id": "new-album",
            "_artist_dir_path": str(artist),
            "_premise": premise,
        },
    }
    assert validate(candidate)["kind"] == "missing"

    added = root / "Artist" / "New Album"
    added.mkdir()
    (added / "01.flac").write_bytes(b"now owned")
    with pytest.raises(CandidateStale):
        validate(candidate)

    # A batch may download another album by this artist first. The directory
    # itself is still the same trusted container even though its tree changed.
    assert validate_container(candidate) == premise

    current_generations = deepcopy(premise["receipt"]["path_generations"])
    replacements = {
        mount_id: mount_id + 200_000 + index
        for index, mount_id in enumerate(sorted({
            identity[6] for identity in current_generations
        }))
    }
    for identity in current_generations:
        identity[6] = replacements[identity[6]]
    monkeypatch.setattr(
        candidate_premise,
        "_path_directory_generations",
        lambda _descriptors: current_generations,
    )
    assert validate_container(candidate) == premise

    current_generations[-1][6] = max(
        identity[6] for identity in current_generations
    ) + 1
    with pytest.raises(CandidateStale):
        validate_container(candidate)


def test_approval_refuses_files_changed_between_its_two_passes(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from qobuz_librarian.library import candidate_premise
    from qobuz_librarian.web import jobs, routes_jobs, runtime

    _root, album, track = _music_album(tmp_path, monkeypatch)
    premise = candidate_premise.capture("missing", album.parent)
    job = jobs.Job(title="review", execute_kind="library",
                   status=jobs.JobStatus.AWAITING_REVIEW)
    for i in range(3):
        job.add_candidate(kind="album", title=str(i), selected=True, payload={
            "album_id": str(i), "_artist_dir_path": str(album.parent),
            "_premise": premise,
        })
    original = deepcopy(job.candidates)
    captures = []
    capture = candidate_premise.capture

    def record(kind, path):
        captures.append(path)
        return capture(kind, path)

    async def authorize(_access):
        assert captures == [str(album.parent)]
        track.write_bytes(b"changed while authorization was pending")
        return SimpleNamespace(token="tok", generation=0)

    async def form():
        return {}

    async def run_in_executor(_executor, fn):
        return fn()

    monkeypatch.setattr(routes_jobs.asyncio, "get_running_loop", lambda: SimpleNamespace(
        run_in_executor=run_in_executor))
    monkeypatch.setattr(candidate_premise, "capture", record)
    monkeypatch.setattr(runtime, "_authorize_qobuz_for_web", authorize)
    monkeypatch.setattr(runtime, "_lock_busy_response", lambda _r: None)
    monkeypatch.setattr(runtime, "_web_writes_paused", lambda: False)
    monkeypatch.setattr(jobs.registry, "get", lambda _id: job)
    monkeypatch.setattr(runtime.flows, "owned_missing_candidate_ids", lambda *_a, **_k: set())
    admitted = []
    monkeypatch.setattr(jobs, "approve", lambda *_a, **_k: admitted.append(True))
    response = asyncio.run(routes_jobs.job_approve(SimpleNamespace(form=form), job.id))
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert not admitted
    assert job.status == jobs.JobStatus.AWAITING_REVIEW
    assert job.candidates == original
