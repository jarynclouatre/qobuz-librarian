"""Completion proof for one new full album, checked again before each step."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath

from qobuz_librarian import config as cfg
from qobuz_librarian.completion import (
    AlbumInventory,
    CompletionExpectation,
    CompletionFailure,
    CompletionScope,
    DownloadCounts,
    DownloadCoverage,
    LandingReceipt,
    ManagedImportEvidence,
    ManagedMapping,
    QualityTarget,
    RecoveryOwner,
    StagedBinding,
    TrackQuality,
    _valid_absolute_path,
    _valid_identity,
    assess_completion,
    authoritative_slot,
    authoritative_slots,
    normalise_album_id,
)
from qobuz_librarian.integrations.downsample_engine import (
    PROBE_BYTES,
    parse_flac_info,
)
from qobuz_librarian.integrations.rip import flac_audio_ok
from qobuz_librarian.library.backup import (
    album_tree_identity,
    capture_album_source_receipt,
    capture_album_tree_identity,
)
from qobuz_librarian.safefile import fsync_dir, fsync_tree


class LiveCompletionUnavailable(OSError):
    """The live library state cannot prove exact completion."""

    def __init__(self, message, *, failure=None):
        super().__init__(message)
        self.failure = failure


def _identity(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


def _strict_relative_parts(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or len(path.parts) > 32
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        return None
    return path.parts


def _metaflac_value(descriptor, option):
    try:
        result = subprocess.run(
            [
                "metaflac",
                option,
                f"/proc/self/fd/{descriptor}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            pass_fds=(descriptor,),
            env=cfg.child_env(),
        )
        value = result.stdout.strip()
        if result.returncode != 0 or not value.isascii() or not value.isdecimal():
            return 0
        parsed = int(value)
        return parsed if parsed > 0 else 0
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return 0


def _descriptor_quality(descriptor):
    if flac_audio_ok(
        Path(f"/proc/self/fd/{descriptor}"), descriptor=descriptor
    ) is not True:
        return 0, 0
    try:
        rate, bits = parse_flac_info(os.pread(descriptor, PROBE_BYTES, 0))
    except OSError:
        return 0, 0
    if not bits:
        bits = _metaflac_value(descriptor, "--show-bps")
    if not rate:
        rate = _metaflac_value(descriptor, "--show-sample-rate")
    return rate, bits


def _validate_inputs(owner, expectation, download):
    if (
        type(owner) is not RecoveryOwner
        or not isinstance(owner.operation_id, str)
        or not isinstance(owner.item_id, str)
        or _HEX_64.fullmatch(owner.operation_id) is None
        or _HEX_64.fullmatch(owner.item_id) is None
        or type(expectation) is not CompletionExpectation
        or expectation.scope is not CompletionScope.ALBUM
        or normalise_album_id(expectation.album_id) != expectation.album_id
        or not authoritative_slots(expectation.catalogue_slots)
        or expectation.requested_slots != expectation.catalogue_slots
        or expectation.baseline_slots
        or type(expectation.quality_targets) is not tuple
        or not all(
            type(target) is QualityTarget
            for target in expectation.quality_targets
        )
        or tuple(
            target.slot for target in expectation.quality_targets
        ) != expectation.catalogue_slots
        or any(
            type(value) is not int or value <= 0
            for target in expectation.quality_targets
            for value in (target.bits, target.rate)
        )
        or type(download) is not DownloadCoverage
        or download.album_id != expectation.album_id
        or download.catalogue_slots != expectation.catalogue_slots
        or download.requested_slots != expectation.requested_slots
        or type(download.bindings) is not tuple
        or not all(
            type(binding) is StagedBinding
            and authoritative_slot(binding.slot)
            and _valid_absolute_path(binding.path)
            and _valid_identity(binding.identity, 6)
            for binding in download.bindings
        )
        or len({binding.slot for binding in download.bindings})
        != len(download.bindings)
        or set(binding.slot for binding in download.bindings)
        != set(expectation.catalogue_slots)
        or type(download.counts) is not DownloadCounts
        or any(
            type(value) is not int or value != 0
            for value in (
                download.counts.failed,
                download.counts.lossy,
                download.counts.broken,
                download.counts.unmatched,
                download.counts.extra,
            )
        )
    ):
        raise LiveCompletionUnavailable(
            "completion inputs are outside the new full-album proof scope",
            failure=CompletionFailure.MALFORMED,
        )


def _prepare_managed(managed_lease, owner, expectation, download):
    try:
        managed = managed_lease.revalidate()
    except AttributeError:
        raise LiveCompletionUnavailable(
            "managed completion authority is unavailable",
            failure=CompletionFailure.MANAGED_IMPORT_REQUIRED,
        ) from None
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        raise LiveCompletionUnavailable(
            "managed completion authority could not be revalidated",
            failure=CompletionFailure.MANAGED_IMPORT_REQUIRED,
        ) from None
    if (
        type(managed) is not ManagedImportEvidence
        or managed.owner != owner
        or not _valid_absolute_path(managed.library_root)
        or not _valid_identity(managed.library_root_identity, 5)
        or not _valid_identity(managed.album_identity, 5)
        or not isinstance(managed.manifest_hash, str)
        or _HEX_64.fullmatch(managed.manifest_hash) is None
        or type(managed.mappings) is not tuple
        or not all(
            type(mapping) is ManagedMapping
            and authoritative_slot(mapping.slot)
            and _valid_absolute_path(mapping.source_path)
            and _valid_identity(mapping.source_identity, 6)
            and _valid_identity(mapping.destination_identity, 5)
            for mapping in managed.mappings
        )
        or len({mapping.slot for mapping in managed.mappings})
        != len(managed.mappings)
        or set(mapping.slot for mapping in managed.mappings)
        != set(expectation.catalogue_slots)
    ):
        raise LiveCompletionUnavailable(
            "managed completion evidence does not match the request",
            failure=CompletionFailure.MANAGED_IMPORT_REQUIRED,
        )

    album_parts = _strict_relative_parts(managed.album_path)
    if album_parts is None:
        raise LiveCompletionUnavailable(
            "managed album path is malformed",
            failure=CompletionFailure.DESTINATION_MISMATCH,
        )
    bindings = {binding.slot: binding for binding in download.bindings}
    mappings = {mapping.slot: mapping for mapping in managed.mappings}
    slot_relatives = []
    seen = set()
    for slot in expectation.catalogue_slots:
        mapping = mappings[slot]
        binding = bindings[slot]
        destination_parts = _strict_relative_parts(mapping.destination_path)
        if (
            mapping.source_path != binding.path
            or mapping.source_identity != binding.identity
            or destination_parts is None
            or len(destination_parts) <= len(album_parts)
            or destination_parts[:len(album_parts)] != album_parts
        ):
            raise LiveCompletionUnavailable(
                "managed track mapping does not match the exact download",
                failure=CompletionFailure.DESTINATION_MISMATCH,
            )
        relative = PurePosixPath(
            *destination_parts[len(album_parts):]
        ).as_posix()
        if relative in seen or PurePosixPath(relative).suffix.lower() != ".flac":
            raise LiveCompletionUnavailable(
                "managed album audio is outside the exact FLAC proof scope",
                failure=CompletionFailure.DESTINATION_MISMATCH,
            )
        seen.add(relative)
        slot_relatives.append((slot, relative))
    return managed, tuple(album_parts), tuple(slot_relatives)


class LiveCompletionLease:
    """An album's completion proof that can be checked again: the album
    must still hold the same files, unwritten, in the same folders."""

    def __init__(
        self,
        *,
        managed_lease,
        owner,
        expectation,
        download,
        managed,
        album,
        receipt,
        slot_relatives,
    ):
        self._managed_lease = managed_lease
        self._owner = owner
        self._expectation = expectation
        self._download = download
        self._managed = managed
        self._album = album
        self._receipt = receipt
        self._tree = album_tree_identity(receipt)["tree"]
        self._slot_relatives = slot_relatives
        self._evidence = None
        self._closed = False
        self._failed = False

    @property
    def evidence(self):
        return self.revalidate()

    def _album_unchanged(self):
        try:
            root_value = os.lstat(self._managed.library_root)
            album_value = os.lstat(self._album)
        except OSError:
            return False
        current = capture_album_tree_identity(self._album)
        return (
            current is not None
            and current["tree"] == self._tree
            and (int(root_value.st_dev), int(root_value.st_ino))
            == self._managed.library_root_identity[:2]
            and _identity(album_value) == self._managed.album_identity
        )

    def _landing(self, slot, relative, mapping):
        try:
            descriptor = os.open(
                self._album / relative,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            raise LiveCompletionUnavailable(
                "managed album audio changed") from None
        try:
            value = os.fstat(descriptor)
            identity = _identity(value)
            receipt = self._receipt["tree"]["files"].get(relative)
            if (
                not stat.S_ISREG(value.st_mode)
                or int(value.st_nlink) != 1
                or identity != mapping.destination_identity
                or not isinstance(receipt, dict)
                or receipt.get("identity")
                != [stat.S_IFREG, identity[0], identity[1]]
                or receipt.get("size") != identity[2]
                or receipt.get("mtime_ns") != identity[3]
                or receipt.get("changed_ns") != identity[4]
            ):
                raise LiveCompletionUnavailable(
                    "managed album audio identity changed")
            digest = receipt.get("sha256")
            rate, bits = _descriptor_quality(descriptor)
        finally:
            os.close(descriptor)
        if not isinstance(digest, str) or not rate or not bits:
            raise LiveCompletionUnavailable(
                "managed album audio quality could not be proved",
                failure=CompletionFailure.QUALITY_MISMATCH,
            )
        landing = LandingReceipt(
            slot=slot,
            path=mapping.destination_path,
            identity=identity,
            sha256=digest,
        )
        return landing, TrackQuality(
            slot=slot,
            path=mapping.destination_path,
            identity=identity,
            sha256=digest,
            served_bits=bits,
            served_rate=rate,
        )

    def _build_evidence(self):
        mapping_by_slot = {
            mapping.slot: mapping for mapping in self._managed.mappings
        }
        landings = []
        quality = []
        for slot, relative in self._slot_relatives:
            landing, track = self._landing(
                slot, relative, mapping_by_slot[slot])
            landings.append(landing)
            quality.append(track)
        inventory = AlbumInventory(
            path=self._managed.album_path,
            identity=self._managed.album_identity,
            audio=tuple(landings),
        )
        assessment = assess_completion(
            owner=self._owner,
            expectation=self._expectation,
            download=self._download,
            managed=self._managed,
            landings=tuple(landings),
            baseline=(),
            inventory=inventory,
            quality=tuple(quality),
            recovery_references=(),
        )
        if assessment.evidence is None:
            raise LiveCompletionUnavailable(
                "exact album completion could not be proved",
                failure=assessment.failure,
            )
        return assessment.evidence

    def _revalidate_once(self):
        if self._evidence is None:
            if self._managed_lease.revalidate() != self._managed:
                raise LiveCompletionUnavailable(
                    "managed completion evidence changed")
            if not self._album_unchanged():
                raise LiveCompletionUnavailable(
                    "managed album changed during completion verification")
            evidence = self._build_evidence()
        else:
            evidence = self._evidence
        if not self._album_unchanged():
            raise LiveCompletionUnavailable(
                "managed album changed during completion verification")
        if self._managed_lease.revalidate() != self._managed:
            raise LiveCompletionUnavailable(
                "managed completion evidence changed")
        return evidence

    def revalidate(self):
        if self._closed or self._failed:
            raise LiveCompletionUnavailable(
                "live completion authority is unavailable")
        try:
            evidence = self._revalidate_once()
        except BaseException as exc:
            self._failed = True
            if isinstance(exc, LiveCompletionUnavailable):
                raise
            if not isinstance(exc, Exception):
                raise
            raise LiveCompletionUnavailable(
                "live completion could not be revalidated") from None
        if self._evidence is None:
            self._evidence = evidence
        return evidence

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
        return False


def capture_new_album_completion(
    *, managed_lease, owner, expectation, download,
):
    """Capture a proof for a brand-new full-album managed import.

    Every file of the album is read once for its receipt, and the album,
    each folder and every folder above it up to the music root are flushed
    to disk before the proof is handed out."""
    _validate_inputs(owner, expectation, download)
    managed, album_parts, slot_relatives = _prepare_managed(
        managed_lease, owner, expectation, download)
    try:
        root_path = os.path.abspath(os.fspath(cfg.MUSIC_ROOT))
    except (OSError, TypeError, ValueError):
        raise LiveCompletionUnavailable(
            "configured music root is unavailable") from None
    if managed.library_root != root_path:
        raise LiveCompletionUnavailable(
            "managed library root changed")
    album = Path(root_path).joinpath(*album_parts)
    receipt = capture_album_source_receipt(album)
    if receipt is None:
        raise LiveCompletionUnavailable(
            "managed album could not be read exactly")
    actual_audio = {
        relative
        for relative in receipt["tree"]["files"]
        if PurePosixPath(relative).suffix.lower() in cfg.AUDIO_EXTS
    }
    if actual_audio != {relative for _slot, relative in slot_relatives}:
        raise LiveCompletionUnavailable(
            "managed album audio inventory is not exact",
            failure=CompletionFailure.INVENTORY_MISMATCH,
        )
    above = [Path(root_path).joinpath(*album_parts[:depth])
             for depth in range(len(album_parts) - 1)]
    if not fsync_tree(album) or not all(map(fsync_dir, above)):
        raise LiveCompletionUnavailable(
            "managed album could not be made durable")
    lease = LiveCompletionLease(
        managed_lease=managed_lease,
        owner=owner,
        expectation=expectation,
        download=download,
        managed=managed,
        album=album,
        receipt=receipt,
        slot_relatives=slot_relatives,
    )
    lease.revalidate()
    return lease
