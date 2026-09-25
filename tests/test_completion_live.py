import os

import pytest

from qobuz_librarian import completion_live
from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    CompletionExpectation,
    CompletionScope,
    DownloadCounts,
    DownloadCoverage,
    ManagedImportEvidence,
    ManagedMapping,
    QualityTarget,
    RecoveryOwner,
    StagedBinding,
)
from qobuz_librarian.completion_live import (
    LiveCompletionUnavailable,
    capture_new_album_completion,
)


def _identity5(path):
    value = path.stat(follow_symlinks=False)
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _identity6(path):
    value = path.stat(follow_symlinks=False)
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _test_flac(payload=b""):
    streaminfo = bytearray(34)
    packed = (44_100 << 44) | (1 << 41) | (15 << 36) | 1
    streaminfo[10:18] = packed.to_bytes(8, "big")
    return b"fLaC" + b"\x80\x00\x00\x22" + bytes(streaminfo) + payload


class _StableManagedLease:
    def __init__(self, evidence):
        self._evidence = evidence

    def revalidate(self):
        return self._evidence


def test_new_album_completion_revalidates_and_refuses_track_replacement(
        tmp_path, monkeypatch):
    def audio_ok(_path, *, descriptor=None):
        return descriptor is not None

    monkeypatch.setattr(
        completion_live,
        "flac_audio_ok",
        audio_ok,
    )
    music = tmp_path / "music"
    staging = tmp_path / "staging" / "Artist - Album"
    album = music / "Artist" / "Album"
    first = staging / "first.flac"
    second = staging / "second.flac"
    first.parent.mkdir(parents=True)
    first.write_bytes(_test_flac(b"one"))
    second.write_bytes(_test_flac(b"two"))
    first_source = (str(first), _identity6(first))
    second_source = (str(second), _identity6(second))

    first_landing = album / "Disc 1" / "01.flac"
    second_landing = album / "Disc 2" / "01.flac"
    first_landing.parent.mkdir(parents=True)
    second_landing.parent.mkdir(parents=True)
    os.replace(first, first_landing)
    os.replace(second, second_landing)
    (album / "cover.jpg").write_bytes(b"cover")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)

    owner = RecoveryOwner("a" * 64, "b" * 64)
    slots = ("qobuz:101", "qobuz:202")
    expectation = CompletionExpectation(
        album_id="303",
        scope=CompletionScope.ALBUM,
        catalogue_slots=slots,
        requested_slots=slots,
        quality_targets=(
            QualityTarget(slots[0], 16, 44_100),
            QualityTarget(slots[1], 16, 44_100),
        ),
    )
    download = DownloadCoverage(
        album_id="303",
        catalogue_slots=slots,
        requested_slots=slots,
        bindings=(
            StagedBinding(slots[1], *second_source),
            StagedBinding(slots[0], *first_source),
        ),
        counts=DownloadCounts(),
    )
    managed = ManagedImportEvidence(
        owner=owner,
        library_root=str(music),
        library_root_identity=_identity5(music),
        album_path="Artist/Album",
        album_identity=_identity5(album),
        manifest_hash="c" * 64,
        mappings=(
            ManagedMapping(
                slots[1],
                second_source[0],
                second_source[1],
                "Artist/Album/Disc 2/01.flac",
                _identity5(second_landing),
            ),
            ManagedMapping(
                slots[0],
                first_source[0],
                first_source[1],
                "Artist/Album/Disc 1/01.flac",
                _identity5(first_landing),
            ),
        ),
    )

    managed_lease = _StableManagedLease(managed)
    with capture_new_album_completion(
        managed_lease=managed_lease,
        owner=owner,
        expectation=expectation,
        download=download,
    ) as live:
        evidence = live.evidence
        assert tuple(item.slot for item in evidence.landings) == slots
        assert evidence.inventory.audio == evidence.landings
        assert tuple(
            (item.served_bits, item.served_rate)
            for item in evidence.quality
        ) == ((16, 44_100), (16, 44_100))
        assert live.revalidate() == evidence

        replacement = tmp_path / "replacement.flac"
        replacement.write_bytes(_test_flac(b"replacement"))
        os.replace(replacement, first_landing)
        with pytest.raises(LiveCompletionUnavailable):
            _ = live.evidence
        with pytest.raises(LiveCompletionUnavailable):
            live.revalidate()

    second_landing.write_bytes(_test_flac(b"writable after close"))
