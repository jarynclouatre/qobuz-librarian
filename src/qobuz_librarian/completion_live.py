"""Live, descriptor-held completion proof for one new full album."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import weakref
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
from qobuz_librarian.interrupts import run_sigint_deferred
from qobuz_librarian.library.backup import (
    _backup_source_is_public,
    _exact_tree_snapshot,
    _fsync_directory_fds,
    _fsync_exact_tree,
    _held_snapshot_files_intact,
    _hold_snapshot_files,
    _open_backup_source,
)


class LiveCompletionUnavailable(OSError):
    """The live library state cannot prove exact completion."""

    def __init__(self, message, *, failure=None):
        super().__init__(message)
        self.failure = failure


def _prefer_cleanup_error(current, candidate):
    if candidate is None:
        return current
    if current is None or (
        isinstance(current, Exception)
        and not isinstance(candidate, Exception)
    ):
        return candidate
    return current


def _run_sigint_deferred(callback):
    """Finish one raw-descriptor handoff before delivering Ctrl-C."""
    return run_sigint_deferred(
        callback, unavailable=LiveCompletionUnavailable,
        detail="interrupt-safe completion access is unavailable")


class _LiveCompletionLifetime:
    """One-shot owner for the tree descriptors and writer exclusions."""

    def __init__(self):
        self.held = {}
        self.chain = []
        self.closed = False
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            if self.closed:
                return None
            error = None
            while self.held:
                _relative, item = self.held.popitem()
                lease = item.get("lease")
                item["lease"] = None
                if lease is not None:
                    try:
                        lease.close()
                    except BaseException as exc:
                        error = _prefer_cleanup_error(error, exc)
                descriptor = item.get("descriptor")
                item["descriptor"] = None
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except BaseException as exc:
                        error = _prefer_cleanup_error(error, exc)
                parents = item.get("parents")
                if isinstance(parents, list):
                    while parents:
                        descriptor = parents.pop()
                        try:
                            os.close(descriptor)
                        except BaseException as exc:
                            error = _prefer_cleanup_error(error, exc)
            while self.chain:
                descriptor = self.chain.pop()
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    error = _prefer_cleanup_error(error, exc)
            self.closed = True
            return error


def _finalize_live_completion_lifetime(lifetime):
    try:
        _run_sigint_deferred(lifetime.close)
    except LiveCompletionUnavailable:
        try:
            lifetime.close()
        except BaseException:
            pass
    except BaseException:
        pass


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
    """A live exact-album proof whose filesystem authority can be rechecked."""

    def __init__(
        self,
        *,
        managed_lease,
        owner,
        expectation,
        download,
        managed,
        public_album,
        root,
        parts,
        snapshot,
        slot_relatives,
        lifetime,
    ):
        self._managed_lease = managed_lease
        self._owner = owner
        self._expectation = expectation
        self._download = download
        self._managed = managed
        self._public_album = public_album
        self._root = root
        self._parts = parts
        self._snapshot = snapshot
        self._slot_relatives = slot_relatives
        self._lifetime = weakref.ref(lifetime)
        self._finalizer = weakref.finalize(
            self, _finalize_live_completion_lifetime, lifetime)
        self._evidence = None
        self._lock = threading.RLock()
        self._closed = False
        self._closing = False
        self._failed = False

    @property
    def evidence(self):
        return self.revalidate()

    def _namespace_intact(self):
        lifetime = self._lifetime()
        if lifetime is None or lifetime.closed or not lifetime.chain:
            return False
        try:
            root_value = os.fstat(lifetime.chain[0])
            album_value = os.fstat(lifetime.chain[-1])
            return (
                _backup_source_is_public(
                    self._root, self._parts, lifetime.chain)
                and (int(root_value.st_dev), int(root_value.st_ino))
                == self._managed.library_root_identity[:2]
                and _identity(album_value) == self._managed.album_identity
            )
        except (OSError, TypeError, ValueError):
            return False

    def _build_evidence(self):
        lifetime = self._lifetime()
        if lifetime is None or lifetime.closed:
            raise LiveCompletionUnavailable(
                "live album descriptors are unavailable")
        mapping_by_slot = {
            mapping.slot: mapping for mapping in self._managed.mappings
        }
        landings = []
        quality = []
        for slot, relative in self._slot_relatives:
            item = lifetime.held.get(relative)
            mapping = mapping_by_slot[slot]
            if not isinstance(item, dict):
                raise LiveCompletionUnavailable(
                    "managed album audio changed")
            descriptor = item.get("descriptor")
            try:
                value = os.fstat(descriptor)
            except (OSError, TypeError, ValueError):
                raise LiveCompletionUnavailable(
                    "managed album audio changed") from None
            identity = _identity(value)
            receipt = self._snapshot["files"].get(relative)
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
            landings.append(landing)
            quality.append(TrackQuality(
                slot=slot,
                path=mapping.destination_path,
                identity=identity,
                sha256=digest,
                served_bits=bits,
                served_rate=rate,
            ))
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
        lifetime = self._lifetime()
        if lifetime is None or lifetime.closed or not lifetime.chain:
            raise LiveCompletionUnavailable(
                "live completion descriptors are unavailable")
        if self._managed_lease.revalidate() != self._managed:
            raise LiveCompletionUnavailable(
                "managed completion evidence changed")
        album_fd = lifetime.chain[-1]
        if (
            not self._namespace_intact()
            or _exact_tree_snapshot(album_fd) != self._snapshot
            or not _held_snapshot_files_intact(
                album_fd, self._snapshot, lifetime.held)
        ):
            raise LiveCompletionUnavailable(
                "managed album changed during completion verification")
        evidence = (
            self._evidence
            if self._evidence is not None
            else self._build_evidence()
        )
        if (
            self._evidence is not None
            and evidence != self._evidence
            or not self._namespace_intact()
            or _exact_tree_snapshot(album_fd) != self._snapshot
            or not _held_snapshot_files_intact(
                album_fd, self._snapshot, lifetime.held)
        ):
            raise LiveCompletionUnavailable(
                "managed album changed during completion verification")
        if self._managed_lease.revalidate() != self._managed:
            raise LiveCompletionUnavailable(
                "managed completion evidence changed")
        return evidence

    def _revalidate_locked(self):
        if self._closed or self._closing or self._failed:
            raise LiveCompletionUnavailable(
                "live completion authority is unavailable")
        try:
            evidence = self._revalidate_once()
            if self._evidence is None:
                self._evidence = evidence
            return evidence
        except BaseException as exc:
            self._failed = True
            if isinstance(exc, LiveCompletionUnavailable):
                raise
            if not isinstance(exc, Exception):
                raise
            raise LiveCompletionUnavailable(
                "live completion could not be revalidated") from None

    def revalidate(self):
        with self._lock:
            return self._revalidate_locked()

    def _close_locked(self):
        if self._closed:
            return
        self._closing = True
        lifetime = self._lifetime()
        if lifetime is None:
            self._closed = True
            if self._finalizer.alive:
                self._finalizer.detach()
            return
        error = None
        try:
            error = _run_sigint_deferred(lifetime.close)
        finally:
            self._closed = lifetime.closed
            if self._closed and self._finalizer.alive:
                self._finalizer.detach()
        if error is not None:
            raise error.with_traceback(error.__traceback__)

    def close(self):
        with self._lock:
            self._close_locked()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, exc, _traceback):
        try:
            self.close()
        except BaseException as close_exc:
            if exc is None:
                raise
            if not isinstance(close_exc, Exception):
                if hasattr(close_exc, "add_note"):
                    close_exc.add_note(
                        f"completion body also failed: {exc}")
                raise
            if hasattr(exc, "add_note"):
                exc.add_note(
                    f"live completion cleanup also failed: {close_exc}")
        return False


def _capture_new_album_completion(
    *, managed_lease, owner, expectation, download,
):
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

    lifetime = _LiveCompletionLifetime()
    lease = None
    try:
        def open_and_adopt_chain():
            source = _open_backup_source(
                Path(root_path).joinpath(*album_parts))
            if source is None:
                raise LiveCompletionUnavailable(
                    "managed album namespace could not be opened")
            public_album, root, parts, chain = source
            lifetime.chain = chain
            return public_album, root, parts

        public_album, root, parts = _run_sigint_deferred(
            open_and_adopt_chain)
        if os.fspath(root) != managed.library_root or parts != album_parts:
            raise LiveCompletionUnavailable(
                "managed album namespace changed")
        if not _backup_source_is_public(root, parts, lifetime.chain):
            raise LiveCompletionUnavailable(
                "managed album namespace changed")
        root_value = os.fstat(lifetime.chain[0])
        album_value = os.fstat(lifetime.chain[-1])
        if (
            (int(root_value.st_dev), int(root_value.st_ino))
            != managed.library_root_identity[:2]
            or _identity(album_value) != managed.album_identity
        ):
            raise LiveCompletionUnavailable(
                "managed album identity changed")
        snapshot = _exact_tree_snapshot(lifetime.chain[-1])
        if snapshot is None:
            raise LiveCompletionUnavailable(
                "managed album contains an unsafe entry")
        actual_audio = {
            relative
            for relative in snapshot["files"]
            if PurePosixPath(relative).suffix.lower() in cfg.AUDIO_EXTS
        }
        if actual_audio != {relative for _slot, relative in slot_relatives}:
            raise LiveCompletionUnavailable(
                "managed album audio inventory is not exact",
                failure=CompletionFailure.INVENTORY_MISMATCH,
            )
        _hold_snapshot_files(
            lifetime.chain[-1], snapshot, lifetime.held)
        if not _fsync_exact_tree(
            lifetime.chain[-1], snapshot, lifetime.held
        ) or not _fsync_directory_fds(*lifetime.chain):
            raise LiveCompletionUnavailable(
                "managed album could not be made durable")
        lease = LiveCompletionLease(
            managed_lease=managed_lease,
            owner=owner,
            expectation=expectation,
            download=download,
            managed=managed,
            public_album=public_album,
            root=root,
            parts=parts,
            snapshot=snapshot,
            slot_relatives=slot_relatives,
            lifetime=lifetime,
        )
        lease.revalidate()
        return lease
    except BaseException as exc:
        cleanup_error = None
        try:
            if lease is not None:
                lease.close()
            else:
                cleanup_error = _run_sigint_deferred(lifetime.close)
        except BaseException as close_exc:
            cleanup_error = close_exc
        if cleanup_error is not None and hasattr(exc, "add_note"):
            exc.add_note(
                f"live completion cleanup also failed: {cleanup_error}")
        if cleanup_error is not None and not isinstance(
            cleanup_error, Exception
        ):
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        if isinstance(exc, LiveCompletionUnavailable):
            raise
        if not isinstance(exc, Exception):
            raise
        raise LiveCompletionUnavailable(
            "live completion evidence could not be captured") from None


def capture_new_album_completion(
    *, managed_lease, owner, expectation, download,
):
    """Capture a live proof for a brand-new full-album managed import."""
    return _capture_new_album_completion(
        managed_lease=managed_lease,
        owner=owner,
        expectation=expectation,
        download=download,
    )
