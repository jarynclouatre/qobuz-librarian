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
    with pytest.raises(CandidateStale, match="local files changed"):
        validate(candidate)


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


def test_candidate_receipt_rejects_changed_mount_boundary_topology(
        tmp_path, monkeypatch):
    import qobuz_librarian.library.candidate_premise as candidate_premise

    _root, album, _track = _music_album(tmp_path, monkeypatch)
    premise = capture("upgrade", album)
    candidate = {
        "kind": "upgrade",
        "payload": {"album_dir": str(album), "_premise": premise},
    }
    current = _renumber_mount_ids(premise)
    new_mount_id = max(
        identity[6] for identity in current["receipt"]["path_generations"]
    ) + 1
    # A newly mounted album has one boundary at its path entry and throughout
    # its tree.  The directory and byte identities remain unchanged, but this
    # is not the reviewed container topology and must be refused.
    current["receipt"]["path_generations"][-1][6] = new_mount_id
    for identity in current["receipt"]["directory_generations"].values():
        identity[6] = new_mount_id
    monkeypatch.setattr(
        candidate_premise, "capture", lambda _kind, _path: current)

    with pytest.raises(CandidateStale, match="local files changed"):
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


def test_legacy_candidate_without_receipt_fails_closed(tmp_path, monkeypatch):
    _root, album, _track = _music_album(tmp_path, monkeypatch)
    candidate = {
        "kind": "downsample",
        "payload": {"album_dir": str(album)},
    }
    with pytest.raises(CandidateStale, match="predates local file receipts"):
        validate(candidate)
