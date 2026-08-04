
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import staging as staging_module
from qobuz_librarian.integrations.staging import (
    FileGroup,
    ParkedGroup,
    StagingReferenceStatus,
    capture_file,
    inspect_staging_group_reference,
    load_file_group,
    park_trees,
    quarantine_file,
)


def _owner(marker):
    return {
        "operation_id": marker * 64,
        "item_id": chr(ord(marker) + 1) * 64,
    }


@pytest.fixture
def staging(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", root)
    return root


def test_owned_staging_group_reference_requires_exact_manifested_contents(
        staging):
    owner = _owner("c")
    source = staging / "Artist - Album"
    source.mkdir(parents=True)
    track = source / "01.flac"
    track.write_bytes(b"original")
    records = []
    group = park_trees(
        [source],
        "failed import",
        owner=owner,
        on_intent=records.append,
    )
    assert group is not None

    matched = inspect_staging_group_reference(records[0], owner)
    assert matched.status is StagingReferenceStatus.MATCH
    assert isinstance(matched.evidence, ParkedGroup)

    parked_track = group.trees[0].path / "01.flac"
    parked_track.write_bytes(b"replacement")
    assert inspect_staging_group_reference(
        records[0], owner).status is StagingReferenceStatus.CHANGED

    wrong_owner = _owner("e")
    assert inspect_staging_group_reference(
        records[0], wrong_owner).status is StagingReferenceStatus.UNAVAILABLE

    loose = staging / "loose.flac"
    loose.write_bytes(b"audio")
    file_records = []
    file_group = quarantine_file(
        capture_file(loose),
        owner=owner,
        on_intent=file_records.append,
    )
    assert file_group is not None
    assert file_group.durable is True
    assert Path(file_group) == file_group.path
    file_match = inspect_staging_group_reference(file_records[0], owner)
    assert file_match.status is StagingReferenceStatus.MATCH
    assert isinstance(file_match.evidence, FileGroup)

    file_group_path = file_match.evidence.path
    file_manifest = file_group_path / ".qobuz-librarian-retry.json"
    replacement_manifest = file_group_path / ".replacement-manifest"
    replacement_manifest.write_bytes(file_manifest.read_bytes())
    replacement_manifest.chmod(0o600)
    replacement_manifest.replace(file_manifest)
    assert inspect_staging_group_reference(
        file_records[0], owner).status is StagingReferenceStatus.CHANGED


def test_quarantine_restores_a_public_replacement_raced_into_private_staging(
        staging, monkeypatch):
    source = staging / "run" / "song.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"sealed audio")
    replacement = source.with_name("incoming.flac")
    replacement.write_bytes(b"late replacement")
    receipt = capture_file(source)
    real_rename = staging_module._rename_noreplace
    raced = False

    def replace_before_move(source_fd, source_name, destination_fd, destination_name):
        nonlocal raced
        if not raced and source_name == source.name and destination_name == source.name:
            replacement.replace(source)
            raced = True
        return real_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(staging_module, "_rename_noreplace", replace_before_move)

    assert quarantine_file(receipt) is None
    assert raced is True
    assert source.read_bytes() == b"late replacement"
    retry_root = staging / cfg.BEETS_RETRY_DIR
    assert not list(retry_root.rglob(source.name))


def test_quarantine_reports_a_retained_file_when_directory_sync_is_uncertain(
        staging, monkeypatch):
    source = staging / "run" / "song.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"sealed audio")
    receipt = capture_file(source)

    def fail_directory_sync(_path):
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(
        staging_module, "_fsync_staging_directory", fail_directory_sync)

    result = quarantine_file(receipt)

    assert result is not None
    assert result.durable is False
    assert result.path.read_bytes() == b"sealed audio"
    assert not source.exists()
    assert load_file_group(result.path.parent) is not None


def test_restore_group_raises_when_an_incomplete_restore_cannot_roll_back(
        staging, monkeypatch):
    first = staging / "Artist" / "First"
    second = staging / "Artist" / "Second"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "01.flac").write_bytes(b"first")
    (second / "01.flac").write_bytes(b"second")
    group = park_trees([first, second], "quality", kind="quality")
    assert group is not None

    real_move = staging_module._move_tree
    calls = 0

    def fail_second_move_and_rollback(receipt, destination, destination_name):
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            return None
        return real_move(receipt, destination, destination_name)

    monkeypatch.setattr(
        staging_module, "_move_tree", fail_second_move_and_rollback)

    with pytest.raises(OSError, match="partially restored"):
        staging_module.restore_group(group)

    public_tracks = [first / "01.flac", second / "01.flac"]
    private_tracks = [tree.path / "01.flac" for tree in group.trees]
    assert sum(path.exists() for path in public_tracks) == 1
    assert sum(path.exists() for path in private_tracks) == 1
    assert {
        path.read_bytes()
        for path in public_tracks + private_tracks
        if path.exists()
    } == {b"first", b"second"}
